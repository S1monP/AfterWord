// =============================================================================
//  AI_NoteTaker_V0.5  -  MVP Firmware for ESP32-S3-DevKitC (N16R8)
//
//  Board  : ESP32-S3-DevKitC with N16R8 module (16MB flash, 8MB octal PSRAM)
//  Mic    : INMP441 (I2S, mono, 32-bit slots, 22.05 kHz)
//  Storage: microSD over SPI (custom pins)
//  LED    : 1x WS2812 NeoPixel on GPIO 48
//  Button : GPIO 1, internal pull-up, active LOW
//
//  WiFi provisioning (V0.4 -- custom captive portal, no WiFiManager):
//    - Up to 5 SSID/password pairs stored in NVS, LIFO order (most recent first)
//    - On boot, try each saved network in order; on first success, continue
//    - If all saved networks fail, start a WPA2 AP ("AI_NoteTaker-Setup")
//      with a 3-page captive portal:
//        /         -- landing page with "Add" and "Manage" buttons
//        /add      -- scan + dropdown + manual SSID + password + save
//        /manage   -- list of saved networks with per-row delete
//
//  Behaviour:
//    - Boot         -> blue slow blink while trying saved networks
//    - AP mode      -> SOLID blue (waiting for user to configure WiFi)
//    - WiFi up      -> fast green blink for 2 s, then solid green (IDLE)
//    - IDLE         -> solid green (waiting for session start)
//    - long press   -> start a new session: create folder /rec_YYYY-MM-DDTHHMM/
//                       open 1.wav, begin RECORDING
//    - RECORDING    -> red 250ms on / 750ms off; chunk rotates every 5 minutes
//    - short press  -> PAUSE: close current chunk, orange slow blink
//    - short press  -> RESUME: open next chunk, back to RECORDING
//    - long press   -> END session: close chunk, RUN TRANSCODING, then IDLE
//                       (V0.5: WAV chunks are transcoded to AAC on-device
//                        before the device returns to IDLE)
//    - TRANSCODING  -> cyan fast blink (NEW in V0.5); non-fatal, returns to IDLE
//    - Any fault    -> ERROR: solid magenta, long press to recover to IDLE
//
//  Button gestures:
//    - short press : button released within 1 s of being pressed
//    - long press  : button held >= 1 s, then released
//    - pre-IDLE presses are ignored
//
//  V0.5 changelog (vs V0.4):
//    - NEW: aac_encoder.h/.cpp module exposing aacEncodeFile(wavPath, aacPath).
//           Encodes 22050 Hz mono 16-bit PCM (WAV) -> AAC-LC 64 kbps CBR with
//           a 7-byte ADTS header per frame, exactly matching the server's
//           canonical format and its binary-concat stitcher.
//    - NEW: STATE_TRANSCODING with cyan fast-blink LED.
//    - NEW: endSession() now triggers a transcoding pass on the just-finished
//           session folder before returning to IDLE.
//    - NEW: boot sweep -- after SD/WiFi/NTP/I2S init, scan /rec_* and
//           transcode any leftover .wav with no matching .aac (crash recovery).
//    - NEW: per-chunk safety rule -- encode to <name>.aac.tmp, verify it
//           (size > 0 and starts 0xFF Fx for ADTS sync), rename to <name>.aac,
//           THEN delete <name>.wav.  A WAV is never removed without a verified
//           AAC replacement.
//    - NEW: failed chunks are retried, then left as WAV for next boot's
//           sweep.  Transcode failures never push the device into STATE_ERROR.
//    - CHANGED: initSdCard() now tries 20 MHz SPI clock, falls back to 4 MHz
//           if that fails (1 MHz was the bottleneck in V0.4: a 26 MB WAV
//           read took ~3.5 min).
//    - See aac_encoder.h for encoder integration notes (vo-aacenc).
//
//  V0.6 changelog (vs V0.5):
//    - NEW: sync_client.h/.cpp -- pushes finished sessions to the server:
//             announce -> PUT chunks (streamed) -> finish -> delete on confirm.
//    - NEW: STATE_SYNCING (blue LED) plus a green flash per synced session.
//    - NEW: runSyncSweep() scans /rec_* for folders holding .aac chunks and
//           uploads them.  Triggers: after boot, after each endSession()
//           transcode, and every SYNC_RETRY_INTERVAL_MS while IDLE.
//    - NEW: local files are deleted ONLY after the server replies 200
//           stitched / already_ready.  Any other outcome keeps the files
//           and the periodic retry is the recovery.
//    - NEW: long-press during SYNCING aborts the attempt cleanly; partial
//           uploads are safe because re-PUT is idempotent server-side.
//    - Sync never runs during RECORDING or PAUSED.
// =============================================================================

#include <Arduino.h>
#include <WiFi.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <Preferences.h>
#include <time.h>
#include <SPI.h>
#include <SD.h>
#include <Adafruit_NeoPixel.h>
#include "ESP_I2S.h"
#include "config.h"
#include "aac_encoder.h"
#include "sync_client.h"

// -----------------------------------------------------------------------------
//  USER CONFIGURATION  --  AP credentials and firmware tuning
// -----------------------------------------------------------------------------
// In V0.3 WiFi credentials are entered at runtime via the captive portal and
// stored in NVS -- no need to hardcode them here anymore.

// -----------------------------------------------------------------------------
//  PIN DEFINITIONS  (locked per hardware spec)
// -----------------------------------------------------------------------------
#define PIN_BUTTON      1
#define PIN_MIC_SCK     38
#define PIN_MIC_WS      39
#define PIN_MIC_SD      40
#define PIN_SD_CS       41
#define PIN_SD_MOSI     42
#define PIN_SD_MISO     47
#define PIN_SD_SCK      21
#define PIN_NEOPIXEL    48

// -----------------------------------------------------------------------------
//  AUDIO / I2S
// -----------------------------------------------------------------------------
#define SAMPLE_RATE         22050
#define BITS_PER_SAMPLE     32
#define CHANNELS            1
#define DMA_BUF_COUNT       8
#define DMA_BUF_LEN         1024

// Software gain applied to every sample before it is written to the WAV
// file.  The INMP441 at normal speaking distance produces samples in the
// low-thousands range out of a possible +/-2^31, which plays back as a
// very quiet recording.  Multiplying by MIC_GAIN boosts the level.  The
// apply-gain loop uses int64_t intermediate values with saturation
// clipping at int32_t limits, so loud transients will clip cleanly
// instead of wrapping around.
//
// Tune by ear:  4 = subtle boost,  8 = comfortable speech,
//              16 = loud speech / quiet speaker,  24 = quiet speaker.
#define MIC_GAIN            8

// Each chunk file holds this much audio before the recorder rotates to
// the next chunk in the same session folder.  Default 5 minutes; lower
// for testing if you want to see rotation behaviour quickly.
#define CHUNK_DURATION_MS   (5UL * 60 * 1000)

// Auto-stop a session after this much *continuous* recording time.  Pauses
// reset the timer, so this is a safety net for "I forgot to turn the
// recorder off" rather than a hard cap on session length.  Default 4 hours.
// Set to 0 to disable the auto-stop entirely.
#define MAX_RECORDING_MS    (4UL * 60 * 60 * 1000)

// -----------------------------------------------------------------------------
//  WI-FI PROVISIONING (V0.3)
// -----------------------------------------------------------------------------
// Maximum number of saved WiFi credentials.  When the list is full and a new
// one is added, the most recently added entry is evicted (LIFO) so the
// longest-standing network (typically home Wi-Fi) is preserved.
#define MAX_SAVED_NETWORKS   5

// Access point credentials used when the device enters config-portal mode.
// The AP is WPA2-protected so only someone with the password can configure
// the device.
#define AP_SSID              "AI_NoteTaker-Setup"
#define AP_PASSWORD          "afterword-setup"

// How long the config portal waits for the user to connect and configure
// before giving up and going to ERROR state (in seconds).
#define AP_PORTAL_TIMEOUT_S  300

// After boot completes (WiFi + NTP + I2S all up), blink green fast for this
// long to give a clear visual "all systems go" signal before settling on
// solid green (IDLE).
#define BOOT_DONE_BLINK_MS   2000

// -----------------------------------------------------------------------------
//  BUTTON TIMING (ms)
// -----------------------------------------------------------------------------
#define BUTTON_DEBOUNCE_MS    200
#define BUTTON_SHORT_MIN_MS    50
#define BUTTON_SHORT_MAX_MS   1000   // releases within 1 s are short; after that, long
#define LONG_PRESS_MS         1000

// -----------------------------------------------------------------------------
//  WIFI / NTP TIMING (ms)
// -----------------------------------------------------------------------------
#define WIFI_TIMEOUT_MS     30000
#define NTP_SYNC_TIMEOUT_MS 20000   // safety cap while we wait for sane time
// -----------------------------------------------------------------------------
//  LED TIMING (ms)
// -----------------------------------------------------------------------------
#define BLUE_SLOW_MS        1000    // 1s on / 1s off
#define RED_FAST_MS          100    // 100ms / 100ms
#define RECORDING_LED_ON_MS    250   // red LED on for 250 ms each cycle
#define RECORDING_LED_PERIOD_MS 1000  // full cycle is 1 s -- off-phase is 750 ms
#define NEOPIXEL_BRIGHTNESS   40    // 0-255

// V0.5 -- new cyan LED used only for STATE_TRANSCODING.
// (orange/green/red/blue/magenta are all taken by other states.)
#define LED_CYAN     0x00FFFFu   // G+B at full, R=0

// V0.5 -- transcoding LED blink period.  Faster than PAUSED (1 s) so it is
// visually distinct: a clear "the device is busy" signal.
#define TRANSCODING_LED_PERIOD_MS  200   // 100 ms on / 100 ms off

