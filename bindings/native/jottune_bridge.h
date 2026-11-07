/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * jottune_bridge.h: the shared native C bridge to the integer FM synth.
 *
 * Host side only: the bridge compiles src/host/harness.c into its own translation unit
 * (main renamed), so a
 * record is read by the harness's own parser and rendered by its own path.
 * A record plays in the app with the samples and the FNV-1a 64 hash that
 * `harness --play` prints, for any callback size (the render is
 * block-independent; the harness checks block_indep=yes). A grammar or engine
 * feature that lands in the harness reaches the app with a rebuild.
 *
 * The library stays integer. Host Keys derivation uses the existing libm
 * profile worker. fms_host_render_f32 converts int16 to the audio device's
 * float format; platform UI drawing is also outside the integer library.
 *
 * Threads: fms_player_* calls that load or inspect run on non-audio threads,
 * one thread per player at a time; loads on independent players are safe (diagnostics and cancellation
 * are scoped to each worker thread). fms_host_render_f32 runs on the
 * audio thread and never allocates, locks or prints. The other fms_host_*
 * calls are made from the UI thread and hand data to or from the audio thread
 * through lock-free single-producer, single-consumer queues and atomics.
 */
#ifndef FMSPIKE_H
#define FMSPIKE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FMS_ERR_LEN    512   /* the size of every err buffer */
#define FMS_OPS        4     /* operators per voice (FM_OPS) */
#define FMS_MAX_CH     16    /* most output channels a record can have (order-3 field) */
#define FMS_MAX_VOICES 64    /* TL_MAX_VOICES */
#define FMS_NAME       32    /* TL_NAME */

/* ---- event kinds (the harness's EV_* mapped to a stable set) ---- */
enum {
    FMS_EV_ON = 1,     /* freq_millihz, value = velocity_q15 */
    FMS_EV_OFF,
    FMS_EV_PITCH,      /* freq_millihz */
    FMS_EV_POS,        /* x, y, z in mm */
    FMS_EV_MOVE,       /* x, y, z in mm where it ends; dur_ns */
    FMS_EV_FILTER,     /* freq_millihz = cutoff, value = q_milli */
    FMS_EV_SEND,       /* bus (0 chorus, 1 delay, 2 reverb, 3 duck, 4 flanger as fx.h numbers them), value = level_q15 */
    FMS_EV_YAW,        /* x = the listener's yaw in millidegrees */
    FMS_EV_OTHER,      /* a kind this bridge does not know yet: still applied, shown as a dot */
    FMS_EV_CTL,        /* bus=input 0..7, x=signed Q15; appended, old IDs unchanged */
    FMS_EV_CRUSH = 11  /* value=BITS 1..24, bus=RATE_DIV 1..64; next FX block */
};

typedef struct {
    uint64_t t_ns;
    uint32_t sample;        /* the frame it applies at: floor(t_ns * fs / 10^9) */
    uint32_t type;          /* FMS_EV_* */
    uint32_t voice;
    uint32_t stream;        /* index into the streams statement */
    uint32_t freq_millihz;
    uint32_t value;
    uint32_t bus;
    int32_t  x, y, z;
    uint64_t dur_ns;
} fms_event_t;

/* ---- transient typed editor views over accepted statement tokens ----
 * The complete patch is carried as canonical text. These small structs
 * only describe individual existing UI controls; none stores a patch. */
typedef struct {
    uint32_t ratio_q16;        /* 65536 = 1.0 */
    uint32_t level_db10;       /* tenths of a dB below full scale, 0 to 1200 */
    uint32_t index_q10;        /* radians Q6.10 */
    uint32_t feedback_q10;     /* radians Q6.10 */
    uint32_t vel_sens_q8;      /* 0 to 256 */
    uint32_t attack_ms, decay_ms;
    uint32_t sustain_db10;     /* below peak, 0 to 1200 */
    uint32_t release_ms;
    uint32_t ks_break_millihz;
    uint32_t ks_slope_db10;
    uint32_t wave;             /* 0 sine, 1 half, 2 absolute, 3 quarter, 4 noise, 5 alternating sine,
                                  6 camel, 7 square, 8 log-saw, 9 pink, 10 colour LP, 11 colour HP;
                                  others play as 0 (fm_a1.h) */
    uint32_t rate_scale;       /* 0 to 7 */
    uint32_t fixed_millihz;    /* 0: follow the note */
    uint32_t phase_u16;
    uint32_t free_run;
} fms_op_t;

