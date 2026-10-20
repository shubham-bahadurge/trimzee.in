"""Flask application: routes connect browser requests to Trimzee's feature modules."""
import os
import secrets
from datetime import timedelta

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template, request,
                   session)

import db
import pgdb

pgdb.install(db)  # uses Postgres when DATABASE_URL exists, otherwise SQLite

import extras  # noqa: E402  (must come after install)
import accounts  # noqa: E402
import boot  # noqa: E402
import chatbot  # noqa: E402
import growth  # noqa: E402
import legal  # noqa: E402
import billing  # noqa: E402
import payments  # noqa: E402
import googleauth  # noqa: E402
import safety  # noqa: E402
import mailer  # noqa: E402
import meta  # noqa: E402
import minipdf  # noqa: E402
import qr  # noqa: E402
import signup  # noqa: E402
import slots  # noqa: E402

# "public" is where Vercel serves static files from.
app = Flask(__name__, static_folder="public", static_url_path="")

secret_key = os.environ.get("SECRET_KEY")
if os.environ.get("VERCEL") and not secret_key:
    raise RuntimeError("Set a stable SECRET_KEY in the Vercel project settings.")

app.config.update(
    SECRET_KEY=secret_key or "dev-only-secret-change-me",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
)

STATUS_LABELS = {"available": "Available", "busy": "Busy", "closed": "Closed", "full": "Fully booked"}

# Create or upgrade the tables only when needed (a stored version number makes cold starts fast).
boot.run(pgdb.init_lock)


# ----------------------------- helpers -------------------------------------
@app.teardown_request
def end_of_request(_):
    pgdb.release()           # hand the shared database connection back clean


def current_user():
    if "user" not in g:
        uid = session.get("user_id")
        g.user = accounts.load_user(uid) if uid else None
        if uid and g.user is None:
            session.pop("user_id", None)
    return g.user


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.context_processor
def inject_user():
    return {"current_user": current_user(), "google_enabled": googleauth.configured()}


@app.template_filter("pretty_time")
def pretty_time(value):
    h, m = int(value[11:13]), value[14:16]
    return f"{(h + 11) % 12 + 1}:{m} {'PM' if h >= 12 else 'AM'}"


@app.before_request
def csrf_protect():
    """Every HTML form POST must carry the secret token from our own pages."""
    if request.method == "POST" and not request.path.startswith("/api/"):
        sent = request.form.get("csrf", "").encode()
        real = session.get("csrf", "").encode()
        if not real or not secrets.compare_digest(sent, real):
            abort(400)


def require_role(role):
    """Returns a redirect/abort response if the visitor isn't allowed, else None."""
    user = current_user()
    if not user:
        return redirect("/login?next=" + request.path)
    if user["role"] != role:
        abort(403)
    return None


def start_session(user_id):
    session.clear()
    session["user_id"] = user_id
    session.permanent = True


def home_for(role):
    return {"owner": "/dashboard", "admin": "/admin"}.get(role, "/")


def client_ip():
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",")[0].strip() or request.remote_addr or "unknown")[:64]


def safe_next(value):
    v = (value or "").strip()
    return v if v.startswith("/") and not v.startswith("//") and "\\" not in v and len(v) < 200 else ""


def public_base():
    env = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.scheme
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.host
    return f"{proto}://{host}"


def viewer_gender():
    user = current_user()
    return user["gender"] if user and user["role"] == "customer" else None


# ----------------------------- public pages --------------------------------
@app.route("/")
def home():
    user = current_user()
    gender = viewer_gender()
    saved_view = request.args.get("view") == "saved"
    return render_template("index.html", salons=slots.get_salons(None if saved_view else gender),
                           labels=STATUS_LABELS, saved_view=saved_view,
                           need_gender=bool(user and user["role"] == "customer" and not gender),
                           my_gender=gender)


@app.route("/shop")
def shop_page():
    user = current_user()
    return render_template("shop.html", on_waitlist=bool(user and growth.on_waitlist(user["id"])))


