/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * fx_ref.c — host-only double model of the effects module (fx_ref.h).
 * Each stage is the effect's definition in real
 * arithmetic; the integer module's rounding, Q formats and saturation are
 * what the differential test (harness --fxref) measures against it.
 */
#include <math.h>
#include <stdlib.h>
#include "fx_ref.h"

#define FXR_PI 3.14159265358979323846

static const unsigned FXR_LEN44[12] = { 1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617, 556, 441, 341, 225 };

typedef struct {
    double  *buf;
    uint32_t mask, len;
    double   store;
} fxr_line_t;

struct fxr {
    uint32_t   on, fs, ch, bus_ch, k, m, n;
    int32_t held[FX_MAX_VOICES];
    unsigned left[FX_MAX_VOICES], bits[FX_MAX_VOICES], div[FX_MAX_VOICES];
    unsigned next_bits[FX_MAX_VOICES], next_div[FX_MAX_VOICES];
    double     gr;                               /* the returns' gain: 1 in mono, 23170 / 32768 in stereo */
    double     dc_r, dc_x1[FX_MAX_CH], dc_y1[FX_MAX_CH];
    double     thr, c, g0, g1, s, peak, *ring;   /* the limiter */
    fxr_line_t cho;
    double     cho_base, cho_depth;              /* samples */
    uint32_t   cho_inc;                          /* the LFO's phase step, 2^-32 cycles: the SPEC's rounding */
    fxr_line_t flg;
    double flg_base, flg_depth, flg_fb;
    uint32_t flg_inc, flg_invert;
    fxr_line_t dly;                              /* T, or 2T for the ping-pong */
    double     dly_a, dly_fb, dly_lp;
    uint32_t   dly_t;                            /* the ping-pong's left tap  */
    double     dly_ha, dly_hp;                   /* its loop high-pass: coefficient (0: none) and lp2 */
    fxr_line_t comb[2][8], allp[2][4];
    double     rv_fb, rv_damp;
    double     gain;                             /* the master gain, 10^(GAIN_DB10 / 200) */
    uint32_t   dith;                             /* the dither's xorshift32 word: fx/'s sequence */
    double     comp_peak, comp_makeup;           /* independent real detector and signal gain */
    uint32_t   comp_log[65], comp_atten[11], comp_dbscale;
    uint32_t   comp_db10, comp_ratio, comp_attack, comp_release;
    uint32_t   comp_g, comp_g0[2], comp_g1[2];    /* specified discrete control law, own state */
    uint32_t   comp_duck;
};

void fxr_set_crush(fxr_t *r, uint32_t voice, uint32_t bits, uint32_t div)
{
    if (voice >= FX_MAX_VOICES) return;
    r->next_bits[voice] = bits == 0 ? 1 : bits > 24 ? 24 : bits;
    r->next_div[voice] = div == 0 ? 1 : div > 64 ? 64 : div;
}

int32_t fxr_crush(fxr_t *r, uint32_t voice, int32_t sample)
{
    int64_t unit, value;
    if (!(r->on & FX_ON_CRUSH) || voice >= FX_MAX_VOICES) return sample;
    if (r->n % r->k == 0) {
        if (r->bits[voice] != r->next_bits[voice] || r->div[voice] != r->next_div[voice]) r->left[voice] = 0;
        r->bits[voice] = r->next_bits[voice];
        r->div[voice] = r->next_div[voice];
    }
    if (r->bits[voice] == 24 && r->div[voice] == 1) return sample;
    if (r->left[voice] != 0) r->left[voice]--;
    else {
        unit = (int64_t)1 << (30 - r->bits[voice]);
        value = sample / unit;
        if (sample < 0 && sample % unit != 0) value--;
        r->held[voice] = (int32_t)(value * unit);
        r->left[voice] = r->div[voice] - 1;
    }
    return r->held[voice];
}

static uint32_t fxr_pow2(uint32_t v)
{
    uint32_t p = 1;
    while (p < v) p <<= 1;
    return p;
}

