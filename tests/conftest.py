"""Shared pytest fixtures.

These fixtures create a fresh data directory and a Flask app for each
test, so tests are isolated and can run in any order.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Make the project root importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as config_mod  # noqa: E402
from app import create_app  # noqa: E402
import storage  # noqa: E402

# Real ffmpeg when the host has one (uploads are transcoded to AAC at
# ingest now, so upload/stitch tests need it); a dummy path otherwise,
# with skipUnless(HAS_FFMPEG) guarding the tests that require it.
FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg-nonexistent-test-binary"
HAS_FFMPEG = shutil.which("ffmpeg") is not None


def skip_without_ffmpeg(test_item):
    """Skip decorator for tests that need a real ffmpeg binary."""
    return unittest.skipUnless(HAS_FFMPEG, "ffmpeg not available")(test_item)


class LRTestCase(unittest.TestCase):
    """Base test case: each test gets its own temp data dir + Flask app."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix="lr-test-"))
        self._cfg = config_mod.Config(
            data_dir=self._tmpdir,
            host="127.0.0.1",
            port=0,
            transcription_api_key="",
            text_api_key="",
            ffmpeg_path=FFMPEG_PATH,
            max_upload_bytes=10 * 1024 * 1024,
        )
        self.app = create_app(self._cfg)
        # Run upload pipelines synchronously in tests so assertions
        # on the post-upload status are deterministic. (Production
        # runs them in a background thread.)
        self.app.config["LR_PIPELINE_BACKGROUND"] = False
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        # Release any per-thread SQLite handles by dropping the app.
        try:
            self.app.config["LR_STORAGE"]._tls.conn.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        # Best-effort cleanup. On Windows, WAL files may still be locked
        # briefly; ignore failures.
        for attempt in range(3):
            try:
                shutil.rmtree(self._tmpdir)
                break
            except OSError:
                import time
                time.sleep(0.05)

    # --- helpers ---------------------------------------------------------

    def make_wav(self, payload: bytes = b"\x00\x00\x00\x00" * 64) -> bytes:
        return storage.make_wav_bytes(22050, 1, 32, payload)

    def make_aac_bytes(self, seconds: float = 1.0) -> bytes:
        """Real ADTS AAC produced from a tiny WAV via ffmpeg.

        Requires ffmpeg (skipWithoutFFmpeg the calling test).
        """
        from services import transcode as tx
        wav = storage.make_wav_bytes(
            22050, 1, 32,
            bytes(int(seconds * 22050 * 4)),
        )
        src = self._tmpdir / "gen.wav"
        src.write_bytes(wav)
        dst = self._tmpdir / "gen.aac"
        tx.transcode_to_aac(src, dst, ffmpeg_path=self._cfg.ffmpeg_path)
        data = dst.read_bytes()
        # Sanity: it must sniff as AAC.
        assert tx.sniff_format(dst).kind == "aac"
        return data
