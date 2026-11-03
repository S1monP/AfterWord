// =============================================================================
//  config.h  --  pins, tuning and server settings shared by every module.
//
//  Per-device secrets go in secrets.h (gitignored); copy secrets.example.h to
//  create it.  It is included first so it can override anything below.
//  main/CMakeLists.txt defines HAVE_SECRETS_H when the file exists.
// =============================================================================
#pragma once

#ifdef HAVE_SECRETS_H
#include "secrets.h"
#endif

#define FW_VERSION              "0.8.0-dev"

// -----------------------------------------------------------------------------
//  Pins (ESP32-S3-DevKitC-1 N16R8 -- unchanged from the Arduino firmware)
// -----------------------------------------------------------------------------
#define PIN_BUTTON              1
#define PIN_MIC_SCK             38
#define PIN_MIC_WS              39
#define PIN_MIC_SD              40
#define PIN_SD_CS               41
#define PIN_SD_MOSI             42
#define PIN_SD_MISO             47
#define PIN_SD_SCK              21
#define PIN_NEOPIXEL            48

// -----------------------------------------------------------------------------
//  Audio
// -----------------------------------------------------------------------------
// INMP441, 32-bit I2S slots, left channel (L/R tied to GND).  Samples are
// multiplied by MIC_GAIN with saturation, then the high 16 bits go to the
// encoder -- the same conversion the Arduino firmware used.  Tune by ear:
// 4 = subtle, 8 = comfortable speech, 16 = quiet speaker.
#define SAMPLE_RATE             22050
#define MIC_GAIN                8

// AAC-LC mono, ADTS framed; must match the server's canonical format.
#define AAC_BITRATE             64000
#define FRAME_SAMPLES           1024        // one AAC-LC frame

// New chunk file every CHUNK_SECONDS of recording (the upload unit).  The
// encoder keeps running across rotations, so chunks join without a gap.
#define CHUNK_SECONDS           300
// fsync the open chunk this often: a power cut loses at most this much.
#define FLUSH_SECONDS           10

// Buffer between microphone and encoder/SD writer, in PSRAM.  Absorbs SD
// card stalls; 512 KB of 16-bit mono is ~11.8 s of audio.
#define PCM_RING_BYTES          (512 * 1024)

// Auto-stop after this much continuous recording (pauses reset it).
#define MAX_RECORDING_MS        (4UL * 60 * 60 * 1000)

// -----------------------------------------------------------------------------
//  Storage
// -----------------------------------------------------------------------------
#define SD_MOUNT                "/sdcard"
#define SESSION_START_FILE      "start.txt"
// Chunks per session folder the sync step handles (5 min each; pauses add
// chunks too).
#define SYNC_MAX_CHUNKS         128

// -----------------------------------------------------------------------------
//  WiFi
// -----------------------------------------------------------------------------
#define MAX_SAVED_NETWORKS      5
// The setup portal's own network. Set your own password in secrets.h
// (WPA2: 8 to 63 characters); the default is only a placeholder.
#ifndef AP_SSID
#define AP_SSID                 "AfterWord-Setup"
#endif
#ifndef AP_PASSWORD
#define AP_PASSWORD             "afterword-setup"
#endif
#define AP_PORTAL_TIMEOUT_S     300
#define WIFI_TRY_MS             10000       // per network, per attempt
#define WIFI_RETRY_INTERVAL_MS  60000       // pause between full rounds
#define WIFI_BOOT_WAIT_MS       20000       // boot waits this long for WiFi...
#define NTP_BOOT_WAIT_MS        10000       // ...and this long for NTP

// -----------------------------------------------------------------------------
//  Server / sync
// -----------------------------------------------------------------------------
// Local development: the PC running `python app.py` in server/ (`ipconfig`
// shows its IPv4 address).  For the VPS set an https:// URL in secrets.h;
// certificates are checked against the built-in CA bundle.
#ifndef SERVER_URL
#define SERVER_URL              "http://192.168.0.50:8000"
#endif
// Sent as "Authorization: Bearer <token>" when non-empty.
#ifndef DEVICE_TOKEN
#define DEVICE_TOKEN            ""
#endif

#define HTTP_TIMEOUT_MS         30000
// The announce is a small JSON answered at once, and it is the first
// request of a sweep: a short timeout finds an absent server quickly.
#define HTTP_ANNOUNCE_TIMEOUT_MS 10000
#define SYNC_RETRY_INTERVAL_MS  (5UL * 60 * 1000)
#define WIFI_UP_SYNC_DELAY_MS   15000       // sync this long after WiFi comes up

// Any time() below this means "clock never set" (the RTC starts at 1970).
#define TIME_VALID_AFTER        1700000000L
