"""A name on the local network: ``http://afterword.local``.

Announces ``<name>.local`` (and an ``_http._tcp`` service, so it shows
up in Bonjour browsers) with multicast DNS, the way printers and Macs
are found. iPhones, Macs and Windows PCs resolve it with no setup;
Android's Chrome does too. The machine's own Windows resolver doesn't
ask other responders on the same machine, so on the server itself use
http://localhost.

Used by serve.py when ``LR_MDNS_NAME`` is set. The LAN address is
checked every few minutes, so the name follows a new address from the
router.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Optional

RECHECK_S = 300


def lan_ip() -> Optional[str]:
    """The address this machine uses on the local network (no packet
    is sent: connecting a UDP socket only picks the route)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))      # TEST-NET; never reached
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def announce(name: str, port: int) -> Optional[threading.Thread]:
    """Announce ``name.local`` for this machine on ``port`` until the
    process ends. Returns None (and says why) when it can't."""
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
    except ImportError:
        print("[LR ] LR_MDNS_NAME is set but zeroconf isn't installed "
              "(pip install -r requirements.txt)", flush=True)
        return None
    name = name.strip().lower().removesuffix(".local")

    def info_for(ip: str) -> "ServiceInfo":
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
                        print(f"[LR ] on the network as http://{name}.local"
                              + ("" if port == 80 else f":{port}") + f" ({ip})", flush=True)
                except Exception as exc:  # pragma: no cover  (try again later)
                    print(f"[LR ] network name: {exc!r}", flush=True)
                    zc, info, ip = None, None, None
            time.sleep(RECHECK_S)

    t = threading.Thread(target=run, name="lr-mdns", daemon=True)
    t.start()
    return t
