"""Tests for configuration loading and parsing."""

import pytest

from hydraping.core.config import (
    ChecksConfig,
    Config,
    DNSConfig,
    UIConfig,
    create_default_config,
)
from hydraping.core.engine import Engine
from hydraping.core.models import (
    CheckType,
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
version = 2
[endpoints]
targets = [
    "8.8.8.8",
    "google.com",
    "https://example.com/"
]

[dns]
servers = []

[checks]
probe_interval_seconds = 1.0
bucket_seconds = 5.0
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
version = 2
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
version = 2
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
version = 2
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
version = 2
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
version = 2
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
version = 2
[endpoints]
targets = ["google.com"]

[checks]
http_success_status_max = 299
""")

        config = Config.load(config_file)
        assert config.checks.http_success_status_max == 299

    def test_load_config_with_adaptive_interval(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = ["192.0.2.1"]

[checks]
probe_interval_seconds = 0.5
probe_interval_max_seconds = 2.0
bucket_seconds = 2.0
adaptive_interval = false
""")

        checks = Config.load(config_file).checks
        assert checks.probe_interval_seconds == 0.5
        assert checks.probe_interval_max_seconds == 2.0
        assert checks.adaptive_interval is False

    def test_load_config_with_a_short_bucket_shortens_the_default_maximum_interval(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = ["192.0.2.1"]

[checks]
bucket_seconds = 2.0
""")

        assert Config.load(config_file).checks.probe_interval_max_seconds == 2.0

    def test_load_config_names_the_maximum_interval_that_exceeds_the_bucket(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = ["192.0.2.1"]

[checks]
bucket_seconds = 2.0
probe_interval_max_seconds = 3.0
""")

        with pytest.raises(
            ValueError,
            match=r"probe_interval_max_seconds must be at most checks.bucket_seconds \(2.0\), "
            r"got: 3.0",
        ):
            Config.load(config_file)

    def test_insecure_turns_off_certificate_verification_for_https(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = [
    { url = "https://router.example.internal/", insecure = true },
    { url = "https://example.com/", primary_check_type = "tls" },
    "https://example.com/other",
]
""")
        config = Config.load(config_file)
        insecure, tls_primary, default = config.endpoints
        assert insecure.insecure is True
        assert tls_primary.insecure is False
        assert tls_primary.primary_check_type == CheckType.TLS
        assert default.insecure is False

    def test_insecure_on_an_endpoint_without_tls_is_an_error(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = [{ url = "http://example.com/", insecure = true }]
""")
        with pytest.raises(ValueError, match="only https:// endpoints have a TLS layer"):
            Config.load(config_file)

    def test_expect_body_is_kept_on_http_targets_and_chain_hops(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = [
    { url = "https://192.0.2.1/cdn-cgi/trace", expect_body = "marker=on" },
    "http://example.com/",
]
[[chains]]
name = "VPN"
hops = [{ url = "http://example.com/health", expect_body = "ok" }]
""")
        config = Config.load(config_file)
        checked, plain = config.endpoints
        assert checked.expect_body == "marker=on"
        assert plain.expect_body is None
        assert config.chains[0].hops[0].expect_body == "ok"

    @pytest.mark.parametrize(
        "endpoint",
        [
            '{ url = "192.0.2.1", expect_body = "ok" }',
            '{ url = "192.0.2.1:443", expect_body = "ok" }',
            '{ url = "example.com", expect_body = "ok" }',
            '{ url = "192.0.2.1:53", protocol = "udp", expect_body = "ok" }',
        ],
    )
    def test_expect_body_on_an_endpoint_without_http_is_an_error(self, tmp_path, endpoint):
        config_file = tmp_path / "test.toml"
        config_file.write_text(f"version = 2\n[endpoints]\ntargets = [{endpoint}]\n")
        with pytest.raises(ValueError, match="only http:// and https:// endpoints have an HTTP"):
            Config.load(config_file)

    @pytest.mark.parametrize(
        "check", ['check = "dns", query = "example.com"', 'check = "tailscale"']
    )
    def test_expect_body_on_a_check_endpoint_is_an_error(self, tmp_path, check):
        config_file = tmp_path / "test.toml"
        config_file.write_text(
            f'version = 2\n[endpoints]\ntargets = [{{ url = "192.0.2.1", {check}, '
            'expect_body = "ok" }]\n'
        )
        with pytest.raises(ValueError, match="'expect_body' cannot be combined with check"):
            Config.load(config_file)

    @pytest.mark.parametrize("value", ['""', "1", "true", '["ok"]'])
    def test_expect_body_must_be_a_non_empty_string(self, tmp_path, value):
        config_file = tmp_path / "test.toml"
        config_file.write_text(
            "version = 2\n[endpoints]\n"
            f'targets = [{{ url = "https://example.com/", expect_body = {value} }}]\n'
        )
        with pytest.raises(ValueError, match="expect_body must be a non-empty string"):
            Config.load(config_file)

    def test_endpoints_with_different_expect_body_are_measured_separately(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = [
    { url = "https://192.0.2.1/cdn-cgi/trace", expect_body = "marker=on" },
    { url = "https://192.0.2.1/cdn-cgi/trace", name = "Same probe", expect_body = "marker=on" },
    "https://192.0.2.1/cdn-cgi/trace",
]
[[chains]]
name = "VPN"
hops = [{ url = "https://192.0.2.1/cdn-cgi/trace", expect_body = "marker=off" }]
""")
        engine = Engine(Config.load(config_file))

        assert engine.target_indices == [0, 0, 1]
        assert engine.chain_hop_indices == [[2]]
        assert [e.expect_body for e in engine.measured] == ["marker=on", None, "marker=off"]

    def _load_checks(self, tmp_path, checks_body: str) -> ChecksConfig:
        config_file = tmp_path / "test.toml"
        config_file.write_text(
            f'version = 2\n[endpoints]\ntargets = ["192.0.2.1"]\n[checks]\n{checks_body}\n'
        )
        return Config.load(config_file).checks

    def test_history_seconds_defaults_to_one_hour(self, tmp_path):
        assert self._load_checks(tmp_path, "timeout_seconds = 5.0").history_seconds == 3600

    def test_history_seconds_custom(self, tmp_path):
        assert self._load_checks(tmp_path, "history_seconds = 600").history_seconds == 600

    @pytest.mark.parametrize("value", ["0", "-60", "true", '"3600"'])
    def test_history_seconds_must_be_a_positive_number(self, tmp_path, value):
        with pytest.raises(
            ValueError, match="checks.history_seconds must be a positive number, got: "
        ):
            self._load_checks(tmp_path, f"history_seconds = {value}")

    def test_history_seconds_must_cover_one_bucket(self, tmp_path):
        with pytest.raises(
            ValueError,
            match=r"checks.history_seconds must be at least checks.bucket_seconds \(5.0\), "
            r"got: 4.0",
        ):
            self._load_checks(tmp_path, "history_seconds = 4.0")
        assert self._load_checks(tmp_path, "history_seconds = 5.0").history_seconds == 5.0

    def _load_ui(self, tmp_path, ui_body: str) -> UIConfig:
        config_file = tmp_path / "test.toml"
        config_file.write_text(
            f'version = 2\n[endpoints]\ntargets = ["192.0.2.1"]\n[ui]\n{ui_body}\n'
        )
        return Config.load(config_file).ui

    def test_tray_refresh_seconds_defaults_to_five(self, tmp_path):
        assert self._load_ui(tmp_path, "graph_width = 0").tray_refresh_seconds == 5.0

    def test_tray_refresh_seconds_custom(self, tmp_path):
        assert self._load_ui(tmp_path, "tray_refresh_seconds = 2.5").tray_refresh_seconds == 2.5

    @pytest.mark.parametrize("value", ["0", "-1.0", '"5"', "true"])
    def test_tray_refresh_seconds_must_be_a_positive_number(self, tmp_path, value):
        with pytest.raises(
            ValueError, match="ui.tray_refresh_seconds must be a positive number, got: "
        ):
            self._load_ui(tmp_path, f"tray_refresh_seconds = {value}")

    def test_tray_leaves_a_target_out_of_the_top_bar(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = [{ url = "192.0.2.1", tray = false }, "192.0.2.2"]
""")
        hidden, shown = Config.load(config_file).endpoints
        assert hidden.tray is False
        assert shown.tray is True

    def test_tray_must_be_a_bool(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = [{ url = "192.0.2.1", tray = "no" }]
""")
        with pytest.raises(ValueError, match="tray must be true or false"):
            Config.load(config_file)

    def test_tray_on_a_chain_hop_is_an_error(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[[chains]]
name = "Company"
hops = [{ url = "192.0.2.1", tray = false }]
""")
        with pytest.raises(ValueError, match="tray has no effect on the chain hop 192.0.2.1"):
            Config.load(config_file)

    def test_insecure_must_be_a_bool(self, tmp_path):
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = [{ url = "https://example.com/", insecure = "yes" }]
""")
        with pytest.raises(ValueError, match="insecure must be true or false"):
            Config.load(config_file)

    def test_invalid_config_no_endpoints(self, tmp_path):
        """Test that config without endpoints raises error."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = []
""")

        with pytest.raises(ValueError, match="No endpoints configured"):
            Config.load(config_file)

    def test_invalid_config_missing_url(self, tmp_path):
        """Test that endpoint object without url raises error."""
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
version = 2
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
version = 2
[endpoints]
targets = [
    { url = "1.1.1.1:53", protocol = "udp", probe_hex = "invalid" },
]
""")

        with pytest.raises(ValueError, match="Invalid hex probe_hex"):
            Config.load(config_file)


class TestUnknownKeys:
    """Test that keys settings.toml does not support stop loading with the key path."""

    def _write(self, tmp_path, body: str):
        config_file = tmp_path / "settings.toml"
        config_file.write_text('version = 2\n[endpoints]\ntargets = ["192.0.2.1"]\n' + body)
        return config_file

    def test_unknown_top_level_table(self, tmp_path):
        config_file = self._write(tmp_path, "\n[graphs]\nwidth = 3\n")
        with pytest.raises(ValueError) as error:
            Config.load(config_file)
        assert str(error.value) == f"Unknown key 'graphs' in {config_file}"

    def test_unknown_key_in_checks(self, tmp_path):
        config_file = self._write(tmp_path, "\n[checks]\nintervl_seconds = 1.0\n")
        with pytest.raises(ValueError) as error:
            Config.load(config_file)
        assert str(error.value) == f"Unknown key 'checks.intervl_seconds' in {config_file}"

    def test_unknown_key_in_endpoint_object(self, tmp_path):
        config_file = tmp_path / "settings.toml"
        config_file.write_text("""
