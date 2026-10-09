# DairyMitra 2.0: FAT/SNF, security and reliability update

This document covers what changed, the business rules the code follows, how
to install, migrate, deploy and roll back, and the actual test results.

---

## 1. Summary

| Area | Result |
|---|---|
| FAT/SNF mode | Owner-only ON/OFF switch in **Settings**, stored in the DB, enforced on the server, audited |
| Rate charts | Owner-defined matrix: any FAT/SNF range and step, generated from a standard rate plus per-step changes, every cell editable, versions, activate/deactivate, history kept |
| Milk collection | Automatic IST date and slot, vendor → FAT → SNF → litres → Enter, live rate/amount preview, idempotent saves, duplicate refusal, soft warnings, Final Submit |
| Calculation / receipts | Vendor-wise calculation (CSV + print), A4 Marathi receipt matching the reference layout (single vendor or all vendors), FAT/SNF milk summary |
| Existing features | Litre-rate (OFF) workflow, Food Sack, Advance, reports and customer portal kept. Several crash bugs fixed |
| Security | CSRF, server-side role checks, OTPs moved out of cookies, brute-force limits, backup/restore locked down, security headers, secrets hygiene |
| Tests | **173 automated tests pass** against real MySQL 8.0.43, plus a browser run (Edge) and a load test |

---

## 2. Business rules implemented

**Settlement (unchanged rule, now applied everywhere)**

```
final payable  = milk amount − Food Sack − advance DEDUCTED in the period
remaining      = all advances − all deductions up to the period end  (carried forward, not deducted again)
```

The rule above was already documented three times in `app.py`. The
per-vendor receipt and the bank report did not follow it, and both crashed;
they now follow it.

**Calculation mode**
* OFF (`general`): existing litre × rate workflow, unchanged.
* ON (`fat_snf`): litre-rate collection endpoints refuse new entries (HTTP 409),
  so the same milk can never be saved in both systems. The old pages redirect
  to their FAT/SNF equivalents. Append `?legacy=1` to open an old page for
  historical records.
* Switching modes never rewrites history. A period that contains both kinds
  of entries is settled with **both**: FAT/SNF milk plus litre-rate milk
  (priced with the existing per-date rate logic). No delivered milk goes
  unpaid in either direction.

**Rate lookup**
1. Tenant comes from the session (never from the request).
2. Vendor milk type must allow the entered type (cow / buffalo / both).
3. Chart: active, `effective_from ≤ entry date`. A cow or buffalo chart beats a
   "both" chart, and the newest date wins.
4. **Exact** FAT/SNF match. No nearest value, no fallback to old rates, never ₹0.
   A missing combination returns a clear Marathi error and keeps the inputs.
5. The applied rate and amount are stored on each entry. Editing a chart later
   never changes saved entries.

**Rounding**: FAT, SNF, litres and rate have 2 decimals. Entry amount =
litres × rate, rounded once with HALF_UP to paise. Totals are sums of rounded
entries, so receipts always add up. All money uses `Decimal`, never float.

**Date and slot**: `Asia/Kolkata`. 15:00–23:59 is evening, otherwise morning.
The server validates both. Staff can only enter today's current slot. The
owner can enter a past date or the other slot through the explicit
"जुनी / दुरुस्ती नोंद" option, which is audited. Future dates are always rejected.

**Corrections**: changing FAT, SNF, litres, date or slot re-reads the rate
from the chart that applies on the entry's date. Finalised entries can only
be corrected by the owner, with a reason. Concurrent edits are detected
(HTTP 409 `stale`). Every create, update, delete and finalise is written to
`dm_audit_log` with old and new values.

**Warnings** (configurable in Settings): litres below or above the limits, or
more than X% away from the vendor's recent average. These are warnings only:
the user confirms and saves anyway.

---

## 3. What changed

