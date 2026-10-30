"""Check the seam between two chunks the way the server stitches them.

usage: check_join.py A.aac B.aac

Joins A+B (plain byte concatenation, as the server does), decodes the
result with ffmpeg and looks at the audio right at the seam:
  * samples: joined length must equal A + B decoded separately (nothing
    lost or duplicated at the seam)
  * level per 1024-sample frame around the seam (a gap shows as a dip)
  * largest sample-to-sample jump at the seam vs. the whole file (a click)

Needs ffmpeg on PATH (or LR_FFMPEG_PATH).  Standard library only.
"""
import array
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

SR = 22050
N = 1024
FFMPEG = os.environ.get("LR_FFMPEG_PATH", "ffmpeg")


def decode(path):
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "s16le",
                          "-ac", "1", "-ar", str(SR), "-"],
                         capture_output=True, check=True).stdout
    pcm = array.array("h")
    pcm.frombytes(raw)
    return pcm


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    a, b = Path(sys.argv[1]), Path(sys.argv[2])
    with tempfile.TemporaryDirectory() as d:
        joined = Path(d) / "joined.aac"
        joined.write_bytes(a.read_bytes() + b.read_bytes())
        pcm = decode(joined)
    pcm_a, pcm_b = decode(a), decode(b)
    seam = len(pcm_a)

    print(f"A: {len(pcm_a)} samples ({len(pcm_a)/SR:.3f} s), "
          f"B: {len(pcm_b)}, joined: {len(pcm)}")
    lost = len(pcm_a) + len(pcm_b) - len(pcm)
    print("samples at the seam: OK (none lost or duplicated)" if lost == 0
          else f"samples at the seam: {lost:+d} vs. A+B decoded separately")

    def rms(lo, hi):
        seg = pcm[lo:hi]
        return (sum(x * x for x in seg) / len(seg)) ** 0.5 if seg else 0.0

    def max_jump(lo, hi):
        return max(abs(pcm[i] - pcm[i - 1]) for i in range(max(lo, 1), hi))

    print("\nlevel per frame around the seam (frame 0 starts at the seam):")
    for k in range(-4, 5):
        lo = seam + k * N
        print(f"  frame {k:+d}: RMS {rms(lo, lo + N):7.1f}   "
              f"max jump {max_jump(lo, lo + N):6d}")

    jumps = [max_jump(i, i + N) for i in range(N, len(pcm) - N, N)]
    print(f"\nmax jump within +-64 samples of the seam: "
          f"{max_jump(seam - 64, seam + 64)}")
    print(f"max jump per frame over the file: median "
          f"{statistics.median(jumps):.0f}, 95th percentile "
          f"{sorted(jumps)[int(len(jumps) * 0.95)]}")
    print("A seam jump well inside the file's usual range means no click.")


if __name__ == "__main__":
    main()
