/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * Host-only  arithmetic and public-hook controls. Including the
 * actual implementations exposes their helpers without changing an API.
 * GCC/clang __int128 is an independent exact product/division oracle.
 * No audio file is written. Link the ordinary fm_a1 object and libm.
 */
#include <stdio.h>
#include <string.h>
#include "../../src/fx/fx.c"
#include "../../src/host/fx_ref.c"

__extension__ typedef __int128 wide_t;
static uint64_t checks, failures;
static uint32_t random_word = 0x43504d50u;
#define CHECK(x) do { checks++; if (!(x)) { \
    if (failures < 12) { fprintf(stderr, "COMPBOUND line=%d\n", __LINE__); } failures++; \
} } while (0)

static uint32_t draw(void)
{
    random_word ^= random_word << 13;
    random_word ^= random_word >> 17;
    random_word ^= random_word << 5;
    return random_word;
}

static wide_t floor_power(wide_t x, unsigned bits)
{
    wide_t den = (wide_t)1 << bits;
    return x < 0 ? -((-x + den - 1) / den) : x / den;
}

static fx_cfg_t config(uint32_t fs, uint32_t ch, uint32_t k)
{
    fx_cfg_t c;
    (void)fs;
    memset(&c, 0, sizeof c);
    c.on = FX_ON_COMPRESSOR;
    c.channels = ch; c.block = k;
    c.compressor_db10 = 120; c.compressor_ratio_tenths = 40;
    c.compressor_attack_ms = 5; c.compressor_release_ms = 120;
    c.compressor_makeup_db10 = 60;
    c.limiter_db10 = 10; c.limiter_release_ms = 100;
    return c;
}

static void arithmetic(void)
{
    uint32_t i, j;
    static const int64_t limits[] = {
        -((int64_t)1 << 58) + 1, -((int64_t)1 << 53) - 1, -4294967297LL,
        -4294967296LL, -1, 0, 1, 4294967295LL, 4294967296LL, ((int64_t)1 << 58) - 1
    };
    static const uint32_t multipliers[] = { 0, 1, (1u << 28), (1u << 30), 0x7fffffffu, 0x80000000u, UINT32_MAX };
    for (i = 0; i < sizeof limits / sizeof limits[0]; i++)
        for (j = 0; j < sizeof multipliers / sizeof multipliers[0]; j++)
            CHECK(fx_gain28(limits[i], multipliers[j]) == (int64_t)floor_power((wide_t)limits[i] * multipliers[j], 28));
    for (i = 0; i < 1000000u; i++) {
        uint64_t a = (((uint64_t)draw() << 32) | draw()) & (((uint64_t)1 << 62) - 1u);
        int64_t z = (i & 1u) ? -(int64_t)a : (int64_t)a;
        int64_t x = z / 16;
        uint32_t g = draw() % ((1u << 30) + 1u), m = draw();
        CHECK(fx_gain28(x, m) == (int64_t)floor_power((wide_t)x * m, 28));
        CHECK(fx_gain30(z, g) == (int64_t)floor_power((wide_t)z * g, 30));
        CHECK(fx_comp_peak(a, g) == (uint64_t)(((wide_t)a * g + ((wide_t)1 << 30) - 1) / ((wide_t)1 << 30)));
    }
}