/* The lfo statement (fm_lfo_patch_t, ). A patch carries it while
 * rate_millihz is not 0, the rule of the harness's case loader. The ranges
 * are the timeline grammar's; fms_patch_lines_check validates them. */
typedef struct {
    uint32_t rate_millihz;     /* 0: no lfo statement; else 10 to 50000 */
    uint32_t wave;             /* 0 sine, 1 triangle, 2 saw up, 3 saw down, 4 square,
                                  5 sample and hold, 6 smoothed random */
    uint32_t delay_ms;         /* 0 to 60000 */
    uint32_t fade_ms;          /* 0 to 60000 */
    uint32_t pitch_cents;      /* peak pitch offset, 0 to 1200 */
    uint32_t level_db10;       /* peak level attenuation, 0 to 1200 */
    uint32_t index_q10;        /* peak index offset, radians Q6.10 */
    uint32_t phase_u16;        /* start phase, 1/65536 turns */
    uint32_t mask_pitch;       /* bit i: op i takes the pitch offset; only the patch's operators */
    uint32_t mask_level;       /* bit i: op i takes the level attenuation */
    uint32_t mask_index;       /* bit i: op i takes the index offset */
    uint32_t free_run;         /* 0 key sync, 2 continuous from voice init () */
} fms_lfo_t;

/* The peg statement (fm_peg_patch_t, ), carried while a level, a time
 * or the mask is not 0. */
typedef struct {
    int32_t  level_cents[4];   /* L1..L4, -4800 to 4800 */
    uint32_t time_ms[4];       /* T1..T4, 0 to 60000 */
    uint32_t mask;             /* bit i: op i follows; 0 every operator */
} fms_peg_t;

/* The fenv statement (fm_fenv_patch_t, ), carried while any field is
 * not 0; the timeline grammar takes it only with a filter . */
typedef struct {
    uint32_t attack_ms, decay_ms, release_ms;   /* 0 to 60000 */
    uint32_t sustain_permille;                  /* 0 to 1000, 1000 full */
    int32_t  depth_cents;                       /* -9600 to 9600 */
    uint32_t track_permille;                    /* 0 to 2000, 1000 one octave per octave */
    uint32_t ref_millihz;                       /* the tracking reference; 0: 261626 */
} fms_fenv_t;

/* ---- what a loaded record is ---- */
typedef struct {
    uint32_t fs;
    uint32_t channels;        /* 1 mono, 2 stereo or binaural, 4 or 16 for amb1 or amb3 */
    uint32_t frames;          /* the record's length (the harness's n) */
    uint64_t length_ns;
    uint32_t nvoices, npatches, nstreams, nevents;
    int      draft;           /* the timeline statement's draft */
    int      space;           /* 1: the stereo module (space stereo) */
    int      route;           /* 0, or SP_FIELD_*: 1 amb1, 2 amb3, 3 binaural, 4 stereo with a pan law */
    uint32_t order;           /* binaural: 1 or 3 */
    uint32_t block, near_mm;  /* the space statements, 0 without space */
    int32_t  yaw_mdeg;
    int      fx_on;           /* the effects module (fx/) is on */
    int      have_hash;
    uint64_t hash_expected;   /* the record's hash statement */
    char     engine[160];     /* the engine line this build writes (tl_engine) */
} fms_info_t;

/* ---- players: one loaded record, rendered from frame 0 forward ---- */
typedef struct fms_player fms_player_t;

/* Reads the files as one pool, as `harness --play f1 f2 ...` does (a header
 * file, then one file per stream; or one file). NULL with a message in err
 * (the harness's own diagnostics) on a refused timeline. */
/* Full-record memory interface (host side). API uses POSIX memory streams,
 * GCC/Clang atomics/TLS: iOS 15+, Android API 23+, macOS and Linux. */
