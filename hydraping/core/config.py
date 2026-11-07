"""Configuration management for HydraPing."""

import os
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tomlkit
import tomlkit.items

from hydraping.core.models import CheckType, Endpoint

# The format this release reads. A file without ``version`` is the v1 format and gets migrated.
CONFIG_VERSION = 2


@dataclass
class DNSConfig:
    """DNS check configuration."""

    servers: list[str] = field(default_factory=list)


@dataclass
class ChecksConfig:
    """Probe timing.

    Probes start every ``probe_interval_seconds`` and may overlap: a probe that has not answered
    yet does not hold back the next one. ``bucket_seconds`` is the width of one graph column.
    """

    probe_interval_seconds: float = 1.0
    bucket_seconds: float = 5.0
    timeout_seconds: float = 5.0
    http_success_status_max: int = 399  # HTTP status codes <= this are considered success

    def __post_init__(self):
        for name in ("probe_interval_seconds", "bucket_seconds", "timeout_seconds"):
            value = getattr(self, name)
            if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"checks.{name} must be a positive number, got: {value!r}")


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
    def load(
        config_path: Path | None = None,
        on_migrated: Callable[[Path, Path], None] | None = None,
    ) -> "Config":
        """Load configuration from a TOML file.

        A v1 file (no ``version`` key) is migrated in place first; see ``migrate_v1_config``.
        ``on_migrated`` gets the file path and the backup path when that happens. The default
        prints one notice to stderr.
        """
        if config_path is None:
            config_path = get_default_config_path()
        if on_migrated is None:
            on_migrated = _print_migration_notice

        if not config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {config_path}\n"
                f"Run 'hydraping init' to create a default configuration."
            )

        with config_path.open("rb") as f:
            data = tomllib.load(f)

        if "version" not in data:
            backup_path = migrate_v1_config(config_path)
            on_migrated(config_path, backup_path)
            with config_path.open("rb") as f:
                data = tomllib.load(f)

        version = data["version"]
        if version != CONFIG_VERSION:
            raise ValueError(
                f"Unsupported config version {version!r} in {config_path}; "
                f"this version of hydraping reads version {CONFIG_VERSION}"
            )

        _reject_unknown_keys(data, config_path)

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
                    from hydraping.core.models import (
                        UDPPortEndpoint,
                        _is_ip_address,
                        _validate_port,
                    )

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

                # Set primary check type if provided
                primary_check = ep_config.get("primary_check_type")
                if primary_check:
                    # Parse check type from string
                    try:
                        check_type = CheckType(primary_check.lower())
                    except ValueError:
                        valid_types = ", ".join([ct.value for ct in CheckType])
                        raise ValueError(
                            f"Invalid primary_check_type '{primary_check}' for {url}. "
                            f"Valid values: {valid_types}"
                        ) from None

                    # Validate the check type is applicable to this endpoint
                    applicable_checks = endpoint.get_check_types()
                    if check_type not in applicable_checks:
                        applicable_str = ", ".join([ct.value for ct in applicable_checks])
                        raise ValueError(
                            f"primary_check_type '{primary_check}' is not applicable "
                            f"to endpoint {url}. This endpoint supports: {applicable_str}"
                        )

                    endpoint.primary_check_type = check_type

            else:
                raise ValueError(
                    f"Invalid endpoint format: {ep_config}. "
                    "Expected string or object with 'url' field."
                )
            endpoints.append(endpoint)

        if not endpoints:
            raise ValueError("No valid endpoints after parsing")

        dns = DNSConfig(servers=data.get("dns", {}).get("servers", []))

        checks_data = data.get("checks", {})
        defaults = ChecksConfig()
        checks = ChecksConfig(
            probe_interval_seconds=checks_data.get(
                "probe_interval_seconds", defaults.probe_interval_seconds
            ),
            bucket_seconds=checks_data.get("bucket_seconds", defaults.bucket_seconds),
            timeout_seconds=checks_data.get("timeout_seconds", defaults.timeout_seconds),
            http_success_status_max=checks_data.get(
                "http_success_status_max", defaults.http_success_status_max
            ),
        )

        # Parse UI config
        ui_data = data.get("ui", {})
        ui = UIConfig(graph_width=ui_data.get("graph_width", 0))

        return Config(endpoints=endpoints, dns=dns, checks=checks, ui=ui)


# Every key settings.toml may contain, per table. An unknown key is an error: a silently ignored
# typo (for example "intervl_seconds") would leave the default in effect without any hint.
_KNOWN_TABLE_KEYS: dict[str, set[str]] = {
    "endpoints": {"targets"},
    "dns": {"servers"},
    "checks": {
        "probe_interval_seconds",
        "bucket_seconds",
        "timeout_seconds",
        "http_success_status_max",
    },
    "ui": {"graph_width"},
}
# Top-level keys that are values, not tables.
_KNOWN_TOP_LEVEL_VALUES = {"version"}
_KNOWN_ENDPOINT_KEYS = {
    "url",
    "name",
    "protocol",
    "ip_version",
    "primary_check_type",
    "probe_hex",
    "probe_ascii",
}