static void controls(uint32_t fs)
{
    fx_cfg_t c = config(fs, 2, 64);
    fx_t *f = malloc(sizeof *f);
    fxr_t *r = fxr_new(&c, fs);
    void *memory = malloc(1024);
    uint32_t i, ratio, threshold;
    CHECK(f && r && memory);
    if (!f || !r || !memory) exit(2);
    CHECK(fx_init(f, fs, &c, memory, 1024) == 0);
    for (i = 0; i < 64; i++) CHECK(FX_LOG2[i] == r->comp_log[i]);
    for (i = 0; i < 11; i++) CHECK(FX_DB10[i] == r->comp_atten[i]);
    CHECK(r->comp_dbscale == 3945660);
    for (ratio = 10; ratio <= 200; ratio++) for (threshold = 0; threshold <= 600; threshold += 100) {
        f->comp_ratio = r->comp_ratio = ratio;
        f->comp_db10 = r->comp_db10 = threshold;
        for (i = 0; i < 75; i++) {
            uint64_t peak = i == 0 ? 0 : (((uint64_t)draw() << 20) | (draw() & 0xfffffu));
            if (i < 59 && i) peak = (uint64_t)1 << (i - 1);
            f->comp_peak = peak;
            r->comp_peak = (double)peak / 35184372088832.0;
            CHECK(fx_comp_target(f) == fxr_comp_target(r));
        }
    }
    for (i = 0; i <= 100; i++) {
        uint64_t samples = ((uint64_t)i * fs + 500) / 1000;
        uint64_t expected = !samples ? (uint64_t)1 << 30 : ((uint64_t)32 << 30) / samples;
        if (expected > ((uint64_t)1 << 30)) expected = (uint64_t)1 << 30;
        CHECK(fx_comp_time(i, fs, 32) == expected);
    }
    c = config(fs, 2, 64);
    CHECK(fx_mem_bytes(&c, fs) == 1024);
    c.on |= FX_ON_LIMITER;
    CHECK(fx_mem_bytes(&c, fs) == 1024);
    for (i = 0; i < 10; i++) {
        fx_cfg_t bad = c;
        switch (i) {
        case 0: bad.compressor_db10 = 601; break;
        case 1: bad.compressor_ratio_tenths = 9; break;
        case 2: bad.compressor_ratio_tenths = 201; break;
        case 3: bad.compressor_attack_ms = 101; break;
        case 4: bad.compressor_release_ms = 0; break;
        case 5: bad.compressor_release_ms = 5001; break;
        case 6: bad.compressor_makeup_db10 = 241; break;
        case 7: bad.compressor_db10 = UINT32_MAX; break;
        case 8: bad.compressor_attack_ms = UINT32_MAX; break;
        default: bad.compressor_makeup_db10 = UINT32_MAX; break;
        }
        CHECK(fx_init(f, fs, &bad, memory, 1024) == -1 && fx_mem_bytes(&bad, fs) == 0);
        bad.on &= ~FX_ON_COMPRESSOR;
        CHECK(fx_init(f, fs, &bad, memory, 1024) == 0);
    }
    fxr_free(r); free(memory); free(f);
}

static int64_t sample(uint32_t frame, uint32_t channel, unsigned pattern)
{
    uint32_t word = (frame * 1664525u + channel * 1013904223u) ^ (frame >> 3);
    int64_t value = (int64_t)(int32_t)(word & 0xffffu) - 32768;
    if (pattern == 0) return value * ((int64_t)1 << 22);  /* safely below threshold0 */
    if (pattern == 1) return value * ((int64_t)1 << 30);  /* approximately full scale */
    if (pattern == 2) return frame % 193u < 97u ? value * ((int64_t)1 << 42) : 0;
    return frame < 97u ? (channel & 1u ? -1 : 1) * (((int64_t)1 << 57) + 12345) : 0;
}

static void render(fx_t *f, int16_t *out, uint32_t count, unsigned pattern, int split)
{
    int64_t in[FX_MAX_CH * FX_MAX_BLOCK];
    uint32_t done = 0, partition_word = 0x70617274u;
    while (done < count) {
        uint32_t len = f->k - (f->n & (f->k - 1u)), c, t;
        if (len > count - done) len = count - done;
        if (split) {
            uint32_t piece;
            partition_word ^= partition_word << 13;
            partition_word ^= partition_word >> 17;
            partition_word ^= partition_word << 5;
            piece = 1u + partition_word % 19u;
            if (len > piece) len = piece;
        }
        for (c = 0; c < f->ch; c++)
            for (t = 0; t < len; t++) in[c * len + t] = sample(done + t, c, pattern);
        (void)fx_master(f, in, len, 45, out + done * f->ch, len);
        done += len;
    }
}

