/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0 */
/* Host wrapper: the production reader/render loop remains the only TL parser. */
#include <stdint.h>
#include <stddef.h>
static int16_t *browser_pcm;
static uint32_t browser_samples, browser_rate, browser_channels;
#define FM_PCM_RESULT(pcm, samples, rate, channels) do { \
    browser_pcm = (pcm); browser_samples = (samples); \
    browser_rate = (rate); browser_channels = (channels); (pcm) = NULL; \
} while (0)
#define main harness_cli_main
#include "harness.c"
#undef main
#ifdef __wasm__
#define EXPORT(name) __attribute__((export_name(name)))
#else
#define EXPORT(name)
#endif
EXPORT("fm_pcm_ptr") uintptr_t fm_pcm_ptr(void) { return (uintptr_t)browser_pcm; }
EXPORT("fm_pcm_samples") uint32_t fm_pcm_samples(void) { return browser_samples; }
EXPORT("fm_pcm_rate") uint32_t fm_pcm_rate(void) { return browser_rate; }
EXPORT("fm_pcm_channels") uint32_t fm_pcm_channels(void) { return browser_channels; }

int main(int argc, char **argv)
{
    return harness_cli_main(argc, argv);
}
