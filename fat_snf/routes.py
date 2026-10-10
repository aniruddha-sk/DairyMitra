"""
FAT/SNF pages and JSON APIs (blueprint "fat_snf").

Authentication and the staff allow-list are enforced in the application's
before_request hook; owner-only operations are additionally checked here.
"""
import csv
import io
import logging
import math
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

import MySQLdb
from flask import (Blueprint, Response, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

from core import security
from fat_snf import logic as L
from fat_snf import service as S
from fat_snf.logic import ValidationError

log = logging.getLogger("dairymitr.fat_snf")

bp = Blueprint("fat_snf", __name__)

PAGE_SIZE = 50
MODE_OFF_MESSAGE = ("FAT/SNF पद्धत सध्या बंद आहे. Settings मध्ये 'Milk Calculation Mode' "
                    "चालू करा.")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def tenant():
    if not session.get("loggedin") or session.get("role") not in ("owner", "staff"):
        return None
    try:
        uid = int(session.get("id"))
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def is_owner():
    return session.get("role") == "owner"


def jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def ok(payload=None, http_status=200, **extra):
    body = {"success": True}
    body.update(jsonable(payload or {}))
    body.update(jsonable(extra))
    return jsonify(body), http_status


def api(owner_only=False):
    """Wrap a JSON endpoint: tenant check, owner check, uniform errors."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            uid = tenant()
            if not uid:
                return security.json_error("कृपया पुन्हा लॉगिन करा.", 401, code="unauthenticated")
            if owner_only and not is_owner():
                return security.json_error(security.FORBIDDEN_MESSAGE, 403, code="forbidden")
            try:
                return fn(uid, *args, **kwargs)
            except ValidationError as e:
                return security.json_error(e.message, 400, code="invalid", field=e.field)
            except S.RateNotFound as e:
                return security.json_error(e.message, 422, code=e.code, **jsonable(e.info))
            except S.Conflict as e:
                return security.json_error(e.message, 409, code=e.code, **jsonable(e.info))
            except S.NotFound as e:
                return security.json_error(e.message, 404, code="not_found")
            except S.Forbidden as e:
                return security.json_error(e.message, 403, code="forbidden")
            except MySQLdb.OperationalError:
                log.exception("database unavailable")
                return security.json_error(
                    "डेटाबेसशी संपर्क होऊ शकला नाही. कृपया थोड्या वेळाने पुन्हा प्रयत्न करा.",
                    503, code="db_unavailable")
            except Exception:
                log.exception("FAT/SNF API failure")
                return security.json_error(
                    "डेटा सेव्ह करताना अडचण आली. तुमची नोंद पुन्हा तपासा.", 500,
                    code="server_error")
        return wrapper
    return deco


def page(owner_only=False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            uid = tenant()
            if not uid:
                return redirect(url_for("login"))
            if owner_only and not is_owner():
                return security.owner_required_response()
            return fn(uid, *args, **kwargs)
        return wrapper
    return deco


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("A valid JSON request body is required.")
    return data


def staff_id():
    return session.get("staff_id") if session.get("role") == "staff" else None


def require_mode_on(uid):
    if not S.is_fat_snf(uid):
        raise S.Conflict("mode_off", MODE_OFF_MESSAGE)


def range_from_args(default_today=True):
    today = L.today_ist()
    start = request.args.get("from_date") or (today.isoformat() if default_today else None)
    end = request.args.get("to_date") or start
    return L.parse_date_range(start, end)


# ======================================================================
# Settings
# ======================================================================
@bp.route("/api/fat_snf/calculation_mode", methods=["GET"])
@api()
def api_get_mode(uid):
    s = S.get_settings(uid)
    return ok(calculation_mode=s["calculation_mode"], settings=s)


@bp.route("/api/fat_snf/calculation_mode", methods=["POST"])
@api(owner_only=True)
def api_set_mode(uid):
    data = body()
    mode = str(data.get("calculation_mode", "")).strip().lower()
    old, new = S.set_mode(uid, mode)
    msg = ("FAT/SNF पद्धत चालू केली." if new == "fat_snf"
           else "FAT/SNF पद्धत बंद केली. लिटर-दर पद्धत चालू आहे.")
    has_chart = any(c["status"] == "active" for c in S.list_charts(uid))
    return ok(message=msg, calculation_mode=new, previous_mode=old,
              needs_chart=(new == "fat_snf" and not has_chart))


@bp.route("/api/fat_snf/settings/thresholds", methods=["POST"])
@api(owner_only=True)
def api_set_thresholds(uid):
    S.set_thresholds(uid, body())
    return ok(message="इशारा मर्यादा सेव्ह झाल्या.")


# ======================================================================
# Rate charts
# ======================================================================
@bp.route("/fat_snf/rate-chart", methods=["GET"])
@page(owner_only=True)
def charts_page(uid):
    return render_template("fat_snf/rate_chart.html", charts=S.list_charts(uid),
                           mode=S.get_mode(uid), type_labels=L.TYPE_LABELS_MR)


@bp.route("/fat_snf/rate-chart/create", methods=["GET"])
@page(owner_only=True)
def chart_create_page(uid):
    source = None
    revise_id = request.args.get("revise", type=int)
    copy_id = revise_id or request.args.get("copy", type=int)
    if copy_id:
        source = S.get_chart(uid, copy_id)
        if not source:
            flash("चार्ट सापडला नाही.", "danger")
            return redirect(url_for("fat_snf.charts_page"))
        source = dict(source, values=S.get_chart_values(copy_id))
    return render_template("fat_snf/chart_editor.html", chart=None, source=jsonable(source),
                           revise_id=revise_id, today=L.today_ist().isoformat())


@bp.route("/fat_snf/rate-chart/<int:chart_id>", methods=["GET"])
@page(owner_only=True)
def chart_edit_page(uid, chart_id):
    chart = S.get_chart(uid, chart_id)
    if not chart:
        flash("चार्ट सापडला नाही.", "danger")
        return redirect(url_for("fat_snf.charts_page"))
    chart = dict(chart, values=S.get_chart_values(chart_id))
    return render_template("fat_snf/chart_editor.html", chart=jsonable(chart),
                           source=jsonable(chart), revise_id=None,
                           today=L.today_ist().isoformat())


@bp.route("/api/fat_snf/charts/preview", methods=["POST"])
@api(owner_only=True)
def api_chart_preview(uid):
    fats, snfs, matrix = L.generate_matrix(body())
    return ok(fat_values=fats, snf_values=snfs,
              matrix=[{"fat": f, "snf": s, "rate": r} for (f, s), r in sorted(matrix.items())])


@bp.route("/api/fat_snf/charts", methods=["POST"])
@api(owner_only=True)
def api_chart_create(uid):
    data = body()
    revise = data.get("revised_from")
    chart_id = S.create_chart(uid, data, staff_id=None,
                              revised_from=int(revise) if revise else None)
    return ok(message="FAT/SNF दर चार्ट सेव्ह झाला.", chart_id=chart_id, http_status=201)


@bp.route("/api/fat_snf/charts/<int:chart_id>", methods=["GET"])
@api(owner_only=True)
def api_chart_get(uid, chart_id):
    chart = S.get_chart(uid, chart_id)
    if not chart:
        raise S.NotFound("चार्ट सापडला नाही.")
    return ok(chart=chart, values=S.get_chart_values(chart_id))


@bp.route("/api/fat_snf/charts/<int:chart_id>", methods=["PUT"])
@api(owner_only=True)
def api_chart_update(uid, chart_id):
    stats = S.update_chart(uid, chart_id, body())
    msg = "चार्ट अपडेट झाला."
    if stats["entries_using_chart"]:
        msg += (f" या चार्टवरील {stats['entries_using_chart']} जुन्या नोंदींचे दर "
                "बदललेले नाहीत (त्या नोंदीच्या वेळचा दर जतन आहे).")
    return ok(message=msg, **stats)


@bp.route("/api/fat_snf/charts/<int:chart_id>/status", methods=["POST"])
@api(owner_only=True)
def api_chart_status(uid, chart_id):
    active = bool(body().get("active"))
    status = S.set_chart_status(uid, chart_id, active)
    return ok(message="चार्ट सक्रिय केला." if status == "active" else "चार्ट निष्क्रिय केला.",
              status=status)


# ======================================================================
# Rate lookup
# ======================================================================
@bp.route("/api/fat_snf/rate", methods=["GET"])
@api()
def api_rate(uid):
    a = request.args
    on_date = L.parse_date(a.get("date") or L.today_ist().isoformat())
    info = S.preview_rate(uid, S.parse_vendor_id(a.get("vendor_id")),
                          L.parse_milk_type(a.get("milk_type")),
                          L.parse_fat(a.get("fat")), L.parse_snf(a.get("snf")), on_date)
    result = {"rate": info["rate"], "chart_id": info["chart_id"],
              "chart_name": info["chart_name"]}
    if a.get("litres"):
        result["amount"] = L.entry_amount(L.parse_litres(a.get("litres")), info["rate"])
    return ok(**result)


# ======================================================================
# Milk collection
# ======================================================================
@bp.route("/fat_snf/milk_collection", methods=["GET"])
@page()
def collection_page(uid):
    """Render the simple row-based FAT/SNF collection page."""
    now = L.now_ist()
    today = L.today_ist(now)
    auto_slot = L.auto_slot(now)
    # Keep the old page's date/slot query-string behavior, while the save API
    # remains the authority for current-slot/backdated permissions.
    try:
        selected_date = L.parse_date(request.args.get("date") or today.isoformat())
        if selected_date > today:
            selected_date = today
    except ValidationError:
        selected_date = today
    try:
        selected_slot = L.parse_slot(request.args.get("slot") or auto_slot)
    except ValidationError:
        selected_slot = auto_slot
    return render_template(
        "fat_snf/collection.html",
        vendors=jsonable(S.list_vendors(uid)),
        today=today.isoformat(),
        slot=selected_slot,
        selected_date=selected_date.isoformat(),
        mode=S.get_mode(uid),
        is_owner=is_owner(),
        backdated=(request.args.get("backdated") == "1" and is_owner()),
        settings=jsonable(S.get_settings(uid)),
        slot_labels=L.SLOT_LABELS_MR,
    )



@bp.route("/api/fat_snf/entries", methods=["GET"])
@api()
def api_entries(uid):
    on_date = L.parse_date(request.args.get("date") or L.today_ist().isoformat())
    slot = request.args.get("slot")
    slot = L.parse_slot(slot) if slot else None
    rows = S.list_entries(uid, on_date, slot)
    total_l = sum((r["quantity"] for r in rows), Decimal(0))
    total_a = sum((r["amount"] for r in rows), Decimal(0))
    saved = sum(1 for r in rows if r["status"] == "saved")
    return ok(entries=rows, totals={"count": len(rows), "litres": total_l, "amount": total_a,
                                    "saved": saved, "final": len(rows) - saved})


@bp.route("/api/fat_snf/entries", methods=["POST"])
@api()
def api_save_entry(uid):
    require_mode_on(uid)
    result = S.save_entry(uid, body(), staff_id=staff_id(), is_owner=is_owner())
    status = 200 if result["replayed"] else 201
    msg = ("ही नोंद आधीच सेव्ह झाली होती." if result["replayed"] else "नोंद सेव्ह झाली.")
    return ok(message=msg, entry=result["entry"], replayed=result["replayed"],
              warnings=result["warnings"], http_status=status)


@bp.route("/api/fat_snf/requests/<request_id>", methods=["GET"])
@api()
def api_request_status(uid, request_id):
    """Lets a client whose save timed out learn whether it actually landed."""
    rid = S._clean_request_id(request_id)
    entry = S.get_entry_by_request(uid, rid) if rid else None
    return ok(saved=bool(entry), entry=entry)


@bp.route("/api/fat_snf/entries/<int:entry_id>", methods=["GET"])
@api()
def api_get_entry(uid, entry_id):
    entry = S.get_entry(uid, entry_id)
    if not entry:
        raise S.NotFound("नोंद सापडली नाही.")
    return ok(entry=entry)


@bp.route("/api/fat_snf/entries/<int:entry_id>", methods=["PUT"])
@api()
def api_update_entry(uid, entry_id):
    result = S.update_entry(uid, entry_id, body(), is_owner=is_owner())
    return ok(message="नोंद दुरुस्त झाली.", **result)


@bp.route("/api/fat_snf/entries/<int:entry_id>", methods=["DELETE"])
@api(owner_only=True)
def api_delete_entry(uid, entry_id):
    S.delete_entry(uid, entry_id, (request.get_json(silent=True) or {}).get("reason"))
    return ok(message="नोंद काढली.")


@bp.route("/api/fat_snf/finalize", methods=["POST"])
@api()
def api_finalize(uid):
    require_mode_on(uid)
    data = body()
    on_date = L.parse_date(data.get("date"))
    slot = L.parse_slot(data.get("slot"))
    if on_date > L.today_ist():
        raise ValidationError("भविष्यातील तारीख निवडता येणार नाही.", "date")
    n = S.finalize(uid, on_date, slot)
    msg = (f"{n} नोंदी अंतिम केल्या." if n else "अंतिम करण्यासाठी नवीन नोंद नाही.")
    return ok(message=msg, finalized=n)


# ======================================================================
# Edit entries
# ======================================================================
@bp.route("/fat_snf/edit_entry", methods=["GET"])
@page()
def edit_entries_page(uid):
    try:
        on_date = L.parse_date(request.args.get("date") or L.today_ist().isoformat())
    except ValidationError:
        on_date = L.today_ist()
    return render_template("fat_snf/edit_entries.html", entries=S.list_entries(uid, on_date),
                           selected_date=on_date.isoformat(), is_owner=is_owner(),
                           today=L.today_ist().isoformat(), slot_labels=L.SLOT_LABELS_MR,
                           type_labels=L.TYPE_LABELS_MR)


# ======================================================================
# Calculation (today's / any period) + CSV export
# ======================================================================
@bp.route("/fat_snf/calculation", methods=["GET"])
@page(owner_only=True)
def calculation(uid):
    try:
        start, end = range_from_args()
    except ValidationError as e:
        flash(e.message, "danger")
        start = end = L.today_ist()

    q = (request.args.get("q") or "").strip().lower()
    milk_type = request.args.get("milk_type") or ""
    show_all = request.args.get("all") == "1"
    vendors = S.list_vendors(uid)
    if q:
        vendors = [v for v in vendors if q in str(v["vendor_id"]) or q in (v["name"] or "").lower()]
    if milk_type in ("cow", "buffalo"):
        vendors = [v for v in vendors if milk_type in S.vendor_types(v)]

    statements, totals = S.calculation_rows(uid, start, end, vendors)
    if not show_all:
        statements = [s for s in statements if s["has_activity"]]

    if request.args.get("export") == "csv":
        return _calculation_csv(statements, start, end)

    page_no = max(request.args.get("page", 1, type=int), 1)
    pages = max(math.ceil(len(statements) / PAGE_SIZE), 1)
    page_no = min(page_no, pages)
    shown = statements[(page_no - 1) * PAGE_SIZE: page_no * PAGE_SIZE]
    return render_template(
        "fat_snf/calculation.html", statements=shown, totals=totals, start=start, end=end,
        q=q, milk_type=milk_type, show_all=show_all, page=page_no, pages=pages,
        count=len(statements), mode=S.get_mode(uid), slot_labels=L.SLOT_LABELS_MR)


def _calculation_csv(statements, start, end):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["vendor_id", "name", "milk_type",
                "morning_litres", "morning_avg_fat", "morning_avg_snf", "morning_amount",
                "evening_litres", "evening_avg_fat", "evening_avg_snf", "evening_amount",
                "fat_snf_amount", "litre_rate_amount", "food_sack", "advance_deduction",
                "final_payable", "remaining_advance"])

    def safe(text):
        text = str(text or "")
        return "'" + text if text[:1] in ("=", "+", "-", "@") else text  # CSV injection

    for st in statements:
        v = st["vendor"]
        sections = st["sections"] or [None]
        for i, sec in enumerate(sections):
            m = sec["morning"] if sec else {}
            e = sec["evening"] if sec else {}
            first = i == 0
            w.writerow([
                v["vendor_id"], safe(v["name"]), sec["type"] if sec else "",
                m.get("litres", ""), m.get("avg_fat") or "", m.get("avg_snf") or "",
                m.get("amount", ""),
                e.get("litres", ""), e.get("avg_fat") or "", e.get("avg_snf") or "",
                e.get("amount", ""),
                st["fsn_total"] if first else "", st["general_total"] if first else "",
                st["food"]["total"] if first else "", st["period_deduction"] if first else "",
                st["final"] if first else "", st["remaining_advance"] if first else "",
            ])
    return Response(
        "﻿" + buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=dairymitr_calculation_{start}_{end}.csv"})


# ======================================================================
# Receipt (single vendor or all vendors) and vendor report
# ======================================================================
def render_receipt(uid, vendor_id=None, base_template="base.html", customer=False):
    """Shared by owners (any vendor / all) and customers (their own vendor)."""
    params = request.form if request.method == "POST" else request.args
    vendors = S.list_vendors(uid)
    from_date = params.get("from_date") or L.today_ist().isoformat()
    to_date = params.get("to_date") or from_date
    if not customer:
        vendor_id = params.get("vendor_id") or vendor_id
    detailed = params.get("detail") == "1" and not customer

    statements = []
    error = None
    generated = request.method == "POST" or params.get("generate") == "1"
    if generated:
        try:
            start, end = L.parse_date_range(from_date, to_date)
            if vendor_id and str(vendor_id) != "all":
                v = S.get_vendor(uid, S.parse_vendor_id(vendor_id))
                if not v:
                    raise ValidationError("ग्राहक सापडला नाही.", "vendor_id")
                selected = [v]
            elif customer:
                raise ValidationError("ग्राहक सापडला नाही.", "vendor_id")
            else:
                selected = vendors
            statements = S.build_statements(uid, start, end, selected)
            if len(selected) > 1:
                statements = [s for s in statements if s["has_activity"]]
            if detailed:
                for st in statements:
                    st["advance_items"] = S.advance_transactions_for(
                        uid, st["vendor"]["vendor_id"], start, end)
        except ValidationError as e:
            error = e.message

    return render_template(
        "fat_snf/receipt.html", base_template=base_template, customer=customer,
        vendors=vendors, statements=statements, from_date=from_date, to_date=to_date,
        vendor_id=str(vendor_id or ""), generated=generated, error=error, detailed=detailed,
        dairy_name=session.get("dairy_name") or "", printed_at=L.now_ist())


@bp.route("/fat_snf/receipt", methods=["GET", "POST"])
@page(owner_only=True)
def receipt(uid):
    return render_receipt(uid)


# ======================================================================
# Milk summary
# ======================================================================
@bp.route("/fat_snf/milk_summary", methods=["GET"])
@page()
def milk_summary(uid):
    try:
        start, end = range_from_args()
    except ValidationError as e:
        flash(e.message, "danger")
        start = end = L.today_ist()
    return render_template("fat_snf/milk_summary.html", data=S.milk_summary(uid, start, end),
                           start=start, end=end, show_money=is_owner(),
                           type_labels=L.TYPE_LABELS_MR, slot_labels=L.SLOT_LABELS_MR)
