# AI NoteTaker — firmware

ESP32-S3 lecture recorder. Records 5-minute WAV chunks to the SD card,
encodes them to AAC when a session ends, and uploads them to the server
in `../Server` once on Wi-Fi.

The sketch is the folder [`AI_NoteTaker/`](AI_NoteTaker/). Open
`AI_NoteTaker/AI_NoteTaker.ino` in the Arduino IDE. Arduino builds every
`.ino` in that folder, so don't put other sketches there.

## Versions

Versions are git history, not file copies:

| Tag | What |
|---|---|
| `fw-v0.4` | Last version flashed and tested on the device |
| `fw-v0.6-untested` | V0.5 + V0.6 exactly as first written (never flashed) |

`git log -- Hardware` shows every change since. `old_versions/` holds the
pre-git `V0.x.ino` files locally and is gitignored.

## Build

Arduino IDE 2 or `arduino-cli`, ESP32 core 3.3.x.

Board: **ESP32S3 Dev Module** with:

- Flash Size: 16MB
- PSRAM: OPI PSRAM
- Partition Scheme: 16M Flash (3MB APP/9.9MB FATFS)

Libraries:

- Adafruit NeoPixel (Library Manager)
- VOAACEncoder: `git clone https://github.com/pschatzmann/codec-vo-aacenc`
  into `Documents/Arduino/libraries/`

Command line:

```
arduino-cli compile -b "esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB" Hardware/AI_NoteTaker
```

## Configuration

- [`config.h`](AI_NoteTaker/config.h): server address and port. The
  default is the dev PC on the LAN (`192.168.0.50:8000`).
- `secrets.h` (gitignored): copy
  [`secrets.example.h`](AI_NoteTaker/secrets.example.h) to set the upload
  token or override the server address. Optional for local testing.
- Wi-Fi networks are entered in the setup portal, not in code. Hold the
  button while powering on, join `AI_NoteTaker-Setup` (password in the
  sketch, `AP_PASSWORD`), and open any page.

## Test plan (V0.7 on hardware)

Test in this order. Each step only depends on the ones before it.

1. **Boot + portal.** Flash, open Serial Monitor at 115200. Hold the
   button at power-on, add your Wi-Fi, check it reboots and connects.
2. **Record + encode, no server.** Unplug or stop the server. Record a
   short session with one pause/resume. After the long press, check the log
   for `[ENC ] OK` per chunk. Pull the SD card and on the PC run
   `ffprobe 1.aac` (expect `aac (LC), 22050 Hz, mono`), then listen,
   especially to the volume and the end of each chunk.
3. **Crash recovery.** Start recording, pull power after ~1 minute, power
   back on. The boot sweep should encode that chunk (not refuse it).
4. **Upload to the local server.** Start `python app.py` in `Server/`,
   check `config.h` matches the PC's IP (`ipconfig`). Record a session;
   after it ends it should upload, flash green, and appear in the web UI.
5. **Offline.** Boot away from any saved network: it should reach solid
   green and record. Back in range, it should upload within a few minutes.
6. **Abort.** Long-press while it's uploading: blue blinking stops, files
   are kept, and the next retry uploads them.
