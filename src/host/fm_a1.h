/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * fm_a1.h — integer FM voice API. Supports the fixed two-operator path
 * or up to FM_OPS operators through an explicit patch routing table.
 *
 * Arithmetic: Q16.48 fp_math.h at note events and at a voice's
 * control ticks (every FM_CTL sounding samples, only for a patch
 * with an LFO, pitch envelope, filter envelope or an active modulation slot).
 * The per-sample path uses 32- and 64-bit adds, shifts,
 * table lookups and 32x32->64 multiplies; no division, no 64x64 multiply,
 * no floating point, no heap, no mutable globals.
 */
#ifndef FM_A1_H
#define FM_A1_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifndef FM_SINE_BITS
#define FM_SINE_BITS 10        /* sine table: 1 << FM_SINE_BITS entries of Q2.30 */
#endif
#ifndef FM_EXP_BITS
#define FM_EXP_BITS 8          /* exp2 table: (1 << FM_EXP_BITS) + 1 entries of Q2.30 */
#endif
#ifndef FM_PHASE64
#define FM_PHASE64 0           /* 1: 64-bit phase accumulators (a 64-bit add per operator) */
#endif
#ifndef FM_ENV64
#define FM_ENV64 0             /* 1: 64-bit envelope accumulators, Q8.56 (a 64-bit add per operator) */
#endif
#ifndef FM_FB_AVG
#define FM_FB_AVG 1            /* 1: feedback from the average of the last two outputs ; 0: from the last one */
#endif
#ifndef FM_OPS
#define FM_OPS 4               /* operators per voice; supported maximum is 4 */
#endif
#if FM_OPS < 2 || FM_OPS > 4
#error "FM_OPS must be 2 to 4: the fixed path needs two operators and the library supports at most four"
#endif
#ifndef FM_CTL
#define FM_CTL 64              /* sounding samples per control tick; LFO updates increments, levels and indices */
#endif
#if FM_CTL < 1 || FM_CTL > 4096
#error "FM_CTL must be 1 to 4096 samples"
#endif

#define FM_REL_OCT     16u     /* release rate: peak to 2^-16 (-96.3 dB) in release_ms */
#define FM_ATT_END_OCT 20u     /* release ends, and the operator idles, at 2^-20 (-120.4 dB) */
#define FM_SEG_MAX     ((1u << 27) - 1u)  /* longest attack, decay or release, in samples */

/* Human-unit operator. Without a route (route.ops 0, the fixed
 * 2-operator path) op[0] is the carrier and op[1] the modulator: the
 * carrier's index, feedback and key scaling, the modulator's output level
 * and every operator above op[1] are unused. With a route, the level is
 * used where the operator is a carrier, the index on each of its edges and
 * the feedback on the operator itself. The last four fields, the rate
 * scaling and the fixed frequency, start phase and free run, apply to
 * every operator the voice steps; all four 0, the operator renders what it
 * rendered before they existed. */
typedef struct {
    uint32_t ratio_q16;      /* frequency ratio to the note, Q16.16 (65536 = 1.0) */
    uint16_t level_db10;     /* output level into the mix, tenths of a dB below full scale, clamped at 1200 */
    uint16_t index_q10;      /* modulation index into the operators it modulates, radians Q6.10 */
    uint16_t feedback_q10;   /* self-modulation index, radians Q6.10 */
    uint16_t vel_sens_q8;    /* velocity sensitivity of level and index: 0 fixed to 256 fully tracking */
    uint16_t attack_ms;      /* silence to peak, linear in amplitude, round(ms * fs / 1000) samples, 1 to FM_SEG_MAX; from a higher gain at the same slope  */
    uint16_t decay_ms;       /* peak to sustain, linear in dB, same sample-count rule */
    uint16_t sustain_db10;   /* sustain, tenths of a dB below peak (600 = -60 dB), clamped at 1200 */
    uint16_t release_ms;     /* peak to -96 dB, linear in dB; from a lower level proportionally shorter */
    uint32_t ks_break_millihz; /* index key scaling : notes above this frequency get a smaller index */
    uint16_t ks_slope_db10;  /* tenths of a dB per octave above the break, set at note-on, clamped at 1200 in all; 0 off */
    uint16_t wave;           /* 0 sine, 1 half, 2 absolute, 3 quarter, 4 noise, 5 alternating sine, 6 camel, 7 square, 8 eight-octave log-saw, 9 pink, 10 colour LP, 11 colour HP; others play as 0 */
    uint16_t rate_scale;     /*  rate scaling, 0 off to 7 (above 7 plays as 7): at note-on each segment's sample count n becomes max(1, round(n * 2^(-oct * RS / 8))), oct = log2(note / 27500 mHz) clamped to 0..8 */
    uint32_t fixed_millihz;  /* : nonzero, the operator's frequency before the ratio whatever the note; pitch events leave it, rate and key scaling still follow the note; 0 the note's */
    uint16_t phase_u16;      /* : the phase in 1/65536 turns at fm_voice_init and at a note-on from silence (0 before ) */
    uint16_t free_run;       /* : 0 key sync, 1 frozen while idle, 2 continuous; other nonzero values play as 1. Retriggers keep phase either way. */
} fm_op_patch_t;

/*  voice filter: a state-variable filter (the trapezoidal form) on the
 * carrier's output, set at note-on from these values and changed by
 * fm_voice_filter until the next note-on. All zero: no filter, and the
 * voice renders what it rendered without one. */
