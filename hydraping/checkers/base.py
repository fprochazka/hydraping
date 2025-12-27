"""Base checker class for all connectivity checks."""

from abc import ABC, abstractmethod
from datetime import datetime

from hydraping.models import CheckResult, CheckType


class BaseChecker(ABC):
    """Abstract base class for all checkers."""

    def __init__(self, timeout: float = 5.0):
        """Initialize checker with timeout."""
        self.timeout = timeout

    @abstractmethod
    async def check(self, target: str, iteration_timestamp: datetime, **kwargs) -> CheckResult:
        """Perform the check and return result.

        Args:
            target: Target to check (IP, domain, URL, etc.)
            iteration_timestamp: Timestamp for this check iteration
            **kwargs: Additional checker-specific parameters (port, ip_version, etc.)

        Returns:
            CheckResult with success status, latency, and error information
        """
        pass

    def _create_result(
        self,
        check_type: CheckType,
        success: bool,
        timestamp: datetime,
        latency_ms: float | None = None,
        error_message: str | None = None,
        port: int | None = None,
        protocol: str | None = None,
        resolved_ip: str | None = None,
    ) -> CheckResult:
        """Helper to create a CheckResult.

        Args:
            check_type: Type of check performed
            success: Whether the check succeeded
            timestamp: Timestamp for this result (from iteration, not check completion time)
            latency_ms: Latency in milliseconds if successful
            error_message: Error message if failed
            port: Port number for TCP checks
            protocol: Protocol for HTTP checks
            resolved_ip: Resolved IP address for DNS checks

        Returns:
            CheckResult with the provided values
        """
        # Ensure latency is non-negative (protect against clock issues)
        if latency_ms is not None and latency_ms < 0:
            latency_ms = 0.0

        return CheckResult(
            timestamp=timestamp,
            check_type=check_type,
            success=success,
            latency_ms=latency_ms,
            error_message=error_message,
            port=port,
            protocol=protocol,
            resolved_ip=resolved_ip,
        )
