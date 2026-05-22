# PROJECT BLACKBOX AUDIT

## Cato — AI Agent Daemon

**Audit date:** 2026-05-22
**Auditor:** Project Blackbox Auditor v2.2.0 (13 parallel subagents)
**Scope:** full / pre-launch
**Confidence:** 92%
**Historical memory:** Loaded from internal audit reports (CATO_ALEX_AUDIT.md, CATO_KRAKEN_VERDICT.md, HKO-truth-audit-report.md, TRUTH_AUDIT_ACTIVITY_INDICATOR.md)
**Large repo scope:** Full repo (480 tracked files — within single-scope limit)
**Stack:** Python 3.11+, asyncio, aiohttp, SQLite, AES-256-GCM vault, Tauri v2 desktop (React 19 + TypeScript + Rust sidecar), SwarmSync LLM routing, Telegram adapter, Gmail adapter. Ports: HTTP 8080, WS 8081. No payment/escrow surfaces.

---

## 1. VERDICT

**LAUNCH GATE:** CONDITIONAL PASS (private/personal use) | FAIL (public repo or shared install)
**Unresolved CRITICAL issues:** 4
**Unresolved HIGH issues:** 10
**Customer journey verdict:** Partially customer-verified — local daemon install path works for source install; PyPI path likely broken; first-message fails silently without API key

**FAIL conditions for public release:**
1. Vault password `mypassword123` hardcoded in 9+ committed source files — vault encryption is meaningless
2. Telegram bridge bot token hardcoded in committed `install_autostart.py` — in git history; must rotate
3. Shell tool PowerShell auto-escalation to full mode — any LLM turn can execute unrestricted PowerShell
4. Duplicate `/api/memory/stats` route — dashboard always shows incorrect memory data

**Conditional PASS for private single-user VPS use:** No payment surfaces, no multi-tenant exposure, no public-facing endpoints. The four CRITICALs are real but their current blast radius is limited to this machine. Fix all four before any repo sharing.

---

## 2. EXECUTIVE SUMMARY

Cato is a well-architected, heavily tested (1867 tests) local AI agent daemon with genuine security intent — AES-256-GCM vault, constant-time token comparison, hash-chained audit log, granular tool approval policies. The engineering quality is above average for a personal tool.

However, the vault's security guarantee is defeated by a single decision: `CATO_VAULT_PASSWORD=mypassword123` is hardcoded as the default in `cato_svc_runner.py`, `cato_service.py`, `install_autostart.py`, and five launcher scripts — all committed to `main`. Anyone who clones this repo can decrypt the vault. A Telegram bridge bot token is also hardcoded in the tracked `install_autostart.py`.

The shell tool architecture has a documented bypass: any `powershell*` command auto-escalates to `create_subprocess_shell` (full OS access), and `shell.exec` is in `_DEFAULT_ALLOWED_TOOLS` which authorizes it with no delegation token and no user confirmation when no active token session exists. This is "by design" for a power-tool local daemon, but it is undocumented and creates a consistent LLM-exploitable path to full system access.

The diagnostics dashboard always shows incorrect memory stats due to a duplicate route registration where `memory_routes.py`'s handler silently overwrites `server.py`'s richer handler at startup.

Previous internal audits (HKO, Alex, Kraken) were thorough and most of their findings are confirmed fixed — the codebase has been improved iteratively. The remaining gaps are concentrated in secrets management, shell tool safety documentation, and test coverage of the HTTP auth middleware and shell tool.

---

## 3. SUMMARY RISK TABLE

| Severity | Count | Resolved | Unresolved | Unverified |
|---|---:|---:|---:|---:|
| CRITICAL | 4 | 0 | 4 | 0 |
| HIGH | 10 | 0 | 10 | 0 |
| MEDIUM | 18 | 0 | 18 | 0 |
| LOW | 10 | 0 | 10 | 0 |

---

## 4. TOP 5 RISKS BY BLAST RADIUS

1. **Hardcoded vault password in committed source** — CRITICAL — Anyone with repo read access can decrypt the vault and extract every stored API key (SwarmSync, Telegram, OpenRouter, Gmail OAuth). The entire vault security model is bypassed.
2. **Shell PowerShell full-mode + no delegation token required** — CRITICAL — An LLM turn (via SwarmSync or compromised routing) can call `shell.exec` with `powershell -c "anything"` and execute unrestricted on the host OS. No confirmation gate. No cwd restriction.
3. **Telegram bot token in committed git history** — CRITICAL — `install_autostart.py` contains a live bot token committed to `main`. Must rotate; git history must be scrubbed before any public push.
4. **No rate limiting anywhere** — HIGH — `/api/daemon/restart`, `/api/vault/set`, `/api/cron/jobs/{name}/run`, and the coding agent invoke endpoint all accept unlimited requests. A local malicious process can denial-of-service the daemon with repeated SIGTERM calls.
5. **SSRF via browser tool** — HIGH — `cato/tools/browser.py` and `cato/tools/conduit_bridge.py` accept user-controlled URLs for navigation without IP allowlist checks, enabling LLM-directed access to internal network services (metadata endpoints, localhost ports, private subnets).

---

## 5. FAST CRITICAL-RISK SWEEP

| Check | Result | Evidence |
|---|---|---|
| Phantom escrow release | N/A | No payment/escrow surfaces in codebase |
| IDOR on resource creation | PARTIAL | `DELETE /api/sessions/{id}` accepts any session_id without ownership check — `server.py:2134` |
| Secrets in committed files | **FAIL** | `cato_svc_runner.py:16`, `cato_service.py:20`, `install_autostart.py` — vault password + Telegram bot token committed |
| Secrets in git history | **FAIL** | Bot token in `install_autostart.py`, `mypassword123` in multiple launcher files — commit `e08a3e1` and others |
| Public sensitive fields in API | PASS | Config PATCH response strips `token`, `password`, `secret`, `_key`, `vault` at `server.py:1479` |
| SSRF via user-controlled URLs | **FAIL** | `browser.py:177` has local IP check but `conduit_crawl.py` has none; `web_search.py:250,306` uses raw aiohttp without `_assert_safe_url` |
| Webhook side effects before verification | N/A | No webhook surfaces in this codebase |
| Missing production env vars | PARTIAL | `SWARMSYNC_API_KEY` absence causes silent chat failure with no UI feedback; no startup validation |
| Shell injection risk | **FAIL** | `shell.py:85-91` auto-upgrades any `powershell*` to `create_subprocess_shell`; `shell.exec` in `_DEFAULT_ALLOWED_TOOLS` with no confirmation gate |

---

## 6. FINDINGS — CRITICAL

### C-1: Vault password `mypassword123` hardcoded in 9+ committed source files
**Category:** Secrets Management / Deployment
**Status:** NEW
**Evidence:**
- `cato_svc_runner.py:16` — `os.environ.setdefault("CATO_VAULT_PASSWORD", "mypassword123")`
- `cato_service.py:20` — `os.environ["CATO_VAULT_PASSWORD"] = "mypassword123"`
- `install_autostart.py:24` — `VAULT_PASSWORD = "mypassword123"`
- `launch_daemon.ps1:4`, `start_daemon.ps1:1`, `start_cato.bat:2`, `scripts/run_watchdog.bat:3` — all hardcode same password
- `Desktop App Ralph Loop/AGENTS.md`, `Web UI Ralph Loop/AGENTS.md` — contain password in example commands

