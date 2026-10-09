"""Post-finish pipeline orchestrator (Milestones 8 + 9).

The ``/finish`` endpoint stitches the session's audio and leaves it in
``status='ready'``. Right after that, the server kicks off the
post-finish pipeline, which runs the LLM steps in order:

  1. ``OpenRouterProvider.transcribe(full.wav)``  → transcript text
  2. Persist the transcript as an ``artifacts`` row (kind='transcript')
  3. ``MiniMaxProvider.classify(transcript)``     → (kind, confidence)
  4. ``Storage.set_kind(...)``                    → session.kind
  5. ``MiniMaxProvider.summarize(transcript)``    → Summary
  6. Persist the summary as an ``artifacts`` row (kind='summary')
  7. ``MiniMaxProvider.extract(transcript, kind)`` → list of items
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
            f"session {session_id!r} has no full.wav",
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
) -> Optional[dict]:
    """Run the post-stitch pipeline for ``session_id``.

    The ``transcript_provider``, ``classifier`` and ``llm_provider``
    parameters are injectable for tests. When they're ``None`` the
    pipeline uses :func:`services.llm.default_providers` to resolve
    them from env vars — real providers when keys are set, no-op
    otherwise.

    The ``llm_provider`` is the MiniMax (or no-op) provider used for
    summarisation and extraction. It defaults to the same
    ``classifier`` provider, since both call the same model. Tests
    that want to exercise just the classification or just the
    summary path can pass a different one for each.

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
            classifier = providers.get("classifier")
        if llm_provider is None:
            # Default to the same provider as the classifier:
            # both hit MiniMax M3 chat completions.
            llm_provider = classifier

    full_path = ctx["full_path"]

    # 3. Transcribe.
    try:
        _set_status(storage, session_id, STATUS_TRANSCRIBING)
    except _PipelineError as exc:
        return _mark_failed(
            storage, session_id, "bad_transition", str(exc),
        )
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
    try:
        storage.add_artifact(
            session_id, "transcript", transcript,
            model=getattr(transcript_provider, "name", None),
        )
        _set_status(storage, session_id, STATUS_TRANSCRIBED)
    except _PipelineError as exc:
        return _mark_failed(
            storage, session_id, "bad_transition", str(exc),
        )
    except Exception as exc:  # pragma: no cover
        return _mark_failed(
            storage, session_id, REASON_TRANSCRIBE_FAILED,
            f"persist transcript: {exc!r}",
        )

    # 5. Classify. A no-op classifier (no API key configured) is
    # allowed to fail without taking the session to 'failed' — we
    # just stop at 'transcribed' and let the user re-run later.
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
    # ``classifying`` for now; the next step moves to ``summarising``.
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

    # 7. Summarise. A no-op summariser (no API key configured)
    # returns an empty Summary, so this step always succeeds in
    # the no-key case.
    try:
        _set_status(storage, session_id, STATUS_SUMMARISING)
    except _PipelineError as exc:
        return _mark_failed(
            storage, session_id, "bad_transition", str(exc),
        )
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


