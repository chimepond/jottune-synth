/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * fms_verify: checks the bridge against the records, without an audio device.
 *   fms_verify FILE...             one pool, rendered at several chunk sizes
 *   fms_verify --live TAKE [--stereo|--binaural]
 *                                  a scripted live take through
 *                                  fms_host_render_f32, written to TAKE and
 *                                  replayed with fms_player_open
 *   fms_verify --space-bounds     exact header ranges/presence/FX and refusal checks
 *   fms_verify --live-values GOLDEN TAKE ORDER [order|yaw|pinna]
 *                                  first64/nonzero4096 plain prefixes; optional dropped-row control
 *   fms_verify --live-move TAKE   two-block XYZ movement and complete take replay
 *   fms_verify --seek FILE...      ten prefix/continuation and handover checks
 *   fms_verify --wav OUT FILE...   offline native WAV and rewind check
 *   fms_verify --gen PREFIX        generator lifecycle fixtures at PREFIX-*.tl
 *   fms_verify --dropped FILE GOLDENS
 *                                  original and typed-statement fixtures;
 *                                  every complete line round trip is exact
 *   fms_verify --lines TEMP FILE...  canonical patch round trips, no audio
 *   fms_verify --lines-drop TEMP FILE...  dropped-line negative control
 *   fms_verify --engine            this build's engine line
 *   fms_verify --midi FILE         MIDI messages through the translator
 *                                  (fms_midi_ump), one event a line: FILE is
 *                                  "config NVOICES RELEASE_MS BEND_CENTS RPN
 *                                  CUTOFF_MILLIHZ Q_MILLI CHANNELS [CTL_READS]"
 *                                  (masks in hex), optional "ctl CHANNEL SPEC" and
 *                                  "mpe off|lower:N|upper:N", then
 *                                  "T_NS HEX..." lines (tools/midi_check.py)
 *   fms_verify --midi-live TAKE    a scripted MIDI stream through the
 *                                  translator under a live player, written to
 *                                  TAKE and replayed with fms_player_open
 *   fms_verify --midi-scan         every pitch exponent the translator meets:
 *                                  none may lie within 2^-60 of a half
 * Exit 0 when every hash matches, 1 otherwise.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "jottune_bridge.h"

#define FNV_BASIS 14695981039346656037ULL
#define FNV_PRIME 1099511628211ULL

static uint64_t fnv(uint64_t h, int16_t s)
{
    uint16_t u = (uint16_t)s;
    h ^= u & 0xFFu;        h *= FNV_PRIME;
    h ^= (u >> 8) & 0xFFu; h *= FNV_PRIME;
    return h;
}

static uint32_t lcg(uint32_t *x) { *x = *x * 1664525u + 1013904223u; return *x >> 8; }

/* Renders the pool at each chunk pattern from frame 0; prints one line. */
static int verify_pool(char **files, int nf)
{
    static const uint32_t SIZES[] = { 1, 7, 64, 471, 4096, 0 /* pseudo-random 1 to 4096 */ };
    char err[FMS_ERR_LEN];
    fms_player_t *p = fms_player_open((const char *const *)files, nf, err, sizeof err);
    fms_info_t in;
    int16_t *buf;
    uint64_t first = 0;
    uint32_t clip0 = 0;
    int bad = 0;
    size_t z;
    if (!p) { printf("FMS %s refused: %s\n", files[0], err); return 1; }
    fms_player_info(p, &in);
    buf = (int16_t *)malloc(sizeof(int16_t) * 4096 * FMS_MAX_CH);
    if (!buf) return 1;
    printf("FMS %s%s", files[0], nf > 1 ? " +" : "");
    if (nf > 1) printf("%d", nf - 1);
    for (z = 0; z < sizeof SIZES / sizeof SIZES[0]; z++) {
        uint32_t seed = 12345u, got, total = 0;
        fms_player_rewind(p);
        do {
            uint32_t want = SIZES[z] ? SIZES[z] : 1u + lcg(&seed) % 4096u;
            got = fms_player_render(p, buf, want);
            total += got;
        } while (got);
        if (!z) { first = fms_player_hash(p); clip0 = fms_player_clipped(p); }
        if (fms_player_hash(p) != first || fms_player_clipped(p) != clip0 || total != in.frames || fms_player_error(p)) bad = 1;
        if (SIZES[z]) printf(" c%u=%016llx", (unsigned)SIZES[z], (unsigned long long)fms_player_hash(p));
        else printf(" rand=%016llx", (unsigned long long)fms_player_hash(p));
    }
    /* Rewind from a sounding prefix as well as from the exhausted record. */
    fms_player_rewind(p);
    (void)fms_player_render(p, buf, 471);
    if (fms_player_render_all(p, 471, NULL) != first) bad = 1;
    printf(" hash=%016llx expected=", (unsigned long long)first);
    if (in.have_hash) printf("%016llx match=%s", (unsigned long long)in.hash_expected, in.hash_expected == first ? "yes" : "NO");
    else printf("- match=-");
    printf(" chunks_equal=%s clipped=%u frames=%u ch=%u\n", bad ? "NO" : "yes", clip0, in.frames, in.channels);
    free(buf);
    fms_player_close(p);
    return bad || (in.have_hash && in.hash_expected != first);
}

static const char TEST_PATCH_LINES[] =
    "patch bell 0 65536 140 0 0 256 2 900 120 250\n"
    "patch bell 1 131072 0 2048 0 128 1 600 200 200\n";
static const char TEST_FILTER_LINES[] =
    "patch bell 0 65536 140 0 0 256 2 900 120 250\n"
    "patch bell 1 131072 0 2048 0 128 1 600 200 200\n"
    "filter bell lp 1500000 707\n";

/* Start from a rendered prefix: export must restart and leave the private
 * player at frame zero on both success and writer refusal. */
static int verify_wav(const char *path, char **files, int nf)
{
    char err[FMS_ERR_LEN];
    int16_t prefix[17 * FMS_MAX_CH];
    fms_player_t *p = fms_player_open((const char *const *)files, nf, err, sizeof err);
    char *header;
    fms_player_t *live;
    size_t need;
    int rc;
    if (!p) { printf("WAV_WRITE refused: %s\n", err); return 1; }
    if (!fms_player_write_wav(NULL, path, err, sizeof err)) {
        fms_player_close(p); puts("WAV_WRITE null player accepted"); return 1;
    }
    need = fms_live_header_text(TEST_PATCH_LINES, "bell", 1, 48000, 0, NULL, NULL, 0);
    header = (char *)malloc(need);
    if (!header || fms_live_header_text(TEST_PATCH_LINES, "bell", 1, 48000, 0, NULL, header, need) != need) {
        free(header); fms_player_close(p); return 1;
    }
    live = fms_player_open_live(header, err, sizeof err);
    free(header);
    if (!live || !fms_player_write_wav(live, path, err, sizeof err)) {
        fms_player_close(live); fms_player_close(p); puts("WAV_WRITE live refusal failed"); return 1;
    }
    fms_player_close(live);
    (void)fms_player_render(p, prefix, 17);
    rc = fms_player_write_wav(p, path, err, sizeof err);
    if (fms_player_position(p) != 0 || fms_player_hash(p) != FNV_BASIS || fms_player_clipped(p)) {
        puts("WAV_WRITE reset failed"); rc = 1;
    } else if (rc) printf("WAV_WRITE refused: %s\n", err);
    else puts("WAV_WRITE reset=yes ok");
    fms_player_close(p);
    return rc;
}

/* Independent plain replay supplies every prefix and continuation. The
 * order is intentionally nonmonotonic, with control-grid neighbours/end. */
