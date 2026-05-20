"""
tests/test_personal_store.py — Tests for cato.core.personal_store.

Uses a fresh in-memory / temp-file SQLite database per test so the
tests are fully isolated and never touch the live personal.sqlite3.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import cato.core.personal_store as store


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path):
    """Point personal_store at a fresh temp DB for each test."""
    db_path = tmp_path / "test_personal.sqlite3"
    store.init_db(db_path)
    yield db_path
    # Reset global path so subsequent tests get a clean slate
    store._DB_PATH = None


# ---------------------------------------------------------------------------
# Email tests
# ---------------------------------------------------------------------------

def test_save_and_fetch_email():
    row_id = store.save_email(
        gmail_message_id="abc123",
        subject="Hello",
        from_email="alice@example.com",
        snippet="Hi there",
        draft_reply="Hi Alice, thanks for reaching out.",
        gmail_draft_id="draft-001",
    )
    assert isinstance(row_id, int)
    assert row_id > 0

    row = store.get_email_by_gmail_id("abc123")
    assert row is not None
    assert row["subject"] == "Hello"
    assert row["from_email"] == "alice@example.com"
    assert row["snippet"] == "Hi there"
    assert row["draft_reply"] == "Hi Alice, thanks for reaching out."
    assert row["gmail_draft_id"] == "draft-001"
    assert row["status"] == "pending"

    # Fetch by row id should return the same record
    row2 = store.get_email_by_id(row_id)
    assert row2 is not None
    assert row2["gmail_message_id"] == "abc123"


def test_claim_email_for_send_is_atomic():
    row_id = store.save_email(
        gmail_message_id="xyz789",
        subject="Follow up",
        from_email="bob@example.com",
        snippet="...",
        draft_reply="Sure.",
        gmail_draft_id="draft-002",
    )

    # First claim should succeed
    result = store.claim_email_for_send(row_id)
    assert result == row_id

    # Second claim on the same row (now status='approved') must return None
    result2 = store.claim_email_for_send(row_id)
    assert result2 is None


def test_update_email_status():
    row_id = store.save_email(
        gmail_message_id="msg001",
        subject="Test",
        from_email="test@example.com",
        snippet="test",
        draft_reply="reply",
        gmail_draft_id=None,
    )
    store.update_email_status(row_id, "sent")
    row = store.get_email_by_id(row_id)
    assert row["status"] == "sent"


def test_update_email_draft_id():
    row_id = store.save_email(
        gmail_message_id="msg002",
        subject="Draft update",
        from_email="test@example.com",
        snippet="...",
        draft_reply="reply",
        gmail_draft_id=None,
    )
    store.update_email_draft_id(row_id, "new-draft-id")
    row = store.get_email_by_id(row_id)
    assert row["gmail_draft_id"] == "new-draft-id"


def test_get_pending_email_drafts_excludes_processed_rows():
    pending_id = store.save_email(
        gmail_message_id="pending001",
        subject="Needs review",
        from_email="sender@example.com",
        snippet="hello",
        draft_reply="Thanks.",
        gmail_draft_id="draft-003",
    )
    dismissed_id = store.save_email(
        gmail_message_id="dismissed001",
        subject="Dismissed",
        from_email="sender@example.com",
        snippet="hello",
        draft_reply="No reply.",
        gmail_draft_id="draft-004",
    )
    store.update_email_status(dismissed_id, "dismissed")

    drafts = store.get_pending_email_drafts()

    assert [d["id"] for d in drafts] == [pending_id]


def test_dismiss_email_draft_only_changes_open_review_states():
    row_id = store.save_email(
        gmail_message_id="dismiss001",
        subject="Dismiss me",
        from_email="sender@example.com",
        snippet="hello",
        draft_reply="Reply",
        gmail_draft_id="draft-005",
    )

    assert store.dismiss_email_draft(row_id) is True
    assert store.get_email_by_id(row_id)["status"] == "dismissed"
    assert store.dismiss_email_draft(row_id) is False


def test_get_approved_email_drafts_supports_deferred_desktop_send():
    row_id = store.save_email(
        gmail_message_id="approved001",
        subject="Approve me",
        from_email="sender@example.com",
        snippet="hello",
        draft_reply="Reply",
        gmail_draft_id="draft-006",
    )
    assert store.claim_email_for_send(row_id) == row_id

    approved = store.get_approved_email_drafts()

    assert len(approved) == 1
    assert approved[0]["id"] == row_id


# ---------------------------------------------------------------------------
# Note tests
# ---------------------------------------------------------------------------

def test_save_and_fetch_note():
    row_id = store.save_note("Buy milk", "todo", due_date="2026-06-01")
    assert isinstance(row_id, int)
    assert row_id > 0

    notes = store.get_recent_notes(10)
    assert len(notes) == 1
    note = notes[0]
    assert note["content"] == "Buy milk"
    assert note["category"] == "todo"
    assert note["due_date"] == "2026-06-01"
    assert note["status"] == "open"


def test_get_todos_returns_only_open():
    # Save open todo
    store.save_note("Open task", "todo")
    # Save another note and mark as done
    done_id = store.save_note("Finished task", "todo")

    # Mark that note done via raw SQL to simulate completed state
    import sqlite3  # noqa: PLC0415
    conn = sqlite3.connect(str(store._get_db_path()))
    try:
        conn.execute("UPDATE notes SET status = 'done' WHERE id = ?", (done_id,))
        conn.commit()
    finally:
        conn.close()

    items = store.get_todos_and_reminders()
    contents = [i["content"] for i in items]
    assert "Open task" in contents
    assert "Finished task" not in contents


def test_get_todos_and_reminders_returns_both_categories():
    store.save_note("Pick up parcel", "reminder", due_date="2026-06-10")
    store.save_note("Write report", "todo")
    store.save_note("Random thought", "idea")  # should not appear

    items = store.get_todos_and_reminders()
    categories = {i["category"] for i in items}
    assert "todo" in categories
    assert "reminder" in categories
    assert "idea" not in categories


def test_get_recent_notes_respects_limit():
    for i in range(15):
        store.save_note(f"Note {i}", "memory")

    notes = store.get_recent_notes(5)
    assert len(notes) == 5


def test_get_open_todos_and_reminders_split_categories():
    todo_id = store.save_note("Open task", "todo")
    reminder_id = store.save_note("Call Sam", "reminder", due_date="2026-06-10")
    store.save_note("Memory", "memory")

    todos = store.get_open_todos()
    reminders = store.get_open_reminders()

    assert [t["id"] for t in todos] == [todo_id]
    assert [r["id"] for r in reminders] == [reminder_id]
