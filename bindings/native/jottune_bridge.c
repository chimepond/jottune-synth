/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * jottune_bridge.c: the bridge of jottune_bridge.h. It includes src/host/harness.c
 * (main renamed), so records are read by tl_read and
 * rendered by tl_apply and tl_mix, the harness's own functions. Host side
 * only: the library objects are linked unmodified.
 */
#if !defined(__APPLE__) && !defined(_POSIX_C_SOURCE)
#define _POSIX_C_SOURCE 200809L
#endif
#include <stdio.h>
#include <stdlib.h>
#include <signal.h>
/* Darwin x86 machine headers expose an unrelated FP_RND_NEAR macro. The
 * arithmetic header is vendored unchanged; isolate that SDK name here. */
#ifdef FP_RND_NEAR
#undef FP_RND_NEAR
#endif
#include "jottune_bridge.h"

/* Interface/host state only; the integer library has no mutable globals.
 * Each worker routes diagnostics/cancellation to its own active operation.
 * GCC/Clang TLS and atomics are available on all supported phone toolchains. */
struct fms_cancel { unsigned requested; };
static __thread FILE *fms_diagnostics;
static __thread const fms_cancel_t *fms_operation_cancel;
static FILE *fms_system_stderr(void) { return stderr; }
static FILE *fms_diagnostic_stream(void)
{
    return fms_diagnostics ? fms_diagnostics : fms_system_stderr();
}
static int fms_cancelled(const fms_cancel_t *token)
{
    return token && __atomic_load_n(&token->requested, __ATOMIC_ACQUIRE);
}
#undef stderr
#define stderr fms_diagnostic_stream()
#define FM_HOST_MEMORY_INPUT 1
#define FM_HOST_CANCELLABLE 1
#define FM_HOST_CANCELLED() fms_cancelled(fms_operation_cancel)
#define main __attribute__((visibility("hidden"))) fms_embedded_cli_main
#include "harness.c"
#undef main

#include <unistd.h>

fms_cancel_t *fms_cancel_create(void) { return (fms_cancel_t *)calloc(1, sizeof(fms_cancel_t)); }
void fms_cancel_request(fms_cancel_t *token)
{
    if (token) __atomic_store_n(&token->requested, 1u, __ATOMIC_RELEASE);
}
void fms_cancel_close(fms_cancel_t *token) { free(token); }

/* C99 ABI guards: the bridge header and the included harness build together. */
typedef char fms_chk_voices[FMS_MAX_VOICES == TL_MAX_VOICES ? 1 : -1];
typedef char fms_chk_name[FMS_NAME == TL_NAME ? 1 : -1];
typedef char fms_chk_ops[FMS_OPS == FM_OPS ? 1 : -1];
typedef char fms_chk_channels[FMS_MAX_CH >= SP_FIELD_MAX_CH ? 1 : -1];
typedef char fms_chk_engine[sizeof(((fms_info_t *)0)->engine) >= 160 ? 1 : -1];

#define FNV_BASIS 14695981039346656037ULL
#define FNV_PRIME 1099511628211ULL

/* ---- small helpers ---- */

static uint64_t fms_fnv(uint64_t h, const int16_t *p, size_t n)
{
    size_t i;
    for (i = 0; i < n; i++) {   /* fm_golden_hash's order: the low byte, then the high byte */
        uint16_t u = (uint16_t)p[i];
        h ^= u & 0xFFu;        h *= FNV_PRIME;
        h ^= (u >> 8) & 0xFFu; h *= FNV_PRIME;
    }
    return h;
}

static void fms_copy_err(char *err, size_t errlen, const char *msg)
{
    if (err && errlen) { snprintf(err, errlen, "%s", msg); }
}

/* An operation-local memory stream, never a process descriptor redirect.
 * Nested bridge operations restore the enclosing worker's diagnostic scope. */
typedef struct { FILE *stream, *previous; char *text; size_t size; } fms_cap_t;
static void fms_cap_begin(fms_cap_t *c)
{
    memset(c, 0, sizeof *c);
    c->previous = fms_diagnostics;
    c->stream = open_memstream(&c->text, &c->size);
    fms_diagnostics = c->stream;
}
static void fms_cap_end(fms_cap_t *c, char *err, size_t errlen, const char *hide, const char *label)
{
    (void)hide; (void)label;
    if (c->stream) fclose(c->stream);
    fms_diagnostics = c->previous;
    if (c->text) {
        size_t n = c->size;
        while (n && (c->text[n - 1] == '\n' || c->text[n - 1] == ' ')) c->text[--n] = 0;
        fms_copy_err(err, errlen, c->text);
    }
    free(c->text);
}

/* ---- players ---- */

struct fms_player {
    tl_t        *tl;
    fm_voice_t  *vs;
    sp_stereo_t *sp;
    fx_t        *fx;
    uint32_t     ch, pos, e, clipped;
    uint64_t     h, positioned;    /* profile: applied/reserved initial POS per voice */
    int          live;
    int          export_ready; /* complete successful checked render only */
    tl_gen_runtime_t *gen;         /* allocated at load; only gen_clock records */
    uint64_t     next_tick;
    uint32_t     render_error;     /* code | id<<8 | field<<16; no callback I/O */
    int32_t      vpos[TL_MAX_VOICES][3];
    uint32_t     seed_count, published;
    uint32_t     crush_reset_head, crush_reset_pending; /* setup before first publication, then callback-owned */
    tl_event_t   seed[FMS_MAX_VOICES + 1]; /* immutable after unpublished candidate setup */
};

static uint32_t fms_gen_error(gen_status_t s)
{
    return (uint32_t)s.code | ((uint32_t)s.id << 8) | ((uint32_t)s.field << 16);
}

static void fms_apply(fms_player_t *p, const tl_event_t *e);

/* tl_render's init, in its order; 0 ok. Generator expansion is load/rewind
 * work on the caller's non-audio thread; gen_clock live headers are refused. */
static int fms_init_state(fms_player_t *p)
{
    const tl_t *tl = p->tl;
    uint32_t i;
    p->render_error = 0;
    p->export_ready = 0;
    for (i = 0; i < tl->nvoices; i++) (void)tl_voice_init(tl, &p->vs[i], i);   /* : the header check bound the banks and buffers once */
    /* Field initialization clears its attachment. Match tl_render's order on
     * every initial open and rewind; field FX uses stereo return storage even
     * when the final output has four or sixteen channels. */
    if (p->sp && (tl->route ? tl_field_init(tl, p->sp) : sp_stereo_init(p->sp, tl->block, tl->near))) {
        p->render_error = 1; return 1;
    }
    if (p->fx && tl_fx_init(tl, p->fx)) { p->render_error = 1; return 1; }
    if (p->sp && p->fx) {
        if (tl->route) {
            if (sp_field_attach_fx((sp_field_t *)p->sp, p->fx)) { p->render_error = 1; return 1; }
        } else p->sp->fx = p->fx;
    }
    p->next_tick = 0;
    if (p->gen) {
        p->render_error = fms_gen_error(tl_gen_init_status(tl, p->gen));
        if (p->render_error) return 1;
    }
    p->pos = p->e = p->clipped = 0;
    p->h = FNV_BASIS;
    memset(p->vpos, 0, sizeof p->vpos);
    p->positioned = 0;
    if (tl->decodable) for (i = 0; i < tl->nev; i++)
        if (tl->ev[i].type == EV_POS) p->positioned |= UINT64_C(1) << tl->ev[i].voice;
    for (i = 0; i < p->seed_count; i++) fms_apply(p, &p->seed[i]);
    return 0;
}

void fms_player_close(fms_player_t *p)
{
    if (!p) return;
    if (p->tl) { free(p->tl->ev); free(p->tl->stems); free(p->tl->ks_pool); }
    free(p->tl); free(p->vs); free(p->sp); free(p->fx); free(p->gen);
    free(p);
}

/* play's allocation after the reading; 0 ok, else a message on stderr. */
static int fms_alloc(fms_player_t *p)
{
    tl_t *tl = p->tl;
    p->ch = 1;
    if (tl->nev) qsort(tl->ev, tl->nev, sizeof *tl->ev, tl_cmp);
    if (tl->route) { p->ch = tl_field_ch(tl); p->sp = (sp_stereo_t *)calloc(1, sizeof(sp_field_t)); }
    else if (tl->space) { p->ch = 2; p->sp = (sp_stereo_t *)calloc(1, sizeof *p->sp); }
    if (tl->fx.on) {
        const size_t bytes = tl_fx_mem_bytes(tl);
        if (bytes > SIZE_MAX - sizeof *p->fx) return tl_err(NULL, 0, "the effects' storage size is refused");
        p->fx = (fx_t *)calloc(1, sizeof *p->fx + bytes);
    }
    if (tl->gen_clock) p->gen = (tl_gen_runtime_t *)calloc(1, sizeof *p->gen);
    p->vs = (fm_voice_t *)alloc_n(tl->nvoices, sizeof *p->vs);
    if (!p->vs || (p->ch > 1 && !p->sp) || (tl->fx.on && !p->fx) || (tl->gen_clock && !p->gen)) { return tl_host_error(NULL, 0, "out of memory"); }
    if (fms_init_state(p)) return tl_err(NULL, 0, "the player configuration is refused");
    return 0;
}

fms_player_t *fms_player_open(const char *const *paths, int npaths, char *err, size_t errlen)
{
    fms_player_t *p;
    fms_cap_t cap;
    int k, rc = 0;
    if (err && errlen) err[0] = 0;
    if (npaths < 1 || npaths > TL_MAX_FILES) { fms_copy_err(err, errlen, "timeline: 1 to 17 files"); return NULL; }
    p = (fms_player_t *)calloc(1, sizeof *p);
    if (p) p->tl = (tl_t *)calloc(1, sizeof *p->tl);
    if (!p || !p->tl) { fms_player_close(p); fms_copy_err(err, errlen, "oom"); return NULL; }
    fms_cap_begin(&cap);
    for (k = 0; k < npaths && !rc; k++) rc = tl_read(p->tl, paths[k], 1);
    if (!rc) rc = tl_check_header(p->tl);
    for (k = 0; k < npaths && !rc; k++) rc = tl_read(p->tl, paths[k], 2);
    if (!rc) rc = tl_check_decodable(p->tl);
    if (!rc) rc = fms_alloc(p);
    fms_cap_end(&cap, err, errlen, NULL, NULL);
    if (rc) {
        if (err && errlen && !err[0]) fms_copy_err(err, errlen, "timeline refused");
        fms_player_close(p);
        return NULL;
    }
    return p;
}

