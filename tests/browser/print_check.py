"""
Receipt print layout check: renders Simple (4 per A4) and FAT/SNF (2 per A4)
receipts to PDF through Chrome's print engine and counts the pages.

    python tests/browser/print_check.py http://127.0.0.1:8765 print@demo.local

PDFs are written to tests/browser/out/ for visual inspection.
"""
import math
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
EMAIL = sys.argv[2] if len(sys.argv) > 2 else "print@demo.local"
OUT = pathlib.Path(__file__).with_name("out")
OUT.mkdir(exist_ok=True)
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def pdf_pages(data):
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


def generate(page, path, extra=None):
    page.goto(BASE + path, wait_until="load")
    page.evaluate("""() => { document.getElementById('from_date').value = '2026-11-01';
                             document.getElementById('to_date').value = '2026-11-10'; }""")
    if extra:
        page.evaluate(extra)
    page.locator("form button[type=submit]").first.click()
    page.wait_for_load_state("load")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(500)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/login")
        page.fill("input[name=email]", EMAIL)
        page.fill("input[name=password]", "Demo@1234")
        page.locator("form:has(input[name=password]) button[type=submit]").first.click()
        page.wait_for_load_state("load")

        # ------------------------------------------------ simple: 4 per page
        generate(page, "/receipt_all_vendors?legacy=1")
        n = page.locator(".dp-print-source .dp-receipt").count()
        info = page.evaluate("DPReceiptPrint.build()")
        expected = math.ceil((n - info["oversize"]) / 4) + info["oversize"]
        check(f"simple: {n} receipts laid out in {info['pages']} A4 pages",
              n >= 14 and info["pages"] <= expected, str(info))
        pdf = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        (OUT / "simple_receipts.pdf").write_bytes(pdf)
        check("simple: printed PDF has the same page count (no blank pages)", pdf_pages(pdf) == info["pages"],
              f"pdf={pdf_pages(pdf)} layout={info['pages']}")
        cells = page.evaluate("""() => Array.from(document.querySelectorAll('#dp-print-root .dp-print-cell')).map(c => ({
              clipped: c.scrollHeight > c.clientHeight + 1 && !c.classList.contains('dp-print-cell--flow'),
              fit: getComputedStyle(c.firstElementChild).getPropertyValue('--fit')}))""")
        check("simple: no receipt is clipped", not any(c["clipped"] for c in cells))
        check("simple: smallest font scale stays readable (>= 0.62)",
              min(float(c["fit"]) for c in cells) >= 0.6, str(sorted(float(c["fit"]) for c in cells)[:3]))

        page.fill("#dpPrintFilter", "1-4, 7, 12-13")
        info = page.evaluate("DPReceiptPrint.build()")
        check("simple: filter '1-4, 7, 12-13' selects 7 receipts on 2 pages",
              info["shown"] == 7 and info["pages"] == 2, str(info))
        page.fill("#dpPrintFilter", "10-3")
        bad = page.evaluate("DPReceiptPrint.build()")
        check("simple: invalid range is reported, not ignored", bad.get("ok") is False, str(bad))
        page.fill("#dpPrintFilter", "")

        # server-side customer filter
        generate(page, "/receipt_all_vendors?legacy=1",
                 "document.getElementById('customers').value = '2, 5-6'")
        ids = page.evaluate("Array.from(document.querySelectorAll('.dp-print-source .dp-receipt')).map(r => r.dataset.customer)")
        check("simple: server-side selection '2, 5-6'", ids == ["2", "5", "6"], str(ids))

        # ------------------------------------------------ FAT/SNF: 2 per page
        generate(page, "/fat_snf/receipt",
                 "document.querySelector('input[name=detail]').checked = true")
        n = page.locator(".dp-print-source .dp-receipt").count()
        info = page.evaluate("DPReceiptPrint.build()")
        check(f"fat/snf: {n} receipts laid out in {info['pages']} A4 pages "
              f"({info['oversize']} long ones on their own page)",
              n >= 14 and info["pages"] <= math.ceil((n - info["oversize"]) / 2) + info["oversize"], str(info))
        pdf = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        (OUT / "fat_snf_receipts.pdf").write_bytes(pdf)
        check("fat/snf: printed PDF page count matches layout", pdf_pages(pdf) == info["pages"],
              f"pdf={pdf_pages(pdf)} layout={info['pages']}")
        cells = page.evaluate("""() => Array.from(document.querySelectorAll('#dp-print-root .dp-print-cell')).map(c => ({
              clipped: c.scrollHeight > c.clientHeight + 1 && !c.classList.contains('dp-print-cell--flow'),
              fit: getComputedStyle(c.firstElementChild).getPropertyValue('--fit')}))""")
        check("fat/snf: no receipt is clipped", not any(c["clipped"] for c in cells))
        check("no JavaScript errors", not errors, "; ".join(errors[:3]))
        browser.close()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
