"""Web UI pages (HTML).
"""
from __future__ import annotations

import io
from typing import Any

from flask import abort, Blueprint, current_app, redirect, render_template, request, send_file, url_for

import services
import services.events  # noqa: F401
import services.classification  # noqa: F401
import timeutil
from services import transcode
from web.pipeline import set_kind_by_hand
from web.common import (
    _selected_session_context,
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


@bp.post("/sessions/<session_id>/kind")
def ui_set_kind(session_id: str) -> Any:
    """Form post from the Other page: classify by hand, then go to
    the page the recording now belongs on."""
    if current_app_storage().get_session(session_id) is None:
        abort(404)
    kind = request.form.get("kind", "").strip()
    if kind not in services.classification.SELECTABLE_KINDS:
        abort(400)
    sess = set_kind_by_hand(current_app._get_current_object(), session_id, kind)
    return redirect(recording_url(sess))


@bp.get("/notes")
def ui_notes() -> Any:
    """Two-pane notes page: sidebar list on the left, embedded
    editor on the right.

    Modes (via query params):

    * ``?note_id=<id>`` — editor prefilled with that note.
    * ``?new=1``        — blank editor for a new note.
    * ``?memo=<id>``    — a voice-memo recording: audio + transcript.
    * neither           — empty right pane ("Pick a note").

    ``q`` filters the sidebar lists (bookmarkable): notes, and voice
    memos by title or transcript.
    """
    q = request.args.get("q", "").strip()
    notes, total = current_app_notes_service().list(
        limit=50, offset=0, search=q or None,
    )
    store = current_app_storage()
    memos = [s for s in store.list_sessions() if s.kind == "voice_memo"]
    if q:
        needle = q.lower()

        def _matches(sess) -> bool:
            transcript = store.get_artifact(sess.id, "transcript")
            text = (transcript or {}).get("content") or ""
            return (needle in (sess.title or "").lower()
                    or needle in text.lower())

        memos = [s for s in memos if _matches(s)]
    mode = "empty"
    note = None
    memo_ctx: dict[str, Any] = {}
    memo_id = request.args.get("memo", "").strip()
    if memo_id:
        memo_ctx = _selected_session_context(memo_id)
        if memo_ctx["selected_session"] is None:
            abort(404)
        mode = "memo"
        memo_ctx["kinds"] = sorted(services.classification.SELECTABLE_KINDS)
    elif request.args.get("new"):
        mode = "new"
    else:
        note_id = request.args.get("note_id", "").strip()
        if note_id:
            note = current_app_notes_service().get(note_id)
            if note is None:
                abort(404)
            mode = "edit"
    return render_template(
        "notes.html",
        notes=notes,
        total=total,
        memos=memos,
        q=q,
        mode=mode,
        note=note,
        **memo_ctx,
    )


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


@bp.get("/calendar")
def ui_calendar() -> Any:
    """Month-grid calendar. Defaults to the current month if
    ``?year=&month=`` are missing or invalid."""
    import datetime as _dt
    cur = timeutil.local_today()
    try:
        year = int(request.args.get("year", str(cur.year)))
        month = int(request.args.get("month", str(cur.month)))
    except ValueError:
        year, month = cur.year, cur.month
    if not (1 <= month <= 12):
        month = cur.month
    if not (1970 <= year <= 2100):
        year = cur.year
    # Load a 42-day window starting at grid_start so the grid has
    # every event it might display.
    first_of_month = _dt.date(year, month, 1)
    leading = first_of_month.weekday()
    grid_start = first_of_month - _dt.timedelta(days=leading)
    # Local days, as stored (UTC).
    window_lo = timeutil.local_midnight_utc(grid_start)
    window_hi = timeutil.local_midnight_utc(
        grid_start + _dt.timedelta(days=43),
    )
    events, _ = current_app_events_service().list(
        starts_after=window_lo, starts_before=window_hi,
        limit=500,
    )
    grid = services.events.compute_month_grid(year, month, events)
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    return render_template(
        "calendar.html",
        year=year, month=month,
        grid=grid,
        month_name=_dt.date(year, month, 1).strftime("%B"),
        prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month,
        today_iso=cur.isoformat(),
        is_current_month=(
            year == cur.year and month == cur.month
        ),
    )


@bp.get("/events/new")
def ui_event_new() -> Any:
    """New event form. ``?date=YYYY-MM-DD`` prefills starts_at to
    noon on that day. Without ``?date=`` we default to today noon."""
    import datetime as _dt
    date_str = request.args.get("date", "").strip()
    try:
        d = (
            _dt.date.fromisoformat(date_str)
            if date_str else timeutil.local_today()
        )
    except ValueError:
        d = timeutil.local_today()
    starts_at_default = _dt.datetime.combine(
        d, _dt.time(12, 0),
    ).strftime("%Y-%m-%dT%H:%M")
    return render_template(
        "event_form.html",
        mode="new", event=None,
        starts_at_default=starts_at_default,
        ends_at_default="",
        courses=current_app_courses_service().list()[0],
    )


@bp.get("/events/<event_id>/edit")
def ui_event_edit(event_id: str) -> Any:
    ev = current_app_events_service().get(event_id)
    if ev is None:
        abort(404)
    # datetime-local inputs take local "YYYY-MM-DDTHH:MM"; the form
    # posts it back without an offset, which reads as local again.
    def _strip(iso):
        dt = timeutil.to_local(iso)
        return dt.strftime("%Y-%m-%dT%H:%M") if dt else ""
    return render_template(
        "event_form.html",
        mode="edit", event=ev,
        starts_at_default=_strip(ev.starts_at),
        ends_at_default=_strip(ev.ends_at),
        courses=current_app_courses_service().list()[0],
    )


@bp.post("/events/new")
def create_event_action() -> Any:
    """HTML form action for the new-event page."""
    payload = {
        "title": (request.form.get("title") or "").strip(),
        "kind": request.form.get("kind") or "lecture",
        "starts_at": request.form.get("starts_at") or "",
        "ends_at": request.form.get("ends_at") or None,
        "description": (
            request.form.get("description") or ""
        ).strip() or None,
        "location": (
            request.form.get("location") or ""
        ).strip() or None,
        "course_id": (
            request.form.get("course_id") or ""
        ).strip() or None,
    }
    repeat_until = (
        request.form.get("repeat_until") or ""
    ).strip() or None
    try:
        if repeat_until:
            current_app_events_service().create_recurring(
                **payload, repeat_until=repeat_until,
            )
        else:
            current_app_events_service().create(**payload)
    except ValueError as exc:
        return (
            render_template(
                "event_form.html",
                mode="new", event=None,
                starts_at_default=payload["starts_at"],
                ends_at_default=payload.get("ends_at") or "",
                error=str(exc),
                courses=current_app_courses_service().list()[0],
            ),
            400,
        )
    return redirect(url_for("pages.ui_calendar"))


@bp.post("/events/<event_id>/edit")
def update_event_action(event_id: str) -> Any:
    payload = {
        "title": (request.form.get("title") or "").strip() or None,
        "kind": request.form.get("kind") or None,
        "starts_at": request.form.get("starts_at") or None,
        "ends_at": request.form.get("ends_at") or None,
        "description": (
            request.form.get("description") or ""
        ).strip() or None,
        "location": (
            request.form.get("location") or ""
        ).strip() or None,
        "course_id": (
            request.form.get("course_id") or ""
        ).strip() or None,
    }
    try:
        current_app_events_service().update(event_id, **payload)
    except KeyError:
        abort(404)
    except ValueError as exc:
        ev = current_app_events_service().get(event_id)
        return (
            render_template(
                "event_form.html",
                mode="edit", event=ev,
                starts_at_default=payload.get("starts_at") or "",
                ends_at_default=payload.get("ends_at") or "",
                error=str(exc),
                courses=current_app_courses_service().list()[0],
            ),
            400,
        )
    return redirect(url_for("pages.ui_calendar"))


@bp.post("/events/<event_id>/delete")
def delete_event_action(event_id: str) -> Any:
    deleted = current_app_events_service().delete(event_id)
    if not deleted:
        abort(404)
    return redirect(url_for("pages.ui_calendar"))


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
