"""Full-text search over session artifacts.

Phase A: stub. Returns an empty list. The :class:`artifacts_fts` table
exists in the schema; a later phase will populate it from the
``artifacts`` table and rewrite this module to query it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class SearchHit:
    session_id: str
    artifact_id: str
    kind: str
    snippet: str


def search(
    query: str,
    *,
    course_id: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 20,
) -> list[SearchHit]:
    """Return matching artifacts for ``query``.

    Phase A: always returns ``[]``. Wired up against ``artifacts_fts`` in
    a later phase. The signature is part of the public contract and
    should not change.
    """
    return []
