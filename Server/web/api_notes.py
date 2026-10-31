"""Notes API.
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

import services
import services.notes  # noqa: F401
import storage
from web.common import current_app_notes_service, current_app_storage


bp = Blueprint("notes_api", __name__)


# --- API: notes (CRUD + render) -------------------------------------
def _note_to_dict(n: storage.Note) -> dict[str, Any]:
    return n.to_api_dict()


@bp.get("/api/v1/notes")
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


@bp.post("/api/v1/notes")
def api_create_note() -> Any:
    """Create a new note. Body: ``{title, body_md?, course_id?, source?,
    source_ref?}``."""
    payload = request.get_json(silent=True) or {}
    try:
        note = current_app_notes_service().create(
            title=payload.get("title", ""),
            body_md=payload.get("body_md", "") or "",
            source=payload.get("source") or "manual",
            source_ref=payload.get("source_ref"),
            course_id=payload.get("course_id"),
        )
    except ValueError as exc:
        return jsonify({"error": "invalid_payload",
                        "detail": str(exc)}), 400
    return jsonify({"note": _note_to_dict(note)}), 201


@bp.get("/api/v1/notes/<note_id>")
def api_get_note(note_id: str) -> Any:
    note = current_app_notes_service().get(note_id)
    if note is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"note": _note_to_dict(note)})


@bp.patch("/api/v1/notes/<note_id>")
def api_update_note(note_id: str) -> Any:
    """Partial update. Body: any of ``{title, body_md, pinned,
    course_id}`` (``course_id`` null clears it)."""
    payload = request.get_json(silent=True) or {}
    kwargs: dict[str, Any] = {}
    if "title" in payload:
        kwargs["title"] = payload["title"]
    if "body_md" in payload:
        kwargs["body_md"] = payload["body_md"] or ""
    if "pinned" in payload:
        kwargs["pinned"] = bool(payload["pinned"])
    if "course_id" in payload:
        kwargs["course_id"] = payload["course_id"]
    try:
        note = current_app_notes_service().update(note_id, **kwargs)
    except KeyError:
        return jsonify({"error": "not_found"}), 404
    except ValueError as exc:
        return jsonify({"error": "invalid_payload",
                        "detail": str(exc)}), 400
    return jsonify({"note": _note_to_dict(note)})


@bp.delete("/api/v1/notes/<note_id>")
def api_delete_note(note_id: str) -> Any:
    """Delete a note. A voice memo's note goes with its recording (the
    audio): they are one thing."""
    note = current_app_notes_service().get(note_id)
    if note is None:
        return jsonify({"error": "not_found"}), 404
    store = current_app_storage()
    if note.source == "voice_memo" and note.source_ref and store.get_session(note.source_ref):
        store.delete_session(note.source_ref)     # takes the note too
    else:
        current_app_notes_service().delete(note_id)
    return ("", 204)
