"""Search: word forms, what is indexed, keeping the index current, and
the dashboard's search API."""

from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs, urlsplit

import search
from services.events import EventsService
from services.notes import NotesService
from services.tasks import TasksService
from tests.conftest import LRTestCase


class QueryTests(LRTestCase):
    def test_terms_are_stemmed_and_the_last_is_a_prefix(self):
        q = search.parse_query("Лекция по алгебре")
        self.assertEqual(
            [(t.word, t.stem, t.prefix) for t in q.terms],
            [
                ("лекция", "лекц", False),  # "по" is a stop word
                ("алгебре", "алгебр", True),
            ],
        )

    def test_only_stop_words_are_kept(self):
        self.assertEqual([t.word for t in search.parse_query("the").terms], ["the"])

    def test_nothing_to_search(self):
        for text in ("", "  ", "?!", "a"):
            self.assertFalse(search.parse_query(text))

    def test_highlight_marks_every_word_form(self):
        segments = search.highlight("Лекции про лекцию, и Ёлка", "лекция ёлка")
        self.assertEqual([s for s, marked in segments if marked], ["Лекции", "лекцию", "Ёлка"])
        self.assertEqual("".join(s for s, _ in segments), "Лекции про лекцию, и Ёлка")

    def test_snippet_cuts_around_the_match(self):
        text = "filler " * 50 + "the midterm is in the main hall " + "more " * 50
        segments = search.snippet(text, "midterm", width=80)
        joined = "".join(s for s, _ in segments)
        self.assertLessEqual(len(joined), 84)
        self.assertTrue(joined.startswith("…") and joined.endswith("…"))
        self.assertIn(("midterm", True), segments)


class _Base(LRTestCase):
    def setUp(self):
        super().setUp()
        self.store = self.app.config["AFTERWORD_STORAGE"]

    def _recording(self, *, title=None, kind="lecture", transcript="", summary=None):
        sid = uuid.uuid4().hex
        self.store.upsert_session(
            sid,
            title="rec_2026-01-15T1000",
            expected_count=1,
            sample_rate=22050,
            channels=1,
            bits_per_sample=16,
            started_at="2026-01-15T10:00:00Z",
        )
        with self.store._tx() as conn:
            conn.execute("UPDATE sessions SET status = 'done', kind = ? WHERE id = ?", (kind, sid))
        if title:
            self.store.set_session_title(sid, title, source="ai")
        if summary is not None:
            self.store.add_artifact(sid, "summary", json.dumps(summary))
        if transcript:
            self.store.add_artifact(sid, "transcript", transcript)
        return sid

    def _ids(self, query, item_type="recording"):
        return [h.item_id for h in search.search(self.store, query)[item_type]]


class SearchTests(_Base):
    def test_russian_and_english_word_forms(self):
        ru = self._recording(transcript="[00:05] Speaker 1: Следующие лекции отменены.")
        en = self._recording(transcript="[00:05] Speaker 1: Two meetings next week.")
        self.assertEqual(self._ids("лекция"), [ru])
        self.assertEqual(self._ids("meeting"), [en])
        self.assertEqual(self._ids("meetin"), [en])  # still typing
        self.assertEqual(self._ids("лекций отменены"), [ru])
        exam = self._recording(transcript="[00:05] Speaker 1: Сегодня экзаменов нет.")
        self.assertEqual(self._ids("экзамены сегодня"), [exam])
        self.assertEqual(self._ids("лекция meeting"), [])  # one line has both

    def test_moments_and_best_piece(self):
        sid = self._recording(
            transcript=(
                "[00:05] Speaker 1: Hello.\n"
                "[01:10] Speaker 1: The entropy of the system grows.\n"
                "[1:02:03] Speaker 2: Entropy again."
            )
        )
        (hit,) = search.search(self.store, "entropy")["recording"]
        self.assertEqual(hit.item_id, sid)
        self.assertEqual(hit.moments, [70_000, 3_723_000])
        self.assertEqual(hit.count, 2)
        self.assertEqual(hit.best["field"], "transcript")

    def test_name_and_summary_are_searched_and_the_name_ranks_first(self):
        named = self._recording(title="Thermodynamics", transcript="[00:01] Speaker 1: hi")
        mentioned = self._recording(
            summary={
                "title": "Heat",
                "text": "An intro to thermodynamics.",
                "sections": [],
                "key_points": [],
            }
        )
        self.assertEqual(self._ids("thermodynamics"), [named, mentioned])

    def test_device_folder_names_and_requests_are_not_found(self):
        self._recording(transcript="[00:01] Speaker 1: rec")  # the word, once
        self._recording(kind="command", transcript="[00:01] Speaker 1: remind me to buy milk")
        self.assertEqual(len(self._ids("rec")), 1)
        self.assertEqual(self._ids("milk"), [])

    def test_the_index_follows_changes(self):
        sid = self._recording(title="Optics", transcript="[00:01] Speaker 1: light")
        self.assertEqual(self._ids("optics"), [sid])
        self.store.set_session_title(sid, "Lenses", source="user")
        self.assertEqual(self._ids("optics"), [])
        self.assertEqual(self._ids("lenses"), [sid])
        self.store.add_artifact(sid, "transcript", "[00:01] Speaker 1: shadow")
        self.assertEqual(self._ids("light"), [])
        self.assertEqual(self._ids("shadow"), [sid])
        self.store.delete_session(sid)
        self.assertEqual(self._ids("lenses"), [])
        with self.store._tx() as conn:
            left = conn.execute("SELECT COUNT(*) FROM search_rows").fetchone()[0]
        self.assertEqual(left, 0)

    def test_notes_tasks_and_events(self):
        notes = NotesService(self.store)
        note = notes.create(title="Ideas", body_md="## Plans\n- buy a **telescope**")
        task = TasksService(self.store).create(title="Return the telescope", kind="task")
        event = EventsService(self.store).create(
            title="Stargazing",
            kind="other",
            starts_at="2026-02-01T20:00:00Z",
            location="Telescope hill",
        )
        found = search.search(self.store, "telescopes")
        self.assertEqual([h.item_id for h in found["note"]], [note.id])
        self.assertEqual(found["note"][0].best["text"], "buy a telescope")
        self.assertEqual([h.item_id for h in found["task"]], [task.id])
        self.assertEqual([h.item_id for h in found["event"]], [event.id])
        # An edit in the same second as the last search is still seen.
        notes.update(note.id, body_md="buy a microscope")
        self.assertEqual(self._ids("telescope", "note"), [])
        notes.delete(note.id)
        self.assertEqual(self._ids("microscope", "note"), [])


