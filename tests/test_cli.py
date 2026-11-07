"""Tests for command routing. No test here starts the live dashboard."""

import pytest
from typer.testing import CliRunner

from hydraping import cli
from hydraping.core.config import Config
from hydraping.core.models import IPEndpoint

runner = CliRunner()


@pytest.fixture
def monitored(monkeypatch) -> list[Config]:
    """Record the configs the monitor command would run, instead of starting the dashboard."""
    started: list[Config] = []
    monkeypatch.setattr(cli, "start_monitoring", started.append)
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
    assert monitored == []


def test_command_name_after_separator_is_an_endpoint(monitored, config_file):
    result = runner.invoke(cli.app, ["--config", str(config_file), "--", "version"])

    assert result.exit_code == 0, result.output
    [config] = monitored
    assert [endpoint.raw for endpoint in config.endpoints] == ["version"]
