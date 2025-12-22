"""DNS resolver checker."""

import time
from datetime import datetime

import dns.asyncresolver
import dns.exception
import dns.resolver

from hydraping.checkers.base import BaseChecker
from hydraping.models import CheckResult, CheckType


class DNSChecker(BaseChecker):
    """Checker for DNS resolution."""

    def __init__(self, timeout: float = 5.0, nameservers: list[str] | None = None):
        """Initialize DNS checker with optional custom nameservers."""
        super().__init__(timeout)
        self.nameservers = nameservers

    async def check(self, target: str, iteration_timestamp: datetime) -> CheckResult:
        """Perform DNS resolution check."""
        try:
            resolver = dns.asyncresolver.Resolver()
            resolver.timeout = self.timeout
            resolver.lifetime = self.timeout

            # Use custom nameservers if provided
            if self.nameservers:
                resolver.nameservers = self.nameservers

            # Measure resolution time
            # Try A (IPv4) records first, fall back to AAAA (IPv6) if no answer
            start_time = time.perf_counter()
            answer = None
            try:
                answer = await resolver.resolve(target, "A")
            except dns.resolver.NoAnswer:
                # No IPv4 records, try IPv6
                answer = await resolver.resolve(target, "AAAA")
            end_time = time.perf_counter()

            # Verify we got at least one record
            if not answer or len(answer) == 0:
                return self._create_result(
                    check_type=CheckType.DNS,
                    success=False,
                    timestamp=iteration_timestamp,
                    error_message="No A or AAAA records returned",
                )

            latency_ms = (end_time - start_time) * 1000

            return self._create_result(
                check_type=CheckType.DNS,
                success=True,
                timestamp=iteration_timestamp,
                latency_ms=latency_ms,
            )

        except dns.resolver.NXDOMAIN:
            return self._create_result(
                check_type=CheckType.DNS,
                success=False,
                timestamp=iteration_timestamp,
                error_message="Domain does not exist (NXDOMAIN)",
            )
        except dns.resolver.Timeout:
            return self._create_result(
                check_type=CheckType.DNS,
                success=False,
                timestamp=iteration_timestamp,
                error_message=f"DNS query timeout (>{self.timeout}s)",
            )
        except dns.resolver.NoAnswer:
            return self._create_result(
                check_type=CheckType.DNS,
                success=False,
                timestamp=iteration_timestamp,
                error_message="No DNS answer received",
            )
        except dns.resolver.NoNameservers:
            return self._create_result(
                check_type=CheckType.DNS,
                success=False,
                timestamp=iteration_timestamp,
                error_message="No nameservers available",
            )
        except Exception as e:
            return self._create_result(
                check_type=CheckType.DNS,
                success=False,
                timestamp=iteration_timestamp,
                error_message=f"DNS error: {e}",
            )
