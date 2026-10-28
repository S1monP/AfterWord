// =============================================================================
//  AI_NoteTaker_V0.4  -  MVP Firmware for ESP32-S3-DevKitC (N16R8)
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
//    - long press   -> END session: close chunk, back to IDLE
//    - Any fault    -> ERROR: solid magenta, long press to recover to IDLE
//
//  Button gestures:
//    - short press : button released within 1 s of being pressed
//    - long press  : button held >= 1 s, then released
//    - pre-IDLE presses are ignored
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
#define LED_ORANGE   0xFF8000u   // G=255, R=128, B=0  (warm amber, used for PAUSED)

#define NUM_PIXELS   1
Adafruit_NeoPixel pixel(NUM_PIXELS, PIN_NEOPIXEL, NEO_GRB + NEO_KHZ800);

// I2S mic instance (used by initI2sMic and the recording loop).
static I2SClass i2sMic;

// -----------------------------------------------------------------------------
//  STATE MACHINE
// -----------------------------------------------------------------------------
enum State : uint8_t {
  STATE_BOOTING,           // transient: blue slow blink until SD + WiFi + NTP + mic
  STATE_IDLE,              // solid green; waiting for session start
  STATE_RECORDING,         // red 500ms flash; chunk N open, writing samples
  STATE_PAUSED,            // orange 1s/1s slow blink; chunk N closed, mic drained
  STATE_ERROR              // solid magenta; long press to recover to IDLE
};

State currentState   = STATE_BOOTING;
unsigned long lastLedToggleMs = 0;
bool          ledPhaseOn      = false;

// -----------------------------------------------------------------------------
//  BOOT PHASE  (V0.3)
// -----------------------------------------------------------------------------
// Tracks what the boot sequence is doing, so updateLeds() can show the right
// LED pattern (blue slow blink = trying networks, solid blue = AP mode,
// fast green blink = almost ready).  Once we reach STATE_IDLE this is
// ignored.
enum BootPhase : uint8_t {
  BOOT_PHASE_TRYING,       // attempting saved networks one by one
  BOOT_PHASE_AP_MODE,      // captive portal AP up, waiting for user
  BOOT_PHASE_DONE_BLINK,   // WiFi+NTP+I2S all up, blinking green fast
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

// =============================================================================
//  SETUP
// =============================================================================
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("[BOOT] AI_NoteTaker V0.2 starting...");
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

      default:
        break;
    }
  }

  switch (currentState) {

    case STATE_IDLE:
      // Nothing to do -- waiting for long press.
      break;

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
  // because the boot phases need distinct visual signals (solid blue in
  // AP mode, fast green blink while finalising) that don't correspond to
  // any of the regular states.
  if (currentState == STATE_BOOTING && bootPhase == BOOT_PHASE_AP_MODE) {
    // Solid blue while the captive portal is up.
    if (!ledPhaseOn) {
      ledPhaseOn = true;
      setLedColor(LED_BLUE, true);
    }
    return;
  }
  if (currentState == STATE_BOOTING && bootPhase == BOOT_PHASE_DONE_BLINK) {
    // Fast green blink for BOOT_DONE_BLINK_MS after WiFi+NTP+I2S all up.
    if (bootDoneBlinkUntilMs == 0) {
      bootDoneBlinkUntilMs = millis() + BOOT_DONE_BLINK_MS;
    }
    if ((uint32_t)(now - lastLedToggleMs) >= 100) {
      lastLedToggleMs = now;
      ledPhaseOn = !ledPhaseOn;
      setLedColor(LED_GREEN, ledPhaseOn);
    }
    if (now >= bootDoneBlinkUntilMs) {
      // Done -- return to the normal state-based LED handling on the next
      // iteration by clearing the override.  Don't return; fall through to
      // the switch so setState(STATE_IDLE)'s solid-green paint takes effect.
      bootPhase = BOOT_PHASE_TRYING;  // sentinel: override no longer active
      bootDoneBlinkUntilMs = 0;
    }
    return;
  }

  switch (currentState) {

    case STATE_BOOTING:
      // Blue slow blink: 1s on / 1s off (while trying saved networks)
      if ((uint32_t)(now - lastLedToggleMs) >= BLUE_SLOW_MS) {
        lastLedToggleMs = now;
        ledPhaseOn = !ledPhaseOn;
        setLedColor(LED_BLUE, ledPhaseOn);
      }
      break;

    case STATE_ERROR:
      // Solid magenta
      if (!ledPhaseOn) {
        ledPhaseOn = true;
        setLedColor(LED_MAGENTA, true);
      }
      break;

    case STATE_IDLE:
      // Solid green
      if (!ledPhaseOn) {
        ledPhaseOn = true;
        setLedColor(LED_GREEN, true);
      }
      break;

    case STATE_RECORDING: {
      // Short red pulse: 250 ms on, 750 ms off, repeating every 1 s.
      // We split the period into on/off durations explicitly rather than
      // using a single toggle interval, so the duty cycle is not 50/50.
      // Wrapped in { } so phaseLen's scope is local to this case -- C++
      // forbids jumping over a variable initialisation in a switch.
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
      // Orange slow blink: 1s on / 1s off
      if ((uint32_t)(now - lastLedToggleMs) >= BLUE_SLOW_MS) {
        lastLedToggleMs = now;
        ledPhaseOn = !ledPhaseOn;
        setLedColor(LED_ORANGE, ledPhaseOn);
      }
      break;
  }
}


// =============================================================================
//  SD CARD
// =============================================================================
bool initSdCard() {
  // Initialise the SPI bus with our custom SD pins.  Calling SPI.begin()
  // here is safe even if the bus was used previously; it rebinds the SCK /
  // MISO / MOSI / CS pins to the values passed in.
  SPI.begin(PIN_SD_SCK, PIN_SD_MISO, PIN_SD_MOSI, PIN_SD_CS);
  if (!SD.begin(PIN_SD_CS, SPI, 1000000)) {
    return false;
  }
  return true;
}


// =============================================================================
//  WIFI PROVISIONING  (V0.3)
// =============================================================================
//
// Up to MAX_SAVED_NETWORKS SSID/password pairs are stored in NVS, ordered
// most-recent first.  On boot we try them in order; if one works, great.
// If none works, we drop into a WPA2-protected AP ("AI_NoteTaker-Setup")
// with a captive-portal web page that lets the user add a new network.

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
      start = millis();   // keep trying; spec requires waiting
    }
    updateLeds();
    delay(200);
  }
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
//  WAV HEADER (little-endian, 44 bytes, PCM 32-bit mono, sample rate from SAMPLE_RATE)
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
  uint32_t byteRate   = SAMPLE_RATE * CHANNELS * (BITS_PER_SAMPLE / 8);   // 32000
  uint16_t blockAlign = CHANNELS * (BITS_PER_SAMPLE / 8);                 // 2

  // RIFF chunk descriptor
  memcpy(&hdr[0],  "RIFF", 4);
  le_write_u32(&hdr[4], 0);                          // placeholder: file size - 8
  memcpy(&hdr[8],  "WAVE", 4);

  // fmt sub-chunk
  memcpy(&hdr[12], "fmt ", 4);
  le_write_u32(&hdr[16], 16);                        // subchunk1 size
  le_write_u16(&hdr[20], 1);                         // PCM
  le_write_u16(&hdr[22], CHANNELS);                  // mono
  le_write_u32(&hdr[24], SAMPLE_RATE);               // sample rate
  le_write_u32(&hdr[28], byteRate);                  // byte rate
  le_write_u16(&hdr[32], blockAlign);                // block align
  le_write_u16(&hdr[34], BITS_PER_SAMPLE);           // bits per sample

  // data sub-chunk
  memcpy(&hdr[36], "data", 4);
  le_write_u32(&hdr[40], 0);                         // placeholder: data size

  f.write(hdr, sizeof(hdr));
}

