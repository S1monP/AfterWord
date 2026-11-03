"""Search for the dashboard's search box (static/js/search.js): the
matches, grouped, with where each one opens."""
from __future__ import annotations

import re
from typing import Any

from flask import Blueprint, jsonify, request, url_for

import search
import timeutil
from web.common import (
    _KIND_LABEL,
    course_cls,
    current_app_storage,
    rec_title,
    recording_url,
)

bp = Blueprint("search_api", __name__)

# Items per group in one response; the box shows a few, then "Show all".
_PER_GROUP = 30
# Moments listed under a recording.
_MOMENTS = 8

_GROUP_LABELS = {
    "recording": "Recordings",
    "note": "Notes",
    "task": "To-dos and deadlines",
    "event": "Calendar",
}


@bp.get("/api/v1/search")
def api_search() -> Any:
    """``?q=`` -> ``{"query", "groups": [{"type", "label", "total",
    "items": [{"title", "meta", "snippet", "at", "url", "moments",
    "more", "icon", "cc"}]}]}``. ``title`` and ``snippet`` are
    ``[[text, matched], ...]`` so the page can mark the matched words;
    ``icon`` names the type (lecture, meeting, note, other, todo,
    deadline, event) and ``cc`` is the course colour class."""
    raw = request.args.get("q", "")
    query = search.parse_query(raw)
    store = current_app_storage()
    groups = []
    for item_type, hits in search.search(store, query).items():
        items = []
        for hit in hits[:_PER_GROUP]:
            item = _ITEM_BUILDERS[item_type](store, hit, query, raw.strip())
            if item is not None:
                items.append(item)
        if items:
            groups.append({
                "type": item_type,
                "label": _GROUP_LABELS[item_type],
                "total": len(hits),
                "items": items,
            })
    return jsonify({"query": raw, "groups": groups})


def _when(iso: str | None, fmt: str = "%a %d %b %Y, %H:%M") -> str:
    dt = timeutil.to_local(iso)
    return dt.strftime(fmt) if dt else ""


def _due(iso: str | None) -> str:
    """A due date: "Tue 03 Nov 2026", with ", 23:59" unless it is all day."""
    dt = timeutil.to_local(iso)
    if dt is None:
        return ""
    return dt.strftime("%a %d %b %Y" if (dt.hour, dt.minute) == (0, 0) else "%a %d %b %Y, %H:%M")


def _clock(ms: int) -> str:
    """754000 -> "12:34"; past an hour "1:02:03"."""
    h, rem = divmod(ms // 1000, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _course(store, course_id: str | None):
    return store.get_course(course_id) if course_id else None


_KIND_ICON = {"lecture": "lecture", "meeting": "meeting", "voice_memo": "note"}


_MD_LINE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\[[ xX]\]\s+|\d+[.)]\s+|>\s*)")
_MD_INLINE = re.compile(r"\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)|`")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _plain(line: str) -> str:
    """A note's Markdown line as it reads: no list marks, checkboxes,
    heading hashes, emphasis or link targets."""
    return _MD_INLINE.sub("", _MD_LINK.sub(r"\1", _MD_LINE.sub("", line))).strip()


def _snippet(hit: search.Hit, query: search.Query, markdown: bool = False) -> list | None:
    if not hit.best:
        return None
    text = _plain(hit.best["text"]) if markdown else hit.best["text"]
    return search.snippet(text, query)


def _recording(store, hit, query, raw) -> dict | None:
    sess = store.get_session(hit.item_id)
    if sess is None:
        return None
    def url(at_ms: int | None = None) -> str:
        args: dict[str, Any] = {"hl": raw}
        if at_ms is not None:
            args["t"] = at_ms
        return recording_url(sess, **args)

    best_at = hit.best["at_ms"] if hit.best else None
    course = _course(store, sess.course_id)
    meta = " · ".join(filter(None, [
        _KIND_LABEL.get(sess.kind or "", "Recording"),
        course.name if course else "",
    ]))
    when = _when(sess.started_at)
    moments = hit.moments if len(hit.moments) > 1 else []
    return {
        "title": search.highlight(rec_title(sess), query),
        "meta": f"{meta} · {when}" if when else meta,
        "snippet": _snippet(hit, query),
        "at": _clock(best_at) if best_at is not None else None,
        "url": url(best_at),
        "moments": [{"label": _clock(ms), "url": url(ms)}
                    for ms in moments[:_MOMENTS]],
        "more": max(0, len(moments) - _MOMENTS),
        "icon": _KIND_ICON.get(sess.kind or "", "other"),
        "cc": course_cls(course.color if course else None),
    }


def _note(store, hit, query, raw) -> dict | None:
    note = store.get_note(hit.item_id)
    if note is None:
        return None
    course = _course(store, note.course_id)
    meta = " · ".join(filter(None, [
        "Voice memo" if note.source == "voice_memo" else "Note",
        course.name if course else "",
        "edited " + _when(note.updated_at, "%d %b %Y"),
    ]))
    return {
        "title": search.highlight(note.title, query),
        "meta": meta,
        "snippet": _snippet(hit, query, markdown=True),
        # The note opens with the words marked.
        "url": url_for("pages.ui_notes", note_id=note.id, hl=raw),
        "icon": "note",
        "cc": course_cls(course.color if course else None),
    }


def _task(store, hit, query, raw) -> dict | None:
    task = store.get_task(hit.item_id)
    if task is None:
        return None
    course = _course(store, task.course_id)
    meta = " · ".join(filter(None, [
        "Deadline" if task.kind == "deadline" else "To-do",
        course.name if course else "",
        "due " + _due(task.due_at) if task.due_at else "",
        task.status if task.status != "open" else "",
    ]))
    return {
        "title": search.highlight(task.title, query),
        "meta": meta,
        "snippet": _snippet(hit, query),
        "url": url_for("pages.ui_todos") + f"#todo-{task.id}",
        "icon": "deadline" if task.kind == "deadline" else "todo",
        "cc": course_cls(course.color if course else None),
    }


def _event(store, hit, query, raw) -> dict | None:
    event = store.get_event(hit.item_id)
    if event is None:
        return None
    course = _course(store, event.course_id)
    start = timeutil.to_local(event.starts_at)
    meta = " · ".join(filter(None, [
        "Event", course.name if course else "", _when(event.starts_at), event.location or "",
    ]))
    day = start.date().isoformat() if start else None
    return {
        "title": search.highlight(event.title, query),
        "meta": meta,
        "snippet": _snippet(hit, query),
        # Its day in the calendar.
        "url": url_for("pages.ui_calendar", date=day, day=day),
        "icon": "event",
        "cc": course_cls(course.color if course else None),
    }


_ITEM_BUILDERS = {
    "recording": _recording,
    "note": _note,
    "task": _task,
    "event": _event,
}
