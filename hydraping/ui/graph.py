"""Latency graph rendering for terminal UI."""

from rich.text import Text

from hydraping.models import CheckResult
from hydraping.ui.constants import (
    LATENCY_GREEN_MAX,
    LATENCY_ORANGE_MAX,
    LATENCY_YELLOW_MAX,
    get_latency_color,
)


class LatencyGraph:
    """Renders latency history as a graph."""

    # Unicode block characters for graph bars (from empty to full)
    BLOCKS = ["·", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    EMPTY_CHAR = "·"  # Character for empty/padding areas

    # Latency thresholds for height calculation (in ms)
    MAX_LATENCY_FOR_GRAPH = 500.0  # Anything above this is max height

    def __init__(self, width: int):
        """Initialize graph with fixed width."""
        self.width = width

    def render(self, bucketed_results: list[CheckResult | None]) -> Text:
        """Render graph from pre-bucketed check results.

        The bucketing, time range calculation, and priority selection are all
        done by EndpointResultHistory. This method is a pure renderer - it just
        iterates over the provided list and renders each position.

        Args:
            bucketed_results: List of CheckResult or None, where each position
                            represents a time bucket from oldest to newest.
                            From EndpointResultHistory.get_bucketed_results().

        Returns:
            Text object with properly styled graph
        """
        # Build graph with proper styling per character
        graph_text = Text()

        # Render each position in the list
        for result in bucketed_results:
            if result is None:
                # No data for this bucket - show dim dot
                graph_text.append(self.EMPTY_CHAR, style="dim")
            elif result.success and result.latency_ms is not None:
                # Calculate bar height and color based on latency
                bar, color = self._get_bar_for_latency(result.latency_ms)
                graph_text.append(bar, style=color)
            else:
                # Failed check - use red exclamation mark
                graph_text.append("!", style="red")

        return graph_text

    def _get_bar_for_latency(self, latency_ms: float) -> tuple[str, str]:
        """
        Get bar character and color for a given latency.

        Uses constants from ui.constants for consistent color thresholds.

        Returns:
            Tuple of (bar_character, color_name)
        """
        # Get color using shared logic
        color = get_latency_color(latency_ms)

        # Determine height ratio within color zone
        if latency_ms < LATENCY_GREEN_MAX:
            # Green zone - good
            ratio = latency_ms / LATENCY_GREEN_MAX
        elif latency_ms < LATENCY_YELLOW_MAX:
            # Yellow zone - medium
            ratio = (latency_ms - LATENCY_GREEN_MAX) / (LATENCY_YELLOW_MAX - LATENCY_GREEN_MAX)
        elif latency_ms < LATENCY_ORANGE_MAX:
            # Orange zone - concerning
            ratio = (latency_ms - LATENCY_YELLOW_MAX) / (LATENCY_ORANGE_MAX - LATENCY_YELLOW_MAX)
        else:
            # Red zone - bad
            ratio = min((latency_ms - LATENCY_ORANGE_MAX) / 300.0, 1.0)

        # Map ratio to block character (8 levels per color zone)
        block_index = int(ratio * 7)  # 0-7
        block_index = max(0, min(7, block_index))  # Clamp to valid range

        bar = self.BLOCKS[block_index + 1]  # Skip first block (·)

        return bar, color

    @staticmethod
    def _worst_color(color1: str, color2: str) -> str:
        """Return the 'worse' of two colors (for overall graph color)."""
        # Order from best to worst
        color_priority = ["green", "yellow", "orange1", "red"]

        idx1 = color_priority.index(color1) if color1 in color_priority else 0
        idx2 = color_priority.index(color2) if color2 in color_priority else 0

        # Return the one with higher index (worse)
        return color_priority[max(idx1, idx2)]