int fms_player_import(const char *text, size_t n, const fms_cancel_t *cancel,
                      fms_player_t **out, char *err, size_t cap)
{
    fms_player_t *p;
    fms_cap_t capture;
    const fms_cancel_t *previous = fms_operation_cancel;
    int rc;
    if (err && cap) err[0] = 0;
    if (out) *out = NULL;
    if (!out || !text || !n) { fms_copy_err(err, cap, "record needs nonempty TL bytes and an output slot"); return FMS_INVALID; }
    if (fms_cancelled(cancel)) { fms_copy_err(err, cap, "cancelled"); return FMS_CANCELLED; }
    p = (fms_player_t *)calloc(1, sizeof *p);
    if (p) p->tl = (tl_t *)calloc(1, sizeof *p->tl);
    if (!p || !p->tl) { fms_player_close(p); fms_copy_err(err, cap, "out of memory"); return FMS_NO_MEMORY; }
    fms_operation_cancel = cancel;
    fms_cap_begin(&capture);
    rc = tl_read_memory(p->tl, text, n, "record", 1);
    if (!rc) rc = tl_check_header(p->tl);
    if (!rc) rc = tl_read_memory(p->tl, text, n, "record", 2);
    if (!rc) rc = tl_check_decodable(p->tl);
    if (!rc && fms_cancelled(cancel)) rc = FMS_CANCELLED;
    if (!rc) rc = fms_alloc(p);
    if (fms_cancelled(cancel)) rc = FMS_CANCELLED;
    fms_cap_end(&capture, err, cap, NULL, NULL);
    fms_operation_cancel = previous;
    if (rc) {
        if (rc == FMS_CANCELLED) fms_copy_err(err, cap, "cancelled");
        else if (!err || !cap || !*err) fms_copy_err(err, cap, "timeline refused");
        fms_player_close(p);
        return rc;
    }
    *out = p;
    return FMS_OK;
}

fms_player_t *fms_player_open_text(const char *text, size_t n, char *err, size_t cap)
{
    fms_player_t *p = NULL;
    (void)fms_player_import(text, n, NULL, &p, err, cap);
    return p;
}

/* Is there a line whose first word is word? */
static int fms_has_statement(const char *text, const char *word)
{
    size_t wl = strlen(word);
    const char *s = text;
    while (*s) {
        while (*s == ' ' || *s == '\t') s++;
        if (!strncmp(s, word, wl) && (s[wl] == ' ' || s[wl] == '\t')) return 1;
        s = strchr(s, '\n');
        if (!s) break;
        s++;
    }
    return 0;
}

fms_player_t *fms_player_open_live(const char *header_text, char *err, size_t errlen)
{
    fms_player_t *p;
    fms_cap_t cap;
    char eng[160], *text = NULL;
    size_t n = 0;
    FILE *f;
    int rc = 0;
    if (err && errlen) err[0] = 0;
    if (!header_text) { fms_copy_err(err, errlen, "live: no header text"); return NULL; }
    f = open_memstream(&text, &n);
    if (!f) { fms_copy_err(err, errlen, "live: cannot allocate header"); return NULL; }
    tl_engine(eng, sizeof eng);
    if (!fms_has_statement(header_text, "engine")) fprintf(f, "engine %s\n", eng);
    fputs(header_text, f);
    fputc('\n', f);
    if (fclose(f)) { free(text); fms_copy_err(err, errlen, "live: cannot allocate header"); return NULL; }
    p = (fms_player_t *)calloc(1, sizeof *p);
    if (p) p->tl = (tl_t *)calloc(1, sizeof *p->tl);
    if (!p || !p->tl) { free(text); fms_player_close(p); fms_copy_err(err, errlen, "oom"); return NULL; }
    p->live = 1;
    fms_cap_begin(&cap);
    rc = tl_read_memory(p->tl, text, n, "live header", 1);
    if (!rc && p->tl->gen_clock)
        rc = tl_err(NULL, 0, "live generator banks are not supported; open the timeline as a record");
    if (!rc && !p->tl->length_ns && p->tl->fs) {
        /* the longest render the header allows: 2^31 - 1 samples over the channels */
        const tl_t *tl = p->tl;
        uint64_t ch = tl->route ? tl_field_ch(tl) : tl->space ? 2u : 1u;
        uint64_t limit = (uint64_t)INT32_MAX / ch;
        p->tl->length_ns = limit * 1000000000u / tl->fs;
    }
    if (!rc) rc = tl_check_header(p->tl);
    if (!rc && (p->tl->route == SP_FIELD_AMB1 || p->tl->route == SP_FIELD_AMB3))
        rc = tl_err(NULL, 0, "ambisonic records play in Records; live space accepts off, stereo or binaural");
    if (!rc) rc = tl_read_memory(p->tl, text, n, "live header", 2);
    if (!rc) rc = tl_check_decodable(p->tl);
    if (!rc && p->tl->decodable && p->tl->nev)
        rc = tl_err(NULL, 0, "decodable live header must be event-free; open complete timelines in Records");
    if (!rc) rc = fms_alloc(p);
    fms_cap_end(&cap, err, errlen, NULL, NULL);
    free(text);
    if (rc) {
        if (err && errlen && !err[0]) fms_copy_err(err, errlen, "live header refused");
        fms_player_close(p);
        return NULL;
    }
    return p;
}

/* tl_write into a string: the whole record of tl with hash. NULL on failure. */
static char *fms_write_text(const tl_t *tl, uint64_t hash, size_t *len)
{
    char *txt = NULL;
    size_t n = 0;
    FILE *f = open_memstream(&txt, &n);
    int rc;
    if (!f) return NULL;
    rc = tl_write_f(f, tl, hash);
    if (fclose(f) || rc) { free(txt); return NULL; }
    if (len) *len = n;
    return txt;
}

/* Copies the lines of txt whose first word is in keep (a NULL-ended list) to
 * out (when cap allows); returns the bytes needed with the NUL. */
static size_t fms_keep_lines(const char *txt, const char *const *keep, int keep_all_but, char *out, size_t cap)
{
    size_t need = 0;
    const char *s = txt;
    while (*s) {
        const char *e = strchr(s, '\n');
        size_t n = e ? (size_t)(e - s) + 1 : strlen(s);
        int k, hit = 0;
        for (k = 0; keep[k]; k++) {
            size_t wl = strlen(keep[k]);
            if (n > wl && !strncmp(s, keep[k], wl) && (s[wl] == ' ' || s[wl] == '\n' || s[wl] == 0)) hit = 1;
        }
        if (hit != keep_all_but) {
            if (out && need + n < cap) memcpy(out + need, s, n);
            need += n;
        }
        s += n;
    }
    if (out && need < cap) out[need] = 0;
    return need + 1;
}

/* Accepted patch lines are already the model. Only replace the patch-name
 * token; bank IDs/names and generator IDs are independent. No grammar or
 * range checks live here: the edit transaction calls the harness validator. */
static char *fms_named_lines(const char *lines, const char *name, size_t *len)
{
    static const char *const context[] = { "gen_clock", "gen", "gen_axiom", "gen_rule", "wtbank", "wtdata", "wthash", "harmonic", NULL };
    char *text = NULL;
    size_t n = 0;
    FILE *f;
    const char *s;
    if (!lines || !name || !tl_name_ok(name)) return NULL;
    f = open_memstream(&text, &n);
    if (!f) return NULL;
    for (s = lines; *s;) {
        const char *e = strchr(s, '\n');
        const size_t count = e ? (size_t)(e - s) : strlen(s);
        size_t word = strcspn(s, " \t\n"), first, last;
        int i, bank = 0;
        for (i = 0; context[i]; i++)
            if (strlen(context[i]) == word && !memcmp(s, context[i], word)) bank = 1;
        first = word;
        while (first < count && (s[first] == ' ' || s[first] == '\t')) first++;
        last = first;
        while (last < count && s[last] != ' ' && s[last] != '\t') last++;
        if (!bank && word && first < last && s[0] != '#') {
            fwrite(s, 1, first, f); fputs(name, f); fwrite(s + last, 1, count - last, f);
        } else fwrite(s, 1, count, f);
        fputc('\n', f);
        s += count + (e ? 1 : 0);
    }
    if (fclose(f)) { free(text); return NULL; }
    if (len) *len = n;
    return text;
}

size_t fms_patch_text(const char *lines, const char *name, char *out, size_t cap)
{
    size_t n;
    char *txt = fms_named_lines(lines, name, &n);
    if (!txt) return 0;
    if (out && cap > n) memcpy(out, txt, n + 1);
    free(txt);
    return n + 1;
}

void fms_space_defaults(fms_space_t *out)
{
    if (!out) return;
    memset(out, 0, sizeof *out);
    out->block = 64; out->near_mm = 100; out->order = 3;
}

