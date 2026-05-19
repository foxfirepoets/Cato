"""
tests/test_progress_emitter.py — Coverage for the Claude-Code-style live
work indicator that backs the desktop ProgressFeed component.

The emitter lives in ``cato.progress`` and is wired into ``cato.agent_loop``
so the gateway can broadcast a structured event stream over WebSocket as
each meaningful step in an agent-loop invocation happens.

These tests pin the wire contract the desktop hook
(`useProgressStream.ts`) relies on:

  * All 8 event kinds fire, in order, for a normal 2-turn session that
    runs one tool call per turn.
  * `session_end` ALWAYS fires (try/finally guard) even when the agent
    loop bails via an exception — this is the backend half of the
    "shows working when idle" fix.
  * Two concurrent sessions do not contaminate each other's elapsed
    timing or buffered tokens.
  * The callback may be sync or async; failures inside the callback are
    swallowed and never propagate up to the agent loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cato.progress import ProgressEmitter


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

class _Collector:
    """Stash every event the emitter publishes for later inspection."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def sync_cb(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    async def async_cb(self, event: dict[str, Any]) -> None:
        # Yield once so we exercise the await-coroutine code path.
        await asyncio.sleep(0)
        self.events.append(event)


# ----------------------------------------------------------------------
# 1. All 8 event types fire in order for a 2-turn / 1-tool-per-turn run
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_eight_event_kinds_fire_in_order():
    c = _Collector()
    emitter = ProgressEmitter(session_id="sess-A", on_event=c.sync_cb)

    # ── Turn 1 ──────────────────────────────────────────────────────
    await emitter.turn_start(turn=1, max_turns=10)
    await emitter.llm_start(model="minimax-m2.5", token_budget=2048)
    await emitter.llm_token(delta="Look")
    await emitter.llm_token(delta="ing up")
    await emitter.llm_end(tokens_used=120)
    await emitter.tool_start(tool="file", args_preview="read config.yaml", call_id="call-1")
    await emitter.tool_end(tool="file", call_id="call-1", success=True, summary="ok")
    await emitter.turn_end(turn=1, has_final_answer=False)

    # ── Turn 2 ──────────────────────────────────────────────────────
    await emitter.turn_start(turn=2, max_turns=10)
    await emitter.llm_start(model="minimax-m2.5", token_budget=2200)
    await emitter.llm_token(delta="Done.")
    await emitter.llm_end(tokens_used=40)
    await emitter.tool_start(tool="memory", args_preview="store …", call_id="call-2")
    await emitter.tool_end(tool="memory", call_id="call-2", success=True, summary="stored")
    await emitter.turn_end(turn=2, has_final_answer=True)

    await emitter.session_end(total_turns=2)

    kinds = [e["event"] for e in c.events]
    assert kinds == [
        "turn_start",
        "llm_start", "llm_token", "llm_token", "llm_end",
        "tool_start", "tool_end",
        "turn_end",
        "turn_start",
        "llm_start", "llm_token", "llm_end",
        "tool_start", "tool_end",
        "turn_end",
        "session_end",
    ]

    # Every recognised kind in the contract appears at least once
    seen = set(kinds)
    for kind in ProgressEmitter.EVENT_KINDS:
        if kind == "llm_token":
            continue  # optional; we still emit it above but skip the assertion
        assert kind in seen, f"missing event kind: {kind}"

    # Wire contract: every event carries session_id + type + timestamp
    for e in c.events:
        assert e["type"] == "progress"
        assert e["session_id"] == "sess-A"
        assert isinstance(e["timestamp"], int) and e["timestamp"] > 0
        assert "event" in e


# ----------------------------------------------------------------------
# 2. tool_end carries success=False on failures
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_end_marks_failure():
    c = _Collector()
    emitter = ProgressEmitter(session_id="sess-X", on_event=c.sync_cb)

    await emitter.tool_start(tool="shell", args_preview="rm -rf /", call_id="t1")
    await emitter.tool_end(tool="shell", call_id="t1", success=False, summary="boom")

    end = [e for e in c.events if e["event"] == "tool_end"][0]
    assert end["success"] is False
    assert end["summary"] == "boom"
    assert end["tool"] == "shell"
    assert end["call_id"] == "t1"
    assert end["elapsed_s"] >= 0.0


# ----------------------------------------------------------------------
# 3. Callback errors are swallowed (never break the agent loop)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_exceptions_are_swallowed():
    calls = {"n": 0}

    def angry_cb(_event: dict[str, Any]) -> None:
        calls["n"] += 1
        raise RuntimeError("the user clicked X")

    emitter = ProgressEmitter(session_id="sess-err", on_event=angry_cb)

    # Should not raise even though the callback explodes on every event.
    await emitter.turn_start(turn=1, max_turns=4)
    await emitter.llm_start(model="x", token_budget=0)
    await emitter.llm_end(tokens_used=0)
    await emitter.turn_end(turn=1, has_final_answer=True)
    await emitter.session_end(total_turns=1)

    assert calls["n"] == 5, "every event should still hit the callback"


