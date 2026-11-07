"""Tests for the Tailscale hop check: the output parser, the CLI runner with fake commands, the
config, the engine wiring and the dashboard. One opt-in integration test runs the real
``tailscale``, against the peer HYDRAPING_TEST_TAILSCALE_PEER names."""

import asyncio
import os
import re
import time
from io import StringIO

import pytest
from rich.console import Console

from hydraping.core.checkers.tailscale import (
    TailscaleChecker,
    TailscalePong,
    parse_go_duration_ms,
    parse_pong,
    parse_tailscale_ping,
)
from hydraping.core.config import Config
from hydraping.core.engine import Engine
from hydraping.core.models import CheckResult, CheckType, LayerKey, TailscalePeerEndpoint
from hydraping.tui.dashboard import Dashboard

# Sample outputs of `tailscale ping -c 1`, written by hand with documentation addresses. stderr is
# merged into stdout, as the checker runs it.
DIRECT = "pong from jumpbox (192.0.2.7) via 198.51.100.20:41641 in 23ms\n"
DIRECT_IPV6 = "pong from jumpbox (192.0.2.7) via [2001:db8::20]:41641 in 1.204s\n"
DERP_RELAY = (
    "pong from jumpbox (192.0.2.7) via DERP(xyz) in 45ms\ndirect connection not established\n"
)
PEER_RELAY = "pong from jumpbox (192.0.2.7) via peer-relay(203.0.113.5:7777:vni:3) in 31ms\n"
WITH_PEER_API_PORT = "pong from jumpbox (192.0.2.7, 41642) via 198.51.100.20:41641 in 8ms\n"
TIMED_OUT = 'ping "192.0.2.7" timed out\nno reply\n'
NO_SUCH_PEER = 'error looking up IP of "jumpbox": lookup jumpbox on 192.0.2.53:53: no such host\n'
NO_MATCHING_PEER = "no matching peer\n"
DAEMON_NOT_RUNNING = (
    "failed to connect to local tailscaled; it doesn't appear to be running "
    "(sudo systemctl start tailscaled ?)\n"
)


class TestParseGoDuration:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("23ms", 23.0), ("1.204s", 1204.0), ("0s", 0.0), ("1m2.5s", 62500.0), ("750µs", 0.75)],
    )
    def test_durations(self, text, expected):
        assert parse_go_duration_ms(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "23", "ms", "fast", "23ms!"])
    def test_anything_else_is_none(self, text):
        assert parse_go_duration_ms(text) is None


class TestParsePong:
    def test_direct(self):
        assert parse_pong(DIRECT) == TailscalePong(
            node="jumpbox", node_ip="192.0.2.7", latency_ms=23.0, relay=None
        )

    def test_peer_api_port_is_not_part_of_the_address(self):
        pong = parse_pong(WITH_PEER_API_PORT)
        assert pong is not None and pong.node_ip == "192.0.2.7" and pong.relay is None

    def test_other_lines_are_not_replies(self):
        assert parse_pong("direct connection not established") is None
        assert parse_pong('ping "192.0.2.7" timed out') is None


class TestParseTailscalePing:
    def test_direct_path(self):
        result = parse_tailscale_ping(DIRECT, 0)

        assert result.check_type == CheckType.TAILSCALE
        assert result.success and result.latency_ms == 23.0
        assert result.relay is None
        assert result.resolved_ip == "192.0.2.7"

    def test_direct_path_over_ipv6_with_seconds(self):
        result = parse_tailscale_ping(DIRECT_IPV6, 0)
        assert result.success and result.latency_ms == pytest.approx(1204.0)
        assert result.relay is None

    def test_derp_relay_is_a_success_despite_the_exit_status(self):
        result = parse_tailscale_ping(DERP_RELAY, 1)

        assert result.success and result.latency_ms == 45.0
        assert result.relay == "DERP xyz"

    def test_peer_relay(self):
        result = parse_tailscale_ping(PEER_RELAY, 0)
        assert result.success and result.relay == "peer relay"

    @pytest.mark.parametrize(
        ("output", "error"),
        [
            (TIMED_OUT, "No reply from the peer"),
            (NO_SUCH_PEER, NO_SUCH_PEER.strip()),
            (NO_MATCHING_PEER, "no matching peer"),
            (DAEMON_NOT_RUNNING, "tailscaled is not running or not reachable"),
            ("", "tailscale ping exited with status 1 without output"),
        ],
    )
    def test_failures(self, output, error):
        result = parse_tailscale_ping(output, 1)

        assert result.check_type == CheckType.TAILSCALE
        assert not result.success and not result.skipped
        assert result.error_message == error


