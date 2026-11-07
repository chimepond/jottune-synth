/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * ref_double.h — host-only double-precision synthesis reference.
 * Same patch, same discrete-time definition (stage machine, feedback from
 * the last two outputs or the last one per FM_FB_AVG, clamps, retrigger,
 * pitch events, key scaling),
 * exact real arithmetic for phase, sine, 2^-x, log2 and the note-on
 * conversions. Output is in int16 units (full scale 32768), unclipped and
 * unrounded. An external validation tool : it includes the library
 * header, never the reverse. Shapes and noise run on the engine's
 * integer phase and noise sequence (the harness gives the reference the
 * engine's increment), so their decisions are the engine's; a shape under
 * phase modulation decides on the real phase. The filter runs in double
 * on exact coefficients, tan(pi fc / fs) from the engine's clamped integer
 * cutoff and Q, and rings out on zero input without end. Routes
 * run in the same order with the same edge rules, exact weights, and the
 * carriers' sum clamped at +-(2^31 - 1) / 2^30 as the engine saturates it.
 * Rate scaling takes the same discrete count, max(1, round(n *
 * 2^(-oct * RS / 8))), with log2 and exp2 in double. The reference lets the harness
 * adopt a differing engine count only inside the independently derived
 * fixed-point error window; every adoption is reported by the harness.
 * Fixed
 * frequency, start phase and free run follow the engine's rules. The
 * control ticks fall on the engine's samples, the first at each note-on,
 * then one after every FM_CTL sounding samples. The FREE 2 LFO instead
 * keeps its grid from voice init and applies at note-on without a step.
 * FREE 2 oscillator phases and noise counters catch up in closed form
 * over idle samples; the integer phase product uses an independent
 * four-product implementation. The LFO is the
 * engine's integer LFO bit for bit (phase, word, waves on the engine's
 * sine table, delay and fade, cents, attenuation and index offset); what
 * it moves is exact: the increment f * ratio * 2^(cents / 1200) in double,
 * the level's 2^-x and the index. The pitch envelope uses an independent
 * closed-form integer glide on the same clock; its cents and the LFO's
 * cents are combined before the exact pitch factor and Nyquist clamp.
 * The filter envelope has its own integer closed form; key tracking
 * uses clamped double log2, checked before the harness adopts the engine's
 * integer cents. Wavetables read the bound bank's exact stored
 * samples with double interpolation at the engine's integer phase plus the
 * reference's PM, as the shapes do; the band comes from the reference's
 * own increment, the frame from the integer position. The
 * Karplus-Strong source fills from the same seeded words, tunes from the
 * engine's increment by its own exact long division and runs the loop on
 * doubles; its prime waits for that increment. Filter
 * coefficients use exact exp2 and tan at the same sample boundary as the
 * engine.
 */
#ifndef REF_DOUBLE_H
#define REF_DOUBLE_H

#include <stdint.h>
#include "fm_a1.h"

typedef struct {
    double a1, a2, a3, ic1, ic2, gain;
    uint32_t center_millihz, level_db10, idle;
} ref_formant_t;

