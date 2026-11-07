/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * fm_a1.c — integer FM voice with two to FM_OPS operators.
 * See fm_a1.h for configuration, state and rendering contracts.
 */
#include "fm_a1.h"
#include "fm_tables.h"
#include "../../vendor/fp_math.h"

#if FM_TABLES_SINE_BITS != FM_SINE_BITS || FM_TABLES_EXP_BITS != FM_EXP_BITS
#error "generated fm_tables.h does not match FM_SINE_BITS / FM_EXP_BITS"
#endif

/* The sample path relies on sign-propagating (arithmetic) right shift of
 * negative signed values, which C99 leaves implementation-defined. Every
 * supported compiler does it; this fails the build where one does not . */
typedef char fm_assert_asr32[((-1) >> 1) == -1 ? 1 : -1];
typedef char fm_assert_asr64[(((int64_t)-1) >> 1) == -1 ? 1 : -1];

/* C99 6.3.1.3p3: signed narrowing must keep the low bits. This is
 * required by fm_free_catchup, the pitch-envelope bases and fm_base_inc
 * through the vendored fp_w_to_i64 conversion; reject other mappings. */
typedef char fm_assert_narrow32[(int32_t)0xFFFFFFFFu == -1 ? 1 : -1];
typedef char fm_assert_narrow64[(int64_t)0xFFFFFFFFFFFFFFFFull == -1 ? 1 : -1];
typedef char fm_assert_int32[sizeof(int) >= 4 ? 1 : -1];
#if FP_MATH_HAS_INT128
typedef char fm_assert_narrow128[(int64_t)((((fp_i128)1) << 64) - 1) == -1 ? 1 : -1];
#endif

#if FM_ENV64
typedef uint64_t fm_env_t;
#define FM_ENV_FRAC 56
#else
typedef uint32_t fm_env_t;
#define FM_ENV_FRAC 24
#endif
#define FM_ATT_END       ((fm_env_t)FM_ATT_END_OCT << FM_ENV_FRAC)
#define FM_GAIN_PEAK     ((uint64_t)1 << 62)
#define FM_SINE_MASK     ((1u << FM_SINE_BITS) - 1u)
#define FM_SINE_FRAC     (32 - FM_SINE_BITS)
#define FM_EXP_MASK      ((1u << FM_EXP_BITS) - 1u)
#define FM_EXP_FRAC      (24 - FM_EXP_BITS)
#if FM_PHASE64
#define FM_PHASE_U16(x)  ((uint64_t)(x) << 48)     /* 's start phase, 1/65536 turns, as a phase */
#else
#define FM_PHASE_U16(x)  ((uint32_t)(x) << 16)
#endif

/* 10 * 20*log10(2) = 60.2059991327962... tenths of a dB per octave, Q16.48
 * (round(60.20599913279623904 * 2^48), computed with 60-digit decimals). */
static const fixed_t FM_DB10_PER_OCT_Q48 = 16946482203745597LL;

/* 's noise seeds, op[0] and op[1] (provisional, ), then op[2] and
 * op[3] (provisional, ): ops 2k and 2k + 1 take the high and low
 * halves of (k + 1) * 0x9E3779B97F4A7C15 modulo 2^64, a rule that extends
 * the table if a D row raises 's 4. */
static const uint32_t FM_NOISE_SEED[4] = { 0x9E3779B9u, 0x7F4A7C15u, 0x3C6EF372u, 0xFE94F82Au };

/* 's LFO word (provisional): the rule's next value, the high half of
 * 3 * 0x9E3779B97F4A7C15 modulo 2^64. */
static const uint32_t FM_LFO_SEED = 0xDAA66D2Cu;

/* 's ring-out (provisional, ): the states are zeroed once both lie
 * within FM_SVF_QUIET or after FM_SVF_RING samples with every carrier idle. */
#define FM_SVF_QUIET 2048u
#define FM_SVF_RING  (1u << 18)

/* ------------------------------------------------------------------ */
/*  Sample path                                                        */
/* ------------------------------------------------------------------ */

#define FM_CHG_PEG 1u
#define FM_MOD_NOTE 4u /* note evaluation samples only shift-zero slots */
#define FM_LFO_HOLD 2u  /* FREE 2 note-on applies without moving its grid */
static unsigned fm_w4_step(fm_voice_t *v); /* : step only on grid ticks */
static void fm_ctl_tick(fm_voice_t *v, unsigned changed); /*  control tier */
static void fm_mod_filter(fm_voice_t *v, int force);
static void fm_mod_pitch_apply(fm_voice_t *v);
static void fm_free_catchup(fm_voice_t *v); /* event/block tier, never an idle tick */
static void fm_assets_clear(fm_voice_t *v);

/* sin(2*pi*p / 2^32) in Q2.30, table with linear interpolation. */
static inline int32_t fm_sine_q30(uint32_t p)
{
    uint32_t i    = p >> FM_SINE_FRAC;
    uint32_t frac = p & ((1u << FM_SINE_FRAC) - 1u);
    int32_t  a    = fm_sine_tbl[i];
    int32_t  b    = fm_sine_tbl[(i + 1u) & FM_SINE_MASK];
    return a + (int32_t)(((int64_t)(b - a) * (int64_t)frac) >> FM_SINE_FRAC);
}

/* 2^(-att) in Q2.30 for att in Q8.24 octaves; 0 at 31 octaves and beyond. */
static inline uint32_t fm_exp2_q30(uint32_t att)
{
    uint32_t oct  = att >> 24;
    uint32_t i    = (att >> FM_EXP_FRAC) & FM_EXP_MASK;
    uint32_t frac = att & ((1u << FM_EXP_FRAC) - 1u);
    int32_t  a, b, g;
    if (oct >= 31u) return 0u;
    a = fm_exp2_tbl[i];
    b = fm_exp2_tbl[i + 1u];
    g = a + (int32_t)(((int64_t)(b - a) * (int64_t)frac) >> FM_EXP_FRAC);
    return (uint32_t)g >> oct;
}

/* Gain in Q2.30 for an envelope attenuation. */
static inline uint32_t fm_att_gain_q30(fm_env_t att)
{
#if FM_ENV64
    return fm_exp2_q30((uint32_t)(att >> 32));
#else
    return fm_exp2_q30(att);
#endif
}

/*  : where a held looping operator would enter SUSTAIN, it
 * enters its attack again from the sustain gain, with the count and step
 * set at note-on. The transition sample still returns the sustain gain. */
static inline void fm_env_loop(fm_op_t *o)
{
    o->stage = FM_STAGE_ATTACK; o->gain = o->loop_g0; o->count = o->loop_count; o->rate_g = o->loop_rate;
}

/* One envelope step; returns this sample's gain in Q2.30. The attack rises
 * linearly in amplitude on the 64-bit gain accumulator; decay and release
 * move linearly in octaves. Attack and decay run for a fixed sample count and
 * land exactly on their target, so the transition sample is the same in the
 * engine and in the reference by construction. Release runs until the end
 * attenuation; an idle operator is silent. */
static inline uint32_t fm_env_step(fm_op_t *o)
{
    fm_env_t a;
    /* Two bounded groups prevent an inlined seven-way jump table from
     * importing a wide-offset dispatch helper on small Thumb targets. */
    if (o->stage & 4u) {
        if (o->stage == FM_STAGE_DELAY) {
            if (--o->count == 0u) { o->stage = FM_STAGE_ATTACK; o->count = o->count_a_wait; }
            return 0u;     /* the boundary sample is still the final silent delay sample */
        }
        if (o->stage == FM_STAGE_DECAY2) {
            if (--o->count == 0u) { o->att = o->sus2; o->stage = FM_STAGE_SUSTAIN; if (o->loop_count) fm_env_loop(o); }
            else {
                a = o->att + o->rate_d2;
                o->att = a < o->sus2 ? a : o->sus2;
            }
        } else if (o->stage == FM_STAGE_RELEASE) {
            a = o->att + o->rate_r;
            if (a >= FM_ATT_END) { a = FM_ATT_END; o->stage = FM_STAGE_IDLE; }
            o->att = a;
        }
    } else {
        if (o->stage == FM_STAGE_IDLE) return 0u;
        if (o->stage == FM_STAGE_ATTACK) {
            o->count--;
            if (o->count == 0u) { o->gain = FM_GAIN_PEAK; o->att = 0u; o->stage = FM_STAGE_DECAY; o->count = o->count_d; }
            else o->gain += o->rate_g;
            return (uint32_t)(o->gain >> 32);
        }
        if (o->stage == FM_STAGE_DECAY) {
            o->count--;
            if (o->count == 0u) {
                o->att = o->sus; o->stage = FM_STAGE_SUSTAIN;
                if (o->decay2_on) {
                    if (o->count_d2) { o->stage = FM_STAGE_DECAY2; o->count = o->count_d2; }
                    else o->att = o->sus2;
                }
                if (o->loop_count && o->stage == FM_STAGE_SUSTAIN) fm_env_loop(o);
            } else o->att += o->rate_d;
        }
    }
    return fm_att_gain_q30(o->att);
}

/* Q8.24 turns -> phase units (turns * 2^32, modulo 2^32). Unsigned shift: UB-free. */
static inline uint32_t fm_turns_to_phase(int32_t t_q24)
{
    return (uint32_t)(((uint64_t)(int64_t)t_q24) << 8);
}

/* 's noise: one xorshift32 step. */
static inline uint32_t fm_noise_step(uint32_t x)
{
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    return x;
}

/*  FREE 2: a permutation of the 32-bit counter, so idle catch-up
 * needs no random draws. The seed and each product are modulo 2^32. */
static uint32_t fm_free_word(uint32_t seed, uint32_t index)
{
    uint32_t x = seed + index * 0x9E3779B9u;
    x ^= x >> 16; x *= 0x85EBCA6Bu;
    x ^= x >> 13; x *= 0xC2B2AE35u;
    return x ^ (x >> 16);
}

static int32_t fm_noise_row(uint32_t word)
{
    return ((int32_t)((word >> 1) ^ 0x40000000u) - 0x40000000) >> 4;
}

static void fm_pink_draw(fm_op_t *o, uint32_t free_seed)
{
    o->noise = free_seed ? fm_free_word(free_seed, ++o->noise_index) : fm_noise_step(o->noise);
}

static void fm_pink_reset(fm_op_t *o, uint32_t seed, int continuous)
{
    unsigned j;
    o->noise = seed; o->noise_index = 0u;
    o->noise_state.pink.sum = 0; o->noise_state.pink.counter = 0u;
    for (j = 0; j < 8u; j++) {
        fm_pink_draw(o, continuous ? seed : 0u);
        o->noise_state.pink.row[j] = fm_noise_row(o->noise);
        o->noise_state.pink.sum += o->noise_state.pink.row[j];
    }
    fm_pink_draw(o, continuous ? seed : 0u);
}

/* At most seven shifts; counter zero draws only the fresh word. */
static void fm_pink_tick(fm_op_t *o, uint32_t free_seed)
{
    uint32_t c = (o->noise_state.pink.counter + 1u) & 255u;
    o->noise_state.pink.counter = c;
    if (c) {
        unsigned j = 0;
        int32_t row;
        while (!(c & 1u)) { c >>= 1; j++; }
        fm_pink_draw(o, free_seed); row = fm_noise_row(o->noise);
        o->noise_state.pink.sum += row - o->noise_state.pink.row[j];
        o->noise_state.pink.row[j] = row;
    }
    fm_pink_draw(o, free_seed);
}

/* FREE 2 reconstructs each row's last replacement, without iterating edges.
 * An edge draws twice except counter zero, which draws once. Keep the wide
 * edge count until division by 256: its high bits affect the draw counter. */
static void fm_pink_skip(fm_op_t *o, uint32_t seed, uint64_t edges)
{
    const uint32_t old = o->noise_state.pink.counter;
    const uint32_t low = (uint32_t)edges & 255u;
    const uint32_t wraps = (uint32_t)(edges >> 8) + ((old + low) >> 8);
    const uint32_t now = (old + low) & 255u;
    unsigned j;
    o->noise_index += 2u * (uint32_t)edges - wraps;
    o->noise_state.pink.counter = now;
    o->noise_state.pink.sum = 0;
    for (j = 0; j < 8u; j++) {
        const uint32_t age = (now - (1u << j)) & ((1u << (j + 1u)) - 1u);
        if (edges > age) {
            const uint32_t after = 2u * age - (age > now);
            o->noise_state.pink.row[j] = fm_noise_row(fm_free_word(seed, o->noise_index - after - 1u));
        }
        o->noise_state.pink.sum += o->noise_state.pink.row[j];
    }
    o->noise = fm_free_word(seed, o->noise_index);
}

/* One operator, one sample: advance phase, look up the waveform at phase +
 * pm, advance envelope, shape. Returns the envelope-shaped wave in Q2.30.
 * 's shapes read the sine table at the modulated phase p: half is 0
 * while p's top bit is set, absolute is abs(sin), quarter is sin of p's
 * first quarter turn while bit 30 is clear and 0 while it is set. Noise
 * holds its word, stepped in each sample where the unmodulated phase
 * changes its top bit (twice the operator's frequency), and ignores pm. */
/* The cached Q31 slope can reach 2^45. Each multiplication below is
 * 32x32->64; the exact result is floor(distance*slope/2^31). */
static inline uint32_t fm_pd_warp(const fm_op_t *o, uint32_t p)
{
    const uint32_t knee = o->pd_knee << 17;
    const unsigned upper = p >= knee;
    const uint32_t distance = upper ? p - knee : p;
    const uint64_t slope = o->pd_slope[upper];
    const uint64_t low = ((uint64_t)distance * (uint32_t)slope) >> 31;
    const uint64_t high = ((uint64_t)distance * (uint32_t)(slope >> 32)) << 1;
    return (upper ? 0x80000000u : 0u) + (uint32_t)(low + high);
}

/* : one Q1.15 frame read at phase p in Q2.30. The top bits select
 * sample j, the next 16 bits interpolate toward sample (j+1) mod length;
 * the numerator stays below 2^32 in magnitude, and (num+1)>>1 rounds ties
 * toward positive infinity, so the result lies in [-2^30, 2^30). */
static inline int32_t fm_wt_sample(const int16_t *t, uint32_t p, uint32_t bits)
{
    const uint32_t j = p >> (32u - bits);
    const int32_t f = (int32_t)((p >> (16u - bits)) & 0xFFFFu);
    const int64_t num = (int64_t)t[j] * (65536 - f) + (int64_t)t[(j + 1u) & ((1u << bits) - 1u)] * f;
    return (int32_t)((num + 1) >> 1);
}

/* : the frame crossfade, two signed 32x32->64 products below 2^46
 * and one narrowing, rounding ties up; a zero fraction reads one frame,
 * which the same formula returns exactly. Unbound is silent. */
static inline int32_t fm_wt_q30(const fm_op_t *o, uint32_t p)
{
    int32_t a;
    if (!o->wt_a) return 0;
    a = fm_wt_sample(o->wt_a, p, o->wt_bits);
    if (!o->wt_frac) return a;
    return (int32_t)(((int64_t)a * (int32_t)(32768u - o->wt_frac)
                    + (int64_t)fm_wt_sample(o->wt_b, p, o->wt_bits) * (int32_t)o->wt_frac
                    + 16384) >> 15);
}

/* : the band's frames for the position: z = P*(frames-1), frame z>>15,
 * fraction z&32767, so P=32768 is the last frame exactly. Control tier. */
static void fm_wt_point(fm_op_t *o)
{
    uint32_t z;
    if (!o->wt_bank) { o->wt_a = o->wt_b = 0; o->wt_frac = 0u; return; }
    z = o->wt_pos * (o->wt_frames - 1u);
    o->wt_frac = z & 32767u;
    o->wt_a = o->wt_bank + ((o->wt_band * o->wt_frames + (z >> 15)) << o->wt_bits);
    o->wt_b = o->wt_frac ? o->wt_a + (1u << o->wt_bits) : o->wt_a;
}

/* : band k serves fundamentals strictly below fs/2^(k+1); take the
 * highest such k up to 11, else 0. With inc in turns * 2^W the test is
 * inc < 2^(W-1-k), exact on inc's top 12 bits; a loop, not a count of
 * leading zeros, keeps the targets' imports. Called after inc changes. */
static void fm_wt_band(fm_op_t *o)
{
#if FM_PHASE64
    const uint32_t top = (uint32_t)(o->inc >> 52);
#else
    const uint32_t top = o->inc >> 20;
#endif
    uint32_t k = FM_WT_BANDS - 1u;
    while (k && top >= (1u << (FM_WT_BANDS - 1u - k))) k--;
    if (k != o->wt_band) { o->wt_band = k; fm_wt_point(o); }
}

/*  Karplus-Strong (design-hybrid section 5). The loop period is
 * 2^P/I samples for increment I in turns * 2^P; the two-point average
 * adds half a sample, so the read delay is 2^P/I - 0.5. It clamps to
 * length-2 for I = 0 or I*(2*length-3) <= 2^(P+1), to 2 for
 * 5*I >= 2^(P+1); otherwise floor(2^(P+48)/I) - 2^47, below 2^61, rounds
 * to Q15 nearest, carrying into the integer part. Called after inc
 * changes; one wide division per change, at the control tier. */
#if FM_PHASE64
#define FM_KS_P 64
#else
#define FM_KS_P 32
#endif
static void fm_ks_tune(fm_op_t *o)
{
    const fp_w128 inc = fp_w_from_u64((uint64_t)o->inc);
    const fp_w128 two = fp_w_shl(fp_w_from_u64(1u), FM_KS_P + 1);
    o->ks_inc = o->inc;
    if (!o->inc || fp_w_cmp(fp_w_mul(inc, fp_w_from_u64(2u * o->ks_mask - 1u)), two) <= 0) {
        o->ks_n = o->ks_mask - 1u; o->ks_f = 0u;
    } else if (fp_w_cmp(fp_w_mul(inc, fp_w_from_u64(5u)), two) >= 0) {
        o->ks_n = 2u; o->ks_f = 0u;
    } else {
        const uint64_t d = (uint64_t)fp_w_to_i64(fp_w_divs(fp_w_shl(fp_w_from_u64(1u), FM_KS_P + 48), inc))
                         - ((uint64_t)1 << 47);
        const uint64_t q = (d + ((uint64_t)1 << 32)) >> 33;
        o->ks_n = (uint32_t)(q >> 15);
        o->ks_f = (uint32_t)q & 32767u;
    }
}

/* The interpolated read at the write index: two 32x32->64 products, ties
 * rounded toward positive infinity, the result inside the samples' range. */
static inline int32_t fm_ks_read(const fm_op_t *o)
{
    const uint32_t m = o->ks_mask, j = o->ks_w - o->ks_n;
    return (int32_t)(((int64_t)o->ks_buf[j & m] * (int32_t)(32768u - o->ks_f)
                    + (int64_t)o->ks_buf[(j - 1u) & m] * (int32_t)o->ks_f + 16384) >> 15);
}

/* One sample: the read, emitted before the envelope; then the rounded
 * average with the previous read, scaled by the loss, written at w. */
