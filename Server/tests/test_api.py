"""API-layer tests using Flask's test client."""
from __future__ import annotations

import json
import time
import unittest

import storage
from app import _enqueue_pipeline, sweep_stranded_sessions
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
        audio.close()

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
        self.assertEqual(r.mimetype, "audio/aac")
        r.close()   # the file is streamed; close it so it isn't left open
        r2 = self.client.get(
            "/api/v1/sessions/abcdef1234567890/audio",
            headers={"Range": "bytes=0-99"},
        )
        self.assertEqual(r2.status_code, 206)
        self.assertEqual(len(r2.get_data()), 100)
        r2.close()

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

            def summarize(self, transcript, kind=None):
                # M9: stub returns an empty Summary so the pipeline
                # can complete without an LLM.
                from services.llm import Summary
                return Summary(text="", key_points=[])

            def extract(self, transcript, kind, recorded_at=None):
                # M9: stub returns an empty list.
                return []

            def transcribe(self, audio_path):
                raise RuntimeError("not used")

        self.app.config["LR_LLM_PROVIDERS"] = {
            "transcript": StubT(),
            "text": StubC(),
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

    def test_transcribe_recovers_stranded_status(self):
        """Regression: a session stranded in an intermediate status
        (e.g. the server was restarted mid-pipeline while it was
        'transcribing') is reset to 'ready' and re-run to 'done'
        by the retry endpoint. Previously this returned 409
        forever."""
        self._stub_providers(kind="lecture", conf=0.9)
        import storage
        sid = self._make_ready_session("stranded0001")
        store = self.app.config["LR_STORAGE"]
        # Force the stuck state (ready -> transcribing is a legal
        # transition, exactly where a killed server would leave it).
        store.set_status(sid, storage.STATUS_TRANSCRIBING)
        r = self.client.post(f"/api/v1/sessions/{sid}/transcribe")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["session"]["status"], "done")
        self.assertEqual(body["session"]["kind"], "lecture")

    def test_transcribe_409_when_pipeline_already_running(self):
        """The in-process guard refuses a second pipeline for the
        same session (e.g. a manual retry racing a background
        upload run)."""
        import app as app_mod
        self._stub_providers()
        sid = self._make_ready_session("stranded0002")
        with app_mod._PIPELINE_LOCK:
            app_mod._PIPELINES_IN_FLIGHT.add(sid)
        try:
            r = self.client.post(f"/api/v1/sessions/{sid}/transcribe")
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.get_json()["reason"], "pipeline_running")
        finally:
            with app_mod._PIPELINE_LOCK:
                app_mod._PIPELINES_IN_FLIGHT.discard(sid)

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

            def summarize(self, transcript, kind=None):
                # M9: even on the "first run" failure path, the
                # pipeline calls summarise after classify. Have
                # it raise so the failure propagates.
                raise LLMError("first run fails")

            def extract(self, transcript, kind, recorded_at=None):
                raise LLMError("first run fails")

        self.app.config["LR_LLM_PROVIDERS"] = {
            "transcript": Bad(), "text": Bad(),
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

            def summarize(self, transcript, kind=None):
                # M9: empty Summary so the pipeline reaches done.
                from services.llm import Summary
                return Summary(text="", key_points=[])

            def extract(self, transcript, kind, recorded_at=None):
                # M9: no items.
                return []

        self.app.config["LR_LLM_PROVIDERS"] = {
            "transcript": Good(), "text": Good(),
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
            "transcript": T(), "text": C(),
        }
        sid = self._make_ready_session("trnsc0000004")
        r = self.client.post(f"/api/v1/sessions/{sid}/transcribe")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "classify_failed")
        self.assertIn("upstream 503", body["detail"])