typedef struct {
    uint32_t cutoff_millihz; /* clamped to fs/2400.. 0.45 fs (fs * 5/12.. fs * 450 mHz, the lower rounded up) */
    uint16_t q_milli;        /* resonance Q in thousandths, clamped to 500.. 16000 */
    uint16_t mode;           /* 0 off, 1 low-pass, 2 band-pass, 3 high-pass, 4 notch; others play as 0 */
    uint16_t stages;         /* 0/1: one stage; 2 or above: two identical stages */
} fm_filter_patch_t;

/* Per-patch routing table. ops 0 keeps the fixed
 * 2-operator path, byte for byte the output it had before routes existed.
 * Otherwise op ops-1 down to op 0 are evaluated each sample. Op i's phase
 * modulation is its own feedback_q10 through the average, as on the
 * fixed path's modulator, plus one term per nonzero w[j][i]: op j's output
 * times op j's index times w[j][i] / 256, with the index fixed at note-on
 * (velocity and key scaling applied) and the output this sample's when
 * j > i, the average of its last two when j < i (a back edge, one
 * sample late). The output is the sum of the carriers' outputs at their
 * levels, in int64, saturated to +-(2^31 - 1) in Q2.30 ( (a)). Every
 * value is defined : ops above FM_OPS play as FM_OPS and 1 as a lone
 * operator; carrier bits at or above ops are ignored, and a route without
 * a carrier below ops is a silent voice; weights above 1024 play as 1024;
 * the diagonal is never read.  kinds 1 AM and 2 ring use the same
 * source timing, before carrier level and independent of source index.
 * Their weights clamp to 256 and their sources to +-1 in Q2.30. Incoming
 * amplitude edges apply in ascending source index after the target's
 * waveform/envelope; its history retains that final sample. Kind 3 is
 * hard sync: a nonzero weight plays as 256
 * and 0 disables the edge. A source's natural wrap (its phase after its
 * add below its phase before) resets the target to its PHASE_U16 before
 * the target's own add, in the same sample for a source above the target
 * and in the next sounding sample for one below; a forced reset is not a
 * wrap, no overshoot is carried, several sources give one reset, and a
 * sync edge adds no PM and no gain. Other kinds play as PM. */
typedef struct {
    uint8_t  ops;                 /* 0 the fixed 2-operator path; else the operators used, 2 to FM_OPS */
    uint8_t  carriers;            /* bit i: op i sums into the voice output */
    uint16_t w[FM_OPS][FM_OPS];   /* w[from][to], Q8.8: 256 = unity, at most 1024, 0 no edge; the diagonal is unused */
    uint8_t kind[FM_OPS][FM_OPS]; /* 0 PM, 1 AM, 2 ring, 3 sync; others play as PM */
} fm_route_t;

/* LFO, one per voice. rate 0
 * is no LFO: its work is skipped even when another control source arms
 * the clock. Otherwise the LFO's 32-bit phase starts
 * at phase_u16 at every note-on by default (key sync, a retrigger included) and steps
 * once per control tick by round(rate * FM_CTL * 2^32 / (1000 fs)), rate
 * clamped to 10.. 50000 mHz, modulo 2^32 (where rate * FM_CTL >= 1000 fs
 * a tick spans whole turns, and waves 5 and 6 miss those wraps). Its
 * value l, Q1.15 (-32768.. 32767), comes
 * from the wave: 0 sine (the sine table), 1 triangle (0 at phase 0, rising
 * first), 2 saw up, 3 saw down, 4 square (32767 in the first half turn,
 * -32768 in the second), 5 sample and hold, 6 smoothed random (5 and 6
 * from a  xorshift32 word seeded at the note-on, a new value at each
 * wrap of the phase, 6 interpolating linearly from the last value to the
 * new one); FREE 2 instead derives both endpoints from the seed and a
 * wrap counter modulo 2^32. Its phase and grid run from voice init,
 * including silent samples, with closed-form idle catch-up. Delay and
 * fade still restart on every note-on. Other waves play as 0. The depths
 * are scaled by g, Q1.15, 0 for
 * delay_ms, then rising linearly to 32768 over fade_ms (fade 0: a step to
 * full depth). Each tick, with each depth first scaled as d * g >> 15, for
 * the operators whose bit is set in the masks (bit i: op i; masks are
 * defaulted by the timeline grammar, 0 here is none): pitch, cents =
 * pitch_cents * l >> 15 (pitch_cents clamped at 1200), the increment of a
 * non-fixed operator times 2^(cents / 1200) (fm_exp2_q48); level, the
 * attenuation level_db10 * (32767 - l) >> 16 tenths of a dB (level_db10
 * clamped at 1200) added to the operator's level_db10 before its clamp;
 * index, index_q10 * l >> 15 added to the operator's index_q10 before its
 * velocity and key-scaling gains, the sum clamped at 0, and a route's
 * edges from it set again. Envelopes, segment lengths and feedback never
 * move at control rate. */
typedef struct {
    uint32_t rate_millihz;   /* 0 no LFO; else clamped to 10.. 50000 */
    uint16_t wave;           /* 0 sine, 1 triangle, 2 saw up, 3 saw down, 4 square, 5 sample and hold, 6 smoothed random */
    uint16_t delay_ms;       /* no modulation for round(ms * fs / 1000) sounding samples after the note-on */
    uint16_t fade_ms;        /* then the depths rise linearly over round(ms * fs / 1000) sounding samples */
    uint16_t pitch_cents;    /* peak pitch offset, cents, clamped at 1200 */
    uint16_t level_db10;     /* peak level attenuation, tenths of a dB, clamped at 1200 */
    uint16_t index_q10;      /* peak index offset, radians Q6.10 */
    uint16_t phase_u16;      /* phase at key-synced note-on or FREE 2 voice init, 1/65536 turns */
    uint8_t  mask_pitch;     /* bit i: op i takes the pitch offset (a fixed-frequency operator never does) */
    uint8_t  mask_level;     /* bit i: op i takes the level attenuation */
    uint8_t  mask_index;     /* bit i: op i takes the index offset */
    uint8_t  free_run;       /* : 2 continuous from voice init; other values key sync. Text accepts only 0 or 2. */
} fm_lfo_patch_t;

