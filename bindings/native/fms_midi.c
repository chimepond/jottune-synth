/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * fms_midi.c: live MIDI input (shared MIDI translator; , , ).
 * MIDI 1.0 channel messages, as Universal MIDI Packets, to fms_event_t by
 * the rules midi2tl.py converts a Standard MIDI File with (Conversion in
 * tools/music/midi2tl.py):
 *   - note-on and note-off (a note-on at velocity 0 is a note-off); a
 *     note-off ends the oldest note of its key; velocity to Q15 as
 *     velocity * 32767 / 127, truncated;
 *   - the sustain pedal (CC 64 at 64 or more holds): a note-off while it is
 *     down keeps the note until the pedal goes up or its key is struck again;
 *   - pitch bend over a range of 2 semitones, or what RPN 0 sets (CC 101 and
 *     100 select, CC 6 sets semitones and cents 0, CC 38 sets cents up to 99),
 *     applied to the notes sounding and to the notes struck after;
 *   - CC 74 as the cutoff base * 2^((cc - 64) / 16), when a cutoff is given;
 *   - the voices by midi2tl.py's pool (Pool.pick): the voice whose release
 *     ended first, else a voice never used (the lowest), else the voice
 *     released first (a release steal), else the note struck first (a steal,
 *     which writes its off first).
 * With the optional  map, selected controllers, pressure, poly pressure,
 * release velocity and seeded per-note random values write CTL before notes.
 * Other messages (programs, unmapped pressure and controllers,
 * MIDI clock, transport, SysEx) is counted and ignored. No CoreMIDI here:
 * the app's MidiInput.swift feeds it, and fms_verify drives it against
 * midi2tl.py (tools/midi_check.py).
 *
 * Frequencies and cutoffs are value * 2^(N / D), rounded half up, in 128-bit
 * integers (: no libm): the fraction's exp(r ln 2 / D) by its Taylor
 * series in Q1.127, which errs by less than 2^-120 of the value; a result
 * within 2^-60 of a half is counted in near_ties, since only there could
 * midi2tl.py's 50-digit decimal round the other way. fms_verify --midi-scan
 * finds no pitch of the bend and RPN domain that near.
 *
 * Live, unlike midi2tl.py: bends and CC 74 moves less than 1 ms apart do not
 * merge, a note shorter than 1 ms is not lengthened, a move and a note-on at
 * one time both apply, and one pool serves every channel let through (in
 * midi2tl.py each channel has its own). Times only order the pool's choices:
 * the events apply at the live player's current frame (fms_host_push).
 */
#include <stdlib.h>
#include <stdio.h>
#include <ctype.h>
#include <string.h>
#include "jottune_bridge.h"

__extension__ typedef unsigned __int128 u128;

#define NOTES      512u                     /* notes whose key is down or the pedal holds */
#define NONE       UINT64_MAX               /* a voice's off while its note is held */
#define PITCH_DEN  9830400                  /* 12 * 100 * 8192: the pitch exponent's unit */

#ifndef FMS_MIDI_NEAR_BITS
#define FMS_MIDI_NEAR_BITS 4                /* near: within 2^(BITS - 64) of a half */
#endif
#ifndef FMS_MIDI_MUTANT
#define FMS_MIDI_MUTANT 0                   /* fms_verify's negative controls: 1 a wrong velocity map, 2 a dropped note-off, 3 a wrong ctl map, 4 a wrong member range */
#endif

/* ln 2 in Q0.128, truncated */
static const u128 LN2 = ((u128)0xB17217F7D1CF79ABull << 64) | 0xC9E3B39803F2F6AFull;

typedef struct {
    uint64_t seq;                /* its note-on's place among the messages */
    int32_t  voice;              /* -1: none (stolen, or taken by the keyboard) */
    uint8_t  used, chan, key, kept, up, has_up;
} mnote_t;

typedef struct {
    uint64_t on, order, off;     /* off: NONE while held */
    int64_t member_bend;         /* exact cents/8192 captured by this zone note */
    int32_t  note;               /* its entry in the note table while held or kept, else -1 */
    uint32_t freq, cutoff;
    uint8_t  used, ext, chan, key;
    int32_t ctl[8];              /* last value written to this engine voice */
} mvoice_t;

typedef struct {
    int64_t  bend;               /* (bend value - 8192) * range, cents / 8192 */
    uint32_t range;              /* the bend range in cents, for bends to come */
    int32_t latest;              /* latest member voice; guarded by its order token */
    uint64_t latest_order;
    uint8_t  msb, lsb, sel;      /* the RPN selected (sel 0: an NRPN is, midi2tl.py's None) */
    uint8_t  pedal, cc74, nrpn_m, nrpn_l, nrpn_sel;
    int32_t ctl[8];              /* channel sources; poly pressure is per key */
} mchan_t;

enum { CT_CC = 1, CT_WIDE, CT_NRPN, CT_PRESSURE, CT_POLY, CT_RELEASE, CT_RAND };
#define CT_KEY(kind, value) (((uint32_t)(kind) << 16) | (value))
typedef struct {
    uint32_t key, seed;
    uint8_t input, msb, lsb;
} mctl_t;
typedef struct {
    mctl_t *src;                 /* allocated during setup only, sorted by key */
    size_t count;
    uint32_t seed, rng;
    uint8_t nrpn;
} mmap_t;

