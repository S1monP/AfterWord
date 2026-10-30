// =============================================================================
//  adts.c  --  find the end of the last complete ADTS frame in a chunk.
//
//  Only the end of a chunk can be cut off, so instead of walking every frame
//  header from the start (~6500 small reads for a 5-minute chunk, at every
//  boot, for every folder) read the last RECOVER_TAIL_BYTES, find a frame
//  boundary there and walk from it.  A boundary is a header that starts a
//  run of RECOVER_CHAIN valid headers whose fixed fields (profile, sample
//  rate, channels) match the file's first frame; a stray 0xFFF in audio data
//  does not survive that.  Short files, or a tail without such a run, fall
//  back to the walk from the start.
// =============================================================================
#include "adts.h"

#include <stdint.h>
#include <stdlib.h>

#define RECOVER_TAIL_BYTES  (64 * 1024)     // malloc puts this in PSRAM (>16 KB)
#define RECOVER_CHAIN       4

// Frame length if `h` is an ADTS header with the stream's fixed fields
// (`ref` = the first header), else 0.
static long frame_len(const uint8_t *h, const uint8_t *ref) {
    if (h[0] != 0xFF || (h[1] & 0xF6) != 0xF0) return 0;              // sync
    if (ref && (h[1] != ref[1] || h[2] != ref[2] || (h[3] & 0xC0) != (ref[3] & 0xC0)))
        return 0;
    long len = ((long)(h[3] & 0x03) << 11) | ((long)h[4] << 3) | (h[5] >> 5);
    return len >= 7 ? len : 0;
}

// Complete frames from `pos` in buf[0..n): end of the last one.
static long walk(const uint8_t *buf, long n, long pos, const uint8_t *ref) {
    while (pos + 7 <= n) {
        long len = frame_len(buf + pos, ref);
        if (len == 0 || pos + len > n) break;                 // lost sync / cut off
        pos += len;
    }
    return pos;
}

// Offset in buf[0..n) where RECOVER_CHAIN valid frames start, or -1.
static long find_boundary(const uint8_t *buf, long n, const uint8_t *ref) {
    for (long p = 0; p + 7 <= n; p++) {
        long q = p;
        int k = 0;
        while (k < RECOVER_CHAIN && q + 7 <= n) {
            long len = frame_len(buf + q, ref);
            if (len == 0 || q + len > n) break;
            q += len;
            k++;
        }
        if (k == RECOVER_CHAIN) return p;
    }
    return -1;
}

long adts_complete_length(FILE *f, long size) {
    uint8_t ref[7];
    if (size < 7 || fseek(f, 0, SEEK_SET) != 0 || fread(ref, 1, 7, f) != 7 ||
        frame_len(ref, NULL) == 0) {
        return 0;
    }
    long start = size > RECOVER_TAIL_BYTES ? size - RECOVER_TAIL_BYTES : 0;
    long n = size - start;
    // Without the buffer, take the slow walk below: never answer 0 here,
    // since the caller deletes a file with no complete frame.
    uint8_t *buf = malloc(n);
    long good = -1;
    if (buf && fseek(f, start, SEEK_SET) == 0 && fread(buf, 1, n, f) == (size_t)n) {
        long p = start == 0 ? 0 : find_boundary(buf, n, ref);
        if (p >= 0) good = start + walk(buf, n, p, ref);
    }
    free(buf);
    if (good >= 0) return good;

    long pos = 0;
    uint8_t h[7];
    while (pos + 7 <= size) {
        if (fseek(f, pos, SEEK_SET) != 0 || fread(h, 1, 7, f) != 7) break;
        long len = frame_len(h, ref);
        if (len == 0 || pos + len > size) break;
        pos += len;
    }
    return pos;
}
