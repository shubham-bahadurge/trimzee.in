"""Start-up: create / upgrade tables only when the stored schema version is out of date (fast cold starts)."""
import os

import accounts
import db
import extras
import growth
import meta
import slots
import billing

SCHEMA_VERSION = "2026-10-v8-billing"


def _wanted():
    return f"{SCHEMA_VERSION}|{os.environ.get('ADMIN_EMAIL', '')}|{bool(os.environ.get('ADMIN_PASSWORD'))}"


def _current():
    conn = db.get_db()
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key = 'schema'").fetchone()
        return bool(row) and row["value"] == _wanted()
    except Exception:
        conn.rollback()          # first run: the table doesn't exist yet
        return False
    finally:
        conn.close()


def _mark():
    conn = db.get_db()
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS app_meta (id {meta.ID}, key TEXT NOT NULL UNIQUE, value TEXT NOT NULL)")
        conn.execute("DELETE FROM app_meta WHERE key = 'schema'")
        conn.execute("INSERT INTO app_meta (key, value) VALUES ('schema', ?)", (_wanted(),))
        conn.commit()
    finally:
        conn.close()


def run(lock):
    """lock: a context manager that serialises set-up across servers (pgdb.init_lock)."""
    if _current():
        return
    with lock():
        if _current():           # another server finished while we waited
            return
        db.init_db()
        extras.init_extras()
        meta.init_meta()
        growth.init_growth()
        accounts.init_accounts()
        slots.init_slots()
        billing.init_billing()
        _mark()
