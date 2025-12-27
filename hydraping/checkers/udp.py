"""UDP port connectivity checker."""

import asyncio
import time
from datetime import datetime

from hydraping.checkers.base import BaseChecker
from hydraping.models import CheckResult, CheckType


class UDPChecker(BaseChecker):
    """Checker for UDP port connectivity."""

    async def check(
        self,
        host: str,
        port: int,
        iteration_timestamp: datetime,
        probe_data: bytes = b"",
    ) -> CheckResult:
        """Perform UDP connectivity check.

        Sends a probe packet and waits for any response.
        Note: Many UDP services require protocol-specific data, so this
        check may not work for all services.

        Args:
            host: Target host
            port: Target UDP port
            iteration_timestamp: Timestamp for this check iteration
            probe_data: Custom probe payload (default: empty datagram)
        """
        try:
            # Measure round-trip time
            start_time = time.perf_counter()

            # Create UDP endpoint connection
            loop = asyncio.get_event_loop()
            transport, protocol = await asyncio.wait_for(
                loop.create_datagram_endpoint(
                    lambda: UDPProbeProtocol(),
                    remote_addr=(host, port),
                ),
                timeout=self.timeout,
            )

            # Send the probe packet (custom payload or empty datagram)
            transport.sendto(probe_data)

            # Wait for response
            try:
                response_received = await asyncio.wait_for(
                    protocol.response_received, timeout=self.timeout
                )

                end_time = time.perf_counter()
                latency_ms = (end_time - start_time) * 1000

                transport.close()

                if response_received:
                    return self._create_result(
                        check_type=CheckType.UDP,
                        success=True,
                        timestamp=iteration_timestamp,
                        latency_ms=latency_ms,
                        port=port,
                    )
                else:
                    return self._create_result(
                        check_type=CheckType.UDP,
                        success=False,
                        timestamp=iteration_timestamp,
                        error_message=(
                            f"No response from UDP port {port} "
                            "(service may not respond to empty datagrams)"
                        ),
                        port=port,
                    )

            except TimeoutError:
                transport.close()
                return self._create_result(
                    check_type=CheckType.UDP,
                    success=False,
                    timestamp=iteration_timestamp,
                    error_message=f"No response from UDP port {port} (timeout)",
                    port=port,
                )

        except OSError as e:
            return self._create_result(
                check_type=CheckType.UDP,
                success=False,
                timestamp=iteration_timestamp,
                error_message=f"Network error: {e}",
                port=port,
            )
        except Exception as e:
            return self._create_result(
                check_type=CheckType.UDP,
                success=False,
                timestamp=iteration_timestamp,
                error_message=f"UDP error: {e}",
                port=port,
            )


class UDPProbeProtocol(asyncio.DatagramProtocol):
    """Simple UDP protocol that tracks if any response is received."""

    def __init__(self):
        """Initialize protocol without event loop dependency."""
        self._response_received_future = None

    @property
    def response_received(self):
        """Get or create the response future lazily.

        This avoids calling get_event_loop() during __init__, which can
        fail or get the wrong loop in Python 3.10+. Instead, we lazily
        create the future when first accessed, using the running loop.
        """
        if self._response_received_future is None:
            loop = asyncio.get_running_loop()
            self._response_received_future = loop.create_future()
        return self._response_received_future

    def datagram_received(self, data, addr):
        """Called when a datagram is received."""
        if not self.response_received.done():
            self.response_received.set_result(True)

    def error_received(self, exc):
        """Called when an error is received (e.g., ICMP port unreachable)."""
        if not self.response_received.done():
            self.response_received.set_result(False)

    def connection_lost(self, exc):
        """Called when connection is closed."""
        if not self.response_received.done():
            self.response_received.set_result(False)
