"""Regression tests for the single-file consolidation and query optimisations."""
from datetime import date

from conftest import enable_fat_snf, login_owner, make_owner, make_vendor, q


def test_vendor_account_number_keeps_leading_zeros(client):
    owner = make_owner()
    login_owner(client, owner)
    r = client.post("/add_vendor", data={
        "name": "राम", "name_en": "Ram", "vendor_id": "7", "address": "Gaon",
        "milk_type": "cow", "phone": "0987654321", "ifsc_code": "SBIN0001234",
        "account_no": "0012345678"})
    assert r.status_code == 302
    row = q("SELECT account_no, phone FROM vendors WHERE user_id=%s AND vendor_id=7",
            (owner,), one=True)
    assert row == {"account_no": "0012345678", "phone": "0987654321"}

    r = client.post("/edit_vendor/7", data={
        "name": "राम", "name_en": "Ram", "address": "Gaon", "milk_type": "cow",
        "phone": "0987654321", "ifsc_code": "SBIN0001234", "account_no": "0099887766"})
    assert r.status_code == 302
    assert q("SELECT account_no FROM vendors WHERE user_id=%s AND vendor_id=7",
             (owner,), one=True)["account_no"] == "0099887766"


def test_new_vendor_appears_on_collection_page(client):
    owner = make_owner()
    login_owner(client, owner)
    make_vendor(owner, 1, "Pahila")
    assert "Pahila" in client.get("/milk_collection?legacy=1").get_data(as_text=True)
    # Inserted behind the app's back, as another gunicorn worker would.
    make_vendor(owner, 2, "Dusra")
    assert "Dusra" in client.get("/milk_collection?legacy=1").get_data(as_text=True)


def test_daily_milk_report_counts_only_the_selected_day(client):
    owner = make_owner()
    login_owner(client, owner)
    make_vendor(owner, 1, "A", milk_type="both")
    make_vendor(owner, 2, "B")
    for d, slot, mt, qty in [("2026-09-10", "morning", "cow", 5),
                             ("2026-09-10", "evening", "buffalo", 3),
                             ("2026-09-11", "morning", "cow", 99)]:
        q("INSERT INTO milk_collection (vendor_id,user_id,date,slot,milk_type,quantity) "
          "VALUES (1,%s,%s,%s,%s,%s)", (owner, d, slot, mt, qty))
    html = client.get("/reports/milk-summary?date=2026-09-10").get_data(as_text=True)
    assert "5.0" in html and "3.0" in html and "99.0" not in html
    assert "8.0" in html                      # vendor A total for the day
    assert html.count("0.0") >= 5             # vendor B present with zeros
    bad = client.get("/reports/milk-summary?date=not-a-date")
    assert bad.status_code == 200 and "99.0" not in bad.get_data(as_text=True)


def test_single_vendor_statement_matches_all_vendor_statement(client):
    import app as A
    owner = make_owner()
    for vid in (1, 2, 3):
        make_vendor(owner, vid, f"V{vid}")
    q("INSERT INTO milk_rates (user_id, animal, rate, date_from) VALUES (%s,'cow',40,'2026-01-01')",
      (owner,))
    for vid in (1, 2, 3):
        q("INSERT INTO milk_collection (vendor_id,user_id,date,slot,milk_type,quantity) "
          "VALUES (%s,%s,'2026-09-05','morning','cow',%s)", (vid, owner, vid * 2))
        q("INSERT INTO advance_transactions (vendor_id,user_id,transaction_type,amount,transaction_date) "
          "VALUES (%s,%s,'advance',1000,'2026-09-01'),(%s,%s,'deduction',%s,'2026-09-05')",
          (vid, owner, vid, owner, vid * 10))
    start, end = date(2026, 9, 1), date(2026, 9, 30)
    with client.application.test_request_context():
        vendors = A.list_vendors(owner)
        everyone = {s["vendor"]["vendor_id"]: s for s in A.build_statements(owner, start, end, vendors)}
    with client.application.test_request_context():
        alone = A.build_statements(owner, start, end, [A.get_vendor(owner, 2)])[0]
    full = everyone[2]
    for key in ("general_total", "milk_total", "period_deduction", "remaining_advance", "final"):
        assert alone[key] == full[key], key
    assert alone["final"] == A.money("160") - A.money("20")   # 4 L x 40 - deduction 20


def test_settings_read_once_per_request_and_refreshed_after_change(client):
    owner = make_owner()
    login_owner(client, owner)
    r = client.post("/api/fat_snf/calculation_mode", json={"calculation_mode": "fat_snf"})
    assert r.get_json()["calculation_mode"] == "fat_snf"
    assert client.get("/api/fat_snf/calculation_mode").get_json()["calculation_mode"] == "fat_snf"
    r = client.post("/api/fat_snf/settings/thresholds",
                    json={"warn_low_litres": "1", "warn_high_litres": "50", "warn_deviation_pct": 40})
    assert r.status_code == 200
    s = client.get("/api/fat_snf/calculation_mode").get_json()["settings"]
    assert (s["warn_high_litres"], s["warn_deviation_pct"]) == ("50.00", 40)
    enable_fat_snf(owner)
    with client.application.test_request_context():
        import app as A
        from flask import g
        A.get_settings(owner)
        g._fsn_settings[owner]["calculation_mode"] = "tampered"
        # callers get copies; the cached row itself is only replaced by a re-read
        assert A.get_settings(owner)["calculation_mode"] == "tampered"
        A.set_mode(owner, "general")
        assert A.get_settings(owner)["calculation_mode"] == "general"


