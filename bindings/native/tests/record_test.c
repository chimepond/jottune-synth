/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * Exercise the shared record contract without an audio device or sandbox I/O.
 * Arguments are complete, independently hashed TL regression fixtures. */
#if !defined(__APPLE__) && !defined(_POSIX_C_SOURCE)
#define _POSIX_C_SOURCE 200809L
#endif
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "jottune_bridge.h"

static unsigned checks, failures, fixtures, keys_manual, keys_generated, fields;
static const char *context = "record contract";
static const char *bounded_source;

#define CHECK(condition) do { \
    ++checks; \
    if (!(condition)) { \
        ++failures; \
        fprintf(stderr, "FAIL %s:%d: %s: %s\n", __FILE__, __LINE__, context, #condition); \
    } \
} while (0)

static uint64_t hash_pcm(const int16_t *pcm, size_t count)
{
    uint64_t hash = UINT64_C(14695981039346656037);
    size_t i;
    for (i = 0; i < count; ++i) {
        const uint16_t word = (uint16_t)pcm[i];
        hash ^= word & 255u; hash *= UINT64_C(1099511628211);
        hash ^= word >> 8; hash *= UINT64_C(1099511628211);
    }
    return hash;
}

static char *read_file(const char *path, size_t *length)
{
    FILE *file = fopen(path, "rb");
    char *text = NULL;
    long size;
    if (!file) return NULL;
    if (!fseek(file, 0, SEEK_END) && (size = ftell(file)) >= 0 &&
        (uint64_t)size < SIZE_MAX && !fseek(file, 0, SEEK_SET)) {
        text = (char *)malloc((size_t)size + 1u);
        if (text && fread(text, 1, (size_t)size, file) != (size_t)size) {
            free(text); text = NULL;
        }
        if (text) { text[size] = 0; *length = (size_t)size; }
    }
    if (fclose(file)) { free(text); text = NULL; }
    return text;
}

static fms_player_t *import_text(const char *text, size_t length)
{
    char error[FMS_ERR_LEN];
    fms_player_t *player = NULL;
    const int status = fms_player_import(text, length, NULL, &player, error, sizeof error);
    CHECK(status == FMS_OK && player != NULL && error[0] == 0);
    if (status) fprintf(stderr, "import: %s\n", error);
    return player;
}

static int render_checked(fms_player_t *player, int16_t *pcm, uint32_t frames,
                          uint32_t channels, uint32_t chunk)
{
    uint32_t position = 0;
    char error[FMS_ERR_LEN];
    int status = FMS_OK;
    while (position < frames) {
        uint32_t written = UINT32_MAX;
        const uint32_t wanted = frames - position < chunk ? frames - position : chunk;
        status = fms_player_render_checked(player, pcm + (size_t)position * channels,
                                           wanted, NULL, &written, error, sizeof error);
        if (status || written != wanted || error[0]) {
            fprintf(stderr, "checked render: status=%d wanted=%u written=%u: %s\n",
                    status, wanted, written, error);
            return 0;
        }
        position += written;
    }
    return status == FMS_OK && fms_player_position(player) == frames && !fms_player_error(player);
}

static char *canonical_text(fms_player_t *player, size_t *length)
{
    const size_t needed = fms_player_record_text(player, NULL, 0);
    char *text;
    CHECK(needed > 1);
    if (needed < 2) return NULL;
    text = (char *)malloc(needed);
    CHECK(text != NULL);
    if (!text) return NULL;
    memset(text, 0xa5, needed);
    CHECK(fms_player_record_text(player, text, needed - 1u) == needed);
    {
        size_t i;
        int untouched = 1;
        for (i = 0; i < needed; ++i) if ((unsigned char)text[i] != 0xa5u) untouched = 0;
        CHECK(untouched);
    }
    CHECK(fms_player_record_text(player, text, needed) == needed);
    CHECK(text[needed - 1u] == 0 && strlen(text) + 1u == needed);
    *length = needed - 1u;
    return text;
}

