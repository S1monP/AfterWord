"""Tests for the Events service: validation, CRUD, match-confidence math,
month-grid layout, and ICS import.

The HTTP API tests live in tests/test_api.py (added in Commit 2, which
introduces the Flask routes for events). The web UI tests also live
in a separate file (``tests/test_calendar.py``) added in Commit 2.

All tests use the temp-dir storage helper from conftest.LRTestCase.
"""
from __future__ import annotations

import json
import unittest

from tests.conftest import LRTestCase


# A minimal hand-crafted .ics calendar with three importable VEVENTs
# plus one malformed one (no SUMMARY). Used by the import tests.
_SAMPLE_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test//EN
BEGIN:VEVENT
UID:event-1@test
SUMMARY:Calc II Lecture
DTSTART:20270315T100000Z
DTEND:20270315T110000Z
LOCATION:Room 101
END:VEVENT
BEGIN:VEVENT
UID:event-2@test
SUMMARY:Sprint Planning
DTSTART;VALUE=DATE:20270316
DTEND;VALUE=DATE:20270316
DESCRIPTION:Weekly sync
END:VEVENT
BEGIN:VEVENT
SUMMARY:No UID event
DTSTART:20270317T090000Z
END:VEVENT
BEGIN:VEVENT
DTSTART:20270318T100000Z
END:VEVENT
END:VCALENDAR
"""
# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class EventsServiceTests(LRTestCase):
    def _service(self):
        return self.app.config["LR_EVENTS_SERVICE"]

    def _create(self, **overrides):
        payload = {
            "title": "Calc II",
            "kind": "lecture",
            "starts_at": "2027-03-15T10:00:00Z",
            "ends_at": "2027-03-15T11:00:00Z",
            "location": "Room 101",
        }
        payload.update(overrides)
        return self._service().create(**payload)

    def test_create_persists_defaults(self):
        ev = self._service().create(
            title="Lecture", kind="lecture",
            starts_at="2027-03-15T10:00:00Z",
        )
        self.assertEqual(ev.title, "Lecture")
        self.assertEqual(ev.kind, "lecture")
        self.assertEqual(ev.source, "manual")
        self.assertIsNone(ev.ends_at)
        self.assertIsNone(ev.description)
        again = self._service().get(ev.id)
        self.assertEqual(again.id, ev.id)

    def test_create_rejects_blank_title(self):
        svc = self._service()
        with self.assertRaises(ValueError):
            svc.create(title="   ", kind="lecture", starts_at="2027-03-15")
        with self.assertRaises(ValueError):
            svc.create(title="", kind="lecture", starts_at="2027-03-15")

    def test_create_rejects_too_long_title(self):
        with self.assertRaises(ValueError):
            self._create(title="x" * 201)

    def test_create_rejects_control_characters(self):
        with self.assertRaises(ValueError):
            self._create(title="hello\x00world")

    def test_create_rejects_bad_kind(self):
        with self.assertRaises(ValueError):
            self._create(kind="gathering")

    def test_create_rejects_bad_source(self):
        with self.assertRaises(ValueError):
            self._create(source="phone_call")

    def test_create_normalises_date_only(self):
        ev = self._create(starts_at="2027-03-15")
        self.assertEqual(ev.starts_at, "2027-03-15T00:00:00Z")

    def test_create_normalises_naive_datetime(self):
        ev = self._create(
            starts_at="2027-03-15T14:30", ends_at=None,
        )
        self.assertEqual(ev.starts_at, "2027-03-15T14:30:00Z")

    def test_create_normalises_offset_timezone_to_utc(self):
        # 10:00 in +01:00 == 09:00 UTC.
        ev = self._create(starts_at="2027-03-15T10:00:00+01:00")
        self.assertEqual(ev.starts_at, "2027-03-15T09:00:00Z")

    def test_create_rejects_invalid_timestamp(self):
        with self.assertRaises(ValueError):
            self._create(starts_at="not-a-date")

    def test_create_rejects_ends_before_starts(self):
        with self.assertRaises(ValueError):
            self._create(
                starts_at="2027-03-15T10:00:00Z",
                ends_at="2027-03-15T09:00:00Z",
            )
        with self.assertRaises(ValueError):
            self._create(
                starts_at="2027-03-15T10:00:00Z",
                ends_at="2027-03-15T10:00:00Z",
            )

    def test_create_rejects_too_long_description(self):
        with self.assertRaises(ValueError):
            self._create(description="x" * 2001)

    def test_update_partial_fields(self):
        ev = self._create()
        updated = self._service().update(ev.id, title="Calc III")
        self.assertEqual(updated.title, "Calc III")
        self.assertEqual(updated.kind, ev.kind)

    def test_update_ends_at_validates_against_new_starts_at(self):
        ev = self._create()
        with self.assertRaises(ValueError):
            self._service().update(
                ev.id,
                starts_at="2027-03-15T10:00:00Z",
                ends_at="2027-03-15T09:00:00Z",
            )

    def test_update_missing_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self._service().update("nope", title="x")

    def test_delete_removes_row(self):
        ev = self._create()
        self.assertTrue(self._service().delete(ev.id))
        self.assertIsNone(self._service().get(ev.id))
        self.assertFalse(self._service().delete(ev.id))

    def test_get_returns_none_for_missing(self):
        self.assertIsNone(self._service().get("nope"))

    def test_delete_requires_event_id(self):
        with self.assertRaises(ValueError):
            self._service().delete("")

    def test_list_returns_sorted_by_starts_at(self):
        a = self._create(starts_at="2027-03-15T10:00:00Z", title="A", ends_at=None)
        b = self._create(starts_at="2027-03-15T09:00:00Z", title="B", ends_at=None)
        c = self._create(starts_at="2027-03-15T11:00:00Z", title="C", ends_at=None)
        events, total = self._service().list()
        self.assertEqual(total, 3)
        self.assertEqual([e.id for e in events], [b.id, a.id, c.id])

    def test_list_filter_by_kind(self):
        a = self._create(title="Lecture", kind="lecture", ends_at=None)
        b = self._create(title="Meeting", kind="meeting", ends_at=None)
        events, total = self._service().list(kind="lecture")
        self.assertEqual(total, 1)
        self.assertEqual(events[0].id, a.id)

    def test_list_filter_by_date_range(self):
        self._create(starts_at="2027-03-10T10:00:00Z", title="Mar 10", ends_at=None)
        b = self._create(starts_at="2027-03-20T10:00:00Z", title="Mar 20", ends_at=None)
        self._create(starts_at="2027-03-30T10:00:00Z", title="Mar 30", ends_at=None)
        events, _ = self._service().list(
            starts_after="2027-03-15T00:00:00Z",
            starts_before="2027-03-25T23:59:59Z",
        )
        self.assertEqual([e.id for e in events], [b.id])

    def test_list_validates_inputs(self):
        with self.assertRaises(ValueError):
            self._service().list(kind="gathering")
        with self.assertRaises(ValueError):
            self._service().list(starts_after="not-a-date")
        with self.assertRaises(ValueError):
            self._service().list(limit=0)
class SuggestMatchConfidenceTests(unittest.TestCase):
    """The pure function ``suggest_match_confidence`` — no DB needed."""

    def test_at_start_returns_one(self):
        self.assertEqual(
            suggest_match_confidence(
                "2026-01-15T10:00:00Z",
                "2026-01-15T10:00:00Z",
                "2026-01-15T11:00:00Z",
            ),
            1.0,
        )

    def test_at_end_returns_half(self):
        self.assertEqual(
            suggest_match_confidence(
                "2026-01-15T11:00:00Z",
                "2026-01-15T10:00:00Z",
                "2026-01-15T11:00:00Z",
            ),
            0.5,
        )

    def test_at_pre_window_edge_returns_half(self):
        self.assertEqual(
            suggest_match_confidence(
                "2026-01-15T09:50:00Z",
                "2026-01-15T10:00:00Z",
                "2026-01-15T11:00:00Z",
            ),
            0.5,
        )

    def test_midpoint_returns_075(self):
        self.assertEqual(
            suggest_match_confidence(
                "2026-01-15T09:55:00Z",
                "2026-01-15T10:00:00Z",
                "2026-01-15T11:00:00Z",
            ),
            0.75,
        )

    def test_after_end_returns_none(self):
        self.assertIsNone(
            suggest_match_confidence(
                "2026-01-15T11:00:01Z",
                "2026-01-15T10:00:00Z",
                "2026-01-15T11:00:00Z",
            ),
        )

    def test_before_pre_window_returns_none(self):
        self.assertIsNone(
            suggest_match_confidence(
                "2026-01-15T09:49:59Z",
                "2026-01-15T10:00:00Z",
                "2026-01-15T11:00:00Z",
            ),
        )

    def test_no_ends_at_defaults_to_one_hour(self):
        self.assertEqual(
            suggest_match_confidence(
                "2026-01-15T10:30:00Z",
                "2026-01-15T10:00:00Z",
                None,
            ),
            0.75,
        )

    def test_returns_none_for_unparseable(self):
        self.assertIsNone(
            suggest_match_confidence("garbage", "2026-01-15T10:00:00Z", None),
        )
class MatchSessionTests(LRTestCase):
    def _service(self):
        return self.app.config["LR_EVENTS_SERVICE"]

    def _store(self):
        return self.app.config["LR_STORAGE"]

    def _seed_session(self) -> str:
        sess_id = "s" + "0" * 31
        self._store().upsert_session(
            sess_id,
            title="test",
            expected_count=1,
            sample_rate=22050,
            channels=1,
            bits_per_sample=32,
            started_at="2026-01-15T10:00:00Z",
        )
        return sess_id

    def test_match_session_sets_fields(self):
        ev = self._service().create(
            title="Lecture",
            kind="lecture",
            starts_at="2026-01-15T10:00:00Z",
            ends_at="2026-01-15T11:00:00Z",
        )
        sid = self._seed_session()
        out = self._service().match_session(ev.id, sid, 0.9)
        self.assertEqual(out.matched_session_id, sid)
        self.assertEqual(out.match_confidence, 0.9)

    def test_match_session_clears_other_matches(self):
        ev_a = self._service().create(
            title="A", kind="lecture",
            starts_at="2026-01-15T10:00:00Z",
        )
        ev_b = self._service().create(
            title="B", kind="lecture",
            starts_at="2026-01-15T10:00:00Z",
        )
        sid = self._seed_session()
        self._service().match_session(ev_a.id, sid, 0.8)
        self._service().match_session(ev_b.id, sid, 0.7)
        # ev_a should have been cleared.
        again_a = self._service().get(ev_a.id)
        self.assertIsNone(again_a.matched_session_id)
        again_b = self._service().get(ev_b.id)
        self.assertEqual(again_b.matched_session_id, sid)

    def test_match_session_validates_confidence(self):
        ev = self._service().create(
            title="A", kind="lecture",
            starts_at="2026-01-15T10:00:00Z",
        )
        sid = self._seed_session()
        with self.assertRaises(ValueError):
            self._service().match_session(ev.id, sid, 1.5)
        with self.assertRaises(ValueError):
            self._service().match_session(ev.id, sid, -0.1)
        with self.assertRaises(ValueError):
            self._service().match_session(ev.id, sid, "high")  # type: ignore

    def test_match_session_missing_event_raises_keyerror(self):
        sid = self._seed_session()
        with self.assertRaises(KeyError):
            self._service().match_session("nope", sid, 0.5)

    def test_match_session_missing_session_raises_keyerror(self):
        ev = self._service().create(
            title="A", kind="lecture",
            starts_at="2026-01-15T10:00:00Z",
        )
        with self.assertRaises(KeyError):
            self._service().match_session(ev.id, "nope", 0.5)

    def test_suggest_match_returns_best_event(self):
        ev_good = self._service().create(
            title="Good match",
            kind="lecture",
            starts_at="2026-01-15T10:00:00Z",
            ends_at="2026-01-15T11:00:00Z",
        )
        self._service().create(
            title="Far away",
            kind="lecture",
            starts_at="2026-01-15T15:00:00Z",
            ends_at="2026-01-15T16:00:00Z",
        )
        sid = self._seed_session()
        match = self._service().suggest_match(sid)
        self.assertIsNotNone(match)
        self.assertEqual(match.id, ev_good.id)

    def test_suggest_match_returns_none_when_no_match(self):
        self._service().create(
            title="Far away",
            kind="lecture",
            starts_at="2026-03-15T10:00:00Z",
        )
        sid = self._seed_session()  # 2026-01-15
        self.assertIsNone(self._service().suggest_match(sid))
class ImportIcsTests(LRTestCase):
    def _service(self):
        return self.app.config["LR_EVENTS_SERVICE"]

    def test_import_ics_returns_count_and_persists(self):
        count = self._service().import_ics(_SAMPLE_ICS)
        self.assertEqual(count, 3)
        events, total = self._service().list()
        self.assertEqual(total, 3)
        for ev in events:
            self.assertEqual(ev.kind, "meeting")
            self.assertEqual(ev.source, "ical_import")
            # source_ref may be None for events that didn't carry a UID.

    def test_import_ics_timed_event(self):
        self._service().import_ics(_SAMPLE_ICS)
        evs, _ = self._service().list()
        ev_timed = next(e for e in evs if e.title == "Calc II Lecture")
        self.assertEqual(ev_timed.starts_at, "2027-03-15T10:00:00Z")
        self.assertEqual(ev_timed.ends_at, "2027-03-15T11:00:00Z")
        self.assertEqual(ev_timed.location, "Room 101")

    def test_import_ics_all_day_event_normalised_to_noon(self):
        self._service().import_ics(_SAMPLE_ICS)
        evs, _ = self._service().list()
        ev_allday = next(e for e in evs if e.title == "Sprint Planning")
        self.assertEqual(ev_allday.starts_at, "2027-03-16T12:00:00Z")
        # Single-day all-day event: DTSTART == DTEND, so we set
        # ends_at to 23:59 of that day so it sorts after the noon start.
        self.assertEqual(ev_allday.ends_at, "2027-03-16T23:59:59Z")
        self.assertEqual(ev_allday.description, "Weekly sync")

    def test_import_ics_empty_bytes_returns_zero(self):
        self.assertEqual(self._service().import_ics(b""), 0)

    def test_import_ics_invalid_bytes_returns_zero(self):
        # Not .ics; icalendar raises a parsing error. We don't blow up;
        # we just return 0.
        self.assertEqual(self._service().import_ics(b"not ics"), 0)

    def test_import_ics_skips_events_without_title(self):
        # The 4th VEVENT in _SAMPLE_ICS has no SUMMARY.
        count = self._service().import_ics(_SAMPLE_ICS)
        self.assertEqual(count, 3)
class MonthGridTests(unittest.TestCase):
    def test_grid_is_always_6_rows_7_cols(self):
        for y, m in [(2026, 1), (2026, 2), (2026, 12), (2024, 2)]:
            grid = compute_month_grid(y, m, [])
            self.assertEqual(len(grid), 6)
            for row in grid:
                self.assertEqual(len(row), 7)

    def test_jan_2026_first_row(self):
        grid = compute_month_grid(2026, 1, [])
        dates = [str(c["date"]) for c in grid[0]]
        # Jan 1 2026 is Thursday = Mon-first index 3.
        self.assertEqual(dates, [
            "2025-12-29", "2025-12-30", "2025-12-31",
            "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04",
        ])
        self.assertEqual(grid[0][3]["date"].day, 1)
        self.assertTrue(grid[0][3]["in_month"])
        for c in grid[0][:3]:
            self.assertFalse(c["in_month"])

    def test_feb_2026_grid_starts_at_jan_26(self):
        grid = compute_month_grid(2026, 2, [])
        # Feb 1 2026 is Sunday = weekday()=6, so grid_start is Jan 26.
        self.assertEqual(grid[0][0]["date"].isoformat(), "2026-01-26")
        self.assertEqual(grid[0][6]["date"].isoformat(), "2026-02-01")
        self.assertTrue(grid[0][6]["in_month"])

    def test_in_month_flag_correct_for_short_month(self):
        grid = compute_month_grid(2026, 2, [])
        self.assertFalse(grid[4][6]["in_month"])
        last_in = None
        for row in grid:
            for c in row:
                if c["in_month"]:
                    last_in = c
        self.assertEqual(last_in["date"].isoformat(), "2026-02-28")

    def test_grid_contains_events_on_correct_dates(self):
        ev = _make_event("2026-01-15T10:00:00Z", "e1")
        grid = compute_month_grid(2026, 1, [ev])
        # Jan 15 2026 is a Thursday = Mon-first index 3, in row 2.
        cell = grid[2][3]
        self.assertEqual(cell["date"].isoformat(), "2026-01-15")
        self.assertEqual(len(cell["events"]), 1)
        self.assertEqual(cell["events"][0].id, "e1")

    def test_grid_caps_events_per_cell_with_overflow(self):
        events = [
            _make_event(f"2026-01-15T{i:02d}:00:00Z", f"e{i}")
            for i in range(5)
        ]
        grid = compute_month_grid(2026, 1, events)
        cell = grid[2][3]
        self.assertEqual(len(cell["events"]), 3)
        self.assertEqual(cell["overflow"], 2)


class RecurringEventTests(LRTestCase):
    """Weekly recurrence: ticking the box materialises one event per
    occurrence, so the calendar, the dashboard, and the pipeline
    time-matcher treat them like any other event."""

    def _create(self, payload):
        return self.client.post(
            "/api/v1/events",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_recurring_makes_weekly_occurrences(self):
        resp = self._create({
            "title": "Linear Algebra", "kind": "lecture",
            "starts_at": "2026-09-01T10:00:00Z",
            "ends_at": "2026-09-01T11:00:00Z",
            "repeat_until": "2026-09-15",
        })
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertEqual(body["created_count"], 3)
        first_id = body["event"]["id"]
        events, total = self.app.config["LR_EVENTS_SERVICE"].list(
            limit=500,
        )
        self.assertEqual(total, 3)
        self.assertEqual(
            [e.starts_at for e in events],
            [
                "2026-09-01T10:00:00Z",
                "2026-09-08T10:00:00Z",
                "2026-09-15T10:00:00Z",
            ],
        )
        for e in events:
            self.assertEqual(e.title, "Linear Algebra")
            self.assertEqual(e.ends_at, e.starts_at.replace("10", "11"))
        self.assertEqual(events[0].id, first_id)

    def test_no_repeat_creates_single_event(self):
        resp = self._create({
            "title": "One-off", "kind": "meeting",
            "starts_at": "2026-09-02T10:00:00Z",
        })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["created_count"], 1)
        _, total = self.app.config["LR_EVENTS_SERVICE"].list(limit=500)
        self.assertEqual(total, 1)

    def test_repeat_until_before_start_rejected(self):
        resp = self._create({
            "title": "Backwards", "kind": "lecture",
            "starts_at": "2026-09-01T10:00:00Z",
            "repeat_until": "2026-08-25",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("start date", resp.get_json()["detail"])

    def test_repeat_cap_enforced(self):
        # A year of weekly events = 53 occurrences > the 52 cap.
        resp = self._create({
            "title": "Forever", "kind": "lecture",
            "starts_at": "2026-09-01T10:00:00Z",
            "repeat_until": "2027-09-01",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("maximum", resp.get_json()["detail"])


def _make_event(starts_at: str, eid: str):
    """Build a minimal Event for tests."""
    return Event(
        id=eid, title=eid, description=None, kind="lecture",
        starts_at=starts_at, ends_at=None, location=None,
        source="manual", source_ref=None, course_id=None,
        matched_session_id=None, match_confidence=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


# Imports for the pure helpers tested above (kept at the bottom so the
# service-level tests above don't accidentally rely on them being in
# scope at import time).
from services.events import (  # noqa: E402
    Event, compute_month_grid, suggest_match_confidence,
)


if __name__ == "__main__":
    unittest.main()