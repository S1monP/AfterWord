// =============================================================================
//  config.h  --  device-wide settings shared by every .cpp in the sketch.
//
//  Anything that more than one translation unit needs (the server address
//  is used by sync_client.cpp, logged by the .ino) lives here, so there is
//  exactly one place to edit.  Per-device secrets go in secrets.h, which is
//  gitignored -- copy secrets.example.h to secrets.h to create it.
// =============================================================================

#ifndef AI_NOTETAKER_CONFIG_H
#define AI_NOTETAKER_CONFIG_H

#define FW_VERSION          "0.7.0-dev"

// -----------------------------------------------------------------------------
//  SECRETS  (optional; see secrets.example.h) -- included first so it can
//  override the defaults below.
// -----------------------------------------------------------------------------
#if __has_include("secrets.h")
  #include "secrets.h"
#endif

// -----------------------------------------------------------------------------
//  SERVER
// -----------------------------------------------------------------------------
// Local development: the PC running `python app.py` (Server/).  Use the PC's
// LAN IPv4 address (`ipconfig` on Windows); it can change if your router
// hands out a new one, so give the PC a DHCP reservation if it moves.
#ifndef SERVER_HOST
  #define SERVER_HOST       "192.168.0.50"
#endif
#ifndef SERVER_PORT
  #define SERVER_PORT       8000
#endif

// Sent as "Authorization: Bearer <token>" on every upload request.  Empty
// means no header is sent (the local dev server does not check it yet).
#ifndef DEVICE_TOKEN
  #define DEVICE_TOKEN      ""
#endif

#endif  // AI_NOTETAKER_CONFIG_H