// V0.5 -- SD SPI clock.  V0.4 used 1 MHz, which made a 26 MB WAV read take
// ~3.5 minutes.  20 MHz is the spec target; we fall back to 4 MHz if that
// fails so flaky cards don't deadlock boot.
#define SD_SPI_HI_HZ   20000000   // 20 MHz primary attempt
#define SD_SPI_FB_HZ    4000000   //  4 MHz fallback

// -----------------------------------------------------------------------------
//  TRANSCODING SAFETY LIMITS  (V0.5)
// -----------------------------------------------------------------------------
// How many times a single chunk may fail to encode before we leave it as WAV
// and move on.  The boot sweep will retry it next boot.
#define AAC_MAX_ATTEMPTS    2

// Size of the plain RIFF/WAVE header writeWavHeader() emits.
#define WAV_HEADER_BYTES    44

// -----------------------------------------------------------------------------
//  SYNC CONFIG  (V0.6)  --  server address / token live in config.h
// -----------------------------------------------------------------------------
// How often the IDLE state retries a failed sync.  The periodic retry --
// not the user -- is the recovery path, so this can be generous.
#define SYNC_RETRY_INTERVAL_MS      (5UL * 60 * 1000)

// Maximum chunk index we will look for in one session folder.  Matches the
// server's own expected_count ceiling in practice (5 min chunks -> ~48 per
// 4 h session).
#define SYNC_MAX_CHUNKS             64

// V0.6 -- STATE_SYNCING uses BLUE, which is otherwise only used during
// BOOTING (blue slow blink = trying networks) and AP setup (solid blue).
// During sync the device is always IDLE-or-busy, never booting, so there is
// no ambiguity.  Green flash marks a per-session success.
#define LED_BLUE_SYNC               0x0000FFu   // same value as LED_BLUE
#define SYNC_LED_PERIOD_MS          1000        // 1 s on / 1 s off
#define SYNC_OK_FLASH_MS            600         // green confirmation per session

// -----------------------------------------------------------------------------
//  LED COLOUR HELPERS  --  standard 0xRRGGBB packing.  Adafruit_NeoPixel
//  setPixelColor(n, uint32_t c) always interprets the argument as RGB; the
//  NEO_GRB flag passed to the constructor only changes the byte order sent
//  on the wire, not the meaning of this uint32_t.
// -----------------------------------------------------------------------------
#define LED_BLUE     0x0000FFu
#define LED_GREEN    0x00FF00u
#define LED_RED      0xFF0000u
#define LED_MAGENTA  0xFF00FFu
// -----------------------------------------------------------------------------
//  STATE MACHINE
// -----------------------------------------------------------------------------
enum State : uint8_t {
  STATE_BOOTING,           // transient: blue slow blink until SD + WiFi + NTP + mic
  STATE_IDLE,              // solid green; waiting for session start
  STATE_RECORDING,         // red 500ms flash; chunk N open, writing samples
  STATE_PAUSED,            // orange 1s/1s slow blink; chunk N closed, mic drained
  STATE_TRANSCODING,       // V0.5 NEW: cyan 100ms/100ms fast blink; encoding WAVs to AAC
  STATE_SYNCING,           // V0.6 NEW: blue 1s/1s slow blink; uploading .aac to server
  STATE_ERROR              // solid magenta; long press to recover to IDLE
};

State currentState   = STATE_BOOTING;
unsigned long lastLedToggleMs = 0;
bool          ledPhaseOn      = false;

// -----------------------------------------------------------------------------
//  BOOT PHASE  (V0.3, extended in V0.5)
// -----------------------------------------------------------------------------
// Tracks what the boot sequence is doing, so updateLeds() can show the right
// LED pattern (blue slow blink = trying networks, solid blue = AP mode,
// fast green blink = almost ready).  Once we reach STATE_IDLE this is
// ignored unless bootPhase == BOOT_PHASE_SWEEP, which makes the LED
// indicate the post-init transcoding sweep.
enum BootPhase : uint8_t {
  BOOT_PHASE_TRYING,       // attempting saved networks one by one
  BOOT_PHASE_AP_MODE,      // captive portal AP up, waiting for user
  BOOT_PHASE_DONE_BLINK,   // WiFi+NTP+I2S all up, blinking green fast
  BOOT_PHASE_SWEEP         // V0.5 NEW: post-init transcoding sweep running
};

BootPhase      bootPhase           = BOOT_PHASE_TRYING;
unsigned long  bootDoneBlinkUntilMs = 0;  // millis() until which we keep fast-blinking green

// -----------------------------------------------------------------------------
//  SAVED WIFI NETWORKS  (V0.3)
// -----------------------------------------------------------------------------
// Up to MAX_SAVED_NETWORKS SSID/password pairs in NVS, ordered most-recent
// first.  On boot, try savedNetworks[0] first; if it fails, try [1], etc.
// When the list is full and a new network is added, the entry at index
// MAX_SAVED_NETWORKS-1 (the most recently added of the older ones) is
// dropped -- i.e. LIFO eviction preserves the longest-standing network.
struct SavedNetwork {
  char ssid[33];
  char pass[64];
};

static SavedNetwork savedNetworks[MAX_SAVED_NETWORKS];
static int          savedNetworkCount = 0;  // 0..MAX_SAVED_NETWORKS

// Captive portal (V0.4 -- custom, no WiFiManager).
// DNSServer redirects every hostname lookup to our AP IP, which makes the
// phone auto-open our web server for any URL the user types.  WebServer is
// the actual HTTP server hosting the three portal pages.
static DNSServer  dnsServer;
static WebServer  webServer(80);

// -----------------------------------------------------------------------------
//  BUTTON STATE
// -----------------------------------------------------------------------------
bool          btnRawStable       = true;   // last debounced reading (true = released)
bool          btnRawLast         = true;   // raw reading from previous sample
unsigned long btnLastChangeMs    = 0;      // for debounce
unsigned long btnPressStartMs    = 0;      // when current press began
bool          btnShortPressEvent = false;  // one-shot: button released within 1 s
bool          btnLongPressEvent  = false;  // one-shot: button held >= 1 s, then released
#define LED_ORANGE   0xFF8000u   // G=255, R=128, B=0  (warm amber, used for PAUSED)

#define NUM_PIXELS   1
// -----------------------------------------------------------------------------
//  RECORDING STATE
// -----------------------------------------------------------------------------
File     recordingFile;
uint32_t dataBytesWritten = 0;

// V0.2 session and chunk tracking.  A "session" is one continuous
// recording run from long-press (start) to long-press (end), with
// optional pause/resume cycles.  Each session lives in its own folder
// on the SD card and contains one or more chunk files (1.wav, 2.wav, ...).
// A "chunk" is one CHUNK_DURATION_MS slice of audio; the recorder rotates
// to the next chunk file when the current one fills up.
char          sessionFolder[32];         // e.g. "/rec_2026-09-30T1430"
uint16_t      currentChunk   = 0;        // chunk number in current session (1..N)
unsigned long chunkStartMs   = 0;        // when current chunk file was opened
unsigned long sessionStartMs = 0;        // when current session began
unsigned long continuousRecordingStartMs = 0;  // millis() when current RECORDING segment began; reset on pause
// Raw 32-bit I2S sample buffer for the recording loop.  Defined at module
// scope (not on the stack) so the 4 KB it consumes does not blow the 8 KB
// Arduino task stack -- a previous version put int32_t[1024] on the stack
// of loop() and caused a watchdog reset on the first button press.
static int32_t i2sSamples[DMA_BUF_LEN];
// dataBytesWritten is uint32_t; at 32 KB/s (16 kHz x 2 B) it overflows after
// ~149 hours.  Plenty for MVP; revisit if long unattended sessions are needed.

// V0.5 -- the SD SPI clock that worked during init, remembered for logging
// and for any future re-init.  0 = unknown / not yet attempted.
static uint32_t sdSpiHzActive = 0;

// -----------------------------------------------------------------------------
//  SYNC STATE  (V0.6)
// -----------------------------------------------------------------------------
// millis() of the last completed sync sweep.  The IDLE branch compares this
// against SYNC_RETRY_INTERVAL_MS to fire the periodic retry.  0 = never run.
unsigned long lastSyncMs = 0;

// Set for SYNC_OK_FLASH_MS after a session uploads and its files are deleted,
// so the LED shows a green confirmation before falling back to IDLE solid.
unsigned long syncOkFlashUntilMs = 0;
bool          syncOkFlashActive  = false;

// -----------------------------------------------------------------------------
//  FORWARD DECLARATIONS
// -----------------------------------------------------------------------------
void   setState(State s);
void   updateLeds();
void   setLedColor(uint32_t color, bool on);

bool   initSdCard();
bool   connectOrProvisionWifi();
void   syncTimeBlocking();
bool   initI2sMic();

void   writeWavHeader(File &f);
void   updateWavSizes(File &f, uint32_t dataSize);

bool   readButtonDebounced();
bool   pollShortPressEvent();
bool   pollLongPressEvent();

bool   openSession();
bool   openChunk(uint16_t n);
void   closeChunk();
void   pauseRecording();
void   resumeRecording();
void   endSession();

// V0.5 -- transcoding pipeline declarations.
void   runTranscodingSweep(const char *onlyFolder);
void   transcodeSessionFolder(const char *folderPath);
bool   transcodeOneChunk(const char *wavPath, const char *aacPath);

// V0.6 -- sync pipeline declarations.
void   runSyncSweep(const char *onlyFolder);
bool   syncOneSession(const char *folderPath);

Adafruit_NeoPixel pixel(NUM_PIXELS, PIN_NEOPIXEL, NEO_GRB + NEO_KHZ800);

// I2S mic instance (used by initI2sMic and the recording loop).
static I2SClass i2sMic;

