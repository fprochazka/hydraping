"""Core data models for HydraPing."""

import ipaddress
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from urllib.parse import urlparse


class CheckType(str, Enum):
    """Type of connectivity check."""

    DNS = "dns"
    ICMP = "icmp"
    TCP = "tcp"
    HTTP = "http"


# Check type priority order (highest to lowest)
# HTTP is the most comprehensive check (includes DNS, TCP, and application layer)
# TCP verifies port connectivity (includes DNS and transport layer)
# DNS verifies name resolution only
# ICMP is the basic network layer check
CHECK_TYPE_PRIORITY = [CheckType.HTTP, CheckType.TCP, CheckType.DNS, CheckType.ICMP]


class EndpointResultHistory:
    """Manages time-bucketed check results for a single endpoint.

    This class encapsulates all logic for:
    - Storing check results in time-ordered fashion
    - Bucketing results by time intervals
    - Selecting highest-priority result per bucket
    - Understanding check type hierarchy

    This ensures all consumers (graph, dashboard, etc.) see consistent data.
    """

    def __init__(self, interval_seconds: float, max_capacity: int = 2400):
        """Initialize result history.

        Args:
            interval_seconds: Time interval for bucketing results
            max_capacity: Maximum number of results to keep in history
        """
        self.interval_seconds = interval_seconds
        self.results: deque[CheckResult] = deque(maxlen=max_capacity)
        self.start_time: float | None = None  # Monotonic time for bucket calculation
        self.start_timestamp: float | None = None  # Wall-clock time for result bucketing
        self._priority_order = {check_type: i for i, check_type in enumerate(CHECK_TYPE_PRIORITY)}

    def add_result(self, result: "CheckResult") -> None:
        """Add a new check result.

        Args:
            result: The check result to add
        """
        if self.start_time is None:
            self.start_time = time.monotonic()
            self.start_timestamp = result.timestamp.timestamp()
        self.results.append(result)

    def get_current_result(self) -> "CheckResult | None":
        """Get the result for the current time bucket.

        This is what should be displayed "now" - the highest-priority result
        in the current time bucket. Uses check type hierarchy to select the
        best result when multiple checks completed in the same bucket.

        Returns:
            The current result to display, or None if no data yet
        """
        if self.start_time is None or self.start_timestamp is None or not self.results:
            return None

        # Calculate current time bucket
        now = time.monotonic()
        elapsed = now - self.start_time
        current_bucket = int(elapsed / self.interval_seconds)

        # Find highest-priority result in current bucket
        current_bucket_result = None

        for result in self.results:
            timestamp_s = result.timestamp.timestamp()
            elapsed_since_start = timestamp_s - self.start_timestamp
            bucket = int(elapsed_since_start / self.interval_seconds)

            # Only consider results in the current bucket
            if bucket != current_bucket:
                continue

            # Keep highest-priority result
            if current_bucket_result is None:
                current_bucket_result = result
            else:
                current_bucket_result = self._select_better_result(current_bucket_result, result)

        return current_bucket_result

    def get_bucketed_results(self, num_buckets: int) -> dict[int, "CheckResult"]:
        """Get bucketed results for the last N time buckets.

        Each bucket contains the highest-priority result that completed
        during that time interval.

        Args:
            num_buckets: Number of recent buckets to return

        Returns:
            Dictionary mapping bucket number to CheckResult
        """
        if self.start_time is None or self.start_timestamp is None:
            return {}

        # Calculate current bucket range
        now = time.monotonic()
        elapsed = now - self.start_time
        current_bucket = int(elapsed / self.interval_seconds)
        start_bucket = max(0, current_bucket - num_buckets + 1)
        end_bucket = current_bucket + 1

        # Build bucketed results
        results_by_bucket = {}

        for result in self.results:
            timestamp_s = result.timestamp.timestamp()
            elapsed_since_start = timestamp_s - self.start_timestamp
            bucket = int(elapsed_since_start / self.interval_seconds)

            # Only include buckets in the requested range
            if bucket < start_bucket or bucket >= end_bucket:
                continue

            # Keep highest-priority result per bucket
            if bucket not in results_by_bucket:
                results_by_bucket[bucket] = result
            else:
                results_by_bucket[bucket] = self._select_better_result(
                    results_by_bucket[bucket], result
                )

        return results_by_bucket

    def get_latest_by_type(self, check_type: "CheckType") -> "CheckResult | None":
        """Get the most recent result of a specific check type.

        Args:
            check_type: The type of check to find

        Returns:
            The most recent result of that type, or None if none found
        """
        for result in reversed(self.results):
            if result.check_type == check_type:
                return result
        return None

    def get_all_results(self, check_type: "CheckType | None" = None) -> list["CheckResult"]:
        """Get all results, optionally filtered by check type.

        Args:
            check_type: If provided, only return results of this type

        Returns:
            List of check results
        """
        if check_type is None:
            return list(self.results)
        return [r for r in self.results if r.check_type == check_type]

    def _select_better_result(
        self, current: "CheckResult", candidate: "CheckResult"
    ) -> "CheckResult":
        """Select the better of two results using check type priority.

        Successful checks are preferred over failures. Among checks with the
        same success state, higher-priority check types (HTTP > TCP > DNS > ICMP)
        are preferred.

        Args:
            current: The current result
            candidate: The candidate result to compare

        Returns:
            The better result
        """
        current_priority = self._priority_order.get(current.check_type, 999)
        candidate_priority = self._priority_order.get(candidate.check_type, 999)

        # Prefer successful results over failures
        if candidate.success and not current.success:
            return candidate
        if not candidate.success and current.success:
            return current

        # Among same success state, prefer higher priority (lower number)
        if candidate_priority < current_priority:
            return candidate

        return current


