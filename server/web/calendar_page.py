"""The Calendar page.

Month or Week, stepping by month or week; a day panel on the right on
desktop (the chosen day, today by default), and on the phone a chosen
day opens full screen as a timeline.
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Any

import timeutil
from web.common import current_app_tasks_service
from web.home import _Ctx, _events_between

HOURS = (8, 21)            # the time grid, widened to fit events
SHOWN_PER_DAY = 3          # month cells: events shown before "+ N more"
LONG_STATUS = {
    "Summary ready": "Recorded · summary ready",
    "Summarising": "Recorded · summarising",
    "Processing failed": "Recorded · processing failed",
}


def _deadlines_between(ctx: _Ctx, first: _dt.date, days: int) -> list[dict[str, Any]]:
    tasks, _ = current_app_tasks_service().list(kind="deadline", limit=1000)
    last = first + _dt.timedelta(days=days)
    out = []
    for t in tasks:
        if t.status == "cancelled":
            continue
        due = timeutil.to_local(t.due_at)
        if due is None or not (first <= due.date() < last):
            continue
        out.append({
            "id": t.id, "title": t.title, "date": due.date(),
            "time": "" if (due.hour, due.minute) == (0, 0) else f"{due:%H:%M}",
            "cc": ctx.cc(t.course_id), "course": ctx.course_name(t.course_id),
            "done": t.status == "done",
        })
    return out


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _timeline(events: list[dict[str, Any]], start_h: int, end_h: int) -> list[dict[str, Any]]:
    span = (end_h - start_h) * 60
    out = []
    for e in events:
        e = dict(e)
        e["top"] = (e["start_min"] - start_h * 60) / span * 100
        e["height"] = max(e["end_min"] - e["start_min"], 20) / span * 100
        out.append(e)
    return out


def _hours_for(events: list[dict[str, Any]]) -> tuple[int, int]:
    start_h = min([HOURS[0]] + [e["start_min"] // 60 for e in events])
    end_h = max([HOURS[1]] + [math.ceil(e["end_min"] / 60) for e in events])
    return start_h, min(end_h, 24)


def _now_pct(ctx: _Ctx, start_h: int, end_h: int) -> float | None:
    now_min = ctx.now.hour * 60 + ctx.now.minute
    if not start_h * 60 <= now_min <= end_h * 60:
        return None
    return (now_min - start_h * 60) / ((end_h - start_h) * 60) * 100


def _day(ctx: _Ctx, day: _dt.date) -> dict[str, Any]:
    events = [dict(e, status=LONG_STATUS.get(e["status"], e["status"]))
              for e in _events_between(ctx, day, 1)]
    deadlines = _deadlines_between(ctx, day, 1)
    start_h, end_h = _hours_for(events)
    counts = [_plural(len(events), "event") if events else "No events"]
    if deadlines:
        counts.append(_plural(len(deadlines), "deadline"))
    return {
        "date": day, "iso": day.isoformat(),
        "title": f"{day:%A} {day.day} {day:%B}",
        "is_today": day == ctx.today, "counts": " · ".join(counts),
        "events": events, "deadlines": deadlines,
        "timeline": _timeline(events, start_h, end_h),
        "hours": list(range(start_h, end_h + 1)),
        "now_pct": _now_pct(ctx, start_h, end_h) if day == ctx.today else None,
        "prev": (day - _dt.timedelta(days=1)).isoformat(),
        "next": (day + _dt.timedelta(days=1)).isoformat(),
    }


def calendar_context(view: str, anchor: _dt.date, chosen: _dt.date | None) -> dict[str, Any]:
    ctx = _Ctx()
    if view == "month":
        first_of_month = anchor.replace(day=1)
        start = first_of_month - _dt.timedelta(days=first_of_month.weekday())
        count = 42
        heading = f"{first_of_month:%B %Y}"
        prev_month = (first_of_month - _dt.timedelta(days=1)).replace(day=1)
        next_month = (first_of_month + _dt.timedelta(days=32)).replace(day=1)
        prev_anchor, next_anchor = prev_month, next_month
    else:
        start = anchor - _dt.timedelta(days=anchor.weekday())
        count = 7
        end = start + _dt.timedelta(days=6)
        heading = f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
        prev_anchor, next_anchor = start - _dt.timedelta(days=7), start + _dt.timedelta(days=7)
    events = _events_between(ctx, start, count)
    deadlines = _deadlines_between(ctx, start, count)
    selected = chosen or (ctx.today if start <= ctx.today < start + _dt.timedelta(days=count) else anchor)
    cells = []
    for i in range(count):
        d = start + _dt.timedelta(days=i)
        evs = [e for e in events if e["date"] == d]
        dls = [x for x in deadlines if x["date"] == d]
        # Month chips: deadlines first, then events, three then "+ N more".
        chips = [dict(x, due=True) for x in dls] + evs
        now_min = ctx.now.hour * 60 + ctx.now.minute
        cells.append({
            "date": d, "iso": d.isoformat(), "num": d.day, "dow": f"{d:%a}",
            "in_month": view != "month" or d.month == anchor.month,
            "is_today": d == ctx.today, "is_selected": d == selected,
            "events": evs, "deadlines": dls,
            "chips": chips[:SHOWN_PER_DAY], "more": max(0, len(chips) - SHOWN_PER_DAY),
            # Where the now line goes in today's list (phone week).
            "now_at": sum(1 for e in evs if e["start_min"] <= now_min) if d == ctx.today else None,
            "dots": [e["cc"] for e in evs[:3]], "extra": max(0, len(evs) - 3),
            "aria": f"{d:%A} {d.day} {d:%B}, " + ", ".join(filter(None, [
                _plural(len(evs), "event") if evs else "no events",
                _plural(len(dls), "deadline") if dls else "",
            ])),
        })
    week = None
    if view == "week":
        start_h, end_h = _hours_for(events)
        week = {
            "hours": list(range(start_h, end_h + 1)),
            "days": [dict(c, timed=_timeline(c["events"], start_h, end_h),
                          now_pct=_now_pct(ctx, start_h, end_h) if c["is_today"] else None) for c in cells],
        }
    return {
        "view": view, "anchor": anchor.isoformat(), "heading": heading,
        "prev": prev_anchor.isoformat(), "next": next_anchor.isoformat(),
        "today": ctx.today.isoformat(), "now": ctx.now,
        "weeks": [cells[i:i + 7] for i in range(0, len(cells), 7)] if view == "month" else None,
        "cells": cells, "week": week,
        "day": _day(ctx, selected),
        "day_chosen": chosen is not None,
        "back_label": f"{selected:%B}" if view == "month" else "Week",
    }
