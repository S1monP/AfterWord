"""Tasks service: the merged todos + deadlines.

A row is either a personal todo (``kind = 'task'``) or a course
deadline (``kind = 'deadline'``). The shape is the same; the
differences are:

* ``kind='task'``: ``due_at`` is optional. No course linkage. Pinned
  flag is meaningful.
* ``kind='deadline'``: ``due_at`` is required. Optional
  ``course_id`` and ``source_offset_ms`` for the recording deep-link.

The service is constructed with a :class:`storage.Storage` instance
so it can be tested without a Flask app.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid
from typing import Any, Optional

import storage
import timeutil
from storage import Task


# --- Limits -----------------------------------------------------------------

MAX_TITLE_LEN = 200
MAX_BODY_MD_LEN = 200_000   # matches notes
MAX_DESCRIPTION_LEN = 4_000

PRIORITIES: frozenset[str] = frozenset({"low", "medium", "high", "urgent"})
STATUSES: frozenset[str] = frozenset({"open", "done", "cancelled"})
KINDS: frozenset[str] = frozenset({"task", "deadline"})

VALID_SOURCES = ("manual", "extracted", "imported")


# Allowed status transitions. Same for both kinds. A done task can
# be re-opened (un-checked), and a cancelled task can be re-opened
# or marked done.
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


def _validate_description(description: object) -> Optional[str]:
    if description is None or description == "":
        return None
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    if len(description) > MAX_DESCRIPTION_LEN:
        raise ValueError(
            f"description must be at most {MAX_DESCRIPTION_LEN} chars"
        )
    return description


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


def _validate_kind(kind: object) -> str:
    if kind is None:
        return "task"
    if not isinstance(kind, str):
        raise ValueError("kind must be a string")
    if kind not in KINDS:
        raise ValueError(
            f"kind must be one of {sorted(KINDS)}; got {kind!r}"
        )
    return kind


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


def _validate_course_id(course_id: object) -> Optional[str]:
    if course_id is None or course_id == "":
        return None
    if not isinstance(course_id, str):
        raise ValueError("course_id must be a string")
    if len(course_id) > 64:
        raise ValueError("course_id must be at most 64 chars")
    return course_id


def _validate_source_offset_ms(offset: object) -> Optional[int]:
    if offset is None:
        return None
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError("source_offset_ms must be an integer or null")
    if offset < 0:
        raise ValueError("source_offset_ms must be >= 0")
    return int(offset)


def _validate_due_at(due_at: object) -> Optional[str]:
    """Parse and validate a due_at timestamp.

    Accepts anything ``datetime.fromisoformat`` can parse (in Python
    3.11+ that includes most ISO-8601 strings). Empty string and
    ``None`` both mean "no due date". Returns the canonical
    ``YYYY-MM-DDTHH:MM:SSZ`` form so the storage always sees a
    consistent shape.

    A value without an offset (what a ``date`` input sends) is local
    time, see ``timeutil``; a date alone is local midnight.
    """
    if due_at is None or due_at == "":
        return None
    if not isinstance(due_at, str):
        raise ValueError("due_at must be a string")
    if len(due_at) > 64:
        raise ValueError("due_at is too long")
    try:
        return timeutil.to_utc_iso(due_at)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"due_at is not a valid ISO-8601 timestamp: {due_at!r}"
        ) from exc


def is_overdue(task_due_at: Optional[str], now_iso: str) -> bool:
    """True iff the task is past due at ``now_iso``.

    A due time of local midnight is a due *date* (that is what the
    date picker sends): the task is overdue once that local day is
    over, not from its first minute. Any other time is a deadline
    that passes at that instant.
    """
    if not task_due_at:
        return False
    due = timeutil.to_local(task_due_at)
    now = timeutil.to_local(now_iso)
    if due is None or now is None:
        return task_due_at < now_iso
    if due.time() == _dt.time(0, 0):
        return due.date() < now.date()
    return due < now


def is_due_today(task_due_at: Optional[str], now_iso: str) -> bool:
    """True iff the task is due on the same local date as now."""
    if not task_due_at:
        return False
    due = timeutil.to_local(task_due_at)
    now = timeutil.to_local(now_iso)
    if due is None or now is None:
        return task_due_at[:10] == now_iso[:10]
    return due.date() == now.date()


# --- Service ----------------------------------------------------------------


class TasksService:
    """Coordinates validation, transition rules, and storage for tasks."""

    def __init__(self, store: storage.Storage) -> None:
        self._store = store

    def create(
        self,
        *,
        title: str,
        body_md: Optional[str] = None,
        description: Optional[str] = None,
        priority: str = "medium",
        status: str = "open",
        kind: str = "task",
        due_at: Optional[str] = None,
        source: str = "manual",
        source_ref: Optional[str] = None,
        source_offset_ms: Optional[int] = None,
        course_id: Optional[str] = None,
        pinned: bool = False,
    ) -> Task:
        title = _validate_title(title)
        body_md = _validate_body(body_md)
        description = _validate_description(description)
        priority = _validate_priority(priority)
        status = _validate_status(status)
        kind = _validate_kind(kind)
        source = _validate_source(source)
        source_ref = _validate_source_ref(source_ref)
        source_offset_ms = _validate_source_offset_ms(source_offset_ms)
        course_id = _validate_course_id(course_id)
        due_at = _validate_due_at(due_at)

        # Deadlines require a due date. Personal tasks can be date-less.
        if kind == "deadline" and due_at is None:
            raise ValueError(
                "deadlines require a due_at; pass an ISO-8601 timestamp"
            )

        task_id = uuid.uuid4().hex
        return self._store.create_task(
            task_id,
            title=title,
            body_md=body_md,
            description=description,
            priority=priority,
            status=status,
            kind=kind,
            due_at=due_at,
            source=source,
            source_ref=source_ref,
            source_offset_ms=source_offset_ms,
            course_id=course_id,
            pinned=pinned,
        )

    def update(
        self,
        task_id: str,
        *,
        title: Optional[str] = None,
        body_md: Any = storage._UNSET,
        description: Any = storage._UNSET,
        priority: Optional[str] = None,
        kind: Optional[str] = None,
        due_at: Any = storage._UNSET,
        source_offset_ms: Any = storage._UNSET,
        course_id: Any = storage._UNSET,
        pinned: Any = storage._UNSET,
    ) -> Task:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id is required")
        existing = self._store.get_task(task_id)
        if existing is None:
            raise KeyError(task_id)
        new_title: Optional[str] = None
        new_body: Any = storage._UNSET
        new_description: Any = storage._UNSET
        new_priority: Optional[str] = None
        new_kind: Optional[str] = None
        new_due_at: Any = storage._UNSET
        new_source_offset_ms: Any = storage._UNSET
        new_course_id: Any = storage._UNSET
        new_pinned: Any = storage._UNSET
        if title is not None:
            new_title = _validate_title(title)
        if body_md is not storage._UNSET:
            new_body = _validate_body(body_md)
        if description is not storage._UNSET:
            new_description = _validate_description(description)
        if priority is not None:
            new_priority = _validate_priority(priority)
        if kind is not None:
            new_kind = _validate_kind(kind)
        if due_at is not storage._UNSET:
            new_due_at = _validate_due_at(due_at)
        if source_offset_ms is not storage._UNSET:
            new_source_offset_ms = _validate_source_offset_ms(source_offset_ms)
        if course_id is not storage._UNSET:
            new_course_id = _validate_course_id(course_id)
        if pinned is not storage._UNSET:
            new_pinned = bool(pinned)
        # If the resulting kind is 'deadline', due_at must be set.
        # (Only validate when the user actually changed kind or due_at;
        # otherwise trust the existing row.)
        resulting_kind = new_kind if new_kind is not None else existing.kind
        resulting_due_at = (
            new_due_at if new_due_at is not storage._UNSET
            else existing.due_at
        )
        if resulting_kind == "deadline" and resulting_due_at is None:
            raise ValueError(
                "deadlines require a due_at; pass an ISO-8601 timestamp"
            )
        return self._store.update_task(
            task_id,
            title=new_title,
            body_md=new_body,
            description=new_description,
            priority=new_priority,
            kind=new_kind,
            due_at=new_due_at,
            source_offset_ms=new_source_offset_ms,
            course_id=new_course_id,
            pinned=new_pinned,
        )

    def set_status(self, task_id: str, status: str) -> Task:
        """Transition a task to a new status.

        Enforces :data:`ALLOWED_TRANSITIONS`. Sets ``completed_at``
        to the current time when transitioning to ``done`` from any
        other state, and clears it when transitioning from ``done``
        to anything else.
        """
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id is required")
        status = _validate_status(status)
        existing = self._store.get_task(task_id)
        if existing is None:
            raise KeyError(task_id)
        if status == existing.status:
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
        return self._store.update_task(
            task_id, status=status, completed_at=completed_at,
        )

    def delete(self, task_id: str) -> bool:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id is required")
        return self._store.delete_task(task_id)

    def get(self, task_id: str) -> Optional[Task]:
        if not isinstance(task_id, str) or not task_id:
            return None
        return self._store.get_task(task_id)

    def list(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Task], int]:
        if kind is not None:
            kind = _validate_kind(kind)
        if status is not None:
            status = _validate_status(status)
        if priority is not None:
            priority = _validate_priority(priority)
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive int")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative int")
        return self._store.list_tasks(
            kind=kind, status=status, priority=priority,
            limit=limit, offset=offset,
        )