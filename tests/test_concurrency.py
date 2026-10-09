"""
Concurrency tests: real threads, one MySQL connection each (Flask-MySQLdb
opens a connection per request context), so locks and unique keys are
exercised exactly as in production.
"""
import threading
from decimal import Decimal as D

from conftest import (enable_fat_snf, login_owner, make_owner, make_vendor, now_date_slot, q,
                      reference_chart_payload)


def run_parallel(app_module, n, fn):
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        client = app_module.app.test_client()
        barrier.wait()
        results[i] = fn(client, i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


def owner_session(client, owner):
    login_owner(client, owner)
    return client


class TestConcurrentWrites:
    def test_same_request_id_saved_once(self, app, client):
        owner = make_owner()
        make_vendor(owner, 1, "A")
        enable_fat_snf(owner)
        login_owner(client, owner)
        client.post("/api/fat_snf/charts", json=reference_chart_payload())
        d, s = now_date_slot()
        body = {"vendor_id": 1, "milk_type": "cow", "fat": "3.5", "snf": "8.5", "litres": "10",
                "date": d, "slot": s, "request_id": "same-key"}

        def save(c, i):
            owner_session(c, owner)
            return c.post("/api/fat_snf/entries", json=body).status_code

        codes = run_parallel(app, 8, save)
        assert q("SELECT COUNT(*) n FROM fat_snf_milk_collection", one=True)["n"] == 1
        assert sorted(codes).count(201) == 1
        assert all(c in (200, 201) for c in codes), codes   # retries replay, never 409/500

    def test_different_request_ids_same_slot_saved_once(self, app, client):
        owner = make_owner()
        make_vendor(owner, 1, "A")
        enable_fat_snf(owner)
        login_owner(client, owner)
        client.post("/api/fat_snf/charts", json=reference_chart_payload())
        d, s = now_date_slot()

        def save(c, i):
            owner_session(c, owner)
            return c.post("/api/fat_snf/entries", json={
                "vendor_id": 1, "milk_type": "cow", "fat": "3.5", "snf": "8.5", "litres": str(10 + i),
                "date": d, "slot": s, "request_id": f"k{i}"}).status_code

        codes = run_parallel(app, 6, save)
        assert q("SELECT COUNT(*) n FROM fat_snf_milk_collection", one=True)["n"] == 1
        assert codes.count(201) == 1 and codes.count(409) == 5, codes

    def test_advance_deductions_cannot_overdraw(self, app, client):
        """Before the transaction fix (autocommit ON), the FOR UPDATE lock was
        released immediately, so parallel deductions could all pass the
        balance check and drive the ledger negative."""
        owner = make_owner()
        make_vendor(owner, 1, "A")
        q("INSERT INTO advance_transactions (vendor_id, user_id, transaction_type, amount, "
          "transaction_date) VALUES (1,%s,'advance',1000,'2026-10-01')", (owner,))

        def deduct(c, i):
            owner_session(c, owner)
            return c.post("/advance/deduct", json={"vendor_id": 1, "amount": "1000",
                                                   "date": "2026-10-02"}).status_code

        codes = run_parallel(app, 6, deduct)
        bal = q("SELECT SUM(CASE WHEN transaction_type='advance' THEN amount ELSE -amount END) b "
                "FROM advance_transactions", one=True)["b"]
        assert bal == D("0.00"), (bal, codes)
        assert codes.count(200) == 1
