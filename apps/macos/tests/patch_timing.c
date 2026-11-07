/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * Measure metadata lock timing without audio rendering.
 * Include the bridge to observe its existing capture lock. A barrier
 * starts the measured patch-text request just after the loader acquires
 * that lock, before its real pool parsing. No sleep extends the load.
 */
#include <pthread.h>
#include <time.h>
#include <sched.h>
#include <stdint.h>

static int timing_lock(pthread_mutex_t *m);
#define pthread_mutex_lock timing_lock
#include "../bridge/jottune_bridge.c"
#undef pthread_mutex_lock

static pthread_t loader_thread;
static int armed, entered, request;
static uint64_t waited_ns;
static const char *const *pool;
static int pool_n, pool_failed;

static uint64_t now_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}

static int timing_lock(pthread_mutex_t *m)
{
    uint64_t t;
    int r;
    if (!__atomic_load_n(&armed, __ATOMIC_ACQUIRE))
        return pthread_mutex_lock(m);
    if (pthread_equal(pthread_self(), loader_thread)) {
        r = pthread_mutex_lock(m);
        __atomic_store_n(&entered, 1, __ATOMIC_RELEASE);
        while (!__atomic_load_n(&request, __ATOMIC_ACQUIRE)) sched_yield();
        return r;
    }
    t = now_ns();
    __atomic_store_n(&request, 1, __ATOMIC_RELEASE);
    r = pthread_mutex_lock(m);
    waited_ns += now_ns() - t;
    return r;
}

static void *load_pool(void *unused)
{
    char err[FMS_ERR_LEN];
    fms_player_t *p;
    (void)unused;
    loader_thread = pthread_self();
    p = fms_player_open(pool, pool_n, err, sizeof err);
    pool_failed = !p;
    if (p) fms_player_close(p);
    return NULL;
}

static int compare_u64(const void *a, const void *b)
{
    const uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}

int main(int argc, char **argv)
{
    fms_player_t *p;
    char *patch;
    size_t patch_n;
    char err[FMS_ERR_LEN], text[8192], names[512];
    uint64_t samples[100], t, total;
    pthread_t thread;
    int i, kind, bad = 0;
    static const char *const labels[] = { "patch_text", "live_header_text", "patch_dropped" };
    if (argc < 3) return 2;
    p = fms_player_open((const char *const *)&argv[1], 1, err, sizeof err);
    patch_n = p ? fms_player_patch_lines(p, 0, NULL, 0) : 0;
    patch = patch_n ? (char *)malloc(patch_n) : NULL;
    if (!patch || fms_player_patch_lines(p, 0, patch, patch_n) != patch_n) return 2;
    printf("TIMING clock=monotonic calls=100 audio_frames=0\n");
    for (kind = 0; kind < 3; kind++) {
        total = 0;
        for (i = 0; i < 100; i++) {
            t = now_ns();
            if (kind == 0) bad |= fms_patch_text(patch, "probe", text, sizeof text) == 0;
            if (kind == 1) {
                size_t n = fms_live_header_text(patch, "probe", 4, 48000, 0, NULL, text, sizeof text);
                bad |= n == 0 || n > sizeof text;
            }
            if (kind == 2) bad |= fms_player_patch_dropped(p, 0, names, sizeof names) < 0;
            samples[i] = now_ns() - t;
            total += samples[i];
        }
        qsort(samples, 100, sizeof samples[0], compare_u64);
        printf("TIMING %s min_ns=%llu median_ns=%llu max_ns=%llu mean_ns=%llu\n", labels[kind],
               (unsigned long long)samples[0], (unsigned long long)samples[50],
               (unsigned long long)samples[99], (unsigned long long)(total / 100u));
    }
    pool = (const char *const *)&argv[2]; pool_n = argc - 2;
    __atomic_store_n(&armed, 1, __ATOMIC_RELEASE);
    if (pthread_create(&thread, NULL, load_pool, NULL)) return 2;
    while (!__atomic_load_n(&entered, __ATOMIC_ACQUIRE)) sched_yield();
    __atomic_store_n(&request, 1, __ATOMIC_RELEASE);
    t = now_ns();
    bad |= fms_patch_text(patch, "probe", text, sizeof text) == 0;
    total = now_ns() - t;
    pthread_join(thread, NULL);
    __atomic_store_n(&armed, 0, __ATOMIC_RELEASE);
    bad |= pool_failed;
    printf("TIMING contended_patch_text total_ns=%llu capture_lock_wait_ns=%llu pool_files=%d load_ok=%s\n",
           (unsigned long long)total, (unsigned long long)waited_ns, pool_n, pool_failed ? "no" : "yes");
    printf("TIMING %s\n", bad ? "FAIL" : "ok");
    fms_player_close(p); free(patch);
    return bad != 0;
}
