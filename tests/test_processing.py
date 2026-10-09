"""Tests for the post-finish pipeline (Milestone 8).

The pipeline walks sessions through
``ready -> transcribing -> transcribed -> classifying -> done`` and
records the transcript as an artifact + a kind on the session.
Tests use stub providers (no real HTTP) to verify each branch:

* happy path
* transcription failure (status -> failed, reason recorded)
* classification failure
* retry after failure
* session not in 'ready' state
"""
from __future__ import annotations

import unittest
import uuid
from pathlib import Path

import storage
from services.llm import (
    Classification, LLMError, LLMProvider,
)
from services import processing
from tests.conftest import LRTestCase


class _StubTranscript(LLMProvider):
    """Returns a fixed transcript string."""

    name = "stub-transcript"

    def __init__(self, text="hello world"):
        self._text = text
        self.calls = []

    def transcribe(self, audio_path):
        self.calls.append(audio_path)
        return self._text

    def classify(self, transcript):
        raise RuntimeError("not used in this stub")


class _StubClassifier(LLMProvider):
    """Returns a fixed (kind, confidence) tuple."""

    name = "stub-classifier"

    def __init__(self, kind="lecture", confidence=0.9, fail=False):
        self._kind = kind
        self._confidence = confidence
        self._fail = fail
        self.calls = []

    def classify(self, transcript):
        self.calls.append(transcript)
        if self._fail:
            raise LLMError("simulated classifier failure")
        return Classification(kind=self._kind, confidence=self._confidence)

    def transcribe(self, audio_path):
        raise RuntimeError("not used in this stub")


def _make_ready_session(case):
    """Create a session in status='ready' with a real full.wav on disk."""
    sid = uuid.uuid4().hex
    store = case.app.config["LR_STORAGE"]
    store.upsert_session(
        sid, title="test", expected_count=1,
        sample_rate=22050, channels=1, bits_per_sample=32,
        started_at="2026-01-15T10:00:00Z",
    )
    full = store.full_audio_path(sid)
    full.write_bytes(case.make_wav(b"\x00" * 256))
    # Walk the legal transition: receiving -> stitching -> ready.
    store.update_status(sid, storage.STATUS_STITCHING, status_detail=None)
    store.update_status(
        sid, storage.STATUS_READY, status_detail=None,
        finished_at=storage.now_iso(),
    )
    return sid


class PipelineHappyPathTests(LRTestCase):
    def test_pipeline_walks_full_lifecycle(self):
        sid = _make_ready_session(self)
        result = processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=_StubTranscript("the quick brown fox"),
            classifier=_StubClassifier("lecture", 0.8),
        )
        self.assertIsNotNone(result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "lecture")
        self.assertEqual(result["confidence"], 0.8)

    def test_pipeline_persists_transcript_artifact(self):
        sid = _make_ready_session(self)
        processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=_StubTranscript("the quick brown fox"),
            classifier=_StubClassifier("meeting", 0.7),
        )
        artifact = self.app.config["LR_STORAGE"].get_artifact(
            sid, "transcript",
        )
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["content"], "the quick brown fox")
        self.assertEqual(artifact["model"], "stub-transcript")

    def test_pipeline_sets_kind_with_confidence(self):
        sid = _make_ready_session(self)
        processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=_StubTranscript("x"),
            classifier=_StubClassifier("voice_memo", 0.55),
        )
        sess = self.app.config["LR_STORAGE"].get_session(sid)
        self.assertEqual(sess.kind, "voice_memo")
        self.assertEqual(sess.kind_confidence, 0.55)

    def test_pipeline_ends_at_status_done(self):
        sid = _make_ready_session(self)
        processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=_StubTranscript("x"),
            classifier=_StubClassifier(),
        )
        sess = self.app.config["LR_STORAGE"].get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_DONE)

    def test_pipeline_calls_providers_with_correct_args(self):
        sid = _make_ready_session(self)
        transcript = _StubTranscript("xyz")
        classifier = _StubClassifier()
        processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=transcript,
            classifier=classifier,
        )
        self.assertEqual(len(transcript.calls), 1)
        self.assertIsInstance(transcript.calls[0], Path)
        self.assertEqual(transcript.calls[0].name, "full.wav")
        self.assertEqual(classifier.calls, ["xyz"])


