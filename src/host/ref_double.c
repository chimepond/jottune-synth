/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * ref_double.c — see ref_double.h. Host tool: double and libm allowed.
 */
#include <math.h>
#include <float.h>
#include "ref_double.h"
#include "fm_tables.h"   /* : the LFO's sine is the engine's */

#define REF_MOD_NOTE 4u
#define REF_LFO_HOLD 2u
static void ref_mod_filter(ref_voice_t *v);
static void ref_mod_pitch_apply(ref_voice_t *v);

#define PI 3.14159265358979323846
#define DB10_PER_OCT 60.20599913279623904   /* 10 * 20*log10(2) */

/* Same integer sample-count rule as the engine. */
static uint32_t ref_segment_samples(uint32_t ms, uint32_t fs)
{
    uint64_t n = ((uint64_t)ms * fs + 500u) / 1000u;
    if (n > FM_SEG_MAX) n = FM_SEG_MAX;
    return n ? (uint32_t)n : 1u;
}

/* : an attack from gain g0 keeps the full attack's slope:
 * n - floor(g0 * n) samples, at least 1. */
static uint32_t ref_attack_samples(double g0, uint32_t n)
{
    double k = floor(g0 * (double)n);
    return (k < (double)n) ? n - (uint32_t)k : 1u;
}

static double ref_db10_to_oct(uint32_t db10)
{
    return (db10 > 1200u ? 1200.0 : (double)db10) / DB10_PER_OCT;
}

/* Turns per sample, clamped at Nyquist. */
static double ref_inc(uint32_t freq_millihz, uint32_t ratio_q16, double fs)
{
    double inc;
    if (fs == 0.0) return 0.5;   /* the direct API's saturated increment */
    inc = (double)freq_millihz * (double)ratio_q16 / (1000.0 * fs * 65536.0);
    return inc >= 0.5 ? 0.5 : inc;
}

/* 's count in double: the octaves above A0 (27500 mHz), clamped to
 * 0..8, times rs / 8, and the count n * 2^-that rounded half up, at least 1. */
uint32_t ref_rate_scaled(uint32_t n, uint32_t freq_millihz, uint32_t rs)
{
    double oct, y;
    if (rs > 7u) rs = 7u;
    if (rs == 0u || freq_millihz <= 27500u) return n;
    oct = log2((double)freq_millihz / 27500.0);
    if (oct > 8.0) oct = 8.0;
    y = floor((double)n * exp2(-oct * (double)rs / 8.0) + 0.5);
    return y >= 1.0 ? (uint32_t)y : 1u;
}

/* : bound the engine's approximation before the count is rounded.
 * With h = 2^-FM_EXP_BITS, linear interpolation of 2^-t on [0,1]
 * differs by at most C = (ln 2)^2 h^2 / 8; rounding a table node adds
 * 2^-31. Inverting it costs at most 2 / ln 2 times that gain error.
 * The Q48 ratio truncation, normalized through at most eight octaves,
 * adds 2^-40 gain; the inverse's final truncation adds 2^-48 octaves.
 * Scaling by rs/8 and rounding to Q24 adds 2^-25 octaves. The forward
 * lookup adds C, node rounding 2^-31 and interpolation floor 2^-30;
 * |d(2^-x)/dx| <= ln 2 for x >= 0. Each term is bounded separately,
 * without assuming that the inverse and forward errors cancel.
 * The host allowance is 32 double epsilons per input sample, far below
 * the fixed-point bound. Bypasses and the capped power-of-two factor
 * are exact and keep a zero window. */
void ref_rate_scaled_window(uint32_t n, uint32_t freq_millihz, uint32_t rs,
                            double *exact, double *tau)
{
    const double ln2 = 0.69314718055994530942;
    const double h = ldexp(1.0, -FM_EXP_BITS);
    const double curve = ln2 * ln2 * h * h / 8.0;
    double oct, inverse_error, exponent_error, factor_error;
    if (rs > 7u) rs = 7u;
    *exact = (double)n;
    *tau = 0.0;
    if (rs == 0u || freq_millihz <= 27500u) return;
    if (freq_millihz >= 7040000u) {
        *exact = ldexp((double)n, -(int)rs);
        return;
    }
    oct = log2((double)freq_millihz / 27500.0);
    *exact = (double)n * exp2(-oct * (double)rs / 8.0);
    inverse_error = 2.0 * (curve + ldexp(1.0, -31) + ldexp(1.0, -40)) / ln2
                  + ldexp(1.0, -48);
    exponent_error = (double)rs * inverse_error / 8.0 + ldexp(1.0, -25);
    factor_error = ln2 * exponent_error + curve + ldexp(1.0, -31) + ldexp(1.0, -30);
    *tau = (double)n * (factor_error + 32.0 * DBL_EPSILON);
}

/* Called immediately after note-on, before  checks the shortened
 * attack. The caller supplies the engine count; this function derives its
 * own ideal interval and changes the reference only after validation. */
int ref_adopt_rate_scaled(ref_voice_t *v, unsigned op, unsigned segment,
                          uint32_t count, double *exact, double *tau)
{
    const fm_op_patch_t *pp;
    ref_op_t *o;
    uint32_t ms, n, ideal;
    double lo, hi;
    *exact = *tau = 0.0;
    if (op >= FM_OPS || segment > 2u) return REF_RATE_FAIL;
    pp = &v->patch.op[op];
    o = &v->op[op];
    ms = segment == 0u ? pp->attack_ms : segment == 1u ? pp->decay_ms : pp->release_ms;
    n = ref_segment_samples(ms, v->fs_hz);
    ideal = ref_rate_scaled(n, v->freq_millihz, pp->rate_scale);
    ref_rate_scaled_window(n, v->freq_millihz, pp->rate_scale, exact, tau);
    if (count == ideal) return REF_RATE_EQUAL;
    lo = floor(*exact - *tau + 0.5);
    hi = floor(*exact + *tau + 0.5);
    if (lo < 1.0) lo = 1.0;
    if (hi < 1.0) hi = 1.0;
    if (hi > (double)n) hi = (double)n;
    if ((double)count < lo || (double)count > hi) return REF_RATE_FAIL;
    if (segment == 0u) {
        o->count_a = count;
        if (!o->rate_attack_kept) {
            const uint32_t attack = ref_attack_samples(o->gain, count);
            if (o->stage == FM_STAGE_DELAY) o->count_a_wait = attack;
            else o->count = attack;
            o->rate_g = (1.0 - o->gain) / (double)attack;
        }
    } else if (segment == 1u) {
        o->count_d = count;
        o->rate_d = o->sus / (double)count;
    } else {
        o->rate_r = (double)FM_REL_OCT / (double)count;
    }
    return REF_RATE_ADOPTED;
}

/* : the attenuation in dB10 for a note above the break point, slope
 * tenths of a dB per octave, at most FM_ATT_END_OCT octaves and 1200 in
 * all; 0 when off. */
static double ref_keyscale_db10(uint32_t freq_millihz, uint32_t break_millihz, uint32_t slope_db10)
{
    double oct, db10;
    if (slope_db10 == 0u || freq_millihz <= break_millihz) return 0.0;
    oct = break_millihz ? log2((double)freq_millihz / (double)break_millihz) : (double)FM_ATT_END_OCT;
    if (oct > (double)FM_ATT_END_OCT) oct = (double)FM_ATT_END_OCT;
    db10 = (double)slope_db10 * oct;
    return db10 > 1200.0 ? 1200.0 : db10;
}

/* : the index's gain for that attenuation. */
static double ref_keyscale(uint32_t freq_millihz, uint32_t break_millihz, uint32_t slope_db10)
{
    if (slope_db10 == 0u || freq_millihz <= break_millihz) return 1.0;
    return exp2(-ref_keyscale_db10(freq_millihz, break_millihz, slope_db10) / DB10_PER_OCT);
}

/* : an operator's output gain for a level in dB10 plus its signed
 * key-scaling offset, clamped to 0..1200. */
static double ref_level(const ref_voice_t *v, int i, double db10)
{
    db10 += v->ks_db10[i];
    return exp2(-(db10 < 0.0 ? 0.0 : db10 > 1200.0 ? 1200.0 : db10) / DB10_PER_OCT);
}

/* An operator's index in turns from Q6.10 radians: velocity and  /
 * attenuation, or 's boost 2^octaves saturated at 131070 Q10 first
 * and the velocity after it. */
static double ref_index(const ref_voice_t *v, int i, double index_q10)
{
    const double t = (index_q10 / 1024.0) / (2.0 * PI);
    if (v->ks_boost[i] != 0.0) {
        const double cap = (131070.0 / 1024.0) / (2.0 * PI), b = t * exp2(v->ks_boost[i]);
        return (b > cap ? cap : b) * v->vg[i];
    }
    return t * v->vg[i] * v->ks[i];
}

/*  at note-on, after v->ks holds  / 's gain: the same side and
 * distance give the attenuation a in dB10. A level target takes a rounded
 * to whole dB10 (the engine's one rounding), negated on a "+" side; an
 * index target keeps the gain on a "-" side and on a "+" side takes unity
 * and a boost of a / DB10_PER_OCT octaves. */
static void ref_ks_ext(ref_voice_t *v, int i, uint32_t freq_millihz)
{
    const fm_op_patch_t *pp = &v->patch.op[i];
    const uint32_t target = v->patch.ext[i].ks_target;
    const int left = v->patch.eg[i].left_slope_db10 && freq_millihz < pp->ks_break_millihz;
    const int plus = (v->patch.ext[i].ks_curve >> (left ? 1 : 0)) & 1;
    const double a = left ? ref_keyscale_db10(pp->ks_break_millihz, freq_millihz, v->patch.eg[i].left_slope_db10)
                          : ref_keyscale_db10(freq_millihz, pp->ks_break_millihz, pp->ks_slope_db10);
    if (a == 0.0) return;
    if (target != 0u) v->ks_db10[i] = plus ? -floor(a + 0.5) : floor(a + 0.5);
    if (target != 1u && plus) { v->ks[i] = 1.0; v->ks_boost[i] = a / DB10_PER_OCT; }
    else if (target == 1u) v->ks[i] = 1.0;
}

/*  : the attack again from the sustain gain at the full
 * attack's slope ; the transition sample returns the sustain gain.
 * Its length is the one the harness adopted at the note-on , if any;
 * the step always comes from this side's own g0. */
static void ref_env_loop(ref_op_t *o)
{
    const double g0 = exp2(-o->att);
    o->count  = o->loop_adopt ? o->loop_adopt : ref_attack_samples(g0, o->count_a);
    o->gain   = g0;
    o->rate_g = (1.0 - g0) / (double)o->count;
    o->stage  = FM_STAGE_ATTACK;
}

uint32_t ref_loop_samples(const ref_voice_t *v, int op)
{
    const ref_op_t *o = &v->op[op];
    return ref_attack_samples(exp2(-(o->decay2_on ? o->sus2 : o->sus)), o->count_a);
}

/* Same stage machine as the engine; returns this sample's gain. */
static double ref_env_step(ref_op_t *o)
{
    switch (o->stage) {
    case FM_STAGE_DELAY:
        o->count--;
        if (!o->count) { o->stage = FM_STAGE_ATTACK; o->count = o->count_a_wait; }
        return 0.0;
    case FM_STAGE_ATTACK:
        o->count--;
        if (o->count == 0u) { o->gain = 1.0; o->att = 0.0; o->stage = FM_STAGE_DECAY; o->count = o->count_d; }
        else o->gain += o->rate_g;
        return o->gain;
    case FM_STAGE_DECAY:
        o->count--;
        if (!o->count) {
            o->att = o->sus; o->stage = FM_STAGE_SUSTAIN;
            if (o->decay2_on && o->count_d2) {
                o->count = o->count_d2; o->stage = FM_STAGE_DECAY2;
            } else if (o->decay2_on) o->att = o->sus2;
            if (o->loop && o->stage == FM_STAGE_SUSTAIN) ref_env_loop(o);
        }
        else o->att += o->rate_d;
        break;
    case FM_STAGE_DECAY2:
        o->count--;
        if (!o->count) { o->att = o->sus2; o->stage = FM_STAGE_SUSTAIN; if (o->loop) ref_env_loop(o); }
        else {
            o->att += o->rate_d2;
            if (o->att > o->sus2) o->att = o->sus2;
        }
        break;
    case FM_STAGE_RELEASE:
        o->att += o->rate_r;
        if (o->att >= (double)FM_ATT_END_OCT) { o->att = (double)FM_ATT_END_OCT; o->stage = FM_STAGE_IDLE; }
        break;
    case FM_STAGE_IDLE:
        return 0.0;
    default:
        break;
    }
    return (o->att >= 31.0) ? 0.0 : exp2(-o->att);
}

static const uint32_t REF_NOISE_SEED[4] = { 0x9E3779B9u, 0x7F4A7C15u, 0x3C6EF372u, 0xFE94F82Au };   /* fm_a1.c's */

static uint32_t ref_free_word(uint32_t seed, uint32_t index)
{
    uint32_t x = seed + index * 0x9E3779B9u;
    x = (x ^ (x >> 16)) * 0x85EBCA6Bu;
    x = (x ^ (x >> 13)) * 0xC2B2AE35u;
    return x ^ (x >> 16);
}

