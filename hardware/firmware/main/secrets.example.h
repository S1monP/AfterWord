// =============================================================================
//  secrets.example.h  --  template for secrets.h (which is gitignored).
//
//  Copy to secrets.h in this folder and fill in.  config.h includes
//  secrets.h automatically when it exists; without it the defaults apply.
// =============================================================================
#pragma once

// Shared upload token; must match AFTERWORD_DEVICE_TOKEN on the server.
#define DEVICE_TOKEN   "change-me"

// Where the server is: the PC on your network, or a VPS.
// #define SERVER_URL  "http://192.168.1.10:8000"
// #define SERVER_URL  "https://notes.example.com"

// Password of the Wi-Fi setup portal (8 to 63 characters).
#define AP_PASSWORD    "change-me-too"
