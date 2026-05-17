"""
cato/vault_crypto.py — Ed25519 keypair bound to Cato's vault for AP2 signing.

Cato participates in SwarmSync's Agent Payments Protocol (AP2) by signing
outbound requests with a long-lived Ed25519 identity key. The private key
material lives ONLY inside the AES-256-GCM-encrypted vault (`vault.enc`)
and is loaded into process memory on demand. The matching public key is
also cached in the vault for fast retrieval and for SwarmSync registration.

Vault layout (keys are base64-encoded strings, since `Vault.set()` is
strings-only):

    CATO_AP2_PRIVKEY  → b64(raw 32-byte Ed25519 private seed)
    CATO_AP2_PUBKEY   → b64(raw 32-byte Ed25519 public key)

Public API:
    get_or_create_keypair(vault) -> tuple[bytes, bytes]
    sign(vault, message: bytes) -> bytes
    public_key_b64(vault) -> str
    verify(public_key_bytes: bytes, message: bytes, signature: bytes) -> bool

Security notes:
    - The private key is never logged, printed, or written outside the vault.
    - Get-or-create is guarded by a module-level threading.Lock so two
      concurrent callers cannot both generate a fresh keypair and race
      to persist it (one would otherwise overwrite the other).
    - verify() swallows InvalidSignature and returns False, so callers can
      treat it as a pure boolean check without try/except boilerplate.
"""

from __future__ import annotations

import base64
import logging
import threading

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

# Vault key names — keep stable; external tooling (SwarmSync registration,
# audit scripts) may read these directly.
_VAULT_PRIVKEY = "CATO_AP2_PRIVKEY"
_VAULT_PUBKEY = "CATO_AP2_PUBKEY"

# Single module-level lock protects the get-or-create critical section
# across all vault instances. The vault itself serializes disk I/O, but
# we still need to ensure only one caller decides "generate new" at a time.
_keypair_lock = threading.Lock()


def _privkey_to_raw_bytes(priv: Ed25519PrivateKey) -> bytes:
    """Serialize an Ed25519 private key to its 32 raw seed bytes."""
    return priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _pubkey_to_raw_bytes(pub: Ed25519PublicKey) -> bytes:
    """Serialize an Ed25519 public key to its 32 raw bytes."""
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def get_or_create_keypair(vault) -> tuple[bytes, bytes]:
    """Return (private_key_bytes, public_key_bytes) for AP2 signing.

    If CATO_AP2_PRIVKEY exists in vault, decode from b64 and return.
    Otherwise generate a new Ed25519 keypair, store both privkey + pubkey
    in the vault as b64 strings (keys: CATO_AP2_PRIVKEY, CATO_AP2_PUBKEY),
    and return the raw 32-byte values.
    """
    with _keypair_lock:
        priv_b64 = vault.get(_VAULT_PRIVKEY)
        if priv_b64:
            priv_bytes = base64.b64decode(priv_b64)
            pub_b64 = vault.get(_VAULT_PUBKEY)
            if pub_b64:
                pub_bytes = base64.b64decode(pub_b64)
            else:
                # Privkey present but pubkey somehow missing — derive and persist.
                priv_obj = Ed25519PrivateKey.from_private_bytes(priv_bytes)
                pub_bytes = _pubkey_to_raw_bytes(priv_obj.public_key())
                vault.set(_VAULT_PUBKEY, base64.b64encode(pub_bytes).decode("ascii"))
            return priv_bytes, pub_bytes

        # No keypair yet — generate, persist, return.
        priv_obj = Ed25519PrivateKey.generate()
        priv_bytes = _privkey_to_raw_bytes(priv_obj)
        pub_bytes = _pubkey_to_raw_bytes(priv_obj.public_key())

        vault.set(_VAULT_PRIVKEY, base64.b64encode(priv_bytes).decode("ascii"))
        vault.set(_VAULT_PUBKEY, base64.b64encode(pub_bytes).decode("ascii"))

        # Pubkey is safe to log; privkey is NEVER logged.
        logger.info(
            "Generated new Ed25519 AP2 keypair; pubkey=%s",
            base64.b64encode(pub_bytes).decode("ascii"),
        )
        return priv_bytes, pub_bytes


def sign(vault, message: bytes) -> bytes:
    """Sign message with vault's Ed25519 private key. Returns raw 64-byte signature."""
    priv_bytes, _pub_bytes = get_or_create_keypair(vault)
    priv_obj = Ed25519PrivateKey.from_private_bytes(priv_bytes)
    return priv_obj.sign(message)


def public_key_b64(vault) -> str:
    """Return base64-encoded Ed25519 public key (for SwarmSync registration)."""
    _priv_bytes, pub_bytes = get_or_create_keypair(vault)
    return base64.b64encode(pub_bytes).decode("ascii")


def verify(public_key_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """Verify a signature. Returns True/False; does not raise on bad sig."""
    try:
        pub_obj = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        pub_obj.verify(signature, message)
        return True
    except InvalidSignature:
        return False
    except Exception:
        # Malformed pubkey, wrong length signature, etc. — treat as "not valid".
        return False
