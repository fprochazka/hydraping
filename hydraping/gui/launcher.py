"""The application launcher: ``$XDG_DATA_HOME/applications/hydraping.desktop`` and its icon.

The launcher puts HydraPing in the desktop's application list, and the icon theme lookup finds
``Icon=hydraping`` in ``$XDG_DATA_HOME/icons/hicolor/scalable/apps``. Like ``autostart``, this
module needs no GUI package, so the launcher can be removed after the ``gui`` extra is gone.
"""

import shutil
import subprocess
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from hydraping.gui.autostart import exec_line, xdg_home

FILE_NAME = "hydraping.desktop"
ICON_NAME = "hydraping"
# How long a cache update may take before it is left alone.
_CACHE_UPDATE_TIMEOUT_SECONDS = 30


def icon_resource() -> Traversable:
    """Return the packaged application icon, an SVG file."""
    return files("hydraping.gui").joinpath("resources", f"{ICON_NAME}.svg")


def data_home() -> Path:
    """Return ``$XDG_DATA_HOME``, or ``~/.local/share``."""
    return xdg_home("XDG_DATA_HOME", ".local/share")


def entry_path() -> Path:
    return data_home() / "applications" / FILE_NAME


def icon_path() -> Path:
    return data_home() / "icons" / "hicolor" / "scalable" / "apps" / f"{ICON_NAME}.svg"


def entry_text(executable: Path, config: Path | None) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=HydraPing\n"
        "GenericName=Connection monitor\n"
        "Comment=Show the latency of DNS, TCP, TLS and HTTP checks live\n"
        f"Exec={exec_line(executable, config, hidden=False)}\n"
        f"Icon={ICON_NAME}\n"
        "Terminal=false\n"
        "Categories=Network;Monitor;\n"
        "Keywords=ping;latency;network;vpn;\n"
        "StartupNotify=true\n"
        # Qt sets the window's class and Wayland app id from setDesktopFileName("hydraping").
        "StartupWMClass=hydraping\n"
    )


def install(executable: Path, config: Path | None) -> tuple[Path, Path]:
    """Write the launcher and the icon, replacing existing ones, and return their paths."""
    icon = icon_path()
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes(icon_resource().read_bytes())
    entry = entry_path()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(entry_text(executable, config))
    update_caches()
    return entry, icon


def remove() -> tuple[Path | None, Path | None]:
    """Delete the launcher and the icon, and return the paths of those that existed."""
    removed = tuple(_unlink(path) for path in (entry_path(), icon_path()))
    update_caches()
    return removed


def update_caches() -> None:
    """Refresh the desktop's caches of the user's launchers and icons, ignoring any failure.

    ``update-desktop-database`` indexes the launchers by MIME type. The icon cache is updated
    only when one exists: GTK checks only the theme directory's time to tell whether the cache
    is current, so a cache that HydraPing creates could later hide icons that another program
    adds without updating it. Without a cache, GTK reads the directories.
    """
    _run_quietly("update-desktop-database", "-q", str(entry_path().parent))
    theme = icon_path().parents[2]
    if (theme / "icon-theme.cache").exists():
        _run_quietly("gtk-update-icon-cache", "-q", "-f", "-t", str(theme))


def _run_quietly(command: str, *arguments: str) -> None:
    executable = shutil.which(command)
    if executable is None:
        return
    try:
        subprocess.run(
            [executable, *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_CACHE_UPDATE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _unlink(path: Path) -> Path | None:
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return path
