/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * field.c — the spatial module's field routes (space/SPEC.md sections 11 to
 * 16): the ambisonic encoder at orders 1 and 3, the yaw, the binaural
 * decoder, the per-voice pinna and the stereo pan law (c).
 *
 * Per block boundary and voice (control rate), when the voice's position
 * or the yaw changed: the direction by space.c's square root and 64/32
 * division, the real spherical harmonics as integer polynomials in it, the
 * yaw as one rotation per pair of harmonics, the pinna's echo delays from
 * the turned direction by an int32 CORDIC, the gains in Q1.23. Per sample
 * and voice: the pinna's few taps, then a 32x32->64 multiply-accumulate per
 * field channel with space.c's ramp. Per sample and channel: one rounding
 * and saturation; on the binaural route a 256-tap FIR per channel and the
 * ears' sums. vendor/fp_math.h gives the CORDIC's angles and gain and pi,
 * read at init; its own sincos and atan2 are not called, as they import a
 * 64-bit arithmetic shift helper the gate refuses on two targets .
 */
#include <stddef.h>
#include "field.h"
#include "../../vendor/fp_math.h"

static const int32_t  sp_rho[5] = { 1 << 21, -(1 << 22), 1 << 21, -(1 << 20), 1 << 20 };   /* Brown and Duda's rho, Q1.22 */
static const uint32_t sp_pa[5]  = { 1, 5, 5, 5, 5 };              /* their A and B, samples at 44.1 kHz (Table I) */
static const uint32_t sp_pb[5]  = { 2, 4, 7, 11, 13 };
static const uint32_t sp_dpb[5] = { 1000, 500, 500, 500, 500 };   /* their D, listeners PB and NH */
static const uint32_t sp_drd[5] = { 850, 350, 350, 350, 350 };    /* listener RD */
static const uint8_t  sp_route_nsh[5] = { 0, 4, 16, 16, 2 };       /* by SP_FIELD_*: field and output channels */
static const uint8_t  sp_route_ch[5]  = { 0, 4, 16, 2, 2 };

/* p * q in Q30, rounded half up; both int32, so one 32x32->64 multiply. */
static int32_t sp_m30(int32_t p, int32_t q)
{
    return (int32_t)(((int64_t)p * q + (1 << 29)) >> 30);
}

/* c / m in Q30, truncated toward zero, |c| <= m (space.c's ux). */
static int32_t sp_unit(int32_t c, uint32_t m)
{
    uint32_t a = (uint32_t)(c < 0 ? -(int64_t)c : (int64_t)c);
    int32_t  q = (int32_t)sp_udiv64_32((uint64_t)a << 30, m);
    return c < 0 ? -q : q;
}

/* cos and sin of a, 0 <= a <= pi/2 in Q30 radians, into f->tc and f->ts in
 * Q30: 30 rotations from (1/K, 0); |x| and |y| stay at most 2^30 + 30. */
static void sp_cordic_rot(sp_field_t *f, int32_t a)
{
    int32_t x = f->kinv, y = 0, z = a;
    int i;
    for (i = 0; i < SP_CORDIC_N; i++) {
        int32_t xs = x >> i, ys = y >> i;
        if (z >= 0) { x -= ys; y += xs; z -= f->ang[i]; }
        else        { x += ys; y -= xs; z += f->ang[i]; }
    }
    f->tc = x;
    f->ts = y;
}

/* atan(y / x) in Q30 radians, 0 to pi/2, for x, y >= 0 not both 0 and
 * |(x, y)| at most 2^30 + 4: the vector grows by K = 1.647 at most, below
 * 2^31. */
static uint32_t sp_cordic_atan(const sp_field_t *f, int32_t x, int32_t y)
{
    int32_t z = 0;
    int i;
    for (i = 0; i < SP_CORDIC_N; i++) {
        int32_t xs = x >> i, ys = y >> i;
        if (y >= 0) { x += ys; y -= xs; z += f->ang[i]; }
        else        { x -= ys; y += xs; z -= f->ang[i]; }
    }
    return z < 0 ? 0u : (uint32_t)z;
}

