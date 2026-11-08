"""Tests for the per-layer connection checker against localhost servers."""

import asyncio
import re
import socket
import ssl
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager

import dns.message
import dns.rrset
import pytest
import trustme
from dns.nameserver import Do53Nameserver

from hydraping.core.checkers.connection import (
    ConnectionChecker,
    ConnectionTarget,
    HTTPRequest,
    _parse_status_line,
)
from hydraping.core.models import CheckType, Measurement

TIMEOUT = 0.5

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def http_responder(status_line: str) -> Handler:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request)
        writer.write(f"{status_line}\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    requests: list[bytes] = []
    handle.requests = requests  # type: ignore[attr-defined]
    return handle


def raw_responder(*parts: bytes, delay: float = 0.0, stall: bool = False) -> Handler:
    """Answer with ``parts`` written one by one, ``delay`` seconds apart.

    With ``stall``, keep the connection open afterwards until the client closes it.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request)
        try:
            for index, part in enumerate(parts):
                if index and delay:
                    await asyncio.sleep(delay)
                writer.write(part)
                await writer.drain()
            if stall:
                await reader.read(-1)
            writer.close()
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass  # The client stopped reading once it had what it needed.

    requests: list[bytes] = []
    handle.requests = requests  # type: ignore[attr-defined]
    return handle


def head(status: str = "200 OK", *headers: str) -> bytes:
    lines = [f"HTTP/1.1 {status}", *headers, "Connection: close", "", ""]
    return "\r\n".join(lines).encode()


async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Accept, read until the client gives up, and never answer."""
    await reader.read(-1)
    writer.close()
    await writer.wait_closed()


@asynccontextmanager
async def server(handler: Handler, ssl_context: ssl.SSLContext | None = None) -> AsyncIterator[int]:
    srv = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ssl_context)
    try:
        yield srv.sockets[0].getsockname()[1]
    finally:
        srv.close()
        await srv.wait_closed()


class SlowDNS(asyncio.DatagramProtocol):
    """A DNS server that answers every A query with 127.0.0.1 after ``delay`` seconds."""

    def __init__(self, delay: float):
        self.delay = delay
        self.transport: asyncio.DatagramTransport | None = None
        self.answers: set[asyncio.Task] = set()

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        task = asyncio.ensure_future(self._answer(data, addr))
        self.answers.add(task)
        task.add_done_callback(self.answers.discard)

    async def _answer(self, data: bytes, addr) -> None:
        await asyncio.sleep(self.delay)
        query = dns.message.from_wire(data)
        response = dns.message.make_response(query)
        name = query.question[0].name
        response.answer.append(dns.rrset.from_text(name, 60, "IN", "A", "127.0.0.1"))
        assert self.transport is not None
        self.transport.sendto(response.to_wire(), addr)


@asynccontextmanager
async def slow_dns_server(delay: float) -> AsyncIterator[int]:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: SlowDNS(delay), local_addr=("127.0.0.1", 0)
    )
    try:
        yield transport.get_extra_info("sockname")[1]
    finally:
        for task in list(protocol.answers):
            task.cancel()
        await asyncio.gather(*protocol.answers, return_exceptions=True)
        transport.close()


def closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def layers_of(measurements: list[Measurement]) -> list[CheckType]:
    return [m.check_type for m in measurements]


def by_layer(measurements: list[Measurement]) -> dict[CheckType, Measurement]:
    return {m.check_type: m for m in measurements}


def assert_skipped(measurement: Measurement, blocked_by: CheckType) -> None:
    assert measurement.skipped
    assert not measurement.success
    assert measurement.blocked_by == blocked_by
    assert measurement.failed_layer == blocked_by
    assert measurement.error_message == f"Not run: {blocked_by.value.upper()} failed"


@pytest.fixture
def ca() -> trustme.CA:
    return trustme.CA()


def server_context(ca: trustme.CA, hostname: str) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert(hostname).configure_cert(context)
    return context


def tls_server(ca: trustme.CA):
    """An HTTPS server answering 200 with a certificate for "localhost" issued by ``ca``."""
    return server(http_responder("HTTP/1.1 200 OK"), server_context(ca, "localhost"))


@contextmanager
def hung_tls_server(ca: trustme.CA) -> Iterator[int]:
    """A TLS server that completes the handshake and then stops reading, as a hung backend does.

    An asyncio server cannot play this part: its SSL protocol answers a close_notify on its own,
    so a graceful close would always complete. This one is a blocking socket in a thread that
    never reads again after the handshake.
    """
    context = server_context(ca, "localhost")
    release = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.settimeout(5.0)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        conn.settimeout(5.0)
        try:
            tls = context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            conn.close()
            return
        with tls:
            release.wait()

    thread = threading.Thread(target=serve, name="hung-tls-server")
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        release.set()
        thread.join(5.0)
        listener.close()


