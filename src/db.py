"""SalonGo database layer (SQLite).

Tables, sample data, salon queries, slots, booking, accounts and owner tools.
Uses only Python's built-in sqlite3, so there is nothing extra to install.
"""
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if os.environ.get("VERCEL"):
    # Vercel's disk is temporary: only /tmp is writable, and it resets often.
    DB_PATH = "/tmp/salongo.db"
else:
    os.makedirs(os.path.join(BASE_DIR, "database"), exist_ok=True)
    DB_PATH = os.path.join(BASE_DIR, "database", "salongo.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'customer'
                  CHECK (role IN ('customer', 'owner', 'admin')),
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS salons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    address     TEXT,
    distance_km REAL NOT NULL DEFAULT 0,
    rating      REAL NOT NULL DEFAULT 0,
    emoji       TEXT NOT NULL DEFAULT '💈',
    image_data  BLOB,
    image_mime  TEXT,
    status      TEXT NOT NULL DEFAULT 'available'
                CHECK (status IN ('available', 'busy', 'closed', 'full')),
    next_slot   TEXT,
    approved    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS services (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    salon_id     INTEGER NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL,
    price        INTEGER NOT NULL CHECK (price >= 0),
    duration_min INTEGER NOT NULL DEFAULT 30 CHECK (duration_min > 0)
);

CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES users(id),
    salon_id    INTEGER NOT NULL REFERENCES salons(id),
    service_id  INTEGER NOT NULL REFERENCES services(id),
    slot_start  TEXT NOT NULL,
    contact_phone TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'confirmed', 'completed',
                                  'cancelled', 'rejected')),
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The database itself refuses two active bookings
-- for the same salon and the same time slot.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_slot
    ON bookings (salon_id, slot_start)
    WHERE status IN ('pending', 'confirmed');

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id  INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
    customer_id INTEGER NOT NULL REFERENCES users(id),
    salon_id    INTEGER NOT NULL REFERENCES salons(id),
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment     TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# (name, distance_km, rating, emoji, status, next_slot, [(service, category, price, minutes)])
SAMPLE_SALONS = [
    ("Style Studio", 1.2, 4.7, "💈", "available", "7:30 PM",
     [("Haircut", "haircut", 150, 30), ("Beard Trim", "beard", 150, 20),
      ("Hair Styling", "hair styling", 250, 45)]),
    ("Royal Cuts", 1.5, 4.5, "👑", "busy", "8:00 PM",
     [("Haircut", "haircut", 200, 30), ("Beard Trim", "beard", 200, 20)]),
    ("Glow Beauty Lounge", 2.1, 4.8, "🌸", "available", "7:45 PM",
     [("Facial", "facial", 400, 45), ("Hair Coloring", "coloring", 1200, 90),
      ("Head Massage", "massage", 600, 30)]),
    ("The Trim Box", 0.8, 4.2, "📦", "full", "Tomorrow 10:00 AM",
     [("Haircut", "haircut", 100, 25)]),
    ("Zen Spa & Salon", 3.4, 4.6, "🧘", "closed", "Opens 10:00 AM",
     [("Full Body Massage", "massage", 500, 60), ("Facial", "facial", 700, 45)]),
    ("Color Theory", 2.8, 4.4, "🌈", "busy", "8:30 PM",
     [("Hair Coloring", "coloring", 800, 90), ("Hair Styling", "hair styling", 900, 60)]),
]


def get_db():
    """Open a connection. Rows can be read like dictionaries."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create the tables, and add sample salons the first time only."""
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(bookings)")]
        if "contact_phone" not in cols:
            conn.execute("ALTER TABLE bookings ADD COLUMN contact_phone TEXT")
        if conn.execute("SELECT COUNT(*) FROM salons").fetchone()[0] == 0:
            seed_sample_data(conn)
        conn.commit()
    finally:
        conn.close()


def seed_sample_data(conn):
    # A placeholder owner nobody can log in as (random password, thrown away).
    cur = conn.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'owner')",
        ("Sample Owner", "owner@salongo.local",
         generate_password_hash(secrets.token_hex(16))),
    )
    owner_id = cur.lastrowid

    for name, km, rating, emoji, status, next_slot, services in SAMPLE_SALONS:
        cur = conn.execute(
            "INSERT INTO salons (owner_id, name, distance_km, rating, emoji, "
            "status, next_slot, approved) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (owner_id, name, km, rating, emoji, status, next_slot),
        )
        salon_id = cur.lastrowid
        for svc_name, category, price, minutes in services:
            conn.execute(
                "INSERT INTO services (salon_id, name, category, price, duration_min) "
                "VALUES (?, ?, ?, ?, ?)",
                (salon_id, svc_name, category, price, minutes),
            )


