"""API-layer tests using Flask's test client."""
from __future__ import annotations

import json
import unittest

from tests.conftest import LRTestCase


class DeviceFlowTests(LRTestCase):

    def test_announce_then_list_then_finish(self) -> None:
        # The no-op providers (no API keys in the test config)
        # return stub data so the pipeline completes successfully
        # and the session ends at status='done'. The classifier
        # returns Classification('other', 0.0) so the kind is
        # 'other' (not 'lecture' as a real LLM would pick).
        # 1) announce
        r = self.client.post(
            "/api/v1/sessions",
            data=json.dumps({
                "id": "abcdef1234567890",
                "title": "CS101 Lecture 1",
                "expected_count": 2,
                "sample_rate": 22050,
                "channels": 1,
                "bits_per_sample": 32,
                "started_at": "2026-10-03T12:00:00Z",
            }),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 201)
        data = r.get_json()
        self.assertTrue(data["created"])
        self.assertEqual(data["session"]["status"], "receiving")

        # 2) upload chunks
        wav1 = self.make_wav(b"AAA" * 100)
        wav2 = self.make_wav(b"BBB" * 100)
        r = self.client.put(
            "/api/v1/sessions/abcdef1234567890/chunks/1",
            data=wav1, content_type="audio/wav",
        )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.client.put(
            "/api/v1/sessions/abcdef1234567890/chunks/2",
            data=wav2, content_type="audio/wav",
        )
        self.assertEqual(r.status_code, 200)

        # 3) list shows the session
        r = self.client.get("/api/v1/sessions")
        self.assertEqual(r.status_code, 200)
        listed = r.get_json()["sessions"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["title"], "CS101 Lecture 1")

        # 4) finish → pipeline runs end-to-end → status='done'
        r = self.client.post("/api/v1/sessions/abcdef1234567890/finish")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["session"]["status"], "done")
        # No-op classifier returns 'other' (low confidence).
        self.assertEqual(body["session"]["kind"], "other")
        self.assertIsNotNone(body["session"]["full_audio_url"])

    def test_finish_with_missing_chunks_returns_409(self) -> None:
        self.client.post(
            "/api/v1/sessions",
            data=json.dumps({"id": "abcdef1234567890", "expected_count": 3}),
            content_type="application/json",
        )
        self.client.put(
            "/api/v1/sessions/abcdef1234567890/chunks/1",
            data=self.make_wav(),
        )
        r = self.client.post("/api/v1/sessions/abcdef1234567890/finish")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["error"], "missing_chunks")
        self.assertEqual(sorted(body["missing"]), [2, 3])
        self.assertEqual(body["present"], [1])

    def test_chunk_reput_is_safe(self) -> None:
        self.client.post(
            "/api/v1/sessions",
            data=json.dumps({"id": "abcdef1234567890", "expected_count": 1}),
            content_type="application/json",
        )
        a = self.make_wav(b"AAAA")
        b = self.make_wav(b"BBBB")
        r1 = self.client.put(
            "/api/v1/sessions/abcdef1234567890/chunks/1",
            data=a, content_type="audio/wav",
        )
        r2 = self.client.put(
            "/api/v1/sessions/abcdef1234567890/chunks/1",
            data=b, content_type="audio/wav",
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        r = self.client.post("/api/v1/sessions/abcdef1234567890/finish")
        self.assertEqual(r.status_code, 200)
        audio = self.client.get("/api/v1/sessions/abcdef1234567890/audio")
        self.assertEqual(audio.status_code, 200)
        self.assertIn(b"BBBB", audio.get_data())

    def test_out_of_order_uploads(self) -> None:
        self.client.post(
            "/api/v1/sessions",
            data=json.dumps({"id": "abcdef1234567890", "expected_count": 3}),
            content_type="application/json",
        )
        for i in (2, 1, 3):
            self.client.put(
                f"/api/v1/sessions/abcdef1234567890/chunks/{i}",
                data=self.make_wav(bytes([i]) * 100),
            )
        r = self.client.post("/api/v1/sessions/abcdef1234567890/finish")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_unknown_session_404(self) -> None:
        r = self.client.post("/api/v1/sessions/nope/finish")
        self.assertEqual(r.status_code, 404)
        r = self.client.put(
            "/api/v1/sessions/nope/chunks/1", data=self.make_wav(),
        )
        self.assertEqual(r.status_code, 404)

    def test_audio_serves_with_range_support(self) -> None:
        self._upload_complete_session()
        r = self.client.get("/api/v1/sessions/abcdef1234567890/audio")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "audio/wav")
        r2 = self.client.get(
            "/api/v1/sessions/abcdef1234567890/audio",
            headers={"Range": "bytes=0-99"},
        )
        self.assertEqual(r2.status_code, 206)
        self.assertEqual(len(r2.get_data()), 100)

    def test_audio_409_when_not_ready(self) -> None:
        self.client.post(
            "/api/v1/sessions",
            data=json.dumps({"id": "abcdef1234567890", "expected_count": 2}),
            content_type="application/json",
        )
        r = self.client.get("/api/v1/sessions/abcdef1234567890/audio")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["status"], "receiving")

    def _upload_complete_session(self) -> None:
        self.client.post(
            "/api/v1/sessions",
            data=json.dumps({
                "id": "abcdef1234567890",
                "title": "Test",
                "expected_count": 2,
            }),
            content_type="application/json",
        )
        for i in (1, 2):
            self.client.put(
                f"/api/v1/sessions/abcdef1234567890/chunks/{i}",
                data=self.make_wav(bytes([i]) * 200),
            )
        self.client.post("/api/v1/sessions/abcdef1234567890/finish")