static void lifecycle(uint32_t fs)
{
    static const uint32_t channels[] = { 1, 2, 4, 16 }, blocks[] = { 32, 64, 128 };
    uint32_t ci, ki, setting, pattern, i;
    const uint32_t count = 1153;
    fx_t *a = malloc(sizeof *a), *b = malloc(sizeof *b);
    int16_t *oa = malloc((size_t)count * FX_MAX_CH * sizeof *oa), *ob = malloc((size_t)count * FX_MAX_CH * sizeof *ob);
    void *ma = malloc(16384), *mb = malloc(16384);
    CHECK(a && b && oa && ob && ma && mb);
    if (!a || !b || !oa || !ob || !ma || !mb) exit(2);
    for (ci = 0; ci < 4; ci++) for (ki = 0; ki < 3; ki++) for (setting = 0; setting < 12; setting++) {
        fx_cfg_t c = config(fs, channels[ci], blocks[ki]);
        c.compressor_db10 = setting % 3u == 0 ? 0u : setting % 3u == 1 ? 120u : 600u;
        c.compressor_ratio_tenths = setting % 3u == 0 ? 10u : setting % 3u == 1 ? 40u : 200u;
        c.compressor_attack_ms = setting % 3u == 0 ? 0u : setting % 3u == 1 ? 5u : 100u;
        c.compressor_release_ms = setting % 3u == 0 ? 1u : setting % 3u == 1 ? 120u : 5000u;
        c.compressor_makeup_db10 = setting % 3u == 0 ? 0u : setting % 3u == 1 ? 60u : 240u;
        if (setting & 1u) c.on |= FX_ON_LIMITER;
        for (pattern = 0; pattern < 4; pattern++) {
            size_t bytes = fx_mem_bytes(&c, fs);
            CHECK(bytes == (size_t)c.channels * c.block * 8u);
            CHECK(fx_init(a, fs, &c, ma, 16384) == 0 && fx_init(b, fs, &c, mb, 16384) == 0);
            if (setting >= 6) a->n = b->n = UINT32_MAX - c.block + 1u;
            render(a, oa, count, pattern, 0); render(b, ob, count, pattern, 1);
            for (i = 0; i < count * c.channels; i++) {
                CHECK(oa[i] == ob[i]);
                if (c.on & FX_ON_LIMITER) {
                    int64_t q = oa[i];
                    CHECK(q >= -29205 && q <= 29205); /* threshold -1dB, rounded */
                }
            }
            CHECK(a->n == b->n && a->comp_peak == b->comp_peak && a->comp_g == b->comp_g
                  && a->lim_peak == b->lim_peak && a->lim_g0 == b->lim_g0 && a->lim_g1 == b->lim_g1 && a->lim_s == b->lim_s);
            for (i = 0; i < 2; i++) CHECK(a->comp_g0[i] == b->comp_g0[i] && a->comp_g1[i] == b->comp_g1[i]);
            CHECK(memcmp(ma, mb, bytes) == 0); /* ring samples, no structure padding */
        }
    }
    for (ci = 0; ci < 4; ci++) for (ki = 0; ki < 3; ki++) {
        fx_cfg_t c = config(fs, channels[ci], blocks[ki]);
        c.on = FX_ON_LIMITER;
        CHECK(fx_init(a, fs, &c, ma, 16384) == 0);
        c.on |= FX_ON_COMPRESSOR;
        c.compressor_db10 = 0; c.compressor_makeup_db10 = 0;
        CHECK(fx_init(b, fs, &c, mb, 16384) == 0);
        render(a, oa, count, 0, 0); render(b, ob, count, 0, 1);
        for (i = 0; i < count * c.channels; i++) CHECK(oa[i] == ob[i]);
        CHECK(a->lim_g0 == b->lim_g0 && a->lim_g1 == b->lim_g1 && a->lim_s == b->lim_s);
    }
    free(a); free(b); free(oa); free(ob); free(ma); free(mb);
}

static void linked(uint32_t fs)
{
    static const uint32_t channels[] = { 2, 4, 16 };
    uint32_t k, i, c;
    for (k = 0; k < 3; k++) {
        fx_cfg_t cfg = config(fs, channels[k], 64);
        fx_t *f = malloc(sizeof *f);
        fxr_t *r;
        void *memory = malloc(8192);
        cfg.compressor_attack_ms = 0; cfg.compressor_makeup_db10 = 0;
        r = fxr_new(&cfg, fs);
        CHECK(f && r && memory);
        if (!f || !r || !memory) exit(2);
        CHECK(fx_init(f, fs, &cfg, memory, 8192) == 0);
        for (i = 0; i < 257; i++) {
            int64_t input[FX_MAX_CH];
            int16_t output[FX_MAX_CH];
            double dry[FX_MAX_CH], model[FX_MAX_CH], bus[FX_BUSES] = { 0 };
            for (c = 0; c < cfg.channels; c++) {
                input[c] = i < 96 ? (c == cfg.channels - 1 ? (int64_t)1 << 45 : (int64_t)1 << 40) : 0;
                dry[c] = (double)input[c] / 35184372088832.0;
            }
            (void)fx_master(f, input, 1, 45, output, 1);
            fxr_step(r, bus, dry, model);
            CHECK(f->comp_g == r->comp_g && f->comp_g0[0] == r->comp_g0[0]
                  && f->comp_g0[1] == r->comp_g0[1] && f->comp_g1[0] == r->comp_g1[0]
                  && f->comp_g1[1] == r->comp_g1[1]);
            if (i == 32) CHECK(f->comp_g < (1u << 30)); /* last channel drives every channel */
            for (c = 0; c < cfg.channels; c++) {
                if (i < cfg.block) CHECK(output[c] == 0); /* exactly one shared ring */
                CHECK(fabs(output[c] - model[c]) <= 0.501);
            }
        }
        fxr_free(r); free(f); free(memory);
    }
}