// =============================================================================
//  SETUP
// =============================================================================
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("[BOOT] AI_NoteTaker " FW_VERSION " starting...");
  Serial.printf("[CFG ] server=http://%s:%d  token=%s\n",
                SERVER_HOST, SERVER_PORT, DEVICE_TOKEN[0] ? "set" : "none");
  Serial.printf("[CHIP] model=%s rev=%d  IDF=%s\n",
                ESP.getChipModel(),
                (int)ESP.getChipRevision(),
                ESP.getSdkVersion());
  Serial.printf("[CFG ] SAMPLE_RATE=%u  BITS=%u  CHANNELS=%u  GAIN=%u  CHUNK_SEC=%lu\n",
                (unsigned)SAMPLE_RATE,
                (unsigned)BITS_PER_SAMPLE,
                (unsigned)CHANNELS,
                (unsigned)MIC_GAIN,
                (unsigned long)(CHUNK_DURATION_MS / 1000));
  Serial.printf("[CFG ] AAC target=%u Hz, %lu bps CBR, %u ch, ADTS frames\n",
                (unsigned)AAC_SAMPLE_RATE,
                (unsigned long)AAC_BITRATE_BPS,
                (unsigned)AAC_CHANNELS);

  // ---- NeoPixel ----
  pixel.begin();
  pixel.setBrightness(NEOPIXEL_BRIGHTNESS);
  pixel.clear();
  pixel.show();

  // ---- Button ----
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  btnRawStable    = (digitalRead(PIN_BUTTON) == HIGH);
  btnRawLast      = btnRawStable;
  btnLastChangeMs = millis();
  // If the button is already being held at boot, seed the press-start time
  // so that a release during the first ~2 seconds does not fire a phantom
  // short- or long-press event with a bogus duration.
  btnPressStartMs  = btnRawStable ? 0 : millis();

  // ---- Initial state: blue slow blink (BOOTING) ----
  setState(STATE_BOOTING);

  // ---- SD card ----
  if (!initSdCard()) {
    Serial.println("[ERR ] SD card init failed");
    setState(STATE_ERROR);
    return;
  }
  Serial.println("[OK  ] SD card ready");

  // ---- WiFi (try saved, fall back to AP/captive portal) ----
  if (!connectOrProvisionWifi()) {
    Serial.println("[ERR ] WiFi config portal timed out");
    setState(STATE_ERROR);
    return;
  }
  Serial.printf("[OK  ] WiFi connected, IP=%s\n", WiFi.localIP().toString().c_str());

  // WiFiManager's autoConnect() can leave the radio in AP+STA mode, which
  // keeps broadcasting the chip's default fallback AP ("ESP_XXXXXX") even
  // though we no longer want it.  Force STA-only mode to shut it down.
  WiFi.mode(WIFI_STA);
  WiFi.softAPdisconnect(true);

  bootPhase = BOOT_PHASE_DONE_BLINK;
  bootDoneBlinkUntilMs = millis() + BOOT_DONE_BLINK_MS;

  // ---- NTP / timezone ----
  Serial.println("[....] Syncing NTP time...");
  syncTimeBlocking();
  Serial.println("[OK  ] Time synchronised");

  // ---- I2S mic ----
  if (!initI2sMic()) {
    Serial.println("[ERR ] I2S mic init failed");
    setState(STATE_ERROR);
    return;
  }
  Serial.println("[OK  ] I2S mic ready");

  // ---- V0.5: boot sweep -- transcode any leftover WAVs from a prior
  // crashed/reset mid-encode (or from any power-loss during a session).
  // This runs only after WiFi+NTP+mic are healthy, so it doesn't delay
  // critical paths.  Failures here never push us into STATE_ERROR; the
  // next boot retries them.
  bootPhase = BOOT_PHASE_SWEEP;
  Serial.println("[....] Post-boot transcoding sweep...");
  updateLeds();
  runTranscodingSweep(nullptr);   // nullptr => scan every /rec_* folder
  Serial.println("[OK  ] Post-boot transcoding sweep done");

  // ---- V0.6: push anything already encoded to the server.  Runs after
  // the transcode sweep so freshly-encoded leftovers from the same boot
  // are uploaded in one pass.  Non-fatal: on failure we still reach IDLE
  // and the periodic retry picks it up.
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("[....] Post-boot sync sweep...");
    runSyncSweep(nullptr);        // nullptr => every /rec_* folder
    Serial.println("[OK  ] Post-boot sync sweep done");
  } else {
    Serial.println("[SYNC] skipped post-boot sync (no WiFi)");
  }
  lastSyncMs = millis();

  // ---- IDLE: ready for the first long press to start a session ----
  setState(STATE_IDLE);
  Serial.println("[OK  ] System READY (IDLE)");
}


// =============================================================================
//  LOOP
// =============================================================================
void loop() {
  updateLeds();

  // Long-press is a global "recover / finish" gesture.  Handled before
  // the state switch so it works from IDLE (start session), RECORDING /
  // PAUSED (end session), and ERROR (recover to IDLE).
  if (pollLongPressEvent()) {
    switch (currentState) {
      case STATE_IDLE:
        Serial.println("[EVT ] Long press -> start session");
        if (!openSession()) {
          Serial.println("[ERR ] Could not start session");
          setState(STATE_ERROR);
        }
        break;

      case STATE_RECORDING:
      case STATE_PAUSED:
        Serial.println("[EVT ] Long press -> end session");
        endSession();
        break;

      case STATE_ERROR:
        Serial.println("[EVT ] Long press -> recover to IDLE");
        setState(STATE_IDLE);
        break;

      // V0.6 -- long-press during SYNCING aborts the in-flight attempt.
      // Files are untouched; the periodic retry picks it up later.  This is
      // safe even mid-PUT because re-PUT is idempotent server-side.
      case STATE_SYNCING:
        Serial.println("[EVT ] Long press -> abort sync");
        syncAbort();
        break;

      // STATE_TRANSCODING ignores long-press; let the pipeline finish so a
      // partially-renamed chunk doesn't leave us with a stranded .tmp file.
      case STATE_TRANSCODING:
      case STATE_BOOTING:
      default:
        break;
    }
  }

  // Per-state logic: only one active at a time.
  switch (currentState) {

    case STATE_IDLE: {
      // V0.6 -- periodic retry.  This is the whole recovery story for a
      // failed sync: we do not ask the user, we just come back later.  Only
      // runs when there is actually something to send and WiFi is up; the
      // first check is cheap (root dir listing) but gating on WiFi first
      // avoids pointless socket setup on a dead link.
      if (WiFi.status() == WL_CONNECTED &&
          (uint32_t)(millis() - lastSyncMs) >= SYNC_RETRY_INTERVAL_MS) {
        Serial.println("[SYNC] periodic retry starting");
        runSyncSweep(nullptr);
        lastSyncMs = millis();
      }
      break;
    }

    case STATE_RECORDING: {
      // Short press pauses (closes current chunk, switches to PAUSED).
      if (pollShortPressEvent()) {
        Serial.println("[EVT ] Short press -> pause");
        pauseRecording();
        break;
      }

      // Auto-stop safety net: if we've been in RECORDING continuously for
      // MAX_RECORDING_MS, end the session cleanly.  Pauses reset this
      // timer, so this only catches the "I forgot to turn it off" case.
      if (MAX_RECORDING_MS > 0 &&
          (uint32_t)(millis() - continuousRecordingStartMs) >= MAX_RECORDING_MS) {
        Serial.println("[AUTO] Max continuous recording time reached, ending session");
        endSession();
        break;
      }

      // Chunk rotation: if the current chunk has been recording for
      // CHUNK_DURATION_MS, close it and open the next one.
      if (millis() - chunkStartMs >= CHUNK_DURATION_MS) {
        Serial.printf("[ROT ] Chunk %u elapsed %lu s - rotating\n",
                      (unsigned)currentChunk,
                      (unsigned long)(CHUNK_DURATION_MS / 1000));
        closeChunk();
        currentChunk++;
        if (!openChunk(currentChunk)) {
          Serial.println("[ERR ] Could not open next chunk");
          setState(STATE_ERROR);
          break;
        }
      }

      // Pull a buffer of raw 32-bit samples from I2S, apply software gain
      // with saturation clipping, then write to the file.
      size_t bytes_read = i2sMic.readBytes((char*)i2sSamples, sizeof(i2sSamples));
      if (bytes_read == 0) {
        // No data this cycle -- wait a tick and try again.
        break;
      }

      // Apply gain in-place.  int64_t intermediate prevents overflow during
      // the multiply; saturation clipping prevents wrap-around buzz on loud
      // transients.
      {
        const size_t num_samples = bytes_read / sizeof(int32_t);
        for (size_t i = 0; i < num_samples; i++) {
          int64_t v = (int64_t)i2sSamples[i] * MIC_GAIN;
          if (v >  2147483647LL) v =  2147483647LL;
          if (v < -2147483648LL) v = -2147483648LL;
          i2sSamples[i] = (int32_t)v;
        }
      }

      size_t written = recordingFile.write((const uint8_t*)i2sSamples, bytes_read);
      if (written != bytes_read) {
        Serial.println("[ERR ] File write short/failed");
        closeChunk();
        setState(STATE_ERROR);
        break;
      }
      dataBytesWritten += written;
      break;
    }

    case STATE_PAUSED: {
      // Keep the I2S DMA flowing by draining samples into a throwaway
      // buffer.  This avoids stale samples being written to the next chunk
      // when the user resumes.
      size_t bytes_read = i2sMic.readBytes((char*)i2sSamples, sizeof(i2sSamples));
      (void)bytes_read;  // discarded

      // Short press resumes (open the next chunk, back to RECORDING).
      if (pollShortPressEvent()) {
        Serial.println("[EVT ] Short press -> resume");
        resumeRecording();
      }
      break;
    }

    // V0.5 -- transcoding runs synchronously and blocks loop(); AAC-LC
    // is cheap (~10-20% on S3@240 MHz per Espressif) and the LED blinks
    // while it runs.  This case is just a no-op; the actual work happens
    // at the moment we enter TRANSCODING from endSession() or the boot
    // sweep.
    case STATE_TRANSCODING:
      break;

    // V0.6 -- syncing also runs synchronously (HTTP PUT is blocking).  All
    // the work happens inside runSyncSweep(); by the time control returns
    // here the state has already moved back to IDLE.  This case exists so
    // the switch stays exhaustive if control ever lands here mid-sweep.
    case STATE_SYNCING:
      break;

    case STATE_ERROR:
      // Wait for long press to recover (handled at the top of loop()).
      break;

    case STATE_BOOTING:
    default:
      // BOOTING is transient and should not appear in loop(); setup()
      // either transitions to IDLE or to ERROR before returning.
      delay(10);
      break;
  }
}


