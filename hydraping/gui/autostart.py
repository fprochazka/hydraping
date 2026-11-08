"""The login autostart entry: ``$XDG_CONFIG_HOME/autostart/hydraping.desktop``.

Desktops that follow the XDG Autostart spec, GNOME among them, run the ``Exec`` line of every
entry in that directory at login. This module needs no GUI package, so the entry can be removed
even after the ``gui`` extra is gone.
"""

import os
import shutil
import sys
from pathlib import Path

FILE_NAME = "hydraping.desktop"

# Characters that make the Desktop Entry spec require quoting an Exec argument.
_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")
# Inside quotes, these keep their meaning unless a backslash precedes them.
_ESCAPED_IN_QUOTES = set('"`$\\')


def xdg_home(variable: str, default: str) -> Path:
    """Return the directory in ``$variable``, or ``~/default`` when it is unset, empty or relative.

    The XDG Base Directory spec says to ignore a relative value.
    """
    value = os.environ.get(variable, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / default


def config_home() -> Path:
    """Return ``$XDG_CONFIG_HOME``, or ``~/.config``."""
    return xdg_home("XDG_CONFIG_HOME", ".config")


def entry_path() -> Path:
    return config_home() / "autostart" / FILE_NAME


def hydraping_executable() -> Path:
    """Return the absolute path of the ``hydraping`` command that runs this process.

    Symlinks stay as they are: ``uv tool install`` links ``~/.local/bin/hydraping`` into the tool's
    environment, and the link keeps working when the tool is reinstalled. When this process isn't
    the ``hydraping`` script, for example under ``python -m hydraping``, the command is looked up
    on ``PATH``. Raises ``FileNotFoundError`` if there is none.
    """
    candidates = [sys.argv[0], "hydraping"] if sys.argv and sys.argv[0] else ["hydraping"]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found is not None and not found.endswith(".py"):
            return Path(found).absolute()
    raise FileNotFoundError("the hydraping command is not on PATH")


def quote_exec_argument(argument: str) -> str:
    """Quote one ``Exec`` argument as the Desktop Entry spec requires.

    An argument with a reserved character goes in double quotes, with a backslash before each
    ``"``, `````, ``$`` and ``\\``. A ``%`` is doubled, because ``%`` starts a field code.
    """
    argument = argument.replace("%", "%%")
    if argument and not set(argument) & _RESERVED:
        return argument
    quoted = "".join(f"\\{char}" if char in _ESCAPED_IN_QUOTES else char for char in argument)
    return f'"{quoted}"'


def escape_string_value(value: str) -> str:
    """Escape a Desktop Entry string value; this applies on top of the ``Exec`` quoting."""
    return (
        value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    )


def exec_line(executable: Path, config: Path | None, *, hidden: bool) -> str:
    """Return the ``Exec`` value that starts the GUI, with ``config`` if one was given."""
    arguments = [str(executable), "gui"] + (["--hidden"] if hidden else [])
    if config is not None:
        # The desktop runs the entry from the home directory, not from the current one.
        arguments += ["--config", str(config.absolute())]
    return escape_string_value(" ".join(quote_exec_argument(arg) for arg in arguments))


def entry_text(executable: Path, config: Path | None) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=HydraPing\n"
        "Comment=Show connection latency in the top bar\n"
        f"Exec={exec_line(executable, config, hidden=True)}\n"
        "Icon=hydraping\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def install(executable: Path, config: Path | None) -> Path:
    """Write the autostart entry, replacing an existing one, and return its path."""
    path = entry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entry_text(executable, config))
    return path


def remove() -> Path | None:
    """Delete the autostart entry and return its path, or None if there was none."""
    path = entry_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return path
