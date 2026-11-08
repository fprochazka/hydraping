"""Tests for the login autostart entry, written into a temporary ``XDG_CONFIG_HOME``."""

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

from hydraping.gui import autostart


@pytest.fixture
def config_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "config home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


def read_entry(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # Desktop Entry keys are case-sensitive.
    parser.read_string(path.read_text())
    assert parser.sections() == ["Desktop Entry"]
    return dict(parser["Desktop Entry"])


def split_exec(value: str) -> list[str]:
    """Undo the string escapes and the ``Exec`` quoting, the way the Desktop Entry spec reads them.

    Written from the spec, not from the code under test, so the two can't share a mistake.
    """
    string_escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    unescaped, chars = "", iter(value)
    for char in chars:
        unescaped += string_escapes[next(chars)] if char == "\\" else char

    arguments: list[str] = []
    current: str | None = None
    quoted = False
    chars = iter(unescaped)
    for char in chars:
        if quoted:
            if char == "\\":
                escaped = next(chars)
                assert escaped in '"`$\\', f"stray backslash before {escaped!r}"
                current += escaped
            elif char == '"':
                quoted = False
            else:
                current += char
        elif char == " ":
            if current is not None:
                arguments.append(current)
            current = None
        elif char == '"':
            quoted, current = True, current or ""
        else:
            current = (current or "") + char
    assert not quoted, "unterminated quote"
    if current is not None:
        arguments.append(current)
    return [argument.replace("%%", "%") for argument in arguments]


class TestInstall:
    def test_writes_an_entry_that_starts_the_gui_hidden(self, config_home, tmp_path):
        executable = tmp_path / "bin dir" / "hydraping"

        path = autostart.install(executable, None)

        assert path == config_home / "autostart" / "hydraping.desktop"
        entry = read_entry(path)
        assert entry["Type"] == "Application"
        assert entry["Name"] == "HydraPing"
        assert entry["Icon"] == "network-transmit-receive"
        assert entry["X-GNOME-Autostart-enabled"] == "true"
        assert split_exec(entry["Exec"]) == [str(executable), "gui", "--hidden"]

    def test_passes_the_config_as_an_absolute_path(self, config_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        path = autostart.install(Path("/opt/hydraping/bin/hydraping"), Path("settings.toml"))

        assert split_exec(read_entry(path)["Exec"]) == [
            "/opt/hydraping/bin/hydraping",
            "gui",
            "--hidden",
            "--config",
            str(tmp_path / "settings.toml"),
        ]

    def test_quotes_reserved_characters(self, config_home):
        executable = Path('/opt/odd $dir "with" `quotes` \\ and 100%/hydraping')

        path = autostart.install(executable, None)

        assert split_exec(read_entry(path)["Exec"]) == [str(executable), "gui", "--hidden"]

    def test_replaces_an_existing_entry(self, config_home):
        autostart.install(Path("/old/hydraping"), None)

        path = autostart.install(Path("/new/hydraping"), None)

        assert split_exec(read_entry(path)["Exec"])[0] == "/new/hydraping"

    @pytest.mark.skipif(
        shutil.which("desktop-file-validate") is None, reason="desktop-file-validate missing"
    )
    def test_the_entry_passes_desktop_file_validate(self, config_home):
        path = autostart.install(Path("/opt/odd dir/hydraping"), Path("/etc/hydra ping.toml"))

        result = subprocess.run(
            ["desktop-file-validate", str(path)], capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == ""


class TestRemove:
    def test_deletes_the_entry(self, config_home):
        path = autostart.install(Path("/opt/hydraping/bin/hydraping"), None)

        assert autostart.remove() == path
        assert not path.exists()

    def test_without_an_entry(self, config_home):
        assert autostart.remove() is None


class TestPaths:
    def test_config_home_defaults_to_dot_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        for value in ("", "relative/dir"):
            monkeypatch.setenv("XDG_CONFIG_HOME", value)
            assert autostart.config_home() == tmp_path / ".config"

    def test_executable_is_the_running_script(self, monkeypatch, tmp_path):
        script = tmp_path / "hydraping"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        monkeypatch.setattr("sys.argv", [str(script), "gui"])

        assert autostart.hydraping_executable() == script

    def test_executable_falls_back_to_path_under_python_m(self, monkeypatch, tmp_path):
        script = tmp_path / "bin" / "hydraping"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", str(script.parent))
        monkeypatch.setattr("sys.argv", [str(tmp_path / "hydraping" / "__main__.py")])

        assert autostart.hydraping_executable() == script

    def test_executable_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["-c"])

        with pytest.raises(FileNotFoundError):
            autostart.hydraping_executable()
