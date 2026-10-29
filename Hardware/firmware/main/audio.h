// =============================================================================
//  audio.h  --  microphone -> AAC chunk files, encoded live.
//
//  Two tasks on core 1:
//    capture  (highest priority): I2S read -> gain -> 16-bit -> ring buffer
//    writer   : ring buffer -> AAC-LC encoder -> <folder>/<N>.aac on the SD
//  The ring buffer (PSRAM, ~11 s) absorbs SD card stalls.  Control commands
//  travel through the same ring as the audio, so a pause or stop lands
//  exactly between two frames.
//
//  Within a recording segment (start/resume .. pause/stop) one encoder
//  instance runs across chunk rotations, so chunks concatenate gaplessly.
// =============================================================================
#pragma once

#include <stdbool.h>
#include "esp_err.h"

// I2S, ring buffer, encoder self-test, tasks.  Call once at boot.
esp_err_t audio_init(void);

// Start a session writing chunk 1 into `folder` (absolute path).
void audio_start(const char *folder);
void audio_pause(void);
void audio_resume(void);   // continues with the next chunk number
// Stop the session; blocks until the last chunk is flushed and closed.
void audio_stop(void);

// True after an SD write/open failure during the current session.
bool audio_has_error(void);
