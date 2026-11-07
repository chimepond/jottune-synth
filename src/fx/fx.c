/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * fx.c — the integer effects module; configuration and API contracts: fx.h.
 *
 * At init (control rate): the coefficients from the configuration, by
 * 64/32 divisions and 32x32 products, and the caller memory's layout.
 * Per block boundary: the send targets roll (fx_open). Per half block:
 * the compressor's table-based gain and limiter's safe gain, with unsigned
 * divisions at that boundary only. Per sample and sending voice: a ramped Q1.15
 * gain and a 32x32->64 multiply-accumulate per bus. Per sample: the
 * buses' effects (a chorus tap by linear interpolation, a delay line
 * with a low-passed loop, Freeverb's eight combs and four allpasses per
 * channel), the returns added to the wide sum, then per channel the gain,
 * the DC blocker, the shared dynamics delay and ramped gains, the
 * soft clip, the dither, and the one rounding and saturation to int16
 * (the order).
 */
#include "fx.h"

/* >> on a negative int32 or int64 is floor : the arithmetic here
 * relies on it, as fm_a1.c does, and every int64 shift amount is a
 * constant (: no target imports a 64-bit arithmetic shift). */
typedef char fx_assert_asr[(((int32_t)-1 >> 1) == -1 && ((int64_t)-1 >> 1) == -1) ? 1 : -1];

#define FX_TWO_PI_Q29 3373259426u          /* round(2 pi 2^29) */
#define FX_RV_GAIN    32212255             /* Freeverb's input gain, 0.015 in Q1.31 */
#define FX_GAIN_C     23170                /* isqrt(2^29): space/the centre gain, Q1.15, for the returns in stereo */
#define FX_LIM_MAX    ((((int64_t)1 << 45) - ((int64_t)1 << 29)) - 1)   /* the largest Q.45 value that rounds to 32767 */
#define FX_ON_SENDS   (FX_ON_BUSES | FX_ON_COMPRESSOR)  /* duck has no audible return */

static const uint16_t FX_COMB[8] = { 1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617 };   /* Freeverb at 44.1 kHz */
static const uint16_t FX_ALLP[4] = { 556, 441, 341, 225 };
static const uint32_t FX_DB10[11] = {      /* round(2^31 * 10^(-2^i / 200)): 2^i tenths of a dB */
    2122901606u, 2098600952u, 2050830962u, 1958528364u, 1786199097u, 1485695697u, 1027850297u, 491960082u,
    112701544u, 5914661u, 16290u            /* old ten entries unchanged; compressor also reads bit10 */
};
/* : round_half_up(32768 * log2(1 + i/64)); endpoint64 is 32768.
 * The SPEC states every value and the little-endian uint16 table hash. */
static const uint16_t FX_LOG2[64] = {
    0, 733, 1455, 2166, 2866, 3556, 4236, 4907,
    5568, 6220, 6863, 7498, 8124, 8742, 9352, 9954,
    10549, 11136, 11716, 12289, 12855, 13415, 13968, 14514,
    15055, 15589, 16117, 16639, 17156, 17667, 18173, 18673,
    19168, 19658, 20143, 20623, 21098, 21568, 22034, 22495,
    22952, 23404, 23852, 24296, 24736, 25172, 25604, 26031,
    26455, 26876, 27292, 27705, 28114, 28520, 28922, 29321,
    29717, 30109, 30498, 30884, 31267, 31647, 32024, 32397
};
static const uint32_t FX_INV[8] = { 0u, 0u, 2147483648u, 1431655765u, 1073741824u, 858993459u, 715827883u, 613566757u };   /* round(2^32 / i) */

/* n / d for d >= 1: unsigned 64-bit, the divide helper the targets import
 * ; control rate only. */
static uint64_t fx_udiv(uint64_t n, uint64_t d)
{
    return n / d;
}

static int32_t fx_sat32(int64_t v)
{
    return v > INT32_MAX ? INT32_MAX : v < INT32_MIN ? INT32_MIN : (int32_t)v;
}

/* The smallest power of two at or above v, v >= 1. */
static uint32_t fx_pow2(uint32_t v)
{
    uint32_t p = 1;
    while (p < v) p <<= 1;
    return p;
}

/* round(us * fs / 10^6) samples, and the same in Q16. */
static uint32_t fx_us(uint32_t us, uint32_t fs)
{
    return (uint32_t)fx_udiv((uint64_t)us * fs + 500000u, 1000000u);
}

static uint32_t fx_q16(uint32_t us, uint32_t fs)
{
    return (uint32_t)fx_udiv((((uint64_t)us * fs) << 16) + 500000u, 1000000u);
}

/* Reverb line i (0 to 7 the combs, 8 to 11 the allpasses) of channel s:
 * Freeverb's length, + 23 on the right, scaled by fs / 44100, rounded. */
static uint32_t fx_rv_len(uint32_t i, uint32_t s, uint32_t fs)
{
    uint32_t len44 = (i < 8u ? FX_COMB[i] : FX_ALLP[i - 8u]) + 23u * s;
    return (uint32_t)fx_udiv((uint64_t)len44 * fs + 22050u, 44100u);
}

/* The delay line's length: T, or 2T for the ping-pong. */
static uint32_t fx_dly_len(const fx_cfg_t *c, uint32_t fs)
{
    return fx_us(c->delay_us, fs) << ((c->on & FX_ON_PINGPONG) ? 1 : 0);
}

static uint32_t fx_cho_size(const fx_cfg_t *c, uint32_t fs)
{
    return fx_pow2(((fx_q16(c->chorus_base_us, fs) + fx_q16(c->chorus_depth_us, fs)) >> 16) + 2u);
}

static uint32_t fx_flg_size(const fx_cfg_t *c, uint32_t fs)
{
    return fx_pow2(((fx_q16(c->flanger_base_us, fs) + fx_q16(c->flanger_depth_us, fs)) >> 16) + 2u);
}

/* round(2^31 (1 - exp(-2 pi fc / fs))) within 1 to 2^31 - 1: the matched
 * pole of a one-pole low-pass. exp(-x) by halving x below 1/16, seven
 * terms of its series in Q32, and squaring back in Q1.31. */