/* Four-stage pitch envelope. All-zero
 * levels disable it regardless of times and mask. From silence it starts
 * at L4, glides to L1, L2 and L3, holds L3, then releases to L4. A
 * retrigger starts the full T1 from the current position. Times round
 * half up to control ticks (FM_CTL samples); zero ticks jump and chain.
 * The integer-cent offset and LFO cents are converted separately to
 * Q16.48 octaves, summed, then exponentiated once. The Nyquist clamp is
 * after that factor. A fixed-frequency operator ignores both sources. */
typedef struct {
    int16_t  level_cents[4]; /* L1..L4, clamped to -4800..4800 */
    uint16_t time_ms[4];     /* T1..T4, clamped to 0..60000 */
    uint16_t mask;           /* bit i follows; 0 all; absent/fixed operator bits ignored */
} fm_peg_patch_t;

/* : envelope and static key tracking for the voice filter.
 * Enabled when depth or tracking is nonzero and the filter is enabled.
 * The envelope is linear in Q1.15 gain, not decibels; its cents offset
 * adds to key tracking before the cutoff factor and  clamps.
 * A running attack continues through retrigger; otherwise it restarts
 * with the full attack slope. Tracking resets at note-on only. */
typedef struct {
    uint32_t ref_millihz;       /* 0 defaults to 261626 (C4) */
    uint16_t attack_ms, decay_ms, release_ms; /* clamped to 0..60000 */
    uint16_t sustain_permille;  /* clamped to 0..1000, 1000 full */
    int16_t  depth_cents;       /* clamped to -9600..9600 */
    uint16_t track_permille;    /* clamped to 0..2000, 1000 one octave per octave */
} fm_fenv_patch_t;

/* the exact integer glide: after j ticks start + sign(d)*floor(j*|d|/n).
 * Shared control-envelope state; q is truncated toward zero and the
 * remainder is carried, so the final tick lands exactly on its target. */
typedef struct {
    int32_t  cur, q;
    uint32_t r, n, acc, left;
    int8_t   dir;
    uint8_t  stage;
} fm_glide_t;

/*  operator extension. A second decay is enabled by either its time or
 * target, before the target is raised to sustain1. Left scaling attenuates
 * only index below the existing key break, selected at note-on (the * fm_op_ext_t can move either side to the level or make it a boost). */
typedef struct {
    uint16_t delay_ms;       /* absolute delay on a silent start, clamped to 60000; 0 bypasses */
    uint16_t decay2_ms;      /* rate-scaled, clamped to 60000; enabled zero time jumps to sustain2 */
    uint16_t sustain2_db10;  /* clamped to sustain1..1200; a zeroed extension stays disabled */
    uint16_t left_slope_db10; /* dB10 per octave below ks_break_millihz, clamped to 1200; 0 off */
} fm_eg_extra_t;

typedef struct {
    uint16_t shift;         /* waves 10/11: alpha=2^-shift, 0 defaults to 4, above 15 clamps */
} fm_noise_patch_t;

/* : memoryless shape after the voice filter, including ring-out.
 * : pos 1 moves the same shaper before the formant bank and the
 * filter (drive into a resonance); kind 4 clips at +1 and -1/2 with no
 * make-up gain. Every kind maps 0 to 0. Spatial : per voice
 * before space/, unchanged by position. */
typedef struct {
    uint16_t drive_db10;    /* 0..240, positive gain before shaping */
    uint16_t kind;          /* 0 bypass, 1 hard, 2 cubic, 3 fold, 4 asymmetric; invalid bypasses */
    uint16_t pos;           /* 0 after the filter, 1 before formants and filter; other values 0 */
} fm_shape_patch_t;

typedef struct {
    uint16_t knee_q15;      /* enabled: clamp to 1..32767; 16384 is identity */
    uint16_t enabled;       /* nonzero enables sine-only phase distortion */
} fm_pd_patch_t;

/* Base vector coordinates: four routed carriers, x then y interpolation. */
typedef struct {
    uint16_t x_q15, y_q15;  /* enabled coordinates clamp to 0..32768 */
    uint16_t enabled;       /* nonzero enables only with route.ops 4/carriers 15 */
} fm_vector_patch_t;

/* Three independently enabled parallel band-pass slots, before the final filter. */
#define FM_FORMANTS 3
typedef struct {
    uint32_t center_millihz; /* raw center, clamped by  when applied */
    uint16_t q_milli;       /* enabled: clamp to 500..16000 */
    uint16_t level_db10;    /* enabled: attenuation, clamp to 0..1200 */
    uint16_t enabled;       /* nonzero enables; disabled fields canonicalize to zero */
} fm_formant_patch_t;

/* : an operator's wavetable selection. It plays only on a sine
 * operator without PD or Karplus-Strong; an unbound bank slot plays silence. */
typedef struct {
    uint16_t position_q15;  /* enabled: clamp to 0..32768, first frame to last */
    uint8_t  bank;          /* slot in the bound assets' wt array */
    uint8_t  enabled;       /* nonzero enables; disabled fields canonicalize to zero */
} fm_wt_patch_t;

/* : an operator's Karplus-Strong source (design-hybrid section 5). It plays only on a sine operator
 * without PD or a wavetable; it reads and writes the buffer bound for its operator, and plays silence unbound.
 * It ignores phase modulation and its own feedback, so sync into it is inaudible; a sync edge from it is dropped (no wrap). */
