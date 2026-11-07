/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 * Host-only binary probe. No host helper is linked into gen.c. */
#include "gen.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { uint8_t data[GEN_WORD_CAP + 1024]; size_t n; } bytes_t;
static void put(bytes_t *b, uint64_t x, unsigned n)
{
    unsigned i;
    for (i = 0; i < n; i++) { b->data[b->n++] = (uint8_t)x; x >>= 8; }
}
static int flush(bytes_t *b)
{
    return fwrite(b->data, 1, b->n, stdout) == b->n;
}

/* Independent vector protocol documented in reference.py. Maps expose the
 * exact full recurrence coordinates after each operation/update. */
static int vector(const gen_source_t *s, const gen_memory_t *m)
{
    bytes_t out;
    unsigned i;
    out.n = 0;
    put(&out, s->config.kind, 1); put(&out, s->random, 4);
    put(&out, (uint32_t)s->raw[0], 4); put(&out, (uint32_t)s->raw[1], 4);
    switch (s->config.kind) {
    case GEN_PINK:
        put(&out, s->state.pink.counter, 2); put(&out, (uint32_t)s->state.pink.sum, 4);
        for (i = 0; i < 16; i++) put(&out, (uint16_t)s->state.pink.row[i], 2);
        break;
    case GEN_BROWN:
        put(&out, (uint64_t)s->state.map.x, 8); put(&out, s->state.map.count, 8); break;
    case GEN_MULTI:
        put(&out, (uint64_t)s->config.p[2], 1);
        for (i = 0; i < (unsigned)s->config.p[2]; i++) put(&out, s->state.multi.position[i], 4);
        for (i = 0; i < 256; i++) put(&out, (uint16_t)s->state.multi.motif[i], 2);
        break;
    case GEN_LOGISTIC: put(&out, (uint64_t)s->state.map.x, 8); break;
    case GEN_HENON:
        put(&out, (uint64_t)s->state.map.x, 8); put(&out, (uint64_t)s->state.map.y, 8);
        put(&out, s->state.map.count, 8); break;
    case GEN_WHITE: break;
    default:
        put(&out, s->state.seq.step, 4); put(&out, s->state.seq.length, 4); put(&out, s->state.seq.rotation, 4);
        if (s->config.kind == GEN_FIBONACCI || s->config.kind == GEN_LSYSTEM)
            for (i = 0; i < s->state.seq.length; i++) put(&out, m->word[s->state.seq.word_slot][i], 1);
        break;
    }
    return flush(&out);
}

static int number(const char *text, int64_t *out)
{
    char *end;
    long long value;
    errno = 0; value = strtoll(text, &end, 10);
    if (errno || !*text || *end) return 0;
    *out = (int64_t)value; return 1;
}

typedef struct {
    gen_bank_t bank;
    gen_memory_t memory;
    uint8_t scratch[2][GEN_WORD_CAP], word[2][GEN_WORD_CAP];
} fixture_t;

static void memory_init(fixture_t *f)
{
    unsigned i;
    memset(f, 0, sizeof *f);
    for (i = 0; i < 2; i++) {
        f->memory.scratch[i] = f->scratch[i]; f->memory.scratch_capacity[i] = GEN_WORD_CAP;
        f->memory.word[i] = f->word[i]; f->memory.word_capacity[i] = GEN_WORD_CAP;
    }
}

static void full_program(gen_program_t *p)
{
    unsigned i;
    memset(p, 0, sizeof *p);
    p->sources = 11;
    for (i = 0; i < 11; i++) (void)gen_defaults(&p->source[i], (uint8_t)(i + 1), (uint8_t)i, i + 1);
    p->texts = 1; p->text[0].id = 10; p->text[0].depth = 5; p->text[0].rule_mask = 3;
    strcpy(p->text[0].axiom, "AB"); strcpy(p->text[0].rule[0], "AB"); strcpy(p->text[0].rule[1], "A");
    p->source[0].div = 6; p->source[0].slew = 512;
    p->source[6].p[0] = 1; p->source[6].p[1] = 4; p->source[6].div = 6;
    p->lanes = 1;
    p->lane[0].gate = 6; p->lane[0].pitch = 1; p->lane[0].accent = -1;
    p->lane[0].voice = 7; p->lane[0].step_ticks = 6; p->lane[0].gate_ticks = 6;
    p->lane[0].base_cents = 6900; p->lane[0].span_cents = 2400;
    p->lane[0].velocity = 32767; p->lane[0].scale_mask = 0xab5;
}

static int frame_output(const gen_frame_t *frame)
{
    bytes_t out;
    unsigned i, axis;
    out.n = 0;
    put(&out, frame->frame, 8); put(&out, frame->events, 1);
    for (i = 0; i < GEN_SOURCES; i++) for (axis = 0; axis < 2; axis++) put(&out, (uint32_t)frame->value[i][axis], 4);
    for (i = 0; i < frame->events; i++) {
        const gen_event_t *e = &frame->event[i];
        put(&out, e->voice, 2); put(&out, e->lane, 1); put(&out, e->on, 1);
        put(&out, (uint32_t)e->cents, 4); put(&out, e->velocity, 2);
    }
    return flush(&out);
}

static int scenario(const char *mode)
{
    fixture_t f;
    gen_program_t p;
    gen_frame_t frame;
    uint8_t snapshot[GEN_SNAPSHOT_CAP];
    size_t n = 0;
    unsigned j;
    memory_init(&f); full_program(&p);
    if (!strcmp(mode, "resume")) {
        n = fread(snapshot, 1, sizeof snapshot, stdin);
        if (ferror(stdin) || !feof(stdin) || gen_restore(&f.bank, &p, &f.memory, snapshot, n).code) return 1;
    } else {
        if (gen_init(&f.bank, &p, &f.memory, 48000, 64).code) return 1;
        if (!strcmp(mode, "checkpoint")) {
            for (j = 0; j < 65; j++) if (gen_tick(&f.bank, &frame).code) return 1;
            if (gen_set(&f.bank, 0, GEN_PARAM_ENABLED, 0).code || gen_reset(&f.bank, 0, 12345).code) return 1;
            if (gen_save(&f.bank, snapshot, sizeof snapshot, &n).code) return 1;
            return fwrite(snapshot, 1, n, stdout) == n ? 0 : 1;
        }
    }
    for (j = 0; j < 4096; j++) {
        if (!strcmp(mode, "scenario")) {
            if (j == 7 && gen_set(&f.bank, 0, GEN_PARAM_ENABLED, 0).code) return 1;
            if (j == 20 && gen_set(&f.bank, 0, GEN_PARAM_ENABLED, 1).code) return 1;
            if (j == 24 && (gen_reset(&f.bank, 0, 0).code || gen_reset(&f.bank, 0, 12345).code)) return 1;
            if (j == 31 && gen_set(&f.bank, 0, GEN_PARAM_SLEW, 17).code) return 1;
            if (j == 13 && gen_lane_enable(&f.bank, 0, 0).code) return 1;
            if (j == 17 && gen_lane_enable(&f.bank, 0, 1).code) return 1;
        }
        if (gen_tick(&f.bank, &frame).code || !frame_output(&frame)) return 1;
    }
    if (!strcmp(mode, "resume")) {
        if (gen_save(&f.bank, snapshot, sizeof snapshot, &n).code) return 1;
        if (fwrite(snapshot, 1, n, stdout) != n) return 1;
    }
    return 0;
}

