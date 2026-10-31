"""Background AI pipeline: FIFO worker, in-flight guard, boot sweep.
"""
from __future__ import annotations

import queue
import threading

from flask import Flask

import storage
from services import processing


# ---------------------------------------------------------------------------
# Pipeline bookkeeping (shared by the upload + retry routes)
# ---------------------------------------------------------------------------

# Post-finish statuses the manual retry endpoint accepts. This
# includes the intermediate pipeline states so sessions stranded by
# a server restart mid-pipeline (e.g. killed while 'transcribing')
# can be reset to 'ready' and re-run — the recovery path for that
# situation. Every transition to 'ready' here is legal per
# storage.ALLOWED_STATUS_TRANSITIONS.
_RETRYABLE_STATUSES = frozenset({
    storage.STATUS_READY,
    storage.STATUS_TRANSCRIBING,
    storage.STATUS_TRANSCRIBED,
    storage.STATUS_CLASSIFYING,
    storage.STATUS_SUMMARISING,
    storage.STATUS_EXTRACTING,
    storage.STATUS_DONE,
    storage.STATUS_FAILED,
})

# Statuses a server restart can strand mid-pipeline. The boot sweep
# resets these to 'ready' and requeues them. 'failed' is deliberately
# excluded — the failure may be a missing API key, which would loop
# forever; failed sessions are retried manually via the /transcribe
# endpoint. 'ready' sessions are swept too (a crash can strike in the
# gap between stitch and pipeline start), but only when no transcript
# artifact exists yet (i.e. the pipeline never got to persist one).
_STRANDED_STATUSES = frozenset({
    storage.STATUS_READY,
    storage.STATUS_TRANSCRIBING,
    storage.STATUS_TRANSCRIBED,
    storage.STATUS_CLASSIFYING,
    storage.STATUS_SUMMARISING,
    storage.STATUS_EXTRACTING,
})


def _enqueue_pipeline(app: Flask, session_id: str) -> bool:
    """Queue a session for the background pipeline worker.

    The worker processes sessions strictly one at a time (FIFO), so
    three lectures recorded in a day are transcribed sequentially
    instead of hammering the transcription API in parallel.

    Returns ``False`` when a pipeline for this session is already
    queued or running (the in-flight guard), ``True`` otherwise.
    """
    with _PIPELINE_LOCK:
        if session_id in _PIPELINES_IN_FLIGHT:
            return False
        _PIPELINES_IN_FLIGHT.add(session_id)
    app.config["LR_PIPELINE_QUEUE"].put(session_id)
    return True


def set_kind_by_hand(
    app: Flask, session_id: str, kind, *, confidence=None,
) -> storage.Session:
    """Set a session's kind by hand (``confidence`` defaults to 1.0).

    When a finished session with a transcript changes kind, its
    summary and extraction are dropped and the pipeline runs again
    with ``resume=True``: the transcript is reused and the kind is
    kept, so only the text steps run, now written for the new kind
    (a voice memo just loses them). Raises ``ValueError`` for a bad
    kind, like :meth:`storage.Storage.set_kind`.
    """
    store = app.config["LR_STORAGE"]
    before = store.get_session(session_id)
    if confidence is None and kind is not None:
        confidence = 1.0
    sess = store.set_kind(session_id, kind, confidence=confidence)
    if before is not None and before.kind == "voice_memo" and kind != "voice_memo":
        # Moved away from Notes: the memo's note goes (it was the memo).
        note = store.note_for_recording(session_id)
        if note is not None:
            store.delete_note(note.id)
    if (
        kind is not None
        and before is not None
        and before.kind != kind
        and before.status == storage.STATUS_DONE
        and store.get_artifact(session_id, "transcript") is not None
    ):
        store.delete_artifacts(session_id, "summary", "extraction")
        store.set_status(session_id, storage.STATUS_READY, status_detail=None)
        if app.config.get("LR_PIPELINE_BACKGROUND", True):
            _enqueue_pipeline(app, session_id)
        else:
            providers = app.config["LR_LLM_PROVIDERS"]
            processing.run_post_finish_pipeline(
                store, session_id,
                transcript_provider=providers.get("transcript"),
                classifier=providers.get("text"),
                llm_provider=providers.get("text"),
                resume=True,
            )
        sess = store.get_session(session_id)
    return sess


def _start_pipeline_worker(app: Flask) -> threading.Thread:
    """Start the FIFO pipeline worker for ``app`` (one per app, daemon).

    The worker resolves providers from ``app.config`` on every
    iteration (cheap), so tests can swap ``LR_LLM_PROVIDERS`` and
    the next queued run picks up the stubs. Runs the pipeline with
    ``resume=True``: artifacts persisted before a crash are reused.
    """
    q = app.config["LR_PIPELINE_QUEUE"] = queue.Queue()

    def _worker() -> None:
        while True:
            session_id = q.get()
            try:
                store = app.config["LR_STORAGE"]
                providers = app.config["LR_LLM_PROVIDERS"]
                processing.run_post_finish_pipeline(
                    store, session_id,
                    transcript_provider=providers.get("transcript"),
                    classifier=providers.get("text"),
                    llm_provider=providers.get("text"),
                    resume=True,
                )
            except Exception as exc:  # pragma: no cover
                print(
                    f"[LR ] background pipeline error: {exc!r}",
                    flush=True,
                )
            finally:
                with _PIPELINE_LOCK:
                    _PIPELINES_IN_FLIGHT.discard(session_id)
                q.task_done()

    t = threading.Thread(
        target=_worker, name="lr-pipeline-worker", daemon=True,
    )
    t.start()
    return t


def sweep_stranded_sessions(app: Flask) -> list[str]:
    """Reset and requeue sessions stranded by a server restart.

    A crash mid-pipeline leaves the session in an intermediate
    status with no thread driving it — and because stitching
    deleted the chunk files, the device can't help either. On boot
    (and callable from tests), every such session is reset to
    ``ready`` — a legal transition — and requeued on the FIFO
    worker with ``resume=True`` so already-persisted artifacts
    (transcript, summary) are not recomputed.

    Sessions in ``failed`` are NOT auto-retried; see
    ``_STRANDED_STATUSES``. Returns the requeued session ids.
    """
    store = app.config["LR_STORAGE"]
    requeued: list[str] = []
    for sess in reversed(store.list_sessions()):
        # list_sessions is newest-first; reversed → oldest first so
        # the queue drains in recording order.
        if sess.status not in _STRANDED_STATUSES:
            continue
        if sess.status == storage.STATUS_READY:
            # Only when the pipeline never persisted anything:
            # a ready session WITH a transcript artifact was reset
            # by something else and will be handled deliberately.
            try:
                if store.get_artifact(sess.id, "transcript") is not None:
                    continue
            except Exception:  # pragma: no cover  (defensive)
                pass
        try:
            if sess.status != storage.STATUS_READY:
                store.set_status(
                    sess.id, storage.STATUS_READY, status_detail=None,
                )
        except ValueError:
            continue  # unresolvable transition; leave for manual retry
        if _enqueue_pipeline(app, sess.id):
            requeued.append(sess.id)
    if requeued:
        print(
            f"[LR ] boot sweep: requeued {len(requeued)} stranded "
            "session(s)",
            flush=True,
        )
    return requeued


# Sessions with a pipeline running in this process right now, so a
# background upload run and a manual retry cannot both drive the
# same session concurrently.
_PIPELINE_LOCK = threading.Lock()
_PIPELINES_IN_FLIGHT: set[str] = set()