void fms_player_space(const fms_player_t *p, fms_space_t *out)
{
    const tl_t *tl;
    if (!out) return;
    fms_space_defaults(out);
    if (!p) return;
    tl = p->tl;
    out->field = tl->space ? FMS_SPACE_STEREO : tl->route == SP_FIELD_BINAURAL ? FMS_SPACE_BINAURAL :
                 tl->route == SP_FIELD_AMB1 ? FMS_SPACE_AMB1 : tl->route == SP_FIELD_AMB3 ? FMS_SPACE_AMB3 : FMS_SPACE_OFF;
    out->block = tl->block; out->near_mm = tl->near;
    out->order = tl->route == SP_FIELD_BINAURAL ? tl->order : 3;
    out->panlaw_q15 = tl->panlaw; out->pinna = (uint32_t)tl->pinna;
    memcpy(out->pinna_d, tl->pinna_d, sizeof out->pinna_d);
    out->yaw_mdeg = tl->yaw;
    out->present = (tl->have_order ? FMS_SPACE_ORDER : 0u) | (tl->have_panlaw ? FMS_SPACE_PANLAW : 0u) |
                   (tl->have_pinna ? FMS_SPACE_PINNA : 0u) | (tl->have_yaw ? FMS_SPACE_YAW : 0u);
}

size_t fms_player_fx_text(const fms_player_t *p, char *out, size_t cap)
{
    /* Reuse the sole canonical writer. Its global header ends at streams;
     * exclude spatial/session rows, keeping every FX family without an
     * independent effect whitelist. A private shallow copy omits events. */
    static const char *const drop[] = { "timeline", "engine", "fs", "gen_clock", "length_ns",
        "space", "block", "near", "order", "panlaw", "pinna", "yaw", NULL };
    tl_t *header;
    char *text, *end;
    size_t need;
    if (!p) return 0;
    header = (tl_t *)malloc(sizeof *header);
    if (!header) return 0;
    *header = *p->tl; header->nev = 0;
    text = fms_write_text(header, 0, NULL);
    free(header);
    if (!text) return 0;
    end = strstr(text, "\nstreams ");
    if (!end) { free(text); return 0; }
    end[1] = 0;
    need = fms_keep_lines(text, drop, 1, NULL, 0);
    if (out && cap >= need) (void)fms_keep_lines(text, drop, 1, out, cap);
    free(text);
    return need;
}

size_t fms_live_header_text(const char *lines, const char *name, uint32_t nvoices, uint32_t fs,
                            const fms_space_t *space, const char *extra, char *out, size_t cap)
{
    static const char *const fields[] = { "off", "stereo", "binaural", "amb1", "amb3" };
    static const char *const pinna[] = { "off", "pbnh", "rd" };
    fms_space_t defaults;
    char eng[160], *patch, *txt = NULL;
    size_t n = 0;
    uint32_t i;
    FILE *f;
    if (!space) { fms_space_defaults(&defaults); space = &defaults; }
    if (!lines || !name || !tl_name_ok(name) || nvoices < 1 || nvoices > FMS_MAX_VOICES || !fs ||
        space->field > FMS_SPACE_AMB3 || (space->present & ~15u) ||
        ((space->present & FMS_SPACE_PINNA) && space->pinna > 3)) return 0;
    patch = fms_named_lines(lines, name, NULL);
    if (!patch) return 0;
    f = open_memstream(&txt, &n);
    if (!f) { free(patch); return 0; }
    tl_engine(eng, sizeof eng);
    fprintf(f, "timeline draft2\nengine %s\nfs %u\nspace %s\nblock %u\nnear %u\n", eng, fs,
            fields[space->field], space->block, space->near_mm);
    if (space->present & FMS_SPACE_ORDER) fprintf(f, "order %u\n", space->order);
    if (space->present & FMS_SPACE_PINNA) {
        if (space->pinna == 3) fprintf(f, "pinna %u %u %u %u %u\n", space->pinna_d[0], space->pinna_d[1],
                                     space->pinna_d[2], space->pinna_d[3], space->pinna_d[4]);
        else fprintf(f, "pinna %s\n", pinna[space->pinna]);
    }
    if (space->present & FMS_SPACE_YAW) fprintf(f, "yaw %d\n", (int)space->yaw_mdeg);
    if (space->present & FMS_SPACE_PANLAW) fprintf(f, "panlaw %u\n", space->panlaw_q15);
    fputs("streams live\n", f);
    fputs(patch, f);
    for (i = 0; i < nvoices; i++) fprintf(f, "voice %u %s\n", i, name);
    if (extra && *extra) { fputs(extra, f); if (extra[strlen(extra) - 1] != '\n') fputc('\n', f); }
    free(patch);
    if (fclose(f)) { free(txt); return 0; }
    if (out && cap > n) memcpy(out, txt, n + 1);
    free(txt);
    return n + 1;
}

void fms_player_info(const fms_player_t *p, fms_info_t *out)
{
    const tl_t *tl;
    memset(out, 0, sizeof *out);
    if (!p) return;
    tl = p->tl;
    out->fs = tl->fs;
    out->channels = p->ch;
    out->frames = tl->n;
    out->length_ns = tl->length_ns;
    out->nvoices = tl->nvoices;
    out->npatches = tl->npatches;
    out->nstreams = tl->nstreams;
    out->nevents = tl->nev;
    out->draft = tl->draft;
    out->space = tl->space;
    out->route = tl->route;
    out->order = tl->route == SP_FIELD_BINAURAL ? tl->order : 0;
    out->block = (tl->space || tl->route) ? tl->block : 0;
    out->near_mm = (tl->space || tl->route) ? tl->near : 0;
    out->yaw_mdeg = tl->yaw;
    out->fx_on = tl->fx.on != 0;
    out->have_hash = tl->have_hash;
    out->hash_expected = tl->hash;
    tl_engine(out->engine, sizeof out->engine);
}

const char *fms_player_stream_name(const fms_player_t *p, uint32_t i)
{
    return p && i < p->tl->nstreams ? p->tl->stream[i] : NULL;
}

const char *fms_player_voice_patch(const fms_player_t *p, uint32_t voice)
{
    return p && voice < p->tl->nvoices ? p->tl->patch[p->tl->voice[voice]].name : NULL;
}

/* A one-patch record of tp for tl_write; the masks are the bits of its
 * statements kept in tl_t (bit 0: present). */
static tl_t *fms_one_patch(const tl_patch_t *tp, uint32_t routes, uint32_t filters, uint32_t shapes,
                           uint32_t lfos, uint32_t pegs, uint32_t fenvs)
{
    tl_t *tl = (tl_t *)calloc(1, sizeof *tl);
    if (!tl) return NULL;
    tl->patch[0] = *tp;
    tl->npatches = 1;
    tl->routes = routes; tl->filters = filters; tl->shapes = shapes;
    tl->lfos = lfos; tl->lfo_masks = lfos; tl->pegs = pegs; tl->fenvs = fenvs;
    tl->draft = 1;
    tl->fs = 48000;
    tl->length_ns = 1;
    return tl;
}

/* Keep the selected patch's complete canonical representation. The bank
 * definitions are context for its accepted wtuse/gen bindings, never events,
 * lanes, effects or spatial settings. All wavetable banks stay available to
 * the editor's record-bank picker; generator context is carried only when
 * this patch binds a generator. The temporary object borrows no ownership. */
size_t fms_player_patch_lines(const fms_player_t *p, uint32_t i, char *out, size_t cap)
{
    static const char *const drop[] = { "timeline", "engine", "fs", "length_ns", "streams", "hash", NULL };
    const tl_t *src;
    tl_t *one;
    char *txt;
    size_t need;
    if (!p || i >= p->tl->npatches) return 0;
    src = p->tl;
    one = fms_one_patch(&src->patch[i], (src->routes >> i) & 1u, (src->filters >> i) & 1u,
                        (src->shapes >> i) & 1u, (src->lfos >> i) & 1u,
                        (src->pegs >> i) & 1u, (src->fenvs >> i) & 1u);
    if (!one) return 0;
    one->amps = (src->amps >> i) & 1u;
    one->wt = src->wt;
    if (src->patch[i].gen_mods) {
        one->gen = src->gen;
        one->gen.lanes = 0;
        one->gen_clock = src->gen_clock;
        one->gen_sources = src->gen_sources;
        one->gen_axioms = src->gen_axioms;
    }
    txt = fms_write_text(one, 0, NULL);
    free(one);
    if (!txt) return 0;
    need = fms_keep_lines(txt, drop, 1, NULL, 0);
    if (out && cap >= need) (void)fms_keep_lines(txt, drop, 1, out, cap);
    free(txt);
    return need;
}

/* Compare complete parsed records, not a second list of allowed headers.
 * This also rejects extra generator/effect/spatial/event state that Save
 * would otherwise omit. Only the expected hash differs by construction. */
int fms_player_is_patch_file(const fms_player_t *p)
{
    char eng[160], err[256], *lines = NULL, *a = NULL, *b = NULL, *text = NULL;
    fms_player_t *expected = NULL;
    FILE *f;
    size_t n, len = 0;
    int same = 0;
    if (!p || p->tl->npatches != 1) return 0;
    n = fms_player_patch_lines(p, 0, NULL, 0);
    lines = (char *)malloc(n);
    if (!lines || fms_player_patch_lines(p, 0, lines, n) != n) goto done;
    f = open_memstream(&text, &len);
    if (!f) goto done;
    tl_engine(eng, sizeof eng);
    fprintf(f, "timeline draft2\nengine %s\nfs %u\nlength_ns 2500000000\nspace off\nstreams main\n%svoice 0 %s\n0 main 0 on 261626 26000\n1500000000 main 0 off\n",
            eng, p->tl->fs, lines, p->tl->patch[0].name);
    if (fclose(f)) goto done;
    expected = fms_player_open_text(text, len, err, sizeof err);
    if (!expected) goto done;
    a = fms_write_text(p->tl, 0, NULL);
    b = fms_write_text(expected->tl, 0, NULL);
    same = a && b && !strcmp(a, b);
done:
    free(text); free(lines); free(a); free(b); fms_player_close(expected);
    return same;
}

/* The harness parses the user's lines first, so its diagnostic line numbers
 * are the editor's. Then append the silent one-voice draft2 wrapper and read
 * the complete record with the same two-pass checks as an ordinary load.
 * Scope is checked from parsed state, not a second statement grammar. */
