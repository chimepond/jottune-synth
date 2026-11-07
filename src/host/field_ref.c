/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * field_ref.c — host side only (doubles allowed, ): the double model of
 * the field routes (space/SPEC.md sections 11 to 16), the harness's
 * --field-test and --field-cost.
 *
 * The model takes the module's integer definitions as given: the positions
 * at each block boundary (sp_coord_at, ), r = floor(sqrt(x^2 + y^2 +
 * z^2)) and its ceiling, and the near radius. Everything after them is
 * double: the direction, the yaw as a rotation of the vector (not of the
 * harmonics), the spherical harmonics by binaural_ab.py's recurrence (not
 * the library's polynomials), Brown and Duda's pinna as binaural_ab.py's
 * pinna_fir, the gains and taps ramped exactly across each block, and the
 * decoder's convolution. --field-test holds the integer routes to the
 * model by  (loss at most 0.10 dB, peak error at most 1 LSB, no clip),
 * the decoder to a plain int64 convolution bit for bit on a table and
 * fields drawn from an LCG (extremes included, so lane G's table is not
 * needed), the engine's path to the Q2.30 path bit for bit (the pinna's
 * tail), and the pan law at W = 32768 to sp_stereo_render bit for bit.
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "fm_a1.h"
#include "../space/field.h"
#include "field_ref.h"
#include "fx_ref.h"

int field_test(void);
int field_cost(void);

#define FT_FS   48000u
#define FT_N    9600u          /* 0.2 s */
#define FT_NV   4u
#define FT_NEAR 100u
#define FT_SHIFT 14            /* the LCG tables' fraction bits, as lane G's SP_BIN_SHIFT */
#define FT_PI   3.14159265358979323846
#define FT_ZODD ((1u << 2) | (1u << 5) | (1u << 7) | (1u << 10) | (1u << 12) | (1u << 14))   /* odd in Z: zero in G's table */
#define FT_ODD  ((1u << 1) | (1u << 4) | (1u << 5) | (1u << 9) | (1u << 10) | (1u << 11))   /* odd under left-right mirroring */

static const double   ft_rho[5] = { 0.5, -1.0, 0.5, -0.25, 0.25 };   /* Brown and Duda, Table I */
static const double   ft_pa[5]  = { 1, 5, 5, 5, 5 };
static const double   ft_pb[5]  = { 2, 4, 7, 11, 13 };
static const uint32_t ft_dpb[5] = { 1000, 500, 500, 500, 500 };
static const uint32_t ft_drd[5] = { 850, 350, 350, 350, 350 };

enum { FT_POS, FT_MOVE, FT_YAW };

typedef struct {
    uint32_t t;
    int      kind;
    uint32_t v;
    int32_t  x, y, z;      /* FT_YAW: x is the yaw in millidegrees */
    uint32_t end;          /* FT_MOVE */
} ft_ev_t;

/* Positions on and off block boundaries, one inside near, one above, one
 * behind, two moves, three yaws; in time order. */
static const ft_ev_t ft_ev[] = {
    {    0, FT_POS,  0,   -700,   710,  300,    0 },
    {    0, FT_POS,  1,   2000,  -500, -400,    0 },
    {    0, FT_POS,  2,      0,    50,    0,    0 },
    {    0, FT_POS,  3,      0,     0, 1000,    0 },
    { 1000, FT_YAW,  0,  45000,     0,    0,    0 },
    { 2000, FT_POS,  0,      0, -1500,  800,    0 },
    { 3000, FT_MOVE, 2,  -3000,     0,    0, 9000 },
    { 4321, FT_YAW,  0, -120500,    0,    0,    0 },
    { 5000, FT_MOVE, 0,   1200,  1200, -600, 8000 },
    { 6007, FT_POS,  3,    300, -1000,  -20,    0 },
    { 7777, FT_YAW,  0, 359999,     0,    0,    0 },
};
#define FT_NEV (sizeof ft_ev / sizeof ft_ev[0])

/* Note events for the engine's path: on (millihertz) or off (0). */
typedef struct { uint32_t t, v, mhz; } ft_note_t;
static const ft_note_t ft_notes[] = {
    {    0, 0, 440000 }, {    0, 1, 659255 }, { 1500, 0, 0 }, { 2000, 1, 0 }, { 2100, 2, 329628 },
    { 4000, 0, 523251 }, { 7000, 2, 0 }, { 8500, 0, 0 },
};
#define FT_NNOTE (sizeof ft_notes / sizeof ft_notes[0])

typedef struct {
    const char *name;
    int         route;
    uint32_t    order, block;
    int         pinna;
    uint32_t    d[5];
    int32_t     yaw0;
    int         yaw_ev;    /* the yaw events apply */
    uint32_t    w;         /* pan law */
} ft_case_t;

static const ft_case_t ft_cases[] = {
    { "amb1",              SP_FIELD_AMB1,     1, 64,  SP_PINNA_OFF,  { 0 },                     0,      0, 0 },
    { "amb3",              SP_FIELD_AMB3,     3, 64,  SP_PINNA_OFF,  { 0 },                     0,      0, 0 },
    { "amb1-yaw",          SP_FIELD_AMB1,     1, 128, SP_PINNA_OFF,  { 0 },                     -90000, 1, 0 },
    { "amb3-yaw",          SP_FIELD_AMB3,     3, 32,  SP_PINNA_OFF,  { 0 },                     30000,  1, 0 },
    { "amb3-pinna",        SP_FIELD_AMB3,     3, 64,  SP_PINNA_PBNH, { 0 },                     0,      0, 0 },
    { "amb3-yaw-pinna",    SP_FIELD_AMB3,     3, 64,  SP_PINNA_PBNH, { 0 },                     0,      1, 0 },
    { "amb1-yaw-pinna-rd", SP_FIELD_AMB1,     1, 128, SP_PINNA_RD,   { 0 },                     -90000, 1, 0 },
    { "amb3-yaw-pinna-d",  SP_FIELD_AMB3,     3, 32,  SP_PINNA_D,    { 1000, 0, 250, 750, 1000 }, 180000, 1, 0 },
    { "binaural1",         SP_FIELD_BINAURAL, 1, 64,  SP_PINNA_OFF,  { 0 },                     0,      1, 0 },
    { "binaural3",         SP_FIELD_BINAURAL, 3, 64,  SP_PINNA_OFF,  { 0 },                     12345,  1, 0 },
    { "binaural3-pinna",   SP_FIELD_BINAURAL, 3, 128, SP_PINNA_PBNH, { 0 },                     0,      1, 0 },
    { "panlaw-19661",      SP_FIELD_PANLAW,   0, 64,  SP_PINNA_OFF,  { 0 },                     0,      0, 19661 },
    { "panlaw-32768",      SP_FIELD_PANLAW,   0, 32,  SP_PINNA_OFF,  { 0 },                     0,      0, 32768 },
};
#define FT_NCASE (sizeof ft_cases / sizeof ft_cases[0])

