"""LLM providers for transcription (OpenRouter Whisper) and
classification (MiniMax M3).

The pipeline in :mod:`services.processing` calls two of these
providers per session:

1. A **transcript provider** — turns a WAV file into text.
   The concrete implementation is :class:`OpenRouterProvider`
   which uses the OpenRouter OpenAI-compatible Whisper endpoint
   (``POST /api/v1/audio/transcriptions``).

2. A **classifier** — picks one of the four recording kinds
   (``lecture``, ``voice_memo``, ``meeting``, ``other``) from
   the transcript. The concrete implementation is
   :class:`MiniMaxProvider` which uses the MiniMax M3 chat
   completions endpoint.

Each provider takes an ``http_transport`` callable (or falls back
to the stdlib ``urllib.request``) so tests can inject a fake
transport without monkey-patching global state.
"""
from __future__ import annotations

import json
import os
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

    def summarize(self, transcript: str) -> Summary:
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

    def summarize(self, transcript: str) -> Summary:
        raise LLMConfigError("no LLM provider configured")

    def extract(self, transcript: str, kind: str) -> list[dict]:
        raise LLMConfigError("no LLM provider configured")


# --- OpenRouter (Whisper) ---------------------------------------------------
#
# OpenRouter's API is OpenAI-compatible. The transcription endpoint
# accepts a multipart/form-data POST with the audio file and returns
# the transcript. We use the stdlib ``email``/``urllib`` machinery
# to build the multipart body (no third-party dep).

OPENROUTER_TRANSCRIPTIONS_URL = (
    "https://openrouter.ai/api/v1/audio/transcriptions"
)
OPENROUTER_WHISPER_MODEL = "openai/whisper-large-v3"

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


class OpenRouterProvider(LLMProvider):
    """Real Whisper transcription via OpenRouter.

    Reads the audio file at ``audio_path`` and posts it to
    ``https://openrouter.ai/api/v1/audio/transcriptions`` as
    multipart/form-data. Returns the transcript text.

    Pass ``http_transport`` to inject a fake (used in tests); the
    default is the stdlib ``urllib.request`` wrapper.
    """
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = OPENROUTER_WHISPER_MODEL,
        http_transport: Optional[HttpTransport] = None,
    ) -> None:
        if not api_key:
            raise LLMConfigError(
                "OpenRouterProvider requires LR_OPENROUTER_API_KEY"
            )
        self._api_key = api_key
        self._model = model
        self._http = http_transport or _urllib_transport

    def transcribe(self, audio_path: Path) -> str:
        if not isinstance(audio_path, Path):
            audio_path = Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(str(audio_path))
        # Build a multipart body. We use a fixed boundary so the
        # request is reproducible in tests.
        boundary = "lr-openrouter-boundary"
        content = audio_path.read_bytes()
        body = _build_multipart_body(
            file_field="file",
            filename=audio_path.name,
            content=content,
            content_type="audio/wav",
            fields={"model": self._model},
            boundary=boundary,
        )
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        status, raw = self._http(
            OPENROUTER_TRANSCRIPTIONS_URL,
            data=body, headers=headers, timeout=120.0,
        )
        if status < 200 or status >= 300:
            raise LLMHTTPError(
                status, raw,
                f"OpenRouter transcription failed: HTTP {status}: "
                f"{raw[:200]!r}",
            )
        # OpenRouter returns either JSON {"text": "..."} or plain
        # text (depending on response_format). Be tolerant: if the
        # body parses as JSON with a "text" field, use that;
        # otherwise decode the body as UTF-8.
        try:
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict) and "text" in payload:
                return str(payload["text"])
        except (ValueError, UnicodeDecodeError):
            pass
        return raw.decode("utf-8", errors="replace")

    # M9: not yet implemented; raise for clarity.
    def classify(self, transcript: str) -> Classification:
        raise LLMConfigError(
            "OpenRouter is the transcription provider; use "
            "MiniMaxProvider for classification"
        )

    def summarize(self, transcript: str) -> Summary:
        raise LLMConfigError("summarisation lands in M9")

    def extract(self, transcript: str, kind: str) -> list[dict]:
        raise LLMConfigError("extraction lands in M9")


