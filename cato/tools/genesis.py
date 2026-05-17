"""Genesis Agents tool — calls SwarmSync-hosted AP2 agents.

Sends signed AP2 envelopes to https://swarmsync-agents.onrender.com/agents/{slug}/run.
Bound to Cato's vault Ed25519 keypair via cato.vault_crypto.

The tool exposes 20 registered Genesis agents (15 deployed, 5 pending). Each
call builds a fresh AP2 envelope: payload + nonce + RFC3339 timestamp, signed
with the vault's long-lived Ed25519 identity key, then POSTed to the agent's
``/agents/{slug}/run`` endpoint with the public key on a sidecar header.

Public symbols:
    GENESIS_AGENTS         -- 20-agent registry dict
    GENESIS_TOOL_SCHEMA    -- tool registry schema for task-03 wiring
    AP2_ENVELOPE_VERSION   -- wire protocol version (1)
    build_envelope         -- pure function, builds + signs envelope
    list_agents            -- returns the registry as a flat list
    GenesisTool            -- the tool class (instance method ``execute``)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import aiohttp

from cato import vault_crypto
from cato.config import CatoConfig

# ---------------------------------------------------------------------------
# 20-agent registry: 15 deployed, 5 pending.
# Keep this dict aligned with ~/.cato/skills/genesis-*/SKILL.md and
# SwarmSync's swarmsync-agents service.
# ---------------------------------------------------------------------------
GENESIS_AGENTS: dict[str, dict[str, Any]] = {
    "genesis-meta":            {"name": "Genesis Meta Agent",      "route": "/orchestrate",            "price_usd": 100, "status": "deployed"},
    "genesis-builder":         {"name": "Genesis Builder Agent",   "route": "/generate/module",        "price_usd": 200, "status": "deployed"},
    "genesis-research":        {"name": "Genesis Research Agent",  "route": "/research/comprehensive", "price_usd": 150, "status": "deployed"},
    "genesis-deploy":          {"name": "Genesis Deploy Agent",    "route": "/deploy/advanced",        "price_usd": 300, "status": "deployed"},
    "genesis-qa":              {"name": "Genesis QA Agent",        "route": "/test/analysis",          "price_usd": 150, "status": "deployed"},
    "genesis-finance":         {"name": "Genesis Finance Agent",   "route": "/finance/strategy",       "price_usd": 400, "status": "deployed"},
    "genesis-marketing":       {"name": "Genesis Marketing Agent", "route": "/marketing/strategy",     "price_usd": 300, "status": "deployed"},
    "genesis-content":         {"name": "Genesis Content Agent",   "route": "/content/whitepaper",     "price_usd": 180, "status": "deployed"},
    "genesis-security":        {"name": "Genesis Security Agent",  "route": "/security/pentest",       "price_usd": 600, "status": "deployed"},
    "genesis-seo":             {"name": "Genesis SEO Agent",       "route": "/seo/strategy",           "price_usd": 180, "status": "deployed"},
    "genesis-support":         {"name": "Genesis Support Agent",   "route": "/support/system",         "price_usd":  75, "status": "deployed"},
    "genesis-email":           {"name": "Genesis Email Agent",     "route": "/email/campaign",         "price_usd": 120, "status": "deployed"},
    "genesis-analyst":         {"name": "Genesis Analyst Agent",   "route": "/analyze/strategy",       "price_usd": 200, "status": "deployed"},
    "genesis-commerce":        {"name": "Genesis Commerce Agent",  "route": "/commerce/integration",   "price_usd": 250, "status": "deployed"},
    "genesis-billing":         {"name": "Genesis Billing Agent",   "route": "/billing/revops",         "price_usd": 100, "status": "deployed"},
    "genesis-legal":              {"name": "Genesis Legal Agent",        "route": None, "price_usd": None, "status": "pending"},
    "genesis-hr":                 {"name": "Genesis HR Agent",           "route": None, "price_usd": None, "status": "pending"},
    "genesis-data-pipeline":      {"name": "Genesis Data Pipeline Agent","route": None, "price_usd": None, "status": "pending"},
    "genesis-workflow-automator": {"name": "Genesis Workflow Automator", "route": None, "price_usd": None, "status": "pending"},
    "genesis-ai-vision":          {"name": "Genesis AI Vision API",      "route": None, "price_usd": None, "status": "pending"},
}

AP2_ENVELOPE_VERSION = 1

# Truncate upstream error bodies to keep tool output bounded.
_UPSTREAM_BODY_TRUNCATE = 500

_logger = logging.getLogger("cato.tools.genesis")


# ---------------------------------------------------------------------------
# Envelope construction (pure, testable, no I/O)
# ---------------------------------------------------------------------------

def _canonical_signed_bytes(payload: dict[str, Any], nonce: str, timestamp: str) -> bytes:
    """Return the canonical-JSON bytes that get signed.

    Must NEVER include the signature or pubkey; if the wire format ever
    grows new signed fields, they MUST be added here in sorted-key form.
    """
    return json.dumps(
        {"payload": payload, "nonce": nonce, "timestamp": timestamp},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _rfc3339_utc_now() -> str:
    """RFC3339 UTC timestamp with a 'Z' suffix and second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_envelope(vault, agent: str, task: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a signed AP2 envelope for the given agent + task.

    Pure with respect to the vault: only uses ``vault_crypto.sign`` and
    ``vault_crypto.get_or_create_keypair``. Generates a fresh nonce + timestamp
    on every call (envelopes are intentionally not idempotent — that's how the
    server detects replays).
    """
    payload = {
        "agent": agent,
        "task": task,
        "params": params or {},
    }
    nonce = uuid.uuid4().hex
    timestamp = _rfc3339_utc_now()

    signed_bytes = _canonical_signed_bytes(payload, nonce, timestamp)
    signature = vault_crypto.sign(vault, signed_bytes)
    _priv, pub_bytes = vault_crypto.get_or_create_keypair(vault)

    return {
        "version": AP2_ENVELOPE_VERSION,
        "payload": payload,
        "nonce": nonce,
        "timestamp": timestamp,
        "pubkey": base64.b64encode(pub_bytes).decode("ascii"),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------

class GenesisTool:
    """Tool wrapper that POSTs signed AP2 envelopes to SwarmSync.

    Vault and config are lazily resolved on first ``execute()`` if not
    supplied to the constructor. The aiohttp session is also lazy — created
    on first use and reused for all subsequent calls until ``close()``.
    """

    def __init__(self, vault: Any = None, config: Any = None) -> None:
        self._vault = vault
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._warmed_up = False
        self._log = _logger

    # ---- lazy dependency resolution -------------------------------------

    def _get_vault(self) -> Any:
        """Resolve vault via constructor injection first, falling back to
        the cato.vault.get_vault() module-level accessor (matches the
        convention used in cato/api/integration_routes.py line 148)."""
        if self._vault is None:
            from cato.vault import get_vault  # lazy import: avoids vault load at import time
            self._vault = get_vault()
        return self._vault

    def _get_config(self) -> CatoConfig:
        if self._config is None:
            self._config = CatoConfig.load()
        return self._config

    # ---- HTTP session ---------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Use ThreadedResolver (OS resolver via thread pool) instead of the
            # default async resolver. On Windows, aiohttp's default resolver has
            # been observed to raise ClientConnectorDNSError intermittently for
            # hosts that urllib (which uses the OS resolver) resolves cleanly.
            # ThreadedResolver gives us urllib-equivalent reliability without
            # pulling in aiodns as a dependency.
            resolver = aiohttp.ThreadedResolver()
            connector = aiohttp.TCPConnector(
                resolver=resolver,
                family=0,  # IPv4 + IPv6 — let the OS pick
                ssl=True,
                limit=10,
            )
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _warmup(self, endpoint: str) -> None:
        """One-shot GET /health to wake the Render free-tier dyno.

        Failures are logged but never raised — the real POST will retry.
        Sets ``self._warmed_up`` regardless of success to avoid retry loops.
        """
        if self._warmed_up:
            return
        self._warmed_up = True  # set up-front so a hang/error doesn't trigger repeated warmups
        url = f"{endpoint.rstrip('/')}/health"
        try:
            session = await self._ensure_session()
            timeout = aiohttp.ClientTimeout(total=60)
            async with session.get(url, timeout=timeout) as resp:
                # Drain the body so the connection can be returned to the pool.
                await resp.read()
                self._log.debug("Genesis warmup %s -> %s", url, resp.status)
        except Exception as exc:  # noqa: BLE001 — warmup must never raise
            self._log.warning("Genesis warmup failed for %s: %s", url, exc)

    async def close(self) -> None:
        """Close the aiohttp session. Idempotent."""
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception as exc:  # noqa: BLE001
                self._log.warning("Error closing Genesis session: %s", exc)
        self._session = None

    # ---- main entry point ----------------------------------------------

    async def execute(self, args: dict[str, Any]) -> str:
        """Dispatch a call to a Genesis agent.

        Args:
            args: {"agent": str, "task": str, "params"?: dict}

        Returns:
            JSON-encoded string. Shape depends on outcome — see module docstring
            and task spec for the eight branches.
        """
        agent = (args.get("agent") or "").strip()
        task = args.get("task") or ""
        params = args.get("params") or {}

        # --- branch 1: unknown agent
        if agent not in GENESIS_AGENTS:
            return json.dumps({
                "ok": False,
                "error": "unknown_agent",
                "agent": agent,
                "known": list(GENESIS_AGENTS.keys()),
            })

        meta = GENESIS_AGENTS[agent]
        config = self._get_config()

        # --- branch 2: globally disabled
        if not getattr(config, "genesis_enabled", True):
            return json.dumps({"ok": False, "error": "genesis_disabled"})

        # --- branch 3: not in allowlist (only enforced when list non-empty)
        allowlist = getattr(config, "genesis_agent_allowlist", []) or []
        if allowlist and agent not in allowlist:
            return json.dumps({
                "ok": False,
                "error": "not_in_allowlist",
                "agent": agent,
            })

        # --- branch 4: pending deployment
        if meta.get("status") == "pending":
            return json.dumps({
                "ok": False,
                "error": "pending_deployment",
                "agent": agent,
                "name": meta.get("name", agent),
                "message": (
                    "This Genesis agent is registered but not yet deployed on "
                    "SwarmSync. Try again later."
                ),
            })

        # --- deployed branch: sign envelope + POST
        endpoint = getattr(config, "genesis_endpoint", "https://swarmsync-agents.onrender.com")
        timeout_s = float(getattr(config, "genesis_timeout_s", 30.0))
        route = meta.get("route") or "/run"
        # The task spec pins the URL shape to /agents/{slug}/run. The per-agent
        # `route` is metadata for documentation/UI; the wire URL is always /run.
        url = f"{endpoint.rstrip('/')}/agents/{agent}/run"

        try:
            vault = self._get_vault()
            envelope = build_envelope(vault, agent, task, params)
        except Exception as exc:  # noqa: BLE001 — surface signing failures as tool errors
            return json.dumps({
                "ok": False,
                "error": "exception",
                "agent": agent,
                "type": type(exc).__name__,
                "message": str(exc),
            })

        # Cold start warmup before the very first real request (60s budget).
        is_cold = not self._warmed_up
        if is_cold:
            await self._warmup(endpoint)

        # Pull gateway API key from the vault. SwarmSync's agents-gateway
        # currently authenticates inbound requests by comparing the
        # X-Agent-Api-Key header against its GATEWAY_API_KEY env var
        # (apps/agents-gateway/main.py — verify_gateway_key()). The header
        # is only sent when the vault actually holds a non-empty value;
        # if the key is missing we omit the header so deployments that
        # have not yet configured a gateway secret continue to work.
        #
        # Operator setup: `cato vault set GATEWAY_API_KEY <value>`.
        #
        # Forward-looking: SwarmSync's signature-verification middleware
        # (see Protocols/VCAP-AP2-Binding-v1.0-draft.md and
        # apps/agents-gateway/trusted_ap2_clients.json) will validate
        # X-AP2-Pubkey against the trusted-client registry, at which point
        # the shared API key becomes optional. We keep both headers wired
        # so the transition is a server-side flip.
        api_key = None
        try:
            api_key = vault.get("GATEWAY_API_KEY")
        except Exception:
            api_key = None

        headers = {
            "Content-Type": "application/json",
            "X-AP2-Version": str(AP2_ENVELOPE_VERSION),
            "X-AP2-Pubkey": envelope["pubkey"],
        }
        if isinstance(api_key, str) and api_key:
            headers["X-Agent-Api-Key"] = api_key

        # Cold-start path budgets 60s total even though config asks for 30s;
        # subsequent calls use config.genesis_timeout_s.
        effective_timeout = 60.0 if is_cold else timeout_s
        client_timeout = aiohttp.ClientTimeout(total=effective_timeout)

        started = time.monotonic()
        try:
            session = await self._ensure_session()
            async with session.post(url, json=envelope, headers=headers, timeout=client_timeout) as resp:
                body = await resp.text()
                elapsed = round(time.monotonic() - started, 3)

                if resp.status == 200:
                    return json.dumps({
                        "ok": True,
                        "agent": agent,
                        "response": body,
                        "elapsed_s": elapsed,
                    })

                # --- branch 6: upstream non-200
                truncated = body if len(body) <= _UPSTREAM_BODY_TRUNCATE else body[:_UPSTREAM_BODY_TRUNCATE]
                return json.dumps({
                    "ok": False,
                    "error": "upstream_error",
                    "agent": agent,
                    "status": resp.status,
                    "body": truncated,
                })

        except asyncio.TimeoutError:
            return json.dumps({
                "ok": False,
                "error": "timeout",
                "agent": agent,
                "timeout_s": effective_timeout,
            })
        except Exception as exc:  # noqa: BLE001 — catch-all for connection / DNS / SSL / etc.
            return json.dumps({
                "ok": False,
                "error": "exception",
                "agent": agent,
                "type": type(exc).__name__,
                "message": str(exc),
            })


# ---------------------------------------------------------------------------
# Introspection helper
# ---------------------------------------------------------------------------

def list_agents() -> list[dict[str, Any]]:
    """Return the full 20-agent registry as a list (CLI / introspection)."""
    return [{"slug": slug, **meta} for slug, meta in GENESIS_AGENTS.items()]


# ---------------------------------------------------------------------------
# Tool schema (consumed by task-03 when wiring into the registry)
# ---------------------------------------------------------------------------

# OpenAI function-calling format — matches sibling entries in
# cato.agent_loop._BUILTIN_SCHEMAS so _sanitize_tool_defs (which reads
# ``d["function"]["name"]``) can normalize this schema uniformly with the
# rest of the tool registry. Anthropic-style ``{"name", "input_schema"}``
# at the top level breaks _sanitize_tool_defs with KeyError: 'function'.
GENESIS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "genesis",
        "description": (
            "Call a hosted Genesis Agent on SwarmSync. The agent slug must be one of the 20 "
            "registered Genesis agents (genesis-meta, genesis-builder, genesis-research, "
            "genesis-deploy, genesis-qa, genesis-finance, genesis-marketing, genesis-content, "
            "genesis-security, genesis-seo, genesis-support, genesis-email, genesis-analyst, "
            "genesis-commerce, genesis-billing, genesis-legal, genesis-hr, genesis-data-pipeline, "
            "genesis-workflow-automator, genesis-ai-vision). Returns the agent's response. "
            "Pending agents return a 'pending_deployment' error."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Agent slug, e.g. 'genesis-research'."},
                "task": {"type": "string", "description": "Plain-text task for the agent to perform."},
                "params": {"type": "object", "description": "Optional structured parameters.", "additionalProperties": True},
            },
            "required": ["agent", "task"],
            "additionalProperties": False,
        },
    },
}


__all__ = [
    "GENESIS_AGENTS",
    "GENESIS_TOOL_SCHEMA",
    "AP2_ENVELOPE_VERSION",
    "GenesisTool",
    "build_envelope",
    "list_agents",
]
