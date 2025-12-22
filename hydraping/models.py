"""Core data models for HydraPing."""

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
        """Validate the check result."""
        if self.success and self.latency_ms is None:
            raise ValueError("Successful check must have latency")
        if not self.success and self.error_message is None:
            raise ValueError("Failed check must have error message")


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

        # IP:port endpoint
        if ":" in endpoint_str:
            parts = endpoint_str.split(":")
            if len(parts) == 2:
                ip_part, port_part = parts
                # Check if it looks like an IP
                if _is_ip_address(ip_part):
                    try:
                        port = int(port_part)
                        return IPPortEndpoint(raw=endpoint_str, ip=ip_part, port=port)
                    except ValueError:
                        pass

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
    """Check if string looks like an IP address (simple check)."""
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False
