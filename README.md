# DairyPro — SMART DAIRY MANAGEMENT

DairyPro (formerly DairyMitra) is a multilingual, offline-capable SaaS for
milk collection centres: customers (farmers), litre-rate or FAT/SNF milk
collection, food sacks, advances, payments, printable receipts, staff and a
customer portal, with Razorpay subscriptions.

Detailed change log and test results: [docs/DAIRYPRO.md](docs/DAIRYPRO.md).
History of the 2.0 release: [docs/DAIRYMITRA_2.0.md](docs/DAIRYMITRA_2.0.md).

---

## 1. Project overview

* One Flask application (`app.py`, organised in clearly marked sections),
  Jinja templates, plain JavaScript, MySQL 8.
* Multi-tenant: every dairy owner's data is isolated by `user_id`; staff and
  customers work inside their owner's dairy.
* Progressive Web App: installable, opens offline after the first visit and
  stores milk, FAT/SNF, food-sack and advance entries on the device until
  they can be synchronised.

## 2. Features

| Area | What it does |
|---|---|
| Milk collection | Litre × rate, or FAT/SNF rate charts (separate cow / buffalo rules) |
| FAT/SNF | Cow: standard 3.5 / 8.5, FAT 2.5–5.0, SNF 8.0–9.0. Buffalo: standard 6.0 / 9.0, FAT 5.0–11.0, SNF 8.0–10.5 (validated in the browser and on the server) |
| Customers | Add / edit / delete with confirmation, bank details, customer portal login |
| Advance & food sack | Ledger with balance checks, food-sack rates with history |
| Accounts | Calculation, payment, bank report (Excel), vendor reports, milk summary |
| Receipts | Simple receipts 4 per A4 page, FAT/SNF receipts 2 per A4 page, automatic font fitting, customer-number filter (`1-10, 15, 36-56`) |
| Languages | Marathi (default), English, Hindi — every page, message and receipt |
| Dates | DD/MM/YYYY everywhere on screen (stored as ISO dates) |
| Offline | Installable PWA, offline pages, device queue with safe synchronisation |
| Subscriptions | 20-day free trial, Monthly ₹399 / 6 months ₹1,199 / Yearly ₹1,999 via Razorpay |
| Security | CSRF, server-side roles, rate limits, hashed OTPs, CSP, tenant isolation, webhook signatures |

## 3. Technology stack

Python 3.11, Flask 3.1, Flask-WTF (CSRF), Flask-MySQLdb / mysqlclient,
gunicorn, MySQL 8.0. Front end: Jinja2 templates, vanilla JavaScript,
self-hosted Lucide icon sprite, Bootstrap 5.3.3 (auth pages only, self-hosted),
Service Worker + IndexedDB. Razorpay is called with the Python standard
library (no extra SDK).

## 4. MySQL database configuration

DairyPro uses **MySQL only** (utf8mb4). Set `MYSQL_HOST`, `MYSQL_PORT`,
`MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DB`. On Railway copy the MySQL
plugin's `MYSQLHOST`, `MYSQLPORT`, `MYSQLUSER`, `MYSQLPASSWORD`,
`MYSQLDATABASE` into those names. Give the application user
SELECT/INSERT/UPDATE/DELETE; only the migration step needs DDL rights.

## 5. Environment variables

All variables are listed with explanations in [.env.example](.env.example).
Required in production: `SECRET_KEY` (64 hex chars) and the five `MYSQL_*`
variables. For payments: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
`RAZORPAY_WEBHOOK_SECRET`. Existing `DAIRYMITR_*` variable names keep working.