static tl_t *fms_patch_lines_read(const char *lines, size_t n, uint32_t fs, char *err, size_t cap)
{
    char eng[160], name[TL_NAME], *text = NULL;
    tl_t *tl = NULL;
    fms_cap_t capture;
    FILE *f;
    size_t len = 0;
    int rc;
    if (err && cap) err[0] = 0;
    if (!lines || !n || !fs || memchr(lines, 0, n)) {
        fms_copy_err(err, cap, "patch lines: nonempty text without embedded NUL and a positive sample rate required");
        return NULL;
    }
    tl = (tl_t *)calloc(1, sizeof *tl);
    if (!tl) { fms_copy_err(err, cap, "oom"); return NULL; }
    fms_cap_begin(&capture);
    rc = tl_read_memory(tl, lines, n, "patch lines", 1);
    if (!rc && (tl->npatches != 1 || tl->have_timeline || tl->have_engine || tl->have_hash ||
        tl->have_space || tl->fs || tl->length_ns || tl->nstreams || tl->nvoices || tl->block || tl->near ||
        tl->have_order || tl->have_pinna || tl->have_yaw || tl->have_panlaw || tl->fx.on || tl->decodable || tl->gen_lanes))
        rc = tl_err(NULL, 0, "patch lines need exactly one patch and its bank definitions, without record, spatial, effects or lane statements");
    if (!rc) {
        snprintf(name, sizeof name, "%s", tl->patch[0].name);
        f = open_memstream(&text, &len);
        if (!f) rc = tl_err(NULL, 0, "cannot complete patch validation record");
        else {
            tl_engine(eng, sizeof eng);
            fwrite(lines, 1, n, f);
            if (lines[n - 1] != '\n') fputc('\n', f);
            fprintf(f, "timeline draft2\nengine %s\nfs %u\nlength_ns 1000000000\nspace off\nstreams edit\nvoice 0 %s\n0 edit 0 on 261626 0\n", eng, fs, name);
            if (fclose(f)) rc = tl_err(NULL, 0, "cannot complete patch validation record");
        }
    }
    if (!rc) {
        free(tl->ev); free(tl->stems); free(tl->ks_pool);
        memset(tl, 0, sizeof *tl);
        rc = tl_read_memory(tl, text, len, "patch lines", 1);
        if (!rc) rc = tl_check_header(tl);
        if (!rc) rc = tl_read_memory(tl, text, len, "patch lines", 2);
        if (!rc) rc = tl_check_decodable(tl);
        if (!rc && tl->nev != 1) rc = tl_err(NULL, 0, "patch lines cannot contain events");
    }
    fms_cap_end(&capture, err, cap, NULL, NULL);
    free(text);
    if (rc) {
        free(tl->ev); free(tl->stems); free(tl->ks_pool); free(tl);
        return NULL;
    }
    return tl;
}

int fms_patch_lines_check(const char *lines, size_t n, uint32_t fs, char *err, size_t cap)
{
    tl_t *tl = fms_patch_lines_read(lines, n, fs, err, cap);
    if (!tl) return 1;
    free(tl->ev); free(tl->stems); free(tl->ks_pool); free(tl);
    return 0;
}

/* MIDI's existing longest-carrier release policy, from the same parsed
 * lines used by the live player. Setup only; never the audio callback. */
uint32_t fms_midi_release_ms(const char *lines, uint32_t fs)
{
    char err[FMS_ERR_LEN];
    tl_t *tl;
    uint32_t i, release = 0;
    const fm_patch_t *p;
    if (!lines) return 0;
    tl = fms_patch_lines_read(lines, strlen(lines), fs, err, sizeof err);
    if (!tl) return 0;
    p = &tl->patch[0].p;
    if (!p->route.ops) release = p->op[0].release_ms;
    else for (i = 0; i < FM_OPS; i++)
        if ((p->route.carriers >> i & 1u) && p->op[i].release_ms > release) release = p->op[i].release_ms;
    free(tl->ev); free(tl->stems); free(tl->ks_pool); free(tl);
    return release;
}

uint32_t fms_midi_ctl_reads(const char *lines, uint32_t fs)
{
    char err[FMS_ERR_LEN];
    tl_t *tl;
    fm_voice_t v;
    uint32_t i, mask = 0;
    if (!lines) return 0;
    tl = fms_patch_lines_read(lines, strlen(lines), fs, err, sizeof err);
    if (!tl) return 0;
    fm_voice_init(&v, &tl->patch[0].p, fs);
    for (i = 0; i < FM_MOD_SLOTS; i++) {
        uint32_t source = tl->patch[0].p.mod[i].source;
        if ((v.mod_active & (1u << i)) && source >= 16 && source < 16 + FM_CTL_INPUTS)
            mask |= 1u << (source - 16);
    }
    free(tl->ev); free(tl->stems); free(tl->ks_pool); free(tl);
    return mask;
}

/* 1 when txt has a whole line equal to the n bytes at line. */
static int fms_has_line(const char *txt, const char *line, size_t n)
{
    while (*txt) {
        const char *e = strchr(txt, '\n');
        size_t m = e ? (size_t)(e - txt) : strlen(txt);
        if (m == n && !memcmp(txt, line, n)) return 1;
        txt += m + (e ? 1 : 0);
    }
    return 0;
}

/* Adds the first word of every line of x that y lacks to the space-separated
 * list in found (cap bytes), once each; returns how many it added. */
static int fms_diff_words(const char *x, const char *y, char *found, size_t cap)
{
    int added = 0;
    while (*x) {
        const char *e = strchr(x, '\n');
        size_t n = e ? (size_t)(e - x) : strlen(x), w = strcspn(x, " \n");
        if (w > n) w = n;
        if (w && !fms_has_line(y, x, n)) {
            const char *f = found;
            int seen = 0;
            while (*f && !seen) {
                size_t fw = strcspn(f, " ");
                seen = fw == w && !memcmp(f, x, w);
                f += fw + (f[fw] == ' ');
            }
            if (!seen) {
                size_t fl = strlen(found);
                if (fl + w + 2 <= cap) {
                    if (fl) found[fl++] = ' ';
                    memcpy(found + fl, x, w);
                    found[fl + w] = 0;
                }
                added++;
            }
        }
        x += n + (e ? 1 : 0);
    }
    return added;
}

int fms_player_patch_dropped(const fms_player_t *p, uint32_t i, char *names, size_t cap)
{
    fms_player_t back;
    tl_t *parsed = NULL;
    char *ta = NULL, *tb = NULL, found[512], err[FMS_ERR_LEN];
    size_t na, nb;
    int count = -1;
    if (names && cap) names[0] = 0;
    if (!p || i >= p->tl->npatches) return -1;
    na = fms_player_patch_lines(p, i, NULL, 0);
    ta = na ? (char *)malloc(na) : NULL;
    if (!ta || fms_player_patch_lines(p, i, ta, na) != na) goto done;
    parsed = fms_patch_lines_read(ta, na - 1, p->tl->fs, err, sizeof err);
    if (!parsed) goto done;
    memset(&back, 0, sizeof back);
    back.tl = parsed;  /* metadata only: no voice initialization or audio */
    nb = fms_player_patch_lines(&back, 0, NULL, 0);
    tb = nb ? (char *)malloc(nb) : NULL;
    if (!tb || fms_player_patch_lines(&back, 0, tb, nb) != nb) goto done;
    found[0] = 0;
    count = fms_diff_words(ta, tb, found, sizeof found);
    count += fms_diff_words(tb, ta, found, sizeof found);
    if (names && cap) snprintf(names, cap, "%s", found);
 done:
    free(ta); free(tb);
    if (parsed) { free(parsed->ev); free(parsed->stems); free(parsed->ks_pool); free(parsed); }
    return count;
}

static void fms_from_event(const tl_event_t *e, fms_event_t *o)
{
    memset(o, 0, sizeof *o);
    o->t_ns = e->t_ns;
    o->sample = e->sample;
    o->voice = e->voice;
    o->stream = e->stream;
    o->x = e->x; o->y = e->y; o->z = e->z;
    o->dur_ns = e->dur_ns;
    switch (e->type) {
    case EV_ON:     o->type = FMS_EV_ON; o->freq_millihz = e->freq_millihz; o->value = e->velocity_q15; break;
    case EV_OFF:    o->type = FMS_EV_OFF; break;
    case EV_PITCH:  o->type = FMS_EV_PITCH; o->freq_millihz = e->freq_millihz; break;
    case EV_POS:    o->type = FMS_EV_POS; break;
    case EV_MOVE:   o->type = FMS_EV_MOVE; break;
    case EV_FILTER: o->type = FMS_EV_FILTER; o->freq_millihz = e->freq_millihz; o->value = e->velocity_q15; break;
    case EV_SEND:   o->type = FMS_EV_SEND; o->bus = e->bus; o->value = e->level_q15; break;
    case EV_YAW:    o->type = FMS_EV_YAW; break;
    case EV_CRUSH:  o->type = FMS_EV_CRUSH; o->value = e->crush_bits; o->bus = e->crush_div; break;
    case EV_CTL:    o->type = FMS_EV_CTL; o->bus = e->velocity_q15; o->x = e->ctl_value; break;
    default:        o->type = FMS_EV_OTHER; break;
    }
}

uint32_t fms_player_events(const fms_player_t *p, fms_event_t *out, uint32_t max)
{
    uint32_t i;
    if (!p) return 0;
    for (i = 0; out && i < max && i < p->tl->nev; i++) fms_from_event(&p->tl->ev[i], &out[i]);
    return p->tl->nev;
}

size_t fms_player_event_text(const fms_player_t *p, uint32_t index, char *out, size_t cap)
{
    tl_t *one;
    char *text, *event;
    size_t n;
    if (!p || index >= p->tl->nev) return 0;
    one = (tl_t *)malloc(sizeof *one);
    if (!one) return 0;
    *one = *p->tl; one->ev = &p->tl->ev[index]; one->nev = 1;
    text = fms_write_text(one, 0, NULL);
    free(one);
    if (!text) return 0;
    event = strstr(text, "\nhash ");
    if (event) event = strchr(event + 1, '\n');
    if (!event) { free(text); return 0; }
    ++event;
    n = strlen(event) + 1u;
    if (out && cap >= n) memcpy(out, event, n);
    free(text);
    return n;
}

