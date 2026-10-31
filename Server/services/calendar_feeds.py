"""Calendar feeds: subscriptions to a calendar link, and .ics imports.

A subscription is a link (the university timetable, an Apple or Google
calendar shared as a link) that is fetched every hour. Its events are
kept in step with it:

* every event in the feed has a **key**: its UID, plus the occurrence's
  original start for repeating events. Syncing updates the event with
  that key instead of adding another, so nothing is ever duplicated;
* repeating events (RRULE, RDATE, EXDATE, moved or cancelled weeks) are
  expanded only inside a window, ``WINDOW_BACK`` to ``WINDOW_AHEAD``
  around today, so an endless series never becomes endless events; a
  feed with more than ``MAX_EVENTS`` in that window is refused;
* events that leave the feed, or are cancelled in it, are removed on
  the next sync; older events (before the window) are left alone;
* the feed owns an event's title, time, place and description; the
  user owns its kind and course, and an event the user deletes stays
  deleted (its key goes into the subscription's ``hidden_keys``);
* a fetch that fails, or returns something that isn't a calendar (a
  login page), changes nothing; the error is shown on the page.

One-off imports (``import_file``) use the same parsing and the same
keys, so importing a file twice updates instead of duplicating.
"""
from __future__ import annotations

import datetime as _dt
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Optional

import storage
import timeutil
from storage import Event, Storage, Subscription

KINDS = ("lecture", "meeting", "other")
WINDOW_BACK = _dt.timedelta(days=90)
WINDOW_AHEAD = _dt.timedelta(days=365)
IMPORT_WINDOW = _dt.timedelta(days=730)     # a file: two years either way
MAX_EVENTS = 2000
MAX_IMPORT_EVENTS = 5000
MAX_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT_S = 25
SYNC_EVERY = _dt.timedelta(hours=1)
SOURCE_SUB = "subscription"
SOURCE_IMPORT = "ical_import"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# MyTimetable (City) stamps every description with the time of the last
# export; without this every sync would "change" every event.
_STAMP = re.compile(r"\s*Last synchroni[sz]ed on [^\n]*$", re.IGNORECASE)


class FeedError(Exception):
    """The link or file can't be used; the message is for the user."""


@dataclass
class FeedEvent:
    key: str
    title: str
    starts_at: str
    ends_at: Optional[str]
    location: Optional[str]
    description: Optional[str]


@dataclass
class Feed:
    name: str
    events: list[FeedEvent]


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    total: int = 0
    skipped: int = 0        # deleted by the user, or already in a subscription

    def summary(self) -> str:
        parts = [f"{self.added} added" if self.added else "",
                 f"{self.updated} updated" if self.updated else "",
                 f"{self.removed} removed" if self.removed else ""]
        return ", ".join(p for p in parts if p) or "No changes"


# --- Fetching and parsing --------------------------------------------------------

def normalise_url(url: str) -> str:
    """``webcal://`` links are plain web links; only http(s) is fetched."""
    url = (url or "").strip()
    if re.match(r"^webcals?://", url, re.IGNORECASE):
        url = "https://" + url.split("://", 1)[1]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError("Paste a calendar link that starts with https://, http:// or webcal://")
    return url


