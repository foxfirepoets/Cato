"""
tests/test_gmail_adapter.py — Tests for cato.adapters.gmail_adapter.

All Gmail API calls are mocked; no real HTTP requests are made.
personal_store is isolated with a temp-file DB.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cato.adapters.gmail_adapter import (
    GmailAdapter,
    _extract_plain_text,
)
import cato.core.personal_store as store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path):
    db_path = tmp_path / "gmail_test.sqlite3"
    store.init_db(db_path)
    yield db_path
    store._DB_PATH = None


def _make_vault(**kwargs) -> MagicMock:
    vault = MagicMock()
    vault.get.side_effect = lambda key: kwargs.get(key, "")
    return vault


def _make_adapter(**vault_kwargs) -> GmailAdapter:
    vault = _make_vault(**vault_kwargs)
    adapter = GmailAdapter(vault=vault)
    return adapter


# ---------------------------------------------------------------------------
# _extract_plain_text tests (pure, no I/O)
# ---------------------------------------------------------------------------

def test_extract_plain_text_from_simple_payload():
    text = "Hello, world!"
    encoded = base64.urlsafe_b64encode(text.encode()).decode()
    payload = {"mimeType": "text/plain", "body": {"data": encoded}}
    result = _extract_plain_text(payload)
    assert result == text


def test_extract_plain_text_from_multipart():
    """text/plain part should be returned when it appears before the HTML part."""
    plain_text = "Plain body"
    encoded = base64.urlsafe_b64encode(plain_text.encode()).decode()
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            # text/plain listed first — should be returned
            {
                "mimeType": "text/plain",
                "body": {"data": encoded},
            },
            {
                "mimeType": "text/html",
                "body": {"data": base64.urlsafe_b64encode(b"<p>HTML</p>").decode()},
            },
        ],
    }
    result = _extract_plain_text(payload)
    assert result == plain_text


def test_extract_plain_text_html_fallback():
    html = "<p>Hello <b>world</b></p>"
    encoded = base64.urlsafe_b64encode(html.encode()).decode()
    payload = {"mimeType": "text/html", "body": {"data": encoded}}
    result = _extract_plain_text(payload)
    # HTML tags stripped; text should contain "Hello" and "world"
    assert "Hello" in result
    assert "world" in result
    assert "<p>" not in result


def test_extract_plain_text_empty_payload():
    assert _extract_plain_text({}) == ""
    assert _extract_plain_text(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Marketing email classification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_marketing_email_is_skipped():
    """When isMarketingEmail returns True, no row should be saved to the DB."""
    adapter = _make_adapter(
        GMAIL_CLIENT_ID="cid",
        GMAIL_CLIENT_SECRET="csec",
        GMAIL_REFRESH_TOKEN="rtoken",
    )

    # Patch LLM classification to always return "marketing"
    async def _mock_is_marketing(*args, **kwargs):
        return True

    adapter._is_marketing_email = _mock_is_marketing

    fake_emails = [
        {
            "id": "mkt001",
            "thread_id": "t001",
            "subject": "50% OFF sale today only!",
            "from_email": "noreply@spam.com",
            "snippet": "Huge discounts",
            "body": "Buy now save big",
        }
    ]

    # Mock Gmail service methods used in _process_email
    mock_service = MagicMock()
    mock_service.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}

    loop = asyncio.get_event_loop()

    with patch.object(adapter, "_mark_as_read_sync") as mock_mark:
        for email_data in fake_emails:
            await adapter._process_email(email_data, mock_service, loop)

    # No email should be persisted
    assert store.get_email_by_gmail_id("mkt001") is None
    # markAsRead should still be called
    mock_mark.assert_called_once_with(mock_service, "mkt001")


@pytest.mark.asyncio
async def test_non_marketing_email_is_saved():
    """When isMarketingEmail returns False, email should be saved."""
    adapter = _make_adapter(
        GMAIL_CLIENT_ID="cid",
        GMAIL_CLIENT_SECRET="csec",
        GMAIL_REFRESH_TOKEN="rtoken",
    )

    async def _mock_not_marketing(*args, **kwargs):
        return False

    adapter._is_marketing_email = _mock_not_marketing

    async def _mock_draft(*args, **kwargs):
        return "Thank you for your email."

    adapter._draft_email_reply = _mock_draft

    fake_emails = [
        {
            "id": "real001",
            "thread_id": "t002",
            "subject": "Project update",
            "from_email": "colleague@work.com",
            "snippet": "Just checking in",
            "body": "Hey, quick update on the project...",
        }
    ]

    mock_service = MagicMock()
    mock_service.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
    mock_service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
        "id": "draft-xyz"
    }

    loop = asyncio.get_event_loop()

    with patch.object(adapter, "_mark_as_read_sync"):
        with patch.object(adapter, "_create_draft_sync", return_value="draft-xyz"):
            await adapter._process_email(fake_emails[0], mock_service, loop)

    saved = store.get_email_by_gmail_id("real001")
    assert saved is not None
    assert saved["subject"] == "Project update"
    assert saved["draft_reply"] == "Thank you for your email."


# ---------------------------------------------------------------------------
# check_once idempotency guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_once_idempotency():
    """Calling check_once while another check is in progress is a no-op."""
    adapter = _make_adapter(GMAIL_REFRESH_TOKEN="rtoken")
    adapter._check_in_progress = True  # simulate an in-progress check

    called = []

    async def _fake_do_check():
        called.append(True)

    adapter._do_check = _fake_do_check  # type: ignore[method-assign]
    await adapter.check_once()
    assert called == []  # must NOT have been called
