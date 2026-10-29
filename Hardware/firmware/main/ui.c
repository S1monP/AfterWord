// =============================================================================
//  ui.c  --  WS2812 status LED (RMT) and the push button (GPIO, polled).
// =============================================================================
#include "ui.h"

#include "config.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "led_strip.h"

static const char *TAG = "ui";

#define UI_TICK_MS          10
#define BTN_DEBOUNCE_MS     30
#define BTN_SHORT_MIN_MS    50
#define BTN_LONG_MS         1000
#define LED_BRIGHTNESS      40      // 0-255, scales every colour

#define RGB_OFF             0x000000u
#define RGB_BLUE            0x0000FFu
#define RGB_GREEN           0x00FF00u
#define RGB_RED             0xFF0000u
#define RGB_ORANGE          0xFF8000u
#define RGB_MAGENTA         0xFF00FFu

static led_strip_handle_t s_strip;
static QueueHandle_t      s_btn_q;
static bool               s_held_at_boot;

static volatile ui_mode_t s_mode = UI_BOOT;
static volatile bool      s_syncing;
static volatile bool      s_portal;
static volatile int64_t   s_boot_done_until_us;
static volatile int64_t   s_sync_ok_until_us;

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

static bool button_pressed(void) { return gpio_get_level(PIN_BUTTON) == 0; }

static void led_show(uint32_t rgb) {
    static uint32_t shown = 0xFFFFFFFFu;
    if (rgb == shown) return;
    shown = rgb;
    uint32_t r = ((rgb >> 16) & 0xFF) * LED_BRIGHTNESS / 255;
    uint32_t g = ((rgb >> 8) & 0xFF) * LED_BRIGHTNESS / 255;
    uint32_t b = (rgb & 0xFF) * LED_BRIGHTNESS / 255;
    led_strip_set_pixel(s_strip, 0, r, g, b);
    led_strip_refresh(s_strip);
}

// Square wave: `on` ms lit, `off` ms dark.
static bool blink(int64_t t, int on, int off) { return (t % (on + off)) < on; }

static uint32_t led_colour(int64_t t_ms) {
    int64_t t_us = t_ms * 1000;
    if (t_us < s_sync_ok_until_us) return RGB_GREEN;
    if (t_us < s_boot_done_until_us) return blink(t_ms, 100, 100) ? RGB_GREEN : RGB_OFF;

    if (s_portal && (s_mode == UI_BOOT || s_mode == UI_IDLE)) return RGB_BLUE;

    switch (s_mode) {
    case UI_BOOT:      return blink(t_ms, 1000, 1000) ? RGB_BLUE : RGB_OFF;
    case UI_IDLE:
        if (s_syncing) return blink(t_ms, 1000, 1000) ? RGB_BLUE : RGB_OFF;
        return RGB_GREEN;
    case UI_RECORDING: return blink(t_ms, 250, 750) ? RGB_RED : RGB_OFF;
    case UI_PAUSED:    return blink(t_ms, 1000, 1000) ? RGB_ORANGE : RGB_OFF;
    case UI_ERROR:     return RGB_MAGENTA;
    }
    return RGB_OFF;
}

static void emit_release(int64_t held_ms) {
    btn_event_t ev;
    if (held_ms >= BTN_LONG_MS) ev = BTN_LONG;
    else if (held_ms >= BTN_SHORT_MIN_MS) ev = BTN_SHORT;
    else return;                            // contact bounce, not a press
    if (xQueueSend(s_btn_q, &ev, 0) != pdTRUE) {
        ESP_LOGW(TAG, "button event dropped (queue full)");
    }
}

static void ui_task(void *arg) {
    bool stable = button_pressed();     // debounced state
    bool last_raw = stable;
    int64_t last_change = now_ms();
    int64_t press_start = now_ms();
    // A press already in progress at boot (the portal gesture) must be
    // released before any event fires.
    bool armed = !stable;

    for (;;) {
        int64_t t = now_ms();

        bool raw = button_pressed();
        if (raw != last_raw) { last_raw = raw; last_change = t; }
        if (raw != stable && t - last_change >= BTN_DEBOUNCE_MS) {
            stable = raw;
            if (stable) {
                press_start = t;
            } else if (!armed) {
                armed = true;               // end of the boot-time hold
            } else {
                emit_release(t - press_start);
            }
        }

        led_show(led_colour(t));
        vTaskDelay(pdMS_TO_TICKS(UI_TICK_MS));
    }
}

void ui_init(void) {
    gpio_config_t btn = {
        .pin_bit_mask = 1ULL << PIN_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&btn));
    vTaskDelay(pdMS_TO_TICKS(5));       // let the pull-up settle
    s_held_at_boot = button_pressed();

    led_strip_config_t strip_cfg = {
        .strip_gpio_num = PIN_NEOPIXEL,
        .max_leds = 1,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_GRB,
    };
    led_strip_rmt_config_t rmt_cfg = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
    };
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_strip));
    led_strip_clear(s_strip);

    s_btn_q = xQueueCreate(8, sizeof(btn_event_t));
    // Low priority, core 0: audio owns core 1.
    xTaskCreatePinnedToCore(ui_task, "ui", 3072, NULL, 3, NULL, 0);
}

bool ui_button_held_at_boot(void) { return s_held_at_boot; }
QueueHandle_t ui_button_queue(void) { return s_btn_q; }
void ui_set_mode(ui_mode_t mode) { s_mode = mode; }
void ui_set_syncing(bool syncing) { s_syncing = syncing; }
void ui_set_portal(bool active) { s_portal = active; }

void ui_flash_boot_done(void) {
    s_boot_done_until_us = esp_timer_get_time() + 2000 * 1000;
}

void ui_flash_sync_ok(void) {
    s_sync_ok_until_us = esp_timer_get_time() + 600 * 1000;
}