int sp_field_init(sp_field_t *f, const sp_field_cfg_t *cfg)
{
    uint32_t i, c, k;
    const uint32_t *d = sp_dpb;
    if (cfg->route < SP_FIELD_AMB1 || cfg->route > SP_FIELD_PANLAW) return -1;
    f->nsh = sp_route_nsh[cfg->route];                                /* a table, not a switch: the gate refuses */
    f->ch  = sp_route_ch[cfg->route];                                 /* Cortex-M0+'s halfword jump-table helper */
    if (cfg->route == SP_FIELD_BINAURAL) {
        if ((cfg->order != 1 && cfg->order != 3) || !cfg->table || cfg->table_shift < 1 || cfg->table_shift > 30) return -1;
        f->nsh = cfg->order == 1 ? 4 : 16;
    }
    if (cfg->route == SP_FIELD_PANLAW && (cfg->panlaw_q15 < 1 || cfg->panlaw_q15 > 32768 || cfg->pinna != SP_PINNA_OFF || cfg->yaw_mdeg))
        return -1;
    if (cfg->pinna < SP_PINNA_OFF || cfg->pinna > SP_PINNA_D) return -1;
    if (cfg->pinna && (cfg->fs < 1 || cfg->fs > SP_PINNA_FS_MAX)) return -1;
    if (cfg->pinna == SP_PINNA_RD) d = sp_drd;
    if (cfg->pinna == SP_PINNA_D) {
        for (i = 0; i < 5; i++)
            if (cfg->pinna_d[i] > 1000) return -1;
        d = cfg->pinna_d;
    }
    if (sp_stereo_init(&f->geo, cfg->block, cfg->near_mm)) return -1;
    f->route = cfg->route;
    f->fs = cfg->fs;
    f->pinna = cfg->pinna;
    f->w = cfg->panlaw_q15;
    f->table = cfg->table;
    f->shift = cfg->table_shift;
    f->odd = (1u << 1) | (1u << 4) | (1u << 5) | (1u << 9) | (1u << 10) | (1u << 11);
    if (f->route == SP_FIELD_BINAURAL) {
        f->dround = (uint64_t)1 << (f->shift + 14);
        f->dbias  = (uint64_t)1 << (47 - f->shift);
    }
    for (i = 0; i < 5; i++) {
        f->pd[i] = d[i];
        if (f->pinna) {                                                   /* round(A fs 2^26 / 44100): A, B at most 13 */
            f->aq[i] = sp_udiv64_32(((uint64_t)sp_pa[i] * cfg->fs << 26) + 22050u, 44100u);
            f->bq[i] = sp_udiv64_32(((uint64_t)sp_pb[i] * cfg->fs << 26) + 22050u, 44100u);
        } else f->aq[i] = f->bq[i] = 0;
    }
    f->sq3    = (int32_t)sp_isqrt64((uint64_t)3 << 60);
    f->sq3_2  = (int32_t)sp_isqrt64((uint64_t)3 << 58);
    f->sq58   = (int32_t)sp_isqrt64((uint64_t)5 << 57);
    f->sq38   = (int32_t)sp_isqrt64((uint64_t)3 << 57);
    f->sq15_2 = (int32_t)sp_isqrt64((uint64_t)15 << 58);
    for (i = 0; i < SP_CORDIC_N; i++) f->ang[i] = fp_q48_to_q30(fp_cordic_angles[i]);
    f->kinv   = fp_q48_to_q30(fp_cordic_gain);
    f->halfpi = (uint32_t)fp_q48_to_q30(FP_PI >> 1);
    f->pi     = 2u * f->halfpi;
    for (i = 0; i < SP_MAX_VOICES + 2u * FX_RETURNS; i++) {
        sp_fvoice_t *v = i < SP_MAX_VOICES ? &f->fv[i] : &f->wet[(i - SP_MAX_VOICES) / 2u][i & 1u];
        for (c = 0; c < SP_FIELD_MAX_CH; c++) v->gt[c] = v->gp[c] = 0;
        for (k = 0; k < SP_PINNA_TAPS; k++) v->ht[k] = v->hp[k] = v->hs[k] = 0;
        for (k = 0; k < SP_PINNA_HIST; k++) v->hist[k] = 0;
        v->ntap = 0;
        v->quiet = SP_PINNA_HIST;
        v->cvalid = 0;
    }
    for (c = 0; c < SP_FIELD_MAX_CH; c++)
        for (k = 0; k < 2 * SP_FIELD_TAPS; k++) f->hist[c][k] = 0;
    sp_field_set_yaw(f, cfg->yaw_mdeg);
    return 0;
}

int sp_field_attach_fx(sp_field_t *f, fx_t *fx)
{
    uint32_t c, j;
    uint64_t h = 0;
    if (f->geo.n) return -1;
    if (!fx) { f->geo.fx = NULL; return 0; }
    if (f->route < SP_FIELD_AMB1 || f->route > SP_FIELD_BINAURAL
        || !fx->field_mode || fx->bus_ch != 2u || fx->fs != f->fs
        || fx->k != f->geo.k || fx->ch != f->ch || fx->n || fx->open) return -1;
    if (f->route == SP_FIELD_BINAURAL) {
        /* One ear's all-channel L1 norm also bounds the mirrored ear.
         * The accepted norm keeps wide decoded Q45 ears below 2^52. */
        for (c = 0; c < f->nsh; c++) for (j = 0; j < SP_FIELD_TAPS; j++) {
            int32_t tap = f->table[(size_t)c * SP_FIELD_TAPS + j];
            h += (uint32_t)(tap < 0 ? -tap : tap);
        }
        if (h > (((uint64_t)1 << (f->shift + 6u)) - 1u)) return -1;
    }
    f->geo.fx = fx;
    return 0;
}