static uint32_t ft_lcg(uint32_t *s)
{
    *s = *s * 1664525u + 1013904223u;
    return *s;
}

static int16_t ft_clip16(double x)
{
    if (x >= 32767.0) return 32767;
    if (x > -32768.0) return (int16_t)x;
    return -32768;
}

static double ft_db(double num, double den)
{
    if (den <= 0.0) return 999.0;
    return 10.0 * log10(num / den);
}

static uint32_t ft_nsh(const ft_case_t *c)
{
    if (c->route == SP_FIELD_PANLAW) return 2;
    return c->route == SP_FIELD_AMB1 || (c->route == SP_FIELD_BINAURAL && c->order == 1) ? 4 : 16;
}

static uint32_t ft_ch(const ft_case_t *c)
{
    return c->route == SP_FIELD_BINAURAL || c->route == SP_FIELD_PANLAW ? 2 : ft_nsh(c);
}

static double ft_fact(int n)
{
    double f = 1;
    while (n > 1) f *= n--;
    return f;
}

/* binaural_ab.py's sh: real harmonics to order (ACN, SN3D, no
 * Condon-Shortley phase) at u in AmbiX axes, order l scaled by |u|^l. */
static void ft_sh(int order, const double *u, double *out)
{
    double rho = sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2]), v[3], phi, z, cz, P[4];
    int m, l, k;
    for (k = 0; k < 3; k++) v[k] = rho > 0 ? u[k] / rho : u[k];
    phi = atan2(v[1], v[0]);
    z = v[2];
    cz = sqrt(fmax(0.0, 1 - z * z));
    for (m = 0; m <= order; m++) {
        double pmm = 1;
        for (k = 1; k < 2 * m; k += 2) pmm *= k;
        P[0] = pmm * pow(cz, m);
        for (l = m + 1; l <= order; l++)
            P[l - m] = ((2 * l - 1) * z * P[l - m - 1] - (l + m - 1) * (l > m + 1 ? P[l - m - 2] : 0)) / (l - m);
        for (l = m; l <= order; l++) {
            double y = sqrt((2 - (m == 0)) * ft_fact(l - m) / ft_fact(l + m)) * P[l - m] * pow(rho, l);
            if (m == 0) out[l * l + l] = y;
            else {
                out[l * l + l + m] = y * cos(m * phi);
                out[l * l + l - m] = y * sin(m * phi);
            }
        }
    }
}

/* binaural_ab.py's pinna_fir at head axes hv (x right, y ahead, z up). */
static void ft_pinna_at(const double *hv, const uint32_t *d, double *h, uint32_t fs)
{
    double n = sqrt(hv[0] * hv[0] + hv[1] * hv[1] + hv[2] * hv[2]), v[3], th, ph;
    int k;
    if (n < 1e-9) n = 1e-9;
    for (k = 0; k < 3; k++) v[k] = hv[k] / n;
    th = asin(fmax(-1.0, fmin(1.0, v[0])));
    ph = atan2(v[2], v[1]);
    for (k = 0; k < SP_PINNA_TAPS; k++) h[k] = 0;
    h[0] = 1;
    if (fabs(ph) > FT_PI / 2) return;
    for (k = 0; k < 5; k++) {
        double tau = (ft_pa[k] * cos(th / 2) * sin(d[k] / 1000.0 * (FT_PI / 2 - ph)) + ft_pb[k]) * fs / 44100.0;
        int i = (int)floor(tau);
        h[i] += ft_rho[k] * (1 - (tau - i));
        h[i + 1] += ft_rho[k] * (tau - i);
    }
}

/* The targets of one voice at position (x, y, z) mm and the yaw (radians). */
static void ft_targets(const ft_case_t *c, int32_t x, int32_t y, int32_t z, double yaw, double *g, double *h)
{
    uint64_t sq = (uint64_t)((int64_t)x * x) + (uint64_t)((int64_t)y * y) + (uint64_t)((int64_t)z * z);
    uint32_t r = sp_isqrt64(sq), rc = r + ((uint64_t)r * r < sq), m = rc > FT_NEAR ? rc : FT_NEAR, k;
    double gd = 1000.0 / (r > 1000 ? r : 1000), u[3], w[3], sh[16], hv[3];
    const uint32_t *d = c->pinna == SP_PINNA_RD ? ft_drd : c->pinna == SP_PINNA_D ? c->d : ft_dpb;
    if (c->route == SP_FIELD_PANLAW) {
        uint32_t mp = r > FT_NEAR ? r : FT_NEAR;
        double ux = (double)x / mp * (c->w / 32768.0);
        g[0] = sqrt((1 - ux) / 2) * gd;
        g[1] = sqrt((1 + ux) / 2) * gd;
        return;
    }
    u[0] = (double)y / m;
    u[1] = -(double)x / m;
    u[2] = (double)z / m;
    w[0] = u[0] * cos(yaw) - u[1] * sin(yaw);
    w[1] = u[0] * sin(yaw) + u[1] * cos(yaw);
    w[2] = u[2];
    for (k = 0; k < 2; k++)                    /* the module turns by exact quadrants at multiples of 90 degrees, and */
        if (fabs(w[k]) < 1e-12) w[k] = 0;      /* the pinna steps at hy = 0: no rounding noise across that line */
    ft_sh(ft_nsh(c) == 4 ? 1 : 3, w, sh);
    for (k = 0; k < ft_nsh(c); k++) g[k] = fmax(-1.0, fmin(1.0, sh[k] * gd));
    hv[0] = -w[1];
    hv[1] = w[0];
    hv[2] = w[2];
    if (c->pinna) ft_pinna_at(hv, d, h, FT_FS);
}

