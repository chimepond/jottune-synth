/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * fx.h — the integer effects module (docs/INSTRUMENT.md, effects): per-voice
 * sends to four audible buses (chorus, delay, reverb, flanger) and the silent duck
 * detector bus. The audible returns join the dry
 * sum, and a master chain (gain, DC blocker, compressor, look-ahead limiter, soft
 * clip, dither; the order) run on the wide sum before its one
 * rounding to int16, on the mono mix or on
 * space/the stereo accumulators. Integer only: no floating point, no libm,
 * no heap, no mutable global state; bit-exact across targets. Every part
 * is opt-in, and with no part on the output is the plain mix's, bit for
 * bit.
 *
 * Layering: fm_a1 <- fx <- space <- the harness. fx calls the engine,
 * space calls fx through voice/master hooks (the field route splits raw
 * returns from the post master under ), and fx
 * never includes space. Memory for the delay lines is the caller's
 * (fx_mem_bytes, fx_init); per-sample work is 32x32->64 multiplies, adds,
 * shifts and table reads, and divisions happen only at init and at the
 * dynamics' half-block boundaries (docs/INSTRUMENT.md, section 6).
 *
 * Units: signals are Q2.30 per voice (1 << 30 = int16 full scale), the
 * audible buses and the effects' lines Q5.27 int32 (1 << 27 = full scale, 16
 * times full scale of headroom for a loud send, the delay's feedback and
 * the combs' sum), the duck sum and master work in Q.45 int64 (1 << 45 = full
 * scale). Send levels are Q1.15, 0 to 32768, ramped linearly across each
 * block like space/the gains.
 */
#ifndef FX_FX_H
#define FX_FX_H

#include <stddef.h>
#include <stdint.h>
#include "fm_a1.h"

#ifdef __cplusplus
extern "C" {
#endif

#define FX_MAX_VOICES 64
#define FX_MAX_BLOCK  128
#define FX_MAX_CH     16

enum { FX_CHORUS = 0, FX_DELAY, FX_REVERB, FX_DUCK, FX_FLANGER, FX_BUSES };   /* the send buses */

#define FX_ON_DC       0x01u     /* fx_cfg_t.on: the parts a configuration turns on */
#define FX_ON_CHORUS   0x02u
#define FX_ON_DELAY    0x04u
#define FX_ON_REVERB   0x08u
#define FX_ON_LIMITER  0x10u
#define FX_ON_SOFTCLIP 0x20u
#define FX_ON_GAIN     0x40u     /* : the master gain, the chain's first stage */
#define FX_ON_DITHER   0x80u     /* : TPDF dither in the rounding */
#define FX_ON_PINGPONG 0x100u    /* : the delay bus as a ping-pong; set with FX_ON_DELAY, stereo only */
#define FX_ON_COMPRESSOR 0x200u  /* : linked master compression, before the limiter */
#define FX_ON_FLANGER   0x400u  /* : a causal feedback line with two modulated taps */
#define FX_ON_CRUSH     0x800u  /* /: per-voice bit/rate reduction before dry and sends */
/* The bit whose statement turns bus b on; a new bus appends its row
 * (the duck bus maps to the compressor's bit). */
static const uint32_t FX_BUS_BIT[FX_BUSES] = { FX_ON_CHORUS, FX_ON_DELAY, FX_ON_REVERB, FX_ON_COMPRESSOR, FX_ON_FLANGER };

#define FX_ON_BUSES    (FX_ON_CHORUS | FX_ON_DELAY | FX_ON_REVERB | FX_ON_FLANGER)
/* Every defined bit: fx_init refuses any other. A new stage takes the next
 * free bit and joins this mask (docs/INSTRUMENT.md, section 5). */
#define FX_ON_ALL      (FX_ON_DC | FX_ON_BUSES | FX_ON_LIMITER | FX_ON_SOFTCLIP | FX_ON_GAIN | FX_ON_DITHER \
                        | FX_ON_PINGPONG | FX_ON_COMPRESSOR | FX_ON_CRUSH)

