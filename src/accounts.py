"""Accounts: loading the signed-in user, profile, referral codes and rewards."""
import secrets
import time

import db
import meta

REWARD_CREDITS = 50
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"     # no 0/O/1/I/L: easy to read out loud


def init_accounts():
    conn = db.get_db()
    try:
        meta._add_column(conn, "users", "referral_code", "TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_referral_code ON users (referral_code)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS referrals (id {meta.ID}, "
                     "referrer_id INTEGER NOT NULL REFERENCES users(id), "
                     "referred_id INTEGER NOT NULL UNIQUE REFERENCES users(id), "
                     f"rewarded INTEGER NOT NULL DEFAULT 0, created_at {meta.REAL} NOT NULL)")
        conn.commit()
    finally:
        conn.close()


def load_user(user_id):
    """One query: everything the pages need about the signed-in user (or None)."""
    conn = db.get_db()
    try:
        row = conn.execute("SELECT id, name, email, role, gender, phone, created_at FROM users "
                           "WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
        if not row:
            return None
        user = dict(row)
        user["joined"] = str(user.pop("created_at"))[:10]
        return user
    finally:
        conn.close()


def update_phone(user_id, raw):
    import growth
    phone = growth.clean_mobile(raw)
    if not phone:
        return False
    conn = db.get_db()
    try:
        conn.execute("UPDATE users SET phone = ? WHERE id = ?", (phone, user_id))
        conn.commit()
        return True
    finally:
        conn.close()


# ----------------------------- referrals -----------------------------------
def ensure_code(user_id):
    conn = db.get_db()
    try:
        row = conn.execute("SELECT referral_code FROM users WHERE id = ?", (user_id,)).fetchone()
        if row and row["referral_code"]:
            return row["referral_code"]
        for _ in range(20):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            try:
                conn.execute("UPDATE users SET referral_code = ? WHERE id = ? AND referral_code IS NULL", (code, user_id))
                conn.commit()
                return code
            except db.sqlite3.IntegrityError:
                conn.rollback()
        return None
    finally:
        conn.close()


def find_referrer(code):
    code = (code or "").strip().upper()
    if not code:
        return None
    conn = db.get_db()
    try:
        row = conn.execute("SELECT id FROM users WHERE referral_code = ? AND is_active = 1", (code,)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def apply_referral(new_user_id, code):
    referrer = find_referrer(code)
    if not referrer or referrer == new_user_id:
        return False
    conn = db.get_db()
    try:
        conn.execute("INSERT INTO referrals (referrer_id, referred_id, created_at) VALUES (?, ?, ?)",
                     (referrer, new_user_id, time.time()))
        conn.commit()
        return True
    except db.sqlite3.IntegrityError:
        conn.rollback()
        return False
    finally:
        conn.close()


def maybe_reward(customer_id):
    """Called when a booking is completed: a referred customer's FIRST completed booking rewards both people."""
    conn = db.get_db()
    try:
        ref = conn.execute("SELECT id, referrer_id FROM referrals WHERE referred_id = ? AND rewarded = 0",
                           (customer_id,)).fetchone()
        if not ref:
            return
        done = conn.execute("SELECT COUNT(*) FROM bookings WHERE customer_id = ? AND status = 'completed'",
                            (customer_id,)).fetchone()[0]
        if done != 1:
            return
        who = conn.execute("SELECT name FROM users WHERE id = ?", (customer_id,)).fetchone()
        conn.execute("UPDATE referrals SET rewarded = 1 WHERE id = ?", (ref["id"],))
        meta._notify(conn, ref["referrer_id"], "update", ref["id"],
                     f"🎉 {who['name']} completed their first booking. You earned {REWARD_CREDITS} Trimzee credits!")
        meta._notify(conn, customer_id, "update", ref["id"],
                     f"🎁 Welcome bonus: you earned {REWARD_CREDITS} Trimzee credits.")
        conn.commit()
    finally:
        conn.close()


def referral_stats(user_id):
    conn = db.get_db()
    try:
        joined = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)).fetchone()[0]
        rewarded = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND rewarded = 1",
                                (user_id,)).fetchone()[0]
        own = conn.execute("SELECT COUNT(*) FROM referrals WHERE referred_id = ? AND rewarded = 1", (user_id,)).fetchone()[0]
        return {"joined": joined, "rewarded": rewarded, "credits": REWARD_CREDITS * (rewarded + own)}
    finally:
        conn.close()


def account_numbers(user_id, role):
    conn = db.get_db()
    try:
        one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
        if role == "customer":
            return {"bookings": one("SELECT COUNT(*) FROM bookings WHERE customer_id = ?", user_id),
                    "completed": one("SELECT COUNT(*) FROM bookings WHERE customer_id = ? AND status = 'completed'", user_id),
                    "reviews": one("SELECT COUNT(*) FROM reviews WHERE customer_id = ?", user_id)}
        return {"bookings": one("SELECT COUNT(*) FROM bookings b JOIN salons s ON s.id = b.salon_id WHERE s.owner_id = ?", user_id),
                "completed": one("SELECT COUNT(*) FROM bookings b JOIN salons s ON s.id = b.salon_id "
                                 "WHERE s.owner_id = ? AND b.status = 'completed'", user_id),
                "reviews": one("SELECT COUNT(*) FROM reviews r JOIN salons s ON s.id = r.salon_id WHERE s.owner_id = ?", user_id)}
    finally:
        conn.close()