struct fms_midi {
    fms_midi_config_t cfg;
    fms_midi_stats_t  st;
    uint64_t last_t, rel_ns, seq;
    mnote_t  note[NOTES];
    mvoice_t v[FMS_MAX_VOICES];
    mchan_t  ch[16];
    mmap_t  map[16];
    uint32_t mapped;
    uint32_t zone_mask;          /* zero off; one selected live zone owns the pool */
    uint8_t master;
    uint8_t started;             /* includes refused input and time-zero external calls */
};

/* ---- setup-only controller maps  ---- */
static int ctl_error(char *err, size_t cap, const char *why)
{
    if (err && cap) (void)snprintf(err, cap, "%s", why);
    return -1;
}
static int ctl_cmp(const void *a, const void *b)
{
    const uint32_t x = ((const mctl_t *)a)->key, y = ((const mctl_t *)b)->key;
    return (x > y) - (x < y);
}
static mctl_t *ctl_find(mmap_t *map, uint32_t key)
{
    size_t lo = 0, hi = map->count;
    while (lo < hi) {
        const size_t mid = lo + (hi - lo) / 2;
        if (map->src[mid].key < key) lo = mid + 1; else hi = mid;
    }
    return lo < map->count && map->src[lo].key == key ? &map->src[lo] : NULL;
}
/* Decimal only, with the same digit limits as midi2tl.py's source grammar. */
static int ctl_uint(const char **p, unsigned digits, uint32_t *out)
{
    uint32_t x = 0;
    unsigned n = 0;
    while (**p >= '0' && **p <= '9') {
        const unsigned d = (unsigned)(*(*p)++ - '0');
        if (++n > digits || x > (UINT32_MAX - d) / 10) return 0;
        x = x * 10 + d;
    }
    *out = x;
    return n != 0;
}
static int ctl_reserved(uint32_t cc)
{
    return cc == 6 || cc == 38 || cc == 64 || (cc >= 96 && cc <= 101);
}
static int ctl_parse(const char *s, mctl_t *out)
{
    uint32_t kind, a = 0, b = 0;
    memset(out, 0, sizeof *out);
    if (!strncmp(s, "cc", 2)) {
        s += 2;
        if (!ctl_uint(&s, 3, &a)) return 0;
        kind = *s == '+' ? (s++, CT_WIDE) : CT_CC;
        if (!a || a > (kind == CT_WIDE ? 31u : 119u) || ctl_reserved(a)) return 0;
    } else if (!strncmp(s, "nrpn", 4)) {
        s += 4;
        if (!ctl_uint(&s, 3, &a) || *s++ != '.' || !ctl_uint(&s, 3, &b) ||
            a > 127 || b > 127 || (a == 127 && b == 127)) return 0;
        kind = CT_NRPN; a = a * 128 + b;
    } else if (!strncmp(s, "rand:", 5)) {
        s += 5;
        if (!ctl_uint(&s, 10, &out->seed) || !out->seed) return 0;
        kind = CT_RAND;
    } else if (!strncmp(s, "pressure", 8)) { kind = CT_PRESSURE; s += 8; }
    else if (!strncmp(s, "polyat", 6)) { kind = CT_POLY; s += 6; }
    else if (!strncmp(s, "relvel", 6)) { kind = CT_RELEASE; s += 6; }
    else return 0;
    if (*s++ != ':' || *s < '0' || *s > '7' || s[1]) return 0;
    out->input = (uint8_t)(*s - '0');
    out->key = CT_KEY(kind, a);
    return 1;
}
int fms_midi_set_ctl(fms_midi_t *m, uint32_t c, const char *spec, char *err, size_t cap)
{
    mmap_t next = { 0 };
    const char *s, *end;
    size_t n = 1, i;
    uint8_t claims[128] = { 0 }, rand_inputs = 0, other_inputs = 0;
    if (err && cap) *err = 0;
    if (!m || c >= 16 || !spec) return ctl_error(err, cap, "ctl: invalid channel or argument");
    if (m->started) return ctl_error(err, cap, "ctl: setup only; reset first");
    while (isspace((unsigned char)*spec)) spec++;
    end = spec + strlen(spec);
    while (end > spec && isspace((unsigned char)end[-1])) end--;
    if (end == spec || ((size_t)(end - spec) == 3 && !strncmp(spec, "off", 3))) {
        free(m->map[c].src); memset(&m->map[c], 0, sizeof m->map[c]); m->mapped &= ~(1u << c);
        return 0;
    }
    if (m->zone_mask && (m->zone_mask & (1u << c)) && c != m->master)
        return ctl_error(err, cap, "ctl: an MPE member cannot have its own map");
    if (!(m->cfg.channels & (1u << c))) return ctl_error(err, cap, "ctl: channel is not enabled");
    for (s = spec; s < end; s++) if (*s == ',') {
        if (n == SIZE_MAX / 7) return ctl_error(err, cap, "ctl: map too large");
        n++;
    }
    /* Each comma item can expand to the seven default sources. */
    if (n > SIZE_MAX / (7 * sizeof *next.src)) return ctl_error(err, cap, "ctl: map too large");
    next.src = (mctl_t *)calloc(n * 7, sizeof *next.src);
    if (!next.src) return ctl_error(err, cap, "ctl: allocation failed");
    for (s = spec; s < end; ) {
        const char *e = s, *a;
        char word[64];
        size_t len;
        while (e < end && *e != ',') e++;
        a = s; while (a < e && isspace((unsigned char)*a)) a++;
        len = (size_t)(e - a); while (len && isspace((unsigned char)a[len - 1])) len--;
        if (!len || len >= sizeof word || (e < end && e + 1 == end)) goto invalid;
        memcpy(word, a, len); word[len] = 0;
        if (!strcmp(word, "default")) {
            static const uint32_t keys[7] = { CT_KEY(CT_CC, 1), CT_KEY(CT_PRESSURE, 0), CT_KEY(CT_POLY, 0),
                CT_KEY(CT_CC, 2), CT_KEY(CT_CC, 11), CT_KEY(CT_RELEASE, 0), CT_KEY(CT_RAND, 0) };
            static const uint8_t inputs[7] = { 0, 1, 1, 2, 3, 5, 6 };
            for (i = 0; i < 7; i++) {
                mctl_t *x = &next.src[next.count++]; x->key = keys[i]; x->input = inputs[i];
                if (i == 6) x->seed = 0x9E3779B9u * (c + 1);
#if FMS_MIDI_MUTANT == 3
                if (i == 0) x->input = 7;
#endif
            }
        } else if (!ctl_parse(word, &next.src[next.count++])) goto invalid;
        s = e < end ? e + 1 : end;
    }
    qsort(next.src, next.count, sizeof *next.src, ctl_cmp);
    for (i = 0; i < next.count; i++) {
        const mctl_t *x = &next.src[i];
        const uint32_t kind = x->key >> 16, a = x->key & 65535;
        if (i && next.src[i - 1].key == x->key) goto invalid;
        if (m->zone_mask && c == m->master && (x->input == 1 || x->input == 4)) {
            free(next.src);
            return ctl_error(err, cap, "ctl: an MPE master cannot map input 1 or 4");
        }
        if (kind == CT_CC || kind == CT_WIDE) {
            if (claims[a] || (a == 74 && m->cfg.cutoff_millihz)) goto invalid;
            claims[a] = 1;
            if (kind == CT_WIDE) { if (claims[a + 32]) goto invalid; claims[a + 32] = 1; }
        }
        if (kind == CT_RAND) { rand_inputs |= (uint8_t)(1u << x->input); next.seed = next.rng = x->seed; }
        else other_inputs |= (uint8_t)(1u << x->input);
        if (kind == CT_NRPN) next.nrpn = 1;
    }
    if (rand_inputs & other_inputs) goto invalid;
    free(m->map[c].src); m->map[c] = next; m->mapped |= 1u << c;
    return 0;
invalid:
    free(next.src);
    return ctl_error(err, cap, "ctl: invalid source, duplicate claim, rand sharing or CC74/filter conflict");
}

