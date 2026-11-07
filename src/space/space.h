/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * space.h — the integer spatial module (space/SPEC.md,  to ): the
 * library's voices placed in a listener-centred space and rendered to
 * channels. Offline stereo first . Integer only: no floating
 * point, no libm, no heap, no mutable global state; bit-exact across
 * targets.
 *
 * Units : positions in mm, x right, y forward, z up, the listener at
 * the origin facing +y. Gains are Q1.15 (32768 = 1.0), recomputed at each
 * block boundary from the position and ramped linearly across the block
 * . A voice is a source . A voice moves in straight lines
 * between block boundaries : its position at boundary b is a
 * function of b alone, so chunking and seeking never change it.
 */
#ifndef SP_SPACE_H
#define SP_SPACE_H

#include <stdint.h>
#include "fm_a1.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SP_MAX_VOICES 64
#define SP_MAX_BLOCK  128
#define SP_POS_MAX    16777216   /* 2^24 mm per axis: the bound the text boundary checks  */

typedef struct {
    int32_t  x, y, z;            /* mm: the position, or the end of the move */
    int32_t  x0, y0, z0;         /* the move's start, its position at block b0 */
    uint32_t b0, b1;             /* the move's first block and the block it ends at; b1 <= b0: no move */
    uint32_t gt[2], gp[2];       /* this block's targets and the last block's, Q1.15, left then right */
} sp_voice_t;

struct fx;

typedef struct sp_stereo {
    uint32_t   n;                /* the absolute sample counter */
    uint32_t   k, log2k;         /* the block: 32, 64 or 128 samples */
    uint32_t   near_mm;          /* the near-field radius */
    sp_voice_t v[SP_MAX_VOICES];
    int32_t    buf[SP_MAX_BLOCK];      /* the render's scratch: one voice's block in Q2.30, and the */
    int64_t    acc[2][SP_MAX_BLOCK];   /* channel accumulators; in the state, so the call's frame stays small */
    /* The effects (fx/SPEC.md, , ): NULL, as sp_stereo_init leaves
     * it, renders as without them. Set after init to an fx state made with
     * this block and 2 channels at the counter's 0: each voice's block goes
     * through fx_voice_process (insert before sends/pan), and the accumulators
     * through fx's returns and master chain instead of the plain rounding.
     * With crusher enabled skipped engines supply zeros to clock every
     * declared voice's insert; the inactive engine itself remains untouched. */
    struct fx *fx;
} sp_stereo_t;

/* block 32, 64 or 128 and near 1 to SP_POS_MAX, else -1 and the state is
 * unusable. Every voice starts at (0, 1000, 0), 1 m ahead at the centre,
 * with the counter at 0. */
int      sp_stereo_init(sp_stereo_t *sp, uint32_t block, uint32_t near_mm);
/* The voice's position from the next block boundary on , ending any
 * move. Coordinates within -SP_POS_MAX to SP_POS_MAX by contract; voices at
 * or past SP_MAX_VOICES are ignored. */
void     sp_stereo_set_pos(sp_stereo_t *sp, uint32_t voice, int32_t x, int32_t y, int32_t z);
/* A move : from the position the voice has at the next boundary b0
 * (the first at or after the counter; a move in flight is read there) in a
 * straight line to (x, y, z), reached at boundary b1, the first at or after
 * sample end. At boundary b, b0 <= b < b1, each coordinate is
 * p0 + trunc((p1 - p0) * (b - b0) / (b1 - b0)), truncated toward zero; p1
 * from b1 on. b1 <= b0 (end before the next boundary): (x, y, z) from b0
 * on, as sp_stereo_set_pos. The next set_pos or move on the voice replaces
 * it. Same contracts as sp_stereo_set_pos. */
void     sp_stereo_move(sp_stereo_t *sp, uint32_t voice, int32_t x, int32_t y, int32_t z, uint32_t end);
/* Renders voices 0 to count - 1 (count at most SP_MAX_VOICES, the same
 * voices and count from call to call) for frames frames into 2 * frames
 * int16, left first, and returns the clipped channel samples. Any frames
 * count: the render splits at block boundaries anchored to the counter, so
 * chunking never changes the bits. */
uint32_t sp_stereo_render(sp_stereo_t *sp, fm_voice_t *voices, uint32_t count, int16_t *out, uint32_t frames);

/* Test surface: floor(sqrt(v)); n / d for d >= 1 ('s one division). */
uint32_t sp_isqrt64(uint64_t v);
uint32_t sp_udiv64_32(uint64_t n, uint32_t d);
/* Shared with field.c: one coordinate of the voice's position at boundary
 * b, from the move's start p0 and end p1 (SPEC section 4). */
int32_t  sp_coord_at(const sp_voice_t *v, uint32_t b, int32_t p0, int32_t p1);

#ifdef __cplusplus
}
#endif

#endif /* SP_SPACE_H */