static inline int32_t fm_ks_q30(fm_op_t *o)
{
    int32_t u, avg;
    if (!o->ks_buf || !o->ks_word) return 0;              /* unfilled since the binding: silent */
    u = fm_ks_read(o);
    avg = (int32_t)(((int64_t)u + o->ks_prev + 1) >> 1);
    o->ks_buf[o->ks_w] = (int32_t)(((int64_t)avg * (int32_t)o->ks_loss + 16384) >> 15);
    o->ks_prev = u;
    o->ks_w = (o->ks_w + 1u) & o->ks_mask;
    return u;
}

/* The excitation: one xorshift32 draw per sample, the word as int32
 * shifted right once (Q2.30); then w = 0 and the previous read primed
 * from the current delay, so the first average is defined. */
static void fm_ks_fill(fm_op_t *o)
{
    uint32_t k, x = o->ks_word;
    for (k = 0; k <= o->ks_mask; k++) {
        x = fm_noise_step(x);
        o->ks_buf[k] = (int32_t)((x >> 1) ^ 0x40000000u) - 0x40000000;
    }
    o->ks_word = x;
    o->ks_w = 0u;
    o->ks_prev = fm_ks_read(o);
}

/* raw nonzero ('s pre-envelope feedback type) also keeps the sample
 * before the envelope multiply in raw_prev and raw_prev2. */
static inline int32_t fm_op_step(fm_op_t *o, uint32_t pm, uint32_t wave, uint32_t free_seed, uint32_t noise_shift, uint32_t raw)
{
    uint32_t p, g, clk;
    int32_t  s, senv;
#if FM_PHASE64
    clk = (uint32_t)(o->phase >> 63);
    o->phase += o->inc;
    clk ^= (uint32_t)(o->phase >> 63);
    p = (uint32_t)(o->phase >> 32) + pm;
#else
    clk = o->phase >> 31;
    o->phase += o->inc;
    clk ^= o->phase >> 31;
    p = o->phase + pm;
#endif
    if (o->pd_knee && o->pd_knee != 16384u) p = fm_pd_warp(o, p);
    switch (wave) {
    case 1u: s = (p & 0x80000000u) ? 0 : fm_sine_q30(p); break;
    case 2u: s = fm_sine_q30(p); if (s < 0) s = -s; break;
    case 3u: s = (p & 0x40000000u) ? 0 : fm_sine_q30(p & 0x3FFFFFFFu); break;
    case 4u: case 10u: case 11u:
        if (clk) o->noise = free_seed ? fm_free_word(free_seed, ++o->noise_index) : fm_noise_step(o->noise);
        s = (int32_t)((o->noise >> 1) ^ 0x40000000u) - 0x40000000;      /* the word as int32, >> 1, without a signed conversion */
        if (wave != 4u) {
            /* Both samples stay in [-2^30, 2^30-1], so delta fits int32.
             * Arithmetic quotient plus the half bit gives signed half-up
             * rounding without a variable-width int64 shift helper. */
            const int32_t delta = s - o->noise_state.colour;
            const int32_t step = (delta >> noise_shift)
                               + (int32_t)(((uint32_t)delta >> (noise_shift - 1u)) & 1u);
            o->noise_state.colour += step;
            s = wave == 10u ? o->noise_state.colour : (int32_t)(((int64_t)s - o->noise_state.colour) >> 1);
        }
        break;
    case 5u: s = (p & 0x80000000u) ? 0 : fm_sine_q30(p << 1); break;
    case 6u: s = (p & 0x80000000u) ? 0 : fm_sine_q30(p << 1); if (s < 0) s = -s; break;
    case 7u: s = (p & 0x80000000u) ? -(1 << 30) : (1 << 30); break;
    case 8u: s = (int32_t)fm_exp2_q30((p & 0x7fffffffu) >> 4); if (p & 0x80000000u) s = -s; break;
    case 9u:
        if (clk) fm_pink_tick(o, free_seed);
        s = o->noise_state.pink.sum + fm_noise_row(o->noise);
        break;
    default: s = o->wt_on ? fm_wt_q30(o, p) : o->ks_on ? fm_ks_q30(o) : fm_sine_q30(p); break;
    }
    if (raw) { o->raw_prev2 = o->raw_prev; o->raw_prev = s; }
    g = fm_env_step(o);
    senv = (int32_t)(((int64_t)s * (int64_t)(int32_t)g) >> 30);
    o->prev2 = o->prev;
    o->prev  = senv;
    return senv;
}

/* Saturates to +-(2^31 - 1), symmetric. */
static inline int32_t fm_sat32(int64_t x)
{
    return x > 2147483647 ? 2147483647 : x < -2147483647 ? -2147483647 : (int32_t)x;
}

/* : one sample of the state-variable filter (trapezoidal integrators,
 * the solved form): v3 = v0 - ic2, v1 = a1 ic1 + a2 v3, v2 = ic2 + a2 ic1 +
 * a3 v3, ic1 = 2 v1 - ic1, ic2 = 2 v2 - ic2; low-pass v2, band-pass v1,
 * high-pass v0 - k v1 - v2, notch v0 - k v1. Products of two int32 values in
 * int64, rounded half up to Q2.30; v3, the states and the output saturate. */
static inline int32_t fm_svf_step(fm_svf_t *f, int32_t v0)
{
    const int32_t v3 = fm_sat32((int64_t)v0 - f->ic2);
    const int64_t v1 = ((int64_t)f->a1 * f->ic1 + (int64_t)f->a2 * v3 + ((int64_t)1 << 29)) >> 30;
    const int64_t v2 = f->ic2 + (((int64_t)f->a2 * f->ic1 + (int64_t)f->a3 * v3 + ((int64_t)1 << 29)) >> 30);
    const int32_t b = fm_sat32(v1), l = fm_sat32(v2);
    int64_t y;
    f->ic1 = fm_sat32(v1 + v1 - f->ic1);
    f->ic2 = fm_sat32(v2 + v2 - f->ic2);
    switch (f->mode) {
    case 1u: return l;
    case 2u: return b;
    case 3u: y = (int64_t)v0 - (((int64_t)f->k * b + ((int64_t)1 << 28)) >> 29) - l; break;
    default: y = (int64_t)v0 - (((int64_t)f->k * b + ((int64_t)1 << 28)) >> 29); break;
    }
    return fm_sat32(y);
}

/* The filter's ring-out with every carrier idle: silent states stay silent
 * and do not advance; others run on zero input until both lie within
 * FM_SVF_QUIET or FM_SVF_RING samples have passed, then are zeroed. */
static inline int32_t fm_svf_ring(fm_svf_t *f)
{
    int32_t y;
    if ((f->ic1 | f->ic2) == 0) return 0;
    y = fm_svf_step(f, 0);
    if (((uint32_t)f->ic1 + FM_SVF_QUIET <= 2u * FM_SVF_QUIET && (uint32_t)f->ic2 + FM_SVF_QUIET <= 2u * FM_SVF_QUIET)
        || ++f->idle >= FM_SVF_RING)
        f->ic1 = f->ic2 = 0;
    return y;
}

/* : one shared idle cap, with the upstream tail driving stage two.
 * The established one-stage helpers remain the exact bypass path. */
static inline int32_t fm_filter_step(fm_voice_t *v, int32_t x)
{
    int32_t y;
    if (!v->svf.mode) return x;
    y = fm_svf_step(&v->svf, x);
    return v->svf2.mode ? fm_svf_step(&v->svf2, y) : y;
}

static inline int32_t fm_filter_ring(fm_voice_t *v)
{
    int32_t y;
    fm_svf_t *a = &v->svf, *b = &v->svf2;
    if (!a->mode) return 0;
    if (!b->mode) return fm_svf_ring(a);
    if ((a->ic1 | a->ic2 | b->ic1 | b->ic2) == 0) return 0;
    y = fm_svf_step(b, fm_svf_step(a, 0));
    if (((uint32_t)a->ic1 + FM_SVF_QUIET <= 2u * FM_SVF_QUIET &&
         (uint32_t)a->ic2 + FM_SVF_QUIET <= 2u * FM_SVF_QUIET &&
         (uint32_t)b->ic1 + FM_SVF_QUIET <= 2u * FM_SVF_QUIET &&
         (uint32_t)b->ic2 + FM_SVF_QUIET <= 2u * FM_SVF_QUIET) ||
        ++a->idle >= FM_SVF_RING)
        a->ic1 = a->ic2 = b->ic1 = b->ic2 = 0;
    return y;
}

/* The bank is a separate upstream lifetime. Test integrators before ringing
 * so its final sample still drives the ordinary downstream filter step. */
static int fm_formant_active(const fm_voice_t *v)
{
    unsigned i;
    for (i = 0; i < FM_FORMANTS; i++)
        if (((v->formant_mask >> i) & 1u) &&
            (v->formant[i].svf.ic1 | v->formant[i].svf.ic2)) return 1;
    return 0;
}

static inline int32_t fm_formant_step(fm_voice_t *v, int32_t x, int idle)
{
    int64_t sum = 0;
    unsigned i;
    for (i = 0; i < FM_FORMANTS; i++) if ((v->formant_mask >> i) & 1u) {
        fm_formant_t *f = &v->formant[i];
        const int32_t y = idle ? fm_svf_ring(&f->svf) : fm_svf_step(&f->svf, x);
        sum += ((int64_t)y * f->gain_q30) >> 30;
    }
    return fm_sat32(sum);
}

static inline int32_t fm_signal_step(fm_voice_t *v, int32_t x)
{
    return fm_filter_step(v, v->formant_mask ? fm_formant_step(v, x, 0) : x);
}

static inline int32_t fm_signal_ring(fm_voice_t *v)
{
    if (v->formant_mask && fm_formant_active(v))
        return fm_filter_step(v, fm_formant_step(v, 0, 1));
    return fm_filter_ring(v);
}

/* Copy only coefficients, never either stage's integrator state. */
static void fm_filter_copy_coef(fm_voice_t *v)
{
    v->svf2.a1 = v->svf.a1; v->svf2.a2 = v->svf.a2;
    v->svf2.a3 = v->svf.a3; v->svf2.k = v->svf.k;
}

/* 's feedback source for a routed operator that has not stepped yet in
 * this sample: floor((prev + prev2) / 2) in 32 bits, Q2.30, or the last
 * output alone, as on the fixed path's modulator. */
static inline int32_t fm_fb_src(const fm_op_t *o)
{
#if FM_FB_AVG
    return (o->prev >> 1) + (o->prev2 >> 1) + (o->prev & o->prev2 & 1);
#else
    return o->prev;
#endif
}

/* 's typed own feedback source, read only behind fb_special. Bit 0
 * takes 's average of the pre-envelope history instead of the post-
 * envelope one; bit 1 then squares it, after the average, into [0, 2^30]
 * for sources in [-2^30, 2^30]; a wider source saturates at 2^31 - 1. */
static inline int32_t fm_fb_typed(const fm_op_t *o, uint32_t type)
{
    int32_t src;
    int64_t sq;
    if (type & 1u) {
#if FM_FB_AVG
        src = (o->raw_prev >> 1) + (o->raw_prev2 >> 1) + (o->raw_prev & o->raw_prev2 & 1);
#else
        src = o->raw_prev;
#endif
    } else src = fm_fb_src(o);
    if (!(type & 2u)) return src;
    sq = ((int64_t)src * src) >> 30;
    return sq > INT32_MAX ? INT32_MAX : (int32_t)sq;
}

/*  amplitude edges: every operand of a multiply is a bounded int32.
 * s-ONE reaches INT32_MIN; form it in int64 before narrowing. The gain
 * and target products use the specified signed floors, not truncation. */
static inline int32_t fm_amp_edge(int32_t target, int32_t source, uint32_t weight, uint32_t kind)
{
    const int32_t one = 1 << 30;
    int32_t gain;
    int64_t value;
    if (source > one) source = one;
    else if (source < -one) source = -one;
    if (kind == 1u) {
        const int32_t unipolar = (int32_t)(((int64_t)source + one) >> 1);
        gain = one - (int32_t)(((int64_t)(int32_t)weight * (one - unipolar)) >> 8);
    } else {
        const int32_t difference = (int32_t)((int64_t)source - one);
        gain = one + (int32_t)(((int64_t)(int32_t)weight * difference) >> 8);
    }
    value = ((int64_t)target * gain) >> 30;
    return value > one ? one : value < -one ? -one : (int32_t)value;
}

/* : 32-by-32 products only. Keep the driven magnitude wide until
 * folding; an int32 clamp here would lose the later triangle periods.
 * The cubic is (3x-x^3)/2, shared with fx: slope 1.5 (+3.52 dB). */
static inline int32_t fm_shape(const fm_voice_t *v, int32_t x)
{
    const uint32_t kind = v->patch.shape.kind;
    const uint32_t one = UINT32_C(1073741824);
    uint32_t magnitude, a, b, c;
    uint64_t driven;
    if (!kind) return x;
    magnitude = x < 0 ? (uint32_t)(-(int64_t)x) : (uint32_t)x;
    driven = ((uint64_t)magnitude * (uint32_t)v->shape_gain_q24) >> 24;
    if (kind == 3u) {
        const int64_t z = x < 0 ? -(int64_t)driven : (int64_t)driven;
        const uint32_t u = (uint32_t)(z + one);
        const int64_t d = (int64_t)u - INT64_C(2147483648);
        return (int32_t)((int64_t)one - (d < 0 ? -d : d));
    }
    if (kind == 4u) {                       /* : hard clips at +one and -one/2, no make-up gain */
        const uint32_t limit = x < 0 ? one >> 1 : one;
        a = driven > limit ? limit : (uint32_t)driven;
        return x < 0 ? -(int32_t)a : (int32_t)a;
    }
    a = driven > one ? one : (uint32_t)driven;
    if (kind == 2u) {
        b = (uint32_t)(((uint64_t)a * a) >> 30);
        c = (uint32_t)(((uint64_t)b * a) >> 30);
        a = (uint32_t)(((uint64_t)3u * a - c + 1u) >> 1);
        if (a > one) a = one;
    }
    return x < 0 ? -(int32_t)a : (int32_t)a;
}

/* : the amp envelope reaching idle ends the voice. Every operator
 * goes idle as a note-off in its delay leaves it, and the filter and
 * formant integrators clear, so fm_voice_active returns 0 and the next
 * note-on starts from silence. The ring counters, phases, noise words and
 * FREE 2's idle count are kept. */
static void fm_amp_end(fm_voice_t *v)
{
    unsigned i;
    for (i = 0; i < FM_OPS; i++) {
        fm_op_t *o = &v->op[i];
        o->stage = FM_STAGE_IDLE; o->gain = 0u; o->att = FM_ATT_END;
        o->count = o->count_a_wait = 0u;
    }
    v->svf.ic1 = v->svf.ic2 = 0;
    v->svf2.ic1 = v->svf2.ic2 = 0;
    for (i = 0; i < FM_FORMANTS; i++) v->formant[i].svf.ic1 = v->formant[i].svf.ic2 = 0;
}

/* : one amp envelope step, fm_env_step without its delay and decay2
 * stages; returns this sample's gain in Q2.30, at most 2^30. The release
 * sample that reaches FM_ATT_END still returns its gain, then the voice
 * ends. */
static inline uint32_t fm_amp_step(fm_voice_t *v)
{
    fm_amp_t *a = &v->amp;
    fm_env_t t;
    if (a->stage & 4u) {                                   /* release, the only stage above 3 */
        t = a->att + a->rate_r;
        if (t >= FM_ATT_END) { t = FM_ATT_END; a->stage = FM_STAGE_IDLE; fm_amp_end(v); }
        a->att = t;
    } else {
        if (a->stage == FM_STAGE_IDLE) return 0u;
        if (a->stage == FM_STAGE_ATTACK) {
            if (--a->count == 0u) { a->gain = FM_GAIN_PEAK; a->att = 0u; a->stage = FM_STAGE_DECAY; a->count = a->count_d; }
            else a->gain += a->rate_g;
            return (uint32_t)(a->gain >> 32);
        }
        if (a->stage == FM_STAGE_DECAY) {
            if (--a->count == 0u) { a->att = a->sus; a->stage = FM_STAGE_SUSTAIN; }
            else a->att += a->rate_d;
        }
    }
    return fm_att_gain_q30(a->att);
}

/* |y| <= 2^31 and the gain <= 2^30: the product fits int64 and the floor
 * shift fits int32; the peak gain returns y unchanged. */
static inline int32_t fm_amp_apply(fm_voice_t *v, int32_t y)
{
    return (int32_t)(((int64_t)y * (int64_t)fm_amp_step(v)) >> 30);
}

/* : the shaper's place. pos 0 shapes the filtered signal, 's order;
 * pos 1 drives the formant bank and the filter with the shaped carrier sum.
 * Every kind maps 0 to 0, so a pre-position ring-out needs no shaper call.
 * : the amp envelope, when enabled, scales the result last. */
static inline int32_t fm_voice_out(fm_voice_t *v, int32_t y)
{
    y = v->patch.shape.pos ? fm_signal_step(v, fm_shape(v, y)) : fm_shape(v, fm_signal_step(v, y));
    return v->patch.amp.enabled ? fm_amp_apply(v, y) : y;   /* : disabled, one load and one branch */
}

static inline int32_t fm_voice_ring(fm_voice_t *v)
{
    const int32_t r = fm_signal_ring(v);
    const int32_t y = v->patch.shape.pos ? r : fm_shape(v, r);
    return v->patch.amp.enabled ? fm_amp_apply(v, y) : y;   /* : the ring-out is stepped too */
}

#if FM_OPS >= 4
/* Three convex lerps, each rounded half up and symmetrically saturated.
 * The weighted numerator fits int64 even at the int32 endpoints. */
static inline int32_t fm_vector_lerp(int32_t a, int32_t b, uint32_t x)
{
    const int64_t sum = (int64_t)a * (32768u - x) + (int64_t)b * x;
    return fm_sat32((sum + 16384) >> 15);
}

static inline int32_t fm_vector_mix(const fm_voice_t *v)
{
    const uint32_t x = v->vector_x, y = v->vector_y;
    const int32_t a = (int32_t)(((int64_t)v->op[0].prev * v->op[0].out) >> 30);
    const int32_t b = (int32_t)(((int64_t)v->op[1].prev * v->op[1].out) >> 30);
    const int32_t c = (int32_t)(((int64_t)v->op[2].prev * v->op[2].out) >> 30);
    const int32_t d = (int32_t)(((int64_t)v->op[3].prev * v->op[3].out) >> 30);
    return fm_vector_lerp(fm_vector_lerp(a, b, x), fm_vector_lerp(c, d, x), y);
}
#endif

/* : one sample of a routed voice, op ops-1 down to op 0 (fm_route_t).
 * An operator above op i has stepped, so its prev is this sample's output;
 * one below has not, so its prev and prev2 are its last two. Each term is
 * rounded down to Q8.24 turns or Q2.30 as the fixed path rounds its own,
 * and summed in int64; a route of one edge at weight 256 into a carrier
 * with no feedback therefore gives the fixed path's bits. 's sync
 * resets a target before its add when a source above it wrapped in this
 * sample (wrap_now) or one below it in the last sounding sample
 * (sync_wrap_prev); every operator's natural wrap is kept for both. */
