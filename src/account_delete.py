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