static void ft_apply(const ft_case_t *c, sp_stereo_t *g, sp_field_t *f, int32_t *yaw, const ft_ev_t *e)
{
    if (e->kind == FT_POS) sp_stereo_set_pos(g, e->v, e->x, e->y, e->z);
    else if (e->kind == FT_MOVE) sp_stereo_move(g, e->v, e->x, e->y, e->z, e->end);
    else if (c->yaw_ev) {
        *yaw = e->x;
        if (f) sp_field_set_yaw(f, e->x);
    }
}

/* The model on sources src (FT_NV per frame, Q2.30): ref[t * ch + k] in
 * LSB of the int16 output. */
static void ft_model(const ft_case_t *c, const int32_t *src, const int16_t *table, double *ref)
{
    static double gt[FT_NV][16], gp[FT_NV][16], ht[FT_NV][SP_PINNA_TAPS], hp[FT_NV][SP_PINNA_TAPS];
    sp_stereo_t *geo = (sp_stereo_t *)calloc(1, sizeof *geo);
    uint32_t nsh = ft_nsh(c), ch = ft_ch(c), K = c->block, t, i, k, j, e = 0;
    int32_t yaw = c->yaw0;
    int bin = c->route == SP_FIELD_BINAURAL;
    double *fld = bin ? (double *)calloc((size_t)nsh * FT_N, sizeof *fld) : NULL;
    if (!geo || (bin && !fld)) { fprintf(stderr, "oom\n"); exit(1); }
    sp_stereo_init(geo, K, FT_NEAR);
    for (t = 0; t < FT_N; t++) {
        double w = (double)(t % K + 1) / K, acc[16] = { 0 };
        geo->n = t;
        while (e < FT_NEV && ft_ev[e].t == t) ft_apply(c, geo, NULL, &yaw, &ft_ev[e++]);
        if (t % K == 0)
            for (i = 0; i < FT_NV; i++) {
                const sp_voice_t *v = &geo->v[i];
                uint32_t b = t / K;
                memcpy(gp[i], gt[i], sizeof gp[i]);
                memcpy(hp[i], ht[i], sizeof hp[i]);
                ft_targets(c, sp_coord_at(v, b, v->x0, v->x), sp_coord_at(v, b, v->y0, v->y), sp_coord_at(v, b, v->z0, v->z),
                           yaw * (FT_PI / 180000.0), gt[i], ht[i]);
                if (t == 0) {
                    memcpy(gp[i], gt[i], sizeof gp[i]);
                    memcpy(hp[i], ht[i], sizeof hp[i]);
                }
            }
        for (i = 0; i < FT_NV; i++) {
            double y = 0;
            if (c->pinna) {
                for (k = 0; k < SP_PINNA_TAPS && k <= t; k++)
                    y += (hp[i][k] + (ht[i][k] - hp[i][k]) * w) * src[(size_t)(t - k) * FT_NV + i];
            } else y = src[(size_t)t * FT_NV + i];
            y /= 1073741824.0;
            for (k = 0; k < nsh; k++) acc[k] += y * (gp[i][k] + (gt[i][k] - gp[i][k]) * w);
        }
        if (bin) for (k = 0; k < nsh; k++) fld[(size_t)k * FT_N + t] = acc[k];
        else for (k = 0; k < ch; k++) ref[(size_t)t * ch + k] = acc[k] * 32768.0;
    }
    if (bin)
        for (t = 0; t < FT_N; t++) {
            double eo[2] = { 0, 0 };
            for (k = 0; k < nsh; k++) {
                double a = 0;
                for (j = 0; j < SP_FIELD_TAPS && j <= t; j++) a += table[k * SP_FIELD_TAPS + j] * fld[(size_t)k * FT_N + t - j];
                eo[(FT_ODD >> k) & 1u] += a;
            }
            ref[2 * t]      = (eo[0] + eo[1]) * 32768.0 / (1u << FT_SHIFT);
            ref[2 * t + 1u] = (eo[0] - eo[1]) * 32768.0 / (1u << FT_SHIFT);
        }
    free(geo);
    free(fld);
}

static int ft_init(const ft_case_t *c, sp_field_t *f, const int16_t *table)
{
    sp_field_cfg_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.route = c->route;
    cfg.order = c->order;
    cfg.block = c->block;
    cfg.near_mm = FT_NEAR;
    cfg.fs = FT_FS;
    cfg.yaw_mdeg = c->yaw0;
    cfg.pinna = c->pinna;
    memcpy(cfg.pinna_d, c->d, sizeof cfg.pinna_d);
    cfg.panlaw_q15 = c->w;
    cfg.table = table;
    cfg.table_shift = FT_SHIFT;
    return sp_field_init(f, &cfg);
}

/* The integer route on src, in chunks of 1 to 200 frames drawn from an LCG
 * and cut at the events. Returns the clipped count. */
static uint32_t ft_run(const ft_case_t *c, const int32_t *src, const int16_t *table, int16_t *out, uint32_t seed)
{
    sp_field_t *f = (sp_field_t *)calloc(1, sizeof *f);
    uint32_t pos = 0, e = 0, clipped = 0, ch = ft_ch(c);
    int32_t yaw = 0;
    if (!f || ft_init(c, f, table)) { fprintf(stderr, "field init failed: %s\n", c->name); exit(1); }
    while (pos < FT_N) {
        uint32_t next, len;
        while (e < FT_NEV && ft_ev[e].t == pos) ft_apply(c, &f->geo, f, &yaw, &ft_ev[e++]);
        next = e < FT_NEV ? ft_ev[e].t : FT_N;
        len = 1 + ft_lcg(&seed) % 200u;
        if (len > next - pos) len = next - pos;
        clipped += sp_field_render_q30(f, src + (size_t)pos * FT_NV, FT_NV, FT_NV, out + (size_t)pos * ch, len);
        pos += len;
    }
    free(f);
    return clipped;
}

