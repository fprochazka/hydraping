"""ICMP ping checker."""

import icmplib

from hydraping.checkers.base import BaseChecker
from hydraping.models import CheckResult, CheckType


class ICMPChecker(BaseChecker):
    """Checker for ICMP ping."""

    def __init__(self, timeout: float = 5.0):
        """Initialize ICMP checker."""
        super().__init__(timeout)
        self._permission_denied = False

    async def check(self, target: str) -> CheckResult:
        """Perform ICMP ping check."""
        # If we've already detected permission issues, skip silently
        if self._permission_denied:
            return self._create_result(
                check_type=CheckType.ICMP,
                success=False,
                error_message="ICMP unavailable (no permissions)",
            )

        try:
            # Use async ping from icmplib
            host = await icmplib.async_ping(target, count=1, timeout=self.timeout, privileged=False)

            if host.is_alive:
                # Convert to milliseconds
                latency_ms = host.avg_rtt
                return self._create_result(
                    check_type=CheckType.ICMP,
                    success=True,
                    latency_ms=latency_ms,
                )
            else:
                return self._create_result(
                    check_type=CheckType.ICMP,
                    success=False,
                    error_message=f"Host unreachable (packet loss: {host.packet_loss})",
                )

        except icmplib.NameLookupError as e:
            return self._create_result(
                check_type=CheckType.ICMP,
                success=False,
                error_message=f"Name lookup failed: {e}",
            )
        except (icmplib.SocketPermissionError, PermissionError, OSError) as e:
            # Mark as permission denied for future checks
            self._permission_denied = True
            return self._create_result(
                check_type=CheckType.ICMP,
                success=False,
                error_message=f"ICMP unavailable: {e}",
            )
        except Exception as e:
            return self._create_result(
                check_type=CheckType.ICMP,
                success=False,
                error_message=f"ICMP error: {e}",
            )
