"""Main terminal UI dashboard for HydraPing."""

from rich.console import Console, Group
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

        # Latency column: fixed width for "999.9ms (ICMP)"
        self.latency_width = 17

        # Spacing between columns (2 spaces between each)
        spacing = 4  # 2 spaces before graph, 2 spaces before latency

        # Graph column: remaining space
        self.graph_width = terminal_width - self.endpoint_width - self.latency_width - spacing
        # Ensure minimum width
        self.graph_width = max(self.graph_width, 20)

    def render(self) -> Group:
        """Render the current state as a Rich group."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))

        # Add columns with fixed widths
        table.add_column("Endpoint", width=self.endpoint_width, no_wrap=True)
        table.add_column("Graph", width=self.graph_width, no_wrap=True)
        table.add_column("Latency", width=self.latency_width, justify="right", no_wrap=True)

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
        # Get the most relevant check result for latency display
        # Priority: HTTP > TCP > DNS > ICMP (most abstract/high-level first)
        # Show the highest-level successful check for better diagnostics
        latency_result = None
        for check_type in [CheckType.HTTP, CheckType.TCP, CheckType.DNS, CheckType.ICMP]:
            result = self.orchestrator.get_latest_result(endpoint, check_type)
            if result and result.success:
                latency_result = result
                break

        # If no successful check, show the highest-level failure
        if not latency_result:
            for check_type in [CheckType.HTTP, CheckType.TCP, CheckType.DNS, CheckType.ICMP]:
                result = self.orchestrator.get_latest_result(endpoint, check_type)
                if result and "unavailable" not in result.error_message:
                    latency_result = result
                    break

        # Format latency with check type indicator
        if latency_result and latency_result.success and latency_result.latency_ms is not None:
            check_label = latency_result.check_type.value.upper()
            latency_str = f"{latency_result.latency_ms:.1f}ms ({check_label})"
            latency_style = self._get_latency_color(latency_result.latency_ms)
        elif latency_result and not latency_result.success:
            check_label = latency_result.check_type.value.upper()
            latency_str = f"FAIL ({check_label})"
            latency_style = "red"
        else:
            latency_str = "N/A"
            latency_style = "dim"

        # Get graph - use best available check type
        # Priority: HTTP > TCP > DNS > ICMP (most abstract/high-level first)
        # Show the highest-level check with successful results
        graph_history = None
        for check_type in [CheckType.HTTP, CheckType.TCP, CheckType.DNS, CheckType.ICMP]:
            history = self.orchestrator.get_history(endpoint, check_type)
            if history:
                # Only use checks with successful results
                successful_history = [r for r in history if r.success]
                if successful_history:
                    graph_history = successful_history
                    break

        if graph_history is None:
            graph_history = []

        graph_renderer = self.graphs[endpoint.raw]
        graph_str, graph_color = graph_renderer.render(graph_history)

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
