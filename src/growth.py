"""Trimzee growth features: owner Accept step, phone numbers, WhatsApp, shop waiting list, accounts."""
import re
import secrets
import time

import db
import mailer
import meta
import extras


def init_growth():
    conn = db.get_db()
    try:
        meta._add_column(conn, "users", "phone", "TEXT")
        conn.execute(f"CREATE TABLE IF NOT EXISTS signups (id {meta.ID}, email TEXT NOT NULL UNIQUE, data TEXT NOT NULL, "
                     f"otp_hash TEXT NOT NULL, expires {meta.REAL} NOT NULL, tries INTEGER NOT NULL DEFAULT 0, "
                     f"sends INTEGER NOT NULL DEFAULT 1, window_start {meta.REAL} NOT NULL, last_sent {meta.REAL} NOT NULL)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS waitlist (id {meta.ID}, "
                     f"user_id INTEGER NOT NULL UNIQUE REFERENCES users(id), created_at {meta.REAL} NOT NULL)")
        conn.commit()
    finally:
        conn.close()


# ----------------------------- phone numbers -------------------------------
def clean_mobile(raw):
    """Indian mobile -> 10 digits, or None."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if re.fullmatch(r"[6-9]\d{9}", d) else None


def wa_number(raw):
    """Digits with country code for WhatsApp (India by default), or None."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 10:
        return "91" + d
    return d if 11 <= len(d) <= 13 else None


# ----------------------------- accounts ------------------------------------
def create_member(data):
    """data: validated sign-up dict with a ready password hash. Returns (user_id, error)."""
    uid, error = extras.create_account(data["name"], data["email"], secrets.token_urlsafe(16),
                                       data["role"], data.get("salon_name", ""))
    if not uid:
        return None, error
    conn = db.get_db()
    try:
        conn.execute("UPDATE users SET password_hash = ?, gender = ?, phone = ? WHERE id = ?",
                     (data["pw_hash"], data["gender"], data.get("phone"), uid))
        if data["role"] == "owner":  # a woman's business is a parlour for women, a man's a salon for men (editable)
            conn.execute("UPDATE salons SET gender = ? WHERE owner_id = ?",
                         ("women" if data["gender"] == "female" else "men", uid))
        conn.commit()
    finally:
        conn.close()
    return uid, None


def venue_word(gender):
    return "parlour" if gender == "women" else "salon"


# ----------------------------- booking flow --------------------------------
def booking_info(booking_id):
    conn = db.get_db()
    try:
        r = conn.execute("SELECT b.id, b.slot_start, b.contact_phone, b.customer_id, s.name AS salon, s.owner_id, "
                         "sv.name AS service, sv.price, br.name AS barber, cu.name AS customer, ow.phone AS owner_phone "
                         "FROM bookings b JOIN salons s ON s.id = b.salon_id JOIN services sv ON sv.id = b.service_id "
                         "LEFT JOIN barbers br ON br.id = b.barber_id "
                         "JOIN users cu ON cu.id = b.customer_id JOIN users ow ON ow.id = s.owner_id "
                         "WHERE b.id = ?", (booking_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def after_booking(booking_id):
    """New bookings wait for the owner's Accept. Returns booking info."""
    conn = db.get_db()
    try:
        conn.execute("UPDATE bookings SET status = 'pending' WHERE id = ? AND status = 'confirmed'", (booking_id,))
        conn.commit()
    finally:
        conn.close()
    return booking_info(booking_id)


def whatsapp_new_booking(info):
    if not info:
        return
    when = f"{info['slot_start'][:10]} {meta._pretty(info['slot_start'])}"
    mailer.send_whatsapp(wa_number(info["contact_phone"]),
                         f"Trimzee: your booking request #{info['id']} at {info['salon']} for {info['service']} "
                         f"on {when} is sent. The salon will confirm shortly.")
    mailer.send_whatsapp(wa_number(info["owner_phone"]),
                         f"Trimzee: new booking request from {info['customer']} for {info['service']} on {when}. "
                         "Open your dashboard to Accept or Reject.")


def owner_accept(owner_id, booking_id):
    conn = db.get_db()
    try:
        cur = conn.execute("UPDATE bookings SET status = 'confirmed' WHERE id = ? AND status = 'pending' "
                           "AND salon_id IN (SELECT id FROM salons WHERE owner_id = ?)", (booking_id, owner_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def tell_customer(booking_id, accepted):
    """In-app alert + WhatsApp (if set up) telling the customer the owner's decision."""
    info = booking_info(booking_id)
    if not info:
        return
    when = meta._pretty(info["slot_start"])
    text = (f"✅ {info['salon']} accepted your booking for {when}. See you soon!" if accepted else
            f"❌ {info['salon']} couldn't take your booking for {when}. Please pick another time.")
    conn = db.get_db()
    try:
        meta._notify(conn, info["customer_id"], "update", booking_id, text)
        conn.commit()
    finally:
        conn.close()
    mailer.send_whatsapp(wa_number(info["contact_phone"]), "Trimzee: " + text)


def salon_owner_phone(salon_id):
    conn = db.get_db()
    try:
        r = conn.execute("SELECT u.phone FROM salons s JOIN users u ON u.id = s.owner_id WHERE s.id = ?",
                         (salon_id,)).fetchone()
        return wa_number(r["phone"]) if r and r["phone"] else None
    finally:
        conn.close()


# ----------------------------- shop waiting list ---------------------------
def join_waitlist(user_id):
    conn = db.get_db()
    try:
        if not conn.execute("SELECT 1 FROM waitlist WHERE user_id = ?", (user_id,)).fetchone():
            conn.execute("INSERT INTO waitlist (user_id, created_at) VALUES (?, ?)", (user_id, time.time()))
            conn.commit()
    except db.sqlite3.IntegrityError:
        conn.rollback()
    finally:
        conn.close()


def on_waitlist(user_id):
    conn = db.get_db()
    try:
        return conn.execute("SELECT 1 FROM waitlist WHERE user_id = ?", (user_id,)).fetchone() is not None
    finally:
        conn.close()


def waitlist_count():
    conn = db.get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0]
    finally:
        conn.close()


# ----------------------------- admin ---------------------------------------
def admin_people():
    customers, owners = meta.admin_people()
    conn = db.get_db()
    try:
        phones = {r["id"]: r["phone"] for r in conn.execute("SELECT id, phone FROM users WHERE role = 'owner'")}
    finally:
        conn.close()
    for o in owners:
        o["phone"] = phones.get(o["id"]) or "-"
    return customers, owners
