idf_component_register(
    SRCS
        "main.c"
        "ui.c"
        "storage.c"
        "adts.c"
        "audio.c"
        "wifi_mgr.c"
        "portal.c"
        "dns_server.c"
        "timesync.c"
        "sync.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_driver_gpio
        esp_driver_i2s
        esp_driver_sdspi
        esp_driver_spi
        esp_event
        esp_http_client
        esp_http_server
        esp_netif
        esp_ringbuf
        esp_timer
        esp_wifi
        fatfs
        mbedtls
        nvs_flash
        sdmmc
        vfs
)

# secrets.h is optional.  Say on the compile command line whether it exists:
# a bare __has_include is invisible to ninja and ccache, which then keep
# objects built without it (wrong server URL).  CMake re-runs when files
# are added to or removed from this folder.
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/secrets.h")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE HAVE_SECRETS_H)
endif()
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
             "${CMAKE_CURRENT_SOURCE_DIR}")