# --- MiniMax M3 (classification) --------------------------------------------
#
# MiniMax exposes an OpenAI-compatible chat completions endpoint
# at https://api.minimax.chat/v1. We POST a small system prompt
# that constrains the response to a JSON object with ``kind`` and
# ``confidence`` fields, then parse the assistant message.

MINIMAX_CHAT_URL = "https://api.minimax.chat/v1/chat/completions"
MINIMAX_MODEL = "MiniMax-M3"

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


class MiniMaxProvider(LLMProvider):
    """Real classification via the MiniMax M3 chat completions API.

    The model is asked to pick one of the four recording kinds
    from a transcript and return a JSON object with ``kind`` and
    ``confidence``. The response is parsed defensively — see
    :func:`_parse_classify_response`.
    """
    name = "minimax"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = MINIMAX_MODEL,
        chat_url: str = MINIMAX_CHAT_URL,
        http_transport: Optional[HttpTransport] = None,
    ) -> None:
        if not api_key:
            raise LLMConfigError(
                "MiniMaxProvider requires LR_MINIMAX_API_KEY"
            )
        self._api_key = api_key
        self._model = model
        self._chat_url = chat_url
        self._http = http_transport or _urllib_transport

    def classify(self, transcript: str) -> Classification:
        if not isinstance(transcript, str):
            raise LLMError("transcript must be a string")
        # Truncate very long transcripts to keep the prompt small.
        # 12_000 chars is roughly 3k tokens — well within MiniMax M3's
        # window. The summary pipeline (M9) will operate on the full
        # text; for classification, the topic is usually established
        # in the first few minutes.
        truncated = transcript[:12_000]
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Classify this transcript:\n\n{truncated}"
                    ),
                },
            ],
            "temperature": 0.0,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        status, raw = self._http(
            self._chat_url, data=body, headers=headers, timeout=60.0,
        )
        if status < 200 or status >= 300:
            raise LLMHTTPError(
                status, raw,
                f"MiniMax classify failed: HTTP {status}: "
                f"{raw[:200]!r}",
            )
        try:
            payload_resp = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LLMError(
                f"MiniMax returned non-JSON body: {raw[:200]!r}"
            ) from exc
        try:
            message = payload_resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"MiniMax response missing choices[0].message.content: "
                f"{raw[:200]!r}"
            ) from exc
        if not isinstance(message, str):
            raise LLMError(
                f"MiniMax response message is not a string: {message!r}"
            )
        return _parse_classify_response(message.encode("utf-8"))

    # Not used in M8; raise for clarity.
    def transcribe(self, audio_path: Path) -> str:
        raise LLMConfigError(
            "MiniMax is the classifier; use OpenRouterProvider for "
            "transcription"
        )

    def summarize(self, transcript: str) -> Summary:
        raise LLMConfigError("summarisation lands in M9")

    def extract(self, transcript: str, kind: str) -> list[dict]:
        raise LLMConfigError("extraction lands in M9")


# --- Factory ----------------------------------------------------------------


def default_providers(
    *,
    openrouter_key: Optional[str] = None,
    minimax_key: Optional[str] = None,
    http_transport: Optional[HttpTransport] = None,
) -> dict[str, LLMProvider]:
    """Return the provider registry based on supplied keys.

    Pulls the keys from env vars (``LR_OPENROUTER_API_KEY``,
    ``LR_MINIMAX_API_KEY``) when the explicit ``openrouter_key`` /
    ``minimax_key`` arguments are not supplied. Absent keys yield
    a no-op provider rather than an error — this keeps the test
    suite (and any local install without keys) working.

    Pass ``http_transport`` to inject a fake for tests.
    """
    or_key = (
        openrouter_key
        if openrouter_key is not None
        else os.environ.get("LR_OPENROUTER_API_KEY", "")
    )
    mm_key = (
        minimax_key
        if minimax_key is not None
        else os.environ.get("LR_MINIMAX_API_KEY", "")
    )
    return {
        "transcript": (
            OpenRouterProvider(
                or_key, http_transport=http_transport,
            )
            if or_key
            else _NoOpProvider()
        ),
        "classifier": (
            MiniMaxProvider(mm_key, http_transport=http_transport)
            if mm_key
            else _NoOpProvider()
        ),
    }


def get_provider(name: str = "transcript") -> LLMProvider:
    """Convenience accessor. Defaults to the transcript provider."""
    return default_providers().get(name) or _NoOpProvider()



