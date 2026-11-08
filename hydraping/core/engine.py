"""Continuous probing: a schedule per check, results into the bucket store, a loop in a thread."""

import asyncio
import dataclasses
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor

from hydraping.core.chains import HopState, current_result, diagnose_chain
from hydraping.core.checkers.connection import ConnectionChecker, ConnectionTarget, HTTPRequest
from hydraping.core.checkers.dns import DNSChecker
from hydraping.core.checkers.icmp import ICMPChecker
from hydraping.core.checkers.tailscale import TailscaleChecker
from hydraping.core.checkers.udp import UDPChecker
from hydraping.core.config import Chain, Config
from hydraping.core.interval import ProbeDurations, interval_for
from hydraping.core.models import (
    CHECK_TYPE_PRIORITY,
    CheckResult,
    CheckType,
    DNSServerEndpoint,
    DomainEndpoint,
    Endpoint,
    HTTPEndpoint,
    IPEndpoint,
    IPPortEndpoint,
    LayerKey,
    Measurement,
    TailscalePeerEndpoint,
    UDPPortEndpoint,
    _is_ip_address,
)
from hydraping.core.route import EgressInterfaces
from hydraping.core.snapshot import ChainSnapshot, EndpointSnapshot, EngineSnapshot, LayerView
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

    With ``max_interval`` set, ``interval`` is only the minimum: the schedule slows down to
    ``max_interval`` while its probes take long or fail, and speeds back up as they get fast again
    (see :mod:`hydraping.core.interval`). The in-flight cap follows the current interval. Without
    ``max_interval``, the interval is fixed.

    A cancelled probe records nothing and leaves the interval alone. Every measurement a probe
    returns is stamped with the probe's start and end time and handed to ``on_results`` in one call,
    so the layers of one probe stay consistent with each other.
    """

    def __init__(
        self,
        name: str,
        probe: Probe,
        interval: float,
        timeout: float,
        on_results: OnResults,
        max_interval: float | None = None,
    ):
        if interval <= 0:
            raise ValueError("interval must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_interval is not None and max_interval < interval:
            raise ValueError("max_interval must be at least interval")
        self.name = name
        self.probe = probe
        self.min_interval = interval
        self.max_interval = interval if max_interval is None else max_interval
        self.interval = interval
        self.timeout = timeout
        self.on_results = on_results
        self.durations = ProbeDurations()
        self._in_flight: dict[asyncio.Task, float] = {}
        self.skipped_ticks = 0

    @property
    def max_in_flight(self) -> int:
        return math.ceil(self.timeout / self.interval) + 1

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
            # A probe with no measurement (ICMP before the name resolved) says nothing about the
            # target's speed, so it leaves the interval alone.
            success = all(result.success for result in results)
            average = self.durations.record(finished_at - started_at, success)
            self.interval = interval_for(average, self.min_interval, self.max_interval)
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
        # The standalone targets, which are the rows of the display.
        self.endpoints: list[Endpoint] = list(config.endpoints)
        # Every distinct endpoint that gets probed; its position is its index in the store. A
        # chain hop that measures the same thing as a target, or as a hop of another chain, shares
        # its probes and its history, whatever name each place gives it.
        self.measured: list[Endpoint] = []
        self.target_indices = [self._measured_index(endpoint) for endpoint in self.endpoints]
        self.chain_hop_indices = [
            [self._measured_index(hop) for hop in chain.hops] for chain in config.chains
        ]
        checks = config.checks

        self.icmp_checker = ICMPChecker(timeout=checks.timeout_seconds)
        self.udp_checker = UDPChecker(timeout=checks.timeout_seconds)
        self.dns_checker = DNSChecker(timeout=checks.timeout_seconds)
        self.tailscale_checker = TailscaleChecker(timeout=checks.timeout_seconds)
        # getaddrinfo blocks a thread and cannot be interrupted. On its own executor, stop() can
        # drop it without waiting; on the loop's default executor, asyncio.run() would wait for a
        # hanging system resolver before returning.
        self.resolver_executor = ThreadPoolExecutor(thread_name_prefix="hydraping-resolver")
        self.connection_checker = ConnectionChecker(
            timeout=checks.timeout_seconds,
            nameservers=config.dns.servers or None,
            http_success_status_max=checks.http_success_status_max,
            resolver_executor=self.resolver_executor,
        )

        self.store = BucketStore(len(self.measured), checks.bucket_seconds)
        self.egress = EgressInterfaces()
        # The outgoing interface per store index, written by the egress loop under the store
        # lock. An endpoint without a known address or route has no entry.
        self._interfaces: dict[int, str] = {}
        self.schedules: list[ProbeSchedule] = []
        for index, endpoint in enumerate(self.measured):
            self.schedules.extend(self._schedules_for(index, endpoint))

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._started = threading.Event()

    # -- schedules ---------------------------------------------------------------------------

    def _measured_index(self, endpoint: Endpoint) -> int:
        """Return the store index of ``endpoint``, adding it when nothing measures it yet."""

        # The name, the primary layer and the tray flag only change how a place shows the endpoint,
        # and each place takes them from its own endpoint, so they don't make it a different
        # measurement.
        def key(endpoint: Endpoint) -> Endpoint:
            return dataclasses.replace(
                endpoint, custom_name=None, primary_check_type=None, tray=True
            )

        for index, measured in enumerate(self.measured):
            if key(measured) == key(endpoint):
                return index
        self.measured.append(endpoint)
        return len(self.measured) - 1

    def _schedules_for(self, index: int, endpoint: Endpoint) -> list[ProbeSchedule]:
        def schedule(layers: Sequence[CheckType], probe: Probe, label: str = "") -> ProbeSchedule:
            name = f"{endpoint.display_name} {'+'.join(layer.value for layer in layers)}{label}"
            checks = self.config.checks
            return ProbeSchedule(
                name=name,
                probe=self._guarded(layers, probe),
                interval=checks.probe_interval_seconds,
                timeout=checks.timeout_seconds,
                on_results=lambda results: self.store.apply(index, results),
                max_interval=(
                    checks.probe_interval_max_seconds if checks.adaptive_interval else None
                ),
            )

        def chain(target: ConnectionTarget, label: str = "") -> ProbeSchedule:
            return schedule(target.layers(), self._connection_probe(target), label)

        icmp = [CheckType.ICMP]
        schedules: list[ProbeSchedule] = []
        if isinstance(endpoint, IPEndpoint):
            schedules.append(schedule(icmp, self._icmp_probe(endpoint.ip)))
        elif isinstance(endpoint, IPPortEndpoint):
            schedules.append(schedule(icmp, self._icmp_probe(endpoint.ip)))
            schedules.append(chain(ConnectionTarget(endpoint.ip, endpoint.port)))
        elif isinstance(endpoint, UDPPortEndpoint):
            schedules.append(schedule(icmp, self._icmp_probe(endpoint.ip)))
            schedules.append(
                schedule(
                    [CheckType.UDP],
                    self._udp_probe(endpoint.ip, endpoint.port, endpoint.probe_data),
                )
            )
        elif isinstance(endpoint, DNSServerEndpoint):
            schedules.append(schedule(icmp, self._icmp_probe(endpoint.ip)))
            schedules.append(schedule([CheckType.DNS], self._dns_server_probe(endpoint)))
        elif isinstance(endpoint, TailscalePeerEndpoint):
            # No ICMP: a peer name is resolved by tailscale itself, not by the system resolver,
            # and the Tailscale ping already measures the round trip to the peer.
            schedules.append(schedule([CheckType.TAILSCALE], self._tailscale_probe(endpoint.peer)))
        elif isinstance(endpoint, DomainEndpoint):
            schedules.append(schedule(icmp, self._icmp_probe(endpoint.domain, endpoint.ip_version)))
            # Without a port, the chains for 80 and 443 share the DNS layer (last result wins) and
            # each writes its own TCP layer, keyed by port.
            for port in endpoint.tcp_ports():
                target = ConnectionTarget(endpoint.domain, port, ip_version=endpoint.ip_version)
                schedules.append(chain(target, f":{port}"))
        elif isinstance(endpoint, HTTPEndpoint):
            schedules.append(schedule(icmp, self._icmp_probe(endpoint.host, endpoint.ip_version)))
            target = ConnectionTarget(
                endpoint.host,
                endpoint.port,
                ip_version=endpoint.ip_version,
                tls=endpoint.uses_tls,
                insecure=endpoint.insecure,
                http=HTTPRequest(path=endpoint.path, scheme=endpoint.scheme),
            )
            schedules.append(chain(target))
        else:
            raise TypeError(f"Unsupported endpoint type: {type(endpoint).__name__}")
        return schedules

    @staticmethod
    def _guarded(layers: Sequence[CheckType], probe: Probe) -> Probe:
        """Turn an unexpected exception in a checker into a failed measurement.

        A checker bug must show up as a red cell with its message, not as a silent gap. The first
        layer carries the failure and the other layers of the probe are skipped, as after any
        failure.
        """

        async def guarded() -> Sequence[Measurement]:
            try:
                return await probe()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failed = Measurement(
                    check_type=layers[0],
                    success=False,
                    error_message=f"Unexpected error: {e}",
                )
                return [failed] + [Measurement.skipped_after(layer, failed) for layer in layers[1:]]

        return guarded

    def _icmp_probe(self, host: str, ip_version: int | None = None) -> Probe:
        async def probe() -> Sequence[Measurement]:
            target = self._resolved_address(host, ip_version)
            if target is None:
                return []
            return [await self.icmp_checker.check(target)]

        return probe

    def _resolved_address(self, host: str, ip_version: int | None) -> str | None:
        """Return the address of ``host``, or None while its chain has not resolved it.

        ICMP and the egress lookup never resolve a name themselves. The lookup would run on the
        loop's default executor, which ``asyncio.run()`` waits for on shutdown, so a hanging
        resolver would block ``stop()``. Until the chain has resolved the name, the ICMP schedule
        records nothing for its ticks.

        The chain of an ``ip_version = 6`` endpoint resolves to an IPv6 address, and its ICMP
        schedule must ping that one, not what the IPv4 twin of the same host resolved.
        """
        if _is_ip_address(host):
            return host
        return self.connection_checker.get_last_resolved_ip(host, ip_version)

    def _endpoint_address(self, endpoint: Endpoint) -> str | None:
        if isinstance(endpoint, (IPEndpoint, IPPortEndpoint, UDPPortEndpoint, DNSServerEndpoint)):
            return endpoint.ip
        if isinstance(endpoint, DomainEndpoint):
            return self._resolved_address(endpoint.domain, endpoint.ip_version)
        if isinstance(endpoint, HTTPEndpoint):
            return self._resolved_address(endpoint.host, endpoint.ip_version)
        if isinstance(endpoint, TailscalePeerEndpoint):
            if _is_ip_address(endpoint.peer):
                return endpoint.peer
            return self.tailscale_checker.get_last_node_ip(endpoint.peer)
        return None

    async def _egress_loop(self) -> None:
        """Keep the outgoing interface of every endpoint's current address up to date.

        Runs once per probe interval, so a newly resolved address shows its interface quickly.
        ``EgressInterfaces`` caches each address, which bounds how often ``ip`` runs.
        """
        while True:
            try:
                await self._refresh_interfaces()
            except Exception:
                logger.exception("Looking up outgoing interfaces failed")
            await asyncio.sleep(self.config.checks.probe_interval_seconds)

    async def _refresh_interfaces(self) -> None:
        addresses = [self._endpoint_address(endpoint) for endpoint in self.measured]
        lookups = [
            self.egress.interface_for(address) if address is not None else _none()
            for address in addresses
        ]
        interfaces = await asyncio.gather(*lookups)
        with self.store.lock:
            self._interfaces = {
                index: interface
                for index, interface in enumerate(interfaces)
                if interface is not None
            }

    def _connection_probe(self, target: ConnectionTarget) -> Probe:
        async def probe() -> Sequence[Measurement]:
            return await self.connection_checker.check(target)

        return probe

    def _dns_server_probe(self, endpoint: DNSServerEndpoint) -> Probe:
        async def probe() -> Sequence[Measurement]:
            measurement = await self.dns_checker.check_server(
                endpoint.ip, endpoint.zone, port=endpoint.port, transport=endpoint.transport
            )
            return [measurement]

        return probe

    def _tailscale_probe(self, peer: str) -> Probe:
        async def probe() -> Sequence[Measurement]:
            return [await self.tailscale_checker.check(peer)]

        return probe

    def _udp_probe(self, host: str, port: int, probe_data: bytes) -> Probe:
        async def probe() -> Sequence[Measurement]:
            return [await self.udp_checker.check(host, port, probe_data)]

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
        tasks.append(asyncio.ensure_future(self._egress_loop()))
        self._started.set()
        try:
            await self._stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # A lookup still running in a resolver thread is left to finish on its own.
            self.resolver_executor.shutdown(wait=False, cancel_futures=True)

    # -- reading -----------------------------------------------------------------------------

    def interfaces(self) -> dict[int, str]:
        """Return the known outgoing interface per store index. Thread-safe."""
        with self.store.lock:
            return dict(self._interfaces)

    def relays(self) -> dict[int, str]:
        """Return the relay of the latest reply per store index, for relayed peers. Thread-safe."""
        with self.store.lock:
            relays = {index: self._relay(index) for index in range(len(self.measured))}
        return {index: relay for index, relay in relays.items() if relay is not None}

    def _relay(self, index: int) -> str | None:
        """Return the relay that carried the latest Tailscale reply of ``index``, if it succeeded.

        Called with the store lock held.
        """
        layer = self.store.endpoints[index].layers.get(LayerKey(CheckType.TAILSCALE))
        latest = layer.latest if layer is not None else None
        return latest.relay if latest is not None and latest.success else None

    def snapshot(self, columns: int, now: float | None = None) -> EngineSnapshot:
        """Return the state for one frame, with ``columns`` buckets per layer. Thread-safe."""
        if now is None:
            now = time.time()
        with self.store.lock:
            icmp_available = self.icmp_checker.is_available()
            rows = tuple(
                self._endpoint_snapshot(index, endpoint, columns, now, icmp_available)
                for index, endpoint in zip(self.target_indices, self.endpoints, strict=True)
            )
            chains = tuple(
                self._chain_snapshot(chain, indices, columns, now, icmp_available)
                for chain, indices in zip(self.config.chains, self.chain_hop_indices, strict=True)
            )
        return EngineSnapshot(
            taken_at=now,
            bucket_seconds=self.store.bucket_seconds,
            columns=columns,
            rows=rows,
            icmp_available=icmp_available,
            chains=chains,
        )

    def _chain_snapshot(
        self, chain: Chain, indices: list[int], columns: int, now: float, icmp_available: bool
    ) -> ChainSnapshot:
        hops = [
            self._endpoint_snapshot(index, hop, columns, now, icmp_available)
            for index, hop in zip(indices, chain.hops, strict=True)
        ]
        states = [
            self._hop_state(index, hop, now) for index, hop in zip(indices, hops, strict=True)
        ]
        diagnosis = diagnose_chain(states)
        if diagnosis.broken_at is None:
            reported = hops
        else:
            broken = hops[diagnosis.broken_at]
            state = states[diagnosis.broken_at]
            assert state.result is not None
            # The diagnosis line already reports the layer that broke; the broken hop keeps its
            # other problem lines. The hops after it are expected to fail with it, so their own
            # lines are suppressed, while they keep being probed so a recovery shows up.
            failed_key = (state.cause or state.result).layer_key
            latest = {key: view.latest for key, view in broken.layers.items() if key != failed_key}
            hops[diagnosis.broken_at] = dataclasses.replace(
                broken, problems=tuple(problems_for(latest, icmp_available))
            )
            for after in range(diagnosis.broken_at + 1, len(hops)):
                hops[after] = dataclasses.replace(hops[after], problems=())
            reported = hops[: diagnosis.broken_at + 1]
        problems = [diagnosis.message] if diagnosis.message else []
        problems += [
            f"{hop.endpoint.display_name}: {problem}"
            for hop in reported
            for problem in hop.problems
        ]
        return ChainSnapshot(
            name=chain.name,
            hops=tuple(hops),
            broken_at=diagnosis.broken_at,
            problems=tuple(problems),
        )

    def _hop_state(self, index: int, hop: EndpointSnapshot, now: float) -> HopState:
        """Return the hop's primary layer result in the current bucket, for the chain diagnosis."""
        layers = self.store.endpoints[index].layers
        primary = layers.get(hop.primary)
        result = current_result(primary.view(now, 2)) if primary is not None else None
        cause = None
        if result is not None and result.skipped:
            # The layer that blocked the primary one, in the same probe: its error is the cause.
            # A layer shared by two probes, such as the DNS layer of a domain's ports 80 and 443,
            # may hold the other probe's result by now; then its latest failure is the cause.
            failures = []
            for key, layer in layers.items():
                candidate = current_result(layer.view(now, 2))
                if key.check_type != result.blocked_by or candidate is None or candidate.skipped:
                    continue
                if candidate.started_at == result.started_at:
                    cause = candidate
                    break
                if not candidate.success:
                    failures.append(candidate)
            if cause is None and failures:
                cause = max(failures, key=lambda failure: failure.started_at)
        return HopState(name=hop.endpoint.display_name, result=result, cause=cause)

    def _endpoint_snapshot(
        self, index: int, endpoint: Endpoint, columns: int, now: float, icmp_available: bool
    ) -> EndpointSnapshot:
        history = self.store.endpoints[index]
        layers = {
            key: LayerView(key=key, latest=layer.latest, buckets=layer.view(now, columns))
            for key, layer in history.layers.items()
        }
        primary = endpoint.get_primary_layer()
        latest = {key: view.latest for key, view in layers.items()}
        return EndpointSnapshot(
            endpoint=endpoint,
            primary=primary,
            layers=layers,
            current=latest.get(primary),
            problems=tuple(problems_for(latest, icmp_available)),
            interface=self._interfaces.get(index),
            relay=self._relay(index),
        )