/* One app part owns one pool. Multiple simultaneous zones require a separate
 * part/pool contract and are explicitly refused here. No MIDI specification
 * conformance claim: this ports the existing  converter behavior. */
int fms_midi_set_mpe(fms_midi_t *m, const char *spec, char *err, size_t cap)
{
    const char *end;
    uint32_t mask, count = 0, c, master;
    size_t i;
    if (err && cap) *err = 0;
    if (!m || !spec) return ctl_error(err, cap, "mpe: invalid argument");
    if (m->started) return ctl_error(err, cap, "mpe: setup only; reset first");
    while (isspace((unsigned char)*spec)) spec++;
    end = spec + strlen(spec);
    while (end > spec && isspace((unsigned char)end[-1])) end--;
    if (end == spec || ((size_t)(end - spec) == 3 && !strncmp(spec, "off", 3))) {
        m->zone_mask = 0; m->master = 0; fms_midi_reset(m); return 0;
    }
    if (!strncmp(spec, "lower:", 6)) master = 0;
    else if (!strncmp(spec, "upper:", 6)) master = 15;
    else return ctl_error(err, cap, "mpe: expected off, lower:N or upper:N");
    spec += 6;
    if (spec == end) return ctl_error(err, cap, "mpe: member count is 1 to 15");
    for (; spec < end; spec++) {
        if (*spec < '0' || *spec > '9' || count > 15) return ctl_error(err, cap, "mpe: one zone, 1 to 15 members");
        count = count * 10 + (uint32_t)(*spec - '0');
    }
    if (!count || count > 15) return ctl_error(err, cap, "mpe: member count is 1 to 15");
    if (!(m->cfg.channels & (1u << master))) return ctl_error(err, cap, "mpe: master channel is not enabled");
    mask = master == 0 ? (1u << (count + 1)) - 1 : (0xFFFFu << (15 - count)) & 0xFFFFu;
    for (c = 0; c < 16; c++) {
        if (!(mask & (1u << c))) continue;
        if (c != master && m->map[c].count) return ctl_error(err, cap, "mpe: a member has its own ctl map");
        if (c == master)
            for (i = 0; i < m->map[c].count; i++)
                if (m->map[c].src[i].input == 1 || m->map[c].src[i].input == 4)
                    return ctl_error(err, cap, "mpe: master ctl map cannot target input 1 or 4");
    }
    m->master = (uint8_t)master; m->zone_mask = mask;
    fms_midi_reset(m);
    return 0;
}

/* ---- 2^(N / D) ---- */

