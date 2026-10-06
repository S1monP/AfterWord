"""Tests for the Todos service and API. Covers service-level validation, status transitions, CRUD, age buckets, and the HTTP surface. All tests use the temp-dir storage helper from tests.conftest.LRTestCase."""

from __future__ import annotations

import json
import unittest
from typing import Any

from tests.conftest import LRTestCase

# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TodosServiceTests(LRTestCase):
    def _service(self):
        return self.app.config["LR_TODOS_SERVICE"]

    def test_create_persists_defaults(self):
        svc = self._service()
        t = svc.create(title="Buy milk")
        self.assertEqual(t.title, "Buy milk")
        self.assertEqual(t.priority, "medium")
        self.assertEqual(t.status, "open")
        self.assertEqual(t.source, "manual")
        self.assertIsNone(t.body_md)
        self.assertIsNone(t.completed_at)
        # Persisted.
        again = svc.get(t.id)
        self.assertIsNotNone(again)
        self.assertEqual(again.title, "Buy milk")

    def test_create_rejects_blank_title(self):
        svc = self._service()
        with self.assertRaises(ValueError):
            svc.create(title="   ")
        with self.assertRaises(ValueError):
            svc.create(title="")
        with self.assertRaises(ValueError):
            svc.create(title=123)  # type: ignore[arg-type]

    def test_create_rejects_too_long_title(self):
        svc = self._service()
        with self.assertRaises(ValueError):
            svc.create(title="x" * 201)

    def test_create_rejects_control_characters(self):
        svc = self._service()
        with self.assertRaises(ValueError):
            svc.create(title="hello\x00world")

    def test_create_rejects_bad_priority(self):
        svc = self._service()
        with self.assertRaises(ValueError):
            svc.create(title="ok", priority="highest")

    def test_update_partial(self):
        svc = self._service()
        t = svc.create(title="A", body_md="body", priority="low")
        updated = svc.update(t.id, title="B", priority="high")
        self.assertEqual(updated.title, "B")
        self.assertEqual(updated.priority, "high")
        self.assertEqual(updated.body_md, "body")
        # Status is unchanged.
        self.assertEqual(updated.status, "open")

    def test_update_missing_raises_keyerror(self):
        svc = self._service()
        with self.assertRaises(KeyError):
            svc.update("nope", title="x")

    def test_update_validation_runs(self):
        svc = self._service()
        t = svc.create(title="A")
        with self.assertRaises(ValueError):
            svc.update(t.id, title="")
        with self.assertRaises(ValueError):
            svc.update(t.id, priority="highest")

    def test_set_status_open_to_done(self):
        svc = self._service()
        t = svc.create(title="A")
        self.assertIsNone(t.completed_at)
        done = svc.set_status(t.id, "done")
        self.assertEqual(done.status, "done")
        self.assertIsNotNone(done.completed_at)

    def test_set_status_done_to_open_clears_completed_at(self):
        svc = self._service()
        t = svc.create(title="A")
        done = svc.set_status(t.id, "done")
        self.assertIsNotNone(done.completed_at)
        opened = svc.set_status(t.id, "open")
        self.assertEqual(opened.status, "open")
        self.assertIsNone(opened.completed_at)

    def test_set_status_open_to_cancelled(self):
        svc = self._service()
        t = svc.create(title="A")
        cancelled = svc.set_status(t.id, "cancelled")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(cancelled.completed_at)

    def test_set_status_same_value_is_noop(self):
        svc = self._service()
        t = svc.create(title="A")
        again = svc.set_status(t.id, "open")
        self.assertEqual(again.status, "open")
        self.assertEqual(again.updated_at, t.updated_at)

    def test_set_status_bad_status(self):
        svc = self._service()
        t = svc.create(title="A")
        with self.assertRaises(ValueError):
            svc.set_status(t.id, "weird")

    def test_set_status_missing(self):
        svc = self._service()
        with self.assertRaises(KeyError):
            svc.set_status("nope", "done")

    def test_delete_removes_row(self):
        svc = self._service()
        t = svc.create(title="A")
        self.assertTrue(svc.delete(t.id))
        self.assertIsNone(svc.get(t.id))
        self.assertFalse(svc.delete(t.id))

    def test_list_open_first_then_by_priority(self):
        svc = self._service()
        t1 = svc.create(title="low task", priority="low")
        t2 = svc.create(title="urgent task", priority="urgent")
        t3 = svc.create(title="high task", priority="high")
        # Mark one as done.
        svc.set_status(t3.id, "done")
        # Default list shows open first.
        listed, total = svc.list()
        self.assertEqual(total, 3)
        # Order: open tasks by priority (urgent, low), then done.
        ids = [t.id for t in listed]
        self.assertEqual(ids[:2], [t2.id, t1.id])  # urgent then low
        self.assertEqual(ids[2], t3.id)            # done

    def test_list_filter_by_status(self):
        svc = self._service()
        a = svc.create(title="A")
        b = svc.create(title="B")
        svc.set_status(b.id, "done")
        open_list, _ = svc.list(status="open")
        self.assertEqual([t.id for t in open_list], [a.id])
        done_list, _ = svc.list(status="done")
        self.assertEqual([t.id for t in done_list], [b.id])

    def test_list_filter_by_priority(self):
        svc = self._service()
        a = svc.create(title="A", priority="low")
        b = svc.create(title="B", priority="urgent")
        urgent, _ = svc.list(priority="urgent")
        self.assertEqual([t.id for t in urgent], [b.id])
        low, _ = svc.list(priority="low")
        self.assertEqual([t.id for t in low], [a.id])

    def test_age_bucket(self):
        from services.todos import age_bucket
        # Now vs one second ago: new.
        self.assertEqual(age_bucket("2026-10-06T10:00:00Z", "2026-10-06T10:00:01Z"), "new")
        # Three days later: aging.
        self.assertEqual(age_bucket("2026-10-01T10:00:00Z", "2026-10-04T10:00:00Z"), "aging")
        # Ten days later: stale.
        self.assertEqual(age_bucket("2026-09-20T10:00:00Z", "2026-10-01T10:00:00Z"), "stale")
        # Bad input: unknown.
        self.assertEqual(age_bucket("not a date", "2026-10-06T10:00:00Z"), "unknown")
        # Future-dated: clamped to 'new'.
        self.assertEqual(age_bucket("2026-10-07T10:00:00Z", "2026-10-06T10:00:00Z"), "new")


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------