## 6. Local installation

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env            # then set SECRET_KEY, MYSQL_*, DAIRYPRO_ENV=development, SESSION_COOKIE_SECURE=0
python -m migrations              # create / update the schema (safe to re-run)
python app.py                     # http://localhost:8000
```

## 7. Database setup

New database: create it (`CREATE DATABASE dairypro CHARACTER SET utf8mb4`),
load `schema.sql`, then run `python -m migrations`.
Existing DairyMitra database: **do not** load `schema.sql`; only run the
migrations (section 8). Demo data (synthetic, refuses unless the database
name ends with `_demo`): `MYSQL_DB=dairypro_demo python tools/seed_demo.py`
— login `owner@demo.local` / `Demo@1234`.

## 8. Migration instructions

Migrations are additive: they add tables, columns and indexes, never drop or
rewrite data, and are re-runnable.

```bash
mysqldump --single-transaction --routines --triggers -h HOST -u USER -p DB > before_dairypro.sql
python -m migrations --status      # shows 001 / 002 applied or PENDING
python -m migrations --dry-run     # what would run
python -m migrations               # apply
```

Migration **002 (DairyPro)** adds: `preferred_language` on users / staff /
vendors; `client_request_id` (+ unique index) on `milk_collection` and
`advance_transactions` for duplicate-free offline sync; an index on
`customer_notifications`; and the tables `dp_plans` (seeded with the three
plans), `dp_trials`, `dp_payment_orders`, `dp_payment_attempts`,
`dp_subscriptions`, `dp_webhook_events`, `dp_reminders`.
Rehearse on a copy of production first (see docs/DAIRYPRO.md §Rollback).

## 9. Railway deployment

1. Create a service from this repository; add the MySQL plugin.
2. Variables: everything from section 5 (`DAIRYPRO_ENV=production`,
   `SESSION_COOKIE_SECURE=1`, `TRUST_PROXY_HOPS=1`, `WEB_CONCURRENCY` ≈ 2×vCPU).
3. Pre-deploy command: `python -m migrations` (Settings → Deploy).
   Start command is in `Procfile`: `gunicorn -c gunicorn.conf.py app:app`.
4. Python version: `runtime.txt` / `.python-version` (3.11).
5. After deploy: `GET /healthcheck?deep=1` must return
   `{"database":"ok","migrations":"up_to_date"}`.
6. Configure the Razorpay webhook (section 10/11) with your Railway domain.

## 10. Razorpay test configuration

1. Razorpay Dashboard → *Test mode* → API keys → set `RAZORPAY_KEY_ID`
   (`rzp_test_…`) and `RAZORPAY_KEY_SECRET`.
2. Webhooks → add `https://<domain>/razorpay/webhook`, secret → set
   `RAZORPAY_WEBHOOK_SECRET`; events: `payment.captured`, `order.paid`,
   `payment.failed`, `refund.processed`.
3. Test checklist (Settings → Subscription): successful card/UPI payment;
   failed payment (test card that fails); closing the window (Back);
   *Check payment status* after closing the tab mid-payment; a webhook
   delivered twice (Dashboard → resend) must not create a second period;
   full refund ends the period, partial refund does not.

## 11. Razorpay live configuration

Switch to live keys (`rzp_live_…`), create a live-mode webhook with a new
secret, redeploy, then make one real ₹399 payment and refund it from the
Dashboard to confirm the flow. Never put keys in code or in `.env.example`.

## 12. Subscription management

* Trial: 20 days from the dairy's **first successful login** (server clock,
  stored in `dp_trials`; one trial per e-mail address, never restarted).
* Reminders: trial started, 10, 5, 3, 1 days left, expired — each shown once
  per account; a banner appears in the last 5 days.
* Payment: order created on the server with the server's price; activation
  only after Razorpay reports a **captured** payment of the exact amount
  (checkout callback, signed webhook or reconciliation — whichever is first,
  exactly once). Pending payments are re-checked automatically.
* Access: owners and staff of an expired dairy can still log in, open
  Settings / Account / Subscription and pay; other features return to the
  subscription page (HTTP 402 for APIs). The customer portal stays readable.
  Enforcement starts only when Razorpay keys are configured
  (`DAIRYPRO_ENFORCE_SUBSCRIPTION=auto`), so existing dairies are not locked
  out of an installation where they cannot pay; existing dairies get the
  20-day trial from their first login after the upgrade.
* Platform administrators: `/admin/subscriptions` (all dairies, payments,
  manual reconciliation).

## 13. Offline functionality