**New modules**
* `fat_snf/logic.py`: pure rules (matrix generation, money, slot, warnings)
* `fat_snf/service.py`: database operations and settlement statements
* `fat_snf/routes.py`: pages and JSON APIs (blueprint `fat_snf`)
* `core/db.py`: real transactions (`transaction()`, `begin()`)
* `core/security.py`: CSRF, headers, staff allow-list, rate limiting, OTP challenges, error pages
* `core/audit.py`: database audit trail
* `migrations/`: versioned, re-runnable migration runner and migration `001`
* Templates: `fat_snf/*.html`, `errors/error.html`, `customer/profile.html`
* Static files: `css/fat_snf.css`, `js/csrf.js`, `js/fsn_*.js`
* `tests/` (173 tests), `tools/seed_demo.py`, `tools/load_test.py`,
  `gunicorn.conf.py`, `.env.example`, `requirements-dev.txt`

**Modified**: `app.py`, `backup_system.py`, `backup_to_drive.py`,
`requirements.txt`, `Procfile`, `.gitignore`, `static/service-worker.js`, and
the templates. In the templates, every POST form gained a CSRF token, plus a
few targeted UI changes.

**Removed**: the earlier partial FAT/SNF code at the end of `app.py`, its four
empty templates, and the committed `vapid_private_key.pem`.

---

## 4. Security fixes

| # | Problem found | Fix |
|---|---|---|
| 1 | Hard-coded fallback `SECRET_KEY` in a public repo: anyone could forge an owner session | App refuses to start in production without a 32+ character `SECRET_KEY` |
| 2 | Staff-login and signup OTPs stored in the session cookie, which is signed but readable. Staff could read the owner-approval OTP | OTPs are hashed in `dm_challenges`; the cookie holds only an opaque id; 5 attempts, single use, expiry |
| 3 | Password-reset OTP had no attempt limit (brute-forceable) | Same challenge store, 5-attempt lockout, IP/email rate limits, 10-minute reset window, password policy |
| 4 | Any logged-in user (any dairy, even staff) could restore the **whole multi-tenant database**, or dump it | Backup/restore limited to platform admins (`DAIRYMITR_ADMIN_EMAILS`), filename allow-list, password no longer on the `mysqldump` command line |
| 5 | Staff restrictions existed only in menus. Staff could delete the owner account, vendors, advances, rates | Server-side staff allow-list in `before_request` |
| 6 | No CSRF protection; staff disable/enable/delete were GET links | Flask-WTF CSRF on every POST (forms + `fetch` header); those actions are now POST |
| 7 | Every request printed cookies and the session (OTPs) to logs; subscribe printed all tenants' subscriptions | Removed |
| 8 | No brute-force protection on owner, staff or customer login | Failed attempts throttled per IP and per account (successful logins never count) |
| 9 | Account deletion was a single click | Password re-entry required, audited |
| 10 | Raw exception text returned to users (AI chat, restore) | Generic Marathi messages plus a request id. Details only in logs |
| 11 | No security headers | CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy, HSTS (when HTTPS) |
| 12 | `debug=True` when run directly | Only with `FLASK_DEBUG=1` |
| 13 | 13 known CVEs in pinned dependencies | Flask 3.1.3, Werkzeug 3.1.9, python-dotenv 1.2.2. Unused `mysql-connector-python` removed. `pip-audit`: **0 known vulnerabilities** |
| 14 | CSV exports could carry spreadsheet formulas from vendor names | Cells starting with `= + - @` are neutralised |

Tenant isolation: every query is scoped by the session's `user_id`, and
FAT/SNF tables now also have foreign keys to `users` and `vendors`.
Cross-tenant reads and writes are covered by tests. (MySQL has no
row-level security; isolation is enforced in code and by constraints.)

---

## 5. Bugs fixed in existing features

* **Transactions did not exist.** `MYSQL_AUTOCOMMIT=True` made every
  statement commit on its own, so the Advance `FOR UPDATE` locks did nothing
  (parallel deductions could overdraw) and "rollback everything" in the
  bulk edit undid nothing. These routes now open real transactions.
  Verified: with the old behaviour simulated, the overdraw and
  partial-edit tests fail; with the fix they pass.
* Per-vendor receipt (`/receipt/<id>`, also used by customers) **always
  crashed**: it queried the non-existent column `advance_transactions.date`.
  Its rate fallback also gave ₹0 when a vendor rate row had an empty cow or
  buffalo rate.
* Bank report **always failed**: same wrong column, and the main body was
  indented inside a `for` loop. It now uses the same settlement rule and
  per-date rates as the Payment page.
