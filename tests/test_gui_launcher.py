"""Tests for the application launcher and its icon, written into a temporary ``XDG_DATA_HOME``."""

import shutil
import subprocess
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from hydraping.core.graphcells import LatencyColor
from hydraping.gui import launcher
from hydraping.gui.colors import LATENCY_RGB, hex_color
from tests.test_gui_autostart import read_entry, split_exec

SVG = "{http://www.w3.org/2000/svg}"


@pytest.fixture
def data_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "data home"
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    return home


class TestInstall:
    def test_writes_an_entry_that_starts_the_gui(self, data_home, tmp_path):
        executable = tmp_path / "bin dir" / "hydraping"

        entry_path, _ = launcher.install(executable, None)

        assert entry_path == data_home / "applications" / "hydraping.desktop"
        entry = read_entry(entry_path)
        assert split_exec(entry.pop("Exec")) == [str(executable), "gui"]
        assert entry == {
            "Type": "Application",
            "Name": "HydraPing",
            "GenericName": "Connection monitor",
            "Comment": "Show the latency of DNS, TCP, TLS and HTTP checks live",
            "Icon": "hydraping",
            "Terminal": "false",
            "Categories": "Network;Monitor;",
            "Keywords": "ping;latency;network;vpn;",
            "StartupNotify": "true",
            "StartupWMClass": "hydraping",
        }

    def test_passes_the_config_as_an_absolute_path(self, data_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        entry_path, _ = launcher.install(Path("/opt/odd $dir/hydraping"), Path("settings.toml"))

        assert split_exec(read_entry(entry_path)["Exec"]) == [
            "/opt/odd $dir/hydraping",
            "gui",
            "--config",
            str(tmp_path / "settings.toml"),
        ]

    def test_copies_the_packaged_icon(self, data_home):
        _, icon_path = launcher.install(Path("/opt/hydraping/bin/hydraping"), None)

        assert icon_path == data_home / "icons/hicolor/scalable/apps/hydraping.svg"
        assert icon_path.read_bytes() == launcher.icon_resource().read_bytes()

    def test_replaces_an_existing_launcher(self, data_home):
        launcher.install(Path("/old/hydraping"), None)

        entry_path, _ = launcher.install(Path("/new/hydraping"), None)

        assert split_exec(read_entry(entry_path)["Exec"])[0] == "/new/hydraping"

    @pytest.mark.skipif(
        shutil.which("desktop-file-validate") is None, reason="desktop-file-validate missing"
    )
    def test_the_entry_passes_desktop_file_validate(self, data_home):
        entry_path, _ = launcher.install(
            Path("/opt/odd dir/hydraping"), Path("/etc/hydra ping.toml")
        )

        result = subprocess.run(
            ["desktop-file-validate", str(entry_path)], capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == ""


class TestRemove:
    def test_deletes_the_launcher_and_the_icon(self, data_home):
        entry_path, icon_path = launcher.install(Path("/opt/hydraping/bin/hydraping"), None)

        assert launcher.remove() == (entry_path, icon_path)
        assert not entry_path.exists()
        assert not icon_path.exists()

    def test_without_a_launcher(self, data_home):
        assert launcher.remove() == (None, None)


def test_data_home_defaults_to_dot_local_share(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for value in ("", "relative/dir"):
        monkeypatch.setenv("XDG_DATA_HOME", value)
        assert launcher.data_home() == tmp_path / ".local" / "share"


class TestPackagedIcon:
    def test_is_a_plain_square_svg(self):
        root = ElementTree.fromstring(launcher.icon_resource().read_bytes())

        assert root.tag == f"{SVG}svg"
        assert root.get("viewBox") == "0 0 128 128"
        # Only shapes: no text, no metadata, no references to other files.
        assert {element.tag for element in root.iter()} == {f"{SVG}svg", f"{SVG}rect"}
        assert not any("href" in name for element in root.iter() for name in element.attrib)

    def test_uses_the_graph_colours(self):
        root = ElementTree.fromstring(launcher.icon_resource().read_bytes())

        fills = {element.get("fill") for element in root.iter(f"{SVG}rect")}

        for color in (LatencyColor.GREEN, LatencyColor.YELLOW, LatencyColor.ORANGE):
            assert hex_color(LATENCY_RGB[color]) in fills
