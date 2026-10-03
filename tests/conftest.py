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


class LRTestCase(unittest.TestCase):
    """Base test case: each test gets its own temp data dir + Flask app."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix="lr-test-"))
        self._cfg = config_mod.Config(
            data_dir=self._tmpdir,
            host="127.0.0.1",
            port=0,
            openrouter_api_key="",
            minimax_api_key="",
        )
        self.app = create_app(self._cfg)
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
