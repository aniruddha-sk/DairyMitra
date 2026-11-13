"""Multilingual checks: catalog completeness, placeholders, every template key
translated, pages rendered in English contain no left-over Marathi text,
JSON API messages are localized."""
import html
import json
import re

import pytest

from conftest import ROOT, enable_fat_snf, login_owner, make_owner, make_vendor, q, reference_chart_payload

CATALOG = json.loads((ROOT / "i18n" / "catalog.json").read_text(encoding="utf-8"))
DEV = re.compile(r"[ऀ-ॿ]")
PLACEHOLDER = re.compile(r"\{(\w+)(?:[:!][^}]*)?\}")


def test_catalog_complete():
    missing = [e for e in CATALOG if not all(e.get(l) for l in ("mr", "en", "hi"))]
    assert not missing, missing[:5]


def test_placeholders_identical_in_all_languages():
    bad = []
    for e in CATALOG:
        sets = {l: set(PLACEHOLDER.findall(e[l])) for l in ("mr", "en", "hi")}
        if not (sets["mr"] == sets["en"] == sets["hi"]):
            bad.append(e)
    assert not bad, bad[:5]


def test_every_template_key_is_in_catalog():
    index = {" ".join(v.split()) for e in CATALOG for v in (e["mr"], e["en"], e["hi"])}
    pat = re.compile(r"""\b_\(\s*('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")
    import ast
    missing = []
    for f in (ROOT / "templates").rglob("*.html"):
        for m in pat.finditer(f.read_text(encoding="utf-8")):
            key = " ".join(ast.literal_eval(m.group(1)).split())
            if key not in index and not key.startswith("payment_status_"):
                missing.append((f.name, key))
    assert not missing, missing[:10]


@pytest.fixture()
def owner_en(client):
    uid = make_owner(dairy="Green Valley Dairy")
    make_vendor(uid, 1, "Ramesh", "cow", address="Pune")
    make_vendor(uid, 2, "Suresh", "buffalo", address="Pune")
    login_owner(client, uid)
    with client.session_transaction() as s:
        s["lang"] = "en"
        s["dairy_name"] = "Green Valley Dairy"
    return uid


ALLOWED_DEVANAGARI = {"मराठी", "हिन्दी"}   # native language names in the switcher

PAGES = ["/", "/milk_collection", "/food_sack", "/advance", "/advance/report", "/vendor_list",
         "/add_vendor", "/edit_entry", "/calculation", "/payment", "/receipt_all_vendors",
         "/milk_summary", "/vendor_range_summary", "/milk_rate", "/vendor_rate", "/food_sack_rate",
         "/settings", "/account", "/subscription", "/staff_list", "/add_staff", "/bank_settings",
         "/about", "/privacy", "/terms", "/contact", "/learn", "/backup_page",
         "/vehicle_milk_report", "/milk_prediction", "/vendor_performance", "/offline"]


def visible_text(page_html):
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", page_html, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    return html.unescape(body)


@pytest.mark.parametrize("path", PAGES)
def test_english_pages_have_no_marathi(client, owner_en, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    text = visible_text(r.get_data(as_text=True))
    words = {w for w in re.findall(r"[ऀ-ॿ][ऀ-ॿ‌‍]*", text)} - ALLOWED_DEVANAGARI
    leftovers = sorted({w for w in words for a in ALLOWED_DEVANAGARI if w not in a})
    assert not leftovers, (path, leftovers[:15])


def test_fat_snf_pages_in_english(client, owner_en):
    enable_fat_snf(owner_en)
    client.post("/api/fat_snf/charts", json=reference_chart_payload("cow"))
    for path in ("/fat_snf/milk_collection", "/fat_snf/rate-chart", "/fat_snf/rate-chart/create",
                 "/fat_snf/calculation", "/fat_snf/milk_summary", "/fat_snf/edit_entry",
                 "/fat_snf/receipt"):
        text = visible_text(client.get(path).get_data(as_text=True))
        words = {w for w in re.findall(r"[ऀ-ॿ]+", text)} - ALLOWED_DEVANAGARI
        assert not words, (path, sorted(words)[:15])


def test_receipt_in_hindi_and_english(client, owner_en):
    q("INSERT INTO milk_rates (user_id, animal, rate, date_from) VALUES (%s,'cow',35,'2026-01-01')",
      (owner_en,))
    q("INSERT INTO milk_collection (vendor_id, user_id, date, slot, milk_type, quantity) "
      "VALUES (1,%s,'2026-11-02','morning','cow',10)", (owner_en,))
    form = {"from_date": "2026-11-01", "to_date": "2026-11-10"}
    en = client.post("/receipt_all_vendors", data=form).get_data(as_text=True)
    assert "Net payable amount" in en and "Total cow milk" in en
    with client.session_transaction() as s:
        s["lang"] = "hi"
    hi = client.post("/receipt_all_vendors", data=form).get_data(as_text=True)
    assert "अंतिम देय राशि" in hi and "कुल गाय का दूध" in hi
    with client.session_transaction() as s:
        s["lang"] = "mr"
    mr = client.post("/receipt_all_vendors", data=form).get_data(as_text=True)
    assert "अंतिम देय रक्कम" in mr


def test_api_messages_follow_language(client, owner_en):
    r = client.post("/submit_milk_ajax", json={"vendor_id": 1, "milk_type": "cow", "quantity": "-1"})
    assert r.status_code == 400 and not DEV.search(r.get_json()["message"])
    with client.session_transaction() as s:
        s["lang"] = "hi"
    r = client.post("/submit_milk_ajax", json={"vendor_id": 999, "milk_type": "cow", "quantity": "5"})
    assert r.get_json()["message"] == "ग्राहक नहीं मिला।"


def test_flash_messages_translated(client):
    with client.session_transaction() as s:
        s["lang"] = "en"
    html_ = client.post("/login", data={"email": "nobody@test.local", "password": "x"}).get_data(as_text=True)
    assert "Invalid credentials!" in html_
    with client.session_transaction() as s:
        s["lang"] = "mr"
    html_ = client.post("/login", data={"email": "nobody@test.local", "password": "x"}).get_data(as_text=True)
    assert "चुकीचा ईमेल किंवा पासवर्ड!" in html_


@pytest.mark.parametrize("path", ["/login", "/forgot-password", "/customer/login", "/offline"])
def test_public_pages_in_english(client, path):
    with client.session_transaction() as s:
        s["lang"] = "en"
    text = visible_text(client.get(path).get_data(as_text=True))
    words = {w for w in re.findall(r"[ऀ-ॿ]+", text)} - ALLOWED_DEVANAGARI
    assert not words, (path, sorted(words)[:15])


def test_customer_portal_in_english(client):
    uid = make_owner(dairy="Green Valley Dairy")
    make_vendor(uid, 7, "Ramesh", "cow", address="Pune", phone="9999999999")
    with client.session_transaction() as s:
        s.update(loggedin=True, role="customer", id=uid, vendor_id=7, customer_id=1, vendor_db_id=1,
                 owner_id=uid, customer_name="Ramesh", dairy_name="Green Valley Dairy", lang="en")
    for path in ("/customer/dashboard", "/customer/milk-history", "/customer/receipts",
                 "/customer/notifications", "/customer/profile", "/customer/food-sack",
                 "/customer/advance"):
        r = client.get(path)
        assert r.status_code in (200, 302), (path, r.status_code)
        if r.status_code == 200:
            text = visible_text(r.get_data(as_text=True))
            words = {w for w in re.findall(r"[ऀ-ॿ]+", text)} - ALLOWED_DEVANAGARI
            assert not words, (path, sorted(words)[:15])
