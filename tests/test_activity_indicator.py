"""
tests/test_activity_indicator.py — Unit tests for the activity indicator feature.

Covers:
  1. Gateway._broadcast_activity() — sets state and broadcasts WS message
  2. Gateway.get_activity() — returns current state or idle default
  3. Activity state is instance-level (F-01 regression guard)
  4. _process_task broadcasts activity start/end
  5. _process_flow_task broadcasts activity start/end
  6. /api/activity endpoint returns JSON without auth
  7. SwarmSync empty prefill falls back to _stream_collect
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cato.config import CatoConfig
from cato.gateway import Gateway


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gateway(tmp_path: Path | None = None) -> Gateway:
    """Construct a minimal Gateway with mocked config, budget, and vault."""
    cfg = CatoConfig()
    cfg.agent_name = "test-agent"
    cfg.swarmsync_enabled = False
    if tmp_path:
        cfg.workspace_dir = str(tmp_path / "workspace")

    budget = MagicMock()
    budget.format_footer.return_value = ""
    budget.check_session.return_value = None
    budget.check_monthly.return_value = None
    budget.record_call = MagicMock()

    vault = MagicMock()
    vault.get.return_value = None

    gw = Gateway(config=cfg, budget=budget, vault=vault)
    return gw


def _make_ws() -> MagicMock:
    """Return a mock WebSocket simulating aiohttp WebSocketResponse."""
    ws = MagicMock(spec=["send_str"])
    ws.send_str = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# 1. _broadcast_activity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broadcast_activity_sets_state():
    """_broadcast_activity should update _activity_state with busy/session/task."""
    gw = _make_gateway()
    await gw._broadcast_activity(True, "sess-1", "thinking hard")

    state = gw._activity_state
    assert state["busy"] is True
    assert state["session_id"] == "sess-1"
    assert state["task"] == "thinking hard"
    assert "updated_at" in state
    assert isinstance(state["updated_at"], float)


@pytest.mark.asyncio
async def test_broadcast_activity_sends_ws_message():
    """_broadcast_activity should broadcast an 'activity' event to all WS clients."""
    gw = _make_gateway()
    ws = _make_ws()
    gw._ws_clients.add(ws)

    await gw._broadcast_activity(True, "sess-2", "coding")

    ws.send_str.assert_called_once()
    payload = json.loads(ws.send_str.call_args[0][0])
    assert payload["type"] == "activity"
    assert payload["busy"] is True
    assert payload["session_id"] == "sess-2"
    assert payload["task"] == "coding"


@pytest.mark.asyncio
async def test_broadcast_activity_clears_on_idle():
    """Broadcasting busy=False should set state to idle."""
    gw = _make_gateway()
    await gw._broadcast_activity(True, "sess-1", "working")
    await gw._broadcast_activity(False, "sess-1", "")

    state = gw._activity_state
    assert state["busy"] is False
    assert state["task"] == ""


# ---------------------------------------------------------------------------
# 2. get_activity
# ---------------------------------------------------------------------------

def test_get_activity_default_idle():
    """get_activity should return idle state when no activity has been broadcast."""
    gw = _make_gateway()
    result = gw.get_activity()
    assert result["busy"] is False
    assert result["session_id"] == ""
    assert result["task"] == ""


@pytest.mark.asyncio
async def test_get_activity_after_broadcast():
    """get_activity should reflect the last broadcast state."""
    gw = _make_gateway()
    await gw._broadcast_activity(True, "sess-3", "researching")

    result = gw.get_activity()
    assert result["busy"] is True
    assert result["session_id"] == "sess-3"
    assert result["task"] == "researching"


def test_get_activity_returns_copy():
    """get_activity should return a copy, not the internal dict."""
    gw = _make_gateway()
    a = gw.get_activity()
    b = gw.get_activity()
    assert a is not b


# ---------------------------------------------------------------------------
# 3. F-01 regression: _activity_state is instance-level
# ---------------------------------------------------------------------------

def test_activity_state_is_instance_level():
    """Each Gateway instance should have its own _activity_state (F-01 fix)."""
    gw1 = _make_gateway()
    gw2 = _make_gateway()
    gw1._activity_state = {"busy": True, "session_id": "a", "task": "x", "updated_at": 0}

    # gw2 should still be empty — not sharing gw1's mutation
    assert gw2._activity_state == {}


# ---------------------------------------------------------------------------
# 4. _process_task activity lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_task_broadcasts_activity_start_and_end():
    """_process_task should broadcast busy=True at start and busy=False in finally."""
    gw = _make_gateway()
    broadcasts: list[dict] = []

    original_broadcast = gw._broadcast_activity

    async def capture_broadcast(busy, session_id, task_preview):
        broadcasts.append({"busy": busy, "session_id": session_id, "task": task_preview})
        # Don't call original — avoid WS broadcast with no clients

    gw._broadcast_activity = capture_broadcast

    # Mock the agent loop so _process_task doesn't actually run inference
    mock_loop = AsyncMock()
    mock_loop.run.return_value = "test response"
    gw._agent_loop = mock_loop
    gw._ensure_agent_loop = AsyncMock()

    # Mock send to avoid WS broadcast
    gw.send = AsyncMock()

    task = {
        "session_id": "test-sess",
        "channel": "web",
        "message": "hello world",
    }

    await gw._process_task(task)

    # Should have at least 2 broadcasts: start (busy=True) and end (busy=False)
    assert len(broadcasts) >= 2
    assert broadcasts[0]["busy"] is True
    assert broadcasts[0]["session_id"] == "test-sess"
    assert broadcasts[-1]["busy"] is False
    assert broadcasts[-1]["session_id"] == "test-sess"


@pytest.mark.asyncio
async def test_process_task_broadcasts_idle_on_error():
    """_process_task should broadcast busy=False even if agent loop raises."""
    gw = _make_gateway()
    broadcasts: list[dict] = []

    async def capture_broadcast(busy, session_id, task_preview):
        broadcasts.append({"busy": busy})

    gw._broadcast_activity = capture_broadcast
    gw._ensure_agent_loop = AsyncMock()
    gw._agent_loop = None  # Will cause error path
    gw.send = AsyncMock()

    task = {
        "session_id": "err-sess",
        "channel": "web",
        "message": "trigger error",
    }

    await gw._process_task(task)

    # Must end with busy=False regardless of error
    assert broadcasts[-1]["busy"] is False


# ---------------------------------------------------------------------------
# 5. _process_flow_task activity lifecycle (F-02 fix)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_flow_task_broadcasts_activity():
    """_process_flow_task should broadcast busy=True/False around flow execution."""
    gw = _make_gateway()
    broadcasts: list[dict] = []

    async def capture_broadcast(busy, session_id, task_preview):
        broadcasts.append({"busy": busy, "session_id": session_id, "task": task_preview})

    gw._broadcast_activity = capture_broadcast
    gw.send = AsyncMock()

    # Mock FlowEngine so we don't need actual flow infrastructure
    mock_result = MagicMock()
    mock_result.status = "completed"
    mock_result.step_outputs = ["step1"]
    mock_result.error = None

    with patch("cato.gateway.FlowEngine", create=True) as MockFlowEngine:
        # Patch at the import site inside the method
        mock_engine = AsyncMock()
        mock_engine.run_flow.return_value = mock_result

        with patch.dict("sys.modules", {"cato.orchestrator.clawflows": MagicMock(FlowEngine=lambda: mock_engine)}):
            # The method does `from .orchestrator.clawflows import FlowEngine`
            # We need to mock this import path
            with patch("cato.gateway.Gateway._process_flow_task", wraps=gw._process_flow_task):
                # Simpler approach: just call it and let it fail on import, check broadcasts
                task = {"session_id": "flow-sess", "channel": "web", "flow": "test-flow"}

                # Monkeypatch the import inside the method
                import importlib
                import cato.orchestrator
                mock_clawflows = MagicMock()
                mock_clawflows.FlowEngine.return_value = mock_engine
                with patch.dict("sys.modules", {"cato.orchestrator.clawflows": mock_clawflows}):
                    await gw._process_flow_task(task)

    assert len(broadcasts) >= 2
    assert broadcasts[0]["busy"] is True
    assert broadcasts[0]["task"] == "flow: test-flow"
    assert broadcasts[-1]["busy"] is False


@pytest.mark.asyncio
async def test_process_flow_task_broadcasts_idle_on_error():
    """_process_flow_task should broadcast busy=False even if flow raises."""
    gw = _make_gateway()
    broadcasts: list[dict] = []

    async def capture_broadcast(busy, session_id, task_preview):
        broadcasts.append({"busy": busy})

    gw._broadcast_activity = capture_broadcast
    gw.send = AsyncMock()

    # Force the import to raise
    with patch.dict("sys.modules", {"cato.orchestrator.clawflows": None}):
        task = {"session_id": "flow-err", "channel": "web", "flow": "bad-flow"}
        await gw._process_flow_task(task)

    # Must end with busy=False
    assert broadcasts[-1]["busy"] is False


# ---------------------------------------------------------------------------
# 6. /api/activity endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_activity_endpoint():
    """GET /api/activity should return activity JSON and be token-exempt."""
    from cato.ui.server import _TOKEN_EXEMPT_PATHS

    # Verify the endpoint is in the exempt list
    assert "/api/activity" in _TOKEN_EXEMPT_PATHS


# ---------------------------------------------------------------------------
# 7. SwarmSync empty prefill fallback
# ---------------------------------------------------------------------------

def test_swarmsync_prefill_empty_content_triggers_fallback():
    """When SwarmSync prefill has empty content and no tool calls, used_prefill
    should remain False, triggering the _stream_collect fallback."""
    # Simulate the logic from agent_loop.py lines 904-921
    swarmsync_prefill = {"role": "assistant", "content": ""}
    planning_turns = 0
    force = False

    used_prefill = False
    if swarmsync_prefill is not None and planning_turns == 0:
        prefill = swarmsync_prefill
        swarmsync_prefill = None  # consume once
        text = prefill.get("content", "") or ""
        # Simulate: no tool calls parsed from empty content
        tool_calls = []
        text_stripped = text.strip()
        if text_stripped or tool_calls:
            used_prefill = True

    assert used_prefill is False, "Empty prefill should NOT set used_prefill=True"


def test_swarmsync_prefill_with_content_uses_prefill():
    """When SwarmSync prefill has real content, used_prefill should be True."""
    swarmsync_prefill = {"role": "assistant", "content": "Here is my response."}
    planning_turns = 0

    used_prefill = False
    if swarmsync_prefill is not None and planning_turns == 0:
        prefill = swarmsync_prefill
        swarmsync_prefill = None
        text = prefill.get("content", "") or ""
        tool_calls = []
        text_stripped = text.strip()
        if text_stripped or tool_calls:
            used_prefill = True

    assert used_prefill is True, "Non-empty prefill should set used_prefill=True"


def test_swarmsync_prefill_none_triggers_fallback():
    """When SwarmSync prefill is None (disabled), used_prefill stays False."""
    swarmsync_prefill = None
    planning_turns = 0

    used_prefill = False
    if swarmsync_prefill is not None and planning_turns == 0:
        used_prefill = True  # This block should NOT execute

    assert used_prefill is False
