"""Tests for the CoursesService and the related API.

Storage helpers (list_sessions_by_course, set_session_course,
count_sessions_by_course, get_artifact) are exercised via the
service tests and via the route tests below.
"""

from __future__ import annotations

import json
import unittest
import uuid

from tests.conftest import LRTestCase

# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class CoursesServiceTests(LRTestCase):
    def _service(self):
        return self.app.config["AFTERWORD_COURSES_SERVICE"]

    def test_create_persists_defaults(self):
        c = self._service().create(name="Biology EP3456")
        self.assertEqual(c.name, "Biology EP3456")
        self.assertIsNone(c.code)
        self.assertEqual(c.color, "1")  # the first free palette colour
        self.assertEqual(c.sort_order, 0)

    def test_new_courses_take_the_next_unused_colour(self):
        svc = self._service()
        svc.create(name="A", color="2")
        self.assertEqual(svc.create(name="B").color, "1")
        self.assertEqual(svc.create(name="C").color, "3")

    def test_create_persists_each_field(self):
        c = self._service().create(
            name="Calc II",
            code="MATH2341",
            color=7,
            sort_order=5,
        )
        self.assertEqual(c.name, "Calc II")
        self.assertEqual(c.code, "MATH2341")
        self.assertEqual(c.color, "7")
        self.assertEqual(c.sort_order, 5)

    def test_an_old_hex_colour_maps_to_the_nearest_slot(self):
        c = self._service().create(name="X", color="#6AA7FF")
        self.assertEqual(c.color, "1")

    def test_create_rejects_blank_name(self):
        with self.assertRaises(ValueError):
            self._service().create(name="   ")
        with self.assertRaises(ValueError):
            self._service().create(name="")

    def test_create_rejects_too_long_name(self):
        with self.assertRaises(ValueError):
            self._service().create(name="x" * 201)

    def test_create_rejects_bad_color(self):
        for bad in ("red", "#xyzxyz", "#1234", "#aabbccdd", "aabbcc", "0", "13", True):
            with self.assertRaises(ValueError):
                self._service().create(name="X", color=bad)

    def test_create_accepts_3_digit_hex_color(self):
        c = self._service().create(name="X", color="#abc")
        self.assertIn(c.color, [str(n) for n in range(1, 13)])

    def test_create_rejects_negative_sort_order(self):
        with self.assertRaises(ValueError):
            self._service().create(name="X", sort_order=-1)

    def test_create_string_sort_order_coerced(self):
        c = self._service().create(name="X", sort_order="3")
        self.assertEqual(c.sort_order, 3)

    def test_create_invalid_string_sort_order_raises(self):
        with self.assertRaises(ValueError):
            self._service().create(name="X", sort_order="abc")

    def test_create_bool_sort_order_rejected(self):
        # Bool is a subclass of int but shouldn't be accepted here.
        with self.assertRaises(ValueError):
            self._service().create(name="X", sort_order=True)

    def test_update_partial_fields(self):
        c = self._service().create(name="Old")
        updated = self._service().update(
            c.id,
            code="CODE101",
            color="12",
            sort_order=10,
        )
        self.assertEqual(updated.name, "Old")
        self.assertEqual(updated.code, "CODE101")
        self.assertEqual(updated.color, "12")
        self.assertEqual(updated.sort_order, 10)

    def test_update_clears_optional_fields_with_none(self):
        c = self._service().create(
            name="X",
            code="C",
            color="3",
        )
        cleared = self._service().update(
            c.id,
            code=None,
            color=None,
        )
        self.assertIsNone(cleared.code)
        self.assertIsNone(cleared.color)

    def test_old_hex_colours_become_slots_on_start(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        c = self._service().create(name="X")
        with store._tx() as conn:
            conn.execute("UPDATE courses SET color = '#3cc7b0' WHERE id = ?", (c.id,))
        store.initialise()
        self.assertEqual(self._service().get(c.id).color, "3")

    def test_update_missing_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self._service().update("nope", name="X")

    def test_update_validates_inputs(self):
        c = self._service().create(name="X")
        with self.assertRaises(ValueError):
            self._service().update(c.id, name="")
        with self.assertRaises(ValueError):
            self._service().update(c.id, color="not-a-color")

    def test_delete_removes_row(self):
        c = self._service().create(name="X")
        self.assertTrue(self._service().delete(c.id))
        self.assertIsNone(self._service().get(c.id))
        self.assertFalse(self._service().delete(c.id))

    def test_get_returns_none_for_missing(self):
        self.assertIsNone(self._service().get("nope"))

    def test_list_orders_by_sort_order_then_created(self):
        self._service().create(name="Z", sort_order=5)
        self._service().create(name="A", sort_order=0)
        self._service().create(name="M", sort_order=0)
        names = [x.name for x in self._service().list()[0]]
        self.assertEqual(names, ["A", "M", "Z"])

    def test_list_respects_limit(self):
        for i in range(3):
            self._service().create(name=f"C{i}")
        items, total = self._service().list(limit=2)
        self.assertEqual(total, 3)
        self.assertEqual(len(items), 2)

    def test_list_validates_inputs(self):
        with self.assertRaises(ValueError):
            self._service().list(limit=0)
        with self.assertRaises(ValueError):
            self._service().list(offset=-1)


def _seed_session(
    self,
    *,
    status: str = "ready",
    kind: str = "lecture",
    course_id: object = None,
) -> str:
    sid = uuid.uuid4().hex
    store = self.app.config["AFTERWORD_STORAGE"]
    store.upsert_session(
        sid,
        title="test",
        expected_count=1,
        sample_rate=22050,
        channels=1,
        bits_per_sample=32,
        started_at="2026-01-15T10:00:00Z",
    )
    if status != "receiving" or kind != "lecture" or course_id is not None:
        with store._tx() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, kind = ?, course_id = ? WHERE id = ?",
                (status, kind, course_id, sid),
            )
    return sid


