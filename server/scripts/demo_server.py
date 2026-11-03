r"""A second server on 127.0.0.1:8010 with made-up demo data, for
screenshots and trying the UI without touching real data.

Throwaway data dir (%TEMP%/afterword-demo-data, rebuilt each start), no AI
keys, no calendar syncing, and "now" pinned to today 13:42 local so the
schedules look like a normal day.

    cd server
    .venv\Scripts\python.exe scripts\demo_server.py
"""
import dataclasses
import datetime as dt
import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
import config as config_mod
import logsetup
import timeutil

DATA = Path(tempfile.gettempdir()) / "afterword-demo-data"
shutil.rmtree(DATA, ignore_errors=True)

_real_now = timeutil.local_now
TODAY = _real_now().date()
NOW = dt.datetime.combine(TODAY, dt.time(13, 42), tzinfo=timeutil.local_tz())
timeutil.local_now = lambda: NOW
timeutil.local_today = lambda: TODAY

from app import create_app

logsetup.setup(log_file="")

cfg = dataclasses.replace(config_mod.load(), data_dir=DATA, port=8010, transcription_api_key="",
                          text_api_key="", web_password="")
app = create_app(cfg)
app.config["AFTERWORD_SUBSCRIPTION_SYNC"] = False
store = app.config["AFTERWORD_STORAGE"]
courses = app.config["AFTERWORD_COURSES_SERVICE"]
events = app.config["AFTERWORD_EVENTS_SERVICE"]
tasks = app.config["AFTERWORD_TASKS_SERVICE"]
notes = app.config["AFTERWORD_NOTES_SERVICE"]
store.set_setting("theme", "dark")


def at(day_offset, h, m=0):
    return timeutil.fmt(dt.datetime.combine(TODAY + dt.timedelta(days=day_offset), dt.time(h, m), tzinfo=timeutil.local_tz()))


la = courses.create(name="Linear Algebra", code="MATH2341")
th = courses.create(name="Thermodynamics")
mi = courses.create(name="Microeconomics")
wr = courses.create(name="Academic Writing")


def recording(title, kind, course, started, *, status="done", summary=None, lines=(), seconds=0):
    sid = uuid.uuid4().hex
    store.upsert_session(sid, title="rec", expected_count=1, sample_rate=22050, channels=1,
                         bits_per_sample=32, started_at=started)
    with store._tx() as conn:
        conn.execute("UPDATE sessions SET status=?, kind=?, course_id=?, title=?, title_source='ai' WHERE id=?",
                     (status, kind, course.id if course else None, title, sid))
    if lines:
        store.add_artifact(sid, "transcript", "\n".join(f"[{t // 60:02d}:{t % 60:02d}] Speaker {s}: {x}" for t, s, x in lines))
    if summary:
        store.add_artifact(sid, "summary", json.dumps({"text": summary, "sections": [], "key_points": []}))
    return sid


la8 = recording("Eigenvalues and diagonalisation", "lecture", la, at(0, 9),
                summary="How to find eigenvalues from the characteristic polynomial, when a matrix can be diagonalised, and a worked 3x3 example.",
                lines=[(12, 1, "Good morning. Today: eigenvalues."), (2480, 1, "For Tuesday, read chapter 5, and problem set 4 is due Tuesday night.")])
th6 = recording("Entropy and the second law", "lecture", th, at(0, 11), status="summarising",
                lines=[(8, 1, "Last time we stopped at the Carnot cycle.")])
memo = recording("Call the plumber about the boiler", "voice_memo", None, at(0, 8, 2),
                 lines=[(0, 1, "Remind me to call the plumber about the boiler before the weekend.")])
meet = recording("Shareholder vote and weekend availability", "meeting", None, at(-1, 14, 25),
                 summary="Who can attend the vote on Saturday, and who collects the proxy forms before Friday.",
                 lines=[(372, 1, "Someone needs to collect the proxy forms by Friday.")])
mi5 = recording("Price elasticity of demand", "lecture", mi, at(-2, 9),
                summary="Elastic and inelastic goods, why it matters for pricing, and the list of midterm topics.",
                lines=[(4360, 1, "The midterm is on the twelfth, nine o'clock, in the main hall.")])
