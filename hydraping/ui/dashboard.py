"""Main terminal UI dashboard for HydraPing."""

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from hydraping.models import CheckType, Endpoint
from hydraping.orchestrator import CheckOrchestrator
from hydraping.ui.constants import get_latency_color
from hydraping.ui.graph import LatencyGraph


class Dashboard:
    """Live-updating terminal dashboard."""

    def __init__(self, orchestrator: CheckOrchestrator):
        """Initialize dashboard with orchestrator."""
        self.orchestrator = orchestrator
        self.console = Console()

        # Calculate column widths
        self._calculate_column_widths()

        # Create graph renderers for each endpoint
        self.graphs: dict[str, LatencyGraph] = {}
        for endpoint in orchestrator.endpoints:
            self.graphs[endpoint.raw] = LatencyGraph(width=self.graph_width)

    def _calculate_column_widths(self):
        """Calculate fixed column widths based on terminal size."""
        terminal_width = self.console.width

        # Validate minimum terminal width
        MIN_TERMINAL_WIDTH = 60
        if terminal_width < MIN_TERMINAL_WIDTH:
            raise ValueError(
                f"Terminal too narrow ({terminal_width} chars). "
                f"Minimum width required: {MIN_TERMINAL_WIDTH} chars"
            )

        # Endpoint column: longest endpoint name + padding
        endpoint_names = [ep.display_name for ep in self.orchestrator.endpoints]
        self.endpoint_width = max(len(name) for name in endpoint_names) + 2
        # Cap at reasonable max
        self.endpoint_width = min(self.endpoint_width, 30)

        # Latency time column: fixed width for "999.9ms" (8 chars)
        self.latency_time_width = 8

        # Protocol column: fixed width for "(TCP:443)" (10 chars)
        self.protocol_width = 10

        # Table padding: padding=(0, 1) means 1 space on each side of each cell
        # With 4 columns: 4 columns * 2 sides * 1 space = 8 spaces total
        table_padding = 8

        # Graph column: remaining space
        total_fixed = (
            self.endpoint_width + self.latency_time_width + self.protocol_width + table_padding
        )
        self.graph_width = terminal_width - total_fixed
        # Ensure minimum width
        self.graph_width = max(self.graph_width, 20)

    def render(self) -> Group:
        """Render the current state as a Rich group."""
        table = Table(show_header=False, box=None, padding=(0, 1))

        # Add columns with fixed widths
        table.add_column("Endpoint", width=self.endpoint_width, no_wrap=True)
        table.add_column("Graph", width=self.graph_width, no_wrap=True)
        table.add_column("Time", width=self.latency_time_width, justify="right", no_wrap=True)
        table.add_column("Protocol", width=self.protocol_width, justify="left", no_wrap=True)

        # Add rows for each endpoint
        for endpoint in self.orchestrator.endpoints:
            self._add_endpoint_row(table, endpoint)

        # Build problems section
        problems_text = self._render_problems()
        renderables = [table]

        if problems_text:
            renderables.append(Text())  # Empty line
            renderables.append(Text("Problems:", style="bold red"))
            for problem in problems_text:
                renderables.append(Text(f"  • {problem}", style="red"))

        return Group(*renderables)

    def _add_endpoint_row(self, table: Table, endpoint: Endpoint):
        """Add a row for an endpoint to the table."""
        # Get result history for this endpoint
        history = self.orchestrator.get_history(endpoint)

        if history is None:
            # No data yet
            time_str = "N/A"
            protocol_str = ""
            latency_style = "dim"
            graph_renderer = self.graphs[endpoint.raw]
            # Render empty graph (all None)
            bucketed_results = [None] * graph_renderer.width
            graph_text = graph_renderer.render(bucketed_results)
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
            graph_renderer = self.graphs[endpoint.raw]
            bucketed_results = history.get_bucketed_results(graph_renderer.width)
            graph_text = graph_renderer.render(bucketed_results)

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
        """Run the live dashboard."""
        # Set up live display with automatic refresh
        # We update the display in the main loop to avoid race conditions
        # from concurrent check results updating simultaneously
        live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=4,
            screen=False,
        )

        # Start orchestrator and live display
        live.start()
        await self.orchestrator.start()

        # Keep running and update display periodically
        try:
            import asyncio

            while True:
                # Update the display with fresh data
                live.update(self.render())
                await asyncio.sleep(1.0)  # Update once per second
        except KeyboardInterrupt:
            # Suppress KeyboardInterrupt output
            pass
        finally:
            await self.orchestrator.stop()
            live.stop()
