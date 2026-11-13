"""DairyPro features: subscriptions (Razorpay mocked), trial, offline sync
contract, tenant isolation, language, per-animal FAT/SNF ranges."""
import hashlib
import hmac
import json
from datetime import timedelta

import pytest

from conftest import (enable_fat_snf, login_owner, login_staff, make_owner, make_staff,
                      make_vendor, now_date_slot, q, reference_chart_payload)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
class FakeRazorpay:
    """Stands in for api.razorpay.com. Records every call."""

    def __init__(self):
        self.calls = []
        self.orders = {}
        self.payments = {}
        self.fail_network = False

    def __call__(self, method, path, payload=None):
        import app as A
        self.calls.append((method, path, payload))
        if self.fail_network:
            raise A.RazorpayError("Razorpay is unreachable.")
        if method == "POST" and path == "/orders":
            oid = f"order_test{len(self.orders) + 1}"
            self.orders[oid] = dict(payload, id=oid)
            return {"id": oid, "amount": payload["amount"], "currency": payload["currency"]}
        if method == "GET" and path.startswith("/payments/"):
            pid = path.split("/")[-1]
            if pid not in self.payments:
                raise A.RazorpayError("not found", 404)
            return self.payments[pid]
        if method == "GET" and path.startswith("/orders/") and path.endswith("/payments"):
            oid = path.split("/")[2]
            return {"items": [p for p in self.payments.values() if p["order_id"] == oid]}
        raise AssertionError(f"unexpected call {method} {path}")

    def capture(self, order_id, payment_id, amount, status="captured"):
        self.payments[payment_id] = {"id": payment_id, "order_id": order_id, "amount": amount,
                                     "currency": "INR", "status": status}


@pytest.fixture()
def razorpay(app, monkeypatch):
    fake = FakeRazorpay()
    monkeypatch.setattr(app, "RAZORPAY_KEY_ID", "rzp_test_key")
    monkeypatch.setattr(app, "RAZORPAY_KEY_SECRET", "test_secret")
    monkeypatch.setattr(app, "RAZORPAY_WEBHOOK_SECRET", "hook_secret")
    monkeypatch.setattr(app, "razorpay_request", fake)
    return fake


def sign(order_id, payment_id, secret="test_secret"):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def webhook(client, payload, secret="hook_secret", event_id="evt_1"):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/razorpay/webhook", data=body, content_type="application/json",
                       headers={"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": event_id})


def expire_trial(owner):
    q("UPDATE dp_trials SET started_at = NOW() - INTERVAL 40 DAY, "
      "ends_at = NOW() - INTERVAL 20 DAY WHERE user_id=%s", (owner,))


@pytest.fixture()
def owner(client):
    uid = make_owner()
    make_vendor(uid, 1, "Ram", "cow")
    make_vendor(uid, 2, "Shyam", "buffalo")
    login_owner(client, uid)
    return uid


# ----------------------------------------------------------------------
# trial
# ----------------------------------------------------------------------
class TestTrial:
    def test_trial_starts_once_with_server_time(self, app, client, owner):
        client.get("/")
        t1 = q("SELECT * FROM dp_trials WHERE user_id=%s", (owner,), one=True)
        assert t1 and (t1["ends_at"] - t1["started_at"]) == timedelta(days=20)
        client.get("/")
        client.get("/settings")
        assert q("SELECT COUNT(*) n FROM dp_trials", one=True)["n"] == 1
        t2 = q("SELECT * FROM dp_trials WHERE user_id=%s", (owner,), one=True)
        assert t2["started_at"] == t1["started_at"]          # never restarted

    def test_real_login_starts_trial(self, client):
        uid = make_owner(email="real@test.local")
        r = client.post("/login", data={"email": "real@test.local", "password": "Secret@123"})
        assert r.status_code == 302
        assert q("SELECT COUNT(*) n FROM dp_trials WHERE user_id=%s", (uid,), one=True)["n"] == 1

    def test_deleted_account_does_not_get_second_trial(self, app, client, owner):
        client.get("/")
        expire_trial(owner)
        q("UPDATE dp_trials SET user_id=NULL WHERE user_id=%s", (owner,))
        q("DELETE FROM vendors WHERE user_id=%s", (owner,))
        q("DELETE FROM users WHERE id=%s", (owner,))
        again = make_owner()                                   # same e-mail
        login_owner(client, again)
        client.get("/subscription")
        row = q("SELECT *, ends_at < UTC_TIMESTAMP() AS ended FROM dp_trials WHERE user_id=%s",
                (again,), one=True)
        assert row and row["ended"] == 1                       # the OLD, expired window
        assert q("SELECT COUNT(*) n FROM dp_trials", one=True)["n"] == 1

    def test_status_api_reports_days_left(self, client, owner):
        data = client.get("/api/subscription/status").get_json()
        assert data["status"] == "trial" and data["trial_days_left"] == 20
        assert data["reminder"] == "trial_started"
        client.post("/api/subscription/reminder-seen", json={"key": "trial_started"})
        assert client.get("/api/subscription/status").get_json()["reminder"] is None


