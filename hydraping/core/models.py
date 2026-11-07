"""Core data models for HydraPing."""

import ipaddress
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse


class CheckType(str, Enum):
    """Type of connectivity check."""

    DNS = "dns"
    ICMP = "icmp"
    TCP = "tcp"
    UDP = "udp"
    HTTP = "http"


# Check type priority order (highest to lowest)
# HTTP is the most comprehensive check (includes DNS, TCP, and application layer)
# TCP/UDP verify port connectivity (includes DNS and transport layer)
# DNS verifies name resolution only
# ICMP is the basic network layer check
CHECK_TYPE_PRIORITY = [CheckType.HTTP, CheckType.TCP, CheckType.UDP, CheckType.DNS, CheckType.ICMP]


class EndpointType(str, Enum):
    """Type of endpoint being checked."""

    IP = "ip"
    IP_PORT = "ip_port"
    UDP_PORT = "udp_port"
    DOMAIN = "domain"
    HTTP = "http"


@dataclass(frozen=True, kw_only=True)
class Measurement:
    """What one check found out, without when it happened.

    Checkers return measurements. The probe scheduler stamps them with the probe's start and end
    times to make them ``CheckResult`` objects, so one clock decides ordering and bucketing.
    """

    check_type: CheckType
    success: bool
    latency_ms: float | None = None
    error_message: str | None = None
    port: int | None = None  # For TCP checks
    protocol: str | None = None  # For HTTP checks (http/https)
    resolved_ip: str | None = None  # For DNS checks - first resolved IP address

    def __post_init__(self):
        if self.success and self.latency_ms is None:
            raise ValueError("Successful check must have latency")
        if not self.success and self.error_message is None:
            raise ValueError("Failed check must have error message")


@dataclass(frozen=True, kw_only=True)
class CheckResult(Measurement):
    """A measurement with the probe's start and end times, as epoch seconds.

    ``finished_at`` decides the bucket a result lands in. ``started_at`` decides which of two
    results of the same layer is newer, so a slow probe cannot overwrite a faster, later one.
    """

    started_at: float
    finished_at: float

    @classmethod
    def from_measurement(
        cls, measurement: Measurement, started_at: float, finished_at: float
    ) -> "CheckResult":
        return cls(
            check_type=measurement.check_type,
            success=measurement.success,
            latency_ms=measurement.latency_ms,
            error_message=measurement.error_message,
            port=measurement.port,
            protocol=measurement.protocol,
            resolved_ip=measurement.resolved_ip,
            started_at=started_at,
            finished_at=finished_at,
        )


@dataclass
class Endpoint:
    """Base class for network endpoints to check."""

    raw: str
    custom_name: str | None = field(default=None, kw_only=True)
    ip_version: int | None = field(default=None, kw_only=True)  # 4 or 6
    primary_check_type: CheckType | None = field(default=None, kw_only=True)

    def __post_init__(self):
        """Initialize endpoint type - to be overridden by subclasses."""
        if not hasattr(self, "endpoint_type"):
            self.endpoint_type = None

    @property
    def display_name(self) -> str:
        """Human-readable name for display in UI."""
        # Use custom name if provided, otherwise use default formatting
        if self.custom_name:
            return self.custom_name
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
            # IPv4:port or domain:port (single colon)
            parts = endpoint_str.split(":")
            if len(parts) == 2:
                host_part, port_part = parts
                # Try to parse port as integer
                try:
                    port = int(port_part)
                    _validate_port(port)
                except ValueError:
                    # Port is not a valid integer, fall through to domain parsing
                    pass
                else:
                    # Port parsed successfully - check if host is IP or domain
                    if _is_ip_address(host_part):
                        return IPPortEndpoint(raw=endpoint_str, ip=host_part, port=port)
                    else:
                        # Domain with port
                        return DomainEndpoint(
                            raw=endpoint_str, domain=host_part, port=port, port_specified=True
                        )

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

    def get_primary_check_type(self) -> CheckType:
        """Return the primary check type to display in graph/latency."""
        return self.primary_check_type if self.primary_check_type else CheckType.ICMP


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
        if self.custom_name:
            return self.custom_name
        # Use bracket notation for IPv6 addresses for clarity
        if _is_ipv6_address(self.ip):
            return f"[{self.ip}]:{self.port}"
        return f"{self.ip}:{self.port}"

    def get_check_types(self) -> list[CheckType]:
        """Return list of check types applicable to this endpoint."""
        return [CheckType.ICMP, CheckType.TCP]

    def get_primary_check_type(self) -> CheckType:
        """Return the primary check type to display in graph/latency."""
        return self.primary_check_type if self.primary_check_type else CheckType.TCP


@dataclass
class UDPPortEndpoint(Endpoint):
    """IP:port endpoint for UDP - ICMP + UDP checks."""

    ip: str
    port: int
    probe_data: bytes = field(default=b"", kw_only=True)  # Custom UDP probe payload

    def __post_init__(self):
        """Set endpoint type."""
        self.endpoint_type = EndpointType.UDP_PORT

    @property
    def display_name(self) -> str:
        """Human-readable name for display in UI."""
        if self.custom_name:
            return self.custom_name
        # Use bracket notation for IPv6 addresses for clarity
        if _is_ipv6_address(self.ip):
            return f"[{self.ip}]:{self.port} (UDP)"
        return f"{self.ip}:{self.port} (UDP)"

    def get_check_types(self) -> list[CheckType]:
        """Return list of check types applicable to this endpoint."""
        return [CheckType.ICMP, CheckType.UDP]

    def get_primary_check_type(self) -> CheckType:
        """Return the primary check type to display in graph/latency."""
        return self.primary_check_type if self.primary_check_type else CheckType.UDP


@dataclass
class DomainEndpoint(Endpoint):
    """Domain name endpoint - DNS + ICMP + TCP checks."""

    domain: str
    port: int = 80  # Default to HTTP port
    port_specified: bool = field(default=False, kw_only=True)  # Was port explicitly set?

    def __post_init__(self):
        """Set endpoint type."""
        self.endpoint_type = EndpointType.DOMAIN

    @property
    def display_name(self) -> str:
        """Human-readable name for display in UI."""
        if self.custom_name:
            return self.custom_name
        if self.port != 80:
            return f"{self.domain}:{self.port}"
        return self.domain

    def get_check_types(self) -> list[CheckType]:
        """Return list of check types applicable to this endpoint.

        Note: For domains without explicit port, TCP checks run on ports 80 and 443.
        For domains with explicit port, only that port is checked.
        """
        return [CheckType.DNS, CheckType.ICMP, CheckType.TCP]

    def get_primary_check_type(self) -> CheckType:
        """Return the primary check type to display in graph/latency.

        Returns TCP as primary when a port is explicitly specified in the URL,
        since port specification implies interest in TCP connectivity.
        For domains without explicit port, returns ICMP for direct connectivity.
        """
        if self.primary_check_type:
            return self.primary_check_type
        # If port was explicitly specified, use TCP as primary
        if self.port_specified:
            return CheckType.TCP
        # For plain domain without port, use ICMP
        return CheckType.ICMP


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

    def get_primary_check_type(self) -> CheckType:
        """Return the primary check type to display in graph/latency."""
        return self.primary_check_type if self.primary_check_type else CheckType.HTTP


def _is_ip_address(s: str) -> bool:
    """Check if string is a valid IP address (IPv4 or IPv6)."""
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _is_ipv6_address(s: str) -> bool:
    """Check if string is a valid IPv6 address."""
    try:
        addr = ipaddress.ip_address(s)
        return isinstance(addr, ipaddress.IPv6Address)
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
