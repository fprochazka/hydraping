"""Main terminal UI dashboard for HydraPing."""

import threading
from dataclasses import dataclass

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from hydraping.core.engine import Engine
from hydraping.core.models import CheckType, Endpoint
from hydraping.core.snapshot import EndpointSnapshot, EngineSnapshot
from hydraping.tui.constants import get_latency_color
from hydraping.tui.graph import LatencyGraph

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

    The dashboard reads engine snapshots from the main thread and draws them at ``FRAME_SECONDS``.
    It keeps no width between frames. Every ``render()`` reads the console size, so the layout
    follows terminal resizes.
    """

    FRAME_SECONDS = 0.25

    def __init__(self, engine: Engine, console: Console | None = None):
        self.engine = engine
        self.console = console if console is not None else Console()
        self.graph = LatencyGraph()
        self._stop = threading.Event()

    def render(self) -> RenderableType:
        """Render the current state at the console's current width."""
        terminal_width = self.console.size.width
        widths = calculate_column_widths(
            self.engine.endpoints, terminal_width, self.engine.config.ui.graph_width
        )
        if widths is None:
            message = Text(
                f"Terminal too narrow: {terminal_width} columns, needs "
                f"{minimum_terminal_width(self.engine.endpoints)}",
                style="yellow",
            )
            # Cut the message to one line, so a very narrow terminal still gets a single row.
            message.truncate(terminal_width, overflow="ellipsis")
            return message

        snapshot = self.engine.snapshot(columns=widths.graph)

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Endpoint", width=widths.endpoint, no_wrap=True)
        table.add_column("Graph", width=widths.graph, no_wrap=True)
        table.add_column("Time", width=LATENCY_TIME_WIDTH, justify="right", no_wrap=True)
        table.add_column("Protocol", width=PROTOCOL_WIDTH, justify="left", no_wrap=True)

        for row in snapshot.rows:
            self._add_endpoint_row(table, row, widths.graph)

        problems = self._problems(snapshot)
        renderables: list[RenderableType] = [table]
        if problems:
            renderables.append(Text())  # Empty line
            renderables.append(Text("Problems:", style="bold red"))
            for problem in problems:
                renderables.append(Text(f"  • {problem}", style="red"))

        return Group(*renderables)

    def _add_endpoint_row(self, table: Table, row: EndpointSnapshot, graph_width: int):
        current = row.current
        if current is not None and current.success and current.latency_ms is not None:
            check_label = current.check_type.value.upper()
            if current.check_type == CheckType.TCP and current.port:
                check_label = f"TCP:{current.port}"
            elif current.check_type == CheckType.HTTP and current.protocol:
                check_label = current.protocol.upper()
            time_str = f"{current.latency_ms:.1f}ms"
            protocol_str = f"({check_label})"
            latency_style = get_latency_color(current.latency_ms)
        elif current is not None:
            time_str = "FAIL"
            protocol_str = f"({current.check_type.value.upper()})"
            latency_style = "red"
        else:
            time_str = "N/A"
            protocol_str = ""
            latency_style = "dim"

        primary_layer = row.primary_layer()
        results = primary_layer.results() if primary_layer is not None else [None] * graph_width
        table.add_row(
            Text(row.endpoint.display_name, style="bold"),
            self.graph.render(results),
            Text(time_str, style=latency_style),
            Text(protocol_str, style="dim"),
        )

    @staticmethod
    def _problems(snapshot: EngineSnapshot) -> list[str]:
        problems = []
        if not snapshot.icmp_available:
            problems.append("ICMP unavailable (no permissions) - ping checks disabled")
        for row in snapshot.rows:
            for problem in row.problems:
                problems.append(f"{row.endpoint.display_name}: {problem}")
        return problems

    def stop(self) -> None:
        """Make ``run()`` return after its current frame. Safe to call from another thread."""
        self._stop.set()

    def run(self) -> None:
        """Run the live dashboard on the alternate screen until ``stop()`` or Ctrl+C.

        The alternate screen gets cleared on every frame, so a resize leaves no stale lines. On
        exit, including Ctrl+C, the last frame goes to normal output and stays in scrollback.
        """
        live = Live(self.render(), console=self.console, auto_refresh=False, screen=True)
        live.start()
        try:
            self.engine.start()
            while not self._stop.wait(self.FRAME_SECONDS):
                live.update(self.render(), refresh=True)
        finally:
            try:
                self.engine.stop()
            finally:
                live.stop()
                self.console.print(self.render())