/* One coordinate triple of voice v at boundary b, as the spatial module
 * reads it (space.h, sp_coord_at). */
static void fms_where_at(const sp_voice_t *v, uint32_t b, int32_t *q)
{
    q[0] = sp_coord_at(v, b, v->x0, v->x);
    q[1] = sp_coord_at(v, b, v->y0, v->y);
    q[2] = sp_coord_at(v, b, v->z0, v->z);
}

/* For drawing: where the spatial module puts voice i at the current block;
 * without the module, the voice's last pos. */
static void fms_voice_where(const fms_player_t *p, uint32_t i, int32_t *q)
{
    if (p->sp && i < SP_MAX_VOICES) {
        fms_where_at(&p->sp->v[i], p->sp->n >> p->sp->log2k, q);
    } else {
        q[0] = p->vpos[i][0]; q[1] = p->vpos[i][1]; q[2] = p->vpos[i][2];
    }
}

static void fms_path_put(fms_pathpt_t *out, uint32_t max, uint32_t *n, uint32_t voice, uint32_t sample,
                         uint32_t pen, const int32_t *q)
{
    if (out && *n < max) {
        fms_pathpt_t *o = &out[*n];
        o->voice = voice; o->sample = sample; o->pen = pen;
        o->x = q[0]; o->y = q[1]; o->z = q[2];
    }
    (*n)++;
}

uint32_t fms_player_voice_pos(const fms_player_t *p, int32_t *xyz, uint32_t max)
{
    uint32_t nv = p->tl ? (uint32_t)p->tl->nvoices : 0, i;
    for (i = 0; i < nv && i < max && xyz; i++) fms_voice_where(p, i, xyz + i * 3);
    return nv;
}

uint32_t fms_player_paths(const fms_player_t *p, fms_pathpt_t *out, uint32_t max)
{
    const tl_t *tl;
    sp_stereo_t *s;
    uint8_t seen[SP_MAX_VOICES], open[SP_MAX_VOICES];
    uint32_t n = 0, i, nv, b, last;
    int32_t q[3];
    if (!p || !p->sp || p->live) return 0;
    tl = p->tl;
    nv = tl->nvoices < SP_MAX_VOICES ? tl->nvoices : SP_MAX_VOICES;
    s = (sp_stereo_t *)calloc(1, sizeof *s);
    if (!s) return 0;
    if (sp_stereo_init(s, tl->block, tl->near)) { free(s); return 0; }
    memset(seen, 0, sizeof seen);
    memset(open, 0, sizeof open);
    for (i = 0; i < tl->nev; i++) {
        const tl_event_t *e = &tl->ev[i];
        sp_voice_t *v;
        if ((e->type != EV_POS && e->type != EV_MOVE) || e->voice >= nv) continue;
        v = &s->v[e->voice];
        s->n = e->sample;                                   /* the counter when the player applies it */
        b = (uint32_t)(((uint64_t)s->n + s->k - 1u) >> s->log2k);   /* the boundary it takes effect at */
        if (open[e->voice] && v->b1 < b) {                  /* the last move ended before it */
            fms_where_at(v, v->b1, q);
            fms_path_put(out, max, &n, e->voice, v->b1 << s->log2k, 1, q);
        }
        open[e->voice] = 0;
        if (b > 0 || e->type == EV_MOVE) {
            fms_where_at(v, b, q);
            fms_path_put(out, max, &n, e->voice, b << s->log2k, seen[e->voice] ? 1 : 0, q);
        }
        seen[e->voice] = 1;
        if (e->type == EV_POS) {
            sp_stereo_set_pos(s, e->voice, e->x, e->y, e->z);
        } else {
            sp_stereo_move(s, e->voice, e->x, e->y, e->z, e->end);
            if (v->b1 > v->b0) { open[e->voice] = 1; continue; }
        }
        q[0] = e->x; q[1] = e->y; q[2] = e->z;
        fms_path_put(out, max, &n, e->voice, b << s->log2k, 0, q);
    }
    last = tl->n ? (tl->n - 1u) >> s->log2k : 0;            /* the record's last block */
    for (i = 0; i < nv; i++) {
        sp_voice_t *v = &s->v[i];
        if (open[i]) {
            b = v->b1 < last ? v->b1 : last;
            fms_where_at(v, b, q);
            fms_path_put(out, max, &n, i, b << s->log2k, 1, q);
        } else if (!seen[i]) {
            fms_where_at(v, 0, q);
            fms_path_put(out, max, &n, i, 0, 0, q);
        }
    }
    free(s);
    return n;
}

void fms_player_rewind(fms_player_t *p)
{
    if (p) (void)fms_init_state(p);   /* immutable configuration validated at open */
}

/* tl_apply, and the last position of each voice for drawing. */
static void fms_apply(fms_player_t *p, const tl_event_t *e)
{
    tl_apply(p->vs, p->sp, p->fx, e);
    if (p->tl->decodable && e->type == EV_POS) p->positioned |= UINT64_C(1) << e->voice;
    if ((e->type == EV_POS || e->type == EV_MOVE) && e->voice < TL_MAX_VOICES) {
        p->vpos[e->voice][0] = e->x; p->vpos[e->voice][1] = e->y; p->vpos[e->voice][2] = e->z;
    }
}

uint32_t fms_player_render(fms_player_t *p, int16_t *out, uint32_t frames)
{
    const tl_t *tl;
    uint32_t done = 0;
    if (!p) return 0;
    tl = p->tl;
    while (done < frames && p->pos < tl->n && !p->render_error) {
        uint64_t next;
        uint32_t len;
        while (p->e < tl->nev && tl->ev[p->e].sample == p->pos) {
            if (p->gen) p->render_error = fms_gen_error(tl_gen_apply_status(p->gen, &tl->ev[p->e]));
            if (p->render_error) break;
            fms_apply(p, &tl->ev[p->e++]);
        }
        if (p->render_error) break;
        if (p->gen && p->pos == p->next_tick) {
            p->render_error = fms_gen_error(tl_gen_step_status(tl, p->gen, p->pos, p->vs, NULL, NULL));
            if (p->render_error) break;
            p->next_tick += tl->gen_clock;
        }
        next = p->e < tl->nev ? tl->ev[p->e].sample : tl->n;
        if (p->gen && next > p->next_tick) next = p->next_tick;
        len = (uint32_t)(next - p->pos);
        if (len > frames - done) len = frames - done;
        /* pos 0: tl_mix uses pos only as an offset into the output */
        p->clipped += tl_mix(tl, p->vs, p->sp, p->fx, out + (size_t)done * p->ch, 0, len);
        p->pos += len;
        done += len;
    }
    p->h = fms_fnv(p->h, out, (size_t)done * p->ch);
    return done;
}

int fms_player_render_checked(fms_player_t *p, int16_t *out, uint32_t frames,
                              const fms_cancel_t *cancel, uint32_t *written,
                              char *err, size_t cap)
{
    uint32_t done = 0;
    if (err && cap) err[0] = 0;
    if (written) *written = 0;
    if (!p || p->live || !written || (!out && frames) ||
        (uint64_t)frames * (p ? p->ch : 1u) > SIZE_MAX / sizeof(int16_t)) {
        fms_copy_err(err, cap, "checked rendering needs a stopped record and PCM buffer");
        return FMS_INVALID;
    }
    p->export_ready = 0;
    while (done < frames && p->pos < p->tl->n && !p->render_error) {
        uint32_t want = frames - done, got;
        if (fms_cancelled(cancel)) break;
        if (want > 256u) want = 256u;
        got = fms_player_render(p, out + (size_t)done * p->ch, want);
        done += got;
        if (!got) break;
    }
    *written = done;
    if (fms_cancelled(cancel)) { fms_copy_err(err, cap, "cancelled"); return FMS_CANCELLED; }
    if (p->render_error) { fms_copy_err(err, cap, "record generator/render failed"); return FMS_RENDER_ERROR; }
    if (p->pos == p->tl->n) {
        if (p->tl->have_hash && p->tl->hash != p->h) {
            fms_copy_err(err, cap, "record hash does not match"); return FMS_HASH_MISMATCH;
        }
        p->export_ready = 1;
    }
    return FMS_OK;
}

size_t fms_player_record_text(const fms_player_t *p, char *out, size_t cap)
{
    char *text;
    size_t n;
    if (!p || p->live || !p->export_ready || p->render_error || p->pos != p->tl->n ||
        (p->tl->have_hash && p->tl->hash != p->h)) return 0;
    text = fms_write_text(p->tl, p->h, &n);
    if (!text) return 0;
    if (out && cap > n) memcpy(out, text, n + 1u);
    free(text);
    return n + 1u;
}

