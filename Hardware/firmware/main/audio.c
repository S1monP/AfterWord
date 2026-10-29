// =============================================================================
//  audio.c  --  I2S capture + live AAC-LC encoding to chunk files.
// =============================================================================
#include "audio.h"

#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "config.h"
#include "driver/i2s_std.h"
#include "esp_aac_enc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/ringbuf.h"
#include "freertos/task.h"

static const char *TAG = "audio";

#define FRAME_US            ((int64_t)FRAME_SAMPLES * 1000000 / SAMPLE_RATE)   // ~46.4 ms
#define FRAMES_PER_CHUNK    ((uint32_t)CHUNK_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
#define FRAMES_PER_FLUSH    ((uint32_t)FLUSH_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
#define FRAMES_PER_STATS    ((uint32_t)60 * SAMPLE_RATE / FRAME_SAMPLES)
// Silent frames fed at the end of a segment to push the encoder's look-ahead
// out, so the last words before a pause/stop are not lost.
#define FLUSH_SILENT_FRAMES 2
#define WRITE_BUF_BYTES     (16 * 1024)

// --- ring buffer items (all produced by the capture task, in order) ---------
typedef enum { IT_PCM = 1, IT_START, IT_PAUSE, IT_RESUME, IT_STOP } item_type_t;

typedef struct {
    uint32_t type;
    int16_t pcm[FRAME_SAMPLES];
} pcm_item_t;

typedef struct {
    uint32_t type;
    char folder[64];
} ctl_item_t;

#define EV_STOPPED  BIT0

static i2s_chan_handle_t  s_rx;
static RingbufHandle_t    s_ring;
static QueueHandle_t      s_cmd_q;      // ctl_item_t, app -> capture
static EventGroupHandle_t s_ev;
static volatile bool      s_error;

// Stats (capture side).
static volatile uint32_t  s_overruns;
static volatile size_t    s_ring_min_free;

// =============================================================================
//  Capture task
// =============================================================================
static void capture_task(void *arg) {
    static int32_t raw[FRAME_SAMPLES];
    static pcm_item_t item = {.type = IT_PCM};
    bool capturing = false;

    for (;;) {
        // Commands go into the ring in order with the audio, between frames.
        ctl_item_t c;
        while (xQueueReceive(s_cmd_q, &c, 0) == pdTRUE) {
            // Control items must never be dropped; wait for room.
            while (xRingbufferSend(s_ring, &c, sizeof(c), pdMS_TO_TICKS(100)) != pdTRUE) {
                ESP_LOGW(TAG, "ring full, waiting to queue control item");
            }
            capturing = (c.type == IT_START || c.type == IT_RESUME);
        }

        size_t got = 0;
        if (i2s_channel_read(s_rx, raw, sizeof(raw), &got, portMAX_DELAY) != ESP_OK
            || got != sizeof(raw)) {
            continue;
        }
        if (!capturing) continue;       // keep DMA drained while idle/paused

        // Gain with saturation, then the high 16 bits (INMP441 data is
        // left-aligned in the 32-bit slot).
        for (int i = 0; i < FRAME_SAMPLES; i++) {
            int64_t v = (int64_t)raw[i] * MIC_GAIN;
            if (v > INT32_MAX) v = INT32_MAX;
            if (v < INT32_MIN) v = INT32_MIN;
            item.pcm[i] = (int16_t)(v >> 16);
        }
        if (xRingbufferSend(s_ring, &item, sizeof(item), 0) != pdTRUE) {
            s_overruns++;               // writer fell > ~11 s behind: frame lost
        }
        size_t free_now = xRingbufferGetCurFreeSize(s_ring);
        if (free_now < s_ring_min_free) s_ring_min_free = free_now;
    }
}

// =============================================================================
//  Writer task: encoder + chunk files
// =============================================================================
typedef struct {
    void    *enc;                   // esp_aac_enc handle
    int      in_bytes;              // encoder input frame size (bytes)
    uint8_t *out;                   // encoder output buffer
    int      out_cap;
    FILE    *f;
    char    *fbuf;                  // stdio buffer for f (PSRAM)
    char     folder[64];
    uint16_t chunk;
    uint32_t chunk_frames;
    uint32_t flush_frames;
    // stats
    uint32_t stat_frames;
    int64_t  stat_us_sum;
    int64_t  stat_us_max;
} writer_t;

static writer_t w;

