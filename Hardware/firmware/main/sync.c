// =============================================================================
//  sync.c
// =============================================================================
#include "sync.h"

#include <dirent.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "app.h"
#include "cJSON.h"
#include "config.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "storage.h"
#include "timesync.h"
#include "ui.h"
#include "wifi_mgr.h"

static const char *TAG = "sync";

#define MAX_FOLDERS     16
#define RESP_CAP        1024
#define IO_BLOCK        4096
#define HTTP_NO_CONNECT (-2)            // http_request: server unreachable

typedef enum { SESS_DONE, SESS_FAILED, SESS_OFFLINE } sess_result_t;

static TaskHandle_t   s_task;
static volatile bool  s_abort;
static volatile bool  s_recording;
static volatile int64_t s_next_us = INT64_MAX;

// The chunks of one session as found on the SD.  Local numbers can have
// gaps; the server wants 1..N, so server index k is the k-th smallest local
// number.  This one list drives the announced count, every PUT, the repair
// and the purge, so only files that were part of a confirmed upload are
// ever deleted.
typedef struct {
    uint16_t local[SYNC_MAX_CHUNKS];
    int      count;
    bool     legacy_wav;
    bool     overflow;
} session_chunks_t;

// -----------------------------------------------------------------------------
//  HTTP
// -----------------------------------------------------------------------------
// One request.  Body comes from `body` or is streamed from `file`.  Returns
// the HTTP status, HTTP_NO_CONNECT when the server cannot be reached, or
// -1 on any other transport error / abort.  The response body (up to
// RESP_CAP-1 bytes) is left NUL-terminated in `resp`.
static int http_request(esp_http_client_method_t method, const char *path,
                        const char *content_type, const char *body,
                        FILE *file, long file_size, char *resp, int timeout_ms) {
    char url[192];
    snprintf(url, sizeof(url), "%s%s", SERVER_URL, path);
    resp[0] = '\0';

    esp_http_client_config_t cfg = {
        .url = url,
        .method = method,
        .timeout_ms = timeout_ms,
        .crt_bundle_attach = strncmp(url, "https:", 6) == 0 ? esp_crt_bundle_attach : NULL,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return -1;
    if (content_type) esp_http_client_set_header(c, "Content-Type", content_type);
    if (DEVICE_TOKEN[0]) {
        char auth[160];
        snprintf(auth, sizeof(auth), "Bearer %s", DEVICE_TOKEN);
        esp_http_client_set_header(c, "Authorization", auth);
    }

    int status = -1;
    long len = file ? file_size : (body ? (long)strlen(body) : 0);
    if (esp_http_client_open(c, len) != ESP_OK) {
        ESP_LOGW(TAG, "%s: cannot connect", url);
        status = HTTP_NO_CONNECT;
        goto out;
    }

    if (file) {
        static char block[IO_BLOCK];
        long sent = 0;
        while (sent < len) {
            if (s_abort) goto out;              // server keeps a torn chunk; re-PUT fixes it
            size_t n = fread(block, 1, sizeof(block), file);
            if (n == 0) goto out;
            for (size_t off = 0; off < n;) {
                int w = esp_http_client_write(c, block + off, n - off);
                if (w <= 0) goto out;
                off += w;
            }
            sent += n;
        }
    } else if (len > 0) {
        if (esp_http_client_write(c, body, len) != len) goto out;
    }

    if (esp_http_client_fetch_headers(c) < 0) goto out;
    status = esp_http_client_get_status_code(c);
    int n = esp_http_client_read_response(c, resp, RESP_CAP - 1);
    resp[n > 0 ? n : 0] = '\0';
out:
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    return status;
}

// Returns the HTTP status (200/201 = announced), or HTTP_NO_CONNECT / -1.
static int announce(const char *id, const char *title, int expected, const char *started_at) {
    static char resp[RESP_CAP];
    char body[320];
    // Provenance only: the format the encoder produced (16-bit PCM in).
    snprintf(body, sizeof(body),
             "{\"id\":\"%s\",\"title\":\"%s\",\"expected_count\":%d,"
             "\"sample_rate\":%d,\"channels\":1,\"bits_per_sample\":16,"
             "\"started_at\":\"%s\"}",
             id, title, expected, SAMPLE_RATE, started_at);
    int st = http_request(HTTP_METHOD_POST, "/api/v1/sessions", "application/json",
                          body, NULL, 0, resp, HTTP_ANNOUNCE_TIMEOUT_MS);
    if (st == 200 || st == 201) {
        ESP_LOGI(TAG, "announce %s -> %d (%d chunks)", id, st, expected);
    } else if (st != HTTP_NO_CONNECT) {
        ESP_LOGW(TAG, "announce %s failed: %d %s", id, st, resp);
    }
    return st;
}

static bool announced(int status) { return status == 200 || status == 201; }

// Returns the HTTP status (200 = stored), or HTTP_NO_CONNECT / -1.
static int put_chunk(const char *id, int index, const char *file_path) {
    static char resp[RESP_CAP];
    struct stat st;
    if (stat(file_path, &st) != 0 || st.st_size == 0) {
        ESP_LOGW(TAG, "chunk %d: %s missing or empty", index, file_path);
        return -1;
    }
    FILE *f = fopen(file_path, "rb");
    if (!f) return -1;
    char path[128];
    snprintf(path, sizeof(path), "/api/v1/sessions/%s/chunks/%d", id, index);
    int64_t t0 = esp_timer_get_time();
    int code = http_request(HTTP_METHOD_PUT, path, "audio/aac", NULL, f, st.st_size, resp,
                            HTTP_TIMEOUT_MS);
    fclose(f);
    if (code == 200) {
        ESP_LOGI(TAG, "chunk %d: %ld B in %lld ms", index, (long)st.st_size,
                 (esp_timer_get_time() - t0) / 1000);
    } else if (!s_abort && code != HTTP_NO_CONNECT) {
        ESP_LOGW(TAG, "chunk %d failed: %d %s", index, code, resp);
    }
    return code;
}

typedef enum { FIN_OK, FIN_MISSING, FIN_FAIL } finish_t;

// On FIN_MISSING, `missing` receives the server's 1-based indices, 0-ended.
static finish_t finish(const char *id, int *missing, int max) {
    static char resp[RESP_CAP];
    char path[128];
    snprintf(path, sizeof(path), "/api/v1/sessions/%s/finish", id);
    int st = http_request(HTTP_METHOD_POST, path, NULL, NULL, NULL, 0, resp, HTTP_TIMEOUT_MS);
    cJSON *j = cJSON_Parse(resp);

    finish_t r = FIN_FAIL;
    if (st == 200) {
        const cJSON *reason = cJSON_GetObjectItem(j, "reason");
        ESP_LOGI(TAG, "finish %s -> 200 %s", id,
                 cJSON_IsString(reason) ? reason->valuestring : "?");
        r = FIN_OK;
    } else if (st == 409) {
        const cJSON *err = cJSON_GetObjectItem(j, "error");
        const cJSON *arr = cJSON_GetObjectItem(j, "missing");
        if (cJSON_IsString(err) && strcmp(err->valuestring, "missing_chunks") == 0
            && cJSON_IsArray(arr)) {
            int n = 0;
            const cJSON *it;
            cJSON_ArrayForEach(it, arr) {
                if (n < max - 1 && cJSON_IsNumber(it)) missing[n++] = it->valueint;
            }
            missing[n] = 0;
            ESP_LOGW(TAG, "finish %s -> 409, %d chunk(s) missing", id, n);
            r = FIN_MISSING;
        } else {
            ESP_LOGW(TAG, "finish %s -> 409 %s", id, resp);
        }
    } else {
        ESP_LOGW(TAG, "finish %s -> %d %s", id, st, resp);
    }
    cJSON_Delete(j);
    return r;
}

// -----------------------------------------------------------------------------
//  Session folders
// -----------------------------------------------------------------------------
static bool list_chunks(const char *folder, session_chunks_t *sc) {
    sc->count = 0;
    sc->legacy_wav = false;
    sc->overflow = false;
    DIR *d = opendir(folder);
    if (!d) return false;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (storage_chunk_number(e->d_name, ".wav")) sc->legacy_wav = true;
        uint16_t n = storage_chunk_number(e->d_name, ".aac");
        if (!n) continue;
        if (sc->count < SYNC_MAX_CHUNKS) sc->local[sc->count++] = n;
        else sc->overflow = true;
    }
    closedir(d);
    for (int i = 1; i < sc->count; i++) {           // directory order != numeric
        uint16_t v = sc->local[i];
        int j = i - 1;
        while (j >= 0 && sc->local[j] > v) { sc->local[j + 1] = sc->local[j]; j--; }
        sc->local[j + 1] = v;
    }
    return true;
}

