# AI NoteTaker server

A small Flask server for the lecture recorder in [`../hardware`](../hardware).
The device uploads each recording in AAC chunks; the server stitches them,
then transcribes, classifies, summarises and extracts tasks in the
background, and serves a web UI for lectures, meetings, notes (with voice
memos), other recordings, tasks and deadlines, and a calendar, with
one search box across all of it.

- **Storage:** SQLite plus one folder per recording under `data/`. Audio is
  AAC-LC, 22050 Hz mono, 64 kbps (ADTS), the same format the device records.
- **AI:** OpenRouter for both models. Transcription:
  `microsoft/mai-transcribe-2` (with speaker labels). Text: `minimax/minimax-m3`.
  Both can be changed in `.env`, and the text model can be called on any
  OpenAI-compatible API instead (`AFTERWORD_TEXT_BASE_URL`), e.g. with a MiniMax
  subscription.
- **Times:** stored in UTC, shown in local time (`AFTERWORD_TIMEZONE`, default
  Europe/London).

## Setup

Needs Python 3.11 or newer and ffmpeg.

```powershell
cd server
setup.cmd                     # Windows: venv, pip install, ffmpeg via winget
```

`setup.sh` does the same on Linux/macOS. By hand:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
winget install Gyan.FFmpeg    # or: apt install ffmpeg / brew install ffmpeg
```

Copy `.env.example` to `.env` and add your OpenRouter key
(`AFTERWORD_TRANSCRIPTION_API_KEY` and `AFTERWORD_TEXT_API_KEY`, which can be the same
key). Without keys, recordings are still stored and playable, just not
transcribed.

## Run

```powershell
.venv\Scripts\python.exe app.py      # development (Flask's server)
.venv\Scripts\python.exe serve.py    # production (waitress)
```

Then open `http://localhost:8000`. By default the server only listens on
this machine. For the recorder (or a phone) to reach it, set
`AFTERWORD_HOST=0.0.0.0` and a password (`AFTERWORD_WEB_PASSWORD`): the
server refuses to listen on the network without one. `SERVER_URL` in the
firmware's `secrets.h` must point at this machine's LAN address
(`ipconfig`), and on Windows the network must be set to **Private** or the
firewall blocks port 8000.

### On a VPS

Run `serve.py` behind Caddy (HTTPS) with:

```ini
AFTERWORD_HOST=127.0.0.1
AFTERWORD_TRUSTED_PROXY=127.0.0.1
AFTERWORD_COOKIE_SECURE=1
AFTERWORD_WEB_PASSWORD=...        # login for the web UI
AFTERWORD_DEVICE_TOKEN=...        # = DEVICE_TOKEN in the firmware's secrets.h
```

Use one process: the AI pipeline worker runs inside it.

## Configuration

All settings are environment variables, read from `server/.env` too.
Relative paths are taken from the `server` folder.

| Variable | Default | Purpose |
|---|---|---|
| `AFTERWORD_DATA_DIR` | `./data` | Database and audio |
| `AFTERWORD_HOST` / `AFTERWORD_PORT` | `127.0.0.1` / `8000` | Bind address; anything but loopback needs a password |
| `AFTERWORD_TRANSCRIPTION_API_KEY` | — | OpenRouter key for transcription |
| `AFTERWORD_TEXT_API_KEY` | — | OpenRouter key for the text model |
| `AFTERWORD_TRANSCRIPTION_MODEL` | `microsoft/mai-transcribe-2` | Speaker labels need `verbose_json` support |
| `AFTERWORD_TEXT_MODEL` | `minimax/minimax-m3` | Classification, summaries, extraction |
| `AFTERWORD_SUMMARY_LANGUAGES` | `English, Russian` | Summaries keep these languages; others are written in the first |
| `AFTERWORD_TEXT_BASE_URL` | OpenRouter | Text model API, e.g. `https://api.minimax.io/v1` (then `AFTERWORD_TEXT_MODEL=MiniMax-M3`) |
| `AFTERWORD_TRANSCRIBE_SPEAKERS` | `1` | `0` = plain transcript without speaker lines |
| `AFTERWORD_TRANSCRIBE_CHUNK_SECONDS` | `1500` | Long recordings are sent in pieces this long |
| `AFTERWORD_TRANSCRIBE_PROVIDER_OPTIONS` | Azure diarization on | JSON passed as `provider.options` |
| `AFTERWORD_PIPELINE_BACKGROUND` | `1` | `0` = run the AI pipeline inside the request (debugging) |
| `AFTERWORD_FFMPEG_PATH` | `ffmpeg` | ffmpeg binary |
| `AFTERWORD_MAX_UPLOAD_BYTES` | 500 MB | Largest web upload; bigger requests get 413 |
| `AFTERWORD_TIMEZONE` | `Europe/London` | Zone for times without an offset, and for display |
| `AFTERWORD_DEVICE_TOKEN` | off | Device uploads need `Authorization: Bearer <token>` |
| `AFTERWORD_WEB_PASSWORD` | off | Login for the web UI and the non-device API |
| `AFTERWORD_SECRET` | generated | Login cookie key (else kept in `data/db/secret_key`) |
| `AFTERWORD_COOKIE_SECURE` | off | Login cookie over HTTPS only |
| `AFTERWORD_ALLOW_OPEN` | off | Listen on the network without a password (trusted networks only) |
| `AFTERWORD_TRUSTED_PROXY` | off | `serve.py`: trust X-Forwarded-* from this address |
| `AFTERWORD_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `AFTERWORD_LOG_FILE` | console | Log to this file instead (rotated at 5 MB, 3 old files kept) |

Settings with the old `LR_` prefix (`LR_TEXT_API_KEY` and so on) are
still read; the `AFTERWORD_` name wins when both are set.

## Tests

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests       # all tests
.venv\Scripts\python.exe -m unittest tests.test_tasks        # one module
.venv\Scripts\python.exe scripts\run_tests.py                # same, output to test.log
.venv\Scripts\python.exe scripts\e2e_check.py [--live]       # device protocol end to end
.venv\Scripts\python.exe scripts\upload_smoke.py             # web upload end to end
```

