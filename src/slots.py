"""Live slots for today and tomorrow, bookings with a 4-digit code, live queue and salon status."""
import hmac
import re
import secrets
import time
from datetime import timedelta

import accounts
import db
import growth
import mailer
import meta
import billing

DAYS = 2
MAX_CODE_TRIES = 5


# ----------------------------- time helpers --------------------------------
def _now():
    return db.now_ist()


def day_strings():
    n = _now()
    return [(n + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS)]


def slots_of(day, opening_time="10:00", closing_time="20:00"):
    start_hour, start_minute = map(int, opening_time.split(":"))
    end_hour, end_minute = map(int, closing_time.split(":"))
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    return [f"{day} {minute // 60:02d}:{minute % 60:02d}"
            for minute in range(start, end, 30)]


def window_slots(opening_time="10:00", closing_time="20:00"):
    return [s for d in day_strings() for s in slots_of(d, opening_time, closing_time)]


def day_tabs():
    return [{"value": d, "label": "Today" if i == 0 else "Tomorrow"} for i, d in enumerate(day_strings())]


def when_label(slot):
    today = _now().strftime("%Y-%m-%d")
    tomorrow = (_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    word = "Today" if slot[:10] == today else "Tomorrow" if slot[:10] == tomorrow else slot[:10]
    return f"{word} {meta._pretty(slot)}"


def init_slots():
    conn = db.get_db()
    try:
        meta._add_column(conn, "bookings", "code", "TEXT")
        meta._add_column(conn, "bookings", "code_tries", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("DROP TABLE IF EXISTS slot_holds")
        conn.commit()
    finally:
        conn.close()


# ----------------------------- live status ---------------------------------
def _taken(conn):
    """{salon_id: set(slot)} of slots with no free barber left (pending/confirmed bookings from today on)."""
    start = _now().strftime("%Y-%m-%d") + " 00:00"
    counts = {}
    for r in conn.execute("SELECT salon_id, slot_start, COUNT(*) AS n FROM bookings WHERE status IN ('pending', 'confirmed') "
                          "AND slot_start >= ? GROUP BY salon_id, slot_start", (start,)):
        counts[(r["salon_id"], r["slot_start"])] = r["n"]
    capacity = billing.capacities(conn)
    out = {}
    for (salon_id, slot), n in counts.items():
        if n >= capacity.get(salon_id, 1):
            out.setdefault(salon_id, set()).add(slot)
    return out


def live_status(stored, taken, opening_time="10:00", closing_time="20:00", next_opening_time=None):
    """(status, next_label). The owner's switch decides ON/OFF; busy / fully booked are worked out live.
    An enabled salon opens and closes automatically at its configured hours."""
    if stored == "closed":
        return "closed", "Switched off"
    hm = _now().strftime("%Y-%m-%d %H:%M")
    today = hm[:10]
    current_time = hm[11:]
    if current_time < opening_time:
        return "closed", "Opens at " + meta._pretty(today + " " + opening_time)
    if current_time >= closing_time:
        tomorrow = (_now() + timedelta(days=1)).strftime("%Y-%m-%d")
        next_opening_time = next_opening_time or opening_time
        return "closed", "Opens tomorrow " + meta._pretty(tomorrow + " " + next_opening_time)
    free = [t for t in window_slots(opening_time, closing_time) if t > hm and t not in taken]
    if not free:
        return "full", "Fully booked"
    busy = False
    current = [t for t in slots_of(today, opening_time, closing_time) if t <= hm]
    busy = bool(current) and current[-1] in taken      # the slot running right now is booked
    return ("busy" if busy else "available"), when_label(free[0]) if free[0][:10] != today else meta._pretty(free[0])


def salon_live(salon_id):
    conn = db.get_db()
    try:
        row = conn.execute("SELECT status, opening_time, closing_time, early_open_date, early_open_time "
                           "FROM salons WHERE id = ?",
                           (salon_id,)).fetchone()
        if not row:
            return "closed", ""
        opening_time, closing_time = db.salon_hours(row)
        return live_status(row["status"], _taken(conn).get(salon_id, set()),
                           opening_time, closing_time, row["opening_time"])
    finally:
        conn.close()


def get_salons(user_gender=None):
    """Home-page list in 3 queries (salons, all service names, all taken slots)."""
    conn = db.get_db()
    try:
        extra, params = "", ()
        if billing.enforce():  # salons whose free month / plan ended are hidden
            billing.grant_pending_trials(conn)
            extra = " AND o.id IN (SELECT user_id FROM subscriptions WHERE kind = 'owner' AND ends > ?)"
            params = (billing.now_str(),)
        rows = conn.execute(
            "SELECT s.id, s.name, s.distance_km, s.rating, s.status, s.gender, s.lat, s.lng, "
            "s.image_mime, s.opening_time, s.closing_time, s.early_open_date, s.early_open_time, "
            "MIN(sv.price) AS price "
            "FROM salons s JOIN users o ON o.id = s.owner_id JOIN services sv ON sv.salon_id = s.id "
            "WHERE s.approved = 1 AND o.is_active = 1" + extra + " GROUP BY s.id ORDER BY s.distance_km", params).fetchall()
        names = {}
        for r in conn.execute("SELECT salon_id, name FROM services ORDER BY id"):
            names.setdefault(r["salon_id"], []).append(r["name"])
        taken = _taken(conn)
    finally:
        conn.close()
    want = {"male": "men", "female": "women"}.get(user_gender)
    out = []
    for r in rows:
        if want and r["gender"] not in (want, "unisex"):
            continue
        opening_time, closing_time = db.salon_hours(r)
        status, nxt = live_status(r["status"], taken.get(r["id"], set()),
                                  opening_time, closing_time, r["opening_time"])
        out.append({"id": r["id"], "name": r["name"], "rating": r["rating"], "distance": r["distance_km"],
                    "price": r["price"], "status": status, "next": nxt, "services": names.get(r["id"], []),
                    "gender": r["gender"], "lat": r["lat"], "lng": r["lng"],
                    "image_url": f"/salons/{r['id']}/image" if r["image_mime"] else None})
    return out


def get_salon(salon_id):
    import extras
    data = extras.get_salon(salon_id)
    if data:
        data["salon"]["status"] = salon_live(salon_id)[0]
    return data


def get_barbers(salon_id):
    conn = db.get_db()
    try:
        barber_ids = billing.active_barbers(conn, salon_id)
        if not barber_ids:
            return []
        placeholders = ",".join("?" for _ in barber_ids)
        rows = conn.execute(
            f"SELECT id, name FROM barbers WHERE salon_id = ? AND active = 1 AND id IN ({placeholders}) "
            "ORDER BY id", (salon_id, *barber_ids)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ----------------------------- slots ----------------------------------------
def _states(conn, salon_id, day, is_open, opening_time, closing_time, barber_id=None):
    """One day of slots. A slot is booked once every working barber is taken."""
    barbers = billing.barber_count(conn, salon_id)
    if barber_id is None:
        booked = {r["slot_start"]: r["n"] for r in conn.execute(
            "SELECT slot_start, COUNT(*) AS n FROM bookings WHERE salon_id = ? "
            "AND status IN ('pending', 'confirmed') AND slot_start LIKE ? GROUP BY slot_start",
            (salon_id, day + "%"))}
    else:
        booked = {r["slot_start"]: r["n"] for r in conn.execute(
            "SELECT slot_start, COUNT(*) AS n FROM bookings WHERE salon_id = ? AND barber_id = ? "
            "AND status IN ('pending', 'confirmed') AND slot_start LIKE ? GROUP BY slot_start",
            (salon_id, barber_id, day + "%"))}
    hm = _now().strftime("%Y-%m-%d %H:%M")
    out = []
    for t in slots_of(day, opening_time, closing_time):
        used = booked.get(t, 0)
        if not is_open:
            state = "closed"
        elif used >= (1 if barber_id is not None else barbers):
            state = "booked"
        elif t <= hm:
            state = "past"
        else:
            state = "free"
        out.append({"slot": t, "time": t[11:], "state": state})
    return out


def get_slots(salon_id, day, viewer_id=None, barber_id=None):
    conn = db.get_db()
    try:
        salon = conn.execute("SELECT status, opening_time, closing_time, early_open_date, early_open_time FROM salons s "
                             "WHERE s.id = ? AND s.approved = 1", (salon_id,)).fetchone()
        if not salon:
            return None
        active_barbers = billing.active_barbers(conn, salon_id)
        if barber_id is not None and barber_id not in active_barbers:
            return {"error": "This barber is no longer available."}
        today = day_strings()[0]
        if day != today:
            day = today
        now = _now()
        now_hm = now.strftime("%H:%M")
        opening_time, closing_time = db.salon_hours(salon, day)
        is_open = (salon["status"] != "closed" and billing.salon_can_book(conn, salon_id)
                   and opening_time <= now_hm < closing_time)
        out = _states(conn, salon_id, day, is_open,
                      opening_time, closing_time, barber_id)
        return {"open": is_open, "day": day, "slots": out, "limited": False, "locked": False}
    finally:
        conn.close()


# ----------------------------- booking -------------------------------------
def book(user_id, salon_id, service_ids, slot, phone, barber_id=None):
    """Returns (http_status, message, booking_id, code). New bookings wait for the owner's Accept."""
    if isinstance(service_ids, (str, int)):
        service_ids = [service_ids]
    try:
        service_ids = list(dict.fromkeys(int(service_id) for service_id in service_ids))
    except (TypeError, ValueError):
        return 400, "Please choose at least one service.", None, None
    if not service_ids:
        return 400, "Please choose at least one service.", None, None
    phone = re.sub(r"\D", "", phone or "")
    if not 10 <= len(phone) <= 12:
        return 400, "Please enter a valid phone number (10-12 digits).", None, None
    conn = db.get_db()
    try:
        salon = conn.execute("SELECT status, opening_time, closing_time, early_open_date, early_open_time FROM salons "
                             "WHERE id = ? AND approved = 1", (salon_id,)).fetchone()
        if not salon:
            return 404, "Salon not found.", None, None
        opening_time, closing_time = db.salon_hours(salon)
        if (not isinstance(slot, str) or slot[:10] != day_strings()[0]
                or slot not in set(window_slots(opening_time, closing_time))):
            return 400, "That time slot is not available.", None, None
        current_time = _now().strftime("%H:%M")
        if not opening_time <= current_time < closing_time:
            return 409, "This salon is currently closed.", None, None
        if slot <= _now().strftime("%Y-%m-%d %H:%M"):
            return 409, "That time has already passed. Pick a later slot.", None, None
        if salon["status"] == "closed" or not billing.salon_can_book(conn, salon_id):
            return 409, "This salon is not taking bookings right now.", None, None
        available_services = {row["id"] for row in conn.execute(
            "SELECT id FROM services WHERE salon_id = ?", (salon_id,)
        )}
        if not set(service_ids).issubset(available_services):
            return 400, "Please choose valid services for this salon.", None, None
        active_barbers = billing.active_barbers(conn, salon_id)
        if barber_id is not None and barber_id not in active_barbers:
            return 400, "The selected barber is no longer available. Please choose another.", None, None
        free_barbers = billing.free_barbers(conn, salon_id, slot)
        if barber_id is not None:
            if barber_id not in free_barbers:
                return 409, "This barber is already booked for that time. Please choose another barber or time.", None, None
            free_barbers = [barber_id]
        elif not free_barbers:
            return 409, "Sorry, someone just booked that slot. Please pick another.", None, None
        code = f"{secrets.randbelow(10000):04d}"
        for assigned_barber_id in free_barbers:
            try:
                booking_id = conn.execute(
                    "INSERT INTO bookings (customer_id, salon_id, service_id, slot_start, status, contact_phone, code, "
                    "barber_id) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                    (user_id, salon_id, service_ids[0], slot, phone, code, assigned_barber_id)).lastrowid
                for service_id in service_ids:
                    conn.execute("INSERT INTO booking_services (booking_id, service_id) VALUES (?, ?)",
                                 (booking_id, service_id))
                conn.commit()
                return 201, "Booking requested.", booking_id, code
            except db.sqlite3.IntegrityError:
                conn.rollback()  # another customer just took this barber: try the next one
        return 409, "Sorry, someone just booked that slot. Please pick another.", None, None
    finally:
        conn.close()


def whatsapp_booking(info, code):
    """Booking messages (only if WhatsApp is set up). The code goes to the customer only."""
    if not info:
        return
    when = when_label(info["slot_start"])
    mailer.send_whatsapp(growth.wa_number(info["contact_phone"]),
                         f"Trimzee: booking request #{info['id']} sent to {info['salon']} for {info['service']} "
                         f"with {info['barber']}, {when}. "
                         f"Your confirmation code is {code}. Show it to the salon when you arrive. "
                         "We'll message you when the salon accepts.")
    mailer.send_whatsapp(growth.wa_number(info["owner_phone"]),
                         f"Trimzee: new booking request from {info['customer']} for {info['service']} "
                         f"with {info['barber']}, {when}. "
                         "Open your dashboard to Accept or Reject.")


def complete_with_code(owner_id, booking_id, code):
    """The owner must type the customer's 4-digit code. Returns (ok, message)."""
    conn = db.get_db()
    try:
        row = conn.execute("SELECT b.status, b.code, b.code_tries, b.customer_id FROM bookings b "
                           "JOIN salons s ON s.id = b.salon_id WHERE b.id = ? AND s.owner_id = ?",
                           (booking_id, owner_id)).fetchone()
        if not row:
            return False, "Booking not found."
        if row["status"] == "pending":
            return False, "Accept this booking first."
        if row["status"] != "confirmed":
            return False, "This booking can't be completed."
        typed = re.sub(r"\D", "", code or "")
        if row["code"]:
            if row["code_tries"] >= MAX_CODE_TRIES:
                return False, "Too many wrong codes for this booking. Please contact Trimzee support."
            if not hmac.compare_digest(typed, row["code"]):
                conn.execute("UPDATE bookings SET code_tries = code_tries + 1 WHERE id = ?", (booking_id,))
                conn.commit()
                left = MAX_CODE_TRIES - row["code_tries"] - 1
                return False, f"Wrong code. {left} {'try' if left == 1 else 'tries'} left."
        conn.execute("UPDATE bookings SET status = 'completed' WHERE id = ? AND status = 'confirmed'", (booking_id,))
        conn.commit()
        customer = row["customer_id"]
    finally:
        conn.close()
    accounts.maybe_reward(customer)
    return True, "Booking completed. Revenue updated."


# ----------------------------- views ---------------------------------------
def bookings_for(salon_id, day):
    conn = db.get_db()
    try:
        rows = conn.execute("SELECT b.id, b.service_id, b.slot_start, b.status, b.contact_phone, u.name AS customer_name, "
                            "sv.name AS service_name, sv.price, br.name AS barber_name "
                            "FROM bookings b JOIN users u ON u.id = b.customer_id "
                            "JOIN services sv ON sv.id = b.service_id LEFT JOIN barbers br ON br.id = b.barber_id "
                            "WHERE b.salon_id = ? AND b.slot_start LIKE ? "
                            "ORDER BY b.slot_start", (salon_id, day + "%")).fetchall()
        out = []
        for row in rows:
            booking = dict(row)
            booking.update(db.booking_service_summary(conn, booking["id"], booking["service_id"]))
            out.append(booking)
        return out
    finally:
        conn.close()


def booking_detail(user_id, booking_id):
    conn = db.get_db()
    try:
        r = conn.execute("SELECT b.id, b.service_id, b.slot_start, b.status, b.code, b.contact_phone, s.id AS salon_id, s.name AS salon, "
                         "s.lat, s.lng, sv.name AS service, sv.price, br.name AS barber, ow.phone AS owner_phone FROM bookings b "
                         "JOIN salons s ON s.id = b.salon_id JOIN services sv ON sv.id = b.service_id "
                         "LEFT JOIN barbers br ON br.id = b.barber_id "
                         "JOIN users ow ON ow.id = s.owner_id WHERE b.id = ? AND b.customer_id = ?",
                         (booking_id, user_id)).fetchone()
        if not r:
            return None
        d = dict(r)
        d.update(db.booking_service_summary(conn, d["id"], d["service_id"]))
        d["service"] = d.pop("service_name")
        d["when"] = when_label(d["slot_start"])
        d["owner_wa"] = growth.wa_number(d.pop("owner_phone"))
        d["can_cancel"] = d["status"] in ("pending", "confirmed") and d["slot_start"] > _now().strftime("%Y-%m-%d %H:%M")
        return d
    finally:
        conn.close()


def maps_link(lat, lng):
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}" if lat is not None and lng is not None else None


def queue_info(salon_id):
    conn = db.get_db()
    try:
        salon = conn.execute("SELECT s.name, s.status, s.gender FROM salons s JOIN users o ON o.id = s.owner_id "
                             "WHERE s.id = ? AND s.approved = 1 AND o.is_active = 1", (salon_id,)).fetchone()
        if not salon:
            return None
        taken = _taken(conn).get(salon_id, set())
        now = _now()
        today = now.strftime("%Y-%m-%d")
        cutoff = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
        rows = conn.execute("SELECT slot_start, status FROM bookings WHERE salon_id = ? AND slot_start LIKE ? "
                            "AND status IN ('pending', 'confirmed')", (salon_id, today + "%")).fetchall()
        ahead = sum(1 for r in rows if r["slot_start"] > cutoff)
        hm = now.strftime("%Y-%m-%d %H:%M")
        serving = any(r["status"] == "confirmed" and r["slot_start"] <= hm and
                      (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M") < r["slot_start"] for r in rows)
        status, nxt = live_status(salon["status"], taken)
        return {"name": salon["name"], "status": status, "next_free": nxt, "ahead": ahead, "serving": serving,
                "gender": salon["gender"]}
    finally:
        conn.close()


def user_bookings(user_id):
    """The customer's bookings with their confirmation code, cancel/review flags and rating."""
    conn = db.get_db()
    try:
        now = _now().strftime("%Y-%m-%d %H:%M")
        rows = conn.execute(
            "SELECT b.id, b.service_id, b.slot_start, b.status, b.code, s.name AS salon_name, sv.name AS service_name, sv.price, "
            "br.name AS barber_name, "
            "r.rating AS my_rating FROM bookings b JOIN salons s ON s.id = b.salon_id "
            "JOIN services sv ON sv.id = b.service_id LEFT JOIN barbers br ON br.id = b.barber_id "
            "LEFT JOIN reviews r ON r.booking_id = b.id "
            "WHERE b.customer_id = ? ORDER BY b.slot_start DESC", (user_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.update(db.booking_service_summary(conn, d["id"], d["service_id"]))
            d["when"] = when_label(d["slot_start"])
            d["can_cancel"] = d["status"] in ("pending", "confirmed") and d["slot_start"] > now
            d["can_review"] = d["status"] == "completed" and d["my_rating"] is None
            d["show_code"] = d["status"] in ("pending", "confirmed") and bool(d["code"])
            out.append(d)
        return out
    finally:
        conn.close()