@app.route("/salon/<int:salon_id>")
def salon_page(salon_id):
    data = slots.get_salon(salon_id)
    if not data:
        abort(404)
    user = current_user()
    if not user:
        return redirect(f"/login?next=/salon/{salon_id}")
    if not meta.gender_allows(salon_id, viewer_gender())[0]:
        abort(404)
    return render_template("salon.html", salon=data["salon"], services=data["services"], labels=STATUS_LABELS,
                           reviews=data["reviews"], review_count=data["review_count"],
                           owner_phone=growth.salon_owner_phone(salon_id), days=slots.day_tabs()[:1],
                           auth=user["role"] if user else "", my_phone=(user or {}).get("phone") or "")


@app.route("/q/<int:salon_id>")
def queue_page(salon_id):
    """The page a salon's QR code opens: service menu + live queue."""
    info = slots.queue_info(salon_id)
    data = db.get_salon(salon_id) if info else None
    if not info or not data:
        abort(404)
    return render_template("queue.html", salon_id=salon_id, info=info, services=data["services"], labels=STATUS_LABELS)


@app.route("/q/<int:salon_id>/qr.svg")
def queue_qr(salon_id):
    if not slots.queue_info(salon_id):
        abort(404)
    return Response(qr.qr_svg(f"{public_base()}/q/{salon_id}"), mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.route("/api/salons/<int:salon_id>/queue")
def api_queue(salon_id):
    info = slots.queue_info(salon_id)
    if not info:
        return jsonify({"error": "Salon not found."}), 404
    return jsonify(info)


# ----------------------------- accounts ------------------------------------
@app.route("/profile/gender", methods=["POST"])
def profile_gender():
    user = current_user()
    if not user:
        return redirect("/login")
    meta.set_user_gender(user["id"], request.form.get("gender", ""))
    return redirect("/")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(home_for(current_user()["role"]))
    if request.method == "GET":
        ref = (request.args.get("ref") or "").strip().upper()
        if ref and accounts.find_referrer(ref):
            session["ref"] = ref
        if safe_next(request.args.get("next")):
            session["next"] = safe_next(request.args.get("next"))
        return render_template("register.html", error=None, form={"ref": session.get("ref", "")})
    form = request.form
    ref = (form.get("ref") or "").strip().upper()
    if not mailer.email_configured():          # no verification possible -> no sign-ups (never skip the OTP)
        return render_template("register.html", error="Sign-up is paused while email verification is being set up. "
                               "Please try again soon.", form=form), 503
    if ref and not accounts.find_referrer(ref):
        return render_template("register.html", error="That referral code isn't valid.", form=form), 400
    mode, payload = signup.begin(form)
    if mode != "otp":
        return render_template("register.html", error=payload if mode == "error" else "Please try again.", form=form), 400
    session["pending_email"] = payload
    session["ref"] = ref
    return redirect("/verify")


def mask_email(email):
    name, _, domain = email.partition("@")
    return (name[:2] + "*" * max(1, len(name) - 2)) + "@" + domain


@app.route("/verify", methods=["GET", "POST"])
def verify():
    email = session.get("pending_email")
    if not email:
        return redirect("/register")
    if request.method == "POST":
        result, value = signup.verify(email, request.form.get("code"))
        if result == "ok":
            ref, nxt = session.get("ref"), session.get("next")
            accounts.ensure_code(value)
            if ref:
                accounts.apply_referral(value, ref)
            start_session(value)
            return redirect(safe_next(nxt) or home_for(accounts.load_user(value)["role"]))
        return render_template("verify.html", email=mask_email(email), error=value, notice=None), 400
    return render_template("verify.html", email=mask_email(email), error=None, notice=None)


@app.route("/verify/resend", methods=["POST"])
def verify_resend():
    email = session.get("pending_email")
    if not email:
        return redirect("/register")
    ok, message = signup.resend(email)
    return render_template("verify.html", email=mask_email(email), error=None if ok else message,
                           notice=message if ok else None), (200 if ok else 429)


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = safe_next(request.values.get("next"))
    if current_user():
        return redirect(nxt or home_for(current_user()["role"]))
    if request.method == "POST":
        email, ip = request.form.get("email", ""), client_ip()
        if extras.too_many_logins(ip, email):
            return render_template("login.html", email=email, next=nxt, error=(
                "Too many failed attempts. Please wait 15 minutes and try again.")), 429
        user = db.authenticate(email, request.form.get("password"))
        if user:
            extras.clear_login_failures(email)
            start_session(user["id"])
            return redirect(nxt or home_for(user["role"]))
        extras.record_login_failure(ip, email)
        return render_template("login.html", error="Incorrect email or password.", email=email, next=nxt), 401
    return render_template("login.html", email="", next=nxt, error=(
        "Password changed. Please sign in with your new password."
        if request.args.get("reset") else None))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if current_user():
        return redirect("/account")
    if request.method == "POST":
        if not safety.hit(client_ip(), "reset", 15):
            return render_template("forgot.html", email=request.form.get("email", ""), error=(
                "Too many requests from this network. Please try again later.")), 429
        session["reset_email"] = safety.reset_begin(request.form.get("email"))
        return redirect("/reset")
    return render_template("forgot.html", error=None, email="")


@app.route("/reset", methods=["GET", "POST"])
def reset_password():
    email = session.get("reset_email")
    if not email:
        return redirect("/forgot")
    if request.method == "POST":
        ok, message = safety.reset_verify(email, request.form.get("code"), request.form.get("password"))
        if ok:
            session.pop("reset_email", None)
            try:
                extras.clear_login_failures(email)
            except Exception:
                pass
            return redirect("/login?reset=1")
        return render_template("reset.html", email=mask_email(email), error=message), 400
    return render_template("reset.html", email=mask_email(email), error=None)


@app.route("/auth/google")
def google_login():
    if current_user():
        return redirect(home_for(current_user()["role"]))
    if not googleauth.configured():
        abort(404)
    ref = (request.args.get("ref") or "").strip().upper()
    if not (ref and accounts.find_referrer(ref)):
        ref = session.get("ref") or ""
    nxt = safe_next(request.args.get("next")) or safe_next(session.get("next"))
    state = secrets.token_urlsafe(24)
    session["g_state"], session["g_next"], session["g_ref"] = state, nxt, ref
    return redirect(googleauth.login_url(public_base() + "/auth/google/callback", state))


@app.route("/auth/google/callback")
def google_callback():
    if not googleauth.configured():
        abort(404)
    state = session.pop("g_state", "")
    nxt, ref = session.pop("g_next", ""), session.pop("g_ref", "")

    def fail(message):
        return render_template("login.html", error=message, email="", next=nxt), 400

    sent = request.args.get("state", "")
    if not state or not sent or not secrets.compare_digest(state, sent):
        return fail("Sign-in expired. Please try again.")
    if request.args.get("error") or not request.args.get("code"):
        return fail("Google sign-in was cancelled.")
    profile, error = googleauth.profile_from_code(
        request.args["code"], public_base() + "/auth/google/callback")
    if error:
        return fail(error)
    conn = db.get_db()
    try:
        row = conn.execute("SELECT id, role, is_active FROM users WHERE email = ?",
                           (profile["email"],)).fetchone()
    finally:
        conn.close()
    if row:
        if not row["is_active"]:
            return fail("This account has been suspended.")
        if row["role"] == "admin":
            return fail("Admin accounts sign in with a password.")
        uid, role = row["id"], row["role"]
    else:
        from werkzeug.security import generate_password_hash
        name = (profile["name"] or profile["email"].split("@")[0]).strip()[:60]
        if len(name) < 2:
            name = "Trimzee user"
        uid, error = growth.create_member({
            "name": name, "email": profile["email"], "role": "customer", "salon_name": "",
            "pw_hash": generate_password_hash(secrets.token_urlsafe(32)), "gender": None, "phone": None})
        if not uid:
            return fail(error or "Could not create your account. Please try again.")
        accounts.ensure_code(uid)
        if ref and accounts.find_referrer(ref):
            accounts.apply_referral(uid, ref)
        role = "customer"
    start_session(uid)
    return redirect(safe_next(nxt) or home_for(role))


def _bill_user(role=None):
    user = current_user()
    if not user:
        return None, redirect("/login?next=" + request.path)
    if role and user["role"] != role:
        abort(403)
    return user, None


def _bill_done(message, error=False, to="/plans"):
    session["bill_flash"] = [message, bool(error)]
    return redirect(to)


def _bill_flash():
    data = session.pop("bill_flash", None)
    return (data[0], bool(data[1])) if data else (None, False)


@app.after_request
def bill_banner(response):
    """Adds a slim bar with the right plan / barber / coupon links to a few pages."""
    try:
        if request.method != "GET" or response.status_code != 200 or response.mimetype != "text/html":
            return response
        if request.path.startswith(("/dashboard", "/admin")):
            return response
        snippet = billing.banner_html(request.path, current_user())
        if not snippet:
            return response
        body = response.get_data(as_text=True)
        start = body.find("<body")
        end = body.find(">", start) if start >= 0 else -1
        if end < 0:
            return response
        response.set_data(body[:end + 1] + snippet + body[end + 1:])
    except Exception:
        pass
    return response


@app.route("/plans")
def bill_plans():
    user, resp = _bill_user()
    if resp:
        return resp
    if user["role"] == "admin":
        return redirect("/admin/billing")
    if user["role"] != "owner":
        return redirect("/")
    notice, error = _bill_flash()
    return render_template("plans.html", notice=notice, error=error, **billing.plans_context(user))


@app.route("/plans/redeem", methods=["POST"])
def bill_redeem():
    user, resp = _bill_user("owner")
    if resp:
        return resp
    if not safety.hit(client_ip(), "coupon", 20):
        return _bill_done("Too many tries from this network. Please wait a while and try again.", True)
    ok, message = billing.redeem(user["id"], user["role"], request.form.get("code"))
    return _bill_done(message, not ok)


def _bill_barber_action(fn, *args):
    user, resp = _bill_user("owner")
    if resp:
        return resp
    salon_id = billing.owner_salon_id(user["id"])
    if not salon_id:
        return _bill_done("You don't have a salon yet.", True, "/dashboard")
    ok, message = fn(salon_id, *args)
    return _bill_done(message, not ok, "/dashboard/barbers")


@app.route("/dashboard/barbers")
def bill_barbers():
    user, resp = _bill_user("owner")
    if resp:
        return resp
    context = billing.barbers_context(user["id"])
    if not context:
        return redirect("/dashboard")
    notice, error = _bill_flash()
    return render_template("barbers.html", notice=notice, error=error, **context)


@app.route("/dashboard/barbers/add", methods=["POST"])
def bill_barber_add():
    return _bill_barber_action(billing.add_barber, request.form.get("name"))


@app.route("/dashboard/barbers/<int:barber_id>/toggle", methods=["POST"])
def bill_barber_toggle(barber_id):
    return _bill_barber_action(billing.set_barber_active, barber_id, request.form.get("active") == "1")


@app.route("/dashboard/barbers/<int:barber_id>/rename", methods=["POST"])
def bill_barber_rename(barber_id):
    return _bill_barber_action(billing.rename_barber, barber_id, request.form.get("name"))


@app.route("/admin/billing")
def bill_admin():
    _, resp = _bill_user("admin")
    if resp:
        return resp
    notice, error = _bill_flash()
    return render_template("admin_billing.html", notice=notice, error=error, **billing.admin_context())


def _bill_admin_done(result):
    ok, message = result
    return _bill_done(message, not ok, "/admin/billing")


@app.route("/admin/billing/plan", methods=["POST"])
def bill_admin_plan():
    _, resp = _bill_user("admin")
    if resp:
        return resp
    return _bill_admin_done(billing.set_plan(request.form.get("code"), request.form.get("price"),
                                             request.form.get("active") == "1"))


@app.route("/admin/billing/coupon", methods=["POST"])
def bill_admin_coupon():
    _, resp = _bill_user("admin")
    if resp:
        return resp
    f = request.form
    return _bill_admin_done(billing.create_coupon(f.get("code"), f.get("audience"), f.get("kind"), f.get("value"),
                                                  f.get("max_uses"), f.get("once_per_user") == "1",
                                                  f.get("expires"), f.get("note")))


@app.route("/admin/billing/coupon/<int:coupon_id>/toggle", methods=["POST"])
def bill_admin_coupon_toggle(coupon_id):
    _, resp = _bill_user("admin")
    if resp:
        return resp
    billing.toggle_coupon(coupon_id)
    return _bill_done("Coupon updated.", False, "/admin/billing")


@app.route("/admin/billing/grant", methods=["POST"])
def bill_admin_grant():
    _, resp = _bill_user("admin")
    if resp:
        return resp
    return _bill_admin_done(billing.grant(request.form.get("email"), request.form.get("days"),
                                          request.form.get("note")))


@app.route("/terms")
def legal_terms():
    return render_template("terms.html", b=legal.BUSINESS, updated=legal.UPDATED)


@app.route("/privacy")
def legal_privacy():
    return render_template("privacy.html", b=legal.BUSINESS, updated=legal.UPDATED)


@app.route("/refund-policy")
def legal_refunds():
    return render_template("refund_policy.html", b=legal.BUSINESS, updated=legal.UPDATED)


@app.route("/contact")
def legal_contact():
    return render_template("contact.html", b=legal.BUSINESS, updated=legal.UPDATED)


@app.after_request
def legal_footer(response):
    """Terms, privacy, refund and contact links at the bottom of public pages."""
    try:
        if request.method != "GET" or response.status_code != 200 or response.mimetype != "text/html":
            return response
        path = request.path
        if path in ("/terms", "/privacy", "/refund-policy", "/contact") or path.startswith(
                ("/dashboard", "/admin", "/pay", "/plans")):
            return response
        body = response.get_data(as_text=True)
        end = body.rfind("</body>")
        if end < 0 or "/refund-policy" in body:
            return response
        links = ('<div style="text-align:center;font:13px system-ui,-apple-system,sans-serif;color:#777;padding:18px 12px">'
                 '<a style="color:#777" href="/terms">Terms</a> &middot; <a style="color:#777" href="/privacy">Privacy</a> '
                 '&middot; <a style="color:#777" href="/refund-policy">Refunds</a> &middot; '
                 '<a style="color:#777" href="/contact">Contact</a></div>')
        response.set_data(body[:end] + links + body[end:])
    except Exception:
        pass
    return response


@app.route("/pay")
def pay_page():
    return redirect("/plans")


@app.route("/plans/buy", methods=["POST"])
def pay_buy():
    user, resp = _bill_user("owner")
    if resp:
        return resp
    if not safety.hit(client_ip(), "pay", 30):
        return _bill_done("Too many tries from this network. Please wait a while and try again.", True, "/pay")
    result = payments.create_order(user, request.form.get("plan"), request.form.get("coupon"))
    if not result["ok"]:
        return _bill_done(result["error"], True, "/pay")
    if result["free"]:
        return _bill_done(result["message"], False, "/plans")
    return render_template("checkout.html", user=user, **result)


@app.route("/plans/paid", methods=["POST"])
def pay_done():
    user, resp = _bill_user("owner")
    if resp:
        return resp
    f = request.form
    ok, message = payments.confirm(user["id"], f.get("razorpay_order_id"), f.get("razorpay_payment_id"),
                                   f.get("razorpay_signature"))
    return _bill_done(message, not ok, "/plans")


@app.route("/api/razorpay/webhook", methods=["POST"])
def pay_webhook():
    status, text = payments.webhook(request.get_data(), request.headers.get("X-Razorpay-Signature", ""))
    return text, status


@app.route("/admin/payments")
def pay_admin():
    _, resp = _bill_user("admin")
    if resp:
        return resp
    return render_template("admin_payments.html", **payments.admin_context())


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


def account_view(notice=None, error=False):
    user = current_user()
    code = accounts.ensure_code(user["id"])
    link = f"{public_base()}/register?ref={code}"
    return render_template("account.html", notice=notice, error=error, code=code, link=link,
                           stats=accounts.account_numbers(user["id"], user["role"]),
                           ref=accounts.referral_stats(user["id"]), reward=accounts.REWARD_CREDITS,
                           share=f"Join me on Trimzee, India's #1 salon booking app. Use my code {code}: {link}")


@app.route("/account")
def account():
    if not current_user():
        return redirect("/login?next=/account")
    return account_view()


@app.route("/account/profile", methods=["POST"])
def account_profile():
    if not current_user():
        return redirect("/login")
    user = current_user()
    ok, message = accounts.update_profile(user["id"], user["role"], request.form.get("name"),
                                          request.form.get("gender"), request.form.get("phone"))
    g.pop("user", None)
    return account_view(message, error=not ok)


# ----------------------------- customer ------------------------------------
@app.route("/my-bookings")
def my_bookings():
    denied = require_role("customer")
    if denied:
        return denied
    return render_template("my_bookings.html", bookings=slots.user_bookings(current_user()["id"]),
                           notice=request.args.get("notice"))


@app.route("/booking/<int:booking_id>")
def booking_page(booking_id):
    denied = require_role("customer")
    if denied:
        return denied
    b = slots.booking_detail(current_user()["id"], booking_id)
    if not b:
        abort(404)
    lat, lng = b["lat"], b["lng"]
    bbox = f"{lng - 0.004},{lat - 0.003},{lng + 0.004},{lat + 0.003}" if lat is not None and lng is not None else None
    return render_template("booking.html", b=b, directions=slots.maps_link(lat, lng), bbox=bbox,
                           marker=f"{lat},{lng}" if bbox else None)


@app.route("/my-bookings/<int:booking_id>/cancel", methods=["POST"])
def cancel_booking(booking_id):
    denied = require_role("customer")
    if denied:
        return denied
    _, message = db.cancel_booking(current_user()["id"], booking_id)
    return render_template("my_bookings.html", bookings=slots.user_bookings(current_user()["id"]),
                           notice=message)


@app.route("/my-bookings/<int:booking_id>/review", methods=["POST"])
def review_booking(booking_id):
    denied = require_role("customer")
    if denied:
        return denied
    _, message = extras.add_review(current_user()["id"], booking_id,
                                   request.form.get("rating"), request.form.get("comment"))
    return render_template("my_bookings.html",
                           bookings=slots.user_bookings(current_user()["id"]), notice=message)


# ----------------------------- owner dashboard -----------------------------
def owner_view(notice=None, error=False):
    salon = extras.owner_salon_full(current_user()["id"])
    if not salon:
        abort(404)
    live, next_label = slots.salon_live(salon["id"])
    gender = meta.owner_gender(current_user()["id"])
    days = [{"label": t["label"], "rows": slots.bookings_for(salon["id"], t["value"])}
            for t in slots.day_tabs()[:1]]
    return render_template("dashboard.html", salon=salon, labels=STATUS_LABELS, notice=notice, error=error,
                           gender=gender, venue=growth.venue_word(gender), live=live, next_label=next_label,
                           rev=meta.revenue(salon["id"]), days=days)


@app.route("/dashboard")
def dashboard():
    denied = require_role("owner")
    return denied or owner_view()


@app.route("/dashboard/status", methods=["POST"])
def dashboard_status():
    denied = require_role("owner")
    if denied:
        return denied
    ok = db.set_salon_status(current_user()["id"], request.form.get("status", ""))
    return owner_view("Status updated." if ok else "Please choose a valid status.", error=not ok)


@app.route("/dashboard/bookings/<int:booking_id>", methods=["POST"])
def dashboard_booking(booking_id):
    denied = require_role("owner")
    if denied:
        return denied
    uid, action = current_user()["id"], request.form.get("action", "")
    if action == "complete":                      # needs the customer's 4-digit code
        ok, message = slots.complete_with_code(uid, booking_id, request.form.get("code"))
        return owner_view(message, error=not ok)
    ok = growth.owner_accept(uid, booking_id) if action == "accept" else db.owner_update_booking(uid, booking_id, action)
    if ok and action in ("accept", "reject"):
        growth.tell_customer(booking_id, accepted=(action == "accept"))
    return owner_view({"accept": "Booking accepted. The customer has been told."}.get(action, "Booking updated.")
                      if ok else "That booking can't be changed.", error=not ok)


@app.route("/dashboard/toggle", methods=["POST"])
def dashboard_toggle():
    denied = require_role("owner")
    if denied:
        return denied
    on = request.form.get("state") == "on"
    meta.toggle_salon(current_user()["id"], on)
    return owner_view("Your salon is ON. Customers can book." if on else "Your salon is OFF. New bookings are paused.")


@app.route("/dashboard/qr")
def dashboard_qr():
    denied = require_role("owner")
    if denied:
        return denied
    salon = extras.owner_salon_full(current_user()["id"])
    if not salon:
        abort(404)
    return render_template("qr_poster.html", salon=salon, url=f"{public_base()}/q/{salon['id']}",
                           venue=growth.venue_word(meta.owner_gender(current_user()["id"])))


@app.route("/dashboard/services")
def owner_services():
    denied = require_role("owner")
    if denied:
        return denied
    return services_view()


def services_view(notice=None, error=False):
    return render_template("services.html", services=extras.get_owner_services(current_user()["id"]),
                           categories=extras.CATEGORY_CHOICES, notice=notice, error=error)


@app.route("/dashboard/services/save", methods=["POST"])
def owner_service_save():
    denied = require_role("owner")
    if denied:
        return denied
    sid = request.form.get("service_id")
    ok, message = extras.save_service(
        current_user()["id"], int(sid) if sid and sid.isdigit() else None,
        request.form.get("name"), request.form.get("category"),
        request.form.get("price"), request.form.get("duration"))
    return services_view(message, error=not ok)


@app.route("/dashboard/services/<int:service_id>/delete", methods=["POST"])
def owner_service_delete(service_id):
    denied = require_role("owner")
    if denied:
        return denied
    ok, message = extras.delete_service(current_user()["id"], service_id)
    return services_view(message, error=not ok)


@app.route("/dashboard/gender", methods=["POST"])
def owner_gender():
    denied = require_role("owner")
    if denied:
        return denied
    ok = meta.set_salon_gender(current_user()["id"], request.form.get("gender", ""))
    return owner_view("Saved." if ok else "Please choose Men, Women or Unisex.", error=not ok)


@app.route("/dashboard/location", methods=["POST"])
def owner_location():
    denied = require_role("owner")
    if denied:
        return denied
    ok = extras.set_salon_location(current_user()["id"], request.form.get("lat"), request.form.get("lng"))
    return owner_view("Salon location saved." if ok else "Could not read that location.", error=not ok)


# ----------------------------- admin ---------------------------------------
def admin_view(notice=None):
    customers, owners = growth.admin_people()
    return render_template("admin.html", stats=extras.admin_overview(), lists=extras.admin_lists(),
                           customers=customers, owners=owners, notice=notice, labels=STATUS_LABELS,
                           waitlist=growth.waitlist_count())


@app.route("/admin")
def admin():
    denied = require_role("admin")
    return denied or admin_view()


@app.route("/admin/salons/<int:salon_id>/<decision>", methods=["POST"])
def admin_salon(salon_id, decision):
    denied = require_role("admin")
    if denied:
        return denied
    ok = extras.admin_set_salon_approval(salon_id, decision)
    word = {"approve": "approved", "reject": "rejected"}.get(decision, decision)
    return admin_view(f"Salon {word}." if ok else "Could not update that salon.")


@app.route("/admin/users/<int:user_id>/<action>", methods=["POST"])
def admin_user(user_id, action):
    denied = require_role("admin")
    if denied:
        return denied
    ok = extras.admin_set_user_active(user_id, action)
    return admin_view("Account updated." if ok else "Could not update that account.")


@app.route("/admin/reviews/<int:review_id>/delete", methods=["POST"])
def admin_review_delete(review_id):
    denied = require_role("admin")
    if denied:
        return denied
    ok = extras.admin_delete_review(review_id)
    return admin_view("Review removed." if ok else "Review not found.")


def pdf_response(name, data):
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.route("/admin/export/<kind>.pdf")
def admin_export(kind):
    denied = require_role("admin")
    if denied:
        return denied
    customers, owners = growth.admin_people()
    stamp = db.now_ist().strftime("%Y-%m-%d %H:%M")
    yes = lambda p: "Yes" if p["is_active"] else "Suspended"
    if kind == "customers":
        rows = [[p["name"], p["email"], p["gender"] or "-", p["joined"], yes(p), p["bookings"]] for p in customers]
        data = minipdf.build_pdf("Trimzee - Customers", f"{len(rows)} customers · generated {stamp} IST",
                                 ["Name", "Email", "Gender", "Joined", "Account", "Bookings"], rows, [3, 4.2, 1.3, 1.8, 1.5, 1.3])
    elif kind == "owners":
        approval = {0: "Pending", 1: "Approved", 2: "Rejected"}
        rows = [[p["name"], p["email"], p["phone"], p["gender"] or "-", p["salon"] or "-",
                 approval.get(p["approved"], "-"), p["joined"], yes(p)] for p in owners]
        data = minipdf.build_pdf("Trimzee - Salon owners", f"{len(rows)} owners · generated {stamp} IST",
                                 ["Name", "Email", "Phone", "Gender", "Business", "Approval", "Joined", "Account"], rows,
                                 [2.2, 3.2, 1.8, 1.0, 2.2, 1.4, 1.6, 1.3])
    else:
        abort(404)
    return pdf_response(f"trimzee-{kind}-{stamp[:10]}.pdf", data)


# ----------------------------- JSON API ------------------------------------
@app.route("/api/salons")
def api_salons():
    return jsonify(slots.get_salons(viewer_gender()))


@app.route("/api/salons/<int:salon_id>/slots")
def api_slots(salon_id):
    user = current_user()
    viewer = user["id"] if user and user["role"] == "customer" else None
    data = slots.get_slots(salon_id, request.args.get("day", ""), viewer) if extras.salon_is_live(salon_id) else None
    if data is None:
        return jsonify({"error": "Salon not found."}), 404
    return jsonify(data)


def booking_guard(salon_id):
    """Shared checks for holding / booking: signed-in customer, salon live, gender rule. Returns (user, error_response)."""
    user = current_user()
    if not user:
        return None, (jsonify({"error": "Please sign in to book.", "login": f"/login?next=/salon/{salon_id}"}), 401)
    if user["role"] != "customer":
        return None, (jsonify({"error": "Please use a customer account to book."}), 403)
    if not extras.salon_is_live(salon_id):
        return None, (jsonify({"error": "Salon not found."}), 404)
    allowed, why = meta.gender_allows(salon_id, user["gender"])
    if not allowed:
        return None, (jsonify({"error": why}), 403)
    return user, None


@app.route("/api/book", methods=["POST"])
def api_book():
    body = request.get_json(silent=True) or {}
    try:
        salon_id = int(body.get("salon_id"))
        service_id = int(body.get("service_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Please choose a service and a time."}), 400
    user, err = booking_guard(salon_id)
    if err:
        return err
    near, why = meta.check_range(salon_id, body.get("lat"), body.get("lng"))
    if not near:
        return jsonify({"error": why}), 403
    code, message, booking_id, secret = slots.book(user["id"], salon_id, service_id, str(body.get("slot", "")),
                                                   body.get("phone"))
    if code != 201:
        return jsonify({"error": message}), code
    info = growth.booking_info(booking_id)
    meta.notify_new_booking(booking_id)
    slots.whatsapp_booking(info, secret)          # only if WhatsApp is set up
    if user.get("phone") is None:
        accounts.update_phone(user["id"], body.get("phone"))
    detail = slots.booking_detail(user["id"], booking_id) or {}
    return jsonify({"message": message, "booking_id": booking_id, "code": secret, "salon": info["salon"],
                    "when": detail.get("when"), "service": info["service"], "price": info["price"],
                    "directions": slots.maps_link(detail.get("lat"), detail.get("lng")),
                    "owner_wa": detail.get("owner_wa"), "detail": f"/booking/{booking_id}"}), 201


@app.route("/api/notifications")
def api_notifications():
    user = current_user()
    if not user:
        return jsonify({"items": []})
    return jsonify({"items": meta.poll_notifications(user)})


@app.route("/api/notifications/read", methods=["POST"])
def api_notifications_read():
    user = current_user()
    body = request.get_json(silent=True) or {}
    if user and isinstance(body.get("ids"), list):
        meta.mark_read(user["id"], body["ids"])
    return jsonify({"ok": True})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json(silent=True) or {}
    if not chatbot.allowed(client_ip()):
        return jsonify({"reply": "You're asking a lot! Please wait a little and try again.", "salons": []}), 429
    text, cards = chatbot.reply(str(body.get("message", "")), slots.get_salons(viewer_gender()))
    return jsonify({"reply": text, "salons": cards})


@app.route("/api/shop-waitlist", methods=["POST"])
def api_shop_waitlist():
    user = current_user()
    if not user:
        return jsonify({"error": "Please sign in first."}), 401
    growth.join_waitlist(user["id"])
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return {"status": "ok", "app": "Trimzee"}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