class WebUITests(LRTestCase):

    def test_dashboard_shows_real_data(self):
        """With a lecture, a meeting, and an event in the DB, the
        dashboard cards show them instead of the empty states."""
        import datetime as _dt
        import storage as storage_mod
        store = self.app.config["LR_STORAGE"]
        for sid, kind, title in (
            ("lect00000001", "lecture", "Linear Algebra"),
            ("meet00000001", "meeting", "Weekly sync"),
        ):
            store.upsert_session(
                sid, title=title, expected_count=1,
                sample_rate=22050, channels=1, bits_per_sample=32,
                started_at="2026-10-01T10:00:00Z",
            )
            store.update_status(sid, storage_mod.STATUS_STITCHING)
            store.update_status(
                sid, storage_mod.STATUS_READY,
                finished_at=storage_mod.now_iso(),
            )
            store.set_kind(sid, kind, confidence=1.0)
        # An event starting tomorrow lands in the "This week" list.
        tomorrow = (
            _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)
        )
        event_starts = tomorrow.strftime("%Y-%m-%dT") + "10:00:00Z"
        self.app.config["LR_EVENTS_SERVICE"].create(
            title="Project demo", kind="other", starts_at=event_starts,
        )
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data()
        self.assertIn(b"Linear Algebra", body)
        self.assertIn(b"Weekly sync", body)
        self.assertIn(b"Project demo", body)
        # The empty-state strings are gone for the cards that now
        # have data. ("Nothing scheduled" legitimately remains: the
        # event is tomorrow, so Today is still empty.)
        self.assertNotIn(b"No lectures yet", body)
        self.assertNotIn(b"No meetings recorded", body)
        self.assertNotIn(b"No events", body)

    def test_recording_opens_on_its_page_with_audio(self) -> None:
        self._upload_complete_session()
        r = self.client.get("/other?session=abcdef1234567890")
        self.assertEqual(r.status_code, 200)
        body = r.get_data()
        self.assertIn(b"<audio", body)
        self.assertIn(b"/api/v1/sessions/abcdef1234567890/audio", body)

    def test_todos_renders(self) -> None:
        r = self.client.get("/todos")
        self.assertEqual(r.status_code, 200)
        body = r.get_data()
        self.assertIn(b"To-dos", body)
        # Both lists.
        self.assertIn(b"Deadlines", body)

    def test_calendar_renders(self) -> None:
        r = self.client.get("/calendar")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Calendar", r.get_data())

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


