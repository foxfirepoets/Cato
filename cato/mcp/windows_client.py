"""
cato/mcp/windows_client.py — Windows MCP client for Cato.

Wraps the CursorTouch/Windows-MCP server (https://github.com/CursorTouch/Windows-MCP)
via the MCP stdio transport.  Cato can use this to control other applications running
on the Windows VPS — opening Claude, Codex, Gemini, or Cursor windows, typing into
them, reading their output via Snapshot, etc.

The server is launched on demand via ``uvx windows-mcp`` and communicates over
stdin/stdout (no network port required).  Each ``WindowsMCPClient`` instance
manages exactly one server subprocess lifetime.

Usage
-----
    client = WindowsMCPClient()
    async with client:
        snapshot = await client.snapshot()
        await client.powershell("notepad.exe")
        await client.click(loc=[100, 200])

Available tools (17 total, see WindowsMCPClient docstring for full reference):
  snapshot, click, type_text, scroll, move, shortcut, wait,
  powershell, filesystem, app, scrape, clipboard, process,
  notification, registry, multiselect, multiedit
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


def _mcp_imports() -> tuple[Any, Any, Any]:
    """Lazy-load real MCP SDK symbols.

    The local mcp/ directory at the project root shadows the installed MCP SDK.
    Tests that mock the client don't need the real SDK — they patch _stdio_client
    and _ClientSession on the instance after construction.  Return lightweight
    stubs so __init__ never fails at import time; the stubs get replaced by mocks
    in tests and by the real SDK when actually running the daemon.
    """
    import site as _site
    import importlib.util as _ilu
    import sys as _sys

    # Try to find and load the real SDK from site-packages, bypassing the shadow.
    sdk_dir: str | None = None
    candidates: list[str] = []
    try:
        candidates += _site.getsitepackages()
    except AttributeError:
        pass
    try:
        candidates.append(_site.getusersitepackages())
    except AttributeError:
        pass

    from pathlib import Path as _P
    for sp in candidates:
        p = _P(sp) / "mcp" / "__init__.py"
        if p.exists():
            sdk_dir = str(_P(sp) / "mcp")
            break

    if sdk_dir:
        _SDK_KEY = "_cato_mcp_sdk"
        if _SDK_KEY not in _sys.modules:
            # Register bare sdk module so its relative imports (mcp.types, etc.) work
            # by pre-populating sys.modules with the real SDK path before exec.
            import os as _os
            _sdk_p = _P(sdk_dir)
            for _root, _dirs, _files in _os.walk(sdk_dir):
                for _f in _files:
                    if not _f.endswith(".py"):
                        continue
                    _abs = _os.path.join(_root, _f)
                    _rel = _os.path.relpath(_abs, sdk_dir).replace("\\", "/")
                    _parts = _rel[:-3].split("/")  # strip .py
                    if _parts[-1] == "__init__":
                        _parts = _parts[:-1]
                    _mod_name = "mcp" + ("." + ".".join(_parts) if _parts else "")
                    if _mod_name not in _sys.modules:
                        _spec2 = _ilu.spec_from_file_location(
                            _mod_name, _abs,
                            submodule_search_locations=(
                                [_os.path.join(_root)] if _f == "__init__.py" else None
                            ),
                        )
                        if _spec2:
                            _sys.modules[_mod_name] = _ilu.module_from_spec(_spec2)

            # Exec the top-level init to populate ClientSession etc.
            _top_spec = _ilu.spec_from_file_location(
                "mcp", str(_sdk_p / "__init__.py"),
                submodule_search_locations=[sdk_dir],
            )
            if _top_spec and _top_spec.loader:
                try:
                    _top_spec.loader.exec_module(_sys.modules["mcp"])  # type: ignore[union-attr]
                    _sys.modules[_SDK_KEY] = _sys.modules["mcp"]
                except Exception:
                    pass

        _sdk = _sys.modules.get(_SDK_KEY)
        if _sdk:
            _ClientSession = getattr(_sdk, "ClientSession", None)
            _StdioServerParameters = getattr(_sdk, "StdioServerParameters", None)
            _stdio_mod = _sys.modules.get("mcp.client.stdio")
            _stdio_client = getattr(_stdio_mod, "stdio_client", None) if _stdio_mod else None
            if _ClientSession and _StdioServerParameters and _stdio_client:
                return _ClientSession, _StdioServerParameters, _stdio_client

    # Fallback stubs — tests replace these via monkeypatch/mock
    class _StubSession:
        pass

    class _StubParams:
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    async def _stub_stdio(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("MCP SDK not available — install 'mcp' package")

    return _StubSession, _StubParams, _stub_stdio  # type: ignore[return-value]


# Module-level names that tests patch via patch("cato.mcp.windows_client.stdio_client").
# Populated lazily on first WindowsMCPClient instantiation.
ClientSession: Any = None
StdioServerParameters: Any = None
stdio_client: Any = None
_mcp_loaded = False


def _ensure_mcp_loaded() -> None:
    """Populate module-level MCP names if they have not been set yet."""
    global ClientSession, StdioServerParameters, stdio_client, _mcp_loaded
    if not _mcp_loaded:
        _cs, _sp, _sc = _mcp_imports()
        # Only overwrite if not already set by a test mock or earlier call
        if ClientSession is None:
            ClientSession = _cs
        if StdioServerParameters is None:
            StdioServerParameters = _sp
        if stdio_client is None:
            stdio_client = _sc
        _mcp_loaded = True


# Default command to launch the Windows MCP server.
# ``uvx`` resolves and runs the package without a permanent install.
_DEFAULT_COMMAND = "uvx"
_DEFAULT_ARGS = ["windows-mcp"]


class WindowsMCPError(RuntimeError):
    """Raised when a Windows MCP tool call fails or the server is unreachable."""


class WindowsMCPClient:
    """
    Async client for the CursorTouch Windows MCP server.

    Lifecycle
    ---------
    Use as an async context manager to ensure the subprocess is started and
    torn down cleanly::

        async with WindowsMCPClient() as client:
            result = await client.snapshot()

    Or manage manually::

        client = WindowsMCPClient()
        await client.start()
        try:
            result = await client.snapshot()
        finally:
            await client.stop()

    Tool reference (17 tools)
    --------------------------
    snapshot(use_vision, use_dom, use_annotation, use_ui_tree, display)
        Capture desktop state — UI element tree and optional screenshot.
        Returns {"content": [...], "isError": False}

    click(loc, label, button, clicks)
        Mouse click at [x, y] coordinates or UI element label from snapshot.

    type_text(text, loc, label, clear, caret_position, press_enter)
        Type text into a field.

    scroll(loc, label, type, direction, wheel_times)
        Scroll at coordinates or element.

    move(loc, label, drag)
        Move mouse cursor or drag to target.

    shortcut(shortcut)
        Execute keyboard shortcut, e.g. "ctrl+c", "win+r".

    wait(duration)
        Sleep for ``duration`` seconds.

    powershell(command, timeout)
        Execute a PowerShell command. Returns stdout + exit code.

    filesystem(mode, path, destination, content, pattern, ...)
        File operations: read/write/copy/move/delete/list/search/info.

    app(mode, name, window_loc, window_size)
        Launch, resize, or switch to an application window.

    scrape(url, query, use_dom, use_sampling)
        Fetch and optionally summarize a web page.

    clipboard(mode, text)
        Get or set Windows clipboard content.

    process(mode, name, pid, sort_by, limit, force)
        List or kill running processes.

    notification(title, message)
        Send a Windows toast notification.

    registry(mode, path, name, value, type)
        Read/write/delete/list Windows Registry values.

    multiselect(locs, labels, press_ctrl)
        Select multiple items simultaneously.

    multiedit(locs, labels)
        Type into multiple fields in one call.
    """

    def __init__(
        self,
        command: str = _DEFAULT_COMMAND,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """
        Args:
            command: Executable to launch the MCP server (default: ``uvx``).
            args: Arguments passed to *command* (default: ``["windows-mcp"]``).
            env: Extra environment variables for the server process.  Pass
                 ``{"MODE": "remote", "SANDBOX_ID": "...", "API_KEY": "..."}``
                 to proxy through the cloud windowsmcp.io VMs.
        """
        _ensure_mcp_loaded()
        import cato.mcp.windows_client as _self_mod
        self._server_params = _self_mod.StdioServerParameters(
            command=command,
            args=args if args is not None else list(_DEFAULT_ARGS),
            env=env,
        )
        self._session: Optional[Any] = None
        self._cm_stack: list[Any] = []   # context managers to exit on stop()
        self._errlog: Any = None          # file handle for server stderr (Windows)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Windows MCP server subprocess and open an MCP session."""
        if self._session is not None:
            return

        if sys.platform == "win32":
            self._errlog = open(  # noqa: WPS515
                "cato_windows_mcp.log", "a", encoding="utf-8", errors="replace"
            )
            errlog = self._errlog
        else:
            errlog = sys.stderr

        # stdio_client and ClientSession are entered manually so stop() can exit
        # them cleanly.  On any failure we call stop() to drain _cm_stack.
        try:
            import cato.mcp.windows_client as _self_mod
            transport_cm = _self_mod.stdio_client(self._server_params, errlog=errlog)
            read_stream, write_stream = await transport_cm.__aenter__()
            self._cm_stack.append(transport_cm)

            session_cm = _self_mod.ClientSession(read_stream, write_stream)
            session = await session_cm.__aenter__()
            self._cm_stack.append(session_cm)

            await session.initialize()
        except Exception:
            await self.stop()
            raise

        self._session = session
        logger.info("Windows MCP client connected (command=%r)", self._server_params.command)

    async def stop(self) -> None:
        """Close the MCP session and terminate the server subprocess."""
        self._session = None
        # Exit context managers in reverse order (session first, then transport)
        for cm in reversed(self._cm_stack):
            try:
                await cm.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing Windows MCP context manager: %s", exc)
        self._cm_stack.clear()
        # Close the Windows errlog file handle if we opened one
        if self._errlog is not None and self._errlog is not sys.stderr:
            try:
                self._errlog.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing errlog: %s", exc)
            self._errlog = None
        logger.info("Windows MCP client disconnected")

    async def __aenter__(self) -> "WindowsMCPClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    async def _call(self, tool: str, **kwargs: Any) -> Any:
        """Call a Windows MCP tool and return the raw result content.

        Raises:
            WindowsMCPError: If the client is not started or the tool returns
                             an error response.
        """
        if self._session is None:
            raise WindowsMCPError("WindowsMCPClient is not started — call start() or use as async context manager")

        # Strip None values — the server infers defaults for missing params
        params = {k: v for k, v in kwargs.items() if v is not None}

        try:
            result = await self._session.call_tool(tool, params)
        except Exception as exc:
            raise WindowsMCPError(f"Windows MCP tool {tool!r} failed: {exc}") from exc

        if result.isError:
            err_text = " ".join(
                getattr(c, "text", str(c)) for c in result.content
            )
            raise WindowsMCPError(f"Windows MCP tool {tool!r} returned error: {err_text}")

        # Return content list; callers unpack as needed
        return result.content

    # ------------------------------------------------------------------
    # Desktop control tools
    # ------------------------------------------------------------------

    async def snapshot(
        self,
        *,
        use_vision: bool = False,
        use_dom: bool = False,
        use_annotation: bool = True,
        use_ui_tree: bool = True,
        display: Optional[list[int]] = None,
    ) -> list[Any]:
        """Capture the desktop UI element tree and optionally a screenshot.

        Args:
            use_vision: Include a screenshot PNG in the response.
            use_dom: Extract browser DOM instead of OS UI tree.
            use_annotation: Draw coloured bounding boxes on screenshot.
            use_ui_tree: Extract interactive element list (set False for screenshot-only).
            display: List of display indices, e.g. [0]. None = full virtual desktop.

        Returns:
            List of content items — always has a text item; has an Image item
            when ``use_vision=True``.
        """
        return await self._call(
            "Snapshot",
            use_vision=use_vision,
            use_dom=use_dom,
            use_annotation=use_annotation,
            use_ui_tree=use_ui_tree,
            display=display,
        )

    async def click(
        self,
        *,
        loc: Optional[list[int]] = None,
        label: Optional[int] = None,
        button: str = "left",
        clicks: int = 1,
    ) -> list[Any]:
        """Click at ``[x, y]`` coordinates or a UI element ``label`` from snapshot.

        Exactly one of ``loc`` or ``label`` must be provided.
        ``clicks=0`` = hover only, ``clicks=2`` = double-click.
        """
        if loc is None and label is None:
            raise ValueError("click() requires either loc=[x, y] or label=<element_id>")
        return await self._call("Click", loc=loc, label=label, button=button, clicks=clicks)

    async def type_text(
        self,
        text: str,
        *,
        loc: Optional[list[int]] = None,
        label: Optional[int] = None,
        clear: bool = False,
        caret_position: str = "idle",
        press_enter: bool = False,
    ) -> list[Any]:
        """Type ``text`` into the field at ``loc`` or ``label``."""
        if loc is None and label is None:
            raise ValueError("type_text() requires either loc=[x, y] or label=<element_id>")
        return await self._call(
            "Type",
            text=text,
            loc=loc,
            label=label,
            clear=clear,
            caret_position=caret_position,
            press_enter=press_enter,
        )

    async def scroll(
        self,
        *,
        loc: Optional[list[int]] = None,
        label: Optional[int] = None,
        direction: str = "down",
        scroll_type: str = "vertical",
        wheel_times: int = 1,
    ) -> list[Any]:
        """Scroll at coordinates or element. ``scroll_type`` = 'vertical' | 'horizontal'."""
        return await self._call(
            "Scroll",
            loc=loc,
            label=label,
            type=scroll_type,
            direction=direction,
            wheel_times=wheel_times,
        )

    async def move(
        self,
        *,
        loc: Optional[list[int]] = None,
        label: Optional[int] = None,
        drag: bool = False,
    ) -> list[Any]:
        """Move cursor to ``loc`` / ``label``. Set ``drag=True`` for drag-and-drop."""
        if loc is None and label is None:
            raise ValueError("move() requires either loc=[x, y] or label=<element_id>")
        return await self._call("Move", loc=loc, label=label, drag=drag)

    async def shortcut(self, shortcut: str) -> list[Any]:
        """Execute a keyboard shortcut, e.g. ``"ctrl+c"``, ``"win+r"``."""
        return await self._call("Shortcut", shortcut=shortcut)

    async def wait(self, duration: int) -> list[Any]:
        """Ask the Windows MCP server to sleep for ``duration`` seconds."""
        return await self._call("Wait", duration=duration)

    # ------------------------------------------------------------------
    # System tools
    # ------------------------------------------------------------------

    async def powershell(self, command: str, *, timeout: int = 30) -> list[Any]:
        """Execute a PowerShell command.

        Returns content with ``"Response: <output>\\nStatus Code: <int>"``.
        """
        return await self._call("PowerShell", command=command, timeout=timeout)

    async def filesystem(
        self,
        mode: str,
        path: str,
        *,
        destination: Optional[str] = None,
        content: Optional[str] = None,
        pattern: Optional[str] = None,
        recursive: bool = False,
        append: bool = False,
        overwrite: bool = False,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        encoding: str = "utf-8",
        show_hidden: bool = False,
    ) -> list[Any]:
        """File system operations.

        ``mode`` must be one of: read, write, copy, move, delete, list, search, info.
        Relative ``path`` values resolve to the user's Desktop.
        """
        return await self._call(
            "FileSystem",
            mode=mode,
            path=path,
            destination=destination,
            content=content,
            pattern=pattern,
            recursive=recursive,
            append=append,
            overwrite=overwrite,
            offset=offset,
            limit=limit,
            encoding=encoding,
            show_hidden=show_hidden,
        )

    async def app(
        self,
        *,
        mode: str = "launch",
        name: Optional[str] = None,
        window_loc: Optional[list[int]] = None,
        window_size: Optional[list[int]] = None,
    ) -> list[Any]:
        """Launch, resize, or switch to a window.

        ``mode``: 'launch' | 'resize' | 'switch'
        ``name``: app name or window title substring.
        ``window_loc``: [x, y] for resize.
        ``window_size``: [width, height] for resize.
        """
        return await self._call(
            "App",
            mode=mode,
            name=name,
            window_loc=window_loc,
            window_size=window_size,
        )

    async def scrape(
        self,
        url: str,
        *,
        query: Optional[str] = None,
        use_dom: bool = False,
        use_sampling: bool = True,
    ) -> list[Any]:
        """Fetch and optionally summarize a web page.

        Args:
            url: URL to fetch.
            query: Focus hint for LLM summarization.
            use_dom: Use active browser tab DOM instead of HTTP fetch.
            use_sampling: Run LLM summarization; False returns raw content.
        """
        return await self._call(
            "Scrape",
            url=url,
            query=query,
            use_dom=use_dom,
            use_sampling=use_sampling,
        )

    async def clipboard(self, mode: str, *, text: Optional[str] = None) -> list[Any]:
        """Read or write the Windows clipboard.

        ``mode``: 'get' | 'set'.  ``text`` is required when ``mode='set'``.
        """
        if mode == "set" and text is None:
            raise ValueError("clipboard(mode='set') requires text=...")
        return await self._call("Clipboard", mode=mode, text=text)

    async def process(
        self,
        mode: str,
        *,
        name: Optional[str] = None,
        pid: Optional[int] = None,
        sort_by: str = "memory",
        limit: int = 20,
        force: bool = False,
    ) -> list[Any]:
        """List or kill running processes.

        ``mode``: 'list' | 'kill'.
        """
        return await self._call(
            "Process",
            mode=mode,
            name=name,
            pid=pid,
            sort_by=sort_by,
            limit=limit,
            force=force,
        )

    async def notification(self, title: str, message: str) -> list[Any]:
        """Send a Windows toast notification."""
        return await self._call("Notification", title=title, message=message)

    async def registry(
        self,
        mode: str,
        path: str,
        *,
        name: Optional[str] = None,
        value: Optional[str] = None,
        reg_type: str = "String",
    ) -> list[Any]:
        """Read/write/delete/list Windows Registry values.

        ``mode``: 'get' | 'set' | 'delete' | 'list'.
        ``path``: PowerShell-style, e.g. ``"HKCU:\\\\Software\\\\MyApp"``.
        ``reg_type``: String | DWord | QWord | Binary | MultiString | ExpandString.
        """
        return await self._call(
            "Registry",
            mode=mode,
            path=path,
            name=name,
            value=value,
            type=reg_type,
        )

    async def multiselect(
        self,
        *,
        locs: Optional[list[list[int]]] = None,
        labels: Optional[list[int]] = None,
        press_ctrl: bool = True,
    ) -> list[Any]:
        """Select multiple items (files, checkboxes) simultaneously."""
        if locs is None and labels is None:
            raise ValueError("multiselect() requires either locs or labels")
        return await self._call("MultiSelect", locs=locs, labels=labels, press_ctrl=press_ctrl)

    async def multiedit(
        self,
        *,
        locs: Optional[list[list]] = None,
        labels: Optional[list[list]] = None,
    ) -> list[Any]:
        """Type into multiple input fields in one call.

        ``locs``: list of [x, y, text].
        ``labels``: list of [label_id, text].
        """
        if locs is None and labels is None:
            raise ValueError("multiedit() requires either locs or labels")
        return await self._call("MultiEdit", locs=locs, labels=labels)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[str]:
        """Return the names of all tools the server exposes."""
        if self._session is None:
            raise WindowsMCPError("Not started")
        result = await self._session.list_tools()
        return [t.name for t in result.tools]

    async def open_app_and_type(
        self,
        app_name: str,
        text: str,
        *,
        wait_sec: int = 2,
        press_enter: bool = False,
    ) -> None:
        """High-level: launch ``app_name``, wait, click to focus, then type ``text``.

        Takes a snapshot after launching to update the server's desktop_state
        cache, then clicks the centre of the screen to ensure focus before
        typing.  Pass ``wait_sec`` to give slow-starting apps more time.
        """
        await self.app(mode="launch", name=app_name)
        await asyncio.sleep(wait_sec)
        # Refresh server's desktop_state (required before label-based targeting)
        await self.snapshot(use_vision=False)
        # Click the app window by switching to it, then type into focused field
        await self.app(mode="switch", name=app_name)
        await self.type_text(text, loc=[960, 540], press_enter=press_enter)
