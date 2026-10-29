// =============================================================================
//  wifi_mgr.c
// =============================================================================
#include "wifi_mgr.h"

#include <string.h>

#include "config.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"
#include "portal.h"

static const char *TAG = "wifi";

#define NVS_NS          "ai_notetaker"
#define EV_GOT_IP       BIT0
#define EV_DISCONNECTED BIT1

static EventGroupHandle_t s_ev;
static SemaphoreHandle_t  s_lock;           // guards s_nets / s_count
static wifi_network_t     s_nets[MAX_SAVED_NETWORKS];
static int                s_count;
static volatile bool      s_recording;
static void             (*s_on_connected)(void);

// -----------------------------------------------------------------------------
//  Saved networks (NVS)
// -----------------------------------------------------------------------------
static void nets_load(void) {
    s_count = 0;
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return;
    int32_t count = 0;
    nvs_get_i32(h, "count", &count);
    if (count < 0) count = 0;
    if (count > MAX_SAVED_NETWORKS) count = MAX_SAVED_NETWORKS;
    for (int i = 0; i < count; i++) {
        char key[16];
        size_t len;
        wifi_network_t n = {0};
        snprintf(key, sizeof(key), "ssid_%d", i);
        len = sizeof(n.ssid);
        if (nvs_get_str(h, key, n.ssid, &len) != ESP_OK || !n.ssid[0]) continue;
        snprintf(key, sizeof(key), "pass_%d", i);
        len = sizeof(n.pass);
        nvs_get_str(h, key, n.pass, &len);
        s_nets[s_count++] = n;
    }
    nvs_close(h);
}

static void nets_save(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_i32(h, "count", s_count);
    for (int i = 0; i < MAX_SAVED_NETWORKS; i++) {
        char sk[16], pk[16];
        snprintf(sk, sizeof(sk), "ssid_%d", i);
        snprintf(pk, sizeof(pk), "pass_%d", i);
        if (i < s_count) {
            nvs_set_str(h, sk, s_nets[i].ssid);
            nvs_set_str(h, pk, s_nets[i].pass);
        } else {
            nvs_erase_key(h, sk);
            nvs_erase_key(h, pk);
        }
    }
    nvs_commit(h);
    nvs_close(h);
}

int wifi_mgr_get_networks(wifi_network_t *out, int max) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    int n = s_count < max ? s_count : max;
    memcpy(out, s_nets, n * sizeof(wifi_network_t));
    xSemaphoreGive(s_lock);
    return n;
}

// Newest first.  An existing SSID moves to the front with the new password;
// when the list is full the newest of the older entries is evicted, so the
// longest-standing network (typically home) survives.
void wifi_mgr_add_network(const char *ssid, const char *pass) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    wifi_network_t n = {0};
    strlcpy(n.ssid, ssid, sizeof(n.ssid));
    strlcpy(n.pass, pass, sizeof(n.pass));
    int existing = -1;
    for (int i = 0; i < s_count; i++) {
        if (strcmp(s_nets[i].ssid, ssid) == 0) { existing = i; break; }
    }
    int shift_from;
    if (existing >= 0) {
        shift_from = existing;
    } else {
        if (s_count >= MAX_SAVED_NETWORKS) s_count = MAX_SAVED_NETWORKS - 1;
        shift_from = s_count++;
    }
    for (int i = shift_from; i > 0; i--) s_nets[i] = s_nets[i - 1];
    s_nets[0] = n;
    nets_save();
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "saved network '%s'", ssid);
}

void wifi_mgr_remove_network(int index) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (index >= 0 && index < s_count) {
        ESP_LOGI(TAG, "removing network '%s'", s_nets[index].ssid);
        for (int i = index; i < s_count - 1; i++) s_nets[i] = s_nets[i + 1];
        s_count--;
        nets_save();
    }
    xSemaphoreGive(s_lock);
}

