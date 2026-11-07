/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0 */
#ifndef FM_WASI_COMPAT_H
#define FM_WASI_COMPAT_H
/* WASI has no process signals. A write failure is returned as an errno. */
#include <signal.h>
struct sigaction { void (*sa_handler)(int); unsigned sa_mask; };
static int sigemptyset(unsigned *set) { *set = 0; return 0; }
static int sigaction(int signal_number, const struct sigaction *action,
                     struct sigaction *previous)
{
    (void)signal_number; (void)action;
    if (previous) { previous->sa_handler = SIG_DFL; previous->sa_mask = 0; }
    return 0;
}
#endif
