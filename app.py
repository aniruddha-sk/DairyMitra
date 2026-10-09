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
import smtplib
from flask import send_from_directory
from functools import wraps
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from push import send_web_push, VAPID_PUBLIC_KEY

import pandas as pd
from ai.milk_prediction import predict_milk
from ai.anomaly_detection import detect_anomaly
from ai.vendor_analysis import analyze_vendor

from functools import lru_cache

import zipfile
from openpyxl.styles import Font, Border, Side, Alignment
from backup_system import create_backup, create_full_backup, restore_backup, list_backups

from datetime import timedelta




load_dotenv()


# ==========================================================
# GEMINI AI CONFIGURATION
# ==========================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    logging.warning("GEMINI_API_KEY is not configured.")

gemini_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY
    else None
)

GEMINI_MODEL = "gemini-3.8-flash"

# ------------------------------
# Basic config & logging
# ------------------------------
app = Flask(__name__)

DAIRYMITR_ENV = os.getenv("DAIRYMITR_ENV", "production").strip().lower()
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

from core import audit as dm_audit
from core import db as dm_db
from core import security as dm_security

app.extensions["dm_mysql"] = mysql
dm_security.init_app(app)

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


def push_to_customer(user_id, vendor_id, title, body, url="/customer/dashboard"):
    print("========== PUSH TO CUSTOMER ==========")
    print("User:", user_id)
    print("Vendor:", vendor_id)

    try:
        cursor = SafeCursor(mysql.connection.cursor())

        cursor.execute("""
            SELECT id, endpoint, p256dh, auth
            FROM push_subscriptions
            WHERE user_id=%s AND vendor_id=%s
        """, (user_id, vendor_id))

        subs = cursor.fetchall()

        print("Subscriptions Found:", len(subs))

        for s in subs:
            print("Sending Push -> Subscription ID:", s["id"])

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

            print("Result:", success, expired)

            if expired:
                print("Deleting expired subscription:", s["id"])

                cursor.execute(
                    "DELETE FROM push_subscriptions WHERE id=%s",
                    (s["id"],)
                )
                mysql.connection.commit()

        cursor.close()

    except Exception as e:
        print("PUSH ERROR:", e)
        logging.exception("push_to_customer failed")


def create_notification(
    user_id,
    vendor_id,
    notification_type,
    title,
    message
):
    print("========== CREATE_NOTIFICATION ==========")
    print(user_id, vendor_id, notification_type)

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

        print("INSERT SUCCESS")

        mysql.connection.commit()

        print("COMMIT SUCCESS")

        cursor.close()

        push_to_customer(user_id, vendor_id, title, message)

    except Exception as e:
        print("ERROR IN create_notification:")
        print(type(e).__name__)
        print(str(e))
        raise

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

        title="Milk Collected",

        message=f"""Date : {date}
Time : {slot.title()}
Animal : {milk_type.title()}
Quantity : {quantity} L"""
    )

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
# DairyMitr AI tools are defined together further below.)


# ------------------------------
# Helper utilities
# ------------------------------
def send_sms(to, body):
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE:
        logging.warning("Twilio not configured; SMS skipped.")
        return False
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(to=to, from_=TWILIO_PHONE, body=body)
        return True
    except Exception:
        logging.exception("SMS sending failed")
        return False


BREVO_API_KEY = os.getenv("BREVO_API_KEY")

