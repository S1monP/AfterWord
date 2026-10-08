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
    Flask, abort, current_app, jsonify, redirect, render_template, request,
    send_file, url_for,
)

import config as config_mod
import search
import services
import services.notes  # noqa: F401  (exposes NotesService, render_html, constants)
import services.tasks  # noqa: F401  (exposes TasksService, KINDS, ALLOWED_TRANSITIONS)
import services.events  # noqa: F401  (exposes EventsService, compute_month_grid, suggest_match_confidence)
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
        # Helpers for task date rendering. Available in every template.
        from services.tasks import is_due_today as _is_due_today
        from services.tasks import is_overdue as _is_overdue
        return {
            "unsorted_count": min(count, 99),
            "now_iso": storage.now_iso(),
            "is_overdue": _is_overdue,
            "is_due_today": _is_due_today,
        }

    _register_filters(app)
    _register_routes(app)
    return app


def current_app_storage() -> storage.Storage:
    """Pull the Storage instance off the active Flask app."""
    return current_app.config["LR_STORAGE"]


def current_app_notes_service() -> services.notes.NotesService:
    """Pull the NotesService off the active Flask app."""
    return current_app.config["LR_NOTES_SERVICE"]


def current_app_tasks_service() -> services.tasks.TasksService:
    """Pull the TasksService off the active Flask app."""
    return current_app.config["LR_TASKS_SERVICE"]


