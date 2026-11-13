# DairyPro: transformation report

Source: `https://github.com/aniruddha-sk/DairyMitra.git`, branch `main`,
commit `74c59a8` ("Complete DairyMitra project update", the only branch and
commit). Database: MySQL 8 only. All tests below were actually run on
13 Nov 2026 (Windows 11, Python 3.11.9, MySQL 8.0 throwaway instance,
Chrome via Playwright). No production database was used.

---

## 1. What was already in place (verified, kept)

The repository had already been hardened in its 2.0 release
(docs/DAIRYMITRA_2.0.md). I checked each claim in the code before relying on it:

* Backend already a **single `app.py`**; no `routes/`, `services/` or
  `utils/` folders exist. `push.py`, `backup_system.py`, `ai/*` were already
  merged into sections of `app.py`. Only `migrations/`, `tests/`, `tools/`
  are separate, and they stay separate (migrations must run without the app;
  tools are hand-run scripts).
* **`tempCodeRunnerFile.py` does not exist** in the repository (only listed in
  `.gitignore`), so there was nothing to remove.
* No hard-coded `SECRET_KEY` fallback (app refuses to start in production
  without a 32+ character key); debug only with `FLASK_DEBUG=1`; CSRF on
  every POST; server-side staff allow-list; hashed OTP challenges; login
  rate limits; parameterised SQL; security headers. 183 existing tests pass.

## 2. Changes by requirement

| § | Requirement | Implementation |
|---|---|---|
| 1 | Branding | Every visible "DairyMitr/DairyMitra/Dairy Mitr" → **DairyPro**, tagline *SMART DAIRY MANAGEMENT*; header, sidebar, login/signup/OTP pages, customer portal, receipts, AI assistant, e-mails, manifest, service worker, favicon, app icons. Internal identifiers (DB names, `DAIRYMITR_*` env vars, function names) unchanged; `DAIRYPRO_ENV` / `DAIRYPRO_ADMIN_EMAILS` accepted as aliases. **The DairyPro logo files were not in the repository or workspace**, so a placeholder logo (`static/images/dairypro-logo.png`, `dairypro-icon.png`, PNG icons) was created; replace those files (same names) with the official artwork. |
| 2.1 | Placeholder styling | `static/css/dairypro.css`: placeholders light grey, 50 % opacity, normal weight, smaller; typed values keep the bold 20 px style. Number spinners removed in the narrow mobile cells (they hid digits). |
| 2.2 | Cow / buffalo values | `ANIMAL_RULES` in `app.py` (cow 3.5/8.5, FAT 2.5–5.0, SNF 8.0–9.0; buffalo 6.0/9.0, FAT 5.0–11.0, SNF 8.0–10.5) enforced on save, correction, rate preview and chart creation; same rules sent to the browser (placeholders, min/max, validation, per-row switch for cow+buffalo customers). Chart editor generates per-animal ranges and base values. Existing charts stay editable (their stored axis values are allowed). |
| 3 | Multilingual | Catalog `i18n/catalog.json` (1,218 strings × mr/en/hi), `_()` in templates, `_t()` in Python, `t()` in JS (`/i18n/<lang>.js`), JSON API messages and flash messages translated centrally; language switch in header, login pages and Settings; stored in session, 1-year cookie and on the account (users/staff/vendors). Receipts and printouts follow the language. |
| 4 | Receipt printing | `static/js/dp-receipt-print.js` + `static/css/dp-print.css`: Simple 4 per A4 (2×2), FAT/SNF 2 per A4; per-receipt font fitting (100 % down to ~6.5 pt); receipts that cannot fit get their own page, and if still too long print across pages (never clipped); customer filter on screen and on the server (`1-10, 15, 17, 36-56`, validated with clear errors). Receipt structure unchanged (customer number added to the Simple receipt header). |
| 5 | DD/MM/YYYY | `dmy` Jinja filter, all report/receipt/table dates, JS-rendered advance dates; every `<input type="date">` shows a DD/MM/YYYY field (`dp-date.js`) while still submitting ISO dates (no day/month confusion, storage unchanged). UTC `date.today()`/`CURDATE()` replaced by IST. |
| 6 | Razorpay | Section "DAIRYPRO" at the end of `app.py`: plans, trial, orders, verify, webhook, reconciliation, refunds, access control, admin page; templates `subscription.html`, `admin/subscriptions.html`. Details in §4. |
| 7 | Offline | `static/js/dp-offline.js` (queue + sync engine + UI), rewritten `static/service-worker.js`, offline page, server contract changes. Details in §5. |
| 8 | Custom dialogs | `static/js/dp-ui.js`: toasts, alert/confirm/prompt dialogs (Promise based, keyboard accessible), declarative `data-confirm` for forms/links. No native `alert/confirm/prompt` call remains (checked by grep and by the browser smoke test, which fails on any); destructive actions use a red confirm button. |
| 9 | Security | See §6. |
| 10 | Stale code | `tempCodeRunnerFile.py` absent. Removed, after checking nothing references them: `static/js/fsn_collection.js` (unused duplicate of the collection page logic), `static/js/receipt.js` (unused print button), `static/js/script.js` (targets classes no template uses). |
| 11 | Single app.py | Already the case; new backend code added as sections of `app.py`. |
| 12 | Icons | One self-hosted Lucide sprite (`static/icons/dp-icons.svg`, 24 KB) via `dp-icons.js`; existing `data-lucide`, Font Awesome and Iconify markup is rendered from it (works offline, consistent stroke). Functional emoji replaced by icons. Edit / Delete / View actions are labelled buttons with icons and confirmations (customer list, staff, rates, charts, entries). |
| 13 | Performance / cost | No new services or runtime dependencies (Razorpay via urllib). CDNs removed (Bootstrap self-hosted only for auth pages, fonts non-blocking). Background requests minimised: sync ping only when entries are waiting, reminder check once per session, page warm-up at most every 6 h. New indexes for idempotency keys and notifications. Request-level caching of subscription/settings state. Financial data is never cached by the service worker. |

