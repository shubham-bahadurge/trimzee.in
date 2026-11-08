"""Order emails routed through the existing mailer."""
import logging

import db
import mailer
import shop_store

logger = logging.getLogger(__name__)


def _rupees(paise):
    whole, fraction = divmod(int(paise), 100)
    return f"Rs {whole:,}.{fraction:02d}"


def paid_order(order_number):
    conn = db.get_db()
    try:
        order = conn.execute(
            "SELECT customer_name, email, total_paise FROM orders WHERE order_number = ?",
            (order_number,)).fetchone()
        support_email = shop_store.settings(conn).get("support_email", "").strip()
        conn.commit()
    finally:
        conn.close()
    if not order:
        logger.error("Paid shop order notification could not find the order")
        return
    if not mailer.email_configured():
        logger.warning("Shop order email notifications skipped because SMTP is not configured")
        return

    subject = f"Trimzee order {order_number} confirmed"
    body = (f"Hi {order['customer_name']},\n\n"
            f"Your Trimzee shop order {order_number} has been paid and confirmed.\n"
            f"Order total: {_rupees(order['total_paise'])}\n\n"
            "We will update you when the order is handed to our delivery team.\n"
            "Thank you for shopping with Trimzee.")
    recipients = [order["email"]]
    if support_email and support_email.lower() != order["email"].lower():
        recipients.append(support_email)
    elif not support_email:
        logger.warning("Shop admin order email skipped because support_email is not configured")
    for recipient in recipients:
        try:
            mailer.send_email(recipient, subject, body)
        except Exception:
            logger.exception("Could not send a shop order email notification")


def status_changed(order_id, status):
    if status not in ("handed_to_courier", "cancelled"):
        return
    conn = db.get_db()
    try:
        order = conn.execute(
            "SELECT order_number, customer_name, email, total_paise FROM orders WHERE id = ?",
            (order_id,)).fetchone()
        conn.commit()
    finally:
        conn.close()
    if not order:
        logger.error("Shop status notification could not find the order")
        return
    if not mailer.email_configured():
        logger.warning("Shop status email skipped because SMTP is not configured")
        return
    if status == "handed_to_courier":
        subject = f"Trimzee order {order['order_number']} is on its way"
        message = "Your order has been handed to the Trimzee delivery team."
    else:
        subject = f"Trimzee order {order['order_number']} was cancelled"
        message = "Your order was cancelled. If you already paid, please contact Trimzee support."
    body = (f"Hi {order['customer_name']},\n\n{message}\n"
            f"Order number: {order['order_number']}\n"
            f"Order total: {_rupees(order['total_paise'])}\n")
    try:
        mailer.send_email(order["email"], subject, body)
    except Exception:
        logger.exception("Could not send a shop status email notification")