typedef struct {
    double inc;      /* turns per sample */
    double phase;    /* turns, [0, 1) */
    double gain;     /* attack: linear gain (1 = peak) */
    double rate_g;   /* attack: gain per sample */
    double att;      /* decay, sustain, release: octaves */
    double rate_d, rate_r, sus;
    double out;      /* output level, amplitude, velocity applied */
    double index;    /* turns of modulated phase per unit of output, velocity and key scaling applied */
    double fb;       /* turns per unit of own output */
    double prev, prev2;
    double raw_prev, raw_prev2; /* : the last two samples before the envelope */
    uint32_t pd_knee; /* independent ideal warp at the effective Q15 knee */
    int    stage;
    uint32_t count, count_d;
    uint32_t count_a;        /* : the full attack's rate-scaled length at the last note-on, the  and  rules' n */
    uint32_t rate_attack_kept; /* : whether that note-on actually kept this attack  */
    uint32_t loop;           /*  : this note's loop flag; the restart forms at the transition from count_a */
    uint32_t loop_adopt;     /* : the loop attack's length the harness adopted at the note-on; 0 forms it at the restart */
    uint64_t iphase, iinc;   /* : the engine's integer phase, turns * 2^64; the harness sets iinc to the engine's */
    uint32_t noise;          /* : the engine's noise word */
    uint32_t noise_index;    /*  FREE 2 half-turn counter */
    uint32_t count_a_wait;   /* : attack prepared behind an exact silent delay */
    uint32_t count_d2;
    double sus2, rate_d2;
    uint8_t decay2_on;
    int32_t pink_rows[8], pink_sum;
    uint32_t pink_counter;
    int32_t colour;
    const int16_t *wt_bank;  /* : the bound bank; NULL: none, silent when wt_on */
    uint32_t wt_on, wt_frames, wt_length, wt_pos;
    double  *ks_buf;         /* : Karplus-Strong samples in units of 1; NULL: unbound, silent when ks_on */
    uint32_t ks_on, ks_len, ks_w, ks_n, ks_f, ks_loss, ks_word, ks_prime;
    uint64_t ks_inc;         /* the iinc the delay was tuned from */
    double   ks_prev;
} ref_op_t;

typedef struct {
    double     fs;
    uint32_t   fs_hz;
    fm_patch_t patch;
    ref_op_t   op[FM_OPS];
    double     fa1, fa2, fa3, fk, fic1, fic2;   /* : the filter's coefficients and states */
    uint32_t   fmode;                           /* the patch's filter mode, 0 when it is not 1 to 4 */
    uint32_t   ops, carriers;                   /* : the engine's bounds (fm_voice_t) */
    double     edge[FM_OPS][FM_OPS];            /* edge[from][to]: op from's index times w / 256, turns */
    /* , : the engine's control clock and integer LFO (fm_voice_t) */
    uint32_t   ctl, lfo_phase, lfo_step, lfo_word, lfo_time, lfo_delay, lfo_fade;
    int32_t    lfo_a, lfo_b, lfo_cents;         /* waves 5 and 6's values; the last tick's pitch offset */
    uint32_t   freq_millihz;                    /* the note's, after pitch events */
    double     vg[FM_OPS], ks[FM_OPS];          /* each operator's velocity and key-scaling gains at the note-on */
    double     ks_db10[FM_OPS], ks_boost[FM_OPS]; /* : the level offset in dB10 and the index boost in octaves */
    /* : independent closed-form segment, checked against fm_glide_t. */
    int32_t    peg_start, peg_target, peg_cur;
    uint32_t   peg_n, peg_j, peg_stage, peg_mask;
    /* : independent filter-envelope segment and note-on tracking. */
    int32_t    fe_start, fe_target, fe_e;
    uint32_t   fe_n, fe_j, fe_stage;
    int32_t    fe_s, fe_track;
    uint32_t   fe_q;
    double     fe_base;                          /* clamped base cutoff, mHz */
    uint64_t   idle_samples;
    uint32_t   free_running, lfo_wrap;
    uint32_t   amp_edges;       /* : ideal AM/ring products follow PM/envelope */
    uint32_t   sync_edges;      /* : bit 4 * to + from, a present sync edge */
    uint32_t   sync_wrap_prev;  /* : the operators that wrapped in the last sounding sample */
    double     shape_gain;     /* exact static 10^(drive_db10/200) */
    double     f2ic1, f2ic2;   /* : independent cascade integrators */
    uint32_t   f2mode, filter_idle;
    int32_t    mod_cur[FM_MOD_SLOTS], ctl_input[FM_CTL_INPUTS];
    int32_t    mod_velocity, mod_note;
    int32_t    mod_pitch[FM_OPS], mod_level[FM_OPS], mod_index[FM_OPS];
    int32_t    mod_cutoff, mod_q;
    uint32_t   mod_active, mod_pitch_mask;
    uint32_t   mod_lfo_rate, mod_lfo_pitch, mod_lfo_level, mod_lfo_index;
    int32_t    gen_input[FM_GEN_INPUTS];
    int32_t    mod_pd[FM_OPS];
    uint32_t   vector_x, vector_y;
    ref_formant_t formant[FM_FORMANTS];
    int64_t    mod_formant_center[FM_FORMANTS];
    int32_t    mod_formant_gain[FM_FORMANTS];
    uint32_t   formant_mask;
    int32_t    mod_wt[FM_OPS];
    /* : the amp envelope, ref_op_t's envelope subset in octaves and linear gain */
    double     amp_gain, amp_rate_g, amp_att, amp_rate_d, amp_rate_r, amp_sus;
    uint32_t   amp_count, amp_count_d, amp_stage;
} ref_voice_t;

