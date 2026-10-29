// =============================================================================
//  storage.c  --  SD card over SPI + power-loss recovery of AAC chunks.
// =============================================================================
#include "storage.h"

#include <dirent.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "config.h"
#include "driver/sdspi_host.h"
#include "driver/spi_common.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"

static const char *TAG = "storage";

static sdmmc_card_t *s_card;

esp_err_t storage_mount(void) {
    spi_bus_config_t bus = {
        .mosi_io_num = PIN_SD_MOSI,
        .miso_io_num = PIN_SD_MISO,
        .sclk_io_num = PIN_SD_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 16 * 1024,
    };
    esp_err_t err = spi_bus_initialize(SPI2_HOST, &bus, SDSPI_DEFAULT_DMA);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPI bus init failed: %s", esp_err_to_name(err));
        return err;
    }

    sdspi_device_config_t slot = SDSPI_DEVICE_CONFIG_DEFAULT();
    slot.gpio_cs = PIN_SD_CS;
    slot.host_id = SPI2_HOST;

    esp_vfs_fat_sdmmc_mount_config_t mount = {
        .format_if_mount_failed = false,
        .max_files = 8,
        .allocation_unit_size = 16 * 1024,
    };

    // Some cheap cards flake at 20 MHz; fall back rather than fail boot.
    static const int freqs_khz[] = {20000, 4000, 1000};
    for (size_t i = 0; i < sizeof(freqs_khz) / sizeof(freqs_khz[0]); i++) {
        sdmmc_host_t host = SDSPI_HOST_DEFAULT();
        host.slot = SPI2_HOST;
        host.max_freq_khz = freqs_khz[i];
        err = esp_vfs_fat_sdspi_mount(SD_MOUNT, &host, &slot, &mount, &s_card);
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "SD mounted at %d kHz, %llu MB", freqs_khz[i],
                     ((uint64_t)s_card->csd.capacity) * s_card->csd.sector_size
                         / (1024 * 1024));
            return ESP_OK;
        }
        ESP_LOGW(TAG, "mount at %d kHz failed: %s", freqs_khz[i], esp_err_to_name(err));
    }
    return err;
}

bool storage_exists(const char *path) {
    struct stat st;
    return stat(path, &st) == 0;
}

uint16_t storage_chunk_number(const char *name, const char *ext) {
    size_t nlen = strlen(name), elen = strlen(ext);
    if (nlen <= elen || strcasecmp(name + nlen - elen, ext) != 0) return 0;
    uint32_t v = 0;
    for (size_t i = 0; i < nlen - elen; i++) {
        if (name[i] < '0' || name[i] > '9') return 0;
        v = v * 10 + (uint32_t)(name[i] - '0');
        if (v > 0xFFFF) return 0;
    }
    return (uint16_t)v;
}

// Length of the leading run of complete ADTS frames in the file.
static long adts_complete_length(FILE *f, long size) {
    long pos = 0;
    uint8_t h[7];
    while (pos + 7 <= size) {
        if (fseek(f, pos, SEEK_SET) != 0 || fread(h, 1, 7, f) != 7) break;
        if (h[0] != 0xFF || (h[1] & 0xF6) != 0xF0) break;     // lost sync
        long len = ((long)(h[3] & 0x03) << 11) | ((long)h[4] << 3) | (h[5] >> 5);
        if (len < 7 || pos + len > size) break;               // cut-off frame
        pos += len;
    }
    return pos;
}

static void recover_folder(const char *folder) {
    DIR *d = opendir(folder);
    if (!d) return;
    uint16_t newest = 0;
    bool has_wav = false;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        uint16_t n = storage_chunk_number(e->d_name, ".aac");
        if (n > newest) newest = n;
        if (storage_chunk_number(e->d_name, ".wav")) has_wav = true;
    }
    closedir(d);

    if (has_wav) {
        // Left over from the Arduino firmware (WAV chunks).  This firmware
        // does not convert them; copy them off and upload via the web UI.
        ESP_LOGW(TAG, "%s holds legacy .wav chunks; it will not be synced", folder);
    }
    if (newest == 0) return;

    // Only the newest chunk can have been open when power was lost.
    char path[300];
    snprintf(path, sizeof(path), "%s/%u.aac", folder, (unsigned)newest);
    struct stat st;
    if (stat(path, &st) != 0) return;
    FILE *f = fopen(path, "rb");
    if (!f) return;
    long good = adts_complete_length(f, (long)st.st_size);
    fclose(f);

    if (good == (long)st.st_size) return;
    if (good == 0) {
        ESP_LOGW(TAG, "%s: no complete frame, removing", path);
        unlink(path);
    } else {
        ESP_LOGW(TAG, "%s: trimming %ld -> %ld bytes (cut off by power loss)",
                 path, (long)st.st_size, good);
        if (truncate(path, good) != 0) {
            ESP_LOGE(TAG, "%s: truncate failed", path);
        }
    }
}

void storage_recover(void) {
    DIR *root = opendir(SD_MOUNT);
    if (!root) {
        ESP_LOGE(TAG, "cannot open %s", SD_MOUNT);
        return;
    }
    struct dirent *e;
    char folder[300];
    while ((e = readdir(root)) != NULL) {
        if (e->d_type != DT_DIR || strncmp(e->d_name, "rec_", 4) != 0) continue;
        snprintf(folder, sizeof(folder), "%s/%s", SD_MOUNT, e->d_name);
        recover_folder(folder);
    }
    closedir(root);
}
