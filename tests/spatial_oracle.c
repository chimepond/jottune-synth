/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 * Exercise actual native playback, independently of the display projection. */
#include "../bindings/native/jottune_bridge.c"
int main(int argc, char **argv)
{
    fms_player_t *p;
    uint32_t sample, voice;
    int16_t pcm[FMS_MAX_CH];
    int32_t coordinates[SP_MAX_VOICES][3], yaw = 0;
    char error[512];
    if (argc != 2) return 2;
    p = fms_player_open((const char *const *)&argv[1], 1, error, sizeof error);
    if (!p) { fprintf(stderr, "%s\n", error); return 2; }
    if (!p->sp) { fms_player_close(p); return 0; }
    for (sample = 0; sample < p->tl->n; ++sample) {
        if (fms_player_render(p, pcm, 1) != 1) { fms_player_close(p); return 2; }
        if (sample % p->tl->block == 0) {
            for (voice = 0; voice < p->tl->nvoices; ++voice)
                fms_where_at(&p->sp->v[voice], sample >> p->sp->log2k, coordinates[voice]);
            yaw = p->tl->route ? ((sp_field_t *)p->sp)->yaw : 0;
        }
        if (sample % p->tl->block > 1 && sample % p->tl->block != p->tl->block-1) continue;
        for (voice = 0; voice < p->tl->nvoices; ++voice) {
            printf("%u %u %d %d %d %d\n", sample, voice,
                coordinates[voice][0], coordinates[voice][1], coordinates[voice][2], yaw);
        }
    }
    fms_player_close(p); return 0;
}
