"""
cato/core/personal_store.py — SQLite store for emails and personal notes.

Schema:
  emails  — Gmail-sourced messages + draft replies awaiting approve/dismiss
  notes   — Free-text notes classified as todo/memory/idea/reminder

The database lives at get_data_dir() / "personal.sqlite3".
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from cato.platform import get_data_dir

logger = logging.getLogger(__name__)

_DB_PATH: Path | None = None
_SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT UNIQUE NOT NULL,
    subject TEXT,
    from_email TEXT,
    snippet TEXT,
    draft_reply TEXT,
    gmail_draft_id TEXT,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'sent', 'dismissed')),
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('todo', 'memory', 'idea', 'reminder')),
    due_date TEXT,
    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'done')),
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def _get_db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = get_data_dir() / "personal.sqlite3"
    return _DB_PATH


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_get_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Initialise the schema. Optionally override the DB path (for tests)."""
    global _DB_PATH
    if db_path is not None:
        _DB_PATH = db_path
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def save_email(
    gmail_message_id: str,
    subject: str,
    from_email: str,
    snippet: str,
    draft_reply: str,
    gmail_draft_id: Optional[str],
) -> int:
    """Insert a new email row and return its integer id."""
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO emails (gmail_message_id, subject, from_email, snippet, draft_reply, gmail_draft_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (gmail_message_id, subject, from_email, snippet, draft_reply, gmail_draft_id),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def get_email_by_gmail_id(gmail_message_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM emails WHERE gmail_message_id = ?",
            (gmail_message_id,),
        )
        return _row_to_dict(cur.fetchone())
    finally:
        conn.close()


def get_email_by_id(row_id: int) -> dict[str, Any] | None:
    conn = _connect()
    try:
        cur = conn.execute("SELECT * FROM emails WHERE id = ?", (row_id,))
        return _row_to_dict(cur.fetchone())
    finally:
        conn.close()


def update_email_status(row_id: int, status: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE emails SET status = ? WHERE id = ?",
            (status, row_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_email_draft_id(row_id: int, gmail_draft_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE emails SET gmail_draft_id = ? WHERE id = ?",
            (gmail_draft_id, row_id),
        )
        conn.commit()
    finally:
        conn.close()


def claim_email_for_send(row_id: int) -> int | None:
    """Atomically transition status pending→approved.

    Returns row_id on success, None when the email is already
    approved/sent/dismissed (idempotency guard against double-tap).
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE emails SET status = 'approved' WHERE id = ? AND status = 'pending'",
            (row_id,),
        )
        conn.commit()
        return row_id if cur.rowcount == 1 else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Note helpers
# ---------------------------------------------------------------------------

def save_note(content: str, category: str, due_date: Optional[str] = None) -> int:
    """Insert a note and return its integer id."""
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO notes (content, category, due_date) VALUES (?, ?, ?)",
            (content, category, due_date),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def get_todos_and_reminders() -> list[dict[str, Any]]:
    """Return all open todos and reminders ordered by creation date."""
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT * FROM notes
            WHERE category IN ('todo', 'reminder') AND status = 'open'
            ORDER BY created_at ASC
            """,
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_recent_notes(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent *limit* notes."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
