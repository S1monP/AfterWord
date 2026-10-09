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


# --- OpenRouter (Whisper) ---------------------------------------------------
#
# OpenRouter's API is OpenAI-compatible. The transcription endpoint
# accepts a multipart/form-data POST with the audio file and returns
# the transcript. We use the stdlib ``email``/``urllib`` machinery
# to build the multipart body (no third-party dep).

OPENROUTER_TRANSCRIPTIONS_URL = (
    "https://openrouter.ai/api/v1/audio/transcriptions"
)
OPENROUTER_WHISPER_MODEL = "microsoft/mai-transcribe-2"

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

MINIMAX_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
MINIMAX_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

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


# Per-section system prompt for the map step. Tells the model it is
# looking at a fragment, so it focuses on what's actually present
# rather than guessing about the whole.
_SUMMARISE_SECTION_PROMPT = (
    "You summarise a single section of a longer audio transcript. "
    "Reply with ONLY a JSON object (no prose, no markdown fences) with "
    "exactly two keys: 'text' and 'key_points'.\n\n"
    "- 'text' is 1-3 sentences summarising THIS SECTION ONLY. Do not "
    "speculate about content outside the section.\n"
    "- 'key_points' is a list of 0-5 short bullet points (each under 20 "
    "words) covering the most important takeaways FROM THIS SECTION.\n\n"
    "If the section is empty, repetitive, or contains no useful content, "
    "return {\"text\": \"\", \"key_points\": []}."
)


# Synthesis prompt: takes the partial summaries and produces a single
# coherent summary + deduplicated key points.
_SYNTHESISE_SUMMARIES_PROMPT = (
    "You are given a list of partial summaries, each describing one "
    "section of a longer audio recording (in order). Your job is to "
    "synthesise them into ONE coherent final summary.\n\n"
    "Reply with ONLY a JSON object (no prose, no markdown fences) with "
    "exactly two keys: 'text' and 'key_points'.\n\n"
    "- 'text' is a 2-4 sentence summary of the WHOLE recording, written "
    "in the third person, present tense. Weave the section summaries "
    "together naturally; don't reference the sections.\n"
    "- 'key_points' is a deduplicated list of 3-7 short bullet points "
    "(each under 20 words) covering the most important takeaways from "
    "the entire recording. Drop near-duplicates; keep distinct points.\n\n"
    "If the partials are empty, return "
    "{\"text\": \"no content\", \"key_points\": []}."
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
    provider: "MiniMaxProvider",
    chunk: str,
    *,
    section_index: int,
    total_sections: int,
) -> Summary:
    """Run the per-chunk summarise LLM call. Returns an empty
    :class:`Summary` on a parse error so the map step degrades
    gracefully (the synthesis step will just have one fewer input).
    """
    user_prompt = (
        f"This is section {section_index + 1} of {total_sections} of "
        f"a longer transcript. Summarise it:\n\n{chunk}"
    )
    try:
        message = provider._chat(
            _SUMMARISE_SECTION_PROMPT, user_prompt,
            max_chars=_CHUNK_SIZE,
        )
        return _parse_summarise_response(message.encode("utf-8"))
    except LLMError:
        return Summary(text="", key_points=[])


def _synthesise_summaries(
    provider: "MiniMaxProvider",
    partials: list[Summary],
) -> Summary:
    """Run the synthesis LLM call that takes the partial
    summaries and produces the final, coherent summary.
    """
    if not partials:
        return Summary(text="", key_points=[])
    formatted = "\n\n".join(
        f"[Section {i + 1}] {p.text}\n  Key points: "
        + (", ".join(p.key_points) if p.key_points else "(none)")
        for i, p in enumerate(partials)
    )
    user_prompt = (
        f"Below are partial summaries of {len(partials)} sections of "
        f"a longer recording. Synthesise them into one final summary:\n\n"
        f"{formatted}"
    )
    message = provider._chat(
        _SYNTHESISE_SUMMARIES_PROMPT, user_prompt,
        max_chars=_CHUNK_SIZE,
    )
    return _parse_summarise_response(message.encode("utf-8"))


def _extract_one_chunk(
    provider: "MiniMaxProvider",
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
# The summariser produces a 2-3 sentence overview plus a list of
# key points. We keep the response shape small enough to fit
# comfortably in the 12k-char prompt window the model is given.

_SUMMARISE_SYSTEM_PROMPT = (
    "You are a personal-note summariser. Given an audio transcript, "
    "produce a JSON object with exactly two keys: 'text' and 'key_points'.\n\n"
    "- 'text' is a 2-3 sentence summary of the recording. "
    "Write in the third person, present tense, like notes you'd read back "
    "next month to remember what was discussed.\n"
    "- 'key_points' is a list of 3-7 short bullet points (each under 20 words) "
    "covering the most important takeaways.\n\n"
    "Reply with ONLY the JSON object, no prose, no markdown fences."
)


def _parse_summarise_response(raw: bytes) -> Summary:
    """Parse the raw model output into a :class:`Summary`.

    Same code-fence / JSON-extraction tolerance as
    :func:`_parse_classify_response`.
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
            f"summary response has no JSON object: {text[:200]!r}"
        )
    candidate = text[start:end + 1]
    try:
        obj = json.loads(candidate)
    except ValueError as exc:
        raise LLMError(
            f"summary response is not valid JSON: {candidate[:200]!r}"
        ) from exc
    if not isinstance(obj, dict):
        raise LLMError(
            f"summary response is not a JSON object: {candidate[:200]!r}"
        )
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
    return Summary(text=text_summary.strip(), key_points=key_points)


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
        # 12_000 chars ≈ 3k tokens — leaves room for the prompt +
        # response in MiniMax M3's window. For classification, the
        # topic is usually established in the first few minutes so
        # this rarely hurts accuracy.
        message = self._chat(
            _CLASSIFY_SYSTEM_PROMPT,
            f"Classify this transcript:\n\n{transcript}",
            max_chars=12_000,
        )
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
                f"MiniMax chat call failed: HTTP {status}: "
                f"{raw[:200]!r}",
            )
        try:
            payload_resp = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LLMError(
                f"MiniMax returned non-JSON body: {raw[:200]!r}"
            ) from exc
        try:
            return payload_resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"MiniMax response missing choices[0].message.content: "
                f"{raw[:200]!r}"
            ) from exc

    def summarize(self, transcript: str) -> Summary:
        """Produce a 2-3 sentence summary plus key points.

        For short transcripts (one chunk or less) this is a single
        LLM call. For long transcripts the work is split into a
        map step (one call per chunk) followed by a reduce step
        (one synthesis call that combines the partials). See
        :func:`_chunk_text` and :func:`_synthesise_summaries`.
        """
        if not isinstance(transcript, str):
            raise LLMError("transcript must be a string")
        chunks = _chunk_text(transcript)
        if len(chunks) == 1:
            # Short path: one call, same shape as before.
            message = self._chat(
                _SUMMARISE_SYSTEM_PROMPT,
                f"Summarise this transcript:\n\n{transcript}",
                max_chars=_CHUNK_SIZE,
            )
            if not isinstance(message, str):
                raise LLMError(
                    f"MiniMax response message is not a string: {message!r}"
                )
            return _parse_summarise_response(message.encode("utf-8"))
        # Long path: map-reduce.
        partials = [
            _summarise_one_chunk(
                self, chunk, section_index=i, total_sections=len(chunks),
            )
            for i, chunk in enumerate(chunks)
        ]
        return _synthesise_summaries(self, partials)

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
                    f"MiniMax response message is not a string: {message!r}"
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



