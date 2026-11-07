"""Tests for core data models and result history."""

import pytest

from hydraping.core.models import (
    CHECK_TYPE_PRIORITY,
    CheckResult,
    CheckType,
    DomainEndpoint,
    Endpoint,
    HTTPEndpoint,
    IPEndpoint,
    IPPortEndpoint,
    LayerKey,
    Measurement,
    UDPPortEndpoint,
)


class TestEndpointParsing:
    """Test Endpoint.parse() method."""

    def test_parse_ipv4_address(self):
        """Test parsing plain IPv4 address."""
        endpoint = Endpoint.parse("8.8.8.8")
        assert isinstance(endpoint, IPEndpoint)
        assert endpoint.ip == "8.8.8.8"
        assert endpoint.raw == "8.8.8.8"

    def test_parse_ipv6_address(self):
        """Test parsing plain IPv6 address."""
        endpoint = Endpoint.parse("2001:4860:4860::8888")
        assert isinstance(endpoint, IPEndpoint)
        assert endpoint.ip == "2001:4860:4860::8888"

    def test_parse_ipv4_port(self):
        """Test parsing IPv4:port."""
        endpoint = Endpoint.parse("1.1.1.1:53")
        assert isinstance(endpoint, IPPortEndpoint)
        assert endpoint.ip == "1.1.1.1"
        assert endpoint.port == 53

    def test_parse_ipv6_port_bracket_notation(self):
        """Test parsing [IPv6]:port with brackets."""
        endpoint = Endpoint.parse("[2001:4860:4860::8888]:53")
        assert isinstance(endpoint, IPPortEndpoint)
        assert endpoint.ip == "2001:4860:4860::8888"
        assert endpoint.port == 53

    def test_parse_domain(self):
        """Test parsing domain name."""
        endpoint = Endpoint.parse("google.com")
        assert isinstance(endpoint, DomainEndpoint)
        assert endpoint.domain == "google.com"
        assert endpoint.port == 80  # Default port
        assert endpoint.port_specified is False

    def test_parse_domain_port(self):
        """Test parsing domain:port."""
        endpoint = Endpoint.parse("example.com:8080")
        assert isinstance(endpoint, DomainEndpoint)
        assert endpoint.domain == "example.com"
        assert endpoint.port == 8080
        assert endpoint.port_specified is True

    def test_parse_domain_port_common_ports(self):
        """Test parsing domain with common ports."""
        # Port 443
        endpoint = Endpoint.parse("api.example.com:443")
        assert isinstance(endpoint, DomainEndpoint)
        assert endpoint.domain == "api.example.com"
        assert endpoint.port == 443
        assert endpoint.port_specified is True

        # Port 3000
        endpoint = Endpoint.parse("localhost:3000")
        assert isinstance(endpoint, DomainEndpoint)
        assert endpoint.domain == "localhost"
        assert endpoint.port == 3000
        assert endpoint.port_specified is True

    def test_parse_http_url(self):
        """Test parsing HTTP URL."""
        endpoint = Endpoint.parse("http://example.com/path")
        assert isinstance(endpoint, HTTPEndpoint)
        assert endpoint.scheme == "http"
        assert endpoint.host == "example.com"
        assert endpoint.port == 80
        assert endpoint.path == "/path"

    def test_parse_https_url(self):
        """Test parsing HTTPS URL."""
        endpoint = Endpoint.parse("https://example.com:8443/api")
        assert isinstance(endpoint, HTTPEndpoint)
        assert endpoint.scheme == "https"
        assert endpoint.host == "example.com"
        assert endpoint.port == 8443
        assert endpoint.path == "/api"


class TestEndpointDisplayNames:
    """Test endpoint display name formatting."""

    def test_ipv4_port_display_name(self):
        """Test IPv4:port display name."""
        endpoint = IPPortEndpoint(raw="1.1.1.1:53", ip="1.1.1.1", port=53)
        assert endpoint.display_name == "1.1.1.1:53"

    def test_ipv6_port_display_name_with_brackets(self):
        """Test IPv6:port displays with bracket notation."""
        endpoint = IPPortEndpoint(raw="[2001:db8::1]:80", ip="2001:db8::1", port=80)
        assert endpoint.display_name == "[2001:db8::1]:80"

    def test_udp_port_display_name_ipv4(self):
        """Test UDP port display name for IPv4."""
        endpoint = UDPPortEndpoint(raw="1.1.1.1:53", ip="1.1.1.1", port=53)
        assert endpoint.display_name == "1.1.1.1:53 (UDP)"

    def test_udp_port_display_name_ipv6(self):
        """Test UDP port display name for IPv6 with brackets."""
        endpoint = UDPPortEndpoint(raw="[2001:db8::1]:53", ip="2001:db8::1", port=53)
        assert endpoint.display_name == "[2001:db8::1]:53 (UDP)"

    def test_domain_default_port_display_name(self):
        """Test domain with default port (80) doesn't show port."""
        endpoint = DomainEndpoint(raw="example.com", domain="example.com", port=80)
        assert endpoint.display_name == "example.com"

    def test_domain_custom_port_display_name(self):
        """Test domain with custom port shows port."""
        endpoint = DomainEndpoint(raw="example.com:8080", domain="example.com", port=8080)
        assert endpoint.display_name == "example.com:8080"

    def test_custom_name_overrides_default(self):
        """Test custom_name overrides default formatting."""
        endpoint = IPEndpoint(raw="8.8.8.8", ip="8.8.8.8", custom_name="Google DNS")
        assert endpoint.display_name == "Google DNS"