class SearchApiTests(_Base):
    def test_groups_and_links(self):
        sid = self._recording(
            title="Optics",
            transcript=(
                "[00:05] Speaker 1: The exam covers lenses.\n[02:00] Speaker 1: Lenses again."
            ),
        )
        task = TasksService(self.store).create(
            title="Revise lenses", kind="deadline", due_at="2026-01-20T17:00:00Z"
        )
        EventsService(self.store).create(
            title="Lens lab", kind="other", starts_at="2026-01-21T09:00:00Z"
        )
        body = self.client.get("/api/v1/search?q=lens").get_json()
        groups = {g["type"]: g for g in body["groups"]}
        self.assertEqual(set(groups), {"recording", "task", "event"})
        rec = groups["recording"]["items"][0]
        self.assertEqual(rec["meta"], "Lecture · Thu 15 Jan 2026, 10:00")
        self.assertIn(["lenses", True], rec["snippet"])
        url = urlsplit(rec["url"])
        self.assertEqual(url.path, "/lectures")
        self.assertEqual(parse_qs(url.query), {"session": [sid], "hl": ["lens"], "t": ["5000"]})
        self.assertEqual([m["label"] for m in rec["moments"]], ["0:05", "2:00"])
        self.assertEqual(groups["task"]["items"][0]["url"], f"/todos#todo-{task.id}")
        self.assertEqual(
            "Deadline · due Tue 20 Jan 2026, 17:00", groups["task"]["items"][0]["meta"]
        )
        # An event opens its day in the calendar.
        self.assertEqual(
            groups["event"]["items"][0]["url"], "/calendar?date=2026-01-21&day=2026-01-21"
        )
        self.assertEqual(groups["event"]["items"][0]["meta"], "Event · Wed 21 Jan 2026, 09:00")

    def test_memos_open_in_notes(self):
        sid = self._recording(kind="voice_memo", transcript="[00:01] Speaker 1: call the plumber")
        from services.notes import ensure_memo_note

        note = ensure_memo_note(self.app.config["AFTERWORD_STORAGE"], sid)
        groups = self.client.get("/api/v1/search?q=plumber").get_json()["groups"]
        self.assertEqual([g["type"] for g in groups], ["note"])  # once, as its note
        item = groups[0]["items"][0]
        self.assertEqual(item["url"], f"/notes?note_id={note.id}&hl=plumber")
        self.assertTrue(item["meta"].startswith("Voice memo · edited"))

    def test_note_snippets_read_without_markdown(self):
        NotesService(self.store).create(
            title="Sources", body_md="- [x] The **policy** on [recording](http://x.y) lectures"
        )
        item = self.client.get("/api/v1/search?q=policy").get_json()["groups"][0]["items"][0]
        self.assertEqual(
            "".join(part for part, _ in item["snippet"]), "The policy on recording lectures"
        )

    def test_empty_query(self):
        body = self.client.get("/api/v1/search?q=").get_json()
        self.assertEqual(body["groups"], [])

    def test_opened_page_marks_the_words(self):
        sid = self._recording(
            title="Optics", transcript=("[00:05] Speaker 1: The <b>lenses</b> are cheap.")
        )
        body = self.client.get(f"/lectures?session={sid}&hl=lens").get_data(as_text=True)
        self.assertIn("&lt;b&gt;<mark>lenses</mark>&lt;/b&gt;", body)
        plain = self.client.get(f"/lectures?session={sid}").get_data(as_text=True)
        self.assertNotIn("<mark>", plain)
