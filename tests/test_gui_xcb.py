"""Tests for the libxcb-cursor check that runs before Qt starts."""

import pytest

from hydraping.gui.xcb import XCB_CURSOR_LIBRARY, can_load_library, missing_xcb_cursor


class Loader:
    """Report every library as ``present`` and record the names asked for."""

    def __init__(self, present: bool):
        self.present = present
        self.asked: list[str] = []

    def __call__(self, name: str) -> bool:
        self.asked.append(name)
        return self.present


X11_SESSION = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
WAYLAND_SESSION = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}


@pytest.mark.parametrize(
    "environ",
    [
        X11_SESSION,
        {},
        {**WAYLAND_SESSION, "QT_QPA_PLATFORM": "xcb"},
        {**X11_SESSION, "QT_QPA_PLATFORM": "xcb:display=:1"},
        {"XDG_SESSION_TYPE": "wayland", "QT_QPA_PLATFORM": ""},
        {"WAYLAND_DISPLAY": "wayland-0"},
    ],
    ids=[
        "x11",
        "no-session",
        "explicit-xcb",
        "xcb-with-args",
        "wayland-without-display",
        "display-without-wayland-session",
    ],
)
def test_xcb_without_the_library_is_missing(environ):
    can_load = Loader(present=False)

    assert missing_xcb_cursor(environ, can_load)
    assert can_load.asked == [XCB_CURSOR_LIBRARY]


def test_xcb_with_the_library_is_not_missing():
    assert not missing_xcb_cursor(X11_SESSION, Loader(present=True))


@pytest.mark.parametrize(
    "environ",
    [
        WAYLAND_SESSION,
        {**X11_SESSION, "QT_QPA_PLATFORM": "offscreen"},
        {**X11_SESSION, "QT_QPA_PLATFORM": "wayland"},
        {**X11_SESSION, "QT_QPA_PLATFORM": "minimal"},
    ],
    ids=["wayland", "offscreen", "explicit-wayland", "minimal"],
)
def test_other_platforms_skip_the_check(environ):
    can_load = Loader(present=False)

    assert not missing_xcb_cursor(environ, can_load)
    assert can_load.asked == []


def test_can_load_library():
    assert can_load_library("libc.so.6")
    assert not can_load_library("libhydraping-does-not-exist.so.0")
