"""Search across recordings (names, summaries, transcripts), notes,
to-dos and deadlines, and calendar events.

Each item is split into short pieces (a title, a summary paragraph, one
transcript line, a line of a note) and every piece is indexed with its
words and the words' stems (snowball: Russian rules for Cyrillic,
English otherwise), so "лекция" also finds "лекции" and "meeting" finds
"meetings". The last word of a query is the one being typed and also
matches longer words ("лекц", "meetin"); so does a stem of 5+ letters,
because snowball does not cut every form alike ("экзамены" -> "экзам",
"экзаменов" -> "экзамен").

The index lives in ``search_rows`` / ``search_fts`` (storage.py) and is
brought up to date before every search: items whose stamp changed are
rebuilt, deleted ones dropped (:func:`sync`).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional, Union

import snowballstemmer

import storage
from services.llm import parse_transcript_lines

ITEM_TYPES = ("recording", "note", "task", "event")

# Recordings that are only a request ("remind me to ...") are not shown
# anywhere but /recordings (services/filing.py), so not in search either.
# A voice memo's text is searched as its note (Notes page).
_HIDDEN_KINDS = {"command", "voice_memo"}

# Most rows one search reads from the index.
_MAX_ROWS = 5000

_WORD = re.compile(r"[^\W_]+")
_CYRILLIC = re.compile("[Ѐ-ӿ]")
_STEMMERS = threading.local()

# Dropped from a query that has other words: they are in nearly every
# line, and a search matches words within one piece.
_STOP_WORDS = frozenset("""
    a an and are as at be by for from in is it of on or that the this to
    was with
    а в во да для до же за и из к как ли на не но о об от по с со то у
    что это
