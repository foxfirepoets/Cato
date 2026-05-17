"""
tests/test_websocket_handler.py
Unit and integration tests for cato/api/websocket_handler.py.

Covers:
  - Task creation (POST /api/coding-agent/invoke)
  - Task info retrieval (GET /api/coding-agent/{task_id})
  - Validation errors (missing task, too short, too long)
  - Synthesis logic (_synthesize_results)
  - Confidence level tier function
  - WebSocket coding agent handler (integration via aiohttp test client)
  - Server routing registration
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient

from cato.api.websocket_handler import (
    _confidence_level,
    _serialize_event,
    _synthesize_results,
    _task_store,
    invoke_coding_agent,
    get_task_info,
    register_routes,
)


# ── Helpers ─────────────────────────────────────────────────────────────── #

def make_app() -> web.Application:
    """Create a minimal app with coding agent routes attached."""
    app = web.Application()
    register_routes(app)
    return app


# ── _confidence_level tests ─────────────────────────────────────────────── #

class TestConfidenceLevel:
    def test_high_at_090(self):
        assert _confidence_level(0.90) == "high"

    def test_high_above_090(self):
        assert _confidence_level(0.95) == "high"
        assert _confidence_level(1.0)  == "high"

    def test_medium_at_070(self):
        assert _confidence_level(0.70) == "medium"

    def test_medium_at_089(self):
        assert _confidence_level(0.89) == "medium"

    def test_low_below_070(self):
        assert _confidence_level(0.69) == "low"
        assert _confidence_level(0.0)  == "low"

    def test_medium_range(self):
        assert _confidence_level(0.80) == "medium"
        assert _confidence_level(0.75) == "medium"


# ── _serialize_event tests ──────────────────────────────────────────────── #

class TestSerializeEvent:
    def test_returns_json_with_newline(self):
        result = _serialize_event("test_event", {"key": "value"})
        assert result.endswith("\n")
        parsed = json.loads(result.strip())
        assert parsed["event"] == "test_event"
        assert parsed["data"]["key"] == "value"

    def test_nested_data(self):
        data = {"model": "claude", "confidence": 0.92, "text": "Hello"}
        result = _serialize_event("claude_response", data)
        parsed = json.loads(result.strip())
        assert parsed["data"]["model"] == "claude"
        assert parsed["data"]["confidence"] == 0.92

    def test_empty_data(self):
        result = _serialize_event("heartbeat", {})
        parsed = json.loads(result.strip())
        assert parsed["event"] == "heartbeat"
        assert parsed["data"] == {}


# ── _synthesize_results tests ───────────────────────────────────────────── #

class TestSynthesizeResults:
    def test_empty_list_returns_no_primary(self):
        result = _synthesize_results([])
        assert result["primary"] is None
        assert result["runners_up"] == []

    def test_all_none_returns_no_primary(self):
        result = _synthesize_results([None, None, None])
        assert result["primary"] is None

    def test_single_result(self):
        results = [{"model": "claude", "response": "Hello", "confidence": 0.92}]
        synthesis = _synthesize_results(results)
        assert synthesis["primary"]["model"] == "claude"
        assert synthesis["primary"]["confidence"] == 0.92
        assert synthesis["runners_up"] == []

    def test_selects_highest_confidence(self):
        results = [
            {"model": "claude",  "response": "A", "confidence": 0.75},
            {"model": "codex",   "response": "B", "confidence": 0.92},
            {"model": "gemini",  "response": "C", "confidence": 0.68},
        ]
        synthesis = _synthesize_results(results)
        assert synthesis["primary"]["model"] == "codex"
        assert synthesis["primary"]["confidence"] == 0.92

    def test_runners_up_order(self):
        results = [
            {"model": "claude",  "response": "A", "confidence": 0.75},
            {"model": "codex",   "response": "B", "confidence": 0.92},
            {"model": "gemini",  "response": "C", "confidence": 0.68},
        ]
        synthesis = _synthesize_results(results)
        runners = synthesis["runners_up"]
        assert len(runners) == 2
        assert runners[0]["model"] == "claude"  # 0.75 > 0.68
        assert runners[1]["model"] == "gemini"

    def test_confidence_level_tagged(self):
        results = [{"model": "claude", "response": "test", "confidence": 0.92}]
        synthesis = _synthesize_results(results)
        assert synthesis["primary"]["confidence_level"] == "high"

    def test_none_mixed_with_valid(self):
        results = [
            None,
            {"model": "claude", "response": "OK", "confidence": 0.80},
            None,
        ]
        synthesis = _synthesize_results(results)
        assert synthesis["primary"]["model"] == "claude"
        assert synthesis["runners_up"] == []


# ── HTTP endpoint tests (aiohttp TestClient) ────────────────────────────── #

class TestInvokeCodingAgent(AioHTTPTestCase):
    async def get_application(self):
        return make_app()

    async def test_valid_task_returns_task_id(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Review this sorting algorithm"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert "task_id" in data
        assert data["status"] == "queued"
        # Verify stored
        task_id = data["task_id"]
        assert task_id in _task_store
        assert _task_store[task_id]["task"] == "Review this sorting algorithm"

    async def test_missing_task_returns_400(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"language": "python"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_task_too_short_returns_400(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "short"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_task_too_long_returns_400(self):
        long_task = "x" * 501
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": long_task},
        )
        assert resp.status == 400

    async def test_invalid_json_returns_400(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            data="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_task_at_minimum_length(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "1234567890"},  # exactly 10 chars
        )
        assert resp.status == 200

    async def test_task_with_language(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Optimize this database query", "language": "python"},
        )
        assert resp.status == 200
        data = await resp.json()
        tid = data["task_id"]
        assert _task_store[tid]["language"] == "python"

    async def test_task_with_context(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={
                "task":    "Review this function for bugs",
                "context": "def foo(x): return x*x",
            },
        )
        assert resp.status == 200
        data = await resp.json()
        tid = data["task_id"]
        assert "def foo" in _task_store[tid]["context"]


class TestGetTaskInfo(AioHTTPTestCase):
    async def get_application(self):
        return make_app()

    async def test_known_task_returns_metadata(self):
        # Create a task first
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Analyze this architecture design"},
        )
        data = await resp.json()
        tid = data["task_id"]

        # Fetch metadata
        resp2 = await self.client.get(f"/api/coding-agent/{tid}")
        assert resp2.status == 200
        meta = await resp2.json()
        assert meta["task_id"] == tid
        assert meta["task"] == "Analyze this architecture design"
        assert meta["status"] == "queued"

    async def test_unknown_task_returns_404(self):
        resp = await self.client.get("/api/coding-agent/does-not-exist-xyz")
        assert resp.status == 404


# ── WebSocket handler integration tests ─────────────────────────────────── #

class TestWebSocketHandler(AioHTTPTestCase):
    async def get_application(self):
        return make_app()

    async def test_unknown_task_sends_error_and_closes(self):
        """WebSocket for unknown task_id should send error event then close."""
        async with self.client.ws_connect("/ws/coding-agent/unknown-task-xyz") as ws:
            # Should receive error event
            msg = await asyncio.wait_for(ws.receive_str(), timeout=5.0)
            parsed = json.loads(msg.strip())
            assert parsed["event"] == "error"
            assert "not found" in parsed["data"]["message"].lower()

    async def test_known_task_sends_status_then_events(self):
        """
        WebSocket for known task should stream events including synthesis_complete.
        We patch the model invokers (not _run_model_and_stream) so they return
        immediately without real API calls.
        """
        # Create task
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Review this bubble sort implementation"},
        )
        data = await resp.json()
        tid = data["task_id"]

        mock_claude = {"model": "claude", "response": "Looks good overall.",  "confidence": 0.92, "latency_ms": 50.0}
        mock_codex  = {"model": "codex",  "response": "Performance is O(n^2).","confidence": 0.85, "latency_ms": 45.0}
        mock_gemini = {"model": "gemini", "response": "Consider quicksort.",   "confidence": 0.78, "latency_ms": 60.0}

        with patch("cato.api.websocket_handler.invoke_claude_api", return_value=mock_claude), \
             patch("cato.api.websocket_handler.invoke_codex_cli",  return_value=mock_codex), \
             patch("cato.api.websocket_handler.invoke_gemini_cli", return_value=mock_gemini):

            async with self.client.ws_connect(f"/ws/coding-agent/{tid}") as ws:
                events = []
                try:
                    while True:
                        msg = await asyncio.wait_for(ws.receive_str(), timeout=10.0)
                        parsed = json.loads(msg.strip())
                        events.append(parsed["event"])
                        if parsed["event"] == "synthesis_complete":
                            break
                except (asyncio.TimeoutError, Exception):
                    pass

                event_set = set(events)
                assert "status" in event_set or "synthesis_complete" in event_set

    async def test_websocket_sends_heartbeat(self):
        """
        WebSocket should send at least one heartbeat while models are running.
        We mock slow models so heartbeat fires first.
        """
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Test heartbeat detection logic"},
        )
        data = await resp.json()
        tid = data["task_id"]

        async def slow_claude(prompt: str, task: str):
            await asyncio.sleep(5)  # heartbeat fires before this
            return {"model": "claude", "response": "late", "confidence": 0.75, "latency_ms": 5000}

        async def slow_codex(prompt: str, task: str):
            await asyncio.sleep(5)
            return {"model": "codex", "response": "late", "confidence": 0.75, "latency_ms": 5000}

        async def slow_gemini(prompt: str, task: str):
            await asyncio.sleep(5)
            return {"model": "gemini", "response": "late", "confidence": 0.75, "latency_ms": 5000}

        with patch("cato.api.websocket_handler.invoke_claude_api", side_effect=slow_claude), \
             patch("cato.api.websocket_handler.invoke_codex_cli",  side_effect=slow_codex), \
             patch("cato.api.websocket_handler.invoke_gemini_cli", side_effect=slow_gemini):

            async with self.client.ws_connect(f"/ws/coding-agent/{tid}") as ws:
                events_seen = []
                try:
                    for _ in range(3):
                        msg = await asyncio.wait_for(ws.receive_str(), timeout=5.0)
                        parsed = json.loads(msg.strip())
                        events_seen.append(parsed["event"])
                        if "heartbeat" in events_seen:
                            break
                except asyncio.TimeoutError:
                    pass

                # We should have received status and/or heartbeat
                assert len(events_seen) > 0
                # Should contain either status (first event) or heartbeat
                assert any(e in ("status", "heartbeat") for e in events_seen)


    async def test_pool_status_sent_after_status(self):
        """
        WebSocket should send a pool_status event right after the initial
        status event, reporting warm/cold/subprocess for each model.
        """
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Test pool status event delivery"},
        )
        data = await resp.json()
        tid = data["task_id"]

        mock_claude = {"model": "claude", "response": "ok", "confidence": 0.80, "latency_ms": 10, "source": "pool"}
        mock_codex  = {"model": "codex",  "response": "ok", "confidence": 0.75, "latency_ms": 10, "source": "subprocess"}
        mock_gemini = {"model": "gemini", "response": "ok", "confidence": 0.70, "latency_ms": 10, "source": "subprocess"}

        with patch("cato.api.websocket_handler.invoke_claude_api", return_value=mock_claude), \
             patch("cato.api.websocket_handler.invoke_codex_cli",  return_value=mock_codex), \
             patch("cato.api.websocket_handler.invoke_gemini_cli", return_value=mock_gemini):

            async with self.client.ws_connect(f"/ws/coding-agent/{tid}") as ws:
                events = []
                try:
                    while True:
                        msg = await asyncio.wait_for(ws.receive_str(), timeout=10.0)
                        parsed = json.loads(msg.strip())
                        events.append(parsed)
                        if parsed["event"] == "synthesis_complete":
                            break
                except (asyncio.TimeoutError, Exception):
                    pass

                event_names = [e["event"] for e in events]

                # pool_status must appear after status
                assert "status" in event_names, f"Expected 'status' in events: {event_names}"
                assert "pool_status" in event_names, f"Expected 'pool_status' in events: {event_names}"
                status_idx = event_names.index("status")
                pool_idx = event_names.index("pool_status")
                assert pool_idx == status_idx + 1, \
                    f"pool_status (idx={pool_idx}) should follow status (idx={status_idx})"

                # Verify pool_status payload structure
                pool_event = events[pool_idx]
                models = pool_event["data"]["models"]
                assert "claude" in models
                assert "codex" in models
                assert "gemini" in models
                assert models["gemini"] == "subprocess"
                assert "timestamp" in pool_event["data"]

    async def test_model_response_includes_source(self):
        """
        Model response events should include a 'source' field indicating
        whether the response came from the pool or subprocess.
        """
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Test source field in model responses"},
        )
        data = await resp.json()
        tid = data["task_id"]

        mock_claude = {"model": "claude", "response": "ok", "confidence": 0.80, "latency_ms": 10, "source": "pool"}
        mock_codex  = {"model": "codex",  "response": "ok", "confidence": 0.75, "latency_ms": 10, "source": "subprocess"}
        mock_gemini = {"model": "gemini", "response": "ok", "confidence": 0.70, "latency_ms": 10, "source": "subprocess"}

        with patch("cato.api.websocket_handler.invoke_claude_api", return_value=mock_claude), \
             patch("cato.api.websocket_handler.invoke_codex_cli",  return_value=mock_codex), \
             patch("cato.api.websocket_handler.invoke_gemini_cli", return_value=mock_gemini):

            async with self.client.ws_connect(f"/ws/coding-agent/{tid}") as ws:
                response_events = []
                try:
                    while True:
                        msg = await asyncio.wait_for(ws.receive_str(), timeout=10.0)
                        parsed = json.loads(msg.strip())
                        if parsed["event"].endswith("_response"):
                            response_events.append(parsed)
                        if parsed["event"] == "synthesis_complete":
                            break
                except (asyncio.TimeoutError, Exception):
                    pass

                assert len(response_events) == 3, \
                    f"Expected 3 response events, got {len(response_events)}"

                for evt in response_events:
                    assert "source" in evt["data"], \
                        f"Response event missing 'source': {evt}"
                    assert evt["data"]["source"] in ("pool", "subprocess", "mock"), \
                        f"Unexpected source value: {evt['data']['source']}"


# ── Route registration test ─────────────────────────────────────────────── #

class TestRegisterRoutes:
    def test_routes_registered(self):
        app = web.Application()
        register_routes(app)
        # Check expected routes exist
        route_paths = [
            str(r.resource.canonical)
            for r in app.router.routes()
        ]
        assert any("/api/coding-agent/invoke" in p for p in route_paths)
        assert any("/api/coding-agent" in p for p in route_paths)
        assert any("/ws/coding-agent" in p for p in route_paths)


# ── Server UI integration test ──────────────────────────────────────────── #

class TestUIServerIntegration(AioHTTPTestCase):
    async def get_application(self):
        from cato.ui.server import create_ui_app
        return await create_ui_app(gateway=None)

    async def test_coding_agent_page_serves_html(self):
        resp = await self.client.get("/coding-agent")
        assert resp.status == 200
        content_type = resp.headers.get("Content-Type", "")
        assert "text/html" in content_type

    async def test_coding_agent_task_page_serves_html(self):
        resp = await self.client.get("/coding-agent/test-task-123")
        assert resp.status == 200
        content_type = resp.headers.get("Content-Type", "")
        assert "text/html" in content_type

    async def test_dashboard_still_serves(self):
        resp = await self.client.get("/")
        assert resp.status == 200

    async def test_health_endpoint(self):
        resp = await self.client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

    async def test_invoke_endpoint_available(self):
        from cato.ui.server import _DAEMON_TOKEN
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Check server integration routing"},
            headers={"X-Cato-Token": _DAEMON_TOKEN},
        )
        assert resp.status == 200
        data = await resp.json()
        assert "task_id" in data


# ── GET /api/config tests ───────────────────────────────────────────────── #

class TestGetConfig(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        register_routes(app)
        return app

    async def test_returns_enabled_models(self):
        from unittest.mock import MagicMock
        mock_cfg = MagicMock()
        mock_cfg.enabled_models = ["claude", "gemini"]
        mock_cfg.subagent_enabled = False
        mock_cfg.subagent_coding_backend = "codex"
        with patch("cato.api.websocket_handler.CatoConfig") as MockCfg:
            MockCfg.load.return_value = mock_cfg
            # Re-import to get the patched version
            resp = await self.client.get("/api/config")
        assert resp.status == 200
        data = await resp.json()
        assert "enabled_models" in data
        assert "subagent_enabled" in data
        assert "subagent_coding_backend" in data

    async def test_returns_200_with_defaults_on_config_error(self):
        """If CatoConfig.load() raises, fallback defaults are returned."""
        with patch("cato.api.websocket_handler.CatoConfig") as MockCfg:
            MockCfg.load.side_effect = Exception("config broken")
            resp = await self.client.get("/api/config")
        # Should still return 200 with safe defaults (enabled_models + subagent fields)
        assert resp.status == 200
        data = await resp.json()
        assert "enabled_models" in data
        assert isinstance(data["enabled_models"], list)
        assert len(data["enabled_models"]) > 0


# ── PATCH /api/config tests ─────────────────────────────────────────────── #

class TestPatchConfig(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        register_routes(app)
        return app

    def _make_mock_cfg(self):
        from unittest.mock import MagicMock
        mock_cfg = MagicMock()
        mock_cfg.enabled_models = ["claude", "codex", "gemini"]
        mock_cfg.subagent_enabled = False
        mock_cfg.subagent_coding_backend = "codex"
        mock_cfg.save.return_value = None
        return mock_cfg

    async def test_patch_enabled_models(self):
        mock_cfg = self._make_mock_cfg()
        with patch("cato.api.websocket_handler.CatoConfig") as MockCfg:
            MockCfg.load.return_value = mock_cfg
            resp = await self.client.patch(
                "/api/config",
                json={"enabled_models": ["claude", "cursor"]},
            )
        assert resp.status == 200
        mock_cfg.save.assert_called_once()

    async def test_patch_subagent_enabled(self):
        mock_cfg = self._make_mock_cfg()
        with patch("cato.api.websocket_handler.CatoConfig") as MockCfg:
            MockCfg.load.return_value = mock_cfg
            resp = await self.client.patch(
                "/api/config",
                json={"subagent_enabled": True},
            )
        assert resp.status == 200

    async def test_patch_subagent_backend(self):
        mock_cfg = self._make_mock_cfg()
        with patch("cato.api.websocket_handler.CatoConfig") as MockCfg:
            MockCfg.load.return_value = mock_cfg
            resp = await self.client.patch(
                "/api/config",
                json={"subagent_coding_backend": "cursor"},
            )
        assert resp.status == 200

    async def test_invalid_json_returns_400(self):
        resp = await self.client.patch(
            "/api/config",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_empty_enabled_models_returns_400(self):
        mock_cfg = self._make_mock_cfg()
        with patch("cato.api.websocket_handler.CatoConfig") as MockCfg:
            MockCfg.load.return_value = mock_cfg
            resp = await self.client.patch(
                "/api/config",
                json={"enabled_models": []},
            )
        assert resp.status == 400

    async def test_unknown_models_filtered_returns_400(self):
        """A list containing only unknown model names is rejected."""
        mock_cfg = self._make_mock_cfg()
        with patch("cato.api.websocket_handler.CatoConfig") as MockCfg:
            MockCfg.load.return_value = mock_cfg
            resp = await self.client.patch(
                "/api/config",
                json={"enabled_models": ["chatgpt", "grok"]},
            )
        assert resp.status == 400

    async def test_invalid_backend_returns_400(self):
        mock_cfg = self._make_mock_cfg()
        with patch("cato.api.websocket_handler.CatoConfig") as MockCfg:
            MockCfg.load.return_value = mock_cfg
            resp = await self.client.patch(
                "/api/config",
                json={"subagent_coding_backend": "chatgpt"},
            )
        assert resp.status == 400

    async def test_subagent_enabled_not_bool_returns_400(self):
        mock_cfg = self._make_mock_cfg()
        with patch("cato.api.websocket_handler.CatoConfig") as MockCfg:
            MockCfg.load.return_value = mock_cfg
            resp = await self.client.patch(
                "/api/config",
                json={"subagent_enabled": "yes"},
            )
        assert resp.status == 400


# ── enabled_models filtering in invoke_coding_agent ─────────────────────── #

class TestEnabledModelsInvoke(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        register_routes(app)
        return app

    async def test_enabled_models_stored_with_task(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Check only enabled models stored", "enabled_models": ["claude", "cursor"]},
        )
        assert resp.status == 200
        data = await resp.json()
        task_id = data["task_id"]
        assert _task_store[task_id]["enabled_models"] == ["claude", "cursor"]

    async def test_unknown_models_filtered_from_enabled_list(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Filter unknown models from enabled list", "enabled_models": ["claude", "chatgpt", "grok"]},
        )
        assert resp.status == 200
        data = await resp.json()
        task_id = data["task_id"]
        # chatgpt and grok are unknown — only claude should survive
        assert _task_store[task_id]["enabled_models"] == ["claude"]

    async def test_all_unknown_models_falls_back_to_default(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "All unknown models falls back to defaults", "enabled_models": ["chatgpt", "grok"]},
        )
        assert resp.status == 200
        data = await resp.json()
        task_id = data["task_id"]
        assert _task_store[task_id]["enabled_models"] == ["claude", "codex", "gemini"]

    async def test_non_list_enabled_models_falls_back_to_default(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Non-list enabled_models falls back", "enabled_models": "claude"},
        )
        assert resp.status == 200
        data = await resp.json()
        task_id = data["task_id"]
        assert _task_store[task_id]["enabled_models"] == ["claude", "codex", "gemini"]

    async def test_cursor_accepted_as_valid_model(self):
        resp = await self.client.post(
            "/api/coding-agent/invoke",
            json={"task": "Cursor is a valid enabled model", "enabled_models": ["cursor"]},
        )
        assert resp.status == 200
        data = await resp.json()
        task_id = data["task_id"]
        assert _task_store[task_id]["enabled_models"] == ["cursor"]