/* Two sines, LCG noise inside near, a low sine; amplitudes in full scale
 * times scale. */
static void ft_sources(int32_t *src, double scale)
{
    static const double amp[FT_NV] = { 0.25, 0.2, 0.1, 0.3 };
    uint32_t t, s = 7;
    for (t = 0; t < FT_N; t++) {
        double ph = 2 * FT_PI * t / FT_FS, v[FT_NV];
        uint32_t i;
        v[0] = sin(440.0 * ph);
        v[1] = sin(1234.5 * ph + 1.0);
        v[2] = ((double)(ft_lcg(&s) >> 8) / 8388608.0) - 1.0;
        v[3] = sin(97.0 * ph);
        for (i = 0; i < FT_NV; i++) src[(size_t)t * FT_NV + i] = (int32_t)lrint(v[i] * amp[i] * scale * 1073741824.0);
    }
}

/*  over the interleaved output: loss against the ceiling, peak error.
 * bound 0: 's; else the pan law's (its gains are space.c's Q1.15, ,
 * so the loss is printed and the peak error held to bound). */
static int ft_d14(const char *name, const int16_t *out, const double *ref, size_t n, uint32_t clipped, double scale, double bound)
{
    double sig = 0, err = 0, qerr = 0, peak = 0, snr, ceiling, loss, rpeak = 0;
    size_t i;
    int bad;
    for (i = 0; i < n; i++) {
        double r = ref[i], d = out[i] - r, q = ft_clip16(nearbyint(r)) - r;
        double ad = fabs(out[i] - (r > 32767.0 ? 32767.0 : r < -32768.0 ? -32768.0 : r));
        sig += r * r;
        err += d * d;
        qerr += q * q;
        if (ad > peak) peak = ad;
        if (fabs(r) > rpeak) rpeak = fabs(r);
    }
    snr = ft_db(sig, err);
    ceiling = ft_db(sig, qerr);
    loss = snr > 900 ? 0.0 : ceiling - snr;
    bad = bound > 0 ? !(peak <= bound) || clipped : !(loss <= 0.10) || !(peak <= 1.0) || clipped;
    printf("FIELD %-18s loss_db=%.4f peak_err_lsb=%.4f snr_db=%.2f ceiling_db=%.2f ref_peak=%.0f scale=%.4f clipped=%u",
           name, loss, peak, snr, ceiling, rpeak, scale, clipped);
    if (bound > 0) printf(" peak_bound=%.2f (Q1.15 gains)", bound);
    printf(" %s\n", bad ? "FAIL" : "ok");
    return bad;
}

/* The LCG tables: for , taps up to 1000 decaying over the 256; for the
 * exact test, the full int16 range. The channels odd in Z are zero, as in
 * G's table, so the decoder's skip runs. */
static void ft_table(int16_t *h, int full, uint32_t seed)
{
    uint32_t c, j;
    for (c = 0; c < SP_FIELD_MAX_CH; c++)
        for (j = 0; j < SP_FIELD_TAPS; j++) {
            uint32_t r = ft_lcg(&seed);
            int32_t v = full ? (int32_t)(int16_t)(r >> 16) : ((int32_t)((r >> 16) % 2001u) - 1000) * (int32_t)(SP_FIELD_TAPS - j) / SP_FIELD_TAPS;
            if (full && j % 61 == 7) v = (r & 1u) ? 32767 : -32768;
            h[c * SP_FIELD_TAPS + j] = (FT_ZODD >> c) & 1u ? 0 : (int16_t)v;
        }
}

static int ft_cases_d14(void)
{
    int32_t *src = (int32_t *)malloc(sizeof *src * FT_N * FT_NV);
    double *ref = (double *)malloc(sizeof *ref * FT_N * 16);
    int16_t *out = (int16_t *)malloc(sizeof *out * FT_N * 16), table[SP_FIELD_MAX_CH * SP_FIELD_TAPS];
    size_t ci;
    int bad = 0;
    if (!src || !ref || !out) { fprintf(stderr, "oom\n"); exit(1); }
    ft_table(table, 0, 99);
    for (ci = 0; ci < FT_NCASE; ci++) {
        const ft_case_t *c = &ft_cases[ci];
        size_t n = (size_t)FT_N * ft_ch(c), i;
        double scale = 1.0, peak = 0;
        int pass;
        for (pass = 0; pass < 3; pass++) {                /* scale the sources so that the model peaks near -4 dBFS */
            ft_sources(src, scale);
            ft_model(c, src, table, ref);
            for (peak = 0, i = 0; i < n; i++) if (fabs(ref[i]) > peak) peak = fabs(ref[i]);
            if (peak <= 22000 && peak >= 16000) break;
            scale *= 20000 / peak;
            if (scale > 6.0) scale = 6.0;                 /* the sources stay within int32 */
        }
        /* the pan law: each Q1.15 gain within 3 units of 2^-15 of the exact (two floors, a rounding and the ramp's
         * floor), over voices whose amplitudes sum to 0.85 times scale, plus the output's rounding */
        bad += ft_d14(c->name, out, ref, n, ft_run(c, src, table, out, 1u + (uint32_t)ci), scale,
                      c->route == SP_FIELD_PANLAW ? 0.5 + 3.0 * 0.85 * scale : 0.0);
    }
    free(src); free(ref); free(out);
    return bad;
}

/* The decoder against a plain int64 convolution, bit for bit, at the table
 * shift: fields of LCG words with every 13th sample at an extreme, decoded
 * in chunks of 1 to 300 frames, once at full scale (the ears mostly
 * saturate) and once scaled down by 2^(28 - shift) (mostly not). Then the
 * symmetry: a field of the even channels alone gives L = R, one of the odd
 * channels alone L = -R (or L + R = 1 at a rounding tie). */
