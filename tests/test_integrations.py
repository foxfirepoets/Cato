from __future__ import annotations

from typing import Any

import pytest

from cato.config import CatoConfig


integration_routes = pytest.importorskip(
    "cato.api.integration_routes",
    reason="new integration route layer is not visible yet",
)


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.extend(_flatten_strings(str(key)))
            out.extend(_flatten_strings(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_flatten_strings(item))
        return out
    return [str(value)]


def _render(value: Any) -> str:
    return "\n".join(_flatten_strings(value))


def test_catalog_status_metadata_redacts_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-live-secret-that-must-not-leak"
    monkeypatch.setenv("SWARMSYNC_API_KEY", secret)
    cfg = CatoConfig()

    status = integration_routes._integration_status(
        integration_routes.IntegrationSpec(
            id="swarmsync",
            name="SwarmSync",
            category="llm_router",
            description="Routes model calls.",
            vault_keys=("SWARMSYNC_API_KEY",),
            env_keys=("SWARMSYNC_API_KEY",),
        ),
        cfg,
        {"SWARMSYNC_API_KEY"},
    )

    rendered = _render(status)
    assert secret not in rendered
    assert "SWARMSYNC_API_KEY" in status["metadata"]["vault_keys_present"]
    assert "SWARMSYNC_API_KEY" in status["metadata"]["env_keys_present"]
    assert status["metadata"]["required_vault_keys"] == ["SWARMSYNC_API_KEY"]


def test_sensitive_config_keys_are_redacted_by_name() -> None:
    assert integration_routes._is_sensitive_key("github_token") is True
    assert integration_routes._is_sensitive_key("stripe_secret") is True
    assert integration_routes._is_sensitive_key("vault_password") is True
    assert integration_routes._is_sensitive_key("openrouter_api_key") is True
    assert integration_routes._is_sensitive_key("workspace_dir") is False

    class _Cfg:
        github_token = "ghp_realtokenvaluewithoutkeyword12345"
        workspace_dir = "/tmp/work"

    subset = integration_routes._config_subset(_Cfg(), ("github_token", "workspace_dir"))
    assert subset["github_token"] == "[redacted]"
    assert subset["workspace_dir"] == "/tmp/work"


def test_vault_presence_marks_secret_configured_without_exposing_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CATO_TEST_INTEGRATION_KEY", raising=False)
    spec = integration_routes.IntegrationSpec(
        id="example",
        name="Example",
        category="test",
        description="Test integration.",
        vault_keys=("CATO_TEST_INTEGRATION_KEY",),
        env_keys=("CATO_TEST_INTEGRATION_KEY",),
    )

    status = integration_routes._integration_status(spec, CatoConfig(), {"CATO_TEST_INTEGRATION_KEY"})

    assert status["configured"] is True
    assert status["metadata"]["vault_keys_present"] == ["CATO_TEST_INTEGRATION_KEY"]
    assert status["metadata"]["env_keys_present"] == []


def test_env_presence_marks_secret_configured_without_exposing_value(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "env-secret-that-must-not-leak"
    monkeypatch.setenv("CATO_TEST_INTEGRATION_KEY", secret)
    spec = integration_routes.IntegrationSpec(
        id="example",
        name="Example",
        category="test",
        description="Test integration.",
        vault_keys=("CATO_TEST_INTEGRATION_KEY",),
        env_keys=("CATO_TEST_INTEGRATION_KEY",),
    )

    status = integration_routes._integration_status(spec, CatoConfig(), set())

    assert status["configured"] is True
    assert status["metadata"]["vault_keys_present"] == []
    assert status["metadata"]["env_keys_present"] == ["CATO_TEST_INTEGRATION_KEY"]
    assert secret not in _render(status)


def test_status_route_uses_shared_builder_catalog() -> None:
    ids = {spec.id for spec in integration_routes._all_integration_specs()}

    for integration_id in {
        "github",
        "vercel",
        "netlify",
        "render",
        "supabase",
        "stripe",
        "google_workspace",
        "notion",
        "slack",
        "discord",
        "telegram",
        "whatsapp",
    }:
        assert integration_id in ids


def test_builder_catalog_has_fuller_action_coverage() -> None:
    from cato.integrations import get_integration

    expected_actions = {
        "github": {"create_repo", "create_issue", "create_pull_request"},
        "vercel": {"create_project", "create_deployment", "set_project_env"},
        "netlify": {"create_site", "trigger_build"},
        "render": {"trigger_deploy"},
        "supabase": {"create_project", "list_organizations"},
        "stripe": {"create_product", "create_price", "create_checkout_session"},
        "google_workspace": {"calendar_create_event", "drive_list_files", "sheets_update"},
        "notion": {"create_page", "update_page", "query_database"},
        "slack": {"post_message", "schedule_message"},
        "discord": {"send_message", "list_channel_messages"},
        "telegram": {"get_me", "send_document"},
        "whatsapp": {"send_text"},
    }

    for integration_id, actions in expected_actions.items():
        definition = get_integration(integration_id)
        assert definition is not None
        assert actions.issubset(definition.actions)


@pytest.mark.asyncio
async def test_dry_run_action_does_not_invoke_network(monkeypatch: pytest.MonkeyPatch) -> None:
    from cato.integrations import runtime
    from cato.integrations.runtime import IntegrationRuntime

    def forbidden_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dry_run action attempted a network call")

    monkeypatch.setattr(runtime, "request_json", forbidden_network)

    result = await IntegrationRuntime().action(
        "github",
        "create_repo",
        {"name": "dry-run-only"},
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["approval_required"] is True
    assert result["planned_request"]["integration"] == "github"
    assert "dry-run-only" not in _render(result)


@pytest.mark.asyncio
async def test_dry_run_plan_redacts_query_values() -> None:
    from cato.integrations.runtime import IntegrationRuntime

    result = await IntegrationRuntime().action(
        "github",
        "list_issues",
        {
            "owner": "acme",
            "repo": "private-repo",
            "labels": "secret-roadmap",
            "state": "open",
        },
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["planned_request"]["query_keys"] == ["labels", "state"]
    assert "secret-roadmap" not in _render(result)


@pytest.mark.asyncio
async def test_write_like_action_requires_explicit_approval_when_live() -> None:
    from cato.integrations.runtime import IntegrationRuntime

    result = await IntegrationRuntime().action(
        "stripe",
        "create_product",
        {"name": "Paid Plan"},
        dry_run=False,
        approved=False,
    )

    assert result["ok"] is False
    assert result["dry_run"] is False
    assert result["approval_required"] is True
    assert "approved=true" in result["error"]


def test_setup_generates_google_oauth_url_without_secret_exchange() -> None:
    from cato.integrations.runtime import IntegrationRuntime

    result = IntegrationRuntime().setup(
        "google_workspace",
        {
            "client_id": "client-123.apps.googleusercontent.com",
            "redirect_uri": "http://localhost:8765/oauth/google/callback",
            "state": "state-abc",
        },
    )

    assert result["ok"] is True
    assert result["secrets_returned"] is False
    assert result["live_checks_performed"] is False
    assert "oauth_authorization_url" in result
    assert "client-123.apps.googleusercontent.com" in result["oauth_authorization_url"]
    assert "state-abc" in result["oauth_authorization_url"]


@pytest.mark.asyncio
async def test_integration_setup_tool_is_public_and_non_secret() -> None:
    import json

    from cato.tools.integration_tool import IntegrationTool

    data = json.loads(await IntegrationTool().setup({"integration": "stripe"}))

    assert data["ok"] is True
    assert data["secrets_returned"] is False
    assert data["live_checks_performed"] is False
    assert data["integration"]["auth_type"] == "api_key"
    assert data["integration"]["setup_steps"]
