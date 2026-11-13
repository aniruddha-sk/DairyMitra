"""
Extra demo accounts used by the browser tests (synthetic data only).
Run after tools/seed_demo.py, against the SAME *_demo database:

    MYSQL_DB=dairypro_demo python tools/seed_browser_tests.py

Creates
  owner2@demo.local / Demo@1234  - a second dairy (offline isolation test)
  print@demo.local  / Demo@1234  - 14 customers, 1-10 Nov 2026 milk, one
                                   customer with 12 food-sack lines and 8
                                   advance entries (receipt print test)
"""
import os
import pathlib
import random
import sys
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DAIRYMITR_ENV", "development")

import MySQLdb  # noqa: E402
import MySQLdb.cursors  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import entry_amount, generate_matrix  # noqa: E402

DB = os.getenv("MYSQL_DB", "")
if not DB.endswith("_demo"):
    sys.exit("Refusing to run: MYSQL_DB must end with '_demo'.")

conn = MySQLdb.connect(host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", 3306)),
                       user=os.getenv("MYSQL_USER"), passwd=os.getenv("MYSQL_PASSWORD", ""), db=DB,
                       charset="utf8mb4", autocommit=True, cursorclass=MySQLdb.cursors.DictCursor)
cur = conn.cursor()
PW = generate_password_hash("Demo@1234")
COW = dict(fat_min="2.5", fat_max="5.0", fat_step="0.1", snf_min="8.0", snf_max="9.0", snf_step="0.1",
           std_fat="3.5", std_snf="8.5", std_rate="34", fat_rate_step="0.3", snf_rate_step="0.3")
BUFFALO = dict(fat_min="5.0", fat_max="11.0", fat_step="0.1", snf_min="8.0", snf_max="10.5",
               snf_step="0.1", std_fat="6.0", std_snf="9.0", std_rate="52", fat_rate_step="0.4",
               snf_rate_step="0.4")


def owner(email, name, code):
    cur.execute("SELECT id FROM users WHERE email=%s", (email,))
    if cur.fetchone():
        print("exists:", email)
        return None
    cur.execute("INSERT INTO users (email,password,dairy_name,is_verified,dairy_code) VALUES (%s,%s,%s,1,%s)",
                (email, PW, name, code))
    uid = cur.lastrowid
    cur.execute("INSERT INTO fat_snf_calculation_settings (user_id,calculation_mode) VALUES (%s,'fat_snf')", (uid,))
    return uid


def chart(uid, milk_type, settings):
    _, _, matrix = generate_matrix(settings)
    cur.execute("INSERT INTO fat_snf_charts (user_id,chart_name,milk_type,effective_from,status) "
                "VALUES (%s,%s,%s,'2026-01-01','active')", (uid, milk_type + " chart", milk_type))
    cid = cur.lastrowid
    cur.executemany("INSERT INTO fat_snf_chart_values (chart_id,fat,snf,rate) VALUES (%s,%s,%s,%s)",
                    [(cid, f, s, r) for (f, s), r in matrix.items()])
    return cid, matrix


uid = owner("owner2@demo.local", "Second Demo Dairy", "DM900002")
if uid:
    cur.execute("INSERT INTO vendors (vendor_id,name,milk_type,user_id) VALUES (101,'Second dairy farmer','cow',%s)", (uid,))
    chart(uid, "cow", COW)

uid = owner("print@demo.local", "श्री ज्योतिर्लिंग सहकारी दूध उत्पादक संस्था मर्यादित", "DM900003")
if uid:
    cur.execute("INSERT INTO milk_rates (user_id,animal,rate,date_from) VALUES (%s,'cow',34,'2026-01-01'),"
                "(%s,'buffalo',52,'2026-01-01')", (uid, uid))
    charts = {"cow": chart(uid, "cow", COW), "buffalo": chart(uid, "buffalo", BUFFALO)}
    names = ["प्रदीप जगदाळे", "सुनील पाटील", "राम शिंदे", "गीता काळे",
             "ज्ञानेश्वर बाळासाहेब देशमुख-पाटील (मोठे नाव)", "संगीता पवार", "अमोल कदम", "विठ्ठल जाधव",
             "सुरेखा माने", "बाळू गायकवाड", "नितीन भोसले", "रेखा शेलार", "महादेव सूर्यवंशी", "शांताबाई मोरे"]
    random.seed(7)
    for i, n in enumerate(names, 1):
        mt = "both" if i == 14 else ("buffalo" if i in (3, 9) else "cow")
        cur.execute("INSERT INTO vendors (vendor_id,name,milk_type,address,user_id) "
                    "VALUES (%s,%s,%s,'पिंपळगाव बसवंत, ता. निफाड',%s)", (i, n, mt, uid))
        for day in range(1, 11):
            for slot in ("morning", "evening"):
                for t in (["cow", "buffalo"] if mt == "both" else [mt]):
                    fat = Decimal(str(round(random.uniform(3.4, 4.4), 1) if t == "cow" else round(random.uniform(6.0, 7.5), 1)))
                    snf = Decimal(str(round(random.uniform(8.3, 8.8), 1) if t == "cow" else round(random.uniform(8.8, 9.4), 1)))
                    q = Decimal(str(round(random.uniform(4, 18), 1)))
                    cid, matrix = charts[t]
                    rate = matrix[(fat.quantize(Decimal("0.01")), snf.quantize(Decimal("0.01")))]
                    cur.execute("INSERT INTO fat_snf_milk_collection (user_id,vendor_id,chart_id,collection_date,slot,"
                                "milk_type,fat,snf,quantity,rate,amount) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                (uid, i, cid, f"2026-11-{day:02d}", slot, t, fat, snf, q, rate, entry_amount(q, rate)))
                    cur.execute("INSERT INTO milk_collection (vendor_id,user_id,date,slot,milk_type,quantity) "
                                "VALUES (%s,%s,%s,%s,%s,%s)", (i, uid, f"2026-11-{day:02d}", slot, t, float(q)))
    for k in range(12):
        cur.execute("INSERT INTO food_sack_rates (user_id,name,rate,date_from) VALUES (%s,%s,%s,'2026-01-01')",
                    (uid, f"पेंड प्रकार {k + 1}", 900 + k * 25))
        sid = cur.lastrowid
        qty = 1 + (k % 3)
        cur.execute("INSERT INTO food_sack (vendor_id,user_id,date,sack_qty,sack_rate_id,sack_rate,total_cost) "
                    "VALUES (5,%s,%s,%s,%s,%s,%s)", (uid, f"2026-11-{(k % 10) + 1:02d}", qty, sid, 900 + k * 25,
                                                     qty * (900 + k * 25)))
    for k in range(8):
        cur.execute("INSERT INTO advance_transactions (vendor_id,user_id,transaction_type,amount,transaction_date) "
                    "VALUES (5,%s,%s,%s,%s)", (uid, "advance" if k % 2 == 0 else "deduction", 1000 + k * 100,
                                               f"2026-11-{k + 1:02d}"))
print("browser-test accounts ready")