def send_email(to, subject, body):

    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = BREVO_API_KEY

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": to}],
        sender={"email": "dairymitra.official@gmail.com", "name": "DairyMitra"},
        subject=subject,
        text_content=body
    )

    try:
        api_instance.send_transac_email(email)
        print("EMAIL SENT SUCCESS")
        return True
    except ApiException as e:
        print("EMAIL ERROR:", e)
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

        if dm_security.rate_limited([
            ("login_ip", dm_security.client_ip(), 30, 900),
            ("login_email", email.lower(), 10, 900),
        ]):
            flash(dm_security.RATE_LIMIT_MESSAGE, 'danger')
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

            cursor.close()

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
            challenge_id, otp = dm_security.create_challenge(
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
                'DairyMitr - Staff Login OTP',
                f"""
DairyMitr Staff Login Verification

A staff member is trying to login to your DairyMitr account.

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
                dm_security.discard_challenge(challenge_id)
                session.clear()
                flash('Unable to send OTP to owner email. Please try again.', 'danger')
                return redirect(url_for('login'))

            flash('OTP has been sent to the owner email. Please enter the OTP.', 'info')
            return redirect(url_for('verify_staff_otp'))

        cursor.close()

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

        if dm_security.rate_limited([
            ("signup_ip", dm_security.client_ip(), 10, 3600),
            ("signup_email", email.lower(), 5, 3600),
        ]):
            flash(dm_security.RATE_LIMIT_MESSAGE, 'danger')
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

        challenge_id, otp = dm_security.create_challenge(
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
            dm_security.discard_challenge(challenge_id)
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
        if dm_security.rate_limited([("otp_ip", dm_security.client_ip(), 30, 900)]):
            flash(dm_security.RATE_LIMIT_MESSAGE, 'danger')
            return render_template('auth/verify_account.html',
                                   email=session.get('signup_email')), 429

        status, temp = dm_security.verify_challenge(
            challenge_id, "signup", request.form.get('otp'))

        if status == "invalid":
            flash('Invalid OTP.', 'danger')
            return render_template('auth/verify_account.html',
                                   email=session.get('signup_email'))

        if status != "ok":
            session.pop('signup_challenge_id', None)
            flash(dm_security.CHALLENGE_MESSAGES[status], 'danger')
            return redirect(url_for('signup'))

        session.pop('signup_challenge_id', None)
        session.pop('signup_email', None)

        try:
            with dm_db.transaction() as cur:
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
            f'Account verified successfully! Your Dairy ID is {dairy_code}',
            'success'
        )

        return redirect(url_for('login'))

    return render_template('auth/verify_account.html', email=session.get('signup_email'))


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()

        if dm_security.rate_limited([
            ("pwreset_ip", dm_security.client_ip(), 20, 3600),
            ("pwreset_email", email.lower(), 5, 3600),
        ]):
            flash(dm_security.RATE_LIMIT_MESSAGE, 'danger')
            return render_template('auth/forgot_password.html'), 429

        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute('SELECT id FROM users WHERE email = %s', (email,))
        account = cursor.fetchone()
        cursor.close()
        if account:
            challenge_id, otp = dm_security.create_challenge(
                "pwreset", email, {"email": email, "user_id": int(account['id'])},
                OTP_EXPIRY_MINUTES * 60)
            if not send_email(email, 'Password reset OTP', f"Your OTP: {otp}"):
                dm_security.discard_challenge(challenge_id)
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
        if dm_security.rate_limited([("otp_ip", dm_security.client_ip(), 30, 900)]):
            flash(dm_security.RATE_LIMIT_MESSAGE, 'danger')
            return render_template('auth/verify_reset_otp.html', email=email), 429

        # Max 5 attempts per OTP (previously unlimited, i.e. brute-forceable).
        status, payload = dm_security.verify_challenge(
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
            flash(dm_security.CHALLENGE_MESSAGES.get(status, 'Invalid or expired OTP.'),
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

        if dm_security.rate_limited([("otp_ip", dm_security.client_ip(), 30, 900)]):
            flash(dm_security.RATE_LIMIT_MESSAGE, 'danger')
            return render_template('auth/verify_staff_otp.html'), 429

        status, payload = dm_security.verify_challenge(
            session.get('staff_challenge_id'), "staff_login",
            request.form.get('otp', '').strip())

        if status == "invalid":
            flash('Invalid OTP. Please try again.', 'danger')
            return render_template('auth/verify_staff_otp.html')

        if status != "ok":
            session.clear()
            flash(dm_security.CHALLENGE_MESSAGES[status], 'danger')
            return redirect(url_for('login'))

        staff_id = payload['staff_id']
        owner_id = payload['owner_id']

        # Re-check the staff account in the database.
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute(
            """
            SELECT id, owner_id, email, vehicle_number, is_active
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
                blocked = dm_security.enforce_staff_allowlist()
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
            blocked = dm_security.enforce_staff_allowlist()
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
from fat_snf import service as fsn_service
from fat_snf.routes import bp as fat_snf_bp, render_receipt as fsn_render_receipt

app.register_blueprint(fat_snf_bp)


def _fat_snf_on():
    if session.get("role") not in ("owner", "staff", "customer"):
        return False
    try:
        return fsn_service.is_fat_snf(int(session.get("id")))
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
            return {"calc_mode": fsn_service.get_mode(int(session.get("id")))}
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
        WHERE user_id = %s AND DATE(date) = CURDATE()
        GROUP BY slot
        """,
        (user_id,)
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

    cursor = SafeCursor(mysql.connection.cursor())

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
            flash(f"❌ Vendor already exists at ID {vendor_id}", "danger")

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
        get_vendors_cached.cache_clear()

        flash("✔ Vendor Added Successfully", "success")

        return redirect(url_for('add_vendor'))

    return render_template(
        'vendors/add_vendor.html',
        next_vendor_id=next_vendor_id
    )


@app.route('/edit_vendor/<string:vendor_id>', methods=['GET', 'POST'])
def edit_vendor(vendor_id):

    cursor = SafeCursor(mysql.connection.cursor())

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
        get_vendors_cached.cache_clear()

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
    get_vendors_cached.cache_clear() 
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

@lru_cache(maxsize=128)
def get_vendors_cached(user_id):

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
    SELECT *
    FROM vendors
    WHERE user_id=%s
    ORDER BY vendor_id ASC
    """,(user_id,))

    return cursor.fetchall()


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


