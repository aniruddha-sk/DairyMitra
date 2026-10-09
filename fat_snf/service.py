"""
FAT/SNF data access and business operations.

Every query is scoped by the authenticated tenant (users.id == session['id'])
which callers pass in explicitly; no tenant or owner id is ever read from
request input.
"""
from collections import defaultdict
from decimal import Decimal

import MySQLdb
from flask import current_app, g

from core import audit, db
from fat_snf import logic as L
from fat_snf.logic import ValidationError

DEFAULT_THRESHOLDS = {
    "warn_low_litres": Decimal("0.50"),
    "warn_high_litres": Decimal("100.00"),
    "warn_deviation_pct": 60,
}


class RateNotFound(Exception):
    def __init__(self, code, message, **info):
        super().__init__(message)
        self.code = code
        self.message = message
        self.info = info


class Conflict(Exception):
    def __init__(self, code, message, **info):
        super().__init__(message)
        self.code = code
        self.message = message
        self.info = info


class NotFound(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class Forbidden(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


# ======================================================================
# Settings / calculation mode
# ======================================================================
def get_settings(user_id, cur=None):
    own = cur is None
    cur = cur or db.cursor()
    cur.execute(
        "SELECT calculation_mode, warn_low_litres, warn_high_litres, "
        "warn_deviation_pct, updated_at FROM fat_snf_calculation_settings "
        "WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    if own:
        cur.close()
    result = {"calculation_mode": "general", **DEFAULT_THRESHOLDS, "updated_at": None}
    if row:
        result.update(row)
    return result


def get_mode(user_id):
    """Cached per request. 'general' (OFF) or 'fat_snf' (ON)."""
    cache = g.setdefault("_fsn_mode", {})
    if user_id not in cache:
        try:
            cache[user_id] = get_settings(user_id)["calculation_mode"]
        except MySQLdb.Error:
            current_app.logger.exception("could not read calculation mode")
            cache[user_id] = "general"
    return cache[user_id]


def is_fat_snf(user_id):
    return bool(user_id) and get_mode(user_id) == "fat_snf"


def set_mode(user_id, mode):
    if mode not in ("general", "fat_snf"):
        raise ValidationError("Calculation mode must be 'general' or 'fat_snf'.", "mode")
    with db.transaction() as cur:
        old = get_settings(user_id, cur)["calculation_mode"]
        cur.execute(
            "INSERT INTO fat_snf_calculation_settings (user_id, calculation_mode, changed_by_role) "
            "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE "
            "calculation_mode = VALUES(calculation_mode), "
            "changed_by_role = VALUES(changed_by_role), updated_at = CURRENT_TIMESTAMP",
            (user_id, mode, audit.actor()[0]))
        if old != mode:
            audit.record("calculation_mode_change", "settings", user_id,
                         old={"calculation_mode": old}, new={"calculation_mode": mode},
                         user_id=user_id, cur=cur)
    g.setdefault("_fsn_mode", {})[user_id] = mode
    return old, mode


def set_thresholds(user_id, data):
    low = L.to_decimal(data.get("warn_low_litres"), "warn_low_litres", "किमान लिटर चुकीचे आहे.")
    high = L.to_decimal(data.get("warn_high_litres"), "warn_high_litres", "कमाल लिटर चुकीचे आहे.")
    try:
        pct = int(str(data.get("warn_deviation_pct")).strip())
    except (TypeError, ValueError):
        raise ValidationError("फरक टक्केवारी चुकीची आहे.", "warn_deviation_pct")
    if low < 0 or high < 0 or (high and low > high) or not 0 <= pct <= 1000:
        raise ValidationError("इशारा मर्यादा तपासा (किमान ≤ कमाल).")
    with db.transaction() as cur:
        old = get_settings(user_id, cur)
        cur.execute(
            "INSERT INTO fat_snf_calculation_settings "
            "(user_id, calculation_mode, warn_low_litres, warn_high_litres, warn_deviation_pct) "
            "VALUES (%s, 'general', %s, %s, %s) ON DUPLICATE KEY UPDATE "
            "warn_low_litres = VALUES(warn_low_litres), warn_high_litres = VALUES(warn_high_litres), "
            "warn_deviation_pct = VALUES(warn_deviation_pct)",
            (user_id, L.money(low), L.money(high), pct))
        audit.record("warning_thresholds_change", "settings", user_id,
                     old={k: old[k] for k in DEFAULT_THRESHOLDS},
                     new={"warn_low_litres": low, "warn_high_litres": high,
                          "warn_deviation_pct": pct},
                     user_id=user_id, cur=cur)


# ======================================================================
# Vendors
# ======================================================================
def list_vendors(user_id):
    cur = db.cursor()
    cur.execute(
        "SELECT vendor_id, name, milk_type, address, phone FROM vendors "
        "WHERE user_id = %s ORDER BY vendor_id", (user_id,))
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def get_vendor(user_id, vendor_id, cur=None):
    own = cur is None
    cur = cur or db.cursor()
    cur.execute(
        "SELECT vendor_id, name, name_en, milk_type, address, phone FROM vendors "
        "WHERE user_id = %s AND vendor_id = %s", (user_id, vendor_id))
    row = cur.fetchone()
    if own:
        cur.close()
    return row


def vendor_types(vendor):
    mt = (vendor or {}).get("milk_type") or "cow"
    return ["cow", "buffalo"] if mt == "both" else [mt]


def parse_vendor_id(value):
    try:
        vid = int(str(value).strip())
        if vid <= 0:
            raise ValueError
        return vid
    except (TypeError, ValueError):
        raise ValidationError("कृपया ग्राहक निवडा.", "vendor_id")


# ======================================================================
# Rate charts
# ======================================================================
SETTING_KEYS = ("fat_min", "fat_max", "fat_step", "snf_min", "snf_max", "snf_step",
                "std_fat", "std_snf", "std_rate", "fat_rate_step", "snf_rate_step")


def list_charts(user_id):
    cur = db.cursor()
    cur.execute(
        "SELECT c.*, "
        " (SELECT COUNT(*) FROM fat_snf_chart_values v WHERE v.chart_id = c.id) AS cell_count, "
        " (SELECT COUNT(*) FROM fat_snf_milk_collection m WHERE m.chart_id = c.id) AS entry_count "
        "FROM fat_snf_charts c WHERE c.user_id = %s "
        "ORDER BY c.milk_type, c.effective_from DESC, c.id DESC", (user_id,))
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def get_chart(user_id, chart_id, cur=None, lock=False):
    own = cur is None
    cur = cur or db.cursor()
    cur.execute(
        "SELECT * FROM fat_snf_charts WHERE id = %s AND user_id = %s"
        + (" FOR UPDATE" if lock else ""), (chart_id, user_id))
    row = cur.fetchone()
    if own:
        cur.close()
    return row


def get_chart_values(chart_id, cur=None):
    own = cur is None
    cur = cur or db.cursor()
    cur.execute(
        "SELECT id, fat, snf, rate FROM fat_snf_chart_values WHERE chart_id = %s "
        "ORDER BY fat, snf", (chart_id,))
    rows = cur.fetchall()
    if own:
        cur.close()
    return list(rows)


def chart_entry_count(chart_id, cur):
    cur.execute("SELECT COUNT(*) AS n FROM fat_snf_milk_collection WHERE chart_id = %s",
                (chart_id,))
    return cur.fetchone()["n"]


def validate_chart_payload(data):
    if not isinstance(data, dict):
        raise ValidationError("A valid JSON request body is required.")
    name = str(data.get("chart_name") or "").strip()
    if not name or len(name) > 150:
        raise ValidationError("चार्टचे नाव आवश्यक आहे (जास्तीत जास्त 150 अक्षरे).", "chart_name")
    milk_type = L.parse_milk_type(data.get("milk_type"), L.CHART_MILK_TYPES)
    effective = L.parse_date(data.get("effective_from") or data.get("effective_date"),
                             "effective_from")
    fats, snfs, matrix = L.validate_matrix(data.get("fat_values"), data.get("snf_values"),
                                           data.get("matrix"))
    settings = None
    raw = data.get("settings")
    if isinstance(raw, dict) and all(raw.get(k) not in (None, "") for k in SETTING_KEYS):
        # Validate by generating; the stored matrix may contain manual edits.
        L.generate_matrix(raw)
        settings = {k: Decimal(str(raw[k])) for k in SETTING_KEYS}
    return {"chart_name": name, "milk_type": milk_type, "effective_from": effective,
            "fats": fats, "snfs": snfs, "matrix": matrix, "settings": settings}


def _active_conflict(cur, user_id, milk_type, effective_from, exclude_id=None):
    sql = ("SELECT id, chart_name FROM fat_snf_charts WHERE user_id = %s AND milk_type = %s "
           "AND effective_from = %s AND status = 'active'")
    params = [user_id, milk_type, effective_from]
    if exclude_id:
        sql += " AND id <> %s"
        params.append(exclude_id)
    cur.execute(sql + " LIMIT 1", params)
    return cur.fetchone()


def _settings_columns(settings):
    return [settings.get(k) if settings else None for k in SETTING_KEYS]


def create_chart(user_id, data, staff_id=None, revised_from=None):
    clean = validate_chart_payload(data)
    with db.transaction() as cur:
        # Serialize chart creation per tenant (row lock on the owner).
        cur.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
        version = 1
        if revised_from:
            src = get_chart(user_id, revised_from, cur)
            if not src:
                raise NotFound("मूळ चार्ट सापडला नाही.")
            version = int(src.get("version") or 1) + 1
        if _active_conflict(cur, user_id, clean["milk_type"], clean["effective_from"]):
            raise Conflict("chart_conflict",
                           "याच दुधाच्या प्रकारासाठी आणि याच तारखेपासून सक्रिय चार्ट आधीच आहे. "
                           "वेगळी तारीख निवडा किंवा जुना चार्ट निष्क्रिय करा.")
        cols = ", ".join(SETTING_KEYS)
        cur.execute(
            f"INSERT INTO fat_snf_charts (user_id, created_by_staff_id, chart_name, milk_type, "
            f"effective_from, status, version, revised_from_chart_id, activated_at, {cols}) "
            f"VALUES (%s,%s,%s,%s,%s,'active',%s,%s,CURRENT_TIMESTAMP,"
            + ",".join(["%s"] * len(SETTING_KEYS)) + ")",
            [user_id, staff_id, clean["chart_name"], clean["milk_type"], clean["effective_from"],
             version, revised_from] + _settings_columns(clean["settings"]))
        chart_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO fat_snf_chart_values (chart_id, fat, snf, rate) VALUES (%s,%s,%s,%s)",
            [(chart_id, f, s, r) for (f, s), r in sorted(clean["matrix"].items())])
        audit.record("rate_chart_create", "fat_snf_chart", chart_id, new={
            "chart_name": clean["chart_name"], "milk_type": clean["milk_type"],
            "effective_from": clean["effective_from"], "version": version,
            "revised_from": revised_from, "cells": len(clean["matrix"]),
            "settings": clean["settings"]}, user_id=user_id, cur=cur)
    return chart_id


def update_chart(user_id, chart_id, data):
    """Edit name/date/cells. Saved entries keep the rate they were saved
    with (stored per entry), so editing never rewrites history."""
    clean = validate_chart_payload(data)
    with db.transaction() as cur:
        chart = get_chart(user_id, chart_id, cur, lock=True)
        if not chart:
            raise NotFound("चार्ट सापडला नाही.")
        if clean["milk_type"] != chart["milk_type"] and chart_entry_count(chart_id, cur):
            raise Conflict("chart_in_use",
                           "या चार्टवर नोंदी आहेत, त्यामुळे दुधाचा प्रकार बदलता येणार नाही. "
                           "नवीन आवृत्ती तयार करा.")
        if chart["status"] == "active" and _active_conflict(
                cur, user_id, clean["milk_type"], clean["effective_from"], exclude_id=chart_id):
            raise Conflict("chart_conflict",
                           "याच दुधाच्या प्रकारासाठी आणि याच तारखेपासून दुसरा सक्रिय चार्ट आहे.")

        old_values = {(v["fat"], v["snf"]): v["rate"] for v in get_chart_values(chart_id, cur)}
        new_values = clean["matrix"]
        changed = {k: (old_values.get(k), r) for k, r in new_values.items()
                   if old_values.get(k) != r}
        removed = [k for k in old_values if k not in new_values]

        cols = ", ".join(f"{k} = %s" for k in SETTING_KEYS)
        cur.execute(
            f"UPDATE fat_snf_charts SET chart_name = %s, milk_type = %s, effective_from = %s, "
            f"{cols} WHERE id = %s AND user_id = %s",
            [clean["chart_name"], clean["milk_type"], clean["effective_from"]]
            + _settings_columns(clean["settings"]) + [chart_id, user_id])
        if changed:
            cur.executemany(
                "INSERT INTO fat_snf_chart_values (chart_id, fat, snf, rate) VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE rate = VALUES(rate)",
                [(chart_id, f, s, new) for (f, s), (_, new) in changed.items()])
        for f, s in removed:
            cur.execute("DELETE FROM fat_snf_chart_values WHERE chart_id = %s AND fat = %s "
                        "AND snf = %s", (chart_id, f, s))

        audit.record("rate_chart_update", "fat_snf_chart", chart_id,
                     old={"chart_name": chart["chart_name"], "milk_type": chart["milk_type"],
                          "effective_from": chart["effective_from"],
                          "cells": {f"{f}/{s}": o for (f, s), (o, _) in changed.items()
                                    if o is not None},
                          "removed": [f"{f}/{s}" for f, s in removed]},
                     new={"chart_name": clean["chart_name"], "milk_type": clean["milk_type"],
                          "effective_from": clean["effective_from"],
                          "cells": {f"{f}/{s}": n for (f, s), (_, n) in changed.items()}},
                     user_id=user_id, cur=cur)
        return {"changed_cells": len(changed), "removed_cells": len(removed),
                "entries_using_chart": chart_entry_count(chart_id, cur)}


def set_chart_status(user_id, chart_id, active):
    with db.transaction() as cur:
        chart = get_chart(user_id, chart_id, cur, lock=True)
        if not chart:
            raise NotFound("चार्ट सापडला नाही.")
        new_status = "active" if active else "inactive"
        if chart["status"] == new_status:
            return new_status
        if active and _active_conflict(cur, user_id, chart["milk_type"],
                                       chart["effective_from"], exclude_id=chart_id):
            raise Conflict("chart_conflict",
                           "याच प्रकारासाठी आणि याच तारखेपासून दुसरा चार्ट सक्रिय आहे. "
                           "तो आधी निष्क्रिय करा.")
        stamp = "activated_at" if active else "deactivated_at"
        cur.execute(f"UPDATE fat_snf_charts SET status = %s, {stamp} = CURRENT_TIMESTAMP "
                    f"WHERE id = %s AND user_id = %s", (new_status, chart_id, user_id))
        audit.record("rate_chart_" + ("activate" if active else "deactivate"),
                     "fat_snf_chart", chart_id, old={"status": chart["status"]},
                     new={"status": new_status}, user_id=user_id, cur=cur)
        return new_status


def find_rate(cur, user_id, milk_type, fat, snf, on_date):
    """Exact lookup. Chart choice: active, effective_from <= date; a chart
    for the specific milk type beats a 'both' chart; newest date wins."""
    cur.execute(
        "SELECT id, chart_name, milk_type, effective_from FROM fat_snf_charts "
        "WHERE user_id = %s AND status = 'active' AND effective_from <= %s "
        "AND milk_type IN (%s, 'both') "
        "ORDER BY (milk_type = %s) DESC, effective_from DESC, id DESC LIMIT 1",
        (user_id, on_date, milk_type, milk_type))
    chart = cur.fetchone()
    if not chart:
        raise RateNotFound(
            "no_chart",
            f"{on_date.strftime('%d/%m/%Y')} साठी {L.TYPE_LABELS_MR[milk_type]} दुधाचा "
            f"सक्रिय दर चार्ट उपलब्ध नाही.")
    cur.execute(
        "SELECT id, rate FROM fat_snf_chart_values WHERE chart_id = %s AND fat = %s AND snf = %s",
        (chart["id"], fat, snf))
    cell = cur.fetchone()
    if not cell:
        cur.execute("SELECT MIN(fat) fmin, MAX(fat) fmax, MIN(snf) smin, MAX(snf) smax "
                    "FROM fat_snf_chart_values WHERE chart_id = %s", (chart["id"],))
        r = cur.fetchone() or {}
        raise RateNotFound(
            "no_cell",
            f"या FAT/SNF संयोजनासाठी दर उपलब्ध नाही (FAT {fat}, SNF {snf}). "
            f"चार्ट '{chart['chart_name']}' मध्ये FAT {r.get('fmin')}–{r.get('fmax')}, "
            f"SNF {r.get('smin')}–{r.get('smax')} आहे.",
            chart_id=chart["id"], chart_name=chart["chart_name"])
    return {"chart_id": chart["id"], "chart_name": chart["chart_name"],
            "chart_value_id": cell["id"], "rate": L.money(cell["rate"])}


# ======================================================================
# Collection entries
# ======================================================================
ENTRY_COLUMNS = (
    "m.id, m.vendor_id, v.name AS vendor_name, v.milk_type AS vendor_milk_type, "
    "m.collection_date, m.slot, m.milk_type, m.fat, m.snf, m.quantity, m.rate, m.amount, "
    "m.status, m.chart_id, m.staff_id, m.created_at, m.updated_at, m.finalized_at, "
    "m.client_request_id")


def list_entries(user_id, on_date, slot=None):
    sql = (f"SELECT {ENTRY_COLUMNS} FROM fat_snf_milk_collection m "
           "JOIN vendors v ON v.vendor_id = m.vendor_id AND v.user_id = m.user_id "
           "WHERE m.user_id = %s AND m.collection_date = %s")
    params = [user_id, on_date]
    if slot:
        sql += " AND m.slot = %s"
        params.append(slot)
    cur = db.cursor()
    cur.execute(sql + " ORDER BY m.slot, m.created_at, m.id", params)
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def get_entry(user_id, entry_id, cur=None, lock=False):
    own = cur is None
    cur = cur or db.cursor()
    cur.execute(
        f"SELECT {ENTRY_COLUMNS} FROM fat_snf_milk_collection m "
        "JOIN vendors v ON v.vendor_id = m.vendor_id AND v.user_id = m.user_id "
        "WHERE m.id = %s AND m.user_id = %s" + (" FOR UPDATE" if lock else ""),
        (entry_id, user_id))
    row = cur.fetchone()
    if own:
        cur.close()
    return row


def get_entry_by_request(user_id, request_id, cur=None):
    own = cur is None
    cur = cur or db.cursor()
    cur.execute(
        f"SELECT {ENTRY_COLUMNS} FROM fat_snf_milk_collection m "
        "JOIN vendors v ON v.vendor_id = m.vendor_id AND v.user_id = m.user_id "
        "WHERE m.user_id = %s AND m.client_request_id = %s", (user_id, request_id))
    row = cur.fetchone()
    if own:
        cur.close()
    return row


def _clean_request_id(value):
    rid = str(value or "").strip()
    if not rid:
        return None
    if len(rid) > 64 or not all(c.isalnum() or c == "-" for c in rid):
        raise ValidationError("Invalid request id.", "request_id")
    return rid


def _check_type(vendor, milk_type):
    allowed = vendor_types(vendor)
    if milk_type not in allowed:
        raise ValidationError(
            f"हा ग्राहक फक्त {L.TYPE_LABELS_MR[allowed[0]]} दूध देतो.", "milk_type")


def recent_litres(cur, user_id, vendor_id, milk_type, limit=20):
    cur.execute(
        "SELECT quantity FROM fat_snf_milk_collection WHERE user_id = %s AND vendor_id = %s "
        "AND milk_type = %s ORDER BY collection_date DESC, id DESC LIMIT %s",
        (user_id, vendor_id, milk_type, limit))
    return [r["quantity"] for r in cur.fetchall()]


def preview_rate(user_id, vendor_id, milk_type, fat, snf, on_date):
    """Rate + amount preview (read only)."""
    cur = db.cursor()
    try:
        vendor = get_vendor(user_id, vendor_id, cur)
        if not vendor:
            raise NotFound("ग्राहक सापडला नाही.")
        _check_type(vendor, milk_type)
        return find_rate(cur, user_id, milk_type, fat, snf, on_date)
    finally:
        cur.close()


class _DuplicateKey(Exception):
    pass


def save_entry(user_id, data, staff_id=None, is_owner=False, now=None):
    """
    Validate, look up the rate, and insert one entry.

    Idempotent: a retry carrying the same request_id returns the entry that
    was already saved instead of creating a second one.
    Returns {"entry": row, "replayed": bool, "warnings": [...]}.
    Raises ValidationError / RateNotFound / Conflict / NotFound.
    """
    request_id = _clean_request_id(data.get("request_id"))
    if request_id:
        existing = get_entry_by_request(user_id, request_id)
        if existing:
            return {"entry": existing, "replayed": True, "warnings": []}

    vendor_id = parse_vendor_id(data.get("vendor_id"))
    milk_type = L.parse_milk_type(data.get("milk_type"))
    fat = L.parse_fat(data.get("fat"))
    snf = L.parse_snf(data.get("snf"))
    litres = L.parse_litres(data.get("litres", data.get("quantity")))
    on_date, slot = resolve_date_slot(data, now=now, allow_backdate=is_owner)

    try:
        return _insert_entry(user_id, data, staff_id, request_id, vendor_id, milk_type,
                             fat, snf, litres, on_date, slot)
    except _DuplicateKey:
        # A concurrent retry of the same request won the race: replay it.
        if request_id:
            existing = get_entry_by_request(user_id, request_id)
            if existing:
                return {"entry": existing, "replayed": True, "warnings": []}
        raise Conflict("duplicate", "या ग्राहकाची या तारखेची आणि स्लॉटची नोंद आधीच आहे.")


def _insert_entry(user_id, data, staff_id, request_id, vendor_id, milk_type, fat, snf,
                  litres, on_date, slot):
    with db.transaction() as cur:
        vendor = get_vendor(user_id, vendor_id, cur)
        if not vendor:
            raise NotFound("ग्राहक सापडला नाही.")
        _check_type(vendor, milk_type)

        cur.execute(
            "SELECT id FROM fat_snf_milk_collection WHERE user_id = %s AND vendor_id = %s "
            "AND collection_date = %s AND slot = %s AND milk_type = %s",
            (user_id, vendor_id, on_date, slot, milk_type))
        dup = cur.fetchone()
        if dup:
            raise Conflict("duplicate",
                           "या ग्राहकाची या तारखेची आणि स्लॉटची नोंद आधीच आहे. "
                           "बदल करायचा असल्यास 'संपादन' वापरा.", entry_id=dup["id"])

        rate_info = find_rate(cur, user_id, milk_type, fat, snf, on_date)
        amount = L.entry_amount(litres, rate_info["rate"])

        warnings = L.entry_warnings(
            litres, recent_litres(cur, user_id, vendor_id, milk_type), get_settings(user_id, cur))
        if warnings and not data.get("confirm_warnings"):
            raise Conflict("needs_confirmation", "कृपया इशारे तपासा आणि खात्री करा.",
                           warnings=warnings, rate=str(rate_info["rate"]), amount=str(amount))

        try:
            cur.execute(
                "INSERT INTO fat_snf_milk_collection (user_id, vendor_id, staff_id, chart_id, "
                "chart_value_id, collection_date, slot, milk_type, fat, snf, quantity, rate, "
                "amount, status, client_request_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'saved',%s)",
                (user_id, vendor_id, staff_id, rate_info["chart_id"],
                 rate_info["chart_value_id"], on_date, slot, milk_type, fat, snf, litres,
                 rate_info["rate"], amount, request_id))
        except MySQLdb.IntegrityError as exc:
            if exc.args and exc.args[0] == 1062:
                raise _DuplicateKey()
            raise
        entry_id = cur.lastrowid
        audit.record("fsn_entry_create", "fat_snf_entry", entry_id, new={
            "vendor_id": vendor_id, "date": on_date, "slot": slot, "milk_type": milk_type,
            "fat": fat, "snf": snf, "litres": litres, "rate": rate_info["rate"],
            "amount": amount, "warnings": warnings or None}, user_id=user_id, cur=cur)
        entry = get_entry(user_id, entry_id, cur)
    return {"entry": entry, "replayed": False, "warnings": warnings}


def resolve_date_slot(data, now=None, allow_backdate=False):
    """
    Server-side date/slot rule:
      * Normal entry: date must be today (IST) and slot the automatic slot.
      * Backdated/correction entry (owner only, explicit `backdated: true`):
        any past or current date and either slot; never a future date.
    """
    now = now or L.now_ist()
    today = L.today_ist(now)
    auto = L.auto_slot(now)
    on_date = L.parse_date(data.get("date") or today.isoformat())
    slot = L.parse_slot(data.get("slot") or auto)
    if on_date > today:
        raise ValidationError("भविष्यातील तारखेची नोंद करता येणार नाही.", "date")
    if data.get("backdated"):
        if not allow_backdate:
            raise Forbidden("जुनी/दुरुस्ती नोंद फक्त मालक करू शकतो.")
        return on_date, slot
    if on_date != today:
        raise Conflict("date_changed",
                       f"तारीख बदलली आहे. आजची तारीख {today.strftime('%d/%m/%Y')} आहे. "
                       "पान रिफ्रेश करा.", date=today.isoformat(), slot=auto)
    if slot != auto:
        raise Conflict("slot_changed",
                       f"वेळ बदलली आहे: आता {L.SLOT_LABELS_MR[auto]} आहे. पान रिफ्रेश करा.",
                       date=today.isoformat(), slot=auto)
    return on_date, slot


def update_entry(user_id, entry_id, data, is_owner):
    """
    Correction policy: changing FAT/SNF/litres (or date/slot) re-reads the
    rate from the chart that applies on the entry's (new) date. Finalized
    entries can only be corrected by the owner with a reason. Old and new
    values are written to the audit log.
    """
    fat = L.parse_fat(data.get("fat"))
    snf = L.parse_snf(data.get("snf"))
    litres = L.parse_litres(data.get("litres", data.get("quantity")))
    reason = str(data.get("reason") or "").strip()[:255]

    with db.transaction() as cur:
        entry = get_entry(user_id, entry_id, cur, lock=True)
        if not entry:
            raise NotFound("नोंद सापडली नाही.")
        if entry["status"] == "final":
            if not is_owner:
                raise Forbidden("अंतिम झालेली नोंद फक्त मालक दुरुस्त करू शकतो.")
            if not reason:
                raise ValidationError("अंतिम नोंद दुरुस्तीसाठी कारण लिहा.", "reason")

        new_date, new_slot = entry["collection_date"], entry["slot"]
        if data.get("date") or data.get("slot"):
            d = L.parse_date(data.get("date") or entry["collection_date"])
            s = L.parse_slot(data.get("slot") or entry["slot"])
            if (d, s) != (entry["collection_date"], entry["slot"]):
                if not is_owner:
                    raise Forbidden("तारीख/वेळ फक्त मालक बदलू शकतो.")
                if d > L.today_ist():
                    raise ValidationError("भविष्यातील तारीख निवडता येणार नाही.", "date")
                new_date, new_slot = d, s

        expected_version = str(data.get("updated_at") or "").replace(" ", "T")
        current_version = entry["updated_at"].isoformat() if entry["updated_at"] else ""
        if expected_version and current_version != expected_version:
            raise Conflict("stale",
                           "ही नोंद दुसऱ्या वापरकर्त्याने आत्ताच बदलली आहे. "
                           "पान रिफ्रेश करून पुन्हा तपासा.")

        rate_info = find_rate(cur, user_id, entry["milk_type"], fat, snf, new_date)
        amount = L.entry_amount(litres, rate_info["rate"])
        try:
            cur.execute(
                "UPDATE fat_snf_milk_collection SET collection_date = %s, slot = %s, "
                "chart_id = %s, chart_value_id = %s, fat = %s, snf = %s, quantity = %s, "
                "rate = %s, amount = %s WHERE id = %s AND user_id = %s",
                (new_date, new_slot, rate_info["chart_id"], rate_info["chart_value_id"],
                 fat, snf, litres, rate_info["rate"], amount, entry_id, user_id))
        except MySQLdb.IntegrityError as exc:
            if exc.args and exc.args[0] == 1062:
                raise Conflict("duplicate",
                               "त्या तारखेला आणि वेळेला या ग्राहकाची नोंद आधीच आहे.")
            raise
        audit.record("fsn_entry_update", "fat_snf_entry", entry_id,
                     old={k: entry[k] for k in ("collection_date", "slot", "fat", "snf",
                                                 "quantity", "rate", "amount", "status")},
                     new={"collection_date": new_date, "slot": new_slot, "fat": fat,
                          "snf": snf, "quantity": litres, "rate": rate_info["rate"],
                          "amount": amount, "reason": reason or None},
                     user_id=user_id, cur=cur)
        updated = get_entry(user_id, entry_id, cur)
    return {"entry": updated, "old_rate": entry["rate"], "old_amount": entry["amount"]}


def delete_entry(user_id, entry_id, reason):
    reason = str(reason or "").strip()[:255]
    if not reason:
        raise ValidationError("नोंद काढण्याचे कारण लिहा.", "reason")
    with db.transaction() as cur:
        entry = get_entry(user_id, entry_id, cur, lock=True)
        if not entry:
            raise NotFound("नोंद सापडली नाही.")
        cur.execute("DELETE FROM fat_snf_milk_collection WHERE id = %s AND user_id = %s",
                    (entry_id, user_id))
        audit.record("fsn_entry_delete", "fat_snf_entry", entry_id,
                     old={k: entry[k] for k in ("vendor_id", "collection_date", "slot",
                                                 "milk_type", "fat", "snf", "quantity",
                                                 "rate", "amount", "status")},
                     new={"reason": reason}, user_id=user_id, cur=cur)


def finalize(user_id, on_date, slot):
    """Mark every saved entry of a date+slot as final, atomically.
    Re-running is harmless (already-final rows are untouched)."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT id FROM fat_snf_milk_collection WHERE user_id = %s AND collection_date = %s "
            "AND slot = %s AND status = 'saved' FOR UPDATE", (user_id, on_date, slot))
        ids = [r["id"] for r in cur.fetchall()]
        if ids:
            cur.execute(
                "UPDATE fat_snf_milk_collection SET status = 'final', "
                "finalized_at = CURRENT_TIMESTAMP WHERE user_id = %s AND collection_date = %s "
                "AND slot = %s AND status = 'saved'", (user_id, on_date, slot))
            cur.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(quantity),0) l, COALESCE(SUM(amount),0) a "
                "FROM fat_snf_milk_collection WHERE user_id = %s AND collection_date = %s "
                "AND slot = %s", (user_id, on_date, slot))
            t = cur.fetchone()
            audit.record("fsn_finalize", "fat_snf_batch", f"{on_date}:{slot}",
                         new={"finalized": len(ids), "entries": t["n"], "litres": t["l"],
                              "amount": t["a"]}, user_id=user_id, cur=cur)
    return len(ids)


# ======================================================================
# Period statements (calculation, receipts, reports)
# ======================================================================
def _empty_slot():
    return {"litres": L.ZERO, "amount": L.ZERO, "pairs_fat": [], "pairs_snf": [], "count": 0}


def _finish_slot(slot):
    slot["avg_fat"] = L.weighted_average(slot.pop("pairs_fat"))
    slot["avg_snf"] = L.weighted_average(slot.pop("pairs_snf"))
    slot["avg_rate"] = (L.money(slot["amount"] / slot["litres"])
                        if slot["litres"] > 0 else None)
    return slot


def fsn_entries_in_range(user_id, start, end, vendor_ids=None):
    sql = ("SELECT id, vendor_id, collection_date, slot, milk_type, fat, snf, quantity, rate, "
           "amount, status FROM fat_snf_milk_collection WHERE user_id = %s "
           "AND collection_date BETWEEN %s AND %s")
    params = [user_id, start, end]
    if vendor_ids:
        sql += " AND vendor_id IN (" + ",".join(["%s"] * len(vendor_ids)) + ")"
        params += list(vendor_ids)
    cur = db.cursor()
    cur.execute(sql + " ORDER BY vendor_id, collection_date, slot", params)
    rows = cur.fetchall()
    cur.close()
    return rows


def fsn_amounts_by_vendor(user_id, start, end):
    """{vendor_id: {"cow": (litres, amount), "buffalo": (...)}} — used by the
    existing litre-rate pages so FAT/SNF milk is never left unpaid."""
    cur = db.cursor()
    cur.execute(
        "SELECT vendor_id, milk_type, SUM(quantity) l, SUM(amount) a "
        "FROM fat_snf_milk_collection WHERE user_id = %s AND collection_date BETWEEN %s AND %s "
        "GROUP BY vendor_id, milk_type", (user_id, start, end))
    out = defaultdict(dict)
    for r in cur.fetchall():
        out[int(r["vendor_id"])][r["milk_type"]] = (Decimal(r["l"] or 0), Decimal(r["a"] or 0))
    cur.close()
    return out


def general_milk_by_vendor(user_id, start, end):
    """Legacy litre-rate milk (milk_collection) priced exactly like the
    existing payment page: rate per (vendor, type, date) via get_vendor_rate."""
    get_rate = current_app.extensions["dm_hooks"]["general_rate"]
    cur = db.cursor()
    cur.execute(
        "SELECT vendor_id, date, milk_type, SUM(quantity) AS qty FROM milk_collection "
        "WHERE user_id = %s AND date BETWEEN %s AND %s GROUP BY vendor_id, date, milk_type",
        (user_id, start, end))
    rows = cur.fetchall()
    cur.close()
    acc = defaultdict(lambda: {"cow": [0.0, 0.0], "buffalo": [0.0, 0.0]})
    for r in rows:
        qty = float(r["qty"] or 0)
        rate = get_rate(None, r["vendor_id"], r["milk_type"], r["date"], user_id=user_id)
        bucket = acc[int(r["vendor_id"])][r["milk_type"]]
        bucket[0] += qty
        bucket[1] += qty * rate
    return {vid: {t: (L.money(v[0]), L.money(v[1])) for t, v in d.items()}
            for vid, d in acc.items()}


def food_by_vendor(user_id, start, end):
    cur = db.cursor()
    cur.execute(
        "SELECT fs.vendor_id, fs.date, fs.sack_qty, r.name, "
        "COALESCE(fs.sack_rate, r.rate, 0) AS rate, COALESCE(fs.total_cost, 0) AS total "
        "FROM food_sack fs JOIN food_sack_rates r ON r.id = fs.sack_rate_id "
        "WHERE fs.user_id = %s AND fs.date BETWEEN %s AND %s ORDER BY fs.vendor_id, fs.date",
        (user_id, start, end))
    out = defaultdict(lambda: {"items": [], "total": L.ZERO})
    for r in cur.fetchall():
        total = L.money(r["total"])
        d = out[int(r["vendor_id"])]
        d["items"].append({"date": r["date"], "name": r["name"], "qty": int(r["sack_qty"] or 0),
                           "rate": L.money(r["rate"]), "total": total})
        d["total"] += total
    cur.close()
    return out


def advance_by_vendor(user_id, start, end):
    """Existing ledger rule: the period deduction is subtracted; the
    remaining balance (all advances - all deductions up to `end`) is shown
    and carried forward, never deducted again."""
    cur = db.cursor()
    cur.execute(
        "SELECT vendor_id, "
        " COALESCE(SUM(CASE WHEN transaction_type='deduction' AND transaction_date >= %s "
        "   THEN amount ELSE 0 END),0) AS period_deduction, "
        " COALESCE(SUM(CASE WHEN transaction_type='advance' THEN amount ELSE 0 END),0) AS adv, "
        " COALESCE(SUM(CASE WHEN transaction_type='deduction' THEN amount ELSE 0 END),0) AS ded "
        "FROM advance_transactions WHERE user_id = %s AND transaction_date <= %s "
        "GROUP BY vendor_id", (start, user_id, end))
    out = {}
    for r in cur.fetchall():
        out[int(r["vendor_id"])] = {
            "period_deduction": L.money(r["period_deduction"]),
            "remaining": max(L.money(Decimal(r["adv"]) - Decimal(r["ded"])), L.ZERO),
        }
    cur.close()
    return out


def advance_transactions_for(user_id, vendor_id, start, end):
    cur = db.cursor()
    cur.execute(
        "SELECT transaction_type, amount, transaction_date FROM advance_transactions "
        "WHERE user_id = %s AND vendor_id = %s AND transaction_date BETWEEN %s AND %s "
        "ORDER BY transaction_date, id", (user_id, vendor_id, start, end))
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def build_statements(user_id, start, end, vendors):
    """
    Per-vendor statement for [start, end].

    milk_total = FAT/SNF amount + legacy litre-rate amount (if any)
    final      = milk_total - food sack - advance deducted in the period
    remaining  = advance balance carried forward (not deducted again)
    """
    vendor_ids = [int(v["vendor_id"]) for v in vendors]
    entries = fsn_entries_in_range(user_id, start, end, vendor_ids) if vendor_ids else []
    general = general_milk_by_vendor(user_id, start, end)
    food = food_by_vendor(user_id, start, end)
    adv = advance_by_vendor(user_id, start, end)

    by_vendor = defaultdict(list)
    for e in entries:
        by_vendor[int(e["vendor_id"])].append(e)

    statements = []
    for v in vendors:
        vid = int(v["vendor_id"])
        types = {}
        index = {}
        for e in by_vendor.get(vid, []):
            t = types.setdefault(e["milk_type"], {
                "morning": _empty_slot(), "evening": _empty_slot()})
            s = t[e["slot"]]
            s["litres"] += e["quantity"]
            s["amount"] += e["amount"]
            s["pairs_fat"].append((e["fat"], e["quantity"]))
            s["pairs_snf"].append((e["snf"], e["quantity"]))
            s["count"] += 1
            index[(e["milk_type"], e["collection_date"], e["slot"])] = e

        shown_types = [t for t in ("cow", "buffalo") if t in vendor_types(v) or t in types]
        sections = []
        fsn_total = L.ZERO
        for t in shown_types:
            slots = types.get(t, {"morning": _empty_slot(), "evening": _empty_slot()})
            all_pairs_fat = slots["morning"]["pairs_fat"] + slots["evening"]["pairs_fat"]
            all_pairs_snf = slots["morning"]["pairs_snf"] + slots["evening"]["pairs_snf"]
            rows = [{"date": d,
                     "morning": index.get((t, d, "morning")),
                     "evening": index.get((t, d, "evening"))}
                    for d in L.date_range(start, end)]
            total_l = slots["morning"]["litres"] + slots["evening"]["litres"]
            total_a = slots["morning"]["amount"] + slots["evening"]["amount"]
            fsn_total += total_a
            sections.append({
                "type": t, "label": L.TYPE_LABELS_MR[t], "rows": rows,
                "morning": _finish_slot(slots["morning"]),
                "evening": _finish_slot(slots["evening"]),
                "litres": total_l, "amount": total_a,
                "avg_fat": L.weighted_average(all_pairs_fat),
                "avg_snf": L.weighted_average(all_pairs_snf),
            })

        gen = general.get(vid, {})
        general_total = sum((a for _, a in gen.values()), L.ZERO)
        f = food.get(vid, {"items": [], "total": L.ZERO})
        a = adv.get(vid, {"period_deduction": L.ZERO, "remaining": L.ZERO})
        milk_total = fsn_total + general_total
        statements.append({
            "vendor": v,
            "sections": sections,
            "fsn_total": fsn_total,
            "general": {t: gen.get(t, (L.ZERO, L.ZERO)) for t in ("cow", "buffalo")},
            "general_total": general_total,
            "milk_total": milk_total,
            "food": f,
            "period_deduction": a["period_deduction"],
            "remaining_advance": a["remaining"],
            "final": L.money(milk_total - f["total"] - a["period_deduction"]),
            "has_activity": bool(by_vendor.get(vid) or general_total or f["items"]
                                 or a["period_deduction"]),
        })
    return statements


def calculation_rows(user_id, start, end, vendors):
    """Flat rows for the calculation page: one row per vendor x milk type."""
    statements = build_statements(user_id, start, end, vendors)
    totals = {"litres": L.ZERO, "fsn": L.ZERO, "general": L.ZERO, "food": L.ZERO,
              "deduction": L.ZERO, "final": L.ZERO, "cow_l": L.ZERO, "buffalo_l": L.ZERO}
    for st in statements:
        for sec in st["sections"]:
            totals["litres"] += sec["litres"]
            totals[f"{sec['type']}_l"] += sec["litres"]
        totals["fsn"] += st["fsn_total"]
        totals["general"] += st["general_total"]
        totals["food"] += st["food"]["total"]
        totals["deduction"] += st["period_deduction"]
        totals["final"] += st["final"]
    return statements, totals


def milk_summary(user_id, start, end):
    cur = db.cursor()
    cur.execute(
        "SELECT m.vendor_id, v.name, m.collection_date, m.slot, m.milk_type, m.fat, m.snf, "
        "m.quantity, m.amount FROM fat_snf_milk_collection m "
        "JOIN vendors v ON v.vendor_id = m.vendor_id AND v.user_id = m.user_id "
        "WHERE m.user_id = %s AND m.collection_date BETWEEN %s AND %s",
        (user_id, start, end))
    rows = cur.fetchall()
    cur.close()

    def bucket():
        return {"litres": L.ZERO, "amount": L.ZERO, "fat": [], "snf": []}

    by_type_slot = defaultdict(bucket)
    by_type = defaultdict(bucket)
    by_date = defaultdict(lambda: defaultdict(bucket))
    by_vendor = {}
    grand = bucket()
    for r in rows:
        for b in (by_type_slot[(r["milk_type"], r["slot"])], by_type[r["milk_type"]],
                  by_date[r["collection_date"]][r["milk_type"]], grand):
            b["litres"] += r["quantity"]
            b["amount"] += r["amount"]
            b["fat"].append((r["fat"], r["quantity"]))
            b["snf"].append((r["snf"], r["quantity"]))
        vb = by_vendor.setdefault(int(r["vendor_id"]), {
            "vendor_id": r["vendor_id"], "name": r["name"],
            "cow": bucket(), "buffalo": bucket(), "amount": L.ZERO})
        vb[r["milk_type"]]["litres"] += r["quantity"]
        vb[r["milk_type"]]["fat"].append((r["fat"], r["quantity"]))
        vb[r["milk_type"]]["snf"].append((r["snf"], r["quantity"]))
        vb["amount"] += r["amount"]

    def fin(b):
        return {"litres": b["litres"], "amount": b["amount"],
                "avg_fat": L.weighted_average(b["fat"]),
                "avg_snf": L.weighted_average(b["snf"])}

    return {
        "type_slot": {f"{t}_{s}": fin(by_type_slot[(t, s)])
                      for t in ("cow", "buffalo") for s in ("morning", "evening")},
        "type": {t: fin(by_type[t]) for t in ("cow", "buffalo")},
        "grand": fin(grand),
        "dates": [{"date": d, "cow": fin(v["cow"]), "buffalo": fin(v["buffalo"])}
                  for d, v in sorted(by_date.items())],
        "vendors": [{"vendor_id": v["vendor_id"], "name": v["name"],
                     "cow": fin(v["cow"]), "buffalo": fin(v["buffalo"]), "amount": v["amount"]}
                    for _, v in sorted(by_vendor.items())],
    }


def fsn_slot_totals(user_id, vendor_id, start, end):
    """{(milk_type, slot): (litres, amount)} for one vendor."""
    cur = db.cursor()
    cur.execute(
        "SELECT milk_type, slot, SUM(quantity) l, SUM(amount) a FROM fat_snf_milk_collection "
        "WHERE user_id = %s AND vendor_id = %s AND collection_date BETWEEN %s AND %s "
        "GROUP BY milk_type, slot", (user_id, vendor_id, start, end))
    out = {(r["milk_type"], r["slot"]): (Decimal(r["l"] or 0), Decimal(r["a"] or 0))
           for r in cur.fetchall()}
    cur.close()
    return out
