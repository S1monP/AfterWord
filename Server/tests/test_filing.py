"""Auto-filing of deadlines, to-dos and events from recordings."""
from __future__ import annotations

import uuid

import storage
from services import filing, processing
from services.llm import Classification, LLMProvider, Summary
from services.tasks import TasksService
from tests.conftest import LRTestCase


class _Provider(LLMProvider):
    """Transcribes, classifies as ``kind`` and extracts ``items``."""
    name = "stub"

    def __init__(self, kind, items):
        self.kind, self.items, self.recorded_at = kind, items, None

    def transcribe(self, audio_path):
        return "[00:10] Speaker 1: the essay is due next Friday"

    def classify(self, transcript):
        return Classification(kind=self.kind, confidence=0.9)

    def summarize(self, transcript, kind=None):
        return Summary(text="overview", key_points=[], title="Essays")

    def extract(self, transcript, kind, recorded_at=None):
        self.recorded_at = recorded_at
        return list(self.items)


class FilingTests(LRTestCase):
    def setUp(self):
        super().setUp()
        self.store = self.app.config["LR_STORAGE"]
        self.tasks = TasksService(self.store)

    def _session(self, kind="lecture"):
        """A ready recording made Thu 15 Jan 2026, 10:00 (London)."""
        sid = uuid.uuid4().hex
        self.store.upsert_session(
            sid, title="rec_2026-01-15T1000", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=16,
            started_at="2026-01-15T10:00:00Z",
        )
        self.store.full_audio_path(sid).write_bytes(self.make_wav(b"\0" * 64))
        self.store.update_status(sid, storage.STATUS_STITCHING)
        self.store.update_status(sid, storage.STATUS_READY)
        with self.store._tx() as conn:
            conn.execute("UPDATE sessions SET kind = ? WHERE id = ?",
                         (kind, sid))
        return sid

    def _all_tasks(self):
        return self.tasks.list(limit=100)[0]

    def test_lecture_items_go_to_each_list_with_a_link(self):
        sid = self._session("lecture")
        counts = filing.refile(self.store, sid, [
            {"type": "deadline", "text": "Submit essay", "due": "2026-01-23",
             "at": "00:10"},
            {"type": "action_item", "text": "Read chapter 4"},
            {"type": "event", "text": "Midterm exam", "start": "2026-02-02T09:00",
             "event_kind": "other", "at": "1:02:03"},
            {"type": "event", "text": "Field trip", "start": "2026-02-10"},
            {"type": "topic", "text": "Entropy"},                 # not filed
            {"type": "deadline", "text": "Old quiz", "due": "2026-01-01"},
            {"type": "deadline", "text": "No date given"},       # -> a to-do
        ])
        self.assertEqual(counts, {"task": 2, "deadline": 2, "event": 1})
        by_title = {t.title: t for t in self._all_tasks()}
        essay = by_title["Submit essay"]
        self.assertEqual(essay.kind, "deadline")
        self.assertEqual(essay.source, "extracted")
        self.assertEqual(essay.source_ref, sid)
        self.assertEqual(essay.source_offset_ms, 10_000)
        self.assertEqual(essay.description, "From a recording at 0:10.")
        self.assertIn("Field trip", by_title)                   # date only
        self.assertEqual(by_title["Field trip"].kind, "deadline")
        self.assertEqual(by_title["No date given"].kind, "task")
        self.assertNotIn("Old quiz", by_title)                  # in the past
        events, _ = self.app.config["LR_EVENTS_SERVICE"].list()
        exam = next(e for e in events if e.title == "Midterm exam")
        self.assertEqual(exam.source, "extracted")
        self.assertEqual(exam.source_ref, sid)
        self.assertEqual((exam.starts_at, exam.ends_at),
                         ("2026-02-02T09:00:00Z", "2026-02-02T10:00:00Z"))
        self.assertIn("at 1:02:03", exam.description)

    def test_command_items_have_no_link(self):
        sid = self._session("command")
        filing.refile(self.store, sid, [
            {"type": "action_item", "text": "Call the dentist",
             "due": "2026-01-16", "at": "00:01"},
        ])
        (task,) = self._all_tasks()
        self.assertEqual(task.title, "Call the dentist")
        self.assertIsNone(task.source_ref)
        self.assertIsNone(task.source_offset_ms)
        self.assertIsNone(task.description)

    def test_duplicates_are_skipped(self):
        sid = self._session("lecture")
        self.tasks.create(title="Submit the essay", kind="deadline",
                          due_at="2026-01-23")
        counts = filing.refile(self.store, sid, [
            {"type": "deadline", "text": "Submit essay", "due": "2026-01-23"},
            {"type": "deadline", "text": "Submit essay", "due": "2026-01-30"},
        ])
        self.assertEqual(counts["deadline"], 1)                 # other day kept

    def test_refiling_replaces_only_untouched_items(self):
        sid = self._session("lecture")
        filing.refile(self.store, sid, [
            {"type": "action_item", "text": "Read chapter 4"},
            {"type": "action_item", "text": "Email the tutor"},
        ])
        done = next(t for t in self._all_tasks() if t.title == "Email the tutor")
        self.tasks.set_status(done.id, "done")
        filing.refile(self.store, sid, [
            {"type": "action_item", "text": "Read chapter 5"},
        ])
        titles = sorted(t.title for t in self._all_tasks())
        self.assertEqual(titles, ["Email the tutor", "Read chapter 5"])

    def test_pipeline_files_for_lectures_and_commands_only(self):
        items = [{"type": "action_item", "text": "Buy a lab coat"}]
        for kind, expect in (("lecture", 1), ("voice_memo", 0),
                             ("other", 0), ("command", 1)):
            with self.subTest(kind=kind):
                with self.store._tx() as conn:
                    conn.execute("DELETE FROM tasks")
                sid = self._session(None)
                provider = _Provider(kind, items)
                processing.run_post_finish_pipeline(
                    self.store, sid, transcript_provider=provider,
                    classifier=provider, llm_provider=provider,
                )
                self.assertEqual(len(self._all_tasks()), expect)
                sess = self.store.get_session(sid)
                self.assertEqual(sess.status, storage.STATUS_DONE)
                if kind in ("lecture", "command"):
                    self.assertEqual(provider.recorded_at,
                                     "Thursday 15 January 2026, 10:00")
                if kind == "command":
                    # No summary; named; nothing linked to the audio.
                    self.assertIsNone(self.store.get_artifact(sid, "summary"))
                    self.assertEqual(self._all_tasks()[0].source_ref, None)

    def test_moving_a_lecture_to_memos_drops_its_untouched_items(self):
        sid = self._session("lecture")
        filing.refile(self.store, sid, [
            {"type": "action_item", "text": "Read chapter 4"},
        ])
        with self.store._tx() as conn:
            conn.execute("UPDATE sessions SET kind = 'voice_memo' WHERE id = ?",
                         (sid,))
        provider = _Provider("voice_memo", [])
        processing.run_post_finish_pipeline(
            self.store, sid, transcript_provider=provider,
            classifier=provider, llm_provider=provider,
        )
        self.assertEqual(self._all_tasks(), [])

    def test_detail_pane_lists_filed_items(self):
        sid = self._session("lecture")
        filing.refile(self.store, sid, [
            {"type": "deadline", "text": "Submit essay", "due": "2026-01-23"},
            {"type": "event", "text": "Midterm exam",
             "start": "2026-02-02T09:00"},
        ])
        body = self.client.get(f"/lectures?session={sid}",
                               headers={"X-Soft-Nav": "1"}).get_data(as_text=True)
        self.assertIn("Added to your lists", body)
        self.assertIn("Submit essay", body)
        self.assertIn("Midterm exam", body)
        todos = self.client.get("/todos").get_data(as_text=True)
        self.assertIn(f"/sessions/{sid}?t=0", todos)
