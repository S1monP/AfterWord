"""Tests for the post-finish pipeline.

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

import json
import unittest
import uuid
from pathlib import Path

import storage
from services import processing
from services.llm import (
    Classification,
    LLMError,
    LLMProvider,
    Summary,
)
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
    """Stub that implements classify, summarise and extract.

    M9: the pipeline uses the same provider for all three LLM
    steps, so the stub has to implement them all to keep the
    M8 happy-path tests green.
    """

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

    def summarize(self, transcript, kind=None):
        # M9: return an empty Summary so the pipeline can run
        # through summarising/extracting to ``done``.
        return Summary(text="", key_points=[])

    def extract(self, transcript, kind, recorded_at=None):
        # M9: no structured items by default.
        return []

    def transcribe(self, audio_path):
        raise RuntimeError("not used in this stub")


def _make_ready_session(case):
    """Create a session in status='ready' with a real full.aac on disk."""
    sid = uuid.uuid4().hex
    store = case.app.config["AFTERWORD_STORAGE"]
    store.upsert_session(
        sid,
        title="rec_2026-01-15T1000",
        expected_count=1,
        sample_rate=22050,
        channels=1,
        bits_per_sample=32,
        started_at="2026-01-15T10:00:00Z",
    )
    full = store.full_audio_path(sid)
    full.write_bytes(case.make_wav(b"\x00" * 256))
    # Walk the legal transition: receiving -> stitching -> ready.
    store.update_status(sid, storage.STATUS_STITCHING, status_detail=None)
    store.update_status(
        sid,
        storage.STATUS_READY,
        status_detail=None,
        finished_at=storage.now_iso(),
    )
    return sid


class PipelineHappyPathTests(LRTestCase):
    def test_pipeline_walks_full_lifecycle(self):
        sid = _make_ready_session(self)
        result = processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
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
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=_StubTranscript("the quick brown fox"),
            classifier=_StubClassifier("meeting", 0.7),
        )
        artifact = self.app.config["AFTERWORD_STORAGE"].get_artifact(
            sid,
            "transcript",
        )
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["content"], "the quick brown fox")
        self.assertEqual(artifact["model"], "stub-transcript")

    def test_pipeline_sets_kind_with_confidence(self):
        sid = _make_ready_session(self)
        processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=_StubTranscript("x"),
            classifier=_StubClassifier("voice_memo", 0.55),
        )
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.kind, "voice_memo")
        self.assertEqual(sess.kind_confidence, 0.55)

    def test_pipeline_ends_at_status_done(self):
        sid = _make_ready_session(self)
        processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=_StubTranscript("x"),
            classifier=_StubClassifier(),
        )
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_DONE)

    def test_pipeline_calls_providers_with_correct_args(self):
        sid = _make_ready_session(self)
        transcript = _StubTranscript("xyz")
        classifier = _StubClassifier()
        processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=transcript,
            classifier=classifier,
        )
        self.assertEqual(len(transcript.calls), 1)
        self.assertIsInstance(transcript.calls[0], Path)
        self.assertEqual(transcript.calls[0].name, "full.aac")
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
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=FailTranscribe(),
            classifier=_StubClassifier(),
        )
        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            processing.REASON_TRANSCRIBE_FAILED,
        )
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_FAILED)
        self.assertIn("simulated upstream 500", sess.status_detail)

    def test_classify_failure_marks_status_failed(self):
        sid = _make_ready_session(self)
        result = processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=_StubTranscript("some text"),
            classifier=_StubClassifier(fail=True),
        )
        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            processing.REASON_CLASSIFY_FAILED,
        )
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
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
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=FailTranscribe(),
            classifier=_StubClassifier(),
        )
        artifact = self.app.config["AFTERWORD_STORAGE"].get_artifact(
            sid,
            "transcript",
        )
        self.assertIsNone(artifact)

    def test_classify_failure_keeps_transcript_artifact(self):
        """If the classifier fails AFTER successful transcription,
        the transcript artifact stays so a manual retry can
        pick it up."""
        sid = _make_ready_session(self)
        processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=_StubTranscript("transcript text"),
            classifier=_StubClassifier(fail=True),
        )
        artifact = self.app.config["AFTERWORD_STORAGE"].get_artifact(
            sid,
            "transcript",
        )
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["content"], "transcript text")

    def test_empty_transcript_ends_done_no_speech(self):
        class EmptyTranscribe(LLMProvider):
            name = "empty"

            def transcribe(self, audio_path):
                return "   \n  "

            def classify(self, transcript):
                raise RuntimeError("not reached")

        store = self.app.config["AFTERWORD_STORAGE"]
        sid = _make_ready_session(self)
        result = processing.run_post_finish_pipeline(
            store,
            sid,
            transcript_provider=EmptyTranscribe(),
            classifier=_StubClassifier(),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["no_speech"])
        sess = store.get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_DONE)
        self.assertEqual(sess.status_detail, processing.NO_SPEECH_DETAIL)
        self.assertIsNone(sess.kind)
        self.assertIsNone(store.get_artifact(sid, "transcript"))

    def test_voice_memo_keeps_only_transcript(self):
        class Memo(LLMProvider):
            name = "memo"

            def transcribe(self, audio_path):
                return "buy milk"

            def classify(self, transcript):
                return Classification(kind="voice_memo", confidence=0.9)

            def suggest_title(self, transcript):
                return "Milk"

            def summarize(self, transcript, kind=None):
                raise RuntimeError("memos are not summarised")

            def extract(self, transcript, kind, recorded_at=None):
                raise RuntimeError("memos are not extracted")

        store = self.app.config["AFTERWORD_STORAGE"]
        sid = _make_ready_session(self)
        # A summary from an earlier run (as another kind) is removed.
        store.add_artifact(sid, "summary", '{"text": "old"}')
        result = processing.run_post_finish_pipeline(
            store,
            sid,
            transcript_provider=Memo(),
            classifier=Memo(),
            llm_provider=Memo(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(store.get_session(sid).status, storage.STATUS_DONE)
        self.assertEqual(
            store.get_artifact(sid, "transcript")["content"],
            "buy milk",
        )
        self.assertIsNone(store.get_artifact(sid, "summary"))
        self.assertIsNone(store.get_artifact(sid, "extraction"))
        self.assertEqual(store.get_session(sid).title, "Milk")


class PipelineSessionStateTests(LRTestCase):
    def test_pipeline_noop_when_not_ready(self):
        sid = uuid.uuid4().hex
        store = self.app.config["AFTERWORD_STORAGE"]
        store.upsert_session(
            sid,
            title="x",
            expected_count=1,
            sample_rate=22050,
            channels=1,
            bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        result = processing.run_post_finish_pipeline(
            store,
            sid,
            transcript_provider=_StubTranscript(),
            classifier=_StubClassifier(),
        )
        self.assertIsNone(result)
        sess = store.get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_RECEIVING)

    def test_pipeline_noop_when_session_missing(self):
        result = processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            "missing-id",
            transcript_provider=_StubTranscript(),
            classifier=_StubClassifier(),
        )
        self.assertIsNone(result)

    def test_pipeline_noop_when_full_audio_missing(self):
        sid = uuid.uuid4().hex
        store = self.app.config["AFTERWORD_STORAGE"]
        store.upsert_session(
            sid,
            title="x",
            expected_count=1,
            sample_rate=22050,
            channels=1,
            bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        store.update_status(sid, storage.STATUS_STITCHING)
        store.update_status(
            sid,
            storage.STATUS_READY,
            finished_at=storage.now_iso(),
        )
        result = processing.run_post_finish_pipeline(
            store,
            sid,
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
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=transcript,
            classifier=_StubClassifier(),
        )
        self.assertFalse(r1["ok"])
        self.app.config["AFTERWORD_STORAGE"].set_status(
            sid,
            storage.STATUS_READY,
        )
        r2 = processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=transcript,
            classifier=_StubClassifier("meeting", 0.6),
        )
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["kind"], "meeting")
        self.assertEqual(transcript.attempts, 2)
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_DONE)


class PipelineReasonConstantsTests(unittest.TestCase):
    def test_reason_constants_are_strings(self):
        self.assertIsInstance(processing.REASON_TRANSCRIBE_FAILED, str)
        self.assertIsInstance(processing.REASON_CLASSIFY_FAILED, str)
        self.assertIsInstance(processing.REASON_NOT_READY, str)
        self.assertIsInstance(processing.REASON_NO_AUDIO, str)
        self.assertIsInstance(processing.REASON_SUMMARISE_FAILED, str)
        self.assertIsInstance(processing.REASON_EXTRACT_FAILED, str)


class PipelineSummariseExtractTests(LRTestCase):
    """M9: the pipeline now produces a summary and an extraction
    artifact after classification. These tests verify both the
    happy path and the failure paths.
    """

    def test_pipeline_persists_summary_and_extraction(self):
        from services.llm import Classification, LLMProvider, Summary

        class StubT(LLMProvider):
            name = "stub-t"

            def transcribe(self, audio_path):
                return "the transcript text"

            def classify(self, transcript):
                raise RuntimeError("not used")

            def summarize(self, transcript, kind=None):
                raise RuntimeError("not used")

            def extract(self, transcript, kind, recorded_at=None):
                raise RuntimeError("not used")

        class StubLLM(LLMProvider):
            name = "stub-llm"

            def classify(self, transcript):
                return Classification(kind="lecture", confidence=0.8)

            def summarize(self, transcript, kind=None):
                return Summary(
                    text="A lecture on testing.",
                    key_points=["tests are good", "coverage matters"],
                )

            def extract(self, transcript, kind, recorded_at=None):
                return [
                    {"type": "topic", "text": "Test coverage"},
                    {"type": "key_term", "text": "Unit test"},
                ]

            def transcribe(self, audio_path):
                raise RuntimeError("not used here")

        sid = _make_ready_session(self)
        result = processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=StubT(),
            classifier=StubLLM(),
            llm_provider=StubLLM(),
        )
        self.assertTrue(result["ok"])
        # Summary artifact exists and round-trips as JSON.
        summary = self.app.config["AFTERWORD_STORAGE"].get_artifact(
            sid,
            "summary",
        )
        self.assertIsNotNone(summary)
        parsed = json.loads(summary["content"])
        self.assertEqual(parsed["text"], "A lecture on testing.")
        self.assertEqual(len(parsed["key_points"]), 2)
        # Extraction artifact exists and has the items.
        extraction = self.app.config["AFTERWORD_STORAGE"].get_artifact(
            sid,
            "extraction",
        )
        self.assertIsNotNone(extraction)
        items = json.loads(extraction["content"])["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["type"], "topic")
        # Session is fully done.
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_DONE)
        self.assertEqual(result["summary_chars"], len("A lecture on testing."))
        self.assertEqual(result["extracted_items"], 2)

    def test_summary_gets_kind_keeps_sections_and_resumes(self):
        from services.llm import (
            Classification,
            LLMError,
            LLMProvider,
            Summary,
        )

        class T(LLMProvider):
            name = "t"

            def __init__(self):
                self.summary_kinds = []
                self.extract_fails = True

            def transcribe(self, audio_path):
                return "x"

            def classify(self, transcript):
                return Classification(kind="lecture", confidence=0.9)

            def summarize(self, transcript, kind=None):
                self.summary_kinds.append(kind)
                return Summary(
                    text="Overview.",
                    key_points=["p"],
                    sections=[{"heading": "Entropy", "text": "Notes."}],
                )

            def extract(self, transcript, kind, recorded_at=None):
                if self.extract_fails:
                    raise LLMError("extraction broke")
                return []

        store = self.app.config["AFTERWORD_STORAGE"]
        sid = _make_ready_session(self)
        t = T()

        def run(resume):
            return processing.run_post_finish_pipeline(
                store,
                sid,
                transcript_provider=t,
                classifier=t,
                llm_provider=t,
                resume=resume,
            )

        self.assertFalse(run(False)["ok"])
        self.assertEqual(t.summary_kinds, ["lecture"])
        parsed = json.loads(store.get_artifact(sid, "summary")["content"])
        self.assertEqual(
            parsed["sections"],
            [{"heading": "Entropy", "text": "Notes."}],
        )
        # Resume reuses the stored summary (sections included).
        store.set_status(sid, storage.STATUS_READY)
        t.extract_fails = False
        self.assertTrue(run(True)["ok"])
        self.assertEqual(t.summary_kinds, ["lecture"])
        parsed = json.loads(store.get_artifact(sid, "summary")["content"])
        self.assertEqual(parsed["sections"][0]["heading"], "Entropy")

    def test_summarise_failure_marks_failed(self):
        from services.llm import (
            Classification,
            LLMError,
            LLMProvider,
        )

        class T(LLMProvider):
            name = "t"

            def transcribe(self, audio_path):
                return "x"

            def classify(self, transcript):
                return Classification(kind="lecture", confidence=0.8)

            def summarize(self, transcript, kind=None):
                raise LLMError("upstream timed out")

            def extract(self, transcript, kind, recorded_at=None):
                return []

        sid = _make_ready_session(self)
        result = processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=T(),
            classifier=T(),
            llm_provider=T(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            processing.REASON_SUMMARISE_FAILED,
        )
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_FAILED)
        self.assertIn("upstream timed out", sess.status_detail)
        # No summary artifact was persisted.
        self.assertIsNone(
            self.app.config["AFTERWORD_STORAGE"].get_artifact(sid, "summary"),
        )

    def test_extract_failure_keeps_summary_artifact(self):
        from services.llm import (
            Classification,
            LLMError,
            LLMProvider,
            Summary,
        )

        class T(LLMProvider):
            name = "t"

            def transcribe(self, audio_path):
                return "x"

            def classify(self, transcript):
                return Classification(kind="meeting", confidence=0.7)

            def summarize(self, transcript, kind=None):
                return Summary(text="meeting summary", key_points=[])

            def extract(self, transcript, kind, recorded_at=None):
                raise LLMError("extraction broke")

        sid = _make_ready_session(self)
        result = processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=T(),
            classifier=T(),
            llm_provider=T(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            processing.REASON_EXTRACT_FAILED,
        )
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.status, storage.STATUS_FAILED)
        # The summary was persisted before the extract step failed.
        summary = self.app.config["AFTERWORD_STORAGE"].get_artifact(
            sid,
            "summary",
        )
        self.assertIsNotNone(summary)
        # No extraction artifact.
        self.assertIsNone(
            self.app.config["AFTERWORD_STORAGE"].get_artifact(
                sid,
                "extraction",
            ),
        )


class _TopicClassifier(_StubClassifier):
    """A classifier whose summary carries a title."""

    def __init__(self, topic, **kw):
        super().__init__(**kw)
        self.topic = topic

    def summarize(self, transcript, kind=None):
        return Summary(text="overview", key_points=[], title=self.topic)


class RecordingTitleTests(LRTestCase):
    def _device_named(self):
        sid = _make_ready_session(self)
        with self.app.config["AFTERWORD_STORAGE"]._tx() as conn:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("rec_2026-01-15T1000", sid))
        return sid

    def _run(self, sid, classifier):
        return processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=_StubTranscript("some talk"),
            classifier=classifier,
        )

    def test_ai_topic_names_the_recording(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = self._device_named()
        self._run(sid, _TopicClassifier("Entropy and the second law"))
        sess = store.get_session(sid)
        self.assertEqual(sess.title, "Entropy and the second law")
        self.assertEqual(sess.title_source, "ai")
        # The title is kept with the summary, for resumed runs.
        self.assertEqual(
            json.loads(store.get_artifact(sid, "summary")["content"])["title"],
            "Entropy and the second law",
        )

    def test_name_given_by_hand_is_kept(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = _make_ready_session(self)
        store.set_session_title(sid, "My name", source="user")
        self._run(sid, _TopicClassifier("Something else"))
        sess = store.get_session(sid)
        self.assertEqual(sess.title, "My name")
        self.assertEqual(sess.title_source, "user")

    def test_no_topic_keeps_device_name(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = self._device_named()
        self._run(sid, _StubClassifier())
        sess = store.get_session(sid)
        self.assertEqual(sess.title, "rec_2026-01-15T1000")
        self.assertIsNone(sess.title_source)

    def test_real_name_from_the_api_is_kept(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        sid = _make_ready_session(self)
        with store._tx() as conn:
            conn.execute("UPDATE sessions SET title = 'Linear Algebra' WHERE id = ?", (sid,))
        self._run(sid, _TopicClassifier("Something else"))
        self.assertEqual(store.get_session(sid).title, "Linear Algebra")


class CalendarTimeMatchTests(LRTestCase):
    """Pipeline step 0: calendar time-match sets kind/course/title
    from the matched event without any AI; the AI classifier is only
    the fallback when nothing matches. Manual kinds are preserved."""

    def _seed(self, *, event_title, event_kind, starts_at, ends_at=None, course=None):
        sid = _make_ready_session(self)  # started 2026-01-15T10:00:00Z
        store = self.app.config["AFTERWORD_STORAGE"]
        course_id = None
        if course:
            course_id = store.create_course(
                uuid.uuid4().hex,
                name=course,
            ).id
        ev = self.app.config["AFTERWORD_EVENTS_SERVICE"].create(
            title=event_title,
            kind=event_kind,
            starts_at=starts_at,
            ends_at=ends_at,
            course_id=course_id,
        )
        return sid, ev, course_id

    def _run(self, sid, classifier=None):
        classifier = classifier or _StubClassifier("meeting", 0.7)
        transcript = _StubTranscript("the quick brown fox")
        result = processing.run_post_finish_pipeline(
            self.app.config["AFTERWORD_STORAGE"],
            sid,
            transcript_provider=transcript,
            classifier=classifier,
        )
        return result, classifier

    def test_time_match_sets_kind_course_title_no_ai(self):
        sid, ev, course_id = self._seed(
            event_title="Lecture 5",
            event_kind="lecture",
            starts_at="2026-01-15T10:05:00Z",
            ends_at="2026-01-15T11:00:00Z",
            course="CS101",
        )
        result, classifier = self._run(sid)
        self.assertTrue(result["ok"])
        store = self.app.config["AFTERWORD_STORAGE"]
        sess = store.get_session(sid)
        self.assertEqual(sess.kind, "lecture")
        self.assertEqual(sess.kind_confidence, 1.0)
        self.assertEqual(sess.course_id, course_id)
        # The course name (pages show the date on its own line).
        self.assertEqual(sess.title, "CS101")
        self.assertEqual(sess.title_source, "calendar")
        # The AI classifier was never called.
        self.assertEqual(classifier.calls, [])
        # The calendar shows the pairing.
        matched = (
            self.app.config["AFTERWORD_EVENTS_SERVICE"]
            .get(
                ev.id,
            )
            .matched_session_id
        )
        self.assertEqual(matched, sid)

    def test_matched_lecture_is_course_and_topic(self):
        sid, _ev, _course_id = self._seed(
            event_title="Lecture 5",
            event_kind="lecture",
            starts_at="2026-01-15T10:05:00Z",
            ends_at="2026-01-15T11:00:00Z",
            course="CS101",
        )
        self._run(sid, _TopicClassifier("Hash tables"))
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.title, "CS101: Hash tables")
        self.assertEqual(sess.title_source, "calendar")

    def test_time_match_without_course_uses_event_name(self):
        sid, _ev, _ = self._seed(
            event_title="Lecture 5",
            event_kind="lecture",
            starts_at="2026-01-15T10:05:00Z",
            ends_at="2026-01-15T11:00:00Z",
        )
        result, classifier = self._run(sid)
        self.assertTrue(result["ok"])
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertIsNone(sess.course_id)
        self.assertEqual(sess.title, "Lecture 5")
        self.assertEqual(classifier.calls, [])

    def test_time_match_meeting_uses_event_name(self):
        sid, _ev, _ = self._seed(
            event_title="Weekly sync",
            event_kind="meeting",
            starts_at="2026-01-15T10:00:00Z",
            ends_at="2026-01-15T11:00:00Z",
            course="CS101",
        )
        result, classifier = self._run(sid)
        self.assertTrue(result["ok"])
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.kind, "meeting")
        # Meetings are named after the event, not the course.
        self.assertEqual(sess.title, "Weekly sync")
        self.assertEqual(classifier.calls, [])

    def test_no_match_uses_ai_fallback(self):
        # An event far away in time does not match.
        self._seed(
            event_title="Lecture 5",
            event_kind="lecture",
            starts_at="2027-06-01T10:00:00Z",
            ends_at="2027-06-01T11:00:00Z",
            course="CS101",
        )
        sid = _make_ready_session(self)
        result, classifier = self._run(sid)
        self.assertTrue(result["ok"])
        self.assertEqual(len(classifier.calls), 1)
        sess = self.app.config["AFTERWORD_STORAGE"].get_session(sid)
        self.assertEqual(sess.kind, "meeting")  # from the AI stub
        self.assertEqual(sess.kind_confidence, 0.7)

    def test_manual_kind_is_preserved(self):
        sid = _make_ready_session(self)
        store = self.app.config["AFTERWORD_STORAGE"]
        store.set_kind(sid, "voice_memo", confidence=1.0)
        self._seed(
            event_title="Lecture 5",
            event_kind="lecture",
            starts_at="2026-01-15T10:05:00Z",
            ends_at="2026-01-15T11:00:00Z",
            course="CS101",
        )
        result, classifier = self._run(sid)
        self.assertTrue(result["ok"])
        sess = store.get_session(sid)
        self.assertEqual(sess.kind, "voice_memo")
        self.assertEqual(classifier.calls, [])


if __name__ == "__main__":
    unittest.main()