static int fxr_line(fxr_line_t *l, uint32_t len, uint32_t size)
{
    l->len  = len;
    l->mask = fxr_pow2(size) - 1u;
    l->buf  = (double *)calloc((size_t)l->mask + 1u, sizeof *l->buf);
    return l->buf ? 0 : -1;
}

/* round(v * fs / d), half up. */
static uint32_t fxr_round(double v, uint32_t fs, double d)
{
    return (uint32_t)floor(v * fs / d + 0.5);
}

void fxr_free(fxr_t *r)
{
    int s, i;
    if (!r) return;
    free(r->ring);
    free(r->cho.buf);
    free(r->flg.buf);
    free(r->dly.buf);
    for (s = 0; s < 2; s++) {
        for (i = 0; i < 8; i++) free(r->comb[s][i].buf);
        for (i = 0; i < 4; i++) free(r->allp[s][i].buf);
    }
    free(r);
}

void fxr_duck(fxr_t *r, int configured)
{
    r->comp_duck = configured != 0;
}

static fxr_t *fxr_new_mode(const fx_cfg_t *cfg, uint32_t fs, int field)
{
    fx_t *probe = (fx_t *)malloc(sizeof *probe);
    size_t bytes = field ? fx_field_mem_bytes(cfg, fs) : fx_mem_bytes(cfg, fs);
    void *mem = malloc(bytes + 8u);
    fxr_t *r = (fxr_t *)calloc(1, sizeof *r);
    int bad = !probe || !mem || !r || (field ? fx_field_init(probe, fs, cfg, mem, bytes) : fx_init(probe, fs, cfg, mem, bytes));
    uint32_t s, i;
    free(probe);
    free(mem);
    if (bad) { free(r); return NULL; }
    r->on = cfg->on;
    r->fs = fs;
    r->ch = cfg->channels;
    r->bus_ch = field ? 2u : r->ch;
    r->k  = cfg->block;
    r->m  = cfg->block / 2u;
    for (i = 0; i < FX_MAX_VOICES; i++) {
        r->bits[i] = r->next_bits[i] = 24;
        r->div[i] = r->next_div[i] = 1;
    }
    r->gr = r->ch == 1u ? 1.0 : 23170.0 / 32768.0;
    r->g0 = r->g1 = r->s = 1.0;
    r->gain = (r->on & FX_ON_GAIN) ? pow(10.0, cfg->gain_db10 / 200.0) : 1.0;
    r->dith = cfg->dither_seed;
    if (r->on & FX_ON_DC) {
        r->dc_r = 1.0 - 2.0 * FXR_PI * (cfg->dc_millihz / 1000.0) / fs;
        if (r->dc_r < 0) r->dc_r = 0;
    }
    if (r->on & FX_ON_LIMITER) {
        double rel = floor((double)cfg->limiter_release_ms * fs / 1000.0 + 0.5), cap = (ldexp(1.0, 45) - ldexp(1.0, 29) - 1.0) / ldexp(1.0, 45);
        r->thr = pow(10.0, -(double)cfg->limiter_db10 / 200.0);
        if (r->thr > cap) r->thr = cap;
        r->c = r->m / rel < 1.0 ? r->m / rel : 1.0;
    }
    if (r->on & (FX_ON_LIMITER | FX_ON_COMPRESSOR)) {
        r->ring = (double *)calloc((size_t)r->ch * r->k, sizeof *r->ring);
        if (!r->ring) { fxr_free(r); return NULL; }
    }
    if (r->on & FX_ON_COMPRESSOR) {
        double attack = floor((double)cfg->compressor_attack_ms * fs / 1000.0 + 0.5);
        double release = floor((double)cfg->compressor_release_ms * fs / 1000.0 + 0.5);
        double ca = attack == 0 ? 1073741824.0 : floor(r->m * 1073741824.0 / attack);
        double cr = floor(r->m * 1073741824.0 / release);
        r->comp_db10 = cfg->compressor_db10;
        r->comp_ratio = cfg->compressor_ratio_tenths;
        r->comp_attack = (uint32_t)(ca > 1073741824.0 ? 1073741824.0 : ca);
        r->comp_release = (uint32_t)(cr > 1073741824.0 ? 1073741824.0 : cr);
        r->comp_makeup = pow(10.0, cfg->compressor_makeup_db10 / 200.0);
        r->comp_g = r->comp_g0[0] = r->comp_g1[0] = r->comp_g0[1] = r->comp_g1[1] = 1u << 30;
        /* Independent host evaluation of the pinned constants. */
        for (i = 0; i <= 64u; i++)
            r->comp_log[i] = (uint32_t)floor(32768.0 * log2(1.0 + i / 64.0) + 0.5);
        for (i = 0; i < 11u; i++)
            r->comp_atten[i] = (uint32_t)floor(2147483648.0 * pow(10.0, -ldexp(1.0, (int)i) / 200.0) + 0.5);
        r->comp_dbscale = (uint32_t)floor(65536.0 * 200.0 * log10(2.0) + 0.5);
    }
    if (r->on & FX_ON_CHORUS) {
        r->cho_base  = (double)cfg->chorus_base_us * fs / 1e6;
        r->cho_depth = (double)cfg->chorus_depth_us * fs / 1e6;
        r->cho_inc   = (uint32_t)((((uint64_t)cfg->chorus_rate_millihz << 32) + 500u * (uint64_t)fs) / (1000u * (uint64_t)fs));
        if (fxr_line(&r->cho, 0, (uint32_t)(r->cho_base + r->cho_depth) + 2u)) { fxr_free(r); return NULL; }
    }
    if (r->on & FX_ON_FLANGER) {
        r->flg_base = (double)cfg->flanger_base_us * fs / 1e6;
        r->flg_depth = (double)cfg->flanger_depth_us * fs / 1e6;
        r->flg_inc = (uint32_t)((((uint64_t)cfg->flanger_rate_millihz << 32) + 500u * (uint64_t)fs) / (1000u * (uint64_t)fs));
        r->flg_fb = cfg->flanger_fb_q15 / 32768.0;
        r->flg_invert = cfg->flanger_invert;
        if (fxr_line(&r->flg, 0, (uint32_t)(r->flg_base + r->flg_depth) + 2u)) { fxr_free(r); return NULL; }
    }
    if (r->on & FX_ON_DELAY) {
        uint32_t len = fxr_round(cfg->delay_us, fs, 1e6);
        r->dly_t = len;
        if (r->on & FX_ON_PINGPONG) len *= 2u;
        if (fxr_line(&r->dly, len, len)) { fxr_free(r); return NULL; }
        r->dly_a  = 1.0 - exp(-2.0 * FXR_PI * cfg->delay_lp_hz / fs);
        r->dly_fb = cfg->delay_fb_q15 / 32768.0;
        if ((r->on & FX_ON_PINGPONG) && cfg->delay_hp_hz) r->dly_ha = 1.0 - exp(-2.0 * FXR_PI * cfg->delay_hp_hz / fs);
    }
    if (r->on & FX_ON_REVERB) {                  /* Freeverb: roomsize 0.7 + 0.28 room, damp 0.4 damp */
        r->rv_fb   = 0.7 + 0.28 * (cfg->reverb_room / 1000.0);
        r->rv_damp = 0.4 * (cfg->reverb_damp / 1000.0);
        for (s = 0; s < r->bus_ch; s++)
            for (i = 0; i < 12u; i++) {
                uint32_t len = fxr_round(FXR_LEN44[i] + 23.0 * s, fs, 44100.0);
                if (fxr_line(i < 8u ? &r->comb[s][i] : &r->allp[s][i - 8u], len, len)) { fxr_free(r); return NULL; }
            }
    }
    return r;
}