class PipelineResilienceTests(LRTestCase):
    """Background FIFO worker, finish idempotency across restarts,
    and the boot sweep that recovers sessions stranded mid-pipeline."""

    def _announce_and_fill(self, sid: str, n: int = 1) -> None:
        self.client.post(
            "/api/v1/sessions",
            data=json.dumps({
                "id": sid,
                "title": "Resilience",
                "expected_count": n,
                "sample_rate": 22050,
                "channels": 1,
                "bits_per_sample": 32,
                "started_at": "2026-10-15T12:00:00Z",
            }),
            content_type="application/json",
        )
        for i in range(1, n + 1):
            r = self.client.put(
                f"/api/v1/sessions/{sid}/chunks/{i}",
                data=self.make_wav(bytes([i]) * 200),
                content_type="audio/wav",
            )
            self.assertEqual(r.status_code, 200)

    def _stranded_session(self, sid: str, to_status: str) -> None:
        """Create a stitched session and strand it at ``to_status``
        (simulating a server restart mid-pipeline)."""
        store = self.app.config["LR_STORAGE"]
        store.upsert_session(
            sid, title="Stranded", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at=storage.now_iso(),
        )
        store.write_chunk(sid, 1, self.make_wav(b"\x00" * 200))
        result = store.finish_session(sid)  # → ready + full.aac on disk
        self.assertTrue(result.ok, result.reason)
        # Walk the legal pipeline chain up to the target status (the
        # transition guard refuses jumps like ready → transcribed).
        chain = [
            storage.STATUS_TRANSCRIBING,
            storage.STATUS_TRANSCRIBED,
            storage.STATUS_CLASSIFYING,
            storage.STATUS_SUMMARISING,
            storage.STATUS_EXTRACTING,
        ]
        if to_status != storage.STATUS_READY:
            for step in chain:
                store.update_status(sid, step)
                if step == to_status:
                    break

    def _wait_status(self, sid: str, want: str, timeout: float = 10.0):
        store = self.app.config["LR_STORAGE"]
        deadline = time.time() + timeout
        status = None
        while time.time() < deadline:
            status = store.get_session(sid).status
            if status == want:
                break
            time.sleep(0.05)
        return status

    def test_finish_returns_fast_and_worker_completes(self) -> None:
        """Device flow, production mode: finish returns right after
        the stitch (before the AI work is done) while the pipeline
        runs on the FIFO worker, then completes to 'done'."""
        sid = "resfinish00000001"
        self._announce_and_fill(sid)
        self.app.config["LR_PIPELINE_BACKGROUND"] = True
        r = self.client.post(f"/api/v1/sessions/{sid}/finish")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["reason"], "stitched")
        self.assertNotEqual(j["session"]["status"], "failed")
        status = self._wait_status(sid, storage.STATUS_DONE)
        self.assertEqual(status, storage.STATUS_DONE)

    def test_finish_idempotent_after_restart(self) -> None:
        """A device re-sending finish after a server restart (session
        stranded mid-pipeline) gets 200 already_ready and does NOT
        trigger a second pipeline run."""
        sid = "residemppotent001"
        self._stranded_session(sid, storage.STATUS_TRANSCRIBING)
        r = self.client.post(f"/api/v1/sessions/{sid}/finish")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual(j["reason"], "already_ready")
        # Give any (wrongly) queued pipeline time to move the status.
        time.sleep(0.3)
        self.assertEqual(
            self.app.config["LR_STORAGE"].get_session(sid).status,
            storage.STATUS_TRANSCRIBING,
        )

    def test_sweep_requeues_stranded_session(self) -> None:
        """The boot sweep resets a stranded session to 'ready' and
        the worker drives it to 'done'."""
        sid = "ressweep000000001"
        self._stranded_session(sid, storage.STATUS_TRANSCRIBING)
        requeued = sweep_stranded_sessions(self.app)
        self.assertIn(sid, requeued)
        status = self._wait_status(sid, storage.STATUS_DONE)
        self.assertEqual(status, storage.STATUS_DONE)

    def test_sweep_resumes_without_retranscribing(self) -> None:
        """A stranded session with a persisted transcript artifact
        skips transcription entirely (the expensive step is not
        repeated after a restart)."""
        store = self.app.config["LR_STORAGE"]
        sid = "resresume00000001"
        self._stranded_session(sid, storage.STATUS_TRANSCRIBED)
        store.add_artifact(sid, "transcript", "kept transcript", model="stub")

        class _Boom:
            name = "boom"

            def transcribe(self, audio_path):
                raise AssertionError("must not re-transcribe on resume")

            def classify(self, t):
                raise AssertionError("must not classify on resume")

            def summarize(self, t, kind=None):
                raise AssertionError("must not re-summarise on resume")

            def extract(self, t, k, recorded_at=None):
                raise AssertionError("must not re-extract on resume")

        noop_text = self.app.config["LR_LLM_PROVIDERS"]["text"]
        self.app.config["LR_LLM_PROVIDERS"] = {
            "transcript": _Boom(), "text": noop_text,
        }
        requeued = sweep_stranded_sessions(self.app)
        self.assertIn(sid, requeued)
        status = self._wait_status(sid, storage.STATUS_DONE)
        self.assertEqual(status, storage.STATUS_DONE)
        artifact = store.get_artifact(sid, "transcript")
        self.assertEqual(artifact["content"], "kept transcript")

    def test_sweep_skips_failed_sessions(self) -> None:
        """Failed sessions are never auto-retried (a missing API key
        would loop forever); they stay for the manual retry endpoint."""
        sid = "resfailed00000001"
        self._stranded_session(sid, storage.STATUS_TRANSCRIBING)
        store = self.app.config["LR_STORAGE"]
        store.update_status(
            sid, storage.STATUS_FAILED, status_detail="boom",
        )
        requeued = sweep_stranded_sessions(self.app)
        self.assertNotIn(sid, requeued)
        self.assertEqual(
            store.get_session(sid).status, storage.STATUS_FAILED,
        )

    def test_worker_processes_sessions_in_fifo_order(self) -> None:
        """Two queued sessions are transcribed one at a time, in
        queue order."""
        from pathlib import Path
        store = self.app.config["LR_STORAGE"]
        sids = ["resfifo0000000001", "resfifo0000000002"]
        for k, sid in enumerate(sids):
            store.upsert_session(
                sid, title=f"fifo{k}", expected_count=1,
                sample_rate=22050, channels=1, bits_per_sample=32,
                started_at=storage.now_iso(),
            )
            store.write_chunk(sid, 1, self.make_wav(b"\x00" * 200))
            self.assertTrue(store.finish_session(sid).ok)

        noop_text = self.app.config["LR_LLM_PROVIDERS"]["text"]

        class _Recorder:
            name = "recorder"

            def __init__(self):
                self.paths = []

            def transcribe(self, audio_path):
                self.paths.append(Path(audio_path).parent.name)
                return "t"

            def classify(self, t):
                return noop_text.classify(t)

            def summarize(self, t, kind=None):
                return noop_text.summarize(t)

            def extract(self, t, k, recorded_at=None):
                return noop_text.extract(t, k)

        rec = _Recorder()
        self.app.config["LR_LLM_PROVIDERS"] = {
            "transcript": rec, "text": noop_text,
        }
        for sid in sids:
            self.assertTrue(_enqueue_pipeline(self.app, sid))
        for sid in sids:
            self.assertEqual(
                self._wait_status(sid, storage.STATUS_DONE),
                storage.STATUS_DONE,
            )
        self.assertEqual(rec.paths, sids)


if __name__ == "__main__":
    unittest.main()