#define FM_KS_BUFFER_MIN 64
#define FM_KS_BUFFER_MAX 8192
typedef struct {
    uint32_t seed;          /* excitation seed; zero maps to 0x9E3779B9 */
    uint16_t loss_q15;      /* enabled: clamp to 0..32767, the loop's gain per pass */
    uint16_t buffer;        /* enabled: clamp to 64..8192, then down to a power of two */
    uint8_t  enabled;       /* nonzero enables; disabled fields canonicalize to zero */
} fm_ks_patch_t;

/* : an operator's key-scaling target and curve signs and its feedback
 * type; all zero plays,  and  as before. Key scaling stays
 * linear in octaves (no exp curves); a "+" side forms its
 * attenuation as a boost instead. */
typedef struct {
    uint8_t loop;           /*  : nonzero repeats attack, decay and decay2 while held */
    uint8_t ks_target;      /* 0 index, 1 level, 2 both; above 2 plays as 0 */
    uint8_t ks_curve;       /* bit 0: the right side boosts ("+"), bit 1: the left side; other bits ignored */
    uint8_t fb_type;        /* bit 0: own feedback from the pre-envelope history, bit 1: the  source squared; other bits ignored */
} fm_op_ext_t;

/*  : a voice amp envelope after the shaper, the units and
 * arithmetic without delay, decay2, velocity or rate scaling. enabled 0 is
 * an exact bypass; any other value applies it, and the voice ends (every
 * operator idle, the filter and formant states cleared) when it reaches
 * idle. */
typedef struct {
    uint16_t attack_ms;      /* silence to peak, as fm_op_patch_t's: 1 to FM_SEG_MAX samples; from a higher gain at the same slope  */
    uint16_t decay_ms;       /* peak to sustain, linear in dB */
    uint16_t sustain_db10;   /* tenths of a dB below peak, clamped at 1200 */
    uint16_t release_ms;     /* peak to -96 dB, linear in dB */
    uint16_t enabled;        /* 0 bypass, nonzero applied */
} fm_amp_patch_t;

/* : fixed, caller-owned modulation slots. Generator source IDs 64..71
 * read host-published caches; family 32 moves an enabled wavetable position,
 * family 33 an enabled sine PD knee;
 * families 34/35 move vector coordinates, 36/37 formant center/attenuation. */
#define FM_MOD_SLOTS 8
#define FM_CTL_INPUTS 8
#define FM_GEN_INPUTS 8
typedef struct {
    int32_t amount;         /* target units at source +32768; clamped by family */
    uint16_t source;        /* 0 LFO, 1 PEG, 2 FENV, 3..6 EG, 7 velocity, 8 note, 16..23 ctl, 64..71 gen */
    uint8_t target;         /* 0 off; 1 pitch, 2 level, 3 index, 4 cutoff, 5 Q, 6..9 LFO parameters, 32 WT position, 33 PD knee, 34/35 vector x/y, 36/37 formant center/gain; targets 6..9 apply from the next tick */
    uint8_t target_index;   /* operator for 1..3/32/33, formant slot for 36/37, otherwise zero */
    /* A smoothed slot starts at 0 on a note-on from silence; a full-scale
     * constant source takes about 67 ticks at SHIFT 3. Use SHIFT 0 for a
     * static source that should act at once. Retriggers retain smoothing. */
    uint8_t shift;          /* 0 immediate; 1..15 convergent source smoothing */
    uint8_t reserved[3];    /* initialized to zero, no meaning */
} fm_mod_slot_patch_t;

typedef struct {
    fm_op_patch_t     op[FM_OPS];
    fm_filter_patch_t filter;   /*  */
    fm_route_t        route;    /* ; all zero: the fixed 2-operator path */
    fm_lfo_patch_t    lfo;      /* ; all zero: no LFO */
    fm_peg_patch_t    peg;      /* ; all zero: no pitch envelope */
    fm_fenv_patch_t   fenv;     /* ; all zero: no filter envelope or tracking */
    fm_eg_extra_t     eg[FM_OPS]; /* ; all zero preserves the original envelope */
    fm_noise_patch_t  noise[FM_OPS]; /* : read only by coloured noise; zero defaults to 4, values above 15 clamp */
    fm_shape_patch_t  shape;    /* all zero: exact bypass */
    fm_mod_slot_patch_t mod[FM_MOD_SLOTS]; /* all zero: established control path */
    fm_pd_patch_t     pd[FM_OPS]; /* disabled by default; a non-sine wave disables it, and any two of PD, a wavetable and Karplus-Strong on one operator disable each other */
    fm_vector_patch_t vector;    /* disabled by default;  can offset the base coordinates */
    fm_formant_patch_t formant[FM_FORMANTS]; /* all disabled: exact bypass */
    fm_wt_patch_t     wt[FM_OPS]; /* : disabled by default; reads bound banks */
    fm_ks_patch_t     ks[FM_OPS]; /* : disabled by default; plays bound buffers */
    fm_op_ext_t       ext[FM_OPS]; /* : all zero plays as before */
    fm_amp_patch_t    amp;      /* : all zero, no amp envelope */
} fm_patch_t;

/* : caller-owned assets for wavetable and Karplus-Strong operators,
 * bound to an initialized voice by fm_voice_bind_assets. A wavetable bank
 * is immutable and may be shared by voices; a Karplus-Strong buffer is
 * mutable, engine-owned while bound and private to one operator of one
 * voice. Arrays are counted. A later method appends its own counted array
 * after ks; size, the descriptor's sizeof as the caller compiled it, tells
 * the library which fields the caller filled. */