class EndpointType(str, Enum):
    """Type of endpoint being checked."""

    IP = "ip"
    IP_PORT = "ip_port"
    DOMAIN = "domain"
    HTTP = "http"


@dataclass
class CheckResult:
    """Result of a single connectivity check."""

    timestamp: datetime
    check_type: CheckType
    success: bool
    latency_ms: float | None = None
    error_message: str | None = None
    port: int | None = None  # For TCP checks
    protocol: str | None = None  # For HTTP checks (http/https)

    def __post_init__(self):
        """Validate the check result.

        Uses assertions to catch bugs during development without crashing
        in production. Checkers are trusted internal code.
        """
        assert not (self.success and self.latency_ms is None), "Successful check must have latency"
        assert not (not self.success and self.error_message is None), (
            "Failed check must have error message"
        )


@dataclass
class Endpoint:
    """Base class for network endpoints to check."""

    raw: str

    def __post_init__(self):
        """Initialize endpoint type - to be overridden by subclasses."""
        if not hasattr(self, "endpoint_type"):
            self.endpoint_type = None

    @property
    def display_name(self) -> str:
        """Human-readable name for display in UI."""
        return self.raw

    @staticmethod
    def parse(endpoint_str: str) -> "Endpoint":
        """Parse an endpoint string into the appropriate Endpoint subclass."""
        # HTTP/HTTPS endpoint
        if endpoint_str.startswith(("http://", "https://")):
            return HTTPEndpoint.from_string(endpoint_str)

        # IP:port endpoint (with IPv6 bracket notation support)
        # IPv6 with port: [2001:4860:4860::8888]:80
        # IPv4 with port: 1.1.1.1:53
        if endpoint_str.startswith("[") and "]:" in endpoint_str:
            # IPv6 bracket notation
            try:
                bracket_end = endpoint_str.index("]:")
                ip_part = endpoint_str[1:bracket_end]
                port_part = endpoint_str[bracket_end + 2 :]
                if _is_ip_address(ip_part):
                    try:
                        port = int(port_part)
                    except ValueError:
                        # Port is not a valid integer, fall through to domain parsing
                        pass
                    else:
                        # Port parsed successfully, now validate it
                        _validate_port(port)
                        return IPPortEndpoint(raw=endpoint_str, ip=ip_part, port=port)
            except IndexError:
                pass
        elif ":" in endpoint_str and not endpoint_str.count(":") > 1:
            # IPv4 with port (single colon)
            parts = endpoint_str.split(":")
            if len(parts) == 2:
                ip_part, port_part = parts
                # Check if it looks like an IPv4 address
                if _is_ip_address(ip_part):
                    try:
                        port = int(port_part)
                    except ValueError:
                        # Port is not a valid integer, fall through to domain parsing
                        pass
                    else:
                        # Port parsed successfully, now validate it
                        _validate_port(port)
                        return IPPortEndpoint(raw=endpoint_str, ip=ip_part, port=port)

        # Plain IP address
        if _is_ip_address(endpoint_str):
            return IPEndpoint(raw=endpoint_str, ip=endpoint_str)

        # Domain name
        return DomainEndpoint(raw=endpoint_str, domain=endpoint_str)


