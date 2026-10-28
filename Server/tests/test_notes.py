"""Tests for the Notes service and API. Covers service-level validation/rendering/CRUD, API-level HTTP surface, and frontend HTML rendering. All tests use the temp-dir storage helper from conftest.LRTestCase."""
from __future__ import annotations

import json
import unittest
from typing import Any

from tests.conftest import LRTestCase



# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class NotesServiceTests(LRTestCase):
    """The service does the validation and rendering; storage just
    persists. These tests go through the service."""

    def _service(self):
        return self.app.config["LR_NOTES_SERVICE"]

    def test_create_persists_and_renders(self):
        svc = self._service()
        note = svc.create(
            title="First note",
            body_md="# Heading\n\nA paragraph with **bold**.",
        )
        self.assertEqual(note.title, "First note")
        self.assertEqual(note.source, "manual")
        self.assertFalse(note.pinned)
        self.assertIsNotNone(note.body_html)
        self.assertIn("<h1>", note.body_html)
        self.assertIn("<strong>bold</strong>", note.body_html)
        again = svc.get(note.id)
        self.assertIsNotNone(again)
        self.assertEqual(again.title, "First note")

    def test_get_returns_none_for_missing(self):
        svc = self._service()
        self.assertIsNone(svc.get("nope"))

    def test_create_rejects_blank_title(self):
        svc = self._service()
        with self.assertRaises(ValueError):
            svc.create(title="   ")
        with self.assertRaises(ValueError):
            svc.create(title="")

    def test_create_rejects_too_long_title(self):
        svc = self._service()
        with self.assertRaises(ValueError):
            svc.create(title="x" * 201)

    def test_create_rejects_control_characters(self):
        svc = self._service()
        with self.assertRaises(ValueError):
            svc.create(title="hello\x00world")
        with self.assertRaises(ValueError):
            svc.create(title="ok", body_md="bad\x00body")

    def test_create_rejects_too_long_body(self):
        svc = self._service()
        with self.assertRaises(ValueError):
            svc.create(title="ok", body_md="x" * 200_001)

    def test_update_partial_fields(self):
        svc = self._service()
        note = svc.create(title="A", body_md="A body")
        updated = svc.update(note.id, title="B")
        self.assertEqual(updated.title, "B")
        self.assertEqual(updated.body_md, "A body")
        self.assertEqual(updated.body_html, note.body_html)

    def test_update_body_rerenders_html(self):
        svc = self._service()
        note = svc.create(title="A", body_md="# old")
        old_html = note.body_html
        updated = svc.update(note.id, body_md="# new")
        self.assertEqual(updated.body_md, "# new")
        self.assertNotEqual(updated.body_html, old_html)
        self.assertIn(">new</h1>", updated.body_html or "")

    def test_update_pinned_flag(self):
        svc = self._service()
        note = svc.create(title="A", body_md="x")
        self.assertFalse(note.pinned)
        pinned = svc.update(note.id, pinned=True)
        self.assertTrue(pinned.pinned)
        unpinned = svc.update(note.id, pinned=False)
        self.assertFalse(unpinned.pinned)

    def test_update_missing_raises_keyerror(self):
        svc = self._service()
        with self.assertRaises(KeyError):
            svc.update("nope", title="x")

    def test_update_validation_runs(self):
        svc = self._service()
        note = svc.create(title="A", body_md="x")
        with self.assertRaises(ValueError):
            svc.update(note.id, title="")
        with self.assertRaises(ValueError):
            svc.update(note.id, body_md="x" * 200_001)

    def test_delete_removes_row(self):
        svc = self._service()
        note = svc.create(title="A", body_md="x")
        self.assertTrue(svc.delete(note.id))
        self.assertIsNone(svc.get(note.id))
        self.assertFalse(svc.delete(note.id))

    def test_list_orders_by_updated_at_desc(self):
        svc = self._service()
        n1 = svc.create(title="First", body_md="1")
        n2 = svc.create(title="Second", body_md="2")
        n3 = svc.create(title="Third", body_md="3")
        # Force distinct updated_at values so order is deterministic.
        store = self.app.config["LR_STORAGE"]
        for n, ts in [(n3, '2026-10-06T10:00:02Z'), (n2, '2026-10-06T10:00:01Z'), (n1, '2026-10-06T10:00:00Z')]:
            with store._tx() as conn:
                conn.execute('UPDATE notes SET updated_at = ? WHERE id = ?', (ts, n.id))
        notes, total = svc.list()
        self.assertEqual(total, 3)
        self.assertEqual([n.id for n in notes], [n3.id, n2.id, n1.id])

    def test_list_pagination(self):
        svc = self._service()
        for i in range(5):
            svc.create(title=f"Note {i}", body_md=str(i))
        page1, total = svc.list(limit=2, offset=0)
        page2, _ = svc.list(limit=2, offset=2)
        page3, _ = svc.list(limit=2, offset=4)
        self.assertEqual(total, 5)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        self.assertEqual(len(page3), 1)
        ids = {n.id for n in page1 + page2 + page3}
        self.assertEqual(len(ids), 5)

    def test_list_search_matches_title_and_body(self):
        svc = self._service()
        svc.create(title="Cooking tips", body_md="salt, pepper")
        svc.create(title="Gardening", body_md="water the plants")
        svc.create(title="Random", body_md="totally unrelated content")
        by_title, t1 = svc.list(search="cooking")
        self.assertEqual(t1, 1)
        self.assertEqual(by_title[0].title, "Cooking tips")
        by_body, t2 = svc.list(search="water")
        self.assertEqual(t2, 1)
        self.assertEqual(by_body[0].title, "Gardening")
        none, t4 = svc.list(search="xyzzy")
        self.assertEqual(t4, 0)
        self.assertEqual(none, [])

    def test_list_empty_search_is_ignored(self):
        svc = self._service()
        svc.create(title="A", body_md="x")
        notes, total = svc.list(search="   ")
        self.assertEqual(total, 1)

    def test_render_html_escapes_script_tags(self):
        from services.notes import render_html
        out = render_html("Hello <script>alert(1)</script> world")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_render_html_supports_standard_markdown(self):
        from services.notes import render_html
        cases = [
            ("# Title", "<h1>"),
            ("## Sub", "<h2>"),
            ("**bold**", "<strong>"),
            ("*italic*", "<em>"),
            ("[link](http://x)", "<a href="),
            ("- a\n- b", "<ul>"),
            ("1. a\n2. b", "<ol>"),
            ("`code`", "<code>"),
            ("> quote", "<blockquote>"),
        ]
        for src, expected in cases:
            out = render_html(src)
            self.assertIn(expected, out, f"failed for {src!r}: {out!r}")


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------


