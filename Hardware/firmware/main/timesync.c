// =============================================================================
//  timesync.c
// =============================================================================
#include "timesync.h"

#include <stdlib.h>
#include <time.h>

#include "config.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "esp_sntp.h"

static const char *TAG = "time";

void timesync_start(void) {
    // Session folders are named in local wall-clock time.
    setenv("TZ", "GMT0BST,M3.5.0/1,M10.5.0/2", 1);
    tzset();

    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG_MULTIPLE(
        2, ESP_SNTP_SERVER_LIST("pool.ntp.org", "time.google.com"));
    esp_netif_sntp_init(&cfg);
}

void timesync_on_connected(void) {
    // SNTP starts before WiFi is up, so its first request fails and lwIP
    // waits 15 s (doubling up to 150 s) before trying again.  Ask now.
    esp_sntp_restart();
}

bool time_valid(void) {
    return time(NULL) >= (time_t)TIME_VALID_AFTER;
}

bool timesync_wait(uint32_t timeout_ms) {
    if (time_valid()) return true;
    if (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(timeout_ms)) == ESP_OK || time_valid()) {
        ESP_LOGI(TAG, "clock set by NTP");
        return true;
    }
    return false;
}
