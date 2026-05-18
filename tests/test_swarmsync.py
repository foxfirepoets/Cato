from __future__ import annotations

from cato.swarmsync import get_swarmsync_api_key, normalize_process_env, swarmsync_key_status


class DummyVault:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str) -> str:
        return self.values.get(key, "")


def test_swarmsync_key_prefers_canonical_vault() -> None:
    key, source = get_swarmsync_api_key(
        DummyVault({"SWARMSYNC_API_KEY": "canonical", "SWARM_SYNC_API_KEY": "legacy"})
    )

    assert key == "canonical"
    assert source == "SWARMSYNC_API_KEY"


def test_swarmsync_key_accepts_legacy_vault() -> None:
    status = swarmsync_key_status(DummyVault({"SWARM_SYNC_API_KEY": "legacy"}))

    assert status["present"] is True
    assert status["source"] == "SWARM_SYNC_API_KEY"
    assert status["needs_normalization"] is True


def test_normalize_process_env_copies_legacy(monkeypatch) -> None:
    monkeypatch.delenv("SWARMSYNC_API_KEY", raising=False)
    monkeypatch.setenv("SWARM_SYNC_API_KEY", "legacy-env")

    normalize_process_env()

    assert get_swarmsync_api_key()[0] == "legacy-env"
    assert get_swarmsync_api_key()[1] == "env:SWARMSYNC_API_KEY"