static void compare_metadata(fms_player_t *memory, fms_player_t *path)
{
    fms_info_t a, b;
    fms_event_t *ea, *eb;
    uint32_t i;
    fms_player_info(memory, &a); fms_player_info(path, &b);
    CHECK(!memcmp(&a, &b, sizeof a));
    CHECK(a.fs && a.channels && a.channels <= FMS_MAX_CH && a.frames && a.have_hash);
    CHECK(fms_player_events(memory, NULL, 0) == a.nevents);
    ea = (fms_event_t *)calloc((size_t)a.nevents + 1u, sizeof *ea);
    eb = (fms_event_t *)calloc((size_t)a.nevents + 1u, sizeof *eb);
    CHECK(ea && eb);
    if (ea && eb) {
        CHECK(fms_player_events(memory, ea, a.nevents) == a.nevents);
        CHECK(fms_player_events(path, eb, a.nevents) == a.nevents);
        CHECK(!memcmp(ea, eb, (size_t)a.nevents * sizeof *ea));
    }
    free(ea); free(eb);
    for (i = 0; i < a.nstreams; ++i)
        CHECK(!strcmp(fms_player_stream_name(memory, i), fms_player_stream_name(path, i)));
    for (i = 0; i < a.nvoices; ++i)
        CHECK(!strcmp(fms_player_voice_patch(memory, i), fms_player_voice_patch(path, i)));
}

static void event_text_contract(fms_player_t *memory, fms_player_t *path, const char *canonical)
{
    const uint32_t count = fms_player_events(memory, NULL, 0);
    const uint32_t position = fms_player_position(memory), clips = fms_player_clipped(memory);
    const uint64_t hash = fms_player_hash(memory);
    fms_event_t *events = (fms_event_t *)calloc((size_t)count + 1u, sizeof *events);
    uint32_t i;
    char guard[4] = { 'a', 'b', 'c', 0 };
    CHECK(fms_player_event_text(memory, count, guard, sizeof guard) == 0);
    CHECK(!strcmp(guard, "abc"));
    CHECK(fms_player_event_text(NULL, 0, NULL, 0) == 0);
    CHECK(events != NULL);
    if (!events) return;
    CHECK(fms_player_events(memory, events, count) == count);
    for (i = 0; i < count; ++i) {
        const size_t need = fms_player_event_text(memory, i, NULL, 0);
        char *a, *b;
        char prefix[96];
        size_t j;
        int untouched = 1;
        CHECK(need >= 3u && fms_player_event_text(path, i, NULL, 0) == need);
        if (need < 3u) continue;
        a = (char *)malloc(need); b = (char *)malloc(need);
        CHECK(a && b);
        if (!a || !b) { free(a); free(b); continue; }
        memset(a, 0xa5, need);
        CHECK(fms_player_event_text(memory, i, a, need - 1u) == need);
        for (j = 0; j < need; ++j) if ((unsigned char)a[j] != 0xa5u) untouched = 0;
        CHECK(untouched);
        CHECK(fms_player_event_text(memory, i, a, need) == need);
        CHECK(fms_player_event_text(path, i, b, need) == need);
        CHECK(a[need - 1u] == 0 && a[need - 2u] == '\n' && strlen(a) + 1u == need);
        CHECK(!memcmp(a, b, need));
        snprintf(prefix, sizeof prefix, "%llu %s ", (unsigned long long)events[i].t_ns,
                 fms_player_stream_name(memory, events[i].stream));
        CHECK(!strncmp(a, prefix, strlen(prefix)));
        if (canonical) {
            const char *at = strstr(canonical, a);
            CHECK(at && (at == canonical || at[-1] == '\n'));
        }
        free(a); free(b);
    }
    free(events);
    CHECK(fms_player_position(memory) == position && fms_player_hash(memory) == hash &&
          fms_player_clipped(memory) == clips);
}

/* Raw AmbiX coefficients survive the shared PCM API and display scope. A
 * two-channel device must receive silence instead of a duplicated W channel. */
