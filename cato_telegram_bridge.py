"""
cato_telegram_bridge.py — Standalone Telegram <-> Claude Code CLI bridge.

TWO-TIER EXECUTION MODEL:
  Tier 1 — Conversational (claude -p): simple questions, status checks,
            short tasks. 300s timeout. Runs inline.
  Tier 2 — Pipeline/Build (claude interactive): complex orchestration,
            multi-step builds, Codex hand-off, Ralph Loop. Runs as a
            background subprocess writing to a log file, with periodic
            Telegram progress updates streamed back to the user.

The bridge auto-classifies incoming messages and routes them correctly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# ── Cato path ──────────────────────────────────────────────────────────────
CATO_ROOT = Path(r"C:\Users\Administrator\Desktop\Cato")
sys.path.insert(0, str(CATO_ROOT))
ENV_PATH = CATO_ROOT / ".env"

try:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH, override=False)
except ImportError:
    pass

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cato_telegram_bridge")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)

# ── python-telegram-bot ────────────────────────────────────────────────────
try:
    from telegram import Bot, Update
    from telegram.constants import ChatAction
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError as exc:
    logger.critical("python-telegram-bot not found. pip install 'python-telegram-bot>=20.0'")
    raise SystemExit(1) from exc


# ── Constants ──────────────────────────────────────────────────────────────
# Tier 1: simple claude -p calls
CLAUDE_TIMEOUT          = 300   # max seconds for a simple conversational reply
CLAUDE_HEARTBEAT_SECS   = 30    # send "still thinking" every N seconds

# Tier 2: background pipeline/build runs
PIPELINE_POLL_SECS      = 20    # how often to check pipeline log for new output
PIPELINE_MAX_HOURS      = 3     # hard kill after this many hours
PIPELINE_TAIL_LINES     = 15    # lines of log to send in each progress update

MAX_TELEGRAM_MSG_LEN    = 4000
STARTUP_TIME            = time.monotonic()

# ── Bot configuration ──────────────────────────────────────────────────────
BRIDGE_BOT_TOKEN_ENV_KEYS = (
    "CATODESKTOP_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "CATO_TELEGRAM_BOT_TOKEN",
)
BRIDGE_BOT_USERNAME_ENV_KEYS = (
    "CATODESKTOP_BOT_USERNAME",
    "TELEGRAM_BOT_USERNAME",
    "CATO_TELEGRAM_BOT_USERNAME",
)

# CWD for claude calls — loads CLAUDE.md + MEMORY.md
CLAUDE_CWD = r"C:\Users\Administrator\Desktop\Cato"

# ── Keyword patterns that trigger Tier 2 (background pipeline) ─────────────
_PIPELINE_PATTERNS = re.compile(
    r"\b("
    r"build|codex|ralph|loop|pipeline|phase|construct|deploy|scaffold|"
    r"hand.?off|hand it|give.?(to|him)|pass.?(to|it)|"
    r"run.?(the|a).*(pipeline|phase|build|loop)|"
    r"start.*(build|pipeline|phase)|"
    r"make.*(site|website|app|saas)|"
    r"create.*(site|website|app|saas)"
    r")\b",
    re.IGNORECASE,
)


def _is_pipeline_request(text: str) -> bool:
    """Return True if the message looks like a complex multi-step build task."""
    return bool(_PIPELINE_PATTERNS.search(text))


# ── Shared state ───────────────────────────────────────────────────────────
_chat_queues:   dict[int, asyncio.Queue] = {}
_chat_workers:  dict[int, asyncio.Task]  = {}
_bg_pipelines:  dict[int, dict]          = {}   # chat_id -> {proc, log_path, task}


# ── Access control ─────────────────────────────────────────────────────────
def _allowed_chat_ids_from_env() -> set[int]:
    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    ids: set[int] = set()
    for value in re.split(r"[,;\s]+", raw):
        value = value.strip()
        if not value:
            continue
        try:
            ids.add(int(value))
        except ValueError:
            logger.warning("Ignoring invalid TELEGRAM_CHAT_ID value: %r", value)
    return ids or {5846582379}


# Telegram chat IDs allowed to drive this bridge. Chat IDs are identifiers,
# not secrets. Prefer .env so the bridge follows the currently configured user.
ALLOWED_CHAT_IDS: set[int] = _allowed_chat_ids_from_env()


def _is_authorized(update: Update) -> bool:
    """Return True if the update originates from an allowlisted chat."""
    chat = update.effective_chat if update else None
    return chat is not None and chat.id in ALLOWED_CHAT_IDS


async def _reject_unauthorized(update: Update) -> bool:
    """
    Gate inbound Telegram updates against ALLOWED_CHAT_IDS.

    Returns True if the update was rejected (caller must return immediately).
    Returns False if the update is authorized and processing should continue.
    """
    if _is_authorized(update):
        return False
    chat = update.effective_chat if update else None
    user = update.effective_user if update else None
    chat_id  = chat.id if chat else None
    username = (user.username if user else None) or "(no username)"
    name = ""
    if user:
        name = ((user.first_name or "") + " " + (user.last_name or "")).strip()
    logger.warning(
        "Rejected unauthorized update: chat_id=%s username=%s name=%s",
        chat_id, username, name,
    )
    try:
        if update and update.message:
            await update.message.reply_text("Unauthorized.")
    except Exception:
        pass
    return True


# ── Env + CLI helpers ──────────────────────────────────────────────────────

def _resolve_token() -> str:
    override = os.environ.get("CLAUDE_BRIDGE_TOKEN", "").strip()
    if override:
        return override
    for key in BRIDGE_BOT_TOKEN_ENV_KEYS:
        token = os.environ.get(key, "").strip()
        if token:
            logger.info("Using Telegram bridge bot token from %s.", key)
            return token
    raise RuntimeError(
        "No bridge bot token configured. Set CATODESKTOP_BOT_TOKEN or TELEGRAM_BOT_TOKEN in .env."
    )


def _resolve_configured_username() -> str:
    for key in BRIDGE_BOT_USERNAME_ENV_KEYS:
        username = os.environ.get(key, "").strip().lstrip("@")
        if username:
            return username
    return ""


def _build_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE", None)
    return env


def _find_claude() -> Optional[str]:
    # Try PATH first, then fall back to known install locations
    found = shutil.which("claude")
    if found:
        return found
    for candidate in [
        r"C:\Users\Administrator\.local\bin\claude.exe",
        r"C:\Users\Administrator\.local\bin\claude",
    ]:
        if Path(candidate).exists():
            return candidate
    return None


def _split_message(text: str, limit: int = MAX_TELEGRAM_MSG_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text); break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def _send_long(update: Update, text: str) -> None:
    for chunk in _split_message(text):
        await update.message.reply_text(chunk)


async def _bot_send(bot, chat_id: int, text: str) -> None:
    for chunk in _split_message(text):
        try:
            await bot.send_message(chat_id=chat_id, text=chunk)
        except Exception as e:
            logger.warning("Failed to send message to %s: %s", chat_id, e)


async def _log_bot_identity(token: str) -> None:
    """Verify the configured token and compare it to the optional username."""
    bot = Bot(token=token)
    me = await bot.get_me()
    actual_username = (me.username or "").lstrip("@")
    configured_username = _resolve_configured_username()
    if configured_username and actual_username.lower() != configured_username.lower():
        raise RuntimeError(
            "Configured Telegram bot username does not match token: "
            f".env has @{configured_username}, Telegram returned @{actual_username or 'unknown'}."
        )
    logger.info(
        "Telegram bridge bot verified: id=%s username=@%s allowed_chats=%s",
        me.id,
        actual_username or "unknown",
        sorted(ALLOWED_CHAT_IDS),
    )


# ── TIER 1: Simple conversational reply ────────────────────────────────────

async def _run_claude_simple(message: str, update: Update) -> str:
    """
    Fire claude -p for a simple conversational message.
    Sends heartbeat messages every CLAUDE_HEARTBEAT_SECS so Telegram
    doesn't look silent.
    """
    claude_path = _find_claude()
    if not claude_path:
        return "ERROR: `claude` CLI not found on PATH."

    CREATE_NO_WINDOW = 0x08000000
    env = _build_env()

    proc = await asyncio.create_subprocess_exec(
        claude_path, "-p", message, "--output-format", "text",
        "--dangerously-skip-permissions",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=CLAUDE_CWD,
        creationflags=CREATE_NO_WINDOW,
    )

    # Heartbeat while waiting
    async def _heartbeat():
        elapsed = 0
        while True:
            await asyncio.sleep(CLAUDE_HEARTBEAT_SECS)
            elapsed += CLAUDE_HEARTBEAT_SECS
            try:
                await update.message.reply_text(f"⏳ Still thinking... ({elapsed}s)")
            except Exception:
                pass

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return (
            f"⚠️ That took longer than {CLAUDE_TIMEOUT}s to answer.\n\n"
            "If this is a build/pipeline task, use:\n"
            "/build <description>\n\n"
            "That runs it as a background job with progress updates."
        )
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except (asyncio.CancelledError, Exception):
            pass

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        detail = stderr or stdout or "(no output)"
        return f"ERROR (exit {proc.returncode}):\n{detail}"

    return stdout or "(Claude returned an empty response.)"


# ── TIER 2: Background pipeline runner ─────────────────────────────────────

def _tail_file(path: Path, n: int = PIPELINE_TAIL_LINES) -> str:
    """Return last N lines of a file."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:]) if lines else "(no output yet)"
    except Exception:
        return "(log not readable yet)"


