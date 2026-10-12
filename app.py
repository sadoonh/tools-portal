"""Disposable, single-worker Cognito pilot. No tokens persist beyond a request."""
import math
import os
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode

from authlib.integrations.flask_client import OAuth
from flask import Flask, g, jsonify, redirect, render_template, request, session


class LoginCapacityError(Exception):
    pass


class LoginCache:
    """Authlib cache contract; bounded pending logins, NOT application sessions.

    Access is serialized by the sole synchronous Gunicorn worker. Authlib stores
    only a state/expiry marker in the cookie when this cache is configured.
    """

    def __init__(self, max_entries=100, ttl=300, clock=time.monotonic):
        self.entries = {}
        self.max_entries = max_entries
        self.ttl = ttl
        self.clock = clock

    def _cleanup(self):
        now = self.clock()
        for key, (_, expiry) in list(self.entries.items()):
            if expiry <= now:
                del self.entries[key]

    def get(self, key):
        self._cleanup()
        value = self.entries.get(key)
        return value[0] if value else None

    def set(self, key, value, timeout):
        self._cleanup()
        if key not in self.entries and len(self.entries) >= self.max_entries:
            raise LoginCapacityError()
        self.entries[key] = (value, self.clock() + min(timeout, self.ttl))

    def delete(self, key):
        self.entries.pop(key, None)


