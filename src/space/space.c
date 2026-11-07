/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * space.c — the integer spatial module, offline stereo (docs/INSTRUMENT.md).
 *
 * Per block boundary and voice (control rate): the position, a straight
 * line between the move's ends by the 64/32 division while the voice
 * moves (section 4), then r = isqrt(x^2 + y^2 + z^2), ux = x /
 * max(r, near) in Q2.30 by the one 64/32 division, the
 * constant-power pan by integer square roots, the 1/r distance gain by a
 * second division, the targets in Q1.15 (sections 2 and 3 of the SPEC).
 * Per sample and voice: the gain ramp, an add and a shift per channel, and
 * a 32x32->64 multiply-accumulate per channel. Per sample and channel: one
 * rounding, one saturation, one clip count (section 4).
 */
#include "space.h"
#include "../fx/fx.h"

/* floor(sqrt(v)), a digit at a time; r + bit stays below 2^64 for every v. */
uint32_t sp_isqrt64(uint64_t v)
{
    uint64_t r = 0, bit = (uint64_t)1 << 62;
    while (bit > v) bit >>= 2;
    while (bit) {
        if (v >= r + bit) { v -= r + bit; r = (r >> 1) + bit; }
        else r >>= 1;
        bit >>= 2;
    }
    return (uint32_t)r;
}

uint32_t sp_udiv64_32(uint64_t n, uint32_t d)
{
    return (uint32_t)(n / d);
}

int sp_stereo_init(sp_stereo_t *sp, uint32_t block, uint32_t near_mm)
{
    uint32_t i;
    if ((block != 32 && block != 64 && block != 128) || near_mm == 0 || near_mm > SP_POS_MAX) return -1;
    sp->n = 0;
    sp->k = block;
    sp->log2k = block == 32 ? 5 : block == 64 ? 6 : 7;
    sp->near_mm = near_mm;
    sp->fx = NULL;
    for (i = 0; i < SP_MAX_VOICES; i++) {
        sp_stereo_set_pos(sp, i, 0, 1000, 0);
        sp->v[i].gt[0] = sp->v[i].gt[1] = sp->v[i].gp[0] = sp->v[i].gp[1] = 0;
    }
    return 0;
}

void sp_stereo_set_pos(sp_stereo_t *sp, uint32_t voice, int32_t x, int32_t y, int32_t z)
{
    sp_voice_t *v;
    if (voice >= SP_MAX_VOICES) return;
    v = &sp->v[voice];
    v->x = v->x0 = x;
    v->y = v->y0 = y;
    v->z = v->z0 = z;
    v->b0 = v->b1 = 0;
}

/* a + trunc((b - a) * i / d) toward zero, i < d: |b - a| < 2^25 keeps the
 * product below 2^57 (the one 64/32 division); the quotient is below
 * 2^25. */
static int32_t sp_lerp(int32_t a, int32_t b, uint32_t i, uint32_t d)
{
    int64_t  delta = (int64_t)b - a;
    uint64_t q = sp_udiv64_32((uint64_t)(delta < 0 ? -delta : delta) * i, d);
    return (int32_t)(a + (delta < 0 ? -(int64_t)q : (int64_t)q));
}

/* One coordinate of the voice's position at boundary b (SPEC section 4),
 * from its start p0 and end p1: the end once b reaches b1, or when there is
 * no move; the start before b0; else the straight line between them. Three
 * scalars rather than an array: a local array draws a stack canary and its
 * __stack_chk_fail import from a hardening compiler, which the gate's
 * import check refuses. */
int32_t sp_coord_at(const sp_voice_t *v, uint32_t b, int32_t p0, int32_t p1)
{
    if (v->b1 <= v->b0 || b >= v->b1) return p1;
    if (b <= v->b0) return p0;
    return sp_lerp(p0, p1, b - v->b0, v->b1 - v->b0);
}

void sp_stereo_move(sp_stereo_t *sp, uint32_t voice, int32_t x, int32_t y, int32_t z, uint32_t end)
{
    sp_voice_t *v;
    int32_t x0, y0, z0;
    uint32_t b0;
    if (voice >= SP_MAX_VOICES) return;
    v  = &sp->v[voice];
    b0 = (uint32_t)(((uint64_t)sp->n + sp->k - 1u) >> sp->log2k);     /* the first boundary at or after the counter */
    x0 = sp_coord_at(v, b0, v->x0, v->x);                             /* where the voice is, or will be, at b0 */
    y0 = sp_coord_at(v, b0, v->y0, v->y);
    z0 = sp_coord_at(v, b0, v->z0, v->z);
    v->x0 = x0;
    v->y0 = y0;
    v->z0 = z0;
    v->x  = x;
    v->y  = y;
    v->z  = z;
    v->b0 = b0;
    v->b1 = (uint32_t)(((uint64_t)end + sp->k - 1u) >> sp->log2k);    /* the first boundary at or after end */
}