# ----------------------------------------------------------------------
# access control
# ----------------------------------------------------------------------
class TestAccess:
    def test_not_enforced_without_razorpay(self, app, client, owner):
        client.get("/")
        expire_trial(owner)
        assert client.get("/vendor_list").status_code == 200     # nobody locked out

    def test_expired_blocks_features_but_not_billing(self, app, client, owner, razorpay):
        client.get("/")
        expire_trial(owner)
        r = client.get("/vendor_list")
        assert r.status_code == 302 and "/subscription" in r.headers["Location"]
        r = client.post("/submit_milk_ajax", json={"vendor_id": 1, "milk_type": "cow", "quantity": 5})
        assert r.status_code == 402 and r.get_json()["code"] == "subscription_required"
        assert client.get("/subscription").status_code == 200
        assert client.get("/settings").status_code == 200
        assert client.get("/logout").status_code == 302

    def test_staff_follows_owner_subscription(self, app, client, owner, razorpay):
        staff = make_staff(owner)
        client.get("/")
        expire_trial(owner)
        login_staff(client, owner, staff)
        r = client.get("/milk_collection")
        assert r.status_code == 302 and "/subscription" in r.headers["Location"]

    def test_staff_cannot_pay(self, app, client, owner, razorpay):
        staff = make_staff(owner)
        login_staff(client, owner, staff)
        r = client.post("/api/subscription/order", json={"plan_code": "monthly"})
        assert r.status_code == 403