class CourseSessionRelationTests(LRTestCase):
    def _store(self):
        return self.app.config["AFTERWORD_STORAGE"]

    def _svc(self):
        return self.app.config["AFTERWORD_COURSES_SERVICE"]

    def test_set_session_course_assigns(self):
        c = self._svc().create(name="X")
        sid = _seed_session(self)
        sess = self._store().set_session_course(sid, c.id)
        self.assertEqual(sess.course_id, c.id)

    def test_set_session_course_unassigns_with_none(self):
        c = self._svc().create(name="X")
        sid = _seed_session(self)
        self._store().set_session_course(sid, c.id)
        sess = self._store().set_session_course(sid, None)
        self.assertIsNone(sess.course_id)

    def test_set_session_course_missing_session_raises(self):
        c = self._svc().create(name="X")
        with self.assertRaises(KeyError):
            self._store().set_session_course("nope", c.id)

    def test_set_session_course_missing_course_raises(self):
        sid = _seed_session(self)
        with self.assertRaises(ValueError):
            self._store().set_session_course(sid, "nope")

    def test_list_sessions_by_course_returns_only_matching(self):
        c = self._svc().create(name="X")
        a = _seed_session(self, course_id=c.id)
        b = _seed_session(self, course_id=None)
        rows = self._store().list_sessions_by_course(c.id)
        ids = {r.id for r in rows}
        self.assertIn(a, ids)
        self.assertNotIn(b, ids)

    def test_list_sessions_by_course_with_none_returns_uncategorised(self):
        a = _seed_session(self, course_id=None)
        b = _seed_session(self, course_id=None)
        # This third one is status='receiving' so it must NOT match
        # the default filter. Use it to verify the filter excludes
        # non-ready sessions.
        c = _seed_session(self, status="receiving", course_id=None)
        rows = self._store().list_sessions_by_course(None)
        ids = {r.id for r in rows}
        self.assertIn(a, ids)
        self.assertIn(b, ids)
        self.assertNotIn(c, ids)

    def test_count_sessions_by_course(self):
        c = self._svc().create(name="X")
        _seed_session(self, course_id=c.id)
        _seed_session(self, course_id=c.id)
        _seed_session(self, course_id=None)
        self.assertEqual(
            self._store().count_sessions_by_course(c.id),
            2,
        )