void sp_field_set_yaw(sp_field_t *f, int32_t mdeg)
{
    uint32_t a = (uint32_t)(mdeg < 0 ? -(int64_t)mdeg : (int64_t)mdeg), r, m;
    r = a - 360000u * sp_udiv64_32(a, 360000u);                       /* |yaw| mod 360 degrees */
    f->yaw = mdeg;
    for (m = 1; m <= 3; m++) {
        uint32_t e = r * m, q = 0;                                    /* below 1080000 */
        int32_t c0 = 1 << 30, s0 = 0;
        while (e >= 360000u) e -= 360000u;
        if (mdeg < 0 && e) e = 360000u - e;                           /* m * yaw mod 360 degrees, 0 to 359999 */
        while (e >= 90000u) { e -= 90000u; q++; }                     /* the quadrant, then exact at its axes */
        if (e) {
            sp_cordic_rot(f, (int32_t)sp_udiv64_32((uint64_t)e * f->halfpi, 90000u));
            c0 = f->tc;
            s0 = f->ts;
        }
        f->cm[m] = q == 0 ? c0 : q == 1 ? -s0 : q == 2 ? -c0 : s0;
        f->sm[m] = q == 0 ? s0 : q == 1 ? c0 : q == 2 ? -s0 : -c0;
    }
}

/* The real spherical harmonics (ACN, SN3D) of the AmbiX vector (a, b, c),
 * |(a, b, c)| <= 1 in Q30, into f->sh: the homogeneous polynomials, so
 * order l scales with the vector's length to the l (SPEC section 11).
 * Every sum is at most 2^31 before its halving or its constant (11 and 13
 * peak at 1.38, 6 and 12 at 2 before the halving), formed in int64. */
static void sp_sh(sp_field_t *f, int32_t a, int32_t b, int32_t c)
{
    int32_t *y = f->sh, aa, bb, cc, ab;
    y[0] = 1 << 30;
    y[1] = b;
    y[2] = c;
    y[3] = a;
    if (f->nsh < 16) return;
    aa = sp_m30(a, a);
    bb = sp_m30(b, b);
    cc = sp_m30(c, c);
    ab = sp_m30(a, b);
    y[4]  = sp_m30(f->sq3, ab);
    y[5]  = sp_m30(f->sq3, sp_m30(b, c));
    y[6]  = (int32_t)(((int64_t)2 * cc - aa - bb + 1) >> 1);
    y[7]  = sp_m30(f->sq3, sp_m30(a, c));
    y[8]  = sp_m30(f->sq3_2, aa - bb);
    y[9]  = sp_m30(f->sq58, (int32_t)((int64_t)3 * sp_m30(b, aa) - sp_m30(b, bb)));
    y[10] = sp_m30(f->sq15_2, (int32_t)((int64_t)2 * sp_m30(ab, c)));
    y[11] = sp_m30(f->sq38, (int32_t)((int64_t)4 * sp_m30(b, cc) - sp_m30(b, aa) - sp_m30(b, bb)));
    y[12] = (int32_t)(((int64_t)2 * sp_m30(c, cc) - (int64_t)3 * sp_m30(c, aa) - (int64_t)3 * sp_m30(c, bb) + 1) >> 1);
    y[13] = sp_m30(f->sq38, (int32_t)((int64_t)4 * sp_m30(a, cc) - sp_m30(a, aa) - sp_m30(a, bb)));
    y[14] = sp_m30(f->sq15_2, sp_m30(c, aa) - sp_m30(c, bb));
    y[15] = sp_m30(f->sq58, (int32_t)((int64_t)sp_m30(a, aa) - (int64_t)3 * sp_m30(a, bb)));
}

/* One pair of harmonics turned by m * yaw: (C, S) = (cos m phi, sin m phi)
 * to those of phi + yaw (SPEC section 13). */
static void sp_turn(int32_t *y, int ci, int si, int32_t cm, int32_t sm)
{
    int32_t c = y[ci], s = y[si];
    y[ci] = (int32_t)(((int64_t)c * cm - (int64_t)s * sm + (1 << 29)) >> 30);
    y[si] = (int32_t)(((int64_t)s * cm + (int64_t)c * sm + (1 << 29)) >> 30);
}

/* Brown and Duda's pinna at the turned direction (SPEC section 14): head
 * axes from the order-1 harmonics, the lateral angle's cos(theta / 2) and
 * the elevation's alpha = 90 degrees - phi, five echoes at
 * tau = A cos(theta / 2) sin(D alpha) + B samples (at 44.1 kHz, scaled to
 * the rate) in Q6.26, each echo's rho shared between the two taps round it
 * by linear interpolation. Behind (phi beyond 90 degrees): the direct path
 * alone. */