// -----------------------------------------------------------------------------
//  Connection
// -----------------------------------------------------------------------------
static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_ev, EV_GOT_IP);
        xEventGroupSetBits(s_ev, EV_DISCONNECTED);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "connected, IP " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupClearBits(s_ev, EV_DISCONNECTED);
        xEventGroupSetBits(s_ev, EV_GOT_IP);
        if (s_on_connected) s_on_connected();
    }
}

static bool try_network(const wifi_network_t *n) {
    ESP_LOGI(TAG, "trying '%s'", n->ssid);
    wifi_config_t cfg = {0};
    strlcpy((char *)cfg.sta.ssid, n->ssid, sizeof(cfg.sta.ssid));
    strlcpy((char *)cfg.sta.password, n->pass, sizeof(cfg.sta.password));
    cfg.sta.threshold.authmode = n->pass[0] ? WIFI_AUTH_WPA_PSK : WIFI_AUTH_OPEN;

    esp_wifi_disconnect();
    xEventGroupClearBits(s_ev, EV_GOT_IP | EV_DISCONNECTED);
    if (esp_wifi_set_config(WIFI_IF_STA, &cfg) != ESP_OK) return false;
    if (esp_wifi_connect() != ESP_OK) return false;
    EventBits_t b = xEventGroupWaitBits(s_ev, EV_GOT_IP, pdFALSE, pdFALSE,
                                        pdMS_TO_TICKS(WIFI_TRY_MS));
    return (b & EV_GOT_IP) != 0;
}

static void wifi_task(void *arg) {
    bool force_portal = (bool)(intptr_t)arg;
    if (force_portal || s_count == 0) {
        ESP_LOGI(TAG, "setup portal (%s)",
                 force_portal ? "button held at boot" : "no saved networks");
        portal_run(AP_PORTAL_TIMEOUT_S);
    }

    for (;;) {
        if (xEventGroupGetBits(s_ev) & EV_GOT_IP) {
            // Connected: sleep until the link drops, then start a round.
            xEventGroupWaitBits(s_ev, EV_DISCONNECTED, pdFALSE, pdFALSE, portMAX_DELAY);
            ESP_LOGW(TAG, "link lost");
            continue;
        }
        if (s_recording || s_count == 0) {
            vTaskDelay(pdMS_TO_TICKS(5000));
            continue;
        }

        wifi_network_t nets[MAX_SAVED_NETWORKS];
        int n = wifi_mgr_get_networks(nets, MAX_SAVED_NETWORKS);
        bool ok = false;
        for (int i = 0; i < n && !ok && !s_recording; i++) ok = try_network(&nets[i]);
        if (!ok) {
            esp_wifi_disconnect();
            ESP_LOGI(TAG, "no saved network in range; retrying in %d s",
                     WIFI_RETRY_INTERVAL_MS / 1000);
            vTaskDelay(pdMS_TO_TICKS(WIFI_RETRY_INTERVAL_MS));
        }
    }
}

void wifi_mgr_start(bool force_portal) {
    s_ev = xEventGroupCreate();
    s_lock = xSemaphoreCreateMutex();
    nets_load();
    ESP_LOGI(TAG, "%d saved network(s)", s_count);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *sta = esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap();
    esp_netif_set_hostname(sta, "ai-notetaker");

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));   // our NVS list is the truth
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED,
                                               on_wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               on_wifi_event, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());

    xTaskCreatePinnedToCore(wifi_task, "wifi_mgr", 6144, (void *)(intptr_t)force_portal,
                            4, NULL, 0);
}

bool wifi_mgr_connected(void) {
    return s_ev && (xEventGroupGetBits(s_ev) & EV_GOT_IP);
}

bool wifi_mgr_wait_connected(uint32_t timeout_ms) {
    return xEventGroupWaitBits(s_ev, EV_GOT_IP, pdFALSE, pdFALSE,
                               pdMS_TO_TICKS(timeout_ms)) & EV_GOT_IP;
}

void wifi_mgr_set_recording(bool recording) { s_recording = recording; }
void wifi_mgr_on_connected(void (*cb)(void)) { s_on_connected = cb; }
