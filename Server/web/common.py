"""Helpers shared by the web modules: app-object accessors, template
filters, display helpers and upload title helpers.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from flask import current_app, Flask, g, has_request_context, request, url_for
from markupsafe import escape, Markup

import services
import services.notes  # noqa: F401
import services.tasks  # noqa: F401
import services.events  # noqa: F401
import services.classification  # noqa: F401
import services.courses  # noqa: F401
import services.llm  # noqa: F401
import search
import storage
import timeutil
from services import transcode


def current_app_storage() -> storage.Storage:
    """Pull the Storage instance off the active Flask app."""
    return current_app.config["LR_STORAGE"]


def current_app_notes_service() -> services.notes.NotesService:
    """Pull the NotesService off the active Flask app."""
    return current_app.config["LR_NOTES_SERVICE"]


def current_app_tasks_service() -> services.tasks.TasksService:
    """Pull the TasksService off the active Flask app."""
    return current_app.config["LR_TASKS_SERVICE"]


def current_app_events_service() -> services.events.EventsService:
    """Pull the EventsService off the active Flask app."""
    return current_app.config["LR_EVENTS_SERVICE"]


def current_app_classification_service() -> (
    services.classification.ClassificationService
):
    """Pull the ClassificationService off the active Flask app."""
    return current_app.config["LR_CLASSIFICATION_SERVICE"]


def current_app_courses_service() -> services.courses.CoursesService:
    """Pull the CoursesService off the active Flask app."""
    return current_app.config["LR_COURSES_SERVICE"]


def current_app_llm_providers() -> dict[str, services.llm.LLMProvider]:
    """Return the LLM provider registry attached to the app.

    Keys are ``"transcript"`` and ``"text"``; values are provider
    instances (real or no-op depending on env keys).
    """
    return current_app.config["LR_LLM_PROVIDERS"]


def current_app_transcript_provider() -> services.llm.LLMProvider:
    """Pull the transcription provider (OpenRouter or no-op)."""
    return current_app.config["LR_LLM_PROVIDERS"]["transcript"]


def current_app_text_provider() -> services.llm.LLMProvider:
    """Pull the text-model provider (OpenRouter chat or no-op)."""
    return current_app.config["LR_LLM_PROVIDERS"]["text"]


# ---------------------------------------------------------------------------
# Jinja filters
# ---------------------------------------------------------------------------

def _parse_summary_artifact(artifact: Optional[dict]) -> Optional[dict]:
    """Split a summary artifact into display fields.

    The pipeline stores the summary as a JSON payload
    ``{"text": ..., "sections": [...], "key_points": [...]}``. This
    returns a copy of the artifact dict with ``content`` replaced by
    the overview text, plus ``sections`` (``{"heading", "text"}``
    dicts; absent in older summaries) and ``key_points`` lists. If the content
    isn't parseable JSON (legacy/plain payloads), the artifact is
    returned unchanged with no key points.
    """
    if not artifact or not artifact.get("content"):
        return None
    try:
        payload = json.loads(artifact["content"])
    except (TypeError, ValueError):
        return artifact
    if not isinstance(payload, dict) or not isinstance(
        payload.get("text"), str,
    ):
        return artifact
    out = dict(artifact)
    out["content"] = payload["text"]
    raw_points = payload.get("key_points")
    out["key_points"] = (
        [str(p) for p in raw_points if str(p).strip()]
        if isinstance(raw_points, list)
        else []
    )
    raw_sections = payload.get("sections")
    out["sections"] = [
        {"heading": str(sec.get("heading") or ""),
         "text": str(sec.get("text"))}
        for sec in (raw_sections if isinstance(raw_sections, list) else [])
        if isinstance(sec, dict) and str(sec.get("text") or "").strip()
    ]
    return out


def _selected_session_context(session_id: str) -> dict[str, Any]:
    """Build the right-pane context dict for a session id.

    Used by the lectures/meetings templates to render the right
    pane (audio + summary + transcript). Returns the empty-shape
    dict if the session doesn't exist.
    """
    store = current_app_storage()
    sess = store.get_session(session_id)
    if sess is None:
        return {
            "selected_session": None,
            "summary": None,
            "transcript": None,
        }
    summary = _parse_summary_artifact(
        store.get_artifact(session_id, "summary"),
    )
    transcript = store.get_artifact(session_id, "transcript")
    filed_tasks, filed_events = store.filed_from(session_id)
    return {
        "selected_session": sess,
        "summary": summary,
        "transcript": transcript,
        "filed_tasks": filed_tasks,
        "filed_events": filed_events,
    }


def register_filters(app: Flask) -> None:
    @app.template_filter("human_size")
    def _human_size(num: Optional[int]) -> str:
        if not num:
            return "0 B"
        size = float(num)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @app.template_filter("human_duration")
    def _human_duration(seconds: Optional[float]) -> str:
        if not seconds or seconds < 0:
            return "0:00"
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"

    # Stored times are UTC; pages show local time (timeutil).
    @app.template_filter("iso_local")
    def _iso_local(iso: Optional[str]) -> str:
        dt = timeutil.to_local(iso)
        if dt is None:
            return iso or "\u2014"
        return dt.strftime("%Y-%m-%d %H:%M")

    @app.template_filter("local_date")
    def _local_date(iso: Optional[str]) -> str:
        dt = timeutil.to_local(iso)
        return dt.strftime("%Y-%m-%d") if dt else (iso or "")[:10]

    @app.template_filter("local_time")
    def _local_time(iso: Optional[str]) -> str:
        dt = timeutil.to_local(iso)
        return dt.strftime("%H:%M") if dt else (iso or "")[11:16]

    app.template_filter("rec_title")(rec_title)
    app.template_filter("rec_when")(rec_when)
    app.add_template_global(recording_url)

    # A page opened from a search result (?hl=<query>) marks the
    # matched words, in every word form (search.highlight).
    @app.template_filter("search_marks")
    def _search_marks(text: Optional[str]) -> Any:
        raw = request.args.get("hl", "") if has_request_context() else ""
        if not raw or not text:
            return text
        if "lr_hl_query" not in g:
            g.lr_hl_query = search.parse_query(raw)
        return Markup("").join(
            Markup("<mark>%s</mark>") % part if matched else escape(part)
            for part, matched in search.highlight(text, g.lr_hl_query)
        )

    # Transcript lines for display: {"t", "stamp", "speaker", "text"}.
    app.template_filter("transcript_lines")(
        services.llm.parse_transcript_lines,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Name shown for a recording that has no real title yet.
_KIND_LABEL = {
    "lecture": "Lecture",
    "meeting": "Meeting",
    "voice_memo": "Voice memo",
    "command": "Request",
    "other": "Recording",
}


# The page each kind of recording is read on, and the query parameter
# that opens one there. Everything else (kind 'other', no kind yet,
# still being processed) is read on Other.
KIND_PAGES = {
    "lecture": ("pages.ui_lectures", "session"),
    "meeting": ("pages.ui_meetings", "session"),
    "voice_memo": ("pages.ui_notes", "memo"),
}


def is_other(sess: storage.Session) -> bool:
    """Read on the Other page: not a lecture, meeting or voice memo."""
    return sess.kind not in KIND_PAGES


def recording_url(
    sess: "storage.Session | str | None", **args: Any,
) -> Optional[str]:
    """Where a recording opens: its page, with it selected. ``sess``
    may be a session or its id; ``args`` go on the URL (``t`` in ms,
    ``hl`` for search marks). None for an unknown recording."""
    if isinstance(sess, str):
        sess = current_app_storage().get_session(sess)
    if sess is None:
        return None
    endpoint, param = KIND_PAGES.get(
        sess.kind or "", ("pages.ui_other", "session"),
    )
    return url_for(endpoint, **{param: sess.id}, **args)


def rec_title(sess: storage.Session) -> str:
    """A recording's name: the stored title, except the device's folder
    name, which shows as the kind ("Meeting") until the AI names it."""
    if sess.title and not (
        sess.title_source is None and storage.title_is_placeholder(sess)
    ):
        return sess.title
    return _KIND_LABEL.get(sess.kind or "", "Recording")


def rec_when(sess: storage.Session) -> str:
    """The line under a recording's name: "Fri 30 Oct 2026, 13:10 · 11 min"."""
    dt = timeutil.to_local(sess.started_at)
    when = dt.strftime("%a %d %b %Y, %H:%M") if dt else ""
    seconds = _estimate_duration_seconds(sess)
    if seconds >= 1:
        when += " · " + _short_duration(seconds)
    return when


