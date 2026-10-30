"""Storage layer: SQLite metadata + filesystem audio files.

This module owns the *shape* of the data on disk. It exposes one helper
class, :class:`Storage`, that the API and services layers talk to.

Phase A responsibilities:

* Maintain the SQLite schema (sessions, chunks, plus the future-use
  courses / artifacts / artifacts_fts tables that are created empty).
* Write each uploaded chunk atomically: ``chunks/NNN.aac.tmp`` then rename.
* List and read stitched audio (``full.aac``).
* Stitch chunks into a single ``full.aac`` and remove the originals.

Concurrency model:

* SQLite is opened in WAL mode; a per-thread connection is held on
  :class:`threading.local` for the lifetime of the request.
* A per-session ``threading.Lock`` is taken for the duration of any
  write that touches the session's directory, so a retry-in-flight and a
  new chunk cannot interleave on the same session.
"""
from __future__ import annotations

import os
import re
import sqlite3
import struct
import threading
import time
import uuid
import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Union

import timeutil
from config import Config


# Status values describing the server-side lifecycle. Phase A uses
# receiving / stitching / ready / failed. Later phases will introduce
# transcribing, summarising, etc., into the same column.
STATUS_RECEIVING = "receiving"
STATUS_STITCHING = "stitching"
STATUS_READY = "ready"
# Milestone 8: lifecycle states for the post-finish pipeline. The
# session goes from ``ready`` to ``transcribing`` → ``transcribed`` →
# ``classifying`` → ``done``. Any error short-circuits to ``failed``
# with a useful ``status_detail``.
STATUS_TRANSCRIBING = "transcribing"
STATUS_TRANSCRIBED = "transcribed"
STATUS_CLASSIFYING = "classifying"
# Milestone 9: after classification the pipeline produces a
# summary and extracts structured items (deadlines, key points,
# topics). The session is fully LLM-processed at ``done``.
STATUS_SUMMARISING = "summarising"
STATUS_EXTRACTING = "extracting"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

ALL_STATUSES: tuple[str, ...] = (
    STATUS_RECEIVING, STATUS_STITCHING, STATUS_READY,
    STATUS_TRANSCRIBING, STATUS_TRANSCRIBED, STATUS_CLASSIFYING,
    STATUS_SUMMARISING, STATUS_EXTRACTING,
    STATUS_DONE, STATUS_FAILED,
)

# Allowed status transitions enforced by the pipeline. ``ready`` is
# the entry point; ``done`` and ``failed`` are terminal. Any other
# transition (e.g. ``transcribing`` → ``classifying``) is treated as
# an internal accident and rejected with a clear ValueError.
#
# Keyed by current status; values are the set of legal next statuses.
ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_RECEIVING: frozenset({STATUS_STITCHING, STATUS_FAILED}),
    STATUS_STITCHING: frozenset({STATUS_READY, STATUS_FAILED}),
    STATUS_READY: frozenset({
        STATUS_TRANSCRIBING, STATUS_FAILED,
    }),
    # ``done`` straight after transcribing: no speech in the recording.
    STATUS_TRANSCRIBING: frozenset({
        STATUS_TRANSCRIBED, STATUS_DONE, STATUS_FAILED, STATUS_READY,
    }),
    # ``done`` after transcribed/classifying: a voice memo, which keeps
    # only its transcript (no summary or extraction).
    STATUS_TRANSCRIBED: frozenset({
        STATUS_CLASSIFYING, STATUS_SUMMARISING, STATUS_DONE,
        STATUS_FAILED, STATUS_READY,
    }),
    STATUS_CLASSIFYING: frozenset({
        STATUS_SUMMARISING, STATUS_DONE, STATUS_FAILED, STATUS_READY,
    }),
    STATUS_SUMMARISING: frozenset({
        STATUS_EXTRACTING, STATUS_FAILED, STATUS_READY,
    }),
    STATUS_EXTRACTING: frozenset({
        STATUS_DONE, STATUS_FAILED, STATUS_READY,
    }),
    STATUS_DONE: frozenset({STATUS_READY, STATUS_FAILED}),
    STATUS_FAILED: frozenset({
        STATUS_RECEIVING, STATUS_READY,
    }),
}


_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")
_CONTROL_CHARS_TITLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# The device's folder names (rec_2026-10-30T1310, rec_nt19_1) and the
# upload page's default: placeholders, not names.
_DEVICE_NAME = re.compile(r"^rec_(\d{4}-\d{2}-\d{2}T\d{4}|nt\d+)(_\d+)?$")


def title_is_placeholder(sess: "Session") -> bool:
    """True when a session has no real name yet, so the calendar or
    the AI may name it: no title, or the device's folder name."""
    if sess.title_source in ("ai", "calendar"):
        return True
    if sess.title_source == "user":
        return False
    return not sess.title or bool(_DEVICE_NAME.match(sess.title))
_CHUNK_INDEX_MIN = 1
WAV_HEADER_SIZE = 44


# Sentinel for "argument was not passed" in update methods where
# ``None`` is a legitimate value (e.g. clearing a column).
class _UnsetType:
    def __repr__(self) -> str:
        return "<UNSET>"

    def __bool__(self) -> bool:
        return False


_UNSET: Any = _UnsetType()


def now_iso() -> str:
    """UTC timestamp in ISO-8601 with a trailing Z, second resolution."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.match(session_id):
        raise ValueError("session_id must be 4-64 chars of [A-Za-z0-9_-]")


def is_valid_session_id(session_id: str) -> bool:
    try:
        validate_session_id(session_id)
        return True
    except ValueError:
        return False


def new_session_id() -> str:
    """Generate a fresh session id (device may also generate its own)."""
    return uuid.uuid4().hex


def _chunk_filename(index: int) -> str:
    """Three-digit zero-padded chunk filename, e.g. 007.aac."""
    return f"{index:03d}.aac"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS courses (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        code        TEXT,
        color       TEXT,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id              TEXT PRIMARY KEY,
        course_id       TEXT REFERENCES courses(id) ON DELETE SET NULL,
        title           TEXT,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        status          TEXT NOT NULL,
        status_detail   TEXT,
        expected_count  INTEGER NOT NULL,
        sample_rate     INTEGER NOT NULL,
        channels        INTEGER NOT NULL,
        bits_per_sample INTEGER NOT NULL,
        notes           TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_status_created ON sessions(status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_course ON sessions(course_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS chunks (
        session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        chunk_index   INTEGER NOT NULL,
        byte_size     INTEGER NOT NULL,
        sha256        TEXT,
        received_at   TEXT NOT NULL,
        PRIMARY KEY (session_id, chunk_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id          TEXT PRIMARY KEY,
        session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,
        content     TEXT NOT NULL,
        model       TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_artifacts_session_kind ON artifacts(session_id, kind)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
        artifact_id UNINDEXED,
        session_id  UNINDEXED,
        course_id   UNINDEXED,
        kind        UNINDEXED,
        body,
        tokenize = 'porter unicode61 remove_diacritics 2'
    )
    """,
    # -- Tier 1 schema additions --
    # Notes: independent of recordings. Markdown source + render cache.
    """
    CREATE TABLE IF NOT EXISTS notes (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        body_md     TEXT NOT NULL,
        body_html   TEXT,
        source      TEXT NOT NULL DEFAULT 'manual',
        source_ref  TEXT,
        pinned      INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC)",
    # Tasks: the merged table replacing todos + deadlines. A row is
    # either a personal todo (``kind = 'task'``) or a course deadline
    # (``kind = 'deadline'``). Personal todos may or may not have a
    # ``due_at``; deadlines must have one (enforced at the service
    # layer, not in the schema, to keep the schema simple). The
    # ``source_offset_ms`` + ``course_id`` columns are populated when
    # a deadline is auto-extracted from a recording.
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id               TEXT PRIMARY KEY,
        title            TEXT NOT NULL,
        body_md          TEXT,
        description      TEXT,
        priority         TEXT NOT NULL DEFAULT 'medium',
        status           TEXT NOT NULL DEFAULT 'open',
        kind             TEXT NOT NULL DEFAULT 'task',
        due_at           TEXT,
        source           TEXT NOT NULL DEFAULT 'manual',
        source_ref       TEXT,
        source_offset_ms INTEGER,
        course_id        TEXT,
        pinned           INTEGER NOT NULL DEFAULT 0,
        completed_at     TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_kind_status_due ON tasks(kind, status, due_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_pinned ON tasks(pinned) WHERE pinned = 1",
    # Events: calendar (lectures + meetings). Matched to sessions.
    # matched_session_id is the session we believe corresponds to this event;
    # match_confidence is the 0..1 score (set by the matcher).
    """
    CREATE TABLE IF NOT EXISTS events (
        id                  TEXT PRIMARY KEY,
        title               TEXT NOT NULL,
        description         TEXT,
        kind                TEXT NOT NULL,
        starts_at           TEXT NOT NULL,
        ends_at             TEXT,
        location            TEXT,
        source              TEXT NOT NULL DEFAULT 'manual',
        source_ref          TEXT,
        course_id           TEXT REFERENCES courses(id) ON DELETE SET NULL,
        matched_session_id  TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        match_confidence    REAL,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_starts ON events(starts_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, starts_at)",
)


