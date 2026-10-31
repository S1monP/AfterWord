"""Web UI pages (HTML).
"""
from __future__ import annotations

import io
from typing import Any

from flask import abort, Blueprint, current_app, redirect, render_template, request, send_file, url_for

import timeutil
from services import transcode
from web.common import (
    current_app_courses_service,
    current_app_events_service,
    current_app_notes_service,
    current_app_storage,
    current_app_tasks_service,
    recording_url,
)


bp = Blueprint("pages", __name__)


# --- Web UI ----------------------------------------------------------
@bp.get("/")
def ui_dashboard() -> Any:
    """Home (REDESIGN.md section 3; phone: Home I, section 14).
    ``?week=`` moves the week schedule by that many weeks."""
    from web.home import home_context
    try:
        week = max(-52, min(52, int(request.args.get("week", "0"))))
    except ValueError:
        week = 0
    return render_template("home.html", **home_context(week))


def _reading_page(page: str, mode: str = "date") -> Any:
    """Lectures, Meetings and Other: a list and a reader (web/recordings.py)."""
    from web.recordings import page_context
    ctx = page_context(page, request.args.get("session", "").strip(), mode)
    return render_template("reading.html", **ctx)


@bp.get("/lectures")
def ui_lectures() -> Any:
    """``?mode=course|date`` groups the list; ``?course=<id>`` (from the
    sidebar) shows that course's lectures."""
    mode = request.args.get("mode", "course")
    if mode not in ("course", "date"):
        mode = "course"
    return _reading_page("lectures", mode)


@bp.get("/meetings")
def ui_meetings() -> Any:
    return _reading_page("meetings")


@bp.get("/other")
def ui_other() -> Any:
    """Recordings that are not lectures, meetings or notes, and ones not
    sorted yet; "Move to" in the actions menu sorts them."""
    return _reading_page("other")


@bp.get("/sessions/<session_id>/export.md")
def ui_export_session(session_id: str) -> Any:
    """Download a recording's name, summary, items and transcript as
    one Markdown file."""
    from web.export import session_markdown
    try:
        name, text = session_markdown(current_app_storage(), session_id)
    except KeyError:
        abort(404)
    return send_file(
        io.BytesIO(text.encode("utf-8")), as_attachment=True,
        download_name=name, mimetype="text/markdown",
    )


@bp.get("/notes")
def ui_notes() -> Any:
    """Notes and voice memos, one list and one live editor
    (web/notes_page.py). ``?note_id=`` opens a note, ``?new=1`` a blank
    one; without either, desktop opens the newest."""
    from web.notes_page import page_context
    ctx = page_context(request.args.get("note_id", "").strip(), bool(request.args.get("new")))
    if ctx.get("missing"):
        abort(404)
    return render_template("notes.html", **ctx)


@bp.get("/notes/<note_id>/export.md")
def ui_export_note(note_id: str) -> Any:
    """Download a note as a Markdown file."""
    note = current_app_notes_service().get(note_id)
    if note is None:
        abort(404)
    title = note.title or "Note"
    text = f"# {title}\n\n{note.body_md}".rstrip() + "\n"
    safe = "".join(c for c in title if c not in '\\/:*?"<>|').strip() or "Note"
    return send_file(io.BytesIO(text.encode("utf-8")), as_attachment=True,
                     download_name=f"{safe}.md", mimetype="text/markdown")


@bp.get("/todos")
def ui_todos() -> Any:
    """The unified Tasks + Deadlines page: personal tasks on the
    left (``?status=`` filter), course deadlines on the right
    (``?deadline_status=`` filter), each with a quick-add form."""
    status_filter = request.args.get("status")
    if status_filter not in (None, "", "open", "done", "cancelled"):
        status_filter = None
    tasks, total = current_app_tasks_service().list(
        kind="task", status=status_filter or None, limit=100,
    )
    open_count = sum(1 for t in tasks if t.status == "open")
    deadline_status = request.args.get("deadline_status")
    if deadline_status not in (None, "", "open", "done", "cancelled"):
        deadline_status = None
    deadlines, deadline_total = current_app_tasks_service().list(
        kind="deadline", status=deadline_status or None, limit=100,
    )
    deadline_open_count = sum(
        1 for d in deadlines if d.status == "open"
    )
    courses, _ = current_app_courses_service().list()
    return render_template(
        "todos.html",
        tasks=tasks,
        total=total,
        open_count=open_count,
        status_filter=status_filter or "",
        deadlines=deadlines,
        deadline_total=deadline_total,
        deadline_open_count=deadline_open_count,
        deadline_status=deadline_status or "",
        courses=courses,
    )