* Editing an advance could drive the ledger negative (only deletes were checked).
* Customers could not subscribe to push notifications (`/subscribe` redirected them).
* Offline milk sync deleted the local copy even when the server rejected it (data loss).
* Customer "My Profile" was a 500 error (missing template).
* Milk prediction used the wrong vendor id.
* The collection default date used server time (UTC) instead of IST between 00:00 and 05:30.
* Legacy collection page JS errors (banner outside a block, IndexedDB upgrade race).
* `google-genai` was imported but missing from `requirements.txt` (a fresh deploy would crash).
* AI tools queried a non-existent `payments` table, the legacy `advance`
  table and wrong food-sack columns. Rewritten on the real tables; money
  answers are for the owner only.
* Broken `logo.png` references and a missing `/favicon.ico` route.

**Known inconsistencies left as they were** (business decisions; not changed silently):
* The OFF-mode "all vendors" receipt prints one rate per vendor taken at the
  period start, while Payment and Calculation price each day at that day's
  rate. If a rate changes inside the period, the totals differ.
* The bank report includes vendors whose payable amount is ₹0 or negative.

---

## 6. Local setup

```bash
python -m venv .venv && .venv\Scripts\activate        # Windows (source .venv/bin/activate on Linux)
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env      # then edit: SECRET_KEY, MYSQL_*, DAIRYMITR_ENV=development, SESSION_COOKIE_SECURE=0
python -m migrations        # apply schema changes (safe to re-run)
python app.py               # http://localhost:8000
```

Demo data (synthetic only; the script refuses unless the database name ends with `_demo`):

```bash
set MYSQL_DB=dairymitr_demo && python tools/seed_demo.py
# login: owner@demo.local / Demo@1234
```

---

## 7. Database migration, backup and rollback

Migration `001` is **additive**. It adds columns, tables, indexes and
foreign keys. It never drops or renames anything and never changes existing
rows. Column type changes are limited to the FAT/SNF tables (UNSIGNED INT →
INT, so they can reference `users` and `vendors`). Foreign keys are skipped
with a warning, never forced, if orphan rows exist.

**Before migrating production**

```bash
# 1. Backup (keep it off the server too)
mysqldump --single-transaction --routines --triggers -h HOST -u USER -p DBNAME > pre_dairymitra2.sql
# 2. Rehearse on a copy
mysql -h HOST -u USER -p -e "CREATE DATABASE dairymitr_copy"
mysql -h HOST -u USER -p dairymitr_copy < pre_dairymitra2.sql
MYSQL_DB=dairymitr_copy python -m migrations --dry-run
MYSQL_DB=dairymitr_copy python -m migrations
# 3. Compare row counts of users, vendors, milk_collection, advance_transactions, food_sack (must be identical)
```

**Apply**: `python -m migrations` (status: `python -m migrations --status`).
Any `WARNING ... orphan row(s)` line names data you may want to review. Nothing was deleted.

**Rollback**
* *Code only*: redeploy the previous version. The migrated schema is
  backward compatible with the old code (new columns have defaults), so no
  database change is needed.
* *Full*: restore `pre_dairymitra2.sql`. Data entered after the migration is
  lost, so export it first if needed.

---

## 8. Production deployment checklist

- [ ] `SECRET_KEY` set (64 hex characters). The app will not start without it.
- [ ] `DAIRYMITR_ENV=production` (default), `SESSION_COOKIE_SECURE=1`, HTTPS on
- [ ] `TRUST_PROXY_HOPS=1` behind Render/Railway/Nginx (correct client IPs for rate limits)
- [ ] Database user has least privilege (SELECT/INSERT/UPDATE/DELETE on the app DB; DDL only for the migration step)
- [ ] Backup taken → `python -m migrations` run → `GET /healthcheck?deep=1` returns `{"database":"ok","migrations":"up_to_date"}`
- [ ] Start command: `gunicorn -c gunicorn.conf.py app:app` (already in `Procfile`)
- [ ] **Rotate the VAPID key pair**: the old private key was public in the repository.
      Run `python generate_vapid_keys.py`, set `VAPID_PUBLIC_KEY` and deploy the
      new `.pem` outside git. Then run `DELETE FROM push_subscriptions;`.
      Customer browsers detect the key change on their next visit
      (`subscribe.js` now compares keys), unsubscribe and subscribe again.
      (Customer push could not work before anyway, because `/subscribe`
      redirected customers; that is now fixed.)
