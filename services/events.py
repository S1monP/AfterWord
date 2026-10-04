"""Events service: calendar (lectures + meetings).

Events are user-owned, not extracted by the LLM. The LLM matches
sessions to events based on time proximity (10 minutes before
``starts_at`` through ``ends_at``).

Tier 1: stub. All public methods raise :class:`NotImplementedError`,
except :meth:`suggest_match_for_session` which is a pure function and
implemented now so the data model is testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


KINDS: frozenset[str] = frozenset({"lecture", "meeting", "other"})


@dataclass
class Event:
    id: str
    title: str
    description: Optional[str]
    kind: str
    starts_at: str
    ends_at: Optional[str]
    location: Optional[str]
    source: str               # 'manual' | 'ical_import'
    source_ref: Optional[str]
    matched_session_id: Optional[str]
    match_confidence: Optional[float]
    created_at: str
    updated_at: str


def suggest_match_confidence(
    session_started_at: str,
    event_starts_at: str,
    event_ends_at: Optional[str],
) -> Optional[float]:
    """Compute a 0..1 confidence score for whether a session belongs
    to an event.

    Returns ``None`` if the session started outside the
    [event_starts_at - 10 min, event_ends_at] window. Otherwise returns
    1.0 if the session started at exactly event_starts_at, decaying
    linearly to 0.5 at the window edges.

    Pure function — no DB, no clock side-effects, takes ISO-8601
    strings so the caller decides the comparison time.
    """
    raise NotImplementedError


class EventsService:
    def create(self, *, title: str, kind: str, starts_at: str,
               description: Optional[str] = None,
               ends_at: Optional[str] = None,
               location: Optional[str] = None,
               source: str = "manual",
               source_ref: Optional[str] = None) -> Event:
        raise NotImplementedError

    def update(self, event_id: str, **fields) -> Event:
        raise NotImplementedError

    def delete(self, event_id: str) -> None:
        raise NotImplementedError

    def get(self, event_id: str) -> Optional[Event]:
        raise NotImplementedError

    def list(self, *, kind: Optional[str] = None,
             starts_after: Optional[str] = None,
             starts_before: Optional[str] = None) -> list[Event]:
        raise NotImplementedError

    def suggest_match(self, session_id: str) -> Optional[Event]:
        raise NotImplementedError

    def match_session(self, event_id: str, session_id: str,
                      confidence: float) -> None:
        raise NotImplementedError

    def import_ics(self, ics_bytes: bytes) -> int:
        """Parse an .ics file and create events. Returns count imported."""
        raise NotImplementedError
