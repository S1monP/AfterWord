"""Recording-kind classification service.

Two paths:

* **Manual override** via :meth:`ClassificationService.override_kind`:
  the user moves a recording to another page ("Move to" in the
  actions menu).

* **LLM-driven classification** via
  :meth:`ClassificationService.classify_session`: calls the
  configured text model with the session's transcript and parses
  the result into one of :data:`VALID_KINDS`. It returns ``None``
  when there is no transcript yet, no text model is configured, or
  the model call fails.

The service is constructed with a :class:`storage.Storage` instance
so it can be tested without a Flask app. Routes get the service via
``app.current_app_classification_service``.
"""

from __future__ import annotations

from storage import Session, Storage

# --- Constants --------------------------------------------------------------

VALID_KINDS: frozenset[str] = frozenset({"lecture", "voice_memo", "meeting", "command", "other"})
#: Kinds offered in the "Classify as" menus. A ``command`` recording
#: (only a request to add a to-do, deadline or calendar entry) is
#: carried out and then kept out of sight, so it is not offered.
SELECTABLE_KINDS: frozenset[str] = VALID_KINDS - {"command"}


# Reason returned by the classify endpoint when the model could not
# classify the session (no transcript yet, no text model configured,
# or the call failed).
LLM_NOT_IMPLEMENTED = "llm_not_implemented"


# --- Service ----------------------------------------------------------------


class ClassificationService:
    """Coordinates manual and (future) LLM classification."""

    def __init__(self, store: Storage) -> None:
        self._store = store

    # -- manual override -------------------------------------------------

    def override_kind(
        self,
        session_id: str,
        kind: str | None,
    ) -> Session:
        """Set the kind manually. Pass ``kind=None`` to clear.

        Manual override uses confidence 1.0 ("the user is certain").
        Raises :class:`KeyError` if the session doesn't exist;
        :class:`ValueError` for unknown kinds or empty session_ids.
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(VALID_KINDS)} or null; got {kind!r}")
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        # set_kind accepts None to clear; confidence of 1.0 marks
        # this as a manual override (vs an LLM guess).
        return self._store.set_kind(
            session_id,
            kind,
            confidence=1.0,
        )

    # -- LLM classify (stub) ---------------------------------------------

    def classify_session(
        self,
        session_id: str,
        *,
        classifier=None,
    ) -> tuple[str, float] | None:
        """Run the LLM classifier on a session's transcript.

        Looks up the most recent ``transcript`` artifact for the
        session and asks the LLM to pick one of the four
        :data:`VALID_KINDS`. The classifier is injectable for
        tests; when omitted, the env-var-driven provider factory
        is used (real ``TextProvider`` when an API key is set,
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
            session_id,
            "transcript",
        )
        if not transcript_artifact or not transcript_artifact.get("content"):
            return None
        transcript = transcript_artifact["content"]
        # Resolve the classifier. Tests pass a fake; production
        # uses the env-var-driven factory. Import lazily to avoid
        # a circular import with ``services.llm``.
        if classifier is None:
            from services.llm import default_providers

            classifier = default_providers().get("text")
        try:
            result = classifier.classify(transcript)
        except Exception:
            # Defensive: any provider failure (LLMError, network
            # error, etc.) collapses to "no result" so the route
            # can surface a clean error to the user.
            return None
        return (result.kind, result.confidence)

    # -- convenience for the /unsorted page -----------------------------


__all__ = [
    "LLM_NOT_IMPLEMENTED",
    "VALID_KINDS",
    "ClassificationService",
]
