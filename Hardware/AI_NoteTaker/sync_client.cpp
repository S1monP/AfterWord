// =============================================================================
//  sync_client.cpp   (V0.6)  --  HTTP upload/sync client.
//
//  Talks to the Server's device-facing API (see sync_client.h for the
//  contract).  All calls are cooperative-abortable so a button press can
//  cut a long sync short without corrupting local state.
// =============================================================================

#include "sync_client.h"

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <SD.h>
#include <esp_mac.h>
#include <time.h>

// AAC_SAMPLE_RATE / AAC_CHANNELS are declared in aac_encoder.h; the announce
// payload must advertise exactly what the encoder produced.
#include "aac_encoder.h"

// SERVER_HOST / SERVER_PORT / DEVICE_TOKEN.  Must come from the shared
// header: a #define in the .ino is invisible to this translation unit.
#include "config.h"

// Adds the bearer token when one is configured (see secrets.example.h).
static void addAuthHeader(HTTPClient &http) {
  if (DEVICE_TOKEN[0] != '\0') {
    http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);
  }
}

// Chunk uploads of ~2.4 MB over WiFi need a generous timeout; the default
// HTTPClient timeout is far too short and aborts mid-PUT, which would look
// like a transport error on every large chunk.
#define SYNC_HTTP_TIMEOUT_MS   30000

// Tiny JSON-string field extractor.  We only ever need to pull an integer
// out of a flat array ("missing": [3, 7]) and a short reason string, so a
// full JSON parser would be a lot of flash for very little benefit.
static bool extractIntArray(const String &body, const char *key,
                            int *out, int maxOut, int &outCount) {
  outCount = 0;
  String needle = String("\"") + key + "\"";
  int k = body.indexOf(needle);
  if (k < 0) return false;
  int lb = body.indexOf('[', k);
  if (lb < 0) return false;
  int rb = body.indexOf(']', lb);
  if (rb < 0) return false;

  String inner = body.substring(lb + 1, rb);
  int start = 0;
  while (start < (int)inner.length() && outCount < maxOut) {
    int comma = inner.indexOf(',', start);
    String tok = (comma < 0) ? inner.substring(start)
                             : inner.substring(start, comma);
    tok.trim();
    if (tok.length()) {
      long v = strtol(tok.c_str(), nullptr, 10);
      if (v > 0) out[outCount++] = (int)v;
    }
    if (comma < 0) break;
    start = comma + 1;
  }
  return true;
}

static String extractReason(const String &body) {
  int k = body.indexOf("\"reason\"");
  if (k < 0) return "";
  int q1 = body.indexOf('"', body.indexOf(':', k));
  if (q1 < 0) return "";
  int q2 = body.indexOf('"', q1 + 1);
  if (q2 < 0) return "";
  return body.substring(q1 + 1, q2);
}

static String extractError(const String &body) {
  int k = body.indexOf("\"error\"");
  if (k < 0) return "";
  int q1 = body.indexOf('"', body.indexOf(':', k));
  if (q1 < 0) return "";
  int q2 = body.indexOf('"', q1 + 1);
  if (q2 < 0) return "";
  return body.substring(q1 + 1, q2);
}

// ---------------------------------------------------------------------------
//  Cooperative abort flag
// ---------------------------------------------------------------------------
static volatile bool g_syncAbort = false;

void syncAbort()        { g_syncAbort = true;  }
void syncClearAbort()   { g_syncAbort = false; }
bool syncAborted()      { return g_syncAbort; }

// ---------------------------------------------------------------------------
//  Deterministic session id + ISO timestamp
// ---------------------------------------------------------------------------
size_t syncBuildSessionId(const char *folderName, char *out, size_t outLen) {
  if (!out || outLen == 0) return 0;
  out[0] = '\0';
  if (!folderName || !*folderName) return 0;

  // 4 hex digits from the efuse MAC keeps ids unique per device while
  // staying readable in logs and within the server's 64-char limit.
  uint8_t mac[6] = {0};
  esp_efuse_mac_get_default(mac);

  // folderName may arrive as "/rec_2026-10-15T1430" or "rec_2026-10-15T1430";
  // normalise to the bare name.
  const char *name = folderName;
  for (const char *p = folderName; *p; p++) {
    if (*p == '/') name = p + 1;
  }

  int n = snprintf(out, outLen, "%02x%02x_%s", mac[4], mac[5], name);
  if (n < 0) { out[0] = '\0'; return 0; }
  return strlen(out);
}

