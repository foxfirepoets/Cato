"""
cato/api/websocket_handler.py — WebSocket endpoint for the coding agent.

Endpoint: GET /ws/coding-agent/{task_id}

Streams model responses as newline-delimited JSON events:
  - claude_response
  - codex_response
  - gemini_response
  - synthesis_complete
  - early_termination
  - error
  - heartbeat

Each JSON event format:
  {"event": "<event_name>", "data": {...}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from typing import Any, Dict, Optional

from aiohttp import web, WSMsgType

try:
    from cato.config import CatoConfig
except ImportError:  # pragma: no cover
    CatoConfig = None  # type: ignore[assignment,misc]

try:
    from cato.orchestrator.metrics import track_invocation, get_token_report
except ImportError:  # pragma: no cover
    def track_invocation(*args, **kwargs) -> None:  # type: ignore[misc]
        pass

    def get_token_report(**kwargs) -> dict:  # type: ignore[misc]
        return {}

# Module-level imports for model invocations — importable at module level so
# that tests can patch them via `cato.api.websocket_handler.invoke_claude_api`.
try:
    from cato.orchestrator.cli_invoker import (
        invoke_claude_api,
        invoke_codex_cli,
        invoke_gemini_cli,
        invoke_cursor_cli,
    )
except ImportError:  # pragma: no cover — running without orchestrator installed
    async def invoke_claude_api(prompt: str, task: str) -> dict:  # type: ignore[misc]
        return {"model": "claude", "response": "Claude unavailable.", "confidence": 0.75, "latency_ms": 0, "source": "mock"}

    async def invoke_codex_cli(prompt: str, task: str) -> dict:  # type: ignore[misc]
        return {"model": "codex", "response": "Codex unavailable.", "confidence": 0.72, "latency_ms": 0, "source": "mock"}

    async def invoke_gemini_cli(prompt: str, task: str) -> dict:  # type: ignore[misc]
        return {"model": "gemini", "response": "Gemini unavailable.", "confidence": 0.68, "latency_ms": 0, "source": "mock"}

    async def invoke_cursor_cli(prompt: str, task: str) -> dict:  # type: ignore[misc]
        return {"model": "cursor", "response": "Cursor unavailable.", "confidence": 0.70, "latency_ms": 0, "source": "mock"}

logger = logging.getLogger(__name__)

# In-memory task store: task_id -> task_info
_task_store: Dict[str, Dict[str, Any]] = {}

HEARTBEAT_INTERVAL = 2.0  # seconds between heartbeats
TASK_TIMEOUT = 30.0        # seconds before task times out


def _serialize_event(event: str, data: Dict[str, Any]) -> str:
    """Serialize a WebSocket event to newline-delimited JSON."""
    return json.dumps({"event": event, "data": data}) + "\n"


def _confidence_level(confidence: float) -> str:
    """Return color tier for confidence score."""
    if confidence >= 0.90:
        return "high"
    elif confidence >= 0.70:
        return "medium"
    return "low"


async def _run_model_and_stream(
    ws: web.WebSocketResponse,
    model: str,
    task: str,
    prompt: str,
) -> Optional[Dict[str, Any]]:
    """
    Invoke a single model and stream the response back over WebSocket.

    Returns the result dict or None on error.
    """
    try:
        # Use module-level references so that unit tests can patch them via:
        #   patch("cato.api.websocket_handler.invoke_claude_api", ...)
        invokers = {
            "claude": invoke_claude_api,
            "codex":  invoke_codex_cli,
            "gemini": invoke_gemini_cli,
            "cursor": invoke_cursor_cli,
        }
        invoker = invokers.get(model)
        if invoker is None:
            raise ValueError(f"Unknown model: {model}")

        result = await invoker(prompt, task)
        confidence = result.get("confidence", 0.75)

        if not ws.closed:
            payload = _serialize_event(f"{model}_response", {
                "id":           str(uuid.uuid4()),
                "model":        model,
                "text":         result.get("response", ""),
                "confidence":   confidence,
                "confidence_level": _confidence_level(confidence),
                "latency_ms":   result.get("latency_ms", 0),
                "timestamp":    int(time.time() * 1000),
                "reasoning":    result.get("reasoning"),
                "code":         result.get("code"),
                "source":       result.get("source", "subprocess"),
            })
            await ws.send_str(payload)

        return result

    except Exception as exc:
        logger.exception("Error invoking model %s: %s", model, exc)
        if not ws.closed:
            error_payload = _serialize_event("error", {
                "message": str(exc),
                "model":   model,
            })
            await ws.send_str(error_payload)
        return None


def _synthesize_results(results: list[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    Synthesize model results by selecting the primary (highest confidence)
    and listing runners-up.
    """
    valid = [r for r in results if r is not None and "response" in r]
    if not valid:
        return {
            "primary":     None,
            "runners_up":  [],
            "early_exit":  False,
        }

    # Sort by confidence descending
    ranked = sorted(valid, key=lambda r: r.get("confidence", 0.0), reverse=True)

    primary = {
        "model":      ranked[0].get("model", "unknown"),
        "response":   ranked[0].get("response", ""),
        "confidence": ranked[0].get("confidence", 0.75),
        "confidence_level": _confidence_level(ranked[0].get("confidence", 0.75)),
        "latency_ms": ranked[0].get("latency_ms", 0),
    }

    runners_up = [
        {
            "model":      r.get("model", "unknown"),
            "response":   r.get("response", ""),
            "confidence": r.get("confidence", 0.75),
            "confidence_level": _confidence_level(r.get("confidence", 0.75)),
        }
        for r in ranked[1:]
    ]

    return {
        "primary":    primary,
        "runners_up": runners_up,
        "early_exit": False,
    }


