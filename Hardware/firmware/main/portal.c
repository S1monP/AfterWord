// =============================================================================
//  portal.c  --  captive setup portal, pages ported from the Arduino V0.4.
// =============================================================================
#include "portal.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "config.h"
#include "dns_server.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ui.h"
#include "wifi_mgr.h"

static const char *TAG = "portal";

static volatile bool s_saved;

// SSIDs seen by the last scan on the add page, to warn when a typed-in one
// is not among them.
#define SEEN_MAX 20
static char s_seen[SEEN_MAX][33];
static int  s_seen_n;

static const char PAGE_HEAD[] =
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>AI NoteTaker</title><style>"
    "body{font-family:sans-serif;margin:1em auto;max-width:480px;padding:0 1em;color:#222}"
    "h1{font-size:1.4em}"
    "button,input,select{font-size:1em;padding:0.5em;margin:0.3em 0;width:100%;box-sizing:border-box}"
    "button{background:#0a7;color:#fff;border:none;border-radius:0.3em;cursor:pointer}"
    "a.btn{display:block;text-align:center;background:#0a7;color:#fff;border-radius:0.3em;"
    "padding:0.7em;margin:0.5em 0;text-decoration:none}"
    "a.back{background:#aaa}"
    ".net{border:1px solid #ddd;border-radius:0.3em;padding:0.5em;margin:0.5em 0;background:#f9f9f9}"
    ".net form{display:inline;float:right;width:auto}"
    ".net button{background:#c33;width:auto;padding:0.3em 0.8em;margin:0}"
    ".empty{color:#888;font-style:italic}"
    "</style></head><body>";
static const char PAGE_TAIL[] = "</body></html>";

// Write `s` HTML-escaped (SSIDs are attacker-controlled radio names).
static void send_escaped(httpd_req_t *req, const char *s) {
    char buf[8];
    for (; *s; s++) {
        const char *rep = NULL;
        switch (*s) {
        case '<': rep = "&lt;"; break;
        case '>': rep = "&gt;"; break;
        case '&': rep = "&amp;"; break;
        case '\'': rep = "&#39;"; break;
        case '"': rep = "&quot;"; break;
        }
        if (rep) {
            httpd_resp_sendstr_chunk(req, rep);
        } else {
            buf[0] = *s;
            buf[1] = '\0';
            httpd_resp_sendstr_chunk(req, buf);
        }
    }
}

static esp_err_t page_root(httpd_req_t *req) {
    httpd_resp_sendstr_chunk(req, PAGE_HEAD);
    httpd_resp_sendstr_chunk(req,
        "<h1>AI NoteTaker WiFi Setup</h1><p>What do you want to do?</p>"
        "<a class='btn' href='/add'>Add network</a>"
        "<a class='btn' href='/manage'>Manage networks</a>");
    httpd_resp_sendstr_chunk(req, PAGE_TAIL);
    return httpd_resp_sendstr_chunk(req, NULL);
}

static esp_err_t page_add(httpd_req_t *req) {
    httpd_resp_sendstr_chunk(req, PAGE_HEAD);
    httpd_resp_sendstr_chunk(req,
        "<h1>Add a network</h1><form method='POST' action='/save'>"
        "<select name='s' onchange=\"document.getElementById('ssid').value=this.value\">"
        "<option value=''>-- choose a visible network --</option>");

    wifi_scan_config_t scan = {.show_hidden = false};
    s_seen_n = 0;
    if (esp_wifi_scan_start(&scan, true) == ESP_OK) {
        uint16_t n = SEEN_MAX;
        wifi_ap_record_t *aps = calloc(n, sizeof(wifi_ap_record_t));
        if (aps && esp_wifi_scan_get_ap_records(&n, aps) == ESP_OK) {
            for (int i = 0; i < n; i++) {
                const char *ssid = (const char *)aps[i].ssid;
                if (!ssid[0]) continue;
                strlcpy(s_seen[s_seen_n++], ssid, sizeof(s_seen[0]));
                httpd_resp_sendstr_chunk(req, "<option value='");
                send_escaped(req, ssid);
                httpd_resp_sendstr_chunk(req, "'>");
                send_escaped(req, ssid);
                httpd_resp_sendstr_chunk(req, "</option>");
            }
        }
        free(aps);
    }

    httpd_resp_sendstr_chunk(req,
        "</select>"
        "<input id='ssid' name='s_manual' placeholder='...or type SSID manually' maxlength='32'>"
        "<input name='p' type='password' placeholder='Wi-Fi password' maxlength='63'>"
        "<button type='submit'>Save</button></form>"
        "<p class='empty'>Only 2.4 GHz networks show up: this device cannot use "
        "5 GHz. For an iPhone hotspot, turn on Maximise Compatibility.</p>"
        "<a class='btn back' href='/'>Back</a>");
    httpd_resp_sendstr_chunk(req, PAGE_TAIL);
    return httpd_resp_sendstr_chunk(req, NULL);
}