class TestPrimaryCheckType:
    """Test default primary check type selection."""

    def test_ipportendpoint_defaults_to_tcp(self):
        """Test IPPortEndpoint uses TCP as primary check by default."""
        endpoint = IPPortEndpoint(raw="1.1.1.1:8080", ip="1.1.1.1", port=8080)
        assert endpoint.get_primary_check_type() == CheckType.TCP

    def test_domain_without_explicit_port_uses_icmp(self):
        """Test DomainEndpoint without explicit port uses ICMP as primary."""
        endpoint = DomainEndpoint(
            raw="example.com", domain="example.com", port=80, port_specified=False
        )
        assert endpoint.get_primary_check_type() == CheckType.ICMP

    def test_domain_explicit_port_80_uses_tcp(self):
        """Test DomainEndpoint with explicit port 80 uses TCP as primary."""
        endpoint = DomainEndpoint(
            raw="example.com:80", domain="example.com", port=80, port_specified=True
        )
        assert endpoint.get_primary_check_type() == CheckType.TCP

    def test_domain_custom_port_uses_tcp(self):
        """Test DomainEndpoint with custom port uses TCP as primary."""
        endpoint = DomainEndpoint(
            raw="example.com:8080", domain="example.com", port=8080, port_specified=True
        )
        assert endpoint.get_primary_check_type() == CheckType.TCP

    def test_domain_port_443_uses_tcp(self):
        """Test DomainEndpoint with port 443 uses TCP as primary."""
        endpoint = DomainEndpoint(
            raw="example.com:443", domain="example.com", port=443, port_specified=True
        )
        assert endpoint.get_primary_check_type() == CheckType.TCP

    def test_httpendpoint_defaults_to_http(self):
        """Test HTTPEndpoint uses HTTP as primary check by default."""
        endpoint = HTTPEndpoint.from_string("https://example.com")
        assert endpoint.get_primary_check_type() == CheckType.HTTP

    def test_primary_layer_carries_the_port_only_for_tcp(self):
        assert Endpoint.parse("192.0.2.1").get_primary_layer() == LayerKey(CheckType.ICMP)
        assert Endpoint.parse("192.0.2.1:22").get_primary_layer() == LayerKey(CheckType.TCP, 22)
        assert Endpoint.parse("example.com:8080").get_primary_layer() == LayerKey(
            CheckType.TCP, 8080
        )
        https = Endpoint.parse("https://example.com:8443/")
        assert https.get_primary_layer() == LayerKey(CheckType.HTTP)
        https.primary_check_type = CheckType.TCP
        assert https.get_primary_layer() == LayerKey(CheckType.TCP, 8443)

    def test_domain_without_a_port_probes_80_and_443_and_graphs_443_for_tcp(self):
        endpoint = Endpoint.parse("example.com")
        assert endpoint.tcp_ports() == [80, 443]
        assert endpoint.get_primary_layer() == LayerKey(CheckType.ICMP)
        endpoint.primary_check_type = CheckType.TCP
        assert endpoint.get_primary_layer() == LayerKey(CheckType.TCP, 443)

    def test_layer_key_of_a_measurement_splits_only_tcp_by_port(self):
        tcp = Measurement(check_type=CheckType.TCP, success=True, latency_ms=1.0, port=443)
        tls = Measurement(check_type=CheckType.TLS, success=True, latency_ms=1.0, port=443)
        assert tcp.layer_key == LayerKey(CheckType.TCP, 443)
        assert tls.layer_key == LayerKey(CheckType.TLS)
        assert LayerKey(CheckType.TCP, 443).label == "TCP:443"
        assert LayerKey(CheckType.DNS).label == "DNS"

    def test_only_https_endpoints_have_a_tls_layer(self):
        https = HTTPEndpoint.from_string("https://example.com/")
        http = HTTPEndpoint.from_string("http://example.com/")
        assert https.uses_tls and not http.uses_tls
        assert https.get_check_types() == [
            CheckType.DNS,
            CheckType.ICMP,
            CheckType.TCP,
            CheckType.TLS,
            CheckType.HTTP,
        ]
        assert http.get_check_types() == [
            CheckType.DNS,
            CheckType.ICMP,
            CheckType.TCP,
            CheckType.HTTP,
        ]

    def test_https_path_keeps_the_query_string(self):
        endpoint = HTTPEndpoint.from_string("https://example.com/health?probe=1")
        assert endpoint.path == "/health?probe=1"