static int verify_seek(char **files, int nf)
{
    char err[FMS_ERR_LEN];
    fms_player_t *p = fms_player_open((const char *const *)files, nf, err, sizeof err);
    fms_player_t *plain = fms_player_open((const char *const *)files, nf, err, sizeof err);
    fms_info_t in;
    int16_t a[471 * FMS_MAX_CH], b[471 * FMS_MAX_CH];
    uint32_t positions[10], k;
    int bad = 0;
    if (!p || !plain) { printf("SEEK refused: %s\n", err); fms_player_close(p); fms_player_close(plain); return 1; }
    fms_player_info(p, &in);
    if (in.frames < 66 || in.frames == UINT32_MAX) { puts("SEEK fixture length"); return 1; }
    positions[0] = 0; positions[1] = 1; positions[2] = 63; positions[3] = 64;
    positions[4] = 65; positions[5] = in.frames / 3; positions[6] = in.frames / 2;
    positions[7] = in.frames - 1; positions[8] = in.frames; positions[9] = in.frames / 5;
    if (!fms_player_seek(NULL, 0, err, sizeof err)) bad = 1;
    for (k = 0; k < 10; k++) {
        const uint32_t target = positions[k];
        uint32_t done = 0;
        uint64_t prefix;
        fms_player_rewind(plain);
        while (done < target) {
            uint32_t want = target - done < 471 ? target - done : 471;
            if (fms_player_render(plain, b, want) != want) { bad = 1; break; }
            done += want;
        }
        prefix = fms_player_hash(plain);
        if (fms_player_seek(p, target, err, sizeof err) || fms_player_position(p) != target ||
            fms_player_hash(p) != prefix || fms_player_clipped(p) != fms_player_clipped(plain)) bad = 1;
        /* A refused request must preserve the complete continuation too. */
        if (!fms_player_seek(p, in.frames + 1u, err, sizeof err) || fms_player_position(p) != target ||
            fms_player_hash(p) != prefix) bad = 1;
        while (done < in.frames) {
            uint32_t want = in.frames - done < 471 ? in.frames - done : 471;
            if (fms_player_render(p, a, want) != want || fms_player_render(plain, b, want) != want ||
                memcmp(a, b, sizeof *a * want * in.channels)) { bad = 1; break; }
            done += want;
        }
        if (fms_player_error(p) || fms_player_hash(p) != fms_player_hash(plain) ||
            fms_player_clipped(p) != fms_player_clipped(plain) ||
            (in.have_hash && fms_player_hash(p) != in.hash_expected)) bad = 1;
    }
    /* The same public nil-handover boundary used by Engine. The first
     * paused callback publishes the prefix without consuming a sample. */
    {
        fms_host_t *host = fms_host_create();
        float samples[FMS_MAX_CH][64];
        float *out[FMS_MAX_CH];
        uint32_t c, i, target = in.frames / 2;
        uint64_t prefix;
        for (c = 0; c < FMS_MAX_CH; c++) out[c] = samples[c];
        fms_host_swap(host, p); fms_host_set_playing(host, 1);
        fms_host_render_f32(host, out, in.channels, 1);
        if (fms_host_swap(host, NULL) != p || fms_host_retired(host)) bad = 1;
        fms_host_render_f32(host, out, in.channels, 1);
        if (!fms_host_retired(host) || fms_player_seek(p, target, err, sizeof err)) bad = 1;
        prefix = fms_player_hash(p);
        fms_host_swap(host, p); fms_host_set_playing(host, 0);
        fms_host_render_f32(host, out, in.channels, 1);
        if (!fms_host_retired(host) || fms_host_position(host) != target || fms_host_hash(host) != prefix) bad = 1;
        fms_player_rewind(plain);
        for (i = 0; i < target;) {
            uint32_t want = target - i < 471 ? target - i : 471;
            if (fms_player_render(plain, b, want) != want) { bad = 1; break; }
            i += want;
        }
        if (fms_player_render(plain, b, 64) != 64) bad = 1;
        fms_host_set_playing(host, 1); fms_host_render_f32(host, out, in.channels, 64);
        for (i = 0; i < 64; i++) for (c = 0; c < in.channels; c++)
            if ((int16_t)(samples[c][i] * 32768.0f) != b[(size_t)i * in.channels + c]) bad = 1;
        if (fms_host_hash(host) != fms_player_hash(plain)) bad = 1;
        fms_host_swap(host, NULL); fms_host_settle(host); fms_host_destroy(host);
    }
    {
        size_t need = fms_live_header_text(TEST_PATCH_LINES, "bell", 1, 48000, 0, NULL, NULL, 0);
        char *header = (char *)malloc(need);
        fms_player_t *live = NULL;
        if (header && fms_live_header_text(TEST_PATCH_LINES, "bell", 1, 48000, 0, NULL, header, need) == need)
            live = fms_player_open_live(header, err, sizeof err);
        free(header);
        if (!live || !fms_player_seek(live, 0, err, sizeof err) || fms_player_position(live) ||
            fms_player_hash(live) != FNV_BASIS) bad = 1;
        fms_player_close(live);
    }
    fms_player_close(p); fms_player_close(plain);
    printf("SEEK %s positions=10 prefix=exact continuation=exact refusal=unchanged retirement=yes paused=yes playing=yes %s\n",
           files[0], bad ? "FAIL" : "ok");
    return bad;
}

static fms_event_t ev(uint32_t type, uint32_t voice, uint32_t f, uint32_t v)
{
    fms_event_t e;
    memset(&e, 0, sizeof e);
    e.type = type; e.voice = voice; e.freq_millihz = f; e.value = v;
    return e;
}

/* A request made before a handover follows the incoming live header. */
static int verify_live_swap(fms_host_t *h, const char *take, int stereo)
{
    char err[FMS_ERR_LEN], *hdr = NULL, *text = NULL, *path = NULL;
    size_t need;
    fms_player_t *incoming = NULL, *replay = NULL, *old;
    fms_info_t info;
    fms_event_t event = ev(FMS_EV_ON, 0, 261626, 26000);
    float left[64], right[64], *outs[2] = { left, right };
    uint64_t hash = FNV_BASIS;
    uint32_t i;
    FILE *file;
    int bad = 1;
    fms_space_t space; fms_space_defaults(&space); space.field = (uint32_t)stereo;
    need = fms_live_header_text(TEST_FILTER_LINES, "bell", 4, 48000, &space, NULL, NULL, 0);
    hdr = (char *)malloc(need);
    path = (char *)malloc(strlen(take) + sizeof ".swap.tl");
    if (!hdr || !path || !need) goto done;
    sprintf(path, "%s.swap.tl", take);
    if (fms_live_header_text(TEST_FILTER_LINES, "bell", 4, 48000, &space, NULL, hdr, need) != need) goto done;
    incoming = fms_player_open_live(hdr, err, sizeof err);
    if (!incoming) goto done;
    fms_host_record_start(h);
    old = fms_host_swap(h, incoming);
    if (fms_host_push(h, &event)) goto done;
    fms_host_render_f32(h, outs, stereo ? 2 : 1, 64);
    if (!fms_host_recording(h) || !fms_host_retired(h)) goto done;
    (void)old; /* the caller retains its original player until this check ends */
    for (i = 0; i < 64; i++) {
        hash = fnv(hash, (int16_t)(left[i] * 32768.0f));
        if (stereo) hash = fnv(hash, (int16_t)(right[i] * 32768.0f));
    }
    fms_host_record_stop_now(h);
    need = fms_host_take_text(h, NULL, 0);
    text = (char *)malloc(need ? need : 1);
    if (!need || !text || fms_host_take_text(h, text, need) != need || !strstr(text, "filter bell lp 1500000 707")) goto done;
    file = fopen(path, "w");
    if (!file || fputs(text, file) < 0 || fclose(file)) goto done;
    replay = fms_player_open((const char *const *)&path, 1, err, sizeof err);
    if (!replay) goto done;
    fms_player_info(replay, &info);
    bad = !info.have_hash || info.hash_expected != hash || info.frames != 64 ||
          fms_player_render_all(replay, 17, NULL) != hash;
done:
    fms_host_record_stop_now(h);
    fms_host_swap(h, NULL);
    fms_host_settle(h);
    fms_player_close(incoming); fms_player_close(replay);
    free(hdr); free(text); free(path);
    printf("LIVE swap %s\n", bad ? "FAIL" : "ok");
    return bad;
}

