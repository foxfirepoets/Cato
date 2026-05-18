"""Shared SwarmSync credential helpers."""

from __future__ import annotations

import os
from typing import Any

CANONICAL_SWARMSYNC_KEY = "SWARMSYNC_API_KEY"
LEGACY_SWARMSYNC_KEYS = ("SWARM_SYNC_API_KEY",)


def _get_vault_value(vault: Any, key: str) -> str:
    if vault is None:
        return ""
    try:
        value = vault.get(key)
    except Exception:
        return ""
    return str(value or "").strip()


def get_swarmsync_api_key(vault: Any = None) -> tuple[str, str]:
    """Return ``(api_key, source)`` using canonical, legacy, then env names."""
    canonical = _get_vault_value(vault, CANONICAL_SWARMSYNC_KEY)
    if canonical:
        return canonical, CANONICAL_SWARMSYNC_KEY
    for legacy_key in LEGACY_SWARMSYNC_KEYS:
        legacy = _get_vault_value(vault, legacy_key)
        if legacy:
            return legacy, legacy_key
    env_canonical = os.environ.get(CANONICAL_SWARMSYNC_KEY, "").strip()
    if env_canonical:
        return env_canonical, f"env:{CANONICAL_SWARMSYNC_KEY}"
    for legacy_key in LEGACY_SWARMSYNC_KEYS:
        env_legacy = os.environ.get(legacy_key, "").strip()
        if env_legacy:
            return env_legacy, f"env:{legacy_key}"
    return "", ""


def swarmsync_key_status(vault: Any = None) -> dict[str, Any]:
    """Return non-secret diagnostics for SwarmSync key normalization."""
    key, source = get_swarmsync_api_key(vault)
    legacy_present = any(bool(_get_vault_value(vault, name)) for name in LEGACY_SWARMSYNC_KEYS)
    env_legacy_present = any(bool(os.environ.get(name, "").strip()) for name in LEGACY_SWARMSYNC_KEYS)
    return {
        "present": bool(key),
        "source": source,
        "canonical_present": bool(_get_vault_value(vault, CANONICAL_SWARMSYNC_KEY)),
        "legacy_present": legacy_present,
        "env_canonical_present": bool(os.environ.get(CANONICAL_SWARMSYNC_KEY, "").strip()),
        "env_legacy_present": env_legacy_present,
        "needs_normalization": bool(key) and source in {
            *LEGACY_SWARMSYNC_KEYS,
            *(f"env:{name}" for name in LEGACY_SWARMSYNC_KEYS),
        },
        "prefix": key[:12] + "..." if key and len(key) > 12 else key,
    }


def normalize_process_env() -> None:
    """Mirror legacy env spelling to the canonical name for child processes."""
    if os.environ.get(CANONICAL_SWARMSYNC_KEY):
        return
    for legacy_key in LEGACY_SWARMSYNC_KEYS:
        value = os.environ.get(legacy_key)
        if value:
            os.environ[CANONICAL_SWARMSYNC_KEY] = value
            return
