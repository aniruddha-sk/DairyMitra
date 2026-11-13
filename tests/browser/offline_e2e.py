"""
Offline end-to-end test in a real browser (Playwright + installed Chrome).

Needs a running DairyPro server with the demo database (tools/seed_demo.py)
plus a second owner (owner2@demo.local / Demo@1234, vendor 101, FAT/SNF on).
Reads the database directly to verify what the server stored.

    python tests/browser/offline_e2e.py http://127.0.0.1:8765

Scenarios
  1. Save offline -> "saved on this device" only after IndexedDB commit;
     reload while offline (page from the service-worker cache); reconnect ->
     synchronised exactly once; resending the same record does not duplicate.
  2. Session expired while entries wait -> nothing is lost, nothing is sent;
     after logging in again the entry synchronises.
  3. Another dairy logs in on the same device -> the first dairy's entries are
     NOT sent under the second dairy; they synchronise when the first dairy
     logs in again.
  4. The same entry cannot be queued twice on the device.
"""
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import MySQLdb
import MySQLdb.cursors
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
DB = dict(host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", 3399)),
          user=os.getenv("MYSQL_USER", "root"), passwd=os.getenv("MYSQL_PASSWORD", ""),
          db=os.getenv("MYSQL_DB", "dairypro_demo"), charset="utf8mb4", autocommit=True,
          cursorclass=MySQLdb.cursors.DictCursor)
TODAY = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
results = []


def sql(query, params=()):
    conn = MySQLdb.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()
    finally:
        conn.close()


def owner_id(email):
    return sql("SELECT id FROM users WHERE email=%s", (email,))[0]["id"]


def server_entries(owner, vendor):
    return sql("SELECT id, fat, snf, quantity FROM fat_snf_milk_collection WHERE user_id=%s "
               "AND vendor_id=%s AND collection_date=%s", (owner, vendor, TODAY))


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def login(page, email):
    page.goto(BASE + "/login")
    page.fill("input[name=email]", email)
    page.fill("input[name=password]", "Demo@1234")
    page.locator("form:has(input[name=password]) button[type=submit]").first.click()
    page.wait_for_load_state("load")


def open_collection(page):
    page.goto(BASE + "/fat_snf/milk_collection", wait_until="load")
    page.evaluate("navigator.serviceWorker.ready.then(() => true)")
    page.wait_for_timeout(600)


def save_row(page, vendor, fat, snf, litres, milk_type=None):
    row = page.locator(f'tr[data-vendor-id="{vendor}"]')
    if milk_type:
        sel = row.locator("select.milk-type")
        if sel.count():
            sel.select_option(milk_type)
    row.locator("input.fat").fill(fat)
    row.locator("input.snf").fill(snf)
    row.locator("input.quantity-input").fill(litres)
    row.locator("button.saveRow").click()
    page.wait_for_timeout(700)


def local_records(page, tenant=None):
    return page.evaluate("t => DPOffline.forTenant(t || DP_SESSION.tenant_id)", tenant)


def settle_and_confirm(page, vendor, owner, check_name):
    """Wait until the queued entry for `vendor` leaves 'pending'. A quantity
    warning parks it as needs_review (never saved silently); confirm it the
    way the user would in the review panel, then expect it on the server."""
    def rec():
        for r in local_records(page):
            if str(r["payload"].get("vendor_id")) == str(vendor) and r["status"] != "synced":
                return r
        return None
    wait_for(page, lambda: (rec() is None) or rec()["status"] != "pending")
    r = rec()
    if r and r["status"] == "needs_review":
        check(check_name + " (warning parked for review, not saved)",
              r.get("review_kind") == "confirm" and len(server_entries(owner, vendor)) == 0, str(r.get("last_error")))
        page.evaluate("id => DPOffline.retry(id, {confirm_warnings: true, force_save: true})", r["local_id"])
        page.evaluate("DPOffline.run({tenant: DP_SESSION.tenant_id, manual: true})")
    return wait_for(page, lambda: len(server_entries(owner, vendor)) == 1)