#define FM_WT_SLOTS      8      /* bank views per descriptor */
#define FM_WT_FRAMES_MAX 8
#define FM_WT_BANDS      12
#define FM_WT_LENGTH_MIN 32     /* frame lengths are powers of two in this range */
#define FM_WT_LENGTH_MAX 2048
typedef struct {
    uint32_t frames;        /* 1..FM_WT_FRAMES_MAX */
    uint32_t length;        /* a power of two, FM_WT_LENGTH_MIN..FM_WT_LENGTH_MAX */
    uint32_t bands;         /* exactly FM_WT_BANDS */
    uint32_t capacity;      /* int16 samples readable at data, at least bands * frames * length */
    const int16_t *data;    /* Q1.15, band-major, then frame, then sample; 2-byte aligned */
    uint64_t data_hash;     /* the bank's canonical content hash; an identity the binding does not recompute */
} fm_wt_view_t;

typedef struct {
    int32_t *data;          /* Q2.30, 4-byte aligned; NULL with capacity 0: no buffer */
    uint32_t capacity;      /* int32 samples writable at data */
} fm_ks_view_t;

typedef struct {
    uint32_t size;          /* sizeof(fm_assets_t) as the caller compiled it */
    uint32_t wt_count;      /* 0..FM_WT_SLOTS views at wt; a patch's bank slot indexes them */
    const fm_wt_view_t *wt;
    uint32_t ks_count;      /* 0..FM_OPS views at ks; ks[i] belongs to operator i */
    const fm_ks_view_t *ks; /* the views are read only; the buffers they name are not */
} fm_assets_t;

/* fm_voice_bind_assets results, the first failure in descriptor order. */
enum { FM_BIND_OK = 0,
       FM_BIND_ACTIVE,      /* a carrier, filter/formant or enabled amp is still active */
       FM_BIND_METADATA,    /* counts, dimensions, size field, missing arrays, address overflow */
       FM_BIND_CAPACITY,    /* a view holds fewer samples than its dimensions need */
       FM_BIND_OVERLAP,     /* misaligned storage, or a mutable range meets another range */
       FM_BIND_UNSUPPORTED  /* a descriptor larger than this library reads */
};

enum { FM_STAGE_IDLE = 0, FM_STAGE_ATTACK, FM_STAGE_DECAY, FM_STAGE_SUSTAIN, FM_STAGE_RELEASE,
       FM_STAGE_DELAY, FM_STAGE_DECAY2 };

typedef union {
    struct { int32_t row[8], sum; uint32_t counter; } pink;
    int32_t colour;         /* Q2.30 one-pole memory; held during FREE 2 idle catch-up */
} fm_noise_state_t;

typedef struct {
#if FM_PHASE64
    uint64_t phase, inc;     /* turns * 2^64 */
#else
    uint32_t phase, inc;     /* turns * 2^32 */
#endif
    uint64_t gain;           /* attack: linear gain, Q2.62 (1 << 62 = peak) */
    uint64_t rate_g;         /* attack: Q2.62 per sample */
#if FM_ENV64
    uint64_t att;            /* decay, sustain, release: attenuation, Q8.56 octaves (0 = peak) */
    uint64_t rate_d;         /* Q8.56 octaves per sample */
    uint64_t rate_r;
    uint64_t sus;            /* Q8.56 octaves */
    uint64_t sus2, rate_d2;  /*  second target and slope, Q8.56 */
#else
    uint32_t att;            /* decay, sustain, release: attenuation, Q8.24 octaves (0 = peak) */
    uint32_t rate_d;         /* Q8.24 octaves per sample */
    uint32_t rate_r;
    uint32_t sus;            /* Q8.24 octaves */
    uint32_t sus2, rate_d2;  /*  second target and slope, Q8.24 */
#endif
    uint32_t count;          /* samples left in the attack or decay segment */
    uint32_t count_d;        /* decay length in samples */
    int32_t  out;            /* output level into the mix, Q2.30, velocity applied */
    int32_t  index;          /* Q8.24 turns of modulated phase per unit of output, velocity and key scaling applied */
    int32_t  fb;             /* Q8.24 turns per unit of own output */
    int32_t  prev;           /* last envelope-shaped sine, Q2.30 (feedback input) */
    int32_t  prev2;          /* the one before it, Q2.30 (feedback input when FM_FB_AVG) */
    uint32_t stage;
    uint32_t noise;          /*  xorshift32 word, or FREE 2's seed/counter word */
    uint32_t noise_index;    /* FREE 2: half-turn counter for white, draw counter for pink; modulo 2^32 */
    uint32_t count_a_wait;   /* : prepared attack length behind the active delay */
    uint32_t count_d2;       /* : zero means an enabled instantaneous second decay */
    uint8_t decay2_on;       /* preserve enable before raising sustain2 to sustain1 */
    fm_noise_state_t noise_state; /* : only the selected wave's arm is read */
    uint64_t pd_slope[2];   /* control-tier divisions; Q31 slopes, split for sample multiplies */
    uint32_t pd_knee;       /* effective Q15 knee; zero disables, 16384 bypasses exactly */
    const int16_t *wt_bank; /* : a wavetable operator's bound bank; NULL: none */
    const int16_t *wt_a, *wt_b; /* the band's frames at and after the position */
    uint32_t wt_on;         /* nonzero: a wavetable operator, silent while unbound */
    uint32_t wt_bits;       /* log2 of the bank's frame length */
    uint32_t wt_frames;     /* the bank's frame count */
    uint32_t wt_band;       /* 0..11, from the increment */
    uint32_t wt_pos;        /* effective Q15 position, 0..32768 */
    uint32_t wt_frac;       /* Q15 crossfade from wt_a toward wt_b */
    int32_t *ks_buf;        /* : a Karplus-Strong operator's bound buffer, Q2.30; NULL: none */
    uint32_t ks_on;         /* nonzero: a Karplus-Strong operator, silent while unbound */
    uint32_t ks_mask;       /* the buffer's length - 1 */
    uint32_t ks_w;          /* write index */
    uint32_t ks_n, ks_f;    /* read delay: integer 2..length-2 and Q15 fraction */
    uint32_t ks_loss;       /* Q15 loop gain, 0..32767 */
    uint32_t ks_word;       /* excitation xorshift32 word */
    int32_t  ks_prev;       /* the previous read, Q2.30 */
#if FM_PHASE64
    uint64_t ks_inc;        /* the increment the delay was tuned from */
#else
    uint32_t ks_inc;
#endif
    int32_t  raw_prev, raw_prev2; /* : the last two samples before the envelope and AM edges, Q2.30; stored only for a pre-envelope feedback type */
    uint64_t loop_g0;       /*  : a loop's restart gain, the sustain's, Q2.62 */
    uint64_t loop_rate;     /* its attack step, Q2.62 per sample */
    uint32_t loop_count;    /* its attack length in samples; zero: no loop */
} fm_op_t;

