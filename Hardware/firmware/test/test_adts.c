// Host test for main/adts.c (power-loss repair of AAC chunks).
//
// The tail-based scan must give exactly the answer of a plain walk over
// every frame header from the start, for every way a power cut can end a
// file.  Runs on a synthetic stream whose payloads are full of fake 0xFFF1
// sync words, plus any real .aac chunks passed as arguments.
//
//   uv run --no-project --with ziglang python -m ziglang cc -O2 -Wall -Wextra \
//       -I ../main test_adts.c ../main/adts.c -o test_adts.exe
//   ./test_adts.exe [chunk.aac ...]
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "adts.h"

static int checks, failures;

// The original algorithm: sync + length only, from offset 0.
static long reference(const uint8_t *b, long size) {
    long pos = 0;
    while (pos + 7 <= size) {
        const uint8_t *h = b + pos;
        if (h[0] != 0xFF || (h[1] & 0xF6) != 0xF0) break;
        long len = ((long)(h[3] & 0x03) << 11) | ((long)h[4] << 3) | (h[5] >> 5);
        if (len < 7 || pos + len > size) break;
        pos += len;
    }
    return pos;
}

static void expect(const char *what, const uint8_t *d, long size) {
    FILE *f = fopen("test_adts.tmp", "wb");
    fwrite(d, 1, size, f);
    fclose(f);
    f = fopen("test_adts.tmp", "rb");
    long got = adts_complete_length(f, size);
    fclose(f);
    long want = reference(d, size);
    checks++;
    if (got != want) {
        failures++;
        printf("FAIL %s: size %ld, want %ld, got %ld\n", what, size, want, got);
    }
}

static long rnd(long lo, long hi) { return lo + rand() % (hi - lo + 1); }

// ADTS stream like the encoder's (AAC-LC, 22.05 kHz, mono, no CRC).
static long synth(uint8_t *d, long cap) {
    long pos = 0;
    while (pos + 700 < cap) {
        long len = rnd(100, 600);
        uint8_t *h = d + pos;
        h[0] = 0xFF;
        h[1] = 0xF1;                                    // MPEG-4, no CRC
        h[2] = (1 << 6) | (7 << 2) | 0;                 // LC, 22050 Hz, ch bit 2
        h[3] = (1 << 6) | (uint8_t)((len >> 11) & 0x03);   // mono
        h[4] = (uint8_t)(len >> 3);
        h[5] = (uint8_t)(((len & 7) << 5) | 0x1F);
        h[6] = 0xFC;
        for (long i = 7; i < len; i++) d[pos + i] = (uint8_t)rand();
        for (int k = 0; k < 3; k++) {                   // fake sync words
            long at = pos + rnd(7, len - 8);
            d[at] = 0xFF;
            d[at + 1] = 0xF1;
        }
        pos += len;
    }
    return pos;
}

static void cuts(const char *name, uint8_t *d, long size) {
    printf("%s: %ld bytes\n", name, size);
    expect("intact", d, size);
    for (int i = 0; i < 300; i++) expect("random cut", d, rnd(7, size));
    expect("cut on a frame boundary", d, reference(d, size / 2));
    memcpy(d + size, "\xFF\xF1\x50", 3);
    expect("partial header appended", d, size + 3);
    for (long n = 7; n < 70000 && n < size; n += 997) expect("short file", d, n);
}

int main(int argc, char **argv) {
    srand(12345);
    long cap = 2500 * 1024;
    uint8_t *d = malloc(cap + 64);
    long size = synth(d, cap);
    cuts("synthetic stream", d, size);

    // Valid start, garbage tail: no boundary in the tail window -> slow walk.
    memset(d + 150000, 0, 70000);
    expect("garbage tail", d, 220000);
    memset(d, 0, 16);
    expect("not ADTS", d, 16);
    free(d);

    for (int a = 1; a < argc; a++) {
        FILE *f = fopen(argv[a], "rb");
        if (!f) { printf("cannot open %s\n", argv[a]); return 2; }
        fseek(f, 0, SEEK_END);
        long n = ftell(f);
        fseek(f, 0, SEEK_SET);
        uint8_t *b = malloc(n + 64);
        if (fread(b, 1, n, f) != (size_t)n) return 2;
        fclose(f);
        cuts(argv[a], b, n);
        free(b);
    }
    remove("test_adts.tmp");
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
