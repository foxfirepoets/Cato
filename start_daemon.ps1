# CATO_VAULT_PASSWORD must be set in the environment before running this script.
# Example: $env:CATO_VAULT_PASSWORD = "your-strong-password-here"
# Do NOT hardcode the password in this file.
if (-not $env:CATO_VAULT_PASSWORD) {
    Write-Host "[CATO] ERROR: CATO_VAULT_PASSWORD environment variable is not set." -ForegroundColor Red
    Write-Host "[CATO] Set it first: `$env:CATO_VAULT_PASSWORD = 'your-strong-password'" -ForegroundColor Yellow
    exit 1
}
Start-Process -FilePath python -ArgumentList 'cato_svc_runner.py' -WorkingDirectory 'C:\Users\Administrator\Desktop\Cato' -WindowStyle Hidden
Write-Host "Daemon launched"
