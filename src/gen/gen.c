/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0 */
#include "gen.h"
#include "../../vendor/fp_math.h"
#if __STDC_HOSTED__
#include <string.h>
#else
/* Freestanding targets need only these standard memory primitives. Their
 * declarations do not require a target C library's hosted string header. */
extern void *memcpy(void *restrict, const void *restrict, size_t);
extern void *memset(void *, int, size_t);
#endif

/* Everything here runs at a caller's control boundary, never per audio
 * sample. Signed divisions deliberately use C99 truncation toward zero. */
#define P_RESET 1u
#define P_ENABLED 2u
#define P_SLEW 4u
#define P_SCALAR(i) (8u << (i))
#define ZERO_SEED UINT32_C(0x6d2b79f5)

static gen_status_t status(unsigned code, unsigned id, unsigned field)
{
    gen_status_t s;
    s.code = (uint8_t)code; s.id = (uint8_t)id; s.field = (uint8_t)field;
    return s;
}
#define OK status(GEN_OK, 0, GEN_FIELD_NONE)
#define BAD(c,id,f) status(c,id,f)

static uint32_t draw(gen_source_t *s)
{
    uint32_t x = s->random;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    s->random = x;
    return x;
}

static int32_t q15(uint32_t word) { return (int32_t)(word >> 16) - 32768; }
static int32_t limit_q15(int64_t x) { return (int32_t)(x < -32768 ? -32768 : x > 32767 ? 32767 : x); }
static int64_t product(int64_t a, int64_t b, int shift)
{
    return fp_w_to_i64(fp_w_asr(fp_w_muls(a, b), shift));
}
/* C99's truncating n/d for d>0, divided as unsigned magnitudes so that 32-bit
 * targets call no signed 64-bit helper ('s form, ). */
static int64_t quotient(int64_t n, uint64_t d)
{
    const uint64_t q = (n < 0 ? 0u - (uint64_t)n : (uint64_t)n) / d;
    return n >= 0 ? (int64_t)q : q ? -1 - (int64_t)(q - 1u) : 0;
}

int64_t gen_quantize(int32_t cents, uint16_t mask)
{
    int32_t note = cents / 100;
    unsigned i;
    if (mask > 4095u) return INT64_MIN;
    if (!mask) return cents;
    if (cents < 0 && cents % 100) note--;
    for (i = 0; i < 12; i++, note--) {
        const int32_t rem = note % 12;
        if (mask & (1u << (unsigned)(rem < 0 ? rem + 12 : rem))) {
            return (int64_t)note * 100;
        }
    }
    return cents;
}

gen_status_t gen_defaults(gen_source_config_t *c, uint8_t kind, uint8_t id, uint32_t seed)
{
    if (!c) return BAD(GEN_ARGUMENT, id, GEN_FIELD_NONE);
    if (id >= GEN_SOURCES || kind < GEN_WHITE || kind > GEN_LSYSTEM)
        return BAD(GEN_RANGE, id, GEN_FIELD_KIND);
    memset(c, 0, sizeof *c);
    c->id = id; c->kind = kind; c->seed = seed; c->div = 1; c->parent = -1;
    switch (kind) {
    case GEN_BROWN: c->p[0] = 16; break;
    case GEN_MULTI: c->p[0] = 256; c->p[1] = 2; c->p[2] = 6; c->p[3] = 24576; break;
    case GEN_LOGISTIC: c->p[0] = (399 * GEN_Q48 + 50) / 100; break;
    case GEN_HENON: c->p[0] = (7 * GEN_Q48 + 2) / 5; c->p[1] = (3 * GEN_Q48 + 5) / 10; break;
    case GEN_EUCLID: c->p[0] = 3; c->p[1] = 8; break;
    case GEN_THUE: case GEN_CANTOR: c->p[0] = 256; break;
    case GEN_FIBONACCI: c->p[0] = 12; break;
    default: break;
    }
    return OK;
}

static gen_status_t config_valid(const gen_source_config_t *c, uint32_t word_length)
{
    const int64_t *p = c->p;
    unsigned fields = 0, i;
    int64_t low[4] = {0,0,0,0}, high[4] = {0,0,0,0};
    if (c->id >= GEN_SOURCES || c->kind < GEN_WHITE || c->kind > GEN_LSYSTEM)
        return BAD(GEN_RANGE, c->id, GEN_FIELD_KIND);
    if (!c->div) return BAD(GEN_RANGE, c->id, GEN_FIELD_DIV);
    if (c->slew > 512) return BAD(GEN_RANGE, c->id, GEN_FIELD_SLEW);
    if (c->parent < -1 || c->parent >= (int)c->id || (c->parent != -1 && c->kind != GEN_EUCLID))
        return BAD(GEN_DEPENDENCY, c->id, GEN_FIELD_KIND);
    switch (c->kind) {
    case GEN_BROWN: fields = 1; low[0] = 1; high[0] = 4096; break;
    case GEN_MULTI:
        fields = 4; low[0] = 2; high[0] = 65536; low[1] = 2; high[1] = 16;
        low[2] = 1; high[2] = 8; high[3] = 32768; break;
    case GEN_LOGISTIC: fields = 1; high[0] = 4 * GEN_Q48; break;
    case GEN_HENON: fields = 2; high[0] = 2 * GEN_Q48; low[1] = -GEN_Q48; high[1] = GEN_Q48; break;
    case GEN_EUCLID:
        fields = 3; low[1] = 1; high[1] = 65535;
        /* Check the length first, before it becomes another field's bound. */
        if (p[1] < low[1] || p[1] > high[1]) return BAD(GEN_RANGE, c->id, GEN_FIELD_P1);
        high[0] = p[1]; high[2] = p[1] - 1; break;
    case GEN_THUE: case GEN_CANTOR:
        fields = 2; low[0] = 1; high[0] = 65535;
        if (p[0] < low[0] || p[0] > high[0]) return BAD(GEN_RANGE, c->id, GEN_FIELD_P0);
        high[1] = p[0] - 1; break;
    case GEN_FIBONACCI:
        fields = 2; high[0] = 16; high[1] = word_length ? (int64_t)word_length - 1 : INT64_MAX; break;
    case GEN_LSYSTEM: fields = 1; high[0] = word_length ? (int64_t)word_length - 1 : INT64_MAX; break;
    default: break;
    }
    for (i = 0; i < fields; i++)
        if (p[i] < low[i] || p[i] > high[i]) return BAD(GEN_RANGE, c->id, GEN_FIELD_P0 + i);
    if (c->kind == GEN_MULTI) {
        uint64_t period = (uint64_t)p[0];
        for (i = 1; i < (unsigned)p[2]; i++) {
            if (period > UINT32_MAX / (uint64_t)p[1]) return BAD(GEN_OVERFLOW, c->id, GEN_FIELD_P2);
            period *= (uint64_t)p[1];
        }
    }
    for (i = fields; i < 4; i++) if (p[i]) return BAD(GEN_RANGE, c->id, GEN_FIELD_P0 + i);
    return OK;
}