@bp.post("/todos/quick")
def ui_todos_quick() -> Any:
    """Quick-add a personal task. Optional fields: priority, due_at.

    ``due_at`` is optional for tasks (kind='task' here). Empty
    form values are normalised to ``None`` so the service sees a
    clean value.
    """
    title = (request.form.get("title") or "").strip()
    if not title:
        return redirect(url_for("pages.ui_todos"))
    priority = (request.form.get("priority") or "medium").strip()
    due_at_raw = (request.form.get("due_at") or "").strip()
    try:
        current_app_tasks_service().create(
            title=title, kind="task",
            priority=priority, due_at=due_at_raw or None,
        )
    except ValueError:
        return redirect(url_for("pages.ui_todos"))
    return redirect(url_for("pages.ui_todos"))


@bp.post("/todos/deadline-quick")
def ui_todos_deadline_quick() -> Any:
    """Quick-add a deadline from the unified Todos page.

    ``due_at`` is required (HTML5 ``required`` in the form, and
    enforced server-side). ``course_id`` is optional — deadlines
    may be associated with a course. Priority stays at its
    'medium' default (no priority control in this form).
    """
    title = (request.form.get("title") or "").strip()
    if not title:
        return redirect(url_for("pages.ui_todos"))
    due_at_raw = (request.form.get("due_at") or "").strip()
    course_id = (request.form.get("course_id") or "").strip()
    description = (request.form.get("description") or "").strip()
    try:
        current_app_tasks_service().create(
            title=title, kind="deadline",
            due_at=due_at_raw or None,
            description=description or None,
            course_id=course_id or None,
        )
    except ValueError:
        return redirect(url_for("pages.ui_todos"))
    return redirect(url_for("pages.ui_todos"))


def _iso_date(raw: str | None):
    import datetime as _dt
    try:
        return _dt.date.fromisoformat((raw or "").strip()[:10])
    except ValueError:
        return None


@bp.get("/calendar")
def ui_calendar() -> Any:
    """Calendar (REDESIGN.md section 8; phone: section 14).
    ``?view=month|week``, ``?date=`` the month or week shown (today by
    default), ``?day=`` the chosen day: the day panel on desktop, the
    full-screen day on the phone."""
    from web.calendar_page import calendar_context
    view = request.args.get("view", "month")
    if view not in ("month", "week"):
        view = "month"
    day = _iso_date(request.args.get("day"))
    anchor = _iso_date(request.args.get("date")) or day or timeutil.local_today()
    return render_template("calendar.html", **calendar_context(view, anchor, day))


def _back_to_day(starts_at: str | None) -> Any:
    """After saving an event, go to its day in the calendar."""
    start = timeutil.to_local(starts_at)
    if start is None:
        return redirect(url_for("pages.ui_calendar"))
    d = start.date().isoformat()
    return redirect(url_for("pages.ui_calendar", date=d, day=d))


def _event_form(mode: str, event: Any, starts: str, ends: str, error: str = "") -> Any:
    """The new / edit event page. ``starts`` and ``ends`` are local
    "YYYY-MM-DDTHH:MM" for the datetime-local inputs; Back and Cancel go
    to the event's day."""
    day = _iso_date(starts)
    back = url_for("pages.ui_calendar", date=day.isoformat(), day=day.isoformat()) if day else url_for("pages.ui_calendar")
    page = render_template(
        "event_form.html", mode=mode, event=event, error=error,
        starts_at_default=starts, ends_at_default=ends, back_url=back,
        courses=current_app_courses_service().list()[0],
    )
    return (page, 400) if error else page


def _local_input(iso: str | None) -> str:
    dt = timeutil.to_local(iso)
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else ""


def _event_fields() -> dict[str, Any]:
    form = request.form
    return {
        "title": (form.get("title") or "").strip(),
        "kind": form.get("kind") or "lecture",
        "starts_at": form.get("starts_at") or "",
        "ends_at": form.get("ends_at") or None,
        "description": (form.get("description") or "").strip() or None,
        "location": (form.get("location") or "").strip() or None,
        "course_id": (form.get("course_id") or "").strip() or None,
    }


@bp.get("/events/new")
def ui_event_new() -> Any:
    """New event form. ``?date=YYYY-MM-DD`` (today by default) prefills
    the start: the next whole hour today, otherwise 09:00."""
    import datetime as _dt
    d = _iso_date(request.args.get("date")) or timeutil.local_today()
    now = timeutil.local_now()
    hour = min(now.hour + 1, 23) if d == now.date() else 9
    start = _dt.datetime.combine(d, _dt.time(hour))
    end = start + _dt.timedelta(hours=1)
    return _event_form("new", None, f"{start:%Y-%m-%dT%H:%M}", f"{end:%Y-%m-%dT%H:%M}" if end.date() == d else "")


