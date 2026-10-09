"""Security tests: CSRF, authz, tenant isolation, OTP, rate limits, XSS, SQLi, headers."""
import base64
import json
import os
import re
import subprocess
import sys
import zlib

import pytest

from conftest import (PASSWORD, ROOT, enable_fat_snf, login_owner, login_staff, make_owner,
                      make_staff, make_vendor, now_date_slot, q, reference_chart_payload)


def decode_session_cookie(client):
    cookie = client.get_cookie("session")
    assert cookie is not None
    payload = cookie.value.split(".")[0]
    compressed = payload.startswith(".") or cookie.value.startswith(".")
    data = cookie.value.lstrip(".").split(".")[0]
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    if cookie.value.startswith("."):
        raw = zlib.decompress(raw)
    return raw.decode("utf-8", "replace")


# ----------------------------------------------------------------------
# CSRF
# ----------------------------------------------------------------------
class TestCSRF:
    def test_post_without_token_rejected(self, app, client):
        owner = make_owner()
        login_owner(client, owner)
        app.app.config["WTF_CSRF_ENABLED"] = True
        r = client.post("/add_vendor", data={"name": "x", "vendor_id": "1", "address": "a",
                                             "milk_type": "cow"})
        assert r.status_code == 302                          # redirected with flash
        assert q("SELECT COUNT(*) n FROM vendors", one=True)["n"] == 0
        r = client.post("/api/fat_snf/calculation_mode", json={"calculation_mode": "fat_snf"})
        assert r.status_code == 400 and r.get_json()["code"] == "csrf"

    def test_post_with_token_accepted(self, app, client):
        owner = make_owner()
        login_owner(client, owner)
        app.app.config["WTF_CSRF_ENABLED"] = True
        html = client.get("/settings").get_data(as_text=True)
        token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        r = client.post("/api/fat_snf/calculation_mode", json={"calculation_mode": "fat_snf"},
                        headers={"X-CSRFToken": token})
        assert r.status_code == 200

    def test_login_form_contains_token(self, client):
        html = client.get("/login").get_data(as_text=True)
        assert html.count('name="csrf_token"') == 2   # login + signup panels


# ----------------------------------------------------------------------
# Role-based access
# ----------------------------------------------------------------------
class TestStaffRestrictions:
    @pytest.fixture()
    def staff_client(self, client):
        owner = make_owner()
        make_vendor(owner, 1, "A")
        staff = make_staff(owner)
        login_staff(client, owner, staff)
        return client, owner

    @pytest.mark.parametrize("method,url", [
        ("post", "/delete_account"), ("post", "/delete_vendor/1"), ("get", "/vendor_rate"),
        ("post", "/advance/add"), ("get", "/payment"), ("get", "/bank_settings"),
        ("post", "/reset_staff_password/1"), ("get", "/staff_list"), ("get", "/settings"),
        ("post", "/backup_my_data"), ("get", "/fat_snf/rate-chart"), ("get", "/fat_snf/calculation"),
        ("post", "/api/fat_snf/charts"), ("get", "/receipt/1"), ("get", "/fat_snf/receipt"),
    ])
    def test_owner_only_endpoints_blocked(self, staff_client, method, url):
        client, owner = staff_client
        r = getattr(client, method)(url)
        assert r.status_code in (302, 403), (url, r.status_code)
        if r.status_code == 302:
            assert r.headers["Location"].endswith("/")
        assert q("SELECT COUNT(*) n FROM users", one=True)["n"] == 1
        assert q("SELECT COUNT(*) n FROM vendors", one=True)["n"] == 1

    @pytest.mark.parametrize("url", ["/", "/milk_collection", "/food_sack", "/edit_entry",
                                     "/milk_summary", "/vendor_range_summary",
                                     "/fat_snf/milk_collection", "/fat_snf/edit_entry",
                                     "/fat_snf/milk_summary"])
    def test_daily_work_allowed(self, staff_client, url):
        client, _ = staff_client
        assert client.get(url).status_code == 200, url

    def test_disabled_staff_logged_out(self, staff_client):
        client, owner = staff_client
        q("UPDATE staff SET is_active=0")
        r = client.get("/")
        assert r.status_code == 302 and "/login" in r.headers["Location"]


