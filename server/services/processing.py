"""Post-finish pipeline orchestrator.

The ``/finish`` endpoint stitches the session's audio and leaves it in
``status='ready'``. Right after that, the server kicks off the
post-finish pipeline, which runs the LLM steps in order:

  1. ``TranscriptionProvider.transcribe(full.aac)``  → transcript text
  2. Persist the transcript as an ``artifacts`` row (kind='transcript')
  3. ``TextProvider.classify(transcript)``        → (kind, confidence)
  4. ``Storage.set_kind(...)``                    → session.kind
  5. ``TextProvider.summarize(transcript, kind)`` → Summary
  6. Persist the summary as an ``artifacts`` row (kind='summary')
  7. ``TextProvider.extract(transcript, kind)``   → list of items
  8. Persist the extraction as an ``artifacts`` row (kind='extraction')

Each step updates ``sessions.status`` so the UI can show progress:

    ready → transcribing → transcribed → classifying
          → summarising → extracting → done

Any exception short-circuits the pipeline to ``status='failed'`` with
a useful ``status_detail`` so the user can see what went wrong and
retry.

The function is intentionally synchronous: the device blocks on the
``/finish`` response, so we keep the conversation in one round-trip.
A background-thread version can come later if the latency hurts.

The providers are injectable for tests; see
:func:`run_post_finish_pipeline` for the contract.
"""

from __future__ import annotations

import json
import traceback

import timeutil
from services import filing
from services.events import EventsService
from services.llm import LLMError, LLMProvider, Summary
from storage import (
    STATUS_CLASSIFYING,
    STATUS_DONE,
    STATUS_EXTRACTING,
    STATUS_FAILED,
    STATUS_READY,
    STATUS_SUMMARISING,
    STATUS_TRANSCRIBED,
    STATUS_TRANSCRIBING,
    Storage,
    title_is_placeholder,
)

# Why the pipeline stopped. Used to label the ``failed`` status so the
# UI can distinguish "transcription failed" from "classification failed"
# without re-running anything.
REASON_TRANSCRIBE_FAILED = "transcribe_failed"
REASON_CLASSIFY_FAILED = "classify_failed"
REASON_SUMMARISE_FAILED = "summarise_failed"
REASON_EXTRACT_FAILED = "extract_failed"
REASON_NO_AUDIO = "no_audio"
REASON_NO_TRANSCRIPT = "no_transcript"
REASON_NOT_READY = "not_ready"

#: ``status_detail`` of a session that finished with no speech in it.
NO_SPEECH_DETAIL = "no speech detected"
#: Kinds that keep only their transcript: no summary, no extraction.
TRANSCRIPT_ONLY_KINDS = frozenset({"voice_memo"})
#: Kinds that get no summary. A command recording ("add X to my
#: calendar") still goes through extraction, so its request is filed.
NO_SUMMARY_KINDS = TRANSCRIPT_ONLY_KINDS | {"command"}


def _set_status(
    storage: Storage,
    session_id: str,
    status: str,
    *,
    status_detail: str | None = None,
) -> None:
    """Helper: update status, swallow transition errors as soft fails.

    The transition guard in ``Storage.update_status`` refuses illegal
    jumps (e.g. ``done`` → ``transcribing``). The pipeline never makes
    illegal jumps, but if a race ever sends one through, the helper
    raises :class:`_PipelineError` so the caller can fail the
    session cleanly.
    """
    try:
        storage.set_status(
            session_id,
            status,
            status_detail=status_detail,
        )
    except ValueError as exc:
        raise _PipelineError(
            f"illegal status transition: {exc}",
            reason="bad_transition",
        ) from exc