# ALTER statements for existing tables. Run idempotently — each is
# wrapped to silently ignore "duplicate column" errors that arise when
# the schema is already up to date.
SCHEMA_ALTERS: tuple[str, ...] = (
    # sessions: recording-kind classification (Tier 1's manual override).
    "ALTER TABLE sessions ADD COLUMN kind TEXT",
    "ALTER TABLE sessions ADD COLUMN kind_confidence REAL",
    "ALTER TABLE sessions ADD COLUMN kind_set_at TEXT",
    # events: course association (calendar time-matching assigns the
    # matched event's course to the recording automatically).
    "ALTER TABLE events ADD COLUMN course_id TEXT REFERENCES courses(id)",
    # sessions: where the title came from ('calendar' | 'ai' | 'user';
    # NULL = the device's folder name, not a real title).
    "ALTER TABLE sessions ADD COLUMN title_source TEXT",
    # tasks: the merged todos + deadlines table; nothing to alter
    # because it was created at table-creation time. Future columns
    # go here.
)


# ---------------------------------------------------------------------------
# WAV helpers (pure functions; usable from tests)
# ---------------------------------------------------------------------------

def _validate_wav_header(header: bytes) -> None:
    if len(header) < 12:
        raise ValueError("WAV header too short")
    if header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")


def _parse_wav_header(header: bytes) -> tuple[int, int, int]:
    """Returns (sample_rate, channels, bits_per_sample) from the fmt chunk.

    Uses the chunk walker so it works with files that have extra chunks
    (LIST, JUNK, INFO, bext, etc.) between ``fmt `` and ``data``.
    """
    _validate_wav_header(header)
    fmt = _find_chunk(header, b"fmt ")
    if fmt is None:
        raise ValueError("WAV has no 'fmt ' chunk")
    if len(fmt) < 16:
        raise ValueError("'fmt ' chunk too short")
    channels = struct.unpack_from("<H", fmt, 2)[0]
    sample_rate = struct.unpack_from("<I", fmt, 4)[0]
    bits_per_sample = struct.unpack_from("<H", fmt, 14)[0]
    return sample_rate, channels, bits_per_sample


def _find_chunk(data: bytes, chunk_id: bytes) -> Optional[bytes]:
    """Return the payload of the first ``chunk_id`` in a RIFF stream.

    Handles arbitrary padding between chunks (RIFF chunks are
    word-aligned: each chunk's data is padded with a zero byte if its
    declared size is odd). Returns ``None`` if the chunk is not found.
    """
    if len(data) < 12:
        return None
    pos = 12  # skip RIFF header
    while pos + 8 <= len(data):
        if data[pos:pos + 4] != chunk_id:
            size = struct.unpack_from("<I", data, pos + 4)[0]
            pos += 8 + size + (size & 1)  # word-aligned
            continue
        size = struct.unpack_from("<I", data, pos + 4)[0]
        start = pos + 8
        end = start + size
        if end > len(data):
            return data[start:]  # truncated; return what we have
        return data[start:end]
    return None


def _wav_data_chunk_size(data: bytes) -> int:
    """Return the size of the ``data`` chunk's payload, or 0 if not found."""
    pos = 12
    while pos + 8 <= len(data):
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if data[pos:pos + 4] == b"data":
            return size
        pos += 8 + size + (size & 1)
    return 0


def _wav_data_offset(data: bytes) -> Optional[int]:
    """Return the byte offset of the ``data`` chunk payload, or None."""
    pos = 12
    while pos + 8 <= len(data):
        if data[pos:pos + 4] == b"data":
            return pos + 8
        size = struct.unpack_from("<I", data, pos + 4)[0]
        pos += 8 + size + (size & 1)
    return None


def _build_wav_header(
    sample_rate: int, channels: int, bits_per_sample: int, data_size: int
) -> bytes:
    """Build a canonical 44-byte PCM WAV header (little-endian)."""
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    riff_size = 36 + data_size
    header = bytearray(44)
    header[0:4] = b"RIFF"
    struct.pack_into("<I", header, 4, riff_size)
    header[8:12] = b"WAVE"
    header[12:16] = b"fmt "
    struct.pack_into("<I", header, 16, 16)
    struct.pack_into("<H", header, 20, 1)
    struct.pack_into("<H", header, 22, channels)
    struct.pack_into("<I", header, 24, sample_rate)
    struct.pack_into("<I", header, 28, byte_rate)
    struct.pack_into("<H", header, 32, block_align)
    struct.pack_into("<H", header, 34, bits_per_sample)
    header[36:40] = b"data"
    struct.pack_into("<I", header, 40, data_size)
    return bytes(header)


def make_wav_bytes(
    sample_rate: int, channels: int, bits_per_sample: int, payload: bytes
) -> bytes:
    """Wrap raw PCM ``payload`` in a fresh WAV header. Useful in tests."""
    return _build_wav_header(
        sample_rate, channels, bits_per_sample, len(payload)
    ) + payload


# ---------------------------------------------------------------------------
# In-memory view
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """In-memory view of a session row."""
    id: str
    course_id: Optional[str]
    title: Optional[str]
    started_at: str
    finished_at: Optional[str]
    status: str
    status_detail: Optional[str]
    expected_count: int
    sample_rate: int
    channels: int
    bits_per_sample: int
    notes: Optional[str]
    created_at: str
    updated_at: str
    received_chunks: int
    full_audio_size: int
    # Tier 1: recording-kind classification. 'lecture' | 'voice_memo' |
    # 'meeting' | 'other' | None (unclassified). kind_confidence is 0..1;
    # kind_set_at is when the classification was assigned (LLM or human).
    kind: Optional[str] = None
    kind_confidence: Optional[float] = None
    kind_set_at: Optional[str] = None
    # Where ``title`` came from: 'calendar' (matched event), 'ai'
    # (topic from the transcript), 'user' (renamed by hand, never
    # overwritten), or None (the device's folder name).
    title_source: Optional[str] = None

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "title": self.title,
            "title_source": self.title_source,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "status_detail": self.status_detail,
            "expected_count": self.expected_count,
            "received_chunks": self.received_chunks,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bits_per_sample": self.bits_per_sample,
            "full_audio_size": self.full_audio_size,
            "full_audio_url": (
                f"/api/v1/sessions/{self.id}/audio"
                if self.status in (
                    STATUS_READY,
                    STATUS_TRANSCRIBING,
                    STATUS_TRANSCRIBED,
                    STATUS_CLASSIFYING,
                    STATUS_SUMMARISING,
                    STATUS_EXTRACTING,
                    STATUS_DONE,
                ) and self.full_audio_size > 0
                else None
            ),
            "kind": self.kind,
            "kind_confidence": self.kind_confidence,
            "kind_set_at": self.kind_set_at,
        }