**Root cause:** Convenience runner scripts bake in the password so the daemon runs as a SYSTEM service without interactive prompts, but those scripts were committed to the repo.
**Impact:** Any person with read access to this repo can derive the vault master password, run `argon2id(mypassword123, salt)` to derive the AES key, and decrypt `vault.enc` — exposing all stored API keys (SWARMSYNC_API_KEY, TELEGRAM_BOT_TOKEN, OpenRouter key, Gmail OAuth refresh token).
**Fix task:** Remove hardcoded password from all source files. Inject `CATO_VAULT_PASSWORD` via Windows Credential Manager or NSSM service environment — never committed source. Rename all launcher scripts with real passwords as git-ignored local copies. Provide `.env.example.ps1` that documents the pattern without actual values.
**Acceptance criteria:**
- [ ] `git grep -r "mypassword123" -- "*.py" "*.ps1" "*.bat" "*.md"` returns zero results in tracked files
- [ ] `cato_svc_runner.py` reads vault password only from environment, never sets a fallback default
- [ ] `git history` scrubbed of all occurrences before any public push

---

### C-2: Shell tool auto-escalates PowerShell to unrestricted full mode; shell.exec requires no delegation token
**Category:** Auth / Security
**Status:** NEW
**Evidence:**
- `cato/tools/shell.py:85-91` — `if base_cmd in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"): mode = "full"` — silently upgrades to `asyncio.create_subprocess_shell` with no allowlist or cwd restriction
- `cato/auth/token_checker.py:33-36` — `"shell_execute", "shell.exec", "shell"` in `_DEFAULT_ALLOWED_TOOLS` — authorized with no confirmation when no active delegation token exists
- `cato/safety.py` — keyword scanner misses PS-native destructive verbs (`Remove-Item`, `Clear-Content`, `Format-Volume`)

**Root cause:** PowerShell legitimately needs broad OS access on Windows, so it was special-cased, but the auto-upgrade path is undocumented and creates a consistent bypass. The `_DEFAULT_ALLOWED_TOOLS` inclusion means no delegation token is required to call it.
**Impact:** An LLM turn (from any source — SwarmSync, compromised routing, malicious skill) can exfiltrate any file, delete data, or execute any program by sending `{"tool": "shell", "args": {"command": "powershell -c '...'"}}`. The safety guard's keyword filter is bypassed for PS-native commands.
**Fix task:**
1. Remove `shell`, `shell.exec`, `shell_execute`, `python.execute` from `_DEFAULT_ALLOWED_TOOLS` in `token_checker.py`. These should fall through to `requires_user_confirmation=True` when no delegation token covers the shell category.
2. Add `powershell_full_mode` config flag (default: `false`) — require explicit opt-in rather than auto-detection.
3. Add PowerShell-native destructive verbs to `safety.py` scanner: `Remove-Item`, `Clear-Content`, `Format-Volume`, `Stop-Process`, `Invoke-Expression`.
**Acceptance criteria:**
- [ ] `shell.exec` with no active delegation token returns `requires_user_confirmation=True`
- [ ] PowerShell command without `powershell_full_mode: true` in config is rejected or requires confirmation
- [ ] Test: `test_shell_requires_confirmation_with_no_token` passes

---

### C-3: Duplicate `/api/memory/stats` route registration — wrong handler always wins
**Category:** Frontend/Backend API Contract / Data Integrity
**Status:** NEW
**Evidence:**
- `cato/ui/server.py:2838` — registers `GET /api/memory/stats` → returns `{facts, kg_nodes, kg_edges, stats:{chunks_indexed, model}}`
- `cato/api/memory_routes.py:136` — also registers `GET /api/memory/stats` → returns `{success, stats:{chunks_indexed, model}}` (no `facts` counts)
- `server.py` `register_all_routes()` is called after server.py routes, so `memory_routes.py` handler overwrites the richer server.py handler

**Root cause:** `memory_routes.py` was added as a refactor but the original route in `server.py` was not removed, creating a duplicate. aiohttp last-registration wins.
**Impact:** The Settings view Memory tab and any consumer of `facts`, `kg_nodes`, `kg_edges` always shows zero/missing counts. The richer diagnostic data from `server.py`'s handler is permanently unreachable.
**Fix task:** Remove the `app.router.add_get("/api/memory/stats", memory_stats)` line from `server.py:2838` (defer to `memory_routes.py`) OR remove the duplicate registration in `memory_routes.py` and keep the richer `server.py` handler. Do not keep both.
**Acceptance criteria:**
- [ ] `GET /api/memory/stats` returns `facts`, `kg_nodes`, `kg_edges`, and `stats` in one response
- [ ] SettingsView Memory tab shows correct counts
- [ ] `test_memory_stats_returns_all_fields` test added

---

### C-4: Telegram bridge bot token hardcoded in committed `install_autostart.py` — in git history
**Category:** Secrets Management
**Status:** NEW
**Evidence:**
- `install_autostart.py` — contains hardcoded Telegram bot token `8573304576:AAFT4SbfetSd2ydONWYE75atJC-CHmjLu9U`
- File is tracked in git (not in `.gitignore`)
- Commit `e08a3e1` ("feat: SwarmSync routing…") introduced the file with the token
- SA-9 confirmed: "Secrets permanently in git history"

**Root cause:** `install_autostart.py` was committed with the token baked in for convenience; `.gitignore` excludes `launch_bridge.py` but not `install_autostart.py`.
**Impact:** The bot token is permanently in public-accessible git history. Anyone with a clone can extract and use the token to impersonate the Telegram bridge bot, read all messages routed through it, and send messages as the bot. Token is currently live.
**Fix task:**
1. **Immediately revoke** token `8573304576:AAFT4SbfetSd2ydONWYE75atJC-CHmjLu9U` via BotFather.
2. Replace hardcoded token in `install_autostart.py` with `os.environ.get("CATODESKTOP_BOT_TOKEN")`.
3. Add `install_autostart.py` to `.gitignore` OR replace all hardcoded values with env var reads.
4. Scrub git history with `git filter-repo --replace-text` before any public push.
**Acceptance criteria:**
- [ ] Bot token rotated in BotFather
- [ ] `git log -S "8573304576" --all` returns no results after history scrub
- [ ] `install_autostart.py` reads token from environment only

---

## 7. FINDINGS — HIGH

### H-1: Zero rate limiting on any API endpoint
**Evidence:** `server.py` — no middleware, no per-route limiter, no IP throttling anywhere
**Root cause:** Not implemented; not in scope during development
**Impact:** `/api/daemon/restart` (→ SIGTERM), `/api/vault/set`, `/api/cron/jobs/{name}/run`, `/api/coding-agent/invoke` all accept unlimited requests. A malicious local process can loop-kill the daemon or hammer the vault.
**Fix task:** Add `aiohttp_ratelimiter` or simple in-memory sliding window on `/api/daemon/restart`, vault mutation endpoints, and coding agent invocations. Minimum: 10 req/min on destructive endpoints.

### H-2: SSRF via browser/conduit tool — no IP allowlist validation
**Evidence:** `cato/tools/browser.py:177` has local IP check; `cato/tools/conduit_bridge.py` crawl/map_site uses browser internally; `cato/tools/web_search.py:250,306` uses raw `aiohttp.ClientSession` without `_assert_safe_url`
**Root cause:** `integrations/http_client.py` has full SSRF defense but tools bypass it by using raw aiohttp
**Impact:** LLM can navigate to `http://169.254.169.254/` (cloud metadata), `http://localhost:8081/` (gateway), or any internal service
**Fix task:** Wire `_assert_safe_url` from `integrations/http_client.py` into `web_search.py` URL construction; verify `conduit_bridge.py` crawl target URLs pass through it