static void field_adapter(const char *text, size_t length, const fms_info_t *info,
                           const int16_t *reference)
{
    fms_player_t *player;
    fms_host_t *host;
    float channels[FMS_MAX_CH][256], *outputs[FMS_MAX_CH];
    int16_t scope[256 * FMS_MAX_CH];
    uint32_t c, i, scope_channels = 0;
    const uint32_t count = info->frames < 256u ? info->frames : 256u;
    int silent = 1, preserved = 1, nonzero = 0;
    if (info->channels != 4u && info->channels != 16u) return;
    ++fields;
    player = import_text(text, length);
    host = fms_host_create();
    CHECK(host != NULL);
    if (!player || !host) { fms_player_close(player); fms_host_destroy(host); return; }
    for (c = 0; c < FMS_MAX_CH; ++c) {
        outputs[c] = channels[c];
        for (i = 0; i < 256u; ++i) channels[c][i] = 1.0f;
    }
    CHECK(fms_host_swap(host, player) == NULL);
    fms_host_set_playing(host, 1);
    fms_host_render_f32(host, outputs, 2, 256);
    for (c = 0; c < 2u; ++c)
        for (i = 0; i < 256u; ++i) if (channels[c][i] != 0.0f) silent = 0;
    CHECK(silent);
    CHECK(fms_host_hash(host) == hash_pcm(reference, (size_t)count * info->channels));
    CHECK(fms_host_scope(host, scope, 256, &scope_channels) == count);
    CHECK(scope_channels == info->channels);
    CHECK(!memcmp(scope, reference, (size_t)count * info->channels * sizeof *scope));
    for (i = 0; i < count * info->channels; ++i) if (scope[i]) nonzero = 1;
    CHECK(nonzero);
    /* Quiescent rewind is legal after the callback returns. */
    fms_host_set_playing(host, 0);
    fms_player_rewind(player);
    fms_host_set_playing(host, 1);
    fms_host_render_f32(host, outputs, info->channels, 256);
    for (c = 0; c < info->channels; ++c)
        for (i = 0; i < count; ++i)
            if (channels[c][i] != (float)reference[(size_t)i * info->channels + c] / 32768.0f)
                preserved = 0;
    CHECK(preserved);
    CHECK(fms_host_swap(host, NULL) == player);
    fms_host_settle(host);
    CHECK(fms_host_retired(host));
    fms_host_destroy(host); fms_player_close(player);
}

/* Compare derived players from both source entry points, including an
 * original at EOF. This catches changing source state/hash or losing the
 * native generator program while cloning it for projection. */
static void keys_parity(fms_player_t *memory, fms_player_t *path, int generated)
{
    fms_info_t before, after, derived_info;
    const uint64_t source_hash = fms_player_hash(memory);
    const uint32_t source_position = fms_player_position(memory);
    const uint32_t source_clipped = fms_player_clipped(memory);
    char error[FMS_ERR_LEN], *source_text, *after_text;
    size_t source_length = 0, after_length = 0;
    fms_cancel_t *cancel = fms_cancel_create();
    fms_player_t *cancelled = NULL;
    int mirror;
    fms_player_info(memory, &before);
    source_text = canonical_text(memory, &source_length);
    CHECK(cancel != NULL);
    if (cancel) {
        fms_cancel_request(cancel);
        CHECK(fms_player_keys(memory, 60, 0, 1, cancel, &cancelled, error, sizeof error) == FMS_CANCELLED);
        CHECK(cancelled == NULL && strstr(error, "cancel") != NULL);
        fms_cancel_close(cancel);
    }
    for (mirror = 0; mirror <= 1; ++mirror) {
        fms_player_t *a = NULL, *b = NULL;
        char *ta = NULL, *tb = NULL;
        size_t na = 0, nb = 0;
        int16_t *pa = NULL, *pb = NULL;
        const int ra = fms_player_keys(memory, 60, mirror, 1, NULL, &a, error, sizeof error);
        CHECK(ra == FMS_OK && a != NULL);
        if (ra) fprintf(stderr, "Keys memory: %s\n", error);
        CHECK(fms_player_keys(path, 60, mirror, 1, NULL, &b, error, sizeof error) == FMS_OK && b != NULL);
        if (!a || !b) { fms_player_close(a); fms_player_close(b); continue; }
        fms_player_info(a, &derived_info);
        CHECK(derived_info.channels == 2u && derived_info.fs == before.fs &&
              derived_info.frames == before.frames && derived_info.have_hash);
        pa = (int16_t *)malloc((size_t)derived_info.frames * 2u * sizeof *pa);
        pb = (int16_t *)malloc((size_t)derived_info.frames * 2u * sizeof *pb);
        CHECK(pa && pb);
        if (pa && pb) {
            CHECK(render_checked(a, pa, derived_info.frames, 2, 4096));
            CHECK(render_checked(b, pb, derived_info.frames, 2, 256));
            CHECK(!memcmp(pa, pb, (size_t)derived_info.frames * 2u * sizeof *pa));
            CHECK(fms_player_hash(a) == derived_info.hash_expected && !fms_player_clipped(a));
            ta = canonical_text(a, &na); tb = canonical_text(b, &nb);
            CHECK(ta && tb && na == nb && !memcmp(ta, tb, na + 1u));
            if (generated && ta) CHECK(strstr(ta, "gen_clock ") && strstr(ta, "gen_lane "));
        }
        free(ta); free(tb); free(pa); free(pb);
        fms_player_close(a); fms_player_close(b);
    }
    fms_player_info(memory, &after);
    CHECK(!memcmp(&before, &after, sizeof before));
    CHECK(fms_player_hash(memory) == source_hash && fms_player_position(memory) == source_position &&
          fms_player_clipped(memory) == source_clipped);
    after_text = canonical_text(memory, &after_length);
    CHECK(source_text && after_text && source_length == after_length &&
          !memcmp(source_text, after_text, source_length + 1u));
    free(source_text); free(after_text);
}