// =============================================================================
//  STATE / LED HELPERS
// =============================================================================
void setState(State s) {
  currentState      = s;
  lastLedToggleMs   = millis();
  ledPhaseOn        = true;

  // Paint the target state's LED colour immediately so transitions are
  // visible without waiting for the next toggle tick.
  switch (s) {
    case STATE_BOOTING:
      setLedColor(LED_BLUE, true);
      break;
    case STATE_ERROR:
      setLedColor(LED_MAGENTA, true);
      break;
    case STATE_IDLE:
      setLedColor(LED_GREEN, true);
      break;
    case STATE_RECORDING:
      setLedColor(LED_RED, true);
      break;
    case STATE_PAUSED:
      setLedColor(LED_ORANGE, true);
      break;
    case STATE_TRANSCODING:
      setLedColor(LED_CYAN, true);
      break;
    case STATE_SYNCING:
      setLedColor(LED_BLUE_SYNC, true);
      break;
  }
}

void setLedColor(uint32_t color, bool on) {
  if (on) {
    pixel.setPixelColor(0, color);
  } else {
    pixel.setPixelColor(0, 0);
  }
  pixel.show();
}

void updateLeds() {
  unsigned long now = millis();

  // Boot-phase overrides take precedence over the state-based LED pattern,
  // because the boot phases need distinct visual signals that don't
  // correspond to any of the regular states.
  if (currentState == STATE_BOOTING && bootPhase == BOOT_PHASE_AP_MODE) {
    if (!ledPhaseOn) { ledPhaseOn = true; setLedColor(LED_BLUE, true); }
    return;
  }
  if (currentState == STATE_BOOTING && bootPhase == BOOT_PHASE_DONE_BLINK) {
    if (bootDoneBlinkUntilMs == 0) bootDoneBlinkUntilMs = millis() + BOOT_DONE_BLINK_MS;
    if ((uint32_t)(now - lastLedToggleMs) >= 100) {
      lastLedToggleMs = now;
      ledPhaseOn = !ledPhaseOn;
      setLedColor(LED_GREEN, ledPhaseOn);
    }
    if (now >= bootDoneBlinkUntilMs) {
      bootPhase = BOOT_PHASE_TRYING;  // sentinel: override no longer active
      bootDoneBlinkUntilMs = 0;
    }
    return;
  }
  if (currentState == STATE_BOOTING && bootPhase == BOOT_PHASE_SWEEP) {
    // V0.5 -- fast cyan blink during the post-boot transcoding sweep.
    if ((uint32_t)(now - lastLedToggleMs) >= TRANSCODING_LED_PERIOD_MS) {
      lastLedToggleMs = now;
      ledPhaseOn = !ledPhaseOn;
      setLedColor(LED_CYAN, ledPhaseOn);
    }
    return;
  }

  switch (currentState) {
    case STATE_BOOTING:
      if ((uint32_t)(now - lastLedToggleMs) >= BLUE_SLOW_MS) {
        lastLedToggleMs = now;
        ledPhaseOn = !ledPhaseOn;
        setLedColor(LED_BLUE, ledPhaseOn);
      }
      break;

    case STATE_ERROR:
      if (!ledPhaseOn) { ledPhaseOn = true; setLedColor(LED_MAGENTA, true); }
      break;

    case STATE_IDLE:
      if (!ledPhaseOn) { ledPhaseOn = true; setLedColor(LED_GREEN, true); }
      break;

    case STATE_RECORDING: {
      uint32_t phaseLen = ledPhaseOn ? RECORDING_LED_ON_MS
                                    : (RECORDING_LED_PERIOD_MS - RECORDING_LED_ON_MS);
      if ((uint32_t)(now - lastLedToggleMs) >= phaseLen) {
        lastLedToggleMs = now;
        ledPhaseOn = !ledPhaseOn;
        setLedColor(LED_RED, ledPhaseOn);
      }
      break;
    }

    case STATE_PAUSED:
      if ((uint32_t)(now - lastLedToggleMs) >= BLUE_SLOW_MS) {
        lastLedToggleMs = now;
        ledPhaseOn = !ledPhaseOn;
        setLedColor(LED_ORANGE, ledPhaseOn);
      }
      break;

    // V0.5 -- cyan fast blink while transcoding runs.
    case STATE_TRANSCODING:
      if ((uint32_t)(now - lastLedToggleMs) >= TRANSCODING_LED_PERIOD_MS) {
        lastLedToggleMs = now;
        ledPhaseOn = !ledPhaseOn;
        setLedColor(LED_CYAN, ledPhaseOn);
      }
      break;

    // V0.6 -- blue slow blink while syncing; if we are flashing a per-
    // session confirmation, override the pattern with solid green for
    // SYNC_OK_FLASH_MS and then fall back to IDLE solid green.
    case STATE_SYNCING:
      if (syncOkFlashActive && (int32_t)(syncOkFlashUntilMs - now) > 0) {
        if (!ledPhaseOn) {
          ledPhaseOn = true;
          setLedColor(LED_GREEN, true);
        }
      } else if ((uint32_t)(now - lastLedToggleMs) >= SYNC_LED_PERIOD_MS) {
        lastLedToggleMs = now;
        ledPhaseOn = !ledPhaseOn;
        setLedColor(LED_BLUE_SYNC, ledPhaseOn);
      }
      break;
  }
}


// =============================================================================
//  SD CARD
// =============================================================================
// V0.5: bumped from V0.4's 1 MHz.  20 MHz cuts a 26 MB WAV read from ~3.5 min
// down to ~12 s, and is also the rate V0.6 will need for the chunk-uploads.
// Falls back to 4 MHz if the higher clock fails (some cheap cards flake at
// 20 MHz), and to 1 MHz only as a final fallback.  The clock that worked
// is recorded in sdSpiHzActive for logging.
bool initSdCard() {
  SPI.begin(PIN_SD_SCK, PIN_SD_MISO, PIN_SD_MOSI, PIN_SD_CS);

  if (SD.begin(PIN_SD_CS, SPI, SD_SPI_HI_HZ)) {
    sdSpiHzActive = SD_SPI_HI_HZ;
    Serial.printf("[OK  ] SD card ready at %lu Hz (primary)\n",
                  (unsigned long)sdSpiHzActive);
    return true;
  }
  Serial.printf("[WARN] SD failed at %lu Hz, retrying at %lu Hz\n",
                (unsigned long)SD_SPI_HI_HZ, (unsigned long)SD_SPI_FB_HZ);
  if (SD.begin(PIN_SD_CS, SPI, SD_SPI_FB_HZ)) {
    sdSpiHzActive = SD_SPI_FB_HZ;
    Serial.printf("[OK  ] SD card ready at %lu Hz (fallback)\n",
                  (unsigned long)sdSpiHzActive);
    return true;
  }
  Serial.printf("[WARN] SD failed at %lu Hz, retrying at 1000000 Hz\n",
                (unsigned long)SD_SPI_FB_HZ);
  if (SD.begin(PIN_SD_CS, SPI, 1000000)) {
    sdSpiHzActive = 1000000;
    Serial.printf("[OK  ] SD card ready at %lu Hz (minimal)\n",
                  (unsigned long)sdSpiHzActive);
    return true;
  }
  sdSpiHzActive = 0;
  return false;
}


// =============================================================================
//  I2S MICROPHONE
// =============================================================================
bool initI2sMic() {
  // INMP441 on standard I2S: 32-bit slots, mono, only the left channel.
  // The mic's L/R pin is assumed tied to GND.  Configuration matches the
  // official ESP_I2S Record_to_WAV example.
  i2sMic.setPins(PIN_MIC_SCK, PIN_MIC_WS, /*dout*/ -1, /*din*/ PIN_MIC_SD);
  if (!i2sMic.begin(I2S_MODE_STD,
                    SAMPLE_RATE,
                    I2S_DATA_BIT_WIDTH_32BIT,
                    I2S_SLOT_MODE_MONO,
                    I2S_STD_SLOT_LEFT)) {
    Serial.printf("[ERR ] i2s.begin failed, lastError=%d\n",
                  (int)i2sMic.lastError());
    return false;
  }
  return true;
}


// =============================================================================
//  WAV HEADER  (V0.4 verbatim -- the recorded WAVs the encoder reads back)
// =============================================================================
static inline void le_write_u16(uint8_t *p, uint16_t v) {
  p[0] = v & 0xFF;
  p[1] = (v >> 8) & 0xFF;
}
static inline void le_write_u32(uint8_t *p, uint32_t v) {
  p[0] = v & 0xFF;
  p[1] = (v >> 8) & 0xFF;
  p[2] = (v >> 16) & 0xFF;
  p[3] = (v >> 24) & 0xFF;
}

void writeWavHeader(File &f) {
  uint8_t hdr[44];
  uint32_t byteRate   = SAMPLE_RATE * CHANNELS * (BITS_PER_SAMPLE / 8);   // 88200
  uint16_t blockAlign = CHANNELS * (BITS_PER_SAMPLE / 8);                 // 4

  memcpy(&hdr[0],  "RIFF", 4);
  le_write_u32(&hdr[4], 0);                          // placeholder: file size - 8
  memcpy(&hdr[8],  "WAVE", 4);

  memcpy(&hdr[12], "fmt ", 4);
  le_write_u32(&hdr[16], 16);                        // subchunk1 size
  le_write_u16(&hdr[20], 1);                         // PCM
  le_write_u16(&hdr[22], CHANNELS);                  // mono
  le_write_u32(&hdr[24], SAMPLE_RATE);               // sample rate
  le_write_u32(&hdr[28], byteRate);                  // byte rate
  le_write_u16(&hdr[32], blockAlign);                // block align
  le_write_u16(&hdr[34], BITS_PER_SAMPLE);           // bits per sample

  memcpy(&hdr[36], "data", 4);
  le_write_u32(&hdr[40], 0);                         // placeholder: data size

  f.write(hdr, sizeof(hdr));
}

