"""SalonGo extras: admin, salon approval, reviews, service editing,
real-location support and login rate limiting. Builds on db.py."""
import os
import re
import time

from werkzeug.security import generate_password_hash

import db
import pgdb
from db import ACTIVE, now_ist


def get_db():
    return db.get_db()


CATEGORY_CHOICES = ["haircut", "beard", "hair styling", "facial", "massage", "coloring", "other"]

# Sample salons are placed around Pune so "real distance" can be demonstrated.
SAMPLE_COORDS = {
    "Style Studio": (18.5314, 73.8446), "Royal Cuts": (18.5167, 73.8563),
    "Glow Beauty Lounge": (18.5362, 73.8939), "The Trim Box": (18.5196, 73.8553),
    "Zen Spa & Salon": (18.5074, 73.8077), "Color Theory": (18.5590, 73.7868),
}

LOGIN_WINDOW = 15 * 60
MAX_FAILS_PER_EMAIL = 5
MAX_FAILS_PER_IP = 20


def init_extras():
    conn = get_db()
    try:
        for col, ddl in (("image_data", "BYTEA" if pgdb.ENABLED else "BLOB"),
                         ("image_mime", "TEXT"), ("opening_time", "TEXT NOT NULL DEFAULT '10:00'"),
                         ("closing_time", "TEXT NOT NULL DEFAULT '20:00'")):
            if pgdb.ENABLED:
                conn.execute(f"ALTER TABLE salons ADD COLUMN IF NOT EXISTS {col} {ddl}")
            else:
                cols = [r["name"] for r in conn.execute("PRAGMA table_info(salons)")]
                if col not in cols:
                    conn.execute(f"ALTER TABLE salons ADD COLUMN {col} {ddl}")
        if not pgdb.ENABLED:  # SQLite: add what the older tables are missing
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(salons)")]
            for col in ("lat", "lng"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE salons ADD COLUMN {col} REAL")
            conn.execute("CREATE TABLE IF NOT EXISTS login_attempts ("
                         "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                         "ip TEXT, email TEXT)")
        for name, (lat, lng) in SAMPLE_COORDS.items():
            conn.execute(
                "UPDATE salons SET lat = ?, lng = ? WHERE name = ? AND lat IS NULL AND owner_id = "
                "(SELECT id FROM users WHERE email = 'owner@salongo.local')", (lat, lng, name))
        _ensure_admin(conn)
        conn.commit()
    finally:
        conn.close()


def _ensure_admin(conn):
    """Admin login comes from project settings; no password is stored in the code."""
    email = os.environ.get("ADMIN_EMAIL", "admin@salongo.local").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        return
    if not conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
        conn.execute("INSERT INTO users (name, email, password_hash, role) VALUES "
                     "('Admin', ?, ?, 'admin')", (email, generate_password_hash(password)))


# ----------------------------- accounts ------------------------------------
def create_account(name, email, password, role, salon_name=""):
    """Like db.create_user, but a new owner's salon waits for admin approval."""
    uid, error = db.create_user(name, email, password, role, salon_name)
    if uid and role == "owner":
        conn = get_db()
        try:
            conn.execute("UPDATE salons SET approved = 0 WHERE owner_id = ?", (uid,))
            conn.commit()
        finally:
            conn.close()
    return uid, error


# ----------------------------- rate limiting -------------------------------
def too_many_logins(ip, email):
    cutoff = time.time() - LOGIN_WINDOW
    email = (email or "").strip().lower()
    conn = get_db()
    try:
        by_email = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE email = ? AND ts > ?",
                                (email, cutoff)).fetchone()[0]
        by_ip = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND ts > ?",
                             (ip, cutoff)).fetchone()[0]
        return by_email >= MAX_FAILS_PER_EMAIL or by_ip >= MAX_FAILS_PER_IP
    finally:
        conn.close()


def record_login_failure(ip, email):
    conn = get_db()
    try:
        conn.execute("DELETE FROM login_attempts WHERE ts < ?", (time.time() - LOGIN_WINDOW,))
        conn.execute("INSERT INTO login_attempts (ts, ip, email) VALUES (?, ?, ?)",
                     (time.time(), ip, (email or "").strip().lower()))
        conn.commit()
    finally:
        conn.close()