/* the filter state. Coefficients from g = tan(pi fc / fs) and k = 1/Q:
 * a1 = 1 / (1 + g (g + k)), a2 = g a1, a3 = g a2, Q2.30, rounded; k Q3.29.
 * The two integrator states are Q2.30, saturated at +-(2^31 - 1). */
typedef struct {
    int32_t  a1, a2, a3, k;
    int32_t  ic1, ic2;
    uint32_t mode;           /* the patch's mode, 0 when it is not 1 to 4 */
    uint32_t idle;           /* samples of ring-out since the last carrier went idle */
} fm_svf_t;

typedef struct {
    fm_svf_t svf;            /* mode 2 when enabled; independent integrators/ring counter */
    uint32_t center_millihz, level_db10; /* effective cached values */
    int32_t gain_q30;        /* static/tick gain, unity at level 0 */
} fm_formant_t;

/* : the amp envelope's state, the delay-free and decay2-free subset
 * of fm_op_t's envelope fields. */
typedef struct {
    uint64_t gain, rate_g;   /* attack: Q2.62 */
#if FM_ENV64
    uint64_t att, rate_d, rate_r, sus;   /* Q8.56 octaves */
#else
    uint32_t att, rate_d, rate_r, sus;   /* Q8.24 octaves */
#endif
    uint32_t count, count_d, stage;
} fm_amp_t;

/* Plain data apart from  bound assets: wt_data and the operators'
 * wt_a/wt_b point at immutable bank samples, while ks_data/ks_buf point
 * at private buffers written by the voice. A byte copy shares those
 * buffers: a copy with bound Karplus-Strong storage must not render
 * while the original does. Rebind disjoint storage for independent use. */
