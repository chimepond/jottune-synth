/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0 */
#include <stdio.h>
/* WASI has no temporary-file API. Only the host parameter self-test uses it. */
FILE *tmpfile(void) { return NULL; }