@dataclass
class Note:
    """In-memory view of a note row.

    ``body_md`` is the canonical source (the user's text). ``body_html`` is
    a pre-rendered cache kept in the row so list views don't re-render
    on every fetch. The service layer is responsible for keeping these
    two in sync; this dataclass is just a view.
    """
    id: str
    title: str
    body_md: str
    body_html: Optional[str]
    source: str
    source_ref: Optional[str]
    pinned: bool
    created_at: str
    updated_at: str

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body_md": self.body_md,
            "body_html": self.body_html,
            "source": self.source,
            "source_ref": self.source_ref,
            "pinned": self.pinned,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Task:
    """In-memory view of a tasks-table row.

    A row is either a personal todo (``kind = 'task'``) or a course
    deadline (``kind = 'deadline'``). The shape is the same; the
    difference is which fields are typically populated and which
    page renders the row.
    """
    id: str
    title: str
    body_md: Optional[str]
    description: Optional[str]
    priority: str
    status: str
    kind: str
    due_at: Optional[str]
    source: str
    source_ref: Optional[str]
    source_offset_ms: Optional[int]
    course_id: Optional[str]
    pinned: bool
    completed_at: Optional[str]
    created_at: str
    updated_at: str

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body_md": self.body_md,
            "description": self.description,
            "priority": self.priority,
            "status": self.status,
            "kind": self.kind,
            "due_at": self.due_at,
            "source": self.source,
            "source_ref": self.source_ref,
            "source_offset_ms": self.source_offset_ms,
            "course_id": self.course_id,
            "pinned": self.pinned,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Event:
    """In-memory view of an events-table row.

    Events are user-owned, not extracted by the LLM. The LLM matches
    sessions to events based on time proximity (10 minutes before
    ``starts_at`` through ``ends_at``). ``starts_at`` and ``ends_at``
    are stored as ISO-8601 strings with a trailing ``Z``; ``ends_at``
    is optional (a lecture with no declared end).
    """
    id: str
    title: str
    description: Optional[str]
    kind: str                          # 'lecture' | 'meeting' | 'other'
    starts_at: str
    ends_at: Optional[str]
    location: Optional[str]
    source: str                        # 'manual' | 'ical_import'
    source_ref: Optional[str]
    course_id: Optional[str]
    matched_session_id: Optional[str]
    match_confidence: Optional[float]
    created_at: str
    updated_at: str

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "kind": self.kind,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "location": self.location,
            "source": self.source,
            "source_ref": self.source_ref,
            "course_id": self.course_id,
            "matched_session_id": self.matched_session_id,
            "match_confidence": self.match_confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Course:
    """In-memory view of a courses-table row.

    ``color`` is a CSS hex string like ``#a3c4f3`` (or ``None``).
    ``sort_order`` is an int used to order the sidebar; lower numbers
    come first. ``created_at`` is ISO-8601 UTC.
    """
    id: str
    name: str
    code: Optional[str]
    color: Optional[str]
    sort_order: int
    created_at: str

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "color": self.color,
            "sort_order": self.sort_order,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Storage facade
# ---------------------------------------------------------------------------

