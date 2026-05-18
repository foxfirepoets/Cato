from __future__ import annotations

from types import SimpleNamespace

import pytest

from cato.router import ModelRouter
from cato.routing_log import get_persistent_routing_history, record_routing_event
from cato.swarmsync import get_swarmsync_api_key, swarmsync_key_status


def test_swarmsync_key_prefers_canonical_vault_key(monkeypatch):
    vault = SimpleNamespace(get=lambda key: {"SWARMSYNC_API_KEY": "canonical", "SWARM_SYNC_API_KEY": "legacy"}.get(key))

    key, source = get_swarmsync_api_key(vault)
    status = swarmsync_key_status(vault)

    assert key == "canonical"
    assert source == "SWARMSYNC_API_KEY"
    assert status["present"] is True
    assert status["needs_normalization"] is False


def test_routing_log_persists_metadata_fields(tmp_path, monkeypatch):
    import cato.routing_log as routing_log

    monkeypatch.setattr(routing_log, "_DB_PATH", tmp_path / "routing.sqlite3")

    record_routing_event({
        "ts": 123.0,
        "provider": "swarmsync",
        "status": "ok",
        "routed_model": "openrouter/example/model",
        "raw_model": "example/model",
        "complexity": 0.25,
        "has_tools": False,
        "msg_count": 2,
        "http_status": 200,
        "content_chars": 42,
        "tool_call_count": 0,
        "metadata": {
            "request_id": "req-123",
            "timestamp": "2026-05-18T00:00:00+00:00",
            "routing_reason": "cost-efficient for simple prompt",
            "considered_models": ["a", "b"],
            "estimated_cost": 0.001,
            "actual_cost": 0.0008,
            "fallback_routing": False,
            "success": True,
        },
    })

    history = get_persistent_routing_history()

    assert len(history) == 1
    event = history[0]
    assert event["request_id"] == "req-123"
    assert event["routing_reason"] == "cost-efficient for simple prompt"
    assert event["considered_models"] == ["a", "b"]
    assert event["actual_cost"] == 0.0008
    assert event["success"] is True


class _FakeResponse:
    status = 200
    headers = {"X-Request-ID": "req-header"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return {
            "model": "auto",
            "swarmsync": {
                "routed_model": "example/model",
                "routing_reason": "balanced quality and cost",
                "tier": "TIER_A",
                "considered_models": ["example/model", "backup/model"],
                "estimated_cost_usd": 0.002,
                "actual_cost_usd": 0.0015,
            },
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }

    async def text(self):
        return ""


class _FakeSession:
    def post(self, *args, **kwargs):
        return _FakeResponse()


@pytest.mark.asyncio
async def test_swarmsync_completion_records_persistent_route(tmp_path, monkeypatch):
    import cato.routing_log as routing_log

    monkeypatch.setattr(routing_log, "_DB_PATH", tmp_path / "routing.sqlite3")
    router = ModelRouter(vault=None)
    monkeypatch.setattr(router, "_get_session", lambda: _FakeSession())
    monkeypatch.setattr(router, "_resolve_swarmsync_model", lambda raw, score: f"openrouter/{raw}")

    model, message = await router._swarmsync_complete_message(
        [{"role": "user", "content": "hello"}],
        "sk-test",
        0.2,
    )

    history = get_persistent_routing_history()
    assert model == "openrouter/example/model"
    assert message["content"] == "ok"
    assert history[-1]["request_id"] == "req-header"
    assert history[-1]["routed_model"] == "openrouter/example/model"
    assert history[-1]["routing_reason"] == "balanced quality and cost"
    assert history[-1]["considered_models"] == ["example/model", "backup/model"]
    assert history[-1]["estimated_cost"] == 0.002
    assert history[-1]["actual_cost"] == 0.0015
    assert history[-1]["success"] is True