* After one online visit the app shell (CSS, JS, icons, translations, offline
  page) is cached; pages you opened are kept **per account** for offline
  reading (marked "offline copy saved on …").
* Milk (litre and FAT/SNF), food-sack and advance entries made offline are
  stored in IndexedDB with the dairy id, user, idempotency key, timestamps,
  status and retry count. Messages: *Saved on this device* → *Waiting for
  synchronization* → *Successfully synchronized* / *Synchronization failed*.
* Synchronisation runs automatically (back online, page open, every minute
  while entries wait, Background Sync where the browser supports it) — one
  run at a time across tabs. Entries leave the device only after the server
  confirms `{"saved": true}`; warnings, duplicates, invalid data and expired
  sessions keep the entry for review (header status button → *Offline
  entries*: Confirm / Retry / Discard).
* Another dairy logging in on the same device never sends the first dairy's
  entries; the server rejects a mismatched dairy id as well.
* Payments, OTP, SMS, finalising a slot, advance deductions/edits and other
  balance-dependent actions need internet and say so.

## 14. PWA installation

Android Chrome: menu → *Install app* (or the install banner). iPhone Safari:
Share → *Add to Home Screen*. Desktop Chrome/Edge: install icon in the
address bar. The app must be opened online once after installation.

## 15. Multilingual system

* Languages: मराठी (default), English, हिन्दी — header switcher and
  Settings → Language; saved on the account and in a 1-year cookie.
* Catalog: `i18n/catalog.json` (one entry per string with `mr`, `en`, `hi`);
  any of the three texts can be used as the key, so templates keep their
  original text: `{{ _('दूध संकलन') }}`, `_t("Invalid OTP.")`, `t("…")` in JS.
* Adding text: wrap it, write its translations in `i18n/sources/*.json`
  (`en_*.json`: `{english: [marathi, hindi]}`, `mr_*.json`:
  `{marathi: [english, hindi]}`), then run `python tools/i18n_extract.py
  --update --report` (must report `incomplete: 0`). `tests/test_i18n.py`
  fails on any untranslated key or Marathi text left on an English page.
* User data (names, addresses) is never translated.

## 16. Backup and recovery

* Database: scheduled `mysqldump --single-transaction` (Railway MySQL
  backups or a cron job); keep copies off the server. Restore with `mysql DB < file.sql`.
* Whole-database backup/restore pages are limited to platform administrators.
* Offline entries live in the browser until synchronised; do not clear site
  data while the header shows waiting entries.

## 17. Troubleshooting

| Symptom | Check |
|---|---|
| App refuses to start | `SECRET_KEY` missing/short in production |
| `/healthcheck?deep=1` says pending | run `python -m migrations` |
| Payments say "not configured" | `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` |
| Payment done but plan not active | Subscription → *Check payment status*; webhook URL/secret; `dp_webhook_events.result` |
| Offline entries not syncing | header status → *Offline entries*: session expired (log in again), needs confirmation, or belongs to another dairy |
| Old icons / styles after deploy | bump `DP_ASSET_VERSION` in `app.py` and `VERSION` in `static/service-worker.js` |
| A label appears untranslated | `DAIRYPRO_I18N_DEBUG=1` logs missing strings; add them as in §15 |

## 18. Testing instructions

```bash
# Server tests: need a DISPOSABLE MySQL 8 server (database dairymitr_pytest is dropped!)
TEST_MYSQL_HOST=127.0.0.1 TEST_MYSQL_PORT=3399 TEST_MYSQL_USER=root pytest tests

# Browser tests (Chrome + Playwright: pip install playwright), against a running demo server
python tests/browser/smoke.py        http://127.0.0.1:8765          # every page, desktop + phone
python tests/browser/offline_e2e.py  http://127.0.0.1:8765          # offline queue / sync / isolation
python tests/browser/print_check.py  http://127.0.0.1:8765 print@demo.local   # A4 receipt layouts

python tools/check_inline_js.py      # syntax-check every script (needs node)
python tools/i18n_extract.py --report
```

Never point tests at the production database.