/* The top 128 bits of a * b. */
static u128 mulhi(u128 a, u128 b)
{
    const uint64_t ah = (uint64_t)(a >> 64), al = (uint64_t)a, bh = (uint64_t)(b >> 64), bl = (uint64_t)b;
    const u128 hh = (u128)ah * bh, hl = (u128)ah * bl, lh = (u128)al * bh, ll = (u128)al * bl;
    const u128 mid = (u128)(uint64_t)hl + (uint64_t)lh + (ll >> 64);
    return hh + (hl >> 64) + (lh >> 64) + (mid >> 64);
}

/* a / k, truncated, 1 <= k < 2^32: three 64-bit divisions. */
static u128 div_small(u128 a, uint32_t k)
{
    const uint64_t hi = (uint64_t)(a >> 64), lo = (uint64_t)a;
    const uint64_t qh = hi / k, t1 = ((hi % k) << 32) | (lo >> 32);
    const uint64_t q1 = t1 / k, t0 = ((t1 % k) << 32) | (lo & 0xFFFFFFFFu);
    return ((u128)qh << 64) | (q1 << 32) | (t0 / k);
}

/* exp(r ln 2 / den) = 2^(r / den) in Q1.127, for 0 <= r < den < 2^32. */
static u128 exp_frac(uint64_t r, uint64_t den)
{
    const u128 x = (u128)r * (LN2 / den) + (u128)r * (uint64_t)(LN2 % den) / den;   /* Q0.128 */
    u128 s = (u128)1 << 127, t = s;
    uint32_t k;
    for (k = 1; ; k++) {
        t = div_small(mulhi(t, x), k);
        if (!t) break;
        s += t;
    }
    return s;
}

/* value * 2^q * s / 2^127, rounded half up; *near when the exact product
 * lies within 2^-60 of a half. Beyond 2^32 the result only needs to say so. */
static uint64_t apply_scale(uint32_t value, int64_t q, u128 s, int *near)
{
    const uint64_t half = (uint64_t)1 << 63;
    u128 lo, hp, mid, r;
    uint64_t p0, p1, p2, f;
    int sh;
    *near = 0;
    if (!value || q < -64) return 0;
    if (q >= 32) return (uint64_t)1 << 40;
    lo = (u128)value * (uint64_t)s;
    hp = (u128)value * (uint64_t)(s >> 64);
    p0 = (uint64_t)lo;
    mid = (lo >> 64) + (uint64_t)hp;
    p1 = (uint64_t)mid;
    p2 = (uint64_t)((hp >> 64) + (mid >> 64));
    sh = 63 - (int)q;                        /* 32 to 127: r = floor(result * 2^64) */
    if (sh >= 64) r = (((u128)p2 << 64) | p1) >> (sh - 64);
    else r = ((((u128)p2 << 64) | p1) << (64 - sh)) | (p0 >> sh);
    f = (uint64_t)r;
    *near = (f > half ? f - half : half - f) < ((uint64_t)1 << FMS_MIDI_NEAR_BITS);
    return (uint64_t)(r >> 64) + (f >= half);
}

/* value * 2^(num / den), rounded half up; den > 0. An integral exponent is
 * exact, as compose.pitch_scale makes it. */
static uint64_t scale2(uint32_t value, int64_t num, int64_t den, int *near)
{
    int64_t q = num / den, r = num % den;
    if (r < 0) { r += den; q--; }
    *near = 0;
    if (r) return apply_scale(value, q, exp_frac((uint64_t)r, (uint64_t)den), near);
    if (!value || q < -40) return 0;
    if (q >= 32) return (uint64_t)1 << 40;
    if (q >= 0) return (uint64_t)value << q;
    return ((uint64_t)value + ((uint64_t)1 << (-q - 1))) >> -q;
}

int fms_midi_pitch_scan(uint64_t *evaluated, uint64_t *near_ties)
{
    /* Every key (0..127) plus two bends (master/member, each -8192..8191
     * at up to 12799 cents) lies in [-28, 27) octaves from A4. Test each
     * nonzero fraction r once at every integral part q; integral exponents
     * use scale2's exact branch and need no near-half scan. */
    uint64_t r, n = 0, nn = 0;
    int64_t q;
    for (r = 1; r < PITCH_DEN; r++) {
        const u128 s = exp_frac(r, PITCH_DEN);
        for (q = -28; q < 27; q++) {
            int near;
            (void)apply_scale(440000, q, s, &near);
            n++;
            nn += (uint64_t)near;
        }
    }
    *evaluated = n;
    *near_ties = nn;
    return nn != 0;
}

/* ---- the translator ---- */

static uint32_t pitch_of(fms_midi_t *m, uint32_t key, int64_t bend)
{
    int near;
    uint64_t f = scale2(440000, ((int64_t)key - 69) * 819200 + bend, PITCH_DEN, &near);
    m->st.near_ties += (uint64_t)near;
    if (f > 0xFFFFFFFFu) { m->st.clamped++; f = 0xFFFFFFFFu; }
    return (uint32_t)f;
}

static uint32_t cutoff_of(fms_midi_t *m, uint32_t cc)
{
    int near;
    uint64_t c = scale2(m->cfg.cutoff_millihz, (int64_t)cc - 64, 16, &near);
    m->st.near_ties += (uint64_t)near;
    return c < 1 ? 1 : c > 0xFFFFFFFFu ? 0xFFFFFFFFu : (uint32_t)c;
}