fxr_t *fxr_new(const fx_cfg_t *cfg, uint32_t fs)
{
    return fxr_new_mode(cfg, fs, 0);
}

fxr_t *fxr_field_new(const fx_cfg_t *cfg, uint32_t fs)
{
    return fxr_new_mode(cfg, fs, 1);
}

static double fxr_at(const fxr_line_t *l, uint32_t n)
{
    return l->buf[n & l->mask];
}

/* The chorus tap at LFO phase p (cycles): base + depth * tri(p), tri from
 * 1 at p = 0 down to -1 at p = 1/2, read between the two samples around it. */
static double fxr_tap(const fxr_t *r, uint32_t n, double p)
{
    double u = 4.0 * fabs(p - floor(p) - 0.5) - 1.0, d = r->cho_base + r->cho_depth * u, f;
    uint32_t di = (uint32_t)floor(d);
    f = d - di;
    return fxr_at(&r->cho, n - di) * (1.0 - f) + fxr_at(&r->cho, n - di - 1u) * f;
}

/* Independent real flanger; the one-sample causal boundary is specified. */
static double fxr_flg_tap(const fxr_t *r, uint32_t n, double p)
{
    double d = r->flg_base + r->flg_depth * (4.0 * fabs(p - floor(p) - 0.5) - 1.0), f;
    uint32_t di;
    if (d < 1.0) d = 1.0;
    di = (uint32_t)floor(d); f = d - di;
    return fxr_at(&r->flg, n - di) * (1.0 - f) + fxr_at(&r->flg, n - di - 1u) * f;
}

