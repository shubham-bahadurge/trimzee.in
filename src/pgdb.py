"""Optional PostgreSQL backend for SalonGo.

When the DATABASE_URL (or POSTGRES_URL) setting exists, SalonGo stores everything in
that Postgres database instead of the temporary SQLite file. db.py and extras.py keep
working unchanged: this module gives them a connection that speaks the same small
dialect (`?` placeholders, `.lastrowid`, rows readable by name or position).
"""
import contextlib
import os
import sqlite3
import threading
import time

try:  # only needed (and installed) when Postgres is used
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
ENABLED = bool(DATABASE_URL)

INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + ((psycopg.IntegrityError,) if psycopg else ())
LOCK_ID = 727001  # any fixed number: makes two starting servers set up the database one at a time


class Row(dict):
    """A row you can read by column name (r["name"]) or position (r[0])."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = [c.name for c in (cursor.description or [])]

    def make_row(values):
        return Row(zip(names, values))
    return make_row


class Cursor:
    def __init__(self, raw_cursor, lastrowid=None):
        self._cur = raw_cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)


class Connection:
    def __init__(self, raw, shared=False):
        self._raw = raw
        self._shared = shared

    def execute(self, sql, params=None):
        text = sql.strip()
        if params:
            # Postgres style: %s placeholders, and a literal % must be written %%.
            text = text.replace("%", "%%").replace("?", "%s")
        is_insert = text[:6].upper() == "INSERT" and "RETURNING" not in text.upper()
        if is_insert:
            text += " RETURNING id"  # lets us offer .lastrowid like SQLite does
        cur = self._raw.execute(text, tuple(params) if params else None)
        if is_insert:
            return Cursor(cur, cur.fetchone()["id"])
        return Cursor(cur)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        if not self._shared:   # the shared connection stays open for the next query (see release())
            self._raw.close()


def _connect():
    # prepare_threshold=None: safe with Neon's connection pooler.
    return psycopg.connect(DATABASE_URL, row_factory=_row_factory, connect_timeout=10,
                           prepare_threshold=None)


# One connection per worker thread, reused by every query (a new Postgres connection costs 100+ ms,
# and a page can run a dozen queries). release() rolls back anything unfinished at the end of a request.
_local = threading.local()
IDLE_PING_SECONDS = 30


def _shared():
    raw = getattr(_local, "raw", None)
    if raw is not None:
        dead = getattr(raw, "closed", False) or getattr(raw, "broken", False)
        if not dead and time.monotonic() - _local.last > IDLE_PING_SECONDS:
            try:                      # the server may have dropped an idle connection: check before use
                raw.execute("SELECT 1")
                raw.rollback()
            except Exception:
                dead = True
        if dead:
            try:
                raw.close()
            except Exception:
                pass
            raw = None
    if raw is None:
        raw = _connect()
        _local.raw = raw
    _local.last = time.monotonic()
    return raw


def get_db():
    return Connection(_shared(), shared=True)


def release():
    """Call at the end of every request: undo anything left open so the next request starts clean."""
    raw = getattr(_local, "raw", None)
    if raw is not None:
        try:
            raw.rollback()
        except Exception:
            _local.raw = None


@contextlib.contextmanager
def init_lock():
    """Hold a database-wide lock while the app sets up tables (no-op for SQLite)."""
    if not ENABLED:
        yield
        return
    raw = _connect()
    try:
        raw.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        yield
    finally:
        raw.close()  # closing the connection releases the lock


PG_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'customer' CHECK (role IN ('customer', 'owner', 'admin')),
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS salons (
        id SERIAL PRIMARY KEY,
        owner_id INTEGER NOT NULL REFERENCES users(id),
        name TEXT NOT NULL,
        address TEXT,
        distance_km DOUBLE PRECISION NOT NULL DEFAULT 0,
        rating DOUBLE PRECISION NOT NULL DEFAULT 0,
        emoji TEXT NOT NULL DEFAULT '💈',
        image_data BYTEA,
        image_mime TEXT,
        status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'busy', 'closed', 'full')),
        next_slot TEXT,
        approved INTEGER NOT NULL DEFAULT 0,
        lat DOUBLE PRECISION,
        lng DOUBLE PRECISION,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS services (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        price INTEGER NOT NULL CHECK (price >= 0),
        duration_min INTEGER NOT NULL DEFAULT 30 CHECK (duration_min > 0))""",
    """CREATE TABLE IF NOT EXISTS bookings (
        id SERIAL PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES users(id),
        salon_id INTEGER NOT NULL REFERENCES salons(id),
        service_id INTEGER NOT NULL REFERENCES services(id),
        slot_start TEXT NOT NULL,
        contact_phone TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'confirmed', 'completed', 'cancelled', 'rejected')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_slot
        ON bookings (salon_id, slot_start) WHERE status IN ('pending', 'confirmed')""",
    """CREATE TABLE IF NOT EXISTS reviews (
        id SERIAL PRIMARY KEY,
        booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
        customer_id INTEGER NOT NULL REFERENCES users(id),
        salon_id INTEGER NOT NULL REFERENCES salons(id),
        rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
        comment TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS login_attempts (
        id SERIAL PRIMARY KEY,
        ts DOUBLE PRECISION NOT NULL,
        ip TEXT,
        email TEXT)""",
]

_db = None


def pg_init_db():
    conn = get_db()
    try:
        for statement in PG_STATEMENTS:
            conn.execute(statement)
        if conn.execute("SELECT COUNT(*) FROM salons").fetchone()[0] == 0:
            _db.seed_sample_data(conn)
        conn.commit()
    finally:
        conn.close()


class _SqliteLikeErrors:
    """Stands in for the sqlite3 module inside db.py so its
    `except sqlite3.IntegrityError` also catches Postgres errors."""
    IntegrityError = INTEGRITY_ERRORS


def install(db_module):
    """Point db.py at Postgres (does nothing when no DATABASE_URL is set)."""
    global _db
    if not ENABLED:
        return
    _db = db_module
    db_module.get_db = get_db
    db_module.init_db = pg_init_db
    db_module.sqlite3 = _SqliteLikeErrors
