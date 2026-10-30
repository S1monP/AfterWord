"""Recordings API: device upload protocol, session list/detail/audio,
pipeline retry, classification, search and web audio upload.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Optional

from flask import abort, Blueprint, current_app, jsonify, request, send_file

import search
import services
import services.classification  # noqa: F401
import storage
import timeutil
from services import processing, transcode
from web.common import (
    _default_title_from_iso,
    _file_mtime_iso,
    _normalise_title,
    current_app_classification_service,
    current_app_storage,
    current_app_text_provider,
    current_app_transcript_provider,
)
from web.pipeline import (
    _PIPELINES_IN_FLIGHT,
    _PIPELINE_LOCK,
    _RETRYABLE_STATUSES,
    _enqueue_pipeline,
    set_kind_by_hand,
)


bp = Blueprint("sessions_api", __name__)


# --- API: sessions (create / list / get) ----------------------------
@bp.post("/api/v1/sessions")
def api_create_session() -> Any:
    payload = request.get_json(silent=True) or {}
    session_id = payload.get("id") or storage.new_session_id()
    if not storage.is_valid_session_id(session_id):
        return jsonify({
            "error": "invalid_session_id",
            "detail": "id must be 4-64 chars of [A-Za-z0-9_-]",
        }), 400
    try:
        expected_count = int(payload.get("expected_count", 0))
        sample_rate = int(payload.get("sample_rate", 22050))
        channels = int(payload.get("channels", 1))
        bits_per_sample = int(payload.get("bits_per_sample", 32))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_audio_format"}), 400
    # The device sends local wall-clock time without an offset.
    started_at = payload.get("started_at") or storage.now_iso()
    try:
        started_at = timeutil.to_utc_iso(str(started_at))
    except ValueError:
        return jsonify({
            "error": "invalid_payload",
            "detail": "started_at must be ISO-8601",
        }), 400
    title = payload.get("title")

    try:
        sess, created = current_app_storage().upsert_session(
            session_id,
            title=title,
            expected_count=expected_count,
            sample_rate=sample_rate,
            channels=channels,
            bits_per_sample=bits_per_sample,
            started_at=started_at,
        )
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "detail": str(exc)}), 400

    return jsonify({
        "created": created,
        "session": sess.to_api_dict(),
    }), (201 if created else 200)


@bp.get("/api/v1/sessions")
def api_list_sessions() -> Any:
    sessions = current_app_storage().list_sessions()
    return jsonify({"sessions": [s.to_api_dict() for s in sessions]})


@bp.get("/api/v1/sessions/<session_id>")
def api_get_session(session_id: str) -> Any:
    sess = current_app_storage().get_session(session_id)
    if sess is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"session": sess.to_api_dict()})


# --- API: chunk upload ----------------------------------------------
@bp.put("/api/v1/sessions/<session_id>/chunks/<int:chunk_index>")
def api_put_chunk(session_id: str, chunk_index: int) -> Any:
    store = current_app_storage()
    if store.get_session(session_id) is None:
        return jsonify({"error": "not_found"}), 404
    body = request.get_data(cache=False, as_text=False)
    if not body:
        return jsonify({"error": "empty_body"}), 400
    try:
        size = store.write_chunk(session_id, chunk_index, body)
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": "rejected", "detail": str(exc)}), 409
    return jsonify({
        "session_id": session_id,
        "chunk_index": chunk_index,
        "byte_size": size,
        "received_at": storage.now_iso(),
    })


# --- API: finish ----------------------------------------------------
@bp.post("/api/v1/sessions/<session_id>/finish")
def api_finish_session(session_id: str) -> Any:
    """Validate + stitch chunks; return fast.

    The stitch is synchronous and quick (<1 s even for hours of
    AAC — it's a binary concat), so the device gets a definitive
    answer and can safely delete its SD files. The AI pipeline
    (transcribe → classify → summarise → extract) then runs on
    the FIFO background worker and can take many minutes.

    Idempotent across restarts: if the session has already been
    stitched (any post-finish status) and ``full.aac`` is on
    disk, a re-sent finish — after a timeout, a server restart,
    or a missed response — returns ``200 already_ready``
    immediately without re-stitching. (A stranded pipeline is
    recovered by the boot sweep, not by device retries.)
    """
    store = current_app_storage()
    sess = store.get_session(session_id)
    if sess is None:
        return jsonify({"error": "not_found"}), 404
    if (
        sess.status in _RETRYABLE_STATUSES
        and store.open_full_audio(session_id) is not None
    ):
        fresh = store.get_session(session_id)
        return jsonify({
            "ok": True,
            "reason": "already_ready",
            "session": fresh.to_api_dict() if fresh else None,
        })
    result = store.finish_session(session_id)
    if not result.ok:
        if result.reason == "missing_chunks":
            present = sorted(store.list_received_chunks(session_id))
            return jsonify({
                "error": "missing_chunks",
                "missing": result.missing,
                "present": present,
            }), 409
        return jsonify({
            "error": result.reason,
            "detail": sess.status_detail,
        }), 500
    if current_app.config.get("LR_PIPELINE_BACKGROUND", True):
        _enqueue_pipeline(current_app._get_current_object(), session_id)
    else:
        # Deterministic mode (used by tests): run inline so the
        # response reflects the final status.
        try:
            processing.run_post_finish_pipeline(store, session_id)
        except Exception as exc:  # pragma: no cover
            print(f"[LR ] post-finish pipeline error: {exc!r}", flush=True)
    sess = store.get_session(session_id)
    return jsonify({
        "ok": True,
        "reason": result.reason,
        "session": sess.to_api_dict() if sess else None,
    })


# --- API: re-run transcription pipeline (manual retry) -------------
@bp.post("/api/v1/sessions/<session_id>/transcribe")
def api_transcribe_session(session_id: str) -> Any:
    """Re-run the post-finish pipeline for ``session_id``.

    Useful when the auto-pipeline failed (network blip, no
    API key at the time, a server restart mid-pipeline, etc.)
    and the user wants to retry without re-uploading the audio.

    Behaviour:

    * The session must be in a post-finish status: ``ready``,
      ``transcribing``, ``transcribed``, ``classifying``,
      ``summarising``, ``extracting``, ``done`` or ``failed``.
      Intermediate states stranded by a server restart
      mid-pipeline are reset to ``ready`` here — that is the
      recovery path for that situation.
    * The full pipeline (transcribe → classify → summarise →
      extract) runs again and overwrites any prior transcript
      artifact and kind.
    * Returns ``409`` if another pipeline is already running
      for this session in this process.

    Returns ``200`` with ``{"ok": true/false, "session": ...}``.
    Returns ``404`` if the session is unknown.
    """
    store = current_app_storage()
    sess = store.get_session(session_id)
    if sess is None:
        return jsonify({"error": "not_found"}), 404
    if sess.status not in _RETRYABLE_STATUSES:
        return jsonify({
            "ok": False,
            "reason": "not_ready",
            "status": sess.status,
            "detail": (
                "session is still receiving/stitching audio; "
                "retry is only possible after finish"
            ),
            "session": sess.to_api_dict(),
        }), 409
    # Prevent a background upload run and this retry from both
    # driving the same session concurrently.
    with _PIPELINE_LOCK:
        if session_id in _PIPELINES_IN_FLIGHT:
            return jsonify({
                "ok": False,
                "reason": "pipeline_running",
                "detail": (
                    "an AI pipeline is already running for this "
                    "session; reload the page in a few seconds"
                ),
                "session": sess.to_api_dict(),
            }), 409
        _PIPELINES_IN_FLIGHT.add(session_id)
    try:
        # Allow the retry to clobber an existing transcript /
        # kind, and recover statuses stranded by a server
        # restart mid-pipeline: move any post-finish status
        # back to 'ready' so the pipeline's transition guard
        # lets it run. All of these transitions are legal.
        if sess.status != storage.STATUS_READY:
            try:
                store.set_status(
                    session_id, storage.STATUS_READY,
                    status_detail=None,
                )
            except ValueError as exc:
                return jsonify({
                    "ok": False,
                    "reason": "illegal_transition",
                    "detail": str(exc),
                    "session": (
                        store.get_session(session_id).to_api_dict()
                    ),
                }), 409
        # Run the pipeline synchronously. Uses the env-var-driven
        # providers unless the test harness replaced them via
        # ``app.config['LR_LLM_PROVIDERS']``.
        result = processing.run_post_finish_pipeline(
            store, session_id,
            transcript_provider=current_app_transcript_provider(),
            classifier=current_app_text_provider(),
            llm_provider=current_app_text_provider(),
        )
    finally:
        with _PIPELINE_LOCK:
            _PIPELINES_IN_FLIGHT.discard(session_id)
    sess = store.get_session(session_id)
    # ``None`` means the session wasn't 'ready' (e.g. it's
    # still 'receiving'). Surface that as a 409 so the UI can
    # explain.
    if result is None:
        return jsonify({
            "ok": False,
            "reason": "not_ready",
            "status": sess.status if sess else None,
            "session": sess.to_api_dict() if sess else None,
        }), 409
    return jsonify({
        "ok": bool(result.get("ok")),
        "reason": result.get("reason"),
        "detail": result.get("detail"),
        "session": sess.to_api_dict() if sess else None,
    })


# --- API: audio serving ---------------------------------------------
@bp.get("/api/v1/sessions/<session_id>/audio")
def api_get_audio(session_id: str) -> Any:
    store = current_app_storage()
    sess = store.get_session(session_id)
    if sess is None:
        abort(404)
    full = store.open_full_audio(session_id)
    if full is None:
        return jsonify({
            "error": "not_ready",
            "detail": "session has no full.aac yet",
            "status": sess.status,
        }), 409
    return send_file(
        str(full),
        mimetype="audio/aac",
        conditional=True,
        as_attachment=False,
        download_name=f"{sess.title or sess.id}.aac",
    )


# --- API: recording-kind classification (manual override) ----------
@bp.post("/api/v1/sessions/<session_id>/kind")
def api_set_kind(session_id: str) -> Any:
    """Manually set or clear a session's recording kind.

    Body: ``{"kind": "lecture" | "voice_memo" | "meeting" | "other" | null}``

    Returns the updated session. A finished session that changes
    kind gets its summary rewritten for the new kind (see
    :func:`web.pipeline.set_kind_by_hand`).
    """
    store = current_app_storage()
    if store.get_session(session_id) is None:
        return jsonify({"error": "not_found"}), 404
    payload = request.get_json(silent=True) or {}
    if "kind" not in payload:
        return jsonify({
            "error": "invalid_payload",
            "detail": "field 'kind' is required (string or null)",
        }), 400
    kind = payload["kind"]
    if kind is not None and kind not in storage.Storage.RECORDING_KINDS:
        return jsonify({
            "error": "invalid_kind",
            "detail": (
                f"kind must be one of "
                f"{sorted(storage.Storage.RECORDING_KINDS)} or null"
            ),
        }), 400
    try:
        sess = set_kind_by_hand(
            current_app._get_current_object(), session_id, kind,
            confidence=payload.get("confidence"),
        )
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "detail": str(exc)}), 400
    return jsonify({"session": sess.to_api_dict()})


@bp.post("/api/v1/sessions/<session_id>/classify")
def api_classify_session(session_id: str) -> Any:
    """LLM classify a single session.

    Looks up the session's most recent ``transcript`` artifact
    and asks the configured text model (via OpenRouter when
    ``LR_TEXT_API_KEY`` is set, no-op otherwise) for a
    kind + confidence. On success the kind is persisted on
    the session.

    Returns one of:

    * ``200`` ``{"kind": "lecture", "confidence": 0.9,
      "reason": "classified"}`` on success.
    * ``200`` ``{"kind": null, "confidence": null,
      "reason": "llm_not_implemented"}`` if the session has
      no transcript yet (so the LLM can't classify) or the
      provider is the no-op.
    * ``404`` if the session is unknown.
    """
    if current_app_storage().get_session(session_id) is None:
        return jsonify({"error": "not_found"}), 404
    outcome = current_app_classification_service().classify_session(
        session_id,
        classifier=current_app_text_provider(),
    )
    if outcome is None:
        return jsonify({
            "kind": None,
            "confidence": None,
            "reason": services.classification.LLM_NOT_IMPLEMENTED,
        })
    kind, confidence = outcome
    sess = current_app_storage().set_kind(
        session_id, kind, confidence=confidence,
    )
    return jsonify({
        "kind": sess.kind,
        "confidence": sess.kind_confidence,
        "reason": "classified",
    })


@bp.post("/api/v1/classifications/bulk")
def api_bulk_classify() -> Any:
    """Bulk classify a list of session ids.

    Body: ``{"session_ids": [...]}``. Returns
    ``{"results": {session_id: {kind, confidence, reason} | null}}``.
    Unknown session ids are silently omitted. Each session
    is classified independently using the configured LLM
    provider; on success the kind is persisted via
    ``set_kind``.
    """
    payload = request.get_json(silent=True) or {}
    session_ids = payload.get("session_ids")
    if not isinstance(session_ids, list):
        return jsonify({
            "error": "invalid_payload",
            "detail": "'session_ids' must be a list",
        }), 400
    svc = current_app_classification_service()
    store = current_app_storage()
    classifier = current_app_text_provider()
    results: dict[str, Any] = {}
    for sid in session_ids:
        if not isinstance(sid, str) or not sid:
            continue
        if store.get_session(sid) is None:
            continue
        outcome = svc.classify_session(sid, classifier=classifier)
        if outcome is None:
            results[sid] = {
                "kind": None,
                "confidence": None,
                "reason": services.classification.LLM_NOT_IMPLEMENTED,
            }
        else:
            k, c = outcome
            sess = store.set_kind(sid, k, confidence=c)
            results[sid] = {
                "kind": sess.kind,
                "confidence": sess.kind_confidence,
                "reason": "classified",
            }
    return jsonify({"results": results})


# --- API: search (Phase A stub returns []) -------------------------
@bp.get("/api/v1/search")
def api_search() -> Any:
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"hits": []})
    hits = search.search(
        q,
        course_id=request.args.get("course_id") or None,
        kind=request.args.get("kind") or None,
    )
    return jsonify({
        "hits": [
            {
                "session_id": h.session_id,
                "artifact_id": h.artifact_id,
                "kind": h.kind,
                "snippet": h.snippet,
            }
            for h in hits
        ],
    })


@bp.post("/api/v1/sessions/<session_id>/course")
def api_assign_course(session_id: str) -> Any:
    """Assign or unassign a course on a session.

    Body: ``{"course_id": "<id>" | null}``.
    """
    store = current_app_storage()
    if store.get_session(session_id) is None:
        return jsonify({"error": "not_found"}), 404
    payload = request.get_json(silent=True) or {}
    if "course_id" not in payload:
        return jsonify({
            "error": "invalid_payload",
            "detail": "field 'course_id' is required (string or null)",
        }), 400
    course_id = payload["course_id"]
    try:
        sess = store.set_session_course(session_id, course_id)
    except ValueError as exc:
        return jsonify({
            "error": "invalid_payload", "detail": str(exc),
        }), 400
    return jsonify({"session": sess.to_api_dict()})


# --- API: web audio upload ------------------------------------------
@bp.post("/api/v1/uploads/audio")
def api_upload_audio() -> Any:
    """Accept one audio file from the browser, transcode to canonical
    AAC if needed, create a one-chunk session, and run the AI
    pipeline (transcribe → classify → summarise → extract).

    Multipart form fields:
      - file: the audio bytes (any format the browser or ffmpeg can read)
      - title: optional string; defaults to rec_YYYY-MM-DDTHHMM
    """
    cfg = current_app.config["LR_CONFIG"]
    store = current_app_storage()

    if "file" not in request.files:
        return jsonify({
            "error": "missing_file",
            "detail": "multipart field 'file' is required",
        }), 400

    upload = request.files["file"]
    raw = upload.read()
    if not raw:
        return jsonify({"error": "empty_file"}), 400
    if len(raw) > cfg.max_upload_bytes:
        return jsonify({
            "error": "file_too_large",
            "detail": (
                f"file is {len(raw)} bytes; max is {cfg.max_upload_bytes}"
            ),
        }), 413

    # Title: empty/whitespace -> default; explicit bad -> 400.
    title_input = request.form.get("title")
    if title_input is not None and title_input.strip() != "":
        title_clean = _normalise_title(title_input)
        if title_clean is None:
            return jsonify({
                "error": "invalid_title",
                "detail": (
                    "title must be 1..200 chars of printable text "
                    "(letters, digits, spaces, basic punctuation)"
                ),
            }), 400
        title = title_clean
    else:
        title = None  # substituted with default after we know mtime

    # Save the upload to the inbox, preserving the original extension
    # so ffmpeg can sniff it correctly.
    original = upload.filename or "upload"
    suffix = Path(original).suffix or ".bin"
    inbox_token = storage.new_session_id()
    inbox_path = cfg.inbox_dir / f"{inbox_token}{suffix}"
    try:
        inbox_path.write_bytes(raw)
    except OSError as exc:
        return jsonify({"error": "io_error", "detail": str(exc)}), 500

    started_at = _file_mtime_iso(inbox_path) or storage.now_iso()
    if title is None:
        title = _default_title_from_iso(started_at)

    work_dir: Optional[Path] = None
    try:
        fmt = transcode.sniff_format(inbox_path)
        if transcode.is_canonical_aac(fmt):
            aac_path = inbox_path
        else:
            if not transcode.ffmpeg_available(cfg.ffmpeg_path):
                return jsonify({
                    "error": "ffmpeg_missing",
                    "detail": (
                        f"this file is {fmt.kind} and needs to be "
                        f"transcoded, but ffmpeg was not found at "
                        f"{cfg.ffmpeg_path!r}. Install ffmpeg or set "
                        "LR_FFMPEG_PATH."
                    ),
                }), 503
            work_dir = cfg.inbox_dir / f"work-{inbox_token}"
            work_dir.mkdir(parents=True, exist_ok=True)
            aac_path = work_dir / "out.aac"
            transcode.transcode_to_aac(
                inbox_path, aac_path, ffmpeg_path=cfg.ffmpeg_path,
            )

        aac_bytes = aac_path.read_bytes()
        if transcode.sniff_format(aac_path).kind != "aac":
            return jsonify({
                "error": "transcode_failed",
                "detail": "output is not valid AAC",
            }), 500

        sess = store.ingest_single_audio(
            session_id=inbox_token,
            title=title,
            audio_bytes=aac_bytes,
            started_at=started_at,
        )
    except transcode.TranscodeError as exc:
        return jsonify({
            "error": exc.reason,
            "detail": exc.detail,
        }), 503
    except Exception as exc:
        return jsonify({
            "error": "ingest_failed", "detail": str(exc),
        }), 500
    finally:
        try:
            if inbox_path.exists():
                inbox_path.unlink()
        except OSError:
            pass
        if work_dir is not None and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

    # The AI pipeline runs on the FIFO background worker so the
    # upload returns immediately and the browser lands on the
    # session page (which polls the live status) instead of
    # babysitting a request that can take many minutes for long
    # lectures. Tests pin LR_PIPELINE_BACKGROUND=False and get
    # the deterministic inline run.
    transcript_provider = current_app_transcript_provider()
    text_provider = current_app_text_provider()
    run_in_background = current_app.config.get(
        "LR_PIPELINE_BACKGROUND", True,
    )
    if run_in_background:
        _enqueue_pipeline(current_app._get_current_object(), sess.id)
    else:
        # Deterministic mode (used by tests): run synchronously
        # so the response reflects the final status.
        try:
            processing.run_post_finish_pipeline(
                store, sess.id,
                transcript_provider=transcript_provider,
                classifier=text_provider,
                llm_provider=text_provider,
            )
        except Exception as exc:  # pragma: no cover
            print(f"[LR ] post-upload pipeline error: {exc!r}", flush=True)
    sess = store.get_session(sess.id) or sess

    return jsonify({
        "created": True,
        "session": sess.to_api_dict(),
        "redirect_url": f"/sessions/{sess.id}",
    }), 201