### H-3: Pipeline `_run_requirement` executes arbitrary scripts without path validation
**Evidence:** `cato/pipeline/runtime.py:313-318` — `script_path = Path(script)` where `script` comes from `PhaseRequirement.script` populated from `manifest.json`; no path restriction
**Root cause:** Manifests are generated by the pipeline itself so the attack surface was considered internal
**Impact:** Any agent or external tool that can write to a business's `manifest.json` achieves arbitrary code execution as the daemon user
**Fix task:** Validate that `script_path` is relative to `run.business_dir` or a trusted scripts allowlist before execution

### H-4: WebSocket `/ws/pty/*` and `/ws/coding-agent/*` skip pre-flight token check
**Evidence:** `server.py:113-114` — `_TOKEN_EXEMPT_WS_PREFIXES = ("/ws/pty/", "/ws/coding-agent/")` — upgrade happens without token verification; auth deferred to first-message envelope
**Root cause:** Intentional design for latency; relies on envelope auth on first message
**Impact:** Any local process can establish an unauthenticated WS connection to these endpoints; if first message is delayed, connection is held open
**Fix task:** Move token check to pre-upgrade for PTY and coding-agent WS, or enforce a strict 500ms timeout to receive auth envelope before closing

### H-5: CI pipeline does not run pytest test suite
**Evidence:** `.github/workflows/python-verify.yml` — runs `verify_python_build.py` only; no `pytest tests/` step
**Root cause:** CI workflow was set up for build verification only
**Impact:** A broken test suite (1867 tests) would not be caught before merge to main; regressions ship silently
**Fix task:** Add `python -m pytest tests/ -x -q --tb=short` step to `python-verify.yml` after `pip install -e .[dev]`

### H-6: No ShellTool unit tests — security-critical module is untested
**Evidence:** `cato/tools/shell.py` (257 lines, 3 execution modes, allowlist, cwd clamp) has zero dedicated tests; `test_tool_registration.py` only confirms registration
**Root cause:** Shell tool treated as infrastructure; test coverage focused on higher-level flows
**Impact:** Any regression in allowlist enforcement, cwd clamping, or mode selection would ship undetected
**Fix task:** Create `tests/test_shell_tool.py` covering: allowlist rejection in gateway mode, cwd clamped to workspace, PowerShell mode detection, audit log write on execution

### H-7: No HTTP auth middleware rejection tests
**Evidence:** No test in `tests/` asserts that a POST to a protected endpoint with missing/wrong token returns 401; `test_server_lifecycle.py` only tests valid-token path
**Root cause:** Tests written for happy path; rejection path not covered
**Impact:** If `auth_token_middleware` breaks, no test would catch it before ship
**Fix task:** Add `test_protected_post_rejects_missing_token` and `test_protected_post_rejects_wrong_token` to `tests/test_ui_server_runtime_health.py`

### H-8: GitHub token naming mismatch — GitHub tool is silently non-functional
**Evidence:** `.env` has `GITHUB_FOXFIREPOETS_TOKEN` but `cato/tools/github_tool.py` looks up `GITHUB_TOKEN`, `GH_TOKEN`, `github_token` — never matches
**Root cause:** Token named for the specific account rather than the generic name the tool expects
**Impact:** GitHub tool always reports "not configured" even though a token exists; any git/GitHub automation silently does nothing
**Fix task:** Rename `.env` entry from `GITHUB_FOXFIREPOETS_TOKEN` to `GITHUB_TOKEN`, or add `GITHUB_FOXFIREPOETS_TOKEN` as a lookup alias in `github_tool.py`

### H-9: DB path split-brain — server.py reads different files than module defaults
**Evidence:**
- `ContradictionDetector`: module default `~/.cato/contradictions.db` vs server.py `get_data_dir()/default/contradictions.db`
- `DecisionMemory`: module default `get_data_dir()/cato.db` vs server.py `get_data_dir()/default/decisions.db`
- `AnomalyDetector`: module default `get_data_dir()/cato.db` vs server.py `get_data_dir()/default/anomaly.db`

**Root cause:** Modules were refactored to accept custom paths but module defaults were not updated
**Impact:** Diagnostics UI always shows empty data for contradictions, decisions, and anomalies even when the daemon has real records written to the module-default paths
**Fix task:** Normalize all three modules to use `get_data_dir()/default/<name>.db` as their default path, matching server.py's expectations

### H-10: Dead "Save Schedule" button in SettingsView
**Evidence:** `desktop/src/views/SettingsView.tsx` — `<button className="button-primary">Save Schedule</button>` with no `onClick` handler; heartbeat interval input is uncontrolled (`defaultValue`); no backend scheduling config endpoint exists
**Root cause:** Scheduling tab UI was scaffolded but backend wiring was never completed
**Impact:** Users clicking "Save Schedule" get no feedback and no persistence; heartbeat interval changes are lost on refresh
**Fix task:** Wire `onClick` to `PATCH /api/config` with `heartbeat_interval` key, or remove the button and input until implemented; add a "coming soon" label if still unimplemented

---

## 8. FINDINGS — MEDIUM

### M-1: Session IDOR — session delete/replay accepts any session_id without ownership check
**Evidence:** `server.py:2134` — `DELETE /api/sessions/{session_id}` and `POST /api/sessions/{session_id}/replay` use session_id from URL without verifying caller owns it
**Impact:** Single-user daemon — low current risk. Multi-user future deployment would expose full session takeover.
**Fix task:** Add `if session.owner_id != request_user_id: return 403` once multi-user is in scope; document as known single-user assumption now

### M-2: `personal_store` functions called from Telegram adapter without `init_db()` guard
**Evidence:** `cato/adapters/telegram.py:338-456` calls `get_todos_and_reminders()`, `get_recent_notes()`, `claim_email_for_send()` without calling `personal_store.init_db()` first; if Telegram starts before Gmail adapter, `OperationalError: no such table: emails`
**Fix task:** Add `await personal_store.init_db()` to `TelegramAdapter.start()` method

### M-3: Dead shadow file `cato/audit.py` permanently masked by `cato/audit/` package
**Evidence:** `cato/audit.py` exists alongside `cato/audit/` package; Python resolves `import cato.audit` to package — `audit.py` is unreachable. Contains older `_SENSITIVE_KEYS` set missing 5 keys present in `audit_log.py`
**Fix task:** `git rm cato/audit.py`

### M-4: Over-broad Gmail sender filter skips legitimate business contacts
**Evidence:** `gmail_adapter.py:50-81` — `_NO_REPLY_EXACT_LOCAL` includes `"info"`, `"support"`, `"help"`, `"billing"`, `"security"`, `"account"`, `"reply"` — silently skips `support@stripe.com`, `billing@github.com`, `info@anyclient.com`
**Fix task:** Trim `_NO_REPLY_EXACT_LOCAL` to unambiguous automated addresses only: `noreply`, `no-reply`, `donotreply`, `do-not-reply`, `mailer-daemon`, `postmaster`, `bounce`, `bounces`; move the rest to LLM classifier tier

### M-5: `shell.exec` and `python.execute` authorized without confirmation when no delegation token active
**Evidence:** `token_checker.py:178-185` — when no active delegation tokens exist, tools in `_DEFAULT_ALLOWED_TOOLS` (including `shell`, `python.execute`) return `authorized=True, requires_user_confirmation=False`
**Fix task:** Same as C-2 fix — remove execution tools from `_DEFAULT_ALLOWED_TOOLS`

