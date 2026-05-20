"""
Tests for cato/ui/server.py — focusing on:
- create_ui_app returns a valid aiohttp Application
- on_startup hook calls pool.start_all()
- on_cleanup hook calls pool.stop_all()
- Startup/cleanup failures are swallowed (pool unavailable should not crash server)
- Health endpoint returns expected JSON structure
- Config POST returns ok
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cato.ui import server as server_module
from cato.ui.server import create_ui_app


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

async def _make_app(gateway=None) -> web.Application:
    """Create the UI app with coding-agent routes import suppressed."""
    with patch("cato.ui.server.create_ui_app.__wrapped__", create=True), \
         patch(
             "cato.ui.server.register_all_routes",
             side_effect=ImportError("routes not available in test"),
             create=True,
         ):
        return await create_ui_app(gateway=gateway)


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"X-Cato-Token": server_module._DAEMON_TOKEN}
    if extra:
        headers.update(extra)
    return headers


def _cli_pool_startup_task(app: web.Application) -> asyncio.Task:
    return app[server_module._CLI_POOL_STARTUP_TASK_KEY]


# ------------------------------------------------------------------ #
# App creation                                                        #
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_create_ui_app_returns_application():
    """create_ui_app must return a web.Application instance."""
    app = await create_ui_app()
    assert isinstance(app, web.Application)


@pytest.mark.asyncio
async def test_create_ui_app_registers_startup_hook():
    """on_startup must contain at least one callable (the pool starter)."""
    app = await create_ui_app()
    assert len(app.on_startup) >= 1


@pytest.mark.asyncio
async def test_create_ui_app_registers_cleanup_hook():
    """on_cleanup must contain at least one callable (the pool stopper)."""
    app = await create_ui_app()
    assert len(app.on_cleanup) >= 1


# ------------------------------------------------------------------ #
# Startup / cleanup lifecycle                                         #
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_startup_calls_pool_start_all():
    """on_startup hook must schedule pool.start_all() without blocking startup."""
    mock_pool = MagicMock()
    mock_pool.start_all = AsyncMock()
    mock_pool.stop_all = AsyncMock()

    async def no_sleep(_seconds):
        return None

    with patch("cato.orchestrator.cli_process_pool.get_pool", return_value=mock_pool), \
         patch("cato.ui.server.asyncio.sleep", side_effect=no_sleep):
        app = await create_ui_app()
        # Run all startup signals manually
        for hook in app.on_startup:
            await hook(app)
        await _cli_pool_startup_task(app)

    mock_pool.start_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_calls_pool_stop_all():
    """on_cleanup hook must call pool.stop_all()."""
    mock_pool = MagicMock()
    mock_pool.start_all = AsyncMock()
    mock_pool.stop_all = AsyncMock()

    with patch("cato.orchestrator.cli_process_pool.get_pool", return_value=mock_pool):
        app = await create_ui_app()
        for hook in app.on_cleanup:
            await hook(app)

    mock_pool.stop_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_failure_does_not_raise():
    """If the pool raises during start_all, the startup hook must not propagate."""
    mock_pool = MagicMock()
    mock_pool.start_all = AsyncMock(side_effect=RuntimeError("pool boom"))
    mock_pool.stop_all = AsyncMock()

    async def no_sleep(_seconds):
        return None

    with patch("cato.orchestrator.cli_process_pool.get_pool", return_value=mock_pool), \
         patch("cato.ui.server.asyncio.sleep", side_effect=no_sleep):
        app = await create_ui_app()
        # Must not raise
        for hook in app.on_startup:
            await hook(app)
        await _cli_pool_startup_task(app)


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_raise():
    """If pool.stop_all raises during cleanup, the cleanup hook must not propagate."""
    mock_pool = MagicMock()
    mock_pool.start_all = AsyncMock()
    mock_pool.stop_all = AsyncMock(side_effect=RuntimeError("stop boom"))

    with patch("cato.orchestrator.cli_process_pool.get_pool", return_value=mock_pool):
        app = await create_ui_app()
        for hook in app.on_cleanup:
            await hook(app)


@pytest.mark.asyncio
async def test_startup_import_error_does_not_raise():
    """If cli_process_pool cannot even be imported, startup must be silent."""
    app = await create_ui_app()

    # Patch get_pool inside the server module's hook by making the import fail
    with patch.dict("sys.modules", {"cato.orchestrator.cli_process_pool": None}):
        for hook in app.on_startup:
            try:
                await hook(app)
            except Exception:
                pytest.fail("Startup hook must never propagate exceptions")


# ------------------------------------------------------------------ #
# Health endpoint                                                     #
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_health_endpoint_returns_ok():
    """GET /health must return JSON with status == 'ok'."""
    app = await create_ui_app(gateway=None)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "sessions" in data
        assert "uptime" in data


@pytest.mark.asyncio
async def test_health_endpoint_sessions_zero_without_gateway():
    """Without a gateway, sessions count must be 0."""
    app = await create_ui_app(gateway=None)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        data = await resp.json()
        assert data["sessions"] == 0


@pytest.mark.asyncio
async def test_health_endpoint_sessions_from_gateway():
    """With a gateway, sessions count comes from len(gateway._lanes)."""
    gateway = MagicMock()
    gateway._lanes = {"a": 1, "b": 2}
    app = await create_ui_app(gateway=gateway)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        data = await resp.json()
        assert data["sessions"] == 2


@pytest.mark.asyncio
async def test_routing_status_accepts_legacy_swarmsync_key_without_live_test():
    """Routing status reports normalized legacy key source without requiring a live call."""
    cfg = MagicMock()
    cfg.swarmsync_enabled = False
    cfg.swarmsync_api_url = "https://example.invalid/v1/chat/completions"
    cfg.default_model = "openrouter/minimax/minimax-m2.5"
    vault = MagicMock()
    vault.get.side_effect = lambda key: {"SWARM_SYNC_API_KEY": "legacy-key"}.get(key, "")
    gateway = MagicMock()
    gateway._cfg = cfg
    gateway._vault = vault
    gateway._lanes = {}
    app = await create_ui_app(gateway=gateway)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/routing/status", headers=_auth_headers())
        assert resp.status == 200
        data = await resp.json()
        assert data["swarm_key_present"] is True
        assert data["swarm_key_source"] == "SWARM_SYNC_API_KEY"
        assert data["swarm_key_needs_normalization"] is True


@pytest.mark.asyncio
async def test_usage_routing_returns_evidence_contract(monkeypatch):
    """Routing history API exposes the full SwarmSync evidence trail."""
    app = await create_ui_app(gateway=None)

    monkeypatch.setattr(
        "cato.router.get_routing_history",
        lambda: [
            {
                "ts": 1770000000.0,
                "timestamp": "2026-05-19T12:00:00+00:00",
                "request_id": "req-api-1",
                "provider": "swarmsync",
                "status": "ok",
                "success": True,
                "routed_model": "openrouter/minimax/minimax-m2.5",
                "raw_model": "minimax/minimax-m2.5",
                "routing_reason": "balanced quality and cost",
                "tier": "economy",
                "considered_models": ["minimax/minimax-m2.5", "gemini/flash"],
                "fallback_routing": False,
                "estimated_cost": 0.002,
                "actual_cost": 0.0015,
                "complexity_score": 0.2,
                "history_length": 2,
                "has_tools": False,
                "http_status": 200,
                "content_chars": 12,
                "tool_call_count": 0,
                "error": "",
            }
        ],
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/usage/routing?limit=1", headers=_auth_headers())
        assert resp.status == 200
        data = await resp.json()
        event = data["events"][0]
        assert data["log_path"]
        assert "request_id" in data["fields"]
        assert event["request_id"] == "req-api-1"
        assert event["routing_reason"] == "balanced quality and cost"
        assert event["considered_models"] == ["minimax/minimax-m2.5", "gemini/flash"]
        assert event["fallback_routing"] is False
        assert event["estimated_cost"] == 0.002
        assert event["actual_cost"] == 0.0015
        assert event["success"] is True
        assert event["timestamp"] == "2026-05-19T12:00:00+00:00"


@pytest.mark.asyncio
async def test_diagnostics_export_returns_attachment_bundle(monkeypatch):
    """Diagnostics export bundles doctor, routing, usage events, logs, and config."""
    class FakeConfig:
        swarmsync_enabled = True
        swarmsync_api_url = "https://api.swarmsync.invalid/v1/chat/completions"
        default_model = "openrouter/minimax/minimax-m2.5"

        def to_dict(self):
            return {
                "swarmsync_enabled": True,
                "swarmsync_api_url": self.swarmsync_api_url,
                "default_model": self.default_model,
                "telegram_bot_token": "123456789:telegram-secret-token",
                "nested": {"api_key": "sk-live-secretvalue"},
            }

    vault = MagicMock()
    vault.get.side_effect = lambda key: {
        "SWARMSYNC_API_KEY": "ssync-super-secret-key",
        "OPENROUTER_API_KEY": "sk-openrouter-secret",
    }.get(key, "")
    gateway = MagicMock()
    gateway._cfg = FakeConfig()
    gateway._vault = vault
    gateway._lanes = {}

    monkeypatch.setenv("SWARMSYNC_API_KEY", "")
    monkeypatch.setattr(
        "cato.router.get_routing_history",
        lambda: [
            {
                "request_id": "req-diag-1",
                "routing_reason": "used SWARMSYNC_API_KEY=ssync-super-secret-key",
                "error": "Bearer sk-live-secretvalue",
            }
        ],
    )

    async def fake_doctor_output():
        return {
            "status": "ok",
            "output": "doctor ok token=doctor-secret",
            "failures": [],
        }

    monkeypatch.setattr(server_module, "_collect_doctor_output", fake_doctor_output)
    monkeypatch.setattr(
        server_module,
        "_collect_daemon_log_files",
        lambda: [
            {
                "path": "daemon.log",
                "tail": "password=hunter2 api_key=sk-live-secretvalue",
            }
        ],
    )

    app = await create_ui_app(gateway=gateway)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/diagnostics/export?limit=1", headers=_auth_headers())
        assert resp.status == 200
        assert resp.headers["Content-Disposition"].startswith('attachment; filename="cato-diagnostics-')
        data = await resp.json()

    assert data["schema_version"] == 1
    assert data["doctor"]["status"] == "ok"
    assert data["routing_status"]["swarmsync_enabled"] is True
    assert data["routing_status"]["live_test"]["skipped"] is True
    assert data["routing_events"][0]["request_id"] == "req-diag-1"
    assert data["logs"]["files"][0]["path"] == "daemon.log"

    serialized = json.dumps(data)
    assert "telegram-secret-token" not in serialized
    assert "ssync-super-secret-key" not in serialized
    assert "sk-live-secretvalue" not in serialized
    assert "hunter2" not in serialized
    assert "[redacted]" in serialized


def test_diagnostics_redaction_recurses_config_style_fields():
    """Diagnostics redaction catches nested config/vault/key/token/password fields."""
    payload = {
        "vault": {"OPENROUTER_API_KEY": "sk-live-secretvalue"},
        "auth": {"token": "abc123"},
        "nested": [{"password": "hunter2"}, {"message": "Bearer abc.def.ghi"}],
        "safe": "plain text",
    }

    redacted = server_module._redact_diagnostics_data(payload)
    serialized = json.dumps(redacted)

    assert "sk-live-secretvalue" not in serialized
    assert "abc123" not in serialized
    assert "hunter2" not in serialized
    assert "Bearer abc.def.ghi" not in serialized
    assert redacted["safe"] == "plain text"


# ------------------------------------------------------------------ #
# Config endpoint                                                     #
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_config_post_returns_ok():
    """POST /config with valid JSON must return {status: ok}."""
    app = await create_ui_app()

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/config", json={"theme": "dark"}, headers=_auth_headers())
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_config_post_invalid_json_returns_400():
    """POST /config with invalid body returns 400."""
    app = await create_ui_app()

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/config",
            data="not valid json",
            headers=_auth_headers({"Content-Type": "application/json"}),
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_config_get_includes_approval_policy_defaults(tmp_path):
    """GET /api/config exposes desktop approval policy fields."""
    with patch("cato.platform.get_data_dir", return_value=tmp_path):
        app = await create_ui_app()

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/config", headers=_auth_headers())
            assert resp.status == 200
            data = await resp.json()

    assert data["strict_approval"] is False
    assert isinstance(data["auto_approved_tools"], list)
    assert "memory.search" in data["auto_approved_tools"]


@pytest.mark.asyncio
async def test_config_patch_persists_approval_policy(tmp_path):
    """PATCH /api/config persists strict_approval and auto_approved_tools."""
    import yaml

    with patch("cato.platform.get_data_dir", return_value=tmp_path):
        app = await create_ui_app()

        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/config",
                json={
                    "strict_approval": True,
                    "auto_approved_tools": ["memory.search", "web.search"],
                },
                headers=_auth_headers(),
            )
            assert resp.status == 200
            data = await resp.json()

    persisted = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert data["config"]["strict_approval"] is True
    assert data["config"]["auto_approved_tools"] == ["memory.search", "web.search"]
    assert persisted["strict_approval"] is True
    assert persisted["auto_approved_tools"] == ["memory.search", "web.search"]


@pytest.mark.asyncio
async def test_config_patch_response_redacts_sensitive_keys(tmp_path):
    """PATCH /api/config must not echo plaintext legacy secrets."""
    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "telegram_bot_token": "123456789:secret-token",
                "openrouter_api_key": "sk-secret",
                "default_model": "openrouter/minimax/minimax-m2.5",
            }
        ),
        encoding="utf-8",
    )

    with patch("cato.platform.get_data_dir", return_value=tmp_path):
        app = await create_ui_app()

        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/config",
                json={"strict_approval": True},
                headers=_auth_headers(),
            )
            assert resp.status == 200
            data = await resp.json()

    serialized = json.dumps(data)
    assert "telegram_bot_token" not in data["config"]
    assert "openrouter_api_key" not in data["config"]
    assert "secret-token" not in serialized
    assert "sk-secret" not in serialized
    assert data["config"]["strict_approval"] is True


# ------------------------------------------------------------------ #
# HTML page routes                                                    #
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_dashboard_route_serves_html():
    """GET / must return 200 with HTML content."""
    app = await create_ui_app()

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/")
        assert resp.status == 200
        content_type = resp.headers.get("Content-Type", "")
        assert "html" in content_type


@pytest.mark.asyncio
async def test_coding_agent_route_serves_html():
    """GET /coding-agent must return 200 with HTML content."""
    app = await create_ui_app()

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/coding-agent")
        assert resp.status == 200


@pytest.mark.asyncio
async def test_coding_agent_task_id_route_serves_html():
    """GET /coding-agent/{task_id} must return 200."""
    app = await create_ui_app()

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/coding-agent/abc-123")
        assert resp.status == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
