# Trimzee

Trimzee is a Flask web application for finding salons, viewing their services and
availability, and requesting appointments. Customers can browse and book without
a customer subscription. Salon eligibility is still controlled by the salon
owner's plan when billing enforcement is enabled. Salon owners manage their salon
and bookings; administrators manage accounts, owner subscriptions, and payments.

This guide is written for someone new to the project. It explains how to start
the app and where to look when changing a feature.

## Run the app locally

You need Python 3.10 or newer. From the project folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
flask --app app run --debug
```

Open <http://127.0.0.1:5000>. The root-level `app.py` is the development entry
point: it loads the Flask app from `src/app.py` and points static files at
`src/public/`.

To stop the development server, press `Ctrl+C` in the terminal. The SQLite
database is stored at `src/database/salongo.db` by default. It is initialized
and upgraded automatically when the app starts.

## Project map

| Path | What it contains |
| --- | --- |
| `app.py` | Local Flask entry point. |
| `src/app.py` | Flask setup, request helpers, and the routes used by browser pages and APIs. |
| `src/` Python modules | Feature logic, separated by responsibility; see the module guide below. |
| `src/templates/` | Jinja templates: HTML pages with placeholders filled by Flask. |
| `src/public/js/` | Browser JavaScript for page interactions and API requests. |
| `src/public/css/` | Stylesheets for layout, colors, and individual pages. |
| `src/db.py` | SQLite schema, database connections, and the local database setup. |
| `src/pgdb.py` | Optional PostgreSQL connection adapter. |
| `requirements.txt` | Python packages needed to run the application. |
| `public/` | Static CSS and JavaScript assets for deployment setups that publish root-level assets. Some files also exist in `src/public/`; check both locations when changing an asset and keep them in sync when both should behave the same. |

### Python module guide

- `src/app.py` connects incoming URLs to feature code and templates.
- `src/db.py` defines the SQLite tables and shared database helpers.
- `src/pgdb.py` uses PostgreSQL instead of SQLite when a database URL is set.
- `src/boot.py` runs database setup and upgrades when the stored schema version changes.
- `src/extras.py` contains salon administration, reviews, and service management.
- `src/slots.py` calculates live availability and creates or updates bookings.
- `src/billing.py` manages salon-owner plans, coupons, subscriptions, and barbers.
- `src/payments.py` creates and verifies Razorpay payments for salon-owner plans.
- `src/accounts.py` loads accounts and handles profile and referral features.
- `src/signup.py` validates registration and verifies email codes.
- `src/googleauth.py` handles Google sign-in.
- `src/growth.py` contains booking notifications, contact details, and growth features.
- `src/mailer.py` sends email and WhatsApp messages when those services are configured.
- `src/safety.py` limits sensitive actions such as repeated email requests.
- `src/meta.py` provides shared salon status, booking, and notification helpers.
- `src/chatbot.py` provides the salon-question assistant.
- `src/legal.py` stores business details used on legal and contact pages.
- `src/minipdf.py` and `src/qr.py` generate PDF and QR-code output without extra packages.

### Browser and page files

- `src/public/js/app.js` powers salon search, filtering, and location-aware results on the homepage.
- `src/public/js/salon.js` loads salon availability and submits appointment requests.
- `src/public/js/notify.js` displays booking and appointment notifications.
- `src/public/js/chat.js` controls the assistant widget.
- `src/public/js/shop.js` controls the shop waiting-list interaction.
- The corresponding files under root `public/` are static-asset copies used by deployment setups.
- `src/templates/` contains pages for account access, salons, bookings, owner dashboards, admin tools, plans, checkout, and legal information. Each template begins with a short comment describing its purpose.

## How appointment booking works

1. The salon page asks `/api/salons/<salon_id>/slots` for current availability.
2. The customer chooses a service and an available time. Choosing a time only selects it in the browser; it does not reserve the time.
3. When the customer confirms, the page sends the request to `/api/book`.
4. The server checks the salon, service, time, customer, and booking rules, then saves a pending booking.
5. The database's unique active-booking index is the final protection against two requests taking the same barber and time. If another request wins first, the customer is asked to choose a different time. Customers can browse every available time without buying a plan; only salon owners have subscriptions.
6. The salon owner can accept or reject the pending request from the dashboard.

The main browser code is in `src/public/js/salon.js`; the route is in
`src/app.py`; booking and availability logic is in `src/slots.py`; barber
capacity and the uniqueness rule are in `src/billing.py`.

## Optional services and environment variables

The app can run locally without configuring third-party services. Set these
environment variables in your shell or hosting provider when you enable them:

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Signs browser sessions and verification codes. Set a long, private value outside local development. |
| `DATABASE_URL` or `POSTGRES_URL` | Connects to PostgreSQL. If neither is set, the app uses SQLite. |
| `PUBLIC_URL` | Public site URL used when generating shareable links. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Create or configure the administrator account during database setup. |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Enable Razorpay checkout and payment verification. |
| `RAZORPAY_WEBHOOK_SECRET` | Verify incoming Razorpay webhook requests. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Configure email delivery for verification and account messages. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` | Configure WhatsApp notifications through Twilio. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Enable Google sign-in. |
| `ANTHROPIC_API_KEY`, `CHAT_MODEL` | Enable the external assistant model. |
| `BILLING_ENFORCE` | Set to `1` to enforce the subscription checks used by billing features. |

Never put real credentials in source files or commit them to Git.

## Making a change

1. Find the page in `src/templates/` and its browser behavior in `src/public/js/`.
2. Find the matching route in `src/app.py`.
3. Update the feature module that owns the business logic rather than putting database logic in a template.
4. If changing a browser asset, keep its root `public/` copy in sync when that file exists in both places.
5. Run the app and verify the affected page. For a Python syntax check, run `python -m compileall -q src`.

HTML comments identify each page, Python module docstrings explain module
responsibilities, and JavaScript and CSS files have purpose comments at the
top. Add further comments for decisions or workflows that would otherwise be
hard for a new contributor to follow; avoid comments that merely repeat the
code.
