"""Audio transcoding and probing using the ffmpeg/ffprobe binaries.

The canonical storage format is **AAC-LC** (~64 kbps, 22050 Hz mono):
the device encodes its WAV chunks to AAC before upload, web uploads
are transcoded to AAC at ingest, and stitching is a plain binary
concatenation of ADTS AAC chunks. AAC keeps request bodies ~5x
smaller than WAV (well under OpenRouter's upload caps), plays in
every browser, and is accepted natively by the transcription API.

We use ffmpeg via subprocess rather than a Python binding for two
reasons: it supports every format under the sun without extra deps, and
the error surface is small and predictable.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Canonical AAC encoding parameters (speech-grade, transparent for
#: ASR; ~2.4 MB per 5-minute chunk instead of ~13 MB of WAV).
AAC_SAMPLE_RATE = 22050
AAC_CHANNELS = 1
AAC_BITRATE = "64k"

#: Audio sent to the transcription API (see :func:`transcode_for_stt`).
STT_SAMPLE_RATE = 16000
STT_BITRATE = "32k"


class TranscodeError(Exception):
    """Raised when transcoding fails for any reason.

    ``reason`` is one of a small set of stable codes so the API layer
    can map it to an HTTP response without parsing strings.
    """

    REASON_FFMPEG_MISSING = "ffmpeg_missing"
    REASON_FFMPEG_FAILED = "ffmpeg_failed"
    REASON_BAD_INPUT = "bad_input"
    REASON_IO = "io_error"

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class AudioFormat:
    """Result of sniffing the first few bytes of a file."""

    kind: str  # 'aac' | 'wav' | 'mp3' | 'mp4' | 'ogg' | 'flac' | 'webm' | 'unknown'
    sample_rate: int = 0
    channels: int = 0
    bits_per_sample: int = 0


# ---------------------------------------------------------------------------
# Format sniff
# ---------------------------------------------------------------------------


def sniff_format(path: Path) -> AudioFormat:
    """Read a few bytes from ``path`` and report the audio format.

    Best-effort: returns ``kind='unknown'`` if nothing matches; never
    raises. Used by ingest (skip transcoding when the file is already
    canonical AAC) and by stitching (consistency check).
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return AudioFormat(kind="unknown")

    if len(head) < 4:
        return AudioFormat(kind="unknown")

    # ADTS AAC: syncword 0xFFFx (12-bit sync + MPEG-4 layer bits).
    # Must be checked BEFORE the generic MPEG-audio fallback below,
    # which would otherwise misclassify the same bytes as MP3.
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0:
        return AudioFormat(kind="aac")

    if head[0:4] == b"RIFF" and len(head) >= 12 and head[8:12] == b"WAVE":
        fmt = AudioFormat(kind="wav")
        # Walk chunks to find the fmt chunk instead of trusting fixed
        # offsets, so we correctly read files that have LIST/JUNK/INFO
        # chunks between RIFF and data (which ffmpeg 9.x produces).
        pos = 12
        while pos + 8 <= len(head):
            chunk_id = head[pos : pos + 4]
            if chunk_id == b"fmt ":
                size = struct.unpack_from("<I", head, pos + 4)[0]
                if size >= 16 and pos + 8 + 16 <= len(head):
                    try:
                        fmt.channels = struct.unpack_from("<H", head, pos + 8 + 2)[0]
                        fmt.sample_rate = struct.unpack_from("<I", head, pos + 8 + 4)[0]
                        fmt.bits_per_sample = struct.unpack_from("<H", head, pos + 8 + 14)[0]
                    except struct.error:
                        pass
                break
            size = struct.unpack_from("<I", head, pos + 4)[0]
            pos += 8 + size + (size & 1)
        return fmt

    if len(head) >= 8 and head[4:8] == b"ftyp":
        return AudioFormat(kind="mp4")

    if head[0:4] == b"OggS":
        return AudioFormat(kind="ogg")

    if head[0:4] == b"fLaC":
        return AudioFormat(kind="flac")

    if head[0:4] == b"\x1a\x45\xdf\xa3":
        return AudioFormat(kind="webm")

    if head[0:3] == b"ID3":
        return AudioFormat(kind="mp3")
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return AudioFormat(kind="mp3")

    return AudioFormat(kind="unknown")


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------


def ffmpeg_available(ffmpeg_path: str = "ffmpeg") -> bool:
    """True if the ffmpeg binary can be found and run."""
    return shutil.which(ffmpeg_path) is not None or os.path.isfile(ffmpeg_path)


def ffmpeg_version(ffmpeg_path: str = "ffmpeg") -> str | None:
    """First line of ``ffmpeg -version`` if available, else None."""
    if not ffmpeg_available(ffmpeg_path):
        return None
    try:
        out = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def is_canonical_aac(fmt: AudioFormat) -> bool:
    """True if the sniffed file is already AAC (the canonical
    storage format) — used to skip the ffmpeg round-trip at ingest.
    """
    return fmt.kind == "aac"


def _ffprobe_path(ffmpeg_path: str) -> str:
    """Derive the ffprobe binary path from the ffmpeg path.

    Handles ``ffmpeg``, ``ffmpeg.exe``, and absolute paths.
    """
    name = Path(ffmpeg_path).name
    if "ffprobe" in name:
        return ffmpeg_path
    probe_name = name.replace("ffmpeg", "ffprobe")
    return str(Path(ffmpeg_path).with_name(probe_name))


def ffprobe_available(ffmpeg_path: str = "ffmpeg") -> bool:
    """True if the ffprobe binary can be found and run."""
    probe = _ffprobe_path(ffmpeg_path)
    return shutil.which(probe) is not None or os.path.isfile(probe)


