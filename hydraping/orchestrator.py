"""Async orchestration of all connectivity checks."""

import asyncio
import time
from datetime import datetime

from hydraping.checkers.dns import DNSChecker
from hydraping.checkers.http import HTTPChecker
from hydraping.checkers.icmp import ICMPChecker
from hydraping.checkers.tcp import TCPChecker
from hydraping.checkers.udp import UDPChecker
from hydraping.config import Config
from hydraping.models import (
    CHECK_TYPE_PRIORITY,
    CheckResult,
    CheckType,
    DomainEndpoint,
    Endpoint,
    EndpointResultHistory,
    HTTPEndpoint,
    IPEndpoint,
    IPPortEndpoint,
    UDPPortEndpoint,
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
        self.udp_checker = UDPChecker(timeout=config.checks.timeout_seconds)
        self.http_checker = HTTPChecker(timeout=config.checks.timeout_seconds)

        # Store results history per endpoint using EndpointResultHistory
        # Each history manages time bucketing, priority selection, and check hierarchy
        # Capacity: MAX_GRAPH_WIDTH * max_check_types (4) * safety_margin (2) = 2400
        self.history_capacity = MAX_GRAPH_WIDTH * 4 * 2
        self.history: dict[str, EndpointResultHistory] = {}

        # Shared time references for all endpoints (set when orchestrator starts)
        # This ensures all endpoints use the same bucket numbering
        self.start_time: float | None = None  # Monotonic time for bucket calculation
        self.start_timestamp: float | None = None  # Wall-clock time for result bucketing

        # Control flags
        self._running = False
        self._task: asyncio.Task | None = None

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

        # Set shared start times for all endpoints
        self.start_time = time.monotonic()
        self.start_timestamp = time.time()
        start_time = self.start_time  # Local variable for loop timing

        iteration = 0

        while self._running:
            # Calculate when this check should start (aligned to interval)
            target_time = start_time + (iteration * interval)
            now = time.monotonic()

            # If we're behind schedule, start immediately
            if now < target_time:
                await asyncio.sleep(target_time - now)

            # Capture iteration timestamp once for all checks
            # This ensures all results from this iteration land in the same bucket
            iteration_timestamp = datetime.now()

            # Run checks for all endpoints concurrently
            tasks = [
                self._check_endpoint(endpoint, iteration_timestamp) for endpoint in self.endpoints
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

            iteration += 1

    async def _check_endpoint(self, endpoint: Endpoint, iteration_timestamp: datetime):
        """Run all applicable checks for a single endpoint."""
        # Determine which checks to run based on endpoint type
        check_tasks = []

        if isinstance(endpoint, IPEndpoint):
            check_tasks.append(self._check_icmp(endpoint, endpoint.ip, iteration_timestamp))

        elif isinstance(endpoint, IPPortEndpoint):
            check_tasks.append(self._check_icmp(endpoint, endpoint.ip, iteration_timestamp))
            check_tasks.append(
                self._check_tcp(endpoint, endpoint.ip, endpoint.port, iteration_timestamp)
            )

        elif isinstance(endpoint, UDPPortEndpoint):
            check_tasks.append(self._check_icmp(endpoint, endpoint.ip, iteration_timestamp))
            check_tasks.append(
                self._check_udp(endpoint, endpoint.ip, endpoint.port, iteration_timestamp)
            )

        elif isinstance(endpoint, DomainEndpoint):
            # For domain: DNS, ICMP, and TCP checks (try both HTTP and HTTPS ports)
            check_tasks.append(
                self._check_dns(endpoint, endpoint.domain, iteration_timestamp, endpoint.ip_version)
            )
            check_tasks.append(self._check_icmp(endpoint, endpoint.domain, iteration_timestamp))
            check_tasks.append(
                self._check_tcp(endpoint, endpoint.domain, 80, iteration_timestamp)
            )  # HTTP
            check_tasks.append(
                self._check_tcp(endpoint, endpoint.domain, 443, iteration_timestamp)
            )  # HTTPS

        elif isinstance(endpoint, HTTPEndpoint):
            # For HTTP endpoint, run DNS, ICMP, TCP, and HTTP
            check_tasks.append(
                self._check_dns(endpoint, endpoint.host, iteration_timestamp, endpoint.ip_version)
            )
            check_tasks.append(self._check_icmp(endpoint, endpoint.host, iteration_timestamp))
            check_tasks.append(
                self._check_tcp(endpoint, endpoint.host, endpoint.port, iteration_timestamp)
            )
            check_tasks.append(self._check_http(endpoint, endpoint.url, iteration_timestamp))

        # Run all checks concurrently
        await asyncio.gather(*check_tasks, return_exceptions=True)

    async def _check_icmp(self, endpoint: Endpoint, target: str, iteration_timestamp: datetime):
        """Run ICMP check and store result."""
        result = await self.icmp_checker.check(target, iteration_timestamp)
        self._store_result(endpoint, result)

    async def _check_dns(
        self,
        endpoint: Endpoint,
        target: str,
        iteration_timestamp: datetime,
        ip_version: int | None = None,
    ):
        """Run DNS check and store result."""
        result = await self.dns_checker.check(target, iteration_timestamp, ip_version)
        self._store_result(endpoint, result)

    async def _check_tcp(
        self, endpoint: Endpoint, host: str, port: int, iteration_timestamp: datetime
    ):
        """Run TCP check and store result."""
        result = await self.tcp_checker.check(host, port, iteration_timestamp)
        self._store_result(endpoint, result)

    async def _check_udp(
        self, endpoint: Endpoint, host: str, port: int, iteration_timestamp: datetime
    ):
        """Run UDP check and store result."""
        result = await self.udp_checker.check(host, port, iteration_timestamp)
        self._store_result(endpoint, result)

    async def _check_http(self, endpoint: Endpoint, url: str, iteration_timestamp: datetime):
        """Run HTTP check and store result."""
        result = await self.http_checker.check(url, iteration_timestamp)
        self._store_result(endpoint, result)

    def _store_result(self, endpoint: Endpoint, result: CheckResult):
        """Store result in history."""
        # Get or create history for this endpoint
        if endpoint.raw not in self.history:
            self.history[endpoint.raw] = EndpointResultHistory(
                interval_seconds=self.config.checks.interval_seconds,
                max_capacity=self.history_capacity,
                start_time=self.start_time,
                start_timestamp=self.start_timestamp,
                primary_check_type=endpoint.get_primary_check_type(),
            )
        self.history[endpoint.raw].add_result(result)

    def get_latest_result(self, endpoint: Endpoint, check_type: CheckType) -> CheckResult | None:
        """Get the most recent result for an endpoint and check type."""
        history = self.history.get(endpoint.raw)
        if history is None:
            return None
        return history.get_latest_by_type(check_type)

    def get_history(self, endpoint: Endpoint) -> EndpointResultHistory | None:
        """Get result history object for an endpoint.

        Returns:
            EndpointResultHistory for the endpoint, or None if no history yet
        """
        return self.history.get(endpoint.raw)

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
