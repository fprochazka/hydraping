"""The top-bar item: a StatusNotifierItem on the session bus, served from its own thread.

Ubuntu's top bar shows such items through the AppIndicator extension, KDE's panel natively. The
item registers with ``org.kde.StatusNotifierWatcher``, and again whenever the watcher's owner
changes, for example after the shell restarts. It serves its icon as raw pixels, a text label
(the Ayatana extension that the AppIndicator extension reads) and a menu over
``com.canonical.dbusmenu``.

``QSystemTrayIcon`` speaks the same protocol, but it can't set a label, which is why this module
talks D-Bus itself. It never touches Qt: the D-Bus thread only calls the callbacks it was given,
and the owner makes those hand over to Qt's thread.
"""

import asyncio
import sys
import threading
from collections.abc import Callable
from typing import Annotated, Any

from dbus_fast import BusType, Message, MessageFlag, MessageType, PropertyAccess
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import (
    DBusBool,
    DBusInt32,
    DBusObjectPath,
    DBusSignature,
    DBusStr,
)
from dbus_fast.service import ServiceInterface, dbus_method, dbus_property, dbus_signal

from hydraping.gui import dbusmenu
from hydraping.gui.tray_icon import icon_pixmaps
from hydraping.gui.tray_status import INITIAL_STATUS, LABEL_GUIDE, TrayStatus

INTERFACE = "org.kde.StatusNotifierItem"
OBJECT_PATH = "/StatusNotifierItem"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_ID = "hydraping"
TITLE = "HydraPing"

# How long ``start()`` waits for the connection and the first registration attempt.
START_TIMEOUT_SECONDS = 5.0

Pixmaps = Annotated[list[Any], DBusSignature("a(iiay)")]
ToolTip = Annotated[list[Any], DBusSignature("(sa(iiay)ss)")]

_NAME_OWNER_CHANGED_RULE = (
    "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
    f"member='NameOwnerChanged',arg0='{WATCHER_NAME}'"
)


class StatusNotifierItemInterface(ServiceInterface):
    """``org.kde.StatusNotifierItem``, with the Ayatana label.

    ``set_status`` updates what the properties return and sends the matching change signals. It
    and every method here run on the D-Bus thread.
    """

    def __init__(self, on_activate: Callable[[str | None], None]):
        super().__init__(INTERFACE)
        self._on_activate = on_activate
        self._status = INITIAL_STATUS
        self._pixmaps = icon_pixmaps(INITIAL_STATUS.bars)
        self._activation_token: str | None = None

    def set_status(self, status: TrayStatus) -> None:
        previous = self._status
        self._status = status
        if status.bars != previous.bars:
            self._pixmaps = icon_pixmaps(status.bars)
            self.NewIcon()
        if (status.tooltip_title, status.tooltip) != (previous.tooltip_title, previous.tooltip):
            self.NewToolTip()
        if status.label != previous.label:
            self.XAyatanaNewLabel()

    @property
    def status(self) -> TrayStatus:
        return self._status

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> DBusStr:
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> DBusStr:
        return ITEM_ID

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> DBusStr:
        return TITLE

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> DBusStr:
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> DBusInt32:
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> DBusStr:
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> DBusObjectPath:
        return dbusmenu.OBJECT_PATH

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> DBusBool:
        return False

    # Hosts prefer an icon name over pixmaps, so the names stay empty.
    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> DBusStr:
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> Pixmaps:
        return [list(pixmap) for pixmap in self._pixmaps]

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> DBusStr:
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> Pixmaps:
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> DBusStr:
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> Pixmaps:
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionMovieName(self) -> DBusStr:
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> ToolTip:
        return ["", [], self._status.tooltip_title, self._status.tooltip]

    @dbus_property(access=PropertyAccess.READ)
    def XAyatanaLabel(self) -> DBusStr:
        return self._status.label

    @dbus_property(access=PropertyAccess.READ)
    def XAyatanaLabelGuide(self) -> DBusStr:
        return LABEL_GUIDE

    @dbus_method()
    def ProvideXdgActivationToken(self, token: DBusStr) -> None:
        # Sent right before Activate. On Wayland, the window may take focus only with this token.
        self._activation_token = token

    @dbus_method()
    def Activate(self, x: DBusInt32, y: DBusInt32) -> None:
        token, self._activation_token = self._activation_token, None
        self._on_activate(token)

    @dbus_method()
    def SecondaryActivate(self, x: DBusInt32, y: DBusInt32) -> None:
        pass

    @dbus_method()
    def ContextMenu(self, x: DBusInt32, y: DBusInt32) -> None:
        # Hosts show the menu from the Menu property; this is for hosts without dbusmenu.
        pass

    @dbus_method()
    def Scroll(self, delta: DBusInt32, orientation: DBusStr) -> None:
        pass

    @dbus_signal()
    def NewTitle(self) -> None:
        return None

    @dbus_signal()
    def NewIcon(self) -> None:
        return None

    @dbus_signal()
    def NewToolTip(self) -> None:
        return None

    @dbus_signal()
    def NewStatus(self) -> DBusStr:
        return "Active"

    @dbus_signal()
    def XAyatanaNewLabel(self) -> Annotated[list[str], DBusSignature("ss")]:
        return [self._status.label, LABEL_GUIDE]


