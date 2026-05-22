# AGENTS.md — Web UI Ralph Loop

## Build & Run
- Backend tests: `cd C:/Users/Administrator/Desktop/Cato && python -m pytest tests/ -x --tb=short`
- Daemon: `cd C:/Users/Administrator/Desktop/Cato && CATO_VAULT_PASSWORD=<your-vault-password> python cato_svc_runner.py`
- Web UI: `http://127.0.0.1:8080/` (served by aiohttp daemon)

## Validation Commands
```bash
cd C:/Users/Administrator/Desktop/Cato && python -m pytest tests/ -x --tb=short
```

## Codebase Patterns
- Web UI: `cato/ui/dashboard.html` (monolithic 1700+ line SPA)
- Backend: `cato/ui/server.py` (aiohttp routes)
- Coding agent page: `cato/ui/coding_agent.html`

## Gotchas
- dashboard.html is ONE file — be careful with edits
- Onboarding overlay blocks ALL nav clicks — fix this FIRST
- Bot name "AI" in 3 locations — search, don't assume line numbers
- Backend fixes from Desktop loop already applied — don't redo
