# Lecture Recorder Server — Phase A

A small personal server that receives audio recordings from an ESP32
device over the local network, stitches the chunks into a single AAC
file per session, and serves a minimal web UI to browse and play them.

> **Phase A scope: storage only.** Receive → store → list → play.
> Transcription, summarisation, search, courses, and AI features are
> explicitly out of scope; the data model leaves room for them.

## What works

1. Device announces a session and how many chunks to expect.
2. Device uploads chunks one at a time over
   `PUT /api/v1/sessions/<id>/chunks/<N>`.
3. Device signals the session is finished.
4. Server stitches all chunks into a single `full.aac` and marks the
   session `ready`.
5. You open `http://<server-ip>:8000/` in a browser, see the list of
   recordings, click one, and the audio plays in the browser.

Re-sending the same chunk is safe (atomic write to a temp file, then
rename). Finishing a session with missing chunks fails with a clear
`409 Conflict` response listing the gaps. Restarting the server keeps
everything; deleting the `data/` directory gives a clean slate.

## Quick start

### Prerequisites

* **Python 3.11 or newer** (3.12 / 3.13 / 3.14 all work). Verify
  with `python --version`.
* **ffmpeg** (required) — all audio is stored as canonical AAC-LC
  (22050 Hz mono 64 kbps); uploads in any other format are
  transcoded on ingest, and transcription needs ffmpeg to segment
  long recordings.
  * Windows: `winget install Gyan.FFmpeg` (or `choco install ffmpeg`)
  * Linux: `sudo apt install ffmpeg` (or `dnf install ffmpeg`)
  * macOS: `brew install ffmpeg`
* No database, no Docker, no Node — just Python and Flask.

### One-shot install

The repo includes `setup.cmd` (Windows) and `setup.sh` (Linux/macOS)
that create the venv, install Python deps, and try to install ffmpeg
for you.

**Windows:**

```powershell
cd "C:\Users\pozig\Documents\Visual Studio\AI_NoteTaker\Server"
setup.cmd
```

**Linux / macOS:**

```bash
cd /path/to/AI_NoteTaker/Server
chmod +x setup.sh
./setup.sh
```

### Manual install

If you'd rather run the steps yourself:

```powershell
cd "C:\Users\pozig\Documents\Visual Studio\AI_NoteTaker\Server"
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

```bash
cd /path/to/AI_NoteTaker/Server
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run

```powershell
python app.py
```

You should see:

```
[LR ] data dir: C:\Users\...\Server\data
[LR ] DB ready:  C:\Users\...\Server\data\db\metadata.db
[LR ] ffmpeg: ffmpeg version ...        (or a WARNING if not installed)
[LR ] listening on http://0.0.0.0:8000
 * Running on http://127.0.0.1:8000
 * Running on http://192.168.x.y:8000
```

Open `http://localhost:8000` in your browser. The first time, you'll
see the empty-state card "No recordings yet".

### Reset to a clean slate

```powershell
# Stop the server (Ctrl+C), then:
rmdir /s /q data
# Optional: drop the venv too
rmdir /s /q .venv
```

The data directory is recreated on the next start. **There is no
delete UI in Phase A** — manual file deletion is the reset mechanism.

## Configuration

Three environment variables (all optional). See `.env.example` for a
template you can copy to `.env`.

| Var | Default | Purpose |
|---|---|---|
| `LR_DATA_DIR` | `./data` | Where SQLite DB and audio files live |
| `LR_HOST` | `0.0.0.0` | Bind address (use `127.0.0.1` to be extra strict) |
| `LR_PORT` | `8000` | Port |

Example for moving the data dir off your system drive:

```powershell
$env:LR_DATA_DIR = "D:\lecture-data"
python app.py
```

For deployment to a Linux server later, just point `LR_DATA_DIR` at a
persistent location and set `LR_HOST=0.0.0.0` (or restrict at the
firewall level).

## Running tests

```powershell
python -m unittest discover -s tests -v
```

45 tests cover the storage layer (atomic writes, retry safety, finish
gating, stitching, status transitions), the HTTP API (announce,
upload, finish, range requests, the web UI, the search stub), and
the web upload path (page rendering, format sniffing, canonical-WAV
skip, ffmpeg-missing 503, title defaulting, size limits).

End-to-end smoke check (in-process, no network):

```powershell
python e2e_check.py
```

Or against a real running server (starts the dev server in a thread,
runs HTTP requests, shuts down):

```powershell
python e2e_check.py --live
```

