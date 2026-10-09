"""
Create a local DEMO database with synthetic data (never real dairy data).

    python tools/seed_demo.py            # uses MYSQL_* from the environment/.env
                                         # and REFUSES unless MYSQL_DB ends with "_demo"

Login afterwards:  owner@demo.local / Demo@1234   (staff: staff@demo.local)
"""
import os
import pathlib
import random
import sys
from datetime import date, timedelta
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

load_dotenv(ROOT / ".env")

import MySQLdb  # noqa: E402
import MySQLdb.cursors  # noqa: E402

from fat_snf.logic import entry_amount, generate_matrix  # noqa: E402
from migrations import runner  # noqa: E402

DB = os.getenv("MYSQL_DB", "")
if not DB.endswith("_demo"):
    sys.exit("Refusing to run: MYSQL_DB must end with '_demo' (this script DROPS the database).")


def connect(db=None):
    kw = dict(host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", 3306)),
              user=os.getenv("MYSQL_USER"), passwd=os.getenv("MYSQL_PASSWORD", ""),
              charset="utf8mb4", autocommit=True, cursorclass=MySQLdb.cursors.DictCursor)
    if db:
        kw["db"] = db
    return MySQLdb.connect(**kw)


def main():
    root = connect()
    root.cursor().execute(f"DROP DATABASE IF EXISTS `{DB}`")
    root.cursor().execute(f"CREATE DATABASE `{DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    root.close()

    conn = connect(DB)
    cur = conn.cursor()
    sql = (ROOT / "schema.sql").read_text(encoding="utf-8")
    for chunk in sql.replace("\r\n", "\n").split(";\n"):
        body = "\n".join(l for l in chunk.splitlines() if not l.strip().startswith("--")).strip().rstrip(";")
        if body:
            cur.execute(body)
    runner.run(conn, out=lambda *_: None)

    pw = generate_password_hash("Demo@1234")
    cur.execute("INSERT INTO users (id, email, password, phone, dairy_name, is_verified, dairy_code) "
                "VALUES (1,'owner@demo.local',%s,'9000000000','ज्योतिर्लिंग दूध संकलन केंद्र',1,'DM000001')", (pw,))
    cur.execute("INSERT INTO staff (owner_id, name, email, password, vehicle_number) "
                "VALUES (1,'चालक','staff@demo.local',%s,'MH12AB1234')", (pw,))
    vendors = [(101, "प्रदीप जगदाळे", "cow", "PINGALI BK"), (102, "सुनील पाटील", "buffalo", "PINGALI BK"),
               (103, "राम शिंदे", "both", "WADGAON"), (104, "गीता काळे", "cow", "WADGAON")]
    for vid, name, mt, addr in vendors:
        cur.execute("INSERT INTO vendors (vendor_id, name, milk_type, address, phone, user_id) "
                    "VALUES (%s,%s,%s,%s,%s,1)", (vid, name, mt, addr, f"98000000{vid % 100:02d}"))

    def chart(name, milk_type, std_rate):
        s = dict(fat_min="3.2", fat_max="5.0", fat_step="0.1", snf_min="8.3", snf_max="8.7",
                 snf_step="0.1", std_fat="3.5", std_snf="8.5", std_rate=std_rate,
                 fat_rate_step="0.30", snf_rate_step="0.30")
        if milk_type == "buffalo":
            s.update(fat_min="5.5", fat_max="8.0", std_fat="6.5", fat_rate_step="0.50")
        fats, snfs, m = generate_matrix(s)
        cur.execute("INSERT INTO fat_snf_charts (user_id, chart_name, milk_type, effective_from, status, "
                    "fat_min, fat_max, fat_step, snf_min, snf_max, snf_step, std_fat, std_snf, std_rate, "
                    "fat_rate_step, snf_rate_step, activated_at) VALUES (1,%s,%s,'2026-01-01','active',"
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
                    (name, milk_type, s["fat_min"], s["fat_max"], s["fat_step"], s["snf_min"],
                     s["snf_max"], s["snf_step"], s["std_fat"], s["std_snf"], s["std_rate"],
                     s["fat_rate_step"], s["snf_rate_step"]))
        cid = cur.lastrowid
        ids = {}
        for (f, sn), r in m.items():
            cur.execute("INSERT INTO fat_snf_chart_values (chart_id, fat, snf, rate) VALUES (%s,%s,%s,%s)",
                        (cid, f, sn, r))
            ids[(f, sn)] = (cur.lastrowid, r)
        return cid, ids, fats, snfs

    cow = chart("गाय दर चार्ट", "cow", "27.00")
    buf = chart("म्हैस दर चार्ट", "buffalo", "48.00")
    cur.execute("INSERT INTO fat_snf_calculation_settings (user_id, calculation_mode) VALUES (1,'fat_snf')")

    rnd = random.Random(42)
    start = date(2026, 9, 21)
    for i in range(10):
        d = start + timedelta(days=i)
        for vid, _, mt, _ in vendors:
            for t in (["cow", "buffalo"] if mt == "both" else [mt]):
                cid, ids, fats, snfs = cow if t == "cow" else buf
                for slot in ("morning", "evening"):
                    f = rnd.choice(fats[2:10])
                    sn = rnd.choice(snfs)
                    litres = Decimal(rnd.choice(["12.5", "15.0", "18.0", "14.2", "16.8"]))
                    vid_cell, rate = ids[(f, sn)]
                    cur.execute(
                        "INSERT INTO fat_snf_milk_collection (user_id, vendor_id, chart_id, chart_value_id, "
                        "collection_date, slot, milk_type, fat, snf, quantity, rate, amount, status, finalized_at) "
                        "VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'final',NOW())",
                        (vid, cid, vid_cell, d, slot, t, f, sn, litres, rate, entry_amount(litres, rate)))

    cur.execute("INSERT INTO food_sack_rates (id, user_id, name, rate, date_from) VALUES (1,1,'नेचर',1900,'2026-01-01')")
    cur.execute("INSERT INTO food_sack (vendor_id, user_id, date, sack_qty, sack_rate_id, total_cost, sack_rate) "
                "VALUES (101,1,'2026-09-25',2,1,3800,1900)")
    cur.execute("INSERT INTO advance_transactions (vendor_id, user_id, transaction_type, amount, transaction_date) "
                "VALUES (102,1,'advance',5000,'2026-09-01'),(102,1,'deduction',1500,'2026-09-28')")
    conn.close()
    print(f"Demo database '{DB}' ready. Login: owner@demo.local / Demo@1234")


if __name__ == "__main__":
    main()
