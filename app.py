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
from typing import Any, Optional

from flask import (
    Flask, abort, current_app, jsonify, render_template, request, send_file,
)

import config as config_mod
import search
import storage
from services import processing


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