static uint32_t fx_onepole(uint32_t fc_hz, uint32_t fs)
{
    uint64_t x = fx_udiv((uint64_t)FX_TWO_PI_Q29 * fc_hz * 8u + (fs >> 1), fs);   /* Q32 */
    uint64_t term = (uint64_t)1 << 32, e = (uint64_t)1 << 32;
    uint32_t k = 0, i, y;
    while (x >= ((uint64_t)1 << 28)) { x = (x + 1u) >> 1; k++; }
    y = (uint32_t)x;
    for (i = 1; i < 8u; i++) {
        term = (term * y) >> 32;                      /* y^i / i!, Q32 */
        if (i > 1u) term = (term * FX_INV[i]) >> 32;
        e = (i & 1u) ? e - term : e + term;
    }
    e = (e + 1u) >> 1;                                /* Q1.31 */
    while (k--) e = (e * e + (1u << 30)) >> 31;
    e = ((uint64_t)1 << 31) - e;
    return e < 1u ? 1u : e > 0x7FFFFFFFu ? 0x7FFFFFFFu : (uint32_t)e;
}

static int fx_valid(const fx_cfg_t *c, uint32_t fs, uint32_t field_mode)
{
    uint32_t on = c->on;
    if (fs < 8000u || fs > 192000u || (on & ~FX_ON_ALL)) return 0;
    if (c->channels != 1u && c->channels != 2u && c->channels != 4u && c->channels != 16u) return 0;
    if (c->block != 32u && c->block != 64u && c->block != 128u) return 0;
    if (field_mode && c->channels == 1u) return 0;
    if (!field_mode && (on & FX_ON_BUSES) && c->channels > 2u) return 0;
    if (field_mode && c->channels > 2u && (on & (FX_ON_COMPRESSOR | FX_ON_LIMITER | FX_ON_SOFTCLIP | FX_ON_DITHER))) return 0;
    if ((on & FX_ON_DC) && (c->dc_millihz < 1u || c->dc_millihz > 100000u)) return 0;
    if ((on & FX_ON_CHORUS) && (c->chorus_base_us < 7000u || c->chorus_base_us > 25000u
        || c->chorus_depth_us < 1000u || c->chorus_depth_us > 5000u
        || c->chorus_rate_millihz < 100u || c->chorus_rate_millihz > 3000u)) return 0;
    if ((on & FX_ON_FLANGER) && (c->flanger_base_us < 100u || c->flanger_base_us > 10000u
        || c->flanger_depth_us < 100u || c->flanger_depth_us > c->flanger_base_us
        || c->flanger_rate_millihz > 2000u || c->flanger_fb_q15 > 29491u
        || c->flanger_invert > 1u)) return 0;
    if ((on & FX_ON_DELAY) && (c->delay_us < 50000u || c->delay_us > 1000000u
        || c->delay_fb_q15 > 29491u || c->delay_lp_hz < 20u || c->delay_lp_hz > 20000u)) return 0;
    if ((on & FX_ON_REVERB) && (c->reverb_room > 1000u || c->reverb_damp > 1000u)) return 0;
    if ((on & FX_ON_LIMITER) && (c->limiter_db10 > 200u || c->limiter_release_ms < 1u || c->limiter_release_ms > 5000u)) return 0;
    if ((on & FX_ON_GAIN) && (c->gain_db10 < -600 || c->gain_db10 > 240)) return 0;
    if ((on & FX_ON_DITHER) && c->dither_seed == 0u) return 0;      /* xorshift32's fixed point */
    if ((on & FX_ON_PINGPONG) && (!(on & FX_ON_DELAY) || (!field_mode && c->channels != 2u) || c->delay_hp_hz > 2000u)) return 0;
    if ((on & FX_ON_COMPRESSOR) && (c->compressor_db10 > 600u
        || c->compressor_ratio_tenths < 10u || c->compressor_ratio_tenths > 200u
        || c->compressor_attack_ms > 100u || c->compressor_release_ms < 1u
        || c->compressor_release_ms > 5000u || c->compressor_makeup_db10 > 240u)) return 0;
    return 1;
}

static size_t fx_mem_bytes_mode(const fx_cfg_t *cfg, uint32_t fs, uint32_t field_mode)
{
    size_t b = 0;
    uint32_t s, i;
    if (!fx_valid(cfg, fs, field_mode)) return 0;
    if (cfg->on & (FX_ON_LIMITER | FX_ON_COMPRESSOR)) b += (size_t)cfg->channels * cfg->block * 8u;
    if (cfg->on & FX_ON_CHORUS)  b += (size_t)fx_cho_size(cfg, fs) * 4u;
    if (cfg->on & FX_ON_FLANGER) b += (size_t)fx_flg_size(cfg, fs) * 4u;
    if (cfg->on & FX_ON_DELAY)   b += (size_t)fx_pow2(fx_dly_len(cfg, fs)) * 4u;
    if (cfg->on & FX_ON_REVERB)
        for (s = 0; s < (field_mode ? 2u : cfg->channels); s++)
            for (i = 0; i < 12u; i++) b += (size_t)fx_pow2(fx_rv_len(i, s, fs)) * 4u;
    return b;
}

size_t fx_mem_bytes(const fx_cfg_t *cfg, uint32_t fs)
{
    return fx_mem_bytes_mode(cfg, fs, 0);
}

size_t fx_field_mem_bytes(const fx_cfg_t *cfg, uint32_t fs)
{
    return fx_mem_bytes_mode(cfg, fs, 1);
}

/* A line of len samples at p: its power-of-two buffer, and the next free byte. */
static unsigned char *fx_line_at(fx_line_t *l, uint32_t len, unsigned char *p)
{
    uint32_t size = fx_pow2(len);
    l->buf  = (int32_t *)(void *)p;
    l->mask = size - 1u;
    l->len  = len;
    return p + (size_t)size * 4u;
}

/* Zeroes bytes at p, one character at a time: a character type may alias
 * any object, the state's int64 and pointer members too. The stores are
 * volatile so that no compiler makes the loop a memset or bzero call,
 * which the lists do not hold on every target; init only. */
static void fx_clear(void *p, size_t bytes)
{
    volatile unsigned char *w = (volatile unsigned char *)p;
    size_t i;
    for (i = 0; i < bytes; i++) w[i] = 0;
}

/* The compressor and limiter share this caller-owned ring and grid. */
static unsigned char *fx_ring_at(fx_t *fx, unsigned char *p)
{
    uint32_t m = fx->k >> 1;
    fx->lim_mmask = m - 1u;
    fx->lim_mul = m == 16u ? 4u : m == 32u ? 2u : 1u;
    fx->lim_ring = (int64_t *)(void *)p;
    return p + (size_t)fx->ch * fx->k * 8u;
}

