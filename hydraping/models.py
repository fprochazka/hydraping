"""Core data models for HydraPing."""

import ipaddress
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
