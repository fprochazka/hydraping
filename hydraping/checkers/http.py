"""HTTP/HTTPS request checker."""

import time

import aiohttp

from hydraping.checkers.base import BaseChecker
from hydraping.models import CheckResult, CheckType


class HTTPChecker(BaseChecker):
    """Checker for HTTP/HTTPS requests."""

    async def check(self, url: str) -> CheckResult:
        """Perform HTTP request check."""
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

                    # Check if status is success (2xx or 3xx)
                    if response.status < 400:
                        return self._create_result(
                            check_type=CheckType.HTTP,
                            success=True,
                            latency_ms=latency_ms,
                        )
                    else:
                        return self._create_result(
                            check_type=CheckType.HTTP,
                            success=False,
                            error_message=f"HTTP {response.status} {response.reason}",
                        )

        except aiohttp.ClientConnectorError as e:
            return self._create_result(
                check_type=CheckType.HTTP,
                success=False,
                error_message=f"Connection failed: {e}",
            )
        except aiohttp.ServerTimeoutError:
            return self._create_result(
                check_type=CheckType.HTTP,
                success=False,
                error_message=f"Request timeout (>{self.timeout}s)",
            )
        except aiohttp.ClientError as e:
            return self._create_result(
                check_type=CheckType.HTTP,
                success=False,
                error_message=f"HTTP client error: {e}",
            )
        except Exception as e:
            return self._create_result(
                check_type=CheckType.HTTP,
                success=False,
                error_message=f"HTTP error: {e}",
            )
