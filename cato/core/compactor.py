"""Conversation compactor — summarizes older turns to fit more context.

Inspired by Claude Code's /compact. Splits the message log into
'older' and 'recent' halves, summarizes the older half via the LLM,
and replaces it in-place with a single system-level summary entry.

The result preserves continuity (user goals, key decisions, specific
identifiers) while dropping the token cost of full transcript replay.

This module is intentionally framework-agnostic: callers supply the
``llm_call`` coroutine, so the same logic works under SwarmSync,
direct provider routing, or test fakes.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

DEFAULT_KEEP_RECENT = 8      # last N user turns kept verbatim
DEFAULT_TARGET_RATIO = 0.30  # summary should be ~30% of original length

SUMMARIZE_PROMPT = """You are a conversation compaction specialist. Below is a conversation between a user and the Cato AI agent. Your job is to summarize it for context preservation.

Preserve:
- The user's stated goals and intentions
- Key decisions made
- Specific values, IDs, file paths, URLs, or names mentioned
- Outcomes of major actions Cato took (success/failure)
- Any unresolved questions or open threads

Drop:
- Conversational pleasantries
- Verbose tool output (replace with a one-line description of what happened)
- Repeated information
- Cato's chain-of-thought reasoning (just the conclusions)

Target length: ~30% of the original. Use compact prose with bullet points where natural. Output the summary directly — no preamble like "Here is a summary".
"""


@dataclass
class CompactionResult:
    """Outcome of a single ``compact_session`` call."""

    compacted_count: int      # number of turns folded into summary
    kept_count: int           # number of messages in the new list
    summary: str              # the LLM-produced summary
    original_tokens: int      # estimated tokens before
    compacted_tokens: int     # estimated tokens after
    elapsed_s: float


def _flatten_content(content: Any) -> str:
    """Render a message ``content`` value as plain text.

    Handles strings, ``None``, and lists of content blocks (the
    OpenAI / Anthropic multi-block format used for tool results).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if text is None:
                    text = block.get("content", "")
                parts.append(str(text))
            else:
                parts.append(str(block))
        return " ".join(parts)
    return str(content)


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: 1 token ~= 4 chars.

    The exact value doesn't matter — this is only used to report the
    compression ratio back to the user.  Real token accounting happens
    inside the model router on the next turn.
    """
    total = 0
    for m in messages:
        total += len(_flatten_content(m.get("content", ""))) // 4
    return total


async def compact_session(
    *,
    messages: list[dict],
    llm_call: Callable[..., Awaitable[str]],
    keep_recent: int = DEFAULT_KEEP_RECENT,
    model_hint: str | None = None,
) -> tuple[list[dict], CompactionResult]:
    """Compact a conversation.  Returns ``(new_messages, result)``.

    Parameters
    ----------
    messages:
        List of ``{role, content, ...}`` dicts (system / user / assistant /
        tool).  Not mutated.
    llm_call:
        Async callable ``(system_prompt, user_prompt, model=...) -> str``
        used to produce the summary.
    keep_recent:
        Number of the most recent USER turns to preserve verbatim.  Each
        message at or after the boundary is kept (so assistant replies and
        tool outputs for those recent user turns remain intact).
    model_hint:
        Optional model identifier passed through to ``llm_call``.  ``None``
        lets the caller pick.

    Notes
    -----
    System messages at the very top of the history (SOUL.md, IDENTITY.md,
    AGENTS.md, etc.) are treated as identity and always preserved.
    LLM failures degrade gracefully — the original message list is returned
    unchanged with ``compacted_count == 0``.
    """
    started = time.time()
    original_tokens = _estimate_tokens(messages)

    # Identify boundary: keep the most recent N user-assistant pairs verbatim
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_indices) <= keep_recent:
        # Nothing to compact
        return messages, CompactionResult(
            compacted_count=0,
            kept_count=len(messages),
            summary="",
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
            elapsed_s=time.time() - started,
        )

    boundary_idx = user_indices[-keep_recent]
    older = list(messages[:boundary_idx])
    recent = list(messages[boundary_idx:])

    # Extract any system message at the very top (e.g. SOUL.md) — always preserved
    system_prefix: list[dict] = []
    while older and older[0].get("role") == "system":
        system_prefix.append(older.pop(0))

    if not older:
        return messages, CompactionResult(
            compacted_count=0,
            kept_count=len(messages),
            summary="",
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
            elapsed_s=time.time() - started,
        )

    # Render older messages as plain text for the summarizer
    transcript_lines: list[str] = []
    for m in older:
        role = str(m.get("role", "?")).upper()
        content = _flatten_content(m.get("content", ""))
        transcript_lines.append(f"[{role}]\n{content}")
    transcript = "\n\n".join(transcript_lines)

    # Summarize via LLM
    try:
        summary = await llm_call(
            system_prompt=SUMMARIZE_PROMPT,
            user_prompt=transcript,
            model=model_hint,
        )
    except Exception as exc:
        log.exception("compaction LLM call failed")
        return messages, CompactionResult(
            compacted_count=0,
            kept_count=len(messages),
            summary=f"[compaction failed: {exc}]",
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
            elapsed_s=time.time() - started,
        )

    summary_text = (summary or "").strip()
    if not summary_text:
        # LLM returned empty — degrade gracefully, do not lose history
        log.warning("compaction LLM returned empty summary; leaving history unchanged")
        return messages, CompactionResult(
            compacted_count=0,
            kept_count=len(messages),
            summary="",
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
            elapsed_s=time.time() - started,
        )

    # Build new message list: system_prefix + summary as system message + recent
    summary_msg = {
        "role": "system",
        "content": (
            f"<conversation_summary timestamp=\"{int(time.time())}\">\n"
            f"{summary_text}\n"
            f"</conversation_summary>"
        ),
    }
    new_messages = system_prefix + [summary_msg] + recent

    return new_messages, CompactionResult(
        compacted_count=len(older),
        kept_count=len(new_messages),
        summary=summary_text,
        original_tokens=original_tokens,
        compacted_tokens=_estimate_tokens(new_messages),
        elapsed_s=time.time() - started,
    )