static double fxr_clip27(double x)
{
    const double hi = 16.0 - 1.0 / 134217728.0;
    return x < -16.0 ? -16.0 : x > hi ? hi : x;
}

static double fxr_reverb(fxr_t *r, uint32_t s, uint32_t n, double in)
{
    double x = in * 0.015, sum = 0;
    int i;
    for (i = 0; i < 8; i++) {
        fxr_line_t *l = &r->comb[s][i];
        double out = fxr_at(l, n - l->len);
        l->store = out * (1.0 - r->rv_damp) + l->store * r->rv_damp;
        l->buf[n & l->mask] = x + l->store * r->rv_fb;
        sum += out;
    }
    x = sum;
    for (i = 0; i < 4; i++) {
        fxr_line_t *l = &r->allp[s][i];
        double bo = fxr_at(l, n - l->len);
        l->buf[n & l->mask] = x + bo * 0.5;
        x = bo - x;
    }
    return x;
}

/* fx/'s dither : the same two xorshift32 draws, their sum in the
 * rounding's units, an output LSB being 2^30. The one integer part of the
 * signal-output sequence in the model: a TPDF is not a value to approximate. */
static double fxr_dither(fxr_t *r)
{
    uint32_t w = r->dith;
    int32_t  a, b;
    w ^= w << 13; w ^= w >> 17; w ^= w << 5;
    a = (int32_t)(w >> 2) - (1 << 29);
    w ^= w << 13; w ^= w >> 17; w ^= w << 5;
    b = (int32_t)(w >> 2) - (1 << 29);
    r->dith = w;
    return ((double)a + (double)b) / 1073741824.0;
}

/* The prescribed integer control law, independently evaluated from this
 * model's own peak. No native state is read or adopted. */
static int64_t fxr_floor_div(int64_t n, int64_t d)
{
    return n < 0 ? -((-n + d - 1) / d) : n / d;
}

static uint32_t fxr_comp_target(const fxr_t *r)
{
    uint64_t peak = (uint64_t)floor(r->comp_peak * 35184372088832.0 + 0.5), q, product;
    uint32_t exponent = 0, norm, index, fraction, logarithm, reduction, gain = 1u << 31, i;
    int32_t level, over;
    if (!peak) return 1u << 30;
    for (q = peak; q > 1; q /= 2) exponent++;
    norm = exponent >= 31 ? (uint32_t)(peak / ((uint64_t)1 << (exponent - 31u)))
         : (uint32_t)(peak * ((uint64_t)1 << (31u - exponent)));
    index = (norm / 33554432u) % 64u;
    fraction = norm % 33554432u;
    product = (uint64_t)(r->comp_log[index + 1u] - r->comp_log[index]) * fraction;
    logarithm = r->comp_log[index] + (uint32_t)((product + 16777216u) / 33554432u);
    level = (int32_t)fxr_floor_div(((int64_t)((int32_t)exponent - 45) * 32768 + logarithm)
                                   * r->comp_dbscale + 1073741824, 2147483648LL);
    over = level + (int32_t)r->comp_db10;
    if (over <= 0) return 1u << 30;
    reduction = (uint32_t)(((uint64_t)(uint32_t)over * (r->comp_ratio - 10u)) / r->comp_ratio);
    for (i = 0; i < 11u; i++)
        if (reduction & (1u << i))
            gain = (uint32_t)(((uint64_t)gain * r->comp_atten[i] + 1073741824u) / 2147483648u);
    return (gain + 1u) / 2u;
}