class GetArtifactTests(LRTestCase):
    def test_get_artifact_returns_none_when_empty(self):
        sid = uuid.uuid4().hex
        self.app.config["AFTERWORD_STORAGE"].upsert_session(
            sid,
            title="t",
            expected_count=1,
            sample_rate=22050,
            channels=1,
            bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        self.assertIsNone(
            self.app.config["AFTERWORD_STORAGE"].get_artifact(sid, "summary"),
        )

    def test_get_artifact_returns_record_when_present(self):
        sid = uuid.uuid4().hex
        store = self.app.config["AFTERWORD_STORAGE"]
        store.upsert_session(
            sid,
            title="t",
            expected_count=1,
            sample_rate=22050,
            channels=1,
            bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO artifacts (id, session_id, kind, content, "
                "model, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("a1", sid, "summary", "Hello world", "test-model", "2026-01-15T10:00:00Z"),
            )
        art = store.get_artifact(sid, "summary")
        self.assertIsNotNone(art)
        self.assertEqual(art["content"], "Hello world")
        self.assertEqual(art["model"], "test-model")
        self.assertEqual(art["kind"], "summary")


class CoursesApiTests(LRTestCase):
    def _create(self, **payload):
        return self.client.post(
            "/api/v1/courses",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_create_returns_201(self):
        resp = self._create(name="Calc II")
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertEqual(body["course"]["name"], "Calc II")
        self.assertIn("id", body["course"])

    def test_create_validates_name(self):
        resp = self._create(name="")
        self.assertEqual(resp.status_code, 400)

    def test_create_validates_color(self):
        resp = self._create(name="X", color="not-a-color")
        self.assertEqual(resp.status_code, 400)

    def test_list_returns_total(self):
        self._create(name="A")
        self._create(name="B")
        resp = self.client.get("/api/v1/courses")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["courses"]), 2)

    def test_list_rejects_bad_limit(self):
        resp = self.client.get("/api/v1/courses?limit=abc")
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get("/api/v1/courses?limit=0")
        self.assertEqual(resp.status_code, 400)

    def test_get_missing_returns_404(self):
        resp = self.client.get("/api/v1/courses/nope")
        self.assertEqual(resp.status_code, 404)

    def test_update_partial(self):
        c = self._create(name="Old").get_json()["course"]
        resp = self.client.patch(
            f"/api/v1/courses/{c['id']}",
            data=json.dumps({"code": "EP101"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["course"]["code"], "EP101")

    def test_update_missing_returns_404(self):
        resp = self.client.patch(
            "/api/v1/courses/nope",
            data=json.dumps({"name": "X"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_delete_returns_204(self):
        c = self._create(name="X").get_json()["course"]
        resp = self.client.delete(f"/api/v1/courses/{c['id']}")
        self.assertEqual(resp.status_code, 204)

    def test_delete_missing_returns_404(self):
        resp = self.client.delete("/api/v1/courses/nope")
        self.assertEqual(resp.status_code, 404)


class SessionCourseAssignApiTests(LRTestCase):
    def _seed(self):
        sid = uuid.uuid4().hex
        self.app.config["AFTERWORD_STORAGE"].upsert_session(
            sid,
            title="t",
            expected_count=1,
            sample_rate=22050,
            channels=1,
            bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        return sid

    def test_assign_course_to_session(self):
        sid = self._seed()
        c = self.client.post(
            "/api/v1/courses",
            data=json.dumps({"name": "X"}),
            content_type="application/json",
        ).get_json()["course"]
        resp = self.client.post(
            f"/api/v1/sessions/{sid}/course",
            data=json.dumps({"course_id": c["id"]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.get_json()["session"]["course_id"],
            c["id"],
        )

    def test_unassign_course(self):
        sid = self._seed()
        resp = self.client.post(
            f"/api/v1/sessions/{sid}/course",
            data=json.dumps({"course_id": None}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()["session"]["course_id"])

    def test_assign_unknown_course_returns_400(self):
        sid = self._seed()
        resp = self.client.post(
            f"/api/v1/sessions/{sid}/course",
            data=json.dumps({"course_id": "nope"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_assign_unknown_session_returns_404(self):
        resp = self.client.post(
            "/api/v1/sessions/nope/course",
            data=json.dumps({"course_id": None}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_assign_requires_course_id_field(self):
        sid = self._seed()
        resp = self.client.post(
            f"/api/v1/sessions/{sid}/course",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)


class LecturesUiTests(LRTestCase):
    def test_lectures_page_has_list_reader_and_course_settings(self):
        body = self.client.get("/lectures").get_data(as_text=True)
        self.assertIn('class="rpage', body)
        self.assertIn('class="lcol"', body)
        self.assertIn('href="/courses"', body)  # the small gear
        self.assertIn("No lectures yet", body)

    def test_lectures_are_grouped_by_course(self):
        svc = self.app.config["AFTERWORD_COURSES_SERVICE"]
        calc = svc.create(name="Calc II", code="MBX101")
        svc.create(name="Biology EP3456")
        _seed_session(self, status="done", course_id=calc.id)
        body = self.client.get("/lectures").get_data(as_text=True)
        self.assertIn("Calc II", body)
        self.assertIn("Biology EP3456", body)
        self.assertIn("Calc II · Lecture 1", body)  # the reader's chip
        body = self.client.get("/lectures?mode=date").get_data(as_text=True)
        self.assertIn("January 2026", body)

    def test_lectures_opens_the_selected_one(self):
        svc = self.app.config["AFTERWORD_COURSES_SERVICE"]
        course = svc.create(name="X")
        sid = _seed_session(self, course_id=course.id)
        body = self.client.get(f"/lectures?session={sid}").get_data(as_text=True)
        self.assertIn(f'data-reader="{sid}"', body)
        self.assertIn('class="rpage is-open"', body)  # full screen on the phone

    def test_without_a_choice_the_newest_opens_on_desktop_only(self):
        sid = _seed_session(self, status="done")
        body = self.client.get("/lectures").get_data(as_text=True)
        self.assertIn(f'data-reader="{sid}"', body)
        self.assertIn("is-selected is-auto", body)  # the phone shows the list
        self.assertNotIn('class="rpage is-open"', body)


class MeetingsUiTests(LRTestCase):
    def test_meetings_page_renders(self):
        body = self.client.get("/meetings").get_data(as_text=True)
        self.assertIn('class="rpage', body)
        self.assertIn("No meetings yet", body)
        self.assertIn("Nothing to show yet", body)

    def test_unknown_meeting_opens_the_newest(self):
        sid = _seed_session(self, status="done", kind="meeting")
        body = self.client.get("/meetings?session=nope").get_data(as_text=True)
        self.assertIn(f'data-reader="{sid}"', body)


class CoursesSettingsUiTests(LRTestCase):
    def test_settings_lists_courses_with_colours(self):
        svc = self.app.config["AFTERWORD_COURSES_SERVICE"]
        svc.create(name="Algebra", code="MA101", color="3")
        svc.create(name="Biology")
        body = self.client.get("/courses").get_data(as_text=True)
        self.assertIn('value="Algebra"', body)
        self.assertIn('value="MA101"', body)
        self.assertIn("crow cc-3", body)
        self.assertIn("0 lectures", body)
        self.assertIn("data-course-add", body)

    def test_settings_empty(self):
        body = self.client.get("/courses").get_data(as_text=True)
        self.assertIn("No courses yet", body)

    def test_old_address_redirects(self):
        resp = self.client.get("/courses/admin")
        self.assertEqual(resp.status_code, 301)
        self.assertTrue(resp.headers["Location"].endswith("/courses"))


class CoursesServiceIsRealTests(unittest.TestCase):
    def test_courses_service_init_signature(self):
        import inspect

        from services.courses import CoursesService

        sig = inspect.signature(CoursesService.__init__)
        self.assertGreaterEqual(
            len(sig.parameters),
            2,
            "CoursesService.__init__ should take (self, store)",
        )


if __name__ == "__main__":
    unittest.main()


class OtherUiTests(LRTestCase):
    def _finished(self, kind, status="done", detail=None):
        sid = _seed_session(self, status=status, kind=kind)
        store = self.app.config["AFTERWORD_STORAGE"]
        store.full_audio_path(sid).write_bytes(self.make_wav(b"\x00" * 256))
        if detail:
            with store._tx() as conn:
                conn.execute(
                    "UPDATE sessions SET status_detail = ? WHERE id = ?",
                    (detail, sid),
                )
        return sid

    def test_lists_other_and_unclassified_finished(self):
        other = self._finished("other")
        silent = self._finished(None, detail="no speech detected")
        broken = self._finished(None, status="failed", detail="boom")
        lecture = self._finished("lecture")
        memo = self._finished("voice_memo")
        body = self.client.get("/other").get_data(as_text=True)
        for sid in (other, silent, broken):
            self.assertIn(f"?session={sid}", body)
        for sid in (lecture, memo):
            self.assertNotIn(f"?session={sid}", body)
        self.assertIn("Not sorted", body)
        detail = self.client.get(f"/other?session={silent}").get_data(as_text=True)
        self.assertIn("No speech detected.", detail)
        self.assertIn(f'data-rec-menu data-id="{silent}"', detail)  # Move to sorts it
        failed = self.client.get(f"/other?session={broken}").get_data(as_text=True)
        self.assertIn("Processing failed", failed)
        self.assertIn(f'data-retry="{broken}"', failed)

    def test_classify_moves_and_rewrites_summary(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = self._finished("other")
        store.add_artifact(sid, "transcript", "some lecture talk")
        store.add_artifact(sid, "summary", '{"text": "old other summary"}')
        resp = self.client.post(f"/api/v1/sessions/{sid}/kind", json={"kind": "lecture"})
        self.assertEqual(resp.status_code, 200)
        sess = store.get_session(sid)
        self.assertEqual(sess.kind, "lecture")
        self.assertEqual(sess.status, "done")
        # The summary was written again (no-op provider in tests).
        self.assertNotIn("old other summary", store.get_artifact(sid, "summary")["content"])

    def test_classify_as_memo_goes_to_notes_without_summary(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = self._finished("other")
        store.add_artifact(sid, "transcript", "[00:00] Speaker 1: buy milk")
        store.add_artifact(sid, "summary", '{"text": "old"}')
        self.client.post(f"/api/v1/sessions/{sid}/kind", json={"kind": "voice_memo"})
        self.assertIsNone(store.get_artifact(sid, "summary"))
        self.assertEqual(store.get_session(sid).status, "done")
        note = store.note_for_recording(sid)
        self.assertEqual(note.body_md, "buy milk")

    def test_classify_rejects_unknown_kind(self):
        sid = self._finished("other")
        resp = self.client.post(f"/api/v1/sessions/{sid}/kind", json={"kind": "x"})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/v1/sessions/nope/kind", json={"kind": "other"})
        self.assertEqual(resp.status_code, 404)


class NotesVoiceMemoTests(LRTestCase):
    def _memo(self, text="[00:01] Speaker 1: remember to buy milk"):
        store = self.app.config["AFTERWORD_STORAGE"]
        memo = _seed_session(self, status="done", kind="voice_memo")
        store.full_audio_path(memo).write_bytes(self.make_wav(b"\x00" * 256))
        store.add_artifact(memo, "transcript", text)
        from services.notes import ensure_memo_note

        return memo, ensure_memo_note(store, memo)

    def test_a_memo_is_a_note_with_audio(self):
        memo, note = self._memo()
        self.assertEqual((note.source, note.source_ref), ("voice_memo", memo))
        self.assertEqual(note.body_md, "remember to buy milk")
        body = self.client.get(f"/notes?note_id={note.id}").get_data(as_text=True)
        self.assertIn("<audio", body)
        self.assertIn("What was said", body)
        self.assertIn('data-seek="1000"', body)
        self.assertIn(f'data-recording="{memo}"', body)  # Move to works on it

    def test_memo_links_open_its_note(self):
        memo, note = self._memo()
        with self.app.test_request_context():
            from web.common import recording_url

            self.assertEqual(recording_url(memo), f"/notes?note_id={note.id}")

    def test_deleting_the_note_deletes_the_recording_and_back(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        memo, note = self._memo()
        self.assertEqual(self.client.delete(f"/api/v1/notes/{note.id}").status_code, 204)
        self.assertIsNone(store.get_session(memo))
        memo2, note2 = self._memo()
        store.delete_session(memo2)
        self.assertIsNone(store.get_note(note2.id))

    def test_moving_a_memo_away_removes_its_note(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        memo, note = self._memo()
        self.client.post(f"/api/v1/sessions/{memo}/kind", json={"kind": "lecture"})
        self.assertIsNone(store.get_note(note.id))
        self.assertEqual(store.get_session(memo).kind, "lecture")

    def test_memos_made_before_get_their_notes_on_start(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        memo = _seed_session(self, status="done", kind="voice_memo")
        from services.notes import sync_memo_notes

        self.assertEqual(sync_memo_notes(store), 1)
        self.assertEqual(sync_memo_notes(store), 0)
        self.assertIsNotNone(store.note_for_recording(memo))


class RecordingNameTests(LRTestCase):
    def test_device_name_shows_as_kind_with_date_line(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = _seed_session(self, status="done", kind="meeting")
        with store._tx() as conn:
            conn.execute("UPDATE sessions SET title = 'rec_2026-01-15T1000' WHERE id = ?", (sid,))
        body = self.client.get("/meetings").get_data(as_text=True)
        self.assertNotIn("rec_2026-01-15T1000", body)
        self.assertIn('class="lrow__title">Meeting<', body)
        self.assertIn("Thu 15 Jan 2026, 10:00", body)

    def test_rename_by_hand(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = _seed_session(self, status="done", kind="meeting")
        resp = self.client.post(f"/api/v1/sessions/{sid}/title", json={"title": "  Budget review "})
        self.assertEqual(resp.status_code, 200)
        sess = store.get_session(sid)
        self.assertEqual((sess.title, sess.title_source), ("Budget review", "user"))
        detail = self.client.get(f"/meetings?session={sid}").get_data(as_text=True)
        self.assertIn("Budget review", detail)
        self.assertIn("data-rename", detail)
        self.assertEqual(
            self.client.post(f"/api/v1/sessions/{sid}/title", json={"title": " "}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/v1/sessions/nope/title", json={"title": "x"}).status_code, 404
        )


class ClickableTranscriptTests(LRTestCase):
    TRANSCRIPT = (
        "[00:05] Speaker 1: Welcome back.\n"
        "[1:02:03] Speaker 2: Any questions?\n"
        "a line without a marker"
    )

    def test_markers_become_play_buttons(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = _seed_session(self, status="done", kind="meeting")
        store.add_artifact(sid, "transcript", self.TRANSCRIPT)
        body = self.client.get(f"/meetings?session={sid}").get_data(as_text=True)
        self.assertIn('data-seek="5000"', body)
        self.assertIn('data-t="3723"', body)  # 1:02:03
        self.assertIn('<span class="tline__who">Speaker 2</span>', body)
        self.assertIn("a line without a marker", body)

    def test_recording_page_has_the_transcript_and_player(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = _seed_session(self, status="done", kind="meeting")
        store.full_audio_path(sid).write_bytes(self.make_wav(b"\x00" * 256))
        store.add_artifact(sid, "transcript", self.TRANSCRIPT)
        body = self.client.get(f"/meetings?session={sid}&t=5000").get_data(as_text=True)
        self.assertEqual(body.count("<audio"), 1)
        self.assertIn('data-t="5"', body)


class DeleteExportTests(LRTestCase):
    def _recording(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = _seed_session(self, status="done", kind="lecture")
        store.full_audio_path(sid).write_bytes(self.make_wav(b"\x00" * 256))
        store.set_session_title(sid, "Entropy and the second law", source="ai")
        store.add_artifact(
            sid,
            "summary",
            json.dumps(
                {
                    "title": "Entropy and the second law",
                    "text": "Overview.",
                    "sections": [{"heading": "Entropy", "text": "Notes."}],
                    "key_points": ["Point one"],
                }
            ),
        )
        store.add_artifact(sid, "transcript", "[00:05] Speaker 1: Hello.")
        from services.tasks import TasksService

        task = TasksService(store).create(
            title="Submit essay",
            kind="deadline",
            due_at="2026-01-23",
            source="extracted",
            source_ref=sid,
            source_offset_ms=5000,
        )
        return store, sid, task

    def test_export_markdown(self):
        _store, sid, _ = self._recording()
        resp = self.client.get(f"/sessions/{sid}/export.md")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "2026-01-15 Entropy and the second law.md", resp.headers["Content-Disposition"]
        )
        md = resp.get_data(as_text=True)
        for part in (
            "# Entropy and the second law",
            "## Summary",
            "Overview.",
            "### Entropy",
            "## Key points",
            "- Point one",
            "- Deadline: Submit essay (due 2026-01-2",
            "## Transcript",
            "[00:05] Speaker 1: Hello.",
        ):
            self.assertIn(part, md)
        self.assertEqual(self.client.get("/sessions/nope/export.md").status_code, 404)

    def test_reader_has_the_actions_menu(self):
        _, sid, _ = self._recording()
        body = self.client.get(f"/lectures?session={sid}").get_data(as_text=True)
        self.assertIn(f'data-rec-menu data-id="{sid}"', body)
        self.assertIn('data-audio="1"', body)
        self.assertIn('data-seek="5000"', body)  # "Added to your lists"
        self.assertIn("Deadline · Submit essay", body)

    def test_delete_keeps_filed_items_unlinked(self):
        store, sid, task = self._recording()
        import search

        self.assertEqual(len(search.search(store, "entropy")["recording"]), 1)
        folder = store.session_dir(sid)
        self.assertTrue(folder.exists())
        resp = self.client.delete(f"/api/v1/sessions/{sid}")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(store.get_session(sid))
        self.assertFalse(folder.exists())
        import search

        self.assertEqual(search.search(store, "entropy")["recording"], [])
        from services.tasks import TasksService

        kept = TasksService(store).get(task.id)
        self.assertEqual(kept.title, "Submit essay")
        self.assertIsNone(kept.source_ref)
        self.assertEqual(self.client.delete(f"/api/v1/sessions/{sid}").status_code, 404)

    def test_delete_refused_while_processing(self):
        from web.pipeline import _PIPELINES_IN_FLIGHT

        _, sid, _ = self._recording()
        _PIPELINES_IN_FLIGHT.add(sid)
        try:
            self.assertEqual(self.client.delete(f"/api/v1/sessions/{sid}").status_code, 409)
        finally:
            _PIPELINES_IN_FLIGHT.discard(sid)
        self.assertIsNotNone(self.app.config["AFTERWORD_STORAGE"].get_session(sid))
