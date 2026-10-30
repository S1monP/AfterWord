# AI NoteTaker server

A small Flask server for the lecture recorder in [`../Hardware`](../Hardware).
The device uploads each recording in AAC chunks; the server stitches them,
then transcribes, classifies, summarises and extracts tasks in the
background, and serves a web UI for lectures, meetings, notes, tasks and
deadlines, and a calendar.

- **Storage:** SQLite plus one folder per recording under `data/`. Audio is
  AAC-LC, 22050 Hz mono, 64 kbps (ADTS), the same format the device records.
- **AI:** OpenRouter for both models. Transcription:
  `microsoft/mai-transcribe-2` (with speaker labels). Text: `minimax/minimax-m3`.
  Both can be changed in `.env`, and the text model can be called on any
  OpenAI-compatible API instead (`LR_TEXT_BASE_URL`), e.g. with a MiniMax
  subscription.
- **Times:** stored in UTC, shown in local time (`LR_TIMEZONE`, default
  Europe/London).

## Setup

Needs Python 3.11 or newer and ffmpeg.

```powershell
cd Server
setup.cmd                     # Windows: venv, pip install, ffmpeg via winget
```

`setup.sh` does the same on Linux/macOS. By hand:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
winget install Gyan.FFmpeg    # or: apt install ffmpeg / brew install ffmpeg
```

Copy `.env.example` to `.env` and add your OpenRouter key
(`LR_TRANSCRIPTION_API_KEY` and `LR_TEXT_API_KEY`, which can be the same
key). Without keys, recordings are still stored and playable, just not
transcribed.

## Run

```powershell
.venv\Scripts\python.exe app.py      # development (Flask's server)
.venv\Scripts\python.exe serve.py    # production (waitress)
```

Then open `http://localhost:8000`. For the device, `SERVER_URL` in the
firmware's `secrets.h` must point at this machine's LAN address
(`ipconfig`), and on Windows the network must be set to **Private** or the
firewall blocks port 8000.

### On a VPS

Run `serve.py` behind Caddy (HTTPS) with:

```ini
LR_HOST=127.0.0.1
LR_TRUSTED_PROXY=127.0.0.1
LR_COOKIE_SECURE=1
LR_WEB_PASSWORD=...        # login for the web UI
LR_DEVICE_TOKEN=...        # = DEVICE_TOKEN in the firmware's secrets.h
```

Use one process: the AI pipeline worker runs inside it.

## Configuration

All settings are environment variables, read from `Server/.env` too.
Relative paths are taken from the `Server` folder.

| Variable | Default | Purpose |
|---|---|---|
| `LR_DATA_DIR` | `./data` | Database and audio |
| `LR_HOST` / `LR_PORT` | `0.0.0.0` / `8000` | Bind address |
| `LR_TRANSCRIPTION_API_KEY` | — | OpenRouter key for transcription |
| `LR_TEXT_API_KEY` | — | OpenRouter key for the text model |
| `LR_TRANSCRIPTION_MODEL` | `microsoft/mai-transcribe-2` | Speaker labels need `verbose_json` support |
| `LR_TEXT_MODEL` | `minimax/minimax-m3` | Classification, summaries, extraction |
| `LR_TEXT_BASE_URL` | OpenRouter | Text model API, e.g. `https://api.minimax.io/v1` (then `LR_TEXT_MODEL=MiniMax-M3`) |
| `LR_TRANSCRIBE_SPEAKERS` | `1` | `0` = plain transcript without speaker lines |
| `LR_TRANSCRIBE_CHUNK_SECONDS` | `1500` | Long recordings are sent in pieces this long |
| `LR_TRANSCRIBE_PROVIDER_OPTIONS` | Azure diarization on | JSON passed as `provider.options` |
| `LR_PIPELINE_BACKGROUND` | `1` | `0` = run the AI pipeline inside the request (debugging) |
| `LR_FFMPEG_PATH` | `ffmpeg` | ffmpeg binary |
| `LR_MAX_UPLOAD_BYTES` | 500 MB | Largest web upload; bigger requests get 413 |
| `LR_TIMEZONE` | `Europe/London` | Zone for times without an offset, and for display |
| `LR_DEVICE_TOKEN` | off | Device uploads need `Authorization: Bearer <token>` |
| `LR_WEB_PASSWORD` | off | Login for the web UI and the non-device API |
| `LR_SECRET` | generated | Login cookie key (else kept in `data/db/secret_key`) |
| `LR_COOKIE_SECURE` | off | Login cookie over HTTPS only |
| `LR_TRUSTED_PROXY` | off | `serve.py`: trust X-Forwarded-* from this address |

