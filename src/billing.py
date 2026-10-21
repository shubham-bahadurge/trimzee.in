"""Plans, barbers, coupons and subscription checks for Trimzee."""

import html
import os
import secrets
from datetime import datetime, timedelta

import db
import meta

try:
    import pgdb
except Exception:  # running without the Postgres layer
    pgdb = None

FMT = "%Y-%m-%d %H:%M"
TRIAL_DAYS = 30
MAX_BARBERS = 12
ACTIVE_SQL = "status IN ('pending', 'confirmed')"
PLAN_DEFAULTS = [
    ("owner_month", "owner", "Salon plan", 299, 30),
]
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_ready = False


# ----------------------------------------------------------------- basics
def enforce():
    """Plans only limit anything when BILLING_ENFORCE=1 is set in Vercel."""
    return os.environ.get("BILLING_ENFORCE", "").strip() == "1"


def now_str():
    return db.now_ist().strftime(FMT)


def pretty_date(text):
    try:
        return datetime.strptime(text, FMT).strftime("%d %b %Y").lstrip("0")
    except Exception:
        return text or ""


def _period(days):
    return {30: "1 month", 90: "3 months", 365: "1 year"}.get(days, f"{days} days")


def _ensure():
    """Tables are made at start-up; this only repairs a process that started before they existed."""
    global _ready
    if _ready:
        return
    conn = db.get_db()
    try:
        try:
            conn.execute("SELECT 1 FROM plans LIMIT 1")
            conn.execute("SELECT barber_id FROM bookings LIMIT 1")
            _ready = True
            return
        except Exception:
            conn.rollback()
    finally:
        conn.close()
    init_billing()


def _drop_unique(conn, table, columns):
    """Drop any UNIQUE index on exactly these columns (the old one-booking-per-slot rules)."""
    pattern = f"%({columns})%"
    if pgdb is not None and getattr(pgdb, "ENABLED", False):
        rows = conn.execute("SELECT indexname AS name FROM pg_indexes WHERE tablename = ? "
                            "AND indexdef LIKE ? AND indexdef LIKE ?", (table, "%UNIQUE%", pattern)).fetchall()
    else:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
                            "AND sql LIKE ? AND sql LIKE ?", (table, "%UNIQUE%", pattern)).fetchall()
    for r in rows:
        try:
            conn.execute(f'DROP INDEX IF EXISTS "{r["name"]}"')
            conn.commit()
        except Exception:  # e.g. the rule is a table constraint: leave it, the app keeps working
            conn.rollback()


