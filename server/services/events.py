"""Events service: calendar (lectures + meetings).

Events are user-owned, not extracted by the LLM. The LLM matches
sessions to events based on time proximity (10 minutes before
``starts_at`` through ``ends_at``).

The service layer owns three concerns the storage layer doesn't:

* Input validation (title, kind, source, ISO timestamp normalisation,
  ``ends_at`` ordering, control-character rejection).
* The pure ``suggest_match_confidence`` function, exposed both as a
  free function and used by :meth:`EventsService.suggest_match`.
* Parsing .ics files into a stream of ``Event`` rows, used by
  :meth:`EventsService.import_ics`.

The service is constructed with a :class:`storage.Storage` instance
so it can be tested without a Flask app. Routes get the service via
``app.current_app_events_service``.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid
from typing import Any

import timeutil
from storage import Event, Storage

# --- Limits -----------------------------------------------------------------

MAX_TITLE_LEN = 200
MAX_DESCRIPTION_LEN = 2000
MAX_LOCATION_LEN = 200
MAX_SOURCE_REF_LEN = 256

# Safety cap for weekly-recurring event series: creating more than a
# year of weekly occurrences in one go is almost certainly a typo
# (e.g. "repeat until 2101-01-01").
MAX_REPEAT_OCCURRENCES = 52

KINDS: frozenset[str] = frozenset({"lecture", "meeting", "other"})
# 'extracted': filed from a recording (services/filing.py).
SOURCES: frozenset[str] = frozenset({"manual", "ical_import", "subscription", "extracted"})

# Match window for session→event matching. A session that started up
# to 10 minutes before ``event_starts_at`` is still considered a
# plausible match (the user might have pressed record slightly early).
MATCH_PRE_WINDOW = _dt.timedelta(minutes=10)


# Reject control characters except common whitespace (\t, \n, \r).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# --- ISO timestamp helpers --------------------------------------------------

# Accept anything that ``datetime.fromisoformat`` can parse, then
# normalise to a UTC ISO-8601 string with a trailing ``Z``. Values
# without an offset are local time (``timeutil``; Europe/London):
#
#   "2027-03-15"                  -> local midnight, in UTC (date-only)
#   "2027-07-15T14:30"            -> "2027-07-15T13:30:00Z" (naive, BST)
#   "2027-03-15T14:30:00Z"        -> pass-through
#   "2027-03-15T14:30:00+01:00"   -> converted to UTC then Z
#
# The :class:`datetime` module's ``fromisoformat`` is permissive enough
# in Python 3.11+ to accept all of these without a third-party parser.

def _normalise_iso(value: object, *, field: str) -> str:
    """Validate and normalise a user-supplied timestamp string.

    Raises :class:`ValueError` with a clear field name if the input
    is not a parseable ISO-8601 date or datetime.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty ISO-8601 string")
    try:
        return timeutil.to_utc_iso(value)
    except ValueError as exc:
        raise ValueError(
            f"{field} must be ISO-8601 (e.g. '2027-03-15' or "
            f"'2027-03-15T14:30:00Z'); got {value!r}"
        ) from exc


def _parse_iso(ts: str) -> _dt.datetime:
    """Parse an ISO-8601 string into an aware UTC datetime (naive =
    local time)."""
    return timeutil.parse(ts)


