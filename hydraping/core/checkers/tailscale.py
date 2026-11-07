"""Tailscale peer checker: latency and path from ``tailscale ping``."""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass

from hydraping.core.checkers.base import BaseChecker
from hydraping.core.models import CheckType, Measurement

# "pong from jumpbox (192.0.2.7) via 198.51.100.20:41641 in 23ms". The parentheses also hold the
# peer API port when the peer has one: "(192.0.2.7, 41642)".
_PONG = re.compile(
    r"^pong from (?P<node>\S+) \((?P<ip>[^,)]+)(?:, \d+)?\) via (?P<via>.+) in (?P<latency>\S+)$"
)
_DERP = re.compile(r"^DERP\((?P<region>[^)]*)\)$")
_PEER_RELAY = re.compile(r"^peer-relay\(.*\)$")
_TIMED_OUT = re.compile(r'^ping ".*" timed out$')
# A Go duration as tailscale prints it: "23ms", "1.204s", "0s", "1m2.5s".
_GO_DURATION = re.compile(r"^(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h))+$")
_GO_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_DURATION_UNIT_MS = {
    "ns": 1e-6,
    "us": 1e-3,
    "µs": 1e-3,
    "ms": 1.0,
    "s": 1e3,
    "m": 60e3,
    "h": 3600e3,
}


@dataclass(frozen=True)
class TailscalePong:
    """One reply line of ``tailscale ping``."""

    node: str
    node_ip: str
    latency_ms: float
    # "DERP <region>" or "peer relay" for a relayed reply, None for a direct one.
    relay: str | None


def parse_go_duration_ms(text: str) -> float | None:
    """Return a Go duration string such as ``23ms`` or ``1.204s`` in milliseconds, or None."""
    if not _GO_DURATION.match(text):
        return None
    return sum(
        float(value) * _DURATION_UNIT_MS[unit] for value, unit in _GO_DURATION_PART.findall(text)
    )


def parse_pong(line: str) -> TailscalePong | None:
    """Return the reply that ``line`` reports, or None when it is not a ``pong from`` line.

    The ``via`` part names the path: an ``address:port`` for a direct path, ``DERP(<region>)`` for
    Tailscale's relay servers, and ``peer-relay(...)`` for a relay on another node of the tailnet.
    """
    match = _PONG.match(line.strip())
    if match is None:
        return None
    latency_ms = parse_go_duration_ms(match["latency"])
    if latency_ms is None:
        return None
    via = match["via"]
    derp = _DERP.match(via)
    if derp is not None:
        relay: str | None = f"DERP {derp['region']}".rstrip()
    elif _PEER_RELAY.match(via):
        relay = "peer relay"
    else:
        relay = None
    return TailscalePong(
        node=match["node"], node_ip=match["ip"], latency_ms=latency_ms, relay=relay
    )


def parse_tailscale_ping(output: str, returncode: int | None = None) -> Measurement:
    """Return the measurement that the output of ``tailscale ping -c 1`` reports.

    The first ``pong from`` line makes it a success, whatever the exit status: with one ping over
    a relay, ``tailscale ping`` replies and then exits with "direct connection not established".
    Without a reply, the error names what went wrong: no reply in time, tailscaled not running,
    or what ``tailscale`` printed, such as "no matching peer".
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in lines:
        pong = parse_pong(line)
        if pong is not None:
            return Measurement(
                check_type=CheckType.TAILSCALE,
                success=True,
                latency_ms=pong.latency_ms,
                resolved_ip=pong.node_ip,
                relay=pong.relay,
            )
    return _failed(_error_message(lines, returncode))


def _error_message(lines: Sequence[str], returncode: int | None) -> str:
    if any(_TIMED_OUT.match(line) or line == "no reply" for line in lines):
        return "No reply from the peer"
    for line in lines:
        if line.startswith("failed to connect to local tailscaled"):
            return "tailscaled is not running or not reachable"
    if lines:
        return lines[0]
    if returncode:
        return f"tailscale ping exited with status {returncode} without output"
    return "tailscale ping printed no reply"


def _failed(message: str) -> Measurement:
    return Measurement(check_type=CheckType.TAILSCALE, success=False, error_message=message)


class TailscaleChecker(BaseChecker):
    """Ping a Tailscale peer with the ``tailscale`` CLI and report the latency and the path.

    Each check runs ``tailscale ping -c 1 --timeout <timeout>s -- <peer>`` and stops reading at the
    first reply. The child is killed and reaped on every path: after the reply (``tailscale ping``
    waits a second before it exits), on timeout, and when the probe is cancelled.
    """

    def __init__(self, timeout: float = 5.0, command: Sequence[str] = ("tailscale",)):
        super().__init__(timeout)
        self.command = tuple(command)
        # The Tailscale IP each peer answered from last, for the outgoing interface lookup.
        self._node_ips: dict[str, str] = {}

    def get_last_node_ip(self, peer: str) -> str | None:
        """Return the Tailscale IP the peer last replied from, or None before its first reply."""
        return self._node_ips.get(peer)

    async def check(self, target: str, **kwargs) -> Measurement:
        """Ping the peer ``target`` (a name or a Tailscale IP) once."""
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                "ping",
                "-c",
                "1",
                "--timeout",
                f"{self.timeout:g}s",
                # A peer name starting with "-" must not be read as a flag.
                "--",
                target,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return _failed(f"{self.command[0]} CLI not found")
        except OSError as e:
            return _failed(f"Could not run {self.command[0]}: {e}")

        lines: list[str] = []

        async def reply_or_exit() -> int | None:
            """Return None once a reply came, or the exit status when the output ended without."""
            assert process.stdout is not None
            if await _read_until_pong(process.stdout, lines):
                return None
            return await process.wait()

        try:
            returncode = await asyncio.wait_for(reply_or_exit(), self.timeout)
        except TimeoutError:
            await _reap(process)
            return _failed(f"No reply within {self.timeout:g}s")
        except BaseException:
            # Cancelled with a newer probe or the engine: never leave the child running.
            await _reap(process)
            raise
        if returncode is None:
            # The reply is all we need; tailscale would idle for another second before exiting.
            await _reap(process)

        measurement = parse_tailscale_ping("\n".join(lines), returncode)
        if measurement.success and measurement.resolved_ip:
            self._node_ips[target] = measurement.resolved_ip
        return measurement


async def _read_until_pong(stream: asyncio.StreamReader, lines: list[str]) -> bool:
    """Collect output lines into ``lines`` until a reply or the end; return whether one came."""
    while True:
        raw = await stream.readline()
        if not raw:
            return False
        line = raw.decode(errors="replace").strip()
        if line:
            lines.append(line)
            if parse_pong(line) is not None:
                return True


async def _reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass  # It exited on its own in the meantime; wait() reaps it.
    await process.wait()