static uint32_t fxr_comp_ramp(const fxr_t *r, uint32_t slot, uint32_t j)
{
    int64_t delta = (int64_t)r->comp_g1[slot] - r->comp_g0[slot];
    return (uint32_t)((int64_t)r->comp_g0[slot] + fxr_floor_div(delta * (j + 1u), r->m));
}

static void fxr_comp_boundary(fxr_t *r)
{
    uint32_t target = fxr_comp_target(r), old = r->comp_g, falling = target < old;
    uint32_t delta = falling ? old - target : target - old;
    uint32_t coefficient = falling ? r->comp_attack : r->comp_release;
    uint32_t step = (uint32_t)(((uint64_t)delta * coefficient + 1073741823u) / 1073741824u);
    uint32_t base = (r->n - r->m) % r->k, slot = base / r->m;
    r->comp_g = falling ? old - step : old + step;
    r->comp_g0[slot] = old;
    r->comp_g1[slot] = r->comp_g;
    r->comp_peak = 0;
    if (r->on & FX_ON_LIMITER) {
        uint32_t j, c;
        r->peak = 0;
        for (j = 0; j < r->m; j++) {
            double gain = fxr_comp_ramp(r, slot, j) / 1073741824.0;
            for (c = 0; c < r->ch; c++) {
                double peak = fabs(r->ring[(base + j) * r->ch + c]) * gain;
                if (peak > r->peak) r->peak = peak;
            }
        }
    }
}

void fxr_field_returns(fxr_t *r, const double *bus, double wet[FX_RETURNS][2])
{
    uint32_t n = r->n, b;
    for (b = 0; b < FX_RETURNS; b++) wet[b][0] = wet[b][1] = 0;
    if (r->on & FX_ON_CHORUS) {
        double p = (double)(uint32_t)((uint64_t)n * r->cho_inc) / 4294967296.0;   /* n steps of the LFO, exact */
        r->cho.buf[n & r->cho.mask] = bus[FX_CHORUS];
        wet[FX_RETURN_CHORUS][0] = fxr_tap(r, n, p);
        if (r->bus_ch == 2u) wet[FX_RETURN_CHORUS][1] = fxr_tap(r, n, p + 0.25);
    }
    if (r->on & FX_ON_FLANGER) {
        double p = (double)(uint32_t)((uint64_t)n * r->flg_inc) / 4294967296.0;
        double lt = fxr_flg_tap(r, n, p);
        double rt = r->bus_ch == 2u ? fxr_flg_tap(r, n, p + 0.25) : 0.0;
        double feedback = lt * r->flg_fb;
        if (r->flg_invert) feedback = -feedback;
        r->flg.buf[n & r->flg.mask] = fxr_clip27(fxr_clip27(bus[FX_FLANGER]) + feedback);
        wet[FX_RETURN_FLANGER][0] = lt; wet[FX_RETURN_FLANGER][1] = rt;
    }
    if (r->on & FX_ON_DELAY) {
        double o = fxr_at(&r->dly, n - r->dly.len);
        r->dly_lp += r->dly_a * (o - r->dly_lp);
        if (r->on & FX_ON_PINGPONG) {            /* : left the tap at T, right at 2T, the high-pass in the loop */
            r->dly_hp += r->dly_ha * (r->dly_lp - r->dly_hp);
            r->dly.buf[n & r->dly.mask] = bus[FX_DELAY] + (r->dly_lp - r->dly_hp) * r->dly_fb;
            wet[FX_RETURN_DELAY][0] = fxr_at(&r->dly, n - r->dly_t);
            wet[FX_RETURN_DELAY][1] = o;
        } else {
            r->dly.buf[n & r->dly.mask] = bus[FX_DELAY] + r->dly_lp * r->dly_fb;
            wet[FX_RETURN_DELAY][0] = o;
            wet[FX_RETURN_DELAY][1] = o;
        }
    }
    if (r->on & FX_ON_REVERB) {
        wet[FX_RETURN_REVERB][0] = fxr_reverb(r, 0, n, bus[FX_REVERB]);
        if (r->bus_ch == 2u) wet[FX_RETURN_REVERB][1] = fxr_reverb(r, 1, n, bus[FX_REVERB]);
    }
}

