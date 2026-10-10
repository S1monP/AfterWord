"""Tests for the LLM providers (Milestone 8).

The transcription and text-model providers talk to external HTTP
APIs. Tests inject a fake transport so no real network call is
ever made. We also exercise the response-parsing code paths
that the providers share (code-fence stripping, JSON
extraction, etc.).
"""
from __future__ import annotations

import json
import os
import struct
import unittest

from services import llm
from services.llm import (
    Classification,
    LLMConfigError,
    LLMError,
    LLMHTTPError,
    LLMProvider,
    TranscriptionProvider,
    TextProvider,
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


def _write_real_wav(path_dir, name="real.wav", rate=10, channels=1,
                    bits=16, data=b"\x00\x01" * 50):
    """Build a well-formed PCM WAV. Defaults: rate 10 Hz, 16-bit
    mono, 100 bytes of data -> 50 whole frames = 5 s of audio."""
    frame = channels * (bits // 8)
    usable = (len(data) // frame) * frame
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + usable, b"WAVE",
        b"fmt ", 16, 1, channels, rate,
        rate * channels * (bits // 8), channels * (bits // 8), bits,
        b"data", usable,
    )
    p = path_dir / name
    p.write_bytes(header + data[:usable])
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


# --- Transcription provider ----------------------------------------------------


class TranscriptionProviderTests(unittest.TestCase):
    # Chunk compression shells out to ffmpeg, which is slow and
    # host-dependent; pin it off for these tests. The compression
    # behaviour itself is covered by dedicated tests below.
    @classmethod
    def setUpClass(cls):
        cls._old_compress = os.environ.get("LR_TRANSCRIBE_COMPRESS")
        os.environ["LR_TRANSCRIBE_COMPRESS"] = "0"

    @classmethod
    def tearDownClass(cls):
        if cls._old_compress is None:
            os.environ.pop("LR_TRANSCRIBE_COMPRESS", None)
        else:
            os.environ["LR_TRANSCRIBE_COMPRESS"] = cls._old_compress

    def test_init_requires_key(self):
        with self.assertRaises(LLMConfigError):
            TranscriptionProvider("")

    def test_transcribe_plain_text_response(self):
        capture = {}
        p = TranscriptionProvider(
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
        # JSON request with base64 input_audio.
        ct = capture["headers"].get("Content-Type", "")
        self.assertEqual(ct, "application/json")
        # The model is in the JSON body.
        self.assertIn(llm.TRANSCRIPTION_MODEL.encode("utf-8"), capture["data"])

    def test_transcribe_json_response_with_text(self):
        p = TranscriptionProvider(
            "k", http_transport=_fake(
                200, b'{"text": "the quick brown fox"}',
            ),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            text = p.transcribe(_write_wav(Path(d)))
        self.assertEqual(text, "the quick brown fox")

    def test_transcribe_sends_response_format_field(self):
        capture = {}
        p = TranscriptionProvider(
            "k", http_transport=_fake(200, b'{"text": "x"}', capture),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p.transcribe(_write_wav(Path(d)))
        self.assertIn(b'"response_format"', capture["data"])
        self.assertIn(b"verbose_json", capture["data"])
        self.assertIn(b'"input_audio"', capture["data"])

    def test_transcribe_sends_azure_diarization_by_default(self):
        capture = {}
        p = TranscriptionProvider(
            "k", http_transport=_fake(200, b'{"text": "x"}', capture),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p.transcribe(_write_wav(Path(d)))
        self.assertIn(b'"azure"', capture["data"])
        self.assertIn(b'"diarization"', capture["data"])
        self.assertIn(b'"enabled": true', capture["data"])

    def test_transcribe_provider_options_env_override(self):
        capture = {}
        env_name = "LR_TRANSCRIBE_PROVIDER_OPTIONS"
        old = os.environ.get(env_name)
        os.environ[env_name] = "{}"
        try:
            p = TranscriptionProvider(
                "k", http_transport=_fake(200, b'{"text": "x"}', capture),
            )
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as d:
                p.transcribe(_write_wav(Path(d)))
        finally:
            if old is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = old
        self.assertNotIn(b'"provider"', capture["data"])

    def test_transcribe_compress_disabled_sends_wav(self):
        capture = {}
        env_name = "LR_TRANSCRIBE_COMPRESS"
        old = os.environ.get(env_name)
        os.environ[env_name] = "0"
        try:
            p = TranscriptionProvider(
                "k", http_transport=_fake(200, b'{"text": "x"}', capture),
            )
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as d:
                p.transcribe(_write_wav(Path(d)))
        finally:
            if old is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = old
        self.assertIn(b'"format": "wav"', capture["data"])

    def test_transcribe_compress_falls_back_without_ffmpeg(self):
        """Compress on + ffmpeg unavailable -> falls back to the
        raw WAV chunk instead of failing."""
        capture = {}
        envs = ("LR_TRANSCRIBE_COMPRESS", "LR_FFMPEG_PATH")
        old = {e: os.environ.get(e) for e in envs}
        os.environ["LR_TRANSCRIBE_COMPRESS"] = "1"
        os.environ["LR_FFMPEG_PATH"] = "ffmpeg-nonexistent-test-binary"
        try:
            p = TranscriptionProvider(
                "k", http_transport=_fake(200, b'{"text": "x"}', capture),
            )
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as d:
                p.transcribe(_write_wav(Path(d)))
        finally:
            for e, v in old.items():
                if v is None:
                    os.environ.pop(e, None)
                else:
                    os.environ[e] = v
        self.assertIn(b'"format": "wav"', capture["data"])

    def test_split_plan_seconds_driven_with_mp3(self):
        """With compression + ffmpeg available, the byte cap is
        non-binding and the duration limit drives chunking."""
        p = TranscriptionProvider(
            "k", compress=True,
            ffmpeg_path="ffmpeg-nonexistent-test-binary",
        )
        # White-box: pretend the ffmpeg probe succeeded.
        p._ffmpeg_ok = True
        max_bytes, max_seconds = p._split_limits()
        self.assertEqual(max_bytes, 1024 ** 3)
        self.assertEqual(max_seconds, 1500)

    def test_split_plan_wav_caps_without_mp3(self):
        """Without compression, the conservative WAV caps apply and
        a non-explicit duration is clamped to the WAV-safe default."""
        p = TranscriptionProvider(
            "k", compress=False,
            ffmpeg_path="ffmpeg-nonexistent-test-binary",
        )
        max_bytes, max_seconds = p._split_limits()
        self.assertEqual(max_bytes, 15 * 1024 * 1024)
        self.assertEqual(max_seconds, 300)

    def test_transcribe_halves_chunk_on_transient_failure(self):
        """A chunk that fails with a transient upstream error is
        split in half and retried; the merged transcript keeps
        continuous timestamps."""
        import tempfile
        from pathlib import Path
        calls = []

        def transport(url, *, data=None, headers=None, timeout=60.0):
            calls.append(1)
            if len(calls) == 1:
                return (503, b"upstream overloaded")
            n = len(calls)
            body = json.dumps({
                "text": f"part {n}",
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": f"part {n}",
                     "speaker": 0},
                ],
            }).encode("utf-8")
            return (200, body)

        # 100 s of audio sent as a single 100 s chunk.
        p = TranscriptionProvider(
            "k", chunk_max_seconds=100,
            ffmpeg_path="ffmpeg-nonexistent-test-binary",
            http_transport=transport,
        )
        with tempfile.TemporaryDirectory() as d:
            audio = _write_real_wav(
                Path(d), rate=10, data=b"\x00\x01" * 1000,
            )
            text = p.transcribe(audio)
        self.assertEqual(len(calls), 3)  # 1 failed + 2 halves
        self.assertEqual(
            text,
            "[00:00] Speaker 1: part 2\n[00:50] Speaker 1: part 3",
        )

    def test_transcribe_does_not_halve_on_client_error(self):
        """Auth errors (401) are not retried by halving."""
        import tempfile
        from pathlib import Path
        calls = []

        def transport(url, *, data=None, headers=None, timeout=60.0):
            calls.append(1)
            return (401, b"unauthorized")

        p = TranscriptionProvider(
            "k", chunk_max_seconds=100,
            ffmpeg_path="ffmpeg-nonexistent-test-binary",
            http_transport=transport,
        )
        with tempfile.TemporaryDirectory() as d:
            audio = _write_real_wav(
                Path(d), rate=10, data=b"\x00\x01" * 1000,
            )
            with self.assertRaises(LLMHTTPError):
                p.transcribe(audio)
        self.assertEqual(len(calls), 1)

    def test_transcribe_halves_oversized_body_automatically(self):
        """A request body over the safety cap is never sent: the
        chunk is split in half and retried (previously this
        surfaced as OpenRouter's bare 413)."""
        import tempfile
        from pathlib import Path
        bodies = []

        def transport(url, *, data=None, headers=None, timeout=60.0):
            bodies.append(len(data or b""))
            n = len(bodies)
            return (200, json.dumps({
                "text": f"part {n}",
                "segments": [],
            }).encode("utf-8"))

        # 40 MB of audio as a single chunk -> ~53 MB body, over the
        # 45 MB cap -> split in half -> two ~27 MB bodies, fine.
        p = TranscriptionProvider(
            "k", chunk_max_bytes=512 * 1024 * 1024,
            chunk_max_seconds=10 ** 9,
            compress=False,
            http_transport=transport,
        )
        with tempfile.TemporaryDirectory() as d:
            audio = _write_real_wav(
                Path(d), data=b"\x00" * (40 * 1024 * 1024),
            )
            text = p.transcribe(audio)
        self.assertEqual(len(bodies), 2)
        self.assertTrue(all(b < 45 * 1024 * 1024 for b in bodies))
        self.assertEqual(text, "part 1\npart 2")

    def test_transcribe_verbose_json_with_speakers(self):
        body = json.dumps({
            "text": "Hello there. General Kanobi!",
            "segments": [
                {"id": 0, "start": 0.0, "end": 1.2,
                 "text": "Hello there.", "speaker": 0},
                {"id": 1, "start": 61.5, "end": 62.4,
                 "text": "General Kanobi!", "speaker": 1},
            ],
        }).encode("utf-8")
        p = TranscriptionProvider("k", http_transport=_fake(200, body))
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            text = p.transcribe(_write_wav(Path(d)))
        self.assertEqual(
            text,
            "[00:00] Speaker 1: Hello there.\n"
            "[01:01] Speaker 2: General Kanobi!",
        )

    def test_transcribe_verbose_json_uses_speaker_label(self):
        body = json.dumps({
            "segments": [
                {"id": 0, "start": 5.0, "end": 6.0,
                 "text": "Good morning.", "speaker_label": "Prof"},
            ],
        }).encode("utf-8")
        p = TranscriptionProvider("k", http_transport=_fake(200, body))
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            text = p.transcribe(_write_wav(Path(d)))
        self.assertEqual(text, "[00:05] Prof: Good morning.")

    def test_transcribe_verbose_json_without_speakers(self):
        body = json.dumps({
            "text": "Hello there. General Kanobi!",
            "segments": [
                {"id": 0, "start": 0.0, "end": 1.2, "text": "Hello there."},
                {"id": 1, "start": 1.5, "end": 2.4,
                 "text": "General Kanobi!"},
            ],
        }).encode("utf-8")
        p = TranscriptionProvider("k", http_transport=_fake(200, body))
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            text = p.transcribe(_write_wav(Path(d)))
        self.assertEqual(text, "Hello there.\nGeneral Kanobi!")

    def test_transcribe_speaker_annotation_disabled(self):
        body = json.dumps({
            "segments": [
                {"id": 0, "start": 0.0, "end": 1.2,
                 "text": "Hello there.", "speaker": 0},
            ],
        }).encode("utf-8")
        p = TranscriptionProvider(
            "k", annotate_speakers=False,
            http_transport=_fake(200, body),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            text = p.transcribe(_write_wav(Path(d)))
        self.assertEqual(text, "Hello there.")

    def test_transcribe_empty_segments_falls_back_to_text(self):
        body = json.dumps({
            "text": "fallback text",
            "segments": [],
        }).encode("utf-8")
        p = TranscriptionProvider("k", http_transport=_fake(200, body))
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            text = p.transcribe(_write_wav(Path(d)))
        self.assertEqual(text, "fallback text")

    def test_transcribe_chunks_long_recording(self):
        import tempfile
        from pathlib import Path
        calls = []

        def transport(url, *, data=None, headers=None, timeout=60.0):
            calls.append(json.loads(data.decode("utf-8")))
            n = len(calls)
            body = json.dumps({
                "text": f"part {n}",
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": f"part {n}",
                     "speaker": 0},
                ],
            }).encode("utf-8")
            return (200, body)

        # Default WAV: 50 frames @ 2 bytes/frame = 100 bytes of data.
        # chunk_max_bytes=40 -> 20 frames (2 s at rate 10) per chunk
        # -> 3 chunks.
        p = TranscriptionProvider(
            "k", chunk_max_bytes=40, http_transport=transport,
        )
        with tempfile.TemporaryDirectory() as d:
            text = p.transcribe(_write_real_wav(Path(d)))
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            text,
            "[00:00] Speaker 1: part 1\n"
            "[00:02] Speaker 1: part 2\n"
            "[00:04] Speaker 1: part 3",
        )

    def test_split_wav_offsets_and_frame_alignment(self):
        import tempfile
        from pathlib import Path
        from services.llm import _parse_wav, _split_wav
        with tempfile.TemporaryDirectory() as d:
            audio = _write_real_wav(Path(d))
            chunks = _split_wav(audio, 40, 300.0, Path(d))
            self.assertEqual(len(chunks), 3)
            self.assertEqual(
                [off for _, off in chunks], [0.0, 2.0, 4.0],
            )
            total = 0
            for chunk_path, _ in chunks:
                data = chunk_path.read_bytes()
                info = _parse_wav(data)
                self.assertIsNotNone(info)
                off, size, rate, channels, bits = info
                self.assertEqual(rate, 10)
                self.assertEqual(size % 2, 0)  # no torn frames
                total += size
            # Every frame is present exactly once across chunks.
            self.assertEqual(
                total, _parse_wav(audio.read_bytes())[1],
            )

    def test_split_wav_small_file_stays_whole(self):
        import tempfile
        from pathlib import Path
        from services.llm import _split_wav
        with tempfile.TemporaryDirectory() as d:
            audio = _write_real_wav(Path(d))
            chunks = _split_wav(audio, 1024 * 1024, 300.0, Path(d))
        self.assertEqual(chunks, [(audio, 0.0)])

    def test_split_wav_unparseable_falls_back_to_whole(self):
        import tempfile
        from pathlib import Path
        from services.llm import _split_wav
        with tempfile.TemporaryDirectory() as d:
            audio = _write_wav(Path(d))  # 8 junk bytes, not a WAV
            chunks = _split_wav(audio, 10, 300.0, Path(d))
        self.assertEqual(chunks, [(audio, 0.0)])

    def test_transcribe_4xx_raises_http_error(self):
        p = TranscriptionProvider(
            "k", http_transport=_fake(401, b"unauthorized"),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(LLMHTTPError) as ctx:
                p.transcribe(_write_wav(Path(d)))
        self.assertEqual(ctx.exception.status, 401)

    def test_transcribe_5xx_raises_http_error(self):
        p = TranscriptionProvider(
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
        p = TranscriptionProvider("k", http_transport=_fake(200, b"x"))
        with self.assertRaises(FileNotFoundError):
            p.transcribe(Path("/no/such/file.wav"))

    def test_classify_is_not_implemented(self):
        p = TranscriptionProvider("k", http_transport=_fake(200, b"x"))
        with self.assertRaises(LLMConfigError):
            p.classify("transcript text")

    def test_summarize_is_not_implemented(self):
        p = TranscriptionProvider("k", http_transport=_fake(200, b"x"))
        with self.assertRaises(LLMConfigError):
            p.summarize("text")


# --- Text provider -------------------------------------------------------


class TextProviderTests(unittest.TestCase):
    def test_init_requires_key(self):
        with self.assertRaises(LLMConfigError):
            TextProvider("")

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
            p = TextProvider("k", http_transport=_fake(200, body))
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
        p = TextProvider(
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
        self.assertEqual(sent["model"], llm.TEXT_MODEL)
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
        p = TextProvider("k", http_transport=_fake(200, body))
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
        p = TextProvider("k", http_transport=_fake(200, body))
        result = p.classify("x")
        self.assertEqual(result.kind, "voice_memo")

    def test_classify_4xx_raises_http_error(self):
        p = TextProvider("k", http_transport=_fake(429, b"rate limit"))
        with self.assertRaises(LLMHTTPError) as ctx:
            p.classify("x")
        self.assertEqual(ctx.exception.status, 429)

    def test_classify_malformed_response_raises_llm_error(self):
        body = json.dumps({
            "choices": [{"message": {"content": "not json at all"}}],
        }).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
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
        p = TextProvider("k", http_transport=_fake(200, body))
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
        p = TextProvider("k", http_transport=_fake(200, body))
        with self.assertRaises(LLMError):
            p.classify("x")

    def test_classify_missing_choices_raises_llm_error(self):
        body = json.dumps({"error": "rate limit"}).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
        with self.assertRaises(LLMError):
            p.classify("x")

    def test_classify_truncates_long_transcripts(self):
        capture = {}
        body = json.dumps({
            "choices": [{
                "message": {"content": '{"kind": "lecture", "confidence": 0.5}'},
            }],
        }).encode("utf-8")
        p = TextProvider(
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
        p = TextProvider("k", http_transport=_fake(200, b"x"))
        with self.assertRaises(LLMConfigError):
            p.transcribe("/tmp/x.wav")

    def test_summarize_returns_summary(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "text": "A meeting about the new design.",
                        "key_points": ["Point 1", "Point 2"],
                    }),
                },
            }],
        }).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
        out = p.summarize("a long transcript")
        self.assertEqual(out.text, "A meeting about the new design.")
        self.assertEqual(out.key_points, ["Point 1", "Point 2"])

    def test_summarize_handles_fenced_json(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": "```json\n{\"text\": \"hi\", \"key_points\": []}\n```",
                },
            }],
        }).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
        out = p.summarize("x")
        self.assertEqual(out.text, "hi")
        self.assertEqual(out.key_points, [])

    def test_summarize_raises_on_http_error(self):
        p = TextProvider("k", http_transport=_fake(503, b"down"))
        with self.assertRaises(LLMHTTPError):
            p.summarize("x")

    def test_summarize_raises_on_malformed_response(self):
        body = json.dumps({
            "choices": [{"message": {"content": "no json here"}}],
        }).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
        with self.assertRaises(LLMError):
            p.summarize("x")

    def test_summarize_rejects_non_dict_text(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({"text": 42, "key_points": []}),
                },
            }],
        }).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
        with self.assertRaises(LLMError):
            p.summarize("x")

    def test_extract_returns_items(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "items": [
                            {"type": "deadline", "text": "Ship by Friday"},
                            {"type": "topic", "text": "Auth flow"},
                        ],
                    }),
                },
            }],
        }).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
        out = p.extract("transcript text", kind="meeting")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["type"], "deadline")
        self.assertEqual(out[1]["type"], "topic")
        self.assertEqual(out[0]["text"], "Ship by Friday")

    def test_extract_handles_fenced_json(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": "```json\n{\"items\": [{\"type\": \"x\", \"text\": \"y\"}]}\n```",
                },
            }],
        }).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
        out = p.extract("x", kind="lecture")
        self.assertEqual(out, [{"type": "x", "text": "y"}])

    def test_extract_rejects_unknown_kind(self):
        p = TextProvider("k", http_transport=_fake(200, b"{}"))
        with self.assertRaises(LLMError):
            p.extract("x", kind="alien")

    def test_extract_raises_on_item_missing_keys(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "items": [{"type": "x"}],  # no 'text'
                    }),
                },
            }],
        }).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
        with self.assertRaises(LLMError):
            p.extract("x", kind="lecture")

    def test_extract_raises_on_http_error(self):
        p = TextProvider("k", http_transport=_fake(500, b"boom"))
        with self.assertRaises(LLMHTTPError):
            p.extract("x", kind="lecture")

    def test_extract_returns_empty_list_when_items_empty(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({"items": []}),
                },
            }],
        }).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
        self.assertEqual(p.extract("x", kind="meeting"), [])

    def test_summarize_rejects_non_string_transcript(self):
        p = TextProvider("k", http_transport=_fake(200, b"x"))
        with self.assertRaises(LLMError):
            p.summarize(None)  # type: ignore[arg-type]

    def test_extract_rejects_non_string_kind(self):
        p = TextProvider("k", http_transport=_fake(200, b"x"))
        with self.assertRaises(LLMError):
            p.extract("x", kind=None)  # type: ignore[arg-type]


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
            transcription_key="", text_key="",
        )
        self.assertEqual(providers["transcript"].name, "noop")
        self.assertEqual(providers["text"].name, "noop")

    def test_keys_return_real_providers(self):
        providers = llm.default_providers(
            transcription_key="k1", text_key="k2",
            http_transport=_fake(200, b"x"),
        )
        self.assertIsInstance(providers["transcript"], TranscriptionProvider)
        self.assertIsInstance(providers["text"], TextProvider)

    def test_get_provider_default(self):
        providers = llm.default_providers(
            transcription_key="", text_key="",
        )
        p = llm.get_provider("transcript")
        self.assertEqual(p.name, "noop")


if __name__ == "__main__":
    unittest.main()