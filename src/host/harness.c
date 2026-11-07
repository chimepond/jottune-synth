/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * harness.c — host renderer and numerical validation tool. Compares
 * the integer A1 voice and the double reference, prints SNR, ceiling, error
 * peaks, tail behaviour and the golden hash, and writes WAV files.
 *
 *   harness [--fs HZ] [--out DIR] [--check [lfo|peg-bounds|fenv-bounds] | --retrig | --fb | --filter]
 *   harness [--fs HZ] --emit DIR
 *   harness --gen-params
 *   harness --gen-events [--] FILE...
 *   harness --gen-soak [--merged PATH] [--] FILE...
 *   harness --play [--wav PATH] [--merged PATH] [--stems WAV] [--metrics] [--ref] [--chunk N|random] [--] FILE...
 *   harness --play-once [--wav PATH] [--merged PATH] [--stems WAV] [--metrics] [--] FILE...
 *   harness --engine | --inspect [--] FILE... | --events [--] FILE...
 *   harness --play --wav-fd N --pcm-limit BYTES [--merged PATH] [--metrics] [--chunk N|random] [--] FILE...
 *   harness --fxref [--] FILE...
 *   harness --field-test | --field-cost
 *
 * --fs takes 1 to 4294967295 Hz; the cases, --check, --retrig, --fb,
 * --filter and --emit need every case to fit in 2^32 - 1 samples, so at
 * most 795364314.
 * Exit 0, 1 on a bad timeline or a block-size dependence, 2 on a usage
 * error, 3 when a render's hash is not the
 * timeline's, 4 when --check, --fxref or --play --ref finds a criterion
 * failed (--play --ref uses 4 only if no other error took precedence),
 * 5 on a host I/O or allocation failure. Format/capacity limits remain 1.
 * --gen-soak also exits 1 when a sample clipped or an audit invariant failed.
 * --play --ref exits 2 before rendering a stereo record, field route or
 * generator session without effects: no double model covers those ((5)).
 * --fs and --out are refused by file/field modes ; -- terminates
 * options for --play, --fxref, --gen-events and --gen-soak (usage).
 *
 * --check runs the cases as the acceptance gate :
 * after each RESULT line a GATE line per failed criterion, then GATE ok or
 * GATE FAIL with their number. Only a build at the approved knobs runs it.
 * --check lfo checks four named LFO cases, phase, two FREE2, nine wave, vectorxy, two wavetable and two
 * Karplus-Strong cases (21), plus permanent raw bounds, at either phase width, with the other approved knobs; it is a subset, not
 * the full gate. --check peg-bounds and --check fenv-bounds check raw API
 * extremes at either phase width, also included in --check. Each subset
 * ends CHECK NAME ok or CHECK NAME FAIL; only the full gate ends GATE ok.
 * Without these modes
 * the metrics never change the exit status
 * (the measurement sweep). --retrig prints the retrigger evidence of  and  instead of the
 * cases (probe_retrig), --fb the high-depth feedback evidence of
 * (probe_fb), --filter the measurements of 's filter (probe_filter).
 * The cases' and --retrig's lines are stable for cross-machine
 * diffs. So are --fb's hashes, block_indep, a1_end and a1_idle. Its other
 * values can follow the host libm's rounding: offharm below about -120 dB
 * (the method's numeric floor), and values from the reference past the
 * loop's stability edge, in the last digits or entirely where the loop is
 * chaotic.
 *
 * --play renders timelines, the one integer text format of , in the
 * draft grammar described at tl_read, and --emit writes the cases as
 * timelines at --fs, each with its hash from fm_render_i16. --fxref
 * measures a timeline with effects against their double model (fx_diff).
 * --play --metrics adds a METRICS line, integer peak, RMS and crest, and
 * --play --ref 's comparison with the double reference (play_ref) or,
 * with effects, --fxref's (PLAN  step 1, ).
 * --stems renders a WAV's channels, one per voice, in place of the voices
 * on a field route; --field-test and --field-cost run field_ref.c's test
 * and cost of the field routes (space/SPEC.md sections 11 to 16), exit 0 or 1.
 */
#if !defined(__APPLE__) && !defined(_XOPEN_SOURCE)
#define _XOPEN_SOURCE 700
#endif
#if !defined(__APPLE__) && !defined(_POSIX_C_SOURCE)
#define _POSIX_C_SOURCE 200809L
#endif
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdarg.h>
#include <stdio.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include <float.h>
#include "fm_a1.h"
#include "ref_double.h"
#include "../gen/gen.h"
#include "../space/space.h"
#include "../fx/fx.h"
#include "fx_ref.h"
#include "field_ref.h"
#include "../space/field.h"
#include "../../vendor/fp_math.h"

/* Embedders may provide an operation-local cancellation predicate. */
#ifndef FM_HOST_CANCELLED
#define FM_HOST_CANCELLED() 0
#endif
#ifndef FM_HOST_CANCELLABLE
#define FM_HOST_CANCELLABLE 0
#endif

#if FM_OPS < 4
#error "the harness's routed cases need FM_OPS 4 or more"
#endif

enum { EV_END = 0, EV_ON, EV_OFF, EV_PITCH, EV_POS, EV_MOVE, EV_FILTER, EV_CTL };   /* EV_POS, EV_MOVE: timelines only (space/SPEC.md); EV_FILTER:  */
enum { EV_SEND = 16, EV_CRUSH };                               /* timelines only (fx/SPEC.md) */

typedef struct {
    uint32_t t_ms;           /* sample index t_ms * fs / 1000, truncated; lists are in time order */
    uint32_t type;           /* EV_ON, EV_OFF, EV_PITCH, EV_FILTER; EV_END (0) ends the list */
    uint32_t freq_millihz;   /* EV_ON and EV_PITCH; EV_FILTER: the cutoff in mHz */
    uint16_t velocity_q15;   /* EV_ON; EV_FILTER: Q in thousandths; EV_CTL: input index */
    int32_t ctl_value;       /* EV_CTL: signed Q15, both endpoints included */
} spike_event_t;

#define MAX_EV 12

typedef struct {
    const char   *name;
    fm_patch_t    patch;
    uint32_t      len_ms;
    spike_event_t ev[MAX_EV];
} spike_case_t;

/* Per operator: ratio_q16, level_db10, index_q10, feedback_q10, vel_sens_q8,
 *               attack_ms, decay_ms, sustain_db10, release_ms,
 *               ks_break_millihz, ks_slope_db10, wave ,
 *               rate_scale , fixed_millihz, phase_u16, free_run ;
 * then the filter : cutoff_millihz, q_milli, mode;
 * then the route : ops, carriers, w[from][to] in Q8.8,
 *               256 unity, and kind[from][to]: 0 PM, 1 AM, 2 ring, 3 sync;
 * then the LFO (, fm_lfo_patch_t): rate_millihz, wave, delay_ms,
 *               fade_ms, pitch_cents, level_db10, index_q10, phase_u16,
 *               mask_pitch, mask_level, mask_index, free_run; { 0 } for none.
 * Then  peg: level_cents[4], time_ms[4], mask; zero levels disable it.
 * Then  fenv: ref_millihz, attack_ms, decay_ms, release_ms,
 * sustain_permille, depth_cents, track_permille; zero depth/track disable it.
 * Then  eg[FM_OPS]: delay_ms, decay2_ms, sustain2_db10, left_slope_db10;
 * zero extensions preserve old output; key scaling affects the operator index.
 * Then  noise[FM_OPS]: shift; zero means default 4, read only by waves 10/11.
 * Route { 0 }, the fixed path: op[0] carrier (level, vel_sens), op[1]
 * modulator (index, feedback, vel_sens, key scaling). Under a route each
 * operator's level counts where it is a carrier, its index scales each
 * edge from it and its feedback applies to itself. The first five are the
 *  golden patches with their depths carried over from Q8.8 radians
 * (x4); the next four exercise the  model, the next two 's pitch
 * event and 's key scaling, the next five 's waveforms, the next
 * six 's filter, the next five 's routes, the next three 's
 * shapes on a modulated carrier (, item 6), the next two 's
 * summed edges and a carrier that outlives op 0, with noise on op 3 and
 * op 2, the next three 's rate scaling and 's fixed frequency,
 * start phase and free run, then eight  LFO cases on 's
 * control clock, two  pitch-envelope cases, two  filter-envelope
 * cases, then 's two route additions and 's attack/fixed-carrier cases. */
static const spike_case_t CASES[] = {
    { "sine",     { { { 65536,  0,     0,    0, 256,   5,  500, 200,  800, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,     0,    0,   0,   5,  500, 200,  800, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 440000, 32767, 0 }, { 1000, EV_OFF, 0, 0, 0 } } },
    { "bell",     { { { 65536,  0,     0,    0, 256,   2, 2500, 900,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 229376, 0,  8192,    0,   0,   1, 1200, 600,  400, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 3000,
      { { 0, EV_ON, 523250, 32767, 0 }, { 2500, EV_OFF, 0, 0, 0 } } },
    { "quiet",    { { { 65536,  0,     0,    0, 256,   5,  300, 400, 2500, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  1536,    0,   0,   5,  300, 200, 2500, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 3000,
      { { 0, EV_ON, 880000, 16384, 0 }, { 400, EV_OFF, 0, 0, 0 } } },
    { "feedback", { { { 65536,  0,     0,    0, 256,   3,  800, 300,  500, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  3072, 1024,   0,   3,  800, 300,  500, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    { "highidx",  { { { 65536,  0,     0,    0, 256,   2, 1500, 600,  400, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0, 12288,    0,   0,   2, 1000, 400,  400, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    /* slow linear attacks: carrier 600 ms, modulator 900 ms (brightness swell) */
    { "swell",    { { { 65536,  0,     0,    0, 256, 600,  800,  60,  700, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  2048,    0,   0, 900,  800, 120,  900, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 3000,
      { { 0, EV_ON, 329628, 32767, 0 }, { 1800, EV_OFF, 0, 0, 0 } } },
    /* note-off in the attack: carrier at 0.375 of peak, modulator at 0.25 */
    { "offatk",   { { { 65536,  0,     0,    0, 256, 800,  500, 100,  600, 0, 0, 0, 0, 0, 0, 0 },
                      { 196608, 0,  3072,    0,   0, 1200, 500, 100,  800, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 261626, 32767, 0 }, { 300, EV_OFF, 0, 0, 0 } } },
    /* retrigger in decay, in attack, in release, with the modulator idle, on an
     * idle voice, and a zero-length note (note-off at gain 0) */
    { "retrig",   { { { 65536,  0,     0,    0, 256,  50,  400, 120,  900, 0, 0, 0, 0, 0, 0, 0 },
                      { 98304,  0,  2560,  512,   0,  30,  600, 200,  700, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 5400,
      { { 0, EV_ON, 440000, 32767, 0 }, { 250, EV_ON, 493883, 24000, 0 }, { 270, EV_ON, 523251, 32767, 0 },
        { 700, EV_OFF, 0, 0, 0 }, { 900, EV_ON, 392000, 20000, 0 }, { 1300, EV_OFF, 0, 0, 0 },
        { 2200, EV_ON, 440000, 32767, 0 }, { 2500, EV_OFF, 0, 0, 0 },
        { 3700, EV_ON, 349228, 32767, 0 }, { 3700, EV_OFF, 0, 0, 0 },
        { 3900, EV_ON, 349228, 32767, 0 }, { 4100, EV_OFF, 0, 0, 0 } } },
    /* velocity crossfade: carrier -6 dB with half sensitivity, modulator index
     * 6 rad with sensitivity 200/256; a full-velocity note, then a soft one */
    { "velmod",   { { { 65536, 60,     0,    0, 128,   5, 1200, 150,  600, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  6144,    0, 200,   5,  900, 300,  600, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 3200,
      { { 0, EV_ON, 293665, 32767, 0 }, { 700, EV_OFF, 0, 0, 0 },
        { 1600, EV_ON, 293665, 9000, 0 }, { 2300, EV_OFF, 0, 0, 0 } } },
    /*  pitch events in the carrier's attack, in the decay, the sustain
     * and the release, on a silent voice (no effect: the next note-on sets
     * its own frequency) and in a second note's decay */
    { "bend",     { { { 65536,  0,     0,    0, 256,   5,  800, 120,  600, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  2048,  512,   0,   3,  500, 200,  500, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 4500,
      { { 0, EV_ON, 440000, 32767, 0 }, { 3, EV_PITCH, 466164, 0, 0 }, { 200, EV_PITCH, 493883, 0, 0 },
        { 400, EV_PITCH, 523251, 0, 0 }, { 1200, EV_PITCH, 392000, 0, 0 }, { 1500, EV_OFF, 0, 0, 0 },
        { 1700, EV_PITCH, 349228, 0, 0 }, { 2600, EV_PITCH, 329628, 0, 0 }, { 2800, EV_ON, 293665, 24000, 0 },
        { 3000, EV_PITCH, 329628, 0, 0 }, { 3400, EV_OFF, 0, 0, 0 } } },
    /*  key scaling: a 1:14 tine whose index falls 12 dB an octave above
     * F5; A4 below the break, C6 and a softer E6 above it, F5 on it, then a
     * retrigger to G6 (scaled again) and a bend to G5 (not rescaled) */
    { "keyscale", { { { 65536,  0,     0,    0, 256,   1, 1500, 400,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 917504, 0,  1536,    0, 128,   1,  250, 300,  300, 698456, 120, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 4000,
      { { 0, EV_ON, 440000, 32767, 0 }, { 400, EV_OFF, 0, 0, 0 },
        { 800, EV_ON, 1046502, 32767, 0 }, { 1200, EV_OFF, 0, 0, 0 },
        { 1600, EV_ON, 1318510, 20000, 0 }, { 2000, EV_OFF, 0, 0, 0 },
        { 2400, EV_ON, 698456, 32767, 0 }, { 2700, EV_ON, 1567982, 32767, 0 },
        { 3000, EV_PITCH, 783991, 0, 0 }, { 3300, EV_OFF, 0, 0, 0 } } },
    /* 's shapes on an unmodulated carrier, where the reference decides
     * on the engine's integer phase */
    { "half",     { { { 65536,  0,     0,    0, 256,   5,  500, 200,  800, 0, 0, 1, 0, 0, 0, 0 },
                      { 65536,  0,     0,    0,   0,   5,  500, 200,  800, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 440000, 32767, 0 }, { 1000, EV_OFF, 0, 0, 0 } } },
    { "abs",      { { { 65536,  0,     0,    0, 256,   5,  500, 200,  800, 0, 0, 2, 0, 0, 0, 0 },
                      { 65536,  0,     0,    0,   0,   5,  500, 200,  800, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 329628, 32767, 0 }, { 1000, EV_OFF, 0, 0, 0 } } },
    { "quarter",  { { { 65536,  0,     0,    0, 256,   5,  500, 200,  800, 0, 0, 3, 0, 0, 0, 0 },
                      { 65536,  0,     0,    0,   0,   5,  500, 200,  800, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1000, EV_OFF, 0, 0, 0 } } },
    /* 's noise as the carrier at -6 dB: a note, a retrigger that keeps
     * the word, and a note from silence that starts the sequence again */
    { "noise",    { { { 65536, 60,     0,    0, 256,   2,  300, 120,  250, 0, 0, 4, 0, 0, 0, 0 },
                      { 65536,  0,     0,    0,   0,   2,  300, 120,  250, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2500,
      { { 0, EV_ON, 4000000, 32767, 0 }, { 300, EV_ON, 8000000, 24000, 0 }, { 600, EV_OFF, 0, 0, 0 },
        { 1500, EV_ON, 4000000, 32767, 0 }, { 1800, EV_OFF, 0, 0, 0 } } },
    /* noise as the modulator, index 1 rad at ratio 3 (a new word 1320 times a second) */
    { "noisemod", { { { 65536,  0,     0,    0, 256,   5,  800, 200,  600, 0, 0, 0, 0, 0, 0, 0 },
                      { 196608, 0,  1024,    0,   0,   5,  800, 200,  600, 0, 0, 4, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1200, EV_OFF, 0, 0, 0 } } },
    /* 's four modes on a bright 1:1 tone at -6 dB; the band-pass voice
     * releases in 2 ms, so its filter rings out after the carrier idles */
    { "lowpass",  { { { 65536, 60,     0,    0, 256,   5,  900, 150,  500, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  4096,    0,   0,   5,  900, 150,  500, 0, 0, 0, 0, 0, 0, 0 } }, { 1200000, 707, 1, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1200, EV_OFF, 0, 0, 0 } } },
    { "bandpass", { { { 65536, 60,     0,    0, 256,   5,  900, 150,    2, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  4096,    0,   0,   5,  900, 150,    2, 0, 0, 0, 0, 0, 0, 0 } }, { 1500000, 2000, 2, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 146832, 32767, 0 }, { 1200, EV_OFF, 0, 0, 0 } } },
    { "highpass", { { { 65536, 60,     0,    0, 256,   5,  900, 150,  500, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  4096,    0,   0,   5,  900, 150,  500, 0, 0, 0, 0, 0, 0, 0 } }, { 2000000, 707, 3, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1200, EV_OFF, 0, 0, 0 } } },
    { "notch",    { { { 65536, 60,     0,    0, 256,   5,  900, 150,  500, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  4096,    0,   0,   5,  900, 150,  500, 0, 0, 0, 0, 0, 0, 0 } }, { 1000000, 1000, 4, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 2000,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1200, EV_OFF, 0, 0, 0 } } },
    /* a resonant low-pass (Q 2) swept by filter events from 500 Hz to 4 kHz
     * and back over a sustained bright tone */
    { "sweep",    { { { 65536, 60,     0,    0, 256,   5, 3000,  60,  500, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  6144,  256,   0,   5, 3000, 120,  500, 0, 0, 0, 0, 0, 0, 0 } }, { 500000, 2000, 1, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 3000,
      { { 0, EV_ON, 110000, 32767, 0 }, { 250, EV_FILTER, 750000, 2000, 0 }, { 500, EV_FILTER, 1100000, 2000, 0 },
        { 750, EV_FILTER, 1700000, 2000, 0 }, { 1000, EV_FILTER, 2600000, 2000, 0 }, { 1250, EV_FILTER, 4000000, 2000, 0 },
        { 1500, EV_FILTER, 2600000, 2000, 0 }, { 1750, EV_FILTER, 1700000, 2000, 0 }, { 2000, EV_FILTER, 1100000, 2000, 0 },
        { 2250, EV_FILTER, 750000, 2000, 0 }, { 2500, EV_OFF, 0, 0, 0 } } },
    /* a band-pass voice whose cutoff an event raises, a retrigger that sets
     * the patch's cutoff again and keeps the state, an event in the release,
     * and a note from silence, which sets the patch's cutoff again */
    { "fretrig",  { { { 65536, 60,     0,    0, 256,  20,  600, 120,  400, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  3072,    0,   0,  10,  600, 200,  400, 0, 0, 0, 0, 0, 0, 0 } }, { 800000, 1500, 2, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 3000,
      { { 0, EV_ON, 196000, 32767, 0 }, { 300, EV_FILTER, 2400000, 1500, 0 }, { 600, EV_ON, 261626, 28000, 0 },
        { 1000, EV_OFF, 0, 0, 0 }, { 1100, EV_FILTER, 3000000, 4000, 0 }, { 1800, EV_ON, 196000, 32767, 0 },
        { 2200, EV_OFF, 0, 0, 0 } } },
    /* : a chain, op 2 modulates op 1, which modulates the carrier op 0 */
    { "route3",   { { { 65536,  0,     0,    0, 256,   3,  900, 150,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  2048,    0,   0,   3,  700, 200,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  1536,    0,   0,   2,  500, 300,  300, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 },
                    { 3, 0x1, { [2] = { [1] = 256 }, [1] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    /* two stacks, 3 on 2 and 1 on 0, the carriers 0 and 2 at -6 dB each */
    { "route4",   { { { 65536, 60,     0,    0, 256,   5,  800, 150,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  1024,    0,   0,   5,  600, 200,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 60,    0,    0, 256,   2,  400, 300,  200, 0, 0, 0, 0, 0, 0, 0 },
                      { 458752, 0,   768,    0,   0,   1,  200, 400,  200, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 },
                    { 4, 0x5, { [3] = { [2] = 256 }, [1] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 261626, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    /* a back edge: the carrier op 0 also modulates the carrier op 2, one
     * sample back, at 1.5 times op 0's index of 1 rad */
    { "routeback", { { { 65536, 60,  1024,    0, 256,   5,  700, 150,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 196608, 0,  1024,    0,   0,   5,  500, 200,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 60,    0,    0, 256,   5,  600, 200,  300, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 },
                    { 3, 0x5, { [1] = { [0] = 256 }, [0] = { [2] = 384 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 196000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    /* feedback on the carrier (0.5 rad) and on the middle modulator (0.75 rad),
     * which op 2 modulates as well */
    { "routefb",  { { { 65536,  0,     0,  512, 256,   3,  800, 200,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  1536,  768,   0,   3,  600, 200,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  1024,    0,   0,   2,  400, 300,  300, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 },
                    { 3, 0x1, { [2] = { [1] = 256 }, [1] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 146832, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    /* three carriers at 0 dB, op 2 also modulating op 0: their sum passes
     * 2.0 and saturates ( (a)), and the int16 output clips */
    { "routesat", { { { 65536,  0,     0,    0, 256,   5,  400,  20,  200, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,     0,    0, 256,   5,  400,  20,  200, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  1024,    0, 256,   5,  400,  20,  200, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 },
                    { 3, 0x7, { [2] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 110000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    /* 's shapes on a carrier the modulator, of the same shape, modulates:
     * the reference decides them on the real phase */
    { "halfmod",  { { { 65536,  0,     0,    0, 256,   5,  600, 200,  300, 0, 0, 1, 0, 0, 0, 0 },
                      { 131072, 0,  1536,    0,   0,   5,  500, 200,  300, 0, 0, 1, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    { "absmod",   { { { 65536,  0,     0,    0, 256,   5,  600, 200,  300, 0, 0, 2, 0, 0, 0, 0 },
                      { 131072, 0,  1536,    0,   0,   5,  500, 200,  300, 0, 0, 2, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    { "quartermod", { { { 65536, 0,    0,    0, 256,   5,  600, 200,  300, 0, 0, 3, 0, 0, 0, 0 },
                      { 131072, 0,  1536,    0,   0,   5,  500, 200,  300, 0, 0, 3, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    /* : three edges summed into the carrier op 0, from op 1 at unity,
     * op 2 at 512 and op 3 at 1024, and op 3 into op 1 at 512; op 3 is
     * 's noise, so its seed has a hash (noise ignores the phase, so op
     * 3's feedback does not sound) */
    { "xmsum",    { { { 65536,  0,     0,    0, 256,   5,  700, 150,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  1024,    0,   0,   5,  600, 200,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 196608, 0,   768,    0,   0,   5,  500, 200,  300, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,   512,  384,   0,   5,  400, 250,  300, 0, 0, 4, 0, 0, 0, 0 } }, { 0, 0, 0, 0 },
                    { 4, 0x1, { [3] = { [0] = 1024, [1] = 512 }, [2] = { [0] = 512 }, [1] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    /* carriers 0 and 2, 1 on 0: op 0 idles 57 ms after the note off at
     * 0.3 s, and op 2, 's noise with an 800 ms release, sounds on alone
     * until it idles at 1.26 s, where the voice ends; op 2's seed has a
     * hash (its feedback does not sound) */
    { "xcarlong", { { { 65536, 60,     0,    0, 256,   5,  300, 100,   50, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  1024,    0,   0,   5,  300, 200,   50, 0, 0, 0, 0, 0, 0, 0 },
                      { 98304, 60,     0,  512, 256,   5,  600, 100,  800, 0, 0, 4, 0, 0, 0, 0 } }, { 0, 0, 0, 0 },
                    { 3, 0x5, { [1] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 196000, 32767, 0 }, { 300, EV_OFF, 0, 0, 0 } } },
    /* : rate scaling 7 on both operators, notes B2, B4, B6 and B8, two
     * octaves apart (B8 above A8: the clamp at 8 octaves, 2^-7); B4
     * retriggers B2's release. At B8 n / 128 is a half for 300 and 20 ms at
     * 48 kHz, so the lengths are 310, 18 and 290 ms: no count lies within
     * 0.02 samples of a rounding boundary at either rate */
    { "ratescale", { { { 65536,  0,     0,    0, 256,  40,  310, 200,  400, 0, 0, 0, 7, 0, 0, 0 },
                       { 131072, 0,  1536,    0,   0,  18,  400, 300,  290, 0, 0, 0, 7, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 123471, 32767, 0 }, { 150, EV_OFF, 0, 0, 0 }, { 200, EV_ON, 493883, 32767, 0 }, { 400, EV_OFF, 0, 0, 0 },
        { 500, EV_ON, 1975533, 32767, 0 }, { 650, EV_OFF, 0, 0, 0 }, { 750, EV_ON, 7902133, 32767, 0 }, { 900, EV_OFF, 0, 0, 0 } } },
    /* : the modulator fixed at 1 kHz (ratio 1.0) under a carrier that
     * bends with four pitch events and a retrigger; the modulator's key
     * scaling (break C4, 6 dB per octave) and rate scaling 4 follow the
     * note, G4 then E4 */
    { "fixedop",  { { { 65536,  0,     0,    0, 256,   5,  400, 120,  200, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  2048,    0,   0,   5,  600, 200,  300, 261626, 60, 0, 4, 1000000, 0, 0 } },
                    { 0, 0, 0, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 391995, 32767, 0 }, { 150, EV_PITCH, 415305, 0, 0 }, { 300, EV_PITCH, 440000, 0, 0 },
        { 450, EV_PITCH, 369994, 0, 0 }, { 550, EV_ON, 329628, 32767, 0 }, { 700, EV_PITCH, 349228, 0, 0 }, { 750, EV_OFF, 0, 0, 0 } } },
    /*  on a route of four, carriers 0, 2 and 3, 1 on 0: op 0 and op 1
     * start a quarter turn in at both note-ons from silence; op 2 (a sine)
     * and op 3 ('s noise) run free across the silence between the
     * notes, keeping their phases and op 3 its noise word */
    { "phase",    { { { 65536, 80,     0,    0, 256,   3,  200,  60,   80, 0, 0, 0, 0, 0, 16384, 0 },
                      { 131072, 0,  1024,    0,   0,   3,  300, 120,   80, 0, 0, 0, 0, 0, 16384, 0 },
                      { 98304, 80,     0,    0, 256,   3,  200,  60,   80, 0, 0, 0, 0, 0,     0, 1 },
                      { 65536, 160,    0,    0, 256,   3,  150, 200,   80, 0, 0, 4, 0, 0,     0, 1 } }, { 0, 0, 0, 0 },
                    { 4, 0xD, { [1] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 300, EV_OFF, 0, 0, 0 }, { 550, EV_ON, 220000, 32767, 0 }, { 850, EV_OFF, 0, 0, 0 } } },
    /*  on 's control clock: a 6 Hz sine of 30 cents on both
     * operators' pitch after a 300 ms delay and a 500 ms fade, and a pitch
     * event (A4 to A#4) under the running LFO */
    { "vibrato",  { { { 65536,  0,     0,    0, 256,  20,  600, 120,  200, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  1536,    0,   0,  20,  800, 200,  300, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 },
                    { 6000, 0, 300, 500, 30, 0, 0, 0, 0x3, 0, 0, 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 440000, 32767, 0 }, { 900, EV_PITCH, 466164, 0, 0 }, { 1200, EV_OFF, 0, 0, 0 } } },
    /* : a 5 Hz triangle of 6 dB on the carrier's level from a quarter
     * turn (its peak, no attenuation), no delay; the retrigger at 600 ms
     * restarts it there */
    { "tremolo",  { { { 65536,  0,     0,    0, 256,  10,  500,  60,  250, 0, 0, 0, 0, 0, 0, 0 },
                      { 196608, 0,  1024,    0,   0,  10,  700, 150,  300, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 },
                    { 5000, 1, 0, 0, 0, 60, 0, 16384, 0, 0x1, 0, 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 329628, 32767, 0 }, { 600, EV_ON, 329628, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /*  on a route of three, 2 on 1 on 0: a 4 Hz square of 2 radians
     * on op 1's index (2 radians: 0 to 4) after a 100 ms delay and a 200 ms
     * fade, so that the edge into op 0 moves at the ticks */
    { "wobble",   { { { 65536,  0,     0,    0, 256,   5,  600, 100,  200, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,  2048,    0,   0,   5,  800, 150,  250, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,   512,    0,   0,   5,  900, 200,  250, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 },
                    { 3, 0x1, { [1] = { [0] = 256 }, [2] = { [1] = 256 } }, { { 0 } } },
                    { 4000, 4, 100, 200, 0, 0, 2048, 0, 0, 0, 0x2, 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 98000, 32767, 0 }, { 1200, EV_OFF, 0, 0, 0 } } },
    /* 's sample and hold: 8 Hz, 1200 cents on every operator of a
     * route of three, 1 and 2 on 0, op 1 's noise; op 2 is fixed at
     * 50 Hz  and so takes no pitch offset */
    { "lfosh",    { { { 65536,  0,     0,    0, 256,   5,  700, 100,  200, 0, 0, 0, 0, 0, 0, 0 },
                      { 262144, 0,  1024,    0,   0,   5,  700, 200,  200, 0, 0, 4, 0, 0, 0, 0 },
                      { 65536,  0,   768,    0,   0,   5,  700, 200,  200, 0, 0, 0, 0, 50000, 0, 0 } }, { 0, 0, 0, 0 },
                    { 3, 0x1, { [1] = { [0] = 256 }, [2] = { [0] = 256 } }, { { 0 } } },
                    { 8000, 5, 0, 0, 1200, 0, 0, 0, 0x7, 0, 0, 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1200, EV_OFF, 0, 0, 0 } } },
    /* 's Nyquist hold regression: op 1 starts at 24 kHz, at or
     * above Nyquist at both gate rates; the sine's negative half must
     * lower it again. The pitch factor is applied before the clamp. */
    { "lfonyq",   { { { 65536,  0,     0,    0, 256,  20,  600, 120,  200, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  1536,    0,   0,  20,  800, 200,  300, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 },
                    { 5000, 0, 0, 0, 1200, 0, 0, 0, 0x3, 0, 0, 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 12000000, 32767, 0 }, { 1200, EV_OFF, 0, 0, 0 } } },
    /* : saw up, with pitch, carrier level and modulator index
     * moving together; the negative half reaches the index's zero clamp. */
    { "lfosawup", { { { 65536, 60,     0,    0, 256,   5,  180, 120,  120, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,   512,    0,   0,   5,  250, 180,  160, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 },
                    { 6000, 2, 0, 0, 20, 60, 768, 0, 0x3, 0x1, 0x2, 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 800,
      { { 0, EV_ON, 440000, 32767, 0 }, { 650, EV_OFF, 0, 0, 0 } } },
    /* : saw down with a 100 ms delay and no fade: the depths
     * switch on at the first tick at or after the delay ends. */
    { "lfosawdn", { { { 65536, 60,     0,    0, 256,   5,  180, 120,  120, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,   768,    0,   0,   5,  250, 180,  160, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 },
                    { 5000, 3, 100, 0, 45, 0, 0, 0, 0x3, 0, 0, 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 800,
      { { 0, EV_ON, 440000, 32767, 0 }, { 650, EV_OFF, 0, 0, 0 } } },
    /* : smoothed random crosses several wraps, resets at a
     * retrigger and keeps its current pitch factor on a pitch event. */
    { "lforand",  { { { 65536, 60,     0,    0, 256,   5,  180, 120,  120, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,   768,    0,   0,   5,  250, 180,  160, 0, 0, 0, 0, 0, 0, 0 } }, { 0, 0, 0, 0 }, { 0 },
                    { 10000, 6, 0, 80, 35, 40, 256, 16384, 0x3, 0x1, 0x2, 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 800,
      { { 0, EV_ON, 440000, 32767, 0 }, { 300, EV_ON, 493883, 24000, 0 },
        { 450, EV_PITCH, 523251, 0, 0 }, { 650, EV_OFF, 0, 0, 0 } } },
    /* : pitch-only drop, release and 's noise clock. */
    { "pegdrop",  { { { 65536, 60, 0, 0, 256, 1, 220, 120, 900, 0, 0, 0, 0, 0, 0, 0 },
                      { 4194304, 120, 0, 0, 0, 1, 60, 1200, 60, 0, 0, 4, 0, 0, 0, 0 } },
                    { 0 }, { 2, 0x3, { { 0 } }, { { 0 } } }, { 0 },
                    { { 0, 0, 0, 2400 }, { 80, 0, 0, 120 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 55000, 32767, 0 }, { 250, EV_OFF, 0, 0, 0 }, { 450, EV_ON, 61735, 24000, 0 },
        { 490, EV_PITCH, 65406, 0, 0 }, { 700, EV_OFF, 0, 0, 0 } } },
    /* : combined LFO/peg, fixed-op exclusion and fractional glides
     * through a mid-segment and a mid-release retrigger. */
    { "pegslide", { { { 65536, 0, 0, 0, 256, 5, 800, 120, 300, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536, 0, 1536, 0, 0, 5, 600, 200, 300, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536, 0, 512, 0, 0, 2, 300, 400, 300, 0, 0, 0, 0, 1000000, 0, 0 } },
                    { 0 }, { 3, 0x1, { { 0 }, { 256, 0 }, { 256, 0, 0 } }, { { 0 } } },
                    { 5500, 0, 300, 200, 20, 0, 0, 0, 0x3, 0, 0, 0 },
                    { { 50, -30, 0, -1200 }, { 400, 150, 100, 240 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 200, EV_ON, 246942, 30000, 0 }, { 950, EV_OFF, 0, 0, 0 },
        { 1050, EV_ON, 220000, 32767, 0 }, { 1250, EV_OFF, 0, 0, 0 } } },
    /* : filter envelope attack/retrigger, release, filter event and
     * note-on base reset, all on the shared control grid. */
    { "fenvpluck", { { { 65536, 60, 0, 0, 256, 2, 900, 300, 150, 0, 0, 0, 0, 0, 0, 0 },
                       { 65536, 0, 4096, 256, 0, 2, 900, 200, 150, 0, 0, 0, 0, 0, 0, 0 } },
                     { 300000, 1500, 1, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 },
                     { 0, 20, 600, 200, 0, 4800, 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 130813, 32767, 0 }, { 10, EV_ON, 130813, 24000, 0 }, { 400, EV_OFF, 0, 0, 0 },
        { 750, EV_ON, 130813, 32767, 0 }, { 850, EV_FILTER, 3000000, 1500, 0 }, { 1050, EV_ON, 146832, 30000, 0 },
        { 1250, EV_OFF, 0, 0, 0 } } },
    /* : one octave of key tracking per octave of note pitch. */
    { "ftrack",   { { { 65536, 60, 0, 0, 256, 2, 900, 300, 150, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536, 0, 4096, 256, 0, 2, 900, 200, 150, 0, 0, 0, 0, 0, 0, 0 } },
                    { 300000, 1500, 1, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 },
                    { 0, 20, 600, 200, 0, 4800, 1000 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 130813, 32767, 0 }, { 170, EV_OFF, 0, 0, 0 }, { 370, EV_ON, 261626, 32767, 0 },
        { 540, EV_OFF, 0, 0, 0 }, { 740, EV_ON, 523251, 32767, 0 }, { 910, EV_OFF, 0, 0, 0 },
        { 1110, EV_ON, 1046502, 32767, 0 }, { 1280, EV_OFF, 0, 0, 0 } } },
    /* : the existing three-carrier saturation, observed through a
     * 1 kHz, Q 0.5 band-pass whose int16 output remains unclipped. */
    { "routeclp", { { { 65536,  0,     0,    0, 256,   5,  400,  20,  200, 0, 0, 0, 0, 0, 0, 0 },
                      { 65536,  0,     0,    0, 256,   5,  400,  20,  200, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0,  1024,    0, 256,   5,  400,  20,  200, 0, 0, 0, 0, 0, 0, 0 } }, { 1000000, 500, 2, 0 },
                    { 3, 0x7, { [2] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 110000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    /* : the deepest feed-forward chain, 3 > 2 > 1 > 0. */
    { "deepchain", { { { 65536, 0,    0,    0, 256,   3,  900, 150,  300, 0, 0, 0, 0, 0, 0, 0 },
                       { 131072, 0, 2048,   0,   0,   3,  700, 200,  300, 0, 0, 0, 0, 0, 0, 0 },
                       { 65536, 0, 1536,    0,   0,   2,  500, 300,  300, 0, 0, 0, 0, 0, 0, 0 },
                       { 196608, 0, 768,    0,   0,   2,  400, 300,  300, 0, 0, 0, 0, 0, 0, 0 } }, { 0 },
                     { 4, 0x1, { [3] = { [2] = 256 }, [2] = { [1] = 256 }, [1] = { [0] = 256 } }, { { 0 } } },
                     { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 110000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    /* : RS 4 halves both attacks at A2. A3 at 45 ms keeps their
     * running attack counts/steps while note-dependent rates update. */
    { "rsattack", { { { 65536,  0,     0,    0, 256, 400,  730, 200,  500, 0, 0, 0, 4, 0, 0, 0 },
                      { 131072, 0,  1536,    0,   0, 700,  610, 300,  500, 0, 0, 0, 4, 0, 0, 0 } },
                    { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1200,
      { { 0, EV_ON, 110000, 32767, 0 }, { 45, EV_ON, 220000, 28000, 0 }, { 800, EV_OFF, 0, 0, 0 } } },
    /* : a 440 Hz fixed carrier; only its ratio-2 modulator follows
     * these pitch events and the second note. */
    { "fixedcar", { { { 65536,  0,     0,    0, 256,   5,  400, 120,  200, 0, 0, 0, 0, 440000, 0, 0 },
                      { 131072, 0,  1536,    0,   0,   5,  600, 200,  300, 0, 0, 0, 0, 0, 0, 0 } },
                    { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 150, EV_PITCH, 330000, 0, 0 }, { 300, EV_PITCH, 440000, 0, 0 },
        { 450, EV_PITCH, 110000, 0, 0 }, { 550, EV_ON, 330000, 32767, 0 }, { 700, EV_PITCH, 220000, 0, 0 }, { 750, EV_OFF, 0, 0, 0 } } },
    /* /: initial and inter-note idle gaps, relative/fixed sine
     * operators and half-turn noise all use continuous FREE2 phase. */
    { "freeop", { { { 65536, 100, 0, 0, 256, 5, 160, 120, 80, 0, 0, 0, 0, 0, 8192, 2 },
                     { 65536, 0, 512, 0, 0, 5, 160, 180, 80, 0, 0, 0, 0, 330000, 16384, 2 },
                     { 65536, 140, 0, 0, 256, 5, 160, 160, 80, 0, 0, 0, 0, 440000, 24576, 2 },
                     { 131072, 220, 0, 0, 256, 3, 100, 240, 60, 0, 0, 4, 0, 70000, 16384, 2 } },
                   { 0 }, { 4, 0xD, { [1] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1200,
      { { 37, EV_ON, 220000, 32767, 0 }, { 280, EV_OFF, 0, 0, 0 },
        { 613, EV_ON, 330000, 32767, 0 }, { 920, EV_OFF, 0, 0, 0 } } },
    /* : random interpolation on an init-anchored grid across idle,
     * a sounding retrigger, and restarted per-note delay/fade. */
    { "freelfo", { { { 65536, 60, 0, 0, 256, 5, 180, 120, 80, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0, 768, 0, 0, 5, 250, 180, 80, 0, 0, 0, 0, 0, 0, 0 } },
                    { 0 }, { 0 }, { 10000, 6, 15, 55, 35, 40, 256, 16384, 0x3, 0x1, 0x2, 2 },
                    { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1200,
      { { 29, EV_ON, 440000, 32767, 0 }, { 240, EV_OFF, 0, 0, 0 },
        { 613, EV_ON, 493883, 32767, 0 }, { 721, EV_ON, 523251, 24000, 0 }, { 900, EV_OFF, 0, 0, 0 } } },
    /*  delay: restart/cancel in delay, ordinary attack retriggers,
     * and a fresh silent start with different per-operator delays. */
    { "egdelay", { { { 65536, 60, 0, 0, 256, 10, 80, 120, 300, 0, 0, 0, 0, 0, 0, 0 },
                      { 131072, 0, 2048, 0, 0, 10, 100, 180, 350, 0, 0, 0, 0, 0, 0, 0 } },
                    { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 },
                    { { 30, 0, 0, 0 }, { 60, 0, 0, 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 440000, 32767, 0 }, { 10, EV_ON, 493883, 32767, 0 }, { 20, EV_OFF, 0, 0, 0 },
        { 100, EV_ON, 440000, 32767, 0 }, { 200, EV_ON, 493883, 32767, 0 }, { 205, EV_ON, 523251, 32767, 0 },
        { 650, EV_OFF, 0, 0, 0 }, { 1050, EV_ON, 440000, 32767, 0 }, { 1250, EV_OFF, 0, 0, 0 } } },
    /*  second decay: note-dependent rates, retrigger and release
     * from the new downward segment. */
    { "egdecay2", { { { 65536, 60, 0, 0, 256, 10, 80, 120, 300, 0, 0, 0, 0, 0, 0, 0 },
                       { 131072, 0, 2048, 0, 0, 10, 100, 180, 350, 0, 0, 0, 3, 0, 0, 0 } },
                     { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 },
                     { { 0, 240, 480, 0 }, { 0, 300, 600, 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1800,
      { { 0, EV_ON, 440000, 32767, 0 }, { 250, EV_ON, 440000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 },
        { 800, EV_ON, 329628, 32767, 0 }, { 900, EV_OFF, 0, 0, 0 } } },
    /* : an instantaneous lower second sustain and an equal-target
     * timed second decay preserve their distinct boundary rules. */
    { "egjump", { { { 65536, 60, 0, 0, 256, 10, 80, 120, 300, 0, 0, 0, 0, 0, 0, 0 },
                     { 131072, 0, 2048, 0, 0, 10, 100, 180, 350, 0, 0, 0, 0, 0, 0, 0 } },
                   { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 },
                   { { 0, 0, 480, 0 }, { 0, 120, 180, 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 700,
      { { 0, EV_ON, 440000, 32767, 0 }, { 300, EV_OFF, 0, 0, 0 } } },
    /*  left index scaling follows the note even on a fixed operator;
     * pitch events keep the gain chosen at note-on. */
    { "ksleft", { { { 65536, 60, 0, 0, 256, 10, 80, 120, 300, 0, 0, 0, 0, 0, 0, 0 },
                     { 131072, 0, 2048, 0, 0, 10, 100, 180, 350, 440000, 60, 0, 0, 1000000, 0, 0 } },
                   { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 },
                   { { 0 }, { 0, 0, 0, 120 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1400,
      { { 0, EV_ON, 220000, 32767, 0 }, { 80, EV_PITCH, 880000, 0, 0 }, { 150, EV_OFF, 0, 0, 0 },
        { 400, EV_ON, 440000, 32767, 0 }, { 550, EV_OFF, 0, 0, 0 },
        { 800, EV_ON, 880000, 32767, 0 }, { 950, EV_OFF, 0, 0, 0 } } },
    /* : design's real-PM carrier case, including a silent restart. */
    { "alternating", { { { 65536, 60, 0, 0, 256, 5, 200, 180, 200, 0, 0, 5, 0, 0, 0, 0 },
                          { 98304, 0, 256, 0, 0, 5, 300, 240, 250, 0, 0, 0, 0, 0, 0, 0 } },
                        { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1200,
      { { 0, EV_ON, 220000, 32767, 0 }, { 450, EV_OFF, 0, 0, 0 },
        { 700, EV_ON, 329628, 32767, 0 }, { 900, EV_OFF, 0, 0, 0 } } },
    /* : design's real-PM carrier case, including a silent restart. */
    { "camel", { { { 65536, 60, 0, 0, 256, 5, 200, 180, 200, 0, 0, 6, 0, 0, 0, 0 },
                          { 98304, 0, 256, 0, 0, 5, 300, 240, 250, 0, 0, 0, 0, 0, 0, 0 } },
                        { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1200,
      { { 0, EV_ON, 220000, 32767, 0 }, { 450, EV_OFF, 0, 0, 0 },
        { 700, EV_ON, 329628, 32767, 0 }, { 900, EV_OFF, 0, 0, 0 } } },
    /*  pink: noise as modulator and carrier, two clocks and seeds. */
    { "pink", { { { 65536, 60, 0, 0, 256, 5, 200, 180, 200, 0, 0, 0, 0, 0, 0, 0 },
                     { 65536, 0, 512, 0, 0, 5, 300, 240, 250, 0, 0, 9, 0, 6000000, 0, 0 },
                     { 65536, 120, 0, 0, 256, 5, 200, 180, 200, 0, 0, 9, 0, 4000000, 0, 1 } },
                   { 0 }, { 3, 5, { [1] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1600,
      { { 0, EV_ON, 220000, 32767, 0 }, { 250, EV_ON, 246942, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 },
        { 1000, EV_ON, 329628, 32767, 0 }, { 1300, EV_OFF, 0, 0, 0 } } },
    /*  pink: noise as modulator and carrier, two clocks and seeds. */
    { "pinkfree", { { { 65536, 60, 0, 0, 256, 5, 200, 180, 200, 0, 0, 0, 0, 0, 0, 0 },
                     { 65536, 0, 512, 0, 0, 5, 300, 240, 250, 0, 0, 9, 0, 6000000, 0, 2 },
                     { 65536, 120, 0, 0, 256, 5, 200, 180, 200, 0, 0, 9, 0, 0, 0, 2 } },
                   { 0 }, { 3, 5, { [1] = { [0] = 256 } }, { { 0 } } }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1600,
      { { 37, EV_ON, 220000, 32767, 0 }, { 250, EV_ON, 246942, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 },
        { 1037, EV_ON, 329628, 32767, 0 }, { 1300, EV_OFF, 0, 0, 0 } } },
    /*  colour: the one-pole updates each active audio sample. */
    { "colourlp", { { { 65536, 60, 0, 0, 256, 5, 200, 180, 200, 0, 0, 10, 0, 6000000, 0, 0 },
                         { 98304, 0, 0, 0, 0, 5, 300, 240, 250, 0, 0, 0, 0, 0, 0, 0 } },
                       { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 4 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1600,
      { { 0, EV_ON, 220000, 32767, 0 }, { 250, EV_ON, 246942, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 },
        { 1000, EV_ON, 329628, 32767, 0 }, { 1300, EV_OFF, 0, 0, 0 } } },
    /*  colour: the one-pole updates each active audio sample. */
    { "colourhp", { { { 65536, 60, 0, 0, 256, 5, 200, 180, 200, 0, 0, 11, 0, 6000000, 0, 1 },
                         { 98304, 0, 0, 0, 0, 5, 300, 240, 250, 0, 0, 0, 0, 0, 0, 0 } },
                       { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 7 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1600,
      { { 0, EV_ON, 220000, 32767, 0 }, { 250, EV_ON, 246942, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 },
        { 1000, EV_ON, 329628, 32767, 0 }, { 1300, EV_OFF, 0, 0, 0 } } },
    /*  colour: the one-pole updates each active audio sample. */
    { "colourfree", { { { 65536, 60, 0, 0, 256, 5, 200, 180, 200, 0, 0, 10, 0, 6000000, 0, 2 },
                         { 98304, 0, 0, 0, 0, 5, 300, 240, 250, 0, 0, 0, 0, 0, 0, 0 } },
                       { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 4 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1600,
      { { 37, EV_ON, 220000, 32767, 0 }, { 250, EV_ON, 246942, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 },
        { 1037, EV_ON, 329628, 32767, 0 }, { 1300, EV_OFF, 0, 0, 0 } } },
    /* : unchanged real-PM oracle at the design's discontinuous shapes. */
    { "square", { { { 65536, 60, 0, 0, 256, 5, 200, 180, 200, 0, 0, 7, 0, 0, 0, 0 },
                     { 98304, 0, 256, 0, 0, 5, 300, 240, 250, 0, 0, 0, 0, 0, 0, 0 } },
                   { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1200,
      { { 0, EV_ON, 220000, 32767, 0 }, { 450, EV_OFF, 0, 0, 0 },
        { 700, EV_ON, 329628, 32767, 0 }, { 900, EV_OFF, 0, 0, 0 } } },
    /* : unchanged real-PM oracle at the design's discontinuous shapes. */
    { "logsaw", { { { 65536, 60, 0, 0, 256, 5, 200, 180, 200, 0, 0, 8, 0, 0, 0, 0 },
                     { 98304, 0, 256, 0, 0, 5, 300, 240, 250, 0, 0, 0, 0, 0, 0, 0 } },
                   { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1200,
      { { 0, EV_ON, 220000, 32767, 0 }, { 450, EV_OFF, 0, 0, 0 },
        { 700, EV_ON, 329628, 32767, 0 }, { 900, EV_OFF, 0, 0, 0 } } },
    /* : design's amplitude edges ignore source index and level. */
    { "amedge", { { { 65536, 120, 0, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 },
                     { 32768, 0, 0, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 } },
                   { 0 }, { 2, 1, { [1] = { [0] = 192 } }, { [1] = { [0] = 1 } } },
                   { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 450, EV_PITCH, 330000, 0, 0 },
        { 700, EV_ON, 220000, 24000, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "ringedge", { { { 65536, 120, 0, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 },
                       { 98304, 0, 0, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 },
                       { 16384, 0, 0, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 } },
                     { 0 }, { 3, 1, { [1] = { [0] = 256 }, [2] = { [0] = 128 } },
                                       { [1] = { [0] = 2 }, [2] = { [0] = 1 } } },
                     { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /* A PM back edge hears the ring-processed carrier history. */
    { "ampback", { { { 65536, 120, 128, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 },
                      { 32768, 0, 0, 256, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 } },
                    { 0 }, { 2, 1, { [0] = { [1] = 64 }, [1] = { [0] = 256 } },
                                      { [1] = { [0] = 2 } } },
                    { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 450, EV_PITCH, 330000, 0, 0 },
        { 700, EV_ON, 220000, 24000, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /* : the design's shape fixtures; one LP before the fold until the cascade stage. */
    { "shapehard", { { { 65536, 60, 0, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 },
                         { 65536, 0, 1024, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 } },
                       { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 180, 1, 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "shapecubic", { { { 65536, 60, 0, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 },
                         { 65536, 0, 1024, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 } },
                       { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 180, 2, 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "shapefold1", { { { 65536, 60, 0, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 },
                         { 65536, 0, 1024, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 } },
                       { 1200000, 707, 1, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 180, 3, 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /* : the reviewed two-stage filter and post-filter fold fixtures. */
    { "filtertwo", { { { 65536, 180, 0, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 },
                         { 65536, 0, 4096, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 } },
                       { 900000, 2000, 1, 2 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 300, EV_FILTER, 2400000, 707, 0 },
        { 500, EV_ON, 330000, 32767, 0 }, { 650, EV_OFF, 0, 0, 0 } } },
    { "shapefold", { { { 65536, 60, 0, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 },
                         { 65536, 0, 1024, 0, 0, 2, 100, 0, 120, 0, 0, 0, 0, 0, 0, 0 } },
                       { 1200000, 707, 1, 2 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 180, 3, 0 }, { { 0 } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /* : controller, smoothing, shared destinations and envelope/LFO sources. */
    { "modctl", { { { 65536, 120, 0, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 },
                        { 131072, 0, 1024, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 } },
                      { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 1200, 16, 1, 0, 0, { 0, 0, 0 } } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_CTL, 0, 0, 32768 }, { 0, EV_ON, 220000, 32767, 0 }, { 250, EV_CTL, 0, 0, -32768 }, { 500, EV_PITCH, 330000, 0, 0 }, { 750, EV_CTL, 0, 0, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "modsmooth", { { { 65536, 120, 0, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 },
                        { 131072, 0, 1024, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 } },
                      { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 120, 16, 2, 0, 3, { 0, 0, 0 } } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 100, EV_CTL, 0, 0, 32768 }, { 400, EV_CTL, 0, 0, -32768 }, { 700, EV_ON, 220000, 24000, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "modmany", { { { 65536, 120, 0, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 },
                        { 131072, 0, 1024, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 } },
                      { 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 4800, 16, 1, 0, 0, { 0, 0, 0 } }, { -4800, 17, 1, 0, 0, { 0, 0, 0 } }, { 1024, 7, 3, 1, 0, { 0, 0, 0 } }, { 60, 3, 2, 0, 1, { 0, 0, 0 } } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_CTL, 0, 0, 32768 }, { 0, EV_CTL, 0, 1, 32768 }, { 0, EV_ON, 220000, 32767, 0 }, { 400, EV_CTL, 0, 1, 0 }, { 800, EV_CTL, 0, 0, -32768 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "modfilter", { { { 65536, 120, 0, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 },
                        { 131072, 0, 1024, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 } },
                      { 500000, 707, 1, 0 }, { 0 }, { 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 4800, 16, 4, 0, 0, { 0, 0, 0 } }, { 4000, 17, 5, 0, 2, { 0, 0, 0 } } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 200, EV_CTL, 0, 0, 32768 }, { 200, EV_CTL, 0, 1, 32768 }, { 500, EV_FILTER, 1000000, 1000, 0 }, { 700, EV_ON, 220000, 32767, 0 }, { 900, EV_OFF, 0, 0, 0 } } },
    { "modlfoself", { { { 65536, 120, 0, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 },
                        { 131072, 0, 1024, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 } },
                      { 0 }, { 0 }, { 3000, 1, 0, 0, 20, 0, 0, 0, 3, 1, 2, 0 }, { { 0 }, { 0 }, 0 }, { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 2000, 0, 6, 0, 0, { 0, 0, 0 } }, { 80, 16, 7, 0, 0, { 0, 0, 0 } }, { 512, 0, 3, 1, 3, { 0, 0, 0 } } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_CTL, 0, 0, 32768 }, { 0, EV_ON, 220000, 32767, 0 }, { 650, EV_ON, 220000, 32767, 0 }, { 800, EV_CTL, 0, 0, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "modenv", { { { 65536, 120, 0, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 },
                        { 131072, 0, 1024, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 } },
                      { 1000000, 707, 1, 0 }, { 0 }, { 0 }, { { 1200, 0, 0, -1200 }, { 200, 300, 0, 150 }, 0 }, { 0, 50, 300, 150, 500, 1200, 0 }, { { 0 } }, { { 0 } }, { 0 }, { { 1200, 1, 1, 1, 1, { 0, 0, 0 } }, { 1024, 2, 3, 1, 0, { 0, 0, 0 } }, { 120, 8, 2, 0, 0, { 0, 0, 0 } } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 300, EV_PITCH, 330000, 0, 0 }, { 700, EV_ON, 440000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /*  wide base: 31.25/34.0136 turns brought into audible range by -9600 cents. */
    { "modwide", { { { 65536, 120, 0, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 },
                         { 131072, 0, 0, 0, 0, 2, 100, 0, 180, 0, 0, 0, 0, 0, 0, 0 } },
                     { 0 }, { 0 }, { 0 }, { { -4800, -4800, -4800, -4800 }, { 0, 0, 0, 0 }, 1 },
                     { 0 }, { { 0 } }, { { 0 } }, { 0 }, { { -4800, 16, 1, 0, 0, { 0, 0, 0 } } } , { { 0 } } , { 0 } , { { 0 } } , { { 0 } } , { { 0 } } , { { 0 } } , { 0 } }, 1500,
      { { 0, EV_CTL, 0, 0, 32768 }, { 0, EV_ON, 1500000000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /* : independent phase distortion and the two extreme knees. */
    { "pdknee", { .op = { { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
                              { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
                       .pd = { { 8192, 1 } } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    { "pdmorph", { .op = { { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
                              { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
                       .pd = { { 16384, 1 } }, .mod = { { 8192, 16, 33, 0, 3, { 0, 0, 0 } } },
                       .peg = { { 0, 0, 0, -1200 }, { 200, 0, 0, 200 }, 0 } }, 2000,
      { { 0, EV_CTL, 0, 0, -32768 }, { 0, EV_ON, 220000, 32767, 0 }, { 400, EV_CTL, 0, 0, 32768 }, { 900, EV_CTL, 0, 0, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    { "pdmin", { .op = { { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
                              { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
                       .pd = { { 1, 1 } } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    { "pdmax", { .op = { { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
                              { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
                       .pd = { { 32767, 1 } } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    /* : moving vector, with independent LFO/PEG coordinate sources. */
    { "vectorxy", { .op = {
        { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
        { .ratio_q16=131072, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
        { .ratio_q16=196608, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
        { .ratio_q16=262144, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
        .route = { .ops=4, .carriers=15 }, .vector = { 16384, 16384, 1 },
        .lfo = { .rate_millihz=1000, .wave=1 },
        .peg = { { 1200, -1200, 0, 0 }, { 400, 600, 200, 300 }, 0 },
        .mod = { { 16384, 0, 34, 0, 3, { 0, 0, 0 } }, { 16384, 1, 35, 0, 3, { 0, 0, 0 } } } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    /* : independent parallel formants, then a moving center/final filter. */
    { "formant3", { .op = {
        { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300, .wave=4 },
        { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
        .formant = { { 500000, 2000, 60, 1 }, { 1500000, 3000, 120, 1 }, { 2500000, 4000, 180, 1 } } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    { "formantmove", { .op = {
        { .ratio_q16=65536, .level_db10=240, .attack_ms=5, .decay_ms=100, .release_ms=300 },
        { .ratio_q16=131072, .level_db10=240, .attack_ms=5, .decay_ms=100, .release_ms=300 },
        { .ratio_q16=196608, .level_db10=240, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
        .route = { .ops=3, .carriers=7 }, .filter = { 4000000, 707, 1, 0 },
        .fenv = { 0, 0, 500, 200, 0, 1200, 0 },
        .formant = { { 500000, 2000, 60, 1 }, { 1500000, 3000, 120, 1 }, { 2500000, 4000, 180, 1 } },
        .mod = { { 300000, 16, 36, 1, 0, { 0, 0, 0 } } } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 600, EV_CTL, 0, 0, 32768 }, { 1500, EV_OFF, 0, 0, 0 } } },
    /* : design-'s sync fixtures, a source above its target (same
     * sample) and one below it (the next sounding sample) beside a ring edge. */
    { "syncturn", { .op = {
        { .ratio_q16=180224, .level_db10=120, .attack_ms=2, .decay_ms=100, .release_ms=120, .phase_u16=16384 },
        { .ratio_q16=65536, .attack_ms=2, .decay_ms=100, .release_ms=120 } },
        .route = { .ops=2, .carriers=1, .w = { [1] = { [0] = 256 } }, .kind = { [1] = { [0] = 3 } } } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 450, EV_PITCH, 330000, 0, 0 },
        { 700, EV_ON, 220000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "syncback", { .op = {
        { .ratio_q16=65536, .attack_ms=2, .decay_ms=100, .release_ms=120 },
        { .ratio_q16=32768, .attack_ms=2, .decay_ms=100, .release_ms=120 },
        { .ratio_q16=180224, .level_db10=120, .attack_ms=2, .decay_ms=100, .release_ms=120 } },
        .route = { .ops=3, .carriers=4, .w = { [0] = { [2] = 256 }, [1] = { [2] = 128 } },
                   .kind = { [0] = { [2] = 3 }, [1] = { [2] = 2 } } } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /* : design-hybrid's wavetable fixtures (section 7), each reading
     * its bank from CASE_WT below through the binding; the carrier is the
     * wavetable operator and the modulator's index is 0. */
    { "wtframe", { .op = {
        { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
        { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
        .wt = { { 0, 0, 1 } }, .mod = { { 32768, 16, 32, 0, 0, { 0, 0, 0 } } } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 400, EV_CTL, 0, 0, 16384 },
        { 800, EV_CTL, 0, 0, 32768 }, { 1500, EV_OFF, 0, 0, 0 } } },
    { "wtmorph", { .op = {
        { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
        { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
        .lfo = { .rate_millihz=2000, .wave=1 }, .wt = { { 16384, 0, 1 } },
        .mod = { { 16384, 0, 32, 0, 3, { 0, 0, 0 } } } }, 2000,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    { "wtbands", { .op = {
        { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
        { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
        .wt = { { 0, 0, 1 } } }, 2000,
      { { 0, EV_ON, 5500000, 32767, 0 }, { 400, EV_PITCH, 6000000, 0, 0 }, { 800, EV_PITCH, 12000000, 0, 0 },
        { 1200, EV_PITCH, 5000000, 0, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    { "additive", { .op = {
        { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=300 },
        { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
        .wt = { { 0, 0, 1 } } }, 2000,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1500, EV_OFF, 0, 0, 0 } } },
    /* : design-hybrid's Karplus-Strong fixtures (section 7): a plucked
     * A2 released at 900 ms, and the same pluck bent up an octave in six
     * whole tones without a refill, through and past its note-off. */
    { "kspluck", { .op = {
        { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=800 },
        { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
        .ks = { { 7, 32700, 2048, 1 } } }, 2000,
      { { 0, EV_ON, 110000, 32767, 0 }, { 900, EV_OFF, 0, 0, 0 } } },
    { "ksbend", { .op = {
        { .ratio_q16=65536, .level_db10=180, .attack_ms=5, .decay_ms=100, .release_ms=800 },
        { .ratio_q16=65536, .attack_ms=5, .decay_ms=100, .release_ms=300 } },
        .ks = { { 7, 32700, 2048, 1 } } }, 2000,
      { { 0, EV_ON, 110000, 32767, 0 }, { 0, EV_PITCH, 110000, 0, 0 }, { 200, EV_PITCH, 123471, 0, 0 },
        { 400, EV_PITCH, 138591, 0, 0 }, { 600, EV_PITCH, 155563, 0, 0 }, { 800, EV_PITCH, 174614, 0, 0 },
        { 900, EV_OFF, 0, 0, 0 }, { 1000, EV_PITCH, 195998, 0, 0 }, { 1200, EV_PITCH, 220000, 0, 0 } } },
    /* : 's shape fixture with the cubic before a resonant low-pass, and
     * the asymmetric kind 4 after no filter and before that low-pass. The two
     * pre-filter cases drive a -12 dB carrier by +8 dB (0.63 into the shaper)
     * so the resonant low-pass output stays below int16 full scale. */
    { "shapepre", { .op = {
        { .ratio_q16=65536, .level_db10=120, .attack_ms=2, .decay_ms=100, .release_ms=120 },
        { .ratio_q16=65536, .index_q10=1024, .attack_ms=2, .decay_ms=100, .release_ms=120 } },
        .filter = { 1200000, 2000, 1, 0 }, .shape = { 80, 2, 1 } }, 1500,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "shapeasym", { .op = {
        { .ratio_q16=65536, .level_db10=60, .attack_ms=2, .decay_ms=100, .release_ms=120 },
        { .ratio_q16=65536, .index_q10=1024, .attack_ms=2, .decay_ms=100, .release_ms=120 } },
        .shape = { 180, 4, 0 } }, 1500,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "shapeasympre", { .op = {
        { .ratio_q16=65536, .level_db10=120, .attack_ms=2, .decay_ms=100, .release_ms=120 },
        { .ratio_q16=65536, .index_q10=1024, .attack_ms=2, .decay_ms=100, .release_ms=120 } },
        .filter = { 1200000, 2000, 1, 0 }, .shape = { 80, 4, 1 } }, 1500,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /* : kinds 1 and 3 before the same resonant low-pass, driven +14 dB
     * (1.25 into the shaper) so the clip and the fold act; at any drive where
     * they act this filter's output passes int16 full scale, as shapehard's
     * +1 plateau meets it. */
    { "shapeprehard", { .op = {
        { .ratio_q16=65536, .level_db10=120, .attack_ms=2, .decay_ms=100, .release_ms=120 },
        { .ratio_q16=65536, .index_q10=1024, .attack_ms=2, .decay_ms=100, .release_ms=120 } },
        .filter = { 1200000, 2000, 1, 0 }, .shape = { 140, 1, 1 } }, 1500,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    { "shapeprefold", { .op = {
        { .ratio_q16=65536, .level_db10=120, .attack_ms=2, .decay_ms=100, .release_ms=120 },
        { .ratio_q16=65536, .index_q10=1024, .attack_ms=2, .decay_ms=100, .release_ms=120 } },
        .filter = { 1200000, 2000, 1, 0 }, .shape = { 140, 3, 1 } }, 1500,
      { { 0, EV_ON, 110000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /*  : level key scaling about a 440 Hz break at notes two
     * octaves either side. Carrier 0 right - 6 dB/oct, carrier 1 right +
     * 9 dB/oct (at 1760 Hz 12 - 18 dB clamps at 0 dB), carrier 2 left +
     * 6 dB/oct; op 3 boosts its 8 rad index right + 15 dB/oct into
     * carrier 0 at 1/16 weight, which saturates at 131070 Q10 at 1760 Hz.
     * Velocity 0.5 at full sensitivity, applied after the clamps, keeps the
     * sum below int16 full scale. */
    { "kslevel", { .op = {
        { .ratio_q16=65536, .level_db10=120, .vel_sens_q8=256, .attack_ms=5, .decay_ms=200, .sustain_db10=60, .release_ms=150,
          .ks_break_millihz=440000, .ks_slope_db10=60 },
        { .ratio_q16=131072, .level_db10=120, .vel_sens_q8=256, .attack_ms=5, .decay_ms=200, .sustain_db10=60, .release_ms=150,
          .ks_break_millihz=440000, .ks_slope_db10=90 },
        { .ratio_q16=196608, .level_db10=120, .vel_sens_q8=256, .attack_ms=5, .decay_ms=200, .sustain_db10=60, .release_ms=150,
          .ks_break_millihz=440000 },
        { .ratio_q16=65536, .index_q10=8192, .vel_sens_q8=256, .attack_ms=5, .decay_ms=300, .sustain_db10=60, .release_ms=150,
          .ks_break_millihz=440000, .ks_slope_db10=150 } },
        .route = { 4, 0x7, { [3] = { [0] = 16 } }, { { 0 } } },
        .eg = { [2] = { .left_slope_db10=60 } },
        .ext = { { .ks_target=1 }, { .ks_target=1, .ks_curve=1 }, { .ks_target=1, .ks_curve=2 }, { .ks_target=0, .ks_curve=1 } } }, 1600,
      { { 0, EV_ON, 110000, 16384, 0 }, { 300, EV_OFF, 0, 0, 0 },
        { 500, EV_ON, 440000, 16384, 0 }, { 800, EV_OFF, 0, 0, 0 },
        { 1000, EV_ON, 1760000, 16384, 0 }, { 1300, EV_OFF, 0, 0, 0 } } },
    /* : the fixed pair's modulator with 1.5 rad of squared feedback
     * into the carrier at 1 rad. The squared source has only even
     * harmonics, so the modulator's output has only odd ones
     * in the reference comparison. */
    { "fbsquare", { .op = {
        { .ratio_q16=65536, .level_db10=60, .attack_ms=5, .decay_ms=400, .sustain_db10=60, .release_ms=200 },
        { .ratio_q16=65536, .index_q10=1024, .feedback_q10=1536, .attack_ms=5, .decay_ms=300, .sustain_db10=30, .release_ms=300 } },
        .ext = { [1] = { .fb_type=2 } } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /* : a routed pair. The modulator's 1.5 rad feedback reads the
     * samples before its envelope, which decays to -40 dB, so the
     * feedback keeps its depth; the carrier adds 0.25 rad of squared
     * pre-envelope feedback. */
    { "fbpre", { .op = {
        { .ratio_q16=65536, .level_db10=60, .feedback_q10=256, .attack_ms=5, .decay_ms=600, .sustain_db10=120, .release_ms=200 },
        { .ratio_q16=65536, .index_q10=1536, .feedback_q10=1536, .attack_ms=5, .decay_ms=800, .sustain_db10=400, .release_ms=200 } },
        .route = { 2, 0x1, { [1] = { [0] = 256 } }, { { 0 } } },
        .ext = { { .fb_type=3 }, { .fb_type=1 } } }, 1500,
      { { 0, EV_ON, 146832, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
    /* Amp envelope fixtures on a fixed operator pair.
     * The operators release over 2000 ms, so the amp's
     * 300 ms release from -12 dB reaches idle first and ends the voice;
     * ampring adds a resonant low-pass and releases at 600 ms, ampretrig
     * retriggers in the amp release ( from the amp's own gain). */
    { "ampeg", { .op = {
        { .ratio_q16=65536, .level_db10=60, .attack_ms=2, .decay_ms=100, .release_ms=2000 },
        { .ratio_q16=65536, .index_q10=1024, .attack_ms=2, .decay_ms=100, .release_ms=2000 } },
        .amp = { 5, 200, 120, 300, 1 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 900, EV_OFF, 0, 0, 0 } } },
    { "ampring", { .op = {
        { .ratio_q16=65536, .level_db10=60, .attack_ms=2, .decay_ms=100, .release_ms=2000 },
        { .ratio_q16=65536, .index_q10=1024, .attack_ms=2, .decay_ms=100, .release_ms=2000 } },
        .filter = { 900000, 2000, 1, 0 }, .amp = { 5, 200, 120, 300, 1 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 600, EV_OFF, 0, 0, 0 } } },
    { "ampretrig", { .op = {
        { .ratio_q16=65536, .level_db10=60, .attack_ms=2, .decay_ms=100, .release_ms=2000 },
        { .ratio_q16=65536, .index_q10=1024, .attack_ms=2, .decay_ms=100, .release_ms=2000 } },
        .amp = { 5, 200, 120, 300, 1 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 900, EV_OFF, 0, 0, 0 }, { 950, EV_ON, 220000, 32767, 0 } } },
    /* Loop fixture on a routed pair.
     * The carrier loops attack 30, decay 80 to -24 dB and decay2 60 to
     * -36 dB while held, each attack from the -36 dB sustain gain at the
     * full attack's slope; the note-off at 1100 ms releases from wherever
     * the loop is. */
    { "egloop", { .op = {
        { .ratio_q16=65536, .level_db10=60, .attack_ms=30, .decay_ms=80, .sustain_db10=240, .release_ms=200 },
        { .ratio_q16=65536, .index_q10=1024, .attack_ms=5, .decay_ms=400, .sustain_db10=120, .release_ms=200 } },
        .eg = { { .decay2_ms=60, .sustain2_db10=360 } },
        .route = { 2, 0x1, { [1] = { [0] = 256 } }, { { 0 } } },
        .ext = { { .loop=1 } } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } },
};
#define NCASE (sizeof CASES / sizeof CASES[0])

static void put_u32(FILE *f, uint32_t v) { unsigned char b[4] = { (unsigned char)v, (unsigned char)(v >> 8), (unsigned char)(v >> 16), (unsigned char)(v >> 24) }; fwrite(b, 1, 4, f); }
static void put_u16(FILE *f, uint16_t v) { unsigned char b[2] = { (unsigned char)v, (unsigned char)(v >> 8) }; fwrite(b, 1, 2, f); }

/* realloc of n elements of size bytes, a malloc when p is NULL; NULL when
 * the size does not fit or the allocation fails, p then left as it was. */
static void *realloc_n(void *p, size_t n, size_t size)
{
    return n && size > SIZE_MAX / n ? NULL : realloc(p, n * size);
}

static void *alloc_n(size_t n, size_t size)
{
    return realloc_n(NULL, n, size);
}

/* : section 2's canonical content hash of a bank, FNV-1a 64 over its
 * frame count, length and band count as little-endian uint32, then every
 * sample as little-endian int16 in band, frame, sample order. */
static uint64_t wt_content_hash(uint32_t frames, uint32_t length, const int16_t *data)
{
    const uint32_t dims[3] = { frames, length, FM_WT_BANDS };
    uint64_t h = 14695981039346656037ULL;
    uint32_t i, n = FM_WT_BANDS * frames * length;
    int k;
    for (i = 0; i < 3u; i++)
        for (k = 0; k < 4; k++) { h ^= (dims[i] >> (8 * k)) & 0xFFu; h *= 1099511628211ULL; }
    for (i = 0; i < n; i++) {
        const uint16_t u = (uint16_t)data[i];
        h ^= u & 0xFFu;        h *= 1099511628211ULL;
        h ^= (u >> 8) & 0xFFu; h *= 1099511628211ULL;
    }
    return h;
}

/* The wavetable cases' banks (design-hybrid section 7), generated by
 * fm_wt_additive on first use: harmonics as frame, number, level, phase.
 * Short frames keep the goldens small; additive takes the longest. */
#define CASE_WT_CAP (FM_WT_BANDS * 2u * 2048u)
static const struct case_wt {
    const char *name;
    uint32_t frames, length, count;
    fm_wt_harmonic_t h[4];
} CASE_WT[] = {
    { "wtframe",  2,  256, 2, { { 0, 1, 0, 0 }, { 1, 2, 0, 0 } } },
    { "wtmorph",  2,  256, 2, { { 0, 1, 0, 0 }, { 1, 2, 0, 0 } } },
    { "wtbands",  1,  256, 4, { { 0, 1, 0, 0 }, { 0, 2, 60, 0 }, { 0, 3, 120, 0 }, { 0, 4, 180, 0 } } },
    { "additive", 1, 2048, 3, { { 0, 1, 0, 0 }, { 0, 2, 60, 16384 }, { 0, 3, 120, 32768 } } },
};
#define NCASE_WT (sizeof CASE_WT / sizeof CASE_WT[0])
static int16_t case_wt_data[NCASE_WT][CASE_WT_CAP];

/* : a Karplus-Strong case's buffers, one per operator, and the
 * reference's doubles; a case's engine voice and reference voice never
 * share storage, and its renders run one after another. */
static int32_t case_ks_data[FM_OPS][FM_KS_BUFFER_MAX];
static double case_ks_ref[FM_OPS * FM_KS_BUFFER_MAX];

/* The binding a case's voices take: its bank in slot 0, its operators'
 * Karplus-Strong buffers, or NULL. */
static const fm_assets_t *case_assets(const spike_case_t *c)
{
    static fm_wt_view_t view[NCASE_WT];
    static fm_assets_t assets[NCASE_WT];
    static fm_ks_view_t ks_view[FM_OPS];
    static fm_assets_t ks_assets;
    size_t i;
    for (i = 0; i < FM_OPS && !c->patch.ks[i].enabled; i++) {}
    if (i < FM_OPS) {
        for (i = 0; i < FM_OPS; i++) {
            ks_view[i].data = c->patch.ks[i].enabled ? case_ks_data[i] : NULL;
            ks_view[i].capacity = c->patch.ks[i].enabled ? FM_KS_BUFFER_MAX : 0u;
        }
        ks_assets = (fm_assets_t){ sizeof ks_assets, 0, NULL, FM_OPS, ks_view };
        return &ks_assets;
    }
    for (i = 0; i < NCASE_WT; i++)
        if (!strcmp(c->name, CASE_WT[i].name)) {
            const struct case_wt *w = &CASE_WT[i];
            if (!assets[i].size) {
                if (fm_wt_additive(case_wt_data[i], CASE_WT_CAP, w->frames, w->length, w->h, w->count) != FM_BIND_OK) {
                    fprintf(stderr, "%s: the bank does not generate\n", c->name);
                    exit(1);
                }
                view[i].frames = w->frames; view[i].length = w->length; view[i].bands = FM_WT_BANDS;
                view[i].capacity = CASE_WT_CAP; view[i].data = case_wt_data[i];
                view[i].data_hash = wt_content_hash(w->frames, w->length, case_wt_data[i]);
                assets[i].size = sizeof assets[i]; assets[i].wt_count = 1u; assets[i].wt = &view[i];
            }
            return &assets[i];
        }
    return NULL;
}

/* fm_voice_init with the case's binding; a refused binding ends the run. */
static void case_voice_init(fm_voice_t *v, const spike_case_t *c, uint32_t fs)
{
    const fm_assets_t *a = case_assets(c);
    fm_voice_init(v, &c->patch, fs);
    if (a && fm_voice_bind_assets(v, a) != FM_BIND_OK) { fprintf(stderr, "%s: the binding is refused\n", c->name); exit(1); }
}

static void case_ref_init(ref_voice_t *r, const spike_case_t *c, uint32_t fs)
{
    ref_voice_init(r, &c->patch, fs);
    ref_voice_bind_assets(r, case_assets(c));
    ref_voice_bind_ks(r, case_assets(c), case_ks_ref);
}

/* Host path errors retain the original cause before close/remove cleanup. */
enum { HOST_ERROR = 5 };
/* Bounds suites return counts; this sentinel preserves allocation failures
 * without counting them as failed synthesis criteria. */
#define BOUNDS_HOST_ERROR UINT_MAX

static int io_fail(const char *path, const char *verb)
{
    const int error = errno ? errno : EIO;
    fprintf(stderr, "%s: cannot %s: %s\n", path, verb, strerror(error));
    return HOST_ERROR;
}

/* Failed requested output takes precedence over a completed render's
 * verdict. Otherwise preserve the first error; these are codes, not bits. */
static int exit_merge(int current, int next)
{
    return next == HOST_ERROR || !current ? next : current;
}

/* A short input at EOF keeps the reader's format verdict; a host read or
 * close failure has its own class. Capture errno before closing. */
static int io_close_input(FILE *f, const char *path, int rc)
{
    if (ferror(f)) rc = io_fail(path, "read");
    if (fclose(f)) rc = exit_merge(rc, io_fail(path, "close"));
    return rc;
}

/* Fixed sibling temporary, owned only after exclusive creation. Never
 * truncate or remove a preexisting temporary (including a symlink). */
static FILE *io_open_tmp(const char *path, const char *mode, char **tmp)
{
    const size_t n = strlen(path);
    FILE *f;
    int fd;
    *tmp = NULL;
    if (n > SIZE_MAX - 5u) { errno = ENAMETOOLONG; io_fail(path, "write"); return NULL; }
    *tmp = (char *)malloc(n + 5u);
    if (!*tmp) { errno = ENOMEM; io_fail(path, "write"); return NULL; }
    memcpy(*tmp, path, n);
    memcpy(*tmp + n, ".tmp", 5u);
    fd = open(*tmp, O_WRONLY | O_CREAT | O_EXCL, 0666);
    if (fd < 0) {
        io_fail(path, "write");
        free(*tmp); *tmp = NULL;
        return NULL;
    }
    f = fdopen(fd, mode);
    if (!f) {
        io_fail(path, "write");
        close(fd); remove(*tmp);
        free(*tmp); *tmp = NULL;
    }
    return f;
}

/* Commit only a fully flushed and closed file; retain the destination on
 * failure. The stream formatter's caller still owns its stream lifetime. */
static int io_finish_tmp(FILE *f, const char *path, char *tmp, int bad)
{
    int rc = 0;
    if (bad || fflush(f)) rc = io_fail(path, "write");
    if (fclose(f) && !rc) rc = io_fail(path, "write");
    if (!rc && rename(tmp, path)) rc = io_fail(path, "write");
    if (rc) remove(tmp);
    free(tmp);
    return rc;
}

/* Opt-in stem publication guards. These are observations, not filesystem locks. */
typedef struct { char *resolved; struct stat st; int exists; } stem_path_t;

static int stem_path_get(const char *path, int destination, stem_path_t *id)
{
    char *parent = NULL, *base;
    const char *slash;
    size_t parent_len, leaf_len, resolved_len;
    memset(id, 0, sizeof *id);
    id->resolved = realpath(path, NULL);
    if (id->resolved) {
        if (stat(path, &id->st)) { free(id->resolved); id->resolved = NULL; return io_fail(path, "stat"); }
        id->exists = 1;
        return 0;
    }
    if (!destination || errno != ENOENT) return io_fail(path, "resolve");
    slash = strrchr(path, '/');
    parent_len = slash ? (size_t)(slash - path) : 1u;
    if (!parent_len) parent_len = 1u;
    parent = (char *)malloc(parent_len + 1u);
    if (!parent) { errno = ENOMEM; return io_fail(path, "resolve"); }
    if (slash) memcpy(parent, path, parent_len);
    else parent[0] = '.';
    parent[parent_len] = 0;
    base = realpath(parent, NULL);
    free(parent);
    if (!base) return io_fail(path, "resolve parent");
    if (stat(base, &id->st)) { free(base); return io_fail(path, "stat parent"); }
    if (!S_ISDIR(id->st.st_mode)) { free(base); errno = ENOTDIR; return io_fail(path, "resolve parent"); }
    leaf_len = strlen(slash ? slash + 1 : path);
    resolved_len = strlen(base);
    if (resolved_len > SIZE_MAX - leaf_len - 2u) {
        free(base); errno = ENAMETOOLONG; return io_fail(path, "resolve");
    }
    id->resolved = (char *)malloc(resolved_len + leaf_len + 2u);
    if (!id->resolved) { free(base); errno = ENOMEM; return io_fail(path, "resolve"); }
    memcpy(id->resolved, base, resolved_len);
    id->resolved[resolved_len] = '/';
    memcpy(id->resolved + resolved_len + 1u, slash ? slash + 1 : path, leaf_len + 1u);
    free(base);
    return 0;
}

static int stem_paths_same(const stem_path_t *a, const stem_path_t *b)
{
    return !strcmp(a->resolved, b->resolved)
        || (a->exists && b->exists && a->st.st_dev == b->st.st_dev && a->st.st_ino == b->st.st_ino);
}

static int stem_paths_check(const char *stem, const char *merged, char **files, int nf)
{
    stem_path_t destinations[2], input;
    const char *names[2] = { stem, merged };
    int i, j, rc = 0;
    memset(destinations, 0, sizeof destinations);
    for (i = 0; i < 2 && !rc; ++i) if (names[i]) rc = stem_path_get(names[i], 1, &destinations[i]);
    if (!rc && stem && merged && stem_paths_same(&destinations[0], &destinations[1])) {
        fprintf(stderr, "--stems-out and --merged destinations identify the same file\n"); rc = 2;
    }
    for (j = 0; j < nf && !rc; ++j) {
        rc = stem_path_get(files[j], 0, &input);
        if (!rc) {
            for (i = 0; i < 2 && !rc; ++i) if (names[i] && stem_paths_same(&destinations[i], &input)) {
                fprintf(stderr, "stem/canonical output destination identifies an input: %s\n", files[j]); rc = 2;
            }
            free(input.resolved);
        }
    }
    for (i = 0; i < 2; ++i) free(destinations[i].resolved);
    return rc;
}

/* The same forward-only PCM16LE serializer serves atomic files and an
 * explicitly inherited pipe. Its callers validate the RIFF dimensions. */
static int write_wav_stream(FILE *f, const int16_t *pcm, uint32_t n, uint32_t fs, uint32_t ch)
{
    uint32_t i;
    fwrite("RIFF", 1, 4, f); put_u32(f, 36u + 2u * n); fwrite("WAVE", 1, 4, f);
    fwrite("fmt ", 1, 4, f); put_u32(f, 16); put_u16(f, 1); put_u16(f, (uint16_t)ch);
    put_u32(f, fs); put_u32(f, fs * 2u * ch); put_u16(f, (uint16_t)(2u * ch)); put_u16(f, 16);
    fwrite("data", 1, 4, f); put_u32(f, 2u * n);
    for (i = 0; i < n; i++) put_u16(f, (uint16_t)pcm[i]);
    return ferror(f);
}

/* Mono int16 WAV. Returns nonzero, with a message, unless the whole file
 * was written. The RIFF sizes and the byte rate are 32-bit fields, so a
 * file holds at most 2147483629 samples at most 2147483647 Hz. */
/* n int16 samples of ch interleaved channels (n a multiple of ch). */
static int write_wav_ch(const char *path, const int16_t *pcm, uint32_t n, uint32_t fs, uint32_t ch)
{
    FILE *f;
    char *tmp;
    int bad;
    if (n > (UINT32_MAX - 36u) / 2u) { fprintf(stderr, "%s: %u samples are too many for a WAV file\n", path, n); return 1; }
    if (fs > UINT32_MAX / (2u * ch)) { fprintf(stderr, "%s: %u Hz is too fast for a WAV file\n", path, fs); return 1; }
    f = io_open_tmp(path, "wb", &tmp);
    if (!f) return HOST_ERROR;
    bad = write_wav_stream(f, pcm, n, fs, ch);
    return io_finish_tmp(f, path, tmp, bad);
}

/* Own and close fd on every path. SIGPIPE is ignored only by the opt-in
 * invocation, so a closed reader becomes an ordinary requested-output error. */
static int write_wav_fd(int fd, const int16_t *pcm, uint32_t n, uint32_t fs, uint32_t ch)
{
    FILE *f = fdopen(fd, "wb");
    int rc = 0;
    if (!f) { rc = io_fail("--wav-fd", "open stream"); close(fd); return rc; }
    if (write_wav_stream(f, pcm, n, fs, ch) || fflush(f)) rc = io_fail("--wav-fd", "write");
    if (fclose(f) && !rc) rc = io_fail("--wav-fd", "close");
    return rc;
}

static int write_wav(const char *path, const int16_t *pcm, uint32_t n, uint32_t fs)
{
    return write_wav_ch(path, pcm, n, fs, 1);
}

/* Saturates to int16; NaN gives -32768 rather than an undefined conversion. */
static int16_t clip16(double x)
{
    if (x >= 32767.0) return 32767;
    if (x > -32768.0) return (int16_t)x;
    return -32768;
}

static double db(double num, double den)
{
    if (den <= 0.0) return 999.0;
    return 10.0 * log10(num / den);
}

static int ev_count(const spike_case_t *c)
{
    int e = 0;
    while (e < MAX_EV && c->ev[e].type != EV_END) e++;
    return e;
}

static uint32_t ev_sample(const spike_event_t *e, uint32_t fs, uint32_t n)
{
    uint32_t t = (uint32_t)((uint64_t)e->t_ms * fs / 1000u);
    return t < n ? t : n;
}

static void a1_event(fm_voice_t *v, const spike_event_t *e)
{
    if (e->type == EV_ON) fm_voice_note_on(v, e->freq_millihz, e->velocity_q15);
    else if (e->type == EV_PITCH) fm_voice_pitch(v, e->freq_millihz);
    else if (e->type == EV_FILTER) fm_voice_filter(v, e->freq_millihz, e->velocity_q15);
    else if (e->type == EV_CTL) fm_voice_ctl(v, e->velocity_q15, e->ctl_value);
    else fm_voice_note_off(v);
}

static void ref_event(ref_voice_t *v, const spike_event_t *e)
{
    if (e->type == EV_ON) ref_voice_note_on(v, e->freq_millihz, e->velocity_q15);
    else if (e->type == EV_PITCH) ref_voice_pitch(v, e->freq_millihz);
    else if (e->type == EV_FILTER) ref_voice_filter(v, e->freq_millihz, e->velocity_q15);
    else if (e->type == EV_CTL) ref_voice_ctl(v, e->velocity_q15, e->ctl_value);
    else ref_voice_note_off(v);
}

/* Integer engine, one block per stretch between events. */
static void render_a1_blocks(const spike_case_t *c, uint32_t fs, int16_t *pcm, uint32_t n)
{
    fm_voice_t v;
    uint32_t pos = 0;
    int e, ne = ev_count(c);
    case_voice_init(&v, c, fs);
    for (e = 0; e < ne; e++) {
        uint32_t t = ev_sample(&c->ev[e], fs, n);
        fm_render_i16(&v, pcm + pos, t - pos);
        pos = t;
        a1_event(&v, &c->ev[e]);
    }
    fm_render_i16(&v, pcm + pos, n - pos);
}

/* Integer engine, one sample per call: block size must not matter. Returns
 * the first sample of the final silent run of the voice (n if it never ends). */
static uint32_t render_a1_samples(const spike_case_t *c, uint32_t fs, int16_t *pcm, uint32_t n)
{
    fm_voice_t v;
    uint32_t i, idle = n;
    int e = 0, ne = ev_count(c);
    case_voice_init(&v, c, fs);
    for (i = 0; i < n; i++) {
        while (e < ne && ev_sample(&c->ev[e], fs, n) == i) a1_event(&v, &c->ev[e++]);
        fm_render_i16(&v, pcm + i, 1);
        if (fm_voice_active(&v)) idle = n;
        else if (idle == n) idle = i;
    }
    return idle;
}

/* Advances an engine voice, discarding its output. */
static void a1_advance(fm_voice_t *v, uint32_t frames)
{
    int16_t p[1024];
    while (frames) {
        uint32_t m = frames < 1024u ? frames : 1024u;
        fm_render_i16(v, p, m);
        frames -= m;
    }
}

/* fm_segment_samples */
static uint32_t segment_samples(uint32_t ms, uint32_t fs)
{
    uint64_t n = ((uint64_t)ms * fs + 500u) / 1000u;
    if (n > FM_SEG_MAX) n = FM_SEG_MAX;
    return n ? (uint32_t)n : 1u;
}

/* ----  oracle rule ----
 * After the same note-on on both voices, an attack count computed from a
 * gain outside the attack (a retrigger in decay, sustain or release) can
 * differ by one between the engine and the reference, each flooring its own
 * g0 * n . The reference then adopts the engine's count when it lies
 * in [n - floor(x + tau), n - floor(x - tau)], at least 1, where x is the
 * reference's g0 * n and tau = n * 2^-18, and re-derives its step from its
 * own g0. Any other difference fails, an attack left running included.
 * n is the full attack's length as the note-on rate-scaled it (; the
 * reference's count_a, which render_ref checks against the engine's).
 * stage_before is the reference operator's stage before the note-on. */
enum { COUNT_EQUAL = 0, COUNT_ADOPTED, COUNT_FAIL };

static int adopt_count(const fm_voice_t *v, ref_voice_t *r, int i, int stage_before)
{
    ref_op_t *o = &r->op[i];
    uint32_t ce = v->op[i].count, n;
    double x, tau, lo, hi;
    if (ce == o->count) return COUNT_EQUAL;
    if (stage_before == FM_STAGE_ATTACK || o->stage != FM_STAGE_ATTACK) return COUNT_FAIL;
    n = o->count_a;
    x = o->gain * (double)n;
    tau = ldexp((double)n, -18);
    lo = (double)n - floor(x + tau);
    hi = (double)n - floor(x - tau);
    if (lo < 1.0) lo = 1.0;
    if (hi < 1.0) hi = 1.0;
    if ((double)ce < lo || (double)ce > hi) return COUNT_FAIL;
    o->count = ce;
    o->rate_g = (1.0 - o->gain) / (double)ce;
    return COUNT_ADOPTED;
}

/* 's window for a count the two sides form from their own g0 on the
 * full length n: the engine's ce is allowed in [n - floor(x + tau),
 * n - floor(x - tau)], at least 1, with x = g0 * n and tau = n * 2^-18. */
static int adopt_window(uint32_t ce, double g0, uint32_t n)
{
    const double x = g0 * (double)n, tau = ldexp((double)n, -18);
    double lo = (double)n - floor(x + tau), hi = (double)n - floor(x - tau);
    if (lo < 1.0) lo = 1.0;
    if (hi < 1.0) hi = 1.0;
    return (double)ce >= lo && (double)ce <= hi;
}

/*  and : the amp's attack after a note-on, as adopt_count does it
 * for an operator; n is the amp's unscaled attack and stage_before the
 * reference amp's stage before the note-on. The step is re-derived from
 * the reference's own g0. */
static int adopt_amp_count(const fm_voice_t *v, ref_voice_t *r, uint32_t fs, uint32_t stage_before)
{
    const uint32_t ce = v->amp.count;
    if (ce == r->amp_count) return COUNT_EQUAL;
    if (stage_before == FM_STAGE_ATTACK || r->amp_stage != FM_STAGE_ATTACK) return COUNT_FAIL;
    if (!adopt_window(ce, r->amp_gain, segment_samples(r->patch.amp.attack_ms, fs))) return COUNT_FAIL;
    r->amp_count  = ce;
    r->amp_rate_g = (1.0 - r->amp_gain) / (double)ce;
    return COUNT_ADOPTED;
}

/*  and : a looping operator's restart length, which the engine
 * prepares at the note-on (loop_count) and the reference forms at each
 * restart from its own sustain gain and count_a (ref_loop_samples). Inside
 * the window the reference keeps the engine's length for every restart of
 * this note (loop_adopt), so the loops cannot drift apart. */
static int adopt_loop_count(const fm_voice_t *v, ref_voice_t *r, int i)
{
    const ref_op_t *o = &r->op[i];
    const uint32_t ce = v->op[i].loop_count, cr = ref_loop_samples(r, i);
    if (ce == cr) return COUNT_EQUAL;
    if (!adopt_window(ce, exp2(-(o->decay2_on ? o->sus2 : o->sus)), o->count_a)) return COUNT_FAIL;
    r->op[i].loop_adopt = ce;
    return COUNT_ADOPTED;
}

/* The reference, with an engine voice rendered alongside so that the
 * rule can compare attack counts after every note-on. Prints an ADOPT line
 * per adoption and a COUNT-FAIL line per other difference and returns their
 * numbers. After every note-on each operator's three rate-scaled segment
 * lengths are checked against 's precision-derived rounding window.
 * Each permitted difference prints ADOPT and updates the reference before
 *  checks its shortened attack; every other difference fails. */
static const char *const SEG_NAME[3] = { "attack", "decay", "release" };

/* : over the control ticks, the largest relative difference between
 * an LFO-pitched operator's increments in the engine and the reference,
 * and the largest difference of their phases, in turns. */
typedef struct { double inc_rel, drift; } lfo_stat_t;

/* Matrix-only increment bound in raw Q0.64 units. fm_exp2_q48 has
 * <=0.5 Q48 final rounding error plus a conservative 2^-44 relative
 * allowance. The latter covers three <=0.5-Q48 cent conversions,
 * <=2^-49 relative table rounding, <2^-50 series remainder, Q62
 * arithmetic and host double evaluation. Base truncation contributes
 * less than M raw units; 2*M includes its factor-error cross term.
 * 2^17 covers the 2^16 below-Nyquist clamp, final floor and double
 * comparison rounding. Phase32 adds its exact half-unit 2^31.
 * Use only on active matrix-pitched operators; legacy  is unchanged. */
static double mod_inc_bound(double base,double x,unsigned cap)
{
    const double m=exp2(x);
    if (x==0.0) return ldexp(1.0,FM_PHASE64?17:33);
    if (base>cap) base=cap;
    return ldexp(1.0,17)+ldexp(base,15)+ldexp(base*m,20)+2.0*m
           +(FM_PHASE64?0.0:ldexp(1.0,31));
}
static int mod_inc_accept(double engine,double reference,double bound)
{
    return fabs(engine-reference)<=bound;
}
/* Common location diagnostics; successful summaries retain their original format. */
typedef struct { const char *tag; unsigned checks, bad; } bound_t;
#define BOUND_CHECK(b, c) do { (b)->checks++; if (!(c)) { \
    if ((b)->bad < 8u) printf("%s line=%d\n", (b)->tag, __LINE__); \
    (b)->bad++; } } while (0)

static unsigned mod_precision_bounds(uint32_t fs,unsigned cap)
{
    static const uint32_t ratios[]={7,32,8192,65536,16*65536-1,16*65536,32*65536,128*65536,255*65536,16777150,256*65536};
    fm_patch_t p={0};unsigned n,k,checks=0,bad=0,old_failures=0,outside=0;
    double maximum=0;
    p.op[0].ratio_q16=65536;p.op[1].ratio_q16=131072;
    p.peg.level_cents[0]=4800;p.peg.mask=1;
    p.lfo.rate_millihz=10;p.lfo.pitch_cents=1200;p.lfo.mask_pitch=1;
    p.mod[0]=(fm_mod_slot_patch_t){4800,16,1,0,0,{0,0,0}};
    for(n=0;n<sizeof ratios/sizeof ratios[0];n++) {
        fm_voice_t v;ref_voice_t r;
        p.op[0].ratio_q16=ratios[n];
        memset(&v,0,sizeof v);memset(&r,0,sizeof r);
        fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
        fm_voice_note_on(&v,fs*1000,32767);ref_voice_note_on(&r,fs*1000,32767);
        for(k=0;k<175;k++) {
            const int cents=k<169?-10800+(int)k*128:k==169?-10719:k==170?-1547:k==171?10800:k==172?0:k==173?-10800:10799;
            const int peg=cents<-4800?-4800:cents>4800?4800:cents;
            const int rest=cents-peg,mod=rest<-4800?-4800:rest>4800?4800:rest;
            const double base=(double)ratios[n]/65536.0,x=cents/1200.0,m=exp2(x);
            const double expected=base>=cap?.5:fmin(.5,base*m),bound=mod_inc_bound(base,x,cap);
            double ai,ri,error,oldbound;
            v.peg.cur=r.peg_cur=peg;v.mod_pitch[0]=r.mod_pitch[0]=mod;v.lfo_cents=r.lfo_cents=rest-mod;
            fm_voice_pitch(&v,fs*1000);ref_voice_pitch(&r,fs*1000);
            ai=ldexp((double)v.op[0].inc,FM_PHASE64?0:32);ri=ldexp(r.op[0].inc,64);
            error=fabs(ai-ri);if(error/bound>maximum)maximum=error/bound;
            checks++;if(!mod_inc_accept(ai,ri,bound))bad++;
            checks++;if(fabs(r.op[0].inc-expected)>16*DBL_EPSILON*fmax(expected,.5))bad++;
            oldbound=x?ldexp(2.0+m,FM_PHASE64?16:32)+ldexp(r.op[0].inc,20):ldexp(1.0,FM_PHASE64?17:33);
            old_failures+=error>oldbound;
            checks+=2;
            outside+=!mod_inc_accept(ri-bound-4096.0,ri,bound);
            outside+=!mod_inc_accept(ri+bound+4096.0,ri,bound);
        }
    }
    if(outside!=2u*175u*(sizeof ratios/sizeof ratios[0]))bad++;
    printf("MODPRECISION fs=%u phase64=%d cap=%u checks=%u old_failures=%u outside_refusals=%u maximum=%.9f failures=%u\n",fs,FM_PHASE64,cap,checks,old_failures,outside,maximum,bad);
    return bad;
}

/* The engine's increments into the reference's integer phases  after
 * an event or a control tick : a bit mask of the operators whose
 * increment lies off the reference's by more than the bound. */
static unsigned sync_inc(const fm_voice_t *a, ref_voice_t *v, int nops)
{
    unsigned bad = 0;
    int i;
    for (i = 0; i < nops; i++) {   /* : the shapes read the engine's integer phase; its increment is within 2^-47 turns of the real one */
        double bound = ldexp(1.0, FM_PHASE64 ? 17 : 33);
#if FM_PHASE64
        const uint64_t ai = a->op[i].inc;
#else
        const uint64_t ai = (uint64_t)a->op[i].inc << 32;
#endif
        if (((a->peg_mask | a->mod_pitch_mask) >> i) & 1u) { /* : wider factor, same rule at zero cents */
            const double x = ((((a->peg_mask >> i) & 1u) ? v->peg_cur : 0) + (((a->mod_pitch_mask >> i) & 1u) ? v->mod_pitch[i] : 0) + ((v->patch.lfo.rate_millihz && ((v->patch.lfo.mask_pitch >> i) & 1u))
                                            ? v->lfo_cents : 0)) / 1200.0;
            if ((a->mod_pitch_mask >> i) & 1u) {
                const double base = v->fs > 0.0
                    ? (double)v->freq_millihz * v->patch.op[i].ratio_q16 / (1000.0 * 65536.0 * v->fs)
                    : 256.0;
                bound = mod_inc_bound(base, x, 256u);
            } else if (x != 0.0) bound = ldexp(2.0 + exp2(x), FM_PHASE64 ? 16 : 32) + ldexp(v->op[i].inc, 20);
        }
        if (!mod_inc_accept((double)ai, ldexp(v->op[i].inc, 64), bound)) bad |= 1u << i;
        v->op[i].iinc = ai;
    }
    ref_voice_ks_sync(v);   /* : the delays follow the engine's increments */
    return bad;
}

/*  fixtures compare independent discrete sources, smoothers and offsets
 * before any increment synchronization. No matrix state is adopted. */
static unsigned check_matrix(const char *name, const fm_voice_t *a, const ref_voice_t *r, uint32_t pos)
{
    unsigned i, bad = 0;
    if (!a->mod_active && !r->mod_active) return 0;
    if (a->mod_active != r->mod_active || a->mod_velocity != r->mod_velocity || a->mod_note != r->mod_note
        || a->mod_cutoff != r->mod_cutoff || a->mod_q != r->mod_q
        || a->mod_lfo_rate != r->mod_lfo_rate || a->mod_lfo_pitch != r->mod_lfo_pitch
        || a->mod_lfo_level != r->mod_lfo_level || a->mod_lfo_index != r->mod_lfo_index) ++bad;
    for (i = 0; i < FM_MOD_SLOTS; ++i) if (a->mod_cur[i] != r->mod_cur[i]) ++bad;
    for (i = 0; i < FM_CTL_INPUTS; ++i) if (a->ctl_input[i] != r->ctl_input[i]) ++bad;
    for (i = 0; i < FM_OPS; ++i)
        if (a->mod_pitch[i] != r->mod_pitch[i] || a->mod_level[i] != r->mod_level[i] || a->mod_index[i] != r->mod_index[i] || a->mod_pd[i] != r->mod_pd[i] || a->op[i].pd_knee != r->op[i].pd_knee) ++bad;
    for (i = 0; i < FM_FORMANTS; ++i)
        if (a->formant[i].center_millihz != r->formant[i].center_millihz || a->formant[i].level_db10 != r->formant[i].level_db10
            || a->mod_formant_center[i] != r->mod_formant_center[i] || a->mod_formant_gain[i] != r->mod_formant_gain[i]) ++bad;
    if (a->vector_x != r->vector_x || a->vector_y != r->vector_y) ++bad;
    if (bad) printf("MOD-FAIL %-8s sample=%u discrete_differences=%u\n", name, pos, bad);
    return bad;
}

static unsigned check_glide(const char *name, const fm_voice_t *a, const ref_voice_t *r, uint32_t pos)
{
    if (a->peg.cur == r->peg_cur && a->peg.stage == r->peg_stage
        && a->fenv.cur == r->fe_e && a->fenv.stage == r->fe_stage) return 0;
    printf("GLIDE-FAIL %-8s sample=%u peg=%d/%d stage=%u/%u fenv=%d/%d stage=%u/%u\n", name, pos,
           a->peg.cur, r->peg_cur, (unsigned)a->peg.stage, r->peg_stage,
           a->fenv.cur, r->fe_e, (unsigned)a->fenv.stage, r->fe_stage);
    return 1;
}

/* : check tracking against the independent logarithm before adopting
 * its integer cents into the reference. The gain inversion caps 20 octaves. */
static unsigned check_track(const char *name, const fm_voice_t *a, ref_voice_t *r, uint32_t freq, uint32_t pos)
{
    double oct, expected;
    const double tolerance = 0.5 + ldexp(1.0, 2 * (10 - FM_EXP_BITS) - 11);
    unsigned failed = 0;
    if (!a->fenv.stage) return 0;
    oct = freq ? log2((double)freq / a->patch.fenv.ref_millihz) : -20.0;
    if (oct < -20.0) oct = -20.0;
    if (oct > 20.0) oct = 20.0;
    expected = oct * 1200.0 * a->patch.fenv.track_permille / 1000.0;
    if (fabs(a->fenv_track - expected) > tolerance) {
        printf("TRACK-FAIL %-8s sample=%u engine=%d reference=%.9f tolerance=%.9f\n",
               name, pos, a->fenv_track, expected, tolerance);
        failed = 1;
    }
    r->fe_track = a->fenv_track;
    ref_voice_filter(r, r->patch.filter.cutoff_millihz, r->patch.filter.q_milli);
    return failed;
}

/* Samples pos to pos + frames - 1 of both voices; with an LFO  in
 * pieces that end at the engine's control ticks , after each of
 * which the reference, ticking at the same sample, takes the engine's new
 * increments (an INC-FAIL line per difference past the bound, returned
 * counted) and st gathers the LFO-pitched operators' differences. */
static unsigned render_stretch(const char *name, const fm_patch_t *patch, ref_voice_t *v, fm_voice_t *a, double *out,
                               uint32_t pos, uint32_t frames, int nops, lfo_stat_t *st)
{
    const fm_lfo_patch_t *lp = &patch->lfo;
    unsigned failed = 0, bad;
    int i;
    while (frames) {
        uint32_t m = frames;
        if (a->ctl != 0u && a->ctl < m) m = a->ctl;
        ref_render(v, out + pos, m);
        a1_advance(a, m);
        pos += m;
        frames -= m;
        failed += check_matrix(name, a, v, pos);
        failed += check_glide(name, a, v, pos);
        if (a->ctl == 0u) continue;
        bad = sync_inc(a, v, nops);
        for (i = 0; i < nops; i++) {
            if ((bad >> i) & 1u) {
                printf("INC-FAIL   %-8s sample=%u op%d\n", name, (unsigned)pos, i);
                failed++;
            }
            if ((((lp->rate_millihz ? lp->mask_pitch : 0u) | a->peg_mask | a->mod_pitch_mask) >> i) & 1u
                && !patch->op[i].fixed_millihz && v->op[i].inc > 0.0
                && fm_voice_active(a) && ref_voice_sounding(v)) {
                const double ri = ldexp(v->op[i].inc, 64), rel = fabs((double)v->op[i].iinc - ri) / ri;
                double d = v->op[i].phase - ldexp((double)a->op[i].phase, FM_PHASE64 ? -64 : -32);
                d -= floor(d + 0.5);
                if (rel > st->inc_rel) st->inc_rel = rel;
                if (fabs(d) > st->drift) st->drift = fabs(d);
            }
        }
    }
    return failed;
}

/* The lockstep of a reference voice v and its engine twin a, both
 * initialised with the same patch and binding, through ne events in time
 * order: event e applies at sample at[e] (at most n); its t_ms only labels
 * the diagnostic lines, which name name. A case (render_ref) and a
 * timeline's voice (play_ref, ) share it. */
static void ref_lockstep(const char *name, const fm_patch_t *patch, ref_voice_t *v, fm_voice_t *a, uint32_t fs,
                         const spike_event_t *evs, const uint32_t *at, int ne, double *out, uint32_t n,
                         unsigned *adopted, unsigned *failed, lfo_stat_t *st)
{
    uint32_t pos = 0;
    unsigned bad;
    int e, i, nops, sr[FM_OPS];
    uint32_t asr;
    nops = a->ops ? (int)a->ops : 2;   /* the operators the voice steps  */
    *adopted = *failed = 0;
    /* FREE2 fixed operators already run during an initial idle stretch. */
    bad = sync_inc(a, v, nops);
    for (i = 0; i < nops; i++)
        if ((bad >> i) & 1u) {
            printf("INC-FAIL   %-8s init op%d\n", name, i);
            ++*failed;
        }
    st->inc_rel = st->drift = 0.0;
    for (e = 0; e < ne; e++) {
        const spike_event_t *ev = &evs[e];
        uint32_t t = at[e];
        *failed += render_stretch(name, patch, v, a, out, pos, t - pos, nops, st);
        pos = t;
        for (i = 0; i < nops; i++) sr[i] = v->op[i].stage;
        asr = v->amp_stage;
        ref_event(v, ev);
        a1_event(a, ev);
        if (ev->type == EV_ON) *failed += check_track(name, a, v, ev->freq_millihz, pos);
        *failed += check_matrix(name, a, v, pos);
        *failed += check_glide(name, a, v, pos);
        bad = sync_inc(a, v, nops);
        for (i = 0; i < nops; i++)
            if ((bad >> i) & 1u) {
                printf("INC-FAIL   %-8s t=%ums op%d\n", name, (unsigned)ev->t_ms, i);
                ++*failed;
            }
        if (ev->type != EV_ON) continue;
        for (i = 0; i < nops; i++) {
            const fm_op_patch_t *pp = &patch->op[i];
            const uint32_t ms[3] = { pp->attack_ms, pp->decay_ms, pp->release_ms };
            int sg;
            for (sg = 0; sg < 3; sg++) {
                const uint32_t ns = segment_samples(ms[sg], fs);
                const uint32_t ce = fm_rate_scaled(ns, ev->freq_millihz, pp->rate_scale);
                const uint32_t cf = ref_rate_scaled(ns, ev->freq_millihz, pp->rate_scale);
                double exact, tau;
                const int k = ref_adopt_rate_scaled(v, (unsigned)i, (unsigned)sg, ce, &exact, &tau);
                if (k == REF_RATE_EQUAL) continue;
                printf("%s %-8s t=%ums op%d rate-scaled %s count=%u/%u exact=%.12f tau=%.9g\n",
                       k == REF_RATE_ADOPTED ? "ADOPT     " : "COUNT-FAIL", name,
                       (unsigned)ev->t_ms, i, SEG_NAME[sg], (unsigned)ce, (unsigned)cf, exact, tau);
                if (k == REF_RATE_ADOPTED) ++*adopted; else ++*failed;
            }
        }
        /*  second decay compares exactly; unlike A/D/R, its count
         * has no adoption window. The active/prepared counts stay separate. */
        for (i = 0; i < nops; i++) {
            const fm_eg_extra_t *eg = &patch->eg[i];
            const uint32_t ms = eg->decay2_ms > 60000u ? 60000u : eg->decay2_ms;
            const uint32_t expected = ms ? fm_rate_scaled(segment_samples(ms, fs), ev->freq_millihz, patch->op[i].rate_scale) : 0u;
            if (a->op[i].count_d2 != expected || v->op[i].count_d2 != expected) {
                printf("COUNT-FAIL %-8s t=%ums op%d decay2=%u/%u expected=%u\n", name,
                       (unsigned)ev->t_ms, i, a->op[i].count_d2, v->op[i].count_d2, expected);
                ++*failed;
            }
        }
        for (i = 0; i < nops; i++) {
            uint32_t cr = v->op[i].count;
            int k;
            /* DELAY is exact, with a distinct attack prepared behind it.
             *  already reconciled its full rate-scaled attack above. */
            if (a->op[i].stage == FM_STAGE_DELAY || v->op[i].stage == FM_STAGE_DELAY) {
                if (a->op[i].stage != (unsigned)v->op[i].stage || a->op[i].count != v->op[i].count
                    || a->op[i].count_a_wait != v->op[i].count_a_wait || a->op[i].gain || v->op[i].gain != 0.0) {
                    printf("COUNT-FAIL %-8s t=%ums op%d delay=%u/%u attack_wait=%u/%u\n", name,
                           (unsigned)ev->t_ms, i, a->op[i].count, v->op[i].count,
                           a->op[i].count_a_wait, v->op[i].count_a_wait);
                    ++*failed;
                }
                continue;
            }
            k = adopt_count(a, v, i, sr[i]);
            if (k == COUNT_EQUAL) continue;
            printf("%s %-8s t=%ums op%d count=%u/%u\n", k == COUNT_ADOPTED ? "ADOPT     " : "COUNT-FAIL",
                   name, (unsigned)ev->t_ms, i, (unsigned)a->op[i].count, (unsigned)cr);
            if (k == COUNT_ADOPTED) ++*adopted; else ++*failed;
        }
        for (i = 0; i < nops; i++) {   /* : after  settled count_a */
            const uint32_t cr = v->op[i].loop ? ref_loop_samples(v, i) : 0u;
            int k;
            if (!v->op[i].loop && !a->op[i].loop_count) continue;
            k = v->op[i].loop ? adopt_loop_count(a, v, i) : COUNT_FAIL;
            if (k == COUNT_EQUAL) continue;
            printf("%s %-8s t=%ums op%d loop count=%u/%u\n", k == COUNT_ADOPTED ? "ADOPT     " : "COUNT-FAIL",
                   name, (unsigned)ev->t_ms, i, (unsigned)a->op[i].loop_count, (unsigned)cr);
            if (k == COUNT_ADOPTED) ++*adopted; else ++*failed;
        }
        if (patch->amp.enabled) {   /*  */
            const uint32_t cr = v->amp_count;
            const int k = adopt_amp_count(a, v, fs, asr);
            if (k != COUNT_EQUAL) {
                printf("%s %-8s t=%ums amp count=%u/%u\n", k == COUNT_ADOPTED ? "ADOPT     " : "COUNT-FAIL",
                       name, (unsigned)ev->t_ms, (unsigned)a->amp.count, (unsigned)cr);
                if (k == COUNT_ADOPTED) ++*adopted; else ++*failed;
            }
        }
    }
    *failed += render_stretch(name, patch, v, a, out, pos, n - pos, nops, st);
}

static void render_ref(const spike_case_t *c, uint32_t fs, double *out, uint32_t n,
                       unsigned *adopted, unsigned *failed, lfo_stat_t *st)
{
    ref_voice_t v;
    fm_voice_t a;
    uint32_t at[MAX_EV];
    int e, ne = ev_count(c);
    case_ref_init(&v, c, fs);
    case_voice_init(&a, c, fs);
    for (e = 0; e < ne; e++) at[e] = ev_sample(&c->ev[e], fs, n);
    ref_lockstep(c->name, &c->patch, &v, &a, fs, c->ev, at, ne, out, n, adopted, failed, st);
}

/* : guard band of the end-sample test, in LSB. */
#define END_EPS (1.0 / 1024.0)

/* The gate's absolute SNR floors , on the five patches they were set
 * from : 75 dB at half-scale velocity, 82 dB at full scale. */
static const struct { const char *name; double snr_db; } SNR_FLOOR[] = {
    { "sine", 82.0 }, { "bell", 82.0 }, { "quiet", 75.0 }, { "feedback", 82.0 }, { "highidx", 82.0 },
};

/* The approved knobs : the only build --check runs. check.sh's
 * negative control defines GATE_BUILD 1 at other knobs, where it must fail. */
#ifndef GATE_BUILD
#define GATE_BUILD (FM_SINE_BITS == 12 && FM_EXP_BITS == 10 && FM_PHASE64 == 1 && FM_ENV64 == 1 && FM_FB_AVG == 1 && FM_CTL == 64)
#endif

/* 's named diagnostic subset, with  phase/FREE2,  waves, 's vectorxy and 's wavetable and Karplus-Strong cases.
 * Earlier LFO cases stay in the full gate; their PHASE64=0 precision
 * does not meet . */
static int lfo_check_case(const spike_case_t *c)
{
    return !strcmp(c->name, "vectorxy") || !strcmp(c->name, "lfonyq") || !strcmp(c->name, "lfosawup")
        || !strcmp(c->name, "lfosawdn") || !strcmp(c->name, "lforand") || !strcmp(c->name, "phase")
        || !strcmp(c->name, "freeop") || !strcmp(c->name, "freelfo")
        || !strcmp(c->name, "alternating") || !strcmp(c->name, "camel")
        || !strcmp(c->name, "pink") || !strcmp(c->name, "pinkfree")
        || !strcmp(c->name, "colourlp") || !strcmp(c->name, "colourhp") || !strcmp(c->name, "colourfree")
        || !strcmp(c->name, "square") || !strcmp(c->name, "logsaw")
        || !strcmp(c->name, "wtmorph") || !strcmp(c->name, "wtbands")
        || !strcmp(c->name, "kspluck") || !strcmp(c->name, "ksbend");
}

/* One failed criterion: its GATE line, counted. */
static void gate_fail(unsigned *gate, const char *name, const char *what)
{
    printf("GATE   %-8s FAIL %s\n", name, what);
    ++*gate;
}

/* : one point of fm_exp2_q48 against exp2 in double, in units of
 * Q16.48's last place, and relative on [-1, 1]; a failure past 4 units or,
 * on [-1, 1], at 2^-44 relative or more. */
static void exp2_point(int64_t x, unsigned *points, unsigned *fails, double *max_ulp, double *max_rel)
{
    const double e = ldexp(exp2(ldexp((double)x, -48)), 48), ulp = fabs((double)fm_exp2_q48(x) - e);
    int bad = !(ulp <= 4.0);
    if (ulp > *max_ulp) *max_ulp = ulp;
    if (x >= -((int64_t)1 << 48) && x <= ((int64_t)1 << 48)) {
        if (ulp / e > *max_rel) *max_rel = ulp / e;
        if (!(ulp / e < ldexp(1.0, -44))) bad = 1;
    }
    ++*points;
    if (bad) ++*fails;
}

/* 's unit test of fm_exp2_q48: the whole numbers -16 to 14, the
 * sixteenths of [-2, 2] and a sweep of [-2, 2) in steps of 2^-16 + 7 *
 * 2^-48 (the LFO's pitch takes [-1, 1]). Prints an EXP2 line and returns
 * the number of failed points. */
static unsigned exp2_test(void)
{
    const int64_t one = (int64_t)1 << 48;
    unsigned points = 0, fails = 0;
    double max_ulp = 0.0, max_rel = 0.0;
    int64_t x;
    int k;
    for (k = -16; k <= 14; k++) exp2_point((int64_t)k * one, &points, &fails, &max_ulp, &max_rel);
    for (k = -32; k <= 32; k++) exp2_point((int64_t)k * (one >> 4), &points, &fails, &max_ulp, &max_rel);
    for (x = -2 * one; x < 2 * one; x += ((int64_t)1 << 32) + 7) exp2_point(x, &points, &fails, &max_ulp, &max_rel);
    printf("EXP2   points=%u max_ulp=%.3f max_rel_pm1=%.2e fails=%u\n", points, max_ulp, max_rel, fails);
    return fails;
}

/* : reconstruct the unbounded carrier sum from each operator's last
 * sample. A dry twin exposes the engine's Q2.30 clamp before int16 can
 * hide it; the filtered fixture must still saturate there and never clip
 * at int16. The reference/golden then measures that clamp through the
 * filter, independently of this exact integer boundary check. */
static unsigned check_route_saturation(const spike_case_t *c, uint32_t fs)
{
    fm_patch_t patch = c->patch;
    fm_voice_t dry, wet;
    uint32_t pos, i, n = (uint32_t)((uint64_t)c->len_ms * fs / 1000u);
    unsigned positive = 0, negative = 0, clips = 0, failed = 0;
    int64_t lo = 0, hi = 0;
    int e = 0, ne = ev_count(c);
    patch.filter.mode = 0;
    fm_voice_init(&dry, &patch, fs);
    fm_voice_init(&wet, &c->patch, fs);
    for (pos = 0; pos < n; pos++) {
        int32_t d, w, expected;
        int64_t sum = 0, pcm;
        int sounding;
        while (e < ne && ev_sample(&c->ev[e], fs, n) == pos) {
            a1_event(&dry, &c->ev[e]); a1_event(&wet, &c->ev[e++]);
        }
        sounding = fm_voice_active(&dry);
        fm_render_q30(&dry, &d, 1);
        fm_render_q30(&wet, &w, 1);
        if (sounding)
            for (i = 0; i < dry.ops; i++)
                if ((dry.carriers >> i) & 1u) sum += ((int64_t)dry.op[i].prev * dry.op[i].out) >> 30;
        if (sum < lo) lo = sum;
        if (sum > hi) hi = sum;
        positive += sum > INT32_MAX;
        negative += sum < -INT32_MAX;
        expected = sum > INT32_MAX ? INT32_MAX : sum < -INT32_MAX ? -INT32_MAX : (int32_t)sum;
        failed += d != expected;
        pcm = ((int64_t)w + 16384) >> 15;
        clips += pcm < -32768 || pcm > 32767;
    }
    printf("ROUTE-SAT %-8s min_q30=%lld max_q30=%lld positive=%u negative=%u pcm_clips=%u clamp_fail=%u\n",
           c->name, (long long)lo, (long long)hi, positive, negative, clips, failed);
    return failed || !positive || !negative || (!strcmp(c->name, "routeclp") && clips);
}

/* 's measures of an output pcm against the reference ref, both n
 * samples in int16 units (run_case's RESULT line, play_ref's REF line):
 * the SNR, the ceiling of rounding the reference, their difference (loss),
 * the peak error against the reference clipped , the same over the
 * last quarter, and 's end-sample test. dif, when not NULL, gets the
 * error times 1024. */
typedef struct {
    double sig, err, qerr, peak, tsig, terr, tpeak, snr, ceiling, loss;
    uint32_t peak_i, a1_end, ref_end, end_lo, end_hi, band_n, spurious;
    int end_ok;
} d14_t;

static void d14_measure(const int16_t *pcm, const double *ref, uint32_t n, int16_t *dif, d14_t *m)
{
    uint32_t i, tail0 = n - n / 4;
    memset(m, 0, sizeof *m);
    for (i = 0; i < n; i++) {
        double r = ref[i];
        double d = (double)pcm[i] - r;
        double q = (double)clip16(nearbyint(r)) - r;
        double ad = fabs((double)pcm[i] - (r > 32767.0 ? 32767.0 : r < -32768.0 ? -32768.0 : r));
        m->sig += r * r; m->err += d * d; m->qerr += q * q;
        if (ad > m->peak) { m->peak = ad; m->peak_i = i; }
        if (i >= tail0) { m->tsig += r * r; m->terr += d * d; if (ad > m->tpeak) m->tpeak = ad; }
        if (pcm[i] != 0) m->a1_end = i + 1;
        if (fabs(r) >= 0.5) m->ref_end = i + 1;
        if (fabs(r) >= 0.5 + END_EPS) m->end_lo = i + 1;
        if (fabs(r) >= 0.5 - END_EPS) m->end_hi = i + 1;
        if (dif) dif[i] = clip16(nearbyint(d * 1024.0));
    }
    for (i = m->end_lo; i < n; i++) {
        if (fabs(ref[i]) >= 0.5 - END_EPS) m->band_n++;
        else if (pcm[i] != 0) m->spurious++;
    }
    m->snr = db(m->sig, m->err);
    m->ceiling = db(m->sig, m->qerr);
    m->loss = m->snr > 900 ? 0.0 : m->ceiling - m->snr;
    m->end_ok = m->end_lo <= m->a1_end && m->a1_end <= m->end_hi && m->spurious == 0;
}

/* Renders one case with both voices, prints its RESULT line and, with
 * outdir, writes its WAV files. Returns nonzero on a malformed case, when
 * out of memory or when a WAV file cannot be written. With gate, checks the
 * case against the acceptance criteria, prints a GATE line per failure and
 * adds their number to *gate; without it the metrics never change anything.
 * The end-sample test ( with 's band): a1_end,
 * one past the engine's last non-zero sample, lies in end_range, from one
 * past the last reference sample with abs(r) >= 0.5 + END_EPS to one past
 * the last with abs(r) >= 0.5 - END_EPS; from the range's start on, band_n
 * samples have abs(r) >= 0.5 - END_EPS, and spurious counts the non-zero
 * engine samples among the others. ref_end is the exact test's bound. The
 * peak error is measured against the reference clipped to int16 , the
 * SNR against the reference itself. */
static int run_case(const spike_case_t *c, uint32_t fs, const char *outdir, unsigned *gate)
{
    uint32_t n = (uint32_t)((uint64_t)c->len_ms * fs / 1000u);
    int16_t *pcm  = (int16_t *)alloc_n(n, sizeof *pcm);
    int16_t *pcm1 = (int16_t *)alloc_n(n, sizeof *pcm1);
    int16_t *dif  = (int16_t *)alloc_n(n, sizeof *dif);
    double  *ref  = (double *)alloc_n(n, sizeof *ref);
    uint64_t h, h1;
    double snr, loss, peak;
    uint32_t i, a1_idle;
    unsigned adopted, count_fail;
    lfo_stat_t st;
    d14_t m;
    int e, end_ok, rc = 0;
    if (!pcm || !pcm1 || !dif || !ref) { fprintf(stderr, "oom\n"); return HOST_ERROR; }
    for (e = 1; e < ev_count(c); e++)
        if (c->ev[e].t_ms < c->ev[e - 1].t_ms) { fprintf(stderr, "%s: events out of order\n", c->name); return 1; }

    render_a1_blocks(c, fs, pcm, n);
    h = fm_golden_hash(pcm, n);
    a1_idle = render_a1_samples(c, fs, pcm1, n);
    h1 = fm_golden_hash(pcm1, n);
    render_ref(c, fs, ref, n, &adopted, &count_fail, &st);

    d14_measure(pcm, ref, n, dif, &m);
    snr = m.snr; loss = m.loss; peak = m.peak; end_ok = m.end_ok;
    printf("RESULT %-8s hash=%016llx block_indep=%s snr_db=%.2f ceiling_db=%.2f loss_db=%.2f rms_err_lsb=%.4f peak_err_lsb=%.2f@%u tail_snr_db=%.2f tail_peak_lsb=%.2f a1_end=%u ref_end=%u end_range=%u..%u band_n=%u spurious=%u end_ok=%s a1_idle=%u adopted=%u count_fail=%u\n",
           c->name, (unsigned long long)h, h == h1 ? "yes" : "NO", m.snr, m.ceiling, m.loss,
           sqrt(m.err / n), m.peak, m.peak_i, db(m.tsig, m.terr), m.tpeak, m.a1_end, m.ref_end, m.end_lo, m.end_hi, m.band_n, m.spurious,
           end_ok ? "yes" : "NO", a1_idle, adopted, count_fail);
    if (c->patch.lfo.rate_millihz && c->patch.lfo.pitch_cents && c->patch.lfo.mask_pitch)   /* , after each tick */
        printf("LFOREF %-8s inc_rel_max=%.2e phase_drift_max=%.2e turns\n", c->name, st.inc_rel, st.drift);
    if (c->patch.peg.level_cents[0] || c->patch.peg.level_cents[1] || c->patch.peg.level_cents[2] || c->patch.peg.level_cents[3])
        printf("PEGREF %-8s inc_rel_max=%.2e phase_drift_max=%.2e turns\n", c->name, st.inc_rel, st.drift);

    /* 's criteria as amended by ,  and ; the golden hashes
     * are the committed timelines' (check.sh). The values are compared
     * unrounded, so a printed 0.10 can fail, and a NaN fails. */
    if ((!strcmp(c->name, "routesat") || !strcmp(c->name, "routeclp")) && check_route_saturation(c, fs) && gate)
        gate_fail(gate, c->name, "carrier saturation or unclipped filtered output");
    if (gate) {
        char what[64];
        size_t k;
        if (h != h1) gate_fail(gate, c->name, "block_indep");
        if (!(loss <= 0.10)) { snprintf(what, sizeof what, "loss_db=%.6f", loss); gate_fail(gate, c->name, what); }
        if (!(peak <= 1.0)) { snprintf(what, sizeof what, "peak_err_lsb=%.6f", peak); gate_fail(gate, c->name, what); }
        if (!end_ok) gate_fail(gate, c->name, "end_ok");
        if (count_fail) gate_fail(gate, c->name, "count_fail");
        for (k = 0; k < sizeof SNR_FLOOR / sizeof SNR_FLOOR[0]; k++)
            if (!strcmp(c->name, SNR_FLOOR[k].name) && !(snr >= SNR_FLOOR[k].snr_db)) {
                snprintf(what, sizeof what, "snr_db=%.6f<%.0f", snr, SNR_FLOOR[k].snr_db);
                gate_fail(gate, c->name, what);
            }
    }

    if (outdir) {
        char path[512];
        int16_t *rq = (int16_t *)alloc_n(n, sizeof *rq);
        if (!rq) { free(pcm); free(pcm1); free(dif); free(ref); return HOST_ERROR; }
        for (i = 0; i < n; i++) rq[i] = clip16(nearbyint(ref[i]));
        snprintf(path, sizeof path, "%s/a1_%s.wav", outdir, c->name);       rc = exit_merge(rc, write_wav(path, pcm, n, fs));
        snprintf(path, sizeof path, "%s/ref_%s.wav", outdir, c->name);      rc = exit_merge(rc, write_wav(path, rq, n, fs));
        snprintf(path, sizeof path, "%s/diff60dB_%s.wav", outdir, c->name); rc = exit_merge(rc, write_wav(path, dif, n, fs));
        free(rq);
    }
    free(pcm); free(pcm1); free(dif); free(ref);
    return rc;
}

/* : real half-boundary adoptions and deliberately rejected counts,
 * with exact endpoint/bypass controls. No new record or command mode. */
/*  for the amp and the operator loop (review of ): a loop
 * restarting from -20 dB and an amp retriggered in its -20 dB sustain,
 * both over a 100 ms attack, put g0 * n on an integer (480 at 48 kHz, 441
 * at 44.1 kHz), where the engine's and the reference's floors split. The
 * reference must adopt the engine's amp count and the loop length at both
 * note-ons, so the case runs with no count failure, at least those three
 * adoptions, and the gate's loss (0.10 dB) and peak error (1 LSB). Prints
 * an EGADOPT line and returns the number of failed criteria. */
static const spike_case_t EGADOPT_CASE =
    { "egadopt", { .op = {
        { .ratio_q16=65536, .level_db10=60, .attack_ms=100, .decay_ms=80, .sustain_db10=200, .release_ms=200 },
        { .ratio_q16=65536, .index_q10=1024, .attack_ms=5, .decay_ms=400, .sustain_db10=120, .release_ms=200 } },
        .route = { 2, 0x1, { [1] = { [0] = 256 } }, { { 0 } } },
        .ext = { { .loop=1 } }, .amp = { 100, 50, 200, 300, 1 } }, 1500,
      { { 0, EV_ON, 220000, 32767, 0 }, { 400, EV_ON, 220000, 32767, 0 }, { 1100, EV_OFF, 0, 0, 0 } } };

static unsigned egadopt_bounds(uint32_t fs)
{
    bound_t bounds = { "EGADOPT-FAIL", 0, 0 };
    const spike_case_t *c = &EGADOPT_CASE;
    const uint32_t n = (uint32_t)((uint64_t)c->len_ms * fs / 1000u);
    int16_t *pcm = (int16_t *)alloc_n(n, sizeof *pcm);
    double *ref = (double *)alloc_n(n, sizeof *ref);
    double sig = 0.0, err = 0.0, qerr = 0.0, peak = 0.0, loss;
    unsigned adopted = 0, count_fail = 0;
    lfo_stat_t st;
    uint32_t i;
    if (!pcm || !ref) { free(pcm); free(ref); printf("EGADOPT fs=%u oom\n", (unsigned)fs); return BOUNDS_HOST_ERROR; }
    render_a1_blocks(c, fs, pcm, n);
    render_ref(c, fs, ref, n, &adopted, &count_fail, &st);
    for (i = 0; i < n; i++) {   /* run_case's loss and peak error, the gate's 0.10 dB and 1 LSB */
        const double r = ref[i], d = (double)pcm[i] - r, q = (double)clip16(nearbyint(r)) - r;
        const double ad = fabs((double)pcm[i] - (r > 32767.0 ? 32767.0 : r < -32768.0 ? -32768.0 : r));
        sig += r * r; err += d * d; qerr += q * q;
        if (ad > peak) peak = ad;
    }
    loss = 10.0 * log10(sig / qerr) - 10.0 * log10(sig / err);
    BOUND_CHECK(&bounds, !(count_fail || adopted < 3u || !(loss <= 0.10) || !(peak <= 1.0)));
    printf("EGADOPT fs=%u adopted=%u count_fail=%u loss_db=%.2f peak_err_lsb=%.2f failures=%u\n",
           (unsigned)fs, adopted, count_fail, loss, peak, bounds.bad);
    free(pcm);
    free(ref);
    return bounds.bad;
}

static unsigned rate_bounds(uint32_t fs)
{
    bound_t bounds = { "RATEBOUND-FAIL", 0, 0 };
    /* Keep tests as the historical call/group count, distinct from assertions. */
    static const uint32_t freqs[] = { 0, 1, 27499, 27500, 27501, 54999, 55000, 55001, 7039999, 7040000, 7040001, UINT32_MAX };
    static const uint32_t lengths[] = { 1, 2, 160, 48000, FM_SEG_MAX };
    static const uint16_t scales[] = { 0, 1, 7, 8 };
    const uint32_t pin_fs = fs == 44100u ? 44100u : 48000u;
    fm_patch_t p;
    ref_voice_t r, before;
    uint32_t n, ce, cf;
    unsigned fi, ni, ri, sg, tests = 0, adopted = 0, rejected = 0;
    double x, tau;
    int verdict;
    memset(&r, 0, sizeof r);
    for (fi = 0; fi < sizeof freqs / sizeof freqs[0]; fi++)
        for (ni = 0; ni < sizeof lengths / sizeof lengths[0]; ni++)
            for (ri = 0; ri < sizeof scales / sizeof scales[0]; ri++) {
                const uint32_t rfs = lengths[ni] == FM_SEG_MAX ? UINT32_MAX : 1000u;
                memset(&p, 0, sizeof p);
                p.op[0].attack_ms = (uint16_t)(lengths[ni] == FM_SEG_MAX ? UINT16_MAX : lengths[ni]);
                p.op[0].rate_scale = scales[ri];
                p.op[0].fixed_millihz = 440000u; /* scaling still follows the note */
                ref_voice_init(&r, &p, rfs);
                ref_voice_note_on(&r, freqs[fi], 32767);
                n = segment_samples(p.op[0].attack_ms, rfs);
                ce = fm_rate_scaled(n, freqs[fi], scales[ri]);
                cf = ref_rate_scaled(n, freqs[fi], scales[ri]);
                verdict = ref_adopt_rate_scaled(&r, 0, 0, ce, &x, &tau);
                tests++;
                BOUND_CHECK(&bounds, !(verdict != (ce == cf ? REF_RATE_EQUAL : REF_RATE_ADOPTED)
                    || r.op[0].count_a != ce || r.op[0].count != ce));
                if (verdict == REF_RATE_ADOPTED) {
                    adopted++;
                    printf("ADOPT      rate-bound n=%u freq=%u rs=%u count=%u/%u exact=%.12f tau=%.9g\n",
                           n, freqs[fi], (unsigned)scales[ri], ce, cf, x, tau);
                }
            }
    /* These existing integer inputs straddle a half without retuning any
     * golden. Both rates exercise all three adopted envelope lengths. */
    memset(&p, 0, sizeof p);
    p.route.ops = 2; p.route.carriers = 2;
    p.op[0].attack_ms = p.op[0].decay_ms = p.op[0].release_ms = pin_fs == 44100u ? 569 : 963;
    p.op[0].sustain_db10 = 120; p.op[0].rate_scale = 1;
    ref_voice_init(&r, &p, pin_fs);
    ref_voice_note_on(&r, 29135, 32767);
    n = segment_samples(p.op[0].attack_ms, pin_fs);
    ce = fm_rate_scaled(n, 29135, 1);
    cf = ref_rate_scaled(n, 29135, 1);
    for (sg = 0; sg < 3; sg++) {
        verdict = ref_adopt_rate_scaled(&r, 0, sg, ce, &x, &tau);
        tests++;
        if (verdict != REF_RATE_ADOPTED) { BOUND_CHECK(&bounds, 0); }
        else { BOUND_CHECK(&bounds, 1);
            adopted++;
            printf("ADOPT      rate-bound fs=%u %s count=%u/%u exact=%.12f tau=%.9g\n",
                   pin_fs, SEG_NAME[sg], ce, cf, x, tau);
        }
        BOUND_CHECK(&bounds, !((sg == 0 && (r.op[0].count_a != ce || r.op[0].count != ce || r.op[0].rate_g != 1.0 / ce))
            || (sg == 1 && (r.op[0].count_d != ce || r.op[0].rate_d != r.op[0].sus / ce))
            || (sg == 2 && r.op[0].rate_r != (double)FM_REL_OCT / ce)));
        /* The upper negative count remains below n but lies beyond the
         * precision-derived window: accepting every plausible count fails. */
        for (ri = 0; ri < 2; ri++) {
            const uint32_t bad = ri ? (uint32_t)floor(x + tau + 0.5) + 1u : 0u;
            memcpy(&before, &r, sizeof r);
            verdict = ref_adopt_rate_scaled(&r, 0, sg, bad, &x, &tau);
            tests++;
            if (verdict != REF_RATE_FAIL || memcmp(&before, &r, sizeof r)) { BOUND_CHECK(&bounds, 0); }
            else { BOUND_CHECK(&bounds, 1); rejected++; }
        }
    }
    {   /* Keep a real partially elapsed attack through its note-on. */
        double sample;
        uint32_t count;
        double step;
        ref_render(&r, &sample, 1);
        count = r.op[0].count; step = r.op[0].rate_g;
        ref_voice_note_on(&r, 29135, 32767);
        verdict = ref_adopt_rate_scaled(&r, 0, 0, ce, &x, &tau);
        tests++;
        BOUND_CHECK(&bounds, !(verdict != REF_RATE_ADOPTED || !r.op[0].rate_attack_kept
            || r.op[0].count != count || r.op[0].rate_g != step));
        if (verdict == REF_RATE_ADOPTED) {
            adopted++;
            printf("ADOPT      rate-bound kept-attack count_a=%u/%u exact=%.12f tau=%.9g\n", ce, cf, x, tau);
        }
    }
    /* An attacking noncarrier does not keep its attack once all carriers
     * are idle. Its next note-on must adopt the newly restarted count. */
    r.op[1].stage = FM_STAGE_IDLE;
    ref_voice_note_on(&r, 29135, 32767);
    verdict = ref_adopt_rate_scaled(&r, 0, 0, ce, &x, &tau);
    tests++;
    BOUND_CHECK(&bounds, !(verdict != REF_RATE_ADOPTED || r.op[0].rate_attack_kept
        || r.op[0].count != ce || r.op[0].rate_g != 1.0 / ce));
    if (verdict == REF_RATE_ADOPTED) {
        adopted++;
        printf("ADOPT      rate-bound restarted-attack count_a=%u/%u exact=%.12f tau=%.9g\n", ce, cf, x, tau);
    }
    printf("RATE_BOUNDS fs=%u tests=%u adopted=%u rejected=%u failures=%u\n", fs, tests, adopted, rejected, bounds.bad);
    return bounds.bad;
}

/* /: compare closed-form idle catch-up to a deliberately simple
 * per-sample unsigned phase clock. This oracle never uses the production
 * wide multiply or its elapsed/control-tick quotient. */
typedef struct {
    uint64_t phase, frames;
    uint32_t noise_index, lfo_phase, lfo_wrap;
} free_clock_t;

static uint32_t free_check_word(uint32_t seed, uint32_t index)
{
    uint32_t x = seed + index * UINT32_C(0x9e3779b9);
    x = (x ^ (x >> 16)) * UINT32_C(0x85ebca6b);
    x = (x ^ (x >> 13)) * UINT32_C(0xc2b2ae35);
    return x ^ (x >> 16);
}

/* Express either phase width in whole Q0.64 turns. */
static uint64_t free_check_phase(uint64_t phase)
{
#if FM_PHASE64
    return phase;
#else
    return phase << 32;
#endif
}

static void free_check_render(fm_voice_t *v, free_clock_t *clock, unsigned n)
{
    unsigned j;
    for (j = 0; j < n; j++) {
        int16_t sample;
        if (v->op[0].stage != FM_STAGE_IDLE || v->patch.op[0].free_run == 2u) {
            const uint64_t old = clock->phase;
            clock->phase += free_check_phase(v->op[0].inc);
            clock->noise_index += (uint32_t)((old ^ clock->phase) >> 63);
        }
        clock->frames++;
        if (v->patch.lfo.free_run == 2u && clock->frames % FM_CTL == 0u) {
            const uint32_t old = clock->lfo_phase;
            clock->lfo_phase += v->lfo_step;
            clock->lfo_wrap += clock->lfo_phase < old;
        }
        fm_render_i16(v, &sample, 1);
    }
}

static unsigned free_bounds(uint32_t fs)
{
    bound_t bounds = { "FREEBOUND-FAIL", 0, 0 };
    static const unsigned gaps[] = { 17, 64, 2057 };
    unsigned mode, fixed, wave, lfo, gap;
    for (mode = 0; mode <= 2; mode++)
        for (fixed = 0; fixed <= 1; fixed++)
            for (wave = 0; wave <= 4; wave += 4)
                for (lfo = 5; lfo <= 6; lfo++) {
                    fm_patch_t p;
                    fm_voice_t v;
                    free_clock_t clock;
                    memset(&p, 0, sizeof p);
                    memset(&clock, 0, sizeof clock);
                    p.op[0].ratio_q16 = 65536;
                    p.op[0].fixed_millihz = fixed ? 123456u : 0u;
                    p.op[0].phase_u16 = 54321;
                    p.op[0].free_run = (uint16_t)mode;
                    p.op[0].wave = (uint16_t)wave;
                    p.op[0].attack_ms = 2;
                    p.op[0].release_ms = 2;
                    p.lfo.rate_millihz = 12345;
                    p.lfo.wave = (uint16_t)lfo;
                    p.lfo.phase_u16 = 45678;
                    p.lfo.delay_ms = 2;
                    p.lfo.fade_ms = 8;
                    p.lfo.free_run = 2;
                    fm_voice_init(&v, &p, fs);
                    clock.phase = (uint64_t)p.op[0].phase_u16 << 48;
                    clock.lfo_phase = (uint32_t)p.lfo.phase_u16 << 16;
                    BOUND_CHECK(&bounds, !(!fm_voice_needs_clock(&v) || fm_voice_active(&v)
                        || (fixed && mode == 2u ? v.op[0].inc == 0 : v.op[0].inc != 0)));
                    for (gap = 0; gap < sizeof gaps / sizeof gaps[0]; gap++) {
                        unsigned tail = 0;
                        uint32_t a, b;
                        free_check_render(&v, &clock, gaps[gap]);
                        /* A pitch event must settle the old increment before
                         * changing a relative operator's future increment. */
                        fm_voice_pitch(&v, 220000u + gap * 123u);
                        BOUND_CHECK(&bounds, !(free_check_phase(v.op[0].phase) != clock.phase));
                        free_check_render(&v, &clock, 11);
                        if (mode == 0u) {
                            clock.phase = (uint64_t)p.op[0].phase_u16 << 48;
                            clock.noise_index = 0;
                        }
                        fm_voice_note_on(&v, 440000, 32767);
                        a = free_check_word(UINT32_C(0xdaa66d2c), clock.lfo_wrap);
                        b = free_check_word(UINT32_C(0xdaa66d2c), clock.lfo_wrap + 1u);
                        BOUND_CHECK(&bounds, !(free_check_phase(v.op[0].phase) != clock.phase
                            || v.lfo_phase != clock.lfo_phase || v.lfo_wrap != clock.lfo_wrap
                            || v.ctl != FM_CTL - clock.frames % FM_CTL
                            || v.lfo_time != 0u || v.lfo_a != (int32_t)(a >> 16) - 32768
                            || (lfo == 6u && v.lfo_b != (int32_t)(b >> 16) - 32768)));
                        if (mode == 2u && wave == 4u) {
                            BOUND_CHECK(&bounds, !(v.op[0].noise_index != clock.noise_index
                                || v.op[0].noise != free_check_word(UINT32_C(0x9e3779b9), clock.noise_index)));
                        }
                        free_check_render(&v, &clock, 13);
                        BOUND_CHECK(&bounds, !(v.lfo_time != 13u));
                        fm_voice_note_on(&v, 330000, 32767);
                        BOUND_CHECK(&bounds, !(free_check_phase(v.op[0].phase) != clock.phase
                            || v.lfo_phase != clock.lfo_phase || v.lfo_time != 0u
                            || v.ctl != FM_CTL - clock.frames % FM_CTL));
                        free_check_render(&v, &clock, 100);
                        fm_voice_note_off(&v);
                        while (fm_voice_active(&v) && tail++ < fs) free_check_render(&v, &clock, 1);
                        BOUND_CHECK(&bounds, !(tail >= fs || fm_voice_active(&v)));
                    }
                }
    /* Operator FREE2 alone leaves 's key-synced grid unchanged.
     * Only an enabled FREE2 LFO shares its init-anchored grid with . */
    for (mode = 0; mode < 3; mode++) {
        unsigned peg, fenv;
        for (peg = 0; peg <= 1; peg++)
            for (fenv = 0; fenv <= 1; fenv++) {
                fm_patch_t p;
                fm_voice_t v;
                int16_t audio[17];
                unsigned expected;
                memset(&p, 0, sizeof p);
                p.op[0].free_run = 2;
                p.op[0].ratio_q16 = 65536;
                p.op[0].attack_ms = 20;
                if (mode) { p.lfo.rate_millihz = 1000; p.lfo.free_run = mode == 2 ? 2 : 0; }
                if (peg) { p.peg.level_cents[0] = 1200; p.peg.time_ms[0] = 100; }
                if (fenv) {
                    p.filter.mode = 1; p.filter.cutoff_millihz = 1000000; p.filter.q_milli = 707;
                    p.fenv.depth_cents = 1200; p.fenv.attack_ms = 100;
                }
                fm_voice_init(&v, &p, fs);
                fm_render_i16(&v, audio, 17);
                fm_voice_note_on(&v, 440000, 32767);
                expected = mode == 2 ? FM_CTL - 17 : (mode || peg || fenv) ? FM_CTL : 0;
                BOUND_CHECK(&bounds, !(v.ctl != expected));
                fm_render_i16(&v, audio, 13);
                fm_voice_note_on(&v, 330000, 32767);
                expected = mode == 2 ? FM_CTL - 30 : (mode || peg || fenv) ? FM_CTL : 0;
                BOUND_CHECK(&bounds, !(v.ctl != expected));
            }
    }
    printf("FREEBOUND fs=%u phase64=%d checks=%u failures=%u\n", fs, FM_PHASE64, bounds.checks, bounds.bad);
    if (bounds.bad) printf("GATE free FAIL %u\n", bounds.bad);
    return bounds.bad;
}

/* : count the silent samples independently of the envelope routine.
 * Delay is an active stage, with oscillator and control work continuing. */
static unsigned delay_bounds(uint32_t fs)
{
    bound_t bounds = { "DELAYBOUND-FAIL", 0, 0 };
    static const uint32_t raw_rates[] = { 0, 1, 17, 44100, 48000, UINT32_MAX };
    static const uint16_t durations[] = { 0, 1, 30, 60000, UINT16_MAX };
    unsigned ri, di, wave;
    for (ri = 0; ri < sizeof raw_rates / sizeof raw_rates[0]; ri++)
        for (di = 0; di < sizeof durations / sizeof durations[0]; di++) {
            fm_patch_t p;
            fm_voice_t v;
            ref_voice_t r;
            uint64_t expected;
            const uint32_t d = durations[di] > 60000u ? 60000u : durations[di];
            memset(&p, 0, sizeof p);
            p.eg[0].delay_ms = durations[di];
            fm_voice_init(&v, &p, raw_rates[ri]);
            ref_voice_init(&r, &p, raw_rates[ri]);
            fm_voice_note_on(&v, 27500, 32767);
            ref_voice_note_on(&r, 27500, 32767);
            expected = ((uint64_t)d * raw_rates[ri] + 500u) / 1000u;
            if (!expected) expected = 1;
            if (expected > FM_SEG_MAX) expected = FM_SEG_MAX;
            BOUND_CHECK(&bounds, !(v.patch.eg[0].delay_ms != d || r.patch.eg[0].delay_ms != d
                || v.op[0].stage != (d ? FM_STAGE_DELAY : FM_STAGE_ATTACK)
                || (unsigned)r.op[0].stage != v.op[0].stage
                || (d && (v.op[0].count != expected || r.op[0].count != expected))));
            if (d && expected == 1u) {
                int16_t a; double b;
                fm_render_i16(&v, &a, 1); ref_render(&r, &b, 1);
                BOUND_CHECK(&bounds, !(a || b != 0.0 || v.op[0].stage != FM_STAGE_ATTACK
                    || r.op[0].stage != FM_STAGE_ATTACK || v.op[0].gain || r.op[0].gain != 0.0));
            }
        }
    for (wave = 0; wave <= 4; wave += 4) {
        fm_patch_t p;
        fm_voice_t v;
        ref_voice_t r;
        uint32_t n, attack, j, noise_index, glide_ticks;
        uint64_t phase;
        memset(&p, 0, sizeof p);
        p.op[0].ratio_q16 = 65536; p.op[0].fixed_millihz = 5500000;
        p.op[0].phase_u16 = 16384; p.op[0].free_run = 2; p.op[0].wave = (uint16_t)wave;
        p.op[0].attack_ms = 20; p.op[0].release_ms = 2;
        p.eg[0].delay_ms = 3;
        p.lfo.rate_millihz = 12345; p.lfo.wave = 5; p.lfo.free_run = 2;
        p.peg.level_cents[0] = 1200; p.peg.time_ms[0] = 100;
        p.filter.mode = 1; p.filter.cutoff_millihz = 1000000; p.filter.q_milli = 707;
        p.fenv.depth_cents = 1200; p.fenv.attack_ms = 100;
        fm_voice_init(&v, &p, fs); ref_voice_init(&r, &p, fs);
        fm_voice_note_on(&v, 440000, 32767); ref_voice_note_on(&r, 440000, 32767);
        n = (uint32_t)(((uint64_t)3 * fs + 500u) / 1000u); if (!n) n = 1;
        attack = (uint32_t)(((uint64_t)20 * fs + 500u) / 1000u); if (!attack) attack = 1;
        glide_ticks = (uint32_t)(((uint64_t)100 * fs + 500u * FM_CTL) / (1000u * FM_CTL));
        phase = (uint64_t)p.op[0].phase_u16 << 48; noise_index = 0;
        for (j = 0; j < n; j++) {
            int16_t a; double b;
            uint64_t inc = v.op[0].inc, old = phase;
#if !FM_PHASE64
            inc <<= 32;
#endif
            phase += inc; noise_index += (uint32_t)((old ^ phase) >> 63);
            fm_render_i16(&v, &a, 1); ref_render(&r, &b, 1);
            if (a || b != 0.0 || v.op[0].gain || r.op[0].gain != 0.0
                || v.idle_samples || v.lfo_time != j + 1u
                || v.ctl != FM_CTL - (j + 1u) % FM_CTL
                || v.peg.cur != (int32_t)(((j + 1u) / FM_CTL) * 1200u / glide_ticks)
                || v.fenv.cur != (int32_t)(((j + 1u) / FM_CTL) * 32768u / glide_ticks)
                || r.peg_cur != v.peg.cur || r.fe_e != v.fenv.cur
                || v.op[0].stage != (j + 1u == n ? FM_STAGE_ATTACK : FM_STAGE_DELAY)
                || (unsigned)r.op[0].stage != v.op[0].stage
                || v.op[0].count != (j + 1u == n ? attack : n - j - 1u)
                || r.op[0].count != v.op[0].count
#if FM_PHASE64
                || v.op[0].phase != phase
#else
                || ((uint64_t)v.op[0].phase << 32) != phase
#endif
                || (wave == 4u && v.op[0].noise_index != noise_index)) BOUND_CHECK(&bounds, 0);
            else BOUND_CHECK(&bounds, 1);
        }
        { int16_t a; double b;
          fm_render_i16(&v, &a, 1); ref_render(&r, &b, 1);
          BOUND_CHECK(&bounds, !(!v.op[0].gain || r.op[0].gain <= 0.0 || v.op[0].count != attack - 1u
              || r.op[0].count != v.op[0].count));
        }
        /* A running attack keeps ; delay must not be reinserted. */
        { const uint32_t left = v.op[0].count; const uint64_t gain = v.op[0].gain;
          fm_voice_note_on(&v, 330000, 32767); ref_voice_note_on(&r, 330000, 32767);
          BOUND_CHECK(&bounds, !(v.op[0].stage != FM_STAGE_ATTACK || v.op[0].count != left || v.op[0].gain != gain));
        }
        /* New voice, partial delay, retrigger restarts the whole delay. */
        fm_voice_init(&v, &p, fs); ref_voice_init(&r, &p, fs);
        fm_voice_note_on(&v, 440000, 32767); ref_voice_note_on(&r, 440000, 32767);
        { int16_t a; double b; fm_render_i16(&v, &a, 1); ref_render(&r, &b, 1); }
        fm_voice_note_on(&v, 330000, 32767); ref_voice_note_on(&r, 330000, 32767);
        BOUND_CHECK(&bounds, !(v.op[0].stage != FM_STAGE_DELAY || v.op[0].count != n
            || v.op[0].count_a_wait != attack || v.op[0].gain || r.op[0].gain != 0.0));
        fm_voice_note_off(&v); ref_voice_note_off(&r);
        BOUND_CHECK(&bounds, !(v.op[0].stage != FM_STAGE_IDLE || r.op[0].stage != FM_STAGE_IDLE
            || v.op[0].count || r.op[0].count || v.op[0].count_a_wait || r.op[0].count_a_wait));
    }
    /* The real  half crossing must adopt the waiting attack, leaving
     * the exact, unscaled delay count untouched. */
    {
        const uint32_t rate = fs == 44100u ? 44100u : 48000u;
        fm_patch_t p; fm_voice_t v; ref_voice_t r;
        uint32_t before, adopted; double exact, tau; int verdict;
        memset(&p, 0, sizeof p);
        p.op[0].attack_ms = rate == 44100u ? 569 : 963;
        p.op[0].rate_scale = 1; p.eg[0].delay_ms = 30;
        fm_voice_init(&v, &p, rate); ref_voice_init(&r, &p, rate);
        fm_voice_note_on(&v, 29135, 32767); ref_voice_note_on(&r, 29135, 32767);
        before = r.op[0].count; adopted = v.op[0].count_a_wait;
        verdict = ref_adopt_rate_scaled(&r, 0, 0, adopted, &exact, &tau);
        BOUND_CHECK(&bounds, !(verdict != REF_RATE_ADOPTED || r.op[0].count_a_wait != adopted
            || r.op[0].count != before || before != v.op[0].count || r.op[0].stage != FM_STAGE_DELAY));
        printf("ADOPT      delay-attack fs=%u count=%u exact=%.12f tau=%.9g\n", rate, adopted, exact, tau);
    }
    printf("DELAYBOUND fs=%u phase64=%d checks=%u failures=%u\n", fs, FM_PHASE64, bounds.checks, bounds.bad);
    if (bounds.bad) printf("GATE delay FAIL %u\n", bounds.bad);
    return bounds.bad;
}

/* Independent bit-at-a-time rational conversion: db10 * 2^96 /
 * 16946482203745597. The remainder stays below 2^55 and the result below
 * 2^53. This host oracle does not call the vendor's wide division. */
static uint64_t decay2_oct(unsigned db10)
{
    uint64_t quotient = 0, remainder = 0;
    int bit;
    for (bit = 107; bit >= 0; bit--) {
        remainder = remainder * 2u + (bit >= 96 ? (db10 >> (bit - 96)) & 1u : 0u);
        quotient *= 2u;
        if (remainder >= UINT64_C(16946482203745597)) {
            remainder -= UINT64_C(16946482203745597); quotient++;
        }
    }
    return quotient;
}

static uint64_t decay2_units(uint64_t oct)
{
#if FM_ENV64
    return oct << 8;
#else
    return (oct + (UINT64_C(1) << 23)) >> 24;
#endif
}

static unsigned decay2_bounds(uint32_t fs)
{
    bound_t bounds = { "DECAY2BOUND-FAIL", 0, 0 };
    unsigned equal, instant, scaled;
    for (equal = 0; equal <= 1; equal++)
        for (instant = 0; instant <= 1; instant++)
            for (scaled = 0; scaled <= 1; scaled++) {
                fm_patch_t p; fm_voice_t v; ref_voice_t r;
                uint32_t na, nd, n2, j;
                uint64_t q1, q2, s1, s2, slope;
                int32_t sample; double reference;
                memset(&p, 0, sizeof p);
                p.op[0].phase_u16 = 16384; /* stationary unit sine exposes gain */
                p.op[0].attack_ms = 1; p.op[0].decay_ms = 2;
                p.op[0].sustain_db10 = 120; p.op[0].release_ms = 300;
                p.op[0].rate_scale = scaled ? 4 : 0;
                p.eg[0].decay2_ms = instant ? 0 : 3;
                p.eg[0].sustain2_db10 = equal ? 120 : 480;
                na = (uint32_t)(((uint64_t)fs + 500u) / 1000u);
                nd = (uint32_t)(((uint64_t)2 * fs + 500u) / 1000u);
                n2 = instant ? 0 : (uint32_t)(((uint64_t)3 * fs + 500u) / 1000u);
                if (scaled) { na = (na + 1u) / 2u; nd = (nd + 1u) / 2u; n2 = (n2 + 1u) / 2u; }
                q1 = decay2_oct(120); q2 = decay2_oct(equal ? 120 : 480);
                s1 = decay2_units(q1); s2 = decay2_units(q2);
                slope = n2 ? decay2_units(((q2 - q1) >> 12) * 4096u / n2) : 0;
                fm_voice_init(&v, &p, fs); ref_voice_init(&r, &p, fs);
                fm_voice_note_on(&v, 110000, 32767); ref_voice_note_on(&r, 110000, 32767);
                BOUND_CHECK(&bounds, !(!v.op[0].decay2_on || v.op[0].count_d2 != n2 || r.op[0].count_d2 != n2
                    || v.op[0].sus != s1 || v.op[0].sus2 != s2 || v.op[0].rate_d2 != slope));
                for (j = 0; j < na + nd; j++) { fm_render_q30(&v, &sample, 1); ref_render(&r, &reference, 1); }
                BOUND_CHECK(&bounds, !(v.op[0].stage != (n2 ? FM_STAGE_DECAY2 : FM_STAGE_SUSTAIN)
                    || r.op[0].stage != (int)v.op[0].stage || v.op[0].att != (n2 ? s1 : s2)
                    || v.op[0].count != n2 || r.op[0].count != n2));
                for (j = 1; j <= n2; j++) {
                    uint64_t expected = j == n2 ? s2 : s1 + j * slope;
                    const double oct1 = 120.0 / (200.0 * log10(2.0));
                    const double oct2 = (equal ? 120.0 : 480.0) / (200.0 * log10(2.0));
                    const double roct = j == n2 ? oct2 : oct1 + (oct2 - oct1) * j / n2;
                    const double h = ldexp(1.0, -FM_EXP_BITS), ln2 = log(2.0);
                    const double bound = (ln2*ln2*h*h/8.0 + ldexp(1.0,-31)
                        + ldexp(1.0,-30) + ln2*ldexp(1.0,-24)) * 1073741824.0 + 3.0;
                    double ideal;
                    if (expected > s2) expected = s2;
                    ideal = exp2(-ldexp((double)expected, FM_ENV64 ? -56 : -24)) * 1073741824.0;
                    fm_render_q30(&v, &sample, 1); ref_render(&r, &reference, 1);
                    BOUND_CHECK(&bounds, !(v.op[0].att != expected || v.op[0].count != n2-j
                        || r.op[0].count != n2-j || v.op[0].stage != (j == n2 ? FM_STAGE_SUSTAIN : FM_STAGE_DECAY2)
                        || r.op[0].stage != (int)v.op[0].stage
                        || fabs(r.op[0].att-roct) > 8.0*(j+2u)*0x1p-52*(1.0+fabs(roct))
                        || fabs(sample-ideal) > bound));
                }
                if (n2) {
                    uint64_t held;
                    fm_voice_init(&v, &p, fs); ref_voice_init(&r, &p, fs);
                    fm_voice_note_on(&v, 110000, 32767); ref_voice_note_on(&r, 110000, 32767);
                    for (j = 0; j < na+nd+n2/2u; j++) { fm_render_q30(&v, &sample, 1); ref_render(&r, &reference, 1); }
                    held = v.op[0].att;
                    fm_voice_note_off(&v); ref_voice_note_off(&r);
                    BOUND_CHECK(&bounds, !(v.op[0].stage != FM_STAGE_RELEASE || v.op[0].att != held || r.op[0].stage != FM_STAGE_RELEASE));
                    fm_voice_note_on(&v, 110000, 32767); ref_voice_note_on(&r, 110000, 32767);
                    BOUND_CHECK(&bounds, !(v.op[0].stage != FM_STAGE_ATTACK || !v.op[0].gain || r.op[0].stage != FM_STAGE_ATTACK || r.op[0].gain <= 0.0));
                    /* A direct retrigger inside DECAY2 also attacks at its current gain. */
                    fm_voice_init(&v, &p, fs); fm_voice_note_on(&v, 110000, 32767);
                    for (j = 0; j < na+nd+n2/2u; j++) fm_render_q30(&v, &sample, 1);
                    fm_voice_note_on(&v, 110000, 32767);
                    BOUND_CHECK(&bounds, !(v.op[0].stage != FM_STAGE_ATTACK || !v.op[0].gain));
                }
            }
    {
        static const uint32_t rates[] = { 0, 1, 44100, 48000, UINT32_MAX };
        unsigned i, mode;
        for (i = 0; i < sizeof rates / sizeof rates[0]; i++) for (mode = 0; mode < 3; mode++) {
            fm_patch_t p; fm_voice_t v; ref_voice_t r; uint64_t expected;
            memset(&p, 0, sizeof p); p.op[0].sustain_db10 = 120;
            p.eg[0].decay2_ms = mode == 0 ? 0 : mode == 1 ? 1 : UINT16_MAX;
            p.eg[0].sustain2_db10 = mode == 0 ? 0 : mode == 1 ? 1 : UINT16_MAX;
            fm_voice_init(&v, &p, rates[i]); ref_voice_init(&r, &p, rates[i]);
            fm_voice_note_on(&v, 27500, 32767); ref_voice_note_on(&r, 27500, 32767);
            expected = (((uint64_t)(mode == 2 ? 60000 : 1) * rates[i]) + 500u) / 1000u;
            if (!expected) expected = 1; if (expected > FM_SEG_MAX) expected = FM_SEG_MAX;
            BOUND_CHECK(&bounds, !(v.op[0].decay2_on != (mode != 0) || r.op[0].decay2_on != (mode != 0)
                || (mode && (v.op[0].count_d2 != expected || r.op[0].count_d2 != expected
                    || v.patch.eg[0].sustain2_db10 != (mode == 1 ? 120 : 1200)))));
        }
    }
    printf("DECAY2BOUND fs=%u phase64=%d env64=%d checks=%u failures=%u\n", fs, FM_PHASE64, FM_ENV64, bounds.checks, bounds.bad);
    if (bounds.bad) printf("GATE decay2 FAIL %u\n", bounds.bad);
    return bounds.bad;
}

/*  pink's independent scalar schedule: row j changes where the
 * edge number modulo 2^(j+1) equals 2^j, including no row at zero.
 * Signed conversion/floor use int64 division, separate from engine shifts. */
typedef struct {
    uint32_t seed, word, index, counter;
    int32_t row[8];
    unsigned continuous;
} pink_check_t;

static int32_t pink_check_row(uint32_t word)
{
    int64_t value = word >> 1;
    if (word >> 31) value -= INT64_C(2147483648);
    return (int32_t)(value >= 0 ? value / 16 : -((-value + 15) / 16));
}

static void pink_check_draw(pink_check_t *p)
{
    if (p->continuous) p->word = free_check_word(p->seed, ++p->index);
    else {
        uint32_t x = p->word;
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        p->word = x;
    }
}

static void pink_check_reset(pink_check_t *p, uint32_t seed, unsigned continuous)
{
    unsigned j;
    memset(p, 0, sizeof *p); p->seed = p->word = seed; p->continuous = continuous;
    for (j = 0; j < 8; ++j) { pink_check_draw(p); p->row[j] = pink_check_row(p->word); }
    pink_check_draw(p);
}

static void pink_check_tick(pink_check_t *p)
{
    unsigned j;
    p->counter = (p->counter + 1u) % 256u;
    for (j = 0; j < 8; ++j)
        if (p->counter % (2u << j) == (1u << j)) {
            pink_check_draw(p); p->row[j] = pink_check_row(p->word);
        }
    pink_check_draw(p);
}

/* Every 256 edges draws 511 words. For a long FREE2 jump, discard whole
 * periods then simulate the last 256..511 edges; every old row has then
 * been replaced. This oracle does not use the engine's per-row ages. */
static void pink_check_skip(pink_check_t *p, uint64_t edges)
{
    if (edges >= 512u) {
        uint64_t periods = (edges - 256u) / 256u;
        p->index += (uint32_t)periods * 511u;
        edges -= periods * 256u;
    }
    while (edges--) pink_check_tick(p);
}

static void pink_check_state(const fm_op_t *o, const pink_check_t *p,
                             int sample, bound_t *bounds)
{
    int64_t sum = 0;
    unsigned j;
    for (j = 0; j < 8; ++j) {
        BOUND_CHECK(bounds, !(o->noise_state.pink.row[j] != p->row[j]
            || p->row[j] < -67108864 || p->row[j] > 67108863));
        sum += p->row[j];
    }
    BOUND_CHECK(bounds, !(o->noise_state.pink.sum != sum || sum < -536870912 || sum > 536870904));
    BOUND_CHECK(bounds, !(o->noise_state.pink.counter != p->counter));
    BOUND_CHECK(bounds, !(o->noise != p->word));
    BOUND_CHECK(bounds, !(o->noise_index != p->index));
    if (sample) {
        int64_t value = sum + pink_check_row(p->word);
        BOUND_CHECK(bounds, !(o->prev != value || value < -603979776 || value > 603979767));
    }
}

static unsigned pink_bounds(uint32_t fs)
{
    bound_t bounds = { "PINKBOUND-FAIL", 0, 0 };
    static const uint32_t seeds[4] = { 0x9E3779B9u, 0x7F4A7C15u, 0x3C6EF372u, 0xFE94F82Au };
    static const uint64_t gaps[] = { 0, 1, 255, 256, 257, 2057, UINT32_MAX, UINT64_MAX };
    fm_patch_t p = { 0 };
    unsigned mode, i, j, g;
    p.route.ops = 4; p.route.carriers = 1;
    for (i = 0; i < 4; ++i) {
        p.op[i].ratio_q16 = 65536; p.op[i].wave = 9;
        p.op[i].fixed_millihz = fs * 250u; /* exact quarter-turn/sample at either gate rate */
    }
    for (mode = 0; mode <= 2; mode += 2) {
        fm_voice_t v;
        pink_check_t m[4];
        int32_t out;
        for (i = 0; i < 4; ++i) p.op[i].free_run = (uint16_t)mode;
        fm_voice_init(&v, &p, fs); fm_voice_note_on(&v, 220000, 32767);
        for (i = 0; i < 4; ++i) {
            pink_check_reset(&m[i], seeds[i], mode == 2);
            pink_check_state(&v.op[i], &m[i], 0, &bounds);
            v.op[i].stage = FM_STAGE_SUSTAIN; v.op[i].att = 0;
            v.op[i].fb = 123456789; /* PM/feedback must not move the noise read clock. */
        }
        for (j = 0; j < 131072u; ++j) {
            if (j & 1u) for (i = 0; i < 4; ++i) pink_check_tick(&m[i]);
            fm_render_q30(&v, &out, 1);
            for (i = 0; i < 4; ++i) pink_check_state(&v.op[i], &m[i], 1, &bounds);
        }
    }
    for (mode = 0; mode <= 2; ++mode) {
        fm_voice_t v, before, twin;
        pink_check_t m[4];
        int32_t out, a[67], b[67];
        unsigned phase = 0;
        for (i = 0; i < 4; ++i) p.op[i].free_run = (uint16_t)mode;
        fm_voice_init(&v, &p, fs);
        for (i = 0; i < 4; ++i) pink_check_reset(&m[i], seeds[i], mode == 2);
        for (j = 0; j < 129; ++j) {
            fm_render_q30(&v, &out, 1);
            if (mode == 2 && (++phase % 2u == 0)) for (i = 0; i < 4; ++i) pink_check_tick(&m[i]);
        }
        fm_voice_note_on(&v, 220000, 32767);
        for (i = 0; i < 4; ++i) pink_check_state(&v.op[i], &m[i], 0, &bounds);
        for (j = 0; j < 193; ++j) {
            if (++phase % 2u == 0) for (i = 0; i < 4; ++i) pink_check_tick(&m[i]);
            fm_render_q30(&v, &out, 1);
            for (i = 0; i < 4; ++i) pink_check_state(&v.op[i], &m[i], 0, &bounds);
        }
        before = v; fm_voice_note_on(&v, 330000, 32767);
        for (i = 0; i < 4; ++i) {
            BOUND_CHECK(&bounds, !(memcmp(&before.op[i].noise_state, &v.op[i].noise_state, sizeof(fm_noise_state_t))));
            pink_check_state(&v.op[i], &m[i], 0, &bounds);
        }
        twin = v;
        for (j = 0; j < 67; ++j) {
            if (++phase % 2u == 0) for (i = 0; i < 4; ++i) pink_check_tick(&m[i]);
            fm_render_q30(&v, &a[j], 1);
        }
        fm_render_q30(&twin, b, 67);
        BOUND_CHECK(&bounds, !(memcmp(a, b, sizeof a)));
        for (i = 0; i < 4; ++i) {
            BOUND_CHECK(&bounds, !(memcmp(&v.op[i].noise_state, &twin.op[i].noise_state, sizeof(fm_noise_state_t))));
        }
        fm_voice_note_off(&v);
        for (j = 0; j < 129; ++j) {
            if ((fm_voice_active(&v) || mode == 2) && (++phase % 2u == 0))
                for (i = 0; i < 4; ++i) pink_check_tick(&m[i]);
            fm_render_q30(&v, &out, 1);
        }
        BOUND_CHECK(&bounds, !(fm_voice_active(&v)));
        for (j = 0; j < 513; ++j) {
            if (mode == 2 && (++phase % 2u == 0)) for (i = 0; i < 4; ++i) pink_check_tick(&m[i]);
            fm_render_q30(&v, &out, 1);
        }
        fm_voice_note_on(&v, 440000, 32767);
        for (i = 0; i < 4; ++i) {
            if (mode == 0) pink_check_reset(&m[i], seeds[i], 0);
            pink_check_state(&v.op[i], &m[i], 0, &bounds);
        }
    }
    for (i = 0; i < 4; ++i) p.op[i].free_run = 2;
    for (g = 0; g < sizeof gaps / sizeof gaps[0]; ++g) {
        fm_voice_t v;
        fm_voice_init(&v, &p, fs); v.idle_samples = gaps[g];
        fm_voice_note_on(&v, 220000, 32767);
        for (i = 0; i < 4; ++i) {
            pink_check_t m;
            pink_check_reset(&m, seeds[i], 1); pink_check_skip(&m, gaps[g] / 2u);
            pink_check_state(&v.op[i], &m, 0, &bounds);
            BOUND_CHECK(&bounds, !(free_check_phase(v.op[i].phase) != (gaps[g] & 3u) * (UINT64_C(1) << 62)));
        }
    }
    {   /* Delay remains silent while the complete pink clock advances. */
        fm_voice_t v;
        pink_check_t m[4];
        int32_t out;
        const uint32_t delay = (2u * fs + 500u) / 1000u;
        for (i = 0; i < 4; ++i) { p.op[i].free_run = 0; p.eg[i].delay_ms = 2; }
        fm_voice_init(&v, &p, fs); fm_voice_note_on(&v, 220000, 32767);
        for (i = 0; i < 4; ++i) pink_check_reset(&m[i], seeds[i], 0);
        for (j = 0; j <= delay; ++j) {
            if (j & 1u) for (i = 0; i < 4; ++i) pink_check_tick(&m[i]);
            fm_render_q30(&v, &out, 1);
            for (i = 0; i < 4; ++i) pink_check_state(&v.op[i], &m[i], 0, &bounds);
            BOUND_CHECK(&bounds, !(j < delay && out != 0));
        }
    }
    {   /* Raw API extremes: independently clock the scalar noise model
         * with the voice's already-validated unsigned increment. */
        const uint32_t rates[] = { 0, 1, fs, UINT32_MAX };
        unsigned ri, ni, qi;
        for (ri = 0; ri < 4; ++ri) for (ni = 0; ni < 2; ++ni) for (qi = 0; qi < 2; ++qi) {
            fm_voice_t v;
            pink_check_t m[4];
            uint64_t phase[4];
            int32_t out;
            for (i = 0; i < 4; ++i) {
                p.op[i].ratio_q16 = qi ? UINT32_MAX : 0;
                p.op[i].fixed_millihz = 0; p.eg[i].delay_ms = 0;
            }
            fm_voice_init(&v, &p, rates[ri]); fm_voice_note_on(&v, ni ? UINT32_MAX : 0, 32767);
            for (i = 0; i < 4; ++i) {
                pink_check_reset(&m[i], seeds[i], 0); phase[i] = free_check_phase(v.op[i].phase);
                v.op[i].stage = FM_STAGE_SUSTAIN; v.op[i].att = 0;
            }
            for (j = 0; j < 129; ++j) {
                for (i = 0; i < 4; ++i) {
                    uint64_t next = phase[i] + free_check_phase(v.op[i].inc);
                    if ((next ^ phase[i]) >> 63) pink_check_tick(&m[i]);
                    phase[i] = next;
                }
                fm_render_q30(&v, &out, 1);
                for (i = 0; i < 4; ++i) pink_check_state(&v.op[i], &m[i], 1, &bounds);
            }
        }
    }
    printf("PINKBOUND fs=%u phase64=%d checks=%u failures=%u\n", fs, FM_PHASE64, bounds.checks, bounds.bad);
    return bounds.bad;
}

/*  colour oracle uses signed division with an explicit floor, not
 * the production arithmetic-shift recurrence. */
static int32_t colour_check_white(uint32_t word)
{
    return (int32_t)((int64_t)(word >> 1) - ((word >> 31) ? INT64_C(2147483648) : 0));
}

static int32_t colour_check_floor(int64_t value, uint32_t den)
{
    return (int32_t)(value >= 0 ? value / den : -((-value + den - 1u) / den));
}

static int32_t colour_check_step(int32_t old, int32_t x, unsigned shift)
{
    const uint32_t den = 1u << shift;
    return old + colour_check_floor((int64_t)x - old + den / 2u, den);
}

static unsigned colour_bounds(uint32_t fs)
{
    bound_t bounds = { "COLOURBOUND-FAIL", 0, 0 };
    static const uint32_t seeds[4] = { 0x9E3779B9u, 0x7F4A7C15u, 0x3C6EF372u, 0xFE94F82Au };
    fm_patch_t p = { 0 };
    unsigned shift, wave, mode, i, j;
    p.route.ops = 4; p.route.carriers = 1;
    for (i = 0; i < 4; ++i) { p.op[i].ratio_q16 = 65536; p.op[i].fixed_millihz = fs * 250u; }
    for (shift = 1; shift <= 15; ++shift) for (wave = 10; wave <= 11; ++wave)
        for (mode = 0; mode <= 2; mode += 2) {
            fm_voice_t v;
            pink_check_t m[4];
            int32_t y[4] = { 0 }, out;
            double ideal[4] = { 0 };
            const double alpha = ldexp(1.0, -(int)shift);
            /* Half-unit step rounding accumulates to <=2^(shift-1).
             * Eight double epsilons per full-scale step, amplified by
             * 1/alpha, conservatively bounds this host oracle's rounding. */
            const double bound = (1u << (shift - 1u)) + 8.0 * 0x1p-52 * 1073741824.0 / alpha;
            for (i = 0; i < 4; ++i) {
                p.op[i].wave = (uint16_t)wave; p.op[i].free_run = (uint16_t)mode;
                p.noise[i].shift = (uint16_t)shift;
                memset(&m[i], 0, sizeof m[i]); m[i].seed = seeds[i]; m[i].continuous = mode == 2;
                m[i].word = mode == 2 ? free_check_word(seeds[i], 0) : seeds[i];
            }
            fm_voice_init(&v, &p, fs); fm_voice_note_on(&v, 220000, 32767);
            for (i = 0; i < 4; ++i) { v.op[i].stage = FM_STAGE_SUSTAIN; v.op[i].att = 0; v.op[i].fb = 123456789; }
            for (j = 0; j < 2048; ++j) {
                for (i = 0; i < 4; ++i) {
                    int32_t x;
                    if (j & 1u) pink_check_draw(&m[i]);
                    x = colour_check_white(m[i].word); y[i] = colour_check_step(y[i], x, shift);
                    ideal[i] += ((double)x - ideal[i]) * alpha;
                }
                fm_render_q30(&v, &out, 1);
                for (i = 0; i < 4; ++i) {
                    int32_t want = wave == 10 ? y[i] : colour_check_floor((int64_t)colour_check_white(m[i].word) - y[i], 2);
                    BOUND_CHECK(&bounds, !(v.op[i].noise != m[i].word));
                    BOUND_CHECK(&bounds, !(v.op[i].noise_index != m[i].index));
                    BOUND_CHECK(&bounds, !(v.op[i].noise_state.colour != y[i] || y[i] < -1073741824 || y[i] > 1073741823));
                    BOUND_CHECK(&bounds, !(v.op[i].prev != want || want < -1073741824 || want > 1073741823));
                    BOUND_CHECK(&bounds, !(fabs((double)y[i] - ideal[i]) > bound));
                }
            }
        }
    /* Constant/alternating extreme inputs and valid extreme memory states.
     * Zero increment makes the manually injected white word hold. */
    p.route.ops = p.route.carriers = 1;
    for (shift = 1; shift <= 15; ++shift) for (wave = 10; wave <= 11; ++wave)
        for (mode = 0; mode < 3; ++mode) {
            fm_voice_t v;
            int32_t y = mode == 0 ? -1073741824 : mode == 1 ? 1073741823 : 0, out;
            p.op[0].wave = (uint16_t)wave; p.op[0].free_run = 0; p.noise[0].shift = (uint16_t)shift;
            fm_voice_init(&v, &p, fs); fm_voice_note_on(&v, 0, 32767);
            v.op[0].stage = FM_STAGE_SUSTAIN; v.op[0].att = 0; v.op[0].inc = 0; v.op[0].noise_state.colour = y;
            for (j = 0; j < 129; ++j) {
                const int positive = mode == 0 || (mode == 2 && (j & 1u));
                const int32_t x = positive ? 1073741823 : -1073741824;
                int32_t want;
                v.op[0].noise = positive ? 0x7FFFFFFEu : 0x80000000u;
                y = colour_check_step(y, x, shift);
                want = wave == 10 ? y : colour_check_floor((int64_t)x - y, 2);
                fm_render_q30(&v, &out, 1);
                BOUND_CHECK(&bounds, !(v.op[0].noise_state.colour != y || v.op[0].prev != want));
                BOUND_CHECK(&bounds, !(y < -1073741824 || y > 1073741823 || want < -1073741824 || want > 1073741823));
            }
        }
    /* Initial/sounding/idle boundaries, copy continuation, and the FREE2
     * contract: its white clock advances in idle while colour memory holds. */
    for (wave = 10; wave <= 11; ++wave) for (mode = 0; mode <= 2; ++mode) {
        fm_voice_t v, twin;
        pink_check_t m;
        int32_t y = 0, out, a[67], b[67];
        unsigned phase = 0;
        p.op[0].wave = (uint16_t)wave; p.op[0].free_run = (uint16_t)mode; p.noise[0].shift = 7;
        memset(&m, 0, sizeof m); m.seed = seeds[0]; m.continuous = mode == 2;
        m.word = mode == 2 ? free_check_word(m.seed, 0) : m.seed;
        fm_voice_init(&v, &p, fs);
        for (j = 0; j < 129; ++j) {
            if (mode == 2 && (++phase % 2u == 0)) pink_check_draw(&m);
            fm_render_q30(&v, &out, 1);
            BOUND_CHECK(&bounds, !(v.op[0].noise_state.colour != 0));
        }
        fm_voice_note_on(&v, 220000, 32767);
        BOUND_CHECK(&bounds, !(v.op[0].noise != m.word || v.op[0].noise_index != m.index || v.op[0].noise_state.colour != y));
        for (j = 0; j < 193; ++j) {
            if (j == 97) {
                fm_voice_note_on(&v, 330000, 32767);
                BOUND_CHECK(&bounds, !(v.op[0].noise_state.colour != y || v.op[0].noise != m.word));
            }
            if (++phase % 2u == 0) pink_check_draw(&m);
            y = colour_check_step(y, colour_check_white(m.word), 7);
            fm_render_q30(&v, &out, 1);
            BOUND_CHECK(&bounds, !(v.op[0].noise != m.word || v.op[0].noise_state.colour != y));
        }
        twin = v;
        for (j = 0; j < 67; ++j) {
            if (++phase % 2u == 0) pink_check_draw(&m);
            y = colour_check_step(y, colour_check_white(m.word), 7);
            fm_render_q30(&v, &a[j], 1);
        }
        fm_render_q30(&twin, b, 67);
        BOUND_CHECK(&bounds, !(memcmp(a, b, sizeof a) || v.op[0].noise_state.colour != twin.op[0].noise_state.colour));
        fm_voice_note_off(&v);
        for (j = 0; j < 642; ++j) {
            const int active = fm_voice_active(&v);
            if ((active || mode == 2) && (++phase % 2u == 0)) pink_check_draw(&m);
            if (active) y = colour_check_step(y, colour_check_white(m.word), 7);
            fm_render_q30(&v, &out, 1);
            BOUND_CHECK(&bounds, !(v.op[0].noise_state.colour != y));
        }
        BOUND_CHECK(&bounds, !(fm_voice_active(&v)));
        fm_voice_pitch(&v, 440000); /* flush the already-accounted idle samples */
        if (mode == 2) {
            const uint64_t gap = UINT64_MAX;
            m.index += (uint32_t)((gap >> 1) + ((phase & 1u) && (gap & 1u)));
            m.word = free_check_word(m.seed, m.index);
            v.idle_samples = gap;
        }
        fm_voice_note_on(&v, 440000, 32767);
        if (mode == 0) { y = 0; m.word = m.seed; m.index = 0; }
        BOUND_CHECK(&bounds, !(v.op[0].noise != m.word || v.op[0].noise_index != m.index || v.op[0].noise_state.colour != y));
    }
    {   /* Raw shift defaults/clamps and all API rates, notes and ratios. */
        const uint32_t rates[] = { 0, 1, fs, UINT32_MAX };
        const uint16_t shifts[] = { 0, 1, 15, UINT16_MAX };
        unsigned ri, si, ni, qi;
        for (wave = 10; wave <= 11; ++wave) for (ri = 0; ri < 4; ++ri) for (si = 0; si < 4; ++si)
            for (ni = 0; ni < 2; ++ni) for (qi = 0; qi < 2; ++qi) {
                fm_voice_t v, twin;
                fm_patch_t q;
                int32_t out, other;
                p.op[0].wave = (uint16_t)wave; p.op[0].free_run = 0; p.op[0].fixed_millihz = 0;
                p.op[0].ratio_q16 = qi ? UINT32_MAX : 0; p.noise[0].shift = shifts[si];
                q = p; q.noise[0].shift = shifts[si] == 0 ? 4 : shifts[si] > 15 ? 15 : shifts[si];
                fm_voice_init(&v, &p, rates[ri]); fm_voice_init(&twin, &q, rates[ri]);
                fm_voice_note_on(&v, ni ? UINT32_MAX : 0, 32767); fm_voice_note_on(&twin, ni ? UINT32_MAX : 0, 32767);
                BOUND_CHECK(&bounds, !(v.patch.noise[0].shift != q.noise[0].shift));
                for (j = 0; j < 129; ++j) {
                    fm_render_q30(&v, &out, 1); fm_render_q30(&twin, &other, 1);
                    BOUND_CHECK(&bounds, !(out != other || v.op[0].noise_state.colour != twin.op[0].noise_state.colour));
                }
            }
    }
    printf("COLOURBOUND fs=%u phase64=%d checks=%u failures=%u\n", fs, FM_PHASE64, bounds.checks, bounds.bad);
    return bounds.bad;
}

/*  shapes: exact phase vertices and one-unit neighbours, table-twin
 * branch checks, and an independent sine interpolation error bound. The
 * stationary unit envelope exposes each Q2.30 oscillator sample before
 * int16 conversion. Routed unity feedback injects the chosen signed PM. */
static int32_t wave_bound_read(const fm_voice_t *base, uint32_t phase,
                               unsigned wave, uint32_t pm)
{
    fm_voice_t v = *base;
    int32_t out;
    const uint32_t u = pm >> 8;
    v.patch.op[0].wave = (uint16_t)wave;
#if FM_PHASE64
    v.op[0].phase = (uint64_t)phase << 32;
#else
    v.op[0].phase = phase;
#endif
    v.op[0].fb = u < 0x800000u ? (int32_t)u : (int32_t)u - 0x1000000;
    v.op[0].prev = v.op[0].prev2 = 1073741824;
    fm_render_q30(&v, &out, 1);
    return v.op[0].prev;
}

/* Exercise the real-PM reference through the public routed renderer;
 * only the stationary unit envelope and test input state are injected. */
static double jump_check_ref(const ref_voice_t *base, uint64_t phase,
                             unsigned wave, double pm)
{
    ref_voice_t v = *base;
    double out;
    v.patch.op[0].wave = (uint16_t)wave;
    v.op[0].iphase = phase; v.op[0].iinc = 0; v.op[0].inc = 0;
    v.op[0].fb = pm; v.op[0].prev = v.op[0].prev2 = 1.0;
    ref_render(&v, &out, 1);
    return v.op[0].prev;
}

static unsigned jump_bounds(uint32_t fs)
{
    bound_t bounds = { "JUMPBOUND-FAIL", 0, 0 };
    fm_patch_t p = { 0 };
    fm_voice_t v;
    ref_voice_t r;
    unsigned i, wave;
    uint32_t rng = 0x7186A9D3u;
    const double ln2 = log(2.0), n = ldexp(1.0, FM_EXP_BITS);
    /* Exp-table curvature + Q24 attenuation truncation of four phase
     * bits, then node rounding, interpolation floor and octave floor. */
    const double bound = 1073741824.0 * (ln2 * 15.0 / 268435456.0 + ln2*ln2/(8.0*n*n)) + 2.5;
    p.route.ops = p.route.carriers = 1; p.op[0].ratio_q16 = 65536;
    fm_voice_init(&v, &p, fs); fm_voice_note_on(&v, 0, 32767);
    ref_voice_init(&r, &p, fs); ref_voice_note_on(&r, 0, 32767);
    v.op[0].stage = FM_STAGE_SUSTAIN; v.op[0].att = 0; v.op[0].inc = 0;
    r.op[0].stage = FM_STAGE_SUSTAIN; r.op[0].att = 0;
    for (i = 0; i < 24u + 4096u; ++i) {
        uint32_t phase, pm, target;
        if (i < 24u) { phase = (uint32_t)(i / 3u) * 0x20000000u + (i % 3u) - 1u; pm = 0; }
        else {
            rng = rng * 1664525u + 1013904223u; phase = rng;
            rng = rng * 1664525u + 1013904223u; pm = rng & 0xFFFFFF00u;
        }
        target = phase + pm;
        for (wave = 7; wave <= 8; ++wave) {
            const int32_t got = wave_bound_read(&v, phase, wave, pm);
            const double sign = target >> 31 ? -1.0 : 1.0;
            const double ideal = wave == 7 ? sign : sign * exp2(-16.0*(target & 0x7FFFFFFFu)/4294967296.0);
            const double actual = jump_check_ref(&r, (uint64_t)phase << 32, wave, (double)pm/4294967296.0);
            BOUND_CHECK(&bounds, !(wave == 7 ? got != (int32_t)(sign*1073741824.0) : fabs((double)got - ideal*1073741824.0) > bound));
            BOUND_CHECK(&bounds, !(fabs(actual - ideal) > 32.0*0x1p-52));
            BOUND_CHECK(&bounds, !(got < -1073741824 || got > 1073741824 || (wave == 8 && llabs((long long)got) < 4194304)));
            if (i < 24u && i % 3u == 1u) {
                const int32_t mag = wave == 7 ? 1073741824 : 1073741824 >> (2u*((i/3u)%4u));
                BOUND_CHECK(&bounds, !(got != (target >> 31 ? -mag : mag)));
            }
        }
    }
    /* A fractional carrier phase of 2^-34 plus PM on either side of
     * its complementary half/wrap boundary by 2^-40 distinguishes real
     * PM from a 32-bit-word floor, even on a phase32 harness build. */
    for (wave = 7; wave <= 8; ++wave) for (i = 0; i < 2; ++i) {
        const double edge = (i ? 0.5 : 1.0) - 0x1p-34;
        const double lo = jump_check_ref(&r, UINT64_C(1) << 30, wave, edge - 0x1p-40);
        const double hi = jump_check_ref(&r, UINT64_C(1) << 30, wave, edge + 0x1p-40);
        BOUND_CHECK(&bounds, !((lo > 0) != (i != 0)));
        BOUND_CHECK(&bounds, !((hi > 0) != (i == 0)));
        BOUND_CHECK(&bounds, !(fabs(lo-hi) <= (wave == 7 ? 1.99 : 1.0)));
    }
    printf("JUMPBOUND fs=%u phase64=%d exp_bits=%d checks=%u failures=%u\n", fs, FM_PHASE64, FM_EXP_BITS, bounds.checks, bounds.bad);
    return bounds.bad;
}

/* AM/ring oracle uses signed quotient/remainder, not the engine's shifts. */
static int64_t amp_check_down(int64_t n, int64_t d)
{
    return n / d - (n % d < 0);
}

static int32_t amp_check_oracle(int32_t target, int32_t source, unsigned weight, unsigned kind)
{
    const int64_t one = INT64_C(1073741824);
    int64_t s = source, gain, result;
    if (s > one) s = one;
    if (s < -one) s = -one;
    gain = kind == 1 ? one - amp_check_down(weight * (one - amp_check_down(s + one, 2)), 256)
                     : one + amp_check_down(weight * (s - one), 256);
    result = amp_check_down(target * gain, one);
    return result > one ? (int32_t)one : result < -one ? -(int32_t)one : (int32_t)result;
}

static uint32_t amp_check_draw(uint32_t *word)
{
    *word ^= *word << 13; *word ^= *word >> 17; *word ^= *word << 5;
    return *word;
}

static fm_patch_t amp_check_patch(void)
{
    fm_patch_t p = { 0 };
    unsigned i;
    p.route.ops = FM_OPS; p.route.carriers = 1;
    for (i = 0; i < FM_OPS; ++i) {
        p.op[i].ratio_q16 = 65536; p.op[i].attack_ms = 2;
        p.op[i].decay_ms = 100; p.op[i].release_ms = 120;
    }
    return p;
}

static void amp_check_ready(fm_voice_t *v, ref_voice_t *r, const fm_patch_t *p, uint32_t fs)
{
    memset(v, 0, sizeof *v); memset(r, 0, sizeof *r);
    fm_voice_init(v, p, fs); ref_voice_init(r, p, fs);
    fm_voice_note_on(v, 220000, 32767); ref_voice_note_on(r, 220000, 32767);
}

/* A stationary noise target exposes every signed Q30 value except +ONE;
 * square at phase zero supplies that endpoint. Source 0 is a back edge
 * to target 1, so equal prev/prev2 exposes a chosen source before it runs.
 * This reaches the primitive through the public renderer and also checks
 * its placement after the waveform/envelope, before outgoing history. */
static int32_t amp_check_read(const fm_voice_t *base, int32_t target, int32_t source,
                              unsigned weight, unsigned kind)
{
    fm_voice_t v = *base;
    int32_t out;
    v.op[0].prev = v.op[0].prev2 = source;
    v.patch.op[1].wave = target == 1073741824 ? 7 : 4;
    v.op[1].noise = (uint32_t)((int64_t)target * 2);
    v.patch.route.w[0][1] = (uint16_t)weight;
    v.patch.route.kind[0][1] = (uint8_t)kind;
    fm_render_q30(&v, &out, 1);
    return v.op[1].prev;
}

static unsigned amp_bounds(uint32_t fs)
{
    bound_t bounds = { "AMBOUND-FAIL", 0, 0 };
    static const int32_t sources[] = { INT32_MIN, -1073741825, -1073741824, -1073741823,
                                      -1, 0, 1, 1073741823, 1073741824, 1073741825, INT32_MAX };
    static const int32_t targets[] = { -1073741824, -1073741823, -1, 0, 1, 1073741823, 1073741824 };
    fm_patch_t p = amp_check_patch();
    fm_voice_t v;
    ref_voice_t r;
    uint32_t rng = 1;
    unsigned a, b, w, kind, i, j, n;
    double worst = 0;
    p.route.ops = 2; p.route.carriers = 2;
    p.route.w[0][1] = 1; p.route.kind[0][1] = 1;
    amp_check_ready(&v, &r, &p, fs);
    for (i = 0; i < 2; ++i) {
        v.op[i].inc = v.op[i].phase = 0;
        v.op[i].stage = FM_STAGE_SUSTAIN; v.op[i].att = 0;
    }
    for (a = 0; a < sizeof sources / sizeof sources[0]; ++a)
        for (b = 0; b < sizeof targets / sizeof targets[0]; ++b)
            for (w = 0; w <= 256; ++w) for (kind = 1; kind <= 2; ++kind)
                BOUND_CHECK(&bounds, amp_check_read(&v, targets[b], sources[a], w, kind)
                          == amp_check_oracle(targets[b], sources[a], w, kind));
    for (a = 0; a < 200000; ++a) {
        const uint32_t word = amp_check_draw(&rng);
        const int32_t source = word <= INT32_MAX ? (int32_t)word : -1 - (int32_t)(UINT32_MAX-word);
        const int32_t target = (int32_t)(amp_check_draw(&rng) >> 1) - 1073741824;
        double s = source / 1073741824.0, gain, error;
        int32_t got;
        w = amp_check_draw(&rng) % 257; kind = 1 + (amp_check_draw(&rng) & 1);
        got = amp_check_read(&v, target, source, w, kind);
        BOUND_CHECK(&bounds, got == amp_check_oracle(target, source, w, kind));
        if (s > 1) s = 1;
        if (s < -1) s = -1;
        gain = kind == 1 ? 1 - (double)w/256 * (1-(1+s)/2) : 1 + (double)w/256 * (s-1);
        error = fabs(target * gain - got);
        if (error > worst) worst = error;
        /* AM's unipolar floor contributes <=0.5, gain floor <1,
         * final product floor <1 Q30 unit; ring omits the first floor.
         * 1e-6 covers binary64 evaluation of a <=2^30 product. */
        BOUND_CHECK(&bounds, error <= (kind == 1 ? 2.5 : 2.0) + 1e-6);
    }
    /* Every non-diagonal pair is AM or ring: descending targets must
     * consume higher sources' new output, lower sources' old history,
     * and apply incoming amplitude edges in ascending source order. */
    p = amp_check_patch();
    for (i = 0; i < FM_OPS; ++i) {
        p.op[i].wave = 4;
        for (j = 0; j < FM_OPS; ++j) if (i != j) {
            p.route.kind[j][i] = (uint8_t)(1 + (j & 1));
            p.route.w[j][i] = (uint16_t)(17 + i*31 + j*19);
        }
    }
    amp_check_ready(&v, &r, &p, fs);
    for (i = 0; i < FM_OPS; ++i) {
        v.op[i].inc = 0; v.op[i].stage = FM_STAGE_SUSTAIN; v.op[i].att = 0;
        v.op[i].prev = (int32_t)(i*199019)-99891; v.op[i].prev2 = (int32_t)(i*291119)-82311;
        r.op[i].inc = 0; r.op[i].iinc = 0; r.op[i].stage = FM_STAGE_SUSTAIN; r.op[i].att = 0;
        r.op[i].prev = v.op[i].prev/1073741824.0; r.op[i].prev2 = v.op[i].prev2/1073741824.0;
    }
    for (n = 0; n < 137; ++n) {
        int32_t prev[FM_OPS], prev2[FM_OPS], now[FM_OPS], out;
        double ref;
        for (i = 0; i < FM_OPS; ++i) { prev[i] = v.op[i].prev; prev2[i] = v.op[i].prev2; }
        for (i = FM_OPS; i-- > 0;) {
            const uint32_t word = v.op[i].noise;
            now[i] = (int32_t)amp_check_down(word <= INT32_MAX ? (int64_t)word : (int64_t)word-INT64_C(4294967296), 2);
            for (j = 0; j < FM_OPS; ++j) if (i != j) {
                const int32_t source = j > i ? now[j] :
#if FM_FB_AVG
                    (int32_t)amp_check_down((int64_t)prev[j] + prev2[j], 2);
#else
                    prev[j];
#endif
                now[i] = amp_check_oracle(now[i], source, p.route.w[j][i], p.route.kind[j][i]);
            }
        }
        fm_render_q30(&v, &out, 1); ref_render(&r, &ref, 1);
        for (i = 0; i < FM_OPS; ++i) {
            BOUND_CHECK(&bounds, v.op[i].prev == now[i] && v.op[i].prev2 == prev[i]);
            BOUND_CHECK(&bounds, v.edge[i][0] == 0);
            /* Each edge adds <=2.5 units; contracting gains and source
             * products leave 10 units/operator conservative here. */
            BOUND_CHECK(&bounds, fabs(r.op[i].prev-now[i]/1073741824.0) < (10.0*FM_OPS)/1073741824.0);
        }
    }
    { /* A sine carrier makes erroneous extra PM observable; white-noise
       * targets alone would hide a bad index-weight cache. */
        fm_patch_t q;
        p.op[0].wave = 0; q = p;
        fm_voice_t va, vb;
        ref_voice_t ra, rb;
        int32_t xa, xb;
        for (i = 1; i < FM_OPS; ++i) {
            q.op[i].index_q10 = 65535; q.op[i].level_db10 = 1200;
            q.op[i].ks_break_millihz = 440000; q.op[i].ks_slope_db10 = 1200;
            q.eg[i].left_slope_db10 = 120;
        }
        amp_check_ready(&va, &ra, &p, fs); amp_check_ready(&vb, &rb, &q, fs);
        for (n = 0; n < 2000; ++n) {
            fm_render_q30(&va, &xa, 1); fm_render_q30(&vb, &xb, 1);
            BOUND_CHECK(&bounds, xa == xb);
        }
    }
    p = amp_check_patch();
    for (kind = 0; kind < 5; ++kind) for (w = 0; w < 3; ++w) {
        fm_patch_t q;
        fm_voice_t va, vb;
        ref_voice_t ra, rb;
        int32_t xa, xb;
        double ya, yb;
        p.route.kind[1][0] = (uint8_t)(kind == 4 ? 255 : kind);
        p.route.w[1][0] = (uint16_t)(w == 0 ? 0 : w == 1 ? 257 : 65535);
        q = p; q.route.kind[1][0] = (uint8_t)(kind <= 3 ? kind : 0);
        if (kind == 3 && q.route.w[1][0]) q.route.w[1][0] = 256;           /* : sync is on or off */
        else if (q.route.w[1][0] > (q.route.kind[1][0] ? 256 : 1024))
            q.route.w[1][0] = q.route.kind[1][0] ? 256 : 1024;
        amp_check_ready(&va, &ra, &p, fs); amp_check_ready(&vb, &rb, &q, fs);
        BOUND_CHECK(&bounds, !memcmp(&va, &vb, sizeof va) && !memcmp(&ra, &rb, sizeof ra));
        fm_render_q30(&va, &xa, 1); fm_render_q30(&vb, &xb, 1);
        ref_render(&ra, &ya, 1); ref_render(&rb, &yb, 1);
        BOUND_CHECK(&bounds, xa == xb && ya == yb);
        BOUND_CHECK(&bounds, va.amp_edges == (kind > 0 && kind < 3 && w > 0));
    }
    p = amp_check_patch(); p.route.kind[0][0] = 1; p.route.w[0][0] = 256;
    amp_check_ready(&v, &r, &p, fs); BOUND_CHECK(&bounds, !v.amp_edges && !r.amp_edges);
    p.route.ops = 0; p.route.kind[1][0] = 2; p.route.w[1][0] = 256;
    amp_check_ready(&v, &r, &p, fs); BOUND_CHECK(&bounds, !v.amp_edges && !r.amp_edges);
    printf("AMBOUND fs=%u phase64=%d checks=%u failures=%u max_ideal_q30=%.9f\n",
           fs, FM_PHASE64, bounds.checks, bounds.bad, worst);
    return bounds.bad;
}

/* : hard sync. An independent model keeps each operator's phase in the
 * selected width and tests a wrap as its increment exceeding the room left
 * in the turn, a noise half turn as the low bits plus the increment reaching
 * half a turn, and FREE 2's idle catch-up one sample at a time. It reads
 * the patch's edges itself and takes from the engine only its increments
 * and whether a carrier sounds. The engine's phases, wrap mask and noise
 * clocks, and the reference's integer phases and wrap mask, must equal it
 * at every sample of seeded routes with every edge kind, sync cycles and
 * several sources, starts at the end of a turn, zero and fixed increments,
 * noise and FREE 0 to 2 sources and targets, retriggers, pitch events,
 * modulators idle under a sounding carrier, ring-outs and silence, with
 * each operator once placed one sample before, at and after its wrap. */
#define SYNC_MASK (FM_PHASE64 ? UINT64_MAX : UINT64_C(0xFFFFFFFF))
#define SYNC_SHIFT (FM_PHASE64 ? 0 : 32)
typedef struct {
    uint64_t phase[FM_OPS], inc[FM_OPS], start[FM_OPS], count[FM_OPS];
    uint32_t in[FM_OPS], free[FM_OPS], prev, idle, n;
} sync_model_t;

static uint32_t sync_model_clock(uint64_t phase, uint64_t inc)
{
    const uint64_t half = (SYNC_MASK >> 1) + 1u;
    return (phase & (half - 1u)) + inc >= half;                   /* inc <= half: at most one half turn */
}

/* One sounding sample, operators high to low; returns the noise clocks. */
static uint32_t sync_model_sample(sync_model_t *m)
{
    uint32_t wrap = 0u, clocks = 0u, i = m->n, j;
    while (i-- > 0) {
        int reset = 0;
        for (j = 0; j < m->n; j++)
            if (j != i && ((m->in[i] >> j) & 1u) && (((j > i ? wrap : m->prev) >> j) & 1u)) reset = 1;
        if (reset) m->phase[i] = m->start[i];
        if (m->inc[i] > SYNC_MASK - m->phase[i]) wrap |= 1u << i;
        if (sync_model_clock(m->phase[i], m->inc[i])) { clocks |= 1u << i; m->count[i]++; }
        m->phase[i] = (m->phase[i] + m->inc[i]) & SYNC_MASK;
    }
    m->prev = wrap;
    return clocks;
}

static void sync_model_catchup(sync_model_t *m)
{
    uint32_t i, k;
    for (i = 0; i < m->n; i++)
        if (m->free[i] == 2u)
            for (k = 0; k < m->idle; k++) {
                m->count[i] += sync_model_clock(m->phase[i], m->inc[i]);
                m->phase[i] = (m->phase[i] + m->inc[i]) & SYNC_MASK;
            }
    m->idle = 0u;
}

static int sync_check_sounding(const fm_voice_t *v)
{
    uint32_t i;
    for (i = 0; i < v->ops; i++)
        if (((v->carriers >> i) & 1u) && v->op[i].stage != FM_STAGE_IDLE) return 1;
    return 0;
}

/* An event on both voices and the model; returns 1 when the reference
 * took the engine's increments within 's bound. */
static int sync_check_event(fm_voice_t *a, ref_voice_t *r, sync_model_t *m, unsigned type, uint32_t freq)
{
    const spike_event_t e = { 0, type, freq, 32767, 0 };
    const int silent = !sync_check_sounding(a);
    unsigned off;
    uint32_t i;
    if (type == EV_ON || type == EV_PITCH) sync_model_catchup(m);
    if (type == EV_ON && silent) {
        for (i = 0; i < m->n; i++)
            if (m->free[i] == 0u) { m->phase[i] = m->start[i]; m->count[i] = 0u; }
        m->prev = 0u;
    }
    a1_event(a, &e); ref_event(r, &e);
    off = sync_inc(a, r, (int)m->n);
    for (i = 0; i < m->n; i++) m->inc[i] = a->op[i].inc;
    return !off;
}

static void sync_check_ref_phase(ref_voice_t *r, uint32_t i, uint64_t phase)
{
    const double d = ldexp((double)phase, FM_PHASE64 ? -64 : -32);
    r->op[i].iphase = phase << SYNC_SHIFT;
    r->op[i].phase = d >= 1.0 ? d - 1.0 : d;
}

static void sync_check_place(fm_voice_t *a, ref_voice_t *r, sync_model_t *m, uint32_t i, uint64_t phase)
{
#if FM_PHASE64
    a->op[i].phase = phase;
#else
    a->op[i].phase = (uint32_t)phase;
#endif
    sync_check_ref_phase(r, i, phase);
    m->phase[i] = phase;
}

static int sync_check_ref_sounding(const ref_voice_t *r)
{
    uint32_t i;
    for (i = 0; i < r->ops; i++)
        if (((r->carriers >> i) & 1u) && r->op[i].stage != FM_STAGE_IDLE) return 1;
    return 0;
}

/* The double envelope may end a sample before or after the engine's, as
 * 's end range allows; sync is not under test on such a sample, so the
 * reference then takes the engine's phases, noise, wrap mask and idle count.
 * Returns 1 when it did. */
static unsigned sync_check_end(const fm_voice_t *a, ref_voice_t *r, int a_sounding, int r_sounding)
{
    uint32_t i;
    if (a_sounding == r_sounding) return 0u;
    for (i = 0; i < a->ops; i++) {
        sync_check_ref_phase(r, i, (uint64_t)a->op[i].phase);
        r->op[i].noise = a->op[i].noise; r->op[i].noise_index = a->op[i].noise_index;
    }
    r->sync_wrap_prev = a->sync_wrap_prev;
    r->idle_samples = a->idle_samples;
    return 1u;
}

/* : the asset-binding contract through its public call. A bank of two
 * 32-sample frames and the largest bank (8 frames of 2048), four disjoint
 * Karplus-Strong buffers of 256 samples, each mutation of that valid
 * descriptor against its FM_BIND_ code, and atomic failure: a refused call
 * leaves every byte of the voice and every asset sample as it was. */
#define BIND_WT0 (2u * 32u * FM_WT_BANDS)
#define BIND_WT1 (FM_WT_FRAMES_MAX * FM_WT_LENGTH_MAX * FM_WT_BANDS)
#define BIND_KS  256u
static union { int32_t align; int16_t s[BIND_WT0 + 2u * BIND_KS]; } bind_bank0;
static union { int32_t align; int16_t s[BIND_WT1]; } bind_bank1;
static int32_t bind_pool[2u * FM_OPS * BIND_KS];

static void bind_valid(fm_assets_t *a, fm_wt_view_t *wt, fm_ks_view_t *ks, int32_t *pool)
{
    uint32_t i;
    memset(a, 0, sizeof *a); a->size = sizeof *a;
    wt[0].frames = 2; wt[0].length = 32; wt[0].bands = FM_WT_BANDS; wt[0].capacity = BIND_WT0;
    wt[0].data = bind_bank0.s; wt[0].data_hash = 0x0123456789abcdefull;
    wt[1].frames = FM_WT_FRAMES_MAX; wt[1].length = FM_WT_LENGTH_MAX; wt[1].bands = FM_WT_BANDS;
    wt[1].capacity = BIND_WT1; wt[1].data = bind_bank1.s; wt[1].data_hash = 0xfedcba9876543210ull;
    a->wt_count = 2; a->wt = wt;
    for (i = 0; i < FM_OPS; i++) { ks[i].data = pool + BIND_KS * i; ks[i].capacity = BIND_KS; }
    a->ks_count = FM_OPS; a->ks = ks;
}

/* The voice holds exactly the descriptor's validated views (NULL: none). */
static int bind_holds(const fm_voice_t *v, const fm_assets_t *a)
{
    uint32_t i;
    for (i = 0; i < FM_WT_SLOTS; i++) {
        const int in = a && i < a->wt_count;
        if (v->wt_data[i] != (in ? a->wt[i].data : NULL) || v->wt_frames[i] != (in ? a->wt[i].frames : 0u)
            || v->wt_length[i] != (in ? a->wt[i].length : 0u)) return 0;
    }
    for (i = 0; i < FM_OPS; i++) {
        const int in = a && i < a->ks_count;
        if (v->ks_data[i] != (in ? a->ks[i].data : NULL) || v->ks_capacity[i] != (in ? a->ks[i].capacity : 0u))
            return 0;
    }
    return 1;
}

/* Copies the binding fields of src into dst, so the rest can be compared. */
static void bind_adopt(fm_voice_t *dst, const fm_voice_t *src)
{
    uint32_t i;
    for (i = 0; i < FM_WT_SLOTS; i++) {
        dst->wt_data[i] = src->wt_data[i]; dst->wt_frames[i] = src->wt_frames[i]; dst->wt_length[i] = src->wt_length[i];
    }
    for (i = 0; i < FM_OPS; i++) { dst->ks_data[i] = src->ks_data[i]; dst->ks_capacity[i] = src->ks_capacity[i]; }
}

static uint64_t bind_assets_hash(void)
{
    uint64_t h = 14695981039346656037ull;
    const unsigned char *b[3] = { (const unsigned char *)bind_bank0.s, (const unsigned char *)bind_bank1.s,
                                  (const unsigned char *)bind_pool };
    const size_t n[3] = { sizeof bind_bank0.s, sizeof bind_bank1.s, sizeof bind_pool };
    size_t i, k;
    for (k = 0; k < 3; k++) for (i = 0; i < n[k]; i++) { h ^= b[k][i]; h *= 1099511628211ull; }
    return h;
}

/* Mutation k of the valid descriptor; returns the code it must give. */
static int bind_mutate(unsigned k, fm_assets_t *a, fm_wt_view_t *wt, fm_ks_view_t *ks, const fm_voice_t *v)
{
    const uintptr_t top = UINTPTR_MAX - 15u;          /* 16-byte aligned; any range past 15 bytes wraps */
    uint32_t i;
    switch (k) {
    case 0:  return FM_BIND_OK;
    case 1:  a->size = 0; return FM_BIND_METADATA;
    case 2:  a->size = sizeof *a - 1u; return FM_BIND_METADATA;
    case 3:  a->size = sizeof *a + 1u; return FM_BIND_UNSUPPORTED;
    case 4:  a->size = sizeof *a + 64u; return FM_BIND_UNSUPPORTED;
    case 5:  for (i = 2; i <= FM_WT_SLOTS; i++) wt[i] = wt[0];
             a->wt_count = FM_WT_SLOTS + 1u; return FM_BIND_METADATA;
    case 6:  for (i = 2; i < FM_WT_SLOTS; i++) wt[i] = wt[0];
             a->wt_count = FM_WT_SLOTS; return FM_BIND_OK;
    case 7:  ks[FM_OPS] = ks[0]; a->ks_count = FM_OPS + 1u; return FM_BIND_METADATA;
    case 8:  a->wt = NULL; return FM_BIND_METADATA;
    case 9:  a->ks = NULL; return FM_BIND_METADATA;
    case 10: a->wt_count = 0; a->wt = NULL; return FM_BIND_OK;
    case 11: a->ks_count = 0; a->ks = NULL; return FM_BIND_OK;
    case 12: wt[1].frames = 0; return FM_BIND_METADATA;
    case 13: wt[1].frames = FM_WT_FRAMES_MAX + 1u; return FM_BIND_METADATA;
    case 14: wt[0].frames = 1; return FM_BIND_OK;
    case 15: wt[0].length = FM_WT_LENGTH_MIN / 2u; return FM_BIND_METADATA;
    case 16: wt[0].length = FM_WT_LENGTH_MAX * 2u; return FM_BIND_METADATA;
    case 17: wt[0].length = 48; return FM_BIND_METADATA;
    case 18: wt[0].length = 0; return FM_BIND_METADATA;
    case 19: wt[0].bands = FM_WT_BANDS - 1u; return FM_BIND_METADATA;
    case 20: wt[0].bands = FM_WT_BANDS + 1u; return FM_BIND_METADATA;
    case 21: wt[0].data = NULL; return FM_BIND_METADATA;
    case 22: wt[0].capacity = BIND_WT0 - 1u; return FM_BIND_CAPACITY;
    case 23: wt[1].capacity = BIND_WT1 - 1u; return FM_BIND_CAPACITY;
    case 24: wt[0].capacity = 0; return FM_BIND_CAPACITY;
    case 25: wt[0].data = (const int16_t *)((uintptr_t)bind_bank0.s + 1u); return FM_BIND_OVERLAP;
    case 26: wt[0].data = (const int16_t *)top; return FM_BIND_METADATA;
    case 27: wt[0].data = (const int16_t *)(const void *)v; return FM_BIND_OVERLAP;
    case 28: wt[0].data = (const int16_t *)((uintptr_t)v - 2u * BIND_WT0); return FM_BIND_OK;   /* ends where the voice begins */
    case 29: wt[0].data = (const int16_t *)((uintptr_t)v + sizeof *v); return FM_BIND_OK;       /* begins where it ends */
    case 30: wt[1] = wt[0]; return FM_BIND_OK;                                                  /* immutable banks may coincide */
    case 31: ks[0].data = NULL; return FM_BIND_METADATA;
    case 32: ks[0].capacity = 0; return FM_BIND_METADATA;
    case 33: ks[FM_OPS - 1u].data = NULL; ks[FM_OPS - 1u].capacity = 0; return FM_BIND_OK;    /* an absent buffer */
    case 34: if (FM_OPS < 2) return FM_BIND_OK;
             ks[1].data = ks[0].data; return FM_BIND_OVERLAP;
    case 35: if (FM_OPS < 2) return FM_BIND_OK;
             ks[FM_OPS - 1u].data = ks[FM_OPS - 2u].data + BIND_KS - 1u; return FM_BIND_OVERLAP;
    case 36: ks[0].data = (int32_t *)((uintptr_t)ks[0].data + 2u); return FM_BIND_OVERLAP;
    case 37: ks[0].data = (int32_t *)(void *)(bind_bank0.s + BIND_WT0 - 2u); return FM_BIND_OVERLAP;
    case 38: ks[0].data = (int32_t *)(void *)(bind_bank0.s + BIND_WT0); return FM_BIND_OK;       /* touches the bank's end */
    case 39: ks[0].data = (int32_t *)(void *)bind_bank1.s; ks[0].capacity = 1; return FM_BIND_OVERLAP;
    case 40: ks[0].data = (int32_t *)(void *)v; ks[0].capacity = 1; return FM_BIND_OVERLAP;
    case 41: ks[0].data = (int32_t *)(void *)a; ks[0].capacity = 1; return FM_BIND_OVERLAP;
    case 42: ks[0].data = (int32_t *)(void *)&wt[1]; ks[0].capacity = 1; return FM_BIND_OVERLAP;
    case 43: ks[0].data = (int32_t *)(void *)&ks[FM_OPS - 1u]; ks[0].capacity = 1; return FM_BIND_OVERLAP;
    case 44: ks[0].data = (int32_t *)top; return FM_BIND_METADATA;
    case 45: ks[0].data = (int32_t *)top; ks[0].capacity = 3; return FM_BIND_OK;                /* 12 bytes fit below the top */
    case 46: ks[0].capacity = 1; return FM_BIND_OK;
    case 47: wt[0].capacity = BIND_WT0 + 2u * BIND_KS; return FM_BIND_OK;                     /* more capacity than needed */
    default: return -1;
    }
}

static unsigned bind_bounds(uint32_t fs)
{
    bound_t bounds = { "BINDBOUND-FAIL", 0, 0 };
    fm_wt_view_t wt[FM_WT_SLOTS + 1u], wt2[FM_WT_SLOTS + 1u];
    fm_ks_view_t ks[FM_OPS + 1u], ks2[FM_OPS + 1u];
    fm_assets_t a, b, prior;
    fm_patch_t p, q;
    fm_voice_t v, w, snap;
    int32_t xa[512], xb[512];
    uint64_t h0;
    unsigned k;
    uint32_t i, n, idle_seen;
    int want, rc;
    for (i = 0; i < sizeof bind_bank0.s / sizeof bind_bank0.s[0]; i++) bind_bank0.s[i] = (int16_t)(i * 7919u);
    for (i = 0; i < BIND_WT1; i++) bind_bank1.s[i] = (int16_t)(i * 104729u);
    for (i = 0; i < sizeof bind_pool / sizeof bind_pool[0]; i++) bind_pool[i] = (int32_t)(i * 2654435761u);
    h0 = bind_assets_hash();
    p = amp_check_patch();
    memset(&v, 0, sizeof v); memset(&w, 0, sizeof w);
    fm_voice_init(&v, &p, fs);
    BOUND_CHECK(&bounds, bind_holds(&v, NULL));                                         /* init binds nothing */
    bind_valid(&a, wt, ks, bind_pool);
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, &a) == FM_BIND_OK && bind_holds(&v, &a));
    /* A second voice shares both banks with buffers of its own. */
    bind_valid(&b, wt2, ks2, bind_pool + FM_OPS * BIND_KS);
    fm_voice_init(&w, &p, fs);
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&w, &b) == FM_BIND_OK && bind_holds(&w, &b));
    BOUND_CHECK(&bounds, v.wt_data[0] == w.wt_data[0] && v.wt_data[1] == w.wt_data[1] && v.ks_data[0] != w.ks_data[0]);
    /* Idle rebind, unbind, and init clearing a binding. */
    a.wt_count = 1; a.ks_count = 2;
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, &a) == FM_BIND_OK && bind_holds(&v, &a));
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, NULL) == FM_BIND_OK && bind_holds(&v, NULL));
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, NULL) == FM_BIND_OK && bind_holds(&v, NULL));
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&w, NULL) == FM_BIND_OK && fm_voice_bind_assets(&w, &b) == FM_BIND_OK);
    fm_voice_init(&w, &p, fs);
    BOUND_CHECK(&bounds, bind_holds(&w, NULL));
    /* Each mutation from a prior binding: success holds the new views,
     * failure leaves every byte of the voice. */
    for (k = 0; ; k++) {
        bind_valid(&prior, wt2, ks2, bind_pool + FM_OPS * BIND_KS);
        prior.wt_count = 1; prior.ks_count = 1;
        bind_valid(&a, wt, ks, bind_pool);
        want = bind_mutate(k, &a, wt, ks, &v);
        if (want < 0) break;
        BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, &prior) == FM_BIND_OK);
        snap = v;
        rc = fm_voice_bind_assets(&v, &a);
        BOUND_CHECK(&bounds, rc == want);
        BOUND_CHECK(&bounds, want == FM_BIND_OK ? bind_holds(&v, &a) : !memcmp(&v, &snap, sizeof v));
        if (rc != want && bounds.bad <= 8) printf("BINDBOUND-FAIL mutation=%u rc=%d want=%d\n", k, rc, want);
    }
    BOUND_CHECK(&bounds, k == 48u);
    /* Binding changes nothing else: a patch without asset sources renders
     * the same bits bound and unbound, and the voices differ only in the
     * binding fields. */
    fm_voice_init(&v, &p, fs); fm_voice_init(&w, &p, fs);
    bind_valid(&a, wt, ks, bind_pool);
    snap = v;
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, &a) == FM_BIND_OK);
    bind_adopt(&snap, &v);
    BOUND_CHECK(&bounds, !memcmp(&v, &snap, sizeof v));
    fm_voice_note_on(&v, 220000, 32767); fm_voice_note_on(&w, 220000, 32767);
    for (n = 0; n < 16u; n++) {
        if (n == 10u) { fm_voice_note_off(&v); fm_voice_note_off(&w); }
        fm_render_q30(&v, xa, 512); fm_render_q30(&w, xb, 512);
        BOUND_CHECK(&bounds, !memcmp(xa, xb, sizeof xa));
    }
    snap = w; bind_adopt(&snap, &v);
    BOUND_CHECK(&bounds, !memcmp(&v, &snap, sizeof v));
    /* A sounding voice refuses binding and unbinding. */
    fm_voice_init(&v, &p, fs);
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, &a) == FM_BIND_OK);
    fm_voice_note_on(&v, 220000, 32767);
    snap = v;
    a.wt_count = 1;
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, &a) == FM_BIND_ACTIVE && !memcmp(&v, &snap, sizeof v));
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, NULL) == FM_BIND_ACTIVE && !memcmp(&v, &snap, sizeof v));
    /* Tails refuse too: a resonant final filter, then a formant slot, ring
     * after the carrier is idle; binding succeeds once the ring ends. */
    for (k = 0; k < 2u; k++) {
        q = amp_check_patch();
        q.op[0].release_ms = 1;
        if (k == 0) { q.filter.cutoff_millihz = 400000; q.filter.q_milli = 16000; q.filter.mode = 1; }
        else q.formant[0] = (fm_formant_patch_t){ 400000, 16000, 0, 1 };
        fm_voice_init(&v, &q, fs);
        fm_voice_note_on(&v, 220000, 32767);
        fm_render_q30(&v, xa, 512);
        fm_voice_note_off(&v);
        idle_seen = 0;
        for (n = 0; n < (1u << 20) && fm_voice_active(&v); n++) {
            if (v.op[0].stage == FM_STAGE_IDLE && !idle_seen) {
                idle_seen = 1;
                snap = v;
                BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, &a) == FM_BIND_ACTIVE && !memcmp(&v, &snap, sizeof v));
            }
            fm_render_q30(&v, xa, 1);
        }
        BOUND_CHECK(&bounds, idle_seen && !fm_voice_active(&v));
        BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, &a) == FM_BIND_OK && bind_holds(&v, &a));
    }
    BOUND_CHECK(&bounds, bind_assets_hash() == h0);                                    /* binding writes no sample */
    printf("BINDBOUND fs=%u phase64=%d checks=%u failures=%u\n", fs, FM_PHASE64, bounds.checks, bounds.bad);
    return bounds.bad;
}

/* ---- WTBOUND: 's wavetable operator and fm_wt_additive ----
 * design-hybrid.md sections 2 and 7, at one rate:
 *   generator  each bank against a double reference of ideal sines and
 *              10^(-db10/200) gains under its own bank-wide denominator
 *              max(1, M): at most 1 LSB (gen_err_lsb, the largest seen);
 *              the case banks, 1024 harmonics, 8 frames, harmonics at and
 *              past length/2 left out, full scale exactly +-32767, zero
 *              and quiet banks all zero, equal partials adding;
 *   refusals   each bad argument returns its code and writes nothing;
 *   read       a one-operator voice at unity sustain against a double
 *              spelling of the integer read (ties up), bit for bit, over
 *              positions 0, 1, 4681, 16384, 32767 and 32768 of an 8-frame
 *              random bank at four pitches, with the band, frames and
 *              fraction the position and increment give;
 *   render     the two-operator voice against ref_double.c, at most 1 LSB
 *              (render_err_lsb), position and pitch moving;
 *   bands      the band at fs / 2^(k+1) and 1 mHz either side against the
 *              integer rule on the increment, the fixture pitches 2, 1, 0
 *              and 2, and refreshes on a pitch event and a control tick;
 *   canonical  twins: enabled 255, position 40000, WT with PD, a non-sine
 *              wave, disabled fields; unbound and empty slots are silent;
 *              family 32 clamps the position to 0..32768.
 * assets is FNV-1a64 over the four case banks' content hashes, which
 * depend only on the 12/10 tables: the same in every build of the gate. */
static double wt_ref_gain(unsigned db10) { return pow(10.0, -(double)db10 / 200.0); }

/* The bank in double LSB, under its own denominator max(1, M). */
static void wt_ref_bank(double *out, uint32_t frames, uint32_t length, const fm_wt_harmonic_t *h, uint32_t count)
{
    const uint32_t per = frames * length;
    double m = 1.0;
    uint32_t k, f, n, i;
    for (k = 0; k < FM_WT_BANDS; k++)
        for (f = 0; f < frames; f++)
            for (n = 0; n < length; n++) {
                double s = 0.0;
                for (i = 0; i < count; i++) {
                    const uint32_t num = h[i].number;
                    if (h[i].frame != f || num > (1u << k) || 2u * num >= length) continue;   /* h <= 2^k, h < length/2 */
                    s += wt_ref_gain(h[i].level_db10)
                       * sin(2.0 * 3.14159265358979323846 * ((double)((num * n) % length) / length + h[i].phase_u16 / 65536.0));
                }
                out[k * per + f * length + n] = s;
                if (fabs(s) > m) m = fabs(s);
            }
    for (i = 0; i < FM_WT_BANDS * per; i++) out[i] = out[i] / m * 32767.0;
}

/* Generates a bank and returns the largest |int - reference| in LSB, or
 * 1e9 when the generator refuses it. */
static double wt_gen_err(int16_t *d, double *ref, uint32_t frames, uint32_t length, const fm_wt_harmonic_t *h, uint32_t count)
{
    const uint32_t total = FM_WT_BANDS * frames * length;
    double e = 0.0;
    uint32_t i;
    if (fm_wt_additive(d, total, frames, length, h, count) != FM_BIND_OK) return 1e9;
    wt_ref_bank(ref, frames, length, h, count);
    for (i = 0; i < total; i++) if (fabs(d[i] - ref[i]) > e) e = fabs(d[i] - ref[i]);
    return e;
}

/* The integer read spelled in double, exact below 2^53: floor((num+1)/2)
 * then floor((a(32768-fr) + b fr + 16384) / 32768). */
static int32_t wt_model(const int16_t *a, const int16_t *b, uint32_t fr, uint32_t p, uint32_t length)
{
    const uint32_t bits = (uint32_t)log2((double)length);
    const uint32_t j = p >> (32u - bits), f = (p >> (16u - bits)) & 0xFFFFu, j1 = (j + 1u) % length;
    const double sa = floor(((double)a[j] * (65536 - (double)f) + (double)a[j1] * f + 1.0) / 2.0);
    const double sb = floor(((double)b[j] * (65536 - (double)f) + (double)b[j1] * f + 1.0) / 2.0);
    return (int32_t)floor((sa * (32768.0 - fr) + sb * fr + 16384.0) / 32768.0);
}

/* The band the integer rule gives: the highest k <= 11 with inc below
 * 2^(W-1-k), W the increment's width, else 0. */
static uint32_t wt_rule_band(const fm_op_t *o)
{
    uint32_t k;
    for (k = FM_WT_BANDS - 1u; k; k--) {
#if FM_PHASE64
        if (o->inc < (UINT64_C(1) << (63u - k))) break;
#else
        if (o->inc < (1u << (31u - k))) break;
#endif
    }
    return k;
}

static fm_patch_t wt_bound_patch(void)
{
    fm_patch_t p = { 0 };
    p.op[0] = (fm_op_patch_t){ .ratio_q16 = 65536, .level_db10 = 60, .attack_ms = 5, .decay_ms = 100, .release_ms = 300 };
    p.op[1] = (fm_op_patch_t){ .ratio_q16 = 65536, .attack_ms = 5, .decay_ms = 100, .release_ms = 300 };
    p.wt[0] = (fm_wt_patch_t){ 0, 0, 1 };
    return p;
}

/* Renders n samples of a patch, bound to a when given, note at freq. */
static void wt_render(const fm_patch_t *p, const fm_assets_t *a, uint32_t fs, uint32_t freq, int16_t *out, uint32_t n)
{
    fm_voice_t v;
    fm_voice_init(&v, p, fs);
    if (a) (void)fm_voice_bind_assets(&v, a);
    fm_voice_note_on(&v, freq, 32767);
    fm_render_i16(&v, out, n);
}

static unsigned wt_bounds(uint32_t fs)
{
    bound_t bounds = { "WTBOUND-FAIL", 0, 0 };
    enum { RN = 14400, RF = 8, RL = 64 };
    static int16_t gen[FM_WT_BANDS * 8 * 2048], rnd[FM_WT_BANDS * RF * RL], zero[FM_WT_BANDS * RL];
    static int16_t one[FM_WT_BANDS * 32], two[FM_WT_BANDS * 32], sentinel[FM_WT_BANDS * 2 * 64];
    static fm_wt_harmonic_t many[1025];
    static int16_t xa[RN], xb[RN];
    static double ref[FM_WT_BANDS * 8 * 2048], rd[RN];
    fm_wt_harmonic_t h[130];
    fm_wt_view_t view[2];
    fm_assets_t a = { sizeof(fm_assets_t), 1, view, 0, NULL };
    fm_patch_t p, q;
    fm_voice_t v, w;
    ref_voice_t r;
    double e, gen_err = 0.0, render_err = 0.0;
    uint64_t assets = UINT64_C(14695981039346656037);
    uint32_t i, k, n, rng = 0x2545F491u;
    /* generator: the case banks, then the extremes */
    for (i = 0; i < NCASE_WT; i++) {
        const struct case_wt *cw = &CASE_WT[i];
        uint64_t hh;
        e = wt_gen_err(gen, ref, cw->frames, cw->length, cw->h, cw->count);
        BOUND_CHECK(&bounds, e <= 1.0); if (e > gen_err) gen_err = e;
        hh = wt_content_hash(cw->frames, cw->length, gen);
        for (k = 0; k < 8u; k++) { assets ^= (hh >> (8u * k)) & 0xFFu; assets *= UINT64_C(1099511628211); }
    }
    for (i = 0; i < 1024u; i++) many[i] = (fm_wt_harmonic_t){ 0, (uint16_t)(i + 1u), 0, 16384 };
    e = wt_gen_err(gen, ref, 1, 2048, many, 1024);                       /* every number, cosine phase */
    BOUND_CHECK(&bounds, e <= 1.0); if (e > gen_err) gen_err = e;
    BOUND_CHECK(&bounds, gen[11u * 2048u] == 32767);                                  /* band 11's peak is the bank's */
    for (n = 0, k = 0; k < 8u; k++)                                       /* 8 frames of 15 harmonics, and 16 and 1024 */
        for (i = 1; i <= 15u; i++) h[n++] = (fm_wt_harmonic_t){ (uint16_t)k, (uint16_t)i, (uint16_t)((k * 37u + i * 53u) % 1201u),
                                                                (uint16_t)((k * 4099u + i * 977u) & 65535u) };
    h[n++] = (fm_wt_harmonic_t){ 0, 16, 0, 0 };
    h[n++] = (fm_wt_harmonic_t){ 0, 1024, 0, 0 };
    e = wt_gen_err(gen, ref, 8, 32, h, n);
    BOUND_CHECK(&bounds, e <= 1.0); if (e > gen_err) gen_err = e;
    h[0] = (fm_wt_harmonic_t){ 0, 1, 0, 16384 };                         /* full scale */
    BOUND_CHECK(&bounds, fm_wt_additive(one, sizeof one / sizeof one[0], 1, 32, h, 1) == FM_BIND_OK);
    for (k = 0, n = 1; k < FM_WT_BANDS; k++) n &= one[k * 32u] == 32767 && one[k * 32u + 16u] == -32767;
    BOUND_CHECK(&bounds, n);
    h[1] = h[0];                                                          /* equal partials add, the denominator follows */
    BOUND_CHECK(&bounds, fm_wt_additive(two, sizeof two / sizeof two[0], 1, 32, h, 2) == FM_BIND_OK && !memcmp(one, two, sizeof one));
    BOUND_CHECK(&bounds, fm_wt_additive(zero, sizeof zero / sizeof zero[0], 1, RL, NULL, 0) == FM_BIND_OK);
    for (i = 0, n = 1; i < sizeof zero / sizeof zero[0]; i++) n &= zero[i] == 0;
    BOUND_CHECK(&bounds, n);
    h[0] = (fm_wt_harmonic_t){ 0, 1, 1200, 0 };                          /* quiet: never normalized up */
    BOUND_CHECK(&bounds, fm_wt_additive(gen, FM_WT_BANDS * RL, 1, RL, h, 1) == FM_BIND_OK);
    for (i = 0, n = 1; i < FM_WT_BANDS * RL; i++) n &= gen[i] == 0;
    BOUND_CHECK(&bounds, n);
    /* refusals: the code, and nothing written */
    {
        static const struct { uint32_t frames, length, count, cap; int nul, bad, want; } R[] = {
            { 0, 64, 1, 1536, 0, 0, FM_BIND_METADATA }, { 9, 64, 1, 1536, 0, 0, FM_BIND_METADATA },
            { 1, 16, 1, 1536, 0, 0, FM_BIND_METADATA }, { 1, 4096, 1, 1536, 0, 0, FM_BIND_METADATA },
            { 1, 48, 1, 1536, 0, 0, FM_BIND_METADATA }, { 2, 64, 1, 1536, 1, 0, FM_BIND_METADATA },
            { 2, 64, 1, 1536, 2, 0, FM_BIND_METADATA }, { 2, 64, 1, 1536, 0, 1, FM_BIND_METADATA },
            { 2, 64, 1, 1536, 0, 2, FM_BIND_METADATA }, { 2, 64, 1, 1536, 0, 3, FM_BIND_METADATA },
            { 2, 64, 1, 1536, 0, 4, FM_BIND_METADATA }, { 1, 64, 1025, 1536, 0, 0, FM_BIND_METADATA },
            { 2, 64, 1, 1535, 0, 0, FM_BIND_CAPACITY }, { 2, 64, 1, 1536, 0, 0, FM_BIND_OK },
            { 2, 64, 0, 1536, 2, 0, FM_BIND_OK },
        };
        for (k = 0; k < sizeof R / sizeof R[0]; k++) {
            fm_wt_harmonic_t one_h = { 1, 3, 0, 0 };
            int rc;
            for (i = 0; i < 1025u; i++) many[i] = (fm_wt_harmonic_t){ 0, 1, 0, 0 };
            if (R[k].bad == 1) one_h.number = 0;
            if (R[k].bad == 2) one_h.number = 1025;
            if (R[k].bad == 3) one_h.level_db10 = 1201;
            if (R[k].bad == 4) one_h.frame = 2;
            for (i = 0; i < sizeof sentinel / sizeof sentinel[0]; i++) sentinel[i] = 0x5A5A;
            rc = fm_wt_additive(R[k].nul == 1 ? NULL : sentinel, R[k].cap, R[k].frames, R[k].length,
                                R[k].nul == 2 ? NULL : R[k].count == 1025u ? many : &one_h, R[k].count);
            for (i = 0, n = 1; i < sizeof sentinel / sizeof sentinel[0]; i++) n &= sentinel[i] == 0x5A5A;
            BOUND_CHECK(&bounds, rc == R[k].want && (rc == FM_BIND_OK || n));
        }
    }
    /* read: bit for bit at unity sustain on one operator */
    for (i = 0; i < sizeof rnd / sizeof rnd[0]; i++) { rng = rng * 1664525u + 1013904223u; rnd[i] = (int16_t)(rng >> 16); }
    rnd[0] = -32768; rnd[1] = 32767; rnd[RL - 1u] = -32768;               /* the extremes and the wrap pair */
    view[0] = (fm_wt_view_t){ RF, RL, FM_WT_BANDS, FM_WT_BANDS * RF * RL, rnd, wt_content_hash(RF, RL, rnd) };
    {
        static const uint16_t pos[6] = { 0, 1, 4681, 16384, 32767, 32768 };
        static const uint32_t hz[4] = { 30000, 440000, 3000000, 15000000 };
        uint32_t pi, fi;
        for (pi = 0; pi < 6u; pi++)
            for (fi = 0; fi < 4u; fi++) {
                const uint32_t z = (uint32_t)pos[pi] * (RF - 1u), fr = z & 32767u;
                const int16_t *wa, *wb;
                uint32_t band, miss = 0;
                p = (fm_patch_t){ 0 };
                p.op[0] = (fm_op_patch_t){ .ratio_q16 = 65536, .attack_ms = 1, .decay_ms = 1, .release_ms = 300 };
                p.route = (fm_route_t){ .ops = 1, .carriers = 1 };
                p.wt[0] = (fm_wt_patch_t){ pos[pi], 0, 1 };
                fm_voice_init(&v, &p, fs);
                BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, &a) == FM_BIND_OK);
                fm_voice_note_on(&v, hz[fi], 32767);
                band = wt_rule_band(&v.op[0]);
                wa = rnd + (band * RF + (z >> 15)) * RL;
                wb = fr ? wa + RL : wa;
                BOUND_CHECK(&bounds, v.op[0].wt_band == band && v.op[0].wt_a == wa && v.op[0].wt_b == wb && v.op[0].wt_frac == fr
                         && v.op[0].wt_pos == pos[pi]);
                for (n = 0; n < 400u; n++) { int32_t y; fm_render_q30(&v, &y, 1); }
                for (n = 0; n < 2000u; n++) {
                    int32_t y;
                    uint32_t ph;
                    fm_render_q30(&v, &y, 1);
#if FM_PHASE64
                    ph = (uint32_t)(v.op[0].phase >> 32);
#else
                    ph = v.op[0].phase;
#endif
                    if (v.op[0].stage != FM_STAGE_SUSTAIN || v.op[0].prev != wt_model(wa, wb, fr, ph, RL)) miss++;
                }
                BOUND_CHECK(&bounds, miss == 0u);
            }
    }
    /* render: against the reference, position and pitch moving; at -12 dB
     * the 16-bit fraction's truncation on full-scale steps stays below 1/4 LSB */
    p = wt_bound_patch();
    p.op[0].level_db10 = 120;
    p.mod[0] = (fm_mod_slot_patch_t){ 32768, 16, 32, 0, 0, { 0, 0, 0 } };
    fm_voice_init(&v, &p, fs); ref_voice_init(&r, &p, fs);
    BOUND_CHECK(&bounds, fm_voice_bind_assets(&v, &a) == FM_BIND_OK);
    ref_voice_bind_assets(&r, &a);
    fm_voice_note_on(&v, 220000, 32767); ref_voice_note_on(&r, 220000, 32767);
    BOUND_CHECK(&bounds, sync_inc(&v, &r, 1) == 0u);
    for (k = 0; k < 12u; k++) {
        const uint32_t m = RN / 12u;
        if (k < 9u) { fm_voice_ctl(&v, 0, (int32_t)(k * 4096u)); ref_voice_ctl(&r, 0, (int32_t)(k * 4096u)); }
        else {
            const uint32_t f = k == 9u ? 3000000u : k == 10u ? 7000000u : 1500000u;
            fm_voice_pitch(&v, f); ref_voice_pitch(&r, f);
            BOUND_CHECK(&bounds, sync_inc(&v, &r, 1) == 0u);
        }
        fm_render_i16(&v, xa + k * m, m); ref_render(&r, rd + k * m, m);
    }
    for (i = 0, e = 0.0; i < RN / 12u * 12u; i++) if (fabs(xa[i] - rd[i]) > e) e = fabs(xa[i] - rd[i]);
    BOUND_CHECK(&bounds, e <= 1.0); render_err = e;
    /* bands: boundaries, fixtures, refreshes */
    p = wt_bound_patch();
    for (k = 0; k < FM_WT_BANDS; k++)
        for (i = 0; i < 3u; i++) {
            const uint32_t f = (uint32_t)((uint64_t)fs * 1000u >> (k + 1u)) + i - 1u;
            fm_voice_init(&v, &p, fs);
            (void)fm_voice_bind_assets(&v, &a);
            fm_voice_note_on(&v, f, 32767);
            BOUND_CHECK(&bounds, v.op[0].wt_band == wt_rule_band(&v.op[0]));
        }
    {
        static const uint32_t fx[4] = { 5500000, 6000000, 12000000, 5000000 }, fb[4] = { 2, 1, 0, 2 };
        for (k = 0; k < 4u; k++) {
            fm_voice_init(&v, &p, fs);
            (void)fm_voice_bind_assets(&v, &a);
            fm_voice_note_on(&v, fx[k], 32767);
            BOUND_CHECK(&bounds, v.op[0].wt_band == fb[k]);
        }
        fm_voice_pitch(&v, 12000000);                                    /* a pitch event refreshes */
        BOUND_CHECK(&bounds, v.op[0].wt_band == 0u && v.op[0].wt_a == rnd);
        q = p;
        q.mod[0] = (fm_mod_slot_patch_t){ 1200, 16, 1, 0, 0, { 0, 0, 0 } };   /* ctl0 to pitch0, an octave */
        fm_voice_init(&v, &q, fs);
        (void)fm_voice_bind_assets(&v, &a);
        fm_voice_note_on(&v, 5000000, 32767);
        BOUND_CHECK(&bounds, v.op[0].wt_band == 2u);
        fm_voice_ctl(&v, 0, 32768);
        fm_render_i16(&v, xa, 2u * FM_CTL + 1u);                         /* a control tick refreshes */
        BOUND_CHECK(&bounds, v.op[0].wt_band == 1u && v.op[0].wt_band == wt_rule_band(&v.op[0]));
    }
    /* canonical twins and silence */
    n = fs / 4u;
    p = wt_bound_patch();
    wt_render(&p, &a, fs, 220000, xa, n);
    q = p; q.wt[0].enabled = 255;
    wt_render(&q, &a, fs, 220000, xb, n); BOUND_CHECK(&bounds, !memcmp(xa, xb, n * sizeof *xa));
    p.wt[0].position_q15 = 32768; wt_render(&p, &a, fs, 220000, xa, n);
    q = p; q.wt[0].position_q15 = 40000;
    wt_render(&q, &a, fs, 220000, xb, n); BOUND_CHECK(&bounds, !memcmp(xa, xb, n * sizeof *xa));
    p = wt_bound_patch(); p.wt[0] = (fm_wt_patch_t){ 0, 0, 0 };            /* the plain sine */
    wt_render(&p, NULL, fs, 220000, xa, n);
    q = wt_bound_patch(); q.pd[0] = (fm_pd_patch_t){ 8192, 1 };         /* WT with PD: neither */
    wt_render(&q, &a, fs, 220000, xb, n); BOUND_CHECK(&bounds, !memcmp(xa, xb, n * sizeof *xa));
    fm_voice_init(&v, &q, fs);
    BOUND_CHECK(&bounds, !v.patch.wt[0].enabled && !v.patch.pd[0].enabled && !v.op[0].wt_on);
    q = wt_bound_patch(); q.wt[0] = (fm_wt_patch_t){ 1234, 3, 0 };      /* disabled fields */
    fm_voice_init(&v, &q, fs);
    BOUND_CHECK(&bounds, !v.patch.wt[0].position_q15 && !v.patch.wt[0].bank && !v.patch.wt[0].enabled);
    wt_render(&q, &a, fs, 220000, xb, n); BOUND_CHECK(&bounds, !memcmp(xa, xb, n * sizeof *xa));
    q = wt_bound_patch(); q.wt[0].position_q15 = 32768;                  /* the table plays, not the sine */
    wt_render(&q, &a, fs, 220000, xb, n); BOUND_CHECK(&bounds, memcmp(xa, xb, n * sizeof *xa) != 0);
    p = wt_bound_patch(); p.op[0].wave = 7;
    wt_render(&p, NULL, fs, 220000, xa, n);
    q = p; q.wt[0] = (fm_wt_patch_t){ 16384, 0, 1 };
    wt_render(&q, &a, fs, 220000, xb, n); BOUND_CHECK(&bounds, !memcmp(xa, xb, n * sizeof *xa));
    fm_voice_init(&v, &q, fs);                                            /* and the form drops it */
    BOUND_CHECK(&bounds, !v.patch.wt[0].enabled && !v.patch.wt[0].position_q15 && !v.op[0].wt_on);
    view[1] = (fm_wt_view_t){ 1, RL, FM_WT_BANDS, FM_WT_BANDS * RL, zero, wt_content_hash(1, RL, zero) };
    for (k = 0; k < 4u; k++) {                                            /* empty slot, slot past 8, unbound, zero bank */
        fm_assets_t b = a;
        q = wt_bound_patch();
        if (k == 0) q.wt[0].bank = 1;
        if (k == 1) q.wt[0].bank = 200;
        if (k == 3) { q.wt[0].bank = 1; b.wt_count = 2; }
        wt_render(&q, k == 2 ? NULL : &b, fs, 220000, xb, n);
        for (i = 0, e = 0.0; i < n; i++) e += xb[i] != 0;
        BOUND_CHECK(&bounds, e == 0.0);
    }
    q = wt_bound_patch(); q.wt[0].position_q15 = 30000;                  /* family 32 clamps */
    q.mod[0] = (fm_mod_slot_patch_t){ 32768, 16, 32, 0, 0, { 0, 0, 0 } };
    fm_voice_init(&v, &q, fs); (void)fm_voice_bind_assets(&v, &a);
    fm_voice_ctl(&v, 0, 32768); fm_voice_note_on(&v, 220000, 32767); fm_render_i16(&v, xa, 2u * FM_CTL + 1u);
    BOUND_CHECK(&bounds, v.mod_wt[0] == 32768 && v.op[0].wt_pos == 32768u);
    q.wt[0].position_q15 = 1000;
    fm_voice_init(&w, &q, fs); (void)fm_voice_bind_assets(&w, &a);
    fm_voice_ctl(&w, 0, -32768); fm_voice_note_on(&w, 220000, 32767); fm_render_i16(&w, xa, 2u * FM_CTL + 1u);
    BOUND_CHECK(&bounds, w.mod_wt[0] == -32768 && w.op[0].wt_pos == 0u);
    printf("WTBOUND fs=%u phase64=%d checks=%u failures=%u assets=%016llx gen_err_lsb=%.3f render_err_lsb=%.3f\n",
           fs, FM_PHASE64, bounds.checks, bounds.bad, (unsigned long long)assets, gen_err, render_err);
    return bounds.bad;
}

/* : Karplus-Strong bounds (design-hybrid.md section 5). An independent
 * model of the fill and the loop, with its own floor spellings, runs beside
 * the engine sample for sample; the tuning is swept against the
 * reference's long division and the exact delay 2^64 / I - 0.5; the
 * lifecycle (note-off, silence, retrigger, pitch), the binding and the
 * canonical twins are pinned. The number of checks does not depend on fs. */
typedef struct {
    int32_t  buf[FM_KS_BUFFER_MAX];
    uint32_t len, w, n, f, loss, word;
    int32_t  prev;
} ks_model_t;

#define KS_GUARD 32u
static int32_t ks_store[FM_OPS][FM_KS_BUFFER_MAX + 2u * KS_GUARD];

static int64_t ks_floor_div(int64_t a, int64_t b)            /* b > 0 */
{
    return a >= 0 ? a / b : -((-a + b - 1) / b);
}

static uint32_t ksm_draw(uint32_t x)
{
    x ^= (uint32_t)(x << 13);
    x ^= x >> 17;
    x ^= (uint32_t)(x << 5);
    return x;
}

static int32_t ksm_read(const ks_model_t *m)
{
    const uint32_t a = (m->w + m->len - m->n) % m->len, b = (a + m->len - 1u) % m->len;
    return (int32_t)ks_floor_div((int64_t)m->buf[a] * (32768 - (int64_t)m->f) + (int64_t)m->buf[b] * (int64_t)m->f + 16384, 32768);
}

/* len draws, each the word as a signed 32-bit value halved toward minus
 * infinity; then the write index at 0 and the previous read primed. */
static void ksm_fill(ks_model_t *m)
{
    uint32_t k;
    for (k = 0; k < m->len; k++) {
        m->word = ksm_draw(m->word);
        m->buf[k] = (int32_t)ks_floor_div(m->word < 0x80000000u ? (int64_t)m->word : (int64_t)m->word - INT64_C(4294967296), 2);
    }
    m->w = 0;
    m->prev = ksm_read(m);
}

static int32_t ksm_step(ks_model_t *m)
{
    const int32_t u = ksm_read(m);
    const int64_t avg = ks_floor_div((int64_t)u + m->prev + 1, 2);
    m->buf[m->w] = (int32_t)ks_floor_div(avg * (int64_t)m->loss + 16384, 32768);
    m->prev = u;
    m->w = (m->w + 1u) % m->len;
    return u;
}

/* The engine's increment in turns * 2^64, as sync_inc gives the reference. */
static uint64_t ks_iinc(const fm_op_t *o)
{
#if FM_PHASE64
    return o->inc;
#else
    return (uint64_t)o->inc << 32;
#endif
}

/* The loop's lowest resonance for delay n + f / 32768 behind the two-point
 * average: w (n + 1/2) + atan2(r sin w, 1 - r + r cos w) = 2 pi, by bisection. */
static double ks_res_hz(uint32_t n, uint32_t f, uint32_t fs)
{
    const double pi = acos(-1.0), r = f / 32768.0, d = n + 0.5;
    double lo = 0.0, hi = 2.0 * pi / d;
    int k;
    for (k = 0; k < 200; k++) {
        const double w = 0.5 * (lo + hi);
        if (w * d + atan2(r * sin(w), 1.0 - r + r * cos(w)) < 2.0 * pi) lo = w; else hi = w;
    }
    return 0.5 * (lo + hi) / (2.0 * pi) * fs;
}

static fm_patch_t ks_bound_patch(uint32_t loss, uint32_t seed, uint32_t buffer)
{
    fm_patch_t p = { 0 };
    p.op[0] = (fm_op_patch_t){ .ratio_q16 = 65536, .attack_ms = 1, .decay_ms = 1, .release_ms = 300 };
    p.op[1] = (fm_op_patch_t){ .ratio_q16 = 65536, .attack_ms = 1, .decay_ms = 1, .release_ms = 300 };
    p.ks[0] = (fm_ks_patch_t){ seed, (uint16_t)loss, (uint16_t)buffer, 1 };
    return p;
}

/* Every operator a view of cap samples into ks_store (cap 0: none). */
static int ks_bind(fm_voice_t *v, uint32_t cap)
{
    fm_ks_view_t view[FM_OPS];
    fm_assets_t a = { sizeof(fm_assets_t), 0, NULL, FM_OPS, view };
    uint32_t i;
    for (i = 0; i < FM_OPS; i++) { view[i].data = cap ? ks_store[i] + KS_GUARD : NULL; view[i].capacity = cap; }
    return fm_voice_bind_assets(v, &a);
}

static uint64_t ks_hash(const fm_patch_t *p, uint32_t fs, uint32_t freq, int16_t *out, uint32_t n)
{
    fm_voice_t v;
    fm_voice_init(&v, p, fs);
    (void)ks_bind(&v, FM_KS_BUFFER_MAX);
    fm_voice_note_on(&v, freq, 32767);
    fm_render_i16(&v, out, n);
    return fm_golden_hash(out, n);
}

/* The model takes the engine's delay after a note-on, a pitch event or a
 * fill, from the reference's division; 1 when the engine's differs. */
static int ks_model_tune(ks_model_t *m, const fm_voice_t *v)
{
    (void)ref_ks_delay(ks_iinc(&v->op[0]), m->len, &m->n, &m->f);
    return v->op[0].ks_n != m->n || v->op[0].ks_f != m->f;
}

/* A pitch event's clamp code; agree 0 when the engine's delay differs. */
static int ks_code_at(fm_voice_t *v, uint32_t freq, uint32_t b, int *agree)
{
    uint32_t n, f;
    int c;
    fm_voice_pitch(v, freq);
    c = ref_ks_delay(ks_iinc(&v->op[0]), b, &n, &f);
    if (v->op[0].ks_n != n || v->op[0].ks_f != f) *agree = 0;
    return c;
}

/* Renders n samples one at a time beside the model; mismatches counted. */
static uint32_t ks_lockstep(fm_voice_t *v, ks_model_t *m, uint32_t n)
{
    uint32_t t, miss = 0;
    int16_t x;
    for (t = 0; t < n; t++) {
        fm_render_i16(v, &x, 1);
        if (v->op[0].ks_prev != ksm_step(m)) miss++;
    }
    return miss;
}

static int ks_same(const ks_model_t *m, const fm_voice_t *v)
{
    return !memcmp(ks_store[0] + KS_GUARD, m->buf, m->len * sizeof *m->buf)
        && v->op[0].ks_w == m->w && v->op[0].ks_prev == m->prev && v->op[0].ks_word == m->word;
}

static unsigned ks_bounds(uint32_t fs)
{
    bound_t bounds = { "KSBOUND-FAIL", 0, 0 };
    static ks_model_t m;
    static int32_t snap[FM_KS_BUFFER_MAX];
    static int16_t xa[24000], xb[24000];
    static const uint32_t BUF[2] = { 64, 8192 };
    static const struct { uint32_t b, millihz, loss, seed; } MX[5] = {
        { 64, 100000, 32767, 7 }, { 64, 0, 30000, 0xDEADBEEFu }, { 64, 1000000, 32700, 12345 },
        { 8192, 110000, 32767, 7 }, { 8192, 2000, 32700, 99 } };
    fm_voice_t v;
    fm_patch_t p, q;
    uint32_t i, k, j, n, interior = 0, clamped = 0, miss, rn, rf;
    double max_tune = 0.0, max_cents = 0.0;
    int32_t residual = 0;
    int code, agree;
    /* tuning: 400 pitch events from 1 Hz to 0.45 fs, the engine's delay the
     * reference's, the clamp the exact rule's, the interior within 2^-16 */
    for (j = 0; j < 2u; j++) {
        const uint32_t b = BUF[j];
        p = ks_bound_patch(32767, 7, b);
        fm_voice_init(&v, &p, fs);
        BOUND_CHECK(&bounds, ks_bind(&v, b) == FM_BIND_OK);
        fm_voice_note_on(&v, 1000, 32767);
        for (k = 0, miss = 0; k < 400u; k++) {
            const uint32_t f = (uint32_t)floor(1000.0 * pow(0.45 * fs, k / 399.0));
            double per, e;
            fm_voice_pitch(&v, f);
            code = ref_ks_delay(ks_iinc(&v.op[0]), b, &rn, &rf);
            per = ldexp(1.0, 64) / (double)ks_iinc(&v.op[0]);
            if (v.op[0].ks_n != rn || v.op[0].ks_f != rf) miss++;
            if (code == 1) { clamped++; if (per < b - 1.5 - 1e-9 * b || rn != b - 2u || rf) miss++; }
            else if (code == 2) { clamped++; if (per > 2.5 + 1e-9 || rn != 2u || rf) miss++; }
            else {
                interior++;
                e = fabs(rn + rf / 32768.0 - (per - 0.5));
                if (e > max_tune) max_tune = e;
                if (per > b - 1.5 + 1e-9 * b || per < 2.5 - 1e-9 || e > ldexp(1.0, -16) + 1e-9) miss++;
            }
        }
        BOUND_CHECK(&bounds, miss == 0u);
        /* the clamps' edges, by bisection on whole millihertz */
        for (k = 0; k < 2u; k++) {
            const double fc = fs * 1000.0 / (k ? 2.5 : b - 1.5);
            uint32_t lo = (uint32_t)(fc * 0.98), hi = (uint32_t)(fc * 1.02);
            int clo, chi;
            agree = 1;
            clo = ks_code_at(&v, lo, b, &agree);
            while (hi - lo > 1u) {
                const uint32_t mid = lo + (hi - lo) / 2u;
                if (ks_code_at(&v, mid, b, &agree) == clo) lo = mid; else hi = mid;
            }
            clo = ks_code_at(&v, lo, b, &agree);
            chi = ks_code_at(&v, hi, b, &agree);
            BOUND_CHECK(&bounds, agree && clo == (k ? 0 : 1) && chi == (k ? 2 : 0));
        }
    }
    /* KSTUNE: the A notes at both lengths, the loop's resonance in cents */
    for (j = 0, miss = 0; j < 2u; j++)
        for (k = 0; k < 9u; k++) {
            const uint32_t b = BUF[j], f = 27500u << k;
            double hz, cents;
            p = ks_bound_patch(32767, 7, b);
            fm_voice_init(&v, &p, fs);
            (void)ks_bind(&v, b);
            fm_voice_note_on(&v, f, 32767);
            code = ref_ks_delay(ks_iinc(&v.op[0]), b, &rn, &rf);
            hz = ks_res_hz(v.op[0].ks_n, v.op[0].ks_f, fs);
            cents = 1200.0 * log2(hz / (f / 1000.0));
            printf("KSTUNE fs=%u buffer=%u note=A%u millihz=%u n=%u f=%u clamp=%s cents=%.3f\n", fs, b, k, f,
                   v.op[0].ks_n, v.op[0].ks_f, code == 1 ? "long" : code == 2 ? "short" : "no", cents);
            if (!code && k >= 1u && k <= 6u) {
                if (fabs(cents) > max_cents) max_cents = fabs(cents);
                if (fabs(cents) > 5.0) miss++;
            }
        }
    BOUND_CHECK(&bounds, miss == 0u);
    /* the loop bit for bit beside the model, guard words untouched, at the
     * shortest and longest buffers with long, short and interior delays */
    for (j = 0; j < 5u; j++) {
        const uint32_t b = MX[j].b, f = MX[j].millihz ? MX[j].millihz : fs * 450u;
        for (i = 0; i < KS_GUARD; i++) ks_store[0][i] = ks_store[0][KS_GUARD + b + i] = 0x5A5A5A5A;
        p = ks_bound_patch(MX[j].loss, MX[j].seed, b);
        fm_voice_init(&v, &p, fs);
        miss = ks_bind(&v, b) != FM_BIND_OK;
        fm_voice_note_on(&v, f, 32767);
        m.len = b; m.loss = MX[j].loss;
        m.word = MX[j].seed ^ 0x9E3779B9u;
        if (!m.word) m.word = 0x9E3779B9u;
        miss += (uint32_t)ks_model_tune(&m, &v);
        ksm_fill(&m);
        miss += !ks_same(&m, &v);
        miss += ks_lockstep(&v, &m, b == 64u ? 3u * b + 100u : 2u * b + 100u);
        miss += !ks_same(&m, &v);
        for (i = 0; i < KS_GUARD; i++) miss += ks_store[0][i] != 0x5A5A5A5A || ks_store[0][KS_GUARD + b + i] != 0x5A5A5A5A;
        BOUND_CHECK(&bounds, miss == 0u);
    }
    /* loss 0: silent from sample N + 1, every sample written zero after a pass */
    p = ks_bound_patch(0, 7, 64);
    fm_voice_init(&v, &p, fs);
    (void)ks_bind(&v, 64);
    fm_voice_note_on(&v, 1000000, 32767);
    n = v.op[0].ks_n;
    for (k = 0, miss = 0; k < 128u; k++) {
        fm_render_i16(&v, xa, 1);
        if (k >= n + 1u && v.op[0].ks_prev) miss++;
    }
    for (i = 0; i < 64u; i++) miss += ks_store[0][KS_GUARD + i] != 0;
    BOUND_CHECK(&bounds, miss == 0u && n >= 2u && n <= 62u);
    /* a long ring decays without a growing tail: loss 32000, 110 Hz, 10 s */
    p = ks_bound_patch(32000, 7, 2048);
    fm_voice_init(&v, &p, fs);
    (void)ks_bind(&v, 2048);
    fm_voice_note_on(&v, 110000, 32767);
    for (n = 10u * fs - fs / 10u; n; n -= k) { k = n < 24000u ? n : 24000u; fm_render_i16(&v, xa, k); }
    for (k = 0; k < fs / 10u; k++) {
        fm_render_i16(&v, xa, 1);
        if (abs(v.op[0].ks_prev) > residual) residual = abs(v.op[0].ks_prev);
    }
    BOUND_CHECK(&bounds, residual < 32768 && fm_voice_active(&v));
    /* the lifecycle beside the model: note-off and the ring-out, silence,
     * a retrigger at a new pitch, a pitch event, a refused rebind */
    p = ks_bound_patch(32700, 5, 256);
    fm_voice_init(&v, &p, fs);
    for (i = 0; i < 256u; i++) ks_store[0][KS_GUARD + i] = 0x13579BDF;
    miss = ks_bind(&v, 256) != FM_BIND_OK;
    for (i = 0; i < 256u; i++) miss += ks_store[0][KS_GUARD + i] != 0x13579BDF;          /* a binding writes nothing */
    miss += v.op[0].ks_word != 0u || v.op[0].ks_w != 0u || v.op[0].ks_prev != 0;
    fm_voice_note_on(&v, 440000, 32767);
    m.len = 256; m.loss = 32700; m.word = 5u ^ 0x9E3779B9u;
    miss += (uint32_t)ks_model_tune(&m, &v);
    ksm_fill(&m);
    memcpy(snap, m.buf, 256u * sizeof *snap);                                      /* the first fill */
    miss += !ks_same(&m, &v) + ks_lockstep(&v, &m, 1000);
    BOUND_CHECK(&bounds, miss == 0u);
    fm_voice_note_off(&v);
    BOUND_CHECK(&bounds, ks_same(&m, &v));                                                     /* note-off leaves the loop */
    for (k = 0, miss = 0; k < fs && fm_voice_active(&v); k++) miss += ks_lockstep(&v, &m, 1);
    BOUND_CHECK(&bounds, miss == 0u && !fm_voice_active(&v));
    fm_render_i16(&v, xa, 500);
    BOUND_CHECK(&bounds, ks_same(&m, &v));                                                     /* an idle voice leaves it too */
    fm_voice_note_on(&v, 440000, 32767);                                           /* from silence: the seed again */
    BOUND_CHECK(&bounds, !memcmp(ks_store[0] + KS_GUARD, snap, 256u * sizeof *snap) && v.op[0].ks_w == 0u);
    m.word = 5u ^ 0x9E3779B9u;
    miss = (uint32_t)ks_model_tune(&m, &v);
    ksm_fill(&m);
    miss += !ks_same(&m, &v) + ks_lockstep(&v, &m, 500);
    fm_voice_note_on(&v, 660000, 32767);                                           /* sounding: the word continues */
    miss += (uint32_t)ks_model_tune(&m, &v);
    ksm_fill(&m);
    miss += !ks_same(&m, &v) + ks_lockstep(&v, &m, 300);
    BOUND_CHECK(&bounds, miss == 0u && memcmp(ks_store[0] + KS_GUARD, snap, 256u * sizeof *snap) != 0);
    memcpy(snap, ks_store[0] + KS_GUARD, 256u * sizeof *snap);
    rn = v.op[0].ks_n; rf = v.op[0].ks_f;
    fm_voice_pitch(&v, 880000);                                                    /* a retune, no refill */
    BOUND_CHECK(&bounds, !memcmp(ks_store[0] + KS_GUARD, snap, 256u * sizeof *snap) && ks_same(&m, &v)
             && (v.op[0].ks_n != rn || v.op[0].ks_f != rf));
    miss = (uint32_t)ks_model_tune(&m, &v) + ks_lockstep(&v, &m, 2000);
    BOUND_CHECK(&bounds, miss == 0u && ks_same(&m, &v));
    memcpy(snap, ks_store[0] + KS_GUARD, 256u * sizeof *snap);
    BOUND_CHECK(&bounds, ks_bind(&v, 256) == FM_BIND_ACTIVE && !memcmp(ks_store[0] + KS_GUARD, snap, 256u * sizeof *snap));
    /* the binding: one sample short refused, none or no binding silent */
    p = ks_bound_patch(32767, 7, 256);
    fm_voice_init(&v, &p, fs);
    BOUND_CHECK(&bounds, ks_bind(&v, 255) == FM_BIND_CAPACITY && ks_bind(&v, 256) == FM_BIND_OK && v.op[0].ks_buf);
    n = fs / 4u;
    for (k = 0; k < 2u; k++) {
        fm_voice_init(&v, &p, fs);
        if (!k) BOUND_CHECK(&bounds, ks_bind(&v, 0) == FM_BIND_OK && !v.op[0].ks_buf);
        else BOUND_CHECK(&bounds, !v.op[0].ks_buf);
        fm_voice_note_on(&v, 220000, 32767);
        fm_render_i16(&v, xa, n);
        for (i = 0, miss = 0; i < n; i++) miss += xa[i] != 0;
        BOUND_CHECK(&bounds, miss == 0u);
    }
    /* canonical twins */
    n = fs / 4u;
    p = ks_bound_patch(32767, 7, 2048); q = ks_bound_patch(40000, 7, 2048);
    BOUND_CHECK(&bounds, ks_hash(&p, fs, 220000, xa, n) == ks_hash(&q, fs, 220000, xb, n));
    fm_voice_init(&v, &q, fs);
    BOUND_CHECK(&bounds, v.patch.ks[0].loss_q15 == 32767u);
    p = ks_bound_patch(32700, 0x9E3779B9u, 2048); q = ks_bound_patch(32700, 0, 2048);
    BOUND_CHECK(&bounds, ks_hash(&p, fs, 220000, xa, n) == ks_hash(&q, fs, 220000, xb, n));
    fm_voice_init(&v, &q, fs);
    BOUND_CHECK(&bounds, v.patch.ks[0].seed == 0x9E3779B9u);
    p = ks_bound_patch(32700, 7, 64); q = ks_bound_patch(32700, 7, 100);
    BOUND_CHECK(&bounds, ks_hash(&p, fs, 220000, xa, n) == ks_hash(&q, fs, 220000, xb, n));
    {
        static const uint16_t bin[5] = { 100, 5000, 0, 65535, 4096 }, bout[5] = { 64, 4096, 64, 8192, 4096 };
        for (k = 0, miss = 0; k < 5u; k++) {
            q = ks_bound_patch(32700, 7, bin[k]);
            fm_voice_init(&v, &q, fs);
            miss += v.patch.ks[0].buffer != bout[k];
        }
        BOUND_CHECK(&bounds, miss == 0u);
    }
    p = ks_bound_patch(32700, 7, 2048); p.ks[0] = (fm_ks_patch_t){ 0, 0, 0, 0 };     /* the plain sine */
    (void)ks_hash(&p, fs, 220000, xa, n);
    q = ks_bound_patch(32700, 7, 2048); q.pd[0] = (fm_pd_patch_t){ 8192, 1 };        /* with PD: neither */
    (void)ks_hash(&q, fs, 220000, xb, n);
    BOUND_CHECK(&bounds, !memcmp(xa, xb, n * sizeof *xa));
    fm_voice_init(&v, &q, fs);
    BOUND_CHECK(&bounds, !v.patch.ks[0].enabled && !v.patch.pd[0].enabled && !v.op[0].ks_on);
    q = ks_bound_patch(32700, 7, 2048); q.wt[0] = (fm_wt_patch_t){ 0, 0, 1 };        /* with a wavetable: neither */
    (void)ks_hash(&q, fs, 220000, xb, n);
    BOUND_CHECK(&bounds, !memcmp(xa, xb, n * sizeof *xa));
    q = ks_bound_patch(32700, 7, 2048); q.ks[0] = (fm_ks_patch_t){ 1234, 500, 300, 0 };  /* disabled fields */
    fm_voice_init(&v, &q, fs);
    BOUND_CHECK(&bounds, !v.patch.ks[0].seed && !v.patch.ks[0].loss_q15 && !v.patch.ks[0].buffer && !v.op[0].ks_on);
    q = ks_bound_patch(32700, 7, 2048);                                             /* the loop plays, not the sine */
    (void)ks_hash(&q, fs, 220000, xb, n);
    BOUND_CHECK(&bounds, memcmp(xa, xb, n * sizeof *xa) != 0);
    p.op[0].wave = 3;                                                               /* a waveform: no loop */
    (void)ks_hash(&p, fs, 220000, xa, n);
    q.op[0].wave = 3;
    (void)ks_hash(&q, fs, 220000, xb, n);
    BOUND_CHECK(&bounds, !memcmp(xa, xb, n * sizeof *xa));
    fm_voice_init(&v, &q, fs);
    BOUND_CHECK(&bounds, !v.patch.ks[0].enabled && !v.op[0].ks_on);
    for (k = 0; k < 3u; k++) {   /* 0: PM into the loop ignored; 1: its sync dropped; 2: its PM out heard */
        uint64_t h[2][2];
        for (j = 0; j < 2u; j++)          /* j 0: op 0 a loop, 1: a sine (the edge's control) */
            for (i = 0; i < 2u; i++) {    /* i 0: without the edge, 1: with it */
                q = ks_bound_patch(32700, 7, 2048);
                if (j) q.ks[0].enabled = 0;
                q.route.ops = 2;
                q.route.carriers = k == 0 ? 1u : k == 1 ? 3u : 2u;
                q.op[1].ratio_q16 = k == 1 ? 98304u : 65536u;
                if (k == 0) { q.op[1].index_q10 = 2000; if (i) q.route.w[1][0] = 256; }
                else { q.op[0].index_q10 = 2000; if (i) { q.route.w[0][1] = 256; q.route.kind[0][1] = k == 1 ? 3u : 0u; } }
                h[j][i] = ks_hash(&q, fs, 220000, xa, n);
                if (k == 1 && i && !j) { fm_voice_init(&v, &q, fs); h[j][i] ^= v.sync_edges; }
            }
        BOUND_CHECK(&bounds, h[1][0] != h[1][1] && (k == 2 ? h[0][0] != h[0][1] : h[0][0] == h[0][1]));
    }
    printf("KSBOUND fs=%u phase64=%d checks=%u failures=%u interior=%u clamped=%u max_tune=%.3g max_cents=%.3f residual_q30=%d\n",
           fs, FM_PHASE64, bounds.checks, bounds.bad, interior, clamped, max_tune, max_cents, (int)residual);
    return bounds.bad;
}

static unsigned sync_bounds(uint32_t fs)
{
    bound_t bounds = { "SYNCBOUND-FAIL", 0, 0 };
    static const uint16_t weights[] = { 0, 1, 255, 256, 257, 65535 };
    static const uint32_t notes[] = { 110000, 220000, 330000 };
    const uint64_t half = (SYNC_MASK >> 1) + 1u;
    fm_patch_t p, q;
    fm_voice_t va, vb;
    ref_voice_t ra, rb;
    int32_t xa, xb;
    double ya, yb;
    uint32_t rng = 0x5bd1e995u, f, i, j, s, k;
    unsigned ends = 0;
    /* Raw weights: nonzero plays as 256, 0 is no edge; never PM or gain. */
    for (k = 0; k < sizeof weights / sizeof weights[0]; ++k) {
        p = amp_check_patch(); p.route.kind[1][0] = 3; p.route.w[1][0] = weights[k];
        p.op[1].index_q10 = 1024;
        q = p; q.route.w[1][0] = weights[k] ? 256 : 0;
        amp_check_ready(&va, &ra, &p, fs); amp_check_ready(&vb, &rb, &q, fs);
        BOUND_CHECK(&bounds, !memcmp(&va, &vb, sizeof va) && !memcmp(&ra, &rb, sizeof ra));
        BOUND_CHECK(&bounds, va.sync_edges == (weights[k] ? 2u : 0u) && ra.sync_edges == va.sync_edges);
        BOUND_CHECK(&bounds, !va.amp_edges && !ra.amp_edges && va.patch.route.w[1][0] == (weights[k] ? 256 : 0));
        for (s = 0; s < 64; ++s) {
            fm_render_q30(&va, &xa, 1); fm_render_q30(&vb, &xb, 1);
            ref_render(&ra, &ya, 1); ref_render(&rb, &yb, 1);
            BOUND_CHECK(&bounds, xa == xb && ya == yb);
        }
    }
    p = amp_check_patch();
    for (i = 0; i < FM_OPS; ++i) for (j = 0; j < FM_OPS; ++j) { p.route.kind[i][j] = 3; p.route.w[i][j] = 256; }
    amp_check_ready(&va, &ra, &p, fs);
    k = 0; for (i = 0; i < FM_OPS; ++i) for (j = 0; j < FM_OPS; ++j) if (i != j) k |= 1u << (4 * j + i);
    BOUND_CHECK(&bounds, va.sync_edges == k && ra.sync_edges == k && !va.amp_edges);   /* the diagonal is never an edge */
    p = amp_check_patch(); p.route.kind[0][0] = 3; p.route.w[0][0] = 256;
    amp_check_ready(&va, &ra, &p, fs); BOUND_CHECK(&bounds, !va.sync_edges && !ra.sync_edges);
    p = amp_check_patch(); p.route.ops = 0; p.route.kind[1][0] = 3; p.route.w[1][0] = 256;
    amp_check_ready(&va, &ra, &p, fs); BOUND_CHECK(&bounds, !va.sync_edges && !ra.sync_edges);
    if (FM_OPS > 2) {
        p = amp_check_patch(); p.route.ops = 2;
        p.route.kind[FM_OPS - 1][0] = 3; p.route.w[FM_OPS - 1][0] = 256;
        p.route.kind[0][FM_OPS - 1] = 3; p.route.w[0][FM_OPS - 1] = 256;
        amp_check_ready(&va, &ra, &p, fs); BOUND_CHECK(&bounds, !va.sync_edges && !ra.sync_edges);
        p.route.kind[1][0] = 3; p.route.w[1][0] = 9; p.route.kind[0][1] = 1; p.route.w[0][1] = 100;
        amp_check_ready(&va, &ra, &p, fs);
        BOUND_CHECK(&bounds, va.sync_edges == 2u && ra.sync_edges == 2u && va.amp_edges && ra.amp_edges);
    }
    /* A sync edge whose source never wraps changes no output, beside PM
     * and AM edges; a target's forced reset is no pulse to a later target. */
    for (k = 0; k < 2; ++k) {
        uint32_t resets = 0;
        uint64_t last = 0;
        p = amp_check_patch(); p.route.ops = 4 < FM_OPS ? 4 : FM_OPS;
        for (i = 0; i < FM_OPS; ++i) { p.op[i].index_q10 = 2048; p.op[i].sustain_db10 = 0; }
        p.op[0].ratio_q16 = 180224;
        if (k == 0) {
            p.op[1].ratio_q16 = 0; p.op[1].phase_u16 = 65535;
            if (FM_OPS > 3) { p.route.w[2][0] = 300; p.route.kind[3][0] = 1; p.route.w[3][0] = 200; }
        } else {
            p.op[1].ratio_q16 = 1; p.op[1].phase_u16 = 32768;
            p.op[2].ratio_q16 = 196608; p.route.kind[2][1] = 3; p.route.w[2][1] = 256;
        }
        q = p; p.route.kind[1][0] = 3; p.route.w[1][0] = 256;
        amp_check_ready(&va, &ra, &p, fs); amp_check_ready(&vb, &rb, &q, fs);
        for (s = 0; s < 4096; ++s) {
            fm_render_q30(&va, &xa, 1); fm_render_q30(&vb, &xb, 1);
            ref_render(&ra, &ya, 1); ref_render(&rb, &yb, 1);
            BOUND_CHECK(&bounds, xa == xb && ya == yb);
            if (s && (uint64_t)va.op[1].phase < last) ++resets;
            last = va.op[1].phase;
        }
        BOUND_CHECK(&bounds, k == 0 ? resets == 0 : resets > 10);
    }
    /* The model against both voices over seeded fixtures. Event times are
     * in samples, so the check count is the same at every rate and width. */
    for (f = 0; f < 40; ++f) {
        sync_model_t m;
        uint32_t ev[8], ty[8], fr[8], wave[FM_OPS], nev = 0, len, e = 0, any = 0;
        memset(&m, 0, sizeof m);
        p = amp_check_patch();
        p.route.ops = (uint8_t)(2 + amp_check_draw(&rng) % (FM_OPS - 1));
        p.route.carriers = (uint8_t)(1 + amp_check_draw(&rng) % ((1u << p.route.ops) - 1u));
        if (amp_check_draw(&rng) & 1u) { p.filter.cutoff_millihz = 1000000; p.filter.q_milli = 707; p.filter.mode = 1; }
        for (i = 0; i < FM_OPS; ++i) {
            fm_op_patch_t *o = &p.op[i];
            const uint32_t r = amp_check_draw(&rng) % 8u;
            o->ratio_q16 = r == 0 ? 0u : r == 1 ? 65536u : 1u + amp_check_draw(&rng) % (64u << 16);
            if (r == 7) o->fixed_millihz = 1u + amp_check_draw(&rng) % 20000000u;
            o->phase_u16 = (uint16_t)(amp_check_draw(&rng) % 4u ? amp_check_draw(&rng) : 65535u);
            o->free_run = (uint16_t)(amp_check_draw(&rng) % 3u);
            o->wave = (uint16_t)(amp_check_draw(&rng) % 4u ? 0 : 4);
            o->level_db10 = (uint16_t)(amp_check_draw(&rng) % 200u);
            o->index_q10 = (uint16_t)(amp_check_draw(&rng) % 4096u);
            o->attack_ms = 1u + amp_check_draw(&rng) % 5u;
            o->sustain_db10 = (uint16_t)(amp_check_draw(&rng) % 2u ? 0 : 200);
            o->release_ms = 1u + amp_check_draw(&rng) % 8u;
            wave[i] = o->wave;
            for (j = 0; j < FM_OPS; ++j) {
                const uint32_t kind = amp_check_draw(&rng) % 8u;
                if (kind < 3u) continue;
                p.route.kind[j][i] = (uint8_t)(kind < 5u ? 3u : kind - 5u);
                p.route.w[j][i] = (uint16_t)(1u + amp_check_draw(&rng) % (kind < 6u ? 1024u : 256u));
            }
        }
        m.n = p.route.ops;
        for (i = 0; i < m.n; ++i) {
            m.start[i] = (uint64_t)p.op[i].phase_u16 << (FM_PHASE64 ? 48 : 16);
            m.free[i] = p.op[i].free_run;
            for (j = 0; j < m.n; ++j)
                if (j != i && p.route.kind[j][i] == 3u && p.route.w[j][i]) m.in[i] |= 1u << j;
            any |= m.in[i];
        }
        ev[nev] = 0; ty[nev] = EV_ON; fr[nev++] = notes[1];
        ev[nev] = 60 + amp_check_draw(&rng) % 300u; ty[nev] = EV_PITCH; fr[nev] = notes[amp_check_draw(&rng) % 3u]; ++nev;
        ev[nev] = ev[nev - 1] + 40 + amp_check_draw(&rng) % 300u; ty[nev] = EV_ON; fr[nev++] = notes[1];
        ev[nev] = ev[nev - 1] + 40 + amp_check_draw(&rng) % 300u; ty[nev] = EV_OFF; fr[nev++] = 0;
        ev[nev] = ev[nev - 1] + 500 + amp_check_draw(&rng) % 300u; ty[nev] = EV_ON; fr[nev++] = notes[0];
        ev[nev] = ev[nev - 1] + 60 + amp_check_draw(&rng) % 200u; ty[nev] = EV_OFF; fr[nev++] = 0;
        ev[nev] = ev[nev - 1] + 10 + amp_check_draw(&rng) % 60u; ty[nev] = EV_ON; fr[nev++] = notes[2];
        ev[nev] = ev[nev - 1] + 30 + amp_check_draw(&rng) % 200u; ty[nev] = EV_PITCH; fr[nev] = notes[amp_check_draw(&rng) % 3u]; ++nev;
        len = ev[nev - 1] + 100 + amp_check_draw(&rng) % 200u;
        memset(&va, 0, sizeof va); memset(&ra, 0, sizeof ra);
        fm_voice_init(&va, &p, fs); ref_voice_init(&ra, &p, fs);
        BOUND_CHECK(&bounds, !sync_inc(&va, &ra, (int)m.n));
        BOUND_CHECK(&bounds, (va.sync_edges != 0u) == (any != 0u));
        for (i = 0; i < m.n; ++i) { m.phase[i] = va.op[i].phase; m.inc[i] = va.op[i].inc; m.count[i] = va.op[i].noise_index; }
        for (s = 0; s < len; ++s) {
            uint32_t word[FM_OPS], clocks = 0u;
            int sounding, rsounding;
            while (e < nev && ev[e] == s) {
                BOUND_CHECK(&bounds, sync_check_event(&va, &ra, &m, ty[e], fr[e]));
                for (i = 0; i < m.n; ++i)
                    BOUND_CHECK(&bounds, m.inc[i] <= half && (uint64_t)va.op[i].phase == m.phase[i]
                               && ra.op[i].iphase == (uint64_t)va.op[i].phase << SYNC_SHIFT
                               && (m.free[i] != 2u || wave[i] != 4u || va.op[i].noise_index == (uint32_t)m.count[i]));
                ++e;
            }
            if (s == 30)                                      /* one sample before, at or after the wrap */
                for (i = 0; i < m.n; ++i)
                    sync_check_place(&va, &ra, &m, i, (SYNC_MASK - m.inc[i] + (f + i) % 3u) & SYNC_MASK);
            sounding = sync_check_sounding(&va);
            rsounding = sync_check_ref_sounding(&ra);
            for (i = 0; i < m.n; ++i) word[i] = va.op[i].noise;
            fm_render_q30(&va, &xa, 1); ref_render(&ra, &ya, 1);
            ends += sync_check_end(&va, &ra, sounding, rsounding);
            if (sounding) clocks = sync_model_sample(&m); else ++m.idle;
            for (i = 0; i < m.n; ++i) {
                BOUND_CHECK(&bounds, (uint64_t)va.op[i].phase == m.phase[i]);
                BOUND_CHECK(&bounds, ra.op[i].iphase == (uint64_t)va.op[i].phase << SYNC_SHIFT);
                if (wave[i] != 4u) BOUND_CHECK(&bounds, va.op[i].noise == word[i]);
                else if (m.free[i] == 2u) BOUND_CHECK(&bounds, va.op[i].noise_index == (uint32_t)m.count[i]);
                else BOUND_CHECK(&bounds, (va.op[i].noise != word[i]) == ((clocks >> i) & 1u));
            }
            BOUND_CHECK(&bounds, va.sync_wrap_prev == (va.sync_edges ? m.prev : 0u));
            BOUND_CHECK(&bounds, ra.sync_wrap_prev == va.sync_wrap_prev);
        }
    }
    /* The paired cases: the reference's integer phase and pulses per sample. */
    for (k = 0; k < NCASE; ++k)
        if (!strcmp(CASES[k].name, "syncturn") || !strcmp(CASES[k].name, "syncback")) {
            const spike_case_t *c = &CASES[k];
            const uint32_t src = !strcmp(c->name, "syncturn") ? 2u : 1u;
            uint32_t wraps = 0;
            int e = 0, ne = ev_count(c), sounding, rsounding;
            memset(&va, 0, sizeof va); memset(&ra, 0, sizeof ra);
            fm_voice_init(&va, &c->patch, fs); ref_voice_init(&ra, &c->patch, fs);
            BOUND_CHECK(&bounds, !sync_inc(&va, &ra, (int)va.ops));
            for (s = 0; s < 66150u; ++s) {
                while (e < ne && ev_sample(&c->ev[e], fs, 66150u) == s) {
                    a1_event(&va, &c->ev[e]); ref_event(&ra, &c->ev[e++]);
                    BOUND_CHECK(&bounds, !sync_inc(&va, &ra, (int)va.ops));
                }
                sounding = sync_check_sounding(&va); rsounding = sync_check_ref_sounding(&ra);
                fm_render_q30(&va, &xa, 1); ref_render(&ra, &ya, 1);
                ends += sync_check_end(&va, &ra, sounding, rsounding);
                for (i = 0; i < va.ops; ++i) BOUND_CHECK(&bounds, ra.op[i].iphase == (uint64_t)va.op[i].phase << SYNC_SHIFT);
                BOUND_CHECK(&bounds, ra.sync_wrap_prev == va.sync_wrap_prev);
                wraps += (va.sync_wrap_prev & src) != 0u;
            }
            BOUND_CHECK(&bounds, wraps > 50);
        }
    printf("SYNCBOUND fs=%u phase64=%d checks=%u failures=%u ends=%u\n", fs, FM_PHASE64, bounds.checks, bounds.bad, ends);
    return bounds.bad;
}

/* Division and a piecewise modulo triangle independently express the
 * transfer; production uses shifts and a shifted unsigned fold ramp. */
static int32_t shape_check_exact(unsigned kind, uint32_t gain, int32_t x)
{
    const int64_t one = INT64_C(1073741824), period = 4*one;
    int64_t magnitude = x < 0 ? -(int64_t)x : x, a, b, c, z;
    if (!kind) return x;
    magnitude = magnitude * gain / 16777216;
    if (kind == 3) {
        z = x < 0 ? -magnitude : magnitude;
        z %= period;
        if (z < 0) z += period;
        return (int32_t)(z <= one ? z : z <= 3*one ? 2*one-z : z-period);
    }
    a = magnitude < one ? magnitude : one;
    if (kind == 4 && x < 0 && a > one/2) a = one/2;        /* : clips at +one and -one/2 */
    if (kind == 2) {
        b = a*a/one; c = b*a/one; a = (3*a-c+1)/2;
        if (a > one) a = one;
    }
    return x < 0 ? -(int32_t)a : (int32_t)a;
}

static double shape_check_ideal(unsigned kind, double gain, double x)
{
    const double z = x*gain;
    double a;
    if (!kind) return x;
    if (kind == 3) {
        double r = fmod(z, 4.0);
        if (r < 0) r += 4;
        return r <= 1 ? r : r <= 3 ? 2-r : r-4;
    }
    a = fabs(z);
    if (a > 1) a = 1;
    if (kind == 4 && z < 0 && a > 0.5) a = 0.5;
    if (kind == 2) a = (3*a-a*a*a)/2;
    return z < 0 ? -a : a;
}

static fm_patch_t shape_check_patch(void)
{
    fm_patch_t p = amp_check_patch();
    unsigned i;
    p.route.ops = p.route.carriers = 0;
    for (i = 0; i < FM_OPS; ++i) {
        p.op[i].release_ms = 1; p.op[i].level_db10 = 60;
    }
    p.op[1].index_q10 = 1024;
    return p;
}

/* Two stationary carriers expose each pre-shape sample in the public
 * renderer's symmetric [-INT32_MAX, INT32_MAX] range. A square supplies
 * a +ONE partial sample, otherwise noise supplies its exact signed word. */
static int32_t shape_check_read(const fm_voice_t *base, int32_t x)
{
    fm_voice_t v = *base;
    const int32_t part[2] = { x/2, x-x/2 };
    int32_t out;
    unsigned i;
    v.ops = 2; v.carriers = 3;
    for (i = 0; i < 2; ++i) {
        v.op[i].stage = FM_STAGE_SUSTAIN; v.op[i].att = 0;
        v.op[i].inc = v.op[i].phase = 0;
        v.op[i].out = 1073741824;
        v.op[i].noise = (uint32_t)((int64_t)part[i]*2);
        v.patch.op[i].wave = part[i] == 1073741824 ? 7 : 4;
    }
    fm_render_q30(&v, &out, 1);
    return out;
}

/* : the same device on a persistent oracle voice, whose filter state
 * alone carries over, so an exact shaped sample reaches the filter through
 * the public renderer. Route edges are cleared so no PM reaches the square. */
static int32_t shape_check_feed(fm_voice_t *o, int32_t x)
{
    const int32_t part[2] = { x/2, x-x/2 };
    int32_t out;
    unsigned i;
    o->ops = 2; o->carriers = 3;
    memset(o->patch.route.w, 0, sizeof o->patch.route.w);
    memset(o->patch.route.kind, 0, sizeof o->patch.route.kind);
    for (i = 0; i < 2; ++i) {
        o->op[i].stage = FM_STAGE_SUSTAIN; o->op[i].att = 0;
        o->op[i].inc = o->op[i].phase = 0;
        o->op[i].out = 1073741824;
        o->op[i].noise = (uint32_t)((int64_t)part[i]*2);
        o->patch.op[i].wave = part[i] == 1073741824 ? 7 : 4;
    }
    fm_render_q30(o, &out, 1);
    return out;
}

static unsigned shape_bounds(uint32_t fs)
{
    bound_t bounds = { "SHAPEBOUND-FAIL", 0, 0 };
    static const int32_t endpoints[] = { -INT32_MAX, -1073741825, -1073741824, -1073741823,
                                        -1, 0, 1, 1073741823, 1073741824, 1073741825, INT32_MAX };
    fm_patch_t p = shape_check_patch();
    fm_voice_t v, twin;
    ref_voice_t r, rtwin;
    unsigned d, kind, i, mode, route, pos, ring = 0, joins = 0, exact_joins = 0, asym = 0, pre_ring = 0;
    uint32_t rng = 0x7A351F29u;
    double max_gain = 0, max_cubic = 0;
    for (d = 0; d <= 240; ++d) for (kind = 0; kind <= 4; ++kind) {
        double gain_error;
        p.shape.kind = (uint16_t)kind; p.shape.drive_db10 = (uint16_t)d;
        amp_check_ready(&v, &r, &p, fs);
        gain_error = fabs(v.shape_gain_q24 - pow(10.0, (double)d/200)*16777216.0);
        if (gain_error > max_gain) max_gain = gain_error;
        /* Half-unit cache rounding plus 1e-5 Q24 units for the Q48
         * dB conversion/exp2 approximation and binary64 evaluation. */
        BOUND_CHECK(&bounds, gain_error < 0.50001);
        for (i = 0; i < sizeof endpoints/sizeof endpoints[0] + 256; ++i) {
            int32_t x, got;
            double error;
            if (i < sizeof endpoints/sizeof endpoints[0]) x = endpoints[i];
            else {
                const uint32_t word = amp_check_draw(&rng);
                x = word <= INT32_MAX ? (int32_t)word : -1-(int32_t)(UINT32_MAX-word);
                if (x == INT32_MIN) x = -INT32_MAX;
            }
            got = shape_check_read(&v, x);
            BOUND_CHECK(&bounds, got == shape_check_exact(kind, (uint32_t)v.shape_gain_q24, x));
            /* Kinds 0 to 3 are odd; kind 4 is checked at -x against its model
             * and counted where it differs from the mirrored output . */
            if (kind == 4) {
                const int32_t mirrored = shape_check_read(&v, -x);
                BOUND_CHECK(&bounds, mirrored == shape_check_exact(4, (uint32_t)v.shape_gain_q24, -x));
                asym += mirrored != -got;
            } else BOUND_CHECK(&bounds, got == -shape_check_read(&v, -x));
            BOUND_CHECK(&bounds, !kind || (got >= -1073741824 && got <= 1073741824));
            BOUND_CHECK(&bounds, kind != 4 || (got >= -536870912 && got <= 1073741824));
            error = fabs(got-shape_check_ideal(kind, v.shape_gain_q24/16777216.0, x/1073741824.0)*1073741824.0);
            if (kind == 2 && error > max_cubic) max_cubic = error;
            /* Gain multiplication floors magnitude by <1 unit. Hard/fold
             * are 1-Lipschitz; cubic is 1.5-Lipschitz and its two floors
             * plus final rounding add <=1.5, hence <=3 units in total.
             * 1e-5 covers binary64 at the maximum driven magnitude <2^35. */
            BOUND_CHECK(&bounds, error <= (kind == 2 ? 3.0 : 1.0) + 1e-5);
        }
        if (kind == 3) {
            unsigned join;
            for (join = 1; join <= 5; join += 2) {
                const int64_t wanted = (int64_t)join * 1073741824;
                const int64_t center = wanted * 16777216 / v.shape_gain_q24;
                int delta;
                for (delta = -1; delta <= 1; ++delta) {
                    const int64_t x = center + delta;
                    int32_t got;
                    int64_t z;
                    if (x < 0 || x > INT32_MAX) continue;
                    z = x*v.shape_gain_q24/16777216;
                    got = shape_check_read(&v, (int32_t)x);
                    BOUND_CHECK(&bounds, got == shape_check_exact(3, (uint32_t)v.shape_gain_q24, (int32_t)x));
                    BOUND_CHECK(&bounds, got == -shape_check_read(&v, -(int32_t)x));
                    ++joins; exact_joins += z == wanted;
                }
            }
        }
    }
    /* The public carrier/filter boundary is symmetric; INT32_MIN is
     * unreachable here. The separate helper audit includes that raw value.
     * Actual authored drive caches give a lattice around +/-1,3,5 joins;
     * joins counts tested neighbouring inputs, exact_joins exact hits. */
    BOUND_CHECK(&bounds, joins > 0 && exact_joins > 0);
    BOUND_CHECK(&bounds, asym > 0);
    for (d = 0; d < 3; ++d) for (kind = 0; kind < 7; ++kind) for (pos = 0; pos < 4; ++pos) {
        fm_patch_t q;
        p.shape.kind = (uint16_t)(kind == 6 ? 65535 : kind);
        p.shape.drive_db10 = (uint16_t)(d == 0 ? 0 : d == 1 ? 241 : 65535);
        p.shape.pos = (uint16_t)(pos == 3 ? 65535 : pos);
        q = p;
        if (q.shape.kind > 4) q.shape.kind = 0;
        if (q.shape.pos != 1) q.shape.pos = 0;
        if (q.shape.drive_db10 > 240) q.shape.drive_db10 = 240;
        amp_check_ready(&v, &r, &p, fs); amp_check_ready(&twin, &rtwin, &q, fs);
        BOUND_CHECK(&bounds, !memcmp(&v, &twin, sizeof v) && !memcmp(&r, &rtwin, sizeof r));
    }
    for (route = 0; route < 2; ++route) for (mode = 0; mode <= 4; ++mode) for (kind = 0; kind <= 4; ++kind) {
        p = shape_check_patch(); p.shape.kind = (uint16_t)kind; p.shape.drive_db10 = 180;
        p.filter.mode = (uint16_t)mode; p.filter.cutoff_millihz = 1200000; p.filter.q_milli = 2000;
        if (route) {
            p.route.ops = 3; p.route.carriers = 5;
            p.route.w[1][0] = 256; p.route.kind[1][0] = 1;
            p.route.w[0][2] = 193; p.route.kind[0][2] = 2;
        }
        amp_check_ready(&v, &r, &p, fs);
        twin = v; twin.patch.shape.kind = 0; rtwin = r; rtwin.patch.shape.kind = 0;
        for (i = 0; i < 4096; ++i) {
            fm_voice_t normalized;
            ref_voice_t rnormalized;
            int32_t shaped, dry;
            double rshaped, rdry, want;
            unsigned j;
            if (i == 256) {
                fm_voice_note_off(&v); fm_voice_note_off(&twin);
                ref_voice_note_off(&r); ref_voice_note_off(&rtwin);
            }
            for (j = 0; j < FM_OPS; ++j) r.op[j].iinc = rtwin.op[j].iinc =
#if FM_PHASE64
                v.op[j].inc;
#else
                (uint64_t)v.op[j].inc << 32;
#endif
            fm_render_q30(&v, &shaped, 1); fm_render_q30(&twin, &dry, 1);
            ref_render(&r, &rshaped, 1); ref_render(&rtwin, &rdry, 1);
            BOUND_CHECK(&bounds, shaped == shape_check_exact(kind, (uint32_t)v.shape_gain_q24, dry));
            want = shape_check_ideal(kind, r.shape_gain, rdry/32768.0)*32768.0;
            BOUND_CHECK(&bounds, fabs(rshaped-want) <= 128*0x1p-52*(1+fabs(rdry/32768.0*r.shape_gain))*32768.0);
            normalized = v; normalized.patch.shape.kind = 0;
            rnormalized = r; rnormalized.patch.shape.kind = 0;
            BOUND_CHECK(&bounds, !memcmp(&normalized, &twin, sizeof twin));
            BOUND_CHECK(&bounds, !memcmp(&rnormalized, &rtwin, sizeof rtwin));
            BOUND_CHECK(&bounds, fm_voice_active(&v) == fm_voice_active(&twin));
            if (mode && i > 512 && twin.op[0].stage == FM_STAGE_IDLE && dry) ++ring;
        }
    }
    BOUND_CHECK(&bounds, ring > 0);
    /* , pos 1: the filter hears the shaped carrier sum. The oracle reads
     * the unshaped, unfiltered sum from a twin, shapes it with the exact model
     * and feeds that into a third voice's filter (shape_check_feed); once the
     * carriers go idle the oracle idles too, so both ring out the same filter
     * state through the public ring path and the shaper is never called. */
    for (route = 0; route < 2; ++route) for (mode = 1; mode <= 4; ++mode) for (kind = 0; kind <= 4; ++kind) {
        fm_voice_t oracle;
        p = shape_check_patch(); p.shape.kind = (uint16_t)kind; p.shape.drive_db10 = 180; p.shape.pos = 1;
        p.filter.mode = (uint16_t)mode; p.filter.cutoff_millihz = 1200000; p.filter.q_milli = 2000;
        if (route) {
            p.route.ops = 3; p.route.carriers = 5;
            p.route.w[1][0] = 256; p.route.kind[1][0] = 1;
            p.route.w[0][2] = 193; p.route.kind[0][2] = 2;
        }
        amp_check_ready(&v, &r, &p, fs);
        oracle = v; oracle.patch.shape.kind = 0;
        p.shape.kind = 0; p.shape.pos = 0; p.filter.mode = 0;
        amp_check_ready(&twin, &rtwin, &p, fs);
        for (i = 0; i < 4096; ++i) {
            int32_t shaped, sum, want;
            unsigned j;
            int sounding;
            if (i == 256) { fm_voice_note_off(&v); fm_voice_note_off(&twin); }
            sounding = fm_voice_active(&twin);     /* the renderer's idle test precedes the step */
            fm_render_q30(&v, &shaped, 1); fm_render_q30(&twin, &sum, 1);
            if (sounding) want = shape_check_feed(&oracle, shape_check_exact(kind, (uint32_t)v.shape_gain_q24, sum));
            else {
                for (j = 0; j < FM_OPS; ++j) oracle.op[j].stage = FM_STAGE_IDLE;
                fm_render_q30(&oracle, &want, 1);
                if (shaped) ++pre_ring;
            }
            BOUND_CHECK(&bounds, shaped == want);
            BOUND_CHECK(&bounds, fm_voice_active(&v) == fm_voice_active(&oracle));
        }
    }
    BOUND_CHECK(&bounds, pre_ring > 0);
    /* : a pre-position voice that never sounded stays exactly silent. */
    for (kind = 1; kind <= 4; ++kind) {
        int32_t out[64];
        p = shape_check_patch(); p.shape.kind = (uint16_t)kind; p.shape.drive_db10 = 240; p.shape.pos = 1;
        p.filter.mode = 1; p.filter.cutoff_millihz = 1200000; p.filter.q_milli = 2000;
        memset(&v, 0, sizeof v); fm_voice_init(&v, &p, fs);
        for (i = 0; i < 16; ++i) {
            unsigned j;
            fm_render_q30(&v, out, 64);
            for (j = 0; j < 64; ++j) BOUND_CHECK(&bounds, out[j] == 0);
            BOUND_CHECK(&bounds, !fm_voice_active(&v));
        }
    }
    printf("SHAPEBOUND fs=%u phase64=%d checks=%u failures=%u joins=%u exact_joins=%u ring=%u asym=%u pre_ring=%u max_gain_q24=%.9f max_cubic_q30=%.9f\n",
           fs, FM_PHASE64, bounds.checks, bounds.bad, joins, exact_joins, ring, asym, pre_ring, max_gain, max_cubic);
    return bounds.bad;
}

/* Independent signed-division recurrence and lifecycle oracle.
 * Coefficients come from initialization: this checks recurrence, saturation
 * and stage ordering, not a new proof of coefficient approximation. */
/* Feed two exact stationary carriers through the public render path.
 * Their sum exposes the symmetric Q2.30 input range while retaining both
 * filter states from the previous call. No private filter helper is used. */
static int32_t cascade_check_read(fm_voice_t *v, int32_t x)
{
    const int32_t part[2] = {
        x/2, x-x/2
    };
    int32_t out;
    unsigned i;
    v->ops = 2;
    v->carriers = 3;
    for (i = 0; i < 2; ++i) {
        v->op[i].stage = FM_STAGE_SUSTAIN;
        v->op[i].att = 0;
        v->op[i].inc = v->op[i].phase = 0;
        v->op[i].out = 1073741824;
        v->op[i].noise = (uint32_t)((int64_t)part[i]*2);
        v->patch.op[i].wave = part[i] == 1073741824 ? 7 : 4;
    }
    fm_render_q30(v, &out, 1);
    return out;
}
typedef struct {
    bound_t bounds;
    uint64_t sats[2][6], byq[2][2][6];
    int32_t peak[2];
} cascade_check_t;
static int64_t cascade_check_down(int64_t x,int64_t d)
{
    int64_t q=x/d;
    return q-(x%d<0);
}

static int32_t cascade_check_clip(cascade_check_t *ctx,int64_t x,unsigned stage,unsigned point)
{
    if(x>INT32_MAX) {
        ctx->sats[stage][point]++;
        return INT32_MAX;
    }
    if(x<-(int64_t)INT32_MAX) {
        ctx->sats[stage][point]++;
        return-INT32_MAX;
    }
    return(int32_t)x;
}

static int32_t cascade_check_model(cascade_check_t *ctx,fm_svf_t*f,int32_t x,unsigned stage)
{
    int64_t d,b,l,y;
    int32_t bp,lp;
    d=cascade_check_clip(ctx,(int64_t)x-f->ic2,stage,0);
    b=cascade_check_down((int64_t)f->a1*f->ic1+(int64_t)f->a2*d+536870912,1073741824);
    l=f->ic2+cascade_check_down((int64_t)f->a2*f->ic1+(int64_t)f->a3*d+536870912,1073741824);
    bp=cascade_check_clip(ctx,b,stage,1);
    lp=cascade_check_clip(ctx,l,stage,2);
    f->ic1=cascade_check_clip(ctx,2*b-f->ic1,stage,3);
    f->ic2=cascade_check_clip(ctx,2*l-f->ic2,stage,4);
    if(f->mode==1)y=lp;
    else if(f->mode==2)y=bp;
    else {
        y=(int64_t)x-cascade_check_down((int64_t)f->k*bp+268435456,536870912);
        if(f->mode==3)y-=lp;
    }
    {
        int32_t z=cascade_check_clip(ctx,y,stage,5);
        int32_t m=z<0?-z:z;
        if(m>ctx->peak[stage])ctx->peak[stage]=m;
        return z;
    }
}

static int cascade_check_quiet(int32_t x)
{
    return x>=-2048&&x<=2048;
}

static int32_t cascade_check_modelring(cascade_check_t *ctx,fm_svf_t*a,fm_svf_t*b)
{
    int32_t y;
    if(!(a->ic1||a->ic2||b->ic1||b->ic2))return 0;
    y=cascade_check_model(ctx,b,cascade_check_model(ctx,a,0,0),1);
    if((cascade_check_quiet(a->ic1)&&cascade_check_quiet(a->ic2)&&cascade_check_quiet(b->ic1)&&cascade_check_quiet(b->ic2))||++a->idle>=262144)a->ic1=a->ic2=b->ic1=b->ic2=0;
    return y;
}

static fm_patch_t cascade_check_patch(void)
{
    fm_patch_t p;
    unsigned i;
    memset(&p,0,sizeof p);
    p.filter.mode=1;
    p.filter.stages=2;
    p.filter.cutoff_millihz=20000;
    p.filter.q_milli=500;
    for(i=0;i<FM_OPS;i++) {
        p.op[i].ratio_q16=65536+i*16384;
        p.op[i].level_db10=120;
        p.op[i].attack_ms=1;
        p.op[i].decay_ms=20;
        p.op[i].release_ms=1;
    }
    return p;
}

static void cascade_check_init(fm_voice_t*v,const fm_patch_t*p,uint32_t fs)
{
    memset(v,0,sizeof*v);
    fm_voice_init(v,p,fs);
}

static int cascade_check_coefeq(const fm_svf_t*a,const fm_svf_t*b)
{
    return a->a1==b->a1&&a->a2==b->a2&&a->a3==b->a3&&a->k==b->k;
}

static void cascade_check_bounds(cascade_check_t *ctx)
{
    const uint32_t rates[]= {
        0,1,44100,48000,UINT32_MAX
    };
    const unsigned stages[]= {
        0,1,2,3,65535
    };
    unsigned ir,im,is;
    for(ir=0;ir<5;ir++)for(im=0;im<7;im++)for(is=0;is<5;is++) {
        fm_patch_t p=cascade_check_patch(),q;
        fm_voice_t a,b;
        ref_voice_t r,s;
        p.filter.stages=(uint16_t)stages[is];
        p.filter.mode=(uint16_t)(im==6?65535:im);
        q=p;
        q.filter.stages=q.filter.stages>2?2:q.filter.stages;
        cascade_check_init(&a,&p,rates[ir]);
        cascade_check_init(&b,&q,rates[ir]);
        BOUND_CHECK(&ctx->bounds, !memcmp(&a,&b,sizeof a));
        BOUND_CHECK(&ctx->bounds, a.svf.mode==(p.filter.mode<=4?p.filter.mode:0));
        BOUND_CHECK(&ctx->bounds, a.svf2.mode==(stages[is]>=2?a.svf.mode:0));
        BOUND_CHECK(&ctx->bounds, cascade_check_coefeq(&a.svf,&a.svf2));
        BOUND_CHECK(&ctx->bounds, !fm_voice_active(&a));
        memset(&r,0,sizeof r);
        memset(&s,0,sizeof s);
        ref_voice_init(&r,&p,rates[ir]);
        ref_voice_init(&s,&q,rates[ir]);
        BOUND_CHECK(&ctx->bounds, !memcmp(&r,&s,sizeof r));
    }
    for(ir=0;ir<5;ir++) {
        uint64_t rate=rates[ir]?rates[ir]:1,lo=(rate*5+11)/12,hi=rate*450;
        fm_patch_t p=cascade_check_patch();
        fm_voice_t a,b;
        p.filter.cutoff_millihz=0;
        p.filter.q_milli=0;
        cascade_check_init(&a,&p,rates[ir]);
        p.filter.cutoff_millihz=(uint32_t)lo;
        p.filter.q_milli=500;
        cascade_check_init(&b,&p,rates[ir]);
        BOUND_CHECK(&ctx->bounds, cascade_check_coefeq(&a.svf,&b.svf));
        p.filter.cutoff_millihz=UINT32_MAX;
        p.filter.q_milli=65535;
        cascade_check_init(&a,&p,rates[ir]);
        p.filter.cutoff_millihz=(uint32_t)(hi<UINT32_MAX?hi:UINT32_MAX);
        p.filter.q_milli=16000;
        cascade_check_init(&b,&p,rates[ir]);
        BOUND_CHECK(&ctx->bounds, cascade_check_coefeq(&a.svf,&b.svf));
    }
}

/* A finite stress grid, not a saturation-free response guarantee.
 * Each rate covers 384 configurations and 4096 samples per configuration.
 * The measured six clamp sites are reported separately for each stage/Q. */
static void cascade_check_grid(cascade_check_t *ctx, uint32_t fs)
{
    const int32_t amps[]= {
        268435456,1073741824,2147483647
    };
    unsigned ir,mode,cut,q,amp,sig,n;
    unsigned long long samples=0;
    memset(ctx->sats,0,sizeof ctx->sats);
    memset(ctx->peak,0,sizeof ctx->peak);
    for(mode=1;mode<=4;mode++)for(cut=0;cut<4;cut++)for(q=0;q<2;q++)for(amp=0;amp<3;amp++)for(sig=0;sig<4;sig++) {
        fm_patch_t p=cascade_check_patch();
        fm_voice_t v;
        fm_svf_t a,b;
        uint32_t seed=0x1729abcd;
        uint64_t start[2][6];
        unsigned si,pt;
        memcpy(start,ctx->sats,sizeof start);
        p.filter.mode=(uint16_t)mode;
        p.filter.cutoff_millihz=cut==0?(fs*5+11)/12:cut==1?20000:cut==2?1000000:fs*450;
        p.filter.q_milli=(uint16_t)(q?16000:500);
        cascade_check_init(&v,&p,fs);
        a=v.svf;
        b=v.svf2;
        for(n=0;n<4096;n++) {
            int32_t x,expect,got;
            seed^=seed<<13;
            seed^=seed>>17;
            seed^=seed<<5;
            x=sig==0?(n==0?amps[amp]:0):sig==1?(n&1?-amps[amp]:amps[amp]):sig==2?(int32_t)llround((double)amps[amp]*sin(6.283185307179586*(double)p.filter.cutoff_millihz/1000/fs*n)):(int32_t)((seed<=INT32_MAX?(int64_t)seed:(int64_t)seed-INT64_C(4294967296))*amps[amp]/INT64_C(2147483648));
            expect=cascade_check_model(ctx,&b,cascade_check_model(ctx,&a,x,0),1);
            got=cascade_check_read(&v,x);
            BOUND_CHECK(&ctx->bounds, got==expect);
            BOUND_CHECK(&ctx->bounds, !memcmp(&a,&v.svf,sizeof a)&&!memcmp(&b,&v.svf2,sizeof b));
            samples++;
        }
        for(si=0;si<2;si++)for(pt=0;pt<6;pt++)ctx->byq[q][si][pt]+=ctx->sats[si][pt]-start[si][pt];
    }
    printf("CASCADEGRID configurations=384 fs=%u samples=%llu points=v3,b,l,ic1,ic2,out\n",(unsigned)fs,samples);
    for(ir=0;ir<2;ir++)printf("CASCADE-SAT stage=%u peak=%d counts=%llu,%llu,%llu,%llu,%llu,%llu\n",ir+1,ctx->peak[ir],(unsigned long long)ctx->sats[ir][0],(unsigned long long)ctx->sats[ir][1],(unsigned long long)ctx->sats[ir][2],(unsigned long long)ctx->sats[ir][3],(unsigned long long)ctx->sats[ir][4],(unsigned long long)ctx->sats[ir][5]);
    for(q=0;q<2;q++)for(ir=0;ir<2;ir++)printf("CASCADE-SAT_Q q=%u stage=%u counts=%llu,%llu,%llu,%llu,%llu,%llu\n",q?16000:500,ir+1,(unsigned long long)ctx->byq[q][ir][0],(unsigned long long)ctx->byq[q][ir][1],(unsigned long long)ctx->byq[q][ir][2],(unsigned long long)ctx->byq[q][ir][3],(unsigned long long)ctx->byq[q][ir][4],(unsigned long long)ctx->byq[q][ir][5]);
}

/* Both states govern quiet exit and one shared timeout. FREE2 elapsed
 * samples advance while oscillator/control state remains frozen. */
static void cascade_check_ring(cascade_check_t *ctx, uint32_t fs)
{
    const int32_t states[][4]= {
        {
            0,0,1<<28,-(1<<27)
        }, {
            1<<28,-(1<<27),0,0
        }, {
            2048,-2048,1<<28,1<<27
        }, {
            1<<28,1<<27,2048,-2048
        }, {
            1,-1,1,-1
        }, {
            0,0,0,0
        }, {
            INT32_MAX,-INT32_MAX,INT32_MAX,-INT32_MAX
        }
    };
    unsigned mode,k,cap,n;
    unsigned upstream=0;
    for(mode=1;mode<=4;mode++)for(k=0;k<7;k++)for(cap=0;cap<2;cap++) {
        fm_patch_t p=cascade_check_patch();
        fm_voice_t v;
        fm_svf_t a,b;
        p.filter.mode=(uint16_t)mode;
        p.filter.q_milli=16000;
        p.lfo.free_run=2;
        p.lfo.rate_millihz=7000;
        p.lfo.wave=6;
        p.lfo.pitch_cents=200;
        p.lfo.mask_pitch=3;
        p.fenv.depth_cents=1200;
        p.fenv.attack_ms=20;
        cascade_check_init(&v,&p,fs);
        fm_voice_note_on(&v,220000,32767);
        {
            unsigned z;
            for(z=0;z<FM_OPS;z++)v.op[z].stage=FM_STAGE_IDLE;
        }v.ctl=1;
        v.svf.ic1=states[k][0];
        v.svf.ic2=states[k][1];
        v.svf2.ic1=states[k][2];
        v.svf2.ic2=states[k][3];
        v.svf.idle=cap?262143:0;
        v.svf2.idle=0;
        a=v.svf;
        b=v.svf2;
        for(n=0;n<512;n++) {
            fm_voice_t frozen=v;
            int32_t x,want=cascade_check_modelring(ctx,&a,&b);
            BOUND_CHECK(&ctx->bounds, fm_voice_active(&v)==!!(v.svf.ic1||v.svf.ic2||v.svf2.ic1||v.svf2.ic2));
            fm_render_q30(&v,&x,1);
            BOUND_CHECK(&ctx->bounds, x==want);
            frozen.svf=a;
            frozen.svf2=b;
            frozen.idle_samples++;
            BOUND_CHECK(&ctx->bounds, !memcmp(&v,&frozen,sizeof v));
            BOUND_CHECK(&ctx->bounds, !memcmp(&a,&v.svf,sizeof a)&&!memcmp(&b,&v.svf2,sizeof b));
            if(k==1&&n==0&&want)upstream++;
            if(cap)BOUND_CHECK(&ctx->bounds, !fm_voice_active(&v));
        }
    }
    BOUND_CHECK(&ctx->bounds, upstream>0);
    printf("CASCADERING upstream_nonzero=%u\n",upstream);
}

static void cascade_check_events(cascade_check_t *ctx, uint32_t fs)
{
    unsigned mode,fenv,k;
    for(mode=1;mode<=4;mode++)for(fenv=0;fenv<2;fenv++) {
        fm_patch_t p=cascade_check_patch();
        fm_voice_t v;
        int32_t s[4]= {
            123456,-345678,765432,-987654
        };
        p.filter.mode=(uint16_t)mode;
        if(fenv) {
            p.fenv.depth_cents=2400;
            p.fenv.attack_ms=20;
            p.fenv.decay_ms=100;
            p.fenv.sustain_permille=300;
            p.fenv.track_permille=1000;
        }
        cascade_check_init(&v,&p,fs);
        v.svf.ic1=s[0];
        v.svf.ic2=s[1];
        v.svf2.ic1=s[2];
        v.svf2.ic2=s[3];
        v.svf.idle=262143;
        for(k=0;k<3;k++) {
            if(k==0)fm_voice_filter(&v,1234000,16000);
            if(k==1)fm_voice_note_on(&v,123456,20000);
            if(k==2)fm_voice_note_on(&v,987654,30000);
            BOUND_CHECK(&ctx->bounds, v.svf.ic1==s[0]&&v.svf.ic2==s[1]&&v.svf2.ic1==s[2]&&v.svf2.ic2==s[3]);
            BOUND_CHECK(&ctx->bounds, cascade_check_coefeq(&v.svf,&v.svf2));
            if(k)BOUND_CHECK(&ctx->bounds, v.svf.idle==0);
        }
    }
}

/* The single-stage twin supplies the first stage output. Copy only its
 * current coefficients into the independent downstream recurrence; any
 * accidental copy of integrator state during a control tick is visible. */
static void cascade_check_public_order(cascade_check_t *ctx, uint32_t fs)
{
    unsigned route,mode,i,j;
    for(route=0;route<2;route++)for(mode=1;mode<=4;mode++) {
        fm_patch_t p=cascade_check_patch();
        fm_voice_t chain,single;
        fm_svf_t downstream;
        p.filter.mode=(uint16_t)mode;
        p.filter.cutoff_millihz=1234000;
        p.filter.q_milli=4000;
        p.fenv.depth_cents=2400;
        p.fenv.attack_ms=20;
        p.fenv.decay_ms=100;
        p.fenv.sustain_permille=500;
        if(route) {
            p.route.ops=3;
            p.route.carriers=5;
            p.route.w[1][0]=192;
            p.route.kind[1][0]=1;
            p.route.w[0][2]=230;
            p.route.kind[0][2]=2;
        }
        cascade_check_init(&chain,&p,fs);
        p.filter.stages=1;
        cascade_check_init(&single,&p,fs);
        fm_voice_note_on(&chain,220000,32767);
        fm_voice_note_on(&single,220000,32767);
        downstream=chain.svf2;
        for(i=0;i<2048;i++) {
            int32_t x,y,want;
            if(i==333) {
                fm_voice_filter(&chain,2400000,707);
                fm_voice_filter(&single,2400000,707);
            }
            if(i==777) {
                fm_voice_note_on(&chain,330000,20000);
                fm_voice_note_on(&single,330000,20000);
            }
            fm_render_q30(&chain,&x,1);
            fm_render_q30(&single,&y,1);
            downstream.a1=single.svf.a1;
            downstream.a2=single.svf.a2;
            downstream.a3=single.svf.a3;
            downstream.k=single.svf.k;
            want=cascade_check_model(ctx,&downstream,y,1);
            BOUND_CHECK(&ctx->bounds, x==want);
            BOUND_CHECK(&ctx->bounds, !memcmp(&downstream,&chain.svf2,sizeof downstream));
            BOUND_CHECK(&ctx->bounds, !memcmp(&chain.svf,&single.svf,sizeof chain.svf));
            for(j=0;j<FM_OPS;j++)BOUND_CHECK(&ctx->bounds, !memcmp(&chain.op[j],&single.op[j],sizeof chain.op[j]));
            BOUND_CHECK(&ctx->bounds, cascade_check_coefeq(&chain.svf,&chain.svf2));
        }
    }
}

static void cascade_check_blocks(cascade_check_t *ctx, uint32_t fs)
{
    unsigned mode,route,at,i;
    for(mode=1;mode<=4;mode++)for(route=0;route<2;route++) {
        fm_patch_t p=cascade_check_patch();
        fm_voice_t a,b;
        int32_t x[257],y[257];
        p.filter.mode=(uint16_t)mode;
        p.filter.q_milli=16000;
        p.op[0].free_run=2;
        p.lfo.free_run=2;
        p.lfo.rate_millihz=7000;
        p.lfo.pitch_cents=100;
        p.lfo.mask_pitch=3;
        if(route) {
            p.route.ops=3;
            p.route.carriers=5;
            p.route.kind[1][0]=1;
            p.route.w[1][0]=256;
        }
        cascade_check_init(&a,&p,fs);
        b=a;
        for(at=0;at<9000;at+=257) {
            if(at==257) {
                fm_voice_note_on(&a,220000,32767);
                fm_voice_note_on(&b,220000,32767);
            }
            if(at==257*10) {
                fm_voice_note_off(&a);
                fm_voice_note_off(&b);
            }
            if(at==257*25) {
                fm_voice_note_on(&a,330000,20000);
                fm_voice_note_on(&b,330000,20000);
            }
            fm_render_q30(&a,x,257);
            for(i=0;i<257;i++)fm_render_q30(&b,y+i,1);
            BOUND_CHECK(&ctx->bounds, !memcmp(x,y,sizeof x)&&!memcmp(&a,&b,sizeof a));
        }
    }
}

/*  public-renderer diagnostic. The caller-owned countdown is prepared
 * at one so each one-sample call reaches exactly one periodic tick. */
static void matrix_check_tick(fm_voice_t *v)
{
    int32_t out;
    v->ctl = 1u;
    fm_render_q30(v, &out, 1u);
}
static int32_t matrix_check_smooth(int32_t x, int32_t t, unsigned shift)
{
    if (!shift) return t;
    return t > x ? x + (int32_t)ceil((double)(t-x)/(1u<<shift))
                 : x - (int32_t)ceil((double)(x-t)/(1u<<shift));
}
static fm_patch_t matrix_check_patch(void)
{
    fm_patch_t p = { 0 };
    unsigned i;
    for (i=0;i<FM_OPS;i++) {
        p.op[i].ratio_q16=(i+1u)*65536u;
        p.op[i].attack_ms=2;p.op[i].decay_ms=100;p.op[i].release_ms=1;
        p.op[i].level_db10=120;
    }
    p.op[1].index_q10=1024;
    return p;
}
static unsigned matrix_bounds(uint32_t fs)
{
    bound_t bounds = { "MODBOUND-FAIL", 0, 0 };
        uint32_t rng=0x12345678u;
    unsigned shift,n,i,src,target,index,sign;
    for(shift=0;shift<=15;shift++) {
        fm_patch_t p=matrix_check_patch();fm_voice_t v;
        p.mod[0]=(fm_mod_slot_patch_t){1200,16,2,0,(uint8_t)shift,{0,0,0}};
        memset(&v,0,sizeof v);fm_voice_init(&v,&p,fs);fm_voice_note_on(&v,220000,32767);
        for(n=0;n<10000;n++) {
            int32_t x,t,w;
            rng^=rng<<13;rng^=rng>>17;rng^=rng<<5;x=(int32_t)(rng%65537u)-32768;
            rng^=rng<<13;rng^=rng>>17;rng^=rng<<5;t=(int32_t)(rng%65537u)-32768;
            w=matrix_check_smooth(x,t,shift);v.mod_cur[0]=x;fm_voice_ctl(&v,0,t);matrix_check_tick(&v);
            BOUND_CHECK(&bounds, v.mod_cur[0]==w);
            BOUND_CHECK(&bounds, v.mod_level[0]==(int32_t)((int64_t)w*1200/32768));
            BOUND_CHECK(&bounds, w >= (x<t?x:t) && w <= (x>t?x:t));
            v.mod_cur[0]=-x;fm_voice_ctl(&v,0,-t);matrix_check_tick(&v);BOUND_CHECK(&bounds, v.mod_cur[0]==-w);
        }
        for(sign=0;sign<2;sign++) {
            int32_t t=sign?32768:-32768;unsigned steps=0;
            v.mod_cur[0]=0;fm_voice_ctl(&v,0,t);
            while(v.mod_cur[0]!=t&&steps<400000u) {
                int32_t x=v.mod_cur[0];matrix_check_tick(&v);
                BOUND_CHECK(&bounds, v.mod_cur[0]==matrix_check_smooth(x,t,shift));steps++;
            }
            BOUND_CHECK(&bounds, v.mod_cur[0]==t);
        }
    }
    {
        const unsigned sources[]={0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,64,65,66,67,68,69,70,71,65535};
        const unsigned targets[]={0,1,2,3,4,5,6,7,8,9,10,32,33,34,35,36,37,255};
        for(src=0;src<sizeof sources/sizeof sources[0];src++)
        for(target=0;target<sizeof targets/sizeof targets[0];target++)
        for(index=0;index<7;index++)for(sign=0;sign<2;sign++) {
            fm_patch_t p=matrix_check_patch();fm_voice_t v;ref_voice_t r;
            unsigned op=index==6?255:index,s=sources[src],t=targets[target];int valid;
            p.route.ops=FM_OPS;p.route.carriers=5;p.route.w[1][0]=256;
            p.lfo.rate_millihz=7000;p.peg.level_cents[0]=1;p.filter.mode=1;p.fenv.depth_cents=1;
            p.mod[0]=(fm_mod_slot_patch_t){sign?INT32_MAX:INT32_MIN,(uint16_t)s,(uint8_t)t,(uint8_t)op,255,{255,255,255}};
            memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
            valid=s<=2||(s>=3&&s<3+FM_OPS)||s==7||s==8||(s>=16&&s<=23)||(s>=64&&s<=71);
            if(t==1)valid=valid&&op<FM_OPS;
            else if(t==2)valid=valid&&op<FM_OPS&&((5u>>op)&1u);
            else if(t==3)valid=valid&&op==1;
            else if(t>=4&&t<=9)valid=valid&&op==0;
            else valid=0;
            BOUND_CHECK(&bounds, v.mod_active==(valid?1u:0u)&&v.mod_active==r.mod_active);
            {
                unsigned slot;
                int same = 1;
                for (slot = 0; slot < FM_MOD_SLOTS; ++slot) {
                    const fm_mod_slot_patch_t *a = &v.patch.mod[slot], *b = &r.patch.mod[slot];
                    if (a->amount != b->amount || a->source != b->source || a->target != b->target
                        || a->target_index != b->target_index || a->shift != b->shift
                        || a->reserved[0] != b->reserved[0] || a->reserved[1] != b->reserved[1]
                        || a->reserved[2] != b->reserved[2]) same = 0;
                }
                BOUND_CHECK(&bounds, same);
            }
            BOUND_CHECK(&bounds, v.patch.mod[0].shift==15&&v.patch.mod[0].reserved[0]==0&&v.patch.mod[0].reserved[1]==0&&v.patch.mod[0].reserved[2]==0);
        }
    }
    for(src=3;src<3+FM_OPS;src++)for(n=0;n<=FM_STAGE_DECAY2;n++) {
        fm_patch_t p=matrix_check_patch();fm_voice_t v;fm_op_t old;unsigned op=src-3;
        uint32_t gain;int32_t expected;
        p.route.ops=FM_OPS;p.route.carriers=(uint8_t)((1u<<FM_OPS)-1u);
        p.mod[0]=(fm_mod_slot_patch_t){120,(uint16_t)src,2,0,0,{0,0,0}};
        memset(&v,0,sizeof v);fm_voice_init(&v,&p,fs);fm_voice_note_on(&v,220000,32767);
        v.op[op].stage=n;v.op[op].count=2;v.op[op].gain=UINT64_C(123456789)<<32;v.op[op].rate_g=0;
        v.op[op].att=(uint64_t)3<<(FM_ENV64?56:24);v.op[op].sus2=v.op[op].att;
        v.op[op].rate_d=v.op[op].rate_d2=v.op[op].rate_r=0;v.op[op].inc=0;
        old=v.op[op];gain=n==FM_STAGE_IDLE||n==FM_STAGE_DELAY?0:n==FM_STAGE_ATTACK?123456789u:134217728u;
        expected=(int32_t)((gain+16384u)>>15);matrix_check_tick(&v);
        BOUND_CHECK(&bounds, v.mod_cur[0]==expected);
        BOUND_CHECK(&bounds, v.op[op].stage==old.stage&&v.op[op].gain==old.gain&&v.op[op].att==old.att&&v.op[op].phase==old.phase&&v.op[op].noise==old.noise);
        BOUND_CHECK(&bounds, v.op[op].count==(n==FM_STAGE_ATTACK||n==FM_STAGE_DECAY||n==FM_STAGE_DECAY2||n==FM_STAGE_DELAY?1u:2u));
    }
    {
        fm_patch_t p=matrix_check_patch();fm_voice_t a,b;int32_t out[64];
        p.mod[0]=(fm_mod_slot_patch_t){1200,16,1,0,0,{0,0,0}};
        memset(&a,0,sizeof a);fm_voice_init(&a,&p,fs);b=a;
        fm_voice_ctl(&a,0,32768);fm_voice_note_on(&a,220000,32767);
        fm_voice_note_on(&b,220000,32767);fm_voice_ctl(&b,0,32768);
        BOUND_CHECK(&bounds, a.mod_cur[0]==32768&&b.mod_cur[0]==0);
        fm_render_q30(&b,out,63);BOUND_CHECK(&bounds, b.mod_cur[0]==0);fm_render_q30(&b,out,1);BOUND_CHECK(&bounds, b.mod_cur[0]==32768);
        p.mod[0].amount=0;fm_voice_init(&a,&p,fs);fm_voice_note_on(&a,220000,32767);BOUND_CHECK(&bounds, !a.mod_active&&!a.ctl);
        for(i=0;i<8;i++)p.mod[i]=(fm_mod_slot_patch_t){i<4?4800:-4800,16,1,0,0,{0,0,0}};
        fm_voice_init(&a,&p,fs);fm_voice_ctl(&a,0,32768);fm_voice_note_on(&a,220000,32767);BOUND_CHECK(&bounds, a.mod_pitch[0]==0);
    }
    printf("MODBOUND fs=%u phase64=%d checks=%lu failures=%lu\n", fs, FM_PHASE64, (unsigned long)bounds.checks, (unsigned long)bounds.bad);
    return (unsigned)bounds.bad;
}

/* An inert ctl0 slot selects the matrix tick without changing its input.
 * Compare every existing control fixture whose authored slots are zero. */
static unsigned inert_slot_bounds(uint32_t fs)
{
    unsigned cases = 0, bad = 0;
    size_t ci;
    for (ci = 0; ci < NCASE; ci++) {
        const spike_case_t *c = &CASES[ci];
        const fm_patch_t *p = &c->patch;
        spike_case_t twin;
        fm_voice_t v;
        int16_t *a, *b;
        uint32_t n, first;
        uint64_t ha, hb;
        unsigned i, carrier;
        int control = p->lfo.rate_millihz != 0u || p->fenv.depth_cents != 0 || p->fenv.track_permille != 0;
        for (i = 0; i < 4u; i++) if (p->peg.level_cents[i]) control = 1;
        if (!control) continue;
        for (i = 0; i < FM_MOD_SLOTS && !p->mod[i].amount; i++) {}
        if (i != FM_MOD_SLOTS) continue;
        cases++;
        case_voice_init(&v, c, fs);
        for (carrier = 0; carrier < FM_OPS && !((v.carriers >> carrier) & 1u); carrier++) {}
        if (carrier == FM_OPS) {
            printf("INERTSLOT-FAIL case=%s fs=%u reason=no-carrier\n", c->name, fs);
            bad++; continue;
        }
        twin = *c;
        twin.patch.mod[0] = (fm_mod_slot_patch_t){1, 16, 2, (uint8_t)carrier, 0, {0,0,0}};
        case_voice_init(&v, &twin, fs);
        if (!v.mod_active) {
            printf("INERTSLOT-FAIL case=%s fs=%u reason=inactive-slot\n", c->name, fs);
            bad++; continue;
        }
        n = (uint32_t)((uint64_t)c->len_ms * fs / 1000u);
        a = (int16_t *)alloc_n(n, sizeof *a);
        b = (int16_t *)alloc_n(n, sizeof *b);
        if (!a || !b) {
            printf("INERTSLOT-FAIL case=%s fs=%u reason=allocation\n", c->name, fs);
            free(a); free(b); return BOUNDS_HOST_ERROR;
        }
        render_a1_blocks(c, fs, a, n);
        render_a1_blocks(&twin, fs, b, n);
        ha = fm_golden_hash(a, n); hb = fm_golden_hash(b, n);
        if (ha != hb) {
            for (first = 0; first < n && a[first] == b[first]; first++) {}
            printf("INERTSLOT-FAIL case=%s fs=%u sample=%u baseline=%d inert=%d hashes=%016llx/%016llx\n",
                   c->name, fs, first, first < n ? (int)a[first] : 0, first < n ? (int)b[first] : 0,
                   (unsigned long long)ha, (unsigned long long)hb);
            bad++;
        }
        free(a); free(b);
    }
    printf("INERTSLOT fs=%u phase64=%d cases=%u failures=%u\n", fs, FM_PHASE64, cases, bad);
    return bad;
}

/* Compare logical state, never padding or the inactive noise-union arm. The
 * shared immutable patch was initialized from the same authored fixture. */
static int formant_state_equal(const fm_formant_t *a, const fm_formant_t *b)
{
    return a->center_millihz == b->center_millihz && a->level_db10 == b->level_db10
        && a->gain_q30 == b->gain_q30 && a->svf.a1 == b->svf.a1 && a->svf.a2 == b->svf.a2
        && a->svf.a3 == b->svf.a3 && a->svf.k == b->svf.k && a->svf.ic1 == b->svf.ic1
        && a->svf.ic2 == b->svf.ic2 && a->svf.mode == b->svf.mode && a->svf.idle == b->svf.idle;
}

static int mod_state_equal(const fm_voice_t *a, const fm_voice_t *b)
{
    unsigned i, j;
#define SAME(member) do { if (a->member != b->member) return 0; } while (0)
    SAME(vector_x); SAME(vector_y); SAME(formant_mask);
    for (i = 0; i < FM_FORMANTS; ++i) {
        SAME(mod_formant_center[i]); SAME(mod_formant_gain[i]);
        SAME(patch.formant[i].center_millihz); SAME(patch.formant[i].q_milli);
        SAME(patch.formant[i].level_db10); SAME(patch.formant[i].enabled);
        if (!formant_state_equal(&a->formant[i], &b->formant[i])) return 0;
    }
    SAME(sample_rate_hz); SAME(ops); SAME(carriers); SAME(ctl);
    SAME(lfo_phase); SAME(lfo_step); SAME(lfo_word); SAME(lfo_a); SAME(lfo_b);
    SAME(lfo_time); SAME(lfo_delay); SAME(lfo_fade); SAME(lfo_m); SAME(lfo_cents);
    SAME(peg_mask); SAME(fenv_ub); SAME(fenv_u); SAME(fenv_ulo); SAME(fenv_uhi);
    SAME(fenv_s); SAME(fenv_track); SAME(fenv_q); SAME(idle_samples); SAME(free_running);
    SAME(lfo_wrap); SAME(amp_edges); SAME(sync_edges); SAME(sync_wrap_prev); SAME(shape_gain_q24);
    SAME(mod_velocity); SAME(mod_note); SAME(mod_cutoff); SAME(mod_q);
    SAME(mod_active); SAME(mod_pitch_mask); SAME(mod_lfo_rate); SAME(mod_lfo_pitch);
    SAME(mod_lfo_level); SAME(mod_lfo_index); SAME(mod_filter_q);
#define SVF(member) SAME(member.a1); SAME(member.a2); SAME(member.a3); SAME(member.k); \
    SAME(member.ic1); SAME(member.ic2); SAME(member.mode); SAME(member.idle)
    SVF(svf); SVF(svf2);
#undef SVF
#define GLIDE(member) SAME(member.cur); SAME(member.q); SAME(member.r); SAME(member.acc); \
    SAME(member.n); SAME(member.left); SAME(member.dir); SAME(member.stage)
    GLIDE(peg); GLIDE(fenv);
#undef GLIDE
    for (i = 0; i < 4; ++i) SAME(peg_n[i]);
    for (i = 0; i < 3; ++i) SAME(fenv_n[i]);
    for (i = 0; i < FM_MOD_SLOTS; ++i) SAME(mod_cur[i]);
    for (i = 0; i < FM_CTL_INPUTS; ++i) SAME(ctl_input[i]);
    for (i = 0; i < FM_GEN_INPUTS; ++i) SAME(gen_input[i]);
    for (i = 0; i < FM_OPS; ++i) {
        SAME(lfo_vg[i]); SAME(lfo_ks[i]); SAME(lfo_base[i]); SAME(peg_base_frac[i]); SAME(peg_base_whole[i]);
        SAME(mod_pitch[i]); SAME(mod_level[i]); SAME(mod_index[i]); SAME(mod_pd[i]); SAME(mod_wt[i]);
        for (j = 0; j < FM_OPS; ++j) SAME(edge[i][j]);
#define OP(member) SAME(op[i].member)
        OP(phase); OP(inc); OP(gain); OP(rate_g); OP(att); OP(rate_d); OP(rate_r); OP(sus);
        OP(sus2); OP(rate_d2); OP(count); OP(count_d); OP(out); OP(index); OP(fb); OP(prev); OP(prev2);
        OP(pd_knee); OP(pd_slope[0]); OP(pd_slope[1]);
        OP(wt_pos); OP(wt_band); OP(wt_frac); OP(wt_a); OP(wt_b);
        OP(stage); OP(noise); OP(noise_index); OP(count_a_wait); OP(count_d2); OP(decay2_on);
        if (a->patch.op[i].wave == 9) {
            for (j = 0; j < 8; ++j) OP(noise_state.pink.row[j]);
            OP(noise_state.pink.sum); OP(noise_state.pink.counter);
        } else if (a->patch.op[i].wave == 10 || a->patch.op[i].wave == 11) OP(noise_state.colour);
#undef OP
    }
#undef SAME
    return 1;
}

/* /: activity is a host scheduling contract, not audibility. */
static const spike_case_t HOSTSKIP_AMP_CASE =
    { "hostskip_amp", { .op = {
        { .ratio_q16=65536, .vel_sens_q8=256, .attack_ms=5, .decay_ms=100, .release_ms=20 },
        { .ratio_q16=65536, .index_q10=2048, .vel_sens_q8=256, .attack_ms=5, .decay_ms=100, .release_ms=20 } },
        .amp = { 300, 10, 0, 3000, 1 } }, 3000,
      { { 0, EV_ON, 440000, 32767, 0 }, { 500, EV_OFF, 0, 0, 0 }, { 2000, EV_ON, 440000, 32767, 0 } } };

static int hostskip_amp_equal(const fm_voice_t *a, const fm_voice_t *b)
{
    return a->amp.gain == b->amp.gain && a->amp.rate_g == b->amp.rate_g
        && a->amp.att == b->amp.att && a->amp.rate_d == b->amp.rate_d
        && a->amp.rate_r == b->amp.rate_r && a->amp.sus == b->amp.sus
        && a->amp.count == b->amp.count && a->amp.count_d == b->amp.count_d
        && a->amp.stage == b->amp.stage;
}

/* Supplement mod_state_equal's explicit logical fields. Mutable storage
 * addresses are independent; compare binding shape and initialized contents.
 * Immutable patch/bank backing is shared. Never inspect padding or spare KS
 * capacity, nor an inactive noise-union arm. */
static int hostskip_state_equal(const fm_voice_t *a, const fm_voice_t *b)
{
    unsigned i, j;
    if (!mod_state_equal(a, b)) return 0;
    if (!hostskip_amp_equal(a, b)) return 0;
    if (a->fb_special != b->fb_special) return 0;
    for (i = 0; i < FM_WT_SLOTS; i++) {
        if (a->wt_data[i] != b->wt_data[i] || a->wt_frames[i] != b->wt_frames[i]
            || a->wt_length[i] != b->wt_length[i]) return 0;
    }
    for (i = 0; i < FM_OPS; i++) {
        const fm_op_t *x = &a->op[i], *y = &b->op[i];
#define HS_OP(member) do { if (x->member != y->member) return 0; } while (0)
        HS_OP(wt_bank); HS_OP(wt_on); HS_OP(wt_bits); HS_OP(wt_frames);
        HS_OP(raw_prev); HS_OP(raw_prev2); HS_OP(loop_g0); HS_OP(loop_rate); HS_OP(loop_count);
        HS_OP(ks_on); HS_OP(ks_mask); HS_OP(ks_w); HS_OP(ks_n); HS_OP(ks_f);
        HS_OP(ks_loss); HS_OP(ks_word); HS_OP(ks_prev); HS_OP(ks_inc);
#undef HS_OP
        if (a->ks_capacity[i] != b->ks_capacity[i] || !!a->ks_data[i] != !!b->ks_data[i]
            || !!x->ks_buf != !!y->ks_buf || a->ks_db10[i] != b->ks_db10[i]
            || a->ks_boost[i] != b->ks_boost[i]) return 0;
        if (x->ks_buf) {
            if (x->ks_buf != a->ks_data[i] || y->ks_buf != b->ks_data[i]
                || x->ks_mask >= a->ks_capacity[i]) return 0;
            for (j = 0; j <= x->ks_mask; j++) if (x->ks_buf[j] != y->ks_buf[j]) return 0;
        }
    }
    return 1;
}

static int hostskip_init(fm_voice_t *voice, const spike_case_t *c, uint32_t fs, int32_t *storage)
{
    const fm_assets_t *assets = case_assets(c);
    fm_assets_t local;
    fm_ks_view_t views[FM_OPS];
    unsigned i;
    fm_voice_init(voice, &c->patch, fs);
    if (!assets) return 0;
    local = *assets;
    if (local.ks_count) {
        for (i = 0; i < local.ks_count; i++) {
            views[i] = local.ks[i];
            if (views[i].data) {
                views[i].data = storage + (size_t)i * FM_KS_BUFFER_MAX;
                memset(views[i].data, 0, (size_t)views[i].capacity * sizeof *storage);
            }
        }
        local.ks = views;
    }
    return fm_voice_bind_assets(voice, &local) != FM_BIND_OK;
}

static unsigned hostskip_amp_contract(uint32_t fs)
{
    fm_voice_t voice, twin;
    uint32_t n, first = 0, ended = 0;
    unsigned failures = 0;
    fm_voice_init(&voice, &HOSTSKIP_AMP_CASE.patch, fs);
    fm_voice_note_on(&voice, 440000, 32767);
    for (n = 0; n < 5u * fs; n++) {
        int32_t y;
        if (n == fs / 2u) fm_voice_note_off(&voice);
        fm_render_q30(&voice, &y, 1);
        if (!first && voice.op[0].stage == FM_STAGE_IDLE && voice.amp.stage == FM_STAGE_RELEASE) {
            int32_t original_sample, changed_sample;
            fm_voice_t original = voice, changed = voice;
            first = n;
            failures += !fm_voice_active(&voice) || fm_voice_needs_clock(&voice);
            twin = voice;
            failures += fm_voice_bind_assets(&twin, NULL) != FM_BIND_ACTIVE || !hostskip_state_equal(&voice, &twin);
            changed.amp.att++;
            if (hostskip_state_equal(&original, &changed)) {
                printf("HOSTSKIP-STATE-CONTROL fs=%u sample=%u amp-only mutation missed\n", fs, n);
                failures++;
            }
            fm_render_q30(&original, &original_sample, 1);
            fm_render_q30(&changed, &changed_sample, 1);
            failures += original_sample != 0 || changed_sample != 0;
        }
        if (n > fs / 2u && voice.amp.stage == FM_STAGE_IDLE) {
            unsigned i;
            ended = n;
            failures += fm_voice_active(&voice) || fm_voice_bind_assets(&voice, NULL) != FM_BIND_OK;
            for (i = 0; i < FM_OPS; i++) failures += voice.op[i].stage != FM_STAGE_IDLE
                || voice.op[i].count != 0 || voice.op[i].count_a_wait != 0;
            failures += voice.svf.ic1 != 0 || voice.svf.ic2 != 0 || voice.svf2.ic1 != 0 || voice.svf2.ic2 != 0;
            for (i = 0; i < FM_FORMANTS; i++) failures += voice.formant[i].svf.ic1 != 0 || voice.formant[i].svf.ic2 != 0;
            twin = voice; twin.patch.amp.enabled = 0; twin.amp.stage = FM_STAGE_RELEASE;
            failures += fm_voice_active(&twin) || fm_voice_needs_clock(&twin);
            twin.free_running = 1;
            failures += fm_voice_active(&twin) || !fm_voice_needs_clock(&twin);
            break;
        }
    }
    failures += first == 0 || ended == 0;
    printf("HOSTSKIP-AMP fs=%u carrier_idle=%u amp_idle=%u state_control=%s failures=%u\n",
           fs, first, ended, first ? "checked" : "missing", failures);
    return failures;
}

/* Each real host route must advance the same voice lifecycle. Compare its
 * own PCM between partitions, never PCM from different spatial/FX routes. */
static unsigned hostskip_routes(uint32_t fs)
{
    static const char *names[] = { "plain", "fx_mono", "stereo", "field_amb1", "field_panlaw" };
    const uint32_t frames = 3u * fs;
    int16_t *first = (int16_t *)alloc_n((size_t)frames * 4u, sizeof *first);
    int16_t *second = (int16_t *)alloc_n((size_t)frames * 4u, sizeof *second);
    sp_field_t *field = (sp_field_t *)calloc(1, sizeof *field);
    fx_t *effects = (fx_t *)calloc(1, sizeof *effects);
    unsigned route, failures = 0;
    if (!first || !second || !field || !effects) {
        free(first); free(second); free(field); free(effects); return BOUNDS_HOST_ERROR;
    }
    for (route = 0; route < sizeof names / sizeof names[0]; route++) {
        const uint32_t channels = route == 3u ? 4u : route < 2u ? 1u : 2u;
        unsigned partition, state_diffs = 0, initialized = 1;
        uint32_t clipped[2] = { 0, 0 }, pcm_diffs = 0;
        for (partition = 0; partition < 2; partition++) {
            fm_voice_t voice, oracle;
            int16_t *pcm = partition ? second : first;
            uint32_t pos = 0, seed = 19;
            int event = 0;
            fx_cfg_t cfg = { 0 };
            sp_field_cfg_t field_cfg = { 0 };
            fm_voice_init(&voice, &HOSTSKIP_AMP_CASE.patch, fs);
            fm_voice_init(&oracle, &HOSTSKIP_AMP_CASE.patch, fs);
            cfg.on = FX_ON_GAIN; cfg.channels = 1; cfg.block = 64; cfg.gain_db10 = 0;
            field_cfg.route = route == 3u ? SP_FIELD_AMB1 : SP_FIELD_PANLAW;
            field_cfg.block = 64; field_cfg.near_mm = 100; field_cfg.fs = fs; field_cfg.panlaw_q15 = 32768;
            if (fx_init(effects, fs, &cfg, NULL, 0) || sp_stereo_init(&field->geo, 64, 100)
                || (route >= 3u && sp_field_init(field, &field_cfg))) {
                printf("HOSTSKIP-ROUTE-FAIL fs=%u route=%s initialization refused\n", fs, names[route]);
                failures++; initialized = 0; break;
            }
            while (pos < frames) {
                int32_t discarded[257];
                uint32_t count = 1, next = frames;
                while (event < ev_count(&HOSTSKIP_AMP_CASE)
                       && ev_sample(&HOSTSKIP_AMP_CASE.ev[event], fs, frames) == pos) {
                    a1_event(&voice, &HOSTSKIP_AMP_CASE.ev[event]);
                    a1_event(&oracle, &HOSTSKIP_AMP_CASE.ev[event++]);
                }
                if (event < ev_count(&HOSTSKIP_AMP_CASE)) next = ev_sample(&HOSTSKIP_AMP_CASE.ev[event], fs, frames);
                if (partition) { seed = seed * UINT32_C(1664525) + UINT32_C(1013904223); count = 1u + seed % 257u; }
                if (count > next - pos) count = next - pos;
                fm_render_q30(&oracle, discarded, count);
                if (route == 0u) clipped[partition] += fm_mix_i16(&voice, 1, pcm + pos, count);
                else if (route == 1u) clipped[partition] += fx_render_mono(effects, &voice, 1, pcm + pos, count);
                else if (route == 2u) clipped[partition] += sp_stereo_render(&field->geo, &voice, 1, pcm + (size_t)pos * channels, count);
                else clipped[partition] += sp_field_render(field, &voice, 1, pcm + (size_t)pos * channels, count);
                if (!hostskip_state_equal(&voice, &oracle)) state_diffs++;
                pos += count;
            }
        }
        if (!initialized) continue;
        { uint32_t i; for (i = 0; i < frames * channels; i++) pcm_diffs += first[i] != second[i]; }
        failures += pcm_diffs != 0 || state_diffs != 0 || clipped[0] != clipped[1];
        printf("HOSTSKIP-ROUTE fs=%u route=%s frames=%u pcm_diffs=%u state_diffs=%u clipped=%u/%u\n",
               fs, names[route], frames, pcm_diffs, state_diffs, clipped[0], clipped[1]);
    }
    free(first); free(second); free(field); free(effects);
    return failures;
}

static unsigned hostskip_check(uint32_t fs)
{
    int32_t *storage = (int32_t *)calloc((size_t)2u * FM_OPS * FM_KS_BUFFER_MAX, sizeof *storage);
    unsigned failures = 0, state_diffs = 0, cases = 0;
    uint64_t diffs = 0;
    size_t ci;
    if (!storage) return BOUNDS_HOST_ERROR;
    for (ci = 0; ci <= NCASE; ci++) {
        const spike_case_t *c = ci == NCASE ? &HOSTSKIP_AMP_CASE : &CASES[ci];
        const uint32_t frames = (uint32_t)((uint64_t)c->len_ms * fs / 1000u);
        fm_voice_t always, skipped;
        uint32_t n, count = 0, first_diff = frames;
        unsigned state;
        int event = 0, ne = ev_count(c);
        if (hostskip_init(&always, c, fs, storage)
            || hostskip_init(&skipped, c, fs, storage + (size_t)FM_OPS * FM_KS_BUFFER_MAX)) {
            printf("HOSTSKIP-FAIL case=%s fs=%u asset initialization refused\n", c->name, fs);
            failures++; continue;
        }
        { unsigned i; for (i = 0; i < FM_OPS; i++)
            if (always.ks_data[i] && always.ks_data[i] == skipped.ks_data[i]) failures++; }
        for (n = 0; n < frames; n++) {
            int32_t a, b = 0;
            while (event < ne && ev_sample(&c->ev[event], fs, frames) == n) {
                a1_event(&always, &c->ev[event]); a1_event(&skipped, &c->ev[event++]);
            }
            fm_render_q30(&always, &a, 1);
            if (fm_voice_active(&skipped) || fm_voice_needs_clock(&skipped)) fm_render_q30(&skipped, &b, 1);
            if (a != b) { if (!count) first_diff = n; count++; }
        }
        state = !hostskip_state_equal(&always, &skipped);
        if (count || state) {
            printf("HOSTSKIP-FAIL case=%s fs=%u diffs=%u first=%u state_diffs=%u\n", c->name, fs, count, first_diff, state);
            failures++;
        }
        diffs += count; state_diffs += state; cases++;
    }
    free(storage);
    failures += hostskip_amp_contract(fs);
    { const unsigned route_failures = hostskip_routes(fs);
      if (route_failures == BOUNDS_HOST_ERROR) return BOUNDS_HOST_ERROR;
      failures += route_failures; }
    printf("HOSTSKIP fs=%u cases=%u diffs=%llu state_diffs=%u failures=%u\n",
           fs, cases, (unsigned long long)diffs, state_diffs, failures);
    return failures;
}


/* Controller writes occur before the named sample and only latch. Exercise
 * the samples around an established tick without forcing its countdown. */
static unsigned mod_event_bounds(uint32_t fs)
{
    bound_t bounds = { "MODEVENT-FAIL", 0, 0 };
    fm_patch_t p = { 0 };
    unsigned at, i;
    int32_t out[257];
    p.op[0].ratio_q16 = 65536; p.op[1].ratio_q16 = 131072;
    p.op[0].release_ms = 1;
    p.mod[0] = (fm_mod_slot_patch_t){ 1200, 16, 1, 0, 0, { 0, 0, 0 } };
    for (at = FM_CTL - 1; at <= FM_CTL + 1; ++at) {
        fm_voice_t v;
        const unsigned wait = FM_CTL - at % FM_CTL;
        memset(&v, 0, sizeof v); fm_voice_init(&v, &p, fs);
        fm_voice_note_on(&v, 220000, 32767);
        fm_render_q30(&v, out, at);
        fm_voice_ctl(&v, 0, 32768); fm_voice_ctl(&v, 0, -32768);
        BOUND_CHECK(&bounds, v.mod_cur[0] == 0 && v.mod_pitch[0] == 0);
        fm_render_q30(&v, out, wait - 1);
        BOUND_CHECK(&bounds, v.mod_cur[0] == 0 && v.ctl == 1);
        fm_render_q30(&v, out, 1);
        BOUND_CHECK(&bounds, v.mod_cur[0] == -32768 && v.mod_pitch[0] == -1200 && v.ctl == FM_CTL);
        for (i = 0; i < FM_CTL_INPUTS; ++i) {
            fm_voice_ctl(&v, i, INT32_MIN); BOUND_CHECK(&bounds, v.ctl_input[i] == -32768);
            fm_voice_ctl(&v, i, INT32_MAX); BOUND_CHECK(&bounds, v.ctl_input[i] == 32768);
        }
        {
            const fm_voice_t before = v;
            fm_voice_ctl(&v, FM_CTL_INPUTS, 100); fm_voice_ctl(&v, UINT32_MAX, -100);
            BOUND_CHECK(&bounds, mod_state_equal(&v, &before));
        }
    }
    for (i = 0; i < 2; ++i) {
        fm_voice_t v;
        int32_t held;
        unsigned guard = 0;
        p.mod[0].shift = 3;
        p.op[0].free_run = i ? 2 : 0;
        p.lfo.rate_millihz = i ? 3000 : 0; p.lfo.free_run = i ? 2 : 0;
        memset(&v, 0, sizeof v); fm_voice_init(&v, &p, fs);
        fm_voice_ctl(&v, 0, 32768); fm_render_q30(&v, out, 193);
        BOUND_CHECK(&bounds, !fm_voice_active(&v) && !v.mod_cur[0] && v.ctl_input[0] == 32768);
        fm_voice_note_on(&v, 220000, 32767);
        BOUND_CHECK(&bounds, v.mod_cur[0] == 0);
        fm_render_q30(&v, out, FM_CTL);
        BOUND_CHECK(&bounds, v.mod_cur[0] == 4096);
        held = v.mod_cur[0]; fm_voice_note_on(&v, 330000, 24000);
        BOUND_CHECK(&bounds, v.mod_cur[0] == held);
        fm_voice_note_off(&v);
        while (fm_voice_active(&v) && ++guard < fs) fm_render_q30(&v, out, 1);
        BOUND_CHECK(&bounds, !fm_voice_active(&v));
        held = v.mod_cur[0]; fm_voice_ctl(&v, 0, -32768);
        fm_render_q30(&v, out, 257);
        BOUND_CHECK(&bounds, v.mod_cur[0] == held && v.ctl_input[0] == -32768);
        fm_voice_note_on(&v, 440000, 32767);
        BOUND_CHECK(&bounds, v.mod_cur[0] == 0 && v.ctl_input[0] == -32768);
    }
    /* Previous effective LFO parameters drive this tick; new destinations
     * are published for the following tick, for keyed and FREE2 clocks. */
    for (i = 0; i < 2; ++i) {
        fm_voice_t v;
        uint32_t phase, step;
        const uint64_t den = (uint64_t)fs * 1000;
        memset(&p, 0, sizeof p);
        p.op[0].ratio_q16 = 65536; p.op[1].ratio_q16 = 131072;
        p.lfo.rate_millihz = 3000; p.lfo.wave = 4; p.lfo.free_run = i ? 2 : 0;
        p.lfo.pitch_cents = 20; p.lfo.mask_pitch = 1;
        p.mod[0] = (fm_mod_slot_patch_t){ 2000, 16, 6, 0, 0, { 0, 0, 0 } };
        p.mod[1] = (fm_mod_slot_patch_t){ 80, 17, 7, 0, 0, { 0, 0, 0 } };
        memset(&v, 0, sizeof v); fm_voice_init(&v, &p, fs);
        fm_voice_ctl(&v, 0, 32768); fm_voice_ctl(&v, 1, 32768);
        fm_voice_note_on(&v, 220000, 32767);
        BOUND_CHECK(&bounds, v.lfo_cents == 19 && v.mod_lfo_pitch == 100 && v.mod_lfo_rate == 5000);
        step = (uint32_t)((((uint64_t)3000 * FM_CTL << 32) + den / 2) / den);
        BOUND_CHECK(&bounds, v.lfo_phase == (i ? 0 : step)); phase = v.lfo_phase;
        step = (uint32_t)((((uint64_t)5000 * FM_CTL << 32) + den / 2) / den);
        for (at = 0; at < 4; ++at) {
            fm_render_q30(&v, out, FM_CTL); phase += step;
            BOUND_CHECK(&bounds, v.lfo_phase == phase && v.lfo_cents == 99);
        }
        fm_voice_note_on(&v, 330000, 32767);
        BOUND_CHECK(&bounds, v.lfo_cents == 19 && v.mod_lfo_pitch == 100);
        fm_render_q30(&v, out, FM_CTL);
        BOUND_CHECK(&bounds, v.lfo_cents == 99);
    }
    printf("MODEVENT fs=%u phase64=%d checks=%u failures=%u\n", fs, FM_PHASE64, bounds.checks, bounds.bad);
    return bounds.bad;
}

static unsigned mod_partition_bounds(uint32_t fs)
{
    static const unsigned sizes[] = { 1, 7, 64, 257, 0 };
    unsigned ci, bi, checks = 0, bad = 0;
    for (ci = 0; ci < NCASE; ++ci) if (!strncmp(CASES[ci].name, "mod", 3)) {
        const spike_case_t *c = &CASES[ci];
        const uint32_t total = c->len_ms * fs / 1000;
        for (bi = 0; bi < sizeof sizes / sizeof sizes[0]; ++bi) {
            fm_voice_t one, block;
            uint32_t pos = 0, rng = 0x91372769u;
            int e = 0, ne = ev_count(c);
            memset(&one, 0, sizeof one); memset(&block, 0, sizeof block);
            fm_voice_init(&one, &c->patch, fs); fm_voice_init(&block, &c->patch, fs);
            while (pos < total) {
                int32_t x[257], y[257];
                unsigned j, count;
                while (e < ne && ev_sample(&c->ev[e], fs, total) == pos) {
                    a1_event(&one, &c->ev[e]); a1_event(&block, &c->ev[e++]);
                    ++checks; if (!mod_state_equal(&one, &block)) ++bad;
                }
                rng = rng * 1664525u + 1013904223u;
                count = sizes[bi] ? sizes[bi] : 1 + rng % 257;
                if (count > total - pos) count = total - pos;
                if (e < ne && count > ev_sample(&c->ev[e], fs, total) - pos)
                    count = ev_sample(&c->ev[e], fs, total) - pos;
                fm_render_q30(&block, y, count);
                for (j = 0; j < count; ++j) fm_render_q30(&one, x + j, 1);
                ++checks; if (memcmp(x, y, count * sizeof *x)) ++bad;
                ++checks; if (!mod_state_equal(&one, &block)) ++bad;
                pos += count;
            }
        }
    }
    printf("MODPARTITION fs=%u phase64=%d checks=%u failures=%u blocks=1,7,64,257,seeded\n", fs, FM_PHASE64, checks, bad);
    return bad;
}

/* Logical reference state for callback equivalence, excluding immutable patch
 * data and all padding. Floating members must match exactly on the two runs. */
static int wide_ref_state_equal(const ref_voice_t *a, const ref_voice_t *b)
{
    unsigned i, j;
#define RS(member) do { if (a->member != b->member) return 0; } while (0)
    RS(vector_x); RS(vector_y); RS(formant_mask);
    for (i = 0; i < FM_FORMANTS; ++i) {
        RS(mod_formant_center[i]); RS(mod_formant_gain[i]);
        RS(patch.formant[i].center_millihz); RS(patch.formant[i].q_milli);
        RS(patch.formant[i].level_db10); RS(patch.formant[i].enabled);
        RS(formant[i].a1); RS(formant[i].a2); RS(formant[i].a3); RS(formant[i].ic1); RS(formant[i].ic2);
        RS(formant[i].gain); RS(formant[i].center_millihz); RS(formant[i].level_db10); RS(formant[i].idle);
    }
    RS(fs); RS(fs_hz); RS(fa1); RS(fa2); RS(fa3); RS(fk); RS(fic1); RS(fic2);
    RS(fmode); RS(ops); RS(carriers); RS(ctl); RS(lfo_phase); RS(lfo_step); RS(lfo_word);
    RS(lfo_time); RS(lfo_delay); RS(lfo_fade); RS(lfo_a); RS(lfo_b); RS(lfo_cents); RS(freq_millihz);
    RS(peg_start); RS(peg_target); RS(peg_cur); RS(peg_n); RS(peg_j); RS(peg_stage); RS(peg_mask);
    RS(fe_start); RS(fe_target); RS(fe_e); RS(fe_n); RS(fe_j); RS(fe_stage); RS(fe_s); RS(fe_track);
    RS(fe_q); RS(fe_base); RS(idle_samples); RS(free_running); RS(lfo_wrap); RS(amp_edges);
    RS(sync_edges); RS(sync_wrap_prev);
    RS(shape_gain); RS(f2ic1); RS(f2ic2); RS(f2mode); RS(filter_idle);
    RS(mod_velocity); RS(mod_note); RS(mod_cutoff); RS(mod_q); RS(mod_active); RS(mod_pitch_mask);
    RS(mod_lfo_rate); RS(mod_lfo_pitch); RS(mod_lfo_level); RS(mod_lfo_index);
    for (i = 0; i < FM_MOD_SLOTS; ++i) RS(mod_cur[i]);
    for (i = 0; i < FM_CTL_INPUTS; ++i) RS(ctl_input[i]);
    for (i = 0; i < FM_GEN_INPUTS; ++i) RS(gen_input[i]);
    for (i = 0; i < FM_OPS; ++i) {
        RS(vg[i]); RS(ks[i]); RS(mod_pitch[i]); RS(mod_level[i]); RS(mod_index[i]); RS(mod_pd[i]); RS(mod_wt[i]);
        for (j = 0; j < FM_OPS; ++j) RS(edge[i][j]);
#define RO(member) RS(op[i].member)
        RO(pd_knee); RO(wt_pos); RO(inc); RO(phase); RO(gain); RO(rate_g); RO(att); RO(rate_d); RO(rate_r); RO(sus);
        RO(out); RO(index); RO(fb); RO(prev); RO(prev2); RO(stage); RO(count); RO(count_d);
        RO(count_a); RO(rate_attack_kept); RO(iphase); RO(iinc); RO(noise); RO(noise_index);
        RO(count_a_wait); RO(count_d2); RO(sus2); RO(rate_d2); RO(decay2_on);
        for (j = 0; j < 8; ++j) RO(pink_rows[j]);
        RO(pink_sum); RO(pink_counter); RO(colour);
#undef RO
    }
#undef RS
    return 1;
}

/* Public renderer plus independent integer/rational PD oracle. */
typedef struct { bound_t bounds; double maxphase,maxsine; } pd_check_stats;
static uint32_t pd_check_draw(uint32_t*r){*r^=*r<<13;*r^=*r>>17;*r^=*r<<5;return*r;}
static fm_patch_t pd_check_patch(void){fm_patch_t p={0};unsigned i;for(i=0;i<FM_OPS;i++){p.op[i].ratio_q16=(i+1)*65536;p.op[i].attack_ms=5;p.op[i].decay_ms=100;p.op[i].release_ms=1;p.op[i].level_db10=180;}return p;}
static void pd_check_maps(pd_check_stats *stats,uint32_t fs)
{
    uint32_t knee,rng=0x5044;const double pi=acos(-1.0),table=(1u<<30)*(2*pi/(1u<<FM_SINE_BITS))*(2*pi/(1u<<FM_SINE_BITS))/8+1.5;
    fm_patch_t p=pd_check_patch();fm_voice_t base,plain;p.pd[0].enabled=1;p.op[1].wave=7;
    for(knee=1;knee<32768;knee++){
        uint32_t K=knee<<17,pins[22];unsigned j;p.pd[0].knee_q15=(uint16_t)knee;
        memset(&base,0,sizeof base);fm_voice_init(&base,&p,fs);p.pd[0].enabled=0;
        memset(&plain,0,sizeof plain);fm_voice_init(&plain,&p,fs);p.pd[0].enabled=1;
        for(j=0;j<2;j++){base.op[j].stage=plain.op[j].stage=FM_STAGE_SUSTAIN;base.op[j].att=plain.op[j].att=0;base.op[j].inc=plain.op[j].inc=0;}
        base.op[0].out=plain.op[0].out=1073741824;base.op[1].phase=plain.op[1].phase=0;
        pins[0]=0;pins[1]=1;pins[2]=K-1;pins[3]=K;pins[4]=K+1;pins[5]=UINT32_MAX;
        for(j=6;j<22;j++)pins[j]=pd_check_draw(&rng);
        BOUND_CHECK(&stats->bounds, base.op[0].pd_slope[0]==(UINT64_C(1)<<62)/K&&base.op[0].pd_slope[1]==(UINT64_C(1)<<62)/((UINT64_C(1)<<32)-K));
        for(j=0;j<22;j++){
            fm_voice_t v=base,w=plain;const uint32_t p32=pins[j],upper=p32>=K,d=upper?p32-K:p32;
            const uint64_t den=upper?(UINT64_C(1)<<32)-K:K;
            /* D<den makes this host oracle's full product <2^62. */
            const uint64_t local=((uint64_t)d*((UINT64_C(1)<<62)/den))>>31;
            const uint32_t warped=knee==16384?p32:(upper?0x80000000u:0u)+(uint32_t)local;
            const uint32_t pm=pd_check_draw(&rng)&0xffffff00u;
            const uint64_t raw=((uint64_t)(p32-pm)<<32)|pd_check_draw(&rng);
            const double real=(upper?.5:0)+.5*(double)d/den,ideal=sin(2*pi*real)*(1u<<30);
            int32_t sample,expected;uint64_t original;
#if FM_PHASE64
            v.op[0].phase=raw;w.op[0].phase=(uint64_t)warped<<32;
#else
            v.op[0].phase=p32-pm;w.op[0].phase=warped;(void)raw;
#endif
            original=v.op[0].phase;v.op[1].index=(int32_t)(pm>>8);w.op[1].index=0;
            fm_render_q30(&v,&sample,1);fm_render_q30(&w,&expected,1);
            BOUND_CHECK(&stats->bounds, sample==expected&&v.op[0].phase==original);
            BOUND_CHECK(&stats->bounds, fabs(sample-ideal)<=table+3*pi/2);
            if(fabs(sample-ideal)>stats->maxsine)stats->maxsine=fabs(sample-ideal);
        }
    }
}
static void pd_check_raw(pd_check_stats *stats,uint32_t fs){(void)fs;const unsigned knees[]={0,1,16384,32767,65535},waves[]={0,1,4,7,9,11,12,65535},rates[]={0,1,44100,48000,UINT32_MAX};unsigned a,b,c,e,i;
 for(a=0;a<5;a++)for(b=0;b<8;b++)for(c=0;c<5;c++)for(e=0;e<2;e++){
  fm_patch_t p=pd_check_patch();fm_voice_t v;ref_voice_t r;uint32_t k;
  p.op[0].wave=(uint16_t)waves[b];p.pd[0]=(fm_pd_patch_t){(uint16_t)knees[a],(uint16_t)(e?65535:0)};
  p.mod[0]=(fm_mod_slot_patch_t){INT32_MAX,16,33,0,0,{0,0,0}};
  memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,rates[c]);ref_voice_init(&r,&p,rates[c]);
  k=e&&waves[b]==0?(knees[a]<1?1:knees[a]>32767?32767:knees[a]):0;
  BOUND_CHECK(&stats->bounds, v.op[0].pd_knee==k&&r.op[0].pd_knee==k);BOUND_CHECK(&stats->bounds, v.patch.pd[0].enabled==(k!=0)&&v.patch.pd[0].knee_q15==k);
  BOUND_CHECK(&stats->bounds, v.mod_active==(k?1u:0u)&&v.mod_active==r.mod_active);BOUND_CHECK(&stats->bounds, v.patch.mod[0].amount==(k?32767:0));
  for(i=1;i<FM_OPS;i++)BOUND_CHECK(&stats->bounds, !v.op[i].pd_knee&&!r.op[i].pd_knee);
 }
}
static void pd_check_matrix(pd_check_stats *stats,uint32_t fs){fm_patch_t p=pd_check_patch();fm_voice_t v;ref_voice_t r;unsigned i,n;int32_t out[64];double rout[64];
 p.pd[0]=(fm_pd_patch_t){16384,1};
 for(i=0;i<8;i++)p.mod[i]=(fm_mod_slot_patch_t){i<4?32767:-32767,16,33,0,0,{0,0,0}};
 memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);fm_voice_ctl(&v,0,32768);ref_voice_ctl(&r,0,32768);fm_voice_note_on(&v,220000,32767);ref_voice_note_on(&r,220000,32767);
 BOUND_CHECK(&stats->bounds, v.mod_pd[0]==0&&r.mod_pd[0]==0&&v.op[0].pd_knee==16384&&r.op[0].pd_knee==16384);
 v.op[0].pd_slope[0]=123;fm_render_q30(&v,out,64);BOUND_CHECK(&stats->bounds, v.op[0].pd_slope[0]==123);v.op[0].pd_slope[0]=UINT64_C(1)<<31;
 memset(p.mod,0,sizeof p.mod);p.mod[0]=(fm_mod_slot_patch_t){32767,16,33,0,3,{0,0,0}};
 fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);fm_voice_ctl(&v,0,-32768);ref_voice_ctl(&r,0,-32768);fm_voice_note_on(&v,220000,32767);ref_voice_note_on(&r,220000,32767);
 BOUND_CHECK(&stats->bounds, v.mod_cur[0]==0&&v.op[0].pd_knee==16384);
 for(n=0;n<400;n++){
  if(n==80){fm_voice_ctl(&v,0,32768);ref_voice_ctl(&r,0,32768);}
  if(n==160){int32_t cur=v.mod_cur[0];fm_voice_note_on(&v,330000,20000);ref_voice_note_on(&r,330000,20000);BOUND_CHECK(&stats->bounds, v.mod_cur[0]==cur);}
  fm_render_q30(&v,out,64);ref_render(&r,rout,64);
  BOUND_CHECK(&stats->bounds, v.mod_cur[0]==r.mod_cur[0]&&v.mod_pd[0]==r.mod_pd[0]&&v.op[0].pd_knee==r.op[0].pd_knee);
  BOUND_CHECK(&stats->bounds, v.op[0].pd_knee>=1&&v.op[0].pd_knee<=32767);
 }
 fm_voice_note_off(&v);ref_voice_note_off(&r);for(n=0;n<20;n++){fm_render_q30(&v,out,64);ref_render(&r,rout,64);}
 {uint32_t knee=v.op[0].pd_knee;int32_t cur=v.mod_cur[0];fm_voice_ctl(&v,0,-32768);fm_render_q30(&v,out,64);BOUND_CHECK(&stats->bounds, v.op[0].pd_knee==knee&&v.mod_cur[0]==cur);}
 fm_voice_note_on(&v,220000,32767);BOUND_CHECK(&stats->bounds, v.op[0].pd_knee==16384&&v.mod_cur[0]==0);
}
static void pd_check_blocks(pd_check_stats *stats,uint32_t fs){unsigned identity,mode,n,i,j;uint32_t rng=9;int32_t x[257],y[257];double rx[257],ry[257];
 for(identity=0;identity<2;identity++)for(mode=0;mode<8;mode++){
  fm_patch_t p=pd_check_patch(),q;fm_voice_t a,b;ref_voice_t r,s;
  p.route.ops=mode%2?4:0;p.route.carriers=5;p.route.w[1][0]=256;p.route.w[3][2]=128;p.route.kind[3][2]=mode%3;
  p.lfo.rate_millihz=5000;p.lfo.free_run=mode%2?2:0;p.lfo.pitch_cents=20;p.lfo.mask_pitch=15;
  p.filter=(fm_filter_patch_t){1000000,707,(uint16_t)(mode%5),(uint16_t)(mode%3)};
  for(i=0;i<FM_OPS;i++){p.op[i].index_q10=300;p.op[i].feedback_q10=123;p.op[i].phase_u16=(uint16_t)(pd_check_draw(&rng));p.op[i].free_run=mode%3;p.pd[i]=(fm_pd_patch_t){identity?16384:(uint16_t)(i%2?1:32767),1};}
  q=p;if(identity)memset(q.pd,0,sizeof q.pd);
  memset(&a,0,sizeof a);memset(&b,0,sizeof b);memset(&r,0,sizeof r);memset(&s,0,sizeof s);
  fm_voice_init(&a,&p,fs);fm_voice_init(&b,&q,fs);ref_voice_init(&r,&p,fs);ref_voice_init(&s,&q,fs);
  for(n=0;n<40;n++){
   if(n==1||n==35){fm_voice_note_on(&a,220000,32767);fm_voice_note_on(&b,220000,32767);ref_voice_note_on(&r,220000,32767);ref_voice_note_on(&s,220000,32767);}
   if(n==10){fm_voice_pitch(&a,330000);fm_voice_pitch(&b,330000);ref_voice_pitch(&r,330000);ref_voice_pitch(&s,330000);}
   if(n==20){fm_voice_note_off(&a);fm_voice_note_off(&b);ref_voice_note_off(&r);ref_voice_note_off(&s);}
   fm_render_q30(&a,x,257);ref_render(&r,rx,257);for(j=0;j<257;j++){fm_render_q30(&b,y+j,1);ref_render(&s,ry+j,1);}
   BOUND_CHECK(&stats->bounds, !memcmp(x,y,sizeof x)&&!memcmp(rx,ry,sizeof rx));
   {fm_voice_t aa=a,bb=b;ref_voice_t rr=r,ss=s;if(identity){memset(aa.patch.pd,0,sizeof aa.patch.pd);memset(rr.patch.pd,0,sizeof rr.patch.pd);for(i=0;i<FM_OPS;i++){aa.op[i].pd_knee=rr.op[i].pd_knee=0;aa.op[i].pd_slope[0]=aa.op[i].pd_slope[1]=0;}}BOUND_CHECK(&stats->bounds, mod_state_equal(&aa,&bb)&&wide_ref_state_equal(&rr,&ss));}
  }
 }
}

static unsigned pd_bounds(uint32_t fs)
{
    pd_check_stats stats={ { "PDBOUND-FAIL", 0, 0 }, 0, 0 };
    pd_check_maps(&stats,fs);pd_check_raw(&stats,fs);pd_check_matrix(&stats,fs);pd_check_blocks(&stats,fs);
    printf("PDBOUND fs=%u phase64=%d checks=%llu failures=%llu max_sine_q30=%.6f\n",fs,FM_PHASE64,(unsigned long long)stats.bounds.checks,(unsigned long long)stats.bounds.bad,stats.maxsine);
    return stats.bounds.bad;
}

/* Public-renderer vector oracle: signed quotient/remainder rounding. */
static int64_t vector_down(int64_t n, int64_t d)
{ return n / d - (n % d < 0); }
static int32_t vector_lerp_expected(int32_t a, int32_t b, uint32_t x)
{
    int64_t z = vector_down((int64_t)a * (32768u-x) + (int64_t)b*x + 16384, 32768);
    return z > INT32_MAX ? INT32_MAX : z < -INT32_MAX ? -INT32_MAX : (int32_t)z;
}
static int32_t vector_expected(const int32_t s[4], uint32_t x, uint32_t y)
{ return vector_lerp_expected(vector_lerp_expected(s[0],s[1],x),vector_lerp_expected(s[2],s[3],x),y); }
static uint32_t vector_draw(uint32_t *r)
{ *r ^= *r << 13; *r ^= *r >> 17; *r ^= *r << 5; return *r; }
static fm_patch_t vector_patch(void)
{
    fm_patch_t p={0};unsigned i;p.route.ops=4;p.route.carriers=15;p.vector.enabled=1;
    for(i=0;i<FM_OPS;i++){p.op[i].ratio_q16=(i+1)*65536;p.op[i].level_db10=180;p.op[i].attack_ms=5;p.op[i].decay_ms=100;p.op[i].release_ms=20;}
    return p;
}
static int32_t vector_read(const fm_voice_t *base,const int32_t s[4],uint32_t x,uint32_t y)
{
    fm_voice_t v=*base;int32_t out;unsigned i;
    v.vector_x=x;v.vector_y=y;
    for(i=0;i<4;i++){v.patch.op[i].wave=s[i]==1073741824?7:4;v.op[i].noise=(uint32_t)((int64_t)s[i]*2);}
    fm_render_q30(&v,&out,1);return out;
}
static unsigned vector_bounds(uint32_t fs)
{
    bound_t bounds = { "VECTORFAIL", 0, 0 };
    static const uint16_t coordinates[]={0,1,16383,16384,16385,32767,32768,65535};
    static const uint8_t ops[]={0,1,2,3,4,5,255},carriers[]={0,1,7,14,15,31,255};
    static const int32_t endpoints[]={-1073741824,-1073741823,-1,0,1,1073741823,1073741824};
    unsigned a,b,c,d,n,i,j;uint32_t rng=0x564543;double worst=0;
    fm_patch_t p=vector_patch();fm_voice_t v,w;ref_voice_t r;int32_t sample;
    for(a=0;a<sizeof ops;a++)for(b=0;b<sizeof carriers;b++)for(c=0;c<8;c++)for(d=0;d<2;d++){
        unsigned enabled;
        p.route.ops=ops[a];p.route.carriers=carriers[b];p.vector=(fm_vector_patch_t){coordinates[c],coordinates[7-c],d?65535:0};
        p.mod[0]=(fm_mod_slot_patch_t){32768,16,34,0,0,{0,0,0}};p.mod[1]=p.mod[0];p.mod[1].target=35;
        memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
        enabled=d&&ops[a]==4&&carriers[b]==15;
        BOUND_CHECK(&bounds, v.patch.vector.enabled==enabled&&r.patch.vector.enabled==enabled);
        BOUND_CHECK(&bounds, v.patch.vector.x_q15==(enabled?(coordinates[c]>32768?32768:coordinates[c]):0));
        BOUND_CHECK(&bounds, v.patch.vector.y_q15==(enabled?(coordinates[7-c]>32768?32768:coordinates[7-c]):0));
        BOUND_CHECK(&bounds, v.patch.vector.x_q15==r.patch.vector.x_q15&&v.patch.vector.y_q15==r.patch.vector.y_q15);
        BOUND_CHECK(&bounds, v.mod_active==(enabled?3u:0u)&&r.mod_active==v.mod_active);
    }
    p=vector_patch();memset(&v,0,sizeof v);fm_voice_init(&v,&p,fs);fm_voice_note_on(&v,220000,32767);
    memset(&r,0,sizeof r);ref_voice_init(&r,&p,fs);ref_voice_note_on(&r,220000,32767);
    for(i=0;i<4;i++){r.op[i].inc=0;r.op[i].iinc=0;r.op[i].phase=0;r.op[i].iphase=0;r.op[i].stage=FM_STAGE_SUSTAIN;r.op[i].att=0;r.op[i].out=1;v.op[i].inc=v.op[i].phase=0;v.op[i].stage=FM_STAGE_SUSTAIN;v.op[i].att=0;v.op[i].out=1073741824;}
    for(a=0;a<7;a++)for(b=0;b<7;b++)for(c=0;c<7;c++)for(d=0;d<7;d++){
        int32_t s[4]={endpoints[a],endpoints[b],endpoints[c],endpoints[d]};
        BOUND_CHECK(&bounds, vector_read(&v,s,0,0)==s[0]);BOUND_CHECK(&bounds, vector_read(&v,s,32768,0)==s[1]);
        BOUND_CHECK(&bounds, vector_read(&v,s,0,32768)==s[2]);BOUND_CHECK(&bounds, vector_read(&v,s,32768,32768)==s[3]);
        BOUND_CHECK(&bounds, vector_read(&v,s,16384,16384)==vector_expected(s,16384,16384));
    }
    for(n=0;n<200000;n++){
        int32_t s[4],t[4],got,transposed,lo=INT32_MAX,hi=INT32_MIN;uint32_t x=vector_draw(&rng)%32769,y=vector_draw(&rng)%32769;double ideal,error;
        for(i=0;i<4;i++){s[i]=(int32_t)(vector_draw(&rng)>>1)-1073741824;if(s[i]<lo)lo=s[i];if(s[i]>hi)hi=s[i];}
        got=vector_read(&v,s,x,y);BOUND_CHECK(&bounds, got==vector_expected(s,x,y));BOUND_CHECK(&bounds, got>=lo&&got<=hi);
        t[0]=s[0];t[1]=s[2];t[2]=s[1];t[3]=s[3];transposed=vector_read(&v,t,y,x);
        BOUND_CHECK(&bounds, (int64_t)got-transposed>=-2&&(int64_t)got-transposed<=2);
        ideal=((double)s[0]*(32768-x)*(32768-y)+(double)s[1]*x*(32768-y)+(double)s[2]*(32768-x)*y+(double)s[3]*x*y)/1073741824.0;
        if(n<10000){ref_voice_t ref=r;double out;ref.vector_x=x;ref.vector_y=y;
            for(i=0;i<4;i++){ref.patch.op[i].wave=4;ref.op[i].noise=(uint32_t)((int64_t)s[i]*2);}
            ref_render(&ref,&out,1);BOUND_CHECK(&bounds, fabs(out*32768.0-ideal)<0.000001);
        }
        error=fabs(got-ideal);if(error>worst)worst=error;BOUND_CHECK(&bounds, error<=1.000001);
        for(i=0;i<4;i++)s[i]=s[0];BOUND_CHECK(&bounds, vector_read(&v,s,x,y)==s[0]);
    }
    /* Edges, levels and envelopes must be evaluated before vector mixing;
     * all operator histories must remain those of the unmodified route. */
    for(n=0;n<48;n++){
        fm_patch_t q;
        int32_t x[257],y[257],s[4];
        p=vector_patch();p.vector.x_q15=(uint16_t)(n*701%32769);p.vector.y_q15=(uint16_t)(n*1009%32769);
        for(i=0;i<4;i++){
            p.op[i].wave=(uint16_t)((n+i)%12);p.op[i].index_q10=(uint16_t)(500+i*233);p.op[i].feedback_q10=200;
            p.op[i].level_db10=(uint16_t)(100+i*73);p.op[i].sustain_db10=(uint16_t)(i*60);
            for(j=0;j<4;j++)if(i!=j){p.route.w[j][i]=(uint16_t)(20+i*13+j*31);p.route.kind[j][i]=(uint8_t)((n+i+j)%3);}
        }
        q=p;q.vector.enabled=0;memset(&v,0,sizeof v);memset(&w,0,sizeof w);fm_voice_init(&v,&p,fs);fm_voice_init(&w,&q,fs);
        fm_voice_note_on(&v,220000,32767);fm_voice_note_on(&w,220000,32767);
        for(a=0;a<513;a++){
            fm_render_q30(&v,x,1);fm_render_q30(&w,y,1);
            {fm_voice_t twin=w;twin.vector_x=v.vector_x;twin.vector_y=v.vector_y;BOUND_CHECK(&bounds, mod_state_equal(&v,&twin));}
            for(i=0;i<4;i++){s[i]=(int32_t)vector_down((int64_t)v.op[i].prev*v.op[i].out,1073741824);}
            BOUND_CHECK(&bounds, x[0]==vector_expected(s,p.vector.x_q15,p.vector.y_q15));
        }
        w=v;fm_render_q30(&v,x,257);for(i=0;i<257;i++)fm_render_q30(&w,y+i,1);
        BOUND_CHECK(&bounds, !memcmp(x,y,sizeof x)&&mod_state_equal(&v,&w));
        fm_voice_note_off(&v);fm_voice_note_off(&w);fm_render_q30(&v,x,257);fm_render_q30(&w,y,257);
        BOUND_CHECK(&bounds, !memcmp(x,y,sizeof x)&&mod_state_equal(&v,&w));
    }
    p=vector_patch();p.vector.x_q15=12345;p.vector.y_q15=23456;p.filter=(fm_filter_patch_t){2000000,707,1,2};p.shape=(fm_shape_patch_t){120,3,0};
    memset(&v,0,sizeof v);fm_voice_init(&v,&p,fs);fm_voice_note_on(&v,220000,32767);w=v;
    for(n=0;n<10000;n++){int32_t expected;if(n==100){fm_voice_note_off(&v);fm_voice_note_off(&w);}fm_render_q30(&v,&sample,1);fm_render_q30(&w,&expected,1);BOUND_CHECK(&bounds, sample==expected&&mod_state_equal(&v,&w));}
    printf("VECTORBOUND fs=%u phase64=%d checks=%u failures=%u max_ideal_q30=%.9f\n",fs,FM_PHASE64,bounds.checks,bounds.bad,worst);
    return bounds.bad;
}

/* Public /vector oracle: exact signed division and independent smoother. */
static int32_t vm_clamp(int64_t x,int32_t a,int32_t b)
{ return x<a?a:x>b?b:(int32_t)x; }
static int32_t vm_smooth(int32_t x,int32_t goal,unsigned shift,int note)
{
    int32_t d,step,den;if(!shift)return goal;if(note)return x;
    d=goal-x;den=1<<shift;step=d/den;if(d%den)step+=d<0?-1:1;return x+step;
}
static unsigned vector_move_bounds(uint32_t fs)
{
    bound_t bounds = { "VMFAIL", 0, 0 };
    unsigned shift,axis,i,j,n,valid;uint32_t rng=0x4d4f5645;
    fm_patch_t p;fm_voice_t v,w;ref_voice_t r;int32_t out[FM_CTL],x[257],y[257];double rout[FM_CTL];
    /* Raw target/index/prerequisite/amount extremes, including zero amount. */
    for(axis=34;axis<=35;axis++)for(i=0;i<256;i++)for(j=0;j<8;j++){
        static const int32_t amount[]={INT32_MIN,-32769,-32768,-1,0,1,32768,INT32_MAX};
        p=vector_patch();p.vector.x_q15=12345;p.vector.y_q15=23456;p.mod[0]=(fm_mod_slot_patch_t){amount[j],16,(uint8_t)axis,(uint8_t)i,255,{1,2,3}};
        memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
        valid=i==0;BOUND_CHECK(&bounds, v.mod_active==(valid&&amount[j]!=0)&&r.mod_active==v.mod_active);
        BOUND_CHECK(&bounds, v.patch.mod[0].amount==(valid?vm_clamp(amount[j],-32768,32768):0));
        BOUND_CHECK(&bounds, v.patch.mod[0].shift==15&&!v.patch.mod[0].reserved[0]&&!v.patch.mod[0].reserved[1]&&!v.patch.mod[0].reserved[2]);
        BOUND_CHECK(&bounds, v.vector_x==12345&&v.vector_y==23456&&r.vector_x==12345&&r.vector_y==23456);
        p.vector.enabled=0;fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
        BOUND_CHECK(&bounds, !v.mod_active&&!r.mod_active&&!v.vector_x&&!v.vector_y&&!r.vector_x&&!r.vector_y);
    }
    /* Eight simultaneous endpoint contributions at raw sample-rate bounds. */
    for(axis=34;axis<=35;axis++)for(n=0;n<4;n++)for(j=0;j<4;j++){
        const uint32_t rates[]={0,1,48000,UINT32_MAX};int32_t ctl=n&1?-32768:32768,amount=n&2?-32768:32768;
        p=vector_patch();p.vector.x_q15=12345;p.vector.y_q15=23456;
        for(i=0;i<8;i++)p.mod[i]=(fm_mod_slot_patch_t){amount,(uint16_t)(64+i),(uint8_t)axis,0,0,{0,0,0}};
        fm_voice_init(&v,&p,rates[j]);ref_voice_init(&r,&p,rates[j]);
        for(i=0;i<8;i++){fm_voice_gen(&v,i,ctl);ref_voice_gen(&r,i,ctl);}
        BOUND_CHECK(&bounds, v.vector_x==12345&&v.vector_y==23456);
        fm_voice_note_on(&v,220000,32767);ref_voice_note_on(&r,220000,32767);
        BOUND_CHECK(&bounds, (axis==34?v.vector_x:v.vector_y)==(ctl==amount?32768u:0u));
        BOUND_CHECK(&bounds, (axis==34?v.vector_y:v.vector_x)==(axis==34?23456u:12345u));
        BOUND_CHECK(&bounds, r.vector_x==v.vector_x&&r.vector_y==v.vector_y);
    }
    /* Every source smoother shift; instantaneous and retrigger ticks HOLD
     * positive-shift state; controller setters themselves never tick. */
    for(shift=0;shift<16;shift++)for(axis=34;axis<=35;axis++){
        int32_t cur[8]={0},control[8]={0};
        p=vector_patch();p.vector.x_q15=9000;p.vector.y_q15=24000;
        for(i=0;i<4;i++){p.op[i].attack_ms=0;p.op[i].release_ms=0;}
        for(i=0;i<8;i++)p.mod[i]=(fm_mod_slot_patch_t){i&1?-32768:32768,(uint16_t)(16+i),(uint8_t)(i&2?69-axis:axis),0,(uint8_t)shift,{0,0,0}};
        memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
        for(i=0;i<8;i++){control[i]=(int32_t)(vector_draw(&rng)%65537)-32768;fm_voice_ctl(&v,i,control[i]);ref_voice_ctl(&r,i,control[i]);}
        BOUND_CHECK(&bounds, v.vector_x==9000&&v.vector_y==24000);
        for(n=0;n<96;n++){
            int note=n==0||n==37;int64_t sumx=0,sumy=0;
            if(note){fm_voice_note_on(&v,220000,32767);ref_voice_note_on(&r,220000,32767);}
            else{fm_render_q30(&v,out,FM_CTL);ref_render(&r,rout,FM_CTL);}
            for(i=0;i<8;i++){
                cur[i]=vm_smooth(cur[i],control[i],shift,note);
                if(p.mod[i].target==34)sumx+=(int64_t)cur[i]*p.mod[i].amount/32768;else sumy+=(int64_t)cur[i]*p.mod[i].amount/32768;
                BOUND_CHECK(&bounds, v.mod_cur[i]==cur[i]&&r.mod_cur[i]==cur[i]);
            }
            BOUND_CHECK(&bounds, v.vector_x==(uint32_t)vm_clamp(9000+sumx,0,32768)&&v.vector_y==(uint32_t)vm_clamp(24000+sumy,0,32768));
            BOUND_CHECK(&bounds, r.vector_x==v.vector_x&&r.vector_y==v.vector_y);
            for(i=0;i<8;i++){control[i]=(int32_t)(vector_draw(&rng)%65537)-32768;fm_voice_ctl(&v,i,control[i]);ref_voice_ctl(&r,i,control[i]);}
        }
    }
    /* Nonlinear clipping belongs after the complete sum. Permuting the
     * cancelling slots cannot move a coordinate (including eight extrema). */
    for(n=0;n<20000;n++){
        int64_t sx=0,sy=0;uint16_t bx=(uint16_t)(vector_draw(&rng)%32769),by=(uint16_t)(vector_draw(&rng)%32769);
        p=vector_patch();p.vector.x_q15=bx;p.vector.y_q15=by;
        for(i=0;i<8;i++){p.mod[i]=(fm_mod_slot_patch_t){(int32_t)(vector_draw(&rng)%65537)-32768,(uint16_t)(16+i),(uint8_t)(34+(i&1)),0,0,{0,0,0}};}
        memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
        for(i=0;i<8;i++){int32_t c=(int32_t)(vector_draw(&rng)%65537)-32768;fm_voice_ctl(&v,i,c);ref_voice_ctl(&r,i,c);if(i&1)sy+=(int64_t)c*p.mod[i].amount/32768;else sx+=(int64_t)c*p.mod[i].amount/32768;}
        fm_voice_note_on(&v,220000,32767);ref_voice_note_on(&r,220000,32767);
        BOUND_CHECK(&bounds, v.vector_x==(uint32_t)vm_clamp(bx+sx,0,32768)&&v.vector_y==(uint32_t)vm_clamp(by+sy,0,32768));
        BOUND_CHECK(&bounds, r.vector_x==v.vector_x&&r.vector_y==v.vector_y);
        for(i=0;i<4;i++){fm_mod_slot_patch_t temp=p.mod[i];p.mod[i]=p.mod[7-i];p.mod[7-i]=temp;}fm_voice_init(&w,&p,fs);for(i=0;i<8;i++)fm_voice_ctl(&w,i,v.ctl_input[i]);
        fm_voice_note_on(&w,220000,32767);BOUND_CHECK(&bounds, w.vector_x==v.vector_x&&w.vector_y==v.vector_y);
    }
    /* Unavailable coordinates are reset by re-init, and zero targets retain
     * static output while the empty matrix path stays bypassed. */
    for(n=0;n<64;n++){
        fm_patch_t q;
        p=vector_patch();p.vector.x_q15=(uint16_t)(vector_draw(&rng)%32769);p.vector.y_q15=(uint16_t)(vector_draw(&rng)%32769);
        q=p;q.mod[0]=(fm_mod_slot_patch_t){0,16,34,0,0,{0,0,0}};q.mod[1]=(fm_mod_slot_patch_t){0,17,35,0,15,{0,0,0}};
        memset(&v,0,sizeof v);memset(&w,0,sizeof w);fm_voice_init(&v,&p,fs);fm_voice_init(&w,&q,fs);fm_voice_note_on(&v,220000,32767);fm_voice_note_on(&w,220000,32767);
        for(j=0;j<12;j++){fm_voice_ctl(&w,0,j&1?32768:-32768);fm_render_q30(&v,x,257);fm_render_q30(&w,y,257);BOUND_CHECK(&bounds, !memcmp(x,y,sizeof x));{fm_voice_t twin=w;memcpy(twin.ctl_input,v.ctl_input,sizeof twin.ctl_input);BOUND_CHECK(&bounds, mod_state_equal(&v,&twin));}BOUND_CHECK(&bounds, w.vector_x==v.vector_x&&w.vector_y==v.vector_y);}
        p.vector.enabled=0;fm_voice_init(&w,&p,fs);BOUND_CHECK(&bounds, !w.vector_x&&!w.vector_y);
    }
    /* FREE2 idle clocks catch up at note-on, but vector/smoother state does
     * no idle work. Mid-note copies and arbitrary block partitions agree. */
    for(shift=0;shift<16;shift++){
        uint32_t xhold,yhold;int32_t curhold[8];
        p=vector_patch();p.vector.x_q15=p.vector.y_q15=16384;p.lfo.rate_millihz=1000;p.lfo.wave=0;p.lfo.free_run=2;
        for(i=0;i<4;i++){p.op[i].attack_ms=0;p.op[i].release_ms=0;p.op[i].free_run=2;}
        p.mod[0]=(fm_mod_slot_patch_t){16384,0,34,0,(uint8_t)shift,{0,0,0}};p.mod[1]=(fm_mod_slot_patch_t){32768,16,35,0,(uint8_t)shift,{0,0,0}};
        memset(&v,0,sizeof v);fm_voice_init(&v,&p,fs);fm_voice_ctl(&v,0,32768);fm_render_q30(&v,x,129);BOUND_CHECK(&bounds, v.vector_x==16384&&v.vector_y==16384&&!v.mod_cur[0]&&!v.mod_cur[1]);
        fm_voice_note_on(&v,220000,32767);w=v;
        for(n=0;n<16;n++){fm_render_q30(&v,x,257);for(i=0;i<257;i++)fm_render_q30(&w,y+i,1);BOUND_CHECK(&bounds, !memcmp(x,y,sizeof x)&&mod_state_equal(&v,&w));}
        fm_voice_note_off(&v);fm_render_q30(&v,x,2);BOUND_CHECK(&bounds, !fm_voice_active(&v));xhold=v.vector_x;yhold=v.vector_y;memcpy(curhold,v.mod_cur,sizeof curhold);
        fm_voice_ctl(&v,0,-32768);for(n=0;n<17;n++)fm_render_q30(&v,x,257);
        BOUND_CHECK(&bounds, v.vector_x==xhold&&v.vector_y==yhold&&!memcmp(curhold,v.mod_cur,sizeof curhold));
        fm_voice_note_on(&v,330000,32767);BOUND_CHECK(&bounds, shift?(v.vector_x==16384&&v.vector_y==16384):(v.vector_y==0));
    }
    printf("VECTORMOVE fs=%u phase64=%d checks=%u failures=%u\n",fs,FM_PHASE64,bounds.checks,bounds.bad);
    return bounds.bad;
}

/* Public formant oracle: quotient/remainder signed rounding, independent
 * recurrence, explicit bank and downstream lifetimes. No private includes. */
static int64_t form_down(int64_t a,int64_t b){return a/b-(a%b<0);}
static int32_t form_clip(int64_t a){return a>INT32_MAX?INT32_MAX:a<-(int64_t)INT32_MAX?-INT32_MAX:(int32_t)a;}
static uint32_t form_draw(uint32_t*r){*r^=*r<<13;*r^=*r>>17;*r^=*r<<5;return *r;}
static int form_quiet(int32_t a){return a>=-2048&&a<=2048;}
static int32_t form_model(fm_svf_t*f,int32_t x,uint64_t*hits)
{
 int64_t d=(int64_t)x-f->ic2,b,l,z;int32_t bp,lp;
 if(d>INT32_MAX||d<-(int64_t)INT32_MAX)hits[0]++;d=form_clip(d);
 b=form_down((int64_t)f->a1*f->ic1+(int64_t)f->a2*d+536870912,1073741824);
 l=f->ic2+form_down((int64_t)f->a2*f->ic1+(int64_t)f->a3*d+536870912,1073741824);
 bp=form_clip(b);lp=form_clip(l);
 if(2*b-f->ic1>INT32_MAX||2*b-f->ic1<-(int64_t)INT32_MAX)hits[1]++;
 if(2*l-f->ic2>INT32_MAX||2*l-f->ic2<-(int64_t)INT32_MAX)hits[2]++;
 f->ic1=form_clip(2*b-f->ic1);f->ic2=form_clip(2*l-f->ic2);
 if(f->mode==1)z=lp;else if(f->mode==2)z=bp;else{z=x-form_down((int64_t)f->k*bp+268435456,536870912);if(f->mode==3)z-=lp;}
 return form_clip(z);
}
static int32_t form_model_ring(fm_svf_t*f,uint64_t*hits)
{
 int32_t y;int stop;if(!f->ic1&&!f->ic2)return 0;y=form_model(f,0,hits);
 stop=form_quiet(f->ic1)&&form_quiet(f->ic2);if(!stop){f->idle++;stop=f->idle>=262144;}
 if(stop)f->ic1=f->ic2=0;return y;
}
static int form_states_equal(const fm_svf_t*a,const fm_svf_t*b)
{return a->ic1==b->ic1&&a->ic2==b->ic2&&a->idle==b->idle;}
static int form_coef_equal(const fm_svf_t*a,const fm_svf_t*b)
{return a->a1==b->a1&&a->a2==b->a2&&a->a3==b->a3&&a->k==b->k;}
static uint32_t form_center_expected(int64_t x,uint32_t fs)
{
 uint64_t rate=fs?fs:1,low=(rate*5+11)/12,high=rate*450;
 if(x<0)x=0;if(x>UINT32_MAX)x=UINT32_MAX;if((uint64_t)x<low)x=(int64_t)low;if((uint64_t)x>high)x=(int64_t)high;return(uint32_t)x;
}
static fm_patch_t form_patch(void)
{
 fm_patch_t p={0};unsigned i;
 p.route.ops=2;p.route.carriers=3;
 for(i=0;i<FM_OPS;i++){p.op[i].ratio_q16=65536;p.op[i].attack_ms=0;p.op[i].decay_ms=20;p.op[i].release_ms=0;p.op[i].level_db10=240;}
 for(i=0;i<3;i++)p.formant[i]=(fm_formant_patch_t){500000+i*1000000,2000+i*1000,i*60,1};
 return p;
}
/* Freeze two legal +/-1 source samples whose exact carrier sum is x. */
static void form_input(fm_voice_t*v,int32_t x)
{
 int32_t source[2];unsigned i;source[0]=(int32_t)form_down(x,2);source[1]=x-source[0];
 for(i=0;i<2;i++){v->op[i].phase=v->op[i].inc=0;v->op[i].att=0;v->op[i].stage=FM_STAGE_SUSTAIN;v->op[i].out=1073741824;
 v->patch.op[i].wave=source[i]==1073741824?7:4;v->op[i].noise=(uint32_t)((int64_t)source[i]*2);}
}
static unsigned formant_bounds(uint32_t fs)
{
    bound_t bounds = { "FORMFAIL", 0, 0 };
 unsigned i,j,k,n,mode,stages,cut,qi,kind,amp,shift;uint32_t rng=0x464f524d;
 uint64_t hits[3]={0},sum_hits=0,byq[2][3]={{0}};fm_patch_t p,q;fm_voice_t v,w;ref_voice_t r;
 int32_t out[257],other[257];double real[257];unsigned final_tail=0,next_ring=0;
 /* Raw fields and effective coefficients agree with canonical D57 twins. */
 for(i=0;i<5;i++)for(j=0;j<6;j++)for(k=0;k<6;k++){
  static const uint32_t rates[]={0,1,44100,48000,UINT32_MAX},centers[]={0,1,19999,20000,UINT32_MAX-1,UINT32_MAX};
  static const uint16_t values[]={0,1,499,500,16000,65535};uint32_t rate=rates[i],center=form_center_expected(centers[j],rate);
  p=form_patch();p.formant[0]=(fm_formant_patch_t){centers[j],values[k],values[5-k],65535};p.formant[1].enabled=p.formant[2].enabled=0;
  memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,rate);ref_voice_init(&r,&p,rate);
  BOUND_CHECK(&bounds, v.formant_mask==1&&r.formant_mask==1&&v.patch.formant[0].enabled==1);
  BOUND_CHECK(&bounds, v.formant[0].center_millihz==center&&r.formant[0].center_millihz==center);
  BOUND_CHECK(&bounds, v.patch.formant[0].q_milli==(values[k]<500?500:values[k]>16000?16000:values[k]));
  BOUND_CHECK(&bounds, v.formant[0].level_db10==(values[5-k]>1200?1200:values[5-k])&&r.formant[0].level_db10==v.formant[0].level_db10);
  q=p;q.formant[0].enabled=0;q.filter=(fm_filter_patch_t){center,v.patch.formant[0].q_milli,2,1};fm_voice_init(&w,&q,rate);
  BOUND_CHECK(&bounds, form_coef_equal(&v.formant[0].svf,&w.svf));BOUND_CHECK(&bounds, !fm_voice_active(&v));
  for(n=1;n<3;n++)BOUND_CHECK(&bounds, !v.patch.formant[n].center_millihz&&!v.patch.formant[n].q_milli&&!v.patch.formant[n].level_db10&&!v.formant[n].gain_q30);
 }
 /* Target indices, disabled prerequisites and full int32 center amounts. */
 for(i=36;i<=37;i++)for(j=0;j<256;j++)for(k=0;k<6;k++){
  static const int32_t amounts[]={INT32_MIN,-INT32_MAX,-1,0,1,INT32_MAX};int32_t lim=i==36?INT32_MAX:1200;int32_t a=amounts[k]<-lim?-lim:amounts[k]>lim?lim:amounts[k];
  p=form_patch();p.mod[0]=(fm_mod_slot_patch_t){amounts[k],16,i,j,0,{0,0,0}};fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
  BOUND_CHECK(&bounds, v.mod_active==(j<3&&a!=0)&&r.mod_active==v.mod_active);
  BOUND_CHECK(&bounds, v.patch.mod[0].amount==(j<3?a:0)&&r.patch.mod[0].amount==v.patch.mod[0].amount);
  if(j<3){p.formant[j].enabled=0;fm_voice_init(&v,&p,fs);BOUND_CHECK(&bounds, !v.mod_active);}
 }
 /* Eight INT32_MAX contributions require >32-bit sums. Cancellation occurs
  * before UINT32 and effective-rate clamps, at all raw sample-rate bounds. */
 for(i=0;i<4;i++)for(k=0;k<4;k++)for(j=0;j<4;j++){
  const uint32_t rates[]={0,1,48000,UINT32_MAX};int64_t sum=0;uint32_t base=k&1?UINT32_MAX:0;
  p=form_patch();p.formant[0].center_millihz=base;
  for(n=0;n<8;n++)p.mod[n]=(fm_mod_slot_patch_t){k&2?-INT32_MAX:INT32_MAX,16+n,36,0,0,{0,0,0}};
  fm_voice_init(&v,&p,rates[i]);ref_voice_init(&r,&p,rates[i]);
  for(n=0;n<8;n++){int32_t c=j==0?32768:j==1?-32768:j==2?(n<4?32768:-32768):(n<7?32768:-32768);sum+=(int64_t)c*p.mod[n].amount/32768;fm_voice_ctl(&v,n,c);ref_voice_ctl(&r,n,c);}
  fm_voice_note_on(&v,220000,32767);ref_voice_note_on(&r,220000,32767);
  BOUND_CHECK(&bounds, v.mod_formant_center[0]==sum&&r.mod_formant_center[0]==sum);
  BOUND_CHECK(&bounds, v.formant[0].center_millihz==form_center_expected(base+sum,rates[i])&&r.formant[0].center_millihz==v.formant[0].center_millihz);
 }
 /* Raw-base and opposite large contributions must cancel before either
  * the uint32 clamp or D57 frequency clamp. */
 for(i=0;i<2;i++){
  p=form_patch();p.formant[0].center_millihz=i?UINT32_MAX:0;
  p.mod[0]=(fm_mod_slot_patch_t){i?-INT32_MAX:INT32_MAX,16,36,0,0,{0,0,0}};
  p.mod[1]=(fm_mod_slot_patch_t){-INT32_MAX+1000000,17,36,0,0,{0,0,0}};
  fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);fm_voice_ctl(&v,0,32768);fm_voice_ctl(&v,1,32768);ref_voice_ctl(&r,0,32768);ref_voice_ctl(&r,1,32768);
  fm_voice_note_on(&v,220000,32767);ref_voice_note_on(&r,220000,32767);
  BOUND_CHECK(&bounds, v.formant[0].center_millihz==1000000+i&&r.formant[0].center_millihz==1000000+i);
 }
 /* Fractional signed Q15 sources exercise the full-width multiply and
  * eight-slot sum independently, rather than only endpoint products. */
 for(n=0;n<4096;n++){
  int64_t sum=0;
  p=form_patch();p.formant[0].center_millihz=form_draw(&rng);
  for(i=0;i<8;i++){int32_t amount=(int32_t)form_draw(&rng);if(amount==INT32_MIN)amount=-INT32_MAX;p.mod[i]=(fm_mod_slot_patch_t){amount,16+i,36,0,0,{0,0,0}};}
  fm_voice_init(&v,&p,UINT32_MAX);ref_voice_init(&r,&p,UINT32_MAX);
  for(i=0;i<8;i++){int32_t c=(int32_t)(form_draw(&rng)%65537)-32768;sum+=(int64_t)c*p.mod[i].amount/32768;fm_voice_ctl(&v,i,c);ref_voice_ctl(&r,i,c);}
  fm_voice_note_on(&v,220000,32767);ref_voice_note_on(&r,220000,32767);
  BOUND_CHECK(&bounds, v.mod_formant_center[0]==sum&&r.mod_formant_center[0]==sum);
  BOUND_CHECK(&bounds, v.formant[0].center_millihz==form_center_expected((int64_t)p.formant[0].center_millihz+sum,UINT32_MAX)&&r.formant[0].center_millihz==v.formant[0].center_millihz);
 }
 /* Exact gain lookup uses the established output attenuation arithmetic. */
 for(i=0;i<=1200;i++){
  p=form_patch();p.formant[0].level_db10=i;p.op[0].level_db10=i;p.route.carriers=1;fm_voice_init(&v,&p,fs);fm_voice_note_on(&v,220000,32767);
  BOUND_CHECK(&bounds, v.formant[0].gain_q30==v.op[0].out);
 }
 /* Public-renderer recurrence grid, both Q endpoints and high/low centers.
  * All gain products accumulate wide; arbitrary sum order stays exact. */
 for(qi=0;qi<2;qi++)for(cut=0;cut<3;cut++)for(kind=0;kind<4;kind++)for(amp=0;amp<3;amp++){
  fm_svf_t model[3];int32_t gains[3];uint64_t start[3]={hits[0],hits[1],hits[2]};
  p=form_patch();for(i=0;i<3;i++){p.formant[i].q_milli=qi?16000:500;p.formant[i].center_millihz=cut==0?(uint32_t)(((uint64_t)fs*5+11)/12):cut==1?1000000:(uint32_t)((uint64_t)fs*450);p.formant[i].level_db10=i==0?0:i==1?60:120;}
  memset(&v,0,sizeof v);fm_voice_init(&v,&p,fs);for(i=0;i<3;i++){model[i]=v.formant[i].svf;gains[i]=v.formant[i].gain_q30;}
  for(n=0;n<2048;n++){
   const int32_t amplitudes[]={134217728,1073741824,INT32_MAX};int32_t a=amplitudes[amp],input;int64_t sum=0,reverse=0;int32_t each[3];
   input=kind==0?(n==0?a:0):kind==1?(n&1?-a:a):kind==2?(int32_t)llround(a*sin(6.283185307179586*(double)p.formant[0].center_millihz/1000/fs*n)):(int32_t)((int64_t)(int32_t)form_draw(&rng)*a/2147483648LL);
   for(i=0;i<3;i++){each[i]=form_model(&model[i],input,hits);sum+=form_down((int64_t)each[i]*gains[i],1073741824);}
   for(i=3;i>0;i--)reverse+=form_down((int64_t)each[i-1]*gains[i-1],1073741824);
   if(sum>INT32_MAX||sum<-(int64_t)INT32_MAX)sum_hits++;
   form_input(&v,input);fm_render_q30(&v,out,1);BOUND_CHECK(&bounds, out[0]==form_clip(sum)&&sum==reverse);
   for(i=0;i<3;i++)BOUND_CHECK(&bounds, form_states_equal(&model[i],&v.formant[i].svf));
  }
  for(i=0;i<3;i++)byq[qi][i]+=hits[i]-start[i];
 }
 /* Opposite-sign saturated BP outputs must cancel before the single sum
  * clamp. This also pins negative gain multiplication's floor convention. */
 for(k=0;k<27;k++)for(j=0;j<3;j++){
  int64_t sum=0;int32_t states[]={-INT32_MAX,-1,INT32_MAX};unsigned code=k;
  p=form_patch();fm_voice_init(&v,&p,fs);
  for(i=0;i<3;i++){fm_formant_t*f=&v.formant[i];f->svf.a1=1073741824;f->svf.a2=f->svf.a3=0;f->svf.ic1=states[code%3];f->svf.ic2=0;f->gain_q30=j==0?1073741824:j==1?536870912:123456789;sum+=form_down((int64_t)f->svf.ic1*f->gain_q30,1073741824);code/=3;}
  form_input(&v,0);fm_render_q30(&v,out,1);BOUND_CHECK(&bounds, out[0]==form_clip(sum));
 }
 /* Independent bank and downstream ring recurrences, including a bank whose
  * last nonzero returned sample clears all its own integrators. */
 for(mode=0;mode<=4;mode++)for(stages=1;stages<=2;stages++)for(k=0;k<5;k++){
  fm_svf_t bank[3],a,b;int32_t gains[3];
  p=form_patch();p.filter=(fm_filter_patch_t){1000000,16000,mode,stages};p.lfo.rate_millihz=1000;p.lfo.free_run=2;p.lfo.wave=6;
  fm_voice_init(&v,&p,fs);v.ctl=1;for(i=0;i<3;i++){v.formant[i].svf.ic1=k==0?0:k==1?1:k==2?2048:k==3?100000000:INT32_MAX;v.formant[i].svf.ic2=k==4?-INT32_MAX:0;v.formant[i].svf.idle=k==4?262143-i:0;bank[i]=v.formant[i].svf;gains[i]=v.formant[i].gain_q30;}
  a=v.svf;b=v.svf2;
  for(n=0;n<600;n++){
   int had=0,after=0,down_had=a.ic1||a.ic2||b.ic1||b.ic2;int32_t expect;int64_t sum=0;uint32_t idle_before=a.idle;
   for(i=0;i<3;i++){had|=bank[i].ic1||bank[i].ic2;sum+=form_down((int64_t)form_model_ring(&bank[i],hits)*gains[i],1073741824);after|=bank[i].ic1||bank[i].ic2;}
   expect=form_clip(sum);
   if(mode){if(had){expect=form_model(&a,expect,hits);if(stages==2)expect=form_model(&b,expect,hits);}
    else if(stages==1)expect=form_model_ring(&a,hits);
    else if(down_had){int stop;expect=form_model(&b,form_model(&a,0,hits),hits);stop=form_quiet(a.ic1)&&form_quiet(a.ic2)&&form_quiet(b.ic1)&&form_quiet(b.ic2);if(!stop){a.idle++;stop=a.idle>=262144;}if(stop)a.ic1=a.ic2=b.ic1=b.ic2=0;}
    else expect=0;
   }
   BOUND_CHECK(&bounds, fm_voice_active(&v)==!!(had||down_had));fm_render_q30(&v,out,1);BOUND_CHECK(&bounds, out[0]==expect);
   for(i=0;i<3;i++)BOUND_CHECK(&bounds, form_states_equal(&bank[i],&v.formant[i].svf));
   BOUND_CHECK(&bounds, form_states_equal(&a,&v.svf)&&form_states_equal(&b,&v.svf2));
   BOUND_CHECK(&bounds, v.ctl==1&&v.idle_samples==n+1);
   if(had&&!after&&sum){final_tail++;if(mode)BOUND_CHECK(&bounds, v.svf.idle==idle_before);}
   if(!had&&down_had)next_ring++;
  }
 }
 BOUND_CHECK(&bounds, final_tail>0&&next_ring>0);
 /* Note/retrigger preserves integrators and changes coefficients only for
  * changed effective centers; unrelated final-filter events stay isolated. */
 for(shift=0;shift<16;shift++){
  p=form_patch();p.mod[0]=(fm_mod_slot_patch_t){300000,16,36,1,shift,{0,0,0}};p.mod[1]=(fm_mod_slot_patch_t){1200,17,37,1,shift,{0,0,0}};
  memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);fm_voice_ctl(&v,0,32768);ref_voice_ctl(&r,0,32768);fm_voice_ctl(&v,1,-32768);ref_voice_ctl(&r,1,-32768);
  for(i=0;i<3;i++){v.formant[i].svf.ic1=12345+i;v.formant[i].svf.ic2=-23456-(int32_t)i;v.formant[i].svf.idle=262143;}
  fm_voice_note_on(&v,220000,32767);ref_voice_note_on(&r,220000,32767);
  for(i=0;i<3;i++)BOUND_CHECK(&bounds, v.formant[i].svf.ic1==12345+(int32_t)i&&v.formant[i].svf.ic2==-23456-(int32_t)i&&!v.formant[i].svf.idle);
  BOUND_CHECK(&bounds, v.formant[1].center_millihz==(shift?1500000:1800000)&&r.formant[1].center_millihz==v.formant[1].center_millihz);
  for(n=0;n<24;n++){fm_render_q30(&v,out,64);ref_render(&r,real,64);BOUND_CHECK(&bounds, v.formant[1].center_millihz==r.formant[1].center_millihz&&v.formant[1].level_db10==r.formant[1].level_db10);}
  w=v;fm_voice_filter(&v,3300000,500);for(i=0;i<3;i++)BOUND_CHECK(&bounds, formant_state_equal(&v.formant[i],&w.formant[i]));
  /* Same center avoids unnecessary divide/coefficient refresh. */
  fm_voice_ctl(&v,0,32768);v.formant[0].svf.a1=1234;fm_render_q30(&v,out,64);BOUND_CHECK(&bounds, v.formant[0].svf.a1==1234);
 }
 /* Full voice copies, sounding/release/idle and every output stage. */
 for(mode=0;mode<=4;mode++)for(stages=1;stages<=2;stages++){
  p=form_patch();p.filter=(fm_filter_patch_t){1000000,707,mode,stages};p.shape=(fm_shape_patch_t){60,2,0};p.lfo.rate_millihz=1000;p.lfo.free_run=2;p.lfo.wave=1;
  p.mod[0]=(fm_mod_slot_patch_t){300000,0,36,1,3,{0,0,0}};memset(&v,0,sizeof v);fm_voice_init(&v,&p,fs);w=v;
  for(n=0;n<40;n++){if(n==1||n==30){fm_voice_note_on(&v,220000,32767);fm_voice_note_on(&w,220000,32767);}if(n==8){fm_voice_note_off(&v);fm_voice_note_off(&w);}
   fm_render_q30(&v,out,257);for(i=0;i<257;i++)fm_render_q30(&w,other+i,1);BOUND_CHECK(&bounds, !memcmp(out,other,sizeof out)&&mod_state_equal(&v,&w));}
 }
 printf("FORMANTBOUND fs=%u phase64=%d checks=%u failures=%u final_tail=%u downstream_ring=%u sum_clamps=%llu Q500_v3_ic1_ic2=%llu,%llu,%llu Q16000_v3_ic1_ic2=%llu,%llu,%llu\n",fs,FM_PHASE64,bounds.checks,bounds.bad,final_tail,next_ring,(unsigned long long)sum_hits,(unsigned long long)byq[0][0],(unsigned long long)byq[0][1],(unsigned long long)byq[0][2],(unsigned long long)byq[1][0],(unsigned long long)byq[1][1],(unsigned long long)byq[1][2]);
 return bounds.bad;
}

static unsigned vector_corners(uint32_t fs)
{
 unsigned i,j,n,bad=0;
 {
  fm_voice_t v[4],single[4];ref_voice_t r[4];uint64_t hash=14695981039346656037ULL;
  double err=0,qerr=0,sig=0,peak=0;unsigned clipped=0;
  memset(v,0,sizeof v);memset(r,0,sizeof r);memset(single,0,sizeof single);
  for(i=0;i<4;i++){
   fm_patch_t p={0};p.route.ops=4;p.route.carriers=15;p.vector=(fm_vector_patch_t){i&1?32768:0,i&2?32768:0,1};
   for(j=0;j<4;j++){p.op[j].ratio_q16=(j+1)*65536;p.op[j].level_db10=180;p.op[j].attack_ms=5;p.op[j].decay_ms=100;p.op[j].release_ms=300;}
   fm_voice_init(&v[i],&p,fs);ref_voice_init(&r[i],&p,fs);p.vector.enabled=0;p.route.carriers=(uint8_t)(1u<<i);fm_voice_init(&single[i],&p,fs);
  }
  for(n=0;n<2*fs;n++){
   int16_t out,control;double real=0,ideal,error,qerror;
   for(i=0;i<4;i++){
    if(n==i*fs/2){fm_voice_note_on(&v[i],220000,32767);ref_voice_note_on(&r[i],220000,32767);fm_voice_note_on(&single[i],220000,32767);}
    if(n==i*fs/2+fs*150/1000){fm_voice_note_off(&v[i]);ref_voice_note_off(&r[i]);fm_voice_note_off(&single[i]);}
    ref_render(&r[i],&ideal,1);real+=ideal;
   }
   clipped+=fm_mix_i16(v,4,&out,1);fm_mix_i16(single,4,&control,1);if(out!=control)bad++;
   if(real>32767)real=32767;if(real< -32768)real=-32768;
   ideal=floor(real+.5);error=out-real;qerror=ideal-real;err+=error*error;qerr+=qerror*qerror;sig+=real*real;if(fabs(error)>peak)peak=fabs(error);
   hash=(hash^(uint8_t)out)*1099511628211ULL;hash=(hash^(uint8_t)((uint16_t)out>>8))*1099511628211ULL;
  }
  {double snr=10*log10(sig/err),ceiling=10*log10(sig/qerr);printf("VECTORCORNERS phase=%d fs=%u hash=%016llx loss_db=%.9f peak_lsb=%.9f clipped=%u corner_diff=%u\n",FM_PHASE64,fs,(unsigned long long)hash,ceiling-snr,peak,clipped,bad);if(ceiling-snr>.10||peak>1||clipped)bad++;}
 }
 return bad;}

static int32_t gen_cache_clamp(int32_t x)
{
    return x < -32768 ? -32768 : x > 32768 ? 32768 : x;
}
static unsigned gen_cache_bounds(uint32_t fs)
{
    bound_t bounds = { "GENCACHE-FAIL", 0, 0 };
    static const int32_t values[]={INT32_MIN,-32769,-32768,-1,0,1,32767,32768,32769,INT32_MAX};
    fm_patch_t p={0};unsigned i,j,shift,block;
    p.route.ops=FM_OPS;p.route.carriers=(uint8_t)((1u<<FM_OPS)-1u);
    for(i=0;i<FM_OPS;i++){p.op[i].ratio_q16=(i+1)*65536;p.op[i].attack_ms=2;p.op[i].decay_ms=100;p.op[i].release_ms=1;p.op[i].level_db10=240;}
    {
        fm_voice_t v,w;ref_voice_t r,s;
        memset(&v,0,sizeof v);memset(&r,0,sizeof r);
        fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
        for(i=0;i<FM_GEN_INPUTS;i++)BOUND_CHECK(&bounds, v.gen_input[i]==0&&r.gen_input[i]==0);
        for(i=0;i<FM_GEN_INPUTS;i++)for(j=0;j<sizeof values/sizeof values[0];j++) {
            w=v;s=r;w.gen_input[i]=s.gen_input[i]=gen_cache_clamp(values[j]);
            fm_voice_gen(&v,i,values[j]);ref_voice_gen(&r,i,values[j]);
            BOUND_CHECK(&bounds, mod_state_equal(&v,&w)&&wide_ref_state_equal(&r,&s));
        }
        w=v;s=r;fm_voice_gen(&v,8,123);fm_voice_gen(&v,UINT32_MAX,123);ref_voice_gen(&r,8,123);ref_voice_gen(&r,UINT32_MAX,123);
        BOUND_CHECK(&bounds, mod_state_equal(&v,&w)&&wide_ref_state_equal(&r,&s));
        BOUND_CHECK(&bounds, !v.mod_active&&!v.ctl&&!fm_voice_active(&v));
        fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
        for(i=0;i<FM_GEN_INPUTS;i++)BOUND_CHECK(&bounds, v.gen_input[i]==0&&r.gen_input[i]==0);
    }
    for(shift=0;shift<16;shift++) {
        fm_patch_t q=p;fm_voice_t a,b;ref_voice_t r,s;int32_t x[257],y[257];double rx[257],ry[257];
        for(i=0;i<8;i++){
            p.mod[i]=(fm_mod_slot_patch_t){120,(uint16_t)(64+i),2,(uint8_t)(i%FM_OPS),(uint8_t)shift,{0,0,0}};
            q.mod[i]=p.mod[i];q.mod[i].source=(uint16_t)(16+i);
        }
        memset(&a,0,sizeof a);memset(&b,0,sizeof b);memset(&r,0,sizeof r);memset(&s,0,sizeof s);
        fm_voice_init(&a,&p,fs);fm_voice_init(&b,&q,fs);ref_voice_init(&r,&p,fs);ref_voice_init(&s,&q,fs);
        BOUND_CHECK(&bounds, a.mod_active==255&&r.mod_active==255);
        fm_voice_note_on(&a,220000,32767);fm_voice_note_on(&b,220000,32767);ref_voice_note_on(&r,220000,32767);ref_voice_note_on(&s,220000,32767);
        for(i=0;i<8;i++)BOUND_CHECK(&bounds, a.mod_cur[i]==0&&r.mod_cur[i]==0);
        for(block=0;block<80;block++) {
            for(i=0;i<8;i++) {
                int32_t value=values[(block+i)%10];
                fm_voice_gen(&a,i,value);fm_voice_ctl(&b,i,value);ref_voice_gen(&r,i,value);ref_voice_ctl(&s,i,value);
            }
            if(block==10||block==65){
                fm_voice_note_on(&a,330000,23000);fm_voice_note_on(&b,330000,23000);ref_voice_note_on(&r,330000,23000);ref_voice_note_on(&s,330000,23000);
            }
            if(block==30){fm_voice_note_off(&a);fm_voice_note_off(&b);ref_voice_note_off(&r);ref_voice_note_off(&s);}
            fm_render_q30(&a,x,257);ref_render(&r,rx,257);
            for(i=0;i<257;i++){fm_render_q30(&b,y+i,1);ref_render(&s,ry+i,1);}
            BOUND_CHECK(&bounds, !memcmp(x,y,sizeof x)&&!memcmp(rx,ry,sizeof rx));
            BOUND_CHECK(&bounds, !memcmp(a.mod_cur,b.mod_cur,sizeof a.mod_cur)&&!memcmp(a.mod_cur,r.mod_cur,sizeof a.mod_cur)&&!memcmp(r.mod_cur,s.mod_cur,sizeof r.mod_cur));
            BOUND_CHECK(&bounds, !memcmp(a.mod_level,b.mod_level,sizeof a.mod_level)&&!memcmp(a.mod_level,r.mod_level,sizeof a.mod_level));
            for(i=0;i<8;i++)BOUND_CHECK(&bounds, a.gen_input[i]==gen_cache_clamp(values[(block+i)%10])&&r.gen_input[i]==a.gen_input[i]&&!a.ctl_input[i]&&!r.ctl_input[i]);
            {
                fm_voice_t aa=a,bb=b;ref_voice_t rr=r,ss=s;
                /* Source-number and input-bank placement are the only
                 * intended state differences between these render twins. */
                for(i=0;i<8;i++){
                    aa.patch.mod[i].source=bb.patch.mod[i].source;
                    rr.patch.mod[i].source=ss.patch.mod[i].source;
                    aa.gen_input[i]=bb.gen_input[i]=aa.ctl_input[i]=bb.ctl_input[i]=0;
                    rr.gen_input[i]=ss.gen_input[i]=rr.ctl_input[i]=ss.ctl_input[i]=0;
                }
                BOUND_CHECK(&bounds, mod_state_equal(&aa,&bb));
                BOUND_CHECK(&bounds, wide_ref_state_equal(&rr,&ss));
            }
        }
    }
    {
        static const unsigned invalid[]={24,63,72,65535};fm_voice_t v;ref_voice_t r;
        memset(p.mod,0,sizeof p.mod);
        for(i=0;i<sizeof invalid/sizeof invalid[0];i++) {
            p.mod[0]=(fm_mod_slot_patch_t){120,(uint16_t)invalid[i],2,0,0,{0,0,0}};
            fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);BOUND_CHECK(&bounds, !v.mod_active&&!r.mod_active);
        }
        p.mod[0]=(fm_mod_slot_patch_t){0,64,2,0,0,{0,0,0}};
        fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);BOUND_CHECK(&bounds, !v.mod_active&&!r.mod_active&&v.patch.mod[0].source==64&&r.patch.mod[0].source==64);
    }
    printf("GENCACHE fs=%u phase64=%d checks=%u failures=%u\n",fs,FM_PHASE64,bounds.checks,bounds.bad);
    return bounds.bad;
}

static uint64_t wide_phase(uint64_t x){
#if FM_PHASE64
 return x;
#else
 return (x+(UINT64_C(1)<<31))>>32;
#endif
}
static fm_patch_t wide_patch(void){fm_patch_t p={0};unsigned i;
 p.op[0].ratio_q16=65536;p.op[1].ratio_q16=131072;
 p.op[0].attack_ms=p.op[1].attack_ms=2;p.op[0].level_db10=120;
 p.op[0].release_ms=p.op[1].release_ms=1;
 for(i=0;i<4;i++)p.peg.level_cents[i]=-4800;
 p.peg.mask=1;
 p.lfo.rate_millihz=10;p.lfo.wave=4;p.lfo.pitch_cents=1200;p.lfo.mask_pitch=1;p.lfo.phase_u16=32768;
 p.mod[0]=(fm_mod_slot_patch_t){4800,16,1,0,0,{0,0,0}};return p;
}
static unsigned wide_bounds(uint32_t fs){
    bound_t bounds = { "WIDEFAIL", 0, 0 };const unsigned bases[]={0,1,15,16,32,255,256};unsigned n,mode,i;int32_t a[257],b[257];
 for(n=0;n<sizeof bases/sizeof*bases;n++){
  fm_patch_t p=wide_patch();fm_voice_t v,w;ref_voice_t r;uint64_t want;
  p.op[0].ratio_q16=bases[n]*65536u;
  memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
  fm_voice_ctl(&v,0,-32768);ref_voice_ctl(&r,0,-32768);fm_voice_note_on(&v,fs*1000,32767);ref_voice_note_on(&r,fs*1000,32767);
  want=bases[n]>=256?(UINT64_C(1)<<63)-65536:(uint64_t)bases[n]<<55;
  BOUND_CHECK(&bounds, v.peg_base_whole[0]==bases[n]);BOUND_CHECK(&bounds, !v.peg_base_frac[0]);
  BOUND_CHECK(&bounds, v.peg.cur==-4800&&v.lfo_cents==-1200&&v.mod_pitch[0]==-4800);
  BOUND_CHECK(&bounds, v.op[0].inc==wide_phase(want));BOUND_CHECK(&bounds, r.op[0].inc==(bases[n]>=256?.5:(double)bases[n]/512));
  /* A cached-pitch event recalculates the wide base without stepping any
   * control source or smoother. Its one-mHz neighbor gives fractional turns. */
  w=v;fm_voice_pitch(&v,fs*1000-1);
  BOUND_CHECK(&bounds, v.ctl==w.ctl&&v.lfo_phase==w.lfo_phase&&!memcmp(v.mod_cur,w.mod_cur,sizeof v.mod_cur));
  BOUND_CHECK(&bounds, v.peg_base_whole[0]==(bases[n]?bases[n]-1:0));
  if(bases[n])BOUND_CHECK(&bounds, v.peg_base_frac[0]!=0);
  /* Clamp reached from the positive extreme must happen before multiplying. */
  v.peg.cur=4800;v.mod_pitch[0]=4800;v.lfo_cents=1200;fm_voice_pitch(&v,fs*1000);
  BOUND_CHECK(&bounds, v.op[0].inc==wide_phase(bases[n]?(UINT64_C(1)<<63)-65536:0));
 }
 for(mode=0;mode<3;mode++){
  fm_patch_t p=wide_patch();fm_voice_t v,w;ref_voice_t r,s;double ra[257],rb[257];
  p.op[0].ratio_q16=32*65536u;p.op[0].free_run=2;p.lfo.free_run=mode==2?2:0;
  if(mode==1)p.mod[0].amount=0;
  memset(&v,0,sizeof v);memset(&r,0,sizeof r);fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
  fm_voice_ctl(&v,0,-32768);ref_voice_ctl(&r,0,-32768);fm_render_q30(&v,a,137);ref_render(&r,ra,137);
  fm_voice_note_on(&v,48000000,32767);ref_voice_note_on(&r,48000000,32767);w=v;s=r;
  if(mode==1)BOUND_CHECK(&bounds, v.peg_base_whole[0]==16&&v.mod_pitch_mask==0);
  for(n=0;n<30;n++){
   if(n==3){fm_voice_pitch(&v,47000000);fm_voice_pitch(&w,47000000);ref_voice_pitch(&r,47000000);ref_voice_pitch(&s,47000000);}
   if(n==8){fm_voice_note_off(&v);fm_voice_note_off(&w);ref_voice_note_off(&r);ref_voice_note_off(&s);}
   if(n==20){fm_voice_note_on(&v,48000000,23000);fm_voice_note_on(&w,48000000,23000);ref_voice_note_on(&r,48000000,23000);ref_voice_note_on(&s,48000000,23000);}
   fm_render_q30(&v,a,257);ref_render(&r,ra,257);
   for(i=0;i<257;i++){fm_render_q30(&w,b+i,1);ref_render(&s,rb+i,1);}
   BOUND_CHECK(&bounds, !memcmp(a,b,sizeof a)&&mod_state_equal(&v,&w));BOUND_CHECK(&bounds, !memcmp(ra,rb,sizeof ra)&&wide_ref_state_equal(&r,&s));
  }
 }
 {
  fm_patch_t p=wide_patch();fm_voice_t v;
  p.op[0].ratio_q16=32;fm_voice_init(&v,&p,fs);fm_voice_ctl(&v,0,-32768);fm_voice_note_on(&v,fs*1000,32767);
  BOUND_CHECK(&bounds, v.peg_base_whole[0]==0&&v.peg_base_frac[0]==(UINT64_C(1)<<53));
  BOUND_CHECK(&bounds, v.op[0].inc==wide_phase(UINT64_C(1)<<44));
  v.peg.cur=4800;v.mod_pitch[0]=4800;v.lfo_cents=1200;fm_voice_pitch(&v,fs*1000);
  BOUND_CHECK(&bounds, v.op[0].inc==wide_phase(UINT64_C(1)<<62));
 }
 {
  fm_patch_t p=wide_patch();fm_voice_t v;int delta;int32_t out;
  /* Positive factor128: PEG+4800, matrix+4800, LFO-1200. Inject
   * adjacent raw bases around2^56, then let the public renderer tick. */
  for(i=0;i<4;i++)p.peg.level_cents[i]=4800;
  for(delta=-1;delta<=1;delta++){
   uint64_t raw=(UINT64_C(1)<<56)+(int64_t)delta;
   fm_voice_init(&v,&p,fs);fm_voice_ctl(&v,0,32768);fm_voice_note_on(&v,fs*1000,32767);
   v.peg_base_whole[0]=0;v.peg_base_frac[0]=raw;v.ctl=1;fm_render_q30(&v,&out,1);
   BOUND_CHECK(&bounds, v.peg.cur==4800&&v.mod_pitch[0]==4800&&v.lfo_cents==-1200);
   BOUND_CHECK(&bounds, v.op[0].inc==wide_phase(delta<0?raw<<7:(UINT64_C(1)<<63)-65536));
  }
 }
 {
  fm_patch_t p=wide_patch();fm_voice_t v;p.op[0].ratio_q16=UINT32_MAX;
  fm_voice_init(&v,&p,UINT32_MAX);fm_voice_ctl(&v,0,-32768);fm_voice_note_on(&v,UINT32_MAX,32767);
  /* freq==fs cancels: base=UINT32_MAX/65536000. These integer pins
   * are floor((35127295<<64)/65536000) and floor(base_raw/512). */
  BOUND_CHECK(&bounds, v.peg_base_whole[0]==65&&v.peg_base_frac[0]==UINT64_C(9887454542033342955));
  BOUND_CHECK(&bounds, v.op[0].inc==wide_phase(UINT64_C(2361183240885066792)));
  fm_voice_init(&v,&p,0);fm_voice_ctl(&v,0,-32768);fm_voice_note_on(&v,UINT32_MAX,32767);
  BOUND_CHECK(&bounds, v.peg_base_whole[0]==256&&v.op[0].inc==wide_phase((UINT64_C(1)<<63)-65536));
 }
 printf("WIDEBOUND fs=%u phase64=%d checks=%u failures=%u\n", fs, FM_PHASE64, bounds.checks, bounds.bad);return bounds.bad;
}



/* Host-only  note-source precision diagnostic, reached through public APIs.
 * h=2^-EXP_BITS. Linear 2^-t interpolation error C=(ln2)^2*h*h/8;
 * table rounding adds 2^-31. At a note source's unclamped +/-8-octave
 * range, normalized Q48 ratio truncation adds at most 2^-40. Inverse
 * slope is at most 2/ln2, final inverse truncation 2^-48, and dropping
 * octaves to Q32 adds 2^-32 before conversion to cents. Therefore
 * tau_cents=1200*(2*(C+2^-31+2^-40)/ln2 + 2^-48 + 2^-32).
 * Integer-cent rounding then Q15 normalization are TWO rounding steps.
 * Bound the allowed integer cents first, then normalize each endpoint;
 * a blanket one-Q15 window would incorrectly reject valid 3/4-unit cases.
 * No source, smoother or offset is copied into the reference. */
static int32_t mod_note_norm_check(int32_t cents)
{
    double q = floor(fabs((double)cents) * 32768.0 / 9600.0 + 0.5);
    if (q > 32768.0) q = 32768.0;
    return cents < 0 ? -(int32_t)q : (int32_t)q;
}
static int mod_note_accept(int32_t value,int32_t lo,int32_t hi)
{
    return value >= lo && value <= hi;
}
static unsigned mod_note_bounds(uint32_t fs)
{
    static const uint32_t pins[] = { 0u,1u,1718u,1719u,440000u,112640000u,UINT32_MAX,
                                     32562933u,76691357u,15182373u,51869888u,33984981u };
    const double ln2 = log(2.0), h = ldexp(1.0,-FM_EXP_BITS);
    const double tau = 1200.0 * (2.0 * (ln2*ln2*h*h/8.0+ldexp(1.0,-31)+ldexp(1.0,-40))/ln2
                                  + ldexp(1.0,-48) + ldexp(1.0,-32));
    fm_patch_t p = { 0 }; fm_voice_t v; ref_voice_t r;
    unsigned n, checks=0, bad=0, differences=0, rejected=0;
    uint32_t rng=0xabcdef01u;
    p.op[0].ratio_q16=65536u; p.op[1].ratio_q16=131072u;
    p.mod[0]=(fm_mod_slot_patch_t){120,8,2,0,0,{0,0,0}};
    memset(&v,0,sizeof v);memset(&r,0,sizeof r);
    fm_voice_init(&v,&p,fs);ref_voice_init(&r,&p,fs);
    for(n=0;n<sizeof pins/sizeof pins[0]+1024u;n++) {
        uint32_t f;double real,allowance;int32_t lo,hi,ql,qh,expected;
        rng^=rng<<13;rng^=rng>>17;rng^=rng<<5;
        f=n<sizeof pins/sizeof pins[0]?pins[n]:1u+rng%112640000u;
        real=f?1200.0*log2((double)f/440000.0):-9600.0;
        if (real < -9600.0) real = -9600.0;
        if (real > 9600.0) real = 9600.0;
        allowance=tau+32.0*DBL_EPSILON*(1.0+fabs(real));
        lo=(int32_t)ceil(real-allowance-0.5);hi=(int32_t)floor(real+allowance+0.5);
        ql=mod_note_norm_check(lo);qh=mod_note_norm_check(hi);
        expected=mod_note_norm_check(real<0?-(int32_t)floor(-real+0.5):(int32_t)floor(real+0.5));
        fm_voice_note_on(&v,f,32767);ref_voice_note_on(&r,f,32767);
        checks++;if(!mod_note_accept(v.mod_note,ql,qh)||v.mod_cur[0]!=v.mod_note||r.mod_note!=expected||r.mod_cur[0]!=expected)bad++;
        if(v.mod_note!=expected) {
            differences++;
            printf("MODNOTE-DIFF freq=%u engine=%d ideal=%d window=%d..%d cents_tau=%.12g\n",f,v.mod_note,expected,ql,qh,allowance);
        }
        /* Values immediately beyond each derived interval must be refused. */
        checks+=2;rejected+=!mod_note_accept(ql-1,ql,qh);rejected+=!mod_note_accept(qh+1,ql,qh);
    }
    if(rejected!=2u*(sizeof pins/sizeof pins[0]+1024u))bad++;
    printf("MODNOTE checks=%u differences=%u outside_refusals=%u fails=%u\n",checks,differences,rejected,bad);
    return bad;
}

static unsigned cascade_bounds(uint32_t fs)
{
    cascade_check_t context = {
        0
    }, *ctx = &context;
    ctx->bounds.tag = "CASCADEBOUND-FAIL";
    cascade_check_bounds(ctx);
    cascade_check_grid(ctx, fs);
    cascade_check_ring(ctx, fs);
    cascade_check_events(ctx, fs);
    cascade_check_public_order(ctx, fs);
    cascade_check_blocks(ctx, fs);
    printf("CASCADEBOUND fs=%u phase64=%d checks=%llu failures=%llu\n", fs, FM_PHASE64, (unsigned long long)ctx->bounds.checks, (unsigned long long)ctx->bounds.bad);
    return ctx->bounds.bad != 0;
}

static unsigned wave_bounds(uint32_t fs)
{
    bound_t bounds = { "WAVEBOUND-FAIL", 0, 0 };
    fm_patch_t p = { 0 };
    fm_voice_t base;
    uint32_t rng = 0x2F619AD5u;
    unsigned w, i;
    const double pi = acos(-1.0), n = ldexp(1.0, FM_SINE_BITS);
    /* Piecewise-linear sine interpolation: max |f''| h^2 / 8,
     * plus half a Q30 unit per rounded table node and <1 floor unit. */
    const double bound = 1073741824.0 * pi * pi / (2.0*n*n) + 1.5;
    p.route.ops = p.route.carriers = 1;
    p.op[0].ratio_q16 = 65536;
    fm_voice_init(&base, &p, fs);
    fm_voice_note_on(&base, 0, 32767);
    base.op[0].stage = FM_STAGE_SUSTAIN;
    base.op[0].att = 0;
    base.op[0].inc = 0;
    for (i = 0; i < 24u + 4096u; ++i) {
        uint32_t phase, pm, target;
        int32_t sine, got, want;
        double ideal;
        if (i < 24u) {
            phase = (uint32_t)(i / 3u) * 0x20000000u + (i % 3u) - 1u;
            pm = 0;
        } else {
            rng = rng * 1664525u + 1013904223u; phase = rng;
            rng = rng * 1664525u + 1013904223u; pm = rng & 0xFFFFFF00u;
        }
        target = phase + pm;
        sine = wave_bound_read(&base, target << 1, 0, 0);
        for (w = 5; w <= 6; ++w) {
            got = wave_bound_read(&base, phase, w, pm);
            want = target & 0x80000000u ? 0 : sine;
            if (w == 6u && want < 0) want = -want;
            BOUND_CHECK(&bounds, !(got != want));
            BOUND_CHECK(&bounds, !(got < -1073741824 || got > 1073741824 || (w == 6u && got < 0)));
            ideal = target & 0x80000000u ? 0.0 : sin(4.0*pi*(double)target/4294967296.0);
            if (w == 6u) ideal = fabs(ideal);
            BOUND_CHECK(&bounds, !(fabs((double)got - 1073741824.0*ideal) > bound));
            if (i < 24u && i % 3u == 1u) {
                static const int32_t vertices[8] = { 0, 1073741824, 0, -1073741824, 0, 0, 0, 0 };
                want = vertices[i/3u]; if (w == 6u && want < 0) want = -want;
                BOUND_CHECK(&bounds, !(got != want));
            }
        }
        want = wave_bound_read(&base, phase, 0, pm);
        BOUND_CHECK(&bounds, !(wave_bound_read(&base, phase, 12, pm) != want));
        BOUND_CHECK(&bounds, !(wave_bound_read(&base, phase, 65535, pm) != want));
    }
    for (w = 5; w <= 6; ++w) {
        fm_voice_t v, twin;
        int32_t out, a[67], b[67];
        uint64_t expected, inc;
        uint32_t noise;
        p.op[0].wave = (uint16_t)w; p.op[0].free_run = 2;
        p.op[0].fixed_millihz = 333000; p.op[0].phase_u16 = 12345;
        p.op[0].release_ms = 1;
        fm_voice_init(&v, &p, fs);
        expected = v.op[0].phase; inc = v.op[0].inc; noise = v.op[0].noise;
        for (i = 0; i < 129; ++i) { fm_render_q30(&v, &out, 1); expected += inc; }
#if !FM_PHASE64
        expected = (uint32_t)expected;
#endif
        fm_voice_note_on(&v, 220000, 32767);
        BOUND_CHECK(&bounds, !(v.op[0].phase != expected));
        for (i = 0; i < 257; ++i) {
            fm_render_q30(&v, &out, 1); expected += inc;
#if !FM_PHASE64
            expected = (uint32_t)expected;
#endif
            BOUND_CHECK(&bounds, !(v.op[0].phase != expected || v.op[0].noise != noise));
        }
        twin = v;
        for (i = 0; i < 67; ++i) fm_render_q30(&v, &a[i], 1);
        fm_render_q30(&twin, b, 67);
        BOUND_CHECK(&bounds, !(memcmp(a, b, sizeof a) || v.op[0].phase != twin.op[0].phase || v.op[0].prev != twin.op[0].prev));
        expected = v.op[0].phase;
        fm_voice_note_off(&v);
        for (i = 0; i < 128; ++i) { fm_render_q30(&v, &out, 1); expected += inc; }
        BOUND_CHECK(&bounds, !(fm_voice_active(&v)));
        for (i = 0; i < 193; ++i) { fm_render_q30(&v, &out, 1); expected += inc; }
#if !FM_PHASE64
        expected = (uint32_t)expected;
#endif
        fm_voice_note_on(&v, 440000, 32767);
        BOUND_CHECK(&bounds, !(v.op[0].phase != expected || v.op[0].noise != noise));
    }
    {   /* Raw API rates/notes/ratios cannot escape the waveform bounds. */
        const uint32_t rates[] = { 0u, 1u, fs, UINT32_MAX };
        unsigned ri, ni, qi;
        for (ri = 0; ri < 4; ++ri) for (ni = 0; ni < 2; ++ni) for (qi = 0; qi < 2; ++qi)
            for (w = 5; w <= 6; ++w) {
                fm_voice_t v;
                int32_t out;
                p.op[0].wave = (uint16_t)w; p.op[0].free_run = 0;
                p.op[0].fixed_millihz = 0; p.op[0].ratio_q16 = qi ? UINT32_MAX : 0u;
                fm_voice_init(&v, &p, rates[ri]);
                fm_voice_note_on(&v, ni ? UINT32_MAX : 0u, 32767);
                v.op[0].stage = FM_STAGE_SUSTAIN; v.op[0].att = 0;
                for (i = 0; i < 129; ++i) {
                    fm_render_q30(&v, &out, 1);
                    BOUND_CHECK(&bounds, !(v.op[0].prev < -1073741824 || v.op[0].prev > 1073741824
                        || (w == 6u && v.op[0].prev < 0)));
                }
            }
    }
    printf("WAVEBOUND fs=%u phase64=%d sine_bits=%d checks=%u failures=%u\n", fs, FM_PHASE64, FM_SINE_BITS, bounds.checks, bounds.bad);
    return bounds.bad;
}

/*  left key scaling: independent logarithmic oracle and an exact
 * swapped-input legacy twin; modulation reuses the note-on gain. */
static unsigned ks_left_bounds(uint32_t fs)
{
    bound_t bounds = { "KSLEFTBOUND-FAIL", 0, 0 };
    static const uint32_t freqs[] = { 0, 1, 27500, 220000, 440000, 660000, 880000, UINT32_MAX };
    static const uint32_t breaks[] = { 0, 440000, UINT32_MAX };
    static const uint16_t slopes[] = { 0, 1, 120, 1200, UINT16_MAX };
    const double ln2 = log(2.0), db_oct = 200.0 * log10(2.0);
    const double h = ldexp(1.0, -FM_EXP_BITS);
    const double curve = ln2 * ln2 * h * h / 8.0;
    /* Q48 ratio truncation normalized through at most 20 octaves is
     * below 2^-28. The inverse exp table has slope magnitude >= ln2/2. */
    const double inverse = 2.0 * (curve + ldexp(1.0,-31) + ldexp(1.0,-28)) / ln2 + ldexp(1.0,-48);
    unsigned fi, bi, si, changed = 0;
    for (fi = 0; fi < sizeof freqs / sizeof freqs[0]; fi++)
        for (bi = 0; bi < sizeof breaks / sizeof breaks[0]; bi++)
            for (si = 0; si < sizeof slopes / sizeof slopes[0]; si++) {
                fm_patch_t p, q; fm_voice_t v, twin; ref_voice_t r;
                const uint32_t f = freqs[fi], b = breaks[bi];
                const uint32_t left = slopes[si] > 1200u ? 1200u : slopes[si];
                const int use_left = left && f < b;
                const uint32_t high = use_left ? b : f, low = use_left ? f : b, slope = use_left ? left : 60;
                double oct = high <= low ? 0.0 : low ? log2((double)high / low) : 20.0;
                double attenuation, ideal, bound;
                int32_t cached, initial_index;
                unsigned j;
                if (oct > 20.0) oct = 20.0;
                attenuation = oct * slope; if (attenuation > 1200.0) attenuation = 1200.0;
                ideal = exp2(-attenuation / db_oct);
                /* Scale inverse error into attenuation octaves, account
                 * for fixed-point division/constant rounding, Q24 round,
                 * then the forward exp table and its floors. */
                bound = ln2 * (slope * inverse / db_oct + ldexp(32.0,-48) + ldexp(1.0,-25))
                    + curve + ldexp(1.0,-31) + ldexp(1.0,-30);
                memset(&p, 0, sizeof p);
                p.op[0].ratio_q16 = 65536; p.op[0].attack_ms = 1;
                p.op[1].ratio_q16 = 65536; p.op[1].fixed_millihz = 1000000;
                p.op[1].index_q10 = 2048; p.op[1].ks_break_millihz = b; p.op[1].ks_slope_db10 = 60;
                p.eg[1].left_slope_db10 = slopes[si];
                p.lfo.rate_millihz = 50000; p.lfo.wave = 4; p.lfo.phase_u16 = 30000;
                p.lfo.index_q10 = 1024; p.lfo.mask_index = 2;
                fm_voice_init(&v, &p, fs); ref_voice_init(&r, &p, fs);
                fm_voice_note_on(&v, f, 32767); ref_voice_note_on(&r, f, 32767);
                cached = v.lfo_ks[1]; initial_index = v.op[1].index;
                BOUND_CHECK(&bounds, !(v.patch.eg[1].left_slope_db10 != left || fabs(cached/1073741824.0-ideal) > bound
                    || fabs(r.ks[1]-ideal) > 32.0*0x1p-52 || (f == b && cached != 1073741824)));
                if (use_left) {
                    q = p; q.eg[1].left_slope_db10 = 0;
                    q.op[1].ks_break_millihz = f; q.op[1].ks_slope_db10 = (uint16_t)left;
                    fm_voice_init(&twin, &q, fs); fm_voice_note_on(&twin, b, 32767);
                    BOUND_CHECK(&bounds, !(cached != twin.lfo_ks[1]));
                }
                q = p; q.eg[1].left_slope_db10 = 0; q.op[1].ks_slope_db10 = 0;
                fm_voice_init(&twin, &q, fs); fm_voice_note_on(&twin, f, 32767);
                for (j = 0; j < 193; j++) {
                    int16_t a, z;
                    const int32_t expected = (int32_t)(((int64_t)twin.op[1].index * cached) >> 30);
                    BOUND_CHECK(&bounds, !(v.lfo_ks[1] != cached || v.op[1].index != expected || v.op[1].out != twin.op[1].out
                        || v.op[1].inc != twin.op[1].inc));
                    if (j == 65u && v.op[1].index != initial_index) changed++;
                    if (j == 97u) {
                        fm_voice_pitch(&v, f ? 0 : UINT32_MAX); fm_voice_pitch(&twin, f ? 0 : UINT32_MAX);
                    }
                    fm_render_i16(&v, &a, 1); fm_render_i16(&twin, &z, 1);
                }
            }
    BOUND_CHECK(&bounds, !(!changed));
    printf("KSLEFTBOUND fs=%u phase64=%d checks=%u changed=%u failures=%u\n", fs, FM_PHASE64, bounds.checks, changed, bounds.bad);
    if (bounds.bad) printf("GATE ksleft FAIL %u\n", bounds.bad);
    return bounds.bad;
}

/*  : level key scaling and the index boost at note-on against
 * the reference's independent double oracle and exact twins; the classic and
 * matrix ticks against level twins; the pre-envelope identity control; the
 * squared source at its extremes (UBSan); canonical values; and fb_special
 * set only by present operators. */
static void ksfb_base(fm_patch_t *p, int routed)
{
    memset(p, 0, sizeof *p);
    p->op[0].ratio_q16 = 65536; p->op[0].level_db10 = 60; p->op[0].attack_ms = 2;
    p->op[0].decay_ms = 100; p->op[0].sustain_db10 = 60; p->op[0].release_ms = 100;
    p->op[1].ratio_q16 = 65536; p->op[1].index_q10 = 1024; p->op[1].attack_ms = 2;
    p->op[1].decay_ms = 100; p->op[1].sustain_db10 = 60; p->op[1].release_ms = 100;
    if (routed) { p->route.ops = 2; p->route.carriers = 1; p->route.w[1][0] = 256; }
}

static unsigned ksfb_render_equal(const fm_patch_t *a, const fm_patch_t *b, uint32_t fs, uint32_t f, unsigned n, unsigned *nonzero)
{
    fm_voice_t va, vb;
    unsigned j, diff = 0;
    fm_voice_init(&va, a, fs); fm_voice_init(&vb, b, fs);
    fm_voice_note_on(&va, f, 32767); fm_voice_note_on(&vb, f, 32767);
    for (j = 0; j < n; j++) {
        int16_t x, y;
        fm_render_i16(&va, &x, 1); fm_render_i16(&vb, &y, 1);
        if (x != y) diff++;
        if (nonzero && x) ++*nonzero;
    }
    return diff;
}

static unsigned ksfb_bounds(uint32_t fs)
{
    bound_t bounds = { "KSFBBOUND-FAIL", 0, 0 };
    /* Keep tests as the historical call/group count, distinct from assertions. */
    static const uint32_t freqs[] = { 27500, 110000, 311127, 440000, 622254, 880000, 1760000, 7040000, 20000000 };
    static const uint16_t slopes[] = { 1, 60, 150, 600, 1200 };
    static const uint16_t indexes[] = { 1, 1024, 8192, 65535 };
    static const uint16_t vels[] = { 32767, 16384 };
    const double pi = 3.14159265358979323846, db_oct = 200.0 * log10(2.0);
    const double cap = 131070.0 / 1024.0 / (2.0 * pi) * 16777216.0;
    unsigned fi, si, side, target, plus, xi, vi, tests = 0, boosted = 0, saturated = 0, levels = 0, identity = 0, nonzero = 0, models = 0;
    for (fi = 0; fi < sizeof freqs / sizeof freqs[0]; fi++)
      for (si = 0; si < sizeof slopes / sizeof slopes[0]; si++)
        for (side = 0; side < 2; side++)
          for (target = 0; target < 3; target++)
            for (plus = 0; plus < 2; plus++)
              for (xi = 0; xi < sizeof indexes / sizeof indexes[0]; xi++)
                for (vi = 0; vi < sizeof vels / sizeof vels[0]; vi++) {
                    fm_patch_t p, q; fm_voice_t v, twin; ref_voice_t r;
                    const uint32_t f = freqs[fi];
                    double a, ideal;
                    unsigned eff_plus;
                    int k;
                    ksfb_base(&p, 1);
                    p.op[1].level_db10 = 300; p.op[1].vel_sens_q8 = 256; p.op[1].index_q10 = indexes[xi];
                    p.op[1].ks_break_millihz = 440000;
                    p.op[1].ks_slope_db10 = side ? 30 : slopes[si];       /* the other side keeps a different slope */
                    p.eg[1].left_slope_db10 = side ? slopes[si] : 30;
                    p.ext[1].ks_target = (uint8_t)target;
                    p.ext[1].ks_curve = (uint8_t)(plus ? (side ? 2u : 1u) : (side ? 1u : 2u));   /* "+" on this side only */
                    fm_voice_init(&v, &p, fs); ref_voice_init(&r, &p, fs);
                    fm_voice_note_on(&v, f, vels[vi]); ref_voice_note_on(&r, f, vels[vi]);
                    /* the side the frequency falls on gives the slope and the sign */
                    eff_plus = (p.ext[1].ks_curve >> (f < 440000u ? 1 : 0)) & 1u;
                    a = f == 440000u ? 0.0 : (double)(f < 440000u ? p.eg[1].left_slope_db10 : p.op[1].ks_slope_db10) * fabs(log2((double)f / 440000.0));
                    if (a > 1200.0) a = 1200.0;
                    tests++;
                    BOUND_CHECK(&bounds, !(target && fabs(fabs(r.ks_db10[1]) - floor(a + 0.5)) > 0.0));          /* the oracle's own distance */
                    /* the level: the engine's rounding equals the oracle's away from a half */
                    if (target && fabs(a - floor(a) - 0.5) > 1e-3) {
                        tests++; levels++;
                        BOUND_CHECK(&bounds, !(v.ks_db10[1] != (int16_t)r.ks_db10[1]));
                    }
                    /* exact twin: the level folded into level_db10, ext cleared, and for
                     * a level-only target or a boost the index slopes cleared */
                    q = p; memset(q.ext, 0, sizeof q.ext);
                    k = 300 + v.ks_db10[1];
                    q.op[1].level_db10 = (uint16_t)(k < 0 ? 0 : k > 1200 ? 1200 : k);
                    if (target == 1u || (eff_plus && target != 1u)) { q.op[1].ks_slope_db10 = 0; q.eg[1].left_slope_db10 = 0; }
                    fm_voice_init(&twin, &q, fs); fm_voice_note_on(&twin, f, vels[vi]);
                    tests++;
                    BOUND_CHECK(&bounds, !(v.op[1].out != twin.op[1].out));
                    if (!(eff_plus && target != 1u) || a == 0.0) {
                        tests++;
                        BOUND_CHECK(&bounds, !(v.op[1].index != twin.op[1].index || v.ks_boost[1] != 0u));
                        continue;
                    }
                    /* the boost: the oracle's 2^(a / 20 log10 2), saturated, then velocity */
                    boosted++;
                    ideal = (double)indexes[xi] / 1024.0 / (2.0 * pi) * 16777216.0 * exp2(a / db_oct);
                    tests += 2;
                    BOUND_CHECK(&bounds, !(fabs(r.op[1].index * 16777216.0 - (ideal > cap ? cap : ideal) * r.vg[1]) > 1e-9 * cap));
                    /* the engine's base index is whole Q24 turns: half an LSB, boosted */
                    BOUND_CHECK(&bounds, !(fabs((double)v.op[1].index - r.op[1].index * 16777216.0) > 1e-5 * r.op[1].index * 16777216.0 + 4.0 + 0.5 * exp2(a / db_oct)));
                    if (ideal >= cap * (1.0 + 1e-5)) {
                        saturated++; tests++;
                        BOUND_CHECK(&bounds, !(vels[vi] == 32767 && fabs((double)v.op[1].index - cap) > 1.0));
                        BOUND_CHECK(&bounds, !(vels[vi] != 32767 && fabs((double)v.op[1].index - cap * r.vg[1]) > 2.0));
                    }
                }
    /* The classic tick (an LFO on the level) and the matrix tick (a slot on
     * the level) form the level as the note-on did: twins with the offset
     * folded into level_db10 render the same samples, "-" and "+". */
    for (plus = 0; plus < 2; plus++)
        for (side = 0; side < 2; side++) {
            fm_patch_t p, q;
            ksfb_base(&p, 1);
            p.op[0].level_db10 = 300; p.op[0].ks_break_millihz = 440000; p.op[0].ks_slope_db10 = 60;
            p.ext[0].ks_target = 2; p.ext[0].ks_curve = (uint8_t)(plus ? 1u : 0u);
            p.lfo.rate_millihz = 7000; p.lfo.wave = 1;
            if (side) { p.mod[0] = (fm_mod_slot_patch_t){ 400, 0, 2, 0, 0, { 0, 0, 0 } }; }
            else { p.lfo.level_db10 = 300; p.lfo.mask_level = 3; }
            q = p; memset(q.ext, 0, sizeof q.ext);
            q.op[0].level_db10 = (uint16_t)(plus ? 300 - 120 : 300 + 120);   /* 1760 Hz: two octaves at 6 dB/oct */
            tests++;
            BOUND_CHECK(&bounds, !(ksfb_render_equal(&p, &q, fs, 1760000, 3000, &nonzero)));
        }
    /* A boosted index through matrix ticks stays the note-on's. */
    {
        fm_patch_t p; fm_voice_t v; unsigned j;
        int32_t first;
        ksfb_base(&p, 1);
        p.op[1].index_q10 = 2048; p.op[1].ks_break_millihz = 440000; p.op[1].ks_slope_db10 = 90;
        p.ext[1].ks_curve = 1;
        p.lfo.rate_millihz = 7000; p.mod[0] = (fm_mod_slot_patch_t){ 400, 0, 2, 0, 0, { 0, 0, 0 } };
        fm_voice_init(&v, &p, fs); fm_voice_note_on(&v, 1760000, 32767);
        first = v.op[1].index;
        tests++;
        BOUND_CHECK(&bounds, !(!v.mod_active || !v.ks_boost[1]));
        for (j = 0; j < 3000; j++) {
            int16_t x;
            fm_render_i16(&v, &x, 1);
            tests++;
            BOUND_CHECK(&bounds, !(v.op[1].index != first));
        }
    }
    /* The pre-envelope identity: a one-sample attack lands on 2^30 and a
     * 0 dB sustain keeps it, so pre- and post-envelope feedback give the
     * same samples on the fixed and the routed path; a -6 dB sustain must
     * differ. */
    for (side = 0; side < 2; side++)
        for (plus = 0; plus < 2; plus++) {
            fm_patch_t p, q;
            unsigned d;
            ksfb_base(&p, (int)side);
            p.op[1].feedback_q10 = 1536; p.op[1].attack_ms = 0; p.op[1].sustain_db10 = plus ? 60 : 0;
            if (side) { p.op[0].feedback_q10 = 512; p.op[0].attack_ms = 0; p.op[0].sustain_db10 = plus ? 60 : 0; }
            q = p;
            p.ext[1].fb_type = 1; if (side) p.ext[0].fb_type = 1;
            d = ksfb_render_equal(&p, &q, fs, 220000, 4000, &nonzero);
            tests++;
            BOUND_CHECK(&bounds, !(plus ? d == 0 : d != 0));
            if (!plus) identity += 4000u - d;
        }
    /* The squared source at its extremes: -2^30 and +2^30 square alike, and
     * INT32_MIN and INT32_MAX both saturate at 2^31 - 1, for the post- and
     * pre-envelope histories on both paths. UBSan sees every product. */
    for (side = 0; side < 2; side++)
        for (target = 2; target < 4; target++) {
            static const int32_t lo[2] = { -(1 << 30), INT32_MIN }, hi[2] = { 1 << 30, INT32_MAX };
            for (xi = 0; xi < 2; xi++) {
                fm_patch_t p; fm_voice_t va, vb; int16_t x[8], y[8]; unsigned j;
                ksfb_base(&p, (int)side);
                p.op[1].feedback_q10 = 65535; p.ext[1].fb_type = (uint8_t)target;
                fm_voice_init(&va, &p, fs); fm_voice_init(&vb, &p, fs);
                fm_voice_note_on(&va, 220000, 32767); fm_voice_note_on(&vb, 220000, 32767);
                va.op[1].prev = va.op[1].prev2 = va.op[1].raw_prev = va.op[1].raw_prev2 = lo[xi];
                vb.op[1].prev = vb.op[1].prev2 = vb.op[1].raw_prev = vb.op[1].raw_prev2 = hi[xi];
                fm_render_i16(&va, x, 1); fm_render_i16(&vb, y, 1);
                tests++;
                BOUND_CHECK(&bounds, !(x[0] != y[0] || va.op[1].prev != vb.op[1].prev));
                /* later samples average the new value with the extreme (they
                 * differ by design); UBSan still sees those products */
                fm_render_i16(&va, x + 1, 7); fm_render_i16(&vb, y + 1, 7);
                for (j = 1; j < 8; j++) { tests++; BOUND_CHECK(&bounds, !(va.op[1].prev2 == vb.op[1].prev2 && x[j] != y[j])); }
            }
        }
    /* The typed source against its exact model: histories a and b with
     * type 1, 2 or 3 give the modulator the sample a classic voice gives
     * with both post-envelope histories set to the model's source ('s
     * average of a and b, squared after the average for bit 1), so the
     * square taken before the average, or a pre type that reads the post-
     * envelope history, fails here. Seeded pairs and the extremes. */
    {
        uint32_t seed = 0x2545F491u;
        unsigned m;
        for (m = 0; m < 96; m++)
            for (side = 0; side < 2; side++)
                for (target = 1; target < 4; target++) {
                    fm_patch_t p, q; fm_voice_t va, vb; int16_t x, y;
                    int32_t a, b, src;
                    int64_t sq;
                    if (m < 4) { a = m & 1u ? 1 << 30 : -(1 << 30); b = m & 2u ? 1 << 30 : -(1 << 30); }
                    else {
                        seed = seed * 1664525u + 1013904223u; a = (int32_t)(seed % 2147483649u) - (1 << 30);
                        seed = seed * 1664525u + 1013904223u; b = (int32_t)(seed % 2147483649u) - (1 << 30);
                    }
#if FM_FB_AVG
                    src = (a >> 1) + (b >> 1) + (a & b & 1);
#else
                    src = a;
#endif
                    sq = ((int64_t)src * src) >> 30;
                    if (target & 2u) src = (int32_t)sq;
                    ksfb_base(&p, (int)side);
                    p.op[1].feedback_q10 = 2048;
                    q = p;
                    p.ext[1].fb_type = (uint8_t)target;
                    fm_voice_init(&va, &p, fs); fm_voice_init(&vb, &q, fs);
                    fm_voice_note_on(&va, 220000, 32767); fm_voice_note_on(&vb, 220000, 32767);
                    if (target & 1u) {
                        va.op[1].raw_prev = a; va.op[1].raw_prev2 = b;
                        va.op[1].prev = va.op[1].prev2 = (int32_t)(seed >> 2);   /* must not be read */
                    } else { va.op[1].prev = a; va.op[1].prev2 = b; }
                    vb.op[1].prev = vb.op[1].prev2 = src;
                    fm_render_i16(&va, &x, 1); fm_render_i16(&vb, &y, 1);
                    tests++; models++;
                    BOUND_CHECK(&bounds, !(va.op[1].prev != vb.op[1].prev || x != y));
                }
    }
    /* Canonical values, read back from the voice and the reference. */
    {
        fm_patch_t p; fm_voice_t v; ref_voice_t r;
        ksfb_base(&p, 0);
        p.ext[1] = (fm_op_ext_t){ 7, 3, 0xFD, 0xFE };
        p.ext[0] = (fm_op_ext_t){ 0, 2, 2, 1 };
        fm_voice_init(&v, &p, fs); ref_voice_init(&r, &p, fs);
        tests++;
        BOUND_CHECK(&bounds, !(v.patch.ext[1].loop != 1 || r.patch.ext[1].loop != 1 || v.patch.ext[1].ks_target != 0 || v.patch.ext[1].ks_curve != 1 || v.patch.ext[1].fb_type != 2
            || v.patch.ext[0].ks_target != 2 || v.patch.ext[0].ks_curve != 2 || v.patch.ext[0].fb_type != 1
            || r.patch.ext[1].ks_target != 0 || r.patch.ext[1].ks_curve != 1 || r.patch.ext[1].fb_type != 2 || !v.fb_special));
    }
    /* fb_special: only operators the voice steps set it, and an unstepped
     * operator's type leaves the samples alone. */
    for (side = 0; side < 2; side++) {
        fm_patch_t p, q; fm_voice_t v;
        ksfb_base(&p, (int)side);
        p.op[1].feedback_q10 = 1024;
        q = p;
        p.ext[3].fb_type = 3; p.ext[2].fb_type = 1;
        fm_voice_init(&v, &p, fs);
        tests += 2;
        BOUND_CHECK(&bounds, !(v.fb_special));
        BOUND_CHECK(&bounds, !(ksfb_render_equal(&p, &q, fs, 220000, 2000, NULL)));
    }
    tests++;
    BOUND_CHECK(&bounds, !(!boosted || !saturated || !levels || !identity || !nonzero || !models));
    printf("KSFBBOUND fs=%u phase64=%d checks=%u failures=%u boosted=%u saturated=%u levels=%u identity=%u models=%u\n",
           fs, FM_PHASE64, tests, bounds.bad, boosted, saturated, levels, identity, models);
    if (bounds.bad) printf("GATE ksfb FAIL %u\n", bounds.bad);
    return bounds.bad;
}

/* : direct API checks that cannot be invalid-input timelines. */
static unsigned peg_twin(const spike_case_t *c, const fm_patch_t *raw, const fm_patch_t *canonical, uint32_t fs)
{
    fm_voice_t a, b;
    uint32_t pos, n = (uint32_t)((uint64_t)c->len_ms * fs / 1000u);
    int e = 0, ne = ev_count(c);
    fm_voice_init(&a, raw, fs); fm_voice_init(&b, canonical, fs);
    for (pos = 0; pos < n; pos++) {
        int16_t x, y;
        while (e < ne && ev_sample(&c->ev[e], fs, n) == pos) {
            a1_event(&a, &c->ev[e]); a1_event(&b, &c->ev[e]); e++;
        }
        fm_render_i16(&a, &x, 1); fm_render_i16(&b, &y, 1);
        if (x != y || a.peg.cur != b.peg.cur || a.peg.stage != b.peg.stage) {
            printf("PEG-BOUNDS-FAIL twin %s sample=%u\n", c->name, pos);
            return 1;
        }
    }
    return 0;
}

/* Independent real increment; fs 0 has a defined saturated peg base,
 * compared directly without division or passing zero into the oracle. */
static unsigned peg_bound_inc(const fm_voice_t *v, uint32_t freq, double *max_bound)
{
#if FM_PHASE64
    const uint64_t ai = v->op[0].inc;
#else
    const uint64_t ai = (uint64_t)v->op[0].inc << 32;
#endif
    const double x = (v->peg.cur + v->lfo_cents) / 1200.0;
    double ri, bound = ldexp(1.0, FM_PHASE64 ? 17 : 33), error;
    if (!v->sample_rate_hz) {
#if FM_PHASE64
        const uint64_t expected = ((UINT64_C(1) << 47) - 1u) << 16;
#else
        const uint64_t expected = UINT64_C(1) << 63;
#endif
        return ai != expected;
    }
    ri = (double)freq * v->patch.op[0].ratio_q16 / (1000.0 * v->sample_rate_hz * 65536.0) * exp2(x);
    if (ri > 0.5) ri = 0.5;
    if (x != 0.0) bound = ldexp(2.0 + exp2(x), FM_PHASE64 ? 16 : 32) + ldexp(ri, 20);
    error = fabs((double)ai - ldexp(ri, 64));
    if (error / bound > *max_bound) *max_bound = error / bound;
    return error > bound;
}

static unsigned peg_bounds(uint32_t fs)
{
    const uint32_t rates[] = { 0, 1, fs, UINT32_MAX };
    const uint32_t ratios[] = { 0, 1, 32768, 65535, 65536, 65537, 131072, 1048575, 1048576, 1048577, UINT32_MAX };
    const int16_t levels[] = { -4800, -1200, 0, 1200, 4800 };
    const spike_case_t *drop = NULL;
    unsigned failed = 0, tests = 0;
    size_t ci, ri, fi, qi, li;
    int lm, ev, j;
    double max_bound = 0.0;
    fm_patch_t raw, canonical;
    for (ci = 0; ci < NCASE; ci++) if (!strcmp(CASES[ci].name, "pegdrop")) drop = &CASES[ci];
    if (!drop) return 1;
    raw = canonical = drop->patch;
    for (j = 0; j < 4; j++) { raw.peg.time_ms[j] = UINT16_MAX; canonical.peg.time_ms[j] = 60000; }
    raw.peg.level_cents[0] = raw.peg.level_cents[3] = INT16_MAX;
    raw.peg.level_cents[1] = INT16_MIN;
    canonical.peg.level_cents[0] = canonical.peg.level_cents[3] = 4800;
    canonical.peg.level_cents[1] = -4800;
    raw.peg.mask = UINT16_MAX; canonical.peg.mask = 0;
    failed += peg_twin(drop, &raw, &canonical, fs); tests++;
    /* All-zero levels stay off even with nonzero times and mask. */
    raw = canonical = CASES[0].patch;
    for (j = 0; j < 4; j++) raw.peg.time_ms[j] = 60000;
    raw.peg.mask = UINT16_MAX;
    failed += peg_twin(&CASES[0], &raw, &canonical, fs); tests++;
    for (ri = 0; ri < sizeof rates / sizeof rates[0]; ri++) {
        const uint64_t turn = (uint64_t)rates[ri] * 1000u;
        const uint32_t freqs[] = { 0, 1, 440000, 12000000, turn <= UINT32_MAX ? (uint32_t)turn : UINT32_MAX, UINT32_MAX };
        for (fi = 0; fi < sizeof freqs / sizeof freqs[0]; fi++)
            for (qi = 0; qi < sizeof ratios / sizeof ratios[0]; qi++)
                for (li = 0; li < sizeof levels / sizeof levels[0]; li++)
                    for (lm = 0; lm < 3; lm++) {
                        fm_patch_t p;
                        fm_voice_t v;
                        memset(&p, 0, sizeof p);
                        p.op[0].ratio_q16 = ratios[qi]; p.peg.mask = 1;
                        for (j = 0; j < 4; j++) p.peg.level_cents[j] = levels[li];
                        if (!levels[li]) p.peg.level_cents[3] = 1; /* active peg, cur 0 after zero chain */
                        if (lm) {
                            p.lfo.rate_millihz = 5000; p.lfo.wave = 4;
                            p.lfo.pitch_cents = 1200; p.lfo.mask_pitch = 1;
                            p.lfo.phase_u16 = lm == 1 ? 32768 : 0; /* exact -1200 or +1199 cents */
                        }
                        fm_voice_init(&v, &p, rates[ri]);
                        fm_voice_note_on(&v, freqs[fi], 32767);
                        for (ev = 0; ev < 2; ev++) {
                            const uint32_t f = ev ? (freqs[fi] == UINT32_MAX ? 0u : UINT32_MAX) : freqs[fi];
                            unsigned bad;
                            if (ev) fm_voice_pitch(&v, f);
                            bad = peg_bound_inc(&v, f, &max_bound);
                            if (bad && failed < 8u) printf("PEG-BOUNDS-FAIL fs=%u freq=%u ratio=%u cents=%d event=%d\n",
                                                         rates[ri], f, ratios[qi], v.peg.cur + v.lfo_cents, ev);
                            failed += bad; tests++;
                        }
                    }
    }
    {   /* T4=0 applies L4 now without stepping/restarting the LFO. */
        fm_patch_t p = drop->patch;
        fm_voice_t v;
        int32_t samples[128];
        uint32_t phase, time, ctl;
        p.peg.time_ms[3] = 0;
        p.lfo.rate_millihz = 5000; p.lfo.wave = 4;
        p.lfo.pitch_cents = 1200; p.lfo.mask_pitch = 1;
        fm_voice_init(&v, &p, fs); fm_voice_note_on(&v, 55000, 32767);
        fm_render_q30(&v, samples, 128);
        phase = v.lfo_phase; time = v.lfo_time; ctl = v.ctl;
        fm_voice_note_off(&v);
        if (v.peg.cur != 2400 || v.peg.stage != 6 || phase != v.lfo_phase || time != v.lfo_time || ctl != v.ctl
            || peg_bound_inc(&v, 55000, &max_bound)) {
            printf("PEG-BOUNDS-FAIL zero release\n"); failed++;
        }
        tests++;
    }
    printf("PEG_BOUNDS fs=%u phase64=%d tests=%u failures=%u bound_ratio_max=%.3e\n", fs, FM_PHASE64, tests, failed, max_bound);
    return failed;
}

/* : raw API twins, tracking oracle and cutoff endpoints. These are
 * host-only probes of public voice state, with no exported engine hook. */
static unsigned fenv_twin(const spike_case_t *c, const fm_patch_t *raw, const fm_patch_t *canonical, uint32_t fs)
{
    fm_voice_t a, b;
    uint32_t pos, n = (uint32_t)((uint64_t)c->len_ms * fs / 1000u);
    int e = 0, ne = ev_count(c);
    fm_voice_init(&a, raw, fs); fm_voice_init(&b, canonical, fs);
    for (pos = 0; pos < n; pos++) {
        int16_t x, y;
        while (e < ne && ev_sample(&c->ev[e], fs, n) == pos) {
            a1_event(&a, &c->ev[e]); a1_event(&b, &c->ev[e]); e++;
        }
        fm_render_i16(&a, &x, 1); fm_render_i16(&b, &y, 1);
        if (x != y || a.fenv.cur != b.fenv.cur || a.fenv.stage != b.fenv.stage
            || a.fenv_track != b.fenv_track || a.fenv_u != b.fenv_u) {
            printf("FENV-BOUNDS-FAIL twin %s sample=%u\n", c->name, pos);
            return 1;
        }
    }
    return 0;
}

static unsigned fenv_bounds(uint32_t fs)
{
    const uint32_t rates[] = { 0, 1, fs, 9544371, UINT32_MAX };
    const uint32_t freqs[] = { 0, 1, 27500, 130813, 261626, 523251, 1046502, UINT32_MAX };
    const uint32_t refs[] = { 0, 1, 261626, 4186009, UINT32_MAX };
    const uint16_t tracks[] = { 0, 1, 999, 1000, 2000, UINT16_MAX };
    const int16_t depths[] = { -9600, 1, 9600 };
    const uint32_t cutoffs[] = { 0, 300000, UINT32_MAX };
    const double tolerance = 0.5 + ldexp(1.0, 2 * (10 - FM_EXP_BITS) - 11);
    const spike_case_t *pluck = NULL;
    unsigned failed = 0, tests = 0;
    size_t ci, ri, fi, bi, ti, di, ui;
    double track_max = 0.0, u_max = 0.0;
    fm_patch_t raw, canonical;
    for (ci = 0; ci < NCASE; ci++) if (!strcmp(CASES[ci].name, "fenvpluck")) pluck = &CASES[ci];
    if (!pluck) return 1;
    raw = canonical = pluck->patch;
    raw.fenv.attack_ms = raw.fenv.decay_ms = raw.fenv.release_ms = UINT16_MAX;
    canonical.fenv.attack_ms = canonical.fenv.decay_ms = canonical.fenv.release_ms = 60000;
    raw.fenv.sustain_permille = raw.fenv.track_permille = UINT16_MAX;
    canonical.fenv.sustain_permille = 1000; canonical.fenv.track_permille = 2000;
    raw.fenv.depth_cents = INT16_MIN; canonical.fenv.depth_cents = -9600;
    raw.fenv.ref_millihz = canonical.fenv.ref_millihz = 1;
    failed += fenv_twin(pluck, &raw, &canonical, fs); tests++;
    raw.fenv.depth_cents = INT16_MAX; canonical.fenv.depth_cents = 9600;
    failed += fenv_twin(pluck, &raw, &canonical, fs); tests++;
    raw = canonical = pluck->patch;
    raw.fenv.track_permille = canonical.fenv.track_permille = 1000;
    canonical.fenv.ref_millihz = 261626; /* raw 0 is the C4 alias */
    failed += fenv_twin(pluck, &raw, &canonical, fs); tests++;
    raw = canonical = CASES[0].patch;
    raw.fenv = pluck->patch.fenv; /* a disabled filter disables fenv too */
    failed += fenv_twin(&CASES[0], &raw, &canonical, fs); tests++;
    for (ri = 0; ri < sizeof rates / sizeof rates[0]; ri++) {
        const uint64_t r = rates[ri] ? rates[ri] : 1u;
        const uint64_t lo = (r * 5u + 11u) / 12u, hi = r * 450u;
        for (fi = 0; fi < sizeof freqs / sizeof freqs[0]; fi++)
            for (bi = 0; bi < sizeof refs / sizeof refs[0]; bi++)
                for (ti = 0; ti < sizeof tracks / sizeof tracks[0]; ti++)
                    for (di = 0; di < sizeof depths / sizeof depths[0]; di++)
                        for (ui = 0; ui < sizeof cutoffs / sizeof cutoffs[0]; ui++) {
                            fm_patch_t p = pluck->patch;
                            fm_voice_t v;
                            double oct, expected, err, factor, base, u, bound;
                            int32_t cents;
                            unsigned bad = 0;
                            p.filter.cutoff_millihz = cutoffs[ui];
                            p.fenv.ref_millihz = refs[bi]; p.fenv.track_permille = tracks[ti];
                            p.fenv.depth_cents = depths[di]; p.fenv.sustain_permille = 1000;
                            p.fenv.attack_ms = p.fenv.decay_ms = p.fenv.release_ms = 0;
                            fm_voice_init(&v, &p, rates[ri]); fm_voice_note_on(&v, freqs[fi], 32767);
                            oct = freqs[fi] ? log2((double)freqs[fi] / (refs[bi] ? refs[bi] : 261626u)) : -20.0;
                            if (oct < -20.0) oct = -20.0;
                            if (oct > 20.0) oct = 20.0;
                            expected = oct * 1200.0 * (tracks[ti] > 2000 ? 2000 : tracks[ti]) / 1000.0;
                            err = fabs(v.fenv_track - expected);
                            if (err > track_max) track_max = err;
                            if (err > tolerance) bad = 1;
                            cents = v.fenv_track + depths[di]; /* full sustain, exact depth */
                            if (cents < -12800) cents = -12800;
                            if (cents > 12800) cents = 12800;
                            factor = exp2(cents / 1200.0);
                            base = (double)(cutoffs[ui] < lo ? lo : cutoffs[ui] > hi ? hi : cutoffs[ui]) / (1000.0 * r);
                            u = base * factor;
                            if (u < (double)lo / (1000.0 * r)) u = (double)lo / (1000.0 * r);
                            if (u > 0.45) u = 0.45;
                            bound = 4.0 + factor + ldexp(u, 4); /* base/product ulps and exp2 relative 2^-44 */
                            err = fabs((double)v.fenv_u - ldexp(u, 48));
                            if (err / bound > u_max) u_max = err / bound;
                            if (err > bound || v.fenv.cur != 32768 || v.fenv.stage != 3u) bad = 1;
                            if (bad && failed < 8u) printf("FENV-BOUNDS-FAIL fs=%u freq=%u ref=%u track=%u depth=%d cutoff=%u\n",
                                                           rates[ri], freqs[fi], refs[bi], tracks[ti], depths[di], cutoffs[ui]);
                            failed += bad; tests++;
                        }
        {   /* The upper normalized endpoint cannot be represented as a
             * uint32 cutoff at high fs; force it with a positive offset. */
            fm_patch_t p = pluck->patch;
            fm_voice_t v;
            p.filter.cutoff_millihz = UINT32_MAX;
            p.fenv.ref_millihz = 1; p.fenv.track_permille = 2000; p.fenv.depth_cents = 9600;
            p.fenv.sustain_permille = 1000; p.fenv.attack_ms = p.fenv.decay_ms = 0;
            fm_voice_init(&v, &p, rates[ri]); fm_voice_note_on(&v, UINT32_MAX, 32767);
            if (v.fenv_uhi != (int64_t)((UINT64_C(1) << 48) * 450u / 1000u) || v.fenv_u != v.fenv_uhi) {
                printf("FENV-BOUNDS-FAIL upper fs=%u\n", rates[ri]); failed++;
            }
            tests++;
        }
    }
    {   /* R=0 restores the last filter event's base/Q immediately, and
         * does not advance the grid or LFO. A pitch event leaves track. */
        fm_patch_t p = pluck->patch, q;
        fm_voice_t a, b;
        int32_t samples[128], track;
        uint32_t phase, time, ctl;
        p.fenv.attack_ms = p.fenv.decay_ms = p.fenv.release_ms = 0;
        p.fenv.sustain_permille = 1000;
        p.fenv.track_permille = 1000; p.fenv.ref_millihz = 130813;
        p.lfo.rate_millihz = 7000;
        fm_voice_init(&a, &p, fs); fm_voice_note_on(&a, 130813, 32767);
        fm_render_q30(&a, samples, 128); fm_voice_filter(&a, 1000000, 707);
        phase = a.lfo_phase; time = a.lfo_time; ctl = a.ctl; track = a.fenv_track;
        fm_voice_pitch(&a, 523251); fm_voice_note_off(&a);
        q = p; memset(&q.fenv, 0, sizeof q.fenv);
        fm_voice_init(&b, &q, fs); fm_voice_filter(&b, 1000000, 707);
        if (a.fenv.cur || a.fenv.stage != 5u || phase != a.lfo_phase || time != a.lfo_time || ctl != a.ctl
            || track != a.fenv_track || a.fenv_q != 707u || a.fenv_u != a.fenv_ub
            || a.svf.a1 != b.svf.a1 || a.svf.a2 != b.svf.a2 || a.svf.a3 != b.svf.a3 || a.svf.k != b.svf.k) {
            printf("FENV-BOUNDS-FAIL zero release\n"); failed++;
        }
        tests++;
    }
    printf("FENV_BOUNDS fs=%u phase64=%d tests=%u failures=%u track_error_max=%.6f track_bound=%.6f cutoff_bound_ratio_max=%.3e\n",
           fs, FM_PHASE64, tests, failed, track_max, tolerance, u_max);
    return failed;
}

/* ---- --retrig: retrigger counts, engine against reference  ----
 * A retrigger outside an operator's attack times the new attack from gain g0
 * as n - floor(g0 * n) samples, at least 1 (fm_attack_samples,
 * ref_attack_samples), each side from its own g0. Where g0 * n lies closer
 * to an integer than the two products differ, the floors can differ by one
 * and the  rule decides (adopt_count). A retrigger in the attack leaves
 * it running on both sides. */

static const char STAGE_CH[] = "IADSRW2";

static char stage_ch(uint32_t st)
{
    return st < sizeof STAGE_CH - 1u ? STAGE_CH[st] : '?';
}

/* d: distance of the engine's g0 * n (g0 in Q2.62) from the nearest integer,
 * exact; dx: the engine's product minus the reference's. */
static void probe_terms(uint64_t g0, double g0_ref, uint32_t n, double *d, double *dx)
{
    const uint64_t one = (uint64_t)1 << 62;
    uint64_t lo  = (g0 & 0xFFFFFFFFu) * n;
    uint64_t t   = (g0 >> 32) * n + (lo >> 32);
    uint64_t k   = t >> 30;                                          /* the engine's floor */
    uint64_t rem = ((t & 0x3FFFFFFFu) << 32) | (lo & 0xFFFFFFFFu);   /* g0 * n - k, in 2^-62 */
    double xr = g0_ref * (double)n, kr = floor(xr);
    *d  = (double)(rem < one / 2u ? rem : one - rem) / (double)one;
    *dx = ((double)k - kr) + ((double)rem / (double)one - (xr - kr));
}

/* Per operator after the same note-on on both voices; se and sr are the
 * stages before it. margin = dist / |dx|; at or below 1 the floors can
 * differ. Applies the  rule to r and prints its verdict: kept (an attack
 * left running), match, adopted or FAIL. For a kept attack, dist, dx and
 * margin describe the count  alone would have recomputed. */
static void probe_line(const char *name, uint32_t t_ms, const fm_voice_t *v, ref_voice_t *r,
                       const uint32_t se[FM_OPS], const int sr[FM_OPS])
{
    const int nops = v->ops ? (int)v->ops : 2;
    int i;
    for (i = 0; i < nops; i++) {
        uint32_t n = r->op[i].count_a, cr = r->op[i].count;   /* the full attack, rate-scaled  */
        double d, dx;
        int k;
        probe_terms(v->op[i].gain, r->op[i].gain, n, &d, &dx);
        k = adopt_count(v, r, i, sr[i]);
        printf("RETRIG %-8s t=%ums op%d stage=%c/%c n=%u count=%u/%u dist=%.3e dx=%+.3e margin=",
               name, t_ms, i, stage_ch(se[i]), stage_ch((uint32_t)sr[i]), n, v->op[i].count, cr, d, dx);
        if (dx != 0.0) printf("%.1f", d / fabs(dx)); else printf("-");
        printf(" %s\n", k == COUNT_ADOPTED ? "adopted" : k == COUNT_FAIL ? "FAIL"
                        : sr[i] == FM_STAGE_ATTACK ? "kept" : "match");
    }
}

static void probe_render(fm_voice_t *v, ref_voice_t *r, uint32_t frames)
{
    int16_t p[1024];
    double d[1024];
    while (frames) {
        uint32_t m = frames < 1024u ? frames : 1024u;
        fm_render_i16(v, p, m);
        ref_render(r, d, m);
        frames -= m;
    }
}

/* Every note-on of a case that meets a sounding voice. */
static void probe_events(const spike_case_t *c, uint32_t fs)
{
    fm_voice_t v;
    ref_voice_t r;
    uint32_t n = (uint32_t)((uint64_t)c->len_ms * fs / 1000u), pos = 0, se[FM_OPS];
    int e, ne = ev_count(c), sr[FM_OPS], i;
    case_voice_init(&v, c, fs);
    case_ref_init(&r, c, fs);
    for (e = 0; e < ne; e++) {
        const spike_event_t *ev = &c->ev[e];
        uint32_t t = ev_sample(ev, fs, n);
        int retrig;
        probe_render(&v, &r, t - pos);
        pos = t;
        retrig = ev->type == EV_ON && fm_voice_active(&v);
        for (i = 0; i < FM_OPS; i++) { se[i] = v.op[i].stage; sr[i] = r.op[i].stage; }
        a1_event(&v, ev);
        ref_event(&r, ev);
        if (retrig) probe_line(c->name, ev->t_ms, &v, &r, se, sr);
    }
}

/* One note of the case's patch, on at 0 and off at off_ms; after every
 * sample while it sounds, a copy of both voices is retriggered. Tallies per
 * operator and the stage it was in the counts that differ and those the
 *  rule adopts; the rest fail. */
static void probe_sweep(const spike_case_t *c, uint32_t fs, uint32_t off_ms)
{
    fm_voice_t v, v2;
    ref_voice_t r, r2;
    int16_t s16;
    double sd;
    uint32_t n = (uint32_t)((uint64_t)c->len_ms * fs / 1000u);
    uint32_t off = (uint32_t)((uint64_t)off_ms * fs / 1000u), s;
    unsigned long tot[2][FM_STAGE_DECAY2 + 1] = { { 0 } };
    unsigned long mis[2][FM_STAGE_DECAY2 + 1] = { { 0 } };
    unsigned long adp[2][FM_STAGE_DECAY2 + 1] = { { 0 } };
    double maxdx[2][FM_STAGE_DECAY2 + 1] = { { 0 } };
    int i, st, k;
    case_voice_init(&v, c, fs);
    case_ref_init(&r, c, fs);
    fm_voice_note_on(&v, 440000, 32767);
    ref_voice_note_on(&r, 440000, 32767);
    for (s = 0; s < n; s++) {
        if (s == off) { fm_voice_note_off(&v); ref_voice_note_off(&r); }
        fm_render_i16(&v, &s16, 1);
        ref_render(&r, &sd, 1);
        if (!fm_voice_active(&v)) break;
        v2 = v;
        r2 = r;
        fm_voice_note_on(&v2, 493883, 24000);
        ref_voice_note_on(&r2, 493883, 24000);
        for (i = 0; i < 2; i++) {
            uint32_t na = r2.op[i].count_a;   /* the full attack, rate-scaled  */
            double d, dx;
            st = (int)v.op[i].stage;
            if (st != r.op[i].stage) { printf("SWEEP %s op%d sample=%u STAGE-DIFF\n", c->name, i, s); continue; }
            probe_terms(v2.op[i].gain, r2.op[i].gain, na, &d, &dx);
            k = adopt_count(&v2, &r2, i, st);
            tot[i][st]++;
            if (k != COUNT_EQUAL) mis[i][st]++;
            if (k == COUNT_ADOPTED) adp[i][st]++;
            if (fabs(dx) > maxdx[i][st]) maxdx[i][st] = fabs(dx);
        }
    }
    for (i = 0; i < 2; i++)
        for (st = FM_STAGE_ATTACK; st <= FM_STAGE_DECAY2; st++)
            if (tot[i][st])
                printf("SWEEP %s op%d stage=%c retriggers=%lu mismatches=%lu adopted=%lu max_abs_dx=%.3e\n",
                       c->name, i, stage_ch((uint32_t)st), tot[i][st], mis[i][st], adp[i][st], maxdx[i][st]);
}

/* Retrigger times for whole renders of the retrig patch (note on at 0 ms,
 * off at 700 ms): both operators in the attack (10, 20, 25), carrier in the
 * attack and modulator in the decay (40), both in the decay (250), both in
 * the sustain (650), both in the release (900, a 400 ms note). */
static const uint32_t PROBE_T_MS[] = { 10, 20, 25, 40, 250, 650, 900 };

/* --retrig, one sample rate per run:
 *   RETRIG  every note-on that meets a sounding voice, in the cases and
 *           in the renders below: per operator the stages (engine/reference)
 *           before it, the full attack n, both counts, dist, dx, margin and
 *           the  verdict.
 *   RESULT  the retrig patch with one retrigger at each of PROBE_T_MS, named
 *           rt<ms>, measured as the cases are, after its ADOPT lines.
 *   SWEEP   probe_sweep on the retrig patch, note-off at 700 ms. */
static int probe_retrig(uint32_t fs)
{
    const spike_case_t *rt = NULL;
    size_t ci, k;
    for (ci = 0; ci < NCASE; ci++) {
        if (!strcmp(CASES[ci].name, "retrig")) rt = &CASES[ci];
        probe_events(&CASES[ci], fs);
    }
    if (!rt) return 1;
    for (k = 0; k < sizeof PROBE_T_MS / sizeof PROBE_T_MS[0]; k++) {
        spike_case_t c = *rt;
        char name[16];
        uint32_t t = PROBE_T_MS[k];
        snprintf(name, sizeof name, "rt%u", (unsigned)t);
        c.name = name;
        memset(c.ev, 0, sizeof c.ev);
        c.ev[0] = (spike_event_t){ 0, EV_ON, 440000, 32767, 0 };
        if (t < 700u) {
            c.ev[1] = (spike_event_t){ t, EV_ON, 493883, 24000, 0 };
            c.ev[2] = (spike_event_t){ 700, EV_OFF, 0, 0, 0 };
        } else {
            c.ev[1] = (spike_event_t){ 700, EV_OFF, 0, 0, 0 };
            c.ev[2] = (spike_event_t){ t, EV_ON, 493883, 24000, 0 };
            c.ev[3] = (spike_event_t){ t + 400u, EV_OFF, 0, 0, 0 };
        }
        probe_events(&c, fs);
        { const int rc = run_case(&c, fs, NULL, NULL); if (rc) return rc; }
    }
    probe_sweep(rt, fs, 700);
    return 0;
}

/* ---- --fb: feedback at high depth  ----
 * The feedback case's patch with both sustains at 0 dB, so that each depth
 * of FB_DEPTH_Q10 (radians Q6.10) holds after the 3 ms attack. Per depth:
 *   FBOP    the fed-back operator's own output (the modulator, read from
 *           both voices after every sample: what it sounds like as a
 *           carrier) in int16 units, over exactly one second from 250 ms
 *           (220 periods of 220 Hz): the engine's SNR against the reference,
 *           its peak error, and for each side the power off DC and off the
 *           harmonics k * 220 Hz below fs / 2, as a share of the total
 *           (offharm; aliased harmonics, subharmonics and noise all count).
 *   RESULT  the patch as heard, named fb<q10>, measured as the cases are.
 * With --out, also a1_fbop<q10>.wav and ref_fbop<q10>.wav, the fed-back
 * operator's output rounded to int16. Builds with FM_FB_AVG 1 and 0 compare
 * the two feedback forms. Depths: 0 (control), 1 (the feedback case), 1.125,
 * 1.5, pi/2, 2, 2.125, pi and 4 pi radians. */
static const uint16_t FB_DEPTH_Q10[] = { 0, 1024, 1152, 1536, 1608, 2048, 2176, 3217, 12868 };

static const double TWO_PI = 6.28318530717958647692;

/* Power off DC and off the harmonics of f0 below fs / 2, as a share of the
 * total, in dB, over x[0..n), n = fs samples, so that harmonic k lies on DFT
 * bin k * f0. cs holds cos and sin of 2 pi j / n. */
static double offharm_db(const double *x, uint32_t n, uint32_t f0, const double *cs)
{
    double tot = 0, dc = 0, harm = 0, off;
    uint32_t i, j, m;
    for (i = 0; i < n; i++) { tot += x[i] * x[i]; dc += x[i]; }
    tot /= n;
    dc /= n;
    for (m = f0; 2 * m < n; m += f0) {
        double re = 0, im = 0;
        for (i = 0, j = 0; i < n; i++) {
            re += x[i] * cs[2 * j];
            im += x[i] * cs[2 * j + 1];
            j += m;
            if (j >= n) j -= n;
        }
        harm += 2.0 * (re * re + im * im) / ((double)n * (double)n);
    }
    off = tot - dc * dc - harm;
    if (off < tot * 1e-30) off = tot * 1e-30;
    return db(off, tot);
}

static int probe_fb(uint32_t fs, const char *outdir)
{
    const spike_case_t *fc = NULL;
    const uint32_t f0 = 220, w0 = fs / 4, wn = fs;
    uint32_t n = 0, j;
    double *cs, *ma, *mr;
    int16_t *q;
    size_t ci, k;
    for (ci = 0; ci < NCASE; ci++)
        if (!strcmp(CASES[ci].name, "feedback")) fc = &CASES[ci];
    if (!fc) return 1;
    n = (uint32_t)((uint64_t)fc->len_ms * fs / 1000u);
    if (n < w0 + wn) return 1;
    cs = (double *)alloc_n(2u * (size_t)wn, sizeof *cs);
    ma = (double *)alloc_n(n, sizeof *ma);
    mr = (double *)alloc_n(n, sizeof *mr);
    q  = (int16_t *)alloc_n(n, sizeof *q);
    if (!cs || !ma || !mr || !q) { fprintf(stderr, "oom\n"); return HOST_ERROR; }
    for (j = 0; j < wn; j++) {
        cs[2 * j]     = cos(TWO_PI * (double)j / (double)wn);
        cs[2 * j + 1] = sin(TWO_PI * (double)j / (double)wn);
    }
    for (k = 0; k < sizeof FB_DEPTH_Q10 / sizeof FB_DEPTH_Q10[0]; k++) {
        spike_case_t c = *fc;
        fm_voice_t v;
        ref_voice_t r;
        char name[16];
        int16_t s16;
        double sd, sig = 0, err = 0, peak = 0;
        uint32_t s, peak_i = 0;
        int e = 0, ne = ev_count(&c);
        c.patch.op[0].sustain_db10 = 0;
        c.patch.op[1].sustain_db10 = 0;
        c.patch.op[1].feedback_q10 = FB_DEPTH_Q10[k];
        snprintf(name, sizeof name, "fb%u", (unsigned)FB_DEPTH_Q10[k]);
        c.name = name;
        fm_voice_init(&v, &c.patch, fs);
        ref_voice_init(&r, &c.patch, fs);
        for (s = 0; s < n; s++) {
            while (e < ne && ev_sample(&c.ev[e], fs, n) == s) { a1_event(&v, &c.ev[e]); ref_event(&r, &c.ev[e]); e++; }
            fm_render_i16(&v, &s16, 1);
            ref_render(&r, &sd, 1);
            ma[s] = (double)v.op[1].prev / 32768.0;          /* Q2.30 to int16 units */
            mr[s] = r.op[1].prev * 32768.0;
        }
        for (s = w0; s < w0 + wn; s++) {
            double d = ma[s] - mr[s];
            sig += mr[s] * mr[s];
            err += d * d;
            if (fabs(d) > peak) { peak = fabs(d); peak_i = s; }
        }
        printf("FBOP   %-8s depth_rad=%.4f snr_db=%.2f peak_err_lsb=%.2f@%u offharm_a1_db=%.2f offharm_ref_db=%.2f\n",
               name, FB_DEPTH_Q10[k] / 1024.0, db(sig, err), peak, peak_i,
               offharm_db(ma + w0, wn, f0, cs), offharm_db(mr + w0, wn, f0, cs));
        if (outdir) {
            char path[512];
            for (s = 0; s < n; s++) q[s] = clip16(nearbyint(ma[s]));
            snprintf(path, sizeof path, "%s/a1_fbop%u.wav", outdir, (unsigned)FB_DEPTH_Q10[k]);
            { const int rc = write_wav(path, q, n, fs); if (rc) return rc; }
            for (s = 0; s < n; s++) q[s] = clip16(nearbyint(mr[s]));
            snprintf(path, sizeof path, "%s/ref_fbop%u.wav", outdir, (unsigned)FB_DEPTH_Q10[k]);
            { const int rc = write_wav(path, q, n, fs); if (rc) return rc; }
        }
        { const int rc = run_case(&c, fs, outdir, NULL); if (rc) return rc; }
    }
    free(cs); free(ma); free(mr); free(q);
    return 0;
}

/* ---- --play, --emit: timelines  ----
 * A timeline is the record of a render: the header fixes everything the
 * output depends on and the events carry library units, so replaying the
 * canonical merged form (--merged) gives the same output and hash. */

#define TL_MAX_STREAMS 16
#define TL_MAX_PATCHES 32
#define TL_MAX_VOICES  64
#define TL_MAX_FILES   (TL_MAX_STREAMS + 1)   /* a header file and one file per stream */
#define TL_MAX_TOK     (FM_OPS * (FM_OPS - 1) + 4 > 24 ? FM_OPS * (FM_OPS - 1) + 4 : 24)   /* a route with every edge fits */
#define TL_NAME        32
#define TL_LINE        1024

typedef struct {
    uint64_t t_ns;
    uint32_t sample;         /* floor(t_ns * fs / 10^9) */
    uint32_t stream;         /* position in the streams statement */
    uint32_t order;          /* input order: files in command-line order, lines in file order */
    uint32_t voice;
    uint32_t type;           /* EV_ON, EV_OFF, EV_PITCH, EV_POS, EV_MOVE, EV_FILTER */
    uint32_t freq_millihz;   /* EV_ON and EV_PITCH; EV_FILTER: the cutoff in mHz */
    uint16_t velocity_q15;   /* EV_ON; EV_FILTER: Q in thousandths; EV_CTL: input index */
    int32_t ctl_value;       /* EV_CTL: signed Q15, both endpoints included */
    int32_t  x, y, z;        /* EV_POS: the voice's position in mm; EV_MOVE: where the move ends (space/SPEC.md) */
    uint64_t dur_ns;         /* EV_MOVE: the move's duration, 1 or more */
    uint32_t end;            /* EV_MOVE: the sample at t_ns + dur_ns, below 2^32; the move ends at the first boundary at or after it */
    /* The effects (fx/SPEC.md): EV_SEND's bus and level. */
    uint32_t bus;            /* FX_CHORUS, FX_DELAY, FX_REVERB */
    uint32_t level_q15;      /* 0 to 32768 */
    uint32_t crush_bits, crush_div; /* EV_CRUSH: 1..24, 1..64 */
    int64_t gen_value;
    uint16_t gen_field;
    uint8_t gen_id, gen_param;
} tl_event_t;

typedef struct {
    char       name[TL_NAME];
    const char *lfo_path;
    unsigned lfo_line;
    char lfo_rate_token[TL_LINE];   /* deferred Nyquist/range diagnostic, host only */
    fm_patch_t p;
    unsigned   ops;          /* bit i: operator i defined */
    unsigned   mods;        /* explicitly authored slots, including zero amounts */
    unsigned   gen_mods;
    uint8_t    gen_id[FM_MOD_SLOTS], gen_axis[FM_MOD_SLOTS];
    unsigned   eg, ks, noise, pd, vector, formant;       /* bit i: one eg/ks/noise statement for this operator  */
    unsigned   wt;                                       /* bit i: a wtuse statement for operator i  */
    char       wt_bank[FM_OPS][TL_NAME];                 /* its bank's name, resolved after the headers */
    unsigned   kssrc;                                    /* bit i: a ksource statement for operator i  */
    unsigned   kstype, fbtype, egloop;                   /* bit i: a kstype, fbtype or egloop statement for operator i  */
} tl_patch_t;

/*  wavetable banks (design-hybrid.md section 2): complete wtdata rows
 * with their wthash, or harmonic statements the header check expands with
 * fm_wt_additive. Bank i is slot i of the assets every voice binds. */
#define TL_WT_POOL (FM_WT_SLOTS * FM_WT_BANDS * FM_WT_FRAMES_MAX * FM_WT_LENGTH_MAX)
typedef struct {
    char     name[TL_NAME];
    uint32_t frames, length, offset;   /* offset: the bank's first sample in pool */
    uint32_t mode;                     /* 0 nothing yet, 1 wtdata, 2 harmonic */
    uint32_t nh;                       /* harmonic statements */
    int      have_hash;
    uint64_t hash;
} tl_wt_bank_t;

typedef struct {
    uint32_t         nbanks, used;     /* used: pool samples the banks take */
    tl_wt_bank_t     bank[FM_WT_SLOTS];
    fm_wt_harmonic_t h[FM_WT_SLOTS][FM_WT_FRAMES_MAX * 1024];
    uint8_t          hseen[FM_WT_SLOTS][FM_WT_FRAMES_MAX][1024 / 8];
    uint32_t         filled[TL_WT_POOL / 32];   /* bit: a wtdata sample is present */
    int16_t          pool[TL_WT_POOL];
    fm_wt_view_t     view[FM_WT_SLOTS];
    fm_assets_t      assets;           /* wt_count 0 until the header check */
} tl_wt_t;

typedef struct {
    int         have_timeline, have_engine, have_hash, have_space;
    int         decodable;                        /*  reader profile, not an effect bit */
    int         draft;                            /* 1, or 2 with the spatial statements (space/SPEC.md) */
    int         space;                            /* draft2: 1 stereo, 0 the mono path */
    uint32_t    block, near;                      /* draft2: the block and the near-field radius; 0 until defaulted */
    uint32_t    fs, n, nstreams, npatches, nvoices, nev, cap;
    uint64_t    length_ns, hash;
    char        stream[TL_MAX_STREAMS][TL_NAME];
    tl_patch_t  patch[TL_MAX_PATCHES];
    char        vpatch[TL_MAX_VOICES][TL_NAME];   /* the voice statement's patch name, "" when absent */
    uint32_t    voice[TL_MAX_VOICES];             /* patch index */
    tl_event_t *ev;
    gen_program_t gen;
    uint32_t gen_clock, gen_sources, gen_lanes, gen_axioms;
    int render_error;
    /* The effects (fx/SPEC.md, section 7): the configuration the effects'
     * statements build, fx.on 0 without them; tl_check_header sets its
     * channels and block. */
    fx_cfg_t    fx;
    /* The engine's statements . */
    uint32_t    shapes;                           /* bit p: a present shape, including zero drive */
    uint32_t    filters;                          /* bit p: patch p has a filter statement (TL_MAX_PATCHES is 32) */
    uint32_t    routes;                           /* bit p: patch p has a route statement  */
    uint32_t    lfos, lfo_masks;                  /* bit p: patch p has an lfo statement , and gave its masks */
    uint32_t    pegs;                            /* bit p: patch p has a peg statement  */
    uint32_t    fenvs;                           /* bit p: patch p has a fenv statement  */
    uint32_t    amps;                            /* bit p: patch p has an amp statement  */
    /* The field routes (space/SPEC.md sections 11 to 16), draft2 only. */
    int         route;                            /* 0, or SP_FIELD_*: space amb1, amb3, binaural, or stereo with panlaw */
    int         have_order, have_pinna, have_yaw, have_panlaw;
    uint32_t    order;                            /* space binaural: 1 or 3; absent: 3 */
    int         pinna;                            /* SP_PINNA_* */
    uint32_t    pinna_d[5];                       /* SP_PINNA_D: thousandths */
    int32_t     yaw;                              /* millidegrees; absent: 0 */
    uint32_t    panlaw;                           /* Q1.15 */
    int32_t    *stems;                            /* --stems: Q2.30, nvoices per frame, or NULL */
    tl_wt_t     wt;                               /* : the banks; nbanks 0 without them */
    /* : every voice's Karplus-Strong buffers in two sets, since --fxref
     * renders two voice sets at once; NULL without a ksource statement. */
    int32_t     *ks_pool;
    fm_ks_view_t ks_view[2][TL_MAX_VOICES][FM_OPS];
    fm_assets_t  ks_assets[2][TL_MAX_VOICES];     /* the banks and the voice's buffers */
} tl_t;

static int tl_err(const char *path, unsigned ln, const char *fmt, ...);
static int tl_check_ks(tl_t *tl);

/* path NULL: an error of the pool of files as a whole. */
static int tl_err(const char *path, unsigned ln, const char *fmt, ...)
{
    va_list ap;
    if (path) fprintf(stderr, "%s:%u: ", path, ln);
    else fprintf(stderr, "timeline: ");
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    return 1;
}

/* Keep the established diagnostic shape while distinguishing host failures. */
static int tl_host_error(const char *path, unsigned ln, const char *message)
{
    (void)tl_err(path, ln, "%s", message);
    return HOST_ERROR;
}

/* The engine statement this build accepts: the core, its compile-time knobs
 * and the hash of its tables. */
static void tl_engine(char *buf, size_t size)
{
    snprintf(buf, size, "a1 sine_bits=%d exp_bits=%d phase64=%d env64=%d fb_avg=%d tables=%016llx",
             FM_SINE_BITS, FM_EXP_BITS, FM_PHASE64, FM_ENV64, FM_FB_AVG, (unsigned long long)fm_table_hash());
}

/* Splits a line into blank-separated tokens up to a '#'. Returns their
 * number, or -1 past TL_MAX_TOK. */
static int tl_split(char *s, char **tok)
{
    int n = 0;
    for (;;) {
        while (*s == ' ' || *s == '\t' || *s == '\r' || *s == '\n') s++;
        if (!*s || *s == '#') return n;
        if (n == TL_MAX_TOK) return -1;
        tok[n++] = s;
        while (*s && *s != ' ' && *s != '\t' && *s != '\r' && *s != '\n' && *s != '#') s++;
        if (*s == '#') { *s = 0; return n; }
        if (*s) *s++ = 0;
    }
}

/* Decimal digits only, at most max. */
static int tl_num(const char *s, uint64_t max, uint64_t *v)
{
    char *end;
    if (*s < '0' || *s > '9') return -1;
    errno = 0;
    *v = strtoull(s, &end, 10);
    return (errno || *end || *v > max) ? -1 : 0;
}

/* Exactly 16 lowercase hex digits. */
static int tl_hex(const char *s, uint64_t *v)
{
    if (strlen(s) != 16 || strspn(s, "0123456789abcdef") != 16) return -1;
    *v = strtoull(s, NULL, 16);
    return 0;
}

/* An optional '-' then decimal digits, magnitude at most max (pos events). */
static int tl_snum(const char *s, uint64_t max, int64_t *v)
{
    uint64_t u;
    int neg = *s == '-';
    if (tl_num(s + neg, max, &u)) return -1;
    *v = neg ? -(int64_t)u : (int64_t)u;
    return 0;
}

/* Native authoring metadata. The reader owns grammar and coupled checks;
 * this table owns scalar ranges and the discoverable field names. */
#define STATEMENT(id, word, section, once, low, high, wordpos) TS_##id,
#define PARAM(id, statement, field, position, type, low, high, unit, human, scale, clamp, def, mut, depends, checker, choices, form)
#define FORM(statement, id, position, match, values, checker)
#define FAMILY(id, path, checker)
enum {
#include "params.def"
    TS_COUNT
};
#undef STATEMENT
#undef PARAM
#define STATEMENT(id, word, section, once, low, high, wordpos)
#define PARAM(id, statement, field, position, type, low, high, unit, human, scale, clamp, def, mut, depends, checker, choices, form) TP_##id,
enum {
#include "params.def"
    TP_COUNT
};
#undef PARAM
#undef STATEMENT

typedef struct {
    const char *id, *name, *section, *once;
    int min_values, max_values, word_position;
} tl_statement_t;
typedef struct {
    const char *id, *name, *position, *type, *unit, *human, *scale, *clamp, *def, *depends, *checker, *choices, *form;
    unsigned statement, mutable;
    int64_t low;
    uint64_t high;
} tl_param_t;
typedef struct {
    unsigned statement;
    const char *id, *position, *match, *values, *checker;
} tl_form_t;
#define STATEMENT(id, word, section, once, low, high, wordpos) { #id, #word, #section, #once, low, high, wordpos },
#define PARAM(id, statement, field, position, type, low, high, unit, human, scale, clamp, def, mut, depends, checker, choices, form)
static const tl_statement_t TL_STATEMENTS[] = {
#include "params.def"
};
#undef STATEMENT
#undef PARAM
#define STATEMENT(id, word, section, once, low, high, wordpos)
#define PARAM(id, statement, field, position, type, low, high, unit, human, scale, clamp, def, mut, depends, checker, choices, form) \
    { #id, #field, position, #type, #unit, human, scale, clamp, def, depends, #checker, choices, #form, TS_##statement, mut, (int64_t)(low), (uint64_t)(high) },
static const tl_param_t TL_PARAMS[] = {
#include "params.def"
};
#undef PARAM
#undef FORM
#define PARAM(id, statement, field, position, type, low, high, unit, human, scale, clamp, def, mut, depends, checker, choices, form)
#define FORM(statement, id, position, match, values, checker) { TS_##statement, #id, position, #match, values, #checker },
static const tl_form_t TL_FORMS[] = {
#include "params.def"
};
#undef STATEMENT
#undef PARAM
#undef FORM
#undef FAMILY

static const tl_param_t *tl_param_at(unsigned statement, unsigned position, const char *form)
{
    char token[16];
    unsigned i;
    snprintf(token, sizeof token, "%u", position);
    for (i = 0; i < TP_COUNT; ++i)
        if (TL_PARAMS[i].statement == statement && !strcmp(TL_PARAMS[i].position, token)
            && !strcmp(TL_PARAMS[i].form, form)) return &TL_PARAMS[i];
    return NULL;
}

static int tl_param_error(const char *path, unsigned ln, char **tok, const tl_param_t *p, const char *value)
{
    const tl_statement_t *s;
    const char *name = "-", *op = NULL;
    if (!p) return tl_err(path, ln, "parameter table is missing a reader field");
    s = &TL_STATEMENTS[p->statement];
    if (!strcmp(s->section, "patch") || !strcmp(s->section, "asset") ||
        (!strcmp(s->section, "generator") && p->statement != TS_gen_clock)) name = tok[1];
    if (!strcmp(s->once, "per_patch_op") && strcmp(p->name, "OP")) op = tok[2];
    if (!strcmp(s->section, "event")) {
        name = tok[1];
        if (strcmp(p->name, "T_NS") && strcmp(p->name, "VOICE"))
            op = tok[p->statement == TS_event_gen || p->statement == TS_event_lane ? 3 : 2];
    }
    if (op) return tl_err(path, ln, "%s %s %s: %s '%s' is not %lld to %llu",
                         s->name, name, op, p->name, value, (long long)p->low, (unsigned long long)p->high);
    return tl_err(path, ln, "%s %s: %s '%s' is not %lld to %llu",
                  s->name, name, p->name, value, (long long)p->low, (unsigned long long)p->high);
}

static int tl_param_u(const char *path, unsigned ln, char **tok, const tl_param_t *p, const char *text, uint64_t *value)
{
    if (!p || tl_num(text, p->high, value) || (p->low > 0 && *value < (uint64_t)p->low))
        return tl_param_error(path, ln, tok, p, text);
    return 0;
}

static int tl_param_s(const char *path, unsigned ln, char **tok, const tl_param_t *p, const char *text, int64_t *value)
{
    const uint64_t negative = p && p->low < 0 ? (uint64_t)(-(p->low + 1)) + 1u : 0;
    const uint64_t limit = p && p->high > negative ? p->high : negative;
    if (!p || tl_snum(text, limit, value) || *value < p->low || (*value >= 0 && (uint64_t)*value > p->high))
        return tl_param_error(path, ln, tok, p, text);
    return 0;
}

/* Literal integer lexical rules remain tl_num/tl_snum's; table-bound callers
 * choose fields only after the existing statement's arity and enum checks. */
#define TL_PARAM_U(id, text, value) tl_param_u(path, ln, tok, &TL_PARAMS[TP_##id], text, value)
#define TL_PARAM_S(id, text, value) tl_param_s(path, ln, tok, &TL_PARAMS[TP_##id], text, value)
#define TL_AT_U(statement, position, text, value) tl_param_u(path, ln, tok, tl_param_at(TS_##statement, position, "all"), text, value)
#define TL_AT_S(statement, position, text, value) tl_param_s(path, ln, tok, tl_param_at(TS_##statement, position, "all"), text, value)

static int tl_params(void)
{
    char engine[160];
    unsigned i;
    tl_engine(engine, sizeof engine);
    printf("PARAMS version=1 engine=%s\n", engine);
    for (i = 0; i < TS_COUNT; ++i) {
        const tl_statement_t *s = &TL_STATEMENTS[i];
        printf("%s id=%s name=%s section=%s once=%s min_values=%d max_values=%d word_position=%d\n",
               !strcmp(s->section, "event") ? "EVENT" : "STATEMENT", s->id, s->name, s->section,
               s->once, s->min_values, s->max_values, s->word_position);
    }
    for (i = 0; i < TP_COUNT; ++i) {
        const tl_param_t *p = &TL_PARAMS[i];
        char low[32] = "none", high[32] = "none";
        if ((p->type[0] == 'u' || p->type[0] == 'i') && p->type[1] >= '0' && p->type[1] <= '9') {
            snprintf(low, sizeof low, "%lld", (long long)p->low);
            snprintf(high, sizeof high, "%llu", (unsigned long long)p->high);
        }
        printf("PARAM id=%s statement=%s name=%s position=%s type=%s lo=%s hi=%s unit=%s human=%s scale=%s clamp=%s default=%s mutable=%u depends=%s checker=%s choices=%s form=%s\n",
               p->id, TL_STATEMENTS[p->statement].id, p->name, p->position, p->type,
               low, high, p->unit, p->human, p->scale,
               p->clamp, p->def, p->mutable, p->depends, p->checker, p->choices, p->form);
    }
    for (i = 0; i < sizeof TL_FORMS / sizeof *TL_FORMS; ++i) {
        const tl_form_t *f = &TL_FORMS[i];
        printf("FORM statement=%s id=%s position=%s match=%s values=%s checker=%s\n",
               TL_STATEMENTS[f->statement].id, f->id, f->position, f->match, f->values, f->checker);
    }
#define STATEMENT(id, word, section, once, low, high, wordpos)
#define PARAM(id, statement, field, position, type, low, high, unit, human, scale, clamp, def, mut, depends, checker, choices, form)
#define FORM(statement, id, position, match, values, checker)
#define FAMILY(id, file, checker) printf("FAMILY id=%s path=%s checker=%s\n", #id, file, #checker);
#include "params.def"
#undef STATEMENT
#undef PARAM
#undef FORM
#undef FAMILY
    printf("PARAMS count=%u statements=%u forms=%u\n", (unsigned)TP_COUNT, (unsigned)TS_COUNT,
           (unsigned)(sizeof TL_FORMS / sizeof *TL_FORMS));
    return ferror(stdout) ? HOST_ERROR : 0;
}

static int tl_params_schema_check(void)
{
    unsigned i, j;
    for (i = 0; i < TS_COUNT; ++i) {
        const tl_statement_t *s = &TL_STATEMENTS[i];
        if (s->min_values < 0 || (s->max_values != -1 && s->max_values < s->min_values) ||
            s->word_position < 0 || s->word_position >= TL_MAX_TOK)
            return tl_err(NULL, 0, "params: invalid statement arity %s", s->id);
        for (j = 0; j < i; ++j)
            if (!strcmp(s->id, TL_STATEMENTS[j].id) ||
                (!strcmp(s->name, TL_STATEMENTS[j].name) &&
                 (!strcmp(s->section, "event") == !strcmp(TL_STATEMENTS[j].section, "event"))))
                return tl_err(NULL, 0, "params: duplicate statement %s", s->id);
    }
    for (i = 0; i < TP_COUNT; ++i) {
        const tl_param_t *p = &TL_PARAMS[i];
        unsigned forms = !strcmp(p->form, "all");
        if (p->statement >= TS_COUNT || (p->low >= 0 && (uint64_t)p->low > p->high) || p->mutable > 1)
            return tl_err(NULL, 0, "params: invalid parameter %s", p->id);
        for (j = 0; j < i; ++j)
            if (!strcmp(p->id, TL_PARAMS[j].id)) return tl_err(NULL, 0, "params: duplicate parameter %s", p->id);
        for (j = 0; j < sizeof TL_FORMS / sizeof *TL_FORMS; ++j)
            if (p->statement == TL_FORMS[j].statement && !strcmp(p->form, TL_FORMS[j].id)) ++forms;
        if (forms != 1) return tl_err(NULL, 0, "params: parameter %s has %u forms", p->id, forms);
    }
    for (i = 0; i < sizeof TL_FORMS / sizeof *TL_FORMS; ++i) {
        const tl_form_t *f = &TL_FORMS[i];
        if (f->statement >= TS_COUNT) return tl_err(NULL, 0, "params: invalid form %s", f->id);
        for (j = 0; j < i; ++j)
            if (f->statement == TL_FORMS[j].statement && !strcmp(f->id, TL_FORMS[j].id))
                return tl_err(NULL, 0, "params: duplicate form %s", f->id);
    }
    printf("PARAMS_SCHEMA ok statements=%u parameters=%u forms=%u\n", (unsigned)TS_COUNT,
           (unsigned)TP_COUNT, (unsigned)(sizeof TL_FORMS / sizeof *TL_FORMS));
    return ferror(stdout) ? HOST_ERROR : 0;
}

/* Writer spellings share table IDs: removing a registration is a build error. */
static void tl_write_stmt(FILE *f, unsigned statement, const char *format, ...)
{
    va_list ap;
    fputs(TL_STATEMENTS[statement].name, f);
    va_start(ap, format);
    vfprintf(f, format, ap);
    va_end(ap);
}

static int tl_name_ok(const char *s)
{
    size_t k = strlen(s);
    return k > 0 && k < TL_NAME
        && strspn(s, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.") == k;
}

static int tl_find_patch(const tl_t *tl, const char *name)
{
    uint32_t i;
    for (i = 0; i < tl->npatches; i++)
        if (!strcmp(tl->patch[i].name, name)) return (int)i;
    return -1;
}

/* Bus names and FX_BUS_BIT in fx/fx.h have the same row order. */
static const char *const FX_BUS_NAME[FX_BUSES] = { "chorus", "delay", "reverb", "duck", "flanger" };

/* One effects statement (fx/SPEC.md, section 7): 0 when read, 1 on an
 * error, -1 when tok[0] names none. */
static int tl_header_fx(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    static const struct { unsigned statement; uint32_t bit; const char *usage; } S[] = {
        { TS_dcblock, FX_ON_DC, "dcblock takes FC_MILLIHZ, 1 to 100000" },
        { TS_chorus, FX_ON_CHORUS, "chorus takes BASE_US 7000 to 25000, DEPTH_US 1000 to 5000 and RATE_MILLIHZ 100 to 3000" },
        { TS_flanger, FX_ON_FLANGER, "flanger takes BASE_US 100 to 10000, DEPTH_US 100 to BASE_US, RATE_MILLIHZ 0 to 2000, FEEDBACK_Q15 0 to 29491 and INVERT 0 or 1" },
        { TS_delay, FX_ON_DELAY, "delay takes TIME_US 50000 to 1000000, FEEDBACK_Q15 0 to 29491 and LOWPASS_HZ 20 to 20000" },
        { TS_pingpong, FX_ON_PINGPONG, "pingpong takes TIME_US 50000 to 1000000, FEEDBACK_Q15 0 to 29491, LOWPASS_HZ 20 to 20000 and HIGHPASS_HZ 0 to 2000" },
        { TS_reverb, FX_ON_REVERB, "reverb takes ROOM_PERMILLE and DAMP_PERMILLE, 0 to 1000" },
        { TS_compressor, FX_ON_COMPRESSOR, "compressor takes THRESHOLD_DB10 0 to 600, RATIO_TENTHS 10 to 200, ATTACK_MS 0 to 100, RELEASE_MS 1 to 5000 and MAKEUP_DB10 0 to 240" },
        { TS_limiter, FX_ON_LIMITER, "limiter takes THRESHOLD_DB10 0 to 200 and RELEASE_MS 1 to 5000" },
        { TS_softclip, FX_ON_SOFTCLIP, "softclip takes no value" },
        { TS_gain, FX_ON_GAIN, "gain takes GAIN_DB10, -600 to 240" },
        { TS_dither, FX_ON_DITHER, "dither takes SEED, 1 to 4294967295" },
        { TS_crush, FX_ON_CRUSH, "crush takes no value" },
        { TS_decodable, 0, "decodable takes no value" },
    };
    fx_cfg_t *c = &tl->fx;
    int64_t v[5] = { 0, 0, 0, 0, 0 };
    size_t s;
    int i;
    for (s = 0; s < sizeof S / sizeof S[0] && strcmp(tok[0], TL_STATEMENTS[S[s].statement].name); s++) {}
    if (s == sizeof S / sizeof S[0]) return -1;
    if (nt != 1 + TL_STATEMENTS[S[s].statement].min_values) return tl_err(path, ln, "%s", S[s].usage);
    if (!S[s].bit) {
        if (tl->decodable) return tl_err(path, ln, "second decodable statement");
        tl->decodable = 1;
        return 0;
    }
    for (i = 0; i < TL_STATEMENTS[S[s].statement].min_values; i++) {
        const tl_param_t *field = tl_param_at(S[s].statement, (unsigned)i + 1u, "all");
        if (!field) return tl_param_error(path, ln, tok, field, tok[1 + i]);
        if (field->low < 0) {
            if (tl_param_s(path, ln, tok, field, tok[1 + i], &v[i])) return 1;
        } else {
            uint64_t value;
            if (tl_param_u(path, ln, tok, field, tok[1 + i], &value)) return 1;
            v[i] = (int64_t)value;
        }
    }
    if ((S[s].bit == FX_ON_DELAY && (c->on & FX_ON_PINGPONG))                /* : one of them */
        || (S[s].bit == FX_ON_PINGPONG && (c->on & (FX_ON_DELAY | FX_ON_PINGPONG)) == FX_ON_DELAY))
        return tl_err(path, ln, "delay and pingpong share the delay bus: one of them");
    if (S[s].bit == FX_ON_FLANGER && v[1] > v[0]) return tl_err(path, ln, "%s", S[s].usage);
    if (c->on & S[s].bit) return tl_err(path, ln, "second %s statement", TL_STATEMENTS[S[s].statement].name);
    c->on |= S[s].bit == FX_ON_PINGPONG ? FX_ON_PINGPONG | FX_ON_DELAY : S[s].bit;
    if (S[s].bit == FX_ON_DC) c->dc_millihz = v[0];
    else if (S[s].bit == FX_ON_CHORUS) { c->chorus_base_us = v[0]; c->chorus_depth_us = v[1]; c->chorus_rate_millihz = v[2]; }
    else if (S[s].bit == FX_ON_FLANGER) {
        c->flanger_base_us = (uint32_t)v[0]; c->flanger_depth_us = (uint32_t)v[1];
        c->flanger_rate_millihz = (uint32_t)v[2]; c->flanger_fb_q15 = (uint32_t)v[3]; c->flanger_invert = (uint32_t)v[4];
    }
    else if (S[s].bit == FX_ON_DELAY || S[s].bit == FX_ON_PINGPONG) {
        c->delay_us = v[0]; c->delay_fb_q15 = v[1]; c->delay_lp_hz = v[2]; c->delay_hp_hz = v[3];
    }
    else if (S[s].bit == FX_ON_REVERB) { c->reverb_room = v[0]; c->reverb_damp = v[1]; }
    else if (S[s].bit == FX_ON_COMPRESSOR) {
        c->compressor_db10 = (uint32_t)v[0]; c->compressor_ratio_tenths = (uint32_t)v[1];
        c->compressor_attack_ms = (uint32_t)v[2]; c->compressor_release_ms = (uint32_t)v[3];
        c->compressor_makeup_db10 = (uint32_t)v[4];
    }
    else if (S[s].bit == FX_ON_LIMITER) { c->limiter_db10 = v[0]; c->limiter_release_ms = v[1]; }
    else if (S[s].bit == FX_ON_GAIN) c->gain_db10 = (int32_t)v[0];
    else if (S[s].bit == FX_ON_DITHER) c->dither_seed = (uint32_t)v[0];
    return 0;
}

enum { EV_GEN_SET = 40, EV_GEN_RESET, EV_GEN_LANE };

/* One host parameter schema supplies parser bounds, defaults and inspection.
 * The generator core remains the authority for coupled and text constraints. */
typedef struct {
    unsigned id, kinds;
    const char *name, *slot, *type, *unit;
    int64_t low, high, value;
    unsigned mutable, has_default;
} tl_gen_param_t;
#define GEN_KIND(id, name)
#define GEN_PARAM(id, name, kinds, slot, type, low, high, unit, mut, has, value) \
    { id, kinds, #name, #slot, #type, #unit, low, high, value, mut, has },
static const tl_gen_param_t TL_GEN_PARAMS[] = {
#include "../gen/params.def"
};
#undef GEN_PARAM
#undef GEN_KIND
#define GEN_KIND(id, name) [id] = #name,
#define GEN_PARAM(id, name, kinds, slot, type, low, high, unit, mut, has, value)
static const char *const TL_GEN_KIND[] = {
#include "../gen/params.def"
};
#undef GEN_PARAM
#undef GEN_KIND

static const tl_gen_param_t *tl_gen_param(unsigned kind, const char *name)
{
    size_t i;
    for (i = 0; i < sizeof TL_GEN_PARAMS / sizeof *TL_GEN_PARAMS; ++i) {
        const tl_gen_param_t *p = &TL_GEN_PARAMS[i];
        if (!strcmp(name, p->name) && (kind ? (p->kinds & (1u << (kind - 1))) : !p->kinds)) return p;
    }
    return NULL;
}

static int tl_gen_number(const char *token, const tl_gen_param_t *p, int64_t *value)
{
    uint64_t u;
    if (!p) return 1;
    if (p->low < 0) {
        const int64_t limit = -p->low > p->high ? -p->low : p->high;
        if (tl_snum(token, limit, value)) return 1;
    } else {
        if (tl_num(token, (uint64_t)p->high, &u)) return 1;
        *value = (int64_t)u;
    }
    return *value < p->low || *value > p->high;
}

static int tl_gen_read(const char *path, unsigned ln, char **tok, const tl_param_t *field,
                       const tl_gen_param_t *bound, const char *text, int64_t *value)
{
    tl_param_t p;
    if (!field || !bound) return tl_err(path, ln, "parameter table is missing a generator reader field");
    if (!tl_gen_number(text, bound, value)) return 0;
    p = *field;
    p.low = bound->low;
    p.high = (uint64_t)bound->high;
    if (!strcmp(p.name, "FIELDS") || !strcmp(p.name, "VALUE")) p.name = bound->name;
    return tl_param_error(path, ln, tok, &p, text);
}

#define TL_GEN_READ(id, bound, text, value) tl_gen_read(path, ln, tok, &TL_PARAMS[TP_##id], bound, text, value)

static int tl_gen_meta(const char *token, const char *name, int64_t *value)
{
    return tl_gen_number(token, tl_gen_param(0, name), value);
}

static int tl_gen_ref(const char *token, int8_t *value)
{
    int64_t v;
    if (!strcmp(token, "-")) { *value = -1; return 0; }
    if (tl_gen_meta(token, "source_id", &v)) return 1;
    *value = (int8_t)v;
    return 0;
}

static const gen_source_config_t *tl_gen_source(const tl_t *tl, unsigned id)
{
    unsigned i;
    for (i = 0; i < tl->gen.sources; ++i)
        if (tl->gen.source[i].id == id) return &tl->gen.source[i];
    return NULL;
}

static int tl_gen_word(const char *s, int empty)
{
    size_t i, n = strlen(s);
    const tl_gen_param_t *d = tl_gen_param(0, empty ? "rule_word" : "axiom_word");
    if (empty && !strcmp(s, "-")) return 1;
    if (n < (size_t)d->low || n > (size_t)d->high) return 0;
    for (i = 0; i < n; ++i) if (s[i] < 'A' || s[i] > 'H') return 0;
    return 1;
}

static int tl_header_gen(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    int64_t id, x;
    unsigned kind, j, fields;
    size_t i;
    if (!strcmp(tok[0], "gen_clock")) {
        if (nt != 2) return tl_err(path, ln, "gen_clock takes CTL 1 to 4096");
        if (TL_GEN_READ(gen_clock_ctl, tl_gen_param(0, "clock"), tok[1], &x)) return 1;
        if (tl->gen_clock) return tl_err(path, ln, "second gen_clock statement");
        if (x != FM_CTL) return tl_err(path, ln, "gen_clock %lld differs from FM_CTL=%d", (long long)x, FM_CTL);
        tl->gen_clock = (uint32_t)x;
        return 0;
    }
    if (!strcmp(tok[0], "gen")) {
        gen_source_config_t *s;
        if (nt < 6) return tl_err(path, ln, "gen takes ID KIND SEED DIV SLEW and every kind field");
        if (TL_GEN_READ(gen_id, tl_gen_param(0, "source_id"), tok[1], &id)) return 1;
        for (kind = 1; kind < sizeof TL_GEN_KIND / sizeof *TL_GEN_KIND; ++kind)
            if (!strcmp(tok[2], TL_GEN_KIND[kind])) break;
        if (kind == sizeof TL_GEN_KIND / sizeof *TL_GEN_KIND) return tl_err(path, ln, "gen KIND '%s' is unavailable", tok[2]);
        if ((tl->gen_sources >> id) & 1u) return tl_err(path, ln, "second gen ID %u", (unsigned)id);
        fields = 0;
        for (i = 0; i < sizeof TL_GEN_PARAMS / sizeof *TL_GEN_PARAMS; ++i)
            if ((TL_GEN_PARAMS[i].kinds & (1u << (kind - 1))) && TL_GEN_PARAMS[i].slot[0] == 'p') ++fields;
        if ((unsigned)nt != 6u + fields + (kind == GEN_EUCLID)) return tl_err(path, ln, "gen %u: wrong number of fields for %s", (unsigned)id, tok[2]);
        s = &tl->gen.source[id];
        memset(s, 0, sizeof *s); s->id = (uint8_t)id; s->kind = (uint8_t)kind; s->parent = -1;
        if (TL_GEN_READ(gen_seed, tl_gen_param(kind, "seed"), tok[3], &x)) return 1; s->seed = (uint32_t)x;
        if (TL_GEN_READ(gen_div, tl_gen_param(kind, "div"), tok[4], &x)) return 1; s->div = (uint16_t)x;
        if (TL_GEN_READ(gen_slew, tl_gen_param(kind, "slew"), tok[5], &x)) return 1; s->slew = (uint16_t)x;
        j = 6;
        for (i = 0; i < sizeof TL_GEN_PARAMS / sizeof *TL_GEN_PARAMS; ++i) {
            const tl_gen_param_t *p = &TL_GEN_PARAMS[i];
            if ((p->kinds & (1u << (kind - 1))) && p->slot[0] == 'p') {
                if (TL_GEN_READ(gen_fields, p, tok[j++], &x)) return 1;
                s->p[(unsigned)(p->slot[1] - '0')] = x;
            }
        }
        if (kind == GEN_EUCLID && tl_gen_ref(tok[j], &s->parent)) return tl_err(path, ln, "gen PARENT must be '-' or a source ID");
        tl->gen_sources |= 1u << id;
        return 0;
    }
    if (!strcmp(tok[0], "gen_axiom") || !strcmp(tok[0], "gen_rule")) {
        gen_text_config_t *t;
        int axiom = !strcmp(tok[0], "gen_axiom");
        if (nt != 4) return tl_err(path, ln, "%s takes ID %s", tok[0], axiom ? "DEPTH WORD" : "SYMBOL WORD_OR_DASH");
        if (tl_gen_read(path, ln, tok, &TL_PARAMS[axiom ? TP_gen_axiom_id : TP_gen_rule_id], tl_gen_param(0, "source_id"), tok[1], &id)) return 1;
        for (j = 0; j < tl->gen.texts && tl->gen.text[j].id != id; ++j) {}
        if (j == tl->gen.texts) {
            if (j == GEN_WORDS) return tl_err(path, ln, "more than %d generator text definitions", GEN_WORDS);
            tl->gen.texts++; tl->gen.text[j].id = (uint8_t)id;
        }
        t = &tl->gen.text[j];
        if (axiom) {
            if ((tl->gen_axioms >> id) & 1u) return tl_err(path, ln, "second gen_axiom ID %u", (unsigned)id);
            if (TL_GEN_READ(gen_axiom_depth, tl_gen_param(0, "axiom_depth"), tok[2], &x)) return 1;
            if (!tl_gen_word(tok[3], 0)) return tl_err(path, ln, "gen_axiom depth or word out of range");
            t->depth = (uint8_t)x; strcpy(t->axiom, tok[3]); tl->gen_axioms |= 1u << id;
        } else {
            unsigned symbol;
            if (strlen(tok[2]) != 1 || tok[2][0] < 'A' || tok[2][0] > 'H' || !tl_gen_word(tok[3], 1)) return tl_err(path, ln, "gen_rule needs symbol A..H and word A..H or '-'");
            symbol = (unsigned)(tok[2][0] - 'A');
            if ((t->rule_mask >> symbol) & 1u) return tl_err(path, ln, "second gen_rule symbol %s for ID %u", tok[2], (unsigned)id);
            t->rule_mask |= (uint8_t)(1u << symbol); if (strcmp(tok[3], "-")) strcpy(t->rule[symbol], tok[3]);
        }
        return 0;
    }
    if (!strcmp(tok[0], "gen_lane")) {
        static const char *const names[] = { "step_ticks", "base_cents", "span_cents", "velocity", "gate_ticks", "scale_mask" };
        int64_t v[6]; gen_lane_config_t *l;
        if (nt != 12) return tl_err(path, ln, "gen_lane takes ID VOICE GATE PITCH ACCENT STEP BASE SPAN VELOCITY GATE_TICKS SCALE_MASK");
        if (TL_GEN_READ(gen_lane_id, tl_gen_param(0, "lane_id"), tok[1], &id)) return 1;
        if ((tl->gen_lanes >> id) & 1u) return tl_err(path, ln, "second gen_lane ID %u", (unsigned)id);
        l = &tl->gen.lane[id]; memset(l, 0, sizeof *l); l->id = (uint8_t)id;
        if (TL_GEN_READ(gen_lane_voice, tl_gen_param(0, "voice"), tok[2], &x)) return 1; l->voice = (uint16_t)x;
        if (tl_gen_ref(tok[3], &l->gate) || tl_gen_ref(tok[4], &l->pitch) || tl_gen_ref(tok[5], &l->accent)) return tl_err(path, ln, "gen_lane source must be '-' or an ID");
        for (j = 0; j < 6; ++j)
            if (tl_gen_read(path, ln, tok, tl_param_at(TS_gen_lane, j + 6u, "all"), tl_gen_param(0, names[j]), tok[j + 6], &v[j])) return 1;
        l->step_ticks = (uint16_t)v[0]; l->base_cents = (int32_t)v[1]; l->span_cents = (uint16_t)v[2];
        l->velocity = (uint16_t)v[3]; l->gate_ticks = (uint16_t)v[4]; l->scale_mask = (uint16_t)v[5];
        tl->gen_lanes |= 1u << id;
        return 0;
    }
    return -1;
}

/* Immutable program and disjoint caller memory outlive each session bank. */
typedef struct {
    gen_bank_t bank;
    gen_memory_t memory;
    uint8_t buffer[4][GEN_WORD_CAP];
    uint64_t events;
} tl_gen_runtime_t;

static gen_status_t tl_gen_init_status(const tl_t *tl, tl_gen_runtime_t *r)
{
    unsigned i;
    memset(r, 0, sizeof *r);
    for (i = 0; i < 2; ++i) {
        r->memory.scratch[i] = r->buffer[i]; r->memory.scratch_capacity[i] = GEN_WORD_CAP;
        r->memory.word[i] = r->buffer[i + 2]; r->memory.word_capacity[i] = GEN_WORD_CAP;
    }
    return gen_init(&r->bank, &tl->gen, &r->memory, tl->fs, tl->gen_clock);
}

static int tl_gen_init(const tl_t *tl, tl_gen_runtime_t *r)
{
    const gen_status_t s = tl_gen_init_status(tl, r);
    if (s.code) return tl_err(NULL, 0, "generator init refused: code=%u id=%u field=%u", s.code, s.id, s.field);
    return 0;
}

static int tl_check_gen(tl_t *tl)
{
    unsigned i, j, n;
    tl_gen_runtime_t *r;
    int rc;
    if (!tl->gen_clock && (tl->gen_sources || tl->gen_lanes || tl->gen.texts)) return tl_err(NULL, 0, "generator definitions need gen_clock");
    for (i = 0; i < tl->npatches; ++i) for (j = 0; j < FM_MOD_SLOTS; ++j)
        if ((tl->patch[i].gen_mods >> j) & 1u) {
            const unsigned id = tl->patch[i].gen_id[j];
            if (!tl->gen_clock) return tl_err(NULL, 0, "mod generator binding needs gen_clock");
            if (!((tl->gen_sources >> id) & 1u)) return tl_err(NULL, 0, "mod generator ID %u is undefined", id);
            if (tl->patch[i].gen_axis[j] && tl->gen.source[id].kind != GEN_HENON) return tl_err(NULL, 0, "mod gen.%u.y needs henon", id);
        }
    if (!tl->gen_clock) return 0;
    for (i = 0; i < tl->gen.texts; ++i)
        if (!((tl->gen_axioms >> tl->gen.text[i].id) & 1u)) return tl_err(NULL, 0, "gen_rule ID %u lacks gen_axiom", tl->gen.text[i].id);
    if (tl->gen.texts == 2 && tl->gen.text[0].id > tl->gen.text[1].id) {
        gen_text_config_t t = tl->gen.text[0]; tl->gen.text[0] = tl->gen.text[1]; tl->gen.text[1] = t;
    }
    for (i = n = 0; i < GEN_SOURCES; ++i) if ((tl->gen_sources >> i) & 1u) tl->gen.source[n++] = tl->gen.source[i];
    tl->gen.sources = (uint8_t)n;
    for (i = n = 0; i < GEN_LANES; ++i) if ((tl->gen_lanes >> i) & 1u) {
        if (tl->gen.lane[i].voice >= tl->nvoices) return tl_err(NULL, 0, "gen_lane %u: voice %u is undefined", i, tl->gen.lane[i].voice);
        tl->gen.lane[n++] = tl->gen.lane[i];
    }
    tl->gen.lanes = (uint8_t)n;
    r = (tl_gen_runtime_t *)malloc(sizeof *r);
    if (!r) return tl_host_error(NULL, 0, "out of memory");
    rc = tl_gen_init(tl, r); free(r); return rc;
}

static int tl_event_gen(const tl_t *tl, const char *path, unsigned ln, char **tok, int nt, tl_event_t *e)
{
    int64_t id, value;
    const tl_gen_param_t *p;
    const gen_source_config_t *s;
    if (strcmp(tok[2], "gen") && strcmp(tok[2], "lane")) return -1;
    if (!tl->gen_clock) return tl_err(path, ln, "generator event needs gen_clock");
    if (nt != 6) return tl_err(path, ln, "generator event takes TIME STREAM gen|lane ID PARAM VALUE");
    if (!strcmp(tok[2], "lane")) {
        if (TL_GEN_READ(event_lane_id, tl_gen_param(0, "lane_id"), tok[3], &id)) return 1;
        if (!((tl->gen_lanes >> id) & 1u)) return tl_err(path, ln, "lane event ID is undefined");
        if (strcmp(tok[4], "enable")) return tl_err(path, ln, "lane takes enable 0|1");
        if (TL_GEN_READ(event_lane_value, tl_gen_param(GEN_WHITE, "enabled"), tok[5], &value)) return 1;
        e->type = EV_GEN_LANE; e->gen_id = (uint8_t)id; e->gen_value = value; return 0;
    }
    if (TL_GEN_READ(event_gen_id, tl_gen_param(0, "source_id"), tok[3], &id)) return 1;
    if (!(s = tl_gen_source(tl, (unsigned)id))) return tl_err(path, ln, "gen event ID is undefined");
    p = tl_gen_param(s->kind, tok[4]);
    if (!p || !p->mutable) return tl_err(path, ln, "gen %u: PARAM '%s' is immutable, unavailable or out of range", (unsigned)id, tok[4]);
    if (TL_GEN_READ(event_gen_value, p, tok[5], &value)) return 1;
    e->type = !strcmp(p->slot, "reset") ? EV_GEN_RESET : EV_GEN_SET;
    e->gen_id = (uint8_t)id; e->gen_value = value; e->gen_field = (uint16_t)p->id;
    e->gen_param = !strcmp(p->slot, "enabled") ? GEN_PARAM_ENABLED : !strcmp(p->slot, "slew") ? GEN_PARAM_SLEW : (uint8_t)(GEN_PARAM_P0 + p->slot[1] - '0');
    return 0;
}

static gen_status_t tl_gen_apply_status(tl_gen_runtime_t *r, const tl_event_t *e)
{
    gen_status_t s = { GEN_OK, 0, GEN_FIELD_NONE };
    if (e->type == EV_GEN_SET) s = gen_set(&r->bank, e->gen_id, e->gen_param, e->gen_value);
    else if (e->type == EV_GEN_RESET) s = gen_reset(&r->bank, e->gen_id, (uint32_t)e->gen_value);
    else if (e->type == EV_GEN_LANE) s = gen_lane_enable(&r->bank, e->gen_id, (uint8_t)e->gen_value);
    return s;
}

static int tl_gen_apply(tl_gen_runtime_t *r, const tl_event_t *e)
{
    const gen_status_t s = tl_gen_apply_status(r, e);
    if (s.code) return tl_err(NULL, 0, "generator event refused: code=%u id=%u field=%u", s.code, s.id, s.field);
    return 0;
}

static int tl_gen_params(void)
{
    unsigned i;
    printf("GEN_PARAMS version=1\n");
    for (i = 1; i < sizeof TL_GEN_KIND / sizeof *TL_GEN_KIND; ++i) printf("GEN_KIND id=%u name=%s\n", i, TL_GEN_KIND[i]);
    for (i = 0; i < sizeof TL_GEN_PARAMS / sizeof *TL_GEN_PARAMS; ++i) {
        const tl_gen_param_t *p = &TL_GEN_PARAMS[i];
        printf("GEN_PARAM id=%u name=%s kinds=%u slot=%s type=%s min=%lld max=%lld unit=%s mutable=%u has_default=%u default=%lld\n",
               p->id, p->name, p->kinds, p->slot, p->type, (long long)p->low, (long long)p->high, p->unit, p->mutable, p->has_default, (long long)p->value);
    }
    printf("GEN_PARAMS count=%u\n", (unsigned)(sizeof TL_GEN_PARAMS / sizeof *TL_GEN_PARAMS));
    return ferror(stdout) ? HOST_ERROR : 0;
}

/* : route PATCH OPS CARRIERS FROM>TO:WEIGHT[:pm|am|ring|sync]..., 0 or 1 as
 * tl_header returns; PM weights 1 to 1024, AM and ring 1 to 256, sync exactly
 * 256 . tl_check_header then holds the patch to operators 0 to OPS - 1. */
static int tl_header_route(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    fm_route_t r;
    uint64_t ops, car, from, to, w;
    int p, k;
    memset(&r, 0, sizeof r);
    if (nt < 4) return tl_err(path, ln, "route takes a patch name, OPS 2 to %d, a carrier mask and edges FROM>TO:WEIGHT", FM_OPS);
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_PARAM_U(route_ops, tok[2], &ops)) return 1;
    if (TL_PARAM_U(route_carriers, tok[3], &car)) return 1;
    if (car > (1u << ops) - 1u)
        return tl_err(path, ln, "route %s: carrier mask '%s' is not 1 to %u", tok[1], tok[3], (1u << ops) - 1u);
    for (k = 4; k < nt; k++) {
        char e[TL_NAME], *gt, *co, *suffix;
        unsigned kind = 0;
        if (strlen(tok[k]) >= sizeof e || !(gt = strchr(strcpy(e, tok[k]), '>')) || !(co = strchr(gt, ':')))
            return tl_err(path, ln, "route %s: edge '%s' is not FROM>TO:WEIGHT", tok[1], tok[k]);
        *gt++ = 0;
        *co++ = 0;
        suffix = strchr(co, ':');
        if (suffix) {
            *suffix++ = 0;
            if (!strcmp(suffix, "pm")) kind = 0;
            else if (!strcmp(suffix, "am")) kind = 1;
            else if (!strcmp(suffix, "ring")) kind = 2;
            else if (!strcmp(suffix, "sync")) kind = 3;
            else return tl_err(path, ln, "route %s: edge '%s': KIND is not pm, am, ring or sync", tok[1], tok[k]);
        }
        if (TL_PARAM_U(route_from, e, &from) || TL_PARAM_U(route_to, gt, &to)) return 1;
        if (from >= ops || to >= ops)
            return tl_err(path, ln, "route %s: edge '%s' names an operator at or above OPS %u", tok[1], tok[k], (unsigned)ops);
        if (from == to)
            return tl_err(path, ln, "route %s: edge '%s' feeds an operator to itself, which its FEEDBACK_Q10 does", tok[1], tok[k]);
        { static const unsigned fields[] = { TP_route_weight_pm, TP_route_weight_am, TP_route_weight_ring, TP_route_weight_sync };
          if (tl_param_u(path, ln, tok, &TL_PARAMS[fields[kind]], co, &w)) return 1; }
        if (r.w[from][to]) return tl_err(path, ln, "route %s: edge %u>%u listed twice", tok[1], (unsigned)from, (unsigned)to);
        r.w[from][to] = (uint16_t)w;
        r.kind[from][to] = (uint8_t)kind;
    }
    r.ops = (uint8_t)ops;
    r.carriers = (uint8_t)car;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->routes & (1u << p)) return tl_err(path, ln, "patch %s has a second route statement", tok[1]);
    tl->routes |= 1u << p;
    tl->patch[p].p.route = r;
    return 0;
}

/* Explicit identity remains enabled; complete headers resolve sine applicability. */
static int tl_header_formant(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t slot, center, q, level;
    int p;
    if (nt != 6) return tl_err(path, ln, "formant takes PATCH SLOT CENTER_MILLIHZ Q_MILLI LEVEL_DB10");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_AT_U(formant, 2, tok[2], &slot)) return 1;
    if (TL_AT_U(formant, 3, tok[3], &center)) return 1;
    if (TL_AT_U(formant, 4, tok[4], &q)) return 1;
    if (TL_AT_U(formant, 5, tok[5], &level)) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++; strcpy(tl->patch[p].name, tok[1]);
    }
    if ((tl->patch[p].formant >> slot) & 1u) return tl_err(path, ln, "patch %s has a second formant SLOT %u", tok[1], (unsigned)slot);
    tl->patch[p].formant |= 1u << slot;
    tl->patch[p].p.formant[slot] = (fm_formant_patch_t){ (uint32_t)center, (uint16_t)q, (uint16_t)level, 1 };
    return 0;
}

static int tl_header_vector(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t x, y;
    int p;
    if (nt != 4) return tl_err(path, ln, "vector takes PATCH X_Q15 Y_Q15");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_PARAM_U(vector_x_q15, tok[2], &x) || TL_PARAM_U(vector_y_q15, tok[3], &y)) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++; strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->patch[p].vector) return tl_err(path, ln, "patch %s has a second vector statement", tok[1]);
    tl->patch[p].vector = 1;
    tl->patch[p].p.vector = (fm_vector_patch_t){ (uint16_t)x, (uint16_t)y, 1 };
    return 0;
}

static int tl_header_pd(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t op, knee;
    int p;
    if (nt != 4) return tl_err(path, ln, "pd takes PATCH OP KNEE_Q15");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_AT_U(pd, 2, tok[2], &op)) return 1;
    if (TL_AT_U(pd, 3, tok[3], &knee)) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->patch[p].pd & (1u << op)) return tl_err(path, ln, "patch %s has a second pd statement for operator %u", tok[1], (unsigned)op);
    tl->patch[p].pd |= 1u << op;
    tl->patch[p].p.pd[op].knee_q15 = (uint16_t)knee;
    tl->patch[p].p.pd[op].enabled = 1;
    return 0;
}

static int tl_find_bank(const tl_t *tl, const char *name)
{
    uint32_t i;
    for (i = 0; i < tl->wt.nbanks; i++)
        if (!strcmp(tl->wt.bank[i].name, name)) return (int)i;
    return -1;
}

/* : wtbank, wtdata, wthash, harmonic and wtuse (design-hybrid.md
 * section 2). A bank's wtbank comes before its other statements; a wtuse
 * may come before its bank, which the header check resolves. */
static int tl_header_wt(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    tl_wt_t *w = &tl->wt;
    tl_wt_bank_t *bk;
    uint64_t a, b, c, d;
    int k;
    if (!strcmp(tok[0], "wtuse")) {
        if (nt != 5) return tl_err(path, ln, "wtuse takes PATCH OP BANK POSITION_Q15");
        if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
        if (TL_AT_U(wtuse, 2, tok[2], &a)) return 1;
        if (!tl_name_ok(tok[3])) return tl_err(path, ln, "bad bank name '%s'", tok[3]);
        if (TL_AT_U(wtuse, 4, tok[4], &b)) return 1;
        k = tl_find_patch(tl, tok[1]);
        if (k < 0) {
            if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
            k = (int)tl->npatches++;
            strcpy(tl->patch[k].name, tok[1]);
        }
        if (tl->patch[k].wt & (1u << a)) return tl_err(path, ln, "patch %s has a second wtuse statement for operator %u", tok[1], (unsigned)a);
        tl->patch[k].wt |= 1u << a;
        strcpy(tl->patch[k].wt_bank[a], tok[3]);
        tl->patch[k].p.wt[a] = (fm_wt_patch_t){ (uint16_t)b, 0, 1 };
        return 0;
    }
    if (nt < 2 || !tl_name_ok(tok[1])) return tl_err(path, ln, "%s needs a bank name", tok[0]);
    k = tl_find_bank(tl, tok[1]);
    if (!strcmp(tok[0], "wtbank")) {
        if (nt != 5) return tl_err(path, ln, "wtbank takes BANK FRAMES LENGTH 12");
        if (k >= 0) return tl_err(path, ln, "bank %s has a second wtbank statement", tok[1]);
        if (w->nbanks == FM_WT_SLOTS) return tl_err(path, ln, "more than %d banks", FM_WT_SLOTS);
        if (TL_AT_U(wtbank, 2, tok[2], &a)) return 1;
        if (TL_AT_U(wtbank, 3, tok[3], &b)) return 1;
        if (b & (b - 1u))
            return tl_err(path, ln, "wtbank %s: LENGTH %s is not a power of two from %d to %d", tok[1], tok[3], FM_WT_LENGTH_MIN, FM_WT_LENGTH_MAX);
        if (strcmp(tok[4], "12")) return tl_err(path, ln, "wtbank %s: BANDS %s is not 12", tok[1], tok[4]);
        bk = &w->bank[w->nbanks++];
        memset(bk, 0, sizeof *bk);
        strcpy(bk->name, tok[1]);
        bk->frames = (uint32_t)a; bk->length = (uint32_t)b; bk->offset = w->used;
        w->used += FM_WT_BANDS * bk->frames * bk->length;   /* at most TL_WT_POOL: 8 banks of the largest size */
        return 0;
    }
    if (k < 0) return tl_err(path, ln, "%s names bank %s before its wtbank statement", tok[0], tok[1]);
    bk = &w->bank[k];
    if (!strcmp(tok[0], "wthash")) {
        if (nt != 3) return tl_err(path, ln, "wthash takes BANK HEX");
        if (bk->have_hash) return tl_err(path, ln, "bank %s has a second wthash statement", tok[1]);
        if (tl_hex(tok[2], &bk->hash)) return tl_err(path, ln, "wthash %s: HEX %s is not 16 lowercase hex digits", tok[1], tok[2]);
        bk->have_hash = 1;
        return 0;
    }
    if (!strcmp(tok[0], "harmonic")) {
        if (nt != 6) return tl_err(path, ln, "harmonic takes BANK FRAME NUMBER LEVEL_DB10 PHASE_U16");
        if (bk->mode == 1) return tl_err(path, ln, "bank %s mixes harmonic and wtdata statements", tok[1]);
        if (TL_AT_U(harmonic, 2, tok[2], &a)) return 1;
        if (a >= bk->frames) return tl_err(path, ln, "harmonic %s: FRAME %s is not 0 to %u", tok[1], tok[2], bk->frames - 1u);
        if (TL_AT_U(harmonic, 3, tok[3], &b)) return 1;
        if (TL_AT_U(harmonic, 4, tok[4], &c)) return 1;
        if (TL_AT_U(harmonic, 5, tok[5], &d)) return 1;
        if ((w->hseen[k][a][(b - 1u) >> 3] >> ((b - 1u) & 7u)) & 1u)
            return tl_err(path, ln, "bank %s has a second harmonic FRAME %u NUMBER %u", tok[1], (unsigned)a, (unsigned)b);
        w->hseen[k][a][(b - 1u) >> 3] |= (uint8_t)(1u << ((b - 1u) & 7u));
        w->h[k][bk->nh++] = (fm_wt_harmonic_t){ (uint16_t)a, (uint16_t)b, (uint16_t)c, (uint16_t)d };
        bk->mode = 2;
        return 0;
    }
    if (!strcmp(tok[0], "wtdata")) {
        int j;
        if (nt < 6 || nt > 21) return tl_err(path, ln, "wtdata takes BANK BAND FRAME START and 1 to 16 samples");
        if (bk->mode == 2) return tl_err(path, ln, "bank %s mixes harmonic and wtdata statements", tok[1]);
        if (TL_AT_U(wtdata, 2, tok[2], &a)) return 1;
        if (TL_AT_U(wtdata, 3, tok[3], &b)) return 1;
        if (b >= bk->frames) return tl_err(path, ln, "wtdata %s: FRAME %s is not 0 to %u", tok[1], tok[3], bk->frames - 1u);
        if (TL_AT_U(wtdata, 4, tok[4], &c)) return 1;
        if (c >= bk->length || c + (uint64_t)(nt - 5) > bk->length)
            return tl_err(path, ln, "wtdata %s: START %s and %d samples pass LENGTH %u", tok[1], tok[4], nt - 5, bk->length);
        for (j = 5; j < nt; j++) {
            const uint32_t at = bk->offset + ((uint32_t)a * bk->frames + (uint32_t)b) * bk->length + (uint32_t)c + (uint32_t)(j - 5);
            int64_t s;
            if (TL_PARAM_S(wtdata_sample, tok[j], &s)) return 1;
            if ((w->filled[at >> 5] >> (at & 31u)) & 1u)
                return tl_err(path, ln, "bank %s has a second sample at BAND %u FRAME %u index %u", tok[1], (unsigned)a, (unsigned)b,
                              (unsigned)c + (unsigned)(j - 5));
            w->filled[at >> 5] |= 1u << (at & 31u);
            w->pool[at] = (int16_t)s;
        }
        bk->mode = 1;
        return 0;
    }
    return tl_err(path, ln, "unknown wavetable statement '%s'", tok[0]);
}

/* : completes every bank after the headers, before rendering: a wtdata
 * bank must be complete and match its wthash; a harmonic bank is expanded
 * and must match a given wthash. Then each voice binds the same assets. */
static int tl_check_wt(tl_t *tl)
{
    tl_wt_t *w = &tl->wt;
    uint32_t k, i;
    for (k = 0; k < w->nbanks; k++) {
        tl_wt_bank_t *bk = &w->bank[k];
        const uint32_t count = FM_WT_BANDS * bk->frames * bk->length;
        int16_t *d = w->pool + bk->offset;
        uint64_t h;
        if (!bk->mode) return tl_err(NULL, 0, "bank %s has no wtdata or harmonic statements", bk->name);
        if (bk->mode == 1) {
            for (i = 0; i < count; i++)
                if (!((w->filled[(bk->offset + i) >> 5] >> ((bk->offset + i) & 31u)) & 1u))
                    return tl_err(NULL, 0, "bank %s lacks the sample at BAND %u FRAME %u index %u", bk->name,
                                  i / (bk->frames * bk->length), i / bk->length % bk->frames, i % bk->length);
            if (!bk->have_hash) return tl_err(NULL, 0, "bank %s has wtdata without a wthash statement", bk->name);
        } else {
            const int r = fm_wt_additive(d, count, bk->frames, bk->length, w->h[k], bk->nh);
            if (r != FM_BIND_OK) return tl_err(NULL, 0, "bank %s: fm_wt_additive refuses its harmonics (%d)", bk->name, r);
        }
        h = wt_content_hash(bk->frames, bk->length, d);
        if (bk->have_hash && h != bk->hash)
            return tl_err(NULL, 0, "bank %s: wthash %016llx does not match its content %016llx", bk->name,
                          (unsigned long long)bk->hash, (unsigned long long)h);
        bk->hash = h; bk->have_hash = 1;
        w->view[k] = (fm_wt_view_t){ bk->frames, bk->length, FM_WT_BANDS, count, d, h };
    }
    w->assets = (fm_assets_t){ (uint32_t)sizeof(fm_assets_t), w->nbanks, w->view, 0, NULL };
    if (w->nbanks) {                                /* so that each render's binding cannot fail */
        static const fm_patch_t none;
        fm_voice_t v;
        int r;
        fm_voice_init(&v, &none, tl->fs);
        if ((r = fm_voice_bind_assets(&v, &w->assets)) != FM_BIND_OK) return tl_err(NULL, 0, "the banks' binding is refused (%d)", r);
    }
    return 0;
}

/* fm_voice_init with the timeline's banks bound , and the voice's
 * Karplus-Strong buffers of set 0 or 1; 0 ok. */
static int tl_voice_init_set(const tl_t *tl, fm_voice_t *v, uint32_t i, unsigned set)
{
    const fm_assets_t *a = tl->ks_pool ? &tl->ks_assets[set][i] : tl->wt.assets.wt_count ? &tl->wt.assets : NULL;
    fm_voice_init(v, &tl->patch[tl->voice[i]].p, tl->fs);
    if (!a) return 0;
    if (fm_voice_bind_assets(v, a) == FM_BIND_OK) return 0;
    return tl_err(NULL, 0, "voice %u: the banks' binding is refused", (unsigned)i);
}

static int tl_voice_init(const tl_t *tl, fm_voice_t *v, uint32_t i)
{
    return tl_voice_init_set(tl, v, i, 0u);
}

/* : ksource PATCH OP LOSS_Q15 SEED BUFFER_SAMPLES (design-hybrid.md
 * section 5). The header check refuses the edges the source cannot take;
 * every voice of the patch gets buffers of its own. */
static int tl_header_ksource(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t op, loss, seed, buf;
    int p;
    if (nt != 6) return tl_err(path, ln, "ksource takes PATCH OP LOSS_Q15 SEED BUFFER_SAMPLES");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_AT_U(ksource, 2, tok[2], &op)) return 1;
    if (TL_AT_U(ksource, 3, tok[3], &loss)) return 1;
    if (TL_AT_U(ksource, 4, tok[4], &seed)) return 1;
    if (TL_AT_U(ksource, 5, tok[5], &buf)) return 1;
    if (buf & (buf - 1u))
        return tl_err(path, ln, "ksource %s: BUFFER_SAMPLES %s is not a power of two from %d to %d", tok[1], tok[5],
                      FM_KS_BUFFER_MIN, FM_KS_BUFFER_MAX);
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->patch[p].kssrc & (1u << op)) return tl_err(path, ln, "patch %s has a second ksource statement for operator %u", tok[1], (unsigned)op);
    tl->patch[p].kssrc |= 1u << op;
    tl->patch[p].p.ks[op] = (fm_ks_patch_t){ (uint32_t)seed, (uint16_t)loss, (uint16_t)buf, 1 };
    return 0;
}

/* : default shift omits canonically; presence still detects duplicates. */
static int tl_header_noise(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t op, shift;
    int p;
    if (nt != 4) return tl_err(path, ln, "noise takes PATCH OP SHIFT");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_AT_U(noise, 2, tok[2], &op)) return 1;
    if (TL_AT_U(noise, 3, tok[3], &shift)) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->patch[p].noise & (1u << op)) return tl_err(path, ln, "patch %s has a second noise statement for operator %u", tok[1], (unsigned)op);
    tl->patch[p].noise |= 1u << op;
    tl->patch[p].p.noise[op].shift = (uint16_t)shift;
    return 0;
}

/* : independent left index slope; presence is resolved after headers. */
static int tl_header_ks(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t op, slope;
    int p;
    if (nt != 4) return tl_err(path, ln, "ks takes PATCH OP LO_SLOPE_DB10");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_AT_U(ks, 2, tok[2], &op)) return 1;
    if (TL_AT_U(ks, 3, tok[3], &slope)) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->patch[p].ks & (1u << op)) return tl_err(path, ln, "patch %s has a second ks statement for operator %u", tok[1], (unsigned)op);
    tl->patch[p].ks |= 1u << op;
    tl->patch[p].p.eg[op].left_slope_db10 = (uint16_t)slope;
    return 0;
}

/*  (, provisional grammar): kstype PATCH OP TARGET RIGHT LEFT
 * with TARGET index, level or both and each side - or +; fbtype PATCH OP
 * TYPE with TYPE classic, pre, square or presquare. The defaults (index
 * - -, classic) normalize away; presence is resolved after the headers. */
static const char *const TL_KS_TARGET[3] = { "index", "level", "both" };
static const char *const TL_FB_TYPE[4] = { "classic", "pre", "square", "presquare" };

static int tl_header_kstype(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t op;
    unsigned target, right, left;
    int p;
    if (nt != 6) return tl_err(path, ln, "kstype takes PATCH OP TARGET RIGHT LEFT");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_AT_U(kstype, 2, tok[2], &op)) return 1;
    for (target = 0; target < 3u && strcmp(tok[3], TL_KS_TARGET[target]); target++) {}
    if (target == 3u) return tl_err(path, ln, "kstype %s: TARGET %s is not index, level or both", tok[1], tok[3]);
    if (strcmp(tok[4], "-") && strcmp(tok[4], "+")) return tl_err(path, ln, "kstype %s: RIGHT %s is not - or +", tok[1], tok[4]);
    if (strcmp(tok[5], "-") && strcmp(tok[5], "+")) return tl_err(path, ln, "kstype %s: LEFT %s is not - or +", tok[1], tok[5]);
    right = tok[4][0] == '+'; left = tok[5][0] == '+';
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->patch[p].kstype & (1u << op)) return tl_err(path, ln, "patch %s has a second kstype statement for operator %u", tok[1], (unsigned)op);
    tl->patch[p].kstype |= 1u << op;
    tl->patch[p].p.ext[op].ks_target = (uint8_t)target;
    tl->patch[p].p.ext[op].ks_curve = (uint8_t)(right | left << 1);
    return 0;
}

static int tl_header_fbtype(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t op;
    unsigned type;
    int p;
    if (nt != 4) return tl_err(path, ln, "fbtype takes PATCH OP TYPE");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_AT_U(fbtype, 2, tok[2], &op)) return 1;
    for (type = 0; type < 4u && strcmp(tok[3], TL_FB_TYPE[type]); type++) {}
    if (type == 4u) return tl_err(path, ln, "fbtype %s: TYPE %s is not classic, pre, square or presquare", tok[1], tok[3]);
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->patch[p].fbtype & (1u << op)) return tl_err(path, ln, "patch %s has a second fbtype statement for operator %u", tok[1], (unsigned)op);
    tl->patch[p].fbtype |= 1u << op;
    tl->patch[p].p.ext[op].fb_type = (uint8_t)type;
    return 0;
}

/*  (, provisional grammar): egloop PATCH OP loops the
 * operator's attack, decay and decay2 while the key is held. Absent, the
 * operator does not loop; presence is resolved after the headers. */
static int tl_header_egloop(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t op;
    int p;
    if (nt != 3) return tl_err(path, ln, "egloop takes PATCH OP");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_AT_U(egloop, 2, tok[2], &op)) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->patch[p].egloop & (1u << op)) return tl_err(path, ln, "patch %s has a second egloop statement for operator %u", tok[1], (unsigned)op);
    tl->patch[p].egloop |= 1u << op;
    tl->patch[p].p.ext[op].loop = 1u;
    return 0;
}

/* : delay and a downward second decay.
 * Resolve operator presence after all patch/route headers have been read. */
static int tl_header_eg(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t op, delay, decay2, sustain2;
    int p;
    if (nt != 6) return tl_err(path, ln, "eg takes PATCH OP DELAY_MS DECAY2_MS SUSTAIN2_DB10");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_AT_U(eg, 2, tok[2], &op)) return 1;
    if (TL_AT_U(eg, 3, tok[3], &delay)) return 1;
    if (TL_AT_U(eg, 4, tok[4], &decay2)) return 1;
    if (TL_AT_U(eg, 5, tok[5], &sustain2)) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->patch[p].eg & (1u << op)) return tl_err(path, ln, "patch %s has a second eg statement for operator %u", tok[1], (unsigned)op);
    tl->patch[p].eg |= 1u << op;
    tl->patch[p].p.eg[op].delay_ms = (uint16_t)delay;
    tl->patch[p].p.eg[op].decay2_ms = (uint16_t)decay2;
    tl->patch[p].p.eg[op].sustain2_db10 = (uint16_t)sustain2;
    return 0;
}

/* 's lfo statement, once per patch (fm_lfo_patch_t), 9, 13 or 14 tokens;
 * tl_check_header gives absent masks every operator of the patch. */
static int tl_header_lfo(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t x[12] = { 0 };
    fm_lfo_patch_t *l;
    int p, k;
    if (nt != 9 && nt != 13 && nt != 14)
        return tl_err(path, ln, "lfo takes a patch name, WAVE RATE_MILLIHZ DELAY_MS FADE_MS PITCH_CENTS LEVEL_DB10 INDEX_Q10, "
                                "then PHASE_U16 MASK_PITCH MASK_LEVEL MASK_INDEX [FREE] or none");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    for (k = 0; k < nt - 2; k++) {
        if (k == 1) {
            const tl_param_t *rate = &TL_PARAMS[TP_lfo_rate_millihz];
            /* Native ordering: representation and low bound here, then the
             * control Nyquist before the semantic high bound after headers. */
            if (tl_num(tok[k + 2], UINT32_MAX, &x[k]) || x[k] < (uint64_t)rate->low)
                return tl_param_error(path, ln, tok, rate, tok[k + 2]);
        } else if (TL_AT_U(lfo, (unsigned)k + 2u, tok[k + 2], &x[k])) return 1;
        if (k == 11 && x[k] == 1)
            return tl_err(path, ln, "lfo %s: FREE %s is not 0 or 2", tok[1], tok[k + 2]);
    }
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->lfos & (1u << p)) return tl_err(path, ln, "patch %s has a second lfo statement", tok[1]);
    tl->lfos |= 1u << p;
    tl->patch[p].lfo_path = path;
    tl->patch[p].lfo_line = ln;
    strcpy(tl->patch[p].lfo_rate_token, tok[3]);
    if (nt >= 13) tl->lfo_masks |= 1u << p;
    l = &tl->patch[p].p.lfo;
    l->wave         = (uint16_t)x[0];
    l->rate_millihz = (uint32_t)x[1];
    l->delay_ms     = (uint16_t)x[2];
    l->fade_ms      = (uint16_t)x[3];
    l->pitch_cents  = (uint16_t)x[4];
    l->level_db10   = (uint16_t)x[5];
    l->index_q10    = (uint16_t)x[6];
    l->phase_u16    = (uint16_t)x[7];
    l->mask_pitch   = (uint8_t)x[8];
    l->mask_level   = (uint8_t)x[9];
    l->mask_index   = (uint8_t)x[10];
    l->free_run     = (uint8_t)x[11];
    return 0;
}

/* : signed cent levels, nonnegative millisecond times, optional
 * all-operator mask. The reader refuses what the direct API clamps. */
static int tl_header_peg(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    fm_peg_patch_t peg = { { 0 }, { 0 }, 0 };
    uint64_t value;
    int64_t level;
    int p, k;
    if (nt != 10 && nt != 11)
        return tl_err(path, ln, "peg takes a patch name, L1 T1 L2 T2 L3 T3 L4 T4 and an optional MASK");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    for (k = 0; k < 4; k++) {
        if (TL_AT_S(peg, 2u + 2u * (unsigned)k, tok[2 + 2*k], &level)) return 1;
        if (TL_AT_U(peg, 3u + 2u * (unsigned)k, tok[3 + 2*k], &value)) return 1;
        peg.level_cents[k] = (int16_t)level;
        peg.time_ms[k] = (uint16_t)value;
    }
    if (nt == 11) {
        if (TL_AT_U(peg, 10, tok[10], &value)) return 1;
        peg.mask = (uint16_t)value;
    }
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->pegs & (1u << p)) return tl_err(path, ln, "patch %s has a second peg statement", tok[1]);
    tl->pegs |= 1u << p;
    tl->patch[p].p.peg = peg;
    return 0;
}

/* : filter envelope with optional C4 reference frequency. */
static int tl_header_fenv(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t x[7] = { 0, 0, 0, 0, 0, 0, 261626 };
    int64_t depth;
    fm_fenv_patch_t *f;
    int p, k;
    if (nt != 8 && nt != 9)
        return tl_err(path, ln, "fenv takes a patch name, ATTACK_MS DECAY_MS SUSTAIN_PERMILLE RELEASE_MS DEPTH_CENTS TRACK_PERMILLE and an optional REF_MILLIHZ");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    for (k = 0; k < 4; k++)
        if (TL_AT_U(fenv, (unsigned)k + 2u, tok[k+2], &x[k])) return 1;
    if (TL_AT_S(fenv, 6, tok[6], &depth)) return 1;
    if (TL_AT_U(fenv, 7, tok[7], &x[5])) return 1;
    if (nt == 9 && TL_AT_U(fenv, 8, tok[8], &x[6])) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->fenvs & (1u << p)) return tl_err(path, ln, "patch %s has a second fenv statement", tok[1]);
    tl->fenvs |= 1u << p;
    f = &tl->patch[p].p.fenv;
    f->attack_ms = (uint16_t)x[0]; f->decay_ms = (uint16_t)x[1];
    f->sustain_permille = (uint16_t)x[2]; f->release_ms = (uint16_t)x[3];
    f->depth_cents = (int16_t)depth; f->track_permille = (uint16_t)x[5];
    f->ref_millihz = (uint32_t)x[6];
    return 0;
}

/*  (, provisional grammar): amp PATCH ATTACK_MS DECAY_MS
 * SUSTAIN_DB10 RELEASE_MS enables the voice amp envelope in
 * fm_op_patch_t's units; absent, it is bypassed. */
static int tl_header_amp(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t x[4];
    int p, i;
    if (nt != 6) return tl_err(path, ln, "amp takes PATCH ATTACK_MS DECAY_MS SUSTAIN_DB10 RELEASE_MS");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    for (i = 0; i < 4; i++)
        if (TL_AT_U(amp, (unsigned)i + 2u, tok[2 + i], &x[i])) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->amps & (1u << p)) return tl_err(path, ln, "patch %s has a second amp statement", tok[1]);
    tl->amps |= 1u << p;
    tl->patch[p].p.amp.attack_ms = (uint16_t)x[0];
    tl->patch[p].p.amp.decay_ms = (uint16_t)x[1];
    tl->patch[p].p.amp.sustain_db10 = (uint16_t)x[2];
    tl->patch[p].p.amp.release_ms = (uint16_t)x[3];
    tl->patch[p].p.amp.enabled = 1u;
    return 0;
}

/* A shape is present even at zero drive; omission alone bypasses it.
 * : the fifth token "pre" puts it before the formant bank and the filter. */
static const char *const TL_SHAPE_KIND[5] = { "", "hard", "cubic", "fold", "asym" };

static int tl_header_shape(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t drive;
    int p, kind;
    if (nt != 4 && nt != 5)
        return tl_err(path, ln, "shape takes a patch name, DRIVE_DB10, hard, cubic, fold or asym and an optional pre");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_AT_U(shape, 2, tok[2], &drive)) return 1;
    for (kind = 1; kind < 5 && strcmp(tok[3], TL_SHAPE_KIND[kind]); ++kind) {}
    if (kind == 5) return tl_err(path, ln, "shape %s: KIND '%s' is not hard, cubic, fold or asym", tok[1], tok[3]);
    if (nt == 5 && strcmp(tok[4], "pre"))   /* the kind comes first: "pre asym" fails on KIND above */
        return tl_err(path, ln, "shape takes a patch name, DRIVE_DB10, hard, cubic, fold or asym and an optional pre");
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->shapes & (1u << p)) return tl_err(path, ln, "patch %s has a second shape statement", tok[1]);
    tl->shapes |= 1u << p;
    tl->patch[p].p.shape.drive_db10 = (uint16_t)drive;
    tl->patch[p].p.shape.kind = (uint16_t)kind;
    tl->patch[p].p.shape.pos = (uint16_t)(nt == 5);
    return 0;
}

/* The engine's header statements , in draft1 and
 * draft2: -1 when tok is not one of them, else 0 or 1 as tl_header
 * returns. A filter, shape, route, lfo, peg or fenv statement may come before its patch's
 * operators. */
/*  names are stable text identifiers; generator/hybrid ranges are reserved. */
static const char *const TL_MOD_SOURCE[24] = {
    "lfo", "peg", "fenv", "eg0", "eg1", "eg2", "eg3", "vel", "note",
    NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    "ctl0", "ctl1", "ctl2", "ctl3", "ctl4", "ctl5", "ctl6", "ctl7"
};
static const char *const TL_MOD_TARGET[38] = {
    "", "pitch", "level", "index", "cutoff", "q", "lfo_rate", "lfo_pitch", "lfo_level", "lfo_index", [32] = "wt_pos", [33] = "pd_knee", [34] = "vector_x", [35] = "vector_y", [36] = "formant_center", [37] = "formant_gain"
};

static int tl_header_mod(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t slot, shift = 3;
    int64_t amount;
    unsigned source, target, op = 0, gen_id = 0, gen_axis = 0, gen_binding = 0;
    int p;
    fm_mod_slot_patch_t *m;
    if (nt != 6 && nt != 7) return tl_err(path, ln, "mod takes PATCH SLOT SOURCE TARGET AMOUNT [SHIFT]");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    if (TL_PARAM_U(mod_slot, tok[2], &slot)) return 1;
    for (source = 0; source < 24; ++source)
        if (TL_MOD_SOURCE[source] && !strcmp(tok[3], TL_MOD_SOURCE[source])) break;
    if (source == 24) {
        const char *dot;
        char number[16];
        int64_t id;
        if (strncmp(tok[3], "gen.", 4) || !(dot = strchr(tok[3] + 4, '.'))
            || dot == tok[3] + 4 || (size_t)(dot - tok[3] - 4) >= sizeof number
            || (strcmp(dot, ".x") && strcmp(dot, ".y")))
            return tl_err(path, ln, "mod SOURCE '%s' is unavailable; generators use gen.ID.x or gen.ID.y", tok[3]);
        memcpy(number, tok[3] + 4, (size_t)(dot - tok[3] - 4)); number[dot - tok[3] - 4] = 0;
        if (tl_gen_meta(number, "source_id", &id)) return tl_err(path, ln, "mod generator ID out of range");
        source = 64u + (unsigned)slot; gen_id = (unsigned)id; gen_axis = dot[1] == 'y'; gen_binding = 1;
    }
    for (target = 1; target <= 9; ++target) {
        const size_t len = strlen(TL_MOD_TARGET[target]);
        if (target <= 3) {
            if (!strncmp(tok[4], TL_MOD_TARGET[target], len) && tok[4][len] >= '0'
                && tok[4][len] <= '3' && tok[4][len + 1] == '\0') { op = (unsigned)(tok[4][len] - '0'); break; }
        } else if (!strcmp(tok[4], TL_MOD_TARGET[target])) break;
    }
    if (target == 10 && !strncmp(tok[4], "wt_pos", 6) && tok[4][6] >= '0'
        && tok[4][6] <= '3' && tok[4][7] == '\0') { target = 32; op = (unsigned)(tok[4][6] - '0'); }
    if (target == 10 && !strncmp(tok[4], "pd_knee", 7) && tok[4][7] >= '0'
        && tok[4][7] <= '3' && tok[4][8] == '\0') { target = 33; op = (unsigned)(tok[4][7] - '0'); }
    if (target == 10 && !strcmp(tok[4], "vector_x")) target = 34;
    if (target == 10 && !strcmp(tok[4], "vector_y")) target = 35;
    if (target == 10) {
        unsigned hybrid;
        for (hybrid = 36; hybrid <= 37; ++hybrid) {
            const size_t len = strlen(TL_MOD_TARGET[hybrid]);
            if (!strncmp(tok[4], TL_MOD_TARGET[hybrid], len) && tok[4][len] >= '0'
                && tok[4][len] <= '2' && tok[4][len + 1] == '\0') {
                target = hybrid; op = (unsigned)(tok[4][len] - '0'); break;
            }
        }
    }
    if (target == 10) return tl_err(path, ln, "mod TARGET '%s' is unavailable; hybrid targets are reserved", tok[4]);
    if (tl_param_s(path, ln, tok, tl_param_at(TS_mod, 5, TL_MOD_TARGET[target]), tok[5], &amount)) return 1;
    if (nt == 7 && TL_PARAM_U(mod_shift, tok[6], &shift)) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if ((tl->patch[p].mods >> slot) & 1u) return tl_err(path, ln, "patch %s has a second mod SLOT %u", tok[1], (unsigned)slot);
    tl->patch[p].mods |= 1u << slot;
    if (gen_binding) {
        tl->patch[p].gen_mods |= 1u << slot; tl->patch[p].gen_id[slot] = (uint8_t)gen_id; tl->patch[p].gen_axis[slot] = (uint8_t)gen_axis;
    }
    m = &tl->patch[p].p.mod[slot];
    m->source = (uint16_t)source; m->target = (uint8_t)target; m->target_index = (uint8_t)op;
    m->amount = (int32_t)amount; m->shift = (uint8_t)shift;
    return 0;
}

/* Validate every authored row, including amount zero, after all headers.
 * This duplicates the public raw boundary's applicability in reader terms,
 * so missing features cannot silently turn an authored slot off. */
static int tl_check_mod(const tl_patch_t *p, uint32_t fs)
{
    const fm_patch_t *q = &p->p;
    const unsigned nops = q->route.ops ? q->route.ops : 2u;
    const unsigned carriers = q->route.ops ? q->route.carriers : 1u;
    uint64_t rate = q->lfo.rate_millihz;
    unsigned i, rates = 0;
    if (rate < 10) rate = 10;
    if (rate > 50000) rate = 50000;
    for (i = 0; i < FM_MOD_SLOTS; ++i) if ((p->mods >> i) & 1u) {
        const fm_mod_slot_patch_t *m = &q->mod[i];
        const unsigned src = m->source, dst = m->target, op = m->target_index;
        if ((src == 0 || (dst >= 6 && dst <= 9)) && !q->lfo.rate_millihz)
            return tl_err(NULL, 0, "patch %s: mod SLOT %u needs a real lfo", p->name, i);
        if (src == 1 && !(q->peg.level_cents[0] || q->peg.level_cents[1] || q->peg.level_cents[2] || q->peg.level_cents[3]))
            return tl_err(NULL, 0, "patch %s: mod SLOT %u needs an active peg", p->name, i);
        if (src == 2 && (!q->filter.mode || !(q->fenv.depth_cents || q->fenv.track_permille)))
            return tl_err(NULL, 0, "patch %s: mod SLOT %u needs an active fenv", p->name, i);
        if (src >= 3 && src <= 6 && src - 3 >= nops)
            return tl_err(NULL, 0, "patch %s: mod SLOT %u names absent eg operator", p->name, i);
        if ((dst <= 3 || dst == 32 || dst == 33) && op >= nops)
            return tl_err(NULL, 0, "patch %s: mod SLOT %u targets absent operator", p->name, i);
        if (dst == 32 && (!q->wt[op].enabled || q->op[op].wave || q->pd[op].enabled))
            return tl_err(NULL, 0, "patch %s: mod SLOT %u needs an enabled wavetable", p->name, i);
        if (dst == 33 && (!q->pd[op].enabled || q->op[op].wave))
            return tl_err(NULL, 0, "patch %s: mod SLOT %u needs enabled sine pd", p->name, i);
        if ((dst == 34 || dst == 35) && !q->vector.enabled)
            return tl_err(NULL, 0, "patch %s: mod SLOT %u needs enabled vector", p->name, i);
        if ((dst == 36 || dst == 37) && !q->formant[op].enabled)
            return tl_err(NULL, 0, "patch %s: mod SLOT %u needs enabled formant slot %u", p->name, i, op);
        if (dst == 2 && !((carriers >> op) & 1u))
            return tl_err(NULL, 0, "patch %s: mod SLOT %u level target is not a carrier", p->name, i);
        if (dst == 3) {
            unsigned to, pm = !q->route.ops && op == 1;
            if (q->route.ops) for (to = 0; to < nops; ++to)
                if (to != op && q->route.w[op][to] && q->route.kind[op][to] == 0) pm = 1;
            if (!pm) return tl_err(NULL, 0, "patch %s: mod SLOT %u index target has no outgoing PM", p->name, i);
        }
        if ((dst == 4 || dst == 5) && !q->filter.mode)
            return tl_err(NULL, 0, "patch %s: mod SLOT %u needs a filter", p->name, i);
        if (dst == 6 && m->amount) { rate += (uint32_t)(m->amount < 0 ? -m->amount : m->amount); ++rates; }
    }
    if (rate > 50000) rate = 50000;
    if (rates && rate * FM_CTL >= (uint64_t)500 * fs)
        return tl_err(NULL, 0, "patch %s: mod reachable lfo_rate %u reaches the control Nyquist", p->name, (unsigned)rate);
    return 0;
}

static const char *const TL_FILTER_MODE[5] = { "", "lp", "bp", "hp", "notch" };

static int tl_header_engine(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    uint64_t c, q;
    int p, m;
    if (FM_CTL != 64 && (!strcmp(tok[0], "mod") || !strcmp(tok[0], "lfo") || !strcmp(tok[0], "peg") || !strcmp(tok[0], "fenv")))
        return tl_err(path, ln, "%s statement needs FM_CTL=64; this build has FM_CTL=%d", tok[0], FM_CTL);
    if (!strcmp(tok[0], "mod")) return tl_header_mod(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "shape")) return tl_header_shape(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "amp")) return tl_header_amp(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "route")) return tl_header_route(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "eg")) return tl_header_eg(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "ks")) return tl_header_ks(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "kstype")) return tl_header_kstype(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "fbtype")) return tl_header_fbtype(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "egloop")) return tl_header_egloop(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "formant")) return tl_header_formant(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "vector")) return tl_header_vector(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "pd")) return tl_header_pd(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "wtbank") || !strcmp(tok[0], "wtdata") || !strcmp(tok[0], "wthash")
        || !strcmp(tok[0], "harmonic") || !strcmp(tok[0], "wtuse")) return tl_header_wt(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "ksource")) return tl_header_ksource(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "noise")) return tl_header_noise(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "lfo")) return tl_header_lfo(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "peg")) return tl_header_peg(tl, path, ln, tok, nt);
    if (!strcmp(tok[0], "fenv")) return tl_header_fenv(tl, path, ln, tok, nt);
    if (strcmp(tok[0], "filter")) return -1;
    if (nt != 5 && nt != 6) return tl_err(path, ln, "filter takes a patch name, lp, bp, hp or notch, a cutoff in mHz and Q in thousandths, optionally x2");
    if (nt == 6 && strcmp(tok[5], "x2")) return tl_err(path, ln, "filter stages must be x2 when present");
    if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
    for (m = 1; m < 5 && strcmp(tok[2], TL_FILTER_MODE[m]); m++) {}
    if (m == 5) return tl_err(path, ln, "filter mode '%s' is not lp, bp, hp or notch", tok[2]);
    if (TL_AT_U(filter, 3, tok[3], &c)) return 1;
    if (TL_AT_U(filter, 4, tok[4], &q)) return 1;
    p = tl_find_patch(tl, tok[1]);
    if (p < 0) {
        if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
        p = (int)tl->npatches++;
        strcpy(tl->patch[p].name, tok[1]);
    }
    if (tl->filters & (1u << p)) return tl_err(path, ln, "patch %s has a second filter statement", tok[1]);
    tl->filters |= 1u << p;
    tl->patch[p].p.filter.mode           = (uint16_t)m;
    tl->patch[p].p.filter.cutoff_millihz = (uint32_t)c;
    tl->patch[p].p.filter.q_milli        = (uint16_t)q;
    tl->patch[p].p.filter.stages         = nt == 6 ? 2 : 0;
    return 0;
}

/* The field routes (space/SPEC.md sections 11 to 16): their statements,
 * the yaw event, the renders and --stems. The binaural route's table
 * header comes from the build (make BINAURAL_H=PATH); without one a
 * space binaural record is refused. */
enum { EV_YAW = 32 };                                 /* the listener's yaw, millidegrees in x */
int field_test(void);                                 /* field_ref.c */
int field_cost(void);
#ifdef SP_BINAURAL_H
#include SP_BINAURAL_H
#if SP_BIN_TAPS != SP_FIELD_TAPS
#error "the binaural table's length is not the decoder's"
#endif
#endif

static int tl_field_route(const char *s)
{
    if (!strcmp(s, "amb1")) return SP_FIELD_AMB1;
    if (!strcmp(s, "amb3")) return SP_FIELD_AMB3;
    if (!strcmp(s, "binaural")) return SP_FIELD_BINAURAL;
    return 0;
}

static const char *tl_field_name(const tl_t *tl)
{
    return tl->route == SP_FIELD_AMB1 ? "amb1" : tl->route == SP_FIELD_AMB3 ? "amb3" : tl->route == SP_FIELD_BINAURAL ? "binaural" : "off";
}

static uint32_t tl_field_ch(const tl_t *tl)
{
    return tl->route == SP_FIELD_AMB1 ? 4u : tl->route == SP_FIELD_AMB3 ? 16u : 2u;
}

/* order, pinna, yaw, panlaw; -1 for a statement not theirs. */
static int tl_header_field(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    const char *k = tok[0];
    uint64_t v;
    int64_t s;
    int i;
    if (!strcmp(k, "order")) {
        if (nt != 2) return tl_err(path, ln, "order takes 1 or 3");
        if (TL_AT_U(header_order, 1, tok[1], &v)) return 1;
        if (v != 1 && v != 3) return tl_err(path, ln, "order takes 1 or 3");
        if (tl->have_order++) return tl_err(path, ln, "second order statement");
        tl->order = (uint32_t)v;
    } else if (!strcmp(k, "pinna")) {
        if (tl->have_pinna++) return tl_err(path, ln, "second pinna statement");
        if (nt == 2 && !strcmp(tok[1], "off")) tl->pinna = SP_PINNA_OFF;
        else if (nt == 2 && !strcmp(tok[1], "pbnh")) tl->pinna = SP_PINNA_PBNH;
        else if (nt == 2 && !strcmp(tok[1], "rd")) tl->pinna = SP_PINNA_RD;
        else if (nt == 6) {
            for (i = 0; i < 5; i++) {
                if (tl_param_u(path, ln, tok, tl_param_at(TS_header_pinna, 1u + (unsigned)i, "weights"), tok[1 + i], &v)) return 1;
                tl->pinna_d[i] = (uint32_t)v;
            }
            tl->pinna = SP_PINNA_D;
        } else return tl_err(path, ln, "pinna takes off, pbnh, rd or five D in thousandths, 0 to 1000");
    } else if (!strcmp(k, "yaw")) {
        if (nt != 2) return tl_err(path, ln, "yaw takes millidegrees, -%d to %d", SP_YAW_MAX, SP_YAW_MAX);
        if (TL_AT_S(header_yaw, 1, tok[1], &s)) return 1;
        if (tl->have_yaw++) return tl_err(path, ln, "second yaw statement");
        tl->yaw = (int32_t)s;
    } else if (!strcmp(k, "panlaw")) {
        if (nt != 2) return tl_err(path, ln, "panlaw takes the width W in Q1.15, 1 to 32768");
        if (TL_AT_U(header_panlaw, 1, tok[1], &v)) return 1;
        if (tl->have_panlaw++) return tl_err(path, ln, "second panlaw statement");
        tl->panlaw = (uint32_t)v;
    } else return -1;
    return 0;
}

/* After pass 1, with n known. */
static int tl_check_field(tl_t *tl)
{
    if (tl->draft == 1 && (tl->have_order || tl->have_pinna || tl->have_yaw || tl->have_panlaw))
        return tl_err(NULL, 0, "order, pinna, yaw and panlaw need timeline draft2");
    if (tl->have_order && tl->route != SP_FIELD_BINAURAL) return tl_err(NULL, 0, "order needs space binaural");
    if ((tl->have_pinna || tl->have_yaw) && !tl->route) return tl_err(NULL, 0, "pinna and yaw need space amb1, amb3 or binaural");
    if (tl->have_panlaw) {
        if (!tl->space) return tl_err(NULL, 0, "panlaw needs space stereo");
        tl->route = SP_FIELD_PANLAW;
    }
    if (tl->route == SP_FIELD_BINAURAL) {
        if (!tl->have_order) tl->order = 3;
#ifdef SP_BINAURAL_H
        if (tl->fs != sp_bin_fs[0] && tl->fs != sp_bin_fs[1])
            return tl_err(NULL, 0, "space binaural needs fs %u or %u, the table's rates", (unsigned)sp_bin_fs[0], (unsigned)sp_bin_fs[1]);
#else
        return tl_err(NULL, 0, "no binaural table in this build (make BINAURAL_H=PATH)");
#endif
    }
    if (tl->route == SP_FIELD_PANLAW && tl->fx.on)
        return tl_err(NULL, 0, "the effects need space off, ordinary stereo, amb1, amb3 or binaural; panlaw is refused");
    if ((tl->route == SP_FIELD_AMB1 || tl->route == SP_FIELD_AMB3)
        && (tl->fx.on & (FX_ON_COMPRESSOR | FX_ON_LIMITER | FX_ON_SOFTCLIP | FX_ON_DITHER)))
        return tl_err(NULL, 0, "amb1/amb3 effects refuse compressor, limiter, softclip and dither; gain, dcblock and source crusher are allowed");
    if (tl->pinna && tl->fs > SP_PINNA_FS_MAX) return tl_err(NULL, 0, "pinna needs fs at most %d", SP_PINNA_FS_MAX);
    if (tl->route && (uint64_t)tl->n * tl_field_ch(tl) > UINT32_MAX)
        return tl_err(NULL, 0, "a render of %u frames of %u channels is past 2^32 - 1 samples", tl->n, tl_field_ch(tl));
    return 0;
}

/* T_NS STREAM 0 yaw MDEG: the listener's, so its voice is 0. */
static int tl_event_field(tl_t *tl, const char *path, unsigned ln, char **tok, int nt, tl_event_t *e)
{
    int64_t s;
    if (tl->draft != 2) return tl_err(path, ln, "yaw needs timeline draft2");
    if (nt != 5) return tl_err(path, ln, "yaw takes millidegrees, -%d to %d", SP_YAW_MAX, SP_YAW_MAX);
    if (TL_AT_S(event_yaw, 4, tok[4], &s)) return 1;
    if (e->voice) return tl_err(path, ln, "yaw is the listener's: its voice is 0");
    if (!tl->route || tl->route == SP_FIELD_PANLAW) return tl_err(path, ln, "yaw needs space amb1, amb3 or binaural");
    e->type = EV_YAW;
    e->x = (int32_t)s;
    return 0;
}

/* The statements present, in tl_read's order. */
static void tl_write_field(FILE *f, const tl_t *tl)
{
    if (tl->have_order) tl_write_stmt(f, TS_header_order, " %u\n", tl->order);
    if (tl->have_pinna) {
        if (tl->pinna == SP_PINNA_D)
            tl_write_stmt(f, TS_header_pinna, " %u %u %u %u %u\n", tl->pinna_d[0], tl->pinna_d[1], tl->pinna_d[2], tl->pinna_d[3], tl->pinna_d[4]);
        else tl_write_stmt(f, TS_header_pinna, " %s\n", tl->pinna == SP_PINNA_PBNH ? "pbnh" : tl->pinna == SP_PINNA_RD ? "rd" : "off");
    }
    if (tl->have_yaw) tl_write_stmt(f, TS_header_yaw, " %d\n", (int)tl->yaw);
    if (tl->have_panlaw) tl_write_stmt(f, TS_header_panlaw, " %u\n", tl->panlaw);
}

/* Shared native/model configuration; no initialization or attachment here. */
static sp_field_cfg_t tl_field_cfg(const tl_t *tl)
{
    sp_field_cfg_t cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.route = tl->route;
    cfg.order = tl->order;
    cfg.block = tl->block;
    cfg.near_mm = tl->near;
    cfg.fs = tl->fs;
    cfg.yaw_mdeg = tl->yaw;
    cfg.pinna = tl->pinna;
    memcpy(cfg.pinna_d, tl->pinna_d, sizeof cfg.pinna_d);
    cfg.panlaw_q15 = tl->panlaw;
#ifdef SP_BINAURAL_H
    if (tl->route == SP_FIELD_BINAURAL) {
        int fi = tl->fs == sp_bin_fs[1];
        cfg.table = tl->order == 1 ? &sp_bin_o1[fi][0][0] : &sp_bin_o3[fi][0][0];
        cfg.table_shift = SP_BIN_SHIFT;
    }
#endif
    return cfg;
}

/* Initialization can refuse; check it on every render and rewind. */
static int tl_field_init(const tl_t *tl, sp_stereo_t *sp)
{
    const sp_field_cfg_t cfg = tl_field_cfg(tl);
    return sp_field_init((sp_field_t *)sp, &cfg);
}

static size_t tl_fx_mem_bytes(const tl_t *tl)
{
    return tl->route ? fx_field_mem_bytes(&tl->fx, tl->fs) : fx_mem_bytes(&tl->fx, tl->fs);
}

static int tl_fx_init(const tl_t *tl, fx_t *fx)
{
    return tl->route ? fx_field_init(fx, tl->fs, &tl->fx, fx + 1, tl_fx_mem_bytes(tl))
                     : fx_init(fx, tl->fs, &tl->fx, fx + 1, tl_fx_mem_bytes(tl));
}

static uint32_t tl_field_mix(const tl_t *tl, fm_voice_t *vs, sp_stereo_t *sp, int16_t *pcm, uint32_t pos, uint32_t len)
{
    int16_t *o = pcm + (size_t)tl_field_ch(tl) * pos;
    if (tl->stems) return sp_field_render_q30((sp_field_t *)sp, tl->stems + (size_t)pos * tl->nvoices, tl->nvoices, tl->nvoices, o, len);
    return sp_field_render((sp_field_t *)sp, vs, tl->nvoices, o, len);
}

static uint32_t tl_le(const unsigned char *b, int k)
{
    uint32_t v = 0;
    while (k--) v = v << 8 | b[k];
    return v;
}

/* New strict PCM32/Q2.30 branch only. The legacy effective bits16 walk
 * keeps its old format, short-input and padding behavior unchanged. */
static int stems_read_exact(FILE *f, void *bytes, size_t count, const char *path)
{
    if (fread(bytes, 1, count, f) == count) return 0;
    return ferror(f) ? io_fail(path, "read") : tl_err(NULL, 0, "%s: truncated Q30 RIFF chunk", path);
}

static int stems_discard(FILE *f, uint64_t count, const char *path)
{
    unsigned char bytes[4096];
    while (count) {
        const size_t n = count < sizeof bytes ? (size_t)count : sizeof bytes;
        const int rc = stems_read_exact(f, bytes, n, path);
        if (rc) return rc;
        count -= n;
    }
    return 0;
}

/* Owns neither FILE nor its close. Call only after old effective fmt selects32. */
static int tl_load_stems_q30(tl_t *tl, FILE *f, const char *path)
{
    unsigned char h[16], frame[4u * TL_MAX_VOICES];
    uint64_t left;
    uint32_t outer, bytes, t, i, channels = 0;
    int have_fmt = 0, have_data = 0, rc;
    if (fseek(f, 0, SEEK_SET)) return io_fail(path, "seek");
    if ((rc = stems_read_exact(f, h, 12, path))) return rc;
    outer = tl_le(h + 4, 4);
    if (memcmp(h, "RIFF", 4) || memcmp(h + 8, "WAVE", 4) || outer < 4u)
        return tl_err(NULL, 0, "%s: malformed Q30 RIFF object", path);
    left = outer - 4u;
    while (left) {
        uint64_t padded;
        if (left < 8u) return tl_err(NULL, 0, "%s: partial Q30 chunk header", path);
        if ((rc = stems_read_exact(f, h, 8, path))) return rc;
        bytes = tl_le(h + 4, 4);
        left -= 8u;
        padded = (uint64_t)bytes + (bytes & 1u);
        if (padded > left) return tl_err(NULL, 0, "%s: Q30 chunk exceeds RIFF object", path);
        if (!memcmp(h, "fmt ", 4)) {
            if (have_fmt || have_data || bytes != 16u)
                return tl_err(NULL, 0, "%s: Q30 requires one16-byte fmt before data", path);
            if ((rc = stems_read_exact(f, h, 16, path))) return rc;
            channels = tl_le(h + 2, 2);
            if (tl_le(h, 2) != 1u || tl_le(h + 14, 2) != 32u || channels != tl->nvoices
                || tl_le(h + 4, 4) != tl->fs || tl_le(h + 12, 2) != channels * 4u
                || (uint64_t)tl->fs * channels * 4u > UINT32_MAX
                || tl_le(h + 8, 4) != tl->fs * channels * 4u)
                return tl_err(NULL, 0, "%s: inconsistent PCM32/Q2.30 geometry", path);
            have_fmt = 1;
        } else if (!memcmp(h, "data", 4)) {
            uint32_t frames, selected;
            if (!have_fmt || have_data || bytes % (channels * 4u))
                return tl_err(NULL, 0, "%s: Q30 requires one whole-frame data chunk after fmt", path);
            if ((uint64_t)tl->n * channels > SIZE_MAX / sizeof *tl->stems)
                return tl_err(NULL, 0, "%s: Q30 input allocation dimensions exceed size_t", path);
            tl->stems = (int32_t *)calloc((size_t)tl->n * channels, sizeof *tl->stems);
            if (!tl->stems) return tl_host_error(NULL, 0, "out of memory");
            frames = bytes / (channels * 4u);
            selected = frames < tl->n ? frames : tl->n;
            for (t = 0; t < selected; ++t) {
                if ((rc = stems_read_exact(f, frame, channels * 4u, path))) return rc;
                for (i = 0; i < channels; ++i) {
                    const uint32_t u = tl_le(frame + i * 4u, 4);
                    const int64_t value = u > INT32_MAX ? (int64_t)u - INT64_C(4294967296) : (int64_t)u;
                    tl->stems[(size_t)t * channels + i] = (int32_t)value;
                }
            }
            if ((rc = stems_discard(f, (uint64_t)(frames - selected) * channels * 4u, path))) return rc;
            have_data = 1;
        } else if ((rc = stems_discard(f, bytes, path))) return rc;
        if ((bytes & 1u) && (rc = stems_read_exact(f, h, 1, path))) return rc;
        left -= padded;
    }
    if (!have_fmt || !have_data) return tl_err(NULL, 0, "%s: Q30 fmt/data missing", path);
    if (fgetc(f) != EOF) return tl_err(NULL, 0, "%s: bytes beyond Q30 RIFF object", path);
    return ferror(f) ? io_fail(path, "read") : 0;
}

/* --stems: one channel per voice at the record's rate. Legacy16 samples
 * scale by2^15; opt-in strict PCM32 carries exact signed Q2.30 words.
 * Valid data is cut/padded to n frames for field-route voice replacement. */
static int tl_load_stems(tl_t *tl, const char *path)
{
    FILE *f;
    unsigned char h[12], b[2 * TL_MAX_VOICES];
    uint32_t size, ch = 0, rate = 0, bits = 0, fmt = 0, t, i, nf;
    int data = 0;
    if (!tl->route) return tl_err(NULL, 0, "--stems needs space amb1, amb3, binaural, or stereo with panlaw");
    f = fopen(path, "rb");
    if (!f) return io_fail(path, "open");
    if (fread(h, 1, 12, f) != 12 || memcmp(h, "RIFF", 4) || memcmp(h + 8, "WAVE", 4)) {
        if (ferror(f)) return io_close_input(f, path, 0);
        return io_close_input(f, path, tl_err(NULL, 0, "%s: not a WAV file", path));
    }
    while (!data && fread(h, 1, 8, f) == 8) {
        size = tl_le(h + 4, 4);
        if (!memcmp(h, "fmt ", 4) && size >= 16 && size <= sizeof b) {
            if (fread(b, 1, size, f) != size) break;
            fmt = tl_le(b, 2); ch = tl_le(b + 2, 2); rate = tl_le(b + 4, 4); bits = tl_le(b + 14, 2);
            if (size & 1u) fgetc(f);
        } else if (!memcmp(h, "data", 4)) data = 1;
        else if (fseek(f, (long)size + (long)(size & 1u), SEEK_CUR))
            return io_close_input(f, path, io_fail(path, "seek"));
    }
    if (ferror(f)) return io_close_input(f, path, 0);
    if (data && bits == 32u) return io_close_input(f, path, tl_load_stems_q30(tl, f, path));
    if (!data || fmt != 1 || bits != 16 || ch != tl->nvoices || rate != tl->fs) {
        return io_close_input(f, path, tl_err(NULL, 0, "%s: needs 16-bit PCM data of %u channels (one per voice) at %u Hz", path, tl->nvoices, tl->fs));
    }
    nf = size / (2u * ch);
    tl->stems = (int32_t *)calloc((size_t)tl->n * ch, sizeof *tl->stems);
    if (!tl->stems) return io_close_input(f, path, tl_host_error(NULL, 0, "out of memory"));
    for (t = 0; t < nf && t < tl->n && fread(b, 2, ch, f) == ch; t++)
        for (i = 0; i < ch; i++) {
            int32_t s = (int32_t)tl_le(b + 2 * i, 2);
            tl->stems[(size_t)t * ch + i] = (s > 32767 ? s - 65536 : s) * 32768;
        }
    return io_close_input(f, path, 0);
}

/* One header statement (pass 1). */
static int tl_header(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    const char *k = tok[0];
    uint64_t v, f[16];
    int i, j;
    if (!strcmp(k, "timeline")) {
        if (nt != 2 || (strcmp(tok[1], "draft1") && strcmp(tok[1], "draft2")))
            return tl_err(path, ln, "expected 'timeline draft1' or 'timeline draft2'");
        if (tl->have_timeline++) return tl_err(path, ln, "second timeline statement");
        tl->draft = tok[1][5] - '0';
    } else if (!strcmp(k, "space")) {
        if (nt != 2 || (strcmp(tok[1], "stereo") && strcmp(tok[1], "off") && !tl_field_route(tok[1])))
            return tl_err(path, ln, "space takes stereo, off, amb1, amb3 or binaural");
        if (tl->have_space++) return tl_err(path, ln, "second space statement");
        tl->space = !strcmp(tok[1], "stereo");
        tl->route = tl_field_route(tok[1]);
    } else if (!strcmp(k, "block")) {
        if (nt != 2) return tl_err(path, ln, "block takes 32, 64 or 128");
        if (TL_AT_U(header_block, 1, tok[1], &v)) return 1;
        if (v != 32 && v != 64 && v != 128) return tl_err(path, ln, "block takes 32, 64 or 128");
        if (tl->block) return tl_err(path, ln, "second block statement");
        tl->block = (uint32_t)v;
    } else if (!strcmp(k, "near")) {
        if (nt != 2) return tl_err(path, ln, "near takes a radius in mm, 1 to %d", SP_POS_MAX);
        if (TL_AT_U(header_near, 1, tok[1], &v)) return 1;
        if (tl->near) return tl_err(path, ln, "second near statement");
        tl->near = (uint32_t)v;
    } else if (!strcmp(k, "engine")) {
        char want[160], got[TL_LINE] = "";
        for (i = 1; i < nt; i++) {
            if (i > 1) strcat(got, " ");
            strcat(got, tok[i]);
        }
        tl_engine(want, sizeof want);
        if (strcmp(got, want)) return tl_err(path, ln, "engine '%s' is not this build's '%s'", got, want);
        if (tl->have_engine++) return tl_err(path, ln, "second engine statement");
    } else if (!strcmp(k, "fs")) {
        if (nt != 2) return tl_err(path, ln, "fs takes a rate in Hz, at least 1");
        if (TL_AT_U(header_fs, 1, tok[1], &v)) return 1;
        if (tl->fs) return tl_err(path, ln, "second fs statement");
        tl->fs = (uint32_t)v;
    } else if (!strcmp(k, "length_ns")) {
        if (nt != 2) return tl_err(path, ln, "length_ns takes a length in ns, at least 1");
        if (TL_AT_U(header_length_ns, 1, tok[1], &v)) return 1;
        if (tl->length_ns) return tl_err(path, ln, "second length_ns statement");
        tl->length_ns = v;
    } else if (!strcmp(k, "streams")) {
        if (nt < 2 || nt - 1 > TL_MAX_STREAMS) return tl_err(path, ln, "streams takes 1 to %d names", TL_MAX_STREAMS);
        if (tl->nstreams) return tl_err(path, ln, "second streams statement");
        for (i = 1; i < nt; i++) {
            if (!tl_name_ok(tok[i])) return tl_err(path, ln, "bad stream name '%s'", tok[i]);
            for (j = 1; j < i; j++)
                if (!strcmp(tok[i], tok[j])) return tl_err(path, ln, "stream '%s' listed twice", tok[i]);
            strcpy(tl->stream[i - 1], tok[i]);
        }
        tl->nstreams = (uint32_t)(nt - 1);
    } else if (!strcmp(k, "patch")) {
        fm_op_patch_t *o;
        int p, nv = nt - 3;
        if (nv != 9 && (nv < 11 || nv > 16)) return tl_err(path, ln, "patch takes a name, an operator and 9 or 11 to 16 values");
        if (!tl_name_ok(tok[1])) return tl_err(path, ln, "bad patch name '%s'", tok[1]);
        if (TL_AT_U(patch, 2, tok[2], &v)) return 1;
        for (i = 0; i < nv; i++)   /* WAVE 0..11, RS 7 and FREE 2 at most  */
            if (TL_AT_U(patch, 3u + (unsigned)i, tok[3 + i], &f[i])) return 1;
        p = tl_find_patch(tl, tok[1]);
        if (p < 0) {
            if (tl->npatches == TL_MAX_PATCHES) return tl_err(path, ln, "more than %d patches", TL_MAX_PATCHES);
            p = (int)tl->npatches++;
            strcpy(tl->patch[p].name, tok[1]);
        }
        if (tl->patch[p].ops & (1u << v)) return tl_err(path, ln, "patch %s operator %d defined twice", tok[1], (int)v);
        tl->patch[p].ops |= 1u << v;
        o = &tl->patch[p].p.op[v];
        o->ratio_q16    = (uint32_t)f[0];
        o->level_db10   = (uint16_t)f[1];
        o->index_q10    = (uint16_t)f[2];
        o->feedback_q10 = (uint16_t)f[3];
        o->vel_sens_q8  = (uint16_t)f[4];
        o->attack_ms    = (uint16_t)f[5];
        o->decay_ms     = (uint16_t)f[6];
        o->sustain_db10 = (uint16_t)f[7];
        o->release_ms   = (uint16_t)f[8];
        o->ks_break_millihz = nv >= 11 ? (uint32_t)f[9] : 0u;
        o->ks_slope_db10    = nv >= 11 ? (uint16_t)f[10] : 0u;
        o->wave             = nv >= 12 ? (uint16_t)f[11] : 0u;   /*  */
        o->rate_scale       = nv >= 13 ? (uint16_t)f[12] : 0u;   /*  */
        o->fixed_millihz    = nv >= 14 ? (uint32_t)f[13] : 0u;   /*  */
        o->phase_u16        = nv >= 15 ? (uint16_t)f[14] : 0u;
        o->free_run         = nv >= 16 ? (uint16_t)f[15] : 0u;
    } else if (!strcmp(k, "voice")) {
        if (nt != 3) return tl_err(path, ln, "voice takes an index below %d and a patch name", TL_MAX_VOICES);
        if (TL_AT_U(header_voice, 1, tok[1], &v)) return 1;
        if (!tl_name_ok(tok[2])) return tl_err(path, ln, "voice takes an index below %d and a patch name", TL_MAX_VOICES);
        if (tl->vpatch[v][0]) return tl_err(path, ln, "voice %d defined twice", (int)v);
        strcpy(tl->vpatch[v], tok[2]);
        if (v + 1 > tl->nvoices) tl->nvoices = (uint32_t)v + 1;
    } else if (!strcmp(k, "hash")) {
        if (nt != 2 || tl_hex(tok[1], &v)) return tl_err(path, ln, "hash takes 16 lowercase hex digits");
        if (tl->have_hash++) return tl_err(path, ln, "second hash statement");
        tl->hash = v;
    } else if ((i = tl_header_gen(tl, path, ln, tok, nt)) >= 0) {
        return i;
    } else if ((i = tl_header_field(tl, path, ln, tok, nt)) >= 0) {
        return i;
    } else {
        if ((i = tl_header_fx(tl, path, ln, tok, nt)) >= 0) return i;
        if ((i = tl_header_engine(tl, path, ln, tok, nt)) >= 0) return i;   /*  */
        return tl_err(path, ln, "unknown statement '%s'", k);
    }
    return 0;
}

/* After pass 1: every statement present, the length in samples, voices
 * 0..N-1 each on a patch with both operators, or with operators 0 to
 * OPS - 1 under its route . */
static int tl_check_header(tl_t *tl)
{
    uint64_t n;
    uint32_t i;
    int p;
    if (!tl->have_timeline) return tl_err(NULL, 0, "no timeline statement");
    if (!tl->have_engine)   return tl_err(NULL, 0, "no engine statement");
    if (!tl->fs)            return tl_err(NULL, 0, "no fs statement");
    if (!tl->length_ns)     return tl_err(NULL, 0, "no length_ns statement");
    if (!tl->nstreams)      return tl_err(NULL, 0, "no streams statement");
    if (!tl->nvoices)       return tl_err(NULL, 0, "no voice statement");
    if (tl->draft == 1 && (tl->have_space || tl->block || tl->near)) return tl_err(NULL, 0, "space, block and near need timeline draft2");
    if (!tl->block) tl->block = 64;
    if (!tl->near)  tl->near = 100;
    tl->fx.channels = tl->space ? 2u : 1u;           /* the effects (fx/SPEC.md, section 7) */
    tl->fx.block    = tl->block;
    if (tl->fx.on && (tl->fs < 8000 || tl->fs > 192000)) return tl_err(NULL, 0, "the effects need fs 8000 to 192000 Hz");
    if (tl->length_ns > UINT64_MAX / tl->fs) return tl_err(NULL, 0, "length_ns too large");
    n = tl->length_ns * tl->fs / 1000000000u;
    if (n == 0 || n > UINT32_MAX) return tl_err(NULL, 0, "length of %llu samples out of range", (unsigned long long)n);
    if (tl->space && n > INT32_MAX) return tl_err(NULL, 0, "a stereo render of %llu frames is past 2^31 - 1", (unsigned long long)n);
    tl->n = (uint32_t)n;
    if (tl_check_field(tl)) return 1;
    if (tl->route) tl->fx.channels = tl_field_ch(tl);
    if (tl_check_wt(tl)) return 1;
    for (i = 0; i < tl->npatches; i++) {
        const int routed = (tl->routes >> i) & 1u;
        const unsigned nop = routed ? tl->patch[i].p.route.ops : 2u, all = (1u << nop) - 1u, got = tl->patch[i].ops;
        int k;
        if (got & ~all) {
            for (k = 0; !(((got & ~all) >> k) & 1u); k++) {}
            if (routed) return tl_err(NULL, 0, "patch %s: operator %d is at or above its route's OPS %u", tl->patch[i].name, k, nop);
            return tl_err(NULL, 0, "patch %s: operator %d needs a route statement with OPS above %d", tl->patch[i].name, k, k);
        }
        for (k = 0; k < (int)nop; k++) {
            const fm_eg_extra_t *eg = &tl->patch[i].p.eg[k];
            const unsigned s1 = tl->patch[i].p.op[k].sustain_db10 > 1200u ? 1200u : tl->patch[i].p.op[k].sustain_db10;
            if ((eg->decay2_ms || eg->sustain2_db10) && eg->sustain2_db10 < s1)
                return tl_err(NULL, 0, "patch %s: eg operator %d SUSTAIN2_DB10 %u is below SUSTAIN1 %u", tl->patch[i].name,
                              k, (unsigned)eg->sustain2_db10, s1);
        }
        if (tl->patch[i].eg & ~all) {
            for (k = 0; !(((tl->patch[i].eg & ~all) >> k) & 1u); k++) {}
            return tl_err(NULL, 0, "patch %s: eg names operator %d, which the patch lacks", tl->patch[i].name, k);
        }
        if (tl->patch[i].ks & ~all) {
            for (k = 0; !(((tl->patch[i].ks & ~all) >> k) & 1u); k++) {}
            return tl_err(NULL, 0, "patch %s: ks names operator %d, which the patch lacks", tl->patch[i].name, k);
        }
        if (tl->patch[i].kstype & ~all) {
            for (k = 0; !(((tl->patch[i].kstype & ~all) >> k) & 1u); k++) {}
            return tl_err(NULL, 0, "patch %s: kstype names operator %d, which the patch lacks", tl->patch[i].name, k);
        }
        if (tl->patch[i].fbtype & ~all) {
            for (k = 0; !(((tl->patch[i].fbtype & ~all) >> k) & 1u); k++) {}
            return tl_err(NULL, 0, "patch %s: fbtype names operator %d, which the patch lacks", tl->patch[i].name, k);
        }
        if (tl->patch[i].egloop & ~all) {
            for (k = 0; !(((tl->patch[i].egloop & ~all) >> k) & 1u); k++) {}
            return tl_err(NULL, 0, "patch %s: egloop names operator %d, which the patch lacks", tl->patch[i].name, k);
        }
        if (tl->patch[i].vector && (!routed || nop != 4 || tl->patch[i].p.route.carriers != 15))
            return tl_err(NULL, 0, "patch %s: vector needs route OPS 4 and CARRIERS 15", tl->patch[i].name);
        if (tl->patch[i].pd & ~all) {
            for (k = 0; !(((tl->patch[i].pd & ~all) >> k) & 1u); k++) {}
            return tl_err(NULL, 0, "patch %s: pd names operator %d, which the patch lacks", tl->patch[i].name, k);
        }
        for (k = 0; k < (int)nop; k++)
            if (((tl->patch[i].pd >> k) & 1u) && tl->patch[i].p.op[k].wave)
                return tl_err(NULL, 0, "patch %s: pd operator %d needs sine WAVE 0", tl->patch[i].name, k);
        if (tl->patch[i].wt & ~all) {
            for (k = 0; !(((tl->patch[i].wt & ~all) >> k) & 1u); k++) {}
            return tl_err(NULL, 0, "patch %s: wtuse names operator %d, which the patch lacks", tl->patch[i].name, k);
        }
        for (k = 0; k < (int)nop; k++)
            if ((tl->patch[i].wt >> k) & 1u) {     /* : a wavetable plays on a sine operator without PD */
                const int b = tl_find_bank(tl, tl->patch[i].wt_bank[k]);
                if (tl->patch[i].p.op[k].wave) return tl_err(NULL, 0, "patch %s: wtuse operator %d needs sine WAVE 0", tl->patch[i].name, k);
                if ((tl->patch[i].pd >> k) & 1u) return tl_err(NULL, 0, "patch %s: operator %d has both pd and wtuse", tl->patch[i].name, k);
                if (b < 0) return tl_err(NULL, 0, "patch %s: wtuse operator %d names no bank %s", tl->patch[i].name, k, tl->patch[i].wt_bank[k]);
                tl->patch[i].p.wt[k].bank = (uint8_t)b;
            }
        if (tl->patch[i].kssrc & ~all) {
            for (k = 0; !(((tl->patch[i].kssrc & ~all) >> k) & 1u); k++) {}
            return tl_err(NULL, 0, "patch %s: ksource names operator %d, which the patch lacks", tl->patch[i].name, k);
        }
        for (k = 0; k < (int)nop; k++)
            if ((tl->patch[i].kssrc >> k) & 1u) {  /* : a sine operator without PD or a wavetable, and nothing into it */
                const fm_patch_t *pp = &tl->patch[i].p;
                int j;
                if (pp->op[k].wave) return tl_err(NULL, 0, "patch %s: ksource operator %d needs sine WAVE 0", tl->patch[i].name, k);
                if ((tl->patch[i].pd >> k) & 1u) return tl_err(NULL, 0, "patch %s: operator %d has both pd and ksource", tl->patch[i].name, k);
                if ((tl->patch[i].wt >> k) & 1u) return tl_err(NULL, 0, "patch %s: operator %d has both wtuse and ksource", tl->patch[i].name, k);
                if (pp->op[k].feedback_q10) return tl_err(NULL, 0, "patch %s: ksource operator %d takes no FEEDBACK", tl->patch[i].name, k);
                if (!routed && k == 0 && pp->op[1].index_q10)
                    return tl_err(NULL, 0, "patch %s: ksource operator 0 takes no modulation, so operator 1's INDEX must be 0", tl->patch[i].name);
                for (j = 0; routed && j < (int)nop; j++) {
                    if (j != k && pp->route.w[j][k])
                        return tl_err(NULL, 0, "patch %s: ksource operator %d takes no route edge, and %d>%d is one", tl->patch[i].name, k, j, k);
                    if (j != k && pp->route.w[k][j] && pp->route.kind[k][j] == 3u)
                        return tl_err(NULL, 0, "patch %s: ksource operator %d drives no sync, and %d>%d is one", tl->patch[i].name, k, k, j);
                }
            }
        if (tl->patch[i].noise & ~all) {
            for (k = 0; !(((tl->patch[i].noise & ~all) >> k) & 1u); k++) {}
            return tl_err(NULL, 0, "patch %s: noise names operator %d, which the patch lacks", tl->patch[i].name, k);
        }
        for (k = 0; k < (int)nop; k++)
            if (((tl->patch[i].noise >> k) & 1u) && tl->patch[i].p.op[k].wave != 10 && tl->patch[i].p.op[k].wave != 11)
                return tl_err(NULL, 0, "patch %s: noise operator %d needs WAVE 10 or 11", tl->patch[i].name, k);
        if (got != all) {
            for (k = 0; (got >> k) & 1u; k++) {}
            return tl_err(NULL, 0, "patch %s lacks operator %d", tl->patch[i].name, k);
        }
        if ((tl->lfos >> i) & 1u) {   /* : absent masks are every operator; a given one names only these */
            fm_lfo_patch_t *l = &tl->patch[i].p.lfo;
            const unsigned m[3] = { l->mask_pitch, l->mask_level, l->mask_index };
            static const char *const mn[3] = { "MASK_PITCH", "MASK_LEVEL", "MASK_INDEX" };
            int j;
            if ((uint64_t)l->rate_millihz * FM_CTL >= (uint64_t)500 * tl->fs)
                return tl_err(NULL, 0, "lfo %s: RATE_MILLIHZ %u reaches the control Nyquist at fs %u with FM_CTL=%d",
                              tl->patch[i].name, (unsigned)l->rate_millihz, (unsigned)tl->fs, FM_CTL);
            if (l->rate_millihz > TL_PARAMS[TP_lfo_rate_millihz].high) {
                char *rate_tok[2];
                rate_tok[0] = (char *)TL_STATEMENTS[TS_lfo].name;
                rate_tok[1] = tl->patch[i].name;
                return tl_param_error(tl->patch[i].lfo_path, tl->patch[i].lfo_line,
                                      rate_tok, &TL_PARAMS[TP_lfo_rate_millihz], tl->patch[i].lfo_rate_token);
            }
            if (!((tl->lfo_masks >> i) & 1u)) l->mask_pitch = l->mask_level = l->mask_index = (uint8_t)all;
            else
                for (j = 0; j < 3; j++)
                    if (m[j] & ~all) {
                        for (k = 0; !(((m[j] & ~all) >> k) & 1u); k++) {}
                        return tl_err(NULL, 0, "patch %s: the lfo's %s names operator %d, which the patch lacks",
                                      tl->patch[i].name, mn[j], k);
                    }
        }
        if (((tl->fenvs >> i) & 1u) && !((tl->filters >> i) & 1u))
            return tl_err(NULL, 0, "patch %s: fenv needs a filter statement", tl->patch[i].name);
        if ((tl->pegs >> i) & 1u) {
            fm_peg_patch_t *peg = &tl->patch[i].p.peg;
            if (!peg->mask) peg->mask = (uint16_t)all;
            if (peg->mask & ~all) {
                for (k = 0; !(((peg->mask & ~all) >> k) & 1u); k++) {}
                return tl_err(NULL, 0, "patch %s: peg MASK %u names operator %d, at or above its %u operators",
                              tl->patch[i].name, (unsigned)peg->mask, k, nop);
            }
        }
        if (tl_check_mod(&tl->patch[i], tl->fs)) return 1;
    }
    for (i = 0; i < tl->nvoices; i++) {
        if (!tl->vpatch[i][0]) return tl_err(NULL, 0, "voice %u missing (voices are 0 to %u)", i, tl->nvoices - 1);
        if ((p = tl_find_patch(tl, tl->vpatch[i])) < 0) return tl_err(NULL, 0, "voice %u: no patch %s", i, tl->vpatch[i]);
        tl->voice[i] = (uint32_t)p;
    }
    { const int rc = tl_check_gen(tl); if (rc) return rc; }
    return tl_check_ks(tl);
}

/* : zeroed buffers for each voice's Karplus-Strong operators, in two
 * sets; each voice's binding is tried once here, so a render's cannot
 * fail. The caller frees ks_pool. */
static int tl_check_ks(tl_t *tl)
{
    size_t total = 0, at = 0;
    uint32_t i, s;
    int k;
    for (i = 0; i < tl->nvoices; i++)
        for (k = 0; k < FM_OPS; k++)
            if ((tl->patch[tl->voice[i]].kssrc >> k) & 1u) total += tl->patch[tl->voice[i]].p.ks[k].buffer;
    if (!total) return 0;
    tl->ks_pool = (int32_t *)alloc_n(2u * total, sizeof *tl->ks_pool);
    if (!tl->ks_pool) return tl_host_error(NULL, 0, "out of memory");
    memset(tl->ks_pool, 0, 2u * total * sizeof *tl->ks_pool);
    for (s = 0; s < 2u; s++)
        for (i = 0; i < tl->nvoices; i++) {
            const tl_patch_t *pt = &tl->patch[tl->voice[i]];
            fm_voice_t v;
            int r;
            for (k = 0; k < FM_OPS; k++) {
                const uint32_t b = ((pt->kssrc >> k) & 1u) ? pt->p.ks[k].buffer : 0u;
                tl->ks_view[s][i][k].data = b ? tl->ks_pool + at : NULL;
                tl->ks_view[s][i][k].capacity = b;
                at += b;
            }
            tl->ks_assets[s][i] = (fm_assets_t){ (uint32_t)sizeof(fm_assets_t), tl->wt.nbanks, tl->wt.nbanks ? tl->wt.view : NULL,
                                                 FM_OPS, tl->ks_view[s][i] };
            fm_voice_init(&v, &pt->p, tl->fs);
            if ((r = fm_voice_bind_assets(&v, &tl->ks_assets[s][i])) != FM_BIND_OK)
                return tl_err(NULL, 0, "voice %u: the Karplus-Strong binding is refused (%d)", (unsigned)i, r);
        }
    return 0;
}

/* Derive refusal text from the bus table so new buses need no string edits. */
static int tl_send_err(const char *path, unsigned ln, const char *bus)
{
    size_t size = 1;
    uint32_t b;
    for (b = 0; b < FX_BUSES; ++b) size += strlen(FX_BUS_NAME[b]) + 4;
    {
        char names[size];
        names[0] = '\0';
        for (b = 0; b < FX_BUSES; ++b) {
            if (b) strcat(names, b + 1 == FX_BUSES ? " or " : ", ");
            strcat(names, FX_BUS_NAME[b]);
        }
        if (bus) return tl_err(path, ln, "send: bus '%s' is not %s", bus, names);
        return tl_err(path, ln, "send takes a bus (%s) and LEVEL_Q15, 0 to 32768", names);
    }
}

/* A send event (fx/SPEC.md, section 7), T_NS STREAM VOICE send BUS
 * LEVEL_Q15, to a bus whose effect the header turns on. */
static int tl_event_fx(const tl_t *tl, const char *path, unsigned ln, char **tok, int nt, tl_event_t *e)
{
    uint64_t v;
    uint32_t b;
    if (nt != 6) return tl_send_err(path, ln, NULL);
    for (b = 0; b < FX_BUSES && strcmp(tok[4], FX_BUS_NAME[b]); b++) {}
    if (b == FX_BUSES) return tl_send_err(path, ln, tok[4]);
    if (!(tl->fx.on & FX_BUS_BIT[b])) return tl_err(path, ln, "send to %s, which no %s statement turns on",
                                                  tok[4], b == FX_DUCK ? "compressor" : tok[4]);
    if (TL_AT_U(event_send, 5, tok[5], &v)) return 1;
    e->type      = EV_SEND;
    e->bus       = b;
    e->level_q15 = (uint32_t)v;
    return 0;
}

/* The engine's events  after T_NS STREAM VOICE, into e: filter
 * CUTOFF_MILLIHZ Q_MILLI, on a voice whose patch has a filter statement. */
static int tl_event_engine(const tl_t *tl, const char *path, unsigned ln, char **tok, int nt, tl_event_t *e)
{
    uint64_t v;
    if (!strcmp(tok[3], "ctl")) {
        int64_t value;
        if (FM_CTL != 64) return tl_err(path, ln, "ctl event needs FM_CTL=64; this build has FM_CTL=%d", FM_CTL);
        if (nt != 6) return tl_err(path, ln, "ctl takes N 0 to 7 and VALUE_Q15 -32768 to 32768");
        if (TL_AT_U(event_ctl, 4, tok[4], &v) || TL_AT_S(event_ctl, 5, tok[5], &value)) return 1;
        e->type = EV_CTL; e->velocity_q15 = (uint16_t)v; e->ctl_value = (int32_t)value;
        return 0;
    }
    if (nt != 6) return tl_err(path, ln, "expected 'filter CUTOFF_MILLIHZ Q_MILLI'");
    if (!(tl->filters & (1u << tl->voice[e->voice])))
        return tl_err(path, ln, "voice %u: patch %s has no filter statement", (unsigned)e->voice, tl->patch[tl->voice[e->voice]].name);
    if (TL_AT_U(event_filter, 4, tok[4], &v)) return 1;
    e->freq_millihz = (uint32_t)v;
    if (TL_AT_U(event_filter, 5, tok[5], &v)) return 1;
    e->velocity_q15 = (uint16_t)v;
    e->type = EV_FILTER;
    return 0;
}

static unsigned tl_event_statement(char **tok)
{
    unsigned i;
    if (!strcmp(tok[2], "gen")) return TS_event_gen;
    if (!strcmp(tok[2], "lane")) return TS_event_lane;
    for (i = 0; i < TS_COUNT; ++i)
        if (TL_STATEMENTS[i].word_position == 3 && !strcmp(TL_STATEMENTS[i].name, tok[3])) return i;
    return TS_event_on;
}

/* One event statement (pass 2). */
static int tl_event(tl_t *tl, const char *path, unsigned ln, char **tok, int nt)
{
    tl_event_t e;
    uint64_t v, smp;
    uint32_t s;
    unsigned statement;
    int grc;
    memset(&e, 0, sizeof e);
    if (nt < 4) return tl_err(path, ln, "an event is T_NS STREAM VOICE followed by on FREQ_MILLIHZ VELOCITY_Q15, pitch FREQ_MILLIHZ, off, pos X Y Z, move X Y Z DUR_NS, send BUS LEVEL_Q15, crush BITS RATE_DIV, filter CUTOFF_MILLIHZ Q_MILLI, ctl N VALUE_Q15 or yaw MDEG; generator forms are T_NS STREAM gen ID PARAM VALUE and T_NS STREAM lane ID enable VALUE (no VOICE field)");
    statement = tl_event_statement(tok);
    if (tl_param_u(path, ln, tok, tl_param_at(statement, 0, "all"), tok[0], &e.t_ns)) return 1;
    if (e.t_ns > UINT64_MAX / tl->fs) return tl_err(path, ln, "bad time '%s'", tok[0]);
    smp = e.t_ns * tl->fs / 1000000000u;
    if (smp >= tl->n) return tl_err(path, ln, "event at sample %llu, at or past the end (%u samples)", (unsigned long long)smp, tl->n);
    e.sample = (uint32_t)smp;
    for (s = 0; s < tl->nstreams && strcmp(tok[1], tl->stream[s]); s++) {}
    if (s == tl->nstreams) return tl_err(path, ln, "stream '%s' is not in the streams statement", tok[1]);
    e.stream = s;
    grc = tl_event_gen(tl, path, ln, tok, nt, &e);
    if (grc > 0) return grc;
    if (grc == 0) goto append_event;
    if (tl_param_u(path, ln, tok, tl_param_at(statement, 2, "all"), tok[2], &v)) return 1;
    if (v >= tl->nvoices) return tl_err(path, ln, "voice '%s' is not 0 to %u", tok[2], tl->nvoices - 1);
    e.voice = (uint32_t)v;
    if (!strcmp(tok[3], "on") && nt == 6) {
        e.type = EV_ON;
        if (TL_AT_U(event_on, 4, tok[4], &v)) return 1;
        e.freq_millihz = (uint32_t)v;
        if (TL_AT_U(event_on, 5, tok[5], &v)) return 1;
        e.velocity_q15 = (uint16_t)v;
    } else if (!strcmp(tok[3], "pitch") && nt == 5) {
        e.type = EV_PITCH;
        if (TL_AT_U(event_pitch, 4, tok[4], &v)) return 1;
        e.freq_millihz = (uint32_t)v;
    } else if (!strcmp(tok[3], "off") && nt == 4) {
        e.type = EV_OFF;
    } else if ((!strcmp(tok[3], "pos") && nt == 7) || (!strcmp(tok[3], "move") && nt == 8)) {
        int64_t c[3];
        int i, move = tok[3][0] == 'm';
        if (tl->draft != 2) return tl_err(path, ln, "%s needs timeline draft2", tok[3]);
        for (i = 0; i < 3; i++)
            if (tl_param_s(path, ln, tok, tl_param_at(statement, 4u + (unsigned)i, "all"), tok[4 + i], &c[i])) return 1;
        e.type = move ? EV_MOVE : EV_POS;
        e.x = (int32_t)c[0];
        e.y = (int32_t)c[1];
        e.z = (int32_t)c[2];
        if (move) {                                   /* the end may lie past the record's end: the move is cut there */
            uint64_t end;
            if (TL_AT_U(event_move, 7, tok[7], &v)) return 1;
            if (v > UINT64_MAX / tl->fs - e.t_ns)
                return tl_err(path, ln, "bad duration '%s': 1 ns or more, ending at a sample below 2^32", tok[7]);
            end = (e.t_ns + v) * tl->fs / 1000000000u;
            if (end > UINT32_MAX) return tl_err(path, ln, "the move ends at sample %llu, past 2^32 - 1", (unsigned long long)end);
            e.dur_ns = v;
            e.end = (uint32_t)end;
        }
    } else if (!strcmp(tok[3], "send")) {
        if (tl_event_fx(tl, path, ln, tok, nt, &e)) return 1;
    } else if (!strcmp(tok[3], "crush")) {
        if (!(tl->fx.on & FX_ON_CRUSH)) return tl_err(path, ln, "crush event needs a crush statement");
        if (nt != 6) return tl_err(path, ln, "crush takes BITS 1 to 24 and RATE_DIV 1 to 64");
        e.type = EV_CRUSH;
        if (TL_AT_U(event_crush, 4, tok[4], &v)) return 1;
        e.crush_bits = (uint32_t)v;
        if (TL_AT_U(event_crush, 5, tok[5], &v)) return 1;
        e.crush_div = (uint32_t)v;
    } else if (!strcmp(tok[3], "filter") || !strcmp(tok[3], "ctl")) {           /*  */
        if (tl_event_engine(tl, path, ln, tok, nt, &e)) return 1;
    } else if (!strcmp(tok[3], "yaw")) {
        if (tl_event_field(tl, path, ln, tok, nt, &e)) return 1;
    } else {
        return tl_err(path, ln, "expected 'on FREQ_MILLIHZ VELOCITY_Q15', 'pitch FREQ_MILLIHZ', 'off', 'pos X Y Z', 'move X Y Z DUR_NS', 'send BUS LEVEL_Q15', 'crush BITS RATE_DIV', 'filter CUTOFF_MILLIHZ Q_MILLI', 'ctl N VALUE_Q15' or 'yaw MDEG'; generator forms are T_NS STREAM gen ID PARAM VALUE and T_NS STREAM lane ID enable VALUE (no VOICE field)");
    }
    if (e.type == EV_ON || e.type == EV_OFF || e.type == EV_PITCH) {
        for (s = 0; s < tl->gen.lanes; ++s)
            if (tl->gen.lane[s].voice == e.voice) return tl_err(path, ln, "manual note event on generator-reserved voice %u", e.voice);
    }
append_event:
    if (tl->nev == tl->cap) {
        uint32_t cap = tl->cap ? 2u * tl->cap : 256u;   /* 0 once 2^31 doubles */
        tl_event_t *ev;
        if (cap <= tl->cap) return tl_err(path, ln, "more than %u events", tl->cap);
        ev = (tl_event_t *)realloc_n(tl->ev, cap, sizeof *ev);
        if (!ev) return tl_host_error(path, ln, "out of memory");
        tl->ev = ev;
        tl->cap = cap;
    }
    e.order = tl->nev;
    tl->ev[tl->nev++] = e;
    return 0;
}

/* Draft grammar of the  timeline; the tools SPEC fixes the final one
 * . One statement per line, tokens separated by blanks, '#' starts a
 * comment. Header statements, each once in the pool of files (a patch's
 * operator and a voice each once):
 *   timeline draft1
 *   engine a1 sine_bits=B exp_bits=B phase64=0|1 env64=0|1 fb_avg=0|1 tables=HASH
 *   fs HZ
 *   length_ns NS            renders floor(NS * fs / 10^9) samples
 *   streams NAME...         the tie order, first first
 *   patch NAME OP RATIO_Q16 LEVEL_DB10 INDEX_Q10 FEEDBACK_Q10 VEL_SENS_Q8
 *         ATTACK_MS DECAY_MS SUSTAIN_DB10 RELEASE_MS [KS_BREAK_MILLIHZ KS_SLOPE_DB10]
 *                            units; OP 0 carrier, 1 modulator (fm_op_patch_t),
 *                           both present; OP 2 and up only under a route (below);
 *                           key scaling  left out is 0 0, and the
 *                           canonical record writes it only when it is not
 *   voice INDEX PATCH       voices 0 to N-1, all present
 *   decodable               opt-in constrained message-decoder profile
 *   hash HASH               optional: the expected FNV-1a 64 of the output
 * Events, in the API's units, lines starting with a digit:
 *   T_NS STREAM VOICE on FREQ_MILLIHZ VELOCITY_Q15
 *   T_NS STREAM VOICE pitch FREQ_MILLIHZ    fm_voice_pitch
 *   T_NS STREAM VOICE off
 * An event applies before sample floor(T_NS * fs / 10^9), which must lie
 * before the end. Events apply in order of T_NS, then stream, then input
 * order. The voices mix at unit gains (fm_mix_i16). The engine statement
 * must match the build, or the render would not be the record's.
 *  session generators, opt-in and valid in either draft:
 *   gen_clock CTL                     explicit matching FM_CTL
 *   gen ID KIND SEED DIV SLEW FIELDS   full fields from gen/params.def
 *   gen_axiom ID DEPTH WORD           one bounded L-system definition
 *   gen_rule ID SYMBOL WORD_OR_DASH   '-' deletes; absent rule copies
 *   gen_lane ID VOICE GATE PITCH ACCENT STEP BASE SPAN VELOCITY GATE_TICKS SCALE
 *   T_NS STREAM gen ID PARAM VALUE    no voice field; bounded pending set/reset
 *   T_NS STREAM lane ID enable VALUE  0 or 1, applied on the next bank tick
 *   mod PATCH SLOT gen.ID.x|y TARGET AMOUNT [SHIFT]
 * A slot maps its binding to raw source64+SLOT; only Henon has Y. Every
 * reference resolves after all headers. Lanes reserve distinct existing
 * voices and refuse their explicit on/off/pitch. Bank frame0 runs after
 * explicit events, publishes cached sources, then emits all offs before ons.
 * Existing voice clocks receive no extra tick. Canonical source IDs ascend,
 * each followed by its axiom/rules; lanes follow voices. Runtime event ties
 * retain time, stream and insertion order. --gen-events uses this same bank
 * and emits version1 rows with checked exact nanosecond event timestamps.
 * Draft2 (space/SPEC.md, section 5) adds, each once:
 *   timeline draft2
 *   space stereo|off        the spatial module's stereo layout, or the mono path; absent: off
 *   block K                 32, 64 or 128; absent: 64
 *   near MM                 the near-field radius, 1 to 2^24; absent: 100
 *   T_NS STREAM VOICE pos X Y Z    the voice's position in mm, signed, at most 2^24,
 *                           applied at the first block boundary at or after its sample
 *   T_NS STREAM VOICE move X Y Z DUR_NS    the voice moves in a straight line from where it is at
 *                           that boundary to X Y Z, reached at the first boundary at or after the
 *                           sample of T_NS + DUR_NS ; DUR_NS 1 or more, that sample below 2^32
 * In a draft1 pool those are errors. The hash of a stereo render covers
 * the interleaved int16 stream, 2 * n samples.
 * The effects (fx/SPEC.md, section 7; reader tl_header_fx), in draft1 and
 * draft2, each once; an absent part is off, and none keeps the plain mix:
 *   dcblock FC_MILLIHZ      the DC blocker's corner, 1 to 100000
 *   chorus BASE_US DEPTH_US RATE_MILLIHZ    7000 to 25000, 1000 to 5000, 100 to 3000
 *   flanger BASE_US DEPTH_US RATE_MILLIHZ FEEDBACK_Q15 INVERT
 *                           100..10000, 100..BASE_US, 0..2000, 0..29491, 0/1
 *   delay TIME_US FEEDBACK_Q15 LOWPASS_HZ   50000 to 1000000, 0 to 29491, 20 to 20000
 *   pingpong TIME_US FEEDBACK_Q15 LOWPASS_HZ HIGHPASS_HZ
 *                           50000 to 1000000, 0 to 29491, 20 to 20000, 0 to 2000 (0 none);
 *                           one of delay and pingpong; needs stereo (two channels), no field route
 *   reverb ROOM_PERMILLE DAMP_PERMILLE      0 to 1000 each: Freeverb's room size and damping
 *   compressor THRESHOLD_DB10 RATIO_TENTHS ATTACK_MS RELEASE_MS MAKEUP_DB10
 *                           0 to 600, 10 to 200, 0 to 100, 1 to 5000, 0 to 240;
 *                           one linked master detector, or the configured duck sum
 *   limiter THRESHOLD_DB10 RELEASE_MS       0 to 200 below full scale, 1 to 5000
 *   softclip                the cubic soft clip in place of the plain rounding
 *   gain GAIN_DB10           signed master gain, -600 to 240 tenths of a dB
 *   dither SEED              TPDF dither, seed 1 to 4294967295
 *   crush                   per-voice bit/rate reduction, initially exact bypass
 *   T_NS STREAM VOICE crush BITS RATE_DIV   1..24, 1..64; requires crush header; next FX block boundary
 *   T_NS STREAM VOICE send chorus|delay|reverb|duck|flanger LEVEL_Q15    the voice's send, 0 to 32768,
 *                           taken before the pan, from the first block boundary at or after
 *                           its sample; the bus's effect must be on (duck needs compressor).
 *                           Every send starts at 0; duck has no audible return.
 * They need fs 8000 to 192000; a draft1 pool's block is 64.
 * The engine's statements , in draft1 and draft2:
 *   patch ... KS_BREAK_MILLIHZ KS_SLOPE_DB10 WAVE    a 12th value, the operator's
 *                           waveform: 0 sine, 1 half, 2 absolute, 3 quarter, 4 noise,
 *                           5 alternating sine, 6 camel, 7 square, 8 log-saw,
 *                           9 pink, 10 low-pass noise, 11 high-pass noise;
 *                           left out 0, written only when not 0, key scaling then as 0 0
 *   patch ... WAVE RS FIXED_MILLIHZ PHASE_U16 FREE    the 13th to 16th values (,
 *                           ), each left out 0; the record writes the values up
 *                           to the last nonzero one, those before it as 0 (key scaling
 *                           always as a pair). RS, 0 to 7: at a note-on each segment's
 *                           samples n become max(1, round(n * 2^(-oct * RS / 8))), oct =
 *                           log2(note / 27500 mHz) clamped to 0..8, a pitch event not
 *                           rescaling; FIXED_MILLIHZ, nonzero: the operator's frequency
 *                           before RATIO_Q16 whatever the note, pitch events leaving
 *                           it, rate and key scaling following the note; PHASE_U16, 0 to
 *                           65535: the phase in 1/65536 turns at a note-on from silence;
 *                           FREE, 0, 1 or 2: 1 keeps phase/noise, frozen while idle;
 *                           2 advances through rendered idle samples in closed form
 *   route PATCH OPS CARRIERS FROM>TO:WEIGHT[:pm|am|ring|sync]...    once per patch (, fm_route_t):
 *                           the patch has operators 0 to OPS - 1, each present once,
 *                           OPS 2 to FM_OPS (4); CARRIERS a bit mask, 1 to 2^OPS - 1,
 *                           bit i summing operator i into the output; zero or more
 *                           edges, each pair once, FROM != TO, both below OPS: FROM
 *                           modulates TO with WEIGHT, 1 to 1024 (Q8.8, 256 unity),
 *                           times FROM's INDEX_Q10. An edge from an operator whose
 *                           INDEX_Q10 is 0 is accepted and adds nothing; the fixed
 *                           path ignores operator 0's index and nearly every
 *                           fixed-path patch carries it as 0, so a former carrier
 *                           routed as a modulator must be given an index. Each
 *                           operator's FEEDBACK_Q10 feeds it back, a carrier's
 *                           included. Without a route a patch plays the fixed path,
 *                           operators 0 and 1 (the carrier's feedback unused).
 *                           A suffix sets the kind, :pm the default: :am and :ring
 *                            multiply TO's output, WEIGHT 1 to 256; :sync
 *                           , WEIGHT exactly 256, restarts TO at its PHASE_U16
 *                           when FROM wraps, adding no PM and no gain.
 *                           Written after the patches, the edges by FROM, then TO
 *   filter PATCH lp|bp|hp|notch CUTOFF_MILLIHZ Q_MILLI [x2]    once per patch: the voice
 *                           filter of fm_filter_patch_t, written after the routes;
 *                           x2 selects two identical stages in series, Q per stage
 *   shape PATCH DRIVE_DB10 hard|cubic|fold|asym [pre]
 *                           , one per patch; drive 0..240, present zero shapes.
 *                           Written after all filters and before the LFOs. :
 *                           asym clips at +1 and -1/2; pre puts the shaper before
 *                           the formant bank and the filter (absent: after them).
 *   amp PATCH ATTACK_MS DECAY_MS SUSTAIN_DB10 RELEASE_MS
 *                           , one per patch: the voice amp envelope after the
 *                           shaper (fm_amp_patch_t, each value 0..65535, 's
 *                           units); absent, bypassed. Written after the shapes.
 *   lfo PATCH WAVE RATE_MILLIHZ DELAY_MS FADE_MS PITCH_CENTS LEVEL_DB10 INDEX_Q10
 *       [PHASE_U16 MASK_PITCH MASK_LEVEL MASK_INDEX [FREE]]    once per patch (,
 *                           fm_lfo_patch_t): the voice's LFO, moving at 's control
 *                           ticks (every FM_CTL samples; FREE 0 restarts at note-on)
 *                           the masked operators' increments, levels and indices. WAVE 0
 *                           sine, 1 triangle, 2 saw up, 3 saw down, 4 square, 5 sample and
 *                           hold, 6 smoothed random; RATE 10 to 50000 mHz and
 *                           RATE * FM_CTL < 500 * fs ; DELAY_MS and
 *                           FADE_MS 0 to 60000: no modulation through the delay, then the
 *                           depths rise linearly over the fade; the depths PITCH_CENTS and
 *                           LEVEL_DB10 0 to 1200 (cents up and down; tenths of a dB of
 *                           attenuation at the LFO's trough) and INDEX_Q10 0 to 65535 (Q6.10
 *                           radians up and down, the index clamped at 0); PHASE_U16 the
 *                           LFO's phase in 1/65536 turns at note-on (FREE 0) or init
 *                           (FREE 2); the masks, bit
 *                           i operator i, naming only the patch's operators, absent every
 *                           one, PHASE_U16 and the three masks all four or none. A fixed
 *                           operator (FIXED_MILLIHZ) takes no pitch offset. Written after
 *                           the filters, all eleven values and FREE only if nonzero.
 *                           FREE 0 (default) keysyncs; FREE 2 runs phase/grid from init,
 *                           advances idle time in closed form, and restarts delay/fade
 *                           at note-on without an extra tick. FREE 1 is refused
 *   peg PATCH L1 T1 L2 T2 L3 T3 L4 T4 [MASK]     once per patch :
 *                           levels -4800..4800 cents, times 0..60000 ms,
 *                           mask 1..(2^FM_OPS-1), omitted all present operators.
 *                           Fixed operators ignore the envelope; zero levels
 *                           disable it. Written after lfo, mask always present.
 *   fenv PATCH ATTACK_MS DECAY_MS SUSTAIN_PERMILLE RELEASE_MS DEPTH_CENTS
 *        TRACK_PERMILLE [REF_MILLIHZ]          once per patch , needs a filter:
 *                           times 0..60000 ms, sustain 0..1000, depth -9600..9600,
 *                           track 0..2000, reference 1..UINT32_MAX (default261626).
 *                           Written after peg, reference always present.
 *                           lfo, peg and fenv statements require FM_CTL=64 .
 *   mod PATCH SLOT SOURCE TARGET AMOUNT [SHIFT]   , FM_CTL=64:
 *                           SLOT0..7, SHIFT0..15/default3; canonical after fenv.
 *                           Smoothed slots start at 0 on a note-on from silence;
 *                           a full-scale constant source takes about 67 ticks at
 *                           SHIFT3. Use SHIFT0 for an immediate static source.
 *                           ctl0..7, vel, note, active lfo/peg/fenv or present egN;
 *                           tl_header_mod and TL_MOD_LIMIT bound signed AMOUNT:
 *                           pitchN +/-4800, levelN +/-1200, indexN +/-65535;
 *                           cutoff +/-9600, q +/-15500, lfo_rate +/-49990,
 *                           lfo_pitch/level +/-1200, lfo_index +/-65535;
 *                           wt_posN +/-32768, pd_kneeN +/-32767 (N0..3);
 *                           vector_x/vector_y +/-32768; formant_centerN
 *                           +/-INT32_MAX, formant_gainN +/-1200 (N0..2).
 *                           Every row needs its source/target features, even AMOUNT0.
 *   T_NS STREAM VOICE ctl N VALUE_Q15         N0..7, signed -32768..32768;
 *                           latch only, no extra tick; same-time input order retained.
 *   eg PATCH OP DELAY_MS DECAY2_MS SUSTAIN2_DB10    once per patch/operator :
 *                           DELAY_MS and DECAY2_MS 0..60000, SUSTAIN2_DB10 0..1200;
 *                           nonzero DECAY2 or SUSTAIN2 enables the second decay,
 *                           whose target must be at least the clamped SUSTAIN1.
 *                           Headers may precede patch/route; OP must be present.
 *                           Written after mod, by OP; all-zero statements omitted.
 *   ks PATCH OP LO_SLOPE_DB10    once per present operator, 0..1200 db10/octave:
 *                           attenuates index below its existing break at note-on,
 *                           including fixed operators; pitch events leave it alone.
 *                           Headers may precede patch/route; written after eg by OP,
 *                           zero omitted. The existing right slope is unchanged.
 *   kstype PATCH OP index|level|both RIGHT LEFT    OP 0 to OPS-1, each side - or +;
 *                           , tl_header_kstype; once per operator;
 *                           default index - - omitted when written.
 *   fbtype PATCH OP classic|pre|square|presquare  , tl_header_fbtype;
 *                           once per operator; classic omitted when written.
 *   egloop PATCH OP         , tl_header_egloop; presence enables the
 *                           attack/decay/decay2 loop. These three headers are
 *                           written after eg/ks, grouped by patch/operator.
 *   noise PATCH OP SHIFT    once per present wave-10/11 operator, 1..15;
 *                           default 4 is omitted. After the operator extensions.
 *   pd PATCH OP KNEE_Q15    1..32767; explicit16384 identity, sine only.
 *   vector PATCH X_Q15 Y_Q15    0..32768; exact four-carrier route only.
 *   formant PATCH SLOT CENTER_MILLIHZ Q_MILLI LEVEL_DB10
 *          SLOT0..2, CENTER1..UINT32_MAX, Q500..16000, LEVEL0..1200.
 *  assets follow formants in tl_write_f; tl_header_wt reads these:
 *   wtbank BANK FRAMES LENGTH 12    FRAMES1..8, power-of-two LENGTH32..2048;
 *                           at most eight banks; declaration precedes its data.
 *   wtdata BANK BAND FRAME START SAMPLE...    BAND0..11, FRAME below FRAMES;
 *                           1..16 signed -32768..32767 samples within LENGTH;
 *                           every sample present exactly once, no gaps/overlap.
 *   wthash BANK HEX         16 lowercase hex digits; required for wtdata.
 *   harmonic BANK FRAME NUMBER LEVEL_DB10 PHASE_U16    authoring alternative
 *                           to wtdata: FRAME below FRAMES, NUMBER1..1024,
 *                           LEVEL0..1200, PHASE0..65535; unique frame/number.
 *                           tl_check_wt expands harmonics, checks any given hash;
 *                           canonical output is wtbank/wtdata/wthash in that order.
 *                           A bank cannot mix harmonic and wtdata statements.
 *   wtuse PATCH OP BANK POSITION_Q15    OP0..OPS-1, POSITION0..32768;
 *                           bank may follow the binding in input; the header check
 *                           resolves it. Written after banks, once per operator.
 *   ksource PATCH OP LOSS_Q15 SEED BUFFER_SAMPLES    tl_header_ksource :
 *                           LOSS0..32767, SEED0..UINT32_MAX, power-of-two buffer
 *                           64..8192; once per present operator. Canonical seed0
 *                           becomes 0x9E3779B9, written in decimal. Written after
 *                           wtuse and before voices; each voice owns its buffers.
 *   T_NS STREAM VOICE filter CUTOFF_MILLIHZ Q_MILLI    fm_voice_filter, on a voice
 *                           whose patch has a filter statement; until its next note-on
 * The field routes (space/SPEC.md sections 11 to 16) add, draft2 only, each once:
 *   space amb1|amb3|binaural   the ambisonic field at order 1 or 3 (4 or 16 interleaved channels,
 *                           AmbiX), or its binaural decode (2 channels; needs a table in the build)
 *   order 1|3               space binaural: the field's order; absent: 3
 *   pinna off|pbnh|rd|D1 D2 D3 D4 D5   Brown and Duda's pinna per voice, with the D of PB and NH,
 *                           of RD, or five D in thousandths, 0 to 1000; fs at most 48000; absent: off
 *   yaw MDEG                the listener's first yaw, millidegrees, at most 360000 in magnitude
 *   panlaw W                space stereo: the pan law (c) of width W in Q1.15, 1 to 32768
 *   T_NS STREAM 0 yaw MDEG  the listener's yaw from the first block boundary at or after its
 *                           sample; the listener is no voice, so the event names voice 0, which
 *                           every record has, and keeps the token layout every reader splits by
 * pinna and yaw need space amb1, amb3 or binaural. The hash covers the
 * interleaved stream of 4, 16 or 2 channels.
 * pass 1 reads the header statements, pass 2 the events. */
/* The stream belongs to the caller, including its read/close error handling.
 * File and in-memory imports share this exact line/token path. */
static int tl_read_stream(tl_t *tl, FILE *f, const char *path, int pass)
{
    char line[TL_LINE], *tok[TL_MAX_TOK];
    unsigned ln = 0;
    int nt, rc = 0;
    while (!rc) {
        size_t used = sizeof line;
        if (FM_HOST_CANCELLED()) return 6;
        memset(line, 0xff, sizeof line);
        if (!fgets(line, sizeof line, f)) break;
        ln++;
        /* fgets wrote a final NUL even if the input itself contains NULs.
         * Its index is the last byte before the untouched 0xff suffix. */
        while (used && (unsigned char)line[used - 1u] == 0xffu) used--;
        if (!used || strlen(line) != used - 1u) { rc = tl_err(path, ln, "NUL byte in line"); break; }
        if (ln == 1 && !strncmp(line, "\xef\xbb\xbf", 3)) {
            rc = tl_err(path, ln, "byte-order mark; save as UTF-8 without one"); break;
        }
        if (!strchr(line, '\n') && !feof(f)) { rc = tl_err(path, ln, "line longer than %d bytes", TL_LINE - 2); break; }
        nt = tl_split(line, tok);
        if (nt < 0) rc = tl_err(path, ln, "more than %d tokens", TL_MAX_TOK);
        else if (nt == 0) continue;
        else if ((tok[0][0] >= '0' && tok[0][0] <= '9') ||
                 ((tok[0][0] == '-' || tok[0][0] == '+') && tok[0][1] >= '0' && tok[0][1] <= '9')) {
            if (pass == 2) rc = tl_event(tl, path, ln, tok, nt);
        }
        else if (pass == 1) rc = tl_header(tl, path, ln, tok, nt);
    }
    return rc;
}

#ifdef FM_HOST_MEMORY_INPUT
static int tl_read_memory(tl_t *tl, const char *text, size_t length, const char *label, int pass)
{
    FILE *f;
    int rc;
    if (!text) return tl_err(label, 0, "no timeline text");
    f = fmemopen((void *)text, length, "r");
    if (!f) return io_fail(label, "open memory input");
    rc = tl_read_stream(tl, f, label, pass);
    return io_close_input(f, label, rc);
}
#endif

static int tl_read(tl_t *tl, const char *path, int pass)
{
    FILE *f = fopen(path, "r");
    int rc;
    if (!f) return io_fail(path, "open");
    rc = tl_read_stream(tl, f, path, pass);
    return io_close_input(f, path, rc);
}

/* One complete diagnostic, without a fixed-size buffer that could drop
 * later offenders. This is host reader validation, outside the library. */
static void tl_decodable_offender(int *bad, const char *fmt, ...)
{
    va_list ap;
    fputs(*bad ? ", " : "timeline: decodable: refused ", stderr);
    *bad = 1;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
}

/* /: after both read passes, before sorting or rendering.
 * A fixed stereo position places the source for the listener; the receiver
 * takes the mid. This conservative profile is not a proof of invertibility
 * or of unclipped output. Field routes and every present shape are in doubt.
 * Later FX statements must explicitly update this profile and its tests. */
static int tl_check_decodable(const tl_t *tl)
{
    unsigned char positioned[TL_MAX_VOICES] = { 0 };
    uint32_t i;
    int bad = 0;
    if (!tl->decodable) return 0;
    if (tl->fx.on & FX_ON_LIMITER) tl_decodable_offender(&bad, "limiter");
    if (tl->fx.on & FX_ON_SOFTCLIP) tl_decodable_offender(&bad, "softclip");
    if (tl->fx.on & FX_ON_COMPRESSOR) tl_decodable_offender(&bad, "compressor");
    if (tl->fx.on & FX_ON_CRUSH) tl_decodable_offender(&bad, "crush");
    if (tl->fx.on & FX_ON_CHORUS) tl_decodable_offender(&bad, "chorus");
    if ((tl->fx.on & FX_ON_FLANGER) && tl->fx.flanger_rate_millihz)
        tl_decodable_offender(&bad, "flanger (nonzero rate %u)", tl->fx.flanger_rate_millihz);
    for (i = 0; i < tl->npatches; ++i)
        if (tl->shapes & (1u << i))
            tl_decodable_offender(&bad, "shape %s drive %u %s (curve applies even at zero drive)",
                                 tl->patch[i].name, (unsigned)tl->patch[i].p.shape.drive_db10,
                                 TL_SHAPE_KIND[tl->patch[i].p.shape.kind]);
    if (tl->route)
        tl_decodable_offender(&bad, "%s (field route in doubt)",
                             tl->have_panlaw ? "panlaw" : tl_field_name(tl));
    for (i = 0; i < tl->nev; ++i) {
        const tl_event_t *e = &tl->ev[i];
        if (e->type == EV_MOVE)
            tl_decodable_offender(&bad, "move (voice %u at %llu ns)", e->voice, (unsigned long long)e->t_ns);
        if (e->type == EV_POS) {
            if (e->t_ns || positioned[e->voice])
                tl_decodable_offender(&bad, "pos (voice %u at %llu ns; %s position in doubt)",
                                     e->voice, (unsigned long long)e->t_ns,
                                     positioned[e->voice] ? "repeated" : "nonzero-time");
            positioned[e->voice] = 1;
        }
    }
    if (bad) fputc('\n', stderr);
    return bad;
}

static int tl_cmp(const void *a, const void *b)
{
    const tl_event_t *x = (const tl_event_t *)a, *y = (const tl_event_t *)b;
    if (x->t_ns != y->t_ns) return x->t_ns < y->t_ns ? -1 : 1;
    if (x->stream != y->stream) return x->stream < y->stream ? -1 : 1;
    return x->order < y->order ? -1 : x->order > y->order;
}

static void tl_write_gen_sources(FILE *f, const tl_t *tl)
{
    unsigned i, j, k;
    size_t p;
    for (i = 0; i < tl->gen.sources; ++i) {
        const gen_source_config_t *s = &tl->gen.source[i];
        tl_write_stmt(f, TS_gen, " %u %s %u %u %u", s->id, TL_GEN_KIND[s->kind], s->seed, s->div, s->slew);
        for (p = 0; p < sizeof TL_GEN_PARAMS / sizeof *TL_GEN_PARAMS; ++p) {
            const tl_gen_param_t *d = &TL_GEN_PARAMS[p];
            if ((d->kinds & (1u << (s->kind - 1))) && d->slot[0] == 'p') fprintf(f, " %lld", (long long)s->p[(unsigned)(d->slot[1] - '0')]);
        }
        if (s->kind == GEN_EUCLID) { if (s->parent < 0) fprintf(f, " -"); else fprintf(f, " %u", (unsigned)s->parent); }
        fputc('\n', f);
        for (j = 0; j < tl->gen.texts; ++j) if (tl->gen.text[j].id == s->id) {
            const gen_text_config_t *t = &tl->gen.text[j];
            tl_write_stmt(f, TS_gen_axiom, " %u %u %s\n", t->id, t->depth, t->axiom);
            for (k = 0; k < 8; ++k) if ((t->rule_mask >> k) & 1u)
                tl_write_stmt(f, TS_gen_rule, " %u %c %s\n", t->id, (int)('A' + k), t->rule[k][0] ? t->rule[k] : "-");
        }
    }
}

static void tl_write_gen_lanes(FILE *f, const tl_t *tl)
{
    unsigned i, j;
    for (i = 0; i < tl->gen.lanes; ++i) {
        const gen_lane_config_t *l = &tl->gen.lane[i];
        const int refs[3] = { l->gate, l->pitch, l->accent };
        tl_write_stmt(f, TS_gen_lane, " %u %u", l->id, l->voice);
        for (j = 0; j < 3; ++j) { if (refs[j] < 0) fprintf(f, " -"); else fprintf(f, " %u", (unsigned)refs[j]); }
        fprintf(f, " %u %d %u %u %u %u\n", l->step_ticks, l->base_cents, l->span_cents, l->velocity, l->gate_ticks, l->scale_mask);
    }
}

static int tl_write_gen_event(FILE *f, const tl_t *tl, const tl_event_t *e)
{
    size_t i;
    const char *name = NULL;
    if (e->type == EV_GEN_LANE) name = "enable";
    else if (e->type == EV_GEN_SET || e->type == EV_GEN_RESET) {
        for (i = 0; i < sizeof TL_GEN_PARAMS / sizeof *TL_GEN_PARAMS; ++i)
            if (TL_GEN_PARAMS[i].id == e->gen_field) { name = TL_GEN_PARAMS[i].name; break; }
    } else return 0;
    fprintf(f, "%llu %s %s %u %s %lld\n", (unsigned long long)e->t_ns, tl->stream[e->stream],
            TL_STATEMENTS[e->type == EV_GEN_LANE ? TS_event_lane : TS_event_gen].name, e->gen_id, name, (long long)e->gen_value);
    return 1;
}

/* The canonical record: header statements in the order of tl_read's list,
 * with hash, then the events in the order they apply, single blanks, no
 * comments. */
static int tl_write_f(FILE *f, const tl_t *tl, uint64_t hash)
{
    char eng[160];
    uint32_t i;
    int op;
    tl_engine(eng, sizeof eng);
    tl_write_stmt(f, TS_header_timeline, " draft%d\n", tl->draft);
    tl_write_stmt(f, TS_header_engine, " %s\n", eng);
    tl_write_stmt(f, TS_header_fs, " %u\n", tl->fs);
    if (tl->gen_clock) tl_write_stmt(f, TS_gen_clock, " %u\n", tl->gen_clock);
    tl_write_stmt(f, TS_header_length_ns, " %llu\n", (unsigned long long)tl->length_ns);
    if (tl->draft == 2) {
        tl_write_stmt(f, TS_header_space, " %s\n", tl->space ? "stereo" : tl_field_name(tl));
        tl_write_stmt(f, TS_header_block, " %u\n", tl->block);
        tl_write_stmt(f, TS_header_near, " %u\n", tl->near);
    }
    if (tl->draft == 2) tl_write_field(f, tl);
    if (tl->fx.on & FX_ON_DC)       tl_write_stmt(f, TS_dcblock, " %u\n", tl->fx.dc_millihz);   /* the effects, each when present */
    if (tl->fx.on & FX_ON_CHORUS)   tl_write_stmt(f, TS_chorus, " %u %u %u\n", tl->fx.chorus_base_us, tl->fx.chorus_depth_us, tl->fx.chorus_rate_millihz);
    if (tl->fx.on & FX_ON_FLANGER)  tl_write_stmt(f, TS_flanger, " %u %u %u %u %u\n", tl->fx.flanger_base_us, tl->fx.flanger_depth_us,
                                         tl->fx.flanger_rate_millihz, tl->fx.flanger_fb_q15, tl->fx.flanger_invert);
    if ((tl->fx.on & (FX_ON_DELAY | FX_ON_PINGPONG)) == FX_ON_DELAY)
        tl_write_stmt(f, TS_delay, " %u %u %u\n", tl->fx.delay_us, tl->fx.delay_fb_q15, tl->fx.delay_lp_hz);
    if (tl->fx.on & FX_ON_PINGPONG)
        tl_write_stmt(f, TS_pingpong, " %u %u %u %u\n", tl->fx.delay_us, tl->fx.delay_fb_q15, tl->fx.delay_lp_hz, tl->fx.delay_hp_hz);
    if (tl->fx.on & FX_ON_REVERB)   tl_write_stmt(f, TS_reverb, " %u %u\n", tl->fx.reverb_room, tl->fx.reverb_damp);
    if (tl->fx.on & FX_ON_COMPRESSOR)
        tl_write_stmt(f, TS_compressor, " %u %u %u %u %u\n", tl->fx.compressor_db10, tl->fx.compressor_ratio_tenths,
                tl->fx.compressor_attack_ms, tl->fx.compressor_release_ms, tl->fx.compressor_makeup_db10);
    if (tl->fx.on & FX_ON_LIMITER)  tl_write_stmt(f, TS_limiter, " %u %u\n", tl->fx.limiter_db10, tl->fx.limiter_release_ms);
    if (tl->fx.on & FX_ON_SOFTCLIP) tl_write_stmt(f, TS_softclip, "\n");
    if (tl->fx.on & FX_ON_GAIN)     tl_write_stmt(f, TS_gain, " %d\n", (int)tl->fx.gain_db10);
    if (tl->fx.on & FX_ON_DITHER)   tl_write_stmt(f, TS_dither, " %lu\n", (unsigned long)tl->fx.dither_seed);
    if (tl->fx.on & FX_ON_CRUSH)    tl_write_stmt(f, TS_crush, "\n");
    if (tl->decodable) tl_write_stmt(f, TS_decodable, "\n");
    tl_write_stmt(f, TS_header_streams, "");
    for (i = 0; i < tl->nstreams; i++) fprintf(f, " %s", tl->stream[i]);
    fputc('\n', f);
    tl_write_gen_sources(f, tl);
    for (i = 0; i < tl->npatches; i++)
        for (op = 0; op < ((tl->routes >> i) & 1u ? (int)tl->patch[i].p.route.ops : 2); op++) {
            const fm_op_patch_t *o = &tl->patch[i].p.op[op];
            tl_write_stmt(f, TS_patch, " %s %d %u %u %u %u %u %u %u %u %u", tl->patch[i].name, op, (unsigned)o->ratio_q16,
                    (unsigned)o->level_db10, (unsigned)o->index_q10, (unsigned)o->feedback_q10, (unsigned)o->vel_sens_q8,
                    (unsigned)o->attack_ms, (unsigned)o->decay_ms, (unsigned)o->sustain_db10, (unsigned)o->release_ms);
            {   /* the optional values up to the last nonzero one, key scaling as a pair  */
                const unsigned x[7] = { o->ks_break_millihz, o->ks_slope_db10, o->wave, o->rate_scale,
                                        o->fixed_millihz, o->phase_u16, o->free_run };
                int last = 7, k;
                while (last > 0 && !x[last - 1]) last--;
                if (last == 1) last = 2;
                for (k = 0; k < last; k++) fprintf(f, " %u", x[k]);
            }
            fputc('\n', f);
        }
    for (i = 0; i < tl->npatches; i++)   /* : the edges by FROM, then TO */
        if (tl->routes & (1u << i)) {
            const fm_route_t *r = &tl->patch[i].p.route;
            int to;
            tl_write_stmt(f, TS_route, " %s %u %u", tl->patch[i].name, (unsigned)r->ops, (unsigned)r->carriers);
            for (op = 0; op < (int)r->ops; op++)
                for (to = 0; to < (int)r->ops; to++)
                    if (to != op && r->w[op][to]) {
                        fprintf(f, " %d>%d:%u", op, to, (unsigned)r->w[op][to]);
                        if (r->kind[op][to]) fprintf(f, ":%s", r->kind[op][to] == 1 ? "am" : r->kind[op][to] == 2 ? "ring" : "sync");
                    }
            fputc('\n', f);
        }
    for (i = 0; i < tl->npatches; i++)   /*  */
        if (tl->filters & (1u << i))
            tl_write_stmt(f, TS_filter, " %s %s %u %u%s\n", tl->patch[i].name, TL_FILTER_MODE[tl->patch[i].p.filter.mode],
                    (unsigned)tl->patch[i].p.filter.cutoff_millihz, (unsigned)tl->patch[i].p.filter.q_milli,
                    tl->patch[i].p.filter.stages == 2 ? " x2" : "");
    for (i = 0; i < tl->npatches; i++)
        if (tl->shapes & (1u << i))
            tl_write_stmt(f, TS_shape, " %s %u %s%s\n", tl->patch[i].name, (unsigned)tl->patch[i].p.shape.drive_db10,
                    TL_SHAPE_KIND[tl->patch[i].p.shape.kind], tl->patch[i].p.shape.pos ? " pre" : "");
    for (i = 0; i < tl->npatches; i++)   /* : enabled patches only; a disabled amp plays as absent */
        if (tl->patch[i].p.amp.enabled) {
            const fm_amp_patch_t *a = &tl->patch[i].p.amp;
            tl_write_stmt(f, TS_amp, " %s %u %u %u %u\n", tl->patch[i].name, (unsigned)a->attack_ms, (unsigned)a->decay_ms,
                    (unsigned)a->sustain_db10, (unsigned)a->release_ms);
        }
    for (i = 0; i < tl->npatches; i++)   /* , every value */
        if (tl->lfos & (1u << i)) {
            const fm_lfo_patch_t *l = &tl->patch[i].p.lfo;
            tl_write_stmt(f, TS_lfo, " %s %u %u %u %u %u %u %u %u %u %u %u", tl->patch[i].name, (unsigned)l->wave,
                    (unsigned)l->rate_millihz, (unsigned)l->delay_ms, (unsigned)l->fade_ms, (unsigned)l->pitch_cents,
                    (unsigned)l->level_db10, (unsigned)l->index_q10, (unsigned)l->phase_u16, (unsigned)l->mask_pitch,
                    (unsigned)l->mask_level, (unsigned)l->mask_index);
            if (l->free_run) fprintf(f, " %u", (unsigned)l->free_run);
            fputc('\n', f);
        }
    for (i = 0; i < tl->npatches; i++)
        if (tl->pegs & (1u << i)) {
            const fm_peg_patch_t *p = &tl->patch[i].p.peg;
            const unsigned nop = tl->patch[i].p.route.ops ? tl->patch[i].p.route.ops : 2u;
            tl_write_stmt(f, TS_peg, " %s %d %u %d %u %d %u %d %u %u\n", tl->patch[i].name,
                    p->level_cents[0], (unsigned)p->time_ms[0], p->level_cents[1], (unsigned)p->time_ms[1],
                    p->level_cents[2], (unsigned)p->time_ms[2], p->level_cents[3], (unsigned)p->time_ms[3],
                    p->mask ? (unsigned)p->mask : (1u << nop) - 1u);
        }
    for (i = 0; i < tl->npatches; i++)
        if (tl->fenvs & (1u << i)) {
            const fm_fenv_patch_t *p = &tl->patch[i].p.fenv;
            tl_write_stmt(f, TS_fenv, " %s %u %u %u %u %d %u %u\n", tl->patch[i].name,
                    (unsigned)p->attack_ms, (unsigned)p->decay_ms, (unsigned)p->sustain_permille,
                    (unsigned)p->release_ms, p->depth_cents, (unsigned)p->track_permille,
                    p->ref_millihz ? p->ref_millihz : 261626u);
        }
    for (i = 0; i < tl->npatches; ++i)
        for (op = 0; op < FM_MOD_SLOTS; ++op) if ((tl->patch[i].mods >> op) & 1u) {
            const fm_mod_slot_patch_t *m = &tl->patch[i].p.mod[op];
            tl_write_stmt(f, TS_mod, " %s %u ", tl->patch[i].name, (unsigned)op);
            if ((tl->patch[i].gen_mods >> op) & 1u) fprintf(f, "gen.%u.%c", tl->patch[i].gen_id[op], tl->patch[i].gen_axis[op] ? 'y' : 'x');
            else fprintf(f, "%s", TL_MOD_SOURCE[m->source]);
            fprintf(f, " %s", TL_MOD_TARGET[m->target]);
            if (m->target <= 3 || m->target == 32 || m->target == 33 || m->target == 36 || m->target == 37) fprintf(f, "%u", (unsigned)m->target_index);
            fprintf(f, " %d %u\n", (int)m->amount, (unsigned)m->shift);
        }
    for (i = 0; i < tl->npatches; i++) {
        for (op = 0; op < FM_OPS; op++)
            if (tl->patch[i].p.eg[op].delay_ms || tl->patch[i].p.eg[op].decay2_ms || tl->patch[i].p.eg[op].sustain2_db10)
                tl_write_stmt(f, TS_eg, " %s %u %u %u %u\n", tl->patch[i].name, (unsigned)op, (unsigned)tl->patch[i].p.eg[op].delay_ms,
                        (unsigned)tl->patch[i].p.eg[op].decay2_ms, (unsigned)tl->patch[i].p.eg[op].sustain2_db10);
    }
    for (i = 0; i < tl->npatches; i++)
        for (op = 0; op < FM_OPS; op++)
            if (tl->patch[i].p.eg[op].left_slope_db10)
                tl_write_stmt(f, TS_ks, " %s %u %u\n", tl->patch[i].name, (unsigned)op, (unsigned)tl->patch[i].p.eg[op].left_slope_db10);
    for (i = 0; i < tl->npatches; i++)         /* : the engine's canonical values; the defaults are omitted */
        for (op = 0; op < FM_OPS; op++) {
            const fm_op_ext_t *x = &tl->patch[i].p.ext[op];
            const unsigned target = x->ks_target <= 2u ? x->ks_target : 0u, curve = x->ks_curve & 3u;
            if (target || curve)
                tl_write_stmt(f, TS_kstype, " %s %u %s %c %c\n", tl->patch[i].name, (unsigned)op, TL_KS_TARGET[target],
                        curve & 1u ? '+' : '-', curve & 2u ? '+' : '-');
            if (x->fb_type & 3u) tl_write_stmt(f, TS_fbtype, " %s %u %s\n", tl->patch[i].name, (unsigned)op, TL_FB_TYPE[x->fb_type & 3u]);
            if (x->loop) tl_write_stmt(f, TS_egloop, " %s %u\n", tl->patch[i].name, (unsigned)op);   /*  */
        }
    for (i = 0; i < tl->npatches; i++) {
        for (op = 0; op < FM_OPS; op++)
            if (tl->patch[i].p.noise[op].shift && tl->patch[i].p.noise[op].shift != 4)
                tl_write_stmt(f, TS_noise, " %s %u %u\n", tl->patch[i].name, (unsigned)op, (unsigned)tl->patch[i].p.noise[op].shift);
    }
    for (i = 0; i < tl->npatches; i++)
        for (op = 0; op < FM_OPS; op++)
            if (tl->patch[i].p.pd[op].enabled)
                tl_write_stmt(f, TS_pd, " %s %u %u\n", tl->patch[i].name, (unsigned)op, (unsigned)tl->patch[i].p.pd[op].knee_q15);
    for (i = 0; i < tl->npatches; i++)
        if (tl->patch[i].p.vector.enabled)
            tl_write_stmt(f, TS_vector, " %s %u %u\n", tl->patch[i].name, (unsigned)tl->patch[i].p.vector.x_q15, (unsigned)tl->patch[i].p.vector.y_q15);
    for (i = 0; i < tl->npatches; i++)
        for (op = 0; op < FM_FORMANTS; op++) {
            const fm_formant_patch_t *bank = &tl->patch[i].p.formant[op];
            if (bank->enabled) tl_write_stmt(f, TS_formant, " %s %u %u %u %u\n", tl->patch[i].name, op,
                bank->center_millihz, (unsigned)bank->q_milli, (unsigned)bank->level_db10);
        }
    for (i = 0; i < tl->wt.nbanks; i++) {        /* : the samples and their hash, whatever the bank's source */
        const tl_wt_bank_t *bk = &tl->wt.bank[i];
        const uint32_t count = FM_WT_BANDS * bk->frames * bk->length;
        uint32_t j, t;
        tl_write_stmt(f, TS_wtbank, " %s %u %u 12\n", bk->name, (unsigned)bk->frames, (unsigned)bk->length);
        for (j = 0; j < count; j += 16u) {        /* LENGTH is a multiple of 16 */
            tl_write_stmt(f, TS_wtdata, " %s %u %u %u", bk->name, (unsigned)(j / (bk->frames * bk->length)),
                    (unsigned)(j / bk->length % bk->frames), (unsigned)(j % bk->length));
            for (t = j; t < j + 16u; t++) fprintf(f, " %d", (int)tl->wt.pool[bk->offset + t]);
            fputc('\n', f);
        }
        tl_write_stmt(f, TS_wthash, " %s %016llx\n", bk->name, (unsigned long long)bk->hash);
    }
    for (i = 0; i < tl->npatches; i++)
        for (op = 0; op < FM_OPS; op++)
            if (tl->patch[i].p.wt[op].enabled && tl->patch[i].p.wt[op].bank < tl->wt.nbanks)
                tl_write_stmt(f, TS_wtuse, " %s %u %s %u\n", tl->patch[i].name, (unsigned)op, tl->wt.bank[tl->patch[i].p.wt[op].bank].name,
                        (unsigned)tl->patch[i].p.wt[op].position_q15);
    for (i = 0; i < tl->npatches; i++)
        for (op = 0; op < FM_OPS; op++)
            if (tl->patch[i].p.ks[op].enabled) {   /* : seed 0 is written as the seed it plays */
                const fm_ks_patch_t *ks = &tl->patch[i].p.ks[op];
                tl_write_stmt(f, TS_ksource, " %s %u %u %lu %u\n", tl->patch[i].name, (unsigned)op, (unsigned)ks->loss_q15,
                        (unsigned long)(ks->seed ? ks->seed : 0x9E3779B9u), (unsigned)ks->buffer);
            }
    for (i = 0; i < tl->nvoices; i++) tl_write_stmt(f, TS_header_voice, " %u %s\n", i, tl->patch[tl->voice[i]].name);
    tl_write_gen_lanes(f, tl);
    tl_write_stmt(f, TS_header_hash, " %016llx\n", (unsigned long long)hash);
    for (i = 0; i < tl->nev; i++) {
        const tl_event_t *e = &tl->ev[i];
        if (tl_write_gen_event(f, tl, e)) continue;
        fprintf(f, "%llu %s %u ", (unsigned long long)e->t_ns, tl->stream[e->stream], (unsigned)e->voice);
        if (e->type == EV_ON) tl_write_stmt(f, TS_event_on, " %u %u\n", (unsigned)e->freq_millihz, (unsigned)e->velocity_q15);
        else if (e->type == EV_PITCH) tl_write_stmt(f, TS_event_pitch, " %u\n", (unsigned)e->freq_millihz);
        else if (e->type == EV_POS) tl_write_stmt(f, TS_event_pos, " %d %d %d\n", (int)e->x, (int)e->y, (int)e->z);
        else if (e->type == EV_MOVE) tl_write_stmt(f, TS_event_move, " %d %d %d %llu\n", (int)e->x, (int)e->y, (int)e->z, (unsigned long long)e->dur_ns);
        else if (e->type == EV_SEND) tl_write_stmt(f, TS_event_send, " %s %u\n", FX_BUS_NAME[e->bus], (unsigned)e->level_q15);
        else if (e->type == EV_CRUSH) tl_write_stmt(f, TS_event_crush, " %u %u\n", e->crush_bits, e->crush_div);
        else if (e->type == EV_FILTER) tl_write_stmt(f, TS_event_filter, " %u %u\n", (unsigned)e->freq_millihz, (unsigned)e->velocity_q15);   /*  */
        else if (e->type == EV_CTL) tl_write_stmt(f, TS_event_ctl, " %u %d\n", (unsigned)e->velocity_q15, (int)e->ctl_value);
        else if (e->type == EV_YAW) tl_write_stmt(f, TS_event_yaw, " %d\n", (int)e->x);
        else tl_write_stmt(f, TS_event_off, "\n");
    }
    return ferror(f) ? 1 : 0;
}

/* Native schema coverage. This enumerates actual canonical writer output, not a second TL parser.
 * The snapshots exercise writer branches only; they do not initialize a
 * voice, render audio, or claim these arbitrary hashes identify audio. */
static int tl_params_writer_visit(const tl_t *tl, unsigned char *seen,
                                  unsigned *lines)
{
    FILE *f = tmpfile();
    char line[TL_LINE], *tok[TL_MAX_TOK];
    int rc = 0;
    if (!f) return io_fail("params writer", "create temporary stream");
    if (tl_write_f(f, tl, 0) || fflush(f) || fseek(f, 0, SEEK_SET)) {
        rc = io_fail("params writer", "write or rewind temporary stream");
        goto done;
    }
    while (fgets(line, sizeof line, f)) {
        unsigned i, found = TS_COUNT, matches = 0;
        int nt, event;
        if (!strchr(line, '\n')) {
            rc = tl_err(NULL, 0, "params writer: unterminated or oversized canonical line");
            goto done;
        }
        nt = tl_split(line, tok);
        if (nt <= 0) {
            rc = tl_err(NULL, 0, "params writer: empty or oversized token row");
            goto done;
        }
        event = tok[0][0] >= '0' && tok[0][0] <= '9';
        for (i = 0; i < TS_COUNT; ++i) {
            const tl_statement_t *s = &TL_STATEMENTS[i];
            const int is_event = !strcmp(s->section, "event");
            /* Ordinary events have their word at3; gen/lane at2. Native
             * writer output has a numeric voice/ID in the other position. */
            if (is_event != event) continue;
            if (s->word_position < nt && !strcmp(s->name, tok[s->word_position])) {
                found = i;
                ++matches;
            }
        }
        if (matches != 1) {
            rc = tl_err(NULL, 0, "params writer: canonical %s row has %u registrations",
                        event ? "event" : tok[0], matches);
            goto done;
        }
        seen[found] = 1;
        ++*lines;
    }
    if (ferror(f)) rc = io_fail("params writer", "read temporary stream");
done:
    if (fclose(f)) rc = io_fail("params writer", "close temporary stream");
    return rc;
}

static int tl_params_writer_check(void)
{
    static const unsigned types[] = {
        EV_ON, EV_PITCH, EV_OFF, EV_POS, EV_MOVE, EV_SEND, EV_CRUSH, EV_FILTER,
        EV_CTL, EV_GEN_SET, EV_GEN_RESET, EV_GEN_LANE
    };
    unsigned char seen[TS_COUNT] = { 0 };
    tl_event_t events[sizeof types / sizeof *types];
    tl_t *tl = (tl_t *)calloc(1, sizeof *tl);
    fm_patch_t *p;
    unsigned i, op, count = 0, lines = 0, snapshots = 0;
    int rc = 0;
    if (!tl) return tl_host_error(NULL, 0, "params writer: out of memory");
    memset(events, 0, sizeof events);
    tl->draft = 2;
    tl->space = 1;
    tl->fs = 48000;
    tl->n = 48000;
    tl->length_ns = UINT64_C(1000000000);
    tl->block = 64;
    tl->near = 100;
    tl->nstreams = 1;
    strcpy(tl->stream[0], "check");
    tl->npatches = tl->nvoices = 4;
    for (i = 0; i < tl->npatches; ++i) {
        snprintf(tl->patch[i].name, sizeof tl->patch[i].name, "check%u", i);
        tl->voice[i] = i;
        for (op = 0; op < FM_OPS; ++op) {
            fm_op_patch_t *o = &tl->patch[i].p.op[op];
            o->ratio_q16 = 65536;
            o->level_db10 = 300;
            o->vel_sens_q8 = 256;
            o->attack_ms = 1;
            o->decay_ms = 100;
            o->sustain_db10 = 120;
            o->release_ms = 100;
        }
    }
    p = &tl->patch[0].p;
    tl->routes = tl->filters = tl->shapes = tl->lfos = tl->pegs = tl->fenvs = 1;
    p->route.ops = FM_OPS;
    p->route.carriers = (1u << FM_OPS) - 1u;
    p->route.w[1][0] = 256;
    p->op[1].index_q10 = 1024;
    p->op[0].ks_break_millihz = 440000;
    p->op[0].ks_slope_db10 = 10;
    p->op[0].rate_scale = 1;
    p->op[0].phase_u16 = 32768;
    p->op[0].free_run = 2;
    p->filter.mode = 1;
    p->filter.cutoff_millihz = 1000000;
    p->filter.q_milli = 707;
    p->filter.stages = 2;
    p->shape.kind = p->shape.pos = 1;
    p->amp.enabled = 1;
    p->amp.attack_ms = 1;
    p->amp.decay_ms = p->amp.release_ms = 100;
    p->amp.sustain_db10 = 120;
    p->lfo.wave = 1;
    p->lfo.rate_millihz = 1000;
    p->lfo.pitch_cents = 10;
    p->lfo.mask_pitch = p->lfo.mask_level = p->lfo.mask_index = p->route.carriers;
    p->lfo.free_run = 2;
    p->peg.level_cents[0] = 100;
    p->peg.time_ms[0] = p->peg.time_ms[1] = p->peg.time_ms[3] = 100;
    p->peg.mask = p->route.carriers;
    p->fenv.attack_ms = p->fenv.decay_ms = p->fenv.release_ms = 100;
    p->fenv.sustain_permille = 500;
    p->fenv.depth_cents = 100;
    p->fenv.ref_millihz = 261626;
    tl->patch[0].mods = 3;
    p->mod[0].source = 0;
    p->mod[0].target = 1;
    p->mod[0].amount = 100;
    p->mod[0].shift = 3;
    tl->patch[0].gen_mods = 2;
    tl->patch[0].gen_id[1] = 0;
    p->mod[1].source = 65;
    p->mod[1].target = 4;
    p->mod[1].amount = 100;
    p->mod[1].shift = 3;
    p->eg[0].delay_ms = 1;
    p->eg[0].left_slope_db10 = 10;
    p->ext[0].ks_target = p->ext[0].ks_curve = p->ext[0].fb_type = p->ext[0].loop = 1;
    p->pd[0].enabled = 1;
    p->pd[0].knee_q15 = 16384;
    /* This is structural writer coverage even on smaller FM_OPS builds;
     * no vector render or applicability acceptance is asserted here. */
    p->vector.enabled = 1;
    p->vector.x_q15 = p->vector.y_q15 = 16384;
    p->formant[0].enabled = 1;
    p->formant[0].center_millihz = 1000000;
    p->formant[0].q_milli = 707;
    tl->patch[1].p.wt[0].enabled = 1;
    tl->patch[1].p.wt[0].position_q15 = 16384;
    tl->wt.nbanks = 1;
    strcpy(tl->wt.bank[0].name, "checkbank");
    tl->wt.bank[0].frames = 1;
    tl->wt.bank[0].length = FM_WT_LENGTH_MIN;
    tl->wt.bank[0].hash = wt_content_hash(1, FM_WT_LENGTH_MIN, tl->wt.pool);
    tl->patch[2].p.ks[0].enabled = 1;
    tl->patch[2].p.ks[0].loss_q15 = 32000;
    tl->patch[2].p.ks[0].buffer = FM_KS_BUFFER_MIN;
    tl->patch[3].p.op[0].wave = 10;
    tl->patch[3].p.noise[0].shift = 3;
    tl->gen_clock = 64;
    tl->gen.sources = 2;
    if (gen_defaults(&tl->gen.source[0], GEN_WHITE, 0, 1).code ||
        gen_defaults(&tl->gen.source[1], GEN_LSYSTEM, 1, 1).code) {
        rc = tl_err(NULL, 0, "params writer: generator defaults unavailable");
        goto done;
    }
    tl->gen.texts = 1;
    tl->gen.text[0].id = 1;
    tl->gen.text[0].depth = 1;
    strcpy(tl->gen.text[0].axiom, "AB");
    tl->gen.text[0].rule_mask = 3;
    strcpy(tl->gen.text[0].rule[0], "AB");
    tl->gen.lanes = 1;
    tl->gen.lane[0].voice = 1;
    tl->gen.lane[0].gate = 0;
    tl->gen.lane[0].pitch = tl->gen.lane[0].accent = -1;
    tl->gen.lane[0].step_ticks = 4;
    tl->gen.lane[0].gate_ticks = 1;
    tl->gen.lane[0].base_cents = 6900;
    tl->gen.lane[0].velocity = 20000;
    tl->fx.on = FX_ON_ALL & ~FX_ON_PINGPONG;
    tl->fx.channels = 2;
    tl->fx.block = 64;
    tl->fx.dc_millihz = 1000;
    tl->fx.chorus_base_us = 10000;
    tl->fx.chorus_depth_us = 1000;
    tl->fx.chorus_rate_millihz = 100;
    tl->fx.flanger_base_us = tl->fx.flanger_depth_us = 100;
    tl->fx.delay_us = 50000;
    tl->fx.delay_lp_hz = 1000;
    tl->fx.compressor_ratio_tenths = 10;
    tl->fx.compressor_release_ms = tl->fx.limiter_release_ms = 1;
    tl->fx.dither_seed = 1;
    for (i = 0; i < sizeof types / sizeof *types; ++i) {
        events[i].type = types[i];
        events[i].t_ns = UINT64_C(1000000) * i;
        events[i].freq_millihz = 440000;
        events[i].velocity_q15 = types[i] == EV_CTL ? 0 : types[i] == EV_FILTER ? 707 : 20000;
        events[i].x = 1000;
        events[i].dur_ns = UINT64_C(1000000);
        events[i].bus = FX_DELAY;
        events[i].level_q15 = 16384;
        events[i].crush_bits = 6;
        events[i].crush_div = 7;
        events[i].gen_field = types[i] == EV_GEN_RESET ? 5 : 4;
        events[i].gen_value = 1;
    }
    tl->ev = events;
    tl->nev = sizeof types / sizeof *types;
    rc = tl_params_writer_visit(tl, seen, &lines);
    if (rc) goto done;
    ++snapshots;
    tl->fx.on |= FX_ON_PINGPONG;
    p->route.kind[1][0] = 1;
    rc = tl_params_writer_visit(tl, seen, &lines);
    if (rc) goto done;
    ++snapshots;
    /* Field writer branches have no effects/send events. */
    tl->fx.on = 0;
    tl->space = 0;
    tl->route = SP_FIELD_BINAURAL;
    tl->have_order = tl->have_pinna = tl->have_yaw = 1;
    tl->order = 3;
    tl->pinna = SP_PINNA_PBNH;
    memset(events, 0, sizeof events);
    events[0].type = EV_YAW;
    tl->nev = 1;
    rc = tl_params_writer_visit(tl, seen, &lines);
    if (rc) goto done;
    ++snapshots;
    tl->pinna = SP_PINNA_D;
    for (i = 0; i < 5; ++i) tl->pinna_d[i] = 200;
    rc = tl_params_writer_visit(tl, seen, &lines);
    if (rc) goto done;
    ++snapshots;
    /* Reuse the object for independent panlaw then decodable headers. */
    tl->space = 1;
    tl->route = SP_FIELD_PANLAW;
    tl->have_order = tl->have_pinna = tl->have_yaw = 0;
    tl->have_panlaw = 1;
    tl->panlaw = 16384;
    tl->nev = 0;
    rc = tl_params_writer_visit(tl, seen, &lines);
    if (rc) goto done;
    ++snapshots;
    tl->space = tl->route = tl->have_panlaw = 0;
    tl->shapes = 0;
    tl->decodable = 1;
    rc = tl_params_writer_visit(tl, seen, &lines);
    if (rc) goto done;
    ++snapshots;
    for (i = 0; i < TS_COUNT; ++i) {
        /* Harmonics are reader authoring sugar; tl_write_f expands them to
         * wtdata, so no canonical output can contain a harmonic row. */
        if (!strcmp(TL_STATEMENTS[i].id, "harmonic")) continue;
        if (!seen[i]) {
            rc = tl_err(NULL, 0, "params writer: statement %s was not exercised", TL_STATEMENTS[i].id);
            goto done;
        }
        ++count;
    }
    printf("PARAMS_WRITER ok statements=%u snapshots=%u lines=%u\n", count, snapshots, lines);
    if (ferror(stdout)) rc = io_fail("stdout", "write parameter coverage");
done:
    /* events belongs to this stack; wt storage is embedded in tl. No reader
     * allocations, generator banks, assets or voice state were created. */
    free(tl);
    return rc;
}

/* Atomic path wrapper; the stream form leaves ownership to its caller. */
static int tl_write(const tl_t *tl, const char *path, uint64_t hash)
{
    char *tmp;
    FILE *f = io_open_tmp(path, "w", &tmp);
    int bad;
    if (!f) return HOST_ERROR;
    bad = tl_write_f(f, tl, hash);
    return io_finish_tmp(f, path, tmp, bad);
}

/* sp is the stereo module's state, or NULL on the mono path, where a pos or
 * move event is carried and not applied (space/SPEC.md, section 5). fx is
 * the effects' state, NULL without them. */
static void tl_apply(fm_voice_t *vs, sp_stereo_t *sp, fx_t *fx, const tl_event_t *e)
{
    if (e->type == EV_ON) fm_voice_note_on(&vs[e->voice], e->freq_millihz, e->velocity_q15);
    else if (e->type == EV_PITCH) fm_voice_pitch(&vs[e->voice], e->freq_millihz);
    else if (e->type == EV_POS) { if (sp) sp_stereo_set_pos(sp, e->voice, e->x, e->y, e->z); }
    else if (e->type == EV_MOVE) { if (sp) sp_stereo_move(sp, e->voice, e->x, e->y, e->z, e->end); }
    else if (e->type == EV_SEND) { if (fx) fx_set_send(fx, e->voice, e->bus, e->level_q15); }
    else if (e->type == EV_CRUSH) { if (fx) fx_set_crush(fx, e->voice, e->crush_bits, e->crush_div); }
    else if (e->type == EV_FILTER) fm_voice_filter(&vs[e->voice], e->freq_millihz, e->velocity_q15);   /*  */
    else if (e->type == EV_CTL) fm_voice_ctl(&vs[e->voice], e->velocity_q15, e->ctl_value);
    else if (e->type == EV_YAW) { if (sp) sp_field_set_yaw((sp_field_t *)sp, e->x); }
    else if (e->type == EV_OFF) fm_voice_note_off(&vs[e->voice]);
}

/* len frames from frame pos: the mono mix, or the stereo module's
 * interleaved pair per frame; on the mono path with fx, the effects'. */
static uint32_t tl_mix(const tl_t *tl, fm_voice_t *vs, sp_stereo_t *sp, fx_t *fx, int16_t *pcm, uint32_t pos, uint32_t len)
{
    if (sp && tl->route) return tl_field_mix(tl, vs, sp, pcm, pos, len);
    if (sp) return sp_stereo_render(sp, vs, tl->nvoices, pcm + 2 * (size_t)pos, len);
    if (fx) return fx_render_mono(fx, vs, tl->nvoices, pcm + pos, len);
    return fm_mix_i16(vs, tl->nvoices, pcm + pos, len);
}

/* MIDI-cent zero is MIDI note 0; 6900 cents is exactly A4=440000 mHz.
 * Round the octave argument symmetrically to Q48, then round the positive
 * wide product half up. All accepted lane cents are -4800..14400. */
static uint32_t tl_gen_millihz(int32_t cents)
{
    const int32_t d = cents - 6900;
    const uint64_t a = (((uint64_t)(uint32_t)(d < 0 ? -d : d) << 44) + 37u) / 75u;
    const int64_t factor = fm_exp2_q48(d < 0 ? -(int64_t)a : (int64_t)a);
    const fp_w128 product = fp_w_add(fp_w_muls(factor, 440000), fp_w_from_u64(UINT64_C(1) << 47));
    return (uint32_t)fp_w_to_i64(fp_w_asr(product, 48));
}

/* Exact ceil(frame*1e9/fs), with checked quotient/remainder arithmetic.
 * The remainder product fits uint64 because fs is at most UINT32_MAX. */
static int tl_gen_time(uint64_t frame, uint32_t fs, uint64_t *time_ns)
{
    uint64_t q, r, tail;
    if (!fs) return 1;
    q = frame / fs; r = frame % fs;
    tail = (r * UINT64_C(1000000000) + fs - 1u) / fs;
    if (q > (UINT64_MAX - tail) / UINT64_C(1000000000)) return 1;
    *time_ns = q * UINT64_C(1000000000) + tail;
    return 0;
}

/* Both render and manifest use this bank boundary. Explicit events have
 * already applied. Publish caches before the ordered generated offs/ons;
 * publishing never forces an extra per-voice control tick. */
/* Status helpers perform no allocation or I/O; persistent host consumers use
 * exactly this boundary. Capture is optional and never changes bank timing. */
static gen_status_t tl_gen_step_status(const tl_t *tl, tl_gen_runtime_t *r, uint64_t sample,
                                      fm_voice_t *vs, fm_voice_t *second, gen_frame_t *capture)
{
    gen_frame_t frame;
    gen_status_t status;
    unsigned v, n;
    /* Preserve gen_tick's invalid-clock/overflow precedence. A valid bank's
     * refused host position is checked before any state or output moves. */
    if (r->bank.ctl && r->bank.tick <= (UINT64_MAX - 1u) / r->bank.ctl
        && r->bank.tick * r->bank.ctl != sample) {
        status.code = GEN_RANGE; status.id = 0; status.field = GEN_FIELD_CLOCK; return status;
    }
    status = gen_tick(&r->bank, &frame);
    if (status.code) return status;
    for (v = 0; v < tl->nvoices; ++v) {
        const tl_patch_t *p = &tl->patch[tl->voice[v]];
        for (n = 0; n < FM_MOD_SLOTS; ++n) if ((p->gen_mods >> n) & 1u) {
            const int32_t value = frame.value[p->gen_id[n]][p->gen_axis[n]];
            if (vs) fm_voice_gen(&vs[v], n, value);
            if (second) fm_voice_gen(&second[v], n, value);
        }
    }
    for (n = 0; n < frame.events; ++n) {
        const gen_event_t *e = &frame.event[n];
        const uint32_t frequency = e->on ? tl_gen_millihz(e->cents) : 0;
        if (e->on) {
            if (vs) fm_voice_note_on(&vs[e->voice], frequency, e->velocity);
            if (second) fm_voice_note_on(&second[e->voice], frequency, e->velocity);
        } else {
            if (vs) fm_voice_note_off(&vs[e->voice]);
            if (second) fm_voice_note_off(&second[e->voice]);
        }
        r->events++;
    }
    if (capture) *capture = frame;
    return status;
}

static int tl_gen_step(const tl_t *tl, tl_gen_runtime_t *r, uint64_t sample,
                       fm_voice_t *vs, fm_voice_t *second, FILE *manifest)
{
    gen_frame_t frame;
    const gen_status_t status = tl_gen_step_status(tl, r, sample, vs, second, &frame);
    uint64_t time_ns = 0;
    unsigned n;
    if (status.code) return tl_err(NULL, 0, "generator tick refused: code=%u id=%u field=%u", status.code, status.id, status.field);
    if (manifest && frame.events) {
        fp_w128 roundtrip;
        if (tl_gen_time(sample, tl->fs, &time_ns) || time_ns >= tl->length_ns)
            return tl_err(NULL, 0, "generated event timestamp is unrepresentable or past the end");
        roundtrip = fp_w_divs(fp_w_mul(fp_w_from_u64(time_ns), fp_w_from_u64(tl->fs)), fp_w_from_u64(UINT64_C(1000000000)));
        if (fp_w_cmp(roundtrip, fp_w_from_u64(sample)))
            return tl_err(NULL, 0, "generated event frame %llu has no exact nanosecond timestamp at fs=%u", (unsigned long long)sample, tl->fs);
    }
    if (manifest) for (n = 0; n < frame.events; ++n) {
        const gen_event_t *e = &frame.event[n];
        fprintf(manifest, "GEN_EVENT frame=%llu time_ns=%llu lane=%u voice=%u kind=%s freq_millihz=%u velocity=%u\n",
                (unsigned long long)sample, (unsigned long long)time_ns, e->lane, e->voice,
                e->on ? "on" : "off", e->on ? tl_gen_millihz(e->cents) : 0, e->on ? e->velocity : 0);
    }
    return manifest && ferror(manifest) ? tl_host_error(NULL, 0, "cannot write generated event manifest") : 0;
}

/* The full-hour audit shares the production adapter loop. Only PCM storage
 * changes: mono blocks are hashed immediately and reuse this small buffer.
 * clock reports process CPU time; external tooling measures wall/RSS. */
typedef struct {
    gen_frame_t frame;
    int16_t pcm[FM_CTL];
    int32_t lo[GEN_SOURCES][2], hi[GEN_SOURCES][2], previous[GEN_SOURCES][2];
    uint64_t count[GEN_SOURCES], old_count[GEN_SOURCES], updates[GEN_SOURCES];
    uint64_t hash, minute_hash, ticks, ons, offs, rests, retriggers;
    uint64_t snapshot_checks, snapshot_bytes, snapshot_next;
    uint32_t resets, pending_resets, active_lanes, minute_frames, minute, peak;
    unsigned failures;
    clock_t tick_start;
    uint64_t tick_total, tick_max, restore_max, init_time;
} tl_gen_audit_t;

static void tl_gen_audit_init(tl_gen_audit_t *a)
{
    unsigned i, j;
    memset(a, 0, sizeof *a);
    a->hash = a->minute_hash = UINT64_C(14695981039346656037);
    a->snapshot_next = 0;
    for (i = 0; i < GEN_SOURCES; ++i) for (j = 0; j < 2; ++j) {
        a->lo[i][j] = INT32_MAX; a->hi[i][j] = INT32_MIN;
    }
}

/* CPU time covers the bank boundary, including cache publication and lane
 * note events. It excludes the engine render, audit validation and snapshots. */
static int tl_gen_cpu_elapsed(clock_t start, uint64_t *elapsed)
{
    const clock_t end = clock();
    if (start == (clock_t)-1 || end == (clock_t)-1 || end < start) return 1;
    *elapsed = (uint64_t)end - (uint64_t)start;
    return 0;
}

static int tl_gen_frame_equal(const gen_frame_t *a, const gen_frame_t *b)
{
    unsigned i, j;
    if (a->frame != b->frame || a->events != b->events) return 0;
    for (i = 0; i < GEN_SOURCES; ++i) for (j = 0; j < 2; ++j)
        if (a->value[i][j] != b->value[i][j]) return 0;
    for (i = 0; i < a->events; ++i) {
        const gen_event_t *x = &a->event[i], *y = &b->event[i];
        if (x->cents != y->cents || x->voice != y->voice || x->velocity != y->velocity
            || x->on != y->on || x->lane != y->lane) return 0;
    }
    return 1;
}

/* Advance the original and a restored bank for64 ticks, compare explicit
 * frames and canonical final bytes, then restore the original. Only public
 * generator calls write state. This is not an audio-state serialization. */
static int tl_gen_audit_snapshot(const tl_t *tl, tl_gen_runtime_t *r, tl_gen_audit_t *a)
{
    static const unsigned seconds[] = { 0, 1, 61, 601, 1801, 3599 };
    tl_gen_runtime_t *copy = NULL;
    uint8_t *saved = NULL, *sx = NULL, *sy = NULL;
    gen_frame_t fxg, fyg;
    gen_status_t status;
    size_t n = 0, nx = 0, ny = 0;
    clock_t start;
    uint64_t elapsed;
    unsigned j;
    int rc = 0;
    if (a->snapshot_next >= sizeof seconds / sizeof *seconds ||
        r->bank.tick < ((uint64_t)seconds[a->snapshot_next] * tl->fs + FM_CTL - 1u) / FM_CTL) return 0;
    copy = (tl_gen_runtime_t *)malloc(sizeof *copy);
    saved = (uint8_t *)malloc(GEN_SNAPSHOT_CAP); sx = (uint8_t *)malloc(GEN_SNAPSHOT_CAP); sy = (uint8_t *)malloc(GEN_SNAPSHOT_CAP);
    if (!copy || !saved || !sx || !sy) { rc = HOST_ERROR; goto done; }
    if (tl_gen_init(tl, copy)) { rc = 1; goto done; }
    status = gen_save(&r->bank, saved, GEN_SNAPSHOT_CAP, &n); if (status.code) { rc = 1; goto done; }
    status = gen_restore(&copy->bank, &tl->gen, &copy->memory, saved, n); if (status.code) { rc = 1; goto done; }
    for (j = 0; j < 64; ++j) {
        status = gen_tick(&r->bank, &fxg); if (status.code) { rc = 1; goto done; }
        status = gen_tick(&copy->bank, &fyg); if (status.code || !tl_gen_frame_equal(&fxg, &fyg)) { rc = 1; goto done; }
    }
    status = gen_save(&r->bank, sx, GEN_SNAPSHOT_CAP, &nx); if (status.code) { rc = 1; goto done; }
    status = gen_save(&copy->bank, sy, GEN_SNAPSHOT_CAP, &ny); if (status.code || nx != ny || memcmp(sx, sy, nx)) { rc = 1; goto done; }
    start = clock(); status = gen_restore(&r->bank, &tl->gen, &r->memory, saved, n);
    if (status.code || tl_gen_cpu_elapsed(start, &elapsed)) { rc = 1; goto done; }
    if (elapsed > a->restore_max) a->restore_max = elapsed;
    ++a->snapshot_checks; ++a->snapshot_next; if (n > a->snapshot_bytes) a->snapshot_bytes = n;
    printf("GENSOAK_SNAPSHOT frame=%llu bytes=%lu continuation_ticks=64 match=yes\n", (unsigned long long)(r->bank.tick * FM_CTL), (unsigned long)n);
done:
    free(copy); free(saved); free(sx); free(sy);
    if (rc) {
        (void)tl_err(NULL, 0, "generator snapshot continuation differs or failed");
        return rc;
    }
    return 0;
}

static int tl_gen_audit_before(const tl_t *tl, tl_gen_runtime_t *r, tl_gen_audit_t *a)
{
    unsigned i, j;
    const int rc = tl_gen_audit_snapshot(tl, r, a);
    if (rc) return rc;
    for (i = 0; i < GEN_SOURCES; ++i) if ((r->bank.source_mask >> i) & 1u) {
        const gen_source_t *s = &r->bank.source[i];
        a->old_count[i] = (s->config.kind == GEN_BROWN || s->config.kind == GEN_HENON || s->config.kind == GEN_LOGISTIC) ? s->state.map.count : 0;
        if (r->bank.tick % s->config.div == 0 && ((a->pending_resets >> i) & 1u)) {
            a->old_count[i] = 0; a->pending_resets &= ~(1u << i); ++a->resets;
        }
        for (j = 0; j < 2; ++j) a->previous[i][j] = s->published[j];
    }
    a->tick_start = clock();
    return a->tick_start == (clock_t)-1 ? tl_err(NULL, 0, "CPU timer unavailable") : 0;
}

static int tl_gen_audit_after(tl_gen_runtime_t *r, tl_gen_audit_t *a)
{
    unsigned i, j, off_mask = 0, on_mask = 0, actual_active = 0, last_off = 0, last_on = 0;
    uint64_t elapsed;
    if (tl_gen_cpu_elapsed(a->tick_start, &elapsed) || elapsed > UINT64_MAX - a->tick_total)
        return tl_err(NULL, 0, "CPU timer unavailable or wrapped");
    a->tick_total += elapsed; if (elapsed > a->tick_max) a->tick_max = elapsed;
    ++a->ticks;
    for (i = 0; i < GEN_SOURCES; ++i) if ((r->bank.source_mask >> i) & 1u) {
        const gen_source_t *s = &r->bank.source[i];
        if ((r->bank.tick - 1u) % s->config.div == 0 && s->enabled) ++a->updates[i];
        if (s->config.kind == GEN_BROWN || s->config.kind == GEN_HENON || s->config.kind == GEN_LOGISTIC) {
            if (s->state.map.count < a->old_count[i]) ++a->failures;
            else a->count[i] += s->state.map.count - a->old_count[i];
            if (s->config.kind != GEN_BROWN && s->state.map.count) ++a->failures;
        }
        for (j = 0; j < 2; ++j) {
            int32_t value = a->frame.value[i][j];
            int64_t delta = (int64_t)value - a->previous[i][j];
            if (value < -32768 || value > 32767 || value != s->published[j]) ++a->failures;
            if (s->config.slew && (delta > s->config.slew || delta < -(int64_t)s->config.slew)) ++a->failures;
            if (value < a->lo[i][j]) a->lo[i][j] = value;
            if (value > a->hi[i][j]) a->hi[i][j] = value;
        }
    }
    if (a->frame.events > 2 * GEN_LANES) return tl_err(NULL, 0, "generator event count exceeds frame");
    for (i = 0; i < a->frame.events; ++i) {
        const gen_event_t *e = &a->frame.event[i];
        const gen_lane_config_t *lane;
        unsigned mask;
        if (e->lane >= GEN_LANES || !((r->bank.lane_mask >> e->lane) & 1u)) return tl_err(NULL, 0, "invalid event lane");
        lane = &r->bank.lane[e->lane].config; mask = 1u << e->lane;
        if (e->voice != lane->voice || e->on > 1) ++a->failures;
        if (e->on) {
            int pitch_class = (e->cents / 100) % 12;
            if ((on_mask & mask) || (on_mask && e->lane <= last_on) || !e->velocity || e->velocity > 32767) ++a->failures;
            last_on = e->lane;
            if (pitch_class < 0) pitch_class += 12;
            if (lane->scale_mask && (e->cents % 100 || !((lane->scale_mask >> pitch_class) & 1u))) ++a->failures;
            if (e->cents < -4800 || e->cents > 14400 || e->voice != lane->voice) ++a->failures;
            if (off_mask & mask) ++a->retriggers;
            on_mask |= mask; a->active_lanes |= mask; ++a->ons;
        } else {
            if (on_mask || (off_mask & mask) || (off_mask && e->lane <= last_off) || e->velocity || e->cents || !(a->active_lanes & mask)) ++a->failures;
            last_off = e->lane; off_mask |= mask; a->active_lanes &= ~mask; ++a->offs;
        }
    }
    for (i = 0; i < GEN_LANES; ++i) if (((r->bank.lane_mask >> i) & 1u) && !(a->active_lanes & (1u << i))) ++a->rests;
    for (i = 0; i < GEN_LANES; ++i) if (r->bank.lane[i].active) actual_active |= 1u << i;
    if (actual_active != a->active_lanes) ++a->failures;
    return a->failures ? tl_err(NULL, 0, "generator soak bound, counter, scale or event invariant failed") : 0;
}

static void tl_gen_audit_pcm(const tl_t *tl, tl_gen_audit_t *a, uint32_t n)
{
    uint32_t i;
    for (i = 0; i < n; ++i) {
        uint16_t u = (uint16_t)a->pcm[i];
        uint32_t peak = (uint32_t)(a->pcm[i] < 0 ? -a->pcm[i] : a->pcm[i]);
        a->hash ^= u & 255u; a->hash *= UINT64_C(1099511628211);
        a->hash ^= u >> 8; a->hash *= UINT64_C(1099511628211);
        a->minute_hash ^= u & 255u; a->minute_hash *= UINT64_C(1099511628211);
        a->minute_hash ^= u >> 8; a->minute_hash *= UINT64_C(1099511628211);
        if (peak > a->peak) a->peak = peak;
        if (++a->minute_frames == (uint64_t)tl->fs * 60u) {
            printf("GENSOAK_MINUTE minute=%u hash=%016llx\n", ++a->minute, (unsigned long long)a->minute_hash);
            a->minute_frames = 0; a->minute_hash = UINT64_C(14695981039346656037);
        }
    }
}

/*  host-only sink: one additional deterministic native dry pass.
 * Bounded scratch, no duration-sized dry array or downstream processing. */
typedef struct {
    FILE *file;
    const char *name;
    uint64_t hash, bytes;
    unsigned char interleaved[128u * TL_MAX_VOICES * 4u];
    int32_t voice[128];
} tl_dry_sink_t;

static uint32_t tl_render_span(tl_t *tl, fm_voice_t *vs, sp_stereo_t *sp, fx_t *fx,
                              int16_t *pcm, uint32_t pos, uint32_t frames, tl_dry_sink_t *dry)
{
    if (!dry) {
        uint32_t clipped = 0;
        if (!FM_HOST_CANCELLABLE) return tl_mix(tl, vs, sp, fx, pcm, pos, frames);
        while (frames && !tl->render_error) {
            const uint32_t n = frames < 256u ? frames : 256u;
            if (FM_HOST_CANCELLED()) { tl->render_error = 6; break; }
            clipped += tl_mix(tl, vs, sp, fx, pcm, pos, n);
            pos += n;
            frames -= n;
        }
        return clipped;
    }
    while (frames && !tl->render_error) {
        const uint32_t n = frames < 128u ? frames : 128u;
        uint32_t v, t, byte;
        size_t count;
        if (FM_HOST_CANCELLED()) { tl->render_error = 6; break; }
        for (v = 0; v < tl->nvoices; ++v) {
            fm_render_q30(&vs[v], dry->voice, n);
            for (t = 0; t < n; ++t) {
                const uint32_t word = (uint32_t)dry->voice[t];
                const size_t at = ((size_t)t * tl->nvoices + v) * 4u;
                for (byte = 0; byte < 4u; ++byte)
                    dry->interleaved[at + byte] = (unsigned char)(word >> (8u * byte));
            }
        }
        count = (size_t)n * tl->nvoices * 4u;
        for (t = 0; t < count; ++t) {
            dry->hash ^= dry->interleaved[t];
            dry->hash *= UINT64_C(1099511628211);
        }
        if (fwrite(dry->interleaved, 1, count, dry->file) != count) {
            tl->render_error = io_fail(dry->name, "write");
            break;
        }
        dry->bytes += count;
        frames -= n;
    }
    return 0;
}

static uint32_t tl_gen_render_mode(tl_t *tl, fm_voice_t *vs, sp_stereo_t *sp, fx_t *fx, int16_t *pcm, int per_sample, tl_gen_audit_t *audit, tl_dry_sink_t *dry)
{
    tl_gen_runtime_t *r = (tl_gen_runtime_t *)malloc(sizeof *r);
    uint32_t pos = 0, e = 0, clipped = 0;
    uint64_t next_tick = 0;
    uint32_t random = 0x243f6a88u;
    if (!r) { tl->render_error = tl_host_error(NULL, 0, "out of memory"); return 0; }
    if (FM_HOST_CANCELLED()) { free(r); tl->render_error = 6; return 0; }
    { clock_t start = audit ? clock() : 0;
      tl->render_error = tl_gen_init(tl, r);
      if (audit && tl_gen_cpu_elapsed(start, &audit->init_time)) tl->render_error = tl_err(NULL, 0, "CPU timer unavailable or wrapped"); }
    while (pos < tl->n && !tl->render_error) {
        uint64_t next;
        if (FM_HOST_CANCELLED()) { tl->render_error = 6; break; }
        while (e < tl->nev && tl->ev[e].sample == pos) {
            tl->render_error = tl_gen_apply(r, &tl->ev[e]);
            if (tl->render_error) break;
            if (audit && tl->ev[e].type == EV_GEN_RESET) audit->pending_resets |= 1u << tl->ev[e].gen_id;
            tl_apply(vs, sp, fx, &tl->ev[e++]);
        }
        if (tl->render_error) break;
        if (pos == next_tick) {
            if (audit && (tl->render_error = tl_gen_audit_before(tl, r, audit))) break;
            { gen_status_t status = tl_gen_step_status(tl, r, pos, vs, NULL, audit ? &audit->frame : NULL);
              if (status.code) tl->render_error = tl_err(NULL, 0, "generator tick refused: code=%u id=%u field=%u", status.code, status.id, status.field); }
            if (tl->render_error) break;
            if (audit && (tl->render_error = tl_gen_audit_after(r, audit))) break;
            next_tick += tl->gen_clock;
        }
        next = next_tick;
        if (per_sample) {
            unsigned chunk;
            if (per_sample < 0) { random ^= random << 13; random ^= random >> 17; random ^= random << 5; chunk = 1u + random % 257u; }
            else chunk = (unsigned)per_sample;
            if (next > (uint64_t)pos + chunk) next = (uint64_t)pos + chunk;
        }
        if (next > tl->n) next = tl->n;
        if (e < tl->nev && next > tl->ev[e].sample) next = tl->ev[e].sample;
        if (audit) {
            clipped += tl_mix(tl, vs, NULL, NULL, audit->pcm, 0, (uint32_t)(next - pos));
            tl_gen_audit_pcm(tl, audit, (uint32_t)(next - pos));
        } else clipped += tl_render_span(tl, vs, sp, fx, pcm, pos, (uint32_t)(next - pos), dry);
        pos = (uint32_t)next;
    }
    free(r); return clipped;
}

static uint32_t tl_gen_render(tl_t *tl, fm_voice_t *vs, sp_stereo_t *sp, fx_t *fx, int16_t *pcm, int per_sample)
{
    return tl_gen_render_mode(tl, vs, sp, fx, pcm, per_sample, NULL, NULL);
}

/* The mix of the timeline's voices, one mix call per stretch between
 * events, or per_sample 1 per sample, >1 fixed chunks, -1 seeded chunks. Returns the clipped
 * count. With sp the render is the stereo module's; its block and near
 * were checked with the header; field/FX initialization and attachment are
 * checked again for each render and rewind. FX memory follows its state. */
static uint32_t tl_render_mode(tl_t *tl, fm_voice_t *vs, sp_stereo_t *sp, fx_t *fx, int16_t *pcm, int per_sample, tl_dry_sink_t *dry)
{
    uint32_t i, e = 0, pos = 0, clipped = 0;
    tl->render_error = 0;
    if (FM_HOST_CANCELLED()) { tl->render_error = 6; return 0; }
    for (i = 0; i < tl->nvoices; i++) (void)tl_voice_init(tl, &vs[i], i);   /* the header check bound the banks once */
    if (sp && (tl->route ? tl_field_init(tl, sp) : sp_stereo_init(sp, tl->block, tl->near))) {
        tl->render_error = tl_err(NULL, 0, "the spatial configuration is refused"); return 0;
    }
    if (fx && tl_fx_init(tl, fx)) {
        tl->render_error = tl_err(NULL, 0, "the effects' configuration is refused"); return 0;
    }
    if (sp && fx) {
        if (tl->route) {
            if (sp_field_attach_fx((sp_field_t *)sp, fx)) {
                tl->render_error = tl_err(NULL, 0, "the field/effects attachment is refused"); return 0;
            }
        } else sp->fx = fx;
    }
    if (tl->gen_clock) return dry ? tl_gen_render_mode(tl, vs, sp, fx, pcm, per_sample, NULL, dry)
                                   : tl_gen_render(tl, vs, sp, fx, pcm, per_sample);
    if (per_sample) {
        uint32_t random = 0x243f6a88u;
        while (pos < tl->n && !tl->render_error) {
            uint32_t chunk, next = tl->n;
            if (FM_HOST_CANCELLED()) { tl->render_error = 6; break; }
            while (e < tl->nev && tl->ev[e].sample == pos) tl_apply(vs, sp, fx, &tl->ev[e++]);
            if (per_sample < 0) { random ^= random << 13; random ^= random >> 17; random ^= random << 5; chunk = 1u + random % 257u; }
            else chunk = (uint32_t)per_sample;
            if (e < tl->nev && next > tl->ev[e].sample) next = tl->ev[e].sample;
            if (next - pos > chunk) next = pos + chunk;
            clipped += tl_render_span(tl, vs, sp, fx, pcm, pos, next - pos, dry);
            pos = next;
        }
        return clipped;
    }
    for (e = 0; e < tl->nev && !tl->render_error; e++) {
        if (FM_HOST_CANCELLED()) { tl->render_error = 6; return clipped; }
        clipped += tl_render_span(tl, vs, sp, fx, pcm, pos, tl->ev[e].sample - pos, dry);
        if (tl->render_error) return clipped;
        pos = tl->ev[e].sample;
        tl_apply(vs, sp, fx, &tl->ev[e]);
    }
    return clipped + tl_render_span(tl, vs, sp, fx, pcm, pos, tl->n - pos, dry);
}

static uint32_t tl_render(tl_t *tl, fm_voice_t *vs, sp_stereo_t *sp, fx_t *fx, int16_t *pcm, int per_sample)
{
    return tl_render_mode(tl, vs, sp, fx, pcm, per_sample, NULL);
}

/* Called only after both ordinary renders, using their now-idle voice bank.
 * fd ownership transfers on entry. The two PCM arrays remain with PLAY. */
static int tl_write_stems(tl_t *tl, fm_voice_t *vs, const char *path, int fd, int partition)
{
    tl_dry_sink_t *dry = (tl_dry_sink_t *)calloc(1, sizeof *dry);
    char *tmp = NULL;
    const char *name = path ? path : "--stems-fd";
    const uint32_t bytes = (uint32_t)((uint64_t)tl->n * tl->nvoices * 4u);
    int rc = 0;
    if (!dry) {
        if (fd >= 0) close(fd);
        return tl_host_error(NULL, 0, "out of memory");
    }
    dry->name = name;
    dry->hash = UINT64_C(14695981039346656037);
    dry->file = path ? io_open_tmp(path, "wb", &tmp) : fdopen(fd, "wb");
    if (!dry->file) {
        if (!path) { rc = io_fail(name, "open stream"); close(fd); }
        else rc = HOST_ERROR;
        free(dry); return rc;
    }
    fwrite("RIFF", 1, 4, dry->file); put_u32(dry->file, bytes + 36u);
    fwrite("WAVEfmt ", 1, 8, dry->file); put_u32(dry->file, 16u);
    put_u16(dry->file, 1u); put_u16(dry->file, (uint16_t)tl->nvoices);
    put_u32(dry->file, tl->fs); put_u32(dry->file, tl->fs * 4u * tl->nvoices);
    put_u16(dry->file, (uint16_t)(4u * tl->nvoices)); put_u16(dry->file, 32u);
    fwrite("data", 1, 4, dry->file); put_u32(dry->file, bytes);
    if (ferror(dry->file)) rc = io_fail(name, "write header");
    else {
        (void)tl_render_mode(tl, vs, NULL, NULL, NULL, partition, dry);
        rc = tl->render_error;
        if (!rc && dry->bytes != bytes) {
            fprintf(stderr, "%s: incomplete dry payload\n", name);
            rc = HOST_ERROR;
        }
    }
    if (path && !rc) rc = io_finish_tmp(dry->file, path, tmp, 0);
    else {
        if (fflush(dry->file)) rc = exit_merge(rc, io_fail(name, "write"));
        if (fclose(dry->file)) rc = exit_merge(rc, io_fail(name, "close"));
        if (tmp) { remove(tmp); free(tmp); }
    }
    if (!rc) {
        printf("STEMS format=s32le_q2_30 stage=voice_out_pre_fx fs=%u frames=%u voices=%u bytes=%u hash=%016llx partition=",
               tl->fs, tl->n, tl->nvoices, bytes, (unsigned long long)dry->hash);
        if (partition < 0) printf("random"); else printf("%d", partition);
        printf(" passes=1 block_indep=unchecked\n");
        /* Canonical publication can block or fail after this completed stage. */
        if (fflush(stdout)) rc = io_fail("stdout", "write STEMS");
    }
    free(dry);
    return rc;
}

/* A per-frame oracle calls the bank and voices directly in the documented
 * order; it does not call the adapter's event, publish or scheduling helpers. */
static unsigned gen_adapter_bounds(uint32_t fs)
{
    bound_t bounds = { "GENADAPTER-FAIL", 0, 0 };
    static const int chunks[] = { 0, 1, 7, 64, 65, 257, -1 };
    tl_t *tl = (tl_t *)calloc(1, sizeof *tl);
    tl_gen_runtime_t *oracle = (tl_gen_runtime_t *)malloc(sizeof *oracle);
    fm_voice_t expected[3], got[3];
    int16_t a[4097], b[4097];
    tl_event_t ev[10];
    unsigned i, j, n, k;
    uint32_t ei = 0;
    uint64_t timestamp;
    gen_status_t status;
    if (!tl || !oracle) { free(tl); free(oracle); return BOUNDS_HOST_ERROR; }
    tl->fs = fs; tl->n = 4097; tl->length_ns = ((uint64_t)tl->n * 1000000000u + fs - 1u) / fs;
    tl->gen_clock = FM_CTL; tl->nvoices = 3; tl->npatches = 1; tl->block = 64;
    tl->patch[0].p.op[0].ratio_q16 = 65536; tl->patch[0].p.op[1].ratio_q16 = 131072;
    tl->patch[0].p.op[0].level_db10 = 240; tl->patch[0].p.op[0].attack_ms = 2;
    tl->patch[0].p.op[0].release_ms = 1; tl->patch[0].p.op[0].free_run = 2;
    tl->patch[0].p.mod[0] = (fm_mod_slot_patch_t){ 600, 64, 1, 0, 0, {0,0,0} };
    tl->patch[0].mods = tl->patch[0].gen_mods = 1; tl->patch[0].gen_id[0] = 0;
    status = gen_defaults(&tl->gen.source[0], GEN_WHITE, 0, 7); BOUND_CHECK(&bounds, !status.code);
    tl->gen.source[0].div = 3; tl->gen.source[0].slew = 512; tl->gen.sources = 1; tl->gen_sources = 1;
    tl->gen.lanes = 2; tl->gen_lanes = 3;
    for (i = 0; i < 2; ++i) {
        gen_lane_config_t *l = &tl->gen.lane[i];
        l->id = (uint8_t)i; l->voice = (uint16_t)i; l->gate = l->accent = -1; l->pitch = 0;
        l->step_ticks = (uint16_t)(i ? 11 : 7); l->gate_ticks = (uint16_t)(i ? 5 : 3);
        l->base_cents = i ? 5700 : 6900; l->span_cents = 1200; l->velocity = 18000;
    }
    memset(ev, 0, sizeof ev);
    ev[0].sample = 0; ev[0].type = EV_ON; ev[0].voice = 2; ev[0].freq_millihz = 440000; ev[0].velocity_q15 = 20000;
    ev[1].sample = 63; ev[1].type = EV_GEN_SET; ev[1].gen_param = GEN_PARAM_SLEW; ev[1].gen_value = 0;
    ev[2].sample = 64; ev[2].type = EV_GEN_RESET; ev[2].gen_value = 42;
    ev[3].sample = 65; ev[3].type = EV_GEN_LANE; ev[3].gen_id = 1; ev[3].gen_value = 0;
    ev[4].sample = 127; ev[4].type = EV_GEN_SET; ev[4].gen_param = GEN_PARAM_ENABLED; ev[4].gen_value = 0;
    ev[5].sample = 192; ev[5].type = EV_GEN_LANE; ev[5].gen_id = 1; ev[5].gen_value = 1;
    ev[6].sample = 193; ev[6].type = EV_GEN_SET; ev[6].gen_param = GEN_PARAM_ENABLED; ev[6].gen_value = 1;
    ev[7] = ev[0]; ev[7].sample = 256; ev[7].freq_millihz = 330000;
    ev[8].sample = 400; ev[8].voice = 2; ev[8].type = EV_OFF;
    ev[9] = ev[0]; ev[9].sample = 1025;
    for (i = 0; i < 10; ++i) BOUND_CHECK(&bounds, !tl_gen_time(ev[i].sample, fs, &ev[i].t_ns));
    tl->ev = ev; tl->nev = 10;
    BOUND_CHECK(&bounds, !tl_gen_init(tl, oracle));
    for (i = 0; i < 3; ++i) fm_voice_init(&expected[i], &tl->patch[0].p, fs);
    for (i = 0; i < tl->n; ++i) {
        while (ei < tl->nev && ev[ei].sample == i) {
            const tl_event_t *e = &ev[ei++];
            if (e->type == EV_ON) fm_voice_note_on(&expected[e->voice], e->freq_millihz, e->velocity_q15);
            else if (e->type == EV_OFF) fm_voice_note_off(&expected[e->voice]);
            else {
                status = e->type == EV_GEN_RESET ? gen_reset(&oracle->bank, e->gen_id, (uint32_t)e->gen_value)
                    : e->type == EV_GEN_LANE ? gen_lane_enable(&oracle->bank, e->gen_id, (uint8_t)e->gen_value)
                    : gen_set(&oracle->bank, e->gen_id, e->gen_param, e->gen_value);
                BOUND_CHECK(&bounds, !status.code);
            }
        }
        if (i % FM_CTL == 0) {
            gen_frame_t f;
            status = gen_tick(&oracle->bank, &f); BOUND_CHECK(&bounds, !status.code && f.frame == i);
            if (i == 0) BOUND_CHECK(&bounds, expected[2].mod_cur[0] == 0 && expected[2].ctl == FM_CTL);
            for (j = 0; j < 3; ++j) {
                const uint32_t ctl = expected[j].ctl;
                const int32_t current = expected[j].mod_cur[0];
                fm_voice_gen(&expected[j], 0, f.value[0][0]);
                BOUND_CHECK(&bounds, expected[j].ctl == ctl && expected[j].mod_cur[0] == current);
            }
            for (j = 0; j < f.events; ++j) {
                const gen_event_t *e = &f.event[j];
                if (e->on) fm_voice_note_on(&expected[e->voice], tl_gen_millihz(e->cents), e->velocity);
                else fm_voice_note_off(&expected[e->voice]);
            }
            if (i == 0) BOUND_CHECK(&bounds, expected[0].mod_cur[0] == f.value[0][0] && expected[0].ctl == FM_CTL);
        }
        fm_mix_i16(expected, 3, &a[i], 1);
    }
    for (k = 0; k < sizeof chunks / sizeof *chunks; ++k) {
        memset(b, 0, sizeof b);
        tl_render(tl, got, NULL, NULL, b, chunks[k]);
        if (tl->render_error == HOST_ERROR) { free(oracle); free(tl); return BOUNDS_HOST_ERROR; }
        BOUND_CHECK(&bounds, !tl->render_error);
        for (i = 0; i < tl->n; ++i) BOUND_CHECK(&bounds, a[i] == b[i]);
        for (i = 0; i < 3; ++i) BOUND_CHECK(&bounds, mod_state_equal(&expected[i], &got[i]));
    }
    /* An unused source cannot consume another source's draws or lane ticks. */
    status = gen_defaults(&tl->gen.source[1], GEN_PINK, 15, 123); BOUND_CHECK(&bounds, !status.code);
    tl->gen.sources = 2; tl->gen_sources |= 1u << 15;
    tl_render(tl, got, NULL, NULL, b, 65);
    if (tl->render_error == HOST_ERROR) { free(oracle); free(tl); return BOUNDS_HOST_ERROR; }
    BOUND_CHECK(&bounds, !tl->render_error);
    for (i = 0; i < tl->n; ++i) BOUND_CHECK(&bounds, a[i] == b[i]);
    for (i = 0; i < 3; ++i) BOUND_CHECK(&bounds, mod_state_equal(&expected[i], &got[i]));
    /* Sparse public IDs and both Henon axes map independently of slot index.
     * Compare bank snapshots to a single-reader oracle after every tick: the
     * three listening voices cannot consume additional source updates. */
    {
        tl_gen_runtime_t *actual = (tl_gen_runtime_t *)malloc(sizeof *actual);
        uint8_t *sa = (uint8_t *)malloc(GEN_SNAPSHOT_CAP), *sb = (uint8_t *)malloc(GEN_SNAPSHOT_CAP);
        gen_source_config_t pink = tl->gen.source[1];
        if (!actual || !sa || !sb) { free(actual); free(sa); free(sb); free(oracle); free(tl); return BOUNDS_HOST_ERROR; }
        status = gen_defaults(&tl->gen.source[1], GEN_HENON, 5, 987); BOUND_CHECK(&bounds, !status.code);
        tl->gen.source[2] = pink; tl->gen.sources = 3; tl->gen_sources |= 1u << 5;
        tl->patch[0].p.mod[7] = (fm_mod_slot_patch_t){ 600, 71, 1, 0, 0, {0,0,0} };
        tl->patch[0].gen_mods = tl->patch[0].mods = 129;
        tl->patch[0].gen_id[0] = 15; tl->patch[0].gen_id[7] = 5;
        for (k = 0; k < 2; ++k) {
            tl->patch[0].gen_axis[7] = (uint8_t)k;
            BOUND_CHECK(&bounds, !tl_gen_init(tl, actual) && !tl_gen_init(tl, oracle));
            for (i = 0; i < 3; ++i) fm_voice_init(&got[i], &tl->patch[0].p, fs);
            for (i = 0; i < 129; ++i) {
                gen_frame_t f;
                size_t na = 0, nb = 0;
                status = gen_tick(&oracle->bank, &f); BOUND_CHECK(&bounds, !status.code);
                BOUND_CHECK(&bounds, !tl_gen_step(tl, actual, (uint64_t)i * FM_CTL, got, NULL, NULL));
                for (j = 0; j < 3; ++j) {
                    BOUND_CHECK(&bounds, got[j].gen_input[0] == f.value[15][0]);
                    BOUND_CHECK(&bounds, got[j].gen_input[7] == f.value[5][k]);
                }
                status = gen_save(&actual->bank, sa, GEN_SNAPSHOT_CAP, &na); BOUND_CHECK(&bounds, !status.code);
                status = gen_save(&oracle->bank, sb, GEN_SNAPSHOT_CAP, &nb); BOUND_CHECK(&bounds, !status.code);
                BOUND_CHECK(&bounds, na == nb && !memcmp(sa, sb, na));
            }
        }
        /* A refused host grid position must not consume a bank tick or
         * pending commands, publish caches, write capture, or emit notes. */
        for (n = 0; n < 6; ++n) {
            fm_voice_t primary_before[3], secondary_before[3];
            gen_frame_t capture, untouched;
            size_t na = 0, nb = 0;
            uint64_t sample, events;
            unsigned code = n == 4 ? GEN_ARGUMENT : n == 5 ? GEN_OVERFLOW : GEN_RANGE;
            BOUND_CHECK(&bounds, !tl_gen_init_status(tl, actual).code);
            for (i = 0; i < 3; ++i) {
                fm_voice_init(&got[i], &tl->patch[0].p, fs);
                fm_voice_init(&expected[i], &tl->patch[0].p, fs);
                fm_voice_gen(&got[i], 0, (int32_t)(123 + i));
                fm_voice_gen(&expected[i], 7, -(int32_t)(456 + i));
            }
            if (n == 1 || n == 2) BOUND_CHECK(&bounds, !tl_gen_step_status(tl, actual, 0, got, expected, NULL).code);
            BOUND_CHECK(&bounds, !gen_reset(&actual->bank, 15, 765).code);
            BOUND_CHECK(&bounds, !gen_lane_enable(&actual->bank, 1, 0).code);
            sample = n == 1 ? 0 : n == 2 ? FM_CTL + 1u : n == 3 ? UINT64_MAX : FM_CTL;
            if (n == 4) actual->bank.ctl = 0; /* exercise the core error precedence */
            if (n == 5) actual->bank.tick = UINT64_MAX;
            for (i = 0; i < 3; ++i) { primary_before[i] = got[i]; secondary_before[i] = expected[i]; }
            memset(&capture, 0xa5, sizeof capture); untouched = capture; events = actual->events;
            BOUND_CHECK(&bounds, !gen_save(&actual->bank, sa, GEN_SNAPSHOT_CAP, &na).code);
            status = tl_gen_step_status(tl, actual, sample, got, expected, &capture);
            BOUND_CHECK(&bounds, status.code == code && status.id == 0 && status.field == GEN_FIELD_CLOCK);
            BOUND_CHECK(&bounds, !gen_save(&actual->bank, sb, GEN_SNAPSHOT_CAP, &nb).code);
            BOUND_CHECK(&bounds, na == nb && !memcmp(sa, sb, na));
            BOUND_CHECK(&bounds, !memcmp(&capture, &untouched, sizeof capture) && actual->events == events);
            for (i = 0; i < 3; ++i) {
                BOUND_CHECK(&bounds, mod_state_equal(&got[i], &primary_before[i]));
                BOUND_CHECK(&bounds, mod_state_equal(&expected[i], &secondary_before[i]));
            }
            if (n < 4) {
                gen_frame_t direct;
                BOUND_CHECK(&bounds, !gen_restore(&oracle->bank, &tl->gen, &oracle->memory, sa, na).code);
                BOUND_CHECK(&bounds, !gen_tick(&oracle->bank, &direct).code);
                BOUND_CHECK(&bounds, !tl_gen_step_status(tl, actual, direct.frame, got, expected, &capture).code);
                BOUND_CHECK(&bounds, !gen_save(&actual->bank, sa, GEN_SNAPSHOT_CAP, &na).code);
                BOUND_CHECK(&bounds, !gen_save(&oracle->bank, sb, GEN_SNAPSHOT_CAP, &nb).code);
                BOUND_CHECK(&bounds, na == nb && !memcmp(sa, sb, na));
            }
        }
        free(actual); free(sa); free(sb);
    }
    /* Exact timestamp round-trip, including the final supported frame.
     * The checked helper also refuses the unrepresentable uint64 extreme. */
    for (i = 0; i < 8192; ++i) {
        BOUND_CHECK(&bounds, !tl_gen_time(i, fs, &timestamp));
        BOUND_CHECK(&bounds, timestamp * fs / UINT64_C(1000000000) == i);
        if (timestamp) BOUND_CHECK(&bounds, (timestamp - 1u) * fs / UINT64_C(1000000000) < i);
    }
    BOUND_CHECK(&bounds, !tl_gen_time(UINT32_MAX, fs, &timestamp));
    BOUND_CHECK(&bounds, timestamp * fs / UINT64_C(1000000000) == UINT32_MAX);
    BOUND_CHECK(&bounds, tl_gen_time(UINT64_MAX, 1, &timestamp));
    BOUND_CHECK(&bounds, tl_gen_time(1, 0, &timestamp));
    /* Rational octave pins include the only exact half-mHz tie in range. */
    { const int32_t cents[] = { -1500, 900, 2100, 3300, 4500, 5700, 6900, 8100, 9300, 10500, 11700, 12900, 14100 };
      const uint32_t frequency[] = { 3438, 13750, 27500, 55000, 110000, 220000, 440000, 880000, 1760000, 3520000, 7040000, 14080000, 28160000 };
      for (n = 0; n < sizeof cents / sizeof *cents; ++n) BOUND_CHECK(&bounds, tl_gen_millihz(cents[n]) == frequency[n]); }
    free(oracle); free(tl);
    printf("GENADAPTER fs=%u phase64=%d checks=%u failures=%u\n", fs, FM_PHASE64, bounds.checks, bounds.bad);
    return bounds.bad;
}

static int gen_events(char **files, int nf)
{
    tl_t *tl = (tl_t *)calloc(1, sizeof *tl);
    tl_gen_runtime_t *r = NULL;
    uint64_t pos = 0, next_tick = 0;
    uint32_t e = 0;
    int k, rc = 0;
    if (!tl) return tl_host_error(NULL, 0, "out of memory");
    for (k = 0; k < nf && !rc; ++k) rc = tl_read(tl, files[k], 1);
    if (!rc) rc = tl_check_header(tl);
    for (k = 0; k < nf && !rc; ++k) rc = tl_read(tl, files[k], 2);
    if (!rc) rc = tl_check_decodable(tl);
    if (!rc && tl->nev) qsort(tl->ev, tl->nev, sizeof *tl->ev, tl_cmp);
    if (!rc && tl->gen_clock) {
        r = (tl_gen_runtime_t *)malloc(sizeof *r);
        if (!r) rc = tl_host_error(NULL, 0, "out of memory");
        else rc = tl_gen_init(tl, r);
    }
    if (!rc) printf("GEN_EVENTS version=1 fs=%u ctl=%u\n", tl->fs, tl->gen_clock ? tl->gen_clock : FM_CTL);
    while (r && !rc && pos < tl->n) {
        while (e < tl->nev && tl->ev[e].sample == pos && !rc) rc = tl_gen_apply(r, &tl->ev[e++]);
        if (!rc && pos == next_tick) { rc = tl_gen_step(tl, r, pos, NULL, NULL, stdout); next_tick += tl->gen_clock; }
        pos = next_tick;
        if (e < tl->nev && pos > tl->ev[e].sample) pos = tl->ev[e].sample;
    }
    if (!rc) printf("GEN_EVENTS count=%llu\n", (unsigned long long)(r ? r->events : 0));
    if (!rc && ferror(stdout)) rc = tl_host_error(NULL, 0, "cannot write generated event manifest");
    free(r); free(tl->ev); free(tl->ks_pool); free(tl); return rc;
}

/* The streaming audit accepts mono/no-effects sessions to keep storage
 * bounded independently of duration. Ordinary --play supports all routes. */
static int gen_soak(char **files, int nf, const char *merged)
{
    tl_t *tl = (tl_t *)calloc(1, sizeof *tl);
    tl_gen_audit_t *a = (tl_gen_audit_t *)malloc(sizeof *a);
    fm_voice_t *vs = NULL;
    uint32_t i, clipped = 0;
    int k, rc = 0;
    if (!tl || !a) { free(tl); free(a); return tl_host_error(NULL, 0, "out of memory"); }
    for (k = 0; k < nf && !rc; ++k) rc = tl_read(tl, files[k], 1);
    if (!rc) rc = tl_check_header(tl);
    for (k = 0; k < nf && !rc; ++k) rc = tl_read(tl, files[k], 2);
    if (!rc) rc = tl_check_decodable(tl);
    if (!rc && (!tl->gen_clock || tl->space || tl->fx.on || tl->route)) rc = tl_err(NULL, 0, "--gen-soak needs a mono generator session without effects");
    if (!rc) {
        if (tl->nev) qsort(tl->ev, tl->nev, sizeof *tl->ev, tl_cmp);
        vs = (fm_voice_t *)alloc_n(tl->nvoices, sizeof *vs);
        if (!vs) rc = tl_host_error(NULL, 0, "out of memory");
    }
    if (!rc) {
        for (i = 0; i < tl->nvoices; ++i) (void)tl_voice_init(tl, &vs[i], i);
        tl_gen_audit_init(a);
        clipped = tl_gen_render_mode(tl, vs, NULL, NULL, NULL, 0, a, NULL);
        rc = tl->render_error;
        if (!rc) {
            for (i = 0; i < tl->gen.sources; ++i) {
                const unsigned id = tl->gen.source[i].id;
                printf("GENSOAK_SOURCE id=%u kind=%u updates=%llu x_min=%d x_max=%d y_min=%d y_max=%d counter=%llu\n",
                    id, tl->gen.source[i].kind, (unsigned long long)a->updates[id], a->lo[id][0], a->hi[id][0], a->lo[id][1], a->hi[id][1], (unsigned long long)a->count[id]);
            }
            printf("GENSOAK fs=%u samples=%u sources=%u voices=%u ticks=%llu events=%llu ons=%llu offs=%llu resting_lane_ticks=%llu retriggers=%llu resets=%u snapshots=%llu snapshot_bytes=%llu hash=%016llx clipped=%u peak=%u failures=%u timer=process_cpu clocks_per_second=%lu tick_mean_us=%.6f tick_max_us=%.6f init_us=%.6f restore_max_us=%.6f pcm_bytes=%lu\n",
                tl->fs, tl->n, tl->gen.sources, tl->nvoices, (unsigned long long)a->ticks,
                (unsigned long long)(a->ons + a->offs), (unsigned long long)a->ons, (unsigned long long)a->offs,
                (unsigned long long)a->rests, (unsigned long long)a->retriggers, a->resets,
                (unsigned long long)a->snapshot_checks, (unsigned long long)a->snapshot_bytes,
                (unsigned long long)a->hash, clipped, a->peak, a->failures, (unsigned long)CLOCKS_PER_SEC,
                a->ticks ? (double)a->tick_total * 1e6 / CLOCKS_PER_SEC / (double)a->ticks : 0,
                (double)a->tick_max * 1e6 / CLOCKS_PER_SEC, (double)a->init_time * 1e6 / CLOCKS_PER_SEC,
                (double)a->restore_max * 1e6 / CLOCKS_PER_SEC, (unsigned long)sizeof a->pcm);
            if (clipped || a->failures) rc = 1;
            else if (tl->have_hash && tl->hash != a->hash) rc = 3;
            if (!rc && merged) rc = tl_write(tl, merged, a->hash);
        }
    }
    free(vs); free(tl->ev); free(tl->ks_pool); free(tl); free(a); return rc;
}

/* ---- --play --metrics and --ref (PLAN  step 1, ) ---- */

/* floor(sqrt(x)) for any uint64_t x, digit by digit. */
static uint64_t isqrt_u64(uint64_t x)
{
    uint64_t r = 0, b = UINT64_C(1) << 62;
    while (b > x) b >>= 2;
    while (b) {
        if (x >= r + b) { x -= r + b; r = (r >> 1) + b; }
        else r >>= 1;
        b >>= 2;
    }
    return r;
}

/* --metrics: the integer peak, RMS and crest of n samples, every step-th
 * from pcm, in integer arithmetic alone, so that every target prints the
 * same digits. rms_milli is floor(1000 sqrt(ss / n)), thousandths of an
 * LSB, ss the samples' sum of squares; crest_milli is floor(1000 peak /
 * rms), from floor(10^6 peak^2 n / ss) (below 2^82, fp_math.h's wide
 * helpers), -1 when every sample is 0. floor(sqrt(floor(x))) is
 * floor(sqrt(x)), so both are exact. n * step is at most 2^32 - 1. */
typedef struct { uint32_t peak; uint64_t rms_milli; int64_t crest_milli; } pm_t;

static void pm_measure(const int16_t *pcm, uint32_t n, uint32_t step, pm_t *m)
{
    uint64_t ss = 0;
    uint32_t i, peak = 0;
    for (i = 0; i < n; i++) {
        const int32_t x = pcm[(size_t)i * step];
        const uint32_t a = (uint32_t)(x < 0 ? -x : x);
        if (a > peak) peak = a;
        ss += (uint64_t)a * a;                       /* at most 2^30 per sample, below 2^62 in all */
    }
    m->peak = peak;
    m->rms_milli = n ? isqrt_u64(ss / n * 1000000u + ss % n * 1000000u / n) : 0u;
    m->crest_milli = -1;
    if (ss) m->crest_milli = (int64_t)isqrt_u64((uint64_t)fp_w_to_i64(fp_w_divs(
        fp_w_mul(fp_w_from_u64((uint64_t)peak * peak * 1000000u), fp_w_from_u64(n)), fp_w_from_u64(ss))));
}

static void pm_print_milli(int64_t v)
{
    if (v < 0) printf("-");
    else printf("%llu.%03llu", (unsigned long long)(v / 1000), (unsigned long long)(v % 1000));
}

/* The METRICS line: peak, rms and crest over all ns samples (the PLAY
 * line's peak and rms_dbfs are over the same), then, on ch > 1 channels,
 * the same per channel in channel order (: a stereo record's L/R
 * levels). */
static void pm_print(const int16_t *pcm, uint32_t ns, uint32_t ch)
{
    pm_t m;
    uint32_t c;
    pm_measure(pcm, ns, 1u, &m);
    printf("METRICS peak=%u rms=", m.peak);
    pm_print_milli((int64_t)m.rms_milli);
    printf(" crest=");
    pm_print_milli(m.crest_milli);
    if (ch > 1) {
        static const char *const key[3] = { " peak_ch=", " rms_ch=", " crest_ch=" };
        int k;
        for (k = 0; k < 3; k++) {
            printf("%s", key[k]);
            for (c = 0; c < ch; c++) {
                pm_measure(pcm + c, ns / ch, ch, &m);
                if (c) putchar(',');
                if (k == 0) printf("%u", m.peak);
                else pm_print_milli(k == 1 ? (int64_t)m.rms_milli : m.crest_milli);
            }
        }
    }
    putchar('\n');
}

/* --ref: 's comparison of a mono record's output pcm with the double
 * reference (ref_double.c). Each voice runs the cases' lockstep
 * (ref_lockstep: an engine twin of voice set 1 beside the reference voice,
 * for 's and 's count rules and the increments' synchronisation)
 * through its own on, off, pitch, filter and ctl events in the record's
 * order; pos and move events, carried and not applied on the mono path,
 * are skipped. The voices' reference outputs add up, as fm_mix_i16 adds
 * their Q2.30 samples before its one rounding, and pcm is measured against
 * the sum (d14_measure). Prints the REF line, RESULT's names and the
 * verdict on 's criteria as --check applies them (loss <= 0.10 dB,
 * peak error <= 1 LSB, the end test, no count failure; the SNR floors are
 * five named cases' own, ), and returns the number of criteria that
 * fail, or -1 when out of memory. Diagnostic lines (ADOPT, COUNT-FAIL and
 * the like) name the patch, with /vN on a record of more voices. */
static int play_ref(const tl_t *tl, const int16_t *pcm)
{
    const size_t ne_max = tl->nev ? tl->nev : 1u;
    double *sum = (double *)calloc(tl->n ? tl->n : 1u, sizeof *sum);
    double *one = (double *)alloc_n(tl->n ? tl->n : 1u, sizeof *one);
    double *store = (double *)alloc_n((size_t)FM_OPS * FM_KS_BUFFER_MAX, sizeof *store);
    spike_event_t *ev = (spike_event_t *)alloc_n(ne_max, sizeof *ev);
    uint32_t *at = (uint32_t *)alloc_n(ne_max, sizeof *at);
    unsigned adopted = 0, count_fail = 0, fails = 0;
    uint32_t vi, e, i;
    d14_t m;
    int rc = -1;
    if (!sum || !one || !store || !ev || !at) goto ref_cleanup;
    for (vi = 0; vi < tl->nvoices; vi++) {
        const tl_patch_t *p = &tl->patch[tl->voice[vi]];
        const fm_assets_t *as = tl->ks_pool ? &tl->ks_assets[1][vi] : tl->wt.assets.wt_count ? &tl->wt.assets : NULL;
        char name[TL_NAME + 8];
        ref_voice_t rv;
        fm_voice_t av;
        lfo_stat_t st;
        unsigned ad, cf;
        int ne = 0;
        for (e = 0; e < tl->nev; e++) {
            const tl_event_t *x = &tl->ev[e];
            if (x->voice != vi || (x->type != EV_ON && x->type != EV_OFF && x->type != EV_PITCH
                                   && x->type != EV_FILTER && x->type != EV_CTL)) continue;
            ev[ne].t_ms = (uint32_t)(x->t_ns / 1000000u);   /* labels the diagnostic lines only */
            ev[ne].type = x->type;
            ev[ne].freq_millihz = x->freq_millihz;
            ev[ne].velocity_q15 = x->velocity_q15;
            ev[ne].ctl_value = x->ctl_value;
            at[ne++] = x->sample < tl->n ? x->sample : tl->n;
        }
        if (tl->nvoices == 1) snprintf(name, sizeof name, "%s", p->name);
        else snprintf(name, sizeof name, "%s/v%u", p->name, (unsigned)vi);
        ref_voice_init(&rv, &p->p, tl->fs);
        ref_voice_bind_assets(&rv, as);
        ref_voice_bind_ks(&rv, as, store);
        if (tl_voice_init_set(tl, &av, vi, 1u)) goto ref_cleanup;
        ref_lockstep(name, &p->p, &rv, &av, tl->fs, ev, at, ne, one, tl->n, &ad, &cf, &st);
        adopted += ad;
        count_fail += cf;
        for (i = 0; i < tl->n; i++) sum[i] += one[i];
    }
    d14_measure(pcm, sum, tl->n, NULL, &m);
    printf("REF snr_db=%.2f ceiling_db=%.2f loss_db=%.2f rms_err_lsb=%.4f peak_err_lsb=%.2f@%u tail_snr_db=%.2f tail_peak_lsb=%.2f a1_end=%u ref_end=%u end_range=%u..%u band_n=%u spurious=%u end_ok=%s adopted=%u count_fail=%u d14=",
           m.snr, m.ceiling, m.loss, tl->n ? sqrt(m.err / tl->n) : 0.0, m.peak, m.peak_i, db(m.tsig, m.terr), m.tpeak,
           m.a1_end, m.ref_end, m.end_lo, m.end_hi, m.band_n, m.spurious, m.end_ok ? "yes" : "NO", adopted, count_fail);
    if (!(m.loss <= 0.10)) printf("%sloss", fails++ ? "," : "FAIL:");   /* unrounded, as --check: a NaN fails */
    if (!(m.peak <= 1.0)) printf("%speak", fails++ ? "," : "FAIL:");
    if (!m.end_ok) printf("%send", fails++ ? "," : "FAIL:");
    if (count_fail) printf("%scount", fails++ ? "," : "FAIL:");
    printf("%s\n", fails ? "" : "ok");
    rc = (int)fails;
ref_cleanup:
    free(sum); free(one); free(store); free(ev); free(at);
    return rc;
}

static int fx_diff(char **files, int nf);
static int fx_diff_input(char **files, int nf, const char *stems);

#ifndef FM_PCM_RESULT
#define FM_PCM_RESULT(pcm, samples, rate, channels) ((void)0)
#endif

/* --play: reads the files as one pool, renders twice (block_indep), prints
 * the PLAY line, writes the WAV and the canonical record if asked. A piece
 * supplies its session.tl and its stream files. --play-once keeps only the
 * first render and reports block_indep=unchecked. Exit 0,
 * 1 on a bad timeline or a block-size dependence, 3 when the output's hash
 * is not the timeline's hash statement. With metrics, a METRICS line
 * follows the PLAY line (pm_print). With ref, 's comparison: a REF
 * line (play_ref) on a mono record, --fxref's FXREF lines (fx_diff) on a
 * record with effects, exit 4 when a criterion fails and nothing else
 * does; a stereo record, a field route or a generator session without
 * effects, which no double model covers, exits 2 before rendering. */
static int play(char **files, int nf, const char *wav, const char *merged, const char *stems, int metrics, int ref, int partition,
                int wav_fd, uint64_t pcm_limit, const char *stems_out, int stems_fd, int once)
{
    tl_t *tl = (tl_t *)calloc(1, sizeof *tl);
    fm_voice_t *vs = NULL;
    sp_stereo_t *sp = NULL;
    fx_t *fx = NULL;
    int16_t *pcm = NULL, *pcm1 = NULL;
    uint64_t h = 0, h1 = 0;
    uint32_t i, ns = 0, ch = 1, clipped, clipped1 = 0, peak = 0;
    double ss = 0;
    int k, rc = 0, fxr = 0;
    if (!tl) { fprintf(stderr, "oom\n"); rc = HOST_ERROR; goto play_cleanup; }
    for (k = 0; k < nf && !rc; k++) rc = tl_read(tl, files[k], 1);
    if (!rc) rc = tl_check_header(tl);
    for (k = 0; k < nf && !rc; k++) rc = tl_read(tl, files[k], 2);
    if (!rc) rc = tl_check_decodable(tl);
    if (!rc && stems) rc = tl_load_stems(tl, stems);
    if (!rc && ref && !tl->fx.on && (tl->space || tl->route || tl->gen_clock)) {
        fprintf(stderr, "--ref: no double model covers %s without effects; it compares mono records and, as --fxref, records with effects\n",
                tl->route ? "a field route" : tl->space ? "a stereo record" : "a generator session");
        rc = 2;
    }
    if (!rc && (wav_fd >= 0 || stems_out || stems_fd >= 0)) {
        const uint32_t channels = tl->route ? tl_field_ch(tl) : tl->space ? 2u : 1u;
        /* Header validation already bounds n*channels by UINT32_MAX.
         * Widen before multiplication; admission precedes both PCM arrays. */
        const uint64_t bytes = (uint64_t)tl->n * channels * 2u;
        if (bytes > pcm_limit) {
            fprintf(stderr, "--pcm-limit: required=%llu allowed=%llu bytes\n",
                    (unsigned long long)bytes, (unsigned long long)pcm_limit);
            rc = 2;
        } else if (wav_fd >= 0 && (bytes > UINT32_MAX - 36u || tl->fs > UINT32_MAX / (2u * channels))) {
            fprintf(stderr, "--wav-fd: record exceeds PCM16 RIFF dimensions\n");
            rc = 2;
        }
    }
    if (!rc && (stems_out || stems_fd >= 0)) {
        const uint64_t bytes = (uint64_t)tl->n * tl->nvoices * 4u;
        if (bytes > pcm_limit || bytes > UINT32_MAX - 36u
            || (uint64_t)tl->fs * tl->nvoices * 4u > UINT32_MAX
            || (uint64_t)tl->n * (tl->route ? tl_field_ch(tl) : tl->space ? 2u : 1u) > SIZE_MAX / sizeof *pcm) {
            fprintf(stderr, "--stems-out/--stems-fd: Q30/mix dimensions or byte limit exceeded (dry=%llu limit=%llu)\n",
                    (unsigned long long)bytes, (unsigned long long)pcm_limit);
            rc = 2;
        }
        if (!rc) rc = stem_paths_check(stems_out, merged, files, nf);
    }
    if (!rc) {
        if (tl->nev) qsort(tl->ev, tl->nev, sizeof *tl->ev, tl_cmp);
        if (tl->route) { ch = tl_field_ch(tl); sp = (sp_stereo_t *)calloc(1, sizeof(sp_field_t)); }
        else if (tl->space) { ch = 2; sp = (sp_stereo_t *)calloc(1, sizeof *sp); }
        if (tl->fx.on) fx = (fx_t *)calloc(1, sizeof *fx + tl_fx_mem_bytes(tl));   /* the state, then its memory */
        ns   = tl->n * ch;                                   /* stereo: n at most 2^31 - 1 (tl_check_header) */
        vs   = (fm_voice_t *)alloc_n(tl->nvoices, sizeof *vs);
        pcm  = (int16_t *)alloc_n(ns, sizeof *pcm);
        if (!once) pcm1 = (int16_t *)alloc_n(ns, sizeof *pcm1);
        if (!vs || !pcm || (!once && !pcm1) || (ch > 1 && !sp) || (tl->fx.on && !fx)) { fprintf(stderr, "oom\n"); rc = HOST_ERROR; }
        else if (fx && tl_fx_init(tl, fx)) rc = tl_err(NULL, 0, "the effects' configuration is refused");
    }
    if (!rc) {
        clipped  = tl_render(tl, vs, sp, fx, pcm, 0);
        if (tl->render_error) { rc = tl->render_error; goto play_cleanup; }
        h        = fm_golden_hash(pcm, ns);
        if (!once) {
            clipped1 = tl_render(tl, vs, sp, fx, pcm1, partition);
            if (tl->render_error) { rc = tl->render_error; goto play_cleanup; }
            h1 = fm_golden_hash(pcm1, ns);
        }
        for (i = 0; i < ns; i++) {
            uint32_t a = (uint32_t)(pcm[i] < 0 ? -pcm[i] : pcm[i]);
            if (a > peak) peak = a;
            ss += (double)pcm[i] * (double)pcm[i];
        }
        printf("PLAY hash=%016llx expected=", (unsigned long long)h);
        if (tl->have_hash) printf("%016llx match=%s", (unsigned long long)tl->hash, tl->hash == h ? "yes" : "NO");
        else printf("- match=-");
        printf(" block_indep=%s clipped=%u peak=%u peak_dbfs=", once ? "unchecked" : h == h1 && clipped == clipped1 ? "yes" : "NO", clipped, peak);
        if (peak) printf("%.2f", 20.0 * log10(peak / 32768.0)); else printf("-inf");
        printf(" rms_dbfs=");
        if (ss > 0) printf("%.2f", 10.0 * log10(ss / ns / (32768.0 * 32768.0))); else printf("-inf");
        printf(" fs=%u samples=%u events=%u voices=%u streams=%u", tl->fs, tl->n, tl->nev, tl->nvoices, tl->nstreams);
        /* channels= stays last; check.sh matches this key. Insert new keys before it. */
        if (ch > 1) printf(" channels=%u", ch);
        putchar('\n');
        if (!tl->nev && !tl->gen_lanes && !peak)
            fprintf(stderr, "harness: %u streams declared and no events: a piece is a directory; pass DIR/session.tl and DIR/*.tl (tools/music/play.sh)\n", tl->nstreams);
        if (metrics) pm_print(pcm, ns, ch);
        if (!once && (h != h1 || clipped != clipped1)) rc = 1;
        if (wav) rc = exit_merge(rc, write_wav_ch(wav, pcm, ns, tl->fs, ch));
        if (wav_fd >= 0) {
            const int fd = wav_fd;
            wav_fd = -1;
            rc = exit_merge(rc, write_wav_fd(fd, pcm, ns, tl->fs, ch));
        }
        if (stems_out || stems_fd >= 0) {
            /* Publish completed mix evidence before dry back-pressure can
             * block this child. Legacy no-stems buffering stays unchanged. */
            if (fflush(stdout)) rc = exit_merge(rc, io_fail("stdout", "write PLAY"));
            else {
                const int fd = stems_fd;
                stems_fd = -1;
                rc = exit_merge(rc, tl_write_stems(tl, vs, stems_out, fd, partition));
            }
        }
        if (merged) rc = exit_merge(rc, tl_write(tl, merged, h));
        if (!rc && tl->have_hash && tl->hash != h) rc = 3;
        if (ref && tl->fx.on) {
            fxr = rc == 0 || rc == 3;
            /* P275: completed PLAY must survive cancellation in the extra
             * field reference. Keep ordinary mono/stereo buffering unchanged. */
            if (fxr && tl->route && fflush(stdout)) {
                rc = exit_merge(rc, io_fail("stdout", "flush")); fxr = 0;
            }
        }
        else if (ref && rc != HOST_ERROR) {
            const int fails = play_ref(tl, pcm);
            if (fails < 0) { fprintf(stderr, "oom\n"); rc = HOST_ERROR; }
            else if (fails && !rc) rc = 4;
        }
        FM_PCM_RESULT(pcm, ns, tl->fs, ch);
    }
play_cleanup:
    if (stems_fd >= 0 && close(stems_fd)) rc = exit_merge(rc, io_fail("--stems-fd", "close"));
    if (wav_fd >= 0 && close(wav_fd)) rc = exit_merge(rc, io_fail("--wav-fd", "close"));
    if (tl) { free(tl->ev); free(tl->stems); free(tl->ks_pool); }
    free(tl); free(vs); free(sp); free(fx); free(pcm); free(pcm1);
    if (fxr) {                                               /* --fxref's own read and renders, after these are freed */
        const int r = fx_diff_input(files, nf, stems);
        rc = exit_merge(rc, r);
    }
    return rc;
}

/* Host-side Keys derivation. The native reader, generator and writer remain
 * authoritative; no profile interpretation enters the shipped audio library. */
typedef struct { uint64_t hash; uint32_t clipped, peak; double peak_dbfs; } keys_measure_t;

static int keys_measure(tl_t *tl, keys_measure_t *out)
{
    const uint32_t ch = tl->route ? tl_field_ch(tl) : tl->space ? 2u : 1u;
    uint32_t ns;
    if ((uint64_t)tl->n * ch > UINT32_MAX || (uint64_t)tl->n * ch > SIZE_MAX / sizeof(int16_t))
        return tl_err(NULL, 0, "Keys PCM dimensions exceed the host allocation range");
    ns = tl->n * ch;
    fm_voice_t *voices = (fm_voice_t *)alloc_n(tl->nvoices, sizeof *voices);
    sp_stereo_t *sp = ch > 1 ? (sp_stereo_t *)calloc(1, tl->route ? sizeof(sp_field_t) : sizeof *sp) : NULL;
    fx_t *fx = tl->fx.on ? (fx_t *)calloc(1, sizeof *fx + tl_fx_mem_bytes(tl)) : NULL;
    int16_t *pcm = (int16_t *)alloc_n(ns, sizeof *pcm);
    uint32_t pass, i, clipped;
    int rc = 0;
    memset(out, 0, sizeof *out);
    if (!voices || !pcm || (ch > 1 && !sp) || (tl->fx.on && !fx)) {
        rc = tl_host_error(NULL, 0, "out of memory"); goto done;
    }
    if (FM_HOST_CANCELLED()) { rc = 6; goto done; }
    if (fx && tl_fx_init(tl, fx)) { rc = tl_err(NULL, 0, "Keys effects configuration is refused"); goto done; }
    for (pass = 0; pass < 2u; ++pass) {
        uint64_t hash;
        clipped = tl_render(tl, voices, sp, fx, pcm, (int)pass);
        if (tl->render_error) { rc = tl->render_error; goto done; }
        hash = fm_golden_hash(pcm, ns);
        if (!pass) { out->hash = hash; out->clipped = clipped; }
        else if (hash != out->hash || clipped != out->clipped) {
            rc = tl_err(NULL, 0, "Keys render depends on block partition"); goto done;
        }
    }
    for (i = 0; i < ns; ++i) {
        const uint32_t a = (uint32_t)(pcm[i] < 0 ? -pcm[i] : pcm[i]);
        if (!(i & 255u) && FM_HOST_CANCELLED()) { rc = 6; goto done; }
        if (a > out->peak) out->peak = a;
    }
    /* Match the existing host converter's two-decimal PLAY measurement. */
    out->peak_dbfs = out->peak ? nearbyint(2000.0 * log10(out->peak / 32768.0)) / 100.0 : -INFINITY;
done:
    free(voices); free(sp); free(fx); free(pcm);
    return rc;
}

static int keys_append(tl_event_t **events, uint32_t *count, uint32_t *capacity, tl_event_t event)
{
    if (*count == *capacity) {
        const uint32_t next = *capacity ? *capacity * 2u : 256u;
        tl_event_t *grown;
        if (next <= *capacity) return tl_err(NULL, 0, "too many Keys positions");
        grown = (tl_event_t *)realloc_n(*events, next, sizeof *grown);
        if (!grown) return tl_host_error(NULL, 0, "out of memory");
        *events = grown; *capacity = next;
    }
    event.order = *count;
    (*events)[(*count)++] = event;
    return 0;
}

static int keys_position(tl_event_t *position, uint32_t frequency, int half_width, int mirror)
{
    double place, angle;
    if (!frequency) return tl_err(NULL, 0, "Keys placement requires a positive note-on frequency");
    place = (69.0 + 12.0 * log2(frequency / 440000.0) - 60.0) / 32.0;
    if (place < -1.0) place = -1.0;
    if (place > 1.0) place = 1.0;
    angle = (mirror ? -1.0 : 1.0) * half_width * place * (TWO_PI / 360.0);
    position->type = EV_POS;
    position->x = (int32_t)nearbyint(1000.0 * sin(angle));
    position->y = (int32_t)nearbyint(1000.0 * cos(angle));
    position->z = 0;
    return 0;
}

static int keys_project(tl_t *tl, int half_width, int mirror, uint32_t *manual, uint32_t *generated)
{
    tl_event_t *events = NULL;
    uint32_t count = 0, capacity = 0, i, e = 0;
    tl_gen_runtime_t *runtime = NULL;
    uint64_t pos = 0, next_tick = 0;
    int rc = 0;
    *manual = *generated = 0;
    for (i = 0; i < tl->nev && !rc; ++i) {
        const tl_event_t original = tl->ev[i];
        if (FM_HOST_CANCELLED()) { rc = 6; break; }
        if (original.type == EV_POS || original.type == EV_MOVE || original.type == EV_YAW) continue;
        if (original.type == EV_ON) {
            tl_event_t position = original;
            rc = keys_position(&position, original.freq_millihz, half_width, mirror);
            if (!rc) rc = keys_append(&events, &count, &capacity, position);
            ++*manual;
        }
        if (!rc) rc = keys_append(&events, &count, &capacity, original);
    }
    if (!rc && FM_HOST_CANCELLED()) rc = 6;
    if (!rc && tl->gen_clock) {
        runtime = (tl_gen_runtime_t *)malloc(sizeof *runtime);
        if (!runtime) rc = tl_host_error(NULL, 0, "out of memory");
        else rc = tl_gen_init(tl, runtime);
    }
    /* Traverse the unchanged native schedule. Positions are explicit events in
     * the first stream; generator programs, lanes and controls are retained. */
    while (runtime && !rc && pos < tl->n) {
        if (FM_HOST_CANCELLED()) { rc = 6; break; }
        while (e < tl->nev && tl->ev[e].sample == pos && !rc) rc = tl_gen_apply(runtime, &tl->ev[e++]);
        if (!rc && pos == next_tick) {
            gen_frame_t frame;
            const gen_status_t status = tl_gen_step_status(tl, runtime, pos, NULL, NULL, &frame);
            if (status.code) { rc = tl_err(NULL, 0, "Keys generator tick refused"); break; }
            for (i = 0; i < frame.events && !rc; ++i) if (frame.event[i].on) {
                const gen_event_t *on = &frame.event[i];
                tl_event_t position;
                fp_w128 roundtrip;
                memset(&position, 0, sizeof position);
                if (tl_gen_time(pos, tl->fs, &position.t_ns) || position.t_ns >= tl->length_ns) {
                    rc = tl_err(NULL, 0, "Keys generator time is unrepresentable"); break;
                }
                roundtrip = fp_w_divs(fp_w_mul(fp_w_from_u64(position.t_ns), fp_w_from_u64(tl->fs)), fp_w_from_u64(UINT64_C(1000000000)));
                if (fp_w_cmp(roundtrip, fp_w_from_u64(pos))) {
                    rc = tl_err(NULL, 0, "Keys generator frame has no exact nanosecond timestamp"); break;
                }
                position.sample = (uint32_t)pos; position.voice = on->voice;
                rc = keys_position(&position, tl_gen_millihz(on->cents), half_width, mirror);
                if (!rc) rc = keys_append(&events, &count, &capacity, position);
                ++*generated;
            }
            next_tick += tl->gen_clock;
        }
        pos = next_tick;
        if (e < tl->nev && pos > tl->ev[e].sample) pos = tl->ev[e].sample;
    }
    free(runtime);
    if (rc) { free(events); return rc; }
    free(tl->ev); tl->ev = events; tl->nev = count; tl->cap = capacity;
    if (count) qsort(events, count, sizeof *events, tl_cmp);
    tl->draft = 2; tl->space = tl->have_space = 1; tl->route = 0;
    tl->have_order = tl->have_pinna = tl->have_yaw = tl->have_panlaw = 0;
    tl->order = tl->panlaw = 0; tl->pinna = 0; tl->yaw = 0;
    tl->have_hash = 0; tl->hash = 0;
    /* The original header already validated every patch/asset and packed the
     * immutable generator program. Do not run that destructive packing twice.
     * Only output routing changed; retain assets and update the FX geometry. */
    tl->fx.channels = 2u; tl->fx.block = tl->block;
    rc = tl_check_field(tl);
    return rc ? rc : tl_check_decodable(tl);
}

static void keys_level_bounds(const tl_t *tl, int *low, int *high)
{
    uint32_t p, op;
    *low = -1200; *high = 1200;
    for (p = 0; p < tl->npatches; ++p) {
        const fm_patch_t *patch = &tl->patch[p].p;
        const unsigned carriers = patch->route.ops ? patch->route.carriers : 1u;
        for (op = 0; op < FM_OPS; ++op) if ((carriers >> op) & 1u) {
            const int level = patch->op[op].level_db10;
            if (-level > *low) *low = -level;
            if (1200 - level < *high) *high = 1200 - level;
        }
    }
}

static int keys_levels(tl_t *tl, int requested, int bounded, int *applied, int *at_lower)
{
    uint32_t p, op;
    int low, high;
    keys_level_bounds(tl, &low, &high);
    *at_lower = low == 0;
    *applied = requested;
    if (requested < low || requested > high) {
        if (!bounded) return tl_err(NULL, 0, "Keys carrier shift exceeds 0..1200; explicit carrier-bound policy may retain quieter output");
        *applied = requested < low ? low : high;
    }
    for (p = 0; p < tl->npatches; ++p) {
        fm_patch_t *patch = &tl->patch[p].p;
        const unsigned carriers = patch->route.ops ? patch->route.carriers : 1u;
        for (op = 0; op < FM_OPS; ++op) if ((carriers >> op) & 1u)
            patch->op[op].level_db10 = (uint16_t)((int)patch->op[op].level_db10 + *applied);
    }
    return 0;
}

typedef struct {
    keys_measure_t source, result;
    uint32_t manual, generated, attempt;
    int source_valid, target_met, total_delta, limited, requested, applied;
} keys_result_t;

/* One host transformation shared by the CLI and native clients. The caller
 * owns the validated TL and discards it after failure: projection and carrier
 * adjustment intentionally mutate this private candidate. No file publication
 * or reporting happens here. */
static int keys_transform(tl_t *tl, int half_width, int mirror, int bounded, keys_result_t *out)
{
    uint32_t attempt;
    int rc;
    memset(out, 0, sizeof *out);
    if (FM_HOST_CANCELLED()) return 6;
    /* Projection always makes two channels. Refuse impossible derived storage
     * before allocating or rendering even a still-valid mono original. */
    if ((uint64_t)tl->n * 2u > UINT32_MAX || (uint64_t)tl->n * 2u > SIZE_MAX / sizeof(int16_t)) {
        return tl_err(NULL, 0, "Keys stereo PCM dimensions exceed the host allocation range");
    }
    if (tl->nev) qsort(tl->ev, tl->nev, sizeof *tl->ev, tl_cmp);
    rc = keys_measure(tl, &out->source);
    if (rc) return rc;
    out->source_valid = 1;
    if (tl->have_hash && tl->hash != out->source.hash) return 3;
    rc = keys_project(tl, half_width, mirror, &out->manual, &out->generated);
    if (rc) return rc;
    for (attempt = 0; attempt < 8u; ++attempt) {
        int at_lower;
        if (FM_HOST_CANCELLED()) return 6;
        out->attempt = attempt;
        rc = keys_measure(tl, &out->result);
        if (rc) return rc;
        if (!out->result.peak || !isfinite(out->result.peak_dbfs)) return tl_err(NULL, 0, "Keys refuses silent output");
        out->target_met = !out->result.clipped && fabs(out->result.peak_dbfs + 1.5) <= 0.5;
        if (out->target_met) break;
        out->requested = out->result.clipped ? 15 : (int)nearbyint((out->result.peak_dbfs + 1.5) * 10.0);
        rc = keys_levels(tl, out->requested, bounded, &out->applied, &at_lower);
        if (rc) return rc;
        if (out->requested != out->applied) out->limited = 1;
        if (bounded && !out->result.clipped && out->result.peak_dbfs < -2.0 && out->requested < 0 && at_lower && !out->applied) break;
        if (!out->applied || attempt == 7u) return tl_err(NULL, 0, "Keys levels did not settle in eight passes");
        out->total_delta += out->applied;
    }
    return FM_HOST_CANCELLED() ? 6 : 0;
}

static int keys_profile(char **files, int nf, const char *merged, int half_width, int mirror, int bounded)
{
    tl_t *tl = (tl_t *)calloc(1, sizeof *tl);
    keys_result_t result;
    int k, rc = 0, have_hash;
    uint64_t expected_hash;
    struct stat output;
    if (!tl) return tl_host_error(NULL, 0, "out of memory");
    /* The browser supplies an empty virtual output. Native callers can use a
     * new path or empty file, never a source alias or populated destination. */
    if (!stat(merged, &output)) {
        for (k = 0; k < nf; ++k) {
            struct stat input;
            if (!stat(files[k], &input) && output.st_dev == input.st_dev && output.st_ino == input.st_ino) {
                rc = tl_err(NULL, 0, "Keys output aliases an original input"); goto done;
            }
        }
        if (!S_ISREG(output.st_mode) || output.st_size) { rc = tl_err(NULL, 0, "Keys output must be new or empty"); goto done; }
    } else if (errno != ENOENT) { rc = io_fail(merged, "inspect output"); goto done; }
    for (k = 0; k < nf && !rc; ++k) rc = tl_read(tl, files[k], 1);
    if (!rc) rc = tl_check_header(tl);
    for (k = 0; k < nf && !rc; ++k) rc = tl_read(tl, files[k], 2);
    if (!rc) rc = tl_check_decodable(tl);
    if (rc) goto done;
    have_hash = tl->have_hash; expected_hash = tl->hash;
    rc = keys_transform(tl, half_width, mirror, bounded, &result);
    if (result.source_valid)
        printf("PROFILE_SOURCE hash=%016llx hash_present=%d match=%s block_indep=yes\n",
               (unsigned long long)result.source.hash, have_hash,
               have_hash ? expected_hash == result.source.hash ? "yes" : "NO" : "unchecked");
    if (rc) goto done;
    rc = tl_write(tl, merged, result.result.hash);
    if (!rc) {
        int low, high;
        uint32_t p, op;
        keys_level_bounds(tl, &low, &high);
        printf("PROFILE half_width=%d mirror=%d level_policy=%s target_met=%s peak_dbfs=%.2f clipped=%u carrier_delta_db10=%d passes=%u manual_positions=%u generated_positions=%u hash=%016llx limited_by_carrier_bounds=%d legal_low_delta_db10=%d legal_high_delta_db10=%d last_requested_delta_db10=%d last_applied_delta_db10=%d\n",
            half_width, mirror, bounded ? "carrier-bound" : "strict", result.target_met ? "yes" : "no", result.result.peak_dbfs,
            result.result.clipped, result.total_delta, result.attempt + 1u, result.manual, result.generated, (unsigned long long)result.result.hash,
            result.limited, low, high, result.requested, result.applied);
        if (result.limited && !result.target_met) for (p = 0; p < tl->npatches; ++p) {
            const fm_patch_t *patch = &tl->patch[p].p;
            const unsigned carriers = patch->route.ops ? patch->route.carriers : 1u;
            for (op = 0; op < FM_OPS; ++op) if (((carriers >> op) & 1u) && !patch->op[op].level_db10)
                printf("PROFILE_LIMIT patch=%s operator=%u level_db10=0\n", tl->patch[p].name, op);
        }
    }
done:
    free(tl->ev); free(tl->ks_pool); free(tl);
    return rc;
}

/* ---- --fxref: the effects against their double model (fx/SPEC.md, section 8) ---- */

/* 's loss bound, which every effect meets, the buses' too (fx/SPEC.md,
 * section 8, gives the measured losses). */
#define FX_TOL_LOSS_DB 0.10

typedef struct {
    double sig, err, qerr, peak;
    uint32_t peak_i;
    uint64_t a_end, r_end, end_lo, end_hi, band_n, spurious;
} fxd_t;

/* One output sample a against the model's r, both in int16 units: the
 * error against r, the rounding of r, the peak error against r clipped. */
static void fxd_add(fxd_t *d, int16_t a, double r, uint64_t i)
{
    double e = (double)a - r, q = (double)clip16(nearbyint(r)) - r;
    double ad = fabs((double)a - (r > 32767.0 ? 32767.0 : r < -32768.0 ? -32768.0 : r));
    d->sig += r * r; d->err += e * e; d->qerr += q * q;
    if (ad > d->peak) { d->peak = ad; d->peak_i = (uint32_t)i; }
    if (a) d->a_end = i + 1u;
    if (clip16(floor(r + 0.5))) d->r_end = i + 1u;
    /* : retain the band/spurious counts strictly from the current lower
     * exclusive end onward. A later strong sample supersedes the earlier tail;
     * the strong sample itself is before the new lower bound. */
    if (fabs(r) >= 0.5 + END_EPS) {
        d->end_lo = i + 1u;
        d->band_n = d->spurious = 0;
    } else if (fabs(r) >= 0.5 - END_EPS) ++d->band_n;
    else if (a) ++d->spurious;
    if (fabs(r) >= 0.5 - END_EPS) d->end_hi = i + 1u;
}

static int fxd_end_ok(const fxd_t *d)
{
    return d->a_end >= d->end_lo && d->a_end <= d->end_hi && d->spurious == 0;
}

/* Prints FXREF, raw LASTNZ and complete  FXEND; returns failed criteria. End counts
 * are one-based interleaved samples, at most 16 * UINT32_MAX; zero is silence. */
static unsigned fxd_report(const char *what, const fxd_t *d)
{
    double snr = db(d->sig, d->err), ceiling = db(d->sig, d->qerr), loss = snr > 900 ? 0.0 : ceiling - snr;
    unsigned fail = !(loss <= FX_TOL_LOSS_DB) + !(d->peak <= 1.0) + !fxd_end_ok(d);
    printf("FXREF  %-11s snr_db=%.2f ceiling_db=%.2f loss_db=%.4f peak_err_lsb=%.3f@%u %s\n",
           what, snr, ceiling, loss, d->peak, d->peak_i, fail ? "FAIL" : "ok");
    printf("LASTNZ %-11s output=%llu model=%llu %s\n", what,
           (unsigned long long)d->a_end, (unsigned long long)d->r_end,
           d->a_end == d->r_end ? "equal" : "DIFFER");
    printf("FXEND  %-11s actual=%llu lower=%llu upper=%llu band=%llu spurious=%llu %s\n", what,
           (unsigned long long)d->a_end, (unsigned long long)d->end_lo,
           (unsigned long long)d->end_hi, (unsigned long long)d->band_n,
           (unsigned long long)d->spurious, fxd_end_ok(d) ? "ok" : "FAIL");
    return fail;
}

/* The master alone on nch channels, channel c fed the mono dry sum delayed
 * by c samples and negated on odd c (fx_master's stride and frac 30),
 * against the model. Returns the failed criteria; *rc 5 out of memory. */
static unsigned fxd_channels(const tl_t *tl, const int64_t *dry, uint32_t nch, int *rc)
{
    fx_cfg_t cfg = tl->fx;
    size_t bytes;
    fx_t *fx;
    fxr_t *r;
    int64_t *acc = (int64_t *)alloc_n((size_t)FX_MAX_CH * FX_MAX_BLOCK, sizeof *acc);
    int16_t out[FX_MAX_BLOCK * FX_MAX_CH];
    double dd[FX_MAX_CH], ro[FX_MAX_CH], bus[FX_BUSES] = { 0, 0, 0 };
    fxd_t d;
    char what[16];
    uint32_t i, t, c, len;
    unsigned fail = 0;
    cfg.channels = nch;
    bytes = fx_mem_bytes(&cfg, tl->fs);
    fx = (fx_t *)calloc(1, sizeof *fx + bytes);
    r  = fxr_new(&cfg, tl->fs);
    memset(&d, 0, sizeof d);
    if (!acc || !fx || !r) { fprintf(stderr, "oom\n"); *rc = HOST_ERROR; }
    else if (fx_init(fx, tl->fs, &cfg, fx + 1, bytes)) { fprintf(stderr, "oom\n"); *rc = 1; }
    else {
        for (i = 0; i < tl->n; i += len) {
            len = tl->block - (i & (tl->block - 1u));
            if (len > tl->n - i) len = tl->n - i;
            for (c = 0; c < nch; c++)
                for (t = 0; t < len; t++) {
                    int64_t v = i + t >= c ? dry[i + t - c] : 0;
                    acc[c * FX_MAX_BLOCK + t] = (c & 1u) ? -v : v;
                }
            fx_master(fx, acc, FX_MAX_BLOCK, 30, out, len);
            for (t = 0; t < len; t++) {
                for (c = 0; c < nch; c++) dd[c] = (double)acc[c * FX_MAX_BLOCK + t] / 1073741824.0;
                fxr_step(r, bus, dd, ro);
                for (c = 0; c < nch; c++) fxd_add(&d, out[t * nch + c], ro[c], ((uint64_t)i + t) * nch + c);
            }
        }
        snprintf(what, sizeof what, "channels=%u", (unsigned)nch);
        fail = fxd_report(what, &d);
    }
    free(acc); free(fx); fxr_free(r);
    return fail;
}

/* P275 field comparison: production uses the existing scheduler, the real
 * model uses the established per-frame FX reference ordering and independent
 * voice/KS set1. No field/FX signal state is borrowed by the model. */
static int fxd_field(tl_t *tl)
{
    const sp_field_cfg_t cfg = tl_field_cfg(tl);
    const uint32_t ch = tl_field_ch(tl);
    const size_t bytes = tl_fx_mem_bytes(tl);
    sp_field_t *field = (sp_field_t *)calloc(1, sizeof *field);
    fx_t *fx = (fx_t *)calloc(1, sizeof *fx + bytes);
    fm_voice_t *native = (fm_voice_t *)alloc_n(tl->nvoices, sizeof *native);
    fm_voice_t *voices = (fm_voice_t *)alloc_n(tl->nvoices, sizeof *voices);
    int16_t *pcm = (int16_t *)alloc_n((size_t)tl->n * ch, sizeof *pcm);
    fieldr_t *model = NULL;
    tl_gen_runtime_t *gen = NULL;
    fxd_t d;
    uint32_t i, v, c, e = 0, clipped = 0, native_passes = 0, model_frames = 0;
    int rc = 0;
    const char *name = tl->route == SP_FIELD_AMB1 ? "field_amb1" :
        tl->route == SP_FIELD_AMB3 ? "field_amb3" : tl->order == 1 ? "field_bin1" : "field_bin3";
    if (!field || !fx || !native || !voices || !pcm) {
        rc = tl_host_error(NULL, 0, "out of memory"); goto done;
    }
    clipped = tl_render(tl, native, &field->geo, fx, pcm, 0);
    if (tl->render_error) { rc = tl->render_error; goto done; }
    native_passes = 1;
    if (clipped) {
        fprintf(stderr, "--fxref: field reference requires an unclipped field render; aggregate clipped=%u includes field and final saturation\n", clipped);
        rc = 2; goto done;
    }
    /* Native render has validated the same configuration and table admission.
     * NULL here is therefore a host model/allocation failure, never a pass. */
    model = fieldr_new(&cfg, &tl->fx);
    if (!model) { rc = tl_host_error(NULL, 0, "field reference construction failed"); goto done; }
    if (tl->gen_clock) {
        gen = (tl_gen_runtime_t *)malloc(sizeof *gen);
        if (!gen) { rc = tl_host_error(NULL, 0, "out of memory"); goto done; }
        if (tl_gen_init(tl, gen)) { rc = 1; goto done; }
    }
    for (v = 0; v < tl->nvoices; v++) {
        if (tl_voice_init_set(tl, &voices[v], v, 1u)) { rc = 1; goto done; }
    }
    memset(&d, 0, sizeof d);
    for (i = 0; i < tl->n; i++) {
        int32_t raw[TL_MAX_VOICES] = { 0 };
        double out[SP_FIELD_MAX_CH];
        for (; e < tl->nev && tl->ev[e].sample == i; e++) {
            const tl_event_t *ev = &tl->ev[e];
            if (ev->type == EV_POS) fieldr_set_pos(model, ev->voice, ev->x, ev->y, ev->z);
            else if (ev->type == EV_MOVE) fieldr_move(model, ev->voice, ev->x, ev->y, ev->z, ev->end);
            else if (ev->type == EV_YAW) fieldr_set_yaw(model, ev->x);
            else if (ev->type == EV_SEND) fieldr_set_send(model, ev->voice, ev->bus, ev->level_q15);
            else if (ev->type == EV_CRUSH) fieldr_set_crush(model, ev->voice, ev->crush_bits, ev->crush_div);
            if (gen && tl_gen_apply(gen, ev)) { rc = 1; goto done; }
            tl_apply(voices, NULL, NULL, ev);
        }
        if (gen && i % tl->gen_clock == 0 && tl_gen_step(tl, gen, i, voices, NULL, NULL)) { rc = 1; goto done; }
        if (tl->stems) memcpy(raw, tl->stems + (size_t)i * tl->nvoices, tl->nvoices * sizeof *raw);
        else for (v = 0; v < tl->nvoices; v++) fm_render_q30(&voices[v], &raw[v], 1);
        fieldr_step(model, raw, tl->nvoices, out);
        for (c = 0; c < ch; c++) fxd_add(&d, pcm[(size_t)i * ch + c], out[c], (uint64_t)i * ch + c);
        model_frames++;
    }
    { unsigned fail = fxd_report(name, &d);
      if (fail) { printf("FXREF FAIL %u\n", fail); rc = 4; }
      else printf("FXREF ok\n"); }
done:
    printf("FIELD_FXREF frames=%u channels=%u clipped=%u native_passes=%u model_frames=%u input=%s scope=unclipped_field_render\n",
           tl->n, ch, clipped, native_passes, model_frames, tl->stems ? "external_q30" : "voice_out_q30");
    free(field); free(fx); free(native); free(voices); free(pcm); free(gen); fieldr_free(model);
    return rc;
}

/* --fxref: renders the pool as --play does, and again through fx_ref's
 * double model fed the same voices: each sounding voice's Q2.30 sample
 * from a copy of the voices, the sends' integer ramps (the SPEC's
 * definition, as space/'s gains) and, in stereo, the spatial module's
 * wide sums from a copy without effects. Prints an FXREF line: the SNR
 * against the model, the ceiling of rounding the model, their difference
 * (loss) and the peak error against the model clipped. On a mono record
 * with no bus effect, the same for the master alone on 4 and 16 channels
 * (fxd_channels). The criteria, 's: peak <= 1 LSB and loss <=
 * FX_TOL_LOSS_DB; complete  lower/upper and post-bound sample comparison.
 * Raw model-rounded/output end equality remains a diagnostic. Exit 0, 1 on a bad timeline, 5 on a host failure, 4 when a
 * criterion fails. */
static int fx_diff_input(char **files, int nf, const char *stems)
{
    uint16_t lvl[TL_MAX_VOICES][FX_BUSES], gp[TL_MAX_VOICES][FX_BUSES], gt[TL_MAX_VOICES][FX_BUSES];
    tl_t *tl = (tl_t *)calloc(1, sizeof *tl);
    fm_voice_t *vs = NULL, *v1 = NULL, *v2 = NULL;
    sp_stereo_t *sp = NULL, *sp2 = NULL;
    fx_t *fx = NULL;
    fxr_t *r = NULL;
    tl_gen_runtime_t *gen = NULL;
    int16_t *pcm = NULL;
    int64_t *dry = NULL;
    size_t bytes = 0;
    uint32_t i, e = 0, ch = 1, b, vi, c;
    unsigned fail = 0;
    fxd_t d;
    int k, rc = 0;
    if (!tl) { fprintf(stderr, "oom\n"); return HOST_ERROR; }
    for (k = 0; k < nf && !rc; k++) rc = tl_read(tl, files[k], 1);
    if (!rc) rc = tl_check_header(tl);
    for (k = 0; k < nf && !rc; k++) rc = tl_read(tl, files[k], 2);
    if (!rc) rc = tl_check_decodable(tl);
    if (!rc && !tl->fx.on) rc = tl_err(NULL, 0, "--fxref needs a pool with an effects statement");
    if (!rc && stems) rc = tl_load_stems(tl, stems);
    if (!rc && tl->route) {
        if (tl->nev) qsort(tl->ev, tl->nev, sizeof *tl->ev, tl_cmp);
        rc = fxd_field(tl);
        goto fx_cleanup;
    }
    if (!rc) {
        if (tl->nev) qsort(tl->ev, tl->nev, sizeof *tl->ev, tl_cmp);
        if (tl->space) { ch = 2; sp = (sp_stereo_t *)calloc(1, sizeof *sp); sp2 = (sp_stereo_t *)calloc(1, sizeof *sp2); }
        bytes = fx_mem_bytes(&tl->fx, tl->fs);
        fx  = (fx_t *)calloc(1, sizeof *fx + bytes);
        vs  = (fm_voice_t *)alloc_n(tl->nvoices, sizeof *vs);
        v1  = (fm_voice_t *)alloc_n(tl->nvoices, sizeof *v1);
        v2  = (fm_voice_t *)alloc_n(tl->nvoices, sizeof *v2);
        pcm = (int16_t *)alloc_n((size_t)tl->n * ch, sizeof *pcm);
        dry = (int64_t *)alloc_n(tl->n, sizeof *dry);
        r   = fxr_new(&tl->fx, tl->fs);
        if (!fx || !vs || !v1 || !v2 || !pcm || !dry || !r || (ch == 2 && (!sp || !sp2))) { fprintf(stderr, "oom\n"); rc = HOST_ERROR; }
        else if (fx_init(fx, tl->fs, &tl->fx, fx + 1, bytes)) rc = tl_err(NULL, 0, "the effects' configuration is refused");
    }
    if (!rc) {
        tl_render(tl, vs, sp, fx, pcm, 0);
        if (tl->render_error) { rc = tl->render_error; goto fx_cleanup; }
        if (tl->gen_clock) {
            gen = (tl_gen_runtime_t *)malloc(sizeof *gen);
            if (!gen) { rc = tl_host_error(NULL, 0, "out of memory"); goto fx_cleanup; }
            if (tl_gen_init(tl, gen)) { rc = 1; goto fx_cleanup; }
        }
        for (i = 0; i < tl->nvoices; i++) {
            (void)tl_voice_init(tl, &v1[i], i);
            (void)tl_voice_init_set(tl, &v2[i], i, 1u);   /* : v1 and v2 sound at once */
        }
        if (sp2) sp_stereo_init(sp2, tl->block, tl->near);
        memset(lvl, 0, sizeof lvl);
        memset(&d, 0, sizeof d);
        for (i = 0; i < tl->n; i++) {
            double bus[FX_BUSES] = { 0, 0, 0 }, dd[2], out[2];
            int64_t sum = 0;
            int32_t crushed[TL_MAX_VOICES] = { 0 };
            uint32_t j = i & (tl->block - 1u);
            for (; e < tl->nev && tl->ev[e].sample == i; e++) {
                const tl_event_t *ev = &tl->ev[e];
                if (ev->type == EV_SEND) lvl[ev->voice][ev->bus] = (uint16_t)ev->level_q15;
                if (ev->type == EV_CRUSH) fxr_set_crush(r, ev->voice, ev->crush_bits, ev->crush_div);
                if (gen && tl_gen_apply(gen, ev)) { rc = 1; goto fx_cleanup; }
                tl_apply(v1, NULL, NULL, ev);
                if (sp2) tl_apply(v2, sp2, NULL, ev);
            }
            if (gen && i % tl->gen_clock == 0 && tl_gen_step(tl, gen, i, v1, sp2 ? v2 : NULL, NULL)) { rc = 1; goto fx_cleanup; }
            if (j == 0) {                                    /* a block boundary: the reference sends' own targets roll */
                int configured = 0;
                for (vi = 0; vi < TL_MAX_VOICES; vi++)
                    for (b = 0; b < FX_BUSES; b++) { gp[vi][b] = i == 0 ? lvl[vi][b] : gt[vi][b]; gt[vi][b] = lvl[vi][b]; }
                for (vi = 0; vi < TL_MAX_VOICES; vi++)
                    if (gp[vi][FX_DUCK] || gt[vi][FX_DUCK]) configured = 1;
                fxr_duck(r, configured);
            }
            for (vi = 0; vi < tl->nvoices; vi++) {
                int32_t x = 0;
                if (!fm_voice_active(&v1[vi]) && !fm_voice_needs_clock(&v1[vi])) {
                    if (!(tl->fx.on & FX_ON_CRUSH)) continue;
                } else fm_render_q30(&v1[vi], &x, 1);
                x = fxr_crush(r, vi, x);
                crushed[vi] = x;
                sum += x;
                for (b = 0; b < FX_BUSES; b++) {
                    int32_t g = ((int32_t)gp[vi][b] * (int32_t)tl->block + ((int32_t)gt[vi][b] - (int32_t)gp[vi][b]) * (int32_t)(j + 1u)) / (int32_t)tl->block;
                    bus[b] += (double)x * g / 35184372088832.0;   /* Q2.30 times Q1.15, in full-scale units */
                }
            }
            dry[i] = sum;
            if (sp2) {
                int16_t o2[2];
                sp_stereo_render(sp2, v2, tl->nvoices, o2, 1);
                if (tl->fx.on & FX_ON_CRUSH) {
                    int64_t placed[2] = { 0, 0 };
                    for (vi = 0; vi < tl->nvoices; vi++) for (c = 0; c < 2; c++) {
                        const sp_voice_t *sv = &sp2->v[vi];
                        int32_t g = ((int32_t)sv->gp[c] * (int32_t)tl->block
                            + ((int32_t)sv->gt[c] - (int32_t)sv->gp[c]) * (int32_t)(j + 1u)) / (int32_t)tl->block;
                        placed[c] += (int64_t)crushed[vi] * g;
                    }
                    dd[0] = (double)placed[0] / 35184372088832.0;
                    dd[1] = (double)placed[1] / 35184372088832.0;
                } else {
                    dd[0] = (double)sp2->acc[0][0] / 35184372088832.0;
                    dd[1] = (double)sp2->acc[1][0] / 35184372088832.0;
                }
            } else dd[0] = (double)sum / 1073741824.0;
            fxr_step(r, bus, dd, out);
            for (c = 0; c < ch; c++) fxd_add(&d, pcm[(size_t)i * ch + c], out[c], (uint64_t)i * ch + c);
        }
        fail = fxd_report(ch == 2 ? "stereo" : "mono", &d);
        if (ch == 1 && !(tl->fx.on & FX_ON_BUSES)) {
            fail += fxd_channels(tl, dry, 4, &rc);
            if (!rc) fail += fxd_channels(tl, dry, 16, &rc);
        }
        if (!rc) {
            if (fail) printf("FXREF FAIL %u\n", fail);
            else printf("FXREF ok\n");
            if (fail) rc = 4;
        }
    }
fx_cleanup:
    free(gen);
    free(tl->ev); free(tl->stems); free(tl->ks_pool); free(tl); free(vs); free(v1); free(v2); free(sp); free(sp2); free(fx); free(pcm); free(dry); fxr_free(r);
    return rc;
}

static int fx_diff(char **files, int nf)
{
    return fx_diff_input(files, nf, NULL);
}

/* --emit: each case as a canonical timeline, DIR/<case>.tl, one voice on
 * stream golden, with the hash of render_a1_blocks (fm_render_i16), so that
 * --play checks fm_mix_i16 against it. */
static int emit(uint32_t fs, const char *dir)
{
    size_t ci;
    for (ci = 0; ci < NCASE; ci++) {
        const spike_case_t *c = &CASES[ci];
        tl_t *tl = (tl_t *)calloc(1, sizeof *tl);
        tl_event_t ev[MAX_EV];
        uint32_t n = (uint32_t)((uint64_t)c->len_ms * fs / 1000u);
        int16_t *pcm = (int16_t *)alloc_n(n, sizeof *pcm);
        char path[512];
        uint64_t h;
        int e, ne = ev_count(c), rc;
        if (!tl || !pcm) { fprintf(stderr, "oom\n"); return HOST_ERROR; }
        tl->draft = 1;
        tl->fs = fs;
        tl->length_ns = (uint64_t)c->len_ms * 1000000u;
        tl->n = n;
        tl->nstreams = 1;
        strcpy(tl->stream[0], "golden");
        tl->npatches = 1;
        strcpy(tl->patch[0].name, c->name);
        tl->patch[0].p = c->patch;
        if (c->patch.filter.mode >= 1u && c->patch.filter.mode <= 4u) tl->filters = 1u;   /*  */
        if (c->patch.shape.kind) tl->shapes = 1u;
        for (e = 0; e < FM_MOD_SLOTS; ++e)
            if (c->patch.mod[e].target) tl->patch[0].mods |= 1u << e;
        if (c->patch.route.ops) tl->routes = 1u;                                          /*  */
        if (c->patch.lfo.rate_millihz) tl->lfos = tl->lfo_masks = 1u;                    /*  */
        for (e = 0; e < 4; e++)
            if (c->patch.peg.level_cents[e] || c->patch.peg.time_ms[e]) tl->pegs = 1u;
        if (c->patch.peg.mask) tl->pegs = 1u;
        { const fm_fenv_patch_t *f = &c->patch.fenv;
          if (f->ref_millihz || f->attack_ms || f->decay_ms || f->sustain_permille || f->release_ms || f->depth_cents || f->track_permille)
              tl->fenvs = 1u; }
        { const fm_assets_t *a = case_assets(c);   /* : the case's bank, named after it, in slot 0 */
          if (a && a->wt_count) {
              const fm_wt_view_t *wv = &a->wt[0];
              tl_wt_bank_t *bk = &tl->wt.bank[0];
              strcpy(bk->name, c->name);
              bk->frames = wv->frames; bk->length = wv->length; bk->mode = 1;
              bk->have_hash = 1; bk->hash = wv->data_hash;
              memcpy(tl->wt.pool, wv->data, (size_t)FM_WT_BANDS * wv->frames * wv->length * sizeof *wv->data);
              tl->wt.nbanks = 1;
          } }

        tl->nvoices = 1;
        tl->voice[0] = 0;
        for (e = 0; e < ne; e++) {
            memset(&ev[e], 0, sizeof ev[e]);
            ev[e].t_ns = (uint64_t)c->ev[e].t_ms * 1000000u;
            ev[e].type = c->ev[e].type;
            ev[e].freq_millihz = c->ev[e].freq_millihz;
            ev[e].velocity_q15 = c->ev[e].velocity_q15;
            ev[e].ctl_value = c->ev[e].ctl_value;
        }
        tl->ev = ev;
        tl->nev = (uint32_t)ne;
        render_a1_blocks(c, fs, pcm, n);
        h = fm_golden_hash(pcm, n);
        snprintf(path, sizeof path, "%s/%s.tl", dir, c->name);
        rc = tl_write(tl, path, h);
        if (!rc) printf("EMIT   %-8s hash=%016llx\n", c->name, (unsigned long long)h);
        free(tl); free(pcm);
        if (rc) return rc;
    }
    return 0;
}

/* ---- --filter: 's filter, engine against the reference ----
 * On the grid FILT_HZ x FILT_Q (cutoff 0 and UINT32_MAX give the clamps,
 * fs / 2400 and 0.45 fs), with the case lowpass's patch:
 *   COEF  the cutoff the engine's rounded coefficients give, fs / pi *
 *         atan(a2 / a1), against the reference's exact tan, in cents, and
 *         k's relative error against 1000 / Q.
 *   FILT  per mode, the tone at 110 Hz held 200 ms and released in 2 ms:
 *         ring, the samples from the carrier's idle to the zeroed states
 *         (2^18 at most); naive and cut, the reference's peak in int16 LSB
 *         from the carrier's idle and from the zeroing, what a cut at each
 *         point loses; err, the engine's peak error against the reference
 *         (Q2.30 / 2^15, unrounded); peak, the reference's peak in LSB;
 *         sat, the engine's samples with its output or a state at
 *         +-(2^31 - 1); and dc, over the last 1000 of 2^18 samples of a
 *         constant input x (a noise carrier at 0 Hz: its word never steps),
 *         the largest |y - x| (low-pass, notch) or |y| (band-pass,
 *         high-pass) in Q2.30 units.
 *   COST  ns per sample of fm_render_q30 on the tone in blocks of 256,
 *         without and with a 1 kHz low-pass, the best of five runs of 2^22
 *         samples (the host's clock: not stable across machines).
 * The other lines are stable for cross-machine diffs apart from the last
 * digits of values from the reference (the host's libm). */
static const uint32_t FILT_HZ[] = { 0, 100000, 1000000, 10000000, UINT32_MAX };
static const uint16_t FILT_Q[] = { 500, 707, 4000, 16000 };

static int probe_filter(uint32_t fs)
{
    const spike_case_t *lc = NULL;
    const uint32_t hold = fs / 5u, ndc = 1u << 18, n = hold + fs / 10u + ndc;
    double cost[2];
    int32_t *x, *y, blk[256];
    size_t ci, h, qi;
    uint32_t m, s;
    int rep;
    for (ci = 0; ci < NCASE; ci++)
        if (!strcmp(CASES[ci].name, "lowpass")) lc = &CASES[ci];
    if (!lc) return 1;
    x = (int32_t *)alloc_n(ndc, sizeof *x);
    y = (int32_t *)alloc_n(n, sizeof *y);
    if (!x || !y) { fprintf(stderr, "oom\n"); return HOST_ERROR; }
    {   /* the constant input, unfiltered */
        fm_patch_t p = lc->patch;
        fm_voice_t v;
        p.op[0].wave = 4;
        p.op[0].sustain_db10 = 0;
        p.filter.mode = 0;
        fm_voice_init(&v, &p, fs);
        fm_voice_note_on(&v, 0, 32767);
        fm_render_q30(&v, x, ndc);
    }
    for (h = 0; h < sizeof FILT_HZ / sizeof FILT_HZ[0]; h++)
        for (qi = 0; qi < sizeof FILT_Q / sizeof FILT_Q[0]; qi++) {
            fm_patch_t p = lc->patch;
            fm_voice_t v;
            ref_voice_t r;
            double ge, gr, fc;
            p.filter.cutoff_millihz = FILT_HZ[h];
            p.filter.q_milli = FILT_Q[qi];
            p.filter.mode = 1;
            p.op[0].release_ms = p.op[1].release_ms = 2;
            fm_voice_init(&v, &p, fs);
            ref_voice_init(&r, &p, fs);
            ge = (double)v.svf.a2 / (double)v.svf.a1;
            gr = r.fa2 / r.fa1;
            fc = (double)fs * 2.0 / TWO_PI * atan(gr);
            printf("COEF   fc_hz=%.3f q=%.3f cents=%+.4f k_rel=%+.2e\n", fc, FILT_Q[qi] / 1000.0,
                   1200.0 * log2(atan(ge) / atan(gr)), ((double)v.svf.k / 536870912.0) / r.fk - 1.0);
            for (m = 1; m <= 4; m++) {
                uint32_t idle = n, gone = n, sat = 0;
                double naive = 0, cut = 0, err = 0, peak = 0;
                int64_t dc = 0;
                p.filter.mode = (uint16_t)m;
                fm_voice_init(&v, &p, fs);
                ref_voice_init(&r, &p, fs);
                fm_voice_note_on(&v, 110000, 32767);
                ref_voice_note_on(&r, 110000, 32767);
                for (s = 0; s < n; s++) {
                    double o;
                    if (s == hold) { fm_voice_note_off(&v); ref_voice_note_off(&r); }
                    if (s > hold && idle == n && v.op[0].stage == FM_STAGE_IDLE) idle = s;
                    if (s > hold && gone == n && !fm_voice_active(&v)) gone = s;
                    fm_render_q30(&v, &y[s], 1);
                    ref_render(&r, &o, 1);
                    if (fabs(y[s] / 32768.0 - o) > err) err = fabs(y[s] / 32768.0 - o);
                    if (fabs(o) > peak) peak = fabs(o);
                    if (y[s] == INT32_MAX || y[s] == -INT32_MAX || v.svf.ic1 == INT32_MAX || v.svf.ic1 == -INT32_MAX
                        || v.svf.ic2 == INT32_MAX || v.svf.ic2 == -INT32_MAX) sat++;
                    if (s >= idle && fabs(o) > naive) naive = fabs(o);
                    if (s >= gone && fabs(o) > cut) cut = fabs(o);
                }
                {   /* the constant input through the filter */
                    fm_patch_t pd = p;
                    pd.op[0].wave = 4;
                    pd.op[0].sustain_db10 = 0;
                    fm_voice_init(&v, &pd, fs);
                    fm_voice_note_on(&v, 0, 32767);
                    fm_render_q30(&v, y, ndc);
                    for (s = ndc - 1000u; s < ndc; s++) {
                        int64_t e = (m == 1u || m == 4u) ? (int64_t)y[s] - x[s] : (int64_t)y[s];
                        if (e < 0) e = -e;
                        if (e > dc) dc = e;
                    }
                }
                printf("FILT   %-5s fc_hz=%.3f q=%.3f ring=%u%s naive_lsb=%.3f cut_lsb=%.4f err_lsb=%.3f peak_lsb=%.1f sat=%u dc=%lld\n",
                       TL_FILTER_MODE[m], fc, FILT_Q[qi] / 1000.0, gone - idle, gone == n ? "(open)" : "",
                       naive, cut, err, peak, sat, (long long)dc);
            }
        }
    for (m = 0; m < 2; m++) {
        fm_patch_t p = lc->patch;
        p.filter.cutoff_millihz = 1000000;
        p.filter.q_milli = 707;
        p.filter.mode = (uint16_t)m;
        cost[m] = 1e30;
        for (rep = 0; rep < 5; rep++) {
            fm_voice_t v;
            clock_t t0;
            double t;
            fm_voice_init(&v, &p, fs);
            fm_voice_note_on(&v, 110000, 32767);
            t0 = clock();
            for (s = 0; s < (1u << 22); s += 256u) fm_render_q30(&v, blk, 256);
            t = (double)(clock() - t0) / CLOCKS_PER_SEC;
            if (t < cost[m]) cost[m] = t;
        }
        cost[m] *= 1e9 / (double)(1u << 22);
    }
    printf("COST   ns_per_sample=%.2f with_lowpass=%.2f filter=%.2f (this host's clock)\n",
           cost[0], cost[1], cost[1] - cost[0]);
    free(x); free(y);
    return 0;
}

static int inspect_record(char **files, int nf, int events)
{
    tl_t *tl = calloc(1, sizeof *tl);
    uint32_t ch, i;
    uint64_t pcm, estimate;
    int k, rc = 0;
    char engine[160];
    if (!tl) return tl_host_error(NULL, 0, "out of memory");
    for (k = 0; k < nf && !rc; k++) rc = tl_read(tl, files[k], 1);
    if (!rc) rc = tl_check_header(tl);
    for (k = 0; k < nf && !rc; k++) rc = tl_read(tl, files[k], 2);
    if (!rc) rc = tl_check_decodable(tl);
    if (rc) goto cleanup;
    ch = tl->route ? tl_field_ch(tl) : tl->space ? 2u : 1u;
    pcm = (uint64_t)tl->n * ch * 2u;
    estimate = sizeof *tl + (uint64_t)tl->cap * sizeof *tl->ev
             + (uint64_t)tl->nvoices * sizeof(fm_voice_t) + 2u * pcm;
    if (tl->route) estimate += sizeof(sp_field_t);
    else if (tl->space) estimate += sizeof(sp_stereo_t);
    if (tl->fx.on) estimate += sizeof(fx_t) + tl_fx_mem_bytes(tl);
    if (tl->gen_clock) estimate += sizeof(tl_gen_runtime_t);
    tl_engine(engine, sizeof engine);
    printf("ENGINE %s\n", engine);
    printf("INSPECT fs=%u frames=%u channels=%u route=%d voices=%u events=%u length_ns=%llu pcm_bytes=%llu render_bytes_lower_bound=%llu once_render_bytes_lower_bound=%llu hash_present=%d expected_hash=",
        tl->fs, tl->n, ch, tl->route, tl->nvoices, tl->nev, (unsigned long long)tl->length_ns,
        (unsigned long long)pcm, (unsigned long long)estimate, (unsigned long long)(estimate - pcm), tl->have_hash);
    if (tl->have_hash) printf("%016llx", (unsigned long long)tl->hash);
    else printf("none");
    printf(" keys_profile=1\n");
    if (events) {
        if (tl->nev) qsort(tl->ev, tl->nev, sizeof *tl->ev, tl_cmp);
        for (i = 0; i < tl->nev; i++) {
            const tl_event_t *e = &tl->ev[i];
            const char *kind = e->type == EV_ON ? "on" : e->type == EV_OFF ? "off" : e->type == EV_PITCH ? "pitch" : e->type == EV_POS ? "pos" : e->type == EV_MOVE ? "move" : "control";
            printf("EVENT time_ns=%llu stream=%s voice=%u patch=%s kind=%s freq_millihz=%u velocity=%u duration_ns=%llu x=%d y=%d z=%d level=%u\n",
                (unsigned long long)e->t_ns, tl->stream[e->stream], e->voice,
                tl->vpatch[e->voice], kind, e->freq_millihz, e->velocity_q15,
                (unsigned long long)e->dur_ns, e->x, e->y, e->z, e->level_q15);
        }
    }
cleanup:
    free(tl->ev); free(tl->ks_pool); free(tl);
    if (!rc && events) rc = gen_events(files, nf);
    return rc;
}

static int usage(void)
{
    fprintf(stderr, "usage: harness [--fs HZ] [--out DIR] [--check [lfo|peg-bounds|fenv-bounds] | --retrig | --fb | --filter]\n"
                    "       harness [--fs HZ] --emit DIR\n"
                    "       harness --gen-params | --params | --params-check\n"
                    "       harness --engine | --inspect [--] FILE... | --events [--] FILE...\n"
                    "       harness --gen-events [--] FILE...\n"
                    "       harness --keys-profile 30|45|60|75|90 [--keys-mirror] [--keys-carrier-bound] --merged PATH [--] FILE...\n"
                    "       harness --gen-soak [--merged PATH] [--] FILE...\n"
                    "       harness --play [--wav PATH] [--merged PATH] [--stems WAV] [--metrics] [--ref] [--chunk N|random] [--] FILE...   (at most %d files)\n"
                    "       harness --play-once [--wav PATH] [--merged PATH] [--stems WAV] [--metrics] [--] FILE...\n"
                    "         one render; block_indep=unchecked; no --ref, --chunk or stems output\n"
                    "       harness --play --wav-fd N --pcm-limit BYTES [--merged PATH] [--metrics] [--chunk N|random] [--] FILE...\n"
                    "       harness --play (--stems-out PATH | --stems-fd N) --pcm-limit BYTES [--merged PATH] [--metrics] [--chunk N|random] [--] FILE...\n"
                    "         stems output: all pre-FX voices, exact signed PCM32/Q2.30 (two mix passes plus one dry pass)\n"
                    "         a piece: its session.tl and its stream files\n"
                    "       harness --fxref [--] FILE...\n"
                    "       harness --field-test | --field-cost\n", TL_MAX_FILES);
    return 2;
}

static int harness_run(int argc, char **argv)
{
    uint32_t fs = 48000;
    const char *outdir = NULL, *emitdir = NULL, *wav = NULL, *merged = NULL, *stems = NULL;
    char *files[TL_MAX_FILES];
    int a, check = 0, lcheck = 0, pbounds = 0, ebounds = 0, retrig = 0, fb = 0, filt = 0, playing = 0, manifest = 0, soak = 0, params = 0, allparams = 0, paramscheck = 0, nf = 0, fxref = 0, ftest = 0, fcost = 0;
    int pmetrics = 0, pref = 0, pchunk = 1, have_chunk = 0, fs_given = 0;
    int once = 0, inspecting = 0, events = 0, engine_info = 0;
    int keys_width = 0, keys_mirror = 0, keys_bounded = 0;
    int wav_fd = -1, stems_fd = -1, have_pcm_limit = 0;
    const char *stems_out = NULL;
    uint64_t pcm_limit = 0;
    unsigned gate = 0;
    uint64_t v;
    size_t ci;

    for (a = 1; a < argc; a++) {
        if (!strcmp(argv[a], "--fs") && a + 1 < argc) {
            if (tl_num(argv[++a], UINT32_MAX, &v) || v == 0) {
                fprintf(stderr, "--fs takes a rate in Hz, 1 to %u\n", (unsigned)UINT32_MAX);
                return 2;
            }
            fs = (uint32_t)v;
            fs_given = 1;
        }
        else if (!strcmp(argv[a], "--out") && a + 1 < argc) outdir = argv[++a];
        else if (!strcmp(argv[a], "--check")) {
            if (a + 1 == argc || argv[a + 1][0] == '-') check = 1;
            else if (!strcmp(argv[++a], "lfo")) lcheck = 1;
            else if (!strcmp(argv[a], "peg-bounds")) pbounds = 1;
            else if (!strcmp(argv[a], "fenv-bounds")) ebounds = 1;
            else return usage();
        }
        else if (!strcmp(argv[a], "--retrig")) retrig = 1;
        else if (!strcmp(argv[a], "--fb")) fb = 1;
        else if (!strcmp(argv[a], "--filter")) filt = 1;
        else if (!strcmp(argv[a], "--emit") && a + 1 < argc) emitdir = argv[++a];
        else if (!strcmp(argv[a], "--gen-soak") && !playing) playing = soak = 1;
        else if (!strcmp(argv[a], "--gen-events") && !playing) playing = manifest = 1;
        else if (!strcmp(argv[a], "--keys-profile") && !playing && a + 1 < argc) {
            if (tl_num(argv[++a], 90, &v) || (v != 30 && v != 45 && v != 60 && v != 75 && v != 90)) return usage();
            keys_width = (int)v; playing = 1;
        }
        else if (!strcmp(argv[a], "--keys-mirror") && !keys_mirror) keys_mirror = 1;
        else if (!strcmp(argv[a], "--keys-carrier-bound") && !keys_bounded) keys_bounded = 1;
        else if (!strcmp(argv[a], "--gen-params")) params = 1;
        else if (!strcmp(argv[a], "--params")) allparams = 1;
        else if (!strcmp(argv[a], "--params-check")) paramscheck = 1;
        else if (!strcmp(argv[a], "--play") && !playing) playing = 1;
        else if (!strcmp(argv[a], "--play-once") && !playing) playing = once = 1;
        else if (!strcmp(argv[a], "--inspect") && !playing) playing = inspecting = 1;
        else if (!strcmp(argv[a], "--events") && !playing) playing = inspecting = events = 1;
        else if (!strcmp(argv[a], "--engine")) engine_info = 1;
        else if (!strcmp(argv[a], "--fxref") && !playing) playing = fxref = 1;
        else if (!strcmp(argv[a], "--wav") && a + 1 < argc) wav = argv[++a];
        else if (!strcmp(argv[a], "--wav-fd") && a + 1 < argc) {
            if (wav_fd >= 0 || tl_num(argv[++a], INT_MAX, &v) || v < 3u) {
                fprintf(stderr, "--wav-fd takes one inherited writable pipe descriptor, 3 to %d\n", INT_MAX);
                return 2;
            }
            wav_fd = (int)v;
        }
        else if (!strcmp(argv[a], "--stems-out") && a + 1 < argc) {
            if (stems_out) return usage();
            stems_out = argv[++a];
        }
        else if (!strcmp(argv[a], "--stems-fd") && a + 1 < argc) {
            if (stems_fd >= 0 || tl_num(argv[++a], INT_MAX, &v) || v < 3u) {
                fprintf(stderr, "--stems-fd takes one inherited writable pipe descriptor, 3 to %d\n", INT_MAX);
                return 2;
            }
            stems_fd = (int)v;
        }
        else if (!strcmp(argv[a], "--pcm-limit") && a + 1 < argc) {
            if (have_pcm_limit || tl_num(argv[++a], UINT64_MAX, &pcm_limit) || !pcm_limit) {
                fprintf(stderr, "--pcm-limit takes one positive uint64 byte count\n");
                return 2;
            }
            have_pcm_limit = 1;
        }
        else if (!strcmp(argv[a], "--merged") && a + 1 < argc) merged = argv[++a];
        else if (!strcmp(argv[a], "--stems") && a + 1 < argc) stems = argv[++a];
        else if (!strcmp(argv[a], "--metrics")) pmetrics = 1;
        else if (!strcmp(argv[a], "--ref")) pref = 1;
        else if (!strcmp(argv[a], "--chunk") && a + 1 < argc) {
            const char *chunk = argv[++a];
            if (!strcmp(chunk, "random")) pchunk = -1;
            else if (tl_num(chunk, INT_MAX, &v) || v == 0) {
                fprintf(stderr, "--chunk takes 1 to %d frames or random\n", INT_MAX);
                return 2;
            } else pchunk = (int)v;
            have_chunk = 1;
        }
        else if (!strcmp(argv[a], "--field-test")) ftest = 1;
        else if (!strcmp(argv[a], "--field-cost")) fcost = 1;
        else if (!strcmp(argv[a], "--") && playing) {
            while (++a < argc) {
                if (nf == TL_MAX_FILES) return usage();
                files[nf++] = argv[a];
            }
            break;
        }
        else if (playing && argv[a][0] != '-' && nf < TL_MAX_FILES) files[nf++] = argv[a];
        else return usage();
    }
    if (check + lcheck + pbounds + ebounds + retrig + fb + filt + !!emitdir + playing + params + allparams + paramscheck + ftest + fcost + engine_info > 1) return usage();
    if ((keys_mirror || keys_bounded) && !keys_width) return usage();
    if (keys_width && (!merged || wav || stems || pref || pmetrics || have_chunk || have_pcm_limit || wav_fd >= 0 || stems_out || stems_fd >= 0)) return usage();
    if ((fs_given || outdir) && (playing || params || allparams || paramscheck || ftest || fcost || engine_info)) return usage();
    if ((pmetrics || pref || have_chunk) && (!playing || soak || manifest || fxref || inspecting)) return usage();
    if (once && (pref || have_chunk || stems_out || stems_fd >= 0)) {
        fprintf(stderr, "--play-once refuses --ref, --chunk and stems output; use --play for those verification paths\n");
        return 2;
    }
    if (stems_out || stems_fd >= 0) {
        if ((stems_out && stems_fd >= 0) || !have_pcm_limit || !playing || soak || manifest || fxref || inspecting
            || wav || wav_fd >= 0 || stems || pref) return usage();
        if (stems_fd >= 0) {
            struct stat st;
            const int mode = fcntl(stems_fd, F_GETFL);
            if (mode < 0 || fstat(stems_fd, &st) || !S_ISFIFO(st.st_mode) || (mode & O_ACCMODE) == O_RDONLY) {
                fprintf(stderr, "--stems-fd requires an inherited writable pipe descriptor\n");
                return 2;
            }
        }
    }
    if ((wav_fd >= 0 || have_pcm_limit) && !stems_out && stems_fd < 0) {
        struct stat st;
        int mode;
        if (wav_fd < 0 || !have_pcm_limit || !playing || soak || manifest || fxref || inspecting || wav || stems || pref)
            return usage();
        mode = fcntl(wav_fd, F_GETFL);
        if (mode < 0 || fstat(wav_fd, &st) || !S_ISFIFO(st.st_mode) || (mode & O_ACCMODE) == O_RDONLY) {
            fprintf(stderr, "--wav-fd requires an inherited writable pipe descriptor\n");
            return 2;
        }
    }
    if ((emitdir || lcheck || pbounds || ebounds) && FM_CTL != 64) {
        if (emitdir) fprintf(stderr, "--emit needs FM_CTL=64; this build has FM_CTL=%d\n", FM_CTL);
        else fprintf(stderr, "--check %s needs FM_CTL=64; this build has FM_CTL=%d\n",
                     lcheck ? "lfo" : pbounds ? "peg-bounds" : "fenv-bounds", FM_CTL);
        return 2;
    }
    if (pbounds) {
        if (fs != 48000u && fs != 44100u) {
            fprintf(stderr, "--check peg-bounds takes --fs 48000 or 44100; raw API extremes are internal cases\n");
            return 2;
        }
        if (peg_bounds(fs)) { printf("CHECK peg-bounds FAIL\n"); return 4; }
        printf("CHECK peg-bounds ok\n"); return 0;
    }
    if (ebounds) {
        if (fs != 48000u && fs != 44100u) {
            fprintf(stderr, "--check fenv-bounds takes --fs 48000 or 44100; raw API extremes are internal cases\n");
            return 2;
        }
        if (fenv_bounds(fs)) { printf("CHECK fenv-bounds FAIL\n"); return 4; }
        printf("CHECK fenv-bounds ok\n"); return 0;
    }
    if (ftest) return field_test();
    if (fcost) return field_cost();
    if (engine_info) {
        char engine[160];
        if (wav || merged || stems) return usage();
        tl_engine(engine, sizeof engine);
        printf("ENGINE %s\n", engine);
        return 0;
    }
    if (params) return wav || merged || stems ? usage() : tl_gen_params();
    if (allparams) return wav || merged || stems ? usage() : tl_params();
    if (paramscheck) {
        int rc;
        if (wav || merged || stems) return usage();
        rc = tl_params_schema_check();
        return rc ? rc : tl_params_writer_check();
    }
    if (playing) {
        if (keys_width) return !nf ? usage() : keys_profile(files, nf, merged, keys_width, keys_mirror, keys_bounded);
        if (inspecting) return !nf || wav || merged || stems ? usage() : inspect_record(files, nf, events);
        if (soak) return !nf || wav || stems ? usage() : gen_soak(files, nf, merged);
        if (manifest) return !nf || wav || merged || stems ? usage() : gen_events(files, nf);
        if (!nf) { fprintf(stderr, "--play needs at least one file\n"); if (stems_fd >= 0) close(stems_fd); return 2; }
        if (fxref) return wav || merged || stems ? usage() : fx_diff(files, nf);
        if (wav_fd >= 0 || stems_fd >= 0) {
            const int pipe_fd = stems_fd >= 0 ? stems_fd : wav_fd;
            const char *pipe_name = stems_fd >= 0 ? "--stems-fd" : "--wav-fd";
            struct sigaction ignore, previous;
            int rc;
            memset(&ignore, 0, sizeof ignore);
            ignore.sa_handler = SIG_IGN;
            sigemptyset(&ignore.sa_mask);
            if (sigaction(SIGPIPE, &ignore, &previous)) {
                rc = io_fail(pipe_name, "set pipe signal handler");
                close(pipe_fd);
                return rc;
            }
            rc = play(files, nf, wav, merged, stems, pmetrics, pref, pchunk, wav_fd, pcm_limit, stems_out, stems_fd, once);
            if (sigaction(SIGPIPE, &previous, NULL))
                rc = exit_merge(rc, io_fail(pipe_name, "restore pipe signal handler"));
            return rc;
        }
        return play(files, nf, wav, merged, stems, pmetrics, pref, pchunk, -1, pcm_limit, stems_out, -1, once);
    }
    for (ci = 0; ci < NCASE; ci++)
        if ((uint64_t)CASES[ci].len_ms * fs / 1000u > UINT32_MAX) {
            fprintf(stderr, "--fs %u: case %s would pass 2^32 - 1 samples\n", (unsigned)fs, CASES[ci].name);
            return 2;
        }
    if (check && !GATE_BUILD) {
        fprintf(stderr, "--check runs only at the approved knobs: SINE_BITS=12 EXP_BITS=10 PHASE64=1 ENV64=1 FB_AVG=1 FM_CTL=64; this build has FM_CTL=%d (SINE_BITS=%d EXP_BITS=%d PHASE64=%d ENV64=%d FB_AVG=%d)\n", FM_CTL, FM_SINE_BITS, FM_EXP_BITS, FM_PHASE64, FM_ENV64, FM_FB_AVG);
        return 2;
    }
    if (lcheck && !(FM_SINE_BITS == 12 && FM_EXP_BITS == 10 && FM_ENV64 == 1 && FM_FB_AVG == 1
                   && (FM_PHASE64 == 0 || FM_PHASE64 == 1))) {
        fprintf(stderr, "--check lfo needs SINE_BITS=12 EXP_BITS=10 PHASE64=0 or 1 ENV64=1 FB_AVG=1\n");
        return 2;
    }

    printf("CONFIG fs=%u sine_bits=%d exp_bits=%d phase64=%d env64=%d fb_avg=%d int128=%d table_bytes=%u table_hash=%016llx voice_bytes=%u patch_bytes=%u fx_bytes=%u stereo_bytes=%u field_bytes=%u\n",
           fs, FM_SINE_BITS, FM_EXP_BITS, FM_PHASE64, FM_ENV64, FM_FB_AVG, FP_MATH_HAS_INT128,
           fm_table_bytes(), (unsigned long long)fm_table_hash(), (unsigned)sizeof(fm_voice_t),
           (unsigned)sizeof(fm_patch_t), (unsigned)sizeof(fx_t),
           (unsigned)sizeof(sp_stereo_t), (unsigned)sizeof(sp_field_t));

    if (retrig) return probe_retrig(fs);
    if (fb) return probe_fb(fs, outdir);
    if (filt) return probe_filter(fs);
    if (emitdir) return emit(fs, emitdir);
    if (check || lcheck) {
        const unsigned failed = gen_adapter_bounds(fs);
        if (failed == BOUNDS_HOST_ERROR) return HOST_ERROR;
        if (failed) gate_fail(&gate, "generator adapter", "grid, ordering, readers or partition");
    }
    if ((check || lcheck) && formant_bounds(fs)) gate_fail(&gate, "formants", "recurrence, saturation, modulation or lifetime");
    if ((check || lcheck) && vector_move_bounds(fs)) gate_fail(&gate, "vector motion", "smoother, coordinate cache or lifecycle");
    if ((check || lcheck) && vector_bounds(fs)) gate_fail(&gate, "vector bounds", "rounding, raw boundary or lifecycle");
    if (check && vector_corners(fs)) gate_fail(&gate, "vector corners", "whole-stream fidelity or selected carriers");
    if ((check || lcheck) && pd_bounds(fs)) gate_fail(&gate, "phase distortion bounds", "integer map, control or lifecycle");
    if ((check || lcheck) && gen_cache_bounds(fs)) gate_fail(&gate, "generator caches", "latching, normalization or lifetime");
    if ((check || lcheck) && wide_bounds(fs)) gate_fail(&gate, "matrix wide", "range, cached pitch or partition");
    if ((check || lcheck) && matrix_bounds(fs)) gate_fail(&gate, "matrix", "sources, smoothing or raw bounds");
    if (check || lcheck) {
        const unsigned failed = inert_slot_bounds(fs);
        if (failed == BOUNDS_HOST_ERROR) return HOST_ERROR;
        if (failed) gate_fail(&gate, "inert slot", "legacy and matrix tick output");
    }
    if ((check || lcheck) && mod_event_bounds(fs)) gate_fail(&gate, "matrix events", "controller ordering or idle lifetime");
    if ((check || lcheck) && mod_partition_bounds(fs)) gate_fail(&gate, "matrix blocks", "logical state or output");
    if ((check || lcheck) && mod_precision_bounds(fs, 256u)) gate_fail(&gate, "matrix precision", "derived increment window");
    if ((check || lcheck) && mod_note_bounds(fs)) gate_fail(&gate, "matrix note", "source rounding interval");
    if ((check || lcheck) && sync_bounds(fs)) gate_fail(&gate, "sync", "timing, history or normalization");
    if ((check || lcheck) && bind_bounds(fs)) gate_fail(&gate, "asset binding", "validation, atomicity or lifetime");
    if ((check || lcheck) && wt_bounds(fs)) gate_fail(&gate, "wavetable", "generator, read, bands or canonical twins");
    if ((check || lcheck) && ks_bounds(fs)) gate_fail(&gate, "karplus", "tuning, loop, wrap, lifecycle or canonical twins");
    if ((check || lcheck) && cascade_bounds(fs)) gate_fail(&gate, "cascade", "recurrence, states or lifetime");
    if ((check || lcheck) && shape_bounds(fs)) gate_fail(&gate, "shape", "transfer, ordering or lifetime");
    if ((check || lcheck) && amp_bounds(fs)) gate_fail(&gate, "am/ring", "arithmetic, ordering or history");
    if ((check || lcheck) && jump_bounds(fs)) gate_fail(&gate, "jumps", "phase, bounds or real PM");
    if ((check || lcheck) && colour_bounds(fs)) gate_fail(&gate, "colour", "recurrence, bounds or lifetime");
    if ((check || lcheck) && pink_bounds(fs)) gate_fail(&gate, "pink", "schedule, state, bounds or lifetime");
    if ((check || lcheck) && wave_bounds(fs)) gate_fail(&gate, "waves", "phase, PM, bounds or FREE2");
    if ((check || lcheck) && ks_left_bounds(fs)) gate_fail(&gate, "ksleft", "gain or cached index scaling");
    if ((check || lcheck) && ksfb_bounds(fs)) gate_fail(&gate, "ksfb", "key-scaling targets, boost, feedback types or identity");
    if ((check || lcheck) && decay2_bounds(fs)) gate_fail(&gate, "decay2", "counts, boundaries or attenuation");
    if ((check || lcheck) && delay_bounds(fs)) gate_fail(&gate, "delay", "silent samples or prepared attack");
    if ((check || lcheck) && free_bounds(fs)) gate_fail(&gate, "free", "phase, noise or control grid");
    if (check && rate_bounds(fs)) gate_fail(&gate, "ratescale", "adoption window boundaries");
    if (check) {
        const unsigned failed = hostskip_check(fs);
        if (failed == BOUNDS_HOST_ERROR) return HOST_ERROR;
        if (failed) gate_fail(&gate, "host skip", "voice lifecycle, state oracle or route partition");
    }
    if (check) {
        const unsigned failed = egadopt_bounds(fs);
        if (failed == BOUNDS_HOST_ERROR) return HOST_ERROR;
        if (failed) gate_fail(&gate, "egadopt", "amp or loop count adoption");
    }
    if (check && peg_bounds(fs)) gate_fail(&gate, "peg", "raw API boundaries");
    if (check && fenv_bounds(fs)) gate_fail(&gate, "fenv", "raw API boundaries");
    if (lcheck) printf("LFO_CHECK subset=phase,lfonyq,lfosawup,lfosawdn,lforand,freeop,freelfo,alternating,camel,pink,pinkfree,colourlp,colourhp,colourfree,square,logsaw,vectorxy,wtmorph,wtbands,kspluck,ksbend phase64=%d\n", FM_PHASE64);
    if ((check || lcheck) && exp2_test()) gate_fail(&gate, "exp2", "past 4 ulp or 2^-44");   /*  */
    for (ci = 0; ci < NCASE; ci++)
        if (!lcheck || lfo_check_case(&CASES[ci])) {
            const int rc = run_case(&CASES[ci], fs, outdir, (check || lcheck) ? &gate : NULL);
            if (rc) return rc;
        }
    if (!check && !lcheck) return 0;
    if (lcheck) {
        if (!gate) { printf("CHECK lfo ok\n"); return 0; }
        printf("CHECK lfo FAIL %u\n", gate);
        return 4;
    }
    if (!gate) { printf("GATE ok\n"); return 0; }
    printf("GATE FAIL %u\n", gate);
    return 4;
}

/* Flush while an I/O failure can still be returned to the caller. */
int main(int argc, char **argv)
{
    const int rc = harness_run(argc, argv);
    if (fflush(stdout) || ferror(stdout)) return io_fail("stdout", "write");
    return rc;
}
