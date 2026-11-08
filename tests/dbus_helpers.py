"""D-Bus helpers for tests: an asyncio loop on its own thread, and a minimal tray watcher."""

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusStr
from dbus_fast.service import ServiceInterface, dbus_method

from hydraping.gui.tray import WATCHER_NAME, WATCHER_PATH

T = TypeVar("T")


class LoopThread:
    """An asyncio loop on a thread of its own, so synchronous tests (and Qt's event loop on the
    main thread) can use asyncio D-Bus clients."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coroutine: Coroutine[Any, Any, T], timeout: float = 5.0) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join()
        self.loop.close()


class FakeWatcher(ServiceInterface):
    """The one method of ``org.kde.StatusNotifierWatcher`` that an item calls."""

    def __init__(self):
        super().__init__(WATCHER_NAME)
        self.items: list[str] = []

    @dbus_method()
    def RegisterStatusNotifierItem(self, service: DBusStr) -> None:
        self.items.append(service)


async def start_watcher(address: str) -> tuple[MessageBus, FakeWatcher]:
    bus = await MessageBus(bus_address=address).connect()
    watcher = FakeWatcher()
    bus.export(WATCHER_PATH, watcher)
    await bus.request_name(WATCHER_NAME)
    return bus, watcher


async def connect(address: str) -> MessageBus:
    return await MessageBus(bus_address=address).connect()


async def disconnect(bus: MessageBus) -> None:
    bus.disconnect()
    await bus.wait_for_disconnect()


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out after {timeout}s waiting for {condition}")
        time.sleep(0.01)
