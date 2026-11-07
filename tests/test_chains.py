"""Tests for chains of dependent hops: config, diagnosis, engine snapshots and the dashboard."""

import dataclasses
import time
from io import StringIO

import pytest
from rich.console import Console

from hydraping.core.chains import HopState, current_result, diagnose_chain
from hydraping.core.config import Chain, Config, create_default_config
from hydraping.core.engine import Engine
from hydraping.core.models import CheckResult, CheckType, Endpoint, Measurement
from hydraping.core.store import Bucket
from hydraping.tui.dashboard import Dashboard

BUCKET = 5.0
# A bucket boundary: the open bucket is [T, T + 5).
T = 1_000_000_000.0


def ok(check_type: CheckType, at: float, latency_ms: float = 10.0, port: int | None = None):
    return CheckResult(
        check_type=check_type,
        success=True,
        latency_ms=latency_ms,
        port=port,
        started_at=at - 0.01,
        finished_at=at,
    )


def failed(check_type: CheckType, at: float, message: str, port: int | None = None):
    return CheckResult(
        check_type=check_type,
        success=False,
        error_message=message,
        port=port,
        started_at=at - 0.01,
        finished_at=at,
    )


def skipped(check_type: CheckType, blocked_by: CheckResult) -> CheckResult:
    return CheckResult.from_measurement(
        Measurement.skipped_after(check_type, blocked_by),
        started_at=blocked_by.started_at,
        finished_at=blocked_by.finished_at,
    )


