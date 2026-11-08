"""What the top-bar item shows, decided from a snapshot without Qt or D-Bus.

The item stands for a few rows: each chain is one row, and so is every target with ``tray = true``.
This module turns a snapshot into those rows, the label next to the icon, the tooltip, and the
bars the icon draws.
"""

from dataclasses import dataclass
from enum import Enum

from hydraping.core.display import chain_status, latency_text
from hydraping.core.graphcells import BAR_LEVELS, LatencyColor, bar_level, latency_color
from hydraping.core.snapshot import ChainSnapshot, EndpointSnapshot, EngineSnapshot
from hydraping.gui.colors import LATENCY_RGB, NO_DATA_RGB, RGB

# The icon has room for this many bars; rows after them only count in the label and the tooltip.
MAX_ICON_BARS = 5

TOOLTIP_TITLE = "HydraPing"
# The label before the first result.
NO_DATA_LABEL = "…"
# The widest label the item usually shows. Hosts that support it reserve this much room, so the
# icon doesn't move when the latency gets a digit longer.
LABEL_GUIDE = "9999 ms"

# The colour zones from fast to slow. A bar's height grows with its zone and with the bar level
# within the zone, so a slower row always has a taller bar.
_ZONES = (LatencyColor.GREEN, LatencyColor.YELLOW, LatencyColor.ORANGE, LatencyColor.RED)
# Bars of measured latencies span this part of the icon's height. A full-height bar means down.
_MIN_BAR_HEIGHT = 0.3
_MAX_LATENCY_BAR_HEIGHT = 0.9


class RowState(str, Enum):
    NO_DATA = "no_data"
    OK = "ok"
    DOWN = "down"


@dataclass(frozen=True)
class TrayRow:
    """One row the item stands for: a chain or a target.

    ``latency_ms`` is what the label compares: the target's latency, or the last hop's for a chain.
    ``worst_latency_ms`` decides the bar: the same for a target, the slowest hop's for a chain.
    ``down_name`` is the label's subject when the row is down: the broken hop for a chain.
    """

    name: str
    state: RowState
    is_chain: bool
    tooltip: str
    latency_ms: float | None = None
    worst_latency_ms: float | None = None
    down_name: str | None = None


@dataclass(frozen=True)
class IconBar:
    """One bar of the icon: its colour and its height as a fraction of the icon's height."""

    rgb: RGB
    height: float


@dataclass(frozen=True)
class TrayStatus:
    label: str
    tooltip_title: str
    tooltip: str
    bars: tuple[IconBar, ...]


def _latency_ms(row: EndpointSnapshot) -> float | None:
    current = row.current
    if current is not None and current.success:
        return current.latency_ms
    return None


def endpoint_tray_row(row: EndpointSnapshot) -> TrayRow:
    name = row.endpoint.display_name
    latency = latency_text(row.current)
    tooltip = f"{name}: {latency.text} {latency.label}".rstrip()
    if row.current is None:
        return TrayRow(name, RowState.NO_DATA, is_chain=False, tooltip=tooltip)
    latency_ms = _latency_ms(row)
    if latency_ms is None:
        return TrayRow(name, RowState.DOWN, is_chain=False, tooltip=tooltip, down_name=name)
    return TrayRow(
        name,
        RowState.OK,
        is_chain=False,
        tooltip=tooltip,
        latency_ms=latency_ms,
        worst_latency_ms=latency_ms,
    )


def chain_tray_row(chain: ChainSnapshot) -> TrayRow:
    status = chain_status(chain)
    if chain.broken_at is not None:
        hop = chain.hops[chain.broken_at].endpoint.display_name
        return TrayRow(
            chain.name,
            RowState.DOWN,
            is_chain=True,
            tooltip=f"{chain.name}: {status.text}",
            down_name=hop,
        )
    latencies = [ms for ms in (_latency_ms(hop) for hop in chain.hops) if ms is not None]
    if not latencies:
        return TrayRow(chain.name, RowState.NO_DATA, is_chain=True, tooltip=f"{chain.name}: N/A")
    last_hop_ms = _latency_ms(chain.hops[-1])
    parts = [status.text] if status.text else []
    if last_hop_ms is not None:
        parts.append(latency_text(chain.hops[-1].current).text)
    return TrayRow(
        chain.name,
        RowState.OK,
        is_chain=True,
        tooltip=f"{chain.name}: {', '.join(parts) or 'N/A'}",
        latency_ms=last_hop_ms,
        worst_latency_ms=max(latencies),
    )


def tray_rows(snapshot: EngineSnapshot) -> list[TrayRow]:
    """Return the rows the item stands for: the chains first, then targets with ``tray = true``."""
    rows = [chain_tray_row(chain) for chain in snapshot.chains]
    rows += [endpoint_tray_row(row) for row in snapshot.rows if row.endpoint.tray]
    return rows


def tray_label(rows: list[TrayRow]) -> str:
    """Return the text next to the icon.

    A broken chain names its broken hop (``Jumpbox down``), failing targets are counted
    (``2 down``), and otherwise the slowest row's latency shows (``38 ms``). Before the first
    result it is ``…``, and without any row it is empty.
    """
    if not rows:
        return ""
    for row in rows:
        if row.is_chain and row.state == RowState.DOWN:
            return f"{row.down_name} down"
    down = sum(1 for row in rows if not row.is_chain and row.state == RowState.DOWN)
    if down:
        return f"{down} down"
    latencies = [row.latency_ms for row in rows if row.latency_ms is not None]
    if not latencies:
        return NO_DATA_LABEL
    return f"{max(latencies):.0f} ms"


def bar_height(latency_ms: float) -> float:
    """Return the height of a bar for a latency, between the minimum and the down height."""
    zone = _ZONES.index(latency_color(latency_ms))
    position = (zone + bar_level(latency_ms) / (BAR_LEVELS - 1)) / len(_ZONES)
    return _MIN_BAR_HEIGHT + position * (_MAX_LATENCY_BAR_HEIGHT - _MIN_BAR_HEIGHT)


def icon_bar(row: TrayRow) -> IconBar:
    if row.state == RowState.DOWN:
        return IconBar(LATENCY_RGB[LatencyColor.RED], 1.0)
    if row.worst_latency_ms is None:
        return IconBar(NO_DATA_RGB, _MIN_BAR_HEIGHT)
    latency_ms = row.worst_latency_ms
    return IconBar(LATENCY_RGB[latency_color(latency_ms)], bar_height(latency_ms))


def icon_bars(rows: list[TrayRow]) -> tuple[IconBar, ...]:
    """Return one bar per row, for the first ``MAX_ICON_BARS`` rows. Without rows, one grey bar
    keeps the icon visible."""
    if not rows:
        return (IconBar(NO_DATA_RGB, _MIN_BAR_HEIGHT),)
    return tuple(icon_bar(row) for row in rows[:MAX_ICON_BARS])


def tray_status(snapshot: EngineSnapshot) -> TrayStatus:
    rows = tray_rows(snapshot)
    return TrayStatus(
        label=tray_label(rows),
        tooltip_title=TOOLTIP_TITLE,
        tooltip="\n".join(row.tooltip for row in rows),
        bars=icon_bars(rows),
    )


# Shown until the first snapshot arrives.
INITIAL_STATUS = TrayStatus(
    label=NO_DATA_LABEL,
    tooltip_title=TOOLTIP_TITLE,
    tooltip="",
    bars=(IconBar(NO_DATA_RGB, _MIN_BAR_HEIGHT),),
)