static void sp_pinna_taps(sp_field_t *f, sp_fvoice_t *v)
{
    int32_t  hx = -f->sh[1], hy = f->sh[3], hz = f->sh[2];
    int32_t *h = v->ht;
    uint64_t yz = (uint64_t)((int64_t)hy * hy) + (uint64_t)((int64_t)hz * hz);
    uint32_t hm, cth = 1u << 30, alpha = f->halfpi, c2, n, k;
    for (k = 0; k < SP_PINNA_TAPS; k++) h[k] = 0;
    h[0] = 1 << 22;
    if (hy < 0) return;
    hm = sp_isqrt64(yz + (uint64_t)((int64_t)hx * hx));
    if (hm) {
        cth = sp_udiv64_32((uint64_t)sp_isqrt64(yz) << 30, hm);      /* cos theta, at most 1 */
        if (hz >= 0) { if (yz) alpha = sp_cordic_atan(f, hz, hy); }
        else alpha = f->pi - sp_cordic_atan(f, -hz, hy);
    }
    c2 = sp_isqrt64((uint64_t)((1u << 30) + cth) << 29);             /* cos(theta / 2) = sqrt((1 + cos theta) / 2) */
    for (n = 0; n < 5; n++) {
        uint32_t beta = sp_udiv64_32((uint64_t)alpha * f->pd[n], 1000u), p, tau, i, fr;
        int32_t  s, w1;
        if (beta > f->halfpi) beta = f->pi - beta;
        sp_cordic_rot(f, (int32_t)beta);
        s = f->ts < 0 ? 0 : f->ts;
        p   = (uint32_t)(((uint64_t)f->aq[n] * c2 + (1u << 29)) >> 30);          /* A cos(theta / 2), Q6.26 */
        tau = f->bq[n] + (uint32_t)(((uint64_t)p * (uint32_t)s + (1u << 29)) >> 30);
        i  = tau >> 26;                                               /* at most 19 at 48 kHz */
        fr = tau & ((1u << 26) - 1u);
        w1 = (int32_t)(((int64_t)sp_rho[n] * (int32_t)fr + (1 << 25)) >> 26);
        h[i]     += sp_rho[n] - w1;
        h[i + 1] += w1;
    }
}

/* Complete the existing SH -> yaw -> pinna -> clamped gain law. */
static void sp_field_target_finish(sp_field_t *f, sp_fvoice_t *v, uint32_t gd)
{
    uint32_t c;
    if (f->yaw) {
        sp_turn(f->sh, 3, 1, f->cm[1], f->sm[1]);
        if (f->nsh == 16) {
            sp_turn(f->sh, 7, 5, f->cm[1], f->sm[1]);
            sp_turn(f->sh, 8, 4, f->cm[2], f->sm[2]);
            sp_turn(f->sh, 13, 11, f->cm[1], f->sm[1]);
            sp_turn(f->sh, 14, 10, f->cm[2], f->sm[2]);
            sp_turn(f->sh, 15, 9, f->cm[3], f->sm[3]);
        }
    }
    if (f->pinna) sp_pinna_taps(f, v);
    for (c = 0; c < f->nsh; c++) {
        int64_t g = ((int64_t)f->sh[c] * (int32_t)gd + (1 << 29)) >> 30;
        v->gt[c] = (int32_t)(g > (1 << 23) ? (1 << 23) : g < -(1 << 23) ? -(1 << 23) : g);
    }
}

/* The targets of one voice from its position at this boundary. */
static void sp_field_targets(sp_field_t *f, sp_fvoice_t *v, int32_t x, int32_t y, int32_t z)
{
    uint64_t sq = (uint64_t)((int64_t)x * x) + (uint64_t)((int64_t)y * y) + (uint64_t)((int64_t)z * z);
    uint32_t r = sp_isqrt64(sq);
    uint32_t rc = r + ((uint64_t)r * r < sq);
    uint32_t m = rc > f->geo.near_mm ? rc : f->geo.near_mm;
    uint32_t gd = sp_udiv64_32((uint64_t)1000 << 23, r > 1000 ? r : 1000);
    sp_sh(f, sp_unit(y, m), sp_unit(-x, m), sp_unit(z, m));
    sp_field_target_finish(f, v, gd);
}

/* : exact +/-30 degree world directions at 1m, unity distance gain.
 * Near-field order scaling uses signed magnitude floor, not rounded mm. */
static void sp_wet_targets(sp_field_t *f, sp_fvoice_t *v, uint32_t ear)
{
    uint32_t m = f->geo.near_mm > 1000u ? f->geo.near_mm : 1000u;
    int32_t a = (int32_t)sp_udiv64_32((uint64_t)929887696u * 1000u, m);
    int32_t b = (int32_t)sp_udiv64_32((uint64_t)536870912u * 1000u, m);
    sp_sh(f, a, ear ? -b : b, 0);
    sp_field_target_finish(f, v, 1u << 23);
}

/* The pan law (c)'s targets (SPEC section 15): space.c's law with the
 * pan's ux narrowed by W, so the far ear keeps (1 - W) / 2 of the power. */
