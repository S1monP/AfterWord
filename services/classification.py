"""Recording-kind classification service.

Tier 1: stub. The only operation that does real work is
:meth:`override_kind`, which is the manual-set path used by the
``/unsorted`` page. LLM-driven classification lands in Tier 3.

When that future implementation arrives, it will:
  1. Take a session's transcript (once transcripts exist in
     ``artifacts``).
  2. Call the MiniMax M3 endpoint with a small classifier prompt.
  3. Parse the response into one of {lecture, voice_memo, meeting, other}.
  4. Call :meth:`Storage.set_kind` with the model's confidence score.
  5. Optionally call :meth:`EventsService.suggest_match` to set
     ``matched_session_id`` on a candidate event.
"""
from __future__ import annotations

from typing import Optional


class ClassificationService:
    def classify_session(self, session_id: str) -> Optional[tuple[str, float]]:
        """Run the LLM classifier on a session's transcript.

        Returns ``(kind, confidence)`` or ``None`` if classification
        cannot be performed (e.g. no transcript yet).
        """
        raise NotImplementedError

    def override_kind(self, session_id: str, kind: str) -> None:
        """Set the kind manually. This is the one operation that does
        work in Tier 1: it just writes to the DB via the storage layer.
        """
        from storage import current_app_storage_if_running
        # In Tier 1 this is exposed via the API directly; the service
        # layer is reserved for the LLM path. The actual write happens
        # in app.py's POST /sessions/<id>/kind route.
        raise NotImplementedError(
            "Use POST /api/v1/sessions/<id>/kind instead"
        )

    def bulk_classify(self, session_ids: list[str]) -> dict[str, Optional[tuple[str, float]]]:
        """Classify many sessions. Returns ``{session_id: (kind, conf) or None}``."""
        raise NotImplementedError
