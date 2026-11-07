"""Tests for the outgoing interface lookup (real ``ip`` and small shell commands)."""

import asyncio
import shutil
import threading
import time
from io import StringIO

import pytest
from rich.cells import cell_len
from rich.console import Console

from hydraping.core.config import ChecksConfig, Config
from hydraping.core.engine import Engine
from hydraping.core.models import Endpoint
from hydraping.core.route import NO_ROUTE, EgressInterfaces, parse_route_get
from hydraping.tui.dashboard import Dashboard

requires_ip = pytest.mark.skipif(shutil.which("ip") is None, reason="iproute2 is not installed")

# Prints a route for whatever address it gets as $1, and counts its runs in the file $0 names.
FAKE_IP = (
    "sh",
    "-c",
    'echo run >> "$0"; printf \'[{"dst":"%s","dev":"eth0"}]\' "$1"',
)

# Passes the self-test for 127.0.0.1, then hangs like an unanswered lookup for anything else.
# ``exec`` replaces the shell, so killing the process also closes the pipe; a forked ``sleep``
# would keep it open after the shell dies.
HANGING_IP = (
    "sh",
    "-c",
    'case "$1" in 127.0.0.1) printf \'[{"dev":"lo"}]\';; *) exec sleep 10;; esac',
    "sh",
)

# Counts its runs in the file $0 names, fails for every address with no output on stdout.
BROKEN_IP = ("sh", "-c", 'echo run >> "$0"; echo "Error: something broke" >&2; exit 2')

# Counts its runs in the file $0 names. Has no route to 192.0.2.x, fails differently for
# 198.51.100.x, and answers for anything else.
PARTIAL_IP = (
    "sh",
    "-c",
    'echo run >> "$0"; case "$1" in '
    "192.0.2.*) echo 'RTNETLINK answers: Network is unreachable' >&2; exit 2;; "
    "198.51.100.*) echo 'Error: inet prefix is expected' >&2; exit 1;; "
    'esac; printf \'[{"dst":"%s","dev":"eth0"}]\' "$1"',
)


class TestParseRouteGet:
    def test_gateway_route(self):
        output = (
            '[{"dst":"203.0.113.10","gateway":"192.0.2.1","dev":"wlan0","prefsrc":"192.0.2.20",'
            '"flags":[],"uid":1000,"cache":[]}]'
        )
        assert parse_route_get(output) == "wlan0"

    def test_bytes_are_accepted(self):
        assert parse_route_get(b'[{"type":"local","dst":"127.0.0.1","dev":"lo"}]') == "lo"

    @pytest.mark.parametrize(
        "output", ["", "not json", "[]", "{}", '[{"dst":"192.0.2.1"}]', '[{"dev":""}]', "[1]"]
    )
    def test_anything_without_an_interface_is_none(self, output):
        assert parse_route_get(output) is None


class TestEgressInterfaces:
    @requires_ip
    async def test_loopback_goes_through_lo(self):
        assert await EgressInterfaces().interface_for("127.0.0.1") == "lo"

    @requires_ip
    async def test_ip_failing_gives_none(self):
        assert await EgressInterfaces().interface_for("not-an-address") is None

    async def test_missing_command_gives_none_and_is_not_retried(self, tmp_path):
        missing = str(tmp_path / "no-such-ip")
        egress = EgressInterfaces(command=(missing,), cache_seconds=0.0)

        assert await egress.interface_for("192.0.2.1") is None
        assert await egress.interface_for("192.0.2.2") is None
        assert egress.lookups == 0

    async def test_ip_failing_its_self_test_disables_lookups(self, tmp_path):
        runs = tmp_path / "runs"
        egress = EgressInterfaces(command=(*BROKEN_IP, str(runs)), cache_seconds=0.0)

        for last_octet in range(1, 4):
            assert await egress.interface_for(f"203.0.113.{last_octet}") is None

        assert runs.read_text().count("run") == 1  # Only the self-test.
        assert egress.lookups == 0

    async def test_address_without_a_route_is_labelled_and_others_keep_working(self, tmp_path):
        runs = tmp_path / "runs"
        egress = EgressInterfaces(command=(*PARTIAL_IP, str(runs)), cache_seconds=60.0)

        assert await egress.interface_for("192.0.2.1") == NO_ROUTE
        assert await egress.interface_for("198.51.100.1") is None
        assert await egress.interface_for("203.0.113.1") == "eth0"
        assert await egress.interface_for("192.0.2.1") == NO_ROUTE  # Cached.

        assert runs.read_text().count("run") == 4  # The self-test and three addresses.

    async def test_many_failing_addresses_in_a_row_never_disable_lookups(self, tmp_path):
        runs = tmp_path / "runs"
        egress = EgressInterfaces(command=(*PARTIAL_IP, str(runs)), cache_seconds=0.0)

        for last_octet in range(1, 21):
            assert await egress.interface_for(f"192.0.2.{last_octet}") == NO_ROUTE
            assert await egress.interface_for(f"198.51.100.{last_octet}") is None

        assert await egress.interface_for("203.0.113.1") == "eth0"
        assert egress.lookups == 41

    async def test_cache_bounds_the_subprocess_rate(self, tmp_path):
        runs = tmp_path / "runs"
        egress = EgressInterfaces(command=(*FAKE_IP, str(runs)), cache_seconds=60.0)

        results = await asyncio.gather(*(egress.interface_for("192.0.2.1") for _ in range(5)))
        assert results == ["eth0"] * 5
        assert await egress.interface_for("192.0.2.1") == "eth0"
        assert await egress.interface_for("198.51.100.1") == "eth0"

        assert runs.read_text().count("run") == 3  # The self-test and two addresses.

    async def test_expired_entry_is_looked_up_again(self, tmp_path):
        runs = tmp_path / "runs"
        egress = EgressInterfaces(command=(*FAKE_IP, str(runs)), cache_seconds=0.05)

        await egress.interface_for("192.0.2.1")
        await asyncio.sleep(0.1)
        await egress.interface_for("192.0.2.1")

        assert runs.read_text().count("run") == 3  # The self-test and two lookups.

    async def test_hanging_command_times_out_to_none(self):
        egress = EgressInterfaces(command=HANGING_IP, timeout=0.1)

        started = time.monotonic()
        assert await egress.interface_for("192.0.2.1") is None
        assert time.monotonic() - started < 2.0

    async def test_cancelled_lookup_kills_the_command(self):
        egress = EgressInterfaces(command=HANGING_IP, timeout=30.0)
        task = asyncio.ensure_future(egress.interface_for("192.0.2.1"))
        await asyncio.sleep(0.1)

        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 2.0