static void sp_panlaw_targets(const sp_field_t *f, sp_fvoice_t *v, int32_t x, int32_t y, int32_t z)
{
    uint64_t sq = (uint64_t)((int64_t)x * x) + (uint64_t)((int64_t)y * y) + (uint64_t)((int64_t)z * z);
    uint32_t r  = sp_isqrt64(sq);
    uint32_t m  = r > f->geo.near_mm ? r : f->geo.near_mm;
    uint32_t ax = (uint32_t)(x < 0 ? -(int64_t)x : (int64_t)x);
    uint32_t um = sp_udiv64_32((uint64_t)ax << 30, m);               /* |ux|, at most 1 << 30 */
    int32_t  ux = (int32_t)(((uint64_t)um * f->w) >> 15);            /* W = 32768: unchanged */
    uint32_t panl, panr, gd;
    if (x < 0) ux = -ux;
    panl = sp_isqrt64((uint64_t)((int64_t)(1 << 30) - ux) >> 1);
    panr = sp_isqrt64((uint64_t)((int64_t)(1 << 30) + ux) >> 1);
    gd   = sp_udiv64_32((uint64_t)1000 << 15, r > 1000 ? r : 1000);
    v->gt[0] = (int32_t)((panl * gd + (1u << 14)) >> 15);
    v->gt[1] = (int32_t)((panr * gd + (1u << 14)) >> 15);
}

/* A block boundary: the last targets become the previous, the targets
 * follow the positions and the yaw at this block (recomputed only when
 * they changed: the same bits), and at the start the previous are the
 * first targets. Then the pinna's live taps. */
static void sp_field_boundary(sp_field_t *f, uint32_t count)
{
    sp_stereo_t *g = &f->geo;
    uint32_t i, c, k, b = g->n >> g->log2k;
    for (i = 0; i < count; i++) {
        const sp_voice_t *gv = &g->v[i];
        sp_fvoice_t *v = &f->fv[i];
        int32_t x = sp_coord_at(gv, b, gv->x0, gv->x), y = sp_coord_at(gv, b, gv->y0, gv->y), z = sp_coord_at(gv, b, gv->z0, gv->z);
        for (c = 0; c < SP_FIELD_MAX_CH; c++) v->gp[c] = v->gt[c];   /* all 16, 0 past nsh: gcc 13 makes nsh of them memmove */
        for (k = 0; k < SP_PINNA_TAPS; k++) v->hp[k] = v->ht[k];
        if (f->route == SP_FIELD_PANLAW) sp_panlaw_targets(f, v, x, y, z);
        else if (!v->cvalid || x != v->cx || y != v->cy || z != v->cz || f->yaw != v->cyaw) {
            sp_field_targets(f, v, x, y, z);
            v->cx = x;
            v->cy = y;
            v->cz = z;
            v->cyaw = f->yaw;
            v->cvalid = 1;
        }
        if (g->n == 0) {
            for (c = 0; c < SP_FIELD_MAX_CH; c++) v->gp[c] = v->gt[c];
            for (k = 0; k < SP_PINNA_TAPS; k++) v->hp[k] = v->ht[k];
        }
        v->ntap = 0;
        if (f->pinna)
            for (k = 0; k < SP_PINNA_TAPS; k++)
                if (v->hp[k] || v->ht[k]) v->tap[v->ntap++] = (uint8_t)k;
    }
    if (g->fx) for (i = 0; i < 2u * FX_RETURNS; i++) {
        sp_fvoice_t *v = &f->wet[i / 2u][i & 1u];
        for (c = 0; c < SP_FIELD_MAX_CH; c++) v->gp[c] = v->gt[c];
        for (k = 0; k < SP_PINNA_TAPS; k++) v->hp[k] = v->ht[k];
        if (!v->cvalid || f->yaw != v->cyaw) {
            sp_wet_targets(f, v, i & 1u);
            v->cyaw = f->yaw;
            v->cvalid = 1;
        }
        if (g->n == 0) {
            for (c = 0; c < SP_FIELD_MAX_CH; c++) v->gp[c] = v->gt[c];
            for (k = 0; k < SP_PINNA_TAPS; k++) v->hp[k] = v->ht[k];
        }
        v->ntap = 0;
        if (f->pinna) for (k = 0; k < SP_PINNA_TAPS; k++)
            if (v->hp[k] || v->ht[k]) v->tap[v->ntap++] = (uint8_t)k;
    }
}

/* One source's block through its pinna into f->pbuf, Q4.28 dry/Q7.25 wet: each live tap
 * ramped over the block as the gains are, the taps summed in int64 (|y| at
 * most 3.5 * 2^29 for |x| up to 2^31). */
static void sp_pinna_run(sp_field_t *f, sp_fvoice_t *v, const int32_t *x, uint32_t j0, uint32_t len)
{
    uint32_t q, t, n = f->geo.n, sh = f->geo.log2k, k = f->geo.k;
    for (q = 0; q < v->ntap; q++) {
        uint32_t m = v->tap[q];
        v->hs[m] = v->hp[m] * (int32_t)(k - j0 - 1u) + v->ht[m] * (int32_t)(j0 + 1u);   /* |hs| < 3.5 * 2^22 * (K + 2) < 2^31 */
    }
    for (t = 0; t < len; t++) {
        uint32_t w = n + t;
        int64_t  a = 0;
        v->hist[w & (SP_PINNA_HIST - 1u)] = x[t];
        for (q = 0; q < v->ntap; q++) {
            uint32_t m = v->tap[q];
            a += (int64_t)(v->hs[m] >> sh) * v->hist[(w - m) & (SP_PINNA_HIST - 1u)];
            v->hs[m] += v->ht[m] - v->hp[m];
        }
        f->pbuf[t] = (int32_t)((a + (1 << 23)) >> 24);
    }
}