void updateWavSizes(File &f, uint32_t dataSize) {
  uint32_t riffSize = dataSize + 36;
  uint8_t  buf[4];
  le_write_u32(buf, riffSize);
  f.seek(4);
  f.write(buf, 4);
  le_write_u32(buf, dataSize);
  f.seek(40);
  f.write(buf, 4);
}
// =============================================================================
//  SESSION AND CHUNK MANAGEMENT  (V0.2 + V0.5 endSession-on-transcode)
// =============================================================================
bool openSession() {
  time_t now = time(nullptr);
  if (now < 1700000000UL) {
    Serial.println("[ERR ] Time not synced, cannot start session");
    return false;
  }
  struct tm tm_local;
  localtime_r(&now, &tm_local);
  snprintf(sessionFolder, sizeof(sessionFolder), "/rec_%04d-%02d-%02dT%02d%02d",
          tm_local.tm_year + 1900,
          tm_local.tm_mon + 1,
          tm_local.tm_mday,
          tm_local.tm_hour,
          tm_local.tm_min);

  if (!SD.mkdir(sessionFolder)) {
    Serial.printf("[ERR ] Could not create session folder %s\n", sessionFolder);
    return false;
  }
  Serial.printf("[OK  ] Session folder: %s\n", sessionFolder);

  currentChunk    = 1;
  sessionStartMs  = millis();
  continuousRecordingStartMs = millis();
  if (!openChunk(currentChunk)) {
    return false;
  }
  setState(STATE_RECORDING);
  return true;
}

bool openChunk(uint16_t n) {
  char path[48];
  snprintf(path, sizeof(path), "%s/%u.wav", sessionFolder, (unsigned)n);
  Serial.printf("[....] Opening chunk %u: %s\n", (unsigned)n, path);

  recordingFile = SD.open(path, FILE_WRITE);
  if (!recordingFile) {
    Serial.printf("[ERR ] Could not open chunk file %s\n", path);
    return false;
  }
  dataBytesWritten = 0;
  writeWavHeader(recordingFile);
  chunkStartMs = millis();
  Serial.printf("[OK  ] Chunk %u opened\n", (unsigned)n);
  return true;
}

void closeChunk() {
  if (recordingFile) {
    updateWavSizes(recordingFile, dataBytesWritten);
    recordingFile.flush();
    recordingFile.close();
    Serial.printf("[OK  ] Chunk %u closed, %u bytes\n",
                  (unsigned)currentChunk,
                  (unsigned)dataBytesWritten);
  }
}

void pauseRecording() {
  continuousRecordingStartMs = 0;
  closeChunk();
  setState(STATE_PAUSED);
}

void resumeRecording() {
  currentChunk++;
  if (!openChunk(currentChunk)) {
    setState(STATE_ERROR);
    return;
  }
  continuousRecordingStartMs = millis();
  setState(STATE_RECORDING);
}

void endSession() {
  // V0.5 -- save the folder path BEFORE zeroing it so we can transcode
  // the just-finished session even after we clear the live session state.
  char justEndedFolder[32];
  strncpy(justEndedFolder, sessionFolder, sizeof(justEndedFolder));
  justEndedFolder[sizeof(justEndedFolder) - 1] = '\0';

  closeChunk();
  currentChunk    = 0;
  chunkStartMs    = 0;
  sessionStartMs  = 0;
  sessionFolder[0] = '\0';
  Serial.println("[OK  ] Session ended");

  // V0.5 -- transcode the just-finished session.  Failures here NEVER push
  // us into STATE_ERROR; orphan WAVs are left and retried next boot.
  if (justEndedFolder[0] != '\0') {
    Serial.printf("[....] Transcoding session %s ...\n", justEndedFolder);
    setState(STATE_TRANSCODING);
    transcodeSessionFolder(justEndedFolder);
  }

  // V0.6 -- push it to the server straight away.  This is the happy path
  // for the common case (one lecture, sync now), so the user never has to
  // wait for the 5-minute retry to notice.  Non-fatal: on any failure we
  // fall through to IDLE and the periodic retry takes over.
  if (justEndedFolder[0] != '\0' && WiFi.status() == WL_CONNECTED) {
    Serial.printf("[SYNC] uploading %s ...\n", justEndedFolder);
    setState(STATE_SYNCING);
    syncOneSession(justEndedFolder);
    lastSyncMs = millis();
  } else if (justEndedFolder[0] != '\0') {
    Serial.println("[SYNC] no WiFi, will retry later");
  }

  setState(STATE_IDLE);
}
// =============================================================================
//  BUTTON  (active LOW, debounced, short-press and long-press detection)
// =============================================================================
bool readButtonDebounced() {
  bool rawNow   = (digitalRead(PIN_BUTTON) == HIGH);
  unsigned long now = millis();

  if (rawNow != btnRawLast) {
    btnRawLast      = rawNow;
    btnLastChangeMs = now;
  }
  if ((uint32_t)(now - btnLastChangeMs) >= BUTTON_DEBOUNCE_MS) {
    if (btnRawStable != btnRawLast) {
      btnRawStable = btnRawLast;
      if (!btnRawStable) {
        btnPressStartMs     = now;
        btnShortPressEvent  = false;
        btnLongPressEvent   = false;
      } else {
        unsigned long held = (uint32_t)(now - btnPressStartMs);
        if (held >= LONG_PRESS_MS) {
          btnLongPressEvent = true;
        } else if (held >= BUTTON_SHORT_MIN_MS && held < LONG_PRESS_MS) {
          btnShortPressEvent = true;
        }
      }
    }
  }
  return btnRawStable;
}

bool pollShortPressEvent() {
  readButtonDebounced();
  if (btnShortPressEvent) {
    btnShortPressEvent = false;
    return true;
  }
  return false;
}

bool pollLongPressEvent() {
  readButtonDebounced();
  if (btnLongPressEvent) {
    btnLongPressEvent = false;
    return true;
  }
  return false;
}


// =============================================================================
//  V0.5 -- TRANSCODING PIPELINE
// =============================================================================
//
//   * Every recorded session ends up on the SD as .aac chunks.
//   * WAVs are deleted ONLY after a verified encode.
//   * Failures never bounce us into STATE_ERROR; the next boot retries
//     any orphan WAVs left behind.
//
//   transcodeOneChunk(wavPath, aacPath)  -- the per-chunk safety dance:
//     1. encode wavPath -> "<aacPath>.tmp"
//     2. verify size > 0 AND first 2 bytes look like ADTS sync (0xFF 0xFx)
//     3. rename .tmp -> aacPath (or copy+remove fallback)
//     4. THEN delete wavPath
//
//   transcodeSessionFolder(folderPath)  -- encode every .wav that does
//     not already have a sibling .aac inside that folder.
//
//   runTranscodingSweep(nullptr | folderPath)  -- the boot sweep:
//     - if folderPath is non-null, scope to that folder (used by endSession)
//     - otherwise, walk /rec_* on the root.

// Cheap ADTS-magic check: open the file, peek at the first two bytes,
// rewind the cursor so we don't disturb the file.  ADTS sync is 0xFFF*
// where the second byte's high nibble is 1111.  This is the spec's
// "starts 0xFF Fx" check; full ADTS parsing happens server-side.
static bool isAdtsHeaderPeek(File &f) {
  if (f.size() < 2) return false;
  uint32_t restore = f.position();
  uint8_t b0 = 0, b1 = 0;
  if (f.readBytes((char*)&b0, 1) != 1) return false;
  if (f.readBytes((char*)&b1, 1) != 1) return false;
  f.seek(restore);
  return (b0 == 0xFF) && ((b1 & 0xF0) == 0xF0);
}

// Return true if "<wavPath minus .wav>.aac" already exists on the SD.
// Pure path-string manipulation -- no file open.
static bool hasMatchingAac(const char *wavPath) {
  size_t len = strlen(wavPath);
  if (len < 5) return false;
  if (strcmp(wavPath + len - 4, ".wav") != 0) return false;
  char aacPath[96];
  if (len + 1 > sizeof(aacPath)) return false;
  memcpy(aacPath, wavPath, len - 4);
  memcpy(aacPath + (len - 4), ".aac", 5);  // 4 chars + '\0'
  return SD.exists(aacPath);
}

