"""DNS resolver checker."""

import time

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

    async def check(self, target: str) -> CheckResult:
        """Perform DNS resolution check."""
        try:
            resolver = dns.asyncresolver.Resolver()
            resolver.timeout = self.timeout
            resolver.lifetime = self.timeout

            # Use custom nameservers if provided
            if self.nameservers:
                resolver.nameservers = self.nameservers

            # Measure resolution time
            start_time = time.perf_counter()
            _ = await resolver.resolve(target, "A")
            end_time = time.perf_counter()

            latency_ms = (end_time - start_time) * 1000

            return self._create_result(
                check_type=CheckType.DNS,
                success=True,
                latency_ms=latency_ms,
            )

        except dns.resolver.NXDOMAIN:
            return self._create_result(
                check_type=CheckType.DNS,
                success=False,
                error_message="Domain does not exist (NXDOMAIN)",
            )
        except dns.resolver.Timeout:
            return self._create_result(
                check_type=CheckType.DNS,
                success=False,
                error_message=f"DNS query timeout (>{self.timeout}s)",
            )
        except dns.resolver.NoAnswer:
            return self._create_result(
                check_type=CheckType.DNS,
                success=False,
                error_message="No DNS answer received",
            )
        except dns.resolver.NoNameservers:
            return self._create_result(
                check_type=CheckType.DNS,
                success=False,
                error_message="No nameservers available",
            )
        except Exception as e:
            return self._create_result(
                check_type=CheckType.DNS,
                success=False,
                error_message=f"DNS error: {e}",
            )
