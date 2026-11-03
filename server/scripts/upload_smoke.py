"""Live smoke test for the web audio upload path.

Generates a tiny audio file in memory (WAV, transcoded to canonical
AAC by the server), starts the dev server in a background thread,
POSTs it as multipart to /api/v1/uploads/audio, and verifies the
result. Requires ffmpeg on the host (uploads are transcoded to AAC).

Run: ``python scripts/upload_smoke.py``.
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


def make_wav(
    payload: bytes = b"\x00\x00\x00\x00" * 800, sr: int = 22050, ch: int = 1, bps: int = 32
) -> bytes:
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


def build_multipart(
    file_bytes: bytes, filename: str, title: str | None = None
) -> tuple[bytes, str]:
    """Return (body, content_type) for a multipart/form-data with one file."""
    boundary = "----afterword-smoke-boundary-7c4f8a2e"
    parts: list[bytes] = []
    if title is not None:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="title"\r\n'
            f"\r\n{title}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: audio/wav\r\n"
        f"\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    ctype = f"multipart/form-data; boundary={boundary}"
    return body, ctype


def main() -> int:
    import config as config_mod
    from app import create_app

    tmp = Path(tempfile.mkdtemp(prefix="afterword-upload-smoke-"))
    cfg = config_mod.Config(
        data_dir=tmp,
        host="127.0.0.1",
        port=0,
        transcription_api_key="",
        text_api_key="",
        ffmpeg_path=FFMPEG,
        max_upload_bytes=10 * 1024 * 1024,
    )

    app = create_app(cfg)
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 8001, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.5)
    base = "http://127.0.0.1:8001"

    def req(method, path, data=None, headers=None):
        r = Request(base + path, method=method, data=data, headers=headers or {})
        try:
            with urlopen(r) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    try:
        s, b, _ = req("GET", "/upload")
        assert s == 200 and b"Drop a file here" in b, (s, b[:80])
        print("1. Upload page OK")

        wav = make_wav()
        body, ctype = build_multipart(wav, "test.wav", "Smoke Title")
        s, b, _ = req("POST", "/api/v1/uploads/audio", data=body, headers={"Content-Type": ctype})
        assert s == 201, (s, b)
        j = json.loads(b)
        assert j["session"]["status"] == "ready"
        assert j["session"]["title"] == "Smoke Title"
        print(f"2. WAV upload (transcoded to AAC) OK, session={j['session']['id']}")

        s, b, hdr = req("GET", j["session"]["full_audio_url"])
        assert s == 200 and b[0] == 0xFF and (b[1] & 0xF6) == 0xF0
        assert hdr.get("Content-Type", "").startswith("audio/aac")
        # AAC is far smaller than the source WAV.
        assert 0 < len(b) < len(wav)
        print(f"3. Audio playable (AAC), size={len(b)}")

        body, ctype = build_multipart(wav, "default.wav", "")
        s, b, _ = req("POST", "/api/v1/uploads/audio", data=body, headers={"Content-Type": ctype})
        assert s == 201
        j = json.loads(b)
        assert j["session"]["title"].startswith("rec_"), j["session"]["title"]
        print(f"4. Default title OK: {j['session']['title']}")

        fake_m4a = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 256
        body, ctype = build_multipart(fake_m4a, "voice.m4a")
        s, b, _ = req("POST", "/api/v1/uploads/audio", data=body, headers={"Content-Type": ctype})
        assert s == 503, (s, b)
        assert json.loads(b)["error"] in (
            "ffmpeg_missing",
            "ffmpeg_failed",
        )
        print("5. Bogus media -> 503 OK")

        boundary = "----afterword-smoke-empty"
        empty = f"--{boundary}--\r\n".encode()
        s, b, _ = req(
            "POST",
            "/api/v1/uploads/audio",
            data=empty,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert s == 400 and json.loads(b)["error"] == "missing_file"
        print("6. Missing file -> 400 OK")

        s, b, _ = req("GET", "/api/v1/sessions")
        assert s == 200
        listed = json.loads(b)["sessions"]
        assert any(x["title"] == "Smoke Title" for x in listed)
        print("7. Session list OK")

        print()
        print("=== ALL UPLOAD SMOKE CHECKS PASS ===")
        return 0
    finally:
        server.shutdown()
        thread.join(timeout=2)
        try:
            app.config["AFTERWORD_STORAGE"]._tls.conn.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
