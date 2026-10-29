// =============================================================================
//  dns_server.c
// =============================================================================
#include "dns_server.h"

#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

static const char *TAG = "dns";

static volatile bool s_run;
static TaskHandle_t  s_task;

static const uint8_t AP_IP[4] = {192, 168, 4, 1};

// Build the reply in place: same id and question, one A record -> AP_IP.
static int make_reply(uint8_t *buf, int len, int cap) {
    if (len < 12) return -1;
    uint16_t qdcount = (buf[4] << 8) | buf[5];
    if ((buf[2] & 0x80) || qdcount < 1) return -1;     // not a query

    // Walk the first question's name to find its end.
    int p = 12;
    while (p < len && buf[p] != 0) {
        if ((buf[p] & 0xC0) == 0xC0) { p += 1; break; }    // compressed
        p += buf[p] + 1;
    }
    p += 1 + 4;                                         // terminator, QTYPE, QCLASS
    if (p > len || p + 16 > cap) return -1;

    buf[2] = 0x81; buf[3] = 0x80;                       // response, RD+RA, no error
    buf[4] = 0; buf[5] = 1;                             // 1 question
    buf[6] = 0; buf[7] = 1;                             // 1 answer
    memset(buf + 8, 0, 4);                              // no NS / AR

    static const uint8_t answer[] = {
        0xC0, 0x0C,             // name: pointer to the question
        0x00, 0x01, 0x00, 0x01, // type A, class IN
        0x00, 0x00, 0x00, 0x3C, // TTL 60 s
        0x00, 0x04,             // 4 bytes of address
    };
    memcpy(buf + p, answer, sizeof(answer));
    memcpy(buf + p + sizeof(answer), AP_IP, 4);
    return p + sizeof(answer) + 4;
}

static void dns_task(void *arg) {
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(53),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (sock < 0 || bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGE(TAG, "cannot bind UDP 53");
        if (sock >= 0) close(sock);
        s_task = NULL;
        vTaskDelete(NULL);
        return;
    }
    struct timeval tv = {.tv_sec = 1};          // wake up to notice stop
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    uint8_t buf[512];
    while (s_run) {
        struct sockaddr_in from;
        socklen_t flen = sizeof(from);
        int n = recvfrom(sock, buf, sizeof(buf), 0, (struct sockaddr *)&from, &flen);
        if (n <= 0) continue;
        int r = make_reply(buf, n, sizeof(buf));
        if (r > 0) sendto(sock, buf, r, 0, (struct sockaddr *)&from, flen);
    }
    close(sock);
    s_task = NULL;
    vTaskDelete(NULL);
}

void dns_server_start(void) {
    if (s_task) return;
    s_run = true;
    xTaskCreatePinnedToCore(dns_task, "dns", 3072, NULL, 3, &s_task, 0);
}

void dns_server_stop(void) {
    s_run = false;
    while (s_task) vTaskDelay(pdMS_TO_TICKS(50));
}