def trusting_checker(ca: trustme.CA) -> ConnectionChecker:
    context = ssl.create_default_context()
    ca.configure_trust(context)
    return ConnectionChecker(timeout=TIMEOUT, ssl_context=context)


class TestLayers:
    def test_ip_target_has_no_dns_layer(self):
        assert ConnectionTarget("127.0.0.1", 80).layers() == [CheckType.TCP]
        assert ConnectionTarget("::1", 80).layers() == [CheckType.TCP]

    def test_https_target_runs_all_four_layers_in_order(self):
        target = ConnectionTarget("example.com", 443, tls=True, http=HTTPRequest("/", "https"))
        assert target.layers() == [CheckType.DNS, CheckType.TCP, CheckType.TLS, CheckType.HTTP]


class TestHTTP:
    async def test_200_passes_every_layer_with_its_own_latency(self):
        handler = http_responder("HTTP/1.1 200 OK")
        async with server(handler) as port:
            target = ConnectionTarget("localhost", port, ip_version=4, http=HTTPRequest("/health"))
            measurements = await ConnectionChecker(timeout=TIMEOUT).check(target)

        assert layers_of(measurements) == [CheckType.DNS, CheckType.TCP, CheckType.HTTP]
        assert all(m.success for m in measurements), measurements
        assert all(m.latency_ms is not None and m.latency_ms >= 0 for m in measurements)
        dns, tcp, http = measurements
        assert dns.resolved_ip == "127.0.0.1"
        assert tcp.port == port
        assert http.protocol == "http"
        [request] = handler.requests
        lines = request.decode().split("\r\n")
        assert lines[0] == "GET /health HTTP/1.1"
        assert f"Host: localhost:{port}" in lines
        assert "Connection: close" in lines
        assert any(line.startswith("User-Agent: hydraping") for line in lines)

    async def test_500_fails_at_http_only(self):
        async with server(http_responder("HTTP/1.1 500 Internal Server Error")) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest())
            measurements = await ConnectionChecker(timeout=TIMEOUT).check(target)

        tcp, http = measurements
        assert tcp.success
        assert not http.success and not http.skipped
        assert http.error_message == "HTTP 500 Internal Server Error"
        assert http.failed_layer == CheckType.HTTP

    async def test_success_status_max_decides(self):
        async with server(http_responder("HTTP/1.1 302 Found")) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest())
            lenient = await ConnectionChecker(timeout=TIMEOUT).check(target)
            strict = await ConnectionChecker(timeout=TIMEOUT, http_success_status_max=299).check(
                target
            )

        assert by_layer(lenient)[CheckType.HTTP].success
        assert by_layer(strict)[CheckType.HTTP].error_message == "HTTP 302 Found"

    async def test_server_that_never_answers_fails_at_http_at_the_probe_deadline(self):
        async with server(silent) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest())
            started = time.perf_counter()
            measurements = await ConnectionChecker(timeout=TIMEOUT).check(target)
            took = time.perf_counter() - started

        tcp, http = measurements
        assert tcp.success
        assert re.fullmatch(
            rf"Timeout: probe deadline {TIMEOUT}s reached \(TCP took 0\.\ds\)", http.error_message
        ), http.error_message
        # The deadline counts from the probe's start, not from the end of the TCP layer.
        assert TIMEOUT <= took < TIMEOUT + 0.3

    async def test_slow_first_layer_leaves_the_next_one_to_hit_the_deadline(self):
        dns_delay = TIMEOUT * 0.6
        async with slow_dns_server(dns_delay) as dns_port, server(silent) as port:
            checker = ConnectionChecker(
                timeout=TIMEOUT, nameservers=[Do53Nameserver("127.0.0.1", dns_port)]
            )
            target = ConnectionTarget("slow.example.internal", port, http=HTTPRequest())
            started = time.perf_counter()
            measurements = await checker.check(target)
            took = time.perf_counter() - started

        dns, tcp, http = measurements
        assert dns.success and dns.resolved_ip == "127.0.0.1"
        assert dns.latency_ms >= dns_delay * 1000
        assert tcp.success and tcp.latency_ms < 100
        assert not http.success and not http.skipped
        assert re.fullmatch(
            rf"Timeout: probe deadline {TIMEOUT}s reached \(DNS took 0\.\ds, TCP took 0\.\ds\)",
            http.error_message,
        ), http.error_message
        assert TIMEOUT <= took < TIMEOUT + 0.3

    async def test_status_line_longer_than_the_stream_limit_fails_at_http(self):
        async def endless(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"x" * (2**16 + 1024))  # Past the 64 KiB StreamReader limit, no newline.
            await writer.drain()
            await reader.read(-1)
            writer.close()
            await writer.wait_closed()

        async with server(endless) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest())
            measurements = await ConnectionChecker(timeout=TIMEOUT).check(target)

        tcp, http = measurements
        assert tcp.success
        assert not http.success and not http.skipped
        assert http.failed_layer == CheckType.HTTP
        assert http.error_message == "Not an HTTP response: no status line in the first 64 KiB"

    async def test_non_http_answer_fails_at_http(self):
        async def ssh_banner(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"SSH-2.0-OpenSSH_9.6\r\n")
            await writer.drain()
            await reader.read(-1)
            writer.close()
            await writer.wait_closed()

        async with server(ssh_banner) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest())
            measurements = await ConnectionChecker(timeout=TIMEOUT).check(target)

        assert by_layer(measurements)[CheckType.HTTP].error_message == (
            "Not an HTTP response: 'SSH-2.0-OpenSSH_9.6'"
        )

    def test_status_line_parsing(self):
        assert _parse_status_line(b"HTTP/1.1 404 Not Found\r\n") == (404, "Not Found")
        assert _parse_status_line(b"HTTP/1.0 200\r\n") == (200, "")
        assert _parse_status_line(b"<html>\r\n") == (None, "")
        assert _parse_status_line(b"HTTP/1.1 abc OK\r\n") == (None, "")


