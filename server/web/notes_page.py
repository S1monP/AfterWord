"""The Notes page.

Notes and voice memos are one thing: a voice memo is a note with its
recording attached (source 'voice_memo', source_ref = the recording).
One list, and one live editor; a note with audio also has a player and
"What was said", the original transcript.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

import timeutil
from services.llm import parse_transcript_lines
from web.common import (
    _estimate_duration_seconds,
    course_cls,
    current_app_courses_service,
    current_app_notes_service,
    current_app_storage,
)

UNTITLED = "Untitled note"


def _clock(seconds: float) -> str:
    s = int(round(seconds))
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _day(dt: Optional[_dt.datetime], today: _dt.date) -> str:
    if dt is None:
        return ""
    if dt.date() == today:
        return f"Today {dt:%H:%M}"
    if dt.date() == today - _dt.timedelta(days=1):
        return f"Yesterday {dt:%H:%M}"
    return f"{dt:%a} {dt.day} {dt:%b}"


def _first_line(body_md: str) -> str:
    for line in (body_md or "").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text.lstrip("-*[] x").strip()
    return ""


class NotesList:
    def __init__(self) -> None:
        self.store = current_app_storage()
        self.today = timeutil.local_today()
        self.notes, _ = current_app_notes_service().list(limit=500)
        self.courses = current_app_courses_service().list()[0]
        self.course_by_id = {c.id: c for c in self.courses}

    def recording(self, note):
        if note.source == "voice_memo" and note.source_ref:
            return self.store.get_session(note.source_ref)
        return None

    def row(self, note) -> dict[str, Any]:
        sess = self.recording(note)
        seconds = _estimate_duration_seconds(sess) if sess else 0
        course = self.course_by_id.get(note.course_id or "")
        return {
            "id": note.id, "title": note.title or UNTITLED,
            "first": _first_line(note.body_md) or "No text yet",
            "when": _day(timeutil.to_local(note.updated_at), self.today),
            "course": course.name if course else "", "course_id": course.id if course else "",
            "cc": course_cls(course.color if course else None),
            "has_audio": sess is not None and bool(sess.full_audio_size and sess.full_audio_size > 8),
            "length": _clock(seconds) if seconds >= 1 else "",
            "recording_id": sess.id if sess else "",
        }


def editor(listing: NotesList, note) -> dict[str, Any]:
    sess = listing.recording(note)
    lines = []
    if sess is not None:
        transcript = listing.store.get_artifact(sess.id, "transcript") or {}
        lines = parse_transcript_lines(transcript.get("content") or "")
    saved = timeutil.to_local(note.updated_at)
    made = timeutil.to_local(note.created_at)
    row = listing.row(note)
    return {
        "note": note, "row": row,
        "saved": f"Saved · {saved:%H:%M}" if saved and saved.date() == listing.today else (f"Saved · {saved.day} {saved:%b}" if saved else ""),
        "made": ((f"Recorded {made:%a} {made.day} {made:%b %Y}, {made:%H:%M}") if sess else (f"Written {made:%a} {made.day} {made:%b %Y}")) if made else "",
        "recording": sess, "lines": lines,
    }


def page_context(note_id: str, new: bool) -> dict[str, Any]:
    listing = NotesList()
    selected = None
    auto = False
    if note_id:
        selected = current_app_notes_service().get(note_id)
        if selected is None:
            return {"missing": True}
    elif listing.notes and not new:
        selected = listing.notes[0]          # newest; the phone shows the list
        auto = True
    return {
        "missing": False,
        "rows": [listing.row(n) for n in listing.notes],
        "total": len(listing.notes),
        "editor": editor(listing, selected) if selected else None,
        "new": new,
        "selected_id": selected.id if selected else "",
        "auto_selected": auto,
        "courses": listing.courses,
    }
