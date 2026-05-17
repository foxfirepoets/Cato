# CATO — MANDATORY DEVELOPMENT RULES

## ⚠️ ROUTING — READ THIS FIRST, EVERY TIME

**Cato routes ALL LLM calls through SwarmSync. Full stop.**

- `swarmsync_enabled: true` in config.yaml — DO NOT change this
- `SWARMSYNC_API_KEY` is in the root `.env` file AND in the vault
- SwarmSync picks the best model per call — Cato does NOT call OpenRouter directly
- **NEVER assume Cato is broken because of a missing OpenRouter/MiniMax/Anthropic key**
- If Cato returns empty responses, check SwarmSync connectivity first
- The default model (`openrouter/minimax/minimax-m2.5`) is only a fallback slug — SwarmSync overrides it

---

## AUDIT GATE: NOTHING GETS PUSHED TO GITHUB WITHOUT PASSING THIS PIPELINE

Every change — no matter how small — must pass through the audit pipeline before any `git push`:

```
CODE COMPLETE
     |
     v

[1] /HKO-truth-audit — Verification & Reality Check
     - Verify authentic and complete
     - Independently verify test results
     - Implement any additional fixes Kraken deems necessary
     
     |
     v
[2] GIT PUSH — Only after both agents approve
```


## WHAT THIS APPLIES TO

- All Python source changes (`cato/`, `tests/`)
- All frontend changes (`desktop/src/`, `cato/ui/`)
- All configuration changes (`pyproject.toml`, `Cargo.toml`, etc.)
- All new files added to the repo
- ALL commits intended for the `main` branch

## PROJECT OVERVIEW

**Cato** — Privacy-focused AI agent daemon. Alternative to OpenClaw/ClawdBot/MoltBot.
- Package: `cato-daemon` v0.2.0, entry point `cato.cli:main`
- Python 3.11+, asyncio, aiohttp, websockets, patchright, tiktoken, sentence-transformers
- Tauri v2 desktop app (`desktop/`) — React 19 + TypeScript + Rust sidecar
- SQLite memory, YAML config, AES-256-GCM encrypted vault
- **Ports: HTTP 8080, WS 8081** (canonical defaults)
- Live install: `pip install -e .` at `C:\Users\Administrator\Desktop\Cato`

## DAEMON CONFIGURATION

- Config: `%APPDATA%\cato\config.yaml`
- Default model: `openrouter/minimax/minimax-m2.5`
- **workspace_dir**: defaults to `%APPDATA%\cato\workspace` on Windows, `~/.cato/workspace` on macOS/Linux (critical for identity files)
- **swarmsync_enabled: true** — SwarmSync routing is enabled; routes calls to the best model based on complexity
- Vault: `%APPDATA%\cato\vault.enc` — stores `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `SWARMSYNC_API_KEY`
- Vault password: `CATO_VAULT_PASSWORD=mypassword123` (**example only — always choose a unique, strong password in real installs**)
- Run daemon: `CATO_VAULT_PASSWORD=<your-strong-password> python cato_svc_runner.py`
- Health check: `curl http://localhost:8080/health`

## TELEGRAM INTEGRATION (2026-03-09)

- **Status**: ENABLED and bidirectional
- **Bot token**: Stored in encrypted vault as `TELEGRAM_BOT_TOKEN` (NOT in config.yaml)
- config.yaml has `telegram_bot_token: ''` and `telegram_enabled: 'true'`
- Messages flow: Telegram → TelegramAdapter → gateway.ingest() → WebSocket broadcast → desktop app
- Responses flow: Agent loop → gateway.send() → WebSocket (desktop) + Telegram adapter (phone)
- Desktop app: `useChatStream.ts` handles `type: "message"` for incoming Telegram user messages
- Gateway: Both `ingest()` and `send()` broadcast telegram/whatsapp channels to WebSocket clients

## KEY DIRECTORIES

