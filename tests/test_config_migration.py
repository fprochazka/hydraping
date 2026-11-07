"""Tests for the automatic migration of v1 settings files."""

import os
import stat

import pytest

from hydraping.core.config import Config, migrate_v1_config

V1 = """# HydraPing Configuration

[endpoints]
# List of endpoints to monitor
targets = [
    { url = "192.0.2.1", name = "Router" },  # the box in the hallway
    "example.com",
]

[dns]
# Optional: custom DNS servers
custom_servers = ["192.0.2.53"]

[checks]
# How often to perform checks (in seconds)
interval_seconds = 3.0  # three is plenty

# Timeout for individual checks (in seconds)
timeout_seconds = 2.5

[ui]
graph_width = 0
"""


@pytest.fixture
def v1_file(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(V1)
    return path


def backups(path):
    return sorted(path.parent.glob(f"{path.name}.v1-backup-*"))


class TestMigrateV1Config:
    def test_rewrites_in_place_and_keeps_comments(self, v1_file):
        backup = migrate_v1_config(v1_file)

        migrated = v1_file.read_text()
        assert migrated.startswith("# HydraPing Configuration\nversion = 2\n")
        assert "# the box in the hallway" in migrated
        assert (
            "# How often to perform checks (in seconds)\nbucket_seconds = 3.0  # three is plenty\n"
        ) in migrated
        assert "# Optional: custom DNS servers\nservers = [" in migrated
        assert "probe_interval_seconds = 1.0" in migrated
        assert "interval_seconds = 3.0" not in migrated.replace("probe_interval_seconds", "")
        assert "custom_servers" not in migrated

        assert backup.parent == v1_file.parent
        assert backup.name.startswith("settings.toml.v1-backup-")
        assert backup.read_text() == V1

    def test_migrated_file_loads_with_the_old_values(self, v1_file):
        migrate_v1_config(v1_file)

        config = Config.load(v1_file)
        assert config.checks.bucket_seconds == 3.0
        assert config.checks.timeout_seconds == 2.5
        assert config.checks.probe_interval_seconds == 1.0
        assert config.dns.servers == ["192.0.2.53"]
        assert [endpoint.display_name for endpoint in config.endpoints] == ["Router", "example.com"]

    def test_load_migrates_once_and_reports_the_backup(self, v1_file):
        notices = []

        def on_migrated(path, backup):
            notices.append((path, backup))

        first = Config.load(v1_file, on_migrated=on_migrated)
        second = Config.load(v1_file, on_migrated=on_migrated)

        assert first.checks.bucket_seconds == second.checks.bucket_seconds == 3.0
        [(path, backup)] = notices
        assert path == v1_file
        assert backups(v1_file) == [backup]

    def test_default_notice_goes_to_stderr(self, v1_file, capsys):
        Config.load(v1_file)

        captured = capsys.readouterr()
        assert captured.out == ""
        [backup] = backups(v1_file)
        assert str(backup) in captured.err
        assert "version 2" in captured.err

    def test_file_without_checks_table_gets_one(self, tmp_path):
        path = tmp_path / "settings.toml"
        path.write_text('[endpoints]\ntargets = ["192.0.2.1"]\n')

        migrate_v1_config(path)

        assert path.read_text() == (
            "version = 2\n\n"
            '[endpoints]\ntargets = ["192.0.2.1"]\n\n'
            "[checks]\nprobe_interval_seconds = 1.0\n"
        )
        assert Config.load(path).checks.probe_interval_seconds == 1.0

    def test_keeps_file_permissions(self, v1_file):
        v1_file.chmod(0o600)

        migrate_v1_config(v1_file)

        assert stat.S_IMODE(v1_file.stat().st_mode) == 0o600

    def test_both_old_and_new_key_is_an_error(self, tmp_path):
        path = tmp_path / "settings.toml"
        path.write_text(
            '[endpoints]\ntargets = ["192.0.2.1"]\n'
            "[checks]\ninterval_seconds = 5.0\nbucket_seconds = 5.0\n"
        )
        with pytest.raises(ValueError, match="both checks.interval_seconds and checks.bucket_"):
            migrate_v1_config(path)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
    def test_unwritable_directory_is_an_error_and_changes_nothing(self, v1_file):
        directory = v1_file.parent
        directory.chmod(0o500)
        try:
            with pytest.raises(OSError, match="Make the directory writable") as error:
                Config.load(v1_file, on_migrated=lambda path, backup: None)
            assert str(v1_file) in str(error.value)
            assert v1_file.read_text() == V1
            assert backups(v1_file) == []
        finally:
            directory.chmod(0o700)
