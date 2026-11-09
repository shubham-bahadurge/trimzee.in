"""Customer-facing product catalog and cart routes."""
import re

from flask import abort, jsonify, redirect, render_template, request, session, url_for

import db
import shop_payments
import shop_store

ORDER_STATUS_LABELS = {
    "pending_payment": "Payment pending",
    "paid": "Order confirmed",
    "packed": "Packed",
    "handed_to_courier": "Shipped",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
    "refunded": "Refunded",
}
ORDER_STATUS_HINTS = {
    "pending_payment": "Complete payment to confirm this order.",
    "paid": "Your order is confirmed and being prepared.",
    "packed": "Your order is packed and getting ready to ship.",
    "handed_to_courier": "Your order is on its way.",
    "delivered": "Your order has been delivered.",
    "cancelled": "This order was cancelled.",
    "refunded": "A refund has been issued for this order.",
}
ORDER_PROGRESS_STEPS = (
    ("paid", "Confirmed"),
    ("packed", "Packed"),
    ("handed_to_courier", "Shipped"),
    ("delivered", "Delivered"),
)


def register_routes(app, current_user, safe_next):
    def cart_key():
        if not session.get("shop_cart_key"):
            session["shop_cart_key"] = shop_store.new_session_key()
        return session["shop_cart_key"]

    def notice(message, error=False):
        session["shop_notice"] = {"message": message, "error": error}

    def take_notice():
        return session.pop("shop_notice", None)

    def price_filter(amount):
        whole, fraction = divmod(int(amount or 0), 100)
        return f"{whole:,}.{fraction:02d}"

    app.jinja_env.filters["paise"] = price_filter

    def cart_context():
        user = current_user()
        cart = shop_store.get_cart(user, cart_key())
        applied = None
        coupon_error = session.pop("shop_coupon_error", None)
        code = session.get("shop_coupon_code")
        if code:
            applied, coupon_error = shop_store.apply_coupon(user, cart["subtotal_paise"], code)
            if not applied:
                session.pop("shop_coupon_code", None)
        discount = applied["discount_paise"] if applied else 0
        cart["discount_paise"] = discount
        cart["total_paise"] = max(0, cart["subtotal_paise"] - discount)
        cart["coupon"] = applied
        cart["coupon_error"] = coupon_error
        cart["store_open"] = shop_store.store_open()
        return cart

    @app.route("/shop")
    def shop_page():
        search = (request.args.get("q") or "").strip()[:80]
        category_slug = (request.args.get("category") or "").strip()[:80]
        category_list = shop_store.categories()
        valid_categories = {category["slug"] for category in category_list}
        if category_slug not in valid_categories:
            category_slug = ""
        catalog = shop_store.products(search, category_slug)
        cart = cart_context()
        return render_template(
            "shop.html", products=catalog, categories=category_list, search=search,
            category_slug=category_slug, cart=cart, notice=take_notice(),
            store_open=cart["store_open"], store_name=shop_store.settings().get("store_name", "Trimzee Shop"))

    @app.route("/shop/<slug>")
    def shop_product(slug):
        item = shop_store.product(slug)
        if not item:
            abort(404)
        related = [product for product in shop_store.products()
                   if product["id"] != item["id"]][:4]
        return render_template("shop_product.html", product=item, related=related,
                               cart=cart_context(), notice=take_notice(), store_open=shop_store.store_open())

    @app.route("/cart")
    def shop_cart():
        return render_template("shop_cart.html", cart=cart_context(), notice=take_notice())

    @app.route("/checkout")
    def shop_checkout():
        user = current_user()
        if not user:
            return redirect("/login?next=%2Fcheckout")
        if user["role"] != "customer":
            abort(403)
        if not shop_store.store_open():
            notice("The shop is temporarily closed. Checkout is paused.", error=True)
            return redirect(url_for("shop_cart"))
        cart = cart_context()
        if not cart["items"]:
            return redirect(url_for("shop_page"))
        address = shop_store.last_address(user["id"])
        settings = shop_store.settings()
        totals = shop_payments.preview_totals(
            cart["subtotal_paise"], cart["discount_paise"], settings)
        return render_template(
            "shop_checkout.html", cart=cart, address=address, user=user,
            key_id=shop_payments.shop_config.load_shop_config().razorpay_key_id,
            gst_percent=settings.get("gst_percent", "0"), tax_paise=totals["tax_paise"],
            checkout_total_paise=totals["total_paise"])

    @app.route("/shop/payment/create", methods=["POST"])
    def shop_payment_create():
        user = current_user()
        if not user:
            return jsonify(error="Please sign in to continue to checkout."), 401
        if user["role"] != "customer":
            abort(403)
        if not shop_store.store_open():
            return jsonify(error="The shop is temporarily closed."), 403

        def clean_field(name, maximum, required=True):
            value = " ".join((request.form.get(name) or "").split())
            if len(value) > maximum or (required and not value):
                raise shop_payments.PaymentError(f"Please provide a valid {name.replace('_', ' ')}.")
            return value

        try:
            address = {
                "customer_name": clean_field("customer_name", 100),
                "phone": clean_field("phone", 14),
                "email": clean_field("email", 160).lower(),
                "address_line1": clean_field("address_line1", 180),
                "address_line2": clean_field("address_line2", 180, required=False),
                "city": clean_field("city", 80),
                "state": clean_field("state", 80),
                "pincode": clean_field("pincode", 6),
                "country": clean_field("country", 80),
            }
            if not db.EMAIL_RE.match(address["email"]):
                raise shop_payments.PaymentError("Enter a valid email address.")
            if not re.fullmatch(r"\+?[0-9 ()-]{8,14}", address["phone"]):
                raise shop_payments.PaymentError("Enter a valid phone number.")
            if not re.fullmatch(r"[0-9]{6}", address["pincode"]):
                raise shop_payments.PaymentError("Enter a valid 6-digit PIN code.")
            if address["country"].lower() != "india":
                raise shop_payments.PaymentError("Delivery is currently available in India only.")
            payment = shop_payments.create_order(
                user["id"], address, session.get("shop_coupon_code", ""))
            return jsonify(payment)
        except shop_payments.PaymentError as error:
            return jsonify(error=str(error)), error.status_code

    @app.route("/shop/payment/verify", methods=["POST"])
    def shop_payment_verify():
        user = current_user()
        if not user:
            return jsonify(error="Please sign in to verify your payment."), 401
        if user["role"] != "customer":
            abort(403)
        try:
            payment = shop_payments.verify_checkout(
                user["id"],
                request.form.get("razorpay_order_id", ""),
                request.form.get("razorpay_payment_id", ""),
                request.form.get("razorpay_signature", ""),
            )
            order = shop_store.order_for_user(user["id"], payment["order_number"])
            if not order:
                abort(404)
            return jsonify(
                redirect=url_for(
                    "shop_order_success" if payment["status"] == "paid" else "shop_order_detail",
                    order_number=payment["order_number"]),
                status=payment["status"])
        except shop_payments.PaymentError as error:
            return jsonify(error=str(error)), error.status_code

    @app.route("/webhooks/razorpay", methods=["POST"])
    def shop_razorpay_webhook():
        try:
            shop_payments.process_webhook(
                request.get_data(), request.headers.get("X-Razorpay-Signature", ""))
        except shop_payments.PaymentError as error:
            return str(error), error.status_code
        return "ok", 200

    @app.route("/orders")
    def shop_orders():
        user = current_user()
        if not user:
            return redirect("/login?next=%2Forders")
        if user["role"] != "customer":
            abort(403)
        orders = shop_store.orders_for_user(user["id"])
        for order in orders:
            order["status_label"] = ORDER_STATUS_LABELS.get(order["status"], order["status"].replace("_", " ").title())
            order["status_hint"] = ORDER_STATUS_HINTS.get(order["status"], "")
        return render_template("shop_orders.html", orders=orders)

    @app.route("/orders/<order_number>")
    def shop_order_detail(order_number):
        user = current_user()
        if not user:
            return redirect("/login?next=%2Forders")
        if user["role"] != "customer":
            abort(403)
        order = shop_store.order_for_user(user["id"], order_number)
        if not order:
            abort(404)
        history_by_status = {entry["to_status"]: entry for entry in order["history"]}
        current_index = next(
            (index for index, (status, _) in enumerate(ORDER_PROGRESS_STEPS)
             if status == order["status"]),
            -1,
        )
        progress_steps = [
            {
                "status": status,
                "label": label,
                "state": "complete" if index < current_index else
                         "current" if index == current_index else "upcoming",
                "history": history_by_status.get(status),
            }
            for index, (status, label) in enumerate(ORDER_PROGRESS_STEPS)
        ]
        return render_template(
            "shop_order.html",
            order=order,
            status_label=ORDER_STATUS_LABELS.get(
                order["status"], order["status"].replace("_", " ").title()),
            status_hint=ORDER_STATUS_HINTS.get(order["status"], ""),
            progress_steps=progress_steps,
            status_is_exception=order["status"] in ("pending_payment", "cancelled", "refunded"),
        )

    @app.route("/order/success/<order_number>")
    def shop_order_success(order_number):
        user = current_user()
        if not user:
            return redirect(url_for("login", next=url_for("shop_order_success", order_number=order_number)))
        if user["role"] != "customer":
            abort(403)
        order = shop_store.order_for_user(user["id"], order_number)
        if not order:
            abort(404)
        return render_template("shop_order_success.html", order=order)

    @app.route("/cart/add", methods=["POST"])
    def shop_cart_add():
        if not shop_store.store_open():
            notice("The shop is temporarily closed. Please try again later.", error=True)
            return redirect(safe_next(request.form.get("next")) or url_for("shop_cart"))
        try:
            product_id = int(request.form.get("product_id", ""))
            variant_value = request.form.get("variant_id", "").strip()
            variant_id = int(variant_value) if variant_value else None
            quantity = int(request.form.get("quantity", "1"))
        except ValueError:
            notice("Choose a valid product and quantity.", error=True)
            return redirect(safe_next(request.form.get("next")) or url_for("shop_page"))
        if quantity < 1 or quantity > 99:
            notice("Choose a quantity between 1 and 99.", error=True)
            return redirect(safe_next(request.form.get("next")) or url_for("shop_page"))
        ok, message = shop_store.add_to_cart(current_user(), cart_key(), product_id, variant_id, quantity)
        notice(message, error=not ok)
        target = safe_next(request.form.get("next"))
        return redirect(target or url_for("shop_cart"))

    @app.route("/cart/items/<int:item_id>/update", methods=["POST"])
    def shop_cart_update(item_id):
        try:
            quantity = int(request.form.get("quantity", ""))
        except ValueError:
            notice("Enter a valid quantity.", error=True)
            return redirect(url_for("shop_cart"))
        ok, message = shop_store.update_cart_item(current_user(), cart_key(), item_id, quantity)
        notice(message, error=not ok)
        return redirect(url_for("shop_cart"))

    @app.route("/cart/items/<int:item_id>/remove", methods=["POST"])
    def shop_cart_remove(item_id):
        ok, message = shop_store.update_cart_item(current_user(), cart_key(), item_id)
        notice(message, error=not ok)
        return redirect(url_for("shop_cart"))

    @app.route("/cart/coupon", methods=["POST"])
    def shop_coupon_apply():
        cart = cart_context()
        coupon, error = shop_store.apply_coupon(current_user(), cart["subtotal_paise"],
                                                request.form.get("code", ""))
        if error:
            session["shop_coupon_error"] = error
            session.pop("shop_coupon_code", None)
        else:
            session["shop_coupon_code"] = coupon["code"]
            session.pop("shop_coupon_error", None)
        return redirect(url_for("shop_cart"))

    @app.route("/cart/coupon/clear", methods=["POST"])
    def shop_coupon_clear():
        session.pop("shop_coupon_code", None)
        session.pop("shop_coupon_error", None)
        notice("Coupon removed.")
        return redirect(url_for("shop_cart"))