static void fixture(const char *path)
{
    static const uint32_t chunks[] = { 1, 7, 256, 4096 };
    fms_player_t *memory = NULL, *legacy = NULL, *again = NULL;
    fms_info_t info;
    char error[FMS_ERR_LEN], *text, *copy = NULL, *canonical = NULL, *canonical_again = NULL;
    size_t length = 0, canonical_length = 0, canonical_again_length = 0, samples;
    int16_t *reference = NULL, *pcm = NULL;
    uint32_t clipped, z;
    context = path;
    text = read_file(path, &length);
    CHECK(text != NULL);
    if (!text) return;
    copy = (char *)malloc(length + 1u);
    CHECK(copy != NULL);
    if (copy) memcpy(copy, text, length + 1u);
    legacy = fms_player_open(&path, 1, error, sizeof error);
    CHECK(legacy != NULL);
    if (!legacy) fprintf(stderr, "legacy import: %s\n", error);
    memory = import_text(text, length);
    CHECK(!copy || !memcmp(copy, text, length + 1u));
    if (copy) {
        /* Imported state must outlive the caller's borrowed input buffer,
         * including embedded wavetable assets and generator definitions. */
        memset(text, 0xa5, length);
        free(text); text = copy; copy = NULL;
    }
    if (!legacy || !memory) goto done;
    ++fixtures;
    compare_metadata(memory, legacy);
    event_text_contract(memory, legacy, NULL);
    fms_player_info(memory, &info);
    CHECK(fms_player_record_text(memory, NULL, 0) == 0);
    if ((uint64_t)info.frames > (uint64_t)info.fs * 10u) {
        printf("RECORD metadata-only %s (%u frames)\n", path, info.frames);
        goto done;
    }
    if (!bounded_source) bounded_source = path;
    samples = (size_t)info.frames * info.channels;
    CHECK(samples <= SIZE_MAX / sizeof *pcm);
    reference = (int16_t *)malloc(samples * sizeof *reference);
    pcm = (int16_t *)malloc(samples * sizeof *pcm);
    CHECK(reference && pcm);
    if (!reference || !pcm) goto done;
    CHECK(fms_player_render(legacy, reference, info.frames) == info.frames);
    CHECK(!fms_player_error(legacy));
    CHECK(fms_player_hash(legacy) == info.hash_expected && hash_pcm(reference, samples) == info.hash_expected);
    clipped = fms_player_clipped(legacy);
    for (z = 0; z < sizeof chunks / sizeof chunks[0]; ++z) {
        uint32_t written = UINT32_MAX;
        fms_player_rewind(memory);
        CHECK(fms_player_record_text(memory, NULL, 0) == 0);
        CHECK(render_checked(memory, pcm, info.frames, info.channels, chunks[z]));
        CHECK(!memcmp(reference, pcm, samples * sizeof *pcm));
        CHECK(fms_player_hash(memory) == info.hash_expected && fms_player_clipped(memory) == clipped);
        CHECK(fms_player_render_checked(memory, pcm, 1, NULL, &written, error, sizeof error) == FMS_OK);
        CHECK(written == 0);
    }
    canonical = canonical_text(memory, &canonical_length);
    if (canonical) {
        event_text_contract(memory, legacy, canonical);
        again = import_text(canonical, canonical_length);
        if (again) {
            CHECK(render_checked(again, pcm, info.frames, info.channels, 4096));
            CHECK(!memcmp(reference, pcm, samples * sizeof *pcm));
            CHECK(fms_player_hash(again) == info.hash_expected);
            canonical_again = canonical_text(again, &canonical_again_length);
            CHECK(canonical_again && canonical_length == canonical_again_length &&
                  !memcmp(canonical, canonical_again, canonical_length + 1u));
        }
    }
    if (!keys_manual && info.channels == 1u && !strstr(text, "gen_clock ")) {
        ++keys_manual; keys_parity(memory, legacy, 0);
    } else if (!keys_generated && strstr(text, "gen_lane ")) {
        ++keys_generated; keys_parity(memory, legacy, 1);
    }
    field_adapter(text, length, &info, reference);
    printf("RECORD %s fs=%u channels=%u frames=%u hash=%016llx\n", path,
           info.fs, info.channels, info.frames, (unsigned long long)info.hash_expected);
done:
    CHECK(!copy || !memcmp(copy, text, length + 1u));
    free(copy); free(text); free(canonical); free(canonical_again); free(reference); free(pcm);
    fms_player_close(memory); fms_player_close(legacy); fms_player_close(again);
}

typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t condition;
    int start;
} start_gate_t;

typedef struct {
    start_gate_t *gate;
    const char *text, *own, *other;
    unsigned failed;
} diagnostic_job_t;

static void *invalid_worker(void *opaque)
{
    diagnostic_job_t *job = (diagnostic_job_t *)opaque;
    unsigned i;
    pthread_mutex_lock(&job->gate->mutex);
    while (!job->gate->start) pthread_cond_wait(&job->gate->condition, &job->gate->mutex);
    pthread_mutex_unlock(&job->gate->mutex);
    for (i = 0; i < 64u; ++i) {
        char error[FMS_ERR_LEN];
        fms_player_t *player = NULL;
        const int status = fms_player_import(job->text, strlen(job->text), NULL, &player, error, sizeof error);
        if (status != FMS_INVALID || player || !strstr(error, job->own) || strstr(error, job->other))
            ++job->failed;
        fms_player_close(player);
    }
    return NULL;
}

static void diagnostic_threads(void)
{
    start_gate_t gate;
    pthread_t threads[2];
    diagnostic_job_t jobs[2];
    int created[2];
    unsigned i;
    context = "concurrent diagnostics";
    CHECK(pthread_mutex_init(&gate.mutex, NULL) == 0);
    CHECK(pthread_cond_init(&gate.condition, NULL) == 0);
    gate.start = 0;
    jobs[0] = (diagnostic_job_t){ &gate, "invalid_alpha 1\n", "invalid_alpha", "invalid_beta", 0 };
    jobs[1] = (diagnostic_job_t){ &gate, "invalid_beta 2\n", "invalid_beta", "invalid_alpha", 0 };
    for (i = 0; i < 2u; ++i) {
        created[i] = pthread_create(&threads[i], NULL, invalid_worker, &jobs[i]) == 0;
        CHECK(created[i]);
    }
    pthread_mutex_lock(&gate.mutex);
    gate.start = 1; pthread_cond_broadcast(&gate.condition);
    pthread_mutex_unlock(&gate.mutex);
    for (i = 0; i < 2u; ++i) if (created[i]) {
        CHECK(pthread_join(threads[i], NULL) == 0);
        CHECK(jobs[i].failed == 0);
    }
    CHECK(pthread_cond_destroy(&gate.condition) == 0);
    CHECK(pthread_mutex_destroy(&gate.mutex) == 0);
}