typedef struct fms_cancel fms_cancel_t;
enum { FMS_OK = 0, FMS_INVALID = 1, FMS_HASH_MISMATCH = 3,
       FMS_NO_MEMORY = 5, FMS_CANCELLED = 6, FMS_RENDER_ERROR = 7 };
/* Single-use token. Request is thread-safe; close only after all users join. */
fms_cancel_t *fms_cancel_create(void);
void fms_cancel_request(fms_cancel_t *token);
void fms_cancel_close(fms_cancel_t *token);
/* text[0..n) is borrowed only during this call; n excludes NUL. Parse and
 * validate with the production reader, without rendering/checking PCM hash.
 * Returned player owns all state. No temporary files or sandbox path needed. */
fms_player_t *fms_player_open_text(const char *text, size_t n, char *err, size_t cap);
/* Same import with cancellation and explicit status. Always initializes *out
 * to NULL; assigns ownership only on success. err is bounded/NUL-terminated
 * when cap>0; NULL err is permitted. One worker exclusively owns the player. */
int fms_player_import(const char *text, size_t n, const fms_cancel_t *cancel,
                      fms_player_t **out, char *err, size_t cap);
/* Offline source-rate/channel PCM16, caller owns frames*info.channels samples.
 * Check cancellation between at most 256-frame render calls. *written reports
 * the valid prefix, including on cancel/error. State/hash retain that prefix.
 * At EOF verify expected hash when present; mismatch returns FMS_HASH_MISMATCH.
 * Cancellation never frees or rewinds p. Join before replacement/close. */
int fms_player_render_checked(fms_player_t *p, int16_t *out, uint32_t frames,
                              const fms_cancel_t *cancel, uint32_t *written,
                              char *err, size_t cap);
/* Canonical complete TL only after full successful checked render. Return
 * bytes INCLUDING NUL, zero if incomplete/cancelled/mismatched/live/OOM.
 * Sizing call: out=NULL. Too-small buffers remain untouched. No file I/O. */
size_t fms_player_record_text(const fms_player_t *p, char *out, size_t cap);
/* Private derived player; source is unchanged. Reuse the CLI Keys worker:
 * half_width 30/45/60/75/90, mirror 0/1, carrier_bound 0(strict)/1(opt-in).
 * Import validates source saved hash during the transformation. Cancellation
 * publishes no result. Inspect/render/export result with ordinary record APIs. */
int fms_player_keys(const fms_player_t *source, uint32_t half_width, int mirror,
                    int carrier_bound, const fms_cancel_t *cancel,
                    fms_player_t **out, char *err, size_t cap);

fms_player_t *fms_player_open(const char *const *paths, int npaths, char *err, size_t errlen);

/* A live player from header text only (timeline, engine, fs, space, patch,
 * voice, fx statements; no events, no length_ns, no hash): it renders until
 * closed, its events arriving through fms_host_push. The engine line may be
 * omitted: the bridge writes this build's. gen_clock is explicitly refused:
 * generator banks currently play through the record interface only.
 * A decodable header is additionally required to be event-free; its live
 * POS events are accepted only once per voice at actual frame zero, and
 * MOVE is refused. Queuing an event does not imply native acceptance.
 * NULL with a message in err. */
fms_player_t *fms_player_open_live(const char *header_text, char *err, size_t errlen);
/* A/B candidate setup: call once on a fresh, quiescent, unpublished live
 * player. Complete unique POS per voice, optionally YAW voice0, with zero
 * time/sample/stream/duration; native ranges/field rules apply. Validates all
 * rows atomically, stores ascending POS then YAW, and applies before sample0.
 * Every rewind restores this static seed; take start logs it before queued
 * events. No move trajectory or active-player state is captured. 0/1 result. */
int fms_player_seed_geometry(fms_player_t *p, const fms_event_t *events,
                             uint32_t count, char *err, size_t cap);

/* Host spatial-header options . AMB1/3 are read-back values only:
 * live open refuses them; Records preserves their source coefficients.
 * Optional rows retain their presence independently of their numeric values. */
