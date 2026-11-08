"""One running GUI per session: the app owns a well-known D-Bus name, and a second start asks the
owner to show its window.

The running app serves ``org.freedesktop.Application`` under that name, the interface desktops
use to activate an app. Its ``Activate`` carries the activation token that the launcher gave to
the second process, so on Wayland the running app may raise its window with it.
"""

import os
from collections.abc import Callable
from typing import Annotated, Any

from dbus_fast import Variant
from dbus_fast.annotations import DBusSignature
from dbus_fast.service import ServiceInterface, dbus_method

BUS_NAME = "io.github.fprochazka.HydraPing"
OBJECT_PATH = "/io/github/fprochazka/HydraPing"
INTERFACE = "org.freedesktop.Application"

# How long a second start waits for the running app to answer.
ACTIVATE_TIMEOUT_SECONDS = 5.0

PlatformData = Annotated[dict[str, Any], DBusSignature("a{sv}")]


def platform_data_from_environment() -> dict[str, Variant]:
    """Return the activation tokens that the launcher of this process left in its environment.

    ``XDG_ACTIVATION_TOKEN`` is the Wayland token, ``DESKTOP_STARTUP_ID`` the X11 startup id.
    """
    data = {}
    for key, variable in (
        ("activation-token", "XDG_ACTIVATION_TOKEN"),
        ("desktop-startup-id", "DESKTOP_STARTUP_ID"),
    ):
        value = os.environ.get(variable)
        if value:
            data[key] = Variant("s", value)
    return data


def activation_token(platform_data: dict[str, Variant]) -> str | None:
    """Return the Wayland activation token from ``platform_data``, if it has one."""
    token = platform_data.get("activation-token")
    if isinstance(token, Variant) and token.signature == "s" and token.value:
        return token.value
    return None


class ApplicationInterface(ServiceInterface):
    """``org.freedesktop.Application``, reduced to ``Activate``.

    ``on_activate`` runs on the D-Bus thread with the caller's activation token, or None.
    """

    def __init__(self, on_activate: Callable[[str | None], None]):
        super().__init__(INTERFACE)
        self._on_activate = on_activate

    @dbus_method()
    def Activate(self, platform_data: PlatformData) -> None:
        self._on_activate(activation_token(platform_data))
