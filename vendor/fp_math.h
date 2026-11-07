/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * fp_math.h v2 — integer-only fixed-point arithmetic.
 *
 * Q16.48 fixed_t uses int64_t with 48 fractional bits. FP_ONE = 2^48;
 * resolution is 2^-48. The fp_w128 working type uses native __int128 or a
 * portable two-limb backend with equivalent results. The header also supplies
 * overflow-checked wide multiplication, Q26.38 time, CORDIC trigonometry,
 * Q2.30 boundary converters and vector/matrix/quaternion operations.
 *
 * Derived from astro-nav-int/fp_math.h, revision
 * 9db73509c7c06d4a7bcdcd706c77a5fde3e728c1. The source reference has SHA-256
 * 2e6450668b714c7c2ba5f497e88c441519f71db815b22f0e17fa516b6842f7f9.
 *
 * All functions are static inline and all tables are static const. No mutable
 * global state, initialization routine, allocation or I/O is required.
 *
 * Keep F=80 intermediate chains wide until an explicit fp_w_dsa_narrow or
 * fp_w_wsr operation. Narrowing to Q16.48 and widening again loses precision.
 * Consult individual functions for rounding, range and checked-status rules.
 */

#ifndef FP_MATH_H
#define FP_MATH_H

#include <stdint.h>

/* ================================================================== */
/*  Configuration / backend selection                                  */
/* ================================================================== */

#if defined(__SIZEOF_INT128__) && !defined(FP_MATH_FORCE_PORTABLE)
#define FP_MATH_HAS_INT128 1
#else
#define FP_MATH_HAS_INT128 0
#endif

#if FP_MATH_HAS_INT128
/* __extension__: __int128 is a documented gcc/clang extension, not ISO C99;
 * this keeps the header clean under -std=c99 -pedantic on both. */
__extension__ typedef __int128          fp_i128;
__extension__ typedef unsigned __int128 fp_u128;
#endif

#define FP_PRECISION 48

typedef int64_t fixed_t;

#define FP_ONE   ((fixed_t)1 << FP_PRECISION)
#define FP_HALF  ((fixed_t)1 << (FP_PRECISION - 1))
#undef  FP_ZERO
#define FP_ZERO  ((fixed_t)0)

/* Q26.38 minutes-since-epoch. Range ±2^25 min ≈ ±63 yr,
 * resolution 2^-38 min ≈ 2.2e-10 s. Fit proof: the raw is value·2^38; the
 * verification set reaches ~1.84e6 min ≈ 2^20.8, so |raw| ≤ 2^58.8 — far inside
 * int64. The nominal ±2^25-min endpoint sits at |raw| = 2^63 (only the negative
 * end is exactly representable); no used tsince approaches it. */
#define FP_TMIN_FRAC 38
typedef int64_t fp_tmin_t;

/* Wide storage scale: F = Q16.48 + K=32 = 80 fractional bits in signed
 * 128-bit storage. Keep intermediate values at this scale until narrowing. */
#define FP_DSA_K    32
#define FP_DSA_FRAC (FP_PRECISION + FP_DSA_K)   /* = 80 */

/* Rounding modes for the wide layer; FP_RND_NEAR uses ties away from zero. */
enum { FP_RND_TRUNC = 0, FP_RND_NEAR = 1 };

/* Wide-op checked-failure status bits. Sticky: an op only ever OR-sets a bit,
 * never clears one.
 * FP_W_ERR_SHIFT   — shift domain S ∉ [1,127]                  (guard a)
 * FP_W_ERR_OVERFLOW— 128-bit quotient fit / signed range ceiling(guards b,c)
 * Consult each operation for its deterministic fallback on a guard trip. */
enum { FP_W_OK = 0, FP_W_ERR_SHIFT = 1, FP_W_ERR_OVERFLOW = 2 };

/* ================================================================== */
/*  128-bit working type: fp_w128 (vendored, both backends bit-equal)  */
/* ================================================================== */

#if FP_MATH_HAS_INT128

typedef fp_i128 fp_w128;

static inline fp_w128 fp_w_from_i64(int64_t v)         { return v; }
static inline fp_w128 fp_w_from_u64(uint64_t v)        { return (fp_i128)v; }
static inline int64_t fp_w_to_i64(fp_w128 a)           { return (int64_t)a; }
static inline fp_w128 fp_w_muls(int64_t a, int64_t b)  { return (fp_i128)a * b; }
static inline fp_w128 fp_w_mul(fp_w128 a, fp_w128 b)   { return (fp_i128)((fp_u128)a * (fp_u128)b); }
static inline fp_w128 fp_w_add(fp_w128 a, fp_w128 b)   { return (fp_i128)((fp_u128)a + (fp_u128)b); }
static inline fp_w128 fp_w_sub(fp_w128 a, fp_w128 b)   { return (fp_i128)((fp_u128)a - (fp_u128)b); }
static inline fp_w128 fp_w_neg(fp_w128 a)              { return (fp_i128)(0 - (fp_u128)a); }
static inline fp_w128 fp_w_shl(fp_w128 a, int k)       { return (fp_i128)((fp_u128)a << k); }
static inline fp_w128 fp_w_asr(fp_w128 a, int k)       { return a >> k; }

static inline fp_w128 fp_w_divs(fp_w128 a, fp_w128 b)
{
    if (b == 0) return 0;                       /* M-2: see the portable twin */
    if (b == (fp_i128)-1) return fp_w_neg(a);
    return a / b;
}

static inline fp_w128 fp_w_divs_pow2(fp_w128 a, int k)
{
    fp_u128 mag = (a < 0) ? (0 - (fp_u128)a) : (fp_u128)a;
    fp_u128 q = mag >> k;
    return (a < 0) ? (fp_i128)(0 - q) : (fp_i128)q;
}