def bucket(result: CheckResult) -> Bucket:
    return Bucket(index=int(result.finished_at // BUCKET), last=result, ok_count=1, failed_count=0)


class TestCurrentResult:
    def test_open_bucket_wins(self):
        older = ok(CheckType.ICMP, T - 1)
        newer = failed(CheckType.ICMP, T + 1, "Timeout")
        assert current_result((bucket(older), bucket(newer))) is newer

    def test_bucket_that_just_closed_stands_in_while_the_open_one_is_empty(self):
        older = ok(CheckType.ICMP, T - 1)
        assert current_result((bucket(older), None)) is older

    def test_older_buckets_do_not_count(self):
        oldest = failed(CheckType.ICMP, T - 6, "Timeout")
        assert current_result((bucket(oldest), None, None)) is None
        assert current_result(()) is None


class TestDiagnoseChain:
    def test_first_hop_broken(self):
        diagnosis = diagnose_chain(
            [
                HopState("Router", failed(CheckType.ICMP, T, "Timeout")),
                HopState("Internet", failed(CheckType.ICMP, T, "Timeout")),
            ]
        )
        assert diagnosis.broken_at == 0
        assert diagnosis.message == "broken at Router (ICMP: Timeout)"

    def test_middle_hop_broken_names_the_hops_before_it(self):
        tcp = failed(CheckType.TCP, T, "Connection refused on port 22", port=22)
        diagnosis = diagnose_chain(
            [
                HopState("Router", ok(CheckType.ICMP, T)),
                HopState("Internet", ok(CheckType.ICMP, T)),
                HopState("Jumpbox", tcp),
                HopState("Intranet", failed(CheckType.HTTP, T, "Timeout")),
            ]
        )
        assert diagnosis.broken_at == 2
        assert diagnosis.message == (
            "broken at Jumpbox (TCP: Connection refused on port 22); Router, Internet OK"
        )

    def test_skipped_primary_layer_reports_the_layer_that_failed(self):
        tcp = failed(CheckType.TCP, T, "Connection refused on port 443", port=443)
        diagnosis = diagnose_chain(
            [
                HopState("Router", ok(CheckType.ICMP, T)),
                HopState("Intranet", skipped(CheckType.HTTP, tcp), cause=tcp),
            ]
        )
        assert diagnosis.message == (
            "broken at Intranet (TCP: Connection refused on port 443); Router OK"
        )

    def test_all_ok(self):
        diagnosis = diagnose_chain(
            [HopState("Router", ok(CheckType.ICMP, T)), HopState("Internet", ok(CheckType.ICMP, T))]
        )
        assert diagnosis.broken_at is None
        assert diagnosis.message is None

    def test_hop_without_a_result_before_a_broken_hop_gives_no_diagnosis(self):
        diagnosis = diagnose_chain(
            [
                HopState("Router", ok(CheckType.ICMP, T)),
                HopState("Internet", None),
                HopState("Jumpbox", failed(CheckType.ICMP, T, "Timeout")),
            ]
        )
        assert diagnosis.broken_at is None
        assert diagnosis.message is None

    def test_broken_hop_before_a_hop_without_a_result_is_reported(self):
        diagnosis = diagnose_chain(
            [
                HopState("Router", ok(CheckType.ICMP, T)),
                HopState("Internet", failed(CheckType.ICMP, T, "Timeout")),
                HopState("Jumpbox", None),
            ]
        )
        assert diagnosis.broken_at == 1
        assert diagnosis.message == "broken at Internet (ICMP: Timeout); Router OK"


def company_chain() -> Chain:
    return Chain(
        name="Company",
        hops=[
            Endpoint.parse("192.0.2.1"),
            Endpoint.parse("198.51.100.1"),
            Endpoint.parse("203.0.113.1"),
        ],
    )


def name(endpoint: Endpoint, display_name: str) -> Endpoint:
    endpoint.custom_name = display_name
    return endpoint


def engine_with(chain: Chain, targets: list[Endpoint] | None = None) -> Engine:
    engine = Engine(Config(endpoints=targets or [], chains=[chain]))
    assert engine.store.bucket_seconds == BUCKET
    return engine


class TestEngineChains:
    def test_middle_hop_broken_suppresses_the_problems_after_it(self):
        chain = company_chain()
        engine = engine_with(chain)
        router, internet, jumpbox = engine.chain_hop_indices[0]
        engine.store.apply(router, [ok(CheckType.ICMP, T + 1)])
        engine.store.apply(internet, [failed(CheckType.ICMP, T + 1, "Timeout")])
        engine.store.apply(jumpbox, [failed(CheckType.ICMP, T + 1, "Timeout")])

        [snapshot] = engine.snapshot(columns=4, now=T + 2).chains

        assert snapshot.broken_at == 1
        assert snapshot.problems == ("broken at 198.51.100.1 (ICMP: Timeout); 192.0.2.1 OK",)
        assert snapshot.hops[2].problems == ()
        assert snapshot.hops[2].current is not None  # Still probed and shown.

    def test_broken_hop_keeps_its_other_problem_lines(self):
        router = name(Endpoint.parse("router"), "Router")
        router.primary_check_type = CheckType.ICMP
        engine = engine_with(Chain(name="Company", hops=[router]))
        [index] = engine.chain_hop_indices[0]
        engine.store.apply(index, [failed(CheckType.ICMP, T + 1, "Timeout")])
        for port in (80, 443):
            tcp = failed(CheckType.TCP, T + 1, f"Connection refused on port {port}", port=port)
            engine.store.apply(index, [ok(CheckType.DNS, T + 1), tcp])

        [snapshot] = engine.snapshot(columns=4, now=T + 2).chains

        # The ICMP failure is in the diagnosis line only; the TCP failures stay as hop lines.
        assert snapshot.problems == (
            "broken at Router (ICMP: Timeout)",
            "Router: TCP: Connection refused on port 80",
            "Router: TCP: Connection refused on port 443",
        )

    def test_recovery_clears_the_diagnosis(self):
        engine = engine_with(company_chain())
        router, internet, jumpbox = engine.chain_hop_indices[0]
        engine.store.apply(router, [failed(CheckType.ICMP, T + 1, "Timeout")])
        assert engine.snapshot(columns=4, now=T + 2).chains[0].broken_at == 0

        for index in (router, internet, jumpbox):
            engine.store.apply(index, [ok(CheckType.ICMP, T + 3)])
        [snapshot] = engine.snapshot(columns=4, now=T + 4).chains

        assert snapshot.broken_at is None
        assert snapshot.problems == ()

    def test_failure_from_an_older_bucket_does_not_break_the_chain(self):
        engine = engine_with(company_chain())
        router = engine.chain_hop_indices[0][0]
        engine.store.apply(router, [failed(CheckType.ICMP, T - 1, "Timeout")])

        # One bucket old, while the open bucket has nothing yet: still current.
        assert engine.snapshot(columns=4, now=T + 1).chains[0].broken_at == 0
        # Two buckets old: not current any more.
        assert engine.snapshot(columns=4, now=T + BUCKET + 1).chains[0].broken_at is None

    def test_hop_listed_as_a_target_shares_its_history_and_keeps_its_problems_there(self):
        chain = company_chain()
        target = name(Endpoint.parse("203.0.113.1"), "Jumpbox")
        engine = engine_with(chain, targets=[target])
        [target_index] = engine.target_indices
        router, internet, jumpbox = engine.chain_hop_indices[0]

        assert jumpbox == target_index
        assert len(engine.measured) == 3
        engine.store.apply(router, [failed(CheckType.ICMP, T + 1, "Timeout")])
        engine.store.apply(jumpbox, [failed(CheckType.ICMP, T + 1, "Timeout")])

        snapshot = engine.snapshot(columns=4, now=T + 2)

        [row] = snapshot.rows
        assert row.endpoint.display_name == "Jumpbox"
        assert row.problems == ("ICMP: Timeout",)
        [chain_snapshot] = snapshot.chains
        assert chain_snapshot.hops[2].endpoint.display_name == "203.0.113.1"
        assert chain_snapshot.hops[2].problems == ()
        assert chain_snapshot.problems == ("broken at 192.0.2.1 (ICMP: Timeout)",)

    def test_same_hop_in_two_chains_is_probed_once(self):
        first = Chain(name="A", hops=[Endpoint.parse("192.0.2.1"), Endpoint.parse("198.51.100.1")])
        second = Chain(name="B", hops=[Endpoint.parse("192.0.2.1"), Endpoint.parse("203.0.113.1")])
        engine = Engine(Config(endpoints=[], chains=[first, second]))

        assert engine.chain_hop_indices == [[0, 1], [0, 2]]
        assert len(engine.schedules) == 3

    def test_hop_that_differs_from_a_target_only_in_its_primary_layer_is_probed_once(self):
        target = Endpoint.parse("192.0.2.1:22")
        target.primary_check_type = CheckType.ICMP
        engine = engine_with(Chain(name="A", hops=[Endpoint.parse("192.0.2.1:22")]), [target])

        assert engine.target_indices == [0]
        assert engine.chain_hop_indices == [[0]]
        assert len(engine.measured) == 1
        snapshot = engine.snapshot(columns=4, now=T + 2)
        assert snapshot.rows[0].primary.check_type == CheckType.ICMP
        assert snapshot.chains[0].hops[0].primary.check_type == CheckType.TCP

    def test_cause_falls_back_to_the_latest_failure_when_the_other_port_overwrote_it(self):
        router = Endpoint.parse("router")
        router.primary_check_type = CheckType.TCP
        engine = engine_with(Chain(name="Company", hops=[router]))
        [index] = engine.chain_hop_indices[0]
        try:
            # Port 443's probe fails at DNS, then port 80's probe overwrites the shared DNS layer.
            for port, at, error in ((443, T + 1, "No answer"), (80, T + 1.5, "SERVFAIL")):
                dns = failed(CheckType.DNS, at, error)
                tcp = dataclasses.replace(skipped(CheckType.TCP, dns), port=port)
                engine.store.apply(index, [dns, tcp])

            [snapshot] = engine.snapshot(columns=4, now=T + 2).chains
        finally:
            engine.resolver_executor.shutdown(wait=False)

        assert snapshot.problems[0] == "broken at router (DNS: SERVFAIL)"

    def test_ip_versions_of_one_host_stay_separate(self):
        v4 = Endpoint.parse("example.com")
        v4.ip_version = 4
        v6 = Endpoint.parse("example.com")
        v6.ip_version = 6
        engine = Engine(Config(endpoints=[v4], chains=[Chain(name="A", hops=[v6])]))
        try:
            assert engine.target_indices == [0]
            assert engine.chain_hop_indices == [[1]]
        finally:
            engine.resolver_executor.shutdown(wait=False)


class TestDashboardChains:
    def test_header_and_indented_hops_with_the_chain_problem(self):
        router = name(Endpoint.parse("192.0.2.1"), "Router")
        jumpbox = name(Endpoint.parse("198.51.100.1"), "Jumpbox")
        target = Endpoint.parse("203.0.113.1")
        engine = engine_with(Chain(name="Company", hops=[router, jumpbox]), targets=[target])
        router_index, jumpbox_index = engine.chain_hop_indices[0]
        now = time.time()
        engine.store.apply(router_index, [ok(CheckType.ICMP, now)])
        engine.store.apply(jumpbox_index, [failed(CheckType.ICMP, now, "Timeout")])

        console = Console(width=100, file=StringIO(), color_system=None)
        dashboard = Dashboard(engine, console=console)
        console.print(dashboard.render())
        lines = console.file.getvalue().splitlines()

        names = [line.split()[0] if line.strip() else "" for line in lines[:5]]
        assert names == ["203.0.113.1", "", "Company", "Router", "Jumpbox"]
        assert lines[2].lstrip().startswith("Company") and "broken at Jumpbox" in lines[2]
        indent = len(lines[3]) - len(lines[3].lstrip())
        assert indent == len(lines[2]) - len(lines[2].lstrip()) + 2
        assert "  • Company: broken at Jumpbox (ICMP: Timeout); Router OK" in lines
        assert not any("Jumpbox: ICMP" in line for line in lines)


class TestChainConfig:
    def write(self, tmp_path, text: str):
        path = tmp_path / "settings.toml"
        path.write_text("version = 2\n" + text)
        return path

    def test_chains_load_with_hops_in_the_target_formats(self, tmp_path):
        path = self.write(
            tmp_path,
            """
[[chains]]
name = "Company"
hops = [
    { url = "router", primary_check_type = "icmp", name = "Router" },
    "192.0.2.1",
    { url = "jumpbox.example.ts.net:22", name = "Jumpbox" },
    { url = "https://intranet.example.internal/health", name = "Intranet" },
]
""",
        )
        config = Config.load(path)

        assert config.endpoints == []
        [chain] = config.chains
        assert chain.name == "Company"
        assert [hop.display_name for hop in chain.hops] == [
            "Router",
            "192.0.2.1",
            "Jumpbox",
            "Intranet",
        ]
        assert chain.hops[0].primary_check_type == CheckType.ICMP

    @pytest.mark.parametrize(
        ("text", "error"),
        [
            (
                '[[chains]]\nname = "A"\nhops = ["192.0.2.1", { url = "192.0.2.2", nmae = "x" }]',
                r"Unknown key 'chains\[0\]\.hops\[1\]\.nmae'",
            ),
            (
                '[[chains]]\nname = "A"\nhops = ["192.0.2.1"]\nhop = []',
                r"Unknown key 'chains\[0\]\.hop'",
            ),
            ('[[chains]]\nhops = ["192.0.2.1"]', r"'chains\[0\]\.name' must be a non-empty"),
            ('[[chains]]\nname = "A"\nhops = []', r"'chains\[0\]\.hops' must list at least one"),
            ('[[chains]]\nname = "A"\nhops = "192.0.2.1"', r"'chains\[0\]\.hops' must be a list"),
            ('chains = "A"', r"'chains' must be an array of tables"),
        ],
    )
    def test_invalid_chains_are_rejected(self, tmp_path, text, error):
        path = self.write(tmp_path, text)
        with pytest.raises(ValueError, match=error) as raised:
            Config.load(path)
        if "Unknown key" in error:
            assert str(path) in str(raised.value)

    def test_no_targets_and_no_chains_is_an_error(self, tmp_path):
        path = self.write(tmp_path, "[endpoints]\ntargets = []\n")
        with pytest.raises(ValueError, match="No endpoints configured"):
            Config.load(path)

    def test_init_template_chain_example_loads_when_uncommented(self, tmp_path):
        path = create_default_config(tmp_path / "settings.toml")
        lines = path.read_text().splitlines()
        start = lines.index("# [[chains]]")
        end = next(i for i in range(start, len(lines)) if lines[i] == "# ]")
        for i in range(start, end + 1):
            lines[i] = lines[i].removeprefix("# ")
        path.write_text("\n".join(lines) + "\n")

        [chain] = Config.load(path).chains

        assert [hop.display_name for hop in chain.hops] == [
            "Router",
            "Internet",
            "Jumpbox",
            "Intranet",
        ]