/* A configuration, in the timeline's units (docs/INSTRUMENT.md, section 7). Only
 * the fields of the parts in `on` are read; their ranges are checked by
 * fx_init. */
typedef struct {
    uint32_t on;                                      /* FX_ON_* */
    uint32_t channels;                                /* 1, 2, 4 or 16; the audible buses need 1 or 2 */
    uint32_t block;                                   /* 32, 64 or 128: the grid of the send ramps and the limiter */
    uint32_t dc_millihz;                              /* 1 to 100000 */
    uint32_t chorus_base_us, chorus_depth_us, chorus_rate_millihz;   /* 7000-25000, 1000-5000, 100-3000 */
    uint32_t delay_us, delay_fb_q15, delay_lp_hz;     /* 50000-1000000, 0-29491, 20-20000 */
    uint32_t reverb_room, reverb_damp;                /* permille, 0-1000 each */
    uint32_t limiter_db10, limiter_release_ms;        /* threshold below full scale 0-200, release 1-5000 */
    int32_t  gain_db10;                               /* the master gain, -600 to 240 tenths of a dB  */
    uint32_t dither_seed;                             /* the dither's xorshift32 seed, 1 to 2^32 - 1  */
    uint32_t delay_hp_hz;                             /* the ping-pong's loop high-pass, 0 (none) to 2000  */
    uint32_t compressor_db10, compressor_ratio_tenths; /* attenuation 0-600, ratio 10-200 */
    uint32_t compressor_attack_ms, compressor_release_ms; /* 0-100, 1-5000 */
    uint32_t compressor_makeup_db10;                  /* positive gain 0-240 */
    uint32_t flanger_base_us, flanger_depth_us, flanger_rate_millihz; /* 100-10000, 100-base, 0-2000 */
    uint32_t flanger_fb_q15, flanger_invert;            /* 0-29491, 0 or 1 */
} fx_cfg_t;

typedef struct {
    int32_t *buf;                /* caller memory, mask + 1 entries, a power of two */
    uint32_t mask, len;          /* the delay in samples, at most mask + 1 */
    int32_t  store;              /* a comb's damping state */
} fx_line_t;

typedef struct {
    uint16_t level, gp, gt;      /* Q1.15: the level events set, last block's target, this block's */
} fx_send_t;

typedef struct {
    int32_t held;                                  /* last quantized Q2.30 sample */
    uint8_t left, bits, div, pending_bits, pending_div;
} fx_crush_t;

/* Raw return slots use network processing order, not FX_BUSES indices; duck has none. */
enum { FX_RETURN_CHORUS = 0, FX_RETURN_FLANGER, FX_RETURN_DELAY, FX_RETURN_REVERB, FX_RETURNS };