def probe_duration(
    path: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    timeout_sec: int = 60,
) -> float | None:
    """Duration of an audio file in seconds via ffprobe.

    Returns ``None`` when ffprobe is unavailable or the file can't
    be probed — callers must treat that as "unknown", not zero.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    probe = _ffprobe_path(ffmpeg_path)
    if not ffprobe_available(ffmpeg_path):
        return None
    cmd = [
        probe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def decode_duration(
    path: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    timeout_sec: int = 600,
) -> float | None:
    """Exact duration of an audio file in seconds by decoding it.

    ADTS AAC has no container duration; ffprobe *estimates* it from
    the bitrate, which drifts badly for quiet audio (silence encodes
    far below the target bitrate). Decoding the whole file and
    reading the last progress ``time=`` is always accurate, at the
    cost of a full decode (fast for compressed audio).

    Returns ``None`` when ffmpeg is unavailable or the file can't
    be decoded.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    if not ffmpeg_available(ffmpeg_path):
        return None
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # With -progress pipe:1, structured progress goes to stdout
    # regardless of loglevel; the final row for a stream is
    # "out_time=00:25:07.551000" (also out_time_us/out_time_ms).
    matches = re.findall(r"out_time=(\d+):(\d+):(\d+\.\d+)", proc.stdout or "")
    if not matches:
        return None
    h, m, s = matches[-1]
    try:
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Transcode
# ---------------------------------------------------------------------------


def _encode(
    src: Path,
    dst: Path,
    codec_args: list[str],
    expect_kind: str,
    *,
    ffmpeg_path: str,
    timeout_sec: int,
) -> int:
    """Run ffmpeg ``src`` -> ``dst`` with ``codec_args`` and check
    the result sniffs as ``expect_kind``. Returns the output size;
    raises :class:`TranscodeError` with a stable ``reason`` code."""
    if not src.exists() or src.stat().st_size == 0:
        raise TranscodeError(TranscodeError.REASON_BAD_INPUT, "empty or missing file")

    if not ffmpeg_available(ffmpeg_path):
        raise TranscodeError(
            TranscodeError.REASON_FFMPEG_MISSING,
            f"ffmpeg not found at {ffmpeg_path!r}; install ffmpeg or set "
            "AFTERWORD_FFMPEG_PATH to a valid binary",
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            dst.unlink()
        except OSError:
            pass

    cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        *codec_args,
        str(dst),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError(
            TranscodeError.REASON_FFMPEG_FAILED,
            f"ffmpeg timed out after {timeout_sec}s",
        ) from exc
    except OSError as exc:
        raise TranscodeError(
            TranscodeError.REASON_FFMPEG_MISSING,
            str(exc),
        ) from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else f"exit {proc.returncode}"
        log.warning("ffmpeg failed: %s", detail)
        raise TranscodeError(TranscodeError.REASON_FFMPEG_FAILED, detail)

    if not dst.exists() or dst.stat().st_size < 8:
        raise TranscodeError(
            TranscodeError.REASON_FFMPEG_FAILED,
            "ffmpeg produced no usable output",
        )

    if sniff_format(dst).kind != expect_kind:
        raise TranscodeError(
            TranscodeError.REASON_FFMPEG_FAILED,
            f"ffmpeg output is not {expect_kind.upper()}",
        )

    return dst.stat().st_size


def transcode_to_aac(
    src: Path,
    dst: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    sample_rate: int = AAC_SAMPLE_RATE,
    channels: int = AAC_CHANNELS,
    bitrate: str = AAC_BITRATE,
    timeout_sec: int = 600,
) -> int:
    """Convert ``src`` to canonical AAC-LC (ADTS) at ``dst``.

    Returns the size of the produced file. Raises
    :class:`TranscodeError` on any failure with a stable ``reason``
    code.
    """
    return _encode(
        src,
        dst,
        [
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-c:a",
            "aac",
            "-b:a",
            bitrate,
            "-f",
            "adts",
        ],
        "aac",
        ffmpeg_path=ffmpeg_path,
        timeout_sec=timeout_sec,
    )


def transcode_for_stt(
    src: Path,
    dst: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    timeout_sec: int = 600,
) -> int:
    """Encode ``src`` as the MP3 sent to the transcription API.

    Azure's MAI Transcribe (behind OpenRouter) rejects AAC, raw ADTS
    or in M4A, with a bare "Provider returned 400"; MP3 is accepted.
    16 kHz mono is what speech models work at, and 32 kbps keeps a
    25-minute piece near 6 MB, well under the 25 MB inline cap.
    """
    return _encode(
        src,
        dst,
        [
            "-ar",
            str(STT_SAMPLE_RATE),
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            STT_BITRATE,
            "-f",
            "mp3",
        ],
        "mp3",
        ffmpeg_path=ffmpeg_path,
        timeout_sec=timeout_sec,
    )


def ingest_audio_file(
    src: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    work_dir: Path | None = None,
) -> tuple[Path, AudioFormat]:
    """Ensure ``src`` is canonical AAC; transcode if not.

    Returns ``(aac_path, sniffed_format)`` where ``aac_path`` is the
    final canonical AAC file. If transcoding was needed, the returned
    path is a new file in ``work_dir`` (or a temp dir); if not, it's
    ``src`` itself. Caller cleans up the returned path if it's not
    ``src``.
    """
    fmt = sniff_format(src)
    if is_canonical_aac(fmt):
        return src, fmt

    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="afterword-tx-"))
    work.mkdir(parents=True, exist_ok=True)
    dst = work / f"{src.stem}.aac"
    transcode_to_aac(src, dst, ffmpeg_path=ffmpeg_path)
    return dst, fmt
