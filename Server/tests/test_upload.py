"""Tests for the web audio upload route.

Designed to run regardless of whether ffmpeg is installed on the host.
Tests that require ffmpeg are skipped if it isn't available.
"""
from __future__ import annotations

import io
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from tests.conftest import LRTestCase, skip_without_ffmpeg


def _make_wav_bytes(payload: bytes = b"\x00\x00\x00\x00" * 200,
                    sample_rate: int = 22050,
                    channels: int = 1,
                    bits_per_sample: int = 32) -> bytes:
    """Build a minimal PCM WAV in memory, matching the device format."""
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    data_size = len(payload)
    riff_size = 36 + data_size
    h = bytearray(44)
    h[0:4] = b"RIFF"
    struct.pack_into("<I", h, 4, riff_size)
    h[8:12] = b"WAVE"
    h[12:16] = b"fmt "
    struct.pack_into("<I", h, 16, 16)
    struct.pack_into("<H", h, 20, 1)
    struct.pack_into("<H", h, 22, channels)
    struct.pack_into("<I", h, 24, sample_rate)
    struct.pack_into("<I", h, 28, byte_rate)
    struct.pack_into("<H", h, 32, block_align)
    struct.pack_into("<H", h, 34, bits_per_sample)
    h[36:40] = b"data"
    struct.pack_into("<I", h, 40, data_size)
    return bytes(h) + payload


def _io_bytes(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)


class UploadPageTests(LRTestCase):

    def test_upload_page_renders(self) -> None:
        r = self.client.get("/upload")
        self.assertEqual(r.status_code, 200)
        body = r.get_data()
        self.assertIn(b"Drop an audio file here", body)
        self.assertIn(b"data-drop", body)
        self.assertIn(b'action="/api/v1/uploads/audio"', body)

    def test_upload_page_mentions_ffmpeg_status(self) -> None:
        # Check against the ffmpeg binary the app is actually
        # configured with (conftest points at a dummy path), not
        # the host PATH.
        from services import transcode
        cfg = self.app.config["LR_CONFIG"]
        r = self.client.get("/upload")
        body = r.get_data()
        if transcode.ffmpeg_available(cfg.ffmpeg_path):
            self.assertNotIn(b"need ffmpeg on the server", body)
        else:
            self.assertIn(b"need ffmpeg on the server", body)