def _short_duration(seconds: float) -> str:
    """45 s, 11 min, 1 h 52 min."""
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    h, m = divmod(round(s / 60), 60)
    return f"{h} h {m} min" if h else f"{m} min"

_STATUS_LABEL = {
    storage.STATUS_RECEIVING: "Receiving",
    storage.STATUS_STITCHING: "Stitching",
    storage.STATUS_READY: "Ready",
    storage.STATUS_TRANSCRIBING: "Transcribing",
    storage.STATUS_TRANSCRIBED: "Transcribed",
    storage.STATUS_CLASSIFYING: "Classifying",
    storage.STATUS_SUMMARISING: "Summarising",
    storage.STATUS_EXTRACTING: "Extracting",
    storage.STATUS_DONE: "Done",
    storage.STATUS_FAILED: "Failed",
}


def _estimate_duration_seconds(session: storage.Session) -> float:
    """Duration of a session's stitched audio via ffprobe.

    Results are cached per (session id, file size) — the stitched
    file never changes, so the probe runs once per file per process
    instead of once per list render.
    """
    if not session.full_audio_size or session.full_audio_size <= 8:
        return 0.0
    cache = _DURATION_CACHE
    key = (session.id, session.full_audio_size)
    cached = cache.get(key)
    if cached is not None:
        return cached
    full = current_app_storage().full_audio_path(session.id)
    if not full.exists():
        return 0.0
    cfg = current_app.config["LR_CONFIG"]
    duration = transcode.probe_duration(full, ffmpeg_path=cfg.ffmpeg_path)
    if duration is None:
        return 0.0
    cache[key] = duration
    return duration