/* A live take driven through fms_host_render_f32 at uneven callback sizes. */
static int verify_live(const char *take, int stereo)
{
    static const uint32_t CB[] = { 37, 256, 511, 1, 4096, 1000, 128, 700 };
    char err[FMS_ERR_LEN], *hdr, *txt;
    size_t need;
    fms_player_t *p, *r;
    fms_host_t *h = fms_host_create();
    float *outs[2];
    uint64_t fh = FNV_BASIS, rh;
    uint32_t k, i, total = 0, nout = stereo ? 2 : 1;
    FILE *f;
    int bad = 0;
    fms_space_t space; fms_space_defaults(&space); space.field = (uint32_t)stereo;
    need = fms_live_header_text(TEST_PATCH_LINES, "bell", 4, 48000, &space, stereo ? NULL : "reverb 400 300\n", NULL, 0);
    hdr = (char *)malloc(need);
    if (!h || !hdr || fms_live_header_text(TEST_PATCH_LINES, "bell", 4, 48000, &space, stereo ? NULL : "reverb 400 300\n", hdr, need) != need) return 1;
    p = fms_player_open_live(hdr, err, sizeof err);
    if (!p) { printf("LIVE refused: %s\n--\n%s--\n", err, hdr); return 1; }
    outs[0] = (float *)calloc(4096, sizeof(float));
    outs[1] = (float *)calloc(4096, sizeof(float));
    fms_host_swap(h, p);
    fms_host_set_playing(h, 1);
    fms_host_render_f32(h, outs, nout, 300);            /* before the take: rewound by the start */
    fms_host_record_start(h);
    for (k = 0; total < 48000 * 2; k++) {
        const uint32_t n = CB[k % (sizeof CB / sizeof CB[0])];
        fms_event_t e;
        switch (k) {                                     /* the script: events between callbacks */
        case 0:  e = ev(FMS_EV_ON, 0, 261626, 26000); fms_host_push(h, &e); break;
        case 3:  e = ev(FMS_EV_ON, 1, 329628, 20000); fms_host_push(h, &e);
                 e = ev(FMS_EV_ON, 2, 391995, 30000); fms_host_push(h, &e); break;
        case 6:  e = ev(FMS_EV_PITCH, 0, 277183, 0); fms_host_push(h, &e); break;
        case 9:  e = ev(FMS_EV_OFF, 1, 0, 0); fms_host_push(h, &e); break;
        case 12: e = ev(FMS_EV_ON, 1, 440000, 32767); fms_host_push(h, &e);
                 e = ev(FMS_EV_SEND, 1, 0, 9000); e.bus = 2; fms_host_push(h, &e); break;
        case 20: e = ev(FMS_EV_OFF, 0, 0, 0); fms_host_push(h, &e);
                 e = ev(FMS_EV_OFF, 2, 0, 0); fms_host_push(h, &e); break;
        case 25: e = ev(FMS_EV_ON, 3, 196000, 25000); fms_host_push(h, &e); break;
        case 30: e = ev(FMS_EV_OFF, 1, 0, 0); fms_host_push(h, &e); break;
        default: break;
        }
        if (stereo && k % 5 == 2) {                      /* place and move voices */
            e = ev(k % 10 == 2 ? FMS_EV_POS : FMS_EV_MOVE, k % 4, 0, 0);
            e.x = (int32_t)(k * 97 % 2001) - 1000; e.y = 1000; e.z = 0; e.dur_ns = 150000000;
            fms_host_push(h, &e);
        }
        fms_host_render_f32(h, outs, nout, n);
        for (i = 0; i < n; i++) {                        /* the floats back to int16: unity gain is exact */
            fh = fnv(fh, (int16_t)(outs[0][i] * 32768.0f));
            if (stereo) fh = fnv(fh, (int16_t)(outs[1][i] * 32768.0f));
        }
        total += n;
    }
    fms_host_record_stop_now(h); /* offline: no audio callback runs */
    need = fms_host_take_text(h, NULL, 0);
    txt = (char *)malloc(need ? need : 1);
    if (!need || !txt || fms_host_take_text(h, txt, need) != need) { printf("LIVE no take\n"); return 1; }
    f = fopen(take, "w");
    if (!f || fputs(txt, f) < 0 || fclose(f)) { printf("LIVE cannot write %s\n", take); return 1; }
    r = fms_player_open((const char *const *)&take, 1, err, sizeof err);
    if (!r) { printf("LIVE take refused: %s\n", err); return 1; }
    rh = fms_player_render_all(r, 471, NULL);
    {
        fms_info_t in;
        fms_player_info(r, &in);
        bad = !in.have_hash || in.hash_expected != rh || fms_host_hash(h) != rh || fh != rh || in.frames != total;
        printf("LIVE %s %s take=%016llx host=%016llx f32=%016llx replay=%016llx frames=%u events=%u match=%s\n",
               stereo == FMS_SPACE_BINAURAL ? "binaural" : stereo ? "stereo" : "mono", take, (unsigned long long)in.hash_expected, (unsigned long long)fms_host_hash(h),
               (unsigned long long)fh, (unsigned long long)rh, in.frames, in.nevents, bad ? "NO" : "yes");
    }
    fms_player_close(r);
    bad |= verify_live_swap(h, take, stereo);
    fms_player_close(p);
    fms_host_destroy(h);
    free(outs[0]); free(outs[1]); free(hdr); free(txt);
    return bad;
}

/* : field metadata and live fixtures use the public bridge only. */
static char *space_header(const char *patch, const fms_space_t *space, const char *fx, uint32_t fs)
{
    size_t n = fms_live_header_text(patch, "tone", 1, fs, space, fx, NULL, 0);
    char *s = n ? (char *)malloc(n) : NULL;
    if (s && fms_live_header_text(patch, "tone", 1, fs, space, fx, s, n) != n) { free(s); s = NULL; }
    return s;
}

static int space_equal(const fms_space_t *a, const fms_space_t *b)
{
    unsigned i;
    if (a->field != b->field || a->block != b->block || a->near_mm != b->near_mm || a->present != b->present ||
        a->order != b->order || a->panlaw_q15 != b->panlaw_q15 || a->pinna != b->pinna || a->yaw_mdeg != b->yaw_mdeg) return 0;
    for (i = 0; i < 5; i++) if (a->pinna_d[i] != b->pinna_d[i]) return 0;
    return 1;
}

static int space_case(const fms_space_t *space, const char *fx, const char *refusal, unsigned *checks)
{
    char err[FMS_ERR_LEN], *text = space_header(TEST_PATCH_LINES, space, fx, 48000), *back = NULL;
    fms_player_t *p = text ? fms_player_open_live(text, err, sizeof err) : NULL;
    fms_space_t actual;
    int bad;
    (*checks)++;
    if (refusal) bad = !text || p || !strstr(err, refusal);
    else {
        size_t need = p ? fms_player_fx_text(p, NULL, 0) : 0;
        char tiny[2] = { 'x', 'y' };
        back = need ? (char *)malloc(need) : NULL;
        fms_player_space(p, &actual);
        bad = !p || !space_equal(space, &actual) || !back ||
              fms_player_fx_text(p, back, need) != need || strcmp(back, fx ? fx : "");
        if (need > sizeof tiny && (fms_player_fx_text(p, tiny, sizeof tiny) != need || tiny[0] != 'x' || tiny[1] != 'y')) bad = 1;
    }
    if (bad) fprintf(stderr, "SPACE case %u failed field=%u expected=%s diagnostic=%s\n", *checks, space->field,
                     refusal ? refusal : "accepted exact read-back", p ? "accepted" : text ? err : "writer refused");
    free(back); free(text); fms_player_close(p);
    return bad;
}

static int verify_space_bounds(void)
{
    static const uint32_t blocks[] = { 32, 64, 128 }, nears[] = { 1, 100, 16777216 };
    static const char *const fx[] = { "dcblock 5000\n", "chorus 15000 2500 800\n", "delay 300000 9830 6000\n",
        "reverb 500 300\n", "limiter 10 200\n", "softclip\n", "gain -120\n", "dither 4294967295\n", "crush\n" };
    fms_space_t s, saved, q;
    unsigned field, b, n, k, checks = 0;
    int bad = 0;
    fms_space_defaults(NULL); fms_player_space(NULL, NULL); fms_player_space(NULL, &q);
    fms_space_defaults(&s);
    if (!space_equal(&s, &q) || fms_player_fx_text(NULL, NULL, 0)) bad = 1;
    for (field = FMS_SPACE_OFF; field <= FMS_SPACE_BINAURAL; field++)
        for (b = 0; b < 3; b++) for (n = 0; n < 3; n++) {
            fms_space_defaults(&s); s.field = field; s.block = blocks[b]; s.near_mm = nears[n];
            bad |= space_case(&s, NULL, NULL, &checks);
        }
    for (field = FMS_SPACE_OFF; field <= FMS_SPACE_BINAURAL; field++) {
        fms_space_defaults(&s); s.field = field;
        for (k = 0; k < sizeof fx / sizeof *fx; k++) bad |= space_case(&s, fx[k], NULL, &checks);
    }
    fms_space_defaults(&s); s.field = FMS_SPACE_STEREO;
    bad |= space_case(&s, "pingpong 375000 19661 3000 120\n", NULL, &checks);
    for (k = 0; k < 2; k++) {
        s.present = FMS_SPACE_PANLAW; s.panlaw_q15 = k ? 32768 : 1;
        bad |= space_case(&s, NULL, NULL, &checks);
    }
    bad |= space_case(&s, "gain 0\n", "effects need space off", &checks);
    fms_space_defaults(&s); s.field = FMS_SPACE_BINAURAL;
    bad |= space_case(&s, "gain 0\n", NULL, &checks);
    bad |= space_case(&s, "pingpong 375000 19661 3000 120\n", NULL, &checks);
    for (k = 0; k < 4; k++) {
        s.present = FMS_SPACE_ORDER | FMS_SPACE_PINNA | FMS_SPACE_YAW;
        s.order = k & 1 ? 1 : 3; s.pinna = k; s.yaw_mdeg = k & 1 ? -360000 : 360000;
        for (n = 0; n < 5; n++) s.pinna_d[n] = k == 3 ? n * 250 : 0;
        bad |= space_case(&s, NULL, NULL, &checks);
    }
    saved = s;
    s.pinna_d[4] = 1001; bad |= space_case(&s, NULL, "not 0 to 1000", &checks); s = saved;
    s.yaw_mdeg = 360001; bad |= space_case(&s, NULL, "yaw -: MDEG '360001' is not -360000 to 360000", &checks); s = saved;
    s.yaw_mdeg = -360001; bad |= space_case(&s, NULL, "yaw -: MDEG '-360001' is not -360000 to 360000", &checks); s = saved;
    for (k = 0; k < 5; k += 2) { s.order = k;
        bad |= space_case(&s, NULL, k == 2 ? "order takes" :
                          k == 0 ? "order -: ORDER '0' is not 1 to 3" : "order -: ORDER '4' is not 1 to 3", &checks); }
    fms_space_defaults(&s);
    s.block = 1; bad |= space_case(&s, NULL, "block -: FRAMES '1' is not 32 to 128", &checks); s.block = 64;
    s.near_mm = 0; bad |= space_case(&s, NULL, "near -: MM '0' is not 1 to 16777216", &checks);
    s.near_mm = 16777217; bad |= space_case(&s, NULL, "near -: MM '16777217' is not 1 to 16777216", &checks); s.near_mm = 100;
    s.present = FMS_SPACE_ORDER; bad |= space_case(&s, NULL, "order needs", &checks);
    s.present = FMS_SPACE_PINNA; bad |= space_case(&s, NULL, "pinna and yaw need", &checks);
    s.present = FMS_SPACE_YAW; bad |= space_case(&s, NULL, "pinna and yaw need", &checks);
    s.present = FMS_SPACE_PANLAW; s.panlaw_q15 = 1; bad |= space_case(&s, NULL, "panlaw needs", &checks);
    s.field = FMS_SPACE_STEREO; s.panlaw_q15 = 0; bad |= space_case(&s, NULL, "panlaw -: WIDTH_Q15 '0' is not 1 to 32768", &checks);
    s.panlaw_q15 = 32769; bad |= space_case(&s, NULL, "panlaw -: WIDTH_Q15 '32769' is not 1 to 32768", &checks);
    for (field = FMS_SPACE_AMB1; field <= FMS_SPACE_AMB3; field++) {
        fms_space_defaults(&s); s.field = field;
        bad |= space_case(&s, NULL, "ambisonic records play in Records", &checks);
    }
    fms_space_defaults(&s); s.field = 5;
    if (fms_live_header_text(TEST_PATCH_LINES, "tone", 1, 48000, &s, NULL, NULL, 0)) bad = 1;
    s.field = 0; s.present = 16;
    if (fms_live_header_text(TEST_PATCH_LINES, "tone", 1, 48000, &s, NULL, NULL, 0)) bad = 1;
    s.present = FMS_SPACE_PINNA; s.pinna = 4;
    if (fms_live_header_text(TEST_PATCH_LINES, "tone", 1, 48000, &s, NULL, NULL, 0)) bad = 1;
    checks += 4;
    printf("SPACE header checks=%u ranges=exact optional_presence=yes fx=exact ambisonic=refused %s\n", checks, bad ? "FAIL" : "ok");
    return bad;
}

