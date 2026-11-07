"""One probe that measures DNS, TCP connect, TLS handshake and HTTP response as separate layers."""

import asyncio
import functools
import ipaddress
import socket
import ssl
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import Executor
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from dns.nameserver import Nameserver

from hydraping.core.checkers.dns import DNSChecker
from hydraping.core.models import CheckType, Measurement


def _user_agent() -> str:
    try:
        return f"hydraping/{version('hydraping')}"
    except PackageNotFoundError:
        return "hydraping"


@dataclass(frozen=True)
class HTTPRequest:
    """The HTTP layer of a probe: a ``GET path`` whose status decides success."""

    path: str = "/"
    scheme: str = "http"

    def default_port(self) -> int:
        return 443 if self.scheme == "https" else 80


@dataclass(frozen=True)
class ConnectionTarget:
    """What one connection probe connects to, and which optional layers it runs.

    ``host`` may be a name or an IP literal. A name adds the DNS layer. ``tls`` adds the TLS layer
    after TCP, and ``http`` adds the HTTP layer last. The layers run in that order, and each one
    runs only when the one before it succeeded.
    """

    host: str
    port: int
    ip_version: int | None = None
    tls: bool = False
    insecure: bool = False
    http: HTTPRequest | None = None

    def layers(self) -> list[CheckType]:
        """Return the layers this target measures, in the order they run."""
        layers = [] if _is_ip_literal(self.host) else [CheckType.DNS]
        layers.append(CheckType.TCP)
        if self.tls:
            layers.append(CheckType.TLS)
        if self.http is not None:
            layers.append(CheckType.HTTP)
        return layers


