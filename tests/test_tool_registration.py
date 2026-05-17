"""
tests/test_tool_registration.py — Verify all tool registration functions work
without error and populate the global registry.

Covers:
  1. _register_file_tools populates registry with 'file' key
  2. _register_browser_tools populates registry with 'browser' key
  3. _register_github_tools populates registry with github.* keys
  4. _register_conduit_tools populates registry with conduit.* keys
  5. _register_shell_tools populates registry with 'shell.exec' key
  6. _register_web_search_tools populates registry with web.* keys
  7. _register_python_executor_tools populates registry with 'python.execute' key
  8. _register_clawflow_tools populates registry with 'flow.run' key
  9. All registered tools have callable handlers
  10. Builtin schemas exist for file, browser, shell
  11. GitHub schemas are well-formed OpenAI function-calling format
  12. get_tool_definitions returns schemas for all registered tools
  13. FileTool.execute dispatches read/write/list correctly
  14. register_all_tools registers everything in one call
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1-4: Registration functions populate the registry
# ---------------------------------------------------------------------------

def test_register_file_tools():
    """_register_file_tools should add 'file' to _TOOL_REGISTRY."""
    from cato.agent_loop import _register_file_tools, _TOOL_REGISTRY
    _register_file_tools()
    assert "file" in _TOOL_REGISTRY
    assert callable(_TOOL_REGISTRY["file"])


def test_register_browser_tools():
    """_register_browser_tools should add 'browser' to _TOOL_REGISTRY."""
    from cato.agent_loop import _register_browser_tools, _TOOL_REGISTRY
    _register_browser_tools()
    assert "browser" in _TOOL_REGISTRY
    assert callable(_TOOL_REGISTRY["browser"])


def test_register_github_tools():
    """_register_github_tools should add github.* to _TOOL_REGISTRY."""
    from cato.agent_loop import _register_github_tools, _TOOL_REGISTRY
    _register_github_tools(vault=None)
    for key in ["github.pr_review", "github.issue_create", "github.issue_list", "github.pr_list"]:
        assert key in _TOOL_REGISTRY, f"{key} not registered"
        assert callable(_TOOL_REGISTRY[key])


def test_register_conduit_tools():
    """_register_conduit_tools should add conduit.* to _TOOL_REGISTRY."""
    from cato.agent_loop import _register_conduit_tools, _TOOL_REGISTRY
    _register_conduit_tools()
    for key in ["conduit.crawl", "conduit.monitor"]:
        assert key in _TOOL_REGISTRY, f"{key} not registered"
        assert callable(_TOOL_REGISTRY[key])


# ---------------------------------------------------------------------------
# 5-8: Pre-existing registration functions still work
# ---------------------------------------------------------------------------

def test_register_shell_tools():
    """_register_shell_tools should add 'shell.exec' to _TOOL_REGISTRY."""
    from cato.agent_loop import _register_shell_tools, _TOOL_REGISTRY
    _register_shell_tools()
    assert "shell.exec" in _TOOL_REGISTRY


def test_register_web_search_tools():
    """_register_web_search_tools should add web.* to _TOOL_REGISTRY."""
    from cato.agent_loop import _register_web_search_tools, _TOOL_REGISTRY
    _register_web_search_tools()
    assert "web.search" in _TOOL_REGISTRY


def test_register_python_executor_tools():
    """_register_python_executor_tools should add 'python.execute' to _TOOL_REGISTRY."""
    from cato.agent_loop import _register_python_executor_tools, _TOOL_REGISTRY
    _register_python_executor_tools()
    assert "python.execute" in _TOOL_REGISTRY


def test_register_clawflow_tools():
    """_register_clawflow_tools should add 'flow.run' to _TOOL_REGISTRY."""
    from cato.agent_loop import _register_clawflow_tools, _TOOL_REGISTRY
    _register_clawflow_tools()
    assert "flow.run" in _TOOL_REGISTRY


# ---------------------------------------------------------------------------
# 9: All handlers are callable
# ---------------------------------------------------------------------------

def test_all_handlers_callable():
    """Every registered tool handler should be callable."""
    from cato.agent_loop import _TOOL_REGISTRY
    for name, handler in _TOOL_REGISTRY.items():
        assert callable(handler), f"Tool '{name}' handler is not callable"


# ---------------------------------------------------------------------------
# 10: Builtin schemas exist
# ---------------------------------------------------------------------------

def test_builtin_schemas_exist():
    """_BUILTIN_SCHEMAS should contain schemas for file, browser, shell."""
    from cato.agent_loop import _BUILTIN_SCHEMAS
    for key in ["file", "browser", "shell"]:
        assert key in _BUILTIN_SCHEMAS, f"Missing builtin schema for '{key}'"
        schema = _BUILTIN_SCHEMAS[key]
        assert schema["type"] == "function"
        assert "function" in schema
        assert "name" in schema["function"]
        assert "parameters" in schema["function"]


# ---------------------------------------------------------------------------
# 11: GitHub schemas are well-formed
# ---------------------------------------------------------------------------

def test_github_schemas_well_formed():
    """GitHub tool schemas should be valid OpenAI function-calling format."""
    from cato.agent_loop import _GITHUB_SCHEMAS
    for key, schema in _GITHUB_SCHEMAS.items():
        assert schema["type"] == "function", f"{key} missing type=function"
        func = schema["function"]
        assert "name" in func, f"{key} missing function.name"
        assert "parameters" in func, f"{key} missing function.parameters"
        assert func["parameters"]["type"] == "object", f"{key} params not object type"


# ---------------------------------------------------------------------------
# 12: get_tool_definitions returns all registered tools
# ---------------------------------------------------------------------------

def test_get_tool_definitions_includes_new_tools():
    """get_tool_definitions should return definitions for file, browser, github.*, conduit.*."""
    from cato.agent_loop import get_tool_definitions, _TOOL_REGISTRY

    # Ensure tools are registered first
    from cato.agent_loop import (
        _register_file_tools, _register_browser_tools,
        _register_github_tools, _register_conduit_tools,
    )
    _register_file_tools()
    _register_browser_tools()
    _register_github_tools()
    _register_conduit_tools()

    defs = get_tool_definitions()
    names = {d["function"]["name"] for d in defs}

    # file and browser use builtin schemas (name matches key)
    assert "file" in names, f"'file' not in tool definitions. Got: {names}"
    assert "browser" in names, f"'browser' not in tool definitions. Got: {names}"

    # GitHub tools use sanitized names (dots → underscores)
    # The schema names use __ separator
    assert "github__pr_review" in names, f"'github__pr_review' not in tool definitions. Got: {names}"
    assert "github__issue_create" in names, f"'github__issue_create' not in tool definitions. Got: {names}"


# ---------------------------------------------------------------------------
# 13: FileTool.execute dispatches correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_tool_read_write_list(tmp_path):
    """FileTool should read, write, and list files within workspace."""
    from cato.tools.file import FileTool

    tool = FileTool()

    # Write a file
    result_raw = await tool.execute({
        "action": "write",
        "path": "test.txt",
        "content": "hello world",
        "agent_id": "test",
        "root": "workspace",
    })
    result = json.loads(result_raw)
    assert result["success"] is True

    # Read it back
    result_raw = await tool.execute({
        "action": "read",
        "path": "test.txt",
        "agent_id": "test",
        "root": "workspace",
    })
    result = json.loads(result_raw)
    assert result["success"] is True
    assert result["content"] == "hello world"

    # List workspace
    result_raw = await tool.execute({
        "action": "list",
        "path": "",
        "agent_id": "test",
        "root": "workspace",
    })
    result = json.loads(result_raw)
    assert result["success"] is True
    entries = json.loads(result["content"])
    assert "test.txt" in entries


# ---------------------------------------------------------------------------
# 14: register_all_tools registers everything
# ---------------------------------------------------------------------------

def test_register_all_tools():
    """register_all_tools should populate the registry with all tool families."""
    from cato.agent_loop import register_all_tools, _TOOL_REGISTRY

    register_all_tools(lambda name, fn: None)

    expected_keys = ["file", "browser", "shell.exec", "web.search", "python.execute", "flow.run"]
    for key in expected_keys:
        assert key in _TOOL_REGISTRY, f"'{key}' missing after register_all_tools"
