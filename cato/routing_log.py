"""Persistent routing event log for SwarmSync-routed LLM calls."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from cato.platform import get_data_dir

logger = logging.getLogger(__name__)

_DB_PATH = get_data_dir() / "routing_log.sqlite3"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    routed_model TEXT NOT NULL,
    raw_model TEXT NOT NULL,
    complexity REAL NOT NULL,
    has_tools INTEGER NOT NULL,
    msg_count INTEGER NOT NULL,
    http_status INTEGER,
    content_chars INTEGER NOT NULL,
    tool_call_count INTEGER NOT NULL,
    error TEXT NOT NULL,
    metadata_json TEXT NOT NULL
)
"""


def get_routing_log_path() -> Path:
    return _DB_PATH


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def record_routing_event(event: dict[str, Any]) -> None:
    """Append one routing event. Failures are logged and never break LLM calls."""
    now = float(event.get("ts") or time.time())
    try:
        conn = _connect()
        try:
            metadata = event.get("metadata") or {}
            conn.execute(
                """
                INSERT INTO routing_events (
                    ts, provider, status, routed_model, raw_model, complexity,
                    has_tools, msg_count, http_status, content_chars,
                    tool_call_count, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    str(event.get("provider") or "swarmsync"),
                    str(event.get("status") or "unknown"),
                    str(event.get("routed_model") or ""),
                    str(event.get("raw_model") or ""),
                    float(event.get("complexity") or 0.0),
                    1 if event.get("has_tools") else 0,
                    int(event.get("msg_count") or 0),
                    event.get("http_status"),
                    int(event.get("content_chars") or 0),
                    int(event.get("tool_call_count") or 0),
                    str(event.get("error") or "")[:500],
                    json.dumps(metadata, ensure_ascii=True, sort_keys=True),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("routing log write failed: %s", exc)


def get_persistent_routing_history(limit: int = 100) -> list[dict[str, Any]]:
    """Return recent persistent routing events in chronological order."""
    limit = max(1, min(int(limit), 1000))
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM routing_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("routing log read failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        item = dict(row)
        item["has_tools"] = bool(item.get("has_tools"))
        try:
            metadata = json.loads(item.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        item["metadata"] = metadata
        for key, value in metadata.items():
            item.setdefault(key, value)
        out.append(item)
    return out