def current_app_events_service() -> services.events.EventsService:
    """Pull the EventsService off the active Flask app."""
    return current_app.config["LR_EVENTS_SERVICE"]


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
        deadlines+Tasks (right), and notes/lectures/meetings (centre)."""
        recent_notes, _ = current_app_notes_service().list(limit=5, offset=0)
        open_tasks, open_task_total = current_app_tasks_service().list(
            kind="task", status="open", limit=5, offset=0,
        )
        open_deadlines, _ = current_app_tasks_service().list(
            kind="deadline", status="open", limit=5, offset=0,
        )
        return render_template(
            "dashboard.html",
            recent_notes=recent_notes,
            open_tasks=open_tasks,
            open_task_count=open_task_total,
            open_deadlines=open_deadlines,
        )

    def _render_recordings(kind_filter: Optional[str] = None,
                           page_title: str = "Recordings",
                           sub: str = "Newest first. Click a row to open.") -> Any:
        all_sessions = current_app_storage().list_sessions()
        if kind_filter is not None:
            sessions = [s for s in all_sessions if s.kind == kind_filter]
        else:
            sessions = all_sessions
        return render_template(
            "index.html",
            sessions=sessions,
            page_title=page_title,
            page_sub=sub,
            kind_filter=kind_filter,
            status_label=_STATUS_LABEL,
            duration_for=_estimate_duration_seconds,
        )

    @app.get("/recordings")
    def ui_index() -> Any:
        return _render_recordings()

    @app.get("/lectures")
    def ui_lectures() -> Any:
        return _render_recordings(
            kind_filter="lecture",
            page_title="Lectures",
            sub="Recordings tagged as lectures. Use Recordings → to see all.",
        )

    @app.get("/meetings")
    def ui_meetings() -> Any:
        return _render_recordings(
            kind_filter="meeting",
            page_title="Meetings",
            sub="Recordings tagged as meetings. Use Recordings → to see all.",
        )

    @app.get("/notes")
    def ui_notes() -> Any:
        """List view: title, updated time, snippet. Search is via
        a query string param ``q`` so it's bookmarkable."""
        q = request.args.get("q", "").strip()
        notes, total = current_app_notes_service().list(
            limit=50, offset=0, search=q or None,
        )
        return render_template(
            "notes.html",
            notes=notes,
            total=total,
            q=q,
        )

    @app.get("/notes/new")
    def ui_notes_new() -> Any:
        """Empty form for creating a note. The form posts to the API."""
        return render_template("note.html", mode="new", note=None)

    @app.get("/notes/<note_id>")
    def ui_note_detail(note_id: str) -> Any:
        note = current_app_notes_service().get(note_id)
        if note is None:
            abort(404)
        return render_template(
            "note.html", mode="read", note=note,
        )

    @app.get("/notes/<note_id>/edit")
    def ui_note_edit(note_id: str) -> Any:
        note = current_app_notes_service().get(note_id)
        if note is None:
            abort(404)
        return render_template(
            "note.html", mode="edit", note=note,
        )

    @app.post("/notes/new")
    def create_note_action() -> Any:
        """Form action: create a note from the editor and redirect
        to its detail page. JSON API at /api/v1/notes is the canonical
        path; this is a thin HTML shortcut for the no-JS path."""
        title = (request.form.get("title") or "").strip()
        body_md = request.form.get("body_md") or ""
        try:
            note = current_app_notes_service().create(
                title=title, body_md=body_md,
            )
        except ValueError as exc:
            return (
                render_template(
                    "note.html", mode="new", note=None,
                    error=str(exc), form_title=title, form_body=body_md,
                ),
                400,
            )
        return redirect(url_for("ui_note_detail", note_id=note.id))

    @app.post("/notes/<note_id>")
    def update_note_action(note_id: str) -> Any:
        """Form action: update a note from the editor."""
        title = request.form.get("title")
        body_md = request.form.get("body_md")
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["title"] = title
        if body_md is not None:
            kwargs["body_md"] = body_md
        try:
            current_app_notes_service().update(note_id, **kwargs)
        except KeyError:
            abort(404)
        except ValueError as exc:
            existing = current_app_notes_service().get(note_id)
            return (
                render_template(
                    "note.html", mode="edit", note=existing,
                    error=str(exc),
                ),
                400,
            )
        return redirect(url_for("ui_note_detail", note_id=note_id))

    @app.post("/notes/<note_id>/delete")
    def delete_note_action(note_id: str) -> Any:
        deleted = current_app_notes_service().delete(note_id)
        if not deleted:
            abort(404)
        return redirect(url_for("ui_notes"))

    @app.get("/todos")
    def ui_todos() -> Any:
        """The Tasks page: list view of ``kind='task'`` rows +
        quick-add form."""
        status_filter = request.args.get("status")
        if status_filter not in (None, "", "open", "done", "cancelled"):
            status_filter = None
        tasks, total = current_app_tasks_service().list(
            kind="task", status=status_filter or None, limit=100,
        )
        open_count = sum(1 for t in tasks if t.status == "open")
        return render_template(
            "todos.html",
            tasks=tasks,
            total=total,
            open_count=open_count,
            status_filter=status_filter or "",
        )

    @app.post("/todos/quick")
    def ui_todos_quick() -> Any:
        """Quick-add a personal task. Optional fields: priority, due_at.

        ``due_at`` is optional for tasks (kind='task' here). Empty
        form values are normalised to ``None`` so the service sees a
        clean value.
        """
        title = (request.form.get("title") or "").strip()
        if not title:
            return redirect(url_for("ui_todos"))
        priority = (request.form.get("priority") or "medium").strip()
        due_at_raw = (request.form.get("due_at") or "").strip()
        try:
            current_app_tasks_service().create(
                title=title, kind="task",
                priority=priority, due_at=due_at_raw or None,
            )
        except ValueError:
            return redirect(url_for("ui_todos"))
        return redirect(url_for("ui_todos"))

    @app.get("/deadlines")
    def ui_deadlines() -> Any:
        """The Deadlines page: list view of ``kind='deadline'`` rows
        + quick-add form. Deadlines MUST have a date; the form
        enforces that with HTML5 ``required`` and the service enforces
        it server-side."""
        status_filter = request.args.get("status")
        if status_filter not in (None, "", "open", "done", "cancelled"):
            status_filter = None
        deadlines, total = current_app_tasks_service().list(
            kind="deadline", status=status_filter or None, limit=100,
        )
        open_count = sum(1 for t in deadlines if t.status == "open")
        return render_template(
            "deadlines.html",
            deadlines=deadlines,
            total=total,
            open_count=open_count,
            status_filter=status_filter or "",
        )

    @app.post("/deadlines/quick")
    def ui_deadlines_quick() -> Any:
        """Quick-add a deadline. ``due_at`` is required.

        Optional: ``description``, ``course_id``, ``source_ref``,
        ``source_offset_ms`` (the last one is hidden in the simple
        form; LLM-extracted deadlines populate it automatically).
        """
        title = (request.form.get("title") or "").strip()
        if not title:
            return redirect(url_for("ui_deadlines"))
        priority = (request.form.get("priority") or "medium").strip()
        due_at_raw = (request.form.get("due_at") or "").strip()
        description = (request.form.get("description") or "").strip()
        course_id = (request.form.get("course_id") or "").strip()
        try:
            current_app_tasks_service().create(
                title=title, kind="deadline",
                priority=priority,
                due_at=due_at_raw or None,
                description=description or None,
                course_id=course_id or None,
            )
        except ValueError:
            return redirect(url_for("ui_deadlines"))
        return redirect(url_for("ui_deadlines"))

    @app.get("/calendar")
    def ui_calendar() -> Any:
        return render_template("calendar.html")

    @app.get("/unsorted")
    def ui_unsorted() -> Any:
        return render_template("unsorted.html")

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

    # --- API: notes (CRUD + render) -------------------------------------
    def _note_to_dict(n: storage.Note) -> dict[str, Any]:
        return n.to_api_dict()

    @app.get("/api/v1/notes")
    def api_list_notes() -> Any:
        """List notes, newest first.

        Query params: ``limit`` (1-200, default 50), ``offset`` (>=0,
        default 0), ``q`` (substring search on title and body).
        """
        try:
            limit = int(request.args.get("limit", 50))
            offset = int(request.args.get("offset", 0))
        except ValueError:
            return jsonify({"error": "invalid_limit_or_offset"}), 400
        if limit < 1 or limit > 200:
            return jsonify({"error": "invalid_limit",
                            "detail": "limit must be 1..200"}), 400
        if offset < 0:
            return jsonify({"error": "invalid_offset",
                            "detail": "offset must be >= 0"}), 400
        q = request.args.get("q", "").strip() or None
        try:
            notes, total = current_app_notes_service().list(
                limit=limit, offset=offset, search=q,
            )
        except ValueError as exc:
            return jsonify({"error": "invalid_payload",
                            "detail": str(exc)}), 400
        return jsonify({
            "notes": [_note_to_dict(n) for n in notes],
            "total": total,
            "limit": limit,
            "offset": offset,
            "q": q,
        })

    @app.post("/api/v1/notes")
    def api_create_note() -> Any:
        """Create a new note. Body: ``{title, body_md?, source?, source_ref?}``."""
        payload = request.get_json(silent=True) or {}
        try:
            note = current_app_notes_service().create(
                title=payload.get("title", ""),
                body_md=payload.get("body_md", "") or "",
                source=payload.get("source") or "manual",
                source_ref=payload.get("source_ref"),
            )
        except ValueError as exc:
            return jsonify({"error": "invalid_payload",
                            "detail": str(exc)}), 400
        return jsonify({"note": _note_to_dict(note)}), 201

    @app.get("/api/v1/notes/<note_id>")
    def api_get_note(note_id: str) -> Any:
        note = current_app_notes_service().get(note_id)
        if note is None:
            return jsonify({"error": "not_found"}), 404
        return jsonify({"note": _note_to_dict(note)})

    @app.patch("/api/v1/notes/<note_id>")
    def api_update_note(note_id: str) -> Any:
        """Partial update. Body: any of ``{title, body_md, pinned}``."""
        payload = request.get_json(silent=True) or {}
        kwargs: dict[str, Any] = {}
        if "title" in payload:
            kwargs["title"] = payload["title"]
        if "body_md" in payload:
            kwargs["body_md"] = payload["body_md"] or ""
        if "pinned" in payload:
            kwargs["pinned"] = bool(payload["pinned"])
        try:
            note = current_app_notes_service().update(note_id, **kwargs)
        except KeyError:
            return jsonify({"error": "not_found"}), 404
        except ValueError as exc:
            return jsonify({"error": "invalid_payload",
                            "detail": str(exc)}), 400
        return jsonify({"note": _note_to_dict(note)})

    @app.delete("/api/v1/notes/<note_id>")
    def api_delete_note(note_id: str) -> Any:
        deleted = current_app_notes_service().delete(note_id)
        if not deleted:
            return jsonify({"error": "not_found"}), 404
        return ("", 204)

    @app.post("/api/v1/notes/render")
    def api_render_note() -> Any:
        """Render markdown to safe HTML. Body: ``{body_md}``.

        Used by the live-preview side of the editor. The output is
        HTML with raw HTML escaped (markdown library default).
        """
        payload = request.get_json(silent=True) or {}
        body_md = payload.get("body_md", "") or ""
        if not isinstance(body_md, str):
            return jsonify({"error": "invalid_payload",
                            "detail": "body_md must be a string"}), 400
        # Hard cap to keep this endpoint cheap; same limit as the service.
        if len(body_md) > services.notes.MAX_BODY_MD_LEN:
            return jsonify({
                "error": "invalid_payload",
                "detail": f"body_md must be at most "
                          f"{services.notes.MAX_BODY_MD_LEN} chars",
            }), 400
        return jsonify({"html": services.notes.render_html(body_md)})

    # --- API: tasks (CRUD + status transitions) --------------------------
    def _task_to_dict(t: storage.Task) -> dict[str, Any]:
        return t.to_api_dict()

    @app.get("/api/v1/tasks")
    def api_list_tasks() -> Any:
        """List tasks. Query params: ``kind`` ('task' or 'deadline'),
        ``status``, ``priority``, ``limit`` (1-200, default 100),
        ``offset`` (>=0)."""
        try:
            limit = int(request.args.get("limit", 100))
            offset = int(request.args.get("offset", 0))
        except ValueError:
            return jsonify({"error": "invalid_limit_or_offset"}), 400
        if limit < 1 or limit > 200:
            return jsonify({"error": "invalid_limit",
                            "detail": "limit must be 1..200"}), 400
        if offset < 0:
            return jsonify({"error": "invalid_offset"}), 400
        kind = request.args.get("kind") or None
        status = request.args.get("status") or None
        priority = request.args.get("priority") or None
        try:
            tasks, total = current_app_tasks_service().list(
                kind=kind, status=status, priority=priority,
                limit=limit, offset=offset,
            )
        except ValueError as exc:
            return jsonify({"error": "invalid_payload",
                            "detail": str(exc)}), 400
        return jsonify({
            "tasks": [_task_to_dict(t) for t in tasks],
            "total": total,
            "limit": limit,
            "offset": offset,
            "kind": kind,
            "status": status,
            "priority": priority,
        })

    @app.post("/api/v1/tasks")
    def api_create_task() -> Any:
        """Create a task. Body: ``{title, kind, body_md?, description?,
        priority?, due_at?, source?, source_ref?, source_offset_ms?,
        course_id?, pinned?}``.

        ``kind`` is 'task' (personal) or 'deadline' (course-linked,
        requires ``due_at``). ``priority`` defaults to 'medium'.
        """
        payload = request.get_json(silent=True) or {}
        try:
            task = current_app_tasks_service().create(
                title=payload.get("title", ""),
                body_md=payload.get("body_md"),
                description=payload.get("description"),
                priority=payload.get("priority") or "medium",
                kind=payload.get("kind") or "task",
                due_at=payload.get("due_at"),
                source=payload.get("source") or "manual",
                source_ref=payload.get("source_ref"),
                source_offset_ms=payload.get("source_offset_ms"),
                course_id=payload.get("course_id"),
                pinned=bool(payload.get("pinned", False)),
            )
        except ValueError as exc:
            return jsonify({"error": "invalid_payload",
                            "detail": str(exc)}), 400
        return jsonify({"task": _task_to_dict(task)}), 201

    @app.get("/api/v1/tasks/<task_id>")
    def api_get_task(task_id: str) -> Any:
        task = current_app_tasks_service().get(task_id)
        if task is None:
            return jsonify({"error": "not_found"}), 404
        return jsonify({"task": _task_to_dict(task)})

    @app.patch("/api/v1/tasks/<task_id>")
    def api_update_task(task_id: str) -> Any:
        """Partial update. Body: any of ``{title, body_md, description,
        priority, kind, due_at, source_offset_ms, course_id, pinned}``.

        Pass ``due_at=null`` explicitly to clear the date. ``kind`` can
        also be changed (e.g. from 'task' to 'deadline'); if the new
        kind is 'deadline' and there's no ``due_at``, the request is
        rejected. Status is changed via the status endpoint so
        transition rules are enforced.
        """
        payload = request.get_json(silent=True) or {}
        kwargs: dict[str, Any] = {}
        for field in ("title", "body_md", "description", "priority",
                      "kind"):
            if field in payload:
                kwargs[field] = payload[field]
        # Sentinel-friendly fields: pass through, the service handles
        # ``None`` as "clear".
        for field in ("due_at", "source_offset_ms", "course_id",
                      "pinned"):
            if field in payload:
                kwargs[field] = payload[field]
        try:
            task = current_app_tasks_service().update(task_id, **kwargs)
        except KeyError:
            return jsonify({"error": "not_found"}), 404
        except ValueError as exc:
            return jsonify({"error": "invalid_payload",
                            "detail": str(exc)}), 400
        return jsonify({"task": _task_to_dict(task)})

    @app.post("/api/v1/tasks/<task_id>/status")
    def api_set_task_status(task_id: str) -> Any:
        """Transition a task's status. Body: ``{status: "open"|"done"|"cancelled"}``."""
        payload = request.get_json(silent=True) or {}
        status = payload.get("status")
        if status is None:
            return jsonify({"error": "invalid_payload",
                            "detail": "field 'status' is required"}), 400
        try:
            task = current_app_tasks_service().set_status(task_id, status)
        except KeyError:
            return jsonify({"error": "not_found"}), 404
        except ValueError as exc:
            return jsonify({"error": "invalid_payload",
                            "detail": str(exc)}), 400
        return jsonify({"task": _task_to_dict(task)})

    @app.delete("/api/v1/tasks/<task_id>")
    def api_delete_task(task_id: str) -> Any:
        deleted = current_app_tasks_service().delete(task_id)
        if not deleted:
            return jsonify({"error": "not_found"}), 404
        return ("", 204)

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
