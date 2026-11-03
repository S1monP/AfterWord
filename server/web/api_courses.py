"""Courses API."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

import storage
from web.common import current_app_courses_service

bp = Blueprint("courses_api", __name__)


# --- API: courses (CRUD + session assignment) ---------------------
def _course_to_dict(c: storage.Course) -> dict[str, Any]:
    return c.to_api_dict()


@bp.get("/api/v1/courses")
def api_list_courses() -> Any:
    """List courses. Query params: ``limit`` (1-500, default
    200), ``offset`` (>=0)."""
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
    courses, total = current_app_courses_service().list(
        limit=limit,
        offset=offset,
    )
    return jsonify(
        {
            "courses": [_course_to_dict(c) for c in courses],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@bp.post("/api/v1/courses")
def api_create_course() -> Any:
    """Create a course. Body: ``{name, code?, color?,
    sort_order?}``."""
    payload = request.get_json(silent=True) or {}
    try:
        course = current_app_courses_service().create(
            name=payload.get("name", ""),
            code=payload.get("code"),
            color=payload.get("color"),
            sort_order=payload.get("sort_order") or 0,
        )
    except ValueError as exc:
        return jsonify(
            {
                "error": "invalid_payload",
                "detail": str(exc),
            }
        ), 400
    return jsonify({"course": _course_to_dict(course)}), 201


@bp.get("/api/v1/courses/<course_id>")
def api_get_course(course_id: str) -> Any:
    course = current_app_courses_service().get(course_id)
    if course is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"course": _course_to_dict(course)})


@bp.patch("/api/v1/courses/<course_id>")
def api_update_course(course_id: str) -> Any:
    """Partial update. Body: any of ``{name, code, color,
    sort_order}``. ``code`` and ``color`` accept null to clear."""
    payload = request.get_json(silent=True) or {}
    kwargs: dict[str, Any] = {}
    for field in ("name", "code", "color", "sort_order"):
        if field in payload:
            kwargs[field] = payload[field]
    try:
        course = current_app_courses_service().update(
            course_id,
            **kwargs,
        )
    except KeyError:
        return jsonify({"error": "not_found"}), 404
    except ValueError as exc:
        return jsonify(
            {
                "error": "invalid_payload",
                "detail": str(exc),
            }
        ), 400
    return jsonify({"course": _course_to_dict(course)})


@bp.delete("/api/v1/courses/<course_id>")
def api_delete_course(course_id: str) -> Any:
    deleted = current_app_courses_service().delete(course_id)
    if not deleted:
        return jsonify({"error": "not_found"}), 404
    return ("", 204)
