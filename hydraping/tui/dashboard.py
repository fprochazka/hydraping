"""Main terminal UI dashboard for HydraPing."""

import threading
from dataclasses import dataclass

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from hydraping.core.config import Chain
from hydraping.core.display import (
    ChainState,
    chain_status,
    interface_label,
    latency_text,
    problem_lines,
    route_label,
)
from hydraping.core.engine import Engine
from hydraping.core.models import Endpoint
from hydraping.core.snapshot import ChainSnapshot, EndpointSnapshot
from hydraping.tui.constants import RICH_COLORS
from hydraping.tui.graph import LatencyGraph

# Fixed columns: "999.9ms" fits in 8 characters, "(TCP:65535)" and "(TS direct)" in 11.
LATENCY_TIME_WIDTH = 8
PROTOCOL_WIDTH = 11
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
    # 0 when no row has a known interface, or when the terminal has no room for the column.
    interface: int = 0


# Hops are indented under their chain's header row.
HOP_INDENT = "  "


def row_names(endpoints: list[Endpoint], chains: list[Chain]) -> list[str]:
    """Return the text of every name cell: targets, chain headers and indented hops."""
    names = [endpoint.display_name for endpoint in endpoints]
    for chain in chains:
        names.append(chain.name)
        names.extend(HOP_INDENT + hop.display_name for hop in chain.hops)
    return names


def _endpoint_width_bounds(names: list[str]) -> tuple[int, int]:
    """Return the (smallest, preferred) endpoint column width."""
    longest_name = max((len(name) for name in names), default=0)
    preferred = min(longest_name + 2, MAX_ENDPOINT_WIDTH)
    return min(preferred, MIN_ENDPOINT_WIDTH), preferred


def minimum_terminal_width(names: list[str]) -> int:
    """Return the narrowest terminal that fits every column at its smallest width."""
    smallest_endpoint, _ = _endpoint_width_bounds(names)
    return smallest_endpoint + FIXED_WIDTH + MIN_GRAPH_WIDTH


def calculate_column_widths(
    names: list[str],
    terminal_width: int,
    ui_graph_width: int = 0,
    interface_width: int = 0,
) -> ColumnWidths | None:
    """Return the column widths for one frame, or None if the terminal is too narrow.

    The columns always add up to at most ``terminal_width``, so Rich never has to shrink the
    graph. A configured ``ui_graph_width`` is capped at the space the terminal has. The interface
    column, ``interface_width`` wide, is optional: it is dropped when it would shrink the graph
    below ``MIN_GRAPH_WIDTH``.
    """
    if terminal_width < minimum_terminal_width(names):
        return None

    smallest_endpoint, preferred_endpoint = _endpoint_width_bounds(names)
    room_for_endpoint = terminal_width - FIXED_WIDTH - MIN_GRAPH_WIDTH
    endpoint_width = max(smallest_endpoint, min(preferred_endpoint, room_for_endpoint))

    available = terminal_width - FIXED_WIDTH - endpoint_width
    # The interface column brings its own padding of one space on each side.
    interface_column = interface_width + 2 if interface_width > 0 else 0
    if available - interface_column >= MIN_GRAPH_WIDTH:
        available -= interface_column
    else:
        interface_width = 0
    graph_width = min(ui_graph_width, available) if ui_graph_width > 0 else available

    return ColumnWidths(endpoint=endpoint_width, graph=graph_width, interface=interface_width)


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
        names = row_names(self.engine.endpoints, self.engine.config.chains)
        route_names = [*self.engine.interfaces().values(), *self.engine.relays().values()]
        interface_width = max((len(interface_label(name)) for name in route_names), default=0)
        widths = calculate_column_widths(
            names,
            terminal_width,
            self.engine.config.ui.graph_width,
            interface_width,
        )
        if widths is None:
            message = Text(
                f"Terminal too narrow: {terminal_width} columns, needs "
                f"{minimum_terminal_width(names)}",
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
        if widths.interface:
            table.add_column("Interface", width=widths.interface, no_wrap=True)

        for row in snapshot.rows:
            self._add_endpoint_row(table, row, widths)
        for index, chain in enumerate(snapshot.chains):
            if snapshot.rows or index > 0:
                table.add_row()  # An empty line before each chain.
            self._add_chain_rows(table, chain, widths)

        problems = problem_lines(snapshot)
        renderables: list[RenderableType] = [table]
        if problems:
            renderables.append(Text())  # Empty line
            renderables.append(Text("Problems:", style="bold red"))
            for problem in problems:
                renderables.append(Text(f"  • {problem}", style="red"))

        return Group(*renderables)

    def _add_chain_rows(self, table: Table, chain: ChainSnapshot, widths: ColumnWidths):
        chain_state = chain_status(chain)
        status_style = {ChainState.BROKEN: "red", ChainState.OK: "green"}.get(chain_state.state)
        status = Text(chain_state.text, style=status_style or "")
        status.truncate(widths.graph, overflow="ellipsis")
        table.add_row(Text(chain.name, style="bold underline"), status)
        for hop in chain.hops:
            self._add_endpoint_row(table, hop, widths, indent=HOP_INDENT)

    def _add_endpoint_row(
        self, table: Table, row: EndpointSnapshot, widths: ColumnWidths, indent: str = ""
    ):
        latency = latency_text(row.current)
        latency_style = RICH_COLORS[latency.color] if latency.color is not None else "dim"

        primary_layer = row.primary_layer()
        results = primary_layer.results() if primary_layer is not None else [None] * widths.graph
        cells = [
            Text(indent + row.endpoint.display_name, style="bold"),
            self.graph.render(results),
            Text(latency.text, style=latency_style),
            Text(latency.label, style="dim"),
        ]
        if widths.interface:
            # An interface that appeared after the widths were computed is cut by Rich, which is
            # harmless for one frame.
            cells.append(Text(route_label(row), style="dim"))
        table.add_row(*cells)

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