def clear_login_failures(email):
    conn = get_db()
    try:
        conn.execute("DELETE FROM login_attempts WHERE email = ?", ((email or "").strip().lower(),))
        conn.commit()
    finally:
        conn.close()


# ----------------------------- public salon data ---------------------------
LIVE = "s.approved = 1 AND o.is_active = 1"


def get_salons():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT s.id, s.name, s.distance_km, s.rating, s.emoji, s.image_mime, s.status, s.next_slot, "
            "s.lat, s.lng, MIN(sv.price) AS price FROM salons s "
            "JOIN users o ON o.id = s.owner_id JOIN services sv ON sv.salon_id = s.id "
            f"WHERE {LIVE} GROUP BY s.id ORDER BY s.distance_km").fetchall()
        out = []
        for r in rows:
            names = [x["name"] for x in conn.execute(
                "SELECT name FROM services WHERE salon_id = ? ORDER BY id", (r["id"],))]
            out.append({"id": r["id"], "name": r["name"], "rating": r["rating"],
                        "distance": r["distance_km"], "price": r["price"], "status": r["status"],
                        "next": r["next_slot"], "emoji": r["emoji"], "services": names,
                        "lat": r["lat"], "lng": r["lng"],
                        "image_url": f"/salons/{r['id']}/image" if r["image_mime"] else None})
        return out
    finally:
        conn.close()


def salon_is_live(salon_id):
    conn = get_db()
    try:
        return conn.execute("SELECT 1 FROM salons s JOIN users o ON o.id = s.owner_id "
                            f"WHERE s.id = ? AND {LIVE}", (salon_id,)).fetchone() is not None
    finally:
        conn.close()


def get_salon(salon_id):
    if not salon_is_live(salon_id):
        return None
    data = db.get_salon(salon_id)
    if data:
        data["reviews"] = get_reviews(salon_id)
        data["review_count"] = review_count(salon_id)
    return data