static int overlap(const void *a, size_t na, const void *b, size_t nb)
{
    const uintptr_t x = (uintptr_t)a, y = (uintptr_t)b;
    if (!na || !nb) return 0;
    if (!a || !b || na > UINTPTR_MAX - x || nb > UINTPTR_MAX - y) return 1;
    return x < y + nb && y < x + na;
}

static gen_status_t memory_valid(const gen_bank_t *bank, const gen_program_t *p, const gen_memory_t *m)
{
    const void *ptr[7];
    size_t size[7];
    unsigned i, j;
    if (!bank || !p || !m) return BAD(GEN_ARGUMENT, 0, GEN_FIELD_MEMORY);
    ptr[0] = bank; size[0] = sizeof *bank;
    ptr[1] = p; size[1] = sizeof *p;
    ptr[2] = m; size[2] = sizeof *m;
    for (i = 0; i < 2; i++) {
        ptr[3+i] = m->scratch[i]; size[3+i] = m->scratch_capacity[i];
        ptr[5+i] = m->word[i]; size[5+i] = m->word_capacity[i];
        if (size[3+i] > GEN_WORD_CAP || size[5+i] > GEN_WORD_CAP)
            return BAD(GEN_CAPACITY, i, GEN_FIELD_MEMORY);
    }
    for (i = 0; i < 7; i++) {
        if (size[i] && (!ptr[i] || size[i] > UINTPTR_MAX - (uintptr_t)ptr[i]))
            return BAD(GEN_ARGUMENT, i, GEN_FIELD_MEMORY);
        for (j = 0; j < i; j++) if (overlap(ptr[i], size[i], ptr[j], size[j]))
            return BAD(GEN_OVERLAP, i, GEN_FIELD_MEMORY);
    }
    return OK;
}

static size_t text_length(const char *word)
{
    size_t i;
    for (i = 0; i <= 32; i++) if (!word[i]) return i;
    return 33;
}

static gen_status_t expand(const gen_program_t *p, const gen_source_config_t *c,
                           const gen_memory_t *m, uint32_t *length, const uint8_t **final)
{
    gen_text_config_t fib;
    const gen_text_config_t *text = 0;
    uint8_t *from = m->scratch[0], *to = m->scratch[1], *swap;
    size_t n, sizes[8];
    unsigned i, depth;
    if (c->kind == GEN_FIBONACCI) {
        memset(&fib, 0, sizeof fib);
        fib.depth = (uint8_t)c->p[0]; fib.rule_mask = 3;
        fib.axiom[0] = 'A'; fib.rule[0][0] = 'A'; fib.rule[0][1] = 'B'; fib.rule[1][0] = 'A';
        text = &fib;
    } else for (i = 0; i < p->texts; i++) if (p->text[i].id == c->id) text = &p->text[i];
    if (!text) return BAD(GEN_DEPENDENCY, c->id, GEN_FIELD_WORD);
    n = text_length(text->axiom);
    if (!n || n > 32 || text->depth > 16) return BAD(GEN_RANGE, c->id, GEN_FIELD_WORD);
    for (i = 0; i < 8; i++) {
        size_t j;
        sizes[i] = text_length(text->rule[i]);
        if (sizes[i] > 32) return BAD(GEN_RANGE, c->id, GEN_FIELD_WORD);
        if (!(text->rule_mask & (1u << i)) && sizes[i]) return BAD(GEN_ARGUMENT, c->id, GEN_FIELD_WORD);
        for (j = 0; j < sizes[i]; j++) if (text->rule[i][j] < 'A' || text->rule[i][j] > 'H')
            return BAD(GEN_RANGE, c->id, GEN_FIELD_WORD);
    }
    if (!from || !to || m->scratch_capacity[0] < GEN_WORD_CAP || m->scratch_capacity[1] < GEN_WORD_CAP)
        return BAD(GEN_CAPACITY, c->id, GEN_FIELD_MEMORY);
    for (i = 0; i < n; i++) {
        if (text->axiom[i] < 'A' || text->axiom[i] > 'H') return BAD(GEN_RANGE, c->id, GEN_FIELD_WORD);
        from[i] = (uint8_t)text->axiom[i];
    }
    for (depth = 0; depth < text->depth; depth++) {
        size_t out = 0, j;
        for (j = 0; j < n; j++) {
            const unsigned symbol = from[j] - 'A';
            const size_t add = text->rule_mask & (1u << symbol) ? sizes[symbol] : 1;
            if (add > GEN_WORD_CAP - out) return BAD(GEN_CAPACITY, c->id, GEN_FIELD_WORD);
            out += add;
        }
        out = 0;
        for (j = 0; j < n; j++) {
            const unsigned symbol = from[j] - 'A';
            if (text->rule_mask & (1u << symbol)) {
                size_t k;
                for (k = 0; k < sizes[symbol]; k++) to[out++] = (uint8_t)text->rule[symbol][k];
            } else to[out++] = from[j];
        }
        n = out; swap = from; from = to; to = swap;
    }
    if (!n) return BAD(GEN_RANGE, c->id, GEN_FIELD_WORD);
    *length = (uint32_t)n; *final = from;
    return OK;
}

static uint64_t hash_word(uint64_t hash, uint64_t value, unsigned bytes)
{
    unsigned i;
    for (i = 0; i < bytes; i++) { hash = (hash ^ (uint8_t)value) * UINT64_C(0x100000001b3); value >>= 8; }
    return hash;
}

static uint64_t program_hash(const gen_program_t *p)
{
    uint64_t h = UINT64_C(0xcbf29ce484222325);
    unsigned id, i, j, k;
    h = hash_word(h, p->sources, 1); h = hash_word(h, p->lanes, 1); h = hash_word(h, p->texts, 1);
    for (id = 0; id < GEN_SOURCES; id++) for (i = 0; i < p->sources; i++) if (p->source[i].id == id) {
        const gen_source_config_t *c = &p->source[i];
        h = hash_word(h, c->id, 1); h = hash_word(h, c->kind, 1); h = hash_word(h, (uint8_t)c->parent, 1);
        h = hash_word(h, c->seed, 4); h = hash_word(h, c->div, 2); h = hash_word(h, c->slew, 2);
        for (j = 0; j < 4; j++) h = hash_word(h, (uint64_t)c->p[j], 8);
    }
    for (id = 0; id < GEN_LANES; id++) for (i = 0; i < p->lanes; i++) if (p->lane[i].id == id) {
        const gen_lane_config_t *c = &p->lane[i];
        h = hash_word(h, c->id, 1); h = hash_word(h, c->voice, 2);
        h = hash_word(h, c->step_ticks, 2); h = hash_word(h, c->gate_ticks, 2);
        h = hash_word(h, (uint32_t)c->base_cents, 4); h = hash_word(h, c->span_cents, 2);
        h = hash_word(h, c->velocity, 2); h = hash_word(h, c->scale_mask, 2);
        h = hash_word(h, (uint8_t)c->gate, 1); h = hash_word(h, (uint8_t)c->pitch, 1); h = hash_word(h, (uint8_t)c->accent, 1);
    }
    for (id = 0; id < GEN_SOURCES; id++) for (i = 0; i < p->texts; i++) if (p->text[i].id == id) {
        const gen_text_config_t *t = &p->text[i];
        h = hash_word(h, t->id, 1); h = hash_word(h, t->depth, 1); h = hash_word(h, t->rule_mask, 1);
        for (j = 0; j <= text_length(t->axiom); j++) h = hash_word(h, (uint8_t)t->axiom[j], 1);
        for (k = 0; k < 8; k++) for (j = 0; j <= text_length(t->rule[k]); j++) h = hash_word(h, (uint8_t)t->rule[k][j], 1);
    }
    return h;
}

