"""Builder-facing integration tools for Cato."""

from __future__ import annotations

from typing import Any

from ..integrations.runtime import IntegrationRuntime, dumps_public


class IntegrationTool:
    """Expose integration metadata and safe action planning/execution."""

    def __init__(self, vault: Any = None) -> None:
        self._runtime = IntegrationRuntime(vault=vault)

    async def status(self, args: dict[str, Any]) -> str:
        integration_id = args.get("integration") or args.get("integration_id")
        return dumps_public(self._runtime.status(integration_id))

    async def setup(self, args: dict[str, Any]) -> str:
        integration_id = args.get("integration") or args.get("integration_id") or ""
        params = args.get("params") or {}
        if not isinstance(params, dict):
            return dumps_public({
                "ok": False,
                "error": "params must be an object",
                "secrets_returned": False,
            })
        return dumps_public(self._runtime.setup(integration_id, params))

    async def action(self, args: dict[str, Any]) -> str:
        integration_id = args.get("integration") or args.get("integration_id") or ""
        action_name = args.get("action") or args.get("action_name") or ""
        params = args.get("params") or {}
        if not isinstance(params, dict):
            return dumps_public({
                "ok": False,
                "error": "params must be an object",
                "dry_run": True,
            })

        dry_run = _as_bool(args.get("dry_run", True), default=True)
        approved = _as_bool(args.get("approved", False), default=False)
        timeout = float(args.get("timeout", 20.0))
        result = await self._runtime.action(
            integration_id=integration_id,
            action_name=action_name,
            params=params,
            dry_run=dry_run,
            approved=approved,
            timeout=timeout,
        )
        return dumps_public(result)


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