`LR_OPENROUTER_API_KEY` and `LR_MINIMAX_API_KEY` are still read as fallbacks
for the two keys.

## Tests

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests       # 525 tests
.venv\Scripts\python.exe -m unittest tests.test_tasks        # one module
.venv\Scripts\python.exe scripts\run_tests.py                # same, output to test.log
.venv\Scripts\python.exe scripts\e2e_check.py [--live]       # device protocol end to end
.venv\Scripts\python.exe scripts\upload_smoke.py             # web upload end to end
```

Tests that need ffmpeg are skipped without it. None of them call the AI
APIs.

## Layout

```
Server/
├── app.py            create_app(): wiring; python app.py = dev server
├── serve.py          production entry point (waitress)
├── config.py         settings from env / .env
├── auth.py           device token, web login, session key
├── timeutil.py       UTC storage / local display rules
├── storage.py        SQLite + files: sessions, chunks, stitching, artifacts
├── search.py         search (stub: returns nothing yet, see below)
├── services/         processing (AI pipeline), llm (OpenRouter), transcode
│                     (ffmpeg), notes, tasks, events (+ ICS), courses,
│                     classification
├── web/              routes as blueprints
│   ├── pages.py          HTML pages
│   ├── api_sessions.py   device protocol, sessions, audio, pipeline, search
│   ├── api_notes.py, api_tasks.py, api_events.py, api_courses.py
│   ├── common.py         accessors, template filters, helpers
│   └── pipeline.py       background worker and boot sweep
├── templates/, static/
├── tests/
└── scripts/          run_tests.py, e2e_check.py, upload_smoke.py
```

## Device protocol

All under `/api/v1/`. With `LR_DEVICE_TOKEN` set, every call needs
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
transcribe → classify (lecture, meeting, …) → summarise → extract items
(topics, facts, action items; stored with the recording), and matches the
recording to a calendar event by start time.

- Each piece is sent to the transcription API as 16 kHz mono MP3: the
  Azure-hosted model rejects AAC.
- After a server restart, sessions stranded mid-pipeline are requeued;
  finished steps are not repeated.
- A failed session (API error, no speech) stays failed; **Retry** on the
  session page (`POST /api/v1/sessions/<id>/transcribe`) runs it again.

## Data

```
data/
├── db/metadata.db        SQLite (WAL)
├── db/secret_key         login cookie key, when LR_SECRET is unset
└── sessions/<id>/
    ├── full.aac          stitched audio
    ├── meta.json         copy of the database row, for debugging
    └── chunks/           only until stitched
```

Deleting `data/` resets the server.

## Troubleshooting

- **The device cannot connect:** check `SERVER_URL`, the Windows network
  profile (Private), and that the server listens on `0.0.0.0`.
- **`Provider returned 400` on transcription:** the model rejected the
  audio or an option. MP3 is accepted by the default model; check
  `LR_TRANSCRIPTION_PROVIDER_OPTIONS` when changing models.
- **`404 ... unavailable for free`:** the text model's free tier was
  withdrawn; set `LR_TEXT_MODEL`.
- **Port 8000 in use:** set `LR_PORT`.
- **Search finds nothing:** not built yet. Artifacts are indexed into the
  `artifacts_fts` table as they are written, but `search.py` is a stub.