static void fxr_post(fxr_t *r, double duck, const double *dry, double *out, double rl, double rr)
{
    uint32_t n = r->n, c;
    double g = 1.0;
    if ((r->on & FX_ON_COMPRESSOR) && n % r->m == 0) fxr_comp_boundary(r);
    if (r->on & FX_ON_LIMITER) {
        uint32_t j = n % r->m;
        if (j == 0) {                            /* the half-block boundary: fx_lim_boundary in real arithmetic */
            double s = r->peak <= r->thr ? 1.0 : r->thr / r->peak, gn = r->g1 + (1.0 - r->g1) * r->c;
            if (s < gn) gn = s;
            if (r->s < gn) gn = r->s;
            r->g0 = r->g1;
            r->g1 = gn;
            r->s = s;
            r->peak = 0;
        }
        g = r->g0 + (r->g1 - r->g0) * (j + 1.0) / r->m;
    }
    if (r->on & FX_ON_COMPRESSOR)
        g *= fxr_comp_ramp(r, (n % r->k) / r->m, n % r->m) / 1073741824.0;
    if ((r->on & FX_ON_COMPRESSOR) && r->comp_duck && fabs(duck) > r->comp_peak)
        r->comp_peak = fabs(duck);
    for (c = 0; c < r->ch; c++) {
        double x = (dry[c] + (c == 0 ? rl : c == 1u ? rr : 0.0)) * r->gain;
        if (r->on & FX_ON_DC) {
            double y = x - r->dc_x1[c] + r->dc_r * r->dc_y1[c];
            r->dc_x1[c] = x;
            r->dc_y1[c] = y;
            x = y;
        }
        if (r->on & FX_ON_COMPRESSOR) {
            double *slot = &r->ring[(n % r->k) * r->ch + c], y = *slot;
            if (!r->comp_duck && fabs(x) > r->comp_peak) r->comp_peak = fabs(x);
            *slot = x * r->comp_makeup;
            x = y * g;
        } else if (r->on & FX_ON_LIMITER) {
            double *slot = &r->ring[(n % r->k) * r->ch + c], y = *slot;
            if (fabs(x) > r->peak) r->peak = fabs(x);
            *slot = x;
            x = y * g;
        }
        if (r->on & FX_ON_SOFTCLIP) {
            if (x > 1.0) x = 1.0;
            else if (x < -1.0) x = -1.0;
            out[c] = (1.5 * x - 0.5 * x * x * x) * 32767.0;
        } else {
            out[c] = x * 32768.0;
        }
        if (r->on & FX_ON_DITHER) out[c] += fxr_dither(r);
    }
    r->n = n + 1u;
}

void fxr_master_post(fxr_t *r, double duck, const double *dry, double *out)
{
    fxr_post(r, duck, dry, out, 0, 0);
}

void fxr_step(fxr_t *r, const double *bus, const double *dry, double *out)
{
    double wet[FX_RETURNS][2], rl = 0, rr = 0;
    uint32_t b;
    fxr_field_returns(r, bus, wet);
    for (b = 0; b < FX_RETURNS; b++) {
        rl += wet[b][0] * r->gr;
        rr += wet[b][1] * r->gr;
    }
    fxr_post(r, bus[FX_DUCK], dry, out, rl, rr);
}
