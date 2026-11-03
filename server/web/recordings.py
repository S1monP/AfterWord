"""The reading pages: Lectures, Meetings and Other. A list column and a
reader; on the phone the list fills the screen and a recording opens full screen.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

import storage
import timeutil
from services.llm import parse_transcript_lines
from services.processing import NO_SPEECH_DETAIL
from web.common import (
    _estimate_duration_seconds,
    _parse_summary_artifact,
    _short_duration,
    course_cls,
    current_app_courses_service,
    current_app_storage,
    is_other,
    rec_title,
    recording_url,
)
from web.home import PROCESSING

# Which recordings each page lists.
PAGES: dict[str, dict[str, Any]] = {
    "lectures": {"title": "Lectures", "noun": "lecture", "kind": "lecture",
                 "match": lambda s: s.kind == "lecture"},
    "meetings": {"title": "Meetings", "noun": "meeting", "kind": "meeting",
                 "match": lambda s: s.kind == "meeting"},
    "other": {"title": "Other", "noun": "recording", "kind": "other",
              "match": is_other},
}
SPEAKER_COLOURS = 4


def _clock(ms: int | None) -> str:
    s = (ms or 0) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _status(sess) -> tuple[str, str]:
    """(label, status class) shown on a row; empty when all is well."""
    if sess.status == storage.STATUS_RECEIVING:
        return "Uploading", "st-busy"
    if sess.status == storage.STATUS_FAILED:
        return "Failed", "st-bad"
    if sess.status in (storage.STATUS_STITCHING, storage.STATUS_READY,
                       storage.STATUS_TRANSCRIBING, storage.STATUS_TRANSCRIBED):
        return "Transcribing", "st-busy"
    if sess.status in PROCESSING:
        return "Summarising", "st-busy"
    if sess.status == storage.STATUS_DONE and sess.kind is None:
        return "Not sorted", "st-later"
    return "", ""


def _date_group(day: _dt.date, today: _dt.date) -> str:
    monday = today - _dt.timedelta(days=today.weekday())
    if day == today:
        return "Today"
    if day == today - _dt.timedelta(days=1):
        return "Yesterday"
    if day >= monday:
        return "Earlier this week"
    if day >= monday - _dt.timedelta(days=7):
        return "Last week"
    return f"{day:%B %Y}"


class Listing:
    """The rows of one reading page, with what the reader needs."""

    def __init__(self, page: str) -> None:
        self.page = page
        self.spec = PAGES[page]
        self.store = current_app_storage()
        self.today = timeutil.local_today()
        self.courses = current_app_courses_service().list()[0]
        self.course_by_id = {c.id: c for c in self.courses}
        self.sessions = [s for s in self.store.list_sessions() if self.spec["match"](s)]
        # Lecture numbers within each course, oldest first.
        self.number: dict[str, int] = {}
        by_course: dict[str, list] = {}
        for s in self.sessions:
            if s.kind == "lecture" and s.course_id:
                by_course.setdefault(s.course_id, []).append(s)
        for items in by_course.values():
            for i, s in enumerate(sorted(items, key=lambda x: x.started_at or "")):
                self.number[s.id] = i + 1

    def row(self, sess) -> dict[str, Any]:
        started = timeutil.to_local(sess.started_at)
        seconds = _estimate_duration_seconds(sess)
        if started:
            day = started.date()
            when = "Today" if day == self.today else ("Yesterday" if day == self.today - _dt.timedelta(days=1) else f"{day:%a} {day.day} {day:%b}")
        else:
            day, when = self.today, ""
        status, sc = _status(sess)
        course = self.course_by_id.get(sess.course_id or "")
        number = self.number.get(sess.id)
        return {
            "id": sess.id, "title": rec_title(sess), "kind": sess.kind or "",
            "when": " · ".join(filter(None, [when, _short_duration(seconds) if seconds >= 1 else ""])),
            "day": day, "status": status, "sc": sc,
            "course_id": sess.course_id or "", "course": course.name if course else "",
            "cc": course_cls(course.color if course else None),
            "number": f"Lecture {number}" if number else "",
            "has_audio": bool(sess.full_audio_size and sess.full_audio_size > 8),
            "url": recording_url(sess) if self.spec["match"](sess) else "",
            "sort": sess.started_at or "",
        }

    def groups(self, mode: str) -> list[dict[str, Any]]:
        rows = sorted((self.row(s) for s in self.sessions), key=lambda r: r["sort"], reverse=True)
        if self.page == "lectures" and mode == "course":
            groups = []
            for c in self.courses:
                items = [r for r in rows if r["course_id"] == c.id]
                groups.append({"key": c.id, "name": c.name, "cc": course_cls(c.color), "rows": items})
            loose = [r for r in rows if r["course_id"] not in self.course_by_id]
            if loose:
                groups.append({"key": "none", "name": "No course", "cc": "cc-0", "rows": loose})
            return groups
        groups: list[dict[str, Any]] = []
        for r in rows:
            name = _date_group(r["day"], self.today)
            if not groups or groups[-1]["name"] != name:
                groups.append({"key": name, "name": name, "cc": "", "rows": []})
            groups[-1]["rows"].append(r)
        return groups


def reader(listing: Listing, sess) -> dict[str, Any]:
    """Everything the reader shows for one recording."""
    store = listing.store
    row = listing.row(sess)
    summary = _parse_summary_artifact(store.get_artifact(sess.id, "summary"))
    transcript = store.get_artifact(sess.id, "transcript")
    lines = parse_transcript_lines((transcript or {}).get("content") or "")
    speakers: dict[str, int] = {}
    for line in lines:
        who = line.get("speaker")
        if who and who not in speakers:
            speakers[who] = len(speakers) % SPEAKER_COLOURS + 1
        line["spk"] = speakers.get(who or "", 0)
    event = store.event_for_session(sess.id)
    started = timeutil.to_local(sess.started_at)
    seconds = _estimate_duration_seconds(sess)
    meta = [
        f"{started:%a} {started.day} {started:%b %Y}, {started:%H:%M}" if started else "",
        _short_duration(seconds) if seconds >= 1 else "",
        (f"{len(speakers)} speaker{'s' if len(speakers) != 1 else ''}" if speakers else ""),
        event.location if event and event.location else "",
    ]
    filed_tasks, filed_events = store.filed_from(sess.id)
    filed = [{
        "ms": t.source_offset_ms or 0, "at": _clock(t.source_offset_ms),
        "label": ("Deadline · " if t.kind == "deadline" else "To-do · ") + t.title,
        "done": t.status == "done",
    } for t in filed_tasks] + [{
        "ms": None, "at": "", "label": "Calendar · " + e.title, "done": False,
    } for e in filed_events]
    kind = sess.kind or "other"
    if kind == "lecture":
        chip = " · ".join(filter(None, [row["course"], row["number"]])) or "Lecture"
    else:
        chip = {"meeting": "Meeting", "voice_memo": "Voice memo"}.get(kind, "Recording")
        if row["course"]:
            chip = f"{row['course']} · {chip}"
    processing = sess.status in PROCESSING or sess.status == storage.STATUS_RECEIVING
    return {
        "sess": sess, "row": row, "chip": chip, "meta": " · ".join(filter(None, meta)),
        "summary": summary, "lines": lines, "filed": filed,
        "points_label": "Decisions" if kind == "meeting" else "Key points",
        "processing": processing,
        "failed": sess.status == storage.STATUS_FAILED,
        "no_speech": sess.status == storage.STATUS_DONE and sess.status_detail == NO_SPEECH_DETAIL,
        "note": (sess.status_detail or "").capitalize() if sess.status == storage.STATUS_DONE and not summary else "",
        "status_detail": sess.status_detail or "",
    }


def page_context(page: str, selected_id: str, mode: str) -> dict[str, Any]:
    listing = Listing(page)
    selected = None
    auto = False
    if selected_id:
        selected = listing.store.get_session(selected_id)
    if selected is None and listing.sessions:
        # Desktop opens the newest; on the phone the list shows instead.
        selected = max(listing.sessions, key=lambda s: s.started_at or "")
        auto = True
    return {
        "page": page, "page_title": PAGES[page]["title"], "noun": PAGES[page]["noun"],
        "kind": PAGES[page]["kind"],
        "groups": listing.groups(mode), "mode": mode, "total": len(listing.sessions),
        "reader": reader(listing, selected) if selected else None,
        "selected_id": selected.id if selected else "",
        "auto_selected": auto,
        "courses": listing.courses,
    }