static esp_err_t page_manage(httpd_req_t *req) {
    wifi_network_t nets[MAX_SAVED_NETWORKS];
    int n = wifi_mgr_get_networks(nets, MAX_SAVED_NETWORKS);
    char line[192];

    httpd_resp_sendstr_chunk(req, PAGE_HEAD);
    snprintf(line, sizeof(line), "<h1>Saved networks (%d/%d)</h1>", n, MAX_SAVED_NETWORKS);
    httpd_resp_sendstr_chunk(req, line);
    if (n == 0) {
        httpd_resp_sendstr_chunk(req, "<p class='empty'>No networks saved yet.</p>");
    }
    for (int i = 0; i < n; i++) {
        httpd_resp_sendstr_chunk(req, "<div class='net'>");
        send_escaped(req, nets[i].ssid);
        snprintf(line, sizeof(line),
                 "<form method='POST' action='/delete'><input type='hidden' name='idx' value='%d'>"
                 "<button type='submit'>Delete</button></form></div>", i);
        httpd_resp_sendstr_chunk(req, line);
    }
    httpd_resp_sendstr_chunk(req, "<a class='btn back' href='/'>Back to home</a>");
    httpd_resp_sendstr_chunk(req, PAGE_TAIL);
    return httpd_resp_sendstr_chunk(req, NULL);
}

// application/x-www-form-urlencoded value decoding ('+' and %XX).
static void url_decode(char *s) {
    char *o = s;
    for (; *s; s++) {
        if (*s == '+') {
            *o++ = ' ';
        } else if (*s == '%' && isxdigit((unsigned char)s[1]) && isxdigit((unsigned char)s[2])) {
            char hex[3] = {s[1], s[2], 0};
            *o++ = (char)strtol(hex, NULL, 16);
            s += 2;
        } else {
            *o++ = *s;
        }
    }
    *o = '\0';
}

static bool read_form(httpd_req_t *req, char *body, size_t cap) {
    int total = req->content_len;
    if (total <= 0 || (size_t)total >= cap) return false;
    int got = 0;
    while (got < total) {
        int r = httpd_req_recv(req, body + got, total - got);
        if (r <= 0) return false;
        got += r;
    }
    body[got] = '\0';
    return true;
}

static bool form_field(const char *body, const char *key, char *out, size_t cap) {
    if (httpd_query_key_value(body, key, out, cap) != ESP_OK) {
        out[0] = '\0';
        return false;
    }
    url_decode(out);
    return true;
}