async def _run_pipeline_background(
    message: str,
    chat_id: int,
    bot,
    log_path: Path,
) -> None:
    """
    Run claude as a detached background subprocess, piping all output to
    log_path. Stream tail updates to Telegram every PIPELINE_POLL_SECS.
    """
    claude_path = _find_claude()
    if not claude_path:
        await _bot_send(bot, chat_id, "ERROR: claude CLI not found.")
        return

    CREATE_NO_WINDOW = 0x08000000
    env = _build_env()

    log_handle = open(log_path, "w", encoding="utf-8")

    try:
        proc = subprocess.Popen(
            [claude_path, "-p", message, "--output-format", "text",
             "--dangerously-skip-permissions"],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            env=env,
            cwd=CLAUDE_CWD,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        log_handle.close()
        await _bot_send(bot, chat_id, f"Failed to launch Claude: {e}")
        return

    # Store proc reference for /stop
    if chat_id in _bg_pipelines:
        _bg_pipelines[chat_id]["proc"] = proc

    start = time.monotonic()
    max_secs = PIPELINE_MAX_HOURS * 3600
    last_sent = ""

    await _bot_send(bot, chat_id,
        "🚀 Background job started. I'll stream progress every "
        f"{PIPELINE_POLL_SECS}s.\n\nSend /stop to cancel."
    )

    while True:
        await asyncio.sleep(PIPELINE_POLL_SECS)
        elapsed = int(time.monotonic() - start)

        # Check if done
        ret = proc.poll()
        tail = _tail_file(log_path)

        if ret is not None:
            log_handle.close()
            status = "✅ Complete" if ret == 0 else f"⚠️ Exited (code {ret})"
            summary = (
                f"{status} in {elapsed//60}m {elapsed%60}s\n\n"
                f"Last output:\n```\n{tail}\n```"
            )
            await _bot_send(bot, chat_id, summary)
            _bg_pipelines.pop(chat_id, None)
            return

        # Still running — send update only if output changed
        if elapsed > max_secs:
            proc.kill()
            log_handle.close()
            await _bot_send(bot, chat_id,
                f"⏱️ Job killed after {PIPELINE_MAX_HOURS}h (safety limit).\n"
                f"Last output:\n```\n{tail}\n```"
            )
            _bg_pipelines.pop(chat_id, None)
            return

        # Only send if output has changed since last update
        if tail != last_sent:
            await _bot_send(bot, chat_id,
                f"⚙️ Running ({elapsed//60}m {elapsed%60}s)...\n```\n{tail}\n```"
            )
            last_sent = tail
        else:
            # No new output — just send a heartbeat dot
            try:
                await bot.send_message(chat_id=chat_id,
                    text=f"⚙️ Still running... ({elapsed//60}m {elapsed%60}s) — no new output yet")
            except Exception:
                pass


async def _launch_pipeline(
    message: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Launch a Tier 2 background pipeline job."""
    chat_id = update.effective_chat.id

    # Kill existing job for this chat if any
    existing = _bg_pipelines.get(chat_id)
    if existing:
        proc = existing.get("proc")
        task = existing.get("task")
        if proc and proc.poll() is None:
            proc.kill()
        if task and not task.done():
            task.cancel()
        await update.message.reply_text("⛔ Cancelled previous background job.")

    # Create log file
    log_dir = CATO_ROOT / "logs" / "pipeline_jobs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"job_{chat_id}_{int(time.time())}.log"

    task = asyncio.create_task(
        _run_pipeline_background(message, chat_id, context.bot, log_path)
    )
    _bg_pipelines[chat_id] = {"task": task, "proc": None, "log": log_path}

    await update.message.reply_text(
        f"📋 Got it. Routing to background job runner.\n"
        f"Log: {log_path.name}\n\n"
        f"Message sent to Claude:\n\"{message[:200]}{'...' if len(message)>200 else ''}\""
    )


# ── Chat worker (Tier 1 queue) ─────────────────────────────────────────────

async def _chat_worker(chat_id: int) -> None:
    """Process Tier 1 messages one at a time per chat."""
    queue = _chat_queues[chat_id]
    while True:
        update, context = await queue.get()
        try:
            user_text = update.message.text or ""
            response = await _run_claude_simple(user_text, update)
            await _send_long(update, response)
        except Exception as exc:
            logger.exception("Error in chat worker for %s", chat_id)
            try:
                await update.message.reply_text(f"Sorry, something went wrong: {exc}")
            except Exception:
                pass
        finally:
            queue.task_done()


# ── Command handlers ───────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    await update.message.reply_text(
        "Cato Telegram bridge is online.\n\n"
        "Commands:\n"
        "  /build <task> — run as background job (for pipelines, builds, Codex hand-offs)\n"
        "  /stop — cancel running background job\n"
        "  /status — bridge status\n"
        "  /help — this message\n\n"
        "Or just type any message for a quick Cato reply.\n\n"
        "Tip: Complex multi-step tasks (build a site, run pipeline, hand to Codex) "
        "work best with /build — they run in the background with live progress updates."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    await update.message.reply_text(
        "📖 How to use this bridge:\n\n"
        "QUICK QUESTIONS (type directly):\n"
        "  \"What's the status of AgentOptimize?\"\n"
        "  \"What domain should I register?\"\n"
        "  \"Explain what context score means\"\n\n"
        "BACKGROUND JOBS (use /build):\n"
        "  /build go to the AgentOptimize folder and hand Codex the phase specs to build the site\n"
        "  /build run phase 3 brand identity for conduitscore\n"
        "  /build start the one-shot pipeline for AI visibility scoring SaaS\n\n"
        "OTHER COMMANDS:\n"
        "  /stop — kill running background job\n"
        "  /status — uptime, Claude path, active jobs\n"
        "  /pipeline <idea> — alias for /build (pipeline mode)\n"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    uptime_s = int(time.monotonic() - STARTUP_TIME)
    h, rem = divmod(uptime_s, 3600)
    m, s = divmod(rem, 60)
    claude_path = _find_claude()
    chat_id = update.effective_chat.id
    bg = _bg_pipelines.get(chat_id)
    bg_status = "none"
    if bg:
        proc = bg.get("proc")
        if proc and proc.poll() is None:
            bg_status = f"running (log: {Path(bg['log']).name})"
        else:
            bg_status = "finished"

    await update.message.reply_text(
        f"🟢 Bridge Status\n"
        f"  Uptime: {h}h {m}m {s}s\n"
        f"  Claude CLI: {'✅ ' + claude_path if claude_path else '❌ NOT FOUND'}\n"
        f"  Background job: {bg_status}\n"
        f"  CWD: {CLAUDE_CWD}"
    )


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/build <task> — always Tier 2 background job."""
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /build <what you want Claude to do>\n\n"
            "Example:\n"
            "/build go to AgentOptimize folder and hand Codex the specs to build the site"
        )
        return
    task = " ".join(context.args)
    await _launch_pipeline(task, update, context)


async def cmd_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pipeline <idea> — background pipeline job."""
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /pipeline <your business idea>")
        return
    idea = " ".join(context.args)
    prompt = f"/one-shot-pipeline --idea '{idea}'"
    await _launch_pipeline(prompt, update, context)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    chat_id = update.effective_chat.id
    bg = _bg_pipelines.get(chat_id)
    if bg:
        proc = bg.get("proc")
        task = bg.get("task")
        killed = False
        if proc and proc.poll() is None:
            proc.kill(); killed = True
        if task and not task.done():
            task.cancel()
        _bg_pipelines.pop(chat_id, None)
        await update.message.reply_text("⛔ Background job stopped." if killed else "Job was already finished.")
    else:
        await update.message.reply_text("No background job running for this chat.")


# ── General message handler ─────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route incoming messages:
      - Pipeline-like requests → Tier 2 background job (with user confirmation)
      - Everything else → Tier 1 claude -p queue
    """
    if await _reject_unauthorized(update):
        return
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    chat_id = update.effective_chat.id
    logger.info("Chat %s: %s", chat_id, user_text[:100])

    # Immediate typing indicator
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    # Auto-detect pipeline requests and suggest /build
    if _is_pipeline_request(user_text):
        await update.message.reply_text(
            "🔍 This looks like a multi-step build/pipeline task.\n\n"
            "For best results, use:\n"
            f"/build {user_text}\n\n"
            "That runs it as a background job with live progress updates "
            "instead of timing out.\n\n"
            "Sending as a quick question anyway — reply with /build if it times out."
        )

    # Queue for Tier 1 processing
    if chat_id not in _chat_queues:
        _chat_queues[chat_id] = asyncio.Queue()
        _chat_workers[chat_id] = asyncio.create_task(_chat_worker(chat_id))

    queue = _chat_queues[chat_id]
    queue_size = queue.qsize()
    await queue.put((update, context))

    if queue_size > 0:
        await update.message.reply_text(
            f"⏳ Previous message still processing — yours is queued ({queue_size + 1} in line)..."
        )


# ── Error handler ──────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram error: %s", context.error, exc_info=context.error)


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    token = _resolve_token()
    asyncio.run(_log_bot_identity(token))
    logger.info("Building Telegram Application...")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("build",    cmd_build))
    app.add_handler(CommandHandler("pipeline", cmd_pipeline))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Cato Telegram Bridge running (two-tier mode).")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
