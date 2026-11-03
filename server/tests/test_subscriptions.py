"""Calendar subscriptions and .ics imports (services/calendar_feeds.py).

The feed is served from memory (``fetch`` is replaced), so these never
touch the network. Dates are relative to today, so the sync window
always covers them.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from unittest import mock

import timeutil
from services import calendar_feeds
from tests.conftest import LRTestCase

TODAY = timeutil.local_today()


def day(offset: int) -> str:
    return (TODAY + dt.timedelta(days=offset)).strftime("%Y%m%d")


def vevent(uid, title, offset, hour=10, *, extra="", minutes=90):
    start = dt.datetime.combine(TODAY + dt.timedelta(days=offset), dt.time(hour))
    end = start + dt.timedelta(minutes=minutes)
    return (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{title}\r\n"
        f"DTSTART;TZID=Europe/London:{start:%Y%m%dT%H%M%S}\r\n"
        f"DTEND;TZID=Europe/London:{end:%Y%m%dT%H%M%S}\r\n"
        f"LOCATION:Room 1\r\n{extra}END:VEVENT\r\n"
    )


def calendar(*events, name="Uni timetable"):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        f"X-WR-CALNAME:{name}\r\n" + "".join(events) + "END:VCALENDAR\r\n"
    ).encode()


URL = "webcal://timetable.example.ac.uk/ical?token=secret"


class _Base(LRTestCase):
    def setUp(self):
        super().setUp()
        self.feed = calendar(
            vevent("a", "MATH101 Algebra/Lecture 01", 1), vevent("b", "PHY202 Optics Lab 2", 2, 14)
        )
        patcher = mock.patch.object(calendar_feeds, "fetch", side_effect=lambda url: self.feed)
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = self.app.config["AFTERWORD_SUBSCRIPTIONS_SERVICE"]
        self.store = self.app.config["AFTERWORD_STORAGE"]

    def events(self):
        return self.store.list_events(limit=5000)[0]

    def titles(self):
        return sorted(e.title for e in self.events())


class SubscribeAndSyncTests(_Base):
    def test_subscribe_adds_events_with_the_kind_chosen(self):
        sub, result = self.svc.add(URL, kind="lecture")
        self.assertEqual(result.added, 2)
        self.assertEqual(sub.name, "Uni timetable")
        self.assertEqual(sub.url, "https://timetable.example.ac.uk/ical?token=secret")
        self.assertEqual(sub.event_count, 2)
        self.assertEqual({e.kind for e in self.events()}, {"lecture"})
        self.assertEqual({e.source for e in self.events()}, {"subscription"})

    def test_syncing_again_never_duplicates(self):
        sub, _ = self.svc.add(URL)
        for _ in range(3):
            result = self.svc.sync(sub.id)
            self.assertEqual((result.added, result.updated, result.removed), (0, 0, 0))
        self.assertEqual(len(self.events()), 2)

    def test_moved_and_renamed_events_are_updated_in_place(self):
        sub, _ = self.svc.add(URL)
        before = {e.source_ref: e.id for e in self.events()}
        self.feed = calendar(
            vevent("a", "MATH101 Algebra/Lecture 01 (new room)", 3, 9),
            vevent("b", "PHY202 Optics Lab 2", 2, 14),
        )
        result = self.svc.sync(sub.id)
        self.assertEqual((result.added, result.updated, result.removed), (0, 1, 0))
        moved = self.store.get_event(before["a"])
        self.assertEqual(moved.title, "MATH101 Algebra/Lecture 01 (new room)")
        self.assertTrue(moved.starts_at.startswith((TODAY + dt.timedelta(days=3)).isoformat()))

    def test_events_gone_from_the_feed_or_cancelled_are_removed(self):
        sub, _ = self.svc.add(URL)
        self.feed = calendar(
            vevent("a", "MATH101 Algebra/Lecture 01", 1, extra="STATUS:CANCELLED\r\n")
        )
        result = self.svc.sync(sub.id)
        self.assertEqual(result.removed, 2)
        self.assertEqual(self.events(), [])

    def test_past_events_outside_the_window_are_kept(self):
        sub, _ = self.svc.add(URL)
        old = self.store.create_event(
            "old",
            title="Last year",
            kind="lecture",
            starts_at="2020-01-01T10:00:00Z",
            source="subscription",
            source_ref="gone-long-ago",
            subscription_id=sub.id,
        )
        self.svc.sync(sub.id)
        self.assertIsNotNone(self.store.get_event(old.id))

    def test_a_failed_fetch_changes_nothing_and_is_reported(self):
        sub, _ = self.svc.add(URL)
        self.fetch.side_effect = calendar_feeds.FeedError(
            "Couldn't reach the calendar (timed out)."
        )
        with self.assertRaises(calendar_feeds.FeedError):
            self.svc.sync(sub.id)
        self.assertEqual(len(self.events()), 2)
        self.assertIn("timed out", self.svc.get(sub.id).last_error)
        # A login page instead of a calendar: also nothing removed.
        self.fetch.side_effect = lambda url: b"<html>Please log in</html>"
        with self.assertRaises(calendar_feeds.FeedError):
            self.svc.sync(sub.id)
        self.assertEqual(len(self.events()), 2)
        # Working again: the error clears.
        self.fetch.side_effect = lambda url: self.feed
        self.svc.sync(sub.id)
        self.assertIsNone(self.svc.get(sub.id).last_error)

    def test_a_link_that_isnt_a_calendar_is_not_saved(self):
        self.fetch.side_effect = lambda url: b"<!doctype html><title>Login</title>"
        with self.assertRaises(calendar_feeds.FeedError):
            self.svc.add(URL)
        self.assertEqual(self.svc.list(), [])
        with self.assertRaises(ValueError):
            self.svc.add("ftp://example.com/cal.ics")
        with self.assertRaises(ValueError):
            self.svc.add("not a link")

    def test_the_same_link_twice_is_refused(self):
        self.svc.add(URL)
        with self.assertRaises(ValueError):
            self.svc.add("https://timetable.example.ac.uk/ical?token=secret")
        self.assertEqual(len(self.events()), 2)


class RepeatingEventTests(_Base):
    def test_an_endless_series_stays_inside_the_window(self):
        self.feed = calendar(vevent("forever", "Weekly seminar", 0, extra="RRULE:FREQ=WEEKLY\r\n"))
        sub, result = self.svc.add(URL)
        weeks = (calendar_feeds.WINDOW_AHEAD.days // 7) + 1
        self.assertLessEqual(result.added, weeks)
        self.assertGreaterEqual(result.added, weeks - 1)
        latest = max(e.starts_at for e in self.events())
        self.assertLessEqual(latest[:10], (TODAY + calendar_feeds.WINDOW_AHEAD).isoformat())
        # And again: still the same events.
        self.assertEqual(self.svc.sync(sub.id).added, 0)

    def test_skipped_moved_and_cancelled_weeks(self):
        moved_from = dt.datetime.combine(TODAY + dt.timedelta(days=14), dt.time(10))
        cancelled = dt.datetime.combine(TODAY + dt.timedelta(days=21), dt.time(10))
        self.feed = calendar(
            vevent(
                "s",
                "Weekly seminar",
                0,
                extra=f"RRULE:FREQ=WEEKLY;COUNT=5\r\n"
                f"EXDATE;TZID=Europe/London:{(TODAY + dt.timedelta(days=7)):%Y%m%d}T100000\r\n",
            ),
            vevent(
                "s",
                "Weekly seminar (moved)",
                15,
                15,
                extra=f"RECURRENCE-ID;TZID=Europe/London:{moved_from:%Y%m%dT%H%M%S}\r\n",
            ),
            vevent(
                "s",
                "Weekly seminar",
                21,
                extra=f"RECURRENCE-ID;TZID=Europe/London:{cancelled:%Y%m%dT%H%M%S}\r\nSTATUS:CANCELLED\r\n",
            ),
        )
        self.svc.add(URL)
        # 5 weeks, minus the skipped one and the cancelled one.
        self.assertEqual(
            self.titles(), ["Weekly seminar", "Weekly seminar", "Weekly seminar (moved)"]
        )

    def test_too_many_events_are_refused(self):
        self.feed = calendar(
            vevent("daily", "Every hour", 0, extra="RRULE:FREQ=HOURLY\r\n", minutes=30)
        )
        with self.assertRaises(calendar_feeds.FeedError):
            self.svc.add(URL)
        self.assertEqual(self.events(), [])


class YourChangesStayTests(_Base):
    def test_kind_and_course_you_set_survive_a_sync(self):
        course = self.app.config["AFTERWORD_COURSES_SERVICE"].create(name="Elsewhere")
        sub, _ = self.svc.add(URL, kind="lecture")
        ev = next(e for e in self.events() if e.source_ref == "a")
        self.store.update_event(ev.id, kind="meeting", course_id=course.id)
        self.feed = calendar(
            vevent("a", "MATH101 Algebra/Lecture 01", 1, 11),
            vevent("b", "PHY202 Optics Lab 2", 2, 14),
        )
        self.svc.sync(sub.id)
        after = self.store.get_event(ev.id)
        self.assertEqual((after.kind, after.course_id), ("meeting", course.id))
        self.assertNotEqual(after.starts_at, ev.starts_at)

    def test_an_event_you_delete_stays_deleted(self):
        sub, _ = self.svc.add(URL)
        ev = next(e for e in self.events() if e.source_ref == "a")
        self.app.config["AFTERWORD_EVENTS_SERVICE"].delete(ev.id)
        result = self.svc.sync(sub.id)
        self.assertEqual((result.added, result.skipped), (0, 1))
        self.assertEqual([e.source_ref for e in self.events()], ["b"])

    def test_changing_the_kind_applies_to_all_its_events(self):
        sub, _ = self.svc.add(URL, kind="lecture")
        self.svc.update(sub.id, kind="other", name="Renamed")
        self.assertEqual({e.kind for e in self.events()}, {"other"})
        self.assertEqual(self.svc.get(sub.id).name, "Renamed")

    def test_removing_a_subscription_removes_its_events_only(self):
        manual = self.app.config["AFTERWORD_EVENTS_SERVICE"].create(
            title="Dentist", kind="other", starts_at="2030-01-01T10:00:00Z"
        )
        sub, _ = self.svc.add(URL)
        self.assertTrue(self.svc.delete(sub.id))
        self.assertEqual([e.id for e in self.events()], [manual.id])


class CourseTests(_Base):
    def test_events_match_courses_by_code_or_name(self):
        courses = self.app.config["AFTERWORD_COURSES_SERVICE"]
        algebra = courses.create(name="Algebra basics", code="math101")
        optics = courses.create(name="Optics")
        self.svc.add(URL)
        by_ref = {e.source_ref: e.course_id for e in self.events()}
        self.assertEqual(by_ref, {"a": algebra.id, "b": optics.id})

    def test_modules_without_a_course_can_be_created(self):
        sub, _ = self.svc.add(URL)
        missing = self.svc.missing_courses(sub.id)
        self.assertEqual(
            [(m["code"], m["name"]) for m in missing],
            [("MATH101", "Algebra"), ("PHY202", "Optics")],
        )
        self.assertEqual(
            self.svc.create_courses(sub.id, self.app.config["AFTERWORD_COURSES_SERVICE"]), 2
        )
        self.assertTrue(all(e.course_id for e in self.events()))
        self.assertEqual(self.svc.missing_courses(sub.id), [])

    def test_module_names(self):
        cases = {
            "SMM940 Financial Markets & Financial Intermediation/Lecture 01": (
                "SMM940",
                "Financial Markets & Financial Intermediation",
            ),
            "EPM965 Foundations of FinTech/Comp Lab/ Lecture 1/01": (
                "EPM965",
                "Foundations of FinTech",
            ),
            "_EPM963 Systems Risk Management Tutorial 2": ("EPM963", "Systems Risk Management"),
            "INM447 Big Data in Finance Lab 2": ("INM447", "Big Data in Finance"),
            "Dinner with Sam": ("", "Dinner with Sam"),
        }
        for title, want in cases.items():
            self.assertEqual(calendar_feeds.module_of(title), want, title)


class ImportTests(_Base):
    def test_importing_twice_updates_instead_of_duplicating(self):
        events = self.app.config["AFTERWORD_EVENTS_SERVICE"]
        self.assertEqual(events.import_ics(self.feed, "lecture"), 2)
        self.feed = calendar(
            vevent("a", "MATH101 Algebra/Lecture 01", 1, 12),
            vevent("b", "PHY202 Optics Lab 2", 2, 14),
        )
        self.assertEqual(events.import_ics(self.feed, "lecture"), 1)  # one moved
        self.assertEqual(len(self.events()), 2)

    def test_a_subscription_takes_over_events_imported_earlier(self):
        self.app.config["AFTERWORD_EVENTS_SERVICE"].import_ics(self.feed)
        self.svc.add(URL)
        self.assertEqual(len(self.events()), 2)
        self.assertEqual({e.source for e in self.events()}, {"subscription"})

    def test_import_endpoint_takes_a_kind_and_rejects_non_calendars(self):
        import io

        r = self.client.post(
            "/api/v1/events/import",
            data={"file": (io.BytesIO(self.feed), "t.ics"), "kind": "lecture"},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.get_json()["added"], 2)
        self.assertEqual({e.kind for e in self.events()}, {"lecture"})
        r = self.client.post(
            "/api/v1/events/import",
            data={"file": (io.BytesIO(b"hello"), "t.ics")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 400)


class ApiAndPageTests(_Base):
    def _post(self, url, body=None):
        return self.client.post(url, data=json.dumps(body or {}), content_type="application/json")

    def test_api_round_trip_hides_the_secret_link(self):
        r = self._post("/api/v1/subscriptions", {"url": URL, "kind": "meeting"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["result"]["added"], 2)
        self.assertNotIn("secret", json.dumps(body))
        sid = body["subscription"]["id"]
        listed = self.client.get("/api/v1/subscriptions").get_json()["subscriptions"]
        self.assertEqual(listed[0]["url_display"], "timetable.example.ac.uk/ical?…")
        self.assertEqual(
            self._post(f"/api/v1/subscriptions/{sid}/sync").get_json()["summary"], "No changes"
        )
        self.fetch.side_effect = calendar_feeds.FeedError("Couldn't reach the calendar (offline).")
        r = self._post(f"/api/v1/subscriptions/{sid}/sync")
        self.assertEqual(r.status_code, 502)
        self.assertIn("offline", r.get_json()["subscription"]["last_error"])
        r = self.client.patch(
            f"/api/v1/subscriptions/{sid}",
            data=json.dumps({"kind": "lecture"}),
            content_type="application/json",
        )
        self.assertEqual(r.get_json()["subscription"]["kind"], "lecture")
        self.assertEqual(
            self._post(f"/api/v1/subscriptions/{sid}/courses").get_json()["created"], 2
        )
        self.assertEqual(self.client.delete(f"/api/v1/subscriptions/{sid}").status_code, 204)
        self.assertEqual(self.events(), [])

    def test_bad_link_is_a_400_with_a_message(self):
        self.fetch.side_effect = lambda url: b"<html></html>"
        r = self._post("/api/v1/subscriptions", {"url": URL})
        self.assertEqual(r.status_code, 400)
        self.assertIn("didn't return a calendar", r.get_json()["detail"])

    def test_page_lists_subscriptions_and_modules(self):
        self.svc.add(URL)
        body = self.client.get("/calendar/subscriptions").get_data(as_text=True)
        self.assertIn("Uni timetable", body)
        self.assertIn("2 events", body)
        self.assertIn("2 modules without a course", body)
        self.assertNotIn("secret", body)
        self.assertIn(
            'href="/calendar/subscriptions"', self.client.get("/calendar").get_data(as_text=True)
        )

    def test_subscribed_events_keep_their_feed_fields(self):
        _sub, _ = self.svc.add(URL)
        ev = next(e for e in self.events() if e.source_ref == "a")
        page = self.client.get(f"/events/{ev.id}/edit").get_data(as_text=True)
        self.assertIn("From “Uni timetable”", page)
        self.client.post(
            f"/events/{ev.id}/edit",
            data={"title": "Hacked", "kind": "meeting", "starts_at": "", "course_id": ""},
        )
        after = self.store.get_event(ev.id)
        self.assertEqual((after.title, after.kind), (ev.title, "meeting"))


class ParseTests(unittest.TestCase):
    def test_city_style_description_stamp_is_dropped(self):
        feed = calendar(
            vevent(
                "x",
                "T",
                1,
                extra="DESCRIPTION:Type: Lecture\\n\\nLast synchronised on 2026-10-31 at 13:37 GMT\r\n",
            )
        )
        events = calendar_feeds.parse(feed, TODAY, TODAY + dt.timedelta(days=5)).events
        self.assertEqual(events[0].description, "Type: Lecture")

    def test_display_url_hides_the_query(self):
        self.assertEqual(calendar_feeds.display_url("https://a.b/ical?eu=x&h=y"), "a.b/ical?…")


if __name__ == "__main__":
    unittest.main()
