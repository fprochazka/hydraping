"""The top-bar item's menu, served over the ``com.canonical.dbusmenu`` D-Bus interface.

The host asks for the menu's items with ``GetLayout`` and ``GetGroupProperties``, and reports a
click with ``Event``. The menu here never changes, so its layout revision stays the same and
``LayoutUpdated`` is never sent.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from dbus_fast import DBusError, ErrorType, PropertyAccess, Variant
from dbus_fast.annotations import (
    DBusBool,
    DBusInt32,
    DBusSignature,
    DBusStr,
    DBusUInt32,
    DBusVariant,
)
from dbus_fast.service import ServiceInterface, dbus_method, dbus_property, dbus_signal

INTERFACE = "com.canonical.dbusmenu"
OBJECT_PATH = "/MenuBar"
# The version of the dbusmenu protocol this menu implements.
PROTOCOL_VERSION = 3
LAYOUT_REVISION = 1
ROOT_ID = 0

LAYOUT_SIGNATURE = "(ia{sv}av)"
Layout = Annotated[list[Any], DBusSignature(LAYOUT_SIGNATURE)]
GroupProperties = Annotated[list[Any], DBusSignature("a(ia{sv})")]
IntList = Annotated[list[int], DBusSignature("ai")]
StrList = Annotated[list[str], DBusSignature("as")]


@dataclass(frozen=True)
class MenuItem:
    id: int
    label: str
    action: Callable[[], None]

    def properties(self) -> dict[str, Variant]:
        return {
            "type": Variant("s", "standard"),
            "label": Variant("s", self.label),
            "enabled": Variant("b", True),
            "visible": Variant("b", True),
        }


ROOT_PROPERTIES = {"children-display": Variant("s", "submenu")}


def _filter(properties: dict[str, Variant], names: Sequence[str]) -> dict[str, Variant]:
    """Return the requested properties; an empty list of names asks for all of them."""
    if not names:
        return dict(properties)
    return {name: value for name, value in properties.items() if name in names}


class Menu:
    """A flat menu: the root and its items, without the D-Bus plumbing, so it can be tested."""

    def __init__(self, items: Sequence[MenuItem]):
        self.items = {item.id: item for item in items}
        if ROOT_ID in self.items:
            raise ValueError(f"Menu item id {ROOT_ID} is the root's")

    def properties(self, item_id: int) -> dict[str, Variant]:
        if item_id == ROOT_ID:
            return dict(ROOT_PROPERTIES)
        return self.items[item_id].properties()

    def has(self, item_id: int) -> bool:
        return item_id == ROOT_ID or item_id in self.items

    def layout(self, parent_id: int, depth: int, names: Sequence[str]) -> list[Any]:
        """Return the ``(ia{sv}av)`` layout of ``parent_id`` with children ``depth`` levels deep;
        a negative depth means all of them."""
        if not self.has(parent_id):
            raise KeyError(parent_id)
        children = []
        if parent_id == ROOT_ID and depth != 0:
            children = [
                Variant(LAYOUT_SIGNATURE, [item.id, _filter(item.properties(), names), []])
                for item in self.items.values()
            ]
        return [parent_id, _filter(self.properties(parent_id), names), children]

    def group_properties(self, ids: Sequence[int], names: Sequence[str]) -> list[Any]:
        """Return ``a(ia{sv})`` for ``ids``, skipping unknown ones; no ids means every item."""
        if not ids:
            ids = [ROOT_ID, *self.items]
        return [[i, _filter(self.properties(i), names)] for i in ids if self.has(i)]

    def event(self, item_id: int, event_id: str) -> bool:
        """Handle an event on an item; return False when there is no such item."""
        if not self.has(item_id):
            return False
        if event_id == "clicked" and item_id in self.items:
            self.items[item_id].action()
        return True


def _invalid_id(item_id: int) -> DBusError:
    return DBusError(ErrorType.INVALID_ARGS, f"No menu item with id {item_id}")


class DBusMenuInterface(ServiceInterface):
    """``com.canonical.dbusmenu`` for a ``Menu``. Its methods run on the D-Bus thread, so the
    items' actions must be safe to call from there."""

    def __init__(self, menu: Menu):
        super().__init__(INTERFACE)
        self.menu = menu

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> DBusUInt32:
        return PROTOCOL_VERSION

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> DBusStr:
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> DBusStr:
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> StrList:
        return []

    @dbus_method()
    def GetLayout(
        self, parentId: DBusInt32, recursionDepth: DBusInt32, propertyNames: StrList
    ) -> Annotated[list[Any], DBusSignature("u" + LAYOUT_SIGNATURE)]:
        try:
            layout = self.menu.layout(parentId, recursionDepth, propertyNames)
        except KeyError:
            raise _invalid_id(parentId) from None
        return [LAYOUT_REVISION, layout]

    @dbus_method()
    def GetGroupProperties(self, ids: IntList, propertyNames: StrList) -> GroupProperties:
        return self.menu.group_properties(ids, propertyNames)

    @dbus_method()
    def GetProperty(self, id: DBusInt32, name: DBusStr) -> DBusVariant:
        if not self.menu.has(id):
            raise _invalid_id(id)
        properties = self.menu.properties(id)
        if name not in properties:
            raise DBusError(ErrorType.INVALID_ARGS, f"Menu item {id} has no property {name!r}")
        return properties[name]

    @dbus_method()
    def Event(
        self, id: DBusInt32, eventId: DBusStr, data: DBusVariant, timestamp: DBusUInt32
    ) -> None:
        if not self.menu.event(id, eventId):
            raise _invalid_id(id)

    @dbus_method()
    def EventGroup(self, events: Annotated[list[Any], DBusSignature("a(isvu)")]) -> IntList:
        # Every event runs; the reply lists the ids that have no item.
        return [
            item_id for item_id, event_id, _, _ in events if not self.menu.event(item_id, event_id)
        ]

    @dbus_method()
    def AboutToShow(self, id: DBusInt32) -> DBusBool:
        if not self.menu.has(id):
            raise _invalid_id(id)
        return False

    @dbus_method()
    def AboutToShowGroup(self, ids: IntList) -> Annotated[list[Any], DBusSignature("aiai")]:
        return [[], [item_id for item_id in ids if not self.menu.has(item_id)]]

    @dbus_signal()
    def ItemsPropertiesUpdated(self) -> Annotated[list[Any], DBusSignature("a(ia{sv})a(ias)")]:
        return [[], []]

    @dbus_signal()
    def LayoutUpdated(self) -> Annotated[list[Any], DBusSignature("ui")]:
        return [LAYOUT_REVISION, ROOT_ID]

    @dbus_signal()
    def ItemActivationRequested(self) -> Annotated[list[Any], DBusSignature("iu")]:
        return [ROOT_ID, 0]
