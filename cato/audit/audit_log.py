"""
cato/audit/audit_log.py — Append-only, hash-chained audit log for CATO.

Every agent action is written here — never updated, never deleted.
The SHA-256 chain allows tamper detection: verify_chain() walks
every row and recomputes each row_hash from its fields + prev_hash.

Storage: SQLite at {data_dir}/cato.db, table audit_log.

NOTE: This file was migrated from cato/audit.py when cato/audit/ package
was created for Phase H (Safety Foundation). The public API is unchanged;
AuditLog is re-exported from cato/audit/__init__.py.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    action_type   TEXT    NOT NULL,
    tool_name     TEXT    NOT NULL,
    inputs_json   TEXT    NOT NULL,
    outputs_json  TEXT    NOT NULL,
    cost_cents    INTEGER NOT NULL DEFAULT 0,
    error         TEXT    NOT NULL DEFAULT '',
    timestamp     REAL    NOT NULL,
    prev_hash     TEXT    NOT NULL DEFAULT '',
    row_hash      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp);
"""

_SENSITIVE_KEYS = frozenset({
    "api_key", "token", "password", "secret", "key", "authorization",
    "bearer", "credential", "passwd", "passphrase",
    "auth", "x-api-key", "bearer_token", "access_token", "refresh_token",
})

_SENSITIVE_VALUE_PREFIXES = ("sk-", "Bearer ", "ghp_", "xoxb-", "xoxp-", "ya29.", "AKIA")