static void chunk_path(const char *folder, uint16_t local, char *out, size_t cap) {
    snprintf(out, cap, "%s/%u.aac", folder, (unsigned)local);
}

static void purge(const char *folder, const session_chunks_t *sc) {
    char p[128];
    for (int i = 0; i < sc->count; i++) {
        chunk_path(folder, sc->local[i], p, sizeof(p));
        unlink(p);
    }
    snprintf(p, sizeof(p), "%s/" SESSION_START_FILE, folder);
    unlink(p);
    if (rmdir(folder) == 0) ESP_LOGI(TAG, "removed %s", folder);
    else ESP_LOGW(TAG, "could not remove %s (not empty?)", folder);
}

static const char *base_name(const char *path) {
    const char *b = strrchr(path, '/');
    return b ? b + 1 : path;
}

// started_at, in local wall-clock time like the folder names.  Timed folders
// carry it in their name; untimed ones (rec_nt...) only get one when their
// start.txt is from this boot and the clock has been set since.  "" makes
// the server use the upload time.
static void started_at_for(const char *folder, char *out, size_t cap) {
    out[0] = '\0';
    int Y, Mo, D, H, Mi;
    if (sscanf(base_name(folder), "rec_%4d-%2d-%2dT%2d%2d", &Y, &Mo, &D, &H, &Mi) == 5) {
        snprintf(out, cap, "%04d-%02d-%02dT%02d:%02d:00", Y, Mo, D, H, Mi);
        return;
    }
    if (!time_valid()) return;
    char p[128];
    snprintf(p, sizeof(p), "%s/" SESSION_START_FILE, folder);
    FILE *f = fopen(p, "r");
    if (!f) return;
    unsigned long boot = 0;
    long long start_us = 0;
    int ok = fscanf(f, "%lu %lld", &boot, &start_us);
    fclose(f);
    if (ok != 2 || boot != app_boot_id()) return;
    time_t start = time(NULL) - (time_t)((esp_timer_get_time() - start_us) / 1000000);
    struct tm t;
    localtime_r(&start, &t);
    strftime(out, cap, "%Y-%m-%dT%H:%M:%S", &t);
}