# ----------------------------- reviews -------------------------------------
def get_reviews(salon_id, limit=10):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT r.rating, r.comment, r.created_at, u.name FROM reviews r "
            "JOIN users u ON u.id = r.customer_id WHERE r.salon_id = ? ORDER BY r.id DESC LIMIT ?",
            (salon_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def review_count(salon_id):
    conn = get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM reviews WHERE salon_id = ?", (salon_id,)).fetchone()[0]
    finally:
        conn.close()


def _recompute_rating(conn, salon_id):
    avg = conn.execute("SELECT AVG(rating) FROM reviews WHERE salon_id = ?", (salon_id,)).fetchone()[0]
    if avg is not None:
        conn.execute("UPDATE salons SET rating = ? WHERE id = ?", (round(float(avg), 1), salon_id))


def add_review(user_id, booking_id, rating, comment):
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return 400, "Please choose a star rating."
    if not 1 <= rating <= 5:
        return 400, "Please choose a star rating from 1 to 5."
    comment = (comment or "").strip()[:300]
    conn = get_db()
    try:
        b = conn.execute("SELECT salon_id, status FROM bookings WHERE id = ? AND customer_id = ?",
                         (booking_id, user_id)).fetchone()
        if not b:
            return 404, "Booking not found."
        if b["status"] != "completed":
            return 409, "You can review a booking after the salon marks it completed."
        try:
            conn.execute("INSERT INTO reviews (booking_id, customer_id, salon_id, rating, comment) "
                         "VALUES (?, ?, ?, ?, ?)", (booking_id, user_id, b["salon_id"], rating, comment))
            _recompute_rating(conn, b["salon_id"])
            conn.commit()
            return 200, "Thanks for your review!"
        except db.sqlite3.IntegrityError:
            conn.rollback()
            return 409, "You already reviewed this booking."
    finally:
        conn.close()


def get_user_bookings(user_id):
    conn = get_db()
    try:
        now = now_ist().strftime("%Y-%m-%d %H:%M")
        rows = conn.execute(
            "SELECT b.id, b.slot_start, b.status, s.name AS salon_name, sv.name AS service_name, "
            "sv.price, r.rating AS my_rating FROM bookings b "
            "JOIN salons s ON s.id = b.salon_id JOIN services sv ON sv.id = b.service_id "
            "LEFT JOIN reviews r ON r.booking_id = b.id "
            "WHERE b.customer_id = ? ORDER BY b.slot_start DESC", (user_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["can_cancel"] = d["status"] in ACTIVE and d["slot_start"] > now
            d["can_review"] = d["status"] == "completed" and d["my_rating"] is None
            out.append(d)
        return out
    finally:
        conn.close()


# ----------------------------- owner tools ---------------------------------
def owner_salon_full(owner_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT id, name, status, approved, lat, lng, image_mime, "
                           "opening_time, closing_time FROM salons "
                           "WHERE owner_id = ? ORDER BY id LIMIT 1", (owner_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_salon_hours(owner_id, opening_time, closing_time):
    time_pattern = re.compile(r"(?:[01]\d|2[0-3]):(?:00|30)")
    if not time_pattern.fullmatch(opening_time or "") or not time_pattern.fullmatch(closing_time or ""):
        return False, "Choose opening and closing times on a 30-minute boundary."
    start = int(opening_time[:2]) * 60 + int(opening_time[3:])
    end = int(closing_time[:2]) * 60 + int(closing_time[3:])
    if end - start < 30:
        return False, "Closing time must be at least 30 minutes after opening time."
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE salons SET opening_time = ?, closing_time = ? "
            "WHERE id = (SELECT id FROM salons WHERE owner_id = ? ORDER BY id LIMIT 1)",
            (opening_time, closing_time, owner_id))
        conn.commit()
        return (True, "Booking hours saved.") if cur.rowcount else (False, "Salon not found.")
    finally:
        conn.close()


def save_salon_image(owner_id, image_data, image_mime):
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE salons SET image_data = ?, image_mime = ? "
            "WHERE id = (SELECT id FROM salons WHERE owner_id = ? ORDER BY id LIMIT 1)",
            (image_data, image_mime, owner_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_salon_image(owner_id):
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE salons SET image_data = NULL, image_mime = NULL "
            "WHERE id = (SELECT id FROM salons WHERE owner_id = ? ORDER BY id LIMIT 1)",
            (owner_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def salon_image(salon_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT image_data, image_mime, approved, owner_id "
                           "FROM salons WHERE id = ?", (salon_id,)).fetchone()
        if not row or not row["image_data"] or not row["image_mime"]:
            return None
        return row["image_data"], row["image_mime"], row["approved"], row["owner_id"]
    finally:
        conn.close()


def get_owner_services(owner_id):
    salon = db.get_owner_salon(owner_id)
    if not salon:
        return []
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, name, category, price, duration_min FROM services "
            "WHERE salon_id = ? ORDER BY id", (salon["id"],))]
    finally:
        conn.close()


def _clean_service(name, category, price, duration):
    name = (name or "").strip()
    if not 2 <= len(name) <= 40:
        return None, "Service name must be 2-40 characters."
    if category not in CATEGORY_CHOICES:
        return None, "Please choose a category."
    try:
        price, duration = int(price), int(duration)
    except (TypeError, ValueError):
        return None, "Price and duration must be whole numbers."
    if not 0 <= price <= 100000:
        return None, "Price must be between 0 and 100000."
    if not 5 <= duration <= 480:
        return None, "Duration must be between 5 and 480 minutes."
    return (name, category, price, duration), None


def save_service(owner_id, service_id, name, category, price, duration):
    """service_id None = add new. Returns (ok, message)."""
    values, error = _clean_service(name, category, price, duration)
    if error:
        return False, error
    salon = db.get_owner_salon(owner_id)
    if not salon:
        return False, "Salon not found."
    conn = get_db()
    try:
        if service_id is None:
            n = conn.execute("SELECT COUNT(*) FROM services WHERE salon_id = ?", (salon["id"],)).fetchone()[0]
            if n >= 30:
                return False, "You can have up to 30 services."
            conn.execute("INSERT INTO services (salon_id, name, category, price, duration_min) "
                         "VALUES (?, ?, ?, ?, ?)", (salon["id"],) + values)
            message = "Service added."
        else:
            cur = conn.execute("UPDATE services SET name = ?, category = ?, price = ?, duration_min = ? "
                               "WHERE id = ? AND salon_id = ?", values + (service_id, salon["id"]))
            if cur.rowcount == 0:
                return False, "Service not found."
            message = "Service saved."
        conn.commit()
        return True, message
    finally:
        conn.close()


def delete_service(owner_id, service_id):
    salon = db.get_owner_salon(owner_id)
    if not salon:
        return False, "Salon not found."
    conn = get_db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM services WHERE salon_id = ?", (salon["id"],)).fetchone()[0]
        if n <= 1:
            return False, "Keep at least one service so customers can book."
        try:
            cur = conn.execute("DELETE FROM services WHERE id = ? AND salon_id = ?", (service_id, salon["id"]))
            conn.commit()
        except db.sqlite3.IntegrityError:
            conn.rollback()
            return False, "This service has bookings, so it can't be deleted. Rename it or set a new price instead."
        return (True, "Service deleted.") if cur.rowcount else (False, "Service not found.")
    finally:
        conn.close()


def set_salon_location(owner_id, lat, lng):
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return False
    conn = get_db()
    try:
        cur = conn.execute("UPDATE salons SET lat = ?, lng = ? WHERE id = "
                           "(SELECT id FROM salons WHERE owner_id = ? ORDER BY id LIMIT 1)",
                           (lat, lng, owner_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ----------------------------- admin ---------------------------------------
def admin_overview():
    conn = get_db()
    try:
        one = lambda q: conn.execute(q).fetchone()[0]
        real_users = "email NOT LIKE 'guest-%@salongo.local' AND email != 'owner@salongo.local'"
        return {
            "customers": one(f"SELECT COUNT(*) FROM users WHERE role = 'customer' AND {real_users}"),
            "owners": one(f"SELECT COUNT(*) FROM users WHERE role = 'owner' AND {real_users}"),
            "salons": one("SELECT COUNT(*) FROM salons WHERE approved = 1"),
            "pending": one("SELECT COUNT(*) FROM salons WHERE approved = 0"),
            "bookings": one("SELECT COUNT(*) FROM bookings"),
            "revenue": one("SELECT COALESCE(SUM(sv.price), 0) FROM bookings b "
                           "JOIN services sv ON sv.id = b.service_id WHERE b.status = 'completed'"),
            "reviews": one("SELECT COUNT(*) FROM reviews"),
        }
    finally:
        conn.close()


def admin_lists():
    conn = get_db()
    try:
        q = lambda sql: [dict(r) for r in conn.execute(sql)]
        return {
            "pending": q("SELECT s.id, s.name, u.name AS owner_name, u.email FROM salons s "
                         "JOIN users u ON u.id = s.owner_id WHERE s.approved = 0 ORDER BY s.id"),
            "salons": q("SELECT s.id, s.name, s.approved, s.status, u.email FROM salons s "
                        "JOIN users u ON u.id = s.owner_id ORDER BY s.id DESC LIMIT 100"),
            "users": q("SELECT id, name, email, role, is_active FROM users "
                       "WHERE email NOT LIKE 'guest-%@salongo.local' AND email != 'owner@salongo.local' "
                       "ORDER BY id DESC LIMIT 100"),
            "bookings": q("SELECT b.id, b.slot_start, b.status, s.name AS salon_name, u.name AS customer_name "
                          "FROM bookings b JOIN salons s ON s.id = b.salon_id "
                          "JOIN users u ON u.id = b.customer_id ORDER BY b.id DESC LIMIT 30"),
            "reviews": q("SELECT r.id, r.rating, r.comment, s.name AS salon_name, u.name AS customer_name "
                         "FROM reviews r JOIN salons s ON s.id = r.salon_id "
                         "JOIN users u ON u.id = r.customer_id ORDER BY r.id DESC LIMIT 30"),
        }
    finally:
        conn.close()


def admin_set_salon_approval(salon_id, decision):
    value = {"approve": 1, "reject": 2}.get(decision)
    if value is None:
        return False
    conn = get_db()
    try:
        cur = conn.execute("UPDATE salons SET approved = ? WHERE id = ?", (value, salon_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def admin_set_user_active(user_id, action):
    value = {"suspend": 0, "activate": 1}.get(action)
    if value is None:
        return False
    conn = get_db()
    try:
        cur = conn.execute("UPDATE users SET is_active = ? WHERE id = ? AND role != 'admin'",
                           (value, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def admin_delete_review(review_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT salon_id FROM reviews WHERE id = ?", (review_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        _recompute_rating(conn, row["salon_id"])
        conn.commit()
        return True
    finally:
        conn.close()