static inline int fp_w_cmp(fp_w128 a, fp_w128 b)
{
    return (a < b) ? -1 : (a > b) ? 1 : 0;
}

static inline int fp_w_bits(fp_w128 a)
{
    uint64_t hi = (uint64_t)((fp_u128)a >> 64);
    if (hi != 0) return 128 - __builtin_clzll(hi);
    uint64_t lo = (uint64_t)(fp_u128)a;
    return lo ? 64 - __builtin_clzll(lo) : 0;
}

#else /* portable two-limb backend */

typedef struct { uint64_t lo, hi; } fp_w128;   /* two's complement */

static inline fp_w128 fp_w_from_i64(int64_t v)
{
    fp_w128 r;
    r.lo = (uint64_t)v;
    r.hi = (v < 0) ? ~(uint64_t)0 : 0;
    return r;
}

static inline fp_w128 fp_w_from_u64(uint64_t v)
{
    fp_w128 r; r.lo = v; r.hi = 0; return r;
}

static inline int64_t fp_w_to_i64(fp_w128 a) { return (int64_t)a.lo; }

static inline fp_w128 fp_w_add(fp_w128 a, fp_w128 b)
{
    fp_w128 r;
    r.lo = a.lo + b.lo;
    r.hi = a.hi + b.hi + (r.lo < a.lo);
    return r;
}

static inline fp_w128 fp_w_neg(fp_w128 a)
{
    fp_w128 r;
    r.lo = ~a.lo + 1;
    r.hi = ~a.hi + (a.lo == 0);
    return r;
}

static inline fp_w128 fp_w_sub(fp_w128 a, fp_w128 b) { return fp_w_add(a, fp_w_neg(b)); }

static inline fp_w128 fp_w_umul64_(uint64_t a, uint64_t b)
{
    uint64_t a0 = a & 0xffffffffu, a1 = a >> 32;
    uint64_t b0 = b & 0xffffffffu, b1 = b >> 32;
    uint64_t p00 = a0 * b0;
    uint64_t p01 = a0 * b1;
    uint64_t p10 = a1 * b0;
    uint64_t p11 = a1 * b1;
    uint64_t mid = p01 + (p00 >> 32);
    uint64_t mid2 = mid + p10;
    fp_w128 r;
    r.lo = (mid2 << 32) | (p00 & 0xffffffffu);
    r.hi = p11 + (mid2 >> 32) + ((mid2 < mid) ? ((uint64_t)1 << 32) : 0);
    return r;
}

static inline fp_w128 fp_w_muls(int64_t a, int64_t b)
{
    uint64_t ua = (a < 0) ? -(uint64_t)a : (uint64_t)a;
    uint64_t ub = (b < 0) ? -(uint64_t)b : (uint64_t)b;
    fp_w128 p = fp_w_umul64_(ua, ub);
    return ((a < 0) != (b < 0)) ? fp_w_neg(p) : p;
}

static inline fp_w128 fp_w_mul(fp_w128 a, fp_w128 b)
{
    fp_w128 r = fp_w_umul64_(a.lo, b.lo);
    r.hi += a.lo * b.hi + a.hi * b.lo;
    return r;
}

static inline fp_w128 fp_w_shl(fp_w128 a, int k)
{
    fp_w128 r;
    if (k == 0) return a;
    if (k < 64) { r.hi = (a.hi << k) | (a.lo >> (64 - k)); r.lo = a.lo << k; }
    else        { r.hi = (k < 128) ? (a.lo << (k - 64)) : 0; r.lo = 0; }
    return r;
}

static inline fp_w128 fp_w_ushr_(fp_w128 a, int k)
{
    fp_w128 r;
    if (k == 0) return a;
    if (k < 64) { r.lo = (a.lo >> k) | (a.hi << (64 - k)); r.hi = a.hi >> k; }
    else        { r.lo = (k < 128) ? (a.hi >> (k - 64)) : 0; r.hi = 0; }
    return r;
}

static inline fp_w128 fp_w_asr(fp_w128 a, int k)
{
    uint64_t s = (a.hi >> 63) ? ~(uint64_t)0 : 0;
    fp_w128 r = fp_w_ushr_(a, k);
    if (k == 0) return r;
    if (k < 64) { r.hi |= s << (64 - k); }
    else {
        r.hi = s;
        if (k > 64 && k < 128) r.lo |= s << (128 - k);
        else if (k >= 128) r.lo = s;
    }
    return r;
}

static inline int fp_w_ucmp_(fp_w128 a, fp_w128 b)
{
    if (a.hi != b.hi) return (a.hi < b.hi) ? -1 : 1;
    if (a.lo != b.lo) return (a.lo < b.lo) ? -1 : 1;
    return 0;
}

static inline int fp_w_cmp(fp_w128 a, fp_w128 b)
{
    fp_w128 au = a, bu = b;
    au.hi ^= (uint64_t)1 << 63;
    bu.hi ^= (uint64_t)1 << 63;
    return fp_w_ucmp_(au, bu);
}

static inline int fp_w_bits(fp_w128 a)
{
    if (a.hi != 0) return 128 - __builtin_clzll(a.hi);
    return a.lo ? 64 - __builtin_clzll(a.lo) : 0;
}

static inline fp_w128 fp_w_udivmod_(fp_w128 n, fp_w128 d, fp_w128 *rem_out)
{
    fp_w128 q = { 0, 0 }, r = { 0, 0 };
    int i = fp_w_bits(n);
    while (i-- > 0) {
        r = fp_w_shl(r, 1);
        r.lo |= (i < 64) ? ((n.lo >> i) & 1) : ((n.hi >> (i - 64)) & 1);
        if (fp_w_ucmp_(r, d) >= 0) {
            r = fp_w_sub(r, d);
            if (i < 64) q.lo |= (uint64_t)1 << i;
            else        q.hi |= (uint64_t)1 << (i - 64);
        }
    }
    if (rem_out) *rem_out = r;
    return q;
}