/* The targets of one voice from its position: SPEC sections 2 and 3. */
static void sp_targets(const sp_stereo_t *sp, sp_voice_t *v, int32_t x, int32_t y, int32_t z)
{
    uint64_t sq = (uint64_t)((int64_t)x * x) + (uint64_t)((int64_t)y * y) + (uint64_t)((int64_t)z * z);
    uint32_t r  = sp_isqrt64(sq);                                     /* < 2^25 */
    uint32_t m  = r > sp->near_mm ? r : sp->near_mm;                  /* >= 1 */
    uint32_t ax = (uint32_t)(x < 0 ? -(int64_t)x : (int64_t)x);
    int32_t  ux = (int32_t)sp_udiv64_32((uint64_t)ax << 30, m);       /* |x| <= r <= m: at most 1 << 30 */
    uint32_t panl, panr, gd;
    if (x < 0) ux = -ux;
    panl = sp_isqrt64((uint64_t)((int64_t)(1 << 30) - ux) >> 1);      /* sqrt((1 - ux) / 2), Q1.15 */
    panr = sp_isqrt64((uint64_t)((int64_t)(1 << 30) + ux) >> 1);      /* sqrt((1 + ux) / 2), Q1.15 */
    gd   = sp_udiv64_32((uint64_t)1000 << 15, r > 1000 ? r : 1000);   /* 1 m / max(r, 1 m), Q1.15 */
    v->gt[0] = (panl * gd + (1u << 14)) >> 15;
    v->gt[1] = (panr * gd + (1u << 14)) >> 15;
}

/* A block boundary: the last targets become the previous gains, and the
 * targets follow the positions at this block. At the start the previous
 * gains are the first targets: no fade-in. */
static void sp_boundary(sp_stereo_t *sp, uint32_t count)
{
    uint32_t i, b = sp->n >> sp->log2k;
    for (i = 0; i < count; i++) {
        sp_voice_t *v = &sp->v[i];
        v->gp[0] = v->gt[0];
        v->gp[1] = v->gt[1];
        sp_targets(sp, v, sp_coord_at(v, b, v->x0, v->x), sp_coord_at(v, b, v->y0, v->y), sp_coord_at(v, b, v->z0, v->z));
        if (sp->n == 0) { v->gp[0] = v->gt[0]; v->gp[1] = v->gt[1]; }
    }
}

uint32_t sp_stereo_render(sp_stereo_t *sp, fm_voice_t *voices, uint32_t count, int16_t *out, uint32_t frames)
{
    int32_t *buf = sp->buf;
    int64_t (*acc)[SP_MAX_BLOCK] = sp->acc;
    uint32_t clipped = 0;
    if (count > SP_MAX_VOICES) count = SP_MAX_VOICES;
    while (frames) {
        uint32_t j0 = sp->n & (sp->k - 1u), len = sp->k - j0, i, t;
        if (len > frames) len = frames;
        if (j0 == 0) sp_boundary(sp, count);
        for (t = 0; t < len; t++) { acc[0][t] = 0; acc[1][t] = 0; }
        for (i = 0; i < count; i++) {
            const sp_voice_t *v = &sp->v[i];
            int32_t dl = (int32_t)v->gt[0] - (int32_t)v->gp[0], dr = (int32_t)v->gt[1] - (int32_t)v->gp[1];
            int32_t bl = (int32_t)(v->gp[0] << sp->log2k), br = (int32_t)(v->gp[1] << sp->log2k);
            int32_t rl = dl * (int32_t)(j0 + 1u), rr = dr * (int32_t)(j0 + 1u);
            if (!fm_voice_active(&voices[i]) && !fm_voice_needs_clock(&voices[i])) {
                if (!sp->fx || !(sp->fx->on & FX_ON_CRUSH)) continue;
                for (t = 0; t < len; t++) buf[t] = 0;
            } else fm_render_q30(&voices[i], buf, len);
            if (sp->fx) fx_voice_process(sp->fx, i, buf, len);      /* insert and sends, before the pan */
            for (t = 0; t < len; t++) {
                int32_t gl = (bl + rl) >> sp->log2k, gr = (br + rr) >> sp->log2k;   /* never negative: SPEC section 4 */
                acc[0][t] += (int64_t)buf[t] * gl;
                acc[1][t] += (int64_t)buf[t] * gr;
                rl += dl;
                rr += dr;
            }
        }
        if (sp->fx) clipped += fx_master(sp->fx, &acc[0][0], SP_MAX_BLOCK, 45, out, len);   /* the returns, the master chain */
        else for (t = 0; t < len; t++) {
            uint32_t c;
            for (c = 0; c < 2u; c++) {
                int64_t s = (acc[c][t] + ((int64_t)1 << 29)) >> 30;    /* Q.45 to Q15, round half up */
                if (s > 32767) { s = 32767; clipped++; }
                else if (s < -32768) { s = -32768; clipped++; }
                out[2 * t + c] = (int16_t)s;
            }
        }
        out += 2 * len;
        sp->n += len;
        frames -= len;
    }
    return clipped;
}
