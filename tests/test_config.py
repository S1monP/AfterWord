"""Tests for config._load_dotenv (the stdlib .env loader).

.env files are hand-edited in whatever the local editor defaults
to, so the loader must tolerate encodings (UTF-8, UTF-8 BOM,
UTF-16/Notepad "Unicode"), quotes, spacing around ``=``, comments,
and must never override real environment variables.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import config as config_mod

# Env keys used only by these tests; cleaned up in tearDown.
_TEST_KEYS = ("LR_TEST_A", "LR_TEST_B")


class LoadDotenvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in _TEST_KEYS}
        for k in _TEST_KEYS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write(self, data: bytes) -> Path:
        p = self._dir / ".env"
        p.write_bytes(data)
        return p

    def test_parses_simple_pairs(self) -> None:
        p = self._write(b"LR_TEST_A=hello\nLR_TEST_B=world\n")
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["LR_TEST_A"], "hello")
        self.assertEqual(os.environ["LR_TEST_B"], "world")

    def test_comments_blank_lines_and_spacing(self) -> None:
        p = self._write(
            b"# a comment\n\n  LR_TEST_A = spaced value  \nLR_TEST_B=2\n",
        )
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["LR_TEST_A"], "spaced value")
        self.assertEqual(os.environ["LR_TEST_B"], "2")

    def test_quotes_are_stripped(self) -> None:
        p = self._write(b"LR_TEST_A='single'\nLR_TEST_B=\"double\"\n")
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["LR_TEST_A"], "single")
        self.assertEqual(os.environ["LR_TEST_B"], "double")

    def test_does_not_override_existing_env(self) -> None:
        os.environ["LR_TEST_A"] = "from-real-env"
        p = self._write(b"LR_TEST_A=from-file\n")
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["LR_TEST_A"], "from-real-env")

    def test_utf8_bom_is_stripped(self) -> None:
        # Notepad / PowerShell redirection often writes a BOM; it
        # must not corrupt the first key.
        p = self._write(b"\xef\xbb\xbfLR_TEST_A=hello\n")
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["LR_TEST_A"], "hello")

    def test_utf16_file_is_decoded(self) -> None:
        # Notepad "Unicode" save: UTF-16-LE with BOM. Read as UTF-8
        # this yields NUL-riddled text and silently matches nothing.
        p = self._write("LR_TEST_A=hello\n".encode("utf-16"))
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["LR_TEST_A"], "hello")

    def test_missing_file_is_ignored(self) -> None:
        config_mod._load_dotenv(self._dir / "does-not-exist.env")

    def test_undecodable_bytes_do_not_crash(self) -> None:
        # e.g. a Latin-1 é in a comment; loader replaces and continues.
        p = self._write(b"# caf\xe9\nLR_TEST_A=ok\n")
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["LR_TEST_A"], "ok")


if __name__ == "__main__":
    unittest.main()