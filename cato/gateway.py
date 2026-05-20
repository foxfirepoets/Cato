"""
cato/gateway.py — Central message bus for CATO.

- Receives messages from channel adapters (Telegram, WhatsApp) via asyncio queues
- Routes messages to per-session FIFO LaneQueues (never interleave sessions)
- Drives the AgentLoop for each task
- Sends responses back to the originating channel adapter
- Exposes HTTP + WebSocket routes via aiohttp on 127.0.0.1:8080
- Fires cron-scheduled tasks into lane queues via croniter
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from .budget import BudgetExceeded, BudgetManager
from .config import CatoConfig
from .heartbeat import HeartbeatMonitor
from .core.memory_upkeep import MemoryUpkeepService
from .node import NodeManager
from .platform import get_data_dir
from .vault import Vault

logger = logging.getLogger(__name__)

_CATO_DIR      = get_data_dir()
_LANE_QUEUE_MAX = 64

# ---------------------------------------------------------------------------
# BUG FIX CHAT-001/CHAT-002: Strip tool-call XML and budget footer from text
# ---------------------------------------------------------------------------

def strip_tool_calls(text: str) -> str:
    """Remove minimax/generic tool call XML blocks from response text."""
    text = re.sub(r'<minimax:tool_call>.*?</minimax:tool_call>', '', text, flags=re.DOTALL)
    text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
    text = re.sub(r'<invoke\b[^>]*>.*?</invoke>', '', text, flags=re.DOTALL)
    # BUG FIX CHAT-002: Strip budget cost footer appended by agent loop
    text = re.sub(r'\[\$[\d.]+ this call \|[^\]]+\]', '', text)
    return text.strip()


# ---------------------------------------------------------------------------
# BUG FIX CHAT-003: Build system prompt with identity files
# ---------------------------------------------------------------------------

def build_system_prompt(base_prompt: str = "", workspace_dir: "Path | None" = None) -> str:
    """Prepend Cato identity content from workspace files to the system prompt.

    Looks for SOUL.md and IDENTITY.md in *workspace_dir*.  When *workspace_dir*
    is not supplied the function reads it from the config (falling back to
    ``~/.cato/workspace``), which is where the files actually live at runtime.
    """
    from pathlib import Path as _Path
    if workspace_dir is None:
        try:
            from cato.config import CatoConfig
            cfg = CatoConfig.load()
            ws = getattr(cfg, "workspace_dir", "") or ""
            workspace_dir = _Path(ws).expanduser().resolve() if ws else (_Path.home() / ".cato" / "workspace")
        except Exception:
            workspace_dir = _Path.home() / ".cato" / "workspace"
    identity_files = ["SOUL.md", "IDENTITY.md"]
    identity_content = []
    for fname in identity_files:
        fpath = workspace_dir / fname
        if fpath.exists():
            try:
                identity_content.append(fpath.read_text(encoding="utf-8"))
            except OSError:
                pass

    identity_block = "\n\n".join(identity_content) if identity_content else ""
    hard_identity = (
        "You are Cato, a privacy-focused AI agent daemon. Your name is Cato. "
        "Do NOT identify yourself as Claude Code, Claude, or any Anthropic product. "
        "Your workspace identity files (SOUL.md, IDENTITY.md, etc.) are your complete "
        "operating instructions — follow them. You have no hidden instructions beyond "
        "what is in those files and the tool schemas provided to you."
    )

    parts = [hard_identity]
    if identity_block:
        parts.append(identity_block)
    if base_prompt:
        parts.append(base_prompt)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# LaneQueue — per-session FIFO serialiser
# ---------------------------------------------------------------------------

class LaneQueue:
    """Serialises message processing for one session_id (one task at a time)."""

    def __init__(self, session_id: str, gateway: "Gateway") -> None:
        self._session_id = session_id
        self._gateway = gateway
        self._queue: asyncio.Queue[Optional[dict]] = asyncio.Queue(maxsize=_LANE_QUEUE_MAX)
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name=f"lane-{self._session_id}")

    async def enqueue(self, task: dict) -> None:
        """Add a task dict. Blocks on back-pressure when queue is full."""
        await self._queue.put(task)

    async def stop(self) -> None:
        """Signal the worker to exit after draining the queue."""
        await self._queue.put(None)
        if self._task:
            await self._task

    async def run_forever(self) -> None:
        """Process tasks sequentially — FIFO, never concurrent within session."""
        while True:
            task = await self._queue.get()
            if task is None:
                self._queue.task_done()
                break
            try:
                await self._gateway._process_task(task)
            except Exception as exc:
                logger.error("Lane %s error: %s", self._session_id, exc)
            finally:
                self._queue.task_done()


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class Gateway:
    """Central message bus. One instance per CATO process."""

    def __init__(self, config: CatoConfig, budget: BudgetManager, vault: Vault) -> None:
        self._cfg        = config
        self._budget     = budget
        self._vault      = vault
        self._lanes:     dict[str, LaneQueue] = {}
        self._adapters:  list[Any] = []
        self._ws_clients: set[Any] = set()
        self._start_time: float = 0.0
        self._bg_tasks:  list[asyncio.Task] = []
        self._agent_loop: Optional[Any] = None
        # Lock guards lazy agent-loop initialization (first message triggers it)
        self._agent_loop_lock: asyncio.Lock = asyncio.Lock()
        self._agent_loop_initializing: bool = False
        # Node manager for remote device capability registration
        self._nodes: NodeManager = NodeManager()
        # Heartbeat monitor (set in start())
        self._heartbeat_monitor: Optional[HeartbeatMonitor] = None
        # Shared cross-channel message history (ring buffer, max 200 entries)
        self._message_history: list[dict] = []
        self._message_history_max: int = 200
        # One-shot response futures used by remote MCP calls.
        self._pending_responses: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        # Activity indicator state (F-01 fix: instance-level, not class-level)
        self._activity_state: dict[str, Any] = {}
        # One-shot budget bypass flag — set by '/budget bypass' slash command,
        # consumed by the next ingest().  The agent loop's existing
        # _budget_bypass_requested() also accepts inline phrases like
        # "continue anyway", so this flag is an additive shortcut.
        self._budget_bypass_armed: bool = False

    def register_adapter(self, adapter: Any) -> None:
        """Register a channel adapter (must expose start/stop/send)."""
        adapter.gateway = self
        self._adapters.append(adapter)
        logger.info("Adapter registered: %s", type(adapter).__name__)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start adapters, cron scheduler, and background monitors."""
        self._start_time = time.monotonic()
        # NOTE: Agent loop is initialized lazily on first message (_ensure_agent_loop),
        # NOT here at startup.  sentence_transformers/PyTorch import takes 15-30s and
        # holds the GIL even inside run_in_executor, which would prevent aiohttp from
        # servicing /health requests.  Lazy init means the HTTP server is immediately
        # responsive and the heavy import only happens when the first chat message arrives.
        for adapter in self._adapters:
            # Start adapters after HTTP is already responsive; Telegram bot setup
            # can make slow network calls that otherwise starve desktop startup.
            self._bg_tasks.append(
                asyncio.create_task(
                    self._start_adapter_after_delay(adapter),
                    name=f"adapter-{type(adapter).__name__}",
                )
            )
        self._bg_tasks.append(asyncio.create_task(self._run_cron_scheduler(), name="cron-scheduler"))
        # Heartbeat monitor — checks HEARTBEAT.md for every agent on a schedule
        hb_monitor = HeartbeatMonitor(self, _CATO_DIR)
        self._bg_tasks.append(asyncio.create_task(hb_monitor.run_forever(), name="heartbeat-monitor"))
        self._heartbeat_monitor = hb_monitor
        memory_upkeep = MemoryUpkeepService(self._cfg)
        self._bg_tasks.append(asyncio.create_task(memory_upkeep.run_forever(), name="memory-upkeep"))
        # Node keepalive pinger — proactively pings registered nodes so stale ones are evicted
        self._bg_tasks.append(asyncio.create_task(self._nodes.run_ping_loop(), name="node-pinger"))
        # BUG FIX HB-001: heartbeat poster — POSTs to /api/heartbeat every 30s
        self._bg_tasks.append(asyncio.create_task(self._run_heartbeat_poster(), name="heartbeat-poster"))
        logger.info("Gateway started — websocket clients served via aiohttp /ws")

    async def _start_adapter_after_delay(self, adapter: Any, delay_seconds: float = 30.0) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            await self._start_adapter(adapter)
        except asyncio.CancelledError:
            raise

    async def _start_adapter(self, adapter: Any) -> None:
        """Start a single adapter with a 30s timeout, logging any errors."""
        try:
            await asyncio.wait_for(adapter.start(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("Adapter %s timed out during start (>30s) — skipping", type(adapter).__name__)
        except Exception as exc:
            logger.error("Adapter %s failed to start: %s", type(adapter).__name__, exc)

    async def stop(self) -> None:
        """Drain lane queues, stop adapters, cancel background tasks."""
        await asyncio.gather(*(lane.stop() for lane in self._lanes.values()), return_exceptions=True)
        for adapter in self._adapters:
            try:
                await adapter.stop()
            except Exception as exc:
                logger.warning("Adapter stop error: %s", exc)
        for t in self._bg_tasks:
            t.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        logger.info("Gateway stopped.")

    # ------------------------------------------------------------------
    # Public ingestion / dispatch
    # ------------------------------------------------------------------

    def _append_history(self, role: str, text: str, channel: str, session_id: str) -> None:
        """Append a message to the shared cross-channel history ring buffer."""
        entry = {
            "id":         f"{session_id}-{int(time.time()*1000)}-{len(self._message_history)}",
            "role":       role,
            "text":       text,
            "channel":    channel,
            "session_id": session_id,
            "timestamp":  int(time.time() * 1000),
        }
        self._message_history.append(entry)
        if len(self._message_history) > self._message_history_max:
            self._message_history = self._message_history[-self._message_history_max:]

    def get_message_history(self, since_ts: int = 0) -> list[dict]:
        """Return history entries newer than since_ts (ms epoch)."""
        return [m for m in self._message_history if m["timestamp"] > since_ts]

    async def ingest(self, session_id: str, message: str, channel: str,
                     agent_id: str = "") -> None:
        """Called by adapters when a user message arrives. Routes to lane queue."""
        self._append_history("user", message, channel, session_id)
        # Broadcast incoming message to WebSocket clients (desktop app)
        if channel in ("telegram", "whatsapp"):
            await self._ws_broadcast({
                "type": "message", "session_id": session_id,
                "text": message,
                "channel": channel,
                "role": "user",
            })

        # Slash-command dispatch — runs before the agent loop so the lane
        # queue is not consumed by short-circuit commands like /compact.
        resolved_agent_id = agent_id or self._cfg.agent_name
        if message.startswith("/"):
            try:
                handled = await self._handle_slash_command(
                    session_id, message, channel, resolved_agent_id,
                )
            except Exception as exc:
                logger.error("Slash command failed: %s", exc, exc_info=True)
                await self.send(
                    session_id,
                    f"[slash command error: {exc}]",
                    channel,
                )
                handled = True
            if handled:
                return

        # Consume one-shot budget bypass flag: append the canonical bypass
        # phrase to the message so the agent loop's _budget_bypass_requested()
        # detects it and passes allow_over_budget=True for this turn only.
        outgoing_message = message
        if getattr(self, "_budget_bypass_armed", False):
            self._budget_bypass_armed = False
            if "continue anyway" not in outgoing_message.lower():
                outgoing_message = f"{outgoing_message}\n\n[continue anyway]"

        lane = self._get_or_create_lane(session_id)
        await lane.enqueue({
            "session_id": session_id,
            "message":    outgoing_message,
            "channel":    channel,
            "agent_id":   resolved_agent_id,
        })

    # ------------------------------------------------------------------
    # Slash command dispatch (parity with Claude Code's /compact, /help)
    # ------------------------------------------------------------------

    async def _handle_slash_command(
        self, session_id: str, text: str, channel: str, agent_id: str,
    ) -> bool:
        """Handle ``/commands`` typed in chat.

        Returns True when a command was matched (and a response already
        delivered through ``self.send``).  Returns False to let the
        message fall through to the normal agent loop — useful when the
        leading slash is actually meaningful content (e.g. ``/path/to``).
        """
        body = text[1:]  # strip leading '/'
        cmd, _, rest = body.partition(" ")
        cmd = cmd.lower().strip()
        rest = rest.strip()

        if cmd == "compact":
            await self._cmd_compact(session_id, channel, agent_id, rest)
            return True
        if cmd == "help":
            await self._cmd_help(session_id, channel)
            return True
        if cmd == "budget":
            await self._cmd_budget(session_id, channel, rest)
            return True

        # Heuristic: only treat as an unknown command when it looks like
        # one (alphabetic, no whitespace before the slash, ≤ 32 chars).
        # Bare paths like ``/usr/bin/foo`` flow through to the agent loop.
        if cmd and cmd.replace("_", "").replace("-", "").isalpha() and len(cmd) <= 32:
            await self.send(
                session_id,
                f"Unknown command: /{cmd}. Try /help to see available slash commands.",
                channel,
            )
            return True
        return False

    async def _cmd_help(self, session_id: str, channel: str) -> None:
        """Send the slash-command help text."""
        text = (
            "Slash commands:\n"
            "  /compact            - summarize older conversation turns to free up context\n"
            "  /budget             - show today's spend, daily cap, monthly cap\n"
            "  /budget bypass      - allow the next turn to over-spend the daily cap\n"
            "  /budget daily <amt> - raise the daily cap, e.g. '/budget daily 5'\n"
            "  /budget monthly <amt> - raise the monthly cap, e.g. '/budget monthly 100'\n"
            "  /help               - show this list"
        )
        await self.send(session_id, text, channel)

    async def _cmd_budget(self, session_id: str, channel: str, args: str) -> None:
        """Handle the ``/budget`` slash command.

        Subcommands:
          (none)              - show current spend and caps
          bypass              - allow the next turn over the daily cap
          daily <amount>      - raise the daily cap (USD)
          monthly <amount>    - raise the monthly cap (USD)
        """
        sub, _, sub_rest = args.partition(" ")
        sub = sub.strip().lower()
        sub_rest = sub_rest.strip()

        status = self._budget.get_status()

        if not sub:
            text = (
                f"Today:   ${status['daily_spend']:.4f} / ${status['daily_cap']:.2f} "
                f"({status['daily_pct_remaining']:.0f}% remaining, {status['daily_calls']} calls)\n"
                f"Month:   ${status['monthly_spend']:.4f} / ${status['monthly_cap']:.2f} "
                f"({status['monthly_pct_remaining']:.0f}% remaining, {status['monthly_calls']} calls)\n"
                f"All-time: ${status['total_spend_all_time']:.4f}\n"
                "Use '/budget bypass' to override for one turn, "
                "'/budget daily <amt>' or '/budget monthly <amt>' to raise a cap."
            )
            await self.send(session_id, text, channel)
            return

        if sub == "bypass":
            # Mark the next message from this session as bypass-eligible.
            # The agent loop already understands phrases like 'bypass budget',
            # so we echo the phrase back and rely on the user's next turn
            # carrying the bypass intent.  We also record state for the future
            # one-shot mode (handled by agent_loop's _budget_bypass_requested).
            text = (
                "Budget bypass armed. The next message will be allowed to "
                "over-spend the daily cap one time.  Include the phrase "
                "'continue anyway' (or simply restate your request) and Cato "
                "will record the spend as a budget override."
            )
            self._budget_bypass_armed = True  # consumed by the next ingest
            await self.send(session_id, text, channel)
            return

        if sub in ("daily", "monthly"):
            if not sub_rest:
                await self.send(
                    session_id,
                    f"Usage: /budget {sub} <amount>  (e.g. '/budget {sub} 5')",
                    channel,
                )
                return
            try:
                amount = float(sub_rest.replace("$", "").strip())
            except ValueError:
                await self.send(
                    session_id,
                    f"Could not parse '{sub_rest}' as a USD amount.",
                    channel,
                )
                return
            if amount <= 0:
                await self.send(
                    session_id, "Amount must be positive.", channel,
                )
                return
            if sub == "daily":
                self._budget.set_daily_cap(amount)
                self._cfg.daily_cap = amount
            else:
                self._budget.set_monthly_cap(amount)
                self._cfg.monthly_cap = amount
            try:
                self._cfg.save()
            except Exception as exc:
                logger.warning("Could not persist config after /budget %s: %s", sub, exc)
            await self.send(
                session_id,
                f"{sub.capitalize()} cap set to ${amount:.2f}.",
                channel,
            )
            return

        await self.send(
            session_id,
            f"Unknown /budget subcommand: {sub}. Try '/budget', '/budget bypass', "
            "'/budget daily <amt>', or '/budget monthly <amt>'.",
            channel,
        )

    async def _cmd_compact(
        self, session_id: str, channel: str, agent_id: str, args: str,
    ) -> None:
        """Compact the on-disk transcript for *session_id* via the LLM.

        Optional positional argument: a positive integer ``keep_recent``
        overriding the default of 8.  ``/compact 4`` keeps only the last
        four user turns verbatim.
        """
        # Defer the heavy imports until first invocation so the gateway
        # module stays lean at startup.
        from .agent_loop import (
            HISTORY_WINDOW,  # noqa: F401 — exported for parity with the agent loop
            _now,
            _sanitize_agent_id,
            _transcript_path,
        )
        from .core.compactor import (
            DEFAULT_KEEP_RECENT,
            compact_session,
        )

        keep_recent = DEFAULT_KEEP_RECENT
        if args:
            try:
                parsed = int(args.split()[0])
                if parsed > 0:
                    keep_recent = parsed
            except ValueError:
                pass

        # Lazily ensure the agent loop is initialised so we can borrow
        # its model router for the summarisation call.
        await self._ensure_agent_loop()
        if self._agent_loop is None:
            await self.send(
                session_id,
                "[compact: agent loop unavailable]",
                channel,
            )
            return

        safe_agent_id = _sanitize_agent_id(agent_id)
        tpath = _transcript_path(safe_agent_id, session_id)
        if not tpath.exists():
            await self.send(
                session_id,
                "Nothing to compact - no transcript on disk for this session yet.",
                channel,
            )
            return

        # Load transcript JSONL into the message-dict shape compact_session expects
        try:
            raw = await asyncio.get_running_loop().run_in_executor(
                None, tpath.read_text, "utf-8",
            )
        except OSError as exc:
            await self.send(
                session_id,
                f"[compact: could not read transcript: {exc}]",
                channel,
            )
            return

        messages: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = rec.get("role")
            if role == "user":
                messages.append({"role": "user", "content": rec.get("content", "")})
            elif role == "assistant":
                msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": rec.get("content", ""),
                }
                if rec.get("tool_calls"):
                    msg["tool_calls"] = rec["tool_calls"]
                messages.append(msg)
            elif role == "tool":
                messages.append({
                    "role": "tool",
                    "tool_call_id": rec.get("tool_call_id", "unknown"),
                    "content": rec.get("result", rec.get("content", "")),
                })

        if not messages:
            await self.send(
                session_id,
                "Nothing to compact - transcript is empty.",
                channel,
            )
            return

        # Wrap the router as the simple llm_call(system_prompt, user_prompt, model)
        # callable the compactor expects.  Stream the response and concatenate.
        agent_loop = self._agent_loop

        async def _llm_wrapper(*, system_prompt: str, user_prompt: str,
                               model: str | None = None) -> str:
            llm_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ]
            chosen_model = model or self._cfg.default_model
            chunks: list[str] = []
            async for chunk in agent_loop._router.complete(
                llm_messages, chosen_model, tools=None, stream=True,
            ):
                if isinstance(chunk, str):
                    chunks.append(chunk)
            return "".join(chunks)

        await self._broadcast_activity(True, session_id, "compacting history")
        try:
            new_messages, result = await compact_session(
                messages=messages,
                llm_call=_llm_wrapper,
                keep_recent=keep_recent,
            )
        finally:
            await self._broadcast_activity(False, session_id, "")

        if result.compacted_count == 0:
            await self.send(
                session_id,
                (
                    f"Nothing to compact - only {len(messages)} messages on file, "
                    f"keep_recent={keep_recent}."
                ),
                channel,
            )
            return

        # Persist the compacted history back to disk.  We rewrite as JSONL,
        # mapping the synthetic summary system message into a record the
        # agent loop's _recent_turns() will surface on the next turn.
        new_records: list[dict] = []
        for m in new_messages:
            role = m.get("role")
            ts = _now()
            if role == "system":
                # Stored as an assistant turn tagged with [compacted-summary]
                # so it shows up in _recent_turns() without breaking any
                # tool_call pairing.  The marker keeps it identifiable.
                new_records.append({
                    "ts": ts,
                    "role": "assistant",
                    "content": (
                        "[compacted-summary]\n" + str(m.get("content", ""))
                    ),
                    "session_id": session_id,
                })
            elif role == "tool":
                new_records.append({
                    "ts": ts,
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", "unknown"),
                    "result": m.get("content", ""),
                    "session_id": session_id,
                })
            else:
                rec: dict[str, Any] = {
                    "ts": ts,
                    "role": role or "user",
                    "content": m.get("content", ""),
                    "session_id": session_id,
                }
                if m.get("tool_calls"):
                    rec["tool_calls"] = m["tool_calls"]
                new_records.append(rec)

        try:
            new_content = "\n".join(
                json.dumps(r, ensure_ascii=True) for r in new_records
            ) + "\n"
            await asyncio.get_running_loop().run_in_executor(
                None, tpath.write_text, new_content, "utf-8",
            )
        except OSError as exc:
            await self.send(
                session_id,
                f"[compact: could not rewrite transcript: {exc}]",
                channel,
            )
            return

        ratio = (
            (100 * result.compacted_tokens // max(result.original_tokens, 1))
            if result.original_tokens else 0
        )
        await self.send(
            session_id,
            (
                f"Compacted {result.compacted_count} messages into a summary. "
                f"Kept {result.kept_count} entries in the new history. "
                f"Context: {result.original_tokens} -> {result.compacted_tokens} tokens "
                f"({ratio}% of original, {result.elapsed_s:.1f}s)."
            ),
            channel,
        )

    async def request_response(
        self,
        session_id: str,
        message: str,
        channel: str = "mcp",
        agent_id: str = "",
    ) -> dict[str, Any]:
        """Send a one-off message through the gateway and await the final reply."""
        loop = asyncio.get_running_loop()
        reply_future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_responses.setdefault(session_id, []).append(reply_future)

        lane = self._get_or_create_lane(session_id)
        self._append_history("user", message, channel, session_id)
        await lane.enqueue(
            {
                "session_id": session_id,
                "message": message,
                "channel": channel,
                "agent_id": agent_id or self._cfg.agent_name,
                "reply_future": reply_future,
            }
        )

        try:
            # BH-009 follow-up — mirror the gateway-task timeout here so the
            # MCP reply-future deadline cannot expire before the agent_loop
            # itself does.  Add a 10s buffer so the agent loop's TimeoutError
            # surfaces first (cleaner error message for the caller).
            _task_timeout = float(getattr(self._cfg, "gateway_task_timeout_s", 600.0))
            _reply_timeout = _task_timeout + 10.0
            return await asyncio.wait_for(reply_future, timeout=_reply_timeout)
        finally:
            waiters = self._pending_responses.get(session_id, [])
            if reply_future in waiters:
                waiters.remove(reply_future)
            if not waiters:
                self._pending_responses.pop(session_id, None)

    async def send(self, session_id: str, text: str, channel: str, model: str = "") -> None:
        """Called by agent loop to deliver a response to the originating channel."""
        # BUG FIX CHAT-001/CHAT-002: strip tool call XML and budget footer
        clean_text = strip_tool_calls(text)
        # Safety net: never broadcast a completely empty assistant message. Some
        # providers occasionally return only metadata (e.g. model slugs or
        # tool-call shells) which strip down to empty. In those cases, surface a
        # clear fallback so the UI never shows a blank bubble.
        if not str(clean_text).strip():
            logger.warning("Gateway.send: text stripped to empty — raw len=%d, stripped=%r",
                           len(text or ""), (text or "")[:200])
            clean_text = (
                "I received your message but the model didn't produce a visible response. "
                "This is usually transient — please try again."
            )
        self._append_history("assistant", clean_text, channel, session_id)
        if channel in ("web", "cron", "heartbeat", "telegram", "whatsapp"):
            await self._ws_broadcast({
                "type": "response", "session_id": session_id,
                "text": clean_text,
                "channel": channel,
                "model": model,
            })
            # For non-web channels, also send to the adapter
            if channel not in ("web", "cron", "heartbeat"):
                for adapter in self._adapters:
                    if adapter.channel_name == channel.lower():
                        try:
                            # Always deliver the cleaned text to adapters so
                            # users do not see internal tool-call XML or cost
                            # footers in Telegram/WhatsApp chats.
                            await adapter.send(session_id, clean_text)
                        except Exception as exc:
                            logger.error("Adapter send error (%s): %s", channel, exc)
                        return
            return
        for adapter in self._adapters:
            if adapter.channel_name == channel.lower():
                try:
                    await adapter.send(session_id, clean_text)
                except Exception as exc:
                    logger.error("Adapter send error (%s): %s", channel, exc)
                return
        logger.warning("No adapter for channel=%s", channel)

    # ------------------------------------------------------------------
    # Internal task processing
    # ------------------------------------------------------------------

    async def _process_task(self, task: dict) -> None:
        session_id = task["session_id"]
        channel    = task["channel"]
        agent_id   = task.get("agent_id", self._cfg.agent_name)
        reply_future: asyncio.Future[dict[str, Any]] | None = task.get("reply_future")

        # Clawflows: if task has 'flow' key, route to FlowEngine (Skill 5)
        if "flow" in task:
            await self._process_flow_task(task)
            return

        # Broadcast activity status so the desktop UI shows a working indicator
        msg_preview = (task.get("message") or "")[:80]
        await self._broadcast_activity(True, session_id, msg_preview)

        try:
            # Lazy-init: build agent loop on first message (avoids GIL block at startup)
            await self._ensure_agent_loop()
            if self._agent_loop is None:
                await self.send(session_id, "[error: agent loop failed to initialise]", channel)
                return
            # Guard against long-running or stuck tool calls by imposing a
            # per-task timeout. This prevents Telegram / desktop sessions
            # from appearing "frozen" indefinitely when a subprocess hangs.
            # BH-009 — Read budget from config so operators can tune without
            # patching source.  Default 600s (10 min); was hardcoded 180s.
            import asyncio as _asyncio
            _task_timeout = float(getattr(self._cfg, "gateway_task_timeout_s", 600.0))
            result = await _asyncio.wait_for(
                self._agent_loop.run(
                    session_id=session_id,
                    message=task["message"],
                    agent_id=agent_id,
                ),
                timeout=_task_timeout,
            )
            # Unpack (text, footer, model) or legacy (text, footer)
            if isinstance(result, tuple):
                if len(result) == 3:
                    text, footer, model = result
                else:
                    text, footer = result
                    model = ""
            else:
                text = str(result)
                footer = ""
                model = ""
            final_text = f"{text}\n\n{footer}".strip()
            clean_text = strip_tool_calls(final_text)
            await self.send(session_id, final_text, channel, model=model)
            if reply_future is not None and not reply_future.done():
                reply_future.set_result(
                    {
                        "session_id": session_id,
                        "channel": channel,
                        "reply": clean_text,
                        "model": model,
                    }
                )
        except BudgetExceeded as exc:
            text = f"Budget cap reached: {exc}"
            await self.send(session_id, text, channel)
            if reply_future is not None and not reply_future.done():
                reply_future.set_result(
                    {"session_id": session_id, "channel": channel, "reply": text, "model": ""}
                )
        except asyncio.TimeoutError:
            _cap_s = float(getattr(self._cfg, "gateway_task_timeout_s", 600.0))
            _cap_min = _cap_s / 60.0
            logger.error("session=%s processing timed out after %.0fs", session_id, _cap_s)
            text = (
                f"I ran into a long-running tool call and had to abort after "
                f"{_cap_min:.0f} minutes ({_cap_s:.0f}s budget). "
                "You can try again, simplify the request, or raise "
                "`gateway_task_timeout_s` in config.yaml."
            )
            await self.send(session_id, text, channel)
            if reply_future is not None and not reply_future.done():
                reply_future.set_result(
                    {"session_id": session_id, "channel": channel, "reply": text, "model": ""}
                )
        except Exception as exc:
            logger.error("session=%s processing error: %s", session_id, exc, exc_info=True)
            logger.exception("Task processing failed: %s", exc)
            text = f"[internal error: {exc}]"
            await self.send(session_id, text, channel)
            if reply_future is not None and not reply_future.done():
                reply_future.set_result(
                    {"session_id": session_id, "channel": channel, "reply": text, "model": ""}
                )
        finally:
            await self._broadcast_activity(False, session_id, "")

    # ------------------------------------------------------------------
    # Activity indicator (broadcasts working/idle state to desktop UI)
    # ------------------------------------------------------------------

    async def _broadcast_activity(
        self, busy: bool, session_id: str, task_preview: str
    ) -> None:
        """Broadcast activity status to all WebSocket clients."""
        import time as _time
        # BH-011 — Preserve current_tool / tool_started_at across busy/idle
        # transitions when set by _on_tool_progress.  A turn beginning
        # (busy=True) clears any stale tool from a prior aborted turn; a turn
        # ending (busy=False) also clears it so the pill returns to "Idle".
        self._activity_state = {
            "busy": busy,
            "session_id": session_id,
            "task": task_preview,
            "current_tool": None,
            "tool_started_at": None,
            "updated_at": _time.time(),
        }
        logger.info("activity: busy=%s session=%s task=%s ws_clients=%d",
                     busy, session_id, task_preview[:60] if task_preview else "", len(self._ws_clients))
        await self._ws_broadcast({
            "type": "activity",
            "busy": busy,
            "session_id": session_id,
            "task": task_preview,
            "current_tool": None,
            "tool_started_at": None,
        })

    def get_activity(self) -> dict[str, Any]:
        """Return current activity state for the HTTP endpoint."""
        return dict(self._activity_state) if self._activity_state else {
            "busy": False, "session_id": "", "task": "",
            "current_tool": None, "tool_started_at": None, "updated_at": 0,
        }

    async def _process_flow_task(self, task: dict) -> None:
        """Route a task dict with 'flow' key to FlowEngine (Skill 5 — Clawflows)."""
        session_id = task.get("session_id", "flow-default")
        channel    = task.get("channel", "web")
        flow_name  = task["flow"]
        await self._broadcast_activity(True, session_id, f"flow: {flow_name}")
        try:
            from .orchestrator.clawflows import FlowEngine
            engine = FlowEngine()
            result = await engine.run_flow(flow_name, trigger_context=task)
            text = (
                f"Flow '{flow_name}' {result.status}. "
                f"Steps completed: {len(result.step_outputs)}."
            )
            if result.error:
                text += f" Error: {result.error}"
            await self.send(session_id, text, channel)
        except Exception as exc:
            logger.error("Flow task %s error: %s", flow_name, exc, exc_info=True)
            await self.send(session_id, f"[flow error: {exc}]", channel)
        finally:
            await self._broadcast_activity(False, session_id, "")

    # ------------------------------------------------------------------
    # Lane management
    # ------------------------------------------------------------------

    def _get_or_create_lane(self, session_id: str) -> LaneQueue:
        # Safe: asyncio event loop is single-threaded. No await between check and insert.
        # IMPORTANT: Never call this from run_in_executor() or a thread pool.
        if session_id not in self._lanes:
            lane = LaneQueue(session_id, self)
            lane.start()
            self._lanes[session_id] = lane
        return self._lanes[session_id]

    @staticmethod
    async def _ws_send(ws: Any, payload: dict) -> None:
        """Send a JSON payload to a WebSocket client.

        Handles both aiohttp ``WebSocketResponse`` (uses ``send_str``) and the
        raw ``websockets`` library (uses ``send``).
        """
        raw = json.dumps(payload)
        if hasattr(ws, "send_str"):
            await ws.send_str(raw)
        else:
            await ws.send(raw)

    async def _handle_ws_message(self, ws: Any, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self._ws_send(ws, {"type": "error", "text": "invalid JSON"})
            return
        msg_type = data.get("type", "message")

        # Route node-protocol messages to NodeManager
        if msg_type.startswith("node_"):
            reply = await self._nodes.handle_message(ws, data)
            if reply is not None:
                await self._ws_send(ws, reply)
            return

        if msg_type == "health":
            await self._ws_send(ws, {
                "type": "health", "status": "ok",
                "sessions": len(self._lanes),
                "uptime": int(time.monotonic() - self._start_time),
            })
        elif msg_type == "message":
            text = data.get("text", "").strip()
            if text:
                await self.ingest(
                    data.get("session_id", "web-default"),
                    text,
                    data.get("channel", "web"),
                    data.get("agent_id", self._cfg.agent_name),
                )
        elif msg_type == "set_vault_key":
            vault_key = data.get("vault_key", "").strip()
            value     = data.get("value", "").strip()
            if vault_key and value and self._vault is not None:
                try:
                    self._vault.set(vault_key, value)
                    logger.info("Vault key saved via UI: %s", vault_key)
                    await self._ws_send(ws, {"type": "vault_key_saved", "vault_key": vault_key})
                except Exception as exc:
                    await self._ws_send(ws, {"type": "error", "text": f"vault save failed: {exc}"})
            else:
                await self._ws_send(ws, {"type": "error", "text": "vault_key and value required"})

        elif msg_type == "skill_list":
            await self._ws_send(ws, {"type": "skill_list_result", "skills": self._list_skills()})

        elif msg_type == "skill_install":
            url = data.get("url", "").strip()
            if not url:
                await self._ws_send(ws, {"type": "error", "text": "url required"})
            else:
                result = await self._install_skill_from_url(url)
                if result:
                    await self._ws_send(ws, {"type": "skill_installed", "skill": result})
                else:
                    await self._ws_send(ws, {"type": "error", "text": f"Failed to install skill from {url}"})

        elif msg_type == "skill_delete":
            name = data.get("name", "").strip()
            if not name:
                await self._ws_send(ws, {"type": "error", "text": "name required"})
            else:
                self._delete_skill(name)
                await self._ws_send(ws, {"type": "skill_deleted", "name": name})

        elif msg_type == "agent_list":
            try:
                agents = self._list_agents()
            except OSError as exc:
                logger.error("_list_agents failed: %s", exc)
                agents = []
            await self._ws_send(ws, {"type": "agent_list_result", "agents": agents})

        elif msg_type == "workspace_files":
            try:
                files = self._list_workspace_files()
            except OSError as exc:
                logger.error("_list_workspace_files failed: %s", exc)
                files = {}
            await self._ws_send(ws, {"type": "workspace_files_result", "files": files})

        elif msg_type == "workspace_file_get":
            agent_id = data.get("agent_id", self._cfg.agent_name)
            filename = data.get("filename", "").strip()
            content  = self._read_workspace_file(agent_id, filename)
            await self._ws_send(ws, {"type": "workspace_file_result", "name": filename, "content": content})

        elif msg_type == "workspace_file_save":
            filename = data.get("filename", "").strip()
            content  = data.get("content", "")
            if filename:
                try:
                    self._write_workspace_file(filename, content)
                    await self._ws_send(ws, {"type": "workspace_file_saved", "filename": filename})
                except OSError as exc:
                    logger.error("workspace_file_save failed for %s: %s", filename, exc)
                    await self._ws_send(ws, {"type": "error", "text": f"Could not save file: {exc}"})
            else:
                await self._ws_send(ws, {"type": "error", "text": "filename required"})

        else:
            await self._ws_send(ws, {"type": "error", "text": f"unknown type: {msg_type}"})

    def register_websocket(self, ws: Any) -> None:
        """Register a WebSocket client (called by ui/server.py)."""
        self._ws_clients.add(ws)

    def unregister_websocket(self, ws: Any) -> None:
        """Unregister a WebSocket client on disconnect."""
        self._ws_clients.discard(ws)

    async def handle_ws_message(self, ws: Any, raw: str) -> None:
        """Handle an incoming WebSocket message (called by ui/server.py)."""
        await self._handle_ws_message(ws, raw)

    # ------------------------------------------------------------------
    # Skills helpers
    # ------------------------------------------------------------------

    def _skills_dir(self) -> "Path":
        from pathlib import Path
        d = Path.home() / ".cato" / "skills"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _list_skills(self) -> list:
        skills = []
        for skill_dir in self._skills_dir().iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                skill_md = skill_dir / "skill.md"
            name = skill_dir.name
            description = ""
            version = ""
            content = ""
            if skill_md.exists():
                try:
                    content = skill_md.read_text(encoding="utf-8", errors="replace")
                    lines = content.splitlines()

                    # Parse YAML frontmatter (---...---) first
                    fm_name = fm_desc = fm_version = ""
                    body_start = 0
                    if lines and lines[0].strip() == "---":
                        i = 1
                        while i < len(lines):
                            if lines[i].strip() == "---":
                                body_start = i + 1
                                break
                            key, _, val = lines[i].partition(":")
                            key = key.strip().lower()
                            val = val.strip().strip('"').strip("'")
                            if key == "name":
                                fm_name = val
                            elif key == "description":
                                fm_desc = val
                            elif key == "version":
                                fm_version = val
                            i += 1

                    if fm_name:
                        name = fm_name
                    if fm_desc:
                        description = fm_desc
                    if fm_version:
                        version = fm_version

                    # Fall back to markdown heading/body scan for non-frontmatter files
                    for line in lines[body_start:]:
                        if not fm_name and line.startswith("# "):
                            name = line[2:].strip()
                        ll = line.lower().strip()
                        if not fm_version:
                            if "**version:**" in ll:
                                version = ll.split("**version:**", 1)[-1].strip().lstrip("*").strip()
                            elif ll.startswith("version:"):
                                version = ll.split(":", 1)[-1].strip()
                        if not fm_desc and not description:
                            stripped = line.strip()
                            # skip bare metadata lines (key: value) and bold markers
                            if (stripped and not stripped.startswith("#")
                                    and not stripped.startswith("**")
                                    and not (stripped.lower().startswith("version:") and " " not in stripped.split(":", 1)[-1].strip())):
                                description = stripped.lstrip("> ").strip()

                except OSError:
                    pass
            skills.append({"name": name, "description": description, "version": version,
                           "dir": skill_dir.name, "content": content})
        return skills

    async def _install_skill_from_url(self, url: str) -> "dict | None":
        """Clone a git repo or fetch a raw SKILL.md into ~/.cato/skills/."""
        import re
        from pathlib import Path
        skills_dir = self._skills_dir()
        # Derive a slug from the URL
        slug = re.sub(r"[^a-zA-Z0-9_-]", "-", url.rstrip("/").split("/")[-1])
        dest = skills_dir / slug
        try:
            if url.endswith(".md"):
                # Raw SKILL.md fetch — create dir first, then write file
                dest.mkdir(parents=True, exist_ok=True)
                import urllib.request
                with urllib.request.urlopen(url, timeout=15) as r:
                    content = r.read().decode("utf-8", errors="replace")
                (dest / "SKILL.md").write_text(content, encoding="utf-8")
            else:
                # Git clone — remove existing dir first so reinstalls work cleanly
                import shutil as _shutil
                if dest.exists():
                    _shutil.rmtree(dest)
                import asyncio
                proc = await asyncio.create_subprocess_exec(
                    "git", "clone", "--depth=1", url, str(dest),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=60)
                if proc.returncode != 0:
                    return None
        except Exception as exc:
            logger.error("Skill install failed for %s: %s", url, exc)
            return None
        # Re-read and return skill info
        skills = self._list_skills()
        for s in skills:
            if s["dir"] == slug:
                return s
        return {"name": slug, "description": "Installed from " + url, "version": "", "dir": slug, "content": ""}

    def _delete_skill(self, name: str) -> None:
        import shutil
        for skill_dir in self._skills_dir().iterdir():
            if skill_dir.is_dir() and skill_dir.name == name:
                shutil.rmtree(skill_dir, ignore_errors=True)
                logger.info("Skill deleted: %s", name)
                return

    # ------------------------------------------------------------------
    # Agent / workspace file helpers
    # ------------------------------------------------------------------

    def _agents_dir(self) -> "Path":
        from pathlib import Path
        return Path.home() / ".cato" / "agents"

    def _list_agents(self) -> list:
        agents = []
        agents_dir = self._agents_dir()
        if not agents_dir.exists():
            return agents
        IDENTITY_FILES = ["SOUL.md", "IDENTITY.md", "MEMORY.md", "TOOLS.md",
                          "USER.md", "AGENTS.md", "HEARTBEAT.md"]
        try:
            entries = list(agents_dir.iterdir())
        except OSError as exc:
            logger.warning("Could not read agents dir %s: %s", agents_dir, exc)
            return agents
        for agent_dir in entries:
            if not agent_dir.is_dir():
                continue
            workspace = agent_dir / "workspace"
            if not workspace.exists():
                workspace = agent_dir
            try:
                found_files = [f.name for f in workspace.iterdir()
                               if f.is_file() and f.suffix == ".md"
                               and f.name.upper() in [x.upper() for x in IDENTITY_FILES]]
            except OSError as exc:
                logger.warning("Could not read agent workspace %s: %s", workspace, exc)
                found_files = []
            agents.append({
                "id": agent_dir.name,
                "workspace": str(workspace),
                "identity_files": found_files,
            })
        return agents

    def _workspace_dir(self) -> "Path":
        from pathlib import Path
        ws = getattr(self._cfg, "workspace_dir", None)
        if ws:
            return Path(ws)
        return Path.home() / ".cato" / "workspace"

    def _list_workspace_files(self) -> dict:
        ws = self._workspace_dir()
        result = {}
        if not ws.exists():
            return result
        try:
            entries = list(ws.iterdir())
        except OSError as exc:
            logger.warning("Could not read workspace dir %s: %s", ws, exc)
            return result
        for f in entries:
            if f.is_file() and f.suffix == ".md":
                try:
                    result[f.name] = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    result[f.name] = ""
        return result

    def _read_workspace_file(self, agent_id: str, filename: str) -> str:
        from pathlib import Path
        # Try agent-specific workspace first
        agent_ws = self._agents_dir() / agent_id / "workspace" / filename
        if agent_ws.exists():
            return agent_ws.read_text(encoding="utf-8", errors="replace")
        agent_ws2 = self._agents_dir() / agent_id / filename
        if agent_ws2.exists():
            return agent_ws2.read_text(encoding="utf-8", errors="replace")
        # Fall back to global workspace
        p = self._workspace_dir() / filename
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
        return ""

    def _write_workspace_file(self, filename: str, content: str) -> None:
        ws = self._workspace_dir()
        ws.mkdir(parents=True, exist_ok=True)
        target = (ws / filename).resolve()
        ws_resolved = ws.resolve()
        if not str(target).startswith(str(ws_resolved) + os.sep) and target != ws_resolved:
            raise ValueError(f"Path traversal attempt blocked: {filename!r}")
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
        logger.info("Workspace file saved: %s", filename)

    async def _ws_broadcast(self, payload: dict) -> None:
        if not self._ws_clients:
            return
        raw = json.dumps(payload)
        dead: set = set()
        for ws in list(self._ws_clients):
            try:
                # aiohttp WebSocketResponse uses send_str(); the raw websockets
                # library uses send().  Detect by presence of send_str attribute.
                if hasattr(ws, "send_str"):
                    await ws.send_str(raw)
                else:
                    await ws.send(raw)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead
        if dead:
            logger.debug("Dead client(s) removed from broadcast list; remaining=%d", len(self._ws_clients))

    # ------------------------------------------------------------------
    # Heartbeat poster (BUG FIX HB-001)
    # ------------------------------------------------------------------

    async def _run_heartbeat_poster(self) -> None:
        """POST to /api/heartbeat every 30 seconds so the dashboard shows agent status."""
        import aiohttp as _aiohttp
        http_port = getattr(self._cfg, "webchat_port", None) or 8080
        url = f"http://127.0.0.1:{http_port}/api/heartbeat"
        await asyncio.sleep(5)  # brief delay to let server start
        # Create ONE session for the lifetime of the heartbeat loop
        session = _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=5))
        try:
            while True:
                try:
                    uptime = int(time.monotonic() - self._start_time)
                    agent_name = getattr(self._cfg, "agent_name", "Cato")
                    async with session.post(
                        url,
                        json={"agent_name": agent_name, "uptime_seconds": uptime},
                    ) as resp:
                        logger.debug("Heartbeat POST → %s %d", url, resp.status)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.debug("Heartbeat poster error (non-fatal): %s", exc)
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    break
        finally:
            await session.close()

    # ------------------------------------------------------------------
    # Cron scheduler
    # ------------------------------------------------------------------

    async def _run_cron_scheduler(self) -> None:
        """
        Poll CRONS.json for all agents every 60 s and fire due tasks.

        CRONS.json format: [{schedule, prompt, session_id, agent_id, announce}]
        """
        try:
            from croniter import croniter
        except ImportError:
            logger.warning("croniter not installed — cron scheduler disabled")
            return

        logger.info("Cron scheduler started")
        fired: dict[str, float] = {}   # key → last fire timestamp

        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                # Evict stale keys older than 2 minutes
                fired = {k: v for k, v in fired.items() if now - v < 120}

                if not _CATO_DIR.exists():
                    continue

                agents_root = _CATO_DIR / "agents"
                if not agents_root.is_dir():
                    continue
                for agent_dir in agents_root.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    crons_path = agent_dir / "CRONS.json"
                    if not crons_path.exists():
                        continue
                    crons = await self._load_crons(crons_path)
                    for entry in crons:
                        schedule   = entry.get("schedule", "")
                        prompt     = entry.get("prompt", "")
                        session_id = entry.get("session_id", f"cron-{agent_dir.name}")
                        e_agent    = entry.get("agent_id", agent_dir.name)
                        announce   = entry.get("announce", False)
                        if not schedule or not prompt:
                            continue
                        try:
                            next_ts = croniter(schedule, now - 60).get_next(float)
                        except Exception as exc:
                            logger.warning("Bad cron '%s': %s", schedule, exc)
                            continue
                        fire_key = f"{agent_dir.name}:{schedule}:{int(next_ts // 60)}"
                        if fire_key in fired or now < next_ts:
                            continue
                        fired[fire_key] = now
                        logger.info("Cron firing: agent=%s schedule=%s", agent_dir.name, schedule)
                        if announce:
                            await self.send(session_id, f"[cron] Starting: {prompt}", "web")
                        await self.ingest(session_id, prompt, "cron", e_agent)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Cron scheduler error: %s", exc, exc_info=True)

    async def _load_crons(self, path: Path) -> list[dict]:
        try:
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, path.read_text, "utf-8")
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return []

    # ------------------------------------------------------------------
    # Agent loop factory (lazy import avoids circular deps)
    # ------------------------------------------------------------------

    async def _ensure_agent_loop(self) -> None:
        """Lazily initialize the agent loop on first use.

        Uses an asyncio.Lock so concurrent messages don't double-initialize.
        The GIL-heavy sentence_transformers import runs in a thread via
        run_in_executor; since this only executes on first message (not at
        startup), the HTTP server is already fully responsive before we get here.
        """
        if self._agent_loop is not None:
            return
        async with self._agent_loop_lock:
            # Double-check after acquiring lock
            if self._agent_loop is not None:
                return
            logger.info("Initializing agent loop (first message) ...")
            try:
                self._agent_loop = await self._build_agent_loop()
                logger.info("Agent loop ready")
            except Exception as exc:
                logger.error("Agent loop init failed: %s", exc, exc_info=True)

    async def _init_agent_loop(self) -> None:
        """Legacy entry point kept for compatibility — delegates to _ensure_agent_loop."""
        await self._ensure_agent_loop()

    async def _build_agent_loop(self) -> Any:
        # Run in executor to avoid blocking the event loop during slow imports
        # (sentence_transformers, torch, etc. can take 10-30s to import)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._build_agent_loop_sync)

    def _build_agent_loop_sync(self) -> Any:
        from .agent_loop import AgentLoop
        from .core.context_builder import ContextBuilder
        from .core.memory import MemorySystem
        from .tools import register_all_tools
        from .agent_loop import register_all_tools as register_conduit_web_tools
        memory = MemorySystem(agent_id=self._cfg.agent_name)
        # Index all workspace .md files (including MEMORY.md) into SQLite so
        # that asearch() can retrieve them semantically each turn.  Idempotent:
        # already-indexed files are skipped based on source_file path key.
        # Prefer the config-declared workspace_dir (e.g. ~/.cato/workspace).
        # Fall back to the old per-agent path so existing installs aren't broken.
        _raw_ws = getattr(self._cfg, "workspace_dir", None)
        workspace_dir = (
            Path(_raw_ws).expanduser().resolve()
            if _raw_ws
            else _CATO_DIR / self._cfg.agent_name / "workspace"
        )
        if workspace_dir.exists():
            try:
                n = memory.load_workspace_files(workspace_dir)
                logger.info("Indexed %d new chunks from workspace at startup", n)
            except Exception as exc:
                logger.warning("workspace indexing failed (non-fatal): %s", exc)
        ctx    = ContextBuilder(max_tokens=self._cfg.context_budget_tokens)
        loop = AgentLoop(
            config=self._cfg, budget=self._budget, vault=self._vault,
            memory=memory, context_builder=ctx,
            on_tool_progress=self._on_tool_progress,
            on_progress_event=self._on_progress_event,
        )
        register_all_tools(loop)  # shell, file, memory, browser (Conduit when conduit_enabled)
        register_conduit_web_tools(loop.register_tool, self._cfg)  # web.search, web.code, etc. with config
        return loop

    # ------------------------------------------------------------------ #
    # BH-011 — Tool-progress callback for the activity indicator         #
    # ------------------------------------------------------------------ #
    async def _on_tool_progress(self, tool_name: str, summary: str, status: str) -> None:
        """Receive start/end events from AgentLoop._dispatch_with_progress.

        Updates the in-memory activity state (consumed by GET /api/activity
        and the WS `activity` event) so the desktop indicator can show the
        currently-running tool, not just busy/idle.
        """
        import time as _time
        if not isinstance(self._activity_state, dict):
            self._activity_state = {}
        if status == "start":
            self._activity_state["current_tool"] = summary
            self._activity_state["tool_started_at"] = _time.time()
        else:  # "end" (or anything non-start) clears the in-flight tool
            self._activity_state.pop("current_tool", None)
            self._activity_state.pop("tool_started_at", None)
        # Re-broadcast so connected WS clients see the change immediately.
        await self._ws_broadcast({
            "type": "activity",
            "busy": self._activity_state.get("busy", False),
            "session_id": self._activity_state.get("session_id", ""),
            "task": self._activity_state.get("task", ""),
            "current_tool": self._activity_state.get("current_tool"),
            "tool_started_at": self._activity_state.get("tool_started_at"),
        })

    # ------------------------------------------------------------------ #
    # Claude-Code-style live work feed                                   #
    # ------------------------------------------------------------------ #
    async def _on_progress_event(self, event: dict) -> None:
        """Broadcast a structured progress event over the WebSocket.

        The agent loop emits 8 event kinds (turn_start, llm_start,
        llm_token, llm_end, tool_start, tool_end, turn_end, session_end)
        which the desktop ProgressFeed renders as a live work indicator.

        Broadcasts are best-effort: dead clients are pruned in
        ``_ws_broadcast`` itself.  Never raises (the agent loop relies
        on the emitter swallowing callback errors).
        """
        try:
            await self._ws_broadcast(event)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("progress event broadcast failed: %s", exc)
