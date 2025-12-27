"""Configuration management for HydraPing."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from hydraping.models import Endpoint


@dataclass
class DNSConfig:
    """DNS check configuration."""

    custom_servers: list[str] = field(default_factory=list)


@dataclass
class ChecksConfig:
    """General checks configuration."""

    interval_seconds: float = 5.0
    timeout_seconds: float = 5.0
    http_success_status_max: int = 399  # HTTP status codes <= this are considered success


@dataclass
class UIConfig:
    """UI display configuration."""

    graph_width: int = 0  # 0 means auto-size to terminal width


@dataclass
class Config:
    """Main configuration for HydraPing."""

    endpoints: list[Endpoint]
    dns: DNSConfig = field(default_factory=DNSConfig)
    checks: ChecksConfig = field(default_factory=ChecksConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    @staticmethod
    def load(config_path: Path | None = None) -> "Config":
        """Load configuration from TOML file."""
        if config_path is None:
            config_path = get_default_config_path()

        if not config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {config_path}\n"
                f"Run 'hydraping init' to create a default configuration."
            )

        with config_path.open("rb") as f:
            data = tomllib.load(f)

        # Parse endpoints
        endpoint_configs = data.get("endpoints", {}).get("targets", [])
        if not endpoint_configs:
            raise ValueError("No endpoints configured in 'endpoints.targets'")

        endpoints = []
        for ep_config in endpoint_configs:
            if isinstance(ep_config, str):
                # Simple string format - use URL as display name
                endpoint = Endpoint.parse(ep_config)
            elif isinstance(ep_config, dict):
                # Object format with optional custom name, protocol, and IP version
                url = ep_config.get("url")
                if not url:
                    raise ValueError(f"Endpoint object missing 'url' field: {ep_config}")

                # Check if this is a UDP endpoint (protocol field)
                protocol = ep_config.get("protocol", "tcp").lower()
                if protocol == "udp":
                    # Parse as UDP endpoint
                    # URL format should be "ip:port" for UDP
                    from hydraping.models import UDPPortEndpoint, _is_ip_address, _validate_port

                    # Parse IP:port
                    if ":" in url:
                        parts = url.rsplit(":", 1)
                        if len(parts) == 2:
                            ip_part, port_part = parts
                            # Handle IPv6 bracket notation
                            if ip_part.startswith("[") and ip_part.endswith("]"):
                                ip_part = ip_part[1:-1]
                            if _is_ip_address(ip_part):
                                try:
                                    port = int(port_part)
                                    _validate_port(port)

                                    # Parse optional probe_data (hex string or ASCII string)
                                    probe_data = b""
                                    probe_hex = ep_config.get("probe_hex")
                                    probe_ascii = ep_config.get("probe_ascii")

                                    if probe_hex:
                                        # Parse hex string (e.g., "deadbeef")
                                        try:
                                            probe_data = bytes.fromhex(probe_hex)
                                        except ValueError as e:
                                            raise ValueError(
                                                f"Invalid hex probe_hex for {url}: {e}"
                                            ) from e
                                    elif probe_ascii:
                                        # Use ASCII string as-is
                                        probe_data = probe_ascii.encode("ascii")

                                    endpoint = UDPPortEndpoint(
                                        raw=url, ip=ip_part, port=port, probe_data=probe_data
                                    )
                                except ValueError as e:
                                    raise ValueError(f"Invalid UDP port in {url}: {e}") from e
                            else:
                                raise ValueError(
                                    f"UDP endpoints require IP address, got: {ip_part}"
                                )
                        else:
                            raise ValueError(
                                f"UDP endpoint must be in format 'ip:port', got: {url}"
                            )
                    else:
                        raise ValueError(f"UDP endpoint must include port, got: {url}")
                else:
                    # Regular endpoint (TCP/HTTP/ICMP)
                    endpoint = Endpoint.parse(url)

                # Set custom name if provided
                custom_name = ep_config.get("name")
                if custom_name:
                    endpoint.custom_name = custom_name

                # Set IP version preference if provided
                ip_version = ep_config.get("ip_version")
                if ip_version is not None:
                    if ip_version not in (4, 6):
                        raise ValueError(f"ip_version must be 4 or 6, got: {ip_version}")
                    endpoint.ip_version = ip_version

            else:
                raise ValueError(
                    f"Invalid endpoint format: {ep_config}. "
                    "Expected string or object with 'url' field."
                )
            endpoints.append(endpoint)

        if not endpoints:
            raise ValueError("No valid endpoints after parsing")

        # Parse DNS config
        dns_data = data.get("dns", {})
        dns = DNSConfig(custom_servers=dns_data.get("custom_servers", []))

        # Parse checks config
        checks_data = data.get("checks", {})
        checks = ChecksConfig(
            interval_seconds=checks_data.get("interval_seconds", 5.0),
            timeout_seconds=checks_data.get("timeout_seconds", 5.0),
            http_success_status_max=checks_data.get("http_success_status_max", 399),
        )

        # Validate timeout doesn't exceed interval
        if checks.timeout_seconds >= checks.interval_seconds:
            import warnings

            warnings.warn(
                f"timeout_seconds ({checks.timeout_seconds}s) should be less than "
                f"interval_seconds ({checks.interval_seconds}s) to avoid overlapping checks. "
                "This may lead to resource issues.",
                UserWarning,
                stacklevel=2,
            )

        # Parse UI config
        ui_data = data.get("ui", {})
        ui = UIConfig(graph_width=ui_data.get("graph_width", 0))

        return Config(endpoints=endpoints, dns=dns, checks=checks, ui=ui)


def get_default_config_path() -> Path:
    """Get the default configuration file path."""
    return Path.home() / ".config" / "hydraping" / "settings.toml"


def create_default_config(config_path: Path | None = None) -> Path:
    """Create a default configuration file."""
    if config_path is None:
        config_path = get_default_config_path()

    # Create directory if it doesn't exist
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Default configuration content
    default_config = """# HydraPing Configuration