enum { FMS_SPACE_OFF = 0, FMS_SPACE_STEREO = 1, FMS_SPACE_BINAURAL = 2,
       FMS_SPACE_AMB1 = 3, FMS_SPACE_AMB3 = 4 };
enum { FMS_SPACE_ORDER = 1, FMS_SPACE_PANLAW = 2,
       FMS_SPACE_PINNA = 4, FMS_SPACE_YAW = 8 };
typedef struct {
    uint32_t field, block, near_mm, order, panlaw_q15, pinna;
    uint32_t pinna_d[5], present;
    int32_t yaw_mdeg;
} fms_space_t;

/* off, block64, near100, effective binaural order3; other values/rows zero.
 * Pinna IDs are off0, pbnh1, rd2, custom3 (five thousandths0..1000).
 * Read-back never imports any position, movement or yaw event. */
void     fms_space_defaults(fms_space_t *out);
void     fms_player_space(const fms_player_t *p, fms_space_t *out);
/* All canonical FX header rows from the native writer, including fields
 * without a typed app control. Metadata only; needed size includes NUL,
 * zero on failure; insufficient buffers are untouched. */
size_t   fms_player_fx_text(const fms_player_t *p, char *out, size_t cap);

/* Builds a draft2 live header from model values. NULL space preserves the
 * original mono/default output. The native live opener validates ranges
 * and combinations, including field master and panlaw/FX restrictions. No value is
 * clamped or discarded here. Extra contains FX rows; events use stream0.
 * Returns needed bytes including NUL, zero on allocation/structural error;
 * insufficient output buffers are untouched. Non-audio threads only. */
size_t   fms_live_header_text(const char *lines, const char *name, uint32_t nvoices, uint32_t fs,
                              const fms_space_t *space, const char *extra, char *out, size_t cap);

/* This build's engine line (tl_engine), the one fms_info_t.engine holds. */
void     fms_engine_line(char *out, size_t cap);

/* True only when canonical parsed state equals the one-patch C4 file form:
 * draft2, source rate, mono, one voice, 1.5-second note in 2.5 seconds.
 * Ignores comments/formatting and the expected hash; never renders. */
int      fms_player_is_patch_file(const fms_player_t *p);
void     fms_player_close(fms_player_t *p);
void     fms_player_info(const fms_player_t *p, fms_info_t *out);
const char *fms_player_stream_name(const fms_player_t *p, uint32_t i);
const char *fms_player_voice_patch(const fms_player_t *p, uint32_t voice);   /* the patch's name */
/* Canonical lines for one patch, including its record's wavetable banks and
 * generator definitions needed by a binding. No voices, lanes, events,
 * effects or spatial state. Returns needed bytes including NUL, 0 on error;
 * writes only when cap is sufficient. Metadata only, never the audio thread. */
size_t   fms_player_patch_lines(const fms_player_t *p, uint32_t i, char *out, size_t cap);
/* n excludes NUL. Validate with the harness as a one-patch draft2 record and
 * a silent note at fs. Return 0, else 1 with the harness's diagnostic; parser
 * line numbers refer to lines. No audio is rendered and input is unchanged. */
int      fms_patch_lines_check(const char *lines, size_t n, uint32_t fs, char *err, size_t cap);

/* Compare the complete canonical lines with their harness-validated re-read.
 * Return differing statement names once each, 0 for an exact round trip,
 * -1 on failure. names is NUL-terminated when cap > 0 and may be truncated.
 * Metadata only; validation uses memory streams, never the audio thread. */
int      fms_player_patch_dropped(const fms_player_t *p, uint32_t i, char *names, size_t cap);
uint32_t fms_player_events(const fms_player_t *p, fms_event_t *out, uint32_t max); /* the sorted events; returns how many exist */
/* Exact canonical line for any explicit event, including generator controls
 * represented as OTHER in the stable typed view above. Uses the sole native
 * writer; includes final newline and NUL in the size, zero if index invalid.
 * Sizing call/insufficient-buffer behavior matches record_text. No render is
 * needed. Runtime-generated notes are not materialized by either event view. */
size_t fms_player_event_text(const fms_player_t *p, uint32_t index, char *out, size_t cap);