class TestTLS:
    async def test_trusted_certificate_passes_tls_and_http(self, ca):
        async with tls_server(ca) as port:
            target = ConnectionTarget(
                "localhost", port, ip_version=4, tls=True, http=HTTPRequest("/", "https")
            )
            measurements = await trusting_checker(ca).check(target)

        assert layers_of(measurements) == [
            CheckType.DNS,
            CheckType.TCP,
            CheckType.TLS,
            CheckType.HTTP,
        ]
        assert all(m.success for m in measurements), measurements
        assert by_layer(measurements)[CheckType.HTTP].protocol == "https"

    async def test_untrusted_certificate_fails_at_tls_and_skips_http(self, ca):
        async with tls_server(ca) as port:
            target = ConnectionTarget("127.0.0.1", port, tls=True, http=HTTPRequest("/", "https"))
            started = time.perf_counter()
            measurements = await ConnectionChecker(timeout=TIMEOUT).check(target)
            took = time.perf_counter() - started

        tcp, tls, http = measurements
        assert tcp.success
        assert not tls.success and not tls.skipped
        assert tls.error_message.startswith("Certificate verification failed: ")
        assert_skipped(http, CheckType.TLS)
        # asyncio closed the transport when the handshake failed; the probe must not sit on
        # wait_closed() for a connection_lost that never reaches the stream.
        assert took < TIMEOUT

    async def test_hostname_mismatch_fails_at_tls(self, ca):
        async with tls_server(ca) as port:
            # The certificate is for "localhost"; connecting by address must not match it.
            target = ConnectionTarget("127.0.0.1", port, tls=True)
            measurements = await trusting_checker(ca).check(target)

        tls = by_layer(measurements)[CheckType.TLS]
        assert not tls.success
        assert "Certificate verification failed" in tls.error_message

    async def test_insecure_accepts_an_untrusted_certificate(self, ca):
        async with tls_server(ca) as port:
            target = ConnectionTarget(
                "127.0.0.1", port, tls=True, insecure=True, http=HTTPRequest("/", "https")
            )
            measurements = await ConnectionChecker(timeout=TIMEOUT).check(target)

        assert all(m.success for m in measurements), measurements

    async def test_plain_http_server_fails_the_handshake(self):
        async def plain_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read(1024)  # the ClientHello
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with server(plain_http) as port:
            target = ConnectionTarget("127.0.0.1", port, tls=True, http=HTTPRequest("/", "https"))
            measurements = await ConnectionChecker(timeout=TIMEOUT).check(target)

        tcp, tls, http = measurements
        assert tcp.success
        assert not tls.success
        assert tls.error_message.startswith("TLS handshake failed: "), tls.error_message
        assert_skipped(http, CheckType.TLS)

    async def test_server_that_never_answers_the_handshake_hits_the_deadline(self):
        async with server(silent) as port:
            target = ConnectionTarget("127.0.0.1", port, tls=True, http=HTTPRequest("/", "https"))
            started = time.perf_counter()
            measurements = await ConnectionChecker(timeout=TIMEOUT).check(target)
            took = time.perf_counter() - started

        tcp, tls, http = measurements
        assert tcp.success
        assert tls.error_message.startswith(
            f"Timeout: probe deadline {TIMEOUT}s reached (TCP took "
        )
        assert_skipped(http, CheckType.TLS)
        assert TIMEOUT <= took < TIMEOUT + 0.3

    async def test_server_that_hangs_after_the_handshake_hits_the_deadline_at_http(self, ca):
        with hung_tls_server(ca) as port:
            target = ConnectionTarget(
                "localhost", port, ip_version=4, tls=True, http=HTTPRequest("/", "https")
            )
            started = time.perf_counter()
            measurements = await trusting_checker(ca).check(target)
            took = time.perf_counter() - started

        dns, tcp, tls, http = measurements
        assert dns.success and tcp.success and tls.success
        assert not http.success and not http.skipped
        assert http.error_message.startswith(f"Timeout: probe deadline {TIMEOUT}s reached (")
        # The peer never answers the TLS close_notify either, so the close must give up on the
        # graceful path and abort instead of waiting for it.
        assert TIMEOUT <= took < TIMEOUT + 0.3


