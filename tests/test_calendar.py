"""Web UI tests for the month-grid calendar page and event forms.

The grid layout math itself is unit-tested in tests/test_events.py
(MonthGridTests); here we test the Flask routes that render the
template and the form prefill behaviour.
"""
from __future__ import annotations

import datetime as dt
import json
import unittest

from tests.conftest import LRTestCase


def _create_event(self, **overrides):
    payload = {
        "title": "Calc II",
        "kind": "lecture",
        "starts_at": "2027-03-15T10:00:00Z",
        "ends_at": "2027-03-15T11:00:00Z",
    }
    payload.update(overrides)
    return self.client.post(
        "/api/v1/events",
        data=json.dumps(payload),
        content_type="application/json",
    ).get_json()["event"]


class CalendarPageTests(LRTestCase):
    def test_calendar_renders_with_grid(self):
        resp = self.client.get("/calendar?year=2026&month=1")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("January 2026", body)
        for wd in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            self.assertIn(wd, body)
        # 42 cells = 6 rows × 7 days. Each cell div starts with
        # ``class="lr-cal__cell`` (the bare class; some cells also have
        # ``--out`` or ``--today`` modifiers but not all do).
        self.assertEqual(body.count('class="lr-cal__cell'), 42)
        # Prev/next nav present: Jan 2026 prev = Dec 2025, next = Feb 2026.
        self.assertIn("year=2025&amp;month=12", body)
        self.assertIn("year=2026&amp;month=2", body)

    def test_calendar_defaults_to_current_month_when_no_params(self):
        resp = self.client.get("/calendar")
        self.assertEqual(resp.status_code, 200)

    def test_calendar_shows_today_button_when_off_current(self):
        resp = self.client.get("/calendar?year=2026&month=2")
        body = resp.get_data(as_text=True)
        self.assertIn(">Today<", body)

    def test_calendar_hides_today_button_on_current_month(self):
        now = dt.datetime.utcnow()
        resp = self.client.get(
            f"/calendar?year={now.year}&month={now.month}"
        )
        body = resp.get_data(as_text=True)
        self.assertNotIn(">Today<", body)

    def test_calendar_renders_event_chip(self):
        _create_event(self, title="Calc II Lecture")
        resp = self.client.get("/calendar?year=2027&month=3")
        body = resp.get_data(as_text=True)
        self.assertIn("Calc II Lecture", body)
        self.assertIn("lr-cal__chip--lecture", body)

    def test_calendar_renders_meeting_chip(self):
        _create_event(self, title="Standup", kind="meeting",
                      starts_at="2027-03-15T09:00:00Z", ends_at=None)
        resp = self.client.get("/calendar?year=2027&month=3")
        body = resp.get_data(as_text=True)
        self.assertIn("lr-cal__chip--meeting", body)

    def test_calendar_other_chip(self):
        _create_event(self, title="Office Hours", kind="other",
                      starts_at="2027-03-15T16:00:00Z", ends_at=None)
        resp = self.client.get("/calendar?year=2027&month=3")
        body = resp.get_data(as_text=True)
        self.assertIn("lr-cal__chip--other", body)

    def test_calendar_day_cells_are_clickable_links(self):
        # In-month cells link to /events/new?date=YYYY-MM-DD.
        resp = self.client.get("/calendar?year=2026&month=1")
        body = resp.get_data(as_text=True)
        self.assertIn("/events/new?date=2026-01-01", body)
        # Out-of-month cells are also clickable.
        self.assertIn("/events/new?date=2025-12-31", body)

    def test_calendar_out_of_month_cells_have_out_class(self):
        resp = self.client.get("/calendar?year=2026&month=1")
        body = resp.get_data(as_text=True)
        self.assertIn("lr-cal__cell--out", body)

    def test_calendar_ics_import_section_present(self):
        resp = self.client.get("/calendar?year=2026&month=1")
        body = resp.get_data(as_text=True)
        self.assertIn("Import .ics", body)
        self.assertIn("action=\"/api/v1/events/import\"", body)

    def test_calendar_handles_invalid_month(self):
        resp = self.client.get("/calendar?year=2026&month=13")
        self.assertEqual(resp.status_code, 200)
class EventOverflowTests(LRTestCase):
    def test_overflow_link_appears_for_more_than_three_events(self):
        # Four events on the same day → "+ 1 more".
        for i in range(4):
            _create_event(
                self, title=f"Event {i}",
                starts_at=f"2027-03-15T{9 + i:02d}:00:00Z",
                ends_at=None,
            )
        resp = self.client.get("/calendar?year=2027&month=3")
        body = resp.get_data(as_text=True)
        self.assertIn("+ 1 more", body)

    def test_no_overflow_when_three_or_fewer(self):
        for i in range(3):
            _create_event(
                self, title=f"Event {i}",
                starts_at=f"2027-03-15T{9 + i:02d}:00:00Z",
                ends_at=None,
            )
        resp = self.client.get("/calendar?year=2027&month=3")
        body = resp.get_data(as_text=True)
        # Only the "+ New event" header button should contain a "+".
        self.assertNotIn("+ 1 more", body)
        self.assertNotIn("+ 2 more", body)


class EventFormTests(LRTestCase):
    def test_new_event_form_renders_with_date_prefill(self):
        resp = self.client.get("/events/new?date=2027-03-15")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # Noon default for the chosen day.
        self.assertIn('value="2027-03-15T12:00"', body)
        for kind in ("lecture", "meeting", "other"):
            self.assertIn(kind, body)

    def test_new_event_form_without_date_uses_today_noon(self):
        resp = self.client.get("/events/new")
        self.assertEqual(resp.status_code, 200)

    def test_new_event_form_with_invalid_date_falls_back_to_today(self):
        resp = self.client.get("/events/new?date=garbage")
        self.assertEqual(resp.status_code, 200)

    def test_edit_event_form_renders_existing(self):
        ev = _create_event(self, title="Calc II Lecture")
        resp = self.client.get(f"/events/{ev['id']}/edit")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Calc II Lecture", body)
        self.assertIn("lecture", body)

    def test_edit_event_form_404(self):
        resp = self.client.get("/events/nope/edit")
        self.assertEqual(resp.status_code, 404)

    def test_create_event_form_action_redirects(self):
        resp = self.client.post(
            "/events/new",
            data={
                "title": "New Lecture",
                "kind": "lecture",
                "starts_at": "2027-03-15T12:00",
                "ends_at": "",
                "description": "",
                "location": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/calendar", resp.headers["Location"])

    def test_create_event_form_invalid_returns_400(self):
        resp = self.client.post(
            "/events/new",
            data={
                "title": "",
                "kind": "lecture",
                "starts_at": "2027-03-15T12:00",
                "ends_at": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("title is required", resp.get_data(as_text=True))

    def test_update_event_form_action_redirects(self):
        ev = _create_event(self, title="Old Title")
        resp = self.client.post(
            f"/events/{ev['id']}/edit",
            data={
                "title": "New Title",
                "kind": "lecture",
                "starts_at": "2027-03-15T10:00",
                "ends_at": "2027-03-15T11:00",
                "description": "",
                "location": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        get_resp = self.client.get(f"/api/v1/events/{ev['id']}")
        self.assertEqual(
            get_resp.get_json()["event"]["title"], "New Title",
        )

    def test_delete_event_form_action_redirects(self):
        ev = _create_event(self)
        resp = self.client.post(
            f"/events/{ev['id']}/delete",
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            self.client.get(f"/api/v1/events/{ev['id']}").status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()