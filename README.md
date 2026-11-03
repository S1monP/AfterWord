# AI NoteTaker

A battery-powered lecture recorder and the server behind it.

The **device** (ESP32-S3) records from a MEMS microphone, encodes AAC live
into 5-minute chunks on a microSD card, and uploads them over Wi-Fi when it
can. It deletes a recording only after the server confirms it.

The **server** (Python/Flask) stitches the chunks, transcribes and
summarises them through OpenRouter, and serves a web UI for lectures,
meetings, notes, tasks, deadlines and a calendar.

```
Hardware/            the recorder
├── firmware/        ESP-IDF v6.1 project (C)
├── tools/           serial logger, chunk-seam and audio checks
└── README.md        wiring, build/flash, using the device, test plan
server/              the server
└── README.md        setup, configuration, API, deployment
```

## Quick start

**Server** (Windows):

```powershell
cd server
setup.cmd                          # venv, dependencies, ffmpeg
copy .env.example .env             # then add your OpenRouter key
.venv\Scripts\python.exe app.py    # http://localhost:8000
```

**Firmware:** install ESP-IDF v6.1 (Espressif's EIM installer), create
`Hardware/firmware/main/secrets.h` from `secrets.example.h` with this PC's
address, then:

```powershell
. C:\Espressif\tools\Microsoft.v6.1.PowerShell_profile.ps1
cd Hardware\firmware
idf.py -p COM3 flash monitor
```

Details: [Hardware/README.md](Hardware/README.md) and
[server/README.md](server/README.md).

## Status

- Firmware: the breadboard prototype passed the whole test plan in
  [Hardware/README.md](Hardware/README.md) (boot, Wi-Fi setup, recording,
  power cut, upload, offline, abort). The SD card dropped out during long
  uploads until decoupling capacitors were added at the SD module and mic.
- Server: runs locally; ready for a VPS behind Caddy (`serve.py`, device
  token, web login).

## Versions

Firmware versions are git tags (`fw-v0.4`, `fw-v0.6-untested`,
`fw-v0.7-arduino`), not file copies. The Arduino sketch was replaced by the
ESP-IDF firmware after `fw-v0.7-arduino`.