void ref_voice_init(ref_voice_t *v, const fm_patch_t *p, uint32_t sample_rate_hz);
/* : the views fm_voice_bind_assets accepted; NULL unbinds. Not validated. */
void ref_voice_bind_assets(ref_voice_t *v, const fm_assets_t *a);
/* : Karplus-Strong operators the engine bound from a (NULL unbinds)
 * take store + op * FM_KS_BUFFER_MAX doubles of the caller's store. */
void ref_voice_bind_ks(ref_voice_t *v, const fm_assets_t *a, double *store);
/* After the harness copies the engine's increments: retune changed delays
 * and prime the previous read of a buffer refilled since the last call. */
void ref_voice_ks_sync(ref_voice_t *v);
/* 's delay for increment iinc (turns * 2^64) and a buffer length:
 * returns 0 interior, 1 clamped long (length - 2), 2 clamped short (2). */
int ref_ks_delay(uint64_t iinc, uint32_t length, uint32_t *n, uint32_t *f);
void ref_voice_note_on(ref_voice_t *v, uint32_t freq_millihz, uint16_t velocity_q15);
/*  and : operator op's loop attack length as its next restart forms
 * it from the final sustain's gain and count_a, ignoring loop_adopt. */
uint32_t ref_loop_samples(const ref_voice_t *v, int op);
void ref_voice_pitch(ref_voice_t *v, uint32_t freq_millihz);
void ref_voice_note_off(ref_voice_t *v);
/* Nonzero while a carrier envelope is not idle, including the fixed path. */
int ref_voice_sounding(const ref_voice_t *v);
void ref_voice_filter(ref_voice_t *v, uint32_t cutoff_millihz, uint16_t q_milli);
void ref_voice_ctl(ref_voice_t *v, uint32_t n, int32_t value_q15);
void ref_voice_gen(ref_voice_t *v, uint32_t n, int32_t value_q15);
void ref_render(ref_voice_t *v, double *out, uint32_t frames);
/* : max(1, round(n * 2^(-oct * rs / 8))), oct = log2(f / 27500 mHz)
 * clamped to 0..8, rs to 0..7, in double; n for rs 0. */
uint32_t ref_rate_scaled(uint32_t n, uint32_t freq_millihz, uint32_t rs);
/* : ideal unrounded count and an absolute error bound derived from the
 * table, inverse Q48 and forward Q24/Q30 precisions. n is 1..FM_SEG_MAX. */
void ref_rate_scaled_window(uint32_t n, uint32_t freq_millihz, uint32_t rs,
                            double *exact, double *tau);
enum { REF_RATE_EQUAL = 0, REF_RATE_ADOPTED, REF_RATE_FAIL };
/* After note-on, validate an explicit engine count and adopt only inside
 * the window. segment: 0 attack, 1 decay, 2 release. An attack that the
 * note-on kept running retains its count and step.
 * Adoption updates the full attack/decay count or release slope and, if
 * needed, recomputes the shortened attack from the reference's own gain.
 * No printing here: the harness reports every ADOPT or COUNT-FAIL. */
int ref_adopt_rate_scaled(ref_voice_t *v, unsigned op, unsigned segment,
                          uint32_t count, double *exact, double *tau);

#endif
