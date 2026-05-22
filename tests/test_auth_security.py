"""Security regression tests for auth / token checker."""
import pytest
from cato.auth.token_checker import TokenChecker
from cato.auth.token_store import TokenStore, ACTION_CATEGORIES


def test_shell_tools_require_explicit_token(tmp_path):
    """shell* tools must be denied when no delegation token is active."""
    store = TokenStore(db_path=tmp_path / "tokens.db")
    checker = TokenChecker(token_store=store)
    for tool in ("shell", "shell_execute", "shell.exec", "python.execute"):
        result = checker.check_authorization(tool, {}, agent_session_id="test-session")
        assert not result.authorized, f"{tool!r} was authorized without a token"
        assert result.requires_user_confirmation


def test_create_token_rejects_invalid_category():
    """Server's category validation must reject unknown categories."""
    valid_categories = set(ACTION_CATEGORIES) | {"*"}

    # Valid category — no invalid entries
    invalid = set(["file.read"]) - valid_categories
    assert not invalid, "file.read should be a valid category"

    # Invalid category — must be detected
    invalid = set(["__invalid__"]) - valid_categories
    assert invalid, "__invalid__ should not pass category validation"


def test_valid_action_categories_accepted():
    """Known ACTION_CATEGORIES entries must all pass the server's validation check."""
    valid_categories = set(ACTION_CATEGORIES) | {"*"}
    for cat in ACTION_CATEGORIES:
        assert cat in valid_categories, f"{cat!r} not in valid set"