/* Each voice's path through space, for drawing . The
 * record's pos and move events go through the spatial module's own
 * sp_stereo_set_pos, sp_stereo_move and sp_coord_at on a scratch state, so
 * a move that a later event cuts short ends where the module puts the
 * voice. Points per voice, in order; sample is the first sample of the
 * block boundary the point is read at:
 * - at each of the voice's pos and move events: where the voice is at the
 *   event's boundary (pen 1: a straight line from its last point; pen 0
 *   when it is the voice's first point), except before a pos at boundary 0;
 * - after a pos, or a move too short to reach a later boundary: the new
 *   position (pen 0: a jump);
 * - where a move ends, when it ends before the voice's next event or the
 *   record's last block (pen 1); a move the record's end cuts ends at
 *   the last block's position.
 * A voice with no pos or move: one point, its start (0, 1000, 0). Returns
 * how many points exist and writes at most max; 0 for a record without
 * the spatial module and for the live player. */
typedef struct {
    uint32_t voice, sample, pen;
    int32_t  x, y, z;       /* mm */
} fms_pathpt_t;
uint32_t fms_player_paths(const fms_player_t *p, fms_pathpt_t *out, uint32_t max);

/* Where each of the first max voices is at this player's current block, as
 * fms_host_voice_pos reports it, written as x, y, z per voice; returns how
 * many voices exist. Read it only while no audio thread renders the player
 * (the loader does, before installing it): at frame 0 a record with the
 * spatial module has every voice at (0, 1000, 0). */
uint32_t fms_player_voice_pos(const fms_player_t *p, int32_t *xyz, uint32_t max);

/* Back to frame 0: voices, space, fx, bank, grid, cursor, hash and clips.
 * Call while rendering is stopped, on a non-audio thread: generator text
 * expansion is bounded initialization work. */
void     fms_player_rewind(fms_player_t *p);

/* Offline record seek: rewind and render the prefix [0, frame), retaining
 * its exact state/hash/clips for continuation. Caller must exclusively own a
 * private player or prove host retirement first; never call on the callback.
 * Null/live/out-of-range requests return nonzero without changing the player.
 * A render error returns nonzero with the reached state and diagnostic. */
int      fms_player_seek(fms_player_t *p, uint32_t frame, char *err, size_t errlen);

/* Renders up to frames frames from the current position into out
 * (interleaved, channels per frame), applying each event at its sample
 * exactly as tl_render does. Returns the frames written: fewer at the
 * record's end, 0 past it, or on a generator error (fms_player_error).
 * Explicit events apply before the session bank tick; caches publish before
 * generated lane offs/ons. No allocation, expansion or I/O while rendering. */
uint32_t fms_player_render(fms_player_t *p, int16_t *out, uint32_t frames);

uint32_t fms_player_position(const fms_player_t *p);    /* frames rendered since the rewind */
uint64_t fms_player_hash(const fms_player_t *p);        /* FNV-1a 64 of every sample rendered so far, as fm_golden_hash */
uint32_t fms_player_clipped(const fms_player_t *p);
/* Numeric generator status: code in bits0..7, ID in8..15, field in16..23.
 * Zero means no error. Read only while the player is not rendering. */
uint32_t fms_player_error(const fms_player_t *p);

/* Renders the whole record offline from frame 0 in chunks of chunk frames
 * (0: one call for all) and returns the hash; rewinds before and after.
 * The verification path: equal to the record's hash and to `harness --play`. */
uint64_t fms_player_render_all(fms_player_t *p, uint32_t chunk, uint32_t *clipped);

/* Offline PCM16 companion, byte-identical to harness --play --wav: 44-byte
 * header, record rate/channels and interleaving, no extra metadata chunks.
 * Uses a private stopped record player, never a host's active/live player.
 * Checks the record hash when present; rewinds before/after rendering.
 * Returns 0 on success, nonzero with a diagnostic on failure. Native atomic
 * file writing preserves an existing destination on failure. */
int fms_player_write_wav(fms_player_t *p, const char *path, char *err, size_t errlen);


