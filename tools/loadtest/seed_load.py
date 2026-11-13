"""
Synthetic multi-tenant data for the load test (never real data).
100 dairies: odd ids = litre-rate mode, even ids = FAT/SNF mode.
60 vendors each, 30 days of morning+evening history up to yesterday (IST).
Owner login: owner<N>@load.test / Load@1234.  Customer: DM<N:06d> + vendor phone.
"""
import os
import random
import sys
from datetime import timedelta
from decimal import Decimal

import MySQLdb
import MySQLdb.cursors
from werkzeug.security import generate_password_hash

os.environ.setdefault("DAIRYMITR_ENV", "development")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
if not os.environ["MYSQL_DB"].endswith("_load"):
    sys.exit("Refusing: MYSQL_DB must end with _load (this script DROPS the database).")
from app import entry_amount, generate_matrix, today_ist  # noqa: E402
from migrations import runner  # noqa: E402

DAIRIES, VENDORS, DAYS = 100, 60, 30
DB = os.environ["MYSQL_DB"]


def connect(db=None):
    kw = dict(host=os.environ["MYSQL_HOST"], port=int(os.environ.get("MYSQL_PORT", 3306)),
              user=os.environ["MYSQL_USER"], passwd=os.environ["MYSQL_PASSWORD"],
              charset="utf8mb4", autocommit=True, cursorclass=MySQLdb.cursors.DictCursor)
    if db:
        kw["db"] = db
    return MySQLdb.connect(**kw)


root = connect()
root.cursor().execute(f"DROP DATABASE IF EXISTS `{DB}`")
root.cursor().execute(f"CREATE DATABASE `{DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
root.close()
conn = connect(DB)
cur = conn.cursor()
for chunk in open("schema.sql", encoding="utf-8").read().replace("\r\n", "\n").split(";\n"):
    body = "\n".join(l for l in chunk.splitlines() if not l.strip().startswith("--")).strip().rstrip(";")
    if body:
        cur.execute(body)
runner.run(conn, out=lambda *_: None)
conn.autocommit(False)

rnd = random.Random(1)
pw = generate_password_hash("Load@1234")
today = today_ist()
start = today - timedelta(days=DAYS)
settings = dict(fat_min="3.2", fat_max="5.0", fat_step="0.1", snf_min="8.3", snf_max="8.7",
                snf_step="0.1", std_fat="3.5", std_snf="8.5", std_rate="27.00",
                fat_rate_step="0.30", snf_rate_step="0.30")
fats, snfs, matrix = generate_matrix(settings)

for d in range(1, DAIRIES + 1):
    fsn = d % 2 == 0
    cur.execute("INSERT INTO users (id, email, password, dairy_name, is_verified, dairy_code) "
                "VALUES (%s,%s,%s,%s,1,%s)", (d, f"owner{d}@load.test", pw, f"Dairy {d}", f"DM{d:06d}"))
    cur.executemany("INSERT INTO vendors (vendor_id, name, milk_type, address, phone, user_id) "
                    "VALUES (%s,%s,'cow','Gaon',%s,%s)",
                    [(v, f"Vendor {d}-{v}", f"9{d:03d}{v:06d}", d) for v in range(1, VENDORS + 1)])
    cur.execute("INSERT INTO milk_rates (user_id, animal, rate, date_from) VALUES "
                "(%s,'cow',35,'2025-01-01'),(%s,'buffalo',50,'2025-01-01')", (d, d))
    cur.execute("INSERT INTO food_sack_rates (user_id, name, rate, date_from) VALUES (%s,'Sugras',1200,'2025-01-01')", (d,))
    sack_id = cur.lastrowid
    cur.executemany("INSERT INTO advance_transactions (vendor_id,user_id,transaction_type,amount,transaction_date) "
                    "VALUES (%s,%s,'advance',5000,%s)", [(v, d, start) for v in range(1, VENDORS + 1, 4)])
    cur.executemany("INSERT INTO food_sack (vendor_id,user_id,date,sack_qty,sack_rate_id,total_cost,sack_rate) "
                    "VALUES (%s,%s,%s,1,%s,1200,1200)",
                    [(v, d, start + timedelta(days=5), sack_id) for v in range(1, VENDORS + 1, 5)])
    if not fsn:
        rows = [(v, d, start + timedelta(days=i), s, "cow", round(rnd.uniform(3, 15), 1))
                for i in range(DAYS) for v in range(1, VENDORS + 1) for s in ("morning", "evening")]
        cur.executemany("INSERT INTO milk_collection (vendor_id,user_id,date,slot,milk_type,quantity) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", rows)
    else:
        cur.execute("INSERT INTO fat_snf_charts (user_id, chart_name, milk_type, effective_from, status, activated_at) "
                    "VALUES (%s,'Load chart','cow','2025-01-01','active',NOW())", (d,))
        cid = cur.lastrowid
        ids = {}
        for (f, s), r in matrix.items():
            cur.execute("INSERT INTO fat_snf_chart_values (chart_id, fat, snf, rate) VALUES (%s,%s,%s,%s)",
                        (cid, f, s, r))
            ids[(f, s)] = (cur.lastrowid, r)
        cells = list(ids.items())
        rows = []
        for i in range(DAYS):
            for v in range(1, VENDORS + 1):
                for slot in ("morning", "evening"):
                    (f, s), (vid_cell, rate) = rnd.choice(cells)
                    litres = Decimal(str(round(rnd.uniform(3, 15), 2)))
                    rows.append((d, v, cid, vid_cell, start + timedelta(days=i), slot, "cow", f, s,
                                 litres, rate, entry_amount(litres, rate)))
        cur.executemany("INSERT INTO fat_snf_milk_collection (user_id, vendor_id, chart_id, chart_value_id, "
                        "collection_date, slot, milk_type, fat, snf, quantity, rate, amount, status) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'final')", rows)
        cur.execute("INSERT INTO fat_snf_calculation_settings (user_id, calculation_mode) VALUES (%s,'fat_snf')", (d,))
    conn.commit()

cur.execute("SELECT (SELECT COUNT(*) FROM milk_collection) mc, (SELECT COUNT(*) FROM fat_snf_milk_collection) fc, "
            "(SELECT COUNT(*) FROM vendors) v")
print("seeded", cur.fetchone(), "history", start, "->", today - timedelta(days=1))
