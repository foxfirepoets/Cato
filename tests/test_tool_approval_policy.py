"""
tests/test_tool_approval_policy.py — Tool approval policy.

Verifies that reversible tools (memory.*, web.*, academic.*, github reads,
file reads) auto-approve without per-call user confirmation, while
irreversible tools (shell.exec, python.execute, file writes, github
writes, email send, payments) continue to require explicit approval.

Also verifies that:
- `strict_approval=True` (or CATO_STRICT_APPROVAL=true) restores the
  original "prompt for everything" behaviour.
- Auto-approvals are logged at INFO level.
- Custom whitelists from config flow through.
- `CatoConfig` ships a sane default `auto_approved_tools` list that
  includes memory.search but NOT shell.exec.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from cato.auth.token_checker import AuthResult, TokenChecker
from cato.auth.token_store import TokenStore
from cato.config import CatoConfig


def _store(tmp_path: Path) -> TokenStore:
    return TokenStore(db_path=tmp_path / "tokens.db")


# ---------------------------------------------------------------------------
# CatoConfig defaults
# ---------------------------------------------------------------------------

class TestCatoConfigDefaults:
    def test_auto_approved_tools_includes_memory(self) -> None:
        cfg = CatoConfig()
        assert "memory.search" in cfg.auto_approved_tools
        assert "memory.federated" in cfg.auto_approved_tools

    def test_auto_approved_tools_includes_reversible_reads(self) -> None:
        cfg = CatoConfig()
        for safe in (
            "web.search",
            "academic.arxiv",
            "graph.query",
            "github.issue_list",
            "github.pr_list",
            "integration.status",
        ):
            assert safe in cfg.auto_approved_tools, safe

    def test_auto_approved_tools_excludes_destructive(self) -> None:
        cfg = CatoConfig()
        for unsafe in (
            "shell.exec",
            "shell",
            "python.execute",
            "github.pr_review",
            "github.issue_create",
            "integration.action",
            "email_send",
            "api_payment",
            "git_commit",
            "git_push",
            "delete_file",
            "write_file",
            "edit_file",
            "file",  # file tool can write — must still gate
        ):
            assert unsafe not in cfg.auto_approved_tools, unsafe

    def test_strict_approval_defaults_false(self) -> None:
        cfg = CatoConfig()
        assert cfg.strict_approval is False


# ---------------------------------------------------------------------------
# TokenChecker auto-approval
# ---------------------------------------------------------------------------

class TestTokenCheckerAutoApproval:
    def test_memory_search_auto_approves_even_with_narrow_token(self, tmp_path: Path) -> None:
        """Even when an active token only covers payment.*, memory.search
        must still auto-approve because it's in the whitelist."""
        store = _store(tmp_path)
        # Active token that does NOT cover file.read (memory's category)
        store.create(["payment.*"], 100.0, 3600)
        checker = TokenChecker(
            token_store=store,
            auto_approved_tools=["memory.search", "memory.federated"],
        )
        result = checker.check_authorization("memory.search", {}, "sess-1")
        assert result.authorized is True
        assert result.requires_user_confirmation is False
        assert "auto_approved_tools" in result.reason
        store.close()

    def test_shell_exec_still_gates_with_narrow_token(self, tmp_path: Path) -> None:
        """shell.exec is not in the whitelist; with a token that doesn't
        cover shell.execute, it must still require user confirmation."""
        store = _store(tmp_path)
        store.create(["file.read"], 100.0, 3600)
        checker = TokenChecker(
            token_store=store,
            auto_approved_tools=["memory.search", "memory.federated"],
        )
        result = checker.check_authorization("shell.exec", {}, "sess-1")
        assert result.authorized is False
        assert result.requires_user_confirmation is True
        store.close()

    def test_python_execute_still_gates(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create(["file.read"], 100.0, 3600)
        checker = TokenChecker(
            token_store=store,
            auto_approved_tools=["memory.search"],
        )
        result = checker.check_authorization("python.execute", {}, "sess-1")
        assert result.authorized is False
        assert result.requires_user_confirmation is True
        store.close()

    def test_custom_whitelist_extends_default(self, tmp_path: Path) -> None:
        """A user can add their own tool to the whitelist via config."""
        store = _store(tmp_path)
        # Token only covers file.read; web.search would normally fail
        store.create(["file.read"], 100.0, 3600)
        checker = TokenChecker(
            token_store=store,
            auto_approved_tools=["web.search"],
        )
        result = checker.check_authorization("web.search", {}, "sess-1")
        assert result.authorized is True

    def test_empty_whitelist_falls_back_to_default(self, tmp_path: Path) -> None:
        """With no auto-approved list, behaviour is the original gate logic."""
        store = _store(tmp_path)
        # No active tokens — falls into _DEFAULT_ALLOWED_TOOLS path
        checker = TokenChecker(token_store=store, auto_approved_tools=[])
        result = checker.check_authorization("memory.search", {}, "sess-1")
        # _DEFAULT_ALLOWED_TOOLS includes memory.search, so still authorized
        assert result.authorized is True
        store.close()

    def test_unmapped_tool_with_no_whitelist_denied(self, tmp_path: Path) -> None:
        """Unknown tool, no whitelist match → requires confirmation."""
        store = _store(tmp_path)
        checker = TokenChecker(token_store=store, auto_approved_tools=[])
        result = checker.check_authorization("totally_unknown_xyz", {}, "sess-1")
        assert result.authorized is False
        assert result.requires_user_confirmation is True
        store.close()


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------

class TestStrictMode:
    def test_strict_mode_disables_auto_approval(self, tmp_path: Path) -> None:
        """strict_approval=True must restore the original gating."""
        store = _store(tmp_path)
        # Token doesn't cover file.read → would normally deny memory.search
        store.create(["payment.*"], 100.0, 3600)
        checker = TokenChecker(
            token_store=store,
            auto_approved_tools=["memory.search"],
            strict_approval=True,
        )
        result = checker.check_authorization("memory.search", {}, "sess-1")
        assert result.authorized is False
        assert result.requires_user_confirmation is True
        store.close()

    def test_env_var_forces_strict_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """CATO_STRICT_APPROVAL=true overrides config strict_approval=False."""
        monkeypatch.setenv("CATO_STRICT_APPROVAL", "true")
        store = _store(tmp_path)
        store.create(["payment.*"], 100.0, 3600)
        checker = TokenChecker(
            token_store=store,
            auto_approved_tools=["memory.search"],
            strict_approval=False,
        )
        result = checker.check_authorization("memory.search", {}, "sess-1")
        assert result.authorized is False
        store.close()

    def test_env_var_off_keeps_lenient(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CATO_STRICT_APPROVAL", raising=False)
        store = _store(tmp_path)
        store.create(["payment.*"], 100.0, 3600)
        checker = TokenChecker(
            token_store=store,
            auto_approved_tools=["memory.search"],
        )
        result = checker.check_authorization("memory.search", {}, "sess-1")
        assert result.authorized is True
        store.close()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class TestApprovalLogging:
    def test_auto_approval_logs_info(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Every auto-approval must be logged at INFO with the tool name."""
        store = _store(tmp_path)
        checker = TokenChecker(
            token_store=store,
            auto_approved_tools=["memory.search"],
        )
        with caplog.at_level(logging.INFO, logger="cato.auth.token_checker"):
            result = checker.check_authorization("memory.search", {}, "sess-xyz")
        assert result.authorized is True
        # At least one INFO-level record mentioning the tool and session
        matches = [
            rec for rec in caplog.records
            if rec.levelno == logging.INFO
            and "memory.search" in rec.getMessage()
            and "sess-xyz" in rec.getMessage()
        ]
        assert matches, f"Expected INFO log for memory.search auto-approval; got: {caplog.records}"
        store.close()