The web upload path has its own end-to-end check:

```powershell
python upload_smoke.py
```

## Project layout

```
Server/
├── app.py                 # Flask app: routes, request handling
├── config.py              # env-var config
├── storage.py             # SQLite + filesystem storage layer
├── search.py              # FTS5 search (Phase A: stub)
├── services/
│   ├── __init__.py
│   ├── llm.py             # Transcription + text-model providers (OpenRouter)
│   ├── processing.py      # post-finish pipeline orchestrator stub
│   └── transcode.py       # ffmpeg wrapper, format sniff
├── templates/
│   ├── base.html
│   ├── index.html         # session list
│   ├── session.html       # one session + player
│   ├── upload.html        # web upload page
│   └── _macros.html       # shared Jinja macros
├── static/
│   ├── css/
│   │   ├── tokens.css     # design tokens (colours, spacing, type)
│   │   ├── base.css       # resets, typography, layout
│   │   ├── components.css # cards, badges, tables, player
│   │   └── upload.css     # drop zone, file picker
│   └── js/
│       ├── app.js         # progressive enhancement (currently empty)
│       └── upload.js      # upload page logic
├── tests/
│   ├── conftest.py
│   ├── test_storage.py
│   ├── test_api.py
│   └── test_upload.py
├── e2e_check.py           # device-flow end-to-end check
├── upload_smoke.py        # web-upload end-to-end check
├── setup.cmd              # Windows one-shot install
├── setup.sh               # Linux/macOS one-shot install
├── requirements.txt
├── .python-version        # 3.11
├── .env.example
├── .gitignore
└── README.md
```

## API reference (device-facing)

All paths under `/api/v1/`. JSON request/response bodies unless noted.

### `POST /api/v1/sessions` — announce

Idempotent on `id` (the device should generate and own the session id,
e.g. a UUID hex string).

```http
POST /api/v1/sessions HTTP/1.1
Content-Type: application/json

{
  "id": "a3f1c0ffeebbeef00",
  "title": "CS101 Lecture 3",
  "expected_count": 12,
  "sample_rate": 22050,
  "channels": 1,
  "bits_per_sample": 32,
  "started_at": "2026-10-03T12:00:00Z"
}
```

* `201 Created` on first create.
* `200 OK` if the id already exists (the original row is preserved).
* `400 Bad Request` on bad audio format or invalid id.

### `PUT /api/v1/sessions/<id>/chunks/<N>` — upload one chunk

Body: the raw AAC (ADTS) bytes, as encoded by the device from its
recorded WAV. Re-PUT is safe (atomic write via temp file + rename).
Out-of-order uploads are fine.

```http
PUT /api/v1/sessions/a3f1c0ffeebbeef00/chunks/1 HTTP/1.1
Content-Type: audio/aac

<binary AAC bytes>
```

* `200 OK` with `{session_id, chunk_index, byte_size, received_at}`.
* `400 Bad Request` on empty body.
* `404 Not Found` if the session was never announced.
* `409 Conflict` if the session is not in `receiving` or `failed` state
  (e.g. already `ready`).

### `POST /api/v1/sessions/<id>/finish` — validate and stitch

Empty body. On success the server:

1. Confirms every chunk `1..expected_count` is present.
2. Stitches the chunks into a single `full.aac` (atomic write).
   ADTS AAC is self-delimiting, so stitching is a plain binary
   concatenation — no re-encode, no quality loss.
3. Deletes the chunk files (the DB rows stay as provenance).
4. Marks the session `ready`, sets `finished_at`, and returns
   immediately — the AI pipeline runs on the background worker
   afterwards (see "Background AI pipeline & restarts").
5. Is idempotent: re-sending finish for an already-stitched session
   returns `200` with `reason: "already_ready"` and does nothing.

```http
POST /api/v1/sessions/a3f1c0ffeebbeef00/finish HTTP/1.1
```

* `200 OK` with `{ok, reason, session}`. `reason` is `stitched` on
  first finish and `already_ready` on re-call.
* `409 Conflict` with `{error: "missing_chunks", missing: [3, 7],
  present: [1,2,4,5,6,8]}` if any chunks are missing.
* `500 Internal Server Error` if stitching itself fails.

## Background AI pipeline & restarts

The AI pipeline (transcribe → classify → summarise → extract) runs on
a single FIFO background worker: sessions are processed strictly one
at a time, in the order they were finished or uploaded, so three
lectures recorded in a day are transcribed sequentially instead of
hammering the transcription API in parallel.