class TestExpectBody:
    async def test_text_in_a_plain_body_passes_with_latency_up_to_the_status_line(self):
        body = b"fl=1\nip=192.0.2.1\nmarker=on\n"
        handler = raw_responder(
            head("200 OK", f"Content-Length: {len(body)}"), body, delay=TIMEOUT * 0.4
        )
        async with server(handler) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest(expect_body="marker=on"))
            tcp, http = await ConnectionChecker(timeout=TIMEOUT).check(target)

        assert tcp.success
        assert http.success, http.error_message
        # The body came TIMEOUT * 0.4 after the status line; the latency does not include it.
        assert http.latency_ms is not None and http.latency_ms < TIMEOUT * 0.4 * 1000
        [request] = handler.requests
        assert b"accept-encoding" not in request.lower()

    async def test_text_split_across_two_chunks_is_found(self):
        chunked = b"6\r\nmarker\r\n3\r\n=on\r\n0\r\n\r\n"
        handler = raw_responder(head("200 OK", "Transfer-Encoding: chunked"), chunked)
        async with server(handler) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest(expect_body="marker=on"))
            _, http = await ConnectionChecker(timeout=TIMEOUT).check(target)

        assert http.success, http.error_message

    async def test_missing_text_fails_at_http_with_the_bytes_read(self):
        body = b"marker=off\n"
        handler = raw_responder(head("200 OK", f"Content-Length: {len(body)}"), body)
        async with server(handler) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest(expect_body="marker=on"))
            tcp, http = await ConnectionChecker(timeout=TIMEOUT).check(target)

        assert tcp.success
        assert not http.success and not http.skipped
        assert http.failed_layer == CheckType.HTTP
        assert http.error_message == 'Response body lacks "marker=on" (read 11 bytes)'

    async def test_body_larger_than_the_limit_without_the_text_fails_after_64_kib(self):
        body = b"x" * (ConnectionChecker.BODY_LIMIT_BYTES + 6 * 1024) + b"marker=on"
        handler = raw_responder(head("200 OK", f"Content-Length: {len(body)}"), body)
        async with server(handler) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest(expect_body="marker=on"))
            _, http = await ConnectionChecker(timeout=TIMEOUT).check(target)

        assert http.error_message == 'Response body lacks "marker=on" in the first 64 KiB'

    async def test_server_that_stalls_after_the_headers_hits_the_deadline_at_http(self):
        handler = raw_responder(head("200 OK", "Content-Length: 100"), b"partial", stall=True)
        async with server(handler) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest(expect_body="marker=on"))
            started = time.perf_counter()
            tcp, http = await ConnectionChecker(timeout=TIMEOUT).check(target)
            took = time.perf_counter() - started

        assert tcp.success
        assert re.fullmatch(
            rf"Timeout: probe deadline {TIMEOUT}s reached \(TCP took 0\.\ds\)", http.error_message
        ), http.error_message
        assert TIMEOUT <= took < TIMEOUT + 0.3

    async def test_failing_status_fails_without_reading_the_body(self):
        # The body never comes: reading it would run into the probe deadline.
        handler = raw_responder(head("503 Service Unavailable", "Content-Length: 100"), stall=True)
        async with server(handler) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest(expect_body="marker=on"))
            started = time.perf_counter()
            _, http = await ConnectionChecker(timeout=TIMEOUT).check(target)
            took = time.perf_counter() - started

        assert http.error_message == "HTTP 503 Service Unavailable"
        assert took < TIMEOUT / 2

    async def test_malformed_chunk_size_fails_at_http(self):
        handler = raw_responder(head("200 OK", "Transfer-Encoding: chunked"), b"zz\r\nabc\r\n")
        async with server(handler) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest(expect_body="marker=on"))
            _, http = await ConnectionChecker(timeout=TIMEOUT).check(target)

        assert http.error_message == "Malformed chunked response: 'zz'"

    async def test_text_found_over_tls(self, ca):
        body = b"marker=on\n"
        handler = raw_responder(head("200 OK", f"Content-Length: {len(body)}"), body)
        async with server(handler, server_context(ca, "localhost")) as port:
            target = ConnectionTarget(
                "localhost",
                port,
                ip_version=4,
                tls=True,
                http=HTTPRequest("/cdn-cgi/trace", "https", expect_body="marker=on"),
            )
            measurements = await trusting_checker(ca).check(target)

        assert all(m.success for m in measurements), measurements