static int space_take(fms_host_t *h, const char *path, uint64_t expected, const char *kind)
{
    size_t n;
    char *text, err[FMS_ERR_LEN];
    fms_player_t *r = NULL;
    fms_info_t info;
    FILE *f;
    int bad = 1;
    fms_host_record_stop_now(h); /* Offline: this test owns the only callback. */
    n = fms_host_take_text(h, NULL, 0); text = n ? (char *)malloc(n) : NULL;
    if (!text || fms_host_take_text(h, text, n) != n) goto done;
    f = fopen(path, "w");
    if (!f) goto done;
    if (fputs(text, f) < 0) { fclose(f); goto done; }
    if (fclose(f)) goto done;
    r = fms_player_open(&path, 1, err, sizeof err);
    if (!r) goto done;
    fms_player_info(r, &info);
    bad = !info.have_hash || info.hash_expected != expected || fms_player_render_all(r, 37, NULL) != expected;
    printf("LIVE %s take=%016llx replay=%s frames=%u\n", kind, (unsigned long long)expected, bad ? "NO" : "yes", info.frames);
done:
    if (bad) fprintf(stderr, "LIVE %s take failed\n", kind);
    free(text); fms_player_close(r); return bad;
}

/* No choreography is extracted. These manually specified first events are
 * the independent prefix in the two committed binaural golden schedules. */
static int verify_live_values(const char *path, const char *take, int third, const char *drop)
{
    fms_player_t *plain = NULL, *live = NULL;
    fms_host_t *h = fms_host_create();
    fms_space_t space;
    fms_info_t info;
    char err[FMS_ERR_LEN], *patch = NULL, *header = NULL;
    size_t n;
    float l[64], r[64], *outs[] = { l, r };
    int16_t reference[128];
    uint64_t prefix = FNV_BASIS, first = 0;
    uint32_t frame, i, nonzero = 0, first_nonzero = 0;
    int bad = 1;
    fms_event_t e;
    plain = fms_player_open(&path, 1, err, sizeof err);
    if (!plain || !h) goto done;
    fms_player_info(plain, &info); fms_player_space(plain, &space);
    if (space.field != FMS_SPACE_BINAURAL || !info.have_hash ||
        fms_player_render_all(plain, 471, NULL) != info.hash_expected) goto done;
    fms_player_rewind(plain);
    if (drop && !strcmp(drop, "order")) space.present &= ~FMS_SPACE_ORDER;
    if (drop && !strcmp(drop, "yaw")) space.present &= ~FMS_SPACE_YAW;
    if (drop && !strcmp(drop, "pinna")) space.present &= ~FMS_SPACE_PINNA;
    n = fms_player_patch_lines(plain, 0, NULL, 0); patch = n ? (char *)malloc(n) : NULL;
    if (!patch || fms_player_patch_lines(plain, 0, patch, n) != n) goto done;
    header = space_header(patch, &space, NULL, info.fs);
    live = header ? fms_player_open_live(header, err, sizeof err) : NULL;
    if (!live) goto done;
    fms_host_swap(h, live); fms_host_set_playing(h, 1); fms_host_record_start(h);
    e = ev(FMS_EV_ON, 0, 440000, 16000); if (fms_host_push(h, &e)) goto done;
    e = ev(FMS_EV_POS, 0, 0, 0); e.x = third ? 1000 : 0; e.y = 1000;
    if (fms_host_push(h, &e)) goto done;
    bad = 0;
    for (frame = 0; frame < 4096; frame += 64) {
        if (fms_player_render(plain, reference, 64) != 64) { bad = 1; break; }
        fms_host_render_f32(h, outs, 2, 64);
        for (i = 0; i < 64; i++) {
            int16_t a = (int16_t)(l[i] * 32768.0f), b = (int16_t)(r[i] * 32768.0f);
            if (a != reference[i * 2] || b != reference[i * 2 + 1]) bad = 1;
            prefix = fnv(fnv(prefix, reference[i * 2]), reference[i * 2 + 1]);
            nonzero += reference[i * 2] != 0 || reference[i * 2 + 1] != 0;
        }
        if (!frame) { first_nonzero = nonzero; first = prefix; if (fms_host_hash(h) != first) bad = 1; }
    }
    if (!nonzero || prefix != fms_host_hash(h)) bad = 1;
    printf("LIVE values fs=%u order=%u first64=%016llx first_nonzero=%u prefix4096=%016llx nonzero=%u pcm=%s%s%s\n", info.fs, space.order,
           (unsigned long long)first, first_nonzero, (unsigned long long)prefix, nonzero, bad ? "NO" : "yes", drop ? " dropped=" : "", drop ? drop : "");
    if (!bad) bad |= space_take(h, take, prefix, "values");
done:
    if (bad) fprintf(stderr, "LIVE values prefix comparison failed%s%s\n", drop ? " dropped=" : "", drop ? drop : "");
    if (h) { fms_host_record_stop_now(h); fms_host_swap(h, NULL); fms_host_settle(h); }
    fms_host_destroy(h); fms_player_close(plain); fms_player_close(live); free(patch); free(header);
    return bad;
}

static int verify_live_move(const char *path)
{
    fms_space_t space;
    char err[FMS_ERR_LEN], *header;
    fms_host_t *h = fms_host_create();
    fms_player_t *p;
    fms_event_t e;
    float l[64], r[64], *outs[] = { l, r };
    int32_t xyz[3];
    int bad = 0;
    fms_space_defaults(&space); space.field = FMS_SPACE_STEREO;
    header = space_header(TEST_PATCH_LINES, &space, NULL, 48000);
    p = header ? fms_player_open_live(header, err, sizeof err) : NULL;
    if (!h || !p) { free(header); fms_player_close(p); fms_host_destroy(h); return 1; }
    fms_host_swap(h, p); fms_host_set_playing(h, 1); fms_host_record_start(h);
    e = ev(FMS_EV_ON, 0, 261626, 16000); bad |= fms_host_push(h, &e);
    e = ev(FMS_EV_POS, 0, 0, 0); e.x = -1000; e.y = 1000; bad |= fms_host_push(h, &e);
    fms_host_render_f32(h, outs, 2, 64);
    e.type = FMS_EV_MOVE; e.x = 1000; e.y = -1000; e.z = 500; e.dur_ns = 2666667;
    bad |= fms_host_push(h, &e);
    fms_host_render_f32(h, outs, 2, 64); fms_host_voice_pos(h, xyz, 1);
    if (xyz[0] != 0 || xyz[1] != 0 || xyz[2] != 250) bad = 1;
    fms_host_render_f32(h, outs, 2, 64); fms_host_voice_pos(h, xyz, 1);
    if (xyz[0] != 1000 || xyz[1] != -1000 || xyz[2] != 500) bad = 1;
    /* Include audible attack/continuation, not only the interpolation metadata. */
    { unsigned i; for (i = 0; i < 61; i++) fms_host_render_f32(h, outs, 2, 64); }
    bad |= space_take(h, path, fms_host_hash(h), "move");
    printf("LIVE move blocks=2 xyz=exact %s\n", bad ? "FAIL" : "ok");
    fms_host_swap(h, NULL); fms_host_settle(h); fms_host_destroy(h); fms_player_close(p); free(header);
    return bad;
}

