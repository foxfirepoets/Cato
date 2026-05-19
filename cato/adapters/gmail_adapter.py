"""
cato/adapters/gmail_adapter.py — Async Gmail polling adapter for Cato.

Polls Gmail every 15 minutes, drafts replies via SwarmSync-routed sonnet,
and sends Telegram notifications with approve/dismiss inline-keyboard buttons.

Credentials are pulled from Cato's vault:
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN

Environment:
  BEN_VOICE_PATH — override path to voice profile (default: Ben Assistant
                   directory next to this repo; falls back to generic prompt)
"""

from __future__ import annotations

import asyncio
import base64
import email as _email_lib
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from cato.vault import Vault
    from cato.router import ModelRouter

logger = logging.getLogger(__name__)

# Default path next to the Cato project on this machine.
_DEFAULT_VOICE_PATH = Path(r"C:\Users\Administrator\Desktop\Ben Assistant\voice\ben-voice.txt")

_POLL_INTERVAL = 15 * 60  # seconds
_RATE_LIMIT_SLEEP = 1.1   # seconds between Telegram notifications


def _load_voice_profile() -> str:
    voice_path_env = os.environ.get("BEN_VOICE_PATH", "")
    candidates = [
        Path(voice_path_env) if voice_path_env else None,
        _DEFAULT_VOICE_PATH,
    ]
    for path in candidates:
        if path and path.exists():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
    return ""


def _extract_plain_text(payload: dict[str, Any]) -> str:
    """Recursively extract plain-text body from a Gmail message payload dict."""
    if not payload:
        return ""

    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})

    if mime_type == "text/plain" and body.get("data"):
        raw = base64.urlsafe_b64decode(body["data"] + "==")
        return raw.decode("utf-8", errors="replace")

    parts = payload.get("parts", [])
    if parts:
        # Two-pass scan: prefer text/plain over text/html regardless of part order
        for part in parts:
            if (part.get("mimeType") == "text/plain" and part.get("body", {}).get("data")):
                text = _extract_plain_text(part)
                if text:
                    return text
        # Second pass: recurse into multipart sub-parts, then accept HTML
        for part in parts:
            if part.get("mimeType") != "text/plain":
                text = _extract_plain_text(part)
                if text:
                    return text

    # Fallback: strip HTML tags if only HTML part exists
    if mime_type == "text/html" and body.get("data"):
        raw = base64.urlsafe_b64decode(body["data"] + "==")
        html = raw.decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", html).strip()

    return ""


def _build_raw_email(to: str, subject: str, body: str, thread_id: str | None) -> bytes:
    """Return a base64url-encoded RFC 2822 message suitable for Gmail API."""
    reply_subject = subject if subject.startswith("Re: ") else f"Re: {subject}"
    lines = [
        f"To: {to}",
        f"Subject: {reply_subject}",
        "Content-Type: text/plain; charset=utf-8",
        "MIME-Version: 1.0",
        "",
        body,
    ]
    raw = "\n".join(lines).encode("utf-8")
    return base64.urlsafe_b64encode(raw)


