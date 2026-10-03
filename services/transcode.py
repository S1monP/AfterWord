"""Audio transcoding using the ffmpeg binary.

The web upload path accepts arbitrary audio files and turns them into
the canonical 22050 Hz mono 32-bit WAV format the device uses. The
browser tries to decode and re-encode first (faster, no ffmpeg
required); when it can't (iPhone Voice Memos M4A is the common case),
the server does it here.

We use ffmpeg via subprocess rather than a Python binding for two
reasons: it supports every format under the sun without extra deps, and
the error surface is small and predictable.
"""
from __future__ import annotations

import logging
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


log = logging.getLogger("lr.transcode")


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
    kind: str            # 'wav' | 'mp3' | 'mp4' | 'ogg' | 'flac' | 'webm' | 'unknown'
    sample_rate: int = 0
    channels: int = 0
    bits_per_sample: int = 0


# ---------------------------------------------------------------------------
# Format sniff
# ---------------------------------------------------------------------------

def sniff_format(path: Path) -> AudioFormat:
    """Read a few bytes from ``path`` and report the audio format.

    Best-effort: returns ``kind='unknown'`` if nothing matches; never
    raises. The format is reliable for our purposes (we only use it to
    skip transcoding when the file is already the canonical WAV).
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return AudioFormat(kind="unknown")

    if len(head) < 4:
        return AudioFormat(kind="unknown")

    if head[0:4] == b"RIFF" and len(head) >= 12 and head[8:12] == b"WAVE":
        fmt = AudioFormat(kind="wav")
        if len(head) >= 36:
            try:
                fmt.channels = struct.unpack_from("<H", head, 22)[0]
                fmt.sample_rate = struct.unpack_from("<I", head, 24)[0]
                fmt.bits_per_sample = struct.unpack_from("<H", head, 34)[0]
            except struct.error:
                pass
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


def ffmpeg_version(ffmpeg_path: str = "ffmpeg") -> Optional[str]:
    """First line of ``ffmpeg -version`` if available, else None."""
    if not ffmpeg_available(ffmpeg_path):
        return None
    try:
        out = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode == 0:
            return out.stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def is_canonical_wav(fmt: AudioFormat) -> bool:
    """True if a sniffed WAV already matches the device format.

    The device records at 22050 Hz mono 32-bit. If the upload is already
    in that shape, we skip the ffmpeg round-trip.
    """
    return (
        fmt.kind == "wav"
        and fmt.sample_rate == 22050
        and fmt.channels == 1
        and fmt.bits_per_sample == 32
    )


# ---------------------------------------------------------------------------
# Transcode
# ---------------------------------------------------------------------------

def transcode_to_wav(
    src: Path,
    dst: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    sample_rate: int = 22050,
    channels: int = 1,
    bits_per_sample: int = 32,
    timeout_sec: int = 600,
) -> int:
    """Convert ``src`` to a canonical WAV at ``dst`` using ffmpeg.

    Returns the size of the produced WAV. Raises :class:`TranscodeError`
    on any failure with a stable ``reason`` code.
    """
    if not src.exists() or src.stat().st_size == 0:
        raise TranscodeError(TranscodeError.REASON_BAD_INPUT,
                             "empty or missing file")

    if not ffmpeg_available(ffmpeg_path):
        raise TranscodeError(
            TranscodeError.REASON_FFMPEG_MISSING,
            f"ffmpeg not found at {ffmpeg_path!r}; install ffmpeg or set "
            "LR_FFMPEG_PATH to a valid binary",
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            dst.unlink()
        except OSError:
            pass

    codec_map = {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}
    codec = codec_map.get(bits_per_sample)
    if codec is None:
        raise TranscodeError(
            TranscodeError.REASON_BAD_INPUT,
            f"unsupported bits_per_sample: {bits_per_sample}",
        )

    cmd = [
        ffmpeg_path,
        "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-c:a", codec,
        "-f", "wav",
        str(dst),
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_sec, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError(
            TranscodeError.REASON_FFMPEG_FAILED,
            f"ffmpeg timed out after {timeout_sec}s",
        ) from exc
    except OSError as exc:
        raise TranscodeError(
            TranscodeError.REASON_FFMPEG_MISSING, str(exc),
        ) from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else f"exit {proc.returncode}"
        log.warning("ffmpeg failed: %s", detail)
        raise TranscodeError(TranscodeError.REASON_FFMPEG_FAILED, detail)

    if not dst.exists() or dst.stat().st_size < 44:
        raise TranscodeError(
            TranscodeError.REASON_FFMPEG_FAILED,
            "ffmpeg produced no usable output",
        )

    return dst.stat().st_size


def ingest_audio_file(
    src: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    work_dir: Optional[Path] = None,
) -> tuple[Path, AudioFormat]:
    """Ensure ``src`` is a canonical WAV; transcode if not.

    Returns ``(wav_path, sniffed_format)`` where ``wav_path`` is the
    final canonical WAV. If transcoding was needed, the returned path
    is a new file in ``work_dir`` (or a temp dir); if not, it's ``src``
    itself. Caller cleans up the returned path if it's not ``src``.
    """
    fmt = sniff_format(src)
    if is_canonical_wav(fmt):
        return src, fmt

    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="lr-tx-"))
    work.mkdir(parents=True, exist_ok=True)
    dst = work / f"{src.stem}.canonical.wav"
    transcode_to_wav(src, dst, ffmpeg_path=ffmpeg_path)
    return dst, fmt