typedef struct fx {
    uint32_t  on;                /* FX_ON_*: the parts on */
    uint32_t  fs, ch;
    uint32_t  field_mode, bus_ch; /* explicit field init: output ch 2/4/16, exactly two return channels */
    uint32_t  piece_len;         /* collecting/returns-produced field piece length; zero when closed */
    uint32_t  n;                 /* the absolute sample counter, also every line's write index */
    uint32_t  k, log2k;          /* the block */
    uint32_t  open;              /* 0 closed, 1 collecting sends, 2 field returns produced */
    fx_send_t send[FX_MAX_VOICES][FX_BUSES];
    fx_crush_t crush[FX_MAX_VOICES];
    int32_t   vbuf[FX_MAX_BLOCK];                    /* fx_render_mono's scratch: one voice's block, Q2.30 */
    int64_t   dry[FX_MAX_BLOCK];                     /* the mono dry sum, Q2.30 */
    int64_t   bus[FX_BUSES][FX_MAX_BLOCK];           /* the bus sums, Q.45; 0 between pieces (fx_master clears what it reads) */
    uint32_t  dc_r;                                  /* the R, Q1.31 */
    int64_t   dc_x1[FX_MAX_CH], dc_y1[FX_MAX_CH];    /* the blocker's state, Q.45 */
    int64_t  *lim_ring;                              /* shared dynamics caller memory: ch * k delayed samples, Q.45 */
    uint64_t  lim_thr, lim_peak;                     /* the threshold and the running half-block peak, Q.45 */
    uint32_t  lim_c, lim_g0, lim_g1, lim_s;          /* release step, ramp ends, the safe gain two half-blocks back, Q1.30 */
    uint32_t  lim_mmask, lim_mul;                    /* half-block mask (k / 2 - 1) and 64 / (k / 2) */
    int32_t  *cho_buf;                               /* caller memory */
    uint32_t  cho_mask, cho_phase, cho_inc;          /* the triangle LFO: phase and step per sample */
    uint32_t  cho_base, cho_depth;                   /* Q16 samples */
    fx_line_t dly;                                   /* the delay line: T, or 2T for the ping-pong */
    uint32_t  dly_a, dly_fb;                         /* the loop's low-pass, Q1.31, and the feedback, Q1.15 */
    int32_t   dly_lp;                                /* the low-pass state */
    uint32_t  dly_t, dly_ha;                         /* the ping-pong's left tap T and its high-pass, Q1.31 (0: none) */
    int32_t   dly_hp;                                /* the high-pass's low-pass state lp2 */
    fx_line_t comb[2][8], allp[2][4];                /* the reverb, left then right */
    uint32_t  rv_fb, rv_damp;                        /* comb feedback and damping, Q1.31 */
    uint32_t  gain_m;                                /* the master gain's multiplier, unsigned Q4.28 */
    uint32_t  dith;                                  /* the dither's xorshift32 word, never 0 */
    uint64_t  comp_peak;                             /* pre-makeup linked detector, Q.45 */
    uint32_t  comp_db10, comp_ratio;                  /* threshold attenuation and ratio in tenths */
    uint32_t  comp_attack, comp_release;              /* half-block convergence coefficients, Q1.30 */
    uint32_t  comp_makeup;                           /* unsigned Q4.28, separate from attenuation */
    uint32_t  comp_g, comp_g0[2], comp_g1[2];         /* endpoint and half-ring-parity ramps, Q1.30 */
    uint32_t  comp_duck;                             /* full-block latched selection, including silent senders */
    int32_t  *flg_buf;                               /* one caller-owned line, both taps read before writing */
    uint32_t  flg_mask, flg_phase, flg_inc;
    uint32_t  flg_base, flg_depth, flg_fb, flg_invert;  /* Q16 samples, Q1.15 feedback, boolean */
} fx_t;

/* All calls run on one thread, fx_set_send included. A host changing
 * sends from another thread queues them as events; plain stores here
 * provide no cross-thread synchronization. fx_init clears the state and
 * used caller memory byte by byte; there is no fx_reset API. */
/* The caller memory a configuration needs at fs, in bytes, or 0 when it
 * needs none or is refused (as fx_init would). */
size_t   fx_mem_bytes(const fx_cfg_t *cfg, uint32_t fs);
/* fs 8000 to 192000, cfg within its ranges (no audible bus effect past 2
 * channels), mem 8-byte aligned with bytes >= fx_mem_bytes(cfg, fs): 0,
 * the memory cleared and every state at rest, the counter at 0 and every
 * send at 0. Else -1 and fx is unusable. */
int      fx_init(fx_t *fx, uint32_t fs, const fx_cfg_t *cfg, void *mem, size_t bytes);
/* Explicit field mode: output ch 2/4/16, two return networks; raw ch 4/16
 * refuses compressor/limiter/softclip/dither. Legacy init/refusals are unchanged. */