/* : half-block convergence coefficient. Nonzero time has at least
 * eight samples at the minimum accepted sample rate. Init only. */
static uint32_t fx_comp_time(uint32_t ms, uint32_t fs, uint32_t m)
{
    uint64_t samples, c;
    if (!ms) return 1u << 30;
    samples = fx_udiv((uint64_t)ms * fs + 500u, 1000u);
    c = fx_udiv((uint64_t)m << 30, samples);
    return c > (1u << 30) ? 1u << 30 : (uint32_t)c;
}

static int fx_init_mode(fx_t *fx, uint32_t fs, const fx_cfg_t *cfg, void *mem, size_t bytes, uint32_t field_mode)
{
    unsigned char *p = (unsigned char *)mem;
    size_t need;
    uint32_t on = cfg->on, s, j;
    if (!fx_valid(cfg, fs, field_mode)) return -1;
    need = fx_mem_bytes_mode(cfg, fs, field_mode);
    if (need && (!mem || bytes < need || ((uintptr_t)mem & 7u))) return -1;
    fx_clear(fx, sizeof *fx);
    if (need) fx_clear(mem, need);
    fx->on    = on;
    fx->fs    = fs;
    fx->ch    = cfg->channels;
    fx->field_mode = field_mode;
    fx->bus_ch = field_mode ? 2u : cfg->channels;
    fx->k     = cfg->block;
    fx->log2k = cfg->block == 32u ? 5u : cfg->block == 64u ? 6u : 7u;
    fx->lim_g0 = fx->lim_g1 = fx->lim_s = 1u << 30;
    for (j = 0; j < FX_MAX_VOICES; j++) {
        fx->crush[j].bits = fx->crush[j].pending_bits = 24u;
        fx->crush[j].div = fx->crush[j].pending_div = 1u;
    }
    if (on & FX_ON_DC) {                                         /* : R = max(0, 1 - 2 pi fc / fs), Q1.31 */
        uint64_t w = fx_udiv((uint64_t)FX_TWO_PI_Q29 * cfg->dc_millihz * 4u + 500u * (uint64_t)fs, 1000u * (uint64_t)fs);
        fx->dc_r = w < ((uint64_t)1 << 31) ? (uint32_t)(((uint64_t)1 << 31) - w) : 0u;
    }
    if (on & FX_ON_LIMITER) {
        uint32_t m = cfg->block >> 1;
        uint64_t r = (uint64_t)1 << 31, rel, c;
        for (j = 0; j < 8u; j++)
            if (cfg->limiter_db10 & (1u << j)) r = (r * FX_DB10[j] + (1u << 30)) >> 31;
        fx->lim_thr = r << 14;                                   /* Q1.31 to Q.45 */
        if (fx->lim_thr > (uint64_t)FX_LIM_MAX) fx->lim_thr = (uint64_t)FX_LIM_MAX;
        rel = fx_udiv((uint64_t)cfg->limiter_release_ms * fs + 500u, 1000u);   /* the release's time constant, >= 8 samples */
        c   = fx_udiv((uint64_t)m << 30, rel);
        fx->lim_c     = c > (1u << 30) ? 1u << 30 : (uint32_t)c;
    }
    if (on & (FX_ON_LIMITER | FX_ON_COMPRESSOR)) p = fx_ring_at(fx, p);
    if (on & FX_ON_COMPRESSOR) {
        uint32_t r = 1u << 31;
        fx->comp_db10 = cfg->compressor_db10;
        fx->comp_ratio = cfg->compressor_ratio_tenths;
        fx->comp_attack = fx_comp_time(cfg->compressor_attack_ms, fs, cfg->block >> 1);
        fx->comp_release = fx_comp_time(cfg->compressor_release_ms, fs, cfg->block >> 1);
        fx->comp_g = fx->comp_g0[0] = fx->comp_g1[0] = fx->comp_g0[1] = fx->comp_g1[1] = 1u << 30;
        for (j = 0; j < 8u; j++)
            if (cfg->compressor_makeup_db10 & (1u << j))
                r = (uint32_t)(((uint64_t)r * FX_DB10[j] + (1u << 30)) >> 31);
        fx->comp_makeup = (uint32_t)fx_udiv(((uint64_t)1 << 59) + (r >> 1), r);
    }
    if (on & FX_ON_GAIN) {                                       /* : m = 10^(g / 200), Q4.28 */
        uint32_t a = cfg->gain_db10 < 0 ? 0u - (uint32_t)cfg->gain_db10 : (uint32_t)cfg->gain_db10;
        uint64_t r = (uint64_t)1 << 31;                          /* 10^(-a / 200), Q1.31 */
        for (j = 0; j < 10u; j++)
            if (a & (1u << j)) r = (r * FX_DB10[j] + (1u << 30)) >> 31;
        fx->gain_m = cfg->gain_db10 <= 0 ? (uint32_t)((r + 4u) >> 3)                         /* 0 gives 2^28 */
                   : (uint32_t)fx_udiv(((uint64_t)1 << 59) + (r >> 1), r);                   /* < 2^32 at 240 */
    }
    if (on & FX_ON_DITHER) fx->dith = cfg->dither_seed;
    if (on & FX_ON_CHORUS) {
        fx->cho_base  = fx_q16(cfg->chorus_base_us, fs);
        fx->cho_depth = fx_q16(cfg->chorus_depth_us, fs);
        fx->cho_inc   = (uint32_t)fx_udiv(((uint64_t)cfg->chorus_rate_millihz << 32) + 500u * (uint64_t)fs, 1000u * (uint64_t)fs);
        fx->cho_buf   = (int32_t *)(void *)p;
        fx->cho_mask  = fx_cho_size(cfg, fs) - 1u;
        p += (size_t)(fx->cho_mask + 1u) * 4u;
    }
    if (on & FX_ON_FLANGER) {
        fx->flg_base = fx_q16(cfg->flanger_base_us, fs);
        fx->flg_depth = fx_q16(cfg->flanger_depth_us, fs);
        fx->flg_inc = (uint32_t)fx_udiv(((uint64_t)cfg->flanger_rate_millihz << 32) + 500u * (uint64_t)fs, 1000u * (uint64_t)fs);
        fx->flg_fb = cfg->flanger_fb_q15;
        fx->flg_invert = cfg->flanger_invert;
        fx->flg_buf = (int32_t *)(void *)p;
        fx->flg_mask = fx_flg_size(cfg, fs) - 1u;
        p += (size_t)(fx->flg_mask + 1u) * 4u;
    }
    if (on & FX_ON_DELAY) {
        p = fx_line_at(&fx->dly, fx_dly_len(cfg, fs), p);
        fx->dly_a  = fx_onepole(cfg->delay_lp_hz, fs);
        fx->dly_fb = cfg->delay_fb_q15;
        fx->dly_t  = fx_us(cfg->delay_us, fs);
        fx->dly_ha = (on & FX_ON_PINGPONG) && cfg->delay_hp_hz ? fx_onepole(cfg->delay_hp_hz, fs) : 0u;
    }
    if (on & FX_ON_REVERB) {                                     /* Freeverb: roomsize 0.7 + 0.28 room, damp 0.4 damp */
        fx->rv_fb   = (uint32_t)fx_udiv(((uint64_t)(70000u + 28u * cfg->reverb_room) << 31) + 50000u, 100000u);
        fx->rv_damp = (uint32_t)fx_udiv(((uint64_t)(4u * cfg->reverb_damp) << 31) + 5000u, 10000u);
        for (s = 0; s < fx->bus_ch; s++) {
            for (j = 0; j < 8u; j++) p = fx_line_at(&fx->comb[s][j], fx_rv_len(j, s, fs), p);
            for (j = 0; j < 4u; j++) p = fx_line_at(&fx->allp[s][j], fx_rv_len(8u + j, s, fs), p);
        }
    }
    return 0;
}

