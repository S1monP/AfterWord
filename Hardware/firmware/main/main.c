// =============================================================================
//  AI NoteTaker  --  lecture recorder firmware (ESP-IDF, ESP32-S3 N16R8)
//
//  Record -> encode AAC live -> 5-minute chunks on SD -> upload to the
//  server when on WiFi -> delete only after the server confirms.
//
//  Button:
//    IDLE       long press   start a session (aborts an upload in progress)
//    RECORDING  short press  pause          long press  end session
//    PAUSED     short press  resume         long press  end session
//    ERROR      long press   restart
//    power-on   hold         open the WiFi setup portal
//
//  LED: blue blink = booting / uploading, solid blue = setup portal,
//  solid green = idle, red blink = recording, orange blink = paused,
//  magenta = error, green flash = session uploaded.
//
//  Recording never needs WiFi.  Without NTP time a session is named
//  rec_nt<boot>_<n> instead of rec_YYYY-MM-DDTHHMM.
// =============================================================================
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#include "app.h"
#include "audio.h"
#include "config.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "storage.h"
#include "sync.h"
#include "timesync.h"
#include "ui.h"
#include "wifi_mgr.h"

static const char *TAG = "app";

typedef enum { ST_IDLE, ST_RECORDING, ST_PAUSED, ST_ERROR } app_state_t;

static app_state_t s_state = ST_IDLE;
static uint32_t    s_boot_id;
static uint16_t    s_untimed_seq;
static char        s_folder[64];
static int64_t     s_segment_start_us;      // for the MAX_RECORDING_MS auto-stop

uint32_t app_boot_id(void) { return s_boot_id; }

static uint32_t next_boot_id(void) {
    // Same NVS layout the Arduino firmware used ("ai_nt_sys" / "boots").
    nvs_handle_t h;
    uint32_t id = 0;
    if (nvs_open("ai_nt_sys", NVS_READWRITE, &h) == ESP_OK) {
        nvs_get_u32(h, "boots", &id);
        id++;
        nvs_set_u32(h, "boots", id);
        nvs_commit(h);
        nvs_close(h);
    }
    return id;
}

static void enter_error(const char *why) {
    ESP_LOGE(TAG, "ERROR: %s (long press to restart)", why);
    s_state = ST_ERROR;
    ui_set_mode(UI_ERROR);
}

// -----------------------------------------------------------------------------
//  Sessions
// -----------------------------------------------------------------------------
static bool make_session_folder(void) {
    char base[48];
    if (time_valid()) {
        time_t now = time(NULL);
        struct tm t;
        localtime_r(&now, &t);
        strftime(base, sizeof(base), SD_MOUNT "/rec_%Y-%m-%dT%H%M", &t);
    } else {
        snprintf(base, sizeof(base), SD_MOUNT "/rec_nt%lu_%u",
                 (unsigned long)s_boot_id, (unsigned)++s_untimed_seq);
        ESP_LOGW(TAG, "clock not set yet -- untimed session");
    }
    // A second session in the same minute gets _2, _3, ...
    snprintf(s_folder, sizeof(s_folder), "%s", base);
    for (int n = 2; storage_exists(s_folder) && n <= 9; n++) {
        snprintf(s_folder, sizeof(s_folder), "%s_%d", base, n);
    }
    if (storage_exists(s_folder) || mkdir(s_folder, 0777) != 0) {
        ESP_LOGE(TAG, "cannot create %s", s_folder);
        return false;
    }
    char p[96];
    snprintf(p, sizeof(p), "%s/" SESSION_START_FILE, s_folder);
    FILE *f = fopen(p, "w");
    if (f) {
        fprintf(f, "%lu %lld\n", (unsigned long)s_boot_id, esp_timer_get_time());
        fclose(f);
    }
    return true;
}

static void session_start(void) {
    sync_set_recording(true);                   // abort any upload, keep SD/radio free
    wifi_mgr_set_recording(true);
    if (!make_session_folder()) {
        sync_set_recording(false);
        wifi_mgr_set_recording(false);
        enter_error("cannot create session folder");
        return;
    }
    ESP_LOGI(TAG, "session start: %s", s_folder);
    audio_start(s_folder);
    s_segment_start_us = esp_timer_get_time();
    s_state = ST_RECORDING;
    ui_set_mode(UI_RECORDING);
}