def init_billing():
    global _ready
    conn = db.get_db()
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS barbers (id {meta.ID}, "
                     "salon_id INTEGER NOT NULL REFERENCES salons(id) ON DELETE CASCADE, "
                     "name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS plans (id {meta.ID}, code TEXT NOT NULL UNIQUE, "
                     "kind TEXT NOT NULL, name TEXT NOT NULL, price INTEGER NOT NULL, days INTEGER NOT NULL, "
                     "active INTEGER NOT NULL DEFAULT 1)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS subscriptions (id {meta.ID}, "
                     "user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL, plan_code TEXT, "
                     "source TEXT NOT NULL, starts TEXT NOT NULL, ends TEXT NOT NULL, coupon_code TEXT, "
                     "note TEXT, created_at TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS subscriptions_by_user ON subscriptions (user_id, kind)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS coupons (id {meta.ID}, code TEXT NOT NULL UNIQUE, "
                     "audience TEXT NOT NULL, kind TEXT NOT NULL, value INTEGER NOT NULL, "
                     "max_uses INTEGER NOT NULL DEFAULT 0, uses INTEGER NOT NULL DEFAULT 0, "
                     "once_per_user INTEGER NOT NULL DEFAULT 1, expires TEXT, active INTEGER NOT NULL DEFAULT 1, "
                     "note TEXT, created_at TEXT NOT NULL)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS coupon_redemptions (id {meta.ID}, "
                     "coupon_id INTEGER NOT NULL REFERENCES coupons(id), "
                     "user_id INTEGER NOT NULL REFERENCES users(id), per_user_key TEXT, detail TEXT, "
                     "redeemed_at TEXT NOT NULL)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_redemption_per_user "
                     "ON coupon_redemptions (per_user_key)")
        conn.commit()
        meta._add_column(conn, "bookings", "barber_id", "INTEGER")
        for code, kind, name, price, days in PLAN_DEFAULTS:
            if not conn.execute("SELECT 1 FROM plans WHERE code = ?", (code,)).fetchone():
                conn.execute("INSERT INTO plans (code, kind, name, price, days, active) VALUES (?, ?, ?, ?, ?, 1)",
                             (code, kind, name, price, days))
        conn.execute("UPDATE plans SET active = 0 WHERE kind = 'customer'")
        for r in conn.execute("SELECT s.id FROM salons s WHERE NOT EXISTS "
                              "(SELECT 1 FROM barbers b WHERE b.salon_id = s.id)").fetchall():
            conn.execute("INSERT INTO barbers (salon_id, name, active, created_at) VALUES (?, 'Barber 1', 1, ?)",
                         (r["id"], now_str()))
        conn.execute("UPDATE bookings SET barber_id = (SELECT MIN(b.id) FROM barbers b "
                     "WHERE b.salon_id = bookings.salon_id) WHERE barber_id IS NULL")
        conn.commit()
        # A slot may now take one booking per barber, so the old one-per-slot rules must go.
        _drop_unique(conn, "bookings", "salon_id, slot_start")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_barber_slot "
                     "ON bookings (salon_id, slot_start, barber_id) WHERE status IN ('pending', 'confirmed')")
        conn.commit()
        _ready = True
    finally:
        conn.close()


# ----------------------------------------------------------------- barbers
def active_barbers(conn, salon_id):
    """Ids of the salon's working barbers (a new salon gets 'Barber 1' automatically)."""
    _ensure()
    if conn.execute("SELECT COUNT(*) FROM barbers WHERE salon_id = ?", (salon_id,)).fetchone()[0] == 0:
        conn.execute("INSERT INTO barbers (salon_id, name, active, created_at) VALUES (?, 'Barber 1', 1, ?)",
                     (salon_id, now_str()))
        conn.commit()
    ids = [r["id"] for r in conn.execute("SELECT id FROM barbers WHERE salon_id = ? AND active = 1 ORDER BY id",
                                         (salon_id,))]
    if not ids:  # never leave a salon without a barber
        first = conn.execute("SELECT MIN(id) FROM barbers WHERE salon_id = ?", (salon_id,)).fetchone()[0]
        conn.execute("UPDATE barbers SET active = 1 WHERE id = ?", (first,))
        conn.commit()
        ids = [first]
    return ids


def barber_count(conn, salon_id):
    return len(active_barbers(conn, salon_id))


def capacities(conn):
    """{salon_id: number of working barbers}; salons without a row count as 1."""
    _ensure()
    return {r["salon_id"]: r["n"] for r in conn.execute(
        "SELECT salon_id, COUNT(*) AS n FROM barbers WHERE active = 1 GROUP BY salon_id")}


def active_bookings(conn, salon_id, day):
    """Active bookings on a day, including the duration of all services in each booking."""
    return [dict(row) for row in conn.execute(
        "SELECT b.id, b.barber_id, b.slot_start, b.duration_min FROM bookings b "
        "WHERE b.salon_id = ? AND b.slot_start LIKE ? AND " + ACTIVE_SQL,
        (salon_id, day + "%"),
    )]


def free_barbers(conn, salon_id, slot, duration_min=30, bookings=None):
    """Barbers free for the whole requested interval, least busy that day first."""
    ids = active_barbers(conn, salon_id)
    bookings = bookings if bookings is not None else active_bookings(conn, salon_id, slot[:10])
    start = datetime.strptime(slot, FMT)
    end = start + timedelta(minutes=duration_min)
    load = {}
    used = set()
    for booking in bookings:
        barber_id = booking["barber_id"]
        load[barber_id] = load.get(barber_id, 0) + 1
        booked_start = datetime.strptime(booking["slot_start"], FMT)
        booked_end = booked_start + timedelta(minutes=booking["duration_min"])
        if start < booked_end and booked_start < end:
            used.add(barber_id)
    return sorted((i for i in ids if i not in used), key=lambda i: (load.get(i, 0), i))


