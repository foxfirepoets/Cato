"""
tests/test_genesis_tool.py — Tests for the Genesis Agents tool (task-02).

Covers:
- 20-agent registry (15 deployed, 5 pending)
- AP2 envelope construction + Ed25519 signature verification
- Canonical-JSON signed bytes (tamper-evident)
- GENESIS_TOOL_SCHEMA shape
- GenesisTool.execute() dispatch branches:
    unknown_agent, pending_deployment, genesis_disabled,
    not_in_allowlist, allowlisted-ok, upstream 200,
    upstream non-200, timeout, exception, captured-envelope
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from typing import Any

import pytest

from cato import vault_crypto
from cato.tools.genesis import (
    AP2_ENVELOPE_VERSION,
    GENESIS_AGENTS,
    GENESIS_TOOL_SCHEMA,
    GenesisTool,
    build_envelope,
    list_agents,
)


# ---------------------------------------------------------------------------
# In-memory test doubles
# ---------------------------------------------------------------------------


class MockVault:
    def __init__(self, initial=None):
        self._d = dict(initial or {})

    def get(self, k):
        return self._d.get(k)

    def set(self, k, v):
        assert isinstance(v, str)
        self._d[k] = v


class MockConfig:
    def __init__(self, **overrides):
        self.genesis_enabled = True
        self.genesis_endpoint = "http://test.local"
        self.genesis_agent_allowlist: list[str] = []
        self.genesis_timeout_s: float = 5.0
        for k, v in overrides.items():
            setattr(self, k, v)


class FakeResp:
    """Async context manager mimicking aiohttp's response object."""

    def __init__(self, status=200, body='{"response":"ok"}'):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def read(self):
        return self._body.encode("utf-8")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Drop-in replacement for aiohttp.ClientSession for tests."""

    def __init__(self, post_resp=None, post_exc=None, get_resp=None):
        self._post_resp = post_resp
        self._post_exc = post_exc
        self._get_resp = get_resp
        self.closed = False

    def post(self, *a, **kw):
        if self._post_exc is not None:
            raise self._post_exc
        return self._post_resp

    def get(self, *a, **kw):
        return self._get_resp or FakeResp(200, '"ok"')

    async def close(self):
        self.closed = True


class CapturingSession:
    """FakeSession variant that records the kwargs of every .post() call."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return FakeResp(200, '{"response":"captured"}')

    def get(self, *a, **kw):
        return FakeResp(200, '"ok"')

    async def close(self):
        self.closed = True


def _new_tool(vault=None, config=None, session=None, skip_warmup=True) -> GenesisTool:
    """Construct a GenesisTool with an in-memory vault + config, optionally
    injecting a fake session and skipping the warmup HTTP."""
    if vault is None:
        vault = MockVault()
    if config is None:
        config = MockConfig()
    tool = GenesisTool(vault=vault, config=config)
    if session is not None:
        tool._session = session  # noqa: SLF001 — test injection
    if skip_warmup:
        tool._warmed_up = True  # noqa: SLF001 — bypass the cold-start /health hop
    return tool


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_twenty_agents_total(self):
        assert len(GENESIS_AGENTS) == 20

    def test_fifteen_deployed_five_pending(self):
        deployed = [m for m in GENESIS_AGENTS.values() if m.get("status") == "deployed"]
        pending = [m for m in GENESIS_AGENTS.values() if m.get("status") == "pending"]
        assert len(deployed) == 15
        assert len(pending) == 5

    def test_known_deployed_slugs_present(self):
        for slug in [
            "genesis-meta",
            "genesis-builder",
            "genesis-research",
            "genesis-deploy",
            "genesis-qa",
            "genesis-finance",
            "genesis-marketing",
            "genesis-content",
            "genesis-security",
            "genesis-seo",
            "genesis-support",
            "genesis-email",
            "genesis-analyst",
            "genesis-commerce",
            "genesis-billing",
        ]:
            assert slug in GENESIS_AGENTS, f"missing deployed slug {slug}"
            assert GENESIS_AGENTS[slug]["status"] == "deployed"

    def test_pending_slugs_present(self):
        for slug in [
            "genesis-legal",
            "genesis-hr",
            "genesis-data-pipeline",
            "genesis-workflow-automator",
            "genesis-ai-vision",
        ]:
            assert slug in GENESIS_AGENTS, f"missing pending slug {slug}"
            assert GENESIS_AGENTS[slug]["status"] == "pending"

    def test_pending_agents_have_no_route_or_price(self):
        for slug, meta in GENESIS_AGENTS.items():
            if meta.get("status") == "pending":
                assert meta.get("route") is None, f"pending {slug} has route"
                assert meta.get("price_usd") is None, f"pending {slug} has price"

    def test_list_agents_shape(self):
        agents = list_agents()
        assert isinstance(agents, list)
        assert len(agents) == 20
        for entry in agents:
            assert "slug" in entry
            assert "name" in entry
            assert "status" in entry
            assert entry["status"] in ("deployed", "pending")


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------


