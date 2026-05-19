"""
cato/auth/token_checker.py — Pre-action scope check for delegation tokens.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .token_store import TokenStore, ACTION_CATEGORIES

logger = logging.getLogger(__name__)

# Tool-name → action category mapping (extends reversibility registry)
_DEFAULT_ALLOWED_TOOLS = frozenset({
    # Read-only / informational — always safe
    "get_time", "read_file", "list_files", "get_config",
    "memory_search", "memory_read",
    # Dotted equivalents used by internal tool registry
    "memory.search", "memory.federated",
    "graph.query", "graph.related",
    # File operations (agent needs these for basic functionality)
    "file", "write_file", "edit_file",
    # Web / search / research (read-only extraction)
    "web_search", "web.search", "web.code", "web.news",
    "academic.arxiv", "academic.semantic_scholar", "academic.pubmed",
    # Conduit browser (navigation + extraction)
    "conduit_navigate", "conduit_extract", "conduit_click", "conduit_type",
    "conduit.crawl", "conduit.monitor",
    "browser",
    # Code execution (sandboxed)
    "python.execute", "shell_execute", "shell.exec", "shell",
    # GitHub (read + write)
    "github.issue_list", "github.pr_list",
    "github.pr_review", "github.issue_create",
    "git_commit", "git_push",
    # Flows
    "flow.run",
})

_TOOL_CATEGORY_MAP: dict[str, str] = {
    # Conduit browser tools
    "conduit_navigate": "web.navigate",
    "conduit_extract":  "web.extract",
    "conduit_click":    "web.navigate",
    "conduit_type":     "web.navigate",
    "conduit.crawl":    "web.extract",
    "conduit.monitor":  "web.extract",
    # Web search / research
    "web_search":           "web.extract",
    "web.search":           "web.extract",
    "web.code":             "web.extract",
    "web.news":             "web.extract",
    "academic.arxiv":       "web.extract",
    "academic.semantic_scholar": "web.extract",
    "academic.pubmed":      "web.extract",
    # File operations
    "file":             "file.read",
    "read_file":        "file.read",
    "write_file":       "file.write",
    "edit_file":        "file.write",
    "delete_file":      "file.delete",
    # Browser
    "browser":          "web.navigate",
    # Memory
    "memory.search":    "file.read",
    "memory.federated": "file.read",
    # Flows
    "flow.run":         "shell.execute",
    # Knowledge graph
    "graph.query":      "file.read",
    "graph.related":    "file.read",
    # GitHub
    "github.pr_review":    "git.write",
    "github.issue_create": "git.write",
    "github.issue_list":   "file.read",
    "github.pr_list":      "file.read",
    # Execution
    "git_commit":       "git.write",
    "git_push":         "git.write",
    "email_send":       "email.send",
    "api_payment":      "payment.*",
    "shell_execute":    "shell.execute",
    "shell.exec":       "shell.execute",
    "shell":            "shell.execute",
    "python.execute":   "shell.execute",
    "python":           "shell.execute",
}


@dataclass
class AuthResult:
    authorized: bool
    token_id: Optional[str]
    reason: str
    requires_user_confirmation: bool


def _env_strict_approval() -> bool:
    """Return True if CATO_STRICT_APPROVAL env var forces strict mode."""
    val = os.environ.get("CATO_STRICT_APPROVAL", "").strip().lower()
    return val in {"1", "true", "yes", "y", "on"}


class TokenChecker:
    """Check delegation tokens before executing tool actions.

    The checker supports an `auto_approved_tools` whitelist that
    short-circuits the gate for known-safe, reversible tools (memory,
    search, reads).  This avoids the daemon being unable to call its own
    memory tool just because no delegation token is currently active or
    because the active token's category list is narrow.

    Strict mode (config field `strict_approval` or env
    `CATO_STRICT_APPROVAL=true`) restores the original behaviour where
    every tool must either be in `_DEFAULT_ALLOWED_TOOLS` or covered by
    an active delegation token.
    """

    def __init__(
        self,
        token_store: Optional[TokenStore] = None,
        db_path: Optional[Path] = None,
        auto_approved_tools: Optional[Iterable[str]] = None,
        strict_approval: bool = False,
    ) -> None:
        self._store = token_store or TokenStore(db_path=db_path)
        self._auto_approved: frozenset[str] = frozenset(auto_approved_tools or [])
        self._strict: bool = bool(strict_approval) or _env_strict_approval()

    def check_authorization(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        agent_session_id: str,
        estimated_cost: float = 0.0,
    ) -> AuthResult:
        # Deactivate expired tokens first
        self._store.deactivate_expired()

        # Short-circuit: auto-approve reversible tools (unless strict mode).
        # This runs BEFORE the delegation-token check so memory.search and
        # other reversible tools work even when a narrow token is active.
        if (
            not self._strict
            and tool_name in self._auto_approved
        ):
            logger.info(
                "auto-approve tool=%s session=%s reason=auto_approved_tools",
                tool_name, agent_session_id,
            )
            return AuthResult(
                authorized=True,
                token_id=None,
                reason=(
                    f"Tool '{tool_name}' is in auto_approved_tools "
                    "(reversible; no per-call user approval required)."
                ),
                requires_user_confirmation=False,
            )

        category = _TOOL_CATEGORY_MAP.get(tool_name)
        if category is None:
            return AuthResult(
                authorized=False,
                token_id=None,
                reason=(
                    f"Tool '{tool_name}' has no mapped category; "
                    "user confirmation required before executing unmapped tools."
                ),
                requires_user_confirmation=True,
            )

        active_tokens = self._store.list_active()
        if not active_tokens:
            if tool_name in _DEFAULT_ALLOWED_TOOLS:
                return AuthResult(
                    authorized=True,
                    token_id=None,
                    reason="No active delegation tokens; tool is in default-allowed list.",
                    requires_user_confirmation=False,
                )
            return AuthResult(
                authorized=False,
                token_id=None,
                reason="No active delegation tokens; tool requires explicit delegation.",
                requires_user_confirmation=True,
            )

        for token in active_tokens:
            # Check category — support wildcard suffix (e.g. "payment.*")
            cats = token.allowed_action_categories
            matched = False
            for c in cats:
                if c == category:
                    matched = True
                    break
                if c.endswith(".*") and category.startswith(c[:-2]):
                    matched = True
                    break
            if not matched:
                continue  # This token doesn't cover this category

            # Check spending ceiling
            remaining = token.spending_ceiling - token.spending_used
            if estimated_cost > 0 and estimated_cost > remaining:
                return AuthResult(
                    authorized=False,
                    token_id=token.token_id,
                    reason=(
                        f"Spending ceiling exceeded: need {estimated_cost:.2f}, "
                        f"have {remaining:.2f} remaining."
                    ),
                    requires_user_confirmation=True,
                )

            # Authorized — deduct cost
            if estimated_cost > 0:
                self._store.deduct_spending(token.token_id, estimated_cost)

            return AuthResult(
                authorized=True,
                token_id=token.token_id,
                reason=(
                    f"Authorized by token {token.token_id[:8]}\u2026 "
                    f"(category={category})"
                ),
                requires_user_confirmation=False,
            )

        return AuthResult(
            authorized=False,
            token_id=None,
            reason=(
                f"No active token covers category '{category}' "
                f"for tool '{tool_name}'."
            ),
            requires_user_confirmation=True,
        )
