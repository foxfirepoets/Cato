from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import cato.core.personal_store as store
from cato.ui import server as server_module
from cato.ui.server import create_ui_app


@pytest.fixture(autouse=True)
def isolated_personal_store(tmp_path: Path):
    db_path = tmp_path / "inbox.sqlite3"
    store.init_db(db_path)
    yield
    store._DB_PATH = None


def _auth_headers() -> dict[str, str]:
    return {"X-Cato-Token": server_module._DAEMON_TOKEN}


@pytest.mark.asyncio
async def test_inbox_api_returns_pending_gmail_drafts_and_personal_store_items():
    store.save_email(
        gmail_message_id="msg-1",
        subject="Project follow-up",
        from_email="alice@example.com",
        snippet="Can you confirm?",
        draft_reply="Confirmed.",
        gmail_draft_id="draft-1",
    )
    dismissed_id = store.save_email(
        gmail_message_id="msg-2",
        subject="Already dismissed",
        from_email="bob@example.com",
        snippet="Ignore",
        draft_reply="No.",
        gmail_draft_id="draft-2",
    )
    store.update_email_status(dismissed_id, "dismissed")
    store.save_note("Ship inbox UI", "todo")
    store.save_note("Call dentist", "reminder", due_date="2026-06-01")
    store.save_note("Ben prefers concise inboxes", "memory")

    app = await create_ui_app(gateway=None)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/inbox", headers=_auth_headers())
        assert resp.status == 200
        data = await resp.json()

    assert data["counts"] == {
        "email_drafts": 1,
        "notes": 3,
        "todos": 1,
        "reminders": 1,
    }
    assert data["email_drafts"][0]["gmail_message_id"] == "msg-1"
    assert data["todos"][0]["content"] == "Ship inbox UI"
    assert data["reminders"][0]["content"] == "Call dentist"


@pytest.mark.asyncio
async def test_inbox_approve_without_gmail_adapter_durably_marks_approved_for_later_send():
    row_id = store.save_email(
        gmail_message_id="msg-approve",
        subject="Please approve",
        from_email="alice@example.com",
        snippet="Send?",
        draft_reply="Yes.",
        gmail_draft_id="draft-approve",
    )
    app = await create_ui_app(gateway=None)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/inbox/email/{row_id}/approve",
            headers=_auth_headers(),
        )
        assert resp.status == 200
        data = await resp.json()

    assert data["status"] == "approved"
    assert data["sent"] is False
    assert store.get_email_by_id(row_id)["status"] == "approved"
    approved = store.get_approved_email_drafts()
    assert [row["id"] for row in approved] == [row_id]


@pytest.mark.asyncio
async def test_inbox_approve_with_nested_gmail_adapter_sends_and_marks_sent():
    row_id = store.save_email(
        gmail_message_id="msg-send",
        subject="Please send",
        from_email="alice@example.com",
        snippet="Send?",
        draft_reply="Yes.",
        gmail_draft_id="draft-send",
    )
    gmail_adapter = SimpleNamespace(send_draft=AsyncMock())
    telegram_adapter = SimpleNamespace(_gmail_adapter=gmail_adapter)
    gateway = SimpleNamespace(_adapters=[telegram_adapter], _cfg=SimpleNamespace(mcp_enabled=False), _lanes={})
    app = await create_ui_app(gateway=gateway)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/inbox/email/{row_id}/approve",
            headers=_auth_headers(),
        )
        assert resp.status == 200
        data = await resp.json()

    gmail_adapter.send_draft.assert_awaited_once_with("draft-send")
    assert data["status"] == "sent"
    assert data["sent"] is True
    assert store.get_email_by_id(row_id)["status"] == "sent"


@pytest.mark.asyncio
async def test_inbox_dismiss_marks_pending_draft_dismissed():
    row_id = store.save_email(
        gmail_message_id="msg-dismiss",
        subject="Dismiss",
        from_email="alice@example.com",
        snippet="Dismiss?",
        draft_reply="No.",
        gmail_draft_id="draft-dismiss",
    )
    app = await create_ui_app(gateway=None)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/inbox/email/{row_id}/dismiss",
            headers=_auth_headers(),
        )
        assert resp.status == 200
        data = await resp.json()

    assert data["status"] == "dismissed"
    assert store.get_email_by_id(row_id)["status"] == "dismissed"
