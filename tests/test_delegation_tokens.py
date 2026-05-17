"""
tests/test_delegation_tokens.py — Tests for Skill 5 (Delegated Authority Token System).

All tests use tmp_path for DB isolation.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from cato.auth.token_store import DelegationToken, TokenStore
from cato.auth.token_checker import AuthResult, TokenChecker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(tmp_path: Path) -> TokenStore:
    return TokenStore(db_path=tmp_path / "tokens.db")


def make_checker(tmp_path: Path) -> TokenChecker:
    store = make_store(tmp_path)
    return TokenChecker(token_store=store)


# ---------------------------------------------------------------------------
# TokenStore.create()
# ---------------------------------------------------------------------------

class TestTokenStoreCreate:
    def test_create_returns_delegation_token(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(
            allowed_action_categories=["file.read", "file.write"],
            spending_ceiling=100.0,
            expires_in_seconds=3600,
        )
        assert isinstance(token, DelegationToken)
        store.close()

    def test_token_id_is_uuid(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, 3600)
        parsed = uuid.UUID(token.token_id)
        assert str(parsed) == token.token_id
        store.close()

    def test_expires_at_after_created_at(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, 3600)
        assert token.expires_at > token.created_at
        store.close()

    def test_spending_used_starts_at_zero(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, 3600)
        assert token.spending_used == 0.0
        store.close()

    def test_active_is_true_on_creation(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, 3600)
        assert token.active is True
        store.close()

    def test_allowed_categories_stored(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        cats = ["file.read", "file.write", "git.read"]
        token = store.create(cats, 100.0, 7200)
        assert set(token.allowed_action_categories) == set(cats)
        store.close()

    def test_spending_ceiling_stored(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 250.0, 3600)
        assert token.spending_ceiling == pytest.approx(250.0)
        store.close()

    def test_token_hash_is_64_char_hex(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, 3600)
        assert len(token.token_hash) == 64
        assert all(c in "0123456789abcdef" for c in token.token_hash)
        store.close()

    def test_parameter_constraints_default_empty(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, 3600)
        assert token.parameter_constraints == {}
        store.close()

    def test_parameter_constraints_stored(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        constraints = {"max_file_size": 1024, "allowed_extensions": [".txt"]}
        token = store.create(["file.read"], 50.0, 3600, parameter_constraints=constraints)
        assert token.parameter_constraints == constraints
        store.close()

    def test_parent_token_id_stored(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        parent_id = str(uuid.uuid4())
        token = store.create(["file.read"], 50.0, 3600, parent_token_id=parent_id)
        assert token.parent_token_id == parent_id
        store.close()


# ---------------------------------------------------------------------------
# TokenStore.list_active() and get()
# ---------------------------------------------------------------------------

class TestTokenStoreListAndGet:
    def test_list_active_returns_created_token(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, 3600)
        active = store.list_active()
        ids = [t.token_id for t in active]
        assert token.token_id in ids
        store.close()

    def test_get_returns_none_for_missing(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        result = store.get(str(uuid.uuid4()))
        assert result is None
        store.close()

    def test_get_returns_correct_token(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, 3600)
        fetched = store.get(token.token_id)
        assert fetched is not None
        assert fetched.token_id == token.token_id
        assert fetched.spending_ceiling == pytest.approx(50.0)
        store.close()


# ---------------------------------------------------------------------------
# TokenStore.revoke()
# ---------------------------------------------------------------------------

class TestTokenStoreRevoke:
    def test_revoke_returns_true_for_existing(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, 3600)
        ok = store.revoke(token.token_id, reason="test revocation")
        assert ok is True
        store.close()

    def test_revoke_returns_false_for_missing(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        ok = store.revoke(str(uuid.uuid4()))
        assert ok is False
        store.close()

    def test_revoked_token_not_in_list_active(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, 3600)
        store.revoke(token.token_id)
        active = store.list_active()
        ids = [t.token_id for t in active]
        assert token.token_id not in ids
        store.close()


# ---------------------------------------------------------------------------
# TokenStore.deduct_spending()
# ---------------------------------------------------------------------------

class TestTokenStoreDeductSpending:
    def test_deduct_spending_updates_atomically(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 100.0, 3600)
        ok = store.deduct_spending(token.token_id, 25.0)
        assert ok is True
        fetched = store.get(token.token_id)
        assert fetched is not None
        assert fetched.spending_used == pytest.approx(25.0)
        store.close()

    def test_deduct_spending_accumulates(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 100.0, 3600)
        store.deduct_spending(token.token_id, 10.0)
        store.deduct_spending(token.token_id, 15.0)
        fetched = store.get(token.token_id)
        assert fetched is not None
        assert fetched.spending_used == pytest.approx(25.0)
        store.close()


# ---------------------------------------------------------------------------
# TokenStore.deactivate_expired()
# ---------------------------------------------------------------------------

class TestTokenStoreExpiry:
    def test_deactivate_expired_marks_expired_inactive(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        # Create a token that expires immediately (negative seconds = already expired)
        token = store.create(["file.read"], 50.0, expires_in_seconds=-1)
        # Should be expired already
        count = store.deactivate_expired()
        assert count >= 1
        fetched = store.get(token.token_id)
        assert fetched is not None
        assert fetched.active is False
        store.close()

    def test_deactivate_expired_does_not_touch_active(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 50.0, expires_in_seconds=3600)
        store.deactivate_expired()
        fetched = store.get(token.token_id)
        assert fetched is not None
        assert fetched.active is True
        store.close()


# ---------------------------------------------------------------------------
# TokenChecker.check_authorization()
# ---------------------------------------------------------------------------

class TestTokenChecker:
    def test_no_active_tokens_allows_proceed(self, tmp_path: Path) -> None:
        checker = make_checker(tmp_path)
        result = checker.check_authorization("read_file", {}, "sess-1")
        assert result.authorized is True
        assert result.token_id is None
        assert "No active delegation tokens" in result.reason

    def test_valid_token_covering_category_authorized(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read", "file.write"], 100.0, 3600)
        checker = TokenChecker(token_store=store)
        result = checker.check_authorization("read_file", {}, "sess-1")
        assert result.authorized is True
        assert result.token_id == token.token_id

    def test_token_not_covering_category_denied(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create(["file.read"], 100.0, 3600)
        checker = TokenChecker(token_store=store)
        # email.send is not covered by file.read
        result = checker.check_authorization("email_send", {}, "sess-1")
        assert result.authorized is False
        assert result.requires_user_confirmation is True

    def test_spending_ceiling_exceeded_denied(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create(["file.read"], 10.0, 3600)
        checker = TokenChecker(token_store=store)
        # estimated_cost > ceiling
        result = checker.check_authorization("read_file", {}, "sess-1", estimated_cost=50.0)
        assert result.authorized is False
        assert "ceiling exceeded" in result.reason.lower()

    def test_unknown_tool_requires_confirmation(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create(["file.read"], 100.0, 3600)
        checker = TokenChecker(token_store=store)
        result = checker.check_authorization("totally_unknown_tool_xyz", {}, "sess-1")
        assert result.authorized is False
        assert result.requires_user_confirmation is True
        assert "no mapped category" in result.reason

    def test_auth_result_fields_populated(self, tmp_path: Path) -> None:
        checker = make_checker(tmp_path)
        result = checker.check_authorization("read_file", {}, "sess-1")
        assert isinstance(result, AuthResult)
        assert isinstance(result.authorized, bool)
        assert isinstance(result.reason, str)
        assert isinstance(result.requires_user_confirmation, bool)

    def test_wildcard_category_payment_star_matches(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create(["payment.*"], 1000.0, 3600)
        checker = TokenChecker(token_store=store)
        result = checker.check_authorization("api_payment", {}, "sess-1")
        # api_payment maps to "payment.*" which should match token with "payment.*"
        assert result.authorized is True

    def test_spending_deducted_on_authorized_call(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        token = store.create(["file.read"], 100.0, 3600)
        checker = TokenChecker(token_store=store)
        checker.check_authorization("read_file", {}, "sess-1", estimated_cost=20.0)
        fetched = store.get(token.token_id)
        assert fetched is not None
        assert fetched.spending_used == pytest.approx(20.0)
        store.close()
