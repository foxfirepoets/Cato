# HKO-Truth-Audit Certificate: Cato — Codex Routing+Budget Work
**Date:** 2026-05-19
**Code audited:** cato/routing_log.py, cato/router.py, cato/ui/server.py,
  desktop/src/views/LogsView.tsx, tests/test_router.py,
  cato/ui/tests/test_server_lifecycle.py, cato/budget.py, cato/config.py,
  cato/cli.py, cato/agent_loop.py, tests/test_budget.py

| Layer | Findings | Critical/High |
|-------|----------|--------------|
| HK (Code) | 1 MEDIUM (fixed inline) + 4 LOW | 0 |
| OTA (Contract) | 0 — DESIGN-TIME (no transcript) | 0 |
| RIO (Integration) | 0 — all 7 integration surfaces verified | 0 |
| MULTI (overlap) | 0 | 0 |
| CAUSAL LINKs | 0 | — |
| HK Coverage | COMPLETE (inline) | — |
| OTA Coverage | DESIGN-TIME — reduced confidence | — |

**Overall result: PASS**

No CRITICAL or HIGH findings. 1 MEDIUM finding (M-01) identified and fixed in this audit cycle.

**M-01 FIXED:** `config.vault` field excluded from YAML serialization in `save()`.
Added `_RUNTIME_ONLY = frozenset({"vault"})` exclusion set in `cato/config.py`. Runtime-only
credentials no longer risk being written to plaintext `~/.cato/config.yaml`.

Test suite: **1804 passed, 4 skipped** — no regressions.

**Residual risks (even after PASS):**
1. `routing_log.sqlite3` has no retention policy — will grow unbounded on long-running deployments.
2. `_direct_cb_open_until` is set in `complete()` on threshold breach but the guard is not checked at loop entry — the direct-LLM circuit breaker accumulates state but does not actually short-circuit retries. Pre-existing behaviour.
3. `ModelRoutingTab` silently swallows fetch errors — user sees an empty table with no error message when the routing history API fails.
