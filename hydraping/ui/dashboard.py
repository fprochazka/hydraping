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

        # Latency time column: fixed width for "999.9ms" (8 chars)
        self.latency_time_width = 8

        # Protocol column: fixed width for "(TCP:443)" (10 chars)
        self.protocol_width = 10

        # Spacing between columns (2 spaces between each)
        spacing = 6  # 2 spaces * 3 gaps (endpoint-graph, graph-time, time-protocol)

        # Graph column: remaining space
        total_fixed = self.endpoint_width + self.latency_time_width + self.protocol_width + spacing
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
        # Get the highest-priority successful check result for latency display
        # Priority: HTTP > TCP > DNS > ICMP (show the most comprehensive check)
        latency_result = None
        for check_type in [CheckType.HTTP, CheckType.TCP, CheckType.DNS, CheckType.ICMP]:
            result = self.orchestrator.get_latest_result(endpoint, check_type)
            if result and result.success and result.latency_ms is not None:
                latency_result = result
                break

        # If no successful check, show the highest-priority failure
        if not latency_result:
            for check_type in [CheckType.HTTP, CheckType.TCP, CheckType.DNS, CheckType.ICMP]:
                result = self.orchestrator.get_latest_result(endpoint, check_type)
                if result and "unavailable" not in result.error_message:
                    latency_result = result
                    break

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
            latency_style = self._get_latency_color(latency_result.latency_ms)
        elif latency_result and not latency_result.success:
            check_label = latency_result.check_type.value.upper()
            time_str = "FAIL"
            protocol_str = f"({check_label})"
            latency_style = "red"
        else:
            time_str = "N/A"
            protocol_str = ""
            latency_style = "dim"

        # Get graph - merge all successful check results across all check types
        # This ensures we show data as soon as ANY check completes, not just the highest priority
        all_successful_results = []
        for check_type in [CheckType.HTTP, CheckType.TCP, CheckType.DNS, CheckType.ICMP]:
            history = self.orchestrator.get_history(endpoint, check_type)
            if history:
                # Collect all successful results
                all_successful_results.extend([r for r in history if r.success])

        # Deduplicate by time interval - keeps best result per bucket across all check types
        graph_history = self._deduplicate_by_interval(
            all_successful_results, self.orchestrator.config.checks.interval_seconds
        )

        graph_renderer = self.graphs[endpoint.raw]
        graph_text = graph_renderer.render(
            graph_history,
            self.orchestrator.start_time,
            self.orchestrator.config.checks.interval_seconds,
        )

        # Add row
        table.add_row(
            Text(endpoint.display_name, style="bold"),
            graph_text,
            Text(time_str, style=latency_style),
            Text(protocol_str, style="dim"),
        )

    def _deduplicate_by_interval(self, results: list, interval_seconds: float) -> list:
        """
        Deduplicate results by time interval, keeping the highest-priority result per interval.

        When multiple checks run in the same interval, prefer higher-priority checks
        (HTTP > TCP > DNS > ICMP) to match what the latency column shows.
        """
        if not results:
            return []

        # Check type priority (HTTP is highest, ICMP is lowest)
        priority_order = {
            CheckType.HTTP: 0,
            CheckType.TCP: 1,
            CheckType.DNS: 2,
            CheckType.ICMP: 3,
        }

        # Group results by time bucket (use interval in seconds for consistent bucketing)
        buckets = {}

        for result in results:
            # Calculate which time bucket this result belongs to
            # Use floor division to get consistent buckets aligned to interval
            timestamp_s = result.timestamp.timestamp()
            bucket = int(timestamp_s / interval_seconds)

            # Keep the highest-priority result per bucket
            if bucket not in buckets:
                buckets[bucket] = result
            else:
                current_priority = priority_order.get(buckets[bucket].check_type, 999)
                new_priority = priority_order.get(result.check_type, 999)
                if new_priority < current_priority:
                    buckets[bucket] = result

        # Sort by bucket number (chronological order)
        deduplicated = [buckets[k] for k in sorted(buckets.keys())]
        return deduplicated

    def _get_latency_color(self, latency_ms: float) -> str:
        """Get color for latency value (matching graph color zones)."""
        if latency_ms < 50:
            return "green"
        elif latency_ms < 100:
            return "yellow"
        elif latency_ms < 200:
            return "orange1"
        else:
            return "red"

    def _render_problems(self) -> list[str]:
        """Get list of current problems across all endpoints."""
        problems = []
        icmp_unavailable_shown = False

        # Check if ICMP is globally unavailable
        if self.orchestrator.icmp_checker._permission_denied and not icmp_unavailable_shown:
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
        # Set up callback to update display when results come in
        live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=4,
            screen=False,
        )

        def on_result(endpoint: Endpoint, result):
            live.update(self.render())

        self.orchestrator.on_result = on_result

        # Start orchestrator and live display
        live.start()
        await self.orchestrator.start()

        # Keep running until interrupted
        try:
            while True:
                await self.orchestrator._task
        except KeyboardInterrupt:
            # Suppress KeyboardInterrupt output
            pass
        finally:
            await self.orchestrator.stop()
            live.stop()
