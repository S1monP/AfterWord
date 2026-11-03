"""Tests for logsetup: format, level, log file and uncaught exceptions."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import logsetup

LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (INFO|WARNING|ERROR) +[\w.]+: ")


class LogSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        self._handlers = list(root.handlers)
        self._level = root.level
        self._hooks = (sys.excepthook, threading.excepthook)
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "logs" / "server.log"

    def tearDown(self) -> None:
        root = logging.getLogger()
        for h in list(root.handlers):
            if h not in self._handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(self._level)
        sys.excepthook, threading.excepthook = self._hooks
        self._tmp.cleanup()

    def lines(self) -> list[str]:
        for h in logging.getLogger().handlers:
            h.flush()
        return self.path.read_text(encoding="utf-8").splitlines()

    def test_file_lines_have_time_level_and_module(self) -> None:
        self.assertEqual(logsetup.setup(log_file=str(self.path), level="INFO"), self.path)
        logging.getLogger("web.pipeline").info("requeued %d", 2)
        line = self.lines()[-1]
        self.assertRegex(line, LINE)
        self.assertTrue(line.endswith("web.pipeline: requeued 2"), line)

    def test_level_filters_records(self) -> None:
        logsetup.setup(log_file=str(self.path), level="warning")
        logging.getLogger("x").info("hidden")
        logging.getLogger("x").warning("shown")
        text = "\n".join(self.lines())
        self.assertNotIn("hidden", text)
        self.assertIn("shown", text)

    def test_level_comes_from_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"AFTERWORD_LOG_LEVEL": "ERROR"}):
            logsetup.setup(log_file=str(self.path))
        self.assertEqual(logging.getLogger().level, logging.ERROR)

    def test_unknown_level_falls_back_to_info(self) -> None:
        logsetup.setup(log_file=str(self.path), level="chatty")
        self.assertEqual(logging.getLogger().level, logging.INFO)
        self.assertIn("unknown AFTERWORD_LOG_LEVEL", "\n".join(self.lines()))

    def test_file_rotates(self) -> None:
        logsetup.setup(log_file=str(self.path), level="INFO")
        mine = [h for h in logging.getLogger().handlers if getattr(h, "_afterword", False)]
        self.assertEqual(len(mine), 1)
        self.assertIsInstance(mine[0], logging.handlers.RotatingFileHandler)
        self.assertEqual(mine[0].maxBytes, logsetup.MAX_BYTES)
        self.assertEqual(mine[0].backupCount, logsetup.BACKUPS)

    def test_setup_again_replaces_its_handler(self) -> None:
        logsetup.setup(log_file=str(self.path), level="INFO")
        logsetup.setup(log_file=str(self.path), level="INFO")
        mine = [h for h in logging.getLogger().handlers if getattr(h, "_afterword", False)]
        self.assertEqual(len(mine), 1)

    def test_console_when_no_file(self) -> None:
        self.assertIsNone(logsetup.setup(log_file="", level="INFO"))
        mine = [h for h in logging.getLogger().handlers if getattr(h, "_afterword", False)]
        self.assertIsInstance(mine[0], logging.StreamHandler)
        self.assertNotIsInstance(mine[0], logging.FileHandler)

    def test_crash_in_a_thread_is_logged_with_its_traceback(self) -> None:
        logsetup.setup(log_file=str(self.path), level="INFO")

        def boom() -> None:
            raise RuntimeError("worker broke")

        t = threading.Thread(target=boom, name="afterword-test-worker")
        t.start()
        t.join()
        text = "\n".join(self.lines())
        self.assertIn("uncaught exception in thread afterword-test-worker", text)
        self.assertIn("RuntimeError: worker broke", text)
        self.assertIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