// SESS_DONE when the session was confirmed and deleted; SESS_OFFLINE when
// the server could not be reached (the sweep stops: the rest would fail
// the same way).
static sess_result_t sync_session(const char *folder) {
    static session_chunks_t sc;
    if (!list_chunks(folder, &sc)) return SESS_FAILED;
    if (sc.legacy_wav) return SESS_FAILED;          // see storage_recover()
    if (sc.overflow) {
        ESP_LOGW(TAG, "%s: more than %d chunks, skipping", folder, SYNC_MAX_CHUNKS);
        return SESS_FAILED;
    }
    if (sc.count == 0) {
        char p[128];
        snprintf(p, sizeof(p), "%s/" SESSION_START_FILE, folder);
        unlink(p);
        if (rmdir(folder) == 0) ESP_LOGI(TAG, "%s held no audio, removed", folder);
        return SESS_FAILED;
    }

    const char *name = base_name(folder);
    uint8_t mac[6];
    esp_efuse_mac_get_default(mac);
    char id[80], started_at[32];
    snprintf(id, sizeof(id), "%02x%02x_%s", mac[4], mac[5], name);
    started_at_for(folder, started_at, sizeof(started_at));
    const int expected = sc.count;
    ESP_LOGI(TAG, "session %s: %d chunk(s), started_at=%s", id, expected,
             started_at[0] ? started_at : "(upload time)");

    int st = announce(id, name, expected, started_at);
    if (st == HTTP_NO_CONNECT) return SESS_OFFLINE;
    if (!announced(st)) return SESS_FAILED;

    char path[128];
    for (int k = 1; k <= expected; k++) {
        if (s_abort) return SESS_FAILED;
        chunk_path(folder, sc.local[k - 1], path, sizeof(path));
        st = put_chunk(id, k, path);
        if (st == HTTP_NO_CONNECT) return SESS_OFFLINE;
        if (st != 200) {
            if (s_abort) return SESS_FAILED;
            // 404/409: the server lost or moved on from the session (e.g. a
            // restart).  Re-announce once and retry this chunk.
            if (!announced(announce(id, name, expected, started_at))) return SESS_FAILED;
            if (put_chunk(id, k, path) != 200) return SESS_FAILED;
        }
    }

    static int missing[SYNC_MAX_CHUNKS + 1];
    finish_t fr = finish(id, missing, SYNC_MAX_CHUNKS + 1);
    if (fr == FIN_MISSING) {                        // one repair round
        for (int m = 0; missing[m] > 0; m++) {
            int idx = missing[m];
            if (idx < 1 || idx > expected || s_abort) return SESS_FAILED;
            chunk_path(folder, sc.local[idx - 1], path, sizeof(path));
            if (put_chunk(id, idx, path) != 200) return SESS_FAILED;
        }
        fr = finish(id, missing, SYNC_MAX_CHUNKS + 1);
    }
    if (fr != FIN_OK) return SESS_FAILED;

    purge(folder, &sc);
    return SESS_DONE;
}