class WavUploadTests(LRTestCase):
    """A WAV upload is transcoded to canonical AAC at ingest."""

    @skip_without_ffmpeg
    def test_wav_upload_transcoded_to_aac(self) -> None:
        wav = _make_wav_bytes()
        r = self.client.post(
            "/api/v1/uploads/audio",
            data={"file": (_io_bytes(wav), "lecture.wav")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body["created"])
        # The upload route now runs the AI pipeline; with no API
        # keys configured the no-op providers complete it, so the
        # session ends at 'done' (not 'ready').
        self.assertEqual(body["session"]["status"], "done")

    @skip_without_ffmpeg
    def test_upload_runs_pipeline_in_background_when_enabled(self):
        """Production mode: the upload returns immediately with the
        pre-pipeline status while the pipeline runs in a background
        thread, then the session reaches 'done'."""
        import time
        self.app.config["LR_PIPELINE_BACKGROUND"] = True
        wav = _make_wav_bytes()
        r = self.client.post(
            "/api/v1/uploads/audio",
            data={"file": (_io_bytes(wav), "lecture.wav")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        body = r.get_json()
        # The response is built before the background pipeline has
        # made any status transitions, so it's still 'ready'.
        self.assertEqual(body["session"]["status"], "ready")
        # Wait for the background pipeline to finish (no-op
        # providers complete quickly without API keys).
        store = self.app.config["LR_STORAGE"]
        sid = body["session"]["id"]
        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            status = store.get_session(sid).status
            if status in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(status, "done")
        self.assertEqual(body["redirect_url"], f"/other?session={sid}")
        self.assertEqual(self.client.get(body["redirect_url"]).status_code, 200)
        audio = self.client.get(body["session"]["full_audio_url"])
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(
            audio.mimetype, "audio/aac",
            audio.get_data(as_text=False)[:16],
        )
        audio.close()

    @skip_without_ffmpeg
    def test_canonical_wav_default_title(self) -> None:
        wav = _make_wav_bytes()
        r = self.client.post(
            "/api/v1/uploads/audio",
            data={"file": (_io_bytes(wav), "lecture.wav")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.get_json()["session"]["title"].startswith("rec_"))

    @skip_without_ffmpeg
    def test_canonical_wav_explicit_title(self) -> None:
        wav = _make_wav_bytes()
        r = self.client.post(
            "/api/v1/uploads/audio",
            data={"file": (_io_bytes(wav), "lecture.wav"),
                  "title": "CS101 Lec 3"},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["session"]["title"], "CS101 Lec 3")

    @skip_without_ffmpeg
    def test_empty_title_uses_default(self) -> None:
        wav = _make_wav_bytes()
        r = self.client.post(
            "/api/v1/uploads/audio",
            data={"file": (_io_bytes(wav), "x.wav"), "title": "  "},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.get_json()["session"]["title"].startswith("rec_"))

    def test_invalid_title_rejected(self) -> None:
        wav = _make_wav_bytes()
        r = self.client.post(
            "/api/v1/uploads/audio",
            data={"file": (_io_bytes(wav), "x.wav"), "title": "bad\x00title"},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "invalid_title")

    def test_missing_file_rejected(self) -> None:
        r = self.client.post(
            "/api/v1/uploads/audio",
            data={},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "missing_file")

    def test_oversize_file_rejected(self) -> None:
        cfg = self.app.config["LR_CONFIG"]
        original = cfg.max_upload_bytes
        # Config is frozen; build a new dataclass with the smaller cap.
        from dataclasses import replace
        new_cfg = replace(cfg, max_upload_bytes=100)
        self.app.config["LR_CONFIG"] = new_cfg
        try:
            wav = _make_wav_bytes(b"X" * 1000)
            r = self.client.post(
                "/api/v1/uploads/audio",
                data={"file": (_io_bytes(wav), "big.wav")},
                content_type="multipart/form-data",
            )
            self.assertEqual(r.status_code, 413)
            self.assertEqual(r.get_json()["error"], "file_too_large")
        finally:
            self.app.config["LR_CONFIG"] = cfg
            _ = original


class NonCanonicalUploadTests(LRTestCase):
    """Non-AAC uploads need ffmpeg; the 503 path is tested by
    pointing the app at a nonexistent binary explicitly (conftest
    uses the real ffmpeg when one is installed)."""

    def test_upload_without_ffmpeg_returns_503(self) -> None:
        from dataclasses import replace
        cfg = self.app.config["LR_CONFIG"]
        self.app.config["LR_CONFIG"] = replace(
            cfg, ffmpeg_path="ffmpeg-nonexistent-test-binary",
        )
        try:
            fake_mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff" * 256
            r = self.client.post(
                "/api/v1/uploads/audio",
                data={"file": (_io_bytes(fake_mp3), "voice.m4a")},
                content_type="multipart/form-data",
            )
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.get_json()["error"], "ffmpeg_missing")
        finally:
            self.app.config["LR_CONFIG"] = cfg

    @skip_without_ffmpeg
    def test_non_aac_with_ffmpeg_succeeds(self) -> None:
        import subprocess
        from dataclasses import replace
        real_ffmpeg = shutil.which("ffmpeg")
        if not real_ffmpeg:
            self.skipTest("ffmpeg not installed on this host")
        # Point the app at the real ffmpeg binary for this test —
        # conftest deliberately configures a dummy path so the 503
        # flow can be tested without ffmpeg.
        cfg = self.app.config["LR_CONFIG"]
        self.app.config["LR_CONFIG"] = replace(cfg, ffmpeg_path=real_ffmpeg)
        try:
            src = self._tmpdir / "src.wav"
            src.write_bytes(_make_wav_bytes(b"\x00\x00\x00\x00" * 500))
            mp3 = self._tmpdir / "src.mp3"
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(src), "-c:a", "libmp3lame", "-b:a", "64k",
                 str(mp3)],
                check=True,
            )
            mp3_bytes = mp3.read_bytes()
            self.assertGreater(len(mp3_bytes), 0)

            r = self.client.post(
                "/api/v1/uploads/audio",
                data={"file": (_io_bytes(mp3_bytes), "lecture.mp3")},
                content_type="multipart/form-data",
            )
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
            # Pipeline ran (no-op without keys) → session is 'done'.
            self.assertEqual(r.get_json()["session"]["status"], "done")
        finally:
            self.app.config["LR_CONFIG"] = cfg


class TranscodeUnitTests(unittest.TestCase):
    """Unit tests for the transcode helpers and title formatters."""

    def test_sniff_wav(self) -> None:
        from services.transcode import sniff_format
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(_make_wav_bytes())
            path = Path(f.name)
        try:
            fmt = sniff_format(path)
            self.assertEqual(fmt.kind, "wav")
            self.assertEqual(fmt.sample_rate, 22050)
            self.assertEqual(fmt.channels, 1)
            self.assertEqual(fmt.bits_per_sample, 32)
            # A WAV is NOT the canonical format anymore.
            self.assertFalse(fmt.kind == "aac")
        finally:
            path.unlink()

    @skip_without_ffmpeg
    def test_sniff_aac(self) -> None:
        from services.transcode import sniff_format, is_canonical_aac
        wav = _make_wav_bytes()
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "sniff.wav"
            src.write_bytes(wav)
            dst = Path(d) / "sniff.aac"
            from services import transcode
            transcode.transcode_to_aac(
                src, dst, ffmpeg_path=shutil.which("ffmpeg") or "ffmpeg",
            )
            fmt = sniff_format(dst)
            self.assertEqual(fmt.kind, "aac")
            self.assertTrue(is_canonical_aac(fmt))
            # AAC must be detected before the generic MPEG fallback
            # (an ADTS header would otherwise sniff as MP3).
            self.assertNotEqual(fmt.kind, "mp3")

    def test_sniff_mp4(self) -> None:
        from services.transcode import sniff_format
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
            f.write(b"\x00\x00\x00\x20ftypM4A ")
            path = Path(f.name)
        try:
            self.assertEqual(sniff_format(path).kind, "mp4")
        finally:
            path.unlink()

    def test_sniff_unknown(self) -> None:
        from services.transcode import sniff_format
        with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
            f.write(b"some random bytes here 12345")
            path = Path(f.name)
        try:
            self.assertEqual(sniff_format(path).kind, "unknown")
        finally:
            path.unlink()

    def test_default_title_format(self) -> None:
        from app import _default_title_from_iso
        # Titles use local time like the device's folder names:
        # 14:30 UTC on 3 Oct is 15:30 BST; a naive time is already local.
        self.assertEqual(
            _default_title_from_iso("2026-10-03T14:30:00Z"),
            "rec_2026-10-03T1530",
        )
        self.assertEqual(
            _default_title_from_iso("2026-10-03T14:30:00"),
            "rec_2026-10-03T1430",
        )

    def test_title_normalisation(self) -> None:
        from app import _normalise_title
        self.assertEqual(_normalise_title("  CS101  "), "CS101")
        self.assertIsNone(_normalise_title(""))
        self.assertIsNone(_normalise_title("a" * 201))
        self.assertIsNone(_normalise_title("bad\x00title"))
        self.assertEqual(_normalise_title("OK title!"), "OK title!")


if __name__ == "__main__":
    unittest.main()
