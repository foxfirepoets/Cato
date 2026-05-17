"""
cato/tools/__init__.py — Register all built-in tools with the agent loop.

Call register_all_tools(agent_loop) once at startup to wire every tool
handler into the loop's _TOOL_REGISTRY.
"""

from .browser import BrowserTool
from .file import FileTool
from .genesis import GENESIS_TOOL_SCHEMA, GenesisTool
from .memory import MemoryTool
from .shell import ShellTool

__all__ = ["ShellTool", "FileTool", "BrowserTool", "MemoryTool", "GenesisTool"]


def register_all_tools(agent_loop) -> None:
    """Register all tools with the module-level tool registry in agent_loop."""
    from ..agent_loop import register_tool
    register_tool("shell", ShellTool().execute)
    register_tool("file", FileTool().execute)
    register_tool("memory", MemoryTool().execute)
    register_tool("genesis", GenesisTool().execute, GENESIS_TOOL_SCHEMA)

    # Use Conduit browser engine if enabled in config, otherwise plain browser
    try:
        conduit_enabled = getattr(agent_loop._cfg, "conduit_enabled", False)
    except Exception:
        conduit_enabled = False

    if conduit_enabled:
        from .conduit_bridge import ConduitBrowserTool
        register_tool("browser", ConduitBrowserTool(agent_loop._cfg, agent_loop._budget).execute)
    else:
        register_tool("browser", BrowserTool().execute)