class TestMutatingGetRemoved:
    def test_staff_actions_require_post(self, client):
        owner = make_owner()
        sid = make_staff(owner)
        login_owner(client, owner)
        for action in ("disable_staff", "enable_staff", "delete_staff"):
            assert client.get(f"/{action}/{sid}").status_code == 405
        assert client.post(f"/disable_staff/{sid}").status_code == 302
        assert q("SELECT is_active FROM staff", one=True)["is_active"] == 0


class TestBackupsLockedDown:
    def test_owner_cannot_restore_or_dump(self, client):
        owner = make_owner()
        login_owner(client, owner)
        assert client.post("/restore_backup/x.sql.gz").status_code == 403
        assert client.post("/backup_my_data").status_code == 403
        assert client.post("/admin_full_backup").status_code == 403
        assert client.get("/backup_history").get_json()["files"] == []

    def test_admin_restore_rejects_unknown_or_traversal(self, client):
        owner = make_owner(email="platform-admin@test.local")
        login_owner(client, owner, email="platform-admin@test.local")
        assert client.post("/restore_backup/..%5C..%5Capp.py").status_code == 404
        assert client.post("/restore_backup/nope.sql.gz").status_code == 404


class TestDeleteAccount:
    def test_requires_password(self, client):
        owner = make_owner()
        login_owner(client, owner)
        client.post("/delete_account", data={"password": "wrong"})
        assert q("SELECT COUNT(*) n FROM users", one=True)["n"] == 1
        client.post("/delete_account", data={"password": PASSWORD})
        assert q("SELECT COUNT(*) n FROM users", one=True)["n"] == 0


# ----------------------------------------------------------------------
# Tenant isolation
# ----------------------------------------------------------------------
class TestTenantIsolation:
    def test_other_tenant_cannot_touch_entries_or_charts(self, client):
        a = make_owner("a@test.local")
        b = make_owner("b@test.local")
        make_vendor(a, 101, "A vendor")
        make_vendor(b, 101, "B vendor")       # same business vendor id, other tenant
        enable_fat_snf(a)
        enable_fat_snf(b)
        login_owner(client, a, "a@test.local")
        chart = client.post("/api/fat_snf/charts", json=reference_chart_payload()).get_json()["chart_id"]
        d, s = now_date_slot()
        eid = client.post("/api/fat_snf/entries", json={
            "vendor_id": 101, "milk_type": "cow", "fat": "3.5", "snf": "8.5", "litres": "10",
            "date": d, "slot": s}).get_json()["entry"]["id"]

        login_owner(client, b, "b@test.local")
        assert client.get(f"/api/fat_snf/entries/{eid}").status_code == 404
        assert client.put(f"/api/fat_snf/entries/{eid}",
                          json={"fat": "4", "snf": "8.5", "litres": "1"}).status_code == 404
        assert client.delete(f"/api/fat_snf/entries/{eid}", json={"reason": "x"}).status_code == 404
        assert client.get(f"/api/fat_snf/charts/{chart}").status_code == 404
        assert client.put(f"/api/fat_snf/charts/{chart}",
                          json=reference_chart_payload()).status_code == 404
        assert client.post(f"/api/fat_snf/charts/{chart}/status",
                           json={"active": False}).status_code == 404
        # B has no chart: A's chart must not be used for B's vendor
        r = client.get("/api/fat_snf/rate?vendor_id=101&milk_type=cow&fat=3.5&snf=8.5")
        assert r.status_code == 422
        d_list = client.get(f"/api/fat_snf/entries?date={d}").get_json()
        assert d_list["entries"] == []
        html = client.get(f"/fat_snf/calculation?from_date={d}&all=1").get_data(as_text=True)
        assert "A vendor" not in html and "B vendor" in html
        # A's entry untouched
        assert q("SELECT quantity FROM fat_snf_milk_collection WHERE id=%s", (eid,), one=True)

    def test_customer_cannot_read_other_vendor(self, client):
        a = make_owner()
        make_vendor(a, 1, "Mine")
        make_vendor(a, 2, "Other")
        with client.session_transaction() as s:
            s.update(loggedin=True, role="customer", id=a, owner_id=a, vendor_id=1, customer_id=1)
        html = client.post("/receipt/2", data={"from_date": "2026-01-01",
                                               "to_date": "2026-01-02"}).get_data(as_text=True)
        assert "Mine" in html and "Other" not in html
        assert client.get("/payment").status_code == 302


