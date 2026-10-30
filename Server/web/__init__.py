"""The web layer: routes as Flask blueprints plus shared helpers.

    common.py        accessors, template filters, display/upload helpers
    pipeline.py      background AI pipeline worker and boot sweep
    pages.py         HTML pages
    api_sessions.py  recordings: device protocol, audio, pipeline
    api_search.py    search (the dashboard's search box)
    api_notes.py, api_tasks.py, api_events.py, api_courses.py

``BLUEPRINTS`` is what ``app.create_app`` registers.
"""
from web import (  # noqa: E402
    api_courses, api_events, api_notes, api_search, api_sessions, api_tasks,
    pages,
)

BLUEPRINTS = (
    pages.bp, api_sessions.bp, api_search.bp, api_notes.bp, api_tasks.bp,
    api_events.bp, api_courses.bp,
)