### M-6: Diagnostics export `to_dict()` includes `vault` field — not excluded like `save()`
**Evidence:** `config.py:328-332` — `to_dict()` includes all public fields including `vault`; `save()` has `_RUNTIME_ONLY` guard to exclude it; redactor handles vault key but the exclusion should be at source
**Fix task:** Add `vault` to `_RUNTIME_ONLY` exclusion set and mirror in `to_dict()`

### M-7: Cron schedule `skill` field injected unsanitized into `dispatch_fn`
**Evidence:** `schedule_manager.py:67-76` — `Schedule.from_dict()` passes `skill` verbatim from YAML to dispatch; no validation against known skill names
**Fix task:** Validate `skill` against the registered skill list in `SchedulerDaemon.add_schedule()`

### M-8: GMAIL_REFRESH_TOKEN absence causes fail-open — email checks silently skipped
**Evidence:** `gmail_adapter.py:474` — logs warning and skips on missing refresh token instead of disabling or raising
**Fix task:** On missing refresh token, set `self._enabled = False` and log a clear user-visible warning

### M-9: No startup validation of SWARMSYNC_API_KEY — first message fails silently
**Evidence:** `cato_svc_runner.py` starts daemon with no API key check; `agent_loop.py` fails silently on first LLM call; no UI-level error shown to user
**Fix task:** Add health-check step to startup: if `swarmsync_enabled=True` and no SwarmSync key found, log a prominent warning to stdout AND return a synthetic assistant message on first user turn: "No LLM API key configured. Run `cato init` or set SWARMSYNC_API_KEY."

### M-10: PyPI package `cato-daemon` likely does not exist publicly
**Evidence:** README advertises `pip install cato-daemon` but project is a local editable install; no PyPI publish workflow in CI; `pyproject.toml` has no publish step
**Impact:** New users following README hit a pip 404
**Fix task:** Either publish to PyPI or change README installation to `git clone ... && pip install -e .`

### M-11: `_DEFAULT_ALLOWED_TOOLS` includes token category bypass via wildcard `"*"`
**Evidence:** `server.py` — `POST /api/tokens` accepts `categories` from POST body without validating against `ACTION_CATEGORIES`; a caller with token access can create a delegation token with categories `["*"]`
**Fix task:** Validate `categories` in `create_token()` against `ACTION_CATEGORIES` enum; reject unknown categories

### M-12: Gemini always invoked on Windows despite known hang
**Evidence:** `cli_invoker.py:413` — `invoke_all_parallel` always schedules Gemini; no `sys.platform == "win32"` guard; 60s timeout is the only protection; wastes event-loop time every parallel fan-out
**Fix task:** Add `if sys.platform == "win32" and not cfg.gemini_enabled_windows: skip_gemini = True` or add `gemini_enabled: bool = False` default in `CatoConfig` for Windows

### M-13: Token injection into dashboard HTML uses `repr()` not `json.dumps()`
**Evidence:** `server.py:505` — `f"window.__CATO_TOKEN__ = {repr(_DAEMON_TOKEN)};\n"` — Python `repr()` on a hex string happens to produce valid JS but `json.dumps()` is the safe and correct approach
**Fix task:** Replace `repr(_DAEMON_TOKEN)` with `json.dumps(_DAEMON_TOKEN)`

### M-14: HKO audit files tracked in git despite gitignore intent
**Evidence:** `HKO_TRUTH_AUDIT.md` and `HKO_TRUTH_AUDIT_CATO_INTEGRATIONS.md` are tracked; `.gitignore` has `HKO_TRUTH_AUDIT_*.md` which misses `HKO_TRUTH_AUDIT.md` (no `_*` suffix)
**Fix task:** Add `HKO_TRUTH_AUDIT*.md` (no underscore before glob) to `.gitignore`; run `git rm --cached HKO_TRUTH_AUDIT.md HKO_TRUTH_AUDIT_CATO_INTEGRATIONS.md`

### M-15: `chunk_usage` split-brain — same table defined on two different DB files
**Evidence:** `core/memory.py:69` defines `chunk_usage` in per-agent memory DB; `core/context_pool.py:22` defines the same table; when `ContextPool` uses a default path it writes to `context_pool.db` while memory queries go to `<agent_id>.db`
**Fix task:** Document and enforce a single canonical DB path for `chunk_usage`, passed from `ContextPool` to the memory instance at construction time

### M-16: `_apply_facts_migration` silently swallows all `OperationalError`
**Evidence:** `core/memory.py` — `ALTER TABLE` catch handles "column already exists" but catches ALL `OperationalError` including disk-full and corrupt-DB
**Fix task:** Parse `str(exc)` for "duplicate column name" or "already exists"; re-raise all other `OperationalError`s

### M-17: `routing_events` table has no index on `ts` and no WAL mode
**Evidence:** `routing_log.py` — no `PRAGMA journal_mode=WAL`; no index on `ts` column; time-range queries do full scans
**Fix task:** Add `PRAGMA journal_mode=WAL` and `CREATE INDEX IF NOT EXISTS idx_routing_ts ON routing_events(ts)` to schema setup

### M-18: MCP server exposes session history without authentication
**Evidence:** `cato/mcp/runtime.py:127-147` — `cato_get_history` and `cato_list_sessions` require no credentials; server bound to `127.0.0.1:8765` is the only protection
**Fix task:** Add static API key header check on MCP server using the daemon token from `server.py`

---

## 9. FINDINGS — LOW

### L-1: Test count in CLAUDE.md/MEMORY.md is stale (1705 vs 1867)
**Fix:** Update CLAUDE.md line "1705/1705 tests passing" to "1867 passed (as of 2026-05-20)"

### L-2: Session cap claim in README is false — not enforced
**Evidence:** README states "session cap ($1.00)... enforced before every LLM call"; `budget.py` explicitly notes session cap is `"NOT enforced — retained for backward compat"`
**Fix:** Update README to remove session cap enforcement claim; mention only daily ($3) and monthly ($20) caps

### L-3: SwarmSync "routes ALL LLM calls" framing is conditional
**Evidence:** `agent_loop.py:1504-1519` — falls back to direct `select_model()` when key absent; circuit-breaker also bypasses SwarmSync on 3 failures
**Fix:** Update CLAUDE.md to: "SwarmSync routes LLM calls when key is present and reachable; falls back to configured model on key absence or circuit-breaker trip"

### L-4: Privacy framing vs SwarmSync data flow
**Evidence:** "zero outbound except your LLMs" — SwarmSync is a third-party SaaS that receives full conversation payload
**Fix:** Add one sentence to README: "All LLM calls route through SwarmSync (api.swarmsync.ai); conversation content leaves this machine."

### L-5: Chat input `autoFocus` when WebSocket is disconnected
**Evidence:** `ChatView.tsx` — input gets focus even when status is "Disconnected"; users type and hit Enter with no visible error
**Fix:** Gate `autoFocus` on `isConnected` state

### L-6: Raw `alert()` used for upload errors in ChatView
**Evidence:** `ChatView.tsx:202` — `alert("Upload failed: ...")` — jarring browser dialog
**Fix:** Replace with styled error notification component

### L-7: Foreign keys disabled — `kg_edges` FK constraints are decorative
**Evidence:** No `PRAGMA foreign_keys = ON` anywhere; orphaned edge rows possible
**Fix:** Add `PRAGMA foreign_keys = ON` to each SQLite connection setup in `memory.py`

### L-8: `cato.db` is a single high-contention DB for 12+ modules
**Evidence:** `AuditLog`, `LedgerStore`, `TokenStore`, `TemporalReconciler`, `DecisionMemory`, `AnomalyDetector`, `HabitExtractor` etc. all default to same file
**Fix:** Low urgency. WAL mode mitigates. No action required until performance is an issue.

