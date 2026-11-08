"""Tests for the probe scheduler (real asyncio, fake probes) and the engine (localhost TCP)."""

import asyncio
import socket
import threading
import time

import pytest

from hydraping.core.checkers.connection import ConnectionTarget
from hydraping.core.config import ChecksConfig, Config
from hydraping.core.engine import Engine, ProbeSchedule, problems_for
from hydraping.core.models import CheckResult, CheckType, Endpoint, LayerKey, Measurement
from hydraping.core.store import BucketStore

INTERVAL = 0.05


def ok(latency_ms: float, check_type: CheckType = CheckType.TCP) -> Measurement:
    return Measurement(check_type=check_type, success=True, latency_ms=latency_ms)


class FakeProbe:
    """A probe whose delay is chosen per call: ``delays[n]`` for the n-th call, the last one after.

    A delay of ``None`` hangs until cancelled. The latency of each measurement is the call number,
    so a test can tell which probe a result came from.
    """

    def __init__(self, *delays: float | None, measurements_per_call: int = 1):
        self.delays = delays
        self.measurements_per_call = measurements_per_call
        self.calls = 0
        self.cancelled: list[int] = []

    async def __call__(self) -> list[Measurement]:
        self.calls += 1
        call = self.calls
        delay = self.delays[min(call, len(self.delays)) - 1]
        try:
            if delay is None:
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.append(call)
            raise
        return [ok(float(call)) for _ in range(self.measurements_per_call)]


class PhasedProbe:
    """A probe whose delay depends on the time since ``start()``; ``phases`` are ``(until, delay)``.

    A delay of ``None`` returns a failed measurement at once. ``ticks`` records when each call
    started, relative to ``start()``.
    """

    def __init__(self, *phases: tuple[float, float | None]):
        self.phases = phases
        self.started_at = 0.0
        self.ticks: list[float] = []
        self.cancelled = 0

    def start(self) -> None:
        self.started_at = time.monotonic()

    async def __call__(self) -> list[Measurement]:
        elapsed = time.monotonic() - self.started_at
        self.ticks.append(elapsed)
        delay = next((delay for until, delay in self.phases if elapsed < until), self.phases[-1][1])
        if delay is None:
            return [Measurement(check_type=CheckType.TCP, success=False, error_message="down")]
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return [ok(delay * 1000)]


def gaps_within(ticks: list[float], start: float, end: float) -> list[float]:
    window = [tick for tick in ticks if start <= tick < end]
    return [b - a for a, b in zip(window, window[1:], strict=False)]


