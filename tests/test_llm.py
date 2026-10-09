"""Tests for the LLM providers (Milestone 8).

The OpenRouter and MiniMax providers talk to external HTTP
APIs. Tests inject a fake transport so no real network call is
ever made. We also exercise the response-parsing code paths
that the providers share (code-fence stripping, JSON
extraction, etc.).
"""
from __future__ import annotations

import json
import unittest

from services import llm
from services.llm import (
    Classification,
    LLMConfigError,
    LLMError,
    LLMHTTPError,
    LLMProvider,
    OpenRouterProvider,
    MiniMaxProvider,
    VALID_KINDS,
    _build_multipart_body,
    _parse_classify_response,
)


# --- Test transport ---------------------------------------------------------


def _fake(status, body=b"", capture=None):
    """Build a fake transport. ``capture`` (optional dict) is
    populated with the last call's (url, data, headers)."""

    def transport(url, *, data=None, headers=None, timeout=60.0):
        if capture is not None:
            capture["url"] = url
            capture["data"] = data
            capture["headers"] = dict(headers or {})
        return (status, body)

    return transport


def _write_wav(tmp_path, name="x.wav", payload=b"RIFF\x00\x00\x00\x00"):
    p = tmp_path / name
    p.write_bytes(payload)
    return p


# --- Multipart builder ------------------------------------------------------


class MultipartBuilderTests(unittest.TestCase):
    def test_builds_body_with_file_and_fields(self):
        body = _build_multipart_body(
            file_field="file", filename="x.wav",
            content=b"AAAA", content_type="audio/wav",
            fields={"model": "openai/whisper-large-v3"},
            boundary="BOUND",
        )
        text = body.decode("ascii")
        # Field part.
        self.assertIn('name="model"', text)
        self.assertIn("openai/whisper-large-v3", text)
        # File part with the right headers.
        self.assertIn('name="file"', text)
        self.assertIn('filename="x.wav"', text)
        self.assertIn("Content-Type: audio/wav", text)
        # File payload is present.
        self.assertIn("AAAA", text)
        # Boundary terminator.
        self.assertTrue(text.rstrip().endswith("--BOUND--"))

    def test_body_is_bytes(self):
        b = _build_multipart_body(
            "f", "x", b"x", "text/plain", {}, "B",
        )
        self.assertIsInstance(b, bytes)


# --- OpenRouter provider ----------------------------------------------------