def fake_tailscale(script: str) -> tuple[str, ...]:
    """Return a command that runs ``script`` with the ping arguments as "$@".

    The script must ``exec`` anything it waits on: killing the shell alone would leave a forked
    child holding the pipe open.
    """
    return ("sh", "-c", script, "tailscale")


class TestTailscaleChecker:
    async def test_reply_is_parsed_and_the_node_ip_kept(self):
        checker = TailscaleChecker(timeout=2.0, command=fake_tailscale(f"printf '{DERP_RELAY}'"))

        result = await checker.check("jumpbox")

        assert result.success and result.relay == "DERP xyz"
        assert checker.get_last_node_ip("jumpbox") == "192.0.2.7"

    async def test_arguments(self, tmp_path):
        args_file = tmp_path / "args"
        script = f'printf "%s\\n" "$@" > {args_file}; printf \'{DIRECT}\''
        checker = TailscaleChecker(timeout=0.5, command=fake_tailscale(script))

        assert (await checker.check("jumpbox.example.ts.net")).success
        assert args_file.read_text().split() == [
            "ping",
            "-c",
            "1",
            "--timeout",
            "0.5s",
            "--",
            "jumpbox.example.ts.net",
        ]

    async def test_stops_at_the_reply_without_waiting_for_the_exit(self):
        # tailscale idles for a second after a relayed reply; the check must not wait for it.
        script = f"printf '{DERP_RELAY}'; exec sleep 10"
        checker = TailscaleChecker(timeout=5.0, command=fake_tailscale(script))

        started = time.monotonic()
        result = await checker.check("jumpbox")

        assert result.success
        assert time.monotonic() - started < 2.0

    async def test_error_output(self):
        script = f"printf '{NO_MATCHING_PEER}'; exit 1"
        checker = TailscaleChecker(timeout=2.0, command=fake_tailscale(script))

        result = await checker.check("jumpbox")

        assert not result.success and result.error_message == "no matching peer"
        assert checker.get_last_node_ip("jumpbox") is None

    async def test_no_output_in_time_is_a_timeout(self):
        checker = TailscaleChecker(timeout=0.2, command=fake_tailscale("exec sleep 10"))

        started = time.monotonic()
        result = await checker.check("jumpbox")

        assert not result.success and result.error_message == "No reply within 0.2s"
        assert time.monotonic() - started < 2.0

    async def test_output_closed_without_an_exit_is_a_timeout(self):
        # The output ends, but the process lingers: waiting for its exit has the timeout too.
        checker = TailscaleChecker(timeout=0.2, command=fake_tailscale("exec sleep 10 >&- 2>&-"))

        started = time.monotonic()
        result = await checker.check("jumpbox")

        assert not result.success and result.error_message == "No reply within 0.2s"
        assert time.monotonic() - started < 2.0

    async def test_missing_cli(self, tmp_path):
        checker = TailscaleChecker(command=(str(tmp_path / "tailscale"),))

        result = await checker.check("jumpbox")

        assert not result.success
        assert result.error_message == f"{tmp_path / 'tailscale'} CLI not found"

    async def test_cancel_kills_the_command(self):
        checker = TailscaleChecker(timeout=30.0, command=fake_tailscale("exec sleep 10"))
        task = asyncio.ensure_future(checker.check("jumpbox"))
        await asyncio.sleep(0.1)

        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 2.0


class TestTailscaleHopConfig:
    def load(self, tmp_path, target: str) -> Config:
        path = tmp_path / "settings.toml"
        path.write_text(f"version = 2\n[endpoints]\ntargets = [{target}]\n")
        return Config.load(path)

    @pytest.mark.parametrize(
        "peer", ["jumpbox", "jumpbox.example.ts.net", "192.0.2.7", "2001:db8::7"]
    )
    def test_peer_names_and_addresses(self, tmp_path, peer):
        config = self.load(tmp_path, f'{{ url = "{peer}", check = "tailscale", name = "VPN" }}')

        [endpoint] = config.endpoints
        assert endpoint == TailscalePeerEndpoint(raw=peer, peer=peer, custom_name="VPN")
        assert endpoint.get_primary_layer() == LayerKey(CheckType.TAILSCALE)

    @pytest.mark.parametrize(
        ("target", "error"),
        [
            ('{ url = "jumpbox:22", check = "tailscale" }', "needs a peer name or Tailscale IP"),
            ('{ url = "https://jumpbox/", check = "tailscale" }', "needs a peer name"),
            ('{ url = "--socket=/tmp/x", check = "tailscale" }', "needs a peer name"),
            ('{ url = "jump box", check = "tailscale" }', "needs a peer name"),
            (
                '{ url = "jumpbox", check = "tailscale", query = "example.com" }',
                "'query' needs check = \"dns\"",
            ),
            (
                '{ url = "jumpbox", check = "tailscale", ip_version = 4 }',
                "'ip_version' cannot be combined",
            ),
            (
                '{ url = "jumpbox", check = "tailscale", primary_check_type = "icmp" }',
                "not applicable",
            ),
        ],
    )
    def test_invalid_tailscale_hops_are_rejected(self, tmp_path, target, error):
        with pytest.raises(ValueError, match=re.escape(error)):
            self.load(tmp_path, target)