int fx_init(fx_t *fx, uint32_t fs, const fx_cfg_t *cfg, void *mem, size_t bytes)
{
    return fx_init_mode(fx, fs, cfg, mem, bytes, 0);
}

int fx_field_init(fx_t *fx, uint32_t fs, const fx_cfg_t *cfg, void *mem, size_t bytes)
{
    return fx_init_mode(fx, fs, cfg, mem, bytes, 1);
}

void fx_set_send(fx_t *fx, uint32_t voice, uint32_t bus, uint32_t level_q15)
{
    if (voice >= FX_MAX_VOICES || bus >= FX_BUSES) return;
    fx->send[voice][bus].level = (uint16_t)(level_q15 > 32768u ? 32768u : level_q15);
}

void fx_set_crush(fx_t *fx, uint32_t voice, uint32_t bits, uint32_t div)
{
    if (voice >= FX_MAX_VOICES) return;
    fx->crush[voice].pending_bits = (uint8_t)(bits < 1u ? 1u : bits > 24u ? 24u : bits);
    fx->crush[voice].pending_div = (uint8_t)(div < 1u ? 1u : div > 64u ? 64u : div);
}

/* The start of a block piece: at a block boundary the targets roll (the
 * last targets become the previous gains, the levels the new targets; at
 * the counter's 0 the previous gains are the targets, no fade-in, as in
 * space/). */
static void fx_open(fx_t *fx)
{
    if ((fx->on & FX_ON_SENDS) && (fx->n & (fx->k - 1u)) == 0) {
        fx_send_t *s = &fx->send[0][0];
        uint32_t i;
        for (i = 0; i < FX_MAX_VOICES * FX_BUSES; i++, s++) {
            s->gp = fx->n == 0 ? s->level : s->gt;
            s->gt = s->level;
        }
        if (fx->on & FX_ON_COMPRESSOR) {
            fx->comp_duck = 0;
            for (i = 0; i < FX_MAX_VOICES; i++)
                if (fx->send[i][FX_DUCK].gp || fx->send[i][FX_DUCK].gt) fx->comp_duck = 1;
        }
    }
    if ((fx->on & FX_ON_CRUSH) && (fx->n & (fx->k - 1u)) == 0) {
        uint32_t i;
        for (i = 0; i < FX_MAX_VOICES; i++) {
            fx_crush_t *c = &fx->crush[i];
            if (c->bits != c->pending_bits || c->div != c->pending_div) c->left = 0;
            c->bits = c->pending_bits;
            c->div = c->pending_div;
        }
    }
    fx->open = 1;
    fx->piece_len = 0;
}

void fx_voice_sends(fx_t *fx, uint32_t voice, const int32_t *buf, uint32_t len)
{
    uint32_t b, t, j0 = fx->n & (fx->k - 1u);
    if (!fx->open) fx_open(fx);
    if (fx->field_mode && !fx->piece_len) fx->piece_len = len;
    if (voice >= FX_MAX_VOICES) return;
    for (b = 0; b < FX_BUSES; b++) {
        int32_t gp = fx->send[voice][b].gp, d = (int32_t)fx->send[voice][b].gt - gp;
        int32_t base = gp * (int32_t)fx->k, r = d * (int32_t)(j0 + 1u);
        int64_t *acc = fx->bus[b];
        if (!(fx->on & FX_BUS_BIT[b]) || (gp == 0 && d == 0)) continue;
        for (t = 0; t < len; t++) {
            acc[t] += (int64_t)buf[t] * ((base + r) >> fx->log2k);  /* the ramp, never negative; exact at the block's end */
            r += d;
        }
    }
}

void fx_voice_process(fx_t *fx, uint32_t voice, int32_t *buf, uint32_t len)
{
    uint32_t t;
    if (!fx->open) fx_open(fx);
    if (fx->field_mode && !fx->piece_len) fx->piece_len = len;
    if (voice >= FX_MAX_VOICES) return;
    if (fx->on & FX_ON_CRUSH) {
        fx_crush_t *c = &fx->crush[voice];
        if (c->bits < 24u || c->div > 1u) {
            uint32_t mask = ~((1u << (30u - c->bits)) - 1u);
            for (t = 0; t < len; t++) {
                if (c->left == 0) {
                    c->held = (int32_t)((uint32_t)buf[t] & mask);
                    c->left = (uint8_t)(c->div - 1u);
                } else c->left--;
                buf[t] = c->held;
            }
        }
    }
    fx_voice_sends(fx, voice, buf, len);
}

/* A bus sample, Q5.27: the Q.45 sum rounded and saturated. Reading it
 * clears it, so the sums start at 0 in the next piece. */
static int32_t fx_bus_pop(fx_t *fx, uint32_t b, uint32_t t)
{
    int64_t v = fx->bus[b][t];
    fx->bus[b][t] = 0;
    return fx_sat32((v + (1 << 17)) >> 18);
}

/* One chorus tap at LFO phase p: the line read d samples back, d = base +
 * depth * tri(p) in Q16 with tri(p) = |p - 2^31| / 2^30 - 1 in Q1.30, by
 * linear interpolation between the two samples around it. */