# ----------------------------------------------------------------------
# payments
# ----------------------------------------------------------------------
class TestPayments:
    def order(self, client, plan="monthly"):
        r = client.post("/api/subscription/order", json={"plan_code": plan})
        assert r.status_code == 200, r.get_json()
        return r.get_json()

    def test_server_side_price(self, app, client, owner, razorpay):
        data = self.order(client, "six_months")
        assert data["amount"] == 119900
        _, _, payload = razorpay.calls[0]
        assert payload["amount"] == 119900
        r = client.post("/api/subscription/order", json={"plan_code": "monthly", "amount": 1})
        assert r.get_json()["amount"] == 39900                 # client amount ignored
        assert client.post("/api/subscription/order", json={"plan_code": "free"}).status_code == 400

    def test_repeated_click_reuses_order(self, app, client, owner, razorpay):
        a = self.order(client)
        b = self.order(client)
        assert a["order_id"] == b["order_id"]
        assert q("SELECT COUNT(*) n FROM dp_payment_orders", one=True)["n"] == 1

    def test_success_activates_once(self, app, client, owner, razorpay):
        o = self.order(client, "yearly")
        razorpay.capture(o["order_id"], "pay_1", 199900)
        body = {"razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_1",
                "razorpay_signature": sign(o["order_id"], "pay_1")}
        r = client.post("/api/subscription/verify", json=body)
        assert r.status_code == 200 and r.get_json()["status"] == "paid"
        # duplicate callback (Scenario D)
        r = client.post("/api/subscription/verify", json=body)
        assert r.status_code == 200 and r.get_json().get("already")
        assert q("SELECT COUNT(*) n FROM dp_subscriptions", one=True)["n"] == 1
        sub = q("SELECT * FROM dp_subscriptions", one=True)
        assert (sub["ends_at"] - sub["starts_at"]).days in (365, 366)
        assert client.get("/api/subscription/status").get_json()["status"] == "active"

    def test_bad_signature_never_activates(self, app, client, owner, razorpay):
        o = self.order(client)
        razorpay.capture(o["order_id"], "pay_1", 39900)
        r = client.post("/api/subscription/verify", json={
            "razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_1",
            "razorpay_signature": "forged"})
        assert r.status_code == 400
        assert q("SELECT COUNT(*) n FROM dp_subscriptions", one=True)["n"] == 0

    def test_signature_alone_is_not_enough(self, app, client, owner, razorpay):
        """Valid signature but Razorpay says the payment is not captured."""
        o = self.order(client)
        razorpay.capture(o["order_id"], "pay_1", 39900, status="failed")
        r = client.post("/api/subscription/verify", json={
            "razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_1",
            "razorpay_signature": sign(o["order_id"], "pay_1")})
        assert r.status_code == 402
        assert q("SELECT COUNT(*) n FROM dp_subscriptions", one=True)["n"] == 0

    def test_amount_mismatch_rejected(self, app, client, owner, razorpay):
        o = self.order(client, "yearly")
        razorpay.capture(o["order_id"], "pay_1", 100)            # paid 1 rupee
        r = client.post("/api/subscription/verify", json={
            "razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_1",
            "razorpay_signature": sign(o["order_id"], "pay_1")})
        assert r.status_code == 409
        assert q("SELECT COUNT(*) n FROM dp_subscriptions", one=True)["n"] == 0

    def test_money_deducted_but_no_response_then_reconciled(self, app, client, owner, razorpay):
        """Scenario C: callback reaches us but Razorpay API is down -> pending,
        never failed, never charged again; later reconciliation activates."""
        o = self.order(client)
        razorpay.capture(o["order_id"], "pay_9", 39900)
        razorpay.fail_network = True
        r = client.post("/api/subscription/verify", json={
            "razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_9",
            "razorpay_signature": sign(o["order_id"], "pay_9")})
        assert r.status_code == 202 and r.get_json()["status"] == "pending"
        assert q("SELECT status FROM dp_payment_orders", one=True)["status"] == "pending"
        assert client.get("/api/subscription/status").get_json()["status"] == "trial"
        razorpay.fail_network = False
        r = client.post("/api/subscription/reconcile", json={})
        assert r.get_json()["status"] == "active"
        assert q("SELECT verified_via FROM dp_payment_orders", one=True)["verified_via"] == "reconcile"
        orders_created = [c for c in razorpay.calls if c[0] == "POST"]
        assert len(orders_created) == 1                          # no second charge

    def test_back_button_keeps_order_retryable(self, app, client, owner, razorpay):
        o = self.order(client)
        r = client.post("/api/subscription/attempt", json={"event": "dismissed",
                                                           "razorpay_order_id": o["order_id"]})
        assert r.status_code == 200
        assert q("SELECT status FROM dp_payment_orders", one=True)["status"] == "created"
        assert self.order(client)["order_id"] == o["order_id"]   # retry uses same order
        assert q("SELECT COUNT(*) n FROM dp_subscriptions", one=True)["n"] == 0

    def test_failure_recorded_and_retry_allowed(self, app, client, owner, razorpay):
        o = self.order(client)
        client.post("/api/subscription/attempt", json={
            "event": "failed", "razorpay_order_id": o["order_id"], "code": "BAD_REQUEST_ERROR",
            "description": "Card declined"})
        row = q("SELECT * FROM dp_payment_orders", one=True)
        assert row["status"] == "attempted" and row["failure_reason"] == "Card declined"
        razorpay.capture(o["order_id"], "pay_2", 39900)
        r = client.post("/api/subscription/verify", json={
            "razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_2",
            "razorpay_signature": sign(o["order_id"], "pay_2")})
        assert r.get_json()["status"] == "paid"

    def test_cannot_verify_another_owners_order(self, app, client, owner, razorpay):
        o = self.order(client)
        other = make_owner(email="other@test.local")
        login_owner(client, other, email="other@test.local")
        razorpay.capture(o["order_id"], "pay_x", 39900)
        r = client.post("/api/subscription/verify", json={
            "razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_x",
            "razorpay_signature": sign(o["order_id"], "pay_x")})
        assert r.status_code == 404

    def test_renewal_extends_current_period(self, app, client, owner, razorpay):
        for n, pid in enumerate(("p1", "p2")):
            o = self.order(client)
            razorpay.capture(o["order_id"], pid, 39900)
            client.post("/api/subscription/verify", json={
                "razorpay_order_id": o["order_id"], "razorpay_payment_id": pid,
                "razorpay_signature": sign(o["order_id"], pid)})
        subs = q("SELECT * FROM dp_subscriptions ORDER BY id")
        assert len(subs) == 2 and subs[1]["starts_at"] == subs[0]["ends_at"]