static void invalid_inputs(const char *path)
{
    static const char nul[] = "timeline draft1\0hidden\n";
    char error[FMS_ERR_LEN], tiny[4] = { 'a', 'b', 'c', 'd' };
    fms_player_t *player = NULL;
    fms_cancel_t *cancel = fms_cancel_create();
    char *text, *bom, *mismatch;
    size_t length = 0;
    context = "invalid inputs and lifecycle";
    text = read_file(path, &length);
    CHECK(text != NULL && cancel != NULL);
    if (!text || !cancel) { free(text); fms_cancel_close(cancel); return; }
    fms_cancel_request(cancel);
    CHECK(fms_player_import(text, length, cancel, &player, error, sizeof error) == FMS_CANCELLED);
    CHECK(player == NULL && strstr(error, "cancel"));
    fms_cancel_close(cancel);
    CHECK(fms_player_import(NULL, 1, NULL, &player, error, sizeof error) == FMS_INVALID);
    CHECK(player == NULL && error[0]);
    CHECK(fms_player_import(text, length, NULL, NULL, error, sizeof error) == FMS_INVALID);
    CHECK(fms_player_import(nul, sizeof nul - 1u, NULL, &player, error, sizeof error) == FMS_INVALID);
    CHECK(player == NULL && strstr(error, "NUL"));
    CHECK(fms_player_import("invalid_statement\n", 18, NULL, &player, tiny + 1, 1) == FMS_INVALID);
    CHECK(player == NULL && tiny[0] == 'a' && tiny[1] == 0 && tiny[2] == 'c' && tiny[3] == 'd');
    bom = (char *)malloc(length + 3u);
    mismatch = (char *)malloc(length + 1u);
    CHECK(bom && mismatch);
    if (bom) {
        memcpy(bom, "\xef\xbb\xbf", 3); memcpy(bom + 3, text, length);
        CHECK(fms_player_import(bom, length + 3u, NULL, &player, error, sizeof error) == FMS_INVALID);
        CHECK(player == NULL && strstr(error, "byte-order mark"));
    }
    if (mismatch) {
        char *hash;
        memcpy(mismatch, text, length + 1u);
        hash = strstr(mismatch, "\nhash ");
        CHECK(hash != NULL);
        if (hash) {
            fms_info_t info;
            uint32_t written = 0;
            int16_t *pcm;
            fms_player_t *derived = NULL;
            hash[6] = hash[6] == '0' ? '1' : '0';
            player = import_text(mismatch, length);
            if (player) {
                fms_player_info(player, &info);
                CHECK(fms_player_keys(player, 60, 0, 1, NULL, &derived, error, sizeof error) == FMS_HASH_MISMATCH);
                CHECK(derived == NULL);
                pcm = (int16_t *)malloc((size_t)info.frames * info.channels * sizeof *pcm);
                CHECK(pcm != NULL);
                if (pcm) {
                    CHECK(fms_player_render_checked(player, pcm, info.frames, NULL, &written, error, sizeof error) == FMS_HASH_MISMATCH);
                    CHECK(written == info.frames && strstr(error, "hash"));
                    CHECK(fms_player_record_text(player, NULL, 0) == 0);
                }
                free(pcm); fms_player_close(player); player = NULL;
            }
        }
    }
    {
        unsigned i;
        int repeated = 1;
        for (i = 0; i < 32u; ++i) {
            int16_t pcm[64 * FMS_MAX_CH];
            uint32_t written = 0;
            if (fms_player_import(text, length, NULL, &player, error, sizeof error) != FMS_OK || !player) {
                repeated = 0; break;
            }
            if (fms_player_render_checked(player, pcm, 64, NULL, &written, error, sizeof error) != FMS_OK || written != 64)
                repeated = 0;
            fms_player_close(player); player = NULL;
        }
        CHECK(repeated);
    }
    fms_player_close(player);
    free(text); free(bom); free(mismatch);
}

typedef struct {
    start_gate_t *gate;
    fms_player_t *player;
    fms_cancel_t *cancel;
    int16_t *pcm;
    uint32_t wanted, written;
    int status;
} render_job_t;

