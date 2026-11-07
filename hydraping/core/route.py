"""Which network interface the kernel routes a target address through."""

import asyncio
import json
import logging
import time
from collections.abc import Sequence

logger = logging.getLogger(__name__)


def parse_route_get(output: str | bytes) -> str | None:
    """Return the outgoing interface from the JSON of ``ip -j route get <address>``.

    The output is a list with one route object; its ``dev`` is the interface. Anything else,
    including invalid JSON, gives None.
    """
    try:
        routes = json.loads(output)
    except ValueError:
        return None
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        return None
    dev = routes[0].get("dev")
    return dev if isinstance(dev, str) and dev else None


# The interface label for an address the kernel has no route to, such as a VPN subnet while the
# VPN is down. The dashboard shows it as "via no route".
NO_ROUTE = "no route"

# What ``ip route get`` prints to stderr when the kernel has no route to the address.
_NO_ROUTE_ERRORS = ("Network is unreachable", "No route to host")

# The address that checks once whether ``ip`` works here: every Linux host has a route to it.
_SELF_TEST_ADDRESS = "127.0.0.1"


class EgressInterfaces:
    """Look up the outgoing interface per address with ``ip -j route get``, cached per address.

    On first use, one lookup of 127.0.0.1 checks that ``ip`` works here. When the command is
    missing, fails or prints something unparsable for it, no lookups are attempted for the rest
    of the session. After that, a failure for one address never turns lookups off: an address
    without a route gives ``NO_ROUTE``, and any other failure gives None.

    Each address is looked up at most once per ``cache_seconds``, whatever the outcome, so the
    subprocess rate is bounded by the number of distinct addresses. Concurrent lookups of the same
    address share one subprocess. A lookup never raises.
    """

    def __init__(
        self,
        command: Sequence[str] = ("ip", "-j", "route", "get"),
        cache_seconds: float = 5.0,
        timeout: float = 2.0,
    ):
        self.command = tuple(command)
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._pending: dict[str, asyncio.Future[str | None]] = {}
        self._works: bool | None = None
        self._self_test: asyncio.Future[bool] | None = None
        self.lookups = 0

    async def interface_for(self, address: str) -> str | None:
        if not await self._command_works():
            return None
        now = time.monotonic()
        cached = self._cache.get(address)
        if cached is not None and now - cached[0] < self.cache_seconds:
            return cached[1]
        pending = self._pending.get(address)
        if pending is None:
            pending = asyncio.ensure_future(self._lookup(address))
            self._pending[address] = pending
            pending.add_done_callback(lambda _: self._pending.pop(address, None))
        # Without a shield: when the engine stops, cancelling the caller also cancels the lookup.
        return await pending

    async def _command_works(self) -> bool:
        """Run the self-test once; concurrent callers share it. A cancelled one runs again."""
        if self._works is None:
            if self._self_test is None or self._self_test.cancelled():
                self._self_test = asyncio.ensure_future(self._test_command())
            self._works = await self._self_test
        return self._works

    async def _test_command(self) -> bool:
        output = await self._exec(_SELF_TEST_ADDRESS)
        if output is None:
            return False
        returncode, stdout, _ = output
        if returncode == 0 and parse_route_get(stdout) is not None:
            return True
        logger.info(
            "%s does not work here (route lookup of %s failed); outgoing interfaces are not shown",
            self.command[0],
            _SELF_TEST_ADDRESS,
        )
        return False

    async def _lookup(self, address: str) -> str | None:
        self.lookups += 1
        interface = await self._run(address)
        now = time.monotonic()
        # Drop expired entries, so addresses a name no longer resolves to don't pile up.
        for stale in [a for a, (at, _) in self._cache.items() if now - at >= self.cache_seconds]:
            del self._cache[stale]
        self._cache[address] = (now, interface)
        return interface

    async def _run(self, address: str) -> str | None:
        output = await self._exec(address)
        if output is None:
            return None
        returncode, stdout, stderr = output
        if returncode == 0:
            return parse_route_get(stdout)
        errors = stderr.decode(errors="replace")
        if any(error in errors for error in _NO_ROUTE_ERRORS):
            return NO_ROUTE
        return None

    async def _exec(self, address: str) -> tuple[int, bytes, bytes] | None:
        """Run the command for ``address``; return its exit status, stdout and stderr, or None
        when it could not run or timed out."""
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                address,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.info("%s not found; outgoing interfaces are not shown", self.command[0])
            return None
        except OSError:
            logger.debug("Could not run %s", self.command[0], exc_info=True)
            return None

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
        except TimeoutError:
            await self._reap(process)
            return None
        except BaseException:
            # Cancelled with the engine: never leave the child running or unreaped.
            await self._reap(process)
            raise
        assert process.returncode is not None
        return process.returncode, stdout, stderr

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.kill()
        await process.wait()
