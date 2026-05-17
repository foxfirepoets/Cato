"""
cato/orchestrator/pty_session.py — PTY-backed interactive CLI sessions.

Provides PtySession for interactive terminal sessions (Claude, Codex, Gemini)
via pywinpty on Windows and ptyprocess on Unix. Used by the desktop "Interactive CLIs"
view; one-shot and coding-agent flows are unchanged (cli_invoker, cli_process_pool).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import uuid
from enum import Enum
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend: pywinpty (Windows) or ptyprocess (Unix)
# ---------------------------------------------------------------------------

_PTY_BACKEND: Optional[str] = None
_winpty_process: Any = None
_ptyprocess: Any = None


def _load_pty_backend() -> None:
    global _PTY_BACKEND, _winpty_process, _ptyprocess
    if _PTY_BACKEND is not None:
        return
    if sys.platform == "win32":
        try:
            from winpty import PtyProcess as WinPtyProcess
            _winpty_process = WinPtyProcess
            _PTY_BACKEND = "winpty"
        except ImportError:
            try:
                from pywinpty.ptyprocess import PtyProcess as WinPtyProcess
                _winpty_process = WinPtyProcess
                _PTY_BACKEND = "winpty"
            except ImportError:
                _PTY_BACKEND = "none"
    else:
        try:
            import ptyprocess
            _ptyprocess = ptyprocess
            _PTY_BACKEND = "ptyprocess"
        except ImportError:
            _PTY_BACKEND = "none"


def pty_available() -> bool:
    """Return True if PTY backend is available on this platform."""
    _load_pty_backend()
    return _PTY_BACKEND != "none"


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class PtyState(str, Enum):
    idle = "idle"
    running = "running"
    waiting_for_input = "waiting_for_input"
    dead = "dead"


# ---------------------------------------------------------------------------
# PtySession
# ---------------------------------------------------------------------------

class PtySession:
    """
    One interactive PTY session: start a process, write stdin, read stdout/stderr
    as chunks. Resize and terminate. State is tracked for UI.
    """

    def __init__(self, session_id: str, cli_name: str) -> None:
        self.session_id = session_id
        self.cli_name = cli_name
        self._state = PtyState.idle
        self._proc: Any = None
        self._queue: Optional[asyncio.Queue[bytes]] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._closed = False
        self._last_activity_at: float = 0.0

    @property
    def state(self) -> PtyState:
        return self._state

    @property
    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        if _PTY_BACKEND == "winpty" and _winpty_process is not None:
            return getattr(self._proc, "isalive", lambda: False)()
        if _PTY_BACKEND == "ptyprocess" and _ptyprocess is not None:
            return self._proc.isalive()
        return False

    @property
    def last_activity_at(self) -> float:
        return self._last_activity_at

    def _mark_activity(self) -> None:
        self._last_activity_at = time.monotonic()

    def start(
        self,
        cmd: list[str],
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        """Start the PTY-backed process. Raises if PTY backend is unavailable."""
        _load_pty_backend()
        if _PTY_BACKEND == "none":
            raise RuntimeError("PTY not available: install pywinpty on Windows or ptyprocess on Unix")

        if self._proc is not None:
            raise RuntimeError("Session already started")

        self._queue = asyncio.Queue()
        self._state = PtyState.running
        self._mark_activity()

        if _PTY_BACKEND == "winpty":
            # winpty often accepts command string; some accept list
            try:
                self._proc = _winpty_process.spawn(cmd, cwd=cwd, env=env)
            except (TypeError, Exception):
                cmd_str = " ".join(cmd)
                self._proc = _winpty_process.spawn(cmd_str, cwd=cwd, env=env)
        else:
            # ptyprocess: spawn(argv, cwd, env, dimensions=(rows, cols))
            self._proc = _ptyprocess.PtyProcess.spawn(
                cmd, cwd=cwd, env=env, dimensions=(rows, cols)
            )

        def reader() -> None:
            try:
                while not self._closed and self.is_alive:
                    try:
                        data = self._proc.read(4096)
                        if data:
                            if isinstance(data, str):
                                data = data.encode("utf-8", errors="replace")
                            try:
                                self._queue.put_nowait(data)
                            except asyncio.QueueFull:
                                pass
                        else:
                            break
                    except (EOFError, OSError, Exception):
                        break
            finally:
                try:
                    self._queue.put_nowait(b"")
                except asyncio.QueueFull:
                    pass

        self._reader_thread = threading.Thread(target=reader, daemon=True)
        self._reader_thread.start()
        self.resize(cols, rows)
        logger.info("PTY session %s started: %s", self.session_id, self.cli_name)

    def write(self, text: str) -> None:
        """Send user input to the PTY. No-op if not started or dead."""
        if self._proc is None or self._closed:
            return
        self._mark_activity()
        data = text.encode("utf-8", errors="replace")
        try:
            if _PTY_BACKEND == "winpty":
                self._proc.write(data)
            else:
                self._proc.write(data)
        except (OSError, BrokenPipeError, Exception):
            self._state = PtyState.dead

    def resize(self, cols: int, rows: int) -> None:
        """Resize the pseudo-terminal. No-op if not supported or dead."""
        if self._proc is None or self._closed:
            return
        try:
            if _PTY_BACKEND == "winpty" and hasattr(self._proc, "set_size"):
                self._proc.set_size(rows, cols)
            elif _PTY_BACKEND == "ptyprocess" and hasattr(self._proc, "setwinsize"):
                self._proc.setwinsize(rows, cols)
        except Exception:
            pass

    def terminate(self) -> None:
        """Terminate the process and stop the reader."""
        self._closed = True
        if self._queue is not None:
            try:
                self._queue.put_nowait(b"")
            except asyncio.QueueFull:
                pass
        if self._proc is None:
            self._state = PtyState.dead
            return
        try:
            if _PTY_BACKEND == "winpty":
                if hasattr(self._proc, "terminate"):
                    self._proc.terminate()
                if hasattr(self._proc, "kill") and getattr(self._proc, "isalive", lambda: False)():
                    self._proc.kill()
                if hasattr(self._proc, "close"):
                    self._proc.close()
            elif _PTY_BACKEND == "ptyprocess":
                self._proc.terminate(force=True)
        except Exception:
            pass
        self._proc = None
        self._state = PtyState.dead
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=0.5)
        logger.info("PTY session %s terminated", self.session_id)

    async def read_chunks(self) -> AsyncIterator[bytes]:
        """Async iterator of output chunks. Stops when process dies or terminate() called."""
        if self._queue is None:
            return
        while True:
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                if not chunk:
                    break
                self._mark_activity()
                yield chunk
            except asyncio.TimeoutError:
                if not self.is_alive and self._state == PtyState.dead:
                    break
                continue
            except Exception:
                break


# ---------------------------------------------------------------------------
# Per-CLI spawn helpers (reuse CLI resolution from cli_invoker)
# ---------------------------------------------------------------------------

def _resolve_cli(name: str) -> list[str]:
    """Resolve CLI command to executable args (Windows .cmd wrapped with cmd.exe /c)."""
    from cato.orchestrator.cli_invoker import _resolve_cli as resolve
    return resolve(name)


def build_pty_cmd(cli_name: str) -> list[str]:
    """
    Build command list for spawning the given CLI in a PTY.
    Claude / Codex / Gemini only; Cursor stays one-shot (no PTY).
    """
    if cli_name.lower() == "cursor":
        raise ValueError("Cursor is one-shot only; no PTY session")
    return _resolve_cli(cli_name)


# ---------------------------------------------------------------------------
# Module-level session store
# ---------------------------------------------------------------------------

_sessions: dict[str, PtySession] = {}


def create_session(cli_name: str) -> PtySession:
    """Create a new PTY session and register it. Caller must call session.start(...)."""
    session_id = str(uuid.uuid4())
    session = PtySession(session_id=session_id, cli_name=cli_name)
    _sessions[session_id] = session
    return session


def get_session(session_id: str) -> Optional[PtySession]:
    return _sessions.get(session_id)


def list_sessions() -> list[dict[str, Any]]:
    """Return list of session summaries for API."""
    return [
        {
            "session_id": s.session_id,
            "cli": s.cli_name,
            "state": s.state.value,
            "last_activity_at": s.last_activity_at,
        }
        for s in _sessions.values()
    ]


def remove_idle_sessions(idle_timeout_sec: int) -> int:
    """Terminate and remove sessions idle longer than idle_timeout_sec. Returns count removed."""
    if idle_timeout_sec <= 0:
        return 0
    now = time.monotonic()
    to_remove = [
        sid for sid, s in _sessions.items()
        if (now - s.last_activity_at) >= idle_timeout_sec
    ]
    for sid in to_remove:
        remove_session(sid)
    return len(to_remove)


def remove_session(session_id: str) -> None:
    """Terminate and remove a session from the store."""
    session = _sessions.pop(session_id, None)
    if session:
        session.terminate()