static int32_t fx_mod_tap(const int32_t *buf, uint32_t mask, uint32_t base, uint32_t depth,
                          uint32_t minimum, uint32_t n, uint32_t p)
{
    uint32_t a  = p ^ 0x80000000u;                               /* p - 2^31 in two's complement */
    uint32_t ua = (a & 0x80000000u) ? 0u - a : a;                /* its magnitude, 0 to 2^31 */
    int32_t  u  = ua >= (1u << 30) ? (int32_t)(ua - (1u << 30)) : -(int32_t)((1u << 30) - ua);
    int64_t  d  = (int64_t)base + (((int64_t)(int32_t)depth * u) >> 30);
    uint32_t di, f;
    int32_t x0, x1;
    if (d < minimum) d = minimum;
    di = (uint32_t)(d >> 16); f = (uint32_t)d & 0xFFFFu;
    x0 = buf[(n - di) & mask]; x1 = buf[(n - di - 1u) & mask];
    return (int32_t)(((int64_t)x0 * (int32_t)(65536u - f) + (int64_t)x1 * (int32_t)f) >> 16);
}

/* Chorus keeps its original arithmetic and write-before-read order. */
static int32_t fx_cho_tap(const fx_t *fx, uint32_t n, uint32_t p)
{
    return fx_mod_tap(fx->cho_buf, fx->cho_mask, fx->cho_base, fx->cho_depth, 0u, n, p);
}

/* The delay line: the sample len back, its low-pass in the loop, the input
 * plus the fed-back low-pass written. Returns the line's output. */
static int32_t fx_dly(fx_t *fx, uint32_t n, int32_t in)
{
    fx_line_t *l = &fx->dly;
    int32_t out = l->buf[(n - l->len) & l->mask], lp = fx->dly_lp, a = (int32_t)fx->dly_a;
    lp = (int32_t)((int64_t)lp + (((int64_t)out * a - (int64_t)lp * a) >> 31));   /* lp += a (out - lp), between them */
    fx->dly_lp = lp;
    l->buf[n & l->mask] = fx_sat32((int64_t)in + (((int64_t)lp * (int32_t)fx->dly_fb) >> 15));
    return out;
}

/* The ping-pong : one line of 2T, the left return the tap at T and
 * the right the tap at 2T; the far tap fed back through the loop's
 * low-pass and a one-pole high-pass h = lp - lp2, lp2 += a_hp (lp - lp2),
 * floored as the low-pass is (a_hp 0: lp2 stays 0 and h = lp). Returns the
 * right tap and writes the left to *left. */
static int32_t fx_pp(fx_t *fx, uint32_t n, int32_t in, int32_t *left)
{
    fx_line_t *l = &fx->dly;
    int32_t out = l->buf[(n - l->len) & l->mask], lp = fx->dly_lp, h = fx->dly_hp;
    int32_t a = (int32_t)fx->dly_a, b = (int32_t)fx->dly_ha;
    *left = l->buf[(n - fx->dly_t) & l->mask];
    lp = (int32_t)((int64_t)lp + (((int64_t)out * a - (int64_t)lp * a) >> 31));
    h  = (int32_t)((int64_t)h + (((int64_t)lp * b - (int64_t)h * b) >> 31));    /* between h and lp */
    fx->dly_lp = lp;
    fx->dly_hp = h;
    l->buf[n & l->mask] = fx_sat32((int64_t)in + ((((int64_t)lp - h) * (int32_t)fx->dly_fb) >> 15));
    return out;
}

/* Freeverb's lowpass-feedback comb: store = out (1 - damp) + store damp.
 * Both products round half up: floored, their bias of half an LSB would
 * reach the output times the loop's DC gain 1 / (1 - fb), up to 50. */
static int32_t fx_comb(fx_line_t *l, uint32_t n, int32_t in, int32_t fb, int32_t damp)
{
    int32_t out = l->buf[(n - l->len) & l->mask];
    l->store = (int32_t)((int64_t)out + (((int64_t)l->store * damp - (int64_t)out * damp + (1 << 30)) >> 31));
    l->buf[n & l->mask] = fx_sat32((int64_t)in + (((int64_t)l->store * fb + (1 << 30)) >> 31));
    return out;
}

/* Freeverb's allpass, feedback 1/2: out = buf - in, buf = in + buf / 2. */
static int32_t fx_allpass(fx_line_t *l, uint32_t n, int32_t in)
{
    int32_t bo = l->buf[(n - l->len) & l->mask];
    l->buf[n & l->mask] = fx_sat32((int64_t)in + (bo >> 1));
    return fx_sat32((int64_t)bo - in);
}

/* One channel of the reverb: the input times 0.015 (rounded, as in the
 * combs), the sum of the eight combs, the four allpasses in series. */
static int32_t fx_reverb(fx_t *fx, uint32_t s, uint32_t n, int32_t in)
{
    int32_t x = (int32_t)(((int64_t)in * FX_RV_GAIN + (1 << 30)) >> 31);
    int64_t sum = 0;
    uint32_t i;
    for (i = 0; i < 8u; i++) sum += fx_comb(&fx->comb[s][i], n, x, (int32_t)fx->rv_fb, (int32_t)fx->rv_damp);
    x = fx_sat32(sum);
    for (i = 0; i < 4u; i++) x = fx_allpass(&fx->allp[s][i], n, x);
    return x;
}

/* : y = x - x1 + floor(R y1 / 2^31) at Q.45, R y1 by two 32x32
 * products on y1's halves. */
static int64_t fx_dc(fx_t *fx, uint32_t c, int64_t x)
{
    int64_t y1 = fx->dc_y1[c], y;
    int32_t r  = (int32_t)fx->dc_r;
    y = x - fx->dc_x1[c] + (int64_t)(int32_t)(y1 >> 32) * r * 2 + (int64_t)(((uint64_t)(uint32_t)y1 * (uint32_t)r) >> 31);
    fx->dc_x1[c] = x;
    fx->dc_y1[c] = y;
    return y;
}

/* Shared dynamics primitives: exact Q.45 peak and one-block ring exchange.
 * No gain or detector policy lives in these helpers. */
static void fx_peak_add(uint64_t *peak, int64_t x)
{
    uint64_t a = x < 0 ? 0u - (uint64_t)x : (uint64_t)x;
    if (a > *peak) *peak = a;
}