def owner_salon_id(owner_id):
    conn = db.get_db()
    try:
        row = conn.execute("SELECT id FROM salons WHERE owner_id = ? ORDER BY id LIMIT 1", (owner_id,)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def _clean_name(name):
    name = " ".join((name or "").split())
    return name if 2 <= len(name) <= 40 else None


def add_barber(salon_id, name):
    name = _clean_name(name)
    if not name:
        return False, "Please enter the barber's name (2-40 characters)."
    conn = db.get_db()
    try:
        active_barbers(conn, salon_id)
        if conn.execute("SELECT COUNT(*) FROM barbers WHERE salon_id = ? AND active = 1", (salon_id,)).fetchone()[0] >= MAX_BARBERS:
            return False, f"You can have up to {MAX_BARBERS} barbers."
        conn.execute("INSERT INTO barbers (salon_id, name, active, created_at) VALUES (?, ?, 1, ?)",
                     (salon_id, name, now_str()))
        conn.commit()
        return True, f"{name} added. Customers can now book one more person per time slot."
    finally:
        conn.close()


def set_barber_active(salon_id, barber_id, active):
    conn = db.get_db()
    try:
        row = conn.execute("SELECT name, active FROM barbers WHERE id = ? AND salon_id = ?",
                           (barber_id, salon_id)).fetchone()
        if not row:
            return False, "Barber not found."
        if active:
            if row["active"]:
                return True, "Already working."
            if conn.execute("SELECT COUNT(*) FROM barbers WHERE salon_id = ? AND active = 1", (salon_id,)).fetchone()[0] >= MAX_BARBERS:
                return False, f"You can have up to {MAX_BARBERS} barbers."
        elif row["active"] and conn.execute("SELECT COUNT(*) FROM barbers WHERE salon_id = ? AND active = 1",
                                            (salon_id,)).fetchone()[0] <= 1:
            return False, "A salon needs at least one working barber."
        conn.execute("UPDATE barbers SET active = ? WHERE id = ? AND salon_id = ?",
                     (1 if active else 0, barber_id, salon_id))
        conn.commit()
        if active:
            return True, f"{row['name']} is working again."
        return True, f"{row['name']} is off. Their existing bookings stay; new bookings use the other barbers."
    finally:
        conn.close()


def rename_barber(salon_id, barber_id, name):
    name = _clean_name(name)
    if not name:
        return False, "Please enter a name (2-40 characters)."
    conn = db.get_db()
    try:
        cur = conn.execute("UPDATE barbers SET name = ? WHERE id = ? AND salon_id = ?", (name, barber_id, salon_id))
        conn.commit()
        return (True, "Name saved.") if cur.rowcount else (False, "Barber not found.")
    finally:
        conn.close()


def barbers_context(owner_id):
    sid = owner_salon_id(owner_id)
    if not sid:
        return None
    conn = db.get_db()
    try:
        active_barbers(conn, sid)
        salon = conn.execute("SELECT name FROM salons WHERE id = ?", (sid,)).fetchone()
        barbers = [dict(r) for r in conn.execute("SELECT id, name, active FROM barbers WHERE salon_id = ? "
                                                 "ORDER BY active DESC, id", (sid,))]
        day = db.now_ist().strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT b.id, b.service_id, b.slot_start, b.status, b.barber_id, u.name AS customer, sv.name AS service FROM bookings b "
            "JOIN users u ON u.id = b.customer_id JOIN services sv ON sv.id = b.service_id "
            "WHERE b.salon_id = ? AND b.slot_start LIKE ? AND b.status IN ('pending', 'confirmed', 'completed') "
            "ORDER BY b.slot_start", (sid, day + "%")).fetchall()
        rows = [dict(row) for row in rows]
        for row in rows:
            row["service"] = db.booking_service_summary(conn, row["id"], row["service_id"])["service_name"]
    finally:
        conn.close()
    by_barber = {}
    for r in rows:
        by_barber.setdefault(r["barber_id"], []).append({"time": r["slot_start"][11:], "status": r["status"],
                                                        "customer": r["customer"], "service": r["service"]})
    for b in barbers:
        b["today"] = by_barber.get(b["id"], [])
    return {"salon": salon["name"], "barbers": barbers, "working": sum(1 for b in barbers if b["active"]),
            "status": status(owner_id, "owner"), "enforce": enforce()}