bool transcodeOneChunk(const char *wavPath, const char *aacPath) {
  Serial.printf("[ENC ] chunk: %s -> %s\n", wavPath, aacPath);

  if (!SD.exists(wavPath)) {
    Serial.printf("[ENC ] source WAV missing: %s (skipping)\n", wavPath);
    return false;
  }
  if (SD.exists(aacPath)) {
    Serial.printf("[ENC ] target AAC already exists: %s (skipping)\n", aacPath);
    return true;
  }

  // A header-only WAV (e.g. pause straight after resume, or power lost
  // before the first buffer landed) holds no audio.  Encoding it would fail
  // forever and block the session from syncing, so drop it.  The sync step
  // tolerates the resulting gap in chunk numbers.
  {
    File w = SD.open(wavPath, FILE_READ);
    uint32_t wsize = w ? (uint32_t)w.size() : 0;
    if (w) w.close();
    if (wsize <= WAV_HEADER_BYTES) {
      Serial.printf("[ENC ] %s has no audio (%u B), removing\n",
                    wavPath, (unsigned)wsize);
      return SD.remove(wavPath);
    }
  }

  // Stage 1: encode to "<aacPath>.tmp".
  char tmpPath[80];
  snprintf(tmpPath, sizeof(tmpPath), "%s.tmp", aacPath);

  if (SD.exists(tmpPath)) {
    Serial.printf("[ENC ] removing stale tmp: %s\n", tmpPath);
    SD.remove(tmpPath);
  }

  bool ok = aacEncodeFile(wavPath, tmpPath);
  if (!ok) {
    Serial.printf("[ENC ] encode FAILED for %s\n", wavPath);
    if (SD.exists(tmpPath)) SD.remove(tmpPath);
    return false;
  }

  // Stage 2: verify the .tmp file.
  File vf = SD.open(tmpPath, FILE_READ);
  if (!vf) {
    Serial.printf("[ENC ] verify open failed: %s\n", tmpPath);
    if (SD.exists(tmpPath)) SD.remove(tmpPath);
    return false;
  }
  uint32_t aacSize = vf.size();
  bool     adtsOk  = isAdtsHeaderPeek(vf);
  vf.close();

  if (aacSize == 0) {
    Serial.printf("[ENC ] verify FAILED: empty output %s\n", tmpPath);
    if (SD.exists(tmpPath)) SD.remove(tmpPath);
    return false;
  }
  if (!adtsOk) {
    Serial.printf("[ENC ] verify FAILED: no ADTS sync in %s\n", tmpPath);
    if (SD.exists(tmpPath)) SD.remove(tmpPath);
    return false;
  }

  // Stage 3: atomic rename .tmp -> final AAC.
  if (!SD.rename(tmpPath, aacPath)) {
    // Rename() across dir entries can be flaky on some SD libs -- fall
    // back to a copy.
    Serial.printf("[ENC ] rename failed, falling back to copy: %s\n", aacPath);
    File in  = SD.open(tmpPath, FILE_READ);
    File out = SD.open(aacPath, FILE_WRITE);
    if (!in || !out) {
      if (in)  in.close();
      if (out) out.close();
      Serial.printf("[ENC ] copy fallback failed too; leaving tmp + WAV for next boot\n");
      return false;
    }
    uint8_t buf[1024];
    bool copyOk = true;
    while (in.available()) {
      int n = in.readBytes((char*)buf, sizeof(buf));
      if (n <= 0) break;
      if (out.write(buf, n) != (size_t)n) { copyOk = false; break; }
    }
    in.close();
    out.flush();
    out.close();
    if (!copyOk) {
      Serial.printf("[ENC ] copy fallback short write; leaving tmp + WAV for next boot\n");
      SD.remove(aacPath);
      return false;
    }
    SD.remove(tmpPath);
  }

  // Stage 4: ONLY now (verified AAC on disk) delete the source WAV.
  if (!SD.remove(wavPath)) {
    // Verified AAC exists, but we couldn't delete the WAV.  Log it; a
    // future boot sweep will see a matching .aac and skip this WAV.
    Serial.printf("[WARN] verified AAC at %s but couldn't delete %s\n",
                  aacPath, wavPath);
    return false;
  }
  Serial.printf("[ENC ] OK: %s (verified ADTS, removed WAV)\n", aacPath);
  return true;
}

void transcodeSessionFolder(const char *folderPath) {
  if (!SD.exists(folderPath)) {
    Serial.printf("[ENC ] folder missing: %s\n", folderPath);
    return;
  }

  // Pass 1: collect candidate .wav files (those without a sibling .aac).
  static struct Candidate { char path[64]; } cands[64];
  uint16_t candCount = 0;

  File d1 = SD.open(folderPath);
  if (!d1 || !d1.isDirectory()) {
    if (d1) d1.close();
    Serial.printf("[ENC ] not a directory: %s\n", folderPath);
    return;
  }
  while (candCount < (uint16_t)(sizeof(cands)/sizeof(cands[0]))) {
    File entry = d1.openNextFile();
    if (!entry) break;
    if (!entry.isDirectory()) {
      const char *name = entry.name();
      size_t nlen = strlen(name);
      if (nlen > 4 && strcmp(name + nlen - 4, ".wav") == 0) {
        char fullPath[96];
        snprintf(fullPath, sizeof(fullPath), "%s/%s", folderPath, name);
        if (!hasMatchingAac(fullPath)) {
          snprintf(cands[candCount].path, sizeof(cands[candCount].path),
                   "%s", fullPath);
          candCount++;
        }
      }
    }
    entry.close();
  }
  d1.close();

  if (candCount == 0) {
    Serial.printf("[ENC ] no orphans in %s\n", folderPath);
    return;
  }
  Serial.printf("[ENC ] %u chunk(s) to transcode in %s\n",
                (unsigned)candCount, folderPath);

  // Pass 2: encode each candidate, with retry.
  for (uint16_t i = 0; i < candCount; i++) {
    char aacPath[96];
    size_t plen = strlen(cands[i].path);
    if (plen + 1 > sizeof(aacPath)) continue;
    memcpy(aacPath, cands[i].path, plen - 4);
    memcpy(aacPath + (plen - 4), ".aac", 5);

    bool done = false;
    for (uint8_t attempt = 1; attempt <= AAC_MAX_ATTEMPTS && !done; attempt++) {
      uint32_t wavBytes = 0;
      {
        File w = SD.open(cands[i].path, FILE_READ);
        if (w) { wavBytes = w.size(); w.close(); }
      }
      uint32_t startedMs = millis();
      Serial.printf("[ENC ] chunk %u/%u: %u bytes -> %s (attempt %u)\n",
                    (unsigned)(i + 1),
                    (unsigned)candCount,
                    (unsigned)wavBytes,
                    aacPath,
                    (unsigned)attempt);
      if (transcodeOneChunk(cands[i].path, aacPath)) {
        uint32_t took = millis() - startedMs;
        uint32_t aacBytes = 0;
        {
          File a = SD.open(aacPath, FILE_READ);
          if (a) { aacBytes = a.size(); a.close(); }
        }
        // Pretty-print in MB to match the spec's example line.
        Serial.printf("[ENC ] chunk %u/%u: %.1f MB -> %.1f MB in %u s\n",
                      (unsigned)(i + 1),
                      (unsigned)candCount,
                      (float)wavBytes  / (1024.0f * 1024.0f),
                      (float)aacBytes  / (1024.0f * 1024.0f),
                      (unsigned)(took / 1000));
        done = true;
      } else if (attempt < AAC_MAX_ATTEMPTS) {
        Serial.printf("[ENC ] chunk %u/%u: attempt %u failed, retrying\n",
                      (unsigned)(i + 1),
                      (unsigned)candCount,
                      (unsigned)attempt);
      } else {
        Serial.printf("[ENC ] chunk %u/%u: giving up after %u attempts; WAV remains for next boot\n",
                      (unsigned)(i + 1),
                      (unsigned)candCount,
                      (unsigned)AAC_MAX_ATTEMPTS);
      }
      updateLeds();
    }
  }
}

void runTranscodingSweep(const char *onlyFolder) {
  if (onlyFolder != nullptr) {
    transcodeSessionFolder(onlyFolder);
    return;
  }

  File root = SD.open("/");
  if (!root || !root.isDirectory()) {
    if (root) root.close();
    Serial.println("[ENC ] sweep: cannot open SD root");
    return;
  }

  static char folders[16][32];
  uint8_t folderCount = 0;
  while (folderCount < (uint8_t)(sizeof(folders)/sizeof(folders[0]))) {
    File entry = root.openNextFile();
    if (!entry) break;
    if (entry.isDirectory()) {
      const char *name = entry.name();
      size_t nlen = strlen(name);
      if (nlen >= 5 && strncmp(name, "rec_", 4) == 0) {
        snprintf(folders[folderCount], sizeof(folders[folderCount]),
                 "/%s", name);
        folderCount++;
      }
    }
    entry.close();
  }
  root.close();

  Serial.printf("[ENC ] sweep: %u session folder(s) to consider\n",
                (unsigned)folderCount);
  for (uint8_t i = 0; i < folderCount; i++) {
    Serial.printf("[ENC ] sweep: %s\n", folders[i]);
    transcodeSessionFolder(folders[i]);
    updateLeds();
  }
}


// =============================================================================
//  V0.6 -- SYNC PIPELINE
// =============================================================================
//
//  Per session folder that contains .aac chunks:
//      announce -> PUT each chunk -> finish -> DELETE on confirmation
//
//  The delete is the only destructive step and happens ONLY when the server
//  answers finish with 200 stitched / already_ready.  Every other outcome
//  (transport error, 404, 409, 500, user abort) leaves the files on the SD
//  so the periodic retry can try again -- losing the periodic retry must
//  never mean losing audio.
//
//  Guard: runSyncSweep() is only ever entered from BOOTING-sweep, IDLE, or
//  endSession() -- i.e. never while RECORDING or PAUSED.

// Count contiguous .aac files 1..N in the folder and return the highest
// index present.  Returns 0 when there are none.  Chunks are named by the
// recorder as 1.wav -> 1.aac, 2.aac, ... so a contiguous 1..N run is the
// normal case; we stop at the first gap so a half-uploaded set does not
// claim a higher expected_count than the server can satisfy.
static int countAacChunks(const char *folderPath) {
  int highest = 0;
  for (int i = 1; i <= SYNC_MAX_CHUNKS; i++) {
    char p[128];
    snprintf(p, sizeof(p), "%s/%d.aac", folderPath, i);
    if (SD.exists(p)) highest = i;
    else break;
  }
  return highest;
}

// Delete every .aac in the folder and then the folder itself.  Only called
// after a confirmed stitch.
static void purgeSessionFolder(const char *folderPath) {
  for (int i = 1; i <= SYNC_MAX_CHUNKS; i++) {
    char p[128];
    snprintf(p, sizeof(p), "%s/%d.aac", folderPath, i);
    if (SD.exists(p)) SD.remove(p);
  }
  if (!SD.rmdir(folderPath)) {
    Serial.printf("[SYNC] could not rmdir %s (may not be empty)\n", folderPath);
  } else {
    Serial.printf("[SYNC] purged session folder %s\n", folderPath);
  }
}