static unsigned text_slot(const gen_program_t *p, unsigned id)
{
    unsigned i, slot = 0;
    for (i = 0; i < p->sources; i++) if (p->source[i].id < id
        && (p->source[i].kind == GEN_FIBONACCI || p->source[i].kind == GEN_LSYSTEM)) slot++;
    return slot;
}

static void source_reset(gen_source_t *s, uint32_t seed)
{
    const int64_t *p = s->config.p;
    unsigned i;
    s->random = seed ? seed : ZERO_SEED;
    s->raw[0] = s->raw[1] = 0;
    switch (s->config.kind) {
    case GEN_PINK:
        s->state.pink.sum = 0; s->state.pink.counter = 0;
        for (i = 0; i < 16; i++) { s->state.pink.row[i] = (int16_t)q15(draw(s)); s->state.pink.sum += s->state.pink.row[i]; }
        break;
    case GEN_BROWN:
        s->state.map.x = ((int64_t)draw(s) - INT64_C(2147483648)) * INT64_C(2147483648);
        s->state.map.y = 0; s->state.map.count = 0; break;
    case GEN_MULTI:
        for (i = 0; i < 256; i++) s->state.multi.motif[i] = (int16_t)q15(draw(s));
        for (i = 0; i < (unsigned)p[2]; i++) {
            s->state.multi.position[i] = 0;
            s->state.multi.period[i] = i ? s->state.multi.period[i-1] * (uint32_t)p[1] : (uint32_t)p[0];
            s->state.multi.weight[i] = i ? (uint32_t)((uint64_t)s->state.multi.weight[i-1] * (uint32_t)p[3] / 32768u) : 32768u;
        }
        break;
    case GEN_LOGISTIC: {
        const uint64_t high = draw(s), low = draw(s);
        s->state.map.x = (int64_t)(1u + ((high << 32) | low) % ((uint64_t)GEN_ONE - 1u));
        s->state.map.y = 0; s->state.map.count = 0; break;
    }
    case GEN_HENON:
        s->state.map.x = quotient((int64_t)q15(draw(s)) * GEN_Q48, 327680u);
        s->state.map.y = 0; s->state.map.count = 0; break;
    case GEN_EUCLID: case GEN_THUE: case GEN_CANTOR: case GEN_FIBONACCI: case GEN_LSYSTEM: {
        uint32_t length;
        const unsigned rfield = s->config.kind == GEN_EUCLID ? 2 : s->config.kind == GEN_LSYSTEM ? 0 : 1;
        if (s->config.kind == GEN_EUCLID) s->state.seq.length = (uint32_t)p[1];
        else if (s->config.kind == GEN_THUE || s->config.kind == GEN_CANTOR) s->state.seq.length = (uint32_t)p[0];
        length = s->state.seq.length;
        s->state.seq.step = 0;
        s->state.seq.rotation = (s->random % length + (uint32_t)p[rfield]) % length;
        break;
    }
    default: break;
    }
}

static gen_status_t program_valid(const gen_bank_t *bank, const gen_program_t *p,
                                  const gen_memory_t *m, uint32_t fs, uint32_t ctl)
{
    uint16_t mask = 0;
    uint8_t lanes = 0, textmask = 0;
    unsigned i, j, words = 0;
    gen_status_t st = memory_valid(bank, p, m);
    if (st.code) return st;
    if (!fs || !ctl || ctl > 4096) return BAD(GEN_RANGE, 0, GEN_FIELD_CLOCK);
    if (p->sources > GEN_SOURCES || p->lanes > GEN_LANES || p->texts > GEN_WORDS)
        return BAD(GEN_CAPACITY, 0, GEN_FIELD_MEMORY);
    for (i = 0; i < p->sources; i++) {
        const gen_source_config_t *c = &p->source[i];
        st = config_valid(c, 0); if (st.code) return st;
        if (mask & (1u << c->id)) return BAD(GEN_DEPENDENCY, c->id, GEN_FIELD_KIND);
        mask |= (uint16_t)(1u << c->id);
        if (c->kind == GEN_FIBONACCI || c->kind == GEN_LSYSTEM) {
            uint32_t length;
            const uint8_t *word;
            const unsigned slot = text_slot(p, c->id);
            if (words >= GEN_WORDS || slot >= GEN_WORDS) return BAD(GEN_CAPACITY, c->id, GEN_FIELD_WORD);
            st = expand(p, c, m, &length, &word); if (st.code) return st;
            st = config_valid(c, length); if (st.code) return st;
            if (m->word_capacity[slot] < length) return BAD(GEN_CAPACITY, c->id, GEN_FIELD_WORD);
            words++;
        }
    }
    for (i = 0; i < p->sources; i++) if (p->source[i].parent >= 0 && !(mask & (1u << p->source[i].parent)))
        return BAD(GEN_DEPENDENCY, p->source[i].id, GEN_FIELD_KIND);
    for (i = 0; i < p->texts; i++) {
        unsigned found = 0;
        for (j = 0; j < p->sources; j++) if (p->source[j].id == p->text[i].id && p->source[j].kind == GEN_LSYSTEM) found++;
        if (!found) return BAD(GEN_DEPENDENCY, p->text[i].id, GEN_FIELD_WORD);
        for (j = 0; j < i; j++) if (p->text[j].id == p->text[i].id) textmask = 1;
        if (textmask) return BAD(GEN_DEPENDENCY, p->text[i].id, GEN_FIELD_WORD);
    }
    for (i = 0; i < p->lanes; i++) {
        const gen_lane_config_t *c = &p->lane[i];
        const int8_t refs[3] = { c->gate, c->pitch, c->accent };
        const int64_t low = (int64_t)c->base_cents - c->span_cents - (c->scale_mask ? 1199 : 0);
        const int64_t high = (int64_t)c->base_cents + c->span_cents;
        if (c->id >= GEN_LANES || (lanes & (1u << c->id)) || !c->step_ticks || !c->gate_ticks
            || c->gate_ticks > c->step_ticks || !c->velocity || c->velocity > 32767
            || c->span_cents > 9600 || c->scale_mask > 4095 || low < -4800 || high > 14400)
            return BAD(GEN_RANGE, c->id, GEN_FIELD_LANE);
        lanes |= (uint8_t)(1u << c->id);
        for (j = 0; j < i; j++) if (p->lane[j].voice == c->voice) return BAD(GEN_DEPENDENCY, c->id, GEN_FIELD_LANE);
        for (j = 0; j < 3; j++) if (refs[j] < -1 || refs[j] >= GEN_SOURCES || (refs[j] >= 0 && !(mask & (1u << refs[j]))))
            return BAD(GEN_DEPENDENCY, c->id, GEN_FIELD_LANE);
    }
    return OK;
}