static esp_err_t page_save(httpd_req_t *req) {
    char body[512], s[100], s_manual[100], p[200];
    if (!read_form(req, body, sizeof(body))) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "bad form");
    }
    form_field(body, "s", s, sizeof(s));
    form_field(body, "s_manual", s_manual, sizeof(s_manual));
    form_field(body, "p", p, sizeof(p));
    const char *ssid = s_manual[0] ? s_manual : s;
    if (!ssid[0] || strlen(ssid) > 32 || strlen(p) > 63) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req,
            "<html><body><h1>Missing or invalid SSID</h1>"
            "<p>Pick a network from the dropdown or type one manually.</p>"
            "<a href='/add'>Back</a></body></html>");
    }
    wifi_mgr_add_network(ssid, p);
    bool seen = false;
    for (int i = 0; i < s_seen_n && !seen; i++) seen = strcmp(s_seen[i], ssid) == 0;
    httpd_resp_sendstr_chunk(req, PAGE_HEAD);
    if (seen) {
        httpd_resp_sendstr_chunk(req,
            "<h1>Saved!</h1><p>The device will now leave setup mode and connect.</p>");
    } else {
        // Typed in and not in the scan: saved anyway (it may be hidden, or
        // switched off for now), but say why it may never connect.
        httpd_resp_sendstr_chunk(req, "<h1>Saved, but not visible</h1><p>&ldquo;");
        send_escaped(req, ssid);
        httpd_resp_sendstr_chunk(req,
            "&rdquo; was not in range when this page loaded. This device only uses "
            "2.4 GHz networks: a 5 GHz network, or a phone hotspot without "
            "Maximise Compatibility, will never connect.</p>"
            "<p>The device now leaves setup mode and tries it every minute. To "
            "change it, hold the button while powering on to reopen this page.</p>");
    }
    httpd_resp_sendstr_chunk(req, PAGE_TAIL);
    httpd_resp_sendstr_chunk(req, NULL);
    s_saved = true;
    return ESP_OK;
}

static esp_err_t page_delete(httpd_req_t *req) {
    char body[64], idx[8];
    if (read_form(req, body, sizeof(body)) && form_field(body, "idx", idx, sizeof(idx))) {
        wifi_mgr_remove_network(atoi(idx));
    }
    httpd_resp_set_status(req, "303 See Other");
    httpd_resp_set_hdr(req, "Location", "/manage");
    return httpd_resp_send(req, NULL, 0);
}

// Anything else (phones probing for captive portals) -> the home page.
static esp_err_t not_found(httpd_req_t *req, httpd_err_code_t err) {
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", "http://192.168.4.1/");
    return httpd_resp_send(req, NULL, 0);
}

void portal_run(uint32_t timeout_s) {
    s_saved = false;
    ui_set_portal(true);

    wifi_config_t ap = {0};
    strlcpy((char *)ap.ap.ssid, AP_SSID, sizeof(ap.ap.ssid));
    strlcpy((char *)ap.ap.password, AP_PASSWORD, sizeof(ap.ap.password));
    ap.ap.ssid_len = strlen(AP_SSID);
    ap.ap.channel = 1;
    ap.ap.max_connection = 2;
    ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    esp_wifi_set_mode(WIFI_MODE_APSTA);     // STA side is needed for scanning
    esp_wifi_set_config(WIFI_IF_AP, &ap);

    httpd_handle_t server = NULL;
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.core_id = 0;
    cfg.stack_size = 6144;
    if (httpd_start(&server, &cfg) == ESP_OK) {
        static const httpd_uri_t uris[] = {
            {.uri = "/", .method = HTTP_GET, .handler = page_root},
            {.uri = "/add", .method = HTTP_GET, .handler = page_add},
            {.uri = "/manage", .method = HTTP_GET, .handler = page_manage},
            {.uri = "/save", .method = HTTP_POST, .handler = page_save},
            {.uri = "/delete", .method = HTTP_POST, .handler = page_delete},
        };
        for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]); i++) {
            httpd_register_uri_handler(server, &uris[i]);
        }
        httpd_register_err_handler(server, HTTPD_404_NOT_FOUND, not_found);
    } else {
        ESP_LOGE(TAG, "web server failed to start");
    }
    dns_server_start();
    ESP_LOGI(TAG, "portal up: join '%s' (password '%s'), open http://192.168.4.1/",
             AP_SSID, AP_PASSWORD);

    int64_t end = esp_timer_get_time() + (int64_t)timeout_s * 1000000;
    while (!s_saved && esp_timer_get_time() < end) vTaskDelay(pdMS_TO_TICKS(200));
    if (s_saved) {
        vTaskDelay(pdMS_TO_TICKS(1000));    // let the "Saved" page reach the phone
    } else {
        ESP_LOGW(TAG, "portal timed out; continuing without new network");
    }

    dns_server_stop();
    if (server) httpd_stop(server);
    esp_wifi_set_mode(WIFI_MODE_STA);
    ui_set_portal(false);
}