static void emit(fms_midi_t *m, fms_event_t *out, uint32_t *n, uint64_t t, uint32_t type, uint32_t voice,
                 uint32_t freq, uint32_t value)
{
    fms_event_t *e = &out[(*n)++];
    memset(e, 0, sizeof *e);
    e->t_ns = t;
    e->type = type;
    e->voice = voice;
    e->stream = m->cfg.stream;
    e->freq_millihz = freq;
    e->value = value;
}

static int32_t ctl_q7(uint32_t value) { return (int32_t)((value * 32768u + 63u) / 127u); }
static void ctl_write(fms_midi_t *m, uint32_t k, uint32_t input, int32_t value, uint64_t t,
                      fms_event_t *out, uint32_t *n)
{
    fms_event_t *e;
    if (!(m->cfg.ctl_reads & (1u << input)) || m->v[k].ctl[input] == value) return;
    m->v[k].ctl[input] = value;
    emit(m, out, n, t, FMS_EV_CTL, k, 0, 0);
    e = &out[*n - 1]; e->bus = input; e->x = value;
    m->st.ctl_events++;
}

/* Voice k's note ends at t: its off, once. */
static void end_voice(fms_midi_t *m, uint32_t k, uint64_t t, fms_event_t *out, uint32_t *n)
{
    mvoice_t *v = &m->v[k];
    if (!v->used || v->off != NONE) return;
    v->off = t;
    v->note = -1;
    emit(m, out, n, t, FMS_EV_OFF, k, 0, 0);
}

/* A note leaves the table; its voice's note ends if it still has one. */
static void end_note(fms_midi_t *m, uint32_t i, uint64_t t, fms_event_t *out, uint32_t *n)
{
    if (m->note[i].voice >= 0) {
        const mnote_t *note = &m->note[i];
        mctl_t *src = ctl_find(&m->map[m->zone_mask ? m->master : note->chan], CT_KEY(CT_RELEASE, 0));
        if (src && note->has_up) ctl_write(m, (uint32_t)note->voice, src->input, ctl_q7(note->up), t, out, n);
        end_voice(m, (uint32_t)note->voice, t, out, n);
    }
    m->note[i].used = 0;
}

/* Sounding at t for channel c's bends and CC 74: held, or still in its release. */
static int sounding(const fms_midi_t *m, uint32_t k, uint32_t c, uint64_t t)
{
    const mvoice_t *v = &m->v[k];
    return v->used && !v->ext && (v->chan == c || (m->zone_mask && c == m->master &&
           (m->zone_mask & (1u << v->chan)))) && (v->off == NONE || v->off + m->rel_ns > t);
}

/* Accepted source messages update channel state even before the first note.
 * Poly pressure changes only matching sounding keys, never the channel latch. */
static uint32_t ctl_message(fms_midi_t *m, uint64_t t, uint32_t c, mctl_t *src, int32_t value,
                             int key, fms_event_t *out)
{
    uint32_t k, n = 0;
    m->st.ctl_messages++;
    if (key < 0) m->ch[c].ctl[src->input] = value;
    for (k = 0; k < m->cfg.nvoices; k++)
        if (sounding(m, k, c, t) && (key < 0 || m->v[k].key == (uint32_t)key))
            ctl_write(m, k, src->input, value, t, out, &n);
    return n;
}
static int ctl_cc(fms_midi_t *m, uint64_t t, uint32_t c, uint32_t a, uint32_t b,
                   fms_event_t *out, uint32_t *n)
{
    mmap_t *map = &m->map[c];
    mchan_t *ch = &m->ch[c];
    mctl_t *src;
    if (!map->count) return 0;
    if (a == 99 || a == 98) {
        if (!ch->nrpn_sel) { ch->nrpn_m = ch->nrpn_l = 127; ch->nrpn_sel = 1; }
        if (a == 99) ch->nrpn_m = (uint8_t)b; else ch->nrpn_l = (uint8_t)b;
        return map->nrpn;
    }
    if (a == 101 || a == 100) { ch->nrpn_sel = 0; return map->nrpn; }
    src = ctl_find(map, CT_KEY(CT_CC, a));
    if (src) { *n = ctl_message(m, t, c, src, ctl_q7(b), -1, out); return 1; }
    src = a <= 31 ? ctl_find(map, CT_KEY(CT_WIDE, a)) :
          a >= 33 && a <= 63 ? ctl_find(map, CT_KEY(CT_WIDE, a - 32)) : NULL;
    if (!src && (a == 6 || a == 38) && ch->nrpn_sel)
        src = ctl_find(map, CT_KEY(CT_NRPN, (uint32_t)ch->nrpn_m * 128 + ch->nrpn_l));
    if (!src) return 0;
    if (a <= 31) { src->msb = (uint8_t)b; src->lsb = 0; } else src->lsb = (uint8_t)b;
    *n = ctl_message(m, t, c, src, (int32_t)(((src->msb * 128u + src->lsb) * 32768u + 8191u) / 16383u), -1, out);
    return 1;
}
static void ctl_on(fms_midi_t *m, uint64_t t, uint32_t c, uint32_t k, fms_event_t *out, uint32_t *n)
{
    const uint32_t source = m->zone_mask ? m->master : c;
    mmap_t *map = &m->map[source];
    mctl_t *rand = ctl_find(map, CT_KEY(CT_RAND, 0));
    uint32_t i;
    if (!map->count && !m->zone_mask) return;
    if (rand) { map->rng ^= map->rng << 13; map->rng ^= map->rng >> 17; map->rng ^= map->rng << 5; }
    for (i = 0; i < 8; i++) {
        const int32_t value = rand && rand->input == i ? (int32_t)(map->rng >> 16) - 32768 :
            m->ch[m->zone_mask && (i == 1 || i == 4) ? c : source].ctl[i];
        ctl_write(m, k, i, value, t, out, n);
    }
}