static int32_t ref_pink_row(uint32_t word)
{
    const int64_t sample = (int64_t)(word >> 1) - (int64_t)(word >> 31) * 2147483648LL;
    return (int32_t)(sample >> 4);
}

static void ref_pink_draw(ref_op_t *o, uint32_t seed)
{
    if (seed) o->noise = ref_free_word(seed, ++o->noise_index);
    else {
        uint32_t x = o->noise;
        x ^= x << 13; x ^= x >> 17; x ^= x << 5; o->noise = x;
    }
}

static void ref_pink_reset(ref_op_t *o, uint32_t seed, int continuous)
{
    unsigned j;
    o->noise = seed; o->noise_index = 0;
    o->pink_sum = 0; o->pink_counter = 0;
    for (j = 0; j < 8; j++) {
        ref_pink_draw(o, continuous ? seed : 0);
        o->pink_rows[j] = ref_pink_row(o->noise); o->pink_sum += o->pink_rows[j];
    }
    ref_pink_draw(o, continuous ? seed : 0);
}

static void ref_pink_tick(ref_op_t *o, uint32_t seed)
{
    unsigned j;
    o->pink_counter = (o->pink_counter + 1) % 256;
    if (o->pink_counter) {
        for (j = 0; ((o->pink_counter >> j) & 1) == 0; j++) {}
        ref_pink_draw(o, seed);
        o->pink_rows[j] = ref_pink_row(o->noise);
    }
    o->pink_sum = 0;
    for (j = 0; j < 8; j++) o->pink_sum += o->pink_rows[j];
    ref_pink_draw(o, seed);
}

static void ref_pink_skip(ref_op_t *o, uint32_t seed, uint64_t edges)
{
    const uint32_t remainder = (uint32_t)(edges % 256);
    const uint32_t wraps = (uint32_t)(edges / 256) + (o->pink_counter + remainder) / 256;
    const uint32_t last = (o->pink_counter + remainder) % 256;
    unsigned j;
    o->noise_index += (uint32_t)edges + (uint32_t)edges - wraps;
    for (j = 0; j < 8; j++) {
        const uint32_t period = 2u << j;
        const uint32_t age = (last + period - (1u << j)) % period;
        if (edges > age) {
            const uint32_t draws = age + age - (age > last ? 1u : 0u);
            o->pink_rows[j] = ref_pink_row(ref_free_word(seed, o->noise_index - draws - 1));
        }
    }
    o->pink_sum = 0;
    for (j = 0; j < 8; j++) o->pink_sum += o->pink_rows[j];
    o->pink_counter = last; o->noise = ref_free_word(seed, o->noise_index);
}

static void ref_free_lfo_advance(ref_voice_t *v, uint64_t ticks)
{
    const uint64_t sum = (uint64_t)v->lfo_step * ticks + v->lfo_phase;
    v->lfo_wrap += (uint32_t)(sum >> 32);
    v->lfo_phase = (uint32_t)sum;
    if (v->patch.lfo.wave == 5u || v->patch.lfo.wave == 6u) {
        v->lfo_word = ref_free_word(0xDAA66D2Cu, v->lfo_wrap);
        v->lfo_a = (int32_t)(v->lfo_word >> 16) - 32768;
        if (v->patch.lfo.wave == 6u)
            v->lfo_b = (int32_t)(ref_free_word(0xDAA66D2Cu, v->lfo_wrap + 1u) >> 16) - 32768;
    }
}

/* Independent four-product multiplication, instead of the engine's wide
 * backend. The integer phase still uses the increment the harness adopts
 * ; the sine phase sums the reference's own double increment with a
 * bounded binary decomposition, never a huge imprecise double product. */
static void ref_free_catchup(ref_voice_t *v)
{
    const uint64_t elapsed = v->idle_samples;
    const unsigned nops = v->ops ? v->ops : 2u;
    unsigned i;
    if (!elapsed) return;
    for (i = 0; i < nops; i++) {
        ref_op_t *o = &v->op[i];
        uint64_t lo, hi, t, before, left;
        double phase_step;
        if (v->patch.op[i].free_run != 2u) continue;
        lo = (uint64_t)(uint32_t)o->iinc * (uint32_t)elapsed;
        t = (lo >> 32) + (uint64_t)(uint32_t)o->iinc * (elapsed >> 32);
        lo = (lo & UINT32_MAX) | (t << 32);
        hi = (o->iinc >> 32) * (elapsed >> 32) + (t >> 32);
        t = (lo >> 32) + (o->iinc >> 32) * (uint32_t)elapsed;
        lo = (lo & UINT32_MAX) | (t << 32);
        hi += t >> 32;
        before = lo;
        lo += o->iphase;
        hi += lo < before;
        if (v->patch.op[i].wave == 4u || v->patch.op[i].wave == 10u || v->patch.op[i].wave == 11u) {
            o->noise_index += (uint32_t)((hi << 1) | (lo >> 63)) - (uint32_t)(o->iphase >> 63);
            o->noise = ref_free_word(REF_NOISE_SEED[i], o->noise_index);
        }
        else if (v->patch.op[i].wave == 9u)
            ref_pink_skip(o, REF_NOISE_SEED[i], ((hi << 1) | (lo >> 63)) - (o->iphase >> 63));
        o->iphase = lo;
        phase_step = o->inc;
        for (left = elapsed; left; left >>= 1) {
            if (left & 1u) { o->phase += phase_step; o->phase -= floor(o->phase); }
            phase_step *= 2.0; phase_step -= floor(phase_step);
        }
    }
    if (v->patch.lfo.rate_millihz && v->patch.lfo.free_run == 2u) {
        const uint32_t rem = v->ctl ? v->ctl : FM_CTL;
        const uint64_t tail = elapsed % FM_CTL + (FM_CTL - rem);
        ref_free_lfo_advance(v, elapsed / FM_CTL + tail / FM_CTL);
        v->ctl = FM_CTL - (uint32_t)(tail % FM_CTL);
    }
    v->idle_samples = 0u;
}

/* 's shapes at the integer phase p (turns * 2^64) plus pm turns: with
 * no modulation the engine's bit decisions on p itself, else on the real
 * phase; and the noise word, stepped when clk, as a signed value >> 1. */
static double ref_wave(ref_op_t *o, double pm, uint32_t wave, int clk, uint32_t free_seed, unsigned shift)
{
    const double two64 = 18446744073709551616.0;
    uint64_t p = o->iphase;
    double t, q;
    if (wave == 9u) {
        if (clk) ref_pink_tick(o, free_seed);
        return (double)(o->pink_sum + ref_pink_row(o->noise)) / 1073741824.0;
    }
    if (wave == 4u || wave == 10u || wave == 11u) {
        if (clk) {
            if (free_seed) o->noise = ref_free_word(free_seed, ++o->noise_index);
            else { o->noise ^= o->noise << 13; o->noise ^= o->noise >> 17; o->noise ^= o->noise << 5; }
        }
        if (wave != 4u) {
            const int64_t x = (int64_t)(o->noise >> 1) - (int64_t)(o->noise >> 31) * 2147483648LL;
            const int64_t change = (x - o->colour + ((int64_t)1 << (shift - 1))) >> shift;
            o->colour = (int32_t)((int64_t)o->colour + change);
            return (double)(wave == 10u ? o->colour : (x - o->colour) >> 1) / 1073741824.0;
        }
        return ((double)(o->noise >> 1) - (double)(o->noise >> 31) * 2147483648.0) / 1073741824.0;
    }
    if (wave == 7u || wave == 8u) {
        if (pm == 0.0) {
            const int second = (int)(p >> 63);
            if (wave == 7u) return second ? -1.0 : 1.0;
            return (second ? -1.0 : 1.0) * exp2(-16.0 * ((double)(p & 0x7fffffffffffffffull) / two64));
        }
        t = (double)p / two64 + pm; t -= floor(t);
        if (wave == 7u) return t >= 0.5 ? -1.0 : 1.0;
        return (t >= 0.5 ? -1.0 : 1.0) * exp2(-16.0 * (t >= 0.5 ? t - 0.5 : t));
    }
    if (wave == 5u || wave == 6u) {
        if (pm == 0.0 && (p >> 63)) return 0.0;
        t = (double)p / two64 + pm;
        t -= floor(t);
        q = t >= 0.5 ? 0.0 : sin(4.0 * PI * t);
        return wave == 6u ? fabs(q) : q;
    }
    if (pm == 0.0) {
        if (wave == 1u) return (p >> 63) ? 0.0 : sin(2.0 * PI * ((double)p / two64));
        if (wave == 2u) return fabs(sin(2.0 * PI * ((double)p / two64)));
        return ((p >> 62) & 1u) ? 0.0 : sin(2.0 * PI * ((double)(p & 0x3FFFFFFFFFFFFFFFull) / two64));
    }
    t = (double)p / two64 + pm;
    t -= floor(t);
    if (wave == 1u) return t >= 0.5 ? 0.0 : sin(2.0 * PI * t);
    if (wave == 2u) return fabs(sin(2.0 * PI * t));
    q = t >= 0.5 ? t - 0.5 : t;
    return q >= 0.25 ? 0.0 : sin(2.0 * PI * q);
}

/* : the band for the ideal increment, the highest k <= 11 with
 * inc < 2^-(k+1), else 0; the frame and its fraction from the integer
 * position; exact stored samples interpolated in double. */
static double ref_wt(const ref_op_t *o, double t)
{
    const double z = (double)o->wt_pos * (double)(o->wt_frames - 1u) / 32768.0;
    const uint32_t frame = (uint32_t)floor(z);
    const double fr = z - (double)frame;
    uint32_t band = FM_WT_BANDS - 1u, j, i;
    double x, f, s[2];
    while (band && !(o->inc < ldexp(1.0, -(int)band - 1))) band--;
    t -= floor(t);
    x = t * (double)o->wt_length;
    j = (uint32_t)floor(x);
    if (j >= o->wt_length) j = o->wt_length - 1u;
    f = x - (double)j;
    for (i = 0; i < 2u; i++) {
        const uint32_t fi = frame + i < o->wt_frames ? frame + i : frame;
        const int16_t *a = o->wt_bank + ((uint32_t)(band * o->wt_frames + fi) * o->wt_length);
        s[i] = (1.0 - f) * (double)a[j] + f * (double)a[(j + 1u) % o->wt_length];
    }
    return ((1.0 - fr) * s[0] + fr * s[1]) / 32768.0;
}

/*  tuning, independent of the engine's wide type: the clamps compare
 * I * c against 2^65 through 32-bit halves, the interior long-divides
 * 2^112 by I (I > 2^51 there) one quotient bit at a time. */
int ref_ks_delay(uint64_t iinc, uint32_t length, uint32_t *n, uint32_t *f)
{
    uint64_t hi, lo, top, q, r;
    int k;
    if (!iinc) { *n = length - 2u; *f = 0u; return 1; }
    hi = (iinc >> 32) * (2u * (uint64_t)length - 3u);
    lo = (iinc & 0xFFFFFFFFu) * (2u * (uint64_t)length - 3u);
    top = hi + (lo >> 32);                                 /* floor(I * c / 2^32) */
    if (top < ((uint64_t)1 << 33) || (top == ((uint64_t)1 << 33) && !(lo & 0xFFFFFFFFu))) {
        *n = length - 2u; *f = 0u; return 1;
    }
    hi = (iinc >> 32) * 5u;
    lo = (iinc & 0xFFFFFFFFu) * 5u;
    if (hi + (lo >> 32) >= ((uint64_t)1 << 33)) { *n = 2u; *f = 0u; return 2; }
    q = UINT64_MAX / iinc;                                 /* floor(2^64 / I) */
    r = UINT64_MAX % iinc + 1u;
    if (r == iinc) { q++; r = 0u; }
    for (k = 0; k < 48; k++) {
        const uint64_t carry = r >> 63;
        r <<= 1; q <<= 1;
        if (carry || r >= iinc) { r -= iinc; q |= 1u; }
    }
    q -= (uint64_t)1 << 47;                                /* the read delay, Q48 */
    q = (q >> 33) + ((q >> 32) & 1u);                      /* to Q15, nearest, ties up */
    *n = (uint32_t)(q >> 15);
    *f = (uint32_t)(q & 32767u);
    return 0;
}

static double ref_ks_read(const ref_op_t *o)
{
    const uint32_t a = (o->ks_w + o->ks_len - o->ks_n) % o->ks_len;
    const uint32_t b = (a + o->ks_len - 1u) % o->ks_len;
    const double fr = (double)o->ks_f / 32768.0;
    return (1.0 - fr) * o->ks_buf[a] + fr * o->ks_buf[b];
}

static void ref_ks_retune(ref_op_t *o)
{
    if (o->ks_inc != o->iinc) { o->ks_inc = o->iinc; ref_ks_delay(o->iinc, o->ks_len, &o->ks_n, &o->ks_f); }
    if (o->ks_prime) { o->ks_prev = ref_ks_read(o); o->ks_prime = 0u; }
}

void ref_voice_ks_sync(ref_voice_t *v)
{
    int i;
    for (i = 0; i < FM_OPS; i++) if (v->op[i].ks_buf) ref_ks_retune(&v->op[i]);
}

