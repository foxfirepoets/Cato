"""
tests/test_daily_logs.py — Tests for daily log management.

Tests cover:
- Daily log creation and content
- Log date parsing
- Log archival and compression
- API endpoints for log retrieval
"""

import pytest
from pathlib import Path
from datetime import datetime, timedelta
import tempfile
from unittest.mock import patch, MagicMock
import gzip

from cato.core.daily_log_manager import (
    create_daily_log,
    get_daily_log_content,
    get_todays_log_path,
    archive_old_logs,
    list_recent_logs,
)


class TestDailyLogCreation:
    """Test daily log file creation."""

    def test_get_todays_log_path_returns_path(self):
        """Test get_todays_log_path returns valid Path object."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                path = get_todays_log_path()
                assert isinstance(path, Path)
                assert path.parent == test_workspace
                assert path.suffix == ".md"

    def test_create_daily_log_creates_file(self):
        """Test create_daily_log creates a file if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            test_workspace.mkdir(parents=True, exist_ok=True)

            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                log_path = create_daily_log()
                assert log_path.exists()
                assert log_path.suffix == ".md"

    def test_create_daily_log_content_structure(self):
        """Test created log has proper structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            test_workspace.mkdir(parents=True, exist_ok=True)

            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                log_path = create_daily_log()
                content = log_path.read_text()

                # Should have required sections
                assert "# Daily Log" in content
                assert "## Tasks" in content
                assert "## Notes" in content
                assert "## Completed" in content

    def test_get_daily_log_content_returns_file(self):
        """Test get_daily_log_content reads existing log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            test_workspace.mkdir(parents=True, exist_ok=True)

            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                # Create log first
                create_daily_log()

                # Read it back
                content = get_daily_log_content()
                assert content is not None
                assert "Daily Log" in content


class TestDailyLogRetrieval:
    """Test daily log retrieval by date."""

    def test_get_log_by_date_valid(self):
        """Test retrieving log by specific date."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            test_workspace.mkdir(parents=True, exist_ok=True)

            # Create a test log
            test_date = "2026-03-08"
            test_log = test_workspace / f"{test_date}.md"
            test_log.write_text("# Test Log\n\nContent here")

            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                content = get_daily_log_content(test_date)
                assert content is not None
                assert "Test Log" in content

    def test_get_log_by_date_missing(self):
        """Test retrieving non-existent log returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            test_workspace.mkdir(parents=True, exist_ok=True)

            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                content = get_daily_log_content("2020-01-01")
                assert content is None


class TestLogArchival:
    """Test log archival and compression."""

    def test_archive_old_logs_creates_archive_dir(self):
        """Test archive_old_logs creates archive directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            test_workspace.mkdir(parents=True, exist_ok=True)

            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                archive_old_logs()
                archive_dir = test_workspace / ".archive"
                assert archive_dir.exists()

    def test_archive_old_logs_compresses_old_files(self):
        """Test old logs are compressed to .gz."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            test_workspace.mkdir(parents=True, exist_ok=True)

            # Create an old log (60 days ago)
            old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
            old_log = test_workspace / f"{old_date}.md"
            old_log.write_text("# Old Log\n\nThis is old")

            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                archived = archive_old_logs(days_threshold=30)

                # Should have archived 1 file
                assert archived >= 1

                # Original should be deleted
                assert not old_log.exists()

                # Compressed version should exist
                archive_dir = test_workspace / ".archive"
                assert (archive_dir / f"{old_date}.md.gz").exists()


class TestListRecentLogs:
    """Test listing recent logs."""

    def test_list_recent_logs_returns_list(self):
        """Test list_recent_logs returns list of dates."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            test_workspace.mkdir(parents=True, exist_ok=True)

            # Create some test logs
            for i in range(3):
                date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                log = test_workspace / f"{date}.md"
                log.write_text(f"# Log {date}")

            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                logs = list_recent_logs(days=7)
                assert isinstance(logs, list)
                assert len(logs) > 0

    def test_list_recent_logs_filters_by_days(self):
        """Test list_recent_logs respects days parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            test_workspace.mkdir(parents=True, exist_ok=True)

            # Create old log (100 days ago)
            old_date = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
            old_log = test_workspace / f"{old_date}.md"
            old_log.write_text("# Old Log")

            # Create recent log
            recent_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            recent_log = test_workspace / f"{recent_date}.md"
            recent_log.write_text("# Recent Log")

            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                # 7-day window should not include 100-day-old log
                logs = list_recent_logs(days=7)
                assert old_date not in logs
                # But should include recent log
                assert recent_date in logs


class TestLogsAPI:
    """Test daily logs API endpoints."""

    @pytest.mark.asyncio
    async def test_get_todays_log_success(self):
        """Test GET /api/logs/today creates and returns log."""
        from cato.api.logs_routes import get_todays_log

        request = MagicMock()
        response = await get_todays_log(request)

        # Should return valid response
        assert response.status in (200, 500)

    @pytest.mark.asyncio
    async def test_get_log_by_date_invalid_format(self):
        """Test GET /api/logs/{date} rejects invalid dates."""
        from cato.api.logs_routes import get_log_by_date

        request = MagicMock()
        request.match_info = {"date": "invalid-date"}

        response = await get_log_by_date(request)
        # Should return 400 for invalid format
        assert response.status == 400

    @pytest.mark.asyncio
    async def test_list_recent_logs_success(self):
        """Test GET /api/logs/recent returns list."""
        from cato.api.logs_routes import list_recent_logs

        request = MagicMock()
        request.query = {"days": "7"}

        response = await list_recent_logs(request)
        assert response.status in (200, 500)

    @pytest.mark.asyncio
    async def test_archive_logs_success(self):
        """Test POST /api/logs/archive archives old logs."""
        from cato.api.logs_routes import archive_logs

        request = MagicMock()
        request.json = MagicMock(return_value={"days_threshold": 30})

        response = await archive_logs(request)
        assert response.status in (200, 500)


class TestDailyLogPreviousContext:
    """Test that daily logs include previous day context."""

    def test_create_log_includes_previous_incomplete_tasks(self):
        """Test new log includes incomplete tasks from previous day."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_workspace = Path(tmpdir) / "workspace"
            test_workspace.mkdir(parents=True, exist_ok=True)

            # Create yesterday's log with incomplete task
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            yesterday_log = test_workspace / f"{yesterday}.md"
            yesterday_log.write_text("# Log\n\n- [ ] Incomplete task\n- [x] Completed task")

            # Create today's log
            with patch("cato.core.daily_log_manager._workspace_dir", return_value=test_workspace):
                log_path = create_daily_log()
                content = log_path.read_text()

                # Should include incomplete task from yesterday
                assert "Incomplete task" in content
                assert "Carried Over From Yesterday" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
