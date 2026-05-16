"""
cato/adapters/telegram.py — Telegram channel adapter (python-telegram-bot v20+).

Listens for text messages and photos via long-polling.
Provides six slash commands for runtime control:
  /start   — greeting and quick-start instructions
  /help    — describe what Cato can do
  /budget  — show current spend vs. caps
  /sessions — list active session IDs
  /kill    — terminate a specific session
  /status  — daemon uptime and adapter health

All credentials are fetched from the Vault — no hardcoded tokens.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from .base import BaseAdapter

if TYPE_CHECKING:
    from ..config import CatoConfig
    from ..gateway import Gateway
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the bot and begin long-polling."""
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

        # Register bot command menu (shown in Telegram UI)
        await self.app.bot.set_my_commands([
            BotCommand("start",    "Start a conversation with Cato"),
            BotCommand("help",     "Show what Cato can do"),
            BotCommand("budget",   "Check current budget usage"),
            BotCommand("sessions", "List active sessions"),
            BotCommand("kill",     "Kill a session: /kill <session_id>"),
            BotCommand("status",   "Show daemon status"),
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
        """Route an incoming plain-text message into the Gateway."""
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