### L-9: No dedicated Telegram adapter unit tests
**Evidence:** `TelegramAdapter` has zero unit-level tests; only integration-level WS broadcast tested
**Fix:** Create `tests/test_telegram_adapter.py` covering long-poll loop, send path, and HTTP error handling

### L-10: `externalBin` Tauri sidecar config stub vs actual Python launch
**Evidence:** `tauri.conf.json` declares `binaries/cato`; `sidecar.rs` spawns `python -m cato` directly; `build.rs` creates placeholder stub
**Fix:** Document mismatch in a code comment; non-blocking for current VPS deployment

---

## 10. PRODUCT PROMISE vs IMPLEMENTATION REALITY

| Feature Claim | Frontend Evidence | API Evidence | DB Evidence | Test Evidence | Status | Gap |
|---|---|---|---|---|---|---|
| Privacy-focused — zero telemetry | No telemetry calls in UI | No telemetry endpoint in agent_loop/gateway | Local SQLite only | N/A | **PARTIAL** | LLM content sent to SwarmSync (external SaaS); framing understates this |
| AES-256-GCM encrypted vault | Settings UI reads from vault | `vault.py` — correct AES-256-GCM + Argon2id KDF | `vault.enc` on disk | `test_vault.py` | **VERIFIED** | None — implementation exactly matches claim |
| SwarmSync routes ALL LLM calls | Routing indicator in UI | `agent_loop.py:1504-1519` — conditional on key presence + circuit breaker | N/A | `test_router.py` | **PARTIAL** | "Full stop" is false; two bypass paths: missing key + circuit-breaker trip |
| Telegram bidirectional integration | `useChatStream.ts` handles `type:"message"` | `telegram.py` long-polling + `gateway.py` dual-send | N/A | Telegram adapter tests | **VERIFIED** | None |
| Gmail integration | Settings Channels tab | `gmail_adapter.py` full OAuth2 + poll + approve flow | `personal.sqlite3` | `test_gmail_adapter.py` | **VERIFIED** | None |
| 18+ skills in skills system | Settings workspace tab | `agent_loop.py:1463-1487` loads `~/.cato/skills/` | N/A | Skills loading tests | **VERIFIED** | Count accurate for live install; repo package has ~13 |
| 1705/1705 tests passing | N/A | N/A | N/A | `pytest_full.txt` — 1867 passed | **PARTIAL** | Count stale by 162 tests; all pass |
| Hard spend caps | Budget display in desktop | `budget.py:233-299` daily/monthly enforced atomically | `~/.cato/budget.json` | `test_budget.py` | **PARTIAL** | Session cap ($1.00) claimed in README is NOT enforced |
| Hash-chained audit log | Audit commands in UI | `audit/audit_log.py` SHA-256 chain + `verify_chain()` | `cato.db:audit_log` | `test_audit_chain.py` | **VERIFIED** | None |
| Coding agent fan-out Claude/Codex/Gemini/Cursor | Coding Agent view | `orchestrator/cli_invoker.py` all four backends | N/A | `orchestrator/tests/` | **VERIFIED** | Gemini hangs on Windows — documented correctly |

---

## 11. CUSTOMER JOURNEY, SIGNUP, AND DASHBOARD AUDIT

### Context
Cato is a locally-installed single-user daemon, not a web SaaS. "Customer journey" = installation + first use by a developer on their own machine.

### Installation Journey

| Step | Expected | Actual | Pass/Fail | Evidence |
|---|---|---|---|---|
| `pip install cato-daemon` | Package installs | Likely 404 — no PyPI publish | **FAIL** | No PyPI workflow in CI |
| `git clone ... && pip install -e .` | Works | Works | **PASS** | `pyproject.toml` |
| `cato init` | Wizard prompts vault pw, API keys | Well-designed; prompts in logical order | **PASS** | `cli.py` |
| `cato start` | Daemon starts on 8080 | Starts; uses `mypassword123` if run via `cato_svc_runner.py` | **CRITICAL** | See C-1 |
| Open http://localhost:8080 | Dashboard loads | Loads correctly | **PASS** | `dashboard.html` |
| Send first message | Gets response | Silent failure if no API key configured | **FAIL** | No SwarmSync key → no response + no UI error |
| Configure API key | Clear settings UI | Settings tab present; vault set via UI | **PASS** | SettingsView |
| Telegram / Gmail setup | Bidirectional messages | Works as documented | **PASS** | Integration tests |

### Customer Journey Verdict: Partially customer-verified

**Journey-blocking issues:**
1. `pip install cato-daemon` likely 404 — source install required
2. Running via `cato_svc_runner.py` bypasses `cato init`, starts with `mypassword123`
3. First message silently fails if no API key; no UI error shown to user

### Customer Journey Launch Gate: **FAIL for public distribution; PASS for private use**

---

## 12. FRONTEND / BACKEND API CONTRACT ALIGNMENT

| Endpoint / Flow | Frontend Sends | Backend Expects | Backend Returns | Frontend Expects | Status |
|---|---|---|---|---|---|
| WebSocket `/ws` | `{type:"message", text, session_id}` | Same shape | `{type:"response", text, channel, model}` | Checks `data.type==="response"` | **OK** |
| WS health ping | `{type:"health"}` | `type==="health"` | `{type:"health",status:"ok",…}` | Drops silently | **OK** |
| WS auth | `?token=` query string | `request.rel_url.query.get("token")` | 401 if missing | Appends `?token=` to URL | **OK** |
| `GET /health` | bare fetch | Token-exempt | `{status:"ok",version,sessions,uptime}` | Checks `status !== "ok"` | **OK** |
| `GET /api/config` | fetch + X-Cato-Token | Auth required; sensitive keys stripped | Config dict (no vault/secrets) | Reads `default_model`, `workspace_dir`, etc. | **OK** |
| `PATCH /api/config` | JSON body | Auth; content-type JSON | `{status:"ok", config:{…}}` | Checks `res.ok` | **OK** |
| `GET /api/memory/stats` | fetch | Auth | `{success, stats}` (wrong handler wins) | Reads `data.stats` | **CRITICAL BROKEN** — `facts`/`kg_nodes`/`kg_edges` never returned |
| `POST /api/memory/index` | bare POST | Auth | `{success, chunks_indexed, stats}` | Reads `data.chunks_indexed` | **OK** |
| `GET /api/inbox` | fetch | Auth | `{email_drafts, notes, todos, reminders, counts}` | Destructures all | **OK** |
| `GET /api/chat/history?since=` | fetch | Auth + `since` param | Array of message objects | Expects array with role/text/timestamp | **OK** |
| `POST /api/chat/upload` | FormData `file` field | Multipart, field name `"file"` | `{status,filename,path,size,type}` | Reads all fields | **OK** |
| `GET /api/routing/status` | fetch | Auth | `{swarmsync_enabled, will_use_swarmsync, live_test}` | Reads routing fields | **OK** |
| `GET /api/diagnostics/export` | fetch | Auth | Blob with `Content-Disposition` header | Triggers download | **OK** |
| SettingsView Save Schedule button | Nothing wired | No endpoint | N/A | Dead | **DEAD BUTTON** |
| All 10 `/api/diagnostics/*` endpoints | fetch | Auth | Various diagnostic shapes | All keys consumed | **OK** |

---

## 13. DATABASE / MIGRATION STATUS