async def coding_agent_ws_handler(request: web.Request) -> web.WebSocketResponse:
    """
    WebSocket handler for /ws/coding-agent/{task_id}.

    Accepts an upgrade, retrieves the task by task_id, invokes all 3 models
    in parallel, streams each response as it arrives, then sends synthesis.

    Heartbeat events are sent every HEARTBEAT_INTERVAL seconds to prevent
    clients from assuming the connection is dead.
    """
    task_id = request.match_info.get("task_id", "unknown")
    logger.info("WebSocket connection for task %s", task_id)

    daemon_token: str = request.app.get("daemon_token", "")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    # Authenticate: only required when daemon_token is configured.
    if daemon_token:
        token = (request.headers.get("X-Cato-Token", "")
                 or request.rel_url.query.get("token", ""))
        if not token:
            try:
                first_msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                if first_msg.type == WSMsgType.TEXT:
                    parsed = json.loads(first_msg.data)
                    if parsed.get("type") == "auth":
                        token = str(parsed.get("token") or "")
            except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
                pass
        if not secrets.compare_digest(token, daemon_token):
            await ws.send_str(_serialize_event("error", {"message": "Unauthorized", "model": None}))
            await ws.close(code=4401, message=b"Unauthorized")
            return ws

    # Retrieve task from store
    task_info = _task_store.get(task_id)
    if task_info is None:
        # Unknown task — send error and close
        await ws.send_str(_serialize_event("error", {
            "message": f"Task '{task_id}' not found.",
            "model":   None,
        }))
        await ws.close()
        return ws

    task_text = task_info.get("task", "")
    prompt    = task_info.get("prompt", task_text)
    # Enabled models come from the task payload (set at invoke time from config)
    enabled_models: list[str] = task_info.get(
        "enabled_models", ["claude", "codex", "gemini"]
    )
    if not enabled_models:
        enabled_models = ["claude", "codex", "gemini"]

    # Send initial status
    await ws.send_str(_serialize_event("status", {
        "task_id":  task_id,
        "message":  "Starting model invocations...",
        "models":   enabled_models,
        "timestamp": int(time.time() * 1000),
    }))

    # Send pool status so the frontend knows warm vs cold
    try:
        from cato.orchestrator.cli_process_pool import get_pool
        pool = get_pool()
        pool_info = {
            "claude": "warm" if pool.is_warm("claude") else "cold",
            "codex":  "warm" if pool.is_warm("codex") else "cold",
            "gemini": "subprocess",  # no daemon mode
            "cursor": "subprocess",  # no daemon mode
        }
    except Exception:
        pool_info = {"claude": "cold", "codex": "cold", "gemini": "subprocess", "cursor": "subprocess"}

    await ws.send_str(_serialize_event("pool_status", {
        "models": pool_info,
        "timestamp": int(time.time() * 1000),
    }))

    # ------------------------------------------------------------------ #
    # Run heartbeat + model invocations concurrently                      #
    # ------------------------------------------------------------------ #

    results: list[Optional[Dict[str, Any]]] = [None] * len(enabled_models)
    done_event = asyncio.Event()

    async def heartbeat_loop() -> None:
        """Send heartbeat pings until the models are done."""
        while not done_event.is_set():
            if ws.closed:
                return
            try:
                await ws.send_str(_serialize_event("heartbeat", {
                    "timestamp": int(time.time() * 1000),
                }))
            except Exception:
                return
            try:
                await asyncio.wait_for(asyncio.shield(done_event.wait()),
                                       timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def run_models() -> None:
        """Run enabled models in parallel; set done_event when complete."""
        tasks = [
            asyncio.create_task(_run_model_and_stream(ws, m, task_text, prompt))
            for m in enabled_models
        ]
        model_results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, r in enumerate(model_results):
            if isinstance(r, Exception):
                results[i] = None
            else:
                results[i] = r
        done_event.set()

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    models_task    = asyncio.create_task(run_models())

    try:
        # Wait for models to finish OR task timeout
        await asyncio.wait_for(models_task, timeout=TASK_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Task %s timed out after %ss", task_id, TASK_TIMEOUT)
        done_event.set()
        if not ws.closed:
            await ws.send_str(_serialize_event("error", {
                "message": f"Task timed out after {TASK_TIMEOUT}s",
                "model":   None,
            }))
    finally:
        done_event.set()
        heartbeat_task.cancel()

    # ------------------------------------------------------------------ #
    # Send synthesis                                                       #
    # ------------------------------------------------------------------ #
    if not ws.closed:
        synthesis = _synthesize_results(results)

        # Check for early termination (any model >= 0.95 confidence)
        primary = synthesis.get("primary")
        early_exit = bool(primary and primary.get("confidence", 0) >= 0.95)
        if early_exit:
            synthesis["early_exit"] = True
            await ws.send_str(_serialize_event("early_termination", {
                "winner":     primary.get("model"),
                "confidence": primary.get("confidence"),
                "timestamp":  int(time.time() * 1000),
            }))

        await ws.send_str(_serialize_event("synthesis_complete", {
            "primary":    synthesis.get("primary"),
            "runners_up": synthesis.get("runners_up", []),
            "early_exit": synthesis.get("early_exit", False),
            "timestamp":  int(time.time() * 1000),
        }))

        # Record invocation metrics and emit token telemetry
        if primary:
            individual_latencies = {
                r.get("model", "unknown"): r.get("latency_ms", 0.0)
                for r in results if r is not None
            }

            # Estimate token counts from the prompt and responses (rough approx:
            # 4 chars per token — avoids importing tiktoken in the WS handler).
            tokens_in_est = max(1, len(prompt) // 4)
            tokens_out_est = max(0, sum(
                len(r.get("response", "")) // 4
                for r in results if r is not None
            ))

            track_invocation(
                task=task_text,
                total_latency_ms=sum(individual_latencies.values()),
                winner_model=primary.get("model", "unknown"),
                winner_confidence=primary.get("confidence", 0.0),
                terminated_early=early_exit,
                models_responded=sum(1 for r in results if r is not None),
                individual_latencies=individual_latencies,
                tokens_in=tokens_in_est,
                tokens_out=tokens_out_est,
                query_tier="tier1",
                context_slots_used={
                    "tier0": 0,
                    "tier1_memory": 0,
                    "tier1_skill": 0,
                    "tier1_tools": 0,
                    "history": tokens_in_est,
                },
            )

            # Emit token_telemetry event so the UI can display live usage
            token_summary = get_token_report()
            if not ws.closed:
                await ws.send_str(_serialize_event("token_telemetry", {
                    "turn_tokens_in": tokens_in_est,
                    "turn_tokens_out": tokens_out_est,
                    "session_total_in": token_summary.get("total_tokens_in", 0),
                    "session_total_out": token_summary.get("total_tokens_out", 0),
                    "avg_tokens_in_last_100": token_summary.get("avg_tokens_in_last_100", 0.0),
                    "input_output_ratio": token_summary.get("ratio_in_to_out", 0.0),
                    "estimated_cost_usd": token_summary.get("estimated_cost_usd", 0.0),
                    "timestamp": int(time.time() * 1000),
                }))

    await ws.close()
    return ws


async def invoke_coding_agent(request: web.Request) -> web.Response:
    """
    POST /api/coding-agent/invoke

    Body:
        {
            "task": "Review this function",
            "language": "python",
            "context": "def foo(): ..."
        }

    Response:
        {
            "task_id": "abc-123",
            "status": "queued"
        }
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "Invalid JSON body"},
            status=400,
        )

    task_text = (body.get("task") or "").strip()
    if not task_text:
        return web.json_response(
            {"error": "task is required"},
            status=400,
        )
    if len(task_text) < 10:
        return web.json_response(
            {"error": "task must be at least 10 characters"},
            status=400,
        )
    if len(task_text) > 500:
        return web.json_response(
            {"error": "task must be at most 500 characters"},
            status=400,
        )

    language = body.get("language", "")
    context  = body.get("context", "")

    # Validate and sanitize enabled_models list
    _all_models = {"claude", "codex", "gemini", "cursor"}
    raw_models = body.get("enabled_models", ["claude", "codex", "gemini"])
    if not isinstance(raw_models, list):
        raw_models = ["claude", "codex", "gemini"]
    enabled_models = [m for m in raw_models if isinstance(m, str) and m in _all_models]
    if not enabled_models:
        enabled_models = ["claude", "codex", "gemini"]

    task_id = str(uuid.uuid4())

    prompt_parts = [task_text]
    if language:
        prompt_parts.append(f"Language: {language}")
    if context:
        prompt_parts.append(f"Code context:\n{context}")

    _task_store[task_id] = {
        "task":           task_text,
        "language":       language,
        "context":        context,
        "prompt":         "\n\n".join(prompt_parts),
        "created_at":     int(time.time() * 1000),
        "status":         "queued",
        "enabled_models": enabled_models,
    }

    logger.info("Created task %s: %r", task_id, task_text[:60])

    return web.json_response({
        "task_id": task_id,
        "status":  "queued",
    })


async def get_task_info(request: web.Request) -> web.Response:
    """
    GET /api/coding-agent/{task_id}

    Returns task metadata (not the responses — those come via WebSocket).
    """
    task_id = request.match_info.get("task_id", "")
    task_info = _task_store.get(task_id)
    if task_info is None:
        return web.json_response({"error": "Task not found"}, status=404)

    # Return safe subset (no full prompt)
    return web.json_response({
        "task_id":    task_id,
        "task":       task_info.get("task", ""),
        "language":   task_info.get("language", ""),
        "created_at": task_info.get("created_at"),
        "status":     task_info.get("status", "queued"),
    })


_ALL_MODELS = {"claude", "codex", "gemini", "cursor"}
_SUBAGENT_BACKENDS = {"claude", "codex", "gemini", "cursor"}


async def get_config(request: web.Request) -> web.Response:
    """
    GET /api/config

    Returns the subagent-related config fields so the UI can render toggles.

    Response::

        {
            "enabled_models": ["claude", "codex", "gemini"],
            "subagent_enabled": false,
            "subagent_coding_backend": "codex"
        }
    """
    try:
        cfg = CatoConfig.load()
        enabled_models = getattr(cfg, "enabled_models", ["claude", "codex", "gemini"])
        if not isinstance(enabled_models, list):
            enabled_models = ["claude", "codex", "gemini"]
    except Exception:
        enabled_models = ["claude", "codex", "gemini"]
        cfg = None

    return web.json_response({
        "enabled_models":          enabled_models,
        "subagent_enabled":        getattr(cfg, "subagent_enabled", False) if cfg else False,
        "subagent_coding_backend": getattr(cfg, "subagent_coding_backend", "codex") if cfg else "codex",
    })


async def patch_config(request: web.Request) -> web.Response:
    """
    PATCH /api/config

    Accepts a partial config update and persists it to ~/.cato/config.yaml.

    Body (all fields optional)::

        {
            "enabled_models": ["claude", "gemini"],
            "subagent_enabled": true,
            "subagent_coding_backend": "cursor"
        }

    Response: the full updated config (same shape as GET /api/config).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    try:
        cfg = CatoConfig.load()
    except Exception as exc:
        return web.json_response({"error": f"Config load failed: {exc}"}, status=500)

    # enabled_models
    if "enabled_models" in body:
        raw = body["enabled_models"]
        if not isinstance(raw, list):
            return web.json_response({"error": "enabled_models must be a list"}, status=400)
        validated = [m for m in raw if isinstance(m, str) and m in _ALL_MODELS]
        if not validated:
            return web.json_response({"error": "enabled_models must contain at least one valid model"}, status=400)
        cfg.enabled_models = validated  # type: ignore[attr-defined]

    # subagent_enabled
    if "subagent_enabled" in body:
        val = body["subagent_enabled"]
        if not isinstance(val, bool):
            return web.json_response({"error": "subagent_enabled must be a boolean"}, status=400)
        cfg.subagent_enabled = val

    # subagent_coding_backend
    if "subagent_coding_backend" in body:
        val = body["subagent_coding_backend"]
        if val not in _SUBAGENT_BACKENDS:
            return web.json_response(
                {"error": f"subagent_coding_backend must be one of {sorted(_SUBAGENT_BACKENDS)}"},
                status=400,
            )
        cfg.subagent_coding_backend = val

    try:
        cfg.save()
    except Exception as exc:
        return web.json_response({"error": f"Config save failed: {exc}"}, status=500)

    return web.json_response({
        "enabled_models":          getattr(cfg, "enabled_models", ["claude", "codex", "gemini"]),
        "subagent_enabled":        cfg.subagent_enabled,
        "subagent_coding_backend": cfg.subagent_coding_backend,
    })


def register_routes(app: web.Application) -> None:
    """Register coding agent routes onto an aiohttp Application."""
    app.router.add_post("/api/coding-agent/invoke",        invoke_coding_agent)
    app.router.add_get("/api/coding-agent/{task_id}",      get_task_info)
    app.router.add_get("/ws/coding-agent/{task_id}",       coding_agent_ws_handler)
    app.router.add_get("/api/config",                      get_config)
    app.router.add_patch("/api/config",                    patch_config)
    logger.info("Coding agent routes registered")
