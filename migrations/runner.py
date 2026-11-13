import importlib
import os
import pkgutil
import sys
from dataclasses import dataclass
from typing import Callable, Optional

import MySQLdb
import MySQLdb.cursors


@dataclass
class Step:
    description: str
    sql: str
    # Returns True when the step is ALREADY applied (so it is skipped).
    applied: Callable[["Schema"], bool]
    # Optional precondition; returns an error string to skip the step safely.
    precondition: Optional[Callable[["Schema"], Optional[str]]] = None


class Schema:
    """Small helper around information_schema for migration guards."""

    def __init__(self, conn):
        self.conn = conn

    def _one(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return row

    def table_exists(self, table):
        return bool(self._one(
            "SELECT 1 FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table,),
        ))

    def column_exists(self, table, column):
        return bool(self._one(
            "SELECT 1 FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (table, column),
        ))

    def column_type(self, table, column):
        row = self._one(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (table, column),
        )
        return row["COLUMN_TYPE"].lower() if row else None

    def index_exists(self, table, index):
        return bool(self._one(
            "SELECT 1 FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s",
            (table, index),
        ))

    def fk_exists(self, table, name):
        return bool(self._one(
            "SELECT 1 FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND CONSTRAINT_NAME = %s AND CONSTRAINT_TYPE = 'FOREIGN KEY'",
            (table, name),
        ))

    def count(self, sql, params=()):
        row = self._one(sql, params)
        return int(list(row.values())[0]) if row else 0


def connect_from_env():
    return MySQLdb.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", 3306)),
        user=os.getenv("MYSQL_USER"),
        passwd=os.getenv("MYSQL_PASSWORD", ""),
        db=os.getenv("MYSQL_DB"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=MySQLdb.cursors.DictCursor,
    )


def _ensure_ledger(conn):
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version VARCHAR(40) NOT NULL PRIMARY KEY,"
        " description VARCHAR(255) NOT NULL,"
        " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    cur.close()


def load_migrations():
    import migrations as pkg
    mods = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("m") and info.name[1:4].isdigit():
            mods.append(importlib.import_module(f"migrations.{info.name}"))
    return sorted(mods, key=lambda m: m.VERSION)


def applied_versions(conn):
    _ensure_ledger(conn)
    cur = conn.cursor()
    cur.execute("SELECT version FROM schema_migrations")
    rows = {r["version"] for r in cur.fetchall()}
    cur.close()
    return rows


def pending_versions(conn):
    done = applied_versions(conn)
    return [m.VERSION for m in load_migrations() if m.VERSION not in done]


def run(conn, dry_run=False, out=print):
    _ensure_ledger(conn)
    schema = Schema(conn)
    done = applied_versions(conn)
    warnings = []

    for mod in load_migrations():
        if mod.VERSION in done:
            out(f"[skip] {mod.VERSION} already applied")
            continue

        out(f"[migrate] {mod.VERSION}: {mod.DESCRIPTION}")
        for step in mod.STEPS:
            if step.applied(schema):
                out(f"   - already applied: {step.description}")
                continue
            if step.precondition:
                problem = step.precondition(schema)
                if problem:
                    msg = f"{mod.VERSION}: skipped '{step.description}': {problem}"
                    warnings.append(msg)
                    out(f"   ! SKIPPED: {step.description} -> {problem}")
                    continue
            if dry_run:
                out(f"   * would run: {step.description}")
                continue
            out(f"   + {step.description}")
            cur = conn.cursor()
            cur.execute(step.sql)
            cur.close()

        if not dry_run:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (%s, %s)",
                (mod.VERSION, mod.DESCRIPTION),
            )
            cur.close()

    for w in warnings:
        out(f"WARNING: {w}")
    return warnings


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    conn = connect_from_env()
    try:
        if "--status" in argv:
            done = applied_versions(conn)
            for m in load_migrations():
                state = "applied" if m.VERSION in done else "PENDING"
                print(f"{m.VERSION:<12} {state:<8} {m.DESCRIPTION}")
            return 0
        run(conn, dry_run="--dry-run" in argv)
        return 0
    finally:
        conn.close()
