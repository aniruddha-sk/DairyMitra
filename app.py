import os
import re
import secrets
import string
import logging
import bisect
from datetime import datetime, timezone, date, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo
import MySQLdb.cursors

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_file, g
from flask_mysqldb import MySQL

from google import genai
from google.genai import types

from werkzeug.security import generate_password_hash, check_password_hash
from twilio.rest import Client
from twilio.http.http_client import TwilioHttpClient
import smtplib
from flask import send_from_directory
from functools import wraps
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException



import zipfile
from openpyxl.styles import Font, Border, Side, Alignment

from datetime import timedelta
import base64
import csv
import gzip
import hashlib
import hmac
import io
import json
import math
import random
import shutil
import subprocess
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlparse

from flask import Blueprint, Response, current_app, has_request_context
from flask_wtf.csrf import CSRFError, CSRFProtect
from pywebpush import WebPushException, webpush




load_dotenv()


# ==========================================================================
# WEB PUSH (VAPID)
# Formerly push.py.
# ==========================================================================

# ==========================================
# VAPID CONFIG
# ==========================================

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")

VAPID_PRIVATE_KEY_FILE = os.getenv(
    "VAPID_PRIVATE_KEY_FILE",
    "vapid_private_key.pem"
)

VAPID_CLAIM_EMAIL = os.getenv(
    "VAPID_CLAIM_EMAIL",
    "mailto:admin@example.com"
)

# Upper bound (seconds) for push / SMS / email provider calls.
EXTERNAL_CALL_TIMEOUT = float(os.getenv("EXTERNAL_CALL_TIMEOUT", "10"))

notify_log = logging.getLogger("dairymitr.notify")


# ==========================================
# SEND PUSH
# ==========================================

def send_web_push(
    subscription_info,
    title,
    body,
    url="/customer/dashboard"
):
    """Send one push message. Returns (success, expired). Expired (404/410)
    subscriptions should be deleted by the caller."""
    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url
    })

    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY_FILE,
            vapid_claims={
                "sub": VAPID_CLAIM_EMAIL
            },
            # A hung push service must not hold a worker thread.
            timeout=EXTERNAL_CALL_TIMEOUT,
        )
        return True, False

    except WebPushException as ex:
        status = ex.response.status_code if ex.response is not None else None
        expired = status in (404, 410)
        if not expired:
            notify_log.warning("web push failed status=%s error=%s", status, ex)
        return False, expired

    except Exception:
        notify_log.exception("web push error")
        return False, False


# ==========================================================================
# DATABASE BACKUP / RESTORE (mysqldump)
# Formerly backup_system.py. Routes further below restrict these to
# platform administrators (DAIRYMITR_ADMIN_EMAILS).
# ==========================================================================


DB_HOST = os.getenv("MYSQL_HOST")
DB_PORT = int(os.getenv("MYSQL_PORT", 3306))
DB_USER = os.getenv("MYSQL_USER")
DB_PASS = os.getenv("MYSQL_PASSWORD")
DB_NAME = os.getenv("MYSQL_DB")

BACKUP_DIR = "backups"
backup_log = logging.getLogger("dairymitr.backup")


def _mysql_env():
    """Pass the password via the environment, not argv (argv is visible to
    every user on the host through `ps`)."""
    env = os.environ.copy()
    env["MYSQL_PWD"] = DB_PASS or ""
    return env

os.makedirs(BACKUP_DIR, exist_ok=True)


# -------------------------------------------------
# TIME (KOLKATA / IST)
# -------------------------------------------------

def get_timestamp():
    return datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).strftime("%Y%m%d_%H%M%S")


# -------------------------------------------------
# CREATE MYSQL BACKUP (FAST + COMPRESSED)
# -------------------------------------------------

def create_backup(backup_type="manual", user_id=1):

    timestamp = get_timestamp()

    sql_file = os.path.join(
        BACKUP_DIR,
        f"{backup_type}_user{user_id}_{timestamp}_IST.sql"
    )

    gzip_file = sql_file + ".gz"

    command = [
        "mysqldump",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--set-gtid-purged=OFF",
        "-h", DB_HOST,
        "-P", str(DB_PORT),
        "-u", DB_USER,
        DB_NAME
    ]

    try:

        backup_log.info("starting database backup")

        with open(sql_file, "w") as f:
            subprocess.run(command, stdout=f, check=True, env=_mysql_env())

        backup_log.info("compressing backup")

        with open(sql_file, "rb") as f_in:
            with gzip.open(gzip_file, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

        os.remove(sql_file)

        cleanup_old_backups()

        backup_log.info("backup created: %s", gzip_file)

        return os.path.basename(gzip_file)

    except subprocess.CalledProcessError as e:

        backup_log.error("backup failed: %s", e)

        if os.path.exists(sql_file):
            os.remove(sql_file)

        raise


# -------------------------------------------------
# FULL DATABASE BACKUP (ADMIN)
# -------------------------------------------------

def create_full_backup():

    timestamp = get_timestamp()

    sql_file = os.path.join(
        BACKUP_DIR,
        f"admin_full_{timestamp}_IST.sql"
    )

    gzip_file = sql_file + ".gz"

    command = [
        "mysqldump",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--set-gtid-purged=OFF",
        "-h", DB_HOST,
        "-P", str(DB_PORT),
        "-u", DB_USER,
        DB_NAME
    ]

    with open(sql_file, "w") as f:
        subprocess.run(command, stdout=f, check=True, env=_mysql_env())

    with open(sql_file, "rb") as f_in:
        with gzip.open(gzip_file, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    os.remove(sql_file)

    cleanup_old_backups()

    return os.path.basename(gzip_file)


# -------------------------------------------------
# RESTORE BACKUP
# -------------------------------------------------

def restore_backup(filename):

    filename = os.path.basename(filename)
    filepath = os.path.join(BACKUP_DIR, filename)

    if not filename.endswith(".sql.gz") or not os.path.exists(filepath):
        raise Exception("Backup file not found")

    # -------------------------------------------------
    # SAFETY BACKUP BEFORE RESTORE
    # -------------------------------------------------

    safety_name = create_backup("safety", 1)
    backup_log.info("safety backup created: %s", safety_name)

    temp_sql = filepath.replace(".gz", ".sql")

    backup_log.info("extracting backup")

    with gzip.open(filepath, "rb") as f_in:
        with open(temp_sql, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    command = [
        "mysql",
        "--max_allowed_packet=512M",
        "-h", DB_HOST,
        "-P", str(DB_PORT),
        "-u", DB_USER,
        DB_NAME
    ]

    backup_log.info("restoring database")

    with open(temp_sql, "rb") as sql_file:
        subprocess.run(command, stdin=sql_file, check=True, env=_mysql_env())

    os.remove(temp_sql)

    backup_log.info("restore completed")

    return True


# -------------------------------------------------
# CLEANUP OLD BACKUPS
# -------------------------------------------------

def cleanup_old_backups():

    files = sorted(
        os.listdir(BACKUP_DIR),
        key=lambda x: os.path.getmtime(os.path.join(BACKUP_DIR, x))
    )

    while len(files) > 30:

        oldest = files[0]
        filepath = os.path.join(BACKUP_DIR, oldest)

        try:
            os.remove(filepath)
            backup_log.info("deleted old backup: %s", oldest)
        except Exception as e:
            backup_log.warning("could not delete old backup: %s", e)

        files.pop(0)


# -------------------------------------------------
# BACKUP HISTORY
# -------------------------------------------------

def list_backups():

    files = sorted(
        os.listdir(BACKUP_DIR),
        key=lambda x: os.path.getmtime(os.path.join(BACKUP_DIR, x)),
        reverse=True
    )

    return files


# ==========================================================================
# AI HELPERS (anomaly check, milk prediction, vendor analysis)
# Formerly ai/*.py. numpy, pandas and scikit-learn are imported inside the
# functions so gunicorn workers that never serve these features do not
# load them (saves memory per worker).
# ==========================================================================

def detect_anomaly(previous_values, new_value):

    if len(previous_values) < 5:
        return False

    import numpy as np

    mean = np.mean(previous_values)
    std = np.std(previous_values)

    threshold = mean + (3 * std)

    if new_value > threshold:
        return True

    return False


def predict_milk(df):

    if df.empty or len(df) < 5:
        return None

    from sklearn.linear_model import LinearRegression

    df = df.sort_values("date")

    df["day_index"] = range(len(df))

    X = df[["day_index"]]
    y = df["quantity"]

    model = LinearRegression()
    model.fit(X, y)

    next_day = [[len(df)]]

    prediction = model.predict(next_day)[0]

    if prediction < 0:
        prediction = 0

    return round(prediction, 2)


def analyze_vendor(values):

    if not values:
        return None

    import numpy as np

    avg = np.mean(values)
    std = np.std(values)

    if avg == 0:
        consistency = 0
    else:
        consistency = max(0, 100 - ((std / avg) * 100))

    if consistency >= 90:
        rating = "Excellent"
    elif consistency >= 70:
        rating = "Good"
    else:
        rating = "Unstable"

    return {
        "average": round(avg, 2),
        "consistency": round(consistency, 2),
        "rating": rating
    }



# ==========================================================
# GEMINI AI CONFIGURATION
# ==========================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    logging.warning("GEMINI_API_KEY is not configured.")

_gemini_client = None


def get_gemini_client():
    """Created on first use in each worker (not at import), so gunicorn's
    preload/fork never shares its HTTP connection pool between processes."""
    global _gemini_client
    if _gemini_client is None and GEMINI_API_KEY:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client

GEMINI_MODEL = "gemini-3.8-flash"

# ------------------------------
# Basic config & logging
# ------------------------------
app = Flask(__name__)

DAIRYMITR_ENV = (os.getenv("DAIRYPRO_ENV") or os.getenv("DAIRYMITR_ENV") or "production").strip().lower()
IS_DEVELOPMENT = DAIRYMITR_ENV in ("development", "dev", "test")

# The previous hard-coded fallback key was published in the repository, which
# would let anyone forge a logged-in session. A real key is now mandatory.
_secret_key = os.getenv("SECRET_KEY", "").strip()
if not _secret_key or len(_secret_key) < 32:
    if not IS_DEVELOPMENT:
        raise RuntimeError(
            "SECRET_KEY environment variable is missing or shorter than 32 characters. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    _secret_key = secrets.token_hex(32)
    logging.warning("DEVELOPMENT MODE: using a random SECRET_KEY (sessions reset on restart).")
app.secret_key = _secret_key

app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "1") != "0"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # largest legitimate body is a rate chart

# Behind a reverse proxy (Render/Railway/Nginx) the client IP arrives in
# X-Forwarded-For; trust exactly the configured number of proxy hops so rate
# limits use the real client address and cannot be spoofed.
_proxy_hops = int(os.getenv("TRUST_PROXY_HOPS", "0") or 0)
if _proxy_hops > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxy_hops, x_proto=_proxy_hops,
                            x_host=_proxy_hops)

# MySQL configuration from environment
app.config['MYSQL_HOST'] = os.getenv("MYSQL_HOST")
app.config['MYSQL_USER'] = os.getenv("MYSQL_USER")
app.config['MYSQL_PASSWORD'] = os.getenv("MYSQL_PASSWORD")
app.config['MYSQL_DB'] = os.getenv("MYSQL_DB")
app.config["MYSQL_PORT"] = int(os.getenv("MYSQL_PORT", 3306))
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'

app.config['MYSQL_CONNECT_TIMEOUT'] = int(os.getenv("MYSQL_CONNECT_TIMEOUT", 10))
app.config['MYSQL_READ_DEFAULT_FILE'] = ''
app.config['MYSQL_CHARSET'] = 'utf8mb4'
# NOTE: autocommit stays ON for the existing single-statement routes.
# Multi-statement financial operations open an explicit transaction
# (core.db.transaction() / core.db.begin()), otherwise FOR UPDATE locks
# and rollbacks would have no effect.
app.config['MYSQL_AUTOCOMMIT'] = True


mysql = MySQL(app)


# ==========================================================================
# BRANDING
# ==========================================================================
BRAND_NAME = "DairyPro"
BRAND_TAGLINE = "SMART DAIRY MANAGEMENT"
# One place for the logo: replace static/images/dairypro-logo.png (or point
# DAIRYPRO_LOGO at another file under static/) to change it everywhere.
BRAND_LOGO = os.getenv("DAIRYPRO_LOGO", "images/dairypro-logo.png")
BRAND_ICON = os.getenv("DAIRYPRO_ICON", "images/dairypro-icon.png")


# ==========================================================================
# I18N: Marathi (default) / English / Hindi
#
# i18n/catalog.json holds every user-visible string as one entry with all
# three languages: {"mr": "...", "en": "...", "hi": "..."}. A lookup may
# start from ANY of the three variants, so existing templates and messages
# keep their original source text (Marathi or English) and are translated
# at render time: {{ _('दूध संकलन') }}, _t("Invalid OTP."), t("...") in JS.
# Values are never machine-translated: user data (names, addresses) is
# simply not passed through _t.
# ==========================================================================
SUPPORTED_LANGUAGES = ("mr", "en", "hi")
DEFAULT_LANGUAGE = "mr"
LANGUAGE_NAMES = {"mr": "मराठी", "en": "English", "hi": "हिन्दी"}
LANGUAGE_COOKIE = "dp_lang"
I18N_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")
i18n_log = logging.getLogger("dairypro.i18n")


def _i18n_norm(text):
    return " ".join(str(text).split())


class TranslationCatalog:
    def __init__(self, path):
        self.path = path
        self.entries = []
        self.index = {}
        self.version = "0"
        self.load()

    def load(self):
        try:
            with open(self.path, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            i18n_log.warning("translation catalog missing: %s", self.path)
            return
        self.version = hashlib.sha1(raw).hexdigest()[:10]
        self.entries = json.loads(raw.decode("utf-8"))
        index = {}
        for entry in self.entries:
            for lang in SUPPORTED_LANGUAGES:
                value = entry.get(lang)
                if value:
                    index.setdefault(_i18n_norm(value), entry)
        self.index = index

    def lookup(self, text, lang):
        entry = self.index.get(_i18n_norm(text))
        if entry is None:
            return None
        return entry.get(lang) or None

    def js_dictionary(self, lang):
        """{any variant: text in `lang`} for strings used by JavaScript."""
        out = {}
        for entry in self.entries:
            if not entry.get("js"):
                continue
            target = entry.get(lang)
            if not target:
                continue
            for l in SUPPORTED_LANGUAGES:
                if entry.get(l):
                    out.setdefault(_i18n_norm(entry[l]), target)
        return out


I18N = TranslationCatalog(os.path.join(I18N_DIR, "catalog.json"))
_I18N_DEBUG = os.getenv("DAIRYPRO_I18N_DEBUG") == "1"


class _KeepMissing(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def normalize_language(value):
    value = (value or "").strip().lower()[:5]
    return value if value in SUPPORTED_LANGUAGES else None


def current_language():
    """Session (loaded from the account at login) -> cookie -> Marathi."""
    if not has_request_context():
        return DEFAULT_LANGUAGE
    lang = getattr(g, "_dp_lang", None)
    if lang:
        return lang
    lang = (normalize_language(session.get("lang"))
            or normalize_language(request.cookies.get(LANGUAGE_COOKIE))
            or DEFAULT_LANGUAGE)
    g._dp_lang = lang
    return lang


def _t(text, lang=None, **params):
    """Translate `text` (any source language) into the current language.
    Unknown text is returned unchanged. `params` fill {placeholders}."""
    if text is None or text == "":
        return text
    if not isinstance(text, str):
        text = str(text)
    found = I18N.lookup(text, lang or current_language())
    if found is None:
        if _I18N_DEBUG and re.search(r"[A-Za-zऀ-ॿ]", text):
            i18n_log.info("missing translation: %r", _i18n_norm(text))
        found = text
    if params:
        return found.format_map(_KeepMissing(params))
    return found


def remember_language_for_account(lang):
    """Persist the language on the logged-in account (owner, staff or
    customer) so it follows the user to other devices."""
    role = session.get("role")
    try:
        cur = db_cursor()
        if role == "owner":
            cur.execute("UPDATE users SET preferred_language=%s WHERE id=%s",
                        (lang, int(session["id"])))
        elif role == "staff" and session.get("staff_id"):
            cur.execute("UPDATE staff SET preferred_language=%s WHERE id=%s",
                        (lang, int(session["staff_id"])))
        elif role == "customer" and session.get("vendor_id"):
            cur.execute("UPDATE vendors SET preferred_language=%s "
                        "WHERE user_id=%s AND vendor_id=%s",
                        (lang, int(session["id"]), int(session["vendor_id"])))
        cur.close()
    except Exception:
        i18n_log.exception("could not store preferred language")


def apply_account_language(preferred):
    """Called right after a successful login with the account's stored
    preference (None keeps the device's current choice)."""
    lang = normalize_language(preferred)
    if lang:
        session["lang"] = lang
    elif not session.get("lang"):
        cookie = normalize_language(request.cookies.get(LANGUAGE_COOKIE))
        if cookie:
            session["lang"] = cookie


app.jinja_env.globals.update(
    _=_t,
    current_language=current_language,
    LANGUAGE_NAMES=LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES=SUPPORTED_LANGUAGES,
    BRAND_NAME=BRAND_NAME,
    BRAND_TAGLINE=BRAND_TAGLINE,
    BRAND_LOGO=BRAND_LOGO,
    BRAND_ICON=BRAND_ICON,
)


@app.template_filter("dmy")
def format_dmy(value, with_time=False):
    """Every user-facing date is DD/MM/YYYY. Accepts date, datetime or an
    ISO string; storage formats are never touched."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        text = value.strip()
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00")) if (
                "T" in text or " " in text) else date.fromisoformat(text[:10])
        except ValueError:
            return text
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M" if with_time else "%d/%m/%Y")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return str(value)


@app.template_filter("inr_paise")
def format_inr_paise(paise):
    """39900 -> '₹399', 119900 -> '₹1,199' (Indian digit grouping)."""
    rupees = Decimal(int(paise or 0)) / 100
    whole = int(rupees)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = head + "," + tail
    frac = rupees - whole
    return "₹" + s + (f"{frac:.2f}"[1:] if frac else "")


_I18N_JSON_FIELDS = ("message", "error", "warning", "reason")


@app.after_request
def localize_json_messages(response):
    """JSON APIs build their messages in the source language; translate the
    user-facing text fields once here so every endpoint is covered."""
    try:
        if (response.direct_passthrough or not response.is_json
                or (response.content_length or 0) > 262144):
            return response
        data = response.get_json(silent=True)
        if not isinstance(data, dict):
            return response
        changed = False
        for key in _I18N_JSON_FIELDS:
            value = data.get(key)
            if isinstance(value, str) and value:
                new = _t(value)
                if new != value:
                    data[key] = new
                    changed = True
        warnings = data.get("warnings")
        if isinstance(warnings, list):
            new_list = [_t(w) if isinstance(w, str) else w for w in warnings]
            if new_list != warnings:
                data["warnings"] = new_list
                changed = True
        skipped = data.get("skipped")
        if isinstance(skipped, list):
            for item in skipped:
                if isinstance(item, dict) and isinstance(item.get("reason"), str):
                    new = _t(item["reason"])
                    if new != item["reason"]:
                        item["reason"] = new
                        changed = True
        if changed:
            response.set_data(json.dumps(data, ensure_ascii=False, default=str))
    except Exception:
        i18n_log.exception("JSON localization failed")
    return response


# ==========================================================================
# DATABASE HELPERS: real transactions
# Formerly core/db.py.
# ==========================================================================

def db_connection():
    return mysql.connection


def db_cursor():
    """Plain DictCursor (no SafeCursor digit coercion: phone numbers and
    OTPs keep their leading zeros)."""
    return db_connection().cursor(MySQLdb.cursors.DictCursor)


@contextmanager
def db_transaction():
    """BEGIN ... COMMIT, or ROLLBACK on any exception.

    Usage:
        with db_transaction() as cur:
            cur.execute(...)
    """
    conn = db_connection()
    conn.begin()
    cur = conn.cursor(MySQLdb.cursors.DictCursor)
    try:
        yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        cur.close()


def db_begin():
    """Start an explicit transaction on the request connection (for legacy
    routes that already call commit()/rollback() themselves)."""
    db_connection().begin()


# ==========================================================================
# AUDIT TRAIL (dm_audit_log)
# Formerly core/audit.py.
# ==========================================================================

audit_logger = logging.getLogger("dairymitr.audit")


def _audit_json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _audit_dump(values):
    if values is None:
        return None
    return json.dumps(values, default=_audit_json_default, ensure_ascii=False)


def audit_actor():
    """(role, actor_id) of the current request."""
    if not has_request_context():
        return "system", None
    role = session.get("role") or "anonymous"
    if role == "staff":
        return role, session.get("staff_id")
    if role == "customer":
        return role, session.get("customer_id")
    return role, session.get("id")


def audit_record(action, target_type=None, target_id=None, old=None, new=None,
           user_id=None, cur=None):
    """Insert one audit row. With `cur`, it joins the caller's transaction
    (and a failure aborts that transaction). Without `cur`, failures are
    logged and swallowed so auditing never breaks a read-only page."""
    role, actor_id = audit_actor()
    if user_id is None and has_request_context():
        user_id = session.get("id")
    request_id = getattr(g, "request_id", None) if has_request_context() else None
    ip = request.remote_addr if has_request_context() else None

    sql = (
        "INSERT INTO dm_audit_log (user_id, actor_role, actor_id, action, "
        "target_type, target_id, old_values, new_values, request_id, ip) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    params = (
        user_id, role, actor_id, action[:60], target_type,
        None if target_id is None else str(target_id)[:64],
        _audit_dump(old), _audit_dump(new), request_id, ip,
    )
    audit_logger.info("audit action=%s tenant=%s actor=%s:%s target=%s:%s req=%s",
             action, user_id, role, actor_id, target_type, target_id, request_id)
    if cur is not None:
        cur.execute(sql, params)
        return
    try:
        c = db_cursor()
        c.execute(sql, params)
        c.close()
    except Exception:
        audit_logger.exception("audit write failed (action=%s)", action)


# ==========================================================================
# SECURITY: CSRF, headers, staff allow-list, rate limits, OTP challenges
# Formerly core/security.py.
# ==========================================================================

security_log = logging.getLogger("dairymitr.security")

csrf = CSRFProtect()

RATE_LIMIT_MESSAGE = "खूप वेळा प्रयत्न झाले. कृपया थोड्या वेळाने पुन्हा प्रयत्न करा."
FORBIDDEN_MESSAGE = "तुम्हाला ही कृती करण्याची परवानगी नाही."

# ----------------------------------------------------------------------
# Staff may only reach these endpoints. Everything else is owner-only.
# (Previously staff restrictions existed only in the templates.)
# ----------------------------------------------------------------------
STAFF_ALLOWED_ENDPOINTS = {
    "dashboard", "logout", "static", "manifest", "service_worker",
    "healthcheck", "favicon",
    # general (OFF-mode) daily work
    "milk_collection", "submit_milk_ajax", "submit_bulk_milk_ajax", "get_milk_data",
    "milk_summary", "vendor_range_summary",
    "food_sack", "submit_food_sack_ajax", "submit_bulk_food_sack_ajax",
    "edit_entry", "update_entries", "delete_entry",
    "edit_food_sack", "delete_food_sack",
    # AI assistant (existing behaviour: available to staff)
    "dairy_mitr_ai_chat",
    # FAT/SNF daily work
    "fat_snf.collection_page", "fat_snf.api_rate", "fat_snf.api_entries",
    "fat_snf.api_save_entry", "fat_snf.api_get_entry", "fat_snf.api_update_entry",
    "fat_snf.api_finalize", "fat_snf.api_request_status",
    "fat_snf.edit_entries_page", "fat_snf.milk_summary", "fat_snf.api_get_mode",
    # DairyPro: language, offline sync, subscription status (read only;
    # paying is owner-only and enforced again in the route)
    "set_language", "i18n_js", "offline_page", "session_ping",
    "subscription_page", "api_subscription_status", "api_subscription_reminder_seen",
}


def wants_json():
    if request.path.startswith("/api/"):
        return True
    if request.is_json:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.accept_mimetypes
    return accept.best == "application/json"


def json_error(message, status, code=None, **extra):
    body = {"success": False, "message": message, "error": message}
    if code:
        body["code"] = code
    rid = getattr(g, "request_id", None)
    if rid:
        body["request_id"] = rid
    body.update(extra)
    return jsonify(body), status


def is_platform_admin():
    """Platform administrators (not dairy owners) may run whole-database
    backup/restore. Configure with DAIRYMITR_ADMIN_EMAILS=a@x.com,b@y.com"""
    if session.get("role") != "owner":
        return False
    admins = {
        e.strip().lower()
        for e in (os.getenv("DAIRYPRO_ADMIN_EMAILS") or os.getenv("DAIRYMITR_ADMIN_EMAILS") or "").split(",")
        if e.strip()
    }
    return (session.get("email") or "").lower() in admins


def enforce_staff_allowlist():
    """Called from the app's before_request. Returns a response to block."""
    if session.get("role") != "staff":
        return None
    endpoint = request.endpoint or ""
    if endpoint in STAFF_ALLOWED_ENDPOINTS:
        return None
    security_log.warning("staff access denied endpoint=%s staff_id=%s",
                endpoint, session.get("staff_id"))
    if wants_json() or request.method != "GET":
        return json_error(FORBIDDEN_MESSAGE, 403, code="forbidden")
    flash(FORBIDDEN_MESSAGE, "danger")
    return redirect(url_for("dashboard"))


def owner_required_response():
    """For routes reachable by owner-only logic inside shared views."""
    if wants_json():
        return json_error(FORBIDDEN_MESSAGE, 403, code="forbidden")
    flash(FORBIDDEN_MESSAGE, "danger")
    return redirect(url_for("dashboard"))


# ----------------------------------------------------------------------
# Rate limiting (fixed window, stored in dm_rate_limits so it works across
# gunicorn workers and restarts)
# ----------------------------------------------------------------------
def client_ip():
    return request.remote_addr or "unknown"


def hit_rate_limit(name, key, limit, window_seconds):
    """Record a hit. Returns True if the caller is still within the limit.
    Fails open (allows) if the limiter table is unavailable, but logs it."""
    bucket = hashlib.sha256(f"{name}:{key}".encode()).hexdigest()
    now = int(time.time())
    expires = now + window_seconds
    try:
        cur = db_cursor()
        cur.execute(
            "INSERT INTO dm_rate_limits (bucket_key, hits, expires_at) "
            "VALUES (%s, 1, %s) ON DUPLICATE KEY UPDATE "
            "hits = IF(expires_at < %s, 1, hits + 1), "
            "expires_at = IF(expires_at < %s, %s, expires_at)",
            (bucket, expires, now, now, expires),
        )
        cur.execute("SELECT hits FROM dm_rate_limits WHERE bucket_key = %s", (bucket,))
        row = cur.fetchone()
        if random.random() < 0.01:
            cur.execute("DELETE FROM dm_rate_limits WHERE expires_at < %s LIMIT 500", (now,))
        cur.close()
        return (row["hits"] if row else 1) <= limit
    except Exception:
        security_log.exception("rate limiter unavailable (name=%s)", name)
        return True


def is_blocked(name, key, limit):
    """Read-only check (no hit recorded): True when the bucket is over limit."""
    bucket = hashlib.sha256(f"{name}:{key}".encode()).hexdigest()
    try:
        cur = db_cursor()
        cur.execute("SELECT hits FROM dm_rate_limits WHERE bucket_key = %s AND expires_at >= %s",
                    (bucket, int(time.time())))
        row = cur.fetchone()
        cur.close()
        return bool(row) and row["hits"] >= limit
    except Exception:
        security_log.exception("rate limiter unavailable (name=%s)", name)
        return False


def failures_blocked(rules):
    """For login forms: only FAILED attempts count. Check before verifying
    the password; call record_failures(rules) after a failed attempt."""
    blocked = any(is_blocked(name, key, limit) for name, key, limit, _ in rules)
    if blocked:
        security_log.warning("login blocked after repeated failures path=%s ip=%s", request.path, client_ip())
    return blocked


def record_failures(rules):
    for name, key, limit, window in rules:
        hit_rate_limit(name, key, limit, window)


def rate_limited(rules):
    """rules: iterable of (name, key, limit, window). True if ANY is exceeded."""
    exceeded = False
    for name, key, limit, window in rules:
        if not hit_rate_limit(name, key, limit, window):
            exceeded = True
    if exceeded:
        security_log.warning("rate limit exceeded path=%s ip=%s", request.path, client_ip())
    return exceeded


# ----------------------------------------------------------------------
# Server-side OTP challenges (dm_challenges). Only the opaque challenge id
# goes into the (signed, NOT encrypted) session cookie — never the OTP.
# ----------------------------------------------------------------------
MAX_OTP_ATTEMPTS = 5


def _otp_hash(challenge_id, otp):
    key = current_app.secret_key.encode() if isinstance(current_app.secret_key, str) \
        else current_app.secret_key
    return hmac.new(key, f"{challenge_id}:{otp}".encode(), hashlib.sha256).hexdigest()


def _account_key(purpose, identity):
    return hashlib.sha256(f"{purpose}:{identity}".lower().encode()).hexdigest()


def create_challenge(purpose, identity, payload, ttl_seconds, otp=None):
    """Create (or replace) the challenge for purpose+identity.
    Returns (challenge_id, otp)."""
    otp = otp or "".join(secrets.choice("0123456789") for _ in range(6))
    challenge_id = secrets.token_hex(32)
    cur = db_cursor()
    cur.execute(
        "REPLACE INTO dm_challenges (id, purpose, account_key, otp_hash, payload, "
        "expires_at, attempts, consumed) VALUES (%s,%s,%s,%s,%s,%s,0,0)",
        (challenge_id, purpose, _account_key(purpose, identity),
         _otp_hash(challenge_id, otp), json.dumps(payload),
         int(time.time()) + ttl_seconds),
    )
    if random.random() < 0.05:
        cur.execute("DELETE FROM dm_challenges WHERE expires_at < %s LIMIT 500",
                    (int(time.time()) - 86400,))
    cur.close()
    return challenge_id, otp


def verify_challenge(challenge_id, purpose, otp):
    """Returns (status, payload). status: ok | invalid | expired | locked | missing.
    A correct OTP consumes the challenge (single use)."""
    if not challenge_id:
        return "missing", None
    otp = (otp or "").strip()
    with db_transaction() as cur:
        cur.execute(
            "SELECT * FROM dm_challenges WHERE id = %s AND purpose = %s FOR UPDATE",
            (challenge_id, purpose),
        )
        row = cur.fetchone()
        if not row or row["consumed"]:
            return "missing", None
        if row["expires_at"] < int(time.time()):
            return "expired", None
        if row["attempts"] >= MAX_OTP_ATTEMPTS:
            return "locked", None
        if not (otp.isdigit() and len(otp) == 6) or not hmac.compare_digest(
                row["otp_hash"], _otp_hash(challenge_id, otp)):
            cur.execute("UPDATE dm_challenges SET attempts = attempts + 1 WHERE id = %s",
                        (challenge_id,))
            return ("locked" if row["attempts"] + 1 >= MAX_OTP_ATTEMPTS else "invalid"), None
        cur.execute("UPDATE dm_challenges SET consumed = 1 WHERE id = %s", (challenge_id,))
        return "ok", json.loads(row["payload"])


def discard_challenge(challenge_id):
    if not challenge_id:
        return
    cur = db_cursor()
    cur.execute("DELETE FROM dm_challenges WHERE id = %s", (challenge_id,))
    cur.close()


CHALLENGE_MESSAGES = {
    "invalid": "चुकीचा OTP. कृपया पुन्हा प्रयत्न करा.",
    "expired": "OTP ची मुदत संपली. कृपया पुन्हा सुरुवात करा.",
    "locked": "खूप वेळा चुकीचा OTP टाकला. कृपया पुन्हा सुरुवात करा.",
    "missing": "सत्र संपले. कृपया पुन्हा सुरुवात करा.",
}


# ----------------------------------------------------------------------
# App wiring: CSRF, request id, headers, error handlers
# ----------------------------------------------------------------------
CSP = "; ".join([
    "default-src 'self'",
    # Inline scripts are still used throughout the existing templates;
    # moving them to files (and dropping 'unsafe-inline') is a follow-up.
    # Icons, fonts and helpers are served from /static (works offline);
    # only Razorpay Checkout is loaded from outside, on the subscription page.
    "script-src 'self' 'unsafe-inline' https://checkout.razorpay.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' data: https://fonts.gstatic.com",
    "img-src 'self' data: blob: https:",
    # the service worker (same CSP) re-fetches the Google Fonts it caches
    "connect-src 'self' https://api.razorpay.com https://lumberjack.razorpay.com "
    "https://fonts.googleapis.com https://fonts.gstatic.com",
    "frame-src https://www.youtube.com https://www.google.com https://api.razorpay.com "
    "https://checkout.razorpay.com",
    "worker-src 'self'",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
])


def init_security(app):
    app.config.setdefault("WTF_CSRF_TIME_LIMIT", None)  # bound to session lifetime
    app.config.setdefault("WTF_CSRF_SSL_STRICT", True)
    csrf.init_app(app)

    @app.before_request
    def _assign_request_id():
        incoming = request.headers.get("X-Request-ID", "")
        g.request_id = incoming[:32] if incoming.isalnum() else uuid.uuid4().hex

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Request-ID", getattr(g, "request_id", ""))
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy",
                                    "geolocation=(), camera=(), microphone=()")
        header = ("Content-Security-Policy-Report-Only"
                  if os.getenv("CSP_REPORT_ONLY") == "1" else "Content-Security-Policy")
        response.headers.setdefault(header, CSP)
        if app.config.get("SESSION_COOKIE_SECURE") and request.is_secure:
            response.headers.setdefault("Strict-Transport-Security",
                                        "max-age=31536000; includeSubDomains")
        return response

    @app.errorhandler(CSRFError)
    def _csrf_error(e):
        security_log.warning("CSRF rejected path=%s reason=%s", request.path, e.description)
        msg = "सत्राची सुरक्षा तपासणी अयशस्वी झाली. कृपया पान रिफ्रेश करून पुन्हा प्रयत्न करा."
        if wants_json():
            return json_error(msg, 400, code="csrf")
        flash(msg, "warning")
        return redirect(request.referrer if _same_origin(request.referrer) else url_for("login"))

    @app.errorhandler(403)
    def _forbidden(e):
        if wants_json():
            return json_error(FORBIDDEN_MESSAGE, 403, code="forbidden")
        return render_template("errors/error.html", code=403, message=FORBIDDEN_MESSAGE), 403

    @app.errorhandler(404)
    def _not_found(e):
        msg = "हे पान सापडले नाही."
        if wants_json():
            return json_error(msg, 404, code="not_found")
        return render_template("errors/error.html", code=404, message=msg), 404

    @app.errorhandler(413)
    def _too_large(e):
        msg = "पाठवलेली माहिती खूप मोठी आहे."
        if wants_json():
            return json_error(msg, 413, code="too_large")
        return render_template("errors/error.html", code=413, message=msg), 413

    @app.errorhandler(429)
    def _too_many(e):
        if wants_json():
            return json_error(RATE_LIMIT_MESSAGE, 429, code="rate_limited")
        return render_template("errors/error.html", code=429, message=RATE_LIMIT_MESSAGE), 429

    @app.errorhandler(500)
    def _server_error(e):
        security_log.error("unhandled error request_id=%s path=%s", getattr(g, "request_id", "-"),
                  request.path)
        msg = "सर्व्हरवर अडचण आली. कृपया पुन्हा प्रयत्न करा."
        if wants_json():
            return json_error(msg, 500, code="server_error")
        return render_template("errors/error.html", code=500, message=msg,
                               request_id=getattr(g, "request_id", None)), 500


def _same_origin(url):
    if not url:
        return False
    p = urlparse(url)
    return p.netloc == request.host


init_security(app)

# ==============================================================================
# PERFORMANCE NOTES (read-only, no behavior change)
# ------------------------------------------------------------------------------
# Recommended MySQL indexes (run these directly on the database - not part of
# this file, since Flask/MySQLdb does not manage schema/indexes):
#
#   CREATE INDEX idx_milk_collection_user_date        ON milk_collection(user_id, date);
#   CREATE INDEX idx_milk_collection_vendor_date       ON milk_collection(vendor_id, date);
#   CREATE INDEX idx_milk_collection_user_vendor_date  ON milk_collection(user_id, vendor_id, date);
#   CREATE INDEX idx_advance_user_date                 ON advance(user_id, date);
#   CREATE INDEX idx_advance_vendor_date               ON advance(vendor_id, date);
#   CREATE INDEX idx_food_sack_user_date               ON food_sack(user_id, date);
#   CREATE INDEX idx_food_sack_vendor_date             ON food_sack(vendor_id, date);
#   CREATE INDEX idx_vendor_milk_rates_vendor_date      ON vendor_milk_rates(vendor_id, user_id, date_from);
#   CREATE INDEX idx_milk_rates_user_animal_date        ON milk_rates(user_id, animal, date_from);
#   CREATE INDEX idx_vendors_user                       ON vendors(user_id);
#   CREATE INDEX idx_staff_owner                        ON staff(owner_id);
# ==============================================================================


class SafeCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, query, params=None):

        if params:

            fixed = []

            for p in params:

                if isinstance(p, bytes):
                    p = p.decode()

                if isinstance(p, str) and p.isdigit():
                    p = int(p)

                fixed.append(p)

            params = tuple(fixed)

        return self.cursor.execute(query, params)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def close(self):
        return self.cursor.close()

    def __getattr__(self, name):
        return getattr(self.cursor, name)


# ----------------------------------------------------------------------
# Background delivery for SMS / web push.
#
# Saving milk must not wait for Twilio or the push services: a bulk submit
# for 200 vendors used to make 200+ provider calls inside the request and
# could pass gunicorn's timeout. The notification ROW is still written in
# the request (so the customer's notification list is never lost); only
# the network delivery runs on a small per-worker thread pool, after the
# data has been committed. Messages still queued when a worker is killed
# are not delivered (the stored notification remains).
# ----------------------------------------------------------------------
NOTIFY_THREADS = max(int(os.getenv("NOTIFY_THREADS", "2") or 2), 1)
_notify_pool = None
_notify_pool_lock = threading.Lock()


def _notify_executor():
    global _notify_pool
    if _notify_pool is None:
        with _notify_pool_lock:
            if _notify_pool is None:
                _notify_pool = ThreadPoolExecutor(max_workers=NOTIFY_THREADS,
                                                  thread_name_prefix="dm-notify")
    return _notify_pool


def _run_in_app_context(fn, args, kwargs):
    try:
        with app.app_context():
            fn(*args, **kwargs)
    except Exception:
        notify_log.exception("background delivery failed (%s)", getattr(fn, "__name__", fn))


def deliver_later(fn, *args, **kwargs):
    """Run a delivery function (SMS / push) off the request thread.
    NOTIFY_SYNC=1 (or app TESTING) runs it inline, which keeps tests and
    local debugging deterministic."""
    if os.getenv("NOTIFY_SYNC") == "1" or app.config.get("TESTING"):
        _run_in_app_context(fn, args, kwargs)
        return None
    try:
        return _notify_executor().submit(_run_in_app_context, fn, args, kwargs)
    except RuntimeError:
        # The pool refuses new work while this worker is shutting down
        # (gunicorn max_requests recycling). Deliver inline instead of
        # failing a request whose data is already saved.
        _run_in_app_context(fn, args, kwargs)
        return None


def push_to_customers(items):
    """items: iterable of (user_id, vendor_id, title, body). One background
    job for a whole batch."""
    for user_id, vendor_id, title, body in items:
        push_to_customer(user_id, vendor_id, title, body)


def push_to_customer(user_id, vendor_id, title, body, url="/customer/dashboard"):
    try:
        cursor = SafeCursor(mysql.connection.cursor())

        cursor.execute("""
            SELECT id, endpoint, p256dh, auth
            FROM push_subscriptions
            WHERE user_id=%s AND vendor_id=%s
        """, (user_id, vendor_id))

        subs = cursor.fetchall()

        for s in subs:
            subscription_info = {
                "endpoint": s["endpoint"],
                "keys": {
                    "p256dh": s["p256dh"],
                    "auth": s["auth"]
                }
            }

            success, expired = send_web_push(
                subscription_info,
                title,
                body,
                url
            )

            if expired:
                notify_log.info("deleting expired push subscription id=%s", s["id"])
                cursor.execute(
                    "DELETE FROM push_subscriptions WHERE id=%s",
                    (s["id"],)
                )
                mysql.connection.commit()

        cursor.close()

    except Exception:
        notify_log.exception("push_to_customer failed")


def create_notification(
    user_id,
    vendor_id,
    notification_type,
    title,
    message
):
    try:
        cursor = SafeCursor(mysql.connection.cursor())

        cursor.execute("""
            INSERT INTO customer_notifications
            (
                user_id,
                vendor_id,
                type,
                title,
                message
            )
            VALUES (%s,%s,%s,%s,%s)
        """, (
            user_id,
            vendor_id,
            notification_type,
            title,
            message
        ))

        mysql.connection.commit()
        cursor.close()

        deliver_later(push_to_customer, user_id, vendor_id, title, message)

    except Exception:
        notify_log.exception("create_notification failed type=%s", notification_type)
        raise

MILK_NOTIFICATION_TITLE = "Milk Collected"


def milk_notification_message(date, slot, milk_type, quantity):
    return f"""Date : {date}
Time : {slot.title()}
Animal : {milk_type.title()}
Quantity : {quantity} L"""


def create_milk_notification(
    user_id,
    vendor_id,
    date,
    slot,
    milk_type,
    quantity
):

    create_notification(
        user_id=user_id,
        vendor_id=vendor_id,
        notification_type="milk",

        title=MILK_NOTIFICATION_TITLE,

        message=milk_notification_message(date, slot, milk_type, quantity)
    )


def create_milk_notifications_bulk(user_id, items):
    """Same rows as create_milk_notification() per item, but one INSERT and
    one commit for the whole batch, then one background push job."""
    rows = [(user_id, it["vendor_id"], "milk", MILK_NOTIFICATION_TITLE,
             milk_notification_message(it["date"], it["slot"], it["milk_type"], it["quantity"]))
            for it in items]
    if not rows:
        return
    cursor = mysql.connection.cursor()
    cursor.executemany("""
        INSERT INTO customer_notifications
        (user_id, vendor_id, type, title, message)
        VALUES (%s,%s,%s,%s,%s)
    """, rows)
    mysql.connection.commit()
    cursor.close()
    deliver_later(push_to_customers, [(r[0], r[1], r[3], r[4]) for r in rows])


def milk_sms_text(date_val, slot_val, milk_type, qty):
    slot_marathi = {"morning": "सकाळ", "evening": "संध्याकाळ"}.get(slot_val.lower(), slot_val)
    milk_marathi = {"cow": "गाय", "buffalo": "म्हैस"}.get(milk_type.lower(), milk_type)
    return f"{date_val} रोजी तुमचे {milk_marathi}चे {slot_marathi}चे {qty} लिटर दूध जमा झाले."


def send_sms_batch(messages):
    """messages: iterable of (phone, text). One background job per batch."""
    for phone, text in messages:
        send_sms(phone, text)

def create_food_sack_notification(
    user_id,
    vendor_id,
    date,
    food_name,
    quantity
):

    create_notification(
        user_id=user_id,
        vendor_id=vendor_id,
        notification_type="food_sack",

        title="Food Sack Issued",

        message=f"""Date : {date}
Food : {food_name}
Quantity : {quantity}"""
    )

def create_advance_notification(
    user_id,
    vendor_id,
    date,
    amount
):

    create_notification(
        user_id=user_id,
        vendor_id=vendor_id,
        notification_type="advance",

        title="Advance Received",

        message=f"""Date : {date}
Amount : ₹{amount}"""
    )

def get_unread_notification_count(user_id, vendor_id):
    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM customer_notifications
        WHERE user_id=%s
        AND vendor_id=%s
        AND is_read=0
    """, (user_id, vendor_id))

    result = cursor.fetchone()
    cursor.close()

    return result["total"] if result else 0
# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = os.getenv("TWILIO_PHONE")

# Email config
EMAIL_ADDRESS = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASS")

# OTP etc
OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES") or 5)
PASSWORD_RESET_EXPIRY_MINUTES = int(os.getenv("PASSWORD_RESET_EXPIRY_MINUTES") or 15)

# Logging (audit)
LOG_FILE = os.getenv("AUDIT_LOG_FILE") or os.path.join(os.getcwd(), "audit.log")
_log_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if os.getenv("LOG_TO_STDOUT", "1") != "0":
    # PaaS platforms (Render/Railway/Heroku) collect stdout; local files are
    # lost on every redeploy.
    _log_handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=_log_handlers,
)


def audit_log(user_id, action, details=""):
    """Append an audit log entry (file-based)."""
    try:
        logging.info(f"user_id={user_id} action={action} details={details}")
    except Exception as e:
        print("Audit logging failed:", e)


# (A duplicate get_today_milk() that used to live here was removed; the
# DairyPro AI tools are defined together further below.)


# ------------------------------
# Helper utilities
# ------------------------------
def send_sms(to, body):
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE:
        logging.warning("Twilio not configured; SMS skipped.")
        return False
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                        http_client=TwilioHttpClient(timeout=EXTERNAL_CALL_TIMEOUT))
        client.messages.create(to=to, from_=TWILIO_PHONE, body=body)
        return True
    except Exception:
        logging.exception("SMS sending failed")
        return False


BREVO_API_KEY = os.getenv("BREVO_API_KEY")
# Must be a sender verified in Brevo (unchanged default keeps existing setups working).
EMAIL_SENDER_ADDRESS = os.getenv("EMAIL_SENDER_ADDRESS", "dairymitra.official@gmail.com")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "dairymitra.official@gmail.com")

def send_email(to, subject, body):

    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = BREVO_API_KEY

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": to}],
        sender={"email": EMAIL_SENDER_ADDRESS, "name": BRAND_NAME},
        subject=subject,
        text_content=body
    )

    try:
        api_instance.send_transac_email(email, _request_timeout=EXTERNAL_CALL_TIMEOUT)
        return True
    except Exception:
        # ApiException, or a network timeout: the caller shows a retry message.
        notify_log.exception("email sending failed")
        return False


def generate_otp(length=6):
    return ''.join(secrets.choice(string.digits) for _ in range(length))


def ensure_user_id_int():
    """
    Ensure session['id'] is an int (fix bytes->int or string).
    Call near start of requests where needed.
    """
    uid = session.get('id')
    if uid is None:
        return None
    try:
        if isinstance(uid, bytes):
            uid = int(uid.decode())
        else:
            uid = int(uid)
        session['id'] = uid
        return uid
    except Exception:
        return session.get('id')


# ------------------------------
# Auth routes (unchanged behavior except ensuring int user_id)
# ------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():

    # Auto-redirect already logged-in owner/staff
    if session.get('loggedin') and session.get('role') in ('owner', 'staff'):
        return redirect(url_for('dashboard'))

    if request.method == 'POST':

        email = (request.form.get('email') or '').strip()
        password = request.form.get('password') or ''

        # Only FAILED attempts count, so owners using several devices are
        # never locked out, while password guessing is.
        login_rules = [
            ("login_fail_ip", client_ip(), 30, 900),
            ("login_fail_email", email.lower(), 10, 900),
        ]
        if failures_blocked(login_rules):
            flash(RATE_LIMIT_MESSAGE, 'danger')
            return render_template('auth/login.html'), 429

        cursor = SafeCursor(mysql.connection.cursor())

        # ==========================================================
        # OWNER LOGIN
        # ==========================================================
        cursor.execute(
            'SELECT * FROM users WHERE email = %s',
            (email,)
        )

        account = cursor.fetchone()

        if account and account.get("is_verified") and check_password_hash(
            account['password'],
            password
        ):

            session.clear()
            session.permanent = True

            session['id'] = int(account['id'])
            session['loggedin'] = True
            session['role'] = "owner"
            session['email'] = account['email']
            session['dairy_name'] = account.get('dairy_name')
            apply_account_language(account.get('preferred_language'))

            cursor.close()

            # Free trial starts at the first successful login (server time;
            # a later login never restarts it).
            try:
                ensure_trial(int(account['id']), account['email'])
            except Exception:
                logging.exception("trial start failed user_id=%s", account['id'])

            audit_log(account['id'], 'owner_login', '')

            flash('Logged in successfully!', 'success')

            return redirect(url_for('dashboard'))

        # ==========================================================
        # STAFF LOGIN (owner approves with an emailed OTP)
        # ==========================================================
        cursor.execute(
            """
            SELECT *
            FROM staff
            WHERE email=%s
            AND is_active=1
            """,
            (email,)
        )

        staff = cursor.fetchone()

        if staff and check_password_hash(
            staff['password'],
            password
        ):

            cursor.execute(
                """
                SELECT id, email, dairy_name
                FROM users
                WHERE id=%s
                """,
                (staff['owner_id'],)
            )

            owner = cursor.fetchone()
            cursor.close()

            if not owner:
                flash('Owner account not found.', 'danger')
                return redirect(url_for('login'))

            # The OTP is stored hashed in dm_challenges. Only the opaque
            # challenge id goes into the session cookie (which is signed
            # but NOT encrypted, so it must never contain the OTP).
            challenge_id, otp = create_challenge(
                "staff_login",
                f"staff:{staff['id']}",
                {"staff_id": int(staff['id']), "owner_id": int(staff['owner_id'])},
                OTP_EXPIRY_MINUTES * 60,
            )

            session.clear()
            session.permanent = True
            session['staff_login_pending'] = True
            session['staff_challenge_id'] = challenge_id

            email_sent = send_email(
                owner['email'],
                'DairyPro - Staff Login OTP',
                f"""
DairyPro Staff Login Verification

A staff member is trying to login to your DairyPro account.

Staff Email:
{staff['email']}

Vehicle Number:
{staff.get('vehicle_number') or 'Not available'}

Your Staff Login OTP is:

{otp}

This OTP will expire in {OTP_EXPIRY_MINUTES} minutes.

Do not share this OTP with anyone.

If you did not authorize this login, please ignore this email.
"""
            )

            if not email_sent:
                discard_challenge(challenge_id)
                session.clear()
                flash('Unable to send OTP to owner email. Please try again.', 'danger')
                return redirect(url_for('login'))

            flash('OTP has been sent to the owner email. Please enter the OTP.', 'info')
            return redirect(url_for('verify_staff_otp'))

        cursor.close()

        record_failures(login_rules)
        flash('Invalid credentials!', 'danger')

    return render_template('auth/login.html')



@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'success')
    return redirect(url_for('login'))


PASSWORD_PATTERN = r'^(?=.*[A-Za-z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$'
PASSWORD_POLICY_MESSAGE = 'पासवर्ड किमान 8 अक्षरे असावा, त्यात अक्षरे, अंक आणि symbol असणे आवश्यक आहे.'


@app.route('/signup', methods=['GET', 'POST'])
def signup():

    if 'loggedin' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':

        dairy_name = request.form.get('dairy_name', '').strip()
        email = (request.form.get('email') or '').strip()
        password = request.form.get('password') or ''
        confirm_password = request.form.get('confirm_password') or ''
        phone = request.form.get('phone', '').strip()

        if rate_limited([
            ("signup_ip", client_ip(), 10, 3600),
            ("signup_email", email.lower(), 5, 3600),
        ]):
            flash(RATE_LIMIT_MESSAGE, 'danger')
            return redirect(url_for('signup'))

        if not dairy_name or len(dairy_name) > 255:
            flash('कृपया डेअरीचे नाव भरा.', 'danger')
            return redirect(url_for('signup'))

        if not re.match(PASSWORD_PATTERN, password):
            flash(PASSWORD_POLICY_MESSAGE, 'danger')
            return redirect(url_for('signup'))

        if password != confirm_password:
            flash('Passwords do not match!', 'danger')
            return redirect(url_for('signup'))

        if len(email) > 255 or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            flash('Invalid email!', 'danger')
            return redirect(url_for('signup'))

        cursor = SafeCursor(mysql.connection.cursor())

        cursor.execute(
            "SELECT id FROM users WHERE email = %s",
            (email,)
        )

        exists = cursor.fetchone()
        cursor.close()

        if exists:
            flash('Account already exists!', 'warning')
            return redirect(url_for('signup'))

        challenge_id, otp = create_challenge(
            "signup",
            email,
            {
                'email': email,
                'password': generate_password_hash(password),
                'phone': phone[:20],
                'dairy_name': dairy_name,
            },
            OTP_EXPIRY_MINUTES * 60,
        )

        session['signup_challenge_id'] = challenge_id
        session['signup_email'] = email

        if not send_email(email, "तुमचा OTP", f"तुमचा OTP: {otp}"):
            discard_challenge(challenge_id)
            session.pop('signup_challenge_id', None)
            flash('OTP पाठवता आला नाही. कृपया पुन्हा प्रयत्न करा.', 'danger')
            return redirect(url_for('signup'))

        flash('OTP sent. Please verify.', 'info')

        return redirect(url_for('verify_account'))

    return render_template('auth/signup.html')


@app.route('/verify-account', methods=['GET', 'POST'])
def verify_account():
    challenge_id = session.get('signup_challenge_id')
    if not challenge_id:
        flash('Session expired. Signup again.', 'warning')
        return redirect(url_for('signup'))

    if request.method == 'POST':
        if rate_limited([("otp_ip", client_ip(), 30, 900)]):
            flash(RATE_LIMIT_MESSAGE, 'danger')
            return render_template('auth/verify_account.html',
                                   email=session.get('signup_email')), 429

        status, temp = verify_challenge(
            challenge_id, "signup", request.form.get('otp'))

        if status == "invalid":
            flash('Invalid OTP.', 'danger')
            return render_template('auth/verify_account.html',
                                   email=session.get('signup_email'))

        if status != "ok":
            session.pop('signup_challenge_id', None)
            flash(CHALLENGE_MESSAGES[status], 'danger')
            return redirect(url_for('signup'))

        session.pop('signup_challenge_id', None)
        session.pop('signup_email', None)

        try:
            with db_transaction() as cur:
                cur.execute("SELECT id FROM users WHERE email = %s FOR UPDATE",
                            (temp['email'],))
                if cur.fetchone():
                    flash('Account already exists!', 'warning')
                    return redirect(url_for('login'))

                cur.execute("""
                    INSERT INTO users
                    (
                        email,
                        password,
                        phone,
                        dairy_name,
                        is_verified
                    )
                    VALUES
                    (%s, %s, %s, %s, TRUE)
                """, (
                    temp['email'],
                    temp['password'],
                    temp['phone'],
                    temp['dairy_name']
                ))

                user_id = cur.lastrowid
                dairy_code = f"DM{user_id:06d}"

                cur.execute(
                    "UPDATE users SET dairy_code = %s WHERE id = %s",
                    (dairy_code, user_id)
                )
        except MySQLdb.IntegrityError:
            flash('Account already exists!', 'warning')
            return redirect(url_for('login'))

        audit_log(user_id, 'signup', f'dairy_code={dairy_code}')

        flash(
            _t("Account verified successfully! Your Dairy ID is {dairy_code}", dairy_code=dairy_code),
            'success'
        )

        return redirect(url_for('login'))

    return render_template('auth/verify_account.html', email=session.get('signup_email'))


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()

        if rate_limited([
            ("pwreset_ip", client_ip(), 20, 3600),
            ("pwreset_email", email.lower(), 5, 3600),
        ]):
            flash(RATE_LIMIT_MESSAGE, 'danger')
            return render_template('auth/forgot_password.html'), 429

        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute('SELECT id FROM users WHERE email = %s', (email,))
        account = cursor.fetchone()
        cursor.close()
        if account:
            challenge_id, otp = create_challenge(
                "pwreset", email, {"email": email, "user_id": int(account['id'])},
                OTP_EXPIRY_MINUTES * 60)
            if not send_email(email, 'Password reset OTP', f"Your OTP: {otp}"):
                discard_challenge(challenge_id)
                flash('OTP पाठवता आला नाही. कृपया पुन्हा प्रयत्न करा.', 'danger')
                return render_template('auth/forgot_password.html')
            session.pop('otp_verified', None)
            session['reset_email'] = email
            session['reset_challenge_id'] = challenge_id
            flash('OTP sent to email.', 'info')
            return redirect(url_for('verify_reset_otp'))
        else:
            flash('Email not found.', 'danger')
    return render_template('auth/forgot_password.html')


@app.route('/verify-reset-otp', methods=['GET', 'POST'])
def verify_reset_otp():
    if 'reset_email' not in session or 'reset_challenge_id' not in session:
        flash('Session expired.', 'warning')
        return redirect(url_for('forgot_password'))

    email = session['reset_email']
    if request.method == 'POST':
        if rate_limited([("otp_ip", client_ip(), 30, 900)]):
            flash(RATE_LIMIT_MESSAGE, 'danger')
            return render_template('auth/verify_reset_otp.html', email=email), 429

        # Max 5 attempts per OTP (previously unlimited, i.e. brute-forceable).
        status, payload = verify_challenge(
            session.get('reset_challenge_id'), "pwreset", request.form.get('otp'))

        if status == "ok" and payload and payload.get("email") == email:
            session.pop('reset_challenge_id', None)
            session['otp_verified'] = True
            session['reset_user_id'] = payload['user_id']
            session['reset_ok_until'] = int(datetime.now(timezone.utc).timestamp()) + 600
            flash('OTP verified. Set new password.', 'success')
            return redirect(url_for('reset_password'))

        if status == "invalid":
            flash('Invalid or expired OTP.', 'danger')
        else:
            for k in ('reset_challenge_id', 'reset_email'):
                session.pop(k, None)
            flash(CHALLENGE_MESSAGES.get(status, 'Invalid or expired OTP.'),
                  'danger')
            return redirect(url_for('forgot_password'))

    return render_template('auth/verify_reset_otp.html', email=email)

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if (not session.get('otp_verified') or 'reset_user_id' not in session
            or session.get('reset_ok_until', 0) < now_ts):
        for k in ('otp_verified', 'reset_user_id', 'reset_ok_until', 'reset_email'):
            session.pop(k, None)
        flash('Unauthorized access.', 'warning')
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        pwd = request.form.get('password') or ''
        cpwd = request.form.get('confirm_password') or ''
        if pwd != cpwd:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('reset_password'))
        if not re.match(PASSWORD_PATTERN, pwd):
            flash(PASSWORD_POLICY_MESSAGE, 'danger')
            return redirect(url_for('reset_password'))
        hashed = generate_password_hash(pwd)
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute(
            "UPDATE users SET password = %s, otp = NULL, otp_expiry = NULL WHERE id = %s",
            (hashed, session['reset_user_id']))
        cursor.close()
        audit_log(session['reset_user_id'], 'password_reset', '')
        for k in ('otp_verified', 'reset_user_id', 'reset_ok_until', 'reset_email'):
            session.pop(k, None)
        flash('Password updated. Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('auth/reset_password.html')

@app.route('/verify-staff-otp', methods=['GET', 'POST'])
def verify_staff_otp():

    if not session.get('staff_login_pending') or not session.get('staff_challenge_id'):
        flash('Session expired. Please login again.', 'warning')
        return redirect(url_for('login'))

    if request.method == 'POST':

        if rate_limited([("otp_ip", client_ip(), 30, 900)]):
            flash(RATE_LIMIT_MESSAGE, 'danger')
            return render_template('auth/verify_staff_otp.html'), 429

        status, payload = verify_challenge(
            session.get('staff_challenge_id'), "staff_login",
            request.form.get('otp', '').strip())

        if status == "invalid":
            flash('Invalid OTP. Please try again.', 'danger')
            return render_template('auth/verify_staff_otp.html')

        if status != "ok":
            session.clear()
            flash(CHALLENGE_MESSAGES[status], 'danger')
            return redirect(url_for('login'))

        staff_id = payload['staff_id']
        owner_id = payload['owner_id']

        # Re-check the staff account in the database.
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute(
            """
            SELECT id, owner_id, email, vehicle_number, is_active, preferred_language
            FROM staff
            WHERE id=%s
            """,
            (staff_id,)
        )
        staff = cursor.fetchone()
        cursor.close()

        if not staff:
            session.clear()
            flash('Staff account not found.', 'danger')
            return redirect(url_for('login'))

        if int(staff['is_active']) != 1:
            session.clear()
            flash('Your account has been disabled by owner.', 'danger')
            return redirect(url_for('login'))

        if int(staff['owner_id']) != int(owner_id):
            session.clear()
            flash('Invalid staff login session.', 'danger')
            return redirect(url_for('login'))

        # OTP verified: create the real login session.
        session.clear()
        session.permanent = True
        session['loggedin'] = True
        session['role'] = 'staff'
        session['staff_id'] = int(staff['id'])
        session['owner_id'] = int(staff['owner_id'])
        session['vehicle'] = staff['vehicle_number']
        # Existing application uses the owner ID as session['id'].
        session['id'] = int(staff['owner_id'])
        session['email'] = staff['email']
        apply_account_language(staff.get('preferred_language'))

        audit_log(
            staff['id'],
            'staff_login',
            f'OTP verified successfully for owner_id={staff["owner_id"]}'
        )

        flash('Staff login successful!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('auth/verify_staff_otp.html')


@app.before_request
def require_login():
    # (Removed: printing every request's cookies and session to the logs
    # leaked session cookies and OTPs.)

    """
    Basic protection
    + Auto logout disabled staff
    + Role-aware redirect
    + Shared routes support for customer/owner/staff
    """

    # ==========================================================
    # PUBLIC ROUTES
    # ==========================================================

    allowed = {
        'login',
        'customer_login',
        'signup',
        'verify_account',
        'forgot_password',
        'verify_reset_otp',
        'verify_staff_otp',
        'reset_password',
        'static',
        'healthcheck',
        'favicon',
        'service_worker',
        'manifest',
        # DairyPro: language switch, JS dictionary, offline shell page,
        # Razorpay webhook (authenticated by its HMAC signature instead)
        'set_language',
        'i18n_js',
        'offline_page',
        'razorpay_webhook',
        'session_ping',
    }

    # ==========================================================
    # CURRENT ENDPOINT
    # ==========================================================

    endpoint = request.endpoint or ""

    # ==========================================================
    # CUSTOMER AREA
    # ==========================================================

    is_customer_route = (
        endpoint.startswith("customer_")
        or request.path.startswith("/customer")
    )

    # ==========================================================
    # SHARED ROUTES
    #
    # These routes can be accessed by both:
    # Customer + Owner + Staff
    #
    # IMPORTANT:
    # /receipt/<vendor_id> is a shared route.
    # ==========================================================

    is_shared_route = (
        request.path.startswith("/receipt/")
        or endpoint in ("subscribe", "vapid_public_key")
    )

    # ==========================================================
    # LOGIN CHECK
    # ==========================================================

    if request.endpoint and request.endpoint not in allowed:

        # ------------------------------------------------------
        # USER NOT LOGGED IN
        # ------------------------------------------------------

        if 'loggedin' not in session:

            # A background/AJAX request must get a machine-readable 401,
            # never a redirect to the HTML login page: fetch() follows the
            # redirect and sees "200 OK", which the old offline sync took
            # as a successful save and deleted the local copy.
            if wants_json():
                return json_error(
                    "Your session has expired. Please log in again.",
                    401, code="auth_required")

            if is_customer_route:
                return redirect(
                    url_for('customer_login')
                )

            return redirect(
                url_for('login')
            )

        # ------------------------------------------------------
        # CUSTOMER AREA
        # ------------------------------------------------------

        if is_customer_route:

            # Customer is allowed
            if session.get('role') == 'customer':
                pass

            # Owner/Staff trying to access customer-only route
            else:
                return redirect(
                    url_for('login')
                )

        # ------------------------------------------------------
        # SHARED ROUTES
        # ------------------------------------------------------
        #
        # Customer + Owner + Staff are allowed.
        #
        elif is_shared_route:

            if session.get('role') == 'staff':
                blocked = enforce_staff_allowlist()
                if blocked is not None:
                    return blocked

            elif session.get('role') in (
                'customer',
                'owner'
            ):
                pass

            else:
                session.clear()

                flash(
                    "Invalid session. Please login again.",
                    "warning"
                )

                return redirect(
                    url_for('login')
                )

        # ------------------------------------------------------
        # OWNER / STAFF AREA
        # ------------------------------------------------------

        else:

            # Customer is NOT allowed in owner/staff-only routes
            if session.get('role') == 'customer':

                return redirect(
                    url_for('customer_login')
                )

            # Staff may only use the daily-work endpoints (server-side
            # enforcement; previously only the menu was hidden).
            blocked = enforce_staff_allowlist()
            if blocked is not None:
                return blocked

    # ==========================================================
    # NORMALIZE SESSION ID
    # ==========================================================

    if 'id' in session:

        uid = session['id']

        if isinstance(uid, bytes):
            uid = uid.decode()

        try:

            session['id'] = int(uid)

        except Exception:

            session.clear()

            flash(
                "Session expired. Please login again.",
                "danger"
            )

            if is_customer_route:
                return redirect(
                    url_for('customer_login')
                )

            return redirect(
                url_for('login')
            )

    # ==========================================================
    # AUTO LOGOUT DISABLED STAFF
    # ==========================================================

    if session.get("role") == "staff":

        staff_id = session.get("staff_id")

        # ------------------------------------------------------
        # SESSION BROKEN
        # ------------------------------------------------------

        if not staff_id:

            session.clear()

            flash(
                "Session invalid. Please login again.",
                "danger"
            )

            return redirect(
                url_for("login")
            )

        try:

            cursor = SafeCursor(
                mysql.connection.cursor()
            )

            cursor.execute("""
                SELECT id, is_active
                FROM staff
                WHERE id=%s
            """, (staff_id,))

            staff = cursor.fetchone()

            cursor.close()

            # --------------------------------------------------
            # STAFF DELETED
            # --------------------------------------------------

            if not staff:

                session.clear()

                flash(
                    "Account not found.",
                    "danger"
                )

                return redirect(
                    url_for("login")
                )

            # --------------------------------------------------
            # STAFF DISABLED BY OWNER
            # --------------------------------------------------

            if int(staff["is_active"]) == 0:

                session.clear()

                flash(
                    "Your account has been disabled by owner.",
                    "danger"
                )

                return redirect(
                    url_for("login")
                )

        except Exception:

            session.clear()

            flash(
                "Session check failed. Please login again.",
                "danger"
            )

            return redirect(
                url_for("login")
            )


# ==========================================================
# MILK CALCULATION MODE ROUTER (server-side enforcement)
#
# OFF ('general'): existing litre x rate workflow.
# ON  ('fat_snf'): FAT/SNF matrix workflow. Litre-rate collection writes
#   are refused so the same milk cannot be saved in both systems; the
#   litre-rate report pages redirect to their FAT/SNF equivalents
#   (append ?legacy=1 to open the old page for historical records).
# ==========================================================
# ==========================================================================
# FAT/SNF: pure rules (parsing, matrix, money, date/slot, warnings)
# Formerly fat_snf/logic.py. No database access in this section.
# ==========================================================================

IST = ZoneInfo("Asia/Kolkata")
PAISE = Decimal("0.01")
ZERO = Decimal("0.00")

MILK_TYPES = ("cow", "buffalo")
CHART_MILK_TYPES = ("cow", "buffalo", "both")
SLOTS = ("morning", "evening")

# Hard limits (critical constraints). Soft limits live in owner settings.
FAT_LIMITS = (Decimal("0.10"), Decimal("20.00"))
SNF_LIMITS = (Decimal("0.10"), Decimal("20.00"))
RATE_LIMITS = (Decimal("0.00"), Decimal("9999.99"))
LITRE_LIMITS = (Decimal("0.01"), Decimal("9999.99"))
MAX_AXIS_VALUES = 100
MAX_CELLS = 2500
MAX_RANGE_DAYS = 366

SLOT_LABELS_MR = {"morning": "सकाळ", "evening": "संध्याकाळ"}
TYPE_LABELS_MR = {"cow": "गाय", "buffalo": "म्हैस", "both": "गाय व म्हैस"}

# Per-animal standard values and allowed ranges (business rule). Applied to
# every new entry, correction, rate preview and new chart, in the browser
# AND here, so a direct API call cannot bypass it. A "both" chart covers
# the union of the two ranges (each ENTRY is still checked against its own
# animal's range).
ANIMAL_RULES = {
    "cow": {"std_fat": Decimal("3.50"), "std_snf": Decimal("8.50"),
            "fat": (Decimal("2.50"), Decimal("5.00")),
            "snf": (Decimal("8.00"), Decimal("9.00"))},
    "buffalo": {"std_fat": Decimal("6.00"), "std_snf": Decimal("9.00"),
                "fat": (Decimal("5.00"), Decimal("11.00")),
                "snf": (Decimal("8.00"), Decimal("10.50"))},
}
ANIMAL_RULES["both"] = {
    "std_fat": ANIMAL_RULES["cow"]["std_fat"], "std_snf": ANIMAL_RULES["cow"]["std_snf"],
    "fat": (ANIMAL_RULES["cow"]["fat"][0], ANIMAL_RULES["buffalo"]["fat"][1]),
    "snf": (ANIMAL_RULES["cow"]["snf"][0], ANIMAL_RULES["buffalo"]["snf"][1]),
}


def animal_rules_json():
    """For templates/JS: the single source of the per-animal limits."""
    return {t: {"std_fat": str(r["std_fat"]), "std_snf": str(r["std_snf"]),
                "fat_min": str(r["fat"][0]), "fat_max": str(r["fat"][1]),
                "snf_min": str(r["snf"][0]), "snf_max": str(r["snf"][1])}
            for t, r in ANIMAL_RULES.items()}


def check_animal_range(milk_type, fat, snf):
    """Raise ValidationError when FAT/SNF is outside the animal's range."""
    rules = ANIMAL_RULES.get(milk_type)
    if not rules:
        return
    animal = _t(TYPE_LABELS_MR.get(milk_type, milk_type))
    lo, hi = rules["fat"]
    if fat is not None and not lo <= fat <= hi:
        raise ValidationError(_t("{animal} दुधासाठी FAT {min} ते {max} दरम्यान असावा.",
                                 animal=animal, min=f"{lo:.1f}", max=f"{hi:.1f}"), "fat")
    lo, hi = rules["snf"]
    if snf is not None and not lo <= snf <= hi:
        raise ValidationError(_t("{animal} दुधासाठी SNF {min} ते {max} दरम्यान असावा.",
                                 animal=animal, min=f"{lo:.1f}", max=f"{hi:.1f}"), "snf")


class ValidationError(ValueError):
    """A user-facing validation problem. `field` names the offending input."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------
def to_decimal(value, field, message):
    if value is None or isinstance(value, bool):
        raise ValidationError(message, field)
    text = str(value).strip()
    if not text:
        raise ValidationError(message, field)
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValidationError(message, field)
    if not result.is_finite():
        raise ValidationError(message, field)
    return result


def _bounded(value, field, message, limits, places=PAISE):
    result = to_decimal(value, field, message)
    if result != result.quantize(places):
        # 3.555 is not silently turned into 3.56: FAT/SNF are matched exactly.
        raise ValidationError(message, field)
    lo, hi = limits
    if result < lo or result > hi:
        raise ValidationError(message, field)
    return result.quantize(places)


def parse_fat(value):
    return _bounded(value, "fat", "कृपया योग्य FAT मूल्य भरा.", FAT_LIMITS)


def parse_snf(value):
    return _bounded(value, "snf", "कृपया योग्य SNF मूल्य भरा.", SNF_LIMITS)


def parse_litres(value):
    result = to_decimal(value, "litres", "दूधाचे लिटर शून्यापेक्षा जास्त असावे.")
    if result <= 0:
        raise ValidationError("दूधाचे लिटर शून्यापेक्षा जास्त असावे.", "litres")
    return _bounded(value, "litres", "कृपया योग्य लिटर भरा (जास्तीत जास्त 2 दशांश).",
                    LITRE_LIMITS)


def parse_rate(value, field="rate"):
    return _bounded(value, field, "कृपया योग्य दर भरा.", RATE_LIMITS)


def parse_milk_type(value, allowed=MILK_TYPES):
    v = str(value or "").strip().lower()
    if v not in allowed:
        raise ValidationError("कृपया दुधाचा प्रकार निवडा.", "milk_type")
    return v


def parse_slot(value):
    v = str(value or "").strip().lower()
    if v not in SLOTS:
        raise ValidationError("कृपया वेळ (सकाळ/संध्याकाळ) निवडा.", "slot")
    return v


def parse_date(value, field="date"):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError("कृपया योग्य तारीख निवडा.", field)


def parse_date_range(from_value, to_value, max_days=MAX_RANGE_DAYS):
    start = parse_date(from_value, "from_date")
    end = parse_date(to_value, "to_date")
    if start > end:
        raise ValidationError("सुरुवातीची तारीख शेवटच्या तारखेपेक्षा नंतरची असू शकत नाही.",
                              "from_date")
    if (end - start).days + 1 > max_days:
        raise ValidationError(_t("एका वेळी जास्तीत जास्त {max_days} दिवसांचा कालावधी निवडा.", max_days=max_days),
                              "to_date")
    return start, end


# ----------------------------------------------------------------------
# Money
# ----------------------------------------------------------------------
def money(value):
    return Decimal(str(value)).quantize(PAISE, rounding=ROUND_HALF_UP)


def entry_amount(litres, rate):
    return money(Decimal(litres) * Decimal(rate))


def weighted_average(pairs):
    """pairs: iterable of (value, litres). Quantity-weighted average, or None."""
    total_l = ZERO
    total = Decimal(0)
    for value, litres in pairs:
        if value is None or litres is None:
            continue
        total_l += Decimal(litres)
        total += Decimal(value) * Decimal(litres)
    if total_l <= 0:
        return None
    return (total / total_l).quantize(PAISE, rounding=ROUND_HALF_UP)


# ----------------------------------------------------------------------
# Date and slot (Asia/Kolkata)
# ----------------------------------------------------------------------
def now_ist():
    return datetime.now(IST)


def today_ist(now=None):
    return (now or now_ist()).astimezone(IST).date()


def auto_slot(now=None):
    """15:00-23:59 IST -> evening, otherwise morning."""
    hour = (now or now_ist()).astimezone(IST).hour
    return "evening" if 15 <= hour <= 23 else "morning"


def date_range(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ----------------------------------------------------------------------
# Matrix
# ----------------------------------------------------------------------
def axis_values(minimum, maximum, step, label):
    """Inclusive arithmetic sequence of Decimals. The range must divide
    exactly by the step so the chart never has a surprise last value."""
    lo = to_decimal(minimum, f"{label}_min", _t("{label_upper} किमान मूल्य चुकीचे आहे.", label_upper=label.upper()))
    hi = to_decimal(maximum, f"{label}_max", _t("{label_upper} कमाल मूल्य चुकीचे आहे.", label_upper=label.upper()))
    st = to_decimal(step, f"{label}_step", _t("{label_upper} वाढ (step) चुकीची आहे.", label_upper=label.upper()))
    if st <= 0 or st != st.quantize(PAISE):
        raise ValidationError(_t("{label_upper} वाढ (step) 0.01 पेक्षा कमी नसावी.", label_upper=label.upper()),
                              f"{label}_step")
    if lo > hi:
        raise ValidationError(_t("{label_upper} किमान मूल्य कमाल मूल्यापेक्षा जास्त आहे.", label_upper=label.upper()),
                              f"{label}_min")
    span = hi - lo
    if span % st != 0:
        raise ValidationError(
            _t("{label_upper} श्रेणी {lo}–{hi} ही वाढ {st} ने समान भागली जात नाही.", label_upper=label.upper(), lo=lo, hi=hi, st=st),
            f"{label}_max")
    count = int(span / st) + 1
    if count > MAX_AXIS_VALUES:
        raise ValidationError(
            _t("{label_upper} ची जास्तीत जास्त {max_axis_values} मूल्ये असू शकतात.", label_upper=label.upper(), max_axis_values=MAX_AXIS_VALUES),
            f"{label}_max")
    return [(lo + st * i).quantize(PAISE) for i in range(count)]


def generate_matrix(settings):
    """
    settings keys: fat_min, fat_max, fat_step, snf_min, snf_max, snf_step,
                   std_fat, std_snf, std_rate, fat_rate_step, snf_rate_step

    rate(fat, snf) = std_rate
                     + (fat - std_fat) / fat_step * fat_rate_step
                     + (snf - std_snf) / snf_step * snf_rate_step

    A positive *_rate_step raises the rate as FAT/SNF increase; a negative
    one lowers it. Returns (fat_values, snf_values, {(fat, snf): rate}).
    """
    fats = axis_values(settings.get("fat_min"), settings.get("fat_max"),
                       settings.get("fat_step"), "fat")
    snfs = axis_values(settings.get("snf_min"), settings.get("snf_max"),
                       settings.get("snf_step"), "snf")
    if len(fats) * len(snfs) > MAX_CELLS:
        raise ValidationError(_t("चार्टमध्ये जास्तीत जास्त {max_cells} दर असू शकतात.", max_cells=MAX_CELLS))

    std_fat = parse_fat(settings.get("std_fat"))
    std_snf = parse_snf(settings.get("std_snf"))
    std_rate = parse_rate(settings.get("std_rate"), "std_rate")
    fat_inc = to_decimal(settings.get("fat_rate_step"), "fat_rate_step",
                         "FAT प्रति वाढ दरातील बदल चुकीचा आहे.")
    snf_inc = to_decimal(settings.get("snf_rate_step"), "snf_rate_step",
                         "SNF प्रति वाढ दरातील बदल चुकीचा आहे.")
    fat_step = Decimal(str(settings.get("fat_step")))
    snf_step = Decimal(str(settings.get("snf_step")))

    matrix = {}
    for f in fats:
        for s in snfs:
            rate = (std_rate
                    + (f - std_fat) / fat_step * fat_inc
                    + (s - std_snf) / snf_step * snf_inc)
            rate = money(rate)
            if rate < 0:
                raise ValidationError(
                    _t("FAT {f} / SNF {s} साठी दर शून्यापेक्षा कमी येतो. वाढ/श्रेणी तपासा.", f=f, s=s))
            matrix[(f, s)] = rate
    return fats, snfs, matrix


def validate_matrix(fat_values, snf_values, cells):
    """
    Validate a submitted (possibly hand-edited) matrix.
    cells: iterable of {"fat", "snf", "rate"}.
    Returns (fats, snfs, {(fat, snf): rate}) with every combination present.
    """
    if not isinstance(fat_values, list) or not fat_values:
        raise ValidationError("FAT मूल्ये आवश्यक आहेत.")
    if not isinstance(snf_values, list) or not snf_values:
        raise ValidationError("SNF मूल्ये आवश्यक आहेत.")
    if len(fat_values) > MAX_AXIS_VALUES or len(snf_values) > MAX_AXIS_VALUES:
        raise ValidationError(_t("FAT/SNF ची जास्तीत जास्त {max_axis_values} मूल्ये असू शकतात.", max_axis_values=MAX_AXIS_VALUES))
    fats = sorted(parse_fat(v) for v in fat_values)
    snfs = sorted(parse_snf(v) for v in snf_values)
    if len(set(fats)) != len(fats):
        raise ValidationError("FAT मूल्ये पुनरावृत्त आहेत.")
    if len(set(snfs)) != len(snfs):
        raise ValidationError("SNF मूल्ये पुनरावृत्त आहेत.")
    if len(fats) * len(snfs) > MAX_CELLS:
        raise ValidationError(_t("चार्टमध्ये जास्तीत जास्त {max_cells} दर असू शकतात.", max_cells=MAX_CELLS))

    expected = {(f, s) for f in fats for s in snfs}
    if not isinstance(cells, list):
        raise ValidationError("दर तक्ता आवश्यक आहे.")
    matrix = {}
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValidationError("दर तक्त्यातील नोंद चुकीची आहे.")
        f = parse_fat(cell.get("fat"))
        s = parse_snf(cell.get("snf"))
        if (f, s) in matrix:
            raise ValidationError(_t("FAT {f} / SNF {s} दोनदा आले आहे.", f=f, s=s))
        if (f, s) not in expected:
            raise ValidationError(_t("FAT {f} / SNF {s} चार्टच्या श्रेणीबाहेर आहे.", f=f, s=s))
        try:
            matrix[(f, s)] = parse_rate(cell.get("rate"))
        except ValidationError:
            raise ValidationError(_t("FAT {f} / SNF {s} साठी योग्य दर भरा.", f=f, s=s), "rate")
    missing = expected - set(matrix)
    if missing:
        f, s = sorted(missing)[0]
        raise ValidationError(
            _t("{len_missing} दर रिकामे आहेत (उदा. FAT {f} / SNF {s}). सर्व दर भरा.", len_missing=len(missing), f=f, s=s))
    return fats, snfs, matrix


# ----------------------------------------------------------------------
# Soft warnings (never block; the user confirms and saves anyway)
# ----------------------------------------------------------------------
def entry_warnings(litres, recent_litres, thresholds):
    """
    litres: Decimal for the new entry
    recent_litres: list of Decimal from this vendor's recent entries
    thresholds: {"warn_low_litres", "warn_high_litres", "warn_deviation_pct"}
    """
    warnings = []
    low = Decimal(str(thresholds.get("warn_low_litres") or "0"))
    high = Decimal(str(thresholds.get("warn_high_litres") or "0"))
    pct = int(thresholds.get("warn_deviation_pct") or 0)

    if high > 0 and litres > high:
        warnings.append(_t("लिटर ({litres}) नेहमीच्या कमाल मर्यादेपेक्षा ({high}) जास्त आहे.", litres=litres, high=high))
    if low > 0 and litres < low:
        warnings.append(_t("लिटर ({litres}) खूप कमी आहे (मर्यादा {low}).", litres=litres, low=low))

    history = [Decimal(x) for x in recent_litres if x is not None]
    if pct > 0 and len(history) >= 5:
        avg = sum(history) / len(history)
        if avg > 0:
            deviation = abs(litres - avg) / avg * 100
            if deviation > pct:
                warnings.append(
                    _t("या ग्राहकाच्या सरासरी दुधापेक्षा ({money_avg} लि.) {int_deviation}% फरक आहे.", money_avg=money(avg), int_deviation=int(deviation)))
    return warnings


# ==========================================================================
# FAT/SNF: data access, settlement statements
# Formerly fat_snf/service.py.
# ==========================================================================

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
    """Owner settings (calculation mode + warning thresholds).

    Without `cur` the row is read once per request and reused (the calc-mode
    menu, the mode router and the page itself all need it). With `cur` it is
    always re-read inside the caller's transaction. Writers below drop the
    cached copy.
    """
    cache = g.setdefault("_fsn_settings", {}) if cur is None else None
    if cache is not None and user_id in cache:
        return dict(cache[user_id])

    own = cur is None
    cur = cur or db_cursor()

    try:
        cur.execute("""
            SELECT calculation_mode,
                   warn_low_litres,
                   warn_high_litres,
                   warn_deviation_pct,
                   updated_at
            FROM fat_snf_calculation_settings
            WHERE user_id = %s
        """, (user_id,))

        row = cur.fetchone()

        result = {
            "calculation_mode": "general",
            **DEFAULT_THRESHOLDS,
            "updated_at": None,
        }

        if row:
            result.update(row)

        if cache is not None:
            cache[user_id] = dict(result)
        return result

    finally:
        if own:
            cur.close()


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
    with db_transaction() as cur:
        old = get_settings(user_id, cur)["calculation_mode"]
        cur.execute(
            "INSERT INTO fat_snf_calculation_settings (user_id, calculation_mode, changed_by_role) "
            "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE "
            "calculation_mode = VALUES(calculation_mode), "
            "changed_by_role = VALUES(changed_by_role), updated_at = CURRENT_TIMESTAMP",
            (user_id, mode, audit_actor()[0]))
        if old != mode:
            audit_record("calculation_mode_change", "settings", user_id,
                         old={"calculation_mode": old}, new={"calculation_mode": mode},
                         user_id=user_id, cur=cur)
    g.setdefault("_fsn_mode", {})[user_id] = mode
    g.setdefault("_fsn_settings", {}).pop(user_id, None)
    return old, mode


def set_thresholds(user_id, data):
    low = to_decimal(data.get("warn_low_litres"), "warn_low_litres", "किमान लिटर चुकीचे आहे.")
    high = to_decimal(data.get("warn_high_litres"), "warn_high_litres", "कमाल लिटर चुकीचे आहे.")
    try:
        pct = int(str(data.get("warn_deviation_pct")).strip())
    except (TypeError, ValueError):
        raise ValidationError("फरक टक्केवारी चुकीची आहे.", "warn_deviation_pct")
    if low < 0 or high < 0 or (high and low > high) or not 0 <= pct <= 1000:
        raise ValidationError("इशारा मर्यादा तपासा (किमान ≤ कमाल).")
    with db_transaction() as cur:
        old = get_settings(user_id, cur)
        cur.execute(
            "INSERT INTO fat_snf_calculation_settings "
            "(user_id, calculation_mode, warn_low_litres, warn_high_litres, warn_deviation_pct) "
            "VALUES (%s, 'general', %s, %s, %s) ON DUPLICATE KEY UPDATE "
            "warn_low_litres = VALUES(warn_low_litres), warn_high_litres = VALUES(warn_high_litres), "
            "warn_deviation_pct = VALUES(warn_deviation_pct)",
            (user_id, money(low), money(high), pct))
        audit_record("warning_thresholds_change", "settings", user_id,
                     old={k: old[k] for k in DEFAULT_THRESHOLDS},
                     new={"warn_low_litres": low, "warn_high_litres": high,
                          "warn_deviation_pct": pct},
                     user_id=user_id, cur=cur)
    g.setdefault("_fsn_settings", {}).pop(user_id, None)


# ======================================================================
# Vendors
# ======================================================================
def list_vendors(user_id):
    cur = db_cursor()
    cur.execute(
        "SELECT vendor_id, name, milk_type, address, phone FROM vendors "
        "WHERE user_id = %s ORDER BY vendor_id", (user_id,))
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def get_vendor(user_id, vendor_id, cur=None):
    own = cur is None
    cur = cur or db_cursor()
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
    cur = db_cursor()
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
    cur = cur or db_cursor()
    cur.execute(
        "SELECT * FROM fat_snf_charts WHERE id = %s AND user_id = %s"
        + (" FOR UPDATE" if lock else ""), (chart_id, user_id))
    row = cur.fetchone()
    if own:
        cur.close()
    return row


def get_chart_values(chart_id, cur=None):
    own = cur is None
    cur = cur or db_cursor()
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


def validate_chart_payload(data, keep_fats=(), keep_snfs=()):
    """keep_fats/keep_snfs: axis values an existing chart already has; they
    stay editable even if they pre-date the per-animal ranges."""
    if not isinstance(data, dict):
        raise ValidationError("A valid JSON request body is required.")
    name = str(data.get("chart_name") or "").strip()
    if not name or len(name) > 150:
        raise ValidationError("चार्टचे नाव आवश्यक आहे (जास्तीत जास्त 150 अक्षरे).", "chart_name")
    milk_type = parse_milk_type(data.get("milk_type"), CHART_MILK_TYPES)
    effective = parse_date(data.get("effective_from") or data.get("effective_date"),
                             "effective_from")
    fats, snfs, matrix = validate_matrix(data.get("fat_values"), data.get("snf_values"),
                                           data.get("matrix"))
    rules = ANIMAL_RULES[milk_type]
    animal = _t(TYPE_LABELS_MR[milk_type])
    bad_fat = [f for f in fats if not rules["fat"][0] <= f <= rules["fat"][1] and f not in keep_fats]
    if bad_fat:
        raise ValidationError(_t("{animal} चार्टसाठी FAT {min} ते {max} दरम्यान असावा (चुकीचे: {bad}).",
                                 animal=animal, min=f"{rules['fat'][0]:.1f}",
                                 max=f"{rules['fat'][1]:.1f}", bad=bad_fat[0]), "fat_values")
    bad_snf = [x for x in snfs if not rules["snf"][0] <= x <= rules["snf"][1] and x not in keep_snfs]
    if bad_snf:
        raise ValidationError(_t("{animal} चार्टसाठी SNF {min} ते {max} दरम्यान असावा (चुकीचे: {bad}).",
                                 animal=animal, min=f"{rules['snf'][0]:.1f}",
                                 max=f"{rules['snf'][1]:.1f}", bad=bad_snf[0]), "snf_values")
    settings = None
    raw = data.get("settings")
    if isinstance(raw, dict) and all(raw.get(k) not in (None, "") for k in SETTING_KEYS):
        # Generation settings are metadata (the matrix itself is what is
        # validated and stored, including manual edits). Keep them only if
        # they are themselves valid.
        try:
            generate_matrix(raw)
            settings = {k: Decimal(str(raw[k])) for k in SETTING_KEYS}
        except (ValidationError, ArithmeticError):
            settings = None
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
    with db_transaction() as cur:
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
        audit_record("rate_chart_create", "fat_snf_chart", chart_id, new={
            "chart_name": clean["chart_name"], "milk_type": clean["milk_type"],
            "effective_from": clean["effective_from"], "version": version,
            "revised_from": revised_from, "cells": len(clean["matrix"]),
            "settings": clean["settings"]}, user_id=user_id, cur=cur)
    return chart_id


def update_chart(user_id, chart_id, data):
    """Edit name/date/cells. Saved entries keep the rate they were saved
    with (stored per entry), so editing never rewrites history."""
    existing = get_chart_values(chart_id) if get_chart(user_id, chart_id) else []
    clean = validate_chart_payload(
        data, keep_fats={Decimal(v["fat"]) for v in existing},
        keep_snfs={Decimal(v["snf"]) for v in existing})
    with db_transaction() as cur:
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

        audit_record("rate_chart_update", "fat_snf_chart", chart_id,
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
    with db_transaction() as cur:
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
        audit_record("rate_chart_" + ("activate" if active else "deactivate"),
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
            _t("{on_date_strftime_d_m_y} साठी {type_labels_mr_milk_type} दुधाचा सक्रिय दर चार्ट उपलब्ध नाही.", on_date_strftime_d_m_y=on_date.strftime('%d/%m/%Y'), type_labels_mr_milk_type=_t(TYPE_LABELS_MR[milk_type])))
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
            _t("या FAT/SNF संयोजनासाठी दर उपलब्ध नाही (FAT {fat}, SNF {snf}). चार्ट '{chart_chart_name}' मध्ये FAT {r_get_fmin}–{r_get_fmax}, SNF {r_get_smin}–{r_get_smax} आहे.", fat=fat, snf=snf, chart_chart_name=chart['chart_name'], r_get_fmin=r.get('fmin'), r_get_fmax=r.get('fmax'), r_get_smin=r.get('smin'), r_get_smax=r.get('smax')),
            chart_id=chart["id"], chart_name=chart["chart_name"])
    return {"chart_id": chart["id"], "chart_name": chart["chart_name"],
            "chart_value_id": cell["id"], "rate": money(cell["rate"])}


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
    cur = db_cursor()
    cur.execute(sql + " ORDER BY m.slot, m.created_at, m.id", params)
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def get_entry(user_id, entry_id, cur=None, lock=False):
    own = cur is None
    cur = cur or db_cursor()
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
    cur = cur or db_cursor()
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
            _t("हा ग्राहक फक्त {type_labels_mr_allowed_0} दूध देतो.", type_labels_mr_allowed_0=_t(TYPE_LABELS_MR[allowed[0]])), "milk_type")


def recent_litres(cur, user_id, vendor_id, milk_type, limit=20):
    cur.execute(
        "SELECT quantity FROM fat_snf_milk_collection WHERE user_id = %s AND vendor_id = %s "
        "AND milk_type = %s ORDER BY collection_date DESC, id DESC LIMIT %s",
        (user_id, vendor_id, milk_type, limit))
    return [r["quantity"] for r in cur.fetchall()]


def preview_rate(user_id, vendor_id, milk_type, fat, snf, on_date):
    """Rate + amount preview (read only)."""
    cur = db_cursor()
    try:
        vendor = get_vendor(user_id, vendor_id, cur)
        if not vendor:
            raise NotFound("ग्राहक सापडला नाही.")
        _check_type(vendor, milk_type)
        check_animal_range(milk_type, fat, snf)
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
    milk_type = parse_milk_type(data.get("milk_type"))
    fat = parse_fat(data.get("fat"))
    snf = parse_snf(data.get("snf"))
    litres = parse_litres(data.get("litres", data.get("quantity")))
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
    with db_transaction() as cur:
        vendor = get_vendor(user_id, vendor_id, cur)
        if not vendor:
            raise NotFound("ग्राहक सापडला नाही.")
        _check_type(vendor, milk_type)
        check_animal_range(milk_type, fat, snf)

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
        amount = entry_amount(litres, rate_info["rate"])

        warnings = entry_warnings(
            litres, recent_litres(cur, user_id, vendor_id, milk_type), get_settings(user_id))
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
        audit_record("fsn_entry_create", "fat_snf_entry", entry_id, new={
            "vendor_id": vendor_id, "date": on_date, "slot": slot, "milk_type": milk_type,
            "fat": fat, "snf": snf, "litres": litres, "rate": rate_info["rate"],
            "amount": amount, "warnings": warnings or None}, user_id=user_id, cur=cur)
        entry = get_entry(user_id, entry_id, cur)
    return {"entry": entry, "replayed": False, "warnings": warnings}


def resolve_date_slot(data, now=None, allow_backdate=False):
    """
    Both owner and staff may save entries for any past or current date.
    The selected slot is accepted for the selected date.
    Future dates are never allowed.
    """
    now = now or now_ist()
    today = today_ist(now)

    on_date = parse_date(
        data.get("date") or today.isoformat()
    )
    slot = parse_slot(
        data.get("slot") or auto_slot(now)
    )

    if on_date > today:
        raise ValidationError(
            "भविष्यातील तारखेची नोंद करता येणार नाही.",
            "date"
        )

    return on_date, slot


def update_entry(user_id, entry_id, data, is_owner):
    """
    Correction policy: changing FAT/SNF/litres (or date/slot) re-reads the
    rate from the chart that applies on the entry's (new) date. Finalized
    entries can only be corrected by the owner with a reason. Old and new
    values are written to the audit log.
    """
    fat = parse_fat(data.get("fat"))
    snf = parse_snf(data.get("snf"))
    litres = parse_litres(data.get("litres", data.get("quantity")))
    reason = str(data.get("reason") or "").strip()[:255]

    with db_transaction() as cur:
        entry = get_entry(user_id, entry_id, cur, lock=True)
        if not entry:
            raise NotFound("नोंद सापडली नाही.")
        check_animal_range(entry["milk_type"], fat, snf)
        if entry["status"] == "final":
            if not is_owner:
                raise Forbidden("अंतिम झालेली नोंद फक्त मालक दुरुस्त करू शकतो.")
            if not reason:
                raise ValidationError("अंतिम नोंद दुरुस्तीसाठी कारण लिहा.", "reason")

        new_date, new_slot = entry["collection_date"], entry["slot"]
        if data.get("date") or data.get("slot"):
            d = parse_date(data.get("date") or entry["collection_date"])
            s = parse_slot(data.get("slot") or entry["slot"])
            if (d, s) != (entry["collection_date"], entry["slot"]):
                if not is_owner:
                    raise Forbidden("तारीख/वेळ फक्त मालक बदलू शकतो.")
                if d > today_ist():
                    raise ValidationError("भविष्यातील तारीख निवडता येणार नाही.", "date")
                new_date, new_slot = d, s

        expected_version = str(data.get("updated_at") or "").replace(" ", "T")
        current_version = entry["updated_at"].isoformat() if entry["updated_at"] else ""
        if expected_version and current_version != expected_version:
            raise Conflict("stale",
                           "ही नोंद दुसऱ्या वापरकर्त्याने आत्ताच बदलली आहे. "
                           "पान रिफ्रेश करून पुन्हा तपासा.")

        rate_info = find_rate(cur, user_id, entry["milk_type"], fat, snf, new_date)
        amount = entry_amount(litres, rate_info["rate"])
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
        audit_record("fsn_entry_update", "fat_snf_entry", entry_id,
                     old={k: entry[k] for k in ("collection_date", "slot", "fat", "snf",
                                                 "quantity", "rate", "amount", "status")},
                     new={"collection_date": new_date, "slot": new_slot, "fat": fat,
                          "snf": snf, "quantity": litres, "rate": rate_info["rate"],
                          "amount": amount, "reason": reason or None},
                     user_id=user_id, cur=cur)
        updated = get_entry(user_id, entry_id, cur)
    return {"entry": updated, "old_rate": entry["rate"], "old_amount": entry["amount"]}


def fsn_delete_entry(user_id, entry_id, reason):
    reason = str(reason or "").strip()[:255]
    if not reason:
        raise ValidationError("नोंद काढण्याचे कारण लिहा.", "reason")
    with db_transaction() as cur:
        entry = get_entry(user_id, entry_id, cur, lock=True)
        if not entry:
            raise NotFound("नोंद सापडली नाही.")
        cur.execute("DELETE FROM fat_snf_milk_collection WHERE id = %s AND user_id = %s",
                    (entry_id, user_id))
        audit_record("fsn_entry_delete", "fat_snf_entry", entry_id,
                     old={k: entry[k] for k in ("vendor_id", "collection_date", "slot",
                                                 "milk_type", "fat", "snf", "quantity",
                                                 "rate", "amount", "status")},
                     new={"reason": reason}, user_id=user_id, cur=cur)


def finalize(user_id, on_date, slot):
    """Mark every saved entry of a date+slot as final, atomically.
    Re-running is harmless (already-final rows are untouched)."""
    with db_transaction() as cur:
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
            audit_record("fsn_finalize", "fat_snf_batch", f"{on_date}:{slot}",
                         new={"finalized": len(ids), "entries": t["n"], "litres": t["l"],
                              "amount": t["a"]}, user_id=user_id, cur=cur)
    return len(ids)


# ======================================================================
# Period statements (calculation, receipts, reports)
# ======================================================================
def _empty_slot():
    return {"litres": ZERO, "amount": ZERO, "pairs_fat": [], "pairs_snf": [], "count": 0}


def _finish_slot(slot):
    slot["avg_fat"] = weighted_average(slot.pop("pairs_fat"))
    slot["avg_snf"] = weighted_average(slot.pop("pairs_snf"))
    slot["avg_rate"] = (money(slot["amount"] / slot["litres"])
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
    cur = db_cursor()
    cur.execute(sql + " ORDER BY vendor_id, collection_date, slot", params)
    rows = cur.fetchall()
    cur.close()
    return rows


def fsn_amounts_by_vendor(user_id, start, end):
    """{vendor_id: {"cow": (litres, amount), "buffalo": (...)}} — used by the
    existing litre-rate pages so FAT/SNF milk is never left unpaid."""
    cur = db_cursor()
    cur.execute(
        "SELECT vendor_id, milk_type, SUM(quantity) l, SUM(amount) a "
        "FROM fat_snf_milk_collection WHERE user_id = %s AND collection_date BETWEEN %s AND %s "
        "GROUP BY vendor_id, milk_type", (user_id, start, end))
    out = defaultdict(dict)
    for r in cur.fetchall():
        out[int(r["vendor_id"])][r["milk_type"]] = (Decimal(r["l"] or 0), Decimal(r["a"] or 0))
    cur.close()
    return out


def _vendor_filter(column, vendor_ids):
    """(" AND <column> IN (...)", params) for an optional vendor subset, so a
    single-vendor statement does not read every vendor's rows."""
    if not vendor_ids:
        return "", []
    return f" AND {column} IN (" + ",".join(["%s"] * len(vendor_ids)) + ")", list(vendor_ids)


def general_milk_by_vendor(user_id, start, end, vendor_ids=None):
    """Legacy litre-rate milk (milk_collection) priced exactly like the
    existing payment page: rate per (vendor, type, date) via get_vendor_rate."""
    extra, extra_params = _vendor_filter("vendor_id", vendor_ids)
    cur = db_cursor()
    cur.execute(
        "SELECT vendor_id, date, milk_type, SUM(quantity) AS qty FROM milk_collection "
        "WHERE user_id = %s AND date BETWEEN %s AND %s" + extra
        + " GROUP BY vendor_id, date, milk_type",
        [user_id, start, end] + extra_params)
    rows = cur.fetchall()
    cur.close()
    acc = defaultdict(lambda: {"cow": [0.0, 0.0], "buffalo": [0.0, 0.0]})
    for r in rows:
        qty = float(r["qty"] or 0)
        rate = get_vendor_rate(None, r["vendor_id"], r["milk_type"], r["date"], user_id=user_id)
        bucket = acc[int(r["vendor_id"])][r["milk_type"]]
        bucket[0] += qty
        bucket[1] += qty * rate
    return {vid: {t: (money(v[0]), money(v[1])) for t, v in d.items()}
            for vid, d in acc.items()}


def food_by_vendor(user_id, start, end, vendor_ids=None):
    extra, extra_params = _vendor_filter("fs.vendor_id", vendor_ids)
    cur = db_cursor()
    cur.execute(
        "SELECT fs.vendor_id, fs.date, fs.sack_qty, r.name, "
        "COALESCE(fs.sack_rate, r.rate, 0) AS rate, COALESCE(fs.total_cost, 0) AS total "
        "FROM food_sack fs JOIN food_sack_rates r ON r.id = fs.sack_rate_id "
        "WHERE fs.user_id = %s AND fs.date BETWEEN %s AND %s" + extra
        + " ORDER BY fs.vendor_id, fs.date",
        [user_id, start, end] + extra_params)
    out = defaultdict(lambda: {"items": [], "total": ZERO})
    for r in cur.fetchall():
        total = money(r["total"])
        d = out[int(r["vendor_id"])]
        d["items"].append({"date": r["date"], "name": r["name"], "qty": int(r["sack_qty"] or 0),
                           "rate": money(r["rate"]), "total": total})
        d["total"] += total
    cur.close()
    return out


def advance_by_vendor(user_id, start, end, vendor_ids=None):
    """Existing ledger rule: the period deduction is subtracted; the
    remaining balance (all advances - all deductions up to `end`) is shown
    and carried forward, never deducted again."""
    extra, extra_params = _vendor_filter("vendor_id", vendor_ids)
    cur = db_cursor()
    cur.execute(
        "SELECT vendor_id, "
        " COALESCE(SUM(CASE WHEN transaction_type='deduction' AND transaction_date >= %s "
        "   THEN amount ELSE 0 END),0) AS period_deduction, "
        " COALESCE(SUM(CASE WHEN transaction_type='advance' THEN amount ELSE 0 END),0) AS adv, "
        " COALESCE(SUM(CASE WHEN transaction_type='deduction' THEN amount ELSE 0 END),0) AS ded "
        "FROM advance_transactions WHERE user_id = %s AND transaction_date <= %s" + extra
        + " GROUP BY vendor_id", [start, user_id, end] + extra_params)
    out = {}
    for r in cur.fetchall():
        out[int(r["vendor_id"])] = {
            "period_deduction": money(r["period_deduction"]),
            "remaining": max(money(Decimal(r["adv"]) - Decimal(r["ded"])), ZERO),
        }
    cur.close()
    return out


def advance_transactions_for(user_id, vendor_id, start, end):
    cur = db_cursor()
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
    general = general_milk_by_vendor(user_id, start, end, vendor_ids) if vendor_ids else {}
    food = food_by_vendor(user_id, start, end, vendor_ids) if vendor_ids else {}
    adv = advance_by_vendor(user_id, start, end, vendor_ids) if vendor_ids else {}

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
        fsn_total = ZERO
        for t in shown_types:
            slots = types.get(t, {"morning": _empty_slot(), "evening": _empty_slot()})
            all_pairs_fat = slots["morning"]["pairs_fat"] + slots["evening"]["pairs_fat"]
            all_pairs_snf = slots["morning"]["pairs_snf"] + slots["evening"]["pairs_snf"]
            rows = [{"date": d,
                     "morning": index.get((t, d, "morning")),
                     "evening": index.get((t, d, "evening"))}
                    for d in date_range(start, end)]
            total_l = slots["morning"]["litres"] + slots["evening"]["litres"]
            total_a = slots["morning"]["amount"] + slots["evening"]["amount"]
            fsn_total += total_a
            sections.append({
                "type": t, "label": TYPE_LABELS_MR[t], "rows": rows,
                "morning": _finish_slot(slots["morning"]),
                "evening": _finish_slot(slots["evening"]),
                "litres": total_l, "amount": total_a,
                "avg_fat": weighted_average(all_pairs_fat),
                "avg_snf": weighted_average(all_pairs_snf),
            })

        gen = general.get(vid, {})
        general_total = sum((a for _, a in gen.values()), ZERO)
        f = food.get(vid, {"items": [], "total": ZERO})
        a = adv.get(vid, {"period_deduction": ZERO, "remaining": ZERO})
        milk_total = fsn_total + general_total
        statements.append({
            "vendor": v,
            "sections": sections,
            "fsn_total": fsn_total,
            "general": {t: gen.get(t, (ZERO, ZERO)) for t in ("cow", "buffalo")},
            "general_total": general_total,
            "milk_total": milk_total,
            "food": f,
            "period_deduction": a["period_deduction"],
            "remaining_advance": a["remaining"],
            "final": money(milk_total - f["total"] - a["period_deduction"]),
            "has_activity": bool(by_vendor.get(vid) or general_total or f["items"]
                                 or a["period_deduction"]),
        })
    return statements


def calculation_rows(user_id, start, end, vendors):
    """Flat rows for the calculation page: one row per vendor x milk type."""
    statements = build_statements(user_id, start, end, vendors)
    totals = {"litres": ZERO, "fsn": ZERO, "general": ZERO, "food": ZERO,
              "deduction": ZERO, "final": ZERO, "cow_l": ZERO, "buffalo_l": ZERO}
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


def fsn_milk_summary(user_id, start, end):
    cur = db_cursor()
    cur.execute(
        "SELECT m.vendor_id, v.name, m.collection_date, m.slot, m.milk_type, m.fat, m.snf, "
        "m.quantity, m.amount FROM fat_snf_milk_collection m "
        "JOIN vendors v ON v.vendor_id = m.vendor_id AND v.user_id = m.user_id "
        "WHERE m.user_id = %s AND m.collection_date BETWEEN %s AND %s",
        (user_id, start, end))
    rows = cur.fetchall()
    cur.close()

    def bucket():
        return {"litres": ZERO, "amount": ZERO, "fat": [], "snf": []}

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
            "cow": bucket(), "buffalo": bucket(), "amount": ZERO})
        vb[r["milk_type"]]["litres"] += r["quantity"]
        vb[r["milk_type"]]["fat"].append((r["fat"], r["quantity"]))
        vb[r["milk_type"]]["snf"].append((r["snf"], r["quantity"]))
        vb["amount"] += r["amount"]

    def fin(b):
        return {"litres": b["litres"], "amount": b["amount"],
                "avg_fat": weighted_average(b["fat"]),
                "avg_snf": weighted_average(b["snf"])}

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
    cur = db_cursor()
    cur.execute(
        "SELECT milk_type, slot, SUM(quantity) l, SUM(amount) a FROM fat_snf_milk_collection "
        "WHERE user_id = %s AND vendor_id = %s AND collection_date BETWEEN %s AND %s "
        "GROUP BY milk_type, slot", (user_id, vendor_id, start, end))
    out = {(r["milk_type"], r["slot"]): (Decimal(r["l"] or 0), Decimal(r["a"] or 0))
           for r in cur.fetchall()}
    cur.close()
    return out


# ==========================================================================
# FAT/SNF: pages and JSON APIs (blueprint "fat_snf")
# Formerly fat_snf/routes.py. The blueprint keeps the "fat_snf.*" endpoint
# names used by templates and the staff allow-list.
# ==========================================================================

fsn_log = logging.getLogger("dairymitr.fat_snf")

fat_snf_bp = Blueprint("fat_snf", __name__)

FSN_PAGE_SIZE = 50
FSN_MODE_OFF_MESSAGE = ("FAT/SNF पद्धत सध्या बंद आहे. Settings मध्ये 'Milk Calculation Mode' "
                    "चालू करा.")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def fsn_tenant():
    if not session.get("loggedin") or session.get("role") not in ("owner", "staff"):
        return None
    try:
        uid = int(session.get("id"))
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def fsn_is_owner():
    return session.get("role") == "owner"


def fsn_jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: fsn_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [fsn_jsonable(v) for v in value]
    return value


def fsn_ok(payload=None, http_status=200, **extra):
    body = {"success": True}
    body.update(fsn_jsonable(payload or {}))
    body.update(fsn_jsonable(extra))
    return jsonify(body), http_status


def fsn_api(owner_only=False):
    """Wrap a JSON endpoint: tenant check, owner check, uniform errors."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            uid = fsn_tenant()
            if not uid:
                return json_error("कृपया पुन्हा लॉगिन करा.", 401, code="unauthenticated")
            if owner_only and not fsn_is_owner():
                return json_error(FORBIDDEN_MESSAGE, 403, code="forbidden")
            try:
                return fn(uid, *args, **kwargs)
            except ValidationError as e:
                return json_error(e.message, 400, code="invalid", field=e.field)
            except RateNotFound as e:
                return json_error(e.message, 422, code=e.code, **fsn_jsonable(e.info))
            except Conflict as e:
                return json_error(e.message, 409, code=e.code, **fsn_jsonable(e.info))
            except NotFound as e:
                return json_error(e.message, 404, code="not_found")
            except Forbidden as e:
                return json_error(e.message, 403, code="forbidden")
            except MySQLdb.OperationalError:
                fsn_log.exception("database unavailable")
                return json_error(
                    "डेटाबेसशी संपर्क होऊ शकला नाही. कृपया थोड्या वेळाने पुन्हा प्रयत्न करा.",
                    503, code="db_unavailable")
            except Exception:
                fsn_log.exception("FAT/SNF API failure")
                return json_error(
                    "डेटा सेव्ह करताना अडचण आली. तुमची नोंद पुन्हा तपासा.", 500,
                    code="server_error")
        return wrapper
    return deco


def fsn_page(owner_only=False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            uid = fsn_tenant()
            if not uid:
                return redirect(url_for("login"))
            if owner_only and not fsn_is_owner():
                return owner_required_response()
            return fn(uid, *args, **kwargs)
        return wrapper
    return deco


def fsn_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("A valid JSON request body is required.")
    return data


def fsn_staff_id():
    return session.get("staff_id") if session.get("role") == "staff" else None


def fsn_require_mode_on(uid):
    if not is_fat_snf(uid):
        raise Conflict("mode_off", FSN_MODE_OFF_MESSAGE)


def fsn_range_from_args(default_today=True):
    today = today_ist()
    start = request.args.get("from_date") or (today.isoformat() if default_today else None)
    end = request.args.get("to_date") or start
    return parse_date_range(start, end)


# ======================================================================
# Settings
# ======================================================================
@fat_snf_bp.route("/api/fat_snf/calculation_mode", methods=["GET"])
@fsn_api()
def api_get_mode(uid):
    s = get_settings(uid)
    return fsn_ok(calculation_mode=s["calculation_mode"], settings=s)


@fat_snf_bp.route("/api/fat_snf/calculation_mode", methods=["POST"])
@fsn_api(owner_only=True)
def api_set_mode(uid):
    data = fsn_body()
    mode = str(data.get("calculation_mode", "")).strip().lower()
    old, new = set_mode(uid, mode)
    msg = ("FAT/SNF पद्धत चालू केली." if new == "fat_snf"
           else "FAT/SNF पद्धत बंद केली. लिटर-दर पद्धत चालू आहे.")
    has_chart = any(c["status"] == "active" for c in list_charts(uid))
    return fsn_ok(message=msg, calculation_mode=new, previous_mode=old,
              needs_chart=(new == "fat_snf" and not has_chart))


@fat_snf_bp.route("/api/fat_snf/settings/thresholds", methods=["POST"])
@fsn_api(owner_only=True)
def api_set_thresholds(uid):
    set_thresholds(uid, fsn_body())
    return fsn_ok(message="इशारा मर्यादा सेव्ह झाल्या.")


# ======================================================================
# Rate charts
# ======================================================================
@fat_snf_bp.route("/fat_snf/rate-chart", methods=["GET"])
@fsn_page(owner_only=True)
def charts_page(uid):
    return render_template("fat_snf/rate_chart.html", charts=list_charts(uid),
                           mode=get_mode(uid), type_labels=TYPE_LABELS_MR)


@fat_snf_bp.route("/fat_snf/rate-chart/create", methods=["GET"])
@fsn_page(owner_only=True)
def chart_create_page(uid):
    source = None
    revise_id = request.args.get("revise", type=int)
    copy_id = revise_id or request.args.get("copy", type=int)
    if copy_id:
        source = get_chart(uid, copy_id)
        if not source:
            flash("चार्ट सापडला नाही.", "danger")
            return redirect(url_for("fat_snf.charts_page"))
        source = dict(source, values=get_chart_values(copy_id))
    return render_template("fat_snf/chart_editor.html", chart=None, source=fsn_jsonable(source),
                           revise_id=revise_id, today=today_ist().isoformat())


@fat_snf_bp.route("/fat_snf/rate-chart/<int:chart_id>", methods=["GET"])
@fsn_page(owner_only=True)
def chart_edit_page(uid, chart_id):
    chart = get_chart(uid, chart_id)
    if not chart:
        flash("चार्ट सापडला नाही.", "danger")
        return redirect(url_for("fat_snf.charts_page"))
    chart = dict(chart, values=get_chart_values(chart_id))
    return render_template("fat_snf/chart_editor.html", chart=fsn_jsonable(chart),
                           source=fsn_jsonable(chart), revise_id=None,
                           today=today_ist().isoformat())


@fat_snf_bp.route("/api/fat_snf/charts/preview", methods=["POST"])
@fsn_api(owner_only=True)
def api_chart_preview(uid):
    fats, snfs, matrix = generate_matrix(fsn_body())
    return fsn_ok(fat_values=fats, snf_values=snfs,
              matrix=[{"fat": f, "snf": s, "rate": r} for (f, s), r in sorted(matrix.items())])


@fat_snf_bp.route("/api/fat_snf/charts", methods=["POST"])
@fsn_api(owner_only=True)
def api_chart_create(uid):
    data = fsn_body()
    revise = data.get("revised_from")
    chart_id = create_chart(uid, data, staff_id=None,
                              revised_from=int(revise) if revise else None)
    return fsn_ok(message="FAT/SNF दर चार्ट सेव्ह झाला.", chart_id=chart_id, http_status=201)


@fat_snf_bp.route("/api/fat_snf/charts/<int:chart_id>", methods=["GET"])
@fsn_api(owner_only=True)
def api_chart_get(uid, chart_id):
    chart = get_chart(uid, chart_id)
    if not chart:
        raise NotFound("चार्ट सापडला नाही.")
    return fsn_ok(chart=chart, values=get_chart_values(chart_id))


@fat_snf_bp.route("/api/fat_snf/charts/<int:chart_id>", methods=["PUT"])
@fsn_api(owner_only=True)
def api_chart_update(uid, chart_id):
    stats = update_chart(uid, chart_id, fsn_body())
    msg = "चार्ट अपडेट झाला."
    if stats["entries_using_chart"]:
        msg += (_t(" या चार्टवरील {stats_entries_using_chart} जुन्या नोंदींचे दर बदललेले नाहीत (त्या नोंदीच्या वेळचा दर जतन आहे).", stats_entries_using_chart=stats['entries_using_chart']))
    return fsn_ok(message=msg, **stats)


@fat_snf_bp.route("/api/fat_snf/charts/<int:chart_id>/status", methods=["POST"])
@fsn_api(owner_only=True)
def api_chart_status(uid, chart_id):
    active = bool(fsn_body().get("active"))
    status = set_chart_status(uid, chart_id, active)
    return fsn_ok(message="चार्ट सक्रिय केला." if status == "active" else "चार्ट निष्क्रिय केला.",
              status=status)


# ======================================================================
# Rate lookup
# ======================================================================
@fat_snf_bp.route("/api/fat_snf/rate", methods=["GET"])
@fsn_api()
def api_rate(uid):
    a = request.args
    on_date = parse_date(a.get("date") or today_ist().isoformat())
    info = preview_rate(uid, parse_vendor_id(a.get("vendor_id")),
                          parse_milk_type(a.get("milk_type")),
                          parse_fat(a.get("fat")), parse_snf(a.get("snf")), on_date)
    result = {"rate": info["rate"], "chart_id": info["chart_id"],
              "chart_name": info["chart_name"]}
    if a.get("litres"):
        result["amount"] = entry_amount(parse_litres(a.get("litres")), info["rate"])
    return fsn_ok(**result)


# ======================================================================
# Milk collection
# ======================================================================
@fat_snf_bp.route("/fat_snf/milk_collection", methods=["GET"])
@fsn_page()
def collection_page(uid):
    """Render the simple row-based FAT/SNF collection page."""
    now = now_ist()
    today = today_ist(now)
    default_slot = auto_slot(now)
    # Keep the old page's date/slot query-string behavior, while the save API
    # remains the authority for current-slot/backdated permissions.
    try:
        selected_date = parse_date(request.args.get("date") or today.isoformat())
        if selected_date > today:
            selected_date = today
    except ValidationError:
        selected_date = today
    try:
        selected_slot = parse_slot(request.args.get("slot") or default_slot)
    except ValidationError:
        selected_slot = default_slot
    return render_template(
        "fat_snf/collection.html",
        vendors=fsn_jsonable(list_vendors(uid)),
        today=today.isoformat(),
        slot=selected_slot,
        selected_date=selected_date.isoformat(),
        mode=get_mode(uid),
        is_owner=fsn_is_owner(),
        backdated=(request.args.get("backdated") == "1" and fsn_is_owner()),
        settings=fsn_jsonable(get_settings(uid)),
        slot_labels=SLOT_LABELS_MR,
    )


@fat_snf_bp.route("/api/fat_snf/entries", methods=["GET"])
@fsn_api()
def api_entries(uid):
    on_date = parse_date(request.args.get("date") or today_ist().isoformat())
    slot = request.args.get("slot")
    slot = parse_slot(slot) if slot else None
    rows = list_entries(uid, on_date, slot)
    total_l = sum((r["quantity"] for r in rows), Decimal(0))
    total_a = sum((r["amount"] for r in rows), Decimal(0))
    saved = sum(1 for r in rows if r["status"] == "saved")
    return fsn_ok(entries=rows, totals={"count": len(rows), "litres": total_l, "amount": total_a,
                                    "saved": saved, "final": len(rows) - saved})


@fat_snf_bp.route("/api/fat_snf/entries", methods=["POST"])
@fsn_api()
def api_save_entry(uid):
    fsn_require_mode_on(uid)
    result = save_entry(uid, fsn_body(), staff_id=fsn_staff_id(), is_owner=fsn_is_owner())
    status = 200 if result["replayed"] else 201
    msg = ("ही नोंद आधीच सेव्ह झाली होती." if result["replayed"] else "नोंद सेव्ह झाली.")
    return fsn_ok(message=msg, entry=result["entry"], replayed=result["replayed"], saved=True,
              warnings=result["warnings"], http_status=status)


@fat_snf_bp.route("/api/fat_snf/requests/<request_id>", methods=["GET"])
@fsn_api()
def api_request_status(uid, request_id):
    """Lets a client whose save timed out learn whether it actually landed."""
    rid = _clean_request_id(request_id)
    entry = get_entry_by_request(uid, rid) if rid else None
    return fsn_ok(saved=bool(entry), entry=entry)


@fat_snf_bp.route("/api/fat_snf/entries/<int:entry_id>", methods=["GET"])
@fsn_api()
def api_get_entry(uid, entry_id):
    entry = get_entry(uid, entry_id)
    if not entry:
        raise NotFound("नोंद सापडली नाही.")
    return fsn_ok(entry=entry)


@fat_snf_bp.route("/api/fat_snf/entries/<int:entry_id>", methods=["PUT"])
@fsn_api()
def api_update_entry(uid, entry_id):
    result = update_entry(uid, entry_id, fsn_body(), is_owner=fsn_is_owner())
    return fsn_ok(message="नोंद दुरुस्त झाली.", **result)


@fat_snf_bp.route("/api/fat_snf/entries/<int:entry_id>", methods=["DELETE"])
@fsn_api(owner_only=True)
def api_delete_entry(uid, entry_id):
    fsn_delete_entry(uid, entry_id, (request.get_json(silent=True) or {}).get("reason"))
    return fsn_ok(message="नोंद काढली.")


@fat_snf_bp.route("/api/fat_snf/finalize", methods=["POST"])
@fsn_api()
def api_finalize(uid):
    fsn_require_mode_on(uid)
    data = fsn_body()
    on_date = parse_date(data.get("date"))
    slot = parse_slot(data.get("slot"))
    if on_date > today_ist():
        raise ValidationError("भविष्यातील तारीख निवडता येणार नाही.", "date")
    n = finalize(uid, on_date, slot)
    msg = (_t("{n} नोंदी अंतिम केल्या.", n=n) if n else "अंतिम करण्यासाठी नवीन नोंद नाही.")
    return fsn_ok(message=msg, finalized=n)


# ======================================================================
# Edit entries
# ======================================================================
@fat_snf_bp.route("/fat_snf/edit_entry", methods=["GET"])
@fsn_page()
def edit_entries_page(uid):
    try:
        on_date = parse_date(request.args.get("date") or today_ist().isoformat())
    except ValidationError:
        on_date = today_ist()
    return render_template("fat_snf/edit_entries.html", entries=list_entries(uid, on_date),
                           selected_date=on_date.isoformat(), is_owner=fsn_is_owner(),
                           today=today_ist().isoformat(), slot_labels=SLOT_LABELS_MR,
                           type_labels=TYPE_LABELS_MR)


# ======================================================================
# Calculation (today's / any period) + CSV export
# ======================================================================
@fat_snf_bp.route("/fat_snf/calculation", methods=["GET"], endpoint="calculation")
@fsn_page(owner_only=True)
def fsn_calculation_view(uid):
    try:
        start, end = fsn_range_from_args()
    except ValidationError as e:
        flash(e.message, "danger")
        start = end = today_ist()

    q = (request.args.get("q") or "").strip().lower()
    milk_type = request.args.get("milk_type") or ""
    show_all = request.args.get("all") == "1"
    vendors = list_vendors(uid)
    if q:
        vendors = [v for v in vendors if q in str(v["vendor_id"]) or q in (v["name"] or "").lower()]
    if milk_type in ("cow", "buffalo"):
        vendors = [v for v in vendors if milk_type in vendor_types(v)]

    statements, totals = calculation_rows(uid, start, end, vendors)
    if not show_all:
        statements = [s for s in statements if s["has_activity"]]

    if request.args.get("export") == "csv":
        return _calculation_csv(statements, start, end)

    page_no = max(request.args.get("page", 1, type=int), 1)
    pages = max(math.ceil(len(statements) / FSN_PAGE_SIZE), 1)
    page_no = min(page_no, pages)
    shown = statements[(page_no - 1) * FSN_PAGE_SIZE: page_no * FSN_PAGE_SIZE]
    return render_template(
        "fat_snf/calculation.html", statements=shown, totals=totals, start=start, end=end,
        q=q, milk_type=milk_type, show_all=show_all, page=page_no, pages=pages,
        count=len(statements), mode=get_mode(uid), slot_labels=SLOT_LABELS_MR)


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
                 f"attachment; filename=dairypro_calculation_{start}_{end}.csv"})


# ======================================================================
# Receipt (single vendor or all vendors) and vendor report
# ======================================================================
def parse_customer_selection(text, max_span=5000):
    """'1-10, 15, 17, 36-56' -> set of customer numbers; '' -> None (all).
    Raises ValidationError with a clear message for invalid input."""
    text = str(text or "").strip()
    if not text:
        return None
    if len(text) > 500:
        raise ValidationError(_t("Customer selection is too long."), "customers")
    selected = set()
    for part in re.split(r"[,;\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a < 1 or b < 1:
                raise ValidationError(_t("Customer numbers must be 1 or more."), "customers")
            if a > b:
                raise ValidationError(_t("Invalid range {range}: the first number must be smaller.",
                                         range=part), "customers")
            if b - a > max_span:
                raise ValidationError(_t("Range {range} is too large.", range=part), "customers")
            selected.update(range(a, b + 1))
        elif part.isdigit():
            if int(part) < 1:
                raise ValidationError(_t("Customer numbers must be 1 or more."), "customers")
            selected.add(int(part))
        else:
            raise ValidationError(_t("'{part}' is not a customer number or range (example: 1-10, 15, 17).",
                                     part=part), "customers")
    return selected


def render_receipt(uid, vendor_id=None, base_template="base.html", customer=False):
    """Shared by owners (any vendor / all) and customers (their own vendor)."""
    params = request.form if request.method == "POST" else request.args
    vendors = list_vendors(uid)
    from_date = params.get("from_date") or today_ist().isoformat()
    to_date = params.get("to_date") or from_date
    if not customer:
        vendor_id = params.get("vendor_id") or vendor_id
    detailed = params.get("detail") == "1" and not customer
    customers_text = "" if customer else (params.get("customers") or "").strip()

    statements = []
    error = None
    generated = request.method == "POST" or params.get("generate") == "1"
    if generated:
        try:
            start, end = parse_date_range(from_date, to_date)
            selection = parse_customer_selection(customers_text)
            if vendor_id and str(vendor_id) != "all":
                v = get_vendor(uid, parse_vendor_id(vendor_id))
                if not v:
                    raise ValidationError("ग्राहक सापडला नाही.", "vendor_id")
                selected = [v]
            elif customer:
                raise ValidationError("ग्राहक सापडला नाही.", "vendor_id")
            else:
                selected = vendors
                if selection is not None:
                    selected = [v for v in vendors if int(v["vendor_id"]) in selection]
            statements = build_statements(uid, start, end, selected) if selected else []
            if len(selected) > 1:
                statements = [s for s in statements if s["has_activity"]]
            if detailed:
                for st in statements:
                    st["advance_items"] = advance_transactions_for(
                        uid, st["vendor"]["vendor_id"], start, end)
        except ValidationError as e:
            error = e.message

    return render_template(
        "fat_snf/receipt.html", base_template=base_template, customer=customer,
        vendors=vendors, statements=statements, from_date=from_date, to_date=to_date,
        vendor_id=str(vendor_id or ""), generated=generated, error=error, detailed=detailed,
        customers=customers_text,
        dairy_name=session.get("dairy_name") or "", printed_at=now_ist())


@fat_snf_bp.route("/fat_snf/receipt", methods=["GET", "POST"])
@fsn_page(owner_only=True)
def receipt(uid):
    return render_receipt(uid)


# ======================================================================
# Milk summary
# ======================================================================
@fat_snf_bp.route("/fat_snf/milk_summary", methods=["GET"], endpoint="milk_summary")
@fsn_page()
def fsn_milk_summary_view(uid):
    try:
        start, end = fsn_range_from_args()
    except ValidationError as e:
        flash(e.message, "danger")
        start = end = today_ist()
    return render_template("fat_snf/milk_summary.html", data=fsn_milk_summary(uid, start, end),
                           start=start, end=end, show_money=fsn_is_owner(),
                           type_labels=TYPE_LABELS_MR, slot_labels=SLOT_LABELS_MR)



app.register_blueprint(fat_snf_bp)


def _fat_snf_on():
    if session.get("role") not in ("owner", "staff", "customer"):
        return False
    try:
        return is_fat_snf(int(session.get("id")))
    except (TypeError, ValueError):
        return False


def _range_args(a, start_key, end_key):
    return {"from_date": a.get(start_key) or None, "to_date": a.get(end_key) or None}


_FSN_REDIRECTS = {
    "milk_collection": lambda a: url_for("fat_snf.collection_page"),
    "edit_entry": lambda a: url_for("fat_snf.edit_entries_page"),
    "calculation": lambda a: url_for("fat_snf.calculation"),
    "payment": lambda a: url_for("fat_snf.calculation",
                                 **_range_args(a, "start_date", "end_date")),
    "receipt_all_vendors": lambda a: url_for("fat_snf.receipt"),
    "milk_summary": lambda a: url_for("fat_snf.milk_summary",
                                      **_range_args(a, "from_date", "to_date")),
    "milk_summary_report": lambda a: url_for("fat_snf.calculation",
                                             from_date=a.get("date") or None),
    "vendor_range_summary": lambda a: url_for("fat_snf.milk_summary"),
    "milk_rate": lambda a: url_for("fat_snf.charts_page"),
    "vendor_rate": lambda a: url_for("fat_snf.charts_page"),
}

MODE_ON_COLLECTION_MESSAGE = (
    "FAT/SNF पद्धत चालू आहे. दूध संकलन FAT/SNF पानावरून करा "
    "(लिटर-दर पद्धतीत नवीन नोंद घेता येणार नाही)."
)


@app.before_request
def fat_snf_mode_router():
    if session.get("role") not in ("owner", "staff"):
        return None
    endpoint = request.endpoint or ""

    if endpoint in ("submit_milk_ajax", "submit_bulk_milk_ajax"):
        if _fat_snf_on():
            return jsonify({"success": False, "code": "mode_on",
                            "message": MODE_ON_COLLECTION_MESSAGE}), 409
        return None

    if request.method != "GET" or request.args.get("legacy") == "1":
        return None
    target = _FSN_REDIRECTS.get(endpoint)
    if target and _fat_snf_on():
        return redirect(target(request.args))
    return None


@app.context_processor
def inject_calculation_mode():
    """Templates use `calc_mode` to show the right menu for the active mode."""
    if session.get("role") in ("owner", "staff"):
        try:
            return {"calc_mode": get_mode(int(session.get("id")))}
        except Exception:
            app.logger.exception("calc mode lookup failed")
    return {"calc_mode": "general"}


# ------------------------------
# Dashboard
# ------------------------------
from datetime import date

@app.route("/")
def dashboard():

    # ✅ role-aware entry point (fix for shared PWA start_url issue)
    if "loggedin" not in session:
        flash("Unauthorized request, please log in.", "danger")
        return redirect(url_for("login"))

    if session.get("role") == "customer":
        return redirect(url_for("customer_dashboard"))

    if session.get("role") not in ("owner", "staff"):
        session.clear()
        flash("Session invalid. Please login again.", "danger")
        return redirect(url_for("login"))

    if "id" not in session:
        flash("Unauthorized request, please log in.", "danger")
        return redirect(url_for("login"))

    user_id = session["id"]

    cursor = SafeCursor(mysql.connection.cursor())

    # ✅ आजचं दूध (सकाळ/संध्याकाळ) – DATE() वापरलं
    cursor.execute(
        """
        SELECT slot, SUM(quantity) AS total_quantity
        FROM milk_collection
        WHERE user_id = %s AND date = %s
        GROUP BY slot
        """,
        (user_id, today_ist())
    )
    milk_data = cursor.fetchall()

    morning_milk = 0
    evening_milk = 0
    for row in milk_data:
        slot = row['slot'].lower()   # ✅ case-insensitive compare
        if slot == 'morning':
            morning_milk = row['total_quantity']
        elif slot == 'evening':
            evening_milk = row['total_quantity']

    # ✅ एकूण ग्राहक
    cursor.execute(
        "SELECT COUNT(*) AS total_vendors FROM vendors WHERE user_id = %s",
        (user_id,)
    )
    vendor_count_result = cursor.fetchone()
    total_vendors = vendor_count_result['total_vendors'] if vendor_count_result else 0

    cursor.close()

    return render_template(
        "dashboard.html",
        morning_milk=morning_milk,
        evening_milk=evening_milk,
        total_vendors=total_vendors
    )



# ------------------------------
# Vendors CRUD
# ------------------------------
@app.route('/add_vendor', methods=['GET', 'POST'])
def add_vendor():

    # Plain cursor: SafeCursor turns digit-only strings into integers, which
    # dropped leading zeros from bank account numbers and phone numbers.
    cursor = db_cursor()

    # NEXT AUTO ID suggestion
    cursor.execute("""
        SELECT MAX(vendor_id) AS max_id
        FROM vendors
        WHERE user_id=%s
    """, (session['id'],))
    

    res = cursor.fetchone()

    next_vendor_id = 1
    if res and res['max_id']:
        next_vendor_id = int(res['max_id']) + 1

    if request.method == 'POST':

        name = request.form.get('name')
        name_en = request.form.get('name_en')          # NEW
        vendor_id = int(request.form.get('vendor_id'))
        address = request.form.get('address')
        milk_type = request.form.get('milk_type')
        phone = request.form.get('phone')
        ifsc_code = request.form.get('ifsc_code')
        account_no = request.form.get('account_no')

        # CHECK IF ID EXISTS
        cursor.execute("""
            SELECT 1
            FROM vendors
            WHERE vendor_id=%s AND user_id=%s
        """, (vendor_id, session['id']))

        existing = cursor.fetchone()

        if existing:
            flash(_t("❌ Vendor already exists at ID {vendor_id}", vendor_id=vendor_id), "danger")

            return render_template(
                'vendors/add_vendor.html',
                next_vendor_id=next_vendor_id,
                name=name,
                name_en=name_en,                        # NEW
                address=address,
                milk_type=milk_type,
                phone=phone,
                ifsc_code=ifsc_code,
                account_no=account_no,
                vendor_id=vendor_id
            )

        # INSERT NEW VENDOR
        cursor.execute("""
            INSERT INTO vendors
            (
                vendor_id,
                name,
                name_en,
                address,
                milk_type,
                phone,
                ifsc_code,
                account_no,
                user_id
            )
            VALUES
            (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            vendor_id,
            name,
            name_en,                                    # NEW
            address,
            milk_type,
            phone,
            ifsc_code,
            account_no,
            session['id']
        ))

        mysql.connection.commit()

        flash("✔ Vendor Added Successfully", "success")

        return redirect(url_for('add_vendor'))

    return render_template(
        'vendors/add_vendor.html',
        next_vendor_id=next_vendor_id
    )


@app.route('/edit_vendor/<string:vendor_id>', methods=['GET', 'POST'])
def edit_vendor(vendor_id):

    # Plain cursor (see add_vendor): keeps leading zeros in account_no / phone.
    cursor = db_cursor()

    # Check vendor belongs to logged-in user
    cursor.execute("""
        SELECT 1
        FROM vendors
        WHERE vendor_id=%s AND user_id=%s
    """, (vendor_id, session['id']))

    if not cursor.fetchone():
        flash('Unauthorized or vendor not found.', 'danger')
        return redirect(url_for('vendor_list'))

    if request.method == 'POST':

        name = request.form.get('name')
        name_en = request.form.get('name_en')           # NEW
        address = request.form.get('address')
        milk_type = request.form.get('milk_type')
        phone = request.form.get('phone')
        ifsc_code = request.form.get('ifsc_code')
        account_no = request.form.get('account_no')

        cursor.execute("""
            UPDATE vendors
            SET
                name=%s,
                name_en=%s,
                address=%s,
                milk_type=%s,
                phone=%s,
                ifsc_code=%s,
                account_no=%s
            WHERE
                vendor_id=%s
                AND user_id=%s
        """, (
            name,
            name_en,                                    # NEW
            address,
            milk_type,
            phone,
            ifsc_code,
            account_no,
            vendor_id,
            session['id']
        ))

        mysql.connection.commit()

        audit_log(
            session['id'],
            'edit_vendor',
            f'vendor_id={vendor_id}'
        )

        flash('Vendor updated successfully.', 'success')
        return redirect(url_for('vendor_list'))

    cursor.execute("""
        SELECT *
        FROM vendors
        WHERE
            user_id=%s
            AND vendor_id=%s
    """, (session['id'], vendor_id))

    vendor = cursor.fetchone()

    return render_template(
        'vendors/edit_vendor.html',
        vendor=vendor
    )

@app.route('/vendor_list', methods=['GET'])
def vendor_list():
    search = request.args.get('search', '')
    cursor = SafeCursor(mysql.connection.cursor())

    query = "SELECT * FROM vendors WHERE user_id = %s"
    params = [session['id']]

    if search:
        query += " AND (name LIKE %s OR vendor_id LIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += " ORDER BY vendor_id ASC"

    cursor.execute(query, params)
    vendors = cursor.fetchall()
    return render_template('vendors/vendor_list.html', vendors=vendors)





@app.route('/delete_vendor/<int:vendor_id>', methods=['POST'])
def delete_vendor(vendor_id):

    confirm = request.form.get('confirm')

    if confirm != "1":
        flash("Confirm deletion first","warning")
        return redirect(url_for('vendor_list'))

    cursor = SafeCursor(mysql.connection.cursor())

    # delete vendor
    cursor.execute("""
        DELETE FROM vendors
        WHERE vendor_id=%s AND user_id=%s
    """,(vendor_id,session['id']))

    mysql.connection.commit()
    flash("Vendor Deleted","success")

    return redirect(url_for('vendor_list'))


@app.route('/add_staff', methods=['GET','POST'])
def add_staff():

    if "id" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "owner":
        flash("Only owner can add staff", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":

        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip()
        raw_password = request.form.get("password") or ""
        vehicle = (request.form.get("vehicle") or "").strip()

        if not name or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            flash("कृपया नाव आणि योग्य ई-मेल भरा.", "danger")
            return render_template("staff/add_staff.html")

        if not re.match(PASSWORD_PATTERN, raw_password):
            flash(PASSWORD_POLICY_MESSAGE, "danger")
            return render_template("staff/add_staff.html")

        password = generate_password_hash(raw_password)

        cursor = SafeCursor(mysql.connection.cursor())

        try:
            cursor.execute("""
            INSERT INTO staff
            (owner_id,name,email,password,vehicle_number)
            VALUES (%s,%s,%s,%s,%s)
            """,(session["id"],name,email,password,vehicle))
        except MySQLdb.IntegrityError:
            flash("हा ई-मेल आधीच वापरात आहे.", "danger")
            return render_template("staff/add_staff.html")

        mysql.connection.commit()
        audit_log(session["id"], "staff_add", f"email={email}")

        flash("Staff added successfully","success")

        return redirect(url_for("staff_list"))

    return render_template("staff/add_staff.html")

@app.route('/staff_list')
def staff_list():

    if "id" not in session:
        return redirect(url_for("login"))

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
    SELECT *
    FROM staff
    WHERE owner_id=%s
    ORDER BY id DESC
    """,(session["id"],))

    staff = cursor.fetchall()

    return render_template("staff/staff_list.html",staff=staff)

@app.route('/edit_staff/<int:staff_id>', methods=['GET','POST'])
def edit_staff(staff_id):

    cursor = SafeCursor(mysql.connection.cursor())

    if request.method == "POST":

        name = request.form.get("name")
        email = request.form.get("email")
        vehicle = request.form.get("vehicle")

        try:
            cursor.execute("""
            UPDATE staff
            SET name=%s,email=%s,vehicle_number=%s
            WHERE id=%s AND owner_id=%s
            """,(name,email,vehicle,staff_id,session["id"]))
        except MySQLdb.IntegrityError:
            flash("हा ई-मेल आधीच वापरात आहे.", "danger")
            return redirect(request.url)

        mysql.connection.commit()

        flash("Staff updated successfully","success")

        return redirect(url_for("staff_list"))

    cursor.execute("""
    SELECT *
    FROM staff
    WHERE id=%s AND owner_id=%s
    """,(staff_id,session["id"]))

    staff = cursor.fetchone()

    return render_template("staff/edit_staff.html",staff=staff)


@app.route('/reset_staff_password/<int:staff_id>', methods=['GET','POST'])
def reset_staff_password(staff_id):

    if "id" not in session:
        return redirect(url_for("login"))

    cursor = SafeCursor(mysql.connection.cursor())

    if request.method == "POST":

        password = request.form.get("password")
        confirm = request.form.get("confirm_password")

        if password != confirm:
            flash("Passwords do not match","danger")
            return redirect(request.url)

        if not re.match(PASSWORD_PATTERN, password or ""):
            flash(PASSWORD_POLICY_MESSAGE, "danger")
            return redirect(request.url)

        hashed = generate_password_hash(password)

        cursor.execute("""
        UPDATE staff
        SET password=%s
        WHERE id=%s AND owner_id=%s
        """,(hashed,staff_id,session["id"]))

        mysql.connection.commit()

        flash("Password reset successfully","success")

        return redirect(url_for("staff_list"))

    return render_template("staff/reset_staff_password.html")

@app.route('/disable_staff/<int:staff_id>', methods=['POST'])
def disable_staff(staff_id):

    # login check
    if "id" not in session:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    # only owner allowed
    if session.get("role") != "owner":
        flash("Unauthorized access.", "danger")
        return redirect(url_for("dashboard"))

    cursor = SafeCursor(mysql.connection.cursor())

    # verify staff belongs to this owner
    cursor.execute("""
        SELECT id
        FROM staff
        WHERE id=%s AND owner_id=%s
    """, (staff_id, session["id"]))

    staff = cursor.fetchone()

    if not staff:
        cursor.close()
        flash("Staff not found.", "danger")
        return redirect(url_for("staff_list"))

    # disable
    cursor.execute("""
        UPDATE staff
        SET is_active=0
        WHERE id=%s AND owner_id=%s
    """, (staff_id, session["id"]))

    mysql.connection.commit()
    cursor.close()

    flash("Staff disabled successfully.", "warning")
    return redirect(url_for("staff_list"))

@app.route('/enable_staff/<int:staff_id>', methods=['POST'])
def enable_staff(staff_id):

    # login check
    if "id" not in session:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    # only owner allowed
    if session.get("role") != "owner":
        flash("Unauthorized access.", "danger")
        return redirect(url_for("dashboard"))

    cursor = SafeCursor(mysql.connection.cursor())

    # verify staff belongs to this owner
    cursor.execute("""
        SELECT id
        FROM staff
        WHERE id=%s AND owner_id=%s
    """, (staff_id, session["id"]))

    staff = cursor.fetchone()

    if not staff:
        cursor.close()
        flash("Staff not found.", "danger")
        return redirect(url_for("staff_list"))

    # enable
    cursor.execute("""
        UPDATE staff
        SET is_active=1
        WHERE id=%s AND owner_id=%s
    """, (staff_id, session["id"]))

    mysql.connection.commit()
    cursor.close()

    flash("Staff enabled successfully.", "success")
    return redirect(url_for("staff_list"))


@app.route('/delete_staff/<int:staff_id>', methods=['POST'])
def delete_staff(staff_id):

    # ==========================================================
    # LOGIN CHECK
    # ==========================================================

    if "id" not in session:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))


    # ==========================================================
    # ONLY OWNER CAN DELETE STAFF
    # ==========================================================

    if session.get("role") != "owner":
        flash("Unauthorized access.", "danger")
        return redirect(url_for("dashboard"))


    cursor = SafeCursor(mysql.connection.cursor())


    # ==========================================================
    # VERIFY STAFF BELONGS TO CURRENT OWNER
    # ==========================================================

    cursor.execute("""
        SELECT id, name
        FROM staff
        WHERE id=%s
        AND owner_id=%s
    """, (staff_id, session["id"]))

    staff = cursor.fetchone()


    if not staff:

        cursor.close()

        flash("Staff not found.", "danger")

        return redirect(url_for("staff_list"))


    # ==========================================================
    # DELETE STAFF
    # ==========================================================

    cursor.execute("""
        DELETE FROM staff
        WHERE id=%s
        AND owner_id=%s
    """, (staff_id, session["id"]))


    mysql.connection.commit()

    cursor.close()


    # ==========================================================
    # SUCCESS
    # ==========================================================

    flash("Staff deleted successfully. Staff account access has been removed.", "success")

    return redirect(url_for("staff_list"))


@app.route("/vehicle_milk_report")
def vehicle_milk_report():

    if "id" not in session:
        return redirect(url_for("login"))

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
    SELECT
        s.vehicle_number,
        SUM(m.quantity) AS total_milk
    FROM milk_collection m
    JOIN staff s
        ON m.staff_id = s.id
    WHERE m.user_id=%s
    GROUP BY s.vehicle_number
    ORDER BY total_milk DESC
    """,(session["id"],))

    vehicles = cursor.fetchall()

    return render_template(
        "reports/vehicle_milk_report.html",
        vehicles=vehicles
    )
# ------------------------------
# Milk Rate
# ------------------------------
@app.route('/milk_rate', methods=['GET', 'POST'])
def milk_rate():
    cursor = SafeCursor(mysql.connection.cursor())
    if request.method == 'POST':
        date_from = request.form.get('date')
        rate = float(request.form.get('rate') or 0)
        animal = request.form.get('animal')
        cursor.execute("""
            INSERT INTO milk_rates (user_id, animal, rate, date_from)
            VALUES (%s, %s, %s, %s)
        """, (session['id'], animal, rate, date_from))
        mysql.connection.commit()
        audit_log(session['id'], 'add_milk_rate', f"{animal} {rate} from {date_from}")
        flash('Milk rate added.', 'success')
        return redirect(url_for('milk_rate'))
    cursor.execute("SELECT * FROM milk_rates WHERE user_id = %s ORDER BY date_from DESC", (session['id'],))
    rates = cursor.fetchall()
    return render_template('rates/milk_rate.html', rates=rates)

os.environ["TZ"] = "Asia/Kolkata"

def _auto_slot():
    """System time पाहून default slot ठरवतो (Asia/Kolkata)."""
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if now.hour < 15:  # 00:00 → 14:59
        return "morning"
    return "evening"

def get_vendors_cached(user_id):
    """Vendor list, read at most once per request.

    This used to be a process-wide @lru_cache. cache_clear() only reached the
    gunicorn worker that handled the add/edit/delete, so the other workers
    kept serving the old list (a new vendor missing from milk collection)."""
    cache = g.setdefault("_vendors_by_user", {})
    if user_id not in cache:
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute("""
        SELECT *
        FROM vendors
        WHERE user_id=%s
        ORDER BY vendor_id ASC
        """,(user_id,))
        cache[user_id] = cursor.fetchall()
        cursor.close()
    return cache[user_id]


# ==============================================================================
# OPTIMIZED RATE RESOLUTION
# ------------------------------------------------------------------------------
# BEFORE: get_vendor_rate() ran 1-2 SQL queries EVERY time it was called. It was
#         wrapped in @lru_cache, but since a fresh `cursor` object was passed in
#         on every call, the cache key was never the same twice - so caching
#         never actually happened. In report pages (payment, calculation,
#         receipt_all_vendors) this function is called once per milk row, so a
#         vendor with 60 rows in the date range triggered ~120 extra queries,
#         and a payment report with 50 vendors could trigger thousands of
#         queries -> Gunicorn worker timeouts.
#
# AFTER:  All of a user's vendor-specific rates (vendor_milk_rates) and default
#         rates (milk_rates) are loaded ONCE per request (2 queries total,
#         cached on Flask's `g` request-scoped object) and kept sorted by date.
#         Each call to get_vendor_rate() is then a bisect (O(log n)) lookup in
#         memory - zero additional SQL queries per row/vendor.
#
# Query count for a report with V vendors and R total rate lookups:
#   BEFORE: up to 2 * R database queries
#   AFTER:  2 database queries total (regardless of V or R)
#
# Output/behavior is identical: same "latest date_from <= entry_date" rule,
# same vendor-special-rate-first-then-default fallback, same return values.
# ==============================================================================

def _to_date(d):
    if isinstance(d, bytes):
        d = d.decode()
    if isinstance(d, str):
        return datetime.strptime(d, "%Y-%m-%d").date()
    if isinstance(d, datetime):
        return d.date()
    return d  # already a date object


def _find_latest(struct, entry_date):
    """struct = {'dates': [...ascending...], 'rows': [...]} -> latest row with date <= entry_date"""
    if not struct or not struct['dates']:
        return None
    idx = bisect.bisect_right(struct['dates'], entry_date) - 1
    if idx >= 0:
        return struct['rows'][idx]
    return None


def _load_rate_cache(user_id):
    """Loads & sorts all rate data for this user ONCE per request (cached on flask.g)."""
    if not hasattr(g, '_rate_cache_by_user'):
        g._rate_cache_by_user = {}

    if user_id in g._rate_cache_by_user:
        return g._rate_cache_by_user[user_id]

    cursor = SafeCursor(mysql.connection.cursor())

    # ---- vendor-specific special rates ----
    cursor.execute("""
        SELECT vendor_id, cow_rate, buffalo_rate, date_from
        FROM vendor_milk_rates
        WHERE user_id=%s
        ORDER BY vendor_id ASC, date_from ASC
    """, (user_id,))

    vendor_rows_raw = cursor.fetchall()

    vendor_grouped = {}
    for r in vendor_rows_raw:
        vid = int(r['vendor_id'])
        vendor_grouped.setdefault(vid, []).append(r)

    vendor_rates = {}
    for vid, rows in vendor_grouped.items():
        rows_sorted = sorted(rows, key=lambda r: _to_date(r['date_from']))
        vendor_rates[vid] = {
            'dates': [_to_date(r['date_from']) for r in rows_sorted],
            'rows': rows_sorted
        }

    # ---- default (fallback) rates ----
    cursor.execute("""
        SELECT animal, rate, date_from
        FROM milk_rates
        WHERE user_id=%s
        ORDER BY animal ASC, date_from ASC
    """, (user_id,))

    default_rows_raw = cursor.fetchall()

    default_grouped = {}
    for r in default_rows_raw:
        default_grouped.setdefault(r['animal'], []).append(r)

    default_rates = {}
    for animal, rows in default_grouped.items():
        rows_sorted = sorted(rows, key=lambda r: _to_date(r['date_from']))
        default_rates[animal] = {
            'dates': [_to_date(r['date_from']) for r in rows_sorted],
            'rows': rows_sorted
        }

    cursor.close()

    cache = {'vendor_rates': vendor_rates, 'default_rates': default_rates}
    g._rate_cache_by_user[user_id] = cache
    return cache


def get_vendor_rate(cursor, vendor_id, animal, entry_date, user_id=None):
    """
    Resolves the milk rate for a vendor/animal/date.
    `cursor` is accepted for backward compatibility with existing call sites
    but is no longer used for extra queries - resolution now happens against
    an in-memory, per-request cache (see _load_rate_cache above).
    Return values are identical to the original implementation.
    """
    if user_id is None:
        user_id = int(session.get('id', 0))

    entry_date = _to_date(entry_date)

    cache = _load_rate_cache(user_id)

    vid = int(vendor_id)

    # -------------------------
    # vendor special rate
    # -------------------------
    special = _find_latest(cache['vendor_rates'].get(vid), entry_date)

    if special:

        if animal == "cow" and special.get('cow_rate'):
            return float(special['cow_rate'])

        if animal == "buffalo" and special.get('buffalo_rate'):
            return float(special['buffalo_rate'])

    # -------------------------
    # fallback default rate
    # -------------------------
    default_row = _find_latest(cache['default_rates'].get(animal), entry_date)

    return float(default_row['rate']) if default_row else 0



@app.route('/vendor_rate', methods=['GET', 'POST'])
def vendor_rate():

    cursor = SafeCursor(mysql.connection.cursor())

    # ==========================================================
    # GET ALL VENDORS
    # ==========================================================

    cursor.execute("""
        SELECT vendor_id, name
        FROM vendors
        WHERE user_id=%s
        ORDER BY vendor_id ASC
    """, (session['id'],))

    vendors = cursor.fetchall()

    # ==========================================================
    # POST REQUEST
    # ==========================================================

    if request.method == 'POST':

        action = request.form.get("action")

        # ======================================================
        # DELETE VENDOR SPECIAL RATE
        # ======================================================

        if action == "delete":

            rate_id = request.form.get("rate_id")

            cursor.execute("""
                DELETE FROM vendor_milk_rates
                WHERE id=%s
                AND user_id=%s
            """, (rate_id, session['id']))

            mysql.connection.commit()

            flash("Vendor special rate deleted successfully", "success")

            return redirect(url_for("vendor_rate"))

        # ======================================================
        # ADD VENDOR SPECIAL RATE
        # ======================================================

        vendor_id = request.form.get("vendor_id")
        cow_rate = request.form.get("cow_rate")
        buffalo_rate = request.form.get("buffalo_rate")
        date_from = request.form.get("date")

        cursor.execute("""
            INSERT INTO vendor_milk_rates
            (
                vendor_id,
                user_id,
                cow_rate,
                buffalo_rate,
                date_from
            )
            VALUES(%s, %s, %s, %s, %s)
        """, (
            vendor_id,
            session['id'],
            cow_rate,
            buffalo_rate,
            date_from
        ))

        mysql.connection.commit()

        flash("Vendor special rate saved", "success")

        return redirect(url_for("vendor_rate"))

    # ==========================================================
    # GET ALL VENDOR SPECIAL RATES
    # ==========================================================

    cursor.execute("""
        SELECT
            v.name,
            r.*
        FROM vendor_milk_rates r
        JOIN vendors v
            ON v.vendor_id = r.vendor_id
            AND v.user_id = r.user_id
        WHERE r.user_id=%s
        ORDER BY r.date_from DESC
    """, (session['id'],))

    rates = cursor.fetchall()

    # ==========================================================
    # RENDER PAGE
    # ==========================================================

    return render_template(
        "rates/vendor_rate.html",
        vendors=vendors,
        rates=rates
    )

@app.route('/delete_milk_rate/<int:rate_id>', methods=['POST'])
def delete_milk_rate(rate_id):
    confirm = request.form.get('confirm') or request.args.get('confirm')
    if str(confirm) != '1':
        flash('Please confirm deletion.', 'warning')
        return redirect(url_for('milk_rate'))
    try:
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute("DELETE FROM milk_rates WHERE id = %s AND user_id = %s", (rate_id, session['id']))
        mysql.connection.commit()
        audit_log(session['id'], 'delete_milk_rate', f"id={rate_id}")
        flash('Milk rate deleted.', 'success')
    except Exception:
        logging.exception("Error deleting milk rate")
        flash('Error deleting milk rate.', 'danger')
    return redirect(url_for('milk_rate'))






def _get_date_slot_from_request(req):

    date_val = req.form.get("date") or req.args.get("date")
    slot_val = req.form.get("slot") or req.args.get("slot")

    # IST date (the server itself usually runs in UTC, where date.today()
    # is still "yesterday" between 00:00 and 05:30 IST).
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()

    # अगर date नसली किंवा जुनी असेल → आजची date
    if not date_val or date_val < today:
        date_val = today

    # slot validation
    if slot_val not in ("morning", "evening"):
        slot_val = _auto_slot()

    return date_val, slot_val

# ------------------------------
# Milk Collection (with improved logic)
# ------------------------------

@app.route('/milk_collection', methods=['GET', 'POST'])
def milk_collection():

    vendors = get_vendors_cached(session['id'])

    # GET / POST मधून date आणि slot घ्या
    today_date, current_slot = _get_date_slot_from_request(request)

    if request.method == 'POST' and 'set_date_slot' in request.form:

        date_val = request.form.get('date') or today_ist().isoformat()
        slot_val = request.form.get('slot')

        if slot_val not in ("morning", "evening"):
            slot_val = _auto_slot()

        return redirect(url_for(
            'milk_collection',
            date=date_val,
            slot=slot_val
        ))

    return render_template(
        'milk_operations/milk_collection.html',
        vendors=vendors,
        today_date=today_date,
        current_slot=current_slot,
        selected_date=today_date,
        selected_slot=current_slot
    )

@app.route('/submit_milk_ajax', methods=['POST'])
def submit_milk_ajax():
    """
    Save one litre-rate milk entry.

    Offline-sync contract (also used by the online page):
      * request carries `client_request_id` (UUID) = idempotency key;
        a retry of the same key returns 200 {"saved": true, "replayed": true}
        and never creates a second row;
      * success is ALWAYS {"success": true, "saved": true, ...} with 200;
      * a quantity anomaly is 409 {"code": "needs_confirmation"} (nothing
        saved) until the user re-sends with force_save=true;
      * a different entry for the same vendor/date/slot/type is
        409 {"code": "duplicate"} (never overwritten).
    """
    ensure_user_id_int()
    data = request.get_json(silent=True) or {}
    user_id = int(session['id'])

    try:
        request_id = _clean_request_id(data.get('client_request_id') or data.get('request_id'))
        vendor_id = parse_vendor_id(data.get('vendor_id'))
        milk_type = parse_milk_type(data.get('milk_type'))
        qty = parse_litres(data.get('quantity'))
        date_val, slot_val = resolve_date_slot(
            {"date": data.get('date'), "slot": data.get('slot')})
    except ValidationError as e:
        return jsonify({"success": False, "saved": False, "code": "invalid",
                        "field": e.field, "message": e.message}), 400

    force_save = data.get('force_save') in (True, 1, "1", "true")
    cursor = db_cursor()
    try:
        if request_id:
            cursor.execute(
                "SELECT id FROM milk_collection WHERE user_id=%s AND client_request_id=%s",
                (user_id, request_id))
            row = cursor.fetchone()
            if row:
                return jsonify({"success": True, "saved": True, "replayed": True,
                                "id": int(row['id']),
                                "message": "This entry was already saved."}), 200

        cursor.execute(
            "SELECT phone, milk_type FROM vendors WHERE vendor_id = %s AND user_id = %s",
            (vendor_id, user_id))
        vendor = cursor.fetchone()
        if not vendor:
            return jsonify({"success": False, "saved": False, "code": "vendor_not_found",
                            "message": "Customer not found."}), 404
        allowed = vendor_types(vendor)
        if milk_type not in allowed:
            return jsonify({"success": False, "saved": False, "code": "invalid",
                            "field": "milk_type",
                            "message": "This customer does not supply this milk type."}), 400

        cursor.execute("""
            SELECT id FROM milk_collection
            WHERE vendor_id=%s AND user_id=%s AND date=%s AND slot=%s AND milk_type=%s
        """, (vendor_id, user_id, date_val, slot_val, milk_type))
        if cursor.fetchone():
            return jsonify({"success": False, "saved": False, "code": "duplicate",
                            "message": "Data already exists for this vendor/date/slot/milk_type."}), 409

        # AI anomaly check: a WARNING, never a save.
        if not force_save:
            try:
                cursor.execute("""
                    SELECT quantity FROM milk_collection
                    WHERE vendor_id=%s AND user_id=%s
                    ORDER BY date DESC LIMIT 20
                """, (vendor_id, user_id))
                previous_values = [r['quantity'] for r in cursor.fetchall()]
                if detect_anomaly(previous_values, float(qty)):
                    return jsonify({
                        "success": False, "saved": False, "code": "needs_confirmation",
                        "warning": "असामान्य दूध प्रमाण आढळले. कृपया तपासा.",
                        "message": "असामान्य दूध प्रमाण आढळले. कृपया तपासा.",
                    }), 409
            except Exception:
                logging.exception("AI anomaly detection failed")

        staff_id = session.get("staff_id")
        try:
            cursor.execute("""
                INSERT INTO milk_collection
                (vendor_id, user_id, staff_id, date, slot, milk_type, quantity, client_request_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (vendor_id, user_id, staff_id, date_val, slot_val, milk_type, float(qty),
                  request_id))
        except MySQLdb.IntegrityError as exc:
            if exc.args and exc.args[0] == 1062:
                if request_id:
                    cursor.execute(
                        "SELECT id FROM milk_collection WHERE user_id=%s AND client_request_id=%s",
                        (user_id, request_id))
                    row = cursor.fetchone()
                    if row:
                        return jsonify({"success": True, "saved": True, "replayed": True,
                                        "id": int(row['id']),
                                        "message": "This entry was already saved."}), 200
                return jsonify({"success": False, "saved": False, "code": "duplicate",
                                "message": "Data already exists for this vendor/date/slot/milk_type."}), 409
            raise
        entry_id = cursor.lastrowid
        mysql.connection.commit()
    except Exception:
        logging.exception("Error inserting milk_collection")
        return jsonify({"success": False, "saved": False, "code": "server_error",
                        "message": "Server error saving data."}), 500
    finally:
        cursor.close()

    # The milk is committed: notification problems must not turn this into
    # an error (the client would retry into a duplicate).
    try:
        create_milk_notification(user_id=user_id, vendor_id=vendor_id, date=date_val.isoformat(),
                                 slot=slot_val, milk_type=milk_type, quantity=float(qty))
    except Exception:
        notify_log.exception("milk notification failed")
    audit_log(user_id, 'insert_milk', f"{vendor_id} {date_val} {slot_val} {milk_type} {qty}")
    if vendor.get("phone"):
        deliver_later(send_sms, vendor["phone"],
                      milk_sms_text(date_val.isoformat(), slot_val, milk_type, float(qty)))

    return jsonify({
        "success": True, "saved": True, "replayed": False, "id": int(entry_id),
        "message": _t("Saved for {vendor} ({milk_type})", vendor=vendor_id,
                      milk_type=_t(TYPE_LABELS_MR[milk_type])),
    }), 200


@app.route('/submit_bulk_milk_ajax', methods=['POST'])
def submit_bulk_milk_ajax():
    """
    Expects JSON: { vendors: [{vendor_id, milk_type, quantity, date, slot}, ...], date?, slot? }
    Returns structured JSON: { saved: [...], skipped: [{vendor_id, reason}], message: "..."}

    OPTIMIZATION NOTE:
    BEFORE: for N items this ran up to 4 queries PER item (ownership check,
            duplicate check, insert, phone lookup) = up to 4N queries.
    AFTER:  vendor ownership+phone and existing milk_collection rows for the
            involved dates are prefetched in 2 bulk queries up front, then the
            loop only does an INSERT per saved row (unavoidable, it's a write).
            Query count drops from ~4N to ~(2 + N).
    """
    ensure_user_id_int()
    payload = request.get_json(silent=True) or {}
    vendors_list = payload.get('vendors') or []
    date_default, slot_default = _get_date_slot_from_request(request)
    date_default = payload.get('date') or date_default
    slot_default = payload.get('slot') or slot_default

    if not vendors_list:
        return jsonify({"message": "No vendor data provided."}), 400

    saved = []
    skipped = []

    user_id = session['id']
    cursor = SafeCursor(mysql.connection.cursor())

    # ---- Prefetch vendor ownership + phone for all vendor_ids referenced ----
    vendor_ids = list({str(item.get('vendor_id')) for item in vendors_list if item.get('vendor_id')})
    vendor_map = {}
    if vendor_ids:
        placeholders = ",".join(["%s"] * len(vendor_ids))
        cursor.execute(
            f"SELECT vendor_id, phone FROM vendors WHERE user_id=%s AND vendor_id IN ({placeholders})",
            tuple([user_id] + vendor_ids)
        )
        for row in cursor.fetchall():
            vendor_map[str(row['vendor_id'])] = row

    # ---- Prefetch existing milk_collection rows for involved dates ----
    dates_involved = list({str(item.get('date') or date_default) for item in vendors_list})
    existing_set = set()
    if dates_involved:
        placeholders = ",".join(["%s"] * len(dates_involved))
        cursor.execute(
            f"""SELECT vendor_id, date, slot, milk_type
                FROM milk_collection
                WHERE user_id=%s AND date IN ({placeholders})""",
            tuple([user_id] + dates_involved)
        )
        for row in cursor.fetchall():
            d = row['date']
            dstr = d.strftime("%Y-%m-%d") if hasattr(d, 'strftime') else str(d)
            existing_set.add((str(row['vendor_id']), dstr, row['slot'], row['milk_type']))

    staff_id = session.get("staff_id")
    sms_queue = []

    for item in vendors_list:
        vendor_id = item.get('vendor_id')
        milk_type = item.get('milk_type')
        quantity = item.get('quantity')
        date_val = item.get('date') or date_default
        slot_val = item.get('slot') or slot_default

        if not vendor_id or quantity is None:
            skipped.append({"vendor_id": vendor_id or "unknown", "reason": "missing fields"})
            continue
        try:
            qty = float(parse_litres(quantity))
            milk_type = parse_milk_type(milk_type)
            d_obj, slot_val = resolve_date_slot({"date": date_val, "slot": slot_val})
            date_val = d_obj.isoformat()
        except ValidationError as e:
            skipped.append({"vendor_id": vendor_id, "reason": e.message})
            continue

        # ownership (from prefetched map)
        vendor = vendor_map.get(str(vendor_id))
        if not vendor:
            skipped.append({"vendor_id": vendor_id, "reason": "unauthorized vendor"})
            continue

        # duplicate? (from prefetched set)
        key = (str(vendor_id), str(date_val), slot_val, milk_type)
        if key in existing_set:
            skipped.append({"vendor_id": vendor_id, "reason": "already exists"})
            continue

        # insert
        try:

            cursor.execute("""
            INSERT INTO milk_collection
            (vendor_id, user_id, staff_id, date, slot, milk_type, quantity)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
            vendor_id,
            user_id,
            staff_id,
            date_val,
            slot_val,
            milk_type,
            qty
            ))
            saved.append({"vendor_id": vendor_id, "date": date_val, "slot": slot_val, "milk_type": milk_type, "quantity": qty})
            existing_set.add(key)  # prevent duplicate within the same batch

            # SMS is queued and only sent after the commit below (it used to
            # go out before the data was committed).
            if vendor.get('phone'):
                sms_queue.append((vendor['phone'],
                                  milk_sms_text(date_val, slot_val, milk_type, qty)))

        except Exception:
            logging.exception("Error on bulk insert")
            skipped.append({"vendor_id": vendor_id, "reason": "server error"})
            continue

    mysql.connection.commit()
    if sms_queue:
        deliver_later(send_sms_batch, sms_queue)
    # Create notifications for all successfully saved entries
    try:
        create_milk_notifications_bulk(user_id, saved)
    except Exception:
        # The milk is already saved; a notification failure must not turn
        # the response into an error (the client would retry and get 409s).
        notify_log.exception("bulk milk notifications failed")
    audit_log(user_id, 'bulk_insert_milk', f"saved={len(saved)} skipped={len(skipped)}")
    result = {"saved": saved, "skipped": skipped, "message": f"Saved {len(saved)} entries; skipped {len(skipped)} entries."}
    status = 207 if skipped else 200
    return jsonify(result), status


@app.route('/get_milk_data')
def get_milk_data():

    if "id" not in session:
        return jsonify([])

    date_val = request.args.get("date")
    slot_val = request.args.get("slot")

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT vendor_id, milk_type, quantity
        FROM milk_collection
        WHERE user_id=%s AND date=%s AND slot=%s
    """, (session['id'], date_val, slot_val))

    return jsonify(cursor.fetchall())


from decimal import Decimal, InvalidOperation


# ------------------------------------------------------------------------------
# Helper: Validate money amount
# ------------------------------------------------------------------------------

def _parse_advance_amount(value):
    """
    Convert incoming amount into Decimal safely.

    Returns:
        Decimal amount

    Raises:
        ValueError for invalid/non-positive amount.
    """

    if value is None:
        raise ValueError("Amount is required.")

    value = str(value).strip()

    if not value:
        raise ValueError("Amount is required.")

    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid amount.")

    # Reject NaN / Infinity
    if not amount.is_finite():
        raise ValueError("Invalid amount.")

    # Amount must be positive
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")

    # Maximum supported amount.
    # DECIMAL(12,2) supports up to 9,999,999,999.99
    if amount > Decimal("9999999999.99"):
        raise ValueError("Amount is too large.")

    # Force exactly 2 decimal places.
    return amount.quantize(Decimal("0.01"))


# ------------------------------------------------------------------------------
# Helper: Validate date
# ------------------------------------------------------------------------------

def _parse_advance_date(value):
    """
    Validate YYYY-MM-DD date.
    """

    if not value:
        return today_ist()

    try:
        return datetime.strptime(
            str(value).strip(),
            "%Y-%m-%d"
        ).date()

    except (ValueError, TypeError):
        raise ValueError("Invalid date.")


# ------------------------------------------------------------------------------
# Helper: JSON / normal response
# ------------------------------------------------------------------------------

def _advance_json_error(message, status=400):
    return jsonify({
        "success": False,
        "message": message
    }), status


# ------------------------------------------------------------------------------
# MAIN ADVANCE PAGE
# ------------------------------------------------------------------------------

@app.route('/advance', methods=['GET'])
def advance():

    if 'id' not in session:
        return redirect(url_for('login'))

    user_id = int(session['id'])

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

    # Do NOT load every vendor here.
    #
    # The new UI uses server-side search through /advance/search.
    # This is much faster for large vendor databases.
    #
    # We still provide today's date to the template.

    cursor.close()

    return render_template(
        'milk_operations/advance.html',
        today_date=today_ist().isoformat()
    )


# ------------------------------------------------------------------------------
# SEARCH VENDORS
# ------------------------------------------------------------------------------
#
# Searches by:
#   - vendor ID
#   - vendor name
#
# Prefix search is intentionally used:
#
#   name LIKE 'rah%'
#
# instead of:
#
#   name LIKE '%rah%'
#
# because prefix search can use an index much more efficiently.
#
# The frontend can call this endpoint while typing.
# ------------------------------------------------------------------------------

@app.route('/advance/search', methods=['GET'])
def advance_search():

    if 'id' not in session:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    user_id = int(session['id'])

    search = request.args.get(
        'q',
        ''
    ).strip()

    # Avoid expensive empty search.
    if not search:
        return jsonify({
            "success": True,
            "vendors": []
        })

    # Prevent unnecessarily huge search strings.
    search = search[:100]

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

    # --------------------------------------------------------------------------
    # Numeric vendor ID search
    # --------------------------------------------------------------------------

    if search.isdigit():

        cursor.execute("""
            SELECT
                vendor_id,
                name,
                address,
                milk_type,
                phone
            FROM vendors
            WHERE user_id=%s
              AND vendor_id=%s
            LIMIT 10
        """, (
            user_id,
            int(search)
        ))

        vendors = cursor.fetchall()

    else:

        # ----------------------------------------------------------------------
        # Name prefix search
        # ----------------------------------------------------------------------

        cursor.execute("""
            SELECT
                vendor_id,
                name,
                address,
                milk_type,
                phone
            FROM vendors
            WHERE user_id=%s
              AND name LIKE %s
            ORDER BY name ASC, vendor_id ASC
            LIMIT 10
        """, (
            user_id,
            search + '%'
        ))

        vendors = cursor.fetchall()

    cursor.close()

    return jsonify({
        "success": True,
        "vendors": vendors
    }), 200


# ------------------------------------------------------------------------------
# GET VENDOR ADVANCE SUMMARY + HISTORY
# ------------------------------------------------------------------------------

@app.route('/advance/vendor/<int:vendor_id>', methods=['GET'])
def advance_vendor(vendor_id):

    if 'id' not in session:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    user_id = int(session['id'])

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

    # --------------------------------------------------------------------------
    # Verify vendor belongs to logged-in owner
    # --------------------------------------------------------------------------

    cursor.execute("""
        SELECT
            vendor_id,
            name,
            address,
            milk_type,
            phone
        FROM vendors
        WHERE vendor_id=%s
          AND user_id=%s
        LIMIT 1
    """, (
        vendor_id,
        user_id
    ))

    vendor = cursor.fetchone()

    if not vendor:

        cursor.close()

        return jsonify({
            "success": False,
            "message": "Vendor not found or unauthorized."
        }), 404

    # --------------------------------------------------------------------------
    # One aggregate query for complete balance.
    # --------------------------------------------------------------------------

    cursor.execute("""
        SELECT
            COALESCE(
                SUM(
                    CASE
                        WHEN transaction_type='advance'
                        THEN amount
                        ELSE 0
                    END
                ),
                0
            ) AS total_advance,

            COALESCE(
                SUM(
                    CASE
                        WHEN transaction_type='deduction'
                        THEN amount
                        ELSE 0
                    END
                ),
                0
            ) AS total_deduction,

            COALESCE(
                SUM(
                    CASE
                        WHEN transaction_type='advance'
                        THEN amount
                        WHEN transaction_type='deduction'
                        THEN -amount
                        ELSE 0
                    END
                ),
                0
            ) AS remaining_advance

        FROM advance_transactions
        WHERE vendor_id=%s
          AND user_id=%s
    """, (
        vendor_id,
        user_id
    ))

    summary = cursor.fetchone()

    # --------------------------------------------------------------------------
    # Transaction history.
    #
    # Latest transactions first.
    # Limit prevents huge responses.
    # --------------------------------------------------------------------------

    cursor.execute("""
        SELECT
            id,
            transaction_type,
            amount,
            transaction_date,
            created_at,
            updated_at
        FROM advance_transactions
        WHERE vendor_id=%s
          AND user_id=%s
        ORDER BY transaction_date DESC, id DESC
        LIMIT 200
    """, (
        vendor_id,
        user_id
    ))

    transactions = cursor.fetchall()

    cursor.close()

    # --------------------------------------------------------------------------
    # Convert DB Decimal/date values into JSON-safe values.
    # --------------------------------------------------------------------------

    transaction_list = []

    for row in transactions:

        transaction_list.append({
            "id": int(row["id"]),
            "transaction_type": row["transaction_type"],
            "amount": float(row["amount"]),
            "transaction_date": (
                row["transaction_date"].isoformat()
                if row["transaction_date"]
                else None
            ),
            "created_at": (
                row["created_at"].isoformat()
                if row["created_at"]
                else None
            ),
            "updated_at": (
                row["updated_at"].isoformat()
                if row["updated_at"]
                else None
            )
        })

    return jsonify({
        "success": True,

        "vendor": {
            "vendor_id": int(vendor["vendor_id"]),
            "name": vendor["name"],
            "address": vendor.get("address"),
            "milk_type": vendor.get("milk_type"),
            "phone": vendor.get("phone")
        },

        "summary": {
            "total_advance": float(
                summary["total_advance"] or 0
            ),
            "total_deduction": float(
                summary["total_deduction"] or 0
            ),
            "remaining_advance": float(
                summary["remaining_advance"] or 0
            )
        },

        "transactions": transaction_list

    }), 200


# ------------------------------------------------------------------------------
# ADD NEW ADVANCE
# ------------------------------------------------------------------------------

@app.route('/advance/add', methods=['POST'])
def advance_add():

    if 'id' not in session:
        return _advance_json_error(
            "Please login first.",
            401
        )

    user_id = int(session['id'])

    # --------------------------------------------------------------------------
    # Support JSON and normal form requests.
    # --------------------------------------------------------------------------

    if request.is_json:

        data = request.get_json(
            silent=True
        ) or {}

    else:

        data = request.form

    # --------------------------------------------------------------------------
    # Read values.
    # --------------------------------------------------------------------------

    vendor_id_raw = data.get("vendor_id")
    amount_raw = data.get("amount")
    transaction_date_raw = data.get("date")

    # --------------------------------------------------------------------------
    # Validate vendor ID.
    # --------------------------------------------------------------------------

    try:

        vendor_id = int(
            str(vendor_id_raw).strip()
        )

        if vendor_id <= 0:
            raise ValueError

    except (TypeError, ValueError):

        return _advance_json_error(
            "Invalid vendor."
        )

    # --------------------------------------------------------------------------
    # Validate amount/date.
    # --------------------------------------------------------------------------

    try:

        amount = _parse_advance_amount(
            amount_raw
        )

        transaction_date = _parse_advance_date(
            transaction_date_raw
        )

    except ValueError as e:

        return _advance_json_error(
            str(e)
        )

    # Offline sync / double-click protection: the same key never creates a
    # second advance.
    try:
        request_id = _clean_request_id(
            data.get("client_request_id") or data.get("request_id"))
    except ValidationError as e:
        return _advance_json_error(e.message)

    if transaction_date > today_ist():
        return _advance_json_error("भविष्यातील तारखेची नोंद करता येणार नाही.")

    cursor = db_cursor()

    try:
        # Real transaction: with autocommit ON the FOR UPDATE lock below
        # would otherwise be released immediately.
        mysql.connection.begin()

        # ======================================================================
        # TRANSACTION START
        #
        # Lock the vendor row.
        #
        # This serializes financial mutations for the same vendor.
        # ======================================================================

        cursor.execute("""
            SELECT vendor_id
            FROM vendors
            WHERE vendor_id=%s
              AND user_id=%s
            FOR UPDATE
        """, (
            vendor_id,
            user_id
        ))

        vendor = cursor.fetchone()

        if not vendor:

            mysql.connection.rollback()
            cursor.close()

            return _advance_json_error(
                "Vendor not found or unauthorized.",
                404
            )

        if request_id:
            cursor.execute(
                "SELECT id FROM advance_transactions "
                "WHERE user_id=%s AND client_request_id=%s",
                (user_id, request_id))
            replay = cursor.fetchone()
            if replay:
                mysql.connection.rollback()
                cursor.close()
                return jsonify({
                    "success": True,
                    "saved": True,
                    "replayed": True,
                    "message": "This entry was already saved.",
                    "transaction_id": int(replay['id'])
                }), 200

        # ======================================================================
        # INSERT ADVANCE TRANSACTION
        # ======================================================================

        cursor.execute("""
            INSERT INTO advance_transactions
            (
                vendor_id,
                user_id,
                transaction_type,
                amount,
                transaction_date,
                client_request_id
            )
            VALUES
            (
                %s,
                %s,
                'advance',
                %s,
                %s,
                %s
            )
        """, (
            vendor_id,
            user_id,
            amount,
            transaction_date,
            request_id
        ))

        transaction_id = cursor.lastrowid

        mysql.connection.commit()

        audit_log(
            user_id,
            'advance_add',
            (
                f'vendor_id={vendor_id} '
                f'transaction_id={transaction_id} '
                f'amount={amount} '
                f'date={transaction_date}'
            )
        )

        cursor.close()

        return jsonify({
            "success": True,
            "saved": True,
            "message": "Advance added successfully.",
            "transaction_id": int(transaction_id)
        }), 200

    except Exception as e:

        mysql.connection.rollback()
        cursor.close()

        logging.exception(
            "advance_add failed"
        )

        return _advance_json_error(
            "Unable to add advance. Please try again.",
            500
        )


# ------------------------------------------------------------------------------
# DEDUCT ADVANCE
# ------------------------------------------------------------------------------

@app.route('/advance/deduct', methods=['POST'])
def advance_deduct():

    if 'id' not in session:
        return _advance_json_error(
            "Please login first.",
            401
        )

    user_id = int(session['id'])

    if request.is_json:

        data = request.get_json(
            silent=True
        ) or {}

    else:

        data = request.form

    vendor_id_raw = data.get("vendor_id")
    amount_raw = data.get("amount")
    transaction_date_raw = data.get("date")

    # --------------------------------------------------------------------------
    # Validate vendor.
    # --------------------------------------------------------------------------

    try:

        vendor_id = int(
            str(vendor_id_raw).strip()
        )

        if vendor_id <= 0:
            raise ValueError

    except (TypeError, ValueError):

        return _advance_json_error(
            "Invalid vendor."
        )

    # --------------------------------------------------------------------------
    # Validate amount/date.
    # --------------------------------------------------------------------------

    try:

        amount = _parse_advance_amount(
            amount_raw
        )

        transaction_date = _parse_advance_date(
            transaction_date_raw
        )

    except ValueError as e:

        return _advance_json_error(
            str(e)
        )

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

    try:
        # Real transaction: with autocommit ON the FOR UPDATE lock below
        # would otherwise be released immediately.
        mysql.connection.begin()

        # ======================================================================
        # LOCK VENDOR
        # ======================================================================

        cursor.execute("""
            SELECT vendor_id
            FROM vendors
            WHERE vendor_id=%s
              AND user_id=%s
            FOR UPDATE
        """, (
            vendor_id,
            user_id
        ))

        vendor = cursor.fetchone()

        if not vendor:

            mysql.connection.rollback()
            cursor.close()

            return _advance_json_error(
                "Vendor not found or unauthorized.",
                404
            )

        # ======================================================================
        # CALCULATE CURRENT BALANCE
        #
        # This query uses the indexed:
        # vendor_id + user_id
        #
        # ======================================================================

        cursor.execute("""
            SELECT
                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type='advance'
                            THEN amount
                            WHEN transaction_type='deduction'
                            THEN -amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS remaining_advance
            FROM advance_transactions
            WHERE vendor_id=%s
              AND user_id=%s
        """, (
            vendor_id,
            user_id
        ))

        balance_row = cursor.fetchone()

        current_balance = Decimal(
            str(
                balance_row["remaining_advance"]
                or 0
            )
        )

        # ======================================================================
        # NEVER ALLOW DEDUCTION GREATER THAN AVAILABLE BALANCE
        # ======================================================================

        if amount > current_balance:

            mysql.connection.rollback()
            cursor.close()

            return _advance_json_error(
                (
                    _t("Deduction cannot exceed remaining advance of ₹{current_balance:.2f}.", current_balance=current_balance)
                )
            )

        # ======================================================================
        # INSERT DEDUCTION
        # ======================================================================

        cursor.execute("""
            INSERT INTO advance_transactions
            (
                vendor_id,
                user_id,
                transaction_type,
                amount,
                transaction_date
            )
            VALUES
            (
                %s,
                %s,
                'deduction',
                %s,
                %s
            )
        """, (
            vendor_id,
            user_id,
            amount,
            transaction_date
        ))

        transaction_id = cursor.lastrowid

        # ======================================================================
        # NEW BALANCE
        # ======================================================================

        new_balance = (
            current_balance - amount
        ).quantize(
            Decimal("0.01")
        )

        mysql.connection.commit()

        audit_log(
            user_id,
            'advance_deduction',
            (
                f'vendor_id={vendor_id} '
                f'transaction_id={transaction_id} '
                f'amount={amount} '
                f'date={transaction_date} '
                f'previous_balance={current_balance} '
                f'new_balance={new_balance}'
            )
        )

        cursor.close()

        return jsonify({
            "success": True,
            "message": "Advance deduction saved successfully.",
            "transaction_id": int(transaction_id),
            "remaining_advance": float(new_balance)
        }), 200

    except Exception:

        mysql.connection.rollback()
        cursor.close()

        logging.exception(
            "advance_deduct failed"
        )

        return _advance_json_error(
            "Unable to deduct advance. Please try again.",
            500
        )


# ------------------------------------------------------------------------------
# EDIT ADVANCE TRANSACTION
# ------------------------------------------------------------------------------

@app.route('/advance/edit/<int:transaction_id>', methods=['POST'])
def advance_edit(transaction_id):

    if 'id' not in session:
        return _advance_json_error(
            "Please login first.",
            401
        )

    user_id = int(session['id'])

    if request.is_json:

        data = request.get_json(
            silent=True
        ) or {}

    else:

        data = request.form

    amount_raw = data.get("amount")
    transaction_date_raw = data.get("date")

    try:

        amount = _parse_advance_amount(
            amount_raw
        )

        transaction_date = _parse_advance_date(
            transaction_date_raw
        )

    except ValueError as e:

        return _advance_json_error(
            str(e)
        )

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

    try:
        # Real transaction: with autocommit ON the FOR UPDATE lock below
        # would otherwise be released immediately.
        mysql.connection.begin()

        # ======================================================================
        # GET TRANSACTION + VERIFY OWNERSHIP
        # ======================================================================

        cursor.execute("""
            SELECT
                id,
                vendor_id,
                transaction_type,
                amount,
                transaction_date
            FROM advance_transactions
            WHERE id=%s
              AND user_id=%s
            LIMIT 1
            FOR UPDATE
        """, (
            transaction_id,
            user_id
        ))

        transaction = cursor.fetchone()

        if not transaction:

            mysql.connection.rollback()
            cursor.close()

            return _advance_json_error(
                "Transaction not found or unauthorized.",
                404
            )

        vendor_id = int(
            transaction["vendor_id"]
        )

        transaction_type = transaction[
            "transaction_type"
        ]

        # ======================================================================
        # LOCK VENDOR
        # ======================================================================

        cursor.execute("""
            SELECT vendor_id
            FROM vendors
            WHERE vendor_id=%s
              AND user_id=%s
            FOR UPDATE
        """, (
            vendor_id,
            user_id
        ))

        if not cursor.fetchone():

            mysql.connection.rollback()
            cursor.close()

            return _advance_json_error(
                "Vendor not found or unauthorized.",
                404
            )

        # ======================================================================
        # IF EDITING DEDUCTION:
        #
        # Calculate balance excluding current transaction.
        #
        # This prevents:
        #
        # Advance = 5000
        # Deduction = 3000
        #
        # Editing deduction to 6000.
        #
        # Such edit must be rejected.
        # ======================================================================

        if transaction_type == 'advance':

            # Reducing an advance below what has already been deducted would
            # make the ledger negative (same rule as advance_delete).
            cursor.execute("""
                SELECT COALESCE(SUM(CASE
                    WHEN transaction_type='advance' THEN amount
                    WHEN transaction_type='deduction' THEN -amount
                    ELSE 0 END), 0) AS balance_without_transaction
                FROM advance_transactions
                WHERE vendor_id=%s AND user_id=%s AND id<>%s
            """, (vendor_id, user_id, transaction_id))

            balance_without_transaction = Decimal(
                str(cursor.fetchone()["balance_without_transaction"] or 0)
            )

            if balance_without_transaction + amount < 0:

                mysql.connection.rollback()
                cursor.close()

                return _advance_json_error(
                    _t("हा ॲडव्हान्स एवढा कमी करता येणार नाही, कारण त्यातून आधीच ₹{balance_without_transaction:.2f} वजा झाले आहेत.", balance_without_transaction=-balance_without_transaction)
                )

        if transaction_type == 'deduction':

            cursor.execute("""
                SELECT
                    COALESCE(
                        SUM(
                            CASE
                                WHEN transaction_type='advance'
                                THEN amount
                                WHEN transaction_type='deduction'
                                THEN -amount
                                ELSE 0
                            END
                        ),
                        0
                    ) AS balance_without_transaction
                FROM advance_transactions
                WHERE vendor_id=%s
                  AND user_id=%s
                  AND id<>%s
            """, (
                vendor_id,
                user_id,
                transaction_id
            ))

            balance_row = cursor.fetchone()

            balance_without_transaction = Decimal(
                str(
                    balance_row[
                        "balance_without_transaction"
                    ] or 0
                )
            )

            if amount > balance_without_transaction:

                mysql.connection.rollback()
                cursor.close()

                return _advance_json_error(
                    (
                        _t("Deduction cannot exceed available advance of ₹{balance_without_transaction:.2f}.", balance_without_transaction=balance_without_transaction)
                    )
                )

        # ======================================================================
        # UPDATE TRANSACTION
        # ======================================================================

        cursor.execute("""
            UPDATE advance_transactions
            SET
                amount=%s,
                transaction_date=%s
            WHERE id=%s
              AND user_id=%s
        """, (
            amount,
            transaction_date,
            transaction_id,
            user_id
        ))

        mysql.connection.commit()

        audit_log(
            user_id,
            'advance_edit',
            (
                f'vendor_id={vendor_id} '
                f'transaction_id={transaction_id} '
                f'type={transaction_type} '
                f'new_amount={amount} '
                f'new_date={transaction_date}'
            )
        )

        cursor.close()

        return jsonify({
            "success": True,
            "message": "Advance transaction updated successfully."
        }), 200

    except Exception:

        mysql.connection.rollback()
        cursor.close()

        logging.exception(
            "advance_edit failed"
        )

        return _advance_json_error(
            "Unable to update transaction. Please try again.",
            500
        )


# ------------------------------------------------------------------------------
# DELETE ADVANCE TRANSACTION
# ------------------------------------------------------------------------------

@app.route('/advance/delete/<int:transaction_id>', methods=['POST'])
def advance_delete(transaction_id):

    if 'id' not in session:
        return _advance_json_error(
            "Please login first.",
            401
        )

    user_id = int(session['id'])

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

    try:
        # Real transaction: with autocommit ON the FOR UPDATE lock below
        # would otherwise be released immediately.
        mysql.connection.begin()

        # ======================================================================
        # GET TRANSACTION + OWNERSHIP
        # ======================================================================

        cursor.execute("""
            SELECT
                id,
                vendor_id,
                transaction_type,
                amount,
                transaction_date
            FROM advance_transactions
            WHERE id=%s
              AND user_id=%s
            LIMIT 1
            FOR UPDATE
        """, (
            transaction_id,
            user_id
        ))

        transaction = cursor.fetchone()

        if not transaction:

            mysql.connection.rollback()
            cursor.close()

            return _advance_json_error(
                "Transaction not found or unauthorized.",
                404
            )

        vendor_id = int(
            transaction["vendor_id"]
        )

        transaction_type = transaction[
            "transaction_type"
        ]

        transaction_amount = Decimal(
            str(
                transaction["amount"]
            )
        )

        # ======================================================================
        # LOCK VENDOR
        # ======================================================================

        cursor.execute("""
            SELECT vendor_id
            FROM vendors
            WHERE vendor_id=%s
              AND user_id=%s
            FOR UPDATE
        """, (
            vendor_id,
            user_id
        ))

        if not cursor.fetchone():

            mysql.connection.rollback()
            cursor.close()

            return _advance_json_error(
                "Vendor not found or unauthorized.",
                404
            )

        # ======================================================================
        # IMPORTANT DELETE RULE
        #
        # If deleting an ADVANCE transaction would make the ledger negative,
        # reject the delete.
        #
        # Example:
        #
        # Advance       5000
        # Deduction     4000
        # Remaining     1000
        #
        # Trying to delete the 5000 advance would produce:
        #
        # 0 - 4000 = -4000
        #
        # This must NOT be allowed.
        # ======================================================================

        if transaction_type == 'advance':

            cursor.execute("""
                SELECT
                    COALESCE(
                        SUM(
                            CASE
                                WHEN transaction_type='advance'
                                THEN amount
                                WHEN transaction_type='deduction'
                                THEN -amount
                                ELSE 0
                            END
                        ),
                        0
                    ) AS balance_without_transaction
                FROM advance_transactions
                WHERE vendor_id=%s
                  AND user_id=%s
                  AND id<>%s
            """, (
                vendor_id,
                user_id,
                transaction_id
            ))

            balance_row = cursor.fetchone()

            balance_without_transaction = Decimal(
                str(
                    balance_row[
                        "balance_without_transaction"
                    ] or 0
                )
            )

            if balance_without_transaction < 0:

                mysql.connection.rollback()
                cursor.close()

                return _advance_json_error(
                    (
                        "This advance cannot be deleted because "
                        "existing deductions depend on it."
                    )
                )

        # ======================================================================
        # DELETE
        # ======================================================================

        cursor.execute("""
            DELETE FROM advance_transactions
            WHERE id=%s
              AND user_id=%s
        """, (
            transaction_id,
            user_id
        ))

        mysql.connection.commit()

        audit_log(
            user_id,
            'advance_delete',
            (
                f'vendor_id={vendor_id} '
                f'transaction_id={transaction_id} '
                f'type={transaction_type} '
                f'amount={transaction_amount}'
            )
        )

        cursor.close()

        return jsonify({
            "success": True,
            "message": "Advance transaction deleted successfully."
        }), 200

    except Exception:

        mysql.connection.rollback()
        cursor.close()

        logging.exception(
            "advance_delete failed"
        )

        return _advance_json_error(
            "Unable to delete transaction. Please try again.",
            500
        )





# ==============================================================================
# ADVANCE REPORT
# ==============================================================================

@app.route('/advance/report', methods=['GET'])
def advance_report():

    # --------------------------------------------------------------------------
    # LOGIN CHECK
    # --------------------------------------------------------------------------
    if 'id' not in session:
        return redirect(url_for('login'))

    user_id = int(session['id'])

    return render_template(
        'milk_operations/advance_report.html',
        today_date=today_ist().isoformat()
    )


# ==============================================================================
# ADVANCE REPORT DATA API
# ==============================================================================

@app.route('/advance/report/data', methods=['GET'])
def advance_report_data():

    # --------------------------------------------------------------------------
    # LOGIN CHECK
    # --------------------------------------------------------------------------
    if 'id' not in session:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    user_id = int(session['id'])

    # --------------------------------------------------------------------------
    # GET DATES
    # --------------------------------------------------------------------------
    from_date_raw = request.args.get('from_date', '').strip()
    to_date_raw = request.args.get('to_date', '').strip()

    if not from_date_raw or not to_date_raw:
        return jsonify({
            "success": False,
            "message": "From Date आणि To Date निवडा."
        }), 400

    # --------------------------------------------------------------------------
    # VALIDATE DATES
    # --------------------------------------------------------------------------
    try:

        from_date = datetime.strptime(
            from_date_raw,
            "%Y-%m-%d"
        ).date()

        to_date = datetime.strptime(
            to_date_raw,
            "%Y-%m-%d"
        ).date()

    except ValueError:

        return jsonify({
            "success": False,
            "message": "Invalid date format."
        }), 400

    # --------------------------------------------------------------------------
    # DATE VALIDATION
    # --------------------------------------------------------------------------
    if from_date > to_date:

        return jsonify({
            "success": False,
            "message": "From Date ही To Date पेक्षा मोठी असू शकत नाही."
        }), 400

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

    try:

        # ======================================================================
        # GET ALL ADVANCE TRANSACTIONS
        #
        # Only transaction_type='advance'
        # Only logged-in user's vendors
        # ======================================================================

        cursor.execute("""
            SELECT

                at.id AS transaction_id,

                at.vendor_id,

                v.name AS vendor_name,

                v.phone AS phone,

                v.address AS address,

                v.milk_type AS milk_type,

                at.amount AS advance_amount,

                at.transaction_date,

                at.created_at

            FROM advance_transactions at

            INNER JOIN vendors v
                ON v.vendor_id = at.vendor_id
               AND v.user_id = at.user_id

            WHERE at.user_id = %s

              AND at.transaction_type = 'advance'

              AND at.transaction_date BETWEEN %s AND %s

            ORDER BY
                at.transaction_date DESC,
                at.id DESC

        """, (
            user_id,
            from_date,
            to_date
        ))

        transactions = cursor.fetchall()

        # ======================================================================
        # PREPARE TRANSACTION DATA
        # ======================================================================

        report = []

        total_advance = Decimal("0.00")

        vendor_ids = set()

        for row in transactions:

            amount = Decimal(
                str(row["advance_amount"] or 0)
            )

            total_advance += amount

            vendor_ids.add(
                int(row["vendor_id"])
            )

            report.append({
                "transaction_id": int(
                    row["transaction_id"]
                ),

                "vendor_id": int(
                    row["vendor_id"]
                ),

                "vendor_name": row["vendor_name"] or "",

                "phone": row["phone"] or "",

                "address": row["address"] or "",

                "milk_type": row["milk_type"] or "",

                "advance_amount": float(
                    amount
                ),

                "transaction_date": (
                    row["transaction_date"].isoformat()
                    if row["transaction_date"]
                    else None
                ),

                "created_at": (
                    row["created_at"].isoformat()
                    if row["created_at"]
                    else None
                )
            })

        # ======================================================================
        # VENDOR-WISE TOTAL ADVANCE IN SELECTED DATE RANGE
        # ======================================================================

        vendor_totals = {}

        for row in report:

            vendor_id = row["vendor_id"]

            if vendor_id not in vendor_totals:

                vendor_totals[vendor_id] = 0

            vendor_totals[vendor_id] += (
                row["advance_amount"]
            )

        # ======================================================================
        # ADD VENDOR TOTAL TO EACH TRANSACTION
        # ======================================================================

        for row in report:

            row["vendor_total_advance"] = round(
                vendor_totals[
                    row["vendor_id"]
                ],
                2
            )

        # ======================================================================
        # RESPONSE
        # ======================================================================

        return jsonify({

            "success": True,

            "from_date": from_date.isoformat(),

            "to_date": to_date.isoformat(),

            "total_transactions": len(report),

            "total_vendors": len(vendor_ids),

            "total_advance": float(
                total_advance
            ),

            "transactions": report

        }), 200

    except Exception as e:

        logging.exception(
            "advance_report_data failed"
        )

        return jsonify({
            "success": False,
            "message": "Advance report load करता आला नाही."
        }), 500

    finally:

        cursor.close()



# ------------------------------
# Food Sack (safe grouping & update)
# ------------------------------
@app.route('/food_sack')
def food_sack():

    vendors = get_vendors_cached(session['id'])

    selected_date = request.args.get('date') or today_ist().isoformat()

    cursor = SafeCursor(mysql.connection.cursor())
    cursor.execute("""
        SELECT * FROM food_sack_rates
        WHERE user_id=%s AND is_active=1
        ORDER BY name
    """, (session['id'],))
    sack_rates = cursor.fetchall()

    return render_template(
        'milk_operations/food_sack.html',
        vendors=vendors,
        sack_rates=sack_rates,
        selected_date=selected_date,
        today_date=selected_date
    )

@app.route('/submit_food_sack_ajax', methods=['POST'])
def submit_food_sack_ajax():
    """Save (upsert) one food-sack issue. Replaying the same request sets
    the same quantity again, so it is naturally idempotent for offline sync."""
    data = request.get_json(silent=True) or {}
    user_id = int(session['id'])

    try:
        vendor_id = parse_vendor_id(data.get('vendor_id'))
        qty = int(str(data.get('quantity')).strip())
        sack_id = int(str(data.get('sack_id')).strip())
        if qty <= 0 or qty > 100000 or sack_id <= 0:
            raise ValueError
        on_date, _slot = resolve_date_slot({"date": data.get('date'), "slot": "morning"})
    except ValidationError as e:
        return jsonify({"success": False, "saved": False, "code": "invalid",
                        "field": e.field, "message": e.message}), 400
    except (TypeError, ValueError):
        return jsonify({"success": False, "saved": False, "code": "invalid",
                        "message": "Invalid data"}), 400

    cursor = db_cursor()
    try:
        cursor.execute("SELECT 1 FROM vendors WHERE vendor_id=%s AND user_id=%s",
                       (vendor_id, user_id))
        if not cursor.fetchone():
            return jsonify({"success": False, "saved": False, "code": "vendor_not_found",
                            "message": "Customer not found."}), 404

        cursor.execute("SELECT rate FROM food_sack_rates WHERE id=%s AND user_id=%s",
                       (sack_id, user_id))
        res = cursor.fetchone()
        if not res:
            return jsonify({"success": False, "saved": False, "code": "invalid",
                            "message": "Invalid sack"}), 400

        rate = float(res['rate'])
        total = qty * rate

        # Update instead of duplicating (one row per vendor/date/sack type)
        cursor.execute("""
            SELECT id FROM food_sack
            WHERE vendor_id=%s AND user_id=%s AND date=%s AND sack_rate_id=%s
        """, (vendor_id, user_id, on_date, sack_id))
        existing = cursor.fetchone()
        if existing:
            cursor.execute("UPDATE food_sack SET sack_qty=%s, total_cost=%s WHERE id=%s",
                           (qty, total, existing['id']))
            entry_id = existing['id']
        else:
            cursor.execute("""
                INSERT INTO food_sack
                (vendor_id, user_id, date, sack_qty, sack_rate_id, sack_rate, total_cost)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (vendor_id, user_id, on_date, qty, sack_id, rate, total))
            entry_id = cursor.lastrowid
        mysql.connection.commit()
    except Exception:
        logging.exception("food sack save failed")
        return jsonify({"success": False, "saved": False, "code": "server_error",
                        "message": "Server error saving data."}), 500
    finally:
        cursor.close()

    return jsonify({"success": True, "saved": True, "id": int(entry_id),
                    "message": _t("Saved for vendor {vendor}", vendor=vendor_id)})


@app.route('/submit_bulk_food_sack_ajax', methods=['POST'])
def submit_bulk_food_sack_ajax():
    """
    OPTIMIZATION NOTE:
    BEFORE: for N entries this ran 2 queries PER entry (rate lookup, existing
            check) plus 1 write = up to 3N queries.
    AFTER:  sack rates and existing food_sack rows are prefetched in 2 bulk
            queries up front; the loop only issues the necessary write per
            entry. Query count drops from ~3N to ~(2 + N).
    """
    data = request.get_json(silent=True) or {}
    raw_entries = data.get("entries") or []

    if not raw_entries:
        return jsonify({"message": "No data"}), 400

    # Validate every item first; invalid items are reported, never half-saved.
    entries, invalid = [], 0
    for item in raw_entries:
        try:
            vid = parse_vendor_id(item.get('vendor_id'))
            q_ = int(str(item.get('quantity')).strip())
            sid = int(str(item.get('sack_id')).strip())
            d_obj, _slot = resolve_date_slot({"date": item.get('date'), "slot": "morning"})
            if q_ <= 0 or q_ > 100000 or sid <= 0:
                raise ValueError
            entries.append({"vendor_id": vid, "quantity": q_, "sack_id": sid,
                            "date": d_obj.isoformat()})
        except (ValidationError, TypeError, ValueError, AttributeError):
            invalid += 1
    if not entries:
        return jsonify({"message": "Invalid data"}), 400

    user_id = session['id']
    cursor = SafeCursor(mysql.connection.cursor())

    # ---- Prefetch all needed sack rates ----
    sack_ids = list({int(item['sack_id']) for item in entries if item.get('sack_id')})
    rate_map = {}
    if sack_ids:
        placeholders = ",".join(["%s"] * len(sack_ids))
        cursor.execute(
            f"SELECT id, rate FROM food_sack_rates WHERE user_id=%s AND id IN ({placeholders})",
            tuple([user_id] + sack_ids)
        )
        for r in cursor.fetchall():
            rate_map[int(r['id'])] = float(r['rate'])

    # ---- Only this dairy's customers (another tenant's id is skipped, not a 500) ----
    vendor_ids = list({item['vendor_id'] for item in entries})
    if vendor_ids:
        vp = ",".join(["%s"] * len(vendor_ids))
        cursor.execute(f"SELECT vendor_id FROM vendors WHERE user_id=%s AND vendor_id IN ({vp})",
                       tuple([user_id] + vendor_ids))
        own = {int(r['vendor_id']) for r in cursor.fetchall()}
        invalid += sum(1 for item in entries if item['vendor_id'] not in own)
        entries = [item for item in entries if item['vendor_id'] in own]
        vendor_ids = [v for v in vendor_ids if v in own]

    # ---- Prefetch existing food_sack rows for involved vendor/date combos ----
    dates_involved = list({item['date'] for item in entries if item.get('date')})
    existing_map = {}
    if vendor_ids and dates_involved:
        vp = ",".join(["%s"] * len(vendor_ids))
        dp = ",".join(["%s"] * len(dates_involved))
        cursor.execute(
            f"""SELECT id, vendor_id, date, sack_rate_id
                FROM food_sack
                WHERE user_id=%s AND vendor_id IN ({vp}) AND date IN ({dp})""",
            tuple([user_id] + vendor_ids + dates_involved)
        )
        for row in cursor.fetchall():
            d = row['date']
            dstr = d.strftime("%Y-%m-%d") if hasattr(d, 'strftime') else str(d)
            existing_map[(str(row['vendor_id']), dstr, int(row['sack_rate_id']))] = row['id']

    saved = 0

    for item in entries:

        vendor_id = item['vendor_id']
        qty = int(item['quantity'])
        sack_id = int(item['sack_id'])
        date_val = item['date']

        rate = rate_map.get(sack_id)
        if rate is None:
            continue

        total = qty * rate

        key = (str(vendor_id), str(date_val), sack_id)
        existing_id = existing_map.get(key)

        if existing_id == "PENDING":
            # This exact combo was inserted earlier in THIS SAME batch.
            cursor.execute("""
                UPDATE food_sack
                SET sack_qty=%s, total_cost=%s
                WHERE vendor_id=%s AND user_id=%s AND date=%s AND sack_rate_id=%s
            """, (qty, total, vendor_id, user_id, date_val, sack_id))
        elif existing_id:
            cursor.execute("""
                UPDATE food_sack
                SET sack_qty=%s, total_cost=%s
                WHERE id=%s
            """, (qty, total, existing_id))
        else:
            cursor.execute("""
                INSERT INTO food_sack
                (vendor_id, user_id, date, sack_qty, sack_rate_id, sack_rate, total_cost)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (vendor_id, user_id, date_val, qty, sack_id, rate, total))
            existing_map[key] = "PENDING"

        saved += 1

    mysql.connection.commit()

    message = _t("{n} entries saved", n=saved)
    if invalid:
        message += " " + _t("{n} entries skipped (invalid data).", n=invalid)
    return jsonify({"success": True, "saved": saved, "skipped_count": invalid,
                    "message": message})



@app.route('/add_food_sack', methods=['POST'])
def add_food_sack():
    if 'id' not in session:
        return redirect(url_for('login'))
    name = request.form.get('name')
    rate = request.form.get('rate')
    date_from = today_ist()
    try:
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute("INSERT INTO food_sack_rates (user_id, name, rate, date_from) VALUES (%s,%s,%s,%s)",
                       (session['id'], name, rate, date_from))
        mysql.connection.commit()
        audit_log(session['id'], 'add_food_sack_rate', f"{name} {rate}")
        flash('Food sack rate added.', 'success')
    except Exception:
        logging.exception("Error adding food_sack_rate")
        flash('Error adding rate.', 'danger')
    return redirect(url_for('food_sack_rate'))


@app.route('/food_sack_rate', methods=['GET', 'POST'])
def food_sack_rate():

    if 'id' not in session:
        return redirect(url_for('login'))

    cursor = SafeCursor(mysql.connection.cursor())

    if request.method == 'POST':

        name = request.form.get('name')
        rate = request.form.get('rate')
        date_from = today_ist()

        cursor.execute("""
        INSERT INTO food_sack_rates
        (user_id,name,rate,date_from)
        VALUES(%s,%s,%s,%s)
        """,(session['id'],name,rate,date_from))

        mysql.connection.commit()

        audit_log(session['id'], 'add_food_sack_rate', f"{name} {rate}")

        flash('Added.', 'success')

        return redirect(url_for('food_sack_rate'))

    # ⚡ optimized select
    cursor.execute("""
    SELECT id,name,rate,date_from
    FROM food_sack_rates
    WHERE user_id=%s
    AND is_active=1
    ORDER BY name
    """,(session['id'],))

    rates = cursor.fetchall()

    cursor.close()

    return render_template(
        'rates/food_sack_rate.html',
        food_sacks=rates
    )
    
    
@app.route('/update_food_sack_rate', methods=['POST'])
def update_food_sack_rate():

    if 'id' not in session:
        return redirect(url_for('login'))

    sack_id = request.form.get('sack_id')
    new_rate = request.form.get('new_rate')
    date_from = request.form.get('date_from')

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
    SELECT name
    FROM food_sack_rates
    WHERE id=%s
    AND user_id=%s
    LIMIT 1
    """,(sack_id,session['id']))

    sack = cursor.fetchone()

    if not sack:
        flash("Invalid sack.", "danger")
        return redirect(url_for('food_sack_rate'))

    name = sack['name']

    # ⚡ insert new rate (history safe)
    cursor.execute("""
    INSERT INTO food_sack_rates
    (user_id,name,rate,date_from)
    VALUES(%s,%s,%s,%s)
    """,(session['id'],name,new_rate,date_from))

    mysql.connection.commit()

    audit_log(session['id'], 'update_food_sack_rate', f"name={name} rate={new_rate}")

    flash("New rate applied.", "success")

    return redirect(url_for('food_sack_rate'))

@app.route('/delete_food_sack_rate/<int:sack_id>', methods=['POST'])
def delete_food_sack_rate(sack_id):

    if 'id' not in session:
        return redirect(url_for('login'))

    cursor = SafeCursor(mysql.connection.cursor())

    # check if sack exists
    cursor.execute("""
        SELECT id
        FROM food_sack_rates
        WHERE id=%s AND user_id=%s
    """, (sack_id, session['id']))

    sack = cursor.fetchone()

    if not sack:
        flash("खाद्य पोती दर सापडला नाही.", "danger")
        return redirect(url_for('food_sack_rate'))

    # SOFT DELETE
    cursor.execute("""
        UPDATE food_sack_rates
        SET is_active = 0
        WHERE id=%s AND user_id=%s
    """, (sack_id, session['id']))

    mysql.connection.commit()

    audit_log(session['id'], 'delete_food_sack_rate', f"id={sack_id}")

    flash('खाद्य पोती दर काढण्यात आला.', 'success')

    return redirect(url_for('food_sack_rate'))
# ------------------------------
# Edit Entry & safer update_entries
# ------------------------------

@app.route('/edit_entry', methods=['GET', 'POST'])
def edit_entry():

    if "id" not in session:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    user_id = int(session['id'])

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

    # =========================================================
    # LOAD VENDORS
    # =========================================================

    cursor.execute("""
        SELECT
            vendor_id,
            name,
            milk_type
        FROM vendors
        WHERE user_id=%s
        ORDER BY vendor_id ASC
    """, (user_id,))

    vendors = cursor.fetchall()

    data = []
    selected_vendor_type = None

    vendor_id = request.args.get("vendor_id")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")

    # =========================================================
    # LOAD SELECTED VENDOR DATA
    # =========================================================

    if vendor_id and from_date and to_date:

        # -----------------------------------------------------
        # OWNERSHIP + MILK TYPE CHECK
        # -----------------------------------------------------

        cursor.execute("""
            SELECT milk_type
            FROM vendors
            WHERE vendor_id=%s
              AND user_id=%s
        """, (
            vendor_id,
            user_id
        ))

        vendor_info = cursor.fetchone()

        if not vendor_info:

            cursor.close()

            flash(
                "Unauthorized vendor.",
                "danger"
            )

            return redirect(
                url_for("edit_entry")
            )

        selected_vendor_type = vendor_info["milk_type"]

        # =====================================================
        # LOAD ALL MILK DATA
        # =====================================================

        cursor.execute("""
            SELECT
                id,
                date,
                slot,
                milk_type,
                quantity
            FROM milk_collection
            WHERE vendor_id=%s
              AND user_id=%s
              AND date BETWEEN %s AND %s
        """, (
            vendor_id,
            user_id,
            from_date,
            to_date
        ))

        milk_rows = cursor.fetchall()

        milk_map = {}

        for r in milk_rows:

            d = r['date'].strftime(
                "%Y-%m-%d"
            )

            key = (
                d,
                r['slot'],
                r['milk_type']
            )

            milk_map[key] = r['quantity']

        # =====================================================
        # BUILD DATE RANGE
        # =====================================================

        start = datetime.strptime(
            from_date,
            "%Y-%m-%d"
        )

        end = datetime.strptime(
            to_date,
            "%Y-%m-%d"
        )

        d = start

        while d <= end:

            ds = d.strftime(
                "%Y-%m-%d"
            )

            rec = {
                "date": ds,

                "cow_morning": milk_map.get(
                    (
                        ds,
                        "morning",
                        "cow"
                    ),
                    0
                ),

                "cow_evening": milk_map.get(
                    (
                        ds,
                        "evening",
                        "cow"
                    ),
                    0
                ),

                "buffalo_morning": milk_map.get(
                    (
                        ds,
                        "morning",
                        "buffalo"
                    ),
                    0
                ),

                "buffalo_evening": milk_map.get(
                    (
                        ds,
                        "evening",
                        "buffalo"
                    ),
                    0
                )
            }

            data.append(rec)

            d += timedelta(
                days=1
            )

    # =========================================================
    # TODAY
    # =========================================================

    today = today_ist().strftime(
        "%Y-%m-%d"
    )

    cursor.close()

    return render_template(
        "milk_operations/edit_entry.html",
        vendors=vendors,
        data=data,
        selected_vendor_type=selected_vendor_type,
        today=today
    )

@app.route('/update_entries', methods=['POST'])
def update_entries():

    if "id" not in session:
        flash(
            "Please login first.",
            "danger"
        )

        return redirect(
            url_for("login")
        )

    user_id = int(
        session['id']
    )

    vendor_id = request.form.get(
        "vendor_id"
    )

    from_date = request.form.get(
        "from_date"
    )

    to_date = request.form.get(
        "to_date"
    )

    # =========================================================
    # BASIC INPUT VALIDATION
    # =========================================================

    if not vendor_id or not from_date or not to_date:

        flash(
            "Invalid update request.",
            "danger"
        )

        return redirect(
            url_for("edit_entry")
        )

    try:

        datetime.strptime(
            from_date,
            "%Y-%m-%d"
        )

        datetime.strptime(
            to_date,
            "%Y-%m-%d"
        )

    except ValueError:

        flash(
            "Invalid date format.",
            "danger"
        )

        return redirect(
            url_for("edit_entry")
        )

    if from_date > to_date:

        flash(
            "Invalid date range.",
            "danger"
        )

        return redirect(
            url_for("edit_entry")
        )

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

    try:
        # Explicit transaction so the rollback below really undoes
        # partial changes (the connection runs with autocommit ON).
        mysql.connection.begin()

        # =====================================================
        # OWNERSHIP + MILK TYPE
        # =====================================================

        cursor.execute("""
            SELECT
                milk_type
            FROM vendors
            WHERE vendor_id=%s
              AND user_id=%s
        """, (
            vendor_id,
            user_id
        ))

        vendor_info = cursor.fetchone()

        if not vendor_info:

            mysql.connection.rollback()

            cursor.close()

            flash(
                "Unauthorized vendor.",
                "danger"
            )

            return redirect(
                url_for("edit_entry")
            )

        vendor_type = vendor_info[
            "milk_type"
        ]

        # =====================================================
        # ALLOWED MILK TYPES
        # =====================================================

        if vendor_type == "cow":

            allowed_types = [
                "cow"
            ]

        elif vendor_type == "buffalo":

            allowed_types = [
                "buffalo"
            ]

        else:

            allowed_types = [
                "cow",
                "buffalo"
            ]

        # =====================================================
        # LOAD EXISTING MILK ENTRIES
        # =====================================================

        cursor.execute("""
            SELECT
                id,
                date,
                slot,
                milk_type,
                quantity
            FROM milk_collection
            WHERE vendor_id=%s
              AND user_id=%s
              AND date BETWEEN %s AND %s
        """, (
            vendor_id,
            user_id,
            from_date,
            to_date
        ))

        existing = cursor.fetchall() or []

        existing_map = {}

        for r in existing:

            dstr = r['date'].strftime(
                "%Y-%m-%d"
            )

            existing_map[
                (
                    dstr,
                    r['slot'],
                    r['milk_type']
                )
            ] = {
                'id': r['id'],
                'quantity': float(
                    r['quantity']
                )
            }

        # =====================================================
        # DATE LOOP
        # =====================================================

        cur_date = datetime.strptime(
            from_date,
            "%Y-%m-%d"
        ).date()

        end_date = datetime.strptime(
            to_date,
            "%Y-%m-%d"
        ).date()

        while cur_date <= end_date:

            ds = cur_date.strftime(
                "%Y-%m-%d"
            )

            # =================================================
            # MILK UPDATE
            # =================================================

            for milk_type in allowed_types:

                for slot in (
                    "morning",
                    "evening"
                ):

                    field_name = (
                        f"{milk_type}_"
                        f"{slot}_"
                        f"{ds}"
                    )

                    raw_qty = request.form.get(
                        field_name
                    )

                    try:

                        qty = float(
                            raw_qty or 0
                        )

                    except (
                        TypeError,
                        ValueError
                    ):

                        raise ValueError(
                            "Invalid milk quantity."
                        )

                    # -------------------------------------------------
                    # SECURITY / DATA VALIDATION
                    # -------------------------------------------------

                    if qty < 0:

                        raise ValueError(
                            "Milk quantity cannot be negative."
                        )

                    key = (
                        ds,
                        slot,
                        milk_type
                    )

                    existing_row = (
                        existing_map.get(
                            key
                        )
                    )

                    # =================================================
                    # INSERT / UPDATE
                    # =================================================

                    if qty > 0:

                        if existing_row:

                            if abs(
                                existing_row[
                                    'quantity'
                                ] - qty
                            ) > 1e-9:

                                cursor.execute("""
                                    UPDATE milk_collection
                                    SET quantity=%s
                                    WHERE id=%s
                                      AND vendor_id=%s
                                      AND user_id=%s
                                """, (
                                    qty,
                                    existing_row['id'],
                                    vendor_id,
                                    user_id
                                ))

                        else:

                            cursor.execute("""
                                INSERT INTO milk_collection
                                (
                                    vendor_id,
                                    user_id,
                                    date,
                                    slot,
                                    milk_type,
                                    quantity
                                )
                                VALUES
                                (
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s
                                )
                            """, (
                                vendor_id,
                                user_id,
                                ds,
                                slot,
                                milk_type,
                                qty
                            ))

                    # =================================================
                    # DELETE MILK
                    # =================================================

                    else:

                        if existing_row:

                            cursor.execute("""
                                DELETE FROM milk_collection
                                WHERE id=%s
                                  AND vendor_id=%s
                                  AND user_id=%s
                            """, (
                                existing_row['id'],
                                vendor_id,
                                user_id
                            ))

            # =====================================================
            # NEXT DATE
            # =====================================================

            cur_date += timedelta(
                days=1
            )

        # =========================================================
        # COMMIT EVERYTHING AT ONCE
        # =========================================================

        mysql.connection.commit()

        flash(
            "Entries updated successfully.",
            "success"
        )

    except Exception as e:

        # =========================================================
        # ROLLBACK EVERYTHING
        # =========================================================

        mysql.connection.rollback()

        app.logger.exception(
            "Error updating entries"
        )

        flash(
            "Error updating entries.",
            "danger"
        )

    finally:

        cursor.close()

    # =========================================================
    # RETURN TO EDIT PAGE
    # =========================================================

    return redirect(
        url_for(
            "edit_entry",
            vendor_id=vendor_id,
            from_date=from_date,
            to_date=to_date
        )
    )

@app.route('/delete_entry', methods=['POST'])
def delete_entry():

    if "id" not in session:
        flash(
            "Please login first.",
            "danger"
        )

        return redirect(
            url_for("login")
        )

    user_id = int(
        session['id']
    )

    vendor_id = request.form.get(
        "vendor_id"
    )

    date_str = request.form.get(
        "date"
    )

    confirm = request.form.get(
        "confirm"
    )

    # =========================================================
    # CONFIRMATION CHECK
    # =========================================================

    if str(confirm) != "1":

        flash(
            "Please confirm deletion.",
            "warning"
        )

        return redirect(
            url_for("edit_entry")
        )

    # =========================================================
    # BASIC VALIDATION
    # =========================================================

    if not vendor_id or not date_str:

        flash(
            "Invalid deletion request.",
            "danger"
        )

        return redirect(
            url_for("edit_entry")
        )

    try:

        datetime.strptime(
            date_str,
            "%Y-%m-%d"
        )

    except ValueError:

        flash(
            "Invalid date.",
            "danger"
        )

        return redirect(
            url_for("edit_entry")
        )

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

    try:
        # Explicit transaction so the rollback below really undoes
        # partial changes (the connection runs with autocommit ON).
        mysql.connection.begin()

        # =====================================================
        # OWNERSHIP CHECK
        # =====================================================

        cursor.execute("""
            SELECT 1
            FROM vendors
            WHERE vendor_id=%s
              AND user_id=%s
            LIMIT 1
        """, (
            vendor_id,
            user_id
        ))

        if not cursor.fetchone():

            mysql.connection.rollback()

            flash(
                "Unauthorized to delete.",
                "danger"
            )

            return redirect(
                url_for("edit_entry")
            )

        # =====================================================
        # DELETE MILK
        # =====================================================

        cursor.execute("""
            DELETE FROM milk_collection
            WHERE vendor_id=%s
              AND user_id=%s
              AND date=%s
        """, (
            vendor_id,
            user_id,
            date_str
        ))

        # =====================================================
        # DELETE FOOD SACK
        # =====================================================

        cursor.execute("""
            DELETE FROM food_sack
            WHERE vendor_id=%s
              AND user_id=%s
              AND date=%s
        """, (
            vendor_id,
            user_id,
            date_str
        ))

        # =====================================================
        # COMMIT
        # =====================================================

        mysql.connection.commit()

        flash(
            "Entry deleted successfully.",
            "success"
        )

    except Exception as e:

        # =====================================================
        # ROLLBACK
        # =====================================================

        mysql.connection.rollback()

        app.logger.exception(
            "Error deleting entry"
        )

        flash(
            "Error deleting entry.",
            "danger"
        )

    finally:

        cursor.close()

    # =========================================================
    # RETURN
    # =========================================================

    return redirect(
        url_for(
            "edit_entry",
            vendor_id=vendor_id,
            from_date=request.form.get(
                "from_date"
            ),
            to_date=request.form.get(
                "to_date"
            )
        )
    )
# ------------------------------
# Receipts, calculation, payment (kept logic but with small safety)
# ------------------------------
# ------------------------------
# Receipts, calculation, payment (kept logic but with small safety)
# ------------------------------
@app.route('/calculation', methods=['GET', 'POST'])
def calculation():

    if 'id' not in session:
        flash('Please login first.', 'danger')
        return redirect(url_for('login'))

    cursor = SafeCursor(
        mysql.connection.cursor(
            MySQLdb.cursors.DictCursor
        )
    )

    cursor.execute("""
        SELECT *
        FROM vendors
        WHERE user_id=%s
        ORDER BY vendor_id ASC
    """, (session['id'],))

    vendors = cursor.fetchall()

    results = None

    if request.method == 'POST':

        vendor_id = request.form.get('vendor_id')
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')

        # =====================================================
        # LOAD MILK DATA
        # =====================================================

        cursor.execute("""
            SELECT
                date,
                slot,
                milk_type,
                quantity
            FROM milk_collection
            WHERE vendor_id=%s
              AND user_id=%s
              AND date BETWEEN %s AND %s
        """, (
            vendor_id,
            session['id'],
            start_date,
            end_date
        ))

        milk_data = cursor.fetchall()

        mcq = ecq = mbq = ebq = 0
        mcp = ecp = mbp = ebp = 0

        # =====================================================
        # MILK CALCULATION
        # =====================================================

        for row in milk_data:

            rate = get_vendor_rate(
                cursor,
                vendor_id,
                row['milk_type'],
                row['date']
            )

            amt = (
                float(row['quantity'])
                * rate
            )

            if row['milk_type'] == "cow":

                if row['slot'] == "morning":

                    mcq += row['quantity']

                    mcp += amt

                else:

                    ecq += row['quantity']

                    ecp += amt

            else:

                if row['slot'] == "morning":

                    mbq += row['quantity']

                    mbp += amt

                else:

                    ebq += row['quantity']

                    ebp += amt

        # =====================================================
        # ADVANCE LEDGER
        # =====================================================
        #
        # Current-period deduction:
        # Only deduction transactions between start_date
        # and end_date.
        #
        # Remaining balance:
        # All advance transactions up to end_date
        # minus all deduction transactions up to end_date.
        # =====================================================

        cursor.execute("""
            SELECT
                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'deduction'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS period_deduction
            FROM advance_transactions
            WHERE vendor_id=%s
              AND user_id=%s
              AND transaction_date BETWEEN %s AND %s
        """, (
            vendor_id,
            session['id'],
            start_date,
            end_date
        ))

        deduction_row = (
            cursor.fetchone()
            or {}
        )

        period_deduction = float(
            deduction_row.get(
                'period_deduction'
            ) or 0
        )

        period_deduction = round(
            period_deduction,
            2
        )

        # =====================================================
        # REMAINING ADVANCE BALANCE
        # =====================================================

        cursor.execute("""
            SELECT
                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'advance'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_advance,

                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'deduction'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_deduction

            FROM advance_transactions

            WHERE vendor_id=%s
              AND user_id=%s
              AND transaction_date <= %s
        """, (
            vendor_id,
            session['id'],
            end_date
        ))

        balance_row = (
            cursor.fetchone()
            or {}
        )

        total_advance_balance = float(
            balance_row.get(
                'total_advance'
            ) or 0
        )

        total_deduction_balance = float(
            balance_row.get(
                'total_deduction'
            ) or 0
        )

        remaining_advance = round(
            total_advance_balance
            - total_deduction_balance,
            2
        )

        remaining_advance = max(
            remaining_advance,
            0
        )

        # =====================================================
        # FOOD SACK
        # =====================================================

        cursor.execute("""
            SELECT
                SUM(total_cost) AS total
            FROM food_sack
            WHERE vendor_id=%s
              AND user_id=%s
              AND date BETWEEN %s AND %s
        """, (
            vendor_id,
            session['id'],
            start_date,
            end_date
        ))

        food_row = cursor.fetchone()

        total_food = (
            food_row['total']
            if food_row
            and food_row['total'] is not None
            else 0
        )

        # FAT/SNF entries (present only after a mode switch) are added so
        # no delivered milk is left out of the calculation.
        for (fsn_type, fsn_slot), (fsn_l, fsn_a) in fsn_slot_totals(
                session['id'], vendor_id, start_date, end_date).items():
            fsn_l, fsn_a = float(fsn_l), float(fsn_a)
            if fsn_type == "cow" and fsn_slot == "morning":
                mcq += fsn_l
                mcp += fsn_a
            elif fsn_type == "cow":
                ecq += fsn_l
                ecp += fsn_a
            elif fsn_slot == "morning":
                mbq += fsn_l
                mbp += fsn_a
            else:
                ebq += fsn_l
                ebp += fsn_a

        # =====================================================
        # FINAL CALCULATION
        # =====================================================

        milk_total = (
            mcp
            + ecp
            + mbp
            + ebp
        )

        # IMPORTANT:
        # Only current-period deduction is subtracted.
        #
        # Old unpaid advance remains in
        # remaining_advance and is NOT deducted again.

        final_payment = round(
            milk_total
            - (
                period_deduction
                + float(total_food)
            ),
            2
        )

        # =====================================================
        # RESULT
        # =====================================================

        results = {

            'morning_cow_quantity':
                mcq,

            'evening_cow_quantity':
                ecq,

            'morning_cow_payment':
                mcp,

            'evening_cow_payment':
                ecp,

            'total_cow_quantity':
                mcq + ecq,

            'total_cow_payment':
                mcp + ecp,

            'morning_buffalo_quantity':
                mbq,

            'evening_buffalo_quantity':
                ebq,

            'morning_buffalo_payment':
                mbp,

            'evening_buffalo_payment':
                ebp,

            'total_buffalo_quantity':
                mbq + ebq,

            'total_buffalo_payment':
                mbp + ebp,

            'total_milk_quantity':
                (
                    mcq
                    + ecq
                    + mbq
                    + ebq
                ),

            'milk_total':
                milk_total,

            'total_food_sack_cost':
                total_food,

            # Current period deduction
            'total_advance':
                period_deduction,

            # Explicit deduction field
            'total_deduction':
                period_deduction,

            # Carry-forward balance
            'remaining_advance':
                remaining_advance,

            'final_payable_amount':
                final_payment
        }

    cursor.close()

    return render_template(
        'milk_operations/calculation.html',
        vendors=vendors,
        results=results
    )
    
    
@app.route('/payment', methods=['GET', 'POST'])
def payment():
    """
    Payment calculation for all vendors.

    Advance ledger logic:

    1. advance:
       Money given to vendor.

    2. deduction:
       Money recovered from vendor.

    3. Current-period deduction:
       Only deduction transactions inside the selected
       start_date -> end_date period.

    4. Remaining advance:
       All advances up to end_date
       minus
       all deductions up to end_date.

    5. Final payment:
       Milk payment
       - Food cost
       - Current-period deduction

    Old unpaid advance is carried forward and is NOT
    deducted again from the next payment.
    """

    if 'id' not in session:
        flash('Please login first.', 'danger')
        return redirect(url_for('login'))

    # ---------------------------------------------------------
    # POST -> redirect
    # ---------------------------------------------------------

    if request.method == 'POST':

        start_date = request.form.get(
            'start_date'
        )

        end_date = request.form.get(
            'end_date'
        )

        return redirect(
            url_for(
                'payment',
                start_date=start_date,
                end_date=end_date
            )
        )

    # ---------------------------------------------------------
    # GET parameters
    # ---------------------------------------------------------

    start_date = request.args.get(
        'start_date'
    )

    end_date = request.args.get(
        'end_date'
    )

    cursor = SafeCursor(
        mysql.connection.cursor(
            MySQLdb.cursors.DictCursor
        )
    )

    data = []

    # ---------------------------------------------------------
    # Only calculate when valid date range is available
    # ---------------------------------------------------------

    if start_date and end_date:

        user_id = session['id']

        # =====================================================
        # LOAD VENDORS
        # =====================================================

        cursor.execute("""
            SELECT
                vendor_id,
                name
            FROM vendors
            WHERE user_id=%s
            ORDER BY vendor_id ASC
        """, (user_id,))

        vendors = cursor.fetchall()

        # =====================================================
        # BULK MILK DATA
        # =====================================================

        cursor.execute("""
            SELECT
                vendor_id,
                date,
                milk_type,
                SUM(quantity) AS qty
            FROM milk_collection
            WHERE user_id=%s
              AND date BETWEEN %s AND %s
            GROUP BY
                vendor_id,
                date,
                milk_type
        """, (
            user_id,
            start_date,
            end_date
        ))

        milk_map = {}

        for row in cursor.fetchall():

            vendor_key = str(
                row['vendor_id']
            )

            milk_map.setdefault(
                vendor_key,
                []
            ).append(row)

        # =====================================================
        # CURRENT PERIOD DEDUCTION
        # =====================================================

        cursor.execute("""
            SELECT
                vendor_id,

                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'deduction'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS period_deduction

            FROM advance_transactions

            WHERE user_id=%s
              AND transaction_date BETWEEN %s AND %s

            GROUP BY vendor_id
        """, (
            user_id,
            start_date,
            end_date
        ))

        deduction_map = {}

        for row in cursor.fetchall():

            deduction_map[
                str(row['vendor_id'])
            ] = round(
                float(
                    row['period_deduction']
                    or 0
                ),
                2
            )

        # =====================================================
        # REMAINING ADVANCE BALANCE
        # =====================================================
        #
        # All transactions up to end_date.
        #
        # remaining =
        #     total advance
        #     -
        #     total deduction
        # =====================================================

        cursor.execute("""
            SELECT
                vendor_id,

                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'advance'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_advance,

                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'deduction'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_deduction

            FROM advance_transactions

            WHERE user_id=%s
              AND transaction_date <= %s

            GROUP BY vendor_id
        """, (
            user_id,
            end_date
        ))

        balance_map = {}

        for row in cursor.fetchall():

            total_advance = float(
                row['total_advance']
                or 0
            )

            total_deduction = float(
                row['total_deduction']
                or 0
            )

            remaining_balance = round(
                total_advance
                - total_deduction,
                2
            )

            remaining_balance = max(
                remaining_balance,
                0
            )

            balance_map[
                str(row['vendor_id'])
            ] = remaining_balance

        # =====================================================
        # BULK FOOD SACK DATA
        # =====================================================

        cursor.execute("""
            SELECT
                vendor_id,
                SUM(total_cost) AS total_food
            FROM food_sack
            WHERE user_id=%s
              AND date BETWEEN %s AND %s
            GROUP BY vendor_id
        """, (
            user_id,
            start_date,
            end_date
        ))

        food_map = {}

        for row in cursor.fetchall():

            food_map[
                str(row['vendor_id'])
            ] = float(
                row['total_food']
                or 0
            )

        # FAT/SNF entries in the range (only non-empty after a mode
        # switch) are added so no delivered milk goes unpaid.
        fsn_amounts = fsn_amounts_by_vendor(user_id, start_date, end_date)

        # =====================================================
        # PROCESS ALL VENDORS
        # =====================================================

        for vendor in vendors:

            vendor_id = vendor[
                'vendor_id'
            ]

            vendor_key = str(
                vendor_id
            )

            milk_entries = milk_map.get(
                vendor_key,
                []
            )

            total_cow = 0
            total_buffalo = 0

            cow_cost = 0
            buffalo_cost = 0

            # -------------------------------------------------
            # Calculate milk payment
            # -------------------------------------------------

            for milk in milk_entries:

                rate = get_vendor_rate(
                    cursor,
                    vendor_id,
                    milk['milk_type'],
                    milk['date'],
                    user_id=user_id
                )

                qty = float(
                    milk['qty']
                    or 0
                )

                if milk['milk_type'] == "cow":

                    total_cow += qty

                    cow_cost += (
                        qty * rate
                    )

                else:

                    total_buffalo += qty

                    buffalo_cost += (
                        qty * rate
                    )

            for fsn_type, (fsn_l, fsn_a) in fsn_amounts.get(int(vendor_id), {}).items():
                if fsn_type == "cow":
                    total_cow += float(fsn_l)
                    cow_cost += float(fsn_a)
                else:
                    total_buffalo += float(fsn_l)
                    buffalo_cost += float(fsn_a)

            # -------------------------------------------------
            # Current period deduction
            # -------------------------------------------------

            deduction = deduction_map.get(
                vendor_key,
                0
            )

            # -------------------------------------------------
            # Remaining advance
            # -------------------------------------------------

            remaining_advance = balance_map.get(
                vendor_key,
                0
            )

            # -------------------------------------------------
            # Food sack total
            # -------------------------------------------------

            food = food_map.get(
                vendor_key,
                0
            )

            # -------------------------------------------------
            # Total milk payment
            # -------------------------------------------------

            total_milk_payment = round(
                cow_cost
                + buffalo_cost,
                2
            )

            # -------------------------------------------------
            # Final payable
            # -------------------------------------------------

            total_payment = round(
                total_milk_payment
                - deduction
                - food,
                2
            )

            # -------------------------------------------------
            # Append vendor result
            # -------------------------------------------------

            data.append({

                'vendor_id':
                    vendor_id,

                'vendor_name':
                    vendor['name'],

                'total_cow':
                    total_cow,

                'total_buffalo':
                    total_buffalo,

                'cow_rate':
                    "-",

                'buffalo_rate':
                    "-",

                'total_milk_payment':
                    total_milk_payment,

                # Current period deduction
                'total_advance':
                    deduction,

                'total_deduction':
                    deduction,

                # Carry-forward balance
                'remaining_advance':
                    remaining_advance,

                'total_food':
                    food,

                'total_payment':
                    total_payment
            })

    # ---------------------------------------------------------
    # Close cursor
    # ---------------------------------------------------------

    cursor.close()

    # ---------------------------------------------------------
    # Render payment page
    # ---------------------------------------------------------

    return render_template(
        'milk_operations/payment.html',
        data=data
    )    
# ------------------------------
# Receipt - All Vendors
# ------------------------------
@app.route('/receipt_all_vendors', methods=['GET', 'POST'])
def receipt_all_vendors():

    if 'id' not in session:
        return redirect(url_for('login'))

    user_id = int(session['id'])

    cursor = mysql.connection.cursor(
        MySQLdb.cursors.DictCursor
    )

    if request.method == 'POST':

        from_date = request.form.get('from_date')
        to_date = request.form.get('to_date')
        customers_text = (request.form.get('customers') or '').strip()

        try:
            start_d, end_d = parse_date_range(from_date, to_date)
            selection = parse_customer_selection(customers_text)
        except ValidationError as e:
            cursor.close()
            flash(e.message, 'danger')
            return render_template('receipt_all_vendors.html', receipts=None,
                                   from_date=from_date, to_date=to_date,
                                   customers=customers_text)
        from_date, to_date = start_d.isoformat(), end_d.isoformat()

        # =====================================================
        # LOAD VENDORS
        # =====================================================

        cursor.execute("""
            SELECT
                vendor_id,
                name,
                milk_type,
                address
            FROM vendors
            WHERE user_id=%s
            ORDER BY vendor_id
        """, (user_id,))

        vendors = cursor.fetchall()
        if selection is not None:
            vendors = [v for v in vendors if int(v['vendor_id']) in selection]

        # =====================================================
        # LOAD MILK DATA
        # =====================================================

        cursor.execute("""
            SELECT
                vendor_id,
                date,
                slot,
                milk_type,
                quantity
            FROM milk_collection
            WHERE user_id=%s
              AND date BETWEEN %s AND %s
        """, (
            user_id,
            from_date,
            to_date
        ))

        milk_rows = cursor.fetchall()

        milk_map = {}

        for row in milk_rows:

            vid = int(row['vendor_id'])

            milk_map.setdefault(
                vid,
                []
            ).append(row)

        # =====================================================
        # LOAD FOOD SACK
        # =====================================================

        cursor.execute("""
            SELECT
                fs.vendor_id,
                fs.sack_qty,
                r.name,
                COALESCE(r.rate, 0) AS rate,
                COALESCE(fs.total_cost, 0) AS total
            FROM food_sack fs
            JOIN food_sack_rates r
                ON r.id = fs.sack_rate_id
            WHERE fs.user_id=%s
              AND fs.date BETWEEN %s AND %s
            ORDER BY fs.vendor_id
        """, (
            user_id,
            from_date,
            to_date
        ))

        food_rows = cursor.fetchall()

        food_map = {}

        for food in food_rows:

            vid = int(food['vendor_id'])

            food_map.setdefault(
                vid,
                []
            ).append(food)

        # =====================================================
        # LOAD ADVANCE LEDGER
        # =====================================================
        #
        # IMPORTANT ACCOUNTING LOGIC
        #
        # advance   = amount given to vendor
        # deduction = amount recovered from vendor
        #
        # 1. Period deduction:
        #    Only deductions inside selected period.
        #
        # 2. Remaining advance:
        #    ALL advances up to end_date
        #    minus
        #    ALL deductions up to end_date
        #
        # This allows old unpaid advances to carry forward.
        # =====================================================

        cursor.execute("""
            SELECT
                vendor_id,

                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'deduction'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS period_deduction

            FROM advance_transactions

            WHERE user_id=%s
              AND transaction_date BETWEEN %s AND %s

            GROUP BY vendor_id
        """, (
            user_id,
            from_date,
            to_date
        ))

        deduction_map = {}

        for row in cursor.fetchall():

            deduction_map[
                int(row['vendor_id'])
            ] = round(
                float(row['period_deduction'] or 0),
                2
            )

        # =====================================================
        # REMAINING ADVANCE BALANCE
        # =====================================================
        #
        # Balance is calculated from the beginning of ledger
        # up to selected end_date.
        #
        # remaining =
        #     total advances
        #     -
        #     total deductions
        #
        # Only transactions belonging to this user are used.
        # =====================================================

        cursor.execute("""
            SELECT
                vendor_id,

                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'advance'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_advance,

                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'deduction'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_deduction

            FROM advance_transactions

            WHERE user_id=%s
              AND transaction_date <= %s

            GROUP BY vendor_id
        """, (
            user_id,
            to_date
        ))

        balance_map = {}

        for row in cursor.fetchall():

            total_advance = float(
                row['total_advance'] or 0
            )

            total_deduction = float(
                row['total_deduction'] or 0
            )

            remaining_balance = round(
                total_advance - total_deduction,
                2
            )

            # Prevent displaying negative "बाकी"
            remaining_balance = max(
                remaining_balance,
                0
            )

            balance_map[
                int(row['vendor_id'])
            ] = remaining_balance

        # FAT/SNF entries (present only after a mode switch)
        fsn_by_vendor = {}
        for fsn_row in fsn_entries_in_range(user_id, from_date, to_date):
            fsn_by_vendor.setdefault(int(fsn_row['vendor_id']), []).append(fsn_row)

        # =====================================================
        # PROCESS VENDORS
        # =====================================================

        all_receipts = []

        for vendor in vendors:

            vid = int(
                vendor['vendor_id']
            )

            # =================================================
            # GET RATES
            # =================================================

            cow_rate = get_vendor_rate(
                cursor,
                vid,
                "cow",
                from_date,
                user_id=user_id
            )

            buffalo_rate = get_vendor_rate(
                cursor,
                vid,
                "buffalo",
                from_date,
                user_id=user_id
            )

            # =================================================
            # GET MILK DATA
            # =================================================

            milk_data = milk_map.get(
                vid,
                []
            )

            grouped = {}

            totals = {
                'cow_morning': 0,
                'cow_evening': 0,
                'buffalo_morning': 0,
                'buffalo_evening': 0
            }

            cow_cost = 0
            buffalo_cost = 0

            # =================================================
            # PROCESS MILK
            # =================================================

            for row in milk_data:

                dt = row['date'].strftime(
                    "%Y-%m-%d"
                )

                slot = row['slot']

                mtype = row['milk_type']

                qty = float(
                    row['quantity']
                )

                rate = (
                    cow_rate
                    if mtype == "cow"
                    else buffalo_rate
                )

                if dt not in grouped:

                    grouped[dt] = {

                        'day':
                            row['date'].strftime("%d"),

                        'cow_morning': 0,

                        'cow_evening': 0,

                        'buffalo_morning': 0,

                        'buffalo_evening': 0
                    }

                grouped[dt][
                    f"{mtype}_{slot}"
                ] += qty

                totals[
                    f"{mtype}_{slot}"
                ] += qty

                if mtype == "cow":

                    cow_cost += (
                        qty * rate
                    )

                else:

                    buffalo_cost += (
                        qty * rate
                    )

            for fsn_row in fsn_by_vendor.get(vid, []):
                dt = fsn_row['collection_date'].strftime("%Y-%m-%d")
                key = f"{fsn_row['milk_type']}_{fsn_row['slot']}"
                if dt not in grouped:
                    grouped[dt] = {
                        'day': fsn_row['collection_date'].strftime("%d"),
                        'cow_morning': 0, 'cow_evening': 0,
                        'buffalo_morning': 0, 'buffalo_evening': 0,
                    }
                grouped[dt][key] += float(fsn_row['quantity'])
                totals[key] += float(fsn_row['quantity'])
                if fsn_row['milk_type'] == "cow":
                    cow_cost += float(fsn_row['amount'])
                else:
                    buffalo_cost += float(fsn_row['amount'])

            # =================================================
            # ENTRIES (date order)
            # =================================================

            entries = [grouped[k] for k in sorted(grouped)]

            # =================================================
            # FOOD SACK
            # =================================================

            food_data = food_map.get(
                vid,
                []
            )

            food_total = sum(
                float(f['total'])
                for f in food_data
            )

            food_sack_details = [

                {
                    "name": f['name'],

                    "rate": float(
                        f['rate'] or 0
                    ),

                    "qty": int(
                        f['sack_qty'] or 0
                    ),

                    "total": float(
                        f['total'] or 0
                    )
                }

                for f in food_data
            ]

            # =================================================
            # PERIOD DEDUCTION
            # =================================================

            period_deduction = float(
                deduction_map.get(
                    vid,
                    0
                )
            )

            # =================================================
            # REMAINING ADVANCE
            # =================================================

            remaining_advance = float(
                balance_map.get(
                    vid,
                    0
                )
            )

            # =================================================
            # FINAL PAYABLE
            # =================================================
            #
            # IMPORTANT:
            #
            # Old unpaid advance is NOT deducted again.
            #
            # Only current period deduction is deducted.
            # =================================================

            final_payable = round(
                (
                    cow_cost
                    + buffalo_cost
                )
                - (
                    period_deduction
                    + food_total
                ),
                2
            )

            # =================================================
            # BUILD RECEIPT
            # =================================================

            all_receipts.append({

                'vendor_id':
                    vid,

                'name':
                    vendor['name'],

                'address':
                    vendor['address'],

                'milk_type':
                    vendor['milk_type'],

                'data':
                    entries,

                'total_cow':
                    (
                        totals['cow_morning']
                        + totals['cow_evening']
                    ),

                'total_buffalo':
                    (
                        totals['buffalo_morning']
                        + totals['buffalo_evening']
                    ),

                'cow_cost':
                    round(
                        cow_cost,
                        2
                    ),

                'buffalo_cost':
                    round(
                        buffalo_cost,
                        2
                    ),

                'food_sack_details':
                    food_sack_details,

                'food_cost':
                    food_total,

                # Current period deduction
                'advance':
                    period_deduction,

                # Remaining unpaid advance
                'remaining_advance':
                    remaining_advance,

                'final_payable':
                    final_payable,

                'total_cow_morning':
                    totals['cow_morning'],

                'total_cow_evening':
                    totals['cow_evening'],

                'total_buffalo_morning':
                    totals['buffalo_morning'],

                'total_buffalo_evening':
                    totals['buffalo_evening'],

                'cow_rate':
                    cow_rate,

                'buffalo_rate':
                    buffalo_rate
            })

        # =====================================================
        # CLOSE CURSOR
        # =====================================================

        cursor.close()

        # =====================================================
        # RENDER RECEIPT
        # =====================================================

        return render_template(
            'receipt_all_vendors.html',
            receipts=all_receipts,
            from_date=from_date,
            to_date=to_date,
            customers=customers_text
        )

    # =========================================================
    # GET REQUEST
    # =========================================================

    cursor.close()

    return render_template(
        'receipt_all_vendors.html',
        receipts=None,
        from_date=None,
        to_date=None,
        customers=''
    )
# ------------------------------
# Edit food sack & delete
# ------------------------------
@app.route("/edit_food_sack", methods=["GET", "POST"])
def edit_food_sack():
    if "id" not in session:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    cursor = SafeCursor(mysql.connection.cursor())
    cursor.execute(
        "SELECT vendor_id, name FROM vendors WHERE user_id = %s ORDER BY vendor_id  ASC",
        (session['id'],)
    )
    vendors = cursor.fetchall()

    sacks = []
    selected_vendor_id = None
    if request.method == "POST":
        selected_vendor_id = request.form.get("vendor_id")
        cursor.execute("""
            SELECT fs.id, fs.date, r.name AS company_name, fs.sack_qty, r.rate, (fs.sack_qty * fs.sack_rate) AS total
            FROM food_sack fs
            JOIN food_sack_rates r ON fs.sack_rate_id = r.id
            WHERE fs.vendor_id=%s AND fs.user_id=%s
            ORDER BY fs.date DESC
        """, (selected_vendor_id, session['id']))
        sacks = cursor.fetchall()

    return render_template(
        "milk_operations/edit_food_sack.html",
        vendors=vendors,
        sacks=sacks,
        selected_vendor_id=selected_vendor_id
    )


@app.route("/delete_food_sack/<int:sack_id>", methods=["POST"])
def delete_food_sack(sack_id):
    if 'id' not in session:
        if request.is_json or request.headers.get("Accept") == "application/json":
            return jsonify({"success": False, "message": "Please login first."}), 401
        flash('Please login first.', 'danger')
        return redirect(url_for('login'))

    confirm = request.form.get('confirm') or request.args.get('confirm')
    if confirm != '1':
        if request.is_json or request.headers.get("Accept") == "application/json":
            return jsonify({"success": False, "message": "Please confirm deletion."}), 400
        flash('Please confirm deletion.', 'warning')
        return redirect(url_for('edit_food_sack'))

    try:
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute(
            "DELETE FROM food_sack WHERE id = %s AND user_id = %s",
            (sack_id, session['id'])
        )
        mysql.connection.commit()

        audit_log(session['id'], 'delete_food_sack', f"id={sack_id}")

        if request.is_json or request.headers.get("Accept") == "application/json":
            return jsonify({"success": True, "message": "Food sack entry deleted."}), 200

        flash('Food sack entry deleted.', 'success')
        return redirect(url_for('edit_food_sack'))

    except Exception as e:
        logging.exception("Error deleting food sack")
        if request.is_json or request.headers.get("Accept") == "application/json":
            return jsonify({"success": False, "message": "Error deleting entry."}), 500
        flash('Error deleting food sack entry.', 'danger')
        return redirect(url_for('edit_food_sack'))



# ------------------------------
# Milk summary
# ------------------------------
@app.route('/milk_summary', methods=['GET', 'POST'])
def milk_summary():
    # 🔥 POST → REDIRECT (PRG)
    if request.method == 'POST':
        from_date = request.form.get('from_date')
        to_date = request.form.get('to_date')

        return redirect(url_for(
            'milk_summary',
            from_date=from_date,
            to_date=to_date
        ))

    # =========================
    # GET request (real work)
    # =========================
    from_date = request.args.get('from_date')
    to_date = request.args.get('to_date')

    totals = {
        'cow_morning': 0,
        'cow_evening': 0,
        'buffalo_morning': 0,
        'buffalo_evening': 0
    }

    if from_date and to_date:
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute("""
            SELECT milk_type, slot, SUM(quantity) AS total_qty
            FROM milk_collection
            WHERE user_id = %s AND date BETWEEN %s AND %s
            GROUP BY milk_type, slot
        """, (session['id'], from_date, to_date))

        for row in cursor.fetchall():
            key = f"{row['milk_type']}_{row['slot']}"
            if key in totals:
                totals[key] = row['total_qty'] or 0

        cursor.close()

    totals['cow_total'] = (totals['cow_morning'] or 0) + (totals['cow_evening'] or 0)
    totals['buffalo_total'] = (totals['buffalo_morning'] or 0) + (totals['buffalo_evening'] or 0)
    totals['grand_total'] = totals['cow_total'] + totals['buffalo_total']

    return render_template(
        "milk_operations/milk_summary.html",
        from_date=from_date,
        to_date=to_date,
        totals=totals
    )

from datetime import date

@app.route('/reports/milk-summary', methods=['GET', 'POST'])
def milk_summary_report():

    # 👉 default आजची date
    selected_date = request.args.get('date') or today_ist().isoformat()
    try:
        query_date = date.fromisoformat(selected_date)
    except ValueError:
        query_date = None

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT 
            v.vendor_id AS id,
            v.name,
            COALESCE(SUM(CASE 
                WHEN m.milk_type='cow' AND m.slot='morning'
                THEN m.quantity END),0) AS cow_morning,

            COALESCE(SUM(CASE 
                WHEN m.milk_type='cow' AND m.slot='evening'
                THEN m.quantity END),0) AS cow_evening,

            COALESCE(SUM(CASE 
                WHEN m.milk_type='buffalo' AND m.slot='morning'
                THEN m.quantity END),0) AS buffalo_morning,

            COALESCE(SUM(CASE 
                WHEN m.milk_type='buffalo' AND m.slot='evening'
                THEN m.quantity END),0) AS buffalo_evening

        FROM vendors v
        LEFT JOIN milk_collection m
            ON v.vendor_id = m.vendor_id
            AND v.user_id = m.user_id
            AND m.date = %s
        WHERE v.user_id = %s
        GROUP BY v.vendor_id, v.name
        ORDER BY v.vendor_id ASC
    """, (
        # The date filter sits in the JOIN so only that day's rows are read
        # (it used to be inside each CASE, which joined the vendor's whole
        # history). An invalid date still matches nothing, as before.
        query_date,
        session['id']
    ))

    data = cursor.fetchall()
    cursor.close()

    return render_template(
        'reports/milk_summary_report.html',
        data=data,
        selected_date=selected_date
    )
# ------------------------------
# Vendor Range Summary Page
# ------------------------------
@app.route("/vendor_range_summary", methods=["GET", "POST"])
def vendor_range_summary():
    """
    OPTIMIZATION NOTE:
    BEFORE: 1 milk_collection query PER selected vendor.
    AFTER: 1 bulk query (grouped by vendor_id, milk_type, slot) for ALL
    selected vendors, then dict lookups in Python. Same totals math and same
    (pre-existing) rounding order as before - only the data-fetching changed.
    """

    if "id" not in session:
        return redirect(url_for("login"))

    cursor = SafeCursor(mysql.connection.cursor())

    # Load vendor list
    cursor.execute("""
        SELECT vendor_id, name 
        FROM vendors 
        WHERE user_id=%s 
        ORDER BY vendor_id ASC
    """, (session["id"],))
    vendors = cursor.fetchall()

    results = []
    from_date = None
    to_date = None

    # GRAND TOTAL VARIABLES
    grand_totals = {
        "cow_morning": 0,
        "cow_evening": 0,
        "buffalo_morning": 0,
        "buffalo_evening": 0,
        "cow_total": 0,
        "buffalo_total": 0
    }

    if request.method == "POST":

        from_date = request.form.get("from_date")
        to_date = request.form.get("to_date")
        selected_vendors = request.form.getlist("vendors")

        vendor_name_map = {str(v['vendor_id']): v['name'] for v in vendors}
        valid_vendor_ids = [vid for vid in selected_vendors if vid in vendor_name_map]

        data_map = {}
        if valid_vendor_ids:
            placeholders = ",".join(["%s"] * len(valid_vendor_ids))
            cursor.execute(f"""
                SELECT vendor_id, milk_type, slot, SUM(quantity) as total_qty
                FROM milk_collection
                WHERE user_id=%s AND vendor_id IN ({placeholders})
                AND date BETWEEN %s AND %s
                GROUP BY vendor_id, milk_type, slot
            """, tuple([session["id"]] + valid_vendor_ids + [from_date, to_date]))

            for row in cursor.fetchall():
                data_map.setdefault(str(row['vendor_id']), []).append(row)

        for vendor_id in valid_vendor_ids:

            summary = {
                "vendor_id": vendor_id,
                "name": vendor_name_map[vendor_id],
                "cow_morning": 0,
                "cow_evening": 0,
                "buffalo_morning": 0,
                "buffalo_evening": 0
            }

            for row in data_map.get(vendor_id, []):
                key = f"{row['milk_type']}_{row['slot']}"
                summary[key] = round(row["total_qty"] or 0, 1)

            summary["cow_total"] = summary["cow_morning"] + summary["cow_evening"]
            summary["buffalo_total"] = summary["buffalo_morning"] + summary["buffalo_evening"]
            for k in grand_totals:
                grand_totals[k] = round(grand_totals[k], 1)
            # ADD INTO GRAND TOTALS
            grand_totals["cow_morning"] += summary["cow_morning"]
            grand_totals["cow_evening"] += summary["cow_evening"]
            grand_totals["buffalo_morning"] += summary["buffalo_morning"]
            grand_totals["buffalo_evening"] += summary["buffalo_evening"]
            grand_totals["cow_total"] += summary["cow_total"]
            grand_totals["buffalo_total"] += summary["buffalo_total"]

            results.append(summary)

    cursor.close()

    return render_template(
        "vendor_range_summary.html",
        vendors=vendors,
        results=results,
        from_date=from_date,
        to_date=to_date,
        grand_totals=grand_totals
    )

@app.context_processor
def inject_now():
    return {'now': now_ist()}




from io import BytesIO
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter


from openpyxl.styles import Font, Border, Side, Alignment

@app.route('/generate_bank_report', methods=['GET', 'POST'])
def generate_bank_report():

    if 'id' not in session:
        return redirect(url_for('login'))

    if request.method == 'GET':
        return render_template(
            'reports/generate_bank_report.html',
            from_date=None,
            to_date=None
        )

    from_date = request.form.get("from_date")
    to_date = request.form.get("to_date")

    if not from_date or not to_date:
        return jsonify({"error": "Please select both dates."}), 400

    try:
        start_d, end_d = parse_date_range(from_date, to_date)
    except ValidationError as e:
        return jsonify({"error": e.message}), 400

    if rate_limited([("bank_report", session["id"], 20, 600)]):
        return jsonify({"error": RATE_LIMIT_MESSAGE}), 429

    user_id = int(session["id"])
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # -------------------------------------------------
    # 0. Owner's bank details (IFSC comparison + header info)
    # -------------------------------------------------
    cursor.execute("""
        SELECT ifsc_code, account_holder_name, branch_name
        FROM bank_settings
        WHERE user_id=%s
    """, (user_id,))

    owner_bank = cursor.fetchone()

    if not owner_bank or not owner_bank.get("ifsc_code"):
        cursor.close()
        return jsonify({
            "error": "Please set up your Bank Settings (IFSC code) before generating this report."
        }), 400

    owner_bank_code = owner_bank["ifsc_code"].strip().upper()[:4]
    owner_name = owner_bank.get("account_holder_name") or ""
    branch_name = owner_bank.get("branch_name") or ""

    # -------------------------------------------------
    # 0b. Logged-in user's name (top line of report header)
    # ASSUMPTION: users table has columns id, name.
    # Change "name" / "users" below if your schema differs.
    # -------------------------------------------------
    cursor.execute("""
            SELECT dairy_name
            FROM users
            WHERE id=%s
    """, (user_id,))
    user_row = cursor.fetchone()
    dairy_name = (user_row.get("dairy_name") if user_row else "") or ""

    # -------------------------------------------------
    # 1. Vendors
    # -------------------------------------------------
    cursor.execute("""
        SELECT vendor_id,name,name_en,address,milk_type,
               ifsc_code,account_no
        FROM vendors
        WHERE user_id=%s
        ORDER BY vendor_id
    """, (user_id,))
    vendors = cursor.fetchall()

    # -------------------------------------------------
    # 2. Payable per vendor — the SAME rule and per-date rates as the
    #    payment page:  (litre-rate milk + FAT/SNF milk) - food sack
    #    - advance deducted in the period. Remaining advance is carried
    #    forward and NOT deducted again.
    #    (Previously this queried a non-existent advance_transactions.date
    #    column and its main body sat inside a loop, so it always failed.)
    # -------------------------------------------------
    from decimal import ROUND_HALF_UP as _HALF_UP
    statements = build_statements(user_id, start_d, end_d, list(vendors))
    payable_map = {
        int(st["vendor"]["vendor_id"]): int(st["final"].quantize(Decimal("1"), rounding=_HALF_UP))
        for st in statements
    }

    # -------------------------------------------------
    # 6. Styles (reused everywhere)
    # -------------------------------------------------
    FONT_NAME = "Times New Roman"

    header_font = Font(name=FONT_NAME, size=12, bold=True)      # dairy/owner/date/branch lines
    col_header_font = Font(name=FONT_NAME, size=12, bold=True)  # table column headings
    data_font = Font(name=FONT_NAME, size=12, bold=False)       # normal data rows

    thin = Side(style="thin", color="000000")
    full_border = Border(left=thin, right=thin, top=thin, bottom=thin)

    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center")
    right_align = Alignment(horizontal="right", vertical="center")

    headers = [
        "Sr.No",
        "Beneficiary IFSC CODE",
        "Beneficiary Account No",
        "Beneficiary Name",
        "ADDRESS",
        "AMOUNT"
    ]
    last_col = len(headers)
    last_col_letter = get_column_letter(last_col)

    def style_range(ws, cell_range):
        """Apply border to every cell in a merged/unmerged range string like 'A1:F1'."""
        for row in ws[cell_range]:
            for cell in row:
                cell.border = full_border

    def write_header_block(ws):
        """
        Row1: dairy/user name
        Row2: owner name
        Row3: date range
        Row4: branch
        Row5: column headings
        Returns the row number where data should start.
        """
        # Row 1 - dairy/login name
        ws.merge_cells(f"A1:{last_col_letter}1")
        c = ws["A1"]
        c.value = dairy_name
        c.font = header_font
        c.alignment = center_align
        style_range(ws, f"A1:{last_col_letter}1")

        # Row 2 - owner name
        ws.merge_cells(f"A2:{last_col_letter}2")
        c = ws["A2"]
        c.value = owner_name
        c.font = header_font
        c.alignment = center_align
        style_range(ws, f"A2:{last_col_letter}2")

        # Row 3 - date range
        ws.merge_cells(f"A3:{last_col_letter}3")
        c = ws["A3"]
        c.value = f"DATE:- {from_date} TO {to_date}"
        c.font = header_font
        c.alignment = center_align
        style_range(ws, f"A3:{last_col_letter}3")

        # Row 4 - branch
        ws.merge_cells(f"A4:{last_col_letter}4")
        c = ws["A4"]
        c.value = f"Branch:- {branch_name}"
        c.font = header_font
        c.alignment = right_align
        style_range(ws, f"A4:{last_col_letter}4")

        # Row 5 - column headings
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=5, column=col_idx, value=h)
            cell.font = col_header_font
            cell.alignment = center_align
            cell.border = full_border

        return 6  # first data row

    def new_workbook(title):
        wb = Workbook()
        ws = wb.active
        ws.title = title
        start_row = write_header_block(ws)
        return wb, ws, start_row

    same_wb, same_ws, same_start_row = new_workbook("Same Bank Report")
    other_wb, other_ws, other_start_row = new_workbook("Other Bank Report")

    same_row = same_start_row
    other_row = other_start_row
    same_sr = 1
    other_sr = 1
    same_total = 0
    other_total = 0

    for vendor in vendors:

        vid = int(vendor["vendor_id"])

        final_payable = payable_map.get(vid, 0)

        vendor_ifsc = (vendor.get("ifsc_code") or "").strip().upper()
        vendor_bank_code = vendor_ifsc[:4]

        if vendor_bank_code and vendor_bank_code == owner_bank_code:
            ws, row_num, sr = same_ws, same_row, same_sr
            same_total += final_payable
        else:
            ws, row_num, sr = other_ws, other_row, other_sr
            other_total += final_payable

        row_values = [
            sr,
            vendor_ifsc,
            vendor.get("account_no") or "",
            (vendor.get("name_en") or vendor.get("name") or "").upper(),
            vendor.get("address"),
            final_payable
        ]

        for col_idx, val in enumerate(row_values, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=val)
            cell.font = data_font
            cell.border = full_border
            if col_idx == 6:
                cell.number_format = '0'
                cell.alignment = right_align
            else:
                cell.alignment = left_align if col_idx != 1 else center_align

        if ws is same_ws:
            same_row += 1
            same_sr += 1
        else:
            other_row += 1
            other_sr += 1

    # -------------------------------------------------
    # 6b. Total row at the bottom of each sheet
    # -------------------------------------------------
    def write_total_row(ws, row_num, total_amount):
        ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=last_col - 1)
        label_cell = ws.cell(row=row_num, column=1, value="Total")
        label_cell.font = col_header_font
        label_cell.alignment = right_align
        label_cell.border = full_border

        for col_idx in range(2, last_col):
            ws.cell(row=row_num, column=col_idx).border = full_border

        total_cell = ws.cell(row=row_num, column=last_col, value=int(round(total_amount)))
        total_cell.font = col_header_font
        total_cell.number_format = '0'
        total_cell.alignment = right_align
        total_cell.border = full_border

    write_total_row(same_ws, same_row, same_total)
    write_total_row(other_ws, other_row, other_total)

    # -------------------------------------------------
    # 7. Fixed column widths
    # -------------------------------------------------
    widths = [8, 20, 22, 40, 32, 14]
    for ws in (same_ws, other_ws):
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        # header rows a bit taller so wrapped text looks clean
        for r in (1, 2, 3, 4):
            ws.row_dimensions[r].height = 20

    cursor.close()

    # -------------------------------------------------
    # 8. Save both workbooks into a single ZIP
    # -------------------------------------------------
    same_bytes = BytesIO()
    same_wb.save(same_bytes)
    same_bytes.seek(0)

    other_bytes = BytesIO()
    other_wb.save(other_bytes)
    other_bytes.seek(0)

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Same_Bank_Report.xlsx", same_bytes.getvalue())
        zf.writestr("Other_Bank_Report.xlsx", other_bytes.getvalue())
    zip_buffer.seek(0)

    return send_file(
        zip_buffer,
        as_attachment=True,
        download_name=f"Bank_Reports_{from_date}_to_{to_date}.zip",
        mimetype="application/zip"
    )
@app.route('/bank_settings', methods=['GET', 'POST'])
def bank_settings():

    if 'id' not in session:
        return redirect(url_for('login'))

    user_id = session['id']
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    if request.method == "POST":

        account_holder_name = request.form.get("account_holder_name")
        bank_name = request.form.get("bank_name")
        branch_name = request.form.get("branch_name")
        account_no = request.form.get("account_no")
        ifsc_code = request.form.get("ifsc_code").upper()

        cursor.execute("""
            SELECT id
            FROM bank_settings
            WHERE user_id=%s
        """, (user_id,))

        exists = cursor.fetchone()

        if exists:

            cursor.execute("""
                UPDATE bank_settings
                SET
                    account_holder_name=%s,
                    bank_name=%s,
                    branch_name=%s,
                    account_no=%s,
                    ifsc_code=%s
                WHERE user_id=%s
            """, (
                account_holder_name,
                bank_name,
                branch_name,
                account_no,
                ifsc_code,
                user_id
            ))

            flash("Bank details updated successfully.", "success")

        else:

            cursor.execute("""
                INSERT INTO bank_settings
                (
                    user_id,
                    account_holder_name,
                    bank_name,
                    branch_name,
                    account_no,
                    ifsc_code
                )
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (
                user_id,
                account_holder_name,
                bank_name,
                branch_name,
                account_no,
                ifsc_code
            ))

            flash("Bank details saved successfully.", "success")

        mysql.connection.commit()

    cursor.execute("""
        SELECT *
        FROM bank_settings
        WHERE user_id=%s
    """, (user_id,))

    bank = cursor.fetchone()

    cursor.close()

    return render_template(
        "settings/bank_settings.html",
        bank=bank
    )
# -------------------------------
# HEAD ROUTES
# -------------------------------
@app.route("/about", methods=["GET", "POST"])
def about():
    if request.method == "POST":
        name = request.form.get("name")
        email = session.get("email")   # ✅ login झाल्यामुळे session मधून email घ्या
        message = request.form.get("message")

        if not name or not message:
            flash("सर्व फील्ड भरा.", "warning")
            return redirect(url_for("about"))

        try:
            msg = EmailMessage()
            msg["Subject"] = f"New Feedback from {name}"
            msg["From"] = EMAIL_ADDRESS
            msg["To"] = SUPPORT_EMAIL
            msg.set_content(f"Name: {name}\nEmail: {email}\n\nMessage:\n{message}")

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                smtp.send_message(msg)

            flash("Feedback पाठवला. धन्यवाद!", "success")
        except Exception as e:
            logging.exception("Feedback send error")
            flash("Feedback पाठवताना error आला.", "danger")

    return render_template("head/about.html")

@app.route("/milk_prediction")
def milk_prediction_page():

    if 'id' not in session:
        return redirect(url_for("login"))

    cursor = SafeCursor(mysql.connection.cursor())

    # The prediction API is keyed by vendor_id (not the vendors.id PK).
    cursor.execute(
        "SELECT vendor_id AS id, name FROM vendors WHERE user_id=%s ORDER BY vendor_id",
        (session['id'],)
    )

    vendors = cursor.fetchall()

    return render_template("ai/milk_prediction.html", vendors=vendors)


@app.route("/api/predict_milk/<int:vendor_id>")
def api_predict_milk(vendor_id):

    if 'id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT date,
               SUM(CASE WHEN slot='morning' THEN quantity ELSE 0 END) AS morning,
               SUM(CASE WHEN slot='evening' THEN quantity ELSE 0 END) AS evening
        FROM milk_collection
        WHERE vendor_id=%s AND user_id=%s
        GROUP BY date
        ORDER BY date DESC
        LIMIT 30
    """, (vendor_id, session['id']))

    data = cursor.fetchall()

    if not data:
        return jsonify({
            "morning_prediction": None,
            "evening_prediction": None
        })

    import pandas as pd

    df = pd.DataFrame(data)

    morning_df = df[["date","morning"]].rename(columns={"morning":"quantity"})
    evening_df = df[["date","evening"]].rename(columns={"evening":"quantity"})

    morning_prediction = predict_milk(morning_df)
    evening_prediction = predict_milk(evening_df)

    return jsonify({
        "morning_prediction": morning_prediction,
        "evening_prediction": evening_prediction
    })
    
    
@app.route("/vendor_performance")
def vendor_performance():

    if 'id' not in session:
        return redirect(url_for("login"))

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT vendor_id, quantity
        FROM milk_collection
        WHERE user_id=%s
    """, (session['id'],))

    data = cursor.fetchall()

    vendors = {}

    for row in data:
        vendors.setdefault(row["vendor_id"], []).append(row["quantity"])

    result = []

    for vid, values in vendors.items():

        analysis = analyze_vendor(values)

        if analysis:
            result.append({
                "vendor_id": vid,
                "average": analysis["average"],
                "consistency": analysis["consistency"],
                "rating": analysis["rating"]
            })

    return render_template(
        "ai/vendor_performance.html",
        data=result
    )

@app.route("/settings")
def settings():
    uid = int(session["id"])
    charts = list_charts(uid)
    return render_template(
        "settings.html",
        fsn=get_settings(uid),
        active_charts=[c for c in charts if c["status"] == "active"],
    )


@app.route("/learn")
def learn():
    return render_template("head/learn.html")


@app.route("/account")
def account():
    if "id" not in session:
        flash("कृपया login करा.", "warning")
        return redirect(url_for("login"))

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT
            id,
            email,
            dairy_name,
            phone,
            dairy_code
        FROM users
        WHERE id = %s
    """, (session["id"],))

    user = cursor.fetchone()

    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("logout"))

    return render_template("head/account.html", user=user)


@app.route("/delete_account", methods=["POST"])
def delete_account():
    if "id" not in session or session.get("role") != "owner":
        flash("Unauthorized request", "danger")
        return redirect(url_for("login"))

    if rate_limited([("delete_account", session["id"], 5, 3600)]):
        flash(RATE_LIMIT_MESSAGE, "danger")
        return redirect(url_for("account"))

    # Irreversible (cascades to every vendor, entry and payment record), so
    # the owner must re-enter the password.
    cursor = SafeCursor(mysql.connection.cursor())
    cursor.execute("SELECT password FROM users WHERE id = %s", (session["id"],))
    row = cursor.fetchone()
    if not row or not check_password_hash(row["password"], request.form.get("password") or ""):
        cursor.close()
        flash("पासवर्ड चुकीचा आहे. खाते delete केले नाही.", "danger")
        return redirect(url_for("account"))

    audit_log(session["id"], "account_delete", f"email={session.get('email')}")
    cursor.execute("DELETE FROM users WHERE id = %s", (session["id"],))
    mysql.connection.commit()
    session.clear()
    flash("Account delete केला.", "success")
    return redirect(url_for("signup"))


@app.route("/contact")
def contact():
    return render_template("head/contact.html")





@app.route("/profile")
def profile():
    return render_template("head/profile.html")  # नसेल तर dummy page बनव

@app.route("/analytics")
def analytics():
    return render_template("head/analytics.html")  # नसेल तर dummy page बनव

@app.route("/terms")
def terms():
    return render_template("head/terms.html")  # Terms & Conditions

@app.route("/privacy")
def privacy():
    return render_template("head/privacy.html")  # Privacy Policy




from flask import send_from_directory

@app.route("/manifest.json")
def manifest():
    response = send_from_directory("static", "manifest.json",
                                   mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache"
    return response

@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static/icons", "icon-192.png", mimetype="image/png")

@app.route("/service-worker.js")
def service_worker():
    # Served from the site root so the worker may control every page
    # (a worker registered under /static/ can only control /static/*).
    response = send_from_directory("static", "service-worker.js",
                                   mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response

@app.after_request
def tag_page_cache_scope(response):
    """Tells the service worker whose page this is, so offline page copies
    are kept per account (an owner's cached pages are never shown to staff,
    a customer or another dairy on the same device)."""
    if session.get("loggedin") and response.mimetype == "text/html":
        role = session.get("role")
        try:
            uid = int(session.get("id"))
        except (TypeError, ValueError):
            return response
        if role == "owner":
            scope = f"o{uid}"
        elif role == "staff":
            scope = f"s{uid}_{session.get('staff_id')}"
        elif role == "customer":
            scope = f"c{uid}_{session.get('vendor_id')}"
        else:
            return response
        response.headers["X-DP-Cache-Scope"] = scope
    return response


@app.after_request
def disable_cache(response):

    if request.path.startswith("/static") \
       or request.path.startswith("/i18n/") \
       or request.path == "/manifest.json" \
       or request.path == "/service-worker.js":
        return response

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    return response


# ----------------------------------------------------------------------
# Backups
#
# These run mysqldump / mysql against the WHOLE multi-tenant database, so
# they are restricted to platform administrators (DAIRYMITR_ADMIN_EMAILS).
# Previously any logged-in user — including staff of any dairy — could
# restore the entire database (wiping every other dairy's data) or dump it.
# ----------------------------------------------------------------------
BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+\.sql\.gz$")


def _require_platform_admin():
    if not is_platform_admin():
        return json_error(
            "Backup/restore फक्त प्लॅटफॉर्म ॲडमिनसाठी उपलब्ध आहे.", 403, code="forbidden")
    return None


@app.route("/backup_my_data", methods=["POST"])
def backup_my_data():

    denied = _require_platform_admin()
    if denied:
        return denied

    try:
        filename = create_backup(backup_type="manual", user_id=session["id"])
    except Exception:
        logging.exception("backup failed")
        return json_error("Backup तयार करता आला नाही.", 500, code="backup_failed")

    audit_log(session["id"], "backup_create", filename)
    return jsonify({"status": "success", "file": filename})


@app.route("/restore_backup/<filename>", methods=["POST"])
def restore_backup_route(filename):

    denied = _require_platform_admin()
    if denied:
        return denied

    if not BACKUP_NAME_RE.match(filename) or filename not in list_backups():
        return json_error("Backup file सापडली नाही.", 404, code="not_found")

    try:
        restore_backup(filename)
    except Exception:
        logging.exception("restore failed for %s", filename)
        return json_error("Restore करता आला नाही.", 500, code="restore_failed")

    audit_record("backup_restore", "backup", filename)
    return {"status": "success", "message": "Backup restored successfully"}


@app.route("/admin_full_backup", methods=["POST"])
def admin_full_backup():

    denied = _require_platform_admin()
    if denied:
        return denied

    try:
        filename = create_full_backup()
    except Exception:
        logging.exception("full backup failed")
        return json_error("Backup तयार करता आला नाही.", 500, code="backup_failed")

    audit_log(session["id"], "backup_full", filename)
    return jsonify({"status": "success", "file": filename})


@app.route("/backup_history")
def backup_history():

    if not is_platform_admin():
        return jsonify({"files": [], "admin": False})

    return jsonify({"files": list_backups(), "admin": True})


@app.route("/backup_page")
def backup_page():
    return render_template("backup.html", is_admin=is_platform_admin())



@app.template_filter('date_indian')
def date_indian(value):

    if not value:
        return ""

    if isinstance(value, str):
        try:
            value = datetime.strptime(value, "%Y-%m-%d")
        except:
            return value

    return value.strftime("%d/%m/%Y")






#========================================================================================================================
#========================================================Customer========================================================
#========================================================================================================================
@app.route('/customer/login', methods=['GET', 'POST'])
def customer_login():

    if request.method == 'POST':

        dairy_id = (request.form.get('dairy_id') or '').strip().upper()
        phone = (request.form.get('phone') or '').strip()

        # Dairy code + phone is a weak credential (the code is not secret),
        # so throttle guessing per IP and per dairy.
        customer_rules = [
            ("customer_fail_ip", client_ip(), 20, 900),
            ("customer_fail_dairy", dairy_id, 50, 900),
        ]
        if failures_blocked(customer_rules):
            flash(RATE_LIMIT_MESSAGE, "danger")
            return render_template("customer/login.html"), 429

        cursor = SafeCursor(mysql.connection.cursor())

        cursor.execute("""
            SELECT
                v.id,
                v.vendor_id,
                v.name,
                v.phone,
                v.user_id,
                v.preferred_language,
                u.dairy_name,
                u.dairy_code
            FROM vendors v
            JOIN users u
                ON v.user_id = u.id
            WHERE
                u.dairy_code=%s
                AND v.phone=%s
            LIMIT 1
        """, (dairy_id, phone))

        customer = cursor.fetchone()

        if customer:

            session.clear()
            session.permanent = True

            session['loggedin'] = True
            session['role'] = 'customer'

            # ✅ Bug 3/6 fix: keep 'id' consistent with owner/staff sessions
            # (owner_id is the dairy owner this customer belongs to)
            session['id'] = customer['user_id']

            session['customer_id'] = customer['id']
            session['vendor_id'] = customer['vendor_id']
            session['vendor_db_id'] = customer['id']
            session['owner_id'] = customer['user_id']
            session['customer_name'] = customer['name']
            session['dairy_name'] = customer['dairy_name']
            apply_account_language(customer.get('preferred_language'))

            flash("Customer Login Successful", "success")

            return redirect(url_for("customer_dashboard"))

        record_failures(customer_rules)
        flash("Invalid Dairy ID or Mobile Number", "danger")


    return render_template("customer/login.html")

@app.route("/customer/dashboard")
def customer_dashboard():

    if session.get("role") != "customer":
        flash("Please login first.", "warning")
        return redirect(url_for("customer_login"))

    cursor = SafeCursor(mysql.connection.cursor())

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

    cursor.execute("""
        SELECT

            COALESCE(SUM(
                CASE
                    WHEN milk_type='cow'
                    AND slot='morning'
                    THEN quantity
                END
            ),0) AS cow_morning,

            COALESCE(SUM(
                CASE
                    WHEN milk_type='cow'
                    AND slot='evening'
                    THEN quantity
                END
            ),0) AS cow_evening,

            COALESCE(SUM(
                CASE
                    WHEN milk_type='buffalo'
                    AND slot='morning'
                    THEN quantity
                END
            ),0) AS buffalo_morning,

            COALESCE(SUM(
                CASE
                    WHEN milk_type='buffalo'
                    AND slot='evening'
                    THEN quantity
                END
            ),0) AS buffalo_evening

        FROM (
            SELECT vendor_id, user_id, date, slot, milk_type, quantity
            FROM milk_collection
            UNION ALL
            SELECT vendor_id, user_id, collection_date AS date,
                   slot COLLATE utf8mb4_0900_ai_ci AS slot,
                   milk_type COLLATE utf8mb4_0900_ai_ci AS milk_type, quantity
            FROM fat_snf_milk_collection
        ) AS milk_all

        WHERE
            vendor_id = %s
            AND user_id = %s
            AND date = %s

    """, (
        session["vendor_id"],
        session["owner_id"],
        today
    ))

    today_data = cursor.fetchone()

    if not today_data:
        today_data = {}

    today_data["cow_morning"] = round(today_data.get("cow_morning", 0), 1)
    today_data["cow_evening"] = round(today_data.get("cow_evening", 0), 1)

    today_data["buffalo_morning"] = round(today_data.get("buffalo_morning", 0), 1)
    today_data["buffalo_evening"] = round(today_data.get("buffalo_evening", 0), 1)

    today_data["cow_total"] = round(
        today_data["cow_morning"] + today_data["cow_evening"], 1
    )

    today_data["buffalo_total"] = round(
        today_data["buffalo_morning"] + today_data["buffalo_evening"], 1
    )

    return render_template(
        "customer/dashboard.html",
        today=today_data
    )
@app.route("/customer/profile")
def customer_profile():

    if session.get("role") != "customer":
        flash("Please login first.", "warning")
        return redirect(url_for("customer_login"))

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT
            id,
            email,
            phone,
            dairy_name,
            dairy_code
        FROM users
        WHERE id = %s
    """, (
        session["owner_id"],
    ))

    customer = cursor.fetchone()

    if not customer:
        flash("Profile not found.", "danger")
        return redirect(url_for("customer_dashboard"))

    return render_template(
        "customer/profile.html",
        customer=customer
    )
@app.route("/customer/milk-history")
def customer_milk_history():

    if session.get("role") != "customer":
        return redirect(url_for("customer_login"))

    cursor = SafeCursor(mysql.connection.cursor())

    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")

    sql = """
    SELECT
        date,

        SUM(CASE WHEN slot='morning' AND milk_type='cow'
            THEN quantity ELSE 0 END) cow_morning,

        SUM(CASE WHEN slot='morning' AND milk_type='buffalo'
            THEN quantity ELSE 0 END) buffalo_morning,

        SUM(CASE WHEN slot='evening' AND milk_type='cow'
            THEN quantity ELSE 0 END) cow_evening,

        SUM(CASE WHEN slot='evening' AND milk_type='buffalo'
            THEN quantity ELSE 0 END) buffalo_evening

    FROM (
        SELECT vendor_id, user_id, date, slot, milk_type, quantity
        FROM milk_collection
        UNION ALL
        SELECT vendor_id, user_id, collection_date AS date,
                   slot COLLATE utf8mb4_0900_ai_ci AS slot,
                   milk_type COLLATE utf8mb4_0900_ai_ci AS milk_type, quantity
        FROM fat_snf_milk_collection
    ) AS milk_all

    WHERE
        vendor_id=%s
        AND user_id=%s
    """

    params = [
        session["vendor_id"],
        session["owner_id"]
    ]

    if from_date:
        sql += " AND date >= %s"
        params.append(from_date)

    if to_date:
        sql += " AND date <= %s"
        params.append(to_date)

    sql += """
    GROUP BY date
    ORDER BY date DESC
    """

    cursor.execute(sql, tuple(params))

    records = cursor.fetchall()

    for r in records:

        r["cow_morning"] = round(r["cow_morning"],1)
        r["buffalo_morning"] = round(r["buffalo_morning"],1)
        r["cow_evening"] = round(r["cow_evening"],1)
        r["buffalo_evening"] = round(r["buffalo_evening"],1)

        r["total"] = round(
            r["cow_morning"] +
            r["buffalo_morning"] +
            r["cow_evening"] +
            r["buffalo_evening"],1
        )

    return render_template(
        "customer/milk_history.html",
        records=records
    )


@app.route("/customer/payment-history")
def customer_payment_history():
    return "<h2>Payment History Coming Soon</h2>"


from datetime import date

@app.route('/receipt/<int:vendor_id>', methods=['GET', 'POST'])
def generate_receipt(vendor_id):

    # ----------------------------
    # Login Check (customers can only ever see their own vendor record)
    # ----------------------------
    is_customer = session.get("role") == "customer"
    if is_customer:
        user_id = session["owner_id"]
        vendor_id = session["vendor_id"]
    elif "id" in session:
        user_id = int(session["id"])
    else:
        flash("Please login first.", "warning")
        return redirect(url_for("login"))

    # FAT/SNF mode: the daily FAT/SNF receipt (same view for owner and customer)
    if is_fat_snf(user_id) and request.args.get("legacy") != "1":
        return render_receipt(
            user_id, vendor_id,
            base_template="customer/customer_base.html" if is_customer else "base.html",
            customer=is_customer)

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # ----------------------------
    # Fetch vendor info
    # ----------------------------
    cursor.execute("""
        SELECT
            vendor_id,
            user_id,
            name,
            address,
            milk_type,
            phone,
            ifsc_code,
            account_no
        FROM vendors
        WHERE vendor_id = %s AND user_id = %s
    """, (vendor_id, user_id))

    vendor = cursor.fetchone()

    if not vendor:
        cursor.close()
        flash('Vendor not found.', 'danger')
        return redirect(url_for('customer_dashboard' if is_customer else 'vendor_list'))

    receipt = None
    from_date = None
    to_date = None

    if request.method == 'POST':
        from_date = request.form.get('from_date')
        to_date = request.form.get('to_date')

        if not from_date or not to_date:
            flash('Please select both From Date and To Date.', 'warning')
        else:

            # ----------------------------
            # Daily milk collection
            # ----------------------------
            cursor.execute("""
                SELECT
                    date,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN milk_type='cow'
                                AND slot='morning'
                                THEN quantity
                            END
                        ), 0
                    ) AS cow_morning,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN milk_type='cow'
                                AND slot='evening'
                                THEN quantity
                            END
                        ), 0
                    ) AS cow_evening,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN milk_type='buffalo'
                                AND slot='morning'
                                THEN quantity
                            END
                        ), 0
                    ) AS buffalo_morning,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN milk_type='buffalo'
                                AND slot='evening'
                                THEN quantity
                            END
                        ), 0
                    ) AS buffalo_evening

                FROM milk_collection
                WHERE vendor_id = %s
                  AND user_id = %s
                  AND date BETWEEN %s AND %s
                GROUP BY date
                ORDER BY date
            """, (vendor_id, user_id, from_date, to_date))

            daily_rows = cursor.fetchall()

            if not daily_rows:
                flash(
                    "No milk collection found for selected period.",
                    "warning"
                )

            total_cow_morning = 0
            total_cow_evening = 0
            total_buffalo_morning = 0
            total_buffalo_evening = 0

            for row in daily_rows:

                row['cow_morning'] = round(
                    float(row['cow_morning']), 1
                )

                row['cow_evening'] = round(
                    float(row['cow_evening']), 1
                )

                row['buffalo_morning'] = round(
                    float(row['buffalo_morning']), 1
                )

                row['buffalo_evening'] = round(
                    float(row['buffalo_evening']), 1
                )

                # Display-friendly date (dd-mm-yyyy)
                row['display_date'] = row['date'].strftime(
                    "%d/%m/%Y"
                )

                total_cow_morning += row['cow_morning']
                total_cow_evening += row['cow_evening']
                total_buffalo_morning += row['buffalo_morning']
                total_buffalo_evening += row['buffalo_evening']

            total_cow_morning = round(
                total_cow_morning, 1
            )

            total_cow_evening = round(
                total_cow_evening, 1
            )

            total_buffalo_morning = round(
                total_buffalo_morning, 1
            )

            total_buffalo_evening = round(
                total_buffalo_evening, 1
            )

            total_cow_milk = round(
                total_cow_morning + total_cow_evening,
                1
            )

            total_buffalo_milk = round(
                total_buffalo_morning + total_buffalo_evening,
                1
            )

            # ----------------------------
            # Amounts: rate resolved per entry date exactly like the payment
            # page (vendor special rate first, else the default rate for that
            # animal). Previously a vendor rate row with an empty cow/buffalo
            # rate produced ₹0 instead of falling back to the default.
            # ----------------------------
            cursor.execute("""
                SELECT date, milk_type, SUM(quantity) AS qty
                FROM milk_collection
                WHERE vendor_id = %s AND user_id = %s AND date BETWEEN %s AND %s
                GROUP BY date, milk_type
            """, (vendor_id, user_id, from_date, to_date))

            cow_amount = 0.0
            buffalo_amount = 0.0
            for r in cursor.fetchall():
                amt = float(r['qty'] or 0) * get_vendor_rate(
                    cursor, vendor_id, r['milk_type'], r['date'], user_id=user_id)
                if r['milk_type'] == 'cow':
                    cow_amount += amt
                else:
                    buffalo_amount += amt

            cow_amount = round(cow_amount, 2)
            buffalo_amount = round(buffalo_amount, 2)

            # Displayed rate: effective average for the period (equals the
            # single rate when the rate did not change inside the period).
            cow_rate = (round(cow_amount / total_cow_milk, 2) if total_cow_milk
                        else get_vendor_rate(cursor, vendor_id, 'cow', to_date, user_id=user_id))
            buffalo_rate = (round(buffalo_amount / total_buffalo_milk, 2) if total_buffalo_milk
                            else get_vendor_rate(cursor, vendor_id, 'buffalo', to_date,
                                                 user_id=user_id))

            total_milk_amount = round(
                cow_amount + buffalo_amount,
                2
            )

            # ----------------------------
            # Advance ledger — same rule as every other payment screen:
            # only the advance DEDUCTED in this period is subtracted.
            # (Previously this queried a non-existent column and crashed.)
            # ----------------------------
            cursor.execute("""
                SELECT COALESCE(SUM(amount), 0) AS period_deduction
                FROM advance_transactions
                WHERE vendor_id = %s
                  AND user_id = %s
                  AND transaction_type = 'deduction'
                  AND transaction_date BETWEEN %s AND %s
            """, (
                vendor_id,
                user_id,
                from_date,
                to_date
            ))

            total_advance = round(
                float((cursor.fetchone() or {}).get('period_deduction') or 0),
                2
            )

            # ----------------------------
            # Food Sack
            # (JOIN with food_sack_rates)
            # ----------------------------
            cursor.execute("""
                SELECT
                    fs.date,
                    r.name,
                    fs.sack_qty,
                    fs.sack_rate,
                    fs.total_cost
                FROM food_sack fs
                JOIN food_sack_rates r
                    ON r.id = fs.sack_rate_id
                WHERE fs.vendor_id = %s
                  AND fs.user_id = %s
                  AND fs.date BETWEEN %s AND %s
                ORDER BY fs.date
            """, (
                vendor_id,
                user_id,
                from_date,
                to_date
            ))

            food_sack_rows = cursor.fetchall()

            total_food_sack = 0

            for fs in food_sack_rows:

                fs['sack_qty'] = round(
                    float(fs['sack_qty']),
                    1
                )

                fs['sack_rate'] = round(
                    float(fs['sack_rate']),
                    2
                )

                fs['total_cost'] = round(
                    float(fs['total_cost']),
                    2
                )

                fs['display_date'] = fs['date'].strftime(
                    "%d/%m/%Y"
                )

                total_food_sack += fs['total_cost']

            total_food_sack = round(
                total_food_sack,
                2
            )

            # ----------------------------
            # Final Payable
            # ----------------------------
            net_payable = round(
                total_milk_amount
                - total_advance
                - total_food_sack,
                2
            )

            # ----------------------------
            # Receipt Data
            # ----------------------------
            receipt = {
                'daily_rows': daily_rows,

                'total_cow_morning':
                    total_cow_morning,

                'total_cow_evening':
                    total_cow_evening,

                'total_buffalo_morning':
                    total_buffalo_morning,

                'total_buffalo_evening':
                    total_buffalo_evening,

                'total_cow_milk':
                    total_cow_milk,

                'total_buffalo_milk':
                    total_buffalo_milk,

                'cow_rate':
                    cow_rate,

                'buffalo_rate':
                    buffalo_rate,

                'cow_amount':
                    cow_amount,

                'buffalo_amount':
                    buffalo_amount,

                'total_milk_amount':
                    total_milk_amount,

                'food_sack_rows':
                    food_sack_rows,

                'total_food_sack':
                    total_food_sack,

                'total_advance':
                    total_advance,

                'net_payable':
                    net_payable,

                'from_date':
                    from_date,

                'to_date':
                    to_date,

                'print_date':
                    today_ist().strftime('%d/%m/%Y')
            }

    cursor.close()

    return render_template(
        'customer/receipt.html',
        vendor=vendor,
        receipt=receipt,
        from_date=from_date,
        to_date=to_date
    )

@app.route('/customer/receipts')
def customer_receipts():


    if 'customer_id' not in session:
        flash('Please login first.', 'warning')
        return redirect(url_for('customer_login'))

    return redirect(
        url_for(
            'generate_receipt',
            vendor_id=session['vendor_id']
        )
    )

@app.route("/customer/food-sack")
def customer_food_sack():
    return "<h2>Food Sack Coming Soon</h2>"


@app.route("/customer/advance")
def customer_advance():
    return "<h2>Advance Coming Soon</h2>"

@app.route('/customer/notifications')
def customer_notifications():

    if session.get("role") != "customer":
        flash("Please login first.", "warning")
        return redirect(url_for("customer_login"))

    cursor = SafeCursor(mysql.connection.cursor())

    # Notification open होताच Read कर
    cursor.execute("""
        UPDATE customer_notifications
        SET is_read=1
        WHERE user_id=%s
        AND vendor_id=%s
        AND is_read=0
    """, (
        session["owner_id"],
        session["vendor_id"]
    ))

    mysql.connection.commit()

    cursor.execute("""
        SELECT
            id,
            type,
            title,
            message,
            is_read,
            created_at
        FROM customer_notifications
        WHERE user_id=%s
        AND vendor_id=%s
        ORDER BY created_at DESC
    """, (
        session["owner_id"],
        session["vendor_id"]
    ))

    notifications = cursor.fetchall()

    cursor.close()

    return render_template(
        "customer/notifications.html",
        notifications=notifications
    )


@app.route('/vapid_public_key')
def vapid_public_key():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route('/subscribe', methods=['POST'])
def subscribe():

    if session.get("role") != "customer":
        return jsonify({"message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    endpoint = data.get("endpoint")
    keys = data.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if (not endpoint or not p256dh or not auth
            or not str(endpoint).startswith("https://") or len(str(endpoint)) > 700
            or len(str(p256dh)) > 255 or len(str(auth)) > 255):
        return jsonify({"message": "Invalid subscription data"}), 400

    user_id = session["owner_id"]
    vendor_id = session["vendor_id"]

    cursor = SafeCursor(mysql.connection.cursor())


    cursor.execute(
        "SELECT id FROM push_subscriptions WHERE endpoint=%s",
        (endpoint,)
    )
    existing = cursor.fetchone()

    if existing:
        cursor.execute("""
            UPDATE push_subscriptions
            SET user_id=%s,
                vendor_id=%s,
                p256dh=%s,
                auth=%s
            WHERE endpoint=%s
        """, (
            user_id,
            vendor_id,
            p256dh,
            auth,
            endpoint
        ))
    else:
        cursor.execute("""
            INSERT INTO push_subscriptions
            (user_id, vendor_id, endpoint, p256dh, auth)
            VALUES (%s,%s,%s,%s,%s)
        """, (
            user_id,
            vendor_id,
            endpoint,
            p256dh,
            auth
        ))

    mysql.connection.commit()


    cursor.close()

    return jsonify({"message": "Subscribed"}), 200


@app.context_processor
def customer_notification_context():

    if session.get("role") == "customer":

        return {
            "unread_count": get_unread_notification_count(
                session["owner_id"],
                session["vendor_id"]
            )
        }

    return {"unread_count": 0}

@app.route("/customer/logout")
def customer_logout():
    # ✅ Bug 1 fix: clear the whole session, not just 3 keys
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for("customer_login"))







# ------------------------------
# Healthcheck (simple)
# ------------------------------
@app.route('/healthcheck')
def healthcheck():
    if request.args.get("deep") != "1":
        return "OK"
    # Deep check: database reachable and migrations applied. Reveals no data.
    try:
        from migrations.runner import load_migrations
        cur = db_cursor()
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
        cur.execute("SELECT version FROM schema_migrations")
        applied = {r["version"] for r in cur.fetchall()}
        cur.close()
        pending = [m.VERSION for m in load_migrations() if m.VERSION not in applied]
        status = 200 if not pending else 503
        return jsonify({"database": "ok",
                        "migrations": "up_to_date" if not pending else "pending",
                        "pending": pending}), status
    except Exception:
        logging.exception("deep healthcheck failed")
        return jsonify({"database": "unavailable"}), 503


# ==========================================================
# ==========================================================
#                 DAIRYMITR AI SYSTEM
# ==========================================================
# ==========================================================


# ==========================================================
# 1. DAIRYMITR AI SYSTEM INSTRUCTION
# ==========================================================

DAIRYMITR_AI_INSTRUCTION = """

You are DairyPro AI, the intelligent AI assistant of DairyPro.

DairyPro is a dairy management platform used by dairy owners
and staff to manage milk collection, vendors, payments,
advances, food sacks, milk rates and dairy reports.

==============================================================
LANGUAGE
==============================================================

You understand:

- Marathi
- Marathi written in English letters
- Hindi
- English

Always reply in the same language used by the user.

Examples:

User:
"आज किती दूध जमा झालं?"

Reply naturally in Marathi.

User:
"aaj kiti dudh jama zala?"

Reply naturally in Marathi/Marathi-English.

User:
"How much milk was collected today?"

Reply in English.

==============================================================
IMPORTANT DATABASE RULE
==============================================================

You have access to controlled DairyPro database tools.

When the user asks for ACTUAL DairyPro data:

NEVER invent numbers.

NEVER guess records.

NEVER create fake vendors.

NEVER create fake payments.

NEVER create fake milk collection.

NEVER create fake advance.

NEVER create fake food sack data.

Use the appropriate database tool.

Only use information returned by the database tool.

If the required database information is unavailable,
clearly tell the user that the required data is unavailable.

==============================================================
AVAILABLE DATABASE CAPABILITIES
==============================================================

You have controlled tools for:

1. Today's milk
2. Date-wise milk collection
3. Vendor-wise milk
4. Vendor information
5. Vendor count
6. Vendor summary
7. Payments
8. Advance
9. Food sack
10. Dairy daily summary
11. Dairy monthly summary
12. Dairy performance

==============================================================
MILK COLLECTION
==============================================================

Use milk tools for questions such as:

- आज किती दूध जमा झालं?
- आजचे दूध किती आहे?
- morning milk किती?
- evening milk किती?
- cow milk किती?
- buffalo milk किती?
- आजचे cow आणि buffalo milk किती?
- काल किती दूध जमा झाले?
- या महिन्यात किती दूध जमा झाले?
- vendor wise milk
- vendor X ने किती दूध दिले?
- date wise milk collection

For actual numbers use database tools.

==============================================================
VENDORS
==============================================================

Use vendor tools for:

- माझ्याकडे किती vendors आहेत?
- माझे vendors किती आहेत?
- vendor list
- सर्व vendors दाखवा
- vendor ची माहिती
- vendor X ची माहिती
- vendor wise summary

Do not expose bank account or IFSC information unless
specifically required and authorized.

==============================================================
PAYMENTS
==============================================================

Use payment tools for:

- आज किती payment झाले?
- total payment
- vendor payment
- pending payment
- vendor X ला किती payment?
- या महिन्याचा payment
- payment summary

Use only actual database values.

==============================================================
ADVANCE
==============================================================

Use advance tools for:

- total advance
- vendor advance
- vendor X ने किती advance घेतला?
- आजचा advance
- advance summary

==============================================================
FOOD SACK
==============================================================

Use food sack tools for:

- किती food sack दिले?
- total sack
- vendor wise sack
- sack amount
- आजचे sack
- monthly sack summary

==============================================================
REPORTS
==============================================================

Use report tools for:

- आजचा dairy report
- daily summary
- monthly summary
- dairy performance
- today's business summary
- महिन्याचा summary

Present reports clearly.

==============================================================
SECURITY
==============================================================

Never reveal:

- database password
- database credentials
- API key
- Flask secret key
- session secrets
- SQL credentials
- internal implementation details

Never execute arbitrary SQL supplied by the user.

Never access another dairy owner's records.

All database queries MUST be restricted to:

session['id']

which represents the current DairyPro owner/user.

==============================================================
RESPONSE STYLE
==============================================================

Be:

- helpful
- professional
- friendly
- concise
- easy to understand

Use emojis when useful.

For numerical reports use clean formatting.

Example:

📊 आजचा Dairy Report

🥛 दूध:
Morning: 120 L
Evening: 105 L
Total: 225 L

👨‍🌾 Vendors:
Total: 48

💰 Payment:
₹XX,XXX

💳 Advance:
₹X,XXX

🌾 Food Sack:
XX sacks

==============================================================
IMPORTANT
==============================================================

Never claim database access unless a database tool actually
returned the requested data.

If a tool returns no records, say that no records were found.

Do not invent missing values.

"""


# ==========================================================
# 2. COMMON AI SECURITY HELPER
# ==========================================================

def dairymitr_ai_get_user_id():

    if not session.get("loggedin"):
        return None

    if session.get("role") not in ("owner", "staff"):
        return None

    user_id = session.get("id")

    try:
        return int(user_id)

    except (TypeError, ValueError):
        return None


# ==========================================================
# 3. DATE HELPER
# ==========================================================

def dairymitr_ai_today():

    return datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).date()


# ==========================================================
# 4. TODAY'S MILK
# ==========================================================

def get_today_milk():

    try:

        user_id = dairymitr_ai_get_user_id()

        if not user_id:

            return {
                "success": False,
                "error": "Unauthorized access."
            }

        today = dairymitr_ai_today()

        cursor = SafeCursor(
            mysql.connection.cursor()
        )

        cursor.execute(
            """
            SELECT
                slot,
                milk_type,
                SUM(quantity) AS total_quantity
            FROM milk_collection
            WHERE
                user_id=%s
                AND DATE(date)=%s
            GROUP BY
                slot,
                milk_type
            ORDER BY
                slot,
                milk_type
            """,
            (
                user_id,
                today
            )
        )

        rows = cursor.fetchall()

        cursor.close()

        morning_cow = 0.0
        morning_buffalo = 0.0

        evening_cow = 0.0
        evening_buffalo = 0.0

        for row in rows:

            slot = str(
                row.get("slot") or ""
            ).lower().strip()

            milk_type = str(
                row.get("milk_type") or ""
            ).lower().strip()

            quantity = float(
                row.get("total_quantity") or 0
            )

            if slot == "morning":

                if milk_type == "cow":
                    morning_cow += quantity

                elif milk_type == "buffalo":
                    morning_buffalo += quantity

            elif slot == "evening":

                if milk_type == "cow":
                    evening_cow += quantity

                elif milk_type == "buffalo":
                    evening_buffalo += quantity

        morning_total = (
            morning_cow +
            morning_buffalo
        )

        evening_total = (
            evening_cow +
            evening_buffalo
        )

        total = (
            morning_total +
            evening_total
        )

        return {

            "success": True,

            "date": str(today),

            "morning": {
                "cow_liters": round(
                    morning_cow, 2
                ),
                "buffalo_liters": round(
                    morning_buffalo, 2
                ),
                "total_liters": round(
                    morning_total, 2
                )
            },

            "evening": {
                "cow_liters": round(
                    evening_cow, 2
                ),
                "buffalo_liters": round(
                    evening_buffalo, 2
                ),
                "total_liters": round(
                    evening_total, 2
                )
            },

            "total_liters": round(
                total, 2
            )
        }

    except Exception:

        logging.exception(
            "get_today_milk failed"
        )

        return {
            "success": False,
            "error": "Unable to retrieve today's milk."
        }


# ==========================================================
# 5. DATE-WISE MILK COLLECTION
# ==========================================================

def get_milk_collection(
    start_date="",
    end_date="",
    vendor_name="",
    slot="",
    milk_type=""
):

    try:

        user_id = dairymitr_ai_get_user_id()

        if not user_id:

            return {
                "success": False,
                "error": "Unauthorized access."
            }

        today = dairymitr_ai_today()

        if not start_date:
            start_date = str(today)

        if not end_date:
            end_date = start_date

        query = """
            SELECT
                mc.date,
                mc.slot,
                mc.milk_type,
                SUM(mc.quantity) AS total_quantity,
                mc.vendor_id,
                v.name AS vendor_name
            FROM milk_collection mc
            LEFT JOIN vendors v
                ON v.vendor_id = mc.vendor_id
                AND v.user_id = mc.user_id
            WHERE
                mc.user_id=%s
                AND DATE(mc.date) BETWEEN %s AND %s
        """

        params = [
            user_id,
            start_date,
            end_date
        ]

        if vendor_name:

            query += """
                AND (
                    v.name LIKE %s
                    OR v.name_en LIKE %s
                )
            """

            search = f"%{vendor_name}%"

            params.extend([
                search,
                search
            ])

        if slot:

            query += """
                AND LOWER(mc.slot)=LOWER(%s)
            """

            params.append(slot)

        if milk_type:

            query += """
                AND LOWER(mc.milk_type)=LOWER(%s)
            """

            params.append(milk_type)

        query += """
            GROUP BY
                mc.date,
                mc.slot,
                mc.milk_type,
                mc.vendor_id,
                v.name
            ORDER BY
                mc.date DESC,
                mc.slot ASC
        """

        cursor = SafeCursor(
            mysql.connection.cursor()
        )

        cursor.execute(
            query,
            tuple(params)
        )

        rows = cursor.fetchall()

        cursor.close()

        result = []

        total = 0.0

        for row in rows:

            quantity = float(
                row.get("total_quantity") or 0
            )

            total += quantity

            result.append({

                "date": str(
                    row.get("date")
                ),

                "slot": row.get("slot"),

                "milk_type": row.get(
                    "milk_type"
                ),

                "vendor_id": row.get(
                    "vendor_id"
                ),

                "vendor_name": row.get(
                    "vendor_name"
                ),

                "quantity_liters": round(
                    quantity,
                    2
                )
            })

        return {

            "success": True,

            "start_date": str(start_date),

            "end_date": str(end_date),

            "records": result,

            "record_count": len(result),

            "total_liters": round(
                total,
                2
            )
        }

    except Exception:

        logging.exception(
            "get_milk_collection failed"
        )

        return {

            "success": False,

            "error":
                "Unable to retrieve milk collection."
        }


# ==========================================================
# 6. VENDORS
# ==========================================================

def get_vendors():

    try:

        user_id = dairymitr_ai_get_user_id()

        if not user_id:

            return {
                "success": False,
                "error": "Unauthorized access."
            }

        cursor = SafeCursor(
            mysql.connection.cursor()
        )

        cursor.execute(
            """
            SELECT
                vendor_id,
                name,
                name_en,
                address,
                milk_type,
                phone
            FROM vendors
            WHERE user_id=%s
            ORDER BY name ASC
            """,
            (user_id,)
        )

        rows = cursor.fetchall()

        cursor.close()

        vendors = []

        for row in rows:

            vendors.append({

                "vendor_id":
                    row.get("vendor_id"),

                "name":
                    row.get("name"),

                "name_en":
                    row.get("name_en"),

                "address":
                    row.get("address"),

                "milk_type":
                    row.get("milk_type"),

                "phone":
                    row.get("phone")
            })

        return {

            "success": True,

            "total_vendors":
                len(vendors),

            "vendors":
                vendors
        }

    except Exception:

        logging.exception(
            "get_vendors failed"
        )

        return {

            "success": False,

            "error":
                "Unable to retrieve vendor data."
        }


# ==========================================================
# 7. VENDOR SUMMARY
# ==========================================================

def get_vendor_summary(
    vendor_name="",
    start_date="",
    end_date=""
):

    try:

        user_id = dairymitr_ai_get_user_id()

        if not user_id:

            return {
                "success": False,
                "error": "Unauthorized access."
            }

        if not vendor_name:

            return {
                "success": False,
                "error": "Vendor name is required."
            }

        today = dairymitr_ai_today()

        if not start_date:
            start_date = str(today)

        if not end_date:
            end_date = start_date

        cursor = SafeCursor(
            mysql.connection.cursor()
        )

        search = f"%{vendor_name}%"

        cursor.execute(
            """
            SELECT
                v.vendor_id,
                v.name,
                v.name_en,
                v.address,
                v.milk_type,
                v.phone,

                COALESCE(
                    SUM(mc.quantity),
                    0
                ) AS total_milk

            FROM vendors v

            LEFT JOIN milk_collection mc
                ON mc.vendor_id=v.vendor_id
                AND mc.user_id=v.user_id
                AND DATE(mc.date)
                    BETWEEN %s AND %s

            WHERE
                v.user_id=%s
                AND (
                    v.name LIKE %s
                    OR v.name_en LIKE %s
                )

            GROUP BY
                v.vendor_id,
                v.name,
                v.name_en,
                v.address,
                v.milk_type,
                v.phone

            ORDER BY
                total_milk DESC

            LIMIT 20
            """,
            (
                start_date,
                end_date,
                user_id,
                search,
                search
            )
        )

        rows = cursor.fetchall()

        cursor.close()

        result = []

        for row in rows:

            result.append({

                "vendor_id":
                    row.get("vendor_id"),

                "name":
                    row.get("name"),

                "name_en":
                    row.get("name_en"),

                "address":
                    row.get("address"),

                "milk_type":
                    row.get("milk_type"),

                "phone":
                    row.get("phone"),

                "total_milk_liters":
                    round(
                        float(
                            row.get(
                                "total_milk"
                            ) or 0
                        ),
                        2
                    )
            })

        return {

            "success": True,

            "start_date":
                str(start_date),

            "end_date":
                str(end_date),

            "vendors":
                result
        }

    except Exception:

        logging.exception(
            "get_vendor_summary failed"
        )

        return {

            "success": False,

            "error":
                "Unable to retrieve vendor summary."
        }


# ==========================================================
# 8. PAYMENT TOOL
# ==========================================================
#
# Payments are not stored as a separate ledger; the payable amount is
# computed with the same rule as the payment page:
#   milk amount - food sack - advance deducted in the period.
# Money data is available to the owner only (staff never see payments).

def _ai_owner_only():
    if session.get("role") != "owner":
        return {"success": False,
                "error": "Payment/advance information is available to the dairy owner only."}
    return None


def _ai_range(start_date, end_date):
    today = dairymitr_ai_today()
    start = start_date or str(today)
    end = end_date or start
    return parse_date_range(start, end)


def _ai_vendors(user_id, vendor_name=""):
    vendors = list_vendors(user_id)
    if vendor_name:
        needle = vendor_name.strip().lower()
        vendors = [v for v in vendors if needle in (v.get("name") or "").lower()]
    return vendors


def get_payments(
    start_date="",
    end_date="",
    vendor_name=""
):

    try:

        user_id = dairymitr_ai_get_user_id()

        if not user_id:
            return {"success": False, "error": "Unauthorized access."}

        denied = _ai_owner_only()
        if denied:
            return denied

        start, end = _ai_range(start_date, end_date)
        statements = build_statements(
            user_id, start, end, _ai_vendors(user_id, vendor_name))

        records = [
            {
                "vendor_id": st["vendor"]["vendor_id"],
                "vendor_name": st["vendor"]["name"],
                "milk_amount": float(st["milk_total"]),
                "food_sack": float(st["food"]["total"]),
                "advance_deducted": float(st["period_deduction"]),
                "final_payable": float(st["final"]),
                "remaining_advance": float(st["remaining_advance"]),
            }
            for st in statements if st["has_activity"]
        ]

        return {
            "success": True,
            "start_date": str(start),
            "end_date": str(end),
            "total_payment": round(sum(r["final_payable"] for r in records), 2),
            "records": records,
            "record_count": len(records),
        }

    except Exception:

        logging.exception("get_payments failed")

        return {"success": False, "error": "Unable to retrieve payment data."}


# ==========================================================
# 9. ADVANCE TOOL (advance_transactions ledger)
# ==========================================================

def get_advance(
    start_date="",
    end_date="",
    vendor_name=""
):

    try:

        user_id = dairymitr_ai_get_user_id()

        if not user_id:
            return {"success": False, "error": "Unauthorized access."}

        denied = _ai_owner_only()
        if denied:
            return denied

        start, end = _ai_range(start_date, end_date)

        sql = """
            SELECT at.transaction_date, at.transaction_type, at.amount,
                   at.vendor_id, v.name AS vendor_name
            FROM advance_transactions at
            JOIN vendors v ON v.vendor_id = at.vendor_id AND v.user_id = at.user_id
            WHERE at.user_id = %s AND at.transaction_date BETWEEN %s AND %s
        """
        params = [user_id, start, end]

        if vendor_name:
            sql += " AND (v.name LIKE %s OR v.name_en LIKE %s)"
            like = "%" + vendor_name.replace("%", "").replace("_", "") + "%"
            params += [like, like]

        cursor = db_cursor()
        cursor.execute(sql + " ORDER BY at.transaction_date DESC, at.id DESC LIMIT 500",
                       params)
        rows = cursor.fetchall()
        cursor.close()

        records = [
            {
                "date": str(r["transaction_date"]),
                "type": r["transaction_type"],
                "amount": float(r["amount"]),
                "vendor_id": r["vendor_id"],
                "vendor_name": r["vendor_name"],
            }
            for r in rows
        ]

        return {
            "success": True,
            "start_date": str(start),
            "end_date": str(end),
            "total_advance": round(sum(r["amount"] for r in records
                                       if r["type"] == "advance"), 2),
            "total_deduction": round(sum(r["amount"] for r in records
                                         if r["type"] == "deduction"), 2),
            "records": records,
        }

    except Exception:

        logging.exception("get_advance failed")

        return {"success": False, "error": "Unable to retrieve advance data."}


# ==========================================================
# 10. FOOD SACK TOOL
# ==========================================================

def get_food_sack(
    start_date="",
    end_date="",
    vendor_name=""
):

    try:

        user_id = dairymitr_ai_get_user_id()

        if not user_id:
            return {"success": False, "error": "Unauthorized access."}

        start, end = _ai_range(start_date, end_date)

        sql = """
            SELECT fs.date, fs.sack_qty, fs.total_cost, r.name AS sack_name,
                   fs.vendor_id, v.name AS vendor_name
            FROM food_sack fs
            JOIN food_sack_rates r ON r.id = fs.sack_rate_id
            JOIN vendors v ON v.vendor_id = fs.vendor_id AND v.user_id = fs.user_id
            WHERE fs.user_id = %s AND fs.date BETWEEN %s AND %s
        """
        params = [user_id, start, end]

        if vendor_name:
            sql += " AND (v.name LIKE %s OR v.name_en LIKE %s)"
            like = "%" + vendor_name.replace("%", "").replace("_", "") + "%"
            params += [like, like]

        cursor = db_cursor()
        cursor.execute(sql + " ORDER BY fs.date DESC LIMIT 500", params)
        rows = cursor.fetchall()
        cursor.close()

        show_money = session.get("role") == "owner"
        records = []
        for r in rows:
            rec = {
                "date": str(r["date"]),
                "sack_name": r["sack_name"],
                "quantity": int(r["sack_qty"] or 0),
                "vendor_id": r["vendor_id"],
                "vendor_name": r["vendor_name"],
            }
            if show_money:
                rec["amount"] = round(float(r["total_cost"] or 0), 2)
            records.append(rec)

        result = {
            "success": True,
            "start_date": str(start),
            "end_date": str(end),
            "total_sacks": sum(r["quantity"] for r in records),
            "records": records,
        }
        if show_money:
            result["total_amount"] = round(sum(r.get("amount", 0) for r in records), 2)
        return result

    except Exception:

        logging.exception("get_food_sack failed")

        return {"success": False, "error": "Unable to retrieve food sack data."}


# ==========================================================
# 11. DAILY SUMMARY
# ==========================================================

def get_daily_summary():

    try:

        user_id = dairymitr_ai_get_user_id()

        if not user_id:

            return {
                "success": False,
                "error": "Unauthorized access."
            }

        today = dairymitr_ai_today()

        milk = get_today_milk()

        payments = get_payments(
            str(today),
            str(today)
        )

        advance = get_advance(
            str(today),
            str(today)
        )

        food = get_food_sack(
            str(today),
            str(today)
        )

        vendors = get_vendors()

        return {

            "success": True,

            "date":
                str(today),

            "milk":
                milk,

            "payments":
                payments,

            "advance":
                advance,

            "food_sack":
                food,

            "vendors":
                {
                    "total":
                        vendors.get(
                            "total_vendors",
                            0
                        )
                }
        }

    except Exception:

        logging.exception(
            "get_daily_summary failed"
        )

        return {

            "success": False,

            "error":
                "Unable to create daily summary."
        }


# ==========================================================
# 12. MONTHLY SUMMARY
# ==========================================================

def get_monthly_summary(
    year="",
    month=""
):

    try:

        user_id = dairymitr_ai_get_user_id()

        if not user_id:

            return {
                "success": False,
                "error": "Unauthorized access."
            }

        today = dairymitr_ai_today()

        if not year:
            year = today.year

        if not month:
            month = today.month

        year = int(year)
        month = int(month)

        start = date(
            year,
            month,
            1
        )

        if month == 12:

            end = date(
                year + 1,
                1,
                1
            ) - timedelta(
                days=1
            )

        else:

            end = date(
                year,
                month + 1,
                1
            ) - timedelta(
                days=1
            )

        start_string = str(start)
        end_string = str(end)

        milk = get_milk_collection(
            start_string,
            end_string
        )

        payments = get_payments(
            start_string,
            end_string
        )

        advance = get_advance(
            start_string,
            end_string
        )

        food = get_food_sack(
            start_string,
            end_string
        )

        return {

            "success": True,

            "year":
                year,

            "month":
                month,

            "start_date":
                start_string,

            "end_date":
                end_string,

            "milk":
                milk,

            "payments":
                payments,

            "advance":
                advance,

            "food_sack":
                food
        }

    except Exception:

        logging.exception(
            "get_monthly_summary failed"
        )

        return {

            "success": False,

            "error":
                "Unable to create monthly summary."
        }


# ==========================================================
# 13. DAIRY PERFORMANCE
# ==========================================================

def get_dairy_performance():

    try:

        user_id = dairymitr_ai_get_user_id()

        if not user_id:

            return {
                "success": False,
                "error": "Unauthorized access."
            }

        today = dairymitr_ai_today()

        month_start = date(
            today.year,
            today.month,
            1
        )

        milk = get_milk_collection(
            str(month_start),
            str(today)
        )

        vendors = get_vendors()

        return {

            "success": True,

            "period": {
                "start":
                    str(month_start),

                "end":
                    str(today)
            },

            "total_vendors":
                vendors.get(
                    "total_vendors",
                    0
                ),

            "milk":
                milk
        }

    except Exception:

        logging.exception(
            "get_dairy_performance failed"
        )

        return {

            "success": False,

            "error":
                "Unable to create dairy performance report."
        }


# ==========================================================
# 14. DAIRYMITR AI CHAT API
# ==========================================================

@app.route(
    "/api/chat",
    methods=["POST"]
)
def dairy_mitr_ai_chat():

    # ======================================================
    # LOGIN
    # ======================================================

    if not session.get("loggedin"):

        return jsonify({

            "success": False,

            "error":
                "Please login first."

        }), 401


    # ======================================================
    # ROLE
    # ======================================================

    if session.get("role") not in (
        "owner",
        "staff"
    ):

        return jsonify({

            "success": False,

            "error":
                "You are not authorized to use DairyPro AI."

        }), 403


    # ======================================================
    # GEMINI
    # ======================================================

    gemini_client = get_gemini_client()

    if gemini_client is None:

        return jsonify({

            "success": False,

            "error":
                "Gemini AI is not configured. "
                "Please check GEMINI_API_KEY."

        }), 500


    # ======================================================
    # REQUEST
    # ======================================================

    data = request.get_json(
        silent=True
    ) or {}

    message = str(
        data.get(
            "message",
            ""
        )
    ).strip()


    if not message:

        return jsonify({

            "success": False,

            "error":
                "Please enter a message."

        }), 400


    if len(message) > 4000:

        return jsonify({

            "success": False,

            "error":
                "Message is too long."

        }), 400


    if rate_limited([("ai_chat", session.get("id"), 40, 600)]):
        return jsonify({"success": False, "error": RATE_LIMIT_MESSAGE}), 429


    # ======================================================
    # GEMINI
    # ======================================================

    try:

        logging.info(
            "DairyPro AI request | user_id=%s",
            session.get("id")
        )


        chat = gemini_client.chats.create(

            model=GEMINI_MODEL,

            config=types.GenerateContentConfig(

                system_instruction=
                    DAIRYMITR_AI_INSTRUCTION,

                temperature=0.2,

                tools=[

                    get_today_milk,

                    get_milk_collection,

                    get_vendors,

                    get_vendor_summary,

                    get_payments,

                    get_advance,

                    get_food_sack,

                    get_daily_summary,

                    get_monthly_summary,

                    get_dairy_performance
                ]
            )
        )


        response = chat.send_message(
            message
        )


        ai_response = ""

        try:

            ai_response = (
                response.text or ""
            ).strip()

        except Exception:

            logging.exception(
                "Gemini response extraction failed"
            )


        if not ai_response:

            ai_response = (
                "माफ करा, मला यावेळी योग्य "
                "उत्तर तयार करता आले नाही."
            )


        # ==================================================
        # AUDIT
        # ==================================================

        try:

            audit_log(

                session.get("id"),

                "dairymitr_ai_chat",

                f"message_length={len(message)}"

            )

        except Exception:

            logging.exception(
                "AI audit log failed"
            )


        return jsonify({

            "success": True,

            "reply":
                ai_response,

            "response":
                ai_response

        }), 200


    except Exception as e:

        logging.exception(
            "DairyPro AI error"
        )

        return jsonify({

            "success": False,

            "error":
                "DairyPro AI सध्या उत्तर देऊ शकत नाही. कृपया थोड्या वेळाने पुन्हा प्रयत्न करा."

        }), 500










































# ==========================================================================
# ==========================================================================
#   DAIRYPRO: language switch, offline support routes, subscriptions
# ==========================================================================
# ==========================================================================

dp_log = logging.getLogger("dairypro")

# Bump when JS/CSS under static/ changes (also part of the service-worker
# cache name, so clients pick up new files).
DP_ASSET_VERSION = "2026.11.13"
app.jinja_env.globals["is_platform_admin"] = is_platform_admin
app.jinja_env.globals["support_email"] = SUPPORT_EMAIL


# ----------------------------------------------------------------------
# Language
# ----------------------------------------------------------------------
@app.route("/set-language", methods=["POST"])
def set_language():
    """Change the UI language. Stored in the session, in a one-year cookie
    (survives logout on this device) and on the account (follows the user
    to other devices)."""
    data = request.get_json(silent=True) if request.is_json else request.form
    lang = normalize_language((data or {}).get("lang"))
    if not lang:
        if wants_json():
            return json_error("Unsupported language.", 400, code="invalid")
        return redirect(request.referrer if _same_origin(request.referrer) else url_for("login"))
    session["lang"] = lang
    g._dp_lang = lang
    if session.get("loggedin"):
        remember_language_for_account(lang)
    if wants_json():
        response = jsonify({"success": True, "lang": lang,
                            "message": _t("Language changed.", lang=lang)})
    else:
        target = request.referrer if _same_origin(request.referrer) else url_for("dashboard")
        response = redirect(target)
    response.set_cookie(LANGUAGE_COOKIE, lang, max_age=365 * 86400, samesite="Lax",
                        secure=app.config.get("SESSION_COOKIE_SECURE", False), httponly=False)
    return response


@app.route("/i18n/<lang>.js")
def i18n_js(lang):
    """JavaScript dictionary for one language. Cached: the ?v= query string
    changes whenever the catalog changes."""
    lang = normalize_language(lang) or DEFAULT_LANGUAGE
    body = "window.DP_LANG=%s;window.DP_I18N=%s;" % (
        json.dumps(lang), json.dumps(I18N.js_dictionary(lang), ensure_ascii=False))
    response = Response(body, mimetype="application/javascript")
    response.headers["Cache-Control"] = "public, max-age=604800"
    return response


@app.context_processor
def inject_dairypro_context():
    ctx = {"lang": current_language(), "i18n_version": I18N.version,
           "animal_rules": animal_rules_json(), "dp_asset_version": DP_ASSET_VERSION,
           "subscription": None}
    if session.get("loggedin") and session.get("role") in ("owner", "staff"):
        try:
            ctx["subscription"] = subscription_state(int(session["id"]))
        except Exception:
            dp_log.exception("subscription state unavailable")
    return ctx


# ----------------------------------------------------------------------
# Offline support
# ----------------------------------------------------------------------
@app.route("/offline")
def offline_page():
    """Shell shown by the service worker when a page is not cached."""
    return render_template("offline.html")


@app.route("/api/session")
def session_ping():
    """The sync engine calls this before every run: it confirms the session
    is alive, WHICH dairy it belongs to (records of another dairy are never
    sent) and returns a fresh CSRF token."""
    if not session.get("loggedin") or session.get("role") not in ("owner", "staff", "customer"):
        return json_error("Your session has expired. Please log in again.", 401,
                          code="auth_required")
    from flask_wtf.csrf import generate_csrf
    body = {"success": True, "tenant_id": int(session["id"]), "role": session.get("role"),
            "staff_id": session.get("staff_id"), "csrf_token": generate_csrf(),
            "lang": current_language()}
    if session.get("role") in ("owner", "staff"):
        state = subscription_state(int(session["id"]))
        body["subscription"] = {"access": state["access"], "status": state["status"]}
    response = jsonify(body)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.before_request
def enforce_offline_tenant():
    """Offline queue requests carry X-DP-Tenant (the dairy that created the
    record). If it is not the logged-in dairy, nothing is saved: one owner's
    queued records can never be stored under another owner's account."""
    header = request.headers.get("X-DP-Tenant")
    if not header or request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if not session.get("loggedin"):
        return None  # require_login already answered with 401
    try:
        same = int(header) == int(session.get("id"))
    except (TypeError, ValueError):
        same = False
    if not same:
        security_log.warning("offline tenant mismatch header=%s session=%s path=%s",
                             header, session.get("id"), request.path)
        return json_error("These entries belong to another dairy account. "
                          "Log in to that account to synchronise them.",
                          409, code="tenant_mismatch")
    return None


# ----------------------------------------------------------------------
# Subscriptions: configuration
# ----------------------------------------------------------------------
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
RAZORPAY_API_BASE = os.getenv("RAZORPAY_API_BASE", "https://api.razorpay.com/v1").rstrip("/")
TRIAL_DAYS = int(os.getenv("DAIRYPRO_TRIAL_DAYS", "20") or 20)
# Days before the end of a paid period when the renewal reminder appears.
RENEWAL_REMINDER_DAYS = 5


def razorpay_configured():
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)


def subscription_enforced():
    """auto (default): enforce only when Razorpay is configured, so nobody is
    locked out of an installation where they cannot pay. 1 / 0 force it."""
    mode = os.getenv("DAIRYPRO_ENFORCE_SUBSCRIPTION", "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    return razorpay_configured()


DEFAULT_PLANS = [
    {"code": "monthly", "name": "Monthly", "duration_months": 1, "price_paise": 39900,
     "savings_pct": 0},
    {"code": "six_months", "name": "Six Months", "duration_months": 6, "price_paise": 119900,
     "savings_pct": 33},
    {"code": "yearly", "name": "Yearly", "duration_months": 12, "price_paise": 199900,
     "savings_pct": 44},
]
PLAN_TAGLINES = {"monthly": "No long-term commitment", "six_months": "Save 33%",
                 "yearly": "Save 44%"}


def utcnow():
    """Naive UTC timestamp (what the DATETIME columns store)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_ist(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(IST)


def add_months(value, months):
    import calendar
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def list_plans():
    try:
        cur = db_cursor()
        cur.execute("SELECT code, name, duration_months, price_paise, savings_pct FROM dp_plans "
                    "WHERE is_active=1 ORDER BY sort_order, id")
        rows = list(cur.fetchall())
        cur.close()
        if rows:
            return rows
    except MySQLdb.Error:
        dp_log.exception("dp_plans unavailable; using built-in plan list")
    return [dict(p) for p in DEFAULT_PLANS]


def get_plan(code):
    for plan in list_plans():
        if plan["code"] == code:
            return plan
    return None


def _email_hash(email):
    return hashlib.sha256((email or "").strip().lower().encode()).hexdigest()


def ensure_trial(user_id, email=None):
    """Start the 20-day trial once per account AND per e-mail address.
    Timestamps come from the server clock only."""
    cur = db_cursor()
    try:
        cur.execute("SELECT started_at, ends_at FROM dp_trials WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if row:
            return row
        if email is None:
            cur.execute("SELECT email FROM users WHERE id=%s", (user_id,))
            u = cur.fetchone()
            if not u:
                return None
            email = u["email"]
        ehash = _email_hash(email)
        now = utcnow().replace(microsecond=0)
        cur.execute(
            "INSERT IGNORE INTO dp_trials (user_id, email_hash, started_at, ends_at) "
            "VALUES (%s,%s,%s,%s)", (user_id, ehash, now, now + timedelta(days=TRIAL_DAYS)))
        if cur.rowcount == 0:
            # This e-mail address already had a trial (account deleted and
            # created again): attach the OLD trial window, no second trial.
            cur.execute("UPDATE dp_trials SET user_id=%s WHERE email_hash=%s AND user_id IS NULL",
                        (user_id, ehash))
        else:
            audit_record("trial_started", "subscription", user_id,
                         new={"ends_at": now + timedelta(days=TRIAL_DAYS)}, user_id=user_id)
        cur.execute("SELECT started_at, ends_at FROM dp_trials WHERE user_id=%s", (user_id,))
        return cur.fetchone()
    finally:
        cur.close()


def subscription_state(user_id, refresh=False):
    """Access state for a dairy (cached per request).

    access is True when the dairy may use the paid features: an active paid
    period, a running trial, or enforcement switched off."""
    cache = g.setdefault("_dp_sub", {})
    if not refresh and user_id in cache:
        return cache[user_id]
    now = utcnow()
    cur = db_cursor()
    try:
        cur.execute(
            "SELECT id, plan_code, starts_at, ends_at, status FROM dp_subscriptions "
            "WHERE user_id=%s AND status IN ('active','cancelled') AND ends_at > %s "
            "ORDER BY ends_at DESC LIMIT 1", (user_id, now))
        sub = cur.fetchone()
        cur.execute(
            "SELECT id, plan_code, amount_paise, status, razorpay_order_id, created_at "
            "FROM dp_payment_orders WHERE user_id=%s AND status='pending' "
            "ORDER BY id DESC LIMIT 1", (user_id,))
        pending = cur.fetchone()
        cur.execute("SELECT plan_code FROM dp_payment_orders WHERE user_id=%s AND status='paid' "
                    "ORDER BY id DESC LIMIT 1", (user_id,))
        last_paid = cur.fetchone()
    finally:
        cur.close()
    trial = ensure_trial(user_id)

    state = {"enforced": subscription_enforced(), "razorpay": razorpay_configured(),
             "plan_code": None, "ends_at": None, "ends_at_ist": None, "days_left": None,
             "trial_ends_at_ist": None, "trial_days_left": None, "pending_order": pending,
             "has_paid": bool(last_paid),
             "last_plan_code": last_paid["plan_code"] if last_paid else None,
             "cancelled": False}
    if trial:
        state["trial_ends_at_ist"] = to_ist(trial["ends_at"])
        seconds = (trial["ends_at"] - now).total_seconds()
        state["trial_days_left"] = max(0, math.ceil(seconds / 86400))
    if sub:
        state.update(status="active", plan_code=sub["plan_code"], ends_at=sub["ends_at"],
                     ends_at_ist=to_ist(sub["ends_at"]),
                     days_left=max(0, math.ceil((sub["ends_at"] - now).total_seconds() / 86400)),
                     cancelled=sub["status"] == "cancelled")
    elif trial and trial["ends_at"] > now:
        state["status"] = "trial"
    elif pending:
        state["status"] = "pending"
    else:
        state["status"] = "expired"
    state["access"] = (not state["enforced"]) or state["status"] in ("active", "trial")
    cache[user_id] = state
    return state


def due_reminder(user_id, state):
    """The one reminder to show now (or None). Each key is shown once per
    account, so the user is not nagged on every page."""
    key = None
    if state["status"] == "trial":
        days = state["trial_days_left"]
        for threshold in (1, 3, 5, 10):
            if days <= threshold:
                key = f"trial_{threshold}"
                break
        else:
            key = "trial_started"
    elif state["status"] == "expired" and not state["has_paid"] and state["trial_ends_at_ist"]:
        key = "trial_expired"
    elif state["status"] == "expired" and state["has_paid"]:
        key = "subscription_expired"
    elif state["status"] == "active" and state["days_left"] is not None \
            and state["days_left"] <= RENEWAL_REMINDER_DAYS:
        key = "renewal_" + state["ends_at"].strftime("%Y%m%d")
    if not key:
        return None
    cur = db_cursor()
    try:
        cur.execute("SELECT 1 FROM dp_reminders WHERE user_id=%s AND reminder_key=%s",
                    (user_id, key))
        if cur.fetchone():
            return None
    finally:
        cur.close()
    return key


# ----------------------------------------------------------------------
# Access control (backend enforcement)
# ----------------------------------------------------------------------
SUBSCRIPTION_FREE_ENDPOINTS = {
    "static", "logout", "login", "signup", "manifest", "service_worker", "favicon",
    "healthcheck", "set_language", "i18n_js", "offline_page", "session_ping",
    "subscription_page", "api_subscription_status", "api_subscription_order",
    "api_subscription_verify", "api_subscription_attempt", "api_subscription_reconcile",
    "api_subscription_reminder_seen", "razorpay_webhook",
    # account management, policies and help stay available
    "account", "delete_account", "settings", "about", "contact", "privacy", "terms",
    "learn", "profile", "bank_settings",
    "admin_subscriptions", "admin_subscription_reconcile",
}


@app.before_request
def enforce_subscription():
    if session.get("role") not in ("owner", "staff") or not session.get("loggedin"):
        return None
    endpoint = request.endpoint or ""
    if endpoint in SUBSCRIPTION_FREE_ENDPOINTS or endpoint.startswith("customer_"):
        return None
    try:
        state = subscription_state(int(session["id"]))
    except Exception:
        # Never lock a dairy out because the billing tables are unavailable.
        dp_log.exception("subscription check failed; allowing access")
        return None
    if state["access"]:
        return None
    msg = ("Your payment is being verified. Access will open as soon as it is confirmed."
           if state["status"] == "pending" else
           "Your subscription is not active. Please renew to continue.")
    if wants_json() or request.method != "GET":
        return json_error(msg, 402, code="subscription_required",
                          subscription_status=state["status"])
    flash(_t(msg), "warning")
    return redirect(url_for("subscription_page"))


# ----------------------------------------------------------------------
# Razorpay API (urllib; no extra dependency)
# ----------------------------------------------------------------------
class RazorpayError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body


def razorpay_request(method, path, payload=None):
    import urllib.error
    import urllib.request
    if not razorpay_configured():
        raise RazorpayError("Razorpay is not configured.")
    url = f"{RAZORPAY_API_BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    token = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=EXTERNAL_CALL_TIMEOUT) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        dp_log.warning("razorpay %s %s -> %s %s", method, path, exc.code, body)
        raise RazorpayError("Razorpay request failed.", exc.code, body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        dp_log.warning("razorpay %s %s network error: %s", method, path, exc)
        raise RazorpayError("Razorpay is unreachable.")


def razorpay_signature_ok(order_id, payment_id, signature):
    if not (order_id and payment_id and signature and RAZORPAY_KEY_SECRET):
        return False
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature))


def razorpay_webhook_signature_ok(body, signature):
    if not (RAZORPAY_WEBHOOK_SECRET and signature):
        return False
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature))


def _record_attempt(cur, order_id, event, payment_id=None, detail=None):
    cur.execute(
        "INSERT INTO dp_payment_attempts (order_id, event, razorpay_payment_id, detail, ip) "
        "VALUES (%s,%s,%s,%s,%s)",
        (order_id, event[:30], payment_id, (detail or "")[:500] or None,
         client_ip() if has_request_context() else None))


def activate_order(order_pk, payment_id, via, amount_paise=None, currency=None):
    """Mark an order paid and create its subscription period, exactly once.

    Called from the verified checkout callback, the signed webhook and the
    reconciliation check; whichever arrives first activates, the others find
    the order already paid (row lock + UNIQUE(order_id)) and change nothing."""
    with db_transaction() as cur:
        cur.execute("SELECT * FROM dp_payment_orders WHERE id=%s FOR UPDATE", (order_pk,))
        order = cur.fetchone()
        if not order:
            raise NotFound("Payment order not found.")
        if order["status"] in ("paid", "refunded", "partially_refunded"):
            return {"activated": False, "already": True,
                    "subscription_id": order["subscription_id"]}
        if amount_paise is not None and int(amount_paise) != int(order["amount_paise"]):
            dp_log.error("amount mismatch order=%s expected=%s got=%s", order_pk,
                         order["amount_paise"], amount_paise)
            _record_attempt(cur, order_pk, "amount_mismatch", payment_id,
                            f"expected {order['amount_paise']} got {amount_paise}")
            raise Conflict("amount_mismatch", "Payment amount does not match the plan price.")
        if currency is not None and str(currency).upper() != order["currency"]:
            raise Conflict("amount_mismatch", "Payment currency does not match.")
        user_id = order["user_id"]
        now = utcnow().replace(microsecond=0)
        start = now
        if user_id:
            cur.execute("SELECT MAX(ends_at) AS e FROM dp_subscriptions WHERE user_id=%s "
                        "AND status IN ('active','cancelled') AND ends_at > %s",
                        (user_id, now))
            latest = cur.fetchone()["e"]
            if latest and latest > start:
                start = latest     # an early renewal extends the current period
        end = add_months(start, int(order["duration_months"]))
        cur.execute(
            "INSERT INTO dp_subscriptions (user_id, plan_code, order_id, status, starts_at, ends_at) "
            "VALUES (%s,%s,%s,'active',%s,%s)", (user_id, order["plan_code"], order_pk, start, end))
        sub_id = cur.lastrowid
        cur.execute(
            "UPDATE dp_payment_orders SET status='paid', razorpay_payment_id=%s, verified_via=%s, "
            "paid_at=%s, subscription_id=%s, failure_code=NULL, failure_reason=NULL WHERE id=%s",
            (payment_id, via, now, sub_id, order_pk))
        _record_attempt(cur, order_pk, f"activated_{via}", payment_id)
        audit_record("subscription_activated", "subscription", sub_id,
                     new={"plan": order["plan_code"], "starts_at": start, "ends_at": end,
                          "order": order["razorpay_order_id"], "payment": payment_id, "via": via},
                     user_id=user_id, cur=cur)
    if has_request_context():
        g.setdefault("_dp_sub", {}).pop(user_id, None)
    return {"activated": True, "already": False, "subscription_id": sub_id,
            "starts_at": start, "ends_at": end}


def _set_order_status(order_pk, status, code=None, reason=None):
    cur = db_cursor()
    try:
        cur.execute(
            "UPDATE dp_payment_orders SET status=%s, failure_code=%s, failure_reason=%s "
            "WHERE id=%s AND status NOT IN ('paid','refunded','partially_refunded')",
            (status, str(code)[:60] if code else None, str(reason)[:255] if reason else None,
             order_pk))
    finally:
        cur.close()


def reconcile_order(order):
    """Ask Razorpay what really happened to an order (money deducted but the
    browser never got the success callback, closed tab, network loss...).
    Activates only on a CAPTURED payment of the exact amount; never creates
    another payment."""
    if order["status"] == "paid" or not order.get("razorpay_order_id"):
        return order["status"]
    payments = razorpay_request("GET", f"/orders/{order['razorpay_order_id']}/payments")
    items = payments.get("items") or []
    cur = db_cursor()
    try:
        cur.execute("UPDATE dp_payment_orders SET last_checked_at=%s WHERE id=%s",
                    (utcnow(), order["id"]))
    finally:
        cur.close()
    captured = [p for p in items if p.get("status") == "captured"]
    if captured:
        p = captured[0]
        activate_order(order["id"], p.get("id"), "reconcile", p.get("amount"), p.get("currency"))
        return "paid"
    if any(p.get("status") == "authorized" for p in items):
        _set_order_status(order["id"], "pending", None, "Payment authorised; waiting for capture.")
        return "pending"
    if items and all(p.get("status") == "failed" for p in items):
        last = items[0]
        _set_order_status(order["id"], "failed", last.get("error_code"),
                          last.get("error_description"))
        return "failed"
    return order["status"]


def _owner_only_json():
    if session.get("role") != "owner":
        return json_error("Only the dairy owner can manage the subscription.", 403,
                          code="forbidden")
    return None


def _order_for_user(user_id, razorpay_order_id):
    cur = db_cursor()
    try:
        cur.execute("SELECT * FROM dp_payment_orders WHERE razorpay_order_id=%s AND user_id=%s",
                    (razorpay_order_id, user_id))
        return cur.fetchone()
    finally:
        cur.close()


PENDING_PAYMENT_MESSAGE = ("Payment received. We are confirming it with the bank; this page "
                           "will update automatically. Do not pay again.")


# ----------------------------------------------------------------------
# Subscription pages and APIs
# ----------------------------------------------------------------------
@app.route("/subscription")
def subscription_page():
    user_id = int(session["id"])
    state = subscription_state(user_id)
    # Unfinished payments are re-checked with Razorpay when the page opens
    # (at most once a minute per order) - recovers "money deducted, no
    # confirmation" without the user paying twice.
    if razorpay_configured() and session.get("role") == "owner":
        cur = db_cursor()
        cur.execute(
            "SELECT * FROM dp_payment_orders WHERE user_id=%s AND status IN "
            "('created','attempted','pending','failed') AND razorpay_order_id IS NOT NULL "
            "AND created_at > %s AND (last_checked_at IS NULL OR last_checked_at < %s) "
            "ORDER BY id DESC LIMIT 3",
            (user_id, utcnow() - timedelta(days=3), utcnow() - timedelta(seconds=60)))
        orders = cur.fetchall()
        cur.close()
        changed = False
        for order in orders:
            try:
                if reconcile_order(order) != order["status"]:
                    changed = True
            except (RazorpayError, Conflict):
                pass
        if changed:
            state = subscription_state(user_id, refresh=True)
    cur = db_cursor()
    cur.execute(
        "SELECT o.id, o.plan_code, o.amount_paise, o.status, o.razorpay_order_id, "
        "o.razorpay_payment_id, o.failure_reason, o.created_at, o.paid_at, s.starts_at, s.ends_at "
        "FROM dp_payment_orders o LEFT JOIN dp_subscriptions s ON s.id = o.subscription_id "
        "WHERE o.user_id=%s ORDER BY o.id DESC LIMIT 25", (user_id,))
    history = cur.fetchall()
    cur.close()
    for h in history:
        h["created_ist"] = to_ist(h["created_at"]) if h["created_at"] else None
        h["starts_ist"] = to_ist(h["starts_at"]) if h["starts_at"] else None
        h["ends_ist"] = to_ist(h["ends_at"]) if h["ends_at"] else None
    return render_template("subscription.html", state=state, plans=list_plans(),
                           history=history, taglines=PLAN_TAGLINES,
                           is_owner=session.get("role") == "owner",
                           razorpay_key=RAZORPAY_KEY_ID if razorpay_configured() else "")


@app.route("/api/subscription/status")
def api_subscription_status():
    user_id = int(session["id"])
    state = subscription_state(user_id)
    reminder = due_reminder(user_id, state) if session.get("role") == "owner" else None
    return jsonify({
        "success": True, "status": state["status"], "access": state["access"],
        "enforced": state["enforced"],
        "trial_days_left": state["trial_days_left"],
        "trial_ends_at": format_dmy(state["trial_ends_at_ist"].date())
        if state["trial_ends_at_ist"] else None,
        "ends_at": format_dmy(state["ends_at_ist"].date()) if state["ends_at_ist"] else None,
        "days_left": state["days_left"], "plan_code": state["plan_code"],
        "pending": bool(state["pending_order"]), "reminder": reminder})


@app.route("/api/subscription/reminder-seen", methods=["POST"])
def api_subscription_reminder_seen():
    key = str((request.get_json(silent=True) or {}).get("key") or "")[:40]
    if not re.match(r"^[a-z0-9_]+$", key):
        return json_error("Invalid reminder.", 400, code="invalid")
    cur = db_cursor()
    cur.execute("INSERT IGNORE INTO dp_reminders (user_id, reminder_key) VALUES (%s,%s)",
                (int(session["id"]), key))
    cur.close()
    return jsonify({"success": True})


@app.route("/api/subscription/order", methods=["POST"])
def api_subscription_order():
    """Create (or reuse) a Razorpay order. Price and duration come ONLY from
    the server's plan table; the client sends just the plan code."""
    blocked = _owner_only_json()
    if blocked:
        return blocked
    if not razorpay_configured():
        return json_error("Online payment is not configured yet. Please contact support.", 503,
                          code="payments_unavailable")
    user_id = int(session["id"])
    if rate_limited([("pay_order_user", str(user_id), 20, 3600)]):
        return json_error(RATE_LIMIT_MESSAGE, 429, code="rate_limited")
    plan = get_plan(str((request.get_json(silent=True) or {}).get("plan_code") or ""))
    if not plan:
        return json_error("Please choose a valid plan.", 400, code="invalid")

    cur = db_cursor()
    try:
        # Reuse a recent unpaid order for the same plan instead of piling up
        # orders on repeated clicks / Back navigation.
        cur.execute(
            "SELECT * FROM dp_payment_orders WHERE user_id=%s AND plan_code=%s AND "
            "status IN ('created','attempted') AND razorpay_order_id IS NOT NULL "
            "AND amount_paise=%s AND created_at > %s ORDER BY id DESC LIMIT 1",
            (user_id, plan["code"], plan["price_paise"], utcnow() - timedelta(minutes=30)))
        order = cur.fetchone()
        if not order:
            receipt = f"dp{user_id}-{secrets.token_hex(8)}"
            cur.execute(
                "INSERT INTO dp_payment_orders (user_id, plan_code, duration_months, amount_paise, "
                "currency, receipt, status) VALUES (%s,%s,%s,%s,'INR',%s,'created')",
                (user_id, plan["code"], plan["duration_months"], plan["price_paise"], receipt))
            order_pk = cur.lastrowid
            try:
                rzp = razorpay_request("POST", "/orders", {
                    "amount": plan["price_paise"], "currency": "INR", "receipt": receipt,
                    "notes": {"dairypro_user_id": str(user_id), "plan_code": plan["code"]}})
            except RazorpayError:
                cur.execute("UPDATE dp_payment_orders SET status='failed', "
                            "failure_reason='order creation failed' WHERE id=%s", (order_pk,))
                return json_error("Could not start the payment. Please try again.", 502,
                                  code="gateway_error")
            if int(rzp.get("amount", -1)) != plan["price_paise"] or not rzp.get("id"):
                cur.execute("UPDATE dp_payment_orders SET status='failed', "
                            "failure_reason='gateway amount mismatch' WHERE id=%s", (order_pk,))
                return json_error("Could not start the payment. Please try again.", 502,
                                  code="gateway_error")
            cur.execute("UPDATE dp_payment_orders SET razorpay_order_id=%s WHERE id=%s",
                        (rzp["id"], order_pk))
            cur.execute("SELECT * FROM dp_payment_orders WHERE id=%s", (order_pk,))
            order = cur.fetchone()
        _record_attempt(cur, order["id"], "checkout_opened")
        cur.execute("SELECT email, phone, dairy_name FROM users WHERE id=%s", (user_id,))
        user = cur.fetchone() or {}
    finally:
        cur.close()
    return jsonify({
        "success": True, "key_id": RAZORPAY_KEY_ID, "order_id": order["razorpay_order_id"],
        "amount": order["amount_paise"], "currency": order["currency"], "name": BRAND_NAME,
        "description": _t(plan["name"]) + " · " + BRAND_NAME,
        "prefill": {"email": user.get("email") or "", "contact": user.get("phone") or "",
                    "name": user.get("dairy_name") or ""}})


@app.route("/api/subscription/verify", methods=["POST"])
def api_subscription_verify():
    """Checkout success callback. The signature proves the ids came from
    Razorpay; activation additionally requires Razorpay's own record of a
    CAPTURED payment of the right amount. If Razorpay cannot be reached the
    order stays 'pending' and is completed by the webhook/reconciliation:
    it is never marked failed and the user is never charged again."""
    blocked = _owner_only_json()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    rzp_order = str(data.get("razorpay_order_id") or "")[:40]
    rzp_payment = str(data.get("razorpay_payment_id") or "")[:40]
    signature = str(data.get("razorpay_signature") or "")[:128]
    user_id = int(session["id"])
    order = _order_for_user(user_id, rzp_order)
    if not order:
        return json_error("Payment order not found.", 404, code="not_found")
    cur = db_cursor()
    try:
        if not razorpay_signature_ok(rzp_order, rzp_payment, signature):
            _record_attempt(cur, order["id"], "bad_signature", rzp_payment)
            security_log.warning("razorpay bad signature user=%s order=%s", user_id, rzp_order)
            return json_error("Payment could not be verified.", 400, code="bad_signature")
        _record_attempt(cur, order["id"], "checkout_success", rzp_payment)
    finally:
        cur.close()
    if order["status"] == "paid":
        return jsonify({"success": True, "status": "paid", "already": True,
                        "message": "Payment successful. Your subscription is active."})
    try:
        payment = razorpay_request("GET", f"/payments/{rzp_payment}")
    except RazorpayError:
        _set_order_status(order["id"], "pending", None, "Waiting for confirmation from Razorpay.")
        return jsonify({"success": False, "status": "pending", "code": "pending",
                        "message": PENDING_PAYMENT_MESSAGE}), 202
    if payment.get("order_id") != rzp_order:
        return json_error("Payment could not be verified.", 400, code="order_mismatch")
    if payment.get("status") == "captured":
        try:
            activate_order(order["id"], rzp_payment, "checkout", payment.get("amount"),
                           payment.get("currency"))
        except Conflict as e:
            return json_error(e.message, 409, code=e.code)
        return jsonify({"success": True, "status": "paid",
                        "message": "Payment successful. Your subscription is active."})
    if payment.get("status") == "authorized":
        _set_order_status(order["id"], "pending", None, "Payment authorised; waiting for capture.")
        return jsonify({"success": False, "status": "pending", "code": "pending",
                        "message": PENDING_PAYMENT_MESSAGE}), 202
    _set_order_status(order["id"], "failed", payment.get("error_code"),
                      payment.get("error_description"))
    return json_error("Payment failed. No subscription was activated. You can try again.", 402,
                      code="payment_failed")


@app.route("/api/subscription/attempt", methods=["POST"])
def api_subscription_attempt():
    """Checkout closed by the user (Back / close) or a failure reported by the
    checkout. Recorded for history; never marks the order paid, and the
    order stays retryable."""
    blocked = _owner_only_json()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    event = str(data.get("event") or "")
    if event not in ("dismissed", "failed"):
        return json_error("Invalid event.", 400, code="invalid")
    order = _order_for_user(int(session["id"]), str(data.get("razorpay_order_id") or "")[:40])
    if not order:
        return json_error("Payment order not found.", 404, code="not_found")
    cur = db_cursor()
    try:
        detail = str(data.get("description") or data.get("reason") or "")[:500]
        _record_attempt(cur, order["id"], f"checkout_{event}",
                        str(data.get("razorpay_payment_id") or "")[:40] or None, detail)
        if event == "failed" and order["status"] in ("created", "attempted"):
            cur.execute("UPDATE dp_payment_orders SET status='attempted', failure_code=%s, "
                        "failure_reason=%s WHERE id=%s",
                        (str(data.get("code") or "")[:60] or None, detail[:255] or None,
                         order["id"]))
    finally:
        cur.close()
    return jsonify({"success": True, "status": order["status"]})


@app.route("/api/subscription/reconcile", methods=["POST"])
def api_subscription_reconcile():
    """'Check payment status' button: asks Razorpay about unfinished orders."""
    blocked = _owner_only_json()
    if blocked:
        return blocked
    user_id = int(session["id"])
    if rate_limited([("pay_reconcile_user", str(user_id), 30, 3600)]):
        return json_error(RATE_LIMIT_MESSAGE, 429, code="rate_limited")
    cur = db_cursor()
    cur.execute("SELECT * FROM dp_payment_orders WHERE user_id=%s AND status IN "
                "('created','attempted','pending','failed') AND razorpay_order_id IS NOT NULL "
                "AND created_at > %s ORDER BY id DESC LIMIT 5",
                (user_id, utcnow() - timedelta(days=7)))
    orders = cur.fetchall()
    cur.close()
    results = []
    try:
        for order in orders:
            results.append(reconcile_order(order))
    except RazorpayError:
        return json_error("Could not reach the payment gateway. Please try again shortly.", 503,
                          code="gateway_error")
    except Conflict as e:
        return json_error(e.message, 409, code=e.code)
    state = subscription_state(user_id, refresh=True)
    message = ("Payment confirmed. Your subscription is active." if "paid" in results else
               "Payment is still being confirmed." if "pending" in results else
               "No completed payment was found.")
    return jsonify({"success": True, "status": state["status"], "message": message})


@app.route("/razorpay/webhook", methods=["POST"])
@csrf.exempt
def razorpay_webhook():
    """Razorpay -> DairyPro. Authenticated by HMAC (X-Razorpay-Signature).
    Every event id is stored once; a replayed delivery is ignored."""
    body = request.get_data(cache=False)
    if not razorpay_webhook_signature_ok(body, request.headers.get("X-Razorpay-Signature")):
        security_log.warning("razorpay webhook rejected (bad signature) ip=%s", client_ip())
        return jsonify({"ok": False}), 400
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"ok": False}), 400
    event_type = str(payload.get("event") or "")[:60]
    event_id = (request.headers.get("X-Razorpay-Event-Id")
                or hashlib.sha256(body).hexdigest())[:64]
    body_payload = payload.get("payload") or {}
    entity = (body_payload.get("payment") or {}).get("entity") or {}
    refund = (body_payload.get("refund") or {}).get("entity") or {}
    order_entity = (body_payload.get("order") or {}).get("entity") or {}
    rzp_order = entity.get("order_id") or order_entity.get("id")
    rzp_payment = entity.get("id") or refund.get("payment_id")
    cur = db_cursor()
    try:
        cur.execute(
            "INSERT IGNORE INTO dp_webhook_events (event_id, event_type, razorpay_order_id, "
            "razorpay_payment_id, payload) VALUES (%s,%s,%s,%s,%s)",
            (event_id, event_type, rzp_order, rzp_payment, body.decode("utf-8")[:60000]))
        if cur.rowcount == 0:
            return jsonify({"ok": True, "duplicate": True})
    finally:
        cur.close()

    result = "ignored"
    try:
        if event_type in ("payment.captured", "order.paid") and rzp_order:
            cur = db_cursor()
            cur.execute("SELECT id FROM dp_payment_orders WHERE razorpay_order_id=%s", (rzp_order,))
            row = cur.fetchone()
            cur.close()
            if row:
                amount = entity.get("amount") if entity else order_entity.get("amount_paid")
                currency = entity.get("currency") if entity else order_entity.get("currency")
                out = activate_order(row["id"], rzp_payment, "webhook", amount, currency)
                result = "activated" if out["activated"] else "already_paid"
            else:
                result = "unknown_order"
        elif event_type == "payment.failed" and rzp_order:
            cur = db_cursor()
            cur.execute("SELECT id FROM dp_payment_orders WHERE razorpay_order_id=%s", (rzp_order,))
            row = cur.fetchone()
            cur.close()
            if row:
                _set_order_status(row["id"], "failed", entity.get("error_code"),
                                  entity.get("error_description"))
                result = "marked_failed"
        elif event_type == "refund.processed" and rzp_payment:
            result = apply_refund(rzp_payment, int(refund.get("amount") or 0), refund.get("id"))
        elif event_type == "payment.authorized" and rzp_order:
            result = "authorized_noted"
    except Conflict as e:
        result = e.code
    except Exception:
        dp_log.exception("webhook processing failed event=%s", event_id)
        # Forget the event so Razorpay's automatic retry is processed.
        cur = db_cursor()
        cur.execute("DELETE FROM dp_webhook_events WHERE event_id=%s", (event_id,))
        cur.close()
        return jsonify({"ok": False}), 500
    cur = db_cursor()
    cur.execute("UPDATE dp_webhook_events SET result=%s, processed_at=%s WHERE event_id=%s",
                (result, utcnow(), event_id))
    cur.close()
    return jsonify({"ok": True, "result": result})


def apply_refund(payment_id, refund_paise, refund_id=None):
    """Only a refund Razorpay reports as PROCESSED changes anything. A full
    refund ends that paid period; a partial refund is recorded only."""
    with db_transaction() as cur:
        cur.execute("SELECT * FROM dp_payment_orders WHERE razorpay_payment_id=%s FOR UPDATE",
                    (payment_id,))
        order = cur.fetchone()
        if not order:
            return "unknown_payment"
        total = int(order["refunded_paise"] or 0) + max(int(refund_paise), 0)
        total = min(total, int(order["amount_paise"]))
        full = total >= int(order["amount_paise"])
        cur.execute("UPDATE dp_payment_orders SET refunded_paise=%s, status=%s WHERE id=%s",
                    (total, "refunded" if full else "partially_refunded", order["id"]))
        if full and order["subscription_id"]:
            cur.execute("UPDATE dp_subscriptions SET status='refunded', cancelled_at=%s, "
                        "cancel_reason=%s WHERE id=%s",
                        (utcnow(), f"refund {refund_id or ''}".strip()[:255],
                         order["subscription_id"]))
        _record_attempt(cur, order["id"], "refund_processed", payment_id,
                        f"refund {refund_id} amount {refund_paise}")
        audit_record("subscription_refund", "subscription", order["subscription_id"],
                     new={"refunded_paise": total, "full": full, "refund_id": refund_id},
                     user_id=order["user_id"], cur=cur)
    if has_request_context() and order["user_id"]:
        g.setdefault("_dp_sub", {}).pop(order["user_id"], None)
    return "refunded" if full else "partially_refunded"


# ----------------------------------------------------------------------
# Platform administrators: subscription overview
# ----------------------------------------------------------------------
@app.route("/admin/subscriptions")
def admin_subscriptions():
    if not is_platform_admin():
        return owner_required_response()
    cur = db_cursor()
    cur.execute(
        "SELECT u.id, u.email, u.dairy_name, u.dairy_code, t.ends_at AS trial_ends, "
        " (SELECT MAX(s.ends_at) FROM dp_subscriptions s WHERE s.user_id=u.id "
        "   AND s.status IN ('active','cancelled')) AS paid_until, "
        " (SELECT COUNT(*) FROM dp_payment_orders o WHERE o.user_id=u.id AND o.status='pending') "
        "   AS pending_orders "
        "FROM users u LEFT JOIN dp_trials t ON t.user_id=u.id ORDER BY u.id DESC LIMIT 500")
    dairies = cur.fetchall()
    cur.execute(
        "SELECT o.*, u.email FROM dp_payment_orders o LEFT JOIN users u ON u.id=o.user_id "
        "ORDER BY o.id DESC LIMIT 100")
    orders = cur.fetchall()
    cur.close()
    return render_template("admin/subscriptions.html", dairies=dairies, orders=orders,
                           now=utcnow())


@app.route("/admin/subscriptions/reconcile/<int:order_id>", methods=["POST"])
def admin_subscription_reconcile(order_id):
    if not is_platform_admin():
        return owner_required_response()
    cur = db_cursor()
    cur.execute("SELECT * FROM dp_payment_orders WHERE id=%s", (order_id,))
    order = cur.fetchone()
    cur.close()
    if not order:
        flash(_t("Payment order not found."), "danger")
    else:
        try:
            status = reconcile_order(order)
            flash(_t("Order status: {status}", status=status), "info")
        except (RazorpayError, Conflict) as e:
            flash(_t(e.message), "danger")
    return redirect(url_for("admin_subscriptions"))


# ============================================================
# FAT/SNF: see fat_snf/ (blueprint registered near require_login).
# The earlier inline implementation that lived here was replaced: it let
# staff change the owner's calculation mode, never enforced the mode,
# and its multi-statement writes were not real transactions.
# ============================================================


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    # Never run the interactive debugger in production (it allows remote
    # code execution). Opt in locally with FLASK_DEBUG=1.
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")