class TestWebhook:
    def make_order(self, client):
        r = client.post("/api/subscription/order", json={"plan_code": "monthly"})
        return r.get_json()["order_id"]

    def captured(self, order_id, pid="pay_w", amount=39900):
        return {"event": "payment.captured", "payload": {"payment": {"entity": {
            "id": pid, "order_id": order_id, "amount": amount, "currency": "INR",
            "status": "captured"}}}}

    def test_bad_signature_rejected(self, app, client, owner, razorpay):
        oid = self.make_order(client)
        r = webhook(client, self.captured(oid), secret="wrong")
        assert r.status_code == 400
        assert q("SELECT COUNT(*) n FROM dp_webhook_events", one=True)["n"] == 0
        assert q("SELECT COUNT(*) n FROM dp_subscriptions", one=True)["n"] == 0

    def test_activates_and_replay_ignored(self, app, client, owner, razorpay):
        oid = self.make_order(client)
        client.application.config["WTF_CSRF_ENABLED"] = True     # webhook is CSRF-exempt
        r = webhook(client, self.captured(oid))
        assert r.status_code == 200 and r.get_json()["result"] == "activated"
        r = webhook(client, self.captured(oid))                   # same event id (Scenario E)
        assert r.get_json().get("duplicate") is True
        r = webhook(client, self.captured(oid), event_id="evt_2") # new id, same payment
        assert r.get_json()["result"] == "already_paid"
        assert q("SELECT COUNT(*) n FROM dp_subscriptions", one=True)["n"] == 1
        client.application.config["WTF_CSRF_ENABLED"] = False

    def test_webhook_amount_mismatch(self, app, client, owner, razorpay):
        oid = self.make_order(client)
        r = webhook(client, self.captured(oid, amount=100))
        assert r.get_json()["result"] == "amount_mismatch"
        assert q("SELECT COUNT(*) n FROM dp_subscriptions", one=True)["n"] == 0

    def test_full_refund_ends_period(self, app, client, owner, razorpay):
        oid = self.make_order(client)
        webhook(client, self.captured(oid))
        r = webhook(client, {"event": "refund.processed", "payload": {"refund": {"entity": {
            "id": "rfnd_1", "payment_id": "pay_w", "amount": 39900}}}}, event_id="evt_r")
        assert r.get_json()["result"] == "refunded"
        assert q("SELECT status FROM dp_subscriptions", one=True)["status"] == "refunded"
        assert client.get("/api/subscription/status").get_json()["status"] == "trial"

    def test_partial_refund_keeps_access(self, app, client, owner, razorpay):
        oid = self.make_order(client)
        webhook(client, self.captured(oid))
        r = webhook(client, {"event": "refund.processed", "payload": {"refund": {"entity": {
            "id": "rfnd_2", "payment_id": "pay_w", "amount": 10000}}}}, event_id="evt_p")
        assert r.get_json()["result"] == "partially_refunded"
        assert client.get("/api/subscription/status").get_json()["status"] == "active"

    def test_payment_failed_event(self, app, client, owner, razorpay):
        oid = self.make_order(client)
        r = webhook(client, {"event": "payment.failed", "payload": {"payment": {"entity": {
            "id": "pay_f", "order_id": oid, "error_code": "BAD_REQUEST_ERROR",
            "error_description": "Payment declined"}}}})
        assert r.get_json()["result"] == "marked_failed"
        assert q("SELECT status FROM dp_payment_orders", one=True)["status"] == "failed"


