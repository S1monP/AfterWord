// =============================================================================
//  wifi_mgr.h  --  saved networks, background (re)connect, setup portal.
//
//  Networks are stored in NVS exactly as the Arduino firmware stored them
//  (namespace "ai_notetaker", keys count / ssid_N / pass_N), so a device
//  that was set up under Arduino keeps its networks.
// =============================================================================
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    char ssid[33];
    char pass[65];
} wifi_network_t;

// Bring up netif/WiFi and start the WiFi task.  Opens the setup portal
// first when no networks are saved or `force_portal` is set.  Non-blocking.
void wifi_mgr_start(bool force_portal);

bool wifi_mgr_connected(void);
bool wifi_mgr_wait_connected(uint32_t timeout_ms);

// While recording, no new connection rounds are started (saves battery;
// nothing is uploaded during a recording anyway).  An existing link stays.
void wifi_mgr_set_recording(bool recording);

// Called (from the WiFi event task) each time an IP address is obtained.
void wifi_mgr_on_connected(void (*cb)(void));

// Saved-network list, most recent first.  Used by the portal.
int  wifi_mgr_get_networks(wifi_network_t *out, int max);
void wifi_mgr_add_network(const char *ssid, const char *pass);
void wifi_mgr_remove_network(int index);
