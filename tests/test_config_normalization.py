from __future__ import annotations

from cato.config import CatoConfig


def test_load_normalizes_legacy_string_scalars(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "swarmsync_enabled: 'false'",
                "telegram_enabled: 'true'",
                "monthly_cap: '42.50'",
                "webchat_port: '8091'",
                "enabled_models: 'claude, cursor, gemini'",
                "vault: 'null'",
            ]
        ),
        encoding="utf-8",
    )

    cfg = CatoConfig.load(config_path=config_path)

    assert cfg.swarmsync_enabled is False
    assert cfg.telegram_enabled is True
    assert cfg.monthly_cap == 42.50
    assert cfg.webchat_port == 8091
    assert cfg.enabled_models == ["claude", "cursor", "gemini"]
    assert cfg.vault is None


def test_load_ignores_nested_legacy_config_block(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "agent_name: top-level",
                "config:",
                "  agent_name: nested-legacy",
                "  telegram_enabled: 'true'",
            ]
        ),
        encoding="utf-8",
    )

    cfg = CatoConfig.load(config_path=config_path)

    assert cfg.agent_name == "top-level"
    assert cfg.telegram_enabled is False
