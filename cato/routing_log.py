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
    request_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 0,
    routed_model TEXT NOT NULL,
    raw_model TEXT NOT NULL,
    routing_reason TEXT NOT NULL DEFAULT '',
    considered_models_json TEXT NOT NULL DEFAULT '[]',
    fallback_routing INTEGER NOT NULL DEFAULT 0,
    estimated_cost REAL,
    actual_cost REAL,
    complexity REAL NOT NULL,
    has_tools INTEGER NOT NULL,
    msg_count INTEGER NOT NULL,
    http_status INTEGER,
    content_chars INTEGER NOT NULL,
    tool_call_count INTEGER NOT NULL,
    error TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routing_ts ON routing_events(ts);
"""

_COLUMNS: dict[str, str] = {
    "request_id": "TEXT NOT NULL DEFAULT ''",
    "success": "INTEGER NOT NULL DEFAULT 0",
    "routing_reason": "TEXT NOT NULL DEFAULT ''",
    "considered_models_json": "TEXT NOT NULL DEFAULT '[]'",
    "fallback_routing": "INTEGER NOT NULL DEFAULT 0",
    "estimated_cost": "REAL",
    "actual_cost": "REAL",
}


def get_routing_log_path() -> Path:
    return _DB_PATH


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(routing_events)")}
    for name, ddl in _COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE routing_events ADD COLUMN {name} {ddl}")


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_models(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, dict):
        return [str(key) for key in value.keys()]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
        return _coerce_models(parsed)
    return []


def record_routing_event(event: dict[str, Any]) -> None:
    """Append one routing event. Failures are logged and never break LLM calls."""
    now = float(event.get("ts") or time.time())
    try:
        conn = _connect()
        try:
            metadata = event.get("metadata") or {}
            request_id = str(event.get("request_id") or metadata.get("request_id") or "")
            success = bool(event.get("success", metadata.get("success", event.get("status") == "ok")))
            routing_reason = str(event.get("routing_reason") or metadata.get("routing_reason") or "")
            considered_models = _coerce_models(
                event.get("considered_models", metadata.get("considered_models"))
            )
            fallback_routing = bool(
                event.get("fallback_routing", metadata.get("fallback_routing", event.get("status") == "fallback"))
            )
            estimated_cost = _coerce_float(event.get("estimated_cost", metadata.get("estimated_cost")))
            actual_cost = _coerce_float(event.get("actual_cost", metadata.get("actual_cost")))
            conn.execute(
                """
                INSERT INTO routing_events (
                    ts, request_id, provider, status, success, routed_model,
                    raw_model, routing_reason, considered_models_json,
                    fallback_routing, estimated_cost, actual_cost, complexity,
                    has_tools, msg_count, http_status, content_chars,
                    tool_call_count, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    request_id,
                    str(event.get("provider") or "swarmsync"),
                    str(event.get("status") or "unknown"),
                    1 if success else 0,
                    str(event.get("routed_model") or ""),
                    str(event.get("raw_model") or ""),
                    routing_reason[:1000],
                    json.dumps(considered_models, ensure_ascii=True),
                    1 if fallback_routing else 0,
                    estimated_cost,
                    actual_cost,
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
        item["success"] = bool(item.get("success"))
        item["fallback_routing"] = bool(item.get("fallback_routing"))
        item["estimated_cost"] = _coerce_float(item.get("estimated_cost"))
        item["actual_cost"] = _coerce_float(item.get("actual_cost"))
        try:
            item["considered_models"] = _coerce_models(item.pop("considered_models_json") or "[]")
        except Exception:
            item["considered_models"] = []
        try:
            metadata = json.loads(item.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        item["metadata"] = metadata
        for key, value in metadata.items():
            item.setdefault(key, value)
        item["request_id"] = str(item.get("request_id") or metadata.get("request_id") or "")
        item["timestamp"] = str(item.get("timestamp") or metadata.get("timestamp") or "")
        item["routing_reason"] = str(item.get("routing_reason") or metadata.get("routing_reason") or "")
        item["considered_models"] = _coerce_models(
            item.get("considered_models") or metadata.get("considered_models")
        )
        item["fallback_routing"] = bool(
            item.get("fallback_routing") or metadata.get("fallback_routing") or item.get("status") == "fallback"
        )
        item["success"] = bool(
            item.get("success") or metadata.get("success") or item.get("status") == "ok"
        )
        item["estimated_cost"] = _coerce_float(item.get("estimated_cost", metadata.get("estimated_cost")))
        item["actual_cost"] = _coerce_float(item.get("actual_cost", metadata.get("actual_cost")))
        out.append(item)
    return out
