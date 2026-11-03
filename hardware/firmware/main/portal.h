// =============================================================================
//  portal.h  --  WiFi setup portal (AP "AI_NoteTaker-Setup" + captive pages).
// =============================================================================
#pragma once

#include <stdint.h>

// Blocking: runs the AP, DNS catch-all and web pages until a network is
// saved or `timeout_s` passes, then returns to plain station mode.
void portal_run(uint32_t timeout_s);
