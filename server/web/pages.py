"""Web UI pages (HTML)."""

from __future__ import annotations

import io
from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

import timeutil
from services import transcode
from web.common import (
    current_app_courses_service,
    current_app_events_service,
    current_app_notes_service,
    current_app_storage,
)

bp = Blueprint("pages", __name__)


# --- Web UI ----------------------------------------------------------
@bp.get("/")
def ui_dashboard() -> Any:
    """Home.
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
        io.BytesIO(text.encode("utf-8")),
        as_attachment=True,
        download_name=name,
        mimetype="text/markdown",
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
    return send_file(
        io.BytesIO(text.encode("utf-8")),
        as_attachment=True,
        download_name=f"{safe}.md",
        mimetype="text/markdown",
    )


@bp.get("/todos")
def ui_todos() -> Any:
    """To-dos and deadlines (web/todos_page.py): two panels on desktop,
    a To-dos | Deadlines switch on the phone. Adding, ticking and the
    actions menu use the tasks API."""
    from web.todos_page import page_context

    return render_template("todos.html", **page_context())


def _iso_date(raw: str | None):
    import datetime as _dt

    try:
        return _dt.date.fromisoformat((raw or "").strip()[:10])
    except ValueError:
        return None


@bp.get("/calendar")
def ui_calendar() -> Any:
    """Calendar.
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


@bp.get("/calendar/subscriptions")
def ui_subscriptions() -> Any:
    """Calendar subscriptions (a link kept in step every hour) and
    one-off .ics imports."""
    from services.calendar_feeds import display_url
    from web.common import current_app_subscriptions_service

    svc = current_app_subscriptions_service()
    today = timeutil.local_today()

    def when(iso):
        t = timeutil.to_local(iso)
        if t is None:
            return ""
        return f"today {t:%H:%M}" if t.date() == today else f"{t:%a} {t.day} {t:%b} {t:%H:%M}"

    subs = [
        {
            "sub": s,
            "url": display_url(s.url),
            "synced": when(s.last_synced_at),
            "tried": when(s.last_attempt_at),
            "missing": svc.missing_courses(s.id),
        }
        for s in svc.list()
    ]
    courses = current_app_courses_service().list()[0]
    return render_template(
        "subscriptions.html",
        subs=subs,
        courses=courses,
        course_options=[{"id": c.id, "name": c.name} for c in courses],
    )


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
    back = (
        url_for("pages.ui_calendar", date=day.isoformat(), day=day.isoformat())
        if day
        else url_for("pages.ui_calendar")
    )
    feed = None
    if event is not None and event.subscription_id:
        from web.common import current_app_subscriptions_service

        feed = current_app_subscriptions_service().get(event.subscription_id)
    page = render_template(
        "event_form.html",
        mode=mode,
        event=event,
        error=error,
        feed=feed,
        starts_at_default=starts,
        ends_at_default=ends,
        back_url=back,
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
    return _event_form(
        "new", None, f"{start:%Y-%m-%dT%H:%M}", f"{end:%Y-%m-%dT%H:%M}" if end.date() == d else ""
    )


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
            ev, _ = current_app_events_service().create_recurring(
                **payload, repeat_until=repeat_until
            )
        else:
            ev = current_app_events_service().create(**payload)
    except ValueError as exc:
        return _event_form("new", None, payload["starts_at"], payload["ends_at"] or "", str(exc))
    return _back_to_day(ev.starts_at)


@bp.post("/events/<event_id>/edit")
def update_event_action(event_id: str) -> Any:
    payload = _event_fields()
    existing = current_app_events_service().get(event_id)
    if existing is not None and existing.subscription_id:
        # The calendar it comes from owns the rest (the form shows them locked).
        payload = {"kind": payload["kind"], "course_id": payload["course_id"]}
    try:
        ev = current_app_events_service().update(event_id, **payload)
    except KeyError:
        abort(404)
    except ValueError as exc:
        ev = current_app_events_service().get(event_id)
        return _event_form(
            "edit",
            ev,
            payload.get("starts_at") or _local_input(ev.starts_at),
            payload.get("ends_at") or _local_input(ev.ends_at),
            str(exc),
        )
    return _back_to_day(ev.starts_at)


@bp.post("/events/<event_id>/delete")
def delete_event_action(event_id: str) -> Any:
    ev = current_app_events_service().get(event_id)
    if ev is None or not current_app_events_service().delete(event_id):
        abort(404)
    return _back_to_day(ev.starts_at)


@bp.get("/upload")
def ui_upload() -> Any:
    cfg = current_app.config["AFTERWORD_CONFIG"]
    return render_template(
        "upload.html",
        max_upload_bytes=cfg.max_upload_bytes,
        ffmpeg_available=transcode.ffmpeg_available(cfg.ffmpeg_path),
    )


# --- Course settings --------------------------------------------------
@bp.get("/courses")
def ui_courses() -> Any:
    """Course settings: colours, names, order, adding and deleting
    (all through the courses API). Reached only from the faint gear in
    the desktop Lectures list; used about once a semester."""
    return render_template("courses.html")


@bp.get("/courses/admin")
def ui_courses_admin() -> Any:
    """The old address, kept for bookmarks."""
    return redirect(url_for("pages.ui_courses"), code=301)
