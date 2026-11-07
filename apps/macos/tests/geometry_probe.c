/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0 */
#include "fmspike-geometry.c"

static unsigned checks;
#define REQUIRE(x) do { checks++; if (!(x)) { fprintf(stderr, "GEOMETRY assertion line=%d: %s\n", __LINE__, #x); exit(1); } } while (0)
static const char *patch = "patch a 0 65536 140 0 0 256 2 900 120 250\npatch a 1 131072 0 2048 0 128 1 600 200 200\n";
static fms_player_t *player(unsigned field, unsigned voices)
{
    fms_space_t space;
    fms_player_t *p;
    char err[FMS_ERR_LEN], *text;
    size_t n;
    fms_space_defaults(&space); space.field = field;
    if (field == FMS_SPACE_BINAURAL) { space.present = FMS_SPACE_YAW; space.yaw_mdeg = 17000; }
    n = fms_live_header_text(patch, "a", voices, 48000, &space, NULL, NULL, 0);
    text = malloc(n); REQUIRE(n && text);
    REQUIRE(fms_live_header_text(patch, "a", voices, 48000, &space, NULL, text, n) == n);
    p = fms_player_open_live(text, err, sizeof err); free(text); REQUIRE(p); return p;
}
static void events(fms_event_t *e, unsigned n, int yaw)
{
    unsigned i;
    memset(e, 0, sizeof *e * (n + (yaw != 0)));
    /* Deliberately unordered; native storage must canonicalize. */
    for (i = 0; i < n; i++) {
        e[i].type = FMS_EV_POS; e[i].voice = n - i - 1;
        e[i].x = (int32_t)e[i].voice * 113 - 800;
        e[i].y = 1000 + (int32_t)e[i].voice * 19;
        e[i].z = 200 - (int32_t)e[i].voice * 7;
    }
    if (yaw) { e[n].type = FMS_EV_YAW; e[n].x = -47000; }
}
static fms_host_t *host(fms_player_t *p)
{
    fms_host_t *h = fms_host_create(); REQUIRE(h);
    fms_host_swap(h, p); fms_host_settle(h); fms_host_set_playing(h, 1); return h;
}
static void note(fms_host_t *h)
{
    fms_event_t e = {0}; e.type = FMS_EV_ON; e.freq_millihz = 261626; e.value = 26000;
    REQUIRE(!fms_host_push(h, &e));
}
static void render(fms_host_t *h, int16_t *pcm, unsigned channels, unsigned frames)
{
    float a[256], b[256], *out[] = {a, b}; unsigned i, c;
    REQUIRE(frames <= 256); fms_host_render_f32(h, out, channels, frames);
    for (i = 0; i < frames; i++) for (c = 0; c < channels; c++) pcm[i * channels + c] = (int16_t)(out[c][i] * 32768.0f);
}
static void close_host(fms_host_t *h)
{
    fms_host_record_stop_now(h); fms_host_swap(h, NULL); fms_host_settle(h); fms_host_destroy(h);
}
static void run(unsigned field, const char *path)
{
    unsigned nv = 2, ch = field == FMS_SPACE_OFF ? 1 : 2, nseed = nv + (field == FMS_SPACE_BINAURAL);
    unsigned i, made;
    fms_event_t e[3], queued = {0}; char err[FMS_ERR_LEN], *text;
    fms_player_t *p = player(field, nv), *plain;
    fms_host_t *h;
    int16_t pcm[512], wanted[512], first[512];
    int32_t xyz[6]; FILE *f; size_t need;
    events(e, nv, field == FMS_SPACE_BINAURAL);
    REQUIRE(!fms_player_seed_geometry(p, e, nseed, err, sizeof err) && !err[0]);
    REQUIRE(p->seed_count == nseed && p->seed[0].voice == 0 && p->seed[1].voice == 1);
    REQUIRE(fms_player_voice_pos(p, xyz, nv) == nv && xyz[0] == -800 && xyz[2] == 200 && xyz[3] == -687);
    h = host(p); note(h); render(h, first, ch, 256);
    fms_player_rewind(p); /* offline: h is quiescent */
    REQUIRE(fms_player_voice_pos(p, xyz, nv) == nv && xyz[0] == -800 && xyz[3] == -687);
    note(h); render(h, pcm, ch, 256); REQUIRE(!memcmp(pcm, first, sizeof(int16_t) * 256 * ch));
    /* Take start rewinds again. An equal-frame queued placement overrides
     * only voice1, after all seed rows and before the note. */
    fms_host_record_start(h);
    queued.type = FMS_EV_POS; queued.voice = 1; queued.x = 555; queued.y = 777; queued.z = -999;
    REQUIRE(!fms_host_push(h, &queued)); note(h); render(h, pcm, ch, 256);
    fms_host_record_stop_now(h);
    need = fms_host_take_text(h, NULL, 0); REQUIRE(need); text = malloc(need); REQUIRE(text);
    REQUIRE(fms_host_take_text(h, text, need) == need);
    f = fopen(path, "w"); REQUIRE(f); REQUIRE(fputs(text, f) >= 0); REQUIRE(!fclose(f)); free(text);
    plain = fms_player_open(&path, 1, err, sizeof err); REQUIRE(plain);
    REQUIRE(fms_player_seed_geometry(plain, e, nseed, err, sizeof err) == 1);
    made = 0;
    while (made < 256) { unsigned n = 256 - made; if (n > 7) n = 7; REQUIRE(fms_player_render(plain, wanted + made * ch, n) == n); made += n; }
    REQUIRE(!memcmp(pcm, wanted, sizeof(int16_t) * 256 * ch));
    REQUIRE(fms_player_hash(plain) == fms_host_hash(h));
    REQUIRE(h->nlog == nseed + 2);
    for (i = 0; i < nv; i++) REQUIRE(h->log[i].type == EV_POS && h->log[i].voice == i && h->log[i].sample == 0);
    if (field == FMS_SPACE_BINAURAL) REQUIRE(h->log[nv].type == EV_YAW && h->log[nv].x == -47000);
    REQUIRE(h->log[nseed].type == EV_POS && h->log[nseed].x == 555 && h->log[nseed + 1].type == EV_ON);
    /* Independent plain record receives explicit rows, not the seed API. */
    fms_player_rewind(plain); REQUIRE(fms_player_render(plain, wanted, 256) == 256);
    REQUIRE(!memcmp(pcm, wanted, sizeof(int16_t) * 256 * ch));
    fms_player_close(plain); close_host(h); fms_player_close(p);
}
static void refusals(void)
{
    unsigned k, field;
    {
        fms_player_t *p = player(FMS_SPACE_OFF, 1); fms_event_t e[1]; char err[FMS_ERR_LEN]; int16_t pcm;
        events(e, 1, 0);
        REQUIRE(fms_player_seed_geometry(NULL, e, 1, err, sizeof err) == 1 && err[0]);
        REQUIRE(fms_player_seed_geometry(p, NULL, 1, err, sizeof err) == 1);
        REQUIRE(fms_player_render(p, &pcm, 1) == 1);
        REQUIRE(fms_player_seed_geometry(p, e, 1, err, sizeof err) == 1 && fms_player_position(p) == 1);
        fms_player_close(p);
    }
    {
        fms_player_t *p = player(FMS_SPACE_OFF, 1); fms_host_t *h = host(p);
        fms_event_t e[1]; char err[FMS_ERR_LEN]; events(e, 1, 0);
        REQUIRE(!fms_player_position(p) && fms_player_seed_geometry(p, e, 1, err, sizeof err) == 1);
        close_host(h); REQUIRE(fms_player_seed_geometry(p, e, 1, err, sizeof err) == 1);
        fms_player_close(p);
    }
    for (field = 0; field <= FMS_SPACE_BINAURAL; field++) for (k = 0; k < 17; k++) {
        fms_player_t *p = player(field, 2);
        fms_event_t e[3]; char err[FMS_ERR_LEN]; unsigned n = 2;
        int32_t before[6], after[6];
        events(e, 2, 0); REQUIRE(fms_player_voice_pos(p, before, 2) == 2);
        switch (k) {
        case 0: n = 0; break;
        case 1: n = 1; break;
        case 2: n = 4; break;
        case 3: e[0].voice = 2; break;
        case 4: e[0].voice = e[1].voice; break;
        case 5: e[0].t_ns = 1; break;
        case 6: e[0].sample = 1; break;
        case 7: e[0].stream = 1; break;
        case 8: e[0].dur_ns = 1; break;
        case 9: e[0].type = FMS_EV_MOVE; break;
        case 10: e[0].type = FMS_EV_ON; break;
        case 11: e[0].x = 16777217; break;
        case 12: e[0].y = -16777217; break;
        case 13: e[0].z = INT32_MIN; break;
        case 14: n = 3; e[2].type = FMS_EV_YAW; e[2].voice = 1; break;
        case 15: n = 3; e[2].type = FMS_EV_YAW; e[2].x = 360001; break;
        case 16: n = 3; e[2].type = FMS_EV_POS; e[2].voice = 1; break;
        }
        REQUIRE(fms_player_seed_geometry(p, e, n, err, sizeof err) == 1 && err[0]);
        REQUIRE(p->seed_count == 0 && fms_player_hash(p) == FNV_BASIS && !fms_player_position(p));
        REQUIRE(fms_player_voice_pos(p, after, 2) == 2 && !memcmp(before, after, sizeof before));
        events(e, 2, 0); REQUIRE(!fms_player_seed_geometry(p, e, 2, err, sizeof err));
        REQUIRE(fms_player_seed_geometry(p, e, 2, err, sizeof err) == 1);
        REQUIRE(p->seed_count == 2); fms_player_close(p);
    }
    for (field = 0; field < FMS_SPACE_BINAURAL; field++) {
        fms_player_t *p = player(field, 1); fms_event_t e[2]; char err[FMS_ERR_LEN];
        events(e, 1, 1); REQUIRE(fms_player_seed_geometry(p, e, 2, err, sizeof err) == 1); fms_player_close(p);
    }
    {
        fms_player_t *p = player(FMS_SPACE_BINAURAL, FMS_MAX_VOICES);
        fms_event_t e[FMS_MAX_VOICES + 1]; char err[FMS_ERR_LEN];
        events(e, FMS_MAX_VOICES, 1); e[0].x = 16777216; e[1].x = -16777216;
        e[FMS_MAX_VOICES].x = -360000;
        REQUIRE(!fms_player_seed_geometry(p, e, FMS_MAX_VOICES + 1, err, sizeof err));
        REQUIRE(p->seed_count == FMS_MAX_VOICES + 1);
        REQUIRE(p->seed[63].x == 16777216 && p->seed[62].x == -16777216);
        fms_player_close(p);
    }
}
int main(int argc, char **argv)
{
    REQUIRE(argc == 2); refusals();
    run(FMS_SPACE_STEREO, argv[1]); run(FMS_SPACE_BINAURAL, argv[1]); run(FMS_SPACE_OFF, argv[1]);
    printf("GEOMETRY_API checks=%u fields=3 voices=64 atomic=yes rewind=yes order=yes replay=yes player_bytes=%zu previous_prefix_bytes=%zu host_bytes=%zu seed_bytes=%zu ok\n",
           checks, sizeof(fms_player_t), offsetof(fms_player_t, seed_count), sizeof(fms_host_t), sizeof(((fms_player_t *)0)->seed));
    return 0;
}