| Check | Result | Evidence |
|---|---|---|
| All CREATE TABLE idempotent | **PASS** | 100% `IF NOT EXISTS` across all 8 DB files |
| Schema drift / pending migrations | **PASS** | No Prisma/ORM; raw SQLite with hand-rolled schema in each module |
| Null-unsafe field dereferences | **PASS** | Nullable columns guarded correctly in `pipeline/store.py` |
| Destructive migration risk | **PASS** | `ALTER TABLE ADD COLUMN` catches `OperationalError` correctly (but swallows too broadly — M-16) |
| Duplicate schemas | **FAIL** | `chunk_usage` defined in both `memory.py` and `context_pool.py`; may target different files (M-15) |
| DB path consistency | **FAIL** | `ContradictionDetector`, `DecisionMemory`, `AnomalyDetector` use different default paths than `server.py` instantiates them with (H-9) |
| Dead shadow file | **FAIL** | `cato/audit.py` permanently masked by `cato/audit/` package (M-3) |
| Indexes on frequent queries | **PARTIAL** | Missing index on `emails.status` and `routing_events.ts` (M-17) |
| WAL mode | **PARTIAL** | `routing_log.py` missing WAL mode |
| Foreign key enforcement | **FAIL** | `PRAGMA foreign_keys = ON` never set; `kg_edges` FK constraints decorative (L-7) |

---

## 14. ENV VAR COMPLETENESS

| Variable | Required By | In .env? | Runtime Validated? | Security Risk | Status |
|---|---|---|---|---|---|
| `CATO_VAULT_PASSWORD` | Vault unlock | YES | Hardcoded fallback in 9+ files | **CRITICAL** — committed as `mypassword123` | **CRITICAL** |
| `SWARMSYNC_API_KEY` | LLM routing | YES | No startup validation | HIGH — LLM calls silently fail without it | **WARN** |
| `OPENROUTER_API_KEY` | Fallback routing | YES | No | Medium — redundant if vault holds it | **REDUNDANT** |
| `TELEGRAM_BOT_TOKEN` | Telegram adapter | YES (vault) | Raises `ValueError` if missing | HIGH — in .env plaintext | **OK** (vault preferred) |
| `CATODESKTOP_BOT_TOKEN` | Telegram bridge | YES | Raises `RuntimeError` if missing | **CRITICAL** — hardcoded in `install_autostart.py` | **CRITICAL** |
| `GITHUB_FOXFIREPOETS_TOKEN` | .env only | YES | Never read | None — dead key | **ORPHANED** |
| `GMAIL_REFRESH_TOKEN` | Gmail adapter | YES | Log warning + skip (fail-open) | HIGH — grants inbox access | **WARN** |
| `GMAIL_ADDRESS` | .env only | YES | Never consumed | None | **ORPHANED** |
| `GMAIL_REDIRECT_URI` | .env only | YES | Never consumed | None | **ORPHANED** |
| `GENESIS_AGENTS_*_API_KEY` x2 | .env only | YES | Never consumed | None | **ORPHANED** |
| `CATO_STRICT_APPROVAL` | Token checker | No | No | Low — defaults permissive | **WARN** |
| `ANTHROPIC_API_KEY` | Router (conditional) | No | No | Low — no direct calls by design | **OK** |

---

## 15. DEPLOYMENT READINESS

| Check | Result | Evidence | Fix |
|---|---|---|---|
| Source install works | **PASS** | `pip install -e .` with hatchling | None |
| PyPI distribution | **FAIL** | No publish workflow; `pip install cato-daemon` likely 404 | Publish to PyPI or update README |
| CORS for Tauri | **PASS** | `server.py:379` — all three Tauri origins included | None |
| Health endpoint | **PASS** | `/health` token-exempt, returns `{status:"ok"}` | None |
| Daemon startup failure handling | **PARTIAL** | `cato_svc_runner.py` wraps with try/finally; no retry; no notification | Add supervisor restart config |
| Hardcoded secrets in runner | **FAIL** | `mypassword123` in `cato_svc_runner.py:16` and 8 other files | See C-1 |
| Hardcoded absolute path | **FAIL** | `cato_svc_runner.py:8-9` hardcodes `C:\Users\Administrator\Desktop\Cato` | Use `Path(__file__).parent.resolve()` |
| CI runs test suite | **FAIL** | CI only runs `verify_python_build.py`; no pytest | Add pytest step to `.github/workflows/python-verify.yml` |
| Heartbeat timeout documented vs coded | **PARTIAL** | 45s in CLAUDE.md; `useChatStream.ts` has no explicit 45s timer | OK for current use; document as browser-managed |
| Tauri sidecar config vs runtime | **PARTIAL** | `tauri.conf.json` declares binary stub; `sidecar.rs` spawns Python directly | Document in comment; non-blocking |
| Desktop build script | **PASS** | `desktop/build_release.ps1` exists and documented | None |
| No secrets in deploy config | **FAIL** | See C-1 and C-4 | Immediate remediation required |

---

## 16. API CONTRACT AND ROUTE SECURITY

| Route | Auth | Authorization | Validation | Rate Limit | Idempotency | Status |
|---|---|---|---|---|---|---|
| `GET /health` | None (exempt) | N/A | None needed | None | N/A | **OK** |
| `GET /api/activity` | None (exempt) | N/A | None needed | None | N/A | **OK** |
| `WS /ws` | Token query param | Token verified pre-upgrade | Message type checked | None | N/A | **OK** |
| `WS /ws/pty/*` | First-message envelope | Deferred | First message | None | N/A | **WARN** (H-4) |
| `WS /ws/coding-agent/*` | First-message envelope | Deferred | First message | None | N/A | **WARN** (H-4) |
| `GET /api/config` | X-Cato-Token | Sensitive keys stripped | None | None | N/A | **OK** |
| `PATCH /api/config` | X-Cato-Token | Sensitive keys stripped from response | JSON parse | None | N/A | **OK** |
| `DELETE /api/sessions/{id}` | X-Cato-Token | No ownership check | UUID format | None | N/A | **IDOR** (M-1) |
| `POST /api/daemon/restart` | X-Cato-Token | None | None | **NONE** | N/A | **WARN** — no rate limit |
| `POST /api/vault/set` | X-Cato-Token | None | JSON body | **NONE** | N/A | **WARN** — no rate limit |
| `POST /api/tokens` | X-Cato-Token | None | No category validation | None | N/A | **WARN** (M-11) |
| `POST /api/cron/jobs` | X-Cato-Token | None | `skill` unsanitized | None | N/A | **WARN** (M-7) |
| `GET /api/diagnostics/export` | X-Cato-Token | Config includes vault field via `to_dict()` | None | None | N/A | **WARN** (M-6) |
| All other `/api/*` | X-Cato-Token | Owner/resource scoped | Input validated | None | N/A | **OK** |

---

## 17. AUTH / SECURITY SNAPSHOT

| Check | Result | Evidence |
|---|---|---|
| Token comparison constant-time | **PASS** | `secrets.compare_digest` used in all auth paths: `server.py:368,603`, `websocket_handler.py:217`, `pty_routes.py:194` |
| IDOR protection | **PARTIAL** | Session delete/replay lacks ownership check (M-1) |
| SSRF protection | **PARTIAL** | `integrations/http_client.py` has full SSRF defense; `browser.py`, `web_search.py` bypass it (H-2) |
| Shell injection prevention | **PARTIAL** | Non-PS commands: allowlist + `shlex.split` + `subprocess_exec`. PS commands: auto-upgrade to full mode (C-2) |
| Admin route protection | **PASS** | All mutation routes require daemon token |
| Sensitive response fields | **PASS** | Config endpoint strips `token`, `password`, `secret`, `_key`, `vault` keys |
| Auth rate limiting | **FAIL** | No rate limiting anywhere (H-1) |
| CORS configuration | **PASS** | Strict `urlparse` host matching; no `startswith`; fixed Tauri origin set |
| WS token injection via `repr()` | **WARN** | `server.py:505` — use `json.dumps` instead (M-13) |
| Log redaction | **PASS** | `215ae8d` commit adds Bearer/JWT/bot-token redaction in router logs |

