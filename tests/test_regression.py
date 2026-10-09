"""
Regression: every page renders for every role in both calculation modes,
and the existing litre-rate (OFF mode) calculations are unchanged.
"""
from datetime import date

import pytest

from conftest import (enable_fat_snf, login_owner, login_staff, make_owner, make_staff,
                      make_vendor, q)

SKIP = {"static", "logout", "customer_logout", "service_worker", "manifest",
        "dairy_mitr_ai_chat", "admin_full_backup", "backup_my_data", "subscribe"}


def get_routes(app_module):
    out = []
    for rule in app_module.app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint in SKIP:
            continue
        args = {a: 1 for a in rule.arguments}
        if "filename" in args:
            continue
        if rule.endpoint.startswith("fat_snf.api") or rule.rule.startswith("/api/"):
            continue
        out.append((rule.endpoint, rule.rule, args))
    return out


@pytest.fixture()
def seeded(client):
    owner = make_owner()
    make_vendor(owner, 1, "गाय वाला", "cow", phone="9876543210")
    make_vendor(owner, 2, "दोन्ही", "both")
    q("INSERT INTO milk_rates (user_id, animal, rate, date_from) VALUES (%s,'cow',30,'2026-01-01'),"
      "(%s,'buffalo',50,'2026-01-01')", (owner, owner))
    q("INSERT INTO milk_collection (vendor_id, user_id, date, slot, milk_type, quantity) VALUES "
      "(1,%s,'2026-09-01','morning','cow',10),(1,%s,'2026-09-01','evening','cow',8),"
      "(2,%s,'2026-09-02','morning','buffalo',5)", (owner, owner, owner))
    rid = q("INSERT INTO food_sack_rates (user_id, name, rate, date_from) VALUES (%s,'पेंड',1000,'2026-01-01')",
            (owner,))
    q("INSERT INTO food_sack (vendor_id, user_id, date, sack_qty, sack_rate_id, total_cost, sack_rate) "
      "VALUES (1,%s,'2026-09-03',1,%s,1000,1000)", (owner, rid))
    q("INSERT INTO advance_transactions (vendor_id, user_id, transaction_type, amount, transaction_date) "
      "VALUES (1,%s,'advance',300,'2026-08-01'),(1,%s,'deduction',100,'2026-09-05')", (owner, owner))
    return owner


@pytest.mark.parametrize("mode", ["general", "fat_snf"])
def test_every_page_renders_for_owner(app, client, seeded, mode):
    if mode == "fat_snf":
        enable_fat_snf(seeded)
    login_owner(client, seeded)
    failures = []
    for endpoint, rule, args in get_routes(app):
        url = rule
        for a in args:
            url = url.replace(f"<int:{a}>", "1").replace(f"<string:{a}>", "1").replace(f"<{a}>", "1")
        r = client.get(url)
        if r.status_code >= 500 or r.status_code == 404 and not args:
            failures.append((url, r.status_code))
    assert not failures, failures


def test_staff_pages(app, client, seeded):
    staff = make_staff(seeded)
    login_staff(client, seeded, staff)
    for mode in ("general", "fat_snf"):
        if mode == "fat_snf":
            enable_fat_snf(seeded)
        for url in ("/", "/milk_collection", "/food_sack", "/edit_entry", "/milk_summary",
                    "/vendor_range_summary"):
            r = client.get(url, follow_redirects=True)
            assert r.status_code == 200, (mode, url)


def test_customer_pages(client, seeded):
    with client.session_transaction() as s:
        s.update(loggedin=True, role="customer", id=seeded, owner_id=seeded, vendor_id=1,
                 customer_id=1, customer_name="x", dairy_name="d")
    for url in ("/customer/dashboard", "/customer/milk-history", "/customer/profile",
                "/customer/notifications"):
        assert client.get(url).status_code == 200, url
    html = client.get("/customer/milk-history?from_date=2026-09-01&to_date=2026-09-30").get_data(as_text=True)
    assert "18.0" in html                     # 10 + 8 litres on 01-09


def test_customer_login(client, seeded):
    code = q("SELECT dairy_code FROM users WHERE id=%s", (seeded,), one=True)["dairy_code"]
    r = client.post("/customer/login", data={"dairy_id": code.lower(), "phone": "9876543210"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/customer/dashboard")


class TestOffModeCalculationsUnchanged:
    """Numbers computed by the untouched litre-rate logic (OFF mode)."""

    def test_payment_page(self, client, seeded):
        login_owner(client, seeded)
        html = client.get("/payment?start_date=2026-09-01&end_date=2026-09-30").get_data(as_text=True)
        # vendor 1: 18 L x 30 = 540 - food 1000 - deduction 100 = -560
        assert "₹-560" in html
        # vendor 2: 5 L buffalo x 50 = 250
        assert "₹250" in html

    def test_calculation_page(self, client, seeded):
        login_owner(client, seeded)
        html = client.post("/calculation", data={"vendor_id": 1, "start_date": "2026-09-01",
                                                 "end_date": "2026-09-30"}).get_data(as_text=True)
        assert "₹540" in html and "₹-560" in html and "₹200" in html   # remaining advance 200

    def test_receipt_all_vendors(self, client, seeded):
        login_owner(client, seeded)
        r = client.post("/receipt_all_vendors", data={"from_date": "2026-09-01",
                                                      "to_date": "2026-09-30"})
        html = r.get_data(as_text=True)
        assert r.status_code == 200 and "₹540" in html and "₹-560" in html

    def test_per_vendor_receipt(self, client, seeded):
        login_owner(client, seeded)
        html = client.post("/receipt/1", data={"from_date": "2026-09-01",
                                               "to_date": "2026-09-30"}).get_data(as_text=True)
        assert "540.0" in html and "-560.0" in html

    def test_advance_api(self, client, seeded):
        login_owner(client, seeded)
        body = client.get("/advance/vendor/1").get_json()
        assert body["summary"]["remaining_advance"] == 200.0
        r = client.post("/advance/deduct", json={"vendor_id": 1, "amount": "500", "date": "2026-10-01"})
        assert r.status_code == 400                          # cannot exceed balance
        r = client.post("/advance/edit/" + str(q(
            "SELECT id FROM advance_transactions WHERE transaction_type='advance'", one=True)["id"]),
            json={"amount": "50", "date": "2026-08-01"})
        assert r.status_code == 400                          # would make ledger negative (new rule)

    def test_vendor_crud(self, client, seeded):
        login_owner(client, seeded)
        r = client.post("/add_vendor", data={"name": "नवा", "name_en": "Nava", "vendor_id": "9",
                                             "address": "गाव", "milk_type": "cow", "phone": ""})
        assert r.status_code == 302
        assert q("SELECT name FROM vendors WHERE vendor_id=9", one=True)["name"] == "नवा"
        r = client.post("/add_vendor", data={"name": "dup", "vendor_id": "9", "address": "x",
                                             "milk_type": "cow"})
        assert "already exists" in r.get_data(as_text=True)
