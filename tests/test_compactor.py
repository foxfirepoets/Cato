"""Tests for the conversation compactor.

Covers:
- Short conversations that fall under ``keep_recent`` are no-ops.
- Long conversations get folded down to a single summary system message.
- A system prefix (SOUL.md / IDENTITY.md) survives compaction untouched.
- LLM failures degrade gracefully without losing history.
- Token estimate is non-zero for non-empty input.
- ``model_hint`` is plumbed through to the LLM call.
- Multi-block (list) content is flattened without crashing.
"""
from __future__ import annotations

import pytest

from cato.core.compactor import (
    DEFAULT_KEEP_RECENT,
    CompactionResult,
    SUMMARIZE_PROMPT,
    _estimate_tokens,
    _flatten_content,
    compact_session,
)


async def _fake_llm(*, system_prompt: str, user_prompt: str, model: str | None = None) -> str:
    """Deterministic stand-in for a real LLM call."""
    return "User asked about X. Cato did Y. Outcome: Z."


@pytest.mark.asyncio
async def test_short_conversation_not_compacted():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    new_msgs, result = await compact_session(
        messages=msgs, llm_call=_fake_llm, keep_recent=8,
    )
    assert new_msgs == msgs
    assert result.compacted_count == 0
    assert result.summary == ""
    assert isinstance(result, CompactionResult)


@pytest.mark.asyncio
async def test_long_conversation_gets_compacted():
    msgs: list[dict] = []
    for i in range(20):
        msgs.append({"role": "user", "content": f"user msg {i}"})
        msgs.append({"role": "assistant", "content": f"assistant reply {i}"})

    new_msgs, result = await compact_session(
        messages=msgs, llm_call=_fake_llm, keep_recent=5,
    )

    # Older turns were folded; new list is strictly smaller.
    assert result.compacted_count > 0
    assert len(new_msgs) < len(msgs)
    # The recent 5 user messages must still be present verbatim
    user_texts_in_new = [
        m["content"] for m in new_msgs if m.get("role") == "user"
    ]
    assert user_texts_in_new[-5:] == [f"user msg {i}" for i in range(15, 20)]
    # Summary message is in the new list
    summary_msgs = [
        m for m in new_msgs
        if m.get("role") == "system"
        and "<conversation_summary" in str(m.get("content", ""))
    ]
    assert len(summary_msgs) == 1
    # Token estimate is sensible (must be smaller after compaction)
    assert result.original_tokens > result.compacted_tokens
    # Reporting fields populated
    assert result.summary
    assert result.elapsed_s >= 0


@pytest.mark.asyncio
async def test_system_prefix_preserved():
    msgs: list[dict] = [
        {"role": "system", "content": "SOUL.md content"},
        {"role": "system", "content": "IDENTITY.md content"},
    ]
    for i in range(15):
        msgs.append({"role": "user", "content": f"user msg {i}"})
        msgs.append({"role": "assistant", "content": f"assistant reply {i}"})

    new_msgs, result = await compact_session(
        messages=msgs, llm_call=_fake_llm, keep_recent=5,
    )

    # System prefix must still be at the top
    assert new_msgs[0]["role"] == "system"
    assert new_msgs[1]["role"] == "system"
    assert "SOUL.md" in new_msgs[0]["content"]
    assert "IDENTITY.md" in new_msgs[1]["content"]
    # Followed by exactly one synthetic summary system message
    assert new_msgs[2]["role"] == "system"
    assert "<conversation_summary" in new_msgs[2]["content"]
    # Then the recent verbatim turns
    assert new_msgs[3]["role"] == "user"
    assert result.compacted_count > 0


@pytest.mark.asyncio
async def test_llm_failure_returns_unchanged():
    async def _failing_llm(*, system_prompt: str, user_prompt: str,
                           model: str | None = None) -> str:
        raise RuntimeError("simulated LLM failure")

    msgs: list[dict] = []
    for i in range(20):
        msgs.append({"role": "user", "content": f"msg {i}"})
        msgs.append({"role": "assistant", "content": f"reply {i}"})

    new_msgs, result = await compact_session(
        messages=msgs, llm_call=_failing_llm, keep_recent=5,
    )

    # On failure, returns original
    assert new_msgs == msgs
    assert result.compacted_count == 0
    assert "simulated LLM failure" in result.summary


@pytest.mark.asyncio
async def test_empty_llm_response_preserves_history():
    async def _empty_llm(*, system_prompt: str, user_prompt: str,
                         model: str | None = None) -> str:
        return "   \n  "

    msgs: list[dict] = []
    for i in range(20):
        msgs.append({"role": "user", "content": f"msg {i}"})
        msgs.append({"role": "assistant", "content": f"reply {i}"})

    new_msgs, result = await compact_session(
        messages=msgs, llm_call=_empty_llm, keep_recent=5,
    )

    assert new_msgs == msgs
    assert result.compacted_count == 0


@pytest.mark.asyncio
async def test_model_hint_passed_through():
    seen: dict[str, str | None] = {"model": "sentinel"}

    async def _capturing_llm(*, system_prompt: str, user_prompt: str,
                             model: str | None = None) -> str:
        seen["model"] = model
        return "summary text"

    msgs: list[dict] = []
    for i in range(20):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})

    _, _ = await compact_session(
        messages=msgs, llm_call=_capturing_llm, keep_recent=5,
        model_hint="openrouter/anthropic/claude-haiku",
    )

    assert seen["model"] == "openrouter/anthropic/claude-haiku"


@pytest.mark.asyncio
async def test_multiblock_content_flattens():
    """Content blocks (list of dicts) must not crash the compactor."""
    msgs: list[dict] = []
    for i in range(15):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({
            "role": "assistant",
            "content": [{"type": "text", "text": f"answer {i}"}],
        })

    new_msgs, result = await compact_session(
        messages=msgs, llm_call=_fake_llm, keep_recent=5,
    )

    assert result.compacted_count > 0
    assert len(new_msgs) < len(msgs)


def test_estimate_tokens_nonzero_for_text():
    assert _estimate_tokens([
        {"role": "user", "content": "a" * 100}
    ]) >= 25


def test_estimate_tokens_handles_blocks():
    assert _estimate_tokens([
        {"role": "assistant", "content": [{"text": "hello world" * 10}]}
    ]) > 0


def test_flatten_content_variants():
    assert _flatten_content(None) == ""
    assert _flatten_content("hi") == "hi"
    assert _flatten_content(["a", "b"]) == "a b"
    assert _flatten_content([{"text": "x"}, {"text": "y"}]) == "x y"
    assert _flatten_content([{"content": "z"}]) == "z"


def test_default_keep_recent_is_eight():
    assert DEFAULT_KEEP_RECENT == 8


def test_summarize_prompt_mentions_preservation():
    assert "Preserve" in SUMMARIZE_PROMPT
    assert "30%" in SUMMARIZE_PROMPT
