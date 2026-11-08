"""The desktop GUI.

Importing this package needs none of the GUI's dependencies, so the CLI can check for them and
print an install hint. ``hydraping.gui.app`` and the modules it imports need PySide6.
"""

import importlib.util

INSTALL_HINT = "Install the GUI with: uv tool install 'hydraping[gui]'"

# The import names of the packages that the ``gui`` extra installs.
GUI_PACKAGES = ("PySide6", "dbus_fast")


def missing_gui_packages() -> list[str]:
    """Return the GUI packages that can't be imported, in ``GUI_PACKAGES`` order."""
    missing = []
    for name in GUI_PACKAGES:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            missing.append(name)
    return missing