static bool enc_open(void) {
    esp_aac_enc_config_t cfg = ESP_AAC_ENC_CONFIG_DEFAULT();
    cfg.sample_rate = SAMPLE_RATE;
    cfg.channel = 1;
    cfg.bits_per_sample = 16;
    cfg.bitrate = AAC_BITRATE;
    cfg.adts_used = true;           // server stitches by concatenating ADTS
    if (esp_aac_enc_open(&cfg, sizeof(cfg), &w.enc) != ESP_AUDIO_ERR_OK) {
        ESP_LOGE(TAG, "AAC encoder open failed");
        w.enc = NULL;
        return false;
    }
    int in = 0, out = 0;
    esp_aac_enc_get_frame_size(w.enc, &in, &out);
    if (in != FRAME_SAMPLES * (int)sizeof(int16_t)) {
        ESP_LOGE(TAG, "unexpected encoder frame size %d", in);
        esp_aac_enc_close(w.enc);
        w.enc = NULL;
        return false;
    }
    if (out > w.out_cap) {
        heap_caps_free(w.out);
        w.out = heap_caps_aligned_alloc(16, out, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        w.out_cap = w.out ? out : 0;
    }
    w.in_bytes = in;
    return w.out != NULL;
}

static void enc_close(void) {
    if (w.enc) esp_aac_enc_close(w.enc);
    w.enc = NULL;
}

static void file_close(void) {
    if (!w.f) return;
    fflush(w.f);
    fsync(fileno(w.f));
    fclose(w.f);
    w.f = NULL;
    ESP_LOGI(TAG, "chunk %u closed (%lu frames)", (unsigned)w.chunk,
             (unsigned long)w.chunk_frames);
}

static bool file_open(void) {
    char path[96];
    snprintf(path, sizeof(path), "%s/%u.aac", w.folder, (unsigned)w.chunk);
    w.f = fopen(path, "wb");
    if (!w.f) {
        ESP_LOGE(TAG, "cannot create %s", path);
        return false;
    }
    setvbuf(w.f, w.fbuf, _IOFBF, WRITE_BUF_BYTES);
    w.chunk_frames = 0;
    w.flush_frames = 0;
    ESP_LOGI(TAG, "chunk %u -> %s", (unsigned)w.chunk, path);
    return true;
}

static void fail(const char *what) {
    ESP_LOGE(TAG, "%s -- stopping capture to SD", what);
    s_error = true;
    if (w.f) { fclose(w.f); w.f = NULL; }
    enc_close();
}

static void log_stats(void) {
    if (w.stat_frames == 0) return;
    int64_t avg = w.stat_us_sum / w.stat_frames;
    size_t ring_used_max = PCM_RING_BYTES - s_ring_min_free;
    ESP_LOGI(TAG, "encode avg %lld us, max %lld us per %lld us frame "
             "(%.1f%% of one core); buffer peak %u KB of %u KB; dropped frames %lu",
             avg, w.stat_us_max, FRAME_US, 100.0 * avg / FRAME_US,
             (unsigned)(ring_used_max / 1024), (unsigned)(PCM_RING_BYTES / 1024),
             (unsigned long)s_overruns);
    w.stat_frames = 0;
    w.stat_us_sum = 0;
    w.stat_us_max = 0;
    s_ring_min_free = PCM_RING_BYTES;
}

static void encode_and_write(const int16_t *pcm) {
    if (!w.enc || !w.f) return;

    // The encoder may require aligned input; ring buffer items are only
    // 4-byte aligned, so hand it an aligned copy (2 KB per 46 ms: trivial).
    static int16_t aligned_in[FRAME_SAMPLES] __attribute__((aligned(16)));
    memcpy(aligned_in, pcm, sizeof(aligned_in));
    esp_audio_enc_in_frame_t in = {.buffer = (uint8_t *)aligned_in, .len = (uint32_t)w.in_bytes};
    esp_audio_enc_out_frame_t out = {.buffer = w.out, .len = (uint32_t)w.out_cap};
    int64_t t0 = esp_timer_get_time();
    esp_audio_err_t r = esp_aac_enc_process(w.enc, &in, &out);
    int64_t dt = esp_timer_get_time() - t0;
    if (r != ESP_AUDIO_ERR_OK) {
        fail("encode failed");
        return;
    }
    w.stat_frames++;
    w.stat_us_sum += dt;
    if (dt > w.stat_us_max) w.stat_us_max = dt;

    if (out.encoded_bytes > 0 &&
        fwrite(w.out, 1, out.encoded_bytes, w.f) != out.encoded_bytes) {
        fail("SD write failed");
        return;
    }
    w.chunk_frames++;
    if (++w.flush_frames >= FRAMES_PER_FLUSH) {
        w.flush_frames = 0;
        fflush(w.f);
        fsync(fileno(w.f));         // bounds what a power cut can lose
    }
    if (w.stat_frames >= FRAMES_PER_STATS) log_stats();

    // Rotate at a frame boundary; the encoder keeps its state, so the next
    // chunk continues the same stream without a gap.
    if (w.chunk_frames >= FRAMES_PER_CHUNK) {
        file_close();
        w.chunk++;
        if (!file_open()) fail("chunk rotation failed");
    }
}

static void segment_begin(void) {
    s_error = false;
    if (!enc_open()) { fail("encoder"); return; }
    if (!file_open()) { fail("chunk open"); return; }
}

static void segment_end(void) {
    if (w.enc && w.f) {
        static int16_t silence[FRAME_SAMPLES];
        for (int i = 0; i < FLUSH_SILENT_FRAMES; i++) encode_and_write(silence);
    }
    file_close();
    enc_close();
    log_stats();
}

static void writer_task(void *arg) {
    for (;;) {
        size_t size = 0;
        void *p = xRingbufferReceive(s_ring, &size, portMAX_DELAY);
        if (!p) continue;
        uint32_t type = *(uint32_t *)p;

        switch (type) {
        case IT_PCM:
            encode_and_write(((pcm_item_t *)p)->pcm);
            break;
        case IT_START:
            snprintf(w.folder, sizeof(w.folder), "%s", ((ctl_item_t *)p)->folder);
            w.chunk = 1;
            segment_begin();
            break;
        case IT_RESUME:
            w.chunk++;
            segment_begin();
            break;
        case IT_PAUSE:
            segment_end();
            break;
        case IT_STOP:
            segment_end();
            xEventGroupSetBits(s_ev, EV_STOPPED);
            break;
        }
        vRingbufferReturnItem(s_ring, p);
    }
}

// =============================================================================
//  Public API
// =============================================================================
static esp_err_t i2s_init(void) {
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan.dma_desc_num = 8;
    chan.dma_frame_num = 512;       // 8 x 512 samples = ~186 ms of DMA
    esp_err_t err = i2s_new_channel(&chan, NULL, &s_rx);
    if (err != ESP_OK) return err;

    // INMP441: standard Philips I2S, 32-bit slots, left channel only.
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                        I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_MIC_SCK,
            .ws = PIN_MIC_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = PIN_MIC_SD,
        },
    };
    std.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
    err = i2s_channel_init_std_mode(s_rx, &std);
    if (err != ESP_OK) return err;
    return i2s_channel_enable(s_rx);
}