gen_status_t gen_init(gen_bank_t *b, const gen_program_t *p, gen_memory_t *m, uint32_t fs, uint32_t ctl)
{
    unsigned i;
    gen_status_t st = program_valid(b, p, m, fs, ctl);
    if (st.code) return st;
    memset(b, 0, sizeof *b);
    b->program = p; b->memory = *m; b->fs = fs; b->ctl = ctl; b->program_hash = program_hash(p);
    for (i = 0; i < p->sources; i++) {
        const gen_source_config_t *c = &p->source[i];
        gen_source_t *s = &b->source[c->id];
        s->config = *c; s->enabled = 1;
        b->source_mask |= (uint16_t)(1u << c->id);
        if (c->kind == GEN_FIBONACCI || c->kind == GEN_LSYSTEM) {
            const uint8_t *word;
            uint32_t n;
            const unsigned slot = text_slot(p, c->id);
            /* Same immutable input and already checked capacities: infallible. */
            (void)expand(p, c, m, &n, &word);
            memcpy(m->word[slot], word, n);
            s->state.seq.word_slot = (uint8_t)slot;
            s->state.seq.length = n; b->word_length[slot] = n; b->words++;
        } else if (c->kind == GEN_EUCLID) s->state.seq.length = (uint32_t)c->p[1];
        else if (c->kind == GEN_THUE || c->kind == GEN_CANTOR) s->state.seq.length = (uint32_t)c->p[0];
        source_reset(s, c->seed);
    }
    for (i = 0; i < p->lanes; i++) {
        const gen_lane_config_t *c = &p->lane[i];
        b->lane[c->id].config = *c; b->lane[c->id].enabled = 1;
        b->lane_mask |= (uint8_t)(1u << c->id);
    }
    return OK;
}

static void raw_update(gen_source_t *s, const gen_memory_t *m, int32_t parent)
{
    const int64_t *p = s->config.p;
    int32_t x = 0, y = 0;
    unsigned i;
    switch (s->config.kind) {
    case GEN_WHITE: x = q15(draw(s)); break;
    case GEN_PINK: {
        const uint16_t counter = (uint16_t)(s->state.pink.counter + 1u);
        s->state.pink.counter = counter;
        if (counter) {
            uint16_t bits = counter;
            int16_t next;
            for (i = 0; !(bits & 1u); i++) bits >>= 1;
            next = (int16_t)q15(draw(s));
            s->state.pink.sum += (int32_t)next - s->state.pink.row[i];
            s->state.pink.row[i] = next;
        }
        x = (s->state.pink.sum + q15(draw(s))) / 17;
        break;
    }
    case GEN_BROWN: {
        const int32_t a = (int32_t)(draw(s) >> 16), b = (int32_t)(draw(s) >> 16);
        const int64_t delta = (int64_t)(a - b) * p[0] * INT64_C(2147483648);
        const int64_t z = s->state.map.x + delta;
        if (z > GEN_ONE) { s->state.map.x = GEN_ONE - (z - GEN_ONE); s->state.map.count++; }
        else if (z < -GEN_ONE) { s->state.map.x = -GEN_ONE - (z + GEN_ONE); s->state.map.count++; }
        else s->state.map.x = z;
        x = limit_q15(s->state.map.x / INT64_C(140737488355328));
        break;
    }
    case GEN_MULTI: {
        int64_t sum = 0;
        uint32_t total = 0;
        for (i = 0; i < (unsigned)p[2]; i++) {
            const uint32_t period = s->state.multi.period[i];
            const uint64_t scaled = (uint64_t)s->state.multi.position[i] * 256u;
            const uint32_t index = (uint32_t)(scaled / period), remainder = (uint32_t)(scaled % period);
            const int32_t a = s->state.multi.motif[index], b = s->state.multi.motif[(index + 1u) & 255u];
            const int32_t value = a + (int32_t)quotient((int64_t)(b - a) * remainder, period);
            sum += (int64_t)value * s->state.multi.weight[i]; total += s->state.multi.weight[i];
            if (++s->state.multi.position[i] == period) s->state.multi.position[i] = 0;
        }
        x = (int32_t)quotient(sum, total);
        break;
    }
    case GEN_LOGISTIC: {
        const int64_t u = product(s->state.map.x, GEN_ONE - s->state.map.x, 62);
        s->state.map.x = product(p[0], u, 48);
        x = limit_q15(s->state.map.x / INT64_C(70368744177664) - 32768);
        break;
    }
    case GEN_HENON: {
        const int64_t square = product(s->state.map.x, s->state.map.x, 48);
        int64_t nx = GEN_Q48 - product(p[0], square, 48) + s->state.map.y;
        int64_t ny = product(p[1], s->state.map.x, 48);
        if (nx < -4 * GEN_Q48) { nx = -4 * GEN_Q48; s->state.map.count++; }
        else if (nx > 4 * GEN_Q48) { nx = 4 * GEN_Q48; s->state.map.count++; }
        if (ny < -4 * GEN_Q48) { ny = -4 * GEN_Q48; s->state.map.count++; }
        else if (ny > 4 * GEN_Q48) { ny = 4 * GEN_Q48; s->state.map.count++; }
        s->state.map.x = nx; s->state.map.y = ny;
        x = limit_q15(nx / INT64_C(34359738368)); y = limit_q15(ny / INT64_C(34359738368));
        break;
    }
    default: {
        const uint32_t length = s->state.seq.length;
        uint32_t j = (s->state.seq.step + s->state.seq.rotation) % length;
        switch (s->config.kind) {
        case GEN_EUCLID:
            x = parent > 0 && ((j + 1u) * (uint32_t)p[0] / length - j * (uint32_t)p[0] / length) ? 32767 : 0;
            break;
        case GEN_THUE:
            while (j) { x ^= (int32_t)(j & 1u); j >>= 1; }
            x *= 32767; break;
        case GEN_CANTOR:
            x = 32767;
            while (j) { if (j % 3u == 1u) { x = 0; break; } j /= 3u; }
            break;
        case GEN_FIBONACCI: x = m->word[s->state.seq.word_slot][j] == 'B' ? 32767 : 0; break;
        case GEN_LSYSTEM: x = (int32_t)(m->word[s->state.seq.word_slot][j] - 'A') * 65535 / 7 - 32768; break;
        default: break;
        }
        if (++s->state.seq.step == length) s->state.seq.step = 0;
        break;
    }
    }
    s->raw[0] = x; s->raw[1] = y;
}

