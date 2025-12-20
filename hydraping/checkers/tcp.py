"""TCP port connectivity checker."""

import asyncio
import time

from hydraping.checkers.base import BaseChecker
from hydraping.models import CheckResult, CheckType


class TCPChecker(BaseChecker):
    """Checker for TCP port connectivity."""

    async def check(self, host: str, port: int) -> CheckResult:
        """Perform TCP connection check."""
        try:
            # Measure connection time
            start_time = time.perf_counter()

            # Attempt TCP connection
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self.timeout
            )

            end_time = time.perf_counter()
            latency_ms = (end_time - start_time) * 1000

            # Close the connection immediately
            writer.close()
            await writer.wait_closed()

            return self._create_result(
                check_type=CheckType.TCP, success=True, latency_ms=latency_ms, port=port
            )

        except TimeoutError:
            return self._create_result(
                check_type=CheckType.TCP,
                success=False,
                error_message=f"Connection timeout (>{self.timeout}s)",
            )
        except ConnectionRefusedError:
            return self._create_result(
                check_type=CheckType.TCP,
                success=False,
                error_message=f"Connection refused on port {port}",
            )
        except OSError as e:
            return self._create_result(
                check_type=CheckType.TCP,
                success=False,
                error_message=f"Network error: {e}",
            )
        except Exception as e:
            return self._create_result(
                check_type=CheckType.TCP,
                success=False,
                error_message=f"TCP error: {e}",
            )
