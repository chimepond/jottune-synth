/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0 */
#include <stddef.h>
void *memcpy(void *restrict dst, const void *restrict src, size_t count)
{
    unsigned char *out = dst;
    const unsigned char *in = src;
    while (count--) *out++ = *in++;
    return dst;
}
void *memset(void *dst, int value, size_t count)
{
    unsigned char *out = dst;
    while (count--) *out++ = (unsigned char)value;
    return dst;
}