/* A zero divisor yields zero on both backends. Callers that need an error
 * indication must validate the divisor before calling this low-level helper. */
static inline fp_w128 fp_w_divs(fp_w128 a, fp_w128 b)
{
    if (b.hi == 0 && b.lo == 0) { fp_w128 z = { 0, 0 }; return z; }   /* M-2 */
    int sa = (a.hi >> 63) != 0, sb = (b.hi >> 63) != 0;
    fp_w128 q = fp_w_udivmod_(sa ? fp_w_neg(a) : a, sb ? fp_w_neg(b) : b, 0);
    return (sa != sb) ? fp_w_neg(q) : q;
}

static inline fp_w128 fp_w_divs_pow2(fp_w128 a, int k)
{
    if (a.hi >> 63) return fp_w_neg(fp_w_ushr_(fp_w_neg(a), k));
    return fp_w_ushr_(a, k);
}

#endif /* backend */

/* Backend-neutral unsigned comparison for restoring division. Native
 * compares the existing object through unsigned __int128 and portable reuses
 * the existing lexicographic limb comparison. */
static inline int fp_w_ucmp(fp_w128 a, fp_w128 b)
{
#if FP_MATH_HAS_INT128
    fp_u128 ua = (fp_u128)a, ub = (fp_u128)b;
    return (ua < ub) ? -1 : (ua > ub) ? 1 : 0;
#else
    return fp_w_ucmp_(a, b);
#endif
}

/* Backend-neutral sign test and INT128 extremes (both backends). */
static inline int fp_w_is_neg(fp_w128 a)
{
#if FP_MATH_HAS_INT128
    return a < 0;
#else
    return (a.hi >> 63) != 0;
#endif
}
static inline fp_w128 fp_w_int128_max(void) { return fp_w_sub(fp_w_shl(fp_w_from_u64(1), 127), fp_w_from_i64(1)); }
static inline fp_w128 fp_w_int128_min(void) { return fp_w_shl(fp_w_from_u64(1), 127); }

/* ================================================================== */
/*  Wide arithmetic: fp_w_wsr, fp_w_mulshr — no I/O, backend-neutral     */
/* ================================================================== */

/* Sign-aware right shift by s, ties-away (rnd=FP_RND_NEAR) or truncate toward
 * zero (FP_RND_TRUNC). UB-free (F4-1): the magnitude is taken by unsigned
 * modular negation and the sign restored the same way, so x == INT128_MIN never
 * hits the signed-overflow UB of a direct -x. This is the ONLY sign-aware-shift
 * idiom the kernel uses. The shift-domain guard (s ∈ [1,127], the same domain
 * `fp_w_mulshr` enforces) is always on: an out-of-domain s OR-sets the sticky
 * `FP_W_ERR_SHIFT` bit in the caller-owned `*status` (if non-NULL) and returns a
 * deterministic zero. In-domain calls never touch `*status`. */
static inline fp_w128 fp_w_wsr(fp_w128 x, int s, int rnd, int *status)
{
    if (s < 1 || s > 127) { if (status) *status |= FP_W_ERR_SHIFT; return fp_w_from_i64(0); }
    int neg = fp_w_is_neg(x);
#if FP_MATH_HAS_INT128
    fp_u128 mag = neg ? ((fp_u128)0 - (fp_u128)x) : (fp_u128)x;
    if (rnd) mag = (mag + ((fp_u128)1 << (s - 1))) >> s;
    else     mag = mag >> s;
    return neg ? (fp_w128)((fp_u128)0 - mag) : (fp_w128)mag;
#else
    fp_w128 mag = neg ? fp_w_neg(x) : x;
    if (rnd) mag = fp_w_add(mag, fp_w_shl(fp_w_from_u64(1), s - 1));
    mag = fp_w_ushr_(mag, s);
    return neg ? fp_w_neg(mag) : mag;
#endif
}

/* Shared 64-bit primitives for the portable four-limb product (also compile on
 * native — pure uint64, no fp_w128). */
static inline void fp_ml_umul64_(uint64_t a, uint64_t b, uint64_t *lo, uint64_t *hi)
{
    uint64_t a0 = a & 0xffffffffu, a1 = a >> 32;
    uint64_t b0 = b & 0xffffffffu, b1 = b >> 32;
    uint64_t p00 = a0 * b0, p01 = a0 * b1, p10 = a1 * b0, p11 = a1 * b1;
    uint64_t mid = p01 + (p00 >> 32);
    uint64_t mid2 = mid + p10;
    *lo = (mid2 << 32) | (p00 & 0xffffffffu);
    *hi = p11 + (mid2 >> 32) + ((mid2 < mid) ? ((uint64_t)1 << 32) : 0);
}
static inline uint64_t fp_addc_(uint64_t a, uint64_t b, uint64_t *carry)
{
    uint64_t s = a + b;   uint64_t c1 = (s < a);
    uint64_t s2 = s + *carry; uint64_t c2 = (s2 < s);
    *carry = c1 + c2; return s2;
}

/* Overflow-safe round((X·Y) / 2^S) for signed 128-bit X, Y, S in [1,127].
 * The full 128×128→256-bit magnitude product is formed from four
 * 64-bit limb products, the 2^(S-1) bias is added ONCE (ties-away on the
 * magnitude), then the shift. Four always-on guards; a trip OR-sets `status`
 * (if non-NULL) and returns a deterministic fallback. Native uses fp_u128
 * limbs; the portable path builds the same bits from fp_ml_umul64_/fp_addc_. */