static inline int32_t fm_voice_step_routed(fm_voice_t *v)
{
    const uint32_t n = v->ops, car = v->carriers, sync = v->sync_edges;
    int64_t pm, acc = 0;
    int32_t senv, y;
    uint32_t i, j, wrap_now = 0u;
    for (i = 0; i < n; i++)
        if (((car >> i) & 1u) && v->op[i].stage != FM_STAGE_IDLE) break;
    if (i == n) {
        if (v->free_running) v->idle_samples++;
        return fm_voice_ring(v);
    }
    for (i = n; i-- > 0; ) {
        fm_op_t *o = &v->op[i];
#if FM_PHASE64
        uint64_t before = 0u;
#else
        uint32_t before = 0u;
#endif
        pm = ((int64_t)(v->fb_special ? fm_fb_typed(o, v->patch.ext[i].fb_type) : fm_fb_src(o)) * (int64_t)o->fb) >> 30;   /* own feedback, Q8.24 turns */
        for (j = 0; j < n; j++) {
            const int32_t e = v->edge[j][i];                               /* 0 on the diagonal and for no edge */
            if (e != 0) pm += ((int64_t)(j > i ? v->op[j].prev : fm_fb_src(&v->op[j])) * (int64_t)e) >> 30;
        }
        if (sync) {                                                        /* : wrap_now holds only the operators above i */
            if ((sync >> (4u * i)) & (wrap_now | (v->sync_wrap_prev & ((1u << i) - 1u))) & 15u)
                o->phase = FM_PHASE_U16(v->patch.op[i].phase_u16);         /* before the add; noise keeps its word */
            before = o->phase;
        }
        senv = fm_op_step(o, (uint32_t)((uint64_t)pm << 8), v->patch.op[i].wave,
                         v->patch.op[i].free_run == 2u ? FM_NOISE_SEED[i] : 0u, v->patch.noise[i].shift,
                         v->fb_special ? v->patch.ext[i].fb_type & 1u : 0u);
        if (sync && o->phase < before) wrap_now |= 1u << i;               /* a natural wrap, never the forced reset */
        if (v->amp_edges) {
            for (j = 0; j < n; j++) {
                const uint32_t kind = v->patch.route.kind[j][i], weight = v->patch.route.w[j][i];
                if (j != i && (kind == 1u || kind == 2u) && weight != 0u)
                    senv = fm_amp_edge(senv, j > i ? v->op[j].prev : fm_fb_src(&v->op[j]), weight, kind);
            }
            o->prev = senv;
        }
        if (!v->patch.vector.enabled && ((car >> i) & 1u))
            acc += ((int64_t)senv * (int64_t)o->out) >> 30;                     /* Q2.30 */
    }
    if (sync) v->sync_wrap_prev = wrap_now;
    y = fm_sat32(acc);                                                     /*  (a): +-2.0, saturated */
#if FM_OPS >= 4
    if (v->patch.vector.enabled) y = fm_vector_mix(v);
#endif
    if (v->patch.lfo.free_run == 2u && v->lfo_time < UINT32_MAX) v->lfo_time++;
    if (v->ctl != 0u && --v->ctl == 0u) fm_ctl_tick(v, fm_w4_step(v));
    return fm_voice_out(v, y);
}

/* One voice, one sample: the voice's output into the mix in Q2.30, at most
 * 1.0 in magnitude for one carrier without a filter, at most 2^31 - 1 with
 * several or with a filter. An inactive FREE 2 voice counts elapsed
 * samples for later catch-up; other inactive voices freeze. An enabled amp
 * remains active until its envelope idles, even after the carriers stop.
 * route.ops 0 takes the fixed 2-operator path below. */
static inline int32_t fm_voice_step(fm_voice_t *v)
{
    fm_op_t *c = &v->op[0];
    fm_op_t *m = &v->op[1];
    int32_t fbs, fbt, senv_m, mod, senv_c, y;
    if (v->ops) return fm_voice_step_routed(v);
    if (c->stage == FM_STAGE_IDLE) {
        if (v->free_running) v->idle_samples++;
        return fm_voice_ring(v);
    }
#if FM_FB_AVG
    fbs    = (m->prev >> 1) + (m->prev2 >> 1) + (m->prev & m->prev2 & 1); /* : floor((prev + prev2) / 2) in 32 bits, Q2.30 */
#else
    fbs    = m->prev;                                                     /* last output, Q2.30 */
#endif
    if (v->fb_special) fbs = fm_fb_typed(m, v->patch.ext[1].fb_type);   /*  */
    fbt    = (int32_t)(((int64_t)fbs * (int64_t)m->fb) >> 30);           /* Q8.24 turns */
    senv_m = fm_op_step(m, fm_turns_to_phase(fbt), v->patch.op[1].wave,
                        v->patch.op[1].free_run == 2u ? FM_NOISE_SEED[1] : 0u, v->patch.noise[1].shift,
                        v->fb_special ? v->patch.ext[1].fb_type & 1u : 0u);
    mod    = (int32_t)(((int64_t)senv_m * (int64_t)m->index) >> 30);     /* Q8.24 turns */
    senv_c = fm_op_step(c, fm_turns_to_phase(mod), v->patch.op[0].wave,
                        v->patch.op[0].free_run == 2u ? FM_NOISE_SEED[0] : 0u, v->patch.noise[0].shift, 0u);
    y      = (int32_t)(((int64_t)senv_c * (int64_t)c->out) >> 30);       /* Q2.30 */
    if (v->patch.lfo.free_run == 2u && v->lfo_time < UINT32_MAX) v->lfo_time++;
    if (v->ctl != 0u && --v->ctl == 0u) fm_ctl_tick(v, fm_w4_step(v));
    return fm_voice_out(v, y);
}

void fm_render_i16(fm_voice_t *v, int16_t *out, uint32_t frames)
{
    uint32_t n;
    if (v->free_running && v->idle_samples > UINT64_MAX - frames) fm_free_catchup(v);
    for (n = 0; n < frames; n++) {
        int64_t s = ((int64_t)fm_voice_step(v) + (1 << 14)) >> 15;       /* round half up to Q15; int64 for a filter's 2^31 - 1 */
        if (s > 32767) s = 32767;
        else if (s < -32768) s = -32768;
        out[n] = (int16_t)s;
    }
}

void fm_render_q30(fm_voice_t *v, int32_t *out, uint32_t frames)
{
    uint32_t n;
    if (v->free_running && v->idle_samples > UINT64_MAX - frames) fm_free_catchup(v);
    for (n = 0; n < frames; n++) out[n] = fm_voice_step(v);
}

/*  mono mixer at unit gains (the SPEC pins the gain format): the Q2.30
 * outputs summed in int64, rounded once, saturated once. For one voice this
 * is fm_render_i16's arithmetic, so the same bits. */
uint32_t fm_mix_i16(fm_voice_t *voices, uint32_t count, int16_t *out, uint32_t frames)
{
    uint32_t n, i, clipped = 0;
    for (i = 0; i < count; i++)
        if (voices[i].free_running && voices[i].idle_samples > UINT64_MAX - frames) fm_free_catchup(&voices[i]);
    for (n = 0; n < frames; n++) {
        int64_t acc = 0, s;
        for (i = 0; i < count; i++) acc += fm_voice_step(&voices[i]);
        s = (acc + (1 << 14)) >> 15;                                      /* round half up to Q15, once */
        if (s > 32767) { s = 32767; clipped++; }
        else if (s < -32768) { s = -32768; clipped++; }
        out[n] = (int16_t)s;
    }
    return clipped;
}

/* ------------------------------------------------------------------ */
/*  Note-event tier: Q16.48 fp_math.h                                  */
/* ------------------------------------------------------------------ */

/* Segment length in samples: round(ms * fs / 1000), 1 to FM_SEG_MAX. The
 * upper clamp keeps fm_rate's operands in range; it binds only above
 * fs = 2.048 MHz, where 65.535 s reach 2^27 samples. */
static uint32_t fm_segment_samples(uint32_t ms, uint32_t fs)
{
    uint64_t n = ((uint64_t)ms * fs + 500u) / 1000u;
    if (n > FM_SEG_MAX) n = FM_SEG_MAX;
    return n ? (uint32_t)n : 1u;
}

/* : an attack from gain g0 (Q2.62, at most peak) keeps the slope of the
 * full attack of n samples, so it lasts ceil((peak - g0) * n / peak) =
 * n - floor(g0 * n / 2^62) samples, at least 1. g0 is split at bit 32 so the
 * product stays in 64 bits; the floor is exact. */
static uint32_t fm_attack_samples(uint64_t g0, uint32_t n)
{
    uint64_t k = ((g0 >> 32) * n + (((g0 & 0xFFFFFFFFu) * n) >> 32)) >> 30;
    return (k < n) ? n - (uint32_t)k : 1u;
}

/* Q16.48 octaves -> envelope units, rounded to nearest. */
static fm_env_t fm_env_from_q48(fixed_t x)
{
#if FM_ENV64
    return (fm_env_t)x << 8;
#else
    return (fm_env_t)((x + ((fixed_t)1 << 23)) >> 24);
#endif
}

/* Octaves per sample to travel dist octaves (Q16.48) in n samples. */
static fm_env_t fm_rate(fixed_t dist_q48, uint32_t n)
{
    /* fp_div(a, b) = a * 2^48 / b exactly. Feeding dist >> 12 over n << 36
     * keeps both operands and the quotient inside range for any count up to
     * FM_SEG_MAX. */
    return fm_env_from_q48(fp_div(dist_q48 >> 12, (fixed_t)n << 36));
}

/* Tenths of a dB -> Q16.48 octaves of attenuation; 1200 (the clamp) is
 * 19.93 octaves, inside FM_ATT_END_OCT. */
static fixed_t fm_db10_to_oct_q48(uint32_t db10)
{
    if (db10 > 1200u) db10 = 1200u;
    return fp_div(fp_from_int((int)db10), FM_DB10_PER_OCT_Q48);
}

/* Radians in Q6.10 -> turns in Q8.24: (q10 / 1024) / (2 pi), rounded. */
static int32_t fm_turns_q24(uint32_t rad_q10)
{
    fixed_t t = fp_div((fixed_t)rad_q10 << 38, FP_PI << 1);
    return (int32_t)((t + ((fixed_t)1 << 23)) >> 24);
}

/* 2^-oct in Q2.30 for Q16.48 octaves, through the envelope's exp2 table;
 * 0 octaves is exactly 1.0. */
static int32_t fm_oct_gain_q30(fixed_t oct)
{
    return (int32_t)fm_exp2_q30((uint32_t)((oct + ((fixed_t)1 << 23)) >> 24));
}

/* Output level in Q2.30 from tenths of a dB below full scale. */
static int32_t fm_level_q30(uint32_t db10)
{
    return fm_oct_gain_q30(fm_db10_to_oct_q48(db10));
}

/* velocity_q15 as a linear gain in Q2.30, rounded; 32767 is exactly 1.0. */
static int32_t fm_velocity_q30(uint32_t vel)
{
    if (vel > 32767u) vel = 32767u;
    return (int32_t)((((uint64_t)vel << 30) + 16383u) / 32767u);
}

/*  velocity crossfade, Q2.30: 1 - sens * (1 - v) / 256, sens at most 256. */
static int32_t fm_vel_gain_q30(int32_t v_q30, uint32_t sens_q8)
{
    uint64_t drop;
    if (sens_q8 > 256u) sens_q8 = 256u;
    drop = ((uint64_t)sens_q8 * (uint32_t)((1 << 30) - v_q30)) >> 8;
    return (1 << 30) - (int32_t)drop;
}

static int32_t fm_mul_q30(int32_t a, int32_t b)
{
    return (int32_t)(((int64_t)a * (int64_t)b) >> 30);
}

/* Octaves of attenuation, Q16.48, for a linear gain g in Q2.62 (0 to 2^62),
 * by inverting the exp2 table and its linear interpolation, the forward map
 * of decay and release, so the gain carries across a note-off in the attack.
 * Gains below 2^-FM_ATT_END_OCT, silence included, clamp to the end. */
static fixed_t fm_gain_to_oct_q48(uint64_t g)
{
    uint32_t oct = 0u, lo = 0u, hi = 1u << FM_EXP_BITS, mid;
    uint64_t num, den;
    if (g < ((uint64_t)1 << (62 - FM_ATT_END_OCT))) return (fixed_t)FM_ATT_END_OCT << 48;
    while (g <= ((uint64_t)1 << 61)) { g <<= 1; oct++; }      /* g in (2^61, 2^62] */
    while (hi - lo > 1u) {                                     /* tbl[lo] >= g > tbl[hi], at Q2.62 */
        mid = (lo + hi) >> 1;
        if (((uint64_t)(uint32_t)fm_exp2_tbl[mid] << 32) >= g) lo = mid;
        else hi = mid;
    }
    num = ((uint64_t)(uint32_t)fm_exp2_tbl[lo] << 32) - g;
    den = (uint64_t)(uint32_t)(fm_exp2_tbl[lo] - fm_exp2_tbl[hi]) << 32;
    return ((fixed_t)oct << 48) + ((fixed_t)lo << (48 - FM_EXP_BITS))
         + (fp_div((fixed_t)num, (fixed_t)den) >> FM_EXP_BITS);
}

/* : oct * rs / 8 in Q8.24 for a note, oct = log2(f / 27500 mHz)
 * clamped to 0..8 (A0 to A8) and rs to 0..7. log2(f / 27500) is the
 * attenuation of the gain 27500 / f, read from the exp2 table inverted as
 * key scaling reads its octaves; the product is rounded half up to Q8.24.
 * 0 for rs 0 or a note at or below 27500 mHz. */
static uint32_t fm_rs_oct_q24(uint32_t freq_millihz, uint32_t rs)
{
    fixed_t oct;
    if (rs > 7u) rs = 7u;
    if (rs == 0u || freq_millihz <= 27500u) return 0u;
    oct = fm_gain_to_oct_q48((uint64_t)fp_div((fixed_t)27500, (fixed_t)freq_millihz) << 14);
    if (oct > ((fixed_t)8 << 48)) oct = (fixed_t)8 << 48;
    return (uint32_t)((oct * (fixed_t)rs + ((fixed_t)1 << 26)) >> 27);
}

/* 's sample count for a segment of n samples, x = oct * rs / 8 in
 * Q8.24 (at most 7 octaves). The rounding rule: 2^-x = g * 2^-k with
 * k = x >> 24 and g = 2^-(x mod 1) in Q2.30 from the exp2 table (exactly
 * 2^30 at a whole octave); the count is n * g / 2^(30 + k) rounded half
 * up, (n * g + 2^(29 + k)) >> (30 + k), and 1 where that is 0. n * g stays
 * below 2^57; x = 0 returns n untouched. */
static uint32_t fm_rs_count(uint32_t n, uint32_t x)
{
    const uint32_t k = x >> 24;
    uint64_t y;
    if (x == 0u) return n;
    y = ((uint64_t)n * fm_exp2_q30(x & 0xFFFFFFu) + ((uint64_t)1 << (29u + k))) >> (30u + k);
    return y ? (uint32_t)y : 1u;
}

uint32_t fm_rate_scaled(uint32_t n, uint32_t freq_millihz, uint32_t rs)
{
    return fm_rs_count(n, fm_rs_oct_q24(freq_millihz, rs));
}

/*  index key scaling, Q2.30: 1.0 at slope 0 or for a note at or below
 * the break point; above it, slope tenths of a dB per octave of note
 * frequency, clamped at 1200 in all as every level is. log2(f / break) is
 * the attenuation of the gain break / f, read from the exp2 table inverted
 * (at most FM_ATT_END_OCT octaves, the answer for a break of 0). */
static fixed_t fm_keyscale_db10_q48(uint32_t freq_millihz, uint32_t break_millihz, uint32_t slope_db10)
{
    const fixed_t full = (fixed_t)1200 << 48;
    fixed_t oct, cap;
    if (slope_db10 == 0u || freq_millihz <= break_millihz) return 0;
    oct = fm_gain_to_oct_q48((uint64_t)fp_div((fixed_t)break_millihz, (fixed_t)freq_millihz) << 14);
    cap = (fixed_t)((uint64_t)full / slope_db10);   /* unsigned, the one 64-bit divide helper the targets import */
    return oct > cap ? full : oct * (fixed_t)slope_db10;
}

static int32_t fm_keyscale_q30(uint32_t freq_millihz, uint32_t break_millihz, uint32_t slope_db10)
{
    if (slope_db10 == 0u || freq_millihz <= break_millihz) return 1 << 30;
    return fm_oct_gain_q30(fp_div(fm_keyscale_db10_q48(freq_millihz, break_millihz, slope_db10), FM_DB10_PER_OCT_Q48));
}

/* 's index boost: t (Q8.24 turns, at least 0) times 2^b for b in
 * Q8.24 octaves (at most 1200 dB10, about 19.93), saturated at the
 * matrix's index bound of 131070 Q10. fm_exp2_q48 clamps at +14 octaves,
 * so the fraction goes through the exp2 table as 2 * 2^-(1 - f) and the
 * whole octaves n by a shift: t <= fm_turns_q24(131070) < 2^29 and
 * g < 2^31, so the product is below 2^30 before the shift and below 2^49
 * after it, in 64 bits. */
static int32_t fm_ks_boost(int32_t t, uint32_t b_q24)
{
    const uint32_t n = b_q24 >> 24, f = b_q24 & 0xFFFFFFu;
    const uint64_t g = f ? (uint64_t)fm_exp2_q30(0x1000000u - f) << 1 : UINT64_C(1) << 30;
    const uint64_t cap = (uint64_t)fm_turns_q24(131070u);
    const uint64_t r = (((uint64_t)(uint32_t)t * g) >> 30) << n;
    return (int32_t)(r > cap ? cap : r);
}

/* An operator's index from Q6.10 radians: velocity and  /
 * attenuation as before, or, with a  boost, the boost saturated
 * first and the velocity after it. */
static int32_t fm_op_index(const fm_voice_t *v, int i, uint32_t index_q10)
{
    const int32_t t = fm_turns_q24(index_q10);
    if (v->ks_boost[i]) return fm_mul_q30(fm_ks_boost(t, v->ks_boost[i]), v->lfo_vg[i]);
    return fm_mul_q30(fm_mul_q30(t, v->lfo_vg[i]), v->lfo_ks[i]);
}

/* A level in dB10 with 's signed key-scaling offset, clamped to
 * 0..1200; with no offset this is the clamp fm_level_q30 applies anyway. */
static uint32_t fm_ks_level(const fm_voice_t *v, int i, int64_t db10)
{
    db10 += v->ks_db10[i];
    return db10 < 0 ? 0u : db10 > 1200 ? 1200u : (uint32_t)db10;
}

/*  at note-on, after lfo_ks holds  / 's gain: the same side and
 * distance give the attenuation a in Q16.48 dB10. A level target takes it
 * rounded once to dB10, negated on a "+" side; an index target keeps
 * lfo_ks on a "-" side, and on a "+" side takes unity and the boost
 * a / 20 log10(2) octaves, rounded to Q8.24. */