- [ ] Rotate any other credential that ever appeared in the repository or in logs
- [ ] `DAIRYMITR_ADMIN_EMAILS` set if you need web backup/restore. Otherwise leave it empty and back up with `mysqldump`.
- [ ] Optional: `CSP_REPORT_ONLY=1` for the first day if you use pages not exercised in testing, then switch it off
- [ ] Smoke test: owner login → Settings toggle → create a rate chart → one collection entry → receipt print preview

---

## 9. Test results (actually executed, 9 Oct 2026)

Environment: Windows 11, Python 3.11.9 (production version), MySQL 8.0.43
(throwaway instance), Microsoft Edge and Google Chrome (headless, via Playwright).

| Suite | Tests | Result |
|---|---|---|
| `test_logic.py`: matrix (all 95 cells of the reference image), parsing, rounding, slot, warnings | 55 | pass |
| `test_fat_snf_integration.py`: mode, charts, collection, finalise, corrections, settlement, receipts, bank report | 47 | pass |
| `test_security.py`: CSRF, staff and customer authz, tenant isolation, OTP, brute force, XSS, SQLi, CSV injection, headers, secret key | 51 | pass |
| `test_regression.py`: every GET page for owner (both modes), staff and customer; OFF-mode numbers unchanged | 11 | pass |
| `test_concurrency.py`: parallel same-key saves, parallel duplicate slot, parallel advance deductions | 3 | pass |
| `test_failures.py`: database down → 503, deep health check, rollback of bulk edit / chart / entry | 5 | pass |
| `test_migrations.py`: data preserved, orphans kept, re-runnable | 1 | pass |
| **Total** | **173** | **173 passed** |

Run: `TEST_MYSQL_HOST=127.0.0.1 TEST_MYSQL_PORT=3399 TEST_MYSQL_USER=root pytest tests`
(the suite drops and recreates `dairymitr_pytest` on that server, so never point it at production).

**Browser run (Edge, re-checked in Chrome; real login with CSRF on)**: chart generation (95 cells),
cell edit and save; keyboard entry with live preview ₹27.00 × 18 L =
₹486.00; duplicate refused; correction recalculated ₹486.00 → ₹491.40;
receipts printed to A4 PDF (cow-only: 1 page; both types over 30 days: 2
pages, no split rows; all vendors: 1 page each); 390 px mobile width with no
horizontal scroll. After fixes: 0 JavaScript errors and 0 broken resources
across 23 pages. This run found and fixed 4 bugs the unit tests could not
see (settings toggle script, hidden-attribute CSS, legacy page errors,
missing logo).

**Load test** (`tools/load_test.py`, 20 concurrent users, 960 requests, Flask
development server on one Windows machine): 145 requests/s, 0 failures,
p95 ≤ 206 ms per endpoint. Each of the 160 saves was retried with the same
key, leaving exactly 160 rows. *These numbers are indicative only:
production uses gunicorn on different hardware. Re-run the script against
staging after deploying.*

**Dependency audit**: `pip-audit -r requirements.txt` found 0 known vulnerabilities (after the upgrades).

---

## 10. Known limitations and items not done

* **Not executed**: tests on the real production database or hosting
  platform; gunicorn (it does not run on Windows); a real email, SMS or push
  provider (mocked in tests); iOS Safari and Firefox (only Chromium-based Edge and Chrome were
  used).
* CSP still allows inline scripts (`'unsafe-inline'`), because the existing
  templates use many inline scripts. Moving them to files is a follow-up.
* Cookie sessions cannot be revoked server-side, so a password reset does not
  log out other devices. Possible improvement: a session version in `users`.
* Customer login is dairy code + phone number (now rate-limited). An SMS OTP
  would be stronger.
* Offline (IndexedDB) queueing exists only on the legacy litre-rate page. The
  FAT/SNF page needs connectivity, but its retries are safe (idempotent) and
  it tells the user when a save outcome is unknown.
* Chart lookup is exact-match only, by design. A nearest-value policy would
  need an explicit, audited setting.
