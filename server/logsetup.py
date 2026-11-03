"""Logging for the server: one line format with a timestamp, a level
from the environment, and an optional log file that rotates.

Modules log through ``logging.getLogger(__name__)``; only the entry
points (``serve.py``, ``app.py``, the demo server) call :func:`setup`.
Tests never do, so they stay quiet apart from warnings.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
#: A log file is rotated at this size; this many old files are kept.
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3

_SERVER_DIR = Path(__file__).resolve().parent
_log = logging.getLogger(__name__)


def setup(log_file: str | None = None, level: str | None = None) -> Path | None:
    """Send all log records to the console or, with ``log_file``, to that
    file (relative paths are taken from the server folder).

    ``log_file`` defaults to ``AFTERWORD_LOG_FILE`` and ``level`` to
    ``AFTERWORD_LOG_LEVEL`` (``INFO`` when unset). Calling it again
    replaces the handler it added before. Returns the log file's path,
    or None when logging to the console.
    """
    if log_file is None:
        log_file = os.environ.get("AFTERWORD_LOG_FILE", "")
    if level is None:
        level = os.environ.get("AFTERWORD_LOG_LEVEL", "")
    level = (level.strip() or "INFO").upper()

    path: Path | None = None
    if log_file.strip():
        path = Path(log_file.strip())
        if not path.is_absolute():
            path = _SERVER_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8",
        )
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
    handler._afterword = True  # type: ignore[attr-defined]

    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, "_afterword", False)]:
        root.removeHandler(old)
        old.close()
    root.addHandler(handler)
    try:
        root.setLevel(level)
    except ValueError:
        root.setLevel(logging.INFO)
        _log.warning("unknown AFTERWORD_LOG_LEVEL %r; using INFO", level)

    _log_uncaught_exceptions()
    return path


def _log_uncaught_exceptions() -> None:
    """Crashes outside a request (main thread or a background thread)
    are logged too, so they reach the log file when there is no
    console window."""
    def main_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        _log.critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        _log.error("uncaught exception in thread %s", name,
                   exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = main_hook
    threading.excepthook = thread_hook
