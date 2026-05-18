"""
cato/router.py — SwarmSync-aware model router for CATO.

Routes tasks to the optimal LLM based on complexity scoring.
When SwarmSync API key is present, delegates to the SwarmSync API.
Supports Anthropic, OpenAI-compatible, and Google streaming APIs.

Includes error classification and fallback model rotation inspired by
Hermes agent's error_classifier.py for resilient LLM call handling.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Optional, Tuple

import aiohttp

from cato.routing_log import get_persistent_routing_history, record_routing_event


# ---------------------------------------------------------------------------
# Error classification (inspired by Hermes agent/error_classifier.py)
# ---------------------------------------------------------------------------

class FailoverReason(Enum):
    AUTH = "auth"
    BILLING = "billing"
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONTEXT_OVERFLOW = "context_overflow"
    MODEL_NOT_FOUND = "model_not_found"
    FORMAT_ERROR = "format_error"
    UNKNOWN = "unknown"


@dataclass
class ClassifiedError:
    reason: FailoverReason
    status_code: int
    message: str
    retryable: bool = True
    should_fallback: bool = False
    should_compress: bool = False


def classify_api_error(exc: Exception) -> ClassifiedError:
    """Classify an API error into a recovery-actionable category."""
    status = 0
    msg = str(exc).lower()

    # Extract status code from aiohttp or generic exceptions
    if hasattr(exc, "status"):
        status = getattr(exc, "status", 0)
    elif hasattr(exc, "response"):
        resp = getattr(exc, "response", None)
        if resp and hasattr(resp, "status"):
            status = resp.status

    # Status-code based classification
    if status == 401 or status == 403:
        return ClassifiedError(FailoverReason.AUTH, status, str(exc),
                               retryable=False, should_fallback=True)
    if status == 402:
        if "rate" in msg or "limit" in msg or "try again" in msg:
            return ClassifiedError(FailoverReason.RATE_LIMIT, status, str(exc),
                                   retryable=True, should_fallback=True)
        return ClassifiedError(FailoverReason.BILLING, status, str(exc),
                               retryable=False, should_fallback=True)
    if status == 404:
        return ClassifiedError(FailoverReason.MODEL_NOT_FOUND, status, str(exc),
                               retryable=False, should_fallback=True)
    if status == 413 or "too large" in msg or "context" in msg and "length" in msg:
        return ClassifiedError(FailoverReason.CONTEXT_OVERFLOW, status, str(exc),
                               retryable=False, should_compress=True)
    if status == 429:
        return ClassifiedError(FailoverReason.RATE_LIMIT, status, str(exc),
                               retryable=True, should_fallback=True)
    if status == 400:
        if "context" in msg or "token" in msg and "max" in msg:
            return ClassifiedError(FailoverReason.CONTEXT_OVERFLOW, status, str(exc),
                                   retryable=False, should_compress=True)
        return ClassifiedError(FailoverReason.FORMAT_ERROR, status, str(exc),
                               retryable=False)
    if 500 <= status < 600:
        return ClassifiedError(FailoverReason.SERVER_ERROR, status, str(exc),
                               retryable=True, should_fallback=True)

    # Message-pattern based classification
    if "timeout" in msg or "timed out" in msg:
        return ClassifiedError(FailoverReason.TIMEOUT, status, str(exc),
                               retryable=True, should_fallback=True)
    if "rate" in msg and "limit" in msg:
        return ClassifiedError(FailoverReason.RATE_LIMIT, status, str(exc),
                               retryable=True, should_fallback=True)
    if "overloaded" in msg or "capacity" in msg:
        return ClassifiedError(FailoverReason.OVERLOADED, status, str(exc),
                               retryable=True, should_fallback=True)
    if "unauthorized" in msg or "authentication" in msg:
        return ClassifiedError(FailoverReason.AUTH, status, str(exc),
                               retryable=False, should_fallback=True)

    return ClassifiedError(FailoverReason.UNKNOWN, status, str(exc), retryable=True)


# Fallback chain: if primary model fails, try these in order
_FALLBACK_CHAIN: list[str] = [
    "claude-sonnet-4-6",
    "gpt-4o",
    "gemini-2.0-flash",
    "deepseek-chat",
    "llama-3.3-70b-versatile",
]

logger = logging.getLogger(__name__)

_LOG_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(bot)\d+:[A-Za-z0-9_-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b([A-Z][A-Z0-9_]{2,}_(?:TOKEN|KEY|SECRET|PASSWORD|PASS)\s*=\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})\b"), "[REDACTED-KEY]"),
)


def _scrub_log_text(text: str) -> str:
    if not text:
        return text
    for pattern, replacement in _LOG_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text

RouterStreamChunk = str | dict[str, Any]

# ---------------------------------------------------------------------------
# Model translation: OpenRouter slugs → native IDs
# ---------------------------------------------------------------------------

MODEL_TRANSLATIONS: dict[str, str] = {
    "anthropic/claude-opus-4-6":     "claude-opus-4-6",
    "anthropic/claude-sonnet-4-6":   "claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5":    "claude-haiku-4-5-20251001",
    "openai/gpt-4o":                 "gpt-4o",
    "openai/gpt-4o-mini":            "gpt-4o-mini",
    "openai/o3-mini":                "o3-mini",
    "google/gemini-2.0-pro":         "gemini-2.0-pro-exp",
    "google/gemini-2.0-flash":       "gemini-2.0-flash",
    "google/gemini-2.0-flash-lite":  "gemini-2.0-flash-lite",
    "deepseek/deepseek-v3":          "deepseek-chat",
    "deepseek/deepseek-r1":          "deepseek-reasoner",
    "groq/llama-3.3-70b":            "llama-3.3-70b-versatile",
    "mistral/mistral-small":         "mistral-small-latest",
    "minimax/minimax-2.5":           "abab7-chat-preview",
    "minimax/minimax-m2.5":          "abab7-chat-preview",
    "Minimax:MiniMax M2.5":          "openrouter/minimax/minimax-m2.5",
    "openrouter/minimax/minimax-2.5":  "abab7-chat-preview",
    "openrouter/minimax/minimax-m2.5": "abab7-chat-preview",
    "moonshot/kimi-k2.5":            "moonshot-v1-8k",
}

_ECONOMY = ["claude-haiku-4-5-20251001", "gemini-2.0-flash-lite", "llama-3.3-70b-versatile"]
_MID     = ["claude-sonnet-4-6", "gemini-2.0-flash", "gpt-4o-mini", "deepseek-chat"]
_PREMIUM = ["claude-opus-4-6", "gemini-2.0-pro-exp", "gpt-4o", "deepseek-reasoner"]

# (prefix, base_url, auth_scheme)
_PROVIDERS: list[tuple[str, str, str]] = [
    ("claude-",     "https://api.anthropic.com/v1/messages",                                    "x-api-key"),
    ("gpt-",        "https://api.openai.com/v1/chat/completions",                               "bearer"),
    ("o3-",         "https://api.openai.com/v1/chat/completions",                               "bearer"),
    ("gemini-",     "https://generativelanguage.googleapis.com/v1beta/models",                  "google"),
    ("deepseek-",   "https://api.deepseek.com/v1/chat/completions",                             "bearer"),
    ("llama-",      "https://api.groq.com/openai/v1/chat/completions",                          "bearer"),
    ("mistral-",    "https://api.mistral.ai/v1/chat/completions",                               "bearer"),
    ("abab",        "https://api.minimax.chat/v1/text/chatcompletion_pro",                      "bearer"),
    ("moonshot-",   "https://api.moonshot.cn/v1/chat/completions",                              "bearer"),
    ("openrouter/", "https://openrouter.ai/api/v1/chat/completions",                            "openrouter"),
]

_KNOWN_MODEL_PROVIDERS = frozenset([
    "openrouter", "anthropic", "openai", "google", "deepseek",
    "groq", "mistral", "minimax", "moonshot", "meta-llama",
])


def _is_model_slug_only(text: str) -> bool:
    """True if text looks like a model id (e.g. openrouter/minimax/minimax-m2.5) and nothing else.
    Some providers echo the model in stream content; we skip it so the UI doesn't show only the model name.
    Requires a known provider prefix to avoid suppressing short responses like "A/B" or "I/O".
    """
    t = (text or "").strip()
    if not t or len(t) > 120:
        return False
    if t.startswith("openrouter/"):
        return True
    if "/" not in t:
        return False
    provider = t.split("/")[0].lower()
    return provider in _KNOWN_MODEL_PROVIDERS and re.match(r"^[\w\-./]+$", t) is not None


# Signal regexes for complexity scoring
_RE_REASON   = re.compile(r"\b(why|analyze|analyse|compare|explain|evaluate|assess)\b", re.I)
_RE_MATH     = re.compile(r"\b(calculate|compute|proof|prove|solve|integral|derivative)\b", re.I)
_RE_MULTI    = re.compile(r"\b(then|after that|first[,\s]|second[,\s]|finally|step \d)\b", re.I)
_RE_CREATIVE = re.compile(r"\b(write|generate|create|compose|draft)\b", re.I)
_RE_CODE     = re.compile(r"(```|def |class |import |#include|function\s+\w+)", re.I)
_RE_NONENGL  = re.compile(r"[^\x00-\x7F]")


# Module-level routing history (ring buffer, max 200 entries)
_routing_history: list[dict] = []
_ROUTING_HISTORY_MAX = 200


def get_routing_history() -> list[dict]:
    """Return the routing decision history buffer."""
    persistent = get_persistent_routing_history(limit=_ROUTING_HISTORY_MAX)
    if persistent:
        return persistent
    return list(_routing_history)


def _coerce_model_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, dict):
        return [str(key) for key in value.keys()]
    return []


def _extract_cost(data: dict[str, Any], *names: str) -> Any:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    swarmsync = data.get("swarmsync") if isinstance(data.get("swarmsync"), dict) else {}
    for name in names:
        for source in (swarmsync, usage, data):
            if source.get(name) is not None:
                return source[name]
    return None


def _record_routing_decision(record: dict[str, Any]) -> None:
    """Persist one SwarmSync routing decision and keep a short in-memory mirror."""
    _routing_history.append(record)
    if len(_routing_history) > _ROUTING_HISTORY_MAX:
        del _routing_history[:-_ROUTING_HISTORY_MAX]
    record_routing_event({
        "ts": time.time(),
        "provider": record.get("provider", "swarmsync"),
        "status": "ok" if record.get("success") else "fallback",
        "routed_model": record.get("routed_model") or record.get("chosen_model") or "",
        "raw_model": record.get("raw_model", ""),
        "complexity": record.get("complexity_score", 0.0),
        "has_tools": record.get("has_tools", False),
        "msg_count": record.get("history_length", 0),
        "http_status": record.get("http_status"),
        "content_chars": record.get("content_chars", 0),
        "tool_call_count": record.get("tool_call_count", 0),
        "error": record.get("error", ""),
        "metadata": record,
    })


class ModelRouter:
    """Routes tasks to optimal model via local scoring or SwarmSync API."""

    def __init__(
        self,
        vault: Any,
        preferred_model: str = "claude-sonnet-4-6",
        blocked_models: Optional[list[str]] = None,
        swarmsync_api_url: str = "https://api.swarmsync.ai/v1/chat/completions",
        max_output_tokens: int = 16384,
    ) -> None:
        self._vault = vault
        # Keep openrouter/ prefixed models untranslated so they route through
        # the OpenRouter provider entry.  All other slugs are translated to their
        # native IDs (e.g. "anthropic/claude-sonnet-4-6" → "claude-sonnet-4-6").
        if preferred_model.startswith("openrouter/"):
            self._preferred = preferred_model
        else:
            self._preferred = MODEL_TRANSLATIONS.get(preferred_model, preferred_model)
        self._blocked: set[str] = set(blocked_models or [])
        self._swarmsync_url = swarmsync_api_url
        self._max_output_tokens = max_output_tokens

        # Circuit breaker state — separate counters for SwarmSync vs direct LLM paths
        self._ss_cb_failures: int = 0       # SwarmSync API path
        self._ss_cb_open_until: float = 0.0
        self._direct_cb_failures: int = 0   # direct provider path
        self._direct_cb_open_until: float = 0.0
        self._CB_THRESHOLD = 3
        self._CB_COOLDOWN = 60.0

        # Shared HTTP session — lazily created and reused across LLM calls
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Return a cached ClientSession, creating one if needed."""
        if self._session is None or self._session.closed:
            import socket
            resolver = aiohttp.ThreadedResolver()
            connector = aiohttp.TCPConnector(family=socket.AF_INET, resolver=resolver)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=120),
            )
        return self._session

    async def close(self) -> None:
        """Close the cached HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def score_task(self, message: str, context_tokens: int, history_len: int) -> float:
        """Return 0.0-1.0 complexity score from message signals."""
        s = 0.0
        if len(message) > 500:       s += 0.10
        if _RE_CODE.search(message):     s += 0.15
        if _RE_REASON.search(message):   s += 0.10
        if _RE_MATH.search(message):     s += 0.15
        if context_tokens > 4000:    s += 0.10
        if _RE_MULTI.search(message):    s += 0.10
        if _RE_CREATIVE.search(message): s += 0.05
        if _RE_NONENGL.search(message):  s += 0.10
        if history_len > 10:         s += 0.05
        return min(1.0, round(s, 4))

    def select_model(self, score: float, task_type: Optional[str] = None) -> str:
        """Return native model ID for score band, respecting blocked/preferred config."""
        # Always honour preferred_model if set and not blocked — even if it's an
        # openrouter/ or other external model not in the local pool lists.
        if self._preferred and self._is_available(self._preferred):
            return self._preferred
        pool = _ECONOMY if score < 0.35 else (_MID if score < 0.70 else _PREMIUM)
        for m in pool:
            if self._is_available(m):
                return m
        if self._preferred and self._preferred not in self._blocked:
            return self._preferred
        for m in _ECONOMY + _MID + _PREMIUM:
            if self._is_available(m):
                return m
        return _ECONOMY[0]  # last-resort: cheapest available model

    async def _swarmsync_route(
        self, messages: list[dict], api_key: str, score: float
    ) -> str:
        """
        Query SwarmSync API and return the routed model name.

        SwarmSync always executes the completion regardless of ``routing_only``
        flag.  Use :meth:`_swarmsync_complete` when you want the completion
        text directly (avoids a second API call).  This method discards the
        completion body and only returns the model that was chosen.

        Falls back to local selection on any error.
        """
        result = await self._swarmsync_complete(messages, api_key, score)
        return result[0]  # (model, text) — caller only wants model here

    async def _swarmsync_complete(
        self, messages: list[dict], api_key: str, score: float
    ) -> tuple[str, str]:
        """
        Call SwarmSync and return ``(routed_model, completion_text)``.

        SwarmSync always runs a full completion regardless of ``routing_only``.
        The system prompt and skills travel in ``messages``, so the model
        receives full context.  We use the response text directly to avoid
        paying twice for the same call.

        Returns ``(select_model(score), "")`` on any error so callers can fall
        back gracefully.
        """
        model, message = await self._swarmsync_complete_message(messages, api_key, score)
        return model, message.get("content", "") or ""

    async def _swarmsync_complete_message(
        self, messages: list[dict], api_key: str, score: float,
        tools: Optional[list[dict]] = None,
    ) -> tuple[str, dict[str, Any]]:
        """
        Call SwarmSync and return ``(routed_model, assistant_message)``.

        Unlike ``_swarmsync_complete``, this preserves structured OpenAI-style
        ``tool_calls`` returned by SwarmSync so the agent loop can continue the
        conversation with real tool messages.
        """
        payload: dict[str, Any] = {
            "model": "auto",
            "messages": messages,
            "stream": False,
            "swarmsync": {
                "complexity_score": score,
                "history_length": len(messages),
            },
        }
        if tools:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        request_id = str(uuid.uuid4())
        base_record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "provider": "swarmsync",
            "complexity_score": score,
            "history_length": len(messages),
            "has_tools": bool(tools),
            "swarmsync_api_url": self._swarmsync_url,
            "fallback_routing": False,
            "success": False,
        }
        # Circuit breaker check — SwarmSync path only
        import time as _time
        if self._ss_cb_failures >= self._CB_THRESHOLD:
            if _time.monotonic() < self._ss_cb_open_until:
                logger.warning("SwarmSync circuit open — using local selection")
                fallback_model = self.select_model(score)
                _record_routing_decision({
                    **base_record,
                    "chosen_model": fallback_model,
                    "raw_model": "",
                    "routing_reason": "SwarmSync circuit breaker open; local fallback selected",
                    "tier": "",
                    "considered_models": [],
                    "estimated_cost": None,
                    "actual_cost": None,
                    "fallback_routing": True,
                    "error": "circuit_open",
                })
                return fallback_model, {"role": "assistant", "content": ""}
            else:
                # Half-open: allow one probe
                self._ss_cb_failures = 0

        try:
            import aiohttp as _aiohttp
            _per_call_timeout = _aiohttp.ClientTimeout(total=45)
            s = self._get_session()
            async with s.post(
                    self._swarmsync_url,
                    json=payload,
                    headers=headers,
                    timeout=_per_call_timeout,
                ) as r:
                    # SwarmSync returns 200 or 201 depending on version
                    if r.status in (200, 201):
                        data = await r.json()
                        response_request_id = (
                            data.get("request_id")
                            or data.get("id")
                            or r.headers.get("X-Request-ID")
                            or request_id
                        )
                        swarmsync_meta = data.get("swarmsync", {}) if isinstance(data.get("swarmsync"), dict) else {}
                        # Extract routed model name
                        raw_model = (
                            swarmsync_meta.get("routed_model", "")
                            or data.get("model", "")
                        )
                        model = self.select_model(score)  # fallback
                        if raw_model and raw_model != "auto":
                            model = self._resolve_swarmsync_model(raw_model, score)
                            logger.info("SwarmSync routed to: %s (raw: %s)", model, raw_model)
                        considered_models = (
                            _coerce_model_list(swarmsync_meta.get("considered_models"))
                            or _coerce_model_list(swarmsync_meta.get("candidates"))
                            or _coerce_model_list(swarmsync_meta.get("model_candidates"))
                        )
                        # Extract completion text from choices
                        message: dict[str, Any] = {"role": "assistant", "content": ""}
                        choices = data.get("choices", [])
                        if choices:
                            raw_message = choices[0].get("message", {}) or {}
                            if isinstance(raw_message, dict):
                                message = {
                                    "role": raw_message.get("role", "assistant"),
                                    "content": raw_message.get("content", "") or "",
                                }
                                if raw_message.get("tool_calls"):
                                    message["tool_calls"] = raw_message["tool_calls"]
                                if raw_message.get("function_call"):
                                    message["function_call"] = raw_message["function_call"]
                        self._ss_cb_failures = 0  # success — reset SwarmSync circuit
                        tool_call_count = len(message.get("tool_calls") or [])
                        _record_routing_decision({
                            **base_record,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "request_id": response_request_id,
                            "chosen_model": model,
                            "routed_model": model,
                            "raw_model": _scrub_log_text(raw_model),
                            "routing_reason": _scrub_log_text(
                                swarmsync_meta.get("routing_reason")
                                or swarmsync_meta.get("reason")
                                or swarmsync_meta.get("selection_reason")
                                or ""
                            ),
                            "tier": swarmsync_meta.get("tier", ""),
                            "considered_models": considered_models,
                            "estimated_cost": _extract_cost(data, "estimated_cost", "estimated_cost_usd", "estimated_total_cost_usd"),
                            "actual_cost": _extract_cost(data, "actual_cost", "actual_cost_usd", "cost", "cost_usd", "total_cost"),
                            "fallback_routing": bool(swarmsync_meta.get("fallback") or swarmsync_meta.get("fallback_routing")),
                            "success": True,
                            "http_status": r.status,
                            "content_chars": len(message.get("content", "") or ""),
                            "tool_call_count": tool_call_count,
                        })
                        logger.info("SwarmSync raw content repr: %r", _scrub_log_text(message.get("content", "")[:300]))
                        return model, message
                    else:
                        body = await r.text()
                        logger.warning("SwarmSync HTTP %d: %s", r.status, body[:200])
                        self._ss_cb_failures += 1
                        fallback_model = self.select_model(score)
                        _record_routing_decision({
                            **base_record,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "chosen_model": fallback_model,
                            "routed_model": fallback_model,
                            "raw_model": "",
                            "routing_reason": f"SwarmSync HTTP {r.status}; local fallback selected",
                            "tier": "",
                            "considered_models": [],
                            "estimated_cost": None,
                            "actual_cost": None,
                            "fallback_routing": True,
                            "http_status": r.status,
                            "error": _scrub_log_text(body[:500]),
                        })
                        if self._ss_cb_failures >= self._CB_THRESHOLD:
                            self._ss_cb_open_until = _time.monotonic() + self._CB_COOLDOWN
                            logger.warning("SwarmSync circuit opened for %.0fs", self._CB_COOLDOWN)
        except Exception as exc:
            logger.warning("SwarmSync completion failed: %s — using local selection", exc)
            self._ss_cb_failures += 1
            fallback_model = self.select_model(score)
            _record_routing_decision({
                **base_record,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "chosen_model": fallback_model,
                "routed_model": fallback_model,
                "raw_model": "",
                "routing_reason": "SwarmSync exception; local fallback selected",
                "tier": "",
                "considered_models": [],
                "estimated_cost": None,
                "actual_cost": None,
                "fallback_routing": True,
                "error": _scrub_log_text(str(exc)),
            })
            if self._ss_cb_failures >= self._CB_THRESHOLD:
                self._ss_cb_open_until = _time.monotonic() + self._CB_COOLDOWN
                logger.warning("SwarmSync circuit opened for %.0fs", self._CB_COOLDOWN)
        return self.select_model(score), {"role": "assistant", "content": ""}

    def _resolve_swarmsync_model(self, raw_model: str, score: float) -> str:
        """
        Convert a SwarmSync-recommended model slug to a locally-usable model ID.

        Strategy (in order):
          1. Direct translation table hit → use native ID
          2. Model already has a native API key → use as-is
          3. OpenRouter key available → prefix with ``openrouter/`` and route
             through OpenRouter (strips leading ``openrouter/`` if already present
             to avoid double-prefix)
          4. Fall back to local score-based selection
        """
        # 1. Check explicit translation table
        if raw_model in MODEL_TRANSLATIONS:
            translated = MODEL_TRANSLATIONS[raw_model]
            if self._is_available(translated):
                return translated

        # 2. Check if raw model is directly usable (native key exists)
        if self._is_available(raw_model):
            return raw_model

        # 3. Route through OpenRouter if we have a key
        try:
            openrouter_key = self._vault.get("OPENROUTER_API_KEY") if self._vault else ""
        except Exception:
            openrouter_key = ""
        if openrouter_key:
            # Avoid double-prefix: openrouter/openrouter/... is invalid
            clean = raw_model.removeprefix("openrouter/")
            as_openrouter = f"openrouter/{clean}"
            return as_openrouter

        # 4. Fall back to local score-based selection
        logger.warning(
            "SwarmSync recommended %r but no suitable API key found — using local selection",
            raw_model,
        )
        return self.select_model(score)

    async def complete(
        self,
        messages: list[dict],
        model: str,
        tools: Optional[list[dict]] = None,
        stream: bool = True,
    ) -> AsyncIterator[RouterStreamChunk]:
        """Stream completions from the provider matched to *model*.

        On transient failures, classifies the error and attempts fallback
        models from _FALLBACK_CHAIN before giving up.
        """
        models_to_try = [model] + [
            m for m in _FALLBACK_CHAIN
            if m != model and self._is_available(m)
        ]
        last_error: Optional[Exception] = None

        for attempt_model in models_to_try:
            try:
                async for chunk in self._complete_single(attempt_model, messages, tools):
                    yield chunk
                # Success — reset direct-LLM circuit breaker
                self._direct_cb_failures = 0
                return
            except Exception as exc:
                classified = classify_api_error(exc)
                logger.warning(
                    "LLM call to %s failed: %s (reason=%s, retryable=%s, should_fallback=%s)",
                    attempt_model, exc, classified.reason.value,
                    classified.retryable, classified.should_fallback,
                )
                last_error = exc

                # Circuit breaker bookkeeping — direct provider path only
                self._direct_cb_failures += 1
                if self._direct_cb_failures >= self._CB_THRESHOLD:
                    self._direct_cb_open_until = time.monotonic() + self._CB_COOLDOWN

                if not classified.should_fallback:
                    # Non-recoverable errors (format, context overflow) — don't
                    # try other models, they'll hit the same issue.
                    raise

                # Otherwise loop to next fallback model
                continue

        # All models exhausted
        if last_error:
            raise last_error

    async def _complete_single(
        self,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> AsyncIterator[RouterStreamChunk]:
        """Dispatch a single completion to the appropriate provider."""
        base_url, auth = self._resolve_provider(model)
        api_key = self._get_api_key(auth, model)
        if auth == "x-api-key":
            async for c in self._anthropic(messages, model, tools or [], api_key):
                yield c
        elif auth == "google":
            async for c in self._google(messages, model, api_key):
                yield c
        else:
            async for c in self._openai_compat(messages, model, tools or [], api_key, base_url):
                yield c

    # ------------------------------------------------------------------
    # Provider implementations
    # ------------------------------------------------------------------

    async def _anthropic(self, messages: list[dict], model: str,
                          tools: list[dict], api_key: str) -> AsyncIterator[RouterStreamChunk]:
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_msgs: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                continue
            if role == "tool":
                # Anthropic native tool_result format — uses content blocks, not
                # plain text stuffed into user messages.  This enables structured
                # tool calling with Claude models.
                user_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", "unknown"),
                        "content": m.get("content", ""),
                    }],
                })
            elif role == "assistant":
                # If the assistant message had tool_calls, convert them to
                # Anthropic's tool_use content blocks so the conversation
                # round-trips correctly.
                tool_calls = m.get("tool_calls") or []
                if tool_calls:
                    blocks: list[dict] = []
                    if m.get("content"):
                        blocks.append({"type": "text", "text": m["content"]})
                    for tc in tool_calls:
                        fn = tc.get("function", tc) if isinstance(tc, dict) else tc
                        fn_name = fn.get("name", "") if isinstance(fn, dict) else ""
                        fn_args = fn.get("arguments", "{}") if isinstance(fn, dict) else "{}"
                        try:
                            parsed_args = json.loads(fn_args) if isinstance(fn_args, str) else fn_args
                        except json.JSONDecodeError:
                            parsed_args = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", "unknown") if isinstance(tc, dict) else "unknown",
                            "name": fn_name,
                            "input": parsed_args,
                        })
                    user_msgs.append({"role": "assistant", "content": blocks})
                else:
                    user_msgs.append(m)
            else:
                user_msgs.append(m)

        # Convert OpenAI-format tool schemas to Anthropic format
        anthropic_tools: list[dict] = []
        for t in tools:
            fn = t.get("function", {})
            anthropic_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })

        payload: dict[str, Any] = {"model": model, "max_tokens": self._max_output_tokens,
                                   "messages": user_msgs, "stream": True}
        if system:
            payload["system"] = system
        if anthropic_tools:
            payload["tools"] = anthropic_tools
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        s = self._get_session()
        async with s.post("https://api.anthropic.com/v1/messages",
                          json=payload, headers=headers) as r:
            r.raise_for_status()
            # Track tool_use blocks from the stream
            current_tool_use: dict[str, Any] | None = None
            tool_calls_collected: list[dict] = []
            async for line in r.content:
                decoded = line.decode("utf-8").strip()
                if not decoded.startswith("data: "):
                    continue
                raw_data = decoded[6:]
                if raw_data == "[DONE]":
                    break
                try:
                    event = json.loads(raw_data)
                    evt_type = event.get("type", "")

                    if evt_type == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            current_tool_use = {
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": "",
                                },
                            }
                        elif block.get("type") == "text":
                            pass  # text block start, content comes in deltas

                    elif evt_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield delta.get("text", "")
                        elif delta.get("type") == "input_json_delta" and current_tool_use:
                            current_tool_use["function"]["arguments"] += delta.get("partial_json", "")

                    elif evt_type == "content_block_stop":
                        if current_tool_use:
                            tool_calls_collected.append(current_tool_use)
                            current_tool_use = None

                except json.JSONDecodeError:
                    pass

            # Yield collected tool calls at the end (same format as OpenAI compat)
            if tool_calls_collected:
                yield {"type": "tool_calls", "tool_calls": tool_calls_collected}

    async def _openai_compat(self, messages: list[dict], model: str, tools: list[dict],
                              api_key: str, base_url: str) -> AsyncIterator[RouterStreamChunk]:
        # OpenRouter expects "provider/model" not "openrouter/provider/model"
        api_model = model.removeprefix("openrouter/") if model.startswith("openrouter/") else model
        payload: dict[str, Any] = {"model": api_model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        s = self._get_session()
        async with s.post(base_url, json=payload, headers=headers) as r:
            r.raise_for_status()
            tool_call_parts: dict[int, dict[str, Any]] = {}
            async for line in r.content:
                decoded = line.decode("utf-8").strip()
                if not decoded.startswith("data: "):
                    continue
                raw = decoded[6:]
                if raw == "[DONE]":
                    break
                try:
                    choice = json.loads(raw)["choices"][0]
                    delta = choice.get("delta", {})
                    content = delta.get("content")
                    if content and not _is_model_slug_only(content):
                        yield content
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index", 0))
                        part = tool_call_parts.setdefault(
                            idx,
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        if tc.get("id"):
                            part["id"] = tc["id"]
                        if tc.get("type"):
                            part["type"] = tc["type"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            part["function"]["name"] += fn["name"]
                        if "arguments" in fn:
                            part["function"]["arguments"] += fn.get("arguments") or ""
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
            if tool_call_parts:
                yield {
                    "type": "tool_calls",
                    "tool_calls": [
                        tool_call_parts[idx] for idx in sorted(tool_call_parts)
                    ],
                }

    async def _google(self, messages: list[dict], model: str,
                       api_key: str) -> AsyncIterator[str]:
        contents: list[dict] = []
        sys_parts: list[dict] = []
        for m in messages:
            if m["role"] == "system":
                sys_parts.append({"text": m["content"]})
                continue
            # Map tool role back to user for Gemini (no native tool result role)
            role_map = {"assistant": "model", "user": "user", "tool": "user"}
            role = role_map.get(m.get("role", "user"), "user")
            text = m.get("content") or m.get("result", "")
            if isinstance(text, str) and text:
                contents.append({"role": role, "parts": [{"text": text}]})
        payload: dict[str, Any] = {"contents": contents}
        if sys_parts:
            payload["system_instruction"] = {"parts": sys_parts}
        # Use ?alt=sse for true SSE streaming instead of buffered JSON array
        url = (f"https://generativelanguage.googleapis.com/v1beta/models"
               f"/{model}:streamGenerateContent?alt=sse")
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        s = self._get_session()
        async with s.post(url, json=payload, headers=headers) as r:
            r.raise_for_status()
            async for line in r.content:
                decoded = line.decode("utf-8").strip()
                if not decoded.startswith("data: "):
                    continue
                raw_data = decoded[6:]
                if raw_data == "[DONE]":
                    break
                try:
                    evt = json.loads(raw_data)
                    for cand in evt.get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            if "text" in part:
                                yield part["text"]
                except (json.JSONDecodeError, KeyError):
                    pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_provider(self, model: str) -> tuple[str, str]:
        for prefix, url, auth in _PROVIDERS:
            if model.startswith(prefix):
                return url, auth
        return "https://api.openai.com/v1/chat/completions", "bearer"

    def _is_available(self, model: str) -> bool:
        resolved = MODEL_TRANSLATIONS.get(model, model)
        if resolved in self._blocked or model in self._blocked:
            return False
        _, auth = self._resolve_provider(resolved)
        api_key = self._get_api_key(auth, resolved)
        return bool(api_key)

    def _get_api_key(self, auth: str, model: str) -> str:
        import os as _os

        def _vault_or_env(vault_key: str) -> str:
            try:
                v = self._vault.get(vault_key)
            except Exception:
                v = None
            return v or _os.environ.get(vault_key, "")

        if auth == "x-api-key":
            return _vault_or_env("ANTHROPIC_API_KEY")
        if auth == "google":
            return _vault_or_env("GOOGLE_API_KEY")
        if auth == "openrouter":
            return _vault_or_env("OPENROUTER_API_KEY")
        mapping = {
            "openrouter/": "OPENROUTER_API_KEY",
            "swarmsync/":  "SWARMSYNC_API_KEY",
            "deepseek-":   "DEEPSEEK_API_KEY",
            "llama-":      "GROQ_API_KEY",
            "mistral-":    "MISTRAL_API_KEY",
            "abab":        "MINIMAX_API_KEY",
            "moonshot-":   "MOONSHOT_API_KEY",
        }
        for prefix, vault_key in mapping.items():
            if model.startswith(prefix):
                return _vault_or_env(vault_key)
        return _vault_or_env("OPENAI_API_KEY")
