"""Trimzee assistant. Answers from live salon data with built-in rules.
If an ANTHROPIC_API_KEY setting exists it asks Claude instead and falls back to the rules on any problem."""
import json
import os
import re
import time
import urllib.request

SERVICE_WORDS = {"haircut": ["haircut", "hair cut", "cut"], "beard": ["beard", "shave", "trim"],
                 "hair styling": ["styling", "style"], "facial": ["facial", "face"],
                 "massage": ["massage", "spa"], "coloring": ["colour", "color", "dye"]}
SYSTEM = ("You are Trimzee's friendly salon-booking assistant for India. Answer in 1-4 short sentences, using ONLY the "
          "salon data below for names, prices, distances and availability. Booking rules: customers book salons within "
          "5 km of their live location; pay at the salon; cancel from My bookings before the slot starts; reviews are "
          "possible after the salon marks a booking completed. If you don't know, say so politely.\nSalons:\n")
_hits = {}


def allowed(ip, limit=30, window=3600):
    now = time.time()
    recent = [t for t in _hits.get(ip, []) if now - t < window]
    _hits[ip] = recent + [now]
    return len(recent) < limit


def _line(s):
    return f"{s['name']} · from ₹{s['price']} · {s['status'].replace('full', 'fully booked')} · {s['distance']:.1f} km"


def _pick(salons, limit=3):
    return [{"id": s["id"], "name": s["name"], "line": _line(s)} for s in salons[:limit]]


def rule_reply(message, salons):
    m = re.sub(r"[^a-z0-9₹ ]", " ", message.lower())
    service = next((k for k, v in SERVICE_WORDS.items() if any(w in m for w in v)), None)
    pool = [s for s in salons if not service or any(service.split()[0] in x.lower() for x in s["services"])]
    live = [s for s in pool if s["status"] in ("available", "busy")]
    has = lambda *keys: any(k in m for k in keys)

    if has("hello", "hi ", "hey", "namaste") or m.strip() in ("hi", "hey", "hello"):
        return "Hi! 👋 I'm the Trimzee assistant. Ask me for the cheapest haircut, salons open now, or how booking works.", []
    if has("cancel"):
        return "Open the menu → Booking, then tap Cancel on a booking that hasn't started yet. The slot is freed straight away.", []
    if has("review", "rating", "rate"):
        return "After the salon marks your visit as completed, a review box appears under that booking in My bookings.", []
    if has("pay", "payment", "upi", "cash"):
        return "Booking is free. You pay the salon directly at your visit. Online payments are coming later.", []
    if has("km", "range", "location", "gps", "far"):
        return "You can book salons within 5 km of your live location. Tap the location button and allow location access.", []
    if has("owner", "my salon", "register salon", "list my", "join as"):
        return "Salon owners: create an account as \"I own a salon\". After admin approval your salon goes live and you manage bookings, services and revenue from your dashboard.", []
    if has("how", "book") and not has("cheap", "price", "near", "open"):
        return "1) Allow location  2) Open a salon  3) Pick a service and a free time  4) Add your phone number and confirm. You'll get a reminder 5 minutes before your slot.", []
    if has("cheap", "lowest", "budget", "price", "cost", "rate", "₹", "rs"):
        best = sorted(live or pool, key=lambda s: s["price"])
        if best:
            label = f" for {service}" if service else ""
            return f"Cheapest options{label} right now:", _pick(best)
    if has("open", "available", "now", "free", "slot", "next", "busy"):
        if live:
            return "Salons you can book right now:", _pick(sorted(live, key=lambda s: s["distance"]))
        return "No salon has a free slot at the moment. Please check again soon.", []
    if has("near", "close", "nearby", "around", "distance") or service or has("salon", "parlour", "parlor"):
        if pool:
            label = f" for {service}" if service else ""
            return f"Closest salons{label}:", _pick(sorted(pool, key=lambda s: s["distance"]))
        return "I couldn't find a matching salon near you yet.", []
    return "I can help with finding salons, prices, free slots and how booking works. Try \"cheapest haircut\" or \"open now\".", []


def ask_claude(message, salons):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    context = "\n".join(f"- {_line(s)}; services: {', '.join(s['services'])}; next free: {s['next']}" for s in salons[:20])
    body = json.dumps({"model": os.environ.get("CHAT_MODEL", "claude-sonnet-5-5"), "max_tokens": 300,
                       "system": SYSTEM + context, "messages": [{"role": "user", "content": message[:500]}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.load(resp)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        return text or None
    except Exception:
        return None


def reply(message, salons):
    """Returns (text, salon_cards)."""
    message = (message or "").strip()[:500]
    if not message:
        return "Ask me anything about salons, prices or booking.", []
    ai = ask_claude(message, salons)
    if ai:
        return ai, []
    return rule_reply(message, salons)
