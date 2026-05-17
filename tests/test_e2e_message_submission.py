"""
tests/test_e2e_message_submission.py

Comprehensive E2E tests for the message submission path.

Covers:
  1. Valid JSON message → gateway receives it without JSONDecodeError
  2. Gateway broadcasts valid JSON to subscribed WebSocket clients
  3. No exception raised end-to-end for a normal message
  4. Malformed JSON → graceful error path, no crash

All tests are offline (no network, no LLM calls). Mocks:
  - AgentLoop / SwarmSync via patch on gateway._ensure_agent_loop
  - LaneQueue._process_task to avoid real agent execution
  - ws client objects are simple AsyncMock instances
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cato.gateway import Gateway


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _make_config() -> Any:
    """Minimal CatoConfig-like object sufficient for Gateway.__init__."""
    cfg = SimpleNamespace(
        agent_name="test-agent",
        webchat_port=8080,
        workspace_dir="",
        swarmsync_enabled=False,
        default_model="test/model",
    )
    return cfg


def _make_budget() -> Any:
    """Minimal BudgetManager stub."""
    bm = MagicMock()
    bm.check = MagicMock(return_value=None)
    return bm


def _make_vault() -> Any:
    """Minimal Vault stub."""
    v = MagicMock()
    v.get = MagicMock(return_value=None)
    return v


def _make_mock_ws() -> AsyncMock:
    """
    Simulates a raw websockets-style client (has .send, not .send_str).
    Gateway._ws_broadcast checks for send_str first, falls back to send.
    """
    ws = AsyncMock()
    # Remove send_str so gateway uses ws.send (raw websockets path)
    del ws.send_str
    return ws


def _make_gateway() -> Gateway:
    return Gateway(_make_config(), _make_budget(), _make_vault())


# ---------------------------------------------------------------------------
# Test 1 — Valid JSON message → no JSONDecodeError, gateway processes it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_json_message_no_decode_error(caplog):
    """
    Submitting a well-formed JSON message via handle_ws_message must not
    trigger a JSONDecodeError anywhere in the gateway.
    """
    gw = _make_gateway()

    # Prevent actual agent-loop execution (would need heavy imports + LLM)
    async def _noop_process(task: dict) -> None:
        return

    with patch.object(gw, "_process_task", side_effect=_noop_process):
        with caplog.at_level(logging.ERROR):
            payload = json.dumps({
                "type": "message",
                "text": "Hello Cato",
                "session_id": "test-session-1",
                "channel": "web",
            })
            # Must complete without raising
            await gw.handle_ws_message(object(), payload)

    # No JSONDecodeError should appear in error logs
    json_errors = [r for r in caplog.records if "JSONDecodeError" in r.message or "invalid JSON" in r.message]
    assert json_errors == [], f"Unexpected JSON errors: {json_errors}"


# ---------------------------------------------------------------------------
# Test 2 — Gateway broadcasts message to subscribed clients
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gateway_broadcasts_to_subscribed_clients():
    """
    When send() is called for a web channel, every registered WS client
    must receive a JSON payload with type='response' and the correct text.
    """
    gw = _make_gateway()

    # Register two mock ws clients
    ws1 = _make_mock_ws()
    ws2 = _make_mock_ws()
    gw.register_websocket(ws1)
    gw.register_websocket(ws2)

    await gw.send("sess-broadcast", "Hello from agent", "web", model="test/model")

    # Both clients must have received a call
    ws1.send.assert_awaited_once()
    ws2.send.assert_awaited_once()

    # Decode and validate the payload sent to ws1
    raw_sent = ws1.send.call_args[0][0]
    data = json.loads(raw_sent)
    assert data["type"] == "response"
    assert data["session_id"] == "sess-broadcast"
    assert "Hello from agent" in data["text"]
    assert data["channel"] == "web"


# ---------------------------------------------------------------------------
# Test 3 — No exception raised end-to-end for a normal message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_exception_end_to_end():
    """
    Submitting a message through the full handle_ws_message → ingest →
    lane-enqueue path must complete without raising any exception.
    """
    gw = _make_gateway()

    async def _noop_process(task: dict) -> None:
        return

    # Patch _process_task so the lane worker does nothing heavy
    with patch.object(gw, "_process_task", side_effect=_noop_process):
        try:
            await gw.handle_ws_message(
                object(),
                json.dumps({
                    "type": "message",
                    "text": "Run without errors",
                    "session_id": "sess-e2e",
                    "channel": "web",
                }),
            )
        except Exception as exc:
            pytest.fail(f"handle_ws_message raised unexpectedly: {exc!r}")

    # Give the lane task a tick to drain
    await asyncio.sleep(0)

    # Verify the lane was created (message was actually ingested)
    assert "sess-e2e" in gw._lanes


# ---------------------------------------------------------------------------
# Test 4 — Malformed JSON → graceful error, gateway does NOT crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_malformed_json_returns_error_not_crash():
    """
    Sending malformed JSON must NOT crash the gateway. Gateway must
    respond with type='error' and keep running (no exception propagated).
    """
    gw = _make_gateway()

    ws = _make_mock_ws()
    gw.register_websocket(ws)

    malformed = "{ not valid json ]]"

    # Must not raise
    try:
        await gw.handle_ws_message(ws, malformed)
    except Exception as exc:
        pytest.fail(f"Gateway crashed on malformed JSON: {exc!r}")

    # Gateway must have sent an error response back on the same ws
    ws.send.assert_awaited_once()
    raw = ws.send.call_args[0][0]
    data = json.loads(raw)
    assert data["type"] == "error"
    assert "invalid JSON" in data.get("text", "").lower() or "json" in data.get("text", "").lower()


# ---------------------------------------------------------------------------
# Test 5 — ingest() for telegram channel broadcasts to ws clients
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_ingest_broadcasts_to_ws_clients():
    """
    When a Telegram message arrives via ingest(), the gateway must broadcast
    a type='message', role='user' payload to all connected WS clients before
    handing it to the lane queue.
    """
    gw = _make_gateway()

    ws = _make_mock_ws()
    gw.register_websocket(ws)

    async def _noop_process(task: dict) -> None:
        return

    with patch.object(gw, "_process_task", side_effect=_noop_process):
        await gw.ingest(
            session_id="tg-session",
            message="Hi from Telegram",
            channel="telegram",
            agent_id="test-agent",
        )

    ws.send.assert_awaited_once()
    raw = ws.send.call_args[0][0]
    data = json.loads(raw)
    assert data["type"] == "message"
    assert data["role"] == "user"
    assert data["channel"] == "telegram"
    assert "Hi from Telegram" in data["text"]
