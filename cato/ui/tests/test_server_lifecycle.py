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
