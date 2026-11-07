"""Main terminal UI dashboard for HydraPing."""

import asyncio
import time
from dataclasses import dataclass

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from hydraping.models import CheckType, Endpoint
from hydraping.orchestrator import CheckOrchestrator
from hydraping.ui.constants import get_latency_color
from hydraping.ui.graph import LatencyGraph

# Fixed columns: "999.9ms" fits in 8 characters, "(TCP:443)" in 10.
LATENCY_TIME_WIDTH = 8
PROTOCOL_WIDTH = 10
# The endpoint column is the first to shrink in a narrow terminal. Names cut with "…" stay
# readable down to MIN_ENDPOINT_WIDTH. A graph cut by Rich would hide the newest buckets instead.
MAX_ENDPOINT_WIDTH = 30
MIN_ENDPOINT_WIDTH = 10
MIN_GRAPH_WIDTH = 20

# padding=(0, 1) puts one space on each side of each of the 4 columns.
TABLE_PADDING = 4 * 2
FIXED_WIDTH = LATENCY_TIME_WIDTH + PROTOCOL_WIDTH + TABLE_PADDING


@dataclass(frozen=True)
class ColumnWidths:
    endpoint: int
    graph: int


def _endpoint_width_bounds(endpoints: list[Endpoint]) -> tuple[int, int]:
    """Return the (smallest, preferred) endpoint column width."""
    longest_name = max((len(ep.display_name) for ep in endpoints), default=0)
    preferred = min(longest_name + 2, MAX_ENDPOINT_WIDTH)
    return min(preferred, MIN_ENDPOINT_WIDTH), preferred


def minimum_terminal_width(endpoints: list[Endpoint]) -> int:
    """Return the narrowest terminal that fits every column at its smallest width."""
    smallest_endpoint, _ = _endpoint_width_bounds(endpoints)
    return smallest_endpoint + FIXED_WIDTH + MIN_GRAPH_WIDTH


def calculate_column_widths(
    endpoints: list[Endpoint], terminal_width: int, ui_graph_width: int = 0
) -> ColumnWidths | None:
    """Return the column widths for one frame, or None if the terminal is too narrow.

    The columns always add up to at most ``terminal_width``, so Rich never has to shrink the
    graph. A configured ``ui_graph_width`` is capped at the space the terminal has.
    """
    if terminal_width < minimum_terminal_width(endpoints):
        return None

    smallest_endpoint, preferred_endpoint = _endpoint_width_bounds(endpoints)
    room_for_endpoint = terminal_width - FIXED_WIDTH - MIN_GRAPH_WIDTH
    endpoint_width = max(smallest_endpoint, min(preferred_endpoint, room_for_endpoint))

    available = terminal_width - FIXED_WIDTH - endpoint_width
    graph_width = min(ui_graph_width, available) if ui_graph_width > 0 else available

    return ColumnWidths(endpoint=endpoint_width, graph=graph_width)