# ----------------------------------------------------------------- subscriptions
def _latest_end(conn, user_id, kind):
    row = conn.execute("SELECT MAX(ends) AS e FROM subscriptions WHERE user_id = ? AND kind = ?",
                       (user_id, kind)).fetchone()
    return row["e"] if row and row["e"] else None


def add_days(conn, user_id, kind, days, plan_code, source, coupon_code=None, note=None):
    """Extend a subscription: new days are added after whatever is still running. Caller commits."""
    now = now_str()
    current = _latest_end(conn, user_id, kind)
    start = current if current and current > now else now
    ends = (datetime.strptime(start, FMT) + timedelta(days=int(days))).strftime(FMT)
    conn.execute("INSERT INTO subscriptions (user_id, kind, plan_code, source, starts, ends, coupon_code, note, "
                 "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (user_id, kind, plan_code, source, start, ends, coupon_code, note, now))
    return ends


def status(user_id, kind):
    _ensure()
    conn = db.get_db()
    try:
        end = _latest_end(conn, user_id, kind)
        last = conn.execute("SELECT source FROM subscriptions WHERE user_id = ? AND kind = ? "
                            "ORDER BY ends DESC, id DESC LIMIT 1", (user_id, kind)).fetchone()
    finally:
        conn.close()
    now = now_str()
    active = bool(end and end > now)
    left = 0
    if active:
        seconds = (datetime.strptime(end, FMT) - datetime.strptime(now, FMT)).total_seconds()
        left = max(1, int(-(-seconds // 86400)))
    return {"active": active, "ends": end, "ends_pretty": pretty_date(end), "days_left": left,
            "source": last["source"] if last else None}


def grant_pending_trials(conn):
    """First month free: every approved salon whose owner never had a plan gets a 30-day trial."""
    rows = conn.execute("SELECT DISTINCT s.owner_id FROM salons s WHERE s.approved = 1 AND NOT EXISTS "
                        "(SELECT 1 FROM subscriptions x WHERE x.user_id = s.owner_id AND x.kind = 'owner')").fetchall()
    for r in rows:
        add_days(conn, r["owner_id"], "owner", TRIAL_DAYS, "owner_trial", "trial", note="first month free")
    if rows:
        conn.commit()


def salon_can_book(conn, salon_id):
    """False once the owner's free month / plan has run out (only when BILLING_ENFORCE=1)."""
    if not enforce():
        return True
    _ensure()
    row = conn.execute("SELECT owner_id FROM salons WHERE id = ?", (salon_id,)).fetchone()
    if not row:
        return False
    grant_pending_trials(conn)
    end = _latest_end(conn, row["owner_id"], "owner")
    return bool(end and end > now_str())


# ----------------------------------------------------------------- coupons
def normalize_code(text):
    return "".join(ch for ch in (text or "").upper() if ch.isalnum() or ch in "-_")[:32]


def describe_coupon(row):
    if row["kind"] == "free_days":
        return f"{row['value']} days free"
    if row["kind"] == "percent":
        return f"{row['value']}% off"
    return f"Rs {row['value']} off"


def create_coupon(code, audience, kind, value, max_uses=0, once_per_user=True, expires="", note=""):
    _ensure()
    code = normalize_code(code) or "TRZ-" + "".join(secrets.choice(CODE_CHARS) for _ in range(6))
    if len(code) < 3:
        return False, "The code must be at least 3 characters."
    if audience != "owner":
        return False, "Coupons can only be created for salon owners."
    if kind not in ("free_days", "percent", "flat"):
        return False, "Choose a coupon type."
    try:
        value = int(value)
        max_uses = int(max_uses or 0)
    except (TypeError, ValueError):
        return False, "Value and maximum uses must be whole numbers."
    if value < 1 or (kind == "percent" and value > 100) or (kind == "free_days" and value > 3650) or value > 100000:
        return False, "That value is not allowed."
    if max_uses < 0 or max_uses > 1000000:
        return False, "Maximum uses must be 0 (unlimited) or more."
    expires = (expires or "").strip()
    if expires:
        try:
            expires = datetime.strptime(expires, "%Y-%m-%d").strftime("%Y-%m-%d") + " 23:59"
        except ValueError:
            return False, "Expiry must look like 2026-12-31."
    conn = db.get_db()
    try:
        try:
            conn.execute("INSERT INTO coupons (code, audience, kind, value, max_uses, uses, once_per_user, expires, "
                         "active, note, created_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 1, ?, ?)",
                         (code, audience, kind, value, max_uses, 1 if once_per_user else 0, expires or None,
                          (note or "").strip()[:200] or None, now_str()))
            conn.commit()
        except db.sqlite3.IntegrityError:
            conn.rollback()
            return False, "That coupon code already exists."
        return True, f"Coupon {code} created."
    finally:
        conn.close()


def redeem(user_id, role, code):
    """Returns (ok, message). Only 'free days' coupons are redeemed here; discounts apply when paying."""
    code = normalize_code(code)
    if not code:
        return False, "Please enter a coupon code."
    if role != "owner":
        return False, "Only salon owners can redeem plan coupons."
    kind = "owner"
    _ensure()
    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM coupons WHERE code = ?", (code,)).fetchone()
        if not row or not row["active"]:
            return False, "That coupon code isn't valid."
        if row["expires"] and row["expires"] < now_str():
            return False, "This coupon has expired."
        if row["audience"] not in (kind, "both"):
            return False, "This coupon is for " + ("salon owners" if row["audience"] == "owner" else "customers") + " only."
        if row["kind"] != "free_days":
            return False, (f"This code gives {describe_coupon(row)} when you pay for a plan. "
                           "It will apply at checkout once online payments are live.")
        if row["max_uses"] and row["uses"] >= row["max_uses"]:
            return False, "This coupon has been fully used."
        if row["once_per_user"] and conn.execute("SELECT 1 FROM coupon_redemptions WHERE coupon_id = ? AND user_id = ?",
                                                 (row["id"], user_id)).fetchone():
            return False, "You have already used this coupon."
        cur = conn.execute("UPDATE coupons SET uses = uses + 1 WHERE id = ? AND active = 1 "
                           "AND (max_uses = 0 OR uses < max_uses)", (row["id"],))
        if not cur.rowcount:
            conn.rollback()
            return False, "This coupon has been fully used."
        try:
            conn.execute("INSERT INTO coupon_redemptions (coupon_id, user_id, per_user_key, detail, redeemed_at) "
                         "VALUES (?, ?, ?, ?, ?)", (row["id"], user_id, f"{row['id']}:{user_id}" if row["once_per_user"] else None,
                                                   describe_coupon(row), now_str()))
        except db.sqlite3.IntegrityError:
            conn.rollback()
            return False, "You have already used this coupon."
        ends = add_days(conn, user_id, kind, row["value"], "coupon", "coupon", coupon_code=code)
        conn.commit()
        return True, f"Coupon applied: {row['value']} days free. Active until {pretty_date(ends)}."
    finally:
        conn.close()


def toggle_coupon(coupon_id):
    conn = db.get_db()
    try:
        conn.execute("UPDATE coupons SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END WHERE id = ?", (coupon_id,))
        conn.commit()
    finally:
        conn.close()


def grant(email, days, note=""):
    """Admin: add free days to a salon owner's plan."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return False, "Days must be a whole number."
    if not 1 <= days <= 3650:
        return False, "Days must be between 1 and 3650."
    _ensure()
    conn = db.get_db()
    try:
        user = conn.execute("SELECT id, role, name FROM users WHERE email = ?", ((email or "").strip().lower(),)).fetchone()
        if not user or user["role"] != "owner":
            return False, "No salon owner has that e-mail."
        ends = add_days(conn, user["id"], user["role"], days, "grant", "admin", note=(note or "").strip()[:200] or None)
        conn.commit()
        return True, f"{user['name']} now has access until {pretty_date(ends)}."
    finally:
        conn.close()


def set_plan(code, price, active):
    try:
        price = int(price)
    except (TypeError, ValueError):
        return False, "Price must be a whole number of rupees."
    if not 0 <= price <= 100000:
        return False, "That price is not allowed."
    _ensure()
    conn = db.get_db()
    try:
        cur = conn.execute("UPDATE plans SET price = ?, active = ? WHERE code = ? AND kind = 'owner'",
                           (price, 1 if active else 0, code))
        conn.commit()
        return (True, "Plan saved.") if cur.rowcount else (False, "Plan not found.")
    finally:
        conn.close()


def plans_context(user):
    kind = "owner"
    _ensure()
    conn = db.get_db()
    try:
        plans = [dict(r) for r in conn.execute("SELECT code, name, price, days FROM plans WHERE kind = ? AND active = 1 "
                                               "ORDER BY days", (kind,))]
    finally:
        conn.close()
    for p in plans:
        p["period"] = _period(p["days"])
    return {"kind": kind, "plans": plans, "status": status(user["id"], kind), "enforce": enforce(),
            "is_owner": kind == "owner", "home": "/dashboard" if kind == "owner" else "/"}


def admin_context():
    _ensure()
    conn = db.get_db()
    try:
        plans = [dict(r) for r in conn.execute(
            "SELECT code, kind, name, price, days, active FROM plans WHERE kind = 'owner' ORDER BY days")]
        coupons = []
        for r in conn.execute("SELECT * FROM coupons ORDER BY id DESC LIMIT 100"):
            d = dict(r)
            d["what"] = describe_coupon(r)
            d["limit"] = d["max_uses"] or "no limit"
            coupons.append(d)
        redemptions = [dict(r) for r in conn.execute(
            "SELECT c.code, u.email, r.detail, r.redeemed_at FROM coupon_redemptions r "
            "JOIN coupons c ON c.id = r.coupon_id JOIN users u ON u.id = r.user_id ORDER BY r.id DESC LIMIT 20")]
        now = now_str()
        totals = {"owner": conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM subscriptions WHERE kind = 'owner' AND ends > ?",
            (now,)).fetchone()[0]}
    finally:
        conn.close()
    return {"plans": plans, "coupons": coupons, "redemptions": redemptions, "totals": totals, "enforce": enforce()}


# ----------------------------------------------------------------- page banner
_BAR = ("background:#111;color:#fff;font:14px/1.4 system-ui,-apple-system,sans-serif;padding:9px 14px;"
        "display:flex;gap:6px 16px;flex-wrap:wrap;align-items:center;justify-content:center;text-align:center")
_LINK = "color:#fff;font-weight:600;text-decoration:underline"


def _bar(text, *links):
    parts = "".join(f'<a style="{_LINK}" href="{html.escape(h)}">{html.escape(t)}</a>' for t, h in links)
    return f'<div style="{_BAR}"><span>{html.escape(text)}</span>{parts}</div>'


def banner_html(path, user):
    """A slim bar with the right links on a few pages. Empty string for everything else."""
    if not user:
        return ""
    role, uid = user["role"], user["id"]
    if role == "admin":
        return _bar("Plans, coupons and prices", ("Open billing", "/admin/billing")) if path == "/admin" else ""
    if role == "owner" and path in ("/dashboard", "/dashboard/services"):
        sid = owner_salon_id(uid)
        if not sid:
            return ""
        conn = db.get_db()
        try:
            n = barber_count(conn, sid)
        finally:
            conn.close()
        st = status(uid, "owner")
        if st["active"]:
            plan = f"Plan active until {st['ends_pretty']}"
        elif enforce():
            plan = "Your plan has ended - customers can't book you"
        else:
            plan = "Plans start soon"
        return _bar(f"{n} barber{'s' if n != 1 else ''} working. {plan}.",
                    ("Manage barbers", "/dashboard/barbers"), ("Plan and coupons", "/plans"))
    return ""