## 3. Files

**Added**: `migrations/m002_dairypro.py`; `i18n/catalog.json`, `i18n/sources/*.json`;
`static/js/dp-ui.js, dp-icons.js, dp-date.js, dp-offline.js, dp-receipt-print.js, dp-ai.js`;
`static/css/dairypro.css, dp-print.css, dp-ai.css`; `static/icons/dp-icons.svg`,
`apple-touch-icon.png`, `favicon-32.png`, `icon-maskable-512.png`;
`static/images/dairypro-logo.png`, `dairypro-icon.png`;
`static/vendor/bootstrap-5.3.3.min.css`; `templates/offline.html`,
`subscription.html`, `admin/subscriptions.html`; `tests/test_dairypro.py`,
`tests/test_i18n.py`, `tests/browser/{smoke,offline_e2e,print_check}.py`;
`tools/i18n_*.py`, `tools/check_inline_js.py`, `tools/seed_browser_tests.py`;
`docs/DAIRYPRO.md`.

**Modified**: `app.py`; all templates (translation, icons, dialogs, dates,
branding); `static/service-worker.js`, `manifest.json`, `icon-192/512.png`,
`fsn_chart_editor.js`, `fsn_common.js`, `csrf.js`, CSS headers; `README.md`,
`.env.example`, `.gitignore`, `requirements-dev.txt`; tests adjusted where the
business rules or default language changed (`conftest.py`,
`test_fat_snf_integration.py` two values, `test_security.py` token count,
`test_regression.py` translated message).

**Removed**: the three unused JS files listed in §2.

## 4. Subscriptions (Razorpay)

New tables (migration 002): `dp_plans` (monthly 39900, six_months 119900,
yearly 199900 paise; 33 % / 44 % savings), `dp_trials` (one per e-mail hash,
survives account deletion so the trial cannot be repeated), `dp_payment_orders`
(unique receipt, Razorpay order id and payment id), `dp_payment_attempts`,
`dp_subscriptions` (one period per paid order, `UNIQUE(order_id)`),
`dp_webhook_events` (`UNIQUE(event_id)`), `dp_reminders`.

| Scenario | Behaviour (tested) |
|---|---|
| A success | signature verified, Razorpay API confirms *captured* + exact amount + same order → period activated |
| B back/close | attempt recorded, order stays retryable, nothing activated, repeated clicks reuse the order |
| C money taken, no answer | order kept *pending* (never failed, never re-charged); webhook or *Check payment status* / page visit reconciles via `GET /orders/{id}/payments` |
| D duplicate callback | row lock + already-paid check + `UNIQUE(order_id)` → one period |
| E webhook replay | event id stored once; replay returns `duplicate`; a different event for the same payment returns `already_paid` |
| F failure | failure reason stored and shown; retry allowed |
| G refund | only `refund.processed` changes state; full refund ends the period (`refunded`), partial refund only recorded |

Also: client price/duration ignored, amount and currency mismatches rejected,
another owner's order cannot be verified, staff cannot pay, webhook is
CSRF-exempt but HMAC-verified, rate limits on order/reconcile.
Access: see README §12 (no lock-out unless Razorpay is configured; trial for
existing dairies starts at their first login after the upgrade).

## 5. Offline: the 12 known bugs

