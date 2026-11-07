"""What each graph column shows, decided once for every frontend.

The terminal UI and the desktop UI draw the same buckets. This module decides per bucket whether
it is a latency bar (with its height and colour), a failure (with the letter of the layer that
broke), a gap, or empty, so both frontends only map cells to their own characters or pixels.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from hydraping.core.models import CheckResult, CheckType

# Latency thresholds in milliseconds. Below LATENCY_GREEN_MAX is green, below LATENCY_YELLOW_MAX
# yellow, below LATENCY_ORANGE_MAX orange, and anything slower red.
LATENCY_GREEN_MAX = 50.0
LATENCY_YELLOW_MAX = 100.0
LATENCY_ORANGE_MAX = 200.0
# The red zone has bars up to this latency; anything slower is a full bar.
LATENCY_RED_BAR_MAX = 500.0

# Each zone's bar heights are computed as a ratio of the zone's width, so equal thresholds would
# divide by zero.
if not (0 < LATENCY_GREEN_MAX < LATENCY_YELLOW_MAX < LATENCY_ORANGE_MAX < LATENCY_RED_BAR_MAX):
    raise ValueError(
        "Latency thresholds must be strictly increasing: "
        f"0 < {LATENCY_GREEN_MAX} < {LATENCY_YELLOW_MAX} < {LATENCY_ORANGE_MAX} "
        f"< {LATENCY_RED_BAR_MAX}"
    )

# Bars have 8 heights per colour zone, level 0 (lowest) to BAR_LEVELS - 1.
BAR_LEVELS = 8

# Up to this many missing buckets between results show as a gap ("!"). A longer run, for example
# after a suspend, shows as empty, so the graph doesn't fill with exclamation marks.
MAX_ERROR_GAP_SIZE = 10

# A failed bucket shows the letter of the layer that broke. ICMP, UDP and Tailscale checks have no
# layers before them, so their failures keep "!".
FAILURE_LETTERS = {
    CheckType.DNS: "D",
    CheckType.TCP: "T",
    CheckType.TLS: "S",
    CheckType.HTTP: "H",
}
GAP_LETTER = "!"


class LatencyColor(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


class CellKind(str, Enum):
    # No result: before the first result, after the last one, or in a long gap.
    EMPTY = "empty"
    # A short run of missing buckets between results.
    GAP = "gap"
    # A successful result with a latency.
    BAR = "bar"
    # A failed or skipped result.
    FAILURE = "failure"


@dataclass(frozen=True)
class GraphCell:
    """One graph column. ``level`` is set for a bar, ``letter`` for a gap or a failure, and
    ``color`` for everything but an empty cell."""

    kind: CellKind
    color: LatencyColor | None = None
    level: int | None = None
    letter: str | None = None


EMPTY_CELL = GraphCell(CellKind.EMPTY)
GAP_CELL = GraphCell(CellKind.GAP, color=LatencyColor.RED, letter=GAP_LETTER)


def latency_color(latency_ms: float) -> LatencyColor:
    """Return the colour zone of a latency."""
    if latency_ms < LATENCY_GREEN_MAX:
        return LatencyColor.GREEN
    if latency_ms < LATENCY_YELLOW_MAX:
        return LatencyColor.YELLOW
    if latency_ms < LATENCY_ORANGE_MAX:
        return LatencyColor.ORANGE
    return LatencyColor.RED


def bar_level(latency_ms: float) -> int:
    """Return the bar height, 0 to ``BAR_LEVELS - 1``, of a latency within its colour zone.

    Each colour zone starts again from the lowest bar, so the colour tells the zone and the height
    tells where in the zone the latency is.
    """
    if latency_ms < LATENCY_GREEN_MAX:
        ratio = latency_ms / LATENCY_GREEN_MAX
    elif latency_ms < LATENCY_YELLOW_MAX:
        ratio = (latency_ms - LATENCY_GREEN_MAX) / (LATENCY_YELLOW_MAX - LATENCY_GREEN_MAX)
    elif latency_ms < LATENCY_ORANGE_MAX:
        ratio = (latency_ms - LATENCY_YELLOW_MAX) / (LATENCY_ORANGE_MAX - LATENCY_YELLOW_MAX)
    else:
        ratio = min(
            (latency_ms - LATENCY_ORANGE_MAX) / (LATENCY_RED_BAR_MAX - LATENCY_ORANGE_MAX), 1.0
        )
    return max(0, min(BAR_LEVELS - 1, int(ratio * (BAR_LEVELS - 1))))


def failure_char(result: CheckResult) -> str:
    """Return the graph character for a failed result: the letter of the layer that failed."""
    failed_layer = result.failed_layer or result.check_type
    return FAILURE_LETTERS.get(failed_layer, GAP_LETTER)


def classify_result(result: CheckResult) -> GraphCell:
    """Return the cell of a bucket that has a result."""
    if result.success and result.latency_ms is not None:
        return GraphCell(
            CellKind.BAR,
            color=latency_color(result.latency_ms),
            level=bar_level(result.latency_ms),
        )
    return GraphCell(CellKind.FAILURE, color=LatencyColor.RED, letter=failure_char(result))


def classify_cells(results: Sequence[CheckResult | None]) -> list[GraphCell]:
    """Return one cell per bucket, oldest first, for the last result of each bucket or None.

    A missing bucket is a gap only between two results and only in a run of at most
    ``MAX_ERROR_GAP_SIZE``; otherwise it is empty.
    """
    present = [i for i, result in enumerate(results) if result is not None]
    if not present:
        return [EMPTY_CELL] * len(results)
    first, last = present[0], present[-1]

    cells: list[GraphCell] = []
    gap_start: int | None = None
    for i, result in enumerate(results):
        if result is not None:
            if gap_start is not None:
                gap = GAP_CELL if i - gap_start <= MAX_ERROR_GAP_SIZE else EMPTY_CELL
                cells.extend([gap] * (i - gap_start))
                gap_start = None
            cells.append(classify_result(result))
        elif first < i < last:
            if gap_start is None:
                gap_start = i
        else:
            cells.append(EMPTY_CELL)
    return cells
