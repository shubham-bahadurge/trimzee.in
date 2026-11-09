"""Razorpay order creation, verification, webhook processing, and stock holds."""
import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import db
import pgdb
import shop_config
import shop_notifications
import shop_store

API = "https://api.razorpay.com/v1"
RESERVATION_MINUTES = 15
logger = logging.getLogger(__name__)


class PaymentError(ValueError):
    """A safe, customer-presentable payment or checkout error."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _api_post(path, payload):
    config = shop_config.load_shop_config()
    token = base64.b64encode(
        f"{config.razorpay_key_id}:{config.razorpay_key_secret}".encode()).decode()
    request = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Basic " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError):
        logger.exception("Razorpay API request failed")
        raise PaymentError("Payment service is temporarily unavailable. Please try again.",
                           status_code=503) from None


def _require_config():
    config = shop_config.load_shop_config()
    if not (config.razorpay_key_id and config.razorpay_key_secret
            and config.razorpay_webhook_secret):
        raise PaymentError("Online payments are not configured yet.", status_code=503)
    if not config.site_url:
        raise PaymentError("The shop website address is not configured.", status_code=503)
    return config


def _release_expired(conn):
    expired = conn.execute(
        "UPDATE orders SET status = 'cancelled', payment_status = 'failed', updated_at = now() "
        "WHERE status = 'pending_payment' AND stock_reserved_until <= now() "
        "RETURNING id, order_number").fetchall()
    conn.execute(
        "UPDATE stock_reservations SET released_at = now() "
        "WHERE released_at IS NULL AND consumed_at IS NULL AND expires_at <= now()")
    conn.execute(
        "UPDATE shop_coupon_reservations SET released_at = now() "
        "WHERE released_at IS NULL AND consumed_at IS NULL AND expires_at <= now()")
    for order in expired:
        conn.execute(
            "INSERT INTO order_status_history (order_id, from_status, to_status, note) "
            "VALUES (?, 'pending_payment', 'cancelled', ?)",
            (order["id"], "Payment reservation expired after 15 minutes."))
    return len(expired)


def _coupon_quote(conn, user_id, code, subtotal):
    code = (code or "").strip().upper()
    if not code:
        return None, 0
    if len(code) > 40:
        raise PaymentError("That coupon code is not valid.")
    coupon_row = conn.execute(
        "SELECT *, (starts_at IS NULL OR starts_at <= now()) AS has_started, "
        "(ends_at IS NULL OR ends_at > now()) AS has_not_ended "
        "FROM shop_coupons WHERE code = ? AND is_active = TRUE FOR UPDATE", (code,)).fetchone()
    if not coupon_row:
        raise PaymentError("That coupon is not valid.")
    coupon = dict(coupon_row)
    if not coupon["has_started"]:
        raise PaymentError("That coupon is not active yet.")
    if not coupon["has_not_ended"]:
        raise PaymentError("That coupon has expired.")
    if subtotal < coupon["min_order_paise"]:
        raise PaymentError("Your cart does not meet the minimum order for this coupon.")
    active = conn.execute(
        "SELECT COUNT(*) FROM shop_coupon_reservations "
        "WHERE coupon_id = ? AND released_at IS NULL AND consumed_at IS NULL AND expires_at > now()",
        (coupon["id"],)).fetchone()[0]
    if coupon["usage_limit"] is not None and coupon["used_count"] + active >= coupon["usage_limit"]:
        raise PaymentError("That coupon has reached its usage limit.")
    if coupon["per_user_limit"] is not None:
        used = conn.execute(
            "SELECT COUNT(*) FROM shop_coupon_redemptions WHERE coupon_id = ? AND user_id = ?",
            (coupon["id"], user_id)).fetchone()[0]
        reserved = conn.execute(
            "SELECT COUNT(*) FROM shop_coupon_reservations WHERE coupon_id = ? AND user_id = ? "
            "AND released_at IS NULL AND consumed_at IS NULL AND expires_at > now()",
            (coupon["id"], user_id)).fetchone()[0]
        if used + reserved >= coupon["per_user_limit"]:
            raise PaymentError("You have already used this coupon the maximum number of times.")
    if coupon["type"] == "percent":
        discount = subtotal * coupon["value"] // 100
        if coupon["max_discount_paise"] is not None:
            discount = min(discount, coupon["max_discount_paise"])
    else:
        discount = coupon["value"]
    return coupon, min(discount, subtotal)


def _tax_paise(settings, taxable_amount):
    try:
        rate = Decimal(settings.get("gst_percent", "0"))
    except InvalidOperation:
        logger.error("Shop GST setting is not a valid decimal percentage")
        raise PaymentError("Checkout is temporarily unavailable. Please contact the shop.") from None
    if not rate.is_finite() or rate < 0 or rate > 100:
        logger.error("Shop GST setting is outside the supported range")
        raise PaymentError("Checkout is temporarily unavailable. Please contact the shop.")
    return int((Decimal(taxable_amount) * rate / Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def preview_totals(subtotal_paise, discount_paise, settings):
    tax = _tax_paise(settings, max(0, subtotal_paise - discount_paise))
    return {"tax_paise": tax, "total_paise": max(0, subtotal_paise - discount_paise) + tax}


def create_order(user_id, address, coupon_code):
    """Reprice a cart, reserve its stock, and create its Razorpay order."""
    config = _require_config()
    conn = db.get_db()
    try:
        _release_expired(conn)
        user = conn.execute(
            "SELECT id, role, shop_blocked FROM users WHERE id = ? FOR UPDATE",
            (user_id,)).fetchone()
        if not user or user["role"] != "customer":
            raise PaymentError("A customer account is required to place a shop order.")
        if user["shop_blocked"]:
            raise PaymentError("This account cannot place shop orders. Please contact Trimzee support.")
        shop_settings = shop_store.settings(conn)
        if shop_settings.get("store_open", "true").lower() != "true":
            raise PaymentError("The shop is temporarily closed.")

        cart = conn.execute("SELECT id FROM carts WHERE user_id = ? FOR UPDATE", (user_id,)).fetchone()
        if not cart:
            raise PaymentError("Your cart is empty.")
        cart_rows = conn.execute(
            "SELECT product_id, variant_id FROM cart_items WHERE cart_id = ? ORDER BY product_id, variant_id",
            (cart["id"],)).fetchall()
        if not cart_rows:
            raise PaymentError("Your cart is empty.")
        product_ids = sorted({row["product_id"] for row in cart_rows})
        variant_ids = sorted({row["variant_id"] for row in cart_rows if row["variant_id"] is not None})
        for product_id in product_ids:
            conn.execute("SELECT id FROM products WHERE id = ? FOR UPDATE", (product_id,)).fetchone()
        for variant_id in variant_ids:
            conn.execute("SELECT id FROM product_variants WHERE id = ? FOR UPDATE", (variant_id,)).fetchone()

        rows = conn.execute(
            "SELECT ci.id AS cart_item_id, ci.product_id, ci.variant_id, ci.quantity, "
            "p.name, p.price_paise, p.stock_qty AS product_stock, p.track_stock, p.is_active, "
            "v.label AS variant_label, v.price_paise AS variant_price, v.stock_qty AS variant_stock "
            "FROM cart_items ci JOIN products p ON p.id = ci.product_id "
            "LEFT JOIN product_variants v ON v.id = ci.variant_id AND v.product_id = ci.product_id "
            "WHERE ci.cart_id = ? ORDER BY ci.product_id, ci.variant_id",
            (cart["id"],)).fetchall()
        if len(rows) != len(cart_rows):
            raise PaymentError("A cart item is no longer available. Please review your cart.")

        subtotal = 0
        order_lines = []
        for row in rows:
            if not row["is_active"] or (row["variant_id"] is not None and row["variant_label"] is None):
                raise PaymentError(f"{row['name']} is no longer available.")
            stock = row["variant_stock"] if row["variant_id"] is not None else row["product_stock"]
            tracks_stock = row["variant_id"] is not None or row["track_stock"]
            if tracks_stock:
                reserved = conn.execute(
                    "SELECT COALESCE(SUM(quantity), 0) FROM stock_reservations "
                    "WHERE product_id = ? AND variant_id IS NOT DISTINCT FROM ? "
                    "AND released_at IS NULL AND consumed_at IS NULL AND expires_at > now()",
                    (row["product_id"], row["variant_id"])).fetchone()[0]
                if row["quantity"] > stock - reserved:
                    raise PaymentError(f"There is not enough stock for {row['name']}.")
            price = row["variant_price"] if row["variant_id"] is not None else row["price_paise"]
            line_total = price * row["quantity"]
            subtotal += line_total
            order_lines.append({
                "product_id": row["product_id"], "variant_id": row["variant_id"],
                "name": row["name"], "variant_label": row["variant_label"],
                "price": price, "quantity": row["quantity"], "line_total": line_total,
            })

        coupon, discount = _coupon_quote(conn, user_id, coupon_code, subtotal)
        tax = _tax_paise(shop_settings, subtotal - discount)
        shipping = 0
        total = subtotal - discount + shipping + tax
        if total <= 0:
            raise PaymentError("This coupon makes the order total zero. Please use a different coupon.")
        order_number = conn.execute(
            "SELECT 'TRZ-' || lpad(nextval('shop_order_number_seq')::text, 6, '0') AS value"
        ).fetchone()["value"]
        order_id = conn.execute(
            "INSERT INTO orders (order_number, user_id, subtotal_paise, discount_paise, shipping_paise, "
            "tax_paise, total_paise, coupon_id, customer_name, phone, email, address_line1, address_line2, "
            "city, state, pincode, country, stock_reserved_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "now() + (? * interval '1 minute')) "
                "RETURNING id",
                (order_number, user_id, subtotal, discount, shipping, tax, total,
                 coupon["id"] if coupon else None, address["customer_name"], address["phone"], address["email"],
                 address["address_line1"], address["address_line2"], address["city"], address["state"],
                 address["pincode"], address["country"], RESERVATION_MINUTES)).fetchone()["id"]
        for line in order_lines:
            order_item_id = conn.execute(
                "INSERT INTO order_items (order_id, product_id, variant_id, product_name_snapshot, "
                "variant_label_snapshot, unit_price_paise, quantity, line_total_paise) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (order_id, line["product_id"], line["variant_id"], line["name"], line["variant_label"],
                 line["price"], line["quantity"], line["line_total"])).fetchone()["id"]
            conn.execute(
                "INSERT INTO stock_reservations (order_item_id, product_id, variant_id, quantity, expires_at) "
                "VALUES (?, ?, ?, ?, now() + (? * interval '1 minute'))",
                (order_item_id, line["product_id"], line["variant_id"], line["quantity"],
                 RESERVATION_MINUTES))
        if coupon:
            conn.execute(
                "INSERT INTO shop_coupon_reservations "
                "(coupon_id, user_id, order_id, expires_at) "
                "VALUES (?, ?, ?, now() + (? * interval '1 minute'))",
                (coupon["id"], user_id, order_id, RESERVATION_MINUTES))
        conn.execute(
            "INSERT INTO order_status_history (order_id, from_status, to_status, note) "
            "VALUES (?, NULL, 'pending_payment', 'Online payment started.')", (order_id,))

        try:
            razorpay_order = _api_post("/orders", {
                "amount": total,
                "currency": "INR",
                "receipt": order_number,
                "notes": {"trimzee_order_number": order_number},
            })
        except PaymentError:
            raise
        razorpay_order_id = razorpay_order.get("id")
        if not isinstance(razorpay_order_id, str) or not razorpay_order_id:
            logger.error("Razorpay order response did not contain an order ID")
            raise PaymentError("Could not start the payment. Please try again.")
        conn.execute(
            "UPDATE orders SET razorpay_order_id = ?, updated_at = now() WHERE id = ?",
            (razorpay_order_id, order_id))
        conn.commit()
        return {
            "order_number": order_number,
            "razorpay_order_id": razorpay_order_id,
            "key_id": config.razorpay_key_id,
            "amount_paise": total,
            "currency": "INR",
            "customer_name": address["customer_name"],
            "email": address["email"],
            "phone": address["phone"],
            "site_name": shop_settings.get("store_name", "Trimzee Shop"),
            "image_url": config.site_url + "/img/logo-mark.png",
        }
    except PaymentError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        logger.exception("Could not create shop payment order")
        raise PaymentError("Could not start the payment. Please try again.") from None
    finally:
        conn.close()
        pgdb.release()


def _signature_ok(message, signature, secret):
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


def _release_order(conn, order_id):
    conn.execute(
        "UPDATE stock_reservations SET released_at = now() "
        "WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id = ?) "
        "AND released_at IS NULL AND consumed_at IS NULL", (order_id,))
    conn.execute(
        "UPDATE shop_coupon_reservations SET released_at = now() "
        "WHERE order_id = ? AND released_at IS NULL AND consumed_at IS NULL", (order_id,))


def _refund_unfulfillable(conn, order, payment_id):
    _api_post(f"/payments/{urllib.parse.quote(payment_id, safe='')}/refund",
              {"amount": order["total_paise"]})
    _release_order(conn, order["id"])
    conn.execute(
        "UPDATE orders SET status = 'refunded', payment_status = 'refunded', razorpay_payment_id = ?, "
        "updated_at = now() WHERE id = ?", (payment_id, order["id"]))
    conn.execute(
        "INSERT INTO order_status_history (order_id, from_status, to_status, note) "
        "VALUES (?, ?, 'refunded', ?)",
        (order["id"], order["status"],
         "Payment arrived after the stock hold expired; a full refund was requested."))


def _mark_paid(conn, order, payment_id, amount=None, currency="INR"):
    if currency != "INR" or (amount is not None and int(amount) != order["total_paise"]):
        raise PaymentError("Payment amount does not match the order.")
    if order["status"] == "paid" and order["payment_status"] == "paid":
        return False, order["order_number"], "paid"
    if order["status"] == "cancelled" and order["payment_status"] == "failed":
        _refund_unfulfillable(conn, order, payment_id)
        return True, order["order_number"], "refunded"
    if order["status"] != "pending_payment":
        return False, order["order_number"], order["status"]

    items = conn.execute(
        "SELECT oi.id, oi.product_id, oi.variant_id, oi.quantity, p.track_stock "
        "FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "WHERE oi.order_id = ? ORDER BY oi.product_id, oi.variant_id",
        (order["id"],)).fetchall()
    can_fulfill = True
    for item in items:
        reservation = conn.execute(
            "SELECT id, expires_at > now() AND released_at IS NULL AND consumed_at IS NULL AS active "
            "FROM stock_reservations WHERE order_item_id = ? FOR UPDATE",
            (item["id"],)).fetchone()
        if reservation and reservation["active"]:
            continue
        if item["variant_id"] is not None:
            stock_row = conn.execute(
                "SELECT stock_qty FROM product_variants WHERE id = ? FOR UPDATE",
                (item["variant_id"],)).fetchone()
            stock = stock_row["stock_qty"] if stock_row else 0
        elif item["track_stock"]:
            stock_row = conn.execute(
                "SELECT stock_qty FROM products WHERE id = ? FOR UPDATE", (item["product_id"],)).fetchone()
            stock = stock_row["stock_qty"] if stock_row else 0
        else:
            stock = 2_147_483_647
        active_reservations = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM stock_reservations "
            "WHERE product_id = ? AND variant_id IS NOT DISTINCT FROM ? "
            "AND order_item_id != ? AND released_at IS NULL AND consumed_at IS NULL AND expires_at > now()",
            (item["product_id"], item["variant_id"], item["id"])).fetchone()[0]
        if item["quantity"] > stock - active_reservations:
            can_fulfill = False
            break
        if reservation:
            conn.execute(
                "UPDATE stock_reservations SET released_at = NULL, "
                "expires_at = now() + (? * interval '1 minute') WHERE id = ?",
                (RESERVATION_MINUTES, reservation["id"]))
        else:
            conn.execute(
                "INSERT INTO stock_reservations "
                "(order_item_id, product_id, variant_id, quantity, expires_at) "
                "VALUES (?, ?, ?, ?, now() + (? * interval '1 minute'))",
                (item["id"], item["product_id"], item["variant_id"], item["quantity"],
                 RESERVATION_MINUTES))

    if not can_fulfill:
        _refund_unfulfillable(conn, order, payment_id)
        return True, order["order_number"], "refunded"

    for item in items:
        if item["variant_id"] is not None:
            conn.execute("UPDATE product_variants SET stock_qty = stock_qty - ?, updated_at = now() WHERE id = ?",
                         (item["quantity"], item["variant_id"]))
        elif item["track_stock"]:
            conn.execute("UPDATE products SET stock_qty = stock_qty - ?, updated_at = now() WHERE id = ?",
                         (item["quantity"], item["product_id"]))
        conn.execute(
            "UPDATE stock_reservations SET consumed_at = now() "
            "WHERE order_item_id = ? AND released_at IS NULL AND consumed_at IS NULL",
            (item["id"],))

    coupon_reservation = conn.execute(
        "SELECT coupon_id, user_id FROM shop_coupon_reservations "
        "WHERE order_id = ? AND consumed_at IS NULL FOR UPDATE",
        (order["id"],)).fetchone()
    if coupon_reservation:
        conn.execute(
            "UPDATE shop_coupons SET used_count = used_count + 1, updated_at = now() WHERE id = ?",
            (coupon_reservation["coupon_id"],))
        conn.execute(
            "INSERT INTO shop_coupon_redemptions (coupon_id, user_id, order_id) VALUES (?, ?, ?) "
            "ON CONFLICT (order_id) DO NOTHING RETURNING id",
            (coupon_reservation["coupon_id"], coupon_reservation["user_id"], order["id"])).fetchone()
        conn.execute(
            "UPDATE shop_coupon_reservations SET released_at = NULL, consumed_at = now() WHERE order_id = ?",
            (order["id"],))

    conn.execute(
        "UPDATE orders SET status = 'paid', payment_status = 'paid', razorpay_payment_id = ?, "
        "updated_at = now() WHERE id = ?", (payment_id, order["id"]))
    conn.execute(
        "INSERT INTO order_status_history (order_id, from_status, to_status, note) "
        "VALUES (?, 'pending_payment', 'paid', 'Online payment confirmed.')", (order["id"],))
    cart = conn.execute("SELECT id FROM carts WHERE user_id = ?", (order["user_id"],)).fetchone()
    if cart:
        for item in items:
            existing = conn.execute(
                "SELECT id, quantity FROM cart_items WHERE cart_id = ? AND product_id = ? "
                "AND variant_id IS NOT DISTINCT FROM ?",
                (cart["id"], item["product_id"], item["variant_id"])).fetchone()
            if existing:
                remaining = existing["quantity"] - item["quantity"]
                if remaining > 0:
                    conn.execute(
                        "UPDATE cart_items SET quantity = ?, updated_at = now() WHERE id = ?",
                        (remaining, existing["id"]))
                else:
                    conn.execute("DELETE FROM cart_items WHERE id = ?", (existing["id"],))
    return True, order["order_number"], "paid"


def _complete_payment(razorpay_order_id, payment_id, amount=None, currency="INR", expected_user_id=None):
    if not razorpay_order_id or not payment_id or len(razorpay_order_id) > 80 or len(payment_id) > 80:
        raise PaymentError("Payment details are incomplete.")
    conn = db.get_db()
    try:
        order = conn.execute(
            "SELECT id, user_id, order_number, status, payment_status, total_paise "
            "FROM orders WHERE razorpay_order_id = ? FOR UPDATE",
            (razorpay_order_id,)).fetchone()
        if not order:
            raise PaymentError("The shop order could not be found.")
        if expected_user_id is not None and order["user_id"] != expected_user_id:
            raise PaymentError("The shop order could not be found.")
        changed, order_number, status = _mark_paid(conn, order, payment_id, amount, currency)
        conn.commit()
        if changed and status == "paid":
            shop_notifications.paid_order(order_number)
        return {"changed": changed, "order_number": order_number, "status": status}
    except PaymentError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        logger.exception("Could not confirm shop payment")
        raise PaymentError("Could not confirm payment yet. Please try again.", status_code=503) from None
    finally:
        conn.close()
        pgdb.release()


def verify_checkout(user_id, razorpay_order_id, payment_id, signature):
    config = _require_config()
    if not razorpay_order_id or not payment_id or not signature:
        raise PaymentError("Payment details are incomplete.")
    message = f"{razorpay_order_id}|{payment_id}".encode()
    if not _signature_ok(message, signature, config.razorpay_key_secret):
        raise PaymentError("Payment verification failed.")
    return _complete_payment(razorpay_order_id, payment_id, expected_user_id=user_id)


def request_admin_refund(order_id, admin_id):
    """Request one full Razorpay refund per order, without holding DB locks over the API call."""
    config = _require_config()
    conn = db.get_db()
    try:
        order = conn.execute(
            "SELECT id, status, payment_status, total_paise, razorpay_payment_id "
            "FROM orders WHERE id = ? FOR UPDATE", (order_id,)).fetchone()
        if not order:
            raise PaymentError("Order not found.")
        if order["status"] == "refunded" or order["payment_status"] == "refunded":
            raise PaymentError("This order is already refunded.")
        if order["payment_status"] != "paid" or not order["razorpay_payment_id"]:
            raise PaymentError("Only paid orders can be refunded.")
        existing = conn.execute(
            "SELECT status FROM shop_refund_requests WHERE order_id = ? FOR UPDATE",
            (order_id,)).fetchone()
        if existing and existing["status"] != "failed":
            raise PaymentError("A refund request already exists for this order.")
        if existing:
            conn.execute(
                "UPDATE shop_refund_requests SET status = 'requested', error_code = NULL, "
                "requested_by = ?, updated_at = now() WHERE order_id = ?", (admin_id, order_id))
        else:
            conn.execute(
                "INSERT INTO shop_refund_requests (order_id, requested_by) VALUES (?, ?)",
                (order_id, admin_id))
        payment_id = order["razorpay_payment_id"]
        amount = order["total_paise"]
        conn.commit()
    except PaymentError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        logger.exception("Could not reserve shop refund request")
        raise PaymentError("Could not start the refund. Please try again.", status_code=503) from None
    finally:
        conn.close()
        pgdb.release()

    try:
        response = _api_post(
            f"/payments/{urllib.parse.quote(payment_id, safe='')}/refund",
            {"amount": amount, "notes": {"trimzee_order_id": str(order_id)}},
        )
    except PaymentError:
        logger.error("Razorpay refund response is uncertain; the request remains reserved for review")
        raise PaymentError(
            "Razorpay did not confirm the refund request. Check the Razorpay dashboard before retrying.",
            status_code=503) from None
    refund_id = response.get("id")
    if not isinstance(refund_id, str) or not refund_id:
        logger.error("Razorpay refund response did not contain a refund ID")
        raise PaymentError(
            "Razorpay did not confirm the refund request. Check the Razorpay dashboard before retrying.",
            status_code=503)
    conn = db.get_db()
    try:
        current = conn.execute(
            "SELECT status, payment_status FROM orders WHERE id = ? FOR UPDATE", (order_id,)).fetchone()
        conn.execute(
            "UPDATE shop_refund_requests SET status = 'submitted', razorpay_refund_id = ?, "
            "updated_at = now() WHERE order_id = ?", (refund_id, order_id))
        if current and current["status"] != "refunded":
            conn.execute(
                "UPDATE orders SET status = 'refunded', payment_status = 'refunded', updated_at = now() "
                "WHERE id = ?", (order_id,))
            conn.execute(
                "INSERT INTO order_status_history (order_id, from_status, to_status, changed_by, note) "
                "VALUES (?, ?, 'refunded', ?, ?)",
                (order_id, current["status"], admin_id, "Full refund submitted to Razorpay."))
        conn.execute(
            "INSERT INTO admin_audit_log "
            "(admin_user_id, action, entity_type, entity_id, details_json) "
            "VALUES (?, 'order.refund_requested', 'order', ?, CAST(? AS JSONB))",
            (admin_id, str(order_id), json.dumps({"refund_id": refund_id, "amount_paise": amount})))
        conn.commit()
        return refund_id
    except Exception:
        conn.rollback()
        logger.exception("Razorpay accepted a refund but local order state could not be updated")
        raise PaymentError(
            "Razorpay accepted the refund, but the order record needs admin review.",
            status_code=503) from None
    finally:
        conn.close()
        pgdb.release()


def _mark_failed(razorpay_order_id):
    conn = db.get_db()
    try:
        order = conn.execute(
            "SELECT id, order_number, status FROM orders WHERE razorpay_order_id = ? FOR UPDATE",
            (razorpay_order_id,)).fetchone()
        if not order or order["status"] != "pending_payment":
            conn.commit()
            return False
        _release_order(conn, order["id"])
        conn.execute(
            "UPDATE orders SET status = 'cancelled', payment_status = 'failed', updated_at = now() WHERE id = ?",
            (order["id"],))
        conn.execute(
            "INSERT INTO order_status_history (order_id, from_status, to_status, note) "
            "VALUES (?, 'pending_payment', 'cancelled', 'Online payment failed.')", (order["id"],))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        logger.exception("Could not release stock after failed shop payment")
        raise
    finally:
        conn.close()
        pgdb.release()


def _mark_refunded(payment_id, refund_id=None):
    conn = db.get_db()
    try:
        order = conn.execute(
            "SELECT id, order_number, status FROM orders WHERE razorpay_payment_id = ? FOR UPDATE",
            (payment_id,)).fetchone()
        if not order or order["status"] == "refunded":
            if order and refund_id:
                conn.execute(
                    "UPDATE shop_refund_requests SET status = 'submitted', razorpay_refund_id = ?, "
                    "updated_at = now() WHERE order_id = ?", (refund_id, order["id"]))
                conn.commit()
                return True
            conn.commit()
            return False
        previous = order["status"]
        conn.execute(
            "UPDATE orders SET status = 'refunded', payment_status = 'refunded', updated_at = now() WHERE id = ?",
            (order["id"],))
        conn.execute(
            "INSERT INTO order_status_history (order_id, from_status, to_status, note) VALUES (?, ?, 'refunded', ?)",
            (order["id"], previous, "Refund confirmed by Razorpay."))
        if refund_id:
            conn.execute(
                "UPDATE shop_refund_requests SET status = 'submitted', razorpay_refund_id = ?, "
                "updated_at = now() WHERE order_id = ?", (refund_id, order["id"]))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        logger.exception("Could not apply shop refund webhook")
        raise
    finally:
        conn.close()
        pgdb.release()


def process_webhook(raw_body, signature):
    config = _require_config()
    if not _signature_ok(raw_body, signature, config.razorpay_webhook_secret):
        raise PaymentError("Webhook signature verification failed.")
    try:
        event = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PaymentError("Webhook payload is invalid.") from None
    if not isinstance(event, dict):
        raise PaymentError("Webhook payload is invalid.")
    event_type = event.get("event")
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        raise PaymentError("Webhook payload is invalid.")
    if event_type == "payment.captured":
        payment_payload = payload.get("payment", {})
        entity = payment_payload.get("entity", {}) if isinstance(payment_payload, dict) else None
        if not isinstance(entity, dict):
            raise PaymentError("Webhook payload is invalid.")
        result = _complete_payment(
            entity.get("order_id"), entity.get("id"), entity.get("amount"), entity.get("currency", ""))
        return result
    if event_type == "payment.failed":
        payment_payload = payload.get("payment", {})
        entity = payment_payload.get("entity", {}) if isinstance(payment_payload, dict) else None
        if not isinstance(entity, dict):
            raise PaymentError("Webhook payload is invalid.")
        return {"changed": _mark_failed(entity.get("order_id")), "status": "cancelled"}
    if event_type == "refund.processed":
        refund_payload = payload.get("refund", {})
        entity = refund_payload.get("entity", {}) if isinstance(refund_payload, dict) else None
        if not isinstance(entity, dict):
            raise PaymentError("Webhook payload is invalid.")
        return {"changed": _mark_refunded(entity.get("payment_id"), entity.get("id")), "status": "refunded"}
    return {"changed": False, "status": "ignored"}
