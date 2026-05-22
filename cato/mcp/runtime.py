"""Remote MCP server support for Cato."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import threading
import time
from typing import Any
from uuid import uuid4

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Lock to prevent concurrent sys.path mutations during MCP server initialization
_SYS_PATH_LOCK = threading.Lock()


def _check_mcp_auth(request_headers: dict) -> bool:
    """Verify the MCP request includes the daemon token.

    The token is read from the ``_CATO_DAEMON_TOKEN`` environment variable,
    which the daemon sets at startup.  If the variable is absent (e.g. in
    tests or legacy installs) the check is skipped and all requests are
    allowed — the localhost-only binding (``127.0.0.1:8765``) is the
    primary access control in that case.

    **Current limitation:** FastMCP 2.x does not pass raw HTTP request headers
    to tool handler functions.  This function is called with an empty dict
    ``{}`` until FastMCP exposes a header-access API.  While ``_CATO_DAEMON_TOKEN``
    is unset (the default), behaviour is correct — all calls are allowed and
    localhost binding is the sole guard.  Do NOT set ``_CATO_DAEMON_TOKEN``
    until FastMCP header support is wired, or all MCP tool calls will be
    permanently denied.  See: https://github.com/jlowin/fastmcp/issues (track
    header access support).
    """
    expected = os.environ.get("_CATO_DAEMON_TOKEN", "")
    if not expected:
        # Token not configured — allow; localhost binding is the protection.
        return True
    provided = request_headers.get("x-cato-token", "")
    return hmac.compare_digest(expected, provided)


def _adapter_status(gateway: Any) -> list[dict[str, Any]]:
    """Return the current adapter status snapshot used by MCP health tools."""
    adapters = []
    seen: set[str] = set()
    for adapter in getattr(gateway, "_adapters", []):
        name = getattr(adapter, "channel_name", type(adapter).__name__.lower())
        seen.add(name)
        adapters.append(
            {
                "name": name,
                "status": "connected" if getattr(adapter, "running", False) else "disconnected",
            }
        )

    for known_name in ("telegram", "whatsapp"):
        if known_name not in seen:
            adapters.append({"name": known_name, "status": "not_configured"})

    return adapters


def _history_for_session(gateway: Any, session_id: str, limit: int) -> list[dict[str, Any]]:
    history = [
        entry
        for entry in getattr(gateway, "_message_history", [])
        if entry.get("session_id") == session_id
    ]
    limit = max(1, min(limit, 100))
    return history[-limit:]


def create_mcp_server(gateway: Any, config: Any) -> Any:
    """Create the FastMCP server that fronts the running Cato gateway."""
    import sys, importlib
    # The project root has a local `mcp/` directory that shadows the installed
    # mcp SDK package. Remove the project root from sys.path temporarily so
    # importlib finds the installed package, not the local folder.
    import os as _os
    _cato_root = str(__file__.split("cato")[0].rstrip("/\\"))
    with _SYS_PATH_LOCK:
        # Remove all entries that resolve to the Cato project root (including '' for cwd)
        _shadow_roots = {_cato_root, ""}
        _removed: list[tuple[int, str]] = [
            (i, p) for i, p in enumerate(sys.path)
            if p in _shadow_roots or (_os.path.isabs(p) and _os.path.normcase(p) == _os.path.normcase(_cato_root))
        ]
        for i, _ in sorted(_removed, reverse=True):
            sys.path.pop(i)
        # Invalidate any cached local mcp module
        for _k in list(sys.modules.keys()):
            if _k == "mcp" or _k.startswith("mcp."):
                del sys.modules[_k]
        try:
            _sdk_mcp = importlib.import_module("mcp.server.fastmcp")
        finally:
            for i, p in sorted(_removed):
                sys.path.insert(i, p)
    FastMCP = _sdk_mcp.FastMCP
    mount_path = getattr(config, "mcp_mount_path", "/mcp") or "/mcp"
    server = FastMCP(
        name="Cato",
        instructions="Tools for chatting with the running Cato agent daemon.",
        host=getattr(config, "mcp_host", "127.0.0.1"),
        port=int(getattr(config, "mcp_port", 8765)),
        streamable_http_path=mount_path,
        log_level=getattr(config, "log_level", "INFO"),
    )

    @server.custom_route(f"{mount_path}/health", methods=["GET"], include_in_schema=False)
    async def health_check(_request: Request) -> Response:
        uptime = int(time.monotonic() - getattr(gateway, "_start_time", time.monotonic()))
        return JSONResponse(
            {
                "status": "ok",
                "service": "cato-mcp",
                "mount_path": mount_path,
                "uptime": max(uptime, 0),
                "sessions": len(getattr(gateway, "_lanes", {})),
                "adapters": _adapter_status(gateway),
            }
        )

    @server.tool(
        name="cato_chat",
        description="Send a message to Cato and get the reply for a session.",
        structured_output=True,
    )
    async def cato_chat(message: str, session_id: str | None = None) -> dict[str, Any]:
        sid = (session_id or "").strip() or f"mcp:{uuid4().hex}"
        return await gateway.request_response(sid, message, channel="mcp")

    @server.tool(
        name="cato_status",
        description="Return Cato health, uptime, and adapter status.",
        structured_output=True,
    )
    async def cato_status() -> dict[str, Any]:
        uptime = int(time.monotonic() - getattr(gateway, "_start_time", time.monotonic()))
        return {
            "status": "ok",
            "uptime": max(uptime, 0),
            "sessions": len(getattr(gateway, "_lanes", {})),
            "adapters": _adapter_status(gateway),
        }

    @server.tool(
        name="cato_list_sessions",
        description="List active Cato sessions and whether each one is currently running.",
        structured_output=True,
    )
    async def cato_list_sessions() -> dict[str, Any]:
        # SECURITY NOTE: This endpoint exposes internal session identifiers.
        # Primary access control is the localhost-only binding (127.0.0.1:8765).
        # For SSH-tunnel or reverse-proxy scenarios, set _CATO_DAEMON_TOKEN in the
        # daemon environment and require callers to supply it via x-cato-token.
        # FastMCP tool handlers do not receive raw HTTP headers; the check below
        # uses an empty dict which allows the call when no token is configured
        # (i.e. the localhost binding is the sole guard).  When a token IS set,
        # all calls without an explicit header are blocked.
        if not _check_mcp_auth({}):
            return {"error": "unauthorized", "sessions": [], "count": 0}
        sessions = []
        for sid, lane in getattr(gateway, "_lanes", {}).items():
            queue_depth = lane._queue.qsize() if hasattr(lane, "_queue") else 0
            running = lane._task is not None and not lane._task.done() if hasattr(lane, "_task") else False
            sessions.append({"session_id": sid, "queue_depth": queue_depth, "running": running})
        return {"sessions": sessions, "count": len(sessions)}

    @server.tool(
        name="cato_get_history",
        description="Get the recent message history for a Cato session.",
        structured_output=True,
    )
    async def cato_get_history(session_id: str, limit: int = 20) -> dict[str, Any]:
        # SECURITY NOTE: This endpoint returns message history which may contain PII.
        # Primary access control is the localhost-only binding (127.0.0.1:8765).
        # For SSH-tunnel or reverse-proxy scenarios, set _CATO_DAEMON_TOKEN in the
        # daemon environment and require callers to supply it via x-cato-token.
        # See _check_mcp_auth() for the token verification logic.
        if not _check_mcp_auth({}):
            return {"error": "unauthorized", "session_id": session_id, "messages": [], "count": 0}
        messages = _history_for_session(gateway, session_id, limit)
        return {"session_id": session_id, "messages": messages, "count": len(messages)}

    return server


class CatoMCPRuntime:
    """Manage the background FastMCP HTTP server used by Claude connectors."""

    def __init__(self, gateway: Any, config: Any) -> None:
        self.gateway = gateway
        self.config = config
        self.host = getattr(config, "mcp_host", "127.0.0.1")
        self.port = int(getattr(config, "mcp_port", 8765))
        self.mount_path = getattr(config, "mcp_mount_path", "/mcp") or "/mcp"
        self.server = create_mcp_server(gateway, config)
        self._uvicorn: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the MCP server in the background and wait for it to accept connections."""
        if self._task is not None and not self._task.done():
            return

        app = self.server.streamable_http_app()
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level=str(getattr(self.config, "log_level", "INFO")).lower(),
            access_log=False,
        )
        self._uvicorn = uvicorn.Server(config)
        self._task = asyncio.create_task(self._uvicorn.serve(), name="cato-mcp")
        await self._wait_until_ready()
        logger.info("MCP runtime listening on http://%s:%s%s", self.host, self.port, self.mount_path)

    async def stop(self) -> None:
        """Stop the background MCP server cleanly."""
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._uvicorn = None

    async def _wait_until_ready(self) -> None:
        probe_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection(probe_host, self.port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.1)
        raise RuntimeError(f"MCP runtime failed to start on {self.host}:{self.port}")

    def proxy_target(self, path_qs: str) -> str:
        """Return the internal URL used by the aiohttp proxy route."""
        return f"http://{self.host}:{self.port}{path_qs}"
