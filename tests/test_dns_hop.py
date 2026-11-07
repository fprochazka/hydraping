"""Tests for the DNS hop check: random names against a localhost DNS server over UDP and TCP."""

import asyncio
import re
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import dns.asyncquery
import dns.message
import dns.rcode
import dns.rrset
import pytest

from hydraping.core.checkers.dns import DNSChecker, random_query_name
from hydraping.core.config import Config
from hydraping.core.engine import Engine
from hydraping.core.models import CheckType, DNSServerEndpoint

ZONE = "example.internal"


class FakeDNS:
    """Answers DNS queries the way ``mode`` says: "answer", "nxdomain", "refused", "servfail",
    or "silent", which never answers. Records every name it was asked for."""

    def __init__(self, mode: str):
        self.mode = mode
        self.names: list[str] = []

    def respond(self, wire: bytes) -> bytes | None:
        query = dns.message.from_wire(wire)
        name = query.question[0].name
        self.names.append(name.to_text())
        if self.mode == "silent":
            return None
        response = dns.message.make_response(query)
        if self.mode == "answer":
            response.answer.append(dns.rrset.from_text(name, 60, "IN", "A", "192.0.2.10"))
        else:
            response.set_rcode(
                {
                    "nxdomain": dns.rcode.NXDOMAIN,
                    "refused": dns.rcode.REFUSED,
                    "servfail": dns.rcode.SERVFAIL,
                }[self.mode]
            )
        return response.to_wire()


class UDPServer(asyncio.DatagramProtocol):
    def __init__(self, fake: FakeDNS):
        self.fake = fake
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        response = self.fake.respond(data)
        if response is not None:
            assert self.transport is not None
            self.transport.sendto(response, addr)


@asynccontextmanager
async def dns_server(mode: str, transport: str) -> AsyncIterator[tuple[int, FakeDNS]]:
    """Run a fake DNS server on 127.0.0.1 and yield its port and the fake."""
    fake = FakeDNS(mode)
    if transport == "udp":
        loop = asyncio.get_running_loop()
        udp, _ = await loop.create_datagram_endpoint(
            lambda: UDPServer(fake), local_addr=("127.0.0.1", 0)
        )
        try:
            yield udp.get_extra_info("sockname")[1], fake
        finally:
            udp.close()
        return

    handlers: set[asyncio.Task] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            (length,) = struct.unpack("!H", await reader.readexactly(2))
            response = fake.respond(await reader.readexactly(length))
            if response is None:
                await reader.read()  # Hold the connection open without answering.
            else:
                writer.write(struct.pack("!H", len(response)) + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], fake
    finally:
        server.close()
        # Python 3.11 does not wait for open connections in wait_closed().
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await server.wait_closed()


TRANSPORTS = pytest.mark.parametrize("transport", ["udp", "tcp"])


class TestRandomQueryName:
    def test_name_is_a_random_label_under_the_zone(self):
        names = {random_query_name("example.com") for _ in range(20)}

        assert len(names) == 20
        assert all(re.fullmatch(r"hp-[a-z]{8}\.example\.com\.", name) for name in names)

    def test_dots_around_the_zone_are_ignored(self):
        assert random_query_name(".example.com.").endswith(".example.com.")


