/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * fx_ref.h — host-only double model of the effects module:
 * the same configuration, line lengths, send ramps, LFO
 * phase step and stage order as fx/, with the compressor's specified integer
 * control sequence evaluated from its own signal and other signal work in real
 * arithmetic: 's R
 * and the matched one-pole from exp, Freeverb's coefficients, the
 * limiter's threshold and the master gain from pow, exact interpolation,
 * no signal rounding and no saturation; compressor detection rounds its own
 * half-block peak to Q.45 before the control law. The dither is fx/'s integer sequence,
 * added to the unrounded output . Signals are in full-scale units (1.0 = int16 full scale);
 * outputs in int16 units, unclipped and unrounded. An external validation
 * tool : it includes the library header, never the reverse.
 */
#ifndef FX_REF_H
#define FX_REF_H

#include <stdint.h>
#include "../fx/fx.h"

typedef struct fxr fxr_t;

/* A model of cfg at fs (channels and block set as for fx_init), or NULL
 * when fx_init would refuse it or out of memory. */
fxr_t *fxr_new(const fx_cfg_t *cfg, uint32_t fs);
/* Independent field-mode signal model: two return networks, output ch2/4/16.
 * Native init is used only for configuration admission, never for signal or
 * hidden-state values. Returns do not advance the model clock; post does. */
fxr_t *fxr_field_new(const fx_cfg_t *cfg, uint32_t fs);
void   fxr_field_returns(fxr_t *r, const double *bus, double wet[FX_RETURNS][2]);
void   fxr_master_post(fxr_t *r, double duck, const double *dry, double *out);
void   fxr_free(fxr_t *r);
/* Host-only : call at a full-block boundary after independently
 * rolling the reference send gains. Nonzero selects the duck detector
 * for the whole block, even when silent; a new model defaults to master.
 * This communicates configuration, never a native signal/peak/gain. */
void   fxr_duck(fxr_t *r, int configured);
/* Independently maintained integer insert state. Call once for every
 * declared voice per frame, with zero input for a skipped engine, before
 * dry/pan/send accumulation. fxr_step alone advances the model clock. */
void   fxr_set_crush(fxr_t *r, uint32_t voice, uint32_t bits, uint32_t div);
int32_t fxr_crush(fxr_t *r, uint32_t voice, int32_t sample);
/* One frame: bus[FX_BUSES] the buses' inputs (the sends' sums), dry[ch]
 * the channels' dry sums; writes ch outputs to out, in int16 units. */
void   fxr_step(fxr_t *r, const double *bus, const double *dry, double *out);

#endif /* FX_REF_H */
