"""Latency graph rendering for terminal UI."""

from hydraping.models import CheckResult


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

    def render(self, results: list[CheckResult]) -> tuple[str, str, str]:
        """
        Render graph from check results.

        Returns:
            Tuple of (padding, bars, color) where:
            - padding is the left padding (dim dots)
            - bars is the actual data bars
            - color is the Rich color for the bars
        """
        if not results:
            # Empty graph - all dots
            return self.EMPTY_CHAR * self.width, "", "dim"

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
                # Failed check - use exclamation mark
                bars.append("!")
                overall_color = "red"

        # Return padding and bars separately
        padding = self.EMPTY_CHAR * (self.width - len(bars))
        bars_str = "".join(bars)

        return padding, bars_str, overall_color

    def _get_bar_for_latency(self, latency_ms: float) -> tuple[str, str]:
        """
        Get bar character and color for a given latency.

        Uses prettyping-inspired color scheme:
        - Green: <50ms
        - Yellow on green: 50-100ms
        - Red on yellow: 100-200ms
        - Red: >200ms

        Returns:
            Tuple of (bar_character, color_name)
        """
        # Determine color zone and height within that zone
        if latency_ms < 50:
            # Green zone (0-50ms)
            color = "green"
            ratio = latency_ms / 50.0
        elif latency_ms < 100:
            # Yellow on green background (50-100ms)
            color = "yellow on green"
            ratio = (latency_ms - 50) / 50.0
        elif latency_ms < 200:
            # Red on yellow background (100-200ms)
            color = "red on yellow"
            ratio = (latency_ms - 100) / 100.0
        else:
            # Pure red (>200ms)
            color = "red"
            ratio = min((latency_ms - 200) / 300.0, 1.0)

        # Map ratio to block character (8 levels per color zone)
        block_index = int(ratio * 7)  # 0-7
        block_index = max(0, min(7, block_index))  # Clamp to valid range

        bar = self.BLOCKS[block_index + 1]  # Skip first block (░)

        return bar, color

    @staticmethod
    def _worst_color(color1: str, color2: str) -> str:
        """Return the 'worse' of two colors (for overall graph color)."""
        # Order from best to worst (matching prettyping zones)
        color_priority = ["green", "yellow on green", "red on yellow", "red"]

        idx1 = color_priority.index(color1) if color1 in color_priority else 0
        idx2 = color_priority.index(color2) if color2 in color_priority else 0

        # Return the one with higher index (worse)
        return color_priority[max(idx1, idx2)]