/* midi2tl.py's Pool.pick. */
static uint32_t pick(fms_midi_t *m, uint64_t t)
{
    uint32_t k, best = UINT32_MAX;
    uint64_t a = 0, b = 0;
    for (k = 0; k < m->cfg.nvoices; k++) {              /* free: the release that ended first */
        const mvoice_t *v = &m->v[k];
        if (v->used && v->off != NONE && v->off + m->rel_ns <= t && (best == UINT32_MAX || v->off + m->rel_ns < a)) {
            best = k; a = v->off + m->rel_ns;
        }
    }
    if (best != UINT32_MAX) return best;
    for (k = 0; k < m->cfg.nvoices; k++)                /* a voice never used */
        if (!m->v[k].used) return k;
    for (k = 0; k < m->cfg.nvoices; k++) {              /* released first */
        const mvoice_t *v = &m->v[k];
        if (v->off != NONE && v->off <= t && (best == UINT32_MAX || v->off < a)) { best = k; a = v->off; }
    }
    if (best != UINT32_MAX) { m->st.release_steals++; return best; }
    for (k = 0; k < m->cfg.nvoices; k++) {              /* struck first */
        const mvoice_t *v = &m->v[k];
        if (best == UINT32_MAX || v->on < a || (v->on == a && v->order < b)) { best = k; a = v->on; b = v->order; }
    }
    m->st.steals++;
    return best;
}

static uint32_t note_on(fms_midi_t *m, uint64_t t, uint32_t c, uint32_t key, uint32_t vel, fms_event_t *out)
{
    mchan_t *ch = &m->ch[c];
    uint32_t i, k, n = 0, slot = NOTES;
    mvoice_t *v;
    for (i = 0; i < NOTES && slot == NOTES; i++)
        if (!m->note[i].used) slot = i;
    if (slot == NOTES) { m->st.refused++; return 0; }    /* the table is full: no change */
    for (i = 0; i < NOTES; i++)                          /* its key struck again under the pedal */
        if (m->note[i].used && m->note[i].kept && m->note[i].chan == c && m->note[i].key == key) {
            end_note(m, i, t, out, &n);
            m->st.restruck++;
        }
    m->st.notes++;
    k = pick(m, t);
    v = &m->v[k];
    if (v->used && v->off == NONE) {                     /* a steal: the note it takes ends now */
        if (v->note >= 0) m->note[v->note].voice = -1;
        end_voice(m, k, t, out, &n);
    }
    m->note[slot].used = 1; m->note[slot].kept = 0; m->note[slot].has_up = 0;
    m->note[slot].chan = (uint8_t)c; m->note[slot].key = (uint8_t)key;
    m->note[slot].seq = m->seq; m->note[slot].voice = (int32_t)k;
    v->used = 1; v->ext = 0; v->chan = (uint8_t)c; v->key = (uint8_t)key;
    v->on = t; v->order = m->seq; v->off = NONE; v->note = (int32_t)slot;
    v->member_bend = m->zone_mask && c != m->master ? ch->bend : 0;
    v->freq = pitch_of(m, key, m->zone_mask ? m->ch[m->master].bend + v->member_bend : ch->bend);
    if (m->zone_mask && c != m->master) {
        ch->latest = (int32_t)k; ch->latest_order = v->order; m->st.mpe_notes++;
    }
    ctl_on(m, t, c, k, out, &n);
#if FMS_MIDI_MUTANT == 1
    emit(m, out, &n, t, FMS_EV_ON, k, v->freq, vel * 258u);
#else
    emit(m, out, &n, t, FMS_EV_ON, k, v->freq, vel * 32767u / 127u);
#endif
    if (m->cfg.cutoff_millihz && !m->zone_mask) {        /* a note-on resets the voice to its patch's cutoff */
        const uint32_t cm = cutoff_of(m, ch->cc74);
        v->cutoff = m->cfg.cutoff_millihz;
        if (cm != v->cutoff) {
            v->cutoff = cm;
            emit(m, out, &n, t, FMS_EV_FILTER, k, cm, m->cfg.q_milli);
            m->st.filter_events++;
        }
    }
    return n;
}

static uint32_t note_off(fms_midi_t *m, uint64_t t, uint32_t c, uint32_t key, uint32_t up, fms_event_t *out)
{
    uint32_t i, n = 0, first = NOTES;
    m->st.note_offs++;
#if FMS_MIDI_MUTANT == 2
    if (m->st.note_offs == 1) return 0;
#endif
    for (i = 0; i < NOTES; i++) {                        /* the oldest note of its key still down */
        const mnote_t *e = &m->note[i];
        if (e->used && !e->kept && e->chan == c && e->key == key && (first == NOTES || e->seq < m->note[first].seq))
            first = i;
    }
    if (first == NOTES) { m->st.orphan_offs++; return 0; }
    m->note[first].up = (uint8_t)up; m->note[first].has_up = 1;
    if (m->ch[m->zone_mask ? m->master : c].pedal) {
        m->note[first].kept = 1;
        m->st.pedal_held++;
        return 0;
    }
    end_note(m, first, t, out, &n);
    return n;
}