| # | Bug | Fix | Verified by |
|---|---|---|---|
| 1 | local copy deleted after any HTTP answer | deleted only after `{"saved": true}`; synced copies kept 3 days | e2e 1f/1g, `classify()` |
| 2 | anomaly warning treated as saved | server returns 409 `needs_confirmation`, entry parked for review | `test_anomaly_warning_is_not_a_save`, e2e 2c/3e |
| 3 | IndexedDB open failures ignored | open timeout/error → "Could not save on this device" + warning toast | code path in `openDB()` |
| 4 | "saved" before transaction completed | messages shown only after `transaction.oncomplete` | e2e 1a/1b |
| 5 | duplicate detection showed success | dedupe inside the same transaction, `duplicate_local` error shown | e2e 4 |
| 6 | expired session lost data | JSON 401 instead of login redirect; `redirect: manual`; entry kept | `test_expired_session_is_json_401`, e2e 2b |
| 7/8 | records not tied to owner / synced under another owner | tenant id on every record, `/api/session` check before each run, server `X-DP-Tenant` check (409) | `test_tenant_mismatch_saves_nothing`, e2e 3b/3d |
| 9 | Background Sync without handler | `sync` event in the service worker runs the same engine | code (cannot be triggered from automation) |
| 10 | concurrent sync loops | Web Locks (`dairypro-sync`) with IndexedDB lease fallback, single-flight per tab | code review |
| 11 | offline navigation to uncached `/` | worker served from `/` scope (it was registered under `/static/`, so it never controlled pages), network-first pages cached per account, `/offline` fallback | e2e 1d |
| 12 | duplicate submissions | idempotency keys (`client_request_id` / `request_id`) with unique indexes; replay returns the stored entry | `test_explicit_ack_and_idempotent_replay`, `test_advance_idempotent`, e2e 1h |

Conflicts: deleted customer → 404 kept for review; duplicate slot from
another device → 409 kept for review (never overwritten); rate chart changed →
server recalculates the rate at sync; validation errors are not retried
automatically; temporary errors retry with exponential backoff (max 8, then
manual retry). Old `DairyMitrOfflineDB` entries are migrated *for review*
(they carry no owner id, so they never sync without confirmation).

## 6. Security

* CSP tightened: no third-party scripts except Razorpay Checkout; icon /
  Bootstrap / Lucide CDNs removed.
* New endpoints: CSRF on all POSTs (webhook uses HMAC instead), JSON 401/402/403
  instead of redirects for APIs, owner-only payment actions, rate limits.
* Tenant isolation extended to offline sync (header check) and service-worker
  page cache (per owner / staff / customer scope; deleted on logout).
* Dates/quantities/milk types validated on the litre-rate and food-sack
  endpoints (previously unvalidated); food-sack endpoints no longer 500 on
  another dairy's customer id.
* Secrets: none in the code or `.env.example`. **Operational step still
  required**: rotate `SECRET_KEY` (invalidates old sessions) and the VAPID key
  pair as described in docs/DAIRYMITRA_2.0.md §8.
* Dependency audit: 0 known vulnerabilities in application dependencies (§7).

The application is not claimed to be "100 % secure": only the tests listed
below were run; there was no external penetration test.

## 7. Test results (run 13 Nov 2026)

| Suite | Result |
|---|---|
| `pytest tests` (MySQL 8, all server tests incl. 59 new DairyPro + 44 i18n tests) | **286 passed** |
| `tests/browser/smoke.py` – 41 pages × desktop + 390 px phone | no JS errors, no app request failures, no horizontal page overflow, no un-rendered icons, no native dialogs (only third-party YouTube/Maps iframe requests aborted on navigation) |
| `tests/browser/offline_e2e.py` | **20/20** |
| `tests/browser/print_check.py` (14 customers, heavy food-sack/advance data) | **11/11** – Simple 14 receipts → 5 A4 pages (1 long receipt alone), FAT/SNF 14 → 8 pages (2 long), PDF page count = layout, nothing clipped, filter and invalid range handled |
| `tools/check_inline_js.py` | 0 syntax errors in all inline and static scripts |
| `pip-audit --local` (all pinned runtime + dev packages installed in the venv) | **0 known vulnerabilities in application dependencies**; the only findings were in the local venv's own `pip`/`setuptools` installers, which are not part of the app (Railway installs its own) |

## 8. Known limitations

* **Logo**: placeholder artwork until the official DairyPro files are added.
* **Razorpay**: implemented against Razorpay's documented API and tested with
  a mocked gateway; it has **not** been run against Razorpay test mode
  (no keys were available here). Run the README §10 checklist before going live.
* Recurring auto-debit (Razorpay Subscriptions API) is not used: plans are
  prepaid periods; renewal is a new payment that extends the current period.
* Offline: first-ever visit needs internet; pages not opened online are not
  available offline; data shown on cached pages is a snapshot. Finalising a
  FAT/SNF slot, advance deductions/edits, edits of existing entries, payments,
  OTP, SMS need internet. Background Sync works only in Chromium browsers
  (others sync when the app is opened).
* Customer names and addresses are not translated (user data). SMS/e-mail
  texts to farmers are unchanged (Marathi), since they are not tied to the
  owner's UI language.
* Phone layout of the FAT/SNF collection table scrolls horizontally inside
  its card (existing design).
* `tests/browser/*` need a running demo server, Chrome and Playwright; they
  are not part of `pytest tests`.

## 8b. Rollback

Code: redeploy the previous version; migration 002 is additive (new nullable
columns and new tables), so the old code runs on the migrated database.
Full: restore the pre-migration dump.