static double ref_ks(ref_op_t *o)
{
    double u;
    ref_ks_retune(o);
    u = ref_ks_read(o);
    o->ks_buf[o->ks_w] = 0.5 * (u + o->ks_prev) * ((double)o->ks_loss / 32768.0);
    o->ks_prev = u;
    o->ks_w = (o->ks_w + 1u) % o->ks_len;
    return u;
}

/* The excitation: the word's floor(int32 / 2), Q2.30, as a real. */
static void ref_ks_fill(ref_op_t *o)
{
    uint32_t k, x = o->ks_word;
    for (k = 0; k < o->ks_len; k++) {
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        o->ks_buf[k] = ldexp((double)((int64_t)(x >> 1) - (int64_t)(x >> 31) * 2147483648LL), -30);
    }
    o->ks_word = x;
    o->ks_w = 0u;
    o->ks_prime = 1u;
}

void ref_voice_bind_ks(ref_voice_t *v, const fm_assets_t *a, double *store)
{
    int i;
    for (i = 0; i < FM_OPS; i++) {
        ref_op_t *o = &v->op[i];
        o->ks_buf = 0;
        if (a && store && o->ks_on && (uint32_t)i < a->ks_count && a->ks[i].data && a->ks[i].capacity >= o->ks_len)
            o->ks_buf = store + (uint32_t)i * FM_KS_BUFFER_MAX;
        o->ks_w = o->ks_prime = o->ks_word = 0u; o->ks_prev = 0.0;
        o->ks_inc = o->iinc; o->ks_n = o->ks_f = 0u;
        if (o->ks_buf) ref_ks_delay(o->iinc, o->ks_len, &o->ks_n, &o->ks_f);
    }
}

static double ref_op_step(ref_op_t *o, double pm_turns, uint32_t wave, uint32_t free_seed, unsigned shift)
{
    double s, senv;
    const uint64_t before = o->iphase;
    o->phase += o->inc;
    if (o->phase >= 1.0) o->phase -= 1.0;
    o->iphase += o->iinc;
    if (o->pd_knee && o->pd_knee != 16384u) {
        const double knee = (double)o->pd_knee / 32768.0;
        double t = o->phase + pm_turns;
        t -= floor(t);
        t = t < knee ? 0.5 * t / knee : 0.5 + 0.5 * (t - knee) / (1.0 - knee);
        s = sin(2.0 * PI * t);
    } else if ((wave >= 1u && wave <= 8u) || (wave >= 9u && wave <= 11u)) s = ref_wave(o, pm_turns, wave, (int)((before ^ o->iphase) >> 63), free_seed, shift);
    else if (o->wt_on) s = o->wt_bank ? ref_wt(o, ldexp((double)o->iphase, -64) + pm_turns) : 0.0;   /* as 's shapes, the integer phase */
    else if (o->ks_on) s = o->ks_buf && o->ks_word ? ref_ks(o) : 0.0;   /* : no phase, so no PM; silent until filled */
    else s = sin(2.0 * PI * (o->phase + pm_turns));
    o->raw_prev2 = o->raw_prev;   /* : kept always, read only by the pre-envelope type */
    o->raw_prev = s;
    senv = s * ref_env_step(o);
    o->prev2 = o->prev;
    o->prev  = senv;
    return senv;
}

/* 's integer cutoff clamps, with widened endpoints at every API rate. */
static double ref_filter_base(const ref_voice_t *v, uint32_t cutoff_millihz)
{
    const uint64_t r  = v->fs_hz ? v->fs_hz : 1u;
    const uint64_t lo = (r * 5u + 11u) / 12u, hi = r * 450u;
    const uint64_t fc = cutoff_millihz < lo ? lo : cutoff_millihz > hi ? hi : cutoff_millihz;
    return (double)fc;
}

/* 's exact coefficients, also accepting 's fractional mHz. */
static void ref_filter_coef_fc(ref_voice_t *v, double fc, uint32_t q_milli)
{
    const uint64_t r = v->fs_hz ? v->fs_hz : 1u;
    const uint32_t q  = q_milli < 500u ? 500u : q_milli > 16000u ? 16000u : q_milli;
    const double g = tan(PI * fc / ((double)r * 1000.0)), k = 1000.0 / (double)q;
    v->fa1 = 1.0 / (1.0 + g * (g + k));
    v->fa2 = g * v->fa1;
    v->fa3 = g * v->fa2;
    v->fk  = k;
}

static void ref_filter_coef(ref_voice_t *v, uint32_t cutoff_millihz, uint32_t q_milli)
{
    ref_filter_coef_fc(v, ref_filter_base(v, cutoff_millihz), q_milli);
}

static double ref_svf(ref_voice_t *v, double v0)
{
    const double v3 = v0 - v->fic2;
    const double v1 = v->fa1 * v->fic1 + v->fa2 * v3;
    const double v2 = v->fic2 + v->fa2 * v->fic1 + v->fa3 * v3;
    v->fic1 = 2.0 * v1 - v->fic1;
    v->fic2 = 2.0 * v2 - v->fic2;
    switch (v->fmode) {
    case 1u: return v2;
    case 2u: return v1;
    case 3u: return v0 - v->fk * v1 - v2;
    default: return v0 - v->fk * v1;
    }
}

/* New cascade reference: exact real recurrences with the explicitly
 * saturated Q2.30 range. The old single-stage ideal stays unchanged. */
static double ref_filter_clip(double x)
{
    const double lim = 2147483647.0 / 1073741824.0;
    return x > lim ? lim : x < -lim ? -lim : x;
}

static double ref_cascade_stage(const ref_voice_t *v, double x, double *ic1, double *ic2)
{
    const double d = ref_filter_clip(x - *ic2);
    const double v1 = v->fa1 * *ic1 + v->fa2 * d;
    const double v2 = *ic2 + v->fa2 * *ic1 + v->fa3 * d;
    const double b = ref_filter_clip(v1), l = ref_filter_clip(v2);
    *ic1 = ref_filter_clip(2.0 * v1 - *ic1);
    *ic2 = ref_filter_clip(2.0 * v2 - *ic2);
    switch (v->fmode) {
    case 1u: return l;
    case 2u: return b;
    case 3u: return ref_filter_clip(x - v->fk * b - l);
    default: return ref_filter_clip(x - v->fk * b);
    }
}

static double ref_filter(ref_voice_t *v, double x, int idle)
{
    double y;
    if (!v->fmode) return x;
    if (!v->f2mode && !v->formant_mask) return ref_svf(v, x);
    if (idle && v->fic1 == 0.0 && v->fic2 == 0.0 && v->f2ic1 == 0.0 && v->f2ic2 == 0.0) return 0.0;
    y = ref_cascade_stage(v, x, &v->fic1, &v->fic2);
    if (v->f2mode) y = ref_cascade_stage(v, y, &v->f2ic1, &v->f2ic2);
    if (idle && ((fabs(v->fic1) <= 2048.0 / 1073741824.0 &&
                  fabs(v->fic2) <= 2048.0 / 1073741824.0 &&
                  fabs(v->f2ic1) <= 2048.0 / 1073741824.0 &&
                  fabs(v->f2ic2) <= 2048.0 / 1073741824.0) ||
                 ++v->filter_idle >= (1u << 18)))
        v->fic1 = v->fic2 = v->f2ic1 = v->f2ic2 = 0.0;
    return y;
}

static uint32_t ref_formant_center(int64_t raw, uint32_t fs)
{
    const uint64_t rate = fs ? fs : 1u;
    const uint64_t low = (rate * 5u + 11u) / 12u, high = rate * 450u;
    uint64_t center = raw <= 0 ? 0u : (uint64_t)raw;
    if (center > UINT32_MAX) center = UINT32_MAX;
    return (uint32_t)(center < low ? low : center > high ? high : center);
}

static void ref_formant_apply(ref_voice_t *v, unsigned i)
{
    const fm_formant_patch_t *p = &v->patch.formant[i];
    ref_formant_t *f = &v->formant[i];
    const uint32_t center = ref_formant_center((int64_t)p->center_millihz + v->mod_formant_center[i], v->fs_hz);
    const int32_t sum = (int32_t)p->level_db10 + v->mod_formant_gain[i];
    const uint32_t level = sum < 0 ? 0u : sum > 1200 ? 1200u : (uint32_t)sum;
    if (f->center_millihz != center) {
        const double rate = v->fs_hz ? (double)v->fs_hz : 1.0;
        const double g = tan(PI * center / (1000.0 * rate));
        f->a1 = 1.0 / (1.0 + g * (g + 1000.0 / p->q_milli));
        f->a2 = g * f->a1; f->a3 = g * f->a2;
        f->center_millihz = center;
    }
    if (f->level_db10 != level) {
        f->gain = pow(10.0, -(double)level / 200.0); f->level_db10 = level;
    }
}

static void ref_formant_init(ref_voice_t *v)
{
    unsigned i;
    v->formant_mask = 0;
    for (i = 0; i < FM_FORMANTS; i++) {
        fm_formant_patch_t *p = &v->patch.formant[i];
        ref_formant_t *f = &v->formant[i];
        p->enabled = p->enabled != 0u;
        if (p->enabled) {
            if (p->q_milli < 500u) p->q_milli = 500u;
            if (p->q_milli > 16000u) p->q_milli = 16000u;
            if (p->level_db10 > 1200u) p->level_db10 = 1200u;
        } else { p->center_millihz = 0; p->q_milli = p->level_db10 = 0; }
        f->a1 = f->a2 = f->a3 = f->ic1 = f->ic2 = f->gain = 0.0;
        f->center_millihz = f->level_db10 = f->idle = 0;
        v->mod_formant_center[i] = 0; v->mod_formant_gain[i] = 0;
        if (p->enabled) {
            v->formant_mask |= 1u << i; f->level_db10 = UINT32_MAX;
            ref_formant_apply(v, i);
        }
    }
}

static int ref_formant_active(const ref_voice_t *v)
{
    unsigned i;
    for (i = 0; i < FM_FORMANTS; i++) if (((v->formant_mask >> i) & 1u) &&
        (v->formant[i].ic1 != 0.0 || v->formant[i].ic2 != 0.0)) return 1;
    return 0;
}

/* Independent real BP recurrence with the same declared state/input bounds.
 * The final nonzero tail sample survives the following state clearing. */
static double ref_formant_step(ref_voice_t *v, double x, int idle)
{
    double sum = 0.0;
    unsigned i;
    for (i = 0; i < FM_FORMANTS; i++) if ((v->formant_mask >> i) & 1u) {
        ref_formant_t *f = &v->formant[i];
        double d, band, low;
        if (idle && f->ic1 == 0.0 && f->ic2 == 0.0) continue;
        d = ref_filter_clip(x - f->ic2);
        band = f->a1 * f->ic1 + f->a2 * d;
        low = f->ic2 + f->a2 * f->ic1 + f->a3 * d;
        f->ic1 = ref_filter_clip(2.0 * band - f->ic1);
        f->ic2 = ref_filter_clip(2.0 * low - f->ic2);
        sum += ref_filter_clip(band) * f->gain;
        if (idle && ((fabs(f->ic1) <= 2048.0 / 1073741824.0 &&
                      fabs(f->ic2) <= 2048.0 / 1073741824.0) || ++f->idle >= (1u << 18)))
            f->ic1 = f->ic2 = 0.0;
    }
    return ref_filter_clip(sum);
}

static double ref_signal(ref_voice_t *v, double x, int idle)
{
    if (!v->formant_mask) return ref_filter(v, x, idle);
    if (!idle) return ref_filter(v, ref_formant_step(v, x, 0), 0);
    if (ref_formant_active(v)) return ref_filter(v, ref_formant_step(v, 0.0, 1), 0);
    return ref_filter(v, 0.0, 1);
}

/* The gain the operator produced on its last sample. */
static double ref_current_gain(const ref_op_t *o)
{
    if (o->stage == FM_STAGE_IDLE || o->stage == FM_STAGE_DELAY) return 0.0;
    if (o->stage == FM_STAGE_ATTACK) return o->gain;
    return (o->att >= 31.0) ? 0.0 : exp2(-o->att);
}

/* The operators the voice steps: 2 on the fixed path, else the route's. */
static int ref_ops_stepped(const ref_voice_t *v)
{
    return v->ops ? (int)v->ops : 2;
}

/* Nonzero while a carrier is not idle (op[0] on the fixed path). */
int ref_voice_sounding(const ref_voice_t *v)
{
    int i;
    for (i = 0; i < FM_OPS; i++)
        if (((v->carriers >> i) & 1u) && v->op[i].stage != FM_STAGE_IDLE) return 1;
    return 0;
}

/* : each edge's index from its operator's, weight at most 1024.
 * Row pointers: gcc 13 -Os deletes the indexed form (ivopts + modref). */
static void ref_set_edges(ref_voice_t *v)
{
    int i, j;
    for (j = 0; j < (int)v->ops; j++) {
        double *e = v->edge[j];
        const uint16_t *wr = v->patch.route.w[j];
        const uint8_t *kr = v->patch.route.kind[j];
        const double index = v->op[j].index;
        for (i = 0; i < (int)v->ops; i++) {
            const uint32_t w = wr[i] <= 1024u ? wr[i] : 1024u;
            e[i] = (i == j || kr[i] != 0u) ? 0.0 : index * (double)w / 256.0;
        }
    }
}

