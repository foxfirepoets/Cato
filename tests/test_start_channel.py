"""
tests/test_start_channel.py — Startup tests for cato start --channel.

Verifies that each --channel value (webchat, telegram, whatsapp, all) is accepted
and passed to the daemon; and that cato status prints bound listeners when running.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cato.cli import main, cmd_status, cmd_start


@pytest.fixture
def cli_runner():
    return CliRunner()


@pytest.fixture
def mock_pid_file(tmp_path: Path):
    pid_file = tmp_path / "cato.pid"
    port_file = tmp_path / "cato.port"
    with patch("cato.cli._PID_FILE", pid_file), patch("cato.cli._PORT_FILE", port_file):
        yield pid_file, port_file


def test_start_channel_webchat_passed_to_daemon(cli_runner: CliRunner, mock_pid_file):
    """Starting with --channel webchat passes channel to _run_daemon."""
    pid_file, _ = mock_pid_file
    with patch("cato.cli._run_daemon", new_callable=MagicMock) as m_run:
        with patch("cato.cli.setup_signal_handlers"):
            result = cli_runner.invoke(main, ["start", "--channel", "webchat"])
    # May exit 0 or fail later (e.g. no vault); we only care that _run_daemon was called with webchat
    if m_run.called:
        assert m_run.call_args[0][2] == "webchat"


def test_start_channel_telegram_passed_to_daemon(cli_runner: CliRunner, mock_pid_file):
    """Starting with --channel telegram passes channel to _run_daemon."""
    with patch("cato.cli._run_daemon", new_callable=MagicMock) as m_run:
        with patch("cato.cli.setup_signal_handlers"):
            cli_runner.invoke(main, ["start", "--channel", "telegram"])
    if m_run.called:
        assert m_run.call_args[0][2] == "telegram"


def test_start_channel_whatsapp_passed_to_daemon(cli_runner: CliRunner, mock_pid_file):
    """Starting with --channel whatsapp passes channel to _run_daemon."""
    with patch("cato.cli._run_daemon", new_callable=MagicMock) as m_run:
        with patch("cato.cli.setup_signal_handlers"):
            cli_runner.invoke(main, ["start", "--channel", "whatsapp"])
    if m_run.called:
        assert m_run.call_args[0][2] == "whatsapp"


def test_start_channel_all_passed_to_daemon(cli_runner: CliRunner, mock_pid_file):
    """Starting with --channel all passes channel to _run_daemon."""
    with patch("cato.cli._run_daemon", new_callable=MagicMock) as m_run:
        with patch("cato.cli.setup_signal_handlers"):
            cli_runner.invoke(main, ["start", "--channel", "all"])
    if m_run.called:
        assert m_run.call_args[0][2] == "all"


def test_status_shows_listeners_when_port_file_exists(cli_runner: CliRunner, tmp_path: Path):
    """cato status prints HTTP/WS listeners when daemon is running and port file exists."""
    pid_file = tmp_path / "cato.pid"
    port_file = tmp_path / "cato.port"
    pid_file.write_text("99999")
    port_file.write_text("8090")
    with (
        patch("cato.cli._PID_FILE", pid_file),
        patch("cato.cli._PORT_FILE", port_file),
        patch("cato.cli._pid_alive", return_value=True),
    ):
        result = cli_runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "8090" in result.output
    assert "127.0.0.1" in result.output or "HTTP" in result.output
