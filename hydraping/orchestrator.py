"""Async orchestration of all connectivity checks."""

import asyncio
import time
from collections import defaultdict, deque

from hydraping.checkers.dns import DNSChecker
from hydraping.checkers.http import HTTPChecker
from hydraping.checkers.icmp import ICMPChecker
from hydraping.checkers.tcp import TCPChecker
from hydraping.config import Config
from hydraping.models import (
    CHECK_TYPE_PRIORITY,
    CheckResult,
    CheckType,
    DomainEndpoint,
    Endpoint,
    HTTPEndpoint,
    IPEndpoint,
    IPPortEndpoint,
)

# Maximum expected graph width for history buffer sizing
MAX_GRAPH_WIDTH = 300


class CheckOrchestrator:
    """Orchestrates all connectivity checks for configured endpoints."""

    def __init__(self, config: Config):
        """Initialize orchestrator with configuration."""
        self.config = config
        self.endpoints = config.endpoints

        # Initialize checkers
        self.icmp_checker = ICMPChecker(timeout=config.checks.timeout_seconds)
        self.dns_checker = DNSChecker(
            timeout=config.checks.timeout_seconds,
            nameservers=config.dns.custom_servers if config.dns.custom_servers else None,
        )
        self.tcp_checker = TCPChecker(timeout=config.checks.timeout_seconds)
        self.http_checker = HTTPChecker(timeout=config.checks.timeout_seconds)

        # Store results history per endpoint (rolling buffer)
        # Key: endpoint.raw, Value: deque of CheckResult
        # Capacity: MAX_GRAPH_WIDTH * max_check_types (4) * safety_margin (2) = 2400
        # Ensures we can display full graph width even on very wide terminals
        history_capacity = MAX_GRAPH_WIDTH * 4 * 2
        self.history: dict[str, deque[CheckResult]] = defaultdict(
            lambda: deque(maxlen=history_capacity)
        )

        # Control flags
        self._running = False
        self._task: asyncio.Task | None = None
        self.start_time: float | None = None  # Monotonic time when checks started
        self.start_timestamp: float | None = None  # Wall-clock time when checks started

    async def start(self):
        """Start the orchestration loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        """Stop the orchestration loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self):
        """Main loop that runs checks at configured interval."""
        interval = self.config.checks.interval_seconds
        start_time = time.monotonic()
        self.start_time = start_time  # Monotonic time for graph rendering
        self.start_timestamp = time.time()  # Wall-clock time for bucket calculations
        iteration = 0

        while self._running:
            # Calculate when this check should start (aligned to interval)
            target_time = start_time + (iteration * interval)
            now = time.monotonic()

            # If we're behind schedule, start immediately
            if now < target_time:
                await asyncio.sleep(target_time - now)

            # Run checks for all endpoints concurrently
            tasks = [self._check_endpoint(endpoint) for endpoint in self.endpoints]
            await asyncio.gather(*tasks, return_exceptions=True)

            iteration += 1

    async def _check_endpoint(self, endpoint: Endpoint):
        """Run all applicable checks for a single endpoint."""
        # Determine which checks to run based on endpoint type
        check_tasks = []

        if isinstance(endpoint, IPEndpoint):
            check_tasks.append(self._check_icmp(endpoint, endpoint.ip))

        elif isinstance(endpoint, IPPortEndpoint):
            check_tasks.append(self._check_icmp(endpoint, endpoint.ip))
            check_tasks.append(self._check_tcp(endpoint, endpoint.ip, endpoint.port))

        elif isinstance(endpoint, DomainEndpoint):
            # For domain: DNS, ICMP, and TCP checks (try both HTTP and HTTPS ports)
            check_tasks.append(self._check_dns(endpoint, endpoint.domain))
            check_tasks.append(self._check_icmp(endpoint, endpoint.domain))
            check_tasks.append(self._check_tcp(endpoint, endpoint.domain, 80))  # HTTP
            check_tasks.append(self._check_tcp(endpoint, endpoint.domain, 443))  # HTTPS

        elif isinstance(endpoint, HTTPEndpoint):
            # For HTTP endpoint, run DNS, ICMP, TCP, and HTTP
            check_tasks.append(self._check_dns(endpoint, endpoint.host))
            check_tasks.append(self._check_icmp(endpoint, endpoint.host))
            check_tasks.append(self._check_tcp(endpoint, endpoint.host, endpoint.port))
            check_tasks.append(self._check_http(endpoint, endpoint.url))

        # Run all checks concurrently
        await asyncio.gather(*check_tasks, return_exceptions=True)

    async def _check_icmp(self, endpoint: Endpoint, target: str):
        """Run ICMP check and store result."""
        result = await self.icmp_checker.check(target)
        self._store_result(endpoint, result)

    async def _check_dns(self, endpoint: Endpoint, target: str):
        """Run DNS check and store result."""
        result = await self.dns_checker.check(target)
        self._store_result(endpoint, result)

    async def _check_tcp(self, endpoint: Endpoint, host: str, port: int):
        """Run TCP check and store result."""
        result = await self.tcp_checker.check(host, port)
        self._store_result(endpoint, result)

    async def _check_http(self, endpoint: Endpoint, url: str):
        """Run HTTP check and store result."""
        result = await self.http_checker.check(url)
        self._store_result(endpoint, result)

    def _store_result(self, endpoint: Endpoint, result: CheckResult):
        """Store result in history."""
        self.history[endpoint.raw].append(result)

    def get_latest_result(self, endpoint: Endpoint, check_type: CheckType) -> CheckResult | None:
        """Get the most recent result for an endpoint and check type."""
        results = self.history.get(endpoint.raw, [])
        for result in reversed(results):
            if result.check_type == check_type:
                return result
        return None

    def get_history(
        self, endpoint: Endpoint, check_type: CheckType | None = None
    ) -> list[CheckResult]:
        """Get result history for an endpoint, optionally filtered by check type."""
        results = list(self.history.get(endpoint.raw, []))
        if check_type:
            results = [r for r in results if r.check_type == check_type]
        return results

    def get_problems(self, endpoint: Endpoint) -> list[str]:
        """Get list of current problems for an endpoint."""
        problems = []

        # Check hierarchy: HTTP > TCP > DNS > ICMP
        # If a higher-level check succeeds, suppress lower-level failures
        check_hierarchy = CHECK_TYPE_PRIORITY

        # Find the highest successful check level
        highest_success_level = -1
        for i, check_type in enumerate(check_hierarchy):
            result = self.get_latest_result(endpoint, check_type)
            if result and result.success:
                highest_success_level = i
                break

        # Only report failures for checks at or above the highest success level
        for i, check_type in enumerate(check_hierarchy):
            result = self.get_latest_result(endpoint, check_type)
            if result and not result.success:
                # Skip ICMP unavailable errors (system-wide permission issues)
                if check_type == CheckType.ICMP and not self.icmp_checker.is_available():
                    continue

                # Skip lower-level failures if a higher-level check succeeded
                if highest_success_level >= 0 and i > highest_success_level:
                    continue

                problems.append(f"{check_type.value.upper()}: {result.error_message}")

        return problems