[endpoints]
# List of endpoints to monitor
# Supported formats:
#   - Simple string: "8.8.8.8", "google.com", "https://example.com/"
#   - With custom name: { url = "8.8.8.8", name = "Google DNS" }
#   - UDP endpoint: { url = "1.1.1.1:53", protocol = "udp", name = "Cloudflare DNS (UDP)" }
#   - UDP with probe: { url = "1.1.1.1:53", protocol = "udp", probe_hex = "deadbeef" }
#   - UDP with ASCII:  { url = "1.1.1.1:123", protocol = "udp", probe_ascii = "hello" }
#   - IPv4/IPv6 preference: { url = "google.com", ip_version = 4, name = "Google (IPv4)" }
#
# Endpoint types:
#   - IPv4 address: "8.8.8.8"
#   - IPv6 address: "2001:4860:4860::8888"
#   - IPv4:port (TCP): "1.1.1.1:53"
#   - IPv6:port (TCP): "[2001:4860:4860::8888]:53" (note the brackets)
#   - UDP port: { url = "1.1.1.1:53", protocol = "udp" }
#   - Domain: "google.com"
#   - HTTP/HTTPS URL: "https://example.com/health"
targets = [
    { url = "1.1.1.1", name = "Cloudflare DNS" },
    { url = "8.8.8.8", name = "Google DNS" },
    "google.com",
]

[dns]
# Optional: Custom DNS servers to query against (in addition to system DNS)
custom_servers = []

[checks]
# How often to perform checks (in seconds)
interval_seconds = 5.0

# Timeout for individual checks (in seconds)
timeout_seconds = 5.0

# Maximum HTTP status code considered successful (default: 399)
# 399: Accept 2xx and 3xx (redirects) as success
# 299: Strict mode - only 2xx responses are successful
http_success_status_max = 399

[ui]
# Width of the latency graph (in characters)
# Set to 0 to auto-size based on terminal width
graph_width = 0
"""

    config_path.write_text(default_config)
    return config_path
