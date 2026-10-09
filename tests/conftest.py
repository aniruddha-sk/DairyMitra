"""
Test harness.

Integration tests need a MySQL 8 server they are allowed to wipe:
    TEST_MYSQL_HOST, TEST_MYSQL_PORT, TEST_MYSQL_USER, TEST_MYSQL_PASSWORD
A database named `dairymitr_pytest` is DROPPED and recreated from schema.sql
plus all migrations. Never point these variables at production.
"""
import os
import pathlib
import sys

import MySQLdb
import MySQLdb.cursors
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = "dairymitr_pytest"
HAVE_DB = bool(os.getenv("TEST_MYSQL_HOST"))

if HAVE_DB:
    os.environ.update({
        "MYSQL_HOST": os.environ["TEST_MYSQL_HOST"],
        "MYSQL_PORT": os.getenv("TEST_MYSQL_PORT", "3306"),
        "MYSQL_USER": os.getenv("TEST_MYSQL_USER", "root"),
        "MYSQL_PASSWORD": os.getenv("TEST_MYSQL_PASSWORD", ""),
        "MYSQL_DB": TEST_DB,
    })
os.environ.setdefault("DAIRYMITR_ENV", "test")
os.environ.setdefault("SECRET_KEY", "t" * 64)
os.environ["SESSION_COOKIE_SECURE"] = "0"
os.environ["LOG_TO_STDOUT"] = "0"
os.environ["AUDIT_LOG_FILE"] = str(ROOT / "tests" / ".test-audit.log")
os.environ["DAIRYMITR_ADMIN_EMAILS"] = "platform-admin@test.local"
for k in ("BREVO_API_KEY", "TWILIO_ACCOUNT_SID", "GEMINI_API_KEY"):
    os.environ.pop(k, None)


def raw_conn(db=TEST_DB):
    kwargs = dict(
        host=os.environ["MYSQL_HOST"], port=int(os.environ["MYSQL_PORT"]),
        user=os.environ["MYSQL_USER"], passwd=os.environ["MYSQL_PASSWORD"],
        charset="utf8mb4", autocommit=True, cursorclass=MySQLdb.cursors.DictCursor)
    if db:
        kwargs["db"] = db
    return MySQLdb.connect(**kwargs)


def q(sql, params=(), one=False):
    conn = raw_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        if cur.description is None:
            return cur.lastrowid
        return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()


def _fresh_database():
    conn = raw_conn(db=None)
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    cur.execute(f"CREATE DATABASE {TEST_DB} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    conn.close()
    conn = raw_conn()
    cur = conn.cursor()
    sql = (ROOT / "schema.sql").read_text(encoding="utf-8")
    for chunk in sql.replace("\r\n", "\n").split(";\n"):
        body = "\n".join(l for l in chunk.splitlines()
                         if not l.strip().startswith("--")).strip().rstrip(";")
        if body:
            cur.execute(body)
    from migrations import runner
    runner.run(conn, out=lambda *_: None)
    conn.close()


@pytest.fixture(scope="session")
def app():
    if not HAVE_DB:
        pytest.skip("integration tests need TEST_MYSQL_HOST (a disposable MySQL 8 server)")
    _fresh_database()
    import app as app_module
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return app_module


@pytest.fixture()
def client(app):
    app.app.config["WTF_CSRF_ENABLED"] = False
    return app.app.test_client()


@pytest.fixture()
def outbox(app, monkeypatch):
    """Capture emails instead of sending them."""
    sent = []

    def fake_send(to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(app, "send_email", fake_send)
    return sent


@pytest.fixture(autouse=True)
def _clean_tables(request):
    if not HAVE_DB or "app" not in request.fixturenames and "client" not in request.fixturenames:
        yield
        return
    yield
    conn = raw_conn()
    cur = conn.cursor()
    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    for t in ("fat_snf_milk_collection", "fat_snf_chart_values", "fat_snf_charts",
              "fat_snf_calculation_settings", "milk_collection", "food_sack",
              "food_sack_rates", "advance_transactions", "milk_rates", "vendor_milk_rates",
              "vendors", "staff", "users", "dm_audit_log", "dm_challenges",
              "dm_rate_limits", "customer_notifications", "bank_settings"):
        cur.execute(f"DELETE FROM {t}")
    cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    conn.close()


# ----------------------------------------------------------------------
# seed helpers
# ----------------------------------------------------------------------
from werkzeug.security import generate_password_hash  # noqa: E402

PASSWORD = "Secret@123"


def make_owner(email="owner@test.local", dairy="ज्योतिर्लिंग दूध संकलन केंद्र"):
    uid = q("INSERT INTO users (email, password, dairy_name, is_verified) VALUES (%s,%s,%s,1)",
            (email, generate_password_hash(PASSWORD), dairy))
    q("UPDATE users SET dairy_code=%s WHERE id=%s", (f"DM{uid:06d}", uid))
    return uid


def make_vendor(owner, vendor_id, name, milk_type="cow", address="PINGALI BK", phone=None):
    q("INSERT INTO vendors (vendor_id, name, address, milk_type, phone, user_id) "
      "VALUES (%s,%s,%s,%s,%s,%s)", (vendor_id, name, address, milk_type, phone, owner))


def make_staff(owner, email="staff@test.local"):
    return q("INSERT INTO staff (owner_id, name, email, password) VALUES (%s,%s,%s,%s)",
             (owner, "Driver", email, generate_password_hash(PASSWORD)))


def login_owner(client, uid, email="owner@test.local"):
    with client.session_transaction() as s:
        s.update(loggedin=True, role="owner", id=uid, email=email, dairy_name="Test Dairy")


def login_staff(client, owner, staff_id):
    with client.session_transaction() as s:
        s.update(loggedin=True, role="staff", id=owner, owner_id=owner, staff_id=staff_id,
                 email="staff@test.local")


REFERENCE_SETTINGS = dict(fat_min="3.2", fat_max="5.0", fat_step="0.1", snf_min="8.3",
                          snf_max="8.7", snf_step="0.1", std_fat="3.5", std_snf="8.5",
                          std_rate="27.00", fat_rate_step="0.30", snf_rate_step="0.30")


def reference_chart_payload(milk_type="cow", effective_from="2026-01-01", name="Reference"):
    from fat_snf.logic import generate_matrix
    fats, snfs, matrix = generate_matrix(REFERENCE_SETTINGS)
    return {
        "chart_name": name, "milk_type": milk_type, "effective_from": effective_from,
        "settings": REFERENCE_SETTINGS,
        "fat_values": [str(f) for f in fats], "snf_values": [str(s) for s in snfs],
        "matrix": [{"fat": str(f), "snf": str(s), "rate": str(r)} for (f, s), r in matrix.items()],
    }


def enable_fat_snf(owner):
    q("INSERT INTO fat_snf_calculation_settings (user_id, calculation_mode) VALUES (%s,'fat_snf') "
      "ON DUPLICATE KEY UPDATE calculation_mode='fat_snf'", (owner,))


def now_date_slot():
    from fat_snf import logic as L
    now = L.now_ist()
    return L.today_ist(now).isoformat(), L.auto_slot(now)