typedef struct {
    uint32_t   sample_rate_hz;
    fm_patch_t patch;
    fm_op_t    op[FM_OPS];
    fm_svf_t   svf;          /*  */
    uint32_t   ops;          /* : 0 the fixed path, else the operators stepped, 1 to FM_OPS */
    uint32_t   carriers;     /* the carrier bits below ops; bit 0 on the fixed path */
    int32_t    edge[FM_OPS][FM_OPS]; /* edge[from][to]: op from's index times w[from][to] / 256, Q8.24 turns, set at note-on and at a tick with an index mask */
    /* the clock, armed by the LFO, the peg or the fenv. */
    uint32_t   ctl;          /* samples left to next tick; FREE 2 accounts for idle samples on catch-up */
    uint32_t   lfo_phase;    /* turns * 2^32: FREE 2's current grid phase, otherwise next tick's */
    uint32_t   lfo_step;     /* per tick, set at note-on and at FREE 2 voice init */
    uint32_t   lfo_word;     /*  xorshift32 word of waves 5 and 6 */
    int32_t    lfo_a, lfo_b; /* waves 5 and 6: the held value, and wave 6's next one, Q1.15 */
    uint32_t   lfo_time;     /* sounding samples since the note-on at the last tick, saturated */
    uint32_t   lfo_delay, lfo_fade;   /* in samples, set at note-on */
    int64_t    lfo_m;        /* the pitch factor 2^(cents / 1200), Q16.48, held between ticks */
    int32_t    lfo_vg[FM_OPS], lfo_ks[FM_OPS];   /* each operator's velocity and key-scaling gains, Q2.30, from the note-on */
    uint64_t   lfo_base[FM_OPS];  /* each pitch-masked operator's increment before the LFO, turns * 2^64 */
    fm_glide_t peg;              /* stages: 0 off, 1..3 toward L1..L3, 4 hold, 5 release to L4, 6 done */
    uint32_t   peg_n[4];         /* T1..T4 in control ticks */
    uint32_t   peg_mask;         /* enabled, present, non-fixed operators only */
    uint64_t   peg_base_frac[FM_OPS]; /* fractional turns times 2^64 before pitch modulation */
    uint16_t   peg_base_whole[FM_OPS]; /* : sentinel16; matrix pitch: whole0..255, sentinel256 */
    int32_t    lfo_cents;        /* last LFO cents, reused by peg and pitch events */
    int64_t    fenv_ub, fenv_u;  /* base and last-applied cutoff/fs, Q16.48 */
    int64_t    fenv_ulo, fenv_uhi; /* widened  bounds divided by fs */
    fm_glide_t fenv;            /* stages: 0 off, 1 attack, 2 decay, 3 sustain, 4 release, 5 done */
    uint32_t   fenv_n[3];       /* attack, decay and release in control ticks */
    int32_t    fenv_s;          /* sustain in Q1.15, 32768 full */
    int32_t    fenv_track;      /* static cents from note-on, -48000..48000 */
    uint32_t   fenv_q;          /* Q milli-units from patch or filter event */
    uint64_t   idle_samples;    /* : skipped samples, caught up before an increment changes */
    uint32_t   free_running;    /* nonzero if a present operator or the enabled LFO uses FREE 2 */
    uint32_t   lfo_wrap;        /* FREE 2's full-turn counter modulo 2^32 */
    uint32_t   amp_edges;       /* : at least one present nonzero AM/ring edge */
    int32_t    shape_gain_q24;  /* : static gain, 2^24 is unity */
    fm_svf_t   svf2;            /* : separate state, shared effective coefficients */
    int32_t    mod_cur[FM_MOD_SLOTS], ctl_input[FM_CTL_INPUTS];
    int32_t    mod_velocity, mod_note;
    int32_t    mod_pitch[FM_OPS], mod_level[FM_OPS], mod_index[FM_OPS];
    int32_t    mod_cutoff, mod_q;
    uint32_t   mod_active, mod_pitch_mask; /* enabled slots and their non-fixed pitch targets */
    uint32_t   mod_lfo_rate, mod_lfo_pitch, mod_lfo_level, mod_lfo_index;
    uint32_t   mod_filter_q; /* last applied Q, separate from the event/patch base */
    int32_t    gen_input[FM_GEN_INPUTS]; /* initialized zero; host caches survive notes and idle */
    int32_t    mod_pd[FM_OPS]; /* summed, bounded Q15 knee offsets */
    uint32_t   vector_x, vector_y; /* effective Q15 coordinates, 0..32768 */
    fm_formant_t formant[FM_FORMANTS];
    int64_t    mod_formant_center[FM_FORMANTS]; /* complete signed mHz sums, up to 8*INT32_MAX */
    int32_t    mod_formant_gain[FM_FORMANTS]; /* complete signed dB10 attenuation sums */
    uint32_t   formant_mask; /* enabled slots; their tails count as active audio */
    uint32_t   sync_edges;   /* : bit 4 * to + from, a present nonzero sync edge */
    uint32_t   sync_wrap_prev; /* : the operators that wrapped in the last sounding sample; a note-on from silence clears it */
    const int16_t *wt_data[FM_WT_SLOTS]; /* : validated bank samples; NULL: the slot is unbound */
    uint16_t   wt_frames[FM_WT_SLOTS], wt_length[FM_WT_SLOTS];
    int32_t   *ks_data[FM_OPS];          /* : validated buffers; NULL: none */
    uint32_t   ks_capacity[FM_OPS];
    int32_t    mod_wt[FM_OPS];           /* summed, bounded Q15 position offsets */
    int16_t    ks_db10[FM_OPS];  /* : signed level offset from the note-on's key scaling, -1200..1200 */
    uint32_t   ks_boost[FM_OPS]; /* : index boost from a "+" side, Q8.24 octaves; 0 none */
    uint32_t   fb_special;       /* : a present operator has a nonzero feedback type */
    fm_amp_t   amp;              /* : read only when patch.amp.enabled */
} fm_voice_t;

/* Initialization leaves the voice with no assets bound. */
void     fm_voice_init(fm_voice_t *v, const fm_patch_t *p, uint32_t sample_rate_hz);
/* : binds assets to an initialized voice, synchronously, with no
 * render or setter running on it. It validates every count, dimension,
 * capacity, size product, address range, alignment and known overlap
 * before it changes the voice: a Karplus-Strong view of an enabled source
 * must hold its patch's buffer length, and its buffer must be disjoint
 * from the voice, the descriptor, both view arrays, every bank and every
 * other buffer; a bank must be disjoint from the voice. On failure the
 * previous binding stays and nothing is written. A voice that is active
 * refuses (FM_BIND_ACTIVE). NULL unbinds. Success copies the validated
 * views into the voice; it starts no note and writes no buffer. Storage
 * stays valid and unchanged by the caller until unbind, rebind or
 * fm_voice_init; another voice's mutable buffers must not meet it, which
 * one voice cannot check. Returns an FM_BIND_ code. */
int      fm_voice_bind_assets(fm_voice_t *v, const fm_assets_t *assets);
/* : one partial of a generated wavetable bank. */
typedef struct {
    uint16_t frame;         /* 0..frames-1 */
    uint16_t number;        /* harmonic 1..1024 */
    uint16_t level_db10;    /* attenuation 0..1200 */
    uint16_t phase_u16;     /* start phase, turns * 2^16 */
} fm_wt_harmonic_t;
/* : fills a 12-band bank from partials, outside any render, with the
 * integer sine and dB10 gain. Band k keeps harmonics h <= 2^k with
 * h < length/2; partials with one frame and number add. One bank-wide
 * denominator, max(2^30, the largest sum), only attenuates. Returns
 * FM_BIND_OK, FM_BIND_METADATA (dimensions, more than frames * 1024 partials, a partial out of range or
 * missing) or FM_BIND_CAPACITY; nothing is written on failure. */
int      fm_wt_additive(int16_t *data, uint32_t capacity, uint32_t frames, uint32_t length,
                        const fm_wt_harmonic_t *h, uint32_t count);