# ----------------------------------------------------------------------
# Authentication flows
# ----------------------------------------------------------------------
class TestStaffOtp:
    def test_otp_never_in_cookie_and_flow_works(self, client, outbox):
        owner = make_owner()
        make_staff(owner)
        r = client.post("/login", data={"email": "staff@test.local", "password": PASSWORD})
        assert r.status_code == 302 and r.headers["Location"].endswith("/verify-staff-otp")
        otp = re.search(r"\b(\d{6})\b", outbox[-1]["body"]).group(1)
        cookie = json.loads(decode_session_cookie(client))
        assert otp not in json.dumps(cookie)
        assert set(cookie) <= {"_flashes", "_permanent", "staff_login_pending", "staff_challenge_id"}
        assert client.post("/verify-staff-otp", data={"otp": otp}).headers["Location"].endswith("/")
        with client.session_transaction() as s:
            assert s["role"] == "staff" and s["id"] == owner

    def test_otp_locks_after_five_wrong(self, client, outbox):
        owner = make_owner()
        make_staff(owner)
        client.post("/login", data={"email": "staff@test.local", "password": PASSWORD})
        otp = re.search(r"\b(\d{6})\b", outbox[-1]["body"]).group(1)
        wrong = "000000" if otp != "000000" else "111111"
        for _ in range(4):
            assert client.post("/verify-staff-otp", data={"otp": wrong}).status_code == 200
        r = client.post("/verify-staff-otp", data={"otp": wrong})
        assert r.status_code == 302 and "/login" in r.headers["Location"]
        r = client.post("/verify-staff-otp", data={"otp": otp})   # even the right OTP now fails
        assert "/login" in r.headers["Location"]


class TestPasswordReset:
    def test_bruteforce_locked(self, client, outbox):
        make_owner()
        client.post("/forgot-password", data={"email": "owner@test.local"})
        otp = re.search(r"(\d{6})", outbox[-1]["body"]).group(1)
        wrong = "000000" if otp != "000000" else "111111"
        for _ in range(5):
            client.post("/verify-reset-otp", data={"otp": wrong})
        r = client.post("/verify-reset-otp", data={"otp": otp})
        assert r.status_code == 302 and "forgot-password" in r.headers["Location"]
        assert client.get("/reset-password").status_code == 302

    def test_reset_flow_and_policy(self, client, outbox):
        make_owner()
        client.post("/forgot-password", data={"email": "owner@test.local"})
        otp = re.search(r"(\d{6})", outbox[-1]["body"]).group(1)
        assert client.post("/verify-reset-otp", data={"otp": otp}).headers["Location"].endswith(
            "/reset-password")
        client.post("/reset-password", data={"password": "weak", "confirm_password": "weak"})
        r = client.post("/login", data={"email": "owner@test.local", "password": PASSWORD})
        assert r.headers["Location"].endswith("/")   # weak password was rejected
        client.get("/logout")
        client.post("/forgot-password", data={"email": "owner@test.local"})
        otp = re.search(r"(\d{6})", outbox[-1]["body"]).group(1)
        client.post("/verify-reset-otp", data={"otp": otp})
        client.post("/reset-password", data={"password": "NewPass@123",
                                             "confirm_password": "NewPass@123"})
        r = client.post("/login", data={"email": "owner@test.local", "password": "NewPass@123"})
        assert r.headers["Location"].endswith("/")


class TestSignup:
    def test_signup_otp_server_side(self, client, outbox):
        client.post("/signup", data={"dairy_name": "D", "email": "new@test.local",
                                     "password": PASSWORD, "confirm_password": PASSWORD})
        otp = re.search(r"(\d{6})", outbox[-1]["body"]).group(1)
        assert otp not in decode_session_cookie(client)
        r = client.post("/verify-account", data={"otp": otp})
        assert r.headers["Location"].endswith("/login")
        u = q("SELECT dairy_code, is_verified FROM users WHERE email='new@test.local'", one=True)
        assert u["is_verified"] == 1 and u["dairy_code"].startswith("DM")


class TestRateLimits:
    def test_login_rate_limited(self, client):
        make_owner()
        codes = [client.post("/login", data={"email": "owner@test.local", "password": "bad"}).status_code
                 for _ in range(11)]
        assert codes[-1] == 429 and 429 not in codes[:10]

    def test_customer_login_rate_limited(self, client):
        codes = [client.post("/customer/login", data={"dairy_id": "DM000001", "phone": str(i)}).status_code
                 for i in range(21)]
        assert codes[-1] == 429


