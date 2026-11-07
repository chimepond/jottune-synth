/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * fx_bench.c — host benchmark of the effects module's cost (fx/SPEC.md,
 * section 6): ns per output frame of fx_render_mono at 48 kHz, block 64,
 * for each part alone and combined, against fm_mix_i16 and against the
 * module with no part on, with 1 and with 16 sustained voices sending 0.25
 * to every audible bus that is on (duck rows also send to the detector).
 * Compressor-only and compressor+limiter include mono/stereo/4/16-channel
 * synthetic masters. The least of five repetitions of 10 s of
 * audio each, interleaved across the configurations (the least, as other
 * work on the machine can only add time); also the caller memory of each
 * configuration, sizeof(fx_t) and each part's largest memory at 48 and
 * 192 kHz. A host tool:
 * it times with clock_gettime and is not part of the gate. From src/host/,
 * after `make SINE_BITS=12 EXP_BITS=10 PHASE64=1 ENV64=1`:
 *
 *   cc -std=c99 -O2 -DFM_SINE_BITS=12 -DFM_EXP_BITS=10 -DFM_PHASE64=1 \
 *      -DFM_ENV64=1 -I. -o build/fx_bench fx_bench.c \
 *      build/s12e10p1v1/fm_a1.o build/s12e10p1v1/fx.o && build/fx_bench
 */
#define _POSIX_C_SOURCE 199309L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "fm_a1.h"
#include "../fx/fx.h"

#define FS     48000u
#define FRAMES (10u * FS)
#define REPS   5
#define CHUNK  512u

static const fm_patch_t PATCH = { {
    { 65536u, 120, 0, 0, 128, 5, 800, 60, 300, 0, 0 },                  /* carrier, -12 dB */
    { 131072u, 0, 1536, 0, 128, 5, 1200, 120, 300, 0, 0 },              /* modulator, index 1.5 */
} };

static double now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}

static void voices_on(fm_voice_t *v, uint32_t nv)
{
    uint32_t i;
    for (i = 0; i < nv; i++) {
        fm_voice_init(&v[i], &PATCH, FS);
        fm_voice_note_on(&v[i], 110000u + 27500u * i, 20000);
    }
}

/* One repetition of one configuration, ns per frame; cfg NULL times
 * fm_mix_i16. */
static double run1(const fx_cfg_t *cfg, uint32_t nv, int duck)
{
    static fm_voice_t v[16];
    static int16_t out[CHUNK * FX_MAX_CH];
    size_t bytes = cfg ? fx_mem_bytes(cfg, FS) : 0;
    fx_t *fx = malloc(sizeof *fx);
    void *mem = bytes ? malloc(bytes) : NULL;   /* malloc's alignment is at least 8 */
    uint32_t done, i, b;
    double t0, t;
    if (!fx || (bytes && !mem)) { fprintf(stderr, "fx_bench: out of memory\n"); exit(1); }
    voices_on(v, nv);
    if (cfg) {
        if (fx_init(fx, FS, cfg, mem, bytes)) { fprintf(stderr, "fx_bench: fx_init refused\n"); exit(1); }
        for (i = 0; i < nv; i++)
            for (b = 0; b < FX_BUSES; b++)
                if (b != FX_DUCK || duck) fx_set_send(fx, i, b, 8192u);
    }
    t0 = now_ns();
    for (done = 0; done < FRAMES; done += CHUNK) {
        uint32_t len = FRAMES - done < CHUNK ? FRAMES - done : CHUNK;
        if (cfg) fx_render_mono(fx, v, nv, out, len);
        else     fm_mix_i16(v, nv, out, len);
    }
    t = (now_ns() - t0) / FRAMES;
    free(mem);
    free(fx);
    return t;
}

