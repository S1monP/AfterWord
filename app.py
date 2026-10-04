"""Lecture Recorder Server — Phase A.

A small Flask app: receive audio chunks over HTTP, stitch them into a
single WAV per session, and serve a minimal web UI to browse and play
recordings.

Run:

    python app.py

Configuration is via env vars (see :mod:`config`).
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from flask import (
    Flask, abort, current_app, jsonify, render_template, request, send_file,
)

import config as config_mod
import search
import storage
from services import processing, transcode


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(config: Optional[config_mod.Config] = None) -> Flask:
    """Build and configure a Flask app. ``config`` is injectable for tests."""
    cfg = config or config_mod.load()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["LR_CONFIG"] = cfg
    app.config["LR_STORAGE"] = storage.Storage(cfg)
    app.config["LR_STORAGE"].initialise()
    app.secret_key = os.environ.get("LR_SECRET", "lr-dev-secret-not-for-prod")

    if not transcode.ffmpeg_available(cfg.ffmpeg_path):
        print(
            f"[LR ] WARNING: ffmpeg not found at {cfg.ffmpeg_path!r}; "
            "non-WAV uploads (iPhone M4A, etc.) will be rejected. "
            "Install ffmpeg or set LR_FFMPEG_PATH.",
            flush=True,
        )
    else:
        v = transcode.ffmpeg_version(cfg.ffmpeg_path)
        print(f"[LR ] ffmpeg: {v}", flush=True)

    @app.context_processor
    def _inject_globals():
        # Pass the count of unclassified sessions to every template so
        # the top-bar badge can render. Capped at 99 in the display.
        try:
            count = sum(
                1 for s in current_app_storage().list_sessions()
                if s.kind is None and s.status == storage.STATUS_READY
            )
        except Exception:
            count = 0
        return {
            "unsorted_count": min(count, 99),
        }

    _register_filters(app)
    _register_routes(app)
    return app


def current_app_storage() -> storage.Storage:
    """Pull the Storage instance off the active Flask app."""
    return current_app.config["LR_STORAGE"]


# ---------------------------------------------------------------------------
# Jinja filters
# ---------------------------------------------------------------------------

def _register_filters(app: Flask) -> None:
    @app.template_filter("human_size")
    def _human_size(num: Optional[int]) -> str:
        if not num:
            return "0 B"
        size = float(num)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @app.template_filter("human_duration")
    def _human_duration(seconds: Optional[float]) -> str:
        if not seconds or seconds < 0:
            return "0:00"
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"

    @app.template_filter("iso_local")
    def _iso_local(iso: Optional[str]) -> str:
        if not iso:
            return "—"
        return iso.replace("T", " ").rstrip("Z")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_LABEL = {
    storage.STATUS_RECEIVING: "Receiving",
    storage.STATUS_STITCHING: "Stitching",
    storage.STATUS_READY: "Ready",
    storage.STATUS_FAILED: "Failed",
}


def _estimate_duration_seconds(session: storage.Session) -> float:
    """Best-effort duration estimate from full.wav size."""
    if not session.full_audio_size or session.full_audio_size <= 44:
        return 0.0
    if not session.channels or not session.bits_per_sample:
        return 0.0
    bytes_per_sample = session.bits_per_sample // 8
    bytes_per_sec = session.sample_rate * session.channels * bytes_per_sample
    payload = session.full_audio_size - 44
    if bytes_per_sec <= 0:
        return 0.0
    return payload / bytes_per_sec


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _register_routes(app: Flask) -> None:
    # --- Web UI ----------------------------------------------------------
    @app.get("/")
    def ui_dashboard() -> Any:
        """The home page: a 3-col bento with calendar, search,
        deadlines+Todos (right), and notes/lectures/meetings (centre)."""
        return render_template("dashboard.html")

    @app.get("/recordings")
    def ui_index() -> Any:
        sessions = current_app_storage().list_sessions()
        return render_template(
            "index.html",
            sessions=sessions,
            status_label=_STATUS_LABEL,
            duration_for=_estimate_duration_seconds,
        )

    @app.get("/sessions/<session_id>")
    def ui_session(session_id: str) -> Any:
        sess = current_app_storage().get_session(session_id)
        if sess is None:
            abort(404)
        return render_template(
            "session.html",
            session=sess,
            status_label=_STATUS_LABEL,
            duration_for=_estimate_duration_seconds,
        )

    @app.get("/upload")
    def ui_upload() -> Any:
        cfg = current_app.config["LR_CONFIG"]
        return render_template(
            "upload.html",
            max_upload_bytes=cfg.max_upload_bytes,
            ffmpeg_available=transcode.ffmpeg_available(cfg.ffmpeg_path),
        )

    # --- API: sessions (create / list / get) ----------------------------
    @app.post("/api/v1/sessions")
    def api_create_session() -> Any:
        payload = request.get_json(silent=True) or {}
        session_id = payload.get("id") or storage.new_session_id()
        if not storage.is_valid_session_id(session_id):
            return jsonify({
                "error": "invalid_session_id",
                "detail": "id must be 4-64 chars of [A-Za-z0-9_-]",
            }), 400
        try:
            expected_count = int(payload.get("expected_count", 0))
            sample_rate = int(payload.get("sample_rate", 22050))
            channels = int(payload.get("channels", 1))
            bits_per_sample = int(payload.get("bits_per_sample", 32))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_audio_format"}), 400
        started_at = payload.get("started_at") or storage.now_iso()
        title = payload.get("title")

        try:
            sess, created = current_app_storage().upsert_session(
                session_id,
                title=title,
                expected_count=expected_count,
                sample_rate=sample_rate,
                channels=channels,
                bits_per_sample=bits_per_sample,
                started_at=started_at,
            )
        except ValueError as exc:
            return jsonify({"error": "invalid_payload", "detail": str(exc)}), 400

        return jsonify({
            "created": created,
            "session": sess.to_api_dict(),
        }), (201 if created else 200)

    @app.get("/api/v1/sessions")
    def api_list_sessions() -> Any:
        sessions = current_app_storage().list_sessions()
        return jsonify({"sessions": [s.to_api_dict() for s in sessions]})

    @app.get("/api/v1/sessions/<session_id>")
    def api_get_session(session_id: str) -> Any:
        sess = current_app_storage().get_session(session_id)
        if sess is None:
            return jsonify({"error": "not_found"}), 404
        return jsonify({"session": sess.to_api_dict()})

    # --- API: chunk upload ----------------------------------------------
    @app.put("/api/v1/sessions/<session_id>/chunks/<int:chunk_index>")
    def api_put_chunk(session_id: str, chunk_index: int) -> Any:
        store = current_app_storage()
        if store.get_session(session_id) is None:
            return jsonify({"error": "not_found"}), 404
        body = request.get_data(cache=False, as_text=False)
        if not body:
            return jsonify({"error": "empty_body"}), 400
        try:
            size = store.write_chunk(session_id, chunk_index, body)
        except (ValueError, RuntimeError) as exc:
            return jsonify({"error": "rejected", "detail": str(exc)}), 409
        return jsonify({
            "session_id": session_id,
            "chunk_index": chunk_index,
            "byte_size": size,
            "received_at": storage.now_iso(),
        })

    # --- API: finish ----------------------------------------------------
    @app.post("/api/v1/sessions/<session_id>/finish")
    def api_finish_session(session_id: str) -> Any:
        store = current_app_storage()
        sess = store.get_session(session_id)
        if sess is None:
            return jsonify({"error": "not_found"}), 404
        result = store.finish_session(session_id)
        if not result.ok:
            if result.reason == "missing_chunks":
                present = sorted(store.list_received_chunks(session_id))
                return jsonify({
                    "error": "missing_chunks",
                    "missing": result.missing,
                    "present": present,
                }), 409
            return jsonify({
                "error": result.reason,
                "detail": sess.status_detail,
            }), 500
        try:
            processing.run_post_finish_pipeline(store, session_id)
        except Exception as exc:  # pragma: no cover
            print(f"[LR ] post-finish pipeline error: {exc!r}", flush=True)
        sess = store.get_session(session_id)
        return jsonify({
            "ok": True,
            "reason": result.reason,
            "session": sess.to_api_dict() if sess else None,
        })

    # --- API: audio serving ---------------------------------------------
    @app.get("/api/v1/sessions/<session_id>/audio")
    def api_get_audio(session_id: str) -> Any:
        store = current_app_storage()
        sess = store.get_session(session_id)
        if sess is None:
            abort(404)
        full = store.open_full_audio(session_id)
        if full is None:
            return jsonify({
                "error": "not_ready",
                "detail": "session has no full.wav yet",
                "status": sess.status,
            }), 409
        return send_file(
            str(full),
            mimetype="audio/wav",
            conditional=True,
            as_attachment=False,
            download_name=f"{sess.title or sess.id}.wav",
        )

    # --- API: recording-kind classification (manual override) ----------
    @app.post("/api/v1/sessions/<session_id>/kind")
    def api_set_kind(session_id: str) -> Any:
        """Manually set or clear a session's recording kind.

        Body: ``{"kind": "lecture" | "voice_memo" | "meeting" | "other" | null}``

        Returns the updated session. This is the only classification
        route that does real work in Tier 1; the LLM-driven
        ``classify`` route lands in Tier 3.
        """
        store = current_app_storage()
        if store.get_session(session_id) is None:
            return jsonify({"error": "not_found"}), 404
        payload = request.get_json(silent=True) or {}
        if "kind" not in payload:
            return jsonify({
                "error": "invalid_payload",
                "detail": "field 'kind' is required (string or null)",
            }), 400
        kind = payload["kind"]
        if kind is not None and kind not in storage.Storage.RECORDING_KINDS:
            return jsonify({
                "error": "invalid_kind",
                "detail": (
                    f"kind must be one of "
                    f"{sorted(storage.Storage.RECORDING_KINDS)} or null"
                ),
            }), 400
        confidence = payload.get("confidence")
        if confidence is None and kind is not None:
            # Manual override — treat as full confidence.
            confidence = 1.0
        try:
            sess = store.set_kind(session_id, kind, confidence=confidence)
        except ValueError as exc:
            return jsonify({"error": "invalid_payload", "detail": str(exc)}), 400
        return jsonify({"session": sess.to_api_dict()})

    # --- API: search (Phase A stub returns []) -------------------------
    @app.get("/api/v1/search")
    def api_search() -> Any:
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify({"hits": []})
        hits = search.search(
            q,
            course_id=request.args.get("course_id") or None,
            kind=request.args.get("kind") or None,
        )
        return jsonify({
            "hits": [
                {
                    "session_id": h.session_id,
                    "artifact_id": h.artifact_id,
                    "kind": h.kind,
                    "snippet": h.snippet,
                }
                for h in hits
            ],
        })

    # --- API: web audio upload ------------------------------------------
    @app.post("/api/v1/uploads/audio")
    def api_upload_audio() -> Any:
        """Accept one audio file from the browser, transcode to canonical
        WAV if needed, and create a one-chunk session.

        Multipart form fields:
          - file: the audio bytes (any format the browser or ffmpeg can read)
          - title: optional string; defaults to rec_YYYY-MM-DDTHHMM
        """
        cfg = current_app.config["LR_CONFIG"]
        store = current_app_storage()

        if "file" not in request.files:
            return jsonify({
                "error": "missing_file",
                "detail": "multipart field 'file' is required",
            }), 400

        upload = request.files["file"]
        raw = upload.read()
        if not raw:
            return jsonify({"error": "empty_file"}), 400
        if len(raw) > cfg.max_upload_bytes:
            return jsonify({
                "error": "file_too_large",
                "detail": (
                    f"file is {len(raw)} bytes; max is {cfg.max_upload_bytes}"
                ),
            }), 413

        # Title: empty/whitespace -> default; explicit bad -> 400.
        title_input = request.form.get("title")
        if title_input is not None and title_input.strip() != "":
            title_clean = _normalise_title(title_input)
            if title_clean is None:
                return jsonify({
                    "error": "invalid_title",
                    "detail": (
                        "title must be 1..200 chars of printable text "
                        "(letters, digits, spaces, basic punctuation)"
                    ),
                }), 400
            title = title_clean
        else:
            title = None  # substituted with default after we know mtime

        # Save the upload to the inbox, preserving the original extension
        # so ffmpeg can sniff it correctly.
        original = upload.filename or "upload"
        suffix = Path(original).suffix or ".bin"
        inbox_token = storage.new_session_id()
        inbox_path = cfg.inbox_dir / f"{inbox_token}{suffix}"
        try:
            inbox_path.write_bytes(raw)
        except OSError as exc:
            return jsonify({"error": "io_error", "detail": str(exc)}), 500

        started_at = _file_mtime_iso(inbox_path) or storage.now_iso()
        if title is None:
            title = _default_title_from_iso(started_at)

        work_dir: Optional[Path] = None
        try:
            fmt = transcode.sniff_format(inbox_path)
            if transcode.is_canonical_wav(fmt):
                wav_path = inbox_path
            else:
                if not transcode.ffmpeg_available(cfg.ffmpeg_path):
                    return jsonify({
                        "error": "ffmpeg_missing",
                        "detail": (
                            f"this file is {fmt.kind} and needs to be "
                            f"transcoded, but ffmpeg was not found at "
                            f"{cfg.ffmpeg_path!r}. Install ffmpeg or set "
                            "LR_FFMPEG_PATH."
                        ),
                    }), 503
                work_dir = cfg.inbox_dir / f"work-{inbox_token}"
                work_dir.mkdir(parents=True, exist_ok=True)
                wav_path = work_dir / "out.wav"
                transcode.transcode_to_wav(
                    inbox_path, wav_path, ffmpeg_path=cfg.ffmpeg_path,
                )

            wav_bytes = wav_path.read_bytes()
            if len(wav_bytes) < 44 or wav_bytes[0:4] != b"RIFF":
                return jsonify({
                    "error": "transcode_failed",
                    "detail": "output is not a valid WAV",
                }), 500

            sess = store.ingest_single_audio(
                session_id=inbox_token,
                title=title,
                wav_bytes=wav_bytes,
                started_at=started_at,
            )
        except transcode.TranscodeError as exc:
            return jsonify({
                "error": exc.reason,
                "detail": exc.detail,
            }), 503
        except Exception as exc:
            return jsonify({
                "error": "ingest_failed", "detail": str(exc),
            }), 500
        finally:
            try:
                if inbox_path.exists():
                    inbox_path.unlink()
            except OSError:
                pass
            if work_dir is not None and work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)

        return jsonify({
            "created": True,
            "session": sess.to_api_dict(),
            "redirect_url": f"/sessions/{sess.id}",
        }), 201


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"^[\w\s\-.,()\[\]!&'\"]+$", re.UNICODE)
_TITLE_MAX = 200


def _normalise_title(raw: str) -> Optional[str]:
    """Return a clean title, or None if it was empty / invalid."""
    title = raw.strip()
    if not title or len(title) > _TITLE_MAX:
        return None
    if not _TITLE_RE.match(title):
        return None
    return "".join(c for c in title if c.isprintable() and c not in "\n\r\t")


def _default_title_from_iso(iso_ts: str) -> str:
    """Convert ISO-8601 '2026-10-03T14:30:00Z' to 'rec_2026-10-03T1430'."""
    core = iso_ts.replace("Z", "").replace("z", "")
    if "T" in core:
        date, time_str = core.split("T", 1)
    else:
        date, time_str = core, "00:00"
    if len(time_str) >= 5 and time_str[2] == ":":
        time_str = time_str[:5].replace(":", "")
    else:
        time_str = "0000"
    return f"rec_{date}T{time_str}"


def _file_mtime_iso(path: Path) -> Optional[str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = config_mod.load()
    app = create_app(cfg)
    print(f"[LR ] listening on http://{cfg.host}:{cfg.port}", flush=True)
    app.run(host=cfg.host, port=cfg.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
