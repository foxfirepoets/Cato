# HKO-Truth-Audit Report: Cato — Post-Fix Verification
**Date:** 2026-05-18
**Scope:** cato/router.py, cato/api/websocket_handler.py, cato/api/pty_routes.py,
  desktop/src/views/SettingsView.tsx, desktop/src/components/TerminalPane.tsx,
  desktop/src/hooks/useTalkPageStream.ts
**Task docs:** HKO-audit-output/HKO-truth-audit-report.md (prior run), CATO_ALEX_AUDIT.md, CATO_KRAKEN_VERDICT.md
**Severity threshold:** HIGH
**OTA mode:** DESIGN-TIME (no transcript; supporting skill not file-backed)
**HK mode:** COMPLETE (inline — supporting skill not file-backed)
**RIO mode:** COMPLETE

---

## Summary

This audit verifies that all 5 findings from the prior HKO run (2026-05-18) were genuinely
fixed by the O2O remediation pipeline, and checks for new issues introduced by the fixes.

All 5 original findings are VERIFIED FIXED. One new LOW finding (token null-coercion, NEW-HK-1)
was identified and fixed inline during this audit run. All 1803 tests pass.

---

## Findings

### [FIXED] F-01 — CRITICAL (prior run) · SettingsView config corruption
**Status: FIXED**

`desktop/src/views/SettingsView.tsx:83–91` — `case 'general':` branch is now present in
`loadSettings()`. It fetches `/api/config` and calls `setDefaultModel(data.default_model || '')`
and `setWorkspacePath(data.workspace_dir || '')`.

`handleSaveConfig()` (lines 167–170) now uses conditional spreads:
```typescript
...(workspacePath ? { workspace_dir: workspacePath } : {}),
...(defaultModel ? { default_model: defaultModel } : {}),
```
Empty strings are no longer sent to `PATCH /api/config`. Config corruption is eliminated.

**Verification:** Code confirmed at `SettingsView.tsx:83–91, 167–170`.

---

### [FIXED] F-03 — MEDIUM (prior run) · Unredacted secrets in routing_log SQLite
**Status: FIXED**

`cato/router.py:501–507` — success path now applies `_scrub_log_text()` to both
`raw_model` and `routing_reason` before passing to `_record_routing_decision()`:
```python
"raw_model": _scrub_log_text(raw_model),
"routing_reason": _scrub_log_text(swarmsync_meta.get("routing_reason") or ...),
```

`cato/router.py:538` — HTTP error path applies `_scrub_log_text(body[:500])` to `error` field.

`cato/router.py:559` — exception path applies `_scrub_log_text(str(exc))` to `error` field.

**Verification:** Code confirmed at `router.py:501,502,538,559`.

---

### [FIXED] F-04 — MEDIUM (prior run) · Shared circuit breaker counter
**Status: FIXED**

`cato/router.py:294–300` — `ModelRouter.__init__` now initialises two independent counters:
```python
self._ss_cb_failures: int = 0       # SwarmSync API path
self._ss_cb_open_until: float = 0.0
self._direct_cb_failures: int = 0   # direct provider path
self._direct_cb_open_until: float = 0.0
```
Old `self._cb_failures`/`self._cb_open_until` are gone. `_swarmsync_complete_message` uses
`_ss_cb_*` exclusively; `complete()` uses `_direct_cb_*` exclusively. Three SwarmSync failures
no longer block the direct-provider fallback chain.

**Verification:** `grep -n "_cb_failures" cato/router.py` shows only `_ss_cb_failures` and
`_direct_cb_failures`; `_cb_failures` absent. Spot-check assertion: `assert not hasattr(r, '_cb_failures')` passes.

---

### [FIXED] F-05 — LOW (prior run) · WS token in URL query parameter
**Status: FIXED**

Frontend:
- `TerminalPane.tsx:47` — `wsUrl` built without `?token=`; auth sent via `ws.onopen` at line 79.
- `useTalkPageStream.ts:235` — URL built without `?token=`; auth sent via `ws.onopen` at line 242.

Backend:
- `cato/api/websocket_handler.py:204–220` — auth check gated on `if daemon_token:`. When no
  daemon token is configured (tests, dev), the auth block is skipped entirely — no 5-second
  blocking wait. Tokens accepted from `X-Cato-Token` header, `?token=` query param (legacy),
  or first-message `{type:"auth",token}` envelope.
- `cato/api/pty_routes.py:182–196` — identical gating.

**Test regression fix:** The original Ben implementation blocked all unauthenticated WS connections
for 5 seconds even in test environments (no daemon_token). Fixed by gating the entire auth flow
on `if daemon_token:`. 4 previously-failing tests now pass.

**Verification:** `tests/test_websocket_handler.py` and `tests/test_pty_routes_integration.py` —
74 passed.

---