static void pending_apply(gen_source_t *s)
{
    const unsigned flags = s->pending.flags;
    unsigned i;
    for (i = 0; i < 4; i++) if (flags & P_SCALAR(i)) s->config.p[i] = s->pending.p[i];
    if (flags & P_SLEW) s->config.slew = s->pending.slew;
    if (flags & P_ENABLED) s->enabled = s->pending.enabled;
    if (flags & P_RESET) { s->config.seed = s->pending.seed; source_reset(s, s->pending.seed); }
    else if (flags & (P_SCALAR(0) | P_SCALAR(1) | P_SCALAR(2) | P_SCALAR(3))) {
        const int64_t *p = s->config.p;
        if (s->config.kind == GEN_MULTI) {
            for (i = 1; i < (unsigned)p[2]; i++)
                s->state.multi.weight[i] = (uint32_t)((uint64_t)s->state.multi.weight[i-1] * (uint32_t)p[3] / 32768u);
        } else if (s->config.kind >= GEN_EUCLID) {
            const unsigned rfield = s->config.kind == GEN_EUCLID ? 2 : s->config.kind == GEN_LSYSTEM ? 0 : 1;
            const uint32_t seed = s->config.seed ? s->config.seed : ZERO_SEED;
            if (s->config.kind == GEN_EUCLID) s->state.seq.length = (uint32_t)p[1];
            else if (s->config.kind == GEN_THUE || s->config.kind == GEN_CANTOR) s->state.seq.length = (uint32_t)p[0];
            s->state.seq.step %= s->state.seq.length;
            s->state.seq.rotation = (seed % s->state.seq.length + (uint32_t)p[rfield]) % s->state.seq.length;
        }
    }
    memset(&s->pending, 0, sizeof s->pending);
}

static int mutable_scalar(unsigned kind, unsigned field)
{
    switch (kind) {
    case GEN_BROWN: case GEN_LOGISTIC: case GEN_LSYSTEM: return field == 0;
    case GEN_HENON: case GEN_THUE: case GEN_CANTOR: return field < 2;
    case GEN_MULTI: return field == 3;
    case GEN_EUCLID: return field < 3;
    case GEN_FIBONACCI: return field == 1;
    default: return 0;
    }
}

gen_status_t gen_set(gen_bank_t *b, uint8_t id, gen_param_t param, int64_t value)
{
    gen_source_t *s;
    gen_source_config_t c;
    gen_status_t st;
    unsigned i;
    if (!b || id >= GEN_SOURCES || !(b->source_mask & (1u << id))) return BAD(GEN_ARGUMENT, id, GEN_FIELD_KIND);
    s = &b->source[id];
    if (param == GEN_PARAM_ENABLED) {
        if (value < 0 || value > 1) return BAD(GEN_RANGE, id, GEN_FIELD_NONE);
        s->pending.enabled = (uint8_t)value; s->pending.flags |= P_ENABLED; return OK;
    }
    if (param == GEN_PARAM_SLEW) {
        if (value < 0 || value > 512) return BAD(GEN_RANGE, id, GEN_FIELD_SLEW);
        s->pending.slew = (uint16_t)value; s->pending.flags |= P_SLEW; return OK;
    }
    if (param < GEN_PARAM_P0 || param > GEN_PARAM_P3 || !mutable_scalar(s->config.kind, param - GEN_PARAM_P0))
        return BAD(GEN_ARGUMENT, id, GEN_FIELD_P0);
    c = s->config;
    for (i = 0; i < 4; i++) if (s->pending.flags & P_SCALAR(i)) c.p[i] = s->pending.p[i];
    i = param - GEN_PARAM_P0; c.p[i] = value;
    st = config_valid(&c, c.kind == GEN_FIBONACCI || c.kind == GEN_LSYSTEM ? s->state.seq.length : 0);
    if (st.code) return st;
    s->pending.p[i] = value; s->pending.flags |= (uint16_t)P_SCALAR(i);
    return OK;
}

gen_status_t gen_reset(gen_bank_t *b, uint8_t id, uint32_t seed)
{
    if (!b || id >= GEN_SOURCES || !(b->source_mask & (1u << id))) return BAD(GEN_ARGUMENT, id, GEN_FIELD_KIND);
    b->source[id].pending.seed = seed; b->source[id].pending.flags |= P_RESET;
    return OK;
}

gen_status_t gen_lane_enable(gen_bank_t *b, uint8_t id, uint8_t enabled)
{
    if (!b || id >= GEN_LANES || !(b->lane_mask & (1u << id))) return BAD(GEN_ARGUMENT, id, GEN_FIELD_LANE);
    if (enabled > 1) return BAD(GEN_RANGE, id, GEN_FIELD_LANE);
    b->lane[id].has_pending = 1; b->lane[id].pending_enabled = enabled;
    return OK;
}

static void emit(gen_frame_t *frame, const gen_lane_t *l, unsigned on, int32_t cents, uint16_t velocity)
{
    gen_event_t *e = &frame->event[frame->events++];
    e->voice = l->config.voice; e->lane = l->config.id; e->on = (uint8_t)on;
    e->cents = cents; e->velocity = velocity;
}