#if FP_MATH_HAS_INT128
static inline fp_w128 fp_w_mulshr(fp_w128 X, fp_w128 Y, int S, int rnd, int *status)
{
    if (S < 1 || S > 127) { if (status) *status |= FP_W_ERR_SHIFT; return 0; }
    int neg = ((X < 0) ^ (Y < 0));
    fp_u128 ux = (X < 0) ? ((fp_u128)0 - (fp_u128)X) : (fp_u128)X;
    fp_u128 uy = (Y < 0) ? ((fp_u128)0 - (fp_u128)Y) : (fp_u128)Y;
    uint64_t a0 = (uint64_t)ux, a1 = (uint64_t)(ux >> 64);
    uint64_t b0 = (uint64_t)uy, b1 = (uint64_t)(uy >> 64);
    fp_u128 p00 = (fp_u128)a0 * b0, p01 = (fp_u128)a0 * b1;
    fp_u128 p10 = (fp_u128)a1 * b0, p11 = (fp_u128)a1 * b1;
    fp_u128 M   = p01 + p10;        fp_u128 cM  = (M < p01) ? 1 : 0;
    fp_u128 lo  = p00 + (M << 64);  fp_u128 cLo = (lo < p00) ? 1 : 0;
    fp_u128 hi  = p11 + (M >> 64) + (cM << 64) + cLo;
    if (rnd) { fp_u128 bias = (fp_u128)1 << (S - 1); fp_u128 nlo = lo + bias; if (nlo < lo) hi += 1; lo = nlo; }
    if ((hi >> S) != 0) { if (status) *status |= FP_W_ERR_OVERFLOW;
                          return neg ? fp_w_int128_min() : fp_w_int128_max(); }
    fp_u128 rmag = (lo >> S) | (hi << (128 - S));
    fp_u128 top  = (fp_u128)1 << 127;
    if (!neg && rmag >= top) { if (status) *status |= FP_W_ERR_OVERFLOW; return fp_w_int128_max(); }
    if ( neg && rmag >  top) { if (status) *status |= FP_W_ERR_OVERFLOW; return fp_w_int128_min(); }
    return neg ? (fp_w128)((fp_u128)0 - rmag) : (fp_w128)rmag;
}
#else
static inline fp_w128 fp_w_mulshr(fp_w128 X, fp_w128 Y, int S, int rnd, int *status)
{
    if (S < 1 || S > 127) { if (status) *status |= FP_W_ERR_SHIFT; return fp_w_from_i64(0); }
    int neg = ((X.hi >> 63) ^ (Y.hi >> 63)) & 1;
    fp_w128 mx = (X.hi >> 63) ? fp_w_neg(X) : X;
    fp_w128 my = (Y.hi >> 63) ? fp_w_neg(Y) : Y;
    uint64_t a0 = mx.lo, a1 = mx.hi, b0 = my.lo, b1 = my.hi;
    uint64_t p00l, p00h, p01l, p01h, p10l, p10h, p11l, p11h;
    fp_ml_umul64_(a0, b0, &p00l, &p00h);
    fp_ml_umul64_(a0, b1, &p01l, &p01h);
    fp_ml_umul64_(a1, b0, &p10l, &p10h);
    fp_ml_umul64_(a1, b1, &p11l, &p11h);
    uint64_t r0 = p00l, r1 = p00h, r2 = p11l, r3 = p11h, cy;
    cy = 0; r1 = fp_addc_(r1, p01l, &cy); r2 = fp_addc_(r2, p01h, &cy); r3 = fp_addc_(r3, 0, &cy);
    cy = 0; r1 = fp_addc_(r1, p10l, &cy); r2 = fp_addc_(r2, p10h, &cy); r3 = fp_addc_(r3, 0, &cy);
    if (rnd) {
        cy = 0;
        if (S - 1 < 64) { uint64_t bias = (uint64_t)1 << (S - 1);
            r0 = fp_addc_(r0, bias, &cy); r1 = fp_addc_(r1, 0, &cy);
            r2 = fp_addc_(r2, 0, &cy);    r3 = fp_addc_(r3, 0, &cy); }
        else { uint64_t bias = (uint64_t)1 << (S - 1 - 64);
            r1 = fp_addc_(r1, bias, &cy); r2 = fp_addc_(r2, 0, &cy); r3 = fp_addc_(r3, 0, &cy); }
    }
    /* overflow guard (b): (r3:r2) >> S != 0 */
    uint64_t h0, h1;
    if (S < 64)      { h0 = (r2 >> S) | (r3 << (64 - S)); h1 = r3 >> S; }
    else if (S == 64){ h0 = r3; h1 = 0; }
    else             { int s2 = S - 64; h0 = r3 >> s2; h1 = 0; }
    if ((h0 | h1) != 0) { if (status) *status |= FP_W_ERR_OVERFLOW;
                          return neg ? fp_w_int128_min() : fp_w_int128_max(); }
    /* rmag = low 128 bits of (r3:r2:r1:r0) >> S */
    fp_w128 rmag;
    if (S < 64)      { rmag.lo = (r0 >> S) | (r1 << (64 - S)); rmag.hi = (r1 >> S) | (r2 << (64 - S)); }
    else if (S == 64){ rmag.lo = r1; rmag.hi = r2; }
    else             { int s2 = S - 64; rmag.lo = (r1 >> s2) | (r2 << (64 - s2)); rmag.hi = (r2 >> s2) | (r3 << (64 - s2)); }
    int ge_top = (rmag.hi >> 63) != 0;                              /* rmag >= 2^127 */
    int gt_top = ge_top && !(rmag.hi == ((uint64_t)1 << 63) && rmag.lo == 0); /* rmag > 2^127 */
    if (!neg && ge_top) { if (status) *status |= FP_W_ERR_OVERFLOW; return fp_w_int128_max(); }
    if ( neg && gt_top) { if (status) *status |= FP_W_ERR_OVERFLOW; return fp_w_int128_min(); }
    return neg ? fp_w_neg(rmag) : rmag;
}
#endif