/* One voice's block into the field's accumulators: per channel space.c's
 * ramp, g = (gp * K + (gt - gp) * (j + 1)) >> log2 K, as a running sum
 * at most 2^30 in magnitude where used (2^23 * (K + 2) < 2^31 after the
 * block's last step); a constant gain needs no ramp, a zero one no work.
 * scale is 1 for dry or 8 for Q27/Q25 wet. Scale only after the gain ramp;
 * the scaled gain fits int32 and the product/accumulation fit int64. */
static void sp_encode(sp_field_t *f, const sp_fvoice_t *v, const int32_t *x, uint32_t j0, uint32_t len, int32_t scale)
{
    uint32_t c, t, sh = f->geo.log2k, k = f->geo.k;
    for (c = 0; c < f->nsh; c++) {
        int32_t  gp = v->gp[c], gt = v->gt[c], d = gt - gp;
        int64_t *a = f->acc[c];
        if (d == 0) {
            if (gp == 0) continue;
            for (t = 0; t < len; t++) a[t] += (int64_t)x[t] * (gp * scale);
        } else {
            int32_t s = gp * (int32_t)(k - j0 - 1u) + gt * (int32_t)(j0 + 1u);
            for (t = 0; t < len; t++) {
                a[t] += (int64_t)x[t] * ((s >> sh) * scale);
                s += d;
            }
        }
    }
}

static int16_t sp_sat16(int64_t v)
{
    return (int16_t)(v > 32767 ? 32767 : v < -32768 ? -32768 : v);
}

/* The decoder on f->fld's len frames at the counter (SPEC section 16):
 * per channel the left ear's FIR over the history, summed into the even
 * and odd channels' sums E and O; left E + O, right E - O, each rounded
 * by the table's shift plus 15 and saturated. A channel whose FIR is all
 * zero is skipped (the same bits). The shift is the caller's, so the
 * rounding's arithmetic shift is a logical shift of the sum biased by
 * 2^62, which the targets' allowed helpers do : |E| + |O| <= 2^58. */
static void sp_decode_sums(sp_field_t *f, uint32_t len)
{
    uint32_t c, t, j, n = f->geo.n;
    for (t = 0; t < len; t++) f->eo[0][t] = f->eo[1][t] = 0;
    for (c = 0; c < f->nsh; c++) {
        const int16_t *h = f->table + (size_t)c * SP_FIELD_TAPS;
        int64_t *e = f->eo[(f->odd >> c) & 1u];
        int32_t *hb = f->hist[c];
        for (j = 0; j < SP_FIELD_TAPS && !h[j]; j++) {}
        if (j == SP_FIELD_TAPS) continue;
        for (t = 0; t < len; t++) {
            uint32_t w = (n + t) & (SP_FIELD_TAPS - 1u);
            const int32_t *x = hb + w + 1;                            /* x[i] is the field at n + t - 255 + i */
            int64_t a = 0;
            hb[w] = hb[w + SP_FIELD_TAPS] = f->fld[c][t];
            for (j = 0; j < SP_FIELD_TAPS; j++) a += (int64_t)(int32_t)h[SP_FIELD_TAPS - 1u - j] * x[j];
            e[t] += a;
        }
    }
}

static uint32_t sp_decode_block(sp_field_t *f, int16_t *out, uint32_t len)
{
    uint32_t t, s = f->shift + 15u, clipped = 0;
    sp_decode_sums(f, len);
    for (t = 0; t < len; t++) {
        int64_t l = (int64_t)(((uint64_t)(f->eo[0][t] + f->eo[1][t]) + f->dround + ((uint64_t)1 << 62)) >> s) - (int64_t)f->dbias;
        int64_t r = (int64_t)(((uint64_t)(f->eo[0][t] - f->eo[1][t]) + f->dround + ((uint64_t)1 << 62)) >> s) - (int64_t)f->dbias;
        clipped += (uint32_t)(l > 32767 || l < -32768) + (uint32_t)(r > 32767 || r < -32768);
        out[2 * t]      = sp_sat16(l);
        out[2 * t + 1u] = sp_sat16(r);
    }
    return clipped;
}

/* A validated attached decoder: |Q45 ear| < 2^52. The division is floor,
 * with unsigned bias to avoid a variable int64 arithmetic-shift helper.
 * There is no ear int16 rounding before the common post master. */