bool syncOneSession(const char *folderPath) {
  if (!SD.exists(folderPath)) {
    Serial.printf("[SYNC] folder missing: %s\n", folderPath);
    return false;
  }

  int expected = countAacChunks(folderPath);
  if (expected == 0) {
    // Nothing encoded yet -- not an error, just nothing to send.  (The
    // transcode sweep owns turning .wav into .aac.)
    return false;
  }

  char sessionId[80], iso[32], title[40];
  syncBuildSessionId(folderPath, sessionId, sizeof(sessionId));
  syncFolderToIso(folderPath, iso, sizeof(iso));
  snprintf(title, sizeof(title), "%s", folderPath);
  for (char *p = title; *p; p++) if (*p == '/') { memmove(title, p + 1, strlen(p)); break; }

  Serial.printf("[SYNC] session %s: %d chunk(s), started_at=%s\n",
                sessionId, expected, iso);

  // ---- 1. Announce ----
  // Idempotent server-side, so a lost response costs us nothing: the retry
  // just gets 200-already-known and proceeds.
  if (!syncAnnounce(sessionId, title, expected, iso)) {
    Serial.println("[SYNC] announce failed, keeping files for retry");
    return false;
  }
  if (syncAborted()) return false;

  // ---- 2. Upload chunks ----
  for (int i = 1; i <= expected; i++) {
    if (syncAborted()) {
      Serial.println("[SYNC] aborted mid-upload; partial chunks kept for retry");
      return false;
    }
    char chunkPath[128];
    snprintf(chunkPath, sizeof(chunkPath), "%s/%d.aac", folderPath, i);

    if (!syncPutChunk(sessionId, i, chunkPath)) {
      // 404/409 mean the server lost our session (restart, or it moved on
      // to ready).  Re-announce once and retry this chunk, per the brief.
      Serial.printf("[SYNC] chunk %d failed, re-announcing and retrying once\n", i);
      updateLeds();
      if (!syncAnnounce(sessionId, title, expected, iso)) return false;
      if (!syncPutChunk(sessionId, i, chunkPath)) {
        Serial.printf("[SYNC] chunk %d failed again, keeping files\n", i);
        return false;
      }
    }
  }

  // ---- 3. Finish ----
  // missing[] handling: if the server says a chunk is absent we re-PUT
  // exactly those and finish once more, then give up (one round only).
  int missing[SYNC_MAX_CHUNKS];
  SyncFinishResult fr = syncFinish(sessionId, missing, SYNC_MAX_CHUNKS);

  if (fr == SYNC_FINISH_MISSING_CHUNKS) {
    Serial.printf("[SYNC] server reported missing chunks, one repair round\n");
    for (int m = 0; m < SYNC_MAX_CHUNKS && missing[m] > 0; m++) {
      int idx = missing[m];
      if (idx < 1 || idx > expected) continue;
      if (syncAborted()) return false;
      char chunkPath[128];
      snprintf(chunkPath, sizeof(chunkPath), "%s/%d.aac", folderPath, idx);
      if (!syncPutChunk(sessionId, idx, chunkPath)) {
        Serial.printf("[SYNC] repair of chunk %d failed, keeping files\n", idx);
        return false;
      }
    }
    fr = syncFinish(sessionId, nullptr, 0);
  }

  // ---- 4. Delete only on confirmation ----
  if (syncFinishMeansSafeToDelete(fr)) {
    Serial.printf("[SYNC] session %s confirmed server-side, deleting local AAC\n",
                  sessionId);
    purgeSessionFolder(folderPath);
    return true;   // caller may flash green
  }

  if (fr == SYNC_FINISH_ABORTED) {
    Serial.println("[SYNC] aborted before finish; keeping files for retry");
  } else {
    Serial.printf("[SYNC] finish not confirmed (%d); keeping files for retry\n", (int)fr);
  }
  return false;
}

void runSyncSweep(const char *onlyFolder) {
  // Clear any stale abort left over from a previous user-initiated abort.
  syncClearAbort();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[SYNC] no WiFi, deferring sync");
    return;
  }

  if (onlyFolder != nullptr) {
    syncOneSession(onlyFolder);
    return;
  }

  // Walk the SD root for /rec_* folders that still hold .aac chunks.
  File root = SD.open("/");
  if (!root || !root.isDirectory()) {
    if (root) root.close();
    Serial.println("[SYNC] cannot open SD root");
    return;
  }

  static char folders[16][32];
  uint8_t folderCount = 0;
  while (folderCount < (uint8_t)(sizeof(folders)/sizeof(folders[0]))) {
    File entry = root.openNextFile();
    if (!entry) break;
    if (entry.isDirectory()) {
      const char *name = entry.name();
      if (strncmp(name, "rec_", 4) == 0) {
        snprintf(folders[folderCount], sizeof(folders[folderCount]),
                 "/%s", name);
        folderCount++;
      }
    }
    entry.close();
  }
  root.close();

  if (folderCount == 0) {
    Serial.println("[SYNC] no /rec_* folders to sync");
    return;
  }

  Serial.printf("[SYNC] sweep: %u session folder(s)\n", (unsigned)folderCount);
  int synced = 0, skipped = 0;
  for (uint8_t i = 0; i < folderCount; i++) {
    if (syncAborted()) {
      Serial.println("[SYNC] sweep aborted by user");
      break;
    }
    // Keep the LED blinking and let a long-press land between sessions.
    updateLeds();

    if (syncOneSession(folders[i])) {
      synced++;
      // Brief green flash confirms to the user that data left the device.
      syncOkFlashActive  = true;
      syncOkFlashUntilMs = millis() + SYNC_OK_FLASH_MS;
      Serial.printf("[OK  ] synced %s\n", folders[i]);
      updateLeds();
      delay(SYNC_OK_FLASH_MS);
      syncOkFlashActive = false;
    } else {
      skipped++;
      Serial.printf("[SYNC] %s still pending\n", folders[i]);
    }
  }

  Serial.printf("[SYNC] sweep done: %d synced, %d pending\n", synced, skipped);
}
// =============================================================================
//  WIFI PROVISIONING  (V0.3 -- preserved as-is for the AP/portal flow)
// =============================================================================
static void loadSavedNetworks() {
  Preferences prefs;
  prefs.begin("ai_notetaker", true);
  savedNetworkCount = prefs.getInt("count", 0);
  if (savedNetworkCount < 0) savedNetworkCount = 0;
  if (savedNetworkCount > MAX_SAVED_NETWORKS) savedNetworkCount = MAX_SAVED_NETWORKS;
  for (int i = 0; i < savedNetworkCount; i++) {
    char ssidKey[16], passKey[16];
    snprintf(ssidKey, sizeof(ssidKey), "ssid_%d", i);
    snprintf(passKey, sizeof(passKey), "pass_%d", i);
    String s = prefs.getString(ssidKey, "");
    String p = prefs.getString(passKey, "");
    strncpy(savedNetworks[i].ssid, s.c_str(), sizeof(savedNetworks[i].ssid));
    savedNetworks[i].ssid[sizeof(savedNetworks[i].ssid) - 1] = '\0';
    strncpy(savedNetworks[i].pass, p.c_str(), sizeof(savedNetworks[i].pass));
    savedNetworks[i].pass[sizeof(savedNetworks[i].pass) - 1] = '\0';
  }
  prefs.end();
}

// Add a new (ssid, password) to the front of the list, evicting the
// most recently added of the older entries (LIFO) if the list is full.
// Alternative if the SSID already exists: move it to front, update password.
static void addOrPromoteNetwork(const char* ssid, const char* pass) {
  int existing = -1;
  for (int i = 0; i < savedNetworkCount; i++) {
    if (strcmp(savedNetworks[i].ssid, ssid) == 0) { existing = i; break; }
  }
  if (existing >= 0) {
    SavedNetwork moved = savedNetworks[existing];
    for (int i = existing; i > 0; i--) {
      savedNetworks[i] = savedNetworks[i - 1];
    }
    strncpy(moved.pass, pass, sizeof(moved.pass));
    moved.pass[sizeof(moved.pass) - 1] = '\0';
    savedNetworks[0] = moved;
  } else {
    if (savedNetworkCount >= MAX_SAVED_NETWORKS) {
      savedNetworkCount = MAX_SAVED_NETWORKS - 1;
    }
    for (int i = savedNetworkCount; i > 0; i--) {
      savedNetworks[i] = savedNetworks[i - 1];
    }
    strncpy(savedNetworks[0].ssid, ssid, sizeof(savedNetworks[0].ssid));
    savedNetworks[0].ssid[sizeof(savedNetworks[0].ssid) - 1] = '\0';
    strncpy(savedNetworks[0].pass, pass, sizeof(savedNetworks[0].pass));
    savedNetworks[0].pass[sizeof(savedNetworks[0].pass) - 1] = '\0';
    savedNetworkCount++;
  }

  Preferences prefs;
  prefs.begin("ai_notetaker", false);
  prefs.putInt("count", savedNetworkCount);
  for (int i = 0; i < savedNetworkCount; i++) {
    char ssidKey[16], passKey[16];
    snprintf(ssidKey, sizeof(ssidKey), "ssid_%d", i);
    snprintf(passKey, sizeof(passKey), "pass_%d", i);
    prefs.putString(ssidKey, savedNetworks[i].ssid);
    prefs.putString(passKey, savedNetworks[i].pass);
  }
  prefs.end();
}

static void clearAllNetworks() {
  Preferences prefs;
  prefs.begin("ai_notetaker", false);
  prefs.clear();
  prefs.end();
  savedNetworkCount = 0;
}