class TestCheckResult:
    """Test CheckResult validation."""

    def test_from_measurement_keeps_every_field(self):
        measurement = Measurement(
            check_type=CheckType.TCP,
            success=True,
            latency_ms=3.5,
            port=443,
            resolved_ip="192.0.2.1",
        )
        result = CheckResult.from_measurement(measurement, started_at=10.0, finished_at=10.1)
        assert result.check_type == CheckType.TCP
        assert result.latency_ms == 3.5
        assert result.port == 443
        assert result.resolved_ip == "192.0.2.1"
        assert (result.started_at, result.finished_at) == (10.0, 10.1)

    def test_successful_check_requires_latency(self):
        """Test that successful checks must have latency."""
        with pytest.raises(ValueError, match="Successful check must have latency"):
            CheckResult(
                started_at=1000.0,
                finished_at=1000.5,
                check_type=CheckType.ICMP,
                success=True,
                latency_ms=None,  # Missing latency
            )

    def test_failed_check_requires_error_message(self):
        """Test that failed checks must have error message."""
        with pytest.raises(ValueError, match="Failed check must have error message"):
            CheckResult(
                started_at=1000.0,
                finished_at=1000.5,
                check_type=CheckType.ICMP,
                success=False,
                error_message=None,  # Missing error
            )

    def test_valid_successful_check(self):
        """Test creating valid successful check."""
        result = CheckResult(
            started_at=1000.0,
            finished_at=1000.5,
            check_type=CheckType.ICMP,
            success=True,
            latency_ms=10.5,
        )
        assert result.success
        assert result.latency_ms == 10.5

    def test_skipped_check_names_the_layer_that_failed(self):
        tcp = Measurement(check_type=CheckType.TCP, success=False, error_message="refused")
        http = Measurement.skipped_after(CheckType.HTTP, tcp)
        assert http.skipped and not http.success
        assert http.blocked_by == CheckType.TCP
        assert http.failed_layer == CheckType.TCP
        assert tcp.failed_layer == CheckType.TCP
        ok = Measurement(check_type=CheckType.TCP, success=True, latency_ms=1.0)
        assert ok.failed_layer is None

        result = CheckResult.from_measurement(http, started_at=1.0, finished_at=1.1)
        assert result.skipped and result.blocked_by == CheckType.TCP

    def test_skipped_check_must_be_consistent(self):
        with pytest.raises(ValueError, match="Skipped check"):
            Measurement(check_type=CheckType.HTTP, success=False, error_message="x", skipped=True)
        with pytest.raises(ValueError, match="Only a skipped check"):
            Measurement(
                check_type=CheckType.HTTP,
                success=False,
                error_message="x",
                blocked_by=CheckType.TCP,
            )

    def test_valid_failed_check(self):
        """Test creating valid failed check."""
        result = CheckResult(
            started_at=1000.0,
            finished_at=1000.5,
            check_type=CheckType.ICMP,
            success=False,
            error_message="Timeout",
        )
        assert not result.success
        assert result.error_message == "Timeout"


class TestCheckTypePriority:
    def test_check_type_priority_order(self):
        """Test that CHECK_TYPE_PRIORITY is correctly ordered."""
        assert CHECK_TYPE_PRIORITY == [
            CheckType.HTTP,
            CheckType.TLS,
            CheckType.TCP,
            CheckType.UDP,
            CheckType.TAILSCALE,
            CheckType.DNS,
            CheckType.ICMP,
        ]


class TestUDPProbeData:
    """Test UDP probe data configuration."""

    def test_udp_endpoint_default_empty_probe(self):
        """Test that UDP endpoints default to empty probe."""
        endpoint = UDPPortEndpoint(raw="1.1.1.1:53", ip="1.1.1.1", port=53)
        assert endpoint.probe_data == b""

    def test_udp_endpoint_custom_probe(self):
        """Test UDP endpoint with custom probe data."""
        probe = b"\xde\xad\xbe\xef"
        endpoint = UDPPortEndpoint(raw="1.1.1.1:53", ip="1.1.1.1", port=53, probe_data=probe)
        assert endpoint.probe_data == probe