### [FIXED] F-06 — LOW (prior run) · `_is_model_slug_only` false positive
**Status: FIXED**

`cato/router.py:187–206` — function now requires a known provider prefix:
```python
_KNOWN_MODEL_PROVIDERS = frozenset([
    "openrouter", "anthropic", "openai", "google", "deepseek",
    "groq", "mistral", "minimax", "moonshot", "meta-llama",
])
def _is_model_slug_only(text: str) -> bool:
    ...
    if "/" not in t: return False
    provider = t.split("/")[0].lower()
    return provider in _KNOWN_MODEL_PROVIDERS and re.match(r"^[\w\-./]+$", t) is not None
```
`"A/B"`, `"I/O"`, `"Yes/No"` all return `False` (provider not in set).

**Verification:** Spot-check assertions all pass.

---

### [NEW — FOUND AND FIXED] NEW-HK-1 — LOW · WS auth token null-coercion
**Status: FIXED inline during this audit**

`cato/api/websocket_handler.py:214` and `cato/api/pty_routes.py:191`:

Original (introduced by F-05 fix):
```python
token = parsed.get("token", "")
```
When a client sends `{"type":"auth","token":null}`, `parsed.get("token", "")` returns `None`
(the default is only used when the key is absent, not when it is `null`). Then:
```python
secrets.compare_digest(None, daemon_token)  # TypeError — unhandled
```
This crashes the WebSocket handler. Only exploitable when `daemon_token` is configured (production
mode). Impact: connection handler exception, connection closes; no information disclosure.

Fix applied:
```python
token = str(parsed.get("token") or "")
```
`or ""` coerces `None`/`0`/`False` to `""`. `str()` ensures the type is always `str` before
`compare_digest`.

**Verification:** `tests/test_websocket_handler.py` 53 passed after fix.

---

## Task Status Table

| Task | Status | Note |
|------|--------|------|
| F-01 · SettingsView General tab case 'general' in loadSettings() | implemented | `SettingsView.tsx:83–91, 167–170` |
| F-03 · _scrub_log_text on routing_reason, raw_model, error | implemented | `router.py:501,502,538,559` |
| F-04 · Split _cb_failures → _ss_cb_* / _direct_cb_* | implemented | `router.py:294–300, 493, 523, 540, 545, 629, 641–643` |
| F-05 · Token moved from URL to first-message auth | implemented | `TerminalPane.tsx:79`, `useTalkPageStream.ts:242`, `websocket_handler.py:204–220`, `pty_routes.py:182–196` |
| F-05 regression · Auth 5s block in test envs | implemented | Gated on `if daemon_token:` in both handlers |
| F-06 · _is_model_slug_only provider-prefix gating | implemented | `router.py:187–206` |
| NEW-HK-1 · WS token null-coercion TypeError | implemented | `websocket_handler.py:214`, `pty_routes.py:191` |
| Test suite 1803 pass | implemented | 1803 passed, 4 skipped, 4 deselected |

---

## Deduplication Log

No cross-layer deduplication required — all HK findings are independent code-level issues.
OTA and RIO found no additional findings to merge.

---

## Causal Links

No new causal links. The NEW-HK-1 finding is a standalone code bug in the F-05 fix; it does not
causally explain any contract or integration failure in the task docs.

---

## Crux

OTA design-time: No structural failure found. All claimed fixes are verified in the code. The O2O
pipeline delivered authentic, complete fixes for all 5 prior findings. One new LOW finding was
introduced by the F-05 implementation and has been corrected.

---

## Remediation Plan

All findings are FIXED. No open remediation items.

### Completed (this audit)
**NEW-HK-1 · code_fix · `cato/api/websocket_handler.py:214` + `cato/api/pty_routes.py:191`**
Changed `parsed.get("token", "")` → `str(parsed.get("token") or "")` to prevent TypeError
when `"token": null` is sent in the auth envelope. Both files updated.

---

## Verification Summary

| Command | Result | Scope | Note |
|---------|--------|-------|------|
| `python -m pytest tests/test_router.py tests/test_swarmsync*.py -q` | 9 passed | in-scope | Router + SwarmSync fixes verified |
| `python -m pytest tests/test_websocket_handler.py tests/test_pty_routes_integration.py tests/test_coding_agent_integration.py -q` | 74 passed | in-scope | WS auth regression + F-05 fixes verified |
| `python -m pytest -q` (full suite) | 1803 passed, 4 skipped | full | No regressions from any fix |
| `python -m compileall cato/router.py` | passed | in-scope | No syntax errors |
| Static analysis · SettingsView.tsx:83–91 | `case 'general':` present | in-scope | F-01 verified |
| Static analysis · router.py:294–300 | `_ss_cb_failures`/`_direct_cb_failures` present; `_cb_failures` absent | in-scope | F-04 verified |
| Spot-check assertions | all passed | in-scope | F-04, F-06 behaviorally verified |
