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
    async def check(self, target: str) -> CheckResult:
        """Perform the check and return result."""
        pass

    def _create_result(
        self,
        check_type: CheckType,
        success: bool,
        latency_ms: float | None = None,
        error_message: str | None = None,
        port: int | None = None,
        protocol: str | None = None,
    ) -> CheckResult:
        """Helper to create a CheckResult."""
        # Ensure latency is non-negative (protect against clock issues)
        if latency_ms is not None and latency_ms < 0:
            latency_ms = 0.0

        return CheckResult(
            timestamp=datetime.now(),
            check_type=check_type,
            success=success,
            latency_ms=latency_ms,
            error_message=error_message,
            port=port,
            protocol=protocol,
        )
