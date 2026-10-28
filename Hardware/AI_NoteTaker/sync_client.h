// =============================================================================
//  sync_client.h   (V0.6)
//
//  HTTP upload/sync client -- pushes transcoded sessions to the server.
//
//  Server contract (verified against Server/README.md and Server/app.py):
//
//    POST /api/v1/sessions
//         body: {id, title, expected_count, sample_rate, channels,
//                bits_per_sample, started_at}
//         201 = created, 200 = already known (idempotent). Both proceed.
//         400 = bad id / audio format.
//
//    PUT /api/v1/sessions/<id>/chunks/<N>
//         body: raw ADTS AAC bytes, Content-Type: audio/aac
//         200 = stored, 400 = empty body,
//         404 = session never announced, 409 = wrong state (e.g. already ready)
//
//    POST /api/v1/sessions/<id>/finish
//         200 = {ok, reason: "stitched" | "already_ready", session}
//         409 = {error: "missing_chunks", missing: [3,7], present: [...]}
//         500 = stitching failed
//
//  Server-side id validation: ^[A-Za-z0-9_\-]{4,64}$  (storage.py), so our
//  deterministic ids ("a1b2_rec_2026-10-15T1430") are legal.
//
//  Design rules enforced here:
//    * Nothing is deleted by this module. The caller decides what to remove
//      and only after a confirmed `stitched` / `already_ready`.
//    * Chunks are STREAMED from the SD file (sendRequest with a File*), never
//      buffered whole -- a 5-minute chunk is ~2.4 MB and the ESP32 heap
//      cannot afford a copy.
//    * Every call is abortable via syncAbort() so a button press can cut a
//      long sync short without leaving half-written state.
// =============================================================================

#ifndef AI_NOTETAKER_SYNC_CLIENT_H
#define AI_NOTETAKER_SYNC_CLIENT_H

#include <Arduino.h>

// Result of a finish call.  STITCHED and ALREADY_READY are both "safe to
// delete local files"; everything else means "keep them".
enum SyncFinishResult : uint8_t {
  SYNC_FINISH_STITCHED = 0,      // 200 reason=stitched
  SYNC_FINISH_ALREADY_READY,     // 200 reason=already_ready
  SYNC_FINISH_MISSING_CHUNKS,    // 409 missing_chunks (see missing[] out-params)
  SYNC_FINISH_SERVER_ERROR,      // 500
  SYNC_FINISH_NOT_FOUND,         // 404 -- session unknown to server
  SYNC_FINISH_TRANSPORT_ERROR,   // socket/timeout; retry later
  SYNC_FINISH_ABORTED            // user pressed the button; retry later
};

// True when the finish result means the server has durably stitched the
// session and it is therefore SAFE TO DELETE the local AAC files.
inline bool syncFinishMeansSafeToDelete(SyncFinishResult r) {
  return (r == SYNC_FINISH_STITCHED) || (r == SYNC_FINISH_ALREADY_READY);
}

// POST /api/v1/sessions
//   sessionId    deterministic id, must match ^[A-Za-z0-9_\-]{4,64}$
//   title        human-readable label (we use the folder name)
//   expectedCount number of chunks the server should expect (1..N)
//   startedAtIso ISO-8601 timestamp string (no trailing Z -- we emit local
//                time, matching the device's BST-configured clock)
// Returns true when the server accepted the announce (HTTP 200 or 201).
bool syncAnnounce(const char *sessionId, const char *title,
                  int expectedCount, const char *startedAtIso);

// PUT /api/v1/sessions/<id>/chunks/<index> streaming the whole file.
//   chunkIndex is 1-based (the server validates 1..expected_count).
//   filePath    absolute path to the .aac on the SD card.
// Returns true on HTTP 200.  404 / 409 are returned as false so the caller
// can re-announce and retry once.
bool syncPutChunk(const char *sessionId, int chunkIndex, const char *filePath);

// POST /api/v1/sessions/<id>/finish
//   missingOut / missingMax  optional buffer; when the result is
//   SYNC_FINISH_MISSING_CHUNKS this receives the indices the server says
//   are absent, so the caller can re-PUT exactly those and finish again.
SyncFinishResult syncFinish(const char *sessionId,
                            int *missingOut = nullptr, int missingMax = 0);

// Cooperative abort.  Set from the long-press handler; every blocking loop
// in this module checks it and bails out promptly.  Clear with
// syncClearAbort() at the start of a sweep.
void syncAbort();
void syncClearAbort();
bool syncAborted();

// Called repeatedly while a chunk PUT is streaming (every ~1.4 KB read), so
// the caller can keep LEDs updated and poll the button -- typically calling
// syncAbort() on a long press.  Must not call back into this module.
void syncSetUiHook(void (*fn)());

// Build a deterministic session id from the 4-hex efuse MAC prefix and a
// folder name, e.g. "a1b2_rec_2026-10-15T1430".  Returns bytes written.
size_t syncBuildSessionId(const char *folderName, char *out, size_t outLen);

// Convert a session folder name ("rec_2026-10-15T1430", optionally with a
// "_2" suffix) into an ISO-8601 local timestamp ("2026-10-15T14:30:00").
// Returns bytes written; 0 (and "") for names without a timestamp.
size_t syncFolderToIso(const char *folderName, char *out, size_t outLen);

#endif  // AI_NOTETAKER_SYNC_CLIENT_H