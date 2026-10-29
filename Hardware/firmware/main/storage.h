// =============================================================================
//  storage.h  --  SD card (SPI) mounted as FAT at SD_MOUNT, plus helpers.
// =============================================================================
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

// Mount the card, trying 20 MHz, then 4 MHz, then 1 MHz.
esp_err_t storage_mount(void);

// Power-loss repair: in each session folder, trim the newest chunk back to
// its last complete ADTS frame (a cut-off write leaves a partial frame at
// the end) and delete it if nothing complete remains.
void storage_recover(void);

bool storage_exists(const char *path);

// "<digits><ext>" -> the number, or 0 when the name does not fit (so
// "3.aac.tmp" and "start.txt" are not chunks).
uint16_t storage_chunk_number(const char *name, const char *ext);
