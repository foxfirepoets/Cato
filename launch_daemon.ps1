# Cato Daemon Launcher
# Run this from PowerShell to start the Cato daemon in its own window

# CATO_VAULT_PASSWORD must be set in the environment before running this script.
# Example: $env:CATO_VAULT_PASSWORD = "your-strong-password-here"
# Do NOT hardcode the password in this file.
if (-not $env:CATO_VAULT_PASSWORD) {
    Write-Host "[CATO] ERROR: CATO_VAULT_PASSWORD environment variable is not set." -ForegroundColor Red
    Write-Host "[CATO] Set it first: `$env:CATO_VAULT_PASSWORD = 'your-strong-password'" -ForegroundColor Yellow
    exit 1
}
Set-Location "C:\Users\Administrator\Desktop\Cato"

# Remove stale PID file
$pidFile = "$env:APPDATA\cato\cato.pid"
if (Test-Path $pidFile) { Remove-Item $pidFile -Force }

Write-Host "Starting Cato daemon..."
python -c @"
import os, sys, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
os.chdir(r'C:\Users\Administrator\Desktop\Cato')
sys.path.insert(0, r'C:\Users\Administrator\Desktop\Cato')
from cato.cli import CatoConfig, Vault, BudgetManager, _CATO_DIR, _run_daemon, _PID_FILE, setup_signal_handlers
import os as _os

vault_path = _CATO_DIR / 'vault.enc'
vault = Vault(vault_path=vault_path) if vault_path.exists() else None
config = CatoConfig.load()
budget = BudgetManager(session_cap=config.session_cap, monthly_cap=config.monthly_cap)
if _PID_FILE.exists(): _PID_FILE.unlink()
_PID_FILE.write_text(str(_os.getpid()))
def _shutdown(): _PID_FILE.unlink(missing_ok=True)
setup_signal_handlers(_shutdown)
try:
    _run_daemon(config, 'claude', 'all')
finally:
    _PID_FILE.unlink(missing_ok=True)
"@
