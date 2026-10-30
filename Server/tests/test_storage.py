"""Storage-layer unit tests.

Run from the Server directory: ``python -m unittest discover -s tests -v``.
"""
from __future__ import annotations

import struct
import unittest

import storage

from tests.conftest import LRTestCase


class InitialiseTests(LRTestCase):

    def test_initialise_creates_dirs(self) -> None:
        store = self.app.config["LR_STORAGE"]
        self.assertTrue(self._tmpdir.exists())
        self.assertTrue((self._tmpdir / "db").exists())
        self.assertTrue((self._tmpdir / "sessions").exists())
        self.assertTrue((self._tmpdir / "db" / "metadata.db").exists())

    def test_schema_includes_future_seams(self) -> None:
        store = self.app.config["LR_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for expected in ("sessions", "chunks", "courses", "artifacts"):
            self.assertIn(expected, tables, f"missing table {expected}")

    def test_search_tables_created(self) -> None:
        store = self.app.config["LR_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            names = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for name in ("search_items", "search_rows", "search_fts"):
            self.assertIn(name, names)
        self.assertNotIn("artifacts_fts", names)  # the old, unused index


class SessionTests(LRTestCase):

    def test_upsert_creates_session(self) -> None:
        store = self.app.config["LR_STORAGE"]
        sid = "abcdef1234567890"
        sess, created = store.upsert_session(
            sid,
            title="Test",
            expected_count=2,
            sample_rate=22050,
            channels=1,
            bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        self.assertTrue(created)
        self.assertEqual(sess.id, sid)
        self.assertEqual(sess.status, storage.STATUS_RECEIVING)

    def test_upsert_is_idempotent(self) -> None:
        store = self.app.config["LR_STORAGE"]
        sid = "abcdef1234567890"
        store.upsert_session(
            sid, title="First", expected_count=2,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        sess, created = store.upsert_session(
            sid, title="Second", expected_count=2,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        self.assertFalse(created)
        self.assertEqual(sess.title, "First")

    def test_session_id_validation(self) -> None:
        store = self.app.config["LR_STORAGE"]
        with self.assertRaises(ValueError):
            store.upsert_session(
                "ab", title="x", expected_count=1,
                sample_rate=22050, channels=1, bits_per_sample=32,
                started_at=storage.now_iso(),
            )

    def test_list_sessions_ordering(self) -> None:
        store = self.app.config["LR_STORAGE"]
        # Use distinct created_at values via started_at so ordering is
        # deterministic without depending on real wall-clock time.
        for i, tag in enumerate(("aaaa", "bbbb", "cccc")):
            store.upsert_session(
                tag, title=tag, expected_count=1,
                sample_rate=22050, channels=1, bits_per_sample=32,
                started_at=f"2026-10-03T12:00:0{i}Z",
            )
        sessions = store.list_sessions()
        self.assertEqual([s.id for s in sessions], ["cccc", "bbbb", "aaaa"])


class ChunkWriteTests(LRTestCase):

    def _make_session(self, expected: int = 2) -> str:
        store = self.app.config["LR_STORAGE"]
        sid = "abcdef1234567890"
        store.upsert_session(
            sid, title="t", expected_count=expected,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        return sid

    def test_write_chunk_creates_file(self) -> None:
        sid = self._make_session()
        store = self.app.config["LR_STORAGE"]
        payload = self.make_wav()
        size = store.write_chunk(sid, 1, payload)
        self.assertEqual(size, len(payload))
        self.assertTrue(store.chunk_path(sid, 1).exists())

    def test_write_chunk_overwrites_safely(self) -> None:
        sid = self._make_session()
        store = self.app.config["LR_STORAGE"]
        a = self.make_wav(b"A" * 200)
        b = self.make_wav(b"B" * 200)
        store.write_chunk(sid, 1, a)
        store.write_chunk(sid, 1, b)
        data = store.chunk_path(sid, 1).read_bytes()
        self.assertEqual(data, b)
        # No .tmp file should remain after a successful write.
        self.assertFalse(
            store.chunk_path(sid, 1).with_suffix(".aac.tmp").exists()
        )

    def test_write_chunk_too_high_index_rejected(self) -> None:
        sid = self._make_session(expected=2)
        store = self.app.config["LR_STORAGE"]
        with self.assertRaises(ValueError):
            store.write_chunk(sid, 5, self.make_wav())

    def test_missing_chunks_reports_gaps(self) -> None:
        sid = self._make_session(expected=3)
        store = self.app.config["LR_STORAGE"]
        store.write_chunk(sid, 1, self.make_wav())
        store.write_chunk(sid, 3, self.make_wav())
        self.assertEqual(store.missing_chunks(sid), [2])

    def test_meta_json_written(self) -> None:
        sid = self._make_session()
        store = self.app.config["LR_STORAGE"]
        meta_path = store.session_dir(sid) / "meta.json"
        self.assertTrue(meta_path.exists())


class FinishTests(LRTestCase):

    def _make_full_session(self, n: int = 3) -> str:
        store = self.app.config["LR_STORAGE"]
        sid = "abcdef1234567890"
        store.upsert_session(
            sid, title="t", expected_count=n,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        for i in range(1, n + 1):
            store.write_chunk(sid, i, self.make_wav(bytes([i]) * 200))
        return sid

    def test_finish_missing_returns_409_payload(self) -> None:
        store = self.app.config["LR_STORAGE"]
        sid = "abcdef1234567890"
        store.upsert_session(
            sid, title="t", expected_count=3,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        store.write_chunk(sid, 1, self.make_wav())
        result = store.finish_session(sid)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "missing_chunks")
        self.assertEqual(result.missing, [2, 3])

    def test_finish_stitches_full_aac(self) -> None:
        sid = self._make_full_session(n=3)
        store = self.app.config["LR_STORAGE"]
        result = store.finish_session(sid)
        self.assertTrue(result.ok, result.reason)
        full = store.open_full_audio(sid)
        self.assertIsNotNone(full)
        data = full.read_bytes()
        # Binary concat of the chunk files (verbatim copy, headers
        # included): 3 * 244-byte WAV chunks.
        self.assertEqual(len(data), 3 * 244)
        # Chunks concatenate in order: chunk k's marker byte is at
        # (k-1)*244 + 44 (its first payload byte).
        self.assertEqual(data[0 * 244 + 44], 1)
        self.assertEqual(data[1 * 244 + 44], 2)
        self.assertEqual(data[2 * 244 + 44], 3)

    def test_finish_removes_chunk_files(self) -> None:
        sid = self._make_full_session(n=2)
        store = self.app.config["LR_STORAGE"]
        store.finish_session(sid)
        # Chunk files are gone; the chunks directory is rmdir'd.
        self.assertFalse(store.chunk_path(sid, 1).exists())
        self.assertFalse(store.chunks_dir(sid).exists())

    def test_finish_idempotent(self) -> None:
        sid = self._make_full_session(n=2)
        store = self.app.config["LR_STORAGE"]
        r1 = store.finish_session(sid)
        r2 = store.finish_session(sid)
        self.assertTrue(r1.ok)
        self.assertTrue(r2.ok)
        self.assertEqual(r2.reason, "already_ready")

    def test_finish_clears_failed_status_on_retry(self) -> None:
        store = self.app.config["LR_STORAGE"]
        sid = self._make_full_session(n=2)
        store.update_status(sid, storage.STATUS_FAILED, status_detail="boom")
        self.assertEqual(store.get_session(sid).status, storage.STATUS_FAILED)
        # Re-send chunk 1 (overwrite); status flips back to receiving.
        store.write_chunk(sid, 1, self.make_wav(b"X" * 50))
        self.assertEqual(store.get_session(sid).status, storage.STATUS_RECEIVING)


class NonMinimalWavTests(LRTestCase):
    """Regression: WAV files that aren't a strict 44-byte header.

    ffmpeg 9.x writes a LIST chunk between fmt and data, and other
    encoders may write JUNK, INFO, or bext. The stitcher is now a
    plain binary concat, so it accepts any consistent audio stream
    and copies chunk files verbatim into full.aac.
    """

    @staticmethod
    def _build_wav_with_extra_chunk(payload: bytes,
                                     extra_chunk_id: bytes = b"LIST",
                                     extra_payload: bytes = b"INFO" + b"\x00" * 8) -> bytes:
        """Build a canonical 22050 Hz mono 32-bit PCM WAV with an extra
        chunk inserted between the fmt and data chunks, mimicking the
        output of ffmpeg 9.x.
        """
        byte_rate = 22050 * 1 * (32 // 8)
        block_align = 1 * (32 // 8)
        data_size = len(payload)
        riff_size = 4 + (8 + 16) + (8 + len(extra_payload)) + (8 + data_size)

        out = bytearray()
        out += b"RIFF"
        out += struct.pack("<I", riff_size)
        out += b"WAVE"

        # fmt chunk (16 bytes payload)
        out += b"fmt "
        out += struct.pack("<I", 16)
        out += struct.pack("<H", 1)            # PCM
        out += struct.pack("<H", 1)            # channels
        out += struct.pack("<I", 22050)       # sample rate
        out += struct.pack("<I", byte_rate)    # byte rate
        out += struct.pack("<H", block_align)  # block align
        out += struct.pack("<H", 32)           # bits per sample

        # extra chunk (LIST/INFO/JUNK/etc.)
        out += extra_chunk_id
        out += struct.pack("<I", len(extra_payload))
        out += extra_payload
        if len(extra_payload) % 2:
            out += b"\x00"  # word-align padding

        # data chunk
        out += b"data"
        out += struct.pack("<I", data_size)
        out += payload
        return bytes(out)

    def test_wav_with_list_chunk_is_accepted_by_sniff(self) -> None:
        wav = self._build_wav_with_extra_chunk(b"\x00" * 200)
        store = self.app.config["LR_STORAGE"]
        sid = "listchunk1234567890"
        store.upsert_session(
            sid, title="t", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        # write_chunk should accept the messy WAV; the existing strict
        # 44-byte check used to be a no-op for write_chunk but
        # finish_session would fail.
        store.write_chunk(sid, 1, wav)
        result = store.finish_session(sid)
        self.assertTrue(result.ok, result.reason)

    def test_wav_with_junk_chunk_is_accepted(self) -> None:
        wav = self._build_wav_with_extra_chunk(
            b"\x00" * 200, extra_chunk_id=b"JUNK",
            extra_payload=b"\x00" * 64,
        )
        store = self.app.config["LR_STORAGE"]
        sid = "junkchunk1234567890"
        store.upsert_session(
            sid, title="t", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        store.write_chunk(sid, 1, wav)
        result = store.finish_session(sid)
        self.assertTrue(result.ok, result.reason)

    def test_stitched_full_aac_concatenates_chunk_files(self) -> None:
        """The full.aac output must be the exact byte concatenation
        of the chunk files (stream copy, no header surgery)."""
        payload = b"ABCD" * 500  # 2000 bytes, easy to identify in output
        wav = self._build_wav_with_extra_chunk(payload)
        store = self.app.config["LR_STORAGE"]
        sid = "payload1234567890ab"
        store.upsert_session(
            sid, title="t", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        store.write_chunk(sid, 1, wav)
        result = store.finish_session(sid)
        self.assertTrue(result.ok, result.reason)
        full = store.open_full_audio(sid).read_bytes()
        # full.aac is the chunk file copied verbatim.
        self.assertEqual(full, wav)

    def test_stitched_multi_chunk_concatenates_in_order(self) -> None:
        store = self.app.config["LR_STORAGE"]
        sid = "concat1234567890a"
        store.upsert_session(
            sid, title="t", expected_count=3,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        chunks = [
            self._build_wav_with_extra_chunk(b"\x01" * 100),
            self._build_wav_with_extra_chunk(b"\x02" * 100),
            self._build_wav_with_extra_chunk(b"\x03" * 100),
        ]
        for i, c in enumerate(chunks, start=1):
            store.write_chunk(sid, i, c)
        result = store.finish_session(sid)
        self.assertTrue(result.ok, result.reason)
        full = store.open_full_audio(sid).read_bytes()
        self.assertEqual(full, b"".join(chunks))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Milestone 8: new statuses, transition guard, add_artifact helper
# ---------------------------------------------------------------------------


class M8StatusConstantsTests(unittest.TestCase):
    """The new status enum values and ALL_STATUSES tuple are
    part of the schema contract; they need to be discoverable
    by other modules (notably :mod:`services.processing`)."""

    def test_new_status_values_exist(self):
        self.assertEqual(storage.STATUS_TRANSCRIBING, "transcribing")
        self.assertEqual(storage.STATUS_TRANSCRIBED, "transcribed")
        self.assertEqual(storage.STATUS_CLASSIFYING, "classifying")
        self.assertEqual(storage.STATUS_DONE, "done")

    def test_all_statuses_includes_new_values(self):
        for s in (
            storage.STATUS_TRANSCRIBING,
            storage.STATUS_TRANSCRIBED,
            storage.STATUS_CLASSIFYING,
            storage.STATUS_DONE,
            storage.STATUS_FAILED,
            storage.STATUS_READY,
        ):
            self.assertIn(s, storage.ALL_STATUSES)


    def test_m9_status_values_exist(self):
        # M9 adds two more pipeline states between
        # ``classifying`` and ``done``.
        self.assertEqual(storage.STATUS_SUMMARISING, "summarising")
        self.assertEqual(storage.STATUS_EXTRACTING, "extracting")
        for s in (
            storage.STATUS_SUMMARISING,
            storage.STATUS_EXTRACTING,
        ):
            self.assertIn(s, storage.ALL_STATUSES)

    def test_allowed_transitions_table_is_populated(self):
        for s in storage.ALL_STATUSES:
            self.assertIn(s, storage.ALLOWED_STATUS_TRANSITIONS)
            self.assertIsInstance(
                storage.ALLOWED_STATUS_TRANSITIONS[s], frozenset,
            )


class M8TransitionGuardTests(LRTestCase):
    """update_status now refuses illegal jumps."""

    def _make(self):
        import uuid
        sid = uuid.uuid4().hex
        store = self.app.config["LR_STORAGE"]
        store.upsert_session(
            sid, title="x", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        return sid, store

    def test_legal_transition_works(self):
        sid, store = self._make()
        store.update_status(sid, storage.STATUS_STITCHING)
        self.assertEqual(
            store.get_session(sid).status, storage.STATUS_STITCHING,
        )

    def test_illegal_transition_raises(self):
        sid, store = self._make()
        with self.assertRaises(ValueError):
            store.update_status(sid, storage.STATUS_DONE)
        self.assertEqual(
            store.get_session(sid).status, storage.STATUS_RECEIVING,
        )

    def test_legal_pipeline_progression(self):
        """The full M8+M9 pipeline: receiving -> stitching -> ready
        -> transcribing -> transcribed -> classifying
        -> summarising -> extracting -> done."""
        sid, store = self._make()
        store.update_status(sid, storage.STATUS_STITCHING)
        store.update_status(
            sid, storage.STATUS_READY, finished_at=storage.now_iso(),
        )
        store.update_status(sid, storage.STATUS_TRANSCRIBING)
        store.update_status(sid, storage.STATUS_TRANSCRIBED)
        store.update_status(sid, storage.STATUS_CLASSIFYING)
        store.update_status(sid, storage.STATUS_SUMMARISING)
        store.update_status(sid, storage.STATUS_EXTRACTING)
        store.update_status(sid, storage.STATUS_DONE)
        self.assertEqual(
            store.get_session(sid).status, storage.STATUS_DONE,
        )

    def test_set_status_is_alias(self):
        sid, store = self._make()
        store.set_status(sid, storage.STATUS_STITCHING)
        self.assertEqual(
            store.get_session(sid).status, storage.STATUS_STITCHING,
        )


class M8AddArtifactTests(LRTestCase):
    """Storage.add_artifact inserts into artifacts and returns the
    row."""

    def _make(self):
        import uuid
        sid = uuid.uuid4().hex
        store = self.app.config["LR_STORAGE"]
        store.upsert_session(
            sid, title="x", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        return sid, store

    def test_add_artifact_persists_row(self):
        sid, store = self._make()
        row = store.add_artifact(
            sid, "transcript", "hello world",
            model="openai/whisper-large-v3",
        )
        self.assertEqual(row["kind"], "transcript")
        self.assertEqual(row["content"], "hello world")
        self.assertEqual(row["model"], "openai/whisper-large-v3")
        self.assertEqual(len(row["id"]), 32)
        fetched = store.get_artifact(sid, "transcript")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["content"], "hello world")

    def test_add_artifact_missing_session_raises_keyerror(self):
        store = self.app.config["LR_STORAGE"]
        with self.assertRaises(KeyError):
            store.add_artifact("no-such-id", "transcript", "x")

    def test_add_artifact_validates_inputs(self):
        sid, store = self._make()
        with self.assertRaises(ValueError):
            store.add_artifact("", "transcript", "x")
        with self.assertRaises(ValueError):
            store.add_artifact(sid, "", "x")
        with self.assertRaises(ValueError):
            store.add_artifact(sid, "transcript", None)  # type: ignore[arg-type]

    def test_add_artifact_replaces_same_kind(self):
        """A second transcript (pipeline retry) replaces the first,
        in the table and the search index; other kinds stay."""
        sid, store = self._make()
        store.add_artifact(sid, "summary", "the summary")
        store.add_artifact(sid, "transcript", "first needle")
        store.add_artifact(sid, "transcript", "second needle")
        fetched = store.get_artifact(sid, "transcript")
        self.assertEqual(fetched["content"], "second needle")
        self.assertEqual(store.get_artifact(sid, "summary")["content"],
                         "the summary")
        with store._tx() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE session_id = ? "
                "AND kind = 'transcript'", (sid,)).fetchone()[0]
        self.assertEqual(rows, 1)
        import search
        hits = search.search(store, "needle")["recording"]
        self.assertEqual([h.best["text"] for h in hits], ["second needle"])
