"""
cato/api/integration_routes.py — Read-only integration catalog/status endpoints.

These routes report configuration metadata only. They never return credential
values and they do not make live calls to third-party services.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from cato.config import CatoConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegrationSpec:
    id: str
    name: str
    category: str
    description: str
    vault_keys: tuple[str, ...] = ()
    env_keys: tuple[str, ...] = ()
    config_keys: tuple[str, ...] = ()
    enable_config_key: str | None = None
    requires_secret: bool = True


_INTEGRATIONS: tuple[IntegrationSpec, ...] = (
    IntegrationSpec(
        id="swarmsync",
        name="SwarmSync",
        category="llm_router",
        description="Routes Cato LLM calls through SwarmSync intelligent model selection.",
        vault_keys=("SWARMSYNC_API_KEY",),
        env_keys=("SWARMSYNC_API_KEY",),
        config_keys=("swarmsync_enabled", "swarmsync_api_url"),
        enable_config_key="swarmsync_enabled",
    ),
    IntegrationSpec(
        id="openrouter",
        name="OpenRouter",
        category="llm_provider",
        description="Fallback model provider used when SwarmSync is disabled or unavailable.",
        vault_keys=("OPENROUTER_API_KEY",),
        env_keys=("OPENROUTER_API_KEY",),
        config_keys=("default_model",),
    ),
    IntegrationSpec(
        id="anthropic",
        name="Anthropic",
        category="llm_provider",
        description="Claude CLI/API credential metadata for coding agent backends.",
        vault_keys=("ANTHROPIC_API_KEY",),
        env_keys=("ANTHROPIC_API_KEY",),
        config_keys=("enabled_models", "subagent_coding_backend"),
    ),
    IntegrationSpec(
        id="openai",
        name="OpenAI",
        category="llm_provider",
        description="OpenAI API credential metadata for tools or CLI backends.",
        vault_keys=("OPENAI_API_KEY",),
        env_keys=("OPENAI_API_KEY",),
        config_keys=("codex_api_key_env",),
    ),
    IntegrationSpec(
        id="gemini",
        name="Gemini",
        category="llm_provider",
        description="Google Gemini credential metadata for coding agent backends.",
        vault_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        env_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        config_keys=("enabled_models", "gemini_api_key_env"),
    ),
    IntegrationSpec(
        id="telegram",
        name="Telegram",
        category="messaging",
        description="Telegram bot channel for phone-to-Cato messaging.",
        vault_keys=("TELEGRAM_BOT_TOKEN",),
        env_keys=("TELEGRAM_BOT_TOKEN",),
        config_keys=("telegram_enabled",),
        enable_config_key="telegram_enabled",
    ),
    IntegrationSpec(
        id="whatsapp",
        name="WhatsApp",
        category="messaging",
        description="WhatsApp Cloud API channel metadata.",
        vault_keys=("WHATSAPP_PHONE_ID", "WHATSAPP_TOKEN", "WHATSAPP_WEBHOOK_VERIFY"),
        env_keys=("WHATSAPP_PHONE_ID", "WHATSAPP_TOKEN", "WHATSAPP_WEBHOOK_VERIFY"),
        config_keys=("whatsapp_enabled",),
        enable_config_key="whatsapp_enabled",
    ),
    IntegrationSpec(
        id="github",
        name="GitHub",
        category="developer_tool",
        description="GitHub CLI/tool token metadata.",
        vault_keys=("github_token", "GITHUB_TOKEN", "GH_TOKEN"),
        env_keys=("GITHUB_TOKEN", "GH_TOKEN"),
    ),
    IntegrationSpec(
        id="conduit",
        name="Conduit",
        category="browser_search",
        description="Config-driven browser/search tooling.",
        vault_keys=(
            "brave_api_key",
            "exa_api_key",
            "tavily_api_key",
            "perplexity_api_key",
            "semantic_scholar_api_key",
        ),
        env_keys=(
            "BRAVE_API_KEY",
            "EXA_API_KEY",
            "TAVILY_API_KEY",
            "PERPLEXITY_API_KEY",
            "SEMANTIC_SCHOLAR_API_KEY",
        ),
        config_keys=("conduit_enabled", "searxng_url", "search_rerank_enabled"),
        enable_config_key="conduit_enabled",
        requires_secret=False,
    ),
    IntegrationSpec(
        id="mcp",
        name="MCP",
        category="local_runtime",
        description="Local MCP runtime/proxy configuration.",
        config_keys=("mcp_enabled", "mcp_host", "mcp_port", "mcp_mount_path"),
        enable_config_key="mcp_enabled",
        requires_secret=False,
    ),
)


def _load_vault_keys() -> tuple[set[str], str]:
    """Return available vault key names and a status string without exposing values."""
    try:
        from cato.vault import get_vault

        keys = get_vault().list_keys()
        return set(keys), "available"
    except Exception as exc:
        logger.info("Integration status could not read vault key names: %s", exc)
        return set(), "unavailable"


def _safe_config_value(value: Any) -> Any:
    """Return config metadata values, redacting fields that should never echo secrets."""
    if isinstance(value, str) and any(token in value.lower() for token in ("token", "secret", "password", "api_key")):
        return "[redacted]"
    if isinstance(value, (str, bool, int, float, list)) or value is None:
        return value
    return str(value)


def _config_subset(cfg: CatoConfig, keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: _safe_config_value(getattr(cfg, key))
        for key in keys
        if hasattr(cfg, key)
    }


def _integration_status(
    spec: IntegrationSpec,
    cfg: CatoConfig,
    vault_keys: set[str],
) -> dict[str, Any]:
    vault_present = sorted(key for key in spec.vault_keys if key in vault_keys)
    env_present = sorted(key for key in spec.env_keys if os.environ.get(key))
    configured_secret = bool(vault_present or env_present)

    enabled = True
    if spec.enable_config_key:
        enabled = bool(getattr(cfg, spec.enable_config_key, False))

    config_values = _config_subset(cfg, spec.config_keys)
    configured = configured_secret if spec.requires_secret else bool(config_values or configured_secret)

    if spec.id == "conduit":
        configured = bool(
            configured_secret
            or getattr(cfg, "searxng_url", "")
            or getattr(cfg, "conduit_enabled", False)
        )
    elif spec.id == "mcp":
        configured = bool(getattr(cfg, "mcp_enabled", False))

    connected = bool(enabled and configured)

    return {
        "id": spec.id,
        "name": spec.name,
        "category": spec.category,
        "description": spec.description,
        "enabled": enabled,
        "configured": configured,
        "connected": connected,
        "connection_source": "metadata_only",
        "status": "connected" if connected else ("configured" if configured else "not_configured"),
        "metadata": {
            "vault_keys_present": vault_present,
            "env_keys_present": env_present,
            "config": config_values,
            "required_vault_keys": list(spec.vault_keys),
            "required_env_keys": list(spec.env_keys),
            "safe_config_guidance": (
                "Store credentials in the encrypted vault or environment. "
                "This endpoint only reports key names/presence and redacted config metadata."
            ),
        },
    }


async def integration_status(request: web.Request) -> web.Response:
    """GET /api/integrations/status — return read-only integration metadata."""
    try:
        cfg = CatoConfig.load()
    except Exception as exc:
        logger.warning("Integration status using default config after load failure: %s", exc)
        cfg = CatoConfig()

    vault_keys, vault_status = _load_vault_keys()
    integrations = [
        _integration_status(spec, cfg, vault_keys)
        for spec in _all_integration_specs()
    ]

    return web.json_response({
        "schema_version": 1,
        "live_checks_performed": False,
        "secrets_returned": False,
        "vault_status": vault_status,
        "config_guidance": {
            "restart_required": False,
            "message": (
                "Configuration status is computed from current config, vault key names, "
                "and environment variables. No third-party services are contacted."
            ),
        },
        "integrations": integrations,
    })


def _runtime():
    """Create the integration runtime with vault access when available."""
    try:
        from cato.integrations import IntegrationRuntime
        from cato.vault import get_vault

        return IntegrationRuntime(vault=get_vault())
    except Exception as exc:
        logger.info("Integration runtime using env-only credentials: %s", exc)
        from cato.integrations import IntegrationRuntime

        return IntegrationRuntime()


async def integration_setup(request: web.Request) -> web.Response:
    """POST /api/integrations/{integration}/setup — return setup/auth guidance."""
    integration_id = request.match_info["integration"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    params = body.get("params", body if isinstance(body, dict) else {})
    if not isinstance(params, dict):
        return web.json_response({
            "ok": False,
            "error": "params must be an object",
            "secrets_returned": False,
        }, status=400)
    result = _runtime().setup(integration_id, params)
    return web.json_response(result, status=200 if result.get("ok") else 404)


async def integration_action(request: web.Request) -> web.Response:
    """POST /api/integrations/{integration}/actions/{action} — dry-run by default."""
    integration_id = request.match_info["integration"]
    action_name = request.match_info["action"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    params = body.get("params", body.get("payload", {}))
    if not isinstance(params, dict):
        return web.json_response({
            "ok": False,
            "error": "params/payload must be an object",
            "dry_run": True,
            "secrets_returned": False,
        }, status=400)

    dry_run = _as_bool(body.get("dry_run", True), default=True)
    approved = _as_bool(body.get("approved", False), default=False)
    timeout = float(body.get("timeout", 20.0))
    result = await _runtime().action(
        integration_id,
        action_name,
        params,
        dry_run=dry_run,
        approved=approved,
        timeout=timeout,
    )
    result["secrets_returned"] = False
    return web.json_response(result, status=200 if result.get("ok") else 400)


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def register_routes(app: web.Application) -> None:
    """Register integration catalog/status routes."""
    app.router.add_get("/api/integrations/status", integration_status)
    app.router.add_get("/api/integrations", integration_status)
    app.router.add_post("/api/integrations/{integration}/setup", integration_setup)
    app.router.add_post("/api/integrations/{integration}/actions/{action}", integration_action)
    logger.info("Integration routes registered")


def _all_integration_specs() -> tuple[IntegrationSpec, ...]:
    """Return route metadata, extended with builder integrations from the shared registry."""
    specs_by_id = {spec.id: spec for spec in _INTEGRATIONS}
    try:
        from cato.integrations import list_integrations
    except Exception as exc:
        logger.info("Builder integration registry unavailable for status route: %s", exc)
        return _INTEGRATIONS

    for definition in list_integrations():
        vault_keys = tuple(
            key for group in definition.credential_groups for key in group
        )
        specs_by_id.setdefault(
            definition.integration_id,
            IntegrationSpec(
                id=definition.integration_id,
                name=definition.display_name,
                category=definition.category,
                description=definition.notes or f"{definition.display_name} builder integration.",
                vault_keys=vault_keys,
                env_keys=tuple(key for key in vault_keys if key.isupper()),
            ),
        )
    return tuple(specs_by_id[key] for key in sorted(specs_by_id))