static int64_t sp_ear_q45(int64_t x, uint32_t shift)
{
    if (shift <= 15u) {
        uint64_t a = (uint64_t)(x < 0 ? -x : x);
        int64_t y = (int64_t)(a << (15u - shift));
        return x < 0 ? -y : y;
    } else {
        uint32_t s = shift - 15u;
        return (int64_t)(((uint64_t)x + ((uint64_t)1 << 62)) >> s)
            - (int64_t)((uint64_t)1 << (62u - s));
    }
}

static uint32_t sp_decode_post(sp_field_t *f, int16_t *out, uint32_t len)
{
    uint32_t t;
    sp_decode_sums(f, len);
    for (t = 0; t < len; t++) {
        int64_t e = f->eo[0][t], o = f->eo[1][t];
        f->eo[0][t] = sp_ear_q45(e + o, f->shift);
        f->eo[1][t] = sp_ear_q45(e - o, f->shift);
    }
    return fx_master_post(f->geo.fx, &f->eo[0][0], SP_MAX_BLOCK, out, len);
}

/* The field routes' render: voices from the engine, or src's samples. */
static uint32_t sp_field_core(sp_field_t *f, fm_voice_t *voices, const int32_t *src, uint32_t stride, uint32_t count,
                              int16_t *out, uint32_t frames)
{
    sp_stereo_t *g = &f->geo;
    fx_t *fx = g->fx;
    uint32_t clipped = 0;
    if (count > SP_MAX_VOICES) count = SP_MAX_VOICES;
    while (frames) {
        uint32_t j0 = g->n & (g->k - 1u), len = g->k - j0, i, t, c;
        if (len > frames) len = frames;
        if (j0 == 0) sp_field_boundary(f, count);
        for (c = 0; c < f->nsh; c++)
            for (t = 0; t < len; t++) f->acc[c][t] = 0;
        for (i = 0; i < count; i++) {
            sp_fvoice_t *v = &f->fv[i];
            uint32_t quiet = v->quiet;
            if (src) {
                for (t = 0; t < len; t++) f->buf[t] = src[(size_t)t * stride + i];
                v->quiet = 0;
            } else if (fm_voice_active(&voices[i]) || fm_voice_needs_clock(&voices[i])) {
                const int active = fm_voice_active(&voices[i]);
                fm_render_q30(&voices[i], f->buf, len);
                v->quiet = active ? 0 : v->quiet + len < SP_PINNA_HIST ? v->quiet + len : SP_PINNA_HIST;
            } else if ((fx && (fx->on & FX_ON_CRUSH)) || (f->pinna && v->quiet < SP_PINNA_HIST)) {
                /* Pinna-only drain is raw zero input, never resent after
                 * pinna. Crusher also needs zeros while a held sample lives. */
                for (t = 0; t < len; t++) f->buf[t] = 0;
                v->quiet = v->quiet + len < SP_PINNA_HIST ? v->quiet + len : SP_PINNA_HIST;
            } else continue;                                             /* its output is 0 and it does not advance */
            if (fx) {
                fx_voice_process(fx, i, f->buf, len);
                if (fx->on & FX_ON_CRUSH) {
                    /* Track the processed source sample by sample: a hold
                     * can end inside this piece. Chunk size must not alter
                     * the history-drain state even while the engine is idle. */
                    for (t = 0; t < len; t++) quiet = f->buf[t] ? 0 : quiet < SP_PINNA_HIST ? quiet + 1u : SP_PINNA_HIST;
                    v->quiet = quiet;
                }
            }
            if (f->pinna) {
                sp_pinna_run(f, v, f->buf, j0, len);
                sp_encode(f, v, f->pbuf, j0, len, 1);
            } else sp_encode(f, v, f->buf, j0, len, 1);
        }
        if (fx) {
            fx_field_returns(fx, &f->wetbuf[0][0][0], 2u * SP_MAX_BLOCK, SP_MAX_BLOCK, len);
            for (i = 0; i < 2u * FX_RETURNS; i++) {
                sp_fvoice_t *v = &f->wet[i / 2u][i & 1u];
                const int32_t *x = f->wetbuf[i / 2u][i & 1u];
                /* Q27->Q25 through the same pinna kernel. Scale the
                 * encoder gain by 8 after its ramp to align dry Q53/Q51;
                 * the bounded product stays int32, with int64 accumulation.
                 * Every wet history advances, including zero bus returns. */
                if (f->pinna) { sp_pinna_run(f, v, x, j0, len); x = f->pbuf; }
                sp_encode(f, v, x, j0, len, 8);
            }
        }
        if (f->route == SP_FIELD_BINAURAL) {
            for (c = 0; c < f->nsh; c++)
                for (t = 0; t < len; t++) {                              /* Q.53 (Q.51 with the pinna) to Q2.30 */
                    int64_t s = f->pinna ? (f->acc[c][t] + (1 << 20)) >> 21 : (f->acc[c][t] + (1 << 22)) >> 23;
                    if (s > INT32_MAX) { s = INT32_MAX; clipped++; }
                    else if (s < INT32_MIN) { s = INT32_MIN; clipped++; }
                    f->fld[c][t] = (int32_t)s;
                }
            clipped += fx ? sp_decode_post(f, out, len) : sp_decode_block(f, out, len);
        } else if (fx) {
            for (c = 0; c < f->nsh; c++) for (t = 0; t < len; t++)
                f->acc[c][t] = f->pinna ? f->acc[c][t] >> 6 : f->acc[c][t] >> 8;
            clipped += fx_master_post(fx, &f->acc[0][0], SP_MAX_BLOCK, out, len);
        } else {
            for (t = 0; t < len; t++)
                for (c = 0; c < f->nsh; c++) {                           /* Q.53 (Q.51) to Q15, one rounding */
                    int64_t s = f->pinna ? (f->acc[c][t] + ((int64_t)1 << 35)) >> 36 : (f->acc[c][t] + ((int64_t)1 << 37)) >> 38;
                    if (s > 32767) { s = 32767; clipped++; }
                    else if (s < -32768) { s = -32768; clipped++; }
                    out[t * f->nsh + c] = (int16_t)s;
                }
        }
        out += (size_t)f->ch * len;
        if (src) src += (size_t)len * stride;
        g->n += len;
        frames -= len;
    }
    return clipped;
}