/* Named narrowing F=80 → Q16.48 (the SINGLE narrowing point; ties-away).
 * The only place a wide D-SA accumulator becomes a Q16.48 value. */
static inline fixed_t fp_w_dsa_narrow(fp_w128 w) { return (fixed_t)fp_w_to_i64(fp_w_wsr(w, FP_DSA_K, FP_RND_NEAR, (int *)0)); }

/* ================================================================== */
/*  Basic Q16.48 arithmetic (vendored)                                 */
/* ================================================================== */

static inline fixed_t fp_mul(fixed_t a, fixed_t b)
{
    return (fixed_t)fp_w_to_i64(fp_w_asr(fp_w_muls(a, b), FP_PRECISION));
}

static inline fixed_t fp_div(fixed_t a, fixed_t b)
{
    uint64_t mag = (a < 0) ? -(uint64_t)a : (uint64_t)a;
    fp_w128 num = fp_w_shl(fp_w_from_u64(mag), FP_PRECISION);
    if (a < 0) num = fp_w_neg(num);
    return (fixed_t)fp_w_to_i64(fp_w_divs(num, fp_w_from_i64(b)));
}

static inline fixed_t fp_abs(fixed_t a) { return a >= 0 ? a : -a; }

static inline fixed_t fp_from_int(int x)
{
    uint64_t mag = (x < 0) ? -(uint64_t)x : (uint64_t)x;
    fp_w128 r = fp_w_shl(fp_w_from_u64(mag), FP_PRECISION);
    if (x < 0) r = fp_w_neg(r);
    return (fixed_t)fp_w_to_i64(r);
}

static inline fixed_t fp_max(fixed_t a, fixed_t b) { return a > b ? a : b; }
static inline fixed_t fp_min(fixed_t a, fixed_t b) { return a < b ? a : b; }

/* Display/testing ONLY — never called by library code (kept out of the
 * float-symbol audit surface: no TU that must be float-free references it). */
static inline double fp_to_double(fixed_t x) { return (double)x / (double)FP_ONE; }

/* ================================================================== */
/*  Mixed-format time helpers (Q26.38 × Q16.48 / F=80 → wide)         */
/* ================================================================== */

/* minutes (int) → Q26.38. Fit proof: |minutes| < 2^25 keeps |raw| < 2^63.
 * UB-free: shift the unsigned magnitude, then restore the sign (a signed
 * left-shift of a negative value is UB, C99 6.5.7p4). */
static inline fp_tmin_t fp_tmin_from_min(int64_t minutes)
{
    uint64_t mag = (minutes < 0) ? -(uint64_t)minutes : (uint64_t)minutes;
    uint64_t raw = mag << FP_TMIN_FRAC;
    return (fp_tmin_t)((minutes < 0) ? (uint64_t)0 - raw : raw);
}

/* wide(F=80 D-SA coeff) × Q26.38 time → wide(F=80), overflow-safe.
 * Raw product is value·2^(80+38); the result is value·2^80, so shift by 38.
 * This is a Horner step: coeff stays wide across every ×t.
 * Per-step renormalization to Q16.48 uses fp_w_dsa_narrow at the END only. */
static inline fp_w128 fp_w_mul_tmin(fp_w128 wcoef_f80, fp_tmin_t t, int *status)
{
    return fp_w_mulshr(wcoef_f80, fp_w_from_i64((int64_t)t), FP_TMIN_FRAC, FP_RND_NEAR, status);
}

/* Q16.48 value promoted to F=80 wide (exact left shift by K). */
static inline fp_w128 fp_w_q48_to_f80(fixed_t q) { return fp_w_shl(fp_w_from_i64(q), FP_DSA_K); }

/* ================================================================== */
/*  Square root / inverse square root (vendored)                       */
/* ================================================================== */

static inline fixed_t fp_sqrt(fixed_t x)
{
    if (x <= 0) return 0;
    fp_w128 n = fp_w_shl(fp_w_from_u64((uint64_t)x), FP_PRECISION);
    int bw = fp_w_bits(n);
    uint64_t r = (uint64_t)1 << ((bw + 1) / 2);
    for (int i = 0; i < 7; i++) {
        if (r == 0) return 0;
        uint64_t q = (uint64_t)fp_w_to_i64(fp_w_divs(n, fp_w_from_u64(r)));
        uint64_t nr = (r + q) >> 1;
        if (nr >= r) break;
        r = nr;
    }
    while (fp_w_cmp(fp_w_muls((int64_t)r, (int64_t)r), n) > 0) r--;
    return (fixed_t)r;
}

static inline fixed_t fp_inv_sqrt(fixed_t x)
{
    if (x <= 0) return 0;
    int msb = 63 - __builtin_clzll((uint64_t)x);
    int k = (msb - 48) >> 1;
    int64_t xn = (k >= 0) ? (x >> (2 * k)) : (x << (-2 * k));
    int msb_n = 63 - __builtin_clzll((uint64_t)xn);
    int64_t y = (msb_n == 48) ? ((int64_t)1 << FP_PRECISION) : ((int64_t)1 << (FP_PRECISION - 1));
    int64_t three = fp_from_int(3);
    for (int i = 0; i < 8; i++) {
        int64_t y2  = fp_w_to_i64(fp_w_asr(fp_w_muls(y, y), FP_PRECISION));
        int64_t xy2 = fp_w_to_i64(fp_w_asr(fp_w_muls(xn, y2), FP_PRECISION));
        int64_t factor = three - xy2;
        y = fp_w_to_i64(fp_w_asr(fp_w_muls(y, factor), FP_PRECISION + 1));
        if (y <= 0) { y = 1; break; }
    }
    if (k >= 0) { int64_t r = y >> k; return (fixed_t)(r < 0 ? 0 : r); }
    fp_w128 r = fp_w_shl(fp_w_from_i64(y), -k);
    if (fp_w_cmp(r, fp_w_from_i64(INT64_MAX)) > 0) return INT64_MAX;
    if (fp_w_cmp(r, fp_w_from_i64(0)) < 0) return 0;
    return (fixed_t)fp_w_to_i64(r);
}

