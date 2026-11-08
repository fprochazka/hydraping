"""The texts of a row, decided once for every frontend.

The terminal UI and the desktop UI show the same latency, protocol label, route and problem lines.
This module turns snapshots into those texts and colours, so both frontends only style them.
"""

from dataclasses import dataclass
from enum import Enum

from hydraping.core.graphcells import LatencyColor, latency_color
from hydraping.core.models import CheckResult, CheckType
from hydraping.core.snapshot import ChainSnapshot, EndpointSnapshot, EngineSnapshot

ICMP_UNAVAILABLE = "ICMP unavailable (no permissions) - ping checks disabled"


@dataclass(frozen=True)
class LatencyText:
    """The latency column and the protocol column of a row.

    ``text`` is the latency (``12.3ms``), ``FAIL`` or ``N/A``. ``label`` is the layer in
    parentheses, for example ``(TCP:443)``, or empty without data. ``color`` is None without data.
    """

    text: str
    label: str
    color: LatencyColor | None


def layer_label(check_type: CheckType) -> str:
    """Return the short name of a layer for the protocol column."""
    return "TS" if check_type == CheckType.TAILSCALE else check_type.value.upper()


def latency_text(current: CheckResult | None) -> LatencyText:
    """Return what the latency and protocol columns show for the primary layer's latest result.

    A failure names the layer that broke, which for a skipped primary layer is an earlier one, so
    an HTTPS row whose TCP connect failed shows ``FAIL (TCP)``.
    """
    if current is None:
        return LatencyText("N/A", "", None)
    if current.success and current.latency_ms is not None:
        check_label = current.check_type.value.upper()
        if current.check_type == CheckType.TCP and current.port:
            check_label = f"TCP:{current.port}"
        elif current.check_type == CheckType.HTTP and current.protocol:
            check_label = current.protocol.upper()
        elif current.check_type == CheckType.TAILSCALE:
            check_label = "TS relay" if current.relay else "TS direct"
        return LatencyText(
            f"{current.latency_ms:.1f}ms", f"({check_label})", latency_color(current.latency_ms)
        )
    failed_layer = current.failed_layer or current.check_type
    return LatencyText("FAIL", f"({layer_label(failed_layer)})", LatencyColor.RED)


def interface_label(interface: str) -> str:
    return f"via {interface}"


def route_label(row: EndpointSnapshot) -> str:
    """Return the text of the interface column: the relay of a relayed Tailscale peer, otherwise
    the outgoing interface, or nothing when neither is known."""
    if row.relay:
        return interface_label(row.relay)
    return interface_label(row.interface) if row.interface else ""


class ChainState(str, Enum):
    BROKEN = "broken"
    OK = "ok"
    # Some hop has no result yet.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ChainStatus:
    """What a chain's header row shows where the endpoint rows have their graph."""

    state: ChainState
    text: str


def chain_status(chain: ChainSnapshot) -> ChainStatus:
    """Return ``broken at <hop>``, ``OK`` once every hop has a result, or nothing before that."""
    if chain.broken_at is not None:
        hop = chain.hops[chain.broken_at].endpoint.display_name
        return ChainStatus(ChainState.BROKEN, f"broken at {hop}")
    if chain.hops and all(hop.current is not None for hop in chain.hops):
        return ChainStatus(ChainState.OK, "OK")
    return ChainStatus(ChainState.UNKNOWN, "")


def problem_lines(snapshot: EngineSnapshot) -> list[str]:
    """Return every problem line of a snapshot, each prefixed with its row's or chain's name."""
    problems = []
    if not snapshot.icmp_available:
        problems.append(ICMP_UNAVAILABLE)
    for row in snapshot.rows:
        for problem in row.problems:
            problems.append(f"{row.endpoint.display_name}: {problem}")
    for chain in snapshot.chains:
        for problem in chain.problems:
            problems.append(f"{chain.name}: {problem}")
    return problems
