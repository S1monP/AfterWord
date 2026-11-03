"""Notes service: independent of recordings, markdown-rendered.

The service owns three concerns the storage layer doesn't:

* Input validation (title length, body size, control characters).
* Markdown rendering and HTML sanitisation: ``markdown`` renders,
  then a ``bleach`` allowlist cleans the HTML, so a ``<script>`` in a
  note never runs.
* Synchronising ``body_html`` when ``body_md`` changes. The storage
  layer keeps the two fields side-by-side so list views don't have
  to render on every read.

The service is constructed with a :class:`storage.Storage` instance
so it can be tested without a Flask app. Routes get the service via
``app.current_app_notes_service``.
"""
from __future__ import annotations

import re
import uuid

import bleach
import markdown as _md

import storage
from storage import Note

# --- Limits -----------------------------------------------------------------

MAX_TITLE_LEN = 200
MAX_BODY_MD_LEN = 200_000  # 200 KB of markdown; generous for a personal app
MAX_SEARCH_LEN = 200

VALID_SOURCES = ("manual", "extracted", "imported", "voice_memo")


# Reject control characters except common whitespace (\t, \n, \r).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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


def _validate_body(body_md: object) -> str:
    if body_md is None:
        return ""
    if not isinstance(body_md, str):
        raise ValueError("body_md must be a string")
    if len(body_md) > MAX_BODY_MD_LEN:
        raise ValueError(f"body_md must be at most {MAX_BODY_MD_LEN} chars")
    if _CONTROL_CHARS.search(body_md):
        raise ValueError("body_md contains control characters")
    return body_md


def _validate_source(source: object) -> str:
    if source is None:
        return "manual"
    if not isinstance(source, str):
        raise ValueError("source must be a string")
    if source not in VALID_SOURCES:
        raise ValueError(
            f"source must be one of {VALID_SOURCES}; got {source!r}"
        )
    return source


def _validate_source_ref(source_ref: object) -> str | None:
    if source_ref is None or source_ref == "":
        return None
    if not isinstance(source_ref, str):
        raise ValueError("source_ref must be a string")
    if len(source_ref) > 256:
        raise ValueError("source_ref must be at most 256 chars")
    return source_ref


# --- Markdown rendering -----------------------------------------------------

# ``markdown.Markdown`` with no extensions: renders core markdown
# (headings, paragraphs, lists, links, code, blockquotes, emphasis).
# Raw HTML in the source is NOT escaped by the markdown library in
# 3.x — we have to sanitise the rendered output ourselves with bleach.
# The allowlist covers everything the markdown library can emit, so
# legitimate content passes through unchanged.
_md_renderer = _md.Markdown()

_ALLOWED_TAGS: list[str] = [
    "a", "abbr", "acronym", "b", "blockquote", "br", "code", "em",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li",
    "ol", "p", "pre", "s", "strong", "sub", "sup", "u", "ul",
]
_ALLOWED_ATTRS: dict[str, list[str]] = {
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
}
_ALLOWED_PROTOCOLS: list[str] = ["http", "https", "mailto"]


def render_html(body_md: str) -> str:
    """Render markdown to safe HTML.

    The output is suitable for direct insertion into a template.
    Raw HTML in the source is sanitised: tags not in the allowlist
    are escaped, dangerous protocols (e.g. ``javascript:``) are
    stripped, and unsafe attributes are removed.
    """
    _md_renderer.reset()
    raw = _md_renderer.convert(body_md or "")
    return bleach.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=False,
    )


# --- Service ----------------------------------------------------------------


