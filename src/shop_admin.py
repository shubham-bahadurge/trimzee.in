"""Private shop owner administration routes."""
import csv
import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from flask import abort, redirect, render_template, request, send_file, session, url_for

import db
import shop_admin_store
import shop_notifications
import shop_payments

logger = logging.getLogger(__name__)
STATUSES = ("pending_payment", "paid", "packed", "handed_to_courier",
            "delivered", "cancelled", "refunded")
LOCAL_TZ = ZoneInfo("Asia/Kolkata")


def register_routes(app, current_user):
    def admin_user():
        user = current_user()
        if not user:
            return None, redirect("/login?next=" + request.path)
        if user["role"] != "admin":
            abort(403)
        return user, None

    def notice(message, error=False):
        session["shop_admin_notice"] = {"message": message, "error": error}

    def take_notice():
        return session.pop("shop_admin_notice", None)

    def money_to_paise(value, field, optional=False):
        value = (value or "").strip()
        if optional and not value:
            return None
        try:
            amount = Decimal(value)
            if not amount.is_finite() or amount < 0:
                raise ValueError(f"{field} must be a non-negative amount with at most two decimals.")
            if amount > Decimal("92233720368547758.07"):
                raise ValueError(f"{field} is too large.")
            rounded = amount.quantize(Decimal("0.01"))
        except InvalidOperation:
            raise ValueError(f"Enter a valid {field}.") from None
        if rounded != amount:
            raise ValueError(f"{field} must be a non-negative amount with at most two decimals.")
        paise = int((rounded * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if paise > 9_223_372_036_854_775_807:
            raise ValueError(f"{field} is too large.")
        return paise

    def parse_date(value, field):
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError(f"Choose a valid {field} date.") from None

    def admin_render(template, **context):
        return render_template(template, notice=take_notice(), **context)

    def checked_ids():
        ids = []
        for value in request.form.getlist("product_ids"):
            if not value.isdigit() or len(value) > 18:
                raise ValueError("Select valid products.")
            ids.append(int(value))
        if not ids:
            raise ValueError("Select at least one product.")
        return ids

    def image_upload():
        upload = request.files.get("image")
        if not upload or not upload.filename:
            return None, None
        data = upload.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            raise ValueError("Product images must be 5 MB or smaller.")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ValueError("Upload a valid JPEG, PNG, or WebP image.")
        return data, mime

    @app.route("/admin/shop")
    def shop_dashboard():
        user, response = admin_user()
        if response:
            return response
        data = shop_admin_store.dashboard()
        data["max_sales"] = max((row["revenue_paise"] for row in data["sales"]), default=0)
        return admin_render("admin_shop.html", dashboard=data)

    @app.route("/admin/shop/products")
    def product_list():
        user, response = admin_user()
        if response:
            return response
        query = (request.args.get("q") or "").strip()[:80]
        return admin_render("admin_shop_products.html", products=shop_admin_store.products(query),
                            categories=shop_admin_store.categories(), search=query)

    @app.route("/admin/shop/products/new", methods=["GET", "POST"])
    @app.route("/admin/shop/products/<int:product_id>/edit", methods=["GET", "POST"])
    def product_edit(product_id=None):
        user, response = admin_user()
        if response:
            return response
        item = shop_admin_store.product(product_id) if product_id else None
        if product_id and not item:
            abort(404)
        if request.method == "POST":
            try:
                name = (request.form.get("name") or "").strip()
                slug = (request.form.get("slug") or "").strip().lower()
                description = (request.form.get("description") or "").strip()
                category_value = (request.form.get("category_id") or "").strip()
                image_url = (request.form.get("image_url") or "").strip()
                if not name or len(name) > 160 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 180:
                    raise ValueError("Provide a product name and a valid lowercase URL slug.")
                if len(description) > 10000:
                    raise ValueError("Description must be 10,000 characters or fewer.")
                category_id = int(category_value) if category_value else None
                category_ids = {row["id"] for row in shop_admin_store.categories()}
                if category_id is not None and category_id not in category_ids:
                    raise ValueError("Choose a valid category.")
                if image_url and (len(image_url) > 500 or "\r" in image_url or "\n" in image_url
                                  or "\\" in image_url or image_url.startswith("//")
                                  or not (image_url.startswith("/") or image_url.startswith("https://"))):
                    raise ValueError("Image URL must be a site path or HTTPS URL.")
                price = money_to_paise(request.form.get("price"), "price")
                mrp = money_to_paise(request.form.get("mrp"), "MRP", optional=True)
                stock = int(request.form.get("stock_qty", ""))
                if stock < 0 or stock > 2_147_483_647:
                    raise ValueError("Stock must be a non-negative whole number.")
                if mrp is not None and mrp < price:
                    raise ValueError("MRP must be equal to or higher than the selling price.")
                variants = []
                labels = request.form.getlist("variant_label")
                prices = request.form.getlist("variant_price")
                stocks = request.form.getlist("variant_stock")
                skus = request.form.getlist("variant_sku")
                variant_ids = request.form.getlist("variant_id")
                if not (len(labels) == len(prices) == len(stocks) == len(skus) == len(variant_ids)):
                    raise ValueError("Variant fields do not match.")
                seen_labels = set()
                for values in zip(labels, prices, stocks, skus, variant_ids):
                    label, variant_price, variant_stock, sku, variant_id = values
                    label = label.strip()
                    if not label and not variant_price and not variant_stock and not sku and not variant_id:
                        continue
                    if not label or len(label) > 80 or label.lower() in seen_labels:
                        raise ValueError("Variant labels must be unique and 1-80 characters.")
                    seen_labels.add(label.lower())
                    sku = sku.strip()
                    if len(sku) > 100 or any(ord(char) < 32 for char in sku):
                        raise ValueError("Variant SKU must be 100 characters or fewer.")
                    try:
                        variant_stock = int(variant_stock)
                        variant_id = int(variant_id) if variant_id else None
                    except ValueError:
                        raise ValueError("Variant stock or ID is invalid.") from None
                    if variant_stock < 0 or variant_stock > 2_147_483_647:
                        raise ValueError("Variant stock must be a non-negative whole number.")
                    variants.append({"id": variant_id, "label": label,
                                     "price_paise": money_to_paise(variant_price, "variant price"),
                                     "stock_qty": variant_stock, "sku": sku or None})
                if len(variants) > 20:
                    raise ValueError("A product can have up to 20 variants.")
                uploaded, mime = image_upload()
                if not uploaded and image_url:
                    image_to_save = image_url
                elif not uploaded and item:
                    image_to_save = item["image_url"]
                else:
                    image_to_save = None
                values = (name, slug, description, category_id, image_to_save, price, mrp,
                          stock, request.form.get("track_stock") == "on",
                          request.form.get("is_active") == "on")
                saved_id = shop_admin_store.save_product(
                    user["id"], product_id, values, variants, uploaded, mime)
                notice("Product saved.")
                return redirect(url_for("product_list"))
            except ValueError as error:
                notice(str(error), error=True)
            except Exception:
                logger.exception("Could not save shop product")
                notice("Could not save the product. Check its slug, SKU, and category for duplicates.",
                       error=True)
        categories = shop_admin_store.categories()
        if request.method == "POST":
            item = {
                "name": request.form.get("name", ""),
                "slug": request.form.get("slug", ""),
                "description": request.form.get("description", ""),
                "category_id": request.form.get("category_id", ""),
                "image_url": request.form.get("image_url", ""),
                "price_paise": request.form.get("price", ""),
                "mrp_paise": request.form.get("mrp", ""),
                "stock_qty": request.form.get("stock_qty", "0"),
                "track_stock": request.form.get("track_stock") == "on",
                "is_active": request.form.get("is_active") == "on",
                "variants": [],
            }
        return admin_render("admin_shop_product_form.html", item=item, categories=categories,
                            editing=bool(product_id))

    @app.route("/admin/shop/products/<int:product_id>/toggle", methods=["POST"])
    def product_toggle(product_id):
        user, response = admin_user()
        if response:
            return response
        active = request.form.get("active") == "1"
        try:
            if not shop_admin_store.toggle_product(user["id"], product_id, active):
                abort(404)
            notice("Product updated.")
        except Exception:
            logger.exception("Could not toggle shop product")
            notice("Could not update product.", error=True)
        return redirect(url_for("product_list"))

    @app.route("/admin/shop/products/<int:product_id>/quick-update", methods=["POST"])
    def product_quick_update(product_id):
        user, response = admin_user()
        if response:
            return response
        try:
            price = money_to_paise(request.form.get("price"), "price")
            stock = int(request.form.get("stock_qty", ""))
            if stock < 0 or stock > 2_147_483_647:
                raise ValueError("Stock must be a non-negative whole number.")
            if not shop_admin_store.quick_update_product(user["id"], product_id, price, stock):
                abort(404)
            notice("Price and stock updated.")
        except ValueError as error:
            notice(str(error), error=True)
        except Exception:
            logger.exception("Could not quickly update shop product")
            notice("Could not update price and stock.", error=True)
        return redirect(url_for("product_list"))

    @app.route("/admin/shop/products/bulk", methods=["POST"])
    def product_bulk():
        user, response = admin_user()
        if response:
            return response
        try:
            ids = checked_ids()
            action = request.form.get("action", "")
            value = None
            if action == "category":
                value = int(request.form.get("category_id", ""))
                if value not in {row["id"] for row in shop_admin_store.categories()}:
                    raise ValueError("Choose a valid category.")
            elif action == "stock":
                direction = request.form.get("direction")
                amount = int(request.form.get("stock_adjustment", ""))
                if direction not in ("increase", "decrease") or amount < 1:
                    raise ValueError("Enter a positive stock adjustment.")
                value = amount if direction == "increase" else -amount
            if action not in ("activate", "deactivate", "category", "stock"):
                raise ValueError("Choose a valid bulk action.")
            changed = shop_admin_store.bulk_products(user["id"], ids, action, value)
            notice(f"Updated {changed} product(s).")
        except ValueError as error:
            notice(str(error), error=True)
        except Exception:
            logger.exception("Could not apply shop product bulk action")
            notice("Could not update the selected products.", error=True)
        return redirect(url_for("product_list"))

    @app.route("/shop/product-images/<int:product_id>")
    def product_image(product_id):
        image = shop_admin_store.product_image(product_id)
        if not image:
            abort(404)
        response = app.response_class(image[0], mimetype=image[1])
        response.headers["Cache-Control"] = "public, max-age=86400"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/admin/shop/categories", methods=["GET", "POST"])
    def category_list():
        user, response = admin_user()
        if response:
            return response
        if request.method == "POST":
            try:
                category_id = request.form.get("category_id", "").strip()
                category_id = int(category_id) if category_id else None
                name = (request.form.get("name") or "").strip()
                slug = (request.form.get("slug") or "").strip().lower()
                sort_order = int(request.form.get("sort_order", "0"))
                if not name or len(name) > 100 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                    raise ValueError("Enter a category name and valid lowercase slug.")
                if sort_order < 0:
                    raise ValueError("Sort order must be zero or greater.")
                shop_admin_store.save_category(user["id"], category_id, name, slug, sort_order)
                notice("Category saved.")
                return redirect(url_for("category_list"))
            except ValueError as error:
                notice(str(error), error=True)
            except Exception:
                logger.exception("Could not save shop category")
                notice("Could not save the category. Its slug may already exist.", error=True)
        return admin_render("admin_shop_categories.html",
                            categories=shop_admin_store.categories())

    @app.route("/admin/shop/categories/<int:category_id>/toggle", methods=["POST"])
    def category_toggle(category_id):
        user, response = admin_user()
        if response:
            return response
        try:
            if not shop_admin_store.toggle_category(
                    user["id"], category_id, request.form.get("active") == "1"):
                abort(404)
            notice("Category visibility updated.")
        except Exception:
            logger.exception("Could not toggle shop category")
            notice("Could not update category visibility.", error=True)
        return redirect(url_for("category_list"))

    @app.route("/admin/shop/orders")
    def order_list():
        user, response = admin_user()
        if response:
            return response
        status = (request.args.get("status") or "").strip()
        if status and status not in STATUSES:
            abort(400)
        start = (request.args.get("start") or "").strip()
        end = (request.args.get("end") or "").strip()
        search = (request.args.get("q") or "").strip()[:80]
        try:
            if start:
                parse_date(start, "start")
            if end:
                parse_date(end, "end")
        except ValueError:
            abort(400)
        if start and end and end < start:
            abort(400)
        return admin_render("admin_shop_orders.html", orders=shop_admin_store.orders(status, start, end, search),
                            statuses=STATUSES, status=status, start=start, end=end, search=search)

    @app.route("/admin/shop/orders/<int:order_id>")
    def order_detail(order_id):
        user, response = admin_user()
        if response:
            return response
        order = shop_admin_store.order(order_id)
        if not order:
            abort(404)
        allowed_next = {"paid": "packed", "packed": "handed_to_courier",
                        "handed_to_courier": "delivered"}.get(order["status"])
        refund = None
        conn = db.get_db()
        try:
            row = conn.execute(
                "SELECT status, razorpay_refund_id FROM shop_refund_requests WHERE order_id = ?",
                (order_id,)).fetchone()
            refund = dict(row) if row else None
        finally:
            conn.close()
            import pgdb
            pgdb.release()
        return admin_render("admin_shop_order.html", order=order, allowed_next=allowed_next, refund=refund)

    @app.route("/admin/shop/orders/<int:order_id>/status", methods=["POST"])
    def order_update(order_id):
        user, response = admin_user()
        if response:
            return response
        status = request.form.get("status", "").strip()
        note = request.form.get("admin_notes")
        try:
            order = shop_admin_store.order(order_id)
            if not order:
                abort(404)
            shop_admin_store.update_order(
                user["id"], order_id, status=status or None,
                note=(note.strip()[:4000] if note is not None else None))
            if status in ("handed_to_courier", "cancelled"):
                shop_notifications.status_changed(order_id, status)
            notice("Order updated.")
        except ValueError as error:
            notice(str(error), error=True)
        except Exception:
            logger.exception("Could not update shop order")
            notice("Could not update this order.", error=True)
        return redirect(url_for("order_detail", order_id=order_id))

    @app.route("/admin/shop/orders/<int:order_id>/cancel", methods=["POST"])
    def order_cancel(order_id):
        user, response = admin_user()
        if response:
            return response
        if request.form.get("confirm") != "yes":
            abort(400)
        try:
            shop_admin_store.cancel_unpaid(user["id"], order_id)
            shop_notifications.status_changed(order_id, "cancelled")
            notice("Unpaid order cancelled.")
        except ValueError as error:
            notice(str(error), error=True)
        except Exception:
            logger.exception("Could not cancel unpaid shop order")
            notice("Could not cancel this order.", error=True)
        return redirect(url_for("order_detail", order_id=order_id))

    @app.route("/admin/shop/orders/<int:order_id>/refund", methods=["GET", "POST"])
    def order_refund(order_id):
        user, response = admin_user()
        if response:
            return response
        order = shop_admin_store.order(order_id)
        if not order:
            abort(404)
        if request.method == "GET":
            if order["payment_status"] != "paid":
                abort(400)
            return admin_render("admin_shop_refund_confirm.html", order=order)
        if request.form.get("confirm") != "yes":
            abort(400)
        try:
            refund_id = shop_payments.request_admin_refund(order_id, user["id"])
            shop_admin_store.mark_refunded(user["id"], order_id, order["razorpay_payment_id"], refund_id)
            notice("Refund submitted to Razorpay.")
        except shop_payments.PaymentError as error:
            notice(str(error), error=True)
        except ValueError as error:
            notice(str(error), error=True)
        except Exception:
            logger.exception("Could not process shop refund")
            notice("Could not process the refund. Check Razorpay before retrying.", error=True)
        return redirect(url_for("order_detail", order_id=order_id))

    @app.route("/admin/shop/coupons")
    def coupon_list():
        user, response = admin_user()
        if response:
            return response
        return admin_render("admin_shop_coupons.html", coupons=shop_admin_store.coupons())

    @app.route("/admin/shop/coupons/new", methods=["GET", "POST"])
    @app.route("/admin/shop/coupons/<int:coupon_id>/edit", methods=["GET", "POST"])
    def coupon_edit(coupon_id=None):
        user, response = admin_user()
        if response:
            return response
        coupon = None
        if coupon_id:
            coupon = next((row for row in shop_admin_store.coupons() if row["id"] == coupon_id), None)
            if not coupon:
                abort(404)
        if request.method == "POST":
            try:
                code = (request.form.get("code") or "").strip().upper()
                kind = request.form.get("type", "")
                raw_value = request.form.get("value", "").strip()
                if not re.fullmatch(r"[A-Z0-9_-]{2,40}", code):
                    raise ValueError("Coupon code must use 2-40 letters, numbers, hyphens, or underscores.")
                if kind not in ("percent", "flat"):
                    raise ValueError("Choose percent or flat discount.")
                value = int(raw_value) if kind == "percent" else money_to_paise(raw_value, "discount")
                if kind == "percent" and not 1 <= value <= 100:
                    raise ValueError("Percent discount must be between 1 and 100.")
                if kind == "flat" and value <= 0:
                    raise ValueError("Flat discount must be greater than zero.")
                if kind == "flat" and value > 2_147_483_647:
                    raise ValueError("Flat discount is too large.")
                minimum = money_to_paise(request.form.get("min_order", "0"), "minimum order")
                maximum = money_to_paise(request.form.get("max_discount", ""), "maximum discount", optional=True)
                usage = request.form.get("usage_limit", "").strip()
                per_user = request.form.get("per_user_limit", "").strip()
                usage = int(usage) if usage else None
                per_user = int(per_user) if per_user else None
                if usage is not None and usage < 1 or per_user is not None and per_user < 1:
                    raise ValueError("Usage limits must be positive whole numbers.")

                def to_datetime(field):
                    value = (request.form.get(field) or "").strip()
                    if not value:
                        return None
                    parsed = datetime.fromisoformat(value)
                    return parsed.replace(tzinfo=LOCAL_TZ) if parsed.tzinfo is None else parsed

                starts = to_datetime("starts_at")
                ends = to_datetime("ends_at")
                if starts and ends and ends <= starts:
                    raise ValueError("Coupon end date must be after its start date.")
                values = (code, kind, value, minimum, maximum, usage, per_user, starts, ends)
                shop_admin_store.save_coupon(user["id"], coupon_id, values)
                notice("Coupon saved.")
                return redirect(url_for("coupon_list"))
            except (ValueError, InvalidOperation) as error:
                notice(str(error) or "Enter valid coupon settings.", error=True)
            except Exception:
                logger.exception("Could not save shop coupon")
                notice("Could not save coupon. Its code may already exist.", error=True)
        return admin_render("admin_shop_coupon_form.html", coupon=coupon)

    @app.route("/admin/shop/coupons/<int:coupon_id>/toggle", methods=["POST"])
    def coupon_toggle(coupon_id):
        user, response = admin_user()
        if response:
            return response
        try:
            if not shop_admin_store.toggle_coupon(
                    user["id"], coupon_id, request.form.get("active") == "1"):
                abort(404)
            notice("Coupon updated.")
        except Exception:
            logger.exception("Could not toggle shop coupon")
            notice("Could not update coupon.", error=True)
        return redirect(url_for("coupon_list"))

    @app.route("/admin/shop/customers")
    def customer_list():
        user, response = admin_user()
        if response:
            return response
        search = (request.args.get("q") or "").strip()[:80]
        return admin_render("admin_shop_customers.html",
                            customers=shop_admin_store.customers(search), search=search)

    @app.route("/admin/shop/customers/<int:customer_id>")
    def customer_detail(customer_id):
        user, response = admin_user()
        if response:
            return response
        customer = shop_admin_store.customer(customer_id)
        if not customer:
            abort(404)
        return admin_render("admin_shop_customer.html", customer=customer)

    @app.route("/admin/shop/customers/<int:customer_id>/block", methods=["POST"])
    def customer_block(customer_id):
        user, response = admin_user()
        if response:
            return response
        try:
            if not shop_admin_store.block_customer(
                    user["id"], customer_id, request.form.get("blocked") == "1"):
                abort(404)
            notice("Customer ordering access updated.")
        except Exception:
            logger.exception("Could not update shop customer access")
            notice("Could not update customer access.", error=True)
        return redirect(url_for("customer_detail", customer_id=customer_id))

    @app.route("/admin/shop/reports")
    def reports():
        user, response = admin_user()
        if response:
            return response
        today = date.today()
        start = request.args.get("start", (today.replace(day=1)).isoformat())
        end = request.args.get("end", today.isoformat())
        try:
            start_date, end_date = parse_date(start, "start"), parse_date(end, "end")
        except ValueError:
            abort(400)
        if end_date < start_date:
            abort(400)
        return admin_render("admin_shop_reports.html", report=shop_admin_store.report(start, end),
                            start=start, end=end)

    @app.route("/admin/shop/reports/orders.csv")
    def report_csv():
        user, response = admin_user()
        if response:
            return response
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        try:
            start_date, end_date = parse_date(start, "start"), parse_date(end, "end")
        except ValueError:
            abort(400)
        if end_date < start_date:
            abort(400)
        rows = shop_admin_store.export_orders(start, end)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(("Order number", "Status", "Payment status", "Total paise", "Customer",
                         "Phone", "Email", "City", "State", "PIN", "Created"))
        for row in rows:
            values = [row["order_number"], row["status"], row["payment_status"], row["total_paise"],
                      row["customer_name"], row["phone"], row["email"], row["city"], row["state"],
                      row["pincode"], row["created_at"]]
            writer.writerow(["'" + str(value) if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                             else value for value in values])
        return send_file(io.BytesIO(stream.getvalue().encode("utf-8-sig")), mimetype="text/csv",
                         as_attachment=True, download_name=f"trimzee-orders-{start}-to-{end}.csv")

    @app.route("/admin/shop/settings", methods=["GET", "POST"])
    def settings():
        user, response = admin_user()
        if response:
            return response
        if request.method == "POST":
            try:
                name = (request.form.get("store_name") or "").strip()
                phone = (request.form.get("support_phone") or "").strip()
                email = (request.form.get("support_email") or "").strip().lower()
                if not name or len(name) > 120 or len(phone) > 30 or len(email) > 160:
                    raise ValueError("Enter a store name and valid support contact details.")
                if email and not db.EMAIL_RE.fullmatch(email):
                    raise ValueError("Enter a valid support email.")
                gst = Decimal(request.form.get("gst_percent", "0"))
                if not gst.is_finite() or not 0 <= gst <= 100:
                    raise ValueError("GST must be between 0 and 100 percent.")
                shipping = money_to_paise(request.form.get("free_shipping_threshold", "0"),
                                          "free shipping threshold")
                low_stock = int(request.form.get("low_stock_threshold", "5"))
                if low_stock < 0 or low_stock > 2_147_483_647:
                    raise ValueError("Low-stock threshold must be a non-negative whole number.")
                values = {
                    "store_open": "true" if request.form.get("store_open") == "on" else "false",
                    "store_name": name,
                    "support_phone": phone,
                    "support_email": email,
                    "gst_percent": str(gst),
                    "free_shipping_threshold_paise": str(shipping),
                    "low_stock_threshold": str(low_stock),
                }
                shop_admin_store.save_settings(user["id"], values)
                notice("Shop settings saved.")
                return redirect(url_for("settings"))
            except (ValueError, InvalidOperation) as error:
                notice(str(error) or "Enter valid shop settings.", error=True)
            except Exception:
                logger.exception("Could not save shop settings")
                notice("Could not save shop settings.", error=True)
        return admin_render("admin_shop_settings.html", settings=shop_admin_store.settings())