/* ---- one patch, one note, offline: for drawing ---- */
/* Renders frames of mono int16 of a single voice of patch at fs: a note-on at
 * frame 0 at freq_millihz and velocity_q15, a note-off at off_frame (or never
 * when off_frame >= frames). 0 ok. Uses the engine directly (fm_voice_*). */
int      fms_render_note(const char *lines, uint32_t fs, uint32_t freq_millihz,
                         uint32_t velocity_q15, uint32_t off_frame, int16_t *out, uint32_t frames);

/* Join accepted patch lines under name, changing only the patch-name token.
 * Independent bank/source names are kept. No parser call, I/O or capture
 * lock; use fms_patch_lines_check for edits. Returns needed bytes including
 * NUL, zero on failure, and writes only when cap is sufficient. */
size_t   fms_patch_text(const char *lines, const char *name, char *out, size_t cap);

/* ---- the host: what the audio thread runs ---- */
typedef struct fms_host fms_host_t;

fms_host_t *fms_host_create(void);
void     fms_host_destroy(fms_host_t *h);

/* Hands p to the audio thread, which starts it at its next callback; returns
 * the player it stops using (the caller closes it once
 * fms_host_retired(h) says the audio thread has let it go), or NULL. p may be
 * NULL (silence). UI calls are serialized. A pending take follows this swap;
 * handover yields while the callback claims its source, so a suspended
 * callback can delay this UI call. The callback itself never waits. */
fms_player_t *fms_host_swap(fms_host_t *h, fms_player_t *p);
int      fms_host_retired(const fms_host_t *h);      /* 1: the last swapped-out player is no longer touched */

void     fms_host_set_playing(fms_host_t *h, int on); /* 0: the callback writes silence and the player waits */
void     fms_host_set_gain_q15(fms_host_t *h, uint32_t gain_q15); /* monitor gain after the hash, 32768 = unity */

/* The audio callback: renders frames of the current player, hashing them,
 * and writes them to the device's float channels (non-interleaved, nout of
 * them): channel c of the record to output c; a mono record to every output;
 * a record with more channels than outputs is silenced (raw amb1/amb3
 * coefficients require a real stereo/binaural derivation). Applies queued live events at the start of the call, at the
 * player's current frame. */
void     fms_host_render_f32(fms_host_t *h, float *const *out, uint32_t nout, uint32_t frames);

/* Live events from the UI thread, applied at the next callback's first frame.
 * type FMS_EV_ON, OFF, PITCH, FILTER, POS, SEND, YAW; returns 0, or -1 when the queue is full. */
/* Crusher edits queued before successful fresh arm/Record are discarded at
 * that reset boundary; later crusher edits and every other event keep order. */
int      fms_host_push(fms_host_t *h, const fms_event_t *e);

/* Recording a live take: from start, every applied live event is logged
 * with the frame it was applied at; stop ends the take at the current frame.
 * fms_host_take_text writes the take as a timeline: the live player's header
 * with length_ns and the hash of the frames rendered during the take, then
 * the events at t_ns = ceil(frame * 10^9 / fs), so that `harness --play`
 * replays the take to exactly those samples and that hash. Events at the
 * take's last frame (played while paused) touch no sample and are left out.
 * Start snapshots the immutable live header on the UI thread; an already
 * pending/active take or a non-live player is ignored. The callback claims
 * that snapshot and rewinds its live player (bounded fms_init_state work).
 * Start, swap, stop and take_text are serialized UI calls. No snapshot copy
 * runs in the callback.
 * Returns the bytes needed (write when cap is large enough). */
void     fms_host_record_start(fms_host_t *h);
void     fms_host_record_stop(fms_host_t *h);
/* Ends the take at once on the calling thread, with no wait: only while no
 * audio callback runs (the app's offline script mode, FMS_SCRIPT). */
void     fms_host_record_stop_now(fms_host_t *h);
int      fms_host_recording(const fms_host_t *h);
/* Whether the last accepted Record request won callback claim and reached
 * rewind, even if it captured no frame or text serialization later failed.
 * NULL returns0. Acquire read; remains valid through end until the next
 * accepted request resets it. After recording becomes0, the serialized UI
 * must snapshot this outcome before accepting another request. */
