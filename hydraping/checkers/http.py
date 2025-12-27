"""HTTP/HTTPS request checker."""

import time
from datetime import datetime

import aiohttp

from hydraping.checkers.base import BaseChecker
from hydraping.models import CheckResult, CheckType


class HTTPChecker(BaseChecker):
    """Checker for HTTP/HTTPS requests."""

    def __init__(self, timeout: float = 5.0, success_status_max: int = 399):
        """Initialize HTTP checker.

        Args:
            timeout: Request timeout in seconds
            success_status_max: Maximum HTTP status code considered successful
                               (default: 399, accepts 2xx and 3xx)
        """
        super().__init__(timeout)
        self.success_status_max = success_status_max

    async def check(self, url: str, iteration_timestamp: datetime) -> CheckResult:
        """Perform HTTP request check."""
        # Determine protocol from URL
        protocol = "https" if url.startswith("https://") else "http"

        try:
            # Measure request time
            start_time = time.perf_counter()

            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=True) as response:
                    # Read response to ensure full request completes
                    await response.read()

                    end_time = time.perf_counter()
                    latency_ms = (end_time - start_time) * 1000

                    # Check if status is success based on configured threshold
                    if response.status <= self.success_status_max:
                        return self._create_result(
                            check_type=CheckType.HTTP,
                            success=True,
                            timestamp=iteration_timestamp,
                            latency_ms=latency_ms,
                            protocol=protocol,
                        )
                    else:
                        return self._create_result(
                            check_type=CheckType.HTTP,
                            success=False,
                            timestamp=iteration_timestamp,
                            error_message=f"HTTP {response.status} {response.reason}",
                            protocol=protocol,
                        )

        except aiohttp.ClientConnectorError as e:
            return self._create_result(
                check_type=CheckType.HTTP,
                success=False,
                timestamp=iteration_timestamp,
                error_message=f"Connection failed: {e}",
                protocol=protocol,
            )
        except aiohttp.ServerTimeoutError:
            return self._create_result(
                check_type=CheckType.HTTP,
                success=False,
                timestamp=iteration_timestamp,
                error_message=f"Request timeout (>{self.timeout}s)",
                protocol=protocol,
            )
        except aiohttp.ClientError as e:
            return self._create_result(
                check_type=CheckType.HTTP,
                success=False,
                timestamp=iteration_timestamp,
                error_message=f"HTTP client error: {e}",
                protocol=protocol,
            )
        except Exception as e:
            return self._create_result(
                check_type=CheckType.HTTP,
                success=False,
                timestamp=iteration_timestamp,
                error_message=f"HTTP error: {e}",
                protocol=protocol,
            )
