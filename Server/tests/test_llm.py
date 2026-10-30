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
from tests.conftest import skip_without_ffmpeg

# Real ffmpeg when the host has one — the AAC segmenter/halving
# tests shell out to it.
import shutil as _shutil
FFMPEG_PATH = _shutil.which("ffmpeg") or "ffmpeg-nonexistent-test-binary"


def _write_aac(path_dir, seconds: float = 5.0, rate: int = 8000):
    """Build a real ADTS AAC file from a generated WAV (needs ffmpeg;
    skip the calling test when it isn't available)."""
    from pathlib import Path as _P
    from services import transcode as tx
    data = b"\x00\x01" * int(seconds * rate)
    wav = _write_real_wav(
        _P(path_dir), name="gen.wav", rate=rate, channels=1, bits=16,
        data=data,
    )
    aac = _P(path_dir) / "gen.aac"
    tx.transcode_to_aac(wav, aac, ffmpeg_path=FFMPEG_PATH)
    return aac


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

    @skip_without_ffmpeg
    def test_transcribe_sends_mp3(self):
        """Stored AAC is sent as MP3: Azure's MAI Transcribe rejects
        AAC (raw or in M4A) with a bare "Provider returned 400"."""
        capture = {}
        p = TranscriptionProvider(
            "k", ffmpeg_path=FFMPEG_PATH,
            http_transport=_fake(200, b'{"text": "x"}', capture),
        )
        import base64
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            audio = _write_aac(d, seconds=0.5)
            p.transcribe(audio)
        payload = json.loads(capture["data"])
        self.assertEqual(payload["input_audio"]["format"], "mp3")
        head = base64.b64decode(payload["input_audio"]["data"])[:3]
        self.assertTrue(head == b"ID3" or head[0] == 0xFF, head)

    @skip_without_ffmpeg
    def test_transcribe_leaves_no_files_next_to_the_recording(self):
        """A short recording is sent whole; its MP3 copy goes in the
        temp work dir, never into the session folder."""
        p = TranscriptionProvider(
            "k", ffmpeg_path=FFMPEG_PATH,
            http_transport=_fake(200, b'{"text": "x"}'),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            audio = _write_aac(d, seconds=0.5)
            before = sorted(Path(d).iterdir())
            p.transcribe(audio)
            self.assertEqual(sorted(Path(d).iterdir()), before)

    def test_transcribe_unsniffable_audio_defaults_to_aac(self):
        capture = {}
        p = TranscriptionProvider(
            "k", http_transport=_fake(200, b'{"text": "x"}', capture),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            audio = _write_wav(Path(d))  # 8 junk bytes
            p.transcribe(audio)
        self.assertIn(b'"format": "aac"', capture["data"])

    @skip_without_ffmpeg
    def test_segment_audio_short_stays_whole(self):
        import tempfile
        from pathlib import Path
        p = TranscriptionProvider("k", chunk_max_seconds=1500)
        with tempfile.TemporaryDirectory() as d:
            audio = _write_aac(d, seconds=1.0)
            pieces = p._segment_audio(audio, Path(d))
        self.assertEqual(pieces, [(audio, 0.0)])

    @skip_without_ffmpeg
    def test_segment_audio_long_recording(self):
        """A recording longer than the piece limit is segmented with
        stream copy into consecutive pieces with cumulative offsets."""
        import tempfile
        from pathlib import Path
        p = TranscriptionProvider(
            "k", chunk_max_seconds=2, ffmpeg_path=FFMPEG_PATH,
        )
        with tempfile.TemporaryDirectory() as d:
            audio = _write_aac(d, seconds=5.0)
            pieces = p._segment_audio(audio, Path(d))
            self.assertEqual(len(pieces), 3)
            offsets = [off for _, off in pieces]
            # Cumulative offsets land at 0 / ~2 / ~4.
            self.assertAlmostEqual(offsets[0], 0.0, places=0)
            self.assertAlmostEqual(offsets[1], 2.0, delta=0.5)
            self.assertAlmostEqual(offsets[2], 4.0, delta=0.6)
            # Pieces are real AAC.
            from services import transcode as tx
            for piece, _ in pieces:
                self.assertEqual(tx.sniff_format(piece).kind, "aac")

    @skip_without_ffmpeg
    def test_transcribe_chunks_long_recording(self):
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

        # 5 s of AAC, segmented at 2 s -> 3 pieces with continuous
        # timestamps after the offsets are applied.
        p = TranscriptionProvider(
            "k", chunk_max_seconds=2, ffmpeg_path=FFMPEG_PATH,
            http_transport=transport,
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            text = p.transcribe(_write_aac(d, seconds=5.0))
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            text,
            "[00:00] Speaker 1: part 1\n"
            "[00:02] Speaker 1: part 2\n"
            "[00:04] Speaker 1: part 3",
        )

    @skip_without_ffmpeg
    def test_transcribe_halves_piece_on_transient_failure(self):
        """A piece that fails with a transient upstream error is cut
        in half and retried; the merged transcript keeps continuous
        timestamps."""
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

        # ~50 s of AAC (silence at 64 kbps target) sent as a single
        # piece; the 90 s floor keeps the halves from being halved
        # again.
        p = TranscriptionProvider(
            "k", chunk_max_seconds=10 ** 9, ffmpeg_path=FFMPEG_PATH,
            http_transport=transport,
        )
        import tempfile
        from pathlib import Path
        from services import transcode as tx
        with tempfile.TemporaryDirectory() as d:
            audio = _write_aac(d, seconds=100.0)
            dur = tx.decode_duration(audio, ffmpeg_path=FFMPEG_PATH)
            self.assertIsNotNone(dur)
            half = dur / 2.0
            text = p.transcribe(audio)
        self.assertEqual(len(calls), 3)  # 1 failed + 2 halves
        self.assertEqual(
            text,
            f"[00:00] Speaker 1: part 2\n[00:{int(half):02d}] Speaker 1: part 3",
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
            "k", chunk_max_seconds=100, ffmpeg_path=FFMPEG_PATH,
            http_transport=transport,
        )
        with tempfile.TemporaryDirectory() as d:
            audio = _write_real_wav(
                Path(d), rate=10, data=b"\x00\x01" * 1000,
            )
            with self.assertRaises(LLMHTTPError):
                p.transcribe(audio)
        self.assertEqual(len(calls), 1)

    @skip_without_ffmpeg
    def test_transcribe_halves_oversized_body_automatically(self):
        """A request body over the safety cap is never sent: the
        piece is cut in half and retried (previously this surfaced
        as OpenRouter's bare 413)."""
        bodies = []

        def transport(url, *, data=None, headers=None, timeout=60.0):
            bodies.append(len(data or b""))
            n = len(bodies)
            return (200, json.dumps({
                "text": f"part {n}",
                "segments": [],
            }).encode("utf-8"))

        # 10 min of 8 kHz WAV sent whole -> ~2.4 MB of 32 kbps MP3,
        # ~3.2 MB as base64. With the cap lowered to 2.5 MB it is cut
        # in half -> two ~1.6 MB bodies, fine.
        from unittest import mock
        cap = int(2.5 * 1024 * 1024)
        p = TranscriptionProvider(
            "k", chunk_max_seconds=10 ** 9, ffmpeg_path=FFMPEG_PATH,
            http_transport=transport,
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(llm, "MAX_TRANSCRIBE_BODY_BYTES", cap):
            audio = _write_real_wav(
                Path(d), rate=8000, data=b"\x00" * (600 * 8000 * 2),
            )
            text = p.transcribe(audio)
        self.assertEqual(len(bodies), 2)
        self.assertTrue(all(b < cap for b in bodies))
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
        # JSON mode: MiniMax M3 wrote invalid lists without it.
        self.assertEqual(sent["response_format"], {"type": "json_object"})
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

    def test_summarize_parses_sections(self):
        body = json.dumps({"choices": [{"message": {"content": json.dumps({
            "text": "Overview.", "key_points": [],
            "sections": [
                {"heading": "Entropy", "text": " Notes. "},
                {"heading": "Empty", "text": ""},       # dropped
                "not an object",                          # dropped
                {"text": "No heading."},
            ],
        })}}]}).encode("utf-8")
        p = TextProvider("k", http_transport=_fake(200, body))
        out = p.summarize("x", kind="lecture")
        self.assertEqual(out.sections, [
            {"heading": "Entropy", "text": "Notes."},
            {"heading": "", "text": "No heading."},
        ])

    def test_summary_length_follows_transcript_and_kind(self):
        body = json.dumps({"choices": [{"message": {
            "content": '{"text": "t", "key_points": []}'}}]}).encode("utf-8")

        def system_prompt(transcript, kind):
            capture = {}
            TextProvider(
                "k", http_transport=_fake(200, body, capture),
            ).summarize(transcript, kind=kind)
            return json.loads(capture["data"])["messages"][0]["content"]

        short = system_prompt("hello there", "voice_memo")
        self.assertIn("'sections' is an empty list", short)
        self.assertIn("1-3 sentences", short)
        self.assertIn("personal voice memo", short)
        medium = system_prompt("word " * 2000, "lecture")
        self.assertIn("0-4 sections", medium)
        self.assertIn("Exam and assignment notes", medium)
        self.assertIn("Never pad", medium)

    def test_output_language_kept_or_english(self):
        from unittest import mock

        def run(detected_reply):
            # Reply to the language question, then to summary/extract.
            calls = []

            def transport(url, *, data=None, headers=None, timeout=60.0):
                sent = json.loads(data)
                calls.append(sent["messages"][0]["content"])
                content = (detected_reply if "main language" in calls[-1]
                           else '{"text": "t", "key_points": [], "items": []}')
                return 200, json.dumps({"choices": [{"message": {
                    "content": content}}]}).encode("utf-8")

            p = TextProvider("k", http_transport=transport)
            p.summarize("some transcript", kind="meeting")
            p.extract("some transcript", "meeting")
            return calls

        with mock.patch.dict(os.environ, {llm.SUMMARY_LANGUAGES_ENV: ""}):
            calls = run('{"language": "russian"}')
            # Asked once, then both prompts say Russian.
            self.assertEqual(len(calls), 3)
            self.assertIn("in Russian, translating", calls[1])
            self.assertIn("in Russian, translating", calls[2])
            calls = run('{"language": "German"}')
            self.assertIn("in English, translating", calls[1])
            calls = run("not json")                # detection failed
            self.assertIn("in English, translating", calls[1])
        env = {llm.SUMMARY_LANGUAGES_ENV: "German"}
        with mock.patch.dict(os.environ, env):
            calls = run('{"language": "Russian"}')
            # One language configured: no question asked.
            self.assertEqual(len(calls), 2)
            self.assertIn("in German, translating", calls[0])

    def test_long_transcript_notes_then_merges_sections(self):
        calls = []

        def transport(url, *, data=None, headers=None, timeout=60.0):
            sent = json.loads(data)
            calls.append(sent)
            if "Combine them" in sent["messages"][1]["content"]:
                content = {"text": "Whole lecture.", "key_points": ["k"],
                           "sections": [{"heading": "A", "text": "B"}]}
            else:
                content = {"heading": f"H{len(calls) - 1}",
                           "text": "Detailed notes.", "key_points": []}
            return 200, json.dumps({"choices": [{"message": {
                "content": json.dumps(content)}}]}).encode("utf-8")

        p = TextProvider("k", http_transport=transport)
        out = p.summarize("word " * 12_000, kind="lecture")   # 60k chars
        self.assertEqual(out.text, "Whole lecture.")
        self.assertEqual(out.sections, [{"heading": "A", "text": "B"}])
        detect, *notes, synthesis = calls
        self.assertIn("main language", detect["messages"][0]["content"])
        self.assertGreater(len(notes), 4)
        self.assertIn("80-150 words", notes[0]["messages"][0]["content"])
        # The longest band, and each part's heading reaches the merge.
        self.assertIn("5-10 sections", synthesis["messages"][0]["content"])
        self.assertIn("[Part 1: H1]", synthesis["messages"][1]["content"])

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

    def test_models_default_and_env_override(self):
        from unittest import mock
        env = {llm.TRANSCRIPTION_MODEL_ENV: "", llm.TEXT_MODEL_ENV: ""}
        with mock.patch.dict(os.environ, env):
            t = TranscriptionProvider("k")
            x = TextProvider("k")
        self.assertEqual(t.name, llm.TRANSCRIPTION_MODEL)
        self.assertEqual(x.name, llm.TEXT_MODEL)
        env = {llm.TRANSCRIPTION_MODEL_ENV: "openai/whisper-1",
               llm.TEXT_MODEL_ENV: "some/text-model"}
        capture = {}
        with mock.patch.dict(os.environ, env):
            t = TranscriptionProvider("k")
            x = TextProvider(
                "k", http_transport=_fake(200, b'{"choices": []}', capture),
            )
        # name is what artifacts record as the generating model.
        self.assertEqual(t.name, "openai/whisper-1")
        self.assertEqual(x.name, "some/text-model")
        try:
            x.classify("hello")
        except llm.LLMError:
            pass                    # empty choices; only the request matters
        self.assertEqual(json.loads(capture["data"])["model"], "some/text-model")

    def test_text_base_url_env_override(self):
        from unittest import mock
        body = json.dumps({"choices": [{"message": {"content":
            '<think>{"kind": "meeting"} maybe?</think>\n'
            '{"kind": "lecture", "confidence": 0.8}'}}]}).encode("utf-8")
        capture = {}
        env = {llm.TEXT_BASE_URL_ENV: "https://api.minimax.io/v1/"}
        with mock.patch.dict(os.environ, env):
            p = TextProvider("k", http_transport=_fake(200, body, capture))
        result = p.classify("x")
        self.assertEqual(
            capture["url"], "https://api.minimax.io/v1/chat/completions",
        )
        # The <think> block is dropped before the JSON is parsed.
        self.assertEqual(result.kind, "lecture")
        with mock.patch.dict(os.environ, {llm.TEXT_BASE_URL_ENV: ""}):
            p = TextProvider("k", http_transport=_fake(200, body, capture))
        p.classify("x")
        self.assertEqual(capture["url"], llm.OPENROUTER_CHAT_URL)

    def test_get_provider_default(self):
        providers = llm.default_providers(
            transcription_key="", text_key="",
        )
        p = llm.get_provider("transcript")
        self.assertEqual(p.name, "noop")


if __name__ == "__main__":
    unittest.main()