/* ================================================================== */
/*  Const CORDIC tables — no initialization, zero mutable globals     */
/* ================================================================== */

/* ==== BEGIN GENERATED CORDIC CONST BLOCK (gen_cordic_tables.py) ==== */
/* Q16.48 literals; replay of the integer Machin/Taylor/Newton
 * derivation (no float). SHA-256 of the emitted decimal payload:
 *   ce652b2a3aee25c40f33b9d8153478f1d23e772d23cc89fdbfe00c56d297666d
 * Regenerate + diff to anti-drift these constants. Do not hand-edit. */
#define FP_CORDIC_N 48
static const fixed_t FP_PI          = 884279719003552LL;
static const fixed_t fp_cordic_gain = 170926505739103LL; /* K^-1 ~ 0.6072529 */
static const fixed_t fp_cordic_angles[FP_CORDIC_N] = {
    221069929750888LL, 130505199945452LL, 68955363498242LL, 35002819193904LL,
    17569333089920LL, 8793231387229LL, 4397688649582LL, 2198978517948LL,
    1099506035422LL, 549755114839LL, 274877819563LL, 137438942550LL,
    68719475371LL, 34359738198LL, 17179869163LL, 8589934590LL,
    4294967296LL, 2147483648LL, 1073741824LL, 536870912LL,
    268435456LL, 134217728LL, 67108864LL, 33554432LL,
    16777216LL, 8388608LL, 4194304LL, 2097152LL,
    1048576LL, 524288LL, 262144LL, 131072LL,
    65536LL, 32768LL, 16384LL, 8192LL,
    4096LL, 2048LL, 1024LL, 512LL,
    256LL, 128LL, 64LL, 32LL,
    16LL, 8LL, 4LL, 2LL,
};
/* ==== END GENERATED CORDIC CONST BLOCK ==== */

/* CORDIC rotation: (cos, sin) via shifts and adds. No init (const tables). */
static inline void fp_sincos(fixed_t angle, fixed_t *cos_out, fixed_t *sin_out)
{
    fixed_t two_pi = FP_PI << 1;
    while (angle > FP_PI) angle -= two_pi;
    while (angle < -FP_PI) angle += two_pi;

    fixed_t pi_half = FP_PI >> 1;
    int negate_cos = 0;
    if (angle > pi_half) { angle = FP_PI - angle; negate_cos = 1; }
    else if (angle < -pi_half) { angle = -FP_PI - angle; negate_cos = 1; }

    fixed_t x = fp_cordic_gain, y = 0, z = angle;
    for (int i = 0; i < FP_CORDIC_N; i++) {
        fixed_t xs = x >> i, ys = y >> i;
        if (z >= 0) { fixed_t xn = x - ys; y = y + xs; z -= fp_cordic_angles[i]; x = xn; }
        else        { fixed_t xn = x + ys; y = y - xs; z += fp_cordic_angles[i]; x = xn; }
    }
    *cos_out = negate_cos ? -x : x;
    *sin_out = y;
}

static inline fixed_t fp_sin(fixed_t a) { fixed_t c, s; fp_sincos(a, &c, &s); return s; }
static inline fixed_t fp_cos(fixed_t a) { fixed_t c, s; fp_sincos(a, &c, &s); return c; }

/* ================================================================== */
/*  Inverse trigonometry: CORDIC vectoring mode                        */
/* ================================================================== */

/* |v| with INT64_MIN saturated to INT64_MAX. Negating INT64_MIN is signed-overflow
 * UB (C99 6.5p5) — the magnitude is formed in unsigned and clamped, so the whole
 * inverse-trig surface is TOTAL over int64. Bit-identical to `v < 0 ? -v : v` for
 * every value except INT64_MIN, where that expression had no defined result at all;
 * the one-raw difference is far below the CORDIC's resolution at 2^63. */
static inline fixed_t fp_abs_sat(fixed_t v)
{
    uint64_t m = (v < 0) ? (uint64_t)0 - (uint64_t)v : (uint64_t)v;
    if (m > (uint64_t)0x7fffffffffffffffULL) m = (uint64_t)0x7fffffffffffffffULL;
    return (fixed_t)m;
}

/* Largest magnitude admitted into the vectoring pass below without pre-scaling.
 *
 * Overflow bound:
 * With x0, y0 >= 0 and M = max(x0, y0), the pass is a sequence of Givens rotations:
 * |(x_i, y_i)| = |(x0, y0)| * prod_{k<i} sqrt(1 + 4^-k) <= G * sqrt(2) * M, where
 * G = 1.6467602581... is the CORDIC gain. The largest quantity FORMED is x_i + ys
 * (or y_i - xs), and by Cauchy-Schwarz |x| + |y|/2^i <= sqrt(1 + 4^-i) * |(x,y)|
 * <= sqrt(1.25) * G * sqrt(2) * M ~= 2.60 * M. At M = 2^61 that is <= 6.0e18, inside
 * INT64_MAX = 9.223e18 (measured worst case over a 2M-case adversarial sweep of the
 * quadrant at M = 2^61: 5.370e18 = 2.329*M, headroom 1.72x; at 2^62 it overflows).
 *
 * Larger inputs are scaled down before the vectoring pass. */
#define FP_ATAN2_SAFE ((fixed_t)1 << 61)

