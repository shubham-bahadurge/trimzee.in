"""Postgres persistence for private Trimzee shop administration."""
import json

import db
import pgdb
import shop_payments


def _connection():
    return db.get_db()


def _audit(conn, admin_id, action, entity_type, entity_id=None, details=None):
    conn.execute(
        "INSERT INTO admin_audit_log "
        "(admin_user_id, action, entity_type, entity_id, details_json) "
        "VALUES (?, ?, ?, ?, CAST(? AS JSONB))",
        (admin_id, action, entity_type, str(entity_id) if entity_id is not None else None,
         json.dumps(details or {}, separators=(",", ":"), ensure_ascii=True)))


def _finish(conn):
    conn.commit()
    conn.close()
    pgdb.release()


def _fail(conn):
    conn.rollback()
    conn.close()
    pgdb.release()


def dashboard():
    conn = _connection()
    try:
        shop_payments._release_expired(conn)
        conn.commit()
        today = conn.execute(
            "SELECT COUNT(*) AS order_count, "
            "COALESCE(SUM(total_paise) FILTER (WHERE payment_status = 'paid'), 0) AS revenue_paise "
            "FROM orders WHERE created_at >= "
            "(date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata')").fetchone()
        waiting = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'paid'").fetchone()[0]
        threshold = int({
            row["key"]: row["value"] for row in conn.execute(
                "SELECT key, value FROM store_settings WHERE key = 'low_stock_threshold'")
        }.get("low_stock_threshold", "5"))
        low_stock = conn.execute(
            "SELECT id, name, stock_qty FROM products WHERE is_active = TRUE AND track_stock = TRUE "
            "AND stock_qty <= ? UNION ALL "
            "SELECT p.id, p.name || ' · ' || v.label AS name, v.stock_qty FROM product_variants v "
            "JOIN products p ON p.id = v.product_id WHERE p.is_active = TRUE AND v.stock_qty <= ? "
            "ORDER BY stock_qty, name LIMIT 10", (threshold, threshold)).fetchall()
        sales = conn.execute(
            "SELECT date_trunc('day', created_at)::date AS day, "
            "COALESCE(SUM(total_paise), 0) AS revenue_paise "
            "FROM orders WHERE payment_status = 'paid' AND created_at >= "
            "((date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata') "
            "- interval '6 days') "
            "GROUP BY 1 ORDER BY 1").fetchall()
        return {"today": dict(today), "waiting": waiting, "low_stock": [dict(row) for row in low_stock],
                "sales": [dict(row) for row in sales]}
    finally:
        conn.close()
        pgdb.release()


def categories():
    conn = _connection()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT id, name, slug, sort_order, is_active FROM categories ORDER BY sort_order, name")]
    finally:
        conn.close()
        pgdb.release()


