// =============================================================================
//  aac_encoder.cpp   (V0.5)  --  WAV -> ADTS-framed AAC-LC encoder.
//
//  Backend:
//
//    This module reads a RIFF/WAVE file (PCM int32 mono at SAMPLE_RATE Hz,
//    as written by the V0.4 recording loop) and writes an AAC-LC ADTS stream
//    via pschatzmann/codec-vo-aacenc (Apache-2.0).
//
//    Required Arduino library
//    ------------------------
//      Install via Library Manager (ZIP) or copy into the libraries folder:
//
//          codec-vo-aacenc  (pschatzmann)  --  Apache-2.0
//
//      This single dependency adds the original vo-aacenc C99 sources and a
//      VOAACEncoder C++ wrapper.  No dynamic memory other than a small PSRAM
//      buffer is used; the encoder is documented as suitable for ESP32-class
//      MCUs with PSRAM (we have 8 MB on N16R8).
//
//      Until it is installed, this file will fail to compile with a clear
//      "VOAACEncoder.h: No such file" error -- which is intentional: silent
//      stubs let bad audio ship to the server.
// =============================================================================

#include "aac_encoder.h"
#include <SD.h>
#include <VOAACEncoder.h>

// Output buffer per AAC frame.  vo-aacenc emits at most ~135-byte payloads
// for our 22050 Hz/64 kbps/mono settings, so 256 bytes is comfortable
// headroom even for slightly larger frames and the ADTS header is included.
#define AAC_OUT_BUF_BYTES        256


// =============================================================================
//  WAV reader: walks the RIFF chunks until "data", returns its offset/size.
//
//  Robust to non-44-byte headers (LIST/JUNK padding and such).  V0.4
//  always writes the plain 44-byte header, but the encoder must not
//  depend on it.
// =============================================================================
static bool wavFindDataOffset(File &wav, uint32_t *outOffset, uint32_t *outSize) {
  if (wav.size() < 12) return false;
  uint8_t hdr[12];
  if (wav.readBytes((char*)hdr, sizeof(hdr)) != sizeof(hdr)) return false;
  if (memcmp(hdr, "RIFF", 4) != 0)     return false;
  if (memcmp(hdr + 8, "WAVE", 4) != 0) return false;

  uint32_t pos = 12;
  while (pos + 8 <= (uint32_t)wav.size()) {
    wav.seek(pos);
    uint8_t ch[8];
    if (wav.readBytes((char*)ch, sizeof(ch)) != sizeof(ch)) return false;
    uint32_t chunkSize = (uint32_t)ch[4]       |
                         ((uint32_t)ch[5] << 8)|
                         ((uint32_t)ch[6] << 16)|
                         ((uint32_t)ch[7] << 24);
    if (memcmp(ch, "data", 4) == 0) {
      *outOffset = pos + 8;
      *outSize   = chunkSize;
      return true;
    }
    uint32_t advance = chunkSize + (chunkSize & 1);
    if (advance == 0) return false;
    pos += 8 + advance;
  }
  return false;
}


// =============================================================================
//  32 -> 16 bit conversion (recorded samples are gain-saturated int32
//  right-justified in their 32-bit I2S slots; >> 16 yields 16-bit
//  signed).
// =============================================================================
static inline int16_t i32_to_i16(int32_t v) {
  if (v >  32767) return  32767;
  if (v < -32768) return -32768;
  return (int16_t)v;
}
// =============================================================================
//  Encoder driver -- vo-aacenc via pschatzmann/codec-vo-aacenc.
//
//  The wrapper VOAACEncoder.begin() takes (sampleRateHz, bitRateBps,
//  channels, useAdts).  ADTS is on, which is exactly what the server's
//  binary-concat stitcher needs.
//
//  The encoder processes one frame at a time (1024 samples per channel),
//  so the loop in aacEncodeFile() does:
//
//      for each 1024-sample chunk:
//          convert int32 -> int16 (>> 16, saturate)
//          enc.encodeFrame(pcm16, 1024, outBuf, sizeof(outBuf), &nOut)
//          file.write(outBuf, nOut)
//
//  Buffers are static (see aacEncodeFile) to keep the Arduino task stack
//  safe -- aacEncodeFile is called 4 frames deep in the call chain.
// =============================================================================