/* atan2 over the first quadrant (x,y >= 0) in [0, pi/2] — one 48-iteration
 * vectoring pass over the same const angle table. TOTAL over the non-negative
 * int64 domain: an out-of-window pair is pre-scaled by an exact power of two,
 * which atan2 is invariant under, so no input can overflow the accumulator. The
 * pre-scale engages only above 2^61 (8192.0 in Q16.48) — 4x above the Q16.48
 * ceiling of any value this library can present — and costs at most two shifts. */
static inline fixed_t fp_atan2_acute_(fixed_t y, fixed_t x)
{
    if (x == 0 && y == 0) return 0;
    while (x > FP_ATAN2_SAFE || y > FP_ATAN2_SAFE) { x >>= 1; y >>= 1; }
    fixed_t z = 0;
    for (int i = 0; i < FP_CORDIC_N; i++) {
        fixed_t xs = x >> i, ys = y >> i;
        if (y >= 0) { x += ys; y -= xs; z += fp_cordic_angles[i]; }
        else        { x -= ys; y += xs; z -= fp_cordic_angles[i]; }
    }
    return z;
}

/* atan2(y, x) over all four quadrants, result clamped to [-pi, pi]. The final
 * clamp special-cases the CORDIC axis bias (a one-raw-ULP overshoot at exact
 * axes, e.g. the y<0,x<0 endpoint could read FP_PI+1) so the documented range
 * holds exactly.
 *
 * Angular resolution also depends on the raw input magnitudes: small values
 * lose information during the vectoring shifts. A Q16.48 return type alone
 * does not imply 48 bits of useful angular precision for every input pair.
 * Callers needing more precision may scale both inputs by the same positive
 * power of two, choosing the shift from max(|x|,|y|) so the scaled maximum
 * lies in [2^60,2^61). Use unsigned magnitudes and avoid signed overflow.
 * This function scales large values down for safety but does not scale small
 * values up; input normalization is an explicit caller choice. */
static inline fixed_t fp_atan2(fixed_t y, fixed_t x)
{
    if (y == 0) return x >= 0 ? 0 : FP_PI;                 /* exact horizontal axis */
    if (x == 0) return y > 0 ? (FP_PI >> 1) : -(FP_PI >> 1); /* exact vertical axis */
    /* Saturating magnitudes: the plain `x < 0 ? -x : x` is UB at INT64_MIN,
     * the one input for which the acute pass then ran on a NEGATIVE "magnitude". */
    fixed_t ax = fp_abs_sat(x), ay = fp_abs_sat(y);
    fixed_t a = fp_atan2_acute_(ay, ax);
    fixed_t r = (x >= 0) ? (y >= 0 ? a : -a) : (y >= 0 ? FP_PI - a : -FP_PI + a);
    if (r >  FP_PI) r =  FP_PI;
    if (r < -FP_PI) r = -FP_PI;
    return r;
}

/* asin(s), |s| clamped to 1. asin = atan2(|s|, sqrt((1-s)(1+s))). The endpoints
 * s = ±1 return exactly ±(FP_PI>>1): the same endpoint clamp used on the
 * atan2/acos axes, absorbing the CORDIC one-raw-ULP bias so the documented
 * [-pi/2, pi/2] range holds exactly (asin(+1) would else read (FP_PI>>1)+1). */
static inline fixed_t fp_asin(fixed_t s)
{
    if (s > FP_ONE)  s = FP_ONE;
    if (s < -FP_ONE) s = -FP_ONE;
    if (s == 0) return 0;                       /* exact center (removes axis bias) */
    if (s == FP_ONE)  return  (FP_PI >> 1);     /* exact +pi/2 endpoint  */
    if (s == -FP_ONE) return -(FP_PI >> 1);     /* exact -pi/2 endpoint  */
    fixed_t as = s < 0 ? -s : s;
    fixed_t c = fp_sqrt(fp_mul(FP_ONE - as, FP_ONE + as));
    fixed_t a = fp_atan2_acute_(as, c);
    return s < 0 ? -a : a;
}

/* acos(c) = pi/2 - asin(c), |c| clamped to 1; result clamped to [0, pi]. The
 * endpoint clamp absorbs the CORDIC axis bias at c = ±1 (acos(+1) would else
 * read -1, acos(-1) FP_PI+1) so the documented [0, pi] range holds exactly. */
static inline fixed_t fp_acos(fixed_t c)
{
    fixed_t r = (FP_PI >> 1) - fp_asin(c);
    if (r < 0)     r = 0;
    if (r > FP_PI) r = FP_PI;
    return r;
}

/* ================================================================== */
/*  Q2.30 <-> Q16.48 boundary converters                              */
/* ================================================================== */

#define FP_Q30_FRAC 30
#define FP_Q30_ONE  ((int32_t)1 << FP_Q30_FRAC)

/* Widen Q2.30 → Q16.48 by an EXACT <<18, UB-free (a signed left shift of a
 * negative is C99 6.5.7p4 undefined): shift the unsigned magnitude, restore the
 * sign. Exact for every int32, INT32_MIN included (2^31<<18 = 2^49 fits). */
static inline fixed_t fp_q30_to_q48(int32_t v)
{
    uint64_t mag = (v < 0) ? (uint64_t)0 - (uint64_t)v : (uint64_t)v;
    uint64_t raw = mag << (FP_PRECISION - FP_Q30_FRAC);   /* <<18, exact */
    return (fixed_t)((v < 0) ? (uint64_t)0 - raw : raw);
}
/* Narrow Q16.48 → Q2.30 by a >>18, round-to-nearest ties away from zero:
 * magnitude-based, so the tie is symmetric and the negative
 * arithmetic-shift down-bias is avoided — bias, shift, restore sign. */
static inline int32_t fp_q48_to_q30(fixed_t v)
{
    uint64_t mag  = (v < 0) ? (uint64_t)0 - (uint64_t)v : (uint64_t)v;
    uint64_t half = (uint64_t)1 << (FP_PRECISION - FP_Q30_FRAC - 1);   /* 2^17 */
    uint64_t q    = (mag + half) >> (FP_PRECISION - FP_Q30_FRAC);      /* >>18 */
    return (int32_t)((v < 0) ? (uint64_t)0 - q : q);
}

