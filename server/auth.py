"""Access control: a device token for uploads, a password for the web UI.

Both are off unless configured, so local development needs no setup.

* ``AFTERWORD_DEVICE_TOKEN``: the device upload endpoints (announce, chunk PUT,
  finish) require ``Authorization: Bearer <token>``. The firmware sends
  it when ``DEVICE_TOKEN`` is set in secrets.h.
* ``AFTERWORD_WEB_PASSWORD``: every other page and API needs a login (one
  user). Device endpoints never need the login; they rely on the token.
* ``AFTERWORD_SECRET``: key for the login cookie. Without it a random key is
  generated once and kept in ``<data>/db/secret_key``, so logins
  survive restarts.
* ``AFTERWORD_COOKIE_SECURE=1``: send the cookie over HTTPS only (behind Caddy).

The cookie is SameSite=Lax, so other sites cannot post forms with it.
"""
from __future__ import annotations

import datetime as _dt
import hmac
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    Flask,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

log = logging.getLogger(__name__)

#: Endpoints the recorder calls. Token-checked; never behind the login.
DEVICE_ENDPOINTS = frozenset({
    "sessions_api.api_create_session", "sessions_api.api_put_chunk", "sessions_api.api_finish_session",
})
#: Reachable without logging in.
OPEN_ENDPOINTS = frozenset({"auth.login", "static"})

#: Pause after a wrong password, to slow guessing.
LOGIN_FAIL_DELAY_S = 1.0

bp = Blueprint("auth", __name__)


def init_app(app: Flask) -> None:
    cfg = app.config["AFTERWORD_CONFIG"]
    app.secret_key = cfg.secret_key or _persistent_secret(cfg.data_dir)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.cookie_secure,
        PERMANENT_SESSION_LIFETIME=_dt.timedelta(days=30),
    )
    app.register_blueprint(bp)
    app.before_request(_guard)
    app.context_processor(lambda: {"auth_enabled": bool(cfg.web_password)})
    if cfg.web_password and not cfg.device_token:
        log.warning("AFTERWORD_WEB_PASSWORD is set but AFTERWORD_DEVICE_TOKEN "
                    "is not; device uploads are open to anyone who can reach "
                    "the server.")


def _persistent_secret(data_dir: Path) -> str:
    path = Path(data_dir) / "db" / "secret_key"
    try:
        key = path.read_text(encoding="ascii").strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="ascii")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _device_token_ok(expected: str) -> bool:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return scheme.lower() == "bearer" and _same(token.strip(), expected)


def _guard() -> Any | None:
    cfg = current_app.config["AFTERWORD_CONFIG"]
    endpoint = request.endpoint
    if endpoint in DEVICE_ENDPOINTS:
        if cfg.device_token and not _device_token_ok(cfg.device_token):
            return jsonify({"error": "unauthorized",
                            "detail": "device token required"}), 401
        return None
    if not cfg.web_password or endpoint in OPEN_ENDPOINTS:
        return None
    if session.get("auth"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized", "detail": "log in first"}), 401
    target = request.full_path if request.query_string else request.path
    return redirect(url_for("auth.login", next=target))


def _safe_next(target: str | None) -> str:
    """Only same-site paths, so ?next= cannot send the user elsewhere."""
    if target and target.startswith("/") and not target.startswith("//") \
            and "\\" not in target:
        return target
    return "/"


@bp.route("/login", methods=["GET", "POST"])
def login() -> Any:
    cfg = current_app.config["AFTERWORD_CONFIG"]
    next_url = _safe_next(request.values.get("next"))
    if not cfg.web_password:
        return redirect(next_url)
    if request.method == "POST":
        if _same(request.form.get("password", ""), cfg.web_password):
            session.clear()
            session["auth"] = True
            session.permanent = True
            return redirect(next_url)
        time.sleep(LOGIN_FAIL_DELAY_S)
        return render_template("login.html", error="Wrong password.",
                               next_url=next_url), 401
    return render_template("login.html", error=None, next_url=next_url)


@bp.post("/logout")
def logout() -> Any:
    session.clear()
    return redirect(url_for("auth.login"))
