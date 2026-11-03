"""The course colour palette.

There are :data:`SLOTS` course colours. Every colour scheme draws them in
its own tones, for light and dark (``--cc1`` .. ``--cc12`` in
``static/css/tokens.css``), so they never clash with the scheme's accent
or status colours. A course stores the *number* of its colour ("3"),
never a hex value, so its colour follows scheme and theme changes.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

SLOTS = 12

# Each slot in the default scheme (Violet, refined; dark). Used only to
# map an old hex colour onto the nearest slot.
REFERENCE = {
    1: "#6aa7ff", 2: "#f08a6c", 3: "#3cc7b0", 4: "#e676a8",
    5: "#5abfd8", 6: "#a8d161", 7: "#b15ed4", 8: "#d8bf5a",
    9: "#66cc85", 10: "#d161bb", 11: "#be9b74", 12: "#8395af",
}

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def nearest_slot(hex_color: str) -> int:
    """The slot whose colour is closest to ``hex_color``."""
    r, g, b = _rgb(hex_color)

    def distance(slot: int) -> int:
        sr, sg, sb = _rgb(REFERENCE[slot])
        return (r - sr) ** 2 + (g - sg) ** 2 + (b - sb) ** 2

    return min(REFERENCE, key=distance)


def to_slot(color: object) -> str | None:
    """A colour given by a caller -> the stored form ("1".."12"), or
    None for "no colour". Accepts a slot number (int or digits) or an
    old-style hex colour, which maps to the nearest slot. Raises
    ``ValueError`` for anything else."""
    if color is None or color == "":
        return None
    if isinstance(color, bool):
        raise ValueError("color must be a palette slot 1-12")
    if isinstance(color, int) or (isinstance(color, str) and color.isdigit()):
        slot = int(color)
        if 1 <= slot <= SLOTS:
            return str(slot)
        raise ValueError(f"color must be a palette slot 1-{SLOTS}; got {color!r}")
    if isinstance(color, str) and _HEX_RE.match(color):
        return str(nearest_slot(color))
    raise ValueError(f"color must be a palette slot 1-{SLOTS}; got {color!r}")


def next_free(used: Iterable[str | None]) -> str:
    """The first slot no course uses yet (cycling once all are taken)."""
    taken = [u for u in used if u]
    for slot in range(1, SLOTS + 1):
        if str(slot) not in taken:
            return str(slot)
    return str(len(taken) % SLOTS + 1)
