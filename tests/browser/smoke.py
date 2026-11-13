"""
Browser smoke test (Playwright + the installed Chrome/Edge).

    python tests/browser/smoke.py http://127.0.0.1:8765 owner@demo.local Demo@1234

Opens every main page as the owner at desktop and phone width and reports:
JavaScript errors, failed requests, horizontal overflow on phones, icons that
did not render, and native alert()/confirm()/prompt() calls.
Needs: pip install playwright (uses the installed Chrome via channel="chrome").
"""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE, EMAIL, PASSWORD = (sys.argv[1:4] + [None] * 3)[:3]
BASE = BASE or "http://127.0.0.1:8765"
EMAIL = EMAIL or "owner@demo.local"
PASSWORD = PASSWORD or "Demo@1234"
OUT = pathlib.Path(__file__).with_name("out")
OUT.mkdir(exist_ok=True)

PAGES = ["/", "/milk_collection?legacy=1", "/fat_snf/milk_collection", "/food_sack", "/advance",
         "/advance/report", "/vendor_list", "/add_vendor", "/edit_entry?legacy=1",
         "/fat_snf/edit_entry", "/calculation?legacy=1", "/fat_snf/calculation", "/payment?legacy=1",
         "/receipt_all_vendors?legacy=1", "/fat_snf/receipt", "/milk_summary?legacy=1",
         "/fat_snf/milk_summary", "/vendor_range_summary?legacy=1", "/milk_rate?legacy=1",
         "/vendor_rate", "/food_sack_rate", "/fat_snf/rate-chart", "/fat_snf/rate-chart/create",
         "/settings", "/account", "/subscription", "/staff_list", "/add_staff", "/bank_settings",
         "/generate_bank_report", "/about", "/learn", "/contact", "/privacy", "/terms",
         "/milk_prediction", "/vendor_performance", "/backup_page", "/vehicle_milk_report",
         "/reports/milk-summary", "/offline"]

NATIVE_DIALOG_PROBE = """
window.__nativeDialogs = [];
['alert','confirm','prompt'].forEach(function (n) {
  var orig = window[n];
  window[n] = function () { window.__nativeDialogs.push(n); return n === 'confirm' ? false : null; };
});
"""


def run():
    report = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        for label, viewport in (("desktop", {"width": 1280, "height": 900}),
                                ("phone", {"width": 390, "height": 844})):
            ctx = browser.new_context(viewport=viewport, locale="en-US")
            ctx.add_init_script(NATIVE_DIALOG_PROBE)
            page = ctx.new_page()
            errors, failed = [], []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("requestfailed", lambda r: failed.append(r.url))
            page.goto(BASE + "/login")
            page.fill("input[name=email]", EMAIL)
            page.fill("input[name=password]", PASSWORD)
            page.locator("form:has(input[name=password]) button[type=submit]").first.click()
            page.wait_for_load_state("networkidle")
            for path in PAGES:
                errors.clear(); failed.clear()
                resp = page.goto(BASE + path, wait_until="load")
                page.wait_for_timeout(900)
                info = page.evaluate("""() => ({
                    overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
                    unrendered: Array.from(document.querySelectorAll('[data-lucide], i[class*="fa-"], .iconify[data-icon]'))
                        .filter(e => !e.querySelector('svg')).map(e => e.outerHTML.slice(0, 80)),
                    dialogs: window.__nativeDialogs || [],
                    title: document.title,
                    url: location.pathname
                })""")
                key = f"{label} {path}"
                report[key] = {"status": resp.status if resp else None, "errors": list(errors),
                               "failed": list(failed), **info}
                if label == "phone" and path in ("/", "/fat_snf/milk_collection", "/milk_collection?legacy=1",
                                                  "/subscription", "/vendor_list", "/settings"):
                    page.screenshot(path=str(OUT / (path.strip("/").replace("/", "_").replace("?", "_") or "home")) + "_phone.png",
                                    full_page=False)
            ctx.close()
        browser.close()
    problems = {k: v for k, v in report.items()
                if v["errors"] or v["failed"] or v["unrendered"] or v["dialogs"]
                or (v["status"] or 0) >= 400 or (k.startswith("phone") and v["overflow"])}
    (OUT / "smoke.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"pages checked: {len(report)}, with problems: {len(problems)}")
    for k, v in problems.items():
        print("-", k, {x: v[x] for x in ("status", "errors", "failed", "unrendered", "dialogs", "overflow", "url") if v.get(x)})
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(run())