class TranscribeRouteTests(LRTestCase):
    """Tests for POST /api/v1/sessions/<id>/transcribe.

    Milestone 8 manual-retry endpoint. Re-runs the post-finish
    pipeline on a session that's already in 'ready' (or 'done'
    / 'failed' / 'transcribed' / 'classifying'). All tests use
    stub providers; no real HTTP is performed.
    """

    def _stub_providers(self, transcript="t", kind="meeting", conf=0.7,
                        fail_classify=False):
        from services.llm import Classification, LLMProvider

        class StubT(LLMProvider):
            name = "stub-t"

            def transcribe(self, audio_path):
                return transcript

            def classify(self, transcript):
                raise RuntimeError("not used")

        class StubC(LLMProvider):
            name = "stub-c"

            def classify(self, t):
                if fail_classify:
                    from services.llm import LLMError
                    raise LLMError("simulated")
                return Classification(kind=kind, confidence=conf)

            def summarize(self, transcript):
                # M9: stub returns an empty Summary so the pipeline
                # can complete without an LLM.
                from services.llm import Summary
                return Summary(text="", key_points=[])

            def extract(self, transcript, kind):
                # M9: stub returns an empty list.
                return []

            def transcribe(self, audio_path):
                raise RuntimeError("not used")

        self.app.config["LR_LLM_PROVIDERS"] = {
            "transcript": StubT(),
            "classifier": StubC(),
        }

    def _make_ready_session(self, sid="trnsc0000001"):
        import storage
        store = self.app.config["LR_STORAGE"]
        store.upsert_session(
            sid, title="x", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        store.full_audio_path(sid).write_bytes(
            self.make_wav(b"\x00" * 256),
        )
        store.update_status(sid, storage.STATUS_STITCHING)
        store.update_status(
            sid, storage.STATUS_READY, finished_at=storage.now_iso(),
        )
        return sid

    def test_transcribe_runs_pipeline_on_ready(self):
        self._stub_providers(kind="lecture", conf=0.85)
        sid = self._make_ready_session()
        r = self.client.post(f"/api/v1/sessions/{sid}/transcribe")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["session"]["status"], "done")
        self.assertEqual(body["session"]["kind"], "lecture")

    def test_transcribe_404_for_unknown(self):
        self._stub_providers()
        r = self.client.post("/api/v1/sessions/nope/transcribe")
        self.assertEqual(r.status_code, 404)

    def test_transcribe_409_when_receiving(self):
        """The session must be in 'ready' (or a retry-able state)
        for the pipeline to run. If it's still 'receiving', the
        route returns 409."""
        self._stub_providers()
        import storage
        store = self.app.config["LR_STORAGE"]
        store.upsert_session(
            "trnsc0000002", title="x", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        r = self.client.post("/api/v1/sessions/trnsc0000002/transcribe")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "not_ready")

    def test_transcribe_retries_from_failed(self):
        """A session that previously failed can be re-run by
        POSTing /transcribe."""
        import storage
        from services.llm import Classification, LLMError, LLMProvider

        class Bad(LLMProvider):
            name = "bad"

            def transcribe(self, audio_path):
                return "x"

            def classify(self, transcript):
                raise LLMError("first run fails")

            def summarize(self, transcript):
                # M9: even on the "first run" failure path, the
                # pipeline calls summarise after classify. Have
                # it raise so the failure propagates.
                raise LLMError("first run fails")

            def extract(self, transcript, kind):
                raise LLMError("first run fails")

        self.app.config["LR_LLM_PROVIDERS"] = {
            "transcript": Bad(), "classifier": Bad(),
        }
        sid = self._make_ready_session("trnsc0000003")
        r1 = self.client.post(f"/api/v1/sessions/{sid}/transcribe")
        self.assertEqual(r1.status_code, 200)
        self.assertFalse(r1.get_json()["ok"])
        self.assertEqual(
            self.app.config["LR_STORAGE"].get_session(sid).status,
            storage.STATUS_FAILED,
        )
        # Swap in working providers and retry.
        class Good(LLMProvider):
            name = "good"

            def transcribe(self, audio_path):
                return "fixed"

            def classify(self, transcript):
                return Classification(
                    kind="other", confidence=0.5,
                )

            def summarize(self, transcript):
                # M9: empty Summary so the pipeline reaches done.
                from services.llm import Summary
                return Summary(text="", key_points=[])

            def extract(self, transcript, kind):
                # M9: no items.
                return []

        self.app.config["LR_LLM_PROVIDERS"] = {
            "transcript": Good(), "classifier": Good(),
        }
        r2 = self.client.post(f"/api/v1/sessions/{sid}/transcribe")
        self.assertEqual(r2.status_code, 200)
        body = r2.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["session"]["status"], "done")
        self.assertEqual(body["session"]["kind"], "other")

    def test_transcribe_classifier_failure_returns_200_with_ok_false(self):
        """When the LLM call fails, the route returns 200 with
        ``ok: false`` and a useful ``reason``/``detail``. The
        status code is 200 (not 5xx) so the UI can display the
        reason without special-case handling."""
        from services.llm import LLMError, LLMProvider

        class T(LLMProvider):
            name = "t"

            def transcribe(self, audio_path):
                return "transcript"

            def classify(self, transcript):
                raise RuntimeError("not used")

        class C(LLMProvider):
            name = "c"

            def classify(self, transcript):
                raise LLMError("upstream 503")

            def transcribe(self, audio_path):
                raise RuntimeError("not used")

        self.app.config["LR_LLM_PROVIDERS"] = {
            "transcript": T(), "classifier": C(),
        }
        sid = self._make_ready_session("trnsc0000004")
        r = self.client.post(f"/api/v1/sessions/{sid}/transcribe")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "classify_failed")
        self.assertIn("upstream 503", body["detail"])