class TodosAPITests(LRTestCase):
    def _create(self, **payload):
        return self.client.post(
            "/api/v1/todos",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_create_returns_201(self):
        resp = self._create(title="A")
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertEqual(body["todo"]["title"], "A")
        self.assertEqual(body["todo"]["status"], "open")
        self.assertEqual(body["todo"]["priority"], "medium")

    def test_create_validates_title(self):
        self.assertEqual(self._create(title="").status_code, 400)
        self.assertEqual(self._create(title="   ").status_code, 400)

    def test_create_with_priority(self):
        resp = self._create(title="A", priority="high")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["todo"]["priority"], "high")

    def test_create_with_bad_priority(self):
        self.assertEqual(self._create(title="A", priority="highest").status_code, 400)

    def test_list_default(self):
        self._create(title="A")
        self._create(title="B", priority="urgent")
        resp = self.client.get("/api/v1/todos")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["todos"]), 2)
        # Order: urgent first.
        self.assertEqual(body["todos"][0]["priority"], "urgent")
        self.assertEqual(body["todos"][1]["priority"], "medium")

    def test_list_with_status_filter(self):
        a = self._create(title="A").get_json()["todo"]
        self._create(title="B")
        # Mark A as done.
        self.client.post(
            f"/api/v1/todos/{a['id']}/status",
            data=json.dumps({"status": "done"}),
            content_type="application/json",
        )
        resp = self.client.get("/api/v1/todos?status=open")
        body = resp.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["todos"][0]["title"], "B")

    def test_get_returns_404_for_missing(self):
        self.assertEqual(self.client.get("/api/v1/todos/zzz").status_code, 404)

    def test_get_returns_todo(self):
        create = self._create(title="A").get_json()
        resp = self.client.get(f"/api/v1/todos/{create['todo']['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["todo"]["title"], "A")

    def test_update_partial(self):
        create = self._create(title="A", priority="low").get_json()
        resp = self.client.patch(
            f"/api/v1/todos/{create['todo']['id']}",
            data=json.dumps({"title": "B", "priority": "high"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()["todo"]
        self.assertEqual(body["title"], "B")
        self.assertEqual(body["priority"], "high")
        self.assertEqual(body["status"], "open")

    def test_update_missing_returns_404(self):
        resp = self.client.patch(
            "/api/v1/todos/zzz",
            data=json.dumps({"title": "B"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_update_validates(self):
        create = self._create(title="A").get_json()
        resp = self.client.patch(
            f"/api/v1/todos/{create['todo']['id']}",
            data=json.dumps({"title": ""}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_status_transition(self):
        create = self._create(title="A").get_json()
        tid = create["todo"]["id"]
        # open -> done
        resp = self.client.post(
            f"/api/v1/todos/{tid}/status",
            data=json.dumps({"status": "done"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["todo"]["status"], "done")
        self.assertIsNotNone(resp.get_json()["todo"]["completed_at"])
        # done -> open
        resp = self.client.post(
            f"/api/v1/todos/{tid}/status",
            data=json.dumps({"status": "open"}),
            content_type="application/json",
        )
        self.assertEqual(resp.get_json()["todo"]["status"], "open")
        self.assertIsNone(resp.get_json()["todo"]["completed_at"])

    def test_status_missing_field(self):
        create = self._create(title="A").get_json()
        resp = self.client.post(
            f"/api/v1/todos/{create['todo']['id']}/status",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_status_missing_todo_returns_404(self):
        resp = self.client.post(
            "/api/v1/todos/zzz/status",
            data=json.dumps({"status": "done"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_status_bad_value(self):
        create = self._create(title="A").get_json()
        resp = self.client.post(
            f"/api/v1/todos/{create['todo']['id']}/status",
            data=json.dumps({"status": "weird"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_returns_204(self):
        create = self._create(title="A").get_json()
        tid = create["todo"]["id"]
        resp = self.client.delete(f"/api/v1/todos/{tid}")
        self.assertEqual(resp.status_code, 204)
        resp = self.client.delete(f"/api/v1/todos/{tid}")
        self.assertEqual(resp.status_code, 404)

    def test_list_validates_limit(self):
        self.assertEqual(self.client.get("/api/v1/todos?limit=0").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/todos?limit=999").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/todos?limit=abc").status_code, 400)


# ---------------------------------------------------------------------------
# Web UI tests
# ---------------------------------------------------------------------------


class TodosWebUITests(LRTestCase):
    def _create(self, **payload):
        return self.client.post(
            "/api/v1/todos",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_todos_list_renders_empty(self):
        resp = self.client.get("/todos")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Tasks", body)
        self.assertIn("0 open", body)

    def test_todos_list_shows_real_todo(self):
        self._create(title="Buy milk")
        resp = self.client.get("/todos")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Buy milk", body)
        self.assertIn("1 open", body)

    def test_todos_list_status_filter(self):
        a = self._create(title="first todo").get_json()["todo"]
        self._create(title="second todo")
        # Mark first as done.
        self.client.post(
            f"/api/v1/todos/{a['id']}/status",
            data=json.dumps({"status": "done"}),
            content_type="application/json",
        )
        resp = self.client.get("/todos?status=done")
        body = resp.get_data(as_text=True)
        self.assertIn("first todo", body)
        self.assertNotIn("second todo", body)

    def test_todos_quick_add(self):
        resp = self.client.post(
            "/todos/quick",
            data={"title": "From form"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("From form", body)

    def test_todos_quick_add_blank(self):
        resp = self.client.post(
            "/todos/quick",
            data={"title": ""},
            follow_redirects=True,
        )
        # Blank titles are silently ignored (redirect to /todos).
        self.assertEqual(resp.status_code, 200)
        # The list is still empty.
        body = resp.get_data(as_text=True)
        self.assertIn("0 open", body)


if __name__ == "__main__":
    unittest.main()
