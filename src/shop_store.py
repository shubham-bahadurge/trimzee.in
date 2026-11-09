"""Database-backed product catalog, guest/customer carts, and cart totals."""
import secrets

import db


def settings(conn=None):
    owns_connection = conn is None
    conn = conn or db.get_db()
    try:
        return {row["key"]: row["value"] for row in conn.execute(
            "SELECT key, value FROM store_settings")}
    finally:
        if owns_connection:
            conn.close()


def store_open():
    return settings().get("store_open", "true").lower() == "true"


def _stock(conn, product_id, variant_id=None):
    if variant_id is None:
        row = conn.execute("SELECT stock_qty, track_stock FROM products WHERE id = ?",
                           (product_id,)).fetchone()
        if not row:
            return 0
        quantity, tracked = row["stock_qty"], row["track_stock"]
        reservation_filter = "variant_id IS NULL"
    else:
        row = conn.execute("SELECT stock_qty FROM product_variants WHERE id = ? AND product_id = ?",
                           (variant_id, product_id)).fetchone()
        if not row:
            return 0
        quantity, tracked = row["stock_qty"], True
        reservation_filter = "variant_id = ?"
    if not tracked:
        return 2_147_483_647
    params = (product_id, variant_id) if variant_id is not None else (product_id,)
    reserved = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM stock_reservations "
        "WHERE product_id = ? AND " + reservation_filter +
        " AND released_at IS NULL AND consumed_at IS NULL AND expires_at > now()", params).fetchone()[0]
    return max(0, quantity - reserved)


def categories():
    conn = db.get_db()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT id, name, slug FROM categories WHERE is_active = TRUE ORDER BY sort_order, name")]
    finally:
        conn.close()