int fms_player_keys(const fms_player_t *source, uint32_t half_width, int mirror,
                    int carrier_bound, const fms_cancel_t *cancel,
                    fms_player_t **out, char *err, size_t cap)
{
    char *text;
    size_t n;
    fms_player_t *p = NULL;
    keys_result_t result;
    fms_cap_t capture;
    const fms_cancel_t *previous;
    int rc;
    if (err && cap) err[0] = 0;
    if (out) *out = NULL;
    if (!out || !source || source->live ||
        (half_width != 30 && half_width != 45 && half_width != 60 && half_width != 75 && half_width != 90) ||
        (mirror != 0 && mirror != 1) || (carrier_bound != 0 && carrier_bound != 1)) {
        fms_copy_err(err, cap, "Keys needs a record, supported half-width and boolean options"); return FMS_INVALID;
    }
    if (fms_cancelled(cancel)) { fms_copy_err(err, cap, "cancelled"); return FMS_CANCELLED; }
    text = fms_write_text(source->tl, source->tl->hash, &n);
    if (!text) { fms_copy_err(err, cap, "out of memory"); return FMS_NO_MEMORY; }
    rc = fms_player_import(text, n, cancel, &p, err, cap);
    free(text);
    if (rc) return rc;
    p->tl->have_hash = source->tl->have_hash;
    p->tl->hash = source->tl->hash;
    /* The host worker changes routing. Reallocate matching playback state only
     * after it succeeds; the source player's state is never modified. */
    free(p->vs); p->vs = NULL; free(p->sp); p->sp = NULL;
    free(p->fx); p->fx = NULL; free(p->gen); p->gen = NULL;
    previous = fms_operation_cancel;
    fms_operation_cancel = cancel;
    fms_cap_begin(&capture);
    rc = keys_transform(p->tl, (int)half_width, mirror, carrier_bound, &result);
    if (!rc) { p->tl->have_hash = 1; p->tl->hash = result.result.hash; rc = fms_alloc(p); }
    if (fms_cancelled(cancel)) rc = FMS_CANCELLED;
    fms_cap_end(&capture, err, cap, NULL, NULL);
    fms_operation_cancel = previous;
    if (rc) {
        if (rc == FMS_CANCELLED) fms_copy_err(err, cap, "cancelled");
        else if (rc == FMS_HASH_MISMATCH) fms_copy_err(err, cap, "source record hash does not match");
        else if (!err || !cap || !*err) fms_copy_err(err, cap, "Keys transformation refused");
        fms_player_close(p); return rc;
    }
    *out = p;
    return FMS_OK;
}

uint32_t fms_player_position(const fms_player_t *p) { return p ? p->pos : 0; }
uint64_t fms_player_hash(const fms_player_t *p)     { return p ? p->h : 0; }
uint32_t fms_player_clipped(const fms_player_t *p)  { return p ? p->clipped : 0; }
uint32_t fms_player_error(const fms_player_t *p)    { return p ? p->render_error : 0; }

uint64_t fms_player_render_all(fms_player_t *p, uint32_t chunk, uint32_t *clipped)
{
    int16_t *buf;
    uint64_t h;
    uint32_t got;
    if (!p || p->live) return 0;
    if (!chunk || chunk > p->tl->n) chunk = p->tl->n;
    buf = (int16_t *)alloc_n((size_t)chunk * p->ch, sizeof *buf);
    if (!buf) return 0;
    fms_player_rewind(p);
    do { got = fms_player_render(p, buf, chunk); } while (got);
    h = p->render_error ? 0 : p->h;
    if (clipped) *clipped = p->clipped;
    free(buf);
    fms_player_rewind(p);
    return h;
}

int fms_player_seek(fms_player_t *p, uint32_t frame, char *err, size_t errlen)
{
    int16_t scratch[256 * FMS_MAX_CH];
    if (err && errlen) err[0] = 0;
    if (!p || p->live || frame > p->tl->n) {
        fms_copy_err(err, errlen, "seek needs a record frame from zero through its length");
        return 1;
    }
    fms_player_rewind(p);
    while (p->pos < frame && !p->render_error) {
        const uint32_t left = frame - p->pos;
        const uint32_t want = left < 256u ? left : 256u;
        if (fms_player_render(p, scratch, want) != want) break;
    }
    if (p->pos != frame || p->render_error) {
        fms_copy_err(err, errlen, "seek prefix rendering failed");
        return 1;
    }
    return 0;
}

/* Offline only. The included harness owns the byte format and atomic file
 * publication; this player owns only rendering and its reset state. */
int fms_player_write_wav(fms_player_t *p, const char *path, char *err, size_t errlen)
{
    uint64_t samples;
    int16_t *pcm;
    int rc = 1;
    fms_cap_t cap;
    if (err && errlen) err[0] = 0;
    if (!p || p->live || !path || !*path) {
        fms_copy_err(err, errlen, "WAV needs a loaded record and an output path");
        return 1;
    }
    samples = (uint64_t)p->tl->n * p->ch;
    if (samples > (UINT32_MAX - 36u) / 2u || samples > SIZE_MAX / sizeof *pcm ||
        p->tl->fs > UINT32_MAX / (2u * p->ch)) {
        fms_copy_err(err, errlen, "record is too large or too fast for a PCM16 WAV");
        return 1;
    }
    pcm = (int16_t *)alloc_n((size_t)samples, sizeof *pcm);
    if (!pcm) { fms_copy_err(err, errlen, "not enough memory to render WAV"); return 1; }
    fms_player_rewind(p);
    if (fms_player_render(p, pcm, p->tl->n) != p->tl->n || p->render_error) {
        fms_copy_err(err, errlen, "record rendering failed; WAV was not written");
    } else if (p->tl->have_hash && p->h != p->tl->hash) {
        fms_copy_err(err, errlen, "record hash does not match; WAV was not written");
    } else {
        /* Capture only this worker's file diagnostic. */
        fms_cap_begin(&cap);
        rc = write_wav_ch(path, pcm, (uint32_t)samples, p->tl->fs, p->ch);
        fms_cap_end(&cap, err, errlen, NULL, NULL);
        if (rc && (!err || !errlen || !*err)) fms_copy_err(err, errlen, "WAV write failed");
    }
    free(pcm);
    fms_player_rewind(p);
    return rc;
}

int fms_render_note(const char *lines, uint32_t fs, uint32_t freq_millihz,
                    uint32_t velocity_q15, uint32_t off_frame, int16_t *out, uint32_t frames)
{
    tl_t *tl;
    fm_voice_t v;
    uint32_t first;
    int bad = 0;
    if (!lines || !out || !fs || velocity_q15 > UINT16_MAX) return 1;
    tl = fms_patch_lines_read(lines, strlen(lines), fs, NULL, 0);
    if (!tl) return 1;
    if (tl->gen_clock) bad = 1;   /* generator-bound patches remain Records-only */
    if (!bad) {
        (void)tl_voice_init(tl, &v, 0);
        fm_voice_note_on(&v, freq_millihz, (uint16_t)velocity_q15);
        first = off_frame < frames ? off_frame : frames;
        fm_render_i16(&v, out, first);
        if (first < frames) {
            fm_voice_note_off(&v);
            fm_render_i16(&v, out + first, frames - first);
        }
    }
    free(tl->ev); free(tl->stems); free(tl->ks_pool); free(tl);
    return bad;
}

/* ---- the host ---- */

#define Q_CAP        1024u          /* live events in flight, a power of two */
#define LOG_CAP      65536u         /* events in one take */
#define SCOPE_FRAMES 8192u          /* a power of two */
#define SCRATCH      512u           /* frames per render call in the callback */

#define LD(x)      __atomic_load_n(&(x), __ATOMIC_ACQUIRE)
#define ST(x, v)   __atomic_store_n(&(x), (v), __ATOMIC_RELEASE)
#define XCHG(x, v) __atomic_exchange_n(&(x), (v), __ATOMIC_ACQ_REL)

/* One owner protects the snapshot and its source through publication/start.
 * CLAIMED contains only a source check and CAS, never initialization or I/O. */
enum { TAKE_IDLE, TAKE_PREPARING, TAKE_PENDING, TAKE_CLAIMED,
       TAKE_RECORDING, TAKE_STOPPING };

struct fms_host {
    /* UI to audio: live events */
    fms_event_t   q[Q_CAP];
    uint32_t      q_head, q_tail;
    /* the player swap */
    fms_player_t *ui_cur;           /* UI side: the last player handed over */
    fms_player_t *next;
    uint32_t      swap_req, swap_done;
    fms_player_t *cur;              /* audio side */
    /* controls */
    int           playing;
    uint32_t      gain_q15;
    /* audio to UI */
    uint32_t      pos, clipped, channels, nvoices, render_error, profile_refusals;
    uint64_t      hash;
    uint32_t      peak[FMS_MAX_CH];
    uint8_t       active[TL_MAX_VOICES];
    int32_t       vpos[TL_MAX_VOICES * 3];
    int16_t       scope[SCOPE_FRAMES * FMS_MAX_CH];
    uint32_t      scope_w;          /* frames written so far */
    int16_t       scratch[SCRATCH * FMS_MAX_CH];
    /* the take */
    int           take_state, recording, have_take, take_full;
    uint32_t      take_started;    /* atomic last-accepted-request claim outcome */
    const fms_player_t *take_src;   /* protected by take_state ownership */
    uint32_t      take_crush_head;  /* same ownership: crusher cutoff at accepted Record */
    tl_event_t   *log;
    uint32_t      nlog, take_frames;
    uint64_t      take_hash;
    tl_t          take_tl;          /* the live player's header at the start */
};