# Bounded just in case; keyed by immutable (id, size) so stale
# entries are harmless.
_DURATION_CACHE: dict[tuple[str, int], float] = {}


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"^[\w\s\-.,()\[\]!&'\"]+$", re.UNICODE)
_TITLE_MAX = 200


def _normalise_title(raw: str) -> Optional[str]:
    """Return a clean title, or None if it was empty / invalid."""
    title = raw.strip()
    if not title or len(title) > _TITLE_MAX:
        return None
    if not _TITLE_RE.match(title):
        return None
    return "".join(c for c in title if c.isprintable() and c not in "\n\r\t")


def _default_title_from_iso(iso_ts: str) -> str:
    """'2026-07-03T14:30:00Z' -> 'rec_2026-07-03T1530': local time, like
    the device's folder names."""
    local = timeutil.to_local(iso_ts)
    if local is not None:
        return local.strftime("rec_%Y-%m-%dT%H%M")
    core = iso_ts.replace("Z", "").replace("z", "")
    if "T" in core:
        date, time_str = core.split("T", 1)
    else:
        date, time_str = core, "00:00"
    if len(time_str) >= 5 and time_str[2] == ":":
        time_str = time_str[:5].replace(":", "")
    else:
        time_str = "0000"
    return f"rec_{date}T{time_str}"


def _file_mtime_iso(path: Path) -> Optional[str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