int      fms_host_record_started(const fms_host_t *h);
size_t   fms_host_take_text(fms_host_t *h, char *out, size_t cap);
/* Asks the callback to end the take at its next call and returns at once
 * (fms_host_record_stop waits up to 250 ms, leaving a stop pending on timeout);
 * fms_host_recording turns
 * 0 once the take has ended. */
void     fms_host_record_request_stop(fms_host_t *h);
/* 1 when the last take ended because its event log was full (65536 events). */
int      fms_host_take_full(const fms_host_t *h);
/* Only while no audio callback runs (the device never started or has
 * stopped, or offline between renders): does the callback's handover here,
 * so a swapped-out player retires (fms_host_retired) and a stop request
 * ends the take. */
void     fms_host_settle(fms_host_t *h);

/* For drawing, read from the UI thread. */
/* Monotonic modulo-2^32 count of callback profile event refusals. No event
 * state/log mutation occurs on refusal; read on UI and report changes. */
uint32_t fms_host_profile_refusals(const fms_host_t *h);
uint32_t fms_host_position(const fms_host_t *h);          /* the current player's frame */
uint32_t fms_host_error(const fms_host_t *h);             /* atomic copy of fms_player_error */
uint64_t fms_host_hash(const fms_host_t *h);              /* its running hash */
uint32_t fms_host_clipped(const fms_host_t *h);
uint32_t fms_host_scope(const fms_host_t *h, int16_t *out, uint32_t frames, uint32_t *channels); /* the latest frames, interleaved */
void     fms_host_peaks(const fms_host_t *h, uint32_t *peak, uint32_t nch); /* per channel since the last call, 0 to 32768 */
uint32_t fms_host_voice_active(const fms_host_t *h, uint8_t *active, uint32_t max); /* 1 per sounding voice */
/* Each voice's position, mm, 3 per voice: with the spatial module, where it
 * puts the voice at the current block (sp_coord_at: a move in flight
 * included, (0, 1000, 0) before any pos); without it, the last pos. */
void     fms_host_voice_pos(const fms_host_t *h, int32_t *xyz, uint32_t max);

/* ---- live MIDI input (fms_midi.c, shared MIDI translator) ----
 * MIDI 1.0 channel messages, as Universal MIDI Packets (what CoreMIDI hands
 * an input port opened for MIDI 1.0), to live events by midi2tl.py's rules:
 * notes with velocity, the sustain pedal (CC 64), pitch bend over a range RPN
 * 0 may set, CC 74 as a cutoff, midi2tl.py's voice pool and opt-in  maps.
 * Other messages (programs, unmapped pressure/controllers, MIDI clock,
 * transport, SysEx) are counted and ignored. Single-threaded: one caller at a time. */
typedef struct fms_midi fms_midi_t;

typedef struct {
    uint32_t nvoices;          /* 1 to FMS_MAX_VOICES: events use voices 0 to nvoices - 1 */
    uint32_t stream;           /* the events' stream */
    uint32_t channels;         /* bit c: MIDI channel c + 1 plays; 0xFFFF every channel */
    uint32_t release_ms;       /* the patch's longest carrier release (fms_midi_release_ms) */
    uint32_t bend_cents;       /* the bend range until RPN 0 sets one: 200 = 2 semitones; at most 12799 */
    uint32_t rpn;              /* 1: RPN 0 (CC 101 and 100 select, CC 6 and 38 enter) sets the bend range */
    uint32_t cutoff_millihz;   /* CC 74's base, a note-on's cutoff; 0: CC 74 is an other controller */
    uint32_t q_milli;          /* the Q its FILTER events carry */
    uint32_t ctl_reads;        /* low eight bits: nonzero valid ctl sources read by the selected patch */
} fms_midi_config_t;

