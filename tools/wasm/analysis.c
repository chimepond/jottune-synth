/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 * Optional source-analysis host. The production TL reader, verifier, dry
 * renderer and writer are included unchanged. No engine bytes are replaced. */
#define main analysis_unused_cli_main
#include "harness.c"
#undef main

/* This wrapper has one fresh, private virtual directory, one fixed input and
 * one fixed output. It deliberately does not expose filesystem destinations:
 * WASI realpath/FIFO are unavailable, whereas the native dry writer itself is
 * supported. The JS adapter consumes dry.wav.tmp as a bounded byte stream. */
int main(int argc, char **argv)
{
    tl_t *tl = NULL;
    fm_voice_t *voices = NULL;
    char *files[] = { "record.tl" }, *end;
    uint64_t limit, dry_bytes, mix_bytes;
    int rc = 2;
    if (argc != 3 || strcmp(argv[1], "record.tl") || !argv[2][0]) return 2;
    for (end = argv[2]; *end; ++end) if (*end < '0' || *end > '9') return 2;
    errno = 0; limit = strtoull(argv[2], &end, 10);
    if (errno || *end || !limit || limit > UINT64_C(2147483648)) return 2;
    tl = (tl_t *)calloc(1, sizeof *tl);
    if (!tl) return HOST_ERROR;
    rc = tl_read(tl, files[0], 1);
    if (!rc) rc = tl_check_header(tl);
    if (!rc) rc = tl_read(tl, files[0], 2);
    if (!rc) rc = tl_check_decodable(tl);
    if (rc) goto cleanup;
    dry_bytes = (uint64_t)tl->n * tl->nvoices * 4u;
    mix_bytes = (uint64_t)tl->n * (tl->route ? tl_field_ch(tl) : tl->space ? 2u : 1u) * 2u;
    if (dry_bytes > limit || mix_bytes > limit || dry_bytes > UINT32_MAX - 36u ||
        (uint64_t)tl->fs * tl->nvoices * 4u > UINT32_MAX ||
        !tl->n || tl->n > (uint64_t)tl->fs * 300u) {
        fprintf(stderr, "Dry analysis exceeds its byte or 300-second work limit\n"); rc = 2; goto cleanup;
    }
    /* Full native verification precedes every diagnostic, including a second
     * block partition. The parent also compares PLAY hash to kept-render PCM. */
    rc = play(files, 1, NULL, NULL, NULL, 0, 0, 1, -1, limit, NULL, -1, 0, NULL);
    if (rc) goto cleanup;
    if (fflush(stdout)) { rc = HOST_ERROR; goto cleanup; }
    if (tl->nev) qsort(tl->ev, tl->nev, sizeof *tl->ev, tl_cmp);
    {
        uint32_t bus, event;
        for (bus = 0; bus < FX_BUSES; ++bus) if (bus != FX_DUCK) {
            uint32_t nonzero = 0;
            for (event = 0; event < tl->nev; ++event)
                if (tl->ev[event].type == EV_SEND && tl->ev[event].bus == bus && tl->ev[event].level_q15) ++nonzero;
            printf("ANALYSIS_BUS bus=%s configured=%u nonzero_send_events=%u\n", FX_BUS_NAME[bus],
                   (tl->fx.on & FX_BUS_BIT[bus]) != 0u, nonzero);
        }
    }
    voices = (fm_voice_t *)alloc_n(tl->nvoices, sizeof *voices);
    if (!voices) { rc = HOST_ERROR; goto cleanup; }
    rc = tl_write_stems(tl, voices, "dry.wav", -1, 1);
cleanup:
    free(voices);
    if (tl) { free(tl->ev); free(tl->ks_pool); free(tl->document_header); free(tl); }
    return rc;
}
