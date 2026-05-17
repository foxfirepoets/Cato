from __future__ import annotations

import socket
from types import SimpleNamespace

import importlib

import pytest

# The local mcp/ package shadows the MCP SDK — import SDK symbols lazily
# so test collection never triggers the circular import in mcp/runtime.py.
try:
    _mcp_sdk = importlib.import_module("mcp")
    _mcp_http = importlib.import_module("mcp.client.streamable_http")
    ClientSession = _mcp_sdk.ClientSession
    streamablehttp_client = _mcp_http.streamablehttp_client
    _MCP_AVAILABLE = True
except Exception:
    _MCP_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _MCP_AVAILABLE, reason="MCP SDK not importable (shadowed by local mcp/ package)")

from cato.mcp import CatoMCPRuntime  # noqa: E402


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _FakeGateway:
    def __init__(self) -> None:
        self._start_time = 0.0
        self._lanes = {"mcp:test": SimpleNamespace(_queue=SimpleNamespace(qsize=lambda: 0), _task=None)}
        self._adapters = [SimpleNamespace(channel_name="telegram", running=True)]
        self._message_history = [
            {"session_id": "mcp:test", "role": "user", "text": "hello"},
            {"session_id": "mcp:test", "role": "assistant", "text": "world"},
        ]

    async def request_response(self, session_id: str, message: str, channel: str = "mcp") -> dict:
        return {
            "session_id": session_id,
            "channel": channel,
            "reply": f"echo:{message}",
            "model": "test-model",
        }


@pytest.mark.asyncio
async def test_mcp_runtime_supports_initialize_list_and_call():
    port = _free_port()
    config = SimpleNamespace(
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_mount_path="/mcp",
        log_level="INFO",
    )
    runtime = CatoMCPRuntime(_FakeGateway(), config)
    await runtime.start()
    try:
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "Cato"

                tools = await session.list_tools()
                tool_names = {tool.name for tool in tools.tools}
                assert {"cato_chat", "cato_status", "cato_list_sessions", "cato_get_history"} <= tool_names

                result = await session.call_tool(
                    "cato_chat",
                    {"message": "hello", "session_id": "mcp:test"},
                )
                assert result.structuredContent["reply"] == "echo:hello"
                assert result.structuredContent["session_id"] == "mcp:test"
    finally:
        await runtime.stop()

