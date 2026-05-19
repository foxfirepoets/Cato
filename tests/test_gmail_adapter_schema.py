"""
tests/test_gmail_adapter_schema.py — Regression test for the missing-emails-table
bug. Prior to this fix, the GmailAdapter.start() coroutine did not initialise the
personal_store schema, so the first INSERT into ``emails`` after a fresh daemon
boot failed with ``sqlite3.OperationalError: no such table: emails``.

The fix calls ``personal_store.init_db()`` at the top of ``GmailAdapter.start()``.
``init_db`` is idempotent (``CREATE TABLE IF NOT EXISTS``), so calling it on
every adapter start is safe and ensures the schema exists.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cato.core.personal_store as store
from cato.adapters.gmail_adapter import GmailAdapter


@pytest.fixture
def isolated_db_path(tmp_path: Path):
    """Provide a fresh DB path without pre-initialising the schema."""
    db_path = tmp_path / "schema_test.sqlite3"
    # NOTE: we intentionally point _DB_PATH at this file but do NOT call
    # init_db() here — that's exactly what the bug we're regressing left
    # to chance.
    store._DB_PATH = db_path
    yield db_path
    store._DB_PATH = None


def _make_adapter() -> GmailAdapter:
    vault = MagicMock()
    vault.get.side_effect = lambda key: ""
    return GmailAdapter(vault=vault)


def test_emails_table_missing_before_adapter_start(isolated_db_path: Path):
    """Sanity: without init_db(), the emails table does not exist."""
    # Touch the DB file by opening a connection
    conn = sqlite3.connect(str(isolated_db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='emails'"
        ).fetchall()
        assert rows == []
    finally:
        conn.close()


def test_gmail_adapter_start_creates_emails_table(isolated_db_path: Path):
    """GmailAdapter.start() must create the emails table before polling."""
    adapter = _make_adapter()

    async def _run_start_briefly():
        # Kick off start() then cancel it before the first poll completes.
        # The init_db() call happens synchronously at the top of start(),
        # so a single tick is enough.
        task = asyncio.create_task(adapter.start())
        await asyncio.sleep(0.05)
        await adapter.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run_start_briefly())

    conn = sqlite3.connect(str(isolated_db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='emails'"
        ).fetchall()
        assert rows == [("emails",)]

        # Also confirm notes table came along (same schema script).
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='notes'"
        ).fetchall()
        assert rows == [("notes",)]
    finally:
        conn.close()


def test_save_email_succeeds_after_adapter_start(isolated_db_path: Path):
    """After GmailAdapter.start() runs, save_email() must work end-to-end.

    This is the actual user-facing symptom: every poll cycle, _process_email
    calls personal_store.save_email() and used to crash with 'no such table'.
    """
    adapter = _make_adapter()

    async def _run_start_briefly():
        task = asyncio.create_task(adapter.start())
        await asyncio.sleep(0.05)
        await adapter.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run_start_briefly())

    row_id = store.save_email(
        gmail_message_id="19e41b8f7761d81a",
        subject="Test subject",
        from_email="sender@example.com",
        snippet="A short snippet.",
        draft_reply="A short reply.",
        gmail_draft_id=None,
    )
    assert isinstance(row_id, int)
    assert row_id > 0

    row = store.get_email_by_gmail_id("19e41b8f7761d81a")
    assert row is not None
    assert row["subject"] == "Test subject"
    assert row["status"] == "pending"


def test_init_db_is_idempotent_when_called_repeatedly(isolated_db_path: Path):
    """Calling init_db() multiple times must not fail or wipe data."""
    store.init_db()
    row_id = store.save_email(
        "msg1", "subj", "from@e.com", "snip", "draft", None
    )

    # Second call: should be a no-op on existing tables.
    store.init_db()
    store.init_db()

    row = store.get_email_by_gmail_id("msg1")
    assert row is not None
    assert row["id"] == row_id
