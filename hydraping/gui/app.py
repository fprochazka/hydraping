"""The desktop app: Qt in the main thread, the engine in its own thread, snapshots in between."""

import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from hydraping.core.config import Config
from hydraping.core.engine import Engine
from hydraping.gui.window import WINDOW_TITLE, MainWindow

# How often the window reads a snapshot from the engine.
REFRESH_MILLISECONDS = 500


class GuiApp:
    """Run the engine and show its snapshots in a ``MainWindow``.

    Qt owns the main thread. The engine probes on its own thread, and a ``QTimer`` reads a
    snapshot every ``REFRESH_MILLISECONDS``, so Qt code never touches the engine's state, only the
    immutable snapshots. Closing the window quits the app.
    """

    def __init__(self, config: Config, qt_app: QApplication):
        self.qt_app = qt_app
        self.engine = Engine(config)
        self.window = MainWindow()
        self.timer = QTimer()
        self.timer.setInterval(REFRESH_MILLISECONDS)
        self.timer.timeout.connect(self.refresh)

    def refresh(self) -> None:
        self.window.show_snapshot(self.engine.snapshot(columns=self.window.graph_columns()))

    def run(self) -> int:
        """Show the window and run Qt's event loop until the window closes or Ctrl+C.

        Returns the event loop's exit code. The engine stops on every way out.
        """
        # Qt's event loop doesn't run Python signal handlers by itself. The refresh timer wakes the
        # interpreter twice a second, which is when a Ctrl+C in the terminal gets handled.
        previous_handler = signal.signal(signal.SIGINT, lambda *_: self.qt_app.quit())
        try:
            self.engine.start()
            self.refresh()
            self.window.show()
            self.timer.start()
            return self.qt_app.exec()
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            self.timer.stop()
            self.engine.stop()


def run_gui(config: Config) -> int:
    """Run the desktop app for ``config`` and return its exit code."""
    qt_app = QApplication.instance()
    if qt_app is None:
        qt_app = QApplication(sys.argv[:1])
    assert isinstance(qt_app, QApplication)
    qt_app.setApplicationName(WINDOW_TITLE)
    # The Wayland app id, which the shell uses to group the window and to find its icon.
    qt_app.setDesktopFileName("hydraping")
    return GuiApp(config, qt_app).run()