""".split())


def _norm(word: str) -> str:
    return word.lower().replace("ё", "е")


@lru_cache(maxsize=100_000)
def _stem(word: str) -> str:
    """Stem of a normalised word; numbers stay as they are."""
    if word.isdigit():
        return word
    lang = "russian" if _CYRILLIC.search(word) else "english"
    stemmer = getattr(_STEMMERS, lang, None)
    if stemmer is None:  # the stemmers keep state while working
        stemmer = snowballstemmer.stemmer(lang)
        setattr(_STEMMERS, lang, stemmer)
    return stemmer.stemWord(word) or word


# ---------------------------------------------------------------------------
# Building the index
# ---------------------------------------------------------------------------

Row = tuple[str, Optional[int], str, str, str]


def _row(field_: str, at_ms: Optional[int], text: str) -> Optional[Row]:
    """``(field, at_ms, text, words, stems)``, or None without words."""
    text = text.strip()
    words = [_norm(w) for w in _WORD.findall(text)]
    if not words:
        return None
    return (field_, at_ms, text, " ".join(words),
            " ".join(_stem(w) for w in words))


_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
# Markdown marks at the start of a line ("## ", "- ", "> ", "1. ") and
# emphasis, so snippets read as plain text.
_MD_LEAD = re.compile(r"^\s*(?:[#>*+-]+|\d+[.)])\s+")
_MD_EMPHASIS = re.compile(r"\*\*|__|`")


def _paragraphs(text: Optional[str]) -> list[str]:
    return [p.strip() for p in _PARAGRAPH_BREAK.split(text or "") if p.strip()]


def _markdown_lines(text: Optional[str]) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        line = _MD_EMPHASIS.sub("", _MD_LEAD.sub("", line)).strip()
        if line:
            out.append(line)
    return out


def _summary_texts(artifact: Optional[dict]) -> list[str]:
    """The summary's overview paragraphs, section headings and texts,
    and key points (pipeline payload; plain text in old summaries)."""
    content = (artifact or {}).get("content") or ""
    try:
        payload = json.loads(content)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return _paragraphs(content)
    texts = _paragraphs(str(payload.get("text") or ""))
    for sec in payload.get("sections") or []:
        if isinstance(sec, dict):
            texts.append(str(sec.get("heading") or ""))
            texts += _paragraphs(str(sec.get("text") or ""))
    texts += [str(p) for p in payload.get("key_points") or []]
    return texts


def _recording_rows(store: storage.Storage, session_id: str) -> Optional[list]:
    sess = store.get_session(session_id)
    if sess is None:
        return None
    if sess.kind in _HIDDEN_KINDS:
        return []
    rows = []
    # The device's folder name is not a name (web.common.rec_title).
    if sess.title and not (
        sess.title_source is None and storage.title_is_placeholder(sess)
    ):
        rows.append(_row("title", None, sess.title))
    for text in _summary_texts(store.get_artifact(session_id, "summary")):
        rows.append(_row("summary", None, text))
    transcript = store.get_artifact(session_id, "transcript")
    for line in parse_transcript_lines((transcript or {}).get("content")):
        at_ms = line["t"] * 1000 if line["t"] is not None else None
        rows.append(_row("transcript", at_ms, line["text"]))
    return rows


def _note_rows(store: storage.Storage, note_id: str) -> Optional[list]:
    note = store.get_note(note_id)
    if note is None:
        return None
    return [_row("title", None, note.title)] + [
        _row("body", None, line) for line in _markdown_lines(note.body_md)
    ]


def _task_rows(store: storage.Storage, task_id: str) -> Optional[list]:
    task = store.get_task(task_id)
    if task is None:
        return None
    return [_row("title", None, task.title)] + [
        _row("body", None, line)
        for line in _markdown_lines(task.description) + _markdown_lines(task.body_md)
    ]


def _event_rows(store: storage.Storage, event_id: str) -> Optional[list]:
    event = store.get_event(event_id)
    if event is None:
        return None
    return [_row("title", None, event.title),
            _row("body", None, event.location or "")] + [
        _row("body", None, line) for line in _markdown_lines(event.description)
    ]


_ROW_BUILDERS = {
    "recording": _recording_rows,
    "note": _note_rows,
    "task": _task_rows,
    "event": _event_rows,
}

_SYNC_LOCK = threading.Lock()


def sync(store: storage.Storage) -> int:
    """Bring the index up to date. Returns how many items changed.

    Cheap when nothing changed (it compares stamps); the first search
    after a recording is processed indexes its transcript."""
    with _SYNC_LOCK:
        current = {key: hashlib.blake2b(stamp.encode(), digest_size=16).hexdigest()
                   for key, stamp in store.search_source_stamps().items()}
        indexed = store.search_indexed_stamps()
        changed = 0
        for key in indexed.keys() - current.keys():
            store.search_drop_item(*key)
            changed += 1
        for key, stamp in current.items():
            if indexed.get(key) == stamp:
                continue
            rows = _ROW_BUILDERS[key[0]](store, key[1])
            if rows is None:  # deleted meanwhile
                store.search_drop_item(*key)
            else:
                store.search_put_item(*key, stamp, [r for r in rows if r])
            changed += 1
        return changed


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

# Stems this long also match longer stems (see the module docstring).
_STEM_PREFIX_MIN = 5


@dataclass(frozen=True)
class Term:
    word: str      # normalised
    stem: str
    prefix: bool   # the word being typed: longer words match too

    @property
    def stem_prefix(self) -> bool:
        return self.prefix or len(self.stem) >= _STEM_PREFIX_MIN

    def matches(self, word: str, stem: str) -> bool:
        if self.prefix and word.startswith(self.word):
            return True
        if self.stem_prefix:
            return stem.startswith(self.stem)
        return stem == self.stem


@dataclass(frozen=True)
class Query:
    terms: tuple[Term, ...]

    def __bool__(self) -> bool:
        return bool(self.terms)

    def fts(self) -> str:
        """The FTS5 MATCH expression: every term in the same piece."""
        parts = []
        for t in self.terms:
            stem = f'stems : "{t.stem}"' + ("*" if t.stem_prefix else "")
            parts.append(f'({stem} OR words : "{t.word}"*)' if t.prefix else stem)
        return " AND ".join(parts)


def parse_query(text: str) -> Query:
    """Terms of a query; empty when there is nothing to search for (no
    words, or a single letter)."""
    words = [_norm(w) for w in _WORD.findall(text or "")]
    if not words or (len(words) == 1 and len(words[0]) < 2):
        return Query(())
    terms = [Term(w, _stem(w), i == len(words) - 1)
             for i, w in enumerate(words)]
    kept = [t for t in terms if t.word not in _STOP_WORDS]
    return Query(tuple(kept or terms))


@dataclass
class Hit:
    item_type: str
    item_id: str
    score: float                  # best piece's, lower is better
    count: int = 0                # matching pieces
    title_match: bool = False
    # The piece to show: for a recording the first line that mentions
    # it, else the best matching piece other than the title.
    best: Optional[dict] = None
    moments: list[int] = field(default_factory=list)  # transcript at_ms


# A match in the name counts double.
_TITLE_WEIGHT = 2.0


def search(store: storage.Storage, query: Union[str, Query]) -> dict[str, list[Hit]]:
    """Matching items by type (:data:`ITEM_TYPES`), best first."""
    if isinstance(query, str):
        query = parse_query(query)
    out: dict[str, list[Hit]] = {t: [] for t in ITEM_TYPES}
    if not query:
        return out
    sync(store)
    try:
        rows = store.search_match(query.fts(), _MAX_ROWS)
    except sqlite3.OperationalError:  # a query FTS5 cannot parse
        return out
    hits: dict[tuple[str, str], Hit] = {}
    for row in rows:
        key = (row["item_type"], row["item_id"])
        score = row["score"]
        if row["field"] == "title":
            score *= _TITLE_WEIGHT  # bm25 is negative
        hit = hits.get(key)
        if hit is None:
            hit = hits[key] = Hit(*key, score=score)
        hit.score = min(hit.score, score)
        hit.count += 1
        at_ms = row["at_ms"] if row["field"] == "transcript" else None
        if row["field"] == "title":
            hit.title_match = True
        elif hit.best is None or (  # rows come best first
            at_ms is not None
            and (hit.best["at_ms"] is None or at_ms < hit.best["at_ms"])
        ):
            hit.best = row
        if at_ms is not None:
            hit.moments.append(at_ms)
    for hit in sorted(hits.values(), key=lambda h: h.score):
        hit.moments.sort()
        out[hit.item_type].append(hit)
    return out


# ---------------------------------------------------------------------------
# Showing matches
# ---------------------------------------------------------------------------

Segments = list[tuple[str, bool]]


def highlight(text: str, query: Union[str, Query]) -> Segments:
    """``text`` as ``[(part, is_match), ...]``, matched words marked."""
    if isinstance(query, str):
        query = parse_query(query)
    out: Segments = []
    pos = 0
    for m in _WORD.finditer(text or ""):
        word = _norm(m.group())
        stem = _stem(word)
        if any(t.matches(word, stem) for t in query.terms):
            if m.start() > pos:
                out.append((text[pos:m.start()], False))
            out.append((m.group(), True))
            pos = m.end()
    if pos < len(text or ""):
        out.append((text[pos:], False))
    return out


def snippet(text: str, query: Union[str, Query], width: int = 160) -> Segments:
    """Like :func:`highlight`, cut to about ``width`` characters around
    the first match."""
    if len(text) > width:
        segments = highlight(text, query)
        first = 0
        for part, marked in segments:
            if marked:
                break
            first += len(part)
        start = max(0, min(first - width // 3, len(text) - width))
        if start:
            space = text.find(" ", start)
            start = space + 1 if 0 <= space < first else start
        end = start + width
        if end < len(text):
            space = text.rfind(" ", start, end)
            end = space if space > first else end
        text = ("…" if start else "") + text[start:end].strip() + (
            "…" if end < len(text) else "")
    return highlight(text, query)