class _PipelineError(Exception):
    """Internal sentinel for the pipeline. Carries a short reason."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _resolve_session(storage: Storage, session_id: str) -> dict:
    """Pull a fresh session row and sanity-check it."""
    sess = storage.get_session(session_id)
    if sess is None:
        raise _PipelineError(
            f"session {session_id!r} not found",
            reason="not_found",
        )
    if sess.status != STATUS_READY:
        raise _PipelineError(
            f"session {session_id!r} is in status {sess.status!r}, not 'ready'",
            reason=REASON_NOT_READY,
        )
    full_path = storage.open_full_audio(session_id)
    if full_path is None:
        raise _PipelineError(
            f"session {session_id!r} has no full.aac",
            reason=REASON_NO_AUDIO,
        )
    return {
        "session": sess,
        "full_path": full_path,
    }


def run_post_finish_pipeline(
    storage: Storage,
    session_id: str,
    *,
    transcript_provider: LLMProvider | None = None,
    classifier: LLMProvider | None = None,
    llm_provider: LLMProvider | None = None,
    resume: bool = False,
) -> dict | None:
    """Run the post-stitch pipeline for ``session_id``.

    The ``transcript_provider``, ``classifier`` and ``llm_provider``
    parameters are injectable for tests. When they're ``None`` the
    pipeline uses :func:`services.llm.default_providers` to resolve
    them from env vars — real providers when keys are set, no-op
    otherwise.

    The ``llm_provider`` is the text-model (or no-op) provider
    used for summarisation and extraction. It defaults to the
    same ``classifier`` provider, since both call the same model.
    Tests that want to exercise just the classification or just
    the summary path can pass a different one for each.

    With ``resume=True`` (used by the boot sweep for sessions
    stranded by a server restart), steps whose artifact was
    already persisted by a previous run are skipped: an existing
    transcript artifact means the recording is not transcribed
    again (the expensive step), an existing summary artifact means
    summarisation is skipped. Statuses still walk the legal
    transition chain so the transition guard stays satisfied. The
    manual retry endpoint deliberately uses ``resume=False`` (full
    redo).

    The pipeline walks these states:

      1. ``transcribing``  → call transcript_provider.transcribe(wav)
      2. ``transcribed``   → store the transcript artifact
      3. ``classifying``   → call classifier.classify(transcript)
      4. ``summarising``   → call llm_provider.summarize(transcript, kind)
      5. ``extracting``    → call llm_provider.extract(transcript, kind)
      6. ``done``          → all artifacts persisted

    Any exception is caught and the session is moved to
    ``status='failed'`` with a ``status_detail`` explaining the step.
    The pipeline always returns; it never re-raises to its caller.

    Returns a small result dict ``{"ok": bool, "reason": str, ...}``
    on success/failure, or ``None`` if the session was not in the
    expected state to begin with. (None lets ``/finish`` ignore
    duplicate runs.)
    """
    # 1. Pre-flight: is the session in 'ready' and has audio?
    try:
        ctx = _resolve_session(storage, session_id)
    except _PipelineError as exc:
        # Not ready — silently no-op so duplicate /finish calls
        # (or re-runs via /transcribe) don't blow up.
        if exc.reason in (REASON_NOT_READY, "not_found"):
            return None
        # Anything else is unexpected; mark failed.
        return _mark_failed(
            storage,
            session_id,
            exc.reason,
            str(exc),
        )

    # 2. Resolve providers. Tests pass fakes; production uses the
    # env-var-driven factory.
    if transcript_provider is None or classifier is None or llm_provider is None:
        from services.llm import default_providers

        providers = default_providers()
        if transcript_provider is None:
            transcript_provider = providers.get("transcript")
        if classifier is None:
            classifier = providers.get("text")
        if llm_provider is None:
            # Default to the same provider as the classifier:
            # both hit the text model's chat completions.
            llm_provider = classifier

    full_path = ctx["full_path"]

    # 0. Calendar time-match (no AI). Correlate the recording's
    # start with calendar events; on a match the kind, course, and
    # title come from the event, and the AI classifier is skipped
    # later. Only applies when the session has no kind yet, so
    # manual overrides are never clobbered.
    _time_match_session(storage, session_id)

    # 3. Transcribe. On resume, a transcript artifact persisted by
    # a previous run is reused verbatim — the recording is NOT
    # transcribed again. Statuses still walk the legal chain so
    # the transition guard stays satisfied.
    resume_transcript: str | None = None
    if resume:
        try:
            existing = storage.get_artifact(session_id, "transcript")
        except Exception:  # pragma: no cover  (defensive)
            existing = None
        if (
            isinstance(existing, dict)
            and isinstance(existing.get("content"), str)
            and existing["content"].strip()
        ):
            resume_transcript = existing["content"]

    try:
        _set_status(storage, session_id, STATUS_TRANSCRIBING)
    except _PipelineError as exc:
        return _mark_failed(
            storage,
            session_id,
            "bad_transition",
            str(exc),
        )
    if resume_transcript is not None:
        transcript = resume_transcript
    else:
        try:
            transcript = transcript_provider.transcribe(full_path)
        except LLMError as exc:
            return _mark_failed(
                storage,
                session_id,
                REASON_TRANSCRIBE_FAILED,
                f"transcribe: {exc}",
            )
        except Exception as exc:  # pragma: no cover  (defensive)
            return _mark_failed(
                storage,
                session_id,
                REASON_TRANSCRIBE_FAILED,
                f"transcribe: {exc!r}",
            )
    if not isinstance(transcript, str) or not transcript.strip():
        # Silence (or noise) is a finished recording, not a failure:
        # it ends done, without a kind, so it shows on the Other page.
        try:
            storage.delete_artifacts(
                session_id,
                "transcript",
                "summary",
                "extraction",
            )
            _set_status(
                storage,
                session_id,
                STATUS_DONE,
                status_detail=NO_SPEECH_DETAIL,
            )
        except _PipelineError as exc:
            return _mark_failed(
                storage,
                session_id,
                "bad_transition",
                str(exc),
            )
        return {
            "ok": True,
            "kind": None,
            "confidence": None,
            "no_speech": True,
            "transcript_chars": 0,
            "summary_chars": 0,
            "extracted_items": 0,
        }
    # 4. Persist the transcript as an artifact and bump status.
    #    (Skipped on resume — the artifact is already there.)
    if resume_transcript is None:
        try:
            storage.add_artifact(
                session_id,
                "transcript",
                transcript,
                model=getattr(transcript_provider, "name", None),
            )
        except _PipelineError as exc:
            return _mark_failed(
                storage,
                session_id,
                "bad_transition",
                str(exc),
            )
        except Exception as exc:  # pragma: no cover
            return _mark_failed(
                storage,
                session_id,
                REASON_TRANSCRIBE_FAILED,
                f"persist transcript: {exc!r}",
            )
    # 4b. Bump status either way (transcribing → transcribed is legal).
    try:
        _set_status(storage, session_id, STATUS_TRANSCRIBED)
    except _PipelineError as exc:
        return _mark_failed(
            storage,
            session_id,
            "bad_transition",
            str(exc),
        )

    # 5. Classify — unless the kind is already known. The calendar
    # time-match (step 0) sets kind/course/title from the matched
    # event without any AI, and a previous run may have classified
    # already. In both cases the classifier is skipped and we go
    # straight from ``transcribed`` to ``summarising`` (the
    # transition table allows that jump).
    kind: str | None = None
    confidence: float | None = None
    sess_now = storage.get_session(session_id)
    kind_already_set = sess_now is not None and sess_now.kind is not None
    if not kind_already_set:
        # A no-op classifier (no API key configured) is allowed to
        # fail without taking the session to 'failed' — we just
        # stop at 'transcribed' and let the user re-run later.
        try:
            _set_status(storage, session_id, STATUS_CLASSIFYING)
        except _PipelineError as exc:
            return _mark_failed(
                storage,
                session_id,
                "bad_transition",
                str(exc),
            )
        try:
            result = classifier.classify(transcript)
        except LLMError as exc:
            return _mark_failed(
                storage,
                session_id,
                REASON_CLASSIFY_FAILED,
                f"classify: {exc}",
            )
        except Exception as exc:  # pragma: no cover
            return _mark_failed(
                storage,
                session_id,
                REASON_CLASSIFY_FAILED,
                f"classify: {exc!r}",
            )

        # 6. Persist the kind on the session. Status stays in
        # ``classifying`` for now; the next step moves to
        # ``summarising``.
        try:
            kind, confidence = result.kind, result.confidence
            storage.set_kind(
                session_id,
                kind,
                confidence=confidence,
            )
        except _PipelineError as exc:
            return _mark_failed(
                storage,
                session_id,
                "bad_transition",
                str(exc),
            )
        except Exception as exc:  # pragma: no cover
            return _mark_failed(
                storage,
                session_id,
                REASON_CLASSIFY_FAILED,
                f"persist kind: {exc!r}",
            )
    else:
        kind, confidence = sess_now.kind, sess_now.kind_confidence

    # 6b. Voice memos keep only their transcript (shown on the Notes
    # page with the audio), so they skip the summary and extraction.
    if kind in TRANSCRIPT_ONLY_KINDS:
        try:
            topic = llm_provider.suggest_title(transcript)
        except Exception:  # pragma: no cover  (a title is optional)
            topic = ""
        _apply_title(storage, session_id, topic)
        _file_items(storage, session_id, kind, [])  # drop earlier filings
        try:
            storage.delete_artifacts(session_id, "summary", "extraction")
            _set_status(storage, session_id, STATUS_DONE)
        except _PipelineError as exc:
            return _mark_failed(
                storage,
                session_id,
                "bad_transition",
                str(exc),
            )
        # The memo's text lives in a note on the Notes page.
        try:
            from services.notes import ensure_memo_note

            ensure_memo_note(storage, session_id)
        except Exception:  # pragma: no cover  (defensive)
            traceback.print_exc()
        return {
            "ok": True,
            "kind": kind,
            "confidence": confidence,
            "transcript_chars": len(transcript),
            "summary_chars": 0,
            "extracted_items": 0,
        }

    # 7. Summarise. A no-op summariser (no API key configured)
    # returns an empty Summary, so this step always succeeds in
    # the no-key case. On resume, a summary artifact persisted by
    # a previous run is reused instead of re-calling the model.
    resume_summary: Summary | None = None
    if resume:
        try:
            existing = storage.get_artifact(session_id, "summary")
        except Exception:  # pragma: no cover  (defensive)
            existing = None
        if isinstance(existing, dict) and isinstance(
            existing.get("content"),
            str,
        ):
            try:
                prior = json.loads(existing["content"])
                resume_summary = Summary(
                    text=str(prior.get("text", "")),
                    key_points=list(prior.get("key_points", [])),
                    sections=list(prior.get("sections", [])),
                    title=str(prior.get("title", "")),
                )
            except (ValueError, TypeError, AttributeError):
                resume_summary = None  # corrupt artifact; redo
    try:
        _set_status(storage, session_id, STATUS_SUMMARISING)
    except _PipelineError as exc:
        return _mark_failed(
            storage,
            session_id,
            "bad_transition",
            str(exc),
        )
    if kind in NO_SUMMARY_KINDS:
        try:
            topic = llm_provider.suggest_title(transcript)
        except Exception:  # pragma: no cover  (a title is optional)
            topic = ""
        summary = Summary(text="", key_points=[], title=topic)
        storage.delete_artifacts(session_id, "summary")
    elif resume_summary is not None:
        summary = resume_summary
        summary_payload = _summary_payload(summary)
    else:
        try:
            summary = llm_provider.summarize(transcript, kind=kind)
            summary_payload = _summary_payload(summary)
        except LLMError as exc:
            return _mark_failed(
                storage,
                session_id,
                REASON_SUMMARISE_FAILED,
                f"summarise: {exc}",
            )
        except Exception as exc:  # pragma: no cover
            return _mark_failed(
                storage,
                session_id,
                REASON_SUMMARISE_FAILED,
                f"summarise: {exc!r}",
            )
        try:
            storage.add_artifact(
                session_id,
                "summary",
                summary_payload,
                model=getattr(llm_provider, "name", None),
            )
        except _PipelineError as exc:
            return _mark_failed(
                storage,
                session_id,
                "bad_transition",
                str(exc),
            )
        except Exception as exc:  # pragma: no cover
            return _mark_failed(
                storage,
                session_id,
                REASON_SUMMARISE_FAILED,
                f"persist summary: {exc!r}",
            )
    _apply_title(storage, session_id, summary.title)

    # 8. Extract. Same pattern: LLM error → mark failed;
    # otherwise persist as an ``extraction`` artifact.
    try:
        _set_status(storage, session_id, STATUS_EXTRACTING)
    except _PipelineError as exc:
        return _mark_failed(
            storage,
            session_id,
            "bad_transition",
            str(exc),
        )
    try:
        items = llm_provider.extract(
            transcript,
            kind,
            recorded_at=_recorded_at(storage.get_session(session_id)),
        )
    except LLMError as exc:
        return _mark_failed(
            storage,
            session_id,
            REASON_EXTRACT_FAILED,
            f"extract: {exc}",
        )
    except Exception as exc:  # pragma: no cover
        return _mark_failed(
            storage,
            session_id,
            REASON_EXTRACT_FAILED,
            f"extract: {exc!r}",
        )
    try:
        storage.add_artifact(
            session_id,
            "extraction",
            json.dumps({"items": list(items)}),
            model=getattr(llm_provider, "name", None),
        )
        filed = _file_items(storage, session_id, kind, items)
        _set_status(storage, session_id, STATUS_DONE)
    except _PipelineError as exc:
        return _mark_failed(
            storage,
            session_id,
            "bad_transition",
            str(exc),
        )
    except Exception as exc:  # pragma: no cover
        return _mark_failed(
            storage,
            session_id,
            REASON_EXTRACT_FAILED,
            f"persist extraction: {exc!r}",
        )

    # 9. A request recording ("remind me to ...") is not kept once
    # what it asked for is filed. If nothing could be filed it was
    # probably misheard, so it moves to Other instead of vanishing.
    deleted = False
    if kind in filing.UNLINKED_KINDS:
        if filed:
            deleted = storage.delete_session(session_id)
        else:
            storage.set_kind(session_id, "other", confidence=confidence)

    return {
        "ok": True,
        "kind": kind,
        "confidence": confidence,
        "transcript_chars": len(transcript),
        "summary_chars": len(summary.text),
        "extracted_items": len(items),
        "deleted": deleted,
    }


def _summary_payload(summary: Summary) -> str:
    """Serialise a Summary as JSON so the storage layer doesn't need
    to know the dataclass shape. ``text`` is the overview,
    ``sections`` a list of ``{"heading", "text"}``, ``key_points``
    a list of strings."""
    return json.dumps(
        {
            "title": summary.title,
            "text": summary.text,
            "sections": list(summary.sections),
            "key_points": list(summary.key_points),
        }
    )


def _recorded_at(sess) -> str | None:
    """When a recording was made, for the extraction prompt (so "next
    Friday" can be worked out): "Thursday 30 October 2026, 13:10"."""
    local = timeutil.to_local(sess.started_at) if sess else None
    return local.strftime("%A %d %B %Y, %H:%M") if local else None


def _file_items(
    storage: Storage,
    session_id: str,
    kind: str | None,
    items: list,
) -> int:
    """Copy deadlines / to-dos / events into the user's lists (only for
    :data:`filing.FILING_KINDS`; other kinds just drop what an earlier
    run filed) and return how many were filed. Never fails the
    pipeline."""
    try:
        counts = filing.refile(
            storage,
            session_id,
            list(items) if kind in filing.FILING_KINDS else [],
        )
    except Exception:  # pragma: no cover  (defensive)
        traceback.print_exc()
        return 0
    return sum(counts.values())


def _apply_title(storage: Storage, session_id: str, topic: str) -> None:
    """Name a recording from its AI topic. A recording matched to a
    calendar event is "<course or event>: <topic>"; one renamed by
    hand keeps its name. Never fails the pipeline."""
    try:
        sess = storage.get_session(session_id)
        if sess is None or not title_is_placeholder(sess):
            return
        event = storage.event_for_session(session_id)
        base = _calendar_base(event, storage) if event else None
        title = f"{base}: {topic}" if base and topic else (base or topic)
        if title:
            storage.set_session_title(
                session_id,
                title[:200],
                source="calendar" if base else "ai",
            )
    except Exception:  # pragma: no cover  (defensive)
        traceback.print_exc()


def _time_match_session(storage: Storage, session_id: str) -> bool:
    """Calendar time-match (pipeline step 0): correlate the
    recording's start with a calendar event — no AI involved.

    On a match, the session's kind, course, and title come from the
    event, and the event is marked as the session's match (so the
    calendar shows the pairing). Returns ``True`` when a match was
    applied.

    Only sessions without a kind are matched, so manual overrides
    and previous runs are never clobbered. Any failure is
    swallowed: the time-match is an enhancement, and the AI
    classifier remains the fallback.
    """
    try:
        sess = storage.get_session(session_id)
        if sess is None or sess.kind is not None:
            return False
        events = EventsService(storage)
        event = events.suggest_match(session_id)
        if event is None:
            return False
        if event.kind in storage.RECORDING_KINDS:
            storage.set_kind(session_id, event.kind, confidence=1.0)
        if event.course_id:
            try:
                storage.set_session_course(session_id, event.course_id)
            except (KeyError, ValueError):
                pass  # course vanished; leave the session unassigned
        if title_is_placeholder(sess):
            try:
                storage.set_session_title(
                    session_id,
                    _calendar_base(event, storage),
                    source="calendar",
                )
            except (KeyError, ValueError):
                pass
        events.match_session(event.id, session_id, 1.0)
        return True
    except Exception:  # pragma: no cover  (defensive)
        traceback.print_exc()
        return False


def _calendar_base(event, storage: Storage) -> str:
    """The calendar part of a matched recording's name: the course for
    a lecture, else the event's own title. The date is not part of the
    name; pages show it on a line of its own."""
    if event.kind == "lecture" and event.course_id:
        course = storage.get_course(event.course_id)
        if course:
            return course.name
    return event.title


def _mark_failed(
    storage: Storage,
    session_id: str,
    reason: str,
    detail: str,
) -> dict:
    """Move the session to ``status='failed'`` with a useful detail.

    Uses a direct DB write (bypassing the transition guard) because
    the failed status is reachable from any prior pipeline state.
    """
    try:
        storage.update_status(
            session_id,
            STATUS_FAILED,
            status_detail=detail,
        )
    except Exception:  # pragma: no cover
        # If even the failed-state write fails, log it but don't
        # re-raise — the pipeline must always return.
        traceback.print_exc()
    return {"ok": False, "reason": reason, "detail": detail}