// Remove the network at the given index (0 = most recent), shifting later
// entries down and persisting the updated list to NVS.
static void removeNetworkByIndex(int idx) {
  if (idx < 0 || idx >= savedNetworkCount) return;
  for (int i = idx; i < savedNetworkCount - 1; i++) {
    savedNetworks[i] = savedNetworks[i + 1];
  }
  savedNetworkCount--;

  Preferences prefs;
  prefs.begin("ai_notetaker", false);
  prefs.putInt("count", savedNetworkCount);
  for (int i = 0; i < savedNetworkCount; i++) {
    char ssidKey[16], passKey[16];
    snprintf(ssidKey, sizeof(ssidKey), "ssid_%d", i);
    snprintf(passKey, sizeof(passKey), "pass_%d", i);
    prefs.putString(ssidKey, savedNetworks[i].ssid);
    prefs.putString(passKey, savedNetworks[i].pass);
  }
  prefs.end();
}

// Try to connect to a single saved network.  Returns true on success.
static bool tryOneNetwork(int idx, uint32_t timeoutMs) {
  if (idx < 0 || idx >= savedNetworkCount) return false;
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true);
  delay(50);
  Serial.printf("[WIP ] Trying network %d/%d: %s\n",
                idx + 1, savedNetworkCount, savedNetworks[idx].ssid);
  WiFi.begin(savedNetworks[idx].ssid, savedNetworks[idx].pass);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if ((uint32_t)(millis() - start) >= timeoutMs) {
      Serial.println("[WIP ]   timed out");
      return false;
    }
    updateLeds();
    delay(50);
  }
  Serial.println("[WIP ]   connected!");
  return true;
}

static bool tryAllSavedNetworks() {
  const uint32_t perNetworkTimeout = 10000;
  for (int i = 0; i < savedNetworkCount; i++) {
    if (tryOneNetwork(i, perNetworkTimeout)) return true;
  }
  return false;
}

// -----------------------------------------------------------------------------
//  Captive portal page builders  (V0.4)
// -----------------------------------------------------------------------------
static String buildRootPage() {
  return
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>AI NoteTaker</title>"
    "<style>"
    "body{font-family:sans-serif;margin:1em auto;max-width:480px;padding:0 1em;color:#222}"
    "h1{font-size:1.4em;margin-bottom:0.2em}"
    "a.btn{display:block;text-align:center;background:#0a7;color:#fff;border-radius:0.3em;padding:0.7em;margin:0.5em 0;text-decoration:none}"
    "</style></head><body>"
    "<h1>AI NoteTaker WiFi Setup</h1>"
    "<p>What do you want to do?</p>"
    "<a class='btn' href='/add'>Add network</a>"
    "<a class='btn' href='/manage'>Manage networks</a>"
    "</body></html>";
}

static String buildAddPage() {
  String page =
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>Add network</title>"
    "<style>"
    "body{font-family:sans-serif;margin:1em auto;max-width:480px;padding:0 1em;color:#222}"
    "h1{font-size:1.4em}"
    "button,input,select{font-size:1em;padding:0.5em;margin:0.3em 0;width:100%;box-sizing:border-box}"
    "button{background:#0a7;color:#fff;border:none;border-radius:0.3em;cursor:pointer}"
    "a.btn{display:block;text-align:center;background:#aaa;color:#fff;border-radius:0.3em;padding:0.5em;margin:0.5em 0;text-decoration:none}"
    "</style></head><body>"
    "<h1>Add a network</h1>"
    "<form method='POST' action='/save'>"
    "<select name='s' id='ssid_select' "
    "onchange=\"document.getElementById('ssid').value=this.value\">"
    "<option value=''>-- choose a visible network --</option>";
  int n = WiFi.scanNetworks();
  for (int i = 0; i < n; i++) {
    String ssid = WiFi.SSID(i);
    if (ssid.length() == 0) continue;
    page += "<option value='" + ssid + "'>" + ssid + "</option>";
  }
  WiFi.scanDelete();
  page +=
    "</select>"
    "<input id='ssid' name='s_manual' placeholder='...or type SSID manually' maxlength='32'>"
    "<input name='p' type='password' placeholder='Wi-Fi password' maxlength='63'>"
    "<button type='submit'>Save &amp; reboot</button>"
    "</form>"
    "<a class='btn' href='/'>Back</a>"
    "</body></html>";
  return page;
}

static String buildManagePage() {
  String page =
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>Manage networks</title>"
    "<style>"
    "body{font-family:sans-serif;margin:1em auto;max-width:480px;padding:0 1em;color:#222}"
    "h1{font-size:1.4em}"
    ".net{border:1px solid #ddd;border-radius:0.3em;padding:0.5em;margin:0.5em 0;background:#f9f9f9}"
    ".net form{display:inline;float:right}"
    ".net button{background:#c33;color:#fff;border:none;border-radius:0.3em;padding:0.3em 0.8em;cursor:pointer}"
    ".empty{color:#888;font-style:italic;margin:1em 0}"
    "a.btn{display:block;text-align:center;background:#aaa;color:#fff;border-radius:0.3em;padding:0.5em;margin:0.5em 0;text-decoration:none}"
    "</style></head><body>"
    "<h1>Saved networks (" + String(savedNetworkCount) + "/" + String(MAX_SAVED_NETWORKS) + ")</h1>";
  if (savedNetworkCount == 0) {
    page += "<p class='empty'>No networks saved yet.</p>";
  } else {
    page += "<ol style='padding-left:1.2em'>";
    for (int i = 0; i < savedNetworkCount; i++) {
      page += "<li class='net'>" + String(savedNetworks[i].ssid);
      page += "<form method='POST' action='/delete'>";
      page += "<input type='hidden' name='idx' value='" + String(i) + "'>";
      page += "<button type='submit'>Delete</button></form></li>";
    }
    page += "</ol>";
  }
  page += "<a class='btn' href='/'>Back to home</a>"
          "</body></html>";
  return page;
}

// -----------------------------------------------------------------------------
//  Captive portal handlers  (V0.4)
// -----------------------------------------------------------------------------
static void handleRoot()    { webServer.send(200, "text/html", buildRootPage()); }
static void handleAdd()     { webServer.send(200, "text/html", buildAddPage()); }
static void handleManage()  { webServer.send(200, "text/html", buildManagePage()); }

static void handleSave() {
  String s        = webServer.arg("s");
  String s_manual = webServer.arg("s_manual");
  String p        = webServer.arg("p");
  String ssid = s_manual.length() > 0 ? s_manual : s;
  if (ssid.length() == 0) {
    webServer.send(400, "text/html",
      "<html><body><h1>Missing SSID</h1>"
      "<p>Pick a network from the dropdown or type one manually.</p>"
      "<a href='/add'>Back</a></body></html>");
    return;
  }
  Serial.printf("[AP  ] Saving network: %s\n", ssid.c_str());
  addOrPromoteNetwork(ssid.c_str(), p.c_str());
  webServer.send(200, "text/html",
    "<html><body><h1>Saved!</h1>"
    "<p>Rebooting in 3 seconds...</p>"
    "<script>setTimeout(function(){location='/';},3000);</script></body></html>");
  delay(500);
  ESP.restart();
}

static void handleDelete() {
  int idx = webServer.arg("idx").toInt();
  if (idx >= 0 && idx < savedNetworkCount) {
    Serial.printf("[AP  ] Deleting network %d: %s\n", idx, savedNetworks[idx].ssid);
    removeNetworkByIndex(idx);
  } else {
    Serial.printf("[AP  ] Delete: invalid index %d\n", idx);
  }
  webServer.sendHeader("Location", "/manage", true);
  webServer.send(302);
}

// Top-level: try saved networks; if all fail, start AP + custom portal.
bool connectOrProvisionWifi() {
  loadSavedNetworks();
  Serial.printf("[WIP ] %d saved network(s)\n", savedNetworkCount);

  if (tryAllSavedNetworks()) {
    bootPhase = BOOT_PHASE_TRYING;
    return true;
  }

  Serial.println("[WIP ] No saved network worked -- starting AP mode");
  bootPhase = BOOT_PHASE_AP_MODE;

  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASSWORD);
  Serial.printf("[AP  ] AP up: SSID=%s  IP=%s\n",
                AP_SSID, WiFi.softAPIP().toString().c_str());

  // Captive portal: redirect every DNS query to our AP IP so any URL the
  // phone opens lands on our web server.
  dnsServer.start(53, "*", WiFi.softAPIP());

  webServer.on("/",       HTTP_GET,  handleRoot);
  webServer.on("/add",    HTTP_GET,  handleAdd);
  webServer.on("/manage", HTTP_GET,  handleManage);
  webServer.on("/save",   HTTP_POST, handleSave);
  webServer.on("/delete", HTTP_POST, handleDelete);
  webServer.onNotFound([]() {
    webServer.sendHeader("Location", "/", true);
    webServer.send(302);
  });
  webServer.begin();

  Serial.println("[AP  ] Portal ready at http://192.168.4.1/");

  unsigned long startMs = millis();
  while ((unsigned long)(millis() - startMs) < (AP_PORTAL_TIMEOUT_S * 1000UL)) {
    dnsServer.processNextRequest();
    webServer.handleClient();
    delay(5);
  }

  Serial.println("[ERR ] AP config portal timed out");
  dnsServer.stop();
  webServer.stop();
  WiFi.softAPdisconnect(true);
  return false;
}


// =============================================================================
//  NTP / TIMEZONE
// =============================================================================
void syncTimeBlocking() {
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  setenv("TZ", "GMT0BST,M3.5.0/1,M10.5.0/2", 1);
  tzset();

  unsigned long start = millis();
  while (time(nullptr) < 1700000000UL) {
    if ((uint32_t)(millis() - start) >= NTP_SYNC_TIMEOUT_MS) {
      Serial.println("[WARN] NTP sync slow - still waiting for sane time");
      start = millis();
    }
    updateLeds();
    delay(200);
  }
}