int main(void)
{
    static const struct { const char *name; uint32_t on, ch; int duck; } C[] = {
        { "fm_mix_i16",        0u,             0u, 0 },
        { "none",              0u,             1u, 0 },
        { "dcblock 5000",      FX_ON_DC,       1u, 0 },
        { "limiter 10 100",    FX_ON_LIMITER,  1u, 0 },
        { "softclip",          FX_ON_SOFTCLIP, 1u, 0 },
        { "chorus",            FX_ON_CHORUS,   1u, 0 },
        { "delay",             FX_ON_DELAY,    1u, 0 },
        { "reverb",            FX_ON_REVERB,   1u, 0 },
        { "all six",           0x3Fu,          1u, 0 },
        { "none, stereo",      0u,             2u, 0 },
        { "chorus, stereo",    FX_ON_CHORUS,   2u, 0 },
        { "reverb, stereo",    FX_ON_REVERB,   2u, 0 },
        { "all six, stereo",   0x3Fu,          2u, 0 },
        { "gain -120",         FX_ON_GAIN,     1u, 0 },
        { "dither",            FX_ON_DITHER,   1u, 0 },
        { "gain and dither",   FX_ON_GAIN | FX_ON_DITHER, 1u, 0 },
        { "all eight",         0x3Fu | FX_ON_GAIN | FX_ON_DITHER, 1u, 0 },
        { "all eight, stereo", 0x3Fu | FX_ON_GAIN | FX_ON_DITHER, 2u, 0 },
        { "none,4ch",          0u, 4u, 0 },
        { "none,16ch",         0u, 16u, 0 },
        { "compressor",        FX_ON_COMPRESSOR, 1u, 0 },
        { "compressor,stereo", FX_ON_COMPRESSOR, 2u, 0 },
        { "compressor,4ch",    FX_ON_COMPRESSOR, 4u, 0 },
        { "compressor,16ch",   FX_ON_COMPRESSOR, 16u, 0 },
        { "comp+lim",          FX_ON_COMPRESSOR | FX_ON_LIMITER, 1u, 0 },
        { "comp+lim,stereo",   FX_ON_COMPRESSOR | FX_ON_LIMITER, 2u, 0 },
        { "comp+lim,4ch",      FX_ON_COMPRESSOR | FX_ON_LIMITER, 4u, 0 },
        { "comp+lim,16ch",     FX_ON_COMPRESSOR | FX_ON_LIMITER, 16u, 0 },
        { "duck",              FX_ON_COMPRESSOR, 1u, 1 },
        { "duck,stereo",       FX_ON_COMPRESSOR, 2u, 1 },
        { "all nine",          0x3Fu | FX_ON_GAIN | FX_ON_DITHER | FX_ON_COMPRESSOR, 1u, 0 },
        { "all nine,stereo",   0x3Fu | FX_ON_GAIN | FX_ON_DITHER | FX_ON_COMPRESSOR, 2u, 0 },
        { "flanger",           FX_ON_FLANGER, 1u, 0 },
        { "flanger,stereo",    FX_ON_FLANGER, 2u, 0 },
        { "all ten",           0x3Fu | FX_ON_GAIN | FX_ON_DITHER | FX_ON_COMPRESSOR | FX_ON_FLANGER, 1u, 0 },
        { "all ten,stereo",    0x3Fu | FX_ON_GAIN | FX_ON_DITHER | FX_ON_COMPRESSOR | FX_ON_FLANGER, 2u, 0 },
    };
    enum { NC = sizeof C / sizeof C[0] };
    static const uint32_t NV[2] = { 1u, 16u };
    fx_cfg_t cfg[NC];
    double best[NC][2];
    size_t i, k;
    int r;
    for (i = 0; i < NC; i++) {
        memset(&cfg[i], 0, sizeof cfg[i]);
        cfg[i].on = C[i].on;
        cfg[i].channels = C[i].ch;
        cfg[i].block = 64u;
        cfg[i].dc_millihz = 5000u;
        cfg[i].chorus_base_us = 15000u, cfg[i].chorus_depth_us = 3000u, cfg[i].chorus_rate_millihz = 800u;
        cfg[i].delay_us = 187500u, cfg[i].delay_fb_q15 = 22938u, cfg[i].delay_lp_hz = 2500u;
        cfg[i].reverb_room = 500u, cfg[i].reverb_damp = 500u;
        cfg[i].limiter_db10 = 10u, cfg[i].limiter_release_ms = 100u;
        cfg[i].gain_db10 = -120, cfg[i].dither_seed = 1234567u;
        cfg[i].compressor_db10 = 120u, cfg[i].compressor_ratio_tenths = 40u;
        cfg[i].compressor_attack_ms = 5u, cfg[i].compressor_release_ms = 120u;
        cfg[i].compressor_makeup_db10 = 60u;
        cfg[i].flanger_base_us = 2000u, cfg[i].flanger_depth_us = 1000u, cfg[i].flanger_rate_millihz = 300u;
        cfg[i].flanger_fb_q15 = 20000u, cfg[i].flanger_invert = 0u;
        best[i][0] = best[i][1] = 1e30;
    }
    for (r = 0; r < REPS; r++)                   /* interleaved: drift in the machine's load reaches every configuration */
        for (i = 0; i < NC; i++)
            for (k = 0; k < 2; k++) {
                double t = run1(C[i].ch ? &cfg[i] : NULL, NV[k], C[i].duck);
                if (t < best[i][k]) best[i][k] = t;
            }
    printf("fx_bench: %u Hz, block 64, %u s per repetition, the least of %d; ns per frame\n", FS, FRAMES / FS, REPS);
    printf("%-18s %10s %10s %10s %10s %9s\n", "configuration", "1 voice", "over none", "16 voices", "over none", "bytes");
    for (i = 0; i < NC; i++) {
        size_t none = C[i].ch == 16u ? 19u : C[i].ch == 4u ? 18u : C[i].ch == 2u ? 9u : 1u;    /* the row with no part on, same channels */
        if (!C[i].ch) { printf("%-18s %10.2f %10s %10.2f\n", C[i].name, best[i][0], "", best[i][1]); continue; }
        printf("%-18s %10.2f %10.2f %10.2f %10.2f %9zu\n", C[i].name, best[i][0], best[i][0] - best[none][0],
               best[i][1], best[i][1] - best[none][1], fx_mem_bytes(&cfg[i], FS));
    }
    {   /* the largest memory each part takes, at 48 and 192 kHz */
        static const uint32_t FSS[2] = { 48000u, 192000u };
        fx_cfg_t m;
        printf("sizeof(fx_t) = %zu bytes; largest caller memory at 48000 and 192000 Hz:\n", sizeof(fx_t));
        for (k = 0; k < 2; k++) {
            memset(&m, 0, sizeof m);
            m.block = 128u, m.channels = 2u;
            m.on = FX_ON_CHORUS, m.chorus_base_us = 25000u, m.chorus_depth_us = 5000u, m.chorus_rate_millihz = 100u;
            printf("  %6u Hz: chorus 25000 5000 %zu", FSS[k], fx_mem_bytes(&m, FSS[k]));
            m.on = FX_ON_FLANGER, m.flanger_base_us = 10000u, m.flanger_depth_us = 10000u;
            printf(", flanger 10000 10000 %zu", fx_mem_bytes(&m, FSS[k]));
            m.on = FX_ON_DELAY, m.delay_us = 1000000u, m.delay_lp_hz = 20000u;
            printf(", delay 1000000 %zu", fx_mem_bytes(&m, FSS[k]));
            m.on = FX_ON_REVERB;
            printf(", reverb stereo %zu", fx_mem_bytes(&m, FSS[k]));
            m.on = FX_ON_LIMITER, m.channels = 16u, m.limiter_release_ms = 100u;
            printf(", limiter 16 channels block 128 %zu", fx_mem_bytes(&m, FSS[k]));
            m.on = FX_ON_COMPRESSOR, m.compressor_ratio_tenths = 40u, m.compressor_release_ms = 120u;
            printf(", compressor 16 channels block 128 %zu", fx_mem_bytes(&m, FSS[k]));
            m.on |= FX_ON_LIMITER;
            printf(", both %zu\n", fx_mem_bytes(&m, FSS[k]));
        }
    }
    return 0;
}
