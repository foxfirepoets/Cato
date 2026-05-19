# HKO-Truth-Audit Certificate: Cato — Ben Assistant Integration
**Date:** 2026-05-19
**Code audited:** cato/adapters/gmail_adapter.py (NEW), cato/core/personal_store.py (NEW),
  cato/adapters/telegram.py (MODIFIED), cato/cli.py (MODIFIED),
  pyproject.toml (MODIFIED), tests/test_personal_store.py (NEW),
  tests/test_gmail_adapter.py (NEW)

| Layer | Findings | Critical/High |
|-------|----------|--------------|
| HK (Code) | 2 HIGH (fixed) + 1 MEDIUM (fixed) + 1 LOW (fixed) | 0 after fixes |
| OTA (Contract) | 0 — DESIGN-TIME (no transcript) | 0 |
| RIO (Integration) | 4 integration surfaces verified | 0 |
| MULTI (overlap) | 0 | 0 |
| CAUSAL LINKs | 0 | — |
| HK Coverage | COMPLETE (all 4 findings fixed inline) | — |
| OTA Coverage | DESIGN-TIME — reduced confidence | — |

**Overall result: PASS**

No CRITICAL or HIGH findings remain. All findings identified and fixed in this audit cycle.

---

## Findings and Fixes

**HIGH-1 FIXED:** `_capture_note` in `cato/adapters/telegram.py` sent `reply_text("Saved as todo.")`
to every free-text message. This caused double replies alongside the agent loop response.
Fix: removed `reply_text` call; note capture is now silent (logger.debug only).

**HIGH-2 FIXED:** `asyncio.ensure_future(gmail.start())` in `cato/cli.py` was used inside a
running event loop where `asyncio.create_task()` is the correct API for proper task tracking
and cancellation semantics. Fixed: replaced with `asyncio.create_task(gmail.start())`.

**MEDIUM-1 FIXED:** `_do_check()` in `gmail_adapter.py` built the Gmail service twice per poll
cycle — once explicitly and once inside `_fetch_unread_emails_sync()`. Each build triggers an
OAuth token refresh. Fixed: `_fetch_unread_emails_sync(service)` now accepts a pre-built service
argument; `_do_check` builds it once and passes it via `run_in_executor`.

**MEDIUM-2 FIXED:** `_cb_email_action` in `telegram.py` used `edit_message_text()` with no
`parse_mode`, but the original notification was sent with `parse_mode="HTML"`. HTML-special chars
in email subjects or bodies would cause Telegram API parse errors. Fixed: replaced all
`edit_message_text` calls in `_cb_email_action` with `edit_message_reply_markup(reply_markup=None)`
+ `query.answer()` toast pattern.

**LOW-1 FIXED:** `_extract_plain_text()` in `gmail_adapter.py` iterated multipart parts in
declaration order — if `text/html` appeared before `text/plain`, HTML was returned instead of
plain text. Fixed: two-pass scan — first pass collects `text/plain`, second pass falls back to
other types.

---

## RIO Integration Surfaces Verified

1. **GmailAdapter ↔ personal_store**: `_process_email` saves to `personal_store.save_email()`;
   `_cb_email_action` reads via `get_email_by_id()` and updates via `update_email_status()`.
   Atomic claim path `claim_email_for_send()` prevents double-send. Verified: test coverage exists.

2. **GmailAdapter ↔ ModelRouter**: `_is_marketing_email` and `_draft_email_reply` call
   `self._router.complete_message()` with model hints. Router is wired from `cli.py` startup.
   Verified: router.py `complete_message` API signature matches call sites.

3. **TelegramAdapter ↔ GmailAdapter**: `tg._gmail_adapter = gmail` wired in `cli.py`.
   `_cb_email_action` calls `self._gmail_adapter.send_draft(gmail_draft_id)`. Verified: wiring
   is guarded by `if tg is not None and vlt is not None` in cli.py.

4. **cli.py startup wiring**: GmailAdapter and ModelRouter instantiated, cross-wired, and
   `asyncio.create_task(gmail.start())` launched in async startup. Verified: try/except guards
   best-effort startup; failure logs warning and does not crash daemon.

---

## Residual Risks

1. **`_telegram_chat_id` not populated at startup**: Scheduled 15-min Gmail polls cannot send
   Telegram notifications until the user first sends `/check` to the bot (which sets
   `_telegram_chat_id`). A daemon restart clears the in-memory value. Workaround: store a
   known chat_id in vault/config and load it at startup.

2. **Gmail OAuth refresh token rotation**: If the refresh token is revoked or expired, all
   Gmail polling silently falls back with a logged error. No user notification path for this
   failure mode exists.

3. **`personal.sqlite3` has no retention policy**: Emails and notes accumulate indefinitely.

---

Test suite: **1819 passed, 4 skipped** — no regressions introduced by the Ben Assistant integration.
