"""
Security infrastructure: CSRF, security headers, request IDs, role
allow-listing, DB-backed rate limiting and server-side OTP challenges.
"""
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import time
import uuid

from flask import (current_app, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_wtf.csrf import CSRFError, CSRFProtect

from core import db

log = logging.getLogger("dairymitr.security")

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
        for e in os.getenv("DAIRYMITR_ADMIN_EMAILS", "").split(",")
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
    log.warning("staff access denied endpoint=%s staff_id=%s",
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
        cur = db.cursor()
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
        log.exception("rate limiter unavailable (name=%s)", name)
        return True


def is_blocked(name, key, limit):
    """Read-only check (no hit recorded): True when the bucket is over limit."""
    bucket = hashlib.sha256(f"{name}:{key}".encode()).hexdigest()
    try:
        cur = db.cursor()
        cur.execute("SELECT hits FROM dm_rate_limits WHERE bucket_key = %s AND expires_at >= %s",
                    (bucket, int(time.time())))
        row = cur.fetchone()
        cur.close()
        return bool(row) and row["hits"] >= limit
    except Exception:
        log.exception("rate limiter unavailable (name=%s)", name)
        return False


def failures_blocked(rules):
    """For login forms: only FAILED attempts count. Check before verifying
    the password; call record_failures(rules) after a failed attempt."""
    blocked = any(is_blocked(name, key, limit) for name, key, limit, _ in rules)
    if blocked:
        log.warning("login blocked after repeated failures path=%s ip=%s", request.path, client_ip())
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
        log.warning("rate limit exceeded path=%s ip=%s", request.path, client_ip())
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
    cur = db.cursor()
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
    with db.transaction() as cur:
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
    cur = db.cursor()
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
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://code.iconify.design "
    "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
    "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com",
    "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com "
    "https://cdn.jsdelivr.net",
    "img-src 'self' data: blob: https:",
    "connect-src 'self' https://api.iconify.design https://api.simplesvg.com "
    "https://api.unisvg.com https://unpkg.com",
    "frame-src https://www.youtube.com https://www.google.com",
    "worker-src 'self'",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
])


def init_app(app):
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
        log.warning("CSRF rejected path=%s reason=%s", request.path, e.description)
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
        log.error("unhandled error request_id=%s path=%s", getattr(g, "request_id", "-"),
                  request.path)
        msg = "सर्व्हरवर अडचण आली. कृपया पुन्हा प्रयत्न करा."
        if wants_json():
            return json_error(msg, 500, code="server_error")
        return render_template("errors/error.html", code=500, message=msg,
                               request_id=getattr(g, "request_id", None)), 500


def _same_origin(url):
    if not url:
        return False
    from urllib.parse import urlparse
    p = urlparse(url)
    return p.netloc == request.host
