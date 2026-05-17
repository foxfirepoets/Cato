"""
tests/test_vault_crypto.py — Tests for cato.vault_crypto (Ed25519 AP2 keypair).

Covers:
- get_or_create_keypair: generation, idempotence, recovery, pubkey-rederive
- sign / verify roundtrip
- verify defensive behaviour (tampered msg, wrong pubkey, truncated sig,
  malformed pubkey)
- public_key_b64 roundtrip
- Thread-safety under contention
- Private key is never logged
"""

from __future__ import annotations

import base64
import logging
import threading

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cato.vault_crypto import (
    get_or_create_keypair,
    public_key_b64,
    sign,
    verify,
)


# ---------------------------------------------------------------------------
# In-memory MockVault — vault stores STRINGS only.
# ---------------------------------------------------------------------------


class MockVault:
    def __init__(self, initial: dict | None = None):
        self._d = dict(initial or {})

    def get(self, k):
        return self._d.get(k)

    def set(self, k, v):
        assert isinstance(v, str), "vault stores strings only"
        self._d[k] = v

    def list_keys(self):
        return list(self._d.keys())

    def delete(self, k):
        self._d.pop(k, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVaultCrypto:
    def test_generates_new_keypair_when_vault_empty(self):
        vault = MockVault()
        priv, pub = get_or_create_keypair(vault)

        assert isinstance(priv, bytes)
        assert isinstance(pub, bytes)
        assert len(priv) == 32
        assert len(pub) == 32
        # Vault must now hold both keys as b64 strings.
        assert "CATO_AP2_PRIVKEY" in vault.list_keys()
        assert "CATO_AP2_PUBKEY" in vault.list_keys()
        assert isinstance(vault.get("CATO_AP2_PRIVKEY"), str)
        assert isinstance(vault.get("CATO_AP2_PUBKEY"), str)
        # Stored values decode back to the same raw bytes.
        assert base64.b64decode(vault.get("CATO_AP2_PRIVKEY")) == priv
        assert base64.b64decode(vault.get("CATO_AP2_PUBKEY")) == pub

    def test_idempotent_returns_same_keypair_on_second_call(self):
        vault = MockVault()
        priv1, pub1 = get_or_create_keypair(vault)
        snap_priv = vault.get("CATO_AP2_PRIVKEY")
        snap_pub = vault.get("CATO_AP2_PUBKEY")

        priv2, pub2 = get_or_create_keypair(vault)

        assert priv1 == priv2
        assert pub1 == pub2
        # Vault values themselves must not have changed.
        assert vault.get("CATO_AP2_PRIVKEY") == snap_priv
        assert vault.get("CATO_AP2_PUBKEY") == snap_pub

    def test_loads_existing_keypair_from_vault(self):
        # Build a known Ed25519 private key out-of-band; pre-populate vault
        # with ONLY the privkey (pubkey deliberately absent).
        priv_obj = Ed25519PrivateKey.generate()
        priv_bytes = priv_obj.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        expected_pub = priv_obj.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        vault = MockVault(
            initial={"CATO_AP2_PRIVKEY": base64.b64encode(priv_bytes).decode("ascii")}
        )
        assert vault.get("CATO_AP2_PUBKEY") is None

        priv_out, pub_out = get_or_create_keypair(vault)

        assert priv_out == priv_bytes
        assert pub_out == expected_pub
        # Pubkey should have been written back into the vault.
        assert vault.get("CATO_AP2_PUBKEY") is not None
        assert base64.b64decode(vault.get("CATO_AP2_PUBKEY")) == expected_pub

    def test_sign_returns_64_byte_signature(self):
        vault = MockVault()
        sig = sign(vault, b"hello world")
        assert isinstance(sig, bytes)
        assert len(sig) == 64

    def test_sign_verify_roundtrip(self):
        vault = MockVault()
        msg = b"AP2 mandate payload"
        sig = sign(vault, msg)
        _priv, pub = get_or_create_keypair(vault)
        assert verify(pub, msg, sig) is True

    def test_verify_rejects_tampered_message(self):
        vault = MockVault()
        msg_a = b"original message"
        msg_b = b"tampered message"
        sig = sign(vault, msg_a)
        _priv, pub = get_or_create_keypair(vault)
        # Must return False, not raise.
        assert verify(pub, msg_b, sig) is False

    def test_verify_rejects_wrong_pubkey(self):
        v1 = MockVault()
        v2 = MockVault()
        msg = b"signed by v1"
        sig = sign(v1, msg)
        _p1, pub1 = get_or_create_keypair(v1)
        _p2, pub2 = get_or_create_keypair(v2)
        assert pub1 != pub2
        # Signature from v1 verified against v2's pubkey -> False.
        assert verify(pub2, msg, sig) is False

    def test_verify_rejects_truncated_signature(self):
        vault = MockVault()
        msg = b"truncate me"
        sig = sign(vault, msg)
        _priv, pub = get_or_create_keypair(vault)
        truncated = sig[:-1]
        # Must return False without raising.
        assert verify(pub, msg, truncated) is False

    def test_verify_rejects_malformed_pubkey(self):
        # 16 bytes instead of 32 — must not raise InvalidKey/ValueError.
        bad_pub = b"\x00" * 16
        sig = b"\x00" * 64
        result = verify(bad_pub, b"any message", sig)
        assert result is False

    def test_public_key_b64_roundtrips(self):
        vault = MockVault()
        b64 = public_key_b64(vault)
        assert isinstance(b64, str)
        decoded = base64.b64decode(b64)
        _priv, pub = get_or_create_keypair(vault)
        assert decoded == pub
        assert len(decoded) == 32

    def test_threadsafe_concurrent_get_or_create(self):
        vault = MockVault()
        n_threads = 8
        barrier = threading.Barrier(n_threads)
        results: list = [None] * n_threads
        errors: list = []

        def worker(idx: int):
            try:
                # Synchronise so all threads hit the critical section together.
                barrier.wait(timeout=5.0)
                results[idx] = get_or_create_keypair(vault)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors, f"thread raised: {errors}"
        assert all(r is not None for r in results)
        first = results[0]
        for r in results[1:]:
            assert r == first, "concurrent callers must observe identical keypair"

    def test_does_not_log_private_key(self, caplog):
        # Capture everything under the "cato" logger tree at DEBUG.
        caplog.set_level(logging.DEBUG, logger="cato")
        vault = MockVault()
        priv, _pub = get_or_create_keypair(vault)
        priv_b64 = base64.b64encode(priv).decode("ascii")

        for record in caplog.records:
            msg = record.getMessage()
            assert priv_b64 not in msg, (
                f"private key b64 leaked into log record: {record.name} {record.levelname}"
            )
