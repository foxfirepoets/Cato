from __future__ import annotations

import pytest

from cato.agent_loop import (
    AgentLoop,
    ToolCall,
    _parse_tool_calls_openai,
    _tool_call_to_openai,
    _tool_result_message,
)


def test_openai_tool_call_parser_preserves_valid_arguments():
    calls = _parse_tool_calls_openai({
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "web.search",
                "arguments": '{"query": "hermes agent"}',
            },
        }]
    })

    assert calls == [ToolCall(name="web.search", args={"query": "hermes agent"}, call_id="call_1", raw={
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "web.search",
            "arguments": '{"query": "hermes agent"}',
        },
    })]


def test_openai_tool_call_parser_marks_invalid_json_recoverable():
    calls = _parse_tool_calls_openai({
        "tool_calls": [{
            "id": "call_bad",
            "function": {"name": "shell.exec", "arguments": '{"command":'},
        }]
    })

    assert calls[0].name == "shell.exec"
    assert calls[0].args == {}
    assert "Invalid JSON arguments" in calls[0].error


def test_tool_call_serialization_uses_provider_safe_function_name():
    wire = _tool_call_to_openai(ToolCall(
        name="shell.exec",
        args={"command": "dir"},
        call_id="call_shell",
    ))

    assert wire["id"] == "call_shell"
    assert wire["function"]["name"] == "shell__exec"
    assert wire["function"]["arguments"] == '{"command": "dir"}'


def test_tool_result_message_uses_tool_role_and_call_id():
    msg = _tool_result_message(
        ToolCall(name="web.search", call_id="call_web"),
        "result text",
    )

    assert msg == {
        "role": "tool",
        "tool_call_id": "call_web",
        "content": "result text",
    }


@pytest.mark.asyncio
async def test_stream_collect_preserves_structured_streamed_tool_calls():
    class FakeRouter:
        async def complete(self, messages, model, tools=None, stream=True):
            yield "checking"
            yield {
                "type": "tool_calls",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "web.search",
                        "arguments": '{"query": "cato"}',
                    },
                }],
            }

    loop = AgentLoop.__new__(AgentLoop)
    loop._router = FakeRouter()

    text, calls = await loop._stream_collect([], "test-model")

    assert text == "checking"
    assert len(calls) == 1
    assert calls[0].name == "web.search"
    assert calls[0].args == {"query": "cato"}
    assert calls[0].call_id == "call_1"