# ----------------------------------------------------------------------
# Injection / XSS / headers / config
# ----------------------------------------------------------------------
class TestInjection:
    def test_xss_in_vendor_name_is_escaped(self, client):
        owner = make_owner()
        make_vendor(owner, 7, "<script>alert(1)</script>", address='"><img src=x onerror=alert(2)>')
        enable_fat_snf(owner)
        login_owner(client, owner)
        client.post("/api/fat_snf/charts", json=reference_chart_payload())
        d, s = now_date_slot()
        client.post("/api/fat_snf/entries", json={"vendor_id": 7, "milk_type": "cow", "fat": "3.5",
                                                  "snf": "8.5", "litres": "5", "date": d, "slot": s})
        for url in (f"/fat_snf/calculation?from_date={d}", f"/fat_snf/edit_entry?date={d}",
                    "/fat_snf/milk_collection", "/vendor_list"):
            html = client.get(url).get_data(as_text=True)
            assert "<script>alert(1)</script>" not in html, url
            assert "<img src=x onerror" not in html, url
        html = client.post("/fat_snf/receipt", data={"from_date": d, "to_date": d,
                                                     "vendor_id": "7"}).get_data(as_text=True)
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_csv_formula_injection_neutralised(self, client):
        owner = make_owner()
        make_vendor(owner, 8, "=HYPERLINK(\"http://evil\")")
        enable_fat_snf(owner)
        login_owner(client, owner)
        client.post("/api/fat_snf/charts", json=reference_chart_payload())
        d, s = now_date_slot()
        client.post("/api/fat_snf/entries", json={"vendor_id": 8, "milk_type": "cow", "fat": "3.5",
                                                  "snf": "8.5", "litres": "5", "date": d, "slot": s})
        csv_text = client.get(f"/fat_snf/calculation?from_date={d}&export=csv").get_data(as_text=True)
        assert "\"'=HYPERLINK" in csv_text and ',"=HYPERLINK' not in csv_text

    @pytest.mark.parametrize("payload", ["' OR '1'='1", "1; DROP TABLE vendors; --",
                                         "3.5' UNION SELECT password FROM users --"])
    def test_sql_injection_attempts_are_inert(self, client, payload):
        owner = make_owner()
        make_vendor(owner, 1, "A")
        login_owner(client, owner)
        r = client.get("/api/fat_snf/rate", query_string={"vendor_id": "1", "milk_type": "cow",
                                                         "fat": payload, "snf": "8.5"})
        assert r.status_code == 400
        r = client.get("/fat_snf/calculation", query_string={"q": payload})
        assert r.status_code == 200
        r = client.get("/vendor_list", query_string={"search": payload})
        assert r.status_code == 200
        assert q("SELECT COUNT(*) n FROM vendors", one=True)["n"] == 1


class TestHeadersAndConfig:
    def test_security_headers(self, client):
        r = client.get("/login")
        h = r.headers
        assert "default-src 'self'" in h["Content-Security-Policy"]
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["X-Frame-Options"] == "SAMEORIGIN"
        assert h["Referrer-Policy"]
        assert h["X-Request-ID"]

    def test_json_errors_have_no_stack_traces(self, client):
        owner = make_owner()
        login_owner(client, owner)
        r = client.post("/api/fat_snf/entries", data="not json", content_type="application/json")
        body = r.get_json()
        assert r.status_code in (400, 409) and "Traceback" not in json.dumps(body)

    def test_production_refuses_missing_secret_key(self):
        env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
        env["DAIRYMITR_ENV"] = "production"
        r = subprocess.run([sys.executable, "-c", "import app"], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode != 0 and "SECRET_KEY" in r.stderr

    def test_vapid_private_key_not_shipped(self):
        assert not (ROOT / "vapid_private_key.pem").exists()
        assert "vapid_private_key.pem" in (ROOT / ".gitignore").read_text()


class TestSuccessfulLoginsNotThrottled:
    def test_many_successful_logins_allowed(self, client):
        make_owner()
        for _ in range(15):
            r = client.post("/login", data={"email": "owner@test.local", "password": PASSWORD})
            assert r.status_code == 302
            client.get("/logout")
