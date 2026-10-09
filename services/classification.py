"""Recording-kind classification service.

Two paths:

* **Manual override** via :meth:`ClassificationService.override_kind` —
  the user picks a kind from a dropdown on the ``/unsorted`` page.
  This is fully implemented; it's the only path that does real
  work in Tier 1 / Milestone 6.

* **LLM-driven classification** via
  :meth:`ClassificationService.classify_session` — calls the
  MiniMax M3 endpoint with the session's transcript (once
  transcripts exist) and parses the result into one of
  ``{lecture, voice_memo, meeting, other}``.

  In Milestone 6 this method is a **stub** that always returns
  ``None``. The real implementation lands in Milestone 8 alongside
  the transcription pipeline: the LLM call needs a transcript,
  which doesn't exist yet. The contract is stable, so the API
  routes and the ``/unsorted`` UI can ship now and just light up
  the LLM button automatically when the backend is ready.

The service is constructed with a :class:`storage.Storage` instance
so it can be tested without a Flask app. Routes get the service via
``app.current_app_classification_service``.
"""
from __future__ import annotations

from typing import Optional

import storage
from storage import Session, Storage


# --- Constants --------------------------------------------------------------

VALID_KINDS: frozenset[str] = frozenset(
    {"lecture", "voice_memo", "meeting", "other"}
)


# Reason returned by the LLM classify endpoint when the AI path
# isn't implemented yet. The UI uses this to surface a friendly
# message ("AI classification lands in a later milestone — pick a
# kind manually for now").
LLM_NOT_IMPLEMENTED = "llm_not_implemented"


# --- Service ----------------------------------------------------------------

class ClassificationService:
    """Coordinates manual and (future) LLM classification."""

    def __init__(self, store: Storage) -> None:
        self._store = store

    # -- manual override -------------------------------------------------

    def override_kind(
        self, session_id: str, kind: Optional[str],
    ) -> Session:
        """Set the kind manually. Pass ``kind=None`` to clear.

        Manual override uses confidence 1.0 ("the user is certain").
        Raises :class:`KeyError` if the session doesn't exist;
        :class:`ValueError` for unknown kinds or empty session_ids.
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(VALID_KINDS)} or null; "
                f"got {kind!r}"
            )
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        # set_kind accepts None to clear; confidence of 1.0 marks
        # this as a manual override (vs an LLM guess).
        return self._store.set_kind(
            session_id, kind, confidence=1.0,
        )

    # -- LLM classify (stub) ---------------------------------------------

    def classify_session(
        self, session_id: str, *, classifier=None,
    ) -> Optional[tuple[str, float]]:
        """Run the LLM classifier on a session's transcript.

        Looks up the most recent ``transcript`` artifact for the
        session and asks the LLM to pick one of the four
        :data:`VALID_KINDS`. The classifier is injectable for
        tests; when omitted, the env-var-driven provider factory
        is used (real ``MiniMaxProvider`` when an API key is set,
        no-op otherwise).

        Returns ``(kind, confidence)`` on success, or ``None`` if
        the session has no transcript yet, the classifier raised,
        or the LLM returned an unrecognised response.

        The caller (the API route) is responsible for persisting
        the returned kind via :meth:`Storage.set_kind`; this
        method does NOT mutate the session.

        Raises :class:`KeyError` if the session doesn't exist;
        :class:`ValueError` for an empty session_id.
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        # No transcript yet → the LLM can't classify. Return None
        # so the route surfaces a friendly "transcribe first"
        # message instead of a stack trace.
        transcript_artifact = self._store.get_artifact(
            session_id, "transcript",
        )
        if not transcript_artifact or not transcript_artifact.get("content"):
            return None
        transcript = transcript_artifact["content"]
        # Resolve the classifier. Tests pass a fake; production
        # uses the env-var-driven factory. Import lazily to avoid
        # a circular import with ``services.llm``.
        if classifier is None:
            from services.llm import default_providers
            classifier = default_providers().get("classifier")
        try:
            result = classifier.classify(transcript)
        except Exception:
            # Defensive: any provider failure (LLMError, network
            # error, etc.) collapses to "no result" so the route
            # can surface a clean error to the user.
            return None
        return (result.kind, result.confidence)

    def bulk_classify(
        self, session_ids: list[str],
    ) -> dict[str, Optional[tuple[str, float]]]:
        """Classify many sessions in one call. Returns a dict
        mapping ``session_id`` to the same shape as
        :meth:`classify_session`. Sessions that don't exist are
        omitted from the result (rather than raising).

        **Currently a stub** (Milestone 6): every value is
        ``None``.
        """
        if not isinstance(session_ids, list):
            raise ValueError("session_ids must be a list")
        out: dict[str, Optional[tuple[str, float]]] = {}
        for sid in session_ids:
            try:
                out[sid] = self.classify_session(sid)
            except (KeyError, ValueError):
                continue
        return out

    # -- convenience for the /unsorted page -----------------------------

    def list_unclassified(
        self, *, limit: int = 100, offset: int = 0,
    ) -> tuple[list[Session], int]:
        """List sessions that need classifying: ``kind IS NULL`` and
        ``status = 'ready'``. Newest first (by ``created_at``).

        Returns ``(sessions, total)``. ``total`` is the count of
        matches BEFORE limit/offset are applied so the UI can render
        pagination.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        return self._store.list_unclassified_sessions(
            limit=limit, offset=offset,
        )


__all__ = [
    "ClassificationService",
    "VALID_KINDS",
    "LLM_NOT_IMPLEMENTED",
]