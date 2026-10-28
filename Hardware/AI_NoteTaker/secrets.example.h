// =============================================================================
//  secrets.example.h  --  template for secrets.h (which is gitignored).
//
//  Copy this file to secrets.h in the same folder and fill it in.  config.h
//  picks secrets.h up automatically when it exists; without it the firmware
//  still builds and uses the defaults from config.h.
// =============================================================================

#ifndef AI_NOTETAKER_SECRETS_H
#define AI_NOTETAKER_SECRETS_H

// Shared upload token; must match the server's device token once server
// auth is enabled.
#define DEVICE_TOKEN   "change-me"

// Optional: override the server address without touching config.h.
// #define SERVER_HOST "notes.example.com"
// #define SERVER_PORT 443

#endif  // AI_NOTETAKER_SECRETS_H