// =============================================================================
//  Public entry point -- aacEncodeFile(wavPath, aacPath)
//
//  Reads the whole WAV, encodes it as ADTS-framed AAC-LC via vo-aacenc,
//  and writes the result.  Returns true on success.
//
//  Input  : RIFF/WAVE PCM int32 mono @ SAMPLE_RATE Hz (see aac_encoder.h).
//  Output : AAC-LC mono @ AAC_SAMPLE_RATE Hz, AAC_BITRATE_BPS CBR,
//           each frame wrapped in a 7-byte ADTS header (vo-aacenc does
//           this when useAdts=true at begin()).
//
//  Tail handling: if the WAV size is not a multiple of the encoder frame
//  size, we pad the trailing partial frame with silence and encode one
//  more frame.  This keeps re-encodes of crash-recovery chunks producing
//  a self-delimiting ADTS stream.
// =============================================================================
bool aacEncodeFile(const char *wavPath, const char *aacPath) {
  Serial.printf("[ENC ] open %s\n", wavPath);
  File in = SD.open(wavPath, FILE_READ);
  if (!in) {
    Serial.printf("[ENC ] cannot open input %s\n", wavPath);
    return false;
  }
  uint32_t dataOffset = 0, dataSize = 0;
  if (!wavFindDataOffset(in, &dataOffset, &dataSize)) {
    Serial.printf("[ENC ] malformed WAV (no data chunk): %s\n", wavPath);
    in.close();
    return false;
  }
  if (dataSize == 0) {
    Serial.printf("[ENC ] zero-length WAV data chunk: %s\n", wavPath);
    in.close();
    return false;
  }

  Serial.printf("[ENC ] open %s (data=%u bytes @ %u)\n",
                aacPath, (unsigned)dataSize, (unsigned)dataOffset);
  File out = SD.open(aacPath, FILE_WRITE);
  if (!out) {
    Serial.printf("[ENC ] cannot open output %s\n", aacPath);
    in.close();
    return false;
  }

  // All scratch buffers are static to keep them off the 8 KB Arduino
  // task stack -- aacEncodeFile is 4 frames deep in the call chain.
  static int32_t raw32[VOAACEncoder::kFrameSamplesPerChannel];
  static int16_t pcm16[VOAACEncoder::kFrameSamplesPerChannel];
  static uint8_t aacOut[AAC_OUT_BUF_BYTES];

  VOAACEncoder enc;
  if (!enc.begin(AAC_SAMPLE_RATE, AAC_BITRATE_BPS, AAC_CHANNELS,
                 /*useAdts=*/ true)) {
    Serial.println("[ENC ] vo-aacenc begin() failed");
    out.close();
    in.close();
    return false;
  }
  in.seek(dataOffset);

  // ---- full frames ----
  const size_t frameInts   = VOAACEncoder::kFrameSamplesPerChannel;
  const size_t frameBytes  = frameInts * sizeof(int32_t);
  uint32_t      bytesLeft  = dataSize;

  while (bytesLeft >= (uint32_t)frameBytes) {
    size_t n = in.readBytes((char*)raw32, frameBytes);
    if (n < frameBytes) break;        // truncated WAV: drop to tail handler
    bytesLeft -= n;

    // int32 -> int16.  >> 16 with saturation.  The encoder requires 16-bit
    // PCM; V0.4 records int32 right-justified in its 32-bit I2S slot, so
    // >> 16 yields the correct magnitude (high word).
    for (size_t i = 0; i < frameInts; i++) {
      pcm16[i] = i32_to_i16(raw32[i] >> 16);
    }

    size_t outBytes = 0;
    if (!enc.encodeFrame(pcm16, frameInts,
                         aacOut, sizeof(aacOut), outBytes)) {
      Serial.printf("[ENC ] encodeFrame failed on full frame in %s\n", wavPath);
      enc.end();
      out.close();
      in.close();
      return false;
    }
    if (outBytes > 0) {
      if (out.write(aacOut, outBytes) != outBytes) {
        Serial.printf("[ENC ] SD write short at %s\n", aacPath);
        enc.end();
        out.close();
        in.close();
        return false;
      }
    }
  }

  // ---- tail frame: zero-pad remaining samples and emit one silence-padded
  //                  frame so the output stays ADTS-self-delimiting. ----
  if (bytesLeft > 0 || dataSize > 0) {
    (void)in.readBytes((char*)pcm16, bytesLeft);  // anything that fits; rest is zero
    for (size_t i = bytesLeft / sizeof(int16_t); i < frameInts; i++) {
      pcm16[i] = 0;
    }

    size_t outBytes = 0;
    if (!enc.encodeFrame(pcm16, frameInts,
                         aacOut, sizeof(aacOut), outBytes)) {
      Serial.printf("[ENC ] tail-pad encodeFrame failed\n");
      enc.end();
      out.close();
      in.close();
      return false;
    }
    if (outBytes > 0) {
      if (out.write(aacOut, outBytes) != outBytes) {
        Serial.printf("[ENC ] SD write short on tail\n");
        enc.end();
        out.close();
        in.close();
        return false;
      }
    }
  }

  enc.end();
  out.flush();
  out.close();
  in.close();
  return true;
}