def median(values: list[float]) -> float:
    assert values
    return sorted(values)[len(values) // 2]


async def run_for(seconds: float, *schedules: ProbeSchedule) -> None:
    tasks = [asyncio.ensure_future(schedule.run()) for schedule in schedules]
    await asyncio.sleep(seconds)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class TestProbeSchedule:
    async def test_newer_finished_probe_cancels_the_slow_older_one(self):
        probe = FakeProbe(10.0, 0.001)
        results: list[CheckResult] = []
        schedule = ProbeSchedule("t", probe, INTERVAL, timeout=1.0, on_results=results.extend)

        await run_for(INTERVAL * 4, schedule)

        assert probe.cancelled[0] == 1, "the first, slow probe was cancelled"
        assert results, "later probes recorded results"
        assert all(r.latency_ms != 1.0 for r in results), "a cancelled probe records nothing"
        assert all(result.started_at <= result.finished_at for result in results)

    async def test_in_flight_cap_holds(self):
        probe = FakeProbe(None)
        results: list[CheckResult] = []
        schedule = ProbeSchedule("t", probe, INTERVAL, timeout=0.2, on_results=results.extend)
        assert schedule.max_in_flight == 5

        await run_for(INTERVAL * 12, schedule)

        assert probe.calls == 5, "ticks past the cap started nothing"
        assert schedule.skipped_ticks >= 5
        assert sorted(probe.cancelled) == [1, 2, 3, 4, 5], "stopping cancels every hung probe"
        assert results == []
        assert schedule.in_flight == 0

    async def test_hanging_schedule_does_not_delay_another(self):
        hanging = FakeProbe(None)
        quick = FakeProbe(0.001)
        quick_results: list[CheckResult] = []
        schedules = [
            ProbeSchedule("hanging", hanging, INTERVAL, timeout=1.0, on_results=lambda r: None),
            ProbeSchedule("quick", quick, INTERVAL, timeout=1.0, on_results=quick_results.extend),
        ]

        await run_for(INTERVAL * 8, *schedules)

        assert len(quick_results) >= 5
        gaps = [
            b.finished_at - a.finished_at
            for a, b in zip(quick_results, quick_results[1:], strict=False)
        ]
        assert max(gaps) < INTERVAL * 3

    async def test_measurements_of_one_probe_share_the_probe_stamps(self):
        probe = FakeProbe(0.01, measurements_per_call=3)
        batches: list[list[CheckResult]] = []
        schedule = ProbeSchedule(
            "t", probe, INTERVAL, timeout=1.0, on_results=lambda r: batches.append(list(r))
        )

        await run_for(INTERVAL * 2.5, schedule)

        assert batches
        for batch in batches:
            assert len(batch) == 3
            assert len({(r.started_at, r.finished_at) for r in batch}) == 1
            assert batch[0].finished_at - batch[0].started_at >= 0.01

    async def test_raising_probe_records_nothing_and_the_schedule_goes_on(self):
        calls = 0

        async def probe() -> list[Measurement]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("checker bug")
            return [ok(float(calls))]

        results: list[CheckResult] = []
        schedule = ProbeSchedule("t", probe, INTERVAL, timeout=1.0, on_results=results.extend)

        await run_for(INTERVAL * 4, schedule)

        assert calls >= 3
        assert results and all(result.latency_ms != 1.0 for result in results)

    def test_rejects_non_positive_timing(self):
        with pytest.raises(ValueError):
            ProbeSchedule("t", FakeProbe(0.0), 0.0, timeout=1.0, on_results=lambda r: None)
        with pytest.raises(ValueError):
            ProbeSchedule("t", FakeProbe(0.0), 1.0, timeout=0.0, on_results=lambda r: None)

    def test_rejects_a_maximum_interval_below_the_interval(self):
        with pytest.raises(ValueError, match="max_interval"):
            ProbeSchedule(
                "t", FakeProbe(0.0), 1.0, timeout=1.0, on_results=lambda r: None, max_interval=0.5
            )

    async def test_interval_follows_the_probe_duration_fast_slow_fast(self):
        """Ticks come every MIN while probes are fast, stretch to MAX while they take long, and come
        back to MIN once the probes are fast again. The slow probes still in flight when the first
        fast one lands are cancelled and don't hold the average up."""
        minimum, maximum = 0.05, 0.25
        # Fast until 0.6 s, then each probe takes half a second (the slow mark), fast again from
        # 2.5 s. The slow phase is long enough for the moving average to settle near the maximum.
        probe = PhasedProbe((0.6, 0.001), (2.5, 0.5), (99.0, 0.001))
        results: list[CheckResult] = []
        schedule = ProbeSchedule(
            "t", probe, minimum, timeout=1.0, on_results=results.extend, max_interval=maximum
        )
        assert schedule.interval == minimum
        probe.start()
        task = asyncio.ensure_future(schedule.run())
        try:
            await asyncio.sleep(0.6)
            assert schedule.interval == minimum
            await asyncio.sleep(1.9)
            assert schedule.interval > 0.2, "slow probes stretched the interval towards 0.25 s"
            await asyncio.sleep(1.0)
            assert schedule.interval == minimum, "fast probes brought the interval back"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert median(gaps_within(probe.ticks, 0.0, 0.6)) < 0.1
        # The first slow result lands at 1.1 s and the third, which takes the interval past 0.2 s,
        # at 1.2 s; the ticks from 1.5 s on are spaced by that interval, and asyncio never fires
        # early. The window is wide enough to hold several ticks even on a loaded machine.
        slow_gaps = gaps_within(probe.ticks, 1.5, 2.5)
        assert len(slow_gaps) >= 2, probe.ticks
        assert min(slow_gaps) >= 0.2
        assert median(gaps_within(probe.ticks, 3.0, 3.5)) < 0.1
        assert probe.cancelled > 0, "the first fast probe cancelled the slow ones in flight"
        assert all(r.latency_ms != 500.0 for r in results[-5:]), "the last results are fast"

    async def test_failures_stretch_the_interval_and_the_in_flight_cap_follows(self):
        probe = PhasedProbe((99.0, None))
        schedule = ProbeSchedule(
            "t", probe, INTERVAL, timeout=1.0, on_results=lambda r: None, max_interval=0.25
        )
        assert schedule.max_in_flight == 21
        probe.start()

        await run_for(1.5, schedule)

        assert schedule.interval > 0.24
        # ceil(1.0 / 0.25) + 1 is 5; the average approaches the slow mark from below, so the
        # interval sits just under 0.25 s and the cap is 6 until it gets there.
        assert schedule.max_in_flight in (5, 6)
        assert min(gaps_within(probe.ticks, 0.8, 1.5)) >= 0.2

    async def test_without_a_maximum_the_interval_is_fixed(self):
        probe = PhasedProbe((99.0, None))
        schedule = ProbeSchedule("t", probe, INTERVAL, timeout=1.0, on_results=lambda r: None)
        probe.start()

        await run_for(INTERVAL * 10, schedule)

        assert schedule.interval == INTERVAL
        assert schedule.max_in_flight == 21
        assert len(probe.ticks) >= 8, "failures did not slow the ticks down"
        assert median(gaps_within(probe.ticks, 0.0, 99.0)) < INTERVAL * 2


# A fixed interval: these tests count probes against the clock. The adaptive interval has its own
# tests in TestProbeSchedule.
FAST_CHECKS = ChecksConfig(
    probe_interval_seconds=0.05, adaptive_interval=False, bucket_seconds=0.5, timeout_seconds=1.0
)


def listening_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock


class TestEngine:
    def test_probes_localhost_and_stops_within_a_second(self):
        server = listening_socket()
        port = server.getsockname()[1]
        config = Config(
            endpoints=[Endpoint.parse(f"127.0.0.1:{port}")],
            checks=FAST_CHECKS,
        )
        engine = Engine(config)
        assert threading.current_thread() is threading.main_thread()
        try:
            engine.start()
            deadline = time.monotonic() + 3.0
            while True:
                snapshot = engine.snapshot(columns=10)
                [row] = snapshot.rows
                if row.current is not None and time.monotonic() < deadline:
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("no TCP result within 3 s")
                time.sleep(0.02)

            assert row.primary == LayerKey(CheckType.TCP, port)
            assert row.current.success, row.current.error_message
            assert row.current.port == port
            # A result can land in the bucket that just closed while the open one is still empty.
            recent = row.layers[LayerKey(CheckType.TCP, port)].buckets[-2:]
            assert row.current in [bucket.last for bucket in recent if bucket is not None]
            assert row.problems == ()
            assert len(row.layers[LayerKey(CheckType.TCP, port)].buckets) == 10
        finally:
            started = time.monotonic()
            engine.stop()
            stop_took = time.monotonic() - started
            server.close()

        assert stop_took < 1.0
        assert not any(t.name == "hydraping-engine" for t in threading.enumerate())

        # Nothing probes after stop: the latest result stays the same.
        latest = engine.snapshot(columns=1).rows[0].current
        time.sleep(0.15)
        assert engine.snapshot(columns=1).rows[0].current is latest

    def test_unreachable_port_shows_a_tcp_problem(self):
        server = listening_socket()
        port = server.getsockname()[1]
        server.close()  # Nothing listens on this port now.
        config = Config(
            endpoints=[Endpoint.parse(f"127.0.0.1:{port}")],
            checks=FAST_CHECKS,
        )
        engine = Engine(config)
        try:
            engine.start()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                [row] = engine.snapshot(columns=5).rows
                tcp = row.layers.get(LayerKey(CheckType.TCP, port))
                if tcp is not None and tcp.latest is not None:
                    break
                time.sleep(0.02)
            else:
                pytest.fail("no TCP result within 3 s")
        finally:
            engine.stop()

        assert tcp.latest.success is False
        assert any(problem.startswith("TCP: Connection refused") for problem in row.problems)

    def test_stop_without_start_is_a_no_op(self):
        Engine(Config(endpoints=[Endpoint.parse("192.0.2.1")])).stop()

    def test_stop_after_the_loop_failed_to_start(self):
        class EngineWithoutLoop(Engine):
            def _thread_main(self) -> None:
                # What asyncio.run() failing before _main() looks like: no loop, no stop event.
                self._started.set()

        engine = EngineWithoutLoop(Config(endpoints=[Endpoint.parse("192.0.2.1")]))
        engine.start()
        engine.stop()
        assert not any(t.name == "hydraping-engine" for t in threading.enumerate())

    def test_stop_does_not_wait_for_a_lookup_stuck_in_the_resolver_thread(self):
        release = threading.Event()
        blocked = threading.Event()

        def hanging_getaddrinfo() -> None:
            blocked.set()
            release.wait()

        engine = Engine(Config(endpoints=[Endpoint.parse("192.0.2.1")], checks=FAST_CHECKS))

        async def stuck_probe() -> list[Measurement]:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(engine.resolver_executor, hanging_getaddrinfo)
            return []

        engine.schedules = [
            ProbeSchedule("stuck", stuck_probe, INTERVAL, timeout=1.0, on_results=lambda r: None)
        ]
        try:
            engine.start()
            assert blocked.wait(3.0), "the probe never reached the resolver thread"
            started = time.monotonic()
            engine.stop()
            stop_took = time.monotonic() - started
        finally:
            release.set()

        assert stop_took < 1.0
        assert not any(t.name == "hydraping-engine" for t in threading.enumerate())

    async def test_icmp_pings_the_address_the_chain_of_the_same_ip_version_resolved(self):
        server = listening_socket()
        port = server.getsockname()[1]
        engine = Engine(Config(endpoints=[Endpoint.parse(f"localhost:{port}")]))
        try:
            await engine.connection_checker.check(ConnectionTarget("localhost", port, ip_version=4))
        finally:
            server.close()
            engine.resolver_executor.shutdown(wait=False)

        assert engine._resolved_address("localhost", 4) == "127.0.0.1"
        # The IPv6 variant has not resolved anything yet; it must not borrow the IPv4 address.
        assert engine._resolved_address("localhost", 6) is None
        assert engine._resolved_address("localhost", None) is None
        assert engine._resolved_address("192.0.2.1", None) == "192.0.2.1"

    async def test_icmp_records_nothing_for_a_name_its_chain_has_not_resolved(self):
        engine = Engine(Config(endpoints=[Endpoint.parse("example.com")]))
        try:
            assert await engine._icmp_probe("example.com")() == []
        finally:
            engine.resolver_executor.shutdown(wait=False)

    async def test_one_closed_port_of_a_domain_keeps_its_own_steady_history(self):
        """One port open and one closed must give a steady open-port history and a steady
        problem line for the closed port, not a TCP layer that alternates with every probe.

        Ports 80 and 443 cannot be bound in a test, so this wires two chains into one endpoint
        history the way ``Engine`` does for a domain without a port, with two ports of its own.
        """
        server = listening_socket()
        open_port = server.getsockname()[1]
        closed = listening_socket()
        closed_port = closed.getsockname()[1]
        closed.close()
        engine = Engine(Config(endpoints=[Endpoint.parse("localhost")], checks=FAST_CHECKS))
        store = BucketStore(endpoint_count=1, bucket_seconds=FAST_CHECKS.bucket_seconds)
        schedules = [
            ProbeSchedule(
                f"localhost dns+tcp:{port}",
                engine._connection_probe(ConnectionTarget("localhost", port, ip_version=4)),
                INTERVAL,
                timeout=1.0,
                on_results=lambda results: store.apply(0, results),
            )
            for port in (open_port, closed_port)
        ]
        try:
            await run_for(INTERVAL * 8, *schedules)
        finally:
            server.close()
            engine.resolver_executor.shutdown(wait=False)

        layers = store.endpoints[0].layers
        assert set(layers) == {
            LayerKey(CheckType.DNS),
            LayerKey(CheckType.TCP, open_port),
            LayerKey(CheckType.TCP, closed_port),
        }

        def counts(key: LayerKey) -> tuple[int, int]:
            buckets = [b for b in layers[key].view(time.time(), 3) if b is not None]
            return sum(b.ok_count for b in buckets), sum(b.failed_count for b in buckets)

        open_ok, open_failed = counts(LayerKey(CheckType.TCP, open_port))
        closed_ok, closed_failed = counts(LayerKey(CheckType.TCP, closed_port))
        assert open_ok >= 3 and open_failed == 0
        assert closed_failed >= 3 and closed_ok == 0
        latest = {key: layer.latest for key, layer in layers.items()}
        assert problems_for(latest, icmp_available=True) == [
            f"TCP: Connection refused on port {closed_port}"
        ]

    def test_schedules_per_endpoint_type(self):
        config = Config(
            endpoints=[
                Endpoint.parse("192.0.2.1"),
                Endpoint.parse("192.0.2.1:22"),
                Endpoint.parse("example.com"),
                Endpoint.parse("example.com:8080"),
                Endpoint.parse("https://example.com/health"),
                Endpoint.parse("http://192.0.2.1/"),
            ]
        )
        names = [schedule.name for schedule in Engine(config).schedules]
        assert names == [
            "192.0.2.1 icmp",
            "192.0.2.1:22 icmp",
            "192.0.2.1:22 tcp",
            "example.com icmp",
            "example.com dns+tcp:80",
            "example.com dns+tcp:443",
            "example.com:8080 icmp",
            "example.com:8080 dns+tcp:8080",
            "https://example.com/health icmp",
            "https://example.com/health dns+tcp+tls+http",
            "http://192.0.2.1/ icmp",
            "http://192.0.2.1/ tcp+http",
        ]

    async def test_unexpected_checker_error_fails_the_first_layer_and_skips_the_rest(self):
        async def broken() -> list[Measurement]:
            raise RuntimeError("checker bug")

        layers = [CheckType.DNS, CheckType.TCP, CheckType.HTTP]
        [dns, tcp, http] = await Engine._guarded(layers, broken)()
        assert dns.error_message == "Unexpected error: checker bug"
        assert tcp.skipped and tcp.blocked_by == CheckType.DNS
        assert http.skipped and http.blocked_by == CheckType.DNS


def failed(check_type: CheckType, message: str) -> CheckResult:
    return CheckResult(
        check_type=check_type,
        success=False,
        error_message=message,
        started_at=1.0,
        finished_at=1.1,
    )


def passed(check_type: CheckType) -> CheckResult:
    return CheckResult(
        check_type=check_type, success=True, latency_ms=1.0, started_at=1.0, finished_at=1.1
    )


def skipped(check_type: CheckType, blocked_by: CheckResult) -> CheckResult:
    return CheckResult.from_measurement(
        Measurement.skipped_after(check_type, blocked_by), started_at=1.0, finished_at=1.1
    )


class TestProblems:
    def test_lower_failures_are_hidden_behind_a_higher_success(self):
        latest = {
            LayerKey(CheckType.HTTP): passed(CheckType.HTTP),
            LayerKey(CheckType.TCP, 443): passed(CheckType.TCP),
            LayerKey(CheckType.ICMP): failed(CheckType.ICMP, "Host unreachable"),
        }
        assert problems_for(latest, icmp_available=True) == []

    def test_failures_above_the_highest_success_are_reported(self):
        latest = {
            LayerKey(CheckType.HTTP): failed(CheckType.HTTP, "HTTP 503"),
            LayerKey(CheckType.TCP, 443): passed(CheckType.TCP),
            LayerKey(CheckType.ICMP): failed(CheckType.ICMP, "Host unreachable"),
        }
        assert problems_for(latest, icmp_available=True) == ["HTTP: HTTP 503"]

    def test_everything_failing_is_all_reported_in_priority_order(self):
        latest = {
            LayerKey(CheckType.ICMP): failed(CheckType.ICMP, "Host unreachable"),
            LayerKey(CheckType.DNS): failed(CheckType.DNS, "NXDOMAIN"),
        }
        assert problems_for(latest, icmp_available=True) == [
            "DNS: NXDOMAIN",
            "ICMP: Host unreachable",
        ]

    def test_skipped_layers_report_only_the_layer_that_failed(self):
        tcp = failed(CheckType.TCP, "Connection refused on port 443")
        latest = {
            LayerKey(CheckType.DNS): passed(CheckType.DNS),
            LayerKey(CheckType.TCP, 443): tcp,
            LayerKey(CheckType.TLS): skipped(CheckType.TLS, tcp),
            LayerKey(CheckType.HTTP): skipped(CheckType.HTTP, tcp),
            LayerKey(CheckType.ICMP): passed(CheckType.ICMP),
        }
        assert problems_for(latest, icmp_available=True) == ["TCP: Connection refused on port 443"]

    def test_a_closed_port_is_reported_next_to_an_open_one(self):
        latest = {
            LayerKey(CheckType.DNS): passed(CheckType.DNS),
            LayerKey(CheckType.TCP, 443): passed(CheckType.TCP),
            LayerKey(CheckType.TCP, 80): failed(CheckType.TCP, "Connection refused on port 80"),
            LayerKey(CheckType.ICMP): failed(CheckType.ICMP, "Host unreachable"),
        }
        assert problems_for(latest, icmp_available=True) == ["TCP: Connection refused on port 80"]

    def test_icmp_failures_are_dropped_while_icmp_is_unavailable(self):
        latest = {LayerKey(CheckType.ICMP): failed(CheckType.ICMP, "ICMP unavailable")}
        assert problems_for(latest, icmp_available=False) == []
        assert problems_for(latest, icmp_available=True) == ["ICMP: ICMP unavailable"]
