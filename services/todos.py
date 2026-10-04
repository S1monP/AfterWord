"""Todos service.

No due dates (per the Tier 1 spec). Priorities are an enum-string.
Status is 'open' | 'done' | 'cancelled'. The user can only transition
``open -> done`` and ``open -> cancelled`` — re-opening is not allowed
(Tier 1.5 nicety, intentionally not implemented).

Tier 1: stub. All public methods raise :class:`NotImplementedError`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


PRIORITIES: frozenset[str] = frozenset({"low", "medium", "high", "urgent"})
STATUSES: frozenset[str] = frozenset({"open", "done", "cancelled"})


@dataclass
class Todo:
    id: str
    title: str
    body_md: Optional[str]
    priority: str            # 'low' | 'medium' | 'high' | 'urgent'
    status: str              # 'open' | 'done' | 'cancelled'
    source: str
    source_ref: Optional[str]
    created_at: str
    updated_at: str
    completed_at: Optional[str]


def age_bucket(created_at: str, now_iso: str) -> str:
    """Classify a todo's age for the UI.

    Returns one of 'new' (< 1 day), 'aging' (1-7 days), 'stale' (> 7 days).
    Pure function so the templates can render the badge without the
    service doing a query.
    """
    raise NotImplementedError


class TodosService:
    def create(self, *, title: str, body_md: Optional[str] = None,
               priority: str = "medium",
               source: str = "manual",
               source_ref: Optional[str] = None) -> Todo:
        raise NotImplementedError

    def update(self, todo_id: str, **fields) -> Todo:
        raise NotImplementedError

    def set_status(self, todo_id: str, status: str) -> Todo:
        """Transition a todo to a new status.

        Allowed transitions: open->done, open->cancelled. Re-opening
        is not allowed in Tier 1; the route handler enforces this.
        """
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        raise NotImplementedError

    def delete(self, todo_id: str) -> None:
        raise NotImplementedError

    def get(self, todo_id: str) -> Optional[Todo]:
        raise NotImplementedError

    def list(self, *, status: Optional[str] = None,
             priority: Optional[str] = None) -> list[Todo]:
        raise NotImplementedError