static void fm_ks_ext(fm_voice_t *v, int i, uint32_t freq_millihz)
{
    const fm_op_patch_t *pp = &v->patch.op[i];
    const uint32_t target = v->patch.ext[i].ks_target;
    const int left = v->patch.eg[i].left_slope_db10 && freq_millihz < pp->ks_break_millihz;
    const int plus = (v->patch.ext[i].ks_curve >> (left ? 1 : 0)) & 1;
    const fixed_t a = left ? fm_keyscale_db10_q48(pp->ks_break_millihz, freq_millihz, v->patch.eg[i].left_slope_db10)
                           : fm_keyscale_db10_q48(freq_millihz, pp->ks_break_millihz, pp->ks_slope_db10);
    if (a == 0) return;
    if (target != 0u) {
        const int32_t db10 = (int32_t)((a + ((fixed_t)1 << 47)) >> 48);
        v->ks_db10[i] = (int16_t)(plus ? -db10 : db10);
    }
    if (target != 1u && plus) {
        v->lfo_ks[i] = 1 << 30;
        v->ks_boost[i] = (uint32_t)((fp_div(a, FM_DB10_PER_OCT_Q48) + ((fixed_t)1 << 23)) >> 24);
    } else if (target == 1u) v->lfo_ks[i] = 1 << 30;
}

/* Phase increment: turns per sample = f_mHz * ratio / (1000 * fs), Q16.48,
 * exact to 2^-48, clamped below Nyquist. The truncated quotient reaches 1/2
 * exactly when prod >= fs * 32768000, so that 64-bit unsigned test clamps
 * first and the division sees only operands and quotients in range. */
static void fm_set_inc(fm_op_t *o, uint32_t freq_millihz, uint32_t ratio_q16, uint32_t fs)
{
    const uint64_t prod = (uint64_t)freq_millihz * ratio_q16;
    fixed_t r;
    if (prod >= (uint64_t)fs * 32768000u) r = FP_HALF - 1;
    else r = fp_div((fixed_t)prod, (fixed_t)fs * 65536000);
#if FM_PHASE64
    o->inc = (uint64_t)r << 16;
#else
    o->inc = (uint32_t)((r + ((fixed_t)1 << 15)) >> 16);
#endif
    if (o->wt_bank) fm_wt_band(o);
    if (o->ks_buf && o->inc != o->ks_inc) fm_ks_tune(o);
}

/* 's coefficients, Q16.48 at control rate. The cutoff clamps to
 * fs * 5/12 .. fs * 450 mHz (fs/2400 to 0.45 fs, the lower rounded up) and Q
 * to 0.5 .. 16. x = pi fc / fs lies in (0, 1.414]; its sine and cosine come
 * from 12 terms of their series on unsigned magnitudes (the last term is
 * below 2^-56 at x = 1.414), each divided by an integer with the unsigned
 * 64-bit divide, then g = sin / cos. */
static fixed_t fm_svf_u(uint32_t cutoff_millihz, uint32_t fs)
{
    const uint64_t r  = fs ? fs : 1u;
    const uint64_t lo = (r * 5u + 11u) / 12u, hi = r * 450u;
    const uint64_t fc = cutoff_millihz < lo ? lo : cutoff_millihz > hi ? hi : cutoff_millihz;
    return fp_div((fixed_t)fc, (fixed_t)(r * 1000u));
}

/*  split: the unchanged coefficient arithmetic consumes cutoff/fs. */
static void fm_svf_coef_u(fm_svf_t *f, fixed_t u, uint32_t q_milli)
{
    const uint32_t q  = q_milli < 500u ? 500u : q_milli > 16000u ? 16000u : q_milli;
    fixed_t x, x2, ts, tc, sn, cs, g, k, a1, a2, a3;
    uint32_t n;
    x  = fp_mul(u, FP_PI);
    x2 = fp_mul(x, x);
    ts = sn = x;
    tc = cs = FP_ONE;
    for (n = 1u; n <= 12u; n++) {
        ts = (fixed_t)((uint64_t)fp_mul(ts, x2) / ((2u * n) * (2u * n + 1u)));
        tc = (fixed_t)((uint64_t)fp_mul(tc, x2) / ((2u * n - 1u) * (2u * n)));
        if (n & 1u) { sn -= ts; cs -= tc; } else { sn += ts; cs += tc; }
    }
    g  = fp_div(sn, cs);
    k  = (fixed_t)((((uint64_t)1000u << 48) + q / 2u) / q);
    a1 = fp_div(FP_ONE, FP_ONE + fp_mul(g, g + k));
    a2 = fp_mul(g, a1);
    a3 = fp_mul(g, a2);
    f->a1 = (int32_t)((a1 + ((fixed_t)1 << 17)) >> 18);
    f->a2 = (int32_t)((a2 + ((fixed_t)1 << 17)) >> 18);
    f->a3 = (int32_t)((a3 + ((fixed_t)1 << 17)) >> 18);
    f->k  = (int32_t)((k + ((fixed_t)1 << 18)) >> 19);
}

static void fm_svf_coef(fm_svf_t *f, uint32_t cutoff_millihz, uint32_t q_milli, uint32_t fs)
{
    fm_svf_coef_u(f, fm_svf_u(cutoff_millihz, fs), q_milli);
}

/* Center sums use int64: eight full signed-amount contributions fit below
 * 2^34 in magnitude. Clamp to the public uint32 domain before  bounds. */
static uint32_t fm_formant_center(int64_t raw, uint32_t fs)
{
    const uint64_t r = fs ? fs : 1u, lo = (r * 5u + 11u) / 12u, hi = r * 450u;
    uint64_t x = raw < 0 ? 0u : raw > UINT32_MAX ? UINT32_MAX : (uint64_t)raw;
    if (x < lo) x = lo;
    if (x > hi) x = hi;
    return (uint32_t)x; /* lo <= UINT32_MAX; x could not exceed UINT32_MAX */
}

static void fm_formant_apply(fm_voice_t *v, unsigned i)
{
    const fm_formant_patch_t *p = &v->patch.formant[i];
    fm_formant_t *f = &v->formant[i];
    const uint32_t center = fm_formant_center((int64_t)p->center_millihz + v->mod_formant_center[i], v->sample_rate_hz);
    const int32_t sum = (int32_t)p->level_db10 + v->mod_formant_gain[i];
    const uint32_t level = sum < 0 ? 0u : sum > 1200 ? 1200u : (uint32_t)sum;
    if (f->center_millihz != center) {
        fm_svf_coef(&f->svf, center, p->q_milli, v->sample_rate_hz);
        f->center_millihz = center;
    }
    if (f->level_db10 != level) { f->gain_q30 = fm_level_q30(level); f->level_db10 = level; }
}

static void fm_formant_init(fm_voice_t *v, const fm_patch_t *p)
{
    unsigned i;
    v->formant_mask = 0;
    for (i = 0; i < FM_FORMANTS; i++) {
        fm_formant_patch_t *q = &v->patch.formant[i];
        fm_formant_t *f = &v->formant[i];
        const fm_formant_patch_t *in = &p->formant[i];
        q->enabled = in->enabled != 0u;
        q->center_millihz = q->enabled ? in->center_millihz : 0u;
        q->q_milli = !q->enabled ? 0u : in->q_milli < 500u ? 500u : in->q_milli > 16000u ? 16000u : in->q_milli;
        q->level_db10 = !q->enabled ? 0u : in->level_db10 > 1200u ? 1200u : in->level_db10;
        f->svf.a1 = f->svf.a2 = f->svf.a3 = f->svf.k = f->svf.ic1 = f->svf.ic2 = 0;
        f->svf.mode = q->enabled ? 2u : 0u; f->svf.idle = 0u;
        f->center_millihz = f->level_db10 = 0u; f->gain_q30 = 0;
        v->mod_formant_center[i] = 0; v->mod_formant_gain[i] = 0;
        if (q->enabled) {
            v->formant_mask |= 1u << i;
            f->level_db10 = UINT32_MAX; /* force the initial gain, including unity */
            fm_formant_apply(v, i);
        }
    }
}

/* The gain the operator produced on its last sample, Q2.62. */
static uint64_t fm_current_gain(const fm_op_t *o)
{
    if (o->stage == FM_STAGE_IDLE || o->stage == FM_STAGE_DELAY) return 0u;
    if (o->stage == FM_STAGE_ATTACK) return o->gain;
    return (uint64_t)fm_att_gain_q30(o->att) << 32;
}

/* The operators a voice steps: 2 on the fixed path, else the route's. */
static int fm_ops_stepped(const fm_voice_t *v)
{
    return v->ops ? (int)v->ops : 2;
}

/* Nonzero while a carrier is not idle (op[0] on the fixed path). */
static int fm_sounding(const fm_voice_t *v)
{
    int i;
    for (i = 0; i < FM_OPS; i++)
        if (((v->carriers >> i) & 1u) && v->op[i].stage != FM_STAGE_IDLE) return 1;
    return 0;
}

/* : each edge's index from its operator's, weight at most 1024.
 * Row pointers: gcc 13 -Os deletes the indexed form (ivopts + modref). */
static void fm_set_edges(fm_voice_t *v)
{
    int i, j;
    for (j = 0; j < (int)v->ops; j++) {
        int32_t *e = v->edge[j];
        const uint16_t *wr = v->patch.route.w[j];
        const uint8_t *kr = v->patch.route.kind[j];
        const int64_t index = v->op[j].index;
        for (i = 0; i < (int)v->ops; i++) {
            const uint32_t w = wr[i] <= 1024u ? wr[i] : 1024u;
            e[i] = (i == j || kr[i] != 0u) ? 0 : (int32_t)((index * (int32_t)w) >> 8);
        }
    }
}

/* ------------------------------------------------------------------ */
/*  Control tier : Q16.48 at a voice's control ticks     */
/* ------------------------------------------------------------------ */

/* 2^(k/16), k = 0..16, Q16.48 rounded to nearest; ln 2 and 1/j!, j = 0..6,
 * Q2.62 rounded to nearest (all computed with 60-digit decimals). */
static const int64_t FM_EXP2_Q48[17] = {
    0x0001000000000000LL, 0x00010B5586CF9891LL, 0x0001172B83C7D518LL, 0x00012387A6E75624LL,
    0x0001306FE0A31B71LL, 0x00013DEA64C12342LL, 0x00014BFDAD5362A2LL, 0x00015AB07DD48543LL,
    0x00016A09E667F3BDLL, 0x00017A11473EB018LL, 0x00018ACE5422AA0ELL, 0x00019C49182A3F09LL,
    0x0001AE89F995AD3BLL, 0x0001C199BDD8552ALL, 0x0001D5818DCFBA48LL, 0x0001EA4AFA2A490ELL,
    0x0002000000000000LL
};
static const int64_t FM_LN2_Q62 = 0x2C5C85FDF473DE6BLL;
static const int64_t FM_EXP_SERIES_Q62[7] = {
    0x4000000000000000LL, 0x4000000000000000LL, 0x2000000000000000LL, 0x0AAAAAAAAAAAAAABLL,
    0x02AAAAAAAAAAAAABLL, 0x0088888888888889LL, 0x0016C16C16C16C17LL
};

/* a * b in Q2.62, rounded half up. */
static int64_t fm_mul_q62(int64_t a, int64_t b)
{
    return fp_w_to_i64(fp_w_asr(fp_w_add(fp_w_muls(a, b), fp_w_from_i64((int64_t)1 << 61)), 62));
}

/* 2^x = 2^(n - 16) * 2^(k/16) * e^(d ln 2) for x + 16 = n + k/16 + d,
 * k the remainder's nearest sixteenth (0 to 16) and |d| <= 1/32: the
 * series through d^6/6! leaves under 2^-50 relative, the Q2.62 steps
 * about 2^-61 each, and the product is rounded once into Q16.48. */
int64_t fm_exp2_q48(int64_t x)
{
    uint64_t u, f;
    uint32_t n, k;
    int64_t d, t, s;
    int j;
    if (x < -((int64_t)16 << 48)) x = -((int64_t)16 << 48);
    if (x > ((int64_t)14 << 48)) x = (int64_t)14 << 48;
    u = (uint64_t)(x + ((int64_t)16 << 48));
    n = (uint32_t)(u >> 48);
    f = u & (((uint64_t)1 << 48) - 1u);
    k = (uint32_t)((f + ((uint64_t)1 << 43)) >> 44);
    d = (int64_t)f - (int64_t)((uint64_t)k << 44);
    t = fp_w_to_i64(fp_w_asr(fp_w_add(fp_w_muls(d, FM_LN2_Q62), fp_w_from_i64((int64_t)1 << 47)), 48));
    s = FM_EXP_SERIES_Q62[6];
    for (j = 5; j >= 0; j--) s = FM_EXP_SERIES_Q62[j] + fm_mul_q62(s, t);
    return fp_w_to_i64(fp_w_asr(fp_w_add(fp_w_muls(FM_EXP2_Q48[k], s), fp_w_shl(fp_w_from_u64(1u), 77 - (int)n)),
                                78 - (int)n));
}

/* c / 1200 in Q16.48, rounded to nearest;  extends |c| to 12800. */
static int64_t fm_cents_q48(int32_t c)
{
    const uint64_t a = (((uint64_t)(uint32_t)(c < 0 ? -c : c) << 44) + 37u) / 75u;
    return c < 0 ? -(int64_t)a : (int64_t)a;
}

/* The increment before the LFO in turns * 2^64, truncated:
 * f * ratio * 2^48 / (1000 fs), below one turn; UINT64_MAX at or above
 * one turn. The LFO's factor is at least 1/2, so a saturated base can
 * never return below Nyquist, but a base between Nyquist and fs can. */
static uint64_t fm_base_inc(uint32_t freq_millihz, uint32_t ratio_q16, uint32_t fs)
{
    const uint64_t prod = (uint64_t)freq_millihz * ratio_q16, den = (uint64_t)fs * 1000u;
    if (prod >= den << 16) return UINT64_MAX;
    return (uint64_t)fp_div((fixed_t)prod, (fixed_t)den);
}

/* An LFO-pitched increment: base * m / 2^48, m the pitch factor in Q16.48,
 * clamped below Nyquist as fm_set_inc clamps. */
static void fm_set_inc_m(fm_op_t *o, uint64_t base, int64_t m)
{
    uint64_t inc = (uint64_t)(FP_HALF - 1) << 16;
    if (base != UINT64_MAX) {
        const fp_w128 p = fp_w_mul(fp_w_from_u64(base), fp_w_from_i64(m));
        if (fp_w_cmp(p, fp_w_shl(fp_w_from_u64(1u), 111)) < 0) inc = (uint64_t)fp_w_to_i64(fp_w_asr(p, 48));
    }
#if FM_PHASE64
    o->inc = inc;
#else
    o->inc = (uint32_t)((inc + 0x80000000u) >> 32);
#endif
    if (o->wt_bank) fm_wt_band(o);
    if (o->ks_buf && o->inc != o->ks_inc) fm_ks_tune(o);
}

/* : one rounding from milliseconds to ticks, capped so a segment
 * cannot exceed FM_SEG_MAX samples. A zero count is an immediate jump. */
static uint32_t fm_ctl_ticks(uint32_t ms, uint32_t fs)
{
    const uint64_t n = ((uint64_t)ms * fs + 500u * (uint64_t)FM_CTL) / (1000u * (uint64_t)FM_CTL);
    return n > FM_SEG_MAX / FM_CTL ? FM_SEG_MAX / FM_CTL : (uint32_t)n;
}

static int fm_peg_on(const fm_voice_t *v)
{
    const int16_t *l = v->patch.peg.level_cents;
    return l[0] || l[1] || l[2] || l[3];
}

static void fm_glide_enter(fm_glide_t *g, int32_t target, uint32_t n)
{
    const int32_t d = target - g->cur;
    const uint32_t a = (uint32_t)(d < 0 ? -d : d);
    const uint32_t qa = (uint32_t)((((uint64_t)a << 32) / n) >> 32);
    g->dir = (int8_t)((d > 0) - (d < 0));
    g->q = g->dir * (int32_t)qa;
    g->r = a - qa * n;
    g->n = n; g->acc = 0u; g->left = n;
}

static int fm_glide_step(fm_glide_t *g)
{
    g->cur += g->q;
    g->acc += g->r;                  /* below 2n, at most 2^28 for FM_CTL 1 */
    if (g->acc >= g->n) { g->acc -= g->n; g->cur += g->dir; }
    return --g->left == 0u;
}

/* At most three zero-time segments chain; release is a single segment. */
static void fm_peg_enter(fm_voice_t *v, uint32_t stage)
{
    for (;;) {
        const uint32_t i = stage == 5u ? 3u : stage - 1u;
        v->peg.stage = (uint8_t)stage;
        if (v->peg_n[i]) {
            fm_glide_enter(&v->peg, v->patch.peg.level_cents[i], v->peg_n[i]);
            return;
        }
        v->peg.cur = v->patch.peg.level_cents[i];
        v->peg.left = 0u;
        if (stage == 3u) { v->peg.stage = 4u; return; }
        if (stage == 5u) { v->peg.stage = 6u; return; }
        stage++;
    }
}

/* 's filter envelope and static note-on key tracking. */
static int fm_fenv_on(const fm_voice_t *v)
{
    return v->svf.mode && (v->patch.fenv.depth_cents || v->patch.fenv.track_permille);
}

static int32_t fm_track_cents(uint32_t freq, uint32_t ref, uint32_t track)
{
    uint32_t lo, hi;
    fixed_t oct;
    uint64_t mag;
    if (!track || freq == ref) return 0;
    lo = freq < ref ? freq : ref;
    hi = freq < ref ? ref : freq; /* ref was normalized to a nonzero value */
    oct = fm_gain_to_oct_q48((uint64_t)fp_div((fixed_t)lo, (fixed_t)hi) << 14);
    mag = ((uint64_t)(oct >> 16) * 6u * track + ((uint64_t)5u << 31)) / ((uint64_t)5u << 32);
    return freq < ref ? -(int32_t)mag : (int32_t)mag;
}

static int32_t fm_fenv_cents(const fm_voice_t *v, int32_t e)
{
    const int32_t depth = v->patch.fenv.depth_cents;
    const uint32_t mag = ((uint32_t)(depth < 0 ? -depth : depth) * (uint32_t)e + 16384u) >> 15;
    const int32_t cents = (depth < 0 ? -(int32_t)mag : (int32_t)mag) + v->fenv_track;
    return cents < -12800 ? -12800 : cents > 12800 ? 12800 : cents;
}

static fixed_t fm_u_cents(fixed_t ub, int32_t cents, fixed_t lo, fixed_t hi)
{
    fixed_t u;
    if (!cents) return ub;
    u = fp_mul(ub, fm_exp2_q48(fm_cents_q48(cents)));
    return u < lo ? lo : u > hi ? hi : u;
}

static void fm_fenv_apply(fm_voice_t *v, int force)
{
    if (v->mod_active) { fm_mod_filter(v, force); return; }
    const fixed_t u = fm_u_cents(v->fenv_ub, fm_fenv_cents(v, v->fenv.cur), v->fenv_ulo, v->fenv_uhi);
    if (force || u != v->fenv_u) {
        fm_svf_coef_u(&v->svf, u, v->fenv_q);
        fm_filter_copy_coef(v);
        v->fenv_u = u;
    }
}