class NotesService:
    """Coordinates validation, rendering, and storage for notes."""

    def __init__(self, store: storage.Storage) -> None:
        self._store = store

    def create(
        self,
        *,
        title: str,
        body_md: str = "",
        source: str = "manual",
        source_ref: str | None = None,
        course_id: str | None = None,
        created_at: str | None = None,
    ) -> Note:
        title = _validate_title(title)
        body_md = _validate_body(body_md)
        source = _validate_source(source)
        source_ref = _validate_source_ref(source_ref)
        course_id = self._validate_course(course_id)
        body_html = render_html(body_md)
        note_id = uuid.uuid4().hex
        return self._store.create_note(
            note_id,
            title=title,
            body_md=body_md,
            body_html=body_html,
            source=source,
            source_ref=source_ref,
            course_id=course_id,
            created_at=created_at,
        )

    def _validate_course(self, course_id: object) -> str | None:
        if course_id is None or course_id == "":
            return None
        if not isinstance(course_id, str) or self._store.get_course(course_id) is None:
            raise ValueError("course_id is not a known course")
        return course_id

    def update(
        self,
        note_id: str,
        *,
        title: str | None = None,
        body_md: str | None = None,
        pinned: bool | None = None,
        course_id: object = storage._UNSET,
    ) -> Note:
        if not isinstance(note_id, str) or not note_id:
            raise ValueError("note_id is required")
        if self._store.get_note(note_id) is None:
            raise KeyError(note_id)
        new_title: str | None = None
        new_body: str | None = None
        new_body_html: str | None = None
        if title is not None:
            new_title = _validate_title(title)
        if body_md is not None:
            new_body = _validate_body(body_md)
            new_body_html = render_html(new_body)
        if course_id is not storage._UNSET:
            course_id = self._validate_course(course_id)
        return self._store.update_note(
            note_id,
            title=new_title,
            body_md=new_body,
            body_html=new_body_html,
            pinned=pinned,
            course_id=course_id,
        )

    def delete(self, note_id: str) -> bool:
        if not isinstance(note_id, str) or not note_id:
            raise ValueError("note_id is required")
        return self._store.delete_note(note_id)

    def get(self, note_id: str) -> Note | None:
        if not isinstance(note_id, str) or not note_id:
            return None
        return self._store.get_note(note_id)

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
    ) -> tuple[list[Note], int]:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive int")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative int")
        if search is not None:
            search = search.strip()
            if not search:
                search = None
            elif len(search) > MAX_SEARCH_LEN:
                raise ValueError(f"search must be at most {MAX_SEARCH_LEN} chars")
        return self._store.list_notes(limit=limit, offset=offset, search=search)


def memo_text(transcript: str) -> str:
    """A voice memo's transcript as the start of its note: the spoken
    lines without timestamps or speaker labels, one paragraph each."""
    from services.llm import parse_transcript_lines
    lines = [line["text"].strip() for line in parse_transcript_lines(transcript or "")]
    return "\n\n".join(line for line in lines if line)


def ensure_memo_note(store: storage.Storage, session_id: str) -> Note | None:
    """Give a finished voice memo its note (once). The note's text starts
    as the transcript; its title is the recording's name."""
    sess = store.get_session(session_id)
    if sess is None or sess.kind != "voice_memo":
        return None
    existing = store.note_for_recording(session_id)
    if existing is not None:
        return existing
    transcript = store.get_artifact(session_id, "transcript") or {}
    named = sess.title if sess.title_source else ""
    return NotesService(store).create(
        title=(named or "Voice memo")[:MAX_TITLE_LEN],
        body_md=memo_text(transcript.get("content") or "")[:MAX_BODY_MD_LEN],
        source="voice_memo", source_ref=session_id,
        course_id=sess.course_id if sess.course_id and store.get_course(sess.course_id) else None,
        created_at=sess.started_at,
    )


def sync_memo_notes(store: storage.Storage) -> int:
    """Give every finished voice memo that has none its note (on start)."""
    made = 0
    for sess in store.list_sessions():
        if sess.kind == "voice_memo" and sess.status == storage.STATUS_DONE \
                and store.note_for_recording(sess.id) is None:
            if ensure_memo_note(store, sess.id) is not None:
                made += 1
    return made
