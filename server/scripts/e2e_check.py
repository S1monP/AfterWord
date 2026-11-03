"""End-to-end smoke test.

Default: uses Flask's test client (no network roundtrip; instant).
``--live``: starts the dev server in a background thread, then runs
real HTTP requests against it.

Run: ``python scripts/e2e_check.py`` (add ``--live`` for real HTTP).
"""

from __future__ import annotations

import json
import shutil
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# The server modules (config, app, ...) live one folder up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FFMPEG = shutil.which("ffmpeg") or "ffmpeg-nonexistent-test-binary"


def make_wav(payload: bytes, sr: int = 22050, ch: int = 1, bps: int = 32) -> bytes:
    byte_rate = sr * ch * (bps // 8)
    block_align = ch * (bps // 8)
    data_size = len(payload)
    riff_size = 36 + data_size
    h = bytearray(44)
    h[0:4] = b"RIFF"
    struct.pack_into("<I", h, 4, riff_size)
    h[8:12] = b"WAVE"
    h[12:16] = b"fmt "
    struct.pack_into("<I", h, 16, 16)
    struct.pack_into("<H", h, 20, 1)
    struct.pack_into("<H", h, 22, ch)
    struct.pack_into("<I", h, 24, sr)
    struct.pack_into("<I", h, 28, byte_rate)
    struct.pack_into("<H", h, 32, block_align)
    struct.pack_into("<H", h, 34, bps)
    h[36:40] = b"data"
    struct.pack_into("<I", h, 40, data_size)
    return bytes(h) + payload


def make_aac(payload: bytes) -> bytes:
    """Canonical ADTS AAC chunk (as the device would upload)."""
    from services import transcode

    with tempfile.TemporaryDirectory(prefix="afterword-e2e-enc-") as d:
        src = Path(d) / "in.wav"
        src.write_bytes(make_wav(payload))
        dst = Path(d) / "out.aac"
        transcode.transcode_to_aac(src, dst, ffmpeg_path=FFMPEG)
        return dst.read_bytes()


def run_checks(req) -> int:
    # 1. Empty state (the recordings list; "/" is the dashboard)
    s, b, _ = req("GET", "/recordings")
    assert s == 200 and b"No recordings yet" in b, (s, b[:80])
    print("1. Empty state OK")

    # 2. Announce
    sid = "e2etest123456789"
    s, b, _ = req(
        "POST",
        "/api/v1/sessions",
        data=json.dumps(
            {
                "id": sid,
                "title": "E2E Test",
                "expected_count": 2,
                "sample_rate": 22050,
                "channels": 1,
                "bits_per_sample": 32,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert s == 201, (s, b)
    assert json.loads(b)["session"]["status"] == "receiving"
    print("2. Announce OK")

    # 3. Upload chunks
    aac1 = make_aac(b"A" * 200)
    aac2 = make_aac(b"B" * 200)
    s, b, _ = req(
        "PUT", f"/api/v1/sessions/{sid}/chunks/1", data=aac1, headers={"Content-Type": "audio/aac"}
    )
    assert s == 200, (s, b)
    s, b, _ = req(
        "PUT", f"/api/v1/sessions/{sid}/chunks/2", data=aac2, headers={"Content-Type": "audio/aac"}
    )
    assert s == 200, (s, b)
    print("3. Chunks OK")

    # 4. Re-PUT chunk 1 (idempotency)
    aac1b = make_aac(b"X" * 200)
    s, b, _ = req(
        "PUT", f"/api/v1/sessions/{sid}/chunks/1", data=aac1b, headers={"Content-Type": "audio/aac"}
    )
    assert s == 200
    print("4. Re-PUT OK")

    # 5. List
    s, b, _ = req("GET", "/api/v1/sessions")
    assert s == 200
    listed = json.loads(b)["sessions"]
    assert len(listed) == 1 and listed[0]["title"] == "E2E Test"
    print("5. List OK")

    # 6. Finish
    s, b, _ = req("POST", f"/api/v1/sessions/{sid}/finish")
    assert s == 200, (s, b)
    j = json.loads(b)
    assert j["ok"] and j["session"]["status"] == "ready"
    print("6. Finish OK")

    # 7. Audio
    s, b, hdr = req("GET", f"/api/v1/sessions/{sid}/audio")
    assert s == 200 and b[0] == 0xFF and (b[1] & 0xF6) == 0xF0
    assert hdr.get("Content-Type", "").startswith("audio/aac")
    assert len(b) == len(aac1b) + len(aac2) and b"X" in b and b"B" in b
    print(f"7. Audio OK, size={len(b)}")

    # 8. UI recordings list
    s, b, _ = req("GET", "/recordings")
    assert s == 200 and b"E2E Test" in b
    print("8. UI recordings list OK")

    # 9. UI session page
    s, b, _ = req("GET", f"/sessions/{sid}")
    assert s == 200 and b"<audio" in b
    print("9. UI session page OK")

    # 10. Missing chunks
    sid2 = "missingtest1234"
    req(
        "POST",
        "/api/v1/sessions",
        data=json.dumps({"id": sid2, "expected_count": 3}).encode(),
        headers={"Content-Type": "application/json"},
    )
    req(
        "PUT",
        f"/api/v1/sessions/{sid2}/chunks/1",
        data=make_aac(b"Y" * 100),
        headers={"Content-Type": "audio/aac"},
    )
    s, b, _ = req("POST", f"/api/v1/sessions/{sid2}/finish")
    assert s == 409 and json.loads(b)["error"] == "missing_chunks"
    print("10. Missing chunks 409 OK")

    # 11. Range request
    s, b, _ = req("GET", f"/api/v1/sessions/{sid}/audio", headers={"Range": "bytes=0-99"})
    assert s == 206 and len(b) == 100
    print("11. Range request 206 OK")

    print()
    print("=== ALL E2E CHECKS PASS ===")
    return 0


def main() -> int:
    import config as config_mod
    from app import create_app

    tmp = Path(tempfile.mkdtemp(prefix="afterword-e2e-"))
    cfg = config_mod.Config(
        data_dir=tmp,
        host="127.0.0.1",
        port=0,
        transcription_api_key="",
        text_api_key="",
        ffmpeg_path=FFMPEG,
        max_upload_bytes=10 * 1024 * 1024,
    )

    try:
        if "--live" in sys.argv:
            # Spawn the dev server in a background thread.
            from werkzeug.serving import make_server

            app = create_app(cfg)
            server = make_server("127.0.0.1", 8000, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            time.sleep(0.5)
            base = "http://127.0.0.1:8000"

            def req(method, path, data=None, headers=None):
                r = Request(base + path, method=method, data=data, headers=headers or {})
                try:
                    with urlopen(r) as resp:
                        return resp.status, resp.read(), dict(resp.headers)
                except HTTPError as e:
                    return e.code, e.read(), dict(e.headers)

            try:
                return run_checks(req)
            finally:
                server.shutdown()
                thread.join(timeout=2)
        else:
            # In-process test client.
            app = create_app(cfg)
            client = app.test_client()

            def req(method, path, data=None, headers=None):
                resp = client.open(path, method=method, data=data, headers=headers or {})
                return resp.status_code, resp.get_data(), dict(resp.headers)

            return run_checks(req)
    finally:
        try:
            app_storage = app.config["AFTERWORD_STORAGE"]
            try:
                app_storage._tls.conn.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
