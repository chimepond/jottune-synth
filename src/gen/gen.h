/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0 */
#ifndef GEN_H
#define GEN_H
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Standalone  control generators. Call at a session grid boundary;
 * there is no audio callback, allocation, global clock or voice owner here.
 * Program and memory descriptors/buffers must outlive the bank and must
 * remain disjoint. Programs are immutable until the bank is reinitialized. */
#define GEN_SOURCES 16
#define GEN_LANES 8
#define GEN_WORDS 2
#define GEN_WORD_CAP 16384
#define GEN_SNAPSHOT_CAP 65536
#define GEN_Q48 INT64_C(281474976710656)
#define GEN_ONE INT64_C(4611686018427387904)

enum {
    GEN_WHITE = 1, GEN_PINK, GEN_BROWN, GEN_MULTI, GEN_LOGISTIC, GEN_HENON,
    GEN_EUCLID, GEN_THUE, GEN_CANTOR, GEN_FIBONACCI, GEN_LSYSTEM
};
enum { GEN_OK, GEN_ARGUMENT, GEN_RANGE, GEN_DEPENDENCY, GEN_CAPACITY,
       GEN_OVERFLOW, GEN_OVERLAP, GEN_SNAPSHOT, GEN_UNIMPLEMENTED };
enum { GEN_FIELD_NONE, GEN_FIELD_KIND, GEN_FIELD_SEED, GEN_FIELD_DIV,
       GEN_FIELD_SLEW, GEN_FIELD_P0, GEN_FIELD_P1, GEN_FIELD_P2, GEN_FIELD_P3,
       GEN_FIELD_WORD, GEN_FIELD_LANE, GEN_FIELD_CLOCK, GEN_FIELD_MEMORY };
typedef struct { uint8_t code, id, field; } gen_status_t;

/* Scalar fields p[0..3], explicitly required (gen_defaults fills defaults):
 * brown: STEP; multi: P,K,OCTAVES,GAIN; logistic: R_Q48;
 * henon: A_Q48,B_Q48; euclid: K,N,ROT; thue/cantor: LENGTH,ROT;
 * fibonacci: DEPTH,ROT; lsystem: ROT, with a matching text definition.
 * Unused p fields must be zero. parent=-1 or any existing smaller source
 * ID; only a Euclidean child can have a parent gate. */
typedef struct {
    int64_t p[4];
    uint32_t seed;
    uint16_t div, slew;
    uint8_t id, kind;
    int8_t parent;
} gen_source_config_t;

typedef struct {
    uint8_t id, depth, rule_mask;
    char axiom[33], rule[8][33]; /* empty rule string means deletion */
} gen_text_config_t;

typedef struct {
    int32_t base_cents;
    uint16_t voice, step_ticks, gate_ticks, span_cents, velocity, scale_mask;
    uint8_t id;
    int8_t gate, pitch, accent; /* -1 means absent */
} gen_lane_config_t;

typedef struct {
    gen_source_config_t source[GEN_SOURCES];
    gen_lane_config_t lane[GEN_LANES];
    gen_text_config_t text[GEN_WORDS];
    uint8_t sources, lanes, texts;
} gen_program_t;

typedef struct {
    uint8_t *scratch[2], *word[GEN_WORDS];
    size_t scratch_capacity[2], word_capacity[GEN_WORDS];
} gen_memory_t;

typedef struct {
    int64_t p[4];
    uint32_t seed;
    uint16_t flags, slew;
    uint8_t enabled;
} gen_pending_t;

typedef struct {
    gen_source_config_t config;
    gen_pending_t pending;
    uint32_t random;
    int32_t raw[2], published[2];
    uint8_t enabled;
    union {
        struct { int16_t row[16]; int32_t sum; uint16_t counter; } pink;
        struct { int64_t x, y; uint64_t count; } map;
        struct {
            int16_t motif[256];
            uint32_t position[8], period[8], weight[8];
        } multi;
        struct { uint32_t step, length, rotation; uint8_t word_slot; } seq;
    } state;
} gen_source_t;

typedef struct {
    gen_lane_config_t config;
    uint64_t next_step, off_tick;
    uint8_t enabled, active, pending_enabled, has_pending;
} gen_lane_t;

typedef struct {
    gen_source_t source[GEN_SOURCES];
    gen_lane_t lane[GEN_LANES];
    const gen_program_t *program;
    gen_memory_t memory;
    uint64_t tick, program_hash;
    uint32_t fs, ctl;
    uint16_t source_mask;
    uint8_t lane_mask, words;
    uint32_t word_length[GEN_WORDS];
} gen_bank_t;

typedef struct {
    int32_t cents;
    uint16_t voice, velocity;
    uint8_t on, lane;
} gen_event_t;

typedef struct {
    int32_t value[GEN_SOURCES][2];
    uint64_t frame;
    gen_event_t event[2 * GEN_LANES];
    uint8_t events;
} gen_frame_t;

enum { GEN_PARAM_ENABLED, GEN_PARAM_SLEW, GEN_PARAM_P0, GEN_PARAM_P1,
       GEN_PARAM_P2, GEN_PARAM_P3 };
typedef uint8_t gen_param_t;

/* Tick, set, reset, lane-enable and save require a successfully initialized
 * or restored bank. Callers must not edit its internal state directly.
 * All refusals leave the bank, words and caller output unchanged. Scratch
 * is work memory and may change during an init/restore preflight.
 * Tick overwrites one complete frame: all source values, absolute sample
 * frame and ordered lane events. Sources are evaluated once per bank tick,
 * independent of readers; absent source slots and unused events are zero. */
gen_status_t gen_defaults(gen_source_config_t *, uint8_t kind, uint8_t id, uint32_t seed);
gen_status_t gen_init(gen_bank_t *, const gen_program_t *, gen_memory_t *, uint32_t fs, uint32_t ctl);
gen_status_t gen_tick(gen_bank_t *, gen_frame_t *);
gen_status_t gen_set(gen_bank_t *, uint8_t id, gen_param_t, int64_t value);
gen_status_t gen_reset(gen_bank_t *, uint8_t id, uint32_t seed);
gen_status_t gen_lane_enable(gen_bank_t *, uint8_t id, uint8_t enabled);
gen_status_t gen_save(const gen_bank_t *, uint8_t *dst, size_t capacity, size_t *written);
gen_status_t gen_restore(gen_bank_t *, const gen_program_t *, gen_memory_t *, const uint8_t *, size_t);
/* A wider result represents the downward snap even for INT32_MIN input.
 * mask0 bypasses; masks above4095 return INT64_MIN (outside every result). */
int64_t gen_quantize(int32_t cents, uint16_t mask);
#ifdef __cplusplus
}
#endif

#endif