class Storage:
    """High-level storage facade.

    One instance per process. All public methods are safe to call from
    multiple threads; per-session writes are serialised by an in-process
    lock keyed on the session id (sufficient for a single-process LAN
    server).
    """

    def __init__(self, config: Config):
        self._config = config
        self._data_dir = config.data_dir
        self._db_path = config.db_path
        self._sessions_dir = config.sessions_dir
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        self._tls = threading.local()

    # --- lifecycle --------------------------------------------------------

    def initialise(self) -> None:
        """Create directories and run schema migrations. Idempotent."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        (self._data_dir / "db").mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._config.inbox_dir.mkdir(parents=True, exist_ok=True)
        # Tidy any half-uploaded files left from a previous run.
        self._clean_inbox()
        with self._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for stmt in SCHEMA_STATEMENTS:
                conn.execute(stmt)
            # Idempotent column additions: swallow "duplicate column" errors.
            for stmt in SCHEMA_ALTERS:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            # The todos/deadlines -> tasks merge only runs on databases
            # that pre-date the merged schema. It's a one-shot upgrade.
            self._migrate_todos_to_tasks(conn)
            self._migrate_naive_started_at(conn)
            conn.commit()
        self._log(f"data dir: {self._data_dir}")
        self._log(f"DB ready: {self._db_path}")

    def _migrate_naive_started_at(self, conn: sqlite3.Connection) -> None:
        """Rewrite ``started_at`` stored without an offset as UTC.

        Device sessions announced before the server normalised
        ``started_at`` kept the device's local wall-clock string
        (e.g. ``2026-10-30T01:11:00``). Read it as local time, like the
        announce endpoint now does. Idempotent: normalised rows end in Z.
        """
        rows = conn.execute(
            "SELECT id, started_at FROM sessions "
            "WHERE started_at NOT LIKE '%Z'"
        ).fetchall()
        for row in rows:
            try:
                fixed = timeutil.to_utc_iso(row[1])
            except (TypeError, ValueError):
                continue
            if fixed != row[1]:
                conn.execute(
                    "UPDATE sessions SET started_at = ? WHERE id = ?",
                    (fixed, row[0]),
                )
        if rows:
            self._log(f"normalised started_at of {len(rows)} session(s) to UTC")

    def _migrate_todos_to_tasks(self, conn: sqlite3.Connection) -> None:
        """One-shot migration: copy ``todos`` rows into ``tasks`` with
        ``kind = 'task'``, then drop the old tables. Idempotent: if
        the ``todos`` table doesn't exist, this is a no-op.

        New columns on ``tasks`` (description, kind, source_offset_ms,
        course_id) get sensible defaults: NULL or the constant ``'task'``
        for kind. Existing columns pass through unchanged. The
        ``pinned`` column doesn't exist on the old ``todos`` table;
        we default it to 0.
        """
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='todos'"
        )
        if cur.fetchone() is None:
            return  # already migrated
        self._log("migrating todos -> tasks (one-shot)")
        # Copy rows. The kind is hard-coded to 'task' for migrated rows;
        # the user can edit them later if any were actually deadlines.
        conn.execute(
            """
            INSERT OR IGNORE INTO tasks
                (id, title, body_md, description, priority, status,
                 kind, due_at, source, source_ref, source_offset_ms,
                 course_id, pinned, completed_at, created_at, updated_at)
            SELECT
                id, title, body_md, NULL, priority, status,
                'task', due_at, source, source_ref, NULL,
                NULL, 0, completed_at, created_at, updated_at
            FROM todos
            """
        )
        # Drop the old tables. The deadlines table was never used, so
        # it's safe to drop without a copy.
        conn.execute("DROP TABLE IF EXISTS todos")
        conn.execute("DROP TABLE IF EXISTS idx_todos_status")
        conn.execute("DROP TABLE IF EXISTS idx_todos_priority")
        conn.execute("DROP TABLE IF EXISTS idx_todos_due")
        conn.execute("DROP TABLE IF EXISTS deadlines")
        conn.execute("DROP TABLE IF EXISTS idx_deadlines_due")
        conn.execute("DROP TABLE IF EXISTS idx_deadlines_status")
        self._log("migration complete: todos and deadlines are gone")

    def _clean_inbox(self) -> None:
        inbox = self._config.inbox_dir
        if not inbox.exists():
            return
        for entry in inbox.iterdir():
            try:
                if entry.is_file() or entry.is_symlink():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                pass

    def _log(self, msg: str) -> None:
        print(f"[LR ] {msg}", flush=True)

    # --- per-thread connection -------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            self._tls.conn = conn
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # --- per-session locks ------------------------------------------------

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    # --- paths ------------------------------------------------------------

    def session_dir(self, session_id: str) -> Path:
        return self._sessions_dir / session_id

    def chunks_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "chunks"

    def full_audio_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "full.aac"

    def chunk_path(self, session_id: str, chunk_index: int) -> Path:
        return self.chunks_dir(session_id) / _chunk_filename(chunk_index)

    # --- session CRUD -----------------------------------------------------

    def upsert_session(
        self,
        session_id: str,
        *,
        title: Optional[str],
        expected_count: int,
        sample_rate: int,
        channels: int,
        bits_per_sample: int,
        started_at: str,
    ) -> tuple[Session, bool]:
        """Create a session, or return the existing one if id is known.

        Returns ``(session, created)`` where ``created`` is True on first
        creation and False on idempotent re-announce.
        """
        validate_session_id(session_id)
        if expected_count < 0:
            raise ValueError("expected_count must be >= 0")
        if sample_rate <= 0 or channels <= 0 or bits_per_sample <= 0:
            raise ValueError("audio format fields must be positive")
        now = now_iso()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is not None:
                return self._row_to_session(conn, row), False
            conn.execute(
                """
                INSERT INTO sessions
                    (id, title, started_at, status, expected_count,
                     sample_rate, channels, bits_per_sample,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, title, started_at, STATUS_RECEIVING,
                    expected_count, sample_rate, channels,
                    bits_per_sample, now, now,
                ),
            )
        # Create the on-disk directory now so chunk PUTs can write to it.
        self.chunks_dir(session_id).mkdir(parents=True, exist_ok=True)
        self._write_meta_json(session_id)
        sess = self.get_session(session_id)
        assert sess is not None
        return sess, True

    def get_session(self, session_id: str) -> Optional[Session]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_session(conn, row)

    def list_sessions(self) -> list[Session]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
            return [self._row_to_session(conn, r) for r in rows]

    def list_unclassified_sessions(
        self, *, limit: int = 100, offset: int = 0,
    ) -> tuple[list[Session], int]:
        """List sessions that need classifying: ``kind IS NULL`` and
        ``status = 'ready'``. Newest first.

        Returns ``(sessions, total)``. ``total`` is the count of
        matches BEFORE limit/offset are applied so the UI can render
        pagination.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        with self._tx() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions "
                "WHERE kind IS NULL AND status = ?",
                (STATUS_READY,),
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM sessions "
                "WHERE kind IS NULL AND status = ? "
                "ORDER BY created_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (STATUS_READY, limit, offset),
            ).fetchall()
        return (
            [self._row_to_session(conn, r) for r in rows],
            int(total),
        )

    def _row_to_session(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> Session:
        received = conn.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE session_id = ?",
            (row["id"],),
        ).fetchone()["c"]
        full_path = self.full_audio_path(row["id"])
        full_size = full_path.stat().st_size if full_path.exists() else 0
        return Session(
            id=row["id"],
            course_id=row["course_id"],
            title=row["title"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            status_detail=row["status_detail"],
            expected_count=row["expected_count"],
            sample_rate=row["sample_rate"],
            channels=row["channels"],
            bits_per_sample=row["bits_per_sample"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            received_chunks=received,
            full_audio_size=full_size,
            kind=row["kind"] if "kind" in row.keys() else None,
            kind_confidence=row["kind_confidence"] if "kind_confidence" in row.keys() else None,
            kind_set_at=row["kind_set_at"] if "kind_set_at" in row.keys() else None,
            title_source=(
                row["title_source"] if "title_source" in row.keys() else None
            ),
        )

    def _write_meta_json(self, session_id: str) -> None:
        """Mirror the session row to ``meta.json`` for human inspection."""
        sess = self.get_session(session_id)
        if sess is None:
            return
        path = self.session_dir(session_id) / "meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": sess.id,
            "title": sess.title,
            "status": sess.status,
            "status_detail": sess.status_detail,
            "expected_count": sess.expected_count,
            "received_chunks": sess.received_chunks,
            "sample_rate": sess.sample_rate,
            "channels": sess.channels,
            "bits_per_sample": sess.bits_per_sample,
            "started_at": sess.started_at,
            "finished_at": sess.finished_at,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
        }
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2, sort_keys=True)

    def update_status(
        self,
        session_id: str,
        status: str,
        *,
        status_detail: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> None:
        if status not in ALL_STATUSES:
            raise ValueError(f"unknown status: {status}")
        now = now_iso()
        # Transition guard: refuse illegal jumps. The post-finish
        # pipeline (M8) drives ``ready → transcribing → transcribed →
        # classifying → done`` and short-circuits to ``failed`` on any
        # error. Manual re-runs can move ``done`` / ``failed`` back
        # to ``ready``.
        with self._tx() as conn:
            row = conn.execute(
                "SELECT status FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            current = row["status"]
            allowed = ALLOWED_STATUS_TRANSITIONS.get(
                current, frozenset(),
            )
            if status not in allowed and current != status:
                raise ValueError(
                    f"illegal status transition: {current} -> {status}"
                )
            conn.execute(
                """
                UPDATE sessions
                SET status = ?, status_detail = ?,
                    finished_at = COALESCE(?, finished_at),
                    updated_at = ?
                WHERE id = ?
                """,
                (status, status_detail, finished_at, now, session_id),
            )
        self._write_meta_json(session_id)

    # Convenience alias for callers that prefer the shorter name.
    # Identical to ``update_status``; included for readability in the
    # post-finish pipeline.
    def set_status(
        self,
        session_id: str,
        status: str,
        *,
        status_detail: Optional[str] = None,
    ) -> None:
        self.update_status(
            session_id, status, status_detail=status_detail,
        )

    # --- kind classification ------------------------------------------------

    # Allowed values for sessions.kind. ``None`` means "unclassified".
    RECORDING_KINDS: frozenset[str] = frozenset(
        {"lecture", "voice_memo", "meeting", "other"}
    )

    # Allowed values for events.kind. Events are user-owned calendar
    # entries (a lecture slot, a meeting slot) — there's no "unclassified"
    # equivalent; the LLM matches sessions to events, not the other way.
    EVENT_KINDS: frozenset[str] = frozenset(
        {"lecture", "meeting", "other"}
    )

    # Allowed values for events.source. ``manual`` is created in the UI,
    # ``ical_import`` is bulk-imported from a .ics file.
    EVENT_SOURCES: frozenset[str] = frozenset(
        {"manual", "ical_import"}
    )

    def set_kind(
        self,
        session_id: str,
        kind: Optional[str],
        *,
        confidence: Optional[float] = None,
    ) -> Session:
        """Set the recording's kind (or clear it with ``None``).

        The caller can supply a confidence (e.g. an LLM-assigned score).
        A user manually setting a kind passes ``confidence=1.0`` implicitly
        by leaving the default — the API layer normalises that.
        """
        if kind is not None and kind not in self.RECORDING_KINDS:
            raise ValueError(
                f"unknown kind: {kind!r}; expected one of "
                f"{sorted(self.RECORDING_KINDS)} or None"
            )
        if confidence is not None and not (0.0 <= confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {confidence}")
        # When the kind is cleared (None), force-clear kind_confidence
        # too. A row with kind=NULL and kind_confidence=1.0 is
        # meaningless and would confuse any consumer that looks at
        # confidence on its own.
        if kind is None:
            confidence = None
        now = now_iso()
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET kind = ?, kind_confidence = ?, kind_set_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (kind, confidence, now, now, session_id),
            )
        self._write_meta_json(session_id)
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(session_id)
        return sess

    # --- courses ---------------------------------------------------------

    def create_course(
        self,
        course_id: str,
        *,
        name: str,
        code: Optional[str] = None,
        color: Optional[str] = None,
        sort_order: int = 0,
    ) -> Course:
        """Insert a new course. All validation is the caller's job
        (the service layer is responsible)."""
        now = now_iso()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO courses
                    (id, name, code, color, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (course_id, name, code, color, int(sort_order), now),
            )
        return self.get_course(course_id)  # type: ignore[return-value]

    def get_course(self, course_id: str) -> Optional[Course]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM courses WHERE id = ?", (course_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_course(row)

    def update_course(
        self,
        course_id: str,
        *,
        name: Optional[str] = None,
        code: Any = _UNSET,
        color: Any = _UNSET,
        sort_order: Optional[int] = None,
    ) -> Course:
        """Partial update. ``name`` and ``sort_order`` use the simple
        None-means-don't-touch convention. ``code`` and ``color`` use
        ``_UNSET`` so callers can pass ``None`` to explicitly clear.
        """
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = ?"); params.append(name)
        if code is not _UNSET:
            sets.append("code = ?"); params.append(code)
        if color is not _UNSET:
            sets.append("color = ?"); params.append(color)
        if sort_order is not None:
            sets.append("sort_order = ?"); params.append(int(sort_order))
        if not sets:
            existing = self.get_course(course_id)
            if existing is None:
                raise KeyError(course_id)
            return existing
        params.append(course_id)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE courses SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if cur.rowcount == 0:
                raise KeyError(course_id)
        updated = self.get_course(course_id)
        assert updated is not None
        return updated

    def delete_course(self, course_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM courses WHERE id = ?", (course_id,)
            )
            return cur.rowcount > 0

    def list_courses(
        self, *, limit: int = 200, offset: int = 0,
    ) -> tuple[list[Course], int]:
        """List courses: ``(sort_order ASC, created_at ASC, id ASC)``."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        with self._tx() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM courses"
            ).fetchone()["c"]
            # Use rowid as the final tiebreaker so that courses
            # created in the same second (and therefore with the
            # same ``created_at``) still sort deterministically
            # by insertion order. SQLite assigns rowids
            # monotonically as rows are inserted, so this matches
            # the user's expectation of "first-inserted first".
            rows = conn.execute(
                """
                SELECT * FROM courses
                ORDER BY sort_order ASC, created_at ASC, rowid ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return (
            [self._row_to_course(r) for r in rows],
            int(total),
        )

    def _row_to_course(self, row: sqlite3.Row) -> Course:
        keys = row.keys()
        return Course(
            id=row["id"],
            name=row["name"],
            code=row["code"],
            color=row["color"] if "color" in keys else None,
            sort_order=row["sort_order"] if "sort_order" in keys else 0,
            created_at=row["created_at"],
        )

    def list_sessions_by_course(
        self, course_id: Optional[str], *,
        status: Union[str, list[str], None] = "ready",
        kind: Optional[str] = None,
    ) -> list[Session]:
        """List sessions under a given course (or NULL course when
        ``course_id is None``), newest first.

        Filters by ``status`` (defaults to "ready") and optionally by
        ``kind``. ``status`` may be a single value, an iterable of
        values (any of which may match), or ``None`` to skip the
        filter. Used by the /lectures and /meetings sidebars; after
        M8 the post-finish pipeline can leave a session in
        ``status='done'`` (or in transient ``transcribing``/
        ``transcribed``/``classifying`` states), so callers
        typically pass a list of "ready" + "done".

        Accepts the legacy string ``"ready"`` for backwards
        compatibility with callers written before the M8 pipeline.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if course_id is None:
            clauses.append("course_id IS NULL")
        else:
            clauses.append("course_id = ?")
            params.append(course_id)
        if status is not None:
            if isinstance(status, str):
                clauses.append("status = ?")
                params.append(status)
            else:
                # Iterable of statuses: use IN (...).
                placeholders = ",".join("?" for _ in status)
                clauses.append(f"status IN ({placeholders})")
                params.extend(status)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where_sql = "WHERE " + " AND ".join(clauses)
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT * FROM sessions {where_sql} "
                f"ORDER BY created_at DESC, id DESC",
                params,
            ).fetchall()
        return [self._row_to_session(conn, r) for r in rows]

    def set_session_course(
        self, session_id: str, course_id: Optional[str],
    ) -> Session:
        """Assign or unassign a course on a session.

        Raises ``KeyError`` if the session doesn't exist;
        ``ValueError`` if the course doesn't exist (when
        ``course_id`` is not None). The schema's
        ``ON DELETE SET NULL`` keeps dangling references clean on
        cascade delete.
        """
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(session_id)
        if course_id is not None and self.get_course(course_id) is None:
            raise ValueError(f"course {course_id!r} does not exist")
        now = now_iso()
        with self._tx() as conn:
            conn.execute(
                "UPDATE sessions SET course_id = ?, updated_at = ? "
                "WHERE id = ?",
                (course_id, now, session_id),
            )
        sess = self.get_session(session_id)
        assert sess is not None
        return sess

    TITLE_SOURCES: frozenset[str] = frozenset({"calendar", "ai", "user"})

    def set_session_title(
        self, session_id: str, title: str, *, source: Optional[str] = None,
    ) -> Session:
        """Rename a session and record where the name came from
        (``source``, one of :attr:`TITLE_SOURCES`, or None).

        Raises ``KeyError`` if the session doesn't exist;
        ``ValueError`` for empty/oversized titles or control
        characters.
        """
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(session_id)
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        title = title.strip()
        if not title:
            raise ValueError("title must not be empty")
        if len(title) > 200:
            raise ValueError("title must be at most 200 chars")
        if _CONTROL_CHARS_TITLE.search(title):
            raise ValueError("title contains control characters")
        if source is not None and source not in self.TITLE_SOURCES:
            raise ValueError(f"unknown title source: {source!r}")
        now = now_iso()
        with self._tx() as conn:
            conn.execute(
                "UPDATE sessions SET title = ?, title_source = ?, "
                "updated_at = ? WHERE id = ?",
                (title, source, now, session_id),
            )
        self._write_meta_json(session_id)
        sess = self.get_session(session_id)
        assert sess is not None
        return sess

    def count_sessions_by_course(self, course_id: str) -> int:
        """How many ready + done sessions are linked to this
        course. Shown next to the course name in the /lectures
        sidebar. After M8, finished sessions can be in
        ``status='done'`` (pipeline ran) as well as ``ready``
        (pipeline was a no-op), so we count both.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions "
                "WHERE course_id = ? AND status IN (?, ?)",
                (course_id, STATUS_READY, STATUS_DONE),
            ).fetchone()
        return int(row["c"])

    def get_artifact(
        self, session_id: str, kind: str,
    ) -> Optional[dict[str, Any]]:
        """Fetch one artifact for a session, by kind.

        Returns ``{"kind", "content", "model", "created_at"}`` or
        ``None`` if no such artifact exists yet. ``content`` may be
        large. Used by the right-pane partial to render summary
        and transcript blocks.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT kind, content, model, created_at "
                "FROM artifacts WHERE session_id = ? AND kind = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (session_id, kind),
            ).fetchone()
        if row is None:
            return None
        return {
            "kind": row["kind"],
            "content": row["content"],
            "model": row["model"],
            "created_at": row["created_at"],
        }

    def add_artifact(
        self,
        session_id: str,
        kind: str,
        content: str,
        *,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Store a session's artifact of ``kind`` and mirror it into
        the FTS table, replacing any earlier one of the same kind.

        ``kind`` is one of the artifact kinds used by the pipeline
        (``transcript``, ``summary``, ``extraction``, …). A session
        has at most one of each: a retry re-runs the pipeline, and
        keeping the old rows left duplicate search hits. The row id
        is a fresh uuid4 hex; ``created_at`` is set to now. The
        FTS table is also written so the artifact is searchable
        later (M10: search).

        Returns the row as a dict with the same shape as
        :meth:`get_artifact`. Raises :class:`KeyError` if the
        session doesn't exist.
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind is required")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        # Validate the session exists; otherwise the FK constraint
        # would surface a less helpful error from SQLite.
        if self.get_session(session_id) is None:
            raise KeyError(session_id)
        artifact_id = uuid.uuid4().hex
        now = now_iso()
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM artifacts_fts WHERE artifact_id IN "
                "(SELECT id FROM artifacts WHERE session_id = ? AND kind = ?)",
                (session_id, kind),
            )
            conn.execute(
                "DELETE FROM artifacts WHERE session_id = ? AND kind = ?",
                (session_id, kind),
            )
            conn.execute(
                """
                INSERT INTO artifacts
                    (id, session_id, kind, content, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, session_id, kind, content, model, now),
            )
            # Mirror into the FTS table so later search includes
            # this artifact. ``course_id`` is left NULL — it's only
            # useful for session-side joins; the search code can
            # resolve it via ``session_id`` if it needs to.
            conn.execute(
                """
                INSERT INTO artifacts_fts
                    (artifact_id, session_id, course_id, kind, body)
                VALUES (?, ?, NULL, ?, ?)
                """,
                (artifact_id, session_id, kind, content),
            )
        return {
            "id": artifact_id,
            "session_id": session_id,
            "kind": kind,
            "content": content,
            "model": model,
            "created_at": now,
        }

    def delete_artifacts(self, session_id: str, *kinds: str) -> None:
        """Remove a session's artifacts of the given kinds (and their
        search rows). Used when a summary no longer fits the session,
        e.g. after its kind was changed by hand."""
        placeholders = ",".join("?" for _ in kinds)
        params = (session_id, *kinds)
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM artifacts_fts WHERE artifact_id IN "
                "(SELECT id FROM artifacts WHERE session_id = ? "
                f"AND kind IN ({placeholders}))",
                params,
            )
            conn.execute(
                "DELETE FROM artifacts WHERE session_id = ? "
                f"AND kind IN ({placeholders})",
                params,
            )

    # --- notes ------------------------------------------------------------

    def create_note(
        self,
        note_id: str,
        *,
        title: str,
        body_md: str,
        body_html: Optional[str],
        source: str = "manual",
        source_ref: Optional[str] = None,
        pinned: bool = False,
    ) -> Note:
        """Insert a new note. The caller is responsible for the rendered
        ``body_html`` (the service layer renders markdown and passes
        both fields in so the row is consistent in a single insert).
        """
        now = now_iso()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO notes
                    (id, title, body_md, body_html, source, source_ref,
                     pinned, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (note_id, title, body_md, body_html, source, source_ref,
                 1 if pinned else 0, now, now),
            )
        return self.get_note(note_id)  # type: ignore[return-value]

    def get_note(self, note_id: str) -> Optional[Note]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_note(row)

    def update_note(
        self,
        note_id: str,
        *,
        title: Optional[str] = None,
        body_md: Optional[str] = None,
        body_html: Optional[str] = None,
        pinned: Optional[bool] = None,
    ) -> Note:
        """Partial update. Only fields that are not None are touched.

        If ``body_md`` is updated, ``body_html`` is expected to be the
        freshly-rendered version of the new markdown (caller's job).
        The previous ``body_html`` is replaced wholesale — we never
        try to merge renders.
        """
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if body_md is not None:
            sets.append("body_md = ?")
            params.append(body_md)
        if body_html is not None:
            sets.append("body_html = ?")
            params.append(body_html)
        if pinned is not None:
            sets.append("pinned = ?")
            params.append(1 if pinned else 0)
        if not sets:
            # No-op update: just return the existing note unchanged.
            existing = self.get_note(note_id)
            if existing is None:
                raise KeyError(note_id)
            return existing
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(note_id)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE notes SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if cur.rowcount == 0:
                raise KeyError(note_id)
        updated = self.get_note(note_id)
        assert updated is not None
        return updated

    def delete_note(self, note_id: str) -> bool:
        """Delete a note. Returns True if a row was removed, False if
        the note didn't exist."""
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            return cur.rowcount > 0

    def list_notes(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        search: Optional[str] = None,
    ) -> tuple[list[Note], int]:
        """List notes, newest first. ``search`` is a case-insensitive
        substring match against ``title`` and ``body_md``.

        Returns ``(notes, total_count)``. The total is the count of
        matches BEFORE the limit/offset is applied, so the caller can
        render pagination controls.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        where_sql = ""
        where_params: list[Any] = []
        if search:
            like = f"%{search}%"
            where_sql = "WHERE title LIKE ? OR body_md LIKE ?"
            where_params = [like, like]
        with self._tx() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM notes {where_sql}",
                where_params,
            ).fetchone()["c"]
            rows = conn.execute(
                f"""
                SELECT * FROM notes
                {where_sql}
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                where_params + [limit, offset],
            ).fetchall()
        return [self._row_to_note(r) for r in rows], int(total)

    def _row_to_note(self, row: sqlite3.Row) -> Note:
        return Note(
            id=row["id"],
            title=row["title"],
            body_md=row["body_md"],
            body_html=row["body_html"],
            source=row["source"],
            source_ref=row["source_ref"],
            pinned=bool(row["pinned"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # --- tasks ------------------------------------------------------------

    def create_task(
        self,
        task_id: str,
        *,
        title: str,
        body_md: Optional[str] = None,
        description: Optional[str] = None,
        priority: str = "medium",
        status: str = "open",
        kind: str = "task",
        due_at: Optional[str] = None,
        source: str = "manual",
        source_ref: Optional[str] = None,
        source_offset_ms: Optional[int] = None,
        course_id: Optional[str] = None,
        pinned: bool = False,
        completed_at: Optional[str] = None,
    ) -> Task:
        now = now_iso()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, title, body_md, description, priority, status,
                     kind, due_at, source, source_ref, source_offset_ms,
                     course_id, pinned, completed_at,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?)
                """,
                (task_id, title, body_md, description, priority, status,
                 kind, due_at, source, source_ref, source_offset_ms,
                 course_id, 1 if pinned else 0,
                 now if status == "done" else completed_at,
                 now, now),
            )
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_task(row)

    def update_task(
        self,
        task_id: str,
        *,
        title: Optional[str] = None,
        body_md: Any = _UNSET,
        description: Any = _UNSET,
        priority: Optional[str] = None,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        completed_at: Any = _UNSET,
        due_at: Any = _UNSET,
        source_offset_ms: Any = _UNSET,
        course_id: Any = _UNSET,
        pinned: Any = _UNSET,
    ) -> Task:
        """Partial update.

        ``body_md``, ``description``, ``completed_at``, ``due_at``,
        ``source_offset_ms``, ``course_id``, ``pinned`` use a
        sentinel so the caller can pass ``None`` to clear them
        explicitly. ``title``, ``priority``, ``status``, ``kind``
        use the simpler None-means-don't-touch convention.
        """
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if body_md is not _UNSET:
            sets.append("body_md = ?")
            params.append(body_md)
        if description is not _UNSET:
            sets.append("description = ?")
            params.append(description)
        if priority is not None:
            sets.append("priority = ?")
            params.append(priority)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if kind is not None:
            sets.append("kind = ?")
            params.append(kind)
        if completed_at is not _UNSET:
            sets.append("completed_at = ?")
            params.append(completed_at)
        if due_at is not _UNSET:
            sets.append("due_at = ?")
            params.append(due_at)
        if source_offset_ms is not _UNSET:
            sets.append("source_offset_ms = ?")
            params.append(source_offset_ms)
        if course_id is not _UNSET:
            sets.append("course_id = ?")
            params.append(course_id)
        if pinned is not _UNSET:
            sets.append("pinned = ?")
            params.append(1 if pinned else 0)
        if not sets:
            existing = self.get_task(task_id)
            if existing is None:
                raise KeyError(task_id)
            return existing
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(task_id)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if cur.rowcount == 0:
                raise KeyError(task_id)
        updated = self.get_task(task_id)
        assert updated is not None
        return updated

    def delete_task(self, task_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cur.rowcount > 0

    def list_tasks(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Task], int]:
        """List tasks with optional filters.

        Sort order: status (open first, then cancelled, then done),
        then by due date (overdue / dated first, no-date last), then
        by priority (urgent → low), then by created_at DESC.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        clauses: list[str] = []
        where_params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            where_params.append(kind)
        if status is not None:
            clauses.append("status = ?")
            where_params.append(status)
        if priority is not None:
            clauses.append("priority = ?")
            where_params.append(priority)
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._tx() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM tasks {where_sql}",
                where_params,
            ).fetchone()["c"]
            rows = conn.execute(
                f"""
                SELECT * FROM tasks
                {where_sql}
                ORDER BY
                    CASE status
                        WHEN 'open' THEN 0
                        WHEN 'cancelled' THEN 1
                        WHEN 'done' THEN 2
                        ELSE 3
                    END,
                    -- Tasks with a due date sort before those without
                    -- (an overdue task is more urgent than any
                    -- no-date task at any priority). Within the
                    -- dated bucket, earlier dates come first.
                    CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
                    due_at ASC,
                    CASE priority
                        WHEN 'urgent' THEN 0
                        WHEN 'high'   THEN 1
                        WHEN 'medium' THEN 2
                        WHEN 'low'    THEN 3
                        ELSE 4
                    END,
                    created_at DESC
                LIMIT ? OFFSET ?
                """,
                where_params + [limit, offset],
            ).fetchall()
        return [self._row_to_task(r) for r in rows], int(total)

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        keys = row.keys()
        return Task(
            id=row["id"],
            title=row["title"],
            body_md=row["body_md"],
            description=row["description"] if "description" in keys else None,
            priority=row["priority"],
            status=row["status"],
            kind=row["kind"] if "kind" in keys else "task",
            due_at=row["due_at"] if "due_at" in keys else None,
            source=row["source"],
            source_ref=row["source_ref"],
            source_offset_ms=(
                row["source_offset_ms"]
                if "source_offset_ms" in keys else None
            ),
            course_id=row["course_id"] if "course_id" in keys else None,
            pinned=bool(row["pinned"]) if "pinned" in keys else False,
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def write_chunk(
        self,
        session_id: str,
        chunk_index: int,
        payload: bytes,
    ) -> int:
        """Atomically write a chunk's WAV bytes. Returns byte size written.

        Safe to call repeatedly with the same ``chunk_index`` (a retry
        overwrites the previous final file). A torn upload leaves only
        the ``.tmp`` file; the final file is unchanged.
        """
        if chunk_index < _CHUNK_INDEX_MIN:
            raise ValueError("chunk_index must be >= 1")
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(session_id)
        if sess.status not in (STATUS_RECEIVING, STATUS_FAILED):
            raise RuntimeError(
                f"session {session_id} is {sess.status}; cannot accept chunks"
            )
        if chunk_index > sess.expected_count and sess.expected_count > 0:
            raise ValueError(
                f"chunk_index {chunk_index} exceeds expected_count "
                f"{sess.expected_count}"
            )

        lock = self._lock_for(session_id)
        with lock:
            chunks_dir = self.chunks_dir(session_id)
            chunks_dir.mkdir(parents=True, exist_ok=True)
            final_path = chunks_dir / _chunk_filename(chunk_index)
            tmp_path = chunks_dir / f"{_chunk_filename(chunk_index)}.tmp"

            with open(tmp_path, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, final_path)

            now = now_iso()
            with self._tx() as conn:
                conn.execute(
                    """
                    INSERT INTO chunks (session_id, chunk_index, byte_size, received_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id, chunk_index) DO UPDATE SET
                        byte_size = excluded.byte_size,
                        received_at = excluded.received_at
                    """,
                    (session_id, chunk_index, len(payload), now),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
            if sess.status == STATUS_FAILED:
                self.update_status(
                    session_id, STATUS_RECEIVING, status_detail=None
                )
            self._write_meta_json(session_id)
        return len(payload)

    def list_received_chunks(self, session_id: str) -> list[int]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT chunk_index FROM chunks WHERE session_id = ? ORDER BY chunk_index",
                (session_id,),
            ).fetchall()
        return [r["chunk_index"] for r in rows]

    def missing_chunks(self, session_id: str) -> list[int]:
        sess = self.get_session(session_id)
        if sess is None:
            return []
        present = set(self.list_received_chunks(session_id))
        return [
            n for n in range(1, sess.expected_count + 1) if n not in present
        ]

    # --- finish / stitching ----------------------------------------------

    @dataclass
    class FinishResult:
        ok: bool
        reason: str
        missing: list[int]
        full_audio_size: int

    def finish_session(self, session_id: str) -> "Storage.FinishResult":
        """Validate chunk presence and stitch into full.aac.

        Idempotent: if the session is already ``ready`` (full.aac exists),
        return success without re-stitching.
        """
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(session_id)
        if (
            sess.status == STATUS_READY
            and self.full_audio_path(session_id).exists()
        ):
            return Storage.FinishResult(
                ok=True, reason="already_ready", missing=[],
                full_audio_size=self.full_audio_path(session_id).stat().st_size,
            )

        missing = self.missing_chunks(session_id)
        if missing:
            return Storage.FinishResult(
                ok=False, reason="missing_chunks", missing=missing,
                full_audio_size=0,
            )

        lock = self._lock_for(session_id)
        with lock:
            sess = self.get_session(session_id)
            assert sess is not None
            if (
                sess.status == STATUS_READY
                and self.full_audio_path(session_id).exists()
            ):
                return Storage.FinishResult(
                    ok=True, reason="already_ready", missing=[],
                    full_audio_size=self.full_audio_path(session_id).stat().st_size,
                )
            self.update_status(session_id, STATUS_STITCHING, status_detail=None)
            try:
                size = self._stitch_chunks(session_id, sess)
            except Exception as exc:
                self.update_status(
                    session_id, STATUS_FAILED,
                    status_detail=f"stitch error: {exc!r}",
                )
                return Storage.FinishResult(
                    ok=False, reason="stitch_error", missing=[],
                    full_audio_size=0,
                )
            self.update_status(
                session_id, STATUS_READY,
                status_detail=None, finished_at=now_iso(),
            )
        return Storage.FinishResult(
            ok=True, reason="stitched", missing=[], full_audio_size=size,
        )

    def _stitch_chunks(self, session_id: str, sess: Session) -> int:
        """Concatenate ``chunks/NNN.aac`` into ``full.aac`` (atomic).

        ADTS AAC is a self-delimiting stream: the concatenation of
        same-stream chunks is itself a valid ADTS stream, so stitching
        is a plain binary copy — instant, with no re-encode and no
        quality loss. Chunks must all sniff as the same audio kind
        (mixing codecs would produce an invalid stream).
        """
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir = self.chunks_dir(session_id)

        chunk_files: list[Path] = []
        for n in range(1, sess.expected_count + 1):
            p = self.chunk_path(session_id, n)
            if not p.exists():
                raise FileNotFoundError(f"chunk {n} missing on disk")
            chunk_files.append(p)

        from services import transcode as _tx
        kinds = {
            _tx.sniff_format(p).kind for p in chunk_files
        }
        if len(kinds) > 1 or kinds == {"unknown"}:
            raise ValueError(
                "chunks are not a consistent audio stream: "
                + ", ".join(sorted(kinds))
            )

        full_tmp = session_dir / "full.aac.tmp"
        full_final = session_dir / "full.aac"

        with open(full_tmp, "wb") as out:
            for p in chunk_files:
                with open(p, "rb") as f:
                    while True:
                        buf = f.read(1024 * 1024)
                        if not buf:
                            break
                        out.write(buf)
                # Every chunk must contribute bytes; an empty chunk
                # would silently drop audio.
                if p.stat().st_size == 0:
                    raise ValueError(f"chunk {p.name} is empty")
            out.flush()
            os.fsync(out.fileno())
        os.replace(full_tmp, full_final)

        # Remove the chunk files; DB rows stay as provenance.
        for p in chunk_files:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        try:
            chunks_dir.rmdir()
        except OSError:
            pass

        return full_final.stat().st_size

    # --- events -----------------------------------------------------------

    def create_event(
        self,
        event_id: str,
        *,
        title: str,
        kind: str,
        starts_at: str,
        ends_at: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        source: str = "manual",
        source_ref: Optional[str] = None,
        course_id: Optional[str] = None,
        matched_session_id: Optional[str] = None,
        match_confidence: Optional[float] = None,
    ) -> Event:
        """Insert a new event. All validation is the caller's job
        (the service layer is responsible)."""
        now = now_iso()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO events
                    (id, title, description, kind, starts_at, ends_at,
                     location, source, source_ref, course_id,
                     matched_session_id, match_confidence,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, title, description, kind, starts_at, ends_at,
                 location, source, source_ref, course_id,
                 matched_session_id, match_confidence, now, now),
            )
        return self.get_event(event_id)  # type: ignore[return-value]

    def get_event(self, event_id: str) -> Optional[Event]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_event(row)

    def update_event(
        self,
        event_id: str,
        *,
        title: Optional[str] = None,
        description: Any = _UNSET,
        kind: Optional[str] = None,
        starts_at: Optional[str] = None,
        ends_at: Any = _UNSET,
        location: Any = _UNSET,
        source: Optional[str] = None,
        source_ref: Any = _UNSET,
        course_id: Any = _UNSET,
        matched_session_id: Any = _UNSET,
        match_confidence: Any = _UNSET,
    ) -> Event:
        """Partial update. ``title``, ``kind``, ``starts_at``, ``source``
        use the simple None-means-don't-touch convention. The other
        fields use ``_UNSET`` so callers can pass ``None`` to explicitly
        clear them.
        """
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title = ?"); params.append(title)
        if description is not _UNSET:
            sets.append("description = ?"); params.append(description)
        if kind is not None:
            sets.append("kind = ?"); params.append(kind)
        if starts_at is not None:
            sets.append("starts_at = ?"); params.append(starts_at)
        if ends_at is not _UNSET:
            sets.append("ends_at = ?"); params.append(ends_at)
        if location is not _UNSET:
            sets.append("location = ?"); params.append(location)
        if source is not None:
            sets.append("source = ?"); params.append(source)
        if source_ref is not _UNSET:
            sets.append("source_ref = ?"); params.append(source_ref)
        if course_id is not _UNSET:
            sets.append("course_id = ?"); params.append(course_id)
        if matched_session_id is not _UNSET:
            sets.append("matched_session_id = ?")
            params.append(matched_session_id)
        if match_confidence is not _UNSET:
            sets.append("match_confidence = ?")
            params.append(match_confidence)
        if not sets:
            existing = self.get_event(event_id)
            if existing is None:
                raise KeyError(event_id)
            return existing
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(event_id)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE events SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if cur.rowcount == 0:
                raise KeyError(event_id)
        updated = self.get_event(event_id)
        assert updated is not None
        return updated

    def delete_event(self, event_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM events WHERE id = ?", (event_id,)
            )
            return cur.rowcount > 0

    def list_events(
        self,
        *,
        kind: Optional[str] = None,
        starts_after: Optional[str] = None,
        starts_before: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> tuple[list[Event], int]:
        """List events ordered by ``starts_at`` ascending. Both bounds
        are inclusive. Returns ``(events, total)`` where ``total`` is
        the count BEFORE limit/offset.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        clauses: list[str] = []
        where_params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            where_params.append(kind)
        if starts_after is not None:
            clauses.append("starts_at >= ?")
            where_params.append(starts_after)
        if starts_before is not None:
            clauses.append("starts_at <= ?")
            where_params.append(starts_before)
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._tx() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM events {where_sql}",
                where_params,
            ).fetchone()["c"]
            rows = conn.execute(
                f"""
                SELECT * FROM events
                {where_sql}
                ORDER BY starts_at ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                where_params + [limit, offset],
            ).fetchall()
        return [self._row_to_event(r) for r in rows], int(total)

    def clear_event_match_for_session(self, session_id: str) -> int:
        """Remove ``matched_session_id`` from any event that points at
        ``session_id``. Returns count of rows cleared."""
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE events
                SET matched_session_id = NULL,
                    match_confidence = NULL,
                    updated_at = ?
                WHERE matched_session_id = ?
                """,
                (now_iso(), session_id),
            )
            return cur.rowcount

    def event_for_session(self, session_id: str) -> Optional[Event]:
        """The calendar event a recording was matched to, if any."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE matched_session_id = ? "
                "ORDER BY starts_at LIMIT 1",
                (session_id,),
            ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        keys = row.keys()
        return Event(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            kind=row["kind"],
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
            location=row["location"],
            source=row["source"],
            source_ref=row["source_ref"] if "source_ref" in keys else None,
            course_id=(
                row["course_id"] if "course_id" in keys else None
            ),
            matched_session_id=(
                row["matched_session_id"]
                if "matched_session_id" in keys else None
            ),
            match_confidence=(
                row["match_confidence"]
                if "match_confidence" in keys else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # --- audio serving ----------------------------------------------------

    def open_full_audio(self, session_id: str) -> Optional[Path]:
        p = self.full_audio_path(session_id)
        return p if p.exists() else None

    # --- web upload ingest ------------------------------------------------

    def ingest_single_audio(
        self,
        session_id: str,
        title: str,
        audio_bytes: bytes,
        started_at: str,
    ) -> Session:
        """Create a session, write one canonical AAC chunk, finish in
        one call.

        Used by the web upload path. ``audio_bytes`` must already be
        canonical AAC (ADTS); transcoding is the caller's
        responsibility.
        """
        self.upsert_session(
            session_id,
            title=title,
            expected_count=1,
            sample_rate=22050,
            channels=1,
            bits_per_sample=16,
            started_at=started_at,
        )
        self.write_chunk(session_id, 1, audio_bytes)
        result = self.finish_session(session_id)
        if not result.ok:
            raise RuntimeError(
                f"ingest_single_audio: finish failed: {result.reason}"
            )
        sess = self.get_session(session_id)
        assert sess is not None
        return sess
