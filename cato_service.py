"""
Cato Windows Service — installs the Cato daemon as a persistent background service.

Install:  python cato_service.py install
Start:    python cato_service.py start
Stop:     python cato_service.py stop
Remove:   python cato_service.py remove
"""

import sys
import os
import threading
import servicemanager
import win32event
import win32service
import win32serviceutil

# Set working directory before anything else
os.chdir(r"C:\Users\Administrator\Desktop\Cato")
# Vault password must be set in the environment before installing/starting the service.
# Example: set CATO_VAULT_PASSWORD=your-strong-password
_vault_pw = os.environ.get("CATO_VAULT_PASSWORD")
if not _vault_pw:
    print("[CATO] ERROR: CATO_VAULT_PASSWORD environment variable is not set.")
    print("[CATO] Set it before running: set CATO_VAULT_PASSWORD=<your-strong-password>")
    sys.exit(1)


class CatoDaemonService(win32serviceutil.ServiceFramework):
    _svc_name_ = "CatoDaemon"
    _svc_display_name_ = "Cato AI Daemon"
    _svc_description_ = (
        "Cato privacy-focused AI agent daemon — HTTP 8080, WS 8081, Telegram bot"
    )

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._thread = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        if self._thread:
            self._thread.join(timeout=10)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._thread = threading.Thread(target=self._run_daemon, daemon=True)
        self._thread.start()
        # Wait until stop signal
        win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

    def _run_daemon(self):
        import asyncio
        import logging

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            filename=r"C:\Users\Administrator\AppData\Roaming\cato\cato_service.log",
        )

        # Load .env
        try:
            from dotenv import load_dotenv
            load_dotenv(r"C:\Users\Administrator\Desktop\Cato\.env")
        except ImportError:
            pass

        sys.path.insert(0, r"C:\Users\Administrator\Desktop\Cato")
        from cato.cli import CatoConfig, Vault, BudgetManager, _CATO_DIR, _run_daemon, _PID_FILE

        vault_path = _CATO_DIR / "vault.enc"
        vault = Vault(vault_path=vault_path) if vault_path.exists() else None
        config = CatoConfig.load()
        budget = BudgetManager(session_cap=config.session_cap, monthly_cap=config.monthly_cap)

        if _PID_FILE.exists():
            _PID_FILE.unlink(missing_ok=True)
        import os as _os
        _PID_FILE.write_text(str(_os.getpid()))

        try:
            _run_daemon(config, "claude", "all")
        finally:
            _PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(CatoDaemonService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(CatoDaemonService)
