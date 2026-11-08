"""The top-bar item on a private D-Bus daemon, read back by a client the way a host reads it."""

import threading

import pytest
from dbus_fast import Message, MessageType, Variant

from hydraping.gui import dbusmenu, instance
from hydraping.gui.tray import INTERFACE, OBJECT_PATH, TrayService
from hydraping.gui.tray_icon import ICON_SIZES
from hydraping.gui.tray_status import INITIAL_STATUS, LABEL_GUIDE, IconBar, TrayStatus
from tests.dbus_helpers import LoopThread, connect, disconnect, start_watcher, wait_until


class Calls:
    """Record the item's callbacks, which arrive on the item's thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.activated: list[str | None] = []
        self.quit = 0
        self.registered: list[bool] = []

    def on_activate(self, token: str | None) -> None:
        with self.lock:
            self.activated.append(token)

    def on_quit(self) -> None:
        with self.lock:
            self.quit += 1

    def on_registered(self, registered: bool) -> None:
        with self.lock:
            self.registered.append(registered)


@pytest.fixture
def loop():
    loop = LoopThread()
    yield loop
    loop.close()


@pytest.fixture
def watcher(private_bus, loop):
    bus, watcher = loop.run(start_watcher(private_bus))
    yield watcher
    if bus.connected:
        loop.run(disconnect(bus))


@pytest.fixture
def client(private_bus, loop):
    bus = loop.run(connect(private_bus))
    yield bus
    loop.run(disconnect(bus))


@pytest.fixture
def calls():
    return Calls()


@pytest.fixture
def tray(private_bus, calls):
    service = TrayService(
        on_activate=calls.on_activate,
        on_quit=calls.on_quit,
        on_registered=calls.on_registered,
        bus_address=private_bus,
    )
    yield service
    service.stop()


async def _proxy(bus, name: str, path: str, interface: str):
    node = await bus.introspect(name, path)
    return bus.get_proxy_object(name, path, node).get_interface(interface)


def item(loop, client, tray):
    return loop.run(_proxy(client, tray.unique_name, OBJECT_PATH, INTERFACE))


def menu(loop, client, tray):
    return loop.run(_proxy(client, tray.unique_name, dbusmenu.OBJECT_PATH, dbusmenu.INTERFACE))


class TestRegistration:
    def test_registers_its_unique_name_with_the_watcher(self, watcher, tray, calls):
        tray.start()

        assert tray.registered
        assert tray.error is None
        assert watcher.items == [tray.unique_name]
        assert calls.registered == [True]

    def test_registers_again_when_a_new_watcher_appears(self, private_bus, loop, tray, calls):
        bus, first = loop.run(start_watcher(private_bus))
        tray.start()
        assert first.items == [tray.unique_name]

        loop.run(disconnect(bus))
        wait_until(lambda: not tray.registered)
        bus, second = loop.run(start_watcher(private_bus))
        try:
            wait_until(lambda: tray.registered)
            assert second.items == [tray.unique_name]
            assert calls.registered == [True, False, True]
        finally:
            loop.run(disconnect(bus))

    def test_without_a_watcher_the_item_is_not_shown(self, private_bus, tray, calls):
        tray.start()

        assert not tray.registered
        assert (tray.error or "").startswith("no status notifier host on the session bus")
        assert calls.registered == []

    def test_without_a_session_bus_the_item_is_not_shown(self, tmp_path, calls):
        service = TrayService(
            on_activate=calls.on_activate,
            on_quit=calls.on_quit,
            bus_address=f"unix:path={tmp_path / 'missing'}",
        )
        service.start()
        service.update(INITIAL_STATUS)
        service.stop()

        assert not service.registered
        assert (service.error or "").startswith("no session bus")

    def test_stop_leaves_the_bus(self, watcher, tray, loop, client):
        tray.start()
        name = tray.unique_name

        tray.stop()

        reply = loop.run(
            client.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="NameHasOwner",
                    signature="s",
                    body=[name],
                )
            )
        )
        assert reply.message_type == MessageType.METHOD_RETURN
        assert reply.body == [False]


class TestItem:
    def test_properties(self, watcher, tray, loop, client):
        tray.start()
        proxy = item(loop, client, tray)

        assert loop.run(proxy.get_id()) == "hydraping"
        assert loop.run(proxy.get_category()) == "ApplicationStatus"
        assert loop.run(proxy.get_title()) == "HydraPing"
        assert loop.run(proxy.get_status()) == "Active"
        assert loop.run(proxy.get_item_is_menu()) is False
        assert loop.run(proxy.get_menu()) == dbusmenu.OBJECT_PATH
        assert loop.run(proxy.get_icon_name()) == ""
        assert loop.run(proxy.get_x_ayatana_label()) == "…"
        assert loop.run(proxy.get_x_ayatana_label_guide()) == LABEL_GUIDE
        pixmaps = loop.run(proxy.get_icon_pixmap())
        assert [(width, height, len(data)) for width, height, data in pixmaps] == [
            (size, size, size * size * 4) for size in ICON_SIZES
        ]
        assert list(loop.run(proxy.get_tool_tip())) == ["", [], "HydraPing", ""]

    def test_signatures_match_the_specification(self, watcher, tray, loop, client):
        tray.start()
        node = loop.run(client.introspect(tray.unique_name, OBJECT_PATH))
        [interface] = [i for i in node.interfaces if i.name == INTERFACE]

        properties = {p.name: p.signature for p in interface.properties}
        methods = {m.name: "".join(a.signature for a in m.in_args) for m in interface.methods}
        signals = {s.name: "".join(a.signature for a in s.args) for s in interface.signals}

        assert properties == {
            "Category": "s",
            "Id": "s",
            "Title": "s",
            "Status": "s",
            "WindowId": "i",
            "IconThemePath": "s",
            "Menu": "o",
            "ItemIsMenu": "b",
            "IconName": "s",
            "IconPixmap": "a(iiay)",
            "OverlayIconName": "s",
            "OverlayIconPixmap": "a(iiay)",
            "AttentionIconName": "s",
            "AttentionIconPixmap": "a(iiay)",
            "AttentionMovieName": "s",
            "ToolTip": "(sa(iiay)ss)",
            "XAyatanaLabel": "s",
            "XAyatanaLabelGuide": "s",
        }
        assert methods == {
            "ProvideXdgActivationToken": "s",
            "Activate": "ii",
            "SecondaryActivate": "ii",
            "ContextMenu": "ii",
            "Scroll": "is",
        }
        assert signals == {
            "NewTitle": "",
            "NewIcon": "",
            "NewToolTip": "",
            "NewStatus": "s",
            "XAyatanaNewLabel": "ss",
        }

    def test_activate_opens_the_window_with_the_activation_token(
        self, watcher, tray, loop, client, calls
    ):
        tray.start()
        proxy = item(loop, client, tray)

        loop.run(proxy.call_provide_xdg_activation_token("token-1"))
        loop.run(proxy.call_activate(10, 20))
        loop.run(proxy.call_activate(10, 20))

        assert calls.activated == ["token-1", None]

    def test_a_status_change_sends_the_changed_parts(self, watcher, tray, loop, client):
        tray.start()
        proxy = item(loop, client, tray)
        received: list[tuple] = []
        proxy.on_x_ayatana_new_label(lambda label, guide: received.append(("label", label, guide)))
        proxy.on_new_icon(lambda: received.append(("icon",)))
        proxy.on_new_tool_tip(lambda: received.append(("tooltip",)))

        status = TrayStatus("38 ms", "HydraPing", "Router: 38.0ms (ICMP)", INITIAL_STATUS.bars)
        tray.update(status)
        wait_until(lambda: len(received) == 2)
        assert received == [("tooltip",), ("label", "38 ms", LABEL_GUIDE)]
        assert loop.run(proxy.get_x_ayatana_label()) == "38 ms"

        received.clear()
        tray.update(status)  # Nothing changed, nothing is sent.
        tray.update(TrayStatus("38 ms", "HydraPing", status.tooltip, (IconBar((0, 0, 0), 1.0),)))
        wait_until(lambda: len(received) == 1)
        assert received == [("icon",)]


class TestMenu:
    def test_layout_lists_open_and_quit(self, watcher, tray, loop, client):
        tray.start()
        proxy = menu(loop, client, tray)

        revision, (root_id, root_properties, children) = loop.run(proxy.call_get_layout(0, -1, []))

        assert revision == dbusmenu.LAYOUT_REVISION
        assert root_id == 0
        assert root_properties["children-display"].value == "submenu"
        labels = [(child.value[0], child.value[1]["label"].value) for child in children]
        assert labels == [(1, "Open HydraPing"), (2, "Quit")]
        assert loop.run(proxy.get_version()) == dbusmenu.PROTOCOL_VERSION

    def test_clicks_open_the_window_and_quit(self, watcher, tray, loop, client, calls):
        tray.start()
        proxy = menu(loop, client, tray)

        assert loop.run(proxy.call_about_to_show(0)) is False
        loop.run(proxy.call_event(1, "clicked", Variant("s", ""), 0))
        loop.run(proxy.call_event(2, "clicked", Variant("s", ""), 0))

        assert calls.activated == [None]
        assert calls.quit == 1


class TestSingleInstance:
    def service(self, private_bus, calls) -> TrayService:
        return TrayService(
            on_activate=calls.on_activate,
            on_quit=calls.on_quit,
            on_registered=calls.on_registered,
            bus_address=private_bus,
            bus_name=instance.BUS_NAME,
        )

    def test_a_second_start_hands_the_activation_token_to_the_first(self, private_bus, watcher):
        first_calls, second_calls = Calls(), Calls()
        first = self.service(private_bus, first_calls)
        second = self.service(private_bus, second_calls)
        try:
            first.start()
            second.start()

            assert first.registered and not first.running_elsewhere
            assert second.running_elsewhere and not second.registered
            # Only the first instance shows an item in the top bar.
            assert watcher.items == [first.unique_name]

            error = second.activate_running_instance({"activation-token": Variant("s", "token-2")})
            assert error is None
            wait_until(lambda: first_calls.activated == ["token-2"])
            assert second_calls.activated == []
        finally:
            second.stop()
            first.stop()

    def test_the_name_is_free_again_after_stop(self, private_bus, calls):
        first = self.service(private_bus, calls)
        first.start()
        first.stop()
        second = self.service(private_bus, calls)
        try:
            second.start()
            assert not second.running_elsewhere
        finally:
            second.stop()

    def test_activate_without_a_token(self, private_bus, loop, client, calls):
        tray = self.service(private_bus, calls)
        try:
            tray.start()
            proxy = loop.run(
                _proxy(client, instance.BUS_NAME, instance.OBJECT_PATH, instance.INTERFACE)
            )
            loop.run(proxy.call_activate({}))
            wait_until(lambda: calls.activated == [None])
        finally:
            tray.stop()


class TestPlatformData:
    def test_reads_the_launcher_tokens_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "wayland-token")
        monkeypatch.setenv("DESKTOP_STARTUP_ID", "x11-id")

        data = instance.platform_data_from_environment()

        assert data == {
            "activation-token": Variant("s", "wayland-token"),
            "desktop-startup-id": Variant("s", "x11-id"),
        }
        assert instance.activation_token(data) == "wayland-token"

    def test_without_tokens(self, monkeypatch):
        monkeypatch.delenv("XDG_ACTIVATION_TOKEN", raising=False)
        monkeypatch.delenv("DESKTOP_STARTUP_ID", raising=False)

        assert instance.platform_data_from_environment() == {}
        assert instance.activation_token({}) is None
