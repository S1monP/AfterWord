"""Calendar events API (incl. ICS import)."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request

import storage
from web.common import current_app_events_service

bp = Blueprint("events_api", __name__)


# --- API: events (calendar) -----------------------------------------
def _event_to_dict(ev: storage.Event) -> dict[str, Any]:
    return ev.to_api_dict()


@bp.get("/api/v1/events")
def api_list_events() -> Any:
    """List events. Query params: ``kind`` (lecture/meeting/other),
    ``starts_after``, ``starts_before`` (ISO-8601), ``limit``
    (1-500, default 200), ``offset`` (>=0)."""
    try:
        limit = int(request.args.get("limit", 200))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "invalid_limit_or_offset"}), 400
    if limit < 1 or limit > 500:
        return jsonify(
            {
                "error": "invalid_limit",
                "detail": "limit must be 1..500",
            }
        ), 400
    if offset < 0:
        return jsonify(
            {
                "error": "invalid_offset",
                "detail": "offset must be >= 0",
            }
        ), 400
    try:
        events, total = current_app_events_service().list(
            kind=request.args.get("kind") or None,
            starts_after=request.args.get("starts_after") or None,
            starts_before=request.args.get("starts_before") or None,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        return jsonify(
            {
                "error": "invalid_payload",
                "detail": str(exc),
            }
        ), 400
    return jsonify(
        {
            "events": [_event_to_dict(e) for e in events],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@bp.post("/api/v1/events")
def api_create_event() -> Any:
    """Create an event. Body: ``{title, kind, starts_at, ends_at?,
    description?, location?, source?, source_ref?}``."""
    payload = request.get_json(silent=True) or {}
    repeat_until = (payload.get("repeat_until") or "").strip() or None
    try:
        if repeat_until:
            ev, count = current_app_events_service().create_recurring(
                title=payload.get("title", ""),
                kind=payload.get("kind", ""),
                starts_at=payload.get("starts_at", ""),
                ends_at=payload.get("ends_at"),
                description=payload.get("description"),
                location=payload.get("location"),
                source=payload.get("source") or "manual",
                source_ref=payload.get("source_ref"),
                course_id=payload.get("course_id"),
                repeat_until=repeat_until,
            )
        else:
            ev = current_app_events_service().create(
                title=payload.get("title", ""),
                kind=payload.get("kind", ""),
                starts_at=payload.get("starts_at", ""),
                ends_at=payload.get("ends_at"),
                description=payload.get("description"),
                location=payload.get("location"),
                source=payload.get("source") or "manual",
                source_ref=payload.get("source_ref"),
                course_id=payload.get("course_id"),
            )
            count = 1
    except ValueError as exc:
        return jsonify(
            {
                "error": "invalid_payload",
                "detail": str(exc),
            }
        ), 400
    return jsonify(
        {
            "event": _event_to_dict(ev),
            "created_count": count,
        }
    ), 201


@bp.get("/api/v1/events/<event_id>")
def api_get_event(event_id: str) -> Any:
    ev = current_app_events_service().get(event_id)
    if ev is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"event": _event_to_dict(ev)})


@bp.patch("/api/v1/events/<event_id>")
def api_update_event(event_id: str) -> Any:
    """Partial update. Body: any of ``{title, kind, starts_at,
    ends_at, description, location, source, source_ref}``.
    Pass ``ends_at=null`` explicitly to clear it; pass
    ``description=null`` etc. to clear those too."""
    payload = request.get_json(silent=True) or {}
    kwargs: dict[str, Any] = {}
    for field in (
        "title",
        "kind",
        "starts_at",
        "ends_at",
        "description",
        "location",
        "source",
        "source_ref",
        "course_id",
    ):
        if field in payload:
            kwargs[field] = payload[field]
    try:
        ev = current_app_events_service().update(event_id, **kwargs)
    except KeyError:
        return jsonify({"error": "not_found"}), 404
    except ValueError as exc:
        return jsonify(
            {
                "error": "invalid_payload",
                "detail": str(exc),
            }
        ), 400
    return jsonify({"event": _event_to_dict(ev)})


@bp.delete("/api/v1/events/<event_id>")
def api_delete_event(event_id: str) -> Any:
    deleted = current_app_events_service().delete(event_id)
    if not deleted:
        return jsonify({"error": "not_found"}), 404
    return ("", 204)


@bp.post("/api/v1/events/import")
def api_import_events_ics() -> Any:
    """Import events from a .ics file. Multipart field ``file``.

    Returns ``{"imported": N, "added", "updated", "summary"}``; a file
    that isn't a calendar is a 400 with a ``detail`` for the user."""
    cfg = current_app.config["AFTERWORD_CONFIG"]
    if "file" not in request.files:
        return jsonify(
            {
                "error": "missing_file",
                "detail": "multipart field 'file' is required",
            }
        ), 400
    upload = request.files["file"]
    raw = upload.read()
    if not raw:
        return jsonify({"error": "empty_file"}), 400
    if len(raw) > cfg.max_upload_bytes:
        return jsonify(
            {
                "error": "file_too_large",
                "detail": (f"file is {len(raw)} bytes; max is {cfg.max_upload_bytes}"),
            }
        ), 413
    # ``kind`` (form field): what the events are; meeting by default.
    # Importing the same file again updates instead of duplicating.
    from services.calendar_feeds import FeedError
    from web.common import current_app_subscriptions_service

    try:
        result = current_app_subscriptions_service().import_file(
            raw,
            (request.form.get("kind") or "meeting").strip(),
        )
    except (ValueError, FeedError) as exc:
        return jsonify(
            {
                "error": "invalid_payload",
                "detail": str(exc),
                "imported": 0,
            }
        ), 400
    return jsonify(
        {
            "imported": result.added + result.updated,
            "added": result.added,
            "updated": result.updated,
            "summary": result.summary(),
        }
    )