static int32_t mpe_latest(const fms_midi_t *m, uint32_t c)
{
    const int32_t k = m->ch[c].latest;
    return k >= 0 && m->v[k].used && !m->v[k].ext && m->v[k].chan == c &&
           m->v[k].order == m->ch[c].latest_order ? k : -1;
}
static uint32_t mpe_expression(fms_midi_t *m, uint64_t t, uint32_t c, uint32_t input,
                                uint32_t value, fms_event_t *out)
{
    uint32_t n = 0;
    const int32_t k = mpe_latest(m, c), q = ctl_q7(value);
    m->ch[c].ctl[input] = q;
    if (input == 4) m->st.mpe_slides++; else m->st.mpe_pressure++;
    /* : existing converter reaches the latest releasing note too.
     *  holds any change; parity fixtures exclude post-key-up expression. */
    if (k >= 0 && sounding(m, (uint32_t)k, m->master, t)) ctl_write(m, (uint32_t)k, input, q, t, out, &n);
    return n;
}

static uint32_t controller(fms_midi_t *m, uint64_t t, uint32_t c, uint32_t a, uint32_t b, fms_event_t *out)
{
    mchan_t *ch = &m->ch[c];
    uint32_t i, k, n = 0;
    if (a == 64) {
        mchan_t *pedal = &m->ch[m->zone_mask ? m->master : c];
        if (b >= 64 && !pedal->pedal) m->st.pedal_downs++;
        else if (b < 64 && pedal->pedal) {
            for (i = 0; i < NOTES; i++)
                if (m->note[i].used && m->note[i].kept &&
                    (m->zone_mask || m->note[i].chan == c)) end_note(m, i, t, out, &n);
        }
        pedal->pedal = b >= 64;
        return n;
    }
    if (a == 74 && m->zone_mask && c != m->master) return mpe_expression(m, t, c, 4, b, out);
    if (a == 74 && m->cfg.cutoff_millihz && !m->zone_mask) {
        const uint32_t cm = cutoff_of(m, b);
        ch->cc74 = (uint8_t)b;
        m->st.cc74++;
        for (k = 0; k < m->cfg.nvoices; k++) {
            if (!sounding(m, k, c, t) || cm == m->v[k].cutoff) continue;
            m->v[k].cutoff = cm;
            emit(m, out, &n, t, FMS_EV_FILTER, k, cm, m->cfg.q_milli);
            m->st.filter_events++;
        }
        return n;
    }
    if ((m->cfg.rpn || m->zone_mask) && (a == 101 || a == 100)) {
        if (!ch->sel) { ch->msb = 127; ch->lsb = 127; ch->sel = 1; }
        if (a == 101) ch->msb = (uint8_t)b; else ch->lsb = (uint8_t)b;
        m->st.rpn_selects++;
        (void)ctl_cc(m, t, c, a, b, out, &n);
        return 0;
    }
    if ((m->cfg.rpn || m->zone_mask) && (a == 99 || a == 98)) ch->sel = 0;
    if (m->zone_mask && (a == 6 || a == 38) && ch->sel && !ch->msb && ch->lsb == 6) {
        m->st.mcm++; return 0;
    }
    if ((m->cfg.rpn || m->zone_mask) && (a == 6 || a == 38) && ch->sel && !ch->msb && !ch->lsb) {
        if (a == 6) ch->range = b * 100u;
        else ch->range = ch->range / 100u * 100u + (b < 99 ? b : 99);
        if (m->zone_mask && c != m->master)
            for (i = 0; i < 16; i++)
                if (i != m->master && (m->zone_mask & (1u << i))) m->ch[i].range = ch->range;
        m->st.rpn_ranges++;
        return 0;
    }
    if (ctl_cc(m, t, c, a, b, out, &n)) return n;
    m->st.other_ccs++;
    return 0;
}

static uint32_t bend(fms_midi_t *m, uint64_t t, uint32_t c, uint32_t value, fms_event_t *out)
{
    mchan_t *ch = &m->ch[c];
    uint32_t k, n = 0;
    m->st.bends++;
    ch->bend = ((int64_t)value - 8192) * (int64_t)ch->range;
    for (k = 0; k < m->cfg.nvoices; k++) {
        uint32_t f;
        int64_t semis = ch->bend;
        if (m->zone_mask) {
            if (c != m->master) {
                if ((int32_t)k != mpe_latest(m, c)) continue;
                m->v[k].member_bend = ch->bend;
            }
            if (!sounding(m, k, m->master, t)) continue;
            semis = m->ch[m->master].bend + m->v[k].member_bend;
        } else if (!sounding(m, k, c, t)) continue;
        f = pitch_of(m, m->v[k].key, semis);
        if (f != m->v[k].freq) {
            m->v[k].freq = f;
            emit(m, out, &n, t, FMS_EV_PITCH, k, f, 0);
            m->st.pitch_events++;
        }
    }
    return n;
}