class NotesAPITests(LRTestCase):
    """The HTTP surface: status codes, JSON shape, error responses."""

    def _create(self, **payload):
        return self.client.post(
            "/api/v1/notes",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_create_returns_201_with_note(self):
        resp = self._create(title="Hello", body_md="**hi**")
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertIn("note", body)
        self.assertEqual(body["note"]["title"], "Hello")
        self.assertIn("<strong>hi</strong>", body["note"]["body_html"])

    def test_create_validates_title(self):
        resp = self._create(title="", body_md="x")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "invalid_payload")

    def test_create_validates_body_size(self):
        resp = self._create(title="ok", body_md="x" * 200_001)
        self.assertEqual(resp.status_code, 400)

    def test_list_default(self):
        self._create(title="A", body_md="1")
        self._create(title="B", body_md="2")
        resp = self.client.get("/api/v1/notes")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["notes"]), 2)
        self.assertEqual(body["limit"], 50)
        self.assertEqual(body["offset"], 0)
        self.assertIsNone(body["q"])

    def test_list_with_search(self):
        self._create(title="Apples", body_md="fruit")
        self._create(title="Cars", body_md="vehicle")
        resp = self.client.get("/api/v1/notes?q=fruit")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["notes"][0]["title"], "Apples")

    def test_list_validates_limit(self):
        self.assertEqual(self.client.get("/api/v1/notes?limit=0").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/notes?limit=999").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/notes?limit=abc").status_code, 400)

    def test_get_returns_404_for_missing(self):
        self.assertEqual(self.client.get("/api/v1/notes/zzz").status_code, 404)

    def test_get_returns_note(self):
        create = self._create(title="A", body_md="x").get_json()
        note_id = create["note"]["id"]
        resp = self.client.get(f"/api/v1/notes/{note_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["note"]["title"], "A")

    def test_update_partial(self):
        create = self._create(title="A", body_md="x").get_json()
        note_id = create["note"]["id"]
        resp = self.client.patch(
            f"/api/v1/notes/{note_id}",
            data=json.dumps({"title": "B"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["note"]["title"], "B")
        self.assertEqual(resp.get_json()["note"]["body_md"], "x")

    def test_update_rerenders_html_on_body_change(self):
        create = self._create(title="A", body_md="# old").get_json()
        old_html = create["note"]["body_html"]
        resp = self.client.patch(
            f"/api/v1/notes/{create['note']['id']}",
            data=json.dumps({"body_md": "# new"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        new_html = resp.get_json()["note"]["body_html"]
        self.assertNotEqual(old_html, new_html)
        self.assertIn(">new</h1>", new_html)

    def test_update_missing_returns_404(self):
        resp = self.client.patch(
            "/api/v1/notes/zzz",
            data=json.dumps({"title": "B"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_update_validates(self):
        create = self._create(title="A", body_md="x").get_json()
        resp = self.client.patch(
            f"/api/v1/notes/{create['note']['id']}",
            data=json.dumps({"title": ""}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_returns_204(self):
        create = self._create(title="A", body_md="x").get_json()
        note_id = create["note"]["id"]
        resp = self.client.delete(f"/api/v1/notes/{note_id}")
        self.assertEqual(resp.status_code, 204)
        resp = self.client.delete(f"/api/v1/notes/{note_id}")
        self.assertEqual(resp.status_code, 404)

    def test_render_endpoint(self):
        resp = self.client.post(
            "/api/v1/notes/render",
            data=json.dumps({"body_md": "# hi"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<h1>", resp.get_json()["html"])

    def test_render_endpoint_escapes(self):
        resp = self.client.post(
            "/api/v1/notes/render",
            data=json.dumps({"body_md": "<script>x</script>"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("<script>", resp.get_json()["html"])

    def test_render_endpoint_validates_body(self):
        resp = self.client.post(
            "/api/v1/notes/render",
            data=json.dumps({"body_md": 123}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# Web UI tests
# ---------------------------------------------------------------------------


class NotesWebUITests(LRTestCase):
    """The HTML pages render correctly with real notes in storage."""

    def _create(self, **payload):
        return self.client.post(
            "/api/v1/notes",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_notes_list_renders_empty(self):
        resp = self.client.get("/notes")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Notes", body)

    def test_notes_list_shows_real_note(self):
        self._create(title="My first note", body_md="hello")
        resp = self.client.get("/notes")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("My first note", body)

    def test_notes_list_search_filters(self):
        self._create(title="Apples", body_md="fruit")
        self._create(title="Cars", body_md="vehicle")
        resp = self.client.get("/notes?q=vehicle")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Cars", body)
        self.assertNotIn("Apples", body)

    def test_notes_new_renders_editor(self):
        resp = self.client.get("/notes/new", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # The embedded editor renders in create mode.
        self.assertIn("lr-note-editor", body)
        self.assertIn("lr-note-title", body)
        self.assertIn("lr-note-body", body)
    def test_note_detail_renders_editor_prefilled(self):
        create = self._create(title="Hello", body_md="**world**").get_json()
        note_id = create["note"]["id"]
        # The legacy deep-link redirects to the notes page with the
        # editor prefilled.
        resp = self.client.get(f"/notes/{note_id}", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Hello", body)
        self.assertIn("**world**", body)
    def test_note_detail_404(self):
        self.assertEqual(self.client.get("/notes/zzz").status_code, 404)

    def test_note_edit_renders_editor_prefilled(self):
        create = self._create(title="Hello", body_md="**world**").get_json()
        note_id = create["note"]["id"]
        resp = self.client.get(
            f"/notes/{note_id}/edit", follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("**world**", body)

    def test_notes_page_renders_selected_note_editor(self):
        create = self._create(title="Hello", body_md="body text").get_json()
        note_id = create["note"]["id"]
        resp = self.client.get(f"/notes?note_id={note_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("lr-note-editor", body)
        self.assertIn("Hello", body)
        self.assertIn("body text", body)
    def test_note_edit_404(self):
        self.assertEqual(self.client.get("/notes/zzz/edit").status_code, 404)


if __name__ == "__main__":
    unittest.main()