class WebUITests(LRTestCase):

    def test_index_empty(self) -> None:
        r = self.client.get("/recordings")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"No recordings yet", r.get_data())

    def test_index_shows_session(self) -> None:
        self._upload_complete_session()
        r = self.client.get("/recordings")
        self.assertEqual(r.status_code, 200)
        body = r.get_data()
        self.assertIn(b"Test", body)
        # After M8, the no-op pipeline completes successfully and
        # the session ends at status='done' (rendered as "Done"
        # in the page). Real LLM keys would also land on 'done'.
        self.assertIn(b"Done", body)

    def test_dashboard_renders(self) -> None:
        """The home page (/) is the bento dashboard."""
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data()
        # Bento wrapper and all six panels.
        self.assertIn(b"lr-dashboard", body)
        for needle in (b"Calendar", b"Deadlines", b"Todos",
                       b"Notes", b"Lectures", b"Meetings"):
            self.assertIn(needle, body)
        # Each panel's empty-state copy.
        self.assertIn(b"Nothing scheduled", body)   # Calendar
        self.assertIn(b"Nothing due", body)           # Deadlines
        self.assertIn(b"All clear", body)             # Todos
        self.assertIn(b"No notes yet", body)          # Notes
        self.assertIn(b"No lectures yet", body)        # Lectures
        self.assertIn(b"No meetings recorded", body)  # Meetings
        # Search bar with the keyboard shortcut hint.
        self.assertIn(b"Search recordings", body)
        self.assertIn(b"lr-search__shortcut", body)
        # Nav has the new structure: Lectures, Meetings, Notes, Todos, Calendar.
        for nav_link in (b"Lectures", b"Meetings", b"Notes",
                         b"Todos", b"Calendar"):
            self.assertIn(nav_link, body)
        # Old nav items should NOT be there.
        self.assertNotIn(b"\">Home</a>", body)  # no nav link to Home (page is the Home)
        self.assertNotIn(b"\">Recs</a>", body)  # no nav link to Recs (removed earlier)
        # Deadlines card now points to /todos (unified page).
        self.assertIn(b"Open &rarr;", body)  # the deadlines card Open arrow link
        # Lectures card has a "View all" link to /lectures.
        self.assertIn(b'href="/lectures"', body)
        # Page wrapper has the with-gap modifier for the top breathing room.
        self.assertIn(b"lr-page--with-gap", body)
        # And the full modifier so it fills the viewport edge-to-edge.
        self.assertIn(b"lr-page--full", body)

    def test_dashboard_gap_token_defined(self) -> None:
        """The dashboard's top-gap token is defined in tokens.css and
        used by the layout. Static check so the visual stays consistent
        if someone refactors."""
        from pathlib import Path
        css_root = Path(__file__).resolve().parent.parent / "static" / "css"
        tokens = (css_root / "tokens.css").read_text(encoding="utf-8")
        layout = (css_root / "layout.css").read_text(encoding="utf-8")
        self.assertIn("--lr-pad-page-top:", tokens)
        self.assertIn("min(12vh, 112px)", tokens)
        self.assertIn("var(--lr-pad-page-top)", layout)
        # The full-width modifier and its edge token.
        self.assertIn("--lr-pad-edge:", tokens)
        self.assertIn(".lr-page--full", layout)
        self.assertIn("var(--lr-pad-edge)", layout)

    def test_session_page_contains_audio(self) -> None:
        self._upload_complete_session()
        r = self.client.get("/sessions/abcdef1234567890")
        self.assertEqual(r.status_code, 200)
        body = r.get_data()
        self.assertIn(b"<audio", body)
        self.assertIn(b"/api/v1/sessions/abcdef1234567890/audio", body)

    def test_session_page_404_for_unknown(self) -> None:
        r = self.client.get("/sessions/missing")
        self.assertEqual(r.status_code, 404)

    def test_todos_renders(self) -> None:
        r = self.client.get("/todos")
        self.assertEqual(r.status_code, 200)
        body = r.get_data()
        self.assertIn(b"Todos", body)
        # Both sub-sections visible.
        self.assertIn(b"Tasks", body)
        self.assertIn(b"Deadlines", body)

    def test_calendar_renders(self) -> None:
        r = self.client.get("/calendar")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Calendar", r.get_data())

    def test_unsorted_renders(self) -> None:
        r = self.client.get("/unsorted")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Sort recordings", r.get_data())

    def test_lectures_filter_works_end_to_end(self) -> None:
        """Set a session's kind to 'lecture' and verify it shows on
        /lectures but not on /meetings."""
        sid = "filtertest0001ab"
        self.client.post("/api/v1/sessions", data=json.dumps({
            "id": sid, "expected_count": 1,
        }), content_type="application/json")
        self.client.put(
            f"/api/v1/sessions/{sid}/chunks/1",
            data=self.make_wav(bytes([1]) * 200),
        )
        self.client.post(f"/api/v1/sessions/{sid}/finish")
        # Tag as lecture.
        self.client.post(
            f"/api/v1/sessions/{sid}/kind",
            data=json.dumps({"kind": "lecture"}),
            content_type="application/json",
        )
        # /lectures shows it.
        r = self.client.get("/lectures")
        self.assertIn(sid[:12].encode(), r.get_data())
        # /meetings does not.
        r2 = self.client.get("/meetings")
        self.assertNotIn(sid[:12].encode(), r2.get_data())

    def _upload_complete_session(self) -> None:
        self.client.post(
            "/api/v1/sessions",
            data=json.dumps({
                "id": "abcdef1234567890",
                "title": "Test",
                "expected_count": 2,
            }),
            content_type="application/json",
        )
        for i in (1, 2):
            self.client.put(
                f"/api/v1/sessions/abcdef1234567890/chunks/{i}",
                data=self.make_wav(bytes([i]) * 200),
            )
        self.client.post("/api/v1/sessions/abcdef1234567890/finish")


