"""Tests for the ClassificationService and the /unsorted page.

The LLM path is a stub (Milestone 6), so the corresponding
``classify_session`` tests assert the stub contract (always returns
None). The real implementation lands in Milestone 8 alongside
transcription.
"""
from __future__ import annotations

import json
import unittest

from tests.conftest import LRTestCase


def _make_session(
    self, *, status: str = "ready", kind: object = None,
) -> str:
    """Create a session row with the desired ``status`` and ``kind``.

    ``upsert_session`` always creates as ``status='receiving'`` and
    ``kind=NULL``, so to set those fields we issue a direct UPDATE
    via the storage layer's connection.
    """
    import uuid
    sid = uuid.uuid4().hex
    store = self.app.config["LR_STORAGE"]
    store.upsert_session(
        sid,
        title="test",
        expected_count=1,
        sample_rate=22050,
        channels=1,
        bits_per_sample=32,
        started_at="2026-01-15T10:00:00Z",
    )
    if status != "receiving" or kind is not None:
        # Direct write through the storage's transaction helper.
        with store._tx() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, kind = ? WHERE id = ?",
                (status, kind, sid),
            )
    return sid


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class ClassificationServiceTests(LRTestCase):
    def _service(self):
        return self.app.config["LR_CLASSIFICATION_SERVICE"]

    def test_override_kind_persists_lecture(self):
        sid = _make_session(self)
        sess = self._service().override_kind(sid, "lecture")
        self.assertEqual(sess.kind, "lecture")
        self.assertEqual(sess.kind_confidence, 1.0)

    def test_override_kind_persists_each_kind(self):
        for kind in ("lecture", "voice_memo", "meeting", "other"):
            sid = _make_session(self)
            sess = self._service().override_kind(sid, kind)
            self.assertEqual(sess.kind, kind)

    def test_override_kind_rejects_bad_kind(self):
        sid = _make_session(self)
        with self.assertRaises(ValueError):
            self._service().override_kind(sid, "gathering")

    def test_override_kind_clear_with_none(self):
        sid = _make_session(self, kind="lecture")
        sess = self._service().override_kind(sid, None)
        self.assertIsNone(sess.kind)
        self.assertIsNone(sess.kind_confidence)

    def test_override_kind_missing_session_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self._service().override_kind("nope", "lecture")

    def test_override_kind_requires_session_id(self):
        with self.assertRaises(ValueError):
            self._service().override_kind("", "lecture")

    def test_classify_session_returns_none_for_now(self):
        """Stub contract: classify_session always returns None for
        now (no transcript exists yet)."""
        sid = _make_session(self)
        self.assertIsNone(
            self._service().classify_session(sid),
        )

    def test_classify_session_missing_session_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self._service().classify_session("nope")

    def test_classify_session_requires_session_id(self):
        with self.assertRaises(ValueError):
            self._service().classify_session("")

    def test_bulk_classify_returns_dict_of_nones(self):
        a = _make_session(self)
        b = _make_session(self)
        out = self._service().bulk_classify([a, b])
        self.assertEqual(set(out.keys()), {a, b})
        for v in out.values():
            self.assertIsNone(v)

    def test_bulk_classify_omits_unknown_ids(self):
        a = _make_session(self)
        out = self._service().bulk_classify([a, "nope", ""])
        self.assertEqual(list(out.keys()), [a])

    def test_bulk_classify_rejects_non_list(self):
        with self.assertRaises(ValueError):
            self._service().bulk_classify("not a list")  # type: ignore

    def test_list_unclassified_filters_by_status_and_kind(self):
        # Only sessions with status='ready' AND kind=NULL should
        # appear. Classified ones (kind set) and not-yet-ready ones
        # (status='receiving') are excluded.
        ready_no_kind_a = _make_session(self, status="ready")
        ready_no_kind_b = _make_session(self, status="ready")
        ready_with_kind = _make_session(
            self, status="ready", kind="lecture",
        )
        receiving_no_kind = _make_session(self, status="receiving")
        sessions, total = self._service().list_unclassified()
        self.assertEqual(total, 2)
        ids = {s.id for s in sessions}
        self.assertIn(ready_no_kind_a, ids)
        self.assertIn(ready_no_kind_b, ids)
        self.assertNotIn(ready_with_kind, ids)
        self.assertNotIn(receiving_no_kind, ids)

    def test_list_unclassified_respects_limit(self):
        for _ in range(3):
            _make_session(self)
        sessions, total = self._service().list_unclassified(limit=2)
        self.assertEqual(total, 3)
        self.assertEqual(len(sessions), 2)

    def test_list_unclassified_validates_inputs(self):
        with self.assertRaises(ValueError):
            self._service().list_unclassified(limit=0)
        with self.assertRaises(ValueError):
            self._service().list_unclassified(offset=-1)