class TestEnvelope:
    def test_envelope_has_required_fields(self):
        vault = MockVault()
        env = build_envelope(vault, "genesis-research", "task text", {"k": "v"})
        for field in ("version", "payload", "nonce", "timestamp", "pubkey", "signature"):
            assert field in env, f"missing field {field}"

    def test_envelope_version_is_1(self):
        vault = MockVault()
        env = build_envelope(vault, "genesis-meta", "x", {})
        assert env["version"] == 1
        assert AP2_ENVELOPE_VERSION == 1

    def test_nonce_is_unique_across_calls(self):
        vault = MockVault()
        nonces = {
            build_envelope(vault, "genesis-meta", "t", {})["nonce"]
            for _ in range(100)
        }
        assert len(nonces) == 100

    def test_timestamp_is_iso8601_z(self):
        vault = MockVault()
        env = build_envelope(vault, "genesis-meta", "t", {})
        ts = env["timestamp"]
        assert isinstance(ts, str)
        assert ts.endswith("Z")
        # Parseable as RFC3339 UTC (strip Z -> use fromisoformat).
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None

    def test_signature_verifies_under_pubkey(self):
        vault = MockVault()
        env = build_envelope(vault, "genesis-meta", "hello", {"a": 1})

        signed_bytes = json.dumps(
            {"payload": env["payload"], "nonce": env["nonce"], "timestamp": env["timestamp"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        pub_bytes = base64.b64decode(env["pubkey"])
        sig_bytes = base64.b64decode(env["signature"])
        assert vault_crypto.verify(pub_bytes, signed_bytes, sig_bytes) is True

    def test_signed_bytes_use_canonical_json(self):
        """Tampering with the payload should invalidate the signature."""
        vault = MockVault()
        env = build_envelope(vault, "genesis-meta", "hello", {"a": 1})

        tampered_payload = dict(env["payload"])
        tampered_payload["task"] = "MALICIOUS"
        tampered_bytes = json.dumps(
            {"payload": tampered_payload, "nonce": env["nonce"], "timestamp": env["timestamp"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        pub_bytes = base64.b64decode(env["pubkey"])
        sig_bytes = base64.b64decode(env["signature"])
        assert vault_crypto.verify(pub_bytes, tampered_bytes, sig_bytes) is False


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_tool_schema_shape(self):
        # OpenAI function-calling format — matches the sibling entries in
        # cato.agent_loop._BUILTIN_SCHEMAS that _sanitize_tool_defs expects.
        assert GENESIS_TOOL_SCHEMA["type"] == "function"
        fn = GENESIS_TOOL_SCHEMA["function"]
        assert fn["name"] == "genesis"
        assert "description" in fn
        schema = fn["parameters"]
        assert schema["type"] == "object"
        assert "agent" in schema["properties"]
        assert "task" in schema["properties"]
        # 'agent' + 'task' are required.
        assert "agent" in schema["required"]
        assert "task" in schema["required"]


# ---------------------------------------------------------------------------
# execute() — non-HTTP branches
# ---------------------------------------------------------------------------


class TestExecuteBranches:
    async def test_unknown_agent_returns_unknown_agent_error(self):
        tool = _new_tool()
        out = json.loads(await tool.execute({"agent": "genesis-does-not-exist", "task": "t"}))
        assert out["ok"] is False
        assert out["error"] == "unknown_agent"
        assert out["agent"] == "genesis-does-not-exist"
        assert "known" in out and isinstance(out["known"], list)

    async def test_pending_agent_returns_pending_deployment(self):
        tool = _new_tool()
        out = json.loads(await tool.execute({"agent": "genesis-legal", "task": "review NDA"}))
        assert out["ok"] is False
        assert out["error"] == "pending_deployment"
        assert out["agent"] == "genesis-legal"
        assert "name" in out

    async def test_disabled_returns_genesis_disabled(self):
        tool = _new_tool(config=MockConfig(genesis_enabled=False))
        out = json.loads(await tool.execute({"agent": "genesis-research", "task": "t"}))
        assert out["ok"] is False
        assert out["error"] == "genesis_disabled"

    async def test_allowlist_blocks_non_listed_agent(self):
        tool = _new_tool(config=MockConfig(genesis_agent_allowlist=["genesis-meta"]))
        out = json.loads(await tool.execute({"agent": "genesis-research", "task": "t"}))
        assert out["ok"] is False
        assert out["error"] == "not_in_allowlist"
        assert out["agent"] == "genesis-research"

    async def test_allowlist_permits_listed_agent(self):
        cfg = MockConfig(genesis_agent_allowlist=["genesis-research"])
        session = FakeSession(post_resp=FakeResp(200, '{"response":"allowlist-ok"}'))
        tool = _new_tool(config=cfg, session=session)
        out = json.loads(await tool.execute({"agent": "genesis-research", "task": "t"}))
        assert out["ok"] is True
        assert out["agent"] == "genesis-research"


# ---------------------------------------------------------------------------
# execute() — HTTP branches (mocked via FakeSession)
# ---------------------------------------------------------------------------


class TestExecuteHTTP:
    async def test_deployed_agent_success(self):
        session = FakeSession(post_resp=FakeResp(200, '{"response":"hello world"}'))
        tool = _new_tool(session=session)
        out = json.loads(await tool.execute({"agent": "genesis-research", "task": "explain X"}))
        assert out["ok"] is True
        assert out["agent"] == "genesis-research"
        assert "response" in out
        assert "hello world" in out["response"]

    async def test_upstream_500_returns_upstream_error(self):
        session = FakeSession(post_resp=FakeResp(500, "internal server error"))
        tool = _new_tool(session=session)
        out = json.loads(await tool.execute({"agent": "genesis-research", "task": "t"}))
        assert out["ok"] is False
        assert out["error"] == "upstream_error"
        assert out["status"] == 500
        assert "internal server error" in out["body"]

    async def test_timeout_returns_timeout_error(self):
        session = FakeSession(post_exc=asyncio.TimeoutError())
        tool = _new_tool(session=session)
        out = json.loads(await tool.execute({"agent": "genesis-research", "task": "t"}))
        assert out["ok"] is False
        assert out["error"] == "timeout"
        assert out["agent"] == "genesis-research"
        assert "timeout_s" in out

    async def test_other_exception_returns_exception(self):
        session = FakeSession(post_exc=ConnectionError("dns failed"))
        tool = _new_tool(session=session)
        out = json.loads(await tool.execute({"agent": "genesis-research", "task": "t"}))
        assert out["ok"] is False
        assert out["error"] == "exception"
        assert out["agent"] == "genesis-research"
        assert out["type"] == "ConnectionError"
        assert "dns failed" in out["message"]

    async def test_does_not_call_http_for_pending_agent(self):
        """Even if the session would happily 200, pending agents short-circuit."""
        session = CapturingSession()
        tool = _new_tool(session=session)
        out = json.loads(await tool.execute({"agent": "genesis-legal", "task": "t"}))
        assert out["error"] == "pending_deployment"
        assert session.calls == []

    async def test_envelope_posted_includes_signature(self):
        session = CapturingSession()
        tool = _new_tool(session=session)
        out = json.loads(await tool.execute({"agent": "genesis-research", "task": "t", "params": {"x": 1}}))
        assert out["ok"] is True
        assert len(session.calls) == 1
        url, kw = session.calls[0]
        assert "/agents/genesis-research/run" in url
        assert "json" in kw
        env = kw["json"]
        for field in ("version", "payload", "nonce", "timestamp", "pubkey", "signature"):
            assert field in env, f"posted envelope missing {field}"
        # Sanity: payload echoes our args.
        assert env["payload"]["agent"] == "genesis-research"
        assert env["payload"]["task"] == "t"
        assert env["payload"]["params"] == {"x": 1}
        # Headers carry the AP2 protocol version + pubkey sidecar.
        headers = kw.get("headers") or {}
        assert headers.get("X-AP2-Version") == str(AP2_ENVELOPE_VERSION)
        assert headers.get("X-AP2-Pubkey") == env["pubkey"]

    async def test_includes_api_key_header_when_vault_has_key(self):
        v = MockVault(initial={"GATEWAY_API_KEY": "test-key-abc"})
        c = MockConfig()
        tool = GenesisTool(vault=v, config=c)
        fake = CapturingSession()
        tool._session = fake; tool._warmed_up = True
        await tool.execute({"agent": "genesis-research", "task": "x"})
        assert fake.calls, "post not called"
        headers = fake.calls[0][1].get("headers", {})
        assert headers.get("X-Agent-Api-Key") == "test-key-abc", headers

    async def test_omits_api_key_header_when_vault_empty(self):
        v = MockVault()
        c = MockConfig()
        tool = GenesisTool(vault=v, config=c)
        fake = CapturingSession()
        tool._session = fake; tool._warmed_up = True
        await tool.execute({"agent": "genesis-research", "task": "x"})
        headers = fake.calls[0][1].get("headers", {})
        assert "X-Agent-Api-Key" not in headers, headers
