"""API-layer tests using Flask's test client."""
from __future__ import annotations

import json
import unittest

from tests.conftest import LRTestCase


class DeviceFlowTests(LRTestCase):

    def test_announce_then_list_then_finish(self) -> None:
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

        # 4) finish
        r = self.client.post("/api/v1/sessions/abcdef1234567890/finish")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["session"]["status"], "ready")
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
        self.assertIn(b"Ready", body)

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
        self.assertIn(b"No courses this semester", body)  # Lectures
        self.assertIn(b"No meetings recorded", body)  # Meetings
        # Search bar with the keyboard shortcut hint.
        self.assertIn(b"Search recordings", body)
        self.assertIn(b"lr-search__shortcut", body)

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


if __name__ == "__main__":
    unittest.main()