Tests that need ffmpeg are skipped without it. None of them call the AI
APIs.

To try the web UI with made-up data, without touching real data:

```powershell
.venv\Scripts\python.exe scripts\demo_server.py              # http://127.0.0.1:8010
```

## Running it on the home network

`scripts\run_home.ps1` runs the production server as **http://afterword.local**
(port 80, plus 8000 for the recorder), logging to `data\server.log`.
`scripts\autostart.ps1` makes it start at Windows logon (`-Remove` undoes it).

## Layout

```
server/
├── app.py            create_app(): wiring; python app.py = dev server
├── serve.py          production entry point (waitress)
├── config.py         settings from env / .env
├── auth.py           device token, web login, session key
├── logsetup.py       log format, level, rotating log file
├── timeutil.py       UTC storage / local display rules
├── storage.py        SQLite + files: sessions, chunks, stitching, artifacts
├── search.py         search index and queries (see Search)
├── services/         processing (AI pipeline), llm (OpenRouter), transcode
│                     (ffmpeg), notes, tasks, events (+ ICS), courses,
│                     classification
├── web/              routes as blueprints
│   ├── pages.py          HTML pages
│   ├── api_sessions.py   device protocol, sessions, audio, pipeline
│   ├── api_search.py     the dashboard's search box
│   ├── api_notes.py, api_tasks.py, api_events.py, api_courses.py
│   ├── common.py         accessors, template filters, helpers
│   └── pipeline.py       background worker and boot sweep
├── templates/, static/
├── tests/
└── scripts/          tests and checks, demo server, browser check, home-network start
```

## Device protocol

All under `/api/v1/`. With `AFTERWORD_DEVICE_TOKEN` set, every call needs
`Authorization: Bearer <token>`.

1. **Announce**: `POST /sessions`

   ```json
   {"id": "1408_rec_2026-10-30T0111", "title": "rec_2026-10-30T0111",
    "expected_count": 4, "sample_rate": 22050, "channels": 1,
    "bits_per_sample": 16, "started_at": "2026-10-30T01:11:00"}
   ```

   `201` on create, `200` if the id exists (the original row is kept).
   `started_at` without an offset is local time; it is stored in UTC.
2. **Chunks**: `PUT /sessions/<id>/chunks/<k>` for k = 1..N, body = raw
   ADTS bytes. Re-sending a chunk is safe (written to a temp file, then
   renamed). `200` stored, `404` unknown session, `409` session no longer
   receiving.
3. **Finish**: `POST /sessions/<id>/finish`
   - `200` with `reason: "stitched"` (or `"already_ready"` when repeated):
     the audio is stored, so the device may delete its copy. The AI
     pipeline runs afterwards in the background.
   - `409` with `{"error": "missing_chunks", "missing": [3, 7]}`: send those
     chunks again, then finish again.

The firmware deletes a session from the SD card only after a `200` finish.

## AI pipeline

After finish, a single background worker processes sessions one at a time:
transcribe → classify (lecture, meeting, voice memo, command, other) →
summarise → extract items → file them, and matches the recording to a
calendar event by start time.