app.extensions["dm_hooks"] = {"general_rate": get_vendor_rate}

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

        date_val = request.form.get('date') or date.today().isoformat()
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

    ensure_user_id_int()

    data = request.get_json(silent=True) or {}

    vendor_id = data.get('vendor_id')
    milk_type = data.get('milk_type')
    quantity = data.get('quantity')
    force_save = data.get('force_save', False)

    date_val, slot_val = _get_date_slot_from_request(request)

    if data.get('date'):
        date_val = data.get('date')

    if data.get('slot'):
        slot_val = data.get('slot')

    if not vendor_id or quantity is None:
        return jsonify({"message": "Missing vendor_id or quantity"}), 400

    try:
        qty = float(quantity)
    except:
        return jsonify({"message": "Invalid quantity"}), 400

    cursor = SafeCursor(mysql.connection.cursor())

    # ===============================
    # Vendor ownership + phone
    # ===============================
    cursor.execute(
        "SELECT phone FROM vendors WHERE vendor_id = %s AND user_id = %s",
        (vendor_id, session['id'])
    )

    vendor = cursor.fetchone()

    if not vendor:
        return jsonify({"message": "Unauthorized vendor."}), 403

    # ===============================
    # Duplicate prevention
    # ===============================
    cursor.execute("""
        SELECT id FROM milk_collection
        WHERE vendor_id=%s AND user_id=%s AND date=%s AND slot=%s AND milk_type=%s
    """, (vendor_id, session['id'], date_val, slot_val, milk_type))

    if cursor.fetchone():
        return jsonify({
            "message": "Data already exists for this vendor/date/slot/milk_type."
        }), 409

    # ===============================
    # AI Anomaly Detection
    # ===============================
    try:
        cursor.execute("""
            SELECT quantity
            FROM milk_collection
            WHERE vendor_id=%s AND user_id=%s
            ORDER BY date DESC
            LIMIT 20
        """, (vendor_id, session['id']))

        rows = cursor.fetchall()
        previous_values = [r['quantity'] for r in rows]

        if detect_anomaly(previous_values, qty) and not force_save:
            return jsonify({
                "warning": "असामान्य दूध प्रमाण आढळले. कृपया तपासा."
            })

    except Exception:
        logging.exception("AI anomaly detection failed")

    # ===============================
    # Insert milk entry
    # ===============================
    try:

        staff_id = session.get("staff_id")  # staff login असेल तर id येईल

        cursor.execute("""
            INSERT INTO milk_collection
            (vendor_id, user_id, staff_id, date, slot, milk_type, quantity)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (
            vendor_id,
            session['id'],
            staff_id,
            date_val,
            slot_val,
            milk_type,
            qty
        ))

        mysql.connection.commit()

        # Create customer notification
        create_milk_notification(
            user_id=session['id'],
            vendor_id=vendor_id,
            date=date_val,
            slot=slot_val,
            milk_type=milk_type,
            quantity=qty
        )

        # Audit log
        audit_log(
            session['id'],
            'insert_milk',
            f"{vendor_id} {date_val} {slot_val} {milk_type} {qty}"
        )
        # ===============================
        # Send SMS to vendor
        # ===============================
        if vendor and vendor.get("phone"):

            slot_map = {
                "morning": "सकाळ",
                "evening": "संध्याकाळ"
            }

            milk_map = {
                "cow": "गाय",
                "buffalo": "म्हैस"
            }

            slot_marathi = slot_map.get(slot_val.lower(), slot_val)
            milk_marathi = milk_map.get(milk_type.lower(), milk_type)

            message = f"{date_val} रोजी तुमचे {milk_marathi}चे {slot_marathi}चे {qty} लिटर दूध जमा झाले."

            send_sms(vendor["phone"], message)

        return jsonify({
            "message": f"Saved for {vendor_id} ({milk_type})"
        }), 200

    except Exception:
        logging.exception("Error inserting milk_collection")
        return jsonify({
            "message": "Server error saving data."
        }), 500


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
            qty = float(quantity)
        except:
            skipped.append({"vendor_id": vendor_id, "reason": "invalid quantity"})
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

            # send sms (non-blocking, phone already prefetched - no extra query)
            if vendor.get('phone'):
                slot_map = {"morning": "सकाळ", "evening": "संध्याकाळ"}
                milk_map = {"cow": "गाय", "buffalo": "म्हैस"}

                slot_marathi = slot_map.get(slot_val.lower(), slot_val)
                milk_marathi = milk_map.get(milk_type.lower(), milk_type)

                message = f"{date_val} रोजी तुमचे {milk_marathi}चे {slot_marathi}चे {qty} लिटर दूध जमा झाले."
                send_sms(vendor['phone'], message)

        except Exception:
            logging.exception("Error on bulk insert")
            skipped.append({"vendor_id": vendor_id, "reason": "server error"})
            continue

    mysql.connection.commit()
    # Create notifications for all successfully saved entries
    for item in saved:
        create_milk_notification(
            user_id=user_id,
            vendor_id=item["vendor_id"],
            date=item["date"],
            slot=item["slot"],
            milk_type=item["milk_type"],
            quantity=item["quantity"]
        )
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

# ------------------------------
# Advance (safe add/update)
# ------------------------------
# ==============================================================================
# ADVANCE MANAGEMENT SYSTEM
# ------------------------------------------------------------------------------
# Ledger-based advance system.
#
# transaction_type:
#   advance  = money given to vendor
#   deduction = money recovered/cut from vendor's advance
#
# Remaining balance is ALWAYS calculated:
#
#   Total Advance Given - Total Deduction = Remaining Advance
#
# IMPORTANT:
# - Old `advance` table is NOT deleted.
# - Existing old data has already been migrated into
#   `advance_transactions`.
# - All queries are restricted by session['id'].
# - Vendor ownership is verified before every operation.
# - Vendor row is locked during financial mutations to avoid
#   concurrent balance corruption.
# - Amounts use Decimal/DECIMAL-safe values.
# ==============================================================================


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
        return date.today()

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
        today_date=date.today().isoformat()
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

    cursor = SafeCursor(
        mysql.connection.cursor()
    )

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
                transaction_date
            )
            VALUES
            (
                %s,
                %s,
                'advance',
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
                    f"Deduction cannot exceed remaining "
                    f"advance of ₹{current_balance:.2f}."
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
                    "हा ॲडव्हान्स एवढा कमी करता येणार नाही, कारण त्यातून आधीच "
                    f"₹{-balance_without_transaction:.2f} वजा झाले आहेत."
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
                        "Deduction cannot exceed available "
                        f"advance of "
                        f"₹{balance_without_transaction:.2f}."
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
        today_date=date.today().isoformat()
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

    selected_date = request.args.get('date') or date.today().isoformat()

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

    data = request.get_json()

    vendor_id = data.get('vendor_id')
    qty = data.get('quantity')
    sack_id = data.get('sack_id')
    date_val = data.get('date')

    if not vendor_id or not qty or not sack_id:
        return jsonify({"message": "Missing data"}), 400

    try:
        qty = int(qty)
        sack_id = int(sack_id)
    except:
        return jsonify({"message": "Invalid data"}), 400

    cursor = SafeCursor(mysql.connection.cursor())

    # rate fetch
    cursor.execute("""
        SELECT rate FROM food_sack_rates
        WHERE id=%s AND user_id=%s
    """, (sack_id, session['id']))
    res = cursor.fetchone()

    if not res:
        return jsonify({"message": "Invalid sack"}), 400

    rate = float(res['rate'])
    total = qty * rate

    # 🔥 UPDATE (duplicate nahi)
    cursor.execute("""
        SELECT id FROM food_sack
        WHERE vendor_id=%s AND user_id=%s AND date=%s AND sack_rate_id=%s
    """, (vendor_id, session['id'], date_val, sack_id))

    existing = cursor.fetchone()

    if existing:
        cursor.execute("""
            UPDATE food_sack
            SET sack_qty=%s, total_cost=%s
            WHERE id=%s
        """, (qty, total, existing['id']))
    else:
        cursor.execute("""
            INSERT INTO food_sack
            (vendor_id, user_id, date, sack_qty, sack_rate_id, sack_rate, total_cost)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (vendor_id, session['id'], date_val, qty, sack_id, rate, total))

    mysql.connection.commit()

    return jsonify({"message": f"Saved for vendor {vendor_id}"})


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
    data = request.get_json()
    entries = data.get("entries", [])

    if not entries:
        return jsonify({"message": "No data"}), 400

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

    # ---- Prefetch existing food_sack rows for involved vendor/date combos ----
    vendor_ids = list({item['vendor_id'] for item in entries if item.get('vendor_id')})
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

    return jsonify({"message": f"{saved} entries saved"})



