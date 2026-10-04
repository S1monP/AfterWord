"""Deadlines service.

Each deadline has a hard due date and time. Status is 'set' or
'complete'. The 'overdue' state is derived: ``set and due_at < now``.

Re-opening a completed deadline is disabled in Tier 1 (the route
handler refuses). The :meth:`reopen` method exists as a stub so the
future API surface is unambiguous, but it raises if called.

Tier 1: stub. All public methods raise :class:`NotImplementedError`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


STATUSES: frozenset[str] = frozenset({"set", "complete"})


@dataclass
class Deadline:
    id: str
    title: str
    description: Optional[str]
    due_at: str                              # ISO-8601 with time
    status: str                             # 'set' | 'complete'
    source: str
    source_ref: Optional[str]
    source_offset_ms: Optional[int]
    created_at: str
    updated_at: str
    completed_at: Optional[str]


def is_overdue(deadline: Deadline, now_iso: str) -> bool:
    """Pure function: is this deadline currently overdue?"""
    raise NotImplementedError


class DeadlinesService:
    def create(self, *, title: str, due_at: str,
               description: Optional[str] = None,
               source: str = "manual",
               source_ref: Optional[str] = None,
               source_offset_ms: Optional[int] = None) -> Deadline:
        raise NotImplementedError

    def update(self, deadline_id: str, **fields) -> Deadline:
        raise NotImplementedError

    def mark_complete(self, deadline_id: str) -> Deadline:
        raise NotImplementedError

    def reopen(self, deadline_id: str) -> Deadline:
        """Re-opening is disabled in Tier 1; the route handler refuses.

        The method exists so the API surface is stable.
        """
        raise NotImplementedError

    def delete(self, deadline_id: str) -> None:
        raise NotImplementedError

    def get(self, deadline_id: str) -> Optional[Deadline]:
        raise NotImplementedError

    def list(self, *, status: Optional[str] = None,
             include_overdue: bool = True) -> list[Deadline]:
        raise NotImplementedError