static int ft_decoder(uint32_t order, uint32_t shift)
{
    enum { NF = 1500 };
    static int16_t table[SP_FIELD_MAX_CH * SP_FIELD_TAPS], out[2 * NF];
    static int32_t fld[NF * SP_FIELD_MAX_CH];
    sp_field_t *f = (sp_field_t *)calloc(1, sizeof *f);
    sp_field_cfg_t cfg;
    uint32_t nsh = order == 1 ? 4 : 16, seed = 5 + shift, t, c, j, pos, clipped = 0, clipped0 = 0, want_clipped = 0, want0 = 0, mism = 0, sym = 0, mode;
    int bad = 0;
    if (!f) { fprintf(stderr, "oom\n"); exit(1); }
    ft_table(table, 1, 1234 + order);
    for (c = 0; c < SP_FIELD_TAPS; c++) table[3 * SP_FIELD_TAPS + c] = order == 3 ? 0 : table[3 * SP_FIELD_TAPS + c];   /* a zero channel beyond G's */
    memset(&cfg, 0, sizeof cfg);
    cfg.route = SP_FIELD_BINAURAL;
    cfg.order = order;
    cfg.block = 64;
    cfg.near_mm = FT_NEAR;
    cfg.fs = FT_FS;
    cfg.table = table;
    cfg.table_shift = shift;
    for (mode = 0; mode < 4; mode++) {                    /* 0 full, 1 even channels only, 2 odd channels only, 3 all (2, 3 small) */
        if (sp_field_init(f, &cfg)) { fprintf(stderr, "decoder init failed\n"); exit(1); }
        for (t = 0; t < NF; t++)
            for (c = 0; c < nsh; c++) {
                uint32_t r = ft_lcg(&seed);
                int32_t v = (int32_t)r;
                if (t % 13 == c % 5) v = (r & 1u) ? INT32_MAX : INT32_MIN;
                if (mode >= 2) v >>= shift < 28 ? 28 - shift : 0;   /* |O| below 2^(shift + 29): no saturation */
                if ((mode == 1 && ((FT_ODD >> c) & 1u)) || (mode == 2 && !((FT_ODD >> c) & 1u))) v = 0;
                fld[t * nsh + c] = v;
            }
        clipped = 0;
        for (pos = 0; pos < NF; ) {
            uint32_t len = 1 + ft_lcg(&seed) % 300u;
            if (len > NF - pos) len = NF - pos;
            clipped += sp_field_decode(f, fld + (size_t)pos * nsh, len, out + 2 * pos);
            pos += len;
        }
        if (mode == 0 || mode == 3) {
            want_clipped = 0;
            for (t = 0; t < NF; t++) {
                int64_t e = 0, o = 0, ear[2];
                int s;
                for (c = 0; c < nsh; c++) {
                    int64_t a = 0;
                    for (j = 0; j < SP_FIELD_TAPS && j <= t; j++) a += (int64_t)table[c * SP_FIELD_TAPS + j] * fld[(t - j) * nsh + c];
                    if ((FT_ODD >> c) & 1u) o += a; else e += a;
                }
                ear[0] = e + o;
                ear[1] = e - o;
                for (s = 0; s < 2; s++) {
                    int64_t q = (ear[s] + ((int64_t)1 << (shift + 14))) >> (shift + 15);
                    if (q > 32767 || q < -32768) want_clipped++;
                    q = q > 32767 ? 32767 : q < -32768 ? -32768 : q;
                    if (out[2 * t + (uint32_t)s] != q) mism++;
                }
            }
            if (mode == 0) { clipped0 = clipped; want0 = want_clipped; }
            if (mism || clipped != want_clipped) bad = 1;
        } else {
            for (t = 0; t < NF; t++) {
                int32_t l = out[2 * t], r = out[2 * t + 1];
                if (mode == 1 ? l != r : (l + r != 0 && l + r != 1)) sym++;
            }
            if (sym) bad = 1;
        }
    }
    printf("FIELD decoder-o%u-shift%-2u  mismatches=%u clipped=%u,%u expected_clipped=%u,%u symmetry_errors=%u frames=%u %s\n",
           order, shift, mism, clipped0, clipped, want0, want_clipped, sym, 2 * NF, bad ? "FAIL" : "ok");
    free(f);
    return bad;
}

static void ft_patch(fm_patch_t *p)
{
    memset(p, 0, sizeof *p);
    p->op[0].ratio_q16 = 65536;  p->op[0].vel_sens_q8 = 256;
    p->op[0].attack_ms = 2;      p->op[0].decay_ms = 300;  p->op[0].sustain_db10 = 200; p->op[0].release_ms = 20;
    p->op[1].ratio_q16 = 131072; p->op[1].index_q10 = 2048;
    p->op[1].attack_ms = 1;      p->op[1].decay_ms = 200;  p->op[1].sustain_db10 = 100; p->op[1].release_ms = 20;
}

static void ft_notes_at(fm_voice_t *vs, uint32_t t, uint32_t *ne)
{
    while (*ne < FT_NNOTE && ft_notes[*ne].t == t) {
        const ft_note_t *n = &ft_notes[(*ne)++];
        if (n->mhz) fm_voice_note_on(&vs[n->v], n->mhz, 6000);
        else fm_voice_note_off(&vs[n->v]);
    }
}

static uint32_t ft_next(uint32_t pos, uint32_t e, uint32_t ne)
{
    uint32_t next = FT_N;
    if (e < FT_NEV && ft_ev[e].t < next) next = ft_ev[e].t;
    if (ne < FT_NNOTE && ft_notes[ne].t < next) next = ft_notes[ne].t;
    return next > pos ? next : pos;
}

/* The engine's path (voices going idle and coming back: the pinna's tail)
 * against the Q2.30 path on the same voices' fm_render_q30 output, bit for
 * bit; and the pan law at W = 32768 against sp_stereo_render. */
