"""Storage layer: SQLite metadata + filesystem audio files.

This module owns the *shape* of the data on disk. It exposes one helper
class, :class:`Storage`, that the API and services layers talk to.

Phase A responsibilities:

* Maintain the SQLite schema (sessions, chunks, plus the future-use
  courses / artifacts / artifacts_fts tables that are created empty).
* Write each uploaded chunk atomically: ``chunks/NNN.wav.tmp`` then rename.
* List and read stitched audio (``full.wav``).
* Stitch chunks into a single ``full.wav`` and remove the originals.

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
from typing import Any, Iterator, Optional

from config import Config


# Status values describing the server-side lifecycle. Phase A uses
# receiving / stitching / ready / failed. Later phases will introduce
# transcribing, summarising, etc., into the same column.
STATUS_RECEIVING = "receiving"
STATUS_STITCHING = "stitching"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

ALL_STATUSES: tuple[str, ...] = (
    STATUS_RECEIVING, STATUS_STITCHING, STATUS_READY, STATUS_FAILED,
)


_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")
_CHUNK_INDEX_MIN = 1
WAV_HEADER_SIZE = 44


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
    """Three-digit zero-padded chunk filename, e.g. 007.wav."""
    return f"{index:03d}.wav"


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

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "title": self.title,
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
                if self.status == STATUS_READY and self.full_audio_size > 0
                else None
            ),
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
            conn.commit()
        self._log(f"data dir: {self._data_dir}")
        self._log(f"DB ready: {self._db_path}")

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
        return self.session_dir(session_id) / "full.wav"

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
        with self._tx() as conn:
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

    # --- chunk writes -----------------------------------------------------

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
        """Validate chunk presence and stitch into full.wav.

        Idempotent: if the session is already ``ready`` (full.wav exists),
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
        """Concatenate ``chunks/NNN.wav`` into ``full.wav`` (atomic).

        Uses a chunk-walking WAV parser so it works with files that
        contain extra chunks between ``fmt `` and ``data`` (LIST, JUNK,
        INFO, bext, ...) -- which is what ffmpeg 9.x produces.
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

        # Read the first chunk's full header so we can verify the audio
        # format and find the data chunk's offset.
        with open(chunk_files[0], "rb") as f:
            first = f.read()
        _validate_wav_header(first)
        sample_rate, channels, bits_per_sample = _parse_wav_header(first)
        if (sample_rate, channels, bits_per_sample) != (
            sess.sample_rate, sess.channels, sess.bits_per_sample
        ):
            raise ValueError(
                "chunk header mismatch: "
                f"file=({sample_rate},{channels},{bits_per_sample}) "
                f"session=({sess.sample_rate},{sess.channels},{sess.bits_per_sample})"
            )

        # For each chunk, locate the data chunk and compute how many
        # bytes of audio are actually present. We trust the header's
        # declared data size but never assume the file is exactly that
        # many bytes after a 44-byte header -- ffmpeg may have added
        # extra chunks, and the file may end with a padding byte.
        per_chunk_audio: list[tuple[Path, int, int]] = []  # (path, data_offset, audio_len)
        total_data = 0
        for p in chunk_files:
            with open(p, "rb") as f:
                hdr = f.read()
            _validate_wav_header(hdr)
            data_offset = _wav_data_offset(hdr)
            if data_offset is None:
                raise ValueError(f"chunk {p.name} has no 'data' chunk")
            declared = _wav_data_chunk_size(hdr)
            available = len(hdr) - data_offset
            audio_len = min(declared, available) if declared else available
            per_chunk_audio.append((p, data_offset, audio_len))
            total_data += audio_len

        full_tmp = session_dir / "full.wav.tmp"
        full_final = session_dir / "full.wav"

        with open(full_tmp, "wb") as out:
            out.write(_build_wav_header(
                sample_rate, channels, bits_per_sample, total_data
            ))
            for p, data_offset, audio_len in per_chunk_audio:
                if audio_len <= 0:
                    continue
                with open(p, "rb") as f:
                    f.seek(data_offset)
                    remaining = audio_len
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(full_tmp, full_final)

        # Remove the chunk files; DB rows stay as provenance.
        for p, _, _ in per_chunk_audio:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        try:
            chunks_dir.rmdir()
        except OSError:
            pass

        return full_final.stat().st_size

    # --- audio serving ----------------------------------------------------

    def open_full_audio(self, session_id: str) -> Optional[Path]:
        p = self.full_audio_path(session_id)
        return p if p.exists() else None

    # --- web upload ingest ------------------------------------------------

    def ingest_single_audio(
        self,
        session_id: str,
        title: str,
        wav_bytes: bytes,
        started_at: str,
    ) -> Session:
        """Create a session, write one canonical WAV chunk, finish in one call.

        Used by the web upload path. ``wav_bytes`` must already be in
        the canonical 22050 Hz mono 32-bit PCM WAV format; transcoding
        is the caller's responsibility.
        """
        self.upsert_session(
            session_id,
            title=title,
            expected_count=1,
            sample_rate=22050,
            channels=1,
            bits_per_sample=32,
            started_at=started_at,
        )
        self.write_chunk(session_id, 1, wav_bytes)
        result = self.finish_session(session_id)
        if not result.ok:
            raise RuntimeError(
                f"ingest_single_audio: finish failed: {result.reason}"
            )
        sess = self.get_session(session_id)
        assert sess is not None
        return sess