@bp.get("/events/<event_id>/edit")
def ui_event_edit(event_id: str) -> Any:
    ev = current_app_events_service().get(event_id)
    if ev is None:
        abort(404)
    # datetime-local inputs take local "YYYY-MM-DDTHH:MM"; the form
    # posts it back without an offset, which reads as local again.
    return _event_form("edit", ev, _local_input(ev.starts_at), _local_input(ev.ends_at))


@bp.post("/events/new")
def create_event_action() -> Any:
    """HTML form action for the new-event page; "Repeats weekly" adds
    one event a week until ``repeat_until``."""
    payload = _event_fields()
    repeat_until = (request.form.get("repeat_until") or "").strip() or None
    try:
        if repeat_until:
            ev, _ = current_app_events_service().create_recurring(**payload, repeat_until=repeat_until)
        else:
            ev = current_app_events_service().create(**payload)
    except ValueError as exc:
        return _event_form("new", None, payload["starts_at"], payload["ends_at"] or "", str(exc))
    return _back_to_day(ev.starts_at)


@bp.post("/events/<event_id>/edit")
def update_event_action(event_id: str) -> Any:
    payload = _event_fields()
    try:
        ev = current_app_events_service().update(event_id, **payload)
    except KeyError:
        abort(404)
    except ValueError as exc:
        return _event_form("edit", current_app_events_service().get(event_id),
                           payload["starts_at"], payload["ends_at"] or "", str(exc))
    return _back_to_day(ev.starts_at)


@bp.post("/events/<event_id>/delete")
def delete_event_action(event_id: str) -> Any:
    ev = current_app_events_service().get(event_id)
    if ev is None or not current_app_events_service().delete(event_id):
        abort(404)
    return _back_to_day(ev.starts_at)


@bp.get("/upload")
def ui_upload() -> Any:
    cfg = current_app.config["LR_CONFIG"]
    return render_template(
        "upload.html",
        max_upload_bytes=cfg.max_upload_bytes,
        ffmpeg_available=transcode.ffmpeg_available(cfg.ffmpeg_path),
    )


# --- UI: courses admin (bookmark-only) ------------------------------
@bp.get("/courses/admin")
def ui_courses_admin() -> Any:
    """Admin page for creating/editing/deleting courses. Not
    linked from the top nav; bookmark this URL."""
    courses, total = current_app_courses_service().list()
    return render_template(
        "courses_admin.html",
        courses=courses,
        total=total,
    )


@bp.get("/courses/new")
def ui_course_new() -> Any:
    return render_template(
        "course_form.html", mode="new", course=None,
        form_name="", form_code="", form_color="",
        form_sort_order="0",
    )


@bp.get("/courses/<course_id>/edit")
def ui_course_edit(course_id: str) -> Any:
    course = current_app_courses_service().get(course_id)
    if course is None:
        abort(404)
    return render_template(
        "course_form.html", mode="edit", course=course,
        form_name=course.name,
        form_code=course.code or "",
        form_color=course.color or "",
        form_sort_order=str(course.sort_order),
    )


@bp.post("/courses/new")
def create_course_action() -> Any:
    payload = {
        "name": (request.form.get("name") or "").strip(),
        "code": (request.form.get("code") or "").strip() or None,
        "color": (request.form.get("color") or "").strip() or None,
        "sort_order": request.form.get("sort_order") or 0,
    }
    try:
        current_app_courses_service().create(**payload)
    except ValueError as exc:
        return (
            render_template(
                "course_form.html",
                mode="new", course=None,
                form_name=payload["name"],
                form_code=payload["code"] or "",
                form_color=payload["color"] or "",
                form_sort_order=str(payload["sort_order"]),
                error=str(exc),
            ),
            400,
        )
    return redirect(url_for("pages.ui_courses_admin"))


@bp.post("/courses/<course_id>/edit")
def update_course_action(course_id: str) -> Any:
    payload = {
        "name": (request.form.get("name") or "").strip() or None,
        "code": (request.form.get("code") or "").strip() or None,
        "color": (request.form.get("color") or "").strip() or None,
        "sort_order": request.form.get("sort_order") or None,
    }
    try:
        current_app_courses_service().update(course_id, **payload)
    except KeyError:
        abort(404)
    except ValueError as exc:
        course = current_app_courses_service().get(course_id)
        return (
            render_template(
                "course_form.html",
                mode="edit", course=course,
                form_name=payload.get("name") or "",
                form_code=payload.get("code") or "",
                form_color=payload.get("color") or "",
                form_sort_order=str(payload.get("sort_order") or 0),
                error=str(exc),
            ),
            400,
        )
    return redirect(url_for("pages.ui_courses_admin"))


@bp.post("/courses/<course_id>/delete")
def delete_course_action(course_id: str) -> Any:
    deleted = current_app_courses_service().delete(course_id)
    if not deleted:
        abort(404)
    return redirect(url_for("pages.ui_courses_admin"))
