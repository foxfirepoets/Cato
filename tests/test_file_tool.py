"""File tool security tests."""
import json
import pytest
import asyncio
from cato.tools.file import FileTool


@pytest.mark.asyncio
async def test_path_traversal_blocked(tmp_path):
    tool = FileTool()
    # Attempt path traversal
    raw = await tool.execute({"action": "read", "path": "../../etc/passwd", "agent_id": "test"})
    result = json.loads(raw)
    assert result.get("success") is False or "error" in result


@pytest.mark.asyncio
async def test_valid_read_write(tmp_path, monkeypatch):
    # BH-010 — the file tool no longer exposes a `_WORKSPACE_ROOT` module
    # constant; it reads `CATO_WORKSPACE_DIR` at call time via the
    # `_workspace_root()` helper.  Monkeypatch the env var so the tool
    # writes into the pytest tmp_path instead of the operator's real
    # workspace.
    monkeypatch.setenv("CATO_WORKSPACE_DIR", str(tmp_path))
    tool = FileTool()
    # Write then read
    write_raw = await tool.execute({"action": "write", "path": "test.txt", "content": "hello", "agent_id": "test"})
    write_result = json.loads(write_raw)
    assert write_result.get("success") is True
    read_raw = await tool.execute({"action": "read", "path": "test.txt", "agent_id": "test"})
    read_result = json.loads(read_raw)
    assert "hello" in read_result.get("content", "")
    # Also verify the file actually landed under tmp_path/main/, proving the
    # env-var bridge worked (regression lock against the BH-010 fix being
    # undone in the future).
    written_file = tmp_path / "test" / "test.txt"
    assert written_file.exists(), f"expected file at {written_file}"
    assert written_file.read_text(encoding="utf-8") == "hello"
