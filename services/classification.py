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
        self, session_id: str,
    ) -> Optional[tuple[str, float]]:
        """Run the LLM classifier on a session's transcript.

        **Currently a stub** (Milestone 6): always returns ``None``
        regardless of the session's state. The contract is stable
        so callers can rely on the return-type signature; when the
        LLM pipeline is wired up in Milestone 8, this will either
        return ``(kind, confidence)`` (success) or ``None``
        (transcript missing, classification failed, etc.).

        Raises :class:`KeyError` if the session doesn't exist;
        :class:`ValueError` for an empty session_id.
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        # No transcript exists yet (M8); the LLM path returns None.
        return None

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