esp_err_t audio_init(void) {
    esp_err_t err = i2s_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2S init failed: %s", esp_err_to_name(err));
        return err;
    }

    // Ring buffer storage in PSRAM; the control structure stays internal.
    uint8_t *storage = heap_caps_malloc(PCM_RING_BYTES, MALLOC_CAP_SPIRAM);
    StaticRingbuffer_t *ctrl = heap_caps_malloc(sizeof(StaticRingbuffer_t),
                                                MALLOC_CAP_INTERNAL);
    w.fbuf = heap_caps_malloc(WRITE_BUF_BYTES, MALLOC_CAP_SPIRAM);
    if (!storage || !ctrl || !w.fbuf) {
        ESP_LOGE(TAG, "out of memory for audio buffers (is PSRAM enabled?)");
        return ESP_ERR_NO_MEM;
    }
    s_ring = xRingbufferCreateStatic(PCM_RING_BYTES, RINGBUF_TYPE_NOSPLIT, storage, ctrl);
    s_cmd_q = xQueueCreate(4, sizeof(ctl_item_t));
    s_ev = xEventGroupCreate();
    s_ring_min_free = PCM_RING_BYTES;

    // Prove the encoder works before anyone tries to record.
    if (!enc_open()) return ESP_FAIL;
    enc_close();

    // Core 1 is reserved for audio; WiFi and the UI live on core 0.  The
    // writer needs a large stack for the encoder.
    xTaskCreatePinnedToCore(capture_task, "capture", 4096, NULL,
                            configMAX_PRIORITIES - 2, NULL, 1);
    xTaskCreatePinnedToCore(writer_task, "writer", 48 * 1024, NULL,
                            configMAX_PRIORITIES - 4, NULL, 1);
    ESP_LOGI(TAG, "audio ready: %d Hz mono, AAC-LC %d bps, %d s chunks, "
             "%u KB buffer", SAMPLE_RATE, AAC_BITRATE, CHUNK_SECONDS,
             (unsigned)(PCM_RING_BYTES / 1024));
    return ESP_OK;
}

static void send_cmd(item_type_t type, const char *folder) {
    ctl_item_t c = {.type = type};
    if (folder) snprintf(c.folder, sizeof(c.folder), "%s", folder);
    xQueueSend(s_cmd_q, &c, portMAX_DELAY);
}

void audio_start(const char *folder) {
    xEventGroupClearBits(s_ev, EV_STOPPED);
    s_error = false;
    send_cmd(IT_START, folder);
}

void audio_pause(void)  { send_cmd(IT_PAUSE, NULL); }
void audio_resume(void) { send_cmd(IT_RESUME, NULL); }

void audio_stop(void) {
    send_cmd(IT_STOP, NULL);
    // The writer may still be draining up to ~11 s of buffered audio.
    if (!(xEventGroupWaitBits(s_ev, EV_STOPPED, pdTRUE, pdTRUE,
                              pdMS_TO_TICKS(30000)) & EV_STOPPED)) {
        ESP_LOGE(TAG, "writer did not confirm stop");
    }
}

bool audio_has_error(void) { return s_error; }