FAST_CHECKS = ChecksConfig(probe_interval_seconds=0.05, bucket_seconds=0.5, timeout_seconds=0.5)


class TestEngineInterfaces:
    @requires_ip
    def test_snapshot_carries_the_interface(self):
        engine = Engine(Config(endpoints=[Endpoint.parse("127.0.0.1")], checks=FAST_CHECKS))
        try:
            engine.start()
            deadline = time.monotonic() + 3.0
            while engine.snapshot(columns=1).rows[0].interface is None:
                if time.monotonic() >= deadline:
                    pytest.fail("no interface within 3 s")
                time.sleep(0.02)
            assert engine.snapshot(columns=1).rows[0].interface == "lo"
        finally:
            engine.stop()
        assert not any(t.name == "hydraping-engine" for t in threading.enumerate())

    def test_missing_ip_leaves_the_interface_out_and_probing_goes_on(self, tmp_path):
        engine = Engine(Config(endpoints=[Endpoint.parse("127.0.0.1")], checks=FAST_CHECKS))
        engine.egress = EgressInterfaces(command=(str(tmp_path / "no-such-ip"),))
        try:
            engine.start()
            time.sleep(0.3)
            assert engine.snapshot(columns=1).rows[0].interface is None
            assert engine.egress.lookups == 0
        finally:
            engine.stop()

    def test_name_not_resolved_yet_has_no_interface(self):
        engine = Engine(Config(endpoints=[Endpoint.parse("example.com")]))
        try:
            assert engine._endpoint_address(engine.endpoints[0]) is None
        finally:
            engine.resolver_executor.shutdown(wait=False)


def _render_lines(engine: Engine, width: int) -> list[str]:
    dashboard = Dashboard(engine, console=Console(width=width, file=StringIO(), color_system=None))
    dashboard.console.print(dashboard.render())
    return dashboard.console.file.getvalue().splitlines()


class TestDashboardInterface:
    def _engine(self) -> Engine:
        endpoints = [Endpoint.parse("192.0.2.1"), Endpoint.parse("198.51.100.1")]
        engine = Engine(Config(endpoints=endpoints))
        engine._interfaces = {0: "tailscale0"}
        return engine

    def test_row_shows_the_interface_as_a_suffix(self):
        lines = _render_lines(self._engine(), 100)

        [row] = [line for line in lines if line.lstrip().startswith("192.0.2.1")]
        assert row.rstrip().endswith("via tailscale0")
        [other] = [line for line in lines if line.lstrip().startswith("198.51.100.1")]
        assert "via" not in other
        assert all(cell_len(line) <= 100 for line in lines)

    def test_narrow_terminal_drops_the_interface_before_the_graph(self):
        # 14 for the name, 27 fixed and a 20-column graph: no room for "via tailscale0".
        lines = _render_lines(self._engine(), 62)

        assert not any("via" in line for line in lines)
        assert all(cell_len(line) <= 62 for line in lines)