size_t   fx_field_mem_bytes(const fx_cfg_t *cfg, uint32_t fs);
int      fx_field_init(fx_t *fx, uint32_t fs, const fx_cfg_t *cfg, void *mem, size_t bytes);
/* A voice's send to a bus (FX_CHORUS, FX_DELAY, FX_REVERB, FX_DUCK, FX_FLANGER), Q1.15 up to
 * 32768, taken before the pan; it applies from the next block boundary on,
 * reached at that block's last sample (docs/INSTRUMENT.md, section 3). Voices at
 * or past FX_MAX_VOICES and other buses are ignored. */
void     fx_set_send(fx_t *fx, uint32_t voice, uint32_t bus, uint32_t level_q15);
/* Pending insert pair: unsigned bits clamp to 1..24 and div to 1..64;
 * voice >= FX_MAX_VOICES is ignored. At the next block boundary a changed
 * pair resets the hold; an identical pair preserves it. Initial 24/1 is
 * exact bypass. Note-off/retrigger do not reset this independent state. */
void     fx_set_crush(fx_t *fx, uint32_t voice, uint32_t bits, uint32_t div);
/* The mono path: renders voices 0 to count - 1 (at most FX_MAX_VOICES) for
 * frames frames and returns the clipped samples, in place of fm_mix_i16
 * when fx is configured. With channels 1 and no part on it gives
 * fm_mix_i16's bits. With more channels every channel carries the same
 * mono mix (and with 2 the buses' returns are stereo), written as frames
 * interleaved frames. Any frames count: pieces split at block boundaries
 * anchored to the counter. */
uint32_t fx_render_mono(fx_t *fx, fm_voice_t *voices, uint32_t count, int16_t *out, uint32_t frames);
/* The hooks for space/ (and any other mixer). Within one block piece of
 * len frames, which never crosses a boundary of fx's block: fx_voice_process
 * once per voice with its writable Q2.30 block, then fx_master once, with
 * the channels' wide sums at acc[c * stride + t] in Q2.30 (frac 30) or
 * Q.45 (frac 45); stride 0 gives every channel the same sum, acc NULL
 * silence. fx_master adds the returns, runs the master chain, writes len
 * interleaved frames of ch channels to out, advances the counter and
 * returns the clipped samples. With crush enabled every declared voice
 * must consume every frame, using zero input when its engine is skipped;
 * do not advance an inactive engine just to clock the insert. The mutable
 * hook applies the insert before every send; the caller then accumulates
 * the modified dry signal. Never call both hooks on the same voice/piece.
 * The original fx_voice_sends remains a const send-only reader for mixers
 * that do not adopt insert processing. fx_master advances fx->n for the
 * combined legacy path; fx_master_post does so for the field path. */
void     fx_voice_sends(fx_t *fx, uint32_t voice, const int32_t *buf, uint32_t len);
void     fx_voice_process(fx_t *fx, uint32_t voice, int32_t *buf, uint32_t len);
uint32_t fx_master(fx_t *fx, const int64_t *acc, uint32_t stride, uint32_t frac, int16_t *out, uint32_t len);
/* Trusted field-mode sequence: collect each voice once, produce returns once,
 * then post-master once for the same positive len <= k-(n mod k). No events
 * or later voice hooks may intervene; n+len must fit uint32. Returns write
 * Q27 at out[slot*bus_stride+ear*ear_stride+t], including inactive zero slots,
 * without advancing n. Post consumes Q45, adds no returns, advances n once
 * and closes the piece. Backing memory/configuration remain immutable. The
 * old fx_master is the combined legacy wrapper, not a field-mode operation. */
void     fx_field_returns(fx_t *fx, int32_t *out, uint32_t bus_stride, uint32_t ear_stride, uint32_t len);
uint32_t fx_master_post(fx_t *fx, const int64_t *acc_q45, uint32_t stride, int16_t *out, uint32_t len);

#ifdef __cplusplus
}
#endif

#endif /* FX_FX_H */