/* One MIDI message as a UMP: a channel message as MT 2, a system one as
 * MT 1, SysEx (F0 ... F7) as one MT 3 packet of its first 6 data bytes; 0
 * words when empty. */
static uint32_t ump_of(const uint8_t *b, uint32_t n, uint32_t *w)
{
    uint32_t i;
    if (!n) return 0;
    if (b[0] == 0xF0) {
        const uint32_t d = n - 1 - (n > 1 && b[n - 1] == 0xF7);
        uint32_t k = d > 6 ? 6 : d, x[6] = { 0 };
        for (i = 0; i < k; i++) x[i] = b[1 + i] & 0x7Fu;
        w[0] = 0x30000000u | k << 16 | x[0] << 8 | x[1];
        w[1] = x[2] << 24 | x[3] << 16 | x[4] << 8 | x[5];
        return 2;
    }
    w[0] = (b[0] >= 0xF0 ? 0x10000000u : 0x20000000u) | (uint32_t)b[0] << 16 |
           (n > 1 ? (uint32_t)b[1] : 0u) << 8 | (n > 2 ? (uint32_t)b[2] : 0u);
    return 1;
}

static const char *const KIND[] = { "?", "on", "off", "pitch", "pos", "move", "filter", "send", "yaw", "other", "ctl" };

static void midi_stats_line(const fms_midi_t *m)
{
    fms_midi_stats_t s;
    fms_midi_stats(m, &s);
    printf("stats messages=%llu notes=%llu note_offs=%llu orphan_offs=%llu restruck=%llu pedal_downs=%llu "
           "pedal_held=%llu steals=%llu release_steals=%llu bends=%llu pitch_events=%llu cc74=%llu "
           "filter_events=%llu rpn_selects=%llu rpn_ranges=%llu programs=%llu aftertouch=%llu pressure=%llu "
           "other_ccs=%llu other_channels=%llu system=%llu unknown=%llu clamped=%llu near_ties=%llu refused=%llu ctl_messages=%llu ctl_events=%llu mpe_notes=%llu mpe_pressure=%llu mpe_slides=%llu mcm=%llu\n",
           (unsigned long long)s.messages, (unsigned long long)s.notes, (unsigned long long)s.note_offs,
           (unsigned long long)s.orphan_offs, (unsigned long long)s.restruck, (unsigned long long)s.pedal_downs,
           (unsigned long long)s.pedal_held, (unsigned long long)s.steals, (unsigned long long)s.release_steals,
           (unsigned long long)s.bends, (unsigned long long)s.pitch_events, (unsigned long long)s.cc74,
           (unsigned long long)s.filter_events, (unsigned long long)s.rpn_selects, (unsigned long long)s.rpn_ranges,
           (unsigned long long)s.programs, (unsigned long long)s.aftertouch, (unsigned long long)s.pressure,
           (unsigned long long)s.other_ccs, (unsigned long long)s.other_channels, (unsigned long long)s.system,
           (unsigned long long)s.unknown, (unsigned long long)s.clamped, (unsigned long long)s.near_ties,
           (unsigned long long)s.refused, (unsigned long long)s.ctl_messages, (unsigned long long)s.ctl_events,
           (unsigned long long)s.mpe_notes, (unsigned long long)s.mpe_pressure,
           (unsigned long long)s.mpe_slides, (unsigned long long)s.mcm);
}

/* The translator on a file of messages: each event as "T_NS KIND VOICE FREQ VALUE". */
static int verify_midi(const char *path)
{
    FILE *f = fopen(path, "r");
    char line[512];
    fms_midi_config_t c;
    fms_midi_t *m = NULL;
    fms_event_t out[FMS_MIDI_OUT];
    unsigned nv, rel, bend, rpn, cut, q, chans, reads = 0;
    if (!f) { fprintf(stderr, "MIDI cannot read %s\n", path); return 1; }
    if (!fgets(line, sizeof line, f) ||
        sscanf(line, "config %u %u %u %u %u %u %x %x", &nv, &rel, &bend, &rpn, &cut, &q, &chans, &reads) < 7) {
        fprintf(stderr, "MIDI %s: no config line\n", path); fclose(f); return 1;
    }
    memset(&c, 0, sizeof c);
    c.nvoices = nv; c.release_ms = rel; c.bend_cents = bend; c.rpn = rpn;
    c.cutoff_millihz = cut; c.q_milli = q; c.channels = chans; c.ctl_reads = reads;
    m = fms_midi_create(&c);
    if (!m) { fprintf(stderr, "MIDI %s: config refused\n", path); fclose(f); return 1; }
    while (fgets(line, sizeof line, f)) {
        unsigned long long t;
        uint8_t b[64];
        uint32_t nb = 0, w[4], nw, at = 0, used, ne, i;
        char *s = line, *end;
        if (!strncmp(line, "mpe ", 4)) {
            char err[160];
            if (fms_midi_set_mpe(m, line + 4, err, sizeof err)) {
                fprintf(stderr, "MIDI %s: mpe setup refused\n", path);
                fclose(f); fms_midi_destroy(m); return 1;
            }
            continue;
        }
        if (!strncmp(line, "ctl ", 4)) {
            unsigned channel; char spec[480], err[160];
            if (sscanf(line, "ctl %u %479[^\n]", &channel, spec) != 2 || channel < 1 || channel > 16 ||
                fms_midi_set_ctl(m, channel - 1, spec, err, sizeof err)) {
                fprintf(stderr, "MIDI %s: ctl setup refused\n", path);
                fclose(f); fms_midi_destroy(m); return 1;
            }
            continue;
        }
        t = strtoull(s, &end, 10);
        if (end == s) continue;
        s = end;
        for (;;) {
            unsigned long x = strtoul(s, &end, 16);
            if (end == s || nb == sizeof b) break;
            b[nb++] = (uint8_t)x;
            s = end;
        }
        nw = ump_of(b, nb, w);
        while (at < nw) {
            ne = fms_midi_ump(m, t, w + at, nw - at, out, FMS_MIDI_OUT, &used);
            for (i = 0; i < ne; i++) {
                if (out[i].type == FMS_EV_CTL)
                    printf("%llu ctl %u %u %d\n", t, out[i].voice, out[i].bus, out[i].x);
                else printf("%llu %s %u %u %u\n", t, KIND[out[i].type < 11 ? out[i].type : 0], out[i].voice,
                            out[i].freq_millihz, out[i].value);
            }
            at += used ? used : nw;
        }
    }
    fclose(f);
    midi_stats_line(m);
    fms_midi_destroy(m);
    return 0;
}

/* A MIDI stream through the translator's entry point under a live player, as
 * the app feeds it, taken and replayed: the same hash . */
