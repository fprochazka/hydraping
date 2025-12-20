"""Latency graph rendering for terminal UI."""

from hydraping.models import CheckResult


class LatencyGraph:
    """Renders latency history as a graph."""

    # Unicode block characters for graph bars (from empty to full)
    BLOCKS = ["░", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

    # Latency thresholds for height calculation (in ms)
    MAX_LATENCY_FOR_GRAPH = 500.0  # Anything above this is max height

    def __init__(self, width: int):
        """Initialize graph with fixed width."""
        self.width = width

    def render(self, results: list[CheckResult]) -> tuple[str, str]:
        """
        Render graph from check results.

        Returns:
            Tuple of (graph_string, color_name) where:
            - graph_string is the rendered graph (exactly self.width characters)
            - color_name is the Rich color for the graph
        """
        if not results:
            # Empty graph - all dots
            return "░" * self.width, "dim"

        # Take only the most recent results that fit in the width
        recent_results = results[-self.width :]

        # Build graph bars from left to right (oldest to newest)
        bars = []
        overall_color = "green"

        for result in recent_results:
            if result.success and result.latency_ms is not None:
                # Calculate bar height and color based on latency
                bar, color = self._get_bar_for_latency(result.latency_ms)
                bars.append(bar)

                # Update overall color (worst color wins)
                overall_color = self._worst_color(overall_color, color)
            else:
                # Failed check - use dim dot
                bars.append("░")
                overall_color = "dim"

        # Pad with dots on the left to reach fixed width
        graph_str = "░" * (self.width - len(bars)) + "".join(bars)

        return graph_str, overall_color

    def _get_bar_for_latency(self, latency_ms: float) -> tuple[str, str]:
        """
        Get bar character and color for a given latency.

        Returns:
            Tuple of (bar_character, color_name)
        """
        # Determine color based on latency
        if latency_ms < 50:
            color = "green"
        elif latency_ms < 150:
            color = "yellow"
        elif latency_ms < 300:
            color = "bright_yellow"  # orange-ish
        else:
            color = "red"

        # Determine bar height (0-8 scale)
        # Map latency to block character index
        ratio = min(latency_ms / self.MAX_LATENCY_FOR_GRAPH, 1.0)
        block_index = int(ratio * (len(self.BLOCKS) - 1))

        # Use empty block for very low latency
        if block_index == 0:
            block_index = 1

        bar = self.BLOCKS[block_index]

        return bar, color

    @staticmethod
    def _worst_color(color1: str, color2: str) -> str:
        """Return the 'worse' of two colors (for overall graph color)."""
        # Order from best to worst
        color_priority = ["green", "yellow", "bright_yellow", "red", "dim"]

        idx1 = color_priority.index(color1) if color1 in color_priority else 0
        idx2 = color_priority.index(color2) if color2 in color_priority else 0

        # Return the one with higher index (worse)
        return color_priority[max(idx1, idx2)]
