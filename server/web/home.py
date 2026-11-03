"""What Home shows.

Everything is worked out here, in local time, so the template only lays
it out: the week and today's schedule with each event's status, the next
event, the recent recordings and notes, deadlines and to-dos.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
from typing import Any

import storage
import timeutil
from services.llm import parse_transcript_lines
from web.common import (
    _estimate_duration_seconds,
    _short_duration,
    course_cls,
    current_app_courses_service,
    current_app_events_service,
    current_app_notes_service,
    current_app_storage,
    current_app_tasks_service,
    rec_title,
    recording_url,
)

# Statuses of a recording the AI is still working on.
PROCESSING = frozenset({
    storage.STATUS_STITCHING, storage.STATUS_READY, storage.STATUS_TRANSCRIBING,
    storage.STATUS_TRANSCRIBED, storage.STATUS_CLASSIFYING,
    storage.STATUS_SUMMARISING, storage.STATUS_EXTRACTING,
})
RECENT_LIMIT = 12
DAY_START, DAY_END = 8, 19          # the Today timeline, widened to fit events


def _clock(ms: int | None) -> str:
    s = (ms or 0) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _day_label(day: _dt.date, today: _dt.date) -> str:
    if day == today:
        return "Today"
    if day == today - _dt.timedelta(days=1):
        return "Yesterday"
    if day == today + _dt.timedelta(days=1):
        return "Tomorrow"
    return f"{day:%a} {day.day} {day:%b}"


def _first_sentence(text: str, limit: int = 180) -> str:
    text = " ".join((text or "").split())
    for end in (". ", "! ", "? "):
        i = text.find(end)
        if 0 < i < limit:
            return text[: i + 1]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _Ctx:
    """Shared lookups for one page render."""

    def __init__(self) -> None:
        self.store = current_app_storage()
        self.now = timeutil.local_now()
        self.today = self.now.date()
        self.courses = {c.id: c for c in current_app_courses_service().list()[0]}

    def cc(self, course_id: str | None) -> str:
        course = self.courses.get(course_id or "")
        return course_cls(course.color if course else None)

    def course_name(self, course_id: str | None) -> str:
        course = self.courses.get(course_id or "")
        return course.name if course else ""


# --- schedule -----------------------------------------------------------

def _event_status(ctx: _Ctx, ev) -> tuple[str, str, str | None]:
    """(label, status class, recording url) for an event, now."""
    sess = ctx.store.get_session(ev.matched_session_id) if ev.matched_session_id else None
    if sess is not None:
        url = recording_url(sess)
        if sess.status == storage.STATUS_DONE:
            return "Summary ready", "st-ok", url
        if sess.status == storage.STATUS_FAILED:
            return "Processing failed", "st-bad", url
        return "Summarising", "st-busy", url
    start = timeutil.to_local(ev.starts_at)
    end = timeutil.to_local(ev.ends_at) if ev.ends_at else (start + _dt.timedelta(hours=1) if start else None)
    if start is None:
        return "", "st-later", None
    if start <= ctx.now < end:
        return "Now", "st-soon", None
    if ctx.now < start:
        minutes = math.ceil((start - ctx.now).total_seconds() / 60)
        if minutes <= 60:
            return f"In {minutes} min", "st-soon", None
        return "Upcoming", "st-later", None
    return "Not recorded", "st-later", None


def _event_view(ctx: _Ctx, ev) -> dict[str, Any]:
    start = timeutil.to_local(ev.starts_at)
    end = timeutil.to_local(ev.ends_at) if ev.ends_at else None
    label, sc, rec = _event_status(ctx, ev)
    start_min = start.hour * 60 + start.minute
    end_min = (end.hour * 60 + end.minute) if end and end.date() == start.date() else start_min + 60
    return {
        "id": ev.id, "title": ev.title, "location": ev.location or "",
        "description": (ev.description or "").strip().split("\n")[0],
        "date": start.date(), "start": f"{start:%H:%M}",
        "end": f"{end:%H:%M}" if end else "",
        "start_min": start_min, "end_min": max(end_min, start_min + 15),
        "cc": ctx.cc(ev.course_id), "status": label, "sc": sc,
        "url": rec or f"/calendar?day={start.date().isoformat()}",
        "recording": rec, "kind": ev.kind,
        "is_past": (end or start + _dt.timedelta(hours=1)) <= ctx.now,
    }


def _events_between(ctx: _Ctx, first: _dt.date, days: int) -> list[dict[str, Any]]:
    events, _ = current_app_events_service().list(
        starts_after=timeutil.local_midnight_utc(first),
        starts_before=timeutil.local_midnight_utc(first + _dt.timedelta(days=days)),
        limit=500,
    )
    views = [_event_view(ctx, ev) for ev in events if timeutil.to_local(ev.starts_at)]
    return sorted(views, key=lambda v: (v["date"], v["start_min"]))


def _schedule(ctx: _Ctx, week_offset: int) -> dict[str, Any]:
    monday = ctx.today - _dt.timedelta(days=ctx.today.weekday()) + _dt.timedelta(weeks=week_offset)
    week_events = _events_between(ctx, monday, 7)
    now_min = ctx.now.hour * 60 + ctx.now.minute
    days = []
    for i in range(7):
        day = monday + _dt.timedelta(days=i)
        items = [v for v in week_events if v["date"] == day]
        days.append({
            "dow": f"{day:%a}", "num": day.day, "is_today": day == ctx.today,
            "events": items,
            # Where the now line goes in today's list.
            "now_at": sum(1 for v in items if v["start_min"] <= now_min) if day == ctx.today else None,
        })
    sunday = monday + _dt.timedelta(days=6)
    today_events = (
        [v for v in week_events if v["date"] == ctx.today] if week_offset == 0
        else _events_between(ctx, ctx.today, 1)
    )
    start_h = min([DAY_START] + [v["start_min"] // 60 for v in today_events])
    end_h = max([DAY_END] + [math.ceil(v["end_min"] / 60) for v in today_events])
    span = (end_h - start_h) * 60
    for v in today_events:
        v["top"] = (v["start_min"] - start_h * 60) / span * 100
        v["height"] = (v["end_min"] - v["start_min"]) / span * 100
    return {
        "week_offset": week_offset,
        "week_label": f"{monday.day} {monday:%b} – {sunday.day} {sunday:%b}",
        "days": days,
        "today": today_events,
        "hours": list(range(start_h, end_h + 1)),
        "day_start": start_h, "day_end": end_h,
        "now_pct": (now_min - start_h * 60) / span * 100 if start_h * 60 <= now_min <= end_h * 60 else None,
    }


def _next_up(ctx: _Ctx) -> dict[str, Any] | None:
    events, _ = current_app_events_service().list(
        starts_after=timeutil.fmt(ctx.now), limit=1,
    )
    if not events:
        return None
    view = _event_view(ctx, events[0])
    start = timeutil.to_local(events[0].starts_at)
    minutes = math.ceil((start - ctx.now).total_seconds() / 60)
    if minutes < 60:
        when = f"in {minutes} min"
    elif start.date() == ctx.today:
        h, m = divmod(minutes, 60)
        when = f"in {h} h {m} min" if m else f"in {h} h"
    else:
        when = _day_label(start.date(), ctx.today)
    view["when"] = when
    view["place"] = " · ".join(filter(None, [view["location"], view["description"]]))
    return view


# --- recent ---------------------------------------------------------------

_KIND_CHIP = {"lecture": "Lecture", "meeting": "Meeting", "voice_memo": "Voice memo"}


def _gist(ctx: _Ctx, sess) -> str:
    summary = ctx.store.get_artifact(sess.id, "summary")
    if summary and summary.get("content"):
        try:
            text = json.loads(summary["content"]).get("text", "")
        except (TypeError, ValueError, AttributeError):
            text = summary["content"]
        if text:
            return _first_sentence(text)
    transcript = ctx.store.get_artifact(sess.id, "transcript")
    if transcript and transcript.get("content"):
        lines = parse_transcript_lines(transcript["content"])
        words = " ".join(line["text"] for line in lines[:3])
        if sess.kind == "voice_memo":
            return "“" + _first_sentence(words, 140) + "”"
        if sess.status in PROCESSING:
            return "The transcript is ready and searchable; the summary follows in a minute."
        return _first_sentence(words)
    if sess.status in PROCESSING:
        return "Being transcribed."
    return ""


def _recording_item(ctx: _Ctx, sess) -> dict[str, Any]:
    started = timeutil.to_local(sess.started_at)
    seconds = _estimate_duration_seconds(sess)
    when = " · ".join(filter(None, [
        f"{_day_label(started.date(), ctx.today)} {started:%H:%M}" if started else "",
        _short_duration(seconds) if seconds >= 1 else "",
    ]))
    processing = sess.status in PROCESSING
    failed = sess.status == storage.STATUS_FAILED
    filed_tasks, _ = ctx.store.filed_from(sess.id)
    filed = [{
        "at": _clock(t.source_offset_ms),
        "label": ("Deadline · " if t.kind == "deadline" else "To-do · ") + t.title,
        "url": recording_url(sess, t=t.source_offset_ms or 0),
    } for t in filed_tasks[:3]]
    kind = sess.kind if sess.kind in _KIND_CHIP else None
    course = ctx.course_name(sess.course_id) if kind == "lecture" else ""
    return {
        "kind": {"voice_memo": "note"}.get(kind or "", kind or "other"),
        "icon": {"voice_memo": "note", None: "other"}.get(kind, kind),
        "chip": course or _KIND_CHIP.get(kind or "", "New recording"),
        "cc": ctx.cc(sess.course_id),
        "when": when,
        "status": "Summarising" if processing and kind else ("Processing" if processing else ("Failed" if failed else "")),
        "sc": "st-busy" if processing else ("st-bad" if failed else "st-ok"),
        "title": rec_title(sess),
        "gist": _gist(ctx, sess),
        "filed": filed,
        "url": recording_url(sess),
        "sort": sess.started_at or "",
    }


def _note_item(ctx: _Ctx, note) -> dict[str, Any]:
    edited = timeutil.to_local(note.updated_at)
    # The first line of text, not a heading.
    first = next((line.strip().lstrip("-*[] x").strip()
                  for line in (note.body_md or "").splitlines()
                  if line.strip() and not line.lstrip().startswith("#")), "")
    when = ""
    sess = ctx.store.get_session(note.source_ref) if note.source == "voice_memo" and note.source_ref else None
    if sess is not None:
        # A voice memo: when it was recorded, and how long.
        started = timeutil.to_local(sess.started_at)
        seconds = _estimate_duration_seconds(sess)
        when = " · ".join(filter(None, [
            f"{_day_label(started.date(), ctx.today)} {started:%H:%M}" if started else "",
            _short_duration(seconds) if seconds >= 1 else "",
        ]))
    elif edited:
        day = _day_label(edited.date(), ctx.today)
        if day in ("Today", "Yesterday"):
            day = day.lower()
        when = f"Edited {day} {edited:%H:%M}"
    course = ctx.courses.get(note.course_id or "")
    return {
        "kind": "note", "icon": "note", "chip": course.name if course else ("Voice memo" if sess else "Note"),
        "cc": ctx.cc(note.course_id), "when": when,
        "status": "", "sc": "", "title": note.title or "Untitled note",
        "gist": _first_sentence(first, 140), "filed": [],
        "url": f"/notes?note_id={note.id}",
        "sort": (sess.started_at if sess else note.updated_at) or "",
    }


def _recent(ctx: _Ctx) -> list[dict[str, Any]]:
    sessions = [
        s for s in ctx.store.list_sessions()
        if s.status != storage.STATUS_RECEIVING
        # Voice memos show as their notes.
        and (s.kind in ("lecture", "meeting") or (s.kind is None and s.status in PROCESSING))
    ][:RECENT_LIMIT]
    notes, _ = current_app_notes_service().list(limit=RECENT_LIMIT)
    items = [_recording_item(ctx, s) for s in sessions] + [_note_item(ctx, n) for n in notes]
    return sorted(items, key=lambda i: i["sort"], reverse=True)[:RECENT_LIMIT]


# --- deadlines and to-dos ---------------------------------------------------

def _said_in(ctx: _Ctx, task) -> dict[str, str] | None:
    if not task.source_ref:
        return None
    sess = ctx.store.get_session(task.source_ref)
    if sess is None:
        return None
    return {
        "label": f"Said in {rec_title(sess)} · {_clock(task.source_offset_ms)}",
        "url": recording_url(sess, t=task.source_offset_ms or 0),
    }


def _due_label(due: _dt.datetime, today: _dt.date, with_time: bool = True) -> str:
    label = _day_label(due.date(), today)
    if with_time and (due.hour, due.minute) != (0, 0):
        label += f", {due:%H:%M}"
    return label


def _deadlines(ctx: _Ctx) -> tuple[list[dict[str, Any]], int]:
    tasks, total = current_app_tasks_service().list(kind="deadline", status="open", limit=100)
    views = []
    for t in tasks:
        due = timeutil.to_local(t.due_at)
        if due is None:
            continue
        days = (due.date() - ctx.today).days
        views.append({
            "id": t.id, "title": t.title, "days": days,
            "days_label": "late" if days < 0 else ("today" if days == 0 else ("day" if days == 1 else "days")),
            "days_num": abs(days) if days else "",
            "due": _due_label(due, ctx.today),
            "course": ctx.course_name(t.course_id), "cc": ctx.cc(t.course_id),
            "said_in": _said_in(ctx, t), "sort": t.due_at,
        })
    views.sort(key=lambda v: v["sort"])
    return views, total


_PRIO = {"high": 0, "urgent": 0, "medium": 1, "low": 2}


def _todos(ctx: _Ctx) -> tuple[list[dict[str, Any]], int]:
    svc = current_app_tasks_service()
    open_tasks, open_total = svc.list(kind="task", status="open", limit=100)
    done, _ = svc.list(kind="task", status="done", limit=20)
    done_today = [t for t in done if t.completed_at and (timeutil.to_local(t.completed_at) or ctx.now).date() == ctx.today]

    def view(t) -> dict[str, Any]:
        due = timeutil.to_local(t.due_at)
        late = due is not None and t.status == "open" and (
            due.date() < ctx.today if (due.hour, due.minute) == (0, 0) else due < ctx.now
        )
        prio = "high" if t.priority in ("high", "urgent") else (t.priority or "medium")
        return {
            "id": t.id, "title": t.title, "done": t.status == "done",
            "prio": prio, "prio_label": prio.capitalize(),
            "due": (("Late · " if late else "") + _due_label(due, ctx.today)) if due else "",
            "due_cls": "is-late" if late else ("is-today" if due and due.date() == ctx.today else ""),
            "said_in": _said_in(ctx, t),
            "sort": (0 if late else 1, _PRIO.get(t.priority, 1), t.due_at or "9999"),
        }

    views = sorted((view(t) for t in open_tasks), key=lambda v: v["sort"])
    return views + [view(t) for t in done_today], open_total


def home_context(week_offset: int = 0) -> dict[str, Any]:
    ctx = _Ctx()
    deadlines, deadline_total = _deadlines(ctx)
    todos, open_todos = _todos(ctx)
    processing = sum(1 for s in ctx.store.list_sessions() if s.status in PROCESSING)
    return {
        "now": ctx.now,
        "date_label": f"{ctx.now:%A} {ctx.now.day} {ctx.now:%B}",
        "schedule": _schedule(ctx, week_offset),
        "next_up": _next_up(ctx),
        "recent": _recent(ctx),
        "deadlines": deadlines, "deadline_total": deadline_total,
        "todos": todos, "open_todos": open_todos,
        "processing": processing,
    }
