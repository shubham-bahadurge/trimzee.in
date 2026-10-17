"""Trimzee features on top of db.py / extras.py: live salon status, revenue,
gender rules, the 5 km rule, notifications and admin people lists."""
import math
import time
from datetime import timedelta

import db
import extras
import pgdb

GENDERS = ("men", "women", "unisex")
USER_GENDERS = ("male", "female")
SAMPLE_GENDERS = {"Style Studio": "men", "Royal Cuts": "men", "Glow Beauty Lounge": "women",
                  "Color Theory": "women"}  # the rest stay "unisex"
MAX_KM = 5.0
ID = "SERIAL PRIMARY KEY" if pgdb.ENABLED else "INTEGER PRIMARY KEY AUTOINCREMENT"
REAL = "DOUBLE PRECISION" if pgdb.ENABLED else "REAL"


def _add_column(conn, table, col, ddl):
    if pgdb.ENABLED:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}")
    else:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_meta():
    conn = db.get_db()
    try:
        _add_column(conn, "salons", "gender", "TEXT NOT NULL DEFAULT 'unisex'")
        _add_column(conn, "users", "gender", "TEXT")
        conn.execute(f"CREATE TABLE IF NOT EXISTS notifications (id {ID}, "
                     "user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL, ref_id INTEGER, "
                     f"message TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0, created_at {REAL} NOT NULL)")
        for name, gender in SAMPLE_GENDERS.items():
            conn.execute("UPDATE salons SET gender = ? WHERE name = ? AND gender = 'unisex' AND owner_id = "
                         "(SELECT id FROM users WHERE email = 'owner@salongo.local')", (gender, name))
        # A salon is now simply ON ('available') or OFF ('closed'); busy/full are worked out live.
        conn.execute("UPDATE salons SET status = 'available' WHERE status IN ('busy', 'full')")
        conn.commit()
    finally:
        conn.close()


# ----------------------------- live status ---------------------------------
def _pretty(t):
    h = int(t[11:13])
    return f"{(h + 11) % 12 + 1}:{t[14:16]} {'PM' if h >= 12 else 'AM'}"


def _taken_today(conn):
    day = db.now_ist().strftime("%Y-%m-%d")
    out = {}
    for r in conn.execute("SELECT salon_id, slot_start FROM bookings WHERE status IN ('pending', 'confirmed') "
                          "AND slot_start LIKE ?", (day + "%",)):
        out.setdefault(r["salon_id"], set()).add(r["slot_start"])
    return out


def live_status(stored, taken):
    """Returns (status, next_label). stored is the owner's switch: 'available' = ON, 'closed' = OFF."""
    if stored == "closed":
        return "closed", "Switched off"
    hm = db.now_ist().strftime("%Y-%m-%d %H:%M")
    day = hm[:10]
    if hm >= f"{day} {db.CLOSE_HOUR:02d}:00":
        return "closed", "Opens tomorrow"
    times = db._todays_slots()
    free = [t for t in times if t > hm and t not in taken]
    current = [t for t in times if t <= hm]
    if not free:
        return "full", "Tomorrow"
    if current and current[-1] in taken:  # the slot running right now is booked -> busy until it ends
        return "busy", _pretty(free[0])
    return "available", _pretty(free[0])


def salon_live(salon_id):
    conn = db.get_db()
    try:
        row = conn.execute("SELECT status FROM salons WHERE id = ?", (salon_id,)).fetchone()
        if not row:
            return "closed", ""
        return live_status(row["status"], _taken_today(conn).get(salon_id, set()))
    finally:
        conn.close()


def get_salons(user_gender=None):
    """Live salons with real-time status, next free slot and gender; filtered for a logged-in user's gender."""
    salons = extras.get_salons()
    conn = db.get_db()
    try:
        rows = {r["id"]: r for r in conn.execute("SELECT id, gender, status FROM salons")}
        taken = _taken_today(conn)
    finally:
        conn.close()
    want = {"male": "men", "female": "women"}.get(user_gender)
    out = []
    for s in salons:
        r = rows.get(s["id"])
        s["gender"] = r["gender"] if r else "unisex"
        if want and s["gender"] not in (want, "unisex"):
            continue
        s["status"], s["next"] = live_status(r["status"] if r else "available", taken.get(s["id"], set()))
        out.append(s)
    return out