static int word_probe(int argc, char **argv)
{
    fixture_t f;
    gen_program_t p;
    int64_t depth;
    int i;
    if (argc < 4 || argc > 12 || strlen(argv[2]) > 32 || !number(argv[3], &depth) || depth < 0 || depth > 16) return 2;
    memory_init(&f); memset(&p, 0, sizeof p);
    p.sources = p.texts = 1;
    (void)gen_defaults(&p.source[0], GEN_LSYSTEM, 0, 1);
    p.text[0].depth = (uint8_t)depth; strcpy(p.text[0].axiom, argv[2]);
    for (i = 4; i < argc; i++) {
        unsigned symbol;
        const char *rhs;
        if (strlen(argv[i]) < 2 || argv[i][0] < 'A' || argv[i][0] > 'H' || argv[i][1] != '=') return 2;
        symbol = (unsigned)(argv[i][0] - 'A'); rhs = argv[i] + 2;
        if (p.text[0].rule_mask & (1u << symbol)) return 2;
        p.text[0].rule_mask |= (uint8_t)(1u << symbol);
        if (!strcmp(rhs, "-")) rhs = "";
        if (strlen(rhs) > 32) return 2;
        strcpy(p.text[0].rule[symbol], rhs);
    }
    if (gen_init(&f.bank, &p, &f.memory, 48000, 64).code) return 2;
    return fwrite(f.word[0], 1, f.bank.word_length[0], stdout) == f.bank.word_length[0] ? 0 : 1;
}

static int euclidean_probe(void)
{
    fixture_t f;
    gen_program_t p;
    gen_frame_t frame;
    unsigned n, k, rot, j, failures = 0, tests = 0;
    memory_init(&f); memset(&p, 0, sizeof p); p.sources = 1;
    (void)gen_defaults(&p.source[0], GEN_EUCLID, 0, 1);
    for (n = 1; n <= 64; n++) for (k = 0; k <= n; k++) {
        uint8_t expected[64];
        unsigned bucket = 0;
        for (j = 0; j < n; j++) { bucket += k; expected[j] = (uint8_t)(bucket >= n); if (bucket >= n) bucket -= n; }
        for (rot = 0; rot < n; rot++) {
            unsigned hits = 0, first = n, previous = n, minimum_gap = n, maximum_gap = 0;
            p.source[0].seed = n; p.source[0].p[0] = k; p.source[0].p[1] = n; p.source[0].p[2] = rot;
            if (gen_init(&f.bank, &p, &f.memory, 48000, 64).code) return 1;
            for (j = 0; j < n; j++) {
                unsigned hit;
                if (gen_tick(&f.bank, &frame).code) return 1;
                hit = frame.value[0][0] == 32767;
                tests++; if (frame.value[0][0] != 32767 * expected[(j + rot) % n]) failures++;
                if (hit) {
                    hits++;
                    if (first == n) first = j;
                    if (previous != n) {
                        const unsigned gap = j - previous;
                        if (gap < minimum_gap) minimum_gap = gap;
                        if (gap > maximum_gap) maximum_gap = gap;
                    }
                    previous = j;
                }
            }
            if (hits) {
                const unsigned gap = n - previous + first;
                if (gap < minimum_gap) minimum_gap = gap;
                if (gap > maximum_gap) maximum_gap = gap;
            }
            tests++; if (hits != k || (k && (minimum_gap < n/k || maximum_gap > (n+k-1)/k))
                || f.bank.source[0].state.seq.step != 0) failures++;
        }
    }
    printf("EUCLIDEAN checks=%u failures=%u\n", tests, failures);
    return failures ? 1 : 0;
}

/* Host-only exact state table: collisions are resolved by both coordinates.
 * 2^22 slots *24 bytes=96 MiB, under the256 MiB per-profile budget. */
typedef struct { int64_t x, y; uint32_t last, used; } cycle_slot_t;
/*  (3): up to two parameter overrides feed the detector a chosen map,
 * the negative controls' short cycle and clamped map; without them the map
 * keeps gen_defaults' parameters and the gate's line is unchanged. */
static int chaos(unsigned kind, uint32_t seed, unsigned overrides, const int64_t *value)
{
    fixture_t f;
    gen_program_t p;
    gen_frame_t frame;
    cycle_slot_t *table;
    unsigned i, failure = 0;
    uint64_t digest = UINT64_C(0xcbf29ce484222325);
    int32_t minimum = 32767, maximum = -32768;
    if (kind != GEN_LOGISTIC && kind != GEN_HENON) return 2;
    memset(&p, 0, sizeof p); p.sources = 1;
    (void)gen_defaults(&p.source[0], (uint8_t)kind, 0, seed);
    for (i = 0; i < overrides; i++) p.source[0].p[i] = value[i];
    memory_init(&f);
    if (gen_init(&f.bank, &p, &f.memory, 48000, 64).code) return 1;
    for (i = 0; i < 65536; i++) if (gen_tick(&f.bank, &frame).code) return 1;
    table = calloc(1u << 22, sizeof *table);
    if (!table) { fprintf(stderr, "INCOMPLETE exact-cycle memory unavailable\n"); return 1; }
    for (i = 0; i < 2000000; i++) {
        uint64_t h;
        unsigned slot;
        const gen_source_t *s = &f.bank.source[0];
        if (gen_tick(&f.bank, &frame).code) { failure = 1; break; }
        if (s->raw[0] < minimum) minimum = s->raw[0]; if (s->raw[0] > maximum) maximum = s->raw[0];
        {
            unsigned axis, byte;
            for (axis = 0; axis < (kind == GEN_HENON ? 2u : 1u); axis++) {
                uint64_t coordinate = (uint64_t)(axis ? s->state.map.y : s->state.map.x);
                for (byte = 0; byte < 8; byte++, coordinate >>= 8)
                    digest = (digest ^ (uint8_t)coordinate) * UINT64_C(0x100000001b3);
            }
        }
        h = (uint64_t)s->state.map.x ^ ((uint64_t)s->state.map.y * UINT64_C(0x9e3779b97f4a7c15));
        h ^= h >> 30; h *= UINT64_C(0xbf58476d1ce4e5b9); h ^= h >> 27;
        slot = (unsigned)h & ((1u << 22) - 1u);
        while (table[slot].used && (table[slot].x != s->state.map.x || table[slot].y != s->state.map.y)) slot = (slot + 1u) & ((1u << 22) - 1u);
        if (table[slot].used && i - table[slot].last < 1000000u) { failure = 1; break; }
        table[slot].used = 1; table[slot].last = i; table[slot].x = s->state.map.x; table[slot].y = s->state.map.y;
    }
    free(table);
    if (minimum == maximum || f.bank.source[0].state.map.count) failure = 1;
    printf("CHAOS kind=%u seed=%u samples=%u minimum=%d maximum=%d clamps=%llu bytes=%lu hash=%016llx result=%s\n",
           kind, seed, i, minimum, maximum, (unsigned long long)f.bank.source[0].state.map.count,
           (unsigned long)((1u << 22) * sizeof(cycle_slot_t)), (unsigned long long)digest, failure ? "FAIL" : "PASS");
    return failure ? 1 : 0;
}

