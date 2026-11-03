"""Timezone rules (timeutil): UTC in storage, naive = Europe/London,
pages in local time. Dates are chosen in British Summer Time (UTC+1)
and across the 25 Oct 2026 clock change, where UTC-only logic is wrong.
"""

from __future__ import annotations

import json
import unittest

import timeutil
from services.tasks import is_due_today, is_overdue
from tests.conftest import LRTestCase


class TimeutilTests(unittest.TestCase):
    def test_naive_is_london_time(self):
        self.assertEqual(timeutil.to_utc_iso("2027-07-10T14:00"), "2027-07-10T13:00:00Z")
        self.assertEqual(timeutil.to_utc_iso("2027-01-10T14:00"), "2027-01-10T14:00:00Z")

    def test_date_only_is_local_midnight(self):
        self.assertEqual(timeutil.to_utc_iso("2027-07-10"), "2027-07-09T23:00:00Z")

    def test_offsets_are_respected(self):
        self.assertEqual(timeutil.to_utc_iso("2027-07-10T14:00:00Z"), "2027-07-10T14:00:00Z")
        self.assertEqual(timeutil.to_utc_iso("2027-07-10T14:00:00+02:00"), "2027-07-10T12:00:00Z")

    def test_to_local(self):
        self.assertEqual(f"{timeutil.to_local('2027-07-10T13:00:00Z'):%H:%M}", "14:00")
        self.assertIsNone(timeutil.to_local(""))
        self.assertIsNone(timeutil.to_local("not a time"))


class TaskDueTests(unittest.TestCase):
    def test_due_date_is_not_overdue_on_its_own_day(self):
        due = timeutil.to_utc_iso("2027-07-10")  # local midnight
        evening = timeutil.to_utc_iso("2027-07-10T22:30")
        self.assertFalse(is_overdue(due, evening))
        self.assertTrue(is_due_today(due, evening))
        self.assertTrue(is_overdue(due, timeutil.to_utc_iso("2027-07-11T00:30")))

    def test_due_time_passes_at_that_moment(self):
        due = timeutil.to_utc_iso("2027-07-10T15:00")
        self.assertFalse(is_overdue(due, timeutil.to_utc_iso("2027-07-10T14:59")))
        self.assertTrue(is_overdue(due, timeutil.to_utc_iso("2027-07-10T15:01")))

    def test_due_today_uses_local_date(self):
        # 23:30 UTC on 9 July is 00:30 on 10 July in London.
        self.assertTrue(
            is_due_today("2027-07-09T23:30:00Z", timeutil.to_utc_iso("2027-07-10T12:00"))
        )


class EventTimezoneTests(LRTestCase):
    def _events(self):
        return self.app.config["AFTERWORD_EVENTS_SERVICE"]

    def test_form_time_is_local(self):
        ev = self._events().create(title="Lecture", kind="lecture", starts_at="2027-07-10T10:00")
        self.assertEqual(ev.starts_at, "2027-07-10T09:00:00Z")

    def test_weekly_series_keeps_local_time_across_clock_change(self):
        # Mondays 10:00, 19 Oct 2026 (BST) to 2 Nov 2026 (GMT).
        _, n = self._events().create_recurring(
            title="Lecture",
            kind="lecture",
            starts_at="2026-10-19T10:00",
            ends_at="2026-10-19T11:00",
            repeat_until="2026-11-02",
        )
        self.assertEqual(n, 3)
        evs, _ = self._events().list()
        starts = sorted(e.starts_at for e in evs)
        self.assertEqual(
            starts, ["2026-10-19T09:00:00Z", "2026-10-26T10:00:00Z", "2026-11-02T10:00:00Z"]
        )
        self.assertTrue(all(f"{timeutil.to_local(s):%H:%M}" == "10:00" for s in starts))
        ends = sorted(e.ends_at for e in evs)
        self.assertTrue(all(f"{timeutil.to_local(s):%H:%M}" == "11:00" for s in ends))

    def test_calendar_uses_local_date(self):
        self._events().create(
            title="Late", kind="other", starts_at="2027-07-01T23:30:00Z"
        )  # 00:30 BST, 2 July

        def day_panel(day):
            body = self.client.get(f"/calendar?day={day}").get_data(as_text=True)
            return body[body.index('id="cday-title"') :]

        on_2nd, on_1st = day_panel("2027-07-02"), day_panel("2027-07-01")
        self.assertIn("Late", on_2nd)
        self.assertIn("00:30", on_2nd)
        self.assertNotIn("Late", on_1st)

    def test_ics_floating_time_is_local(self):
        ics = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:a@b\r\n"
            b"SUMMARY:Seminar\r\nDTSTART:20270710T140000\r\n"
            b"DTEND:20270710T150000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        self.assertEqual(self._events().import_ics(ics), 1)
        ev = self._events().list()[0][0]
        self.assertEqual(
            (ev.starts_at, ev.ends_at), ("2027-07-10T13:00:00Z", "2027-07-10T14:00:00Z")
        )


class SessionTimezoneTests(LRTestCase):
    def _announce(self, started_at):
        return self.client.post(
            "/api/v1/sessions",
            data=json.dumps({"id": "tz_session_1", "expected_count": 1, "started_at": started_at}),
            content_type="application/json",
        )

    def test_device_local_started_at_is_stored_as_utc(self):
        resp = self._announce("2027-07-10T14:00:00")  # what the firmware sends
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["session"]["started_at"], "2027-07-10T13:00:00Z")

    def test_bad_started_at_is_rejected(self):
        self.assertEqual(self._announce("yesterday").status_code, 400)

    def test_pages_show_local_time(self):
        self._announce("2027-07-10T14:00:00")
        page = self.client.get("/other?session=tz_session_1").get_data(as_text=True)
        self.assertIn("10 Jul 2027, 14:00", page)
        self.assertNotIn("â€”", page)  # the old mojibake dash

    def test_old_naive_started_at_is_migrated(self):
        store = self.app.config["AFTERWORD_STORAGE"]
        self._announce("2027-07-10T14:00:00")
        with store._tx() as conn:
            conn.execute("UPDATE sessions SET started_at = '2027-07-10T14:00:00'")
        store.initialise()  # runs at every start
        self.assertEqual(store.get_session("tz_session_1").started_at, "2027-07-10T13:00:00Z")


if __name__ == "__main__":
    unittest.main()
