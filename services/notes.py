"""Notes service: independent of recordings, markdown-rendered.

Tier 1: stub. All public methods raise :class:`NotImplementedError`. The
shape of the API is part of the contract that the future implementation
will follow, so adding it now means a stable surface for the routes
and templates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Note:
    id: str
    title: str
    body_md: str
    body_html: Optional[str]
    source: str               # 'manual' | 'extracted' | 'imported'
    source_ref: Optional[str]
    pinned: bool
    created_at: str
    updated_at: str


class NotesService:
    def create(self, *, title: str, body_md: str,
               source: str = "manual",
               source_ref: Optional[str] = None) -> Note:
        raise NotImplementedError

    def update(self, note_id: str, *,
               title: Optional[str] = None,
               body_md: Optional[str] = None,
               pinned: Optional[bool] = None) -> Note:
        raise NotImplementedError

    def delete(self, note_id: str) -> None:
        raise NotImplementedError

    def get(self, note_id: str) -> Optional[Note]:
        raise NotImplementedError

    def list(self, *, limit: int = 50, offset: int = 0,
             search: Optional[str] = None) -> list[Note]:
        raise NotImplementedError

    def render_html(self, body_md: str) -> str:
        """Render markdown to HTML. Returns a sanitised HTML string."""
        raise NotImplementedError