class TestLowerLayers:
    async def test_closed_port_fails_at_tcp_and_skips_the_rest(self):
        port = closed_port()
        target = ConnectionTarget("127.0.0.1", port, tls=True, http=HTTPRequest("/", "https"))
        measurements = await ConnectionChecker(timeout=TIMEOUT).check(target)

        tcp, tls, http = measurements
        assert tcp.error_message == f"Connection refused on port {port}"
        assert tcp.port == port
        assert_skipped(tls, CheckType.TCP)
        assert_skipped(http, CheckType.TCP)

    async def test_unresolvable_name_fails_at_dns_and_skips_the_rest(self):
        target = ConnectionTarget(
            "nonexistent.invalid", 443, tls=True, http=HTTPRequest("/", "https")
        )
        checker = ConnectionChecker(timeout=5.0)
        measurements = await checker.check(target)

        dns, tcp, tls, http = measurements
        assert dns.check_type == CheckType.DNS
        assert not dns.success and not dns.skipped
        assert dns.error_message.startswith("DNS lookup")
        assert layers_of(measurements) == [
            CheckType.DNS,
            CheckType.TCP,
            CheckType.TLS,
            CheckType.HTTP,
        ]
        for measurement in (tcp, tls, http):
            assert_skipped(measurement, CheckType.DNS)
        assert checker.get_last_resolved_ip("nonexistent.invalid") is None

    async def test_resolved_address_is_kept_per_ip_version_for_the_icmp_schedule(self):
        async with server(silent) as port:
            checker = ConnectionChecker(timeout=TIMEOUT)
            await checker.check(ConnectionTarget("localhost", port, ip_version=4))
        assert checker.get_last_resolved_ip("localhost", 4) == "127.0.0.1"
        # The IPv6 and the unpinned variant of the same host are their own entries.
        assert checker.get_last_resolved_ip("localhost", 6) is None
        assert checker.get_last_resolved_ip("localhost") is None

    async def test_dns_layer_runs_on_the_given_executor(self):
        threads: set[str] = set()

        class RecordingExecutor(ThreadPoolExecutor):
            def submit(self, fn, /, *args, **kwargs):
                def record():
                    threads.add(threading.current_thread().name)
                    return fn(*args, **kwargs)

                return super().submit(record)

        executor = RecordingExecutor(thread_name_prefix="test-resolver")
        try:
            async with server(silent) as port:
                checker = ConnectionChecker(timeout=TIMEOUT, resolver_executor=executor)
                [dns, _tcp] = await checker.check(ConnectionTarget("localhost", port, ip_version=4))
        finally:
            executor.shutdown(wait=True)

        assert dns.success
        assert threads and all(name.startswith("test-resolver") for name in threads)


class TestCancellation:
    async def test_cancelled_probe_closes_the_connection_and_applies_nothing(self):
        peer_closed = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read(-1)
            peer_closed.set()
            writer.close()
            await writer.wait_closed()

        async with server(handler) as port:
            target = ConnectionTarget("127.0.0.1", port, http=HTTPRequest())
            task = asyncio.ensure_future(ConnectionChecker(timeout=10.0).check(target))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(peer_closed.wait(), timeout=1.0)
