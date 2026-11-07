"""Rich styles for the latency colours that the core decides."""

from hydraping.core.graphcells import LatencyColor, latency_color

RICH_COLORS = {
    LatencyColor.GREEN: "green",
    LatencyColor.YELLOW: "yellow",
    LatencyColor.ORANGE: "orange1",
    LatencyColor.RED: "red",
}


def get_latency_color(latency_ms: float) -> str:
    """Return the Rich colour name for a latency."""
    return RICH_COLORS[latency_color(latency_ms)]
