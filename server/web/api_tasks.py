"""Tasks and deadlines API.
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

import storage
from web.common import current_app_tasks_service

bp = Blueprint("tasks_api", __name__)


# --- API: tasks (CRUD + status transitions) --------------------------
def _task_to_dict(t: storage.Task) -> dict[str, Any]:
    return t.to_api_dict()


@bp.get("/api/v1/tasks")
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


@bp.post("/api/v1/tasks")
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


@bp.get("/api/v1/tasks/<task_id>")
def api_get_task(task_id: str) -> Any:
    task = current_app_tasks_service().get(task_id)
    if task is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"task": _task_to_dict(task)})


@bp.patch("/api/v1/tasks/<task_id>")
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


@bp.post("/api/v1/tasks/<task_id>/status")
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


@bp.delete("/api/v1/tasks/<task_id>")
def api_delete_task(task_id: str) -> Any:
    deleted = current_app_tasks_service().delete(task_id)
    if not deleted:
        return jsonify({"error": "not_found"}), 404
    return ("", 204)
