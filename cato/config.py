"""
cato/config.py — Configuration management for CATO.

Loads and saves ~/.cato/config.yaml with defaults for all known fields.
First-run detection: returns defaults when the config file does not yet exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

import yaml

from .platform import get_data_dir

_CONFIG_FILE = get_data_dir() / "config.yaml"
logger = logging.getLogger(__name__)


_TRUE_STRINGS = {"1", "true", "yes", "y", "on"}
_FALSE_STRINGS = {"0", "false", "no", "n", "off"}
_NULL_STRINGS = {"null", "none", "~"}


def _normalize_config_value(value: Any, default: Any) -> Any:
    """Coerce legacy YAML string scalars to the type used by the config default."""
    if isinstance(value, str):
        stripped = value.strip()
        lowered = stripped.lower()
        if lowered in _NULL_STRINGS:
            return None

        if isinstance(default, bool):
            if lowered in _TRUE_STRINGS:
                return True
            if lowered in _FALSE_STRINGS:
                return False
            return value

        if isinstance(default, int) and not isinstance(default, bool):
            try:
                return int(stripped)
            except ValueError:
                return value

        if isinstance(default, float):
            try:
                return float(stripped)
            except ValueError:
                return value

        if isinstance(default, list):
            if lowered in {"", "[]"}:
                return []
            if stripped.startswith("["):
                try:
                    parsed = yaml.safe_load(stripped)
                except yaml.YAMLError:
                    parsed = None
                if isinstance(parsed, list):
                    return parsed
            return [item.strip() for item in stripped.split(",") if item.strip()]

    return value


@dataclass
class CatoConfig:
    """
    Full CATO configuration.

    All fields have safe defaults so CATO works out-of-the-box.
    Persist changes with :meth:`save`.
    """

    # Identity
    agent_name: str = "cato"

    # Model selection (fallback slug — SwarmSync overrides this when enabled)
    default_model: str = "openrouter/minimax/minimax-m2.5"

    # SwarmSync intelligent routing
    swarmsync_enabled: bool = True
    swarmsync_api_url: str = "https://api.swarmsync.ai/v1/chat/completions"

    # Budget caps (USD)
    session_cap: float = 1.00
    monthly_cap: float = 20.00

    # Workspace
    workspace_dir: str = str(get_data_dir() / "workspace")
    pipeline_root_dir: str = str(get_data_dir() / "businesses")

    # Logging
    log_level: str = "INFO"

    # Messaging channels
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    whatsapp_enabled: bool = False
    webchat_port: int = 8080
    mcp_enabled: bool = False
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765
    mcp_mount_path: str = "/mcp"

    # Planning
    max_planning_turns: int = 6
    context_budget_tokens: int = 7000
    max_output_tokens: int = 16384          # max tokens per LLM response
    # BH-009 — Hard cap on the per-message agent-loop run.  This budget covers
    # ALL planning turns + LLM round trips + tool executions for a single
    # inbound message.  When it expires the user sees the "long-running tool
    # call had to abort after N minutes" fallback.  Default 600s (10 min) is
    # generous enough for multi-tool plans against a degraded SwarmSync.  Raise
    # for long-running ops; lower for fast-feedback interactive use.
    gateway_task_timeout_s: float = 600.0

    # Conduit browser engine (opt-in)
    conduit_enabled: bool = False
    conduit_budget_per_session: int = 100   # cents
    conduit_extract_max_chars: int = 20_000
    searxng_url: str = ""
    search_rerank_enabled: bool = False
    conduit_crawl_delay_sec: float = 1.0
    conduit_crawl_max_delay_sec: float = 60.0
    selector_healing_enabled: bool = False
    vault: Optional[dict] = None   # API keys / credentials for search, login, etc.

    # Active model toggles — which CLIs are included in coding-agent fan-out
    enabled_models: list = field(default_factory=lambda: ["claude", "codex", "gemini"])

    # Subagent routing (mirrors OpenClaw's ChatGPT-subagent feature)
    # When enabled, TIER_C coding tasks are delegated to the chosen CLI backend
    # so users can leverage plan-included usage from their preferred provider.
    subagent_enabled: bool = False
    subagent_coding_backend: str = "codex"  # claude | codex | gemini | cursor

    # Safety gates
    safety_mode: str = "strict"             # strict | permissive | off

    # Budget forecast
    budget_forecast_enabled: bool = True    # show cost estimate before tasks

    # Audit log
    audit_enabled: bool = True              # append-only action log

    # Interactive PTY CLI sessions (desktop)
    interactive_cli_enabled: bool = True
    cli_session_cwd: str = ""              # empty = use process cwd
    claude_auth_dir: str = ""
    codex_api_key_env: str = "OPENAI_API_KEY"
    gemini_api_key_env: str = "GEMINI_API_KEY"
    pty_default_cols: int = 80
    pty_default_rows: int = 24
    pty_idle_timeout_sec: int = 0           # 0 = no auto-cleanup

    # Internal — path is excluded from YAML serialisation
    _path: Path = field(default_factory=lambda: _CONFIG_FILE, repr=False, compare=False)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "CatoConfig":
        """
        Load config from *config_path* (default ~/.cato/config.yaml).

        Missing fields fall back to dataclass defaults.
        If the file does not exist the default config is returned (first run).
        """
        path = config_path or _CONFIG_FILE
        instance = cls()
        instance._path = path

        if not path.exists():
            return instance  # first-run defaults

        try:
            raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return instance  # corrupted file — return defaults
        if not isinstance(raw_data, dict):
            logger.warning("Ignoring config file with non-mapping root: %s", path)
            return instance

        # Only set fields that are declared on the dataclass
        field_by_name = {
            f.name: f
            for f in fields(cls)
            if not f.name.startswith("_")
        }
        for key, value in raw_data.items():
            if key == "config" and isinstance(value, dict):
                logger.warning("Ignoring nested legacy 'config' block in %s", path)
                continue
            if key in field_by_name:
                default = getattr(instance, key)
                setattr(instance, key, _normalize_config_value(value, default))

        return instance

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, config_path: Optional[Path] = None) -> None:
        """Write current config to YAML file, creating parent dirs as needed."""
        path = config_path or self._path
        path.parent.mkdir(parents=True, exist_ok=True)

        # Serialise all public fields
        data: dict[str, Any] = {}
        for f in fields(self):
            if not f.name.startswith("_"):
                data[f.name] = getattr(self, f.name)

        path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def workspace_path(self) -> Path:
        """Return :attr:`workspace_dir` as a resolved Path object."""
        return Path(self.workspace_dir).expanduser().resolve()

    def is_first_run(self) -> bool:
        """Return True if no config file exists on disk."""
        return not self._path.exists()

    def get(self, key: str, default: Any = None) -> Any:
        """Vault-style get for API keys (used by WebSearchTool and Conduit login)."""
        if self.vault and isinstance(self.vault, dict):
            return self.vault.get(key, default)
        return default

    def to_conduit_bridge_config(
        self,
        session_id: str,
        data_dir: Optional[str] = None,
        conduit_budget_per_session: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Build config dict for ConduitBridge so bridge _config drives Conduit behavior.

        Use when creating the bridge (e.g. when conduit_enabled)::

            bridge = ConduitBridge(
                cfg.to_conduit_bridge_config(
                    session_id,
                    data_dir=str(get_data_dir()),
                    conduit_budget_per_session=cfg.conduit_budget_per_session,
                ),
                session_id,
            )
        """
        out: dict[str, Any] = {
            "session_id": session_id,
            "conduit_extract_max_chars": self.conduit_extract_max_chars,
            "searxng_url": self.searxng_url or "",
            "search_rerank_enabled": self.search_rerank_enabled,
            "conduit_crawl_delay_sec": self.conduit_crawl_delay_sec,
            "conduit_crawl_max_delay_sec": self.conduit_crawl_max_delay_sec,
            "selector_healing_enabled": self.selector_healing_enabled,
            "vault": self.vault,
        }
        if data_dir is not None:
            out["data_dir"] = data_dir
        if conduit_budget_per_session is not None:
            out["conduit_budget_per_session"] = conduit_budget_per_session
        return out

    def to_dict(self) -> dict[str, Any]:
        """Serialise config to a plain dict (excluding private fields)."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_")
        }
