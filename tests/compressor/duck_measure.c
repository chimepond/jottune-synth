/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 * Host-only decomposition of the pinned duck fixture. Reuse the actual
 * native reader and spatial dry placement; compare two independent real
 * compressor instances, one specified ratio and one unity ratio. Both
 * receive the kick's own Q30 source as the detector, never native FX state.
 * Including fx_ref exposes only the reference trace, not an adopted oracle.
 */
#define main compressor_harness_main
#include "../../src/host/harness.c"
#undef main
#include "../../src/host/fx_ref.c"

int main(int argc, char **argv)
{
    tl_t *tl = calloc(1, sizeof *tl);
    fm_voice_t *detector = calloc(2, sizeof *detector), *bass = calloc(2, sizeof *bass);
    sp_stereo_t *sp = calloc(1, sizeof *sp);
    fxr_t *atten = NULL, *unity = NULL;
    fx_cfg_t cfg;
    uint32_t i, e = 0, sends = 0, min_gain = 1u << 30;
    double energy = 0, baseline = 0;
    int rc = 1;
    if (argc != 2 || !tl || !detector || !bass || !sp) goto end;
    if (tl_read(tl, argv[1], 1) || tl_check_header(tl) || tl_read(tl, argv[1], 2)) goto end;
    if (tl->nvoices != 2 || tl->space != 1 || tl->route || tl->fx.on != FX_ON_COMPRESSOR || tl->gen_clock) goto end;
    qsort(tl->ev, tl->nev, sizeof *tl->ev, tl_cmp);
    for (i = 0; i < 2; i++) {
        if (tl_voice_init(tl, &detector[i], i) || tl_voice_init(tl, &bass[i], i)) goto end;
    }
    if (sp_stereo_init(sp, tl->block, tl->near)) goto end;
    cfg = tl->fx; atten = fxr_new(&cfg, tl->fs);
    cfg.compressor_ratio_tenths = 10; unity = fxr_new(&cfg, tl->fs);
    if (!atten || !unity) goto end;
    for (i = 0; i < tl->n; i++) {
        int32_t kick;
        int16_t ignored[2];
        double bus[FX_BUSES] = { 0 }, dry[2], out[2], ref[2];
        for (; e < tl->nev && tl->ev[e].sample == i; e++) {
            const tl_event_t *event = &tl->ev[e];
            tl_apply(detector, NULL, NULL, event);
            /* Keep all spatial events, but never start the kick in the
             * bass-only dry pool. Voice1 retains exact note/event timing. */
            if (event->voice == 1 || event->type == EV_POS || event->type == EV_MOVE)
                tl_apply(bass, sp, NULL, event);
            if (event->type == EV_SEND) {
                if (event->voice != 0 || event->bus != FX_DUCK || event->sample || event->level_q15 != 32768 || ++sends != 1) goto end;
            }
        }
        if (sends != 1) goto end; /* the actual fixture must configure this detector */
        fm_render_q30(&detector[0], &kick, 1);
        (void)sp_stereo_render(sp, bass, 2, ignored, 1);
        if (i % tl->block == 0) { fxr_duck(atten, 1); fxr_duck(unity, 1); }
        bus[FX_DUCK] = (double)kick / 1073741824.0; /* full, fixed authored send */
        dry[0] = (double)sp->acc[0][0]/35184372088832.0;
        dry[1] = (double)sp->acc[1][0]/35184372088832.0;
        fxr_step(atten, bus, dry, out); fxr_step(unity, bus, dry, ref);
        if (atten->comp_g < min_gain) min_gain = atten->comp_g;
        energy += out[0]*out[0] + out[1]*out[1];
        baseline += ref[0]*ref[0] + ref[1]*ref[1];
        if (unity->comp_g != (1u << 30)) goto end;
    }
    if (!(baseline > energy && energy > 0 && min_gain < (1u << 30))) goto end;
    printf("DUCKBASS fs=%u frames=[0,%u) channels=2 weights=equal samples=%llu latency=%u "
           "rms_unity_lsb=%.9f rms_duck_lsb=%.9f reduction_db=%.9f min_gain_q30=%u min_gain_db=%.9f\n",
           tl->fs, tl->n, (unsigned long long)tl->n*2u, tl->block,
           sqrt(baseline/((double)tl->n*2)), sqrt(energy/((double)tl->n*2)),
           10*log10(baseline/energy), min_gain, -20*log10((double)min_gain/1073741824.0));
    rc = 0;
end:
    if (rc) fprintf(stderr, "DUCKBASS failed fixture/decomposition check\n");
    if (tl) { free(tl->ev); free(tl->ks_pool); free(tl->stems); }
    free(tl); free(detector); free(bass); free(sp); fxr_free(atten); fxr_free(unity);
    return rc;
}
