// =============================================================================
//  app.h  --  bits of app state other modules need.
// =============================================================================
#pragma once

#include <stdint.h>

// Persistent boot counter (NVS).  Names sessions recorded before the clock
// is set, and tells whether a start.txt was written during this boot.
uint32_t app_boot_id(void);
