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
from unittest import mock

import config as config_mod

# Env keys used only by these tests; cleaned up in tearDown.
_TEST_KEYS = ("AFTERWORD_TEST_A", "AFTERWORD_TEST_B")


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
        p = self._write(b"AFTERWORD_TEST_A=hello\nAFTERWORD_TEST_B=world\n")
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["AFTERWORD_TEST_A"], "hello")
        self.assertEqual(os.environ["AFTERWORD_TEST_B"], "world")

    def test_comments_blank_lines_and_spacing(self) -> None:
        p = self._write(
            b"# a comment\n\n  AFTERWORD_TEST_A = spaced value  \nAFTERWORD_TEST_B=2\n",
        )
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["AFTERWORD_TEST_A"], "spaced value")
        self.assertEqual(os.environ["AFTERWORD_TEST_B"], "2")

    def test_quotes_are_stripped(self) -> None:
        p = self._write(b"AFTERWORD_TEST_A='single'\nAFTERWORD_TEST_B=\"double\"\n")
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["AFTERWORD_TEST_A"], "single")
        self.assertEqual(os.environ["AFTERWORD_TEST_B"], "double")

    def test_does_not_override_existing_env(self) -> None:
        os.environ["AFTERWORD_TEST_A"] = "from-real-env"
        p = self._write(b"AFTERWORD_TEST_A=from-file\n")
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["AFTERWORD_TEST_A"], "from-real-env")

    def test_utf8_bom_is_stripped(self) -> None:
        # Notepad / PowerShell redirection often writes a BOM; it
        # must not corrupt the first key.
        p = self._write(b"\xef\xbb\xbfAFTERWORD_TEST_A=hello\n")
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["AFTERWORD_TEST_A"], "hello")

    def test_utf16_file_is_decoded(self) -> None:
        # Notepad "Unicode" save: UTF-16-LE with BOM. Read as UTF-8
        # this yields NUL-riddled text and silently matches nothing.
        p = self._write("AFTERWORD_TEST_A=hello\n".encode("utf-16"))
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["AFTERWORD_TEST_A"], "hello")

    def test_missing_file_is_ignored(self) -> None:
        config_mod._load_dotenv(self._dir / "does-not-exist.env")

    def test_undecodable_bytes_do_not_crash(self) -> None:
        # e.g. a Latin-1 é in a comment; loader replaces and continues.
        p = self._write(b"# caf\xe9\nAFTERWORD_TEST_A=ok\n")
        config_mod._load_dotenv(p)
        self.assertEqual(os.environ["AFTERWORD_TEST_A"], "ok")


class ResolveDirTests(unittest.TestCase):
    """AFTERWORD_DATA_DIR: relative paths belong to the server folder."""

    def test_relative_path_is_taken_from_base_not_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as base, \
                tempfile.TemporaryDirectory() as elsewhere:
            old = os.getcwd()
            os.chdir(elsewhere)
            try:
                got = config_mod._resolve_dir("./data", Path(base))
            finally:
                os.chdir(old)
            self.assertEqual(got, (Path(base) / "data").resolve())

    def test_absolute_path_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            got = config_mod._resolve_dir(d, Path("/unused"))
            self.assertEqual(got, Path(d).resolve())


class LegacyNamesTests(unittest.TestCase):
    """Settings named ``LR_*`` (before the AfterWord prefix) still work."""

    def apply(self, env: dict[str, str]) -> dict[str, str]:
        env = dict(env)
        config_mod.apply_legacy_names(env)
        return env

    def test_old_name_is_copied_to_the_new_name(self) -> None:
        env = self.apply({"LR_PORT": "9000", "LR_TEXT_MODEL": "m"})
        self.assertEqual(env["AFTERWORD_PORT"], "9000")
        self.assertEqual(env["AFTERWORD_TEXT_MODEL"], "m")

    def test_new_name_wins(self) -> None:
        env = self.apply({"LR_PORT": "9000", "AFTERWORD_PORT": "8001"})
        self.assertEqual(env["AFTERWORD_PORT"], "8001")

    def test_empty_old_value_is_ignored(self) -> None:
        env = self.apply({"LR_HOST": ""})
        self.assertNotIn("AFTERWORD_HOST", env)

    def test_openrouter_key_serves_both_models(self) -> None:
        env = self.apply({"LR_OPENROUTER_API_KEY": "k"})
        self.assertEqual(env["AFTERWORD_TRANSCRIPTION_API_KEY"], "k")
        self.assertEqual(env["AFTERWORD_TEXT_API_KEY"], "k")
        self.assertNotIn("AFTERWORD_OPENROUTER_API_KEY", env)

    def test_specific_keys_beat_the_openrouter_key(self) -> None:
        env = self.apply({
            "LR_OPENROUTER_API_KEY": "shared",
            "LR_TEXT_API_KEY": "text",
            "LR_MINIMAX_API_KEY": "minimax",
        })
        self.assertEqual(env["AFTERWORD_TEXT_API_KEY"], "text")
        self.assertEqual(env["AFTERWORD_TRANSCRIPTION_API_KEY"], "shared")

    def test_minimax_key_beats_the_openrouter_key_for_text(self) -> None:
        env = self.apply({"LR_OPENROUTER_API_KEY": "shared",
                          "LR_MINIMAX_API_KEY": "minimax"})
        self.assertEqual(env["AFTERWORD_TEXT_API_KEY"], "minimax")

    def test_load_reads_old_names(self) -> None:
        # No .env files: only the patched environment counts.
        with mock.patch.object(config_mod, "_load_dotenv", lambda path: None), \
                mock.patch.dict(os.environ, {"LR_PORT": "9123"}):
            os.environ.pop("AFTERWORD_PORT", None)
            self.assertEqual(config_mod.load().port, 9123)


class ExposureTests(unittest.TestCase):
    """Listening beyond this machine needs a password (or an explicit opt-out)."""

    def cfg(self, host: str, **kw) -> config_mod.Config:
        return config_mod.Config(
            data_dir=Path("."), host=host, port=8000, transcription_api_key="",
            text_api_key="", ffmpeg_path="ffmpeg", max_upload_bytes=1, **kw,
        )

    def test_loopback_needs_no_password(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"):
            self.assertEqual(config_mod.exposure_problem(self.cfg(host)), "", host)

    def test_network_without_password_is_refused(self) -> None:
        for host in ("0.0.0.0", "::", "192.168.1.20", "afterword.local"):
            problem = config_mod.exposure_problem(self.cfg(host))
            self.assertIn("AFTERWORD_WEB_PASSWORD", problem, host)

    def test_network_with_password_is_allowed(self) -> None:
        self.assertEqual(
            config_mod.exposure_problem(self.cfg("0.0.0.0", web_password="pw")), "")

    def test_allow_open_is_an_explicit_opt_out(self) -> None:
        self.assertEqual(
            config_mod.exposure_problem(self.cfg("0.0.0.0", allow_open=True)), "")

    def test_default_host_is_this_machine(self) -> None:
        with mock.patch.object(config_mod, "_load_dotenv", lambda path: None), \
                mock.patch.dict(os.environ, {}, clear=True):
            cfg = config_mod.load()
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertFalse(cfg.allow_open)


if __name__ == "__main__":
    unittest.main()