/* , : the engine's control clock and integer LFO (fm_a1.c), the
 * same bits; the increments, levels and indices they move are exact. */
static const uint32_t REF_LFO_SEED = 0xDAA66D2Cu;   /* fm_a1.c's */

static int32_t ref_lfo_draw(ref_voice_t *v)
{
    uint32_t x = v->lfo_word;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    v->lfo_word = x;
    return (int32_t)(x >> 16) - 32768;
}

/* fm_sine_q30: the engine's table, linearly interpolated, Q2.30. */
static int32_t ref_sine_q30(uint32_t p)
{
    const uint32_t i = p >> (32 - FM_TABLES_SINE_BITS), frac = p & ((1u << (32 - FM_TABLES_SINE_BITS)) - 1u);
    const int32_t a = fm_sine_tbl[i], b = fm_sine_tbl[(i + 1u) & ((1u << FM_TABLES_SINE_BITS) - 1u)];
    return a + (int32_t)(((int64_t)(b - a) * (int64_t)frac) >> (32 - FM_TABLES_SINE_BITS));
}

/* The LFO's value at phase p (turns * 2^32), Q1.15 (fm_lfo_wave). */
static int32_t ref_lfo_wave(const ref_voice_t *v, uint32_t p)
{
    int32_t l;
    uint32_t d;
    switch (v->patch.lfo.wave) {
    case 1u:
        d = p + 0x40000000u;
        d = d >= 0x80000000u ? d - 0x80000000u : 0x80000000u - d;
        l = 32768 - (int32_t)(d >> 15);
        return l > 32767 ? 32767 : l;
    case 2u: return (int32_t)((p >> 16) ^ 0x8000u) - 32768;
    case 3u: return (int32_t)(((0u - p) >> 16) ^ 0x8000u) - 32768;
    case 4u: return (p & 0x80000000u) ? -32768 : 32767;
    case 5u: return v->lfo_a;
    case 6u: return v->lfo_a + (int32_t)(((int64_t)(v->lfo_b - v->lfo_a) * (int64_t)(p >> 17)) >> 15);
    default:
        l = ref_sine_q30(p) >> 15;
        return l > 32767 ? 32767 : l;
    }
}

/* The depths' gain, Q1.15 (fm_lfo_gain). */
static int64_t ref_lfo_gain(const ref_voice_t *v)
{
    uint32_t e;
    if (v->lfo_time < v->lfo_delay) return 0;
    e = v->lfo_time - v->lfo_delay;
    if (e >= v->lfo_fade) return 32768;
    return (int64_t)(((uint64_t)e << 15) / v->lfo_fade);
}

/* Turns per sample at cents from the note, clamped at Nyquist. */
static double ref_inc_cents(uint32_t freq_millihz, uint32_t ratio_q16, int32_t cents, double fs)
{
    double inc;
    if (fs == 0.0) return 0.5;
    inc = (double)freq_millihz * (double)ratio_q16 * exp2((double)cents / 1200.0) / (1000.0 * fs * 65536.0);
    return inc >= 0.5 ? 0.5 : inc;
}

/* : zero levels disable the envelope even when times or mask are set. */
static int ref_peg_on(const ref_voice_t *v)
{
    int i;
    for (i = 0; i < 4; i++)
        if (v->patch.peg.level_cents[i] != 0) return 1;
    return 0;
}

/* One rounding to control ticks; a zero count is an immediate jump. */
static uint32_t ref_ctl_ticks(uint32_t ms, uint32_t fs)
{
    const uint64_t n = ((uint64_t)ms * fs + 500u * (uint64_t)FM_CTL) / (1000u * (uint64_t)FM_CTL);
    return n > FM_SEG_MAX / FM_CTL ? (uint32_t)(FM_SEG_MAX / FM_CTL) : (uint32_t)n;
}

/* Stages 1..3 approach L1..L3, 4 holds, 5 approaches L4, 6 is done. */
static void ref_peg_enter(ref_voice_t *v, uint32_t stage)
{
    for (;;) {
        const uint32_t i = stage == 5u ? 3u : stage - 1u;
        v->peg_stage = stage;
        v->peg_start = v->peg_cur;
        v->peg_target = v->patch.peg.level_cents[i];
        v->peg_n = ref_ctl_ticks(v->patch.peg.time_ms[i], v->fs_hz);
        v->peg_j = 0u;
        if (v->peg_n != 0u) return;
        v->peg_cur = v->peg_target;
        if (stage == 3u) { v->peg_stage = 4u; return; }
        if (stage == 5u) { v->peg_stage = 6u; return; }
        stage++;
    }
}

/* Independent of the engine's quotient/remainder accumulator: after j
 * ticks, start + sign(d)*floor(j*abs(d)/n), exact at the endpoint. */
static int ref_peg_step(ref_voice_t *v)
{
    const int32_t before = v->peg_cur;
    if ((v->peg_stage >= 1u && v->peg_stage <= 3u) || v->peg_stage == 5u) {
        const int64_t d = (int64_t)v->peg_target - v->peg_start;
        const int64_t mag = d < 0 ? -d : d;
        const int64_t step = (int64_t)++v->peg_j * mag / v->peg_n;
        v->peg_cur = v->peg_start + (int32_t)(d < 0 ? -step : step);
        if (v->peg_j == v->peg_n) {
            if (v->peg_stage == 3u) v->peg_stage = 4u;
            else if (v->peg_stage == 5u) v->peg_stage = 6u;
            else ref_peg_enter(v, v->peg_stage + 1u);
        }
    }
    return v->peg_cur != before;
}

/* Apply cached offsets without advancing either envelope or control clock.
 * The base frequency is never clamped before modulation. */
static void ref_peg_pitch(ref_voice_t *v)
{
    const fm_lfo_patch_t *lp = &v->patch.lfo;
    const int nops = ref_ops_stepped(v);
    int i;
    for (i = 0; i < nops; i++) {
        if ((v->peg_mask >> i) & 1u) {
            const int32_t cents = v->peg_cur
                + (lp->rate_millihz != 0u && ((lp->mask_pitch >> i) & 1u) ? v->lfo_cents : 0);
            v->op[i].inc = ref_inc_cents(v->freq_millihz, v->patch.op[i].ratio_q16, cents, v->fs);
            v->op[i].iinc = (uint64_t)ldexp(v->op[i].inc, 64);   /* render_ref adopts the engine's */
        }
    }
}

static int ref_fenv_on(const ref_voice_t *v)
{
    return v->fmode && (v->patch.fenv.depth_cents || v->patch.fenv.track_permille);
}

/* Integer cents, with ties away from zero, then the audible-range clamp. */
static int32_t ref_fenv_cents(const ref_voice_t *v, int32_t e)
{
    const int32_t depth = v->patch.fenv.depth_cents;
    const uint32_t mag = ((uint32_t)(depth < 0 ? -depth : depth) * (uint32_t)e + 16384u) >> 15;
    const int32_t c = (depth < 0 ? -(int32_t)mag : (int32_t)mag) + v->fe_track;
    return c < -12800 ? -12800 : c > 12800 ? 12800 : c;
}

static int32_t ref_track_cents(const ref_voice_t *v, uint32_t freq)
{
    const fm_fenv_patch_t *p = &v->patch.fenv;
    double oct, cents;
    if (!p->track_permille || freq == p->ref_millihz) return 0;
    oct = freq ? log2((double)freq / (double)p->ref_millihz) : -20.0;
    if (oct < -20.0) oct = -20.0;
    if (oct > 20.0) oct = 20.0;
    cents = (double)p->track_permille / 1000.0 * 1200.0 * oct;
    return cents < 0.0 ? -(int32_t)floor(-cents + 0.5) : (int32_t)floor(cents + 0.5);
}

static void ref_fenv_apply(ref_voice_t *v)
{
    if (v->mod_active) { ref_mod_filter(v); return; }
    const uint64_t r = v->fs_hz ? v->fs_hz : 1u;
    const double lo = (double)((r * 5u + 11u) / 12u), hi = (double)(r * 450u);
    const int32_t c = ref_fenv_cents(v, v->fe_e);
    double fc = v->fe_base * exp2((double)c / 1200.0);
    if (fc < lo) fc = lo;
    if (fc > hi) fc = hi;
    ref_filter_coef_fc(v, fc, v->fe_q);
}

/* Stages 1 attack, 2 decay, 3 sustain, 4 release, 5 done. A retrigger
 * supplies its shortened attack count; zero-time attack chains to decay. */
static void ref_fenv_enter(ref_voice_t *v, uint32_t stage, uint32_t n)
{
    for (;;) {
        v->fe_stage = stage;
        v->fe_start = v->fe_e;
        v->fe_target = stage == 1u ? 32768 : stage == 2u ? v->fe_s : 0;
        v->fe_n = n; v->fe_j = 0u;
        if (n != 0u) return;
        v->fe_e = v->fe_target;
        if (stage != 1u) { v->fe_stage = stage == 2u ? 3u : 5u; return; }
        stage = 2u;
        n = ref_ctl_ticks(v->patch.fenv.decay_ms, v->fs_hz);
    }
}

static void ref_fenv_step(ref_voice_t *v)
{
    const int32_t before = v->fe_e;
    if (v->fe_stage == 1u || v->fe_stage == 2u || v->fe_stage == 4u) {
        const int64_t d = (int64_t)v->fe_target - v->fe_start;
        const int64_t mag = d < 0 ? -d : d;
        const int64_t step = (int64_t)++v->fe_j * mag / v->fe_n;
        v->fe_e = v->fe_start + (int32_t)(d < 0 ? -step : step);
        if (v->fe_j == v->fe_n) {
            if (v->fe_stage == 1u) ref_fenv_enter(v, 2u, ref_ctl_ticks(v->patch.fenv.decay_ms, v->fs_hz));
            else v->fe_stage = v->fe_stage == 2u ? 3u : 5u;
        }
    }
    if (!v->mod_active && v->fe_e != before && ref_fenv_cents(v, v->fe_e) != ref_fenv_cents(v, before)) ref_fenv_apply(v);
}

static int ref_w4_step(ref_voice_t *v)
{
    const int changed = ref_peg_step(v);
    if (ref_fenv_on(v)) ref_fenv_step(v);
    return changed;
}

/* The unchanged  LFO arithmetic, with peg operators applied separately. */
static void ref_lfo_tick(ref_voice_t *v)
{
    const fm_lfo_patch_t *lp = &v->patch.lfo;
    const int nops = ref_ops_stepped(v);
    const uint32_t p = v->lfo_phase;
    const int64_t l = ref_lfo_wave(v, p), g = ref_lfo_gain(v);
    const int64_t pc = ((int64_t)(lp->pitch_cents < 1200u ? lp->pitch_cents : 1200u) * g) >> 15;
    const int64_t lc = ((int64_t)(lp->level_db10 < 1200u ? lp->level_db10 : 1200u) * g) >> 15;
    const int64_t ic = ((int64_t)lp->index_q10 * g) >> 15;
    const uint32_t att = (uint32_t)((lc * (32767 - l)) >> 16);
    const int64_t ioff = (ic * l) >> 15;
    int i;
    v->lfo_cents = (int32_t)((pc * l) >> 15);
    for (i = 0; i < nops; i++) {
        const fm_op_patch_t *pp = &v->patch.op[i];
        ref_op_t *o = &v->op[i];
        if (((lp->mask_pitch >> i) & 1u) && !pp->fixed_millihz && !((v->peg_mask >> i) & 1u)) {
            o->inc = ref_inc_cents(v->freq_millihz, pp->ratio_q16, v->lfo_cents, v->fs);
            o->iinc = (uint64_t)ldexp(o->inc, 64);   /* render_ref adopts the engine's */
        }
        if ((lp->mask_level >> i) & 1u) o->out = ref_level(v, (int)i, (double)pp->level_db10 + (double)att) * v->vg[i];
        if ((lp->mask_index >> i) & 1u) {
            const int64_t x = (int64_t)pp->index_q10 + ioff;
            o->index = ref_index(v, (int)i, (double)(x > 0 ? x : 0));
        }
    }
    if (lp->mask_index) ref_set_edges(v);
    if (lp->free_run != 2u) {
        v->lfo_phase = p + v->lfo_step;
        if (v->lfo_phase < p) {
            if (lp->wave == 5u) v->lfo_a = ref_lfo_draw(v);
            else if (lp->wave == 6u) { v->lfo_a = v->lfo_b; v->lfo_b = ref_lfo_draw(v); }
        }
        v->lfo_time = v->lfo_time <= UINT32_MAX - FM_CTL ? v->lfo_time + FM_CTL : UINT32_MAX;
    }
}

/* Independent  reference: ideal gains/logarithms feed the same specified
 * integer source quantization. No engine source/smoother state is adopted. */
static int32_t ref_mod_clamp(int64_t x, int32_t lo, int32_t hi)
{
    return x < lo ? lo : x > hi ? hi : (int32_t)x;
}