gen_status_t gen_tick(gen_bank_t *b, gen_frame_t *frame)
{
    unsigned i, axis;
    if (!b || !frame || !b->ctl) return BAD(GEN_ARGUMENT, 0, GEN_FIELD_CLOCK);
    if (overlap(b, sizeof *b, frame, sizeof *frame) || overlap(b->program, sizeof *b->program, frame, sizeof *frame))
        return BAD(GEN_OVERLAP, 0, GEN_FIELD_MEMORY);
    for (i = 0; i < GEN_WORDS; i++) if (overlap(frame, sizeof *frame, b->memory.word[i], b->memory.word_capacity[i])
        || overlap(frame, sizeof *frame, b->memory.scratch[i], b->memory.scratch_capacity[i]))
        return BAD(GEN_OVERLAP, i, GEN_FIELD_MEMORY);
    if (b->tick > (UINT64_MAX - 1u) / b->ctl) return BAD(GEN_OVERFLOW, 0, GEN_FIELD_CLOCK);
    /* No source has moved when a lane addition is refused. */
    for (i = 0; i < GEN_LANES; i++) if (b->lane_mask & (1u << i)) {
        const gen_lane_t *l = &b->lane[i];
        const unsigned enabled = l->has_pending ? l->pending_enabled : l->enabled;
        const uint64_t next = l->has_pending && enabled && !l->enabled ? b->tick : l->next_step;
        if (enabled && next <= b->tick && b->tick > UINT64_MAX - l->config.step_ticks)
            return BAD(GEN_OVERFLOW, i, GEN_FIELD_LANE);
    }
    /* Map counters normally cannot approach overflow at an audio grid.
     * At ctl1, test the rare final increments on one bounded local state
     * before touching the bank, including queued resets/configuration. */
    for (i = 0; i < GEN_SOURCES; i++) if (b->source_mask & (1u << i)) {
        const gen_source_t *s = &b->source[i];
        if ((s->config.kind == GEN_HENON || s->config.kind == GEN_BROWN)
            && s->state.map.count > UINT64_MAX - 2u && b->tick % s->config.div == 0u) {
            gen_source_t copy = *s;
            pending_apply(&copy);
            if (copy.enabled) {
                const uint64_t before = copy.state.map.count;
                raw_update(&copy, &b->memory, 32767);
                if (copy.state.map.count < before) return BAD(GEN_OVERFLOW, i, GEN_FIELD_KIND);
            }
        }
    }
    memset(frame, 0, sizeof *frame);
    frame->frame = b->tick * b->ctl;
    for (i = 0; i < GEN_SOURCES; i++) if (b->source_mask & (1u << i)) {
        gen_source_t *s = &b->source[i];
        if (b->tick % s->config.div == 0u) {
            pending_apply(s);
            if (s->enabled) raw_update(s, &b->memory, s->config.parent < 0 ? 32767 : b->source[(unsigned)s->config.parent].published[0]);
        }
        if (!s->enabled) s->raw[0] = s->raw[1] = 0;
        for (axis = 0; axis < 2; axis++) {
            int32_t difference = s->raw[axis] - s->published[axis];
            if (s->config.slew) {
                if (difference > s->config.slew) difference = s->config.slew;
                if (difference < -(int32_t)s->config.slew) difference = -(int32_t)s->config.slew;
            }
            s->published[axis] += difference;
            frame->value[i][axis] = s->published[axis];
        }
    }
    /* First all offs, then all ons; each lane emits at most one of each. */
    for (i = 0; i < GEN_LANES; i++) if (b->lane_mask & (1u << i)) {
        gen_lane_t *l = &b->lane[i];
        if (l->has_pending) {
            if (l->pending_enabled && !l->enabled) l->next_step = b->tick;
            l->enabled = l->pending_enabled; l->has_pending = 0; l->pending_enabled = 0;
        }
        if (l->active && (!l->enabled || l->off_tick <= b->tick
            || (l->next_step <= b->tick && (l->config.gate < 0 || frame->value[(unsigned)l->config.gate][0] > 0)))) {
            emit(frame, l, 0, 0, 0); l->active = 0; l->off_tick = 0;
        }
    }
    for (i = 0; i < GEN_LANES; i++) if (b->lane_mask & (1u << i)) {
        gen_lane_t *l = &b->lane[i];
        const gen_lane_config_t *c = &l->config;
        if (!l->enabled || l->next_step > b->tick) continue;
        l->next_step = b->tick + c->step_ticks;
        if (c->gate < 0 || frame->value[(unsigned)c->gate][0] > 0) {
            const int32_t pitch = c->pitch < 0 ? 0 : frame->value[(unsigned)c->pitch][0];
            const int32_t accent = c->accent < 0 ? 32767 : frame->value[(unsigned)c->accent][0];
            const int32_t cents = c->base_cents + (int32_t)((int64_t)c->span_cents * pitch / 32768);
            const uint32_t velocity = (uint32_t)c->velocity * (uint32_t)(accent + 32768) / 65535u;
            emit(frame, l, 1, (int32_t)gen_quantize(cents, c->scale_mask), (uint16_t)(velocity ? velocity : 1));
            l->active = 1; l->off_tick = b->tick + c->gate_ticks;
        }
    }
    b->tick++;
    return OK;
}

/* One explicit canonical encoding is shared by size, write and read passes.
 * Reads use one source-sized local record; no second bank/word allocation. */
typedef struct { const uint8_t *src; uint8_t *dst; size_t size, at; int bad; } io_t;
static uint64_t io(io_t *c, uint64_t value, unsigned bytes)
{
    uint64_t result = c->src ? 0 : value;
    unsigned i;
    if (c->at > c->size || bytes > c->size - c->at) { c->bad = 1; return 0; }
    for (i = 0; i < bytes; i++) {
        if (c->src) result |= (uint64_t)c->src[c->at + i] << (8u * i);
        if (c->dst) c->dst[c->at + i] = (uint8_t)(value >> (8u * i));
    }
    c->at += bytes;
    return result;
}
static int64_t signed64(uint64_t x) { return x <= INT64_MAX ? (int64_t)x : -1 - (int64_t)(UINT64_MAX - x); }
#define U8(f) (f) = (uint8_t)io(c, (uint64_t)(f), 1)
#define U16(f) (f) = (uint16_t)io(c, (uint64_t)(f), 2)
#define U32(f) (f) = (uint32_t)io(c, (uint64_t)(f), 4)
#define U64(f) (f) = io(c, (uint64_t)(f), 8)
#define I32(f) do { uint32_t val_ = (uint32_t)io(c, (uint32_t)(f), 4); (f) = val_ <= INT32_MAX ? (int32_t)val_ : -1 - (int32_t)(UINT32_MAX - val_); } while (0)
#define I64(f) (f) = signed64(io(c, (uint64_t)(f), 8))

static void source_io(io_t *c, gen_source_t *s)
{
    unsigned j;
    uint8_t parent = (uint8_t)s->config.parent;
    U8(s->config.id); U8(s->config.kind); U8(parent);
    s->config.parent = (int8_t)(parent <= 127u ? (int)parent : (int)parent - 256);
    U32(s->config.seed); U16(s->config.div); U16(s->config.slew);
    for (j = 0; j < 4; j++) I64(s->config.p[j]);
    U16(s->pending.flags); U32(s->pending.seed); U16(s->pending.slew); U8(s->pending.enabled);
    for (j = 0; j < 4; j++) I64(s->pending.p[j]);
    U32(s->random); U8(s->enabled);
    for (j = 0; j < 2; j++) { I32(s->raw[j]); I32(s->published[j]); }
    switch (s->config.kind) {
    case GEN_PINK:
        U16(s->state.pink.counter); I32(s->state.pink.sum);
        for (j = 0; j < 16; j++) {
            uint16_t value = (uint16_t)s->state.pink.row[j];
            U16(value); s->state.pink.row[j] = (int16_t)(value <= 32767u ? (int)value : (int)value - 65536);
        }
        break;
    case GEN_BROWN: case GEN_LOGISTIC: case GEN_HENON:
        I64(s->state.map.x); I64(s->state.map.y); U64(s->state.map.count); break;
    case GEN_MULTI:
        /* Fixed eight-layer encoding includes zeroed unused positions. */
        for (j = 0; j < 8; j++) { U32(s->state.multi.position[j]); U32(s->state.multi.period[j]); U32(s->state.multi.weight[j]); }
        for (j = 0; j < 256; j++) {
            uint16_t value = (uint16_t)s->state.multi.motif[j];
            U16(value); s->state.multi.motif[j] = (int16_t)(value <= 32767u ? (int)value : (int)value - 65536);
        }
        break;
    case GEN_WHITE: break;
    default:
        U32(s->state.seq.step); U32(s->state.seq.length); U32(s->state.seq.rotation); U8(s->state.seq.word_slot); break;
    }
}

