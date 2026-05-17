"""
cato/tools/file.py — File read/write tool scoped to the agent's workspace.

All paths are validated against the workspace root to prevent path traversal.
Default base: ~/.cato/workspace/{agent_id}/

All operations are logged to ~/.cato/logs/file_audit.log.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..platform import get_data_dir

logger = logging.getLogger(__name__)

_CATO_DIR = get_data_dir()
_AUDIT_LOG = _CATO_DIR / "logs" / "file_audit.log"


def _workspace_root() -> Path:
    """Resolve the workspace root, honouring the ``CATO_WORKSPACE_DIR`` env
    override.

    BH-010 — `config.yaml`'s `workspace_dir` is bridged into the file tool
    through this env var (set by ``cato_svc_runner.py`` at daemon startup).
    Without it the tool silently falls back to ``~/.cato/workspace`` even
    when the operator pointed `workspace_dir` elsewhere.
    """
    custom = os.environ.get("CATO_WORKSPACE_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    return _CATO_DIR / "workspace"
_MAX_READ_BYTES = 500 * 1024   # 500 KB
_MAX_WRITE_BYTES = 1024 * 1024  # 1 MB


class FileTool:
    """Read and write files within the agent's workspace.

    All paths are validated to prevent path traversal attacks.
    Default base: ~/.cato/workspace/{agent_id}/
    """

    def __init__(self) -> None:
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def execute(self, args: dict[str, Any]) -> str:
        """Dispatch from agent_loop tool registry (receives raw args dict)."""
        action = args.get("action", "")
        path = args.get("path", "")
        content = args.get("content")
        encoding = args.get("encoding", "utf-8")
        agent_id = args.get("agent_id", "main")
        recursive = bool(args.get("recursive", False))
        root = args.get("root", "workspace")

        result = await self._run(
            action=action,
            path=path,
            content=content,
            encoding=encoding,
            agent_id=agent_id,
            recursive=recursive,
            root=root,
        )
        return json.dumps(result)

    def _trusted_roots(self, agent_id: str) -> dict[str, Path]:
        """Return the set of trusted root directories for this agent."""
        return {"workspace": _workspace_root() / agent_id}

    async def _run(
        self,
        action: str,
        path: str,
        content: Optional[str] = None,
        encoding: str = "utf-8",
        agent_id: str = "main",
        recursive: bool = False,
        root: str = "workspace",
    ) -> dict:
        """
        Perform file operations.

        Args:
            action:   "read" | "write" | "append" | "delete" | "list" | "exists" | "roots"
            path:     Relative path within root (e.g. "notes/today.md")
            content:  Content for write/append actions
            encoding: File encoding (default utf-8)
            agent_id: Agent workspace to use
            root:     Named root key from _trusted_roots (default "workspace")

        Returns:
            {"success": bool, "content": str|None, "error": str|None, "path": str}
        """
        if action == "roots":
            roots = self._trusted_roots(agent_id)
            return {"success": True, "content": json.dumps({k: str(v) for k, v in roots.items()}), "error": None, "path": ""}

        roots = self._trusted_roots(agent_id)
        base = roots.get(root)
        if base is None:
            return {"success": False, "content": None, "error": f"Unknown root: {root!r}. Valid: {list(roots)}", "path": path}

        base.mkdir(parents=True, exist_ok=True)

        try:
            safe = self._resolve_safe_path(base, path)
        except ValueError as exc:
            self._audit(action, path, agent_id, success=False)
            return {"success": False, "content": None, "error": str(exc), "path": path}

        dispatch = {
            "read":   self._read,
            "write":  self._write,
            "append": self._append,
            "delete": self._delete,
            "list":   self._list,
            "exists": self._exists,
        }

        if action not in dispatch:
            return {
                "success": False,
                "content": None,
                "error": f"Unknown action: {action!r}. Valid: {list(dispatch) + ['roots']}",
                "path": str(safe),
            }

        result = dispatch[action](safe, content=content, encoding=encoding, recursive=recursive)
        self._audit(action, path, agent_id, success=result.get("success", False))
        return result

    # ------------------------------------------------------------------
    # Action implementations
    # ------------------------------------------------------------------

    def _read(self, safe: Path, *, content=None, encoding="utf-8", recursive=False) -> dict:
        if not safe.exists():
            return {"success": False, "content": None, "error": "File not found", "path": str(safe)}
        if not safe.is_file():
            return {"success": False, "content": None, "error": "Path is not a file", "path": str(safe)}

        size = safe.stat().st_size
        truncated = size > _MAX_READ_BYTES
        try:
            raw = safe.read_bytes()[:_MAX_READ_BYTES]
            text = raw.decode(encoding, errors="replace")
            if truncated:
                text += f"\n[... truncated — file is {size} bytes, read limit is {_MAX_READ_BYTES} bytes ...]"
            return {"success": True, "content": text, "error": None, "path": str(safe), "truncated": truncated}
        except OSError as exc:
            return {"success": False, "content": None, "error": str(exc), "path": str(safe)}

    def _write(self, safe: Path, *, content=None, encoding="utf-8", recursive=False) -> dict:
        if content is None:
            return {"success": False, "content": None, "error": "content is required for write", "path": str(safe)}

        encoded = content.encode(encoding, errors="replace")
        if len(encoded) > _MAX_WRITE_BYTES:
            return {
                "success": False, "content": None,
                "error": f"Content exceeds {_MAX_WRITE_BYTES} byte write limit",
                "path": str(safe),
            }

        safe.parent.mkdir(parents=True, exist_ok=True)
        tmp = safe.with_suffix(safe.suffix + ".tmp")
        try:
            tmp.write_bytes(encoded)
            tmp.replace(safe)
            return {"success": True, "content": None, "error": None, "path": str(safe)}
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            return {"success": False, "content": None, "error": str(exc), "path": str(safe)}

    def _append(self, safe: Path, *, content=None, encoding="utf-8", recursive=False) -> dict:
        if content is None:
            return {"success": False, "content": None, "error": "content is required for append", "path": str(safe)}

        safe.parent.mkdir(parents=True, exist_ok=True)
        try:
            with safe.open("a", encoding=encoding) as fh:
                fh.write(content)
            return {"success": True, "content": None, "error": None, "path": str(safe)}
        except OSError as exc:
            return {"success": False, "content": None, "error": str(exc), "path": str(safe)}

    def _delete(self, safe: Path, *, content=None, encoding="utf-8", recursive=False) -> dict:
        try:
            if safe.is_file():
                safe.unlink()
            elif safe.is_dir():
                import shutil
                shutil.rmtree(safe)
            else:
                return {"success": False, "content": None, "error": "Path does not exist", "path": str(safe)}
            return {"success": True, "content": None, "error": None, "path": str(safe)}
        except OSError as exc:
            return {"success": False, "content": None, "error": str(exc), "path": str(safe)}

    def _list(self, safe: Path, *, content=None, encoding="utf-8", recursive=False) -> dict:
        if not safe.exists():
            return {"success": False, "content": None, "error": "Path does not exist", "path": str(safe)}
        if not safe.is_dir():
            return {"success": False, "content": None, "error": "Path is not a directory", "path": str(safe)}

        try:
            if recursive:
                entries = [str(p.relative_to(safe)) for p in sorted(safe.rglob("*"))]
            else:
                entries = [p.name for p in sorted(safe.iterdir())]
            return {"success": True, "content": json.dumps(entries), "error": None, "path": str(safe)}
        except OSError as exc:
            return {"success": False, "content": None, "error": str(exc), "path": str(safe)}

    def _exists(self, safe: Path, *, content=None, encoding="utf-8", recursive=False) -> dict:
        return {
            "success": True,
            "content": json.dumps({"exists": safe.exists(), "is_file": safe.is_file(), "is_dir": safe.is_dir()}),
            "error": None,
            "path": str(safe),
        }

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_safe_path(workspace: Path, rel_path: str) -> Path:
        """Resolve rel_path within workspace, raising ValueError on traversal."""
        if not rel_path:
            return workspace
        candidate = (workspace / rel_path).resolve()
        try:
            candidate.relative_to(workspace.resolve())
        except ValueError:
            raise ValueError(
                f"Path traversal detected: {rel_path!r} escapes workspace {workspace}"
            )
        return candidate

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    def _audit(self, action: str, path: str, agent_id: str, success: bool) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        status = "OK" if success else "FAIL"
        line = f"{ts} | agent={agent_id} | action={action} | status={status} | path={path!r}\n"
        try:
            with _AUDIT_LOG.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            logger.warning("Could not write file audit log: %s", _AUDIT_LOG)
