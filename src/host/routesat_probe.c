/* Copyright (c) 2026 Nenad Micic */
/* SPDX-License-Identifier: Apache-2.0 */
/* Host probe, outside the gate (, lane  of 2026-10-11): does the
 * carrier-sum saturation bind in the routesat case, and does the saturated
 * render equal the clamped unsaturated sum? Renders the case through
 * fm_render_q30 with all three carriers, then once per single carrier; the
 * single renders summed in int64 are the unsaturated sum (the operators step
 * the same whatever the carrier mask, and the three envelopes idle together).
 * Also counts the samples the int16 output clip would catch, to show which
 * limit binds first.
 *
 * Build from src/host/ with the gate's knobs, after make has written the tables
 * header into the build directory:
 *   cc -std=c99 -O2 -DFM_SINE_BITS=12 -DFM_EXP_BITS=10 -DFM_PHASE64=1 \
 *      -DFM_ENV64=1 -I. -Ibuild/s12e10p1v1 routesat_probe.c fm_a1.c \
 *      -o build/s12e10p1v1/routesat_probe && build/s12e10p1v1/routesat_probe
 * Measured 2026-10-11: at 48000 the clamp binds on 7654 of 48000 samples, the
 * unsaturated sum peaks at 2.708 full scale, the saturated render equals the
 * clamped sum on every sample and the int16 clip binds on 16287 samples; at
 * 44100 the counts are 7030, 2.708, all and 14978. */
#include <stdio.h>
#include <stdint.h>
#include "fm_a1.h"

static const fm_patch_t P = {
    { { 65536, 0, 0, 0, 256, 5, 400, 20, 200, 0, 0, 0 },
      { 65536, 0, 0, 0, 256, 5, 400, 20, 200, 0, 0, 0 },
      { 131072, 0, 1024, 0, 256, 5, 400, 20, 200, 0, 0, 0 } },
    { 0, 0, 0 },
    { 3, 0x7, { [2] = { [0] = 256 } } } };

static void run(uint32_t fs, uint8_t carriers, int32_t *buf, uint32_t n)
{
    fm_voice_t v;
    fm_patch_t p = P;
    uint32_t on = fs * 6 / 10;
    p.route.carriers = carriers;
    fm_voice_init(&v, &p, fs);
    fm_voice_note_on(&v, 110000, 32767);
    fm_render_q30(&v, buf, on);
    fm_voice_note_off(&v);
    fm_render_q30(&v, buf + on, n - on);
}

static int32_t all[48000], one[3][48000];

int main(void)
{
    uint32_t fss[2] = { 48000, 44100 }, k, i;
    for (k = 0; k < 2; k++) {
        uint32_t fs = fss[k], n = fs, at = 0, over = 0, agree = 0, clip16 = 0, between = 0;
        int64_t umax = 0;
        run(fs, 0x7, all, n);
        for (i = 0; i < 3; i++) run(fs, (uint8_t)(1u << i), one[i], n);
        for (i = 0; i < n; i++) {
            int64_t u = (int64_t)one[0][i] + one[1][i] + one[2][i];
            int64_t s = u > 2147483647 ? 2147483647 : u < -2147483647 ? -2147483647 : u;
            int64_t r = ((int64_t)all[i] + (1 << 14)) >> 15;
            if (all[i] == 2147483647 || all[i] == -2147483647) at++;
            if (u > 2147483647 || u < -2147483647) over++;
            if (s == all[i]) agree++;
            if (r > 32767 || r < -32768) clip16++;
            if ((u > 2147483647 || u < -2147483647) == 0 && (((u + (1 << 14)) >> 15) > 32767 || ((u + (1 << 14)) >> 15) < -32768)) between++;
            if ((u < 0 ? -u : u) > umax) umax = u < 0 ? -u : u;
        }
        printf("fs=%u samples=%u at_clamp=%u unsat_over=%u sat_equals_clamped_unsat=%u/%u int16_clips=%u clipped_but_unsaturated=%u max_unsat=%lld (%.3f full scale)\n",
               fs, n, at, over, agree, n, clip16, between, (long long)umax, (double)umax / 1073741824.0);
    }
    return 0;
}