@app.route('/add_food_sack', methods=['POST'])
def add_food_sack():
    if 'id' not in session:
        return redirect(url_for('login'))
    name = request.form.get('name')
    rate = request.form.get('rate')
    date_from = datetime.today().date()
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
        date_from = datetime.today().date()

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

    today = date.today().strftime(
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
        for (fsn_type, fsn_slot), (fsn_l, fsn_a) in fsn_service.fsn_slot_totals(
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
        fsn_amounts = fsn_service.fsn_amounts_by_vendor(user_id, start_date, end_date)

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
        for fsn_row in fsn_service.fsn_entries_in_range(user_id, from_date, to_date):
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
            to_date=to_date
        )

    # =========================================================
    # GET REQUEST
    # =========================================================

    cursor.close()

    return render_template(
        'receipt_all_vendors.html',
        receipts=None,
        from_date=None,
        to_date=None
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
    selected_date = request.args.get('date') or date.today().isoformat()

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT 
            v.vendor_id AS id,
            v.name,
            COALESCE(SUM(CASE 
                WHEN m.milk_type='cow' AND m.slot='morning' AND DATE(m.date)=%s 
                THEN m.quantity END),0) AS cow_morning,

            COALESCE(SUM(CASE 
                WHEN m.milk_type='cow' AND m.slot='evening' AND DATE(m.date)=%s 
                THEN m.quantity END),0) AS cow_evening,

            COALESCE(SUM(CASE 
                WHEN m.milk_type='buffalo' AND m.slot='morning' AND DATE(m.date)=%s 
                THEN m.quantity END),0) AS buffalo_morning,

            COALESCE(SUM(CASE 
                WHEN m.milk_type='buffalo' AND m.slot='evening' AND DATE(m.date)=%s 
                THEN m.quantity END),0) AS buffalo_evening

        FROM vendors v
        LEFT JOIN milk_collection m
            ON v.vendor_id = m.vendor_id
            AND v.user_id = m.user_id
        WHERE v.user_id = %s
        GROUP BY v.vendor_id, v.name
        ORDER BY v.vendor_id ASC
    """, (
        selected_date,
        selected_date,
        selected_date,
        selected_date,
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
    return {'now': datetime.utcnow()}




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
        from fat_snf.logic import parse_date_range, ValidationError as _VE
        start_d, end_d = parse_date_range(from_date, to_date)
    except _VE as e:
        return jsonify({"error": e.message}), 400

    if dm_security.rate_limited([("bank_report", session["id"], 20, 600)]):
        return jsonify({"error": dm_security.RATE_LIMIT_MESSAGE}), 429

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
    statements = fsn_service.build_statements(user_id, start_d, end_d, list(vendors))
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
            msg["To"] = "dairymitra.official@gmail.com"
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
    charts = fsn_service.list_charts(uid)
    return render_template(
        "settings.html",
        fsn=fsn_service.get_settings(uid),
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

    if dm_security.rate_limited([("delete_account", session["id"], 5, 3600)]):
        flash(dm_security.RATE_LIMIT_MESSAGE, "danger")
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
    return send_from_directory("static", "manifest.json")

@app.route("/service-worker.js")
def service_worker():
    return send_from_directory("static", "service-worker.js")

@app.after_request
def disable_cache(response):

    if request.path.startswith("/static") \
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
    if not dm_security.is_platform_admin():
        return dm_security.json_error(
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
        return dm_security.json_error("Backup तयार करता आला नाही.", 500, code="backup_failed")

    audit_log(session["id"], "backup_create", filename)
    return jsonify({"status": "success", "file": filename})


@app.route("/restore_backup/<filename>", methods=["POST"])
def restore_backup_route(filename):

    denied = _require_platform_admin()
    if denied:
        return denied

    if not BACKUP_NAME_RE.match(filename) or filename not in list_backups():
        return dm_security.json_error("Backup file सापडली नाही.", 404, code="not_found")

    try:
        restore_backup(filename)
    except Exception:
        logging.exception("restore failed for %s", filename)
        return dm_security.json_error("Restore करता आला नाही.", 500, code="restore_failed")

    dm_audit.record("backup_restore", "backup", filename)
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
        return dm_security.json_error("Backup तयार करता आला नाही.", 500, code="backup_failed")

    audit_log(session["id"], "backup_full", filename)
    return jsonify({"status": "success", "file": filename})


@app.route("/backup_history")
def backup_history():

    if not dm_security.is_platform_admin():
        return jsonify({"files": [], "admin": False})

    return jsonify({"files": list_backups(), "admin": True})


@app.route("/backup_page")
def backup_page():
    return render_template("backup.html", is_admin=dm_security.is_platform_admin())



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
        if dm_security.rate_limited([
            ("customer_login_ip", dm_security.client_ip(), 20, 900),
            ("customer_login_dairy", dairy_id, 50, 900),
        ]):
            flash(dm_security.RATE_LIMIT_MESSAGE, "danger")
            return render_template("customer/login.html"), 429

        cursor = SafeCursor(mysql.connection.cursor())

        cursor.execute("""
            SELECT
                v.id,
                v.vendor_id,
                v.name,
                v.phone,
                v.user_id,
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

            flash("Customer Login Successful", "success")

            return redirect(url_for("customer_dashboard"))

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
    if fsn_service.is_fat_snf(user_id) and request.args.get("legacy") != "1":
        return fsn_render_receipt(
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
                    "%d-%m-%Y"
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
                    "%d-%m-%Y"
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
                    date.today().strftime('%d-%m-%Y')
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
        print("UPDATED EXISTING SUBSCRIPTION")
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
        print("INSERTED NEW SUBSCRIPTION")

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
        cur = dm_db.cursor()
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

You are DairyMitr AI, the intelligent AI assistant of DairyMitr.

DairyMitr is a dairy management platform used by dairy owners
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

You have access to controlled DairyMitr database tools.

When the user asks for ACTUAL DairyMitr data:

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

which represents the current DairyMitr owner/user.

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
    from fat_snf.logic import parse_date_range
    today = dairymitr_ai_today()
    start = start_date or str(today)
    end = end_date or start
    return parse_date_range(start, end)


def _ai_vendors(user_id, vendor_name=""):
    vendors = fsn_service.list_vendors(user_id)
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
        statements = fsn_service.build_statements(
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

        cursor = dm_db.cursor()
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

        cursor = dm_db.cursor()
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
                "You are not authorized to use DairyMitr AI."

        }), 403


    # ======================================================
    # GEMINI
    # ======================================================

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


    if dm_security.rate_limited([("ai_chat", session.get("id"), 40, 600)]):
        return jsonify({"success": False, "error": dm_security.RATE_LIMIT_MESSAGE}), 429


    # ======================================================
    # GEMINI
    # ======================================================

    try:

        logging.info(
            "DairyMitr AI request | user_id=%s",
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
            "DairyMitr AI error"
        )

        return jsonify({

            "success": False,

            "error":
                "DairyMitr AI सध्या उत्तर देऊ शकत नाही. कृपया थोड्या वेळाने पुन्हा प्रयत्न करा."

        }), 500










































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