/* velocity_q15 is a linear gain, 32767 = full, larger values clamp. On a
 * sounding voice this retriggers and phases continue: an operator still in
 * its attack keeps it running, any other restarts its attack from the
 * current gain at the full attack's slope. On a silent voice it starts
 * from silence, each operator at its phase_u16 or, with free_run, at the
 * phase and noise word it kept. FREE 2 first advances the phase
 * across skipped samples using the last instantaneous increment; its
 * noise word follows the number of half-turn crossings. Pink reconstructs
 * its rows in bounded work; coloured noise holds its one-pole memory
 * while idle and resumes it with the caught-up white word. Sounding means a carrier is not idle
 * (op[0] on the fixed path, any carrier of a route); filter/formant ring-out
 * does not count. Every operator the voice steps takes the note, a fixed
 * one keeping its frequency ; its attack, decay and release lengths
 * are rate-scaled from this note, a running attack's excepted
 * ; a route's edge indices are set from the operators' indices here.
 * The filter's coefficients are set again from the patch; its state
 * carries on. Formant integrators also carry on, with each ring
 * counter reset; effective centers/gains refresh only when changed. A key-synced LFO restarts phase and control grid at
 * every note-on. A FREE 2 LFO applies its current grid phase without an
 * extra step or grid reset; the peg and fenv share its remaining interval.
 * Both LFO modes restart delay/fade and apply their current value at once.
 * Last, each bound Karplus-Strong buffer refills with xorshift32 draws, the
 * word seeded from the patch seed xor the operator's  salt on a silent
 * voice and continued on a sounding one; FREE keeps no buffer. */
void     fm_voice_note_on(fm_voice_t *v, uint32_t freq_millihz, uint16_t velocity_q15);
/* Sets the voice's frequency and nothing else : phases, envelopes,
 * levels, the index as its note-on scaled it, the segment lengths as its
 * note-on rate-scaled them, and feedback carry on, so a sounding
 * note bends without a retrigger; an operator with a fixed frequency keeps
 * it. An LFO carries on, its last pitch factor applied to the new
 * frequency at once. A Karplus-Strong source retunes its delay
 * without a refill. A silent voice stays silent; its next note-on
 * sets the frequency again. FREE 2 catches up with its old increment
 * before the pitch event changes it. */
void     fm_voice_pitch(fm_voice_t *v, uint32_t freq_millihz);
void     fm_voice_note_off(fm_voice_t *v);
/* : the filter's cutoff and Q from now until the next note-on, in the
 * patch's units and clamps; nothing on a voice whose patch has no filter. */
void     fm_voice_filter(fm_voice_t *v, uint32_t cutoff_millihz, uint16_t q_milli);
/* Nonzero while a carrier sounds, a formant/final filter rings out, or an
 * enabled amp envelope is not idle. An active amp must keep consuming
 * frames even with silent carriers; this query does not prove audibility.
 * Each formant slot rings until both states lie within +-2048 (Q2.30,
 * 1/16 of an output LSB) or for 2^18 samples, then clears its states.
 * Its final sample still feeds the downstream filter; that filter starts
 * its own ring on the following sample. A cascade shares its ring cap. */
int      fm_voice_active(const fm_voice_t *v);
/* A FREE 2 voice must consume renderer frames even when inactive, so it
 * can account for elapsed samples. This query does not make it audible. */
int      fm_voice_needs_clock(const fm_voice_t *v);
/* : latch a controller in [-32768,32768]; n>=8 is ignored. This does
 * not tick, start a voice or alter smoothers. Banks survive note events. */
void     fm_voice_ctl(fm_voice_t *v, uint32_t n, int32_t value_q15);
/* Generator caches follow the same clamp and lifetime as controllers.
 * The host publishes its bank values; this setter never ticks the voice. */
void     fm_voice_gen(fm_voice_t *v, uint32_t n, int32_t value_q15);
void     fm_render_i16(fm_voice_t *v, int16_t *out, uint32_t frames);
/* One voice's output in Q2.30 for mixers outside the library (
 * space/): at most 1 << 30 in magnitude for one carrier without a filter,
 * at most 2^31 - 1 with several (: the carriers' sum, saturated) or
 * with a filter (: a resonant filter's gain, saturated);
 * fm_render_i16's samples are (out + (1 << 14)) >> 15 saturated, and
 * fm_mix_i16 sums these values. An inactive voice writes zeros; FREE 2
 * additionally counts elapsed samples for its next closed-form catch-up.
 * Operator FREE 2 alone does not change the control grid: only an enabled
 * FREE 2 LFO anchors that grid at voice init. */
void     fm_render_q30(fm_voice_t *v, int32_t *out, uint32_t frames);
/* Mono mixer of count caller-allocated voices at unit gains : returns
 * the number of clipped samples. One voice gives fm_render_i16's bits. */
uint32_t fm_mix_i16(fm_voice_t *voices, uint32_t count, int16_t *out, uint32_t frames);
uint64_t fm_golden_hash(const int16_t *pcm, uint32_t frames);   /* FNV-1a 64 over little-endian bytes */

/* Numerical validation helpers. */
uint32_t fm_table_bytes(void);
uint64_t fm_table_hash(void);
/* : the sample count note-on gives a segment of n samples (1 to
 * FM_SEG_MAX) for a note of freq_millihz at rate scaling rs (above 7 as 7),
 * the value the harness compares with the reference's. */
uint32_t fm_rate_scaled(uint32_t n, uint32_t freq_millihz, uint32_t rs);
/* : 2^x for x in Q16.48, clamped to -16.. 14, as Q16.48: a 17-entry
 * table of 2^(k/16) in Q16.48 times a seven-term series (degree 6) on the
 * remainder within 1/32, in Q2.62, rounded once. Relative error under
 * 2^-47 on [-1, 14) as sampled; below -1 the result's half unit of 2^-48
 * grows to 2^-33 of it at -16. The harness measures at most 1.75 units of
 * 2^-48 on [-2, 2); above 2 the absolute error grows as 2^x (the table's
 * rounding, scaled).
 * The pitch factor of the control clock; exported for the harness's unit
 * test. */
int64_t  fm_exp2_q48(int64_t x_q48);

#ifdef __cplusplus
}
#endif

#endif /* FM_A1_H */
