"""To-dos and Deadlines (REDESIGN.md section 9; phone: section 14).

Two lists, each in groups: to-dos by when they are due (Overdue, Today,
Upcoming, No date) with High urgency first in a group, deadlines by week
(Overdue, This week, Next week, Later) with a days-left tile. Done
items close each list.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

import timeutil
from web.common import current_app_tasks_service
from web.home import _PRIO, _Ctx, _due_label, _said_in

DONE_SHOWN = 100


def _row(ctx: _Ctx, t) -> dict[str, Any]:
    due = timeutil.to_local(t.due_at)
    all_day = due is not None and (due.hour, due.minute) == (0, 0)
    done = t.status == "done"
    late = due is not None and not done and (due.date() < ctx.today if all_day else due < ctx.now)
    prio = "high" if t.priority in ("high", "urgent") else (t.priority or "medium")
    days = (due.date() - ctx.today).days if due else None
    row = {
        "id": t.id, "kind": t.kind, "title": t.title, "done": done,
        "prio": prio, "prio_label": prio.capitalize(),
        "due": (("Late · " if late else "") + _due_label(due, ctx.today)) if due else "",
        "due_cls": "is-late" if late else ("is-today" if days == 0 and not done else ""),
        "due_date": f"{due:%Y-%m-%d}" if due else "",
        "due_time": "" if due is None or all_day else f"{due:%H:%M}",
        "course_id": t.course_id or "", "course": ctx.course_name(t.course_id), "cc": ctx.cc(t.course_id),
        "said_in": _said_in(ctx, t),
        "late": late, "days": days,
    }
    if t.kind == "deadline" and days is not None:
        # The days-left tile: "3 days", "1 day", "today", "2 late".
        row["tile_num"] = "" if days == 0 else abs(days)
        row["tile_unit"] = "today" if days == 0 else ("late" if days < 0 else ("day" if days == 1 else "days"))
    return row


def _group(label: str, rows: list[dict[str, Any]], cls: str = "") -> dict[str, Any]:
    return {"label": label, "rows": rows, "cls": cls}


def _todo_groups(ctx: _Ctx, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list] = {"Overdue": [], "Today": [], "Upcoming": [], "No date": []}
    for r in rows:
        if r["late"]:
            buckets["Overdue"].append(r)
        elif r["days"] is None:
            buckets["No date"].append(r)
        elif r["days"] <= 0:
            buckets["Today"].append(r)
        else:
            buckets["Upcoming"].append(r)
    for rs in buckets.values():
        rs.sort(key=lambda r: (_PRIO.get(r["prio"], 1), r["due_date"] or "9999", r["due_time"]))
    return [_group(k, v, "is-danger" if k == "Overdue" else "") for k, v in buckets.items() if v]


def _deadline_groups(ctx: _Ctx, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sunday = ctx.today + _dt.timedelta(days=6 - ctx.today.weekday())
    buckets: dict[str, list] = {"Overdue": [], "This week": [], "Next week": [], "Later": []}
    for r in sorted(rows, key=lambda r: (r["due_date"], r["due_time"] or "99")):
        due = _dt.date.fromisoformat(r["due_date"])
        if r["late"]:
            buckets["Overdue"].append(r)
        elif due <= sunday:
            buckets["This week"].append(r)
        elif due <= sunday + _dt.timedelta(days=7):
            buckets["Next week"].append(r)
        else:
            buckets["Later"].append(r)
    return [_group(k, v, "is-danger" if k == "Overdue" else "") for k, v in buckets.items() if v]


def _list(ctx: _Ctx, kind: str) -> dict[str, Any]:
    svc = current_app_tasks_service()
    open_tasks, _ = svc.list(kind=kind, status="open", limit=1000)
    done_tasks, done_total = svc.list(kind=kind, status="done", limit=DONE_SHOWN)
    rows = [_row(ctx, t) for t in open_tasks if kind == "task" or t.due_at]
    # Done: the most recently finished first.
    done = [_row(ctx, t) for t in sorted(done_tasks, key=lambda t: t.completed_at or t.updated_at, reverse=True)]
    groups = _todo_groups(ctx, rows) if kind == "task" else _deadline_groups(ctx, rows)
    return {"groups": groups, "open": len(rows), "done": done, "done_total": done_total}


def page_context() -> dict[str, Any]:
    ctx = _Ctx()
    courses = sorted(ctx.courses.values(), key=lambda c: c.name.lower())
    return {
        "todos": _list(ctx, "task"),
        "deadlines": _list(ctx, "deadline"),
        "courses": courses,
        "course_options": [{"id": c.id, "name": c.name, "cc": ctx.cc(c.id)} for c in courses],
        "today": ctx.today.isoformat(),
    }
