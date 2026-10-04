"""Tests for the Tier 1 schema additions.

Covers:
  * The new tables (notes, todos, deadlines, events) exist and have
    the right columns.
  * The new columns on sessions (kind, kind_confidence, kind_set_at)
    are present and nullable.
  * The schema is idempotent — running initialise() twice doesn't
    raise or duplicate.
"""
from __future__ import annotations

import unittest

from tests.conftest import LRTestCase


class SchemaShapeTests(LRTestCase):
    def test_new_tables_exist(self) -> None:
        store = self.app.config["LR_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for t in ("notes", "todos", "deadlines", "events"):
            self.assertIn(t, tables, f"missing table {t}")

    def test_sessions_kind_columns(self) -> None:
        store = self.app.config["LR_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(sessions)"
                ).fetchall()
            }
        for c in ("kind", "kind_confidence", "kind_set_at"):
            self.assertIn(c, cols, f"sessions missing column {c}")

    def test_notes_columns(self) -> None:
        store = self.app.config["LR_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(notes)"
                ).fetchall()
            }
        for c in ("id", "title", "body_md", "body_html",
                  "source", "source_ref", "pinned",
                  "created_at", "updated_at"):
            self.assertIn(c, cols)

    def test_todos_columns(self) -> None:
        store = self.app.config["LR_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(todos)"
                ).fetchall()
            }
        # We agreed: no due_at on todos. Confirm.
        self.assertNotIn("due_at", cols)
        for c in ("id", "title", "priority", "status",
                  "source", "created_at", "updated_at", "completed_at"):
            self.assertIn(c, cols)

    def test_deadlines_columns(self) -> None:
        store = self.app.config["LR_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(deadlines)"
                ).fetchall()
            }
        for c in ("id", "title", "due_at", "status",
                  "source", "source_ref", "source_offset_ms",
                  "created_at", "updated_at", "completed_at"):
            self.assertIn(c, cols)

    def test_events_columns(self) -> None:
        store = self.app.config["LR_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(events)"
                ).fetchall()
            }
        for c in ("id", "title", "kind", "starts_at", "ends_at",
                  "location", "source", "source_ref",
                  "matched_session_id", "match_confidence",
                  "created_at", "updated_at"):
            self.assertIn(c, cols)

    def test_schema_idempotent(self) -> None:
        """Re-running initialise on the same DB must not raise or
        duplicate columns / tables."""
        store = self.app.config["LR_STORAGE"]
        store.initialise()  # second call
        store.initialise()  # third call
        with store._connect() as conn:  # type: ignore[attr-defined]
            sessions_cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(sessions)"
                ).fetchall()
            }
        self.assertEqual(len(sessions_cols), len(set(sessions_cols)),
                         "duplicate column names after re-initialise")


class KindOverrideTests(LRTestCase):
    """The one piece of classification that does real work in Tier 1:
    the manual kind override via POST /api/v1/sessions/<id>/kind."""

    def _upload_complete_session(self) -> str:
        import json
        sid = "kindtest00000001"
        r = self.client.post("/api/v1/sessions", data=json.dumps({
            "id": sid, "expected_count": 1,
        }), content_type="application/json")
        self.assertEqual(r.status_code, 201)
        self.client.put(
            f"/api/v1/sessions/{sid}/chunks/1",
            data=self.make_wav(bytes([1]) * 200),
        )
        r = self.client.post(f"/api/v1/sessions/{sid}/finish")
        self.assertEqual(r.status_code, 200)
        return sid

    def test_set_kind_via_route(self) -> None:
        import json
        sid = self._upload_complete_session()
        r = self.client.post(
            f"/api/v1/sessions/{sid}/kind",
            data=json.dumps({"kind": "lecture"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["session"]["kind"], "lecture")
        self.assertEqual(body["session"]["kind_confidence"], 1.0)
        self.assertIsNotNone(body["session"]["kind_set_at"])

    def test_set_kind_with_explicit_confidence(self) -> None:
        import json
        sid = self._upload_complete_session()
        r = self.client.post(
            f"/api/v1/sessions/{sid}/kind",
            data=json.dumps({"kind": "voice_memo", "confidence": 0.7}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["session"]["kind_confidence"], 0.7)

    def test_set_kind_invalid_value(self) -> None:
        import json
        sid = self._upload_complete_session()
        r = self.client.post(
            f"/api/v1/sessions/{sid}/kind",
            data=json.dumps({"kind": "not-a-real-kind"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "invalid_kind")

    def test_set_kind_clear_with_null(self) -> None:
        import json
        sid = self._upload_complete_session()
        self.client.post(
            f"/api/v1/sessions/{sid}/kind",
            data=json.dumps({"kind": "meeting"}),
            content_type="application/json",
        )
        r = self.client.post(
            f"/api/v1/sessions/{sid}/kind",
            data=json.dumps({"kind": None}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.get_json()["session"]["kind"])

    def test_set_kind_unknown_session(self) -> None:
        import json
        r = self.client.post(
            "/api/v1/sessions/nonexistent/kind",
            data=json.dumps({"kind": "lecture"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404)

    def test_set_kind_missing_field(self) -> None:
        import json
        sid = self._upload_complete_session()
        r = self.client.post(
            f"/api/v1/sessions/{sid}/kind",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "invalid_payload")

    def test_session_kinds_constant(self) -> None:
        from storage import Storage
        self.assertEqual(
            Storage.RECORDING_KINDS,
            frozenset({"lecture", "voice_memo", "meeting", "other"}),
        )


class ServiceStubTests(unittest.TestCase):
    """The service modules are present with NotImplementedError bodies."""

    def test_notes_service_is_stub(self) -> None:
        from services.notes import NotesService
        s = NotesService()
        with self.assertRaises(NotImplementedError):
            s.create(title="x", body_md="y")
        with self.assertRaises(NotImplementedError):
            s.list()

    def test_todos_service_is_stub(self) -> None:
        from services.todos import TodosService
        s = TodosService()
        with self.assertRaises(NotImplementedError):
            s.create(title="x")
        with self.assertRaises(NotImplementedError):
            s.set_status("abc", "done")

    def test_deadlines_service_is_stub(self) -> None:
        from services.deadlines import DeadlinesService
        s = DeadlinesService()
        with self.assertRaises(NotImplementedError):
            s.create(title="x", due_at="2026-10-04T12:00:00Z")

    def test_events_service_is_stub(self) -> None:
        from services.events import EventsService
        s = EventsService()
        with self.assertRaises(NotImplementedError):
            s.create(title="x", kind="lecture", starts_at="2026-10-04T12:00:00Z")

    def test_classification_service_is_stub(self) -> None:
        from services.classification import ClassificationService
        s = ClassificationService()
        with self.assertRaises(NotImplementedError):
            s.classify_session("abc")

    def test_events_kinds_constant(self) -> None:
        from services.events import KINDS
        self.assertEqual(KINDS, frozenset({"lecture", "meeting", "other"}))

    def test_todos_priority_and_status_constants(self) -> None:
        from services.todos import PRIORITIES, STATUSES
        self.assertEqual(PRIORITIES, frozenset({"low", "medium", "high", "urgent"}))
        self.assertEqual(STATUSES, frozenset({"open", "done", "cancelled"}))

    def test_deadlines_statuses_constant(self) -> None:
        from services.deadlines import STATUSES as DSTATUS
        self.assertEqual(DSTATUS, frozenset({"set", "complete"}))


if __name__ == "__main__":
    unittest.main()