/* Zero attack may chain through zero decay; release is one segment. */
static void fm_fenv_enter(fm_voice_t *v, uint32_t stage, uint32_t n)
{
    for (;;) {
        const int32_t target = stage == 1u ? 32768 : stage == 2u ? v->fenv_s : 0;
        v->fenv.stage = (uint8_t)stage;
        if (n) { fm_glide_enter(&v->fenv, target, n); return; }
        v->fenv.cur = target; v->fenv.left = 0u;
        if (stage == 2u) { v->fenv.stage = 3u; return; }
        if (stage == 4u) { v->fenv.stage = 5u; return; }
        stage = 2u; n = v->fenv_n[1];
    }
}

static void fm_fenv_step(fm_voice_t *v)
{
    const uint32_t stage = v->fenv.stage;
    const int32_t before = v->fenv.cur;
    if (stage == 1u || stage == 2u || stage == 4u) {
        if (fm_glide_step(&v->fenv)) {
            if (stage == 1u) fm_fenv_enter(v, 2u, v->fenv_n[1]);
            else v->fenv.stage = (uint8_t)(stage + 1u);
        }
    }
    if (!v->mod_active && before != v->fenv.cur && fm_fenv_cents(v, before) != fm_fenv_cents(v, v->fenv.cur))
        fm_fenv_apply(v, 0);
}

static unsigned fm_w4_step(fm_voice_t *v)
{
    const int32_t before = v->peg.cur;
    const uint32_t stage = v->peg.stage;
    if ((stage >= 1u && stage <= 3u) || stage == 5u) {
        if (fm_glide_step(&v->peg)) {
            if (stage == 3u) v->peg.stage = 4u;
            else if (stage == 5u) v->peg.stage = 6u;
            else fm_peg_enter(v, stage + 1u);
        }
    }
    if (fm_fenv_on(v)) fm_fenv_step(v);
    return v->peg.cur != before ? FM_CHG_PEG : 0u;
}

/* A peg can bring a base above one turn back below Nyquist. Keep whole
 * and fractional turns up to 16: the minimum combined factor is 1/32,
 * so a base >= 16 can never fall below Nyquist. In the other branch,
 * prod < 16*den < 2^62: prod<<64 fits signed wide below 2^126. The
 * quotient's low 64 bits are the fractional turn on both backends. */
static void fm_peg_base(fm_voice_t *v, int i, uint32_t freq_millihz)
{
    const uint64_t prod = (uint64_t)freq_millihz * v->patch.op[i].ratio_q16;
    const uint64_t den = (uint64_t)v->sample_rate_hz * 1000u * 65536u;
    const uint64_t whole = den ? prod / den : 16u;
    if (whole >= 16u) { v->peg_base_whole[i] = 16u; v->peg_base_frac[i] = 0u; }
    else {
        /* Dividing the full numerator avoids a separate modulo helper;
         * narrowing discards the whole turns already stored below. */
        const fp_w128 num = fp_w_shl(fp_w_from_u64(prod), 64);
        v->peg_base_whole[i] = (uint8_t)whole;
        v->peg_base_frac[i] = (uint64_t)fp_w_to_i64(fp_w_divs(num, fp_w_from_u64(den)));
    }
}

/* base < 2^68 raw, factor <= 2^53 raw, product < 2^121. Compare
 * against half a turn at bit 111 before narrowing to the phase width. */
static void fm_peg_inc(fm_voice_t *v, int i, int64_t factor)
{
    uint64_t inc = (uint64_t)(FP_HALF - 1) << 16;
    if (v->peg_base_whole[i] < 16u) {
        const fp_w128 base = fp_w_add(fp_w_shl(fp_w_from_u64(v->peg_base_whole[i]), 64),
                                     fp_w_from_u64(v->peg_base_frac[i]));
        const fp_w128 product = fp_w_mul(base, fp_w_from_i64(factor));
        if (fp_w_cmp(product, fp_w_shl(fp_w_from_u64(1u), 111)) < 0)
            inc = (uint64_t)fp_w_to_i64(fp_w_asr(product, 48));
    }
#if FM_PHASE64
    v->op[i].inc = inc;
#else
    v->op[i].inc = (uint32_t)((inc + 0x80000000u) >> 32);
#endif
    if (v->op[i].wt_bank) fm_wt_band(&v->op[i]);
    if (v->op[i].ks_buf && v->op[i].inc != v->op[i].ks_inc) fm_ks_tune(&v->op[i]);
}

/* Only matrix-pitched operators need the wider base. prod fits uint64;
 * den < 2^58 and rem < den, so rem<<64 is positive and below 2^122.
 * Taking the remainder before shifting also handles prod >= 2^63. */
static void fm_mod_base(fm_voice_t *v, int i, uint32_t freq_millihz)
{
    const uint64_t prod = (uint64_t)freq_millihz * v->patch.op[i].ratio_q16;
    const uint64_t den = (uint64_t)v->sample_rate_hz * 1000u * 65536u;
    const uint64_t whole = den ? prod / den : 256u;
    if (whole >= 256u) {
        v->peg_base_whole[i] = 256u;
        v->peg_base_frac[i] = 0u;
    } else {
        /* whole <= 255. Splitting the product into 32-bit limbs prevents
         * GCC from replacing this remainder with an unapproved modulo
         * helper. Unsigned wrap reconstructs the exact low 64-bit product;
         * whole*den <= prod guarantees that product itself fits. */
        const uint32_t w = (uint32_t)whole;
        const uint64_t rem = prod - (uint64_t)w * (uint32_t)den
                           - ((uint64_t)(w * (uint32_t)(den >> 32)) << 32);
        const fp_w128 num = fp_w_shl(fp_w_from_u64(rem), 64);
        v->peg_base_whole[i] = (uint16_t)whole;
        v->peg_base_frac[i] = (uint64_t)fp_w_to_i64(fp_w_divs(num, fp_w_from_u64(den)));
    }
}

/* factor is positive Q48 in [2^39,2^57]. A raw base can approach 2^72;
 * its unguarded product could need 129 bits. Compare before multiplying:
 * base < ceil(2^111/factor) iff base*factor < 2^111. */
static void fm_mod_inc(fm_voice_t *v, int i, int64_t factor)
{
    uint64_t inc = (uint64_t)(FP_HALF - 1) << 16;
    if (v->peg_base_whole[i] < 256u) {
        const fp_w128 base = fp_w_add(fp_w_shl(fp_w_from_u64(v->peg_base_whole[i]), 64),
                                     fp_w_from_u64(v->peg_base_frac[i]));
        const fp_w128 limit = fp_w_divs(fp_w_add(fp_w_shl(fp_w_from_u64(1u), 111),
                                                fp_w_from_i64(factor - 1)),
                                        fp_w_from_i64(factor));
        if (fp_w_cmp(base, limit) < 0) {
            const fp_w128 product = fp_w_mul(base, fp_w_from_i64(factor));
            inc = (uint64_t)fp_w_to_i64(fp_w_asr(product, 48));
        }
    }
#if FM_PHASE64
    v->op[i].inc = inc;
#else
    v->op[i].inc = (uint32_t)((inc + 0x80000000u) >> 32);
#endif
    if (v->op[i].wt_bank) fm_wt_band(&v->op[i]);
    if (v->op[i].ks_buf && v->op[i].inc != v->op[i].ks_inc) fm_ks_tune(&v->op[i]);
}

/* Apply peg and cached LFO pitch only; events use this without changing
 * the LFO's phase, random word, time or control countdown. */
static void fm_peg_pitch(fm_voice_t *v, uint32_t mask)
{
    const uint32_t lm = v->patch.lfo.rate_millihz ? v->patch.lfo.mask_pitch : 0u;
    int64_t xp, mp, mlp;
    int i, nops;
    mask &= v->peg_mask;
    if (!mask) return;
    xp = fm_cents_q48(v->peg.cur);
    mp = fm_exp2_q48(xp);
    mlp = (mask & lm) ? fm_exp2_q48(xp + fm_cents_q48(v->lfo_cents)) : mp;
    nops = fm_ops_stepped(v);
    for (i = 0; i < nops; i++)
        if ((mask >> i) & 1u) fm_peg_inc(v, i, ((lm >> i) & 1u) ? mlp : mp);
}

/* One value of 's xorshift32 for waves 5 and 6, Q1.15. */
static int32_t fm_lfo_draw(fm_voice_t *v)
{
    v->lfo_word = fm_noise_step(v->lfo_word);
    return (int32_t)(v->lfo_word >> 16) - 32768;
}

/* The LFO's value at phase p (turns * 2^32), Q1.15, -32768 to 32767.
 * Bit tests, not a switch: gcc's jump table for one imports
 * __gnu_thumb1_case_uhi on Cortex-M0+, outside 's list. */
static int32_t fm_lfo_wave(const fm_voice_t *v, uint32_t p)
{
    const uint32_t w = v->patch.lfo.wave;
    int32_t l;
    uint32_t d;
    if (w == 0u || w > 6u) {                               /* 0 and above 6: sine, from the sine table */
        l = fm_sine_q30(p) >> 15;
        return l > 32767 ? 32767 : l;
    }
    if (w & 4u) {
        if (w & 2u)                                        /* 6, smoothed random: the held value to the next, linearly */
            return v->lfo_a + (int32_t)(((int64_t)(v->lfo_b - v->lfo_a) * (int64_t)(p >> 17)) >> 15);
        if (w & 1u)                                        /* 5, sample and hold */
            return v->lfo_a;
        return (p & 0x80000000u) ? -32768 : 32767;         /* 4, square: the phase's top bit */
    }
    if (w & 2u) {
        if (w & 1u)                                        /* 3, saw down: 0, falling, 32767 from just past 1/2 */
            return (int32_t)(((0u - p) >> 16) ^ 0x8000u) - 32768;
        return (int32_t)((p >> 16) ^ 0x8000u) - 32768;     /* 2, saw up: 0, rising, -32768 from 1/2 */
    }
    d = p + 0x40000000u;                                   /* 1, triangle: 0, up to the peak at 1/4, down to -32768 at 3/4 */
    d = d >= 0x80000000u ? d - 0x80000000u : 0x80000000u - d;
    l = 32768 - (int32_t)(d >> 15);
    return l > 32767 ? 32767 : l;
}

/* The depths' gain at this tick, Q1.15: 0 in the delay, then rising
 * linearly over the fade to 32768, which it keeps. */
static int64_t fm_lfo_gain(const fm_voice_t *v)
{
    uint32_t e;
    if (v->lfo_time < v->lfo_delay) return 0;
    e = v->lfo_time - v->lfo_delay;
    if (e >= v->lfo_fade) return 32768;
    return (int64_t)(((uint64_t)e << 15) / v->lfo_fade);
}

/* FREE 2's phase is the value at the current grid point. Its random
 * endpoints depend only on seed and wrap index, including while idle. */
static void fm_free_lfo_advance(fm_voice_t *v, uint64_t ticks)
{
    const uint64_t sum = (uint64_t)v->lfo_step * ticks + v->lfo_phase;
    v->lfo_wrap += (uint32_t)(sum >> 32);
    v->lfo_phase = (uint32_t)sum;
    if (v->patch.lfo.wave == 5u || v->patch.lfo.wave == 6u) {
        v->lfo_word = fm_free_word(FM_LFO_SEED, v->lfo_wrap);
        v->lfo_a = (int32_t)(v->lfo_word >> 16) - 32768;
        if (v->patch.lfo.wave == 6u)
            v->lfo_b = (int32_t)(fm_free_word(FM_LFO_SEED, v->lfo_wrap + 1u) >> 16) - 32768;
    }
}

/* Closed form at a note/pitch event, or at a render-block boundary before
 * the pending count could overflow. The last instantaneous increment is
 * held while idle. Its product with elapsed samples fits below 2^127;
 * the phase and noise counter intentionally retain only their low bits.
 * No envelope, filter, LFO tick or per-skipped-sample loop runs here. */
static void fm_free_catchup(fm_voice_t *v)
{
    const uint64_t elapsed = v->idle_samples;
    int i;
    if (!elapsed) return;
    for (i = 0; i < fm_ops_stepped(v); i++) {
        fm_op_t *o = &v->op[i];
        fp_w128 sum;
        uint64_t crossings;
        if (v->patch.op[i].free_run != 2u) continue;
        sum = fp_w_add(fp_w_mul(fp_w_from_u64(o->inc), fp_w_from_u64(elapsed)), fp_w_from_u64(o->phase));
#if FM_PHASE64
        crossings = (uint64_t)fp_w_to_i64(fp_w_asr(sum, 63)) - (o->phase >> 63);
        o->phase = (uint64_t)fp_w_to_i64(sum);
#else
        crossings = (uint64_t)fp_w_to_i64(fp_w_asr(sum, 31)) - (o->phase >> 31);
        o->phase = (uint32_t)fp_w_to_i64(sum);
#endif
        if (v->patch.op[i].wave == 4u || v->patch.op[i].wave == 10u || v->patch.op[i].wave == 11u) {
            o->noise_index += (uint32_t)crossings;
            o->noise = fm_free_word(FM_NOISE_SEED[i], o->noise_index);
        }
        else if (v->patch.op[i].wave == 9u) fm_pink_skip(o, FM_NOISE_SEED[i], crossings);
    }
    if (v->patch.lfo.rate_millihz && v->patch.lfo.free_run == 2u) {
        const uint32_t rem = v->ctl ? v->ctl : FM_CTL;
        const uint64_t tail = elapsed % FM_CTL + (FM_CTL - rem);
        const uint64_t ticks = elapsed / FM_CTL + tail / FM_CTL;
        fm_free_lfo_advance(v, ticks);
        v->ctl = FM_CTL - (uint32_t)(tail % FM_CTL);
    }
    v->idle_samples = 0;
}

/* 's new control path is entered only with at least one valid slot. */
static int32_t fm_mod_clamp(int64_t x, int32_t lo, int32_t hi)
{
    return x < lo ? lo : x > hi ? hi : (int32_t)x;
}

/* Initialization and changed control values only; never the sample tier. */
static void fm_pd_set(fm_op_t *o, uint32_t knee)
{
    if (o->pd_knee == knee) return;
    o->pd_knee = knee;
    if (knee) {
        const uint64_t k = (uint64_t)knee << 17;
        o->pd_slope[0] = (UINT64_C(1) << 62) / k;
        o->pd_slope[1] = (UINT64_C(1) << 62) / ((UINT64_C(1) << 32) - k);
    } else o->pd_slope[0] = o->pd_slope[1] = 0;
}

static int32_t fm_mod_normalize(int32_t x, uint32_t scale)
{
    const uint32_t mag = (uint32_t)(x < 0 ? -x : x);
    const uint32_t n = (uint32_t)(((uint64_t)mag * 32768u + scale / 2u) / scale);
    const int32_t y = n > 32768u ? 32768 : (int32_t)n;
    return x < 0 ? -y : y;
}

static int fm_mod_pm_source(const fm_voice_t *v, uint32_t i)
{
    uint32_t j;
    if (!v->ops) return i == 1u;
    for (j = 0; j < v->ops; j++)
        if (j != i && v->patch.route.w[i][j] && !v->patch.route.kind[i][j]) return 1;
    return 0;
}

static void fm_mod_lfo_reset(fm_voice_t *v)
{
    const fm_lfo_patch_t *p = &v->patch.lfo;
    v->mod_lfo_rate = p->rate_millihz < 10u ? 10u : p->rate_millihz > 50000u ? 50000u : p->rate_millihz;
    v->mod_lfo_pitch = p->pitch_cents > 1200u ? 1200u : p->pitch_cents;
    v->mod_lfo_level = p->level_db10 > 1200u ? 1200u : p->level_db10;
    v->mod_lfo_index = p->index_q10;
}

static void fm_mod_init(fm_voice_t *v, const fm_patch_t *p)
{
    uint32_t i, rate_mask = 0u;
    uint64_t rate_upper;
    const uint32_t nops = (uint32_t)fm_ops_stepped(v);
    v->mod_active = v->mod_pitch_mask = 0u;
    v->mod_velocity = v->mod_note = v->mod_cutoff = v->mod_q = 0;
    v->mod_filter_q = v->fenv_q < 500u ? 500u : v->fenv_q > 16000u ? 16000u : v->fenv_q;
    fm_mod_lfo_reset(v);
    rate_upper = v->mod_lfo_rate;
    for (i = 0; i < FM_OPS; i++) v->mod_pitch[i] = v->mod_level[i] = v->mod_index[i] = v->mod_pd[i] = v->mod_wt[i] = 0;
    for (i = 0; i < FM_CTL_INPUTS; i++) v->ctl_input[i] = 0;
    for (i = 0; i < FM_GEN_INPUTS; i++) v->gen_input[i] = 0;
    for (i = 0; i < FM_MOD_SLOTS; i++) {
        const fm_mod_slot_patch_t *in = &p->mod[i];
        fm_mod_slot_patch_t *s = &v->patch.mod[i];
        const uint32_t source = in->source, target = in->target, op = in->target_index;
        int32_t limit = 0;
        int valid = source == 0u ? v->patch.lfo.rate_millihz != 0u
                  : source == 1u ? fm_peg_on(v)
                  : source == 2u ? fm_fenv_on(v)
                  : source >= 3u && source <= 6u ? source - 3u < nops
                  : source == 7u || source == 8u || (source >= 16u && source < 24u)
                    || (source >= 64u && source < 64u + FM_GEN_INPUTS);
        s->source = in->source; s->target = in->target; s->target_index = in->target_index;
        s->shift = in->shift > 15u ? 15u : in->shift;
        s->reserved[0] = s->reserved[1] = s->reserved[2] = 0u;
        if (target <= 3u) {
            if (target == 1u) { limit = 4800; valid = valid && op < nops; }
            else if (target == 2u) { limit = 1200; valid = valid && op < nops && ((v->carriers >> op) & 1u); }
            else if (target == 3u) { limit = 65535; valid = valid && op < nops && fm_mod_pm_source(v, op); }
            else valid = 0;
        } else if (target <= 5u) {
            limit = target == 4u ? 9600 : 15500;
            valid = valid && op == 0u && v->svf.mode;
        } else if (target <= 9u) {
            limit = target == 6u ? 49990 : target == 9u ? 65535 : 1200;
            valid = valid && op == 0u && v->patch.lfo.rate_millihz;
        } else if (target == 32u) {
            limit = 32768;
            valid = valid && op < nops && v->patch.wt[op].enabled;
        } else if (target == 33u) {
            limit = 32767;
            valid = valid && op < nops && v->patch.pd[op].enabled;
        } else if (target == 34u || target == 35u) {
            limit = 32768;
            valid = valid && op == 0u && v->patch.vector.enabled;
        } else if (target == 36u || target == 37u) {
            limit = target == 36u ? INT32_MAX : 1200;
            valid = valid && op < FM_FORMANTS && v->patch.formant[op].enabled;
        } else valid = 0;
        s->amount = fm_mod_clamp(in->amount, -limit, limit);
        v->mod_cur[i] = 0;
        if (!valid) { s->target = 0u; s->amount = 0; }
        if (!valid || !s->amount) continue;
        v->mod_active |= 1u << i;
        if (target == 1u && !v->patch.op[op].fixed_millihz) v->mod_pitch_mask |= 1u << op;
        if (target == 6u) {
            rate_mask |= 1u << i;
            rate_upper += (uint32_t)(s->amount < 0 ? -s->amount : s->amount);
        }
    }
    if (rate_upper > 50000u) rate_upper = 50000u;
    if (rate_mask && rate_upper * FM_CTL >= (uint64_t)500u * v->sample_rate_hz) {
        v->mod_active &= ~rate_mask;
        for (i = 0; i < FM_MOD_SLOTS; i++) if ((rate_mask >> i) & 1u) {
            v->patch.mod[i].target = 0u; v->patch.mod[i].amount = 0;
        }
    }
}

