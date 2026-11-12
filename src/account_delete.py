"""Permanent account deletion with an e-mail OTP."""
import hashlib
import hmac
import os
import secrets
import time

import db
import mailer
from meta import ID, REAL

TTL = 600           # the code expires after 10 minutes
MAX_TRIES = 5
GAP = 60            # wait at least a minute before sending another code

_ready = False


def _ensure():
    global _ready
    if not _ready:
        conn = db.get_db()
        try:
            conn.execute(f"CREATE TABLE IF NOT EXISTS deletions (id {ID}, user_id INTEGER NOT NULL UNIQUE, "
                         f"otp_hash TEXT NOT NULL, expires {REAL} NOT NULL, "
                         f"tries INTEGER NOT NULL DEFAULT 0, last_sent {REAL} NOT NULL)")
            conn.commit()
        finally:
            conn.close()
        _ready = True


def _hash(user_id, code):
    key = (os.environ.get("SECRET_KEY") or "dev").encode()
    return hmac.new(key, f"delete:{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def request_code(user):
    """Email a 6-digit code to the account owner. Returns (ok, message)."""
    if not mailer.email_configured():
        return False, "Email is not set up, so account deletion is unavailable right now."
    _ensure()
    conn = db.get_db()
    try:
        now = time.time()
        old = conn.execute("SELECT last_sent FROM deletions WHERE user_id = ?", (user["id"],)).fetchone()
        if old and now - old["last_sent"] < GAP:
            return False, "Please wait a minute before asking for another code."
        code = f"{secrets.randbelow(10 ** 6):06d}"
        conn.execute("DELETE FROM deletions WHERE user_id = ?", (user["id"],))
        conn.execute("INSERT INTO deletions (user_id, otp_hash, expires, tries, last_sent) VALUES (?, ?, ?, 0, ?)",
                     (user["id"], _hash(user["id"], code), now + TTL, now))
        conn.commit()
    finally:
        conn.close()
    try:
        mailer.send_email(user["email"], "Confirm deleting your Trimzee account",
                          f"Your code to permanently delete your Trimzee account is {code}.\n\n"
                          "It expires in 10 minutes. If you did not ask for this, ignore this email; "
                          "your account is safe.")
    except Exception:
        return False, "Could not send the code. Please try again in a moment."
    return True, f"We sent a 6-digit code to {user['email']}."


def delete_account(user, code):
    """Check the 6-digit code, then anonymize a customer account. Returns (ok, message)."""
    if user["role"] == "owner":
        return False, "Owner accounts can't be deleted here yet. Please contact Trimzee support."
    typed = "".join(ch for ch in (code or "") if ch.isdigit())
    uid, email = user["id"], user["email"]
    from safety import _ensure as _safety_ensure
    _safety_ensure()
    import growth
    growth.init_growth()
    _ensure()
    conn = db.get_db()
    try:
        row = conn.execute("SELECT otp_hash, expires, tries FROM deletions WHERE user_id = ?", (uid,)).fetchone()
        if not row or time.time() > row["expires"]:
            conn.execute("DELETE FROM deletions WHERE user_id = ?", (uid,))
            conn.commit()
            return False, "This code has expired. Please ask for a new one."
        if not hmac.compare_digest(_hash(uid, typed), row["otp_hash"]):
            conn.execute("UPDATE deletions SET tries = tries + 1 WHERE user_id = ?", (uid,))
            tries = conn.execute("SELECT tries FROM deletions WHERE user_id = ?", (uid,)).fetchone()["tries"]
            if tries >= MAX_TRIES:
                conn.execute("DELETE FROM deletions WHERE user_id = ?", (uid,))
            conn.commit()
            if tries >= MAX_TRIES:
                return False, "Too many wrong codes. Please ask for a new one."
            return False, f"That code is not right. {MAX_TRIES - tries} tries left."

        # Everything below succeeds together or not at all.
        conn.execute("UPDATE bookings SET contact_phone = NULL WHERE customer_id = ?", (uid,))
        conn.execute("UPDATE bookings SET status = 'cancelled' WHERE customer_id = ? AND status = 'pending'", (uid,))
        conn.execute("DELETE FROM referrals WHERE referrer_id = ? OR referred_id = ?", (uid, uid))
        conn.execute("DELETE FROM waitlist WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM notifications WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM signups WHERE email = ?", (email,))
        conn.execute("DELETE FROM resets WHERE email = ?", (email,))
        conn.execute("DELETE FROM deletions WHERE user_id = ?", (uid,))
        conn.execute("UPDATE users SET name = 'Deleted user', email = ?, phone = NULL, gender = NULL, "
                     "referral_code = NULL, password_hash = '!', is_active = 0 WHERE id = ?",
                     (f"deleted-{uid}@deleted.invalid", uid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return True, "Your account has been deleted."
