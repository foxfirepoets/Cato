"""Runtime planner/executor for integration actions."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote, urlencode

from .credentials import CredentialLookup, resolve_credential_groups
from .http_client import request_json
from .registry import IntegrationAction, IntegrationDefinition, get_integration, list_integrations


class IntegrationRuntime:
    """Integration metadata, credential status, dry-run planning, and execution."""

    def __init__(self, vault: Any = None) -> None:
        self._vault = vault

    def status(self, integration_id: str | None = None) -> dict[str, Any]:
        """Return metadata and masked credential status."""
        if integration_id:
            definition = get_integration(integration_id)
            if definition is None:
                return {
                    "ok": False,
                    "error": f"Unknown integration: {integration_id}",
                    "supported_integrations": [item.integration_id for item in list_integrations()],
                }
            return {"ok": True, "integration": self._status_one(definition)}

        return {
            "ok": True,
            "integrations": [self._status_one(defn) for defn in list_integrations()],
        }

    def setup(
        self,
        integration_id: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return setup/auth guidance and optional OAuth authorization URL."""
        params = params or {}
        definition = get_integration(integration_id)
        if definition is None:
            return {
                "ok": False,
                "error": f"Unknown integration: {integration_id}",
                "supported_integrations": [item.integration_id for item in list_integrations()],
            }

        data = {
            "ok": True,
            "integration": definition.public_dict(),
            "credential_status": [
                item.public_dict() for item in self._credentials(definition)
            ],
            "secrets_returned": False,
            "live_checks_performed": False,
        }
        auth_url = self._oauth_authorization_url(definition, params)
        if auth_url:
            data["oauth_authorization_url"] = auth_url
            data["oauth_note"] = (
                "Open this URL yourself, complete consent, then store the resulting "
                "access/refresh token in Cato's vault. Callback token exchange is "
                "intentionally not automated in this first setup layer."
            )
        return data

    async def action(
        self,
        integration_id: str,
        action_name: str,
        params: dict[str, Any] | None = None,
        *,
        dry_run: bool = True,
        approved: bool = False,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Plan or execute an integration action.

        Write-like actions are dry-run by default and require ``approved=True``
        before a live call can be made.
        """
        params = params or {}
        definition = get_integration(integration_id)
        if definition is None:
            return {
                "ok": False,
                "error": f"Unknown integration: {integration_id}",
                "dry_run": dry_run,
            }

        action = definition.actions.get(action_name)
        if action is None:
            return {
                "ok": False,
                "error": f"Unknown action '{action_name}' for {definition.integration_id}",
                "dry_run": dry_run,
                "available_actions": sorted(definition.actions),
            }

        missing_params = [key for key in action.required_params if key not in params]
        plan = self._build_plan(definition, action, params)
        approval_required = bool(action.write)

        if missing_params:
            return {
                "ok": False,
                "error": "Missing required params.",
                "missing_params": missing_params,
                "dry_run": dry_run,
                "approval_required": approval_required,
                "planned_request": plan,
            }

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "approval_required": approval_required,
                "would_execute": not approval_required or approved,
                "planned_request": plan,
            }

        if approval_required and not approved:
            return {
                "ok": False,
                "error": "This write-like action requires approved=true when dry_run=false.",
                "dry_run": False,
                "approval_required": True,
                "planned_request": plan,
            }

        credentials = self._credentials(definition)
        primary = self._primary_credential(credentials)
        if primary is None:
            return {
                "ok": False,
                "error": "No usable credential found for this integration.",
                "dry_run": False,
                "approval_required": approval_required,
                "credential_status": [item.public_dict() for item in credentials],
                "planned_request": plan,
            }

        headers = self._headers(definition, action, primary)
        url = self._build_url(definition, action, params, token=primary.value)
        body = {
            key: value
            for key, value in params.items()
            if key not in self._path_keys(action) and key not in action.query_params
        }
        response = request_json(
            method=action.method,
            url=url,
            headers=headers,
            body=body if action.method.upper() != "GET" else None,
            body_format=action.body_format,
            timeout=timeout,
        )
        return {
            "ok": 200 <= response.status < 300,
            "dry_run": False,
            "approval_required": approval_required,
            "response": response.as_dict(),
        }

    def _status_one(self, definition: IntegrationDefinition) -> dict[str, Any]:
        credentials = self._credentials(definition)
        data = definition.public_dict()
        data["credential_status"] = [item.public_dict() for item in credentials]
        data["credentials_ready"] = any(item.found for item in credentials)
        return data

    def _credentials(self, definition: IntegrationDefinition) -> list[CredentialLookup]:
        return resolve_credential_groups(self._vault, definition.credential_groups)

    def _primary_credential(self, credentials: list[CredentialLookup]) -> CredentialLookup | None:
        for item in credentials:
            if item.found and item.value:
                return item
        return None

    def _build_plan(
        self,
        definition: IntegrationDefinition,
        action: IntegrationAction,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        path = action.path
        for key in self._path_keys(action):
            if key in params:
                path = path.replace("{" + key + "}", quote(str(params[key]), safe=""))
        if action.auth == "telegram_bot":
            path = path.replace("{token}", "***")
        url = self._base_url(definition, action).rstrip("/") + path
        query_keys = sorted(key for key in action.query_params if key in params)
        if query_keys:
            url += "?" + urlencode({key: "***" for key in query_keys}, doseq=True)
        body_keys = sorted(
            key for key in params
            if key not in self._path_keys(action) and key not in action.query_params
        )
        return {
            "integration": definition.integration_id,
            "action": action.name,
            "method": action.method,
            "url": url,
            "query_keys": query_keys,
            "body_keys": body_keys,
            "body_format": action.body_format if action.method.upper() != "GET" else "",
        }

    def _build_url(
        self,
        definition: IntegrationDefinition,
        action: IntegrationAction,
        params: dict[str, Any],
        *,
        token: str = "",
    ) -> str:
        path = action.path
        for key in self._path_keys(action):
            if key in params:
                path = path.replace("{" + key + "}", quote(str(params[key]), safe=""))
        if action.auth == "telegram_bot":
            path = path.replace("{token}", token)
        url = self._base_url(definition, action).rstrip("/") + path
        query = {
            key: params[key]
            for key in action.query_params
            if key in params and params[key] is not None
        }
        if query:
            url += "?" + urlencode(query, doseq=True)
        return url

    def _headers(
        self,
        definition: IntegrationDefinition,
        action: IntegrationAction,
        credential: CredentialLookup,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Cato-IntegrationTool/1.0",
        }
        if definition.integration_id == "notion":
            headers["Notion-Version"] = "2022-06-28"

        if definition.integration_id == "github":
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if action.auth == "bot":
            headers["Authorization"] = f"Bot {credential.value}"
        elif definition.integration_id == "twilio":
            headers["Authorization"] = "Basic " + base64.b64encode(
                credential.value.encode("utf-8")
            ).decode("ascii")
        elif action.auth != "telegram_bot":
            headers["Authorization"] = f"Bearer {credential.value}"
        return headers

    def _base_url(
        self,
        definition: IntegrationDefinition,
        action: IntegrationAction,
    ) -> str:
        return action.base_url or definition.base_url

    def _oauth_authorization_url(
        self,
        definition: IntegrationDefinition,
        params: dict[str, Any],
    ) -> str:
        if not definition.oauth_authorize_url:
            return ""
        client_id = str(params.get("client_id") or "").strip()
        redirect_uri = str(params.get("redirect_uri") or "").strip()
        if not client_id or not redirect_uri:
            return ""
        scopes = params.get("scopes") or definition.oauth_scopes
        if isinstance(scopes, str):
            scopes_value = scopes
        else:
            scopes_value = " ".join(str(scope) for scope in scopes)
        query = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": params.get("response_type", "code"),
            "scope": scopes_value,
            "access_type": params.get("access_type", "offline"),
            "prompt": params.get("prompt", "consent"),
        }
        state = params.get("state")
        if state:
            query["state"] = str(state)
        return definition.oauth_authorize_url + "?" + urlencode(query)

    def _path_keys(self, action: IntegrationAction) -> set[str]:
        keys: set[str] = set()
        for chunk in action.path.split("{")[1:]:
            if "}" in chunk:
                keys.add(chunk.split("}", 1)[0])
        return keys


def dumps_public(data: dict[str, Any]) -> str:
    """Stable JSON serialization for tool responses."""
    return json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True)