static int fms_take_change(fms_host_t *h, int from, int to)
{
    return __atomic_compare_exchange_n(&h->take_state, &from, to, 0,
                                       __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
}

fms_host_t *fms_host_create(void)
{
    fms_host_t *h = (fms_host_t *)calloc(1, sizeof *h);
    if (!h) return NULL;
    h->log = (tl_event_t *)calloc(LOG_CAP, sizeof *h->log);
    if (!h->log) { free(h); return NULL; }
    h->gain_q15 = 32768;
    return h;
}

void fms_host_destroy(fms_host_t *h)
{
    if (!h) return;
    free(h->log);
    free(h);
}

fms_player_t *fms_host_swap(fms_host_t *h, fms_player_t *p)
{
    fms_player_t *old = h->ui_cur;
    int prepared;
    /* Serialized UI calls may wait for the callback's short source check.
     * Yield rather than spin; a suspended callback can delay this handover.
     * Retrying also retargets a mismatched claim after it returns PENDING. */
    prepared = 0;
    for (;;) {
        const int state = LD(h->take_state);
        if (state == TAKE_PENDING) {
            prepared = fms_take_change(h, TAKE_PENDING, TAKE_PREPARING);
            if (!prepared) continue; /* recheck the state that won the CAS */
        } else if (state == TAKE_CLAIMED) {
            usleep(1);
            continue;
        }
        break;
    }
    if (prepared && p && p->live) {
        memcpy(&h->take_tl, p->tl, sizeof h->take_tl);
        h->take_src = p;
    }
    if (p && !p->published) {
        /* This fresh player is not callback-visible yet. Later re-publication
         * of an existing pointer never writes its callback-owned cutoff. */
        p->crush_reset_head = LD(h->q_head);
        p->crush_reset_pending = 1;
        /* A pending take retargeted by a newer successful arm uses that later
         * boundary; it must not restore the older Record cutoff on claim. */
        if (prepared && p->live) h->take_crush_head = p->crush_reset_head;
    }
    if (p) p->published = 1; /* an installed player is never a fresh seed candidate */
    h->ui_cur = p;
    ST(h->next, p);
    ST(h->swap_req, LD(h->swap_req) + 1);
    if (prepared) ST(h->take_state, p && p->live ? TAKE_PENDING : TAKE_IDLE);
    return old;
}

int fms_host_retired(const fms_host_t *h)
{
    return LD(h->swap_done) == LD(h->swap_req);
}

void fms_host_set_playing(fms_host_t *h, int on) { ST(h->playing, on ? 1 : 0); }
void fms_host_set_gain_q15(fms_host_t *h, uint32_t gain_q15) { ST(h->gain_q15, gain_q15); }

int fms_host_push(fms_host_t *h, const fms_event_t *e)
{
    uint32_t head = h->q_head, tail = LD(h->q_tail);
    if (head - tail >= Q_CAP) return -1;
    h->q[head & (Q_CAP - 1)] = *e;
    ST(h->q_head, head + 1);
    return 0;
}

/* A live event as tl_event would read it at frame pos, checked by its rules;
 * 0 ok, 1 bounds/type refusal, 2 profile refusal. Validation never changes
 * the placement mask (whole-seed preflight uses a separate local mask).
 * t_ns = ceil(pos * 10^9 / fs), so floor(t_ns * fs / 10^9) = pos. */
static int fms_live_event(const fms_player_t *p, const fms_event_t *in, tl_event_t *e)
{
    const tl_t *tl = p->tl;
    memset(e, 0, sizeof *e);
    if (p->gen) return 1;  /* stale keyboard events cannot alter a generator record */
    if (in->voice >= tl->nvoices || in->stream >= tl->nstreams || p->pos >= tl->n) return 1;
    e->t_ns = ((uint64_t)p->pos * 1000000000u + tl->fs - 1) / tl->fs;
    e->sample = p->pos;
    e->stream = in->stream;
    e->voice = in->voice;
    switch (in->type) {
    case FMS_EV_ON:
        if (in->value > UINT16_MAX) return 1;
        e->type = EV_ON; e->freq_millihz = in->freq_millihz; e->velocity_q15 = (uint16_t)in->value; return 0;
    case FMS_EV_OFF:   e->type = EV_OFF; return 0;
    case FMS_EV_PITCH: e->type = EV_PITCH; e->freq_millihz = in->freq_millihz; return 0;
    case FMS_EV_POS:
    case FMS_EV_MOVE:
        if (tl->draft != 2) return 1;
        if (in->x > SP_POS_MAX || in->x < -SP_POS_MAX || in->y > SP_POS_MAX || in->y < -SP_POS_MAX ||
            in->z > SP_POS_MAX || in->z < -SP_POS_MAX) return 1;
        if (tl->decodable && (in->type == FMS_EV_MOVE || p->pos ||
            (p->positioned & (UINT64_C(1) << in->voice)))) return 2;
        e->x = in->x; e->y = in->y; e->z = in->z;
        if (in->type == FMS_EV_POS) { e->type = EV_POS; return 0; }
        {
            uint64_t end;
            if (!in->dur_ns || in->dur_ns > UINT64_MAX / tl->fs - e->t_ns) return 1;
            end = (e->t_ns + in->dur_ns) * tl->fs / 1000000000u;
            if (end > UINT32_MAX) return 1;
            e->type = EV_MOVE; e->dur_ns = in->dur_ns; e->end = (uint32_t)end;
        }
        return 0;
    case FMS_EV_FILTER:
        if (in->value > UINT16_MAX || !(tl->filters & (1u << tl->voice[in->voice]))) return 1;
        e->type = EV_FILTER; e->freq_millihz = in->freq_millihz; e->velocity_q15 = (uint16_t)in->value; return 0;
    case FMS_EV_CRUSH:
        if (!(tl->fx.on & FX_ON_CRUSH) || in->value < 1 || in->value > 24 || in->bus < 1 || in->bus > 64) return 1;
        e->type = EV_CRUSH; e->crush_bits = in->value; e->crush_div = in->bus; return 0;
    case FMS_EV_CTL:
        if (in->bus >= FM_CTL_INPUTS || in->x < -32768 || in->x > 32768) return 1;
        e->type = EV_CTL; e->velocity_q15 = (uint16_t)in->bus; e->ctl_value = in->x; return 0;
    case FMS_EV_SEND:
        if (in->bus >= FX_BUSES || in->value > 32768 || !(tl->fx.on & FX_BUS_BIT[in->bus])) return 1;
        e->type = EV_SEND; e->bus = in->bus; e->level_q15 = in->value; return 0;
    case FMS_EV_YAW:
        if (tl->draft != 2 || in->voice || !tl->route || tl->route == SP_FIELD_PANLAW ||
            in->x > SP_YAW_MAX || in->x < -SP_YAW_MAX) return 1;
        e->type = EV_YAW; e->x = in->x; return 0;
    default:
        return 1;
    }
}

/* Candidate setup only: validate every row before publishing any seed state. */
int fms_player_seed_geometry(fms_player_t *p, const fms_event_t *events,
                             uint32_t count, char *err, size_t cap)
{
    tl_event_t seed[FMS_MAX_VOICES + 1];
    uint64_t seen = 0;
    uint32_t i, nv, have_yaw = 0;
    const char *why = "geometry seed requires a fresh unpublished live candidate";
    if (err && cap) err[0] = 0;
    if (!p || !p->live || p->pos || p->e || p->h != FNV_BASIS || p->seed_count || p->published) goto refused;
    nv = p->tl->nvoices;
    why = "geometry seed requires one pos per voice and at most one yaw";
    if (!events || !nv || nv > FMS_MAX_VOICES || count < nv || count > nv + 1) goto refused;
    memset(seed, 0, sizeof seed);
    for (i = 0; i < count; i++) {
        const fms_event_t *in = &events[i];
        tl_event_t e;
        why = "geometry seed events must have zero time, sample, stream and duration";
        if (in->t_ns || in->sample || in->stream || in->dur_ns) goto refused;
        why = "geometry seed permits unique pos rows and optional yaw for voice zero";
        if (in->type == FMS_EV_POS) {
            if (in->voice >= nv || (seen & (UINT64_C(1) << in->voice))) goto refused;
            if (fms_live_event(p, in, &e)) { why = "geometry seed pos is refused by native bounds or profile reservation"; goto refused; }
            seen |= UINT64_C(1) << in->voice;
            e.order = in->voice; seed[in->voice] = e;
        } else if (in->type == FMS_EV_YAW) {
            if (have_yaw || in->voice) goto refused;
            if (fms_live_event(p, in, &e)) { why = "geometry seed yaw is refused by native field or bounds"; goto refused; }
            have_yaw = 1; e.order = nv; seed[nv] = e;
        } else goto refused;
    }
    why = "geometry seed is missing a voice position";
    if (seen != (nv == 64 ? UINT64_MAX : (UINT64_C(1) << nv) - 1)) goto refused;
    memcpy(p->seed, seed, (size_t)(nv + have_yaw) * sizeof *seed);
    p->seed_count = nv + have_yaw;
    for (i = 0; i < p->seed_count; i++) fms_apply(p, &p->seed[i]);
    return 0;
refused:
    fms_copy_err(err, cap, why);
    return 1;
}

/* Audio thread: the take ends at the current player's frame. Events logged
 * at that frame (applied while paused, or just before a full log) touch no
 * sample of the take, and the harness refuses an event at the end: they are
 * left out. */
static void fms_take_end(fms_host_t *h)
{
    if (!h->recording) return;
    h->take_frames = h->cur ? h->cur->pos : 0;
    h->take_hash = h->cur ? h->cur->h : 0;
    while (h->nlog && h->log[h->nlog - 1].sample >= h->take_frames) h->nlog--;
    ST(h->recording, 0);
    ST(h->have_take, 1);
    ST(h->take_state, TAKE_IDLE); /* release the snapshot only after all final writes */
}

static void fms_peak_max(uint32_t *slot, uint32_t v)
{
    uint32_t old = LD(*slot);
    while (v > old && !__atomic_compare_exchange_n(slot, &old, v, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {}
}

/* The callback's handover, at the start of each call: takes the next player
 * (a swap ends a take), so the one swapped out retires. */
static void fms_swap_in(fms_host_t *h)
{
    uint32_t req = LD(h->swap_req);
    if (req != h->swap_done) {
        fms_take_end(h);                       /* a swap ends a take */
        h->cur = LD(h->next);
        ST(h->render_error, h->cur ? h->cur->render_error : 0);
        ST(h->swap_done, req);
    }
}

void fms_host_settle(fms_host_t *h)
{
    fms_swap_in(h);
    if (LD(h->take_state) == TAKE_STOPPING) fms_take_end(h);
}

/* Queue sequence arithmetic is meaningful only inside its bounded live
 * window. Clear an exhausted cutoff so it cannot reappear after uint32 wrap. */
static int fms_before_crush_reset(fms_player_t *p, uint32_t tail)
{
    uint32_t distance;
    if (!p || !p->crush_reset_pending) return 0;
    distance = p->crush_reset_head - tail;
    if (!distance || distance > Q_CAP) { p->crush_reset_pending = 0; return 0; }
    return 1;
}

void fms_host_render_f32(fms_host_t *h, float *const *out, uint32_t nout, uint32_t frames)
{
    fms_player_t *p;
    uint32_t done = 0, c, i, ch, head;
    float g;
    if (!frames) return;
    fms_swap_in(h);
    p = h->cur;
    if (fms_take_change(h, TAKE_PENDING, TAKE_CLAIMED)) {
        /* Read the source only after taking ownership: UI copies cannot
         * replace it between this comparison and the start decision. */
        if (p && p->live && p == h->take_src) {
            if (fms_take_change(h, TAKE_CLAIMED, TAKE_RECORDING)) {
                ST(h->take_started, 1); /* authoritative accepted request reached rewind */
                /* The claim is over before bounded initialization begins. */
                fms_player_rewind(p);
                if (!p->render_error) {
                    p->crush_reset_head = h->take_crush_head;
                    p->crush_reset_pending = 1;
                }
                /* The immutable candidate placement precedes queued live events. */
                h->nlog = p->seed_count;
                memcpy(h->log, p->seed, (size_t)h->nlog * sizeof *h->log);
                ST(h->take_full, 0);
                ST(h->recording, 1);
            } else ST(h->take_state, TAKE_IDLE); /* stop won before start */
        } else if (!fms_take_change(h, TAKE_CLAIMED, TAKE_PENDING)) {
            ST(h->take_state, TAKE_IDLE); /* stop won over the mismatch retry */
        }
    }
    if (LD(h->take_state) == TAKE_STOPPING) fms_take_end(h);
    /* live events, at the player's current frame */
    head = LD(h->q_head);
    while (h->q_tail != head) {
        const fms_event_t *in = &h->q[h->q_tail & (Q_CAP - 1)];
        tl_event_t e;
        const int before_reset = fms_before_crush_reset(p, h->q_tail);
        const int refusal = p && !(in->type == FMS_EV_CRUSH && before_reset)
            ? fms_live_event(p, in, &e) : 1;
        if (!refusal) {
            if (h->recording && h->nlog == LOG_CAP) {   /* the take is full: it ends here */
                ST(h->take_full, 1);
                fms_take_end(h);
            }
            if (h->recording) { e.order = h->nlog; h->log[h->nlog++] = e; }
            fms_apply(p, &e);
        } else if (refusal == 2) ST(h->profile_refusals, LD(h->profile_refusals) + 1u);
        ST(h->q_tail, h->q_tail + 1);
    }
    (void)fms_before_crush_reset(p, h->q_tail); /* also clears an empty/fully drained cutoff */
    ch = p ? p->ch : 1;
    g = (float)LD(h->gain_q15) / (32768.0f * 32768.0f);
    if (!p || !LD(h->playing)) {
        for (c = 0; c < nout; c++) memset(out[c], 0, sizeof(float) * frames);
    } else {
        while (done < frames) {
            uint32_t want = frames - done < SCRATCH ? frames - done : SCRATCH;
            uint32_t got = fms_player_render(p, h->scratch, want), w = h->scope_w;
            if (got < want) memset(h->scratch + (size_t)got * ch, 0, sizeof(int16_t) * (want - got) * ch);
            for (i = 0; i < got; i++, w++)
                memcpy(&h->scope[(w & (SCOPE_FRAMES - 1)) * FMS_MAX_CH], &h->scratch[(size_t)i * ch], sizeof(int16_t) * ch);
            ST(h->scope_w, w);
            for (c = 0; c < ch && c < FMS_MAX_CH; c++) {
                uint32_t pk = 0;
                for (i = 0; i < got; i++) {
                    int32_t s = h->scratch[(size_t)i * ch + c];
                    uint32_t a = (uint32_t)(s < 0 ? -s : s);
                    if (a > pk) pk = a;
                }
                fms_peak_max(&h->peak[c], pk);
            }
            for (c = 0; c < nout; c++) {
                const uint32_t src = ch == 1 ? 0 : c;
                float *o = out[c] + done;
                if (src >= ch || ch > nout) { memset(o, 0, sizeof(float) * want); continue; }
                for (i = 0; i < want; i++) o[i] = (float)h->scratch[(size_t)i * ch + src] * g;
            }
            done += want;
        }
    }
    ST(h->render_error, p ? p->render_error : 0);
    /* for drawing */
    if (p) {
        ST(h->channels, p->ch);
        ST(h->nvoices, p->tl->nvoices);
        ST(h->pos, p->pos);
        ST(h->hash, p->h);
        ST(h->clipped, p->clipped);
        for (i = 0; i < p->tl->nvoices; i++) {
            ST(h->active[i], (uint8_t)(fm_voice_active(&p->vs[i]) ? 1 : 0));
            int32_t q[3];
            fms_voice_where(p, i, q);
            ST(h->vpos[i * 3], q[0]);
            ST(h->vpos[i * 3 + 1], q[1]);
            ST(h->vpos[i * 3 + 2], q[2]);
        }
    } else {
        ST(h->nvoices, 0);
        ST(h->pos, 0);
    }
}

void fms_host_record_start(fms_host_t *h)
{
    if (!h || !h->ui_cur || !h->ui_cur->live ||
        !fms_take_change(h, TAKE_IDLE, TAKE_PREPARING)) return;
    ST(h->have_take, 0);
    ST(h->take_started, 0); /* only the successful PREPARING owner starts a new outcome */
    memcpy(&h->take_tl, h->ui_cur->tl, sizeof h->take_tl);
    h->take_src = h->ui_cur;
    h->take_crush_head = LD(h->q_head);
    ST(h->take_state, TAKE_PENDING);
}

void fms_host_record_request_stop(fms_host_t *h)
{
    int state = LD(h->take_state);
    /* UI calls are serialized, so PREPARING cannot belong to another UI
     * operation here. A concurrent callback can change only the other states. */
    while (state == TAKE_PENDING || state == TAKE_CLAIMED || state == TAKE_RECORDING) {
        int next = state == TAKE_PENDING ? TAKE_IDLE : TAKE_STOPPING;
        if (__atomic_compare_exchange_n(&h->take_state, &state, next, 0,
                                        __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return;
    }
}

/* A timeout cannot prove that a callback stopped. Leave its stop pending;
 * callers with proven quiescence use record_stop_now instead. */
void fms_host_record_stop(fms_host_t *h)
{
    int k;
    fms_host_record_request_stop(h);
    for (k = 0; k < 250 && LD(h->take_state) == TAKE_STOPPING; k++) usleep(1000);
}

int fms_host_take_full(const fms_host_t *h)
{
    return LD(h->take_full);
}

/* Offline rendering only (FMS_SCRIPT): no audio callback runs, so the take
 * ends here at once, with no wait. */
void fms_host_record_stop_now(fms_host_t *h)
{
    fms_host_record_request_stop(h);
    fms_take_end(h);
}

int fms_host_recording(const fms_host_t *h)
{
    return LD(h->take_state) != TAKE_IDLE;
}

int fms_host_record_started(const fms_host_t *h)
{
    return h ? LD(h->take_started) : 0;
}

size_t fms_host_take_text(fms_host_t *h, char *out, size_t cap)
{
    tl_t *tl;
    char *txt;
    size_t len = 0;
    if (!LD(h->have_take) || !h->take_frames || !h->take_tl.fs) return 0;
    tl = (tl_t *)malloc(sizeof *tl);
    if (!tl) return 0;
    memcpy(tl, &h->take_tl, sizeof *tl);
    tl->length_ns = ((uint64_t)h->take_frames * 1000000000u + tl->fs - 1) / tl->fs;
    tl->n = h->take_frames;
    tl->ev = h->log;
    tl->nev = h->nlog;
    tl->stems = NULL;
    txt = fms_write_text(tl, h->take_hash, &len);
    free(tl);
    if (!txt) return 0;
    if (out && len + 1 <= cap) memcpy(out, txt, len + 1);
    free(txt);
    return len + 1;
}

uint32_t fms_host_position(const fms_host_t *h) { return LD(h->pos); }
uint32_t fms_host_error(const fms_host_t *h) { return h ? LD(h->render_error) : 0; }
uint32_t fms_host_profile_refusals(const fms_host_t *h) { return h ? LD(h->profile_refusals) : 0; }
uint64_t fms_host_hash(const fms_host_t *h)     { return LD(h->hash); }
uint32_t fms_host_clipped(const fms_host_t *h)  { return LD(h->clipped); }

uint32_t fms_host_scope(const fms_host_t *h, int16_t *out, uint32_t frames, uint32_t *channels)
{
    uint32_t w = LD(h->scope_w), ch = LD(h->channels), n, i, start;
    if (!ch) ch = 1;
    if (channels) *channels = ch;
    n = frames < SCOPE_FRAMES ? frames : SCOPE_FRAMES;
    if (n > w) n = w;
    start = w - n;
    for (i = 0; i < n; i++)
        memcpy(out + (size_t)i * ch, &h->scope[((start + i) & (SCOPE_FRAMES - 1)) * FMS_MAX_CH], sizeof(int16_t) * ch);
    return n;
}

void fms_host_peaks(const fms_host_t *h, uint32_t *peak, uint32_t nch)
{
    fms_host_t *m = (fms_host_t *)h;   /* the read clears the peaks */
    uint32_t c;
    for (c = 0; c < nch; c++) peak[c] = c < FMS_MAX_CH ? XCHG(m->peak[c], 0) : 0;
}

uint32_t fms_host_voice_active(const fms_host_t *h, uint8_t *active, uint32_t max)
{
    uint32_t n = LD(h->nvoices), i;
    for (i = 0; i < max && i < n; i++) active[i] = LD(h->active[i]);
    return n;
}

void fms_host_voice_pos(const fms_host_t *h, int32_t *xyz, uint32_t max)
{
    uint32_t n = LD(h->nvoices), i;
    for (i = 0; i < max && i < n; i++) {
        xyz[i * 3] = LD(h->vpos[i * 3]);
        xyz[i * 3 + 1] = LD(h->vpos[i * 3 + 1]);
        xyz[i * 3 + 2] = LD(h->vpos[i * 3 + 2]);
    }
}

/* For fms_verify and the Makefile: this build's engine line. */
void fms_engine_line(char *out, size_t cap)
{
    tl_engine(out, cap);
}