/* ================================================================== */
/*  vec3 / mat3 / quaternion arithmetic in Q16.48                     */
/* ================================================================== */

typedef struct { fixed_t x, y, z; }      fp_vec3;   /* Q16.48 */
typedef struct { fixed_t q[4]; }         fp_quat;   /* [x,y,z,w] Q16.48 */
typedef struct { fixed_t m[9]; }         fp_mat3;   /* row-major Q16.48 */

static inline fixed_t fp_vec3_dot(const fp_vec3 *a, const fp_vec3 *b)
{
    return fp_mul(a->x, b->x) + fp_mul(a->y, b->y) + fp_mul(a->z, b->z);
}

static inline void fp_vec3_cross(const fp_vec3 *a, const fp_vec3 *b, fp_vec3 *o)
{
    fixed_t x = fp_mul(a->y, b->z) - fp_mul(a->z, b->y);
    fixed_t y = fp_mul(a->z, b->x) - fp_mul(a->x, b->z);
    fixed_t z = fp_mul(a->x, b->y) - fp_mul(a->y, b->x);
    o->x = x; o->y = y; o->z = z;
}

/* 1/‖v‖ then scale: unit vector via fp_inv_sqrt (no divide). For bounded
 * (unit-scale) inputs the dot stays well inside Q16.48. */
static inline void fp_vec3_normalize(const fp_vec3 *v, fp_vec3 *o)
{
    fixed_t n2 = fp_vec3_dot(v, v);
    fixed_t inv = fp_inv_sqrt(n2);
    o->x = fp_mul(v->x, inv); o->y = fp_mul(v->y, inv); o->z = fp_mul(v->z, inv);
}

/* Hamilton product, scalar-last [x,y,z,w]. */
static inline void fp_quat_mul(const fp_quat *a, const fp_quat *b, fp_quat *o)
{
    fixed_t ax = a->q[0], ay = a->q[1], az = a->q[2], aw = a->q[3];
    fixed_t bx = b->q[0], by = b->q[1], bz = b->q[2], bw = b->q[3];
    fixed_t x = fp_mul(aw, bx) + fp_mul(ax, bw) + fp_mul(ay, bz) - fp_mul(az, by);
    fixed_t y = fp_mul(aw, by) - fp_mul(ax, bz) + fp_mul(ay, bw) + fp_mul(az, bx);
    fixed_t z = fp_mul(aw, bz) + fp_mul(ax, by) - fp_mul(ay, bx) + fp_mul(az, bw);
    fixed_t w = fp_mul(aw, bw) - fp_mul(ax, bx) - fp_mul(ay, by) - fp_mul(az, bz);
    o->q[0] = x; o->q[1] = y; o->q[2] = z; o->q[3] = w;
}

static inline void fp_quat_normalize(const fp_quat *a, fp_quat *o)
{
    fixed_t n2 = fp_mul(a->q[0], a->q[0]) + fp_mul(a->q[1], a->q[1])
               + fp_mul(a->q[2], a->q[2]) + fp_mul(a->q[3], a->q[3]);
    fixed_t inv = fp_inv_sqrt(n2);
    for (int i = 0; i < 4; i++) o->q[i] = fp_mul(a->q[i], inv);
}

/* Method-C mat3 kernels (Q16.48, row-major) — the bounded rotation substrate for
 * P4/P5 frames/attitude. Consistent with the vec3/quat set: every product
 * renormalizes through fp_mul, so on unit-scale operands intermediates stay in
 * Q16.48. Exercised in the golden schedule so determinism covers them. */
static inline void fp_mat3_mulv(const fp_mat3 *M, const fp_vec3 *v, fp_vec3 *o)  /* o = M·v */
{
    fixed_t x = fp_mul(M->m[0], v->x) + fp_mul(M->m[1], v->y) + fp_mul(M->m[2], v->z);
    fixed_t y = fp_mul(M->m[3], v->x) + fp_mul(M->m[4], v->y) + fp_mul(M->m[5], v->z);
    fixed_t z = fp_mul(M->m[6], v->x) + fp_mul(M->m[7], v->y) + fp_mul(M->m[8], v->z);
    o->x = x; o->y = y; o->z = z;
}
static inline void fp_mat3_mul(const fp_mat3 *A, const fp_mat3 *B, fp_mat3 *o)  /* o = A·B, alias-safe */
{
    fp_mat3 r;
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 3; j++)
            r.m[i * 3 + j] = fp_mul(A->m[i * 3 + 0], B->m[0 * 3 + j])
                           + fp_mul(A->m[i * 3 + 1], B->m[1 * 3 + j])
                           + fp_mul(A->m[i * 3 + 2], B->m[2 * 3 + j]);
    *o = r;
}
static inline void fp_mat3_transpose(const fp_mat3 *M, fp_mat3 *o)  /* o = Mᵀ, alias-safe */
{
    fp_mat3 r;
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 3; j++) r.m[i * 3 + j] = M->m[j * 3 + i];
    *o = r;
}

/* Sine/versine-domain agreement metric: 1 - |q·q_ref| in Q16.48,
 * sign-invariant (q and -q represent the same rotation). */
static inline fixed_t fp_quat_align_err(const fp_quat *a, const fp_quat *b)
{
    fixed_t d = fp_mul(a->q[0], b->q[0]) + fp_mul(a->q[1], b->q[1])
              + fp_mul(a->q[2], b->q[2]) + fp_mul(a->q[3], b->q[3]);
    if (d < 0) d = -d;
    return FP_ONE - d;
}

#endif /* FP_MATH_H */
