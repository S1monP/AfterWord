// =============================================================================
//  adts.h  --  ADTS (AAC) frame scanning for power-loss repair.
//
//  Plain C with no ESP-IDF dependencies, so test/test_adts.c can build and
//  run it on a PC.
// =============================================================================
#pragma once

#include <stdio.h>

// Length of the leading run of complete ADTS frames in the open file `f`
// of `size` bytes: everything after it is a frame cut off by power loss.
// 0 means no complete frame (or not ADTS at all).
long adts_complete_length(FILE *f, long size);