size_t syncFolderToIso(const char *folderName, char *out, size_t outLen) {
  if (!out || outLen == 0) return 0;
  out[0] = '\0';
  if (!folderName || !*folderName) return 0;

  const char *name = folderName;
  for (const char *p = folderName; *p; p++) {
    if (*p == '/') name = p + 1;
  }

  // Expect: rec_YYYY-MM-DDTHHMM  -> "YYYY-MM-DDTHH:MM:00"
  // We deliberately emit LOCAL time with no trailing Z.  The device clock is
  // configured to Europe/London (syncTimeBlocking sets TZ=GMT0BST), so the
  // wall-clock time is the time the lecture actually happened; labelling it
  // as UTC with a Z would shift every session by an hour across the DST
  // boundary.  The server stores started_at as text and does not re-parse
  // it, so a naive local ISO string round-trips unchanged.
  int Y, Mo, D, H, Mi;
  if (sscanf(name, "rec_%4d-%2d-%2dT%2d%2d", &Y, &Mo, &D, &H, &Mi) != 5) {
    // Fall back to "now" rather than sending garbage.
    time_t now = time(nullptr);
    struct tm tmv;
    localtime_r(&now, &tmv);
    int n = snprintf(out, outLen, "%04d-%02d-%02dT%02d:%02d:%02d",
                     tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday,
                     tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
    return (n < 0) ? 0 : strlen(out);
  }
  int n = snprintf(out, outLen, "%04d-%02d-%02dT%02d:%02d:00", Y, Mo, D, H, Mi);
  return (n < 0) ? 0 : strlen(out);
}

// ---------------------------------------------------------------------------
//  1. Announce  --  POST /api/v1/sessions
// ---------------------------------------------------------------------------
bool syncAnnounce(const char *sessionId, const char *title,
                  int expectedCount, const char *startedAtIso) {
  if (g_syncAbort) return false;

  HTTPClient http;
  http.setTimeout(SYNC_HTTP_TIMEOUT_MS);

  char url[96];
  snprintf(url, sizeof(url), "http://%s:%d/api/v1/sessions",
           SERVER_HOST, SERVER_PORT);
  if (!http.begin(url)) {
    Serial.printf("[SYNC] announce: begin() failed for %s\n", url);
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  addAuthHeader(http);

  // The audio format we actually produced: AAC-LC mono 22050 Hz.  The
  // server stores these as provenance metadata; bits_per_sample is 16
  // because that is the PCM width the encoder consumed.
  char body[320];
  int n = snprintf(body, sizeof(body),
      "{\"id\":\"%s\",\"title\":\"%s\",\"expected_count\":%d,"
      "\"sample_rate\":%d,\"channels\":%d,\"bits_per_sample\":%d,"
      "\"started_at\":\"%s\"}",
      sessionId, title ? title : "", expectedCount,
      AAC_SAMPLE_RATE, AAC_CHANNELS, 16, startedAtIso ? startedAtIso : "");
  if (n < 0 || (size_t)n >= sizeof(body)) {
    http.end();
    Serial.println("[SYNC] announce: body too long");
    return false;
  }

  int code = http.POST(body);
  String resp = (code > 0) ? http.getString() : String("");

  if (code == 201) {
    Serial.printf("[SYNC] announce %s -> 201 created (expect %d chunks)\n",
                  sessionId, expectedCount);
    http.end();
    return true;
  }
  if (code == 200) {
    // Idempotent re-announce: a previous POST succeeded but the response was
    // lost, or we are retrying after a failed upload.  Proceed either way.
    Serial.printf("[SYNC] announce %s -> 200 already known\n", sessionId);
    http.end();
    return true;
  }
  Serial.printf("[SYNC] announce %s FAILED http=%d %s\n",
                sessionId, code, resp.c_str());
  http.end();
  return false;
}

// ---------------------------------------------------------------------------
//  2. Upload one chunk  --  PUT /api/v1/sessions/<id>/chunks/<N>
// ---------------------------------------------------------------------------
bool syncPutChunk(const char *sessionId, int chunkIndex, const char *filePath) {
  if (g_syncAbort) return false;

  File f = SD.open(filePath, FILE_READ);
  if (!f) {
    Serial.printf("[SYNC] chunk %d: cannot open %s\n", chunkIndex, filePath);
    return false;
  }
  size_t size = f.size();
  if (size == 0) {
    Serial.printf("[SYNC] chunk %d: %s is empty, skipping\n",
                  chunkIndex, filePath);
    f.close();
    return false;
  }

  HTTPClient http;
  http.setTimeout(SYNC_HTTP_TIMEOUT_MS);

  char url[160];
  snprintf(url, sizeof(url), "http://%s:%d/api/v1/sessions/%s/chunks/%d",
           SERVER_HOST, SERVER_PORT, sessionId, chunkIndex);
  if (!http.begin(url)) {
    Serial.printf("[SYNC] chunk %d: begin() failed\n", chunkIndex);
    f.close();
    return false;
  }
  http.addHeader("Content-Type", "audio/aac");
  addAuthHeader(http);

  // Stream the file straight off the SD card -- sendRequest with a File*
  // reads and writes in bounded chunks internally, so a 2.4 MB chunk never
  // sits in RAM.  This is the whole reason we do not build a String body.
  unsigned long t0 = millis();
  int code = http.sendRequest("PUT", &f, size);

  if (code == 200) {
    Serial.printf("[SYNC] chunk %d: %u B in %u s\n",
                  chunkIndex, (unsigned)size,
                  (unsigned)((millis() - t0) / 1000));
    http.end();
    f.close();
    return true;
  }

  String resp = (code > 0) ? http.getString() : String("");
  Serial.printf("[SYNC] chunk %d FAILED http=%d %s\n",
                chunkIndex, code, resp.c_str());
  http.end();
  f.close();
  return false;
}

// ---------------------------------------------------------------------------
//  3. Finish  --  POST /api/v1/sessions/<id>/finish
// ---------------------------------------------------------------------------
SyncFinishResult syncFinish(const char *sessionId,
                            int *missingOut, int missingMax) {
  if (g_syncAbort) return SYNC_FINISH_ABORTED;

  HTTPClient http;
  http.setTimeout(SYNC_HTTP_TIMEOUT_MS);

  char url[128];
  snprintf(url, sizeof(url), "http://%s:%d/api/v1/sessions/%s/finish",
           SERVER_HOST, SERVER_PORT, sessionId);
  if (!http.begin(url)) {
    Serial.printf("[SYNC] finish: begin() failed\n");
    return SYNC_FINISH_TRANSPORT_ERROR;
  }
  addAuthHeader(http);

  int code = http.POST("");           // empty body, per the contract
  String resp = (code > 0) ? http.getString() : String("");
  http.end();

  if (code == 200) {
    String reason = extractReason(resp);
    if (reason == "already_ready") {
      // Covers "our previous finish timed out" and "server restarted".
      Serial.printf("[SYNC] finish %s -> 200 already_ready (safe to delete)\n",
                    sessionId);
      return SYNC_FINISH_ALREADY_READY;
    }
    Serial.printf("[SYNC] finish %s -> 200 %s (safe to delete)\n",
                  sessionId, reason.length() ? reason.c_str() : "stitched");
    return SYNC_FINISH_STITCHED;
  }

  if (code == 409) {
    String err = extractError(resp);
    if (err == "missing_chunks" && missingOut && missingMax > 0) {
      int n = 0;
      extractIntArray(resp, "missing", missingOut, missingMax, n);
      Serial.printf("[SYNC] finish %s -> 409 missing_chunks, %d reported\n",
                    sessionId, n);
    } else {
      Serial.printf("[SYNC] finish %s -> 409 %s\n",
                    sessionId, err.length() ? err.c_str() : "conflict");
    }
    return SYNC_FINISH_MISSING_CHUNKS;
  }

  if (code == 404) {
    Serial.printf("[SYNC] finish %s -> 404 unknown to server\n", sessionId);
    return SYNC_FINISH_NOT_FOUND;
  }

  if (code <= 0) {
    Serial.printf("[SYNC] finish %s transport error (%d), will retry\n",
                  sessionId, code);
    return SYNC_FINISH_TRANSPORT_ERROR;
  }

  Serial.printf("[SYNC] finish %s FAILED http=%d %s\n",
                sessionId, code, resp.c_str());
  return SYNC_FINISH_SERVER_ERROR;
}