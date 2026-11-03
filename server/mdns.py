"""A name on the local network: ``http://afterword.local``.

Announces ``<name>.local`` (and an ``_http._tcp`` service, so it shows
up in Bonjour browsers) with multicast DNS, the way printers and Macs
are found. iPhones, Macs and Windows PCs resolve it with no setup;
Android's Chrome does too. The machine's own Windows resolver doesn't
ask other responders on the same machine, so on the server itself use
http://localhost.

Used by serve.py when ``AFTERWORD_MDNS_NAME`` is set. The LAN address is
checked every few minutes, so the name follows a new address from the
router.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

log = logging.getLogger(__name__)

RECHECK_S = 300


def lan_ip() -> str | None:
    """The address this machine uses on the local network (no packet
    is sent: connecting a UDP socket only picks the route)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))      # TEST-NET; never reached
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def announce(name: str, port: int) -> threading.Thread | None:
    """Announce ``name.local`` for this machine on ``port`` until the
    process ends. Returns None (and says why) when it can't."""
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
    except ImportError:
        log.warning("AFTERWORD_MDNS_NAME is set but zeroconf isn't installed "
                    "(pip install -r requirements.txt)")
        return None
    name = name.strip().lower().removesuffix(".local")

    def info_for(ip: str) -> ServiceInfo:
        return ServiceInfo(
            "_http._tcp.local.", f"AfterWord ({name})._http._tcp.local.",
            addresses=[socket.inet_aton(ip)], port=port,
            server=f"{name}.local.", properties={"path": "/"},
        )

    def run() -> None:
        ip, zc, info = None, None, None
        while True:
            now_ip = lan_ip()
            if now_ip != ip:
                try:
                    if zc is not None:
                        zc.unregister_service(info)
                        zc.close()
                    zc, info, ip = None, None, now_ip
                    if ip:
                        zc = Zeroconf(interfaces=[ip], ip_version=IPVersion.V4Only)
                        info = info_for(ip)
                        zc.register_service(info, allow_name_change=True)
                        log.info("on the network as http://%s.local%s (%s)", name,
                                 "" if port == 80 else f":{port}", ip)
                except Exception as exc:  # pragma: no cover  (try again later)
                    log.warning("network name: %r", exc)
                    zc, info, ip = None, None, None
            time.sleep(RECHECK_S)

    t = threading.Thread(target=run, name="afterword-mdns", daemon=True)
    t.start()
    return t