class Dashboard:
    """Live-updating terminal dashboard.

    The dashboard keeps no width between frames. Every ``render()`` reads the console size, so the
    layout follows terminal resizes.
    """

    # How often the loop checks the terminal size. A resize redraws at once.
    RESIZE_POLL_SECONDS = 0.25
    # How often the data redraws when the size stays the same. Each frame scans the whole history,
    # which holds up to HISTORY_CAPACITY results per endpoint, so this stays at once per second.
    REFRESH_SECONDS = 1.0

    def __init__(self, orchestrator: CheckOrchestrator, console: Console | None = None):
        """Initialize dashboard with orchestrator.

        Args:
            orchestrator: The check orchestrator managing endpoint checks
            console: Optional Rich console (creates new one if not provided)
        """
        self.orchestrator = orchestrator
        self.console = console if console is not None else Console()
        self.graph = LatencyGraph()

    def render(self) -> RenderableType:
        """Render the current state at the console's current width."""
        terminal_width = self.console.size.width
        widths = calculate_column_widths(
            self.orchestrator.endpoints,
            terminal_width,
            self.orchestrator.config.ui.graph_width,
        )
        if widths is None:
            message = Text(
                f"Terminal too narrow: {terminal_width} columns, needs "
                f"{minimum_terminal_width(self.orchestrator.endpoints)}",
                style="yellow",
            )
            # Cut the message to one line, so a very narrow terminal still gets a single row.
            message.truncate(terminal_width, overflow="ellipsis")
            return message

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Endpoint", width=widths.endpoint, no_wrap=True)
        table.add_column("Graph", width=widths.graph, no_wrap=True)
        table.add_column("Time", width=LATENCY_TIME_WIDTH, justify="right", no_wrap=True)
        table.add_column("Protocol", width=PROTOCOL_WIDTH, justify="left", no_wrap=True)

        for endpoint in self.orchestrator.endpoints:
            self._add_endpoint_row(table, endpoint, widths.graph)

        # Build problems section
        problems_text = self._render_problems()
        renderables = [table]

        if problems_text:
            renderables.append(Text())  # Empty line
            renderables.append(Text("Problems:", style="bold red"))
            for problem in problems_text:
                renderables.append(Text(f"  • {problem}", style="red"))

        return Group(*renderables)

    def _add_endpoint_row(self, table: Table, endpoint: Endpoint, graph_width: int):
        """Add a row for an endpoint to the table."""
        # Get result history for this endpoint
        history = self.orchestrator.get_history(endpoint)

        if history is None:
            # No data yet
            time_str = "N/A"
            protocol_str = ""
            latency_style = "dim"
            graph_text = self.graph.render([None] * graph_width)
        else:
            # Get the current result (what should be displayed now)
            # This is synchronized with what the graph shows
            latency_result = history.get_current_result()

            # Format latency time and protocol separately
            if latency_result and latency_result.success and latency_result.latency_ms is not None:
                # Build check label with port/protocol info
                check_label = latency_result.check_type.value.upper()
                if latency_result.check_type == CheckType.TCP and latency_result.port:
                    check_label = f"TCP:{latency_result.port}"
                elif latency_result.check_type == CheckType.HTTP and latency_result.protocol:
                    check_label = latency_result.protocol.upper()

                time_str = f"{latency_result.latency_ms:.1f}ms"
                protocol_str = f"({check_label})"
                latency_style = get_latency_color(latency_result.latency_ms)
            elif latency_result and not latency_result.success:
                check_label = latency_result.check_type.value.upper()
                time_str = "FAIL"
                protocol_str = f"({check_label})"
                latency_style = "red"
            else:
                time_str = "N/A"
                protocol_str = ""
                latency_style = "dim"

            # Get bucketed results and render the graph
            # History prepares the ready-to-render list, graph just renders it
            bucketed_results = history.get_bucketed_results(graph_width)
            graph_text = self.graph.render(bucketed_results)

        # Add row
        table.add_row(
            Text(endpoint.display_name, style="bold"),
            graph_text,
            Text(time_str, style=latency_style),
            Text(protocol_str, style="dim"),
        )

    def _render_problems(self) -> list[str]:
        """Get list of current problems across all endpoints."""
        problems = []
        icmp_unavailable_shown = False

        # Check if ICMP is globally unavailable
        if not self.orchestrator.icmp_checker.is_available() and not icmp_unavailable_shown:
            problems.append("ICMP unavailable (no permissions) - ping checks disabled")
            icmp_unavailable_shown = True

        # Add endpoint-specific problems
        for endpoint in self.orchestrator.endpoints:
            endpoint_problems = self.orchestrator.get_problems(endpoint)
            for problem in endpoint_problems:
                problems.append(f"{endpoint.display_name}: {problem}")

        return problems

    async def run(self):
        """Run the live dashboard on the alternate screen until cancelled.

        The alternate screen gets cleared on every frame, so a resize leaves no stale lines. On
        exit, including Ctrl+C, the last frame goes to normal output and stays in scrollback.
        """
        # The loop redraws in the event loop thread rather than through Live's refresh thread,
        # because check results mutate the history in this thread.
        live = Live(self.render(), console=self.console, auto_refresh=False, screen=True)
        live.start()
        try:
            await self.orchestrator.start()
            last_size = self.console.size
            last_refresh = time.monotonic()
            while True:
                await asyncio.sleep(self.RESIZE_POLL_SECONDS)
                size = self.console.size
                now = time.monotonic()
                if size != last_size or now - last_refresh >= self.REFRESH_SECONDS:
                    live.update(self.render(), refresh=True)
                    last_size = size
                    last_refresh = now
        finally:
            try:
                await self.orchestrator.stop()
            finally:
                live.stop()
                self.console.print(self.render())
