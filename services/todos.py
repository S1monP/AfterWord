"""Todos service.

Tier 1: no due dates (per spec). Priorities are an enum-string.
Status is ``open`` | ``done`` | ``cancelled``. The user can transition
``open -> done`` and ``open -> cancelled`` only. Re-opening a done
todo is allowed (Tier 1 lets you un-check a checkbox), but cancelling
a done todo is not. The service is the gatekeeper for these rules.

The service is constructed with a :class:`storage.Storage` instance
so it can be tested without a Flask app.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Optional

import storage
from storage import Todo


# --- Limits -----------------------------------------------------------------

MAX_TITLE_LEN = 200
MAX_BODY_MD_LEN = 50_000  # 50 KB; smaller than notes because todos are short

PRIORITIES: frozenset[str] = frozenset({"low", "medium", "high", "urgent"})
STATUSES: frozenset[str] = frozenset({"open", "done", "cancelled"})

VALID_SOURCES = ("manual", "extracted", "imported")


# Allowed transitions for ``set_status``.
# - open -> done | cancelled (normal user actions)
# - done -> open (uncheck, allowed)
# - done -> cancelled (rare; mark a finished task as cancelled instead)
# - cancelled -> open (re-open; allowed)
# - cancelled -> done (allowed)
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"done", "cancelled"}),
    "done": frozenset({"open", "cancelled"}),
    "cancelled": frozenset({"open", "done"}),
}


# Reject control characters except common whitespace (\t, \n, \r).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _validate_title(title: object) -> str:
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    title = title.strip()
    if not title:
        raise ValueError("title is required")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(f"title must be at most {MAX_TITLE_LEN} chars")
    if _CONTROL_CHARS.search(title):
        raise ValueError("title contains control characters")
    return title


def _validate_body(body_md: object) -> Optional[str]:
    if body_md is None or body_md == "":
        return None
    if not isinstance(body_md, str):
        raise ValueError("body_md must be a string")
    if len(body_md) > MAX_BODY_MD_LEN:
        raise ValueError(f"body_md must be at most {MAX_BODY_MD_LEN} chars")
    if _CONTROL_CHARS.search(body_md):
        raise ValueError("body_md contains control characters")
    return body_md


def _validate_priority(priority: object) -> str:
    if priority is None:
        return "medium"
    if not isinstance(priority, str):
        raise ValueError("priority must be a string")
    if priority not in PRIORITIES:
        raise ValueError(
            f"priority must be one of {sorted(PRIORITIES)}; got {priority!r}"
        )
    return priority


def _validate_status(status: object) -> str:
    if status is None:
        return "open"
    if not isinstance(status, str):
        raise ValueError("status must be a string")
    if status not in STATUSES:
        raise ValueError(
            f"status must be one of {sorted(STATUSES)}; got {status!r}"
        )
    return status


def _validate_source(source: object) -> str:
    if source is None:
        return "manual"
    if not isinstance(source, str):
        raise ValueError("source must be a string")
    if source not in VALID_SOURCES:
        raise ValueError(
            f"source must be one of {VALID_SOURCES}; got {source!r}"
        )
    return source


def _validate_source_ref(source_ref: object) -> Optional[str]:
    if source_ref is None or source_ref == "":
        return None
    if not isinstance(source_ref, str):
        raise ValueError("source_ref must be a string")
    if len(source_ref) > 256:
        raise ValueError("source_ref must be at most 256 chars")
    return source_ref


# --- Age buckets ------------------------------------------------------------


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse the storage's ISO-8601 timestamps. Returns None on failure."""
    try:
        # The storage uses ``%Y-%m-%dT%H:%M:%SZ`` (UTC).
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def age_bucket(created_at: str, now_iso: str) -> str:
    """Classify a todo's age for the UI.

    Returns one of:

    * ``'new'``  -- under 1 day old
    * ``'aging'`` -- 1 to 7 days old
    * ``'stale'`` -- over 7 days old
    * ``'unknown'`` -- if the timestamps can't be parsed

    Pure function so the templates can render the badge without the
    service doing a query.
    """
    created = _parse_iso(created_at)
    now = _parse_iso(now_iso)
    if created is None or now is None:
        return "unknown"
    delta = now - created
    if delta.days < 0:
        return "new"  # clock skew shouldn't surprise the UI
    if delta.days < 1:
        return "new"
    if delta.days < 7:
        return "aging"
    return "stale"


