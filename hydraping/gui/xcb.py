"""Check for the system library that Qt's X11 plugin needs, without importing Qt.

Qt 6.5 and later load ``libxcb-cursor.so.0`` for the xcb platform plugin, and the PySide6 wheels
don't bundle it. Without it, Qt prints a message and aborts the process with a core dump, so the
check has to run before the ``QApplication`` is created.
"""

import ctypes
import os
from collections.abc import Callable, Mapping

XCB_CURSOR_LIBRARY = "libxcb-cursor.so.0"
XCB_CURSOR_HINT = (
    "The desktop window needs libxcb-cursor0 on X11. "
    "Install it with: sudo apt install libxcb-cursor0"
)


def can_load_library(name: str) -> bool:
    """Return whether the dynamic linker can load the shared library ``name``."""
    try:
        ctypes.CDLL(name)
    except OSError:
        return False
    return True


def uses_xcb(environ: Mapping[str, str]) -> bool:
    """Return whether Qt will pick its xcb (X11) platform plugin in the environment ``environ``.

    An explicit ``QT_QPA_PLATFORM`` decides: one that starts with ``xcb`` uses it, any other value
    (``offscreen``, ``wayland`` and so on) doesn't. Without one, Qt uses xcb unless the session is
    a Wayland session with a Wayland display.
    """
    platform = environ.get("QT_QPA_PLATFORM", "")
    if platform:
        return platform.startswith("xcb")
    wayland_session = environ.get("XDG_SESSION_TYPE") == "wayland"
    return not (wayland_session and environ.get("WAYLAND_DISPLAY"))


def missing_xcb_cursor(
    environ: Mapping[str, str] = os.environ,
    can_load: Callable[[str], bool] = can_load_library,
) -> bool:
    """Return whether Qt will use xcb in ``environ`` and ``can_load`` can't load libxcb-cursor."""
    return uses_xcb(environ) and not can_load(XCB_CURSOR_LIBRARY)
