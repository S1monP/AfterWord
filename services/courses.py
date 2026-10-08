"""Courses service: the user-owned organisational unit behind
recordings.

A course is something like "Biology EP3456" — a class the user
takes. Lectures are tagged with a ``course_id``; deleting a course
unlinks its sessions (the schema's ``ON DELETE SET NULL``).

The service layer owns three concerns the storage layer doesn't:

* Input validation (``name`` required, ``color`` is a hex string,
  ``sort_order`` is an int).
* Generating ids (``uuid4().hex``) so callers don't have to.
* A small ``color_hex_re`` guard so a bad value is rejected
  early, not silently stored.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

import storage
from storage import Course, Storage


# --- Limits -----------------------------------------------------------------

MAX_NAME_LEN = 200
MAX_CODE_LEN = 50
MAX_COLOR_LEN = 16  # "#rrggbb" plus a little slack


# Match "#RGB" or "#RRGGBB" (3 or 6 hex digits), with the leading "#".
_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# --- Validators -------------------------------------------------------------

def _validate_name(name: object) -> str:
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"name must be at most {MAX_NAME_LEN} chars")
    return name


def _validate_code(code: object) -> Optional[str]:
    if code is None or code == "":
        return None
    if not isinstance(code, str):
        raise ValueError("code must be a string")
    if len(code) > MAX_CODE_LEN:
        raise ValueError(f"code must be at most {MAX_CODE_LEN} chars")
    return code


def _validate_color(color: object) -> Optional[str]:
    if color is None or color == "":
        return None
    if not isinstance(color, str):
        raise ValueError("color must be a string")
    if not _COLOR_RE.match(color):
        raise ValueError(
            f"color must be a CSS hex string like '#a3c4f3' "
            f"(3 or 6 hex digits); got {color!r}"
        )
    # Normalise to lowercase.
    return color.lower()


def _validate_sort_order(sort_order: object) -> int:
    if sort_order is None:
        return 0
    if isinstance(sort_order, bool):
        # bool is a subclass of int; reject it explicitly.
        raise ValueError("sort_order must be an integer")
    if isinstance(sort_order, int):
        if sort_order < 0:
            raise ValueError("sort_order must be >= 0")
        return sort_order
    if isinstance(sort_order, str):
        try:
            value = int(sort_order)
        except ValueError as exc:
            raise ValueError(
                f"sort_order must be an integer; got {sort_order!r}"
            ) from exc
        if value < 0:
            raise ValueError("sort_order must be >= 0")
        return value
    raise ValueError(
        f"sort_order must be an integer; got {type(sort_order).__name__}"
    )


# --- Service ----------------------------------------------------------------

class CoursesService:
    """Coordinates validation, id generation, and storage for
    courses."""

    def __init__(self, store: Storage) -> None:
        self._store = store

    def create(
        self,
        *,
        name: str,
        code: Optional[str] = None,
        color: Optional[str] = None,
        sort_order: int = 0,
    ) -> Course:
        name = _validate_name(name)
        code = _validate_code(code)
        color = _validate_color(color)
        sort_order = _validate_sort_order(sort_order)
        course_id = uuid.uuid4().hex
        return self._store.create_course(
            course_id,
            name=name, code=code, color=color, sort_order=sort_order,
        )

    def update(self, course_id: str, **fields: object) -> Course:
        if not isinstance(course_id, str) or not course_id:
            raise ValueError("course_id is required")
        if self._store.get_course(course_id) is None:
            raise KeyError(course_id)
        cleaned: dict[str, object] = {}
        if "name" in fields:
            cleaned["name"] = _validate_name(fields["name"])
        if "code" in fields:
            cleaned["code"] = _validate_code(fields["code"])
        if "color" in fields:
            cleaned["color"] = _validate_color(fields["color"])
        if "sort_order" in fields:
            cleaned["sort_order"] = _validate_sort_order(
                fields["sort_order"]
            )
        if not cleaned:
            existing = self._store.get_course(course_id)
            assert existing is not None
            return existing
        return self._store.update_course(course_id, **cleaned)

    def delete(self, course_id: str) -> bool:
        if not isinstance(course_id, str) or not course_id:
            raise ValueError("course_id is required")
        return self._store.delete_course(course_id)

    def get(self, course_id: str) -> Optional[Course]:
        if not isinstance(course_id, str) or not course_id:
            return None
        return self._store.get_course(course_id)

    def list(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[Course], int]:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive int")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative int")
        return self._store.list_courses(limit=limit, offset=offset)


__all__ = ["CoursesService"]