@echo off
REM CATO_VAULT_PASSWORD must be set in the environment before running this script.
REM Example: set CATO_VAULT_PASSWORD=your-strong-password
REM Do NOT hardcode the password in this file.
if "%CATO_VAULT_PASSWORD%"=="" (
    echo [CATO] ERROR: CATO_VAULT_PASSWORD environment variable is not set.
    echo [CATO] Set it first: set CATO_VAULT_PASSWORD=your-strong-password
    exit /b 1
)
cd /d C:\Users\Administrator\Desktop\Cato
python -c "
import os, sys, logging
logging.basicConfig(level=logging.INFO, format='%%(asctime)s %%(name)s %%(levelname)s %%(message)s')
os.chdir(r'C:\Users\Administrator\Desktop\Cato')
from cato.cli import CatoConfig, Vault, BudgetManager, _CATO_DIR, _run_daemon, safe_print, _PID_FILE, setup_signal_handlers
from pathlib import Path
import os as _os

vault_path = _CATO_DIR / 'vault.enc'
vault = Vault(vault_path=vault_path) if vault_path.exists() else None
config = CatoConfig.load()
budget = BudgetManager(session_cap=config.session_cap, monthly_cap=config.monthly_cap)

if _PID_FILE.exists():
    _PID_FILE.unlink()

_PID_FILE.write_text(str(_os.getpid()))

def _shutdown():
    _PID_FILE.unlink(missing_ok=True)

setup_signal_handlers(_shutdown)
try:
    _run_daemon(config, 'claude', 'all')
finally:
    _PID_FILE.unlink(missing_ok=True)
"
