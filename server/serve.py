"""Production entry point: the app under waitress instead of Flask's
development server.

    python serve.py

Reads the same settings as ``python app.py`` (``.env`` / ``AFTERWORD_*``). On a
VPS, put Caddy in front for HTTPS, bind to localhost, and set:

    AFTERWORD_HOST=127.0.0.1
    AFTERWORD_TRUSTED_PROXY=127.0.0.1     # believe Caddy's X-Forwarded-* headers
    AFTERWORD_COOKIE_SECURE=1
    AFTERWORD_WEB_PASSWORD=...            # login for the web UI
    AFTERWORD_DEVICE_TOKEN=...            # same value as DEVICE_TOKEN in secrets.h

On the home network (a test drive before the VPS):

    AFTERWORD_MDNS_NAME=afterword         # http://afterword.local (mdns.py)
    AFTERWORD_EXTRA_PORTS=80              # also on port 80, so no ":8000" in
                                          # the address; the recorder keeps
                                          # AFTERWORD_PORT
    AFTERWORD_LOG_FILE=data/server.log    # when started in the background
                                          # (rotated at 5 MB, logsetup.py)

Run one process: the AI pipeline worker lives inside it.
"""

from __future__ import annotations

import logging
import os
import sys

from waitress import serve

import config as config_mod
import logsetup
from app import create_app

# Named, not __name__: ``python serve.py`` would log as "__main__".
log = logging.getLogger("serve")


def _ports(cfg) -> list[int]:
    ports = [cfg.port]
    for part in os.environ.get("AFTERWORD_EXTRA_PORTS", "").replace(",", " ").split():
        if part.isdigit() and int(part) not in ports:
            ports.append(int(part))
    return ports


def main() -> None:
    cfg = config_mod.load()
    logsetup.setup()
    problem = config_mod.exposure_problem(cfg)
    if problem:
        log.error("not starting: %s", problem)
        sys.exit(2)
    app = create_app(cfg)
    if not cfg.web_password and not config_mod.is_loopback(cfg.host):
        log.warning(
            "AFTERWORD_ALLOW_OPEN is set; the web UI is open to anyone who can reach the server."
        )
    proxy = os.environ.get("AFTERWORD_TRUSTED_PROXY", "").strip()
    extra = {}
    if proxy:
        extra = {
            "trusted_proxy": proxy,
            "trusted_proxy_headers": "x-forwarded-for x-forwarded-proto x-forwarded-host",
            "clear_untrusted_proxy_headers": True,
        }
    ports = _ports(cfg)
    listen = " ".join(f"{cfg.host}:{p}" for p in ports)
    log.info("waitress on %s%s", listen, f" (behind proxy {proxy})" if proxy else "")
    name = os.environ.get("AFTERWORD_MDNS_NAME", "").strip()
    if name:
        import mdns

        # The friendliest port: 80 when it's served (no ":8000" to type).
        mdns.announce(name, 80 if 80 in ports else cfg.port)
    serve(
        app,
        listen=listen,
        threads=8,
        # Body limit matches Flask's MAX_CONTENT_LENGTH (create_app).
        max_request_body_size=app.config["MAX_CONTENT_LENGTH"],
        **extra,
    )


if __name__ == "__main__":
    main()
