"""Tests for Home (web/home.py, templates/home.html)."""
import datetime as dt
import json
import uuid

import timeutil
from tests.conftest import LRTestCase


class HomeTests(LRTestCase):
    def _store(self):
        return self.app.config["AFTERWORD_STORAGE"]

    def _recording(self, *, kind="lecture", status="done", course_id=None, started_at=None):
        sid = uuid.uuid4().hex
        store = self._store()
        store.upsert_session(sid, title="test", expected_count=1, sample_rate=22050,
                             channels=1, bits_per_sample=32,
                             started_at=started_at or timeutil.fmt(timeutil.local_now()))
        with store._tx() as conn:
            conn.execute("UPDATE sessions SET status = ?, kind = ?, course_id = ?, title = ?, title_source = 'ai' WHERE id = ?",
                         (status, kind, course_id, "Eigenvalues and diagonalisation", sid))
        return sid

    def test_empty_home_shows_empty_states_and_search(self):
        body = self.client.get("/").get_data(as_text=True)
        for text in ("Nothing scheduled", "No recordings yet", "Nothing due.", "All clear.",
                     "data-home-search", 'id="home-search-results"', "js/home.js", "js/search.js"):
            self.assertIn(text, body)
        self.assertIn('class="hp"', body)            # the phone's Home I
        self.assertIn("Search everything", body)     # its bottom dock

    def test_schedule_next_up_deadlines_and_todos(self):
        now = timeutil.local_now()
        events = self.app.config["AFTERWORD_EVENTS_SERVICE"]
        start = now + dt.timedelta(minutes=30)
        events.create(title="Project sync with Sam", kind="meeting",
                      starts_at=timeutil.fmt(start), ends_at=timeutil.fmt(start + dt.timedelta(minutes=30)),
                      location="Library room 3")
        tasks = self.app.config["AFTERWORD_TASKS_SERVICE"]
        due = (now + dt.timedelta(days=4)).replace(hour=23, minute=59)
        tasks.create(title="Problem set 4", kind="deadline", due_at=timeutil.fmt(due))
        tasks.create(title="Email Sam the slides", kind="task", priority="high")
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("Project sync with Sam", body)
        self.assertIn("In 30 min", body)                    # its status in the schedule
        self.assertIn("in 30 min", body)                    # Next up on the phone
        self.assertIn("Library room 3", body)
        self.assertIn("Problem set 4", body)
        self.assertRegex(body, r'tile__n">4</span><span class="tile__u">days')
        self.assertIn("Email Sam the slides", body)
        self.assertIn('class="tag prio--high">High', body)

    def test_recent_lists_recordings_with_gist_and_filed_moments(self):
        sid = self._recording()
        self._store().add_artifact(sid, "summary", json.dumps(
            {"text": "How to find eigenvalues. And more.", "sections": [], "key_points": []}))
        self.app.config["AFTERWORD_TASKS_SERVICE"].create(
            title="Read chapter 5", kind="task", source="extracted", source_ref=sid, source_offset_ms=2480000)
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("Eigenvalues and diagonalisation", body)
        self.assertIn("How to find eigenvalues.", body)
        self.assertIn("41:20", body)
        self.assertIn(f"/lectures?session={sid}", body)

    def test_week_can_move(self):
        self.assertEqual(self.client.get("/?week=-1").status_code, 200)
        self.assertEqual(self.client.get("/?week=nope").status_code, 200)

    def test_processing_count(self):
        self._recording(status="summarising")
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("1 recording processing", body)