# ----------------------------------------------------------------------
# offline sync contract
# ----------------------------------------------------------------------
class TestOfflineSync:
    def milk(self, client, rid="11111111-1111-4111-8111-111111111111", **extra):
        d, s = now_date_slot()
        body = {"vendor_id": 1, "milk_type": "cow", "quantity": "12.5", "date": d, "slot": s,
                "client_request_id": rid}
        body.update(extra)
        return client.post("/submit_milk_ajax", json=body,
                           headers={"X-Requested-With": "XMLHttpRequest"})

    def test_explicit_ack_and_idempotent_replay(self, client, owner):
        r = self.milk(client)
        assert r.status_code == 200 and r.get_json()["saved"] is True
        r = self.milk(client)                                    # retry after lost answer
        assert r.status_code == 200 and r.get_json()["replayed"] is True
        assert q("SELECT COUNT(*) n FROM milk_collection", one=True)["n"] == 1

    def test_different_entry_same_slot_is_duplicate_conflict(self, client, owner):
        self.milk(client)
        r = self.milk(client, rid="22222222-2222-4222-8222-222222222222", quantity="9")
        assert r.status_code == 409 and r.get_json()["code"] == "duplicate"
        assert q("SELECT quantity FROM milk_collection", one=True)["quantity"] == 12.5

    def test_anomaly_warning_is_not_a_save(self, app, client, owner, monkeypatch):
        monkeypatch.setattr(app, "detect_anomaly", lambda prev, new: True)
        r = self.milk(client)
        assert r.status_code == 409 and r.get_json()["code"] == "needs_confirmation"
        assert r.get_json()["saved"] is False
        assert q("SELECT COUNT(*) n FROM milk_collection", one=True)["n"] == 0
        r = self.milk(client, force_save=True)
        assert r.status_code == 200 and r.get_json()["saved"] is True

    def test_expired_session_is_json_401_not_redirect(self, client):
        r = client.post("/submit_milk_ajax", json={"vendor_id": 1, "quantity": 5},
                        headers={"X-Requested-With": "XMLHttpRequest"})
        assert r.status_code == 401 and r.get_json()["code"] == "auth_required"
        r = client.get("/api/session")
        assert r.status_code == 401

    def test_tenant_mismatch_saves_nothing(self, client, owner):
        other = make_owner(email="b@test.local")
        make_vendor(other, 1, "B", "cow")
        d, s = now_date_slot()
        r = client.post("/submit_milk_ajax", json={
            "vendor_id": 1, "milk_type": "cow", "quantity": "5", "date": d, "slot": s,
            "client_request_id": "33333333-3333-4333-8333-333333333333"},
            headers={"X-DP-Tenant": str(other)})
        assert r.status_code == 409 and r.get_json()["code"] == "tenant_mismatch"
        assert q("SELECT COUNT(*) n FROM milk_collection", one=True)["n"] == 0
        r = client.post("/submit_milk_ajax", json={
            "vendor_id": 1, "milk_type": "cow", "quantity": "5", "date": d, "slot": s,
            "client_request_id": "33333333-3333-4333-8333-333333333333"},
            headers={"X-DP-Tenant": str(owner)})
        assert r.status_code == 200
        assert q("SELECT user_id FROM milk_collection", one=True)["user_id"] == owner

    def test_session_ping_returns_tenant_and_token(self, client, owner):
        data = client.get("/api/session").get_json()
        assert data["tenant_id"] == owner and data["csrf_token"] and data["role"] == "owner"

    def test_validation_rejects_bad_values(self, client, owner):
        assert self.milk(client, quantity="-2").status_code == 400
        assert self.milk(client, milk_type="goat").status_code == 400
        assert self.milk(client, date="2999-01-01").status_code == 400
        assert self.milk(client, vendor_id=999).status_code == 404
        assert self.milk(client, milk_type="buffalo").status_code == 400    # vendor 1 = cow
        assert self.milk(client, client_request_id="bad id!").status_code == 400

    def test_customer_deleted_while_offline(self, client, owner):
        q("DELETE FROM vendors WHERE user_id=%s AND vendor_id=1", (owner,))
        r = self.milk(client)
        assert r.status_code == 404 and r.get_json()["code"] == "vendor_not_found"

    def test_advance_idempotent(self, client, owner):
        body = {"vendor_id": 1, "amount": "500", "date": now_date_slot()[0],
                "client_request_id": "44444444-4444-4444-8444-444444444444"}
        r1 = client.post("/advance/add", json=body)
        r2 = client.post("/advance/add", json=body)
        assert r1.status_code == 200 and r2.get_json()["replayed"] is True
        assert q("SELECT COUNT(*) n FROM advance_transactions", one=True)["n"] == 1

    def test_food_sack_upsert_is_replay_safe(self, client, owner):
        sid = q("INSERT INTO food_sack_rates (user_id, name, rate, date_from) "
                "VALUES (%s,'Pend',1200,'2026-01-01')", (owner,))
        body = {"vendor_id": 1, "quantity": 2, "sack_id": sid, "date": now_date_slot()[0]}
        assert client.post("/submit_food_sack_ajax", json=body).get_json()["saved"] is True
        assert client.post("/submit_food_sack_ajax", json=body).get_json()["saved"] is True
        assert q("SELECT COUNT(*) n FROM food_sack", one=True)["n"] == 1
        other = make_owner(email="c@test.local")
        make_vendor(other, 7, "X", "cow")
        r = client.post("/submit_food_sack_ajax", json=dict(body, vendor_id=7))
        assert r.status_code == 404                              # not this dairy's customer

    def test_page_cache_scope_header(self, client, owner):
        r = client.get("/settings")
        assert r.headers["X-DP-Cache-Scope"] == f"o{owner}"

    def test_service_worker_served_from_root(self, client):
        r = client.get("/service-worker.js")
        assert r.status_code == 200 and r.headers["Service-Worker-Allowed"] == "/"
        assert b"dairypro-sync" in r.data or b"SYNC_TAG" in r.data


