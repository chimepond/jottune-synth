/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 * Read-only browser projection. The native bridge owns TL parsing and paths;
 * the kept library owns coordinate interpolation. This never renders audio. */
#include <stddef.h>
#include "../../bindings/native/jottune_bridge.c"

#define VIEW_LIMIT 20000u

static void state_row(uint32_t voice, uint32_t sample, const sp_voice_t *v, int comma)
{
    printf("%s[%u,%u,%d,%d,%d,%d,%d,%d,%u,%u]", comma ? "," : "",
           voice, sample, v->x, v->y, v->z, v->x0, v->y0, v->z0, v->b0, v->b1);
}

int main(int argc, char **argv)
{
    fms_player_t *p;
    const tl_t *tl;
    fms_pathpt_t *paths;
    sp_stereo_t *space;
    char error[512];
    uint32_t count, i, emitted = 0, spatial_events = 0;
    if (argc != 2) return 2;
    p = fms_player_open((const char *const *)&argv[1], 1, error, sizeof error);
    if (!p) { fprintf(stderr, "%s\n", error); return 2; }
    tl = p->tl;
    if (!p->sp) { puts("null"); fms_player_close(p); return 0; }
    count = fms_player_paths(p, NULL, 0);
    if (!count) {
        fputs("Native spatial paths are unavailable.\n", stderr); fms_player_close(p); return 2;
    }
    for (i = 0; i < tl->nev; ++i)
        if (tl->ev[i].type == EV_POS || tl->ev[i].type == EV_MOVE || tl->ev[i].type == EV_YAW) ++spatial_events;
    if (count > VIEW_LIMIT || spatial_events > VIEW_LIMIT) {
        fputs("Spatial view exceeds its event limit.\n", stderr); fms_player_close(p); return 2;
    }
    paths = (fms_pathpt_t *)calloc(count ? count : 1u, sizeof *paths);
    space = (sp_stereo_t *)calloc(1, sizeof *space);
    if (!paths || !space || sp_stereo_init(space, tl->block, tl->near)) {
        free(paths); free(space); fms_player_close(p); return 2;
    }
    if (fms_player_paths(p, paths, count) != count) {
        fputs("Native spatial paths are incomplete.\n", stderr);
        free(paths); free(space); fms_player_close(p); return 2;
    }
    printf("{\"format\":1,\"block\":%u,\"near\":%u,\"initialYaw\":%d,\"frames\":%u,\"voices\":%u,\"fs\":%u,\"channels\":%u,\"route\":%d,",
           tl->block, tl->near, tl->yaw, tl->n, tl->nvoices, tl->fs, p->ch, tl->route);
    printf("\"layout\":{\"bytes\":%u,\"b0\":%u,\"b1\":%u},\"paths\":[",
           (unsigned)sizeof(sp_voice_t), (unsigned)offsetof(sp_voice_t, b0), (unsigned)offsetof(sp_voice_t, b1));
    for (i = 0; i < count; ++i) {
        const fms_pathpt_t *q = &paths[i];
        printf("%s{\"voice\":%u,\"sample\":%u,\"pen\":%u,\"x\":%d,\"y\":%d,\"z\":%d}",
               i ? "," : "", q->voice, q->sample, q->pen, q->x, q->y, q->z);
    }
    printf("],\"states\":[");
    for (i = 0; i < tl->nvoices; ++i) state_row(i, 0, &space->v[i], emitted++ != 0);
    for (i = 0; i < tl->nev; ++i) {
        const tl_event_t *e = &tl->ev[i];
        uint32_t sample;
        if (e->type != EV_POS && e->type != EV_MOVE) continue;
        space->n = e->sample;
        sample = (uint32_t)(((uint64_t)e->sample + space->k - 1u) >> space->log2k) << space->log2k;
        if (e->type == EV_POS) sp_stereo_set_pos(space, e->voice, e->x, e->y, e->z);
        else sp_stereo_move(space, e->voice, e->x, e->y, e->z, e->end);
        state_row(e->voice, sample, &space->v[e->voice], emitted++ != 0);
    }
    printf("],\"yaws\":[");
    emitted = 0;
    for (i = 0; i < tl->nev; ++i) if (tl->ev[i].type == EV_YAW) {
        const tl_event_t *e = &tl->ev[i];
        uint32_t sample = (uint32_t)(((uint64_t)e->sample + space->k - 1u) >> space->log2k) << space->log2k;
        printf("%s{\"sample\":%u,\"yaw\":%d}", emitted++ ? "," : "", sample, e->x);
    }
    puts("]}");
    free(paths); free(space); fms_player_close(p); return 0;
}
