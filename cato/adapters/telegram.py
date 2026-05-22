"""
cato/adapters/telegram.py — Telegram channel adapter (python-telegram-bot v20+).

Listens for text messages and photos via long-polling.
Provides slash commands for runtime control and personal assistant features:
  /start    — greeting and quick-start instructions
  /help     — describe what Cato can do
  /budget   — show current spend vs. caps
  /sessions — list active session IDs
  /kill     — terminate a specific session
  /status   — daemon uptime and adapter health
  /check    — trigger immediate Gmail check
  /today    — generate a day summary from open todos/reminders
  /notes    — show recent 10 notes
  /todos    — show open todos

All credentials are fetched from the Vault — no hardcoded tokens.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from .base import BaseAdapter

if TYPE_CHECKING:
    from ..adapters.gmail_adapter import GmailAdapter
    from ..config import CatoConfig
    from ..gateway import Gateway
    from ..router import ModelRouter
    from ..vault import Vault

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)

_TELEGRAM_MAX_LEN = 4000   # hard Telegram limit is 4096; leave headroom


class TelegramAdapter(BaseAdapter):
    """Telegram channel adapter using python-telegram-bot v20+.

    Initialisation flow:
      1. Fetch ``telegram_bot_token`` from the Vault.
      2. Build a ``telegram.ext.Application`` with long-polling.
      3. Register text, photo, and command handlers.
      4. Start polling — drop any pending updates accumulated while offline
         so the user does not receive a stale backlog.
    """

    channel_name = "telegram"

    def __init__(self, gateway: "Gateway", vault: "Vault", config: "CatoConfig") -> None:
        super().__init__(gateway, vault, config)
        self.app: Application | None = None
        self._bot_token: str | None = None

        # Wired externally after construction (Ben Assistant features)
        self._gmail_adapter: Optional["GmailAdapter"] = None
        self._router: Optional["ModelRouter"] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the bot and begin long-polling."""
        # Ensure personal_store schema exists before any handler can call it.
        # init_db is idempotent (CREATE TABLE IF NOT EXISTS) and sync.
        try:
            from cato.core import personal_store  # noqa: PLC0415
            personal_store.init_db()
        except Exception as exc:
            logger.error("Failed to initialise personal_store schema: %s", exc)

        self._bot_token = (
            self.vault.get("TELEGRAM_BOT_TOKEN")
            or self.vault.get("CATODESKTOP_BOT_TOKEN")
            or os.environ.get("CATODESKTOP_BOT_TOKEN", "").strip()
            or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        )
        if not self._bot_token:
            raise ValueError(
                "Telegram bot token not found. Set CATODESKTOP_BOT_TOKEN or TELEGRAM_BOT_TOKEN in vault/.env."
            )

        self.app = Application.builder().token(self._bot_token).build()

        # --- message handlers ---
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        self.app.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))

        # --- command handlers ---
        self.app.add_handler(CommandHandler("start",    self._cmd_start))
        self.app.add_handler(CommandHandler("help",     self._cmd_help))
        self.app.add_handler(CommandHandler("budget",   self._cmd_budget))
        self.app.add_handler(CommandHandler("sessions", self._cmd_sessions))
        self.app.add_handler(CommandHandler("kill",     self._cmd_kill))
        self.app.add_handler(CommandHandler("status",   self._cmd_status))
        self.app.add_handler(CommandHandler("check",    self._cmd_check))
        self.app.add_handler(CommandHandler("today",    self._cmd_today))
        self.app.add_handler(CommandHandler("notes",    self._cmd_notes))
        self.app.add_handler(CommandHandler("todos",    self._cmd_todos))

        # Approve/dismiss callback buttons from email notifications
        self.app.add_handler(
            CallbackQueryHandler(self._cb_email_action, pattern=r"^(approve|dismiss)_\d+$")
        )

        # Register bot command menu (shown in Telegram UI)
        await self.app.bot.set_my_commands([
            BotCommand("start",    "Start a conversation with Cato"),
            BotCommand("help",     "Show what Cato can do"),
            BotCommand("budget",   "Check current budget usage"),
            BotCommand("sessions", "List active sessions"),
            BotCommand("kill",     "Kill a session: /kill <session_id>"),
            BotCommand("status",   "Show daemon status"),
            BotCommand("check",    "Trigger immediate Gmail check"),
            BotCommand("today",    "Get a day summary from todos/reminders"),
            BotCommand("notes",    "Show recent 10 notes"),
            BotCommand("todos",    "Show open todos"),
        ])

        self.running = True
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("TelegramAdapter started (polling)")

    async def stop(self) -> None:
        """Stop polling and shut down the Application cleanly."""
        self.running = False
        if self.app:
            try:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception as exc:
                logger.warning("TelegramAdapter stop error: %s", exc)
        logger.info("TelegramAdapter stopped")

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(self, session_id: str, text: str) -> None:
        """Deliver text to the Telegram user identified by session_id.

        Splits messages longer than 4000 characters into sequential
        chunks so they fit within Telegram's 4096-character limit.
        Uses Markdown parse mode so the agent can format code blocks
        and bold/italic text naturally.
        """
        if self.app is None:
            logger.error("TelegramAdapter.send() called before start()")
            return

        user_id = session_id.split(":")[-1]
        if user_id == "main":
            # Pooled session — no single chat_id to address; skip silently.
            logger.debug("send() skipped for pooled session %s", session_id)
            return

        chunks = [text[i : i + _TELEGRAM_MAX_LEN] for i in range(0, len(text), _TELEGRAM_MAX_LEN)]
        for chunk in chunks:
            try:
                safe_chunk = escape_markdown(chunk, version=2)
                await self.app.bot.send_message(
                    chat_id=int(user_id),
                    text=safe_chunk,
                    parse_mode="MarkdownV2",
                )
            except Exception as exc:
                logger.error("Telegram send error (user=%s): %s", user_id, exc)

    # ------------------------------------------------------------------
    # Inbound message handlers
    # ------------------------------------------------------------------

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Route an incoming plain-text message into the Gateway.

        Also classifies free-text messages as personal notes (todo/memory/
        idea/reminder) and saves them to personal_store via haiku.
        """
        if update.effective_user is None or update.message is None:
            return
        user_id    = str(update.effective_user.id)
        text       = update.message.text or ""
        session_id = self.make_session_id("telegram", user_id)

        # Optimistic typing indicator — does not block ingestion
        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
        except Exception:
            pass

        await self.gateway.ingest(session_id, text, "telegram")

        # Classify and persist as a personal note (best-effort; never raises)
        if text.strip() and not text.startswith("/"):
            await self._capture_note(text, update)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle an incoming photo by passing the file URL as a synthetic message.

        Picks the highest-resolution version (last element in photo array).
        Preserves any caption the user attached.
        """
        if update.effective_user is None or update.message is None:
            return
        user_id    = str(update.effective_user.id)
        session_id = self.make_session_id("telegram", user_id)

        photo = update.message.photo[-1]   # highest resolution
        try:
            file = await context.bot.get_file(photo.file_id)
            file_url = file.file_path
        except Exception as exc:
            logger.warning("Could not fetch photo file info: %s", exc)
            file_url = f"<photo file_id={photo.file_id}>"

        caption = update.message.caption or ""
        message = f"[Image attached: {file_url}] {caption}".strip()
        await self.gateway.ingest(session_id, message, "telegram")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Greet the user and describe how to interact with Cato."""
        name = update.effective_user.first_name if update.effective_user else "there"
        await update.message.reply_text(
            f"Hi {name}, I'm Cato — your personal AI agent daemon.\n\n"
            "Just send me a message and I'll get to work. "
            "Type /help to see what I can do."
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the help text."""
        await update.message.reply_text(
            "*Cato commands:*\n"
            "/budget   — Check your spend vs. caps\n"
            "/sessions — List active session IDs\n"
            "/kill     — End a session: `/kill <session_id>`\n"
            "/status   — Daemon uptime and adapter health\n\n"
            "Or just send me any message to start a conversation.",
            parse_mode="Markdown",
        )

    async def _cmd_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Report current budget usage from the BudgetManager."""
        try:
            footer = self.gateway._budget.format_footer()
        except Exception:
            footer = "(budget info unavailable)"
        await update.message.reply_text(f"Budget status:\n{footer}")

    async def _cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List all active session IDs in the Gateway lane table."""
        lanes = list(self.gateway._lanes.keys())
        if not lanes:
            await update.message.reply_text("No active sessions.")
            return
        body = "\n".join(f"  • `{s}`" for s in lanes)
        await update.message.reply_text(
            f"*Active sessions ({len(lanes)}):*\n{body}",
            parse_mode="Markdown",
        )

    async def _cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Kill a session: /kill <session_id>."""
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /kill <session_id>")
            return
        target = args[0]
        lane = self.gateway._lanes.get(target)
        if lane is None:
            await update.message.reply_text(f"Session `{target}` not found.", parse_mode="Markdown")
            return
        try:
            await lane.stop()
            del self.gateway._lanes[target]
            await update.message.reply_text(f"Session `{target}` killed.", parse_mode="Markdown")
        except Exception as exc:
            await update.message.reply_text(f"Error killing session: {exc}")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Report daemon uptime and adapter health."""
        import time
        uptime_s = int(time.monotonic() - (self.gateway._start_time or 0))
        hours, rem   = divmod(uptime_s, 3600)
        minutes, sec = divmod(rem, 60)
        adapters = [type(a).__name__ for a in self.gateway._adapters]
        await update.message.reply_text(
            f"*Cato daemon status*\n"
            f"Uptime: {hours}h {minutes}m {sec}s\n"
            f"Active sessions: {len(self.gateway._lanes)}\n"
            f"Adapters: {', '.join(adapters) or 'none'}",
            parse_mode="Markdown",
        )

    # ------------------------------------------------------------------
    # Ben Assistant commands
    # ------------------------------------------------------------------

    async def _cmd_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Trigger an immediate Gmail check."""
        if self._gmail_adapter is None:
            await update.message.reply_text("Gmail adapter not configured.")
            return
        await update.message.reply_text("Checking Gmail now...")
        try:
            # Wire Telegram app + chat_id for this session so notifications arrive
            self._gmail_adapter._telegram_app = self.app
            self._gmail_adapter._telegram_chat_id = str(update.effective_chat.id)
            await self._gmail_adapter.check_once()
            await update.message.reply_text("Email check complete.")
        except Exception as exc:
            logger.error("/check failed: %s", exc)
            await update.message.reply_text(f"Email check failed: {exc}")

    async def _cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Generate a plain-English day summary from open todos/reminders."""
        from cato.core import personal_store  # noqa: PLC0415

        items = personal_store.get_todos_and_reminders()
        if not items:
            await update.message.reply_text("Nothing on the list today.")
            return

        if self._router is None:
            lines = "\n".join(
                f"- [{i['category']}] {i['content']}" for i in items
            )
            await update.message.reply_text(f"Open items:\n{lines}")
            return

        item_list = "\n".join(
            f"- [{i['category']}] {i['content']}"
            + (f" (due: {i['due_date']})" if i.get("due_date") else "")
            for i in items
        )
        prompt = (
            "Write a brief, plain-English daily briefing based on these open items. "
            "Be direct and concise. No preamble.\n\n"
            f"{item_list}"
        )
        try:
            from cato.swarmsync import get_swarmsync_api_key  # noqa: PLC0415
            api_key, _source = get_swarmsync_api_key(self._router._vault)
            messages = [{"role": "user", "content": prompt}]
            _model, msg = await self._router._swarmsync_complete_message(messages, api_key, 0.2)
            summary = (msg.get("content") or "").strip() or "No summary generated."
            await update.message.reply_text(summary)
        except Exception as exc:
            logger.error("/today LLM call failed: %s", exc)
            await update.message.reply_text(f"Summary generation failed: {exc}")

    async def _cmd_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the most recent 10 notes."""
        from cato.core import personal_store  # noqa: PLC0415

        notes = personal_store.get_recent_notes(10)
        if not notes:
            await update.message.reply_text("No notes yet.")
            return
        lines = []
        for n in notes:
            due = f" ({n['due_date']})" if n.get("due_date") else ""
            lines.append(f"[{n['category']}] {n['content']}{due}")
        await update.message.reply_text("\n".join(lines))

    async def _cmd_todos(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show open todos."""
        from cato.core import personal_store  # noqa: PLC0415

        todos = personal_store.get_todos_and_reminders()
        open_todos = [t for t in todos if t.get("category") == "todo"]
        if not open_todos:
            await update.message.reply_text("No open todos.")
            return
        lines = []
        for i, t in enumerate(open_todos, 1):
            due = f" (due: {t['due_date']})" if t.get("due_date") else ""
            lines.append(f"{i}. {t['content']}{due}")
        await update.message.reply_text("\n".join(lines))

    # ------------------------------------------------------------------
    # Email approve/dismiss callback
    # ------------------------------------------------------------------

    async def _cb_email_action(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle approve_<id> and dismiss_<id> callback button taps."""
        query = update.callback_query
        if query is None:
            return
        await query.answer()  # stop the loading spinner on the button

        data = query.data or ""
        match = re.match(r"^(approve|dismiss)_(\d+)$", data)
        if not match:
            await query.edit_message_text("Unknown action.")
            return

        action, row_id_str = match.group(1), match.group(2)
        row_id = int(row_id_str)

        from cato.core import personal_store  # noqa: PLC0415

        if action == "dismiss":
            personal_store.update_email_status(row_id, "dismissed")
            await query.edit_message_reply_markup(reply_markup=None)
            await query.answer("Dismissed.", show_alert=False)
            return

        # approve
        claimed = personal_store.claim_email_for_send(row_id)
        if claimed is None:
            await query.answer("Already processed.", show_alert=True)
            return

        email_row = personal_store.get_email_by_id(row_id)
        if email_row is None:
            await query.answer("Email record not found.", show_alert=True)
            return

        gmail_draft_id = email_row.get("gmail_draft_id")
        if not gmail_draft_id:
            personal_store.update_email_status(row_id, "dismissed")
            await query.answer("No draft ID — could not send.", show_alert=True)
            return

        if self._gmail_adapter is None:
            personal_store.update_email_status(row_id, "dismissed")
            await query.answer("Gmail adapter not available.", show_alert=True)
            return

        try:
            await self._gmail_adapter.send_draft(gmail_draft_id)
            personal_store.update_email_status(row_id, "sent")
            await query.edit_message_reply_markup(reply_markup=None)
            await query.answer("Sent!", show_alert=False)
        except Exception as exc:
            logger.error("send_draft failed for row %d: %s", row_id, exc)
            await query.answer(f"Send failed: {exc}"[:200], show_alert=True)

    # ------------------------------------------------------------------
    # Note capture helper
    # ------------------------------------------------------------------

    async def _capture_note(self, text: str, update: Update) -> None:
        """Classify *text* as a personal note and persist it (best-effort)."""
        from cato.core import personal_store  # noqa: PLC0415

        category = "memory"
        due_date = None

        if self._router is not None:
            prompt = (
                "Classify this note as exactly one of: todo, memory, idea, reminder.\n"
                "Also extract a due_date if mentioned (ISO YYYY-MM-DD format) or null.\n"
                "Return JSON only, no explanation: "
                '{"category": "...", "due_date": "...or null"}\n\n'
                f"<note>\n{text}\n</note>"
            )
            try:
                from cato.swarmsync import get_swarmsync_api_key  # noqa: PLC0415
                api_key, _source = get_swarmsync_api_key(self._router._vault)
                messages = [{"role": "user", "content": prompt}]
                _model, msg = await self._router._swarmsync_complete_message(
                    messages, api_key, 0.1
                )
                raw = (msg.get("content") or "").strip()
                # Tolerate ```json fences
                json_match = re.search(r"\{[\s\S]*?\}", raw)
                if json_match:
                    import json  # noqa: PLC0415
                    parsed = json.loads(json_match.group(0))
                    raw_cat = (parsed.get("category") or "memory").lower()
                    valid = {"todo", "memory", "idea", "reminder"}
                    category = raw_cat if raw_cat in valid else "memory"
                    raw_due = parsed.get("due_date")
                    if raw_due and re.match(r"^\d{4}-\d{2}-\d{2}", str(raw_due)):
                        due_date = str(raw_due)[:10]
            except Exception as exc:
                logger.debug("Note classification failed (%s), defaulting to memory", exc)

        try:
            personal_store.save_note(text, category, due_date)
            logger.debug("Note saved: [%s] %s", category, text[:50])
        except Exception as exc:
            logger.warning("Failed to save note: %s", exc)
