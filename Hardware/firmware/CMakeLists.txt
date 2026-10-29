# AI NoteTaker firmware (ESP-IDF v5.5, ESP32-S3).
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(ai_notetaker)

# esp_audio_codec adds an unquoted "-L <its lib dir>" link flag, which breaks
# the link when the project path contains a space ("Visual Studio").  The
# flag is redundant -- its libraries are linked by full path through
# add_prebuilt_library -- so drop it here rather than patching the managed
# component (which would be overwritten on update).
set(_acodec __idf_espressif__esp_audio_codec)
if(TARGET ${_acodec})
    foreach(_prop LINK_LIBRARIES INTERFACE_LINK_LIBRARIES)
        get_target_property(_libs ${_acodec} ${_prop})
        if(_libs)
            list(FILTER _libs EXCLUDE REGEX "^(\\$<LINK_ONLY:)?-L ")
            set_target_properties(${_acodec} PROPERTIES ${_prop} "${_libs}")
        endif()
    endforeach()
endif()
