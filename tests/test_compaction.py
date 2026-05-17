"""
tests/test_compaction.py — Unit tests for AgentLoop compaction and
ContextBuilder distilled-summary injection.

Covers:
  - _maybe_compact: no-op when transcript is within limits
  - _maybe_compact: triggers when turn count exceeds COMPACT_TURN_THRESHOLD
  - _maybe_compact: triggers when history tokens exceed COMPACT_TOKEN_THRESHOLD
  - _maybe_compact: rewrites transcript to HISTORY_WINDOW turns after compaction
  - _maybe_compact: stores distillation in memory
  - _load_distilled_summary: returns None when no summaries exist
  - _load_distilled_summary: formats summary block correctly
  - ContextBuilder: injects distilled_summary into system prompt
  - ContextBuilder: distilled_summary absent when None passed
  - ContextBuilder: distilled_summary uses at most half of tier1_memory slot
  - COMPACT_TOKEN_THRESHOLD constant is 2500
  - COMPACT_TURN_THRESHOLD constant is 30
  - HISTORY_WINDOW constant is 12
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cato.agent_loop import (
    COMPACT_TOKEN_THRESHOLD,
    COMPACT_TURN_THRESHOLD,
    HISTORY_WINDOW,
    AgentLoop,
    _transcript_path,
)
from cato.core.context_builder import ContextBuilder, SlotBudget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_turns(n: int, content: str = "hello world") -> list[dict]:
    """Generate n alternating user/assistant JSONL records."""
    turns = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        turns.append({"role": role, "content": f"{content} {i}", "ts": "2026-01-01T00:00:00Z", "session_id": "sess"})
    return turns


def _write_transcript(path: Path, turns: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for t in turns:
            fh.write(json.dumps(t) + "\n")


def _read_transcript(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _make_mock_loop() -> AgentLoop:
    """Build a minimal AgentLoop with mocked dependencies."""
    cfg = MagicMock()
    cfg.swarmsync_enabled = False
    cfg.max_planning_turns = 2
    cfg.audit_enabled = False
    cfg.safety_mode = "strict"
    cfg.workspace_dir = None
    cfg.default_model = "openrouter/minimax/minimax-m2.5"
    cfg.swarmsync_api_url = "https://api.swarmsync.ai/v1/chat/completions"

    budget = MagicMock()
    budget.check_and_deduct = AsyncMock()
    budget._last_call_cost = 0.0
    budget.format_footer = MagicMock(return_value="")

    vault = MagicMock()
    vault.get = MagicMock(return_value=None)

    memory = MagicMock()
    memory.asearch = AsyncMock(return_value=[])
    memory.astore = AsyncMock()
    memory.store_distillation = MagicMock()
    memory.load_recent_distillations = MagicMock(return_value=[])

    ctx = ContextBuilder()

    loop = AgentLoop.__new__(AgentLoop)
    loop._cfg = cfg
    loop._budget = budget
    loop._vault = vault
    loop._memory = memory
    loop._ctx = ctx
    loop._audit_log = None
    loop._safety = MagicMock()
    loop._safety.check_and_confirm = MagicMock(return_value=True)
    loop._bg_tasks = set()

    from cato.router import ModelRouter
    loop._router = ModelRouter(vault=vault, preferred_model=cfg.default_model)

    return loop


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_compact_token_threshold():
    assert COMPACT_TOKEN_THRESHOLD == 9000


def test_compact_turn_threshold():
    assert COMPACT_TURN_THRESHOLD == 30


def test_history_window():
    assert HISTORY_WINDOW == 12


# ---------------------------------------------------------------------------
# _maybe_compact: no-op cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_compact_noop_when_no_transcript(tmp_path):
    loop = _make_mock_loop()
    fake_path = tmp_path / "nonexistent.jsonl"
    # Should not raise
    await loop._maybe_compact(fake_path, "sess1")
    loop._memory.store_distillation.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_compact_noop_when_within_window(tmp_path):
    """When turn count <= HISTORY_WINDOW, compaction must not trigger."""
    loop = _make_mock_loop()
    tpath = tmp_path / "sess.jsonl"
    turns = _make_turns(HISTORY_WINDOW)  # exactly at the window limit
    _write_transcript(tpath, turns)

    await loop._maybe_compact(tpath, "sess1")
    loop._memory.store_distillation.assert_not_called()
    # Transcript unchanged
    assert len(_read_transcript(tpath)) == HISTORY_WINDOW


@pytest.mark.asyncio
async def test_maybe_compact_noop_when_tokens_and_turns_below_threshold(tmp_path):
    """Small transcript above HISTORY_WINDOW but below both thresholds → no-op."""
    loop = _make_mock_loop()
    tpath = tmp_path / "sess.jsonl"
    # 15 turns, each with very short content → well below 2500 token threshold
    turns = _make_turns(15, content="hi")
    _write_transcript(tpath, turns)

    await loop._maybe_compact(tpath, "sess1")
    loop._memory.store_distillation.assert_not_called()


# ---------------------------------------------------------------------------
# _maybe_compact: trigger on turn count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_compact_triggers_on_turn_count(tmp_path):
    """When total turns > COMPACT_TURN_THRESHOLD, compaction must fire."""
    loop = _make_mock_loop()
    tpath = tmp_path / "sess.jsonl"
    # One more than the threshold
    turns = _make_turns(COMPACT_TURN_THRESHOLD + 1, content="hi")
    _write_transcript(tpath, turns)

    await loop._maybe_compact(tpath, "sess1")
    loop._memory.store_distillation.assert_called_once()


@pytest.mark.asyncio
async def test_maybe_compact_rewrites_transcript_to_window(tmp_path):
    """After compaction, transcript must contain exactly HISTORY_WINDOW records."""
    loop = _make_mock_loop()
    tpath = tmp_path / "sess.jsonl"
    turns = _make_turns(COMPACT_TURN_THRESHOLD + 5, content="hi")
    _write_transcript(tpath, turns)

    await loop._maybe_compact(tpath, "sess1")
    remaining = _read_transcript(tpath)
    assert len(remaining) == HISTORY_WINDOW


@pytest.mark.asyncio
async def test_maybe_compact_keeps_most_recent_turns(tmp_path):
    """The kept turns must be the LAST HISTORY_WINDOW records, not the first."""
    loop = _make_mock_loop()
    tpath = tmp_path / "sess.jsonl"
    n = COMPACT_TURN_THRESHOLD + 5
    turns = _make_turns(n, content="msg")
    _write_transcript(tpath, turns)

    await loop._maybe_compact(tpath, "sess1")
    remaining = _read_transcript(tpath)
    # The last record should be from the original last turn
    expected_last_content = turns[-1]["content"]
    assert remaining[-1]["content"] == expected_last_content


# ---------------------------------------------------------------------------
# _maybe_compact: trigger on token count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_compact_triggers_on_token_threshold(tmp_path):
    """When history tokens > COMPACT_TOKEN_THRESHOLD, compaction fires even if turns are few."""
    loop = _make_mock_loop()
    tpath = tmp_path / "sess.jsonl"
    # 20 turns (above HISTORY_WINDOW but below COMPACT_TURN_THRESHOLD)
    # Use very long content to push token count above threshold (9000).
    # ~500 words ≈ 500 tokens each × 20 turns = ~10,000 tokens > 9000 threshold.
    long_content = "word " * 500  # ~500 words ≈ 500 tokens each
    turns = _make_turns(20, content=long_content)
    _write_transcript(tpath, turns)

    await loop._maybe_compact(tpath, "sess1")
    loop._memory.store_distillation.assert_called_once()


# ---------------------------------------------------------------------------
# _maybe_compact: distillation storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_compact_stores_distillation(tmp_path):
    """store_distillation must be called with a DistillationResult."""
    from cato.core.distiller import DistillationResult

    loop = _make_mock_loop()
    tpath = tmp_path / "sess.jsonl"
    turns = _make_turns(COMPACT_TURN_THRESHOLD + 1, content="key point is important")
    _write_transcript(tpath, turns)

    await loop._maybe_compact(tpath, "sess1")

    call_args = loop._memory.store_distillation.call_args
    assert call_args is not None
    result = call_args[0][0]
    assert isinstance(result, DistillationResult)
    assert result.session_id == "sess1"
    assert result.turn_start == 0


# ---------------------------------------------------------------------------
# _load_distilled_summary
# ---------------------------------------------------------------------------


def test_load_distilled_summary_returns_none_when_empty():
    loop = _make_mock_loop()
    loop._memory.load_recent_distillations = MagicMock(return_value=[])
    result = loop._load_distilled_summary("sess1")
    assert result is None


def test_load_distilled_summary_returns_formatted_string():
    loop = _make_mock_loop()
    loop._memory.load_recent_distillations = MagicMock(return_value=[
        {
            "summary": "We decided to use Redis for rate limiting.",
            "key_facts": ["the rate limit is 100/min", "important: burst allowed"],
            "decisions": ["We decided to use Redis"],
            "open_questions": ["Should we add a fallback?"],
        }
    ])
    result = loop._load_distilled_summary("sess1")
    assert result is not None
    assert "Conversation History Summary" in result
    assert "Redis" in result
    assert "rate limit" in result


def test_load_distilled_summary_handles_exception_gracefully():
    loop = _make_mock_loop()
    loop._memory.load_recent_distillations = MagicMock(side_effect=RuntimeError("db error"))
    result = loop._load_distilled_summary("sess1")
    assert result is None


def test_load_distilled_summary_handles_multiple_rows():
    loop = _make_mock_loop()
    loop._memory.load_recent_distillations = MagicMock(return_value=[
        {
            "summary": "First batch summary.",
            "key_facts": ["fact A"],
            "decisions": [],
            "open_questions": [],
        },
        {
            "summary": "Second batch summary.",
            "key_facts": ["fact B"],
            "decisions": ["decided to proceed"],
            "open_questions": [],
        },
    ])
    result = loop._load_distilled_summary("sess1")
    assert result is not None
    assert "First batch" in result
    assert "Second batch" in result


# ---------------------------------------------------------------------------
# ContextBuilder: distilled_summary injection
# ---------------------------------------------------------------------------


def test_context_builder_injects_distilled_summary(tmp_path):
    cb = ContextBuilder()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    summary = "## Conversation History Summary\n**Summary:** We discussed Redis.\n**Key facts:** rate limit is 100/min"
    prompt = cb.build_system_prompt(
        workspace_dir=workspace,
        distilled_summary=summary,
    )
    assert "CONVERSATION_HISTORY_SUMMARY" in prompt
    assert "Redis" in prompt


def test_context_builder_no_summary_block_when_none(tmp_path):
    cb = ContextBuilder()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    prompt = cb.build_system_prompt(
        workspace_dir=workspace,
        distilled_summary=None,
    )
    assert "CONVERSATION_HISTORY_SUMMARY" not in prompt


def test_context_builder_distilled_summary_within_memory_slot(tmp_path):
    """Distilled summary must not exceed half of tier1_memory tokens."""
    cb = ContextBuilder()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Build a very long summary (~2000 words)
    long_summary = "## Conversation History Summary\n" + ("Important fact here. " * 500)
    budget = SlotBudget()
    max_allowed = budget.tier1_memory // 2

    prompt = cb.build_system_prompt(
        workspace_dir=workspace,
        distilled_summary=long_summary,
        slot_budget=budget,
    )

    # Extract only the summary section
    if "CONVERSATION_HISTORY_SUMMARY" in prompt:
        start = prompt.index("<!-- CONVERSATION_HISTORY_SUMMARY -->")
        end = prompt.index("============================================================", start + 1)
        end = prompt.index("============================================================", end + 1)
        summary_section = prompt[start:end + 60]
        actual_tokens = cb.count_tokens(summary_section)
        assert actual_tokens <= max_allowed + 50  # small margin for wrapper markup


def test_context_builder_distilled_summary_absent_when_no_budget(tmp_path):
    """If remaining budget is 0 when summary is reached, skip it gracefully."""
    cb = ContextBuilder()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Fill up workspace with a large SOUL.md
    large_soul = "x " * 3000
    (workspace / "SOUL.md").write_text(large_soul, encoding="utf-8")

    # Use a very tight total budget so remaining hits 0 before summary
    budget = SlotBudget(tier0_identity=200, tier0_agents=0, tier1_skill=0,
                        tier1_memory=0, tier1_tools=0, tier1_history=0, headroom=0, total=200)

    summary = "## Summary\nSome summary text."
    # Should not raise even when budget is exhausted
    prompt = cb.build_system_prompt(
        workspace_dir=workspace,
        distilled_summary=summary,
        slot_budget=budget,
    )
    assert isinstance(prompt, str)