class GmailAdapter:
    """Async Gmail poller that runs inside Cato's event loop.

    Lifecycle:
      - Call ``start()`` as an asyncio task to begin polling.
      - Call ``stop()`` to cancel the polling loop.

    After construction, wire external dependencies via attributes before
    calling ``start()``:
      - ``_router``         — ModelRouter instance
      - ``_telegram_app``   — python-telegram-bot Application (for sending notifications)
      - ``_telegram_chat_id`` — Telegram chat ID to send email notifications to
    """

    def __init__(self, vault: "Vault") -> None:
        self._vault = vault
        self._running = False
        self._task: asyncio.Task | None = None
        self._check_in_progress = False

        # Wired externally after construction
        self._router: Optional["ModelRouter"] = None
        self._telegram_app: Any = None   # telegram.ext.Application
        self._telegram_chat_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the polling loop (run as an asyncio task)."""
        self._running = True
        logger.info("GmailAdapter started (poll interval: %ds)", _POLL_INTERVAL)
        while self._running:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("GmailAdapter poll error: %s", exc)
            try:
                await asyncio.sleep(_POLL_INTERVAL)
            except asyncio.CancelledError:
                break

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("GmailAdapter stopped")

    # ------------------------------------------------------------------
    # Gmail API helpers (sync, run in executor)
    # ------------------------------------------------------------------

    def _get_gmail_service(self) -> Any:
        """Build an authenticated Gmail API service using vault credentials."""
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        client_id = self._vault.get("GMAIL_CLIENT_ID") or ""
        client_secret = self._vault.get("GMAIL_CLIENT_SECRET") or ""
        refresh_token = self._vault.get("GMAIL_REFRESH_TOKEN") or ""

        if not all([client_id, client_secret, refresh_token]):
            raise ValueError(
                "Gmail credentials not configured in vault. "
                "Set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN."
            )

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _fetch_unread_emails_sync(self, service: Any) -> list[dict[str, Any]]:
        """Blocking Gmail API calls — run via run_in_executor. Accepts a pre-built service."""
        result = (
            service.users()
            .messages()
            .list(userId="me", q="is:unread in:inbox", maxResults=50)
            .execute()
        )
        messages = result.get("messages", [])
        emails = []

        for msg in messages:
            try:
                full = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg["id"], format="full")
                    .execute()
                )
                headers = full.get("payload", {}).get("headers", [])
                subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(no subject)")
                from_raw = next((h["value"] for h in headers if h["name"] == "From"), "")
                # Extract bare email address
                match = re.search(r"<([^<>]+@[^<>]+)>", from_raw)
                from_email = match.group(1) if match else from_raw

                body = _extract_plain_text(full.get("payload", {}))
                emails.append(
                    {
                        "id": msg["id"],
                        "thread_id": full.get("threadId"),
                        "subject": subject,
                        "from_email": from_email,
                        "snippet": full.get("snippet", ""),
                        "body": body,
                    }
                )
            except Exception as exc:
                logger.warning("Failed to fetch email %s: %s", msg.get("id"), exc)

        return emails

    def _mark_as_read_sync(self, service: Any, message_id: str) -> None:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()

    def _create_draft_sync(
        self, service: Any, to: str, subject: str, body: str, thread_id: str | None
    ) -> str:
        raw = _build_raw_email(to, subject, body, thread_id).decode("ascii")
        draft_body: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            draft_body["message"]["threadId"] = thread_id
        res = service.users().drafts().create(userId="me", body=draft_body).execute()
        return res["id"]

    def _send_draft_sync(self, draft_id: str) -> None:
        service = self._get_gmail_service()
        service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        logger.info("Gmail draft %s sent", draft_id)

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------

    async def _is_marketing_email(self, from_email: str, subject: str, snippet: str) -> bool:
        if self._router is None:
            return False

        prompt = (
            "Is this email marketing/promotional or spam? "
            "Respond with exactly 'yes' or 'no'. No other text.\n\n"
            f"<from>{from_email}</from>\n"
            f"<subject>{subject}</subject>\n"
            f"<snippet>\n{snippet}\n</snippet>"
        )
        try:
            from cato.swarmsync import get_swarmsync_api_key  # noqa: PLC0415
            api_key, _source = get_swarmsync_api_key(self._router._vault)
            messages = [{"role": "user", "content": prompt}]
            _model, msg = await self._router._swarmsync_complete_message(messages, api_key, 0.1)
            text = (msg.get("content") or "").strip().lower().rstrip(".")
            return text == "yes"
        except Exception as exc:
            logger.warning("isMarketingEmail LLM call failed (%s), allowing through", exc)
            return False

    async def _draft_email_reply(self, subject: str, body: str, from_email: str) -> str:
        if self._router is None:
            return "(LLM router not available — draft not generated)"

        voice_profile = _load_voice_profile()
        if voice_profile:
            system_prompt = (
                "You are drafting email replies. Write exactly as the owner would.\n"
                f"Voice profile:\n\n{voice_profile}"
            )
        else:
            system_prompt = (
                "You are drafting email replies. Keep replies short, direct, and professional."
            )

        prompt = (
            "Draft a reply to this email:\n\n"
            f"<from>{from_email}</from>\n"
            f"<subject>{subject}</subject>\n"
            f"<email_body>\n{body}\n</email_body>\n\n"
            "Return only the reply text. No subject line. No 'Draft:' prefix. No preamble."
        )

        try:
            from cato.swarmsync import get_swarmsync_api_key  # noqa: PLC0415
            api_key, _source = get_swarmsync_api_key(self._router._vault)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
            _model, msg = await self._router._swarmsync_complete_message(messages, api_key, 0.6)
            return (msg.get("content") or "").strip()
        except Exception as exc:
            logger.error("draftEmailReply LLM call failed: %s", exc)
            return "(draft generation failed)"

    # ------------------------------------------------------------------
    # Core check loop
    # ------------------------------------------------------------------

    async def check_once(self) -> None:
        """Fetch unread Gmail, draft replies, notify via Telegram."""
        if self._check_in_progress:
            logger.warning("Email check already in progress — skipping overlapping run")
            return

        self._check_in_progress = True
        try:
            await self._do_check()
        finally:
            self._check_in_progress = False

    async def _do_check(self) -> None:
        from cato.core import personal_store  # noqa: PLC0415

        loop = asyncio.get_event_loop()

        # Validate credentials before hitting the API
        refresh_token = self._vault.get("GMAIL_REFRESH_TOKEN") or ""
        if not refresh_token:
            logger.warning("GMAIL_REFRESH_TOKEN not set in vault — skipping email check")
            return

        # Build service once for the entire check cycle
        try:
            service = await loop.run_in_executor(None, self._get_gmail_service)
        except Exception as exc:
            logger.error("Could not build Gmail service: %s", exc)
            if "invalid_grant" in str(exc).lower():
                await self._send_telegram_text(
                    "Gmail auth expired. Re-run setup to reconnect Gmail."
                )
            return

        try:
            emails = await loop.run_in_executor(None, self._fetch_unread_emails_sync, service)
        except Exception as exc:
            logger.error("Failed to fetch emails: %s", exc)
            return

        if not emails:
            logger.debug("No new unread emails")
            return

        for email_data in emails:
            try:
                await self._process_email(email_data, service, loop)
            except Exception as exc:
                logger.error("Failed to process email %s: %s", email_data.get("id"), exc)

            await asyncio.sleep(_RATE_LIMIT_SLEEP)

    async def _process_email(
        self, email_data: dict[str, Any], service: Any, loop: asyncio.AbstractEventLoop
    ) -> None:
        from cato.core import personal_store  # noqa: PLC0415

        gmail_id = email_data["id"]
        existing = personal_store.get_email_by_gmail_id(gmail_id)
        if existing:
            return

        # Marketing check via haiku
        is_marketing = await self._is_marketing_email(
            email_data["from_email"],
            email_data["subject"],
            email_data["snippet"],
        )
        if is_marketing:
            logger.info("Skipped marketing email: %s", email_data["subject"])
            await loop.run_in_executor(
                None, self._mark_as_read_sync, service, gmail_id
            )
            return

        # Draft reply via sonnet through SwarmSync
        draft_text = await self._draft_email_reply(
            email_data["subject"],
            email_data["body"],
            email_data["from_email"],
        )

        # Persist email + draft (no gmail_draft_id yet)
        row_id = personal_store.save_email(
            gmail_id,
            email_data["subject"],
            email_data["from_email"],
            email_data["snippet"],
            draft_text,
            None,
        )

        # Create the actual Gmail draft
        try:
            gmail_draft_id = await loop.run_in_executor(
                None,
                self._create_draft_sync,
                service,
                email_data["from_email"],
                email_data["subject"],
                draft_text,
                email_data.get("thread_id"),
            )
            personal_store.update_email_draft_id(row_id, gmail_draft_id)
        except Exception as exc:
            logger.error("Failed to create Gmail draft for email %s: %s", gmail_id, exc)
            gmail_draft_id = None

        # Send Telegram notification with approve/dismiss buttons
        await self._notify_telegram(email_data, draft_text, row_id)

        # Mark as read
        try:
            await loop.run_in_executor(
                None, self._mark_as_read_sync, service, gmail_id
            )
        except Exception as exc:
            logger.warning("Could not mark email %s as read: %s", gmail_id, exc)

        logger.info("Email processed: %s", email_data["subject"])

    async def _notify_telegram(
        self,
        email_data: dict[str, Any],
        draft_text: str,
        row_id: int,
    ) -> None:
        if self._telegram_app is None or self._telegram_chat_id is None:
            logger.debug("No Telegram app/chat_id wired — skipping notification")
            return

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: PLC0415

        subject = email_data.get("subject", "(no subject)")
        from_email = email_data.get("from_email", "")
        snippet = email_data.get("snippet", "")

        # Truncate draft preview for the notification
        preview = draft_text[:300] + ("..." if len(draft_text) > 300 else "")

        text = (
            f"<b>New email from:</b> {from_email}\n"
            f"<b>Subject:</b> {subject}\n"
            f"<b>Snippet:</b> {snippet[:200]}\n\n"
            f"<b>Draft reply:</b>\n{preview}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve & Send", callback_data=f"approve_{row_id}"),
                    InlineKeyboardButton("Dismiss", callback_data=f"dismiss_{row_id}"),
                ]
            ]
        )

        try:
            await self._telegram_app.bot.send_message(
                chat_id=self._telegram_chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.error("Failed to send Telegram email notification: %s", exc)

    async def _send_telegram_text(self, text: str) -> None:
        if self._telegram_app is None or self._telegram_chat_id is None:
            return
        try:
            await self._telegram_app.bot.send_message(
                chat_id=self._telegram_chat_id, text=text
            )
        except Exception as exc:
            logger.warning("Telegram send error: %s", exc)

    # ------------------------------------------------------------------
    # Public: send an approved draft
    # ------------------------------------------------------------------

    async def send_draft(self, draft_id: str) -> None:
        """Send a Gmail draft identified by *draft_id*."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._send_draft_sync, draft_id)
