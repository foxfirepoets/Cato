"""
cato/vault.py — Encrypted credential storage for CATO.

AES-256-GCM encryption with Argon2id key derivation.
Stores API keys, tokens, and passwords in ~/.cato/vault.enc.
Master password is prompted once on first run; derived key is cached in memory only.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from argon2.low_level import hash_secret_raw, Type
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .platform import get_data_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-level vault password cache (F-07)
# ---------------------------------------------------------------------------

_CACHED_VAULT_PASSWORD: str | None = None


def _get_vault_password() -> str | None:
    """Return the vault password, caching it on first read to survive env var removal."""
    global _CACHED_VAULT_PASSWORD
    if _CACHED_VAULT_PASSWORD:
        return _CACHED_VAULT_PASSWORD
    env_password = os.environ.get("CATO_VAULT_PASSWORD")
    if env_password:
        _CACHED_VAULT_PASSWORD = env_password
        os.environ.pop("CATO_VAULT_PASSWORD", None)
        return _CACHED_VAULT_PASSWORD
    return None


# ---------------------------------------------------------------------------
# Canary key (P2-11)
# ---------------------------------------------------------------------------

CANARY_KEY_NAME = "_cato_canary_"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VAULT_FILE = get_data_dir() / "vault.enc"
_SALT_SIZE = 32       # bytes — stored inside the vault file
_NONCE_SIZE = 12      # bytes — per-encryption nonce
_KEY_SIZE = 32        # bytes — AES-256

# Argon2id parameters (OWASP recommended minimum)
_ARGON2_TIME_COST = 3
_ARGON2_MEMORY_COST = 65536   # 64 MiB
_ARGON2_PARALLELISM = 4


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit key from *password* using Argon2id."""
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_COST,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=_KEY_SIZE,
        type=Type.ID,
    )