/* Combine un-clamped  cents before the matrix's final audible bound. */
static void fm_mod_filter(fm_voice_t *v, int force)
{
    int32_t cents = v->mod_cutoff;
    uint32_t q;
    fixed_t u;
    if (!v->svf.mode) return;
    if (fm_fenv_on(v)) {
        const int32_t d = v->patch.fenv.depth_cents;
        const uint32_t m = ((uint32_t)(d < 0 ? -d : d) * (uint32_t)v->fenv.cur + 16384u) >> 15;
        cents += (d < 0 ? -(int32_t)m : (int32_t)m) + v->fenv_track;
    }
    cents = fm_mod_clamp(cents, -12800, 12800);
    q = (uint32_t)fm_mod_clamp((int64_t)v->fenv_q + v->mod_q, 500, 16000);
    u = fm_u_cents(v->fenv_ub, cents, v->fenv_ulo, v->fenv_uhi);
    if (force || u != v->fenv_u || q != v->mod_filter_q) {
        fm_svf_coef_u(&v->svf, u, q); fm_filter_copy_coef(v);
        v->fenv_u = u; v->mod_filter_q = q;
    }
}

/* Only active matrix pitch uses the wider base. Unaffected operators
 * keep their established LFO-only /  paths and rounding. */
static void fm_mod_pitch_apply(fm_voice_t *v)
{
    const fm_lfo_patch_t *lp = &v->patch.lfo;
    const uint32_t lm = lp->rate_millihz ? lp->mask_pitch : 0u;
    int i;
    for (i = 0; i < fm_ops_stepped(v); i++) {
        if (v->patch.op[i].fixed_millihz) continue;
        if ((v->mod_pitch_mask >> i) & 1u) {
            const int64_t x = fm_cents_q48(v->mod_pitch[i])
                + (((v->peg_mask >> i) & 1u) ? fm_cents_q48(v->peg.cur) : 0)
                + (((lm >> i) & 1u) ? fm_cents_q48(v->lfo_cents) : 0);
            fm_mod_inc(v, i, fm_exp2_q48(x));
        } else if ((v->peg_mask >> i) & 1u) fm_peg_pitch(v, 1u << i);
        else if ((lm >> i) & 1u) fm_set_inc_m(&v->op[i], v->lfo_base[i], v->lfo_m);
    }
}

/* Source state is held throughout slot evaluation. Offset accumulators below
 * are not sources; sampling on demand avoids a host-only stack-array helper. */
static int32_t fm_mod_source(const fm_voice_t *v, uint32_t id, int32_t lfo, int32_t peg)
{
    if (id == 0u) return lfo;
    if (id == 1u) return peg;
    if (id == 2u) return v->fenv.cur;
    if (id <= 6u) {
        const uint32_t gain = (uint32_t)(fm_current_gain(&v->op[id - 3u]) >> 32);
        const uint32_t value = (gain + 16384u) >> 15;
        return value > 32768u ? 32768 : (int32_t)value;
    }
    if (id == 7u) return v->mod_velocity;
    if (id == 8u) return v->mod_note;
    if (id >= 64u) return v->gen_input[id - 64u];
    return v->ctl_input[id - 16u];
}

static void fm_mod_tick(fm_voice_t *v, unsigned changed)
{
    const fm_lfo_patch_t *lp = &v->patch.lfo;
    int32_t lfo_source = 0, peg_source;
    int32_t rate_sum = 0, pitch_sum = 0, level_sum = 0, index_sum = 0;
    int32_t vector_x_sum = 0, vector_y_sum = 0;
    int64_t l = 0, g = 0, att = 0, ioff = 0;
    uint32_t p = v->lfo_phase, i;
    const int note = (changed & FM_MOD_NOTE) != 0u;
    /* Reuse caller-owned target fields until their final clamp. No source
     * reads these fields, and no derived gain changes until all slots finish. */
    for (i = 0; i < (uint32_t)fm_ops_stepped(v); i++)
        v->mod_pitch[i] = v->mod_level[i] = v->mod_index[i] = v->mod_pd[i] = v->mod_wt[i] = 0;
    v->mod_cutoff = v->mod_q = 0;
    for (i = 0; i < FM_FORMANTS; i++) { v->mod_formant_center[i] = 0; v->mod_formant_gain[i] = 0; }
    if (lp->rate_millihz) {
        if (lp->free_run == 2u && !(changed & FM_LFO_HOLD)) fm_free_lfo_advance(v, 1u);
        p = v->lfo_phase; l = fm_lfo_wave(v, p); g = fm_lfo_gain(v);
        lfo_source = (int32_t)((l * g) >> 15);
        /* Keep the original depth/fade rounding separately from source0. */
        v->lfo_cents = (int32_t)(((((int64_t)v->mod_lfo_pitch * g) >> 15) * l) >> 15);
        v->lfo_m = fm_exp2_q48(fm_cents_q48(v->lfo_cents));
        att = ((((int64_t)v->mod_lfo_level * g) >> 15) * (32767 - l)) >> 16;
        ioff = ((((int64_t)v->mod_lfo_index * g) >> 15) * l) >> 15;
    }
    peg_source = fm_mod_normalize(v->peg.cur, 4800u);
    for (i = 0; i < FM_MOD_SLOTS; i++) if ((v->mod_active >> i) & 1u) {
        const fm_mod_slot_patch_t *s = &v->patch.mod[i];
        const int32_t value = fm_mod_source(v, s->source, lfo_source, peg_source);
        int32_t x = v->mod_cur[i], contribution;
        int64_t product;
        uint64_t magnitude;
        if (!s->shift) x = value;
        else if (!note) {
            const int32_t d = value - x;
            const uint32_t m = (uint32_t)(d < 0 ? -d : d);
            const int32_t step = (int32_t)((m + ((1u << s->shift) - 1u)) >> s->shift);
            x += d < 0 ? -step : step;
        }
        v->mod_cur[i] = x;
        product = (int64_t)x * s->amount;
        magnitude = (uint64_t)(product < 0 ? -product : product);
        contribution = product < 0 ? -(int32_t)(magnitude >> 15) : (int32_t)(magnitude >> 15);
        if (s->target <= 3u) {
            int32_t *dest = s->target == 1u ? v->mod_pitch : s->target == 2u ? v->mod_level : v->mod_index;
            dest[s->target_index] += contribution;
        } else if (s->target <= 5u) {
            if (s->target == 4u) v->mod_cutoff += contribution;
            else v->mod_q += contribution;
        } else if (s->target <= 9u) {
            /* Families 6/7 and 8/9: bit tests avoid a wide Thumb switch. */
            if (s->target & 2u) {
                if (s->target & 1u) pitch_sum += contribution;
                else rate_sum += contribution;
            } else {
                if (s->target & 1u) index_sum += contribution;
                else level_sum += contribution;
            }
        }
        else if (s->target == 32u) v->mod_wt[s->target_index] += contribution;
        else if (s->target == 33u) v->mod_pd[s->target_index] += contribution;
        else if (s->target == 34u) vector_x_sum += contribution;
        else if (s->target == 35u) vector_y_sum += contribution;
        else if (s->target == 36u) v->mod_formant_center[s->target_index] += contribution;
        else if (s->target == 37u) v->mod_formant_gain[s->target_index] += contribution;
    }
    for (i = 0; i < (uint32_t)fm_ops_stepped(v); i++) {
        const fm_op_patch_t *op = &v->patch.op[i];
        int32_t level, index;
        v->mod_pitch[i] = fm_mod_clamp(v->mod_pitch[i], -4800, 4800);
        v->mod_level[i] = fm_mod_clamp(v->mod_level[i], -1200, 1200);
        v->mod_index[i] = fm_mod_clamp(v->mod_index[i], -65535, 65535);
        v->mod_pd[i] = fm_mod_clamp(v->mod_pd[i], -32767, 32767);
        if (v->patch.pd[i].enabled)
            fm_pd_set(&v->op[i], (uint32_t)fm_mod_clamp((int32_t)v->patch.pd[i].knee_q15 + v->mod_pd[i], 1, 32767));
        v->mod_wt[i] = fm_mod_clamp(v->mod_wt[i], -32768, 32768);
        if (v->patch.wt[i].enabled) {
            const uint32_t pos = (uint32_t)fm_mod_clamp((int32_t)v->patch.wt[i].position_q15 + v->mod_wt[i], 0, 32768);
            if (pos != v->op[i].wt_pos) { v->op[i].wt_pos = pos; fm_wt_point(&v->op[i]); }
        }
        level = fm_mod_clamp((int64_t)op->level_db10 + (lp->rate_millihz && ((lp->mask_level >> i) & 1u) ? att : 0) + v->mod_level[i] + v->ks_db10[i], 0, 1200);
        index = fm_mod_clamp((int64_t)op->index_q10 + (lp->rate_millihz && ((lp->mask_index >> i) & 1u) ? ioff : 0) + v->mod_index[i], 0, 131070);
        v->op[i].out = fm_mul_q30(fm_level_q30((uint32_t)level), v->lfo_vg[i]);
        v->op[i].index = fm_op_index(v, (int)i, (uint32_t)index);
    }
    /* Coordinates are not matrix sources. Publish both only after all sums. */
    v->vector_x = (uint32_t)fm_mod_clamp((int32_t)v->patch.vector.x_q15 + vector_x_sum, 0, 32768);
    v->vector_y = (uint32_t)fm_mod_clamp((int32_t)v->patch.vector.y_q15 + vector_y_sum, 0, 32768);
    v->mod_cutoff = fm_mod_clamp(v->mod_cutoff, -9600, 9600);
    v->mod_q = fm_mod_clamp(v->mod_q, -15500, 15500);
    fm_mod_pitch_apply(v); fm_set_edges(v); fm_mod_filter(v, note);
    for (i = 0; i < FM_FORMANTS; i++) if ((v->formant_mask >> i) & 1u) fm_formant_apply(v, i);
    if (lp->rate_millihz) {
        const uint64_t den = (uint64_t)v->sample_rate_hz * 1000u;
        if (lp->free_run != 2u) {
            v->lfo_phase = p + v->lfo_step;
            if (v->lfo_phase < p) {
                if (lp->wave == 5u) v->lfo_a = fm_lfo_draw(v);
                else if (lp->wave == 6u) { v->lfo_a = v->lfo_b; v->lfo_b = fm_lfo_draw(v); }
            }
            v->lfo_time = v->lfo_time <= UINT32_MAX - FM_CTL ? v->lfo_time + FM_CTL : UINT32_MAX;
        }
        fm_mod_lfo_reset(v);
        v->mod_lfo_rate = (uint32_t)fm_mod_clamp((int64_t)v->mod_lfo_rate + fm_mod_clamp(rate_sum, -49990, 49990), 10, 50000);
        v->mod_lfo_pitch = (uint32_t)fm_mod_clamp((int64_t)v->mod_lfo_pitch + fm_mod_clamp(pitch_sum, -1200, 1200), 0, 1200);
        v->mod_lfo_level = (uint32_t)fm_mod_clamp((int64_t)v->mod_lfo_level + fm_mod_clamp(level_sum, -1200, 1200), 0, 1200);
        v->mod_lfo_index = (uint32_t)fm_mod_clamp((int64_t)v->mod_lfo_index + fm_mod_clamp(index_sum, -65535, 65535), 0, 65535);
        v->lfo_step = den ? (uint32_t)((((uint64_t)v->mod_lfo_rate * FM_CTL << 32) + den / 2u) / den) : 0u;
    }
    if (!(changed & FM_LFO_HOLD)) v->ctl = FM_CTL;
}

/* 's control tick: the LFO's value and gain move the masked
 * operators' increments, levels and indices (fm_lfo_patch_t), then the
 * LFO steps; at a wrap waves 5 and 6 draw their next value. */
static void fm_ctl_tick(fm_voice_t *v, unsigned changed)
{
    if (v->mod_active) { fm_mod_tick(v, changed); return; }
    const fm_lfo_patch_t *lp = &v->patch.lfo;
    if (lp->rate_millihz) {
        if (lp->free_run == 2u && !(changed & FM_LFO_HOLD)) fm_free_lfo_advance(v, 1);
        const int nops = fm_ops_stepped(v);
        const uint32_t p = v->lfo_phase;
        const int64_t l = fm_lfo_wave(v, p), g = fm_lfo_gain(v);
        const int64_t pc = ((int64_t)(lp->pitch_cents < 1200u ? lp->pitch_cents : 1200u) * g) >> 15;
        const int64_t lc = ((int64_t)(lp->level_db10 < 1200u ? lp->level_db10 : 1200u) * g) >> 15;
        const int64_t ic = ((int64_t)lp->index_q10 * g) >> 15;
        const int32_t cents = (int32_t)((pc * l) >> 15);
        const uint32_t att = (uint32_t)((lc * (32767 - l)) >> 16);
        const int64_t ioff = (ic * l) >> 15;
        int i;
        v->lfo_cents = cents;
        v->lfo_m = fm_exp2_q48(fm_cents_q48(cents));
        for (i = 0; i < nops; i++) {
            const fm_op_patch_t *pp = &v->patch.op[i];
            fm_op_t *o = &v->op[i];
            if (((lp->mask_pitch >> i) & 1u) && !pp->fixed_millihz && !((v->peg_mask >> i) & 1u))
                fm_set_inc_m(o, v->lfo_base[i], v->lfo_m);
            if ((lp->mask_level >> i) & 1u)
                o->out = fm_mul_q30(fm_level_q30(fm_ks_level(v, i, (int64_t)pp->level_db10 + att)), v->lfo_vg[i]);
            if ((lp->mask_index >> i) & 1u) {
                const int64_t x = (int64_t)pp->index_q10 + ioff;
                o->index = fm_op_index(v, i, x > 0 ? (uint32_t)x : 0u);
            }
        }
        if (lp->mask_index) fm_set_edges(v);
        if (lp->free_run != 2u) {
            v->lfo_phase = p + v->lfo_step;
            if (v->lfo_phase < p) {
                if (lp->wave == 5u) v->lfo_a = fm_lfo_draw(v);
                else if (lp->wave == 6u) { v->lfo_a = v->lfo_b; v->lfo_b = fm_lfo_draw(v); }
            }
            v->lfo_time = v->lfo_time <= UINT32_MAX - FM_CTL ? v->lfo_time + FM_CTL : UINT32_MAX;
        }
    }
    fm_peg_pitch(v, ((changed & FM_CHG_PEG) ? v->peg_mask : 0u)
                      | (lp->rate_millihz ? lp->mask_pitch : 0u));
    if (!(changed & FM_LFO_HOLD)) v->ctl = FM_CTL;
}

/*  at a note-on: the LFO from its start phase, its word from the
 * seed, its delay and fade from now, and its first tick at once. */
static void fm_lfo_start(fm_voice_t *v)
{
    const fm_lfo_patch_t *lp = &v->patch.lfo;
    const uint64_t fs = v->sample_rate_hz, den = fs * 1000u;
    const uint64_t r = lp->rate_millihz < 10u ? 10u : lp->rate_millihz > 50000u ? 50000u : lp->rate_millihz;
    const uint64_t dl = ((uint64_t)lp->delay_ms * fs + 500u) / 1000u, fd = ((uint64_t)lp->fade_ms * fs + 500u) / 1000u;
    v->lfo_step  = den ? (uint32_t)((((r * FM_CTL) << 32) + den / 2u) / den) : 0u;
    v->lfo_time  = 0u;
    v->lfo_delay = dl > UINT32_MAX ? UINT32_MAX : (uint32_t)dl;
    v->lfo_fade  = fd > UINT32_MAX ? UINT32_MAX : (uint32_t)fd;
    if (lp->free_run == 2u) fm_ctl_tick(v, FM_CHG_PEG | FM_LFO_HOLD | FM_MOD_NOTE);
    else {
        v->lfo_phase = (uint32_t)lp->phase_u16 << 16;
        v->lfo_word = FM_LFO_SEED;
        v->lfo_a = v->lfo_b = 0;
        if (lp->wave == 5u || lp->wave == 6u) v->lfo_a = fm_lfo_draw(v);
        if (lp->wave == 6u) v->lfo_b = fm_lfo_draw(v);
        fm_ctl_tick(v, FM_CHG_PEG | FM_MOD_NOTE);
    }
}