def tailscale_result(at: float, relay: str | None, success: bool = True) -> CheckResult:
    return CheckResult(
        check_type=CheckType.TAILSCALE,
        success=success,
        latency_ms=23.0 if success else None,
        error_message=None if success else "No reply from the peer",
        relay=relay,
        resolved_ip="192.0.2.7" if success else None,
        started_at=at - 0.1,
        finished_at=at,
    )


class TestEngineAndDashboard:
    def engine(self) -> Engine:
        peer = TailscalePeerEndpoint(raw="jumpbox", peer="jumpbox", custom_name="VPN")
        return Engine(Config(endpoints=[peer]))

    def row_lines(self, engine: Engine) -> list[str]:
        """Return the rendered lines of the VPN row, leaving out the problem lines."""
        lines = self.render(engine).splitlines()
        return [line for line in lines if line.lstrip().startswith("VPN")]

    def render(self, engine: Engine) -> str:
        console = Console(width=100, file=StringIO(), color_system=None)
        dashboard = Dashboard(engine, console=console)
        console.print(dashboard.render())
        return console.file.getvalue()

    def test_one_tailscale_schedule_and_the_node_ip_for_the_interface(self):
        engine = self.engine()
        try:
            assert [s.name for s in engine.schedules] == ["VPN tailscale"]
            assert engine._endpoint_address(engine.endpoints[0]) is None
            engine.tailscale_checker._node_ips["jumpbox"] = "192.0.2.7"
            assert engine._endpoint_address(engine.endpoints[0]) == "192.0.2.7"
        finally:
            engine.resolver_executor.shutdown(wait=False)

    def test_relayed_reply_shows_the_relay_in_place_of_the_interface(self):
        engine = self.engine()
        try:
            engine._interfaces = {0: "tailscale0"}
            engine.store.apply(0, [tailscale_result(time.time(), "DERP xyz")])

            [row] = engine.snapshot(columns=10).rows
            assert row.relay == "DERP xyz"
            [line] = self.row_lines(engine)
            assert re.search(r"23\.0ms +\(TS relay\) +via DERP xyz *$", line)
        finally:
            engine.resolver_executor.shutdown(wait=False)

    def test_direct_reply_shows_the_interface(self):
        engine = self.engine()
        try:
            engine._interfaces = {0: "tailscale0"}
            engine.store.apply(0, [tailscale_result(time.time(), None)])

            assert engine.snapshot(columns=10).rows[0].relay is None
            [line] = self.row_lines(engine)
            assert re.search(r"23\.0ms +\(TS direct\) +via tailscale0 *$", line)
        finally:
            engine.resolver_executor.shutdown(wait=False)

    def test_failure(self):
        engine = self.engine()
        try:
            engine.store.apply(0, [tailscale_result(time.time(), None, success=False)])

            snapshot = engine.snapshot(columns=10)
            assert snapshot.rows[0].problems == ("TAILSCALE: No reply from the peer",)
            [line] = self.row_lines(engine)
            assert re.search(r"FAIL +\(TS\) *$", line)
        finally:
            engine.resolver_executor.shutdown(wait=False)


# Opt-in, so a test run doesn't ping a peer on the developer's tailnet unasked.
TAILSCALE_PEER = os.environ.get("HYDRAPING_TEST_TAILSCALE_PEER", "")


@pytest.mark.skipif(
    not TAILSCALE_PEER, reason="set HYDRAPING_TEST_TAILSCALE_PEER to a peer to ping it"
)
async def test_real_tailscale_ping_has_the_expected_shape():
    peer = TAILSCALE_PEER
    result = await TailscaleChecker(timeout=5.0).check(peer)

    # Only the shape: names, addresses and paths differ from machine to machine.
    assert result.check_type == CheckType.TAILSCALE
    if result.success:
        assert result.latency_ms is not None and result.latency_ms >= 0
        assert result.relay in (None, "peer relay") or result.relay.startswith("DERP ")
        assert result.resolved_ip
    else:
        assert result.error_message
