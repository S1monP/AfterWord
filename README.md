# Lecture Recorder Server — Phase A

A small personal server that receives audio recordings from an ESP32
device over the local network, stitches the chunks into a single WAV
file per session, and serves a minimal web UI to browse and play them.

> **Phase A scope: storage only.** Receive → store → list → play.
> Transcription, summarisation, search, courses, and AI features are
> explicitly out of scope; the data model leaves room for them.

## What works

1. Device announces a session and how many chunks to expect.
2. Device uploads chunks one at a time over
   `PUT /api/v1/sessions/<id>/chunks/<N>`.
3. Device signals the session is finished.
4. Server stitches all chunks into a single `full.wav` and marks the
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
* No database, no Docker, no Node — just Python and Flask.

### Install (Windows)

```powershell
cd "C:\Users\pozig\Documents\Visual Studio\AI_NoteTaker\Server"
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### Install (Linux / macOS)

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

29 tests cover the storage layer (atomic writes, retry safety, finish
gating, stitching, status transitions) and the HTTP API (announce,
upload, finish, range requests, the web UI, the search stub).

End-to-end smoke check (in-process, no network):

```powershell
python e2e_check.py
```

Or against a real running server (starts the dev server in a thread,
runs HTTP requests, shuts down):

```powershell
python e2e_check.py --live
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
│   ├── llm.py             # OpenRouter + MiniMax provider stubs
│   └── processing.py      # post-finish pipeline orchestrator stub
├── templates/
│   ├── base.html
│   ├── index.html         # session list
│   ├── session.html       # one session + player
│   └── _macros.html       # shared Jinja macros
├── static/
│   ├── css/
│   │   ├── tokens.css     # design tokens (colours, spacing, type)
│   │   ├── base.css       # resets, typography, layout
│   │   └── components.css # cards, badges, tables, player
│   └── js/
│       └── app.js         # progressive enhancement (currently empty)
├── tests/
│   ├── conftest.py
│   ├── test_storage.py
│   └── test_api.py
├── e2e_check.py           # manual end-to-end check
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

Body: the raw WAV bytes (PCM, little-endian, as produced by the device).
Re-PUT is safe (atomic write via temp file + rename). Out-of-order
uploads are fine.

```http
PUT /api/v1/sessions/a3f1c0ffeebbeef00/chunks/1 HTTP/1.1
Content-Type: audio/wav

<binary WAV bytes>
```

* `200 OK` with `{session_id, chunk_index, byte_size, received_at}`.
* `400 Bad Request` on empty body.
* `404 Not Found` if the session was never announced.
* `409 Conflict` if the session is not in `receiving` or `failed` state
  (e.g. already `ready`).

### `POST /api/v1/sessions/<id>/finish` — validate and stitch

Empty body. On success the server:

1. Confirms every chunk `1..expected_count` is present.
2. Stitches the chunks into a single `full.wav` (atomic write).
3. Deletes the chunk files (the DB rows stay as provenance).
4. Marks the session `ready` and sets `finished_at`.

```http
POST /api/v1/sessions/a3f1c0ffeebbeef00/finish HTTP/1.1
```

* `200 OK` with `{ok, reason, session}`. `reason` is `stitched` on
  first finish and `already_ready` on re-call.
* `409 Conflict` with `{error: "missing_chunks", missing: [3, 7],
  present: [1,2,4,5,6,8]}` if any chunks are missing.
* `500 Internal Server Error` if stitching itself fails.

### `GET /api/v1/sessions` — list all sessions

JSON list, newest first.

### `GET /api/v1/sessions/<id>` — session detail

JSON: `{session: {...}}` matching the schema below.

### `GET /api/v1/sessions/<id>/audio` — stream the WAV

Streams `full.wav` with `Content-Type: audio/wav` and HTTP Range
support, so the browser's `<audio>` element can seek. Returns
`409 Conflict` if the session is not yet `ready`.

### `GET /api/v1/search?q=...` — search (Phase A stub)

Always returns `{hits: []}`. The endpoint exists so the UI can wire up
a search box without a contract change later; the implementation is a
no-op until the FTS5 index is populated.

## Web UI

* `/` — list of all recordings, newest first. Each row shows title,
  start time, status, chunks received / expected, duration, size.
* `/sessions/<id>` — detail view with audio player and download button.

Status badges:

* **Receiving** (blue): announced, chunks still arriving.
* **Stitching** (amber): all chunks present, server is concatenating.
* **Ready** (green): `full.wav` exists, playable.
* **Failed** (red): something went wrong (missing chunks, stitch
  error). The device can re-send missing chunks and call `/finish`
  again to recover.

## Data model

### Filesystem layout

```
data/
├── db/
│   └── metadata.db          # SQLite (WAL mode)
└── sessions/
    └── <session_id>/
        ├── full.wav         # stitched audio (only after finish)
        ├── meta.json        # debug-friendly mirror of the SQLite row
        └── chunks/          # exists only before stitching
            ├── 001.wav
            ├── 002.wav
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
3. For each chunk file (1.wav, 2.wav, ..., N.wav) on the SD card, in
   any order:
     PUT /api/v1/sessions/<id>/chunks/<N>  with the raw WAV bytes
       - 200 = ok.
       - 409 = session not in receiving state; re-list and decide.
4. POST /api/v1/sessions/<id>/finish
   - 200 + session.status=ready → done.
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
  `full.wav`, writes a `transcript` artifact, mirrors it into
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
* **WAV file in browser "could not be decoded"**: the chunk headers
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
