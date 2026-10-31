"""AI NoteTaker server: Flask app factory and development entry point.

Receives recordings from the device (announce, chunk uploads, finish),
stitches the AAC chunks, runs the AI pipeline (transcribe, classify,
summarise, extract) in the background, and serves the web UI.

Development:  python app.py      (Flask's server)
Production:   python serve.py    (waitress; see serve.py)

Configuration is via env vars / .env (see :mod:`config`). Routes live in
the :mod:`web` package as blueprints.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from flask import Flask, jsonify

import services
import services.notes  # noqa: F401
import services.tasks  # noqa: F401
import services.events  # noqa: F401
import services.classification  # noqa: F401
import services.courses  # noqa: F401
import services.llm  # noqa: F401
import auth
import config as config_mod
import storage
from services import transcode
import web
from web.common import nav_context, register_filters
from web.pipeline import _start_pipeline_worker, sweep_stranded_sessions

# Re-exported: tests and scripts import these from ``app``.
from web.common import _default_title_from_iso, _normalise_title  # noqa: F401
from web.pipeline import (  # noqa: F401
    _PIPELINE_LOCK, _PIPELINES_IN_FLIGHT, _enqueue_pipeline,
)


def create_app(config: Optional[config_mod.Config] = None) -> Flask:
    """Build and configure a Flask app. ``config`` is injectable for tests."""
    cfg = config or config_mod.load()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["LR_CONFIG"] = cfg
    app.config["LR_STORAGE"] = storage.Storage(cfg)
    app.config["LR_STORAGE"].initialise()
    # NotesService is constructed once per app and cached. It holds a
    # reference to the storage layer, which itself is per-app.
    app.config["LR_NOTES_SERVICE"] = services.notes.NotesService(
        app.config["LR_STORAGE"]
    )
    app.config["LR_TASKS_SERVICE"] = services.tasks.TasksService(
        app.config["LR_STORAGE"]
    )
    app.config["LR_EVENTS_SERVICE"] = services.events.EventsService(
        app.config["LR_STORAGE"]
    )
    app.config["LR_CLASSIFICATION_SERVICE"] = (
        services.classification.ClassificationService(
            app.config["LR_STORAGE"]
        )
    )
    app.config["LR_COURSES_SERVICE"] = services.courses.CoursesService(
        app.config["LR_STORAGE"]
    )
    # LLM providers. The factory reads ``LR_TRANSCRIPTION_API_KEY``
    # / ``LR_TEXT_API_KEY`` from the process env (a `.env` file in
    # the Server directory is loaded by ``config.load()``; the
    # legacy ``LR_OPENROUTER_API_KEY`` / ``LR_MINIMAX_API_KEY``
    # names are accepted as fallbacks). Absent keys yield no-op
    # providers so the server can boot and the API surface still
    # works (it just returns no-op stub data).
    app.config["LR_LLM_PROVIDERS"] = services.llm.default_providers(
        transcription_key=cfg.transcription_api_key,
        text_key=cfg.text_api_key,
    )
    # Boot diagnostic: say loudly whether the API keys from .env /
    # env vars were actually picked up, so "the AI didn't work" is
    # diagnosable from the console alone. Prints provider names
    # only — never key material.
    _t = app.config["LR_LLM_PROVIDERS"]["transcript"]
    _x = app.config["LR_LLM_PROVIDERS"]["text"]
    print(
        f"[LR ] LLM providers: transcript={_t.name}"
        + (
            "  <- no LR_TRANSCRIPTION_API_KEY set!"
            if _t.name == "noop" else ""
        )
        + f", text={_x.name}"
        + (
            "  <- no LR_TEXT_API_KEY set!"
            if _x.name == "noop" else ""
        ),
        flush=True,
    )
    auth.init_app(app)
    # Refuse oversized bodies before reading them (the upload handlers
    # used to buffer the whole body, then check). The margin covers
    # multipart overhead on top of the file itself.
    app.config["MAX_CONTENT_LENGTH"] = cfg.max_upload_bytes + 1024 * 1024

    @app.errorhandler(413)
    def _too_large(_exc: Exception) -> Any:
        return jsonify({
            "error": "file_too_large",
            "detail": f"request is over {app.config['MAX_CONTENT_LENGTH']} bytes",
        }), 413

    if not transcode.ffmpeg_available(cfg.ffmpeg_path):
        print(
            f"[LR ] WARNING: ffmpeg not found at {cfg.ffmpeg_path!r}; "
            "non-AAC uploads (iPhone M4A, etc.) will be rejected. "
            "Install ffmpeg or set LR_FFMPEG_PATH.",
            flush=True,
        )
    else:
        v = transcode.ffmpeg_version(cfg.ffmpeg_path)
        print(f"[LR ] ffmpeg: {v}", flush=True)

    # Background pipeline worker + boot sweep. Always started: in
    # tests the queue stays empty (sync mode runs pipelines inline
    # inside the request), and the sweep is a no-op on a fresh data
    # directory. In production this is what survives a restart: any
    # session stranded mid-pipeline is requeued automatically.
    # The flag is env-wired here so operators can run fully
    # synchronous pipelines with LR_PIPELINE_BACKGROUND=0; tests
    # override app.config directly, which still wins.
    app.config.setdefault(
        "LR_PIPELINE_BACKGROUND",
        os.environ.get("LR_PIPELINE_BACKGROUND", "").strip().lower()
        not in ("0", "false", "no", "off"),
    )
    _start_pipeline_worker(app)
    try:
        sweep_stranded_sessions(app)
    except Exception as exc:  # pragma: no cover
        print(f"[LR ] boot sweep error: {exc!r}", flush=True)

    @app.context_processor
    def _inject_globals():
        # Helpers for task date rendering. Available in every template.
        from services.tasks import is_due_today as _is_due_today
        from services.tasks import is_overdue as _is_overdue
        from web.api_settings import ui_settings
        try:
            nav = nav_context()
        except Exception:  # pragma: no cover  (a page still renders)
            nav = None
        return {
            "ui_settings": ui_settings(),
            "nav": nav,
            "now_iso": storage.now_iso(),
            "is_overdue": _is_overdue,
            "is_due_today": _is_due_today,
        }

    register_filters(app)
    for bp in web.BLUEPRINTS:
        app.register_blueprint(bp)
    return app


def main() -> None:
    cfg = config_mod.load()
    app = create_app(cfg)
    print(f"[LR ] listening on http://{cfg.host}:{cfg.port}", flush=True)
    app.run(host=cfg.host, port=cfg.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