class OpenRouterProviderTests(unittest.TestCase):
    def test_init_requires_key(self):
        with self.assertRaises(LLMConfigError):
            OpenRouterProvider("")

    def test_transcribe_plain_text_response(self):
        capture = {}
        p = OpenRouterProvider(
            "key", http_transport=_fake(
                200, b"hello world", capture,
            ),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            audio = _write_wav(Path(d))
            text = p.transcribe(audio)
        self.assertEqual(text, "hello world")
        # URL is the right endpoint.
        self.assertIn("/audio/transcriptions", capture["url"])
        # Auth header is set.
        self.assertEqual(
            capture["headers"].get("Authorization"), "Bearer key",
        )
        # Content-Type has the boundary.
        ct = capture["headers"].get("Content-Type", "")
        self.assertTrue(ct.startswith("multipart/form-data"))
        self.assertIn("boundary=lr-openrouter-boundary", ct)
        # Body is a multipart payload.
        self.assertIn(b"filename=\"x.wav\"", capture["data"])
        # The 'model' field is included in the multipart.
        self.assertIn(b"openai/whisper-large-v3", capture["data"])

    def test_transcribe_json_response_with_text(self):
        p = OpenRouterProvider(
            "k", http_transport=_fake(
                200, b'{"text": "the quick brown fox"}',
            ),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            text = p.transcribe(_write_wav(Path(d)))
        self.assertEqual(text, "the quick brown fox")

    def test_transcribe_4xx_raises_http_error(self):
        p = OpenRouterProvider(
            "k", http_transport=_fake(401, b"unauthorized"),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(LLMHTTPError) as ctx:
                p.transcribe(_write_wav(Path(d)))
        self.assertEqual(ctx.exception.status, 401)

    def test_transcribe_5xx_raises_http_error(self):
        p = OpenRouterProvider(
            "k", http_transport=_fake(500, b"upstream dead"),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(LLMHTTPError) as ctx:
                p.transcribe(_write_wav(Path(d)))
        self.assertEqual(ctx.exception.status, 500)

    def test_transcribe_missing_file_raises_filenotfound(self):
        from pathlib import Path
        p = OpenRouterProvider("k", http_transport=_fake(200, b"x"))
        with self.assertRaises(FileNotFoundError):
            p.transcribe(Path("/no/such/file.wav"))

    def test_classify_is_not_implemented(self):
        p = OpenRouterProvider("k", http_transport=_fake(200, b"x"))
        with self.assertRaises(LLMConfigError):
            p.classify("transcript text")

    def test_summarize_is_not_implemented(self):
        p = OpenRouterProvider("k", http_transport=_fake(200, b"x"))
        with self.assertRaises(LLMConfigError):
            p.summarize("text")


# --- MiniMax provider -------------------------------------------------------


class MiniMaxProviderTests(unittest.TestCase):
    def test_init_requires_key(self):
        with self.assertRaises(LLMConfigError):
            MiniMaxProvider("")

    def test_classify_each_kind(self):
        for kind in VALID_KINDS:
            body = json.dumps({
                "choices": [{
                    "message": {
                        "content": json.dumps(
                            {"kind": kind, "confidence": 0.8},
                        ),
                    },
                }],
            }).encode("utf-8")
            p = MiniMaxProvider("k", http_transport=_fake(200, body))
            result = p.classify("some transcript text")
            self.assertEqual(result.kind, kind)
            self.assertAlmostEqual(result.confidence, 0.8)

    def test_classify_sends_chat_completions_request(self):
        capture = {}
        body = json.dumps({
            "choices": [{
                "message": {"content": '{"kind": "lecture", "confidence": 0.9}'},
            }],
        }).encode("utf-8")
        p = MiniMaxProvider(
            "k", http_transport=_fake(200, body, capture),
        )
        p.classify("transcript")
        self.assertIn("chat/completions", capture["url"])
        self.assertEqual(
            capture["headers"].get("Authorization"), "Bearer k",
        )
        # The body is JSON with a messages field.
        sent = json.loads(capture["data"].decode("utf-8"))
        self.assertIn("messages", sent)
        self.assertEqual(sent["model"], "MiniMax-M3")
        # The user content includes the transcript.
        user_msg = sent["messages"][-1]["content"]
        self.assertIn("transcript", user_msg)

    def test_classify_handles_markdown_fenced_response(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": '```json\n{"kind": "meeting", "confidence": 0.7}\n```',
                },
            }],
        }).encode("utf-8")
        p = MiniMaxProvider("k", http_transport=_fake(200, body))
        result = p.classify("x")
        self.assertEqual(result.kind, "meeting")
        self.assertAlmostEqual(result.confidence, 0.7)

    def test_classify_handles_prose_around_json(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": 'Sure! Here you go: {"kind": "voice_memo", "confidence": 0.6} -- hope that helps.',
                },
            }],
        }).encode("utf-8")
        p = MiniMaxProvider("k", http_transport=_fake(200, body))
        result = p.classify("x")
        self.assertEqual(result.kind, "voice_memo")

    def test_classify_4xx_raises_http_error(self):
        p = MiniMaxProvider("k", http_transport=_fake(429, b"rate limit"))
        with self.assertRaises(LLMHTTPError) as ctx:
            p.classify("x")
        self.assertEqual(ctx.exception.status, 429)

    def test_classify_malformed_response_raises_llm_error(self):
        body = json.dumps({
            "choices": [{"message": {"content": "not json at all"}}],
        }).encode("utf-8")
        p = MiniMaxProvider("k", http_transport=_fake(200, body))
        with self.assertRaises(LLMError):
            p.classify("x")

    def test_classify_unknown_kind_raises_llm_error(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps(
                        {"kind": "gathering", "confidence": 0.5},
                    ),
                },
            }],
        }).encode("utf-8")
        p = MiniMaxProvider("k", http_transport=_fake(200, body))
        with self.assertRaises(LLMError):
            p.classify("x")

    def test_classify_bad_confidence_raises_llm_error(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps(
                        {"kind": "lecture", "confidence": 1.5},
                    ),
                },
            }],
        }).encode("utf-8")
        p = MiniMaxProvider("k", http_transport=_fake(200, body))
        with self.assertRaises(LLMError):
            p.classify("x")

    def test_classify_missing_choices_raises_llm_error(self):
        body = json.dumps({"error": "rate limit"}).encode("utf-8")
        p = MiniMaxProvider("k", http_transport=_fake(200, body))
        with self.assertRaises(LLMError):
            p.classify("x")

    def test_classify_truncates_long_transcripts(self):
        capture = {}
        body = json.dumps({
            "choices": [{
                "message": {"content": '{"kind": "lecture", "confidence": 0.5}'},
            }],
        }).encode("utf-8")
        p = MiniMaxProvider(
            "k", http_transport=_fake(200, body, capture),
        )
        long = "x" * 50_000
        p.classify(long)
        sent = json.loads(capture["data"].decode("utf-8"))
        # The user content is truncated to <= 12_000 chars
        # (plus the prompt prefix).
        user_content = sent["messages"][-1]["content"]
        self.assertLess(len(user_content), 20_000)

    def test_transcribe_is_not_implemented(self):
        p = MiniMaxProvider("k", http_transport=_fake(200, b"x"))
        with self.assertRaises(LLMConfigError):
            p.transcribe("/tmp/x.wav")