class SearchStubTests(LRTestCase):

    def test_search_returns_empty_hits(self) -> None:
        r = self.client.get("/api/v1/search?q=anything")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["hits"], [])

class EventsApiTests(LRTestCase):
    """HTTP surface for /api/v1/events/* (added in Milestone 5)."""

    def _base(self, **overrides):
        payload = {
            "title": "Lecture",
            "kind": "lecture",
            "starts_at": "2027-03-15T10:00:00Z",
            "ends_at": "2027-03-15T11:00:00Z",
        }
        payload.update(overrides)
        return payload

    def _create(self, **payload):
        return self.client.post(
            "/api/v1/events",
            data=json.dumps(self._base(**payload)),
            content_type="application/json",
        )

    def test_create_returns_201(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertEqual(body["event"]["title"], "Lecture")
        self.assertEqual(
            body["event"]["starts_at"], "2027-03-15T10:00:00Z",
        )

    def test_create_validates_title(self):
        resp = self._create(title="")
        self.assertEqual(resp.status_code, 400)

    def test_create_validates_kind(self):
        resp = self._create(kind="gathering")
        self.assertEqual(resp.status_code, 400)

    def test_create_validates_starts_at(self):
        resp = self._create(starts_at="not-a-date")
        self.assertEqual(resp.status_code, 400)

    def test_create_normalises_date_only(self):
        resp = self._create(starts_at="2027-03-15")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            resp.get_json()["event"]["starts_at"],
            "2027-03-15T00:00:00Z",
        )

    def test_list_returns_events_with_total(self):
        self._create(title="A")
        self._create(title="B")
        resp = self.client.get("/api/v1/events")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["events"]), 2)

    def test_list_filter_by_kind(self):
        self._create(title="L", kind="lecture")
        self._create(title="M", kind="meeting")
        resp = self.client.get("/api/v1/events?kind=lecture")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["events"][0]["title"], "L")

    def test_list_rejects_invalid_kind(self):
        resp = self.client.get("/api/v1/events?kind=garbage")
        self.assertEqual(resp.status_code, 400)

    def test_list_rejects_bad_limit(self):
        resp = self.client.get("/api/v1/events?limit=abc")
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get("/api/v1/events?limit=0")
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get("/api/v1/events?limit=9999")
        self.assertEqual(resp.status_code, 400)

    def test_get_returns_one(self):
        create = self._create().get_json()
        resp = self.client.get(
            f"/api/v1/events/{create['event']['id']}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.get_json()["event"]["id"], create["event"]["id"],
        )

    def test_get_missing_returns_404(self):
        resp = self.client.get("/api/v1/events/nope")
        self.assertEqual(resp.status_code, 404)

    def test_update_partial(self):
        create = self._create().get_json()
        resp = self.client.patch(
            f"/api/v1/events/{create['event']['id']}",
            data=json.dumps({"title": "Calc III"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["event"]["title"], "Calc III")

    def test_update_clear_ends_at(self):
        create = self._create().get_json()
        resp = self.client.patch(
            f"/api/v1/events/{create['event']['id']}",
            data=json.dumps({"ends_at": None}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()["event"]["ends_at"])

    def test_update_missing_returns_404(self):
        resp = self.client.patch(
            "/api/v1/events/nope",
            data=json.dumps({"title": "X"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_delete_returns_204(self):
        create = self._create().get_json()
        resp = self.client.delete(
            f"/api/v1/events/{create['event']['id']}"
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(
            self.client.get(
                f"/api/v1/events/{create['event']['id']}"
            ).status_code,
            404,
        )

    def test_import_requires_file_field(self):
        resp = self.client.post("/api/v1/events/import")
        self.assertEqual(resp.status_code, 400)

    def test_import_counts_events(self):
        import io as _io
        sample = (
            b"BEGIN:VCALENDAR\r\n"
            b"VERSION:2.0\r\n"
            b"BEGIN:VEVENT\r\n"
            b"UID:t1\r\nSUMMARY:Test 1\r\n"
            b"DTSTART:20270315T100000Z\r\n"
            b"DTEND:20270315T110000Z\r\n"
            b"END:VEVENT\r\n"
            b"BEGIN:VEVENT\r\n"
            b"UID:t2\r\nSUMMARY:Test 2\r\n"
            b"DTSTART:20270316T100000Z\r\n"
            b"END:VEVENT\r\n"
            b"END:VCALENDAR\r\n"
        )
        resp = self.client.post(
            "/api/v1/events/import",
            data={"file": (_io.BytesIO(sample), "cal.ics")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["imported"], 2)

if __name__ == "__main__":
    unittest.main()
