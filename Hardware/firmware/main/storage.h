// =============================================================================
//  storage.h  --  SD card (SPI) mounted as FAT at SD_MOUNT, plus helpers.
// =============================================================================
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

// Mount the card, trying 20 MHz, then 4 MHz, then 1 MHz.
esp_err_t storage_mount(void);

// Unmount and mount again: a card that stopped answering (seen when WiFi
// transmits hard on the breadboard supply) needs a fresh init sequence.
// No file may be open; callers make sure audio and sync are idle.
esp_err_t storage_remount(void);

// True when the card answers a status request (CMD13).  Talks to the card
// directly, outside the file system lock: only call it with audio and sync
// idle.
bool storage_card_ok(void);

// Power-loss repair: in each session folder, trim the newest chunk back to
// its last complete ADTS frame (a cut-off write leaves a partial frame at
// the end) and delete it if nothing complete remains.
void storage_recover(void);

bool storage_exists(const char *path);

// "<digits><ext>" -> the number, or 0 when the name does not fit (so
// "3.aac.tmp" and "start.txt" are not chunks).
uint16_t storage_chunk_number(const char *name, const char *ext);