def _reject_unknown_keys(data: dict, config_path: Path) -> None:
    """Raise ValueError naming the first key that settings.toml does not support."""

    def unknown(key_path: str) -> ValueError:
        return ValueError(f"Unknown key '{key_path}' in {config_path}")

    for table_name, table in data.items():
        if table_name in _KNOWN_TOP_LEVEL_VALUES:
            continue
        if table_name not in _KNOWN_TABLE_KEYS:
            raise unknown(table_name)
        if not isinstance(table, dict):
            raise ValueError(f"'{table_name}' must be a table in {config_path}")
        for key in table:
            if key not in _KNOWN_TABLE_KEYS[table_name]:
                raise unknown(f"{table_name}.{key}")

    targets = data.get("endpoints", {}).get("targets", [])
    if not isinstance(targets, list):
        # A string would otherwise be iterated as one endpoint per character.
        raise ValueError(f"'endpoints.targets' must be a list in {config_path}")
    for index, target in enumerate(targets):
        if isinstance(target, str):
            continue
        if not isinstance(target, dict):
            raise ValueError(
                f"'endpoints.targets[{index}]' must be a string or a table in {config_path}"
            )
        for key in target:
            if key not in _KNOWN_ENDPOINT_KEYS:
                raise unknown(f"endpoints.targets[{index}].{key}")


def _print_migration_notice(config_path: Path, backup_path: Path) -> None:
    print(
        f"Migrated {config_path} to the version {CONFIG_VERSION} format. "
        f"The original is kept at {backup_path}",
        file=sys.stderr,
    )


def migrate_v1_config(config_path: Path) -> Path:
    """Rewrite a v1 settings file as v2 in place and return the path of the backup.

    The backup is a copy named ``<name>.v1-backup-<YYYYmmdd-HHMMSS>`` in the same directory, and
    it is written before anything else. The rewrite goes through tomlkit, so comments and layout
    survive, and lands through a temporary file plus rename, so a crash leaves either the old file
    or the new one, never a half-written one. The v1 keys ``checks.interval_seconds`` and
    ``dns.custom_servers`` are renamed in place; ``version`` and ``checks.probe_interval_seconds``
    are added.

    Raises OSError when the directory cannot be written. Nothing is changed in that case.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = config_path.with_name(f"{config_path.name}.v1-backup-{stamp}")
    try:
        shutil.copy2(config_path, backup_path)
    except OSError as e:
        raise OSError(
            f"Cannot migrate {config_path} to the version {CONFIG_VERSION} format: the backup "
            f"{backup_path} could not be written ({e.strerror}). Make the directory writable, "
            "or migrate the file by hand: add 'version = 2', rename checks.interval_seconds to "
            "checks.bucket_seconds and dns.custom_servers to dns.servers."
        ) from e

    document = tomlkit.parse(config_path.read_text())
    document.add("version", CONFIG_VERSION)
    _rename_key(document, "checks", "interval_seconds", "bucket_seconds", config_path)
    _rename_key(document, "dns", "custom_servers", "servers", config_path)
    checks = document.get("checks")
    if checks is None:
        checks = tomlkit.table()
        document.add("checks", checks)
    if "probe_interval_seconds" not in checks:
        checks["probe_interval_seconds"] = ChecksConfig().probe_interval_seconds

    fd, temp_name = tempfile.mkstemp(dir=config_path.parent, prefix=f".{config_path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(tomlkit.dumps(document))
        shutil.copymode(config_path, temp_name)
        os.replace(temp_name, config_path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return backup_path


def _rename_key(
    document: tomlkit.TOMLDocument, table_name: str, old: str, new: str, config_path: Path
) -> None:
    table = document.get(table_name)
    if table is None or old not in table:
        return
    if new in table:
        raise ValueError(
            f"{config_path} has both {table_name}.{old} and {table_name}.{new}; "
            f"keep only {table_name}.{new}"
        )
    # tomlkit has no public rename. Assigning a new key would move it to the end of the table and
    # away from its comments; _replace keeps the position and the trailing comment.
    table.value._replace(old, new, table[old])


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
version = 2

[endpoints]
# List of endpoints to monitor
# Supported formats:
#   - Simple string: "8.8.8.8", "google.com", "https://example.com/"
#   - With custom name: { url = "8.8.8.8", name = "Google DNS" }
#   - UDP endpoint: { url = "1.1.1.1:53", protocol = "udp", name = "Cloudflare DNS (UDP)" }
#   - UDP with probe: { url = "1.1.1.1:53", protocol = "udp", probe_hex = "deadbeef" }
#   - UDP with ASCII:  { url = "1.1.1.1:123", protocol = "udp", probe_ascii = "hello" }
#   - IPv4/IPv6 preference: { url = "google.com", ip_version = 4, name = "Google (IPv4)" }
#   - Primary check type: { url = "example.com", primary_check_type = "tcp" }
#
# Endpoint types:
#   - IPv4 address: "8.8.8.8"
#   - IPv6 address: "2001:4860:4860::8888"
#   - IPv4:port (TCP): "1.1.1.1:53"
#   - IPv6:port (TCP): "[2001:4860:4860::8888]:53" (note the brackets)
#   - UDP port: { url = "1.1.1.1:53", protocol = "udp" }
#   - Domain: "google.com"
#   - HTTP/HTTPS URL: "https://example.com/health"
#
# Primary check type (optional):
#   Override which check is graphed/displayed (dns/icmp/tcp/udp/http)
#   All checks still run; only affects what's shown in graph and latency column
#   Useful when ICMP is blocked but you want to monitor TCP/HTTP connectivity
targets = [
    { url = "1.1.1.1", name = "Cloudflare DNS" },
    { url = "8.8.8.8", name = "Google DNS" },
    "google.com",
]

[dns]
# Optional: DNS servers to query instead of the system resolver
servers = []

[checks]
# How often a new probe starts (in seconds). Probes overlap: a slow one does not delay the next.
probe_interval_seconds = 1.0

# Width of one graph column (in seconds). The newest column keeps updating until its time is over.
bucket_seconds = 5.0

# Timeout for one probe (in seconds)
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