def display_url(url: str) -> str:
    """The link without its secret part (the query), for the page."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path if len(parts.path) <= 24 else parts.path[:22] + "…"
    return parts.netloc + path + ("?…" if parts.query else "")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(normalise_url(url), headers={
        "User-Agent": "AfterWord calendar sync", "Accept": "text/calendar, */*;q=0.5",
    })
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            data = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FeedError(f"The calendar server answered {exc.code} ({exc.reason}). Is the link still valid?") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise FeedError(f"Couldn't reach the calendar ({reason}).") from exc
    if len(data) > MAX_BYTES:
        raise FeedError("That calendar is larger than 10 MB.")
    return data


def _utc(value, *, end_of_day: bool = False) -> _dt.datetime:
    """A DTSTART/DTEND value as UTC. All-day dates become local noon (or
    23:59:59 for the end of a one-day event), as imports always did;
    floating times are local."""
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        t = _dt.time(23, 59, 59) if end_of_day else _dt.time(12, 0)
        value = _dt.datetime.combine(value, t, tzinfo=timeutil.local_tz())
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timeutil.local_tz())
    return value.astimezone(_dt.timezone.utc)


def _iso(value: _dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = _CONTROL.sub("", str(value)).strip()
    return text[:limit] or None


def parse(data: bytes, start: _dt.date, end: _dt.date, cap: int = MAX_EVENTS) -> Feed:
    """The feed's events between ``start`` and ``end`` (repeats
    expanded, cancelled ones left out), each with its key."""
    from icalendar import Calendar
    import recurring_ical_events

    head = (data or b"").lstrip(b"\xef\xbb\xbf \t\r\n")[:15].upper()
    if not head.startswith(b"BEGIN:VCALENDAR"):
        raise FeedError("That link didn't return a calendar (maybe a web page or a login page).")
    try:
        cal = Calendar.from_ical(bytes(data))
    except (ValueError, TypeError) as exc:
        raise FeedError("The calendar couldn't be read.") from exc
    repeating = {
        str(c.get("UID", "")) for c in cal.walk("VEVENT")
        if "RRULE" in c or "RDATE" in c or "RECURRENCE-ID" in c
    }
    try:
        occurrences = recurring_ical_events.of(cal, skip_bad_series=True).between(start, end)
    except Exception as exc:  # the library raises its own errors for broken rules
        raise FeedError(f"The calendar's repeating events couldn't be read ({exc}).") from exc

    events: list[FeedEvent] = []
    seen: set[str] = set()
    for comp in occurrences:
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        title = _clean(comp.get("SUMMARY"), 200)
        dtstart = comp.get("DTSTART")
        if not title or dtstart is None:
            continue
        try:
            starts = _utc(dtstart.dt)
            dtend = comp.get("DTEND")
            ends = None
            if dtend is not None:
                all_day = not isinstance(dtend.dt, _dt.datetime)
                one_day = all_day and dtend.dt <= (dtstart.dt + _dt.timedelta(days=1))
                ends = _utc(dtstart.dt if one_day else dtend.dt, end_of_day=one_day)
            elif comp.get("DURATION") is not None:
                ends = starts + comp["DURATION"].dt
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if ends is not None and ends < starts:
            ends = None
        uid = _clean(comp.get("UID"), 400) or f"{title}|{_iso(starts)}"
        key = uid
        if uid in repeating:
            rid = comp.get("RECURRENCE-ID")
            key = f"{uid}@{_iso(_utc(rid.dt)) if rid is not None else _iso(starts)}"
        if key in seen:
            continue
        seen.add(key)
        description = _clean(comp.get("DESCRIPTION"), 4000)
        if description:
            description = _STAMP.sub("", description).strip()[:2000] or None
        events.append(FeedEvent(
            key=key, title=title, starts_at=_iso(starts), ends_at=_iso(ends) if ends else None,
            location=_clean(comp.get("LOCATION"), 200), description=description,
        ))
        if len(events) > cap:
            raise FeedError(f"That calendar has more than {cap} events in the coming year; it can't be added.")
    # X-WR-CALNAME isn't a standard property, so its text escapes ("\,")
    # are left in.
    raw_name = re.sub(r"\\([,;\\])", r"\1", str(cal.get("X-WR-CALNAME", ""))).replace("\\n", " ")
    name = _clean(raw_name, 100) or ""
    return Feed(name=name, events=events)


# --- Courses ----------------------------------------------------------------------

_CODE = re.compile(r"^([A-Za-z]{2,5}\d{2,4}[A-Za-z]?)\b[\s:·-]*(.*)$")
_PART = re.compile(r"\s+(lecture|lab|tutorial|seminar|workshop|class|practical)s?\s*\d*$", re.IGNORECASE)


def module_of(title: str) -> tuple[str, str]:
    """``("SMM940", "Financial Markets …")`` from a timetable title such
    as "SMM940 Financial Markets …/Lecture 01"; ``("", …)`` when the
    title has no module code."""
    head = title.split("/")[0].strip().lstrip("_").strip()
    m = _CODE.match(head)
    code, name = (m.group(1).upper(), m.group(2).strip()) if m else ("", head)
    return code, _PART.sub("", name).strip() or name


def match_course(title: str, courses: list) -> Optional[str]:
    """The course an event belongs to: its code in the title, or else
    the longest course name in it."""
    low = title.lower()
    for c in courses:
        code = (c.code or "").strip().lower()
        if code and re.search(r"(?<![a-z0-9])" + re.escape(code) + r"(?![a-z0-9])", low):
            return c.id
    named = [c for c in courses if len(c.name.strip()) >= 4 and c.name.strip().lower() in low]
    return max(named, key=lambda c: len(c.name)).id if named else None


# --- Applying a feed --------------------------------------------------------------

def _changes(ev: Event, fe: FeedEvent) -> dict:
    out = {}
    for f in ("title", "starts_at", "ends_at", "location", "description"):
        if getattr(ev, f) != getattr(fe, f):
            out[f] = getattr(fe, f)
    return out


class SubscriptionsService:
    """Subscriptions, their syncing, and .ics imports."""

    def __init__(self, store: Storage) -> None:
        self._store = store
        self._lock = threading.Lock()       # one sync at a time

    # --- the list -----------------------------------------------------------------
    def list(self) -> list[Subscription]:
        return self._store.list_subscriptions()

    def get(self, sub_id: str) -> Optional[Subscription]:
        return self._store.get_subscription(sub_id)

    def _check(self, kind: str, course_id: Optional[str]) -> None:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if course_id and self._store.get_course(course_id) is None:
            raise ValueError("That course doesn't exist")

    def add(self, url: str, *, name: str = "", kind: str = "lecture",
            course_id: Optional[str] = None) -> tuple[Subscription, SyncResult]:
        """Check the link (it must return a calendar), save it and add
        its events. Raises ``ValueError`` / ``FeedError`` and saves
        nothing when the link doesn't work."""
        url = normalise_url(url)
        self._check(kind, course_id)
        if any(s.url == url for s in self.list()):
            raise ValueError("You're already subscribed to that calendar")
        feed = self._read(url)
        sub = self._store.create_subscription(
            uuid.uuid4().hex, name=_clean(name, 100) or feed.name or urllib.parse.urlsplit(url).netloc,
            url=url, kind=kind, course_id=course_id or None,
        )
        with self._lock:
            result = self._apply_subscription(sub, feed)
        return self._store.get_subscription(sub.id), result  # type: ignore[return-value]

    def update(self, sub_id: str, *, name: Optional[str] = None, kind: Optional[str] = None,
               course_id: object = storage._UNSET) -> Subscription:
        """Rename, or change what its events are: a new kind or course
        applies to all of them (``course_id=None``: match by name)."""
        sub = self._store.get_subscription(sub_id)
        if sub is None:
            raise KeyError(sub_id)
        fields: dict = {}
        if name is not None:
            if not _clean(name, 100):
                raise ValueError("A subscription needs a name")
            fields["name"] = _clean(name, 100)
        if kind is not None and kind != sub.kind:
            self._check(kind, None)
            fields["kind"] = kind
        if course_id is not storage._UNSET and course_id != sub.course_id:
            self._check(sub.kind, course_id)  # type: ignore[arg-type]
            fields["course_id"] = course_id or None
        sub = self._store.update_subscription(sub_id, **fields) if fields else sub
        if "kind" in fields or "course_id" in fields:
            courses = self._store.list_courses()[0]
            for ev in self._store.events_from_feed(sub.id):
                change: dict = {}
                if "kind" in fields and ev.kind != sub.kind:
                    change["kind"] = sub.kind
                if "course_id" in fields:
                    want = sub.course_id or match_course(ev.title, courses)
                    if ev.course_id != want:
                        change["course_id"] = want
                if change:
                    self._store.update_event(ev.id, **change)
        return sub

    def delete(self, sub_id: str) -> bool:
        """Remove a subscription and its events (recordings stay)."""
        return self._store.delete_subscription(sub_id)

    def hide(self, event: Event) -> None:
        """The user deleted a subscribed event: keep it out from now on."""
        sub = self._store.get_subscription(event.subscription_id or "")
        if sub is not None and event.source_ref:
            self._store.update_subscription(sub.id, hidden_keys=sub.hidden_keys + [event.source_ref])

    # --- syncing --------------------------------------------------------------------
    def _read(self, url: str) -> Feed:
        today = timeutil.local_today()
        return parse(fetch(url), today - WINDOW_BACK, today + WINDOW_AHEAD)

    def sync(self, sub_id: str) -> SyncResult:
        """Fetch and apply one subscription. On a failure nothing
        changes, the error is kept for the page, and FeedError raised."""
        sub = self._store.get_subscription(sub_id)
        if sub is None:
            raise KeyError(sub_id)
        with self._lock:
            self._store.update_subscription(sub.id, last_attempt_at=storage.now_iso())
            try:
                feed = self._read(sub.url)
            except (FeedError, ValueError) as exc:
                self._store.update_subscription(sub.id, last_error=str(exc))
                raise FeedError(str(exc)) from exc
            return self._apply_subscription(self._store.get_subscription(sub.id), feed)  # type: ignore[arg-type]

    def sync_due(self, every: _dt.timedelta = SYNC_EVERY) -> None:
        """Sync each subscription not tried in the last ``every``."""
        now = _dt.datetime.now(_dt.timezone.utc)
        for sub in self.list():
            last = timeutil.to_local(sub.last_attempt_at)
            if last is not None and now - last < every:
                continue
            try:
                self.sync(sub.id)
            except FeedError as exc:
                print(f"[LR ] calendar {sub.name!r}: {exc}", flush=True)
            except Exception as exc:  # pragma: no cover  (keep the loop alive)
                print(f"[LR ] calendar {sub.name!r}: {exc!r}", flush=True)

    def _apply_subscription(self, sub: Subscription, feed: Feed) -> SyncResult:
        store, result = self._store, SyncResult(total=len(feed.events))
        courses = store.list_courses()[0]
        hidden = set(sub.hidden_keys)
        existing: dict[str, Event] = {}
        for ev in store.events_from_feed(sub.id):
            if ev.source_ref in existing:          # a duplicate from before: drop it
                store.delete_event(ev.id)
                continue
            existing[ev.source_ref or ""] = ev
        # Events imported from a file earlier become this subscription's
        # (instead of a second copy).
        imported = {ev.source_ref: ev for ev in store.events_from_feed(None, source=SOURCE_IMPORT)}
        seen: set[str] = set()
        for fe in feed.events:
            seen.add(fe.key)
            if fe.key in hidden:
                result.skipped += 1
                continue
            ev = existing.get(fe.key)
            if ev is None and fe.key in imported:
                ev = store.update_event(imported[fe.key].id, subscription_id=sub.id, source=SOURCE_SUB)
                existing[fe.key] = ev
            if ev is not None:
                change = _changes(ev, fe)
                if ev.course_id is None:
                    found = sub.course_id or match_course(fe.title, courses)
                    if found:
                        change["course_id"] = found
                if change:
                    store.update_event(ev.id, **change)
                    result.updated += 1
                continue
            store.create_event(
                uuid.uuid4().hex, title=fe.title, kind=sub.kind, starts_at=fe.starts_at,
                ends_at=fe.ends_at, description=fe.description, location=fe.location,
                source=SOURCE_SUB, source_ref=fe.key, subscription_id=sub.id,
                course_id=sub.course_id or match_course(fe.title, courses),
            )
            result.added += 1
        # Gone from the feed (or cancelled): remove it, but only inside
        # the window; older events are history and stay.
        window_start = _iso(_utc(_dt.datetime.combine(timeutil.local_today() - WINDOW_BACK, _dt.time())))
        for key, ev in existing.items():
            if key not in seen and ev.starts_at >= window_start:
                store.delete_event(ev.id)
                result.removed += 1
        store.update_subscription(
            sub.id, last_synced_at=storage.now_iso(), last_error=None,
            event_count=len(store.events_from_feed(sub.id)),
        )
        return result

    # --- one-off imports ---------------------------------------------------------------
    def import_file(self, data: bytes, kind: str = "meeting") -> SyncResult:
        """Add a calendar file's events; importing the same file again
        updates them instead of adding copies. Events already coming
        from a subscription are left to it."""
        self._check(kind, None)
        today = timeutil.local_today()
        feed = parse(data, today - IMPORT_WINDOW, today + IMPORT_WINDOW, cap=MAX_IMPORT_EVENTS)
        store, result = self._store, SyncResult(total=len(feed.events))
        courses = store.list_courses()[0]
        subscribed = {ev.source_ref for s in self.list() for ev in store.events_from_feed(s.id)}
        existing = {ev.source_ref: ev for ev in store.events_from_feed(None, source=SOURCE_IMPORT)}
        with self._lock:
            for fe in feed.events:
                if fe.key in subscribed:
                    result.skipped += 1
                    continue
                ev = existing.get(fe.key)
                if ev is not None:
                    change = _changes(ev, fe)
                    if change:
                        store.update_event(ev.id, **change)
                        result.updated += 1
                    continue
                store.create_event(
                    uuid.uuid4().hex, title=fe.title, kind=kind, starts_at=fe.starts_at,
                    ends_at=fe.ends_at, description=fe.description, location=fe.location,
                    source=SOURCE_IMPORT, source_ref=fe.key,
                    course_id=match_course(fe.title, courses),
                )
                result.added += 1
        return result

    # --- courses for a subscription's modules --------------------------------------------
    def missing_courses(self, sub_id: str) -> list[dict]:
        """Modules (with a code) among a subscription's events that have
        no course yet: ``[{code, name, events}]``."""
        found: dict[str, dict] = {}
        for ev in self._store.events_from_feed(sub_id):
            if ev.course_id:
                continue
            code, name = module_of(ev.title)
            if not code:
                continue
            entry = found.setdefault(code, {"code": code, "name": name, "events": 0})
            entry["events"] += 1
        return sorted(found.values(), key=lambda m: m["code"])

    def create_courses(self, sub_id: str, courses_service) -> int:
        """Create a course for each module without one, then give the
        subscription's course-less events their course. Returns the
        number of courses created."""
        made = 0
        for m in self.missing_courses(sub_id):
            courses_service.create(name=m["name"] or m["code"], code=m["code"])
            made += 1
        courses = self._store.list_courses()[0]
        for ev in self._store.events_from_feed(sub_id):
            if not ev.course_id:
                found = match_course(ev.title, courses)
                if found:
                    self._store.update_event(ev.id, course_id=found)
        return made


def start_scheduler(app) -> threading.Thread:
    """Sync due subscriptions every few minutes, in the background.
    ``app.config["LR_SUBSCRIPTION_SYNC"] = False`` turns it off (tests)."""
    def run() -> None:
        time.sleep(20)
        while True:
            if app.config.get("LR_SUBSCRIPTION_SYNC", True):
                try:
                    app.config["LR_SUBSCRIPTIONS_SERVICE"].sync_due()
                except Exception as exc:  # pragma: no cover
                    print(f"[LR ] calendar sync: {exc!r}", flush=True)
            time.sleep(300)

    t = threading.Thread(target=run, name="lr-calendar-sync", daemon=True)
    t.start()
    return t