static int32_t ref_mod_normalize(int32_t x, uint32_t scale)
{
    const double z = floor(fabs((double)x) * 32768.0 / scale + 0.5);
    const int32_t y = z > 32768.0 ? 32768 : (int32_t)z;
    return x < 0 ? -y : y;
}

static int ref_mod_pm_source(const ref_voice_t *v, uint32_t i)
{
    uint32_t j;
    if (!v->ops) return i == 1u;
    for (j = 0; j < v->ops; j++)
        if (j != i && v->patch.route.w[i][j] && !v->patch.route.kind[i][j]) return 1;
    return 0;
}

static void ref_mod_lfo_reset(ref_voice_t *v)
{
    const fm_lfo_patch_t *p = &v->patch.lfo;
    v->mod_lfo_rate = p->rate_millihz < 10u ? 10u : p->rate_millihz > 50000u ? 50000u : p->rate_millihz;
    v->mod_lfo_pitch = p->pitch_cents > 1200u ? 1200u : p->pitch_cents;
    v->mod_lfo_level = p->level_db10 > 1200u ? 1200u : p->level_db10;
    v->mod_lfo_index = p->index_q10;
}

static void ref_mod_init(ref_voice_t *v)
{
    uint32_t i, rate_mask = 0u;
    uint64_t upper;
    const uint32_t nops = (uint32_t)ref_ops_stepped(v);
    v->mod_active = v->mod_pitch_mask = 0u;
    v->mod_velocity = v->mod_note = v->mod_cutoff = v->mod_q = 0;
    ref_mod_lfo_reset(v); upper = v->mod_lfo_rate;
    for (i = 0; i < FM_OPS; i++) v->mod_pitch[i] = v->mod_level[i] = v->mod_index[i] = v->mod_pd[i] = 0;
    for (i = 0; i < FM_CTL_INPUTS; i++) v->ctl_input[i] = 0;
    for (i = 0; i < FM_GEN_INPUTS; i++) v->gen_input[i] = 0;
    for (i = 0; i < FM_MOD_SLOTS; i++) {
        fm_mod_slot_patch_t *s = &v->patch.mod[i];
        const uint32_t source = s->source, target = s->target, op = s->target_index;
        int32_t limit = 0;
        int valid = source == 0u ? v->patch.lfo.rate_millihz != 0u
                  : source == 1u ? ref_peg_on(v)
                  : source == 2u ? ref_fenv_on(v)
                  : source >= 3u && source <= 6u ? source - 3u < nops
                  : source == 7u || source == 8u || (source >= 16u && source < 24u)
                    || (source >= 64u && source < 64u + FM_GEN_INPUTS);
        if (s->shift > 15u) s->shift = 15u;
        s->reserved[0] = s->reserved[1] = s->reserved[2] = 0u;
        if (target == 1u) { limit = 4800; valid = valid && op < nops; }
        else if (target == 2u) { limit = 1200; valid = valid && op < nops && ((v->carriers >> op) & 1u); }
        else if (target == 3u) { limit = 65535; valid = valid && op < nops && ref_mod_pm_source(v, op); }
        else if (target == 4u || target == 5u) { limit = target == 4u ? 9600 : 15500; valid = valid && op == 0u && v->fmode; }
        else if (target >= 6u && target <= 9u) { limit = target == 6u ? 49990 : target == 9u ? 65535 : 1200; valid = valid && op == 0u && v->patch.lfo.rate_millihz; }
        else if (target == 32u) { limit = 32768; valid = valid && op < nops && v->patch.wt[op].enabled; }
        else if (target == 33u) { limit = 32767; valid = valid && op < nops && v->patch.pd[op].enabled; }
        else if (target == 34u || target == 35u) { limit = 32768; valid = valid && op == 0u && v->patch.vector.enabled; }
        else if (target == 36u || target == 37u) { limit = target == 36u ? INT32_MAX : 1200; valid = valid && op < FM_FORMANTS && v->patch.formant[op].enabled; }
        else valid = 0;
        s->amount = ref_mod_clamp(s->amount, -limit, limit);
        v->mod_cur[i] = 0;
        if (!valid) { s->target = 0u; s->amount = 0; }
        if (!valid || !s->amount) continue;
        v->mod_active |= 1u << i;
        if (target == 1u && !v->patch.op[op].fixed_millihz) v->mod_pitch_mask |= 1u << op;
        if (target == 6u) { rate_mask |= 1u << i; upper += (uint32_t)(s->amount < 0 ? -s->amount : s->amount); }
    }
    if (upper > 50000u) upper = 50000u;
    if (rate_mask && upper * FM_CTL >= (uint64_t)500u * v->fs_hz) {
        v->mod_active &= ~rate_mask;
        for (i = 0; i < FM_MOD_SLOTS; i++) if ((rate_mask >> i) & 1u) {
            v->patch.mod[i].target = 0u; v->patch.mod[i].amount = 0;
        }
    }
}

static void ref_mod_filter(ref_voice_t *v)
{
    const uint64_t rate = v->fs_hz ? v->fs_hz : 1u;
    const double lo = (double)((rate * 5u + 11u) / 12u), hi = (double)(rate * 450u);
    int32_t c = v->mod_cutoff;
    uint32_t q;
    double fc;
    if (!v->fmode) return;
    if (ref_fenv_on(v)) {
        const int32_t d = v->patch.fenv.depth_cents;
        const int32_t mag = (int32_t)floor(fabs((double)d) * v->fe_e / 32768.0 + 0.5);
        c += (d < 0 ? -mag : mag) + v->fe_track;
    }
    c = ref_mod_clamp(c, -12800, 12800);
    q = (uint32_t)ref_mod_clamp((int64_t)v->fe_q + v->mod_q, 500, 16000);
    fc = v->fe_base * exp2((double)c / 1200.0);
    if (fc < lo) fc = lo;
    if (fc > hi) fc = hi;
    ref_filter_coef_fc(v, fc, q);
}

static void ref_mod_pitch_apply(ref_voice_t *v)
{
    const fm_lfo_patch_t *lp = &v->patch.lfo;
    const uint32_t lm = lp->rate_millihz ? lp->mask_pitch : 0u;
    int i;
    for (i = 0; i < ref_ops_stepped(v); i++) {
        ref_op_t *o = &v->op[i];
        const fm_op_patch_t *p = &v->patch.op[i];
        int32_t c;
        if (p->fixed_millihz) continue;
        if (!(((v->mod_pitch_mask | v->peg_mask | lm) >> i) & 1u)) continue;
        c = (((v->mod_pitch_mask >> i) & 1u) ? v->mod_pitch[i] : 0)
          + (((v->peg_mask >> i) & 1u) ? v->peg_cur : 0)
          + (((lm >> i) & 1u) ? v->lfo_cents : 0);
        /* Matrix pitch alone has the wider +/-9-octave base range. */
        if (((v->mod_pitch_mask >> i) & 1u) && (v->fs == 0.0 ||
            (double)v->freq_millihz * p->ratio_q16 / (1000.0 * v->fs * 65536.0) >= 256.0)) o->inc = 0.5;
        else o->inc = ref_inc_cents(v->freq_millihz, p->ratio_q16, c, v->fs);
        o->iinc = (uint64_t)ldexp(o->inc, 64);
    }
}

static void ref_mod_tick(ref_voice_t *v, unsigned changed)
{
    const fm_lfo_patch_t *lp = &v->patch.lfo;
    int32_t source[24] = { 0 }, sum[10][FM_OPS] = { { 0 } };
    int32_t pd_sum[FM_OPS] = { 0 }, wt_sum[FM_OPS] = { 0 };
    int32_t vector_sum[2] = { 0, 0 };
    int64_t formant_center[FM_FORMANTS] = { 0 };
    int32_t formant_gain[FM_FORMANTS] = { 0 };
    int64_t l = 0, g = 0, att = 0, ioff = 0;
    uint32_t p = v->lfo_phase, i;
    const int note = (changed & REF_MOD_NOTE) != 0u;
    if (lp->rate_millihz) {
        if (lp->free_run == 2u && !(changed & REF_LFO_HOLD)) ref_free_lfo_advance(v, 1u);
        p = v->lfo_phase; l = ref_lfo_wave(v, p); g = ref_lfo_gain(v);
        source[0] = (int32_t)floor((double)l * (double)g / 32768.0);
        v->lfo_cents = (int32_t)(((((int64_t)v->mod_lfo_pitch * g) >> 15) * l) >> 15);
        att = ((((int64_t)v->mod_lfo_level * g) >> 15) * (32767 - l)) >> 16;
        ioff = ((((int64_t)v->mod_lfo_index * g) >> 15) * l) >> 15;
    }
    source[1] = ref_mod_normalize(v->peg_cur, 4800u);
    source[2] = v->fe_e;
    for (i = 0; i < (uint32_t)ref_ops_stepped(v); i++) {
        const double gain = ref_current_gain(&v->op[i]);
        source[3u + i] = (int32_t)floor(gain * 32768.0 + 0.5);
        if (source[3u + i] > 32768) source[3u + i] = 32768;
    }
    source[7] = v->mod_velocity; source[8] = v->mod_note;
    for (i = 0; i < FM_CTL_INPUTS; i++) source[16u + i] = v->ctl_input[i];
    for (i = 0; i < FM_MOD_SLOTS; i++) if ((v->mod_active >> i) & 1u) {
        const fm_mod_slot_patch_t *s = &v->patch.mod[i];
        const int32_t value = s->source >= 64u ? v->gen_input[s->source - 64u] : source[s->source];
        int32_t x = v->mod_cur[i];
        if (!s->shift) x = value;
        else if (!note) {
            const int32_t d = value - x;
            const int32_t denominator = 1 << s->shift;
            int32_t step = d / denominator;
            if (d % denominator) step += d > 0 ? 1 : -1;
            x += step;
        }
        v->mod_cur[i] = x;
        if (s->target == 36u) formant_center[s->target_index] += (int64_t)x * s->amount / 32768;
        else if (s->target == 37u) formant_gain[s->target_index] += (int32_t)((int64_t)x * s->amount / 32768);
        else if (s->target == 34u || s->target == 35u) vector_sum[s->target - 34u] += (int32_t)((int64_t)x * s->amount / 32768);
        else if (s->target == 33u) pd_sum[s->target_index] += (int32_t)((int64_t)x * s->amount / 32768);
        else if (s->target == 32u) wt_sum[s->target_index] += (int32_t)((int64_t)x * s->amount / 32768);
        else sum[s->target][s->target_index] += (int32_t)((int64_t)x * s->amount / 32768);
    }
    for (i = 0; i < (uint32_t)ref_ops_stepped(v); i++) {
        const fm_op_patch_t *op = &v->patch.op[i];
        int32_t level, index;
        v->mod_pitch[i] = ref_mod_clamp(sum[1][i], -4800, 4800);
        v->mod_level[i] = ref_mod_clamp(sum[2][i], -1200, 1200);
        v->mod_index[i] = ref_mod_clamp(sum[3][i], -65535, 65535);
        v->mod_pd[i] = ref_mod_clamp(pd_sum[i], -32767, 32767);
        if (v->patch.pd[i].enabled)
            v->op[i].pd_knee = (uint32_t)ref_mod_clamp((int32_t)v->patch.pd[i].knee_q15 + v->mod_pd[i], 1, 32767);
        v->mod_wt[i] = ref_mod_clamp(wt_sum[i], -32768, 32768);
        if (v->patch.wt[i].enabled)
            v->op[i].wt_pos = (uint32_t)ref_mod_clamp((int32_t)v->patch.wt[i].position_q15 + v->mod_wt[i], 0, 32768);
        level = ref_mod_clamp((int64_t)op->level_db10 + (lp->rate_millihz && ((lp->mask_level >> i) & 1u) ? att : 0) + v->mod_level[i] + (int64_t)v->ks_db10[i], 0, 1200);
        index = ref_mod_clamp((int64_t)op->index_q10 + (lp->rate_millihz && ((lp->mask_index >> i) & 1u) ? ioff : 0) + v->mod_index[i], 0, 131070);
        v->op[i].out = exp2(-ref_db10_to_oct((uint32_t)level)) * v->vg[i];
        v->op[i].index = ref_index(v, (int)i, (double)index);
    }
    v->vector_x = (uint32_t)ref_mod_clamp((int32_t)v->patch.vector.x_q15 + vector_sum[0], 0, 32768);
    v->vector_y = (uint32_t)ref_mod_clamp((int32_t)v->patch.vector.y_q15 + vector_sum[1], 0, 32768);
    v->mod_cutoff = ref_mod_clamp(sum[4][0], -9600, 9600);
    v->mod_q = ref_mod_clamp(sum[5][0], -15500, 15500);
    ref_mod_pitch_apply(v); ref_set_edges(v); ref_mod_filter(v);
    for (i = 0; i < FM_FORMANTS; i++) {
        v->mod_formant_center[i] = formant_center[i]; v->mod_formant_gain[i] = formant_gain[i];
        if ((v->formant_mask >> i) & 1u) ref_formant_apply(v, i);
    }
    if (lp->rate_millihz) {
        const uint64_t den = (uint64_t)v->fs_hz * 1000u;
        if (lp->free_run != 2u) {
            v->lfo_phase = p + v->lfo_step;
            if (v->lfo_phase < p) {
                if (lp->wave == 5u) v->lfo_a = ref_lfo_draw(v);
                else if (lp->wave == 6u) { v->lfo_a = v->lfo_b; v->lfo_b = ref_lfo_draw(v); }
            }
            v->lfo_time = v->lfo_time <= UINT32_MAX - FM_CTL ? v->lfo_time + FM_CTL : UINT32_MAX;
        }
        ref_mod_lfo_reset(v);
        v->mod_lfo_rate = (uint32_t)ref_mod_clamp((int64_t)v->mod_lfo_rate + ref_mod_clamp(sum[6][0], -49990, 49990), 10, 50000);
        v->mod_lfo_pitch = (uint32_t)ref_mod_clamp((int64_t)v->mod_lfo_pitch + ref_mod_clamp(sum[7][0], -1200, 1200), 0, 1200);
        v->mod_lfo_level = (uint32_t)ref_mod_clamp((int64_t)v->mod_lfo_level + ref_mod_clamp(sum[8][0], -1200, 1200), 0, 1200);
        v->mod_lfo_index = (uint32_t)ref_mod_clamp((int64_t)v->mod_lfo_index + ref_mod_clamp(sum[9][0], -65535, 65535), 0, 65535);
        v->lfo_step = den ? (uint32_t)((((uint64_t)v->mod_lfo_rate * FM_CTL << 32) + den / 2u) / den) : 0u;
    }
    if (!(changed & REF_LFO_HOLD)) v->ctl = FM_CTL;
}