async def _none() -> None:
    return None


def problems_for(latest: Mapping[LayerKey, CheckResult | None], icmp_available: bool) -> list[str]:
    """Return the problem lines for one endpoint from its latest result per layer.

    A failure below a layer that succeeded is noise (ICMP blocked by a firewall while HTTP works),
    so only failures at or above the highest successful layer are reported. The two TCP layers of
    a domain without a port sit on one level: a closed port 80 is reported next to a working port
    443. A skipped layer is not a failure of its own: the layer that blocked it carries the problem
    line. ICMP failures are dropped while ICMP is unavailable on this machine; the frontend shows
    one line for that.
    """
    level_of = {check_type: level for level, check_type in enumerate(CHECK_TYPE_PRIORITY)}
    highest_success_level = min(
        (
            level_of[key.check_type]
            for key, result in latest.items()
            if result is not None and result.success
        ),
        default=-1,
    )

    problems = []
    for key in sorted(latest, key=lambda key: (level_of[key.check_type], key.port or 0)):
        result = latest[key]
        if result is None or result.success or result.skipped:
            continue
        if key.check_type == CheckType.ICMP and not icmp_available:
            continue
        if 0 <= highest_success_level < level_of[key.check_type]:
            continue
        problems.append(f"{key.check_type.value.upper()}: {result.error_message}")
    return problems