void fm_voice_init(fm_voice_t *v, const fm_patch_t *p, uint32_t sample_rate_hz)
{
    int i, j;
    v->sample_rate_hz = sample_rate_hz;
    v->patch.filter.cutoff_millihz = p->filter.cutoff_millihz;
    v->patch.filter.q_milli = p->filter.q_milli;
    v->patch.filter.mode = p->filter.mode;
    v->patch.filter.stages = p->filter.stages > 2u ? 2u : p->filter.stages;
    v->patch.route.ops = p->route.ops;                    /* explicit route copy avoids enlarged aggregate memcpy */
    v->patch.route.carriers = p->route.carriers;
    v->patch.vector.enabled = FM_OPS >= 4 && p->vector.enabled && p->route.ops == 4u && p->route.carriers == 15u;
    v->patch.vector.x_q15 = v->patch.vector.enabled ? (p->vector.x_q15 > 32768u ? 32768u : p->vector.x_q15) : 0u;
    v->patch.vector.y_q15 = v->patch.vector.enabled ? (p->vector.y_q15 > 32768u ? 32768u : p->vector.y_q15) : 0u;
    v->vector_x = v->patch.vector.x_q15; v->vector_y = v->patch.vector.y_q15;
    for (i = 0; i < FM_OPS; i++) for (j = 0; j < FM_OPS; j++) {
        const uint32_t kind = p->route.kind[i][j] <= 3u ? p->route.kind[i][j] : 0u;
        const uint32_t limit = kind ? 256u : 1024u;
        v->patch.route.kind[i][j] = (uint8_t)kind;
        v->patch.route.w[i][j] = kind == 3u && p->route.w[i][j] ? (uint16_t)256u                   /* : sync has no depth */
                               : p->route.w[i][j] > limit ? (uint16_t)limit : p->route.w[i][j];
    }
    v->patch.shape.kind = p->shape.kind <= 4u ? p->shape.kind : 0u;
    v->patch.shape.pos = p->shape.pos == 1u ? 1u : 0u;                 /*  */
    v->patch.shape.drive_db10 = p->shape.drive_db10 > 240u ? 240u : p->shape.drive_db10;
    v->shape_gain_q24 = (int32_t)((fm_exp2_q48(fm_db10_to_oct_q48(v->patch.shape.drive_db10))
                                + (INT64_C(1) << 23)) >> 24);
    v->patch.lfo = p->lfo;                                /*  */
    v->patch.peg = p->peg;                                /* : clamp before any arithmetic */
    v->patch.fenv = p->fenv;                              /*  */
    if (!v->patch.fenv.ref_millihz) v->patch.fenv.ref_millihz = 261626u;
    if (v->patch.fenv.attack_ms > 60000u) v->patch.fenv.attack_ms = 60000u;
    if (v->patch.fenv.decay_ms > 60000u) v->patch.fenv.decay_ms = 60000u;
    if (v->patch.fenv.release_ms > 60000u) v->patch.fenv.release_ms = 60000u;
    if (v->patch.fenv.sustain_permille > 1000u) v->patch.fenv.sustain_permille = 1000u;
    if (v->patch.fenv.depth_cents < -9600) v->patch.fenv.depth_cents = -9600;
    if (v->patch.fenv.depth_cents > 9600) v->patch.fenv.depth_cents = 9600;
    if (v->patch.fenv.track_permille > 2000u) v->patch.fenv.track_permille = 2000u;
    for (i = 0; i < 4; i++) {
        const int32_t level = p->peg.level_cents[i];
        v->patch.peg.level_cents[i] = (int16_t)(level < -4800 ? -4800 : level > 4800 ? 4800 : level);
        if (v->patch.peg.time_ms[i] > 60000u) v->patch.peg.time_ms[i] = 60000u;
        v->peg_n[i] = fm_ctl_ticks(v->patch.peg.time_ms[i], sample_rate_hz);
    }
    for (i = 0; i < FM_OPS; i++) {
        fm_op_t *o = &v->op[i];
        v->patch.op[i] = p->op[i];
        v->patch.eg[i] = p->eg[i];
        v->patch.ext[i].loop = p->ext[i].loop ? 1u : 0u;  /*   */
        v->patch.ext[i].ks_target = p->ext[i].ks_target <= 2u ? p->ext[i].ks_target : 0u;
        v->patch.ext[i].ks_curve = p->ext[i].ks_curve & 3u;
        v->patch.ext[i].fb_type = p->ext[i].fb_type & 3u;
        v->ks_db10[i] = 0; v->ks_boost[i] = 0u;
        const int ks = p->ks[i].enabled && p->op[i].wave == 0u && !p->wt[i].enabled && !p->pd[i].enabled;
        v->patch.pd[i].enabled = p->pd[i].enabled && p->op[i].wave == 0u && !p->wt[i].enabled && !p->ks[i].enabled;  /* : PD, a wavetable and Karplus-Strong disable each other */
        v->patch.pd[i].knee_q15 = v->patch.pd[i].enabled
            ? (p->pd[i].knee_q15 < 1u ? 1u : p->pd[i].knee_q15 > 32767u ? 32767u : p->pd[i].knee_q15) : 0u;
        o->pd_knee = 0;
        o->pd_slope[0] = o->pd_slope[1] = 0;
        fm_pd_set(o, v->patch.pd[i].knee_q15);
        v->patch.wt[i].enabled = p->wt[i].enabled && p->op[i].wave == 0u && !p->pd[i].enabled && !p->ks[i].enabled;
        v->patch.wt[i].bank = v->patch.wt[i].enabled ? p->wt[i].bank : 0u;
        v->patch.wt[i].position_q15 = !v->patch.wt[i].enabled ? 0u
            : p->wt[i].position_q15 > 32768u ? 32768u : p->wt[i].position_q15;
        o->wt_on = v->patch.wt[i].enabled;
        o->wt_bank = o->wt_a = o->wt_b = 0;               /* bound below by fm_assets_clear or a binding */
        o->wt_bits = o->wt_frames = o->wt_band = o->wt_frac = 0u;
        o->wt_pos = v->patch.wt[i].position_q15;
        {   /* : the buffer length clamps, then rounds down to a power of two */
            uint32_t b = p->ks[i].buffer < FM_KS_BUFFER_MIN ? FM_KS_BUFFER_MIN
                       : p->ks[i].buffer > FM_KS_BUFFER_MAX ? FM_KS_BUFFER_MAX : p->ks[i].buffer;
            while (b & (b - 1u)) b &= b - 1u;
            v->patch.ks[i].enabled = (uint8_t)ks;
            v->patch.ks[i].buffer = (uint16_t)(ks ? b : 0u);
            v->patch.ks[i].loss_q15 = !ks ? 0u : p->ks[i].loss_q15 > 32767u ? 32767u : p->ks[i].loss_q15;
            v->patch.ks[i].seed = !ks ? 0u : p->ks[i].seed ? p->ks[i].seed : 0x9E3779B9u;
        }
        o->ks_on = (uint32_t)ks;
        o->ks_buf = 0;                                    /* bound below by fm_assets_clear or a binding */
        o->ks_mask = o->ks_w = o->ks_n = o->ks_f = o->ks_word = 0u;
        o->ks_loss = v->patch.ks[i].loss_q15;
        o->ks_prev = 0;
        o->ks_inc = 0u;
        v->patch.noise[i].shift = p->noise[i].shift == 0u ? 4u : p->noise[i].shift > 15u ? 15u : p->noise[i].shift;
        if (v->patch.eg[i].delay_ms > 60000u) v->patch.eg[i].delay_ms = 60000u;
        if (v->patch.eg[i].left_slope_db10 > 1200u) v->patch.eg[i].left_slope_db10 = 1200u;
        o->decay2_on = p->eg[i].decay2_ms != 0u || p->eg[i].sustain2_db10 != 0u;
        if (v->patch.eg[i].decay2_ms > 60000u) v->patch.eg[i].decay2_ms = 60000u;
        if (v->patch.eg[i].sustain2_db10 > 1200u) v->patch.eg[i].sustain2_db10 = 1200u;
        if (o->decay2_on) {
            const uint16_t s1 = p->op[i].sustain_db10 > 1200u ? 1200u : p->op[i].sustain_db10;
            if (v->patch.eg[i].sustain2_db10 < s1) v->patch.eg[i].sustain2_db10 = s1;
        }
        o->phase = FM_PHASE_U16(p->op[i].phase_u16);     /* : where a free-running operator's first note starts */
        o->inc = 0; o->gain = 0; o->rate_g = 0;
        o->att = FM_ATT_END; o->rate_d = o->rate_r = 0; o->sus = 0;
        o->count = o->count_d = 0;
        o->count_a_wait = 0u;
        o->count_d2 = 0u; o->sus2 = o->rate_d2 = 0u;
        o->out = o->index = o->fb = o->prev = o->prev2 = 0;
        o->raw_prev = o->raw_prev2 = 0;
        o->loop_g0 = o->loop_rate = 0u; o->loop_count = 0u;
        o->stage = FM_STAGE_IDLE;
        o->noise = FM_NOISE_SEED[i];
        o->noise_index = 0u;
        if (p->op[i].free_run == 2u) {
            o->noise = fm_free_word(FM_NOISE_SEED[i], 0u);
            if (p->op[i].fixed_millihz) fm_set_inc(o, p->op[i].fixed_millihz, p->op[i].ratio_q16, sample_rate_hz);
        }
        if (p->op[i].wave == 9u) fm_pink_reset(o, FM_NOISE_SEED[i], p->op[i].free_run == 2u);
        else if (p->op[i].wave == 10u || p->op[i].wave == 11u) o->noise_state.colour = 0;
        for (j = 0; j < FM_OPS; j++) v->edge[i][j] = 0;
        v->lfo_vg[i] = v->lfo_ks[i] = 1 << 30;
        v->lfo_base[i] = 0u;
        v->peg_base_frac[i] = 0u;
        v->peg_base_whole[i] = 0u;
    }
    v->ctl = 0u;                                          /* : no tick before an LFO's note-on */
    v->lfo_phase = v->lfo_step = 0u;
    v->lfo_word = FM_LFO_SEED;
    v->lfo_a = v->lfo_b = 0;
    v->lfo_time = v->lfo_delay = v->lfo_fade = 0u;
    v->lfo_m = FP_ONE;
    v->lfo_cents = 0;
    v->peg.cur = v->peg.q = 0;
    v->peg.r = v->peg.n = v->peg.acc = v->peg.left = 0u;
    v->peg.dir = 0; v->peg.stage = 0u;
    v->fenv.cur = v->fenv.q = 0;
    v->fenv.r = v->fenv.n = v->fenv.acc = v->fenv.left = 0u;
    v->fenv.dir = 0; v->fenv.stage = 0u;
    v->fenv_n[0] = fm_ctl_ticks(v->patch.fenv.attack_ms, sample_rate_hz);
    v->fenv_n[1] = fm_ctl_ticks(v->patch.fenv.decay_ms, sample_rate_hz);
    v->fenv_n[2] = fm_ctl_ticks(v->patch.fenv.release_ms, sample_rate_hz);
    v->fenv_s = (int32_t)(((((uint64_t)v->patch.fenv.sustain_permille * 32768u + 500u) << 32) / 1000u) >> 32);
    v->fenv_track = 0;
    v->fenv_q = v->patch.filter.q_milli;
    v->fenv_ub = v->fenv_u = fm_svf_u(v->patch.filter.cutoff_millihz, sample_rate_hz);
    {   /* Widen before forming the upper endpoint: it can exceed UINT32_MAX mHz. */
        const uint64_t r = sample_rate_hz ? sample_rate_hz : 1u;
        v->fenv_ulo = fp_div((fixed_t)((r * 5u + 11u) / 12u), (fixed_t)(r * 1000u));
        v->fenv_uhi = fp_div((fixed_t)(r * 450u), (fixed_t)(r * 1000u));
    }
    v->ops = p->route.ops <= FM_OPS ? p->route.ops : FM_OPS;                 /* 's bounds (fm_route_t) */
    v->carriers = v->ops ? p->route.carriers & ((1u << v->ops) - 1u) : 1u;
    v->amp_edges = v->sync_edges = v->sync_wrap_prev = 0u;
    v->fb_special = 0u;                                   /* : one branch per operator-sample while zero */
    for (i = 0; i < fm_ops_stepped(v); i++)
        if (v->patch.ext[i].fb_type) v->fb_special = 1u;
    for (i = 0; i < (int)v->ops; i++) for (j = 0; j < (int)v->ops; j++)
        if (i != j && v->patch.route.w[i][j]) {
            if (v->patch.route.kind[i][j] == 3u)          /* : a Karplus-Strong source has no wrap to sync from */
                v->sync_edges |= (v->patch.ks[i].enabled ? 0u : 1u) << (4 * j + i);
            else if (v->patch.route.kind[i][j]) v->amp_edges = 1u;
        }
    v->peg_mask = fm_peg_on(v) ? (p->peg.mask ? p->peg.mask : (1u << fm_ops_stepped(v)) - 1u) : 0u;
    v->peg_mask &= (1u << fm_ops_stepped(v)) - 1u;
    for (i = 0; i < fm_ops_stepped(v); i++)
        if (v->patch.op[i].fixed_millihz) v->peg_mask &= ~(1u << i);
    v->svf.mode = p->filter.mode <= 4u ? p->filter.mode : 0u;
    v->svf2.mode = v->patch.filter.stages == 2u ? v->svf.mode : 0u;
    v->svf2.ic1 = v->svf2.ic2 = 0; v->svf2.idle = 0u;
    v->svf.ic1 = v->svf.ic2 = 0;
    v->svf.idle = 0;
    fm_svf_coef(&v->svf, p->filter.cutoff_millihz, p->filter.q_milli, sample_rate_hz);
    fm_filter_copy_coef(v);
    v->idle_samples = 0u; v->free_running = 0u; v->lfo_wrap = 0u;
    for (i = 0; i < fm_ops_stepped(v); i++)
        if (p->op[i].free_run == 2u) v->free_running = 1u;
    if (p->lfo.rate_millihz && p->lfo.free_run == 2u) {
        const uint64_t den = (uint64_t)sample_rate_hz * 1000u;
        const uint64_t r = p->lfo.rate_millihz < 10u ? 10u : p->lfo.rate_millihz > 50000u ? 50000u : p->lfo.rate_millihz;
        v->free_running = 1u; v->ctl = FM_CTL;
        v->lfo_step = den ? (uint32_t)((((r * FM_CTL) << 32) + den / 2u) / den) : 0u;
        v->lfo_phase = (uint32_t)p->lfo.phase_u16 << 16;
        fm_free_lfo_advance(v, 0u);
    }
    fm_formant_init(v, p);
    fm_mod_init(v, p);
    fm_assets_clear(v);                                   /* : init leaves nothing bound */
    v->patch.amp = p->amp;                                /*  */
    v->patch.amp.enabled = p->amp.enabled ? 1u : 0u;
    v->amp.gain = v->amp.rate_g = 0u;
    v->amp.att = FM_ATT_END; v->amp.rate_d = v->amp.rate_r = v->amp.sus = 0u;
    v->amp.count = v->amp.count_d = 0u;
    v->amp.stage = FM_STAGE_IDLE;
}

/* : the amp envelope's note-on, with the operators' helpers and no
 * velocity or rate scaling. It restarts from its own current gain at the
 * full attack's slope whenever it is not idle , whatever the
 * carriers do, and a running attack keeps its count and step . */
static void fm_amp_note_on(fm_voice_t *v)
{
    fm_amp_t *a = &v->amp;
    const fm_amp_patch_t *pp = &v->patch.amp;
    const uint32_t fs = v->sample_rate_hz;
    const fixed_t sus_q48 = fm_db10_to_oct_q48(pp->sustain_db10);
    a->sus     = fm_env_from_q48(sus_q48);
    a->count_d = fm_segment_samples(pp->decay_ms, fs);
    a->rate_d  = fm_rate(sus_q48, a->count_d);
    a->rate_r  = fm_rate((fixed_t)FM_REL_OCT << 48, fm_segment_samples(pp->release_ms, fs));
    if (a->stage != FM_STAGE_ATTACK) {
        const uint64_t g0 = a->stage == FM_STAGE_IDLE ? 0u : (uint64_t)fm_att_gain_q30(a->att) << 32;
        a->count  = fm_attack_samples(g0, fm_segment_samples(pp->attack_ms, fs));
        a->gain   = g0;
        a->rate_g = (FM_GAIN_PEAK - g0) / a->count;
        a->stage  = FM_STAGE_ATTACK;
    }
}

void fm_voice_note_on(fm_voice_t *v, uint32_t freq_millihz, uint16_t velocity_q15)
{
    const uint32_t fs = v->sample_rate_hz;
    const int retrig = fm_sounding(v);                     /* a filter's ring-out alone is not a sounding voice */
    const int32_t vel_q30 = fm_velocity_q30(velocity_q15);
    const int nops = fm_ops_stepped(v);
    int i;
    fm_free_catchup(v);
    if (!retrig) v->sync_wrap_prev = 0u;                   /* : a retrigger keeps the pending wraps */
    if (v->mod_active) {
        uint32_t j;
        const uint32_t vel = velocity_q15 > 32767u ? 32767u : velocity_q15;
        v->mod_velocity = (int32_t)(((((uint64_t)vel * 32768u + 16383u) << 32) / 32767u) >> 32);
        v->mod_note = fm_mod_normalize(fm_mod_clamp(fm_track_cents(freq_millihz, 440000u, 1000u), -9600, 9600), 9600u);
        if (!retrig) {
            for (j = 0; j < FM_MOD_SLOTS; j++) v->mod_cur[j] = 0;
            for (j = 0; j < FM_OPS; j++) v->mod_pitch[j] = v->mod_level[j] = v->mod_index[j] = v->mod_pd[j] = v->mod_wt[j] = 0;
            v->mod_cutoff = v->mod_q = 0;
        }
        fm_mod_lfo_reset(v);
        v->fenv_ub = fm_svf_u(v->patch.filter.cutoff_millihz, fs);
        v->fenv_q = v->patch.filter.q_milli;
    }
    for (i = 0; i < nops; i++) {
        const fm_op_patch_t *pp = &v->patch.op[i];
        fm_op_t *o = &v->op[i];
        const int delay = v->patch.eg[i].delay_ms && (!retrig || o->stage == FM_STAGE_DELAY);
        const uint64_t g0 = retrig ? fm_current_gain(o) : 0u;
        const int32_t  vg = fm_vel_gain_q30(vel_q30, pp->vel_sens_q8);
        const uint32_t rsx = fm_rs_oct_q24(freq_millihz, pp->rate_scale);   /* : from the note, a fixed operator's too */
        fixed_t sus_q48;
        fm_set_inc(o, pp->fixed_millihz ? pp->fixed_millihz : freq_millihz, pp->ratio_q16, fs);   /* : a fixed operator's increment ignores the note, pitch events, the pitch LFO and the pitch envelope */
        if (!retrig) {                                     /* a sounding voice keeps its phases and noise */
            o->prev = o->prev2 = 0;
            o->raw_prev = o->raw_prev2 = 0;
            if (!pp->free_run) {
                o->phase = FM_PHASE_U16(pp->phase_u16); o->noise = FM_NOISE_SEED[i];
                if (pp->wave == 9u) fm_pink_reset(o, FM_NOISE_SEED[i], 0);
                else if (pp->wave == 10u || pp->wave == 11u) o->noise_state.colour = 0;
            }   /* FREE and retriggers preserve the complete generator */
        }
        sus_q48    = fm_db10_to_oct_q48(pp->sustain_db10);
        o->sus     = fm_env_from_q48(sus_q48);
        o->count_d = fm_rs_count(fm_segment_samples(pp->decay_ms, fs), rsx);
        o->rate_d  = fm_rate(sus_q48, o->count_d);
        o->rate_r  = fm_rate((fixed_t)FM_REL_OCT << 48, fm_rs_count(fm_segment_samples(pp->release_ms, fs), rsx));
        if (o->decay2_on) {
            const fixed_t s2 = fm_db10_to_oct_q48(v->patch.eg[i].sustain2_db10);
            const uint32_t ms = v->patch.eg[i].decay2_ms;
            o->sus2 = fm_env_from_q48(s2);
            o->count_d2 = ms ? fm_rs_count(fm_segment_samples(ms, fs), rsx) : 0u;
            o->rate_d2 = o->count_d2 ? fm_rate(s2 - sus_q48, o->count_d2) : 0u;
        }
        if (!(retrig && o->stage == FM_STAGE_ATTACK)) {    /* : a running attack keeps its count and step */
            o->count  = fm_attack_samples(g0, fm_rs_count(fm_segment_samples(pp->attack_ms, fs), rsx));
            o->gain   = g0;
            o->rate_g = (FM_GAIN_PEAK - g0) / o->count;    /* g0 + (count-1)*rate <= peak; the last step lands on it */
            o->stage  = FM_STAGE_ATTACK;
            if (delay) {
                o->count_a_wait = o->count;
                o->count = fm_segment_samples(v->patch.eg[i].delay_ms, fs);
                o->stage = FM_STAGE_DELAY;
            }
        }
        o->loop_count = 0u;
        if (v->patch.ext[i].loop) {                        /* : the restart from the final sustain at the full attack's slope */
            const uint64_t lg0 = (uint64_t)fm_att_gain_q30(o->decay2_on ? o->sus2 : o->sus) << 32;
            o->loop_g0    = lg0;
            o->loop_count = fm_attack_samples(lg0, fm_rs_count(fm_segment_samples(pp->attack_ms, fs), rsx));
            o->loop_rate  = (FM_GAIN_PEAK - lg0) / o->loop_count;
        }
        v->lfo_vg[i] = vg;                                 /* : the gains an LFO's ticks apply again */
        v->lfo_ks[i] = v->patch.eg[i].left_slope_db10 && freq_millihz < pp->ks_break_millihz
                    ? fm_keyscale_q30(pp->ks_break_millihz, freq_millihz, v->patch.eg[i].left_slope_db10)
                    : fm_keyscale_q30(freq_millihz, pp->ks_break_millihz, pp->ks_slope_db10);
        v->ks_db10[i] = 0; v->ks_boost[i] = 0u;
        if (v->patch.ext[i].ks_target || v->patch.ext[i].ks_curve) fm_ks_ext(v, i, freq_millihz);
        if ((v->mod_pitch_mask >> i) & 1u)
            fm_mod_base(v, i, freq_millihz);
        else if ((v->peg_mask >> i) & 1u)
            fm_peg_base(v, i, freq_millihz);
        else if (v->patch.lfo.rate_millihz != 0u && (((uint32_t)v->patch.lfo.mask_pitch >> i) & 1u) && !pp->fixed_millihz)
            v->lfo_base[i] = fm_base_inc(freq_millihz, pp->ratio_q16, fs);
        o->out     = fm_mul_q30(fm_level_q30(fm_ks_level(v, i, pp->level_db10)), vg);
        o->index   = fm_op_index(v, i, pp->index_q10);
        o->fb      = fm_turns_q24(pp->feedback_q10);
    }
    fm_set_edges(v);                                       /* : each edge's index from this note's */
    if (v->patch.amp.enabled) fm_amp_note_on(v);           /*  */
    if (fm_peg_on(v)) {
        if (!retrig) v->peg.cur = v->patch.peg.level_cents[3];
        fm_peg_enter(v, 1u);
    }
    if (fm_fenv_on(v)) {
        if (!retrig) v->fenv.cur = 0;
        if (!retrig || v->fenv.stage != 1u) {
            const uint32_t n = v->fenv_n[0] - (uint32_t)(((uint64_t)v->fenv.cur * v->fenv_n[0]) >> 15);
            fm_fenv_enter(v, 1u, n);
        }
        v->fenv_track = fm_track_cents(freq_millihz, v->patch.fenv.ref_millihz, v->patch.fenv.track_permille);
        v->fenv_ub = fm_svf_u(v->patch.filter.cutoff_millihz, fs);
        v->fenv_q = v->patch.filter.q_milli;
        if (!v->mod_active) fm_fenv_apply(v, 1);
    }
    for (i = 0; i < FM_FORMANTS; i++) if ((v->formant_mask >> i) & 1u) {
        v->formant[i].svf.idle = 0u;
        if (!v->mod_active) fm_formant_apply(v, (unsigned)i);
    }
    if (v->patch.lfo.rate_millihz != 0u) fm_lfo_start(v);   /* : at every note-on, its first tick now */
    else if (fm_peg_on(v) || fm_fenv_on(v) || v->mod_active) fm_ctl_tick(v, FM_CHG_PEG | FM_MOD_NOTE); /* apply without stepping */
    if (v->svf.mode) {                                     /* : new coefficients, the state carries on */
        if (!v->mod_active && !fm_fenv_on(v)) fm_svf_coef(&v->svf, v->patch.filter.cutoff_millihz, v->patch.filter.q_milli, fs);
        fm_filter_copy_coef(v);
        v->svf.idle = 0;
    }
    for (i = 0; i < nops; i++) {                           /* : after the final increments, refill */
        fm_op_t *o = &v->op[i];
        if (!o->ks_buf) continue;
        if (!retrig || !o->ks_word) {                      /* a sounding voice continues its word, once seeded */
            o->ks_word = v->patch.ks[i].seed ^ FM_NOISE_SEED[i];
            if (!o->ks_word) o->ks_word = 0x9E3779B9u;
        }
        fm_ks_fill(o);
    }
}

