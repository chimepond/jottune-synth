/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * field.h — the spatial module's field routes (docs/INSTRUMENT.md, spatial routes; ): the voices encoded into an ambisonic field at order 1 or
 * 3 (AmbiX: ACN channel order, SN3D, no Condon-Shortley phase), the field
 * turned by the listener's yaw, then written out as 4 or 16 channels or
 * decoded to two ears by the caller's FIR table; Brown and Duda's pinna per
 * voice ahead of the encoder; and the stereo pan law (c), whose far ear
 * never falls silent. space.h's rules hold: integer only, no libm, no heap,
 * no mutable global state, bit-exact across targets.
 *
 * The field state starts with an sp_stereo_t (geo): the voices' positions,
 * moves, block and counter are space.h's, set with sp_stereo_set_pos and
 * sp_stereo_move on &f->geo (or on the state's address cast to
 * sp_stereo_t *, which C99 6.7.2.1 makes the same pointer). Units: as
 * space.h, plus the yaw in millidegrees (the azimuth the listener faces,
 * clockwise from +y), the field's gains Q1.23, the pinna's taps Q1.22 and
 * the field Q2.30.
 */
#ifndef SP_FIELD_H
#define SP_FIELD_H

#include "space.h"
#include "../fx/fx.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SP_FIELD_MAX_CH 16       /* order 3: (3 + 1)^2 channels */
#define SP_FIELD_TAPS   256      /* the decoder's FIR length per channel */
#define SP_PINNA_TAPS   21       /* the longest echo, 19.6 samples at 48 kHz, and its right neighbour */
#define SP_PINNA_HIST   32       /* the pinna's delay line per voice, a power of 2 above SP_PINNA_TAPS */
#define SP_PINNA_FS_MAX 48000
#define SP_YAW_MAX      360000   /* the yaw statement's bound, millidegrees; the API takes any int32 */
#define SP_CORDIC_N     30

enum { SP_FIELD_AMB1 = 1, SP_FIELD_AMB3, SP_FIELD_BINAURAL, SP_FIELD_PANLAW };
enum { SP_PINNA_OFF = 0, SP_PINNA_PBNH, SP_PINNA_RD, SP_PINNA_D };

typedef struct {
    int            route;        /* SP_FIELD_* */
    uint32_t       order;        /* SP_FIELD_BINAURAL: 1 or 3; amb1 and amb3 carry theirs */
    uint32_t       block, near_mm;   /* as sp_stereo_init */
    uint32_t       fs;           /* the sample rate: the pinna's delays scale with it; at most SP_PINNA_FS_MAX with the pinna */
    int32_t        yaw_mdeg;     /* the first yaw, as sp_field_set_yaw */
    int            pinna;        /* SP_PINNA_*; not with SP_FIELD_PANLAW */
    uint32_t       pinna_d[5];   /* SP_PINNA_D: Brown and Duda's D per echo in thousandths, 0 to 1000 */
    uint32_t       panlaw_q15;   /* SP_FIELD_PANLAW: the width W, 1 to 32768 (32768: space.c's law) */
    const int16_t *table;        /* SP_FIELD_BINAURAL: the left ear's FIRs, [channel][SP_FIELD_TAPS] */
    uint32_t       table_shift;  /* SP_FIELD_BINAURAL: the coefficients' fraction bits, 1 to 30 */
} sp_field_cfg_t;

typedef struct {
    int32_t  gt[SP_FIELD_MAX_CH], gp[SP_FIELD_MAX_CH];   /* field gains Q1.23 (pan law: Q1.15), this boundary's and the last */
    int32_t  ht[SP_PINNA_TAPS], hp[SP_PINNA_TAPS];       /* pinna taps Q1.22, likewise */
    int32_t  hs[SP_PINNA_TAPS];                          /* the taps' ramps within the block, times the block */
    int32_t  hist[SP_PINNA_HIST];                        /* pinna input, Q2.30 dry/Q5.27 wet, at the absolute counter mod 32 */
    uint8_t  tap[SP_PINNA_TAPS];                         /* the taps nonzero at either boundary */
    uint32_t ntap, quiet;                                /* quiet: zero samples fed since the voice was last active */
    int32_t  cx, cy, cz, cyaw;                           /* the inputs the targets came from */
    uint32_t cvalid;
} sp_fvoice_t;