class ConnectionChecker:
    """Measure the layers of one connection, so a failure names the layer that broke.

    One probe has one deadline: ``timeout`` seconds from its start, shared by all its layers. The
    layer that is still running when the deadline passes fails with a timeout whose message says
    what the earlier layers took, so a slow DNS lookup is not mistaken for a slow server. The
    latency of a finished layer covers that layer only: the DNS time is the lookup, the TCP time
    is the connect, the TLS time is the handshake, and the HTTP time runs from the request to the
    status line.

    The DNS layer asks ``socket.getaddrinfo``, so it sees what applications see: ``/etc/hosts``, the
    stub resolver and ``AI_ADDRCONFIG`` filtering of address families this machine cannot use. The
    lookup blocks a thread, so it runs on ``resolver_executor`` (the loop's default executor when
    None). The engine passes its own executor, one it can shut down without waiting, so a system
    resolver that hangs cannot keep the loop from closing. With ``nameservers`` set, the DNS layer
    queries those servers through dnspython instead. The TCP layer connects to the first address
    either one returned. The last resolved address of each (host, ip_version) is kept for the ICMP
    schedule, which pings the same address the chain used.

    HTTP is HTTP/1.1 only, one ``GET`` with ``Connection: close``. The probe reads the status line
    and closes; redirects are not followed, the status code is the result.

    ``ssl_context`` replaces the default verifying context; tests use it to trust a test CA.
    """

    # A graceful close waits for the peer's FIN or TLS close_notify. It gets what is left of the
    # probe's deadline, within these bounds, and the connection is aborted when that runs out.
    CLOSE_GRACE_MIN_SECONDS = 0.05
    CLOSE_GRACE_MAX_SECONDS = 1.0

    def __init__(
        self,
        timeout: float = 5.0,
        nameservers: Sequence[str | Nameserver] | None = None,
        http_success_status_max: int = 399,
        ssl_context: ssl.SSLContext | None = None,
        resolver_executor: Executor | None = None,
    ):
        self.timeout = timeout
        self.http_success_status_max = http_success_status_max
        self.dns_checker: DNSChecker | None = None
        if nameservers:
            self.dns_checker = DNSChecker(timeout=timeout, nameservers=nameservers)
        self._ssl_context = ssl_context
        self._resolver_executor = resolver_executor
        self._resolved_ips: dict[tuple[str, int | None], str] = {}

    def get_last_resolved_ip(self, host: str, ip_version: int | None = None) -> str | None:
        """Return the address the last successful DNS layer resolved ``host`` to.

        The IPv4 and IPv6 variants of one host are separate entries, so an ``ip_version = 6``
        endpoint never gets the address its IPv4 twin resolved.
        """
        return self._resolved_ips.get((host, ip_version))

    async def check(self, target: ConnectionTarget) -> list[Measurement]:
        """Run the layers of ``target`` in order and return one measurement per layer.

        Layers after the first failed one are returned as skipped. The connection is closed on
        every path, including a timeout and a cancellation. A cancellation propagates, so the
        scheduler records nothing for the probe.
        """
        layers = target.layers()
        budget = _Budget(self.timeout)
        measurements: list[Measurement] = []
        writer: asyncio.StreamWriter | None = None
        try:
            address = target.host
            if CheckType.DNS in layers:
                dns = await self._resolve(target.host, target.ip_version, budget)
                measurements.append(dns)
                if not dns.success:
                    return self._skip_rest(measurements, layers)
                assert dns.resolved_ip is not None
                address = dns.resolved_ip

            tcp, streams = await self._connect(address, target.port, budget)
            measurements.append(tcp)
            if streams is None:
                return self._skip_rest(measurements, layers)
            reader, writer = streams

            if target.tls:
                tls = await self._handshake(writer, target, budget)
                measurements.append(tls)
                if not tls.success:
                    return self._skip_rest(measurements, layers)

            if target.http is not None:
                measurements.append(await self._request(reader, writer, target, budget))
            return measurements
        finally:
            if writer is not None:
                grace = min(
                    max(budget.remaining(), self.CLOSE_GRACE_MIN_SECONDS),
                    self.CLOSE_GRACE_MAX_SECONDS,
                )
                await _close(writer, grace)

    @staticmethod
    def _skip_rest(measurements: list[Measurement], layers: list[CheckType]) -> list[Measurement]:
        failed = measurements[-1]
        for layer in layers[len(measurements) :]:
            measurements.append(Measurement.skipped_after(layer, failed))
        return measurements

    # -- layers ------------------------------------------------------------------------------

    async def _resolve(self, host: str, ip_version: int | None, budget: "_Budget") -> Measurement:
        with budget.layer(CheckType.DNS) as remaining:
            if remaining <= 0:
                return _failed(CheckType.DNS, budget.timeout_message())
            try:
                if self.dns_checker is not None:
                    measurement = await asyncio.wait_for(
                        self.dns_checker.check(host, ip_version), timeout=remaining
                    )
                else:
                    measurement = await asyncio.wait_for(
                        self._getaddrinfo(host, ip_version), timeout=remaining
                    )
            except TimeoutError:
                return _failed(CheckType.DNS, budget.timeout_message())
        if measurement.success and measurement.resolved_ip:
            self._resolved_ips[host, ip_version] = measurement.resolved_ip
        return measurement

    async def _getaddrinfo(self, host: str, ip_version: int | None) -> Measurement:
        family = {4: socket.AF_INET, 6: socket.AF_INET6}.get(ip_version, socket.AF_UNSPEC)
        lookup = functools.partial(
            socket.getaddrinfo,
            host,
            None,
            family=family,
            type=socket.SOCK_STREAM,
            flags=socket.AI_ADDRCONFIG,
        )
        loop = asyncio.get_running_loop()
        started = time.perf_counter()
        try:
            infos = await loop.run_in_executor(self._resolver_executor, lookup)
        except socket.gaierror as e:
            return _failed(CheckType.DNS, f"DNS lookup failed: {e.strerror or e}")
        except OSError as e:
            return _failed(CheckType.DNS, f"DNS error: {e}")
        latency_ms = (time.perf_counter() - started) * 1000
        if not infos:
            return _failed(CheckType.DNS, "No A or AAAA records found")
        return Measurement(
            check_type=CheckType.DNS,
            success=True,
            latency_ms=max(latency_ms, 0.0),
            resolved_ip=infos[0][4][0],
        )

    async def _connect(
        self, address: str, port: int, budget: "_Budget"
    ) -> tuple[Measurement, tuple[asyncio.StreamReader, asyncio.StreamWriter] | None]:
        with budget.layer(CheckType.TCP) as remaining:
            if remaining <= 0:
                return _failed(CheckType.TCP, budget.timeout_message(), port), None
            started = time.perf_counter()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(address, port), timeout=remaining
                )
            except TimeoutError:
                return _failed(CheckType.TCP, budget.timeout_message(), port), None
            except ConnectionRefusedError:
                return _failed(CheckType.TCP, f"Connection refused on port {port}", port), None
            except OSError as e:
                return _failed(CheckType.TCP, f"Network error: {e}", port), None
            latency_ms = (time.perf_counter() - started) * 1000
        return (
            Measurement(
                check_type=CheckType.TCP, success=True, latency_ms=max(latency_ms, 0.0), port=port
            ),
            (reader, writer),
        )

    async def _handshake(
        self, writer: asyncio.StreamWriter, target: ConnectionTarget, budget: "_Budget"
    ) -> Measurement:
        context = self._ssl_context_for(target)
        with budget.layer(CheckType.TLS) as remaining:
            if remaining <= 0:
                return _failed(CheckType.TLS, budget.timeout_message(), target.port)
            started = time.perf_counter()
            try:
                await asyncio.wait_for(
                    writer.start_tls(context, server_hostname=target.host), timeout=remaining
                )
            except TimeoutError:
                return _failed(CheckType.TLS, budget.timeout_message(), target.port)
            except ssl.SSLCertVerificationError as e:
                return _failed(
                    CheckType.TLS,
                    f"Certificate verification failed: {e.verify_message}",
                    target.port,
                )
            except ssl.SSLError as e:
                reason = e.reason or str(e)
                return _failed(CheckType.TLS, f"TLS handshake failed: {reason}", target.port)
            except OSError as e:
                return _failed(CheckType.TLS, f"TLS handshake failed: {e}", target.port)
            latency_ms = (time.perf_counter() - started) * 1000
        return Measurement(
            check_type=CheckType.TLS,
            success=True,
            latency_ms=max(latency_ms, 0.0),
            port=target.port,
        )

    def _ssl_context_for(self, target: ConnectionTarget) -> ssl.SSLContext:
        if target.insecure:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        if self._ssl_context is None:
            self._ssl_context = ssl.create_default_context()
        return self._ssl_context

    async def _request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: ConnectionTarget,
        budget: "_Budget",
    ) -> Measurement:
        assert target.http is not None
        request = target.http
        protocol = request.scheme
        host_header = f"[{target.host}]" if ":" in target.host else target.host
        if target.port != request.default_port():
            host_header = f"{host_header}:{target.port}"
        head = (
            f"GET {request.path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            f"User-Agent: {_user_agent()}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        with budget.layer(CheckType.HTTP) as remaining:
            if remaining <= 0:
                return _failed(CheckType.HTTP, budget.timeout_message(), protocol=protocol)
            started = time.perf_counter()
            try:
                writer.write(head.encode("ascii", "strict"))
                await asyncio.wait_for(writer.drain(), timeout=remaining)
                status_line = await asyncio.wait_for(reader.readline(), timeout=budget.remaining())
            except TimeoutError:
                return _failed(CheckType.HTTP, budget.timeout_message(), protocol=protocol)
            except UnicodeEncodeError:
                return _failed(
                    CheckType.HTTP,
                    f"Request path is not ASCII: {request.path!r}",
                    protocol=protocol,
                )
            except (OSError, asyncio.IncompleteReadError) as e:
                return _failed(CheckType.HTTP, f"Connection error: {e}", protocol=protocol)
            except ValueError:
                # readline() gives up when the stream's limit (64 KiB) fills without a newline.
                return _failed(
                    CheckType.HTTP,
                    "Not an HTTP response: no status line in the first 64 KiB",
                    protocol=protocol,
                )
            latency_ms = (time.perf_counter() - started) * 1000

        if not status_line:
            return _failed(CheckType.HTTP, "Connection closed before a response", protocol=protocol)
        status, reason = _parse_status_line(status_line)
        if status is None:
            text = status_line.decode("latin-1").strip()
            return _failed(CheckType.HTTP, f"Not an HTTP response: {text!r}", protocol=protocol)
        if status > self.http_success_status_max:
            return _failed(CheckType.HTTP, f"HTTP {status} {reason}".rstrip(), protocol=protocol)
        return Measurement(
            check_type=CheckType.HTTP,
            success=True,
            latency_ms=max(latency_ms, 0.0),
            protocol=protocol,
        )


class _Budget:
    """The deadline of one probe and what its finished layers took.

    ``layer()`` hands a layer the seconds it has left and, when the block ends, records how long
    the layer ran, so a timeout message can say where the time went.
    """

    def __init__(self, timeout: float):
        self.timeout = timeout
        self._loop = asyncio.get_running_loop()
        self._deadline = self._loop.time() + timeout
        self.spent: list[tuple[CheckType, float]] = []

    def remaining(self) -> float:
        return max(self._deadline - self._loop.time(), 0.0)

    @contextmanager
    def layer(self, check_type: CheckType) -> Iterator[float]:
        started = self._loop.time()
        try:
            yield self.remaining()
        finally:
            self.spent.append((check_type, self._loop.time() - started))

    def timeout_message(self) -> str:
        """Return the failure text of the layer that hit the deadline. Call it inside its block."""
        earlier = ", ".join(
            f"{check_type.value.upper()} took {seconds:.1f}s" for check_type, seconds in self.spent
        )
        suffix = f" ({earlier})" if earlier else ""
        return f"Timeout: probe deadline {self.timeout}s reached{suffix}"


def _parse_status_line(line: bytes) -> tuple[int | None, str]:
    """Return (status, reason) from an HTTP/1.x status line, or (None, "") if it is not one."""
    parts = line.decode("latin-1").rstrip("\r\n").split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        return None, ""
    return int(parts[1]), parts[2] if len(parts) == 3 else ""


def _failed(
    check_type: CheckType, message: str, port: int | None = None, protocol: str | None = None
) -> Measurement:
    return Measurement(
        check_type=check_type,
        success=False,
        error_message=message,
        port=port,
        protocol=protocol,
    )


async def _close(writer: asyncio.StreamWriter, grace: float) -> None:
    """Close the connection: gracefully within ``grace`` seconds, by aborting it after that.

    A transport that asyncio already closed is left alone. ``start_tls`` closes the TCP transport
    when the handshake fails or is cancelled, and in that state the stream protocol never hears
    ``connection_lost``, so ``wait_closed()`` would never return. The close itself is already
    scheduled; one loop iteration lets it run.

    A graceful close over TLS needs the peer's close_notify. A peer that stopped answering never
    sends one, so after ``grace`` the connection is aborted, which closes the socket on the next
    loop iteration without waiting for anyone.
    """
    transport = writer.transport
    if transport.is_closing():
        await asyncio.sleep(0)
        return
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=grace)
    except TimeoutError:
        transport.abort()
        await asyncio.sleep(0)
    except (OSError, ssl.SSLError):
        pass  # The peer was already gone; the socket is closed all the same.


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
