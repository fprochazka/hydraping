"""ICMP ping checker."""

import icmplib

from hydraping.checkers.base import BaseChecker
from hydraping.models import CheckResult, CheckType


class ICMPChecker(BaseChecker):
    """Checker for ICMP ping."""

    async def check(self, target: str) -> CheckResult:
        """Perform ICMP ping check."""
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
        except icmplib.SocketPermissionError:
            return self._create_result(
                check_type=CheckType.ICMP,
                success=False,
                error_message="Permission denied (try running with CAP_NET_RAW or as root)",
            )
        except Exception as e:
            return self._create_result(
                check_type=CheckType.ICMP,
                success=False,
                error_message=f"ICMP error: {e}",
            )