recording("Project sync with Sam", "meeting", None, at(-3, 15),
          summary="Slides are due on Friday; Sam presents the demo.", lines=[(1325, 1, "I will email you the slides by Thursday.")])

e = events.create(title="Linear Algebra · Lecture 8", kind="lecture", starts_at=at(0, 9), ends_at=at(0, 10, 30), location="Room B12", course_id=la.id)
with store._tx() as conn:
    conn.execute("UPDATE events SET matched_session_id=? WHERE id=?", (la8, e.id))
e = events.create(title="Thermodynamics · Lecture 6", kind="lecture", starts_at=at(0, 11), ends_at=at(0, 12), location="Main hall", course_id=th.id)
with store._tx() as conn:
    conn.execute("UPDATE events SET matched_session_id=? WHERE id=?", (th6, e.id))
events.create(title="Project sync with Sam", kind="meeting", starts_at=at(0, 14), ends_at=at(0, 14, 30), location="Library room 3",
              description="Go through the slides and who presents the demo.")
events.create(title="Lecture 1", kind="lecture", starts_at=at(0, 16), ends_at=at(0, 17, 30), location="No room yet")
monday = -TODAY.weekday()
events.create(title="Linear Algebra · Lecture 7", kind="lecture", starts_at=at(monday, 10), ends_at=at(monday, 11, 30), location="Room B12", course_id=la.id)
events.create(title="Microeconomics · Lecture 5", kind="lecture", starts_at=at(monday + 2, 9), ends_at=at(monday + 2, 10, 20), location="Room A3", course_id=mi.id)
events.create(title="Academic Writing · Seminar 4", kind="lecture", starts_at=at(monday + 1, 11), ends_at=at(monday + 1, 11, 50), location="Room C2", course_id=wr.id)

tasks.create(title="Problem set 4", kind="deadline", due_at=at(4, 23, 59), course_id=la.id, source="extracted", source_ref=la8, source_offset_ms=2480000)
tasks.create(title="Lab report: entropy", kind="deadline", due_at=at(7, 17), course_id=th.id)
tasks.create(title="Midterm", kind="deadline", due_at=at(13, 9), course_id=mi.id, source="extracted", source_ref=mi5, source_offset_ms=4360000)
tasks.create(title="Essay draft", kind="deadline", due_at=at(17, 0), course_id=wr.id)
tasks.create(title="Email Sam the slides", kind="task", priority="high", due_at=at(-1, 0))
tasks.create(title="Collect the proxy forms", kind="task", priority="high", due_at=at(0, 0), source="extracted", source_ref=meet, source_offset_ms=372000)
tasks.create(title="Read chapter 5 before Tuesday", kind="task", priority="medium", due_at=at(4, 0), source="extracted", source_ref=la8, source_offset_ms=2480000)
tasks.create(title="Return the Ford, check the deposit", kind="task", priority="low")
done1 = tasks.create(title="Buy printer paper", kind="task", priority="medium")
tasks.set_status(done1.id, "done")
done2 = tasks.create(title="Form group for assignment", kind="deadline", due_at=at(-6, 17), course_id=wr.id)
tasks.set_status(done2.id, "done")
notes.create(title="Ideas for the essay", course_id=wr.id, body_md=(
    "## Argument\nA recorder lets attention go to the argument instead of to writing everything down.\n\n"
    "## Three angles\n- Accessibility: students who cannot write fast\n- Memory: going back to the moment that matters\n"
    "- Privacy: the recordings stay on my own server\n\n## Sources to check\n"
    "- [ ] Mueller and Oppenheimer (2014), longhand vs laptop notes\n- [x] The university policy on recording lectures"))
notes.create(title="Exam topics", course_id=mi.id, body_md=(
    "## Midterm, Thu 12 Nov\n- Price elasticity of demand\n- Consumer surplus\n\nRevise the elasticity examples from lecture 5 *first*."))
from services.notes import sync_memo_notes

sync_memo_notes(store)

print("[demo] http://localhost:8010", flush=True)
app.run(host="127.0.0.1", port=8010, debug=False, threaded=True)
