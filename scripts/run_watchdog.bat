@echo off
cd /d C:\Users\Administrator\Desktop\Cato
REM CATO_VAULT_PASSWORD must be set in the environment before running this script.
REM Example: set CATO_VAULT_PASSWORD=your-strong-password
REM Do NOT hardcode the password in this file.
if "%CATO_VAULT_PASSWORD%"=="" (
    echo [CATO] ERROR: CATO_VAULT_PASSWORD environment variable is not set.
    echo [CATO] Set it first: set CATO_VAULT_PASSWORD=your-strong-password
    exit /b 1
)
"C:\Program Files\Python312\python.exe" scripts\watchdog.py >> C:\Users\Administrator\AppData\Roaming\cato\watchdog_task.log 2>&1