# ---------------------------------------------------------------------------
# API HTTP surface
# ---------------------------------------------------------------------------


class ClassificationApiTests(LRTestCase):
    def _make(self, **kwargs):
        return _make_session(self, **kwargs)

    def test_api_set_kind_works(self):
        """The existing /kind route works end-to-end via the
        service (manual override path)."""
        sid = self._make()
        resp = self.client.post(
            f"/api/v1/sessions/{sid}/kind",
            data=json.dumps({"kind": "lecture"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["session"]["kind"], "lecture")
        self.assertEqual(
            resp.get_json()["session"]["kind_confidence"], 1.0,
        )

    def test_api_classify_session_returns_stub_shape(self):
        sid = self._make()
        resp = self.client.post(f"/api/v1/sessions/{sid}/classify")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsNone(body["kind"])
        self.assertIsNone(body["confidence"])
        self.assertEqual(body["reason"], "llm_not_implemented")

    def test_api_classify_session_404_for_unknown(self):
        resp = self.client.post("/api/v1/sessions/nope/classify")
        self.assertEqual(resp.status_code, 404)

    def test_api_bulk_classify_returns_results_dict(self):
        a = self._make()
        b = self._make()
        resp = self.client.post(
            "/api/v1/classifications/bulk",
            data=json.dumps({"session_ids": [a, b, "nope"]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("results", body)
        self.assertIn(a, body["results"])
        self.assertIn(b, body["results"])
        self.assertNotIn("nope", body["results"])
        self.assertIsNone(body["results"][a]["kind"])
        self.assertEqual(
            body["results"][a]["reason"], "llm_not_implemented",
        )

    def test_api_bulk_classify_requires_list(self):
        resp = self.client.post(
            "/api/v1/classifications/bulk",
            data=json.dumps({"session_ids": "not-a-list"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_api_bulk_classify_empty_list(self):
        resp = self.client.post(
            "/api/v1/classifications/bulk",
            data=json.dumps({"session_ids": []}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["results"], {})


# ---------------------------------------------------------------------------
# /unsorted page
# ---------------------------------------------------------------------------


class UnsortedPageTests(LRTestCase):
    def test_unsorted_lists_only_unclassified_ready_sessions(self):
        unclassified = _make_session(self, status="ready")
        classified = _make_session(self, status="ready", kind="lecture")
        receiving = _make_session(self, status="receiving")
        resp = self.client.get("/unsorted")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # Only the unclassified session appears as a row.
        self.assertIn('data-session-id="' + unclassified + '"', body)
        self.assertNotIn(
            'data-session-id="' + classified + '"', body,
            "Classified sessions should not appear on /unsorted",
        )
        self.assertNotIn(
            'data-session-id="' + receiving + '"', body,
            "Receiving sessions should not appear on /unsorted",
        )

    def test_unsorted_dropdown_options_present(self):
        _make_session(self)
        resp = self.client.get("/unsorted")
        body = resp.get_data(as_text=True)
        for kind in ("lecture", "voice_memo", "meeting", "other"):
            self.assertIn(f'value="{kind}"', body)

    def test_unsorted_renders_empty_state_when_all_sorted(self):
        _make_session(self, status="ready", kind="lecture")
        resp = self.client.get("/unsorted")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("All recordings are sorted", body)

    def test_unsorted_count_in_subheading(self):
        for _ in range(3):
            _make_session(self)
        resp = self.client.get("/unsorted")
        body = resp.get_data(as_text=True)
        self.assertIn("3 unclassified", body)

    def test_unsorted_renders_save_button_per_row(self):
        _make_session(self)
        _make_session(self)
        resp = self.client.get("/unsorted")
        body = resp.get_data(as_text=True)
        self.assertEqual(body.count("lr-sort__save"), 2)

    def test_unsorted_renders_bulk_classify_button(self):
        _make_session(self)
        resp = self.client.get("/unsorted")
        body = resp.get_data(as_text=True)
        self.assertIn("Classify all with AI", body)


class ClassificationServiceIsRealTests(unittest.TestCase):
    """Quick schema-style check: the service exists and the
    constructor takes a storage instance."""

    def test_classification_service_init_signature(self):
        from services.classification import ClassificationService
        import inspect
        sig = inspect.signature(ClassificationService.__init__)
        self.assertGreaterEqual(
            len(sig.parameters), 2,
            "ClassificationService.__init__ should take (self, store)",
        )


if __name__ == "__main__":
    unittest.main()