- Each piece is sent to the transcription API as 16 kHz mono MP3: the
  Azure-hosted model rejects AAC.
- **Names:** the summary includes a short title in the recording's
  language; a recording matched to a calendar event is "<course or
  event>: <topic>". Pages show the date, time and length under the name.
  A name given by hand (the pencil) is never replaced.
- **Filing** (`services/filing.py`): deadlines, to-dos and dated events
  from lectures and meetings go into Deadlines, Todos and the Calendar,
  linked to the moment in the recording; the detail pane lists them.
  Duplicates of open items and dates before the recording are skipped;
  re-processing replaces only items never touched. Voice memos and
  "other" recordings (including videos playing) file nothing.
- **Command** recordings ("remind me to call the dentist tomorrow") only
  file their request, without a link, and are then deleted: the
  recording is not kept. One that filed nothing (probably misheard)
  moves to Other instead.
- Summaries are sized to the transcript (a few sentences for a short memo,
  headed sections for an hour-long lecture) and structured by kind. They
  are written in the recording's language if it is in
  `AFTERWORD_SUMMARY_LANGUAGES`, otherwise in the first of those.
- Voice memos keep only their transcript (no summary or extraction) and
  become notes: a note with the recording attached, whose text starts as
  the transcript and can be edited (the exact words stay under "What was
  said"). Deleting the note deletes the recording; moving the recording
  to another page removes the note (`services/notes.py`).
- A recording with no speech ends done ("no speech detected"), without a
  kind; it is listed on the Other page.
- Changing a finished recording's kind by hand ("Classify as" on the
  Other page or a memo) rewrites its summary for the new kind, reusing
  the transcript.
- After a server restart, sessions stranded mid-pipeline are requeued;
  finished steps are not repeated.
- A failed session (API error) stays failed and is listed on the Other
  page; **Retry** there (`POST /api/v1/sessions/<id>/transcribe`) runs it
  again. Other also lists recordings with no kind yet (still being
  processed) and is where they are sorted by hand.

Every recording is read on one page: Lectures, Meetings, Notes (voice
memos) or Other (everything else). Links to a recording (search results,
"Said in" on to-dos) open it there (`web.common.recording_url`).

Each recording's pane also has: clickable transcript timestamps (play
from there), **Export (.md)** (name, summary, items, transcript),
**Download audio**, and **Delete** (`DELETE /api/v1/sessions/<id>`; the
items it filed stay, unlinked).

## Search

The search box on the dashboard searches recordings (names, summaries,
transcripts), notes, to-dos and deadlines, and calendar events. Matches
drop down as you type, grouped; Enter or a click opens one, and `/`
jumps to the box.

- **Word forms:** words are matched by their stems (snowballstemmer,
  Russian or English rules by alphabet), so "лекция" finds "лекции" and
  "meeting" finds "meetings". The word being typed matches by its
  beginning too. All words must be in one piece (a transcript line, a
  summary paragraph, a line of a note).
- **Recordings** show the first line that mentions the words and the
  times of the other mentions; each opens the recording there, with the
  matched words marked (`?hl=`) and the audio cued (`?t=` in ms).
  "Command" recordings are not searched.
- **Index:** `search_rows` / `search_fts` in the database, kept up to
  date before every search: anything added, changed or deleted since
  the last search is re-indexed then (`search.sync`), so nothing else
  has to remember to update it.
- **API:** `GET /api/v1/search?q=...` returns the groups with links.

## Data

```
data/
├── db/metadata.db        SQLite (WAL)
├── db/secret_key         login cookie key, when AFTERWORD_SECRET is unset
└── sessions/<id>/
    ├── full.aac          stitched audio
    ├── meta.json         copy of the database row, for debugging
    └── chunks/           only until stitched
```

Deleting `data/` resets the server.

## Troubleshooting

- **The device cannot connect:** check `SERVER_URL`, the Windows network
  profile (Private), and that the server listens on `0.0.0.0`.
- **`NOT STARTING: AFTERWORD_HOST=0.0.0.0 ...`:** the server is set to be
  reachable from other devices but has no password; set
  `AFTERWORD_WEB_PASSWORD`.
- **`Provider returned 400` on transcription:** the model rejected the
  audio or an option. MP3 is accepted by the default model; check
  `AFTERWORD_TRANSCRIPTION_PROVIDER_OPTIONS` when changing models.
- **`404 ... unavailable for free`:** the text model's free tier was
  withdrawn; set `AFTERWORD_TEXT_MODEL`.
- **Port 8000 in use:** set `AFTERWORD_PORT`.