static int verify_midi_live(const char *take)
{
    static const uint32_t CB[] = { 37, 256, 511, 1, 4096, 1000, 128, 700 };
    /* callback index, then the message (3 bytes; a 0 status ends the list) */
    static const uint8_t SCRIPT[][4] = {
        { 0, 0x90, 60, 100 }, { 0, 0x90, 64, 90 },          /* two notes at one frame */
        { 2, 0xE0, 0x00, 0x50 }, { 3, 0xB0, 74, 90 },        /* bend up, CC 74 up */
        { 4, 0x90, 67, 127 }, { 4, 0xF8, 0, 0 },             /* a third note; MIDI clock, ignored */
        { 5, 0xB0, 64, 127 }, { 6, 0x80, 60, 0 },            /* pedal down; 60 up, held */
        { 7, 0x90, 72, 1 }, { 8, 0x90, 76, 64 },             /* the fifth note steals */
        { 9, 0xC0, 5, 0 }, { 9, 0xB0, 1, 70 },               /* a program and CC 1, ignored */
        { 10, 0x90, 60, 80 },                                /* 60 again under the pedal */
        { 12, 0xB0, 101, 0 }, { 12, 0xB0, 100, 0 }, { 12, 0xB0, 6, 12 },   /* RPN 0: 12 semitones */
        { 13, 0xE0, 0x7F, 0x7F }, { 14, 0xB0, 64, 0 },       /* bend to the top; pedal up */
        { 16, 0x90, 64, 0 }, { 17, 0xB0, 74, 30 },           /* 64 off by velocity 0; CC 74 down */
        { 19, 0xE0, 0x00, 0x40 }, { 20, 0x80, 67, 0 },       /* bend home; 67 up */
        { 22, 0x80, 72, 0 }, { 22, 0x80, 76, 0 }, { 23, 0x80, 60, 0 },
        { 25, 0x91, 48, 110 }, { 30, 0x81, 48, 0 },          /* channel 2 */
        { 0, 0, 0, 0 }
    };
    fms_midi_config_t c;
    fms_midi_t *m;
    fms_event_t out[FMS_MIDI_OUT];
    char err[FMS_ERR_LEN], *hdr, *txt;
    size_t need;
    fms_player_t *p, *r;
    fms_host_t *h = fms_host_create();
    float *outs[1];
    uint64_t fh = FNV_BASIS, rh;
    uint32_t k, i, s = 0, total = 0, made = 0, pushed = 0;
    FILE *f;
    int bad = 0;
    memset(&c, 0, sizeof c);
    c.nvoices = 4; c.channels = 0xFFFF; c.release_ms = fms_midi_release_ms(TEST_FILTER_LINES, 48000); c.bend_cents = 200; c.rpn = 1;
    c.cutoff_millihz = 1500000; c.q_milli = 707;
    bad |= c.release_ms != 250;
    m = fms_midi_create(&c);
    need = fms_live_header_text(TEST_FILTER_LINES, "bell", 4, 48000, 0, NULL, NULL, 0);
    hdr = (char *)malloc(need);
    if (!h || !m || !hdr || fms_live_header_text(TEST_FILTER_LINES, "bell", 4, 48000, 0, NULL, hdr, need) != need) return 1;
    p = fms_player_open_live(hdr, err, sizeof err);
    if (!p) { printf("MIDILIVE refused: %s\n", err); return 1; }
    outs[0] = (float *)calloc(4096, sizeof(float));
    if (!outs[0]) return 1;
    fms_host_swap(h, p);
    fms_host_set_playing(h, 1);
    fms_host_record_start(h);
    for (k = 0; total < 48000 * 2; k++) {
        const uint32_t n = CB[k % (sizeof CB / sizeof CB[0])];
        const uint64_t t = (uint64_t)total * 1000000000u / 48000u;
        for (; SCRIPT[s][1] && SCRIPT[s][0] == k; s++) {
            uint32_t w[2], used, ne;
            (void)ump_of(&SCRIPT[s][1], 3, w);
            ne = fms_midi_ump(m, t, w, 1, out, FMS_MIDI_OUT, &used);
            made += ne;
            for (i = 0; i < ne; i++) pushed += fms_host_push(h, &out[i]) == 0;
        }
        if (k == 11) {                                   /* the keyboard takes voice 2, then lets go */
            fms_event_t e = ev(FMS_EV_ON, 2, 523251, 20000);
            fms_midi_external(m, t, 2, 1);
            made++; pushed += fms_host_push(h, &e) == 0;
        } else if (k == 18) {
            fms_event_t e = ev(FMS_EV_OFF, 2, 0, 0);
            fms_midi_external(m, t, 2, 0);
            made++; pushed += fms_host_push(h, &e) == 0;
        }
        fms_host_render_f32(h, outs, 1, n);
        for (i = 0; i < n; i++) fh = fnv(fh, (int16_t)(outs[0][i] * 32768.0f));
        total += n;
    }
    fms_host_record_stop_now(h); /* offline: no audio callback runs */
    need = fms_host_take_text(h, NULL, 0);
    txt = (char *)malloc(need ? need : 1);
    if (!need || !txt || fms_host_take_text(h, txt, need) != need) { printf("MIDILIVE no take\n"); return 1; }
    f = fopen(take, "w");
    if (!f || fputs(txt, f) < 0 || fclose(f)) { printf("MIDILIVE cannot write %s\n", take); return 1; }
    r = fms_player_open((const char *const *)&take, 1, err, sizeof err);
    if (!r) { printf("MIDILIVE take refused: %s\n", err); return 1; }
    rh = fms_player_render_all(r, 471, NULL);
    {
        fms_info_t in;
        fms_midi_stats_t st;
        fms_player_info(r, &in);
        fms_midi_stats(m, &st);
        bad = !in.have_hash || in.hash_expected != rh || fms_host_hash(h) != rh || fh != rh || in.frames != total ||
              in.nevents != made || pushed != made || SCRIPT[s][1] || !st.steals || !st.restruck || !st.pitch_events ||
              !st.filter_events || !st.rpn_ranges || st.system != 1 || st.programs != 1 || st.refused;
        printf("MIDILIVE %s take=%016llx host=%016llx f32=%016llx replay=%016llx frames=%u events=%u made=%u "
               "steals=%llu restruck=%llu pitch=%llu filter=%llu match=%s\n",
               take, (unsigned long long)in.hash_expected, (unsigned long long)fms_host_hash(h), (unsigned long long)fh,
               (unsigned long long)rh, in.frames, in.nevents, made, (unsigned long long)st.steals,
               (unsigned long long)st.restruck, (unsigned long long)st.pitch_events,
               (unsigned long long)st.filter_events, bad ? "NO" : "yes");
    }
    fms_player_close(r);
    fms_host_swap(h, NULL);
    fms_player_close(p);
    fms_host_destroy(h);
    fms_midi_destroy(m);
    free(outs[0]); free(hdr); free(txt);
    return bad;
}

/* Short lifecycle fixtures exercise the persistent bank and its errors.
 * The files also replay through the native harness in tools/verify.sh. */
static int gen_file(const char *path, int failure, int stereo)
{
    char engine[160];
    FILE *f = fopen(path, "w");
    if (!f) return 1;
    fms_engine_line(engine, sizeof engine);
    fprintf(f, "timeline draft2\nengine %s\nfs 48000\nlength_ns 500000000\nstreams test\ngen_clock 64\n", engine);
    if (stereo) fputs("space stereo\nblock 64\nnear 1000\nreverb 400 300\n", f);
    fputs("gen 0 euclid 1 1 0 3 8 0 -\n"
          "patch tail 0 65536 180 0 0 0 1 1 0 100\n"
          "patch tail 1 65536 0 0 0 0 1 1 0 100\n"
          "mod tail 0 gen.0.x pitch0 1200 3\n"
          "voice 0 tail\n", f);
    if (failure) fputs("0 test 0 on 440000 20000\n1333334 test gen 0 n 2\n", f);
    else {
        fputs("gen_lane 0 0 - - - 100 6900 0 20000 1 0\n1333334 test lane 0 enable 0\n", f);
        if (stereo) fputs("0 test 0 pos -300 1000 0\n0 test 0 send reverb 12000\n", f);
    }
    return fclose(f) != 0;
}

static int verify_gen(const char *prefix)
{
    char path[1024], err[FMS_ERR_LEN], *one;
    fms_player_t *p;
    int16_t buf[512 * FMS_MAX_CH], first[64];
    uint32_t n, total = 0, tail = 0, end = 0, status;
    int bad = 0;
    snprintf(path, sizeof path, "%s-tail.tl", prefix); one = path;
    if (gen_file(path, 0, 0) || verify_pool(&one, 1)) return 1;
    p = fms_player_open((const char *const *)&one, 1, err, sizeof err);
    if (!p) return 1;
    {
        size_t need = fms_player_patch_lines(p, 0, NULL, 0);
        char *lines = need ? (char *)malloc(need) : NULL;
        bad |= !lines || fms_player_patch_lines(p, 0, lines, need) != need;
        if (lines) bad |= !strstr(lines, "gen_clock ") || !strstr(lines, "gen.") ||
            fms_patch_lines_check(lines, need - 1, 48000, err, sizeof err);
        free(lines);
    }
    while ((n = fms_player_render(p, buf, 512))) {
        uint32_t i;
        for (i = 0; i < n; i++) {
            if (total + i >= 64 && total + i < 4096 && buf[i]) tail++;
            if (total + i >= 12000 && buf[i]) end++;
        }
        total += n;
    }
    bad |= total != 24000 || !tail || end || fms_player_error(p);
    fms_player_close(p);
    snprintf(path, sizeof path, "%s-stereo.tl", prefix);
    if (gen_file(path, 0, 1) || verify_pool(&one, 1)) return 1;
    snprintf(path, sizeof path, "%s-failure.tl", prefix);
    if (gen_file(path, 1, 0)) return 1;
    p = fms_player_open((const char *const *)&one, 1, err, sizeof err);
    if (!p) return 1;
    n = fms_player_render(p, buf, 512); status = fms_player_error(p);
    memcpy(first, buf, sizeof first);
    bad |= n != 64 || !status || fms_player_render(p, buf, 512) != 0;
    fms_player_rewind(p);
    bad |= fms_player_error(p) != 0 || fms_player_position(p) != 0;
    {
        fms_host_t *h = fms_host_create();
        float samples[512], *out = samples;
        if (!h) return 1;
        fms_event_t stale = ev(FMS_EV_OFF, 0, 0, 0);
        fms_host_swap(h, p); fms_host_set_playing(h, 1);
        fms_host_render_f32(h, &out, 1, 32);
        for (n = 0; n < 32; n++) if ((int16_t)(samples[n] * 32768.0f) != first[n]) bad = 1;
        bad |= fms_host_push(h, &stale) != 0;
        fms_host_render_f32(h, &out, 1, 512);
        bad |= fms_host_error(h) != status || fms_host_position(h) != 64;
        for (n = 0; n < 32; n++) if ((int16_t)(samples[n] * 32768.0f) != first[n + 32]) bad = 1;
        for (n = 32; n < 512; n++) if (samples[n] != 0) bad = 1;
        fms_player_rewind(p);  /* callback stopped, same as the app loader */
        fms_host_swap(h, p);
        fms_host_render_f32(h, &out, 1, 32);
        bad |= fms_host_error(h) != 0 || fms_host_position(h) != 32;
        fms_host_render_f32(h, &out, 1, 512);
        bad |= fms_host_error(h) != status || fms_host_position(h) != 64;
        fms_host_destroy(h);
    }
    fms_player_close(p);
    p = fms_player_open_live("timeline draft2\nfs 48000\nstreams live\ngen_clock 64\n", err, sizeof err);
    bad |= p != NULL || !strstr(err, "live generator banks are not supported");
    fms_player_close(p);
    printf("GENPLAYER tail=%u silent_end=%u error=%u live_refused=%s %s\n", tail, end, status,
           strstr(err, "live generator banks are not supported") ? "yes" : "NO", bad ? "FAIL" : "ok");
    return bad;
}