typedef struct {
    sp_stereo_t    geo;                  /* first: positions, moves, the block and the counter */
    int            route;
    uint32_t       nsh, ch;              /* field channels; output channels (4, 16 or 2) */
    uint32_t       fs;                   /* retained for field-FX attachment validation */
    int            pinna;
    uint32_t       pd[5];                /* the pinna's D, thousandths */
    uint32_t       aq[5], bq[5];         /* the echoes' delays' A and B in samples, Q6.26, at this rate */
    uint32_t       w;                    /* pan law width, Q1.15 */
    const int16_t *table;
    uint32_t       shift, odd;           /* the table's shift; bit c set for the m < 0 channels */
    uint64_t       dround, dbias;        /* the ears' rounding: 2^(shift + 14), 2^(47 - shift) */
    int32_t        yaw;
    int32_t        cm[4], sm[4];         /* cos and sin of m * yaw, m = 1 to 3, Q30 */
    int32_t        sq3, sq3_2, sq58, sq38, sq15_2;   /* SH constants, Q30: sqrt 3, sqrt 3 / 2, sqrt 5/8, sqrt 3/8, sqrt 15 / 2 */
    int32_t        ang[SP_CORDIC_N], kinv;           /* CORDIC: atan 2^-i and the gain's inverse, Q30 */
    uint32_t       halfpi, pi;           /* Q30 radians */
    int32_t        sh[SP_FIELD_MAX_CH];  /* scratch: one voice's harmonics, Q30 */
    int32_t        tc, ts;               /* scratch: the last CORDIC rotation's cos and sin */
    sp_fvoice_t    fv[SP_MAX_VOICES];
    sp_fvoice_t    wet[FX_RETURNS][2];     /* fixed world left/right sources, separate from musical voices */
    int32_t        wetbuf[FX_RETURNS][2][SP_MAX_BLOCK]; /* caller-owned Q5.27 FX returns */
    int32_t        buf[SP_MAX_BLOCK], pbuf[SP_MAX_BLOCK];   /* one voice's block; after its pinna (Q4.28) */
    int64_t        acc[SP_FIELD_MAX_CH][SP_MAX_BLOCK];      /* the field's accumulators */
    int32_t        fld[SP_FIELD_MAX_CH][SP_MAX_BLOCK];      /* the binaural route's field, Q2.30 */
    int32_t        hist[SP_FIELD_MAX_CH][2 * SP_FIELD_TAPS];   /* the decoder's history, each sample twice */
    int64_t        eo[2][SP_MAX_BLOCK];                     /* the ears' even and odd sums */
} sp_field_t;

/* 0, or -1 on a bad configuration (the state is then unusable). Calls
 * sp_stereo_init: every voice at (0, 1000, 0), the counter at 0. */
int      sp_field_init(sp_field_t *f, const sp_field_cfg_t *cfg);
/* Attach an initialized field-mode FX state at both counters zero, or -1
 * without changing the old pointer. PANLAW is refused. Binaural FIRs must
 * have sum(abs(taps)) <= 2^(table_shift+6)-1. NULL detaches only at n=0.
 * Validated tables/configuration/backing storage remain immutable and valid
 * throughout attachment; initialize field BEFORE attaching, also on reset.
 * Render calls are single-threaded, event-split, with n+frames<=UINT32_MAX.
 * sp_field_decode remains decoder-only and must not be interleaved with an
 * attached render: it does not consume or advance FX. */
int      sp_field_attach_fx(sp_field_t *f, fx_t *fx);
/* The listener's yaw from the next block boundary on: the field turns so
 * that a source at azimuth a (clockwise from ahead) is heard at a - yaw.
 * The pinna sees the turned direction. */
void     sp_field_set_yaw(sp_field_t *f, int32_t mdeg);
/* Renders voices 0 to count - 1 (the same from call to call) for frames
 * frames into f->ch interleaved int16 channels per frame and returns the
 * clipped samples: the output's saturations, plus the field's on the
 * binaural route. Chunking never changes the bits. */
uint32_t sp_field_render(sp_field_t *f, fm_voice_t *voices, uint32_t count, int16_t *out, uint32_t frames);
/* The same with the sources given as Q2.30 samples, src[t * stride + i]
 * for voice i at frame t, every voice active: stems and tests. Samples are
 * copied from const input, then processed once before pinna and sends. */
uint32_t sp_field_render_q30(sp_field_t *f, const int32_t *src, uint32_t stride, uint32_t count, int16_t *out, uint32_t frames);

/* Test surface: the binaural decoder alone on a Q2.30 field of f->nsh
 * interleaved channels, frames frames to 2 * frames int16 at the counter,
 * which it advances. Returns the ears' saturations. */
uint32_t sp_field_decode(sp_field_t *f, const int32_t *fld, uint32_t frames, int16_t *out);

#ifdef __cplusplus
}
#endif

#endif /* SP_FIELD_H */
