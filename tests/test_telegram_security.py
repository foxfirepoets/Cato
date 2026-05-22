"""Security regression tests for the Telegram adapter."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_adapter():
    """Build a TelegramAdapter with fully mocked dependencies."""
    gateway = MagicMock()
    vault = MagicMock()
    vault.get.return_value = ""  # no token by default
    config = MagicMock()
    config.agent_name = "cato"
    config.dm_scope = "per-channel-peer"

    from cato.adapters.telegram import TelegramAdapter
    return TelegramAdapter(gateway=gateway, vault=vault, config=config)


@pytest.mark.asyncio
async def test_telegram_adapter_init_db_called_on_start():
    """personal_store.init_db must be called when start() is invoked."""
    adapter = _make_adapter()
    # Provide a bot token so we don't error on missing token
    adapter.vault.get.side_effect = lambda key: (
        "fake-token:AAtest" if "TOKEN" in key else ""
    )

    with patch("cato.adapters.telegram.Application") as mock_app_cls:
        # Wire up the Application builder chain
        mock_app = MagicMock()
        mock_app.bot = AsyncMock()
        mock_app.bot.set_my_commands = AsyncMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.updater = MagicMock()
        mock_app.updater.start_polling = AsyncMock()
        mock_app.add_handler = MagicMock()
        mock_app_cls.builder.return_value.token.return_value.build.return_value = mock_app

        # Patch init_db at the actual module path it lives in
        import cato.core.personal_store as ps_mod
        with patch.object(ps_mod, "init_db") as mock_init_db:
            await adapter.start()

    mock_init_db.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_send_without_bot_raises_or_logs(caplog):
    """Calling send() before start() must log an error, not crash silently."""
    adapter = _make_adapter()
    # app is None (never started)
    assert adapter.app is None

    with caplog.at_level(logging.ERROR, logger="cato.adapters.telegram"):
        await adapter.send("main:telegram:12345", "hello")

    assert any("start" in r.message.lower() or "before" in r.message.lower()
               for r in caplog.records), (
        "Expected an error log when send() called before start()"
    )
