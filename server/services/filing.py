"""Filing: deadlines, to-dos and dated events found in a recording.

The extraction step (``services.llm``) returns typed items. Those from
lectures and meetings are only *proposed* (:func:`proposals`): the
reader lists them below the summary and the user adds each one by hand
(:func:`decide`). Those from a ``command`` recording are the user's own
request and are filed at once (:func:`refile`). Either way, three item
types are copied into the user's lists (``source='extracted'``):

* ``deadline`` with a ``due`` date  -> Deadlines (a task of kind deadline)
* ``action_item``                    -> Todos (``due`` if one was said)
* ``event`` with a ``start`` time    -> Calendar (date only -> a deadline)

Items from lectures and meetings link back to the recording and the
moment they were said (``source_ref``, ``source_offset_ms``). Items
from a ``command`` recording ("add X to my calendar") carry no link:
the recording is only its request.

Re-processing a recording first removes the linked items it filed
that were never touched (still open, never edited), so a re-run does
not pile up copies and nothing the user edited or ticked off is lost.
Items that repeat something already in the lists, or that are dated
before the recording, are skipped.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any

import timeutil
from services.events import KINDS as EVENT_KINDS
from services.events import EventsService
from services.tasks import TasksService
from storage import Storage, title_is_placeholder

#: Recording kinds whose items are filed. Ordinary voice memos are
#: notes, and "other" recordings (a video playing, say) are too noisy.
FILING_KINDS = frozenset({"lecture", "meeting", "command"})
#: Kinds whose filed items carry no link back to the recording.
UNLINKED_KINDS = frozenset({"command"})

SOURCE = "extracted"
_EVENT_LENGTH = _dt.timedelta(hours=1)
_MAX_TITLE = 200


class _Link:
    """Where a filed item came from: the recording and the moment."""

    def __init__(self, sess, offset_ms: int | None) -> None:
        self.session_id = sess.id
        self.offset_ms = offset_ms
        named = sess.title and not (sess.title_source is None and title_is_placeholder(sess))
        name = f"the recording “{sess.title}”" if named else "a recording"
        at = f" at {_clock(offset_ms)}" if offset_ms is not None else ""
        self.description = f"From {name}{at}."


#: Recording kinds whose items are only proposed: the reader lists them
#: below the summary and the user adds each one by hand. A "command"
#: recording is the user's own request, so it is filed straight away.
PROPOSAL_KINDS = FILING_KINDS - UNLINKED_KINDS

#: Artifact holding what was decided about each proposal, by position in
#: the extraction: ``{"3": "added", "5": "dismissed"}``.
DECISIONS = "decisions"


def _extracted_items(storage: Storage, session_id: str) -> list:
    art = storage.get_artifact(session_id, "extraction")
    try:
        items = json.loads((art or {}).get("content") or "")["items"]
    except (TypeError, ValueError, KeyError):
        return []
    return items if isinstance(items, list) else []


def _decisions(storage: Storage, session_id: str) -> dict[str, str]:
    art = storage.get_artifact(session_id, DECISIONS)
    try:
        data = json.loads((art or {}).get("content") or "")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _decide(storage: Storage, session_id: str, index: int, what: str) -> None:
    decisions = _decisions(storage, session_id)
    decisions[str(index)] = what
    storage.add_artifact(session_id, DECISIONS, json.dumps(decisions))


_LABEL = {"deadline": "Deadline", "action_item": "To-do", "event": "Calendar"}


def proposals(storage: Storage, session_id: str) -> list[dict]:
    """What the recording suggested and the user has not yet decided on:
    ``{"index", "label", "when", "ms"}`` per item. Only lectures and
    meetings propose; items that could not be filed (no usable date,
    dated before the recording) are not shown."""
    sess = storage.get_session(session_id)
    if sess is None or sess.kind not in PROPOSAL_KINDS:
        return []
    recorded = (timeutil.to_local(sess.started_at) or timeutil.local_now()).date()
    decided = _decisions(storage, session_id)
    out = []
    for index, item in enumerate(_extracted_items(storage, session_id)):
        if str(index) in decided or not isinstance(item, dict):
            continue
        shown = _describe(item, recorded)
        if shown is None:
            continue
        label, text, when = shown
        out.append(
            {
                "index": index,
                "label": f"{label} · {text}",
                "when": when,
                "ms": _offset_ms(item.get("at")),
            }
        )
    return out


def _describe(item: dict, recorded_day: _dt.date) -> tuple[str, str, str] | None:
    """``(list name, text, date shown)`` for an item that can be filed."""
    kind = item.get("type")
    text = " ".join(str(item.get("text", "")).split())[:_MAX_TITLE]
    if kind not in _LABEL or not text:
        return None
    if kind == "event":
        when = _local(item.get("start"))
        label = "Calendar" if when and when[1] else "Deadline"
    else:
        when = _local(item.get("due"))
        label = "Deadline" if kind == "deadline" and when else "To-do"
    if kind == "event" and when is None:
        return None
    if when is not None and when[0].date() < recorded_day:
        return None
    shown = ""
    if when is not None:
        shown = f"{when[0]:%a} {when[0].day} {when[0]:%b}" + (
            f", {when[0]:%H:%M}" if when[1] else ""
        )
    return label, text, shown


def decide(storage: Storage, session_id: str, index: int, action: str) -> str:
    """Add (``"add"``) or drop (``"dismiss"``) one proposal. Returns
    ``"added"``, ``"duplicate"`` (already in the lists) or
    ``"dismissed"``. Raises ``KeyError`` for a proposal that does not
    exist or was already decided."""
    sess = storage.get_session(session_id)
    if sess is None or sess.kind not in PROPOSAL_KINDS:
        raise KeyError(session_id)
    items = _extracted_items(storage, session_id)
    if not 0 <= index < len(items) or str(index) in _decisions(storage, session_id):
        raise KeyError(index)
    item = items[index]
    if action == "dismiss":
        _decide(storage, session_id, index, "dismissed")
        return "dismissed"
    recorded = timeutil.to_local(sess.started_at) or timeutil.local_now()
    link = _Link(sess, _offset_ms(item.get("at") if isinstance(item, dict) else None))
    filed = _file_one(
        item, sess, recorded.date(), TasksService(storage), EventsService(storage), link
    )
    _decide(storage, session_id, index, "added")
    return "added" if filed else "duplicate"


def refile(storage: Storage, session_id: str, items: list[dict]) -> dict:
    """Replace what ``session_id`` filed with ``items``.

    Returns counts per list: ``{"task": n, "deadline": n, "event": n}``.
    Items that cannot be filed (no usable date where one is needed,
    a duplicate, a past date) are skipped silently.
    """
    counts = {"task": 0, "deadline": 0, "event": 0}
    sess = storage.get_session(session_id)
    if sess is None:
        return counts
    storage.delete_untouched_filed(session_id)
    linked = sess.kind not in UNLINKED_KINDS
    recorded = timeutil.to_local(sess.started_at) or timeutil.local_now()
    tasks, events = TasksService(storage), EventsService(storage)
    for item in items:
        if not isinstance(item, dict):
            continue
        link = _Link(sess, _offset_ms(item.get("at"))) if linked else None
        try:
            filed = _file_one(item, sess, recorded.date(), tasks, events, link)
        except ValueError:
            continue
        if filed:
            counts[filed] += 1
    return counts


def _file_one(
    item: dict,
    sess,
    recorded_day: _dt.date,
    tasks: TasksService,
    events: EventsService,
    link: _Link | None,
) -> str | None:
    kind = item.get("type")
    text = " ".join(str(item.get("text", "")).split())[:_MAX_TITLE]
    if kind not in ("deadline", "action_item", "event") or not text:
        return None
    if kind == "event":
        start = _local(item.get("start"))
        if start is None:
            return None
        if not start[1]:  # a date without a time
            return _add_task(tasks, sess, "deadline", text, start[0], recorded_day, link)
        return _add_event(events, sess, text, item, start[0], recorded_day, link)
    due = _local(item.get("due"))
    if kind == "deadline" and due is not None:
        return _add_task(tasks, sess, "deadline", text, due[0], recorded_day, link)
    return _add_task(tasks, sess, "task", text, due[0] if due else None, recorded_day, link)


def _add_task(
    tasks: TasksService,
    sess,
    kind: str,
    text: str,
    due: _dt.datetime | None,
    recorded_day: _dt.date,
    link: _Link | None,
) -> str | None:
    if due is not None and due.date() < recorded_day:
        return None
    day = due.date() if due else None
    # Compared with both lists: the same thing may come back once as a
    # deadline and once as a to-do.
    open_items, _ = tasks.list(status="open", limit=1000)
    if any(_same_day(t.due_at, day) and _similar(t.title, text) for t in open_items):
        return None
    tasks.create(
        title=text,
        kind=kind,
        due_at=due.strftime("%Y-%m-%dT%H:%M") if due else None,
        description=link.description if link else None,
        source=SOURCE,
        source_ref=link.session_id if link else None,
        source_offset_ms=link.offset_ms if link else None,
        course_id=sess.course_id,
    )
    return kind


def _add_event(
    events: EventsService,
    sess,
    text: str,
    item: dict,
    start: _dt.datetime,
    recorded_day: _dt.date,
    link: _Link | None,
) -> str | None:
    if start.date() < recorded_day:
        return None
    end = _local(item.get("end"))
    end_dt = end[0] if end and end[1] and end[0] > start else start + _EVENT_LENGTH
    day_start = _dt.datetime.combine(start.date(), _dt.time())
    same_day, _ = events.list(
        starts_after=day_start.strftime("%Y-%m-%dT%H:%M"),
        starts_before=(day_start + _dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
    )
    if any(_similar(e.title, text) for e in same_day):
        return None
    event_kind = item.get("event_kind")
    if event_kind not in EVENT_KINDS:
        event_kind = "other"
    location = item.get("location")
    events.create(
        title=text,
        kind=event_kind,
        location=" ".join(location.split())[:200]
        if isinstance(location, str) and location.strip()
        else None,
        starts_at=start.strftime("%Y-%m-%dT%H:%M"),
        ends_at=end_dt.strftime("%Y-%m-%dT%H:%M"),
        description=link.description if link else None,
        source=SOURCE,
        source_ref=link.session_id if link else None,
        course_id=sess.course_id if event_kind == "lecture" else None,
    )
    return "event"


# --- parsing --------------------------------------------------------------

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _local(value: Any) -> tuple[_dt.datetime, bool] | None:
    """A model date ('2026-11-06' or '2026-11-06T14:00', local time) as
    ``(naive local datetime, has_time)``; None if absent or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if _DATE.match(value):
            return _dt.datetime.fromisoformat(value), False
        dt = _dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timeutil.local_tz()).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0), True


def _offset_ms(value: Any) -> int | None:
    """'12:34' or '1:02:03' (a transcript marker) as milliseconds."""
    if not isinstance(value, str):
        return None
    parts = value.strip().strip("[]").split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + int(p)
    return seconds * 1000


def _clock(offset_ms: int) -> str:
    s = offset_ms // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# --- duplicates -----------------------------------------------------------

_WORD = re.compile(r"\w+")


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower().replace("ё", "е")) if len(w) > 2}


def _similar(a: str, b: str) -> bool:
    """The same item worded a little differently (most words shared)."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return a.strip().lower() == b.strip().lower()
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.75


def _same_day(due_at: str | None, day: _dt.date | None) -> bool:
    if due_at is None or day is None:
        return due_at is None and day is None
    local = timeutil.to_local(due_at)
    return local is not None and local.date() == day