# ----------------------------------------------------------------------
# FAT/SNF per-animal ranges
# ----------------------------------------------------------------------
@pytest.fixture()
def fsn(client):
    uid = make_owner()
    make_vendor(uid, 101, "Cow farmer", "cow")
    make_vendor(uid, 102, "Buffalo farmer", "buffalo")
    login_owner(client, uid)
    enable_fat_snf(uid)
    assert client.post("/api/fat_snf/charts", json=reference_chart_payload("cow")).status_code == 201
    return uid


def buffalo_payload():
    from app import generate_matrix
    settings = dict(fat_min="5.0", fat_max="11.0", fat_step="0.1", snf_min="8.0",
                    snf_max="10.5", snf_step="0.1", std_fat="6.0", std_snf="9.0",
                    std_rate="45.00", fat_rate_step="0.40", snf_rate_step="0.40")
    fats, snfs, matrix = generate_matrix(settings)
    return {"chart_name": "Buffalo", "milk_type": "buffalo", "effective_from": "2026-01-01",
            "settings": settings, "fat_values": [str(f) for f in fats],
            "snf_values": [str(s) for s in snfs],
            "matrix": [{"fat": str(f), "snf": str(s), "rate": str(r)} for (f, s), r in matrix.items()]}


def fsn_entry(client, vendor, milk_type, fat, snf, litres="10"):
    d, s = now_date_slot()
    return client.post("/api/fat_snf/entries", json={
        "vendor_id": vendor, "milk_type": milk_type, "fat": fat, "snf": snf, "litres": litres,
        "date": d, "slot": s})