/* One control tick: only the enabled LFO advances; peg was stepped by the
 * grid caller, or entered without stepping at note-on. */
static void ref_ctl_tick(ref_voice_t *v, int peg_changed)
{
    if (v->mod_active) { ref_mod_tick(v, (unsigned)peg_changed); return; }
    const fm_lfo_patch_t *lp = &v->patch.lfo;
    if (lp->rate_millihz && lp->free_run == 2u) ref_free_lfo_advance(v, 1u);
    if (lp->rate_millihz != 0u) ref_lfo_tick(v);
    if (peg_changed || (lp->rate_millihz != 0u && (lp->mask_pitch & v->peg_mask))) ref_peg_pitch(v);
    v->ctl = FM_CTL;
}

/* The LFO at a note-on (fm_lfo_start), its first tick at once. */
static void ref_lfo_start(ref_voice_t *v)
{
    const fm_lfo_patch_t *lp = &v->patch.lfo;
    const uint64_t fs = v->fs_hz, den = fs * 1000u;
    const uint64_t r = lp->rate_millihz < 10u ? 10u : lp->rate_millihz > 50000u ? 50000u : lp->rate_millihz;
    const uint64_t dl = ((uint64_t)lp->delay_ms * fs + 500u) / 1000u, fd = ((uint64_t)lp->fade_ms * fs + 500u) / 1000u;
    v->lfo_step  = den ? (uint32_t)((((r * FM_CTL) << 32) + den / 2u) / den) : 0u;
    v->lfo_time  = 0u;
    v->lfo_delay = dl > UINT32_MAX ? UINT32_MAX : (uint32_t)dl;
    v->lfo_fade  = fd > UINT32_MAX ? UINT32_MAX : (uint32_t)fd;
    if (lp->free_run == 2u) {
        if (v->mod_active) ref_mod_tick(v, REF_MOD_NOTE | REF_LFO_HOLD);
        else { ref_lfo_tick(v); ref_peg_pitch(v); }
    } else {
        v->lfo_phase = (uint32_t)lp->phase_u16 << 16;
        v->lfo_word = REF_LFO_SEED;
        v->lfo_a = v->lfo_b = 0;
        if (lp->wave == 5u || lp->wave == 6u) v->lfo_a = ref_lfo_draw(v);
        if (lp->wave == 6u) v->lfo_b = ref_lfo_draw(v);
        ref_ctl_tick(v, 1 | REF_MOD_NOTE);
    }
}

void ref_voice_init(ref_voice_t *v, const fm_patch_t *p, uint32_t sample_rate_hz)
{
    int i, j;
    v->fs = (double)sample_rate_hz;
    v->fs_hz = sample_rate_hz;
    v->patch = *p;
    v->patch.vector.enabled = FM_OPS >= 4 && p->vector.enabled && p->route.ops == 4u && p->route.carriers == 15u;
    v->patch.vector.x_q15 = v->patch.vector.enabled ? (p->vector.x_q15 > 32768u ? 32768u : p->vector.x_q15) : 0u;
    v->patch.vector.y_q15 = v->patch.vector.enabled ? (p->vector.y_q15 > 32768u ? 32768u : p->vector.y_q15) : 0u;
    v->vector_x = v->patch.vector.x_q15; v->vector_y = v->patch.vector.y_q15;
    if (v->patch.filter.stages > 2u) v->patch.filter.stages = 2u;
    v->patch.shape.kind = p->shape.kind <= 4u ? p->shape.kind : 0u;
    v->patch.shape.pos = p->shape.pos == 1u ? 1u : 0u;                 /*  */
    v->patch.shape.drive_db10 = p->shape.drive_db10 > 240u ? 240u : p->shape.drive_db10;
    v->shape_gain = pow(10.0, (double)v->patch.shape.drive_db10 / 200.0);
    for (i = 0; i < FM_OPS; i++) for (j = 0; j < FM_OPS; j++) {
        const uint32_t kind = p->route.kind[i][j] <= 3u ? p->route.kind[i][j] : 0u;
        const uint32_t limit = kind ? 256u : 1024u;
        v->patch.route.kind[i][j] = (uint8_t)kind;
        v->patch.route.w[i][j] = kind == 3u && p->route.w[i][j] ? (uint16_t)256u      /* : sync is on or off */
                               : p->route.w[i][j] > limit ? (uint16_t)limit : p->route.w[i][j];
    }
    for (i = 0; i < FM_OPS; i++) {
        ref_op_t *o = &v->op[i];
        v->patch.noise[i].shift = p->noise[i].shift == 0u ? 4u : p->noise[i].shift > 15u ? 15u : p->noise[i].shift;
        v->patch.ext[i].ks_target = p->ext[i].ks_target <= 2u ? p->ext[i].ks_target : 0u;   /*  */
        v->patch.ext[i].ks_curve = p->ext[i].ks_curve & 3u;
        v->patch.ext[i].fb_type = p->ext[i].fb_type & 3u;
        v->patch.ext[i].loop = p->ext[i].loop ? 1u : 0u;                                     /*  */
        v->ks_db10[i] = v->ks_boost[i] = 0.0;
        v->patch.pd[i].enabled = p->pd[i].enabled && p->op[i].wave == 0u && !p->wt[i].enabled && !p->ks[i].enabled;
        v->patch.pd[i].knee_q15 = v->patch.pd[i].enabled
            ? (p->pd[i].knee_q15 < 1u ? 1u : p->pd[i].knee_q15 > 32767u ? 32767u : p->pd[i].knee_q15) : 0u;
        o->pd_knee = v->patch.pd[i].knee_q15;
        v->patch.wt[i].enabled = p->wt[i].enabled && p->op[i].wave == 0u && !p->pd[i].enabled && !p->ks[i].enabled;
        v->patch.wt[i].bank = v->patch.wt[i].enabled ? p->wt[i].bank : 0u;
        v->patch.wt[i].position_q15 = !v->patch.wt[i].enabled ? 0u
            : p->wt[i].position_q15 > 32768u ? 32768u : p->wt[i].position_q15;
        o->wt_on = v->patch.wt[i].enabled;
        o->wt_bank = 0; o->wt_frames = o->wt_length = 0u;
        o->wt_pos = v->patch.wt[i].position_q15;
        v->mod_wt[i] = 0;
        {   /* : on a plain sine only; the largest power of two in 64..8192 not above the request */
            const int on = p->ks[i].enabled && p->op[i].wave == 0u && !p->wt[i].enabled && !p->pd[i].enabled;
            const uint32_t want = p->ks[i].buffer;
            uint32_t len = FM_KS_BUFFER_MIN;
            while (len < FM_KS_BUFFER_MAX && 2u * len <= want) len *= 2u;
            v->patch.ks[i].enabled = (uint8_t)on;
            v->patch.ks[i].buffer = (uint16_t)(on ? len : 0u);
            v->patch.ks[i].loss_q15 = (uint16_t)(on ? (p->ks[i].loss_q15 < 32767u ? p->ks[i].loss_q15 : 32767u) : 0u);
            v->patch.ks[i].seed = on ? (p->ks[i].seed ? p->ks[i].seed : 0x9E3779B9u) : 0u;
            o->ks_on = (uint32_t)on;
            o->ks_len = v->patch.ks[i].buffer;
            o->ks_loss = v->patch.ks[i].loss_q15;
            o->ks_buf = 0; o->ks_w = o->ks_n = o->ks_f = o->ks_word = o->ks_prime = 0u;
            o->ks_inc = 0u; o->ks_prev = 0.0;
        }
        if (v->patch.eg[i].delay_ms > 60000u) v->patch.eg[i].delay_ms = 60000u;
        if (v->patch.eg[i].left_slope_db10 > 1200u) v->patch.eg[i].left_slope_db10 = 1200u;
        o->decay2_on = p->eg[i].decay2_ms != 0u || p->eg[i].sustain2_db10 != 0u;
        if (v->patch.eg[i].decay2_ms > 60000u) v->patch.eg[i].decay2_ms = 60000u;
        if (v->patch.eg[i].sustain2_db10 > 1200u) v->patch.eg[i].sustain2_db10 = 1200u;
        if (o->decay2_on) {
            const uint16_t s1 = p->op[i].sustain_db10 > 1200u ? 1200u : p->op[i].sustain_db10;
            if (v->patch.eg[i].sustain2_db10 < s1) v->patch.eg[i].sustain2_db10 = s1;
        }
        o->inc = o->gain = o->rate_g = 0.0; o->att = (double)FM_ATT_END_OCT;
        o->phase = (double)p->op[i].phase_u16 / 65536.0;   /*  */
        o->rate_d = o->rate_r = o->sus = 0.0;
        o->out = o->index = o->fb = o->prev = o->prev2 = 0.0;
        o->raw_prev = o->raw_prev2 = 0.0;
        o->stage = FM_STAGE_IDLE; o->count = o->count_d = o->count_a = 0; o->loop = 0u; o->loop_adopt = 0u;
        o->rate_attack_kept = 0u;
        o->count_a_wait = 0u;
        o->count_d2 = 0u; o->sus2 = o->rate_d2 = 0.0;
        o->iphase = (uint64_t)p->op[i].phase_u16 << 48; o->iinc = 0; o->noise = REF_NOISE_SEED[i];
        o->noise_index = 0u;
        if (p->op[i].free_run == 2u) {
            o->noise = ref_free_word(REF_NOISE_SEED[i], 0u);
            if (p->op[i].fixed_millihz) {
                o->inc = ref_inc(p->op[i].fixed_millihz, p->op[i].ratio_q16, v->fs);
                o->iinc = (uint64_t)ldexp(o->inc, 64);
            }
        }
        if (p->op[i].wave == 9u) ref_pink_reset(o, REF_NOISE_SEED[i], p->op[i].free_run == 2u);
        else if (p->op[i].wave == 10u || p->op[i].wave == 11u) o->colour = 0;
        for (j = 0; j < FM_OPS; j++) v->edge[i][j] = 0.0;
        v->vg[i] = v->ks[i] = 1.0;
    }
    v->ctl = v->lfo_phase = v->lfo_step = v->lfo_time = v->lfo_delay = v->lfo_fade = 0u;
    v->lfo_word = REF_LFO_SEED;
    v->lfo_a = v->lfo_b = v->lfo_cents = 0;
    v->freq_millihz = 0u;
    v->ops = p->route.ops <= FM_OPS ? p->route.ops : FM_OPS;
    v->carriers = v->ops ? p->route.carriers & ((1u << v->ops) - 1u) : 1u;
    v->amp_edges = v->sync_edges = v->sync_wrap_prev = 0u;
    for (i = 0; i < (int)v->ops; i++) for (j = 0; j < (int)v->ops; j++)
        if (i != j && v->patch.route.w[i][j]) {
            if (v->patch.route.kind[i][j] == 3u) { if (!v->op[i].ks_on) v->sync_edges |= 1u << (4 * j + i); }   /* : no wrap */
            else if (v->patch.route.kind[i][j]) v->amp_edges = 1u;
        }
    for (i = 0; i < 4; i++) {
        if (v->patch.peg.level_cents[i] < -4800) v->patch.peg.level_cents[i] = -4800;
        if (v->patch.peg.level_cents[i] > 4800) v->patch.peg.level_cents[i] = 4800;
        if (v->patch.peg.time_ms[i] > 60000u) v->patch.peg.time_ms[i] = 60000u;
    }
    v->peg_start = v->peg_target = v->peg_cur = 0;
    v->peg_n = v->peg_j = v->peg_stage = v->peg_mask = 0u;
    if (ref_peg_on(v)) {
        const uint32_t all = (1u << ref_ops_stepped(v)) - 1u;
        v->peg_mask = (v->patch.peg.mask ? v->patch.peg.mask : all) & all;
        for (i = 0; i < ref_ops_stepped(v); i++)
            if (v->patch.op[i].fixed_millihz != 0u) v->peg_mask &= ~(1u << i);
    }
    v->fmode = p->filter.mode <= 4u ? p->filter.mode : 0u;
    if (v->patch.fenv.attack_ms > 60000u) v->patch.fenv.attack_ms = 60000u;
    if (v->patch.fenv.decay_ms > 60000u) v->patch.fenv.decay_ms = 60000u;
    if (v->patch.fenv.release_ms > 60000u) v->patch.fenv.release_ms = 60000u;
    if (v->patch.fenv.sustain_permille > 1000u) v->patch.fenv.sustain_permille = 1000u;
    if (v->patch.fenv.depth_cents < -9600) v->patch.fenv.depth_cents = -9600;
    if (v->patch.fenv.depth_cents > 9600) v->patch.fenv.depth_cents = 9600;
    if (v->patch.fenv.track_permille > 2000u) v->patch.fenv.track_permille = 2000u;
    if (!v->patch.fenv.ref_millihz) v->patch.fenv.ref_millihz = 261626u;
    v->fe_start = v->fe_target = v->fe_e = 0;
    v->fe_n = v->fe_j = v->fe_stage = 0u;
    v->fe_s = (int32_t)(((uint64_t)v->patch.fenv.sustain_permille * 32768u + 500u) / 1000u);
    v->fe_track = 0;
    v->fe_q = p->filter.q_milli;
    v->fe_base = ref_filter_base(v, p->filter.cutoff_millihz);
    v->fic1 = v->fic2 = 0.0;
    v->f2ic1 = v->f2ic2 = 0.0; v->filter_idle = 0u;
    v->f2mode = v->patch.filter.stages == 2u ? v->fmode : 0u;
    ref_filter_coef(v, p->filter.cutoff_millihz, p->filter.q_milli);
    v->idle_samples = 0u; v->free_running = 0u; v->lfo_wrap = 0u;
    for (i = 0; i < ref_ops_stepped(v); i++)
        if (p->op[i].free_run == 2u) v->free_running = 1u;
    if (p->lfo.rate_millihz && p->lfo.free_run == 2u) {
        const uint64_t den = (uint64_t)sample_rate_hz * 1000u;
        const uint64_t r = p->lfo.rate_millihz < 10u ? 10u : p->lfo.rate_millihz > 50000u ? 50000u : p->lfo.rate_millihz;
        v->free_running = 1u; v->ctl = FM_CTL;
        v->lfo_step = den ? (uint32_t)((((r * FM_CTL) << 32) + den / 2u) / den) : 0u;
        v->lfo_phase = (uint32_t)p->lfo.phase_u16 << 16;
        ref_free_lfo_advance(v, 0u);
    }
    ref_formant_init(v);
    ref_mod_init(v);
    v->amp_gain = v->amp_rate_g = v->amp_rate_d = v->amp_rate_r = v->amp_sus = 0.0;   /*  */
    v->amp_att = (double)FM_ATT_END_OCT;
    v->amp_count = v->amp_count_d = 0u;
    v->amp_stage = FM_STAGE_IDLE;
}