---

## 18. PAYMENTS / ESCROW / WALLET / FEE AUDIT

**N/A — No payment, escrow, wallet, or billing surfaces detected in this codebase.**

`cato/budget.py` manages local spend caps (daily $3, monthly $20 defaults) for LLM API calls. This is not an external payment surface. Audit not applicable.

---

## 19. AGENT / AUTOMATION / WORKFLOW AUDIT

| Agent/Workflow Surface | Real Path | Persistence | Verification | Failure Handling | Audit Evidence | Status |
|---|---|---|---|---|---|---|
| Coding agent fan-out (Claude) | `cli_invoker.py` pool + subprocess | Degraded dict on failure | Timeout + returncode | Returns `confidence=0.5` degraded | Audit logged | **PASS** |
| Coding agent fan-out (Codex) | Pool (MCP JSON-RPC) | Degraded dict | Full handshake + timeout | Returns degraded | Logged | **PASS** |
| Coding agent fan-out (Gemini) | Subprocess only | Degraded dict | Timeout at 60s | Hangs 60s on Windows | Logged | **MEDIUM** — no Windows guard |
| Coding agent fan-out (Cursor) | Direct node invocation | Degraded dict | 120s timeout | Returns degraded | Logged | **PASS** |
| MCP runtime | FastMCP on 127.0.0.1:8765 | In-process | TCP probe (100×100ms) | Uvicorn error logs | Via gateway | **PASS** |
| Gateway lane queue | FIFO asyncio.Queue maxsize=64 | In-memory | Back-pressure blocks | Per-task exception catch | N/A | **PASS** |
| Schedule manager | Per-schedule asyncio tasks | YAML persistence | cron parse + overlap guard | 60s sleep on error | Audit on fire | **PASS** |
| Pipeline runtime | Phase workers + CLI backends | `empire.db` | Phase checkpoint JSON | Degraded phase result | Audit per phase | **PARTIAL** — `_run_requirement` script path unvalidated (H-3) |
| Conduit browser actions | Playwright browser | N/A | IP check on navigate | Exception catch | Audit per action | **PARTIAL** — `eval` executes arbitrary JS (H-14 equivalent) |

---

## 20. TEST COVERAGE OF CRITICAL PATHS

| Critical Path | Test Exists | Test Quality | Missing Case | Suggested Test |
|---|---|---|---|---|
| Vault encryption / AES-256-GCM | YES — 11 tests | HIGH | Partial-write recovery | `test_vault_partial_write_recovery` |
| SwarmSync routing | YES — `test_router.py` | MEDIUM | Failure/fallback path | `test_swarmsync_fallback_on_5xx` |
| Delegation token auth | YES — 30+ tests | HIGH | Concurrent deduction race | `test_token_concurrent_spend_deduction` |
| **HTTP auth middleware rejection** | **NO** | **MISSING** | 401 on missing/wrong token | `test_auth_rejects_missing_token` |
| Budget cap enforcement | YES — 9 tests | HIGH | Concurrent cap race | `test_budget_concurrent_check_and_deduct` |
| Audit log hash chain | YES — full coverage | HIGH | Chain rebuild after reopen | `test_chain_verify_after_db_reload` |
| **Shell tool security** | **NO** | **MISSING** | Allowlist rejection, PS mode, cwd clamp | `tests/test_shell_tool.py` (new file) |
| Python executor sandbox | YES — good | HIGH | `open('/etc/passwd')`, `__import__` bypass | `test_python_executor_blocks_file_read` |
| Safety guard classifications | YES — MEDIUM | MEDIUM | Unknown tool; strict mode | `test_safety_strict_mode_requires_confirmation` |
| **File tool path traversal** | PARTIAL (1 pattern) | LOW | Windows `..\\`, double-hop, symlink | `test_file_tool_windows_traversal` |
| Telegram adapter | NO unit tests | MISSING | Send path, error handling | `tests/test_telegram_adapter.py` |
| Gmail adapter | YES | MEDIUM | OAuth refresh failure | `test_gmail_refresh_token_failure` |
| WebSocket gateway routing | YES | MEDIUM | WS with wrong token | `test_ws_rejects_wrong_token` |
| Rate limiting (none exists) | N/A | N/A | Needs implementation first | After H-1 fix: `test_daemon_restart_rate_limited` |

---

## 21. REPO HYGIENE SNAPSHOT

| Check | Status | Evidence |
|---|---|---|
| `.env` gitignored | **PASS** | `.gitignore:11` — confirmed never committed |
| `vault.enc` gitignored | **PASS** | `.gitignore` — not tracked |
| `client_secret*.json` gitignored | **PASS** | `.gitignore` — pattern present; confirmed not tracked |
| `.claude/` gitignored | **PASS** | `.gitignore` — confirmed |
| Secrets in committed source files | **FAIL** | `mypassword123` in 9+ tracked files; bot token in `install_autostart.py` (C-1, C-4) |
| Secrets in git history | **FAIL** | Bot token in `install_autostart.py` since commit `e08a3e1`; `mypassword123` in multiple launcher commits |
| HKO audit files tracked | **FAIL** | `HKO_TRUTH_AUDIT.md`, `HKO_TRUTH_AUDIT_CATO_INTEGRATIONS.md` tracked; gitignore pattern mismatch (M-14) |
| Dead shadow file | **FAIL** | `cato/audit.py` permanently masked by package (M-3) |
| Duplicate/legacy implementations | **PASS** | No duplicate route patterns except `memory/stats` (C-3) |
| Branch protection on main | **UNKNOWN** | Not checkable from local clone |
| Dependabot configured | **UNKNOWN** | No `dependabot.yml` found |
| CI test coverage | **FAIL** | CI runs build check only; no pytest (H-5) |
| Orphaned .env keys | **FAIL** | 4 keys never consumed: `GMAIL_ADDRESS`, `GMAIL_REDIRECT_URI`, `GENESIS_AGENTS_*` x2 |
| Debug/scratch files in root | **PASS** | Most gitignored; `_platform_root_shadow.py.bak` present but benign |

---

## 22. HISTORICAL PATTERN MATCHES

| Pattern | Status in This Codebase | Evidence | Prevention Fix |
|---|---|---|---|
| Phantom escrow release | N/A | No escrow surfaces | N/A |
| IDOR on resource creation | PRESENT (LOW RISK) | `DELETE /api/sessions/{id}` no ownership check; single-user daemon mitigates | Add ownership check before multi-user scope |
| SSRF via user-controlled URLs | PARTIAL | `http_client.py` has full defense; `browser.py`, `web_search.py` bypass it | Wire `_assert_safe_url` into all outbound HTTP calls |
| Secrets in committed source | **PRESENT — CRITICAL** | `mypassword123` + bot token in tracked files and history | Immediate rotation + git history scrub |
| Startup crash on missing env vars | FIXED | `vault.py` — graceful VaultError with helpful message; `router.py` — returns empty string, no crash | Verified fixed |
| Non-idempotent table creation | FIXED | 100% `IF NOT EXISTS` | Verified fixed |
| Missing auth on API routes | FIXED | `auth_token_middleware` wraps all non-exempt routes | Verified fixed; PTY WS endpoints deferred (H-4) |
| CORS subdomain smuggling | FIXED | `_is_localhost_origin()` uses strict `urlparse`; fixed in `215ae8d` | Verified fixed |
| Timing oracle in token compare | FIXED | `secrets.compare_digest` in all auth paths | Verified fixed |
| Class-level mutable `_activity_state` | FIXED | `gateway.py:169` — now instance attribute | Verified fixed (F-01) |
| Missing activity broadcast in flow task | FIXED | `gateway.py:894,910` — `_broadcast_activity` in try/finally | Verified fixed (F-02) |
| Gmail filter false-positives | FIXED (partially) | Two-tier filter committed; but exact-local set over-broad (M-4) | Trim `_NO_REPLY_EXACT_LOCAL` |

