# Hardware test tools

Helpers used while working through the test plan in
[`../README.md`](../README.md). Run them from any folder.

| Tool | What it does | Needs |
|---|---|---|
| `serial_log.py` | Saves the serial log to a file, unattended. Reconnects through resets and power cuts | pyserial |
| `check_join.py` | Checks the seam between two chunks for a gap or click, joined the way the server joins them | ffmpeg |
| `audio_report.py` | Loudness per minute, where clipping happens, treble, noise floor | ffmpeg, numpy |

## Serial log

ESP-IDF's Python already has pyserial:

```powershell
C:\Espressif\tools\python\v6.1\venv\Scripts\python.exe serial_log.py COM3 300 boot.log --reset
```

`--reset` restarts the chip so the log starts at the bootloader. Leave it
out to listen to a device that is already running. Close `idf.py monitor`
first: only one program can hold the port.

## Audio

Copy a session folder off the SD card (power the device off before pulling
the card), then:

```powershell
python check_join.py rec_2026-10-30T0111\1.aac rec_2026-10-30T0111\2.aac
uv run --no-project --with numpy python audio_report.py rec_2026-10-30T0111\1.aac rec_2026-10-30T0111\2.aac
```

`audio_report.py` gives clipping times as mm:ss into the recording. To match
them to button presses, subtract the `session start` time in the device log
from each `app: pause` / `resume` / `session end` time.
