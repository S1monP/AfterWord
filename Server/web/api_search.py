"""Search for the dashboard's search box (static/js/search.js): the
matches, grouped, with where each one opens."""
from __future__ import annotations

from typing import Any, Optional

from flask import Blueprint, jsonify, request, url_for

import search
import timeutil
from web.common import _KIND_LABEL, current_app_storage, rec_title
from web.pages import _KIND_PAGES, _is_other

bp = Blueprint("search_api", __name__)

# Items per group in one response; the box shows a few, then "Show all".
_PER_GROUP = 30
# Moments listed under a recording.
_MOMENTS = 8

_GROUP_LABELS = {
    "recording": "Recordings",
    "note": "Notes",
    "task": "To-dos and deadlines",
    "event": "Calendar",
}


@bp.get("/api/v1/search")
def api_search() -> Any:
    """``?q=`` -> ``{"query", "groups": [{"type", "label", "total",
    "items": [{"title", "meta", "snippet", "at", "url", "moments",
    "more"}]}]}``. ``title`` and ``snippet`` are ``[[text, matched],
    ...]`` so the page can mark the matched words."""
    raw = request.args.get("q", "")
    query = search.parse_query(raw)
    store = current_app_storage()
    groups = []
    for item_type, hits in search.search(store, query).items():
        items = []
        for hit in hits[:_PER_GROUP]:
            item = _ITEM_BUILDERS[item_type](store, hit, query, raw.strip())
            if item is not None:
                items.append(item)
        if items:
            groups.append({
                "type": item_type,
                "label": _GROUP_LABELS[item_type],
                "total": len(hits),
                "items": items,
            })
    return jsonify({"query": raw, "groups": groups})


def _when(iso: Optional[str], fmt: str = "%a %d %b %Y, %H:%M") -> str:
    dt = timeutil.to_local(iso)
    return dt.strftime(fmt) if dt else ""


def _clock(ms: int) -> str:
    """754000 -> "12:34"; past an hour "1:02:03"."""
    h, rem = divmod(ms // 1000, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _snippet(hit: search.Hit, query: search.Query) -> Optional[list]:
    return search.snippet(hit.best["text"], query) if hit.best else None


def _recording(store, hit, query, raw) -> Optional[dict]:
    sess = store.get_session(hit.item_id)
    if sess is None:
        return None
    page = _KIND_PAGES.get(sess.kind or ("other" if _is_other(sess) else ""))

    def url(at_ms: Optional[int] = None) -> str:
        args: dict[str, Any] = {"hl": raw}
        if at_ms is not None:
            args["t"] = at_ms
        if page is None:  # still being processed
            return url_for("pages.ui_session", session_id=sess.id, **args)
        endpoint, param = page
        return url_for(endpoint, **{param: sess.id}, **args)

    best_at = hit.best["at_ms"] if hit.best else None
    meta = _KIND_LABEL.get(sess.kind or "", "Recording")
    when = _when(sess.started_at)
    moments = hit.moments if len(hit.moments) > 1 else []
    return {
        "title": search.highlight(rec_title(sess), query),
        "meta": f"{meta} · {when}" if when else meta,
        "snippet": _snippet(hit, query),
        "at": _clock(best_at) if best_at is not None else None,
        "url": url(best_at),
        "moments": [{"label": _clock(ms), "url": url(ms)}
                    for ms in moments[:_MOMENTS]],
        "more": max(0, len(moments) - _MOMENTS),
    }


def _note(store, hit, query, raw) -> Optional[dict]:
    note = store.get_note(hit.item_id)
    if note is None:
        return None
    return {
        "title": search.highlight(note.title, query),
        "meta": "Note · edited " + _when(note.updated_at, "%d %b %Y"),
        "snippet": _snippet(hit, query),
        "url": url_for("pages.ui_notes", note_id=note.id),
    }


def _task(store, hit, query, raw) -> Optional[dict]:
    task = store.get_task(hit.item_id)
    if task is None:
        return None
    meta = "Deadline" if task.kind == "deadline" else "To-do"
    if task.due_at:
        meta += " · due " + _when(task.due_at, "%a %d %b %Y, %H:%M")
    if task.status != "open":
        meta += f" · {task.status}"
    return {
        "title": search.highlight(task.title, query),
        "meta": meta,
        "snippet": _snippet(hit, query),
        "url": url_for("pages.ui_todos") + f"#todo-{task.id}",
    }


def _event(store, hit, query, raw) -> Optional[dict]:
    event = store.get_event(hit.item_id)
    if event is None:
        return None
    meta = _when(event.starts_at)
    if event.location:
        meta += f" · {event.location}"
    return {
        "title": search.highlight(event.title, query),
        "meta": meta,
        "snippet": _snippet(hit, query),
        "url": url_for("pages.ui_event_edit", event_id=event.id),
    }


_ITEM_BUILDERS = {
    "recording": _recording,
    "note": _note,
    "task": _task,
    "event": _event,
}
