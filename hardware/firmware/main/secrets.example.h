// =============================================================================
//  secrets.example.h  --  template for secrets.h (which is gitignored).
//
//  Copy to secrets.h in this folder and fill in.  config.h includes
//  secrets.h automatically when it exists; without it the defaults apply.
// =============================================================================
#pragma once

// Shared upload token; must match the server once server auth is enabled.
#define DEVICE_TOKEN   "change-me"

// Override the server, e.g. the VPS:
// #define SERVER_URL  "https://notes.example.com"