static void body_io(const gen_bank_t *b, io_t *c)
{
    unsigned i, j;
    io(c, 'G', 1); io(c, 'N', 1); io(c, 'S', 1); io(c, '1', 1);
    io(c, 1, 2); io(c, 0, 2);
    io(c, b->program_hash, 8); io(c, b->fs, 4); io(c, b->ctl, 4); io(c, b->tick, 8);
    io(c, b->source_mask, 2); io(c, b->lane_mask, 1); io(c, b->words, 1);
    for (i = 0; i < GEN_WORDS; i++) io(c, b->word_length[i], 4);
    for (i = 0; i < GEN_SOURCES; i++) if (b->source_mask & (1u << i)) {
        gen_source_t copy = b->source[i];
        source_io(c, &copy);
    }
    for (i = 0; i < GEN_LANES; i++) if (b->lane_mask & (1u << i)) {
        const gen_lane_t *l = &b->lane[i];
        io(c, i, 1); io(c, l->enabled, 1); io(c, l->active, 1);
        io(c, l->has_pending, 1); io(c, l->pending_enabled, 1);
        io(c, l->next_step, 8); io(c, l->off_tick, 8);
    }
    for (i = 0; i < b->words; i++) for (j = 0; j < b->word_length[i]; j++) io(c, b->memory.word[i][j], 1);
}

static uint64_t bytes_hash(const uint8_t *data, size_t n)
{
    uint64_t h = UINT64_C(0xcbf29ce484222325);
    size_t i;
    for (i = 0; i < n; i++) h = (h ^ data[i]) * UINT64_C(0x100000001b3);
    return h;
}

static int state_overlap(const gen_bank_t *b, const gen_program_t *p, const gen_memory_t *m, const void *ptr, size_t n)
{
    unsigned i;
    if (overlap(ptr, n, b, sizeof *b) || overlap(ptr, n, p, sizeof *p) || overlap(ptr, n, m, sizeof *m)) return 1;
    for (i = 0; i < GEN_WORDS; i++) if (overlap(ptr, n, m->word[i], m->word_capacity[i])
        || overlap(ptr, n, m->scratch[i], m->scratch_capacity[i])) return 1;
    return 0;
}

gen_status_t gen_save(const gen_bank_t *b, uint8_t *dst, size_t capacity, size_t *written)
{
    io_t cursor;
    size_t required;
    uint64_t checksum;
    if (!b || !b->program || !dst || !written) return BAD(GEN_ARGUMENT, 0, GEN_FIELD_MEMORY);
    if (state_overlap(b, b->program, &b->memory, dst, capacity)
        || state_overlap(b, b->program, &b->memory, written, sizeof *written)
        || overlap(dst, capacity, written, sizeof *written)) return BAD(GEN_OVERLAP, 0, GEN_FIELD_MEMORY);
    cursor.src = 0; cursor.dst = 0; cursor.size = GEN_SNAPSHOT_CAP - 8; cursor.at = 0; cursor.bad = 0;
    body_io(b, &cursor); required = cursor.at + 8;
    if (cursor.bad || required > capacity) return BAD(GEN_CAPACITY, 0, GEN_FIELD_MEMORY);
    cursor.dst = dst; cursor.size = capacity; cursor.at = 0;
    body_io(b, &cursor); checksum = bytes_hash(dst, cursor.at); io(&cursor, checksum, 8);
    *written = cursor.at;
    return OK;
}

static const gen_source_config_t *find_source(const gen_program_t *p, unsigned id)
{
    unsigned i;
    for (i = 0; i < p->sources; i++) if (p->source[i].id == id) return &p->source[i];
    return 0;
}

static int restored_source_valid(const gen_source_t *s, const gen_source_config_t *original,
                                 const gen_program_t *p, const uint32_t *word_lengths)
{
    gen_source_config_t pending;
    unsigned i;
    uint32_t length = 0;
    if (!original || s->config.id != original->id || s->config.kind != original->kind
        || s->config.div != original->div || s->config.parent != original->parent || !s->random
        || s->enabled > 1 || s->pending.enabled > 1 || s->pending.slew > 512 || s->pending.flags > 127) return 0;
    if (s->config.kind == GEN_FIBONACCI || s->config.kind == GEN_LSYSTEM) {
        const unsigned slot = text_slot(p, s->config.id);
        if (slot >= GEN_WORDS || s->state.seq.word_slot != slot) return 0;
        length = word_lengths[slot];
    }
    if (config_valid(&s->config, length).code) return 0;
    pending = s->config;
    for (i = 0; i < 4; i++) {
        if (!mutable_scalar(s->config.kind, i) && s->config.p[i] != original->p[i]) return 0;
        if (s->pending.flags & P_SCALAR(i)) {
            if (!mutable_scalar(s->config.kind, i)) return 0;
            pending.p[i] = s->pending.p[i];
        } else if (s->pending.p[i]) return 0;
    }
    if (config_valid(&pending, length).code) return 0;
    if (!(s->pending.flags & P_RESET) && s->pending.seed) return 0;
    if (!(s->pending.flags & P_SLEW) && s->pending.slew) return 0;
    if (!(s->pending.flags & P_ENABLED) && s->pending.enabled) return 0;
    for (i = 0; i < 2; i++) if (s->raw[i] < -32768 || s->raw[i] > 32767
        || s->published[i] < -32768 || s->published[i] > 32767) return 0;
    if (s->config.kind != GEN_HENON && (s->raw[1] || s->published[1])) return 0;
    switch (s->config.kind) {
    case GEN_PINK: {
        int32_t sum = 0;
        for (i = 0; i < 16; i++) sum += s->state.pink.row[i];
        return sum == s->state.pink.sum;
    }
    case GEN_BROWN: return s->state.map.x >= -GEN_ONE && s->state.map.x <= GEN_ONE && !s->state.map.y;
    case GEN_LOGISTIC: return s->state.map.x >= 0 && s->state.map.x <= GEN_ONE && !s->state.map.y && !s->state.map.count;
    case GEN_HENON: return s->state.map.x >= -4 * GEN_Q48 && s->state.map.x <= 4 * GEN_Q48
        && s->state.map.y >= -4 * GEN_Q48 && s->state.map.y <= 4 * GEN_Q48;
    case GEN_MULTI: {
        uint32_t period = (uint32_t)s->config.p[0], weight = 32768;
        for (i = 0; i < 8; i++) {
            if (i >= (unsigned)s->config.p[2]) {
                if (s->state.multi.period[i] || s->state.multi.weight[i] || s->state.multi.position[i]) return 0;
            } else {
                if (s->state.multi.period[i] != period || s->state.multi.weight[i] != weight || s->state.multi.position[i] >= period) return 0;
                if (i + 1u < (unsigned)s->config.p[2]) period *= (uint32_t)s->config.p[1];
                weight = (uint32_t)((uint64_t)weight * (uint32_t)s->config.p[3] / 32768u);
            }
        }
        return 1;
    }
    case GEN_WHITE: return 1;
    default: {
        const unsigned rfield = s->config.kind == GEN_EUCLID ? 2 : s->config.kind == GEN_LSYSTEM ? 0 : 1;
        const uint32_t seed = s->config.seed ? s->config.seed : ZERO_SEED;
        if (s->config.kind == GEN_EUCLID) length = (uint32_t)s->config.p[1];
        else if (s->config.kind == GEN_THUE || s->config.kind == GEN_CANTOR) length = (uint32_t)s->config.p[0];
        return length && s->state.seq.length == length && s->state.seq.step < length
            && s->state.seq.rotation == (seed % length + (uint32_t)s->config.p[rfield]) % length
            && ((s->config.kind == GEN_FIBONACCI || s->config.kind == GEN_LSYSTEM) || !s->state.seq.word_slot);
    }
    }
}

