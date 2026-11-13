import os

# Business details shown on the Terms, Privacy, Refund and Contact pages. Edit this file to change them.

UPDATED = "19 October 2026"

BUSINESS = {'name': 'trimzee',
 'owner': 'shubham abhiman bahadurge',
 'email': 'trimzee.in01@gmail.com',
 'phone': '7499386605',
 'address': 'post rupur tq shirur dis beed',
 'city': 'beed , maharashtra',
 'gst': '',
 'site': os.environ.get("SITE_URL", "").strip().rstrip("/") or "https://trimzee.vercel.app"}