/* The pan law (c): sp_stereo_render's loop on the field state's gains. */
static uint32_t sp_panlaw_core(sp_field_t *f, fm_voice_t *voices, const int32_t *src, uint32_t stride, uint32_t count,
                               int16_t *out, uint32_t frames)
{
    sp_stereo_t *sp = &f->geo;
    int32_t *buf = sp->buf;
    int64_t (*acc)[SP_MAX_BLOCK] = sp->acc;
    uint32_t clipped = 0;
    if (count > SP_MAX_VOICES) count = SP_MAX_VOICES;
    while (frames) {
        uint32_t j0 = sp->n & (sp->k - 1u), len = sp->k - j0, i, t;
        if (len > frames) len = frames;
        if (j0 == 0) sp_field_boundary(f, count);
        for (t = 0; t < len; t++) { acc[0][t] = 0; acc[1][t] = 0; }
        for (i = 0; i < count; i++) {
            const sp_fvoice_t *v = &f->fv[i];
            int32_t dl = v->gt[0] - v->gp[0], dr = v->gt[1] - v->gp[1];
            int32_t bl = (int32_t)((uint32_t)v->gp[0] << sp->log2k), br = (int32_t)((uint32_t)v->gp[1] << sp->log2k);
            int32_t rl = dl * (int32_t)(j0 + 1u), rr = dr * (int32_t)(j0 + 1u);
            if (src) for (t = 0; t < len; t++) buf[t] = src[(size_t)t * stride + i];
            else if (!fm_voice_active(&voices[i]) && !fm_voice_needs_clock(&voices[i])) continue;
            else fm_render_q30(&voices[i], buf, len);
            for (t = 0; t < len; t++) {
                int32_t gl = (bl + rl) >> sp->log2k, gr = (br + rr) >> sp->log2k;
                acc[0][t] += (int64_t)buf[t] * gl;
                acc[1][t] += (int64_t)buf[t] * gr;
                rl += dl;
                rr += dr;
            }
        }
        for (t = 0; t < len; t++) {
            int c;
            for (c = 0; c < 2; c++) {
                int64_t s = (acc[c][t] + ((int64_t)1 << 29)) >> 30;
                if (s > 32767) { s = 32767; clipped++; }
                else if (s < -32768) { s = -32768; clipped++; }
                out[2 * t + (uint32_t)c] = (int16_t)s;
            }
        }
        out += 2 * len;
        if (src) src += (size_t)len * stride;
        sp->n += len;
        frames -= len;
    }
    return clipped;
}

uint32_t sp_field_render(sp_field_t *f, fm_voice_t *voices, uint32_t count, int16_t *out, uint32_t frames)
{
    if (f->route == SP_FIELD_PANLAW) return sp_panlaw_core(f, voices, NULL, 0, count, out, frames);
    return sp_field_core(f, voices, NULL, 0, count, out, frames);
}

uint32_t sp_field_render_q30(sp_field_t *f, const int32_t *src, uint32_t stride, uint32_t count, int16_t *out, uint32_t frames)
{
    if (f->route == SP_FIELD_PANLAW) return sp_panlaw_core(f, NULL, src, stride, count, out, frames);
    return sp_field_core(f, NULL, src, stride, count, out, frames);
}

uint32_t sp_field_decode(sp_field_t *f, const int32_t *fld, uint32_t frames, int16_t *out)
{
    uint32_t clipped = 0;
    while (frames) {
        uint32_t len = frames < SP_MAX_BLOCK ? frames : SP_MAX_BLOCK, c, t;
        for (c = 0; c < f->nsh; c++)
            for (t = 0; t < len; t++) f->fld[c][t] = fld[(size_t)t * f->nsh + c];
        clipped += sp_decode_block(f, out, len);
        fld += (size_t)len * f->nsh;
        out += 2 * len;
        f->geo.n += len;
        frames -= len;
    }
    return clipped;
}
