"""A recording as one Markdown file: name, date, summary, key points,
the items it filed, and the transcript."""
from __future__ import annotations

import json
import re

import storage
from web.common import _KIND_LABEL, rec_title, rec_when

# Characters Windows does not allow in file names.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def session_markdown(store: storage.Storage, session_id: str) -> tuple[str, str]:
    """``(file name, Markdown text)`` for a recording."""
    sess = store.get_session(session_id)
    if sess is None:
        raise KeyError(session_id)
    title = rec_title(sess)
    out = [f"# {title}", "", rec_when(sess)]
    if sess.kind:
        out.append(f"Kind: {_KIND_LABEL.get(sess.kind, sess.kind)}")
    summary = _summary(store.get_artifact(session_id, "summary"))
    if summary.get("text"):
        out += ["", "## Summary", "", summary["text"]]
    for sec in summary.get("sections") or []:
        if isinstance(sec, dict) and sec.get("text"):
            out += ["", f"### {sec.get('heading') or ''}".rstrip(), "", sec["text"]]
    points = [p for p in summary.get("key_points") or [] if str(p).strip()]
    if points:
        out += ["", "## Key points", ""] + [f"- {p}" for p in points]
    tasks, events = store.filed_from(session_id)
    if tasks or events:
        out += ["", "## Added to your lists", ""]
        for t in tasks:
            label = "Deadline" if t.kind == "deadline" else "To-do"
            due = f" (due {t.due_at[:10]})" if t.due_at else ""
            out.append(f"- {label}: {t.title}{due}")
        for e in events:
            out.append(f"- Calendar: {e.title} ({e.starts_at[:16].replace('T', ' ')} UTC)")
    transcript = store.get_artifact(session_id, "transcript")
    if transcript and transcript.get("content"):
        out += ["", "## Transcript", "", transcript["content"].strip()]
    day = (sess.started_at or "")[:10]
    name = _UNSAFE.sub(" ", f"{day} {title}").strip()[:120] or session_id
    return f"{name}.md", "\n".join(out) + "\n"


def _summary(artifact) -> dict:
    if not artifact or not artifact.get("content"):
        return {}
    try:
        payload = json.loads(artifact["content"])
    except (TypeError, ValueError):
        return {"text": artifact["content"]}
    return payload if isinstance(payload, dict) else {}
