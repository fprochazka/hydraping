"""Main terminal UI dashboard for HydraPing."""

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from hydraping.models import CheckType, Endpoint
from hydraping.orchestrator import CheckOrchestrator
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

        # Endpoint column: longest endpoint name + padding
        endpoint_names = [ep.display_name for ep in self.orchestrator.endpoints]
        self.endpoint_width = max(len(name) for name in endpoint_names) + 2
        # Cap at reasonable max
        self.endpoint_width = min(self.endpoint_width, 30)

        # Latency column: fixed width for "999.99ms"
        self.latency_width = 10

        # Spacing between columns (2 spaces between each)
        spacing = 4  # 2 spaces before graph, 2 spaces before latency

        # Graph column: remaining space
        self.graph_width = terminal_width - self.endpoint_width - self.latency_width - spacing
        # Ensure minimum width
        self.graph_width = max(self.graph_width, 20)

    def render(self) -> Table:
        """Render the current state as a Rich table."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))

        # Add columns with fixed widths
        table.add_column("Endpoint", width=self.endpoint_width, no_wrap=True)
        table.add_column("Graph", width=self.graph_width, no_wrap=True)
        table.add_column("Latency", width=self.latency_width, justify="right", no_wrap=True)

        # Add rows for each endpoint
        for endpoint in self.orchestrator.endpoints:
            self._add_endpoint_row(table, endpoint)

        # Add problems section if any
        problems_text = self._render_problems()
        if problems_text:
            # Add empty row for spacing
            table.add_row("", "", "")
            # Add problems header
            table.add_row(Text("Problems:", style="bold red"), "", "")
            # Add each problem
            for problem in problems_text:
                table.add_row(Text(f"  • {problem}", style="red"), "", "")

        return table

    def _add_endpoint_row(self, table: Table, endpoint: Endpoint):
        """Add a row for an endpoint to the table."""
        # Get the most relevant check result for latency display
        # Priority: HTTP > TCP > ICMP > DNS
        latency_result = None
        for check_type in [CheckType.HTTP, CheckType.TCP, CheckType.ICMP, CheckType.DNS]:
            result = self.orchestrator.get_latest_result(endpoint, check_type)
            if result:
                latency_result = result
                break

        # Format latency
        if latency_result and latency_result.success and latency_result.latency_ms is not None:
            latency_str = f"{latency_result.latency_ms:.1f}ms"
            latency_style = self._get_latency_color(latency_result.latency_ms)
        elif latency_result and not latency_result.success:
            latency_str = "FAIL"
            latency_style = "red"
        else:
            latency_str = "-"
            latency_style = "dim"

        # Get graph - for now just use ICMP results for graph
        # (We could aggregate multiple check types later)
        icmp_history = self.orchestrator.get_history(endpoint, CheckType.ICMP)
        graph_renderer = self.graphs[endpoint.raw]
        graph_str, graph_color = graph_renderer.render(icmp_history)

        # Add row
        table.add_row(
            Text(endpoint.display_name, style="bold"),
            Text(graph_str, style=graph_color),
            Text(latency_str, style=latency_style),
        )

    def _get_latency_color(self, latency_ms: float) -> str:
        """Get color for latency value."""
        if latency_ms < 50:
            return "green"
        elif latency_ms < 150:
            return "yellow"
        elif latency_ms < 300:
            return "bright_yellow"
        else:
            return "red"

    def _render_problems(self) -> list[str]:
        """Get list of current problems across all endpoints."""
        problems = []
        for endpoint in self.orchestrator.endpoints:
            endpoint_problems = self.orchestrator.get_problems(endpoint)
            for problem in endpoint_problems:
                problems.append(f"{endpoint.display_name}: {problem}")
        return problems

    async def run(self):
        """Run the live dashboard."""
        with Live(self.render(), console=self.console, refresh_per_second=4, screen=False) as live:
            # Set up callback to update display when results come in
            def on_result(endpoint: Endpoint, result):
                live.update(self.render())

            self.orchestrator.on_result = on_result

            # Start orchestrator
            await self.orchestrator.start()

            # Keep running until interrupted
            try:
                while True:
                    await self.orchestrator._task
            except KeyboardInterrupt:
                pass
            finally:
                await self.orchestrator.stop()
