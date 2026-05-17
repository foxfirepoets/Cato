from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web

from cato.budget import BudgetManager
from cato.cli import _bind_http_site_with_fallback
from cato.config import CatoConfig
from cato.gateway import Gateway
from cato.ui.server import create_ui_app, _DAEMON_TOKEN


class _StubAgentLoop:
    async def run(self, session_id: str, message: str, agent_id: str) -> tuple[str, str, str]:
        return (f"stub response to {message}", "", "stub-model")


async def _receive_ws_message_of_type(
    ws: aiohttp.ClientWebSocketResponse,
    expected_type: str,
    *,
    timeout: float = 8.0,
) -> dict:
    while True:
        msg = await ws.receive(timeout=timeout)
        if msg.type == aiohttp.WSMsgType.TEXT:
            payload = json.loads(msg.data)
            if payload.get("type") == expected_type:
                return payload
        elif msg.type == aiohttp.WSMsgType.CLOSED:
            raise AssertionError(f"websocket closed before receiving {expected_type!r}")
        elif msg.type == aiohttp.WSMsgType.ERROR:
            raise AssertionError(f"websocket error before receiving {expected_type!r}: {ws.exception()}")


async def _receive_coding_agent_event(
    ws: aiohttp.ClientWebSocketResponse,
    expected_event: str,
    *,
    timeout: float = 8.0,
) -> dict:
    while True:
        msg = await ws.receive(timeout=timeout)
        if msg.type == aiohttp.WSMsgType.TEXT:
            payload = json.loads(msg.data)
            if payload.get("event") == expected_event:
                return payload
        elif msg.type == aiohttp.WSMsgType.CLOSED:
            raise AssertionError(f"coding-agent websocket closed before receiving {expected_event!r}")
        elif msg.type == aiohttp.WSMsgType.ERROR:
            raise AssertionError(
                f"coding-agent websocket error before receiving {expected_event!r}: {ws.exception()}"
            )


def _occupy_port_8080() -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 8080))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


@pytest.mark.asyncio
async def test_fallback_port_keeps_http_and_websocket_surfaces_working(tmp_path: Path):
    occupied_socket = _occupy_port_8080()

    config = CatoConfig(
        agent_name="fallback-test-agent",
        workspace_dir=str(tmp_path / "workspace"),
        webchat_port=8080,
        telegram_enabled=False,
        whatsapp_enabled=False,
    )
    budget = BudgetManager(
        session_cap=config.session_cap,
        monthly_cap=config.monthly_cap,
        budget_path=tmp_path / "budget.json",
    )
    gateway = Gateway(config, budget, vault=None)
    gateway._agent_loop = _StubAgentLoop()

    mock_pool = MagicMock()
    mock_pool.start_all = AsyncMock()
    mock_pool.stop_all = AsyncMock()
    mock_pool.is_warm.return_value = False

    coding_agent_responses = {
        "claude": {"model": "claude", "response": "claude ok", "confidence": 0.92, "latency_ms": 10},
        "codex": {"model": "codex", "response": "codex ok", "confidence": 0.81, "latency_ms": 12},
        "gemini": {"model": "gemini", "response": "gemini ok", "confidence": 0.75, "latency_ms": 11},
    }

    with (
        patch.object(gateway, "_ensure_agent_loop", AsyncMock()),
        patch("cato.orchestrator.cli_process_pool.get_pool", return_value=mock_pool),
        patch("cato.api.websocket_handler.invoke_claude_api", AsyncMock(return_value=coding_agent_responses["claude"])),
        patch("cato.api.websocket_handler.invoke_codex_cli", AsyncMock(return_value=coding_agent_responses["codex"])),
        patch("cato.api.websocket_handler.invoke_gemini_cli", AsyncMock(return_value=coding_agent_responses["gemini"])),
    ):
        app = await create_ui_app(gateway)
        runner = web.AppRunner(app)
        await runner.setup()
        _site, actual_port = await _bind_http_site_with_fallback(
            runner,
            "127.0.0.1",
            8080,
            max_attempts=5,
            retry_delay=0.01,
        )
        await gateway.start()

        try:
            assert actual_port != 8080

            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{actual_port}/health") as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["status"] == "ok"

                async with session.ws_connect(f"http://127.0.0.1:{actual_port}/ws", headers={"X-Cato-Token": _DAEMON_TOKEN}) as ws:
                    await ws.send_json(
                        {
                            "type": "node_register",
                            "node_id": "occupied-port-node",
                            "name": "Occupied Port Node",
                            "capabilities": ["shell"],
                        }
                    )
                    node_registered = await _receive_ws_message_of_type(ws, "node_registered")
                    assert node_registered["node_id"] == "occupied-port-node"

                    await ws.send_json(
                        {
                            "type": "message",
                            "session_id": "occupied-port-session",
                            "channel": "web",
                            "text": "hello from fallback",
                        }
                    )
                    response = await _receive_ws_message_of_type(ws, "response")
                    assert response["session_id"] == "occupied-port-session"
                    assert response["model"] == "stub-model"
                    assert "stub response to hello from fallback" in response["text"]

                async with session.post(
                    f"http://127.0.0.1:{actual_port}/api/coding-agent/invoke",
                    json={"task": "Verify coding-agent websocket survives fallback port"},
                    headers={"X-Cato-Token": _DAEMON_TOKEN},
                ) as resp:
                    assert resp.status == 200
                    task_data = await resp.json()

                task_id = task_data["task_id"]
                async with session.ws_connect(
                    f"http://127.0.0.1:{actual_port}/ws/coding-agent/{task_id}?token={_DAEMON_TOKEN}"
                ) as coding_ws:
                    synthesis = await _receive_coding_agent_event(coding_ws, "synthesis_complete")
                    assert synthesis["data"]["primary"]["model"] == "claude"
                    assert synthesis["data"]["primary"]["response"] == "claude ok"
        finally:
            await runner.cleanup()
            await gateway.stop()
            if occupied_socket is not None:
                occupied_socket.close()