def products(search=""):
    conn = _connection()
    try:
        params = ()
        where = ""
        if search:
            where = "WHERE lower(p.name) LIKE ? OR lower(p.slug) LIKE ?"
            term = "%" + search.lower()[:80] + "%"
            params = (term, term)
        rows = conn.execute(
            "SELECT p.id, p.name, p.slug, p.price_paise, p.mrp_paise, p.stock_qty, p.track_stock, "
            "p.is_active, p.category_id, c.name AS category_name, "
            "(SELECT COUNT(*) FROM product_variants v WHERE v.product_id = p.id) AS variant_count, "
            "EXISTS (SELECT 1 FROM order_items oi WHERE oi.product_id = p.id) AS has_orders "
            "FROM products p LEFT JOIN categories c ON c.id = p.category_id " + where +
            " ORDER BY p.updated_at DESC, p.id DESC LIMIT 200", params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
        pgdb.release()


def product(product_id):
    conn = _connection()
    try:
        row = conn.execute(
            "SELECT id, name, slug, description, category_id, image_url, price_paise, mrp_paise, "
            "stock_qty, track_stock, is_active FROM products WHERE id = ?", (product_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["variants"] = [dict(variant) for variant in conn.execute(
            "SELECT id, label, price_paise, stock_qty, sku FROM product_variants "
            "WHERE product_id = ? ORDER BY id", (product_id,))]
        return result
    finally:
        conn.close()
        pgdb.release()


def save_product(admin_id, product_id, values, variants, image_data=None, image_mime=None):
    conn = _connection()
    try:
        if product_id:
            previous = conn.execute(
                "SELECT id FROM products WHERE id = ? FOR UPDATE", (product_id,)).fetchone()
            if not previous:
                raise ValueError("Product not found.")
            if image_data is not None:
                conn.execute(
                    "UPDATE products SET name = ?, slug = ?, description = ?, category_id = ?, "
                    "image_url = ?, image_data = ?, image_mime = ?, price_paise = ?, mrp_paise = ?, "
                    "stock_qty = ?, track_stock = ?, is_active = ?, updated_at = now() WHERE id = ?",
                    (*values[:4], f"/shop/product-images/{product_id}", image_data, image_mime,
                     *values[5:], product_id))
            else:
                conn.execute(
                    "UPDATE products SET name = ?, slug = ?, description = ?, category_id = ?, "
                    "image_url = ?, price_paise = ?, mrp_paise = ?, stock_qty = ?, track_stock = ?, "
                    "is_active = ?, updated_at = now() WHERE id = ?",
                    (*values[:4], values[4], *values[5:], product_id))
            existing = {row["id"] for row in conn.execute(
                "SELECT id FROM product_variants WHERE product_id = ?", (product_id,))}
        else:
            if image_data is not None:
                product_id = conn.execute(
                    "INSERT INTO products (name, slug, description, category_id, image_url, image_data, image_mime, "
                    "price_paise, mrp_paise, stock_qty, track_stock, is_active) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                    (*values[:4], image_data, image_mime, *values[5:])).fetchone()["id"]
                conn.execute("UPDATE products SET image_url = ? WHERE id = ?",
                             (f"/shop/product-images/{product_id}", product_id))
            else:
                product_id = conn.execute(
                    "INSERT INTO products (name, slug, description, category_id, image_url, price_paise, mrp_paise, "
                    "stock_qty, track_stock, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                    values).fetchone()["id"]
            existing = set()

        retained = set()
        for variant in variants:
            variant_id = variant.pop("id", None)
            if variant_id and variant_id in existing:
                conn.execute(
                    "UPDATE product_variants SET label = ?, price_paise = ?, stock_qty = ?, sku = ?, "
                    "updated_at = now() WHERE id = ? AND product_id = ?",
                    (variant["label"], variant["price_paise"], variant["stock_qty"], variant["sku"],
                     variant_id, product_id))
                retained.add(variant_id)
            else:
                new_id = conn.execute(
                    "INSERT INTO product_variants (product_id, label, price_paise, stock_qty, sku) "
                    "VALUES (?, ?, ?, ?, ?) RETURNING id",
                    (product_id, variant["label"], variant["price_paise"], variant["stock_qty"],
                     variant["sku"])).fetchone()["id"]
                retained.add(new_id)
        for remove_id in existing - retained:
            conn.execute("DELETE FROM product_variants WHERE id = ? AND product_id = ?",
                         (remove_id, product_id))
        _audit(conn, admin_id, "product.saved", "product", product_id,
               {"name": values[0], "slug": values[1]})
        _finish(conn)
        return product_id
    except Exception:
        _fail(conn)
        raise


def toggle_product(admin_id, product_id, active):
    conn = _connection()
    try:
        cursor = conn.execute(
            "UPDATE products SET is_active = ?, updated_at = now() WHERE id = ?",
            (active, product_id))
        if cursor.rowcount:
            _audit(conn, admin_id, "product.toggled", "product", product_id, {"active": bool(active)})
        _finish(conn)
        return bool(cursor.rowcount)
    except Exception:
        _fail(conn)
        raise


def quick_update_product(admin_id, product_id, price_paise, stock_qty):
    conn = _connection()
    try:
        cursor = conn.execute(
            "UPDATE products SET price_paise = ?, mrp_paise = CASE "
            "WHEN mrp_paise IS NOT NULL AND mrp_paise < ? THEN ? ELSE mrp_paise END, "
            "stock_qty = ?, updated_at = now() WHERE id = ?",
            (price_paise, price_paise, price_paise, stock_qty, product_id))
        if cursor.rowcount:
            _audit(conn, admin_id, "product.inline_updated", "product", product_id,
                   {"price_paise": price_paise, "stock_qty": stock_qty})
        _finish(conn)
        return bool(cursor.rowcount)
    except Exception:
        _fail(conn)
        raise


def bulk_products(admin_id, ids, action, value=None):
    conn = _connection()
    try:
        changed = 0
        for product_id in sorted(set(ids)):
            if action in ("activate", "deactivate"):
                cursor = conn.execute(
                    "UPDATE products SET is_active = ?, updated_at = now() WHERE id = ?",
                    (action == "activate", product_id))
            elif action == "category":
                cursor = conn.execute(
                    "UPDATE products SET category_id = ?, updated_at = now() WHERE id = ?",
                    (value, product_id))
            elif action == "stock":
                cursor = conn.execute(
                    "UPDATE products SET stock_qty = GREATEST(0, stock_qty + ?), "
                    "updated_at = now() WHERE id = ?", (value, product_id))
            else:
                raise ValueError("Choose a valid bulk action.")
            changed += cursor.rowcount
        _audit(conn, admin_id, "products.bulk_updated", "product", None,
               {"action": action, "value": value, "count": changed})
        _finish(conn)
        return changed
    except Exception:
        _fail(conn)
        raise


def save_category(admin_id, category_id, name, slug, sort_order):
    conn = _connection()
    try:
        if category_id:
            cursor = conn.execute(
                "UPDATE categories SET name = ?, slug = ?, sort_order = ?, updated_at = now() WHERE id = ?",
                (name, slug, sort_order, category_id))
            if not cursor.rowcount:
                raise ValueError("Category not found.")
            saved_id = category_id
            action = "category.updated"
        else:
            saved_id = conn.execute(
                "INSERT INTO categories (name, slug, sort_order) VALUES (?, ?, ?) RETURNING id",
                (name, slug, sort_order)).fetchone()["id"]
            action = "category.created"
        _audit(conn, admin_id, action, "category", saved_id, {"name": name, "slug": slug})
        _finish(conn)
        return saved_id
    except Exception:
        _fail(conn)
        raise


def toggle_category(admin_id, category_id, active):
    conn = _connection()
    try:
        cursor = conn.execute(
            "UPDATE categories SET is_active = ?, updated_at = now() WHERE id = ?",
            (active, category_id))
        if cursor.rowcount:
            _audit(conn, admin_id, "category.toggled", "category", category_id, {"active": bool(active)})
        _finish(conn)
        return bool(cursor.rowcount)
    except Exception:
        _fail(conn)
        raise


def orders(status="", start_date="", end_date="", search=""):
    conn = _connection()
    try:
        clauses, params = [], []
        if status:
            clauses.append("o.status = ?")
            params.append(status)
        if start_date:
            clauses.append("o.created_at >= ?::date")
            params.append(start_date)
        if end_date:
            clauses.append("o.created_at < (?::date + interval '1 day')")
            params.append(end_date)
        if search:
            clauses.append("(lower(o.order_number) LIKE ? OR o.phone LIKE ? OR lower(o.email) LIKE ?)")
            term = "%" + search.lower()[:80] + "%"
            params.extend((term, term, term))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            "SELECT o.id, o.order_number, o.status, o.payment_status, o.total_paise, o.customer_name, "
            "o.phone, o.email, o.created_at FROM orders o " + where +
            " ORDER BY o.created_at DESC LIMIT 300", tuple(params)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
        pgdb.release()


def order(order_id):
    conn = _connection()
    try:
        row = conn.execute(
            "SELECT o.*, u.shop_blocked FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = ?",
            (order_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["items"] = [dict(item) for item in conn.execute(
            "SELECT oi.*, p.slug FROM order_items oi JOIN products p ON p.id = oi.product_id "
            "WHERE oi.order_id = ? ORDER BY oi.id", (order_id,))]
        result["history"] = [dict(item) for item in conn.execute(
            "SELECT h.*, u.name AS changed_by_name FROM order_status_history h "
            "LEFT JOIN users u ON u.id = h.changed_by WHERE h.order_id = ? ORDER BY h.created_at, h.id",
            (order_id,))]
        return result
    finally:
        conn.close()
        pgdb.release()


def update_order(admin_id, order_id, status=None, note=None):
    allowed = {
        "paid": {"packed"}, "packed": {"handed_to_courier"},
        "handed_to_courier": {"delivered"},
    }
    conn = _connection()
    try:
        current = conn.execute("SELECT status FROM orders WHERE id = ? FOR UPDATE", (order_id,)).fetchone()
        if not current:
            raise ValueError("Order not found.")
        changed = False
        previous = current["status"]
        if status:
            if status not in allowed.get(previous, set()):
                raise ValueError("That order status transition is not allowed.")
            conn.execute("UPDATE orders SET status = ?, updated_at = now() WHERE id = ?",
                         (status, order_id))
            conn.execute(
                "INSERT INTO order_status_history (order_id, from_status, to_status, changed_by, note) "
                "VALUES (?, ?, ?, ?, ?)", (order_id, previous, status, admin_id, ""))
            _audit(conn, admin_id, "order.status_changed", "order", order_id,
                   {"from": previous, "to": status})
            changed = True
        if note is not None:
            conn.execute("UPDATE orders SET admin_notes = ?, updated_at = now() WHERE id = ?",
                         (note, order_id))
            _audit(conn, admin_id, "order.note_updated", "order", order_id, {"note_length": len(note)})
            changed = True
        _finish(conn)
        return changed
    except Exception:
        _fail(conn)
        raise


def cancel_unpaid(admin_id, order_id):
    conn = _connection()
    try:
        current = conn.execute(
            "SELECT status, payment_status FROM orders WHERE id = ? FOR UPDATE", (order_id,)).fetchone()
        if not current or current["status"] != "pending_payment":
            raise ValueError("Only unpaid pending orders can be cancelled.")
        conn.execute(
            "UPDATE orders SET status = 'cancelled', payment_status = 'failed', updated_at = now() WHERE id = ?",
            (order_id,))
        shop_payments._release_order(conn, order_id)
        conn.execute(
            "INSERT INTO order_status_history (order_id, from_status, to_status, changed_by, note) "
            "VALUES (?, 'pending_payment', 'cancelled', ?, 'Cancelled by shop admin.')",
            (order_id, admin_id))
        _audit(conn, admin_id, "order.cancelled", "order", order_id)
        _finish(conn)
        return True
    except Exception:
        _fail(conn)
        raise


def mark_refunded(admin_id, order_id, payment_id, refund_id):
    conn = _connection()
    try:
        current = conn.execute(
            "SELECT status, payment_status FROM orders WHERE id = ? FOR UPDATE", (order_id,)).fetchone()
        if not current:
            raise ValueError("Order not found.")
        if current["status"] == "refunded":
            _finish(conn)
            return False
        conn.execute(
            "UPDATE orders SET status = 'refunded', payment_status = 'refunded', updated_at = now() "
            "WHERE id = ?", (order_id,))
        conn.execute(
            "INSERT INTO order_status_history (order_id, from_status, to_status, changed_by, note) "
            "VALUES (?, ?, 'refunded', ?, ?)",
            (order_id, current["status"], admin_id, "Razorpay refund " + refund_id))
        _audit(conn, admin_id, "order.refunded", "order", order_id, {"refund_id": refund_id})
        _finish(conn)
        return True
    except Exception:
        _fail(conn)
        raise


def coupons():
    conn = _connection()
    try:
        rows = conn.execute(
            "SELECT id, code, type, value, min_order_paise, max_discount_paise, usage_limit, "
            "per_user_limit, used_count, starts_at, ends_at, is_active, "
            "(NOT is_active OR (starts_at IS NOT NULL AND starts_at > now()) OR "
            "(ends_at IS NOT NULL AND ends_at <= now()) OR "
            "(usage_limit IS NOT NULL AND used_count >= usage_limit)) AS unavailable "
            "FROM shop_coupons ORDER BY created_at DESC LIMIT 300")
        return [dict(row) for row in rows]
    finally:
        conn.close()
        pgdb.release()


def save_coupon(admin_id, coupon_id, values):
    conn = _connection()
    try:
        if coupon_id:
            cursor = conn.execute(
                "UPDATE shop_coupons SET code = ?, type = ?, value = ?, min_order_paise = ?, "
                "max_discount_paise = ?, usage_limit = ?, per_user_limit = ?, starts_at = ?, "
                "ends_at = ?, updated_at = now() WHERE id = ?",
                (*values, coupon_id))
            if not cursor.rowcount:
                raise ValueError("Coupon not found.")
            saved_id, action = coupon_id, "coupon.updated"
        else:
            saved_id = conn.execute(
                "INSERT INTO shop_coupons (code, type, value, min_order_paise, max_discount_paise, "
                "usage_limit, per_user_limit, starts_at, ends_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id", values).fetchone()["id"]
            action = "coupon.created"
        _audit(conn, admin_id, action, "coupon", saved_id, {"code": values[0]})
        _finish(conn)
        return saved_id
    except Exception:
        _fail(conn)
        raise


def toggle_coupon(admin_id, coupon_id, active):
    conn = _connection()
    try:
        cursor = conn.execute(
            "UPDATE shop_coupons SET is_active = ?, updated_at = now() WHERE id = ?",
            (active, coupon_id))
        if cursor.rowcount:
            _audit(conn, admin_id, "coupon.toggled", "coupon", coupon_id, {"active": bool(active)})
        _finish(conn)
        return bool(cursor.rowcount)
    except Exception:
        _fail(conn)
        raise


def customers(search=""):
    conn = _connection()
    try:
        params = ()
        where = ""
        if search:
            term = "%" + search.lower()[:80] + "%"
            where = "AND (lower(u.name) LIKE ? OR lower(u.email) LIKE ? OR u.phone LIKE ?)"
            params = (term, term, term)
        rows = conn.execute(
            "SELECT u.id, u.name, u.email, u.phone, u.shop_blocked, COUNT(o.id) AS order_count, "
            "COALESCE(SUM(o.total_paise) FILTER (WHERE o.payment_status = 'paid'), 0) AS total_spent_paise, "
            "MAX(o.created_at) AS last_order_at FROM users u LEFT JOIN orders o ON o.user_id = u.id "
            "WHERE u.role = 'customer' " + where +
            " GROUP BY u.id ORDER BY last_order_at DESC NULLS LAST, u.created_at DESC LIMIT 300", params)
        return [dict(row) for row in rows]
    finally:
        conn.close()
        pgdb.release()


def customer(user_id):
    conn = _connection()
    try:
        user = conn.execute(
            "SELECT id, name, email, phone, shop_blocked, created_at FROM users "
            "WHERE id = ? AND role = 'customer'", (user_id,)).fetchone()
        if not user:
            return None
        result = dict(user)
        result["orders"] = [dict(row) for row in conn.execute(
            "SELECT id, order_number, status, payment_status, total_paise, created_at "
            "FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 100", (user_id,))]
        return result
    finally:
        conn.close()
        pgdb.release()


def block_customer(admin_id, user_id, blocked):
    conn = _connection()
    try:
        cursor = conn.execute(
            "UPDATE users SET shop_blocked = ? WHERE id = ? AND role = 'customer'",
            (blocked, user_id))
        if cursor.rowcount:
            _audit(conn, admin_id, "customer.blocked" if blocked else "customer.unblocked",
                   "customer", user_id, {"blocked": bool(blocked)})
        _finish(conn)
        return bool(cursor.rowcount)
    except Exception:
        _fail(conn)
        raise


def settings():
    conn = _connection()
    try:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM store_settings")}
    finally:
        conn.close()
        pgdb.release()


def save_settings(admin_id, values):
    conn = _connection()
    try:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO store_settings (key, value, updated_at) VALUES (?, ?, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (key, str(value)))
        _audit(conn, admin_id, "settings.updated", "store_settings", None,
               {"keys": sorted(values)})
        _finish(conn)
    except Exception:
        _fail(conn)
        raise


def report(start_date, end_date):
    conn = _connection()
    try:
        params = (start_date, end_date)
        sales = [dict(row) for row in conn.execute(
            "SELECT date_trunc('day', created_at)::date AS day, COUNT(*) AS order_count, "
            "SUM(total_paise) AS revenue_paise FROM orders WHERE payment_status = 'paid' "
            "AND created_at >= ?::date AND created_at < (?::date + interval '1 day') "
            "GROUP BY 1 ORDER BY 1", params)]
        top_products = [dict(row) for row in conn.execute(
            "SELECT oi.product_name_snapshot AS name, SUM(oi.quantity) AS quantity, "
            "SUM(oi.line_total_paise) AS revenue_paise FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.payment_status = 'paid' AND o.created_at >= ?::date "
            "AND o.created_at < (?::date + interval '1 day') GROUP BY 1 ORDER BY quantity DESC LIMIT 20",
            params)]
        coupon_usage = [dict(row) for row in conn.execute(
            "SELECT c.code, COUNT(o.id) AS redemptions, "
            "COALESCE(SUM(o.discount_paise), 0) AS discount_paise "
            "FROM shop_coupons c LEFT JOIN shop_coupon_redemptions cr ON cr.coupon_id = c.id "
            "LEFT JOIN orders o ON o.id = cr.order_id AND o.created_at >= ?::date "
            "AND o.created_at < (?::date + interval '1 day') GROUP BY c.id ORDER BY redemptions DESC",
            params)]
        return {"sales": sales, "top_products": top_products, "coupon_usage": coupon_usage}
    finally:
        conn.close()
        pgdb.release()


def export_orders(start_date, end_date):
    conn = _connection()
    try:
        rows = conn.execute(
            "SELECT order_number, status, payment_status, total_paise, customer_name, phone, email, "
            "city, state, pincode, created_at FROM orders WHERE created_at >= ?::date "
            "AND created_at < (?::date + interval '1 day') ORDER BY created_at", (start_date, end_date))
        return [dict(row) for row in rows]
    finally:
        conn.close()
        pgdb.release()


def product_image(product_id):
    conn = _connection()
    try:
        row = conn.execute(
            "SELECT image_data, image_mime FROM products WHERE id = ?", (product_id,)).fetchone()
        if not row or not row["image_data"] or row["image_mime"] not in (
                "image/jpeg", "image/png", "image/webp"):
            return None
        return bytes(row["image_data"]), row["image_mime"]
    finally:
        conn.close()
        pgdb.release()
