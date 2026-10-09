"""Failure handling: database unavailable, partial failures roll back."""
from decimal import Decimal as D

import pytest

from conftest import enable_fat_snf, login_owner, make_owner, make_vendor, now_date_slot, q


@pytest.fixture()
def db_down(app):
    real = app.app.config["MYSQL_PORT"]
    app.app.config["MYSQL_PORT"] = 1        # nothing listens there
    app.app.config["MYSQL_CONNECT_TIMEOUT"] = 2
    yield
    app.app.config["MYSQL_PORT"] = real


def test_api_reports_503_not_success_when_db_down(app, client, db_down):
    login_owner(client, 1)
    r = client.post("/api/fat_snf/entries", json={"vendor_id": 1, "milk_type": "cow", "fat": "3.5",
                                                   "snf": "8.5", "litres": "5"})
    body = r.get_json()
    assert r.status_code in (409, 503)      # mode lookup fails closed (OFF) or DB error
    assert body["success"] is False and "Traceback" not in str(body)
    r = client.get("/api/fat_snf/entries")
    assert r.status_code == 503 and r.get_json()["code"] == "db_unavailable"


def test_deep_healthcheck_reports_db_down(app, client, db_down):
    r = client.get("/healthcheck?deep=1")
    assert r.status_code == 503 and r.get_json() == {"database": "unavailable"}


def test_update_entries_rolls_back_on_error(app, client):
    """Legacy bulk edit: an invalid value half-way must undo earlier changes
    (previously autocommit made each statement permanent)."""
    owner = make_owner()
    make_vendor(owner, 1, "A", "cow")
    q("INSERT INTO milk_collection (vendor_id, user_id, date, slot, milk_type, quantity) "
      "VALUES (1,%s,'2026-09-01','morning','cow',10)", (owner,))
    login_owner(client, owner)
    client.post("/update_entries", data={
        "vendor_id": 1, "from_date": "2026-09-01", "to_date": "2026-09-02",
        "cow_morning_2026-09-01": "99", "cow_evening_2026-09-01": "0",
        "cow_morning_2026-09-02": "abc", "cow_evening_2026-09-02": "0"})
    assert q("SELECT quantity FROM milk_collection", one=True)["quantity"] == 10


def test_chart_creation_is_atomic(app, client, monkeypatch):
    """A failure AFTER the chart and its 95 cells were inserted (here: the
    audit write) must roll everything back — no orphan chart or cells."""
    owner = make_owner()
    login_owner(client, owner)
    from conftest import reference_chart_payload
    from fat_snf import service

    def boom(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(service.audit, "record", boom)
    r = client.post("/api/fat_snf/charts", json=reference_chart_payload())
    assert r.status_code == 500 and r.get_json()["success"] is False
    assert q("SELECT COUNT(*) n FROM fat_snf_charts", one=True)["n"] == 0
    assert q("SELECT COUNT(*) n FROM fat_snf_chart_values", one=True)["n"] == 0


def test_entry_save_is_atomic(app, client, monkeypatch):
    owner = make_owner()
    make_vendor(owner, 1, "A")
    enable_fat_snf(owner)
    login_owner(client, owner)
    from conftest import reference_chart_payload
    client.post("/api/fat_snf/charts", json=reference_chart_payload())
    from fat_snf import service

    def boom(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(service.audit, "record", boom)
    d, s = now_date_slot()
    r = client.post("/api/fat_snf/entries", json={"vendor_id": 1, "milk_type": "cow", "fat": "3.5",
                                                   "snf": "8.5", "litres": "5", "date": d, "slot": s})
    assert r.status_code == 500
    assert q("SELECT COUNT(*) n FROM fat_snf_milk_collection", one=True)["n"] == 0