def get_salon(salon_id):
    data = extras.get_salon(salon_id)
    if data:
        data["salon"]["status"] = salon_live(salon_id)[0]
    return data


# ----------------------------- gender --------------------------------------
def user_gender(user_id):
    conn = db.get_db()
    try:
        row = conn.execute("SELECT gender FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["gender"] if row else None
    finally:
        conn.close()


def set_user_gender(user_id, gender):
    if gender not in USER_GENDERS:
        return False
    conn = db.get_db()
    try:
        cur = conn.execute("UPDATE users SET gender = ? WHERE id = ?", (gender, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def gender_allows(salon_id, gender):
    """Returns (ok, message): men see men's salons, women see women's, both see unisex."""
    want = {"male": "men", "female": "women"}.get(gender)
    if not want:
        return True, ""
    conn = db.get_db()
    try:
        row = conn.execute("SELECT gender FROM salons WHERE id = ?", (salon_id,)).fetchone()
    finally:
        conn.close()
    if row and row["gender"] not in (want, "unisex"):
        return False, f"This salon is for {row['gender']} only."
    return True, ""


def owner_gender(owner_id):
    conn = db.get_db()
    try:
        row = conn.execute("SELECT gender FROM salons WHERE owner_id = ? ORDER BY id LIMIT 1",
                           (owner_id,)).fetchone()
        return row["gender"] if row else "unisex"
    finally:
        conn.close()


def set_salon_gender(owner_id, gender):
    if gender not in GENDERS:
        return False
    conn = db.get_db()
    try:
        cur = conn.execute("UPDATE salons SET gender = ? WHERE id = "
                           "(SELECT id FROM salons WHERE owner_id = ? ORDER BY id LIMIT 1)", (gender, owner_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ----------------------------- 5 km rule -----------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def check_range(salon_id, lat, lng):
    """Returns (ok, message). Needs the visitor's live location and the salon's saved location."""
    try:
        lat, lng = float(lat), float(lng)
        assert -90 <= lat <= 90 and -180 <= lng <= 180
    except (TypeError, ValueError, AssertionError):
        return False, "Please turn on your live location. We use it to check the salon is within 5 km of you."
    conn = db.get_db()
    try:
        row = conn.execute("SELECT lat, lng FROM salons WHERE id = ?", (salon_id,)).fetchone()
    finally:
        conn.close()
    if not row or row["lat"] is None or row["lng"] is None:
        return False, "This salon hasn't shared its location yet, so we can't confirm it's near you."
    km = haversine_km(lat, lng, row["lat"], row["lng"])
    if km > MAX_KM:
        return False, f"This salon is {km:.1f} km away. You can book salons within {MAX_KM:.0f} km of your live location."
    return True, ""


# ----------------------------- owner revenue -------------------------------
def revenue(salon_id):
    now = db.now_ist()
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    conn = db.get_db()
    try:
        def total(prefix, statuses):
            marks = ",".join("?" * len(statuses))
            row = conn.execute("SELECT COALESCE(SUM(sv.price), 0) AS amount, COUNT(*) AS n FROM bookings b "
                               "JOIN services sv ON sv.id = b.service_id WHERE b.salon_id = ? AND b.slot_start LIKE ? "
                               f"AND b.status IN ({marks})", (salon_id, prefix + "%") + tuple(statuses)).fetchone()
            return int(row["amount"]), int(row["n"])
        today_earned, today_done = total(day, ["completed"])
        today_expected, today_upcoming = total(day, ["pending", "confirmed"])
        month_earned, month_done = total(month, ["completed"])
        return {"today_earned": today_earned, "today_done": today_done, "today_expected": today_expected,
                "today_upcoming": today_upcoming, "month_earned": month_earned, "month_done": month_done}
    finally:
        conn.close()


def toggle_salon(owner_id, on):
    return db.set_salon_status(owner_id, "available" if on else "closed")


# ----------------------------- notifications -------------------------------
def _notify(conn, user_id, kind, ref_id, message):
    conn.execute("INSERT INTO notifications (user_id, kind, ref_id, message, created_at) VALUES (?, ?, ?, ?, ?)",
                 (user_id, kind, ref_id, message, time.time()))


def notify_new_booking(booking_id):
    """Tell the salon owner about a new booking."""
    conn = db.get_db()
    try:
        r = conn.execute("SELECT s.owner_id, b.slot_start, u.name AS customer, sv.name AS service FROM bookings b "
                         "JOIN salons s ON s.id = b.salon_id JOIN users u ON u.id = b.customer_id "
                         "JOIN services sv ON sv.id = b.service_id WHERE b.id = ?", (booking_id,)).fetchone()
        if r:
            _notify(conn, r["owner_id"], "booking", booking_id,
                    f"New booking: {r['customer']} · {r['service']} at {_pretty(r['slot_start'])}")
            conn.commit()
    finally:
        conn.close()


def poll_notifications(user):
    """Create 5-minute reminders for a customer, then return their unread notifications."""
    conn = db.get_db()
    try:
        if user["role"] == "customer":
            now = db.now_ist()
            lo, hi = now.strftime("%Y-%m-%d %H:%M"), (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
            due = conn.execute("SELECT b.id, b.slot_start, s.name FROM bookings b JOIN salons s ON s.id = b.salon_id "
                               "WHERE b.customer_id = ? AND b.status = 'confirmed' AND b.slot_start > ? "
                               "AND b.slot_start <= ?", (user["id"], lo, hi)).fetchall()
            for b in due:
                seen = conn.execute("SELECT 1 FROM notifications WHERE user_id = ? AND kind = 'reminder' "
                                    "AND ref_id = ?", (user["id"], b["id"])).fetchone()
                if not seen:
                    _notify(conn, user["id"], "reminder", b["id"],
                            f"⏰ Your appointment at {b['name']} starts at {_pretty(b['slot_start'])} (in about 5 minutes).")
            conn.commit()
        rows = conn.execute("SELECT id, kind, message FROM notifications WHERE user_id = ? AND is_read = 0 "
                            "ORDER BY id LIMIT 20", (user["id"],)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_read(user_id, ids):
    clean = [int(i) for i in ids if str(i).isdigit()][:50]
    if not clean:
        return
    conn = db.get_db()
    try:
        marks = ",".join("?" * len(clean))
        conn.execute(f"UPDATE notifications SET is_read = 1 WHERE user_id = ? AND id IN ({marks})",
                     (user_id,) + tuple(clean))
        conn.commit()
    finally:
        conn.close()


# ----------------------------- admin lists ---------------------------------
def admin_people():
    conn = db.get_db()
    try:
        q = lambda sql: [dict(r) for r in conn.execute(sql)]
        customers = q("SELECT u.id, u.name, u.email, u.gender, u.is_active, u.created_at, "
                      "(SELECT COUNT(*) FROM bookings b WHERE b.customer_id = u.id) AS bookings FROM users u "
                      "WHERE u.role = 'customer' AND u.email NOT LIKE 'guest-%@salongo.local' ORDER BY u.id DESC")
        owners = q("SELECT u.id, u.name, u.email, u.gender, u.is_active, u.created_at, "
                   "(SELECT s.name FROM salons s WHERE s.owner_id = u.id ORDER BY s.id LIMIT 1) AS salon, "
                   "(SELECT s.approved FROM salons s WHERE s.owner_id = u.id ORDER BY s.id LIMIT 1) AS approved "
                   "FROM users u WHERE u.role = 'owner' AND u.email != 'owner@salongo.local' ORDER BY u.id DESC")
        for p in customers + owners:
            p["joined"] = str(p["created_at"])[:10]
        return customers, owners
    finally:
        conn.close()