static void sweep(void) {
    DIR *root = opendir(SD_MOUNT);
    if (!root) return;
    static char folders[MAX_FOLDERS][300];
    int n = 0;
    struct dirent *e;
    while (n < MAX_FOLDERS && (e = readdir(root)) != NULL) {
        if (e->d_type == DT_DIR && strncmp(e->d_name, "rec_", 4) == 0) {
            snprintf(folders[n++], sizeof(folders[0]), "%s/%s", SD_MOUNT, e->d_name);
        }
    }
    closedir(root);
    if (n == 0) return;

    ESP_LOGI(TAG, "sweep: %d session folder(s)", n);
    ui_set_syncing(true);
    int done = 0;
    bool offline = false;
    for (int i = 0; i < n && !s_abort && !offline; i++) {
        sess_result_t r = sync_session(folders[i]);
        if (r == SESS_DONE) {
            done++;
            ui_flash_sync_ok();
        }
        offline = (r == SESS_OFFLINE);
    }
    ui_set_syncing(false);
    if (s_abort) ESP_LOGI(TAG, "sweep aborted (recording started)");
    if (offline) {
        ESP_LOGW(TAG, "server %s unreachable; next try in %lu min", SERVER_URL,
                 (unsigned long)(SYNC_RETRY_INTERVAL_MS / 60000));
    }
    ESP_LOGI(TAG, "sweep done: %d of %d uploaded", done, n);
}

static void sync_task(void *arg) {
    for (;;) {
        int64_t now = esp_timer_get_time();
        int64_t wait_us = s_next_us - now;
        if (wait_us > 0) {
            TickType_t ticks = wait_us > (int64_t)3600 * 1000000
                                   ? pdMS_TO_TICKS(3600 * 1000)
                                   : pdMS_TO_TICKS(wait_us / 1000) + 1;
            ulTaskNotifyTake(pdTRUE, ticks);
            continue;                               // re-evaluate the deadline
        }
        s_next_us = esp_timer_get_time() + (int64_t)SYNC_RETRY_INTERVAL_MS * 1000;
        if (s_recording || !wifi_mgr_connected()) continue;
        s_abort = false;
        sweep();
    }
}

void sync_start(void) {
    // 12 KB: an HTTPS (TLS) handshake needs the headroom.
    xTaskCreatePinnedToCore(sync_task, "sync", 12 * 1024, NULL, 2, &s_task, 0);
}

void sync_kick(uint32_t delay_ms) {
    int64_t at = esp_timer_get_time() + (int64_t)delay_ms * 1000;
    if (at < s_next_us) s_next_us = at;
    if (s_task) xTaskNotifyGive(s_task);
}

void sync_set_recording(bool recording) {
    s_recording = recording;
    if (recording) s_abort = true;
}
