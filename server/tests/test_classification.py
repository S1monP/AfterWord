"""Tests for the ClassificationService and the Other page.

The LLM path is tested with stub classifiers; no real API calls are
made.
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
        return self.app.config["AFTERWORD_CLASSIFICATION_SERVICE"]

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

    def test_classify_session_returns_none_without_transcript(self):
        """No transcript artifact → LLM path returns None. The
        service can't classify audio that hasn't been
        transcribed yet."""
        sid = _make_session(self)
        self.assertIsNone(
            self._service().classify_session(sid),
        )

    def test_classify_session_returns_tuple_with_transcript(self):
        """With a transcript present and a stub classifier, the
        service returns ``(kind, confidence)`` for the session.
        """
        from services.llm import Classification, LLMProvider

        class StubClassifier(LLMProvider):
            name = "stub"

            def classify(self, transcript):
                return Classification(
                    kind="lecture", confidence=0.77,
                )

        sid = _make_session(self)
        self.app.config["AFTERWORD_STORAGE"].add_artifact(
            sid, "transcript", "lorem ipsum transcript",
        )
        out = self._service().classify_session(
            sid, classifier=StubClassifier(),
        )
        self.assertEqual(out, ("lecture", 0.77))

    def test_classify_session_collapses_provider_errors_to_none(self):
        """If the LLM provider raises (network error, bad JSON,
        …), the service swallows the exception and returns
        ``None``. The route layer surfaces this as the
        ``llm_not_implemented`` reason; the user is never
        shown a raw traceback.
        """
        from services.llm import LLMError, LLMProvider

        class FailingClassifier(LLMProvider):
            name = "fail"

            def classify(self, transcript):
                raise LLMError("simulated upstream failure")

        sid = _make_session(self)
        self.app.config["AFTERWORD_STORAGE"].add_artifact(
            sid, "transcript", "something",
        )
        self.assertIsNone(
            self._service().classify_session(
                sid, classifier=FailingClassifier(),
            )
        )

    def test_classify_session_missing_session_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self._service().classify_session("nope")

    def test_classify_session_requires_session_id(self):
        with self.assertRaises(ValueError):
            self._service().classify_session("")


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
        """With no transcript and no API key, the route returns
        the ``llm_not_implemented`` placeholder. (Same shape as
        the old stub; the contract is preserved.)"""
        sid = self._make()
        resp = self.client.post(f"/api/v1/sessions/{sid}/classify")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsNone(body["kind"])
        self.assertIsNone(body["confidence"])
        self.assertEqual(body["reason"], "llm_not_implemented")

    def test_api_classify_session_returns_classified_shape(self):
        """With a transcript and a stub classifier injected into
        the app config, the route returns the real shape:
        ``{kind, confidence, reason: "classified"}``.
        """
        from services.llm import Classification, LLMProvider

        class StubClassifier(LLMProvider):
            name = "stub"

            def classify(self, transcript):
                return Classification(
                    kind="meeting", confidence=0.65,
                )

        sid = self._make()
        self.app.config["AFTERWORD_STORAGE"].add_artifact(
            sid, "transcript", "all-hands meeting notes",
        )
        # Replace the registered classifier with the stub.
        original = self.app.config["AFTERWORD_LLM_PROVIDERS"]["text"]
        self.app.config["AFTERWORD_LLM_PROVIDERS"]["text"] = (
            StubClassifier()
        )
        try:
            resp = self.client.post(
                f"/api/v1/sessions/{sid}/classify",
            )
        finally:
            self.app.config["AFTERWORD_LLM_PROVIDERS"]["text"] = original
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["kind"], "meeting")
        self.assertEqual(body["confidence"], 0.65)
        self.assertEqual(body["reason"], "classified")
        # The kind should also be persisted on the session row.
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.kind, "meeting")
        self.assertEqual(sess.kind_confidence, 0.65)

    def test_api_classify_session_404_for_unknown(self):
        resp = self.client.post("/api/v1/sessions/nope/classify")
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Other page: everything that is not a lecture, meeting or voice memo
# ---------------------------------------------------------------------------


class OtherPageTests(LRTestCase):
    def test_other_lists_unsorted_and_unfinished_recordings(self):
        unclassified = _make_session(self, status="ready")
        receiving = _make_session(self, status="receiving")
        other = _make_session(self, status="done", kind="other")
        lecture = _make_session(self, status="done", kind="lecture")
        body = self.client.get("/other").get_data(as_text=True)
        for sid in (unclassified, receiving, other):
            self.assertIn(sid, body)
        self.assertNotIn(lecture, body)


class ClassificationServiceIsRealTests(unittest.TestCase):
    """Quick schema-style check: the service exists and the
    constructor takes a storage instance."""

    def test_classification_service_init_signature(self):
        import inspect

        from services.classification import ClassificationService
        sig = inspect.signature(ClassificationService.__init__)
        self.assertGreaterEqual(
            len(sig.parameters), 2,
            "ClassificationService.__init__ should take (self, store)",
        )


if __name__ == "__main__":
    unittest.main()