# --- Response parser (unit) -------------------------------------------------


class ParseClassifyResponseTests(unittest.TestCase):
    def test_parses_pure_json(self):
        c = _parse_classify_response(
            b'{"kind": "lecture", "confidence": 0.9}',
        )
        self.assertEqual(c.kind, "lecture")
        self.assertEqual(c.confidence, 0.9)

    def test_strips_fences(self):
        c = _parse_classify_response(
            b"```json\n{\"kind\": \"meeting\", \"confidence\": 0.5}\n```",
        )
        self.assertEqual(c.kind, "meeting")

    def test_finds_json_in_prose(self):
        c = _parse_classify_response(
            b'Sure! {"kind": "voice_memo", "confidence": 0.5} ok',
        )
        self.assertEqual(c.kind, "voice_memo")

    def test_no_json_object_raises(self):
        with self.assertRaises(LLMError):
            _parse_classify_response(b"this is not json")

    def test_invalid_json_raises(self):
        with self.assertRaises(LLMError):
            _parse_classify_response(b"{not valid json}")


# --- Factory ----------------------------------------------------------------


class DefaultProvidersTests(unittest.TestCase):
    def test_no_keys_returns_noops(self):
        providers = llm.default_providers(
            openrouter_key="", minimax_key="",
        )
        self.assertEqual(providers["transcript"].name, "noop")
        self.assertEqual(providers["classifier"].name, "noop")

    def test_keys_return_real_providers(self):
        providers = llm.default_providers(
            openrouter_key="k1", minimax_key="k2",
            http_transport=_fake(200, b"x"),
        )
        self.assertIsInstance(providers["transcript"], OpenRouterProvider)
        self.assertIsInstance(providers["classifier"], MiniMaxProvider)

    def test_get_provider_default(self):
        providers = llm.default_providers(
            openrouter_key="", minimax_key="",
        )
        p = llm.get_provider("transcript")
        self.assertEqual(p.name, "noop")


if __name__ == "__main__":
    unittest.main()