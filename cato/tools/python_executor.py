"""
cato/tools/python_executor.py — Python Execution Sandbox (Skill 7).

Executes user-supplied Python code in an isolated subprocess with:
- Blocked dangerous patterns (filesystem destruction, network sockets, subprocess)
- Execution timeout via asyncio.wait_for
- matplotlib plt.show() auto-replacement with savefig
- Artifact directory for saved plots
"""

from __future__ import annotations

import asyncio
import re
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..platform import get_data_dir

_DATA_DIR = get_data_dir()


def _sandbox_dir() -> Path:
    """Resolve the python-executor sandbox dir, honouring ``CATO_WORKSPACE_DIR``.

    BH-010 — bridge config.yaml's `workspace_dir` here too so the python
    sandbox lives under the operator's chosen workspace instead of the
    hard-coded ``~/.cato/workspace/sandbox`` location.
    """
    custom = os.environ.get("CATO_WORKSPACE_DIR")
    if custom:
        return (Path(custom).expanduser().resolve() / "sandbox")
    return _DATA_DIR / "workspace" / "sandbox"


# Backwards-compatible module-level constant — evaluated at import time so
# existing callers that imported the symbol directly keep working.  Prefer
# `_sandbox_dir()` in new code so env-var changes take effect immediately.
SANDBOX_DIR = _sandbox_dir()

BLOCKED_PATTERNS: list[str] = [
    "os.remove",
    "shutil.rmtree",
    "subprocess.run",
    "subprocess.call",
    "socket.connect",
]


class SandboxViolationError(Exception):
    """Raised when submitted code contains blocked patterns."""


@dataclass
class ExecutionResult:
    """Result of a sandboxed code execution."""
    code: str
    stdout: str
    stderr: str
    returncode: int
    rounds_used: int
    success: bool
    artifacts: list[Path] = field(default_factory=list)


def _check_blocked_patterns(code: str) -> None:
    """Raise SandboxViolationError if code contains any blocked pattern."""
    for pattern in BLOCKED_PATTERNS:
        if pattern in code:
            raise SandboxViolationError(
                f"Blocked pattern detected: {pattern!r}. "
                "This operation is not permitted in the sandbox."
            )


def _patch_matplotlib(code: str, artifacts_dir: Path) -> str:
    """
    Replace plt.show() calls with plt.savefig(...) to capture plots as files.

    The replacement saves to a timestamped PNG in *artifacts_dir*.
    """
    if "plt.show()" not in code:
        return code

    ts = int(time.time() * 1000)
    save_path = artifacts_dir / f"plot_{ts}.png"
    # Escape Windows backslashes for the embedded path string
    save_path_str = str(save_path).replace("\\", "\\\\")
    patched = code.replace(
        "plt.show()",
        f"plt.savefig(r'{save_path_str}'); plt.close()",
    )
    return patched


class PythonExecutor:
    """
    Sandboxed Python code executor.

    Usage::

        executor = PythonExecutor()
        result = await executor.execute("print('hello')")
        print(result.stdout)  # "hello\\n"
    """

    def __init__(self, sandbox_dir: Optional[Path] = None) -> None:
        self._sandbox_dir = sandbox_dir or _sandbox_dir()
        self._sandbox_dir.mkdir(parents=True, exist_ok=True)
        self._artifacts_dir = self._sandbox_dir / "artifacts"
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)

    async def execute(
        self,
        code: str,
        timeout_sec: float = 30.0,
        max_rounds: int = 5,
    ) -> ExecutionResult:
        """
        Execute *code* in a subprocess sandbox.

        Steps:
        1. Check for blocked patterns — raise SandboxViolationError if found.
        2. Patch plt.show() → plt.savefig().
        3. Write code to a temp file in SANDBOX_DIR.
        4. Run via asyncio subprocess with timeout.
        5. Return ExecutionResult.
        """
        _check_blocked_patterns(code)

        patched_code = _patch_matplotlib(code, self._artifacts_dir)

        # Write to temp file
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            dir=self._sandbox_dir,
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(patched_code)
            tmp_path = Path(tmp.name)

        try:
            return await asyncio.wait_for(
                self._run_subprocess(patched_code, tmp_path, max_rounds),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            return ExecutionResult(
                code=code,
                stdout="",
                stderr=f"Execution timed out after {timeout_sec}s",
                returncode=-1,
                rounds_used=1,
                success=False,
            )
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _run_subprocess(
        self,
        code: str,
        tmp_path: Path,
        max_rounds: int,
    ) -> ExecutionResult:
        """Run the temp file as a subprocess and capture output."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        returncode = proc.returncode if proc.returncode is not None else -1

        # Collect any newly created artifacts
        artifacts = list(self._artifacts_dir.glob("*.png"))

        return ExecutionResult(
            code=code,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            rounds_used=1,
            success=(returncode == 0),
            artifacts=artifacts,
        )