# --- Service ----------------------------------------------------------------


class TodosService:
    """Coordinates validation, transition rules, and storage for todos."""

    def __init__(self, store: storage.Storage) -> None:
        self._store = store

    def create(
        self,
        *,
        title: str,
        body_md: Optional[str] = None,
        priority: str = "medium",
        source: str = "manual",
        source_ref: Optional[str] = None,
    ) -> Todo:
        title = _validate_title(title)
        body_md = _validate_body(body_md)
        priority = _validate_priority(priority)
        source = _validate_source(source)
        source_ref = _validate_source_ref(source_ref)
        todo_id = uuid.uuid4().hex
        return self._store.create_todo(
            todo_id,
            title=title,
            body_md=body_md,
            priority=priority,
            status="open",
            source=source,
            source_ref=source_ref,
        )

    def update(
        self,
        todo_id: str,
        *,
        title: Optional[str] = None,
        body_md: Any = storage._UNSET,
        priority: Optional[str] = None,
    ) -> Todo:
        if not isinstance(todo_id, str) or not todo_id:
            raise ValueError("todo_id is required")
        if self._store.get_todo(todo_id) is None:
            raise KeyError(todo_id)
        new_title: Optional[str] = None
        new_body: Any = storage._UNSET
        new_priority: Optional[str] = None
        if title is not None:
            new_title = _validate_title(title)
        if body_md is not storage._UNSET:
            # ``None`` is a valid value here (clears the column);
            # otherwise validate as a normal string.
            new_body = _validate_body(body_md)
        if priority is not None:
            new_priority = _validate_priority(priority)
        return self._store.update_todo(
            todo_id,
            title=new_title,
            body_md=new_body,
            priority=new_priority,
        )

    def set_status(self, todo_id: str, status: str) -> Todo:
        """Transition a todo to a new status.

        Enforces :data:`ALLOWED_TRANSITIONS`. Sets ``completed_at`` to
        the current time when transitioning to ``done`` from any other
        state, and clears it when transitioning from ``done`` to
        anything else.
        """
        if not isinstance(todo_id, str) or not todo_id:
            raise ValueError("todo_id is required")
        status = _validate_status(status)
        existing = self._store.get_todo(todo_id)
        if existing is None:
            raise KeyError(todo_id)
        if status == existing.status:
            # No-op: same status, just return the row.
            return existing
        if status not in ALLOWED_TRANSITIONS[existing.status]:
            raise ValueError(
                f"illegal transition {existing.status!r} -> {status!r}; "
                f"allowed from {existing.status!r}: "
                f"{sorted(ALLOWED_TRANSITIONS[existing.status])}"
            )
        completed_at: Optional[str]
        if status == "done" and existing.completed_at is None:
            completed_at = storage.now_iso()
        elif existing.status == "done" and status != "done":
            completed_at = None
        else:
            completed_at = existing.completed_at
        return self._store.update_todo(
            todo_id, status=status, completed_at=completed_at,
        )

    def delete(self, todo_id: str) -> bool:
        if not isinstance(todo_id, str) or not todo_id:
            raise ValueError("todo_id is required")
        return self._store.delete_todo(todo_id)

    def get(self, todo_id: str) -> Optional[Todo]:
        if not isinstance(todo_id, str) or not todo_id:
            return None
        return self._store.get_todo(todo_id)

    def list(
        self,
        *,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Todo], int]:
        if status is not None:
            status = _validate_status(status)
        if priority is not None:
            priority = _validate_priority(priority)
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive int")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative int")
        return self._store.list_todos(
            status=status, priority=priority, limit=limit, offset=offset,
        )