static void *render_worker(void *opaque)
{
    render_job_t *job = (render_job_t *)opaque;
    char error[FMS_ERR_LEN];
    pthread_mutex_lock(&job->gate->mutex);
    job->gate->start = 1; pthread_cond_signal(&job->gate->condition);
    pthread_mutex_unlock(&job->gate->mutex);
    job->status = fms_player_render_checked(job->player, job->pcm, job->wanted,
                                           job->cancel, &job->written, error, sizeof error);
    return NULL;
}

static void cancellation(void)
{
    char engine[160], text[1024], error[FMS_ERR_LEN];
    fms_player_t *player = NULL, *replacement = NULL;
    fms_cancel_t *cancel = fms_cancel_create();
    int16_t *pcm;
    uint32_t written = 0, position;
    pthread_t thread;
    start_gate_t gate;
    render_job_t job;
    struct timespec delay = { 0, 1000000 };
    context = "cancel, join and replace";
    fms_engine_line(engine, sizeof engine);
    snprintf(text, sizeof text,
             "timeline draft1\nengine %s\nfs 48000\nlength_ns 3600000000000\nstreams cancel\n"
             "patch tone 0 65536 60 0 0 256 1 0 0 1\n"
             "patch tone 1 65536 0 0 0 0 1 0 0 1\n"
             "voice 0 tone\n0 cancel 0 on 440000 32767\n", engine);
    player = import_text(text, strlen(text));
    pcm = (int16_t *)malloc((64u + 48000u * 30u) * sizeof *pcm);
    CHECK(player && pcm && cancel);
    if (!player || !pcm || !cancel) goto done;
    CHECK(fms_player_render_checked(player, pcm, 64, NULL, &written, error, sizeof error) == FMS_OK && written == 64);
    CHECK(pthread_mutex_init(&gate.mutex, NULL) == 0);
    CHECK(pthread_cond_init(&gate.condition, NULL) == 0);
    gate.start = 0;
    job = (render_job_t){ &gate, player, cancel, pcm + 64u, 48000u * 30u, 0, FMS_INVALID };
    if (pthread_create(&thread, NULL, render_worker, &job) == 0) {
        pthread_mutex_lock(&gate.mutex);
        while (!gate.start) pthread_cond_wait(&gate.condition, &gate.mutex);
        pthread_mutex_unlock(&gate.mutex);
        (void)nanosleep(&delay, NULL);
        fms_cancel_request(cancel);
        CHECK(pthread_join(thread, NULL) == 0);
        CHECK(job.status == FMS_CANCELLED && job.written < job.wanted);
        position = 64u + job.written;
        CHECK(fms_player_position(player) == position && fms_player_hash(player) == hash_pcm(pcm, position));
        CHECK(!fms_player_error(player) && fms_player_record_text(player, NULL, 0) == 0);
        /* The token is one-use. A fresh import replaces the cancelled worker
         * only after join; it receives fresh independently owned state. */
        replacement = import_text(text, strlen(text));
        CHECK(replacement && fms_player_position(replacement) == 0);
        if (replacement) {
            written = 0;
            CHECK(fms_player_render_checked(replacement, pcm, 64, NULL, &written, error, sizeof error) == FMS_OK && written == 64);
            CHECK(fms_player_hash(replacement) == hash_pcm(pcm, 64));
        }
    } else CHECK(0);
    CHECK(pthread_cond_destroy(&gate.condition) == 0);
    CHECK(pthread_mutex_destroy(&gate.mutex) == 0);
done:
    fms_player_close(player); fms_player_close(replacement);
    fms_cancel_close(cancel); free(pcm);
}

int main(int argc, char **argv)
{
    int i;
    if (argc < 2) { fprintf(stderr, "usage: record_test COMPLETE_HASHED_TL...\n"); return 2; }
    for (i = 1; i < argc; ++i) fixture(argv[i]);
    CHECK(bounded_source != NULL);
    if (bounded_source) invalid_inputs(bounded_source);
    diagnostic_threads();
    cancellation();
    context = "fixture coverage";
    CHECK(fixtures == (unsigned)(argc - 1));
    CHECK(keys_manual == 1 && keys_generated == 1);
    CHECK(fields >= 2);
    printf("RECORD checks=%u failures=%u fixtures=%u Keys_manual=%u Keys_generator=%u raw_fields=%u\n",
           checks, failures, fixtures, keys_manual, keys_generated, fields);
    return failures != 0;
}