void fm_voice_pitch(fm_voice_t *v, uint32_t freq_millihz)
{
    const int nops = fm_ops_stepped(v);
    int i;
    fm_free_catchup(v);
    for (i = 0; i < nops; i++) {
        if (v->patch.op[i].fixed_millihz) continue;        /* : a fixed operator's increment ignores the note, pitch events, the pitch LFO and the pitch envelope */
        if (v->ctl != 0u && ((v->mod_pitch_mask >> i) & 1u)) fm_mod_base(v, i, freq_millihz);
        else if (v->ctl != 0u && ((v->peg_mask >> i) & 1u)) fm_peg_base(v, i, freq_millihz);
        else if (v->ctl != 0u && v->patch.lfo.rate_millihz && (((uint32_t)v->patch.lfo.mask_pitch >> i) & 1u)) {   /* : the LFO's last pitch factor on the new note */
            v->lfo_base[i] = fm_base_inc(freq_millihz, v->patch.op[i].ratio_q16, v->sample_rate_hz);
            fm_set_inc_m(&v->op[i], v->lfo_base[i], v->lfo_m);
        }
        else fm_set_inc(&v->op[i], freq_millihz, v->patch.op[i].ratio_q16, v->sample_rate_hz);
    }
    if (v->ctl != 0u) {
        if (v->mod_active) fm_mod_pitch_apply(v);
        else fm_peg_pitch(v, v->peg_mask);
    }
}

/* Release from wherever each operator is. In the attack the linear gain is
 * converted to octaves once, here, so the release starts from the same level. */
void fm_voice_note_off(fm_voice_t *v)
{
    const int nops = fm_ops_stepped(v);
    int i;
    if (v->peg.stage >= 1u && v->peg.stage <= 4u) {
        const int32_t before = v->peg.cur;
        fm_peg_enter(v, 5u);
        if (v->peg.cur != before) {
            if (v->mod_active) fm_mod_pitch_apply(v);
            else fm_peg_pitch(v, v->peg_mask);
        }
    }
    if (v->fenv.stage >= 1u && v->fenv.stage <= 3u) {
        fm_fenv_enter(v, 4u, v->fenv_n[2]);
        if (!v->fenv_n[2]) fm_fenv_apply(v, 1);           /* zero release applies now, without a clock tick */
    }
    for (i = 0; i < nops; i++) {
        fm_op_t *o = &v->op[i];
        if (o->stage == FM_STAGE_DELAY) {
            o->stage = FM_STAGE_IDLE; o->gain = 0u; o->att = FM_ATT_END;
            o->count = o->count_a_wait = 0u;
            continue;
        }
        if (o->stage == FM_STAGE_ATTACK) o->att = fm_env_from_q48(fm_gain_to_oct_q48(o->gain));
        if (o->stage != FM_STAGE_IDLE) o->stage = FM_STAGE_RELEASE;
    }
    if (v->patch.amp.enabled) {                            /* : the operators' conversion */
        fm_amp_t *a = &v->amp;
        if (a->stage == FM_STAGE_ATTACK) a->att = fm_env_from_q48(fm_gain_to_oct_q48(a->gain));
        if (a->stage != FM_STAGE_IDLE) a->stage = FM_STAGE_RELEASE;
    }
}

void fm_voice_filter(fm_voice_t *v, uint32_t cutoff_millihz, uint16_t q_milli)
{
    if (v->mod_active && v->svf.mode) {
        v->fenv_ub = fm_svf_u(cutoff_millihz, v->sample_rate_hz);
        v->fenv_q = q_milli;
        fm_mod_filter(v, 1);
    }
    else if (fm_fenv_on(v)) {
        v->fenv_ub = fm_svf_u(cutoff_millihz, v->sample_rate_hz);
        v->fenv_q = q_milli;
        fm_fenv_apply(v, 1);
    }
    else if (v->svf.mode) {
        fm_svf_coef(&v->svf, cutoff_millihz, q_milli, v->sample_rate_hz);
        fm_filter_copy_coef(v);
    }
}

void fm_voice_ctl(fm_voice_t *v, uint32_t n, int32_t value_q15)
{
    if (n < FM_CTL_INPUTS) v->ctl_input[n] = fm_mod_clamp(value_q15, -32768, 32768);
}

void fm_voice_gen(fm_voice_t *v, uint32_t n, int32_t value_q15)
{
    if (n < FM_GEN_INPUTS) v->gen_input[n] = fm_mod_clamp(value_q15, -32768, 32768);
}

int fm_voice_active(const fm_voice_t *v)
{
    return fm_sounding(v) || (v->svf.ic1 | v->svf.ic2 | v->svf2.ic1 | v->svf2.ic2) != 0
        || (v->formant_mask && fm_formant_active(v))
        || (v->patch.amp.enabled && v->amp.stage != FM_STAGE_IDLE);
}

int fm_voice_needs_clock(const fm_voice_t *v)
{
    return v->free_running != 0u;
}

/* : a byte range [addr, addr + bytes) whose end does not wrap. */
static int fm_range_fits(uintptr_t addr, uint64_t bytes)
{
    return bytes <= (uint64_t)(UINTPTR_MAX - addr);
}

/* Two fitting byte ranges share a byte; empty ranges meet nothing. */
static int fm_ranges_meet(uintptr_t a, uint64_t na, uintptr_t b, uint64_t nb)
{
    return na && nb && (uint64_t)a < (uint64_t)b + nb && (uint64_t)b < (uint64_t)a + na;
}

/* : each wavetable operator reads its slot's bound bank at the band
 * for its increment; an unbound or out-of-range slot leaves it silent. */
static void fm_wt_attach(fm_voice_t *v)
{
    int i;
    for (i = 0; i < FM_OPS; i++) {
        fm_op_t *o = &v->op[i];
        const uint32_t s = v->patch.wt[i].bank;
        o->wt_bank = 0;
        if (o->wt_on && s < FM_WT_SLOTS && v->wt_data[s]) {
            o->wt_bank = v->wt_data[s];
            o->wt_frames = v->wt_frames[s];
            o->wt_bits = 5u;
            while ((1u << o->wt_bits) < v->wt_length[s]) o->wt_bits++;
            o->wt_band = FM_WT_BANDS;                     /* differs from every band: points the frames */
            fm_wt_band(o);
        } else fm_wt_point(o);
    }
}

/* : each Karplus-Strong operator takes its own view when it holds the
 * patch's length; the write index and previous read reset and the delay is
 * tuned from the current increment. The samples stay unwritten, and the
 * operator silent, until a note-on fills them. */
static void fm_ks_attach(fm_voice_t *v)
{
    int i;
    for (i = 0; i < FM_OPS; i++) {
        fm_op_t *o = &v->op[i];
        const uint32_t b = v->patch.ks[i].buffer;
        o->ks_buf = o->ks_on && v->ks_data[i] && v->ks_capacity[i] >= b ? v->ks_data[i] : 0;
        o->ks_mask = o->ks_buf ? b - 1u : 0u;
        o->ks_w = o->ks_word = 0u; o->ks_prev = 0;
        o->ks_n = o->ks_f = 0u; o->ks_inc = 0u;
        if (o->ks_buf) fm_ks_tune(o);
    }
}

static void fm_assets_clear(fm_voice_t *v)
{
    int i;
    for (i = 0; i < FM_WT_SLOTS; i++) { v->wt_data[i] = 0; v->wt_frames[i] = v->wt_length[i] = 0u; }
    for (i = 0; i < FM_OPS; i++) { v->ks_data[i] = 0; v->ks_capacity[i] = 0u; }
    fm_wt_attach(v);
    fm_ks_attach(v);
}

int fm_voice_bind_assets(fm_voice_t *v, const fm_assets_t *a)
{
    const uintptr_t va = (uintptr_t)v;
    const uint64_t vn = sizeof *v;
    uint32_t i, j, wn, kn;
    if (fm_voice_active(v)) return FM_BIND_ACTIVE;
    if (!a) { fm_assets_clear(v); return FM_BIND_OK; }
    if (a->size < sizeof *a) return FM_BIND_METADATA;
    if (a->size > sizeof *a) return FM_BIND_UNSUPPORTED;
    wn = a->wt_count; kn = a->ks_count;
    if (wn > FM_WT_SLOTS || kn > FM_OPS || (wn && !a->wt) || (kn && !a->ks)) return FM_BIND_METADATA;
    for (i = 0; i < wn; i++) {
        const fm_wt_view_t *w = &a->wt[i];
        const uintptr_t wa = (uintptr_t)w->data;
        const uint64_t wb = (uint64_t)w->capacity * sizeof *w->data;
        if (w->frames < 1u || w->frames > FM_WT_FRAMES_MAX || w->bands != FM_WT_BANDS
            || w->length < FM_WT_LENGTH_MIN || w->length > FM_WT_LENGTH_MAX
            || (w->length & (w->length - 1u)) || !w->data || !fm_range_fits(wa, wb))
            return FM_BIND_METADATA;
        if (w->capacity < w->frames * w->length * FM_WT_BANDS) return FM_BIND_CAPACITY;
        if ((wa & (sizeof *w->data - 1u)) || fm_ranges_meet(wa, wb, va, vn)) return FM_BIND_OVERLAP;
    }
    for (i = 0; i < kn; i++) {
        const fm_ks_view_t *k = &a->ks[i];
        const uintptr_t ka = (uintptr_t)k->data;
        const uint64_t kb = (uint64_t)k->capacity * sizeof *k->data;
        if (!k->data != !k->capacity || !fm_range_fits(ka, kb)) return FM_BIND_METADATA;
        if (!k->data) continue;
        if (v->patch.ks[i].enabled && k->capacity < v->patch.ks[i].buffer) return FM_BIND_CAPACITY;
        if ((ka & (sizeof *k->data - 1u)) || fm_ranges_meet(ka, kb, va, vn)
            || fm_ranges_meet(ka, kb, (uintptr_t)a, a->size)
            || fm_ranges_meet(ka, kb, (uintptr_t)a->wt, (uint64_t)wn * sizeof *a->wt)
            || fm_ranges_meet(ka, kb, (uintptr_t)a->ks, (uint64_t)kn * sizeof *a->ks))
            return FM_BIND_OVERLAP;
        for (j = 0; j < wn; j++)
            if (fm_ranges_meet(ka, kb, (uintptr_t)a->wt[j].data, (uint64_t)a->wt[j].capacity * sizeof *a->wt[j].data))
                return FM_BIND_OVERLAP;
        for (j = 0; j < i; j++)
            if (fm_ranges_meet(ka, kb, (uintptr_t)a->ks[j].data, (uint64_t)a->ks[j].capacity * sizeof *a->ks[j].data))
                return FM_BIND_OVERLAP;
    }
    for (i = 0; i < FM_WT_SLOTS; i++) {
        v->wt_data[i] = i < wn ? a->wt[i].data : 0;
        v->wt_frames[i] = (uint16_t)(i < wn ? a->wt[i].frames : 0u);
        v->wt_length[i] = (uint16_t)(i < wn ? a->wt[i].length : 0u);
    }
    for (i = 0; i < FM_OPS; i++) {
        v->ks_data[i] = i < kn ? a->ks[i].data : 0;
        v->ks_capacity[i] = i < kn ? a->ks[i].capacity : 0u;
    }
    fm_wt_attach(v);
    fm_ks_attach(v);
    return FM_BIND_OK;
}

/* : one band, frame and sample of a generated bank in Q2.30: the
 * partials of that frame with numbers up to keep, each the integer sine at
 * number * n / length turns plus its phase, scaled by its dB10 gain. With
 * at most 8192 partials the sum stays below 2^43 in magnitude. */
static int64_t fm_wt_sum(const fm_wt_harmonic_t *h, uint32_t count, uint32_t frame,
                         uint32_t keep, uint32_t n, uint32_t bits)
{
    int64_t sum = 0;
    uint32_t i;
    for (i = 0; i < count; i++)
        if (h[i].frame == frame && h[i].number <= keep) {
            const uint32_t p = (((uint32_t)h[i].number * n) << (32u - bits)) + ((uint32_t)h[i].phase_u16 << 16);
            sum += fm_mul_q30(fm_sine_q30(p), fm_level_q30(h[i].level_db10));
        }
    return sum;
}

int fm_wt_additive(int16_t *data, uint32_t capacity, uint32_t frames, uint32_t length,
                   const fm_wt_harmonic_t *h, uint32_t count)
{
    uint64_t den = UINT64_C(1) << 30;
    uint32_t bits = 5u, k, f, n, i, pass;
    if (frames < 1u || frames > FM_WT_FRAMES_MAX || length < FM_WT_LENGTH_MIN || length > FM_WT_LENGTH_MAX
        || (length & (length - 1u)) || !data || (count && !h) || count > frames * 1024u)
        return FM_BIND_METADATA;
    for (i = 0; i < count; i++)
        if (h[i].frame >= frames || h[i].number < 1u || h[i].number > 1024u || h[i].level_db10 > 1200u)
            return FM_BIND_METADATA;
    if (capacity < FM_WT_BANDS * frames * length) return FM_BIND_CAPACITY;
    while ((1u << bits) < length) bits++;
    /* Pass 0 finds the bank-wide denominator max(2^30, M); pass 1 writes
     * sign(a) * floor((|a| * 32767 + den/2) / den), at most 32767. */
    for (pass = 0; pass < 2u; pass++)
        for (k = 0; k < FM_WT_BANDS; k++) {
            const uint32_t keep = (1u << k) < length / 2u ? 1u << k : length / 2u - 1u;   /* h <= 2^k, h < length/2 */
            for (f = 0; f < frames; f++)
                for (n = 0; n < length; n++) {
                    const int64_t a = fm_wt_sum(h, count, f, keep, n, bits);
                    const uint64_t m = (uint64_t)(a < 0 ? -a : a);
                    if (!pass) { if (m > den) den = m; continue; }
                    {
                        const int32_t q = (int32_t)((m * 32767u + den / 2u) / den);
                        data[((k * frames + f) << bits) + n] = (int16_t)(a < 0 ? -q : q);
                    }
                }
        }
    return FM_BIND_OK;
}

/* ------------------------------------------------------------------ */
/*  Test surface                                                       */
/* ------------------------------------------------------------------ */

static uint64_t fm_fnv1a_i32(uint64_t h, const int32_t *w, uint32_t n)
{
    uint32_t i;
    int k;
    for (i = 0; i < n; i++) {
        uint32_t u = (uint32_t)w[i];
        for (k = 0; k < 4; k++) {
            h ^= (u >> (8 * k)) & 0xFFu;
            h *= 1099511628211ULL;
        }
    }
    return h;
}

uint64_t fm_golden_hash(const int16_t *pcm, uint32_t frames)
{
    uint64_t h = 14695981039346656037ULL;
    uint32_t i;
    for (i = 0; i < frames; i++) {
        uint16_t u = (uint16_t)pcm[i];
        h ^= u & 0xFFu;        h *= 1099511628211ULL;
        h ^= (u >> 8) & 0xFFu; h *= 1099511628211ULL;
    }
    return h;
}

uint32_t fm_table_bytes(void)
{
    return (uint32_t)(sizeof fm_sine_tbl + sizeof fm_exp2_tbl);
}

uint64_t fm_table_hash(void)
{
    uint64_t h = 14695981039346656037ULL;
    h = fm_fnv1a_i32(h, fm_sine_tbl, (uint32_t)(sizeof fm_sine_tbl / sizeof fm_sine_tbl[0]));
    h = fm_fnv1a_i32(h, fm_exp2_tbl, (uint32_t)(sizeof fm_exp2_tbl / sizeof fm_exp2_tbl[0]));
    return h;
}
