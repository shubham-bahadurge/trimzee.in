"""Business and contact details configuration for public and legal pages."""
import os

UPDATED = "19 October 2026"

BUSINESS = {
    "name": "Trimzee",
    "email": "trimzee.in01@gmail.com",
    "phone": "[Business phone]",
    "address": "[Business address]",
    "city": "[Business city]",
    "gst": "",
    "site": os.environ.get("SITE_URL", "").strip().rstrip("/") or "https://trimzee.vercel.app",
    "grievance_officer": "Grievance Officer",
}
