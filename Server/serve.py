"""Production entry point: the app under waitress instead of Flask's
development server.

    python serve.py

Reads the same settings as ``python app.py`` (``.env`` / ``LR_*``). On a
VPS, put Caddy in front for HTTPS, bind to localhost, and set:

    LR_HOST=127.0.0.1
    LR_TRUSTED_PROXY=127.0.0.1     # believe Caddy's X-Forwarded-* headers
    LR_COOKIE_SECURE=1
    LR_WEB_PASSWORD=...            # login for the web UI
    LR_DEVICE_TOKEN=...            # same value as DEVICE_TOKEN in secrets.h

Run one process: the AI pipeline worker lives inside it.
"""
from __future__ import annotations

import os

from waitress import serve

import config as config_mod
from app import create_app


def main() -> None:
    cfg = config_mod.load()
    app = create_app(cfg)
    if not cfg.web_password:
        print("[LR ] WARNING: LR_WEB_PASSWORD is not set; the web UI is open "
              "to anyone who can reach the server.", flush=True)
    proxy = os.environ.get("LR_TRUSTED_PROXY", "").strip()
    extra = {}
    if proxy:
        extra = {
            "trusted_proxy": proxy,
            "trusted_proxy_headers": "x-forwarded-for x-forwarded-proto "
                                     "x-forwarded-host",
            "clear_untrusted_proxy_headers": True,
        }
    print(f"[LR ] waitress on http://{cfg.host}:{cfg.port}"
          + (f" (behind proxy {proxy})" if proxy else ""), flush=True)
    serve(
        app, host=cfg.host, port=cfg.port, threads=8,
        # Body limit matches Flask's MAX_CONTENT_LENGTH (create_app).
        max_request_body_size=app.config["MAX_CONTENT_LENGTH"],
        **extra,
    )


if __name__ == "__main__":
    main()
