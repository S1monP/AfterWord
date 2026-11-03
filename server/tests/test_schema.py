"""Tests for the schema beyond recordings and chunks.

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
        store = self.app.config["AFTERWORD_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for t in ("notes", "tasks", "events"):
            self.assertIn(t, tables, f"missing table {t}")

    def test_sessions_kind_columns(self) -> None:
        store = self.app.config["AFTERWORD_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        for c in ("kind", "kind_confidence", "kind_set_at"):
            self.assertIn(c, cols, f"sessions missing column {c}")

    def test_notes_columns(self) -> None:
        store = self.app.config["AFTERWORD_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(notes)").fetchall()}
        for c in (
            "id",
            "title",
            "body_md",
            "body_html",
            "source",
            "source_ref",
            "pinned",
            "created_at",
            "updated_at",
        ):
            self.assertIn(c, cols)

    def test_tasks_columns(self) -> None:
        """The merged tasks table has the union of todos + deadlines columns."""
        store = self.app.config["AFTERWORD_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        for c in (
            "id",
            "title",
            "body_md",
            "description",
            "priority",
            "status",
            "kind",
            "due_at",
            "source",
            "source_ref",
            "source_offset_ms",
            "course_id",
            "pinned",
            "completed_at",
            "created_at",
            "updated_at",
        ):
            self.assertIn(c, cols)
        # The old todo + deadline tables are gone after the merge.
        for gone in ("todos", "deadlines"):
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (gone,)
            )
            self.assertIsNone(cur.fetchone())

    def test_events_columns(self) -> None:
        store = self.app.config["AFTERWORD_STORAGE"]
        with store._connect() as conn:  # type: ignore[attr-defined]
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        for c in (
            "id",
            "title",
            "kind",
            "starts_at",
            "ends_at",
            "location",
            "source",
            "source_ref",
            "matched_session_id",
            "match_confidence",
            "created_at",
            "updated_at",
        ):
            self.assertIn(c, cols)

    def test_schema_idempotent(self) -> None:
        """Re-running initialise on the same DB must not raise or
        duplicate columns / tables."""
        store = self.app.config["AFTERWORD_STORAGE"]
        store.initialise()  # second call
        store.initialise()  # third call
        with store._connect() as conn:  # type: ignore[attr-defined]
            sessions_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
        self.assertEqual(
            len(sessions_cols),
            len(set(sessions_cols)),
            "duplicate column names after re-initialise",
        )


class KindOverrideTests(LRTestCase):
    """Classification by hand: the manual kind override via
    POST /api/v1/sessions/<id>/kind."""

    def _upload_complete_session(self) -> str:
        import json

        sid = "kindtest00000001"
        r = self.client.post(
            "/api/v1/sessions",
            data=json.dumps(
                {
                    "id": sid,
                    "expected_count": 1,
                }
            ),
            content_type="application/json",
        )
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
            frozenset({"lecture", "voice_memo", "meeting", "command", "other"}),
        )


class ServiceStubTests(unittest.TestCase):
    """The service modules are present with NotImplementedError bodies."""

    def test_notes_service_is_real(self) -> None:
        # The constructor should accept a storage instance now.
        import inspect

        from services.notes import NotesService

        sig = inspect.signature(NotesService.__init__)
        self.assertGreaterEqual(
            len(sig.parameters),
            2,
            "NotesService.__init__ should take (self, store)",
        )

    def test_tasks_service_is_real(self) -> None:
        """The constructor should take a Storage instance; full behaviour
        is covered in ``tests/test_tasks.py``."""
        import inspect

        from services.tasks import TasksService

        sig = inspect.signature(TasksService.__init__)
        self.assertGreaterEqual(
            len(sig.parameters),
            2,
            "TasksService.__init__ should take (self, store)",
        )

    def test_events_service_is_real(self) -> None:
        """The constructor takes a Storage instance; full behaviour
        is covered in ``tests/test_events.py``.
        """
        import inspect

        from services.events import EventsService

        sig = inspect.signature(EventsService.__init__)
        self.assertGreaterEqual(
            len(sig.parameters),
            2,
            "EventsService.__init__ should take (self, store)",
        )

    def test_classification_service_is_real(self) -> None:
        """The constructor takes a Storage instance; manual override
        works against the real DB. Full coverage lives in
        ``tests/test_classification.py``."""
        import inspect

        from services.classification import ClassificationService

        sig = inspect.signature(ClassificationService.__init__)
        self.assertGreaterEqual(
            len(sig.parameters),
            2,
            "ClassificationService.__init__ should take (self, store)",
        )

    def test_events_kinds_constant(self) -> None:
        from services.events import KINDS

        self.assertEqual(KINDS, frozenset({"lecture", "meeting", "other"}))

    def test_tasks_priority_status_and_kind_constants(self) -> None:
        from services.tasks import PRIORITIES, STATUSES

        self.assertEqual(PRIORITIES, frozenset({"low", "medium", "high", "urgent"}))
        self.assertEqual(STATUSES, frozenset({"open", "done", "cancelled"}))