static int64_t fx_ring_sample(fx_t *fx, uint32_t n, uint32_t c, int64_t x)
{
    int64_t *slot = &fx->lim_ring[(n & (fx->k - 1u)) * fx->ch + c];
    int64_t y = *slot;
    *slot = x;
    return y;
}

/* The safe gain for a half block's peak, Q1.30: 1 at or below the
 * threshold, else thr / peak floored, both scaled below 2^31 first (the
 * peak rounded up, the threshold down) for the one 64/32 division. */
static uint32_t fx_lim_safe(uint64_t thr, uint64_t peak)
{
    if (peak <= thr) return 1u << 30;
    while (peak >= ((uint64_t)1 << 31)) { peak = (peak + 1u) >> 1; thr >>= 1; }
    return (uint32_t)fx_udiv(thr << 30, peak);
}

/* A half-block boundary: the gain at the end of this half block is the
 * least of the two half blocks' safe gains its ramps reach and one
 * release step up from the last end, G + ceil((1 - G) c), which lands on
 * 1 exactly. Both ramp endpoints are bounded by the half block's safe
 * gain, so interpolation cannot exceed it. This bound is before softclip,
 * dither and final PCM rounding. */
static void fx_lim_boundary(fx_t *fx)
{
    uint32_t s = fx_lim_safe(fx->lim_thr, fx->lim_peak), g1 = fx->lim_g1;
    uint32_t g = g1 + (uint32_t)(((uint64_t)((1u << 30) - g1) * fx->lim_c + ((1u << 30) - 1u)) >> 30);
    if (s < g) g = s;
    if (fx->lim_s < g) g = fx->lim_s;
    fx->lim_g0   = g1;
    fx->lim_g1   = g;
    fx->lim_s    = s;
    fx->lim_peak = 0;
}

/* floor(x g / 2^30) for g <= 2^30 and |x| < 2^62, on x's halves.
 * Every intermediate fits int64. */
static int64_t fx_gain30(int64_t x, uint32_t g)
{
    return (int64_t)(int32_t)(x >> 32) * (int32_t)g * 4 + (int64_t)(((uint64_t)(uint32_t)x * g) >> 30);
}

/* floor(x m / 2^28) for m < 2^32 and |x| < 2^58, on x's halves.
 * Master gain uses |x| < 2^53; compressor makeup uses the wider bound.
 * Do not apply this to the post-makeup |z| < 2^62 signal. */
static int64_t fx_gain28(int64_t x, uint32_t m)
{
    int32_t hi = (int32_t)(x >> 32);
    int64_t r = (int64_t)hi * (int32_t)(m & 0x7FFFFFFFu) * 16
              + (int64_t)(((uint64_t)(uint32_t)x * m) >> 28);
    if (m & 0x80000000u) r += (int64_t)hi * ((int64_t)1 << 35);
    return r;
}

/* Signed integer dB10 of a nonzero Q.45 peak. Normalize to bit31 using
 * constant one-bit shifts; interpolate six index bits plus 25 fraction
 * bits. All wide shifts are constant, including negative rounding. */
static int32_t fx_comp_level(uint64_t peak)
{
    int32_t exponent = 31, log2q15;
    uint32_t norm, i, f, lo, hi;
    while (peak >= ((uint64_t)1 << 32)) { peak >>= 1; exponent++; }
    while (peak < ((uint64_t)1 << 31)) { peak <<= 1; exponent--; }
    norm = (uint32_t)peak;
    i = (norm >> 25) & 63u;
    f = norm & ((1u << 25) - 1u);
    lo = FX_LOG2[i];
    hi = i == 63u ? 32768u : FX_LOG2[i + 1u];
    log2q15 = (exponent - 45) * 32768 + (int32_t)(lo
        + (uint32_t)(((uint64_t)(hi - lo) * f + (1u << 24)) >> 25));
    return (int32_t)(((int64_t)log2q15 * 3945660 + ((int64_t)1 << 30)) >> 31);
}

static uint32_t fx_comp_target(const fx_t *fx)
{
    int32_t over;
    uint32_t reduction, r = 1u << 31, i;
    if (!fx->comp_peak) return 1u << 30;
    over = fx_comp_level(fx->comp_peak) + (int32_t)fx->comp_db10;
    if (over <= 0) return 1u << 30;
    reduction = (uint32_t)fx_udiv((uint64_t)(uint32_t)over * (fx->comp_ratio - 10u), fx->comp_ratio);
    for (i = 0; i < 11u; i++)
        if (reduction & (1u << i)) r = (uint32_t)(((uint64_t)r * FX_DB10[i] + (1u << 30)) >> 31);
    return (r + 1u) >> 1;
}

static uint32_t fx_comp_ramp(const fx_t *fx, uint32_t slot, uint32_t j)
{
    return (uint32_t)((int64_t)fx->comp_g0[slot]
        + (((int64_t)((int32_t)fx->comp_g1[slot] - (int32_t)fx->comp_g0[slot])
            * (int32_t)(fx->lim_mul * (j + 1u))) >> 6));
}

/* Exact ceil(u*g/2^30), u < 2^62 and g <= 2^30. Both products use
 * 32-bit halves; the low product plus rounding is strictly below 2^62. */
static uint64_t fx_comp_peak(uint64_t u, uint32_t g)
{
    return (uint64_t)(uint32_t)(u >> 32) * g * 4u
        + (((uint64_t)(uint32_t)u * g + ((1u << 30) - 1u)) >> 30);
}

/* Fix the just-completed input half's ramp, to play one half later.
 * The parity opposite the current output half is safe to overwrite.
 * Only the combined compressor/limiter path scans that raw ring half. */
static void fx_comp_boundary(fx_t *fx, uint32_t n)
{
    uint32_t target = fx_comp_target(fx), old = fx->comp_g;
    uint32_t falling = target < old, delta = falling ? old - target : target - old;
    uint32_t coefficient = falling ? fx->comp_attack : fx->comp_release;
    uint32_t step = (uint32_t)(((uint64_t)delta * coefficient + ((1u << 30) - 1u)) >> 30);
    uint32_t base = (n - (fx->k >> 1)) & (fx->k - 1u);
    uint32_t slot = base > fx->lim_mmask ? 1u : 0u;
    fx->comp_g = falling ? old - step : old + step;
    fx->comp_g0[slot] = old;
    fx->comp_g1[slot] = fx->comp_g;
    fx->comp_peak = 0;
    if (fx->on & FX_ON_LIMITER) {
        uint32_t j, c;
        fx->lim_peak = 0;
        for (j = 0; j <= fx->lim_mmask; j++) {
            uint32_t g = fx_comp_ramp(fx, slot, j);
            for (c = 0; c < fx->ch; c++) {
                int64_t x = fx->lim_ring[(base + j) * fx->ch + c];
                uint64_t u = x < 0 ? 0u - (uint64_t)x : (uint64_t)x;
                uint64_t bound = fx_comp_peak(u, g);
                if (bound > fx->lim_peak) fx->lim_peak = bound;
            }
        }
    }
}

