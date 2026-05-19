# HKO-Truth-Audit Report: Cato — Codex Routing+Budget Work
**Date:** 2026-05-19
**Scope:** cato/routing_log.py, cato/router.py, cato/ui/server.py,
  desktop/src/views/LogsView.tsx, tests/test_router.py,
  cato/ui/tests/test_server_lifecycle.py, cato/budget.py, cato/config.py,
  cato/cli.py, cato/agent_loop.py, tests/test_budget.py
**Mode:** HK (code) + OTA design-time + RIO (integration)
**Severity threshold:** MEDIUM

---

## Findings

### MEDIUM — SECURITY

**M-01** `config.vault` serialized to plaintext YAML on `save()`
- **Source:** HK
- **Location:** `cato/config.py:222-231` (`save()` method)
- **Description:** `CatoConfig.vault` is an `Optional[dict]` that can hold credentials (Conduit bridge login, WebSearchTool keys). `save()` iterates all non-private fields with no exclusion list, so `vault: {password: secret}` would be written to `~/.cato/config.yaml` in plaintext — outside the AES-256-GCM vault.enc.
- **Fix:** Exclude `vault` from YAML serialization in `save()`. It is a runtime-only field, not a persisted config value.
- **Status:** FIXED in this audit cycle.

---

### LOW

**L-01** `_ensure_columns` uses f-string in ALTER TABLE
- **Source:** HK
- **Location:** `cato/routing_log.py:72`
- **Description:** `f"ALTER TABLE routing_events ADD COLUMN {name} {ddl}"` — `name` and `ddl` come from a hardcoded module-level constant `_COLUMNS`. No injection risk in practice, but violates parameterized-query convention.
- **Recommended fix:** Acceptable as-is given constant source; or add assertion `assert name.isidentifier()`.

**L-02** Routing log SQLite DB has no retention policy
- **Source:** RIO
- **Location:** `cato/routing_log.py`
- **Description:** `routing_log.sqlite3` grows without bound. On a busy daemon this will fill disk over weeks.
- **Recommended fix:** Add a `DELETE FROM routing_events WHERE id NOT IN (SELECT id FROM routing_events ORDER BY id DESC LIMIT 10000)` sweep in `record_routing_event` on every 1000th write.

**L-03** `complete()` direct circuit breaker open-until not checked
- **Source:** HK (pre-existing)
- **Location:** `cato/router.py:667`
- **Description:** `_direct_cb_open_until` is set when threshold is breached but the loop entry does not check it, so the circuit does not actually short-circuit direct LLM retries.
- **Recommended fix:** Add guard at the top of the `for attempt_model in models_to_try:` loop (mirror the SwarmSync pattern).

**L-04** Dead `"swarmsync/"` prefix branch in `_get_api_key`
- **Source:** HK
- **Location:** `cato/router.py:947`
- **Description:** `"swarmsync/": "SWARMSYNC_API_KEY"` — no model name starts with `"swarmsync/"` in normal routing. Dead code.
- **Recommended fix:** Remove the entry or document its intent.

**L-05** `ModelRoutingTab` silently swallows fetch errors
- **Source:** HK
- **Location:** `desktop/src/views/LogsView.tsx:193`
- **Description:** `catch { /* silent */ }` — routing history fetch errors show no user feedback; tab appears empty with no indication of failure.
- **Recommended fix:** Set an `error` state in the catch branch and render a short error message.

---

## Task Status Table (RIO)

| Task | Status | Note |
|------|--------|------|
| Routing telemetry (routing_log.py) | implemented | SQLite persisted, server endpoint wired, LogsView consuming |
| Budget caps (budget.py) | implemented | All caps, formatting, and call log working |
| Config changes (config.py) | implemented | Vault field excluded from save() after M-01 fix |
| CLI changes (cli.py) | implemented | Entry point registered |
| Agent loop (agent_loop.py) | implemented | SwarmSync key import from swarmsync module wired |
| Server routing endpoint | implemented | `/api/usage/routing` returns `{events, log_path, fields}` |
| LogsView desktop tab | implemented | Model Routing tab consuming `/api/usage/routing` |
| Test coverage | implemented | 1804 tests passing |

---

## Causal Links

None — the MEDIUM finding (M-01) is a code-level security issue with no associated OTA or RIO failure.

---

## Crux

No structural orchestration failure found. The code is functionally correct; M-01 is a security hygiene gap in config serialization.

---

## Remediation Plan

1. **[DONE] MEDIUM code_fix `cato/config.py:save()`** — Exclude `vault` field from YAML output by adding it to a `_RUNTIME_ONLY` exclusion set inside `save()`.

2. **LOW code_fix `cato/routing_log.py`** — Add retention sweep every N writes.

3. **LOW code_fix `cato/router.py:630`** — Wire `_direct_cb_open_until` guard into `complete()` loop entry.

4. **LOW code_fix `desktop/src/views/LogsView.tsx:193`** — Replace silent catch with error state display.

5. **LOW code_fix `cato/router.py:947`** — Remove dead `"swarmsync/"` key mapping.

---

## Verification Summary

| Command | Result | Scope |
|---------|--------|-------|
| `pytest tests/test_router.py tests/test_budget.py cato/ui/tests/test_server_lifecycle.py` | 25 passed | in-scope |
| `pytest --tb=no -q` (full suite) | 1804 passed, 4 skipped | full suite |
| `python -m compileall cato/config.py cato/router.py cato/routing_log.py cato/budget.py` | OK | compile check |
