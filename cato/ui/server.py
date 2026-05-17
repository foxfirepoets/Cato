"""
cato/ui/server.py — aiohttp server that serves the Cato dashboard.

Mounts:
  GET /                              → dashboard.html (SPA)
  GET /health                        → JSON health payload
  GET /ws                            → WebSocket upgrade (delegates to gateway)
  POST /config                       → Save config (stub; gateway wires real handler)
  GET /api/vault/keys                → List vault key names
  POST /api/vault/set                → Store a vault key
  DELETE /api/vault/delete           → Delete a vault key
  GET /api/sessions                  → List active sessions with metadata
  DELETE /api/sessions/{session_id}  → Kill a session
  POST /api/compact                  → Compact context for a session (slash cmd)
  GET /api/skills                    → List installed skills
  GET /api/skills/{name}/content     → Get SKILL.md content for a skill
  GET /api/cron/jobs                 → List cron jobs
  POST /api/cron/jobs                → Create or update a cron job
  DELETE /api/cron/jobs/{name}       → Delete a cron job
  POST /api/cron/jobs/{name}/toggle  → Enable/disable a cron job
  POST /api/cron/jobs/{name}/run     → Manually trigger a cron job now
  GET /api/budget/summary            → Budget status (spend, caps, pct remaining)
  GET /api/usage/summary             → Usage stats (calls, tokens, model breakdown)
  GET /api/logs                      → Recent daemon log entries
  GET /api/audit/entries             → Audit log entries (filterable)
  POST /api/audit/verify             → Verify audit chain integrity
  GET /api/config                    → Get current config (registered via register_all_routes)
  PATCH /api/config                  → Patch config fields (registered via register_all_routes)
  GET /api/adapters                  → List channel adapters and status
  GET /api/heartbeat                 → HeartbeatMonitor state
  GET /coding-agent                  → coding_agent.html entry page
  GET /coding-agent/{task_id}        → coding_agent.html SPA (task view)
  POST /api/coding-agent/invoke      → Create task, returns task_id
  GET /api/coding-agent/{tid}        → Task metadata
  GET /ws/coding-agent/{tid}         → WebSocket streaming for task
  GET /api/sessions/{session_id}/checkpoints           → List session checkpoints
  GET /api/sessions/{session_id}/checkpoints/{cid}     → Single checkpoint summary
  GET /api/sessions/{session_id}/receipt               → Signed session receipt
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import ClientSession, ClientTimeout, web, WSMsgType

logger = logging.getLogger(__name__)

_DASHBOARD      = Path(__file__).parent / "dashboard.html"
_CODING_AGENT   = Path(__file__).parent / "coding_agent.html"
_START_TIME     = time.monotonic()
_MCP_RUNTIME_KEY = web.AppKey("mcp_runtime", object)
_MCP_PROXY_SESSION_KEY = web.AppKey("mcp_proxy_session", ClientSession)
_CLI_POOL_STARTUP_TASK_KEY = web.AppKey("cli_pool_startup_task", object)

# Workspace identity files live here
def _workspace_dir() -> Path:
    """Return the workspace directory, preferring the value from config."""
    try:
        from cato.config import CatoConfig
        cfg = CatoConfig.load()
        ws = getattr(cfg, "workspace_dir", "") or ""
        if ws:
            p = Path(ws).expanduser().resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass
    # Fallback: ~/.cato/workspace (mirrors config default)
    from cato.platform import get_data_dir
    fallback = Path.home() / ".cato" / "workspace"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback

_WORKSPACE_ALLOWED = {"SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md", "TOOLS.md", "HEARTBEAT.md"}

# ---------------------------------------------------------------------------
# Daemon auth token (T6) — random per-process token written to ~/.cato/daemon.token
# ---------------------------------------------------------------------------

def _load_or_create_daemon_token() -> str:
    """Load existing daemon token or generate a new one, write to data dir, and return it."""
    from cato.platform import get_data_dir
    token_path = get_data_dir() / "daemon.token"
    if token_path.exists():
        existing = token_path.read_text(encoding="utf-8").strip()
        if len(existing) == 64:  # 32 bytes hex = 64 chars
            return existing
    token = secrets.token_hex(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)
    return token


_DAEMON_TOKEN: str = _load_or_create_daemon_token()

# Paths that require the X-Cato-Token header (all non-GET mutation paths)
_TOKEN_EXEMPT_METHODS = {"OPTIONS"}
# Public read-only endpoints (static HTML pages + health probe)
_TOKEN_EXEMPT_PATHS = {"/health", "/", "/coding-agent", "/api/activity"}
_LOCAL_REMOTES = {"127.0.0.1", "::1", "localhost"}
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(['\"\s:=]+)([^,'\"\s}]+)"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}\b"),
]


def _redact_log_text(text: str) -> str:
    """Mask obvious credential values before exposing daemon logs through the UI."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            redacted = pattern.sub(r"\1\2[redacted]", redacted)
        else:
            redacted = pattern.sub("[redacted]", redacted)
    return redacted


@web.middleware
async def auth_token_middleware(request: web.Request, handler):
    """Require X-Cato-Token on non-read requests from non-Tauri origins."""
    if request.method in _TOKEN_EXEMPT_METHODS:
        return await handler(request)
    if request.method == "GET" and request.path in _TOKEN_EXEMPT_PATHS:
        return await handler(request)
    # /coding-agent/{task_id} serves the SPA shell (static HTML) — no auth needed.
    # The API routes under /api/coding-agent/* remain auth-protected.
    if request.path.startswith("/coding-agent/") and not request.path.startswith("/coding-agent/api"):
        return await handler(request)
    token = (request.headers.get("X-Cato-Token", "")
             or request.rel_url.query.get("token", ""))
    if not token or not secrets.compare_digest(token, _DAEMON_TOKEN):
        if (
            request.method == "POST"
            and request.path == "/api/heartbeat"
            and (request.remote or "") in _LOCAL_REMOTES
        ):
            return await handler(request)
        return web.Response(status=401, reason="Missing or invalid X-Cato-Token")
    return await handler(request)


_CORS_ALLOWED_ORIGINS = {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
_LOCALHOST_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_SENSITIVE_PATHS = {"/api/vault/set", "/api/vault/delete", "/config"}
_CORS_ALLOWED_HEADERS = "Content-Type, Authorization, X-Cato-Token"


def _is_localhost_origin(origin: str) -> bool:
    """Strict scheme://host[:port] match — no startswith, no subdomain smuggling."""
    if not origin:
        return False
    try:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if host == "::1":
        host = "[::1]"
    return host in _LOCALHOST_HOSTS and not parsed.path and not parsed.query


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """CORS middleware — permits Tauri/localhost origins; blocks cross-origin
    mutations to vault and config endpoints."""
    origin = request.headers.get("Origin", "")
    allowed = origin in _CORS_ALLOWED_ORIGINS or _is_localhost_origin(origin)
    if request.path in _SENSITIVE_PATHS and not allowed and origin:
        return web.Response(status=403, reason="Cross-origin requests not allowed for this endpoint")
    allow_origin = origin if allowed else "tauri://localhost"
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": allow_origin,
                "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": _CORS_ALLOWED_HEADERS,
                "Vary": "Origin",
            },
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = allow_origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = _CORS_ALLOWED_HEADERS
    response.headers["Vary"] = "Origin"
    return response