class TrayService:
    """Run the top-bar item on its own thread with its own asyncio loop.

    The callbacks run on that thread: ``on_activate`` with the activation token or None when the
    host asks to open the window, ``on_quit`` for the menu's Quit, and ``on_registered`` with
    whether a host shows the item now. ``update`` and ``stop`` may be called from any thread.
    """

    def __init__(
        self,
        on_activate: Callable[[str | None], None],
        on_quit: Callable[[], None],
        on_registered: Callable[[bool], None] = lambda _: None,
        bus_address: str | None = None,
    ):
        self._on_registered = on_registered
        self._bus_address = bus_address
        self.item = StatusNotifierItemInterface(on_activate)
        self.menu = dbusmenu.DBusMenuInterface(
            dbusmenu.Menu(
                [
                    dbusmenu.MenuItem(1, "Open HydraPing", lambda: on_activate(None)),
                    dbusmenu.MenuItem(2, "Quit", on_quit),
                ]
            )
        )
        self.registered = False
        # Why the item isn't shown, when it isn't: no session bus, or no host on it.
        self.error: str | None = None
        self.unique_name: str | None = None
        self._lock = threading.Lock()
        self._status = INITIAL_STATUS
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping: asyncio.Event | None = None
        self._started = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Connect and register; return once the first registration attempt is done."""
        self._thread = threading.Thread(target=self._run, name="hydraping-tray", daemon=True)
        self._thread.start()
        self._started.wait(START_TIMEOUT_SECONDS)

    def update(self, status: TrayStatus) -> None:
        """Show ``status``. Only the parts that changed are signalled to the host."""
        with self._lock:
            self._status = status
            loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._apply_status)
            except RuntimeError:
                pass  # The loop has closed; the item is gone.

    def stop(self) -> None:
        """Leave the session bus, which removes the item from the host, and join the thread."""
        with self._lock:
            loop, stopping = self._loop, self._stopping
        if loop is not None and stopping is not None:
            try:
                loop.call_soon_threadsafe(stopping.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _apply_status(self) -> None:
        with self._lock:
            status = self._status
        self.item.set_status(status)

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        finally:
            self._started.set()

    async def _main(self) -> None:
        try:
            bus = MessageBus(bus_address=self._bus_address, bus_type=BusType.SESSION)
            await bus.connect()
        except Exception as e:
            self.error = f"no session bus: {e}"
            return
        try:
            bus.export(OBJECT_PATH, self.item)
            bus.export(dbusmenu.OBJECT_PATH, self.menu)
            bus.add_message_handler(lambda message: self._on_message(bus, message))
            await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[_NAME_OWNER_CHANGED_RULE],
                )
            )
            self.unique_name = bus.unique_name
            with self._lock:
                self._loop = asyncio.get_running_loop()
                self._stopping = asyncio.Event()
                stopping = self._stopping
            self._apply_status()
            await self._register(bus)
            self._started.set()
            await stopping.wait()
        finally:
            bus.disconnect()
            try:
                await bus.wait_for_disconnect()
            except Exception:
                pass
            with self._lock:
                self._loop = None
            self._set_registered(False)

    def _on_message(self, bus: MessageBus, message: Message) -> None:
        if (
            message.message_type == MessageType.SIGNAL
            and message.member == "NameOwnerChanged"
            and message.body
            and message.body[0] == WATCHER_NAME
        ):
            if message.body[2]:
                # A new watcher knows nothing of this item.
                asyncio.get_running_loop().create_task(self._register(bus))
            else:
                self.error = "the status notifier host left the session bus"
                self._set_registered(False)

    async def _register(self, bus: MessageBus) -> None:
        reply = await bus.call(
            Message(
                destination=WATCHER_NAME,
                path=WATCHER_PATH,
                interface=WATCHER_NAME,
                member="RegisterStatusNotifierItem",
                signature="s",
                body=[bus.unique_name],
                flags=MessageFlag.NO_AUTOSTART,
            )
        )
        if reply.message_type == MessageType.ERROR:
            self.error = f"no status notifier host on the session bus ({reply.error_name})"
            self._set_registered(False)
        else:
            self.error = None
            self._set_registered(True)

    def _set_registered(self, registered: bool) -> None:
        if registered != self.registered:
            self.registered = registered
            self._on_registered(registered)


def print_unavailable(service: TrayService) -> None:
    """Tell the user on stderr why there is no top-bar item."""
    print(
        f"HydraPing: no top bar item, {service.error or 'the session bus did not answer'}. "
        "Closing the window quits HydraPing.",
        file=sys.stderr,
    )