static int restore_pass(gen_bank_t *b, const gen_program_t *p, const gen_memory_t *m,
                         const uint8_t *src, size_t n, unsigned write)
{
    io_t cursor, *c = &cursor;
    uint64_t digest, tick, magic, version, reserved;
    uint32_t fs, ctl, word_lengths[GEN_WORDS];
    unsigned mask, lanes, words, expected_mask = 0, expected_lanes = 0, expected_words = 0, i, j;
    cursor.src = src; cursor.dst = 0; cursor.at = 0; cursor.size = n - 8; cursor.bad = 0;
    magic = io(c, 0, 4); version = io(c, 0, 2); reserved = io(c, 0, 2);
    if (!write && (magic != UINT32_C(0x31534e47) || version != 1 || reserved)) return 0;
    digest = io(c, 0, 8); fs = (uint32_t)io(c, 0, 4); ctl = (uint32_t)io(c, 0, 4); tick = io(c, 0, 8);
    mask = (unsigned)io(c, 0, 2); lanes = (unsigned)io(c, 0, 1); words = (unsigned)io(c, 0, 1);
    for (i = 0; i < GEN_WORDS; i++) word_lengths[i] = (uint32_t)io(c, 0, 4);
    if (!write) {
        if (program_valid(b, p, m, fs, ctl).code || digest != program_hash(p)
            || tick > (UINT64_MAX - 1u) / ctl + 1u) return 0;
        for (i = 0; i < p->sources; i++) {
            expected_mask |= 1u << p->source[i].id;
            if (p->source[i].kind == GEN_FIBONACCI || p->source[i].kind == GEN_LSYSTEM) expected_words++;
        }
        for (i = 0; i < p->lanes; i++) expected_lanes |= 1u << p->lane[i].id;
        if (mask != expected_mask || lanes != expected_lanes || words != expected_words) return 0;
        for (i = 0; i < GEN_WORDS; i++) if (word_lengths[i] > m->word_capacity[i]
            || (i < words ? !word_lengths[i] : word_lengths[i] != 0)) return 0;
    } else {
        memset(b, 0, sizeof *b);
        b->program = p; b->memory = *m; b->program_hash = digest; b->tick = tick; b->fs = fs; b->ctl = ctl;
        b->source_mask = (uint16_t)mask; b->lane_mask = (uint8_t)lanes; b->words = (uint8_t)words;
        for (i = 0; i < GEN_WORDS; i++) b->word_length[i] = word_lengths[i];
    }
    for (i = 0; i < GEN_SOURCES; i++) if (mask & (1u << i)) {
        gen_source_t s;
        memset(&s, 0, sizeof s); source_io(c, &s);
        if (!write && (c->bad || !restored_source_valid(&s, find_source(p, i), p, word_lengths))) return 0;
        if (write) b->source[i] = s;
    }
    for (i = 0; i < GEN_LANES; i++) if (lanes & (1u << i)) {
        gen_lane_t l;
        const unsigned id = (unsigned)io(c, 0, 1);
        memset(&l, 0, sizeof l);
        for (j = 0; j < p->lanes; j++) if (p->lane[j].id == i) l.config = p->lane[j];
        U8(l.enabled); U8(l.active); U8(l.has_pending); U8(l.pending_enabled); U64(l.next_step); U64(l.off_tick);
        if (!write && (id != i || l.enabled > 1 || l.active > 1 || l.has_pending > 1 || l.pending_enabled > 1
            || (!l.has_pending && l.pending_enabled) || (l.active ? (!l.enabled || l.off_tick < tick) : l.off_tick != 0)
            || (l.enabled && l.next_step < tick))) return 0;
        if (write) b->lane[i] = l;
    }
    for (i = 0; i < words; i++) {
        const uint8_t *expected = 0;
        if (!write) {
            uint32_t expected_length = 0;
            const gen_source_config_t *config = 0;
            for (j = 0; j < p->sources; j++) if ((p->source[j].kind == GEN_FIBONACCI || p->source[j].kind == GEN_LSYSTEM)
                && text_slot(p, p->source[j].id) == i) config = &p->source[j];
            if (!config || expand(p, config, m, &expected_length, &expected).code || expected_length != word_lengths[i]) return 0;
        }
        for (j = 0; j < word_lengths[i]; j++) {
            const uint8_t value = (uint8_t)io(c, 0, 1);
            if (!write && value != expected[j]) return 0;
            if (write) m->word[i][j] = value;
        }
    }
    return write ? 1 : !c->bad && c->at == n - 8;
}

gen_status_t gen_restore(gen_bank_t *b, const gen_program_t *p, gen_memory_t *m, const uint8_t *src, size_t n)
{
    uint64_t checksum = 0;
    unsigned i;
    gen_status_t st = memory_valid(b, p, m);
    if (st.code) return st;
    if (!src || n < 52 || n > GEN_SNAPSHOT_CAP) return BAD(GEN_SNAPSHOT, 0, GEN_FIELD_MEMORY);
    if (state_overlap(b, p, m, src, n)) return BAD(GEN_OVERLAP, 0, GEN_FIELD_MEMORY);
    for (i = 0; i < 8; i++) checksum |= (uint64_t)src[n - 8 + i] << (8u * i);
    if (checksum != bytes_hash(src, n - 8) || !restore_pass(b, p, m, src, n, 0))
        return BAD(GEN_SNAPSHOT, 0, GEN_FIELD_NONE);
    /* Stable, disjoint bytes and immutable program: the second pass only
     * copies already validated fields. There is no expansion or refusal. */
    (void)restore_pass(b, p, m, src, n, 1);
    return OK;
}
#undef U8
#undef U16
#undef U32
#undef U64
#undef I32
#undef I64
