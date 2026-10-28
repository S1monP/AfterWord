"""Post-finish pipeline orchestrator (Milestones 8 + 9).

The ``/finish`` endpoint stitches the session's audio and leaves it in
``status='ready'``. Right after that, the server kicks off the
post-finish pipeline, which runs the LLM steps in order:

  1. ``TranscriptionProvider.transcribe(full.aac)``  → transcript text
  2. Persist the transcript as an ``artifacts`` row (kind='transcript')
  3. ``TextProvider.classify(transcript)``        → (kind, confidence)
  4. ``Storage.set_kind(...)``                    → session.kind
  5. ``TextProvider.summarize(transcript)``       → Summary
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
from typing import Optional

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
)

from services.events import EventsService
from services.llm import LLMError, LLMProvider


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


def _set_status(
    storage: Storage,
    session_id: str,
    status: str,
    *,
    status_detail: Optional[str] = None,
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
            session_id, status, status_detail=status_detail,
        )
    except ValueError as exc:
        raise _PipelineError(
            f"illegal status transition: {exc}", reason="bad_transition",
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
            f"session {session_id!r} not found", reason="not_found",
        )
    if sess.status != STATUS_READY:
        raise _PipelineError(
            f"session {session_id!r} is in status {sess.status!r}, "
            f"not 'ready'",
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
    transcript_provider: Optional[LLMProvider] = None,
    classifier: Optional[LLMProvider] = None,
    llm_provider: Optional[LLMProvider] = None,
    resume: bool = False,
) -> Optional[dict]:
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
      4. ``summarising``   → call llm_provider.summarize(transcript)
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
            storage, session_id, exc.reason, str(exc),
        )

    # 2. Resolve providers. Tests pass fakes; production uses the
    # env-var-driven factory.
    if (
        transcript_provider is None
        or classifier is None
        or llm_provider is None
    ):
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
    resume_transcript: Optional[str] = None
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
            storage, session_id, "bad_transition", str(exc),
        )
    if resume_transcript is not None:
        transcript = resume_transcript
    else:
        try:
            transcript = transcript_provider.transcribe(full_path)
        except LLMError as exc:
            return _mark_failed(
                storage, session_id, REASON_TRANSCRIBE_FAILED,
                f"transcribe: {exc}",
            )
        except Exception as exc:  # pragma: no cover  (defensive)
            return _mark_failed(
                storage, session_id, REASON_TRANSCRIBE_FAILED,
                f"transcribe: {exc!r}",
            )
    if not isinstance(transcript, str) or not transcript.strip():
        return _mark_failed(
            storage, session_id, REASON_TRANSCRIBE_FAILED,
            "transcribe: empty result",
        )
    # 4. Persist the transcript as an artifact and bump status.
    #    (Skipped on resume — the artifact is already there.)
    if resume_transcript is None:
        try:
            storage.add_artifact(
                session_id, "transcript", transcript,
                model=getattr(transcript_provider, "name", None),
            )
        except _PipelineError as exc:
            return _mark_failed(
                storage, session_id, "bad_transition", str(exc),
            )
        except Exception as exc:  # pragma: no cover
            return _mark_failed(
                storage, session_id, REASON_TRANSCRIBE_FAILED,
                f"persist transcript: {exc!r}",
            )
    # 4b. Bump status either way (transcribing → transcribed is legal).
    try:
        _set_status(storage, session_id, STATUS_TRANSCRIBED)
    except _PipelineError as exc:
        return _mark_failed(
            storage, session_id, "bad_transition", str(exc),
        )

    # 5. Classify — unless the kind is already known. The calendar
    # time-match (step 0) sets kind/course/title from the matched
    # event without any AI, and a previous run may have classified
    # already. In both cases the classifier is skipped and we go
    # straight from ``transcribed`` to ``summarising`` (the
    # transition table allows that jump).
    kind: Optional[str] = None
    confidence: Optional[float] = None
    sess_now = storage.get_session(session_id)
    kind_already_set = (
        sess_now is not None and sess_now.kind is not None
    )
    if not kind_already_set:
        # A no-op classifier (no API key configured) is allowed to
        # fail without taking the session to 'failed' — we just
        # stop at 'transcribed' and let the user re-run later.
        try:
            _set_status(storage, session_id, STATUS_CLASSIFYING)
        except _PipelineError as exc:
            return _mark_failed(
                storage, session_id, "bad_transition", str(exc),
            )
        try:
            result = classifier.classify(transcript)
        except LLMError as exc:
            return _mark_failed(
                storage, session_id, REASON_CLASSIFY_FAILED,
                f"classify: {exc}",
            )
        except Exception as exc:  # pragma: no cover
            return _mark_failed(
                storage, session_id, REASON_CLASSIFY_FAILED,
                f"classify: {exc!r}",
            )

        # 6. Persist the kind on the session. Status stays in
        # ``classifying`` for now; the next step moves to
        # ``summarising``.
        try:
            kind, confidence = result.kind, result.confidence
            storage.set_kind(
                session_id, kind, confidence=confidence,
            )
        except _PipelineError as exc:
            return _mark_failed(
                storage, session_id, "bad_transition", str(exc),
            )
        except Exception as exc:  # pragma: no cover
            return _mark_failed(
                storage, session_id, REASON_CLASSIFY_FAILED,
                f"persist kind: {exc!r}",
            )
    else:
        kind, confidence = sess_now.kind, sess_now.kind_confidence

    # 7. Summarise. A no-op summariser (no API key configured)
    # returns an empty Summary, so this step always succeeds in
    # the no-key case. On resume, a summary artifact persisted by
    # a previous run is reused instead of re-calling the model.
    resume_summary: Optional[Summary] = None
    if resume:
        try:
            existing = storage.get_artifact(session_id, "summary")
        except Exception:  # pragma: no cover  (defensive)
            existing = None
        if isinstance(existing, dict) and isinstance(
            existing.get("content"), str,
        ):
            try:
                prior = json.loads(existing["content"])
                resume_summary = Summary(
                    text=str(prior.get("text", "")),
                    key_points=list(prior.get("key_points", [])),
                )
            except (ValueError, TypeError, AttributeError):
                resume_summary = None  # corrupt artifact; redo
    try:
        _set_status(storage, session_id, STATUS_SUMMARISING)
    except _PipelineError as exc:
        return _mark_failed(
            storage, session_id, "bad_transition", str(exc),
        )
    if resume_summary is not None:
        summary = resume_summary
        summary_payload = json.dumps({
            "text": summary.text,
            "key_points": list(summary.key_points),
        })
    else:
        try:
            summary = llm_provider.summarize(transcript)
            # Serialise the Summary as JSON so the storage layer
            # doesn't need to know the dataclass shape. ``text`` is
            # the display string; ``key_points`` is a list.
            summary_payload = json.dumps({
                "text": summary.text,
                "key_points": list(summary.key_points),
            })
        except LLMError as exc:
            return _mark_failed(
                storage, session_id, REASON_SUMMARISE_FAILED,
                f"summarise: {exc}",
            )
        except Exception as exc:  # pragma: no cover
            return _mark_failed(
                storage, session_id, REASON_SUMMARISE_FAILED,
                f"summarise: {exc!r}",
            )
        try:
            storage.add_artifact(
                session_id, "summary", summary_payload,
                model=getattr(llm_provider, "name", None),
            )
        except _PipelineError as exc:
            return _mark_failed(
                storage, session_id, "bad_transition", str(exc),
            )
        except Exception as exc:  # pragma: no cover
            return _mark_failed(
                storage, session_id, REASON_SUMMARISE_FAILED,
                f"persist summary: {exc!r}",
            )

    # 8. Extract. Same pattern: LLM error → mark failed;
    # otherwise persist as an ``extraction`` artifact.
    try:
        _set_status(storage, session_id, STATUS_EXTRACTING)
    except _PipelineError as exc:
        return _mark_failed(
            storage, session_id, "bad_transition", str(exc),
        )
    try:
        items = llm_provider.extract(transcript, kind)
    except LLMError as exc:
        return _mark_failed(
            storage, session_id, REASON_EXTRACT_FAILED,
            f"extract: {exc}",
        )
    except Exception as exc:  # pragma: no cover
        return _mark_failed(
            storage, session_id, REASON_EXTRACT_FAILED,
            f"extract: {exc!r}",
        )
    try:
        storage.add_artifact(
            session_id, "extraction",
            json.dumps({"items": list(items)}),
            model=getattr(llm_provider, "name", None),
        )
        _set_status(storage, session_id, STATUS_DONE)
    except _PipelineError as exc:
        return _mark_failed(
            storage, session_id, "bad_transition", str(exc),
        )
    except Exception as exc:  # pragma: no cover
        return _mark_failed(
            storage, session_id, REASON_EXTRACT_FAILED,
            f"persist extraction: {exc!r}",
        )

    return {
        "ok": True,
        "kind": kind,
        "confidence": confidence,
        "transcript_chars": len(transcript),
        "summary_chars": len(summary.text),
        "extracted_items": len(items),
    }


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
        try:
            storage.set_session_title(
                session_id, _event_title_for_session(event, storage, sess),
            )
        except (KeyError, ValueError):
            pass
        events.match_session(event.id, session_id, 1.0)
        return True
    except Exception:  # pragma: no cover  (defensive)
        traceback.print_exc()
        return False


def _event_title_for_session(
    event, storage: Storage, session,
) -> str:
    """Human name for a time-matched recording.

    Lectures are named after their course ("CS101 · 15 Oct 2026"),
    other events after the event itself ("Weekly sync · 15 Oct
    2026"). If the exact title already exists on another session,
    the recording start time disambiguates ("… · 14:05").
    """
    try:
        date_part = _fmt_event_date(event.starts_at)
    except (ValueError, AttributeError):
        date_part = ""
    if event.kind == "lecture" and event.course_id:
        course = storage.get_course(event.course_id)
        base = course.name if course else event.title
    else:
        base = event.title
    title = f"{base} · {date_part}" if date_part else base
    taken = any(
        s.id != session.id and s.title == title
        for s in storage.list_sessions()
    )
    if taken and session.started_at:
        title = f"{title} · {session.started_at[11:16]}"
    return title


def _fmt_event_date(iso_ts: str) -> str:
    """Format an ISO-8601 timestamp as a human date ("15 Oct 2026")."""
    import datetime as _dt
    dt = _dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return dt.strftime("%d %b %Y")


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
            session_id, STATUS_FAILED, status_detail=detail,
        )
    except Exception:  # pragma: no cover
        # If even the failed-state write fails, log it but don't
        # re-raise — the pipeline must always return.
        traceback.print_exc()
    return {"ok": False, "reason": reason, "detail": detail}


