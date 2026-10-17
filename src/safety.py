"""Per-IP limits for actions that send e-mail, and password reset by e-mail code."""

import hashlib
import hmac
import os
import secrets
import time

from werkzeug.security import generate_password_hash

import db
import mailer
import meta

TTL, MAX_TRIES, GAP, WINDOW = 600, 5, 45, 3600
_ready = False


def _ensure():
    global _ready
    if not _ready:
        init_safety()
        _ready = True


def init_safety():
    conn = db.get_db()
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS send_log (id {meta.ID}, ts {meta.REAL} NOT NULL, "
                     "ip TEXT NOT NULL, kind TEXT NOT NULL)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS resets (id {meta.ID}, email TEXT NOT NULL UNIQUE, "
                     f"otp_hash TEXT NOT NULL, expires {meta.REAL} NOT NULL, "
                     f"tries INTEGER NOT NULL DEFAULT 0, last_sent {meta.REAL} NOT NULL)")
        conn.commit()
    finally:
        conn.close()


def hit(ip, kind, limit):
    """Count one e-mail-sending action for this IP. False when the IP used up its hourly limit."""
    _ensure()
    now = time.time()
    conn = db.get_db()
    try:
        conn.execute("DELETE FROM send_log WHERE ts < ?", (now - WINDOW,))
        used = conn.execute("SELECT COUNT(*) FROM send_log WHERE ip = ? AND kind = ? AND ts > ?",
                            (ip, kind, now - WINDOW)).fetchone()[0]
        if used >= limit:
            conn.commit()
            return False
        conn.execute("INSERT INTO send_log (ts, ip, kind) VALUES (?, ?, ?)", (now, ip, kind))
        conn.commit()
        return True
    finally:
        conn.close()


def _hash(email, code):
    key = (os.environ.get("SECRET_KEY") or "dev").encode()
    return hmac.new(key, f"reset:{email}:{code}".encode(), hashlib.sha256).hexdigest()


def reset_begin(email):
    """Send a code if the e-mail belongs to an active, non-admin account.
    Always returns the cleaned e-mail, so the page looks the same whether or not the account exists."""
    email = (email or "").strip().lower()
    if not email or len(email) > 120 or not db.EMAIL_RE.match(email) or not mailer.email_configured():
        return email
    _ensure()
    conn = db.get_db()
    try:
        user = conn.execute("SELECT id FROM users WHERE email = ? AND is_active = 1 "
                            "AND role != 'admin' AND password_hash != '!'", (email,)).fetchone()
        if not user:
            return email
        now = time.time()
        old = conn.execute("SELECT last_sent FROM resets WHERE email = ?", (email,)).fetchone()
        if old and now - old["last_sent"] < GAP:
            return email
        code = f"{secrets.randbelow(10 ** 6):06d}"
        conn.execute("DELETE FROM resets WHERE email = ?", (email,))
        conn.execute("INSERT INTO resets (email, otp_hash, expires, tries, last_sent) VALUES (?, ?, ?, 0, ?)",
                     (email, _hash(email, code), now + TTL, now))
        conn.commit()
    finally:
        conn.close()
    try:
        mailer.send_email(email, "Your Trimzee password reset code",
                          f"Your Trimzee password reset code is {code}.\n\nIt expires in 10 minutes. "
                          "If you did not ask for this, ignore this email: your password has not changed.")
    except Exception:
        pass
    return email


def reset_verify(email, code, password):
    """Returns (ok, message)."""
    email = (email or "").strip().lower()
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not 8 <= len(password or "") <= 128:
        return False, "Password must be 8-128 characters."
    _ensure()
    conn = db.get_db()
    try:
        row = conn.execute("SELECT otp_hash, expires, tries FROM resets WHERE email = ?", (email,)).fetchone()
        if not row:
            return False, "This code has expired. Please ask for a new one."
        if time.time() > row["expires"]:
            conn.execute("DELETE FROM resets WHERE email = ?", (email,))
            conn.commit()
            return False, "This code has expired. Please ask for a new one."
        if not hmac.compare_digest(_hash(email, code), row["otp_hash"]):
            tries = row["tries"] + 1
            if tries >= MAX_TRIES:
                conn.execute("DELETE FROM resets WHERE email = ?", (email,))
                conn.commit()
                return False, "Too many wrong codes. Please ask for a new one."
            conn.execute("UPDATE resets SET tries = ? WHERE email = ?", (tries, email))
            conn.commit()
            return False, f"That code is not right. {MAX_TRIES - tries} tries left."
        conn.execute("UPDATE users SET password_hash = ? WHERE email = ? AND is_active = 1 AND role != 'admin'",
                     (generate_password_hash(password), email))
        conn.execute("DELETE FROM resets WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()
    return True, "Password changed. You can sign in now."