static int ft_paths(const ft_case_t *c, const int16_t *table, int stereo)
{
    static fm_voice_t va[FT_NV], vb[FT_NV];
    static int32_t src[FT_N * FT_NV], tmp[FT_N];
    static int16_t oa[FT_N * 16], ob[FT_N * 16];
    sp_field_t *f = (sp_field_t *)calloc(1, sizeof *f);
    sp_stereo_t *s = (sp_stereo_t *)calloc(1, sizeof *s);
    fm_patch_t p;
    uint32_t i, pos, e, ne, ca = 0, cb = 0, seed = 77, ch = ft_ch(c), diff = 0;
    int32_t yaw = 0;
    int bad;
    if (!f || !s) { fprintf(stderr, "oom\n"); exit(1); }
    ft_patch(&p);
    for (i = 0; i < FT_NV; i++) { fm_voice_init(&va[i], &p, FT_FS); fm_voice_init(&vb[i], &p, FT_FS); }
    if (ft_init(c, f, table) || sp_stereo_init(s, c->block, FT_NEAR)) { fprintf(stderr, "init failed\n"); exit(1); }
    for (pos = 0, e = 0, ne = 0; pos < FT_N; ) {                              /* path A: the engine inside the render */
        uint32_t len;
        while (e < FT_NEV && ft_ev[e].t == pos) ft_apply(c, stereo ? s : &f->geo, stereo ? NULL : f, &yaw, &ft_ev[e++]);
        ft_notes_at(va, pos, &ne);
        len = 1 + ft_lcg(&seed) % 150u;
        if (len > ft_next(pos, e, ne) - pos) len = ft_next(pos, e, ne) - pos;
        ca += stereo ? sp_stereo_render(s, va, FT_NV, oa + (size_t)pos * ch, len) : sp_field_render(f, va, FT_NV, oa + (size_t)pos * ch, len);
        pos += len;
    }
    for (pos = 0, ne = 0; pos < FT_N; ) {                                     /* the voices alone, Q2.30 */
        uint32_t len, t;
        ft_notes_at(vb, pos, &ne);
        len = ft_next(pos, FT_NEV, ne) - pos;
        for (i = 0; i < FT_NV; i++) {
            fm_render_q30(&vb[i], tmp, len);
            for (t = 0; t < len; t++) src[(size_t)(pos + t) * FT_NV + i] = tmp[t];
        }
        pos += len;
    }
    if (stereo) {                                                              /* path B: the pan law at W = 32768 on the engine */
        for (i = 0; i < FT_NV; i++) fm_voice_init(&vb[i], &p, FT_FS);
        if (ft_init(c, f, table)) exit(1);
        for (pos = 0, e = 0, ne = 0; pos < FT_N; ) {
            uint32_t len;
            while (e < FT_NEV && ft_ev[e].t == pos) ft_apply(c, &f->geo, f, &yaw, &ft_ev[e++]);
            ft_notes_at(vb, pos, &ne);
            len = 1 + ft_lcg(&seed) % 150u;
            if (len > ft_next(pos, e, ne) - pos) len = ft_next(pos, e, ne) - pos;
            cb += sp_field_render(f, vb, FT_NV, ob + (size_t)pos * ch, len);
            pos += len;
        }
    } else cb = ft_run(c, src, table, ob, 4242);                               /* path B: the Q2.30 path */
    for (i = 0; i < FT_N * ch; i++) diff += oa[i] != ob[i];
    bad = diff || ca != cb;
    printf("FIELD %-18s %s differing_samples=%u clipped=%u/%u hash=%016llx %s\n", c->name,
           stereo ? "sp_stereo_render vs pan law W=32768:" : "engine path vs Q2.30 path:", diff, ca, cb,
           (unsigned long long)fm_golden_hash(oa, FT_N * ch), bad ? "FAIL" : "ok");
    free(f); free(s);
    return bad;
}

/* The headroom at its bounds (SPEC section 12): 64 voices of int32
 * extremes at full gain, the pinna on, a full-range table. Under UBSan any
 * overflow aborts; here the render must count clips and not fail. */
static int ft_headroom(int route, const int16_t *table)
{
    enum { NH = 2048 };
    static int32_t src[NH * SP_MAX_VOICES];
    static int16_t out[NH * 16];
    sp_field_t *f = (sp_field_t *)calloc(1, sizeof *f);
    sp_field_cfg_t cfg;
    uint32_t t, i, seed = 3, clipped;
    if (!f) { fprintf(stderr, "oom\n"); exit(1); }
    memset(&cfg, 0, sizeof cfg);
    cfg.route = route;
    cfg.order = 3;
    cfg.block = 128;
    cfg.near_mm = FT_NEAR;
    cfg.fs = FT_FS;
    cfg.pinna = SP_PINNA_PBNH;
    cfg.table = table;
    cfg.table_shift = FT_SHIFT;
    if (sp_field_init(f, &cfg)) exit(1);
    for (t = 0; t < NH; t++)
        for (i = 0; i < SP_MAX_VOICES; i++) src[t * SP_MAX_VOICES + i] = (ft_lcg(&seed) >> 31) ? INT32_MAX : INT32_MIN;
    for (i = 0; i < SP_MAX_VOICES; i++) sp_stereo_set_pos(&f->geo, i, i % 3 ? 0 : 1, 1000, (int32_t)(i % 5) * 10);
    clipped = sp_field_render_q30(f, src, SP_MAX_VOICES, SP_MAX_VOICES, out, NH);
    printf("FIELD headroom-%-9s 64 voices of int32 extremes, pinna on: clipped=%u of %u samples%s %s\n",
           route == SP_FIELD_AMB3 ? "amb3" : "binaural", clipped, NH * (route == SP_FIELD_AMB3 ? 16u : 18u),
           route == SP_FIELD_AMB3 ? "" : " (field and ears)", clipped ? "ok" : "FAIL");
    free(f);
    return !clipped;
}

int field_test(void)
{
    static int16_t table[SP_FIELD_MAX_CH * SP_FIELD_TAPS];
    static const uint32_t shifts[4] = { 1, 14, 15, 30 };
    int bad = 0, k;
    bad += ft_cases_d14();
    for (k = 0; k < 4; k++) {
        bad += ft_decoder(3, shifts[k]);
        bad += ft_decoder(1, shifts[k]);
    }
    ft_table(table, 0, 99);
    bad += ft_paths(&ft_cases[5], table, 0);
    bad += ft_paths(&ft_cases[10], table, 0);
    bad += ft_paths(&ft_cases[12], table, 1);
    ft_table(table, 1, 4321);
    bad += ft_headroom(SP_FIELD_AMB3, table);
    bad += ft_headroom(SP_FIELD_BINAURAL, table);
    if (bad) { printf("FIELD FAIL %d\n", bad); return 1; }
    printf("FIELD ok\n");
    return 0;
}

