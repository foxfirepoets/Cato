"""
install_autostart.py
Registers the Telegram bridge to auto-start on Windows login
using the HKCU Run registry key — no admin rights needed.

Also sets CATO_VAULT_PASSWORD in the user environment registry.

Run once:
    python install_autostart.py
    python install_autostart.py --remove   (to uninstall)
"""

import argparse
import os
import sys
import winreg

BRIDGE_SCRIPT  = r"C:\Users\Administrator\Desktop\Cato\cato_telegram_bridge.py"
PYTHON_EXE     = r"C:\Python313\python.exe"
LOG_FILE       = r"C:\Users\Administrator\Desktop\Cato\logs\telegram_bridge.log"
REG_RUN_KEY    = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME      = "CatoTelegramBridge"
BOT_TOKEN      = os.environ.get("CATODESKTOP_BOT_TOKEN", "")
VAULT_PASSWORD = os.environ.get("CATO_VAULT_PASSWORD", "")
# Wraps in cmd /c with env vars set inline, stdout+stderr redirected to log
CMD = (
    f'cmd.exe /c "set TELEGRAM_BOT_TOKEN={BOT_TOKEN}'
    f' && set CATO_VAULT_PASSWORD={VAULT_PASSWORD}'
    f' && \\"{PYTHON_EXE}\\" \\"{BRIDGE_SCRIPT}\\" >> \\"{LOG_FILE}\\" 2>&1"'
)


def install():
    if not BOT_TOKEN:
        print("[autostart] ERROR: CATODESKTOP_BOT_TOKEN environment variable is not set.")
        print("[autostart] Set it before running: set CATODESKTOP_BOT_TOKEN=<your-bot-token>")
        sys.exit(1)
    if not VAULT_PASSWORD:
        print("[autostart] ERROR: CATO_VAULT_PASSWORD environment variable is not set.")
        print("[autostart] Set it before running: set CATO_VAULT_PASSWORD=<your-strong-password>")
        sys.exit(1)
    # Ensure log dir exists
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    # 1. Register in HKCU\...\Run  (runs at every user login, no admin needed)
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, TASK_NAME, 0, winreg.REG_SZ, CMD)
    print(f"[autostart] Registered in HKCU Run: {TASK_NAME}")

    # 2. Persist credentials in user environment (survives reboots)
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Environment",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, "CATO_VAULT_PASSWORD", 0, winreg.REG_SZ, VAULT_PASSWORD)
        winreg.SetValueEx(key, "TELEGRAM_BOT_TOKEN",  0, winreg.REG_SZ, BOT_TOKEN)
    print("[autostart] CATO_VAULT_PASSWORD + TELEGRAM_BOT_TOKEN written to user Environment registry")

    print()
    print("Done. The Telegram bridge will start automatically on next login.")
    print(f"Log file: {LOG_FILE}")
    print()
    print("To start it right now without rebooting, run:")
    print(f"  .\\start_telegram_bridge.ps1")


def remove():
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, TASK_NAME)
        print(f"[autostart] Removed from HKCU Run: {TASK_NAME}")
    except FileNotFoundError:
        print(f"[autostart] Not found in registry: {TASK_NAME}")

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, "CATO_VAULT_PASSWORD")
        print("[autostart] Removed CATO_VAULT_PASSWORD from user Environment")
    except FileNotFoundError:
        print("[autostart] CATO_VAULT_PASSWORD not found in registry")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--remove", action="store_true", help="Unregister autostart")
    args = parser.parse_args()

    if args.remove:
        remove()
    else:
        install()
