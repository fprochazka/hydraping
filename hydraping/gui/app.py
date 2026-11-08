"""The desktop app: Qt in the main thread, the engine and the top-bar item in their own threads."""

import os
import signal
import sys
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication

from hydraping.core.config import Config
from hydraping.core.engine import Engine
from hydraping.gui import instance, launcher
from hydraping.gui.tray import TrayService, print_unavailable
from hydraping.gui.tray_status import tray_status
from hydraping.gui.window import WINDOW_TITLE, MainWindow

# How often the window reads a snapshot from the engine. The top-bar item gets one of these
# snapshots at most every ``ui.tray_refresh_seconds``.
REFRESH_MILLISECONDS = 500
# How long a hidden window waits for a top-bar host before it shows. A host that restarts comes
# back within this time, and at login the host may start a few seconds after HydraPing.
TRAY_GRACE_MILLISECONDS = 8_000


class _TrayBridge(QObject):
    """Carry the top-bar item's callbacks from the D-Bus thread to Qt's thread.

    The D-Bus thread only emits these signals. Their queued connections run the slots in Qt's
    thread, so no Qt widget is touched from the D-Bus thread.
    """

    open_requested = Signal(str)
    quit_requested = Signal()
    registered_changed = Signal(bool)


class GuiApp:
    """Run the engine and show its snapshots in a ``MainWindow`` and in the top bar.

    Qt owns the main thread. The engine probes on its own thread, and a ``QTimer`` reads a
    snapshot every ``REFRESH_MILLISECONDS``, so Qt code never touches the engine's state, only the
    immutable snapshots. The top-bar item runs on a third thread and gets plain status values,
    at most once per ``ui.tray_refresh_seconds``, because a label and an icon that change with
    every probe flicker. The first refresh updates it at once.

    While a host shows the top-bar item, closing the window hides it, and Quit is in the item's
    menu. Without a host, closing the window quits the app. ``bus_address`` picks the D-Bus
    session bus; None means the one of the user's session.

    Only one app runs per session: if another one owns ``instance.BUS_NAME``, ``run()`` asks it to
    show its window and returns without probing. With ``hidden``, the window stays hidden at the
    start while a host shows the top-bar item, and a second hidden start shows nothing.

    The app is never left without a way to reach it: while the window is hidden and no host shows
    the item, the window shows after ``TRAY_GRACE_MILLISECONDS``, unless a host appears first.
    """

    def __init__(
        self,
        config: Config,
        qt_app: QApplication,
        bus_address: str | None = None,
        hidden: bool = False,
    ):
        self.qt_app = qt_app
        self.hidden = hidden
        self.engine = Engine(config)
        self.tray_refresh_seconds = config.ui.tray_refresh_seconds
        self._tray_updated_at: float | None = None
        self.window = MainWindow()
        self.window.closed.connect(self.quit)
        self.timer = QTimer()
        self.timer.setInterval(REFRESH_MILLISECONDS)
        self.timer.timeout.connect(self.refresh)
        self._tray_grace = QTimer()
        self._tray_grace.setSingleShot(True)
        self._tray_grace.timeout.connect(self._on_tray_grace_over)

        self._bridge = _TrayBridge()
        queued = Qt.ConnectionType.QueuedConnection
        self._bridge.open_requested.connect(self.open_window, queued)
        self._bridge.quit_requested.connect(self.quit, queued)
        self._bridge.registered_changed.connect(self._on_tray_registered, queued)
        self.tray = TrayService(
            on_activate=lambda token: self._bridge.open_requested.emit(token or ""),
            on_quit=self._bridge.quit_requested.emit,
            on_registered=self._bridge.registered_changed.emit,
            bus_address=bus_address,
            bus_name=instance.BUS_NAME,
        )

    def refresh(self) -> None:
        snapshot = self.engine.snapshot(columns=self.window.graph_columns())
        self.window.show_snapshot(snapshot)
        now = time.monotonic()
        if (
            self._tray_updated_at is None
            or now - self._tray_updated_at >= self.tray_refresh_seconds
        ):
            self._tray_updated_at = now
            self.tray.update(tray_status(snapshot))

    def open_window(self, activation_token: str = "") -> None:
        """Show the window and ask the desktop to bring it to the front."""
        if activation_token and QGuiApplication.platformName() == "wayland":
            # Qt's Wayland shell takes the token from this variable when it activates a window.
            # Without one, the desktop may leave the window behind the others.
            os.environ["XDG_ACTIVATION_TOKEN"] = activation_token
        if self.window.isMinimized():
            self.window.showNormal()
        else:
            self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def quit(self) -> None:
        self.qt_app.quit()

    def _on_tray_registered(self, registered: bool) -> None:
        self.window.hide_on_close = registered
        if registered:
            self._tray_grace.stop()
        elif self.window.isHidden():
            self._start_tray_grace()

    def _start_tray_grace(self) -> None:
        # Read at start, so tests can shorten it.
        self._tray_grace.start(TRAY_GRACE_MILLISECONDS)

    def _on_tray_grace_over(self) -> None:
        if not self.tray.registered:
            self.open_window()

    def run(self) -> int:
        """Show the window and run Qt's event loop until Quit, Ctrl+C, or closing the window
        while no top-bar item is shown.

        Returns the event loop's exit code, or the hand-over's when another instance runs. The
        engine and the top-bar item stop on every way out.
        """
        # Qt's event loop doesn't run Python signal handlers by itself. The refresh timer wakes the
        # interpreter twice a second, which is when a Ctrl+C in the terminal gets handled.
        previous_handler = signal.signal(signal.SIGINT, lambda *_: self.quit())
        # The top-bar item replaces the window as the way to quit.
        previous_quit_on_close = self.qt_app.quitOnLastWindowClosed()
        self.qt_app.setQuitOnLastWindowClosed(False)
        try:
            self.tray.start()
            if self.tray.running_elsewhere:
                return self._hand_over()
            self.engine.start()
            if not self.tray.registered:
                print_unavailable(self.tray)
            # The callback may still be queued; the window must know before it can be closed.
            self.window.hide_on_close = self.tray.registered
            self.refresh()
            if not self.hidden:
                self.window.show()
            elif not self.tray.registered:
                # The host may still be starting, as at login; without one, show the window.
                self._start_tray_grace()
            self.timer.start()
            return self.qt_app.exec()
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            self.qt_app.setQuitOnLastWindowClosed(previous_quit_on_close)
            self.timer.stop()
            self._tray_grace.stop()
            self.tray.stop()
            self.engine.stop()

    def _hand_over(self) -> int:
        """Ask the running instance to show its window, unless hidden, and return the exit code."""
        if self.hidden:
            print("HydraPing is already running.", file=sys.stderr)
            return 0
        error = self.tray.activate_running_instance(instance.platform_data_from_environment())
        if error is not None:
            print(f"HydraPing is already running, but did not answer: {error}", file=sys.stderr)
            return 1
        print("HydraPing is already running; its window is shown now.", file=sys.stderr)
        return 0


def application_icon() -> QIcon:
    """Return the packaged SVG icon, which Qt renders at every size it is asked for.

    Qt reads the file when it first draws the icon, so the file has to stay where it is. It does
    for a package installed as files, the way uv and pip install it.
    """
    return QIcon(str(launcher.icon_resource()))


def run_gui(config: Config, hidden: bool = False) -> int:
    """Run the desktop app for ``config`` and return its exit code."""
    qt_app = QApplication.instance()
    if qt_app is None:
        qt_app = QApplication(sys.argv[:1])
    assert isinstance(qt_app, QApplication)
    qt_app.setApplicationName(WINDOW_TITLE)
    # The Wayland app id, which the shell uses to group the window and to find its icon.
    qt_app.setDesktopFileName("hydraping")
    # The icon of every window, for desktops that don't take it from the launcher.
    qt_app.setWindowIcon(application_icon())
    return GuiApp(config, qt_app, hidden=hidden).run()