/* : from the amp's own current gain whenever it is not idle ; a
 * running attack is kept . */
static void ref_amp_note_on(ref_voice_t *v)
{
    const fm_amp_patch_t *pp = &v->patch.amp;
    v->amp_sus     = ref_db10_to_oct(pp->sustain_db10);
    v->amp_count_d = ref_segment_samples(pp->decay_ms, v->fs_hz);
    v->amp_rate_d  = v->amp_sus / (double)v->amp_count_d;
    v->amp_rate_r  = (double)FM_REL_OCT / (double)ref_segment_samples(pp->release_ms, v->fs_hz);
    if (v->amp_stage != FM_STAGE_ATTACK) {
        const double g0 = v->amp_stage == FM_STAGE_IDLE ? 0.0 : exp2(-v->amp_att);
        v->amp_count  = ref_attack_samples(g0, ref_segment_samples(pp->attack_ms, v->fs_hz));
        v->amp_gain   = g0;
        v->amp_rate_g = (1.0 - g0) / (double)v->amp_count;
        v->amp_stage  = FM_STAGE_ATTACK;
    }
}

void ref_voice_note_on(ref_voice_t *v, uint32_t freq_millihz, uint16_t velocity_q15)
{
    const int retrig = ref_voice_sounding(v);
    const double vel = (velocity_q15 > 32767u ? 32767.0 : (double)velocity_q15) / 32767.0;
    const int nops = ref_ops_stepped(v);
    int i;
    ref_free_catchup(v);
    if (!retrig) v->sync_wrap_prev = 0u;   /* : only a note-on from silence forgets the last wraps */
    if (v->mod_active) {
        uint32_t j;
        const double cents = freq_millihz ? 1200.0 * log2((double)freq_millihz / 440000.0) : -9600.0;
        const int32_t rounded = cents < 0.0 ? -(int32_t)floor(-cents + 0.5) : (int32_t)floor(cents + 0.5);
        v->mod_velocity = (int32_t)floor(vel * 32768.0 + 0.5);
        v->mod_note = ref_mod_normalize(ref_mod_clamp(rounded, -9600, 9600), 9600u);
        if (!retrig) {
            for (j = 0; j < FM_MOD_SLOTS; j++) v->mod_cur[j] = 0;
            for (j = 0; j < FM_OPS; j++) v->mod_pitch[j] = v->mod_level[j] = v->mod_index[j] = v->mod_pd[j] = v->mod_wt[j] = 0;
            v->mod_cutoff = v->mod_q = 0;
        }
        ref_mod_lfo_reset(v);
        v->fe_base = ref_filter_base(v, v->patch.filter.cutoff_millihz);
        v->fe_q = v->patch.filter.q_milli;
    }
    for (i = 0; i < nops; i++) {
        const fm_op_patch_t *pp = &v->patch.op[i];
        ref_op_t *o = &v->op[i];
        const int delay = v->patch.eg[i].delay_ms && (!retrig || o->stage == FM_STAGE_DELAY);
        const double g0 = retrig ? ref_current_gain(o) : 0.0;
        const double sens = pp->vel_sens_q8 > 256u ? 256.0 : (double)pp->vel_sens_q8;
        const double vg = 1.0 - sens * (1.0 - vel) / 256.0;
        o->inc = ref_inc(pp->fixed_millihz ? pp->fixed_millihz : freq_millihz, pp->ratio_q16, v->fs);   /*  */
        o->iinc = (uint64_t)ldexp(o->inc, 64);   /* at most 2^63; render_ref adopts the engine's */
        if (!retrig) {
            o->prev = o->prev2 = 0.0;
            o->raw_prev = o->raw_prev2 = 0.0;
            if (!pp->free_run) {                 /* : FREE keeps the phase and the noise word */
                o->phase = (double)pp->phase_u16 / 65536.0;
                o->iphase = (uint64_t)pp->phase_u16 << 48;
                o->noise = REF_NOISE_SEED[i];
                if (pp->wave == 9u) ref_pink_reset(o, REF_NOISE_SEED[i], 0);
                else if (pp->wave == 10u || pp->wave == 11u) o->colour = 0;
            }
        }
        o->sus     = ref_db10_to_oct(pp->sustain_db10);
        o->count_d = ref_rate_scaled(ref_segment_samples(pp->decay_ms, v->fs_hz), freq_millihz, pp->rate_scale);
        o->rate_d  = o->sus / (double)o->count_d;
        o->rate_r  = (double)FM_REL_OCT
                   / (double)ref_rate_scaled(ref_segment_samples(pp->release_ms, v->fs_hz), freq_millihz, pp->rate_scale);
        if (o->decay2_on) {
            const uint32_t ms = v->patch.eg[i].decay2_ms;
            o->sus2 = ref_db10_to_oct(v->patch.eg[i].sustain2_db10);
            o->count_d2 = ms ? ref_rate_scaled(ref_segment_samples(ms, v->fs_hz), freq_millihz, pp->rate_scale) : 0u;
            o->rate_d2 = o->count_d2 ? (o->sus2 - o->sus) / (double)o->count_d2 : 0.0;
        }
        o->count_a = ref_rate_scaled(ref_segment_samples(pp->attack_ms, v->fs_hz), freq_millihz, pp->rate_scale);
        o->loop    = v->patch.ext[i].loop;
        o->loop_adopt = 0u;
        o->rate_attack_kept = retrig && o->stage == FM_STAGE_ATTACK;
        if (!o->rate_attack_kept) {    /* : a running attack is kept */
            o->count  = ref_attack_samples(g0, o->count_a);
            o->gain   = g0;
            o->rate_g = (1.0 - g0) / (double)o->count;
            o->stage  = FM_STAGE_ATTACK;
            if (delay) {
                o->count_a_wait = o->count;
                o->count = ref_segment_samples(v->patch.eg[i].delay_ms, v->fs_hz);
                o->stage = FM_STAGE_DELAY;
            }
        }
        v->vg[i]   = vg;                       /* : the gains a tick applies again */
        v->ks[i]   = v->patch.eg[i].left_slope_db10 && freq_millihz < pp->ks_break_millihz
                   ? ref_keyscale(pp->ks_break_millihz, freq_millihz, v->patch.eg[i].left_slope_db10)
                   : ref_keyscale(freq_millihz, pp->ks_break_millihz, pp->ks_slope_db10);
        v->ks_db10[i] = v->ks_boost[i] = 0.0;
        if (v->patch.ext[i].ks_target || v->patch.ext[i].ks_curve) ref_ks_ext(v, (int)i, freq_millihz);
        o->out     = ref_level(v, (int)i, (double)pp->level_db10) * vg;
        o->index   = ref_index(v, (int)i, (double)pp->index_q10);
        o->fb      = ((double)pp->feedback_q10 / 1024.0) / (2.0 * PI);
    }
    v->freq_millihz = freq_millihz;
    ref_set_edges(v);
    if (ref_peg_on(v)) {
        if (!retrig) v->peg_cur = v->patch.peg.level_cents[3];
        ref_peg_enter(v, 1u);
    }
    if (ref_fenv_on(v)) {
        const uint32_t na = ref_ctl_ticks(v->patch.fenv.attack_ms, v->fs_hz);
        if (!retrig) {
            v->fe_e = 0;
            ref_fenv_enter(v, 1u, na);
        } else if (v->fe_stage != 1u) {
            const uint32_t nr = na - (uint32_t)(((uint64_t)v->fe_e * na) >> 15);
            ref_fenv_enter(v, 1u, nr);
        }
        v->fe_track = ref_track_cents(v, freq_millihz);
        v->fe_base = ref_filter_base(v, v->patch.filter.cutoff_millihz);
        v->fe_q = v->patch.filter.q_milli;
        if (!v->mod_active) ref_fenv_apply(v);
    }
    for (i = 0; i < FM_FORMANTS; i++) if ((v->formant_mask >> i) & 1u) {
        v->formant[i].idle = 0;
        if (!v->mod_active) ref_formant_apply(v, (unsigned)i);
    }
    if (v->patch.lfo.rate_millihz != 0u) ref_lfo_start(v);   /* : at every note-on */
    else if (ref_peg_on(v) || ref_fenv_on(v) || v->mod_active) ref_ctl_tick(v, 1 | REF_MOD_NOTE);
    if (!v->mod_active && v->fmode && !ref_fenv_on(v)) ref_filter_coef(v, v->patch.filter.cutoff_millihz, v->patch.filter.q_milli);
    v->filter_idle = 0u;
    if (v->patch.amp.enabled) ref_amp_note_on(v);   /*  */
    for (i = 0; i < nops; i++) {                     /* : every note-on refills; only silence reseeds */
        ref_op_t *o = &v->op[i];
        if (!o->ks_buf) continue;
        if (!retrig || !o->ks_word) {
            o->ks_word = v->patch.ks[i].seed ^ REF_NOISE_SEED[i];
            if (!o->ks_word) o->ks_word = 0x9E3779B9u;
        }
        ref_ks_fill(o);
    }
}

void ref_voice_pitch(ref_voice_t *v, uint32_t freq_millihz)
{
    const int nops = ref_ops_stepped(v);
    int i;
    ref_free_catchup(v);
    v->freq_millihz = freq_millihz;
    for (i = 0; i < nops; i++) {
        if (v->patch.op[i].fixed_millihz) continue;   /* : a fixed operator keeps its frequency */
        if ((v->peg_mask >> i) & 1u) continue;   /* : applied below with both cached offsets */
        if (v->ctl != 0u && v->patch.lfo.rate_millihz != 0u && ((v->patch.lfo.mask_pitch >> i) & 1u))   /*  */
            v->op[i].inc = ref_inc_cents(freq_millihz, v->patch.op[i].ratio_q16, v->lfo_cents, v->fs);
        else v->op[i].inc = ref_inc(freq_millihz, v->patch.op[i].ratio_q16, v->fs);
        v->op[i].iinc = (uint64_t)ldexp(v->op[i].inc, 64);
    }
    if (v->mod_active) ref_mod_pitch_apply(v);
    else ref_peg_pitch(v);
}

