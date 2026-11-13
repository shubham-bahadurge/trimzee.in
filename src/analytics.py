"""Google Analytics 4 integration and server-side Measurement Protocol helper."""
import hashlib
import json
import logging
import os
import urllib.request

logger = logging.getLogger("trimzee.analytics")


def get_measurement_id():
    """Return GA_MEASUREMENT_ID if configured, else empty string."""
    return os.environ.get("GA_MEASUREMENT_ID", "").strip()


def send_booking_completed(booking_id, salon_id, value=0.0, user_id=None):
    """Send server-side booking_completed event to GA4 via Measurement Protocol.

    Strictly complies with privacy rules: never includes phone numbers, names, or visit codes.
    """
    ga_id = get_measurement_id()
    if not ga_id:
        return

    api_secret = os.environ.get("GA_API_SECRET", "").strip()
    url = f"https://www.google-analytics.com/mp/collect?measurement_id={ga_id}"
    if api_secret:
        url += f"&api_secret={api_secret}"

    client_id = (
        hashlib.sha256(f"tz_user_{user_id}".encode()).hexdigest()[:16]
        if user_id
        else f"tz_book_{booking_id}"
    )

    payload = {
        "client_id": client_id,
        "events": [
            {
                "name": "booking_completed",
                "params": {
                    "booking_id": int(booking_id),
                    "salon_id": int(salon_id),
                    "value": float(value or 0),
                    "currency": "INR",
                },
            }
        ],
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception as exc:
        logger.warning("Failed to send GA4 booking_completed event: %s", exc)