/* The cost per frame at 48 kHz on this host (clock, CPU time): the Q2.30
 * path, one second of LCG sources per run, for the routes at 1, 16 and 64
 * voices, the pinna off and on; the binaural table is the LCG's, with G's
 * zero channels. */
int field_cost(void)
{
    enum { NC = 48000 };
    static const int routes[4] = { SP_FIELD_AMB1, SP_FIELD_AMB3, SP_FIELD_BINAURAL, SP_FIELD_BINAURAL };
    static const char *names[4] = { "amb1", "amb3", "binaural1", "binaural3" };
    static const uint32_t nvs[3] = { 1, 16, 64 };
    static int16_t table[SP_FIELD_MAX_CH * SP_FIELD_TAPS], out[NC * 16];
    int32_t *src = (int32_t *)malloc(sizeof *src * NC * SP_MAX_VOICES);
    sp_field_t *f = (sp_field_t *)calloc(1, sizeof *f);
    uint32_t i, seed = 11;
    int r, v, pin;
    if (!src || !f) { fprintf(stderr, "oom\n"); return 1; }
    ft_table(table, 0, 99);
    for (i = 0; i < NC * SP_MAX_VOICES; i++) src[i] = (int32_t)ft_lcg(&seed) >> 6;
    for (r = 0; r < 4; r++)
        for (pin = 0; pin < 2; pin++)
            for (v = 0; v < 3; v++) {
                sp_field_cfg_t cfg;
                clock_t c0;
                double ns;
                memset(&cfg, 0, sizeof cfg);
                cfg.route = routes[r];
                cfg.order = r == 2 ? 1 : 3;
                cfg.block = 64;
                cfg.near_mm = FT_NEAR;
                cfg.fs = FT_FS;
                cfg.pinna = pin ? SP_PINNA_PBNH : SP_PINNA_OFF;
                cfg.table = table;
                cfg.table_shift = FT_SHIFT;
                if (sp_field_init(f, &cfg)) return 1;
                for (i = 0; i < nvs[v]; i++) sp_stereo_move(&f->geo, i, (int32_t)(i * 97 % 2000) - 1000, 1000, (int32_t)(i * 31 % 600) - 300, NC);
                c0 = clock();
                sp_field_render_q30(f, src, SP_MAX_VOICES, nvs[v], out, NC);
                ns = (double)(clock() - c0) / CLOCKS_PER_SEC * 1e9 / NC;
                printf("COST %-9s pinna=%d voices=%-2u ns_per_frame=%8.1f ns_per_voice_frame=%7.2f cpu_at_48k=%6.2f%%\n",
                       names[r], pin, nvs[v], ns, ns / nvs[v], ns * 48000 / 1e7);
            }
    free(src); free(f);
    return 0;
}

/* P275 reusable field model. The SH recurrence and real pinna above are
 * independent of the library's rounded polynomial/tap implementation. */
#define FR_SOURCES (SP_MAX_VOICES + 2u * FX_RETURNS)
struct fieldr {
    sp_stereo_t geo;                 /* independent input geometry and clock only */
    sp_field_cfg_t cfg;
    fxr_t *fx;
    uint32_t nsh,ch,pd[5];
    int32_t yaw;
    uint32_t send[SP_MAX_VOICES][FX_BUSES],prev[SP_MAX_VOICES][FX_BUSES],target[SP_MAX_VOICES][FX_BUSES];
    double gp[FR_SOURCES][16],gt[FR_SOURCES][16];
    double hp[FR_SOURCES][SP_PINNA_TAPS],ht[FR_SOURCES][SP_PINNA_TAPS];
    double history[FR_SOURCES][SP_PINNA_HIST];
    double field[16][SP_FIELD_TAPS];
};

void fieldr_free(fieldr_t *r)
{
    if (!r) return;
    fxr_free(r->fx);
    free(r);
}

fieldr_t *fieldr_new(const sp_field_cfg_t *cfg,const fx_cfg_t *fxcfg)
{
    sp_field_t *f = (sp_field_t *)calloc(1,sizeof *f);
    fx_t *x = (fx_t *)calloc(1,sizeof *x);
    size_t bytes = fx_field_mem_bytes(fxcfg,cfg->fs);
    void *mem = malloc(bytes+8u);
    fieldr_t *r = (fieldr_t *)calloc(1,sizeof *r);
    uint32_t j;
    int bad = !f || !x || !mem || !r || sp_field_init(f,cfg)
        || fx_field_init(x,cfg->fs,fxcfg,mem,bytes) || sp_field_attach_fx(f,x);
    if (!bad) { r->nsh=f->nsh;r->ch=f->ch; }
    free(mem);free(x);free(f);
    if (bad) { free(r);return NULL; }
    r->fx = fxr_field_new(fxcfg,cfg->fs);
    if (!r->fx) { free(r);return NULL; }
    r->cfg = *cfg;r->yaw=cfg->yaw_mdeg;
    if (sp_stereo_init(&r->geo,cfg->block,cfg->near_mm)) {fieldr_free(r);return NULL;}
    for (j=0;j<5;j++) r->pd[j]=cfg->pinna==SP_PINNA_D?cfg->pinna_d[j]:cfg->pinna==SP_PINNA_RD?ft_drd[j]:ft_dpb[j];
    return r;
}

void fieldr_set_pos(fieldr_t *r,uint32_t voice,int32_t x,int32_t y,int32_t z)
{ sp_stereo_set_pos(&r->geo,voice,x,y,z); }
void fieldr_move(fieldr_t *r,uint32_t voice,int32_t x,int32_t y,int32_t z,uint32_t end)
{ sp_stereo_move(&r->geo,voice,x,y,z,end); }
void fieldr_set_yaw(fieldr_t *r,int32_t mdeg) { r->yaw=mdeg; }
void fieldr_set_send(fieldr_t *r,uint32_t voice,uint32_t bus,uint32_t level)
{ if(voice<SP_MAX_VOICES&&bus<FX_BUSES)r->send[voice][bus]=level>32768?32768:level; }
void fieldr_set_crush(fieldr_t *r,uint32_t voice,uint32_t bits,uint32_t div)
{ fxr_set_crush(r->fx,voice,bits,div); }

