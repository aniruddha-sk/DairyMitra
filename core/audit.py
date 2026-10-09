"""
Database audit trail (dm_audit_log).

Financial and configuration changes are written with the caller's cursor so
the audit row commits or rolls back together with the change it describes.
Passwords, OTPs, tokens and secrets must never be passed in old/new values.
"""
import json
import logging
from datetime import date, datetime
from decimal import Decimal

from flask import g, has_request_context, request, session

from core import db

log = logging.getLogger("dairymitr.audit")


def _default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _dump(values):
    if values is None:
        return None
    return json.dumps(values, default=_default, ensure_ascii=False)


def actor():
    """(role, actor_id) of the current request."""
    if not has_request_context():
        return "system", None
    role = session.get("role") or "anonymous"
    if role == "staff":
        return role, session.get("staff_id")
    if role == "customer":
        return role, session.get("customer_id")
    return role, session.get("id")


def record(action, target_type=None, target_id=None, old=None, new=None,
           user_id=None, cur=None):
    """Insert one audit row. With `cur`, it joins the caller's transaction
    (and a failure aborts that transaction). Without `cur`, failures are
    logged and swallowed so auditing never breaks a read-only page."""
    role, actor_id = actor()
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
        _dump(old), _dump(new), request_id, ip,
    )
    log.info("audit action=%s tenant=%s actor=%s:%s target=%s:%s req=%s",
             action, user_id, role, actor_id, target_type, target_id, request_id)
    if cur is not None:
        cur.execute(sql, params)
        return
    try:
        c = db.cursor()
        c.execute(sql, params)
        c.close()
    except Exception:
        log.exception("audit write failed (action=%s)", action)