def create_app(test_config=None):
    app = Flask(__name__, template_folder="portal/templates")
    app.config.from_mapping(
        APP_MODE=os.environ.get("APP_MODE", "cognito"),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", ""),
        COGNITO_CLIENT_ID=os.environ.get("COGNITO_CLIENT_ID", ""),
        COGNITO_ISSUER=os.environ.get("COGNITO_ISSUER", ""),
        COGNITO_DOMAIN=os.environ.get("COGNITO_DOMAIN", ""),
        MARIMOHUB_URL=os.environ.get("MARIMOHUB_URL", ""),
        PORTAL_TITLE=os.environ.get("PORTAL_TITLE", "Tools Portal"),
        SECRET_KEY=secrets.token_bytes(32),
        SESSION_COOKIE_NAME="__Host-mvp_session",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH="/",
        SESSION_COOKIE_DOMAIN=None,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=1),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=8192,
        DEBUG=False,
    )
    if test_config:
        if not test_config.get("TESTING"):
            raise ValueError("Overrides are only permitted in Flask test configuration")
        app.config.update(test_config)
    title = app.config["PORTAL_TITLE"]
    if not isinstance(title, str) or not title.strip() or len(title) > 80 or "\n" in title or "\r" in title:
        raise ValueError("PORTAL_TITLE must be nonempty plain text, at most 80 characters without line breaks")
    hub_url = app.config["MARIMOHUB_URL"]
    if hub_url and not re.fullmatch(r"https://[a-z0-9]+\.cloudfront\.net", hub_url):
        raise ValueError("MARIMOHUB_URL must be a fixed HTTPS CloudFront origin")
    # Navigation only: no hub API, shared credentials or notebook orchestration.
    tools = [{
        "id": "marimohub", "name": "Marimohub",
        "description": "Create, edit and run Python notebooks", "url": hub_url,
    }] if hub_url else []
    mode = app.config["APP_MODE"]
    if mode not in {"placeholder", "cognito"}:
        raise ValueError("APP_MODE must be placeholder or cognito")

    oauth_client = None
    if mode == "cognito":
        base = app.config["PUBLIC_BASE_URL"]
        issuer = app.config["COGNITO_ISSUER"]
        domain = app.config["COGNITO_DOMAIN"]
        # This MVP deliberately has no custom domains or alternate redirects.
        if not re.fullmatch(r"https://[a-z0-9]+\.cloudfront\.net", base):
            raise ValueError("PUBLIC_BASE_URL must be the exact HTTPS CloudFront origin")
        match = re.fullmatch(
            r"https://cognito-idp\.([a-z]{2}-[a-z]+-\d)\.amazonaws\.com/\1_[A-Za-z0-9]+",
            issuer,
        )
        if not match or not re.fullmatch(
            rf"https://[a-z0-9-]+\.auth\.{re.escape(match[1])}\.amazoncognito\.com",
            domain,
        ):
            raise ValueError("Missing or invalid fixed Cognito issuer/domain")
        if not re.fullmatch(r"[a-z0-9]+", app.config["COGNITO_CLIENT_ID"]):
            raise ValueError("Missing or invalid public Cognito client ID")
        cache = LoginCache()
        oauth = OAuth(app, cache=cache)
        oauth_client = oauth.register(
            "cognito",
            client_id=app.config["COGNITO_CLIENT_ID"],
            server_metadata_url=issuer + "/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid",
                "code_challenge_method": "S256",
                "token_endpoint_auth_method": "none",
                "default_timeout": (3, 5),
                "verify": True,
            },
        )
        oauth_client.framework.expires_in = 300
        app.extensions["login_cache"] = cache
        app.extensions["cognito_client"] = oauth_client

    def checked_metadata():
        metadata = oauth_client.load_server_metadata()
        expected = {
            "issuer": app.config["COGNITO_ISSUER"],
            "jwks_uri": app.config["COGNITO_ISSUER"] + "/.well-known/jwks.json",
            "authorization_endpoint": app.config["COGNITO_DOMAIN"] + "/oauth2/authorize",
            "token_endpoint": app.config["COGNITO_DOMAIN"] + "/oauth2/token",
        }
        if any(metadata.get(k) != v for k, v in expected.items()):
            raise ValueError("Unexpected OIDC metadata")
        if "RS256" not in metadata.get("id_token_signing_alg_values_supported", []):
            raise ValueError("Missing Cognito signing algorithm")
        # Restrict Authlib's JWT decoder, even if discovery advertises others.
        metadata["id_token_signing_alg_values_supported"] = ["RS256"]

    def authenticated():
        now = time.time()
        expiry = session.get("session_expires_at")
        started = session.get("authenticated_at")
        valid_times = all(
            isinstance(v, (int, float)) and math.isfinite(v)
            for v in (expiry, started)
        )
        if (not session.get("sub") or not valid_times
                or not started <= now < expiry <= started + 3600):
            session.clear()
            return False
        return True

    def clear_pending():
        if oauth_client:
            for key in list(session):
                if key.startswith("_state_cognito_"):
                    oauth_client.framework.clear_state_data(
                        session, key.removeprefix("_state_cognito_")
                    )

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/")
    def index():
        if mode == "placeholder":
            return "MVP connectivity check; authentication not configured."
        if not authenticated():
            return redirect("/login")
        return render_template(
            "dashboard.html", title=title, csrf=session["logout_csrf"], tools=tools,
        )

    @app.get("/login")
    def login():
        if not oauth_client:
            return "Authentication not configured.", 503
        clear_pending()  # At most one outstanding login per browser.
        try:
            checked_metadata()
            return oauth_client.authorize_redirect(
                app.config["PUBLIC_BASE_URL"] + "/callback", response_mode="query"
            )
        except LoginCapacityError:
            return "Login busy; please retry shortly.", 503
        except Exception:  # noqa: BLE001 -- OAuth boundary must not leak provider exceptions.
            # Never log exception messages: providers may include codes/tokens.
            app.logger.warning("Login could not be started")
            return "Login unavailable; please retry.", 503

    @app.get("/callback")
    def callback():
        if not oauth_client:
            return "Authentication not configured.", 503
        state = request.args.get("state", "")
        # Authlib 1.6.12 checks the browser's signed session marker with a cache.
        # Reject before authorize_access_token so a different browser cannot
        # consume/delete another browser's transaction from the global cache.
        marker = session.get("_state_cognito_" + state)
        if (not state or len(state) > 256 or not marker
                or marker.get("exp", 0) <= time.time()
                or len(request.args.getlist("state")) != 1):
            return "Login invalid or expired; start login again.", 400
        try:
            if request.args.get("error") or not request.args.get("code"):
                raise ValueError("Authorization denied or incomplete")
            if len(request.args.getlist("code")) != 1:
                raise ValueError("Ambiguous authorization code")
            data = oauth_client.framework.get_state_data(session, state)
            if not data or not data.get("nonce") or not data.get("code_verifier"):
                raise ValueError("Missing transaction")
            checked_metadata()
            token = oauth_client.authorize_access_token(
                claims_options={
                    "iss": {"essential": True, "value": app.config["COGNITO_ISSUER"]},
                    "aud": {"essential": True, "value": app.config["COGNITO_CLIENT_ID"]},
                    "nonce": {"essential": True, "value": data["nonce"]},
                    "token_use": {"essential": True, "value": "id"},
                },
                leeway=0,
            )
            claims = token.get("userinfo")  # Only Authlib-verified ID-token claims.
            if not claims or not isinstance(claims.get("sub"), str) or not claims["sub"]:
                raise ValueError("Missing validated identity")
            # Authlib does not validate values for non-registered token_use,
            # and nonce_supported=false can suppress its nonce comparison.
            # These are additional checks on already signature-verified claims.
            if claims.get("token_use") != "id" or claims.get("nonce") != data["nonce"]:
                raise ValueError("Invalid ID-token purpose or nonce")
            now = time.time()
            expiry = min(float(claims["exp"]), now + 3600)
            if not math.isfinite(expiry) or expiry <= now:
                raise ValueError("Expired token")
            clear_pending()
            session.clear()
            session.permanent = True
            session.update(
                sub=claims["sub"], authenticated_at=now, session_expires_at=expiry,
                logout_csrf=secrets.token_urlsafe(32),
            )
            return redirect("/")
        except Exception:  # noqa: BLE001 -- Malformed tokens fail closed without error dumps.
            app.logger.warning("Login callback rejected")
            return "Login invalid or expired; start login again.", 400
        finally:
            oauth_client.framework.clear_state_data(session, state)
            g.pop("_oauth_token_cognito", None)

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        if request.method == "GET":
            # Completion does not mutate a valid session, even when visited directly.
            return '<!doctype html><title>MVP</title><p>Signed-out completion.</p><a href="/">Return</a>'
        if not oauth_client:
            return "Authentication not configured.", 503
        if not authenticated():
            return "Not signed in.", 403
        supplied = request.form.get("csrf", "")
        expected = session.get("logout_csrf", "")
        if not supplied or not expected or not secrets.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        ):
            return "Invalid logout request.", 403
        clear_pending()
        session.clear()
        return redirect(app.config["COGNITO_DOMAIN"] + "/logout?" + urlencode({
            "client_id": app.config["COGNITO_CLIENT_ID"],
            "logout_uri": app.config["PUBLIC_BASE_URL"] + "/logout",
        }))

    return app


# Fail closed by default. Local/container connectivity tests explicitly opt in.
app = create_app()
