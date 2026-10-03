"""Storage-layer unit tests.

Run from the Server directory: ``python -m unittest discover -s tests -v``.
"""
from __future__ import annotations

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

    def test_fts5_table_created(self) -> None:
        store = self.app.config["LR_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            names = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("artifacts_fts", names)


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
            store.chunk_path(sid, 1).with_suffix(".wav.tmp").exists()
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

    def test_finish_stitches_full_wav(self) -> None:
        sid = self._make_full_session(n=3)
        store = self.app.config["LR_STORAGE"]
        result = store.finish_session(sid)
        self.assertTrue(result.ok, result.reason)
        full = store.open_full_audio(sid)
        self.assertIsNotNone(full)
        data = full.read_bytes()
        self.assertEqual(data[0:4], b"RIFF")
        self.assertEqual(data[8:12], b"WAVE")
        # 3 * 200 + 44 (header)
        self.assertEqual(len(data), 3 * 200 + 44)

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


if __name__ == "__main__":
    unittest.main()