version = 2
[endpoints]
targets = [
    "192.0.2.1",
    { url = "example.com", nmae = "Example" },
]
""")
        with pytest.raises(ValueError) as error:
            Config.load(config_file)
        assert str(error.value) == f"Unknown key 'endpoints.targets[1].nmae' in {config_file}"


class TestConfigVersion:
    def test_unsupported_version_is_an_error(self, tmp_path):
        config_file = tmp_path / "settings.toml"
        config_file.write_text('version = 3\n[endpoints]\ntargets = ["192.0.2.1"]\n')
        with pytest.raises(ValueError, match="Unsupported config version 3"):
            Config.load(config_file)

    def test_v1_keys_in_a_v2_file_are_unknown(self, tmp_path):
        config_file = tmp_path / "settings.toml"
        config_file.write_text(
            'version = 2\n[endpoints]\ntargets = ["192.0.2.1"]\n[checks]\ninterval_seconds = 5.0\n'
        )
        with pytest.raises(ValueError) as error:
            Config.load(config_file)
        assert str(error.value) == f"Unknown key 'checks.interval_seconds' in {config_file}"


class TestConfigShape:
    """Test that values of the wrong kind stop loading with the key path."""

    def test_table_that_is_not_a_table(self, tmp_path):
        config_file = tmp_path / "settings.toml"
        config_file.write_text('version = 2\nchecks = 5\n[endpoints]\ntargets = ["192.0.2.1"]\n')
        with pytest.raises(ValueError) as error:
            Config.load(config_file)
        assert str(error.value) == f"'checks' must be a table in {config_file}"

    def test_targets_that_is_not_a_list(self, tmp_path):
        config_file = tmp_path / "settings.toml"
        config_file.write_text('version = 2\n[endpoints]\ntargets = "192.0.2.1"\n')
        with pytest.raises(ValueError) as error:
            Config.load(config_file)
        assert str(error.value) == f"'endpoints.targets' must be a list in {config_file}"

    def test_target_that_is_neither_string_nor_table(self, tmp_path):
        config_file = tmp_path / "settings.toml"
        config_file.write_text('version = 2\n[endpoints]\ntargets = ["192.0.2.1", 53]\n')
        with pytest.raises(ValueError) as error:
            Config.load(config_file)
        assert str(error.value) == (
            f"'endpoints.targets[1]' must be a string or a table in {config_file}"
        )


class TestConfigClasses:
    """Test configuration dataclasses."""

    def test_dns_config_defaults(self):
        """Test DNSConfig default values."""
        dns = DNSConfig()
        assert dns.servers == []

    def test_checks_config_defaults(self):
        """Test ChecksConfig default values."""
        checks = ChecksConfig()
        assert checks.probe_interval_seconds == 1.0
        assert checks.probe_interval_max_seconds == 5.0
        assert checks.adaptive_interval is True
        assert checks.bucket_seconds == 5.0
        assert checks.timeout_seconds == 5.0
        assert checks.history_seconds == 3600
        assert checks.http_success_status_max == 399

    def test_checks_config_rejects_non_positive_timing(self):
        with pytest.raises(ValueError, match="checks.bucket_seconds must be a positive number"):
            ChecksConfig(bucket_seconds=0)
        with pytest.raises(
            ValueError, match="checks.probe_interval_max_seconds must be a positive number"
        ):
            ChecksConfig(probe_interval_max_seconds=-1.0)

    def test_checks_config_keeps_the_maximum_interval_between_the_minimum_and_the_bucket(self):
        with pytest.raises(
            ValueError,
            match=r"probe_interval_max_seconds must be at least checks.probe_interval_seconds "
            r"\(2.0\), got: 1.5",
        ):
            ChecksConfig(probe_interval_seconds=2.0, probe_interval_max_seconds=1.5)
        with pytest.raises(
            ValueError,
            match=r"probe_interval_max_seconds must be at most checks.bucket_seconds \(2.0\), "
            r"got: 5.0",
        ):
            ChecksConfig(bucket_seconds=2.0, probe_interval_max_seconds=5.0)
        assert ChecksConfig(probe_interval_max_seconds=1.0).probe_interval_max_seconds == 1.0
        assert ChecksConfig(probe_interval_max_seconds=5.0).probe_interval_max_seconds == 5.0

    def test_checks_config_maximum_interval_defaults_to_the_shorter_of_five_and_the_bucket(self):
        assert ChecksConfig(bucket_seconds=3.0).probe_interval_max_seconds == 3.0
        assert ChecksConfig(bucket_seconds=30.0).probe_interval_max_seconds == 5.0
        with pytest.raises(ValueError, match="checks.bucket_seconds must be a positive number"):
            ChecksConfig(bucket_seconds="5")

    def test_checks_config_adaptive_interval_must_be_a_bool(self):
        with pytest.raises(ValueError, match="checks.adaptive_interval must be true or false"):
            ChecksConfig(adaptive_interval="yes")

    def test_ui_config_defaults(self):
        """Test UIConfig default values."""
        ui = UIConfig()
        assert ui.graph_width == 0
        assert ui.tray_refresh_seconds == 5.0


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
