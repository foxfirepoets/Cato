"""
tests/test_pty_routes_integration.py — Integration tests for PTY REST and WebSocket.

- Creates app with PTY routes, POSTs to create session, connects WS, sends input, asserts output.
- Skips when PTY backend or CLI (claude/codex/gemini) is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from cato.api.pty_routes import register_routes
from cato.orchestrator.pty_session import pty_available, build_pty_cmd


def _has_cli() -> bool:
    for name in ("claude", "codex", "gemini"):
        try:
            build_pty_cmd(name)
            return True
        except (ValueError, FileNotFoundError):
            continue
    return False


def make_app() -> web.Application:
    app = web.Application()
    register_routes(app)
    return app


@pytest.mark.skipif(not pty_available(), reason="PTY backend not available")
@pytest.mark.skipif(not _has_cli(), reason="No CLI (claude/codex/gemini) on PATH")
class TestPtyRoutesIntegration(AioHTTPTestCase):
    async def get_application(self):
        return make_app()

    async def test_create_session_and_ws_output(self):
        class FakeSession:
            def __init__(self):
                self.session_id = "test-session"
                self.cli_name = "claude"
                self.last_activity_at = time.monotonic()
                self.started = False
                self.closed = False
                self.writes: list[str] = []

            @property
            def is_alive(self):
                return not self.closed

            def start(self, *args, **kwargs):
                self.started = True

            def write(self, text: str):
                self.writes.append(text)

            def resize(self, cols: int, rows: int):
                pass

            def terminate(self):
                self.closed = True

            async def read_chunks(self):
                yield b"fake cli ready\r\n"
                while not self.closed:
                    await asyncio.sleep(0.05)

        fake = FakeSession()
        sessions = {fake.session_id: fake}

        def fake_remove_session(session_id: str):
            session = sessions.pop(session_id, None)
            if session:
                session.terminate()

        with (
            patch(f"{__name__}.build_pty_cmd", return_value=["fake-cli"]),
            patch("cato.api.pty_routes.build_pty_cmd", return_value=["fake-cli"]),
            patch("cato.api.pty_routes.create_session", return_value=fake),
            patch("cato.api.pty_routes.get_session", side_effect=lambda session_id: sessions.get(session_id)),
            patch("cato.api.pty_routes.remove_session", side_effect=fake_remove_session),
            patch("cato.api.pty_routes.list_sessions", side_effect=lambda: [
                {
                    "session_id": s.session_id,
                    "cli": s.cli_name,
                    "state": "running",
                    "last_activity_at": s.last_activity_at,
                }
                for s in sessions.values()
            ]),
        ):
            resp = await self.client.post(
                "/api/pty/sessions",
                json={"cli": "claude"},
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            session_id = data["session_id"]
            assert data["cli"] == "claude"

            received = []
            async with self.client.ws_connect(f"/ws/pty/{session_id}") as ws:
                msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                if msg.type == web.WSMsgType.TEXT:
                    obj = json.loads(msg.data)
                    received.append(obj)
                await ws.send_str(json.dumps({"type": "input", "data": "help\n"}))
                for _ in range(20):
                    msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                    if msg.type == web.WSMsgType.TEXT:
                        obj = json.loads(msg.data)
                        received.append(obj)
                        if obj.get("type") == "output" and obj.get("data"):
                            break

            assert fake.started
            assert "help\n" in fake.writes
            assert any(r.get("type") == "output" for r in received) or any(
                r.get("type") == "session_event" for r in received
            )

            resp2 = await self.client.get("/api/pty/sessions")
            assert resp2.status == 200
            listed_sessions = (await resp2.json())["sessions"]
            assert not any(s["session_id"] == session_id for s in listed_sessions)