typedef struct {
    uint64_t messages;         /* every UMP message fed */
    uint64_t notes, note_offs, orphan_offs, restruck, pedal_downs, pedal_held;
    uint64_t steals, release_steals;
    uint64_t bends, pitch_events, cc74, filter_events, rpn_selects, rpn_ranges;
    uint64_t programs, aftertouch, pressure, other_ccs;
    uint64_t other_channels;   /* channel messages on a channel not let through */
    uint64_t system;           /* system common and real time (clock, transport), SysEx, utility */
    uint64_t unknown;          /* other UMP types (MIDI 2.0 voice, data) and stray status */
    uint64_t clamped;          /* pitches kept within 32 bits */
    uint64_t near_ties;        /* results within 2^-60 of a half (see fms_midi.c) */
    uint64_t refused;          /* a note-on with 512 notes down or held, a cut UMP, out too small */
    uint64_t ctl_messages, ctl_events; /* mapped source messages, changed voice values written */
    uint64_t mpe_notes, mpe_pressure, mpe_slides, mcm; /* member activity; ignored RPN6 data */
} fms_midi_stats_t;

enum { FMS_MIDI_OUT = 2 * FMS_MAX_VOICES + 10 }; /* shared C/Swift event capacity */

/* NULL on a config out of range. */
fms_midi_t *fms_midi_create(const fms_midi_config_t *cfg);
void     fms_midi_destroy(fms_midi_t *m);
/* Setup only (before any UMP/external call, or after reset): channel 0..15,
 * spec off/empty, default, or comma-separated  SOURCE:N entries; default
 * may also appear in a list. Enabled channel required except when clearing.
 * Atomic refusal, -1 with diagnostic; 0 on success. Allocates only here.
 * CC74 conflicts with a configured filter; rand owns its input alone. */
int      fms_midi_set_ctl(fms_midi_t *m, uint32_t channel, const char *spec, char *err, size_t cap);
/* Setup only: off/empty, lower:N or upper:N (N1..15). One zone owns the
 * selected patch/pool, accepts only its master/members, and counts other
 * channels as ignored. Config.channels must enable its master. Multiple
 * zones, member ctl maps and master ctl1/ctl4 maps are refused atomically.
 * Preserves the current converter behavior; this is not a protocol conformance claim. */
int      fms_midi_set_mpe(fms_midi_t *m, const char *spec, char *err, size_t cap);
/* Clear notes, latches and counts; preserve zone/maps and restore their seeds. */
void     fms_midi_reset(fms_midi_t *m);
/* One UMP message from w (nw words), stamped t_ns on any one clock (an
 * earlier time than the last is taken as the last): its events go to out
 * (cap at least 2*nvoices+10 with a configured map or MPE zone and patch reads,
 * else nvoices+2;
 * an undersized buffer refuses before changing clocks, notes, random or latches),
 * in the order they apply; returns how many. *used gets the words the message took (at least
 * 1 while nw > 0), so a packet's words are fed by calling again. */
uint32_t fms_midi_ump(fms_midi_t *m, uint64_t t_ns, const uint32_t *w, uint32_t nw,
                      fms_event_t *out, uint32_t cap, uint32_t *used);
/* Another player of the same voices (the on-screen keyboard): on 1, it took
 * voice at t_ns (a MIDI note there loses its voice, with no event); on 0, it
 * released it. The pool then weighs the voice as any other. */
void     fms_midi_external(fms_midi_t *m, uint64_t t_ns, uint32_t voice, int on);
void     fms_midi_stats(const fms_midi_t *m, fms_midi_stats_t *out);
/* The longest release of the patch's carriers (op 0 on the fixed path), ms. */
/* Accepted patch lines, non-audio setup only. Longest carrier release;
 * return 0 for invalid/null input or a zero release. */
uint32_t fms_midi_release_ms(const char *lines, uint32_t fs);
/* Accepted patch lines, setup only: bit n when an effective nonzero matrix
 * slot reads ctl n. Invalid/null lines return 0; no rendering or clock tick. */
uint32_t fms_midi_ctl_reads(const char *lines, uint32_t fs);
/* fms_verify --midi-scan: every pitch exponent the translator can meet,
 * counting those within 2^-60 of a half; 0 when there are none. */
int      fms_midi_pitch_scan(uint64_t *evaluated, uint64_t *near_ties);

#ifdef __cplusplus
}
#endif
#endif /* FMSPIKE_H */