def wait_for(page, fn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        page.wait_for_timeout(500)
    return False


def main():
    o1 = owner_id("owner@demo.local")
    o2 = owner_id("owner2@demo.local")
    for v in (101, 102, 104):
        sql("DELETE FROM fat_snf_milk_collection WHERE user_id=%s AND vendor_id=%s AND collection_date=%s",
            (o1, v, TODAY))
    sql("DELETE FROM fat_snf_milk_collection WHERE user_id=%s AND collection_date=%s", (o2, TODAY))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ---------------------------------------------------------- 1
        login(page, "owner@demo.local")
        open_collection(page)
        page.reload(wait_until="load")          # now controlled by the service worker
        page.wait_for_timeout(800)
        ctx.set_offline(True)
        save_row(page, 104, "3.60", "8.50", "7.50")
        recs = local_records(page)
        check("1a offline save stored on device (pending)",
              len(recs) == 1 and recs[0]["status"] == "pending" and recs[0]["tenant_id"] == str(o1),
              str([(r["status"], r["tenant_id"]) for r in recs]))
        toast = page.locator(".dp-toast").last.inner_text() if page.locator(".dp-toast").count() else ""
        check("1b message says saved on this device (not 'saved')", "device" in toast.lower() or "डिव्हाइस" in toast,
              toast)
        check("1c nothing reached the server while offline", len(server_entries(o1, 104)) == 0)
        page.reload(wait_until="load")          # offline reload
        page.wait_for_timeout(800)
        check("1d page opens offline from cache",
              page.locator('meta[name="dp-offline-copy"]').count() == 1 or page.url.endswith("/fat_snf/milk_collection"),
              page.url)
        check("1e queued entry still on device after offline reload", len(local_records(page)) == 1)
        ctx.set_offline(False)
        page.evaluate("window.dispatchEvent(new Event('online'))")
        synced = wait_for(page, lambda: all(r["status"] == "synced" for r in local_records(page)))
        check("1f entry synchronised after reconnect", synced, str([r["status"] for r in local_records(page)]))
        rows = server_entries(o1, 104)
        check("1g server has exactly one entry", len(rows) == 1, str(rows))
        local_id = local_records(page)[0]["local_id"]
        page.evaluate("id => DPOffline.retry(id)", local_id)            # resend same key
        page.evaluate("DPOffline.run({tenant: DP_SESSION.tenant_id, manual: true})")
        page.wait_for_timeout(2500)
        check("1h resending the same record does not duplicate", len(server_entries(o1, 104)) == 1)

        # ---------------------------------------------------------- 2
        ctx.set_offline(True)
        save_row(page, 101, "3.80", "8.60", "5.00")
        before = [r for r in local_records(page) if r["status"] == "pending"]
        check("2a second entry queued", len(before) == 1)
        ctx.clear_cookies()                      # session expires while offline
        ctx.set_offline(False)
        page.evaluate("DPOffline.run({tenant: DP_SESSION.tenant_id})")
        page.wait_for_timeout(2500)
        pend = [r for r in local_records(page) if r["status"] == "pending"]
        check("2b expired session: entry kept, nothing sent",
              len(pend) == 1 and len(server_entries(o1, 101)) == 0,
              str([(r["status"], r.get("last_error")) for r in local_records(page)]))
        login(page, "owner@demo.local")
        page.wait_for_timeout(500)
        ok = settle_and_confirm(page, 101, o1, "2c")
        check("2c after logging in again the entry synchronises", ok)

        # ---------------------------------------------------------- 3
        open_collection(page)
        ctx.set_offline(True)
        save_row(page, 102, "6.00", "8.50", "4.00")
        check("3a owner-1 entry queued", any(r["status"] == "pending" for r in local_records(page)))
        ctx.clear_cookies()
        ctx.set_offline(False)
        login(page, "owner2@demo.local")          # another dairy on the same device
        page.wait_for_timeout(500)
        page.evaluate("DPOffline.run({tenant: DP_SESSION.tenant_id, manual: true})")
        page.wait_for_timeout(3000)
        mine = local_records(page, str(o1))
        check("3b owner-1 entry NOT sent under owner-2",
              len(server_entries(o1, 102)) == 0 and len(sql(
                  "SELECT id FROM fat_snf_milk_collection WHERE user_id=%s AND collection_date=%s",
                  (o2, TODAY))) == 0 and any(r["status"] == "pending" for r in mine),
              str([(r["status"], r["tenant_id"]) for r in mine]))
        other = page.evaluate("DPOffline.summary(DP_SESSION.tenant_id)")
        check("3c owner-2 sees that another account has waiting entries", other["other_tenants"] >= 1, str(other))
        # A forged request with owner-1's tenant header is refused by the server
        res = page.evaluate("""async (t) => {
            const r = await fetch('/api/fat_snf/entries', {method: 'POST', credentials: 'same-origin',
              headers: {'Content-Type': 'application/json', 'X-CSRFToken': DP.csrfToken(), 'X-DP-Tenant': String(t),
                        'Accept': 'application/json'},
              body: JSON.stringify({vendor_id: 101, milk_type: 'cow', fat: '3.5', snf: '8.5', litres: '1'})});
            return [r.status, (await r.json()).code];
        }""", o1)
        check("3d server refuses another dairy's tenant header", res == [409, "tenant_mismatch"], str(res))
        page.goto(BASE + "/logout")
        login(page, "owner@demo.local")
        ok = settle_and_confirm(page, 102, o1, "3e")
        check("3e owner-1 entry synchronises after owner-1 logs in", ok)

        # ---------------------------------------------------------- 4
        open_collection(page)
        ctx.set_offline(True)
        sql("DELETE FROM fat_snf_milk_collection WHERE user_id=%s AND vendor_id=104 AND collection_date=%s",
            (o1, TODAY))
        n0 = len([r for r in local_records(page) if r["status"] != "synced"])
        spec = {"type": "fsn_milk", "endpoint": "/api/fat_snf/entries", "idempotency_field": "request_id",
                "payload": {"vendor_id": 104, "milk_type": "cow", "fat": "3.5", "snf": "8.5", "litres": "2",
                            "date": TODAY, "slot": "morning"},
                "dedupe_key": f"fsn|104|{TODAY}|morning|cow", "tenant_id": o1, "label": "dup test"}
        first = page.evaluate("s => DPOffline.enqueue(s).then(() => 'ok', e => e.code)", spec)
        second = page.evaluate("s => DPOffline.enqueue(s).then(() => 'ok', e => e.code)", spec)
        n1 = len([r for r in local_records(page) if r["status"] != "synced"])
        check("4 the same entry cannot be queued twice", first == "ok" and second == "duplicate_local" and n1 == n0 + 1,
              f"{first} {second} {n0}->{n1}")
        # clean up the extra queued record
        for r in local_records(page):
            if r["label"] == "dup test":
                page.evaluate("id => DPOffline.remove(id)", r["local_id"])
        ctx.set_offline(False)
        check("no JavaScript errors", not errors, "; ".join(errors[:3]))
        browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