class TestAnimalRanges:
    def test_buffalo_chart_with_buffalo_range(self, client, fsn):
        r = client.post("/api/fat_snf/charts", json=buffalo_payload())
        assert r.status_code == 201, r.get_json()
        assert q("SELECT COUNT(*) n FROM fat_snf_chart_values v JOIN fat_snf_charts c "
                 "ON c.id=v.chart_id WHERE c.milk_type='buffalo'", one=True)["n"] == 61 * 26

    def test_buffalo_chart_rejects_cow_values(self, client, fsn):
        r = client.post("/api/fat_snf/charts", json=reference_chart_payload("buffalo", name="b"))
        assert r.status_code == 400 and r.get_json()["field"] == "fat_values"

    @pytest.mark.parametrize("fat,snf,ok", [
        ("2.50", "8.00", True), ("5.00", "9.00", True), ("3.50", "8.50", True),
        ("2.49", "8.50", False), ("5.01", "8.50", False),
        ("3.50", "7.99", False), ("3.50", "9.01", False)])
    def test_cow_boundaries(self, client, fsn, fat, snf, ok):
        r = fsn_entry(client, 101, "cow", fat, snf)
        if ok:
            assert r.status_code != 400 or r.get_json()["field"] not in ("fat", "snf"), r.get_json()
        else:
            assert r.status_code == 400 and r.get_json()["field"] in ("fat", "snf")

    @pytest.mark.parametrize("fat,snf,ok", [
        ("5.00", "8.00", True), ("11.00", "10.50", True), ("6.00", "9.00", True),
        ("4.99", "9.00", False), ("11.01", "9.00", False),
        ("6.00", "7.99", False), ("6.00", "10.51", False)])
    def test_buffalo_boundaries(self, client, fsn, fat, snf, ok):
        client.post("/api/fat_snf/charts", json=buffalo_payload())
        r = fsn_entry(client, 102, "buffalo", fat, snf)
        if ok:
            assert r.status_code == 201, r.get_json()
        else:
            assert r.status_code == 400 and r.get_json()["field"] in ("fat", "snf")

    def test_rate_preview_validates_range(self, client, fsn):
        d, _ = now_date_slot()
        r = client.get(f"/api/fat_snf/rate?vendor_id=101&milk_type=cow&fat=6.0&snf=8.5&date={d}")
        assert r.status_code == 400

    def test_buffalo_rate_uses_buffalo_chart(self, client, fsn):
        client.post("/api/fat_snf/charts", json=buffalo_payload())
        r = fsn_entry(client, 102, "buffalo", "6.00", "9.00")
        assert r.status_code == 201 and r.get_json()["entry"]["rate"] in ("45.00", 45.0, "45.0")


# ----------------------------------------------------------------------
# language
# ----------------------------------------------------------------------
class TestLanguage:
    def test_switch_persists_on_account_and_cookie(self, client, owner):
        r = client.post("/set-language", data={"lang": "hi"})
        assert r.status_code == 302
        assert "dp_lang=hi" in r.headers.get("Set-Cookie", "")
        assert q("SELECT preferred_language FROM users WHERE id=%s", (owner,), one=True)[
            "preferred_language"] == "hi"
        html = client.get("/settings").get_data(as_text=True)
        assert '<html lang="hi">' in html

    def test_language_restored_at_login(self, client):
        uid = make_owner(email="lang@test.local")
        q("UPDATE users SET preferred_language='en' WHERE id=%s", (uid,))
        client.post("/login", data={"email": "lang@test.local", "password": "Secret@123"})
        assert '<html lang="en">' in client.get("/settings").get_data(as_text=True)

    def test_invalid_language_rejected(self, client, owner):
        r = client.post("/set-language", json={"lang": "fr"})
        assert r.status_code == 400

    def test_js_dictionary(self, client):
        r = client.get("/i18n/en.js")
        assert r.status_code == 200 and r.data.startswith(b'window.DP_LANG="en"')