/* The existing limiter record's three tones/events, extended to two
 * seconds to include the complete release tail. This is a diagnostic
 * candidate, not a new golden or permission to change an old hash. */
static void audio(uint32_t fs, int fast, int limiter)
{
    fx_cfg_t c = config(fs, 1, 64);
    fm_patch_t patch;
    fm_voice_t *voices = calloc(3, sizeof *voices);
    fx_t *f = malloc(sizeof *f);
    fxr_t *r;
    void *memory = malloc(512);
    uint32_t i, j, clipped = 0, native_end = 0, model_end = 0;
    double error = 0, rounding = 0, peak = 0, loss;
    if (fast) { c.compressor_ratio_tenths = 200; c.compressor_attack_ms = 0; }
    if (limiter) c.on |= FX_ON_LIMITER;
    r = fxr_new(&c, fs);
    memset(&patch, 0, sizeof patch);
    for (i = 0; i < 2; i++) {
        patch.op[i].ratio_q16 = 65536; patch.op[i].attack_ms = 5;
        patch.op[i].decay_ms = 500; patch.op[i].release_ms = 300;
    }
    patch.op[0].vel_sens_q8 = 256;
    CHECK(voices && f && r && memory);
    if (!voices || !f || !r || !memory) exit(2);
    CHECK(fx_init(f, fs, &c, memory, 512) == 0);
    for (i = 0; i < 3; i++) fm_voice_init(&voices[i], &patch, fs);
    for (i = 0; i < 2u * fs; i++) {
        int64_t sum = 0;
        int16_t native;
        double bus[FX_BUSES] = { 0 }, dry, model, quantized, e;
        if (i == 0) fm_voice_note_on(&voices[0], 220000, 16384);
        if (i == 3u * fs / 10u) fm_voice_note_on(&voices[1], 277183, 32767);
        if (i == 6u * fs / 10u) fm_voice_note_on(&voices[2], 329628, 32767);
        if (i == fs) fm_voice_note_off(&voices[2]);
        if (i == 12u * fs / 10u) fm_voice_note_off(&voices[1]);
        if (i == 14u * fs / 10u) fm_voice_note_off(&voices[0]);
        for (j = 0; j < 3; j++) {
            int32_t x;
            fm_render_q30(&voices[j], &x, 1);
            sum += x;
        }
        clipped += fx_master(f, &sum, 0, 30, &native, 1);
        dry = (double)sum / 1073741824.0;
        fxr_step(r, bus, &dry, &model);
        CHECK(f->comp_g == r->comp_g && f->comp_g0[0] == r->comp_g0[0]
              && f->comp_g0[1] == r->comp_g0[1] && f->comp_g1[0] == r->comp_g1[0]
              && f->comp_g1[1] == r->comp_g1[1]);
        quantized = nearbyint(model);
        if (quantized > 32767) quantized = 32767;
        if (quantized < -32768) quantized = -32768;
        e = native - model; error += e * e;
        e = quantized - model; rounding += e * e;
        e = fabs(native - (model > 32767 ? 32767 : model < -32768 ? -32768 : model));
        if (e > peak) peak = e;
        if (native) native_end = i + 1;
        if (floor(model + 0.5) != 0) model_end = i + 1;
    }
    loss = error > 0 && rounding > 0 ? 10 * log10(error / rounding) : 0;
    printf("COMPAUDIO fs=%u fast=%d limiter=%d loss=%.9f peak=%.9f lastnz=%u/%u clipped=%u\n",
           fs, fast, limiter, loss, peak, native_end, model_end, clipped);
    CHECK(loss <= 0.10 && peak <= 1 && native_end == model_end);
    fxr_free(r); free(f); free(memory); free(voices);
}

/* Public-hook duck exercise. Its send model rolls independent levels,
 * divides the nonnegative ramp numerator, and sums Q30 * Q15 directly. */
