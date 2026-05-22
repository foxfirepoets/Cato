# AGENTS.md — Desktop App Ralph Loop

## Build & Run
- Backend tests: `cd C:/Users/Administrator/Desktop/Cato && python -m pytest tests/ -x --tb=short`
- Frontend build: `cd C:/Users/Administrator/Desktop/Cato/desktop && npm run build`
- Frontend dev: `cd C:/Users/Administrator/Desktop/Cato/desktop && npm run dev`
- Daemon: `cd C:/Users/Administrator/Desktop/Cato && CATO_VAULT_PASSWORD=<your-vault-password> python cato_svc_runner.py`

## Validation Commands
```bash
cd C:/Users/Administrator/Desktop/Cato && python -m pytest tests/ -x --tb=short && cd desktop && npm run build
```

## Codebase Patterns
- Python backend: `cato/ui/server.py` (aiohttp routes)
- React views: `desktop/src/views/*.tsx`
- React hooks: `desktop/src/hooks/*.ts`
- Styles: `desktop/src/styles/app.css`
- Main app: `desktop/src/App.tsx`

## Gotchas
- CORS has TWO locations in server.py (lines 75 and 81)
- Desktop uses port 8080 for HTTP, port 8081 for gateway WS
- Tests are in `tests/` — 1285+ tests, ALL must pass
- AuthKeysView has hardcoded CLI statuses — must be replaced with live fetch
- Config endpoint leaks secrets — must filter before returning