class TestCheckServer:
    @TRANSPORTS
    @pytest.mark.parametrize("mode", ["answer", "nxdomain"])
    async def test_answer_and_nxdomain_are_success(self, transport, mode):
        async with dns_server(mode, transport) as (port, fake):
            checker = DNSChecker(timeout=2.0)
            first = await checker.check_server("127.0.0.1", ZONE, port=port, transport=transport)
            second = await checker.check_server("127.0.0.1", ZONE, port=port, transport=transport)

        assert first.success and second.success, (first.error_message, second.error_message)
        assert first.check_type == CheckType.DNS
        assert first.latency_ms is not None and first.latency_ms >= 0
        assert len(fake.names) == 2 and fake.names[0] != fake.names[1]
        assert all(name.endswith(f".{ZONE}.") for name in fake.names)

    @TRANSPORTS
    @pytest.mark.parametrize(("mode", "error"), [("refused", "REFUSED"), ("servfail", "SERVFAIL")])
    async def test_error_codes_are_failures(self, transport, mode, error):
        async with dns_server(mode, transport) as (port, _):
            result = await DNSChecker(timeout=2.0).check_server(
                "127.0.0.1", ZONE, port=port, transport=transport
            )

        assert not result.success
        assert result.error_message == f"{error} from 127.0.0.1"

    @TRANSPORTS
    async def test_no_answer_is_a_timeout_failure(self, transport):
        async with dns_server("silent", transport) as (port, _):
            result = await DNSChecker(timeout=0.3).check_server(
                "127.0.0.1", ZONE, port=port, transport=transport
            )

        assert not result.success
        assert result.error_message == "No answer from 127.0.0.1 within 0.3s"

    async def test_error_without_a_message_is_named_by_its_type(self, monkeypatch):
        async def closed_without_reason(*args, **kwargs):
            raise EOFError()

        monkeypatch.setattr(dns.asyncquery, "udp", closed_without_reason)
        result = await DNSChecker(timeout=0.3).check_server("127.0.0.1", ZONE)

        assert not result.success
        assert result.error_message == "DNS query to 127.0.0.1 failed: EOFError"

    async def test_engine_probe_queries_the_configured_server(self):
        async with dns_server("nxdomain", "udp") as (port, fake):
            endpoint = DNSServerEndpoint(raw="dns", ip="127.0.0.1", port=port, zone=ZONE)
            engine = Engine(Config(endpoints=[endpoint]))
            [measurement] = await engine._dns_server_probe(endpoint)()

        assert measurement.success, measurement.error_message
        assert len(fake.names) == 1
        assert endpoint.get_primary_layer().check_type == CheckType.DNS
        assert [s.name for s in engine.schedules] == ["dns icmp", "dns dns"]


class TestDNSHopConfig:
    def load(self, tmp_path, target: str) -> Config:
        path = tmp_path / "settings.toml"
        path.write_text(f"version = 2\n[endpoints]\ntargets = [{target}]\n")
        return Config.load(path)

    def test_dns_hop_with_defaults(self, tmp_path):
        config = self.load(tmp_path, '{ url = "192.0.2.53", check = "dns", query = "example.com" }')

        [endpoint] = config.endpoints
        assert endpoint == DNSServerEndpoint(raw="192.0.2.53", ip="192.0.2.53", zone="example.com")
        assert endpoint.port == 53 and endpoint.transport == "udp"

    def test_port_and_tcp_transport(self, tmp_path):
        config = self.load(
            tmp_path,
            '{ url = "[2001:db8::53]:5353", check = "dns", query = "example.com", '
            'transport = "tcp" }',
        )

        [endpoint] = config.endpoints
        assert (endpoint.ip, endpoint.port, endpoint.transport) == ("2001:db8::53", 5353, "tcp")

    @pytest.mark.parametrize(
        ("target", "error"),
        [
            ('{ url = "192.0.2.53", check = "dns" }', "needs 'query'"),
            ('{ url = "192.0.2.53", check = "dns", query = "" }', "needs 'query'"),
            ('{ url = "192.0.2.53", check = "ping", query = "example.com" }', "Unknown check"),
            (
                '{ url = "192.0.2.53", check = "dns", query = "example.com", transport = "tls" }',
                "transport must be",
            ),
            ('{ url = "192.0.2.53", query = "example.com" }', 'need check = "dns"'),
            (
                '{ url = "192.0.2.53", check = "dns", query = "example.com", protocol = "udp" }',
                "'protocol' cannot be combined",
            ),
            (
                '{ url = "dns.example.com", check = "dns", query = "example.com" }',
                "needs the DNS server's IP address",
            ),
            (
                '{ url = "192.0.2.53", check = "dns", query = "a..example" }',
                "Invalid query zone 'a..example' on 192.0.2.53",
            ),
            (
                f'{{ url = "192.0.2.53", check = "dns", query = "{"a" * 64}.example" }}',
                "Invalid query zone",
            ),
            (
                f'{{ url = "192.0.2.53", check = "dns", query = "{".".join(["a" * 60] * 4)}" }}',
                "Invalid query zone",
            ),
        ],
    )
    def test_invalid_dns_hops_are_rejected(self, tmp_path, target, error):
        with pytest.raises(ValueError, match=re.escape(error)):
            self.load(tmp_path, target)