static void duck_run(fx_t *f, fxr_t *r, int16_t *pcm, uint32_t frames, unsigned pattern, int split)
{
    uint32_t level[FX_MAX_VOICES] = { 0 }, previous[FX_MAX_VOICES] = { 0 }, target[FX_MAX_VOICES] = { 0 };
    uint32_t done = 0, rng = 0x4455434bu, configured = 0;
    uint64_t peak = 0;
    while (done < frames) {
        uint32_t len = f->k - done % f->k, t, c, v, until;
        int32_t voices[FX_MAX_VOICES][FX_MAX_BLOCK];
        int64_t input[FX_MAX_CH][FX_MAX_BLOCK];
        /* Off-grid changes are staged until the next absolute boundary.
         * The zero target's last ramp still selects duck for that block. */
        if (done == 0) {
            if (pattern == 1) level[63] = 32768;
            if (pattern == 2) level[0] = level[1] = 32768;
            if (pattern == 3) for (v = 0; v < FX_MAX_VOICES; v++) level[v] = 32768;
        }
        if (pattern == 4) {
            if (done == 9) level[63] = 32768;
            if (done == 2u*f->k + 3u) level[63] = 0;
            if (done == 4u*f->k + 7u) level[0] = 32768;
            if (done == 6u*f->k + 1u) level[0] = 0;
        }
        for (v = 0; v < FX_MAX_VOICES; v++) fx_set_send(f, v, FX_DUCK, level[v]);
        fx_set_send(f, FX_MAX_VOICES, FX_DUCK, 32768); /* invalid indices must be inert */
        fx_set_send(f, 0, FX_BUSES, 32768);
        if (len > frames - done) len = frames - done;
        until = 9;
        if (done < until && len > until - done) len = until - done;
        for (v = 2; v <= 6; v += 2) {
            until = v*f->k + (v == 2 ? 3u : v == 4 ? 7u : 1u);
            if (done < until && len > until - done) len = until - done;
        }
        if (split) {
            rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
            if (len > 1u + rng % 13u) len = 1u + rng % 13u;
        }
        for (t = 0; t < len; t++) {
            uint32_t n = done + t;
            for (c = 0; c < f->ch; c++) input[c][t] = (c & 1u ? -1 : 1) * (((int64_t)1 << 44) + (n % 7u ? 0 : (int64_t)1 << 43));
            for (v = 0; v < FX_MAX_VOICES; v++) {
                int32_t x = n % 3u ? 1073741824 : -1073741824;
                if (pattern == 1 || (pattern == 4 && v == 63)) x = 0; /* configured inactive sender */
                if (pattern == 2 && v == 1) x = -x;
                voices[v][t] = x;
            }
        }
        for (v = 0; v < FX_MAX_VOICES; v++)
            if (!(pattern == 1 || (pattern == 4 && v == 63))) fx_voice_sends(f, v, voices[v], len);
        (void)fx_master(f, &input[0][0], FX_MAX_BLOCK, 45, pcm + done*f->ch, len);
        for (t = 0; t < len; t++) {
            uint32_t n = done + t, j = n % f->k;
            int64_t bus_sum = 0;
            double bus[FX_BUSES] = { 0 }, dry[FX_MAX_CH], model[FX_MAX_CH];
            if (j == 0) {
                configured = 0;
                for (v = 0; v < FX_MAX_VOICES; v++) {
                    previous[v] = n == 0 ? level[v] : target[v]; target[v] = level[v];
                    if (previous[v] || target[v]) configured = 1;
                }
                fxr_duck(r, (int)configured);
            }
            if (n % (f->k/2u) == 0) peak = 0;
            for (v = 0; v < FX_MAX_VOICES; v++) {
                int64_t numerator = (int64_t)previous[v]*f->k + ((int64_t)target[v]-previous[v])*(j+1u);
                uint32_t gain = (uint32_t)(numerator/f->k);
                bus_sum += (int64_t)voices[v][t]*gain;
            }
            if (configured) {
                uint64_t magnitude = bus_sum < 0 ? (uint64_t)-bus_sum : (uint64_t)bus_sum;
                if (magnitude > peak) peak = magnitude;
            }
            for (c = 0; c < f->ch; c++) {
                uint64_t magnitude = input[c][t] < 0 ? (uint64_t)-input[c][t] : (uint64_t)input[c][t];
                if (!configured && magnitude > peak) peak = magnitude;
                dry[c] = (double)input[c][t]/35184372088832.0;
            }
            bus[FX_DUCK] = (double)bus_sum/35184372088832.0;
            fxr_step(r, bus, dry, model);
            for (c = 0; c < f->ch; c++) CHECK(fabs((double)pcm[n*f->ch+c] - model[c]) <= 0.501);
        }
        CHECK(f->comp_duck == configured && f->comp_peak == peak);
        CHECK(f->comp_g == r->comp_g && f->comp_g0[0] == r->comp_g0[0]
              && f->comp_g0[1] == r->comp_g0[1] && f->comp_g1[0] == r->comp_g1[0]
              && f->comp_g1[1] == r->comp_g1[1]);
        for (v = 0; v < FX_MAX_VOICES; v++) {
            CHECK(f->send[v][FX_DUCK].level == level[v] && f->send[v][FX_DUCK].gp == previous[v]
                  && f->send[v][FX_DUCK].gt == target[v]);
        }
        for (t = 0; t < FX_MAX_BLOCK; t++) CHECK(f->bus[FX_DUCK][t] == 0);
        if (pattern == 1 || pattern == 2) CHECK(f->comp_g == (1u << 30));
        if (pattern == 3 && done == 0) CHECK(peak == ((uint64_t)64 << 45)); /* no audible-bus saturation */
        done += len;
    }
}

