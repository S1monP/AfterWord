"""Calendar subscriptions (services/calendar_feeds.py).

The link itself is a secret (it lets anyone read the timetable), so
responses show only ``url_display``.
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

import storage
from services.calendar_feeds import FeedError, display_url
from web.common import current_app_courses_service, current_app_subscriptions_service

bp = Blueprint("subscriptions_api", __name__)


def sub_dict(sub: storage.Subscription) -> dict[str, Any]:
    return {
        "id": sub.id, "name": sub.name, "url_display": display_url(sub.url),
        "kind": sub.kind, "course_id": sub.course_id,
        "last_synced_at": sub.last_synced_at, "last_attempt_at": sub.last_attempt_at,
        "last_error": sub.last_error, "event_count": sub.event_count,
        "hidden": len(sub.hidden_keys),
    }


def _bad(exc: Exception, status: int = 400):
    return jsonify({"error": "invalid_payload", "detail": str(exc)}), status


@bp.get("/api/v1/subscriptions")
def api_list_subscriptions() -> Any:
    return jsonify({"subscriptions": [sub_dict(s) for s in current_app_subscriptions_service().list()]})


@bp.post("/api/v1/subscriptions")
def api_add_subscription() -> Any:
    """Body ``{url, name?, kind, course_id?}``. The link is fetched
    first; a link that doesn't return a calendar is not saved."""
    body = request.get_json(silent=True) or {}
    try:
        sub, result = current_app_subscriptions_service().add(
            str(body.get("url") or ""), name=str(body.get("name") or ""),
            kind=str(body.get("kind") or "lecture"), course_id=body.get("course_id") or None,
        )
    except (ValueError, FeedError) as exc:
        return _bad(exc)
    return jsonify({"subscription": sub_dict(sub), "result": vars(result), "summary": result.summary()}), 201


@bp.patch("/api/v1/subscriptions/<sub_id>")
def api_update_subscription(sub_id: str) -> Any:
    """Body: any of ``{name, kind, course_id}`` (``course_id: null`` =
    match each event to a course by name). A new kind or course applies
    to all its events."""
    body = request.get_json(silent=True) or {}
    kwargs: dict[str, Any] = {k: body[k] for k in ("name", "kind") if k in body}
    if "course_id" in body:
        kwargs["course_id"] = body["course_id"] or None
    try:
        sub = current_app_subscriptions_service().update(sub_id, **kwargs)
    except KeyError:
        return jsonify({"error": "not_found"}), 404
    except ValueError as exc:
        return _bad(exc)
    return jsonify({"subscription": sub_dict(sub)})


@bp.post("/api/v1/subscriptions/<sub_id>/sync")
def api_sync_subscription(sub_id: str) -> Any:
    svc = current_app_subscriptions_service()
    try:
        result = svc.sync(sub_id)
    except KeyError:
        return jsonify({"error": "not_found"}), 404
    except FeedError as exc:
        # Nothing changed; the page shows the error on the subscription.
        return jsonify({"error": "sync_failed", "detail": str(exc),
                        "subscription": sub_dict(svc.get(sub_id))}), 502
    return jsonify({"subscription": sub_dict(svc.get(sub_id)), "result": vars(result), "summary": result.summary()})


@bp.delete("/api/v1/subscriptions/<sub_id>")
def api_delete_subscription(sub_id: str) -> Any:
    if not current_app_subscriptions_service().delete(sub_id):
        return jsonify({"error": "not_found"}), 404
    return ("", 204)


@bp.post("/api/v1/subscriptions/<sub_id>/courses")
def api_subscription_courses(sub_id: str) -> Any:
    """Create a course for each of its modules that has none, and file
    its events under them."""
    svc = current_app_subscriptions_service()
    if svc.get(sub_id) is None:
        return jsonify({"error": "not_found"}), 404
    made = svc.create_courses(sub_id, current_app_courses_service())
    return jsonify({"created": made})
