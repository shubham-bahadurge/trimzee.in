"""Web Push subscriptions and delivery for notifications outside the open app."""
import json
import logging
import os
import time
from urllib.parse import urlparse

import db
import pgdb
import requests
from py_vapid import Vapid, VapidException
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)
SUPPORTED_PUSH_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
)


def _vapid_configured():
    return all(os.environ.get(key) for key in
               ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_CLAIMS_EMAIL"))


def available():
    return _vapid_configured()


def _private_key():
    value = os.environ["VAPID_PRIVATE_KEY"]
    if "-----BEGIN" in value:
        return Vapid.from_pem(value.encode("utf-8"))
    return value


def init_push():
    conn = db.get_db()
    try:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS push_subscriptions ("
            "endpoint TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "p256dh TEXT NOT NULL, auth TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS push_subscriptions_by_user ON push_subscriptions (user_id)")
        conn.commit()
    finally:
        conn.close()


def _valid_endpoint(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        return False
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return False
    host = parsed.hostname.lower()
    return (host in SUPPORTED_PUSH_HOSTS
            or host.endswith(".notify.windows.com"))


def save_subscription(user_id, data):
    if not isinstance(data, dict):
        return False
    endpoint = data.get("endpoint")
    keys = data.get("keys")
    if (not _valid_endpoint(endpoint) or not isinstance(keys, dict)
            or not isinstance(keys.get("p256dh"), str) or not 1 <= len(keys["p256dh"]) <= 256
            or not isinstance(keys.get("auth"), str) or not 1 <= len(keys["auth"]) <= 256):
        return False

    conn = db.get_db()
    try:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.execute("INSERT INTO push_subscriptions (endpoint, user_id, p256dh, auth, created_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (endpoint, user_id, keys["p256dh"], keys["auth"], time.time()))
        conn.commit()
        return True
    except pgdb.INTEGRITY_ERRORS:
        conn.rollback()
        return False
    finally:
        conn.close()


def remove_subscription(user_id, endpoint):
    if not _valid_endpoint(endpoint):
        return
    conn = db.get_db()
    try:
        conn.execute("DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
                     (user_id, endpoint))
        conn.commit()
    except pgdb.DATABASE_ERRORS as error:
        conn.rollback()
        logger.warning("Could not remove Web Push subscription for user %s: %s", user_id, type(error).__name__)
    finally:
        conn.close()


def send_to_user(user_id, message, url, tag):
    if not _vapid_configured():
        return
    conn = db.get_db()
    try:
        subscriptions = conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    except pgdb.DATABASE_ERRORS as error:
        logger.warning("Could not load Web Push subscriptions for user %s: %s", user_id, type(error).__name__)
        return
    finally:
        conn.close()

    payload = json.dumps({
        "title": "Trimzee",
        "body": message,
        "url": url,
        "tag": tag,
        "icon": "/img/pwa-icon-192.png",
    })
    for subscription in subscriptions:
        info = {
            "endpoint": subscription["endpoint"],
            "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
        }
        try:
            webpush(
                subscription_info=info,
                data=payload,
                vapid_private_key=_private_key(),
                vapid_claims={"sub": os.environ["VAPID_CLAIMS_EMAIL"]},
                timeout=5,
            )
        except WebPushException as error:
            status = error.response.status_code if error.response is not None else None
            if status in (404, 410):
                remove_subscription(user_id, subscription["endpoint"])
            logger.warning("Web Push delivery failed for user %s (HTTP %s)", user_id, status)
        except (requests.RequestException, VapidException, ValueError):
            logger.warning("Web Push delivery failed for user %s", user_id)
