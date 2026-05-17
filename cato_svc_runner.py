"""Minimal runner script for the Cato daemon — used by Task Scheduler / NSSM."""
import io
import logging
import os
import sys
from pathlib import Path

os.chdir(r"C:\Users\Administrator\Desktop\Cato")
sys.path.insert(0, r"C:\Users\Administrator\Desktop\Cato")

_DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "cato"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_DAEMON_LOG = _DATA_DIR / "daemon_runner.log"

# Vault password — baked in so no env var needed when run as SYSTEM
os.environ.setdefault("CATO_VAULT_PASSWORD", "mypassword123")

# Hidden/background launches on Windows can have no real stdout/stderr.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # type: ignore[assignment]
elif getattr(sys.stdout, "closed", False):
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # type: ignore[assignment]

if sys.stderr is None:
    sys.stderr = open(_DAEMON_LOG, "a", encoding="utf-8")  # type: ignore[assignment]
elif getattr(sys.stderr, "closed", False):
    sys.stderr = open(_DAEMON_LOG, "a", encoding="utf-8")  # type: ignore[assignment]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(_DAEMON_LOG, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

try:
    from dotenv import load_dotenv
    load_dotenv(r"C:\Users\Administrator\Desktop\Cato\.env")
except ImportError:
    pass

from cato.cli import CatoConfig, Vault, BudgetManager, _CATO_DIR, _run_daemon, _PID_FILE, _read_live_pid

vault_path = _CATO_DIR / "vault.enc"
vault = Vault(vault_path=vault_path) if vault_path.exists() else None
config = CatoConfig.load()
budget = BudgetManager(session_cap=config.session_cap, monthly_cap=config.monthly_cap)

# BH-010 — Propagate config.workspace_dir to the file/shell/python tools via
# an env var.  The tools resolve their workspace root at call time from
# `CATO_WORKSPACE_DIR` if set (see cato/tools/file.py etc).  Without this
# bridge the tools fall back to ~/.cato/workspace even when config points
# elsewhere, which silently breaks the workspace_dir setting.
if getattr(config, "workspace_dir", None):
    os.environ["CATO_WORKSPACE_DIR"] = str(config.workspace_dir)

live_pid = _read_live_pid()
if live_pid is not None and live_pid != os.getpid():
    logging.info("Cato daemon already running; runner exiting.")
    sys.exit(0)
_PID_FILE.write_text(str(os.getpid()))

try:
    _run_daemon(config, "claude", "all")
finally:
    _PID_FILE.unlink(missing_ok=True)