static void checksum(uint8_t *data, size_t n)
{
    uint64_t h = UINT64_C(0xcbf29ce484222325);
    size_t i;
    for (i = 0; i < n - 8; i++) h = (h ^ data[i]) * UINT64_C(0x100000001b3);
    for (i = n - 8; i < n; i++, h >>= 8) data[i] = (uint8_t)h;
}

static int lifecycle_probe(void)
{
    fixture_t a, b;
    gen_program_t p, other;
    gen_frame_t fa, fb;
    uint8_t sa[GEN_SNAPSHOT_CAP], sb[GEN_SNAPSHOT_CAP], bad[GEN_SNAPSHOT_CAP];
    size_t na = 0, nb = 0;
    unsigned tests = 0, failures = 0, i, j, k;
#define CHECK(c) do { tests++; if (!(c)) { if (failures < 16) fprintf(stderr, "LIFECYCLE line=%d\n", __LINE__); failures++; } } while (0)
    memory_init(&a); memory_init(&b);
    /* Length/rotation changes and reset at the same due tick use the new
     * recurrence configuration. This expectation does not inspect helpers. */
    for (k = GEN_EUCLID; k <= GEN_CANTOR; k++) for (i = 0; i < 2; i++) {
        memset(&p, 0, sizeof p); p.sources = 1;
        CHECK(!gen_defaults(&p.source[0], (uint8_t)k, 0, 1).code);
        p.source[0].div = 3;
        CHECK(!gen_init(&a.bank, &p, &a.memory, 48000, 64).code);
        CHECK(!gen_tick(&a.bank, &fa).code);
        if (k == GEN_EUCLID) CHECK(!gen_set(&a.bank, 0, GEN_PARAM_P0, 1).code);
        CHECK(!gen_set(&a.bank, 0, k == GEN_EUCLID ? GEN_PARAM_P1 : GEN_PARAM_P0, 7).code);
        CHECK(!gen_set(&a.bank, 0, k == GEN_EUCLID ? GEN_PARAM_P2 : GEN_PARAM_P1, 2).code);
        if (i) CHECK(!gen_reset(&a.bank, 0, 9).code);
        CHECK(!gen_tick(&a.bank, &fa).code && !gen_tick(&a.bank, &fa).code);
        CHECK(a.bank.source[0].state.seq.length != 7);
        CHECK(!gen_tick(&a.bank, &fa).code);
        CHECK(a.bank.source[0].state.seq.length == 7 && a.bank.source[0].state.seq.rotation == (i ? 4u : 3u)
              && a.bank.source[0].state.seq.step == (i ? 1u : 2u));
        CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code);
        CHECK(!gen_restore(&b.bank, &p, &b.memory, sa, na).code);
    }
    /* Independent source and lane phase periods, including DIV before LCM. */
    for (k = 0; k < 3; k++) {
        static const unsigned lengths[3][3] = {{5,7,11},{4,6,0},{4,0,0}};
        static const unsigned periods[3] = {385,12,24};
        uint8_t hits[770][3];
        memset(&p, 0, sizeof p);
        for (i = 0; i < 3 && lengths[k][i]; i++) {
            CHECK(!gen_defaults(&p.source[i], GEN_EUCLID, (uint8_t)i, lengths[k][i]).code);
            p.source[i].p[0] = 1; p.source[i].p[1] = lengths[k][i]; p.source[i].div = k == 2 ? 6 : 1;
            p.lane[i].id = (uint8_t)i; p.lane[i].voice = (uint16_t)(10 + i);
            p.lane[i].gate = (int8_t)i; p.lane[i].pitch = p.lane[i].accent = -1;
            p.lane[i].step_ticks = p.lane[i].gate_ticks = k == 2 ? 6 : 1;
            p.lane[i].velocity = 32767; p.lane[i].base_cents = 6900;
            p.sources++; p.lanes++;
        }
        CHECK(!gen_init(&a.bank, &p, &a.memory, 48000, 64).code);
        memset(hits, 0, sizeof hits);
        for (j = 0; j < 2 * periods[k]; j++) {
            CHECK(!gen_tick(&a.bank, &fa).code);
            for (i = 0; i < fa.events; i++) if (fa.event[i].on) {
                CHECK(fa.event[i].voice == 10 + fa.event[i].lane);
                hits[j][fa.event[i].lane] = 1;
            }
            for (i = 0; i < p.sources; i++) {
                const unsigned div = p.source[i].div, step = j / div;
                const unsigned expected = j % div == 0 && step % lengths[k][i] == lengths[k][i] - 1;
                CHECK(hits[j][i] == expected);
                CHECK(a.bank.source[i].state.seq.step == ((j / div + 1) % lengths[k][i]));
            }
            if (j >= periods[k]) CHECK(!memcmp(hits[j], hits[j-periods[k]], sizeof hits[j]));
        }
        for (j = 1; j < periods[k]; j++) {
            unsigned same = 1;
            for (i = 0; i < periods[k]; i++) if (memcmp(hits[i], hits[(i+j)%periods[k]], 3)) { same = 0; break; }
            CHECK(!same);
        }
    }
    /* Parent output is the just-published lower-ID value; a blocked child
     * advances normally. Adding unrelated state does not perturb a source. */
    memset(&p, 0, sizeof p); p.sources = 2;
    for (i = 0; i < 2; i++) {
        CHECK(!gen_defaults(&p.source[i], GEN_EUCLID, (uint8_t)i, 6+i).code);
        p.source[i].p[0] = 1; p.source[i].p[1] = 3+i;
    }
    p.source[1].parent = 0;
    CHECK(!gen_init(&a.bank, &p, &a.memory, 48000, 64).code);
    for (j = 0; j < 120; j++) {
        const unsigned parent = j % 3 == 2, child = (j + 3) % 4 == 3;
        CHECK(!gen_tick(&a.bank, &fa).code);
        CHECK(fa.value[1][0] == (parent && child ? 32767 : 0));
        CHECK(a.bank.source[1].state.seq.step == (j+1)%4);
    }
    memset(&p, 0, sizeof p); p.sources = 1;
    CHECK(!gen_defaults(&p.source[0], GEN_PINK, 0, 12345).code);
    other = p; other.sources = 2; CHECK(!gen_defaults(&other.source[1], GEN_MULTI, 15, 991).code);
    CHECK(!gen_init(&a.bank, &p, &a.memory, 48000, 64).code);
    CHECK(!gen_init(&b.bank, &other, &b.memory, 48000, 64).code);
    for (i = 0; i < 4096; i++) {
        CHECK(!gen_tick(&a.bank, &fa).code && !gen_tick(&b.bank, &fb).code);
        CHECK(!memcmp(&a.bank.source[0], &b.bank.source[0], sizeof(gen_source_t)));
    }
    /* A repaired integrity hash must not bypass field validation. Offsets
     * below address the documented 44-byte header and first white source. */
    p.source[0].kind = GEN_WHITE;
    CHECK(!gen_init(&a.bank, &p, &a.memory, 48000, 64).code);
    CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code);
    for (i = 0; i < 9; i++) {
        memcpy(bad, sa, na);
        switch (i) {
        case 0: bad[4] = 2; break; /* unsupported version */
        case 1: bad[8] ^= 1; break; /* wrong program digest */
        case 2: memset(bad+16, 0, 4); break; /* fs0 */
        case 3: memset(bad+20, 0, 4); break; /* ctl0 */
        case 4: memset(bad+51, 0, 2); break; /* DIV0 */
        case 5: bad[55] = 1; break; /* unused scalar */
        case 6: bad[88] = 1; break; /* unknown pending flag */
        case 7: memset(bad+128, 0, 4); break; /* zero PRNG word */
        default: bad[132] = 2; break; /* enabled outside boolean */
        }
        checksum(bad, na);
        CHECK(gen_restore(&a.bank, &p, &a.memory, bad, na).code == GEN_SNAPSHOT);
        CHECK(!gen_save(&a.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    }
    /* Capacity is independent of definition order, including an excessive
     * text source whose sorted word slot is already out of range. */
    other = p; other.sources = 3;
    for (i = 0; i < 3; i++) CHECK(!gen_defaults(&other.source[i], GEN_FIBONACCI, (uint8_t)(2-i), 1).code);
    CHECK(gen_init(&a.bank, &other, &a.memory, 48000, 64).code == GEN_CAPACITY);
    CHECK(!gen_save(&a.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    /* Common configuration endpoints, all eight simultaneous lane offs/ons,
     * and checked clock/counter additions before any output mutation. */
    memset(&p, 0, sizeof p); p.sources = 1;
    CHECK(!gen_defaults(&p.source[0], GEN_WHITE, 15, 0).code);
    p.source[0].div = 65535; p.source[0].slew = 512;
    CHECK(!gen_init(&a.bank, &p, &a.memory, UINT32_MAX, 4096).code);
    CHECK(!gen_tick(&a.bank, &fa).code && fa.frame == 0 && fa.value[15][0] >= -512 && fa.value[15][0] <= 512);
    CHECK(!gen_init(&a.bank, &p, &a.memory, 1, 1).code);
    CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code);
    for (i = 0; i < 6; i++) {
        other = p;
        switch (i) {
        case 0: other.source[0].div = 0; break;
        case 1: other.source[0].slew = 513; break;
        case 2: other.source[0].id = 16; break;
        case 3: other.sources = 17; break;
        case 4: other.lanes = 9; break;
        default: other.texts = 3; break;
        }
        CHECK(gen_init(&a.bank, &other, &a.memory, 48000, 64).code != GEN_OK);
        CHECK(!gen_save(&a.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    }
    p.lanes = GEN_LANES;
    for (i = 0; i < GEN_LANES; i++) {
        p.lane[i].id = (uint8_t)i; p.lane[i].voice = (uint16_t)(UINT16_MAX-i);
        p.lane[i].gate = p.lane[i].pitch = p.lane[i].accent = -1;
        p.lane[i].step_ticks = p.lane[i].gate_ticks = 1;
        p.lane[i].velocity = i ? 32767 : 1; p.lane[i].base_cents = i ? 14400 : -4800;
    }
    CHECK(!gen_init(&a.bank, &p, &a.memory, 48000, 1).code);
    CHECK(!gen_tick(&a.bank, &fa).code && fa.events == 8);
    CHECK(!gen_tick(&a.bank, &fa).code && fa.events == 16);
    for (i = 0; i < 8; i++) CHECK(fa.event[i].on == 0 && fa.event[i].lane == i && fa.event[i+8].on == 1 && fa.event[i+8].lane == i);
    CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code);
    for (i = 0; i < 9; i++) {
        other = p;
        switch (i) {
        case 0: other.lane[0].step_ticks = 0; break;
        case 1: other.lane[0].gate_ticks = 0; break;
        case 2: other.lane[0].gate_ticks = 2; break;
        case 3: other.lane[0].velocity = 0; break;
        case 4: other.lane[0].velocity = 32768; break;
        case 5: other.lane[0].span_cents = 9601; break;
        case 6: other.lane[0].scale_mask = 4096; break;
        case 7: other.lane[0].base_cents = -4801; break;
        default: other.lane[1].base_cents = 14401; break;
        }
        CHECK(gen_init(&a.bank, &other, &a.memory, 48000, 64).code == GEN_RANGE);
        CHECK(!gen_save(&a.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    }
    a.bank.tick = UINT64_MAX-1u; a.bank.lane[0].next_step = a.bank.tick; a.bank.lane[0].config.step_ticks = 2;
    CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code); memset(&fa, 0xa5, sizeof fa); fb = fa;
    CHECK(gen_tick(&a.bank, &fa).code == GEN_OVERFLOW && !memcmp(&fa, &fb, sizeof fa));
    CHECK(!gen_save(&a.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    memset(&p, 0, sizeof p); p.sources = 1;
    CHECK(!gen_defaults(&p.source[0], GEN_HENON, 0, 1).code);
    CHECK(!gen_init(&a.bank, &p, &a.memory, 48000, 1).code);
    a.bank.source[0].state.map.x = 4 * GEN_Q48; a.bank.source[0].state.map.count = UINT64_MAX;
    CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code); memset(&fa, 0xa5, sizeof fa); fb = fa;
    CHECK(gen_tick(&a.bank, &fa).code == GEN_OVERFLOW && !memcmp(&fa, &fb, sizeof fa));
    CHECK(!gen_save(&a.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    /* Canonical ordering does not depend on caller definition order. */
    full_program(&p); other = p;
    for (i = 0; i < p.sources; i++) other.source[i] = p.source[p.sources-1u-i];
    CHECK(!gen_init(&a.bank, &p, &a.memory, 48000, 64).code);
    CHECK(!gen_init(&b.bank, &other, &b.memory, 48000, 64).code);
    CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code);
    CHECK(!gen_save(&b.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    CHECK(!gen_restore(&b.bank, &other, &b.memory, sa, na).code);
    for (j = 0; j < 4096; j++) CHECK(!gen_tick(&a.bank, &fa).code && !gen_tick(&b.bank, &fb).code && !memcmp(&fa,&fb,sizeof fa));
    /* Genuine continuation detector controls: each omission remains a valid
     * snapshot, but changes the subsequent output/event stream. */
    full_program(&p);
    CHECK(!gen_init(&a.bank, &p, &a.memory, 48000, 64).code);
    for (j = 0; j < 73; j++) CHECK(!gen_tick(&a.bank, &fa).code);
    CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code);
    for (k = 0; k < 5; k++) {
        unsigned different = 0;
        CHECK(!gen_restore(&a.bank, &p, &a.memory, sa, na).code);
        CHECK(!gen_restore(&b.bank, &p, &b.memory, sa, na).code);
        switch (k) {
        case 0: b.bank.source[1].state.pink.counter = 0; break;
        case 1: b.bank.source[0].random = 1; break;
        case 2: b.bank.source[0].published[0] = 0; break;
        case 3: b.bank.source[1].state.pink.sum -= b.bank.source[1].state.pink.row[0]; b.bank.source[1].state.pink.row[0] = 0; break;
        default: b.bank.lane[0].active = 0; b.bank.lane[0].off_tick = 0; break;
        }
        CHECK(!gen_save(&b.bank, sb, sizeof sb, &nb).code);
        CHECK(!gen_restore(&b.bank, &p, &b.memory, sb, nb).code);
        for (j = 0; j < 4096; j++) {
            CHECK(!gen_tick(&a.bank, &fa).code && !gen_tick(&b.bank, &fb).code);
            if (memcmp(&fa, &fb, sizeof fa)) different = 1;
        }
        CHECK(different);
    }
    printf("LIFECYCLE checks=%u failures=%u\n", tests, failures);
#undef CHECK
    return failures ? 1 : 0;
}

static int selftest(void)
{
    fixture_t a, b;
    gen_program_t program, invalid;
    gen_frame_t fa, fb;
    uint8_t sa[GEN_SNAPSHOT_CAP], sb[GEN_SNAPSHOT_CAP], bad[GEN_SNAPSHOT_CAP];
    size_t na = 0, nb = 0;
    unsigned tests = 0, failures = 0, i, j;
    size_t largest_snapshot;
    static const unsigned checkpoints[] = { 0, 1, 63, 64, 65, 4095, 4096, 65535, 65536 };
#define CHECK(condition) do { tests++; if (!(condition)) { if (failures < 16) fprintf(stderr, "SELFTEST line=%d\n", __LINE__); failures++; } } while (0)
    memset(&program, 0, sizeof program);
    memory_init(&a); memory_init(&b);
    program.sources = 11;
    for (i = 0; i < 11; i++) CHECK(!gen_defaults(&program.source[i], (uint8_t)(i + 1), (uint8_t)i, i + 1).code);
    program.texts = 1; program.text[0].id = 10; program.text[0].depth = 5; program.text[0].rule_mask = 3;
    strcpy(program.text[0].axiom, "AB"); strcpy(program.text[0].rule[0], "AB"); strcpy(program.text[0].rule[1], "A");
    program.source[0].div = 6; program.source[0].slew = 512;
    program.source[6].p[0] = 1; program.source[6].p[1] = 4; program.source[6].div = 6;
    program.lanes = 1;
    program.lane[0].gate = 6; program.lane[0].pitch = 1; program.lane[0].accent = -1;
    program.lane[0].voice = 7; program.lane[0].step_ticks = 6; program.lane[0].gate_ticks = 6;
    program.lane[0].base_cents = 6900; program.lane[0].span_cents = 2400;
    program.lane[0].velocity = 32767; program.lane[0].scale_mask = 0xab5;
    CHECK(sizeof(gen_source_t) <= 768 && sizeof(gen_bank_t) <= 16384);
    CHECK(!gen_init(&a.bank, &program, &a.memory, 48000, 64).code);
    for (i = 0; i < sizeof checkpoints / sizeof checkpoints[0]; i++) {
        CHECK(!gen_init(&a.bank, &program, &a.memory, 48000, 64).code);
        while (a.bank.tick < checkpoints[i]) CHECK(!gen_tick(&a.bank, &fa).code);
        CHECK(!gen_set(&a.bank, 0, GEN_PARAM_ENABLED, i & 1u).code);
        CHECK(!gen_set(&a.bank, 0, GEN_PARAM_SLEW, 17 + i).code);
        CHECK(!gen_reset(&a.bank, 0, i).code);
        CHECK(!gen_lane_enable(&a.bank, 0, (uint8_t)(i & 1u)).code);
        CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code);
        CHECK(!gen_restore(&b.bank, &program, &b.memory, sa, na).code);
        CHECK(!gen_save(&b.bank, sb, sizeof sb, &nb).code);
        CHECK(na == nb && !memcmp(sa, sb, na));
        for (j = 0; j < 4096; j++) {
            int32_t previous = a.bank.source[0].published[0];
            CHECK(!gen_tick(&a.bank, &fa).code && !gen_tick(&b.bank, &fb).code);
            CHECK(!memcmp(&fa, &fb, sizeof fa));
            CHECK(abs(a.bank.source[0].published[0] - previous) <= a.bank.source[0].config.slew);
        }
        CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code && !gen_save(&b.bank, sb, sizeof sb, &nb).code);
        CHECK(na == nb && !memcmp(sa, sb, na));
    }
    /* Every refused set/initialization/restore retains the canonical state. */
    CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code);
    CHECK(gen_set(&a.bank, 0, GEN_PARAM_SLEW, 513).code == GEN_RANGE);
    CHECK(gen_set(&a.bank, 0, GEN_PARAM_ENABLED, -1).code == GEN_RANGE);
    CHECK(gen_set(&a.bank, 3, GEN_PARAM_P0, 17).code == GEN_ARGUMENT);
    CHECK(gen_set(&a.bank, 6, GEN_PARAM_P0, 5).code == GEN_RANGE);
    invalid = program; invalid.source[1].id = 0;
    CHECK(gen_init(&a.bank, &invalid, &a.memory, 48000, 64).code != GEN_OK);
    invalid = program; invalid.lanes = 2; invalid.lane[1] = invalid.lane[0]; invalid.lane[1].id = 1;
    CHECK(gen_init(&a.bank, &invalid, &a.memory, 48000, 64).code == GEN_DEPENDENCY);
    CHECK(gen_init(&a.bank, &program, &a.memory, 0, 64).code == GEN_RANGE);
    CHECK(gen_init(&a.bank, &program, &a.memory, 48000, 0).code == GEN_RANGE);
    CHECK(gen_init(&a.bank, &program, &a.memory, 48000, 4097).code == GEN_RANGE);
    for (i = 0; i < 128; i++) {
        const size_t at = i * (na - 1) / 127;
        memcpy(bad, sa, na); bad[at] ^= (uint8_t)(1u << (i & 7u));
        CHECK(gen_restore(&a.bank, &program, &a.memory, bad, na).code != GEN_OK);
        CHECK(gen_restore(&a.bank, &program, &a.memory, sa, at).code != GEN_OK);
    }
    CHECK(gen_restore(&a.bank, &program, &a.memory, (const uint8_t *)&a.bank, sizeof a.bank).code == GEN_OVERLAP);
    CHECK(gen_save(&a.bank, (uint8_t *)&a.bank, sizeof a.bank, &nb).code == GEN_OVERLAP);
    bad[0] = 0xa5; nb = 123;
    CHECK(gen_save(&a.bank, bad, 1, &nb).code == GEN_CAPACITY && bad[0] == 0xa5 && nb == 123);
    CHECK(!gen_save(&a.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    /* The full tick is refused before either an output or source changes. */
    a.bank.tick = (UINT64_MAX - 1u) / a.bank.ctl + 1u;
    CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code);
    memset(&fa, 0xa5, sizeof fa); fb = fa;
    CHECK(gen_tick(&a.bank, &fa).code == GEN_OVERFLOW && !memcmp(&fa, &fb, sizeof fa));
    CHECK(!gen_save(&a.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    for (i = 1; i <= 4095; i++) for (j = 0; j < 7; j++) {
        const int32_t cents = -4800 + (int32_t)j * 3199;
        const int64_t q = gen_quantize(cents, (uint16_t)i);
        const int semitone = (int)(q / 100), pitchclass = semitone % 12;
        CHECK(q <= cents && cents - q < 1200 && q % 100 == 0
              && (i & (1u << (unsigned)(pitchclass < 0 ? pitchclass + 12 : pitchclass))));
    }
    CHECK(gen_quantize(INT32_MIN, 1) <= INT32_MIN && gen_quantize(1, 4096) == INT64_MIN);
    /* Largest retained words together with every remaining slot at the
     * largest recurrence-state shape; measure the actual canonical size. */
    memset(&program, 0, sizeof program); program.sources = GEN_SOURCES; program.texts = 2;
    for (i = 0; i < GEN_SOURCES; i++) CHECK(!gen_defaults(&program.source[i], i < 2 ? GEN_LSYSTEM : GEN_MULTI, (uint8_t)i, i).code);
    for (i = 0; i < 2; i++) {
        program.text[i].id = (uint8_t)i; program.text[i].depth = 14; program.text[i].rule_mask = 1;
        strcpy(program.text[i].axiom, "A"); strcpy(program.text[i].rule[0], "AA");
    }
    CHECK(!gen_init(&a.bank, &program, &a.memory, 48000, 64).code);
    CHECK(a.bank.word_length[0] == GEN_WORD_CAP && a.bank.word_length[1] == GEN_WORD_CAP);
    CHECK(!gen_save(&a.bank, sa, sizeof sa, &na).code); largest_snapshot = na;
    CHECK(!gen_restore(&b.bank, &program, &b.memory, sa, na).code);
    CHECK(!gen_save(&b.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    invalid = program; strcpy(invalid.text[0].axiom, "AB");
    CHECK(gen_init(&a.bank, &invalid, &a.memory, 48000, 64).code == GEN_CAPACITY);
    CHECK(!gen_save(&a.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    invalid = program; memset(invalid.text[0].axiom, 'A', sizeof invalid.text[0].axiom);
    CHECK(gen_init(&a.bank, &invalid, &a.memory, 48000, 64).code == GEN_RANGE);
    CHECK(!gen_save(&a.bank, sb, sizeof sb, &nb).code && na == nb && !memcmp(sa, sb, na));
    printf("GEN_SELFTEST checks=%u failures=%u snapshot=%lu\n", tests, failures, (unsigned long)largest_snapshot);
#undef CHECK
    return failures ? 1 : 0;
}

/* The host table is data shared with authoring tools, not a library ABI. */
typedef struct {
    unsigned id, mask;
    const char *name, *slot, *type, *unit;
    int64_t low, high, value;
    unsigned mutable, has_default;
} param_descriptor_t;
#ifndef GEN_PARAMS_FILE
#define GEN_PARAMS_FILE "params.def"
#endif
#define GEN_KIND(id,name)
#define GEN_PARAM(id,name,mask,slot,type,low,high,unit,mut,has,def) \
    {id,mask,#name,#slot,#type,#unit,low,high,def,mut,has},
static const param_descriptor_t param_descriptors[] = {
#include GEN_PARAMS_FILE
};
#undef GEN_PARAM
#undef GEN_KIND

/* Pin the stable text-kind mapping against the public C constants. */
#define KIND_white GEN_WHITE
#define KIND_pink GEN_PINK
#define KIND_brown GEN_BROWN
#define KIND_multi GEN_MULTI
#define KIND_logistic GEN_LOGISTIC
#define KIND_henon GEN_HENON
#define KIND_euclid GEN_EUCLID
#define KIND_thue GEN_THUE
#define KIND_cantor GEN_CANTOR
#define KIND_fibonacci GEN_FIBONACCI
#define KIND_lsystem GEN_LSYSTEM
#define GEN_KIND(id,name) {id,KIND_##name},
#define GEN_PARAM(id,name,mask,slot,type,low,high,unit,mut,has,def)
static const unsigned kind_descriptors[][2] = {
#include GEN_PARAMS_FILE
};
#undef GEN_PARAM
#undef GEN_KIND

static int scalar_slot(const param_descriptor_t *d)
{
    return d->slot[0] == 'p' && d->slot[1] >= '0' && d->slot[1] <= '3'
        && !d->slot[2] ? d->slot[1] - '0' : -1;
}

static void descriptor_program(gen_program_t *p, unsigned kind, int support)
{
    memset(p, 0, sizeof *p); p->sources = 1;
    (void)gen_defaults(&p->source[0], (uint8_t)kind, 0, 12345);
    if (kind == GEN_LSYSTEM) {
        p->texts = 1; strcpy(p->text[0].axiom, "A");
        if (support) {
            p->text[0].depth = 14; p->text[0].rule_mask = 1;
            strcpy(p->text[0].rule[0], "AA");
        }
    }
    if (!support) return;
    /* Independent coupled preconditions make each scalar's outer bound
     * attainable, except Fibonacci ROT: depth16 produces only2584 symbols.
     * That smaller dynamic limit is checked separately below. */
    if (kind == GEN_MULTI) { p->source[0].p[0] = 2; p->source[0].p[1] = 2; p->source[0].p[2] = 1; }
    if (kind == GEN_EUCLID) { p->source[0].p[0] = 0; p->source[0].p[1] = 65535; }
    if (kind == GEN_THUE || kind == GEN_CANTOR) p->source[0].p[0] = 65535;
    if (kind == GEN_FIBONACCI) p->source[0].p[0] = 16;
}

static int params_selftest(void)
{
    fixture_t f;
    gen_program_t p;
    gen_bank_t before;
    gen_frame_t frame;
    unsigned tests = 0, failures = 0, kind, i, j;
    unsigned seen_ids[116] = {0};
    const unsigned count = (unsigned)(sizeof param_descriptors / sizeof param_descriptors[0]);
#define PARAM_CHECK(expr) do { tests++; if (!(expr)) { failures++; \
    fprintf(stderr,"params check line=%d kind=%u row=%u\n",__LINE__,kind,i); } } while (0)
    kind = i = 0;
    PARAM_CHECK(count == 37);
    PARAM_CHECK(sizeof kind_descriptors / sizeof kind_descriptors[0] == 11);
    for (i = 0; i < sizeof kind_descriptors / sizeof kind_descriptors[0]; i++)
        PARAM_CHECK(kind_descriptors[i][0] == kind_descriptors[i][1]);
    for (i = 0; i < count; i++) {
        const param_descriptor_t *d = &param_descriptors[i];
        PARAM_CHECK(d->id < 116 && !seen_ids[d->id]);
        if (d->id < 116) seen_ids[d->id]++;
        PARAM_CHECK(d->low <= d->high && d->mutable <= 1 && d->has_default <= 1 && !(d->mask & ~2047u));
        PARAM_CHECK(!d->has_default || (d->low <= d->value && d->value <= d->high));
        /* These host IDs are version1 metadata, separate from gen_param_t. */
        if (d->id >= 16 && d->id <= 31) PARAM_CHECK(scalar_slot(d) >= 0 && !strcmp(d->type,"i64"));
    }
    for (i = 1; i <= 115; i++)
        PARAM_CHECK(seen_ids[i] == (unsigned)(i <= 5 || (i >= 16 && i <= 31) || i >= 100));
    for (kind = GEN_WHITE; kind <= GEN_LSYSTEM; kind++) {
        unsigned slots = 0;
        descriptor_program(&p,kind,0); memory_init(&f);
        PARAM_CHECK(!gen_init(&f.bank,&p,&f.memory,48000,64).code);
        for (i = 0; i < count; i++) {
            const param_descriptor_t *d = &param_descriptors[i];
            const int slot = scalar_slot(d);
            int64_t endpoint[4] = {d->low,d->high,d->low-1,d->high+1};
            if (!(d->mask & (1u << (kind-1)))) continue;
            if (slot >= 0) {
                PARAM_CHECK(!(slots & (1u << slot))); slots |= 1u << slot;
                PARAM_CHECK(d->has_default && p.source[0].p[slot] == d->value);
                for (j = 0; j < 4; j++) {
                    const int expected = j < 2 && !(kind == GEN_FIBONACCI && slot == 1 && endpoint[j] >= 2584);
                    gen_status_t st;
                    descriptor_program(&p,kind,1); p.source[0].p[slot] = endpoint[j]; memory_init(&f);
                    st = gen_init(&f.bank,&p,&f.memory,48000,64);
                    PARAM_CHECK((st.code == GEN_OK) == expected);
                    descriptor_program(&p,kind,1); memory_init(&f);
                    PARAM_CHECK(!gen_init(&f.bank,&p,&f.memory,48000,64).code);
                    before = f.bank;
                    st = gen_set(&f.bank,0,(gen_param_t)(GEN_PARAM_P0+slot),endpoint[j]);
                    PARAM_CHECK((st.code == GEN_OK) == (expected && d->mutable));
                    if (st.code) PARAM_CHECK(!memcmp(&before,&f.bank,sizeof before));
                    else {
                        PARAM_CHECK(!gen_tick(&f.bank,&frame).code);
                        PARAM_CHECK(f.bank.source[0].config.p[slot] == endpoint[j]);
                    }
                }
                if ((kind == GEN_FIBONACCI && slot == 1) || (kind == GEN_LSYSTEM && slot == 0)) {
                    uint32_t length;
                    descriptor_program(&p,kind,1); memory_init(&f);
                    PARAM_CHECK(!gen_init(&f.bank,&p,&f.memory,48000,64).code);
                    length = f.bank.word_length[0];
                    PARAM_CHECK(length == (kind == GEN_FIBONACCI ? 2584u : GEN_WORD_CAP));
                    PARAM_CHECK(d->high == GEN_WORD_CAP-1 && d->low == 0);
                    PARAM_CHECK(!gen_set(&f.bank,0,(gen_param_t)(GEN_PARAM_P0+slot),length-1).code);
                    PARAM_CHECK(gen_set(&f.bank,0,(gen_param_t)(GEN_PARAM_P0+slot),length).code != GEN_OK);
                    p.source[0].p[slot] = length-1;
                    PARAM_CHECK(!gen_init(&f.bank,&p,&f.memory,48000,64).code);
                    p.source[0].p[slot] = length;
                    PARAM_CHECK(gen_init(&f.bank,&p,&f.memory,48000,64).code != GEN_OK);
                }
                descriptor_program(&p,kind,0);
            } else if (!strcmp(d->slot,"enabled") || !strcmp(d->slot,"slew")) {
                const gen_param_t param = !strcmp(d->slot,"enabled") ? GEN_PARAM_ENABLED : GEN_PARAM_SLEW;
                descriptor_program(&p,kind,0); memory_init(&f);
                PARAM_CHECK(!gen_init(&f.bank,&p,&f.memory,48000,64).code);
                PARAM_CHECK(d->mutable && d->has_default && d->value ==
                            (param == GEN_PARAM_ENABLED ? f.bank.source[0].enabled : f.bank.source[0].config.slew));
                for (j = 0; j < 4; j++) {
                    gen_status_t st;
                    before = f.bank; st = gen_set(&f.bank,0,param,endpoint[j]);
                    PARAM_CHECK((st.code == GEN_OK) == (j < 2));
                    if (st.code) PARAM_CHECK(!memcmp(&before,&f.bank,sizeof before));
                    else {
                        PARAM_CHECK(!gen_tick(&f.bank,&frame).code);
                        PARAM_CHECK(endpoint[j] == (param == GEN_PARAM_ENABLED ? f.bank.source[0].enabled : f.bank.source[0].config.slew));
                    }
                }
            } else if (!strcmp(d->slot,"seed") || !strcmp(d->slot,"reset")) {
                PARAM_CHECK(d->low == 0 && d->high == UINT32_MAX && !d->has_default);
                PARAM_CHECK(d->mutable == (unsigned)!strcmp(d->slot,"reset"));
                for (j = 0; j < 2; j++) {
                    descriptor_program(&p,kind,0); p.source[0].seed = (uint32_t)endpoint[j]; memory_init(&f);
                    PARAM_CHECK(!gen_init(&f.bank,&p,&f.memory,48000,64).code);
                    PARAM_CHECK(f.bank.source[0].config.seed == endpoint[j]);
                    PARAM_CHECK(!gen_reset(&f.bank,0,(uint32_t)endpoint[1-j]).code);
                    PARAM_CHECK(!gen_tick(&f.bank,&frame).code);
                    PARAM_CHECK(f.bank.source[0].config.seed == endpoint[1-j]);
                }
            } else if (!strcmp(d->slot,"div")) {
                PARAM_CHECK(!d->mutable && d->has_default && d->value == 1 && d->low == 1 && d->high == UINT16_MAX);
                for (j = 0; j < 3; j++) {
                    descriptor_program(&p,kind,0); p.source[0].div = (uint16_t)endpoint[j]; memory_init(&f);
                    PARAM_CHECK((gen_init(&f.bank,&p,&f.memory,48000,64).code == GEN_OK) == (j < 2));
                }
            } else PARAM_CHECK(0);
            descriptor_program(&p,kind,0);
        }
        for (j = 0; j < 4; j++) if (!(slots & (1u << j))) {
            descriptor_program(&p,kind,0); p.source[0].p[j] = 1; memory_init(&f);
            PARAM_CHECK(gen_init(&f.bank,&p,&f.memory,48000,64).code != GEN_OK);
            p.source[0].p[j] = 0;
            PARAM_CHECK(!gen_init(&f.bank,&p,&f.memory,48000,64).code);
            PARAM_CHECK(gen_set(&f.bank,0,(gen_param_t)(GEN_PARAM_P0+j),0).code != GEN_OK);
        }
    }
    printf("GEN_PARAMS checks=%u failures=%u kinds=11 rows=%u\n",tests,failures,count);
#undef PARAM_CHECK
    return failures ? 1 : 0;
}

int main(int argc, char **argv)
{
    gen_program_t p;
    gen_memory_t memory;
    gen_bank_t bank;
    gen_frame_t frame;
    uint8_t scratch[2][GEN_WORD_CAP], words[2][GEN_WORD_CAP];
    int64_t kind, seed, count, warmup = 0;
    gen_status_t st;
    uint64_t i;
    int a, stream;
    if (argc == 2 && !strcmp(argv[1], "selftest")) return selftest();
    if (argc == 2 && !strcmp(argv[1], "params-selftest")) return params_selftest();
    if (argc == 2 && !strcmp(argv[1], "lifecycle")) return lifecycle_probe();
    if (argc == 2 && !strcmp(argv[1], "euclidean")) return euclidean_probe();
    if (argc >= 2 && !strcmp(argv[1], "word")) return word_probe(argc, argv);
    if (argc == 2 && (!strcmp(argv[1], "scenario") || !strcmp(argv[1], "checkpoint") || !strcmp(argv[1], "resume"))) return scenario(argv[1]);
    if (argc >= 4 && argc <= 6 && !strcmp(argv[1], "chaos") && number(argv[2], &kind) && number(argv[3], &seed)
        && kind >= 0 && kind <= 255 && seed >= 0 && seed <= UINT32_MAX) {
        int64_t value[2] = { 0, 0 };
        for (a = 4; a < argc; a++) if (!number(argv[a], &value[a - 4])) return 2;
        return chaos((unsigned)kind, (uint32_t)seed, (unsigned)(argc - 4), value);
    }
    if (argc == 2 && !strcmp(argv[1], "sizes")) {
        printf("source=%lu bank=%lu program=%lu frame=%lu mutable=%lu\n",
               (unsigned long)sizeof(gen_source_t), (unsigned long)sizeof(gen_bank_t),
               (unsigned long)sizeof(gen_program_t), (unsigned long)sizeof(gen_frame_t),
               (unsigned long)(sizeof(gen_bank_t) + 4 * GEN_WORD_CAP));
        return 0;
    }
    if (argc < 5 || argc > 10 || (strcmp(argv[1], "vector") && strcmp(argv[1], "stream"))
        || !number(argv[2], &kind) || !number(argv[3], &seed) || !number(argv[4], &count)
        || kind < GEN_WHITE || kind > GEN_LSYSTEM || seed < 0 || seed > UINT32_MAX || count < 0)
        return 2;
    stream = !strcmp(argv[1], "stream");
    if (stream && (argc < 6 || !number(argv[5], &warmup) || warmup < 0)) return 2;
    memset(&p, 0, sizeof p); memset(&memory, 0, sizeof memory);
    p.sources = 1;
    st = gen_defaults(&p.source[0], (uint8_t)kind, 0, (uint32_t)seed);
    if (st.code) return 2;
    for (a = stream ? 6 : 5; a < argc; a++) {
        unsigned index = (unsigned)(a - (stream ? 6 : 5));
        if (index >= 4 || !number(argv[a], &p.source[0].p[index])) return 2;
    }
    if (kind == GEN_LSYSTEM) {
        p.texts = 1; p.text[0].depth = 5; p.text[0].rule_mask = 3;
        strcpy(p.text[0].axiom, "AB"); strcpy(p.text[0].rule[0], "AB"); strcpy(p.text[0].rule[1], "A");
    }
    for (a = 0; a < 2; a++) {
        memory.scratch[a] = scratch[a]; memory.scratch_capacity[a] = sizeof scratch[a];
        memory.word[a] = words[a]; memory.word_capacity[a] = sizeof words[a];
    }
    st = gen_init(&bank, &p, &memory, 48000, 64);
    if (st.code) { fprintf(stderr, "init code=%u id=%u field=%u\n", st.code, st.id, st.field); return 2; }
    for (i = 0; i < (uint64_t)warmup; i++) if (gen_tick(&bank, &frame).code) return 1;
    if (!stream && !vector(&bank.source[0], &memory)) return 1;
    for (i = 0; i < (uint64_t)count; i++) {
        if (gen_tick(&bank, &frame).code) return 1;
        if (stream) {
            bytes_t out;
            out.n = 0;
            put(&out, (uint32_t)bank.source[0].raw[0], 4);
            put(&out, (uint32_t)bank.source[0].raw[1], 4);
            if (!flush(&out)) return 1;
        } else if (!vector(&bank.source[0], &memory)) return 1;
    }
    fprintf(stderr, "ticks=%llu count=%llu\n", (unsigned long long)bank.tick,
            (unsigned long long)((kind == GEN_BROWN || kind == GEN_HENON) ? bank.source[0].state.map.count : 0));
    return ferror(stdout) ? 1 : 0;
}
