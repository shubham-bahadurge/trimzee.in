"""Sign-up with an e-mail OTP. When e-mail isn't set up yet, sign-up works without the code."""
import hashlib
import hmac
import json
import os
import secrets
import time

from werkzeug.security import generate_password_hash

import db
import growth
import mailer
import meta

TTL, MAX_TRIES, GAP, MAX_SENDS, WINDOW = 600, 5, 45, 5, 3600


def _hash(email, code):
    key = (os.environ.get("SECRET_KEY") or "dev").encode()
    return hmac.new(key, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def validate(form):
    """Returns (data, error)."""
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    role, gender = form.get("role"), form.get("gender")
    salon = (form.get("salon_name") or "").strip()
    if gender not in meta.USER_GENDERS:
        return None, "Please choose your gender (Male or Female)."
    if role not in ("customer", "owner"):
        return None, "Please choose an account type."
    if not 2 <= len(name) <= 60:
        return None, "Please enter your name (2-60 characters)."
    if len(email) > 120 or not db.EMAIL_RE.match(email):
        return None, "Please enter a valid email address."
    if not 8 <= len(password) <= 128:
        return None, "Password must be 8-128 characters."
    phone = growth.clean_mobile(form.get("phone"))
    if role == "owner":
        if not 2 <= len(salon) <= 60:
            return None, "Please enter your business name (2-60 characters)."
        if not phone:
            return None, "Please enter a valid 10-digit mobile number."
    return {"name": name, "email": email, "role": role, "gender": gender, "salon_name": salon,
            "phone": phone, "pw_hash": generate_password_hash(password)}, None


def _exists(conn, email):
    return conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None


def _send_code(email, code):
    mailer.send_email(email, "Your Trimzee verification code",
                      f"Your Trimzee verification code is {code}.\n\nIt expires in 10 minutes. "
                      "If you didn't try to sign up, you can ignore this email.")


def begin(form):
    """Returns ("error", message) | ("direct", data) | ("otp", email)."""
    data, error = validate(form)
    if error:
        return "error", error
    conn = db.get_db()
    try:
        if _exists(conn, data["email"]):
            return "error", "That email is already registered. Try logging in."
        if not mailer.email_configured():
            return "direct", data
        now = time.time()
        conn.execute("DELETE FROM signups WHERE expires < ?", (now - 3600,))
        old = conn.execute("SELECT sends, window_start, last_sent FROM signups WHERE email = ?", (data["email"],)).fetchone()
        sends, window = 1, now
        if old and now - old["window_start"] < WINDOW:
            if now - old["last_sent"] < GAP:
                return "error", f"Please wait {int(GAP - (now - old['last_sent']))} seconds before asking for another code."
            if old["sends"] >= MAX_SENDS:
                return "error", "Too many codes requested. Please try again in an hour."
            sends, window = old["sends"] + 1, old["window_start"]
        code = f"{secrets.randbelow(10**6):06d}"
        conn.execute("DELETE FROM signups WHERE email = ?", (data["email"],))
        conn.execute("INSERT INTO signups (email, data, otp_hash, expires, tries, sends, window_start, last_sent) "
                     "VALUES (?, ?, ?, ?, 0, ?, ?, ?)", (data["email"], json.dumps(data), _hash(data["email"], code),
                                                         now + TTL, sends, window, now))
        conn.commit()
    finally:
        conn.close()
    try:
        _send_code(data["email"], code)
    except Exception:
        return "error", "We couldn't send the verification email. Please check the address and try again."
    return "otp", data["email"]


def verify(email, code):
    """Returns ("ok", user_id) or ("error", message)."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    conn = db.get_db()
    try:
        row = conn.execute("SELECT data, otp_hash, expires, tries FROM signups WHERE email = ?", (email,)).fetchone()
        if not row:
            return "error", "This code has expired. Please sign up again."
        if time.time() > row["expires"]:
            conn.execute("DELETE FROM signups WHERE email = ?", (email,))
            conn.commit()
            return "error", "This code has expired. Please sign up again."
        if not hmac.compare_digest(_hash(email, code), row["otp_hash"]):
            tries = row["tries"] + 1
            if tries >= MAX_TRIES:
                conn.execute("DELETE FROM signups WHERE email = ?", (email,))
                conn.commit()
                return "error", "Too many wrong codes. Please sign up again."
            conn.execute("UPDATE signups SET tries = ? WHERE email = ?", (tries, email))
            conn.commit()
            return "error", f"That code isn't right. {MAX_TRIES - tries} tries left."
        data = json.loads(row["data"])
        conn.execute("DELETE FROM signups WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()
    uid, error = growth.create_member(data)
    return ("ok", uid) if uid else ("error", error)


def resend(email):
    """Returns (ok, message)."""
    conn = db.get_db()
    try:
        row = conn.execute("SELECT data, sends, window_start, last_sent FROM signups WHERE email = ?", (email,)).fetchone()
        if not row:
            return False, "Please sign up again."
        now = time.time()
        if now - row["last_sent"] < GAP:
            return False, f"Please wait {int(GAP - (now - row['last_sent']))} seconds before asking again."
        if now - row["window_start"] < WINDOW and row["sends"] >= MAX_SENDS:
            return False, "Too many codes requested. Please try again in an hour."
        sends, window = (row["sends"] + 1, row["window_start"]) if now - row["window_start"] < WINDOW else (1, now)
        code = f"{secrets.randbelow(10**6):06d}"
        conn.execute("UPDATE signups SET otp_hash = ?, expires = ?, tries = 0, sends = ?, window_start = ?, last_sent = ? "
                     "WHERE email = ?", (_hash(email, code), now + TTL, sends, window, now, email))
        conn.commit()
    finally:
        conn.close()
    try:
        _send_code(email, code)
    except Exception:
        return False, "We couldn't send the email. Please try again."
    return True, "A new code is on its way."