# ----------------------------------------------------------------------
# SMS / push delivery runs after the commit, off the request thread
# ----------------------------------------------------------------------
def _bulk_payload(n, day="2026-09-10"):
    return {"vendors": [{"vendor_id": str(v), "milk_type": "cow", "quantity": "5.5",
                         "date": day, "slot": "morning"} for v in range(1, n + 1)]}


def test_bulk_submit_does_not_wait_for_sms(app, client, monkeypatch):
    import threading
    import time

    owner = make_owner()
    login_owner(client, owner)
    for v in (1, 2, 3):
        make_vendor(owner, v, f"V{v}", phone=f"98765432{v:02d}")
    sent, done = [], threading.Event()

    def slow_sms(phone, text):
        time.sleep(1.5)            # a slow provider
        sent.append(phone)
        if len(sent) == 3:
            done.set()
        return True

    monkeypatch.setattr(app, "send_sms", slow_sms)
    monkeypatch.setitem(app.app.config, "TESTING", False)   # real background path
    t = time.perf_counter()
    r = client.post("/submit_bulk_milk_ajax", json=_bulk_payload(3))
    elapsed = time.perf_counter() - t
    assert r.status_code == 200, r.get_json()
    assert elapsed < 1.0                       # used to take 3 x 1.5 s
    assert q("SELECT COUNT(*) n FROM milk_collection WHERE user_id=%s", (owner,), one=True)["n"] == 3
    assert done.wait(10) and sorted(sent) == [f"98765432{v:02d}" for v in (1, 2, 3)]


def test_bulk_submit_writes_one_notification_per_saved_entry(app, client, monkeypatch):
    owner = make_owner()
    login_owner(client, owner)
    for v in (1, 2):
        make_vendor(owner, v, f"V{v}")
    pushed = []
    monkeypatch.setattr(app, "push_to_customer", lambda *a, **k: pushed.append(a[:2]))
    r = client.post("/submit_bulk_milk_ajax", json=_bulk_payload(2))
    assert r.status_code == 200
    rows = q("SELECT vendor_id, type, title, message FROM customer_notifications "
             "WHERE user_id=%s ORDER BY vendor_id", (owner,))
    assert [(x["vendor_id"], x["type"], x["title"]) for x in rows] == [
        (1, "milk", "Milk Collected"), (2, "milk", "Milk Collected")]
    assert "Quantity : 5.5 L" in rows[0]["message"]
    assert sorted(pushed) == [(owner, "1"), (owner, "2")]
    # retrying the same batch saves nothing new and sends nothing new
    pushed.clear()
    r = client.post("/submit_bulk_milk_ajax", json=_bulk_payload(2))
    assert r.status_code == 207 and pushed == []
    assert q("SELECT COUNT(*) n FROM customer_notifications WHERE user_id=%s",
             (owner,), one=True)["n"] == 2


def test_no_sms_when_bulk_commit_fails(app, client, monkeypatch):
    owner = make_owner()
    login_owner(client, owner)
    make_vendor(owner, 1, "V1", phone="9876543201")
    sent = []
    monkeypatch.setattr(app, "send_sms", lambda phone, text: sent.append(phone))

    class FailingCommit:
        def __init__(self, conn):
            self._conn = conn

        def commit(self):
            raise RuntimeError("simulated commit failure")

        def __getattr__(self, name):
            return getattr(self._conn, name)

    real = type(app.mysql).connection
    monkeypatch.setattr(type(app.mysql), "connection",
                        property(lambda self: FailingCommit(real.fget(self))))
    try:
        client.post("/submit_bulk_milk_ajax", json=_bulk_payload(1))
    except RuntimeError:
        pass
    assert sent == []          # SMS used to be sent before the commit


def test_notification_failure_after_save_is_not_an_error(app, client, monkeypatch):
    owner = make_owner()
    login_owner(client, owner)
    make_vendor(owner, 1, "V1")

    def boom(*a, **k):
        raise RuntimeError("notification table unavailable")

    monkeypatch.setattr(app, "create_milk_notification", boom)
    r = client.post("/submit_milk_ajax", json={"vendor_id": "1", "milk_type": "cow",
                                               "quantity": "4", "date": "2026-09-10",
                                               "slot": "morning"})
    assert r.status_code == 200
    assert q("SELECT COUNT(*) n FROM milk_collection WHERE user_id=%s", (owner,), one=True)["n"] == 1


def test_delivery_during_worker_shutdown_does_not_fail_the_save(app, client, monkeypatch):
    """gunicorn recycles workers (max_requests); during shutdown the thread
    pool refuses work. The save must still succeed and the SMS still go out."""
    owner = make_owner()
    login_owner(client, owner)
    make_vendor(owner, 1, "V1", phone="9876543201")
    sent = []
    monkeypatch.setattr(app, "send_sms", lambda phone, text: sent.append(phone))

    class ShutDownPool:
        def submit(self, *a, **k):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(app, "_notify_executor", lambda: ShutDownPool())
    monkeypatch.setitem(app.app.config, "TESTING", False)
    r = client.post("/submit_milk_ajax", json={"vendor_id": "1", "milk_type": "cow", "quantity": "4",
                                               "date": "2026-09-10", "slot": "morning"})
    assert r.status_code == 200
    assert sent == ["9876543201"]
