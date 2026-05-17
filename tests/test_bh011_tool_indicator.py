"""
BH-011 regression tests for the live tool indicator + secret scrubbing.

After the first HKO audit of this session flagged that `_summarize_tool_call`
could leak secrets in the WebSocket activity broadcast, this spec locks the
scrubbing contract.  It also covers the basic shape-correctness of
`_summarize_tool_call` for every supported tool family.
"""
from __future__ import annotations

import pytest

from cato.agent_loop import (
    ToolCall,
    _summarize_tool_call,
    _scrub_secrets,
)


# ---------------------------------------------------------------------------
# Secret scrubber
# ---------------------------------------------------------------------------

def test_scrub_redacts_bearer_token_in_authorization_header():
    raw = 'curl -H "Authorization: Bearer abc123xyz789verylongtoken"'
    out = _scrub_secrets(raw)
    assert "abc123xyz789verylongtoken" not in out
    assert "[REDACTED" in out


def test_scrub_redacts_basic_auth_header():
    raw = 'curl -H "Authorization: Basic dXNlcjpwYXNz"'
    out = _scrub_secrets(raw)
    assert "dXNlcjpwYXNz" not in out


def test_scrub_redacts_x_api_key_header():
    raw = "Header: x-api-key: sk-live-secret-blob-here-1234567890"
    out = _scrub_secrets(raw)
    assert "sk-live-secret-blob-here-1234567890" not in out


def test_scrub_redacts_cli_password_flag():
    raw = "mysql --password=hunter2 -u root"
    out = _scrub_secrets(raw)
    assert "hunter2" not in out


def test_scrub_redacts_cli_token_flag():
    raw = "gh auth login --token gh_abcdef1234567890abcdef1234567890"
    out = _scrub_secrets(raw)
    assert "gh_abcdef1234567890abcdef1234567890" not in out


def test_scrub_redacts_env_var_secret_assignment():
    raw = "STRIPE_API_KEY=sk_live_abcdef1234567890 npm run deploy"
    out = _scrub_secrets(raw)
    assert "sk_live_abcdef1234567890" not in out


def test_scrub_redacts_long_opaque_blob():
    raw = "deploying abc1234567890abc1234567890abc12345 to prod"
    out = _scrub_secrets(raw)
    assert "abc1234567890abc1234567890abc12345" not in out
    assert "[REDACTED-TOKEN]" in out


def test_scrub_preserves_short_alphanumerics():
    raw = "ls -la /home/user/docs"
    out = _scrub_secrets(raw)
    assert "/home/user/docs" in out


def test_scrub_handles_empty_input():
    assert _scrub_secrets("") == ""


# ---------------------------------------------------------------------------
# Tool-call summariser shape correctness + scrubbing integration
# ---------------------------------------------------------------------------

def test_summarize_shell_command_includes_clipped_command():
    tc = ToolCall(name="shell", args={"command": "ls -la /tmp"})
    s = _summarize_tool_call(tc)
    assert s.startswith("shell(")
    assert "ls -la /tmp" in s


def test_summarize_shell_command_scrubs_bearer_token():
    tc = ToolCall(name="shell.exec", args={
        "command": 'curl -H "Authorization: Bearer secret_token_abc123xyz789more"'
    })
    s = _summarize_tool_call(tc)
    assert "secret_token_abc123xyz789more" not in s


def test_summarize_shell_command_scrubs_password_flag():
    tc = ToolCall(name="shell", args={"command": "psql --password=hunter2"})
    s = _summarize_tool_call(tc)
    assert "hunter2" not in s


def test_summarize_file_includes_action_and_path():
    tc = ToolCall(name="file", args={"action": "write", "path": "notes/today.md"})
    s = _summarize_tool_call(tc)
    assert "write" in s
    assert "notes/today.md" in s


def test_summarize_web_search_quotes_query():
    tc = ToolCall(name="web.search", args={"query": "remote ai jobs"})
    s = _summarize_tool_call(tc)
    assert "'remote ai jobs'" in s


def test_summarize_python_includes_clipped_code():
    tc = ToolCall(name="python.exec", args={"code": "print(2+2)"})
    s = _summarize_tool_call(tc)
    assert "print(2+2)" in s


def test_summarize_browser_includes_url():
    tc = ToolCall(name="browser.navigate", args={"url": "https://example.com/path"})
    s = _summarize_tool_call(tc)
    assert "https://example.com/path" in s


def test_summarize_unknown_tool_returns_bare_name():
    tc = ToolCall(name="weather.forecast", args={"city": "SF"})
    s = _summarize_tool_call(tc)
    assert s == "weather.forecast"


def test_summarize_clips_long_command():
    tc = ToolCall(name="shell", args={"command": "x" * 500})
    s = _summarize_tool_call(tc)
    # The clipped command + the tool wrapper + ellipsis should all be << 500
    assert len(s) < 120


def test_summarize_handles_non_dict_args():
    tc = ToolCall(name="shell", args="not-a-dict")  # type: ignore[arg-type]
    s = _summarize_tool_call(tc)
    # falls through to "shell(<empty>)" — defensive default
    assert s.startswith("shell(")


def test_summarize_handles_none_name():
    tc = ToolCall(name="", args={"command": "x"})
    s = _summarize_tool_call(tc)
    assert s == "tool"
