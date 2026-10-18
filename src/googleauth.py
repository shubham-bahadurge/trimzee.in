"""Sign in with Google (OpenID Connect, authorization-code flow). Standard library only."""

import json
import os
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def _client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def _client_secret():
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def configured():
    return bool(_client_id() and _client_secret())


def login_url(redirect_uri, state):
    query = {"client_id": _client_id(), "redirect_uri": redirect_uri, "response_type": "code",
             "scope": "openid email profile", "state": state, "prompt": "select_account"}
    return AUTH_URL + "?" + urllib.parse.urlencode(query)


def _json(request):
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def profile_from_code(code, redirect_uri):
    """Trade the one-time code for the person's verified Google e-mail and name.
    Returns (profile, error_message); exactly one of them is None."""
    try:
        body = urllib.parse.urlencode({
            "code": code, "client_id": _client_id(), "client_secret": _client_secret(),
            "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode()
        token = _json(urllib.request.Request(
            TOKEN_URL, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"}))
        access = token.get("access_token")
        if not access:
            return None, "Google sign-in failed. Please try again."
        info = _json(urllib.request.Request(USERINFO_URL, headers={"Authorization": "Bearer " + access}))
    except Exception:
        return None, "Google sign-in failed. Please try again."
    email = (info.get("email") or "").strip().lower()
    if not email or info.get("email_verified") not in (True, "true"):
        return None, "Your Google email address is not verified."
    return {"email": email, "name": (info.get("name") or "").strip()}, None