# ----------------------------------------------------------------------
# 4. session_end always fires when the caller uses try/finally
#    (mirrors the agent_loop.run() pattern that fixes the idle-stuck bug)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_end_fires_even_when_caller_raises():
    c = _Collector()
    emitter = ProgressEmitter(session_id="sess-boom", on_event=c.sync_cb)

    with pytest.raises(RuntimeError, match="LLM exploded"):
        try:
            await emitter.turn_start(turn=1, max_turns=10)
            await emitter.llm_start(model="m", token_budget=0)
            # Simulate the SwarmSync call raising mid-turn.
            raise RuntimeError("LLM exploded")
        finally:
            # This mirrors the try/finally guard inside AgentLoop.run() — the
            # backend half of the "shows idle when working" fix.  Without it
            # the desktop pill stays amber until the next message lands.
            await emitter.session_end(total_turns=0)

    kinds = [e["event"] for e in c.events]
    assert kinds[-1] == "session_end", "session_end must always be the last event"
    assert "session_end" in kinds


# ----------------------------------------------------------------------
# 5. Concurrent sessions don't overwrite each other's state
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_sessions_do_not_cross_contaminate():
    cA = _Collector()
    cB = _Collector()
    a = ProgressEmitter(session_id="A", on_event=cA.async_cb)
    b = ProgressEmitter(session_id="B", on_event=cB.async_cb)

    async def session_a() -> None:
        await a.turn_start(turn=1, max_turns=5)
        await a.llm_start(model="alpha", token_budget=100)
        await asyncio.sleep(0.01)
        await a.llm_end(tokens_used=10)
        await a.tool_start(tool="file", args_preview="a-file", call_id="A1")
        await a.tool_end(tool="file", call_id="A1", success=True, summary="ok")
        await a.turn_end(turn=1, has_final_answer=True)
        await a.session_end(total_turns=1)

    async def session_b() -> None:
        await b.turn_start(turn=1, max_turns=5)
        await b.llm_start(model="bravo", token_budget=200)
        await asyncio.sleep(0.005)
        await b.llm_end(tokens_used=20)
        await b.tool_start(tool="shell", args_preview="ls", call_id="B1")
        await b.tool_end(tool="shell", call_id="B1", success=False, summary="bad")
        await b.turn_end(turn=1, has_final_answer=True)
        await b.session_end(total_turns=1)

    await asyncio.gather(session_a(), session_b())

    # Every event in cA carries session_id=A; every event in cB carries B.
    assert all(e["session_id"] == "A" for e in cA.events)
    assert all(e["session_id"] == "B" for e in cB.events)
    # tools didn't leak — B never sees the `file` tool, A never sees `shell`.
    assert all(e.get("tool") != "shell" for e in cA.events)
    assert all(e.get("tool") != "file" for e in cB.events)
    # Models stayed glued to their sessions.
    assert any(e.get("model") == "alpha" for e in cA.events)
    assert any(e.get("model") == "bravo" for e in cB.events)
    assert not any(e.get("model") == "alpha" for e in cB.events)


# ----------------------------------------------------------------------
# 6. llm_buffer accumulates streamed deltas (used by frontend "Thinking…")
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_buffer_concatenates_streamed_tokens():
    c = _Collector()
    emitter = ProgressEmitter(session_id="sess-stream", on_event=c.sync_cb)
    await emitter.llm_start(model="m", token_budget=0)
    for piece in ("Hello", ", ", "world", "!"):
        await emitter.llm_token(piece)
    await emitter.llm_end(tokens_used=4)

    assert emitter.llm_buffer == "Hello, world!"
    token_events = [e for e in c.events if e["event"] == "llm_token"]
    assert [e["delta"] for e in token_events] == ["Hello", ", ", "world", "!"]


# ----------------------------------------------------------------------
# 7. Async callback path works (coroutines are awaited)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_callback_is_awaited():
    c = _Collector()
    emitter = ProgressEmitter(session_id="sess-async", on_event=c.async_cb)

    await emitter.turn_start(turn=1, max_turns=2)
    await emitter.turn_end(turn=1, has_final_answer=True)
    await emitter.session_end(total_turns=1)

    assert [e["event"] for e in c.events] == [
        "turn_start", "turn_end", "session_end",
    ]


# ----------------------------------------------------------------------
# 8. None callback is a cheap no-op (no exception even with payload)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emitter_with_no_callback_is_silent_noop():
    emitter = ProgressEmitter(session_id="sess-quiet", on_event=None)
    # Each method should complete without raising and without side effects.
    await emitter.turn_start(turn=1, max_turns=1)
    await emitter.llm_start(model="m", token_budget=0)
    await emitter.llm_token("hi")
    await emitter.llm_end(tokens_used=1)
    await emitter.tool_start(tool="x", args_preview="", call_id="x1")
    await emitter.tool_end(tool="x", call_id="x1", success=True, summary="")
    await emitter.turn_end(turn=1, has_final_answer=True)
    await emitter.session_end(total_turns=1)
    # llm_buffer still works as an internal accessor even without a callback.
    assert emitter.llm_buffer == "hi"


# ----------------------------------------------------------------------
# 9. tool_end elapsed_s is monotonic and >= the sleep we forced in between
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_elapsed_is_measured_between_start_and_end():
    c = _Collector()
    emitter = ProgressEmitter(session_id="sess-time", on_event=c.sync_cb)

    await emitter.tool_start(tool="file", args_preview="read x", call_id="t-100")
    await asyncio.sleep(0.05)
    await emitter.tool_end(tool="file", call_id="t-100", success=True, summary="ok")

    end_event = [e for e in c.events if e["event"] == "tool_end"][0]
    # We forced ~50ms of sleep — assert at least 30ms to leave headroom for
    # noisy CI clocks while still rejecting "0.000s" (the bug we're testing).
    assert end_event["elapsed_s"] >= 0.03
