from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from cato.config import CatoConfig


integration_routes = pytest.importorskip(
    "cato.api.integration_routes",
    reason="integration route module is not visible yet",
)


class TestIntegrationRoutes(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        app = web.Application()
        integration_routes.register_routes(app)
        return app

    async def test_status_route_redacts_secret_values(self) -> None:
        secret = "ghp_route_secret_that_must_not_leak"
        with (
            patch.object(integration_routes.CatoConfig, "load", return_value=CatoConfig()),
            patch.object(integration_routes, "_load_vault_keys", return_value=({"GITHUB_TOKEN"}, "available")),
            patch.dict(integration_routes.os.environ, {"GITHUB_TOKEN": secret}, clear=False),
        ):
            resp = await self.client.get("/api/integrations/status")

        assert resp.status == 200, await resp.text()
        text = await resp.text()
        data = await resp.json()
        assert secret not in text
        assert data["secrets_returned"] is False
        assert data["live_checks_performed"] is False
        github = next(item for item in data["integrations"] if item["id"] == "github")
        assert github["metadata"]["vault_keys_present"] == ["GITHUB_TOKEN"]
        assert github["metadata"]["env_keys_present"] == ["GITHUB_TOKEN"]

    async def test_catalog_alias_route_redacts_secret_values(self) -> None:
        secret = "xoxb_route_secret_that_must_not_leak"
        with (
            patch.object(integration_routes.CatoConfig, "load", return_value=CatoConfig()),
            patch.object(integration_routes, "_load_vault_keys", return_value=({"SLACK_BOT_TOKEN"}, "available")),
            patch.dict(integration_routes.os.environ, {"SLACK_BOT_TOKEN": secret}, clear=False),
        ):
            resp = await self.client.get("/api/integrations")

        assert resp.status == 200, await resp.text()
        text = await resp.text()
        data = await resp.json()
        assert secret not in text
        assert data["secrets_returned"] is False
        assert data["live_checks_performed"] is False

    async def test_dry_run_action_route_contract_is_pending_until_route_exists(self) -> None:
        resp = await self.client.post(
            "/api/integrations/github/actions/create_issue",
            json={
                "payload": {
                    "owner": "acme",
                    "repo": "private",
                    "title": "No network",
                },
                "dry_run": True,
            },
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["dry_run"] is True
        assert data.get("performed") is not True
        assert data["secrets_returned"] is False

    async def test_write_like_action_route_contract_is_pending_until_route_exists(self) -> None:
        resp = await self.client.post(
            "/api/integrations/github/actions/create_issue",
            json={
                "payload": {
                    "owner": "acme",
                    "repo": "private",
                    "title": "Needs approval",
                },
                "dry_run": True,
            },
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["approval_required"] is True

    async def test_setup_route_generates_oauth_url_without_secret_exchange(self) -> None:
        resp = await self.client.post(
            "/api/integrations/google_workspace/setup",
            json={
                "params": {
                    "client_id": "client-abc",
                    "redirect_uri": "http://localhost/callback",
                    "state": "state-xyz",
                }
            },
        )

        assert resp.status == 200, await resp.text()
        text = await resp.text()
        data = await resp.json()
        assert data["ok"] is True
        assert data["secrets_returned"] is False
        assert data["live_checks_performed"] is False
        assert "oauth_authorization_url" in data
        assert "client-abc" in data["oauth_authorization_url"]
        assert "state-xyz" in data["oauth_authorization_url"]