def get_salons():
    """Approved salons, nearest first, in the shape the homepage expects."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT s.id, s.name, s.distance_km, s.rating, s.emoji, s.status, "
            "s.next_slot, MIN(sv.price) AS price "
            "FROM salons s JOIN services sv ON sv.salon_id = s.id "
            "WHERE s.approved = 1 GROUP BY s.id ORDER BY s.distance_km"
        ).fetchall()

        salons = []
        for r in rows:
            names = [
                x["name"] for x in conn.execute(
                    "SELECT name FROM services WHERE salon_id = ? ORDER BY id",
                    (r["id"],),
                )
            ]
            salons.append({
                "id": r["id"],
                "name": r["name"],
                "rating": r["rating"],
                "distance": r["distance_km"],
                "price": r["price"],
                "status": r["status"],
                "next": r["next_slot"],
                "emoji": r["emoji"],
                "services": names,
            })
        return salons
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Salon page, time slots, booking
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))  # prices are in rupees, so India time
OPEN_HOUR, CLOSE_HOUR = 10, 20                  # slots every 30 min, 10:00 - 19:30


def now_ist():
    return datetime.now(IST)


def get_salon(salon_id):
    conn = get_db()
    try:
        s = conn.execute("SELECT id, owner_id, name, address, distance_km, rating, emoji, image_mime, "
                         "status, next_slot, approved, created_at, lat, lng, gender "
                         "FROM salons WHERE id = ? AND approved = 1",
                         (salon_id,)).fetchone()
        if not s:
            return None
        rows = conn.execute(
            "SELECT id, name, price, duration_min FROM services "
            "WHERE salon_id = ? ORDER BY id", (salon_id,)).fetchall()
        return {"salon": dict(s), "services": [dict(r) for r in rows]}
    finally:
        conn.close()


def _todays_slots():
    day = now_ist().strftime("%Y-%m-%d")
    return [f"{day} {h:02d}:{m:02d}"
            for h in range(OPEN_HOUR, CLOSE_HOUR) for m in (0, 30)]


def get_slots(salon_id):
    """Today's slots with a state: free / booked / past / closed."""
    conn = get_db()
    try:
        salon = conn.execute("SELECT status FROM salons WHERE id = ? AND approved = 1",
                             (salon_id,)).fetchone()
        if not salon:
            return None
        times = _todays_slots()
        taken = {r["slot_start"] for r in conn.execute(
            "SELECT slot_start FROM bookings WHERE salon_id = ? "
            "AND status IN ('pending', 'confirmed') AND slot_start LIKE ?",
            (salon_id, times[0][:10] + "%"))}
        now = now_ist().strftime("%Y-%m-%d %H:%M")
        is_open = salon["status"] in ("available", "busy")
        slots = []
        for t in times:
            if not is_open:
                state = "closed"
            elif t in taken:
                state = "booked"
            elif t <= now:
                state = "past"
            else:
                state = "free"
            slots.append({"slot": t, "time": t[11:], "state": state})
        return {"status": salon["status"], "slots": slots}
    finally:
        conn.close()


def create_booking(salon_id, service_id, slot, name, phone, customer_id=None):
    """Returns (http_status, message, booking_id)."""
    phone = (phone or "").strip()
    conn = get_db()
    try:
        customer = None
        if customer_id is not None:
            customer = conn.execute(
                "SELECT id FROM users WHERE id = ? AND role = 'customer' AND is_active = 1",
                (customer_id,)).fetchone()
        name = (name or "").strip()
        if customer is None and not 2 <= len(name) <= 60:
            return 400, "Please enter your name (2-60 characters).", None
        if not (phone.isdigit() and 10 <= len(phone) <= 12):
            return 400, "Please enter a valid phone number (10-12 digits).", None

        salon = conn.execute("SELECT status FROM salons WHERE id = ? AND approved = 1",
                             (salon_id,)).fetchone()
        if not salon:
            return 404, "Salon not found.", None
        if salon["status"] not in ("available", "busy"):
            return 409, "This salon is not taking bookings right now.", None
        svc = conn.execute("SELECT id FROM services WHERE id = ? AND salon_id = ?",
                           (service_id, salon_id)).fetchone()
        if not svc:
            return 400, "Please choose a service.", None
        if slot not in _todays_slots():
            return 400, "That time slot is not available.", None
        if slot <= now_ist().strftime("%Y-%m-%d %H:%M"):
            return 409, "That time has already passed. Pick a later slot.", None

        try:
            if customer is not None:
                uid = customer["id"]
            else:  # guest booking (no account)
                uid = conn.execute(
                    "INSERT INTO users (name, email, password_hash, role) "
                    "VALUES (?, ?, ?, 'customer')",
                    (name, f"guest-{uuid.uuid4().hex}@salongo.local", "!")).lastrowid
            cur = conn.execute(
                "INSERT INTO bookings (customer_id, salon_id, service_id, slot_start, "
                "status, contact_phone) VALUES (?, ?, ?, ?, 'confirmed', ?)",
                (uid, salon_id, service_id, slot, phone))
            conn.commit()
            return 201, "Booking confirmed!", cur.lastrowid
        except sqlite3.IntegrityError:
            # The one-booking-per-slot rule fired: someone was faster.
            conn.rollback()
            return 409, "Sorry, someone just booked that slot. Please pick another.", None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Accounts, customer bookings, owner dashboard
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEFAULT_SERVICES = [("Haircut", "haircut", 150, 30), ("Beard Trim", "beard", 100, 20),
                    ("Facial", "facial", 400, 45)]