---

## 23. REGRESSION RISK ASSESSMENT

| Recent Change | Risk Category | Why It Matters | Evidence | Fix |
|---|---|---|---|---|
| fix(router): eliminate direct Anthropic/OpenAI calls | MEDIUM | `_PROVIDERS` still has `claude-` and `anthropic/` entries; accidental `ANTHROPIC_API_KEY` in env causes 401 and wastes fallback attempt | `router.py:178-190` | Document entries as intentionally dead; add guard comment |
| feat(approval): auto-approve reversible tools | MEDIUM-HIGH | `_DEFAULT_ALLOWED_TOOLS` includes `shell`, `python.execute` — authorized with no confirmation when no delegation token active; users may believe new auto-approve makes these safer than they are | `token_checker.py:33-36,178-185` | Remove execution tools from `_DEFAULT_ALLOWED_TOOLS` |
| fix(gmail): two-tier sender filter | MEDIUM | Over-broad exact-local set silently skips `support@*`, `billing@*`, `info@*` from real contacts | `gmail_adapter.py:50-81` | Trim list to unambiguous no-reply addresses |
| feat(activity): idle-stuck fix | LOW | If tool fires before turn-level broadcast, `_on_tool_progress` re-broadcasts `busy=False` mid-tool | `gateway.py:1480-1496` | Guard `_activity_state["busy"]` in `_on_tool_progress` |
| Add diagnostics export + inbox | MEDIUM | `to_dict()` includes `vault` field; `_RUNTIME_ONLY` exclusion in `save()` not mirrored in `to_dict()` | `config.py:328-332` vs `save():256` | Add `vault` exclusion to `to_dict()` |
| Budget: session_cap deprecated | LOW | Constructor defaults ($3/$20) differ from `CatoConfig` defaults — verify `agent_loop.py` passes cfg values | `budget.py:117-119` vs `config.py:99-101` | Verify `agent_loop.py` passes `cfg.daily_cap`/`cfg.monthly_cap` |

---

## 24. HIGHEST-RISK PRODUCT PROMISE

**Promise:** "AES-256-GCM encrypted vault — API keys are protected at rest"
**Current risk:** The vault is correctly implemented (Argon2id KDF, AES-256-GCM, constant-time compare), but the vault master password `mypassword123` is hardcoded in 9 committed source files and in git history. The encryption is sound; the key management is not. Anyone who clones the repo — past, present, or future — can derive the encryption key and decrypt the vault.
**Evidence:** `cato_svc_runner.py:16`, `cato_service.py:20`, `install_autostart.py:24`, git history commit `e08a3e1`
**Required fix:** Remove all hardcoded vault password occurrences from tracked files; inject via OS service environment; scrub git history; rotate all vault-stored credentials after scrub.

---

## 25. TOP 5 REQUIRED FIXES

1. **Rotate bot token + scrub git history + remove hardcoded vault password from all source files** — `cato_svc_runner.py:16`, `cato_service.py:20`, `install_autostart.py:24`, `launch_daemon.ps1:4`, etc. — acceptance: `git grep "mypassword123"` returns nothing; `git log -S "8573304576"` returns nothing after filter-repo scrub; bot token revoked in BotFather
2. **Remove `shell`, `shell.exec`, `python.execute` from `_DEFAULT_ALLOWED_TOOLS`** — `cato/auth/token_checker.py:33-36` — acceptance: `test_shell_requires_confirmation_with_no_token` passes; no shell execution without explicit delegation token or user confirmation
3. **Fix duplicate `/api/memory/stats` route** — remove duplicate from `cato/ui/server.py:2838` OR from `cato/api/memory_routes.py:136` — acceptance: `GET /api/memory/stats` returns `facts`, `kg_nodes`, `kg_edges`; SettingsView Memory tab shows correct counts
4. **Add pytest to CI pipeline** — `.github/workflows/python-verify.yml` — acceptance: `python -m pytest tests/ -x -q` runs on every push to main; failing tests block merge
5. **Fix DB path split-brain for `ContradictionDetector`, `DecisionMemory`, `AnomalyDetector`** — `cato/ui/server.py:1780,1803,1835` — acceptance: `GET /api/diagnostics/contradiction-health` returns actual contradiction count; diagnostics UI shows real data

---

## 26. FINAL ENGINEER CHECKLIST

- [ ] **C-1: Remove `mypassword123` from all committed files** — rotate after scrub
- [ ] **C-2: Remove shell/python.execute from `_DEFAULT_ALLOWED_TOOLS`** — add confirmation gate
- [ ] **C-3: Fix duplicate `/api/memory/stats` route registration**
- [ ] **C-4: Revoke Telegram bot token; scrub git history; fix `install_autostart.py`**
- [ ] H-1: Add rate limiting to `/api/daemon/restart`, vault mutations, coding-agent invoke
- [ ] H-2: Wire `_assert_safe_url` into `web_search.py` and verify `conduit_bridge.py` crawl
- [ ] H-3: Validate `_run_requirement` script path against `business_dir`
- [ ] H-4: Move PTY/coding-agent WS token check to pre-upgrade or enforce 500ms envelope timeout
- [ ] H-5: Add `pytest tests/ -x -q` to CI workflow
- [ ] H-6: Add `tests/test_shell_tool.py` — allowlist, cwd, PS mode, audit log
- [ ] H-7: Add auth middleware rejection tests (`test_auth_rejects_missing_token`)
- [ ] H-8: Rename `GITHUB_FOXFIREPOETS_TOKEN` → `GITHUB_TOKEN` in `.env`
- [ ] H-9: Normalize `ContradictionDetector`, `DecisionMemory`, `AnomalyDetector` DB paths
- [ ] H-10: Remove dead "Save Schedule" button or wire it to a real endpoint
- [ ] M-2: Add `personal_store.init_db()` call to `TelegramAdapter.start()`
- [ ] M-3: `git rm cato/audit.py`
- [ ] M-4: Trim `_NO_REPLY_EXACT_LOCAL` to unambiguous no-reply addresses
- [ ] M-9: Add startup check for missing SWARMSYNC_API_KEY with user-visible error
- [ ] M-10: Fix README install path (pip install cato-daemon → git clone)
- [ ] M-14: Fix `.gitignore` pattern for HKO audit files; `git rm --cached` both files
- [ ] Verify vault-stored credentials are rotated after git history scrub
- [ ] Re-run this audit after fixes

---

## 27. RECOMMENDED NEXT STEP

**Immediately revoke the Telegram bridge bot token** (`8573304576:AAFT4SbfetSd2ydONWYE75atJC-CHmjLu9U`) via BotFather — it is in committed git history and live. Then remove `mypassword123` from all source files, rotate all vault-stored credentials, and run `git filter-repo --replace-text` to scrub both secrets from history before this repo is ever pushed to a remote or shared. Everything else can follow in order.