void updateWavSizes(File &f, uint32_t dataSize) {
  uint32_t riffSize = dataSize + 36;   // file size - 8 = 36 + dataSize
  uint8_t  buf[4];

  le_write_u32(buf, riffSize);
  f.seek(4);
  f.write(buf, 4);

  le_write_u32(buf, dataSize);
  f.seek(40);
  f.write(buf, 4);
}


// =============================================================================
//  RECORDING FILE MANAGEMENT  (V0.2: superseded by session/chunk helpers below)
// =============================================================================
// In V0.2 the per-file open / close logic moved into openSession() / openChunk()
// / closeChunk(), which manage the session folder and chunk rotation.  The
// legacy createRecordingFile() / stopRecordingAndClose() helpers from V0.1
// have been removed; only the WAV header helpers remain.

// =============================================================================
//  SESSION AND CHUNK MANAGEMENT  (V0.2)
// =============================================================================
//
// A "session" is the period between a long press (start) and the next
// long press (end).  Each session gets its own folder on the SD card
// named after the start time, e.g. "/rec_2026-09-30T1430".  Inside that
// folder, audio is written to sequentially-numbered chunk files:
// "1.wav", "2.wav", ...  A new chunk is opened either when the previous
// one reaches CHUNK_DURATION_MS, or when the user resumes from a pause.

bool openSession() {
  // Build the session folder name from NTP-synced local time.  We use
  // minute resolution (no seconds) to keep folder names short; the
  // chunk numbering inside the folder provides finer timing resolution.
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
  continuousRecordingStartMs = millis();  // start the continuous-recording timer
  if (!openChunk(currentChunk)) {
    return false;
  }
  // Transition into RECORDING now that the first chunk is open.  Without
  // this, the state machine stays in IDLE and the recording loop never
  // runs even though the file is open and ready.
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
  // Pausing resets the continuous-recording timer so a long pause
  // followed by a short resume doesn't accidentally trigger auto-stop.
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
  continuousRecordingStartMs = millis();  // restart the auto-stop clock
  setState(STATE_RECORDING);
}

void endSession() {
  closeChunk();
  currentChunk    = 0;
  chunkStartMs    = 0;
  sessionStartMs  = 0;
  sessionFolder[0] = '\0';
  setState(STATE_IDLE);
  Serial.println("[OK  ] Session ended");
}


// =============================================================================
//  BUTTON  (active LOW, debounced, short-press and long-press detection)
// =============================================================================
bool readButtonDebounced() {
  bool rawNow   = (digitalRead(PIN_BUTTON) == HIGH);   // true = released
  unsigned long now = millis();

  if (rawNow != btnRawLast) {
    btnRawLast      = rawNow;
    btnLastChangeMs = now;
  }
  if ((uint32_t)(now - btnLastChangeMs) >= BUTTON_DEBOUNCE_MS) {
    if (btnRawStable != btnRawLast) {
      btnRawStable = btnRawLast;
      if (!btnRawStable) {
        // button just pressed (active LOW)
        btnPressStartMs     = now;
        btnShortPressEvent  = false;
        btnLongPressEvent   = false;
      } else {
        // button just released -- classify the gesture
        unsigned long held = (uint32_t)(now - btnPressStartMs);
        if (held >= LONG_PRESS_MS) {
          btnLongPressEvent = true;
        } else if (held >= BUTTON_SHORT_MIN_MS && held < LONG_PRESS_MS) {
          btnShortPressEvent = true;
        }
        // Held < BUTTON_SHORT_MIN_MS => ignore as bounce / glitch.
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