```
cato/                  Python daemon source
  api/                 aiohttp web + WebSocket handlers
  orchestrator/        Multi-model CLI fan-out (Claude/Codex/Gemini/Cursor)
    cli_invoker.py     Claude/Codex/Gemini/Cursor invocation with timeouts
    cli_process_pool.py Warm pool for Claude/Codex
  audit/               Hash-chained audit log (PACKAGE)
  auth/                Token store + checker
  core/                Memory, context, scheduling
    memory.py          MemorySystem
    context_builder.py ContextBuilder (loads SOUL.md, IDENTITY.md, SKILL.md)
    schedule_manager.py SchedulerDaemon
  ui/
    server.py          aiohttp server, workspace_put/get endpoints, CORS middleware
    dashboard.html     Web UI (monolithic SPA, ~1700 lines)
  adapters/
    telegram.py        Telegram long-polling adapter
  cli.py               Main Click CLI
  agent_loop.py        Core agent loop + tool registry (file, browser, shell, github, conduit, memory, graph, web_search, python, clawflows)
  gateway.py           Message routing hub — WebSocket broadcast + adapter delivery + activity indicator
  vault.py             AES-256-GCM vault
  budget.py            Hard spend caps
desktop/               Tauri v2 desktop app
  src/                 React/TypeScript frontend
    hooks/
      useChatStream.ts WebSocket hook — handles web + Telegram messages, 5s history poll
    components/
      ActivityIndicator.tsx  Real-time busy/idle pill (polls /api/activity + WS events)
    views/
      ChatView.tsx     Main chat interface
      SettingsView.tsx Settings tabs (general/memory/channels/scheduling/workspace)
  src-tauri/           Rust sidecar
    target/release/    cato-desktop.exe (17MB release build)
tests/                 pytest test suite (1346+ tests, must stay 100%)
```

## DESKTOP APP DETAILS

- Built: `desktop/src-tauri/target/release/cato-desktop.exe`
- Desktop shortcut: `C:\Users\Administrator\Desktop\Cato.lnk` → points to exe above
- Build script: `desktop/build_release.ps1`
- Build env: MSVC 14.44.35207 + Windows SDK 10.0.26100.0
- **Heartbeat timeout**: 45s (server sends every 30s)
- **CORS**: `cors_middleware` in `cato/ui/server.py` — whitelists `tauri://localhost`, `http://tauri.localhost`, `https://tauri.localhost`, `http://127.0.0.1`, `http://localhost`
- Coding agent WS is on port 8080 (aiohttp), NOT 8081 (gateway)
- Logo: `cato-logo.png` (transparent 1024×1024 PNG), 44×44px in sidebar
- **Activity Indicator**: green "Idle" / amber "Working… <task>" pill in Dashboard + Chat headers. Backend: `gateway._broadcast_activity()` pushes WS events + `GET /api/activity` HTTP polling (token-exempt). Frontend: `ActivityIndicator.tsx` polls every 2s, listens for WS `type: "activity"` events.

## SKILLS SYSTEM

- Skills directory: `~/.cato/skills/` (18+ skills: add-notion, coding-agent, daily-digest, etc.)
- System prompt injection: `agent_loop.py` builds prompt with `skills_dir` parameter
- SwarmSync routing is enabled (`swarmsync_enabled: true`) — routes each call to the best model
- Workspace files (`SOUL.md`, `IDENTITY.md`, `AGENTS.md`, `TOOLS.md`) loaded from `workspace_dir`

## CODING AGENT STATUS

Fan-out to Claude/Codex/Gemini/Cursor in parallel (60s timeout each):
- **Claude**: cli_process_pool (warm) — nested execution, blocked in production
- **Codex**: cli_process_pool (warm) — works
- **Gemini**: Subprocess only — hangs on Windows (stdin pipe detection issue)
- **Cursor**: Subprocess only — most reliable on this system
- All timeouts return degraded response with confidence 0.5

## WINDOWS-SPECIFIC NOTES

- npm CLIs (codex, gemini) are .CMD files; resolved via `shutil.which()` + `["cmd.exe", "/c", path]`
- ANTHROPIC_API_KEY loaded from `.env` (python-dotenv); OpenRouter env key in `.env` is STALE — use vault
- Cato is run as SEPARATE daemon — Claude CLI is NOT nested in production
- PowerShell required for build scripts; bash available via Git Bash

## TEST INFRASTRUCTURE

- pytest asyncio_mode=auto, tests/ directory
- Coverage via pytest-cov; `norecursedirs` excludes `.claude`, `BRAINSTORM`, `venv`
- **1705/1705 tests passing** as of 2026-05-13

## AUDIT REPORT LOCATIONS

- Alex audit: `CATO_ALEX_AUDIT.md` (repo root)
- Kraken verdict: `CATO_KRAKEN_VERDICT.md` (repo root)
- Historical verdicts: `KRAKEN_VERDICT_*.md`
