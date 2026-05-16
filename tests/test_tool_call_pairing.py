"""
tests/test_tool_call_pairing.py — Regression tests for BH-007.

Bug:  Cato responded with "The model returned no readable content after
multiple attempts."

Root cause:  ``_recent_turns`` slices ``lines[-HISTORY_WINDOW:]`` and
``_maybe_compact`` rewrites the transcript to its tail without preserving
the ``assistant(tool_calls=[X])`` → ``tool(tool_call_id=X)`` pairing
required by OpenAI/MiniMax.  When the assistant message is dropped but the
tool result survives, the provider rejects the request with::

    Invalid parameter: messages with role 'tool' must be a response to a
    preceeding message with 'tool_calls'.

SwarmSync masks the rejection as an empty completion, the agent falls back
to ``_stream_collect`` with the same broken history, all retries return
empty, and the BH-006 fallback message reaches the user.

Fix:  ``_repair_tool_call_pairing`` walks the message list, drops orphan
tool messages, and strips unfulfilled ``tool_calls`` entries before the
request is submitted.  It is invoked at both ``_recent_turns`` (read path)
and ``_sanitize_messages_for_api`` (chokepoint before SwarmSync submission).

These tests freeze the contract so the bug cannot regress.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cato.agent_loop import (
    AgentLoop,
    _repair_tool_call_pairing,
    _sanitize_messages_for_api,
)


# ---------------------------------------------------------------------------
# Pure repair function
# ---------------------------------------------------------------------------

def test_repair_drops_orphan_tool_with_no_preceding_assistant():
    messages = [
        {"role": "tool", "tool_call_id": "call_lost", "content": "result"},
        {"role": "user", "content": "next"},
    ]
    assert _repair_tool_call_pairing(messages) == [
        {"role": "user", "content": "next"},
    ]


def test_repair_preserves_valid_assistant_tool_pair():
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "shell", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    assert _repair_tool_call_pairing(messages) == messages


def test_repair_strips_unfulfilled_tool_call_when_partial_results_returned():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "shell"}},
                {"id": "call_2", "function": {"name": "file"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        # call_2 never returned → next user message arrives
        {"role": "user", "content": "continue"},
    ]
    repaired = _repair_tool_call_pairing(messages)
    assert repaired == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "shell"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "user", "content": "continue"},
    ]


def test_repair_drops_empty_assistant_when_all_tool_calls_unfulfilled():
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "shell"}}],
        },
        {"role": "user", "content": "stop"},  # tool result never arrived
    ]
    assert _repair_tool_call_pairing(messages) == [
        {"role": "user", "content": "go"},
        {"role": "user", "content": "stop"},
    ]


def test_repair_keeps_assistant_with_visible_text_when_tool_calls_orphaned():
    messages = [
        {
            "role": "assistant",
            "content": "Working on it...",
            "tool_calls": [{"id": "call_1", "function": {"name": "shell"}}],
        },
        {"role": "user", "content": "wait"},
    ]
    repaired = _repair_tool_call_pairing(messages)
    assert repaired == [
        {"role": "assistant", "content": "Working on it..."},
        {"role": "user", "content": "wait"},
    ]


def test_repair_is_idempotent():
    messages = [
        {"role": "tool", "tool_call_id": "ghost", "content": "leftover"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_x", "function": {"name": "shell"}}],
        },
        {"role": "tool", "tool_call_id": "call_x", "content": "ok"},
        {"role": "tool", "tool_call_id": "phantom", "content": "?"},
        {"role": "assistant", "content": "done"},
    ]
    once = _repair_tool_call_pairing(messages)
    twice = _repair_tool_call_pairing(once)
    assert once == twice


def test_repair_tool_with_missing_id_is_dropped():
    messages = [
        {"role": "tool", "content": "no id at all"},  # missing tool_call_id
        {"role": "user", "content": "hi"},
    ]
    assert _repair_tool_call_pairing(messages) == [
        {"role": "user", "content": "hi"},
    ]


def test_repair_handles_empty_input():
    assert _repair_tool_call_pairing([]) == []


def test_repair_does_not_mutate_input():
    original = [
        {"role": "tool", "tool_call_id": "ghost", "content": "x"},
        {"role": "user", "content": "go"},
    ]
    snapshot = json.dumps(original, sort_keys=True)
    _repair_tool_call_pairing(original)
    assert json.dumps(original, sort_keys=True) == snapshot


def test_repair_handles_back_to_back_tool_use_blocks():
    messages = [
        {"role": "user", "content": "do two things"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "a", "function": {"name": "shell"}}],
        },
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "b", "function": {"name": "file"}}],
        },
        {"role": "tool", "tool_call_id": "b", "content": "2"},
        {"role": "assistant", "content": "all done"},
    ]
    assert _repair_tool_call_pairing(messages) == messages


# ---------------------------------------------------------------------------
# Integration with _recent_turns (the real-world failure mode)
# ---------------------------------------------------------------------------

def test_recent_turns_repairs_orphans_when_window_severs_pair(tmp_path: Path):
    """Reproduces the exact failure observed in
    ``cato/sessions/45dfbbbe-...jsonl`` on 2026-05-14: an assistant with
    tool_calls falls outside ``lines[-HISTORY_WINDOW:]`` while its tool
    result stays inside, producing the HTTP 400 that became
    'no readable content'.
    """
    transcript = tmp_path / "session.jsonl"

    records: list[dict] = []
    # Lines 1-2: a tool call/result pair we deliberately push out of window.
    records.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_OUT_OF_WINDOW",
            "type": "function",
            "function": {"name": "shell", "arguments": "{}"},
        }],
    })
    records.append({
        "role": "tool",
        "tool_call_id": "call_OUT_OF_WINDOW",
        "result": "stdout from a long-ago shell call",
    })
    # Lines 3-N: filler turns to push the pair out of the window.
    for i in range(20):
        records.append({"role": "user" if i % 2 == 0 else "assistant",
                        "content": f"chat turn {i}"})
    # Final tool message echoing the same id (the corruption pattern we saw
    # in real transcripts after multiple compactions).
    records.append({"role": "tool", "tool_call_id": "call_OUT_OF_WINDOW",
                    "result": "stale orphan"})

    transcript.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )

    loop = AgentLoop.__new__(AgentLoop)  # bypass __init__
    msgs = loop._recent_turns(transcript, limit=12)

    # Contract check: every `tool` message has a preceding `assistant` whose
    # tool_calls contains the matching id.
    for i, m in enumerate(msgs):
        if m.get("role") != "tool":
            continue
        tcid = m["tool_call_id"]
        # Walk back to find the most recent assistant
        for prev in reversed(msgs[:i]):
            if prev.get("role") == "assistant":
                ids = {tc.get("id") for tc in (prev.get("tool_calls") or [])}
                assert tcid in ids, (
                    f"Orphan tool message at index {i} (id={tcid!r}); "
                    f"preceding assistant tool_calls={ids}"
                )
                break
        else:
            pytest.fail(f"Tool message at index {i} has no preceding assistant")


def test_recent_turns_preserves_intact_pair_inside_window(tmp_path: Path):
    """Sanity check: when the slice keeps both halves of the pair, both
    survive the repair pass."""
    transcript = tmp_path / "session.jsonl"
    records = [
        {"role": "user", "content": "earlier chat"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_keep",
                "type": "function",
                "function": {"name": "shell", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_keep", "result": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    transcript.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )

    loop = AgentLoop.__new__(AgentLoop)
    msgs = loop._recent_turns(transcript, limit=12)

    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert msgs[1]["tool_calls"][0]["id"] == "call_keep"
    assert msgs[2]["tool_call_id"] == "call_keep"


# ---------------------------------------------------------------------------
# Integration with _sanitize_messages_for_api (the SwarmSync chokepoint)
# ---------------------------------------------------------------------------

def test_sanitize_messages_for_api_drops_orphan_tool_message():
    """The SwarmSync path runs everything through
    :func:`_sanitize_messages_for_api`.  Defence-in-depth: even if a future
    code path forgets to call ``_recent_turns``, this chokepoint must not
    forward orphan tool messages to the provider."""
    messages = [
        {"role": "system", "content": "you are cato"},
        {"role": "tool", "tool_call_id": "call_orphan", "content": "leftover"},
        {"role": "user", "content": "hi"},
    ]
    cleaned = _sanitize_messages_for_api(messages)
    assert cleaned == [
        {"role": "system", "content": "you are cato"},
        {"role": "user", "content": "hi"},
    ]


def test_sanitize_messages_for_api_normalises_names_then_repairs_pairing():
    """Both passes run together: dotted tool names get sanitised AND
    orphan tool messages are dropped."""
    messages = [
        {"role": "tool", "tool_call_id": "call_orphan", "content": "drop me"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_keep",
                "type": "function",
                "function": {"name": "web.search", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_keep", "content": "ok"},
    ]
    cleaned = _sanitize_messages_for_api(messages)
    # The orphan is gone; the kept tool_call has its dotted name normalised.
    assert cleaned[0] == {"role": "user", "content": "hi"}
    assert cleaned[1]["role"] == "assistant"
    assert cleaned[1]["tool_calls"][0]["function"]["name"] == "web__search"
    assert cleaned[2] == {"role": "tool", "tool_call_id": "call_keep",
                          "content": "ok"}