def products(search="", category_slug=""):
    conn = db.get_db()
    try:
        clauses = ["p.is_active = TRUE"]
        params = []
        if search:
            clauses.append("(lower(p.name) LIKE ? OR lower(p.description) LIKE ?)")
            query = "%" + search.strip().lower()[:80] + "%"
            params.extend((query, query))
        if category_slug:
            clauses.append("c.slug = ?")
            params.append(category_slug)
        rows = conn.execute(
            "SELECT p.id, p.name, p.slug, p.description, p.image_url, p.price_paise, p.mrp_paise, "
            "p.stock_qty, p.track_stock, c.name AS category_name, "
            "(SELECT COUNT(*) FROM product_variants v WHERE v.product_id = p.id) AS variant_count "
            "FROM products p LEFT JOIN categories c ON c.id = p.category_id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY p.created_at DESC, p.id DESC", tuple(params)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["available_qty"] = _stock(conn, item["id"])
            if item["variant_count"]:
                variants = conn.execute("SELECT id FROM product_variants WHERE product_id = ?",
                                        (item["id"],)).fetchall()
                item["available_qty"] = sum(_stock(conn, item["id"], variant["id"])
                                            for variant in variants)
            out.append(item)
        return out
    finally:
        conn.close()


def product(slug):
    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT p.id, p.name, p.slug, p.description, p.image_url, p.price_paise, p.mrp_paise, "
            "p.stock_qty, p.track_stock, p.category_id, c.name AS category_name "
            "FROM products p LEFT JOIN categories c ON c.id = p.category_id "
            "WHERE p.slug = ? AND p.is_active = TRUE", (slug,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["available_qty"] = _stock(conn, item["id"])
        variants = conn.execute(
            "SELECT id, label, price_paise, stock_qty, sku FROM product_variants "
            "WHERE product_id = ? ORDER BY id", (item["id"],)).fetchall()
        item["variants"] = []
        for variant in variants:
            option = dict(variant)
            option["available_qty"] = _stock(conn, item["id"], option["id"])
            item["variants"].append(option)
        item["variants"].sort(key=lambda option: option["available_qty"] == 0)
        return item
    finally:
        conn.close()


def _get_cart(conn, user, session_key):
    if user and user["role"] == "customer":
        cart = conn.execute("SELECT id FROM carts WHERE user_id = ?", (user["id"],)).fetchone()
        guest = conn.execute("SELECT id FROM carts WHERE session_id = ?", (session_key,)).fetchone()
        if cart:
            cart_id = cart["id"]
            if guest:
                guest_items = conn.execute(
                    "SELECT product_id, variant_id, quantity FROM cart_items WHERE cart_id = ?",
                    (guest["id"],)).fetchall()
                for item in guest_items:
                    existing = conn.execute(
                        "SELECT id, quantity FROM cart_items WHERE cart_id = ? AND product_id = ? "
                        "AND variant_id IS NOT DISTINCT FROM ?",
                        (cart_id, item["product_id"], item["variant_id"])).fetchone()
                    if existing:
                        conn.execute("UPDATE cart_items SET quantity = ?, updated_at = now() WHERE id = ?",
                                     (existing["quantity"] + item["quantity"], existing["id"]))
                    else:
                        conn.execute(
                            "INSERT INTO cart_items (cart_id, product_id, variant_id, quantity) "
                            "VALUES (?, ?, ?, ?)",
                            (cart_id, item["product_id"], item["variant_id"], item["quantity"]))
                conn.execute("DELETE FROM carts WHERE id = ?", (guest["id"],))
                conn.commit()
            return cart_id
        if guest:
            conn.execute("UPDATE carts SET user_id = ?, session_id = NULL, updated_at = now() WHERE id = ?",
                         (user["id"], guest["id"]))
            conn.commit()
            return guest["id"]
        inserted = conn.execute(
            "INSERT INTO carts (user_id) VALUES (?) ON CONFLICT DO NOTHING RETURNING id",
            (user["id"],)).fetchone()
        conn.commit()
        if inserted:
            return inserted["id"]
        return conn.execute("SELECT id FROM carts WHERE user_id = ?", (user["id"],)).fetchone()["id"]

    cart = conn.execute("SELECT id FROM carts WHERE session_id = ?", (session_key,)).fetchone()
    if cart:
        return cart["id"]
    inserted = conn.execute(
        "INSERT INTO carts (session_id) VALUES (?) ON CONFLICT DO NOTHING RETURNING id",
        (session_key,)).fetchone()
    conn.commit()
    if inserted:
        return inserted["id"]
    return conn.execute("SELECT id FROM carts WHERE session_id = ?", (session_key,)).fetchone()["id"]


def get_cart(user, session_key):
    conn = db.get_db()
    try:
        cart_id = _get_cart(conn, user, session_key)
        rows = conn.execute(
            "SELECT ci.id AS item_id, ci.product_id, ci.variant_id, ci.quantity, "
            "p.name, p.slug, p.image_url, p.price_paise, p.stock_qty AS product_stock, p.track_stock, "
            "v.label AS variant_label, v.price_paise AS variant_price, v.stock_qty AS variant_stock "
            "FROM cart_items ci JOIN products p ON p.id = ci.product_id "
            "LEFT JOIN product_variants v ON v.id = ci.variant_id AND v.product_id = ci.product_id "
            "WHERE ci.cart_id = ? ORDER BY ci.id", (cart_id,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["unit_price_paise"] = item["variant_price"] if item["variant_id"] else item["price_paise"]
            item["available_qty"] = _stock(conn, item["product_id"], item["variant_id"])
            item["line_total_paise"] = item["unit_price_paise"] * item["quantity"]
            items.append(item)
        subtotal = sum(item["line_total_paise"] for item in items)
        return {"id": cart_id, "items": items, "subtotal_paise": subtotal}
    finally:
        conn.close()


def last_address(user_id):
    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT customer_name, phone, email, address_line1, address_line2, city, state, pincode, country "
            "FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def orders_for_user(user_id):
    conn = db.get_db()
    try:
        rows = conn.execute(
            "SELECT order_number, status, subtotal_paise, discount_paise, shipping_paise, tax_paise, "
            "total_paise, created_at FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 100",
            (user_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def order_for_user(user_id, order_number):
    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT order_number, status, subtotal_paise, discount_paise, shipping_paise, tax_paise, "
            "total_paise, payment_status, customer_name, phone, email, address_line1, address_line2, "
            "city, state, pincode, country, created_at FROM orders "
            "WHERE user_id = ? AND order_number = ?", (user_id, order_number)).fetchone()
        if not row:
            return None
        order = dict(row)
        order["items"] = [dict(item) for item in conn.execute(
            "SELECT product_name_snapshot, variant_label_snapshot, unit_price_paise, quantity, line_total_paise "
            "FROM order_items WHERE order_id = (SELECT id FROM orders WHERE user_id = ? AND order_number = ?) "
            "ORDER BY id", (user_id, order_number))]
        order["history"] = [dict(item) for item in conn.execute(
            "SELECT from_status, to_status, note, created_at FROM order_status_history "
            "WHERE order_id = (SELECT id FROM orders WHERE user_id = ? AND order_number = ?) "
            "ORDER BY created_at, id", (user_id, order_number))]
        return order
    finally:
        conn.close()


def add_to_cart(user, session_key, product_id, variant_id, quantity):
    conn = db.get_db()
    try:
        product_row = conn.execute("SELECT id FROM products WHERE id = ? AND is_active = TRUE",
                                   (product_id,)).fetchone()
        if not product_row:
            return False, "That product is no longer available."
        has_variants = conn.execute(
            "SELECT 1 FROM product_variants WHERE product_id = ? LIMIT 1", (product_id,)).fetchone()
        if has_variants and variant_id is None:
            return False, "Choose a product option before adding it to your cart."
        if variant_id is not None and not conn.execute(
                "SELECT id FROM product_variants WHERE id = ? AND product_id = ?",
                (variant_id, product_id)).fetchone():
            return False, "Choose a valid product option."
        cart_id = _get_cart(conn, user, session_key)
        existing = conn.execute(
            "SELECT id, quantity FROM cart_items WHERE cart_id = ? AND product_id = ? "
            "AND variant_id IS NOT DISTINCT FROM ?",
            (cart_id, product_id, variant_id)).fetchone()
        new_quantity = quantity + (existing["quantity"] if existing else 0)
        if new_quantity > _stock(conn, product_id, variant_id):
            return False, "There isn't enough stock for that quantity."
        if existing:
            conn.execute("UPDATE cart_items SET quantity = ?, updated_at = now() WHERE id = ?",
                         (new_quantity, existing["id"]))
        else:
            conn.execute(
                "INSERT INTO cart_items (cart_id, product_id, variant_id, quantity) VALUES (?, ?, ?, ?)",
                (cart_id, product_id, variant_id, quantity))
        conn.execute("UPDATE carts SET updated_at = now() WHERE id = ?", (cart_id,))
        conn.commit()
        return True, "Added to your cart."
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_cart_item(user, session_key, item_id, quantity=None):
    conn = db.get_db()
    try:
        cart_id = _get_cart(conn, user, session_key)
        item = conn.execute(
            "SELECT product_id, variant_id FROM cart_items WHERE id = ? AND cart_id = ?",
            (item_id, cart_id)).fetchone()
        if not item:
            return False, "Cart item not found."
        if quantity is None:
            conn.execute("DELETE FROM cart_items WHERE id = ? AND cart_id = ?", (item_id, cart_id))
        elif quantity < 1:
            return False, "Quantity must be at least one."
        elif quantity > _stock(conn, item["product_id"], item["variant_id"]):
            return False, "There isn't enough stock for that quantity."
        else:
            conn.execute("UPDATE cart_items SET quantity = ?, updated_at = now() WHERE id = ?",
                         (quantity, item_id))
        conn.execute("UPDATE carts SET updated_at = now() WHERE id = ?", (cart_id,))
        conn.commit()
        return True, "Cart updated."
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_coupon(user, subtotal_paise, code):
    code = (code or "").strip().upper()
    if not code or len(code) > 40:
        return None, "Enter a valid coupon code."
    conn = db.get_db()
    try:
        coupon = conn.execute(
            "SELECT *, (starts_at IS NULL OR starts_at <= now()) AS has_started, "
            "(ends_at IS NULL OR ends_at > now()) AS has_not_ended "
            "FROM coupons WHERE code = ? AND is_active = TRUE",
            (code,)).fetchone()
        if not coupon:
            return None, "That coupon is not valid."
        coupon = dict(coupon)
        if not coupon["has_started"]:
            return None, "That coupon is not active yet."
        if not coupon["has_not_ended"]:
            return None, "That coupon has expired."
        active = conn.execute(
            "SELECT COUNT(*) FROM shop_coupon_reservations "
            "WHERE coupon_id = ? AND released_at IS NULL AND consumed_at IS NULL AND expires_at > now()",
            (coupon["id"],)).fetchone()[0]
        if coupon["usage_limit"] is not None and coupon["used_count"] + active >= coupon["usage_limit"]:
            return None, "That coupon has reached its usage limit."
        if subtotal_paise < coupon["min_order_paise"]:
            return None, "Your cart does not meet the minimum order for this coupon."
        if user and coupon["per_user_limit"] is not None:
            used = conn.execute(
                "SELECT COUNT(*) FROM coupon_redemptions WHERE coupon_id = ? AND user_id = ?",
                (coupon["id"], user["id"])).fetchone()[0]
            reserved = conn.execute(
                "SELECT COUNT(*) FROM shop_coupon_reservations WHERE coupon_id = ? AND user_id = ? "
                "AND released_at IS NULL AND consumed_at IS NULL AND expires_at > now()",
                (coupon["id"], user["id"])).fetchone()[0]
            if used + reserved >= coupon["per_user_limit"]:
                return None, "You have already used this coupon the maximum number of times."
        if coupon["type"] == "percent":
            discount = subtotal_paise * coupon["value"] // 100
            if coupon["max_discount_paise"] is not None:
                discount = min(discount, coupon["max_discount_paise"])
        else:
            discount = coupon["value"]
        return {"id": coupon["id"], "code": coupon["code"],
                "discount_paise": min(discount, subtotal_paise)}, None
    finally:
        conn.close()


def new_session_key():
    return secrets.token_urlsafe(32)