_MAX_OUTPUT_CHARS = 2000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_inputs(inputs: dict) -> dict:
    """Remove any vault keys or sensitive values from inputs before logging."""
    if not isinstance(inputs, dict):
        return {}
    clean: dict = {}
    for k, v in inputs.items():
        key_sensitive = any(s in k.lower() for s in _SENSITIVE_KEYS)
        val_sensitive = isinstance(v, str) and any(v.startswith(p) for p in _SENSITIVE_VALUE_PREFIXES)
        if key_sensitive or val_sensitive:
            clean[k] = "[REDACTED]"
        else:
            clean[k] = v
    return clean


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _row_hash(
    row_id: int,
    session_id: str,
    action_type: str,
    tool_name: str,
    cost_cents: int,
    timestamp: float,
    prev_hash: str,
) -> str:
    """Compute SHA-256 hash for a row — used to build the tamper-evident chain."""
    payload = (
        f"{row_id}:{session_id}:{action_type}:{tool_name}:"
        f"{cost_cents}:{timestamp}:{prev_hash}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# AuditLog
# ---------------------------------------------------------------------------

class AuditLog:
    """
    Append-only SQLite audit log with SHA-256 hash chain.

    Usage::

        log = AuditLog()
        log.connect()
        row_id = log.log(
            session_id="sess-001",
            action_type="tool_call",
            tool_name="browser.navigate",
            inputs={"url": "https://example.com"},
            outputs={"title": "Example", "text": "..."},
            cost_cents=1,
        )
        summary = log.session_summary("sess-001")
        ok = log.verify_chain("sess-001")
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        from ..platform import get_data_dir
        self._db_path = db_path or (get_data_dir() / "cato.db")
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = threading.Lock()

    def connect(self) -> None:
        """Open (or create) the SQLite database and apply the schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.debug("AuditLog connected to %s", self._db_path)

    def _ensure_connected(self) -> None:
        if self._conn is None:
            self.connect()

    def _last_row_hash(self, session_id: str) -> str:
        """Return the row_hash of the most recent row for this session, or ''."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT row_hash FROM audit_log WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row["row_hash"] if row else ""

    def log(
        self,
        session_id: str,
        action_type: str,
        tool_name: str,
        inputs: Any,
        outputs: Any,
        cost_cents: int = 0,
        error: str = "",
    ) -> int:
        """
        Append one audit row and return its auto-increment id.

        action_type: "tool_call" | "llm_response" | "skill_load" | "error"
        inputs: sanitized — vault keys are redacted automatically.
        outputs: truncated to 2000 chars.
        """
        self._ensure_connected()
        assert self._conn is not None

        ts = time.time()
        safe_inputs = _sanitize_inputs(inputs if isinstance(inputs, dict) else {})
        inputs_json = json.dumps(safe_inputs, ensure_ascii=True)

        raw_output = (
            outputs if isinstance(outputs, str)
            else json.dumps(outputs, ensure_ascii=True)
        )
        outputs_json = _truncate(raw_output)

        with self._write_lock:
            prev_hash = self._last_row_hash(session_id)

            # We need the id first — insert a placeholder then update the hash
            cur = self._conn.execute(
                """
                INSERT INTO audit_log
                  (session_id, action_type, tool_name, inputs_json, outputs_json,
                   cost_cents, error, timestamp, prev_hash, row_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, action_type, tool_name, inputs_json, outputs_json,
                    cost_cents, error, ts, prev_hash, "",
                ),
            )
            row_id = cur.lastrowid
            assert row_id is not None

            rh = _row_hash(
                row_id, session_id, action_type, tool_name, cost_cents, ts, prev_hash
            )
            self._conn.execute(
                "UPDATE audit_log SET row_hash = ? WHERE id = ?",
                (rh, row_id),
            )
            self._conn.commit()
        return row_id

    def session_summary(self, session_id: str) -> dict:
        """
        Return aggregate stats for a session.

        Keys: action_count (alias: count), total_cost_cents, errors,
              start_ts, end_ts, tools_used.
        """
        self._ensure_connected()
        assert self._conn is not None

        rows = self._conn.execute(
            """
            SELECT action_type, tool_name, cost_cents, error, timestamp
            FROM audit_log
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()

        if not rows:
            return {
                "action_count": 0, "count": 0, "total_cost_cents": 0, "errors": 0,
                "start_ts": None, "end_ts": None, "tools_used": [],
            }

        tools_used = sorted({r["tool_name"] for r in rows if r["tool_name"]})
        error_count = sum(1 for r in rows if r["error"])
        total_cost = sum(r["cost_cents"] for r in rows)
        timestamps = [r["timestamp"] for r in rows]
        n = len(rows)

        return {
            "action_count": n,    # canonical name used by audit/receipt/CLI
            "count": n,           # backward-compat alias
            "total_cost_cents": total_cost,
            "errors": error_count,
            "start_ts": min(timestamps),
            "end_ts": max(timestamps),
            "tools_used": tools_used,
        }

    def export_session(self, session_id: str, fmt: str = "jsonl") -> str:
        """
        Export all rows for *session_id* as JSONL or CSV string.

        fmt: "jsonl" | "csv"
        """
        self._ensure_connected()
        assert self._conn is not None

        rows = self._conn.execute(
            """
            SELECT id, session_id, action_type, tool_name, inputs_json,
                   outputs_json, cost_cents, error, timestamp, prev_hash, row_hash
            FROM audit_log
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()

        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                "id", "session_id", "action_type", "tool_name",
                "inputs_json", "outputs_json", "cost_cents", "error",
                "timestamp", "prev_hash", "row_hash",
            ])
            for r in rows:
                writer.writerow(list(r))
            return buf.getvalue()

        # Default: JSONL
        lines: list[str] = []
        for r in rows:
            lines.append(json.dumps(dict(r), ensure_ascii=True))
        return "\n".join(lines)

    def verify_chain(self, session_id: str) -> bool:
        """
        Verify the SHA-256 chain for all rows in *session_id*.

        Returns True if every row_hash matches recomputed value.
        Logs a warning for each mismatch.
        """
        self._ensure_connected()
        assert self._conn is not None

        rows = self._conn.execute(
            """
            SELECT id, session_id, action_type, tool_name, cost_cents,
                   timestamp, prev_hash, row_hash
            FROM audit_log
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()

        ok = True
        for r in rows:
            expected = _row_hash(
                r["id"], r["session_id"], r["action_type"], r["tool_name"],
                r["cost_cents"], r["timestamp"], r["prev_hash"],
            )
            if expected != r["row_hash"]:
                logger.warning(
                    "AuditLog chain broken at row id=%s (session=%s)",
                    r["id"], session_id,
                )
                ok = False

        return ok

    def verify_recent_sessions(self, hours: int = 24) -> None:
        """
        Verify the hash chain for every session active in the last *hours* hours.

        Logs CRITICAL for any session whose chain is broken.
        """
        self._ensure_connected()
        assert self._conn is not None
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT DISTINCT session_id FROM audit_log WHERE timestamp >= ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            sid = row["session_id"]
            if not self.verify_chain(sid):
                logger.critical(
                    "AuditLog: chain integrity FAILED for session %s", sid
                )

    def get_session_rows(self, session_id: str) -> list[dict]:
        """
        Return all audit rows for *session_id* as a list of plain dicts.
        Used by ConduitProof to build the exportable bundle.
        """
        self._ensure_connected()
        assert self._conn is not None
        rows = self._conn.execute(
            """
            SELECT id, session_id, action_type, tool_name, inputs_json,
                   outputs_json, cost_cents, error, timestamp, prev_hash, row_hash
            FROM audit_log
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "AuditLog":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()
