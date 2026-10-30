// =============================================================================
//  sync.h  --  uploads finished sessions to the server, then deletes them.
//
//  Protocol (Server/README.md, "ESP32 device protocol contract"):
//    POST /api/v1/sessions                    announce {id, expected_count, ...}
//    PUT  /api/v1/sessions/<id>/chunks/<k>    raw ADTS bytes, k = 1..N
//    POST /api/v1/sessions/<id>/finish        200 stitched | already_ready
//  Local files are deleted ONLY after finish answers 200.  Anything else
//  keeps them and the periodic retry is the recovery.
//
//  Runs in its own task, so the app and UI stay responsive.  Never runs
//  while recording; starting a recording aborts a sweep in progress.
// =============================================================================
#pragma once

#include <stdbool.h>
#include <stdint.h>

void sync_start(void);

// Request a sweep `delay_ms` from now (earliest request wins).
void sync_kick(uint32_t delay_ms);

// Recording on: abort any sweep and start no new ones.  Off: allow again.
void sync_set_recording(bool recording);

// True while a sweep is reading the card / uploading.
bool sync_busy(void);

// True after a card read failed during a sweep.  The sweep stops and no
// new one starts until main has checked the card and cleared it.
bool sync_sd_error(void);
void sync_clear_sd_error(void);