static void session_end(void) {
    ESP_LOGI(TAG, "session end: %s", s_folder);
    audio_stop();                               // blocks until the last chunk is closed
    s_folder[0] = '\0';
    s_state = ST_IDLE;
    ui_set_mode(UI_IDLE);
    wifi_mgr_set_recording(false);
    sync_set_recording(false);
    sync_kick(0);                               // upload straight away if online
}

static void handle_button(btn_event_t ev) {
    switch (s_state) {
    case ST_IDLE:
        if (ev == BTN_LONG) session_start();
        break;
    case ST_RECORDING:
        if (ev == BTN_SHORT) {
            ESP_LOGI(TAG, "pause");
            audio_pause();
            s_state = ST_PAUSED;
            ui_set_mode(UI_PAUSED);
        } else {
            session_end();
        }
        break;
    case ST_PAUSED:
        if (ev == BTN_SHORT) {
            ESP_LOGI(TAG, "resume");
            audio_resume();
            s_segment_start_us = esp_timer_get_time();
            s_state = ST_RECORDING;
            ui_set_mode(UI_RECORDING);
        } else {
            session_end();
        }
        break;
    case ST_ERROR:
        if (ev == BTN_LONG) {
            // BTN_LONG arrives while the button is still held; restarting
            // now would read the hold as the setup-portal gesture.
            while (ui_button_down()) vTaskDelay(pdMS_TO_TICKS(20));
            vTaskDelay(pdMS_TO_TICKS(100));
            esp_restart();
        }
        break;
    }
}

static void on_wifi_connected(void) {
    sync_kick(WIFI_UP_SYNC_DELAY_MS);           // give NTP a moment first
}

// -----------------------------------------------------------------------------
//  Boot + main loop
// -----------------------------------------------------------------------------
void app_main(void) {
    ESP_LOGI(TAG, "AI NoteTaker " FW_VERSION " -- server %s, token %s",
             SERVER_URL, DEVICE_TOKEN[0] ? "set" : "none");

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        // Partition unusable as-is (layout change); this loses saved WiFi.
        ESP_LOGW(TAG, "NVS needs erasing (%s)", esp_err_to_name(err));
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    s_boot_id = next_boot_id();
    ESP_LOGI(TAG, "boot #%lu", (unsigned long)s_boot_id);

    ui_init();
    ui_set_mode(UI_BOOT);
    bool force_portal = ui_button_held_at_boot();

    bool ok = true;
    if (storage_mount() != ESP_OK) {
        enter_error("SD card");
        ok = false;
    } else {
        storage_recover();
        if (audio_init() != ESP_OK) {
            enter_error("microphone / encoder");
            ok = false;
        }
    }

    wifi_mgr_on_connected(on_wifi_connected);
    wifi_mgr_start(force_portal);
    timesync_start();
    if (ok) sync_start();

    // Give a known network a moment so the first session gets a real
    // timestamp, but never block recording on it.
    if (ok && !force_portal && wifi_mgr_wait_connected(WIFI_BOOT_WAIT_MS)) {
        if (!timesync_wait(NTP_BOOT_WAIT_MS)) {
            ESP_LOGW(TAG, "NTP not answering (no internet?) -- continuing");
        }
    }
    if (ok) {
        ui_set_mode(UI_IDLE);
        ui_flash_boot_done();
        ESP_LOGI(TAG, "ready%s", wifi_mgr_connected() ? "" : " (offline)");
    }

    QueueHandle_t buttons = ui_button_queue();
    for (;;) {
        btn_event_t ev;
        if (xQueueReceive(buttons, &ev, pdMS_TO_TICKS(250)) == pdTRUE) {
            handle_button(ev);
        }
        if (s_state == ST_RECORDING || s_state == ST_PAUSED) {
            if (audio_has_error()) {
                audio_stop();
                wifi_mgr_set_recording(false);
                sync_set_recording(false);
                enter_error("SD write failed while recording");
            } else if (s_state == ST_RECORDING && MAX_RECORDING_MS > 0 &&
                       esp_timer_get_time() - s_segment_start_us
                           >= (int64_t)MAX_RECORDING_MS * 1000) {
                ESP_LOGW(TAG, "max continuous recording time reached");
                session_end();
            }
        }
    }
}
