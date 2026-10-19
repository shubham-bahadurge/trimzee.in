"""Online payments with Razorpay: plan checkout, signature check, webhook."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.request

import billing
import db
import meta

API = "https://api.razorpay.com/v1"
_ready = False


# ----------------------------------------------------------------- settings
def key_id():
    return os.environ.get("RAZORPAY_KEY_ID", "").strip()


def _secret():
    return os.environ.get("RAZORPAY_KEY_SECRET", "").strip()


def configured():
    return bool(key_id() and _secret())


def test_mode():
    return key_id().startswith("rzp_test_")


# ----------------------------------------------------------------- tables
def init_payments():
    global _ready
    conn = db.get_db()
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS payments (id {meta.ID}, "
                     "user_id INTEGER NOT NULL REFERENCES users(id), purpose TEXT NOT NULL, plan_code TEXT, "
                     "amount INTEGER NOT NULL, coupon_code TEXT, order_id TEXT NOT NULL UNIQUE, payment_id TEXT, "
                     "status TEXT NOT NULL DEFAULT 'created', created_at TEXT NOT NULL, paid_at TEXT)")
        conn.commit()
        _ready = True
    finally:
        conn.close()


def _ensure():
    global _ready
    if _ready:
        return
    billing._ensure()
    conn = db.get_db()
    try:
        try:
            conn.execute("SELECT 1 FROM payments LIMIT 1")
            _ready = True
            return
        except Exception:
            conn.rollback()
    finally:
        conn.close()
    init_payments()


def _post(path, payload):
    """One call to the Razorpay API (kept separate so tests can replace it)."""
    token = base64.b64encode(f"{key_id()}:{_secret()}".encode()).decode()
    request = urllib.request.Request(API + path, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Basic " + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


# ----------------------------------------------------------------- price + coupon
def discounted(row, price):
    if row["kind"] == "percent":
        return price - (price * row["value"] + 50) // 100   # discount rounded to the nearest rupee
    return max(0, price - row["value"])


def _coupon_error(conn, row, kind, user_id):
    if not row or not row["active"]:
        return "That coupon code isn't valid."
    if row["expires"] and row["expires"] < billing.now_str():
        return "This coupon has expired."
    if row["audience"] not in (kind, "both"):
        return "This coupon is for " + ("salon owners" if row["audience"] == "owner" else "customers") + " only."
    if row["max_uses"] and row["uses"] >= row["max_uses"]:
        return "This coupon has been fully used."
    if row["once_per_user"] and conn.execute("SELECT 1 FROM coupon_redemptions WHERE coupon_id = ? AND user_id = ?",
                                             (row["id"], user_id)).fetchone():
        return "You have already used this coupon."
    return None


def quote(user, plan_code, coupon_code=""):
    if user["role"] != "owner":
        return {"ok": False, "error": "Only salon owners can buy a plan."}
    kind = "owner"
    _ensure()
    conn = db.get_db()
    try:
        plan = conn.execute("SELECT code, kind, name, price, days FROM plans WHERE code = ? AND active = 1",
                            (plan_code,)).fetchone()
        if not plan or plan["kind"] != kind:
            return {"ok": False, "error": "Please choose a plan."}
        price, code, discount, final = plan["price"], billing.normalize_code(coupon_code), 0, plan["price"]
        if code:
            row = conn.execute("SELECT * FROM coupons WHERE code = ?", (code,)).fetchone()
            error = _coupon_error(conn, row, kind, user["id"])
            if error:
                return {"ok": False, "error": error}
            if row["kind"] == "free_days":
                return {"ok": False, "error": "This code gives free days. Use the Redeem free days button next to the code."}
            final = discounted(row, price)
            discount = price - final
        return {"ok": True, "plan": dict(plan), "price": price, "discount": discount, "final": final,
                "coupon": code or None, "kind": kind}
    finally:
        conn.close()


# ----------------------------------------------------------------- activation
def _activate(conn, pay, payment_id, record_coupon=True):
    """Mark a payment paid and give the plan days. Returns True only the first time."""
    cur = conn.execute("UPDATE payments SET status = 'paid', payment_id = ?, paid_at = ? "
                       "WHERE order_id = ? AND status != 'paid'", (payment_id, billing.now_str(), pay["order_id"]))
    if not cur.rowcount:
        conn.rollback()
        return False
    plan = conn.execute("SELECT code, kind, days FROM plans WHERE code = ?", (pay["plan_code"],)).fetchone()
    if not plan or plan["kind"] != "owner":
        conn.rollback()
        return False
    coupon = pay["coupon_code"]
    if coupon and record_coupon:
        row = conn.execute("SELECT id, once_per_user FROM coupons WHERE code = ?", (coupon,)).fetchone()
        if row:
            try:
                conn.execute("UPDATE coupons SET uses = uses + 1 WHERE id = ?", (row["id"],))
                conn.execute("INSERT INTO coupon_redemptions (coupon_id, user_id, per_user_key, detail, redeemed_at) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (row["id"], pay["user_id"], f"{row['id']}:{pay['user_id']}" if row["once_per_user"] else None,
                              "used when paying", billing.now_str()))
            except db.sqlite3.IntegrityError:  # used twice at the same moment: still honour what was paid
                conn.rollback()
                return _activate(conn, pay, payment_id, record_coupon=False)
    billing.add_days(conn, pay["user_id"], plan["kind"], plan["days"], plan["code"], "payment",
                     coupon_code=coupon, note=f"paid Rs {pay['amount'] / 100:.0f}")
    conn.commit()
    return True


def create_order(user, plan_code, coupon_code=""):
    q = quote(user, plan_code, coupon_code)
    if not q["ok"]:
        return q
    conn = db.get_db()
    try:
        now = billing.now_str()
        if q["final"] == 0:  # a 100% coupon: no payment needed
            order_id = "free-" + secrets.token_hex(8)
            conn.execute("INSERT INTO payments (user_id, purpose, plan_code, amount, coupon_code, order_id, status, "
                         "created_at) VALUES (?, 'plan', ?, 0, ?, ?, 'created', ?)",
                         (user["id"], plan_code, q["coupon"], order_id, now))
            conn.commit()
            pay = conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()
            _activate(conn, pay, "free")
            ends = billing.status(user["id"], q["kind"])["ends_pretty"]
            return {"ok": True, "free": True, "message": f"Your plan is active until {ends}. No payment was needed."}
        if not configured():
            return {"ok": False, "error": "Online payments are not set up yet."}
        try:
            order = _post("/orders", {"amount": q["final"] * 100, "currency": "INR",
                                      "receipt": f"trz{user['id']}-{secrets.token_hex(4)}",
                                      "notes": {"user_id": str(user["id"]), "plan": plan_code, "coupon": q["coupon"] or ""}})
            order_id = order["id"]
        except Exception:
            return {"ok": False, "error": "Could not start the payment. Please try again in a moment."}
        conn.execute("INSERT INTO payments (user_id, purpose, plan_code, amount, coupon_code, order_id, status, created_at) "
                     "VALUES (?, 'plan', ?, ?, ?, ?, 'created', ?)",
                     (user["id"], plan_code, q["final"] * 100, q["coupon"], order_id, now))
        conn.commit()
        return {"ok": True, "free": False, "order_id": order_id, "amount_paise": q["final"] * 100, "key_id": key_id(),
                "quote": q, "test_mode": test_mode()}
    finally:
        conn.close()


def _signature_ok(message, signature, secret):
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


def confirm(user_id, order_id, payment_id, signature):
    """Called when the customer comes back from the Razorpay window. Returns (ok, message)."""
    if not (order_id and payment_id and signature) or not configured():
        return False, "Payment could not be verified."
    _ensure()
    conn = db.get_db()
    try:
        pay = conn.execute("SELECT p.*, u.role AS user_role FROM payments p JOIN users u ON u.id = p.user_id "
                           "WHERE p.order_id = ? AND p.user_id = ?", (order_id, user_id)).fetchone()
        if not pay or pay["user_role"] != "owner":
            return False, "Payment could not be verified."
        plan = conn.execute("SELECT kind FROM plans WHERE code = ?", (pay["plan_code"],)).fetchone()
        if not plan or plan["kind"] != "owner":
            return False, "Payment could not be verified."
        if not _signature_ok(f"{order_id}|{payment_id}".encode(), signature, _secret()):
            return False, "Payment could not be verified. If money was deducted, it will be matched automatically."
        _activate(conn, pay, payment_id)   # False here just means the webhook was faster: the plan is active
    finally:
        conn.close()
    ends = billing.status(user_id, "owner")["ends_pretty"]
    return True, f"Payment received. Your plan is active until {ends}."


def webhook(raw_body, signature):
    """Razorpay calls this when a payment succeeds, even if the customer closed the page. Returns (status, text)."""
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not secret:
        return 503, "webhook not configured"
    if not _signature_ok(raw_body, signature, secret):
        return 400, "bad signature"
    try:
        data = json.loads(raw_body.decode("utf-8"))
        if data.get("event") not in ("payment.captured", "order.paid"):
            return 200, "ignored"
        entity = data["payload"]["payment"]["entity"]
        order_id, payment_id = entity["order_id"], entity["id"]
    except Exception:
        return 200, "ignored"
    _ensure()
    conn = db.get_db()
    try:
        pay = conn.execute("SELECT p.*, u.role AS user_role FROM payments p JOIN users u ON u.id = p.user_id "
                           "WHERE p.order_id = ?", (order_id,)).fetchone()
        if pay and pay["user_role"] == "owner":
            _activate(conn, pay, payment_id)
    finally:
        conn.close()
    return 200, "ok"


def admin_context():
    _ensure()
    conn = db.get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT p.id, p.created_at, p.paid_at, p.amount, p.status, p.plan_code, p.coupon_code, p.order_id, "
            "p.payment_id, u.email FROM payments p JOIN users u ON u.id = p.user_id ORDER BY p.id DESC LIMIT 100")]
        paid = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid'").fetchone()[0]
    finally:
        conn.close()
    for r in rows:
        r["rupees"] = f"{r['amount'] / 100:.0f}"
    return {"payments": rows, "total": f"{paid / 100:.0f}", "test_mode": test_mode(), "configured": configured()}