static uint32_t channel_message(fms_midi_t *m, uint64_t t, uint32_t status, uint32_t a, uint32_t b, fms_event_t *out)
{
    const uint32_t c = status & 0x0Fu;
    if (!((m->zone_mask ? m->zone_mask : m->cfg.channels) >> c & 1u)) { m->st.other_channels++; return 0; }
    m->seq++;
    switch (status & 0xF0u) {
    case 0x90: if (b) return note_on(m, t, c, a, b, out);
               return note_off(m, t, c, a, 64, out);
    case 0x80: return note_off(m, t, c, a, b, out);
    case 0xB0: return controller(m, t, c, a, b, out);
    case 0xE0: return bend(m, t, c, a | b << 7, out);
    case 0xC0: m->st.programs++; return 0;
    case 0xA0: {
        mctl_t *src = ctl_find(&m->map[c], CT_KEY(CT_POLY, 0));
        if (src) return ctl_message(m, t, c, src, ctl_q7(b), (int)a, out);
        m->st.aftertouch++; return 0;
    }
    default: {
        mctl_t *src;
        if (m->zone_mask && c != m->master) return mpe_expression(m, t, c, 1, a, out);
        src = ctl_find(&m->map[c], CT_KEY(CT_PRESSURE, 0));
        if (src) return ctl_message(m, t, c, src, ctl_q7(a), -1, out);
        m->st.pressure++; return 0;
    }
    }
}

uint32_t fms_midi_ump(fms_midi_t *m, uint64_t t_ns, const uint32_t *w, uint32_t nw,
                      fms_event_t *out, uint32_t cap, uint32_t *used)
{
    static const uint8_t WORDS[16] = { 1, 1, 1, 2, 2, 4, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4 };
    uint32_t mt, status;
    if (used) *used = nw ? 1 : 0;
    if (!m || !w || !nw) return 0;
    mt = w[0] >> 28;
    if (used) *used = WORDS[mt] <= nw ? WORDS[mt] : nw;
    m->started = 1;
    m->st.messages++;
    if (WORDS[mt] > nw || !out || cap < ((m->mapped || m->zone_mask) && m->cfg.ctl_reads ? 2 * m->cfg.nvoices + 10 : m->cfg.nvoices + 2)) { m->st.refused++; return 0; }
    if (t_ns < m->last_t) t_ns = m->last_t;              /* one clock, never backwards */
    m->last_t = t_ns;
    if (mt == 0 || mt == 1 || mt == 3) { m->st.system++; return 0; }   /* utility, system, SysEx */
    if (mt != 2) { m->st.unknown++; return 0; }
    status = (w[0] >> 16) & 0xFFu;
    if (status < 0x80 || status >= 0xF0) { m->st.unknown++; return 0; }
    return channel_message(m, t_ns, status, (w[0] >> 8) & 0x7Fu, w[0] & 0x7Fu, out);
}

void fms_midi_external(fms_midi_t *m, uint64_t t_ns, uint32_t voice, int on)
{
    mvoice_t *v;
    if (!m || voice >= m->cfg.nvoices) return;
    m->started = 1;
    if (t_ns < m->last_t) t_ns = m->last_t;
    m->last_t = t_ns;
    v = &m->v[voice];
    if (on) {
        if (v->used && v->off == NONE && v->note >= 0) m->note[v->note].voice = -1;   /* its note now has no voice */
        m->seq++;
        v->used = 1; v->ext = 1; v->note = -1;
        v->on = t_ns; v->order = m->seq; v->off = NONE;
    } else if (v->ext && v->off == NONE) {
        v->off = t_ns;
    }
}

void fms_midi_reset(fms_midi_t *m)
{
    uint32_t c;
    if (!m) return;
    memset(&m->st, 0, sizeof m->st);
    memset(m->note, 0, sizeof m->note);
    memset(m->v, 0, sizeof m->v);
    memset(m->ch, 0, sizeof m->ch);
    m->last_t = 0;
    m->seq = 0;
    m->started = 0;
    for (c = 0; c < 16; c++) {
        m->ch[c].range = m->cfg.bend_cents;
        m->ch[c].latest = -1;
        if (m->zone_mask && c != m->master && (m->zone_mask & (1u << c))) {
#if FMS_MIDI_MUTANT == 4
            m->ch[c].range = 4700;
#else
            m->ch[c].range = 4800;
#endif
        }
        m->ch[c].msb = 127; m->ch[c].lsb = 127; m->ch[c].sel = 1;
        size_t i;
        m->ch[c].cc74 = 64;
        m->map[c].rng = m->map[c].seed;
        for (i = 0; i < m->map[c].count; i++) m->map[c].src[i].msb = m->map[c].src[i].lsb = 0;
    }
}

fms_midi_t *fms_midi_create(const fms_midi_config_t *cfg)
{
    fms_midi_t *m;
    if (!cfg || !cfg->nvoices || cfg->nvoices > FMS_MAX_VOICES || cfg->bend_cents > 12799 ||
        cfg->q_milli > UINT16_MAX || cfg->ctl_reads > 255u)
        return NULL;
    m = (fms_midi_t *)calloc(1, sizeof *m);
    if (!m) return NULL;
    m->cfg = *cfg;
    m->rel_ns = (uint64_t)cfg->release_ms * 1000000u;
    fms_midi_reset(m);
    return m;
}

void fms_midi_destroy(fms_midi_t *m)
{
    uint32_t c;
    if (!m) return;
    for (c = 0; c < 16; c++) free(m->map[c].src);
    free(m);
}

void fms_midi_stats(const fms_midi_t *m, fms_midi_stats_t *out)
{
    if (m && out) *out = m->st;
}
