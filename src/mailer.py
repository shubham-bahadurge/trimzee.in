"""Outgoing messages: e-mail (SMTP) for sign-up codes, WhatsApp (Twilio) for booking messages.
Both stay switched off until their settings exist in the project, and never crash the app."""
import base64
import os
import smtplib
import ssl
import urllib.parse
import urllib.request
from email.message import EmailMessage


def email_configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))


def send_email(to, subject, body):
    """Raises on failure, so the caller can show a friendly message."""
    host, user, password = os.environ["SMTP_HOST"], os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    sender = os.environ.get("SMTP_FROM") or user
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = f"Trimzee <{sender}>", to, subject
    msg.set_content(body)
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=10, context=context) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg)


def whatsapp_configured():
    return bool(os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN")
                and os.environ.get("TWILIO_WHATSAPP_FROM"))


def send_whatsapp(number, text):
    """number: digits with country code, e.g. 919876543210. Returns True if sent; never raises."""
    if not number or not whatsapp_configured():
        return False
    sid, token = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    sender = os.environ["TWILIO_WHATSAPP_FROM"].replace("whatsapp:", "").strip()
    data = urllib.parse.urlencode({"From": f"whatsapp:{sender}", "To": f"whatsapp:+{number}", "Body": text[:1500]}).encode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False
