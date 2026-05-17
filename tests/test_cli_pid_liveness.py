from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from cato.cli import main


def test_status_removes_stale_pid_and_reports_stopped(tmp_path):
    pid_file = tmp_path / "cato.pid"
    port_file = tmp_path / "cato.port"
    pid_file.write_text("99999", encoding="utf-8")
    port_file.write_text("8090", encoding="utf-8")

    with (
        patch("cato.cli._PID_FILE", pid_file),
        patch("cato.cli._PORT_FILE", port_file),
        patch("cato.cli._pid_alive", return_value=False),
    ):
        result = CliRunner().invoke(main, ["status"])

    assert result.exit_code == 0
    assert "Daemon:  STOPPED" in result.output
    assert not pid_file.exists()
    assert not port_file.exists()


def test_stop_does_not_signal_stale_pid(tmp_path):
    pid_file = tmp_path / "cato.pid"
    port_file = tmp_path / "cato.port"
    pid_file.write_text("99999", encoding="utf-8")

    with (
        patch("cato.cli._PID_FILE", pid_file),
        patch("cato.cli._PORT_FILE", port_file),
        patch("cato.cli._pid_alive", return_value=False),
        patch("cato.cli.os.kill") as kill,
    ):
        result = CliRunner().invoke(main, ["stop"])

    assert result.exit_code == 0
    assert "Cato is not running." in result.output
    kill.assert_not_called()
    assert not pid_file.exists()
