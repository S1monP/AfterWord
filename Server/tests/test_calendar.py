"""Web UI tests for the Calendar page (month, week, the day) and the
event forms.
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
    def _get(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _day_panel(self, url):
        body = self._get(url)
        return body[body.index('id="cday-title"'):]

    def test_month_renders_42_days(self):
        body = self._get("/calendar?date=2026-01-10")
        self.assertIn("January 2026", body)
        for wd in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            self.assertIn(wd, body)
        self.assertEqual(body.count('role="gridcell"'), 42)
        # Previous / next month.
        self.assertIn("date=2025-12-01", body)
        self.assertIn("date=2026-02-01", body)
        self.assertIn("mcell is-out", body)

    def test_defaults_and_bad_params(self):
        self._get("/calendar")
        self._get("/calendar?date=garbage&day=nope&view=year")
        self.assertIn(">Today<", self._get("/calendar"))

    def test_day_cells_choose_the_day(self):
        body = self._get("/calendar?date=2026-01-10")
        self.assertIn("day=2026-01-01", body)
        self.assertIn("day=2025-12-29", body)   # out-of-month cells too

    def test_event_chip_and_day_panel(self):
        _create_event(self, title="Calc II Lecture", location="Room 4")
        body = self._get("/calendar?date=2027-03-01")
        self.assertIn("Calc II Lecture", body)
        panel = self._day_panel("/calendar?day=2027-03-15")
        self.assertIn("Monday 15 March", panel)
        self.assertIn("1 event", panel)
        self.assertIn("Room 4", panel)
        self.assertIn("Add an event on this day", panel)
        self.assertIn("/events/new?date=2027-03-15", panel)

    def test_deadlines_show_as_due_chips(self):
        self.client.post("/api/v1/tasks", data=json.dumps({
            "title": "Problem set 4", "kind": "deadline", "due_at": "2027-03-16T23:59:00Z",
        }), content_type="application/json")
        body = self._get("/calendar?date=2027-03-01")
        self.assertIn("Due · Problem set 4", body)
        panel = self._day_panel("/calendar?day=2027-03-16")
        self.assertIn("1 deadline", panel)

    def test_overflow_after_three(self):
        for i in range(4):
            _create_event(self, title=f"Event {i}",
                          starts_at=f"2027-03-15T{9 + i:02d}:00:00Z", ends_at=None)
        body = self._get("/calendar?date=2027-03-01")
        self.assertIn("+ 1 more", body)
        # The day panel lists all four.
        self.assertIn("Event 3", self._day_panel("/calendar?day=2027-03-15"))

    def test_no_overflow_when_three_or_fewer(self):
        for i in range(3):
            _create_event(self, title=f"Event {i}",
                          starts_at=f"2027-03-15T{9 + i:02d}:00:00Z", ends_at=None)
        self.assertNotIn(" more</span>", self._get("/calendar?date=2027-03-01"))

    def test_week_view(self):
        _create_event(self, title="Calc II Lecture")
        body = self._get("/calendar?view=week&date=2027-03-17")
        self.assertIn("15 Mar – 21 Mar 2027", body)
        self.assertIn('class="wgrid', body)
        self.assertIn("Calc II Lecture", body)
        # Steps a week at a time, keeping the view.
        self.assertIn("view=week&amp;date=2027-03-08", body)
        self.assertIn("view=week&amp;date=2027-03-22", body)

    def test_subscriptions_link(self):
        # Subscriptions and .ics imports live on their own page.
        body = self._get("/calendar")
        self.assertIn('href="/calendar/subscriptions"', body)
        self.assertIn("Import a file", self._get("/calendar/subscriptions"))


class EventFormTests(LRTestCase):
    def test_new_event_form_renders_with_date_prefill(self):
        resp = self.client.get("/events/new?date=2027-03-15")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # 09:00 to 10:00 on another day than today.
        self.assertIn('value="2027-03-15T09:00"', body)
        self.assertIn('value="2027-03-15T10:00"', body)
        # Back goes to that day.
        self.assertIn("/calendar?date=2027-03-15&amp;day=2027-03-15", body)
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
        self.assertIn("/calendar?date=2027-03-15&day=2027-03-15", resp.headers["Location"])

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