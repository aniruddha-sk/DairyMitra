"""Migration safety: existing data preserved, orphans never deleted, re-runnable."""
import pytest

from conftest import HAVE_DB, ROOT, raw_conn

pytestmark = pytest.mark.skipif(not HAVE_DB, reason="needs TEST_MYSQL_HOST")

DB = "dairymitr_migration_test"


def load_original_schema(cur):
    sql = (ROOT / "schema.sql").read_text(encoding="utf-8")
    for chunk in sql.replace("\r\n", "\n").split(";\n"):
        body = "\n".join(l for l in chunk.splitlines()
                         if not l.strip().startswith("--")).strip().rstrip(";")
        if body:
            cur.execute(body)


@pytest.fixture()
def legacy_db():
    root = raw_conn(db=None)
    c = root.cursor()
    c.execute(f"DROP DATABASE IF EXISTS {DB}")
    c.execute(f"CREATE DATABASE {DB} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    root.close()
    conn = raw_conn(db=DB)
    cur = conn.cursor()
    load_original_schema(cur)
    # Pre-existing production-shaped data (before migration 001)
    cur.execute("INSERT INTO users (id, email, password, is_verified) VALUES (1,'a@x','h',1)")
    cur.execute("INSERT INTO vendors (vendor_id, name, user_id, milk_type) VALUES (5,'V',1,'cow')")
    cur.execute("INSERT INTO milk_collection (vendor_id, user_id, date, slot, milk_type, quantity) "
                "VALUES (5,1,'2026-09-01','morning','cow',12.5)")
    cur.execute("INSERT INTO fat_snf_charts (id, user_id, chart_name, milk_type, effective_from) "
                "VALUES (1,1,'old','cow','2026-01-01')")
    cur.execute("INSERT INTO fat_snf_chart_values (chart_id, fat, snf, rate) VALUES (1,3.5,8.5,27)")
    cur.execute("INSERT INTO fat_snf_calculation_settings (user_id, calculation_mode) "
                "VALUES (1,'fat_snf')")
    # an orphan chart (owner no longer exists) must NOT be deleted
    cur.execute("INSERT INTO fat_snf_charts (id, user_id, chart_name, milk_type, effective_from) "
                "VALUES (2,999,'orphan','cow','2026-01-01')")
    yield conn
    conn.close()
    root = raw_conn(db=None)
    root.cursor().execute(f"DROP DATABASE IF EXISTS {DB}")
    root.close()


def counts(cur):
    out = {}
    for t in ("users", "vendors", "milk_collection", "fat_snf_charts", "fat_snf_chart_values",
              "fat_snf_calculation_settings", "advance_transactions", "food_sack"):
        cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
        out[t] = cur.fetchone()["n"]
    return out


def test_migration_preserves_data_and_is_rerunnable(legacy_db):
    from migrations import runner
    cur = legacy_db.cursor()
    before = counts(cur)

    warnings = runner.run(legacy_db, out=lambda *_: None)
    assert any("fk_fsn_chart_user" in w and "orphan" in w for w in warnings)
    assert counts(cur) == before                                    # nothing deleted/added

    cur.execute("SELECT calculation_mode FROM fat_snf_calculation_settings WHERE user_id=1")
    assert cur.fetchone()["calculation_mode"] == "fat_snf"         # setting kept
    cur.execute("SELECT version, status FROM fat_snf_charts WHERE id=1")
    assert cur.fetchone() == {"version": 1, "status": "active"}
    cur.execute("SELECT chart_name FROM fat_snf_charts WHERE id=2")
    assert cur.fetchone()["chart_name"] == "orphan"

    # Re-running is a no-op; a re-run of the steps themselves (ledger row
    # removed, as after a crash mid-migration) is also safe.
    assert runner.pending_versions(legacy_db) == []
    cur.execute("DELETE FROM schema_migrations")
    runner.run(legacy_db, out=lambda *_: None)
    assert counts(cur) == before
