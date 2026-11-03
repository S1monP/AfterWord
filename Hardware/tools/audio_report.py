"""Level, clipping, bandwidth and noise floor of a recording.

usage: audio_report.py FILE [FILE ...]

Pass a session's chunks in order (1.aac 2.aac ...) to analyse them as one
recording; any format ffmpeg reads works.  Reports:
  * loudness per minute and where clipping happens (mm:ss), to line up
    with button presses in the device log
  * average spectrum of the louder half, per 1 kHz band (how much treble
    there is; 22.05 kHz audio stops at 11 kHz)
  * noise floor of the quietest seconds and its spectrum (hum vs. hiss)

For a true noise floor, record a minute with nothing playing and hands
off the board.  Needs ffmpeg on PATH (or AFTERWORD_FFMPEG_PATH) and numpy:
    uv run --no-project --with numpy python audio_report.py 1.aac 2.aac
"""
import os
import subprocess
import sys

import numpy as np

SR = 22050
N = 4096
FFMPEG = os.environ.get("AFTERWORD_FFMPEG_PATH", "ffmpeg")


def decode(paths):
    parts = []
    for p in paths:
        raw = subprocess.run([FFMPEG, "-v", "error", "-i", p, "-f", "s16le",
                              "-ac", "1", "-ar", str(SR), "-"],
                             capture_output=True, check=True).stdout
        parts.append(np.frombuffer(raw, dtype="<i2"))
    return np.concatenate(parts).astype(np.float64) / 32768.0


def db(x):
    return 20 * np.log10(np.maximum(x, 1e-9))


def band_spectrum(frames):
    spec = (np.abs(np.fft.rfft(frames * np.hanning(N), axis=1)) ** 2).mean(axis=0)
    return np.fft.rfftfreq(N, 1 / SR), 10 * np.log10(spec + 1e-20)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    x = decode(sys.argv[1:])
    secs = len(x) // SR
    sec = x[: secs * SR].reshape(secs, SR)
    srms = np.sqrt((sec ** 2).mean(axis=1))
    print(f"{secs // 60}:{secs % 60:02d} of audio, overall RMS "
          f"{db(np.sqrt((x ** 2).mean())):.1f} dBFS, peak {db(np.abs(x).max()):.1f} dBFS")

    print("\nloudness per minute (median of 1 s RMS):")
    for m in range(0, secs, 60):
        print(f"  {m // 60}:00  {db(np.median(srms[m:m + 60])):6.1f} dBFS")

    clip = (np.abs(sec) >= 32700 / 32768).sum(axis=1)
    bad = np.nonzero(clip)[0]
    print(f"\nclipping in {len(bad)} of {secs} seconds"
          + (":" if len(bad) else ""))
    runs, start = [], None
    for i, s in enumerate(bad):
        if start is None:
            start = s
        if i == len(bad) - 1 or bad[i + 1] != s + 1:
            runs.append((start, s, int(clip[start:s + 1].sum())))
            start = None
    for a, b, n in runs[:30]:
        print(f"  {a // 60}:{a % 60:02d}-{b // 60}:{b % 60:02d}  {n} samples")

    frames = np.lib.stride_tricks.sliding_window_view(x, N)[:: N // 2]
    frms = np.sqrt((frames ** 2).mean(axis=1))
    freqs, spec = band_spectrum(frames[frms > np.percentile(frms, 60)])
    ref = spec[(freqs > 300) & (freqs < 3000)].mean()
    print("\nspectrum of the louder half (dB vs. the 0.3-3 kHz speech band):")
    for lo in range(0, 11000, 1000):
        band = (freqs >= lo) & (freqs < lo + 1000)
        print(f"  {lo / 1000:4.0f}-{(lo + 1000) / 1000:<3.0f} kHz  {spec[band].mean() - ref:6.1f} dB")

    quiet = np.argsort(srms)[:10]
    print(f"\nnoise floor (10 quietest seconds): {db(srms[quiet].mean()):.1f} dBFS, at "
          + ", ".join(f"{i // 60}:{i % 60:02d}" for i in sorted(quiet)))
    q = np.concatenate([sec[i] for i in quiet])
    freqs, qspec = band_spectrum(np.lib.stride_tricks.sliding_window_view(q, N)[:: N // 2])
    qref = qspec[(freqs > 1000) & (freqs < 3000)].mean()
    print("noise spectrum (dB vs. its own 1-3 kHz level; high below 300 Hz = hum/rumble):")
    for lo, hi in [(0, 100), (100, 300), (300, 1000), (1000, 3000),
                   (3000, 6000), (6000, 9000), (9000, 11025)]:
        band = (freqs >= lo) & (freqs < hi)
        print(f"  {lo:5d}-{hi:<5d} Hz  {qspec[band].mean() - qref:6.1f} dB")


if __name__ == "__main__":
    main()