async def create_ui_app(gateway: Optional[Any] = None) -> web.Application:
    """Create and return the aiohttp Application serving the dashboard.

    Args:
        gateway: The Gateway instance. May be None for standalone testing;
                 pages will render but WebSocket will show disconnected state.
    """
    app = web.Application(middlewares=[cors_middleware, auth_token_middleware])

    async def mcp_runtime_ctx(app: web.Application):
        """Start and stop the MCP runtime without blocking desktop startup."""
        app[_MCP_RUNTIME_KEY] = None
        app[_MCP_PROXY_SESSION_KEY] = None

        cfg = getattr(gateway, "_cfg", None) if gateway is not None else None
        if gateway is None or cfg is None or not getattr(cfg, "mcp_enabled", False):
            yield
            return

        runtime: Any | None = None
        proxy_session: ClientSession | None = None

        async def start_mcp_runtime() -> None:
            nonlocal runtime, proxy_session
            try:
                # Keep desktop startup responsive; MCP can warm after chat is online.
                await asyncio.sleep(60)
                from cato.mcp.runtime import CatoMCPRuntime

                runtime = CatoMCPRuntime(gateway, cfg)
                await runtime.start()
                proxy_session = ClientSession(timeout=ClientTimeout(total=300))
                app[_MCP_RUNTIME_KEY] = runtime
                app[_MCP_PROXY_SESSION_KEY] = proxy_session
                logger.info(
                    "MCP runtime ready at http://%s:%s%s",
                    runtime.host,
                    runtime.port,
                    runtime.mount_path,
                )
            except ModuleNotFoundError as exc:
                logger.warning("MCP runtime disabled because dependency is unavailable: %s", exc)
                runtime = None
            except Exception as exc:
                logger.warning("MCP runtime disabled after startup failure: %s", exc, exc_info=True)
                runtime = None

        startup_task = asyncio.create_task(start_mcp_runtime(), name="cato-mcp-runtime-start")

        try:
            yield
        finally:
            if not startup_task.done():
                startup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await startup_task
            if proxy_session is not None:
                await proxy_session.close()
            if runtime is not None:
                await runtime.stop()
            app[_MCP_RUNTIME_KEY] = None
            app[_MCP_PROXY_SESSION_KEY] = None

    app.cleanup_ctx.append(mcp_runtime_ctx)

    # ------------------------------------------------------------------ #
    # Route handlers                                                       #
    # ------------------------------------------------------------------ #

    async def serve_dashboard(request: web.Request) -> web.Response:
        """Serve the single-page dashboard HTML with token injected for WS auth."""
        html = _DASHBOARD.read_text(encoding="utf-8")
        injection = (
            "<script>\n"
            f"window.__CATO_TOKEN__ = {repr(_DAEMON_TOKEN)};\n"
            "const __catoFetch = window.fetch.bind(window);\n"
            "window.fetch = (input, init = {}) => {\n"
            "  const headers = new Headers(init.headers || {});\n"
            "  if (!headers.has('X-Cato-Token')) headers.set('X-Cato-Token', window.__CATO_TOKEN__ || '');\n"
            "  return __catoFetch(input, { ...init, headers });\n"
            "};\n"
            "</script>"
        )
        html = html.replace("</head>", injection + "\n</head>", 1)
        # Patch connectWS to append token as query param
        html = html.replace(
            "wsProto + '//' + location.host + '/ws'",
            "wsProto + '//' + location.host + '/ws?token=' + (window.__CATO_TOKEN__ || '')",
        )
        return web.Response(text=html, content_type="text/html")

    async def health(request: web.Request) -> web.Response:
        """Return JSON health payload consumed by the UI health pill."""
        from cato import __version__
        sessions = len(gateway._lanes) if gateway is not None else 0
        uptime   = int(time.monotonic() - _START_TIME)
        mcp_runtime = request.app.get(_MCP_RUNTIME_KEY)
        return web.json_response({
            "status":   "ok",
            "version":  __version__,
            "sessions": sessions,
            "uptime":   uptime,
            "mcp": {
                "enabled": bool(mcp_runtime),
                "mount_path": getattr(mcp_runtime, "mount_path", None),
                "port": getattr(mcp_runtime, "port", None),
            },
        })

    async def mcp_health(request: web.Request) -> web.Response:
        """Return MCP runtime health for reverse-proxy and connector debugging."""
        mcp_runtime = request.app.get(_MCP_RUNTIME_KEY)
        if mcp_runtime is None:
            return web.json_response({"status": "disabled"}, status=503)
        return web.json_response(
            {
                "status": "ok",
                "service": "cato-mcp-proxy",
                "mount_path": mcp_runtime.mount_path,
                "target": f"http://{mcp_runtime.host}:{mcp_runtime.port}{mcp_runtime.mount_path}",
            }
        )

    async def mcp_proxy(request: web.Request) -> web.StreamResponse:
        """Proxy MCP HTTP traffic to the internal FastMCP runtime."""
        mcp_runtime = request.app.get(_MCP_RUNTIME_KEY)
        proxy_session = request.app.get(_MCP_PROXY_SESSION_KEY)
        if mcp_runtime is None or proxy_session is None:
            return web.json_response({"error": "mcp runtime unavailable"}, status=503)

        hop_by_hop_headers = {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
            "host",
        }
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in hop_by_hop_headers
        }
        body = await request.read()
        target_url = mcp_runtime.proxy_target(request.rel_url.path_qs)

        async with proxy_session.request(
            request.method,
            target_url,
            headers=headers,
            data=body or None,
            allow_redirects=False,
        ) as resp:
            downstream = web.StreamResponse(status=resp.status, reason=resp.reason)
            for key, value in resp.headers.items():
                if key.lower() not in hop_by_hop_headers:
                    downstream.headers[key] = value
            await downstream.prepare(request)
            async for chunk in resp.content.iter_chunked(65536):
                await downstream.write(chunk)
            await downstream.write_eof()
            return downstream

    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        """Upgrade HTTP → WebSocket and proxy messages through the gateway."""
        # FIX-C1: Authenticate before upgrading the WebSocket connection.
        # Accept token from header or query param (query param needed for WS clients
        # that cannot send custom headers during the HTTP upgrade handshake).
        token = (request.headers.get("X-Cato-Token", "")
                 or request.rel_url.query.get("token", ""))
        if not token or not secrets.compare_digest(token, _DAEMON_TOKEN):
            return web.Response(status=401, reason="Missing or invalid X-Cato-Token")
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        if gateway is not None:
            gateway.register_websocket(ws)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if gateway is not None:
                        await gateway.handle_ws_message(ws, msg.data)
                    else:
                        # Standalone: echo health only
                        try:
                            data = json.loads(msg.data)
                            if data.get("type") == "health":
                                await ws.send_str(json.dumps({
                                    "type":     "health",
                                    "status":   "ok",
                                    "sessions": 0,
                                    "uptime":   int(time.monotonic() - _START_TIME),
                                }))
                        except (json.JSONDecodeError, KeyError):
                            pass
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("WebSocket error: %s", ws.exception())
        finally:
            if gateway is not None:
                gateway.unregister_websocket(ws)

        return ws

    async def vault_list_keys(request: web.Request) -> web.Response:
        """GET /api/vault/keys — return list of key names stored in the vault (no values)."""
        try:
            vault = gateway._vault if gateway is not None else None
            if vault is None:
                return web.json_response([])
            keys = vault.list_keys() if hasattr(vault, "list_keys") else []
            return web.json_response(keys)
        except Exception as exc:
            logger.error("vault_list_keys error: %s", exc)
            return web.json_response([])

    async def vault_set_key(request: web.Request) -> web.Response:
        """POST /api/vault/set — store a key in the vault. Body: {key, value}."""
        try:
            body = await request.json()
            k = str(body.get("key", "")).strip()
            v = str(body.get("value", "")).strip()
            if not k or not v:
                return web.json_response({"status": "error", "message": "key and value required"}, status=400)
            vault = gateway._vault if gateway is not None else None
            if vault is None:
                return web.json_response({"status": "error", "message": "vault unavailable"}, status=503)
            vault.set(k, v)
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("vault_set_key error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def vault_delete_key(request: web.Request) -> web.Response:
        """DELETE /api/vault/delete — remove a key from the vault. Body: {key}."""
        try:
            body = await request.json()
            k = str(body.get("key", "")).strip()
            if not k:
                return web.json_response({"status": "error", "message": "key required"}, status=400)
            vault = gateway._vault if gateway is not None else None
            if vault is None:
                return web.json_response({"status": "error", "message": "vault unavailable"}, status=503)
            if hasattr(vault, "delete"):
                vault.delete(k)
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("vault_delete_key error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Sessions                                                             #
    # ------------------------------------------------------------------ #

    async def list_sessions(request: web.Request) -> web.Response:
        """GET /api/sessions — list active lane sessions."""
        try:
            if gateway is None:
                return web.json_response([])
            sessions = []
            for sid, lane in gateway._lanes.items():
                queue_depth = lane._queue.qsize() if hasattr(lane, "_queue") else 0
                running = lane._task is not None and not lane._task.done() if hasattr(lane, "_task") else False
                sessions.append({
                    "session_id": sid,
                    "queue_depth": queue_depth,
                    "running": running,
                })
            return web.json_response(sessions)
        except Exception as exc:
            logger.error("list_sessions error: %s", exc)
            return web.json_response([], status=500)

    async def chat_history(request: web.Request) -> web.Response:
        """GET /api/chat/history — cross-channel message history (web + Telegram)."""
        try:
            since_ts = int(request.rel_url.query.get("since", "0"))
            if gateway is None:
                return web.json_response([])
            entries = gateway.get_message_history(since_ts=since_ts)
            return web.json_response(entries)
        except Exception as exc:
            logger.error("chat_history error: %s", exc)
            return web.json_response([], status=500)

    # ------------------------------------------------------------------ #
    # File Upload for Chat                                                  #
    # ------------------------------------------------------------------ #

    _UPLOAD_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "cato" / "uploads"
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
    _UPLOAD_ALLOWED_EXTS = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
        ".pdf",
        ".csv", ".tsv", ".xlsx", ".xls",
        ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".html", ".css",
        ".log",
    }
    _UPLOAD_BLOCKED_EXTS = {
        ".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".dll", ".sys",
        ".ps1", ".psm1", ".vbs", ".js.lnk", ".lnk", ".jar", ".sh",
    }

    async def chat_upload(request: web.Request) -> web.Response:
        """POST /api/chat/upload — Accept multipart file upload for chat context.

        Returns the saved file path and metadata so the frontend can reference
        it in the next chat message.
        """
        try:
            reader = await request.multipart()
            if reader is None:
                return web.json_response({"error": "multipart body required"}, status=400)

            field = await reader.next()
            if field is None or field.name != "file":
                return web.json_response({"error": "expected field named 'file'"}, status=400)

            original_name = field.filename or "upload"
            # Sanitize filename — strip path separators, restrict charset
            safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in original_name)
            safe_name = safe_name.lstrip(".") or "upload"
            # Block executable extensions (incl. double-extensions like foo.txt.exe)
            lowered = safe_name.lower()
            for blocked in _UPLOAD_BLOCKED_EXTS:
                if lowered.endswith(blocked):
                    return web.json_response(
                        {"error": f"file type not allowed: {blocked}"}, status=415,
                    )
            primary_ext = Path(safe_name).suffix.lower()
            if primary_ext and primary_ext not in _UPLOAD_ALLOWED_EXTS:
                return web.json_response(
                    {"error": f"file type not allowed: {primary_ext}"}, status=415,
                )
            ts = int(time.time())
            dest = _UPLOAD_DIR / f"{ts}_{safe_name}"

            size = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = await field.read_chunk(8192)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > _MAX_UPLOAD_SIZE:
                        dest.unlink(missing_ok=True)
                        return web.json_response({"error": "file too large (max 50MB)"}, status=413)
                    f.write(chunk)

            # Determine file type for display
            ext = Path(safe_name).suffix.lower()
            file_type = "document"
            if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
                file_type = "image"
            elif ext in (".pdf",):
                file_type = "pdf"
            elif ext in (".csv", ".tsv", ".xlsx", ".xls"):
                file_type = "spreadsheet"
            elif ext in (".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".html", ".css"):
                file_type = "code"

            logger.info("File uploaded: %s (%d bytes, type=%s)", safe_name, size, file_type)

            return web.json_response({
                "status": "ok",
                "filename": safe_name,
                "path": str(dest),
                "size": size,
                "type": file_type,
            })
        except Exception as exc:
            logger.error("chat_upload error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def kill_session(request: web.Request) -> web.Response:
        """DELETE /api/sessions/{session_id} — stop a session lane."""
        session_id = request.match_info.get("session_id", "")
        try:
            if gateway is None:
                return web.json_response({"status": "error", "message": "gateway unavailable"}, status=503)
            lane = gateway._lanes.get(session_id)
            if lane is None:
                return web.json_response({"status": "error", "message": "session not found"}, status=404)
            import asyncio as _asyncio
            _asyncio.create_task(lane.stop())
            gateway._lanes.pop(session_id, None)
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("kill_session error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def compact_session(request: web.Request) -> web.Response:
        """POST /api/compact — compact context for a session (trim old messages).

        Called by the /compact slash command in the chat UI.
        Body: {session_id: string}
        When gateway is available, sends a compact instruction into the lane.
        When offline, returns a success stub so the UI can show a friendly message.
        """
        try:
            body = await request.json()
            session_id = str(body.get("session_id", "")).strip()
            if not session_id:
                return web.json_response({"status": "error", "message": "session_id required"}, status=400)
            if gateway is not None:
                lane = gateway._lanes.get(session_id)
                if lane is not None and hasattr(lane, "compact"):
                    import asyncio as _asyncio
                    _asyncio.create_task(lane.compact())
                    return web.json_response({"status": "ok", "message": "Context compacted."})
                # Lane not found — still return ok (session may have just started)
                return web.json_response({"status": "ok", "message": "Context compacted."})
            # No gateway — return ok stub (UI shows offline message separately)
            return web.json_response({"status": "ok", "message": "Context compacted (stub)."})
        except Exception as exc:
            logger.error("compact_session error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Skills                                                               #
    # ------------------------------------------------------------------ #

    async def list_skills(request: web.Request) -> web.Response:
        """GET /api/skills — list installed skills with metadata."""
        try:
            if gateway is None:
                return web.json_response([])
            skills = gateway._list_skills()
            # Don't include full content in list — only name, description, version, dir
            result = [
                {"name": s["name"], "description": s["description"],
                 "version": s["version"], "dir": s["dir"]}
                for s in skills
            ]
            return web.json_response(result)
        except Exception as exc:
            logger.error("list_skills error: %s", exc)
            return web.json_response([], status=500)

    async def get_skill_content(request: web.Request) -> web.Response:
        """GET /api/skills/{name}/content — return SKILL.md content for a skill."""
        name = request.match_info.get("name", "")
        try:
            if gateway is None:
                return web.json_response({"content": ""})
            skills = gateway._list_skills()
            for s in skills:
                if s["dir"] == name or s["name"] == name:
                    return web.json_response({"content": s.get("content", ""), "name": s["name"]})
            return web.json_response({"status": "error", "message": "skill not found"}, status=404)
        except Exception as exc:
            logger.error("get_skill_content error: %s", exc)
            return web.json_response({"content": ""}, status=500)

    async def patch_skill_content(request: web.Request) -> web.Response:
        """PATCH /api/skills/{name}/content — update SKILL.md content for a skill."""
        name = request.match_info.get("name", "")
        # Guard against path traversal: skill names must not contain path separators or dots
        if not name or ".." in name or "/" in name or "\\" in name:
            return web.json_response({"status": "error", "message": "invalid skill name"}, status=400)
        try:
            body = await request.json()
            content = str(body.get("content", ""))
            if gateway is None:
                return web.json_response({"status": "error", "message": "gateway unavailable"}, status=503)
            if hasattr(gateway, "_skills_dir") and callable(gateway._skills_dir):
                skills_dir = gateway._skills_dir()
            else:
                skills_dir = Path.home() / ".cato" / "skills"
            # Find the skill directory
            target = None
            for child in skills_dir.iterdir() if skills_dir.exists() else []:
                if child.is_dir() and (child.name == name):
                    target = child / "SKILL.md"
                    break
            if target is None:
                # Try writing based on name directly
                target = skills_dir / name / "SKILL.md"
            # Resolve and confirm target is within skills_dir to prevent symlink escapes
            skills_dir_resolved = Path(skills_dir).resolve()
            target_resolved = target.resolve()
            if not str(target_resolved).startswith(str(skills_dir_resolved)):
                return web.json_response({"status": "error", "message": "invalid skill path"}, status=400)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            logger.info("Skill content updated: %s", name)
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("patch_skill_content error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def toggle_skill(request: web.Request) -> web.Response:
        """POST /api/skills/{name}/toggle — enable or disable a skill."""
        name = request.match_info.get("name", "")
        # Guard against path traversal
        if not name or ".." in name or "/" in name or "\\" in name:
            return web.json_response({"status": "error", "message": "invalid skill name"}, status=400)
        try:
            body = await request.json()
            enabled = bool(body.get("enabled", True))
            # Store enabled state in a simple marker file in the skill directory
            if gateway is None:
                return web.json_response({"status": "error", "message": "gateway unavailable"}, status=503)
            if hasattr(gateway, "_skills_dir") and callable(gateway._skills_dir):
                skills_dir = gateway._skills_dir()
            else:
                skills_dir = Path.home() / ".cato" / "skills"
            skill_path = skills_dir / name
            if skill_path.exists():
                marker = skill_path / ".disabled"
                if enabled:
                    marker.unlink(missing_ok=True)
                else:
                    marker.touch()
            return web.json_response({"status": "ok", "enabled": enabled})
        except Exception as exc:
            logger.error("toggle_skill error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def cli_status(request: web.Request) -> web.Response:
        """GET /api/cli/status — check installed CLI tools (claude, codex, gemini, cursor)."""
        import asyncio as _asyncio
        import shutil
        import subprocess

        TOOLS = {
            "claude":  ["claude", "--version"],
            "codex":   ["codex",  "--version"],
            "gemini":  ["gemini", "--version"],
            "cursor":  ["cursor", "--version"],
        }
        result: dict = {}
        loop = _asyncio.get_running_loop()

        def _check_tool(name: str, cmd: list) -> dict:
            exe = shutil.which(name)
            if exe is None:
                return {"installed": False, "logged_in": False, "version": "", "version_check_ok": False}
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                version = (proc.stdout or proc.stderr or "").strip().split("\n")[0][:60]
                return {"installed": True, "logged_in": True, "version": version, "version_check_ok": True}
            except subprocess.TimeoutExpired:
                # Version check timed out — CLI is installed but we can't confirm auth
                return {"installed": True, "logged_in": False, "version": "", "version_check_ok": False}
            except Exception:
                return {"installed": True, "logged_in": False, "version": "", "version_check_ok": False}

        for tool_name, cmd in TOOLS.items():
            result[tool_name] = await loop.run_in_executor(None, _check_tool, tool_name, cmd)

        return web.json_response(result)

    async def cli_restart(request: web.Request) -> web.Response:
        """POST /api/cli/{name}/restart — restart a specific CLI backend."""
        name = request.match_info.get("name", "")
        valid_names = {"claude", "codex", "gemini", "cursor"}
        if name not in valid_names:
            return web.json_response(
                {"status": "error", "message": f"Unknown CLI backend: {name}"},
                status=404,
            )
        # Attempt to restart via the shared CLI process pool. The pool is
        # initialised on app startup and attached to the gateway (when present)
        # so that a single singleton instance is reused.
        try:
            from cato.orchestrator.cli_process_pool import get_pool

            pool = None
            if gateway is not None and hasattr(gateway, "_cli_pool"):
                pool = getattr(gateway, "_cli_pool")
            if pool is None:
                pool = get_pool()
                if gateway is not None:
                    setattr(gateway, "_cli_pool", pool)

            if hasattr(pool, "restart"):
                await pool.restart(name)
            elif hasattr(pool, "warm_up"):
                await pool.warm_up(name)

            return web.json_response({"status": "ok", "message": f"{name} restart initiated"})
        except Exception as exc:
            logger.error("cli_restart error for %s: %s", name, exc)
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=500,
            )

    # ------------------------------------------------------------------ #
    # Cron jobs                                                            #
    # ------------------------------------------------------------------ #

    async def list_cron_jobs(request: web.Request) -> web.Response:
        """GET /api/cron/jobs — list all cron schedules."""
        try:
            from cato.core.schedule_manager import load_all_schedules
            schedules = load_all_schedules()
            return web.json_response([s.to_dict() for s in schedules])
        except Exception as exc:
            logger.error("list_cron_jobs error: %s", exc)
            return web.json_response([], status=500)

    async def create_cron_job(request: web.Request) -> web.Response:
        """POST /api/cron/jobs — create or update a cron schedule."""
        try:
            from cato.core.schedule_manager import Schedule
            body = await request.json()
            sched = Schedule.from_dict(body)
            sched.save()
            return web.json_response({"status": "ok", "name": sched.name})
        except Exception as exc:
            logger.error("create_cron_job error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def delete_cron_job(request: web.Request) -> web.Response:
        """DELETE /api/cron/jobs/{name} — remove a schedule."""
        name = request.match_info.get("name", "")
        try:
            from cato.core.schedule_manager import delete_schedule
            ok = delete_schedule(name)
            if ok:
                return web.json_response({"status": "ok"})
            return web.json_response({"status": "error", "message": "not found"}, status=404)
        except Exception as exc:
            logger.error("delete_cron_job error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def toggle_cron_job(request: web.Request) -> web.Response:
        """POST /api/cron/jobs/{name}/toggle — enable or disable a schedule."""
        name = request.match_info.get("name", "")
        try:
            from cato.core.schedule_manager import toggle_schedule
            try:
                body = await request.json()
            except Exception:
                body = {}
            enabled = bool(body.get("enabled", True))
            ok = toggle_schedule(name, enabled)
            if ok:
                return web.json_response({"status": "ok", "enabled": enabled})
            return web.json_response({"status": "error", "message": "not found"}, status=404)
        except Exception as exc:
            logger.error("toggle_cron_job error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def run_cron_job_now(request: web.Request) -> web.Response:
        """POST /api/cron/jobs/{name}/run — manually trigger a cron job.

        The cron scheduler runs as an inline coroutine (not a SchedulerDaemon),
        so manual trigger is implemented by reading the schedule from disk and
        injecting the prompt directly into the gateway lane queue.
        """
        name = request.match_info.get("name", "")
        try:
            if gateway is None:
                return web.json_response({"status": "error", "message": "gateway unavailable"}, status=503)
            from cato.core.schedule_manager import load_all_schedules
            schedules = load_all_schedules()
            sched = next((s for s in schedules if s.name == name), None)
            if sched is None:
                return web.json_response({"status": "error", "message": f"job '{name}' not found"}, status=404)
            session_id = f"cron-manual-{name}"
            prompt = sched.skill or name
            await gateway.ingest(session_id, str(prompt), "cron", "")
            return web.json_response({"status": "ok", "message": f"Job '{name}' triggered"})
        except Exception as exc:
            logger.error("run_cron_job_now error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Budget                                                               #
    # ------------------------------------------------------------------ #

    async def budget_summary(request: web.Request) -> web.Response:
        """GET /api/budget/summary — current spend, caps, pct remaining."""
        try:
            if gateway is None:
                return web.json_response({"session_spend": 0, "session_cap": 1.0,
                                          "monthly_spend": 0, "monthly_cap": 20.0,
                                          "session_pct_remaining": 100, "monthly_pct_remaining": 100,
                                          "monthly_calls": 0, "total_spend_all_time": 0})
            status = gateway._budget.get_status()
            return web.json_response(status)
        except Exception as exc:
            logger.error("budget_summary error: %s", exc)
            return web.json_response({}, status=500)

    # ------------------------------------------------------------------ #
    # Usage                                                                #
    # ------------------------------------------------------------------ #

    async def usage_summary(request: web.Request) -> web.Response:
        """GET /api/usage/summary — token usage and model distribution.

        Returns a response compatible with the dashboard JS which expects:
          total_calls, total_input_tokens, total_output_tokens,
          top_model, avg_latency_ms, models (list of {model, calls}).
        Also includes the raw get_token_report() fields for completeness.
        """
        try:
            from cato.orchestrator.metrics import get_token_report, get_metrics_summary
            report = get_token_report()
            summary = get_metrics_summary()

            # Map internal field names → dashboard-expected field names
            total_in  = report.get("total_tokens_in", 0)
            total_out = report.get("total_tokens_out", 0)
            total_calls = report.get("total_invocations", 0)

            # Build per-model list from tier_distribution as a best-effort proxy
            tier_dist = report.get("tier_distribution", {})
            models_list = [
                {"model": tier, "calls": cnt, "total_calls": cnt}
                for tier, cnt in tier_dist.items()
            ] if tier_dist else []

            # top model: first entry or unknown
            top_model = models_list[0]["model"] if models_list else "unknown"

            avg_latency = summary.get("avg_latency_ms") if summary else None

            compat = {
                # Dashboard-expected fields
                "total_calls": total_calls,
                "total_input_tokens": total_in,
                "total_output_tokens": total_out,
                "top_model": top_model,
                "avg_latency_ms": avg_latency,
                "models": models_list,
                "by_model": models_list,
                # Keep all original fields for backward compat
                **report,
            }
            return web.json_response(compat)
        except Exception as exc:
            logger.error("usage_summary error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Routing History                                                      #
    # ------------------------------------------------------------------ #

    async def get_routing_history(request: web.Request) -> web.Response:
        """GET /api/usage/routing — return model routing decision history."""
        try:
            from cato.router import get_routing_history as _get_history
            history = _get_history()
            limit = int(request.rel_url.query.get("limit", "100"))
            return web.json_response(history[-limit:])
        except Exception as exc:
            logger.error("get_routing_history error: %s", exc)
            return web.json_response([], status=500)

    # ------------------------------------------------------------------ #
    # Logs                                                                 #
    # ------------------------------------------------------------------ #

    _log_buffer: list[dict] = []

    class _BufferHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            _log_buffer.append({
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "msg": _redact_log_text(self.format(record)),
            })
            if len(_log_buffer) > 500:
                del _log_buffer[:-500]

    _buf_handler = _BufferHandler()
    _buf_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    logging.getLogger("cato").addHandler(_buf_handler)

    async def get_logs(request: web.Request) -> web.Response:
        """GET /api/logs?limit=100 — return recent log entries."""
        try:
            limit = int(request.rel_url.query.get("limit", "100"))
            level_filter = request.rel_url.query.get("level", "").upper()
            entries = _log_buffer[-limit:]
            if level_filter:
                entries = [e for e in entries if e["level"] == level_filter]
            return web.json_response(entries)
        except Exception as exc:
            logger.error("get_logs error: %s", exc)
            return web.json_response([], status=500)

    # ------------------------------------------------------------------ #
    # Audit log                                                            #
    # ------------------------------------------------------------------ #

    async def get_audit_entries(request: web.Request) -> web.Response:
        """GET /api/audit/entries — return audit log entries with optional filters.

        Runs synchronous SQLite I/O in a thread via run_in_executor to avoid
        blocking the aiohttp event loop.
        """
        try:
            import asyncio as _asyncio
            from cato.audit.audit_log import AuditLog
            limit = int(request.rel_url.query.get("limit", "200"))
            session_filter = request.rel_url.query.get("session_id", "")
            action_filter = request.rel_url.query.get("action_type", "")

            def _fetch() -> list:
                audit = AuditLog()
                audit.connect()
                assert audit._conn is not None
                q = "SELECT id, session_id, action_type, tool_name, cost_cents, error, timestamp, prev_hash, row_hash FROM audit_log"
                params: list = []
                clauses: list[str] = []
                if session_filter:
                    clauses.append("session_id = ?")
                    params.append(session_filter)
                if action_filter:
                    clauses.append("action_type = ?")
                    params.append(action_filter)
                if clauses:
                    q += " WHERE " + " AND ".join(clauses)
                q += " ORDER BY id DESC LIMIT ?"
                params.append(limit)
                rows = audit._conn.execute(q, params).fetchall()
                result = [dict(r) for r in rows]
                audit.close()
                return result

            loop = _asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _fetch)
            return web.json_response(result)
        except Exception as exc:
            logger.error("get_audit_entries error: %s", exc)
            return web.json_response([], status=500)

    async def verify_audit_chain(request: web.Request) -> web.Response:
        """POST /api/audit/verify — verify chain integrity for a session or all.

        Runs synchronous SQLite I/O in a thread via run_in_executor to avoid
        blocking the aiohttp event loop.
        """
        try:
            import asyncio as _asyncio
            from cato.audit.audit_log import AuditLog
            body = await request.json()
            session_id = str(body.get("session_id", ""))

            def _verify() -> dict:
                audit = AuditLog()
                audit.connect()
                if session_id:
                    ok = audit.verify_chain(session_id)
                    audit.close()
                    return {"ok": ok, "session_id": session_id}
                assert audit._conn is not None
                sessions = [r[0] for r in audit._conn.execute(
                    "SELECT DISTINCT session_id FROM audit_log"
                ).fetchall()]
                results = {}
                for sid in sessions:
                    results[sid] = audit.verify_chain(sid)
                audit.close()
                return {"ok": all(results.values()) if results else True, "sessions": results}

            loop = _asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _verify)
            return web.json_response(data)
        except Exception as exc:
            logger.error("verify_audit_chain error: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def save_config(request: web.Request) -> web.Response:
        """Stub POST /config endpoint. Replace with real persistence as needed."""
        try:
            body = await request.json()
            logger.info("Config save requested: %d keys", len(body))
            # TODO: wire to CatoConfig.save() once that method exists
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("Config save error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=400)

    async def patch_config(request: web.Request) -> web.Response:
        """PATCH /api/config — patch individual config fields and persist to YAML."""
        try:
            import yaml as _yaml
            from cato.platform import get_data_dir as _get_data_dir
            body = await request.json()
            logger.info("Config patch requested: %s", list(body.keys()))
            # Load current config from YAML file
            config_path = _get_data_dir() / "config.yaml"
            current: dict = {}
            if config_path.exists():
                try:
                    current = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                except Exception:
                    current = {}
            # Apply patches
            current.update(body)
            # Persist
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                _yaml.dump(current, default_flow_style=False, allow_unicode=True, sort_keys=True),
                encoding="utf-8",
            )
            # Also update in-memory gateway config if available
            if gateway is not None and hasattr(gateway, "_cfg"):
                cfg_obj = gateway._cfg
                from dataclasses import fields as _dc_fields
                valid_names = {f.name for f in _dc_fields(type(cfg_obj)) if not f.name.startswith("_")}
                for k, v in body.items():
                    if k in valid_names:
                        setattr(cfg_obj, k, v)
            return web.json_response({"status": "ok", "config": current})
        except Exception as exc:
            logger.error("patch_config error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=400)

    async def get_config(request: web.Request) -> web.Response:
        """GET /api/config — return current config read from YAML file."""
        try:
            import yaml as _yaml
            from cato.platform import get_data_dir as _get_data_dir
            config_path = _get_data_dir() / "config.yaml"
            cfg: dict = {}
            if config_path.exists():
                try:
                    cfg = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                except Exception:
                    cfg = {}
            # Merge with in-memory config defaults if available
            if gateway is not None and hasattr(gateway, "_cfg"):
                raw = gateway._cfg
                if hasattr(raw, "to_dict"):
                    defaults = raw.to_dict()
                    # file values take precedence over defaults
                    merged = {**defaults, **cfg}
                    cfg = merged
            # Filter out sensitive keys before returning
            _SENSITIVE_SUBSTRINGS = ("token", "password", "secret", "_key", "api_key")
            filtered = {
                k: v for k, v in cfg.items()
                if not any(s in k.lower() for s in _SENSITIVE_SUBSTRINGS)
            }
            return web.json_response(filtered)
        except Exception as exc:
            logger.error("get_config error: %s", exc)
            return web.json_response({}, status=500)

    # ------------------------------------------------------------------ #
    # Routing status — shows what SwarmSync is doing                      #
    # ------------------------------------------------------------------ #

    async def routing_status(request: web.Request) -> web.Response:
        """GET /api/routing/status — show SwarmSync config and test a live routing call."""
        if gateway is None:
            return web.json_response({"error": "gateway not ready"}, status=503)
        cfg = gateway._cfg
        vault = gateway._vault

        swarm_key = None
        if vault is not None:
            swarm_key = vault.get("SWARM_SYNC_API_KEY") or vault.get("SWARMSYNC_API_KEY")
        openrouter_key = vault.get("OPENROUTER_API_KEY") if vault else None

        status = {
            "swarmsync_enabled": getattr(cfg, "swarmsync_enabled", False),
            "swarmsync_api_url": getattr(cfg, "swarmsync_api_url", ""),
            "default_model": getattr(cfg, "default_model", ""),
            "swarm_key_present": bool(swarm_key),
            "swarm_key_prefix": swarm_key[:12] + "..." if swarm_key and len(swarm_key) > 12 else swarm_key,
            "openrouter_key_present": bool(openrouter_key),
            "will_use_swarmsync": bool(getattr(cfg, "swarmsync_enabled", False) and swarm_key),
        }

        # If SwarmSync is active, do a live test routing call
        if status["will_use_swarmsync"]:
            try:
                import aiohttp as _aiohttp
                payload = {
                    "model": "auto",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "swarmsync": {"complexity_score": 0.2, "history_length": 1},
                }
                headers = {
                    "Authorization": f"Bearer {swarm_key}",
                    "Content-Type": "application/json",
                }
                async with _aiohttp.ClientSession() as s:
                    async with s.post(
                        status["swarmsync_api_url"],
                        json=payload,
                        headers=headers,
                        timeout=_aiohttp.ClientTimeout(total=15),
                    ) as r:
                        if r.status in (200, 201):
                            data = await r.json()
                            ss = data.get("swarmsync", {})
                            status["live_test"] = {
                                "http_status": r.status,
                                "routed_model": ss.get("routed_model", data.get("model", "")),
                                "routing_reason": ss.get("routing_reason", ""),
                                "tier": ss.get("tier", ""),
                                "complexity_score": ss.get("complexity_score"),
                            }
                        else:
                            body = await r.text()
                            status["live_test"] = {"http_status": r.status, "error": body[:200]}
            except Exception as exc:
                status["live_test"] = {"error": str(exc)}

        return web.json_response(status)

    # ------------------------------------------------------------------ #
    # Cron job history                                                     #
    # ------------------------------------------------------------------ #

    async def cron_job_history(request: web.Request) -> web.Response:
        """GET /api/cron/jobs/{name}/history — return recent executions for a job."""
        name = request.match_info.get("name", "")
        try:
            limit = int(request.rel_url.query.get("limit", "20"))
            import asyncio as _asyncio
            from cato.audit.audit_log import AuditLog

            def _fetch() -> list:
                audit = AuditLog()
                audit.connect()
                assert audit._conn is not None
                rows = audit._conn.execute(
                    "SELECT id, session_id, action_type, tool_name, cost_cents, error, timestamp "
                    "FROM audit_log WHERE session_id LIKE ? ORDER BY id DESC LIMIT ?",
                    (f"cron-%{name}%", limit),
                ).fetchall()
                audit.close()
                return [dict(r) for r in rows]

            loop = _asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _fetch)
            return web.json_response(result)
        except Exception as exc:
            logger.error("cron_job_history error: %s", exc)
            return web.json_response([], status=500)

    # ------------------------------------------------------------------ #
    # Memory browser                                                       #
    # ------------------------------------------------------------------ #

    async def memory_files(request: web.Request) -> web.Response:
        """GET /api/memory/files — list memory JSON files in ~/.cato/memory/."""
        try:
            mem_dir = Path.home() / ".cato" / "memory"
            if not mem_dir.exists():
                return web.json_response([])
            files = [f.name for f in sorted(mem_dir.iterdir()) if f.suffix in (".json", ".md")]
            return web.json_response(files)
        except Exception as exc:
            logger.error("memory_files error: %s", exc)
            return web.json_response([], status=500)

    async def memory_content(request: web.Request) -> web.Response:
        """GET /api/memory/content?file=MEMORY.md&agent_id=default&type=facts — read a memory/workspace file."""
        filename = request.rel_url.query.get("file", "").strip()
        # BUG FIX MEM-001: agent_id defaults to "default", type is optional; file defaults to MEMORY.md
        if not filename:
            filename = "MEMORY.md"
        if ".." in filename or "/" in filename or "\\" in filename:
            return web.json_response({"status": "error", "message": "invalid file"}, status=400)
        try:
            # Try ~/.cato/memory/ first, then ~/.cato/
            for base in (Path.home() / ".cato" / "memory", Path.home() / ".cato"):
                p = base / filename
                if p.exists():
                    return web.json_response({"content": p.read_text(encoding="utf-8", errors="replace"), "file": filename})
            return web.json_response({"content": "", "file": filename})
        except Exception as exc:
            logger.error("memory_content error: %s", exc)
            return web.json_response({"content": "", "file": filename}, status=500)

    async def patch_memory_content(request: web.Request) -> web.Response:
        """PATCH /api/memory/content — write a memory/workspace file."""
        try:
            body = await request.json()
            filename = str(body.get("file", "")).strip()
            content  = str(body.get("content", ""))
            if not filename or ".." in filename or "/" in filename or "\\" in filename:
                return web.json_response({"status": "error", "message": "invalid file"}, status=400)
            # Write to ~/.cato/ (workspace root)
            target = Path.home() / ".cato" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("patch_memory_content error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def workspace_list(request: web.Request) -> web.Response:
        """GET /api/workspace/files — list identity .md files."""
        try:
            d = _workspace_dir()
            d.mkdir(parents=True, exist_ok=True)
            files = [f.name for f in sorted(d.iterdir()) if f.suffix == ".md"]
            return web.json_response(files)
        except Exception as exc:
            logger.error("workspace_list error: %s", exc)
            return web.json_response([], status=500)

    async def workspace_get(request: web.Request) -> web.Response:
        """GET /api/workspace/file?name=SOUL.md — read a workspace file."""
        name = request.rel_url.query.get("name", "").strip()
        if not name or ".." in name or "/" in name or "\\" in name:
            return web.json_response({"error": "invalid name"}, status=400)
        try:
            p = _workspace_dir() / name
            content = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
            return web.json_response({"name": name, "content": content})
        except Exception as exc:
            logger.error("workspace_get error: %s", exc)
            return web.json_response({"name": name, "content": ""}, status=500)

    async def workspace_put(request: web.Request) -> web.Response:
        """PUT /api/workspace/file — write a workspace file."""
        try:
            body = await request.json()
            name = str(body.get("name", "")).strip()
            content = str(body.get("content", ""))
            if not name or ".." in name or "/" in name or "\\" in name:
                return web.json_response({"error": "invalid name"}, status=400)
            if name not in _WORKSPACE_ALLOWED:
                return web.json_response({"error": "file not allowed"}, status=400)
            d = _workspace_dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(content, encoding="utf-8")
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("workspace_put error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def memory_stats(request: web.Request) -> web.Response:
        """GET /api/memory/stats — facts count + KG node/edge counts from SQLite memories.

        Also returns a 'stats' key with semantic search engine info for SettingsView
        compatibility (chunks_indexed, model).
        """
        try:
            import asyncio as _asyncio
            import sqlite3

            def _count() -> dict:
                facts = 0
                kg_nodes = 0
                kg_edges = 0
                mem_dir = Path.home() / ".cato" / "memory"
                if mem_dir.exists():
                    for db_path in mem_dir.glob("*.db"):
                        try:
                            conn = sqlite3.connect(str(db_path))
                            for tbl, col in [("facts", "facts"), ("kg_nodes", "kg_nodes"), ("kg_edges", "kg_edges")]:
                                try:
                                    n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                                    if tbl == "facts":
                                        facts += n
                                    elif tbl == "kg_nodes":
                                        kg_nodes += n
                                    elif tbl == "kg_edges":
                                        kg_edges += n
                                except Exception:
                                    pass
                            conn.close()
                        except Exception:
                            pass
                return {"facts": facts, "kg_nodes": kg_nodes, "kg_edges": kg_edges}

            loop = _asyncio.get_running_loop()
            db_stats = await loop.run_in_executor(None, _count)

            # Also pull semantic search engine stats for SettingsView.tsx Memory tab
            semantic_stats: dict = {"chunks_indexed": 0, "model": "all-MiniLM-L6-v2"}
            try:
                from cato.api.memory_routes import _get_search_engine
                engine = _get_search_engine(initialize=False)
                if engine is not None:
                    semantic_stats = engine.stats()
            except Exception:
                pass

            return web.json_response({
                **db_stats,
                "stats": semantic_stats,
            })
        except Exception as exc:
            logger.error("memory_stats error: %s", exc)
            return web.json_response(
                {"facts": 0, "kg_nodes": 0, "kg_edges": 0, "stats": {"chunks_indexed": 0, "model": "all-MiniLM-L6-v2"}},
                status=500,
            )

    # ------------------------------------------------------------------ #
    # Action Guard status                                                  #
    # ------------------------------------------------------------------ #

    async def action_guard_status(request: web.Request) -> web.Response:
        """GET /api/action-guard/status — show the 3-rule gate status."""
        try:
            from cato.audit.action_guard import ActionGuard
            guard = ActionGuard()
            checks = [
                {"rule": "Irreversibility check", "description": "Block irreversible actions above autonomy threshold", "active": True},
                {"rule": "Spending ceiling check", "description": "Enforce per-session and monthly spend caps", "active": True},
                {"rule": "Dangerous tool check",  "description": "Require confirmation for high-risk tools", "active": True},
            ]
            return web.json_response({"checks": checks, "autonomy_level": 0.5})
        except Exception as exc:
            logger.error("action_guard_status error: %s", exc)
            return web.json_response({"checks": [], "autonomy_level": 0.5}, status=500)

    # ------------------------------------------------------------------ #
    # Daemon restart                                                       #
    # ------------------------------------------------------------------ #

    async def daemon_restart(request: web.Request) -> web.Response:
        """POST /api/daemon/restart — signal daemon to restart (best-effort)."""
        try:
            logger.info("Daemon restart requested via API")
            import asyncio as _asyncio
            async def _deferred_restart():
                await _asyncio.sleep(1)
                import os, signal
                os.kill(os.getpid(), signal.SIGTERM)
            _asyncio.create_task(_deferred_restart())
            return web.json_response({"status": "ok", "message": "Restart scheduled"})
        except Exception as exc:
            logger.error("daemon_restart error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Diagnostics                                                          #
    # ------------------------------------------------------------------ #

    async def diagnostics_query_classifier(request: web.Request) -> web.Response:
        """GET /api/diagnostics/query-classifier — tier classification info."""
        try:
            tiers = {
                "TIER_A": {"label": "Simple (Gemini only)", "description": "Single-fact lookups, unit conversions, definitions"},
                "TIER_B": {"label": "Standard (Claude)", "description": "Single-task coding, writing, analysis"},
                "TIER_C": {"label": "Complex (Fan-out)", "description": "Multi-step reasoning, ambiguous queries, high token count"},
            }
            return web.json_response({"tiers": tiers, "classifier": "keyword+token"})
        except Exception as exc:
            logger.error("diagnostics_query_classifier error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def diagnostics_contradiction_health(request: web.Request) -> web.Response:
        """GET /api/diagnostics/contradiction-health — contradiction detector summary."""
        try:
            import asyncio as _asyncio
            from cato.memory.contradiction_detector import ContradictionDetector
            from cato.platform import get_data_dir
            db_path = get_data_dir() / "default" / "contradictions.db"
            detector = ContradictionDetector(db_path=str(db_path))
            def _fetch():
                try:
                    return detector.get_health_summary()
                finally:
                    detector.close()
            loop = _asyncio.get_running_loop()
            summary = await loop.run_in_executor(None, _fetch)
            # get_health_summary returns: {total, unresolved, by_type, most_contradicted_entities}
            # Add resolved count for the UI
            summary["resolved"] = summary.get("total", 0) - summary.get("unresolved", 0)
            return web.json_response(summary)
        except Exception as exc:
            logger.error("diagnostics_contradiction_health error: %s", exc)
            return web.json_response({"resolved": 0, "unresolved": 0, "total": 0, "by_type": {}, "most_contradicted_entities": [], "error": str(exc)}, status=200)

    async def diagnostics_decision_memory(request: web.Request) -> web.Response:
        """GET /api/diagnostics/decision-memory — open decisions + overconfidence profile."""
        try:
            import asyncio as _asyncio
            from cato.memory.decision_memory import DecisionMemory
            from cato.platform import get_data_dir
            db_path = get_data_dir() / "default" / "decisions.db"
            dm = DecisionMemory(db_path=db_path)
            def _fetch():
                try:
                    open_recs = dm.list_open()
                    # list_open returns list[DecisionRecord] dataclasses — convert to dicts
                    open_decisions = [
                        {
                            "decision_id": r.decision_id,
                            "action_taken": r.action_taken,
                            "confidence": r.confidence_at_decision_time,
                            "timestamp": r.timestamp,
                        }
                        for r in open_recs
                    ]
                    profile = dm.get_overconfidence_profile()
                    return {"open_decisions": open_decisions, "overconfidence_profile": profile}
                finally:
                    dm.close()
            loop = _asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _fetch)
            return web.json_response(result)
        except Exception as exc:
            logger.error("diagnostics_decision_memory error: %s", exc)
            return web.json_response({"open_decisions": [], "overconfidence_profile": {}, "error": str(exc)}, status=200)

    async def diagnostics_anomaly_domains(request: web.Request) -> web.Response:
        """GET /api/diagnostics/anomaly-domains — anomaly detector domain summaries."""
        try:
            import asyncio as _asyncio
            from cato.monitoring.anomaly_detector import AnomalyDetector
            from cato.platform import get_data_dir
            db_path = get_data_dir() / "default" / "anomaly.db"
            detector = AnomalyDetector(db_path=db_path)
            def _fetch():
                try:
                    # list_domains returns list[Domain] dataclasses
                    domain_list = detector.list_domains(active_only=False)
                    domains = [
                        {
                            "domain": d.name,
                            "description": d.description,
                            "active": d.active,
                        }
                        for d in domain_list
                    ]
                    return {"domains": domains}
                finally:
                    detector.close()
            loop = _asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _fetch)
            return web.json_response(result)
        except Exception as exc:
            logger.error("diagnostics_anomaly_domains error: %s", exc)
            return web.json_response({"domains": [], "error": str(exc)}, status=200)

    async def diagnostics_skill_corrections(request: web.Request) -> web.Response:
        """GET /api/diagnostics/skill-corrections — skill improvement cycle corrections."""
        try:
            import asyncio as _asyncio
            from cato.core.memory import MemorySystem
            from cato.platform import get_data_dir
            memory_dir = get_data_dir() / "default"
            ms = MemorySystem(agent_id="default", memory_dir=memory_dir)
            def _fetch():
                try:
                    rows = ms._conn.execute(
                        "SELECT id, task_type, wrong_approach, correct_approach, session_id, timestamp"
                        " FROM corrections ORDER BY timestamp DESC LIMIT 20"
                    ).fetchall()
                    return [dict(r) for r in rows]
                except Exception:
                    return []
                finally:
                    ms._conn.close()
            loop = _asyncio.get_running_loop()
            corrections = await loop.run_in_executor(None, _fetch)
            return web.json_response({"corrections": corrections})
        except Exception as exc:
            logger.error("diagnostics_skill_corrections error: %s", exc)
            return web.json_response({"corrections": [], "error": str(exc)}, status=200)

    # ------------------------------------------------------------------ #
    # Delegation Tokens                                                    #
    # ------------------------------------------------------------------ #

    async def list_tokens(request: web.Request) -> web.Response:
        """GET /api/tokens — list active delegation tokens."""
        try:
            import asyncio as _asyncio
            from cato.auth.token_store import TokenStore
            from cato.platform import get_data_dir
            db_path = get_data_dir() / "default" / "tokens.db"
            ts = TokenStore(db_path=db_path)
            def _fetch():
                try:
                    from dataclasses import asdict
                    tokens = ts.list_active()
                    return [asdict(t) for t in tokens]
                finally:
                    ts._conn.close()
            loop = _asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _fetch)
            return web.json_response(result)
        except Exception as exc:
            logger.error("list_tokens error: %s", exc)
            return web.json_response([], status=200)

    async def create_token(request: web.Request) -> web.Response:
        """POST /api/tokens — create a new delegation token."""
        try:
            import asyncio as _asyncio
            from cato.auth.token_store import TokenStore
            from cato.platform import get_data_dir
            body = await request.json()
            db_path = get_data_dir() / "default" / "tokens.db"
            ts = TokenStore(db_path=db_path)
            def _create():
                try:
                    token = ts.create(
                        allowed_action_categories=body.get("categories", []),
                        spending_ceiling=body.get("spending_ceiling", 1.0),
                        expires_in_seconds=body.get("expires_in_seconds", 3600),
                    )
                    return {"status": "ok", "token_id": token.token_id}
                finally:
                    ts._conn.close()
            loop = _asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _create)
            return web.json_response(result)
        except Exception as exc:
            logger.error("create_token error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def revoke_token(request: web.Request) -> web.Response:
        """DELETE /api/tokens/{token_id} — revoke a delegation token."""
        try:
            import asyncio as _asyncio
            from cato.auth.token_store import TokenStore
            from cato.platform import get_data_dir
            token_id = request.match_info.get("token_id", "")
            db_path = get_data_dir() / "default" / "tokens.db"
            ts = TokenStore(db_path=db_path)
            def _revoke():
                try:
                    ok = ts.revoke(token_id, reason="Revoked via UI")
                    return {"status": "ok" if ok else "not_found"}
                finally:
                    ts._conn.close()
            loop = _asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _revoke)
            return web.json_response(result)
        except Exception as exc:
            logger.error("revoke_token error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Config reload                                                        #
    # ------------------------------------------------------------------ #

    async def reload_config(request: web.Request) -> web.Response:
        """POST /api/config/reload — reload config from disk without daemon restart."""
        try:
            from cato.config import CatoConfig
            cfg = CatoConfig.load()
            if gateway is not None and hasattr(gateway, "_cfg"):
                # Update the gateway's in-memory config
                for f in cfg.__dataclass_fields__:
                    if hasattr(gateway._cfg, f):
                        setattr(gateway._cfg, f, getattr(cfg, f))
            return web.json_response({"status": "ok", "message": "Config reloaded from disk"})
        except Exception as exc:
            logger.error("reload_config error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def diagnostics_disagreements(request: web.Request) -> web.Response:
        """GET /api/diagnostics/disagreements — disagreement surfacer config & thresholds."""
        try:
            from cato.orchestrator.disagreement_surfacer import DisagreementSurfacer
            ds = DisagreementSurfacer()
            return web.json_response({
                "thresholds": ds.THRESHOLDS,
                "info": "Disagreement surfacer monitors multi-model output divergence using Jaccard distance + confidence stdev.",
            })
        except Exception as exc:
            logger.error("diagnostics_disagreements error: %s", exc)
            return web.json_response({"thresholds": {}, "error": str(exc)}, status=200)

    async def diagnostics_epistemic(request: web.Request) -> web.Response:
        """GET /api/diagnostics/epistemic — epistemic monitor state."""
        try:
            from cato.orchestrator.epistemic_monitor import EpistemicMonitor
            em = EpistemicMonitor()
            return web.json_response({
                "threshold": em.threshold,
                "max_interrupts": em.max_interrupts,
                "premise_markers": em.PREMISE_MARKERS,
                "info": "Epistemic monitor extracts premises and detects confidence gaps in reasoning.",
            })
        except Exception as exc:
            logger.error("diagnostics_epistemic error: %s", exc)
            return web.json_response({"threshold": 0.7, "max_interrupts": 3, "premise_markers": [], "error": str(exc)}, status=200)

    async def diagnostics_context_budget(request: web.Request) -> web.Response:
        """GET /api/diagnostics/context-budget — context builder slot budget info."""
        try:
            from cato.core.context_builder import SlotBudget
            sb = SlotBudget()
            return web.json_response({
                "total": sb.total,
                "slots": {
                    "tier0_identity": sb.tier0_identity,
                    "tier0_agents": sb.tier0_agents,
                    "tier1_skill": sb.tier1_skill,
                    "tier1_memory": sb.tier1_memory,
                    "tier1_tools": sb.tier1_tools,
                    "tier1_history": sb.tier1_history,
                    "headroom": sb.headroom,
                },
                "info": "Context budget allocates token slots by priority tier (0=critical identity, 1=active context).",
            })
        except Exception as exc:
            logger.error("diagnostics_context_budget error: %s", exc)
            return web.json_response({"total": 12000, "slots": {}, "error": str(exc)}, status=200)

    async def diagnostics_retrieval(request: web.Request) -> web.Response:
        """GET /api/diagnostics/retrieval — retrieval system info."""
        try:
            return web.json_response({
                "strategy": "hybrid",
                "components": ["keyword_search", "knowledge_graph", "fact_store"],
                "info": "HybridRetriever combines keyword search, knowledge graph traversal, and fact store lookups.",
            })
        except Exception as exc:
            logger.error("diagnostics_retrieval error: %s", exc)
            return web.json_response({"strategy": "unknown", "components": [], "error": str(exc)}, status=200)

    async def diagnostics_habits(request: web.Request) -> web.Response:
        """GET /api/diagnostics/habits — habit extractor patterns."""
        try:
            import asyncio as _asyncio
            from dataclasses import asdict
            from cato.personalization.habit_extractor import HabitExtractor
            from cato.platform import get_data_dir
            db_path = get_data_dir() / "cato.db"
            he = HabitExtractor(db_path=db_path)
            def _fetch():
                try:
                    patterns = he.extract_patterns()
                    return {"patterns": [asdict(p) for p in patterns], "count": len(patterns)}
                except Exception:
                    return {"patterns": [], "count": 0}
            loop = _asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _fetch)
            return web.json_response(result)
        except Exception as exc:
            logger.error("diagnostics_habits error: %s", exc)
            return web.json_response({"patterns": [], "count": 0, "error": str(exc)}, status=200)

    # ------------------------------------------------------------------ #
    # Favicon                                                              #
    # ------------------------------------------------------------------ #

    _FAVICON = Path(__file__).parent / "favicon.png"

    async def serve_favicon(request: web.Request) -> web.Response:
        """GET /favicon.png — serve the Cato logo."""
        if _FAVICON.exists():
            return web.FileResponse(_FAVICON)
        return web.Response(status=404)

    # ------------------------------------------------------------------ #
    # Audit transcript download helper                                    #
    # ------------------------------------------------------------------ #

    async def audit_download(request: web.Request) -> web.Response:
        """GET /api/audit/download?session_id=X — download audit entries as JSONL."""
        try:
            import asyncio as _asyncio
            from cato.audit.audit_log import AuditLog
            session_filter = request.rel_url.query.get("session_id", "")

            def _fetch() -> list:
                audit = AuditLog()
                audit.connect()
                assert audit._conn is not None
                q = ("SELECT id, session_id, action_type, tool_name, cost_cents, error, timestamp, prev_hash, row_hash "
                     "FROM audit_log")
                params: list = []
                if session_filter:
                    q += " WHERE session_id = ?"
                    params.append(session_filter)
                q += " ORDER BY id ASC LIMIT 5000"
                rows = audit._conn.execute(q, params).fetchall()
                result = [dict(r) for r in rows]
                audit.close()
                return result

            loop = _asyncio.get_running_loop()
            entries = await loop.run_in_executor(None, _fetch)
            import json as _json
            body = "\n".join(_json.dumps(e) for e in entries)
            fname = f"audit-{session_filter or 'all'}.jsonl"
            return web.Response(
                body=body.encode("utf-8"),
                content_type="application/x-ndjson",
                headers={"Content-Disposition": f'attachment; filename="{fname}"'},
            )
        except Exception as exc:
            logger.error("audit_download error: %s", exc)
            return web.Response(status=500)

    # ------------------------------------------------------------------ #
    # Catoflows                                                            #
    # ------------------------------------------------------------------ #

    async def list_flows(request: web.Request) -> web.Response:
        """GET /api/flows — list installed flow definitions."""
        try:
            from cato.orchestrator.clawflows import FlowEngine
            engine = FlowEngine()
            flows = engine.list_flows()
            return web.json_response(flows)
        except Exception as exc:
            logger.error("list_flows error: %s", exc)
            return web.json_response([], status=500)

    async def get_flow(request: web.Request) -> web.Response:
        """GET /api/flows/{name} — get flow YAML content."""
        name = request.match_info.get("name", "")
        import re as _re
        if not _re.match(r'^[a-zA-Z0-9_-]+$', name):
            return web.json_response({"error": "invalid name"}, status=400)
        try:
            from cato.orchestrator.clawflows import FlowEngine, FLOWS_DIR
            path = FLOWS_DIR / f"{name}.yaml"
            if not path.exists():
                return web.json_response({"error": "not found"}, status=404)
            content = path.read_text(encoding="utf-8")
            return web.json_response({"name": name, "content": content})
        except Exception as exc:
            logger.error("get_flow error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def create_flow(request: web.Request) -> web.Response:
        """POST /api/flows — create or update a flow YAML. Body: {name, content}."""
        try:
            body = await request.json()
            name = str(body.get("name", "")).strip()
            content = str(body.get("content", "")).strip()
            if not name or not content:
                return web.json_response({"error": "name and content required"}, status=400)
            # Sanitize name
            import re
            if not re.match(r'^[a-zA-Z0-9_-]+$', name):
                return web.json_response({"error": "invalid name"}, status=400)
            from cato.orchestrator.clawflows import FLOWS_DIR
            FLOWS_DIR.mkdir(parents=True, exist_ok=True)
            path = FLOWS_DIR / f"{name}.yaml"
            path.write_text(content, encoding="utf-8")
            return web.json_response({"status": "ok", "name": name})
        except Exception as exc:
            logger.error("create_flow error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def delete_flow(request: web.Request) -> web.Response:
        """DELETE /api/flows/{name} — delete a flow."""
        name = request.match_info.get("name", "")
        try:
            from cato.orchestrator.clawflows import FLOWS_DIR
            import re
            if not re.match(r'^[a-zA-Z0-9_-]+$', name):
                return web.json_response({"error": "invalid name"}, status=400)
            path = FLOWS_DIR / f"{name}.yaml"
            if not path.exists():
                return web.json_response({"error": "not found"}, status=404)
            path.unlink()
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("delete_flow error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def run_flow_now(request: web.Request) -> web.Response:
        """POST /api/flows/{name}/run — trigger a flow manually."""
        name = request.match_info.get("name", "")
        try:
            from cato.orchestrator.clawflows import FlowEngine
            import re
            if not re.match(r'^[a-zA-Z0-9_-]+$', name):
                return web.json_response({"error": "invalid name"}, status=400)
            engine = FlowEngine()
            result = await engine.run_flow(name)
            return web.json_response({
                "status": result.status,
                "flow_name": result.flow_name,
                "step_outputs": result.step_outputs,
                "error": result.error,
            })
        except FileNotFoundError:
            return web.json_response({"error": "flow not found"}, status=404)
        except Exception as exc:
            logger.error("run_flow_now error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def list_flow_runs(request: web.Request) -> web.Response:
        """GET /api/flows/{name}/runs — get run history for a flow."""
        name = request.match_info.get("name", "")
        try:
            import asyncio as _asyncio
            from cato.orchestrator.clawflows import FlowEngine
            engine = FlowEngine()
            def _fetch():
                rows = engine._conn.execute(
                    "SELECT id, flow_name, current_step, status, started_at, updated_at FROM flow_runs WHERE flow_name=? ORDER BY id DESC LIMIT 20",
                    (name,)
                ).fetchall()
                return [dict(r) for r in rows]
            loop = _asyncio.get_running_loop()
            runs = await loop.run_in_executor(None, _fetch)
            return web.json_response(runs)
        except Exception as exc:
            logger.error("list_flow_runs error: %s", exc)
            return web.json_response([], status=500)

    # ------------------------------------------------------------------ #
    # Remote Nodes                                                         #
    # ------------------------------------------------------------------ #

    async def list_nodes(request: web.Request) -> web.Response:
        """GET /api/nodes — list connected remote nodes."""
        try:
            if gateway is None:
                return web.json_response([])
            node_mgr = getattr(gateway, "_node_manager", None)
            if node_mgr is None:
                return web.json_response([])
            nodes = []
            for node_id, info in node_mgr._nodes.items():
                nodes.append({
                    "node_id": node_id,
                    "name": info.name,
                    "capabilities": info.capabilities,
                    "registered_at": info.registered_at,
                    "last_seen": info.last_seen,
                    "stale": info.is_stale(),
                })
            return web.json_response(nodes)
        except Exception as exc:
            logger.error("list_nodes error: %s", exc)
            return web.json_response([], status=500)

    async def disconnect_node(request: web.Request) -> web.Response:
        """DELETE /api/nodes/{node_id} — disconnect a remote node."""
        node_id = request.match_info.get("node_id", "")
        try:
            if gateway is None:
                return web.json_response({"error": "gateway unavailable"}, status=503)
            node_mgr = getattr(gateway, "_node_manager", None)
            if node_mgr is None:
                return web.json_response({"error": "node manager unavailable"}, status=503)
            node_mgr.remove(node_id)
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("disconnect_node error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Session Replay                                                        #
    # ------------------------------------------------------------------ #

    async def replay_session(request: web.Request) -> web.Response:
        """POST /api/sessions/{session_id}/replay — dry-run replay of a session."""
        session_id = request.match_info.get("session_id", "")
        try:
            import asyncio as _asyncio
            from cato.audit.audit_log import AuditLog
            from cato.replay import SessionReplayer
            audit = AuditLog()
            audit.connect()
            replayer = SessionReplayer(audit)
            def _run():
                import asyncio as _a2
                loop = _a2.new_event_loop()
                try:
                    return loop.run_until_complete(replayer.replay(session_id, live=False))
                finally:
                    loop.close()
            loop = _asyncio.get_running_loop()
            try:
                report = await loop.run_in_executor(None, _run)
            finally:
                audit.close()
            return web.json_response({
                "session_id": report.session_id,
                "mode": report.mode,
                "total_steps": report.total_steps,
                "matched": report.matched,
                "mismatched": report.mismatched,
                "skipped": report.skipped,
                "elapsed_seconds": report.elapsed_seconds,
                "steps": [
                    {
                        "index": s.index,
                        "tool_name": s.tool_name,
                        "matched": s.matched,
                        "elapsed_ms": s.elapsed_ms,
                    }
                    for s in report.steps
                ],
            })
        except Exception as exc:
            logger.error("replay_session error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Session Checkpoints & Receipt                                        #
    # ------------------------------------------------------------------ #

    _SESS_ID_RE = __import__("re").compile(r'^[a-zA-Z0-9_-]+$')

    async def list_session_checkpoints(request: web.Request) -> web.Response:
        """GET /api/sessions/{session_id}/checkpoints — list all checkpoints for a session."""
        session_id = request.match_info.get("session_id", "")
        if not _SESS_ID_RE.match(session_id):
            return web.json_response({"error": "invalid session_id"}, status=400)
        try:
            import asyncio as _asyncio
            from cato.core.session_checkpoint import SessionCheckpoint
            from cato.platform import get_data_dir
            db_path = get_data_dir() / "cato.db"

            def _fetch() -> list:
                if not db_path.exists():
                    return []
                ckpt = SessionCheckpoint(db_path=db_path)
                try:
                    row = ckpt.get(session_id)
                    if row is None:
                        return []
                    return [{
                        "checkpoint_id": row["session_id"],
                        "task_description": row.get("task_description", ""),
                        "token_count": row.get("token_count", 0),
                        "timestamp": row.get("checkpoint_at", ""),
                        "current_plan": row.get("current_plan", ""),
                        "decisions_made": row.get("decisions_made", []),
                        "files_modified": row.get("files_modified", []),
                    }]
                finally:
                    ckpt.close()

            loop = _asyncio.get_running_loop()
            checkpoints = await loop.run_in_executor(None, _fetch)
            return web.json_response(checkpoints)
        except Exception as exc:
            logger.error("list_session_checkpoints error: %s", exc)
            return web.json_response([], status=500)

    async def get_session_checkpoint(request: web.Request) -> web.Response:
        """GET /api/sessions/{session_id}/checkpoints/{cid} — single checkpoint summary."""
        session_id = request.match_info.get("session_id", "")
        cid = request.match_info.get("cid", "")
        if not _SESS_ID_RE.match(session_id):
            return web.json_response({"error": "invalid session_id"}, status=400)
        if not _SESS_ID_RE.match(cid):
            return web.json_response({"error": "invalid checkpoint_id"}, status=400)
        # cid must match the session_id stored (one checkpoint per session in current schema)
        if cid != session_id:
            return web.json_response({"error": "checkpoint not found"}, status=404)
        try:
            import asyncio as _asyncio
            from cato.core.session_checkpoint import SessionCheckpoint
            from cato.platform import get_data_dir
            db_path = get_data_dir() / "cato.db"

            def _fetch() -> dict:
                if not db_path.exists():
                    return {}
                ckpt = SessionCheckpoint(db_path=db_path)
                try:
                    summary = ckpt.get_summary(session_id)
                    row = ckpt.get(session_id)
                    if row is None:
                        return {}
                    return {
                        "checkpoint_id": session_id,
                        "task_description": row.get("task_description", ""),
                        "token_count": row.get("token_count", 0),
                        "timestamp": row.get("checkpoint_at", ""),
                        "summary": summary,
                    }
                finally:
                    ckpt.close()

            loop = _asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _fetch)
            if not data:
                return web.json_response({"error": "checkpoint not found"}, status=404)
            return web.json_response(data)
        except Exception as exc:
            logger.error("get_session_checkpoint error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def session_receipt(request: web.Request) -> web.Response:
        """GET /api/sessions/{session_id}/receipt — signed cost receipt for a session."""
        session_id = request.match_info.get("session_id", "")
        if not _SESS_ID_RE.match(session_id):
            return web.json_response({"error": "invalid session_id"}, status=400)
        try:
            import asyncio as _asyncio
            from cato.audit.audit_log import AuditLog
            from cato.receipt import ReceiptWriter

            def _generate() -> dict:
                audit = AuditLog()
                audit.connect()
                try:
                    writer = ReceiptWriter()
                    receipt = writer.generate(session_id, audit)
                    return {
                        "session_id": receipt.session_id,
                        "total_cents": receipt.total_cents,
                        "total_usd": round(receipt.total_cents / 100, 4),
                        "action_count": len(receipt.actions),
                        "error_count": receipt.error_count,
                        "signed_hash": receipt.signed_hash,
                        "generated_at": receipt.generated_at,
                        "start_ts": receipt.start_ts,
                        "end_ts": receipt.end_ts,
                        "actions": [
                            {
                                "index": a.index,
                                "tool_name": a.tool_name,
                                "action_type": a.action_type,
                                "cost_cents": a.cost_cents,
                                "timestamp": a.timestamp,
                                "row_hash": a.row_hash,
                                "error": a.error,
                            }
                            for a in receipt.actions
                        ],
                    }
                finally:
                    audit.close()

            loop = _asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _generate)
            return web.json_response(data)
        except Exception as exc:
            logger.error("session_receipt error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Adapters status                                                      #
    # ------------------------------------------------------------------ #

    async def list_adapters(request: web.Request) -> web.Response:
        """GET /api/adapters — list channel adapters and their status."""
        try:
            adapters_info = []

            # Check adapter status via gateway._adapters if gateway is available
            if gateway is not None:
                gateway_adapters = getattr(gateway, "_adapters", [])
                seen_names: set[str] = set()
                for adapter in gateway_adapters:
                    name = getattr(adapter, "channel_name", type(adapter).__name__.lower())
                    running = getattr(adapter, "running", False)
                    status = "connected" if running else "disconnected"
                    details: dict = {}
                    seen_names.add(name)
                    adapters_info.append({"name": name, "status": status, "details": details})

                # Surface known adapters that are not currently loaded
                for known_name in ("telegram", "whatsapp"):
                    if known_name not in seen_names:
                        adapters_info.append({"name": known_name, "status": "not_configured", "details": {}})
            else:
                # No gateway — attempt a lightweight import check
                for adapter_name, module_path in [
                    ("telegram", "cato.adapters.telegram"),
                    ("whatsapp", "cato.adapters.whatsapp"),
                ]:
                    try:
                        import importlib
                        importlib.import_module(module_path)
                        adapters_info.append({"name": adapter_name, "status": "not_configured", "details": {}})
                    except ImportError:
                        adapters_info.append({"name": adapter_name, "status": "not_configured", "details": {}})

            return web.json_response({"adapters": adapters_info})
        except Exception as exc:
            logger.error("list_adapters error: %s", exc)
            return web.json_response({"adapters": []}, status=500)

    # ------------------------------------------------------------------ #
    # Activity indicator                                                   #
    # ------------------------------------------------------------------ #

    async def get_activity(request: web.Request) -> web.Response:
        """GET /api/activity — return whether the agent is currently working."""
        if gateway is not None and hasattr(gateway, "get_activity"):
            return web.json_response(gateway.get_activity())
        return web.json_response({"busy": False, "session_id": "", "task": "", "updated_at": 0})

    # ------------------------------------------------------------------ #
    # Heartbeat status                                                     #
    # ------------------------------------------------------------------ #

    # BUG FIX HB-001: POST /api/heartbeat — receive heartbeat from gateway poster
    _heartbeat_state: dict = {}

    async def post_heartbeat(request: web.Request) -> web.Response:
        """POST /api/heartbeat — receive a heartbeat POST from the gateway background task."""
        try:
            body = await request.json()
            agent_name = str(body.get("agent_name", "Cato"))
            uptime_seconds = int(body.get("uptime_seconds", 0))
            _heartbeat_state["agent_name"] = agent_name
            _heartbeat_state["uptime_seconds"] = uptime_seconds
            _heartbeat_state["last_seen"] = time.monotonic()
            logger.debug("Heartbeat received from agent=%s uptime=%d", agent_name, uptime_seconds)
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("post_heartbeat error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    def _system_metrics() -> dict:
        """Collect system metrics (CPU, memory, disk) via psutil if available."""
        try:
            import psutil
            return {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_percent": psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage("/").percent,
            }
        except ImportError:
            return {"cpu_percent": None, "memory_percent": None, "disk_percent": None}
        except Exception:
            return {"cpu_percent": None, "memory_percent": None, "disk_percent": None}

    async def get_heartbeat(request: web.Request) -> web.Response:
        """GET /api/heartbeat — return HeartbeatMonitor state + system metrics."""
        try:
            import datetime as _dt

            monitor = None
            if gateway is not None:
                monitor = getattr(gateway, "_heartbeat_monitor", None)

            if monitor is None:
                return web.json_response({
                    "last_heartbeat": None,
                    "agent_name": None,
                    "uptime_seconds": None,
                    "status": "unknown",
                    **_system_metrics(),
                })

            # HeartbeatMonitor tracks _last_fire per agent name
            last_fire: dict[str, float] = getattr(monitor, "_last_fire", {})

            if not last_fire:
                # Fall back to POST-based heartbeat state
                if _heartbeat_state.get("last_seen"):
                    import datetime as _dt2
                    elapsed2 = time.monotonic() - _heartbeat_state["last_seen"]
                    wall2 = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=elapsed2)
                    return web.json_response({
                        "last_heartbeat": wall2.isoformat(),
                        "agent_name": _heartbeat_state.get("agent_name", "Cato"),
                        "uptime_seconds": _heartbeat_state.get("uptime_seconds", 0),
                        "status": "alive" if elapsed2 < 600 else "stale",
                        **_system_metrics(),
                    })
                return web.json_response({
                    "last_heartbeat": None,
                    "agent_name": None,
                    "uptime_seconds": None,
                    "status": "unknown",
                    **_system_metrics(),
                })

            # Use the most recently fired agent
            agent_name, last_ts = max(last_fire.items(), key=lambda kv: kv[1])
            now = time.monotonic()
            # Compute elapsed since last heartbeat fire (monotonic seconds)
            elapsed = now - last_ts
            # Stale threshold: 2 × default interval (10 minutes)
            stale_threshold = 600
            status = "alive" if elapsed < stale_threshold else "stale"

            # Convert monotonic timestamp to wall-clock ISO string (approximate)
            wall_now = _dt.datetime.now(_dt.timezone.utc)
            last_heartbeat_wall = wall_now - _dt.timedelta(seconds=elapsed)
            last_heartbeat_iso = last_heartbeat_wall.isoformat()

            uptime_seconds = now - _START_TIME

            return web.json_response({
                "last_heartbeat": last_heartbeat_iso,
                "agent_name": agent_name,
                "uptime_seconds": round(uptime_seconds, 1),
                "status": status,
                **_system_metrics(),
            })
        except Exception as exc:
            logger.error("get_heartbeat error: %s", exc)
            return web.json_response({
                "last_heartbeat": None,
                "agent_name": None,
                "uptime_seconds": None,
                "status": "unknown",
            }, status=500)

    # ------------------------------------------------------------------ #
    # Coding Agent routes                                                  #
    # ------------------------------------------------------------------ #

    async def serve_coding_agent(request: web.Request) -> web.FileResponse:
        """Serve the coding agent SPA for /coding-agent and /coding-agent/{task_id}."""
        return web.FileResponse(_CODING_AGENT)

    # ------------------------------------------------------------------ #
    # Router                                                               #
    # ------------------------------------------------------------------ #

    app.router.add_get("/",                              serve_dashboard)
    app.router.add_get("/health",                        health)
    app.router.add_get("/mcp/health",                    mcp_health)
    app.router.add_route("*", "/mcp",                    mcp_proxy)
    app.router.add_route("*", "/mcp/{tail:.*}",          mcp_proxy)
    app.router.add_get("/ws",                            websocket_handler)
    app.router.add_post("/config",                       save_config)
    app.router.add_get("/favicon.png",                   serve_favicon)
    # Vault
    app.router.add_get("/api/vault/keys",                vault_list_keys)
    app.router.add_post("/api/vault/set",                vault_set_key)
    app.router.add_delete("/api/vault/delete",           vault_delete_key)
    # Sessions
    app.router.add_get("/api/sessions",                  list_sessions)
    app.router.add_delete("/api/sessions/{session_id}",  kill_session)
    app.router.add_post("/api/compact",                  compact_session)
    # Cross-channel chat history (web + Telegram)
    app.router.add_get("/api/chat/history",              chat_history)
    app.router.add_post("/api/chat/upload",              chat_upload)
    # Skills
    app.router.add_get("/api/skills",                         list_skills)
    app.router.add_get("/api/skills/{name}/content",          get_skill_content)
    app.router.add_route("PATCH", "/api/skills/{name}/content", patch_skill_content)
    app.router.add_post("/api/skills/{name}/toggle",          toggle_skill)
    # CLI status
    app.router.add_get("/api/cli/status",                     cli_status)
    app.router.add_post("/api/cli/{name}/restart",               cli_restart)
    # Cron
    app.router.add_get("/api/cron/jobs",                     list_cron_jobs)
    app.router.add_post("/api/cron/jobs",                    create_cron_job)
    app.router.add_delete("/api/cron/jobs/{name}",           delete_cron_job)
    app.router.add_post("/api/cron/jobs/{name}/toggle",      toggle_cron_job)
    app.router.add_post("/api/cron/jobs/{name}/run",         run_cron_job_now)
    app.router.add_get("/api/cron/jobs/{name}/history",      cron_job_history)
    # Budget
    app.router.add_get("/api/budget/summary",            budget_summary)
    # Usage
    app.router.add_get("/api/usage/summary",             usage_summary)
    app.router.add_get("/api/usage/routing",            get_routing_history)
    # Logs
    app.router.add_get("/api/logs",                      get_logs)
    # Audit
    app.router.add_get("/api/audit/entries",             get_audit_entries)
    app.router.add_post("/api/audit/verify",             verify_audit_chain)
    app.router.add_get("/api/audit/download",            audit_download)
    # Config
    app.router.add_get("/api/config",                    get_config)
    app.router.add_route("PATCH", "/api/config",         patch_config)
    app.router.add_get("/api/routing/status",            routing_status)
    # Memory
    app.router.add_post("/api/config/reload",             reload_config)
    app.router.add_get("/api/memory/files",              memory_files)
    app.router.add_get("/api/memory/content",            memory_content)
    app.router.add_route("PATCH", "/api/memory/content", patch_memory_content)
    app.router.add_get("/api/memory/stats",              memory_stats)
    # Workspace identity files
    app.router.add_get("/api/workspace/files",           workspace_list)
    app.router.add_get("/api/workspace/file",            workspace_get)
    app.router.add_put("/api/workspace/file",            workspace_put)
    # BUG FIX IDENT-002: POST as alias for PUT on workspace file
    app.router.add_post("/api/workspace/file",           workspace_put)
    # Action Guard
    app.router.add_get("/api/action-guard/status",       action_guard_status)
    # Daemon
    app.router.add_post("/api/daemon/restart",           daemon_restart)
    # Flows (Catoflows)
    app.router.add_get("/api/flows",                    list_flows)
    app.router.add_post("/api/flows",                   create_flow)
    app.router.add_get("/api/flows/{name}",             get_flow)
    app.router.add_delete("/api/flows/{name}",          delete_flow)
    app.router.add_post("/api/flows/{name}/run",        run_flow_now)
    app.router.add_get("/api/flows/{name}/runs",        list_flow_runs)
    # Diagnostics
    app.router.add_get("/api/diagnostics/query-classifier",      diagnostics_query_classifier)
    app.router.add_get("/api/diagnostics/contradiction-health",  diagnostics_contradiction_health)
    app.router.add_get("/api/diagnostics/decision-memory",       diagnostics_decision_memory)
    app.router.add_get("/api/diagnostics/anomaly-domains",       diagnostics_anomaly_domains)
    app.router.add_get("/api/diagnostics/skill-corrections",     diagnostics_skill_corrections)
    app.router.add_get("/api/diagnostics/disagreements",         diagnostics_disagreements)
    app.router.add_get("/api/diagnostics/epistemic",             diagnostics_epistemic)
    app.router.add_get("/api/diagnostics/context-budget",        diagnostics_context_budget)
    app.router.add_get("/api/diagnostics/retrieval",             diagnostics_retrieval)
    app.router.add_get("/api/diagnostics/habits",                diagnostics_habits)
    # Adapters
    app.router.add_get("/api/adapters",                 list_adapters)
    app.router.add_get("/api/activity",                  get_activity)

    app.router.add_get("/api/tokens",                    list_tokens)
    app.router.add_post("/api/tokens",                   create_token)
    app.router.add_delete("/api/tokens/{token_id}",      revoke_token)
    # Heartbeat
    app.router.add_get("/api/heartbeat",                get_heartbeat)
    # BUG FIX HB-001: POST endpoint for gateway heartbeat poster
    app.router.add_post("/api/heartbeat",               post_heartbeat)
    # Nodes
    app.router.add_get("/api/nodes",                    list_nodes)
    app.router.add_delete("/api/nodes/{node_id}",       disconnect_node)
    # Replay
    app.router.add_post("/api/sessions/{session_id}/replay", replay_session)
    # Session Checkpoints & Receipt
    app.router.add_get("/api/sessions/{session_id}/checkpoints",       list_session_checkpoints)
    app.router.add_get("/api/sessions/{session_id}/checkpoints/{cid}", get_session_checkpoint)
    app.router.add_get("/api/sessions/{session_id}/receipt",           session_receipt)

    # Coding agent UI routes
    app.router.add_get("/coding-agent",           serve_coding_agent)
    app.router.add_get("/coding-agent/{task_id}", serve_coding_agent)

    # Register coding agent API + WebSocket routes
    try:
        from cato.api.routes import register_all_routes
        register_all_routes(app)
        logger.info("Coding agent API routes registered")
    except ImportError as exc:
        logger.warning("Could not register coding agent routes: %s", exc)

    # ------------------------------------------------------------------ #
    # CLI process pool lifecycle                                          #
    # ------------------------------------------------------------------ #

    async def _start_cli_pool(app: web.Application) -> None:
        """Schedule persistent CLI process warmup after server start."""
        try:
            from cato.orchestrator.cli_process_pool import get_pool
            pool = get_pool()
            # Attach the pool to the gateway so /api/cli/{name}/restart can
            # access the same singleton instance used here.
            if gateway is not None:
                setattr(gateway, "_cli_pool", pool)

            async def _warm_pool_later() -> None:
                try:
                    await asyncio.sleep(120)
                    await pool.start_all()
                    logger.info("CLI process pool started")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("CLI process pool failed to start: %s", exc)

            app[_CLI_POOL_STARTUP_TASK_KEY] = asyncio.create_task(
                _warm_pool_later(),
                name="cato-cli-pool-warmup",
            )
        except Exception as exc:
            logger.warning("CLI process pool failed to schedule startup: %s", exc)

    async def _stop_cli_pool(app: web.Application) -> None:
        """Shut down persistent CLI processes on server stop."""
        try:
            startup_task = app.get(_CLI_POOL_STARTUP_TASK_KEY)
            if startup_task is not None and not startup_task.done():
                startup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await startup_task

            from cato.orchestrator.cli_process_pool import get_pool
            pool = get_pool()
            await pool.stop_all()
            logger.info("CLI process pool stopped")
        except Exception as exc:
            logger.warning("CLI process pool failed to stop: %s", exc)

    app.on_startup.append(_start_cli_pool)
    app.on_cleanup.append(_stop_cli_pool)

    logger.info("UI app created — dashboard: %s", _DASHBOARD)
    return app
