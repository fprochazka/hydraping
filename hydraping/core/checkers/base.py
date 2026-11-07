"""Base checker class for all connectivity checks."""

from abc import ABC, abstractmethod

from hydraping.core.models import CheckType, Measurement


class BaseChecker(ABC):
    """Abstract base class for all checkers.

    A checker measures one thing and returns within ``timeout`` seconds, as a failed measurement
    when the target does not answer in time. It never raises for network conditions.
    """

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    @abstractmethod
    async def check(self, target: str, **kwargs) -> Measurement:
        """Measure ``target`` and return the outcome."""

    def _create_result(
        self,
        check_type: CheckType,
        success: bool,
        latency_ms: float | None = None,
        error_message: str | None = None,
        port: int | None = None,
        protocol: str | None = None,
        resolved_ip: str | None = None,
    ) -> Measurement:
        # A clock that steps back during a check must not produce a negative latency.
        if latency_ms is not None and latency_ms < 0:
            latency_ms = 0.0

        return Measurement(
            check_type=check_type,
            success=success,
            latency_ms=latency_ms,
            error_message=error_message,
            port=port,
            protocol=protocol,
            resolved_ip=resolved_ip,
        )
