"""Tests for command routing. No test here starts the live dashboard."""

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from hydraping import cli
from hydraping.core.config import Config
from hydraping.core.models import IPEndpoint
from hydraping.gui import INSTALL_HINT

runner = CliRunner()


@pytest.fixture
def monitored(monkeypatch) -> list[Config]:
    """Record the configs the monitor command would run, instead of starting the dashboard."""
    started: list[Config] = []
    monkeypatch.setattr(cli, "start_monitoring", started.append)
    return started


@pytest.fixture
def gui_started(monkeypatch) -> list[tuple[Config, bool]]:
    """Record the configs and the ``hidden`` flags the gui command would run, instead of opening
    a window."""
    started: list[tuple[Config, bool]] = []

    def start_gui(cfg: Config, hidden: bool = False) -> int:
        started.append((cfg, hidden))
        return 0

    monkeypatch.setattr(cli, "start_gui", start_gui)
    return started


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('version = 2\n[endpoints]\ntargets = ["198.51.100.7"]\n')
    return path


def test_version_prints_version(monitored):
    result = runner.invoke(cli.app, ["version"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("hydraping version ")
    assert monitored == []


def test_init_writes_config(monitored, tmp_path):
    path = tmp_path / "settings.toml"

    result = runner.invoke(cli.app, ["init", "--config", str(path)])

    assert result.exit_code == 0, result.output
    assert path.exists()
    assert monitored == []


def test_options_before_command_name_go_to_the_command(monitored, tmp_path):
    path = tmp_path / "settings.toml"

    result = runner.invoke(cli.app, ["--config", str(path), "init"])

    assert result.exit_code == 0, result.output
    assert path.exists()
    assert monitored == []


def test_endpoint_arguments_override_config(monitored, config_file):
    result = runner.invoke(cli.app, ["192.0.2.1", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    [config] = monitored
    [endpoint] = config.endpoints
    assert isinstance(endpoint, IPEndpoint)
    assert endpoint.ip == "192.0.2.1"


def test_config_option_alone_monitors_configured_endpoints(monitored, config_file):
    result = runner.invoke(cli.app, ["--config", str(config_file)])

    assert result.exit_code == 0, result.output
    [config] = monitored
    assert [endpoint.raw for endpoint in config.endpoints] == ["198.51.100.7"]


def test_no_arguments_monitors_default_config(monitored, monkeypatch, config_file):
    monkeypatch.setattr(cli, "get_default_config_path", lambda: config_file)

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.output
    assert len(monitored) == 1


def test_help_lists_commands(monitored):
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "monitor" in result.output
    assert "version" in result.output
    assert "init" in result.output
    assert "gui" in result.output
    assert monitored == []


def test_command_name_after_separator_is_an_endpoint(monitored, config_file):
    result = runner.invoke(cli.app, ["--config", str(config_file), "--", "version"])

    assert result.exit_code == 0, result.output
    [config] = monitored
    assert [endpoint.raw for endpoint in config.endpoints] == ["version"]


def test_gui_command_runs_the_gui_with_the_config(monitored, gui_started, config_file):
    result = runner.invoke(cli.app, ["gui", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    [(config, hidden)] = gui_started
    assert [endpoint.raw for endpoint in config.endpoints] == ["198.51.100.7"]
    assert not hidden
    assert monitored == []


def test_gui_hidden_is_passed_on(gui_started, config_file):
    result = runner.invoke(cli.app, ["gui", "--hidden", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    [(_, hidden)] = gui_started
    assert hidden


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "config-home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


@pytest.fixture
def hydraping_script(tmp_path, monkeypatch):
    """Make ``sys.argv[0]`` an executable ``hydraping`` script, as when the command runs."""
    script = tmp_path / "bin" / "hydraping"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(script), "gui"])
    return script


def test_gui_install_autostart_writes_the_entry(
    gui_started, config_home, hydraping_script, config_file
):
    result = runner.invoke(cli.app, ["gui", "--install-autostart", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    entry = config_home / "autostart" / "hydraping.desktop"
    exec_line = f"{hydraping_script} gui --hidden --config {config_file}"
    assert f"Exec={exec_line}\n" in entry.read_text()
    assert result.output == (
        f"Wrote the autostart entry {entry}, which runs at login:\n  {exec_line}\n"
    )
    assert gui_started == []


def test_gui_install_autostart_checks_the_config(
    gui_started, config_home, hydraping_script, tmp_path
):
    broken = tmp_path / "broken.toml"
    broken.write_text("version = 2\n[checks]\nnot_a_key = 1\n")

    result = runner.invoke(cli.app, ["gui", "--install-autostart", "--config", str(broken)])

    assert result.exit_code == 1
    assert "not_a_key" in result.output
    assert not (config_home / "autostart").exists()


def test_gui_remove_autostart(gui_started, config_home, hydraping_script, config_file):
    runner.invoke(cli.app, ["gui", "--install-autostart", "--config", str(config_file)])
    entry = config_home / "autostart" / "hydraping.desktop"

    removed = runner.invoke(cli.app, ["gui", "--remove-autostart"])
    again = runner.invoke(cli.app, ["gui", "--remove-autostart"])

    assert removed.exit_code == 0, removed.output
    assert removed.output == f"Removed the autostart entry {entry}\n"
    assert not entry.exists()
    assert again.exit_code == 0, again.output
    assert again.output == f"No autostart entry to remove at {entry}\n"
    assert gui_started == []


def test_gui_autostart_options_exclude_each_other(gui_started, config_home):
    result = runner.invoke(cli.app, ["gui", "--install-autostart", "--remove-autostart"])

    assert result.exit_code == 2
    assert "use only one of" in result.output
    assert gui_started == []


def test_gui_without_pyside6_prints_the_install_hint():
    # sys.modules[name] = None makes both importing and finding the package fail, as when the
    # gui extra is not installed. A subprocess keeps this away from the PySide6 other tests use.
    code = (
        "import sys; sys.modules['PySide6'] = None; "
        "from hydraping.cli import app; app(['gui'], prog_name='hydraping')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=False
    )

    assert result.returncode == 1
    assert result.stderr.strip() == INSTALL_HINT
