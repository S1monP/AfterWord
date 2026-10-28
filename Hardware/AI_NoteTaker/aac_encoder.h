// =============================================================================
//  aac_encoder.h   (V0.5)
//
//  AAC encoder integration layer.
//
//  Goal of V0.5:
//      Every recorded session ends up on the SD as `.aac` chunks; WAVs are
//      deleted after a verified encode.  Fully verifiable without the server
//      (pull SD, check with ffprobe).
//
//  Public API
//  ----------
//      bool aacEncodeFile(const char *wavPath, const char *aacPath);
//
//          Reads the entire WAV file at wavPath (RIFF/WAVE, PCM int32 mono,
//          22050 Hz, 32-bit-slot samples written by V0.4's recording loop),
//          encodes it as AAC-LC mono 22050 Hz 64 kbps, and writes the result
//          to aacPath as a stream of ADTS-framed packets.
//
//          Returns true on success.  On failure, writes nothing (any partial
//          output is cleaned up by the caller before renaming) and returns
//          false so the caller can log + leave the source WAV for next boot.
//
//  Output format contract (must match the server's canonical settings)
//  -----------------------------------------------------------------
//      All three constants below are the SINGLE source of truth shared by
//      aac_encoder.cpp and the .ino (which logs them at boot).  Do not
//      re-declare them in the .ino or you will silently desync the encoder
//      from the logging path.
// =============================================================================

#ifndef AI_NOTETAKER_AAC_ENCODER_H
#define AI_NOTETAKER_AAC_ENCODER_H

#include <Arduino.h>

// AAC output format -- matches the server's canonical session settings.
//   - 22050 Hz sample rate is one of vo-aacenc's documented supported rates.
//   - 64 kbps is a discrete bandwidth preset per vo-aacenc's encoder.
//   - mono (1 channel) is required: the INMP441 mic writes one channel.
//   - ADTS framing is enabled at runtime (see aac_encoder.cpp).
#define AAC_SAMPLE_RATE     22050
#define AAC_BITRATE_BPS     64000
#define AAC_CHANNELS        1

bool aacEncodeFile(const char *wavPath, const char *aacPath);

#endif  // AI_NOTETAKER_AAC_ENCODER_H
