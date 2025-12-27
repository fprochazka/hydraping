"""Tests for graph rendering."""

from datetime import datetime

from hydraping.models import CheckResult, CheckType
from hydraping.ui.graph import LatencyGraph


class TestLatencyGraph:
    """Test latency graph rendering."""

    def test_empty_graph_renders_dots(self):
        """Test that empty graph renders with dots."""
        graph = LatencyGraph(width=10)
        bucketed = [None] * 10

        result = graph.render(bucketed)
        # Should be 10 dim dots
        assert len(result.plain) == 10
        assert "·" in result.plain

    def test_successful_result_renders_bar(self):
        """Test that successful check renders colored bar."""
        graph = LatencyGraph(width=5)

        # Create a successful result
        result = CheckResult(
            timestamp=datetime.now(),
            check_type=CheckType.ICMP,
            success=True,
            latency_ms=25.0,
        )

        bucketed = [None, None, result, None, None]
        rendered = graph.render(bucketed)

        # Should have some block character (not just dots)
        assert len(rendered.plain) == 5
        # Green latency should use a block character
        assert any(c in rendered.plain for c in "▁▂▃▄▅▆▇█")

    def test_failed_result_renders_exclamation(self):
        """Test that failed check renders exclamation mark."""
        graph = LatencyGraph(width=5)

        result = CheckResult(
            timestamp=datetime.now(),
            check_type=CheckType.ICMP,
            success=False,
            error_message="Timeout",
        )

        bucketed = [None, None, result, None, None]
        rendered = graph.render(bucketed)

        assert "!" in rendered.plain

    def test_gap_detection_small_gap_shows_errors(self):
        """Test that small gaps show as exclamation marks."""
        graph = LatencyGraph(width=10)

        result1 = CheckResult(
            timestamp=datetime.now(),
            check_type=CheckType.ICMP,
            success=True,
            latency_ms=10.0,
        )

        result2 = CheckResult(
            timestamp=datetime.now(),
            check_type=CheckType.ICMP,
            success=True,
            latency_ms=10.0,
        )

        # Small gap of 3 buckets between results (should show as errors)
        bucketed = [result1, None, None, None, result2, None, None, None, None, None]
        rendered = graph.render(bucketed)

        # Gap should be marked with exclamation marks
        assert "!" in rendered.plain

    def test_gap_detection_large_gap_shows_dots(self):
        """Test that large gaps show as dots to avoid noise."""
        graph = LatencyGraph(width=50)

        result1 = CheckResult(
            timestamp=datetime.now(),
            check_type=CheckType.ICMP,
            success=True,
            latency_ms=10.0,
        )

        result2 = CheckResult(
            timestamp=datetime.now(),
            check_type=CheckType.ICMP,
            success=True,
            latency_ms=10.0,
        )

        # Large gap of 20 buckets (should show as dots, not errors)
        bucketed = [result1] + [None] * 20 + [result2] + [None] * 28
        rendered = graph.render(bucketed)

        # Large gap should be dots, not exclamation marks
        assert "·" in rendered.plain
