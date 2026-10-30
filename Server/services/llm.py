"""LLM providers for transcription and text-model tasks, both via
OpenRouter.

The pipeline in :mod:`services.processing` calls these providers per
session:

1. A **transcription provider** — turns a WAV file into text.
   The concrete implementation is :class:`TranscriptionProvider`
   which uses the OpenRouter OpenAI-compatible transcription
   endpoint (``POST /api/v1/audio/transcriptions``). When the
   upstream model returns diarization data, the transcript is
   rendered with speaker annotations (``[MM:SS] Speaker 1: ...``).

2. A **text provider** — a chat-completions model used for
   classification (``lecture`` / ``voice_memo`` / ``meeting`` /
   ``other``), summarisation, and structured item extraction.
   The concrete implementation is :class:`TextProvider` which
   uses the OpenRouter chat completions endpoint.

Each provider takes an ``http_transport`` callable (or falls back
to the stdlib ``urllib.request``) so tests can inject a fake
transport without monkey-patching global state.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol


# --- Classifier result type -------------------------------------------------


@dataclass
class Classification:
    """One kind + confidence pair from the classifier."""
    kind: str
    confidence: float

    def to_tuple(self) -> tuple[str, float]:
        return (self.kind, self.confidence)


# --- HTTP transport protocol ------------------------------------------------
#
# The transport is a function that takes (url, data, headers) and
# returns a ``(status_code, body_bytes)`` tuple. Real impl wraps
# ``urllib.request.urlopen``; tests inject a fake.

HttpResponse = tuple[int, bytes]


class HttpTransport(Protocol):
    def __call__(
        self,
        url: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 60.0,
    ) -> HttpResponse: ...


def _urllib_transport(
    url: str,
    *,
    data: Optional[bytes] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 60.0,
) -> HttpResponse:
    """Stdlib HTTP transport using ``urllib.request``.

    We avoid a hard dependency on ``requests`` so this module can be
    imported in minimal test environments.
    """
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.getcode(), resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return (exc.code, body)


# --- Errors -----------------------------------------------------------------


class LLMError(RuntimeError):
    """Base class for all LLM provider errors."""


class LLMHTTPError(LLMError):
    """The upstream returned a non-2xx status. Includes the body."""

    def __init__(self, status: int, body: bytes, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class LLMConfigError(LLMError):
    """The provider is mis-configured (missing key, unknown model)."""


# --- Summary / extraction dataclasses (M9) ---------------------------------


@dataclass
class Deadline:
    text: str
    due: Optional[str] = None  # ISO-8601 date if the model could extract one


@dataclass
class Summary:
    text: str
    key_points: list[str] = field(default_factory=list)
    #: ``{"heading": ..., "text": ...}`` dicts, in order. Empty for
    #: short recordings.
    sections: list[dict] = field(default_factory=list)
    deadlines: list[Deadline] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)


# --- Abstract base ---------------------------------------------------------


class LLMProvider:
    """Abstract provider. ``name`` is the short identifier for logging."""
    name: str = "base"

    def transcribe(self, audio_path: Path) -> str:
        raise NotImplementedError

    def classify(self, transcript: str) -> Classification:
        """Pick a recording kind from a transcript. Returns
        :class:`Classification` with kind + confidence."""
        raise NotImplementedError

    def summarize(
        self, transcript: str, kind: Optional[str] = None,
    ) -> Summary:
        raise NotImplementedError

    def extract(self, transcript: str, kind: str) -> list[dict]:
        raise NotImplementedError


class _NoOpProvider(LLMProvider):
    """Returned when no API keys are configured.

    Returns **stub data** rather than raising so the pipeline
    can complete end-to-end without an LLM. The session ends
    at ``status='done'`` with an empty transcript and a
    low-confidence ``other`` kind; the user can later re-run
    the pipeline after setting keys via the ``/transcribe``
    endpoint. This is intentionally different from a real
    LLM failure: a missing key is a configuration issue, not
    a runtime error, so it shouldn't mark the session
    ``failed``.

    The base ``LLMProvider`` surface (summarize, extract) still
    raises :class:`LLMConfigError` because those endpoints
    don't have a sensible "no-op" shape.
    """
    name = "noop"

    def transcribe(self, audio_path: Path) -> str:
        # Return a short placeholder so the pipeline's
        # non-empty check passes. The UI shows the standard
        # "no transcript" placeholder when content is short;
        # in any case the user can re-run with real keys.
        return "[no transcript - no LLM provider configured]"

    def classify(self, transcript: str) -> Classification:
        # Low-confidence "other" so the session is technically
        # classified but obviously not from an LLM.
        return Classification(kind="other", confidence=0.0)

    def summarize(
        self, transcript: str, kind: Optional[str] = None,
    ) -> Summary:
        # Empty Summary so the pipeline persists an empty
        # summary artifact; the UI's "no summary yet" placeholder
        # shows. Matches the no-op semantic: no LLM = no work.
        return Summary(
            text="[no summary - no LLM provider configured]",
            key_points=[],
        )

    def extract(self, transcript: str, kind: str) -> list[dict]:
        # Empty list so the pipeline persists an empty
        # extraction artifact. The UI can render "no
        # structured items" without special-casing.
        return []


# --- Transcription (OpenRouter) ---------------------------------------------
#
# OpenRouter's transcription API is OpenAI-compatible. The
# transcription endpoint accepts a multipart/form-data POST with the
# audio file and returns the transcript. We use the stdlib
# ``email``/``urllib`` machinery to build the multipart body (no
# third-party dep).
#
# We request ``response_format=verbose_json`` so the response can
# carry timestamped segments with speaker (diarization) data. When
# the upstream model returns speaker labels, the transcript is
# rendered as annotated dialogue lines; otherwise we fall back to
# the plain text.

OPENROUTER_TRANSCRIPTIONS_URL = (
    "https://openrouter.ai/api/v1/audio/transcriptions"
)
TRANSCRIPTION_MODEL = "microsoft/mai-transcribe-2"
#: Env override for the transcription model (an OpenRouter slug).
TRANSCRIPTION_MODEL_ENV = "LR_TRANSCRIPTION_MODEL"

#: Env flag: render speaker annotations in the transcript when the
#: transcription model returns diarization data. Set to
#: ``0``/``false``/``no``/``off`` to always get plain text.
TRANSCRIBE_SPEAKERS_ENV = "LR_TRANSCRIBE_SPEAKERS"

#: OpenRouter caps inline transcription uploads at 25 MB (multipart)
#: and JSON request bodies at 50 MB, and upstream providers time out
#: after ~60 s of processing per request. Canonical storage is AAC
#: (~8 KB/s), so a 1500 s (25 min) piece is ~12 MB — comfortably
#: under the body caps. Longer stitched recordings are segmented to
#: this length (stream copy, no re-encode) before transcription.
TRANSCRIBE_CHUNK_SECONDS_ENV = "LR_TRANSCRIBE_CHUNK_SECONDS"
DEFAULT_TRANSCRIBE_CHUNK_SECONDS = 1500

#: Provider-specific options passed as ``provider.options`` on the
#: transcription request. The default enables Azure diarization so
#: ``microsoft/mai-transcribe-2`` (hosted by Azure) returns segment
#: speaker labels — without this option the model emits no
#: ``speaker`` fields at all. Override with the
#: ``LR_TRANSCRIBE_PROVIDER_OPTIONS`` env var (a JSON object) when
#: using a model served by a different provider; set it to ``{}``
#: to send no provider options.
TRANSCRIBE_PROVIDER_OPTIONS_ENV = "LR_TRANSCRIBE_PROVIDER_OPTIONS"
DEFAULT_TRANSCRIBE_PROVIDER_OPTIONS: dict = {
    "azure": {"diarization": {"enabled": True}},
}

#: OpenRouter takes JSON bodies up to 50 MB, but only OpenAI and Groq
#: models accept inline audio over 25 MB; other providers (Azure,
#: which serves the default model) reject it. We refuse to send
#: anything over this and raise a clear, actionable error instead
#: (the piece is then halved). A 25-minute MP3 piece is ~8 MB.
MAX_TRANSCRIBE_BODY_BYTES = 24 * 1024 * 1024

#: Valid recording kinds the classifier can return.
#: Mirrors ``storage.RECORDING_KINDS`` and
#: ``services.classification.VALID_KINDS``; kept here to avoid an
#: import cycle.
VALID_KINDS: frozenset[str] = frozenset(
    {"lecture", "voice_memo", "meeting", "other"}
)


def _build_multipart_body(
    file_field: str,
    filename: str,
    content: bytes,
    content_type: str,
    fields: dict[str, str],
    boundary: str,
) -> bytes:
    """Build a multipart/form-data body for an HTTP POST.

    Stdlib-only; avoids a dependency on ``requests``. The caller
    picks a stable ``boundary`` so the body is reproducible in
    tests.
    """
    lines: list[bytes] = []
    for k, v in fields.items():
        lines.append(f"--{boundary}".encode("ascii"))
        lines.append(
            f'Content-Disposition: form-data; name="{k}"'.encode("ascii")
        )
        lines.append(b"")
        lines.append(v.encode("utf-8"))
    # File part.
    lines.append(f"--{boundary}".encode("ascii"))
    lines.append(
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"'.encode("ascii")
    )
    lines.append(f"Content-Type: {content_type}".encode("ascii"))
    lines.append(b"")
    lines.append(content)
    lines.append(f"--{boundary}--".encode("ascii"))
    lines.append(b"")
    return b"\r\n".join(lines)


def _read_env_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to ``default``."""
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _format_timestamp(seconds: object) -> Optional[str]:
    """Format a segment start time (float seconds) as ``[MM:SS]``
    (or ``[H:MM:SS]`` past one hour). Returns ``None`` when the
    value is missing or not a number.
    """
    try:
        total = float(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if total < 0:
        total = 0.0
    total = int(total)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _segments_to_transcript(
    segments: list,
    *,
    annotate_speakers: bool,
) -> str:
    """Render verbose_json ``segments`` into transcript text.

    When ``annotate_speakers`` is true and at least one segment
    carries speaker data (``speaker`` index or ``speaker_label``
    string), lines are rendered as ``[MM:SS] Speaker N: text``.
    Otherwise plain per-segment text is joined with newlines.
    Returns ``""`` when no segment has usable text.
    """
    has_speakers = any(
        isinstance(s, dict)
        and (s.get("speaker") is not None or s.get("speaker_label"))
        for s in segments
    )
    lines: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        if annotate_speakers and has_speakers:
            label = seg.get("speaker_label")
            if not label and seg.get("speaker") is not None:
                label = f"Speaker {int(seg['speaker']) + 1}"
            label = str(label)
            ts = _format_timestamp(seg.get("start"))
            if ts:
                lines.append(f"[{ts}] {label}: {text}")
            else:
                lines.append(f"{label}: {text}")
        else:
            lines.append(text)
    return "\n".join(lines).strip()


def _speakers_enabled() -> bool:
    """Resolve the speaker-annotation flag from the environment.

    Defaults to on; set ``LR_TRANSCRIBE_SPEAKERS`` to
    ``0``/``false``/``no``/``off`` (case-insensitive) to disable.
    """
    raw = os.environ.get(TRANSCRIBE_SPEAKERS_ENV, "")
    return raw.strip().lower() not in ("0", "false", "no", "off")


class TranscriptionProvider(LLMProvider):
    """Real audio transcription via OpenRouter.

    Posts audio as base64 JSON (``input_audio``) with
    ``response_format=verbose_json`` and Azure diarization enabled
    by default (see ``DEFAULT_TRANSCRIBE_PROVIDER_OPTIONS`` — the
    JSON request shape is required because provider options are
    not supported on the multipart path), so supported models
    return segment-level speaker labels rendered as
    ``[MM:SS] Speaker N: text`` lines.

    Storage is canonical AAC (the device encodes before upload and
    web uploads are transcoded at ingest), so requests stay small
    (~12 MB per 25-minute piece). A stitched recording longer than
    ``LR_TRANSCRIBE_CHUNK_SECONDS`` (default 1500 s) is segmented
    with ffmpeg stream-copy (no re-encode, no quality loss) and the
    pieces are transcribed with cumulative offsets so the merged
    transcript has continuous times. If a piece hits a transient
    upstream failure (the ~60 s processing timeout, a 5xx, or an
    oversized-body rejection), it is automatically cut in half and
    retried. Caveat: speaker cluster indices are assigned per
    request, so "Speaker 1" in one piece is not guaranteed to be
    the same person in the next piece.

    Pass ``http_transport`` to inject a fake (used in tests); the
    default is the stdlib ``urllib.request`` wrapper.
    """

    # ``name`` is stored on artifacts as the generating model, so the
    # UI can show "generated by microsoft/mai-transcribe-2". Set per
    # instance to the model actually used.
    name = TRANSCRIPTION_MODEL

    def __init__(
        self,
        api_key: str,
        *,
        model: Optional[str] = None,
        annotate_speakers: Optional[bool] = None,
        chunk_max_seconds: Optional[float] = None,
        provider_options: Optional[dict] = None,
        ffmpeg_path: Optional[str] = None,
        http_transport: Optional[HttpTransport] = None,
    ) -> None:
        if not api_key:
            raise LLMConfigError(
                "TranscriptionProvider requires LR_TRANSCRIPTION_API_KEY"
            )
        self._api_key = api_key
        self._model = (
            model or os.environ.get(TRANSCRIPTION_MODEL_ENV, "").strip()
            or TRANSCRIPTION_MODEL
        )
        self.name = self._model
        self._annotate_speakers = (
            _speakers_enabled()
            if annotate_speakers is None
            else bool(annotate_speakers)
        )
        self._chunk_max_seconds = (
            float(_read_env_int(
                TRANSCRIBE_CHUNK_SECONDS_ENV,
                DEFAULT_TRANSCRIBE_CHUNK_SECONDS,
            ))
            if chunk_max_seconds is None
            else float(chunk_max_seconds)
        )
        if provider_options is None:
            raw = os.environ.get(TRANSCRIBE_PROVIDER_OPTIONS_ENV, "").strip()
            if raw:
                try:
                    provider_options = json.loads(raw)
                except ValueError:
                    # Unparseable env JSON: fall back to the default
                    # rather than silently dropping diarization.
                    provider_options = dict(
                        DEFAULT_TRANSCRIBE_PROVIDER_OPTIONS,
                    )
            else:
                provider_options = dict(DEFAULT_TRANSCRIBE_PROVIDER_OPTIONS)
        self._provider_options = provider_options
        # ffmpeg path for segmentation/cutting; same env var the app
        # config uses so one setting covers both.
        self._ffmpeg_path = (
            ffmpeg_path or os.environ.get("LR_FFMPEG_PATH", "ffmpeg")
        )
        self._ffmpeg_ok: Optional[bool] = None
        self._http = http_transport or _urllib_transport

    def transcribe(self, audio_path: Path) -> str:
        if not isinstance(audio_path, Path):
            audio_path = Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(str(audio_path))
        # Segment long stitched recordings into ~25-minute pieces
        # (stream copy); short files come back as a single piece.
        tmp_dir = Path(tempfile.mkdtemp(prefix="lr-stt-"))
        try:
            pieces = self._segment_audio(audio_path, tmp_dir)
            total = len(pieces)
            if total > 1:
                print(
                    f"[LR ] transcribing: {total} pieces to send",
                    flush=True,
                )
            seg_parts: list[dict] = []
            text_parts: list[str] = []
            all_had_segments = True
            for piece_no, (piece_path, offset) in enumerate(
                pieces, start=1,
            ):
                if total > 1:
                    # Console progress: a long multi-piece run is
                    # completely silent otherwise (the dev server
                    # only logs the HTTP request once it completes),
                    # which makes it look hung.
                    print(
                        f"[LR ] transcribing: piece {piece_no}/"
                        f"{total} (starts at {int(offset)} s)",
                        flush=True,
                    )
                had, segs, texts = self._transcribe_piece(
                    piece_path, offset, tmp_dir,
                )
                all_had_segments = all_had_segments and had
                seg_parts.extend(segs)
                text_parts.extend(texts)
            if total > 1:
                print(
                    f"[LR ] transcribing: merged {total} piece(s)",
                    flush=True,
                )
            if all_had_segments and seg_parts:
                rendered = _segments_to_transcript(
                    seg_parts, annotate_speakers=self._annotate_speakers,
                )
                if rendered:
                    return rendered
            return "\n".join(text_parts).strip()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _post_transcription(
        self, audio_path: Path, audio_format: Optional[str] = None,
    ) -> bytes:
        """POST one audio piece and return the raw response body.

        Uses OpenRouter's JSON request shape (base64
        ``input_audio``) because provider-specific options such as
        Azure diarization are only supported on the JSON path.
        The format label defaults to the sniffed kind of the file
        (canonical storage is AAC; halving pieces inherit whatever
        the source was).
        """
        if audio_format is None:
            from services import transcode as _tx
            fmt = _tx.sniff_format(audio_path).kind
            audio_format = fmt if fmt != "unknown" else "aac"
        content = audio_path.read_bytes()
        payload: dict = {
            "model": self._model,
            "input_audio": {
                "data": base64.b64encode(content).decode("ascii"),
                "format": audio_format,
            },
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment"],
        }
        if self._provider_options:
            payload["provider"] = {"options": self._provider_options}
        body = json.dumps(payload).encode("utf-8")
        # OpenRouter rejects oversized bodies with a bare 413. Fail
        # fast here with an actionable message instead.
        if len(body) > MAX_TRANSCRIBE_BODY_BYTES:
            raise LLMError(
                f"transcription request body is {len(body)} bytes, "
                f"over the {MAX_TRANSCRIBE_BODY_BYTES} byte safety "
                "cap; lower LR_TRANSCRIBE_CHUNK_SECONDS (current "
                f"piece limit: {self._chunk_max_seconds} s)"
            )
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        status, raw = self._http(
            OPENROUTER_TRANSCRIPTIONS_URL,
            data=body,
            headers=headers, timeout=120.0,
        )
        if status < 200 or status >= 300:
            raise LLMHTTPError(
                status, raw,
                f"OpenRouter transcription failed: HTTP {status}: "
                f"{raw[:200]!r}",
            )
        return raw

    def _extract_chunk(
        self, raw: bytes, offset: float,
    ) -> tuple[Optional[list[dict]], Optional[str]]:
        """Parse one transcription response.

        Returns ``(segments, text)``: ``segments`` is the list of
        verbose_json segment dicts with ``start``/``end`` shifted by
        ``offset`` (``None`` when the provider returned no
        segments), and ``text`` is the plain transcript (``None``
        when unavailable). Tolerates plain-text bodies.
        """
        decoded = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(decoded)
        except ValueError:
            return (None, decoded)
        if not isinstance(payload, dict):
            return (None, decoded)
        segments: Optional[list[dict]] = None
        raw_segments = payload.get("segments")
        if isinstance(raw_segments, list) and raw_segments:
            segments = []
            for seg in raw_segments:
                if not isinstance(seg, dict):
                    continue
                shifted = dict(seg)
                for field in ("start", "end"):
                    value = shifted.get(field)
                    if isinstance(value, (int, float)):
                        shifted[field] = float(value) + offset
                segments.append(shifted)
        text = payload.get("text")
        return (segments, text if isinstance(text, str) else None)

    def _probe_ffmpeg(self) -> bool:
        """One-time check that the ffmpeg binary runs (cached)."""
        if self._ffmpeg_ok is None:
            try:
                result = subprocess.run(
                    [self._ffmpeg_path, "-version"],
                    capture_output=True,
                )
                self._ffmpeg_ok = result.returncode == 0
            except OSError:
                self._ffmpeg_ok = False
        return self._ffmpeg_ok

    def _probe_duration(self, path: Path) -> Optional[float]:
        """Accurate duration of an audio file, or ``None``.

        ADTS AAC has no container duration; ffprobe *estimates* it
        from the bitrate, which drifts badly for quiet audio. The
        decode-based probe is exact, so prefer it and fall back to
        ffprobe only when ffmpeg is missing (where nothing else
        works anyway).
        """
        from services import transcode as _tx
        d = _tx.decode_duration(path, ffmpeg_path=self._ffmpeg_path)
        if d is not None:
            return d
        return _tx.probe_duration(path, ffmpeg_path=self._ffmpeg_path)

    def _segment_audio(
        self, audio_path: Path, work_dir: Path,
    ) -> list[tuple[Path, float]]:
        """Split a long recording into ≤``chunk_max_seconds`` pieces.

        Stream-copy segmentation (``-f segment -c copy``): no
        re-encode, no quality loss, fast. Returns
        ``[(piece_path, offset_seconds), ...]`` with offsets taken
        from the segment muxer's exact start times (via its
        ``-segment_list`` CSV; ffprobe estimates for ADTS drift,
        especially for quiet audio). Files that are short enough —
        or systems without ffprobe/ffmpeg — come back whole as
        ``[(audio_path, 0.0)]`` (the per-request body guard still
        applies).
        """
        duration = self._probe_duration(audio_path)
        if duration is None or duration <= self._chunk_max_seconds:
            return [(audio_path, 0.0)]
        if not self._probe_ffmpeg():
            # Can't segment without ffmpeg: send the whole file and
            # let the body guard produce a clear error if too big.
            return [(audio_path, 0.0)]
        list_path = Path(work_dir) / "pieces.csv"
        cmd = [
            self._ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(audio_path),
            "-f", "segment",
            "-segment_time", str(int(self._chunk_max_seconds)),
            "-c", "copy",
            "-segment_list", str(list_path),
            "-segment_list_type", "csv",
            str(Path(work_dir) / "piece_%04d.aac"),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True)
        except OSError as exc:
            raise LLMError(f"ffmpeg segmentation failed: {exc}") from exc
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode(
                "utf-8", errors="replace",
            ).strip().splitlines()
            raise LLMError(
                "ffmpeg segmentation failed: "
                + (stderr[-1] if stderr else f"exit {result.returncode}")
            )
        # Exact piece start times from the segment list
        # (filename,start_time,end_time rows). Fall back to ffprobe
        # estimates if the list is missing or unparseable.
        starts: Optional[list[float]] = None
        try:
            rows = list_path.read_text().splitlines()
            starts = [float(r.split(",")[1]) for r in rows if r.strip()]
        except (OSError, IndexError, ValueError):
            starts = None
        pieces: list[tuple[Path, float]] = []
        files = sorted(Path(work_dir).glob("piece_*.aac"))
        if not files:
            return [(audio_path, 0.0)]
        if starts is not None and len(starts) == len(files):
            return list(zip(files, starts))
        offset = 0.0
        for p in files:
            pieces.append((p, offset))
            d = self._probe_duration(p)
            offset += d if d is not None else self._chunk_max_seconds
        return pieces

    def _cut_piece(
        self, src: Path, start: float, duration: float,
        out_dir: Path, depth: int, idx: int,
    ) -> Path:
        """Stream-copy ``[start, start+duration)`` of ``src`` to a
        new audio file for the halving retry.

        The output keeps the source's extension: with ``-c copy``
        the muxer must match the stream codec, and halving pieces
        inherit whatever format the recording was (canonical AAC,
        or WAV for a legacy recording).
        """
        suffix = src.suffix if src.suffix else ".aac"
        out = Path(out_dir) / f"cut_{depth}_{idx}_{int(start * 1000)}{suffix}"
        cmd = [
            self._ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", str(src),
            "-t", f"{duration:.3f}",
            "-c", "copy",
            str(out),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True)
        except OSError as exc:
            raise LLMError(f"ffmpeg cut failed: {exc}") from exc
        if result.returncode != 0 or not out.exists():
            raise LLMError(
                f"ffmpeg cut failed: exit {result.returncode}"
            )
        return out

    def _stt_audio(
        self, piece_path: Path, mp3: Path,
    ) -> tuple[Path, Optional[str]]:
        """The file and format label to send for one piece.

        Pieces stay AAC (stream-copied from storage) for segmenting
        and halving; only the upload is MP3, written to ``mp3`` in
        the work dir, because the Azure-hosted model rejects AAC
        (see ``transcode.transcode_for_stt``). Without ffmpeg, or
        for input ffmpeg cannot decode, the piece is sent as it is,
        labelled with its sniffed format.
        """
        if not self._probe_ffmpeg():
            return piece_path, None
        from services import transcode as _tx
        try:
            _tx.transcode_for_stt(
                piece_path, mp3, ffmpeg_path=self._ffmpeg_path,
            )
        except _tx.TranscodeError:
            return piece_path, None
        return mp3, "mp3"

    @staticmethod
    def _is_transient_failure(exc: Exception) -> bool:
        """Whether halving the piece could plausibly help: upstream
        timeouts / 5xx / oversized-body rejections and network-level
        errors. Client errors (401, 400) are not retryable."""
        if isinstance(exc, LLMHTTPError):
            return exc.status in (408, 413, 500, 502, 503, 504)
        if isinstance(exc, LLMError):
            # Our own oversized-body guard raises a plain LLMError.
            return True
        # urllib.error.URLError, socket timeouts, and other
        # network-level OSErrors.
        return isinstance(exc, (urllib.error.URLError, OSError))

    def _transcribe_piece(
        self,
        piece_path: Path,
        offset: float,
        work_dir: Path,
        depth: int = 0,
    ) -> tuple[bool, list[dict], list[str]]:
        """Transcribe one piece file.

        Returns ``(had_segments, segments, texts)``. On a transient
        upstream failure (timeout, 5xx, oversized body), the piece
        is cut in half and retried recursively — large pieces that
        would otherwise fail (e.g. against the upstream ~60 s
        processing timeout) degrade to smaller pieces instead of
        failing the whole recording.
        """
        try:
            raw = self._post_transcription(*self._stt_audio(
                piece_path,
                Path(work_dir) / f"stt-{depth}-{int(offset * 1000)}.mp3",
            ))
        except (LLMError, urllib.error.URLError, OSError) as exc:
            duration = self._probe_duration(piece_path)
            if (
                depth < 3
                and duration is not None
                and duration > 90
                and self._probe_ffmpeg()
                and self._is_transient_failure(exc)
            ):
                print(
                    f"[LR ] transcribing: piece failed ({exc}); "
                    "cutting it in half and retrying",
                    flush=True,
                )
                # Sub-pieces go into their own directory so retry
                # generations never overwrite each other's files.
                sub_dir = Path(work_dir) / (
                    f"retry-{depth}-{int(offset * 1000)}"
                )
                sub_dir.mkdir(parents=True, exist_ok=True)
                half = duration / 2.0
                had_all = True
                segs: list[dict] = []
                texts: list[str] = []
                for idx, (start, dur) in enumerate(
                    ((0.0, half), (half, duration - half)), start=1,
                ):
                    sub_path = self._cut_piece(
                        piece_path, start, dur, sub_dir, depth, idx,
                    )
                    h, s, t = self._transcribe_piece(
                        sub_path, offset + start, work_dir,
                        depth=depth + 1,
                    )
                    had_all = had_all and h
                    segs.extend(s)
                    texts.extend(t)
                return (had_all, segs, texts)
            raise
        segments, text = self._extract_chunk(raw, offset)
        texts = (
            [text] if isinstance(text, str) and text.strip() else []
        )
        return (segments is not None, segments or [], texts)

    # The transcription model only transcribes; raise for clarity.
    def classify(self, transcript: str) -> Classification:
        raise LLMConfigError(
            "TranscriptionProvider only transcribes; use "
            "TextProvider for classification"
        )

    def summarize(
        self, transcript: str, kind: Optional[str] = None,
    ) -> Summary:
        raise LLMConfigError(
            "TranscriptionProvider only transcribes; use "
            "TextProvider for summarisation"
        )

    def extract(self, transcript: str, kind: str) -> list[dict]:
        raise LLMConfigError(
            "TranscriptionProvider only transcribes; use "
            "TextProvider for extraction"
        )


# --- Text model (OpenRouter chat completions) -------------------------------
#
# The text model handles classification, summarisation, and
# structured extraction through OpenRouter's OpenAI-compatible
# chat completions endpoint. We POST a small system prompt that
# constrains the response to a JSON object with ``kind`` and
# ``confidence`` fields, then parse the assistant message.

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
#: MiniMax M3 ($0.30/M in, $1.20/M out, 1M context). The previous
#: default, nemotron-3-super ":free", was withdrawn (OpenRouter 404
#: "unavailable for free"); free tiers can go at any time.
TEXT_MODEL = "minimax/minimax-m3"
#: Env override for the text model (an OpenRouter slug).
TEXT_MODEL_ENV = "LR_TEXT_MODEL"
#: Env override for the text API's OpenAI-compatible base URL, e.g.
#: ``https://api.minimax.io/v1`` for a MiniMax subscription key.
TEXT_BASE_URL_ENV = "LR_TEXT_BASE_URL"

# Reasoning models served directly (not through OpenRouter) may put
# their thinking inside the message as <think>...</think>.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# System prompt: keeps the model on task. We give the model the
# four valid kinds and ask for a JSON object back. The confidence
# is the model's own 0..1 estimate.
_CLASSIFY_SYSTEM_PROMPT = (
    "You classify audio transcripts into one of four kinds. "
    "Reply with ONLY a JSON object, no prose, no markdown fences. "
    'The object must have exactly two keys: "kind" and "confidence".\n\n'
    '"kind" must be one of: "lecture" (a class or seminar — '
    "long, structured, audience-facing), "
    '"voice_memo" (a personal note, sometimes one-sided, often short), '
    '"meeting" (multiple participants discussing a shared topic), '
    '"other" (none of the above).\n\n'
    '"confidence" is your own 0..1 estimate; use 0.0 when you are '
    "guessing and 1.0 when you are certain."
)





# --- Map-reduce for long transcripts (M9.1) -----------------------------
#
# The system prompts above (summarise/extract) assume the entire
# transcript fits in one prompt. For long recordings (a 30+ minute
# lecture transcript is often 30-60k characters) that means the
# model only sees the first slice and ignores the rest.
#
# The fix is map-reduce: split the transcript into overlapping chunks,
# run the per-chunk LLM call on each, then either dedupe (extraction)
# or synthesise (summarise) the partials into a final result.
#
# All of this lives in helpers below; the public ``summarize()`` and
# ``extract()`` methods stay simple wrappers.

# Approximate per-chunk budget. The system prompt + response
# together cost ~1-2k tokens, so leave headroom in a 32k-token
# model window. 12_000 chars ~ 3k tokens is comfortable.
_CHUNK_SIZE = 12_000
# Overlap between consecutive chunks. Lets the model see context
# that straddles a boundary. ~1k chars is enough for ~2 sentences
# of overlap.
_CHUNK_OVERLAP = 1_000


def _chunk_text(
    text: str, *, chunk_size: int = _CHUNK_SIZE,
    overlap: int = _CHUNK_OVERLAP,
) -> list[str]:
    """Split ``text`` into overlapping chunks of ``chunk_size`` chars.

    Returns at least one chunk, even if ``text`` is empty (the
    chunk is then ``""``). Each chunk (except the first) starts
    ``overlap`` characters before where the previous one ended,
    so context that straddles a boundary is still visible to the
    model.

    Edge cases:
    - empty / whitespace-only text -> [""]
    - text shorter than ``chunk_size`` -> [text]
    - text exactly ``chunk_size`` -> [text]
    - text longer than ``chunk_size`` -> N overlapping chunks
    """
    if not text:
        return [""]
    if len(text) <= chunk_size:
        return [text]
    if overlap >= chunk_size:
        raise ValueError(
            f"chunk overlap ({overlap}) must be smaller than "
            f"chunk_size ({chunk_size})"
        )
    chunks: list[str] = []
    start = 0
    stride = chunk_size - overlap
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += stride
    return chunks


# Map step for long recordings: notes on one part of the transcript,
# with a heading. The synthesis step merges them into sections.
def _section_notes_prompt(kind: Optional[str]) -> str:
    return (
        "You write notes on ONE part of a longer audio transcript. "
        "Reply with ONLY a JSON object (no prose, no markdown fences) "
        "with exactly three keys: 'heading', 'text' and 'key_points'.\n\n"
        "- 'heading' is a short title (under 8 words) for what this "
        "part covers.\n"
        "- 'text' is notes on THIS PART ONLY, about 80-150 words, "
        "keeping the specifics: names, numbers, definitions, examples, "
        "decisions. Write less if the part has little content.\n"
        "- 'key_points' is a list of 0-5 short bullet points (each "
        "under 20 words) FROM THIS PART.\n\n"
        + _KIND_GUIDE.get(kind or "other", _KIND_GUIDE["other"])
        + "\n\nIf the part is empty, repetitive, or has no useful "
        "content, return {\"heading\": \"\", \"text\": \"\", "
        "\"key_points\": []}."
    )


# Synthesis step: merges the notes on each part into one summary
# without shortening them back down to a few sentences.
def _synthesis_prompt(kind: Optional[str], transcript_chars: int) -> str:
    return (
        "You are given notes on consecutive parts of one audio "
        "recording, in order. Combine them into ONE summary of the "
        "whole recording.\n\n"
        + _summary_shape(kind, transcript_chars)
        + "\n\nMerge neighbouring parts on the same subject into one "
        "section, and split a part that covers two subjects. Keep the "
        "specifics from the notes; do not reduce them to one-line "
        "mentions. Do not refer to 'parts' or 'notes'.\n\n"
        "If the notes are empty, return "
        "{\"text\": \"no content\", \"sections\": [], "
        "\"key_points\": []}."
    )


# Per-section system prompt for extraction. Same shape as the
# single-shot one but explicit that the model is looking at a
# fragment.
_EXTRACT_SECTION_PROMPT = (
    "You extract structured items from a single section of a longer "
    "transcript. Reply with ONLY a JSON object (no prose, no markdown "
    "fences) with one key: 'items'.\n\n"
    "'items' is a list of objects. Each object has at minimum two keys: "
    "'type' (short snake_case identifier like 'deadline', 'action_item', "
    "'topic', 'key_term', 'study_question', or 'fact') and 'text' "
    "(the item's content, plain English).\n\n"
    "Type guidance by recording kind:\n"
    "- 'meeting': focus on 'action_item' and 'deadline'.\n"
    "- 'lecture': focus on 'topic', 'key_term', 'study_question', 'fact'.\n"
    "- 'voice_memo': focus on 'fact' and 'topic'.\n"
    "- 'other': include a mix of 'topic' and 'fact'.\n\n"
    "Aim for 3-10 items. Only include items that are clearly supported "
    "by THIS SECTION; do not invent. If the section has no relevant "
    "items, return {\"items\": []}."
)


def _summarise_one_chunk(
    provider: "TextProvider",
    chunk: str,
    *,
    section_index: int,
    total_sections: int,
    kind: Optional[str] = None,
) -> Summary:
    """Run the per-chunk notes LLM call. Returns an empty
    :class:`Summary` on a parse error so the map step degrades
    gracefully (the synthesis step will just have one fewer input).
    The part's heading is returned as the only entry of ``sections``.
    """
    user_prompt = (
        f"This is part {section_index + 1} of {total_sections} of "
        f"a longer transcript. Write notes on it:\n\n{chunk}"
    )
    try:
        message = provider._chat(
            _section_notes_prompt(kind), user_prompt,
            timeout=_SUMMARY_TIMEOUT, max_chars=_CHUNK_SIZE,
        )
        raw = message.encode("utf-8")
        heading = _load_json_object(raw, "summary").get("heading")
        out = _parse_summarise_response(raw)
    except LLMError:
        return Summary(text="", key_points=[])
    out.sections = (
        [{"heading": heading.strip(), "text": out.text}]
        if isinstance(heading, str) and heading.strip() and out.text
        else []
    )
    return out


def _synthesise_summaries(
    provider: "TextProvider",
    partials: list[Summary],
    *,
    kind: Optional[str] = None,
    transcript_chars: int = 0,
) -> Summary:
    """Run the synthesis LLM call that merges the notes on each
    part into the final summary.
    """
    if not partials:
        return Summary(text="", key_points=[])
    formatted = "\n\n".join(
        f"[Part {i + 1}"
        + (f": {p.sections[0]['heading']}" if p.sections else "")
        + f"]\n{p.text}\nKey points: "
        + ("; ".join(p.key_points) if p.key_points else "(none)")
        for i, p in enumerate(partials)
    )
    user_prompt = (
        f"Below are notes on {len(partials)} consecutive parts of "
        f"one recording. Combine them into one summary:\n\n"
        f"{formatted}"
    )
    message = provider._chat(
        _synthesis_prompt(kind, transcript_chars), user_prompt,
        timeout=_SUMMARY_TIMEOUT, max_chars=_SYNTHESIS_MAX_CHARS,
    )
    return _parse_summarise_response(message.encode("utf-8"))


def _extract_one_chunk(
    provider: "TextProvider",
    chunk: str,
    *,
    kind: str,
    section_index: int,
    total_sections: int,
) -> list[dict]:
    """Run the per-chunk extract LLM call. Returns an empty list
    on a parse error so the dedupe step is robust to a single
    chunk failing.
    """
    user_prompt = (
        f"Recording kind: {kind}\n\n"
        f"This is section {section_index + 1} of {total_sections} of a "
        f"longer transcript. Extract structured items from this section:\n\n"
        f"{chunk}"
    )
    try:
        message = provider._chat(
            _EXTRACT_SECTION_PROMPT, user_prompt,
            max_chars=_CHUNK_SIZE,
        )
        return _parse_extract_response(message.encode("utf-8"))
    except LLMError:
        return []


def _normalise_item_text(text: str) -> str:
    """Lowercase, strip whitespace, collapse runs of whitespace.
    Used to fingerprint items for dedup so the same item mentioned
    in two chunks with slightly different surface forms still
    matches.
    """
    return " ".join(text.lower().split())


def _dedupe_items(items: list[dict]) -> list[dict]:
    """Drop near-duplicate items across chunks.

    Two items are considered duplicates when they share the same
    ``type`` and a normalised ``text`` fingerprint. The first
    occurrence (which usually has the most context) is kept.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for item in items:
        item_type = str(item.get("type", ""))
        text = str(item.get("text", ""))
        fingerprint = (item_type, _normalise_item_text(text))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(item)
    return out



def _parse_classify_response(raw: bytes) -> Classification:
    """Parse the raw HTTP body of a classify call.

    Tolerates code-fenced JSON, leading prose, and trailing prose;
    the assistant's reply is usually short and may be wrapped in
    ```json ... ``` fences even when asked not to be.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    # Strip ```json fences if present.
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Find the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(
            f"classifier response has no JSON object: {text[:200]!r}"
        )
    candidate = text[start:end + 1]
    try:
        obj = json.loads(candidate)
    except ValueError as exc:
        raise LLMError(
            f"classifier response is not valid JSON: {candidate[:200]!r}"
        ) from exc
    if not isinstance(obj, dict):
        raise LLMError(
            f"classifier response is not a JSON object: {candidate[:200]!r}"
        )
    kind = obj.get("kind")
    confidence = obj.get("confidence")
    if kind not in VALID_KINDS:
        raise LLMError(
            f"classifier returned an unknown kind: {kind!r}"
        )
    try:
        confidence_f = float(confidence)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise LLMError(
            f"classifier returned non-numeric confidence: {confidence!r}"
        ) from exc
    if not (0.0 <= confidence_f <= 1.0):
        raise LLMError(
            f"classifier confidence out of range [0, 1]: {confidence_f}"
        )
    return Classification(kind=kind, confidence=confidence_f)


# --- Summarisation (M9) --------------------------------------------------
#
# A summary is an overview ('text'), sections with headings, and key
# points. Its length is picked here from the transcript length, not
# left to the model, so a two-minute memo stays short and a two-hour
# lecture gets real notes. Its structure follows the recording kind.

#: (transcript chars up to, overview, sections, key points). Speech is
#: roughly 800 characters a minute: ~4 min, ~15 min, ~1 h, longer.
_SUMMARY_BANDS: list[tuple[float, str, str, str]] = [
    (3_000, "1-3 sentences", "", "0-3"),
    (12_000, "2-4 sentences", "0-4 sections of 1-3 sentences each", "3-6"),
    (48_000, "3-5 sentences",
     "3-6 sections of about 50-100 words each", "5-10"),
    (float("inf"), "4-6 sentences",
     "5-10 sections of about 60-120 words each", "8-15"),
]

# How each kind of recording is structured.
_KIND_GUIDE: dict[str, str] = {
    "lecture": (
        "This is a lecture. Write notes a student could revise from: "
        "follow the order the material was taught and cover the main "
        "concepts, definitions, formulas, examples and arguments, with "
        "their specifics. If the lecturer stressed something as "
        "important, for the exam, or for an assignment, end with a "
        "section headed 'Exam and assignment notes'."
    ),
    "meeting": (
        "This is a meeting. Order the sections: the purpose, the main "
        "discussion points (one section per topic), 'Decisions', then "
        "'Open questions'. Leave out any that have no content. Say who "
        "said or decided what when the speakers can be told apart."
    ),
    "voice_memo": (
        "This is a personal voice memo. Stay close to what was said. "
        "Use sections only if it covers several separate subjects."
    ),
    "other": (
        "Use sections only if the recording covers several separate "
        "subjects."
    ),
}

# Requests that write long summaries need more than the default 60 s.
_SUMMARY_TIMEOUT = 180.0
# The synthesis prompt carries the notes on every part (~1 KB each).
_SYNTHESIS_MAX_CHARS = 60_000


def _summary_shape(kind: Optional[str], transcript_chars: int) -> str:
    """The JSON shape, length and structure rules for a summary."""
    overview, sections, points = next(
        (o, sec, kp) for limit, o, sec, kp in _SUMMARY_BANDS
        if transcript_chars <= limit
    )
    sections_rule = (
        f"- 'sections' is a list of {sections}, in the order they came "
        "up. Each is an object with 'heading' (a short title, under 8 "
        "words) and 'text' (plain prose, no bullet points).\n"
        if sections else
        "- 'sections' is an empty list: the recording is short.\n"
    )
    return (
        "Reply with ONLY a JSON object (no prose, no markdown fences) "
        "with exactly three keys: 'text', 'sections' and "
        "'key_points'.\n\n"
        f"- 'text' is an overview of the whole recording in {overview}.\n"
        + sections_rule
        + f"- 'key_points' is a list of {points} short bullet points "
        "(each under 20 words) with the most important takeaways.\n\n"
        "Write in the third person, present tense, like notes you'd "
        "read back next month. Use only what is in the transcript. "
        "These lengths are upper limits: if the recording has little "
        "content, write less. Never pad.\n\n"
        + _KIND_GUIDE.get(kind or "other", _KIND_GUIDE["other"])
    )


def _summarise_prompt(kind: Optional[str], transcript_chars: int) -> str:
    return (
        "You are a personal-note summariser. Given an audio transcript, "
        "produce a summary.\n\n" + _summary_shape(kind, transcript_chars)
    )


def _load_json_object(raw: bytes, what: str) -> dict:
    """Find and parse the JSON object in a model reply, tolerating
    code fences and prose around it."""
    text = raw.decode("utf-8", errors="replace").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(
            f"{what} response has no JSON object: {text[:200]!r}"
        )
    candidate = text[start:end + 1]
    try:
        obj = json.loads(candidate)
    except ValueError as exc:
        raise LLMError(
            f"{what} response is not valid JSON: {candidate[:200]!r}"
        ) from exc
    if not isinstance(obj, dict):
        raise LLMError(
            f"{what} response is not a JSON object: {candidate[:200]!r}"
        )
    return obj


def _parse_sections(raw_sections: object) -> list[dict]:
    """Keep the well-formed ``{"heading", "text"}`` entries."""
    if not isinstance(raw_sections, list):
        return []
    out = []
    for sec in raw_sections:
        if not isinstance(sec, dict):
            continue
        heading, body = sec.get("heading"), sec.get("text")
        if isinstance(body, str) and body.strip():
            out.append({
                "heading": heading.strip() if isinstance(heading, str) else "",
                "text": body.strip(),
            })
    return out


def _parse_summarise_response(raw: bytes) -> Summary:
    """Parse the raw model output into a :class:`Summary`.

    Same code-fence / JSON-extraction tolerance as
    :func:`_parse_classify_response`. ``sections`` is optional:
    malformed entries are dropped rather than failing the summary.
    """
    obj = _load_json_object(raw, "summary")
    text_summary = obj.get("text", "")
    if not isinstance(text_summary, str):
        raise LLMError(
            f"summary 'text' is not a string: {text_summary!r}"
        )
    raw_points = obj.get("key_points", [])
    if not isinstance(raw_points, list):
        raise LLMError(
            f"summary 'key_points' is not a list: {raw_points!r}"
        )
    key_points = [str(p) for p in raw_points]
    return Summary(
        text=text_summary.strip(), key_points=key_points,
        sections=_parse_sections(obj.get("sections")),
    )


# --- Extraction (M9) -----------------------------------------------------
#
# The extractor pulls structured items out of a transcript. The
# ``kind`` argument (lecture, voice_memo, meeting, other) tells the
# model what kind of artefacts to look for — a meeting transcript
# gets action items, a lecture gets key terms + study questions, etc.
# The result is a list of plain dicts; the caller is responsible for
# validating the shape (e.g. ``add_artifact(kind='extraction', content=...)``
# doesn't care about the structure).

_EXTRACT_SYSTEM_PROMPT = (
    "You extract structured items from transcripts. Reply with ONLY a JSON "
    "object (no prose, no markdown fences) with one key: 'items'.\n\n"
    "'items' is a list of objects. Each object has at minimum two keys: "
    "'type' (short snake_case identifier like 'deadline', 'action_item', "
    "'topic', 'key_term', 'study_question', or 'fact') and 'text' "
    "(the item's content, plain English).\n\n"
    "Type guidance by recording kind:\n"
    "- 'meeting': focus on 'action_item' and 'deadline'.\n"
    "- 'lecture': focus on 'topic', 'key_term', 'study_question', 'fact'.\n"
    "- 'voice_memo': focus on 'fact' and 'topic'.\n"
    "- 'other': include a mix of 'topic' and 'fact'.\n\n"
    "Aim for 3-10 items. Only include items that are clearly supported by "
    "the transcript; do not invent."
)


def _parse_extract_response(raw: bytes) -> list[dict]:
    """Parse the model output into a list of structured items.

    Defensive against code fences, prose around JSON, and
    heterogeneous shapes. We coerce the ``text`` field to a string
    but otherwise pass dicts through unchanged so the caller can
    decide what to do with the structure.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(
            f"extraction response has no JSON object: {text[:200]!r}"
        )
    candidate = text[start:end + 1]
    try:
        obj = json.loads(candidate)
    except ValueError as exc:
        raise LLMError(
            f"extraction response is not valid JSON: {candidate[:200]!r}"
        ) from exc
    if not isinstance(obj, dict):
        raise LLMError(
            f"extraction response is not a JSON object: {candidate[:200]!r}"
        )
    raw_items = obj.get("items", [])
    if not isinstance(raw_items, list):
        raise LLMError(
            f"extraction 'items' is not a list: {raw_items!r}"
        )
    out: list[dict] = []
    for i, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise LLMError(
                f"extraction item {i} is not a dict: {item!r}"
            )
        if "type" not in item or "text" not in item:
            raise LLMError(
                f"extraction item {i} missing 'type' or 'text': {item!r}"
            )
        # Coerce text to a string; keep other keys verbatim so callers
        # can extend the schema (e.g. add a 'due' field for deadlines).
        normalised = dict(item)
        normalised["text"] = str(item["text"])
        out.append(normalised)
    return out


class TextProvider(LLMProvider):
    """Classification / summarisation / extraction via the
    OpenRouter chat completions API.

    The configured text model is asked to pick one of the four
    recording kinds from a transcript and return a JSON object
    with ``kind`` and ``confidence``. The response is parsed
    defensively — see :func:`_parse_classify_response`.
    """

    # ``name`` is stored on artifacts as the generating model.
    name = TEXT_MODEL

    def __init__(
        self,
        api_key: str,
        *,
        model: Optional[str] = None,
        chat_url: Optional[str] = None,
        http_transport: Optional[HttpTransport] = None,
    ) -> None:
        if not api_key:
            raise LLMConfigError(
                "TextProvider requires LR_TEXT_API_KEY"
            )
        self._api_key = api_key
        self._model = (
            model or os.environ.get(TEXT_MODEL_ENV, "").strip() or TEXT_MODEL
        )
        self.name = self._model
        base = os.environ.get(TEXT_BASE_URL_ENV, "").strip().rstrip("/")
        self._chat_url = chat_url or (
            f"{base}/chat/completions" if base else OPENROUTER_CHAT_URL
        )
        self._http = http_transport or _urllib_transport

    def classify(self, transcript: str) -> Classification:
        if not isinstance(transcript, str):
            raise LLMError("transcript must be a string")
        # 12_000 chars ≈ 3k tokens — leaves room for the prompt +
        # response in the text model's window. For classification,
        # the topic is usually established in the first few minutes
        # so this rarely hurts accuracy.
        message = self._chat(
            _CLASSIFY_SYSTEM_PROMPT,
            f"Classify this transcript:\n\n{transcript}",
            max_chars=12_000,
        )
        if not isinstance(message, str):
            raise LLMError(
                f"Text model response message is not a string: {message!r}"
            )
        return _parse_classify_response(message.encode("utf-8"))

    # The text model is not a transcription model; raise for clarity.
    def transcribe(self, audio_path: Path) -> str:
        raise LLMConfigError(
            "TextProvider is not a transcription model; use "
            "TranscriptionProvider for transcription"
        )

    def _chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        timeout: float = 60.0,
        max_chars: int = 16_000,
    ) -> str:
        """Shared HTTP plumbing for summarisation / extraction /
        classification.

        POSTs a chat-completions request with the given system
        prompt + user content (truncated to ``max_chars``), returns
        the assistant message text. Raises :class:`LLMError` /
        :class:`LLMHTTPError` on failure.
        """
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt[:max_chars]},
            ],
            "temperature": 0.0,
            # Every caller wants one JSON object back. Without this,
            # MiniMax M3 sometimes writes lists as ["a"] ["b"] instead
            # of ["a", "b"]. Providers that lack the option ignore it.
            "response_format": {"type": "json_object"},
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        status, raw = self._http(
            self._chat_url, data=body, headers=headers, timeout=timeout,
        )
        if status < 200 or status >= 300:
            raise LLMHTTPError(
                status, raw,
                f"Text model chat call failed: HTTP {status}: "
                f"{raw[:200]!r}",
            )
        try:
            payload_resp = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LLMError(
                f"Text model returned non-JSON body: {raw[:200]!r}"
            ) from exc
        try:
            content = payload_resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"Text model response missing choices[0].message.content: "
                f"{raw[:200]!r}"
            ) from exc
        if isinstance(content, str):
            content = _THINK_RE.sub("", content).strip()
        return content

    def summarize(
        self, transcript: str, kind: Optional[str] = None,
    ) -> Summary:
        """Produce an overview, sections and key points, sized to the
        transcript and structured by ``kind`` (see
        :func:`_summary_shape`).

        For short transcripts (one chunk or less) this is a single
        LLM call. For long transcripts the work is split into a
        map step (notes on each chunk) followed by a reduce step
        (one synthesis call that merges them). See
        :func:`_chunk_text` and :func:`_synthesise_summaries`.
        """
        if not isinstance(transcript, str):
            raise LLMError("transcript must be a string")
        chunks = _chunk_text(transcript)
        if len(chunks) == 1:
            # Short path: one call, same shape as before.
            message = self._chat(
                _summarise_prompt(kind, len(transcript)),
                f"Summarise this transcript:\n\n{transcript}",
                timeout=_SUMMARY_TIMEOUT, max_chars=_CHUNK_SIZE,
            )
            if not isinstance(message, str):
                raise LLMError(
                    f"Text model response message is not a string: "
                    f"{message!r}"
                )
            return _parse_summarise_response(message.encode("utf-8"))
        # Long path: map-reduce.
        partials = [
            _summarise_one_chunk(
                self, chunk, section_index=i, total_sections=len(chunks),
                kind=kind,
            )
            for i, chunk in enumerate(chunks)
        ]
        return _synthesise_summaries(
            self, partials, kind=kind, transcript_chars=len(transcript),
        )

    def extract(self, transcript: str, kind: str) -> list[dict]:
        """Pull structured items out of a transcript.

        For short transcripts this is a single LLM call. For long
        transcripts the work is split per-chunk and the partial
        item lists are de-duplicated (a ``deadline`` mentioned in
        chunks 1 and 3 should appear once in the final result).
        ``kind`` is one of ``lecture``/``voice_memo``/``meeting``/
        ``other``; it tunes the prompt so the model knows what
        kind of items to look for (action items vs. study
        questions, etc.).
        """
        if not isinstance(transcript, str):
            raise LLMError("transcript must be a string")
        if not isinstance(kind, str):
            raise LLMError("kind must be a string")
        if kind not in VALID_KINDS:
            raise LLMError(
                f"extract: kind must be one of {sorted(VALID_KINDS)}, "
                f"got {kind!r}"
            )
        chunks = _chunk_text(transcript)
        if len(chunks) == 1:
            # Short path: one call, same shape as before.
            message = self._chat(
                _EXTRACT_SYSTEM_PROMPT,
                (
                    f"Recording kind: {kind}\n\n"
                    f"Transcript:\n\n{transcript}"
                ),
                max_chars=_CHUNK_SIZE,
            )
            if not isinstance(message, str):
                raise LLMError(
                    f"Text model response message is not a string: "
                    f"{message!r}"
                )
            return _parse_extract_response(message.encode("utf-8"))
        # Long path: per-chunk extraction, then dedupe.
        all_items: list[dict] = []
        for i, chunk in enumerate(chunks):
            all_items.extend(
                _extract_one_chunk(
                    self, chunk, kind=kind,
                    section_index=i, total_sections=len(chunks),
                )
            )
        return _dedupe_items(all_items)


# --- Factory ----------------------------------------------------------------


def default_providers(
    *,
    transcription_key: Optional[str] = None,
    text_key: Optional[str] = None,
    http_transport: Optional[HttpTransport] = None,
) -> dict[str, LLMProvider]:
    """Return the provider registry based on supplied keys.

    Pulls the keys from env vars when the explicit arguments are
    not supplied:

    * transcription key: ``LR_TRANSCRIPTION_API_KEY`` (fallback:
      legacy ``LR_OPENROUTER_API_KEY``)
    * text key: ``LR_TEXT_API_KEY`` (fallbacks: legacy
      ``LR_MINIMAX_API_KEY``, then ``LR_OPENROUTER_API_KEY``)

    Absent keys yield a no-op provider rather than an error — this
    keeps the test suite (and any local install without keys)
    working. Pass ``http_transport`` to inject a fake for tests.

    Registry keys: ``"transcript"`` (TranscriptionProvider) and
    ``"text"`` (TextProvider — used for classify/summarise/extract).
    """
    t_key = (
        transcription_key
        if transcription_key is not None
        else (
            os.environ.get("LR_TRANSCRIPTION_API_KEY", "")
            or os.environ.get("LR_OPENROUTER_API_KEY", "")
        )
    )
    txt_key = (
        text_key
        if text_key is not None
        else (
            os.environ.get("LR_TEXT_API_KEY", "")
            or os.environ.get("LR_MINIMAX_API_KEY", "")
            or os.environ.get("LR_OPENROUTER_API_KEY", "")
        )
    )
    return {
        "transcript": (
            TranscriptionProvider(t_key, http_transport=http_transport)
            if t_key
            else _NoOpProvider()
        ),
        "text": (
            TextProvider(txt_key, http_transport=http_transport)
            if txt_key
            else _NoOpProvider()
        ),
    }


# Backward-compatible aliases for the pre-rename class names.
OpenRouterProvider = TranscriptionProvider
MiniMaxProvider = TextProvider


def get_provider(name: str = "transcript") -> LLMProvider:
    """Convenience accessor. Defaults to the transcript provider."""
    return default_providers().get(name) or _NoOpProvider()



