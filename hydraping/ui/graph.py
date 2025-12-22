"""Latency graph rendering for terminal UI."""

import time

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

    def render(
        self, results: list[CheckResult], start_time: float | None, interval_seconds: float
    ) -> Text:
        """
        Render graph from check results with time-bucket awareness.

        Returns:
            Text object with properly styled graph
        """
        if start_time is None:
            # Not started yet - all dots
            return Text(self.EMPTY_CHAR * self.width, style="dim")

        # Calculate current time bucket
        now = time.monotonic()
        elapsed = now - start_time
        current_bucket = int(elapsed / interval_seconds)

        # Calculate which buckets to display (most recent width buckets)
        start_bucket = max(0, current_bucket - self.width + 1)
        end_bucket = current_bucket + 1

        # Check type priority (HTTP is highest, ICMP is lowest)
        from hydraping.models import CheckType

        priority_order = {
            CheckType.HTTP: 0,
            CheckType.TCP: 1,
            CheckType.DNS: 2,
            CheckType.ICMP: 3,
        }

        # Create a dict of results by bucket number
        # Need to convert result timestamps to bucket numbers relative to start_time
        results_by_bucket = {}

        # Get the Unix timestamp of start_time for conversion
        start_timestamp = time.time() - (now - start_time)

        for result in results:
            timestamp_s = result.timestamp.timestamp()
            # Calculate bucket relative to start_time
            elapsed_since_start = timestamp_s - start_timestamp
            bucket = int(elapsed_since_start / interval_seconds)

            # Keep highest-priority result per bucket (successful or failed)
            if bucket not in results_by_bucket:
                results_by_bucket[bucket] = result
            else:
                current_result = results_by_bucket[bucket]
                # Prefer successful results, then by priority
                current_priority = priority_order.get(current_result.check_type, 999)
                new_priority = priority_order.get(result.check_type, 999)

                # Replace if new is better: success over failure, or same state + higher priority
                should_replace = False
                if result.success and not current_result.success:
                    should_replace = True
                elif result.success == current_result.success and new_priority < current_priority:
                    should_replace = True

                if should_replace:
                    results_by_bucket[bucket] = result

        # Build graph with proper styling per character
        graph_text = Text()

        # Calculate which buckets to display
        buckets_to_show = list(range(start_bucket, end_bucket))

        # Ensure we show exactly self.width buckets
        if len(buckets_to_show) > self.width:
            buckets_to_show = buckets_to_show[-self.width :]

        # Calculate padding needed on the left
        padding_needed = max(0, self.width - len(buckets_to_show))

        # Render padding (empty dots) on the left
        for _ in range(padding_needed):
            graph_text.append(self.EMPTY_CHAR, style="dim")

        # Render each bucket with appropriate styling
        for bucket_num in buckets_to_show:
            result = results_by_bucket.get(bucket_num)

            if result is None:
                # No data for this bucket yet - show dim dot
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
