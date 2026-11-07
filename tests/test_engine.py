"""Tests for the probe scheduler (real asyncio, fake probes) and the engine (localhost TCP)."""

import asyncio
import socket
import threading
import time

import pytest

from hydraping.core.config import ChecksConfig, Config
from hydraping.core.engine import Engine, ProbeSchedule, problems_for
from hydraping.core.models import CheckResult, CheckType, Endpoint, Measurement

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


FAST_CHECKS = ChecksConfig(probe_interval_seconds=0.05, bucket_seconds=0.5, timeout_seconds=1.0)


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

            assert row.primary == CheckType.TCP
            assert row.current.success, row.current.error_message
            assert row.current.port == port
            # A result can land in the bucket that just closed while the open one is still empty.
            recent = row.layers[CheckType.TCP].buckets[-2:]
            assert row.current in [bucket.last for bucket in recent if bucket is not None]
            assert row.problems == ()
            assert len(row.layers[CheckType.TCP].buckets) == 10
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
                tcp = row.layers.get(CheckType.TCP)
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

    def test_schedules_per_endpoint_type(self):
        config = Config(
            endpoints=[
                Endpoint.parse("192.0.2.1"),
                Endpoint.parse("192.0.2.1:22"),
                Endpoint.parse("example.com"),
                Endpoint.parse("example.com:8080"),
                Endpoint.parse("https://example.com/health"),
            ]
        )
        names = [schedule.name for schedule in Engine(config).schedules]
        assert names == [
            "192.0.2.1 icmp",
            "192.0.2.1:22 icmp",
            "192.0.2.1:22 tcp",
            "example.com dns",
            "example.com icmp",
            "example.com tcp:80",
            "example.com tcp:443",
            "example.com:8080 dns",
            "example.com:8080 icmp",
            "example.com:8080 tcp:8080",
            "https://example.com/health dns",
            "https://example.com/health icmp",
            "https://example.com/health tcp",
            "https://example.com/health http",
        ]


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


class TestProblems:
    def test_lower_failures_are_hidden_behind_a_higher_success(self):
        latest = {
            CheckType.HTTP: passed(CheckType.HTTP),
            CheckType.TCP: passed(CheckType.TCP),
            CheckType.ICMP: failed(CheckType.ICMP, "Host unreachable"),
        }
        assert problems_for(latest, icmp_available=True) == []

    def test_failures_above_the_highest_success_are_reported(self):
        latest = {
            CheckType.HTTP: failed(CheckType.HTTP, "HTTP 503"),
            CheckType.TCP: passed(CheckType.TCP),
            CheckType.ICMP: failed(CheckType.ICMP, "Host unreachable"),
        }
        assert problems_for(latest, icmp_available=True) == ["HTTP: HTTP 503"]

    def test_everything_failing_is_all_reported_in_priority_order(self):
        latest = {
            CheckType.ICMP: failed(CheckType.ICMP, "Host unreachable"),
            CheckType.DNS: failed(CheckType.DNS, "NXDOMAIN"),
        }
        assert problems_for(latest, icmp_available=True) == [
            "DNS: NXDOMAIN",
            "ICMP: Host unreachable",
        ]

    def test_icmp_failures_are_dropped_while_icmp_is_unavailable(self):
        latest = {CheckType.ICMP: failed(CheckType.ICMP, "ICMP unavailable")}
        assert problems_for(latest, icmp_available=False) == []
        assert problems_for(latest, icmp_available=True) == ["ICMP: ICMP unavailable"]
