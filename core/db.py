"""
Database helpers.

The application connection runs with autocommit=True (existing behaviour
that many routes rely on). Multi-statement financial operations must
therefore open an explicit transaction, otherwise SELECT ... FOR UPDATE
locks are released immediately and "rollback" undoes nothing.
"""
from contextlib import contextmanager

import MySQLdb.cursors
from flask import current_app


def mysql():
    return current_app.extensions["dm_mysql"]


def connection():
    return mysql().connection


def cursor():
    """Plain DictCursor (no SafeCursor digit coercion: phone numbers and
    OTPs keep their leading zeros)."""
    return connection().cursor(MySQLdb.cursors.DictCursor)


@contextmanager
def transaction():
    """BEGIN ... COMMIT, or ROLLBACK on any exception.

    Usage:
        with transaction() as cur:
            cur.execute(...)
    """
    conn = connection()
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


def begin():
    """Start an explicit transaction on the request connection (for legacy
    routes that already call commit()/rollback() themselves)."""
    connection().begin()
