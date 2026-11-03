# AI NoteTaker — firmware

ESP32-S3 lecture recorder (ESP32-S3-DevKitC-1 N16R8). Records from an
INMP441 microphone, encodes AAC live, writes 5-minute chunks to the SD card,
and uploads them to the server in `../Server` once on Wi-Fi. Local files are
deleted only after the server confirms.

The firmware is the ESP-IDF project in [`firmware/`](firmware/).

## Versions

Versions are git history, not file copies:

| Tag | What |
|---|---|
| `fw-v0.4` | Arduino. Last version flashed and tested on the device (WAV only) |
| `fw-v0.6-untested` | Arduino V0.5 + V0.6 as first written (WAV → AAC after recording, upload) |
| `fw-v0.7-arduino` | Arduino with all review fixes. Never flashed; superseded by the ESP-IDF firmware |

Sketches from before the repository existed (`V0.1`–`V0.6`) are not
included.

## How it works

Tasks, two cores:

| Task | Core | Job |
|---|---|---|
| capture | 1 (top priority) | I2S mic → gain → 16-bit → ring buffer (~11 s, PSRAM) |
| writer | 1 | ring buffer → Espressif AAC-LC encoder → `rec_…/N.aac` |
| ui | 0 | LED + button (display and battery go here later) |
| wifi_mgr | 0 | saved networks, background reconnect, setup portal |
| sync | 0 | upload finished sessions, then delete them |

- One encoder runs across chunk rotations, so chunks join without gaps.
- The open chunk is flushed every 10 s, so a power cut loses at most ~10 s.
  On the next boot a cut-off last frame is trimmed (`adts.c` checks only
  the last 64 KB of each chunk).
- The ring buffer absorbs SD card stalls.
- While recording, the device logs every 60 s: encoder time per frame (and
  as % of one core), buffer peak in seconds and dropped frames.
- Sync stops at the first "cannot connect" and retries every 5 minutes. If
  the SD card stops answering during an upload, the device remounts it
  when idle (magenta if that fails), and starting a session remounts once
  before giving up.
- The clock is set by NTP as soon as Wi-Fi connects (London time).

Settings: [`firmware/main/config.h`](firmware/main/config.h). The upload
token and server URL override go in `firmware/main/secrets.h` (gitignored;
copy `secrets.example.h`).

## Using the device

| When | Button | LED |
|---|---|---|
| Power-on | hold → Wi-Fi setup portal | blue blink = booting, solid blue = portal |
| Idle | long press → start session | solid green, blue blink = uploading, green flash = uploaded |
| Recording | short = pause, long = end | red blink |
| Paused | short = resume, long = end | orange blink |
| Error | long press → restart | magenta |

A long press counts once the button has been held for 1 s: the LED changes
while it is still down, then let go.

- **Wi-Fi setup:** hold the button while powering on (or boot with no
  saved networks). Join `AI_NoteTaker-Setup` (password `afterword-setup`), and the
  setup page opens. Networks saved by the Arduino firmware carry over.
  The ESP32-S3 only uses **2.4 GHz** networks (for an iPhone hotspot, turn
  on Maximise Compatibility).
- **Offline:** recording never needs Wi-Fi. Without NTP time, sessions are
  named `rec_nt<boot>_<n>`; their real start time is filled in once the
  clock is set, and kept across reboots.

## Build and flash

Toolchain: **ESP-IDF v6.1**, installed with Espressif's EIM installer
(`winget install Espressif.EIM-CLI`) at `C:\esp\v6.1\esp-idf` with tools in
`C:\Espressif\tools`.

**VS Code (ESP-IDF extension):** the extension finds EIM installs by itself.
Run *ESP-IDF: Select Current ESP-IDF Version* → `v6.1`. Open the
`Hardware/firmware` folder, set the target to `esp32s3`, select the COM
port, then *Build, Flash and Monitor* (the flame icon in the status bar).

**Command line (PowerShell):**

```powershell
. C:\Espressif\tools\Microsoft.v6.1.PowerShell_profile.ps1
cd Hardware\firmware
idf.py build
idf.py -p COM5 flash monitor     # your port; Ctrl+] quits the monitor
```

Upgrading ESP-IDF regenerates `sdkconfig`: delete it and `build/`, then
build again. The same goes for a change to `sdkconfig.defaults`, which only
applies when `sdkconfig` is created.

Logs are printed at 115200 baud on both USB ports.

**Host test** (no board needed): the chunk-repair code in `main/adts.c`
builds on a PC with zig's C compiler:

```powershell
cd Hardware\firmware\test
uv run --no-project --with ziglang python -m ziglang cc -O2 -Wall -I ../main test_adts.c ../main/adts.c -o test_adts.exe
.\test_adts.exe [some\chunk.aac ...]
```

## Test plan

Watch the monitor output for each step. [`tools/`](tools/) has a serial
logger that survives power cuts and scripts that check chunk seams,
clipping and noise in recordings.

1. **Boot.** Expect `SD mounted`, `audio ready`, and either `connected,
   IP …` or the setup portal. Hold the button at power-on: the portal should
   open, a network can be added, and the device connects without a reboot.
2. **Record, server off.** Record ~2 minutes with a pause/resume.
   - Check the 60 s `encode avg … us … (% of one core)` lines (this is the
     performance benchmark), plus `dropped frames 0`.
   - Pull the SD card. On the PC, `ffprobe 1.aac` should show
     `aac (LC), 22050 Hz, mono`.
   - Listen: volume (`MIC_GAIN` in `config.h`), and no gap where one chunk
     ends and the next begins. For a quick chunk test, temporarily set
     `CHUNK_SECONDS` to 30.
3. **Power cut.** Record ~1 minute, pull power, power on again. The last
   chunk should hold the audio up to the last 10 s save before the cut
   and decode cleanly. The card only learns the new file size at a save,
   so there is usually nothing to trim; a `trimming` line appears only
   when the cut lands mid-save. After the cold boot, expect
   `clock set by NTP` within a few seconds of `connected`.
4. **Upload.** Start `python app.py` in `Server/`. `SERVER_URL` in
   `secrets.h` must match the PC's IP (`ipconfig`), and the Windows network
   must be Private (firewall). End a session: it should upload, flash
   green, and appear in the web UI. Include a session with full 5-minute
   chunks: long uploads are where the breadboard SD card dropped out.
   With the server off, the log should show one `cannot connect` and
   `unreachable; next try in 5 min`, not one wait per session.
5. **Offline.** Boot away from saved networks: green within ~20 s, and
   recording works. Back in range, it uploads within a minute or so.
6. **Abort.** Long-press during an upload: recording starts at once, and
   the interrupted session uploads after this one ends.