void ref_voice_note_off(ref_voice_t *v)
{
    const int nops = ref_ops_stepped(v);
    int i;
    for (i = 0; i < nops; i++) {
        ref_op_t *o = &v->op[i];
        if (o->stage == FM_STAGE_DELAY) {
            o->stage = FM_STAGE_IDLE; o->gain = 0.0; o->att = (double)FM_ATT_END_OCT;
            o->count = o->count_a_wait = 0u;
            continue;
        }
        if (o->stage == FM_STAGE_ATTACK) {
            o->att = (o->gain > 0.0) ? -log2(o->gain) : (double)FM_ATT_END_OCT;
            if (o->att > (double)FM_ATT_END_OCT) o->att = (double)FM_ATT_END_OCT;
            if (o->att < 0.0) o->att = 0.0;
        }
        if (o->stage != FM_STAGE_IDLE) o->stage = FM_STAGE_RELEASE;
    }
    if (v->patch.amp.enabled) {                      /* : the operators' conversion */
        if (v->amp_stage == FM_STAGE_ATTACK) {
            v->amp_att = (v->amp_gain > 0.0) ? -log2(v->amp_gain) : (double)FM_ATT_END_OCT;
            if (v->amp_att > (double)FM_ATT_END_OCT) v->amp_att = (double)FM_ATT_END_OCT;
            if (v->amp_att < 0.0) v->amp_att = 0.0;
        }
        if (v->amp_stage != FM_STAGE_IDLE) v->amp_stage = FM_STAGE_RELEASE;
    }
    if (v->peg_stage >= 1u && v->peg_stage <= 4u) {
        const int32_t before = v->peg_cur;
        ref_peg_enter(v, 5u);
        if (v->peg_cur != before) {
            if (v->mod_active) ref_mod_pitch_apply(v);
            else ref_peg_pitch(v);
        }
    }
    if (v->fe_stage >= 1u && v->fe_stage <= 3u) {
        const uint32_t nr = ref_ctl_ticks(v->patch.fenv.release_ms, v->fs_hz);
        ref_fenv_enter(v, 4u, nr);
        if (nr == 0u) ref_fenv_apply(v);
    }
}

void ref_voice_filter(ref_voice_t *v, uint32_t cutoff_millihz, uint16_t q_milli)
{
    if (v->mod_active && v->fmode) {
        v->fe_base = ref_filter_base(v, cutoff_millihz);
        v->fe_q = q_milli; ref_mod_filter(v);
    }
    else if (ref_fenv_on(v)) {
        v->fe_base = ref_filter_base(v, cutoff_millihz);
        v->fe_q = q_milli;
        ref_fenv_apply(v);
    } else if (v->fmode) ref_filter_coef(v, cutoff_millihz, q_milli);
}

/* 's feedback source, exact: the mean of the last two outputs, or the last. */
static double ref_fb_src(const ref_op_t *o)
{
#if FM_FB_AVG
    return 0.5 * (o->prev + o->prev2);
#else
    return o->prev;
#endif
}

/* 's typed own feedback: bit 0 takes 's source from the samples
 * before the envelope, bit 1 squares it after the mean, at most the
 * engine's 2^31 - 1 in Q2.30. Type 0 is 's source. */
static double ref_fb_typed(const ref_op_t *o, uint32_t type)
{
    const double lim = 2147483647.0 / 1073741824.0;
    double src;
#if FM_FB_AVG
    src = type & 1u ? 0.5 * (o->raw_prev + o->raw_prev2) : ref_fb_src(o);
#else
    src = type & 1u ? o->raw_prev : ref_fb_src(o);
#endif
    if (type & 2u) src = src * src > lim ? lim : src * src;
    return src;
}

/* : one sample of a routed voice, op ops-1 down to op 0, as the
 * engine's fm_voice_step_routed; the carriers' sum clamped at the engine's
 * +-(2^31 - 1) in Q2.30. Returns the voice's output before the filter, or
 * 0 with *idle set when every carrier is idle. 's sync reads the
 * integer phase: a source's wrap is its iphase falling across its own add,
 * seen by a target below it in this sample and above it in the next
 * sounding one; the target restarts at its PHASE_U16 before its add. */
static double ref_routed(ref_voice_t *v, int *idle)
{
    const double lim = 2147483647.0 / 1073741824.0;
    const uint32_t n = v->ops;
    double pm, y = 0.0;
    uint32_t i, j, wrapped = 0u;
    *idle = !ref_voice_sounding(v);
    if (*idle) return 0.0;
    for (i = n; i-- > 0; ) {
        ref_op_t *o = &v->op[i];
        double senv;
        uint64_t start = 0u;
        pm = ref_fb_typed(o, v->patch.ext[i].fb_type) * o->fb;
        for (j = 0; j < n; j++)
            if (v->edge[j][i] != 0.0) pm += (j > i ? v->op[j].prev : ref_fb_src(&v->op[j])) * v->edge[j][i];
        if (v->sync_edges) {
            for (j = 0; j < n; j++)
                if (j != i && ((v->sync_edges >> (4u * i + j)) & 1u) && (((j > i ? wrapped : v->sync_wrap_prev) >> j) & 1u)) {
                    o->phase = (double)v->patch.op[i].phase_u16 / 65536.0;
                    o->iphase = (uint64_t)v->patch.op[i].phase_u16 << 48;
                }
            start = o->iphase;
        }
        senv = ref_op_step(o, pm, v->patch.op[i].wave, v->patch.op[i].free_run == 2u ? REF_NOISE_SEED[i] : 0u, v->patch.noise[i].shift);
        if (v->sync_edges && o->iphase < start) wrapped |= 1u << i;
        if (v->amp_edges) {
            for (j = 0; j < n; j++) {
                const uint32_t kind = v->patch.route.kind[j][i], weight = v->patch.route.w[j][i];
                if (j != i && (kind == 1u || kind == 2u) && weight) {
                    double source = j > i ? v->op[j].prev : ref_fb_src(&v->op[j]);
                    double gain;
                    if (source > 1.0) source = 1.0;
                    else if (source < -1.0) source = -1.0;
                    gain = kind == 1u ? 1.0 - (double)weight / 256.0 * (1.0 - (1.0 + source) / 2.0)
                                      : 1.0 + (double)weight / 256.0 * (source - 1.0);
                    senv *= gain;
                    if (senv > 1.0) senv = 1.0;
                    else if (senv < -1.0) senv = -1.0;
                }
            }
            o->prev = senv;
        }
        if (!v->patch.vector.enabled && ((v->carriers >> i) & 1u)) y += senv * o->out;
    }
#if FM_OPS >= 4
    if (v->patch.vector.enabled) {
        const double x = v->vector_x / 32768.0;
        const double z = v->vector_y / 32768.0;
        y = v->op[0].prev * v->op[0].out * (1.0 - x) * (1.0 - z)
          + v->op[1].prev * v->op[1].out * x * (1.0 - z)
          + v->op[2].prev * v->op[2].out * (1.0 - x) * z
          + v->op[3].prev * v->op[3].out * x * z;
    }
#endif
    if (v->sync_edges) v->sync_wrap_prev = wrapped;
    return y > lim ? lim : y < -lim ? -lim : y;
}

/* : real transfer functions, independent of the integer rounding. */
static double ref_shape(const ref_voice_t *v, double x)
{
    double z, a;
    if (!v->patch.shape.kind) return x;
    z = x * v->shape_gain;
    if (v->patch.shape.kind == 3u) {
        const double u = z + 1.0 - 4.0 * floor((z + 1.0) / 4.0);
        return 1.0 - fabs(u - 2.0);
    }
    a = fabs(z);
    if (a > 1.0) a = 1.0;
    if (v->patch.shape.kind == 4u && z < 0.0 && a > 0.5) a = 0.5;     /* : +1 and -1/2 */
    if (v->patch.shape.kind == 2u) a = (3.0 * a - a * a * a) / 2.0;
    return z < 0.0 ? -a : a;
}

/* : the amp envelope reaching idle ends the voice, as the engine's
 * does: every operator idle, the filter and formant states cleared. */
static void ref_amp_end(ref_voice_t *v)
{
    int i;
    for (i = 0; i < FM_OPS; i++) {
        ref_op_t *o = &v->op[i];
        o->stage = FM_STAGE_IDLE; o->gain = 0.0; o->att = (double)FM_ATT_END_OCT;
        o->count = o->count_a_wait = 0u;
    }
    v->fic1 = v->fic2 = v->f2ic1 = v->f2ic2 = 0.0;
    for (i = 0; i < FM_FORMANTS; i++) v->formant[i].ic1 = v->formant[i].ic2 = 0.0;
}

/* : ref_env_step's subset without delay and decay2. */
static double ref_amp_step(ref_voice_t *v)
{
    switch (v->amp_stage) {
    case FM_STAGE_ATTACK:
        if (--v->amp_count == 0u) {
            v->amp_gain = 1.0; v->amp_att = 0.0; v->amp_stage = FM_STAGE_DECAY; v->amp_count = v->amp_count_d;
        } else v->amp_gain += v->amp_rate_g;
        return v->amp_gain;
    case FM_STAGE_DECAY:
        if (--v->amp_count == 0u) { v->amp_att = v->amp_sus; v->amp_stage = FM_STAGE_SUSTAIN; }
        else v->amp_att += v->amp_rate_d;
        break;
    case FM_STAGE_RELEASE:
        v->amp_att += v->amp_rate_r;
        if (v->amp_att >= (double)FM_ATT_END_OCT) {
            v->amp_att = (double)FM_ATT_END_OCT; v->amp_stage = FM_STAGE_IDLE; ref_amp_end(v);
        }
        break;
    case FM_STAGE_IDLE:
        return 0.0;
    default:
        break;
    }
    return exp2(-v->amp_att);
}

/* : the shaper's place, before (pos 1) or after (pos 0) the formant
 * bank and the filter. An idle step ignores its input, and every kind maps
 * 0 to 0, so the pre position adds nothing to a ring-out.
 * : the amp envelope, when enabled, scales the result last. */
static double ref_out(ref_voice_t *v, double y, int idle)
{
    const double r = v->patch.shape.pos ? ref_signal(v, ref_shape(v, y), idle) : ref_shape(v, ref_signal(v, y, idle));
    return v->patch.amp.enabled ? r * ref_amp_step(v) : r;   /* , the ring-out included */
}

void ref_voice_ctl(ref_voice_t *v, uint32_t n, int32_t value_q15)
{
    if (n < FM_CTL_INPUTS) v->ctl_input[n] = ref_mod_clamp(value_q15, -32768, 32768);
}

void ref_voice_gen(ref_voice_t *v, uint32_t n, int32_t value_q15)
{
    if (n < FM_GEN_INPUTS) v->gen_input[n] = ref_mod_clamp(value_q15, -32768, 32768);
}

void ref_render(ref_voice_t *v, double *out, uint32_t frames)
{
    ref_op_t *c = &v->op[0];
    ref_op_t *m = &v->op[1];
    uint32_t n;
    if (v->free_running && v->idle_samples > UINT64_MAX - frames) ref_free_catchup(v);
    for (n = 0; n < frames; n++) {
        double senv_m, senv_c;
        if (v->ops) {
            int idle;
            const double y = ref_routed(v, &idle);
            if (idle && v->free_running) v->idle_samples++;
            if (!idle && v->patch.lfo.free_run == 2u && v->lfo_time < UINT32_MAX) v->lfo_time++;
            if (!idle && v->ctl != 0u && --v->ctl == 0u) ref_ctl_tick(v, ref_w4_step(v));
            out[n] = ref_out(v, y, idle) * 32768.0;
            continue;
        }
        if (c->stage == FM_STAGE_IDLE) {
            if (v->free_running) v->idle_samples++;
            out[n] = ref_out(v, 0.0, 1) * 32768.0; continue;
        }
        senv_m = ref_op_step(m, ref_fb_typed(m, v->patch.ext[1].fb_type) * m->fb, v->patch.op[1].wave,
                            v->patch.op[1].free_run == 2u ? REF_NOISE_SEED[1] : 0u, v->patch.noise[1].shift);
        senv_c = ref_op_step(c, senv_m * m->index, v->patch.op[0].wave,
                            v->patch.op[0].free_run == 2u ? REF_NOISE_SEED[0] : 0u, v->patch.noise[0].shift);
        {
            const double y = senv_c * c->out;   /* a level LFO changes out at the tick */
            if (v->patch.lfo.free_run == 2u && v->lfo_time < UINT32_MAX) v->lfo_time++;
            if (v->ctl != 0u && --v->ctl == 0u) ref_ctl_tick(v, ref_w4_step(v));
            out[n] = ref_out(v, y, 0) * 32768.0;
        }
    }
}

void ref_voice_bind_assets(ref_voice_t *v, const fm_assets_t *a)
{
    int i;
    for (i = 0; i < FM_OPS; i++) {
        ref_op_t *o = &v->op[i];
        const uint32_t slot = v->patch.wt[i].bank;
        o->wt_bank = 0;
        if (a && o->wt_on && slot < a->wt_count) {
            o->wt_bank = a->wt[slot].data;
            o->wt_frames = a->wt[slot].frames;
            o->wt_length = a->wt[slot].length;
        }
    }
}
