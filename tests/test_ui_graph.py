"""Tests for graph rendering."""

import threading
import time
from io import StringIO

from rich.cells import cell_len
from rich.console import Console

from hydraping.core.config import Config
from hydraping.core.engine import Engine
from hydraping.core.models import CheckResult, CheckType, Endpoint
from hydraping.tui.dashboard import Dashboard
from hydraping.tui.graph import LatencyGraph


def icmp_ok(latency_ms: float) -> CheckResult:
    now = time.time()
    return CheckResult(
        check_type=CheckType.ICMP,
        success=True,
        latency_ms=latency_ms,
        started_at=now - 0.01,
        finished_at=now,
    )


def icmp_failed() -> CheckResult:
    now = time.time()
    return CheckResult(
        check_type=CheckType.ICMP,
        success=False,
        error_message="Timeout",
        started_at=now - 0.01,
        finished_at=now,
    )


class TestLatencyGraph:
    """Test latency graph rendering."""

    def test_empty_graph_renders_dots(self):
        """Test that empty graph renders with dots."""
        graph = LatencyGraph()
        bucketed = [None] * 10

        result = graph.render(bucketed)
        # Should be 10 dim dots
        assert len(result.plain) == 10
        assert "·" in result.plain

    def test_successful_result_renders_bar(self):
        """Test that successful check renders colored bar."""
        graph = LatencyGraph()

        # Create a successful result
        result = icmp_ok(25.0)

        bucketed = [None, None, result, None, None]
        rendered = graph.render(bucketed)

        # Should have some block character (not just dots)
        assert len(rendered.plain) == 5
        # Green latency should use a block character
        assert any(c in rendered.plain for c in "▁▂▃▄▅▆▇█")

    def test_failed_result_renders_exclamation(self):
        """Test that failed check renders exclamation mark."""
        graph = LatencyGraph()

        result = icmp_failed()

        bucketed = [None, None, result, None, None]
        rendered = graph.render(bucketed)

        assert "!" in rendered.plain

    def test_gap_detection_small_gap_shows_errors(self):
        """Test that small gaps show as exclamation marks."""
        graph = LatencyGraph()

        result1 = icmp_ok(10.0)

        result2 = icmp_ok(10.0)

        # Small gap of 3 buckets between results (should show as errors)
        bucketed = [result1, None, None, None, result2, None, None, None, None, None]
        rendered = graph.render(bucketed)

        # Gap should be marked with exclamation marks
        assert "!" in rendered.plain

    def test_gap_detection_large_gap_shows_dots(self):
        """Test that large gaps show as dots to avoid noise."""
        graph = LatencyGraph()

        result1 = icmp_ok(10.0)

        result2 = icmp_ok(10.0)

        # Large gap of 20 buckets (should show as dots, not errors)
        bucketed = [result1] + [None] * 20 + [result2] + [None] * 28
        rendered = graph.render(bucketed)

        # Large gap should be dots, not exclamation marks
        assert "·" in rendered.plain


GRAPH_CHARS = set(LatencyGraph.BLOCKS) | {"!"}


def _dashboard() -> Dashboard:
    config = Config(endpoints=[Endpoint.parse("192.0.2.1"), Endpoint.parse("example.com")])
    engine = Engine(config)
    engine.store.apply(0, [icmp_ok(12.0)])
    return Dashboard(engine)


def _render_lines(dashboard: Dashboard, width: int) -> list[str]:
    dashboard.console = Console(width=width, file=StringIO(), color_system=None)
    dashboard.console.print(dashboard.render())
    return dashboard.console.file.getvalue().splitlines()


class TestDashboardWidth:
    """Test that the dashboard follows the console width on every frame."""

    def test_same_dashboard_follows_resize(self):
        dashboard = _dashboard()

        narrow = _render_lines(dashboard, 80)
        wide = _render_lines(dashboard, 140)

        assert all(cell_len(line) <= 80 for line in narrow)
        assert all(cell_len(line) <= 140 for line in wide)

        def graph_width(lines: list[str]) -> int:
            row = next(line for line in lines if line.lstrip().startswith("192.0.2.1"))
            return sum(1 for char in row if char in GRAPH_CHARS)

        assert graph_width(wide) - graph_width(narrow) == 60

    def test_long_name_shrinks_before_the_graph(self):
        endpoint = Endpoint.parse("192.0.2.1")
        endpoint.custom_name = "jumpbox.example.internal-ssh22"  # 30 characters
        engine = Engine(Config(endpoints=[endpoint]))
        engine.store.apply(0, [icmp_ok(12.0)])

        lines = _render_lines(Dashboard(engine), 65)

        [row] = [line for line in lines if "12.0ms" in line]
        assert cell_len(row) <= 65
        assert "jumpbox" in row and "…" in row
        graph = "".join(char for char in row if char in GRAPH_CHARS)
        # 65 columns minus 26 fixed and the 19 left for the shrunk name.
        assert len(graph) == 20
        assert graph[-1] in LatencyGraph.BLOCKS[1:], "the newest bucket is the rightmost cell"

    def test_narrow_terminal_shows_message_instead_of_raising(self):
        lines = _render_lines(_dashboard(), 50)

        # The names shrink to the 10-column floor: 10 + 26 fixed + 20 graph = 56.
        assert lines == ["Terminal too narrow: 50 columns, needs 56"]

    def test_narrow_message_fits_on_one_line(self):
        lines = _render_lines(_dashboard(), 30)

        assert len(lines) == 1
        assert cell_len(lines[0]) <= 30

    def test_stopped_run_prints_last_frame(self):
        config = Config(endpoints=[Endpoint.parse("127.0.0.1")])
        console = Console(width=80, file=StringIO(), force_terminal=True, color_system=None)
        dashboard = Dashboard(Engine(config), console=console)

        thread = threading.Thread(target=dashboard.run)
        thread.start()
        time.sleep(0.3)
        dashboard.stop()
        thread.join(5.0)
        assert not thread.is_alive()

        output = console.file.getvalue()
        leave_alternate_screen = "\x1b[?1049l"
        assert "\x1b[?1049h" in output
        assert "127.0.0.1" in output.rpartition(leave_alternate_screen)[2]

    def test_failed_current_result_shows_fail(self):
        config = Config(endpoints=[Endpoint.parse("192.0.2.1")])
        engine = Engine(config)
        engine.store.apply(0, [icmp_failed()])

        lines = _render_lines(Dashboard(engine), 80)

        [row] = [line for line in lines if "FAIL" in line]
        assert row.lstrip().startswith("192.0.2.1") and "(ICMP)" in row
        assert row.rstrip().endswith("(ICMP)")
        assert any("192.0.2.1: ICMP: Timeout" in line for line in lines)
