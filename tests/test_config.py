"""Tests for configuration loading and parsing."""

import pytest

from hydraping.config import (
    ChecksConfig,
    Config,
    DNSConfig,
    UIConfig,
    create_default_config,
)
from hydraping.models import (
    DomainEndpoint,
    HTTPEndpoint,
    IPEndpoint,
    UDPPortEndpoint,
)


class TestConfigLoading:
    """Test loading configuration from TOML files."""

    def test_load_simple_config(self, tmp_path):
        """Test loading a simple config with basic endpoints."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[endpoints]
targets = [
    "8.8.8.8",
    "google.com",
    "https://example.com/"
]

[dns]
custom_servers = []

[checks]
interval_seconds = 5.0
timeout_seconds = 5.0

[ui]
graph_width = 0
""")

        config = Config.load(config_file)
        assert len(config.endpoints) == 3
        assert isinstance(config.endpoints[0], IPEndpoint)
        assert isinstance(config.endpoints[1], DomainEndpoint)
        assert isinstance(config.endpoints[2], HTTPEndpoint)

    def test_load_config_with_custom_names(self, tmp_path):
        """Test loading config with custom endpoint names."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[endpoints]
targets = [
    { url = "8.8.8.8", name = "Google DNS" },
    { url = "1.1.1.1", name = "Cloudflare DNS" },
]
""")

        config = Config.load(config_file)
        assert config.endpoints[0].custom_name == "Google DNS"
        assert config.endpoints[1].custom_name == "Cloudflare DNS"

    def test_load_config_with_udp_endpoints(self, tmp_path):
        """Test loading UDP endpoints from config."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[endpoints]
targets = [
    { url = "1.1.1.1:53", protocol = "udp", name = "Cloudflare DNS (UDP)" },
]
""")

        config = Config.load(config_file)
        assert len(config.endpoints) == 1
        assert isinstance(config.endpoints[0], UDPPortEndpoint)
        assert config.endpoints[0].ip == "1.1.1.1"
        assert config.endpoints[0].port == 53

    def test_load_config_with_udp_probe_hex(self, tmp_path):
        """Test loading UDP endpoint with hex probe data."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[endpoints]
targets = [
    { url = "1.1.1.1:53", protocol = "udp", probe_hex = "deadbeef" },
]
""")

        config = Config.load(config_file)
        endpoint = config.endpoints[0]
        assert isinstance(endpoint, UDPPortEndpoint)
        assert endpoint.probe_data == b"\xde\xad\xbe\xef"

    def test_load_config_with_udp_probe_ascii(self, tmp_path):
        """Test loading UDP endpoint with ASCII probe data."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[endpoints]
targets = [
    { url = "1.1.1.1:123", protocol = "udp", probe_ascii = "hello" },
]
""")

        config = Config.load(config_file)
        endpoint = config.endpoints[0]
        assert isinstance(endpoint, UDPPortEndpoint)
        assert endpoint.probe_data == b"hello"

    def test_load_config_with_ip_version(self, tmp_path):
        """Test loading config with IP version preference."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[endpoints]
targets = [
    { url = "google.com", ip_version = 4 },
    { url = "google.com", ip_version = 6 },
]
""")

        config = Config.load(config_file)
        assert config.endpoints[0].ip_version == 4
        assert config.endpoints[1].ip_version == 6

    def test_load_config_with_http_success_status(self, tmp_path):
        """Test loading config with HTTP success status threshold."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[endpoints]
targets = ["google.com"]

[checks]
http_success_status_max = 299
""")

        config = Config.load(config_file)
        assert config.checks.http_success_status_max == 299

    def test_invalid_config_no_endpoints(self, tmp_path):
        """Test that config without endpoints raises error."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[endpoints]
targets = []
""")

        with pytest.raises(ValueError, match="No endpoints configured"):
            Config.load(config_file)

    def test_invalid_config_missing_url(self, tmp_path):
        """Test that endpoint object without url raises error."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[endpoints]
targets = [
    { name = "Missing URL" }
]
""")

        with pytest.raises(ValueError, match="missing 'url' field"):
            Config.load(config_file)

    def test_invalid_udp_probe_hex(self, tmp_path):
        """Test that invalid hex in probe_hex raises error."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[endpoints]
targets = [
    { url = "1.1.1.1:53", protocol = "udp", probe_hex = "invalid" },
]
""")

        with pytest.raises(ValueError, match="Invalid hex probe_hex"):
            Config.load(config_file)


class TestConfigClasses:
    """Test configuration dataclasses."""

    def test_dns_config_defaults(self):
        """Test DNSConfig default values."""
        dns = DNSConfig()
        assert dns.custom_servers == []

    def test_checks_config_defaults(self):
        """Test ChecksConfig default values."""
        checks = ChecksConfig()
        assert checks.interval_seconds == 5.0
        assert checks.timeout_seconds == 5.0
        assert checks.http_success_status_max == 399

    def test_ui_config_defaults(self):
        """Test UIConfig default values."""
        ui = UIConfig()
        assert ui.graph_width == 0


class TestCreateDefaultConfig:
    """Test default config file creation."""

    def test_create_default_config(self, tmp_path):
        """Test creating default config file."""
        config_path = tmp_path / "settings.toml"
        created_path = create_default_config(config_path)

        assert created_path.exists()
        assert created_path == config_path

        # Verify it can be loaded
        config = Config.load(created_path)
        assert len(config.endpoints) > 0

    def test_create_default_config_creates_directory(self, tmp_path):
        """Test that creating config creates parent directories."""
        config_path = tmp_path / "subdir" / "config" / "settings.toml"
        created_path = create_default_config(config_path)

        assert created_path.exists()
        assert created_path.parent.exists()