/* Extract a golden through the same native line API as an app import. */
static int dropped_golden(FILE *f, const char *goldens, const char *family, const char *golden)
{
    char input[1024], name[FMS_NAME], err[FMS_ERR_LEN];
    const char *one = input;
    fms_player_t *p;
    char *lines = NULL, *renamed = NULL;
    size_t n, m;
    snprintf(input, sizeof input, strchr(golden, '/') ? "%s/%s.tl" : "%s/48000/%s.tl", goldens, golden);
    snprintf(name, sizeof name, "typed-%s", family);
    p = fms_player_open(&one, 1, err, sizeof err);
    n = p ? fms_player_patch_lines(p, 0, NULL, 0) : 0;
    lines = n ? (char *)malloc(n) : NULL;
    if (!lines || fms_player_patch_lines(p, 0, lines, n) != n) {
        fprintf(stderr, "DROPPED fixture %s: %s\n", golden, p ? "extraction failed" : err);
        free(lines); fms_player_close(p); return 1;
    }
    m = fms_patch_text(lines, name, NULL, 0);
    renamed = m ? (char *)malloc(m) : NULL;
    if (!renamed || fms_patch_text(lines, name, renamed, m) != m) {
        free(lines); free(renamed); fms_player_close(p); return 1;
    }
    fputs(renamed, f);
    free(lines); free(renamed); fms_player_close(p);
    return 0;
}

/* Preserve the original 18 fixtures, then add a patch per newly typed
 * statement family from the current golden records. Harmonic source rows
 * are checked separately because canonical goldens contain expanded data. */
static int dropped_file(const char *path, const char *goldens, unsigned group)
{
    static const char *const two = "%s 0 65536 180 0 0 0 5 100 0 300\npatch %s 1 65536 0 0 0 0 5 100 0 300\n";
    static const char *const stmt[][2] = {
        { "plain",        "route plain 2 1 1>0:256\nfilter plain lp 900000 2000\n" },
        { "with-route",   "route with-route 2 1 1>0:256:ring\n" },
        { "with-filter",  "filter with-filter lp 900000 2000 x2\n" },
        { "with-shape",   "shape with-shape 180 cubic\n" },
        { "kept-lfo",     "lfo kept-lfo 0 5000 0 0 1200 0 0 0 3 0 0\n" },          /* carried since  */
        { "kept-lfo9",    "lfo kept-lfo9 6 10000 15 55 35 40 256 16384 3 1 2 2\n" },
        { "kept-lfo-nomask", "lfo kept-lfo-nomask 1 4000 0 0 50 0 0\n" },     /* masks absent: every operator */
        { "kept-peg",     "peg kept-peg 0 80 0 0 0 0 2400 120 3\n" },
        { "kept-peg-nomask", "peg kept-peg-nomask 50 400 -30 150 0 100 -1200 240\n" },
        { "kept-fenv",    "filter kept-fenv lp 300000 1500\nfenv kept-fenv 20 600 0 200 4800 0 261626\n" },
        { "kept-fenv-ref", "filter kept-fenv-ref lp 300000 1500\nfenv kept-fenv-ref 20 600 0 200 4800 1000\n" },
        { "with-mod",     "mod with-mod 0 ctl0 pitch0 1200 0\n" },
        { "with-eg",      "eg with-eg 0 30 0 0\n" },
        { "with-pd",      "pd with-pd 0 8192\n" },
        { "with-formant", "formant with-formant 0 500000 2000 60\n" },
    };
    char engine[160];
    size_t k;
    FILE *f = fopen(path, "w");
    if (!f) return 1;
    fms_engine_line(engine, sizeof engine);
    fprintf(f, "timeline draft1\nengine %s\nfs 48000\nlength_ns 1000000\nstreams test\n", engine);
    /* Keep each generated record within the parser's 32-patch limit. */
    if (group) {
        static const char *const more[][2] = { { "amp", "ampeg" }, { "egloop", "egloop" },
                                                { "kstype", "kslevel" }, { "fbtype", "fbpre" }, { "crush", "fx/crush" } };
        for (k = 0; k < sizeof more / sizeof *more; k++)
            if (dropped_golden(f, goldens, more[k][0], more[k][1])) { fclose(f); return 1; }
        fputs("crush\nvoice 0 typed-amp\n", f);
        return fclose(f) != 0;
    }
    for (k = 0; k < sizeof stmt / sizeof stmt[0]; k++) {
        fputs("patch ", f);
        fprintf(f, two, stmt[k][0], stmt[k][0]);
        fputs(stmt[k][1], f);
    }
    fputs("patch with-ks 0 65536 60 0 0 256 10 80 120 300\n"   /* ks needs op 1's break */
          "patch with-ks 1 131072 0 2048 0 0 10 100 180 350 440000 60 0 0 1000000\n"
          "ks with-ks 1 120\n"
          "patch with-noise 0 65536 60 0 0 256 5 200 180 200 0 0 11 0 6000000 0 1\n"
          "patch with-noise 1 98304 0 0 0 0 5 300 240 250\n"
          "noise with-noise 0 7\n", f);
    for (k = 0; k < 4; k++)                                    /* vector needs four ops */
        fprintf(f, "patch with-vector %u %u 180 0 0 0 5 100 0 300\n", (unsigned)k, (unsigned)(65536 * (k + 1)));
    fputs("route with-vector 4 15\nvector with-vector 0 0\n", f);
    {
        static const char *const fixtures[][2] = {
            { "route", "amedge" }, { "filter", "filtertwo" }, { "shape", "shapehard" },
            { "shape31", "shapeasympre" },
            { "mod", "modenv" }, { "eg", "egdelay" }, { "ks", "ksleft" },
            { "noise", "colourhp" }, { "pd", "pdknee" }, { "vector", "vectorxy" },
            { "formant", "formant3" }, { "ksource", "kspluck" }, { "wtuse", "wtframe" }
        };
        for (k = 0; k < sizeof fixtures / sizeof *fixtures; k++)
            if (dropped_golden(f, goldens, fixtures[k][0], fixtures[k][1])) { fclose(f); return 1; }
    }
    fputs("patch typed-harmonic 0 65536 180 0 0 0 5 100 0 300\n"
          "patch typed-harmonic 1 65536 0 0 0 0 5 100 0 300\n"
          "wtbank harmonics 1 256 12\nharmonic harmonics 0 1 0 0\n"
          "harmonic harmonics 0 2 60 0\nwtuse typed-harmonic 0 harmonics 0\n"
          "voice 0 plain\n", f);
    return fclose(f) != 0;
}

static int verify_dropped(const char *path, const char *goldens)
{
    uint32_t group, total = 0, labelled = 0;
    int bad = 0;
    for (group = 0; group < 2; group++) {
        char generated[1024], err[FMS_ERR_LEN], names[256], cut[4];
        const char *one = generated;
        fms_player_t *p;
        fms_info_t info;
        uint32_t i;
        int n = snprintf(generated, sizeof generated, "%s%s", path, group ? ".followups.tl" : "");
        if (n < 0 || (size_t)n >= sizeof generated || dropped_file(generated, goldens, group)) return 1;
        p = fms_player_open(&one, 1, err, sizeof err);
        if (!p) { fprintf(stderr, "DROPPED: %s\n", err); return 1; }
        fms_player_info(p, &info);
        for (i = 0; i < info.npatches; i++) {
            int changed = fms_player_patch_dropped(p, i, names, sizeof names);
            if (changed != 0 || *names) {
                printf("  dropped group %u patch %u: \"%s\" (%d), want empty\n", group, i, names, changed);
                bad = 1;
            }
            if (changed > 0) labelled++;
        }
        /* Empty result obeys short-buffer and count-only contracts. */
        memset(cut, 'x', sizeof cut);
        bad |= info.npatches != (group ? 5u : 32u) || fms_player_patch_dropped(p, 1, cut, 1) != 0 || cut[0] != 0 || cut[1] != 'x'
            || fms_player_patch_dropped(p, 1, NULL, 0) != 0
            || fms_player_patch_dropped(p, info.npatches, names, sizeof names) != -1;
        total += info.npatches;
        fms_player_close(p);
    }
    printf("DROPPED patches=%u labelled=%u %s\n", total, labelled, bad ? "FAIL" : "ok");
    return bad;
}

