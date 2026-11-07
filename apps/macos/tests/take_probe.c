/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0 */
#include "fmspike-take.c"
#include <time.h>

static pthread_mutex_t gate = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static unsigned blocked, reached[8], released[8], checks, scenarios;
#define REQUIRE(x) do { checks++; if (!(x)) { fprintf(stderr, "TAKE_API assertion line=%d: %s\n", __LINE__, #x); exit(1); } } while (0)
static void take_hook(int point)
{
    pthread_mutex_lock(&gate);
    reached[point]++;
    pthread_cond_broadcast(&changed);
    while ((blocked & (1u << point)) && !released[point]) pthread_cond_wait(&changed, &gate);
    pthread_mutex_unlock(&gate);
}
static void reset_hooks(unsigned mask)
{
    pthread_mutex_lock(&gate);
    blocked = mask; memset(reached, 0, sizeof reached); memset(released, 0, sizeof released);
    pthread_mutex_unlock(&gate);
}
static void await_hook(int point)
{
    struct timespec end;
    clock_gettime(CLOCK_REALTIME, &end); end.tv_sec += 3;
    pthread_mutex_lock(&gate);
    while (!reached[point]) {
        const int rc = pthread_cond_timedwait(&changed, &gate, &end);
        if (rc) { fprintf(stderr, "TAKE_API assertion hook timeout=%d\n", point); exit(1); }
    }
    pthread_mutex_unlock(&gate);
}
static void release_hook(int point)
{
    pthread_mutex_lock(&gate); released[point] = 1;
    pthread_cond_broadcast(&changed); pthread_mutex_unlock(&gate);
}
static const char *patches[] = {
    "patch a 0 65536 140 0 0 256 2 900 120 250\npatch a 1 131072 0 2048 0 128 1 600 200 200\n",
    "patch b 0 131072 60 0 0 256 2 900 120 250\npatch b 1 131072 0 2048 0 128 1 600 200 200\n",
    "patch c 0 196608 100 0 0 256 2 900 120 250\npatch c 1 131072 0 2048 0 128 1 600 200 200\n"
};
static fms_player_t *player(unsigned n)
{
    const char name[2] = { (char)('a' + n), 0 };
    char err[FMS_ERR_LEN], *text;
    size_t need = fms_live_header_text(patches[n], name, 1, 48000, 0, NULL, NULL, 0);
    fms_player_t *p;
    REQUIRE(need); text = malloc(need); REQUIRE(text);
    REQUIRE(fms_live_header_text(patches[n], name, 1, 48000, 0, NULL, text, need) == need);
    p = fms_player_open_live(text, err, sizeof err); free(text); REQUIRE(p); return p;
}
static fms_host_t *host(fms_player_t *a)
{
    fms_host_t *h = fms_host_create(); REQUIRE(h);
    fms_host_swap(h, a); fms_host_settle(h); fms_host_set_playing(h, 1); return h;
}
static void render(fms_host_t *h)
{
    float output[64], *outs[] = { output };
    fms_host_render_f32(h, outs, 1, 64);
}
static void note(fms_host_t *h)
{
    fms_event_t e = {0}; e.type = FMS_EV_ON; e.freq_millihz = 261626; e.value = 26000;
    REQUIRE(fms_host_push(h, &e) == 0);
}
struct work { fms_host_t *h; fms_player_t *p; int op; };
static void *worker(void *arg)
{
    struct work *w = arg;
    if (w->op == 0) render(w->h);
    else if (w->op == 1) fms_host_swap(w->h, w->p);
    else fms_host_record_start(w->h);
    return NULL;
}
static pthread_t launch(struct work *w)
{
    pthread_t t; REQUIRE(!pthread_create(&t, NULL, worker, w)); return t;
}
static void join(pthread_t t) { REQUIRE(!pthread_join(t, NULL)); }
static void dispose(fms_host_t *h)
{
    reset_hooks(0); fms_host_record_stop_now(h); fms_host_swap(h, NULL);
    fms_host_settle(h); REQUIRE(fms_host_retired(h)); fms_host_destroy(h);
}
static void replay(fms_host_t *h, const char *path)
{
    char err[FMS_ERR_LEN], *text;
    size_t need;
    FILE *f;
    fms_player_t *p;
    fms_info_t info;
    uint64_t hash;
    fms_host_record_stop_now(h); REQUIRE(!fms_host_recording(h));
    need = fms_host_take_text(h, NULL, 0); REQUIRE(need); text = malloc(need); REQUIRE(text);
    REQUIRE(fms_host_take_text(h, text, need) == need);
    f = fopen(path, "w"); REQUIRE(f); REQUIRE(fputs(text, f) >= 0); REQUIRE(!fclose(f)); free(text);
    p = fms_player_open(&path, 1, err, sizeof err); REQUIRE(p); fms_player_info(p, &info);
    hash = fms_player_render_all(p, 17, NULL);
    REQUIRE(info.have_hash && info.hash_expected == hash && h->take_hash == hash && info.frames == 64);
    fms_player_close(p);
}
int main(int argc, char **argv)
{
    fms_player_t *a = player(0), *b = player(1), *c = player(2);
    fms_host_t *h;
    pthread_t audio, ui;
    struct work aw, uw;
    unsigned mismatch;
    REQUIRE(argc == 2);
    /* UI owns PREPARING while audio keeps rendering: no callback snapshot copy. */
    h = host(a); reset_hooks(1u << 5); uw = (struct work){ h, NULL, 2 }; ui = launch(&uw); await_hook(5);
    REQUIRE(LD(h->take_state) == TAKE_PREPARING); render(h); REQUIRE(!h->recording);
    REQUIRE(LD(h->take_state) == TAKE_PREPARING); release_hook(5); join(ui);
    reset_hooks(0); note(h); render(h); REQUIRE(h->recording && h->take_src == a); replay(h, argv[1]); dispose(h); scenarios++;

    /* A claimed matching source excludes another start and a swap's snapshot.
     * The callback wins this start, then the later handover ends it normally. */
    h = host(a); fms_host_record_start(h); note(h); reset_hooks(1u << 3);
    aw = (struct work){ h, NULL, 0 }; audio = launch(&aw); await_hook(3);
    fms_host_record_start(h); REQUIRE(LD(h->take_state) == TAKE_CLAIMED);
    uw = (struct work){ h, b, 1 }; ui = launch(&uw); await_hook(6);
    REQUIRE(h->take_src == a && LD(h->take_state) == TAKE_CLAIMED);
    release_hook(3); join(audio); join(ui); REQUIRE(h->recording && h->take_src == a);
    replay(h, argv[1]); dispose(h); scenarios++;

    /* B replaces the pending source while A is this callback's local player;
     * C arrives during the mismatch claim and must retarget after it yields. */
    h = host(a); fms_host_record_start(h); reset_hooks((1u << 1) | (1u << 2));
    aw = (struct work){ h, NULL, 0 }; audio = launch(&aw); await_hook(1);
    fms_host_swap(h, b); release_hook(1); await_hook(2);
    REQUIRE(h->take_src == b && h->cur == a);
    uw = (struct work){ h, c, 1 }; ui = launch(&uw); await_hook(6);
    REQUIRE(LD(h->take_state) == TAKE_CLAIMED); release_hook(2); join(audio); join(ui);
    REQUIRE(LD(h->take_state) == TAKE_PENDING && h->take_src == c);
    reset_hooks(0); fms_host_settle(h); REQUIRE(fms_host_retired(h));
    fms_player_close(b); b = NULL; /* reclaim the superseded pending source */
    note(h); render(h); REQUIRE(h->cur == c && h->recording); replay(h, argv[1]); dispose(h);
    b = player(1); scenarios++;

    /* Stop must win both claim exits. The matching case also forces the real
     * 250ms timeout while audio is stalled: elapsed time cannot release it. */
    for (mismatch = 0; mismatch < 2; mismatch++) {
        h = host(a); fms_host_record_start(h); reset_hooks((1u << 1) | (1u << 2));
        aw = (struct work){ h, NULL, 0 }; audio = launch(&aw); await_hook(1);
        if (mismatch) fms_host_swap(h, b);
        release_hook(1); await_hook(2);
        if (mismatch) fms_host_record_request_stop(h); else fms_host_record_stop(h);
        REQUIRE(LD(h->take_state) == TAKE_STOPPING && fms_host_recording(h));
        fms_host_record_start(h); REQUIRE(LD(h->take_state) == TAKE_STOPPING);
        release_hook(2); join(audio);
        REQUIRE(LD(h->take_state) == TAKE_IDLE && !h->recording && !h->have_take);
        dispose(h); scenarios++;
    }

    /* Stop after RECORDING publication but before rewind/recording flag. */
    h = host(a); fms_host_record_start(h); reset_hooks(1u << 7);
    aw = (struct work){ h, NULL, 0 }; audio = launch(&aw); await_hook(7);
    REQUIRE(LD(h->take_state) == TAKE_RECORDING && !h->recording);
    fms_host_record_stop(h); REQUIRE(LD(h->take_state) == TAKE_STOPPING);
    fms_host_record_start(h); REQUIRE(LD(h->take_state) == TAKE_STOPPING);
    release_hook(7); join(audio); REQUIRE(!fms_host_recording(h) && !h->recording && !h->take_frames);
    dispose(h); scenarios++;

    /* Pending cancel, non-live swap, and offline finalization need no worker. */
    h = host(a); reset_hooks(0); fms_host_record_start(h); fms_host_record_request_stop(h);
    render(h); REQUIRE(!fms_host_recording(h) && !h->recording);
    fms_host_record_start(h); fms_host_swap(h, NULL); render(h);
    REQUIRE(!fms_host_recording(h)); fms_host_record_start(h); REQUIRE(!fms_host_recording(h));
    fms_host_swap(h, a); fms_host_settle(h); fms_host_record_start(h); note(h); render(h);
    REQUIRE(h->recording); fms_host_record_start(h); REQUIRE(h->recording && h->take_src == a);
    replay(h, argv[1]); dispose(h); scenarios++;

    fms_player_close(a); fms_player_close(b); fms_player_close(c);
    printf("TAKE_API checks=%u scenarios=%u snapshot=UI claim=exclusive stop=retained swap=retargeted replay=yes ok\n", checks, scenarios);
    return 0;
}