static void duck(uint32_t fs)
{
    static const uint32_t channels[] = { 1, 2, 4, 16 }, blocks[] = { 32, 64, 128 };
    uint32_t ci, ki, pattern, i;
    uint64_t start = checks, bad = failures;
    fx_t *a = malloc(sizeof *a), *b = malloc(sizeof *b);
    void *ma = malloc(16384), *mb = malloc(16384);
    int16_t *oa = malloc(1200u*FX_MAX_CH*sizeof *oa), *ob = malloc(1200u*FX_MAX_CH*sizeof *ob);
    CHECK(a && b && ma && mb && oa && ob);
    if (!a || !b || !ma || !mb || !oa || !ob) exit(2);
    for (ci = 0; ci < 4; ci++) for (ki = 0; ki < 3; ki++) for (pattern = 0; pattern < 5; pattern++) {
        fx_cfg_t cfg = config(fs, channels[ci], blocks[ki]);
        fxr_t *ra, *rb;
        uint32_t count = 9u*cfg.block + 17u;
        cfg.compressor_attack_ms = 0; cfg.compressor_release_ms = 1; cfg.compressor_makeup_db10 = 0;
        CHECK(fx_init(a, fs, &cfg, ma, 16384) == 0 && fx_init(b, fs, &cfg, mb, 16384) == 0);
        ra = fxr_new(&cfg, fs); rb = fxr_new(&cfg, fs);
        CHECK(ra && rb);
        if (!ra || !rb) exit(2);
        CHECK(a->comp_duck == 0 && ra->comp_duck == 0 && rb->comp_duck == 0);
        for (i = 0; i < FX_MAX_VOICES; i++) CHECK(a->send[i][FX_DUCK].level == 0 && a->send[i][FX_DUCK].gp == 0 && a->send[i][FX_DUCK].gt == 0);
        duck_run(a, ra, oa, count, pattern, 0); duck_run(b, rb, ob, count, pattern, 1);
        for (i = 0; i < count*cfg.channels; i++) CHECK(oa[i] == ob[i]);
        CHECK(a->n == b->n && a->comp_peak == b->comp_peak && a->comp_g == b->comp_g && a->comp_duck == b->comp_duck);
        for (i = 0; i < cfg.channels*cfg.block; i++) CHECK(a->lim_ring[i] == b->lim_ring[i]);
        fxr_free(ra); fxr_free(rb);
    }
    printf("DUCKBOUND fs=%u checks=%llu failures=%llu\n", fs,
           (unsigned long long)(checks-start), (unsigned long long)(failures-bad));
    free(a); free(b); free(ma); free(mb); free(oa); free(ob);
}

int main(void)
{
    printf("COMPSIZE fx_t=%zu fx_cfg_t=%zu buses=%u\n", sizeof(fx_t), sizeof(fx_cfg_t), (unsigned)FX_BUSES);
    arithmetic();
    controls(48000); controls(44100);
    lifecycle(48000); lifecycle(44100);
    linked(48000); linked(44100);
    duck(48000); duck(44100);
    audio(48000, 0, 0); audio(44100, 0, 0);
    audio(48000, 1, 0); audio(44100, 1, 0);
    audio(48000, 0, 1); audio(44100, 0, 1);
    audio(48000, 1, 1); audio(44100, 1, 1);
    printf("COMPBOUND checks=%llu failures=%llu\n", (unsigned long long)checks, (unsigned long long)failures);
    return failures ? 1 : 0;
}