/* The dither : two steps of the xorshift32, each giving a draw
 * uniform on [-2^29, 2^29 - 1], half an output LSB either way; their sum
 * is triangular over +-1 LSB in the rounding's units (an LSB is 2^30). */
static int64_t fx_dither(fx_t *fx)
{
    uint32_t w = fx->dith;
    int32_t  a, b;
    w ^= w << 13; w ^= w >> 17; w ^= w << 5;
    a = (int32_t)(w >> 2) - (1 << 29);
    w ^= w << 13; w ^= w >> 17; w ^= w << 5;
    b = (int32_t)(w >> 2) - (1 << 29);
    fx->dith = w;
    return (int64_t)a + b;
}

/* The cubic soft clip 1.5 x - 0.5 x^3 on x clamped to +-1, its +-1 mapped
 * to +-32767: the rounding's argument, in which one output LSB is 2^30 as
 * in Q.45. Rounded half up without the dither it never reaches the rails'
 * clip; with it the final rounded value can exceed the top rail and is
 * counted by fx_post_sample's saturation step. */
static int64_t fx_softclip(int64_t x)
{
    const int64_t one = (int64_t)1 << 45;
    int32_t x30, x2, x3, y;
    if (x > one) x = one;
    else if (x < -one) x = -one;
    x30 = (int32_t)(x >> 15);                                    /* |x30| <= 2^30 */
    x2  = (int32_t)(((int64_t)x30 * x30) >> 30);
    x3  = (int32_t)(((int64_t)x2 * x30) >> 30);
    y   = x30 + (x30 >> 1) - (x3 >> 1);                          /* Q2.30, within +-(2^30 + 2) */
    return (int64_t)y * 32767;
}

/* One Q5.27 stereo pair, returned by value instead of through a caller aggregate.
 * Callers consume chorus, flanger, delay and reverb in that order, once each.
 * The duck sum is left for post. No network or master arithmetic changes. */
typedef struct { int32_t l, r; } fx_return_t;
static fx_return_t fx_return_sample(fx_t *fx, uint32_t n, uint32_t t, uint32_t bus)
{
    uint32_t on = fx->on, ch = fx->bus_ch;
    fx_return_t wet = {0, 0};
    if (bus == FX_RETURN_CHORUS && (on & FX_ON_CHORUS)) {
        fx->cho_buf[n & fx->cho_mask] = fx_bus_pop(fx, FX_CHORUS, t);
        wet.l = fx_cho_tap(fx, n, fx->cho_phase);
        if (ch == 2u) wet.r = fx_cho_tap(fx, n, fx->cho_phase + 0x40000000u);
        fx->cho_phase += fx->cho_inc;
    }
    if (bus == FX_RETURN_FLANGER && (on & FX_ON_FLANGER)) {
        int32_t in = fx_bus_pop(fx, FX_FLANGER, t);
        int32_t lt = fx_mod_tap(fx->flg_buf, fx->flg_mask, fx->flg_base, fx->flg_depth,
                                65536u, n, fx->flg_phase);
        int32_t rt = ch == 2u ? fx_mod_tap(fx->flg_buf, fx->flg_mask, fx->flg_base, fx->flg_depth,
                                          65536u, n, fx->flg_phase + 0x40000000u) : 0;
        int32_t feedback = (int32_t)(((int64_t)lt * (int32_t)fx->flg_fb) >> 15);
        if (fx->flg_invert) feedback = -feedback;
        fx->flg_buf[n & fx->flg_mask] = fx_sat32((int64_t)in + feedback);
        wet.l = lt;
        wet.r = rt;
        fx->flg_phase += fx->flg_inc;
    }
    if (bus == FX_RETURN_DELAY && (on & FX_ON_DELAY)) {
        int32_t in = fx_bus_pop(fx, FX_DELAY, t), lt;
        if (on & FX_ON_PINGPONG) {
            wet.r = fx_pp(fx, n, in, &lt);
            wet.l = lt;
        } else {
            int32_t d = fx_dly(fx, n, in);
            wet.l = wet.r = d;
        }
    }
    if (bus == FX_RETURN_REVERB && (on & FX_ON_REVERB)) {
        int32_t in = fx_bus_pop(fx, FX_REVERB, t);
        wet.l = fx_reverb(fx, 0, n, in);
        if (ch == 2u) wet.r = fx_reverb(fx, 1, n, in);
    }
    return wet;
}

/* Existing post-return chain, one sample. Legacy input is below 2^53 Q45;
 * raw field mode admits below 2^55 only with gain/DC (no nonlinear master).
 * That wider linear bound and its <2^62 DC intermediates are the proof. */