@dataclass
class IPEndpoint(Endpoint):
    """IP address endpoint - only ICMP check."""

    ip: str

    def __post_init__(self):
        """Set endpoint type."""
        self.endpoint_type = EndpointType.IP

    def get_check_types(self) -> list[CheckType]:
        """Return list of check types applicable to this endpoint."""
        return [CheckType.ICMP]


@dataclass
class IPPortEndpoint(Endpoint):
    """IP:port endpoint - ICMP + TCP checks."""

    ip: str
    port: int

    def __post_init__(self):
        """Set endpoint type."""
        self.endpoint_type = EndpointType.IP_PORT

    @property
    def display_name(self) -> str:
        """Human-readable name for display in UI."""
        return f"{self.ip}:{self.port}"

    def get_check_types(self) -> list[CheckType]:
        """Return list of check types applicable to this endpoint."""
        return [CheckType.ICMP, CheckType.TCP]


@dataclass
class DomainEndpoint(Endpoint):
    """Domain name endpoint - DNS + ICMP + TCP checks."""

    domain: str
    port: int = 80  # Default to HTTP port

    def __post_init__(self):
        """Set endpoint type."""
        self.endpoint_type = EndpointType.DOMAIN

    @property
    def display_name(self) -> str:
        """Human-readable name for display in UI."""
        if self.port != 80:
            return f"{self.domain}:{self.port}"
        return self.domain

    def get_check_types(self) -> list[CheckType]:
        """Return list of check types applicable to this endpoint.

        Note: TCP checks may run multiple times on different ports (80, 443).
        """
        return [CheckType.DNS, CheckType.ICMP, CheckType.TCP]


@dataclass
class HTTPEndpoint(Endpoint):
    """HTTP/HTTPS endpoint - DNS + ICMP + TCP + HTTP checks."""

    url: str
    scheme: str
    host: str
    port: int
    path: str

    def __post_init__(self):
        """Set endpoint type."""
        self.endpoint_type = EndpointType.HTTP

    @staticmethod
    def from_string(url: str) -> "HTTPEndpoint":
        """Parse HTTP/HTTPS URL into HTTPEndpoint."""
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid HTTP URL: {url}")

        # Determine port
        if parsed.port:
            port = parsed.port
        elif parsed.scheme == "https":
            port = 443
        else:
            port = 80

        return HTTPEndpoint(
            raw=url,
            url=url,
            scheme=parsed.scheme,
            host=parsed.hostname or parsed.netloc,
            port=port,
            path=parsed.path or "/",
        )

    def get_check_types(self) -> list[CheckType]:
        """Return list of check types applicable to this endpoint."""
        return [CheckType.DNS, CheckType.ICMP, CheckType.TCP, CheckType.HTTP]


def _is_ip_address(s: str) -> bool:
    """Check if string is a valid IP address (IPv4 or IPv6)."""
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _validate_port(port: int) -> None:
    """Validate port number is in valid range.

    Args:
        port: Port number to validate

    Raises:
        ValueError: If port is out of valid range (1-65535)
    """
    if not (1 <= port <= 65535):
        raise ValueError(f"Port {port} out of valid range 1-65535")