/* Metadata-only round trips: every patch of every supplied record. The
 * negative mode removes an accepted, written shape line before re-reading;
 * the ordinary byte comparison must reject that loss. */
static int verify_lines(const char *scratch, char **paths, int count, int drop_line)
{
    uint32_t patches = 0;
    int k, bad = 0, dropped = 0;
    char err[FMS_ERR_LEN], eng[160];
    fms_engine_line(eng, sizeof eng);
    for (k = 0; k < count && !bad; k++) {
        const char *path = paths[k];
        fms_player_t *p = fms_player_open(&path, 1, err, sizeof err);
        fms_info_t info;
        uint32_t i;
        if (!p) { fprintf(stderr, "LINES load %s: %s\n", path, err); return 1; }
        fms_player_info(p, &info);
        for (i = 0; i < info.npatches && !bad; i++) {
            char lost[256];
            if (fms_player_patch_dropped(p, i, lost, sizeof lost) != 0 || *lost) {
                fprintf(stderr, "LINES dropped label record=%d patch=%u\n", k, i);
                bad = 1; break;
            }
            size_t need = fms_player_patch_lines(p, i, NULL, 0), again;
            char *lines = need ? (char *)malloc(need) : NULL, *written = NULL, *back = NULL;
            const char *line;
            char name[FMS_NAME] = "", shortbuf[2] = { 'x', 'y' };
            fms_player_t *q = NULL;
            FILE *f;
            if (!lines || fms_player_patch_lines(p, i, lines, need) != need ||
                fms_player_patch_lines(p, i, shortbuf, sizeof shortbuf) != need ||
                shortbuf[0] != 'x' || shortbuf[1] != 'y' ||
                fms_patch_lines_check(lines, need - 1, info.fs, err, sizeof err)) {
                fprintf(stderr, "LINES extract/check %s patch=%u: %s\n", path, i, err);
                bad = 1; free(lines); break;
            }
            for (line = lines; line && *line; line = strchr(line, '\n'), line = line ? line + 1 : NULL)
                if (!strncmp(line, "patch ", 6)) { (void)sscanf(line, "patch %31s", name); break; }
            written = (char *)malloc(need);
            if (!written || !*name) { bad = 1; free(lines); free(written); break; }
            memcpy(written, lines, need);
            if (drop_line && !dropped) {
                char *d;
                for (d = written; d && *d; d = strchr(d, '\n'), d = d ? d + 1 : NULL)
                    if (!strncmp(d, "shape ", 6)) {
                        char *e = strchr(d, '\n');
                        if (e) memmove(d, e + 1, strlen(e + 1) + 1);
                        else *d = 0;
                        dropped = 1; break;
                    }
            }
            f = fopen(scratch, "w");
            if (!f) bad = 1;
            else {
                fprintf(f, "timeline draft2\nengine %s\nfs %u\nlength_ns 1000000000\nspace off\nstreams edit\n", eng, info.fs);
                fputs(written, f);
                fprintf(f, "voice 0 %s\n0 edit 0 on 261626 0\n", name);
                if (fclose(f)) bad = 1;
            }
            if (!bad) q = fms_player_open(&scratch, 1, err, sizeof err);
            again = q ? fms_player_patch_lines(q, 0, NULL, 0) : 0;
            back = again ? (char *)malloc(again) : NULL;
            if (!q || !back || fms_player_patch_lines(q, 0, back, again) != again ||
                again != need || memcmp(lines, back, need)) {
                fprintf(stderr, "LINES mismatch record=%d patch=%u%s\n", k, i, dropped ? " dropped=shape" : "");
                bad = 1;
            }
            if (!bad) patches++;
            fms_player_close(q); free(lines); free(written); free(back);
        }
        if (fms_player_patch_lines(p, info.npatches, NULL, 0)) bad = 1;
        fms_player_close(p);
    }
    remove(scratch);
    if (drop_line && !dropped) { fprintf(stderr, "LINES negative control did not find shape\n"); return 2; }
    if (!bad && !drop_line) {
        static const char good[] = "patch check 0 65536 0 0 0 0 0 0 0 0\npatch check 1 65536 0 0 0 0 0 0 0 0\n";
        static const char *const refused[] = {
            "patch check 0 bad 0 0 0 0 0 0 0 0\n",
            "shape check 241 hard\n", "space stereo\n", "gen_lane 0 0 - - - 1 6900 0 32768 1 0\n",
            "0 edit 0 off\n", "patch other 0 65536 0 0 0 0 0 0 0 0\n", "unknown check 1\n"
        };
        char joined[512], embedded[] = "patch\0hidden";
        size_t j;
        if (fms_patch_lines_check(good, strlen(good), 48000, err, sizeof err) ||
            !fms_patch_lines_check(good, strlen(good), 0, err, sizeof err) ||
            !fms_patch_lines_check(embedded, sizeof embedded - 1, 48000, err, sizeof err) ||
            !fms_patch_lines_check(NULL, 0, 48000, err, sizeof err)) bad = 1;
        for (j = 0; j < sizeof refused / sizeof *refused; j++) {
            snprintf(joined, sizeof joined, "%s%s", good, refused[j]);
            if (!fms_patch_lines_check(joined, strlen(joined), 48000, err, sizeof err) || !*err) bad = 1;
        }
        /* The parser's first malformed line is reported as editor line 1. */
        if (!fms_patch_lines_check(refused[0], strlen(refused[0]), 48000, err, sizeof err) ||
            !strstr(err, "patch lines:1:")) bad = 1;
    }
    printf("LINES %s records=%d patches=%u audio_frames=0\n", bad ? "FAIL" : "ok", count, patches);
    return bad;
}

int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "--space-bounds")) return verify_space_bounds();
    if (argc == 3 && !strcmp(argv[1], "--live-move")) return verify_live_move(argv[2]);
    if ((argc == 5 || argc == 6) && !strcmp(argv[1], "--live-values"))
        return verify_live_values(argv[2], argv[3], !strcmp(argv[4], "3"), argc == 6 ? argv[5] : NULL);
    if (argc >= 3 && !strcmp(argv[1], "--seek")) return verify_seek(argv + 2, argc - 2);
    if (argc >= 4 && !strcmp(argv[1], "--wav")) return verify_wav(argv[2], argv + 3, argc - 3);
    if (argc >= 4 && (!strcmp(argv[1], "--lines") || !strcmp(argv[1], "--lines-drop")))
        return verify_lines(argv[2], argv + 3, argc - 3, !strcmp(argv[1], "--lines-drop"));
    if (argc == 3 && !strcmp(argv[1], "--gen")) return verify_gen(argv[2]);
    if (argc == 4 && !strcmp(argv[1], "--dropped")) return verify_dropped(argv[2], argv[3]);
    if (argc == 2 && !strcmp(argv[1], "--engine")) {
        char eng[160];
        fms_engine_line(eng, sizeof eng);
        printf("%s\n", eng);
        return 0;
    }
    if ((argc == 3 || argc == 4) && !strcmp(argv[1], "--live"))
        return verify_live(argv[2], argc == 4 && !strcmp(argv[3], "--binaural") ? FMS_SPACE_BINAURAL :
                           argc == 4 && !strcmp(argv[3], "--stereo") ? FMS_SPACE_STEREO : FMS_SPACE_OFF);
    if (argc == 3 && !strcmp(argv[1], "--midi")) return verify_midi(argv[2]);
    if (argc == 3 && !strcmp(argv[1], "--midi-live")) return verify_midi_live(argv[2]);
    if (argc == 2 && !strcmp(argv[1], "--midi-scan")) {
        uint64_t n, near;
        int r = fms_midi_pitch_scan(&n, &near);
        printf("MIDISCAN exponents=%llu near_ties=%llu %s\n", (unsigned long long)n, (unsigned long long)near, r ? "FAIL" : "ok");
        return r;
    }
    if (argc < 2 || argv[1][0] == '-') {
        fprintf(stderr, "usage: fms_verify FILE... | --seek FILE... | --wav OUT FILE... | --live TAKE [--stereo|--binaural] | --gen PREFIX | --dropped FILE | --engine\n"
                        "       | --space-bounds | --live-values GOLDEN TAKE ORDER [DROP] | --live-move TAKE\n"
                        "       | --midi FILE | --midi-live TAKE | --midi-scan\n");
        return 2;
    }
    return verify_pool(argv + 1, argc - 1);
}
