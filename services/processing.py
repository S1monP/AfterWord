"""Post-finish pipeline orchestrator.

Phase A: a no-op. A later phase will hook the stitching->ready boundary
and run, in order:

  1. ``stt.transcribe(full.wav)``            via OpenRouter
  2. ``llm.summarize(transcript)``           via MiniMax M3
  3. ``llm.extract(transcript, kind)``       via MiniMax M3 (key_points, deadlines, topics)
  4. Persist each result into the ``artifacts`` table and mirror the
     searchable text into ``artifacts_fts``.

The public function :func:`run_post_finish_pipeline` is called by the
``/finish`` endpoint. Its signature is the contract; the body is empty
for Phase A.
"""
from __future__ import annotations

from typing import Optional

from storage import Storage


def run_post_finish_pipeline(
    storage: Storage, session_id: str, *, transcript_provider=None
) -> None:
    """Run any post-stitch work for ``session_id``.

    Phase A: no-op. The function exists so the finish endpoint can be
    wired without a future schema change at the call site.
    """
    return None