def _encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Return nonce || ciphertext using AES-256-GCM."""
    nonce = secrets.token_bytes(_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return nonce + ciphertext


def _decrypt(blob: bytes, key: bytes) -> bytes:
    """Decrypt nonce || ciphertext produced by _encrypt."""
    nonce = blob[:_NONCE_SIZE]
    ciphertext = blob[_NONCE_SIZE:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------

class VaultError(Exception):
    """Raised on vault authentication or I/O failures."""


class Vault:
    """
    AES-256-GCM encrypted credential store.

    The on-disk layout of ``vault.enc`` is:

        base64( salt[32] + nonce[12] + aesgcm_ciphertext )

    The plaintext inside the ciphertext is a UTF-8 JSON object
    mapping string keys to string values.

    Usage::

        vault = Vault()
        vault.set("OPENAI_API_KEY", "sk-...")
        key = vault.get("OPENAI_API_KEY")
        vault.delete("OPENAI_API_KEY")
    """

    def __init__(self, vault_path: Optional[Path] = None) -> None:
        self._path: Path = vault_path or _VAULT_FILE
        self._key: Optional[bytes] = None          # in-memory only
        self._data: Optional[dict[str, str]] = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _prompt_password(self, confirm: bool = False) -> str:
        """Prompt for the master password, with optional confirmation."""
        import sys
        # Check process-level cache and environment variable first
        cached = _get_vault_password()
        if cached:
            return cached

        if not sys.stdin.isatty():
            raise VaultError(
                "Vault is locked and no TTY is available to prompt for the master password. "
                "Run 'cato init' interactively first, then set CATO_VAULT_PASSWORD "
                "in the environment or call vault.unlock(password) before starting the daemon."
            )
        password = getpass.getpass("Vault master password: ")
        if confirm:
            confirm_pw = getpass.getpass("Confirm master password: ")
            if password != confirm_pw:
                raise VaultError("Passwords do not match.")
        return password

    def _unlock(self) -> None:
        """Load and decrypt the vault, caching the key and data in memory."""
        if self._key is not None and self._data is not None:
            return  # already unlocked

        if not self._path.exists():
            # First run — create new vault
            password = self._prompt_password(confirm=True)
            salt = secrets.token_bytes(_SALT_SIZE)
            self._key = _derive_key(password, salt)
            self._data = {}
            self._save(salt)
            return

        # Existing vault
        raw = base64.b64decode(self._path.read_bytes())
        salt = raw[:_SALT_SIZE]
        blob = raw[_SALT_SIZE:]

        password = self._prompt_password(confirm=False)
        key = _derive_key(password, salt)

        try:
            plaintext = _decrypt(blob, key)
        except Exception as exc:
            raise VaultError("Wrong master password or corrupted vault.") from exc

        self._key = key
        self._data = json.loads(plaintext.decode("utf-8"))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self, salt: Optional[bytes] = None) -> None:
        """Encrypt current _data and write to disk."""
        assert self._key is not None and self._data is not None

        if salt is None:
            # Re-read existing salt from disk
            existing = base64.b64decode(self._path.read_bytes())
            salt = existing[:_SALT_SIZE]

        plaintext = json.dumps(self._data, ensure_ascii=True).encode("utf-8")
        blob = _encrypt(plaintext, self._key)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_bytes(base64.b64encode(salt + blob))
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        """Return the stored value for *key*, or None if not found.

        If the returned value matches the canary key, logs a warning
        to alert of a potential credential leak.
        """
        try:
            self._unlock()
        except Exception:
            return os.environ.get(key)
        assert self._data is not None
        value = self._data.get(key)
        if not value:
            value = os.environ.get(key) or value
        # Canary detection: if the value looks like our canary, warn
        if value is not None and key != CANARY_KEY_NAME:
            canary_val = self._data.get(CANARY_KEY_NAME)
            if canary_val and value == canary_val:
                logger.warning(
                    "CANARY KEY ACCESSED — possible credential leak! "
                    "Key '%s' returned the canary value. Investigate immediately.", key
                )
        return value

    def set(self, key: str, value: str) -> None:
        """Store *value* under *key* and persist to disk."""
        self._unlock()
        assert self._data is not None
        self._data[key] = value
        self._save()

    def delete(self, key: str) -> bool:
        """Remove *key* from the vault. Returns True if it existed."""
        self._unlock()
        assert self._data is not None
        existed = key in self._data
        if existed:
            del self._data[key]
            self._save()
        return existed

    def list_keys(self) -> list[str]:
        """Return sorted list of stored key names (not values).

        Excludes the internal canary key from the public listing.
        """
        self._unlock()
        assert self._data is not None
        return sorted(k for k in self._data.keys() if k != CANARY_KEY_NAME)

    # ------------------------------------------------------------------
    # Canary key (P2-11)
    # ------------------------------------------------------------------

    def create_canary(self) -> str:
        """
        Generate a synthetic API key, store it as _cato_canary_, and return it.

        The canary looks like a real API key (starts with 'sk-cato-canary-')
        so it would trigger external API rejections if accidentally used.
        If any real key in the vault ever returns this value, a warning is logged.
        """
        self._unlock()
        assert self._data is not None
        # Generate a realistic-looking synthetic key
        canary_val = "sk-cato-canary-" + secrets.token_hex(24)
        self._data[CANARY_KEY_NAME] = canary_val
        self._save()
        logger.info("Vault canary key created (stored as %s)", CANARY_KEY_NAME)
        return canary_val

    def check_canary_used(self, key_val: str) -> bool:
        """
        Return True if *key_val* matches the stored canary value.

        Used by external monitors to detect if the canary key was used
        in any outbound request.
        """
        self._unlock()
        assert self._data is not None
        canary = self._data.get(CANARY_KEY_NAME)
        return canary is not None and key_val == canary

    def is_locked(self) -> bool:
        """Return True if the vault has not yet been unlocked this session."""
        return self._key is None

    @classmethod
    def create(cls, password: str, vault_path: Path | None = None) -> "Vault":
        """Create and initialize a new vault with the given password.

        Any existing vault file is removed first so the password always
        produces a fresh vault (reinit flow in `cato init`).
        """
        v = cls(vault_path)
        if v._path.exists():
            v._path.unlink()
        v.unlock(password)
        return v

    def unlock(self, password: str) -> None:
        """Unlock the vault with the given password (bypasses getpass prompt).

        Creates a new vault if the file does not yet exist.
        Raises VaultError on wrong password.
        """
        if self._key is not None and self._data is not None:
            return  # already unlocked

        if not self._path.exists():
            salt = secrets.token_bytes(_SALT_SIZE)
            self._key = _derive_key(password, salt)
            self._data = {}
            self._save(salt)
            return

        raw = base64.b64decode(self._path.read_bytes())
        salt = raw[:_SALT_SIZE]
        blob = raw[_SALT_SIZE:]
        key = _derive_key(password, salt)
        try:
            plaintext = _decrypt(blob, key)
        except Exception as exc:
            raise VaultError("Wrong master password or corrupted vault.") from exc
        self._key = key
        self._data = json.loads(plaintext.decode("utf-8"))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_vault_instance: Optional[Vault] = None


def get_vault() -> Vault:
    """Return the module-level Vault singleton."""
    global _vault_instance
    if _vault_instance is None:
        _vault_instance = Vault()
    return _vault_instance
