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

# ------------------------------
# Basic config & logging
# ------------------------------
app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "d7e5f19e4c2a4a7b93c6f405f3d9a8c3b1a0c9e7e8d5f4c2a7b6f5e3a9d0c8f2")

app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# MySQL configuration from environment
app.config['MYSQL_HOST'] = os.getenv("MYSQL_HOST")
app.config['MYSQL_USER'] = os.getenv("MYSQL_USER")
app.config['MYSQL_PASSWORD'] = os.getenv("MYSQL_PASSWORD")
app.config['MYSQL_DB'] = os.getenv("MYSQL_DB")
app.config["MYSQL_PORT"] = int(os.getenv("MYSQL_PORT", 3306))
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'

app.config['MYSQL_CONNECT_TIMEOUT'] = 30
app.config['MYSQL_READ_DEFAULT_FILE'] = ''
app.config['MYSQL_AUTOCOMMIT'] = True


mysql = MySQL(app)

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
LOG_FILE = os.path.join(os.getcwd(), "audit.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


def audit_log(user_id, action, details=""):
    """Append an audit log entry (file-based)."""
    try:
        logging.info(f"user_id={user_id} action={action} details={details}")
    except Exception as e:
        print("Audit logging failed:", e)



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

        email = request.form.get('email')
        password = request.form.get('password')

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

            flash('Logged in successfully!', 'success')

            return redirect(url_for('dashboard'))

        # ==========================================================
        # STAFF LOGIN
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

        # ----------------------------------------------------------
        # STAFF PASSWORD CHECK
        # ----------------------------------------------------------
        if staff and check_password_hash(
            staff['password'],
            password
        ):

            # ------------------------------------------------------
            # FIND OWNER EMAIL
            # ------------------------------------------------------
            cursor.execute(
                """
                SELECT id, email, dairy_name
                FROM users
                WHERE id=%s
                """,
                (staff['owner_id'],)
            )

            owner = cursor.fetchone()

            if not owner:
                cursor.close()

                flash(
                    'Owner account not found.',
                    'danger'
                )

                return redirect(url_for('login'))

            # ------------------------------------------------------
            # GENERATE STAFF LOGIN OTP
            # ------------------------------------------------------
            otp = generate_otp()

            # ------------------------------------------------------
            # CREATE TEMPORARY LOGIN SESSION
            #
            # IMPORTANT:
            # loggedin=True is NOT set here.
            #
            # Staff gets actual login session ONLY after
            # OTP verification.
            # ------------------------------------------------------
            session.clear()
            session.permanent = True

            session['staff_login_pending'] = True

            session['pending_staff_id'] = int(staff['id'])
            session['pending_owner_id'] = int(staff['owner_id'])

            session['pending_staff_email'] = staff['email']
            session['pending_vehicle'] = staff.get('vehicle_number')

            session['staff_login_otp'] = otp

            session['staff_login_otp_expiry'] = (
                datetime.now(timezone.utc)
                + timedelta(minutes=OTP_EXPIRY_MINUTES)
            ).isoformat()

            # ------------------------------------------------------
            # SEND OTP TO OWNER EMAIL
            # ------------------------------------------------------
            email_sent = send_email(
                owner['email'],
                'DairyMitra - Staff Login OTP',
                f"""
DairyMitra Staff Login Verification

A staff member is trying to login to your DairyMitra account.

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

            cursor.close()

            # ------------------------------------------------------
            # EMAIL FAILED
            # ------------------------------------------------------
            if not email_sent:

                session.clear()

                flash(
                    'Unable to send OTP to owner email. Please try again.',
                    'danger'
                )

                return redirect(url_for('login'))

            # ------------------------------------------------------
            # OTP SENT SUCCESSFULLY
            # ------------------------------------------------------
            flash(
                'OTP has been sent to the owner email. Please enter the OTP.',
                'info'
            )

            return redirect(url_for('verify_staff_otp'))

        # ----------------------------------------------------------
        # INVALID LOGIN
        # ----------------------------------------------------------
        cursor.close()

        flash(
            'Invalid credentials!',
            'danger'
        )

    return render_template('auth/login.html')



@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'success')
    return redirect(url_for('login'))


@app.route('/signup', methods=['GET', 'POST'])
def signup():

    if 'loggedin' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':

        dairy_name = request.form.get('dairy_name', '').strip()
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        phone = request.form.get('phone', '').strip()

        # password validation
        password_pattern = r'^(?=.*[A-Za-z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$'

        if not re.match(password_pattern, password):
            flash('पासवर्ड किमान 8 अक्षरे असावा, त्यात अक्षरे, अंक आणि symbol असणे आवश्यक आहे.', 'danger')
            return redirect(url_for('signup'))

        if password != confirm_password:
            flash('Passwords do not match!', 'danger')
            return redirect(url_for('signup'))

        if not re.match(r'[^@]+@[^@]+\.[^@]+', email):
            flash('Invalid email!', 'danger')
            return redirect(url_for('signup'))

        cursor = SafeCursor(mysql.connection.cursor())

        cursor.execute(
            "SELECT id FROM users WHERE email = %s",
            (email,)
        )

        if cursor.fetchone():
            flash('Account already exists!', 'warning')
            return redirect(url_for('signup'))

        # OTP generate
        otp = generate_otp()

        hashed_password = generate_password_hash(password)

        # temporarily store signup data
        session['temp_signup'] = {
            'email': email,
            'password': hashed_password,
            'phone': phone,
            'dairy_name': dairy_name,
            'otp': otp
        }

        email_sent = send_email(
            email,
            "तुमचा OTP",
            f"तुमचा OTP: {otp}"
        )

        flash('OTP sent. Please verify.', 'info')

        return redirect(url_for('verify_account'))

    return render_template('auth/signup.html')


@app.route('/verify-account', methods=['GET', 'POST'])
def verify_account():
    temp = session.get('temp_signup')
    if not temp:
        flash('Session expired. Signup again.', 'warning')
        return redirect(url_for('signup'))

    if request.method == 'POST':
        otp_entered = request.form.get('otp')
        if otp_entered == temp.get('otp'):
            cursor = SafeCursor(mysql.connection.cursor())

            # ----------------------------
            # Insert new user
            # ----------------------------
            cursor.execute("""
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

            mysql.connection.commit()

            # ----------------------------
            # Generate Dairy ID
            # ----------------------------
            user_id = cursor.lastrowid
            dairy_code = f"DM{user_id:06d}"

            cursor.execute("""
                UPDATE users
                SET dairy_code = %s
                WHERE id = %s
            """, (
                dairy_code,
                user_id
            ))

            mysql.connection.commit()

            session.pop('temp_signup', None)

            flash(
                f'Account verified successfully! Your Dairy ID is {dairy_code}',
                'success'
            )

            return redirect(url_for('login'))
        else:
            flash('Invalid OTP.', 'danger')

    return render_template('auth/verify_account.html', email=temp.get('email'))

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute('SELECT id FROM users WHERE email = %s', (email,))
        account = cursor.fetchone()
        if account:
            otp = generate_otp()
            cursor.execute("""
                UPDATE users SET otp = %s, otp_expiry = DATE_ADD(NOW(), INTERVAL %s MINUTE)
                WHERE email = %s
            """, (otp, OTP_EXPIRY_MINUTES, email))
            mysql.connection.commit()
            send_email(email, 'Password reset OTP', f"Your OTP: {otp}")
            session['reset_email'] = email
            flash('OTP sent to email.', 'info')
            return redirect(url_for('verify_reset_otp'))
        else:
            flash('Email not found.', 'danger')
    return render_template('auth/forgot_password.html')


@app.route('/verify-reset-otp', methods=['GET', 'POST'])
def verify_reset_otp():
    if 'reset_email' not in session:
        flash('Session expired.', 'warning')
        return redirect(url_for('forgot_password'))

    email = session['reset_email']
    if request.method == 'POST':
        otp = request.form.get('otp')
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute('SELECT otp, otp_expiry FROM users WHERE email = %s', (email,))
        acc = cursor.fetchone()
        if not acc:
            flash('Invalid request.', 'danger')
            return redirect(url_for('forgot_password'))

        now_utc = datetime.now(timezone.utc)
        otp_expiry = acc.get('otp_expiry')

        # ✅ Fix: ensure both datetimes are comparable
        if otp_expiry and otp_expiry.tzinfo is None:
            otp_expiry = otp_expiry.replace(tzinfo=timezone.utc)

        if otp == acc.get('otp') and (not otp_expiry or now_utc < otp_expiry):
            session['otp_verified'] = True
            flash('OTP verified. Set new password.', 'success')
            return redirect(url_for('reset_password'))
        else:
            flash('Invalid or expired OTP.', 'danger')

    return render_template('auth/verify_reset_otp.html', email=email)

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if not session.get('otp_verified') or 'reset_email' not in session:
        flash('Unauthorized access.', 'warning')
        return redirect(url_for('forgot_password'))
    email = session['reset_email']
    if request.method == 'POST':
        pwd = request.form.get('password')
        cpwd = request.form.get('confirm_password')
        if pwd != cpwd:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('reset_password'))
        hashed = generate_password_hash(pwd)
        cursor = SafeCursor(mysql.connection.cursor())
        cursor.execute("UPDATE users SET password = %s, otp = NULL, otp_expiry = NULL WHERE email = %s", (hashed, email))
        mysql.connection.commit()
        session.pop('otp_verified', None)
        session.pop('reset_email', None)
        flash('Password updated. Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('auth/reset_password.html')

@app.route('/verify-staff-otp', methods=['GET', 'POST'])
def verify_staff_otp():

    # ==========================================================
    # CHECK PENDING STAFF LOGIN
    # ==========================================================
    if not session.get('staff_login_pending'):

        flash(
            'Session expired. Please login again.',
            'warning'
        )

        return redirect(url_for('login'))

    # ==========================================================
    # OTP SUBMISSION
    # ==========================================================
    if request.method == 'POST':

        otp_entered = request.form.get('otp', '').strip()

        stored_otp = session.get('staff_login_otp')

        expiry_string = session.get(
            'staff_login_otp_expiry'
        )

        # ------------------------------------------------------
        # CHECK OTP SESSION DATA
        # ------------------------------------------------------
        if not stored_otp or not expiry_string:

            session.clear()

            flash(
                'OTP session expired. Please login again.',
                'warning'
            )

            return redirect(url_for('login'))

        # ------------------------------------------------------
        # CONVERT EXPIRY TIME
        # ------------------------------------------------------
        try:

            otp_expiry = datetime.fromisoformat(
                expiry_string
            )

            if otp_expiry.tzinfo is None:

                otp_expiry = otp_expiry.replace(
                    tzinfo=timezone.utc
                )

        except Exception:

            session.clear()

            flash(
                'Invalid OTP session. Please login again.',
                'warning'
            )

            return redirect(url_for('login'))

        # ------------------------------------------------------
        # CURRENT UTC TIME
        # ------------------------------------------------------
        now_utc = datetime.now(timezone.utc)

        # ------------------------------------------------------
        # OTP EXPIRED
        # ------------------------------------------------------
        if now_utc >= otp_expiry:

            session.clear()

            flash(
                'OTP has expired. Please login again.',
                'danger'
            )

            return redirect(url_for('login'))

        # ------------------------------------------------------
        # OTP INCORRECT
        # ------------------------------------------------------
        if otp_entered != stored_otp:

            flash(
                'Invalid OTP. Please try again.',
                'danger'
            )

            return render_template(
                'auth/verify_staff_otp.html'
            )

        # ======================================================
        # OTP CORRECT
        # ======================================================

        staff_id = session.get(
            'pending_staff_id'
        )

        owner_id = session.get(
            'pending_owner_id'
        )

        # ------------------------------------------------------
        # BASIC SESSION DATA CHECK
        # ------------------------------------------------------
        if not staff_id or not owner_id:

            session.clear()

            flash(
                'Invalid login session. Please login again.',
                'danger'
            )

            return redirect(url_for('login'))

        # ------------------------------------------------------
        # CHECK STAFF FROM DATABASE AGAIN
        # ------------------------------------------------------
        cursor = SafeCursor(
            mysql.connection.cursor()
        )

        cursor.execute(
            """
            SELECT
                id,
                owner_id,
                email,
                vehicle_number,
                is_active
            FROM staff
            WHERE id=%s
            """,
            (staff_id,)
        )

        staff = cursor.fetchone()

        cursor.close()

        # ------------------------------------------------------
        # STAFF NOT FOUND
        # ------------------------------------------------------
        if not staff:

            session.clear()

            flash(
                'Staff account not found.',
                'danger'
            )

            return redirect(url_for('login'))

        # ------------------------------------------------------
        # STAFF DISABLED
        # ------------------------------------------------------
        if int(staff['is_active']) != 1:

            session.clear()

            flash(
                'Your account has been disabled by owner.',
                'danger'
            )

            return redirect(url_for('login'))

        # ------------------------------------------------------
        # OWNER CHECK
        # ------------------------------------------------------
        if int(staff['owner_id']) != int(owner_id):

            session.clear()

            flash(
                'Invalid staff login session.',
                'danger'
            )

            return redirect(url_for('login'))

        # ======================================================
        # OTP VERIFIED
        #
        # NOW CREATE REAL LOGIN SESSION
        # ======================================================

        session.clear()

        session.permanent = True

        # ------------------------------------------------------
        # LOGIN STATUS
        # ------------------------------------------------------
        session['loggedin'] = True

        # ------------------------------------------------------
        # ROLE
        # ------------------------------------------------------
        session['role'] = 'staff'

        # ------------------------------------------------------
        # STAFF INFORMATION
        # ------------------------------------------------------
        session['staff_id'] = int(
            staff['id']
        )

        session['owner_id'] = int(
            staff['owner_id']
        )

        session['vehicle'] = (
            staff['vehicle_number']
        )

        # ------------------------------------------------------
        # IMPORTANT:
        # Existing application uses owner ID as session['id']
        # ------------------------------------------------------
        session['id'] = int(
            staff['owner_id']
        )

        # ------------------------------------------------------
        # OPTIONAL STAFF EMAIL
        # ------------------------------------------------------
        session['email'] = staff['email']

        # ------------------------------------------------------
        # AUDIT LOG
        # ------------------------------------------------------
        audit_log(
            staff['id'],
            'staff_login',
            f'OTP verified successfully for owner_id={staff["owner_id"]}'
        )

        # ------------------------------------------------------
        # LOGIN SUCCESS
        # ------------------------------------------------------
        flash(
            'Staff login successful!',
            'success'
        )

        return redirect(
            url_for('dashboard')
        )

    # ==========================================================
    # GET REQUEST
    # ==========================================================
    return render_template(
        'auth/verify_staff_otp.html'
    )

@app.before_request
def require_login():

    print("=" * 60)
    print("PATH :", request.path)
    print("COOKIE:", request.cookies)
    print("SESSION:", dict(session))
    print("=" * 60)

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

            if session.get('role') in (
                'customer',
                'owner',
                'staff'
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
# ------------------------------
# Dashboard
# ------------------------------
from datetime import date

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

        name = request.form.get("name")
        email = request.form.get("email")
        password = generate_password_hash(request.form.get("password"))
        vehicle = request.form.get("vehicle")

        cursor = SafeCursor(mysql.connection.cursor())

        cursor.execute("""
        INSERT INTO staff
        (owner_id,name,email,password,vehicle_number)
        VALUES (%s,%s,%s,%s,%s)
        """,(session["id"],name,email,password,vehicle))

        mysql.connection.commit()

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

        cursor.execute("""
        UPDATE staff
        SET name=%s,email=%s,vehicle_number=%s
        WHERE id=%s AND owner_id=%s
        """,(name,email,vehicle,staff_id,session["id"]))

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

@app.route('/disable_staff/<int:staff_id>')
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

@app.route('/enable_staff/<int:staff_id>')
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


@app.route('/delete_staff/<int:staff_id>')
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
    print("DEBUG TIME (IST):", now.strftime("%Y-%m-%d %H:%M:%S"), "Hour:", now.hour)
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

    today = date.today().isoformat()

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
@app.route('/advance', methods=['GET', 'POST'])
def advance():
    cursor = SafeCursor(mysql.connection.cursor())
    cursor.execute("""
SELECT * FROM vendors
WHERE user_id = %s
ORDER BY vendor_id ASC
""", (session['id'],))
    vendors = cursor.fetchall()

    if request.method == 'POST':
        # Support both JSON and form POST
        if request.is_json:
            data = request.get_json(silent=True) or {}
            entry_date = data.get('date') or date.today().isoformat()
        else:
            data = request.form
            entry_date = request.form.get('date') or date.today().isoformat()

        # OPTIMIZATION: prefetch existing advance rows for this date ONCE
        # instead of running 1 SELECT per vendor (BEFORE: up to N queries,
        # AFTER: 1 query, regardless of vendor count).
        cursor.execute("""
            SELECT id, vendor_id, amount FROM advance
            WHERE user_id=%s AND date=%s
        """, (session['id'], entry_date))
        existing_map = {str(r['vendor_id']): r for r in cursor.fetchall()}

        for v in vendors:
            vid = v['vendor_id']
            # For JSON payload, the key will match "advance_<vendor_id>"
            amount = data.get(f'advance_{vid}')
            if not amount:
                continue
            try:
                amt = float(amount)
            except:
                continue

            # check existing (from prefetched map)
            existing = existing_map.get(str(vid))
            if existing:
                # update to new total (keep behavior: add amount)
                new_amt = float(existing['amount']) + amt
                cursor.execute("UPDATE advance SET amount=%s WHERE id=%s", (new_amt, existing['id']))
                existing['amount'] = new_amt  # keep map consistent within loop
            else:
                cursor.execute(
                    "INSERT INTO advance (vendor_id, user_id, date, amount) VALUES (%s,%s,%s,%s)",
                    (vid, session['id'], entry_date, amt)
                )

        mysql.connection.commit()
        audit_log(session['id'], 'advance_update', f"date={entry_date}")

        # Return JSON if AJAX request
        if request.is_json:
            return jsonify({"success": True, "message": "Advance saved successfully."}), 200
        else:
            flash('Advance saved.', 'success')
            return redirect(url_for('advance'))

    return render_template('milk_operations/advance.html',
                           vendors=vendors,
                           today_date=date.today().isoformat())


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

@app.route('/edit_entry', methods=['GET','POST'])
def edit_entry():

    if "id" not in session:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT vendor_id,name,milk_type
        FROM vendors
        WHERE user_id=%s
        ORDER BY vendor_id ASC
    """,(session['id'],))

    vendors = cursor.fetchall()

    data=[]
    selected_vendor_type=None

    vendor_id=request.args.get("vendor_id")
    from_date=request.args.get("from_date")
    to_date=request.args.get("to_date")

    if vendor_id and from_date and to_date:

        cursor.execute("""
            SELECT milk_type
            FROM vendors
            WHERE vendor_id=%s AND user_id=%s
        """,(vendor_id,session['id']))

        vendor_info=cursor.fetchone()

        if not vendor_info:
            flash("Unauthorized vendor.","danger")
            return redirect(url_for("edit_entry"))

        selected_vendor_type=vendor_info["milk_type"]

        # 🔹 LOAD ALL MILK DATA (1 query)
        cursor.execute("""
            SELECT date,slot,milk_type,quantity
            FROM milk_collection
            WHERE vendor_id=%s
            AND user_id=%s
            AND date BETWEEN %s AND %s
        """,(vendor_id,session['id'],from_date,to_date))

        milk_rows=cursor.fetchall()

        milk_map={}
        for r in milk_rows:
            d=r['date'].strftime("%Y-%m-%d")
            key=(d,r['slot'],r['milk_type'])
            milk_map[key]=r['quantity']

        # 🔹 LOAD ADVANCE DATA (1 query)
        cursor.execute("""
            SELECT date,amount
            FROM advance
            WHERE vendor_id=%s
            AND user_id=%s
            AND date BETWEEN %s AND %s
        """,(vendor_id,session['id'],from_date,to_date))

        adv_rows=cursor.fetchall()

        advance_map={}
        for a in adv_rows:
            d=a['date'].strftime("%Y-%m-%d")
            advance_map[d]=a['amount']

        start=datetime.strptime(from_date,"%Y-%m-%d")
        end=datetime.strptime(to_date,"%Y-%m-%d")

        d=start

        while d<=end:

            ds=d.strftime("%Y-%m-%d")

            rec={
                "date":ds,
                "cow_morning":milk_map.get((ds,"morning","cow"),0),
                "cow_evening":milk_map.get((ds,"evening","cow"),0),
                "buffalo_morning":milk_map.get((ds,"morning","buffalo"),0),
                "buffalo_evening":milk_map.get((ds,"evening","buffalo"),0),
                "advance_amt":advance_map.get(ds,0)
            }

            data.append(rec)

            d+=timedelta(days=1)
    today = date.today().strftime('%Y-%m-%d')
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
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    vendor_id = request.form.get("vendor_id")
    from_date = request.form.get("from_date")
    to_date = request.form.get("to_date")

    cursor = SafeCursor(mysql.connection.cursor())

    # Ownership + milk_type
    cursor.execute("""
        SELECT milk_type
        FROM vendors
        WHERE vendor_id=%s AND user_id=%s
    """, (vendor_id, session['id']))
    vendor_info = cursor.fetchone()

    if not vendor_info:
        flash("Unauthorized vendor.", "danger")
        return redirect(url_for("edit_entry"))

    vendor_type = vendor_info["milk_type"]

    # Allowed milk types
    if vendor_type == "cow":
        allowed_types = ["cow"]
    elif vendor_type == "buffalo":
        allowed_types = ["buffalo"]
    else:
        allowed_types = ["cow", "buffalo"]

    # Load existing milk entries
    cursor.execute("""
        SELECT id, date, slot, milk_type, quantity
        FROM milk_collection
        WHERE vendor_id=%s AND user_id=%s
        AND date BETWEEN %s AND %s
    """, (vendor_id, session['id'], from_date, to_date))

    existing = cursor.fetchall() or {}
    existing_map = {}

    for r in existing:
        dstr = r['date'].strftime("%Y-%m-%d")
        existing_map[(dstr, r['slot'], r['milk_type'])] = {
            'id': r['id'],
            'quantity': float(r['quantity'])
        }

    # Advances
    cursor.execute("""
        SELECT id, date, amount
        FROM advance
        WHERE vendor_id=%s AND user_id=%s
        AND date BETWEEN %s AND %s
    """, (vendor_id, session['id'], from_date, to_date))

    advs = cursor.fetchall() or {}
    adv_map = {}

    for a in advs:
        dstr = a['date'].strftime("%Y-%m-%d")
        adv_map[dstr] = {
            'id': a['id'],
            'amount': float(a['amount'])
        }

    cur_date = datetime.strptime(from_date, "%Y-%m-%d").date()
    end_date = datetime.strptime(to_date, "%Y-%m-%d").date()

    while cur_date <= end_date:
        ds = cur_date.strftime("%Y-%m-%d")

        for milk_type in allowed_types:
            for slot in ('morning', 'evening'):

                field_name = f"{milk_type}_{slot}_{ds}"
                qty = float(request.form.get(field_name) or 0)

                key = (ds, slot, milk_type)
                existing_row = existing_map.get(key)

                if qty > 0:
                    if existing_row:
                        if abs(existing_row['quantity'] - qty) > 1e-9:
                            cursor.execute(
                                "UPDATE milk_collection SET quantity=%s WHERE id=%s",
                                (qty, existing_row['id'])
                            )
                    else:
                        cursor.execute("""
                            INSERT INTO milk_collection
                            (vendor_id, user_id, date, slot, milk_type, quantity)
                            VALUES (%s,%s,%s,%s,%s,%s)
                        """, (vendor_id, session['id'], ds, slot, milk_type, qty))
                else:
                    if existing_row:
                        cursor.execute(
                            "DELETE FROM milk_collection WHERE id=%s",
                            (existing_row['id'],)
                        )

        # Advance
        adv_field = f"advance_{ds}"
        adv_amt = float(request.form.get(adv_field) or 0)
        existing_adv = adv_map.get(ds)

        if adv_amt > 0:
            if existing_adv:
                if abs(existing_adv['amount'] - adv_amt) > 1e-9:
                    cursor.execute(
                        "UPDATE advance SET amount=%s WHERE id=%s",
                        (adv_amt, existing_adv['id'])
                    )
            else:
                cursor.execute("""
                    INSERT INTO advance (vendor_id, user_id, date, amount)
                    VALUES (%s,%s,%s,%s)
                """, (vendor_id, session['id'], ds, adv_amt))
        else:
            if existing_adv:
                cursor.execute(
                    "DELETE FROM advance WHERE id=%s",
                    (existing_adv['id'],)
                )

        cur_date += timedelta(days=1)

    mysql.connection.commit()
    flash("Entries updated successfully.", "success")
    return redirect(url_for(
    "edit_entry",
    vendor_id=vendor_id,
    from_date=from_date,
    to_date=to_date
))

@app.route('/delete_entry', methods=['POST'])
def delete_entry():

    if "id" not in session:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    vendor_id = request.form.get('vendor_id')
    date_str = request.form.get('date')
    confirm = request.form.get('confirm')

    if str(confirm) != '1':
        flash("Please confirm deletion.", "warning")
        return redirect(url_for('edit_entry'))

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT 1 FROM vendors
        WHERE vendor_id=%s AND user_id=%s
    """, (vendor_id, session['id']))

    if not cursor.fetchone():
        flash("Unauthorized to delete.", "danger")
        return redirect(url_for('edit_entry'))

    try:
        cursor.execute("DELETE FROM milk_collection WHERE vendor_id=%s AND user_id=%s AND date=%s",
                       (vendor_id, session['id'], date_str))
        cursor.execute("DELETE FROM advance WHERE vendor_id=%s AND user_id=%s AND date=%s",
                       (vendor_id, session['id'], date_str))
        cursor.execute("DELETE FROM food_sack WHERE vendor_id=%s AND user_id=%s AND date=%s",
                       (vendor_id, session['id'], date_str))

        mysql.connection.commit()
        flash("Entry deleted successfully.", "success")

    except Exception as e:
        mysql.connection.rollback()
        flash("Error deleting entry.", "danger")

    return redirect(url_for(
    "edit_entry",
    vendor_id=vendor_id,
    from_date=request.form.get("from_date"),
    to_date=request.form.get("to_date")
))
# ------------------------------
# Receipts, calculation, payment (kept logic but with small safety)
# ------------------------------
@app.route('/calculation', methods=['GET', 'POST'])
def calculation():

    if 'id' not in session:
        flash('Please login first.', 'danger')
        return redirect(url_for('login'))

    cursor = SafeCursor(mysql.connection.cursor())

    cursor.execute("""
        SELECT *
        FROM vendors
        WHERE user_id=%s
        ORDER BY vendor_id ASC
    """,(session['id'],))

    vendors = cursor.fetchall()

    results=None

    if request.method=='POST':

        vendor_id = request.form.get('vendor_id')
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')

        cursor.execute("""
            SELECT date,slot,milk_type,quantity
            FROM milk_collection
            WHERE vendor_id=%s AND user_id=%s
            AND date BETWEEN %s AND %s
        """,(vendor_id,session['id'],start_date,end_date))

        milk_data = cursor.fetchall()

        mcq=ecq=mbq=ebq=0
        mcp=ecp=mbp=ebp=0

        # NOTE: get_vendor_rate() now resolves against an in-memory,
        # per-request rate cache (see _load_rate_cache) instead of hitting
        # MySQL on every row - same output, far fewer queries.
        for row in milk_data:

            rate = get_vendor_rate(cursor, vendor_id, row['milk_type'], row['date'])

            amt = float(row['quantity']) * rate

            if row['milk_type']=="cow":

                if row['slot']=="morning":
                    mcq+=row['quantity']; mcp+=amt
                else:
                    ecq+=row['quantity']; ecp+=amt

            else:

                if row['slot']=="morning":
                    mbq+=row['quantity']; mbp+=amt
                else:
                    ebq+=row['quantity']; ebp+=amt

        cursor.execute("""
            SELECT SUM(amount) total
            FROM advance
            WHERE vendor_id=%s AND user_id=%s
            AND date BETWEEN %s AND %s
        """,(vendor_id,session['id'],start_date,end_date))

        total_advance = cursor.fetchone()['total'] or 0

        cursor.execute("""
            SELECT SUM(total_cost) total
            FROM food_sack
            WHERE vendor_id=%s AND user_id=%s
            AND date BETWEEN %s AND %s
        """,(vendor_id,session['id'],start_date,end_date))

        total_food = cursor.fetchone()['total'] or 0

        milk_total = mcp+ecp+mbp+ebp
        final_payment = milk_total-(total_advance+total_food)

        results={

            'morning_cow_quantity':mcq,
            'evening_cow_quantity':ecq,
            'morning_cow_payment':mcp,
            'evening_cow_payment':ecp,
            'total_cow_quantity':mcq+ecq,
            'total_cow_payment':mcp+ecp,

            'morning_buffalo_quantity':mbq,
            'evening_buffalo_quantity':ebq,
            'morning_buffalo_payment':mbp,
            'evening_buffalo_payment':ebp,
            'total_buffalo_quantity':mbq+ebq,
            'total_buffalo_payment':mbp+ebp,

            'total_milk_quantity':mcq+ecq+mbq+ebq,
            'milk_total':milk_total,
            'total_food_sack_cost':total_food,
            'total_advance':total_advance,
            'final_payable_amount':final_payment

        }

    return render_template(
        'milk_operations/calculation.html',
        vendors=vendors,
        results=results
    )
    
    
@app.route('/payment', methods=['GET', 'POST'])
def payment():
    """
    OPTIMIZATION NOTE:
    BEFORE: for V vendors this ran 3 queries PER vendor (milk_collection,
            advance, food_sack) = 3V queries, PLUS get_vendor_rate() ran up
            to 2 more queries per milk row inside that loop (effectively
            unbounded). A 100-vendor report over a month could hit thousands
            of queries -> worker timeout.
    AFTER:  milk_collection, advance and food_sack are each fetched in ONE
            bulk, grouped query for the whole date range (3 queries total),
            then looped in Python using dict lookups. get_vendor_rate() now
            reads from the in-memory rate cache. Total queries for the whole
            report: ~5, regardless of vendor count.
    Output is mathematically identical - same GROUP BY math, just done once
    instead of once per vendor.
    """

    if 'id' not in session:
        flash('Please login first.', 'danger')
        return redirect(url_for('login'))

    # POST → redirect
    if request.method == 'POST':

        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')

        return redirect(url_for(
            'payment',
            start_date=start_date,
            end_date=end_date
        ))

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    cursor = SafeCursor(mysql.connection.cursor())

    data = []

    if start_date and end_date:

        user_id = session['id']

        # load vendors
        cursor.execute("""
            SELECT vendor_id,name
            FROM vendors
            WHERE user_id=%s
            ORDER BY vendor_id ASC
        """,(user_id,))

        vendors = cursor.fetchall()

        # ---- bulk milk data for ALL vendors in one query ----
        cursor.execute("""
            SELECT vendor_id, date, milk_type, SUM(quantity) AS qty
            FROM milk_collection
            WHERE user_id=%s AND date BETWEEN %s AND %s
            GROUP BY vendor_id, date, milk_type
        """, (user_id, start_date, end_date))

        milk_map = {}
        for r in cursor.fetchall():
            vid = str(r['vendor_id'])
            milk_map.setdefault(vid, []).append(r)

        # ---- bulk advance totals for ALL vendors in one query ----
        cursor.execute("""
            SELECT vendor_id, SUM(amount) AS total_advance
            FROM advance
            WHERE user_id=%s AND date BETWEEN %s AND %s
            GROUP BY vendor_id
        """, (user_id, start_date, end_date))

        adv_map = {str(r['vendor_id']): float(r['total_advance'] or 0) for r in cursor.fetchall()}

        # ---- bulk food sack totals for ALL vendors in one query ----
        cursor.execute("""
            SELECT vendor_id, SUM(total_cost) AS total_food
            FROM food_sack
            WHERE user_id=%s AND date BETWEEN %s AND %s
            GROUP BY vendor_id
        """, (user_id, start_date, end_date))

        food_map = {str(r['vendor_id']): float(r['total_food'] or 0) for r in cursor.fetchall()}

        for v in vendors:

            vendor_id = v['vendor_id']
            milk_entries = milk_map.get(str(vendor_id), [])

            total_cow = 0
            total_buffalo = 0

            cow_cost = 0
            buffalo_cost = 0

            for m in milk_entries:

                rate = get_vendor_rate(
                    cursor,
                    vendor_id,
                    m['milk_type'],
                    m['date'],
                    user_id=user_id
                )

                qty = float(m['qty'])

                if m['milk_type'] == "cow":

                    total_cow += qty
                    cow_cost += qty * rate

                else:

                    total_buffalo += qty
                    buffalo_cost += qty * rate

            adv = adv_map.get(str(vendor_id), 0)
            food = food_map.get(str(vendor_id), 0)

            total_milk_payment = round(cow_cost + buffalo_cost, 2)

            total_payment = round(
                total_milk_payment - adv - food,
                2
            )

            data.append({

                'vendor_id': vendor_id,
                'vendor_name': v['name'],

                'total_cow': total_cow,
                'total_buffalo': total_buffalo,

                'cow_rate': "-",
                'buffalo_rate': "-",

                'total_milk_payment': total_milk_payment,

                'total_advance': adv,
                'total_food': food,

                'total_payment': total_payment
            })

    cursor.close()

    return render_template(
        'milk_operations/payment.html',
        data=data
    )
    
    
@app.route('/receipt_all_vendors', methods=['GET', 'POST'])
def receipt_all_vendors():


    if 'id' not in session:
        return redirect(url_for('login'))

    user_id = int(session['id'])
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    if request.method == 'POST':

        from_date = request.form.get('from_date')
        to_date = request.form.get('to_date')

        # -------------------------
        # LOAD VENDORS
        # -------------------------
        cursor.execute("""
            SELECT vendor_id, name, milk_type, address
            FROM vendors
            WHERE user_id=%s
            ORDER BY vendor_id
        """, (user_id,))
        vendors = cursor.fetchall()

        # -------------------------
        # LOAD MILK DATA
        # -------------------------
        cursor.execute("""
            SELECT vendor_id, date, slot, milk_type, quantity
            FROM milk_collection
            WHERE user_id=%s
            AND date BETWEEN %s AND %s
        """, (user_id, from_date, to_date))

        milk_rows = cursor.fetchall()

        milk_map = {}
        for r in milk_rows:
            vid = int(r['vendor_id'])
            milk_map.setdefault(vid, []).append(r)

        # -------------------------
        # LOAD FOOD SACK
        # -------------------------
        cursor.execute("""
            SELECT
                fs.vendor_id,
                fs.sack_qty,
                r.name,
                COALESCE(r.rate,0) AS rate,
                COALESCE(fs.total_cost,0) AS total
            FROM food_sack fs
            JOIN food_sack_rates r
            ON r.id = fs.sack_rate_id
            WHERE fs.user_id=%s
            AND fs.date BETWEEN %s AND %s
            ORDER BY fs.vendor_id
        """,(user_id,from_date,to_date))

        food_rows = cursor.fetchall()

        food_map = {}
        for f in food_rows:
            vid = int(f['vendor_id'])
            food_map.setdefault(vid, []).append(f)

        # -------------------------
        # LOAD ADVANCE
        # -------------------------
        cursor.execute("""
            SELECT vendor_id, SUM(amount) total
            FROM advance
            WHERE user_id=%s
            AND date BETWEEN %s AND %s
            GROUP BY vendor_id
        """, (user_id, from_date, to_date))

        adv_map = {
            int(a['vendor_id']): float(a['total'] or 0)
            for a in cursor.fetchall()
        }

        # -------------------------
        # PROCESS VENDORS
        # NOTE: get_vendor_rate() below now resolves from the in-memory,
        # per-request rate cache (2 queries loaded once, shared across all
        # vendors) instead of running 2 fresh SQL queries per vendor.
        # -------------------------
        all_receipts = []

        for vendor in vendors:

            vid = int(vendor['vendor_id'])

            cow_rate = get_vendor_rate(cursor, vid, "cow", from_date, user_id=user_id)
            buffalo_rate = get_vendor_rate(cursor, vid, "buffalo", from_date, user_id=user_id)

            milk_data = milk_map.get(vid, [])

            grouped = {}

            totals = {
                'cow_morning': 0,
                'cow_evening': 0,
                'buffalo_morning': 0,
                'buffalo_evening': 0
            }

            cow_cost = 0
            buffalo_cost = 0

            for row in milk_data:

                dt = row['date'].strftime("%Y-%m-%d")
                slot = row['slot']
                mtype = row['milk_type']
                qty = float(row['quantity'])

                rate = cow_rate if mtype == "cow" else buffalo_rate

                if dt not in grouped:
                    grouped[dt] = {
                        'day': row['date'].strftime("%d"),
                        'cow_morning': 0,
                        'cow_evening': 0,
                        'buffalo_morning': 0,
                        'buffalo_evening': 0
                    }

                grouped[dt][f"{mtype}_{slot}"] += qty
                totals[f"{mtype}_{slot}"] += qty

                if mtype == "cow":
                    cow_cost += qty * rate
                else:
                    buffalo_cost += qty * rate

            entries = list(grouped.values())

            food_data = food_map.get(vid, [])
            food_total = sum(float(f['total']) for f in food_data)

            food_sack_details = [
                {
                    "name": f['name'],
                    "rate": float(f['rate'] or 0),
                    "qty": int(f['sack_qty'] or 0),
                    "total": float(f['total'] or 0)
                }
                for f in food_data
            ]

            advance = float(adv_map.get(vid, 0))

            final_payable = round(
                (cow_cost + buffalo_cost) - (advance + food_total),
                2
            )

            all_receipts.append({

                'vendor_id': vid,
                'name': vendor['name'],
                'address': vendor['address'],
                'milk_type': vendor['milk_type'],

                'data': entries,

                'total_cow': totals['cow_morning'] + totals['cow_evening'],
                'total_buffalo': totals['buffalo_morning'] + totals['buffalo_evening'],

                'cow_cost': round(cow_cost, 2),
                'buffalo_cost': round(buffalo_cost, 2),

                'food_sack_details': food_sack_details,
                'food_cost': food_total,

                'advance': advance,
                'final_payable': final_payable,

                'total_cow_morning': totals['cow_morning'],
                'total_cow_evening': totals['cow_evening'],

                'total_buffalo_morning': totals['buffalo_morning'],
                'total_buffalo_evening': totals['buffalo_evening'],

                'cow_rate': cow_rate,
                'buffalo_rate': buffalo_rate
            })

        cursor.close()

        return render_template(
    'receipt_all_vendors.html',
    receipts=all_receipts,
    from_date=from_date,
    to_date=to_date
)
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

    if from_date > to_date:
        return jsonify({"error": "Invalid date range."}), 400

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
    # 2. Milk quantities - aggregated in SQL itself
    # -------------------------------------------------
    cursor.execute("""
        SELECT vendor_id, milk_type, SUM(quantity) AS qty
        FROM milk_collection
        WHERE user_id=%s
        AND date BETWEEN %s AND %s
        GROUP BY vendor_id, milk_type
    """, (user_id, from_date, to_date))

    milk_map = {}
    for r in cursor.fetchall():
        vid = int(r["vendor_id"])
        milk_map.setdefault(vid, {})[r["milk_type"]] = float(r["qty"] or 0)

    # -------------------------------------------------
    # 3. Food sack totals - aggregated in SQL itself
    # -------------------------------------------------
    cursor.execute("""
        SELECT fs.vendor_id, SUM(COALESCE(fs.total_cost,0)) AS total
        FROM food_sack fs
        WHERE fs.user_id=%s
        AND fs.date BETWEEN %s AND %s
        GROUP BY fs.vendor_id
    """, (user_id, from_date, to_date))

    food_map = {
        int(f["vendor_id"]): float(f["total"] or 0)
        for f in cursor.fetchall()
    }

    # -------------------------------------------------
    # 4. Advances
    # -------------------------------------------------
    cursor.execute("""
        SELECT vendor_id,SUM(amount) total
        FROM advance
        WHERE user_id=%s
        AND date BETWEEN %s AND %s
        GROUP BY vendor_id
    """, (user_id, from_date, to_date))

    adv_map = {
        int(a["vendor_id"]): float(a["total"] or 0)
        for a in cursor.fetchall()
    }

    # -------------------------------------------------
    # 5. Rates - fetched ONCE for all vendors (no N+1 queries)
    # -------------------------------------------------
    if isinstance(from_date, str):
        rate_date = datetime.strptime(from_date, "%Y-%m-%d").date()
    else:
        rate_date = from_date

    cursor.execute("""
        SELECT vendor_id, cow_rate, buffalo_rate, date_from
        FROM vendor_milk_rates
        WHERE user_id=%s
        AND date_from<=%s
        ORDER BY vendor_id, date_from DESC
    """, (user_id, rate_date))

    special_rate_map = {}
    for r in cursor.fetchall():
        vid = int(r["vendor_id"])
        if vid not in special_rate_map:
            special_rate_map[vid] = r

    cursor.execute("""
        SELECT animal, rate, date_from
        FROM milk_rates
        WHERE user_id=%s
        AND date_from<=%s
        ORDER BY animal, date_from DESC
    """, (user_id, rate_date))

    default_rate_map = {}
    for r in cursor.fetchall():
        if r["animal"] not in default_rate_map:
            default_rate_map[r["animal"]] = float(r["rate"])

    def resolve_rate(vid, animal):
        special = special_rate_map.get(vid)
        if special:
            val = special.get(f"{animal}_rate")
            if val:
                return float(val)
        return default_rate_map.get(animal, 0)

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

        cow_rate = resolve_rate(vid, "cow")
        buffalo_rate = resolve_rate(vid, "buffalo")

        qtys = milk_map.get(vid, {})
        cow_qty = qtys.get("cow", 0)
        buffalo_qty = qtys.get("buffalo", 0)

        cow_cost = cow_qty * cow_rate
        buffalo_cost = buffalo_qty * buffalo_rate

        food_total = food_map.get(vid, 0)
        advance = adv_map.get(vid, 0)

        final_payable = int(round(
            (cow_cost + buffalo_cost) - (advance + food_total)
        ))

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

    cursor.execute(
        "SELECT id, name FROM vendors WHERE user_id=%s",
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
    return render_template("settings.html")


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
    if "id" not in session:
        flash("Unauthorized request", "danger")
        return redirect(url_for("login"))

    cursor = SafeCursor(mysql.connection.cursor())
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


@app.route("/backup_my_data")
def backup_my_data():

    if "id" not in session:
        return {"error": "Unauthorized"}, 401

    filename = create_backup()

    return jsonify({
        "status": "success",
        "file": filename
    })


@app.route("/restore_backup/<filename>", methods=["POST"])
def restore_backup_route(filename):

    if "id" not in session:
        return {"error": "Unauthorized"}, 401

    try:

        restore_backup(filename)

        return {
            "status": "success",
            "message": "Backup restored successfully"
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }, 500


@app.route("/admin_full_backup")
def admin_full_backup():

    filename = create_full_backup()

    return jsonify({
        "status": "success",
        "file": filename
    })


@app.route("/backup_history")
def backup_history():

    files = list_backups()

    return jsonify({
        "files": files
    })


@app.route("/backup_page")
def backup_page():
    return render_template("backup.html")



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

        dairy_id = request.form.get('dairy_id')
        phone = request.form.get('phone')

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
        print("LOGIN SUCCESS SESSION:", dict(session))
        print("REDIRECT URL:", url_for("customer_dashboard"))


    return render_template("customer/login.html")

@app.route("/customer/dashboard")
def customer_dashboard():
    print("ENTER CUSTOMER DASHBOARD")
    print("SESSION IN DASHBOARD:", dict(session))

    if session.get("role") != "customer":
        flash("Please login first.", "warning")
        return redirect(url_for("customer_login"))

    cursor = SafeCursor(mysql.connection.cursor())

    today = date.today()

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

        FROM milk_collection

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

    FROM milk_collection

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
    print("SESSION IN RECEIPT =", dict(session))

    # ----------------------------
    # Login Check
    # ----------------------------
    if session.get("role") == "customer":
        user_id = session["owner_id"]
        vendor_id = session["vendor_id"]
    elif "id" in session:
        user_id = int(session["id"])
    else:
        flash("Please login first.", "warning")
        return redirect(url_for("login"))

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
        return redirect(url_for('vendor_list'))

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
                    COALESCE(SUM(CASE WHEN milk_type='cow' AND slot='morning' THEN quantity END),0) AS cow_morning,
                    COALESCE(SUM(CASE WHEN milk_type='cow' AND slot='evening' THEN quantity END),0) AS cow_evening,
                    COALESCE(SUM(CASE WHEN milk_type='buffalo' AND slot='morning' THEN quantity END),0) AS buffalo_morning,
                    COALESCE(SUM(CASE WHEN milk_type='buffalo' AND slot='evening' THEN quantity END),0) AS buffalo_evening
                FROM milk_collection
                WHERE vendor_id = %s
                  AND user_id = %s
                  AND date BETWEEN %s AND %s
                GROUP BY date
                ORDER BY date
            """, (vendor_id, user_id, from_date, to_date))

            daily_rows = cursor.fetchall()

            if not daily_rows:
                flash("No milk collection found for selected period.", "warning")

            total_cow_morning = 0
            total_cow_evening = 0
            total_buffalo_morning = 0
            total_buffalo_evening = 0

            for row in daily_rows:
                row['cow_morning'] = round(float(row['cow_morning']), 1)
                row['cow_evening'] = round(float(row['cow_evening']), 1)
                row['buffalo_morning'] = round(float(row['buffalo_morning']), 1)
                row['buffalo_evening'] = round(float(row['buffalo_evening']), 1)

                # Display-friendly date (dd-mm-yyyy)
                row['display_date'] = row['date'].strftime("%d-%m-%Y")

                total_cow_morning += row['cow_morning']
                total_cow_evening += row['cow_evening']
                total_buffalo_morning += row['buffalo_morning']
                total_buffalo_evening += row['buffalo_evening']

            total_cow_morning = round(total_cow_morning, 1)
            total_cow_evening = round(total_cow_evening, 1)
            total_buffalo_morning = round(total_buffalo_morning, 1)
            total_buffalo_evening = round(total_buffalo_evening, 1)

            total_cow_milk = round(total_cow_morning + total_cow_evening, 1)
            total_buffalo_milk = round(total_buffalo_morning + total_buffalo_evening, 1)

            # ----------------------------
            # Rates: vendor-specific first, else default
            # ----------------------------
            cursor.execute("""
                SELECT cow_rate, buffalo_rate
                FROM vendor_milk_rates
                WHERE vendor_id = %s
                  AND user_id = %s
                  AND date_from <= %s
                ORDER BY date_from DESC
                LIMIT 1
            """, (vendor_id, user_id, to_date))

            vendor_rate = cursor.fetchone()

            if vendor_rate:
                cow_rate = float(vendor_rate.get('cow_rate') or 0)
                buffalo_rate = float(vendor_rate.get('buffalo_rate') or 0)
            else:
                cursor.execute("""
                    SELECT animal, rate
                    FROM milk_rates
                    WHERE user_id = %s
                      AND date_from <= %s
                    ORDER BY date_from DESC
                """, (user_id, to_date))

                rate_rows = cursor.fetchall()

                cow_rate = 0
                buffalo_rate = 0
                got_cow = False
                got_buffalo = False

                for r in rate_rows:
                    if r['animal'] == 'cow' and not got_cow:
                        cow_rate = float(r['rate'] or 0)
                        got_cow = True
                    elif r['animal'] == 'buffalo' and not got_buffalo:
                        buffalo_rate = float(r['rate'] or 0)
                        got_buffalo = True
                    if got_cow and got_buffalo:
                        break

            cow_amount = round(total_cow_milk * cow_rate, 2)
            buffalo_amount = round(total_buffalo_milk * buffalo_rate, 2)
            total_milk_amount = round(cow_amount + buffalo_amount, 2)

            # ----------------------------
            # Advance
            # ----------------------------
            cursor.execute("""
                SELECT COALESCE(SUM(amount),0) AS total_advance
                FROM advance
                WHERE vendor_id = %s
                  AND user_id = %s
                  AND date BETWEEN %s AND %s
            """, (vendor_id, user_id, from_date, to_date))

            total_advance = round(float(cursor.fetchone()['total_advance']), 2)

            # ----------------------------
            # Food Sack (JOIN with food_sack_rates)
            # ----------------------------
            cursor.execute("""
                SELECT
                    fs.date,
                    r.name,
                    fs.sack_qty,
                    fs.sack_rate,
                    fs.total_cost
                FROM food_sack fs
                JOIN food_sack_rates r ON r.id = fs.sack_rate_id
                WHERE fs.vendor_id = %s
                  AND fs.user_id = %s
                  AND fs.date BETWEEN %s AND %s
                ORDER BY fs.date
            """, (vendor_id, user_id, from_date, to_date))

            food_sack_rows = cursor.fetchall()

            total_food_sack = 0
            for fs in food_sack_rows:
                fs['sack_qty'] = round(float(fs['sack_qty']), 1)
                fs['sack_rate'] = round(float(fs['sack_rate']), 2)
                fs['total_cost'] = round(float(fs['total_cost']), 2)
                fs['display_date'] = fs['date'].strftime("%d-%m-%Y")
                total_food_sack += fs['total_cost']

            total_food_sack = round(total_food_sack, 2)

            # ----------------------------
            # Final Payable
            # ----------------------------
            net_payable = round(total_milk_amount - total_advance - total_food_sack, 2)

            receipt = {
                'daily_rows': daily_rows,
                'total_cow_morning': total_cow_morning,
                'total_cow_evening': total_cow_evening,
                'total_buffalo_morning': total_buffalo_morning,
                'total_buffalo_evening': total_buffalo_evening,
                'total_cow_milk': total_cow_milk,
                'total_buffalo_milk': total_buffalo_milk,
                'cow_rate': cow_rate,
                'buffalo_rate': buffalo_rate,
                'cow_amount': cow_amount,
                'buffalo_amount': buffalo_amount,
                'total_milk_amount': total_milk_amount,
                'food_sack_rows': food_sack_rows,
                'total_food_sack': total_food_sack,
                'total_advance': total_advance,
                'net_payable': net_payable,
                'from_date': from_date,
                'to_date': to_date,
                'print_date': date.today().strftime('%d-%m-%Y')
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

    print("SESSION =", dict(session))
    print("CUSTOMER RECEIPTS ROUTE CALLED")

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

    if not endpoint or not p256dh or not auth:
        return jsonify({"message": "Invalid subscription data"}), 400

    user_id = session["owner_id"]
    vendor_id = session["vendor_id"]

    cursor = SafeCursor(mysql.connection.cursor())

    # Current Database
    cursor.execute("SELECT DATABASE()")
    print("SUBSCRIBE DB:", cursor.fetchone())

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

    print("COMMIT SUCCESS")

    cursor.execute("""
        SELECT
            id,
            user_id,
            vendor_id
        FROM push_subscriptions
    """)

    print("ALL SUBSCRIPTIONS:")
    print(cursor.fetchall())

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
    return "OK"


import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)