ACTIVE = ("pending", "confirmed")


def create_user(name, email, password, role, salon_name=""):
    """Returns (user_id, error_message)."""
    name = (name or "").strip()
    email = (email or "").strip().lower()
    salon_name = (salon_name or "").strip()
    password = password or ""
    if not 2 <= len(name) <= 60:
        return None, "Please enter your name (2-60 characters)."
    if len(email) > 120 or not EMAIL_RE.match(email):
        return None, "Please enter a valid email address."
    if not 8 <= len(password) <= 128:
        return None, "Password must be 8-128 characters."
    if role not in ("customer", "owner"):
        return None, "Please choose an account type."
    if role == "owner" and not 2 <= len(salon_name) <= 60:
        return None, "Please enter your salon's name (2-60 characters)."

    conn = get_db()
    try:
        try:
            uid = conn.execute(
                "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
                (name, email, generate_password_hash(password), role)).lastrowid
            if role == "owner":
                sid = conn.execute(
                    "INSERT INTO salons (owner_id, name, distance_km, rating, emoji, "
                    "status, next_slot, approved) VALUES (?, ?, 1.0, 0, '✂️', "
                    "'available', 'Today', 1)", (uid, salon_name)).lastrowid
                for svc, cat, price, minutes in DEFAULT_SERVICES:
                    conn.execute(
                        "INSERT INTO services (salon_id, name, category, price, duration_min) "
                        "VALUES (?, ?, ?, ?, ?)", (sid, svc, cat, price, minutes))
            conn.commit()
            return uid, None
        except sqlite3.IntegrityError:
            conn.rollback()
            return None, "That email is already registered. Try logging in."
    finally:
        conn.close()


def authenticate(email, password):
    """Returns the user dict, or None if email/password is wrong."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, role, password_hash FROM users "
            "WHERE email = ? AND is_active = 1", ((email or "").strip().lower(),)).fetchone()
        if not row or row["password_hash"] == "!":
            return None
        try:
            ok = check_password_hash(row["password_hash"], password or "")
        except ValueError:
            ok = False
        return {"id": row["id"], "name": row["name"], "role": row["role"]} if ok else None
    finally:
        conn.close()


def get_user(user_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT id, name, role FROM users WHERE id = ? AND is_active = 1",
                           (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_bookings(user_id):
    conn = get_db()
    try:
        now = now_ist().strftime("%Y-%m-%d %H:%M")
        rows = conn.execute(
            "SELECT b.id, b.slot_start, b.status, s.name AS salon_name, "
            "sv.name AS service_name, sv.price FROM bookings b "
            "JOIN salons s ON s.id = b.salon_id JOIN services sv ON sv.id = b.service_id "
            "WHERE b.customer_id = ? ORDER BY b.slot_start DESC", (user_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["can_cancel"] = d["status"] in ACTIVE and d["slot_start"] > now
            out.append(d)
        return out
    finally:
        conn.close()


def cancel_booking(user_id, booking_id):
    """Customers may cancel their own active booking until it starts."""
    conn = get_db()
    try:
        b = conn.execute("SELECT status, slot_start FROM bookings WHERE id = ? AND customer_id = ?",
                         (booking_id, user_id)).fetchone()
        if not b:
            return 404, "Booking not found."
        if b["status"] not in ACTIVE:
            return 409, "This booking can no longer be cancelled."
        if b["slot_start"] <= now_ist().strftime("%Y-%m-%d %H:%M"):
            return 409, "This booking has already started, so it can't be cancelled."
        conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
        conn.commit()
        return 200, "Booking cancelled. The slot is free again."
    finally:
        conn.close()


def get_owner_salon(owner_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT id, name, status FROM salons WHERE owner_id = ? "
                           "ORDER BY id LIMIT 1", (owner_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_salon_bookings_today(salon_id):
    conn = get_db()
    try:
        day = now_ist().strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT b.id, b.slot_start, b.status, b.contact_phone, u.name AS customer_name, "
            "sv.name AS service_name, sv.price FROM bookings b "
            "JOIN users u ON u.id = b.customer_id JOIN services sv ON sv.id = b.service_id "
            "WHERE b.salon_id = ? AND b.slot_start LIKE ? ORDER BY b.slot_start",
            (salon_id, day + "%")).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_salon_status(owner_id, status):
    if status not in ("available", "busy", "closed", "full"):
        return False
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE salons SET status = ? WHERE id = "
            "(SELECT id FROM salons WHERE owner_id = ? ORDER BY id LIMIT 1)", (status, owner_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def owner_update_booking(owner_id, booking_id, action):
    new = {"complete": "completed", "reject": "rejected"}.get(action)
    if not new:
        return False
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE bookings SET status = ? WHERE id = ? AND status IN ('pending', 'confirmed') "
            "AND salon_id IN (SELECT id FROM salons WHERE owner_id = ?)",
            (new, booking_id, owner_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
