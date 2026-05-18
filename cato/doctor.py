"""
cato/doctor.py — Workspace health auditor for CATO.

Invoked by `cato doctor` (defined in cli.py).

Checks performed:
  1. Config file exists and parses as valid YAML
  2. Vault file is present (decryption not attempted — avoids password prompt)
  3. Per-agent workspace files: token counts vs. recommended limits
     SOUL.md:   warn if > 800 tokens
     AGENTS.md: warn if > 1500 tokens
     USER.md:   warn if > 500 tokens
     MEMORY.md: warn if > 1000 tokens
     (Other .md files: warn if > 600 tokens each)
  4. Budget status (monthly spent vs. cap)
  5. Active sessions (PID file)
  6. Telegram / WhatsApp configured
  7. Patchright / Chromium available
  8. Vault keys listed (count only — no values shown)
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from cato.budget import BudgetManager
from cato.config import CatoConfig
from cato.platform import get_data_dir
from cato.swarmsync import swarmsync_key_status

console = Console()

def _cato_dir() -> Path:
    """Canonical Cato data directory (Windows: %APPDATA%/cato, POSIX: ~/.cato)."""
    return get_data_dir()

_PID_FILE = _cato_dir() / "cato.pid"
_PORT_FILE = _cato_dir() / "cato.port"

# Recommended token ceilings per workspace file
_TOKEN_LIMITS: dict[str, int] = {
    "SOUL.md": 800,
    "AGENTS.md": 1500,
    "USER.md": 500,
    "MEMORY.md": 1000,
    "IDENTITY.md": 600,
    "TOOLS.md": 600,
    "HEARTBEAT.md": 400,
}
_DEFAULT_LIMIT = 600          # applied to any .md not in the table above
_CONTEXT_BUDGET = 7000        # total bootstrap context tokens available

# Cache the tiktoken encoding at module level so it is only loaded once.
try:
    import tiktoken as _tiktoken
    _ENC = _tiktoken.get_encoding("cl100k_base")
except Exception:
    _tiktoken = None  # type: ignore[assignment]
    _ENC = None


def _count_tokens(text: str) -> int:
    """Approximate token count using tiktoken cl100k_base encoding."""
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:
            pass
    # Fallback: ~4 chars per token
    return max(1, len(text) // 4)


class DoctorReport:
    """
    Audits the Cato workspace and prints a structured health report.

    Parameters
    ----------
    agent_id:
        When given, restrict the workspace audit to this agent only.
    """

    def __init__(self, agent_id: Optional[str] = None) -> None:
        self.agent_id = agent_id
        self._config: Optional[CatoConfig] = None
        self._failures: list[tuple[str, str]] = []

    def _fail(self, problem: str, fix: str) -> None:
        self._failures.append((problem, fix))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, agent_id: Optional[str] = None) -> None:
        """Run all health checks and print the report."""
        if agent_id:
            self.agent_id = agent_id

        console.print("\n[bold cyan]Cato Doctor[/bold cyan]")
        console.print("=" * 54)

        self._check_config()
        self._check_vault()
        self._check_workspaces()
        self._check_budget()
        self._check_daemon()
        self._check_desktop_launcher()
        self._check_swarmsync_key_normalization()
        self._check_routing_status()
        self._check_channels()
        self._check_browser()
        self._print_failure_summary()
        console.print()

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_config(self) -> None:
        """Check 1: config file exists and is valid YAML."""
        console.print("\n[bold]Config[/bold]")
        data_dir = _cato_dir()
        config_path = data_dir / "config.yaml"
        # Warn if legacy path exists (e.g. ~/.cato on Windows when we use %APPDATA%\cato)
        legacy = Path.home() / ".cato"
        if legacy.exists() and data_dir != legacy and (legacy / "config.yaml").exists():
            console.print(f"  [yellow]LEGACY PATH[/yellow] — config also at {legacy}; current data dir: {data_dir}")
        if not config_path.exists():
            console.print("  [yellow]NOT FOUND[/yellow] — run 'cato init' to create config")
            self._fail("Config file is missing", "Run: cato init")
            return
        try:
            self._config = CatoConfig.load(config_path)
            console.print(f"  [green]OK[/green] — {config_path}")
            console.print(f"     model: {self._config.default_model}")
            console.print(f"     monthly cap: ${self._config.monthly_cap:.2f}"
                          f"  |  session cap: ${self._config.session_cap:.2f}")
        except Exception as exc:
            console.print(f"  [red]INVALID[/red] — {exc}")
            self._fail("Config file is invalid", f"Fix YAML at {config_path}: {exc}")

    def _check_vault(self) -> None:
        """Check 2: vault file is present."""
        console.print("\n[bold]Vault[/bold]")
        vault_path = _cato_dir() / "vault.enc"
        if vault_path.exists():
            size_kb = vault_path.stat().st_size / 1024
            console.print(
                f"  [green]OK[/green] — {vault_path}  ({size_kb:.1f} KB)"
            )
        else:
            console.print(
                "  [yellow]NOT FOUND[/yellow] — run 'cato init' to create vault"
            )
            self._fail("Vault is missing", "Run: cato init, then store SWARMSYNC_API_KEY in the vault")

    def _check_workspaces(self) -> None:
        """Check 3: per-agent workspace file token audit."""
        console.print("\n[bold]Workspace Token Audit[/bold]")
        data_dir = _cato_dir()
        agents_dir = data_dir / "agents"
        if not agents_dir.exists():
            console.print("  [yellow]No agents directory found[/yellow]")
            return

        agent_dirs: list[Path] = sorted(
            d for d in agents_dir.iterdir()
            if d.is_dir() and (self.agent_id is None or d.name == self.agent_id)
        )
        if not agent_dirs:
            label = f"'{self.agent_id}'" if self.agent_id else "any"
            console.print(f"  [yellow]No agent workspace found for {label}[/yellow]")
            return

        for agent_dir in agent_dirs:
            self._audit_agent_workspace(agent_dir)

    def _audit_agent_workspace(self, agent_dir: Path) -> None:
        """Print a token table for one agent's workspace."""
        table = Table(
            title=f"Agent: {agent_dir.name}",
            show_lines=True,
            show_footer=True,
        )
        table.add_column("File", style="cyan")
        table.add_column("Tokens", justify="right")
        table.add_column("Limit", justify="right")
        table.add_column("Status")

        total_tokens = 0
        md_files = sorted(agent_dir.glob("*.md"))

        for md in md_files:
            try:
                content = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            tokens = _count_tokens(content)
            total_tokens += tokens
            limit = _TOKEN_LIMITS.get(md.name, _DEFAULT_LIMIT)
            if tokens > limit:
                status = f"[red]OVER LIMIT[/red]  (trim by {tokens - limit} tokens)"
            else:
                status = "[green]OK[/green]"
            table.add_row(md.name, str(tokens), str(limit), status)

        # Summary row
        bootstrap_pct = int(total_tokens / _CONTEXT_BUDGET * 100)
        pct_color = "red" if bootstrap_pct > 90 else ("yellow" if bootstrap_pct > 70 else "green")
        table.add_row(
            "[bold]TOTAL[/bold]",
            f"[bold]{total_tokens}[/bold]",
            str(_CONTEXT_BUDGET),
            f"[{pct_color}]{bootstrap_pct}% of context budget[/{pct_color}]",
        )

        console.print(table)

        # Cost hint: at 150 output tokens per reply, bootstrap is a fixed overhead
        cost_per_1k = (total_tokens / 1_000_000) * 3.00 * 1000   # sonnet-4-6 input rate
        console.print(
            f"  Estimated bootstrap cost at sonnet-4-6: "
            f"${cost_per_1k:.4f} per 1,000 messages\n"
        )

    def _check_budget(self) -> None:
        """Check 4: budget status."""
        console.print("[bold]Budget[/bold]")
        try:
            cfg = self._config
            bm = BudgetManager(
                session_cap=cfg.session_cap if cfg else 1.00,
                monthly_cap=cfg.monthly_cap if cfg else 20.00,
            )
            status = bm.get_status()
            monthly_color = "red" if status["monthly_pct_remaining"] < 20 else "green"
            console.print(
                f"  Monthly:  ${status['monthly_spend']:.4f} / ${status['monthly_cap']:.2f}"
                f"  [{monthly_color}]({status['monthly_pct_remaining']:.0f}% remaining)[/{monthly_color}]"
            )
            console.print(
                f"  Session:  ${status['session_spend']:.4f} / ${status['session_cap']:.2f}"
            )
            console.print(f"  All-time: ${status['total_spend_all_time']:.4f}")
            console.print(f"  Calls this month: {status['monthly_calls']}")
        except Exception as exc:
            console.print(f"  [red]Could not read budget: {exc}[/red]")

    def _check_daemon(self) -> None:
        """Check 5: PID/port liveness and /health."""
        console.print("\n[bold]Daemon[/bold]")
        pid: int | None = None
        if _PID_FILE.exists():
            raw_pid = _PID_FILE.read_text().strip()
            try:
                pid = int(raw_pid)
            except ValueError:
                console.print(f"  [red]STALE PID[/red]  invalid pid file: {_PID_FILE}")
                self._fail("Invalid daemon pid file", f"Delete {_PID_FILE} and restart Cato")
            else:
                if self._pid_alive(pid):
                    console.print(f"  [green]PID OK[/green]  {pid}")
                else:
                    console.print(f"  [red]STALE PID[/red]  PID {pid} is not running")
                    self._fail("Stale daemon pid file", f"Delete {_PID_FILE} and {_PORT_FILE}, then run: cato start --channel webchat")
        else:
            console.print("  [dim]STOPPED[/dim]  (run 'cato start' to launch)")
            self._fail("Daemon is not running", "Run: cato start --channel webchat")

        port = self._read_port()
        if port is None:
            cfg_port = getattr(self._config, "webchat_port", 8080) if self._config else 8080
            port = int(cfg_port or 8080)
            console.print(f"  [dim]Port file missing[/dim] — checking configured port {port}")
        elif not self._port_open(port):
            console.print(f"  [red]PORT CLOSED[/red]  cato.port says {port}, but nothing is listening")
            self._fail("Stale daemon port file", f"Delete {_PORT_FILE} and restart with: cato start --channel webchat")

        health = self._get_json(f"http://127.0.0.1:{port}/health", timeout=2)
        if health.get("ok"):
            payload = health.get("json", {})
            console.print(f"  [green]/health OK[/green]  http://127.0.0.1:{port}/health status={payload.get('status')}")
        else:
            error = health.get("error", "unknown error")
            console.print(f"  [red]/health FAIL[/red]  http://127.0.0.1:{port}/health — {error}")
            self._fail("Daemon /health is unavailable", f"Start or restart daemon, then verify: curl http://127.0.0.1:{port}/health")

    def _check_desktop_launcher(self) -> None:
        """Check current desktop launcher script and release exe paths."""
        console.print("\n[bold]Desktop Launcher[/bold]")
        repo_root = Path(__file__).resolve().parents[1]
        launcher = repo_root / "Launch-CatoDesktop.ps1"
        exe = repo_root / "desktop" / "src-tauri" / "target" / "release" / "cato-desktop.exe"
        shortcut = Path.home() / "Desktop" / "Cato.lnk"
        if launcher.exists():
            console.print(f"  [green]Launcher OK[/green]  {launcher}")
        else:
            console.print(f"  [red]Launcher missing[/red]  {launcher}")
            self._fail("Desktop launcher script is missing", f"Restore {launcher} or run desktop\\build_release.ps1")
        if exe.exists():
            console.print(f"  [green]Desktop exe OK[/green]  {exe}")
        else:
            console.print(f"  [red]Desktop exe missing[/red]  {exe}")
            self._fail("Desktop executable is missing", "Run: powershell -ExecutionPolicy Bypass -File desktop\\build_release.ps1")
        if shortcut.exists():
            console.print(f"  [green]Shortcut present[/green]  {shortcut}")
        else:
            console.print(f"  [yellow]Shortcut missing[/yellow]  {shortcut}")
            self._fail("Desktop shortcut is missing", "Run: powershell -ExecutionPolicy Bypass -File desktop\\build_release.ps1")

    def _check_swarmsync_key_normalization(self) -> None:
        """Check SwarmSync key aliases that have caused empty-response confusion."""
        console.print("\n[bold]SwarmSync Key Normalization[/bold]")
        cfg_enabled = bool(getattr(self._config, "swarmsync_enabled", False)) if self._config else False
        env_keys = self._read_env_keys()
        env_new = env_keys.get("SWARMSYNC_API_KEY") or os.environ.get("SWARMSYNC_API_KEY")
        env_legacy = env_keys.get("SWARM_SYNC_API_KEY") or os.environ.get("SWARM_SYNC_API_KEY")
        vault_new = vault_legacy = None
        key_status: dict[str, object] = {"present": False, "source": "", "needs_normalization": False}
        if os.environ.get("CATO_VAULT_PASSWORD"):
            try:
                from cato.vault import get_vault
                vault = get_vault()
                key_status = swarmsync_key_status(vault)
                vault_new = vault.get("SWARMSYNC_API_KEY")
                vault_legacy = vault.get("SWARM_SYNC_API_KEY")
            except Exception as exc:
                console.print(f"  [yellow]Vault key check skipped[/yellow]  {exc}")

        has_key = bool(env_new or env_legacy or vault_new or vault_legacy)
        console.print(f"  swarmsync_enabled: {'true' if cfg_enabled else 'false'}")
        console.print(f"  SWARMSYNC_API_KEY present: {'yes' if bool(env_new or vault_new) else 'no'}")
        console.print(f"  legacy SWARM_SYNC_API_KEY present: {'yes' if bool(env_legacy or vault_legacy) else 'no'}")
        if key_status.get("present"):
            console.print(f"  normalized source: {key_status.get('source')}")
        if cfg_enabled and not has_key:
            self._fail("SwarmSync is enabled but no key was found", "Set SWARMSYNC_API_KEY in the vault or root .env; do not rely on OpenRouter for routed calls")
        if key_status.get("needs_normalization") or ((env_legacy or vault_legacy) and not (env_new or vault_new)):
            self._fail("Only legacy SWARM_SYNC_API_KEY is present", "Normalize to SWARMSYNC_API_KEY in the vault or root .env")

    def _check_routing_status(self) -> None:
        """Check /api/routing/status from the running daemon."""
        console.print("\n[bold]Routing Status API[/bold]")
        port = self._read_port() or int(getattr(self._config, "webchat_port", 8080) if self._config else 8080)
        token = self._read_daemon_token()
        result = self._get_json(
            f"http://127.0.0.1:{port}/api/routing/status",
            timeout=15,
            headers={"X-Cato-Token": token} if token else None,
        )
        if not result.get("ok"):
            console.print(f"  [red]FAIL[/red]  {result.get('error')}")
            self._fail("/api/routing/status is unavailable", f"Restart daemon and check: curl http://127.0.0.1:{port}/api/routing/status")
            return
        data = result.get("json", {})
        live = data.get("live_test") or {}
        console.print(f"  will_use_swarmsync: {data.get('will_use_swarmsync')}")
        console.print(f"  key present: {data.get('swarm_key_present')}")
        if live.get("routed_model"):
            console.print(f"  [green]live routed model[/green]  {live.get('routed_model')}")
            console.print(f"  reason: {live.get('routing_reason') or '(not returned)'}")
            console.print(f"  tier: {live.get('tier') or '(not returned)'}")
        elif live.get("error"):
            console.print(f"  [red]live test error[/red]  {live.get('error')}")
            self._fail("SwarmSync live routing test failed", "Fix SWARMSYNC_API_KEY/connectivity, then re-run: cato doctor")

    def _print_failure_summary(self) -> None:
        console.print("\n[bold]Failure Summary[/bold]")
        if not self._failures:
            console.print("  [green]No blocking failures detected[/green]")
            return
        for idx, (problem, fix) in enumerate(self._failures, start=1):
            console.print(f"  [red]{idx}. {problem}[/red]")
            console.print(f"     Fix: {fix}")

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _read_port() -> int | None:
        if not _PORT_FILE.exists():
            return None
        try:
            port = int(_PORT_FILE.read_text().strip())
            if 0 < port <= 65535:
                return port
        except (OSError, ValueError):
            return None
        return None

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False

    @staticmethod
    def _get_json(url: str, timeout: int, headers: Optional[dict[str, str]] = None) -> dict[str, object]:
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(1_000_000).decode("utf-8", errors="replace")
                return {"ok": 200 <= resp.status < 300, "status": resp.status, "json": json.loads(raw)}
        except urllib.error.HTTPError as exc:
            body = exc.read(500).decode("utf-8", errors="replace")
            return {"ok": False, "status": exc.code, "error": body or str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _read_daemon_token() -> str:
        try:
            return (_cato_dir() / "daemon.token").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @staticmethod
    def _read_env_keys() -> dict[str, str]:
        repo_root = Path(__file__).resolve().parents[1]
        keys: dict[str, str] = {}
        for path in (repo_root / ".env", _cato_dir() / ".env"):
            if not path.exists():
                continue
            try:
                for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    keys[key.strip()] = value.strip().strip("\"'")
            except OSError:
                continue
        return keys

    def _check_channels(self) -> None:
        """Check 6: Telegram / WhatsApp configured."""
        console.print("\n[bold]Channels[/bold]")
        cfg = self._config
        if cfg is None:
            console.print("  [yellow]Config not loaded — skipping channel check[/yellow]")
            return

        tg_status = "[green]enabled[/green]" if cfg.telegram_enabled else "[dim]disabled[/dim]"
        wa_status = "[green]enabled[/green]" if cfg.whatsapp_enabled else "[dim]disabled[/dim]"
        console.print(f"  Telegram: {tg_status}")
        console.print(f"  WhatsApp: {wa_status}")
        console.print(f"  WebChat:  port {cfg.webchat_port}")

    def _check_browser(self) -> None:
        """Check 7: Patchright / Chromium available."""
        console.print("\n[bold]Browser (Patchright)[/bold]")
        patchright_cli = shutil.which("patchright")
        chromium = shutil.which("chromium") or shutil.which("chromium-browser")

        if patchright_cli:
            console.print(f"  [green]patchright[/green]  — {patchright_cli}")
        else:
            try:
                import patchright  # noqa: F401  — importable is enough
                console.print("  [green]patchright[/green]  — installed (Python package)")
            except ImportError:
                console.print(
                    "  [yellow]patchright not found[/yellow]  — "
                    "install with: pip install patchright"
                )

        if chromium:
            console.print(f"  [green]chromium[/green]   — {chromium}")
        else:
            console.print(
                "  [dim]chromium not found in PATH[/dim]  — "
                "browser tools may auto-download via Playwright"
            )
