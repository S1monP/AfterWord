// =============================================================================
//  timesync.h  --  SNTP + UK timezone.  Non-blocking: SNTP keeps polling in
//  the background, so the clock is set whenever the network comes up.
// =============================================================================
#pragma once

#include <stdbool.h>
#include <stdint.h>

void timesync_start(void);              // after esp_netif_init()
bool time_valid(void);
bool timesync_wait(uint32_t timeout_ms);
