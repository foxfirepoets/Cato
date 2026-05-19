"""
cato/progress.py — Claude-Code-style live work indicator events.

The agent loop emits a stream of fine-grained `progress` events as it
processes a single user message.  The gateway wires those events onto
the WebSocket so the desktop UI can render a real-time feed (turn
boundaries, LLM thinking, tool dispatch, results) instead of the bare
"Working…" pill that does not actually tell the user what is happening.

Event vocabulary (kind):

    turn_start    {"turn": int, "max_turns": int}
    llm_start     {"model": str, "token_budget": int}
    llm_token     {"delta": str}                 # only when streaming
    llm_end       {"elapsed_s": float, "tokens_used": int}
    tool_start    {"tool": str, "args_preview": str, "call_id": str}
    tool_end      {"call_id": str, "tool": str, "success": bool,
                   "elapsed_s": float, "summary": str}
    turn_end      {"turn": int, "has_final_answer": bool}
    session_end   {"total_turns": int, "total_elapsed_s": float}

Every emitted event also carries:

    type:        "progress"            (fixed; identifies the WS message)
    session_id:  str                   (the active session)
    event:       <kind>                (one of the strings above)
    timestamp:   int (ms since epoch)
    + the kind-specific payload merged in at the top level for ease of
      consumption on the wire.

A single callable ``on_event(payload: dict) -> Any`` is accepted; the
emitter never raises if the callback errors so progress reporting can
never break the underlying agent loop.  The callback may be sync or
async (coroutine return values are awaited).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger(__name__)

# A progress callback receives a single dict and may return either None or a
# coroutine to be awaited.  Failures are swallowed (logged at debug only).
ProgressCallback = Callable[[dict[str, Any]], Union[None, Awaitable[None]]]


def _now_ms() -> int:
    return int(time.time() * 1000)


class ProgressEmitter:
    """Emits structured progress events for one agent-loop invocation.

    Construct one per session.  Pass the instance into AgentLoop so each
    meaningful step (turn boundary, LLM call, tool call) fires an event.
    """

    # All recognised event names (kept here so tests can pin the contract).
    EVENT_KINDS = (
        "turn_start",
        "llm_start",
        "llm_token",
        "llm_end",
        "tool_start",
        "tool_end",
        "turn_end",
        "session_end",
    )

    def __init__(
        self,
        session_id: str,
        on_event: Optional[ProgressCallback] = None,
    ) -> None:
        self.session_id = session_id
        self._on_event = on_event
        self._started_at = time.monotonic()
        # Per-turn / per-LLM start timestamps so the emitter can compute
        # elapsed_s without callers having to thread a stopwatch through.
        self._llm_started_at: Optional[float] = None
        self._tool_started_at: dict[str, float] = {}
        # Buffer streaming LLM tokens so an optional consumer can grab the
        # complete text on llm_end without re-collecting.
        self._llm_buffer: list[str] = []
        # Lock to serialise async dispatch so concurrent emits do not
        # interleave on the wire when the callback is async.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, kind: str, payload: dict[str, Any]) -> None:
        if kind not in self.EVENT_KINDS:
            # Tolerate forward-compatible event names but log for diagnosis.
            logger.debug("ProgressEmitter: unknown event kind %r", kind)
        event = {
            "type": "progress",
            "session_id": self.session_id,
            "event": kind,
            "timestamp": _now_ms(),
        }
        event.update(payload)
        if self._on_event is None:
            return
        try:
            async with self._lock:
                res = self._on_event(event)
                if asyncio.iscoroutine(res):
                    await res
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("ProgressEmitter callback failed for %s: %s", kind, exc)

    # ------------------------------------------------------------------
    # Turn-level events
    # ------------------------------------------------------------------

    async def turn_start(self, turn: int, max_turns: int) -> None:
        await self._dispatch("turn_start", {"turn": turn, "max_turns": max_turns})

    async def turn_end(self, turn: int, has_final_answer: bool) -> None:
        await self._dispatch(
            "turn_end",
            {"turn": turn, "has_final_answer": bool(has_final_answer)},
        )

    # ------------------------------------------------------------------
    # LLM call events
    # ------------------------------------------------------------------

    async def llm_start(self, model: str, token_budget: int = 0) -> None:
        self._llm_started_at = time.monotonic()
        self._llm_buffer = []
        await self._dispatch(
            "llm_start",
            {"model": model or "", "token_budget": int(token_budget or 0)},
        )

    async def llm_token(self, delta: str) -> None:
        if not delta:
            return
        self._llm_buffer.append(delta)
        await self._dispatch("llm_token", {"delta": delta})

    async def llm_end(self, tokens_used: int = 0) -> None:
        elapsed = 0.0
        if self._llm_started_at is not None:
            elapsed = max(0.0, time.monotonic() - self._llm_started_at)
        self._llm_started_at = None
        await self._dispatch(
            "llm_end",
            {"elapsed_s": round(elapsed, 3), "tokens_used": int(tokens_used or 0)},
        )

    # ------------------------------------------------------------------
    # Tool dispatch events
    # ------------------------------------------------------------------

    async def tool_start(self, tool: str, args_preview: str, call_id: str) -> None:
        # Always record the start time so tool_end can compute elapsed
        # regardless of whether the caller passes call_id back.
        key = call_id or tool
        self._tool_started_at[key] = time.monotonic()
        await self._dispatch(
            "tool_start",
            {
                "tool": tool or "",
                "args_preview": args_preview or "",
                "call_id": call_id or "",
            },
        )

    async def tool_end(
        self,
        tool: str,
        call_id: str,
        success: bool,
        summary: str = "",
    ) -> None:
        key = call_id or tool
        started = self._tool_started_at.pop(key, None)
        elapsed = 0.0
        if started is not None:
            elapsed = max(0.0, time.monotonic() - started)
        await self._dispatch(
            "tool_end",
            {
                "tool": tool or "",
                "call_id": call_id or "",
                "success": bool(success),
                "elapsed_s": round(elapsed, 3),
                "summary": summary or "",
            },
        )

    # ------------------------------------------------------------------
    # Session boundary
    # ------------------------------------------------------------------

    async def session_end(self, total_turns: int) -> None:
        total_elapsed = max(0.0, time.monotonic() - self._started_at)
        await self._dispatch(
            "session_end",
            {
                "total_turns": int(total_turns),
                "total_elapsed_s": round(total_elapsed, 3),
            },
        )

    # ------------------------------------------------------------------
    # Convenience accessors (used in tests)
    # ------------------------------------------------------------------

    @property
    def llm_buffer(self) -> str:
        return "".join(self._llm_buffer)
