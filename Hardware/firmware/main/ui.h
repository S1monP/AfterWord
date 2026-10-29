// =============================================================================
//  ui.h  --  status LED + button, serviced by their own task.
//
//  The UI task never blocks on anything else, so the LED keeps blinking and
//  button presses are caught even while the app is busy (WiFi, uploads).
//  This is also where the display and battery readout will live.
// =============================================================================
#pragma once

#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

typedef enum {
    UI_BOOT,        // blue slow blink
    UI_IDLE,        // solid green (blue slow blink while syncing, solid
                    // blue while the setup portal is up)
    UI_RECORDING,   // red 250 ms on / 750 ms off
    UI_PAUSED,      // orange slow blink
    UI_ERROR,       // solid magenta
} ui_mode_t;

typedef enum {
    BTN_SHORT,      // released within 1 s
    BTN_LONG,       // held >= 1 s, then released
} btn_event_t;

// Initialise LED + button and start the UI task.  Samples the button first,
// so ui_button_held_at_boot() reflects its state at power-on.
void ui_init(void);

bool ui_button_held_at_boot(void);

// Queue of btn_event_t.  A press that was already held at boot is swallowed.
QueueHandle_t ui_button_queue(void);

void ui_set_mode(ui_mode_t mode);
// While true and the mode is UI_IDLE, show the syncing pattern instead.
void ui_set_syncing(bool syncing);
// While true and the mode is UI_BOOT or UI_IDLE, show solid blue.
void ui_set_portal(bool active);
// Transient overlays drawn on top of the current mode.
void ui_flash_boot_done(void);   // fast green blink, 2 s
void ui_flash_sync_ok(void);     // solid green, 600 ms
