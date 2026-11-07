"""Continuous probing: a schedule per check, results into the bucket store, a loop in a thread."""

import asyncio
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable, Sequence

from hydraping.core.checkers.dns import DNSChecker
from hydraping.core.checkers.http import HTTPChecker
from hydraping.core.checkers.icmp import ICMPChecker
from hydraping.core.checkers.tcp import TCPChecker
from hydraping.core.checkers.udp import UDPChecker
from hydraping.core.config import Config
from hydraping.core.models import (
    CHECK_TYPE_PRIORITY,
    CheckResult,
    CheckType,
    DomainEndpoint,
    Endpoint,
    HTTPEndpoint,
    IPEndpoint,
    IPPortEndpoint,
    Measurement,
    UDPPortEndpoint,
    _is_ip_address,
)
from hydraping.core.snapshot import EndpointSnapshot, EngineSnapshot, LayerView
from hydraping.core.store import BucketStore

logger = logging.getLogger(__name__)

Probe = Callable[[], Awaitable[Sequence[Measurement]]]
OnResults = Callable[[Sequence[CheckResult]], None]


class ProbeSchedule:
    """Start ``probe`` every ``interval`` seconds without waiting for the previous one.

    The probe owns its timeout: it must come back within ``timeout`` seconds, as failed
    measurements when the target does not answer. The schedule adds two guards around that:

    - At most ``ceil(timeout / interval) + 1`` probes are in flight. When the cap is reached, the
      tick is skipped and nothing is recorded, so a probe that ignores its timeout shows up as a gap
      rather than as a growing pile of tasks.
    - A probe that finishes cancels every in-flight probe that started before it. The newest
      measurement is the one worth showing, and a slow older probe must not land on top of it.

    A cancelled probe records nothing. Every measurement a probe returns is stamped with the probe's
    start and end time and handed to ``on_results`` in one call, so the layers of one probe stay
    consistent with each other.
    """

    def __init__(
        self,
        name: str,
        probe: Probe,
        interval: float,
        timeout: float,
        on_results: OnResults,
    ):
        if interval <= 0:
            raise ValueError("interval must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.name = name
        self.probe = probe
        self.interval = interval
        self.timeout = timeout
        self.on_results = on_results
        self.max_in_flight = math.ceil(timeout / interval) + 1
        self._in_flight: dict[asyncio.Task, float] = {}
        self.skipped_ticks = 0

    @property
    def in_flight(self) -> int:
        return len(self._in_flight)

    async def run(self) -> None:
        """Tick until cancelled. On cancellation, cancel and await every in-flight probe."""
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        try:
            while True:
                if len(self._in_flight) < self.max_in_flight:
                    started_at = time.time()
                    task = asyncio.ensure_future(self._run_probe(started_at))
                    self._in_flight[task] = started_at
                else:
                    self.skipped_ticks += 1
                next_tick += self.interval
                now = loop.time()
                if next_tick < now:
                    # Late after a suspend or a long stall: resume the cadence from now instead of
                    # firing the missed ticks in a burst.
                    next_tick = now + self.interval
                await asyncio.sleep(next_tick - now)
        finally:
            tasks = list(self._in_flight)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_probe(self, started_at: float) -> None:
        task = asyncio.current_task()
        assert task is not None
        try:
            measurements = await self.probe()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Probe %s raised; the tick recorded nothing", self.name)
            return
        finally:
            self._in_flight.pop(task, None)

        finished_at = time.time()
        for other, other_started_at in list(self._in_flight.items()):
            if other_started_at < started_at:
                other.cancel()
        results = [
            CheckResult.from_measurement(measurement, started_at, finished_at)
            for measurement in measurements
        ]
        if results:
            self.on_results(results)


class Engine:
    """Run all probe schedules on an asyncio loop in a background thread.

    Frontends call ``start()``, read ``snapshot()`` from their own thread as often as they draw,
    and call ``stop()`` on exit. The engine owns the loop, so the frontend's main thread stays free
    for a GUI toolkit or for Rich.
    """

    STOP_TIMEOUT_SECONDS = 5.0

    def __init__(self, config: Config):
        self.config = config
        self.endpoints: list[Endpoint] = list(config.endpoints)
        checks = config.checks

        self.icmp_checker = ICMPChecker(timeout=checks.timeout_seconds)
        self.dns_checker = DNSChecker(
            timeout=checks.timeout_seconds,
            nameservers=config.dns.servers or None,
        )
        self.tcp_checker = TCPChecker(timeout=checks.timeout_seconds)
        self.udp_checker = UDPChecker(timeout=checks.timeout_seconds)
        self.http_checker = HTTPChecker(
            timeout=checks.timeout_seconds,
            success_status_max=checks.http_success_status_max,
        )

        self.store = BucketStore(len(self.endpoints), checks.bucket_seconds)
        self.schedules: list[ProbeSchedule] = []
        for index, endpoint in enumerate(self.endpoints):
            self.schedules.extend(self._schedules_for(index, endpoint))

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._started = threading.Event()

    # -- schedules ---------------------------------------------------------------------------

    def _schedules_for(self, index: int, endpoint: Endpoint) -> list[ProbeSchedule]:
        def schedule(check_type: CheckType, probe: Probe, label: str = "") -> ProbeSchedule:
            name = f"{endpoint.display_name} {check_type.value}{label}"
            return ProbeSchedule(
                name=name,
                probe=self._guarded(check_type, probe),
                interval=self.config.checks.probe_interval_seconds,
                timeout=self.config.checks.timeout_seconds,
                on_results=lambda results: self.store.apply(index, results),
            )

        schedules: list[ProbeSchedule] = []
        if isinstance(endpoint, IPEndpoint):
            schedules.append(schedule(CheckType.ICMP, self._icmp_probe(endpoint.ip)))
        elif isinstance(endpoint, IPPortEndpoint):
            schedules.append(schedule(CheckType.ICMP, self._icmp_probe(endpoint.ip)))
            schedules.append(schedule(CheckType.TCP, self._tcp_probe(endpoint.ip, endpoint.port)))
        elif isinstance(endpoint, UDPPortEndpoint):
            schedules.append(schedule(CheckType.ICMP, self._icmp_probe(endpoint.ip)))
            schedules.append(
                schedule(
                    CheckType.UDP,
                    self._udp_probe(endpoint.ip, endpoint.port, endpoint.probe_data),
                )
            )
        elif isinstance(endpoint, DomainEndpoint):
            schedules.append(
                schedule(CheckType.DNS, self._dns_probe(endpoint.domain, endpoint.ip_version))
            )
            schedules.append(schedule(CheckType.ICMP, self._icmp_probe(endpoint.domain)))
            ports = [endpoint.port] if endpoint.port_specified else [80, 443]
            for port in ports:
                schedules.append(
                    schedule(CheckType.TCP, self._tcp_probe(endpoint.domain, port), f":{port}")
                )
        elif isinstance(endpoint, HTTPEndpoint):
            schedules.append(
                schedule(CheckType.DNS, self._dns_probe(endpoint.host, endpoint.ip_version))
            )
            schedules.append(schedule(CheckType.ICMP, self._icmp_probe(endpoint.host)))
            schedules.append(schedule(CheckType.TCP, self._tcp_probe(endpoint.host, endpoint.port)))
            schedules.append(schedule(CheckType.HTTP, self._http_probe(endpoint.url)))
        else:
            raise TypeError(f"Unsupported endpoint type: {type(endpoint).__name__}")
        return schedules

    @staticmethod
    def _guarded(check_type: CheckType, probe: Probe) -> Probe:
        """Turn an unexpected exception in a checker into a failed measurement.

        A checker bug must show up as a red cell with its message, not as a silent gap.
        """

        async def guarded() -> Sequence[Measurement]:
            try:
                return await probe()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return [
                    Measurement(
                        check_type=check_type,
                        success=False,
                        error_message=f"Unexpected error: {e}",
                    )
                ]

        return guarded

    def _icmp_probe(self, target: str) -> Probe:
        async def probe() -> Sequence[Measurement]:
            icmp_target = target
            if not _is_ip_address(target):
                # Ping what DNS resolved, so the ICMP and DNS layers agree on the address.
                icmp_target = self.dns_checker.get_last_resolved_ip(target) or target
            return [await self.icmp_checker.check(icmp_target)]

        return probe

    def _dns_probe(self, target: str, ip_version: int | None) -> Probe:
        async def probe() -> Sequence[Measurement]:
            return [await self.dns_checker.check(target, ip_version)]

        return probe

    def _tcp_probe(self, host: str, port: int) -> Probe:
        async def probe() -> Sequence[Measurement]:
            return [await self.tcp_checker.check(host, port)]

        return probe

    def _udp_probe(self, host: str, port: int, probe_data: bytes) -> Probe:
        async def probe() -> Sequence[Measurement]:
            return [await self.udp_checker.check(host, port, probe_data)]

        return probe

    def _http_probe(self, url: str) -> Probe:
        async def probe() -> Sequence[Measurement]:
            return [await self.http_checker.check(url)]

        return probe

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        """Start the probing thread. Returns once the loop runs; a second call does nothing."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, name="hydraping-engine")
        self._thread.daemon = True
        self._thread.start()
        self._started.wait()

    def stop(self) -> None:
        """Cancel every probe, close the loop and join the thread."""
        if self._thread is None:
            return
        # The loop and the event are None when asyncio.run() failed before _main() ran; the thread
        # is then already on its way out and there is nothing to signal.
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass  # The loop closed between the check and the call.
        self._thread.join(self.STOP_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            raise RuntimeError("The engine thread did not stop in time")
        self._thread = None

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        finally:
            # Unblock start() even if the loop could not be created.
            self._started.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        tasks = [asyncio.ensure_future(schedule.run()) for schedule in self.schedules]
        self._started.set()
        try:
            await self._stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- reading -----------------------------------------------------------------------------

    def snapshot(self, columns: int, now: float | None = None) -> EngineSnapshot:
        """Return the state for one frame, with ``columns`` buckets per layer. Thread-safe."""
        if now is None:
            now = time.time()
        with self.store.lock:
            icmp_available = self.icmp_checker.is_available()
            rows = tuple(
                self._endpoint_snapshot(index, endpoint, columns, now, icmp_available)
                for index, endpoint in enumerate(self.endpoints)
            )
        return EngineSnapshot(
            taken_at=now,
            bucket_seconds=self.store.bucket_seconds,
            columns=columns,
            rows=rows,
            icmp_available=icmp_available,
        )

    def _endpoint_snapshot(
        self, index: int, endpoint: Endpoint, columns: int, now: float, icmp_available: bool
    ) -> EndpointSnapshot:
        history = self.store.endpoints[index]
        layers = {
            check_type: LayerView(
                check_type=check_type,
                latest=layer.latest,
                buckets=layer.view(now, columns),
            )
            for check_type, layer in history.layers.items()
        }
        primary = endpoint.get_primary_check_type()
        latest = {check_type: view.latest for check_type, view in layers.items()}
        return EndpointSnapshot(
            endpoint=endpoint,
            primary=primary,
            layers=layers,
            current=latest.get(primary),
            problems=tuple(problems_for(latest, icmp_available)),
        )


def problems_for(latest: dict[CheckType, CheckResult | None], icmp_available: bool) -> list[str]:
    """Return the problem lines for one endpoint from its latest result per layer.

    A failure below a layer that succeeded is noise (ICMP blocked by a firewall while HTTP works),
    so only failures at or above the highest successful layer are reported. ICMP failures are
    dropped while ICMP is unavailable on this machine; the frontend shows one line for that.
    """
    highest_success_level = -1
    for level, check_type in enumerate(CHECK_TYPE_PRIORITY):
        result = latest.get(check_type)
        if result is not None and result.success:
            highest_success_level = level
            break

    problems = []
    for level, check_type in enumerate(CHECK_TYPE_PRIORITY):
        result = latest.get(check_type)
        if result is None or result.success:
            continue
        if check_type == CheckType.ICMP and not icmp_available:
            continue
        if 0 <= highest_success_level < level:
            continue
        problems.append(f"{check_type.value.upper()}: {result.error_message}")
    return problems