static void fr_targets(fieldr_t *r,uint32_t source)
{
    double u[3],w[3],sh[16],hv[3],gd=1.0,angle=r->yaw*(FT_PI/180000.0);
    uint32_t c;
    if(source<SP_MAX_VOICES) {
        const sp_voice_t *v=&r->geo.v[source];uint32_t b=r->geo.n/r->geo.k;
        int32_t x=sp_coord_at(v,b,v->x0,v->x),y=sp_coord_at(v,b,v->y0,v->y),z=sp_coord_at(v,b,v->z0,v->z);
        uint64_t sq=(uint64_t)((int64_t)x*x)+(uint64_t)((int64_t)y*y)+(uint64_t)((int64_t)z*z);
        uint32_t rad=sp_isqrt64(sq),rc=rad+((uint64_t)rad*rad<sq),m=rc>r->geo.near_mm?rc:r->geo.near_mm;
        gd=1000.0/(rad>1000?rad:1000);u[0]=(double)y/m;u[1]=-(double)x/m;u[2]=(double)z/m;
    } else {
        uint32_t m=r->geo.near_mm>1000?r->geo.near_mm:1000;
        /* Exact pinned radial truncation, independently with host division. */
        u[0]=(double)((UINT64_C(929887696)*1000u)/m)/1073741824.0;
        u[1]=(double)((UINT64_C(536870912)*1000u)/m)/1073741824.0;
        if (source & 1u) u[1] = -u[1];
        u[2] = 0;
    }
    w[0]=u[0]*cos(angle)-u[1]*sin(angle);w[1]=u[0]*sin(angle)+u[1]*cos(angle);w[2]=u[2];
    for(c=0;c<2;c++)if(fabs(w[c])<1e-12)w[c]=0;
    ft_sh(r->nsh==4?1:3,w,sh);
    for(c=0;c<r->nsh;c++)r->gt[source][c]=fmax(-1.0,fmin(1.0,sh[c]*gd));
    hv[0]=-w[1];hv[1]=w[0];hv[2]=w[2];
    if(r->cfg.pinna)ft_pinna_at(hv,r->pd,r->ht[source],r->cfg.fs);
}

static void fr_boundary(fieldr_t *r,uint32_t count)
{
    uint32_t i,c,b;int duck=0;
    for(i=0;i<FR_SOURCES;i++) {
        if(i>=count&&i<SP_MAX_VOICES)continue;
        for(c=0;c<r->nsh;c++)r->gp[i][c]=r->gt[i][c];
        for(c=0;c<SP_PINNA_TAPS;c++)r->hp[i][c]=r->ht[i][c];
        fr_targets(r,i);
        if(!r->geo.n) {
            for(c=0;c<r->nsh;c++)r->gp[i][c]=r->gt[i][c];
            for(c=0;c<SP_PINNA_TAPS;c++)r->hp[i][c]=r->ht[i][c];
        }
    }
    for(i=0;i<SP_MAX_VOICES;i++)for(b=0;b<FX_BUSES;b++) {
        r->prev[i][b]=r->geo.n?r->target[i][b]:r->send[i][b];r->target[i][b]=r->send[i][b];
        if(b==FX_DUCK&&(r->prev[i][b]||r->target[i][b]))duck=1;
    }
    fxr_duck(r->fx,duck);
}

static void fr_encode(fieldr_t *r,uint32_t i,double x,double w,double *acc)
{
    uint32_t n=r->geo.n,c,j;double y=x;
    r->history[i][n%SP_PINNA_HIST]=x;
    if(r->cfg.pinna) {
        y=0;
        for(j=0;j<SP_PINNA_TAPS;j++)
            y+=(r->hp[i][j]+(r->ht[i][j]-r->hp[i][j])*w)*r->history[i][(n-j)%SP_PINNA_HIST];
    }
    for(c=0;c<r->nsh;c++)acc[c]+=y*(r->gp[i][c]+(r->gt[i][c]-r->gp[i][c])*w);
}

void fieldr_step(fieldr_t *r,const int32_t *raw_q30,uint32_t count,double *out)
{
    uint32_t n=r->geo.n,i,b,c,j;double w=(n%r->geo.k+1.0)/r->geo.k;
    double acc[16]={0},bus[FX_BUSES]={0},wet[FX_RETURNS][2],master[16]={0};
    if(count>SP_MAX_VOICES)count=SP_MAX_VOICES;
    if(n%r->geo.k==0)fr_boundary(r,count);
    for(i=0;i<count;i++) {
        int32_t sample=fxr_crush(r->fx,i,raw_q30[i]);double x=sample/1073741824.0;
        for(b=0;b<FX_BUSES;b++) {
            int64_t q=((int64_t)r->prev[i][b]*r->geo.k+((int64_t)r->target[i][b]-r->prev[i][b])*(n%r->geo.k+1u))/r->geo.k;
            bus[b]+=x*(q/32768.0);
        }
        fr_encode(r,i,x,w,acc);
    }
    fxr_field_returns(r->fx,bus,wet);
    for(b=0;b<FX_RETURNS;b++)for(c=0;c<2;c++)fr_encode(r,SP_MAX_VOICES+2*b+c,wet[b][c],w,acc);
    if(r->cfg.route==SP_FIELD_BINAURAL) {
        double eo[2]={0};
        for(c=0;c<r->nsh;c++) {
            r->field[c][n%SP_FIELD_TAPS]=acc[c];
            for(j=0;j<SP_FIELD_TAPS;j++)eo[(FT_ODD>>c)&1u]+=r->cfg.table[c*SP_FIELD_TAPS+j]*r->field[c][(n-j)%SP_FIELD_TAPS];
        }
        master[0]=ldexp(eo[0]+eo[1],-(int)r->cfg.table_shift);master[1]=ldexp(eo[0]-eo[1],-(int)r->cfg.table_shift);
    } else for(c=0;c<r->ch;c++)master[c]=acc[c];
    fxr_master_post(r->fx,bus[FX_DUCK],master,out);r->geo.n=n+1u;
}