class PipelineFailureTests(LRTestCase):
    def test_transcribe_failure_marks_status_failed(self):
        class FailTranscribe(LLMProvider):
            name = "fail-transcribe"

            def transcribe(self, audio_path):
                raise LLMError("simulated upstream 500")

            def classify(self, transcript):
                raise RuntimeError("not reached")

        sid = _make_ready_session(self)
        result = processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=FailTranscribe(),
            classifier=_StubClassifier(),
        )
        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"], processing.REASON_TRANSCRIBE_FAILED,
        )
        sess = self.app.config["LR_STORAGE"].get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_FAILED)
        self.assertIn("simulated upstream 500", sess.status_detail)

    def test_classify_failure_marks_status_failed(self):
        sid = _make_ready_session(self)
        result = processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=_StubTranscript("some text"),
            classifier=_StubClassifier(fail=True),
        )
        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"], processing.REASON_CLASSIFY_FAILED,
        )
        sess = self.app.config["LR_STORAGE"].get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_FAILED)

    def test_transcribe_failure_keeps_no_artifact(self):
        """If the transcription step fails before the artifact
        is written, no transcript artifact is created."""
        class FailTranscribe(LLMProvider):
            name = "fail"

            def transcribe(self, audio_path):
                raise LLMError("nope")

            def classify(self, transcript):
                raise RuntimeError("not reached")

        sid = _make_ready_session(self)
        processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=FailTranscribe(),
            classifier=_StubClassifier(),
        )
        artifact = self.app.config["LR_STORAGE"].get_artifact(
            sid, "transcript",
        )
        self.assertIsNone(artifact)

    def test_classify_failure_keeps_transcript_artifact(self):
        """If the classifier fails AFTER successful transcription,
        the transcript artifact stays so a manual retry can
        pick it up."""
        sid = _make_ready_session(self)
        processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=_StubTranscript("transcript text"),
            classifier=_StubClassifier(fail=True),
        )
        artifact = self.app.config["LR_STORAGE"].get_artifact(
            sid, "transcript",
        )
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["content"], "transcript text")

    def test_empty_transcript_marks_failed(self):
        class EmptyTranscribe(LLMProvider):
            name = "empty"

            def transcribe(self, audio_path):
                return "   \n  "

            def classify(self, transcript):
                raise RuntimeError("not reached")

        sid = _make_ready_session(self)
        result = processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=EmptyTranscribe(),
            classifier=_StubClassifier(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"], processing.REASON_TRANSCRIBE_FAILED,
        )


class PipelineSessionStateTests(LRTestCase):
    def test_pipeline_noop_when_not_ready(self):
        sid = uuid.uuid4().hex
        store = self.app.config["LR_STORAGE"]
        store.upsert_session(
            sid, title="x", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        result = processing.run_post_finish_pipeline(
            store, sid,
            transcript_provider=_StubTranscript(),
            classifier=_StubClassifier(),
        )
        self.assertIsNone(result)
        sess = store.get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_RECEIVING)

    def test_pipeline_noop_when_session_missing(self):
        result = processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], "missing-id",
            transcript_provider=_StubTranscript(),
            classifier=_StubClassifier(),
        )
        self.assertIsNone(result)

    def test_pipeline_noop_when_full_audio_missing(self):
        sid = uuid.uuid4().hex
        store = self.app.config["LR_STORAGE"]
        store.upsert_session(
            sid, title="x", expected_count=1,
            sample_rate=22050, channels=1, bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        store.update_status(sid, storage.STATUS_STITCHING)
        store.update_status(
            sid, storage.STATUS_READY, finished_at=storage.now_iso(),
        )
        result = processing.run_post_finish_pipeline(
            store, sid,
            transcript_provider=_StubTranscript(),
            classifier=_StubClassifier(),
        )
        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], processing.REASON_NO_AUDIO)


class PipelineRetryTests(LRTestCase):
    def test_retry_after_failure_succeeds(self):
        """A session that previously failed can be re-run by
        resetting its status to 'ready' and calling the
        pipeline again."""

        class FailThenSucceed(LLMProvider):
            name = "flaky"

            def __init__(self):
                self.attempts = 0

            def transcribe(self, audio_path):
                self.attempts += 1
                if self.attempts == 1:
                    raise LLMError("transient")
                return "second time lucky"

            def classify(self, transcript):
                return Classification(kind="meeting", confidence=0.6)

        sid = _make_ready_session(self)
        transcript = FailThenSucceed()
        r1 = processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=transcript,
            classifier=_StubClassifier(),
        )
        self.assertFalse(r1["ok"])
        self.app.config["LR_STORAGE"].set_status(
            sid, storage.STATUS_READY,
        )
        r2 = processing.run_post_finish_pipeline(
            self.app.config["LR_STORAGE"], sid,
            transcript_provider=transcript,
            classifier=_StubClassifier("meeting", 0.6),
        )
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["kind"], "meeting")
        self.assertEqual(transcript.attempts, 2)
        sess = self.app.config["LR_STORAGE"].get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_DONE)


class PipelineReasonConstantsTests(unittest.TestCase):
    def test_reason_constants_are_strings(self):
        self.assertIsInstance(processing.REASON_TRANSCRIBE_FAILED, str)
        self.assertIsInstance(processing.REASON_CLASSIFY_FAILED, str)
        self.assertIsInstance(processing.REASON_NOT_READY, str)
        self.assertIsInstance(processing.REASON_NO_AUDIO, str)


if __name__ == "__main__":
    unittest.main()