What each failure looks like:

* **Device finish times out** — the stitch itself is fast (<1 s);
  only the AI work is slow. The device should re-send finish: the
  server replies `200 already_ready` without re-stitching. The
  already-queued pipeline keeps running.
* **Server restart mid-pipeline** — the session would be stranded in
  an intermediate status (`transcribing`, `summarising`, ...) with
  the chunk files already deleted. On the next boot a sweep finds
  every such session, resets it to `ready`, and requeues it.
  Artifacts persisted before the crash (transcript, summary) are
  reused, so the expensive transcription step is **not** repeated —
  a restart costs nothing except the steps that hadn't run yet.
* **Pipeline fails** (bad audio, API error) — the session is marked
  `failed` with a reason in `status_detail`. Failed sessions are
  deliberately *not* auto-retried (a missing API key would loop
  forever): use the "Retry" action on the session page (the
  `POST /api/v1/sessions/<id>/transcribe` endpoint), which re-runs
  the whole pipeline from scratch.

Set `LR_PIPELINE_BACKGROUND=0` to run the pipeline synchronously
inside the HTTP request instead (slower responses; useful for
debugging).

### `GET /api/v1/sessions` — list all sessions

JSON list, newest first.

### `GET /api/v1/sessions/<id>` — session detail

JSON: `{session: {...}}` matching the schema below.

### `GET /api/v1/sessions/<id>/audio` — stream the audio

Streams `full.aac` with `Content-Type: audio/aac` and HTTP Range
support, so the browser's `<audio>` element can seek. Returns
`409 Conflict` if the session is not yet `ready`.

### `GET /api/v1/search?q=...` — search (Phase A stub)

Always returns `{hits: []}`. The endpoint exists so the UI can wire up
a search box without a contract change later; the implementation is a
no-op until the FTS5 index is populated.

### `POST /api/v1/uploads/audio` — web upload (single file)

Used by the `/upload` page. Accepts a multipart form with:

* `file` — the audio bytes (any format the server's ffmpeg can read,
  plus AAC always).
* `title` — optional. Defaults to `rec_YYYY-MM-DDTHHMM` (the device's
  naming scheme) based on the current time.

The server sniffs the file format. If it's already canonical AAC
(ADTS), it skips ffmpeg. Otherwise it transcodes the file to
AAC-LC 22050 Hz mono 64 kbps, creates a one-chunk session, finishes
it (equivalent to one-shot stitch), and returns:

```json
{
  "created": true,
  "session": { ... },
  "redirect_url": "/sessions/<id>"
}
```

Responses:

* `201 Created` on success.
* `400 Bad Request` on missing file, empty file, or invalid title.
* `413 Payload Too Large` if the file exceeds `LR_MAX_UPLOAD_BYTES`.
* `503 Service Unavailable` if the file needs transcoding but ffmpeg
  isn't available.

## Web UI

* `/` — list of all recordings, newest first. Each row shows title,
  start time, status, chunks received / expected, duration, size.
* `/sessions/<id>` — detail view with audio player and download button.

Status badges:

* **Receiving** (blue): announced, chunks still arriving.
* **Stitching** (amber): all chunks present, server is concatenating.
* **Ready** (green): `full.aac` exists, playable.
* **Failed** (red): something went wrong (missing chunks, stitch
  error). The device can re-send missing chunks and call `/finish`
  again to recover.

## Web upload (iPhone Voice Memos and other files)

Use the **Upload** link in the header to open `/upload`. Drag a single
audio file onto the drop zone (or click to browse), optionally edit
the title, and click Upload. The page accepts:

* **WAV** at any sample rate / channel / bit depth (transcoded to
  canonical AAC).
* **M4A / AAC** from iPhone Voice Memos (needs ffmpeg on the server).
* **MP3, OGG, FLAC, WebM** (needs ffmpeg).

Title defaults to `rec_YYYY-MM-DDTHHMM` (the same scheme the device
uses) based on the current server time. Edit it before uploading if
you want a different label.

Without ffmpeg, the upload page shows a yellow warning and rejects any
non-AAC file. Install ffmpeg (or set `LR_FFMPEG_PATH` to your binary)
to enable the WAV / iPhone / MP3 / OGG paths.

## Data model

### Filesystem layout

```
data/
├── db/
│   └── metadata.db          # SQLite (WAL mode)
└── sessions/
    └── <session_id>/
        ├── full.aac         # stitched audio (only after finish)
        ├── meta.json        # debug-friendly mirror of the SQLite row
        └── chunks/          # exists only before stitching
            ├── 001.aac
            ├── 002.aac
            └── ...
```

### SQLite schema

Forward-compatible: tables for future courses, artifacts, and FTS5
search index exist from day one but are unused in Phase A.

* `sessions` — `id`, `course_id` (nullable FK), `title`, `started_at`,
  `finished_at`, `status`, `status_detail`, `expected_count`,
  `sample_rate`, `channels`, `bits_per_sample`, `notes`, `created_at`,
  `updated_at`. Status values Phase A uses: `receiving`, `stitching`,
  `ready`, `failed`. Future phases will add `transcribing`,
  `summarising` to the same column.
* `chunks` — `(session_id, chunk_index)`, `byte_size`, `sha256`,
  `received_at`. Provenance only after stitching; chunk files are
  deleted but the rows remain.
* `courses` — `id`, `name`, `code`, `color`, `sort_order`,
  `created_at`. Reserved for a later phase.
* `artifacts` — `id`, `session_id`, `kind` (`transcript`, `summary`,
  `key_points`, `deadlines`, …), `content`, `model`, `created_at`.
  Reserved for later phases.
* `artifacts_fts` — FTS5 virtual table over artifacts, ready to be
  populated.

## ESP32 device protocol contract

When the firmware is updated to upload, the contract is:

```
1. Generate session_id (UUID hex, 16+ chars).
2. POST /api/v1/sessions  with {id, title?, expected_count,
                                sample_rate, channels, bits_per_sample,
                                started_at}
   - 201 = first time, server created the row.
   - 200 = server already had the id (you crashed previously);
           that's fine, the original session is preserved.
3. For each chunk file (1.aac, 2.aac, ..., N.aac) on the SD card, in
   any order:
     PUT /api/v1/sessions/<id>/chunks/<N>  with the raw AAC bytes
       - 200 = ok.
       - 409 = session not in receiving state; re-list and decide.
4. POST /api/v1/sessions/<id>/finish
   - 200 + reason=stitched  → the server has stitched the audio and
     queued the AI pipeline in the background. **Safe to delete the
     chunk files on the SD card now** — transcription happens
     server-side and can take many minutes, which is why finish
     returns before it completes.
   - 200 + reason=already_ready → this session was already stitched
     (e.g. your previous finish timed out or the server restarted).
     Treat exactly like stitched: safe to delete the SD files.
   - 409 + missing_chunks → re-PUT the missing chunks, retry finish.
```

If the device crashes between any two steps, on next boot it can list
sessions to discover its existing id, re-PUT any missing chunks, and
retry `/finish`. **No state is ever lost to a partial upload**; a torn
upload leaves only a `.tmp` file, the real chunk file is unchanged.

## Future phases (data model already supports these)

The following are intentionally out of scope for Phase A. The data
model already accommodates them — adding the code is a future task.

* **Transcription**: post-finish pipeline calls OpenRouter with
  `full.aac`, writes a `transcript` artifact, mirrors it into
  `artifacts_fts`.
* **Summarisation / extraction**: MiniMax M3 consumes the transcript,
  writes `summary`, `key_points`, `deadlines`, `topics` artifacts.
* **Search**: `search.search()` switches from no-op to a real
  `artifacts_fts` query, the `/api/v1/search` endpoint lights up.
* **Courses / folders**: `courses` table gains a CRUD UI; sessions get
  assigned to a course; the index page groups by course.

## Troubleshooting

* **"Address already in use" on port 8000**: another process is using
  the port. Either stop it or set `LR_PORT=8001` (or whatever).
* **`pip install` complains about externally-managed-environment**:
  you're outside a venv. Run `python -m venv .venv` first and activate
  it.
* **Browser shows old / empty list after uploads**: hard-refresh
  (Ctrl+Shift+R). The UI does not auto-poll yet; that's a future
  enhancement.
* **AAC file in browser "could not be decoded"**: the chunk headers
  were inconsistent. The session will be marked `failed`; check
  `data/sessions/<id>/meta.json` for `status_detail`.

## Git / GitHub

This directory is a self-contained Git repo. To publish it:

```powershell
git remote add origin https://github.com/<you>/lecture-recorder-server.git
git push -u origin main
```

Suggested commit cadence: one commit per meaningful milestone
(scaffold → storage → API → stitching → UI → tests → docs).

## License

Personal project. No license declared.