static uint32_t fx_post_sample(fx_t *fx, const int64_t *acc, uint32_t stride, uint32_t frac,
                              int16_t *out, uint32_t t, int64_t rl, int64_t rr)
{
    uint32_t n = fx->n + t, g = 1u << 30, on = fx->on, ch = fx->ch, c, clipped = 0;
    if (on & (FX_ON_LIMITER | FX_ON_COMPRESSOR)) {
        uint32_t j = n & fx->lim_mmask;
        if (j == 0 && (on & FX_ON_COMPRESSOR)) fx_comp_boundary(fx, n);
        if (on & FX_ON_LIMITER) {
            if (j == 0) fx_lim_boundary(fx);
            g = (uint32_t)((int64_t)fx->lim_g0
                + (((int64_t)((int32_t)fx->lim_g1 - (int32_t)fx->lim_g0) * (int32_t)(fx->lim_mul * (j + 1u))) >> 6));
        }
        if (on & FX_ON_COMPRESSOR) {
            uint32_t slot = (n & (fx->k - 1u)) > fx->lim_mmask ? 1u : 0u;
            g = (uint32_t)(((uint64_t)g * fx_comp_ramp(fx, slot, j)) >> 30);
        }
    }
    if (on & FX_ON_COMPRESSOR) {
        if (fx->comp_duck) fx_peak_add(&fx->comp_peak, fx->bus[FX_DUCK][t]);
        fx->bus[FX_DUCK][t] = 0;                             /* a detector sum, no return or Q27 conversion */
    }
    for (c = 0; c < ch; c++) {
        int64_t x = acc ? acc[c * stride + t] : 0, s;
        if (frac == 30u) x *= (int64_t)1 << 15;              /* Q2.30 to Q.45: a shift, |x| < 2^38 */
        x += c == 0 ? rl : c == 1u ? rr : 0;
        if (on & FX_ON_GAIN) x = fx_gain28(x, fx->gain_m);
        if (on & FX_ON_DC) x = fx_dc(fx, c, x);
        if (on & FX_ON_COMPRESSOR) {
            int64_t y;
            if (!fx->comp_duck) fx_peak_add(&fx->comp_peak, x);
            y = fx_ring_sample(fx, n, c, fx_gain28(x, fx->comp_makeup));
            x = fx_gain30(y, g);
        } else if (on & FX_ON_LIMITER) {
            int64_t y = fx_ring_sample(fx, n, c, x);
            fx_peak_add(&fx->lim_peak, x);
            x = fx_gain30(y, g);
        }
        s = (on & FX_ON_SOFTCLIP) ? fx_softclip(x) : x;     /* the rounding's argument: an output LSB is 2^30 */
        if (on & FX_ON_DITHER) s += fx_dither(fx);
        s = (s + ((int64_t)1 << 29)) >> 30;                   /* Q.45 to Q15, round half up */
        if (s > 32767) { s = 32767; clipped++; }
        else if (s < -32768) { s = -32768; clipped++; }
        out[t * ch + c] = (int16_t)s;
    }
    return clipped;
}

uint32_t fx_master(fx_t *fx, const int64_t *acc, uint32_t stride, uint32_t frac, int16_t *out, uint32_t len)
{
    uint32_t t, clipped = 0;
    int32_t gr = (fx->ch == 1u ? 32768 : FX_GAIN_C) * 8;
    if (!fx->open) fx_open(fx);
    for (t = 0; t < len; t++) {
        fx_return_t chorus = fx_return_sample(fx, fx->n + t, t, FX_RETURN_CHORUS);
        fx_return_t flanger = fx_return_sample(fx, fx->n + t, t, FX_RETURN_FLANGER);
        fx_return_t delay = fx_return_sample(fx, fx->n + t, t, FX_RETURN_DELAY);
        fx_return_t reverb = fx_return_sample(fx, fx->n + t, t, FX_RETURN_REVERB);
        int64_t rl = 0, rr = 0;
        rl += (int64_t)chorus.l * gr; rr += (int64_t)chorus.r * gr;
        rl += (int64_t)flanger.l * gr; rr += (int64_t)flanger.r * gr;
        rl += (int64_t)delay.l * gr; rr += (int64_t)delay.r * gr;
        rl += (int64_t)reverb.l * gr; rr += (int64_t)reverb.r * gr;
        clipped += fx_post_sample(fx, acc, stride, frac, out, t, rl, rr);
    }
    fx->n += len;
    fx->open = fx->piece_len = 0;
    return clipped;
}

void fx_field_returns(fx_t *fx, int32_t *out, uint32_t bus_stride, uint32_t ear_stride, uint32_t len)
{
    uint32_t t;
    if (!fx->open) fx_open(fx);
    if (!fx->piece_len) fx->piece_len = len;
    for (t = 0; t < len; t++) {
        fx_return_t chorus = fx_return_sample(fx, fx->n + t, t, FX_RETURN_CHORUS);
        fx_return_t flanger = fx_return_sample(fx, fx->n + t, t, FX_RETURN_FLANGER);
        fx_return_t delay = fx_return_sample(fx, fx->n + t, t, FX_RETURN_DELAY);
        fx_return_t reverb = fx_return_sample(fx, fx->n + t, t, FX_RETURN_REVERB);
        out[(size_t)FX_RETURN_CHORUS * bus_stride + t] = chorus.l;
        out[(size_t)FX_RETURN_CHORUS * bus_stride + ear_stride + t] = chorus.r;
        out[(size_t)FX_RETURN_FLANGER * bus_stride + t] = flanger.l;
        out[(size_t)FX_RETURN_FLANGER * bus_stride + ear_stride + t] = flanger.r;
        out[(size_t)FX_RETURN_DELAY * bus_stride + t] = delay.l;
        out[(size_t)FX_RETURN_DELAY * bus_stride + ear_stride + t] = delay.r;
        out[(size_t)FX_RETURN_REVERB * bus_stride + t] = reverb.l;
        out[(size_t)FX_RETURN_REVERB * bus_stride + ear_stride + t] = reverb.r;
    }
    fx->open = 2;
}

uint32_t fx_master_post(fx_t *fx, const int64_t *acc_q45, uint32_t stride, int16_t *out, uint32_t len)
{
    uint32_t t, clipped = 0;
    /* Trusted field caller already produced returns for this same piece. */
    for (t = 0; t < len; t++) clipped += fx_post_sample(fx, acc_q45, stride, 45, out, t, 0, 0);
    fx->n += len;
    fx->open = fx->piece_len = 0;
    return clipped;
}

uint32_t fx_render_mono(fx_t *fx, fm_voice_t *voices, uint32_t count, int16_t *out, uint32_t frames)
{
    uint32_t clipped = 0;
    if (count > FX_MAX_VOICES) count = FX_MAX_VOICES;
    while (frames) {
        uint32_t len = fx->k - (fx->n & (fx->k - 1u)), i, t, any = 0;
        if (len > frames) len = frames;
        if (!fx->open) fx_open(fx);
        for (i = 0; i < count; i++) {
            if (!fm_voice_active(&voices[i]) && !fm_voice_needs_clock(&voices[i])) {
                if (!(fx->on & FX_ON_CRUSH)) continue;
                for (t = 0; t < len; t++) fx->vbuf[t] = 0;
            } else fm_render_q30(&voices[i], fx->vbuf, len);
            fx_voice_process(fx, i, fx->vbuf, len);
            if (any) for (t = 0; t < len; t++) fx->dry[t] += fx->vbuf[t];
            else     for (t = 0; t < len; t++) fx->dry[t] = fx->vbuf[t];
            any = 1;
        }
        clipped += fx_master(fx, any ? fx->dry : NULL, 0, 30, out, len);   /* stride 0: the mix in every channel */
        out += (size_t)len * fx->ch;
        frames -= len;
    }
    return clipped;
}