def _fmt_iso(dt: _dt.datetime) -> str:
    """Format an aware datetime as the storage-normalised ISO string."""
    return dt.astimezone(_dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _validate_repeat_until(value: object) -> str | None:
    """Validate the optional weekly-recurrence end date (inclusive).

    Accepts ``YYYY-MM-DD``; returns a normalised ISO date string or
    ``None`` when no recurrence was requested.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("repeat_until must be a date string")
    try:
        d = _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "repeat_until must be an ISO date (YYYY-MM-DD)"
        ) from exc
    return d.isoformat()


# --- Validators -------------------------------------------------------------

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


def _validate_kind(kind: object) -> str:
    if not isinstance(kind, str):
        raise ValueError("kind must be a string")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}; got {kind!r}")
    return kind


def _validate_source(source: object) -> str:
    if source is None:
        return "manual"
    if not isinstance(source, str):
        raise ValueError("source must be a string")
    if source not in SOURCES:
        raise ValueError(
            f"source must be one of {sorted(SOURCES)}; got {source!r}"
        )
    return source


def _validate_optional_text(
    value: object, *, field: str, max_len: int,
) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if len(value) > max_len:
        raise ValueError(f"{field} must be at most {max_len} chars")
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"{field} contains control characters")
    return value


def _validate_source_ref(source_ref: object) -> str | None:
    return _validate_optional_text(
        source_ref, field="source_ref", max_len=MAX_SOURCE_REF_LEN,
    )


def _validate_course_id(course_id: object, store: Storage) -> str | None:
    """Validate an event's optional course association: ``None`` or
    an existing course id. Used by the calendar time-matcher, which
    assigns the matched event's course to the recording."""
    if course_id is None or course_id == "":
        return None
    if not isinstance(course_id, str):
        raise ValueError("course_id must be a string or null")
    if store.get_course(course_id) is None:
        raise ValueError(f"course {course_id!r} does not exist")
    return course_id


def _validate_starts_at(value: object) -> str:
    return _normalise_iso(value, field="starts_at")


def _validate_ends_at(
    value: object, *, starts_at: str,
) -> str | None:
    if value is None or value == "":
        return None
    normalised = _normalise_iso(value, field="ends_at")
    if normalised <= starts_at:
        raise ValueError("ends_at must be strictly after starts_at")
    return normalised


# --- Match confidence math --------------------------------------------------

def suggest_match_confidence(
    session_started_at: str,
    event_starts_at: str,
    event_ends_at: str | None,
) -> float | None:
    """Compute a 0..1 confidence score for whether a session belongs
    to an event.

    Returns ``None`` if the session started outside the
    ``[event_starts_at - 10 min, event_ends_at]`` window. Otherwise
    returns ``1.0`` if the session started at exactly
    ``event_starts_at``, decaying linearly to ``0.5`` at the window
    edges. When ``event_ends_at`` is ``None``, the window's right
    edge is one hour past ``event_starts_at`` (a sensible default for
    events without a declared duration).

    Pure function — no DB, no clock side-effects, takes ISO-8601
    strings so the caller decides the comparison time.
    """
    try:
        session_dt = _parse_iso(session_started_at)
        start_dt = _parse_iso(event_starts_at)
        if event_ends_at is not None:
            end_dt = _parse_iso(event_ends_at)
        else:
            end_dt = start_dt + _dt.timedelta(hours=1)
    except ValueError:
        return None

    window_start = start_dt - MATCH_PRE_WINDOW
    if session_dt < window_start or session_dt > end_dt:
        return None

    # Tent shape: peak at event_starts_at (1.0), falling linearly to
    # 0.5 at both edges (window_start on the left, end_dt on the
    # right). This matches the docstring.
    left_span = (start_dt - window_start).total_seconds()
    right_span = (end_dt - start_dt).total_seconds()
    if session_dt <= start_dt:
        if left_span <= 0:
            # Degenerate: event_start == window_start. Anything inside
            # the pre-window scores 0.7; outside scores None.
            return 0.7 if session_dt >= window_start else None
        pos = (session_dt - window_start).total_seconds() / left_span
        pos = max(0.0, min(1.0, pos))
        return 0.5 + 0.5 * pos
    else:
        if right_span <= 0:
            # Event start == event end: only an instant at event start.
            return 1.0 if session_dt == start_dt else (
                0.5 if session_dt > window_start else None
            )
        pos = (end_dt - session_dt).total_seconds() / right_span
        pos = max(0.0, min(1.0, pos))
        return 0.5 + 0.5 * pos


# --- Service ----------------------------------------------------------------

class EventsService:
    """Coordinates validation, normalisation, and storage for events."""

    def __init__(self, store: Storage) -> None:
        self._store = store

    def create(
        self,
        *,
        title: str,
        kind: str,
        starts_at: str,
        ends_at: str | None = None,
        description: str | None = None,
        location: str | None = None,
        source: str = "manual",
        source_ref: str | None = None,
        course_id: str | None = None,
    ) -> Event:
        title = _validate_title(title)
        kind = _validate_kind(kind)
        source = _validate_source(source)
        starts_at_norm = _validate_starts_at(starts_at)
        ends_at_norm = _validate_ends_at(ends_at, starts_at=starts_at_norm)
        description = _validate_optional_text(
            description, field="description", max_len=MAX_DESCRIPTION_LEN,
        )
        location = _validate_optional_text(
            location, field="location", max_len=MAX_LOCATION_LEN,
        )
        source_ref = _validate_source_ref(source_ref)
        course_id = _validate_course_id(course_id, self._store)
        event_id = uuid.uuid4().hex
        return self._store.create_event(
            event_id,
            title=title,
            kind=kind,
            starts_at=starts_at_norm,
            ends_at=ends_at_norm,
            description=description,
            location=location,
            source=source,
            source_ref=source_ref,
            course_id=course_id,
        )

    def update(self, event_id: str, **fields: Any) -> Event:
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id is required")
        if self._store.get_event(event_id) is None:
            raise KeyError(event_id)
        cleaned: dict[str, Any] = {}
        if "title" in fields:
            cleaned["title"] = _validate_title(fields["title"])
        if "kind" in fields:
            cleaned["kind"] = _validate_kind(fields["kind"])
        if "source" in fields:
            cleaned["source"] = _validate_source(fields["source"])
        if "starts_at" in fields:
            cleaned["starts_at"] = _validate_starts_at(fields["starts_at"])
        if "ends_at" in fields:
            effective_starts = (
                cleaned.get("starts_at")
                or self._store.get_event(event_id).starts_at
            )
            cleaned["ends_at"] = _validate_ends_at(
                fields["ends_at"], starts_at=effective_starts,
            )
        if "description" in fields:
            cleaned["description"] = _validate_optional_text(
                fields["description"],
                field="description", max_len=MAX_DESCRIPTION_LEN,
            )
        if "location" in fields:
            cleaned["location"] = _validate_optional_text(
                fields["location"],
                field="location", max_len=MAX_LOCATION_LEN,
            )
        if "source_ref" in fields:
            cleaned["source_ref"] = _validate_source_ref(
                fields["source_ref"]
            )
        if "course_id" in fields:
            cleaned["course_id"] = _validate_course_id(
                fields["course_id"], self._store,
            )
        if not cleaned:
            existing = self._store.get_event(event_id)
            assert existing is not None
            return existing
        return self._store.update_event(event_id, **cleaned)

    def create_recurring(
        self,
        *,
        title: str,
        kind: str,
        starts_at: str,
        ends_at: str | None = None,
        description: str | None = None,
        location: str | None = None,
        source: str = "manual",
        source_ref: str | None = None,
        course_id: str | None = None,
        repeat_until: str | None = None,
    ) -> tuple[Event, int]:
        """Create a weekly-recurring event series.

        The event repeats on the weekday of ``starts_at``, from the
        start date through ``repeat_until`` (inclusive, ``YYYY-MM-DD``).
        Every occurrence is an independent, fully materialised event
        row — so the calendar, the dashboard, and the pipeline's
        calendar time-matcher all treat it like any other event. The
        time-of-day and duration (``ends_at``) are preserved per
        occurrence; title/kind/course/description/location are shared.

        Returns ``(first_event, created_count)``.

        Raises ``ValueError`` when ``repeat_until`` is before the
        start date or the series would exceed
        :data:`MAX_REPEAT_OCCURRENCES`.
        """
        until_norm = _validate_repeat_until(repeat_until)
        starts_norm = _validate_starts_at(starts_at)
        ends_norm = (
            _validate_ends_at(ends_at, starts_at=starts_norm)
            if ends_at
            else None
        )
        course_id = _validate_course_id(course_id, self._store)
        starts_dt = _parse_iso(starts_norm)
        if until_norm is None:
            weeks = 1
        else:
            until_date = _dt.date.fromisoformat(until_norm)
            start_day = starts_dt.astimezone(timeutil.local_tz()).date()
            if until_date < start_day:
                raise ValueError(
                    "repeat_until must be on or after the event's "
                    "start date"
                )
            weeks = (until_date - start_day).days // 7 + 1
            if weeks > MAX_REPEAT_OCCURRENCES:
                raise ValueError(
                    f"a weekly series until {until_norm} would create "
                    f"{weeks} events; the maximum is "
                    f"{MAX_REPEAT_OCCURRENCES}"
                )
        # Step in local wall-clock time: aware arithmetic on a zoneinfo
        # datetime keeps the time of day, so a 10:00 lecture stays at
        # 10:00 across a clock change (7 x 24 h in UTC would not).
        tz = timeutil.local_tz()
        start_local = starts_dt.astimezone(tz)
        end_local = _parse_iso(ends_norm).astimezone(tz) if ends_norm else None
        first: Event | None = None
        for k in range(weeks):
            step = _dt.timedelta(days=7 * k)
            ev = self.create(
                title=title,
                kind=kind,
                starts_at=_fmt_iso(start_local + step),
                ends_at=(
                    _fmt_iso(end_local + step)
                    if end_local
                    else None
                ),
                description=description,
                location=location,
                source=source,
                source_ref=source_ref,
                course_id=course_id,
            )
            if first is None:
                first = ev
        assert first is not None
        return first, weeks

    def delete(self, event_id: str) -> bool:
        """Delete an event. One from a calendar subscription stays
        deleted: the subscription won't add it again."""
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id is required")
        event = self._store.get_event(event_id)
        if event is not None and event.subscription_id:
            from services.calendar_feeds import SubscriptionsService
            SubscriptionsService(self._store).hide(event)
        return self._store.delete_event(event_id)

    def get(self, event_id: str) -> Event | None:
        if not isinstance(event_id, str) or not event_id:
            return None
        return self._store.get_event(event_id)

    def list(
        self,
        *,
        kind: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> tuple[list[Event], int]:
        if kind is not None:
            kind = _validate_kind(kind)
        if starts_after is not None:
            starts_after = _normalise_iso(starts_after, field="starts_after")
        if starts_before is not None:
            starts_before = _normalise_iso(
                starts_before, field="starts_before",
            )
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive int")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative int")
        return self._store.list_events(
            kind=kind,
            starts_after=starts_after,
            starts_before=starts_before,
            limit=limit,
            offset=offset,
        )

    def suggest_match(self, session_id: str) -> Event | None:
        """Find the best event whose window covers the session's
        ``started_at``. Returns ``None`` if the session doesn't exist
        or no event matches.

        ``matched_session_id`` on the event is NOT touched here —
        this is read-only. Use :meth:`match_session` to commit.
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        sess = self._store.get_session(session_id)
        if sess is None:
            return None
        session_start_dt = _parse_iso(sess.started_at)
        window_lo = (
            session_start_dt - _dt.timedelta(hours=12)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        window_hi = (
            session_start_dt + _dt.timedelta(hours=12)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        candidates, _ = self._store.list_events(
            starts_after=window_lo, starts_before=window_hi, limit=500,
        )
        best: tuple[float, Event] | None = None
        for ev in candidates:
            conf = suggest_match_confidence(
                sess.started_at, ev.starts_at, ev.ends_at,
            )
            if conf is None:
                continue
            if best is None or conf > best[0]:
                best = (conf, ev)
        return best[1] if best else None

    def match_session(
        self, event_id: str, session_id: str, confidence: float,
    ) -> Event:
        """Mark ``event_id`` as the match for ``session_id`` with the
        given confidence. Any other event that was pointing at the
        same session is cleared, enforcing one-event-per-session.

        Raises :class:`KeyError` if the event or session doesn't exist.
        """
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id is required")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        if not isinstance(confidence, (int, float)):
            raise ValueError("confidence must be a number")
        if not (0.0 <= float(confidence) <= 1.0):
            raise ValueError("confidence must be in [0, 1]")
        if self._store.get_event(event_id) is None:
            raise KeyError(event_id)
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        self._store.clear_event_match_for_session(session_id)
        return self._store.update_event(
            event_id,
            matched_session_id=session_id,
            match_confidence=float(confidence),
        )

    # -- .ics import -----------------------------------------------------

    def import_ics(self, ics_bytes: bytes, kind: str = "meeting") -> int:
        """Add the events of an .ics file (repeating ones expanded) as
        ``kind``; returns how many were added or updated. Importing the
        same file again updates its events instead of adding copies
        (services/calendar_feeds.py). A file that isn't a calendar
        imports nothing."""
        from services.calendar_feeds import FeedError, SubscriptionsService
        if not isinstance(ics_bytes, (bytes, bytearray)):
            raise ValueError("ics_bytes must be bytes")
        if not ics_bytes:
            return 0
        try:
            result = SubscriptionsService(self._store).import_file(bytes(ics_bytes), kind)
        except FeedError:
            return 0
        return result.added + result.updated


__all__ = [
    "KINDS",
    "Event",
    "EventsService",
    "suggest_match_confidence",
]
