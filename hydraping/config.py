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

    interval_seconds: float = 1.0
    timeout_seconds: float = 5.0


@dataclass
class UIConfig:
    """UI display configuration."""

    graph_width: int = 60


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
        endpoint_strs = data.get("endpoints", {}).get("targets", [])
        if not endpoint_strs:
            raise ValueError("No endpoints configured in 'endpoints.targets'")

        endpoints = [Endpoint.parse(ep) for ep in endpoint_strs]

        # Parse DNS config
        dns_data = data.get("dns", {})
        dns = DNSConfig(custom_servers=dns_data.get("custom_servers", []))

        # Parse checks config
        checks_data = data.get("checks", {})
        checks = ChecksConfig(
            interval_seconds=checks_data.get("interval_seconds", 1.0),
            timeout_seconds=checks_data.get("timeout_seconds", 5.0),
        )

        # Parse UI config
        ui_data = data.get("ui", {})
        ui = UIConfig(graph_width=ui_data.get("graph_width", 60))

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
#   - IP address: "8.8.8.8"
#   - IP:port: "1.1.1.1:53"
#   - Domain: "google.com"
#   - HTTP/HTTPS URL: "https://example.com/health"
targets = [
    "8.8.8.8",
    "1.1.1.1",
    "google.com",
]

[dns]
# Optional: Custom DNS servers to query against (in addition to system DNS)
custom_servers = []

[checks]
# How often to perform checks (in seconds)
interval_seconds = 1.0

# Timeout for individual checks (in seconds)
timeout_seconds = 5.0

[ui]
# Width of the latency graph (in characters)
# Set to 0 to auto-size based on terminal width
graph_width = 0
"""

    config_path.write_text(default_config)
    return config_path
