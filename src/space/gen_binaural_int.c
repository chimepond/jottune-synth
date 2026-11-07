/*
 * Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 *
 * gen_binaural_int.c — the binaural decoder's filters : a host tool
 * held to the library's arithmetic. It prints sp_binaural_tables.h, the
 * left ear's MagLS filters (Schoerkhuber, Zaunschirm and Hoeldrich 2018)
 * for the first- and third-order fields at 44.1 and 48 kHz, from the
 * vendored fp_math.h alone: no float, no libm. It computes the double
 * design of src/host/music/binaural_ab.py (head, ear, sh, grid, magls,
 * decoders) step for step; `binaural_ab.py design-check` compares the two.
 *   head  Brown and Duda's sphere: a / c = 0.0875 m / 343 m/s = 1/3920 s,
 *         the ears at +-100 degrees; the shadow beta = alpha(theta) /
 *         alpha(100 degrees) = (42 + 38 cos 1.2 theta) / 23 as the shelf
 *         beta + (1 - beta) LP, LP the one-pole low pass 1 / (1 + j KB
 *         tan(w/2)) of the bilinear transform, KB = 23 fs / 156800;
 *         Woodworth's delay T = (1 + td) fs / 3920 samples (td = -cos theta
 *         facing the ear, theta - pi/2 behind it); a latency of T0 = 128
 *         samples; and 1/sqrt 2.
 *   grid  32 Gauss-Legendre rows (Newton's method on P_32) by 64 azimuths.
 *   fit   SN3D harmonics in ACN order on AmbiX axes; the Gram matrix
 *         factored once per order (Cholesky) into the least-squares
 *         projection; below bin kc, the bin nearest N c / (2 pi a), the fit
 *         of the complex responses, above it the fit of their magnitudes,
 *         each bin's phase the last bin's decoded phase plus the omni
 *         filter's mean phase step over its last five bins below kc.
 *   taps  the 512-point inverse DFT summed directly (exact in 128 bits, one
 *         rounding per tap), cut to 256 taps with a half-Hann over the last
 *         32, rounded half away from zero to Q2.14: three of the four
 *         designs peak above 1 (1.07 to 1.22), out of Q1.15's reach.
 * Values are Q16.48; products and sums are exact in 128 bits, and every
 * narrowing rounds half away from zero, so the arithmetic is odd-symmetric
 * and the right ear's design is the left's with the channels odd under
 * mirroring (m < 0) negated, bit for bit: `mirror` designs it and compares.
 * Usage: gen_binaural_int          > sp_binaural_tables.h
 *        gen_binaural_int q48      the cut taps in Q16.48, before rounding
 *        gen_binaural_int mirror   design the right ear too and compare
 */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "../../vendor/fp_math.h"

#define NZ     32                   /* Gauss-Legendre rows */
#define NA     64                   /* azimuths */
#define NQ     (NZ * NA)            /* grid points */
#define NFFT   512                  /* the design's length */
#define NB     (NFFT / 2 + 1)       /* its bins */
#define TAPS   256                  /* the filters' length */
#define TAPER  32                   /* the half-Hann's length */
#define SHIFT  14                   /* Q2.14 */
#define KMAX   32                   /* above every kc here */
#define CMAX   16                   /* channels at order 3 */

typedef fixed_t fx;

static const int rates[2] = { 44100, 48000 };

static int w_status;                    /* fp_w_* guard bits: any set bit fails the run */
static int w_range;                     /* a value outside int64, or a division by zero */

static fx cs[1024], sn[1024];           /* cos and sin of 2 pi n / 1024 */
static fx cos100, sin100, rhalf;        /* the ear's azimuth; sqrt(1/2) */
static fx win[TAPER];                   /* the half-Hann */
static fx gz[NZ], gwt[NZ], gc[NZ];      /* the rows: z, weight, sqrt(1 - z^2) */
static fx Y[NQ][CMAX], WY[NQ][CMAX];    /* the harmonics on the grid, and times the weights */
static fx P1[4][NQ], P3[CMAX][NQ];      /* the least-squares projections */
static fx lpr[NB], lpi[NB];             /* the shelf's low pass at each bin */
static fx hb[NQ], ht[NQ];               /* each point's beta and delay T */
static fx hm[NB][NQ];                   /* |H|, the ear's magnitude */
static fx hre[KMAX][NQ], him[KMAX][NQ]; /* H below KMAX */
static fx Dr[NB][CMAX], Di[NB][CMAX];   /* the filters' spectra */
static fx tr[NQ], ti[NQ];               /* a bin's target */
static fx out1[2][2][4][TAPS];          /* [ear][fs][ACN][tap], Q16.48 */
static fx out3[2][2][CMAX][TAPS];
static int kcs[2][2];                   /* [order 1, 3][fs] */

static fx to64(fp_w128 x)
{
    if (fp_w_cmp(x, fp_w_from_i64(INT64_MAX)) > 0 || fp_w_cmp(x, fp_w_from_i64(-INT64_MAX)) < 0)
        w_range = 1;
    return fp_w_to_i64(x);
}

/* round(x / 2^s), ties away from zero: odd-symmetric */
static fx nar(fp_w128 x, int s) { return to64(fp_w_wsr(x, s, FP_RND_NEAR, &w_status)); }
static fx mulr(fx a, fx b) { return nar(fp_w_muls(a, b), 48); }
static fp_w128 wide(fx a) { return fp_w_muls(a, FP_ONE); }          /* Q16.48 to Q32.96 */
static fp_w128 wabs(fp_w128 x) { return fp_w_is_neg(x) ? fp_w_neg(x) : x; }

/* round(n / d), ties away from zero: odd-symmetric */
static fp_w128 wdivr(fp_w128 n, fp_w128 d)
{
    fp_w128 ad = wabs(d), q;
    if (fp_w_bits(ad) == 0) {
        w_range = 1;
        return fp_w_from_i64(0);
    }
    q = fp_w_divs(fp_w_add(wabs(n), fp_w_asr(ad, 1)), ad);
    return fp_w_is_neg(n) != fp_w_is_neg(d) ? fp_w_neg(q) : q;
}
static fx divq(fp_w128 n, fx d) { return to64(wdivr(n, fp_w_from_i64(d))); }   /* Q32.96 / Q16.48 */
static fx divi(fp_w128 n, int64_t d) { return to64(wdivr(n, fp_w_from_i64(d))); }

/* floor(sqrt(n)) for 0 <= n < 2^126, digit by digit */
static fx isqrt_w(fp_w128 n)
{
    fp_w128 r = fp_w_from_i64(0), bit;
    int b = fp_w_bits(n);
    if (fp_w_is_neg(n) || b == 0)
        return 0;
    bit = fp_w_shl(fp_w_from_i64(1), (b - 1) & ~1);
    while (fp_w_bits(bit)) {
        fp_w128 t = fp_w_add(r, bit);
        r = fp_w_asr(r, 1);
        if (fp_w_cmp(n, t) >= 0) {
            n = fp_w_sub(n, t);
            r = fp_w_add(r, bit);
        }
        bit = fp_w_asr(bit, 2);
    }
    return fp_w_to_i64(r);
}

/* atan2 of a pair of any size, scaled together to 61 bits first (fp_math.h's
 * idiom), so that the angle resolves to 2^-60 whatever the pair's size */
static fx atan2w(fp_w128 y, fp_w128 x)
{
    int b = fp_w_bits(wabs(y)), bx = fp_w_bits(wabs(x));
    if (bx > b)
        b = bx;
    if (b == 0)
        return 0;
    if (b > 61) {
        y = fp_w_wsr(y, b - 61, FP_RND_NEAR, &w_status);
        x = fp_w_wsr(x, b - 61, FP_RND_NEAR, &w_status);
    } else if (b < 61) {
        y = fp_w_shl(y, 61 - b);
        x = fp_w_shl(x, 61 - b);
    }
    return fp_atan2(fp_w_to_i64(y), fp_w_to_i64(x));
}

/* The circle in 1024 steps: the first octant by fp_sincos, the rest by its
 * symmetries, so that every reflection is exact. */
static void trig_init(void)
{
    fx c80, s80;
    for (int n = 1; n < 128; n++)
        fp_sincos(nar(fp_w_muls(FP_PI, n), 9), &cs[n], &sn[n]);
    cs[0] = FP_ONE;
    sn[0] = 0;
    cs[128] = sn[128] = rhalf = isqrt_w(fp_w_shl(fp_w_from_i64(1), 95));
    for (int n = 129; n <= 256; n++) {
        cs[n] = sn[256 - n];
        sn[n] = cs[256 - n];
    }
    for (int n = 257; n <= 512; n++) {
        cs[n] = -cs[512 - n];
        sn[n] = sn[512 - n];
    }
    for (int n = 513; n < 1024; n++) {
        cs[n] = cs[1024 - n];
        sn[n] = -sn[1024 - n];
    }
    fp_sincos(divi(fp_w_muls(FP_PI, 4), 9), &c80, &s80);       /* 80 degrees */
    cos100 = -c80;
    sin100 = s80;
    /* numpy's hanning(64)[32 + i] = 1/2 + cos(pi (2i + 1) / 63) / 2, 0 at the end */
    for (int i = 0; i < TAPER - 1; i++)
        win[i] = nar(fp_w_from_i64(FP_ONE + fp_cos(divi(fp_w_muls(FP_PI, 2 * i + 1), 63))), 1);
    win[TAPER - 1] = 0;
}

/* P_32(x) and P_31(x) by the three-term recurrence */
static void legendre(fx x, fx *pn, fx *pm)
{
    fx p0 = FP_ONE, p1 = x;
    for (int k = 1; k < NZ; k++) {
        /* (k + 1) P_{k+1} = (2k + 1) x P_k - k P_{k-1} */
        fp_w128 t = fp_w_sub(fp_w_mul(fp_w_muls(x, p1), fp_w_from_i64(2 * k + 1)),
                             fp_w_muls(p0, (int64_t)k << 48));
        fx p2 = to64(wdivr(t, fp_w_from_i64((int64_t)(k + 1) << 48)));
        p0 = p1;
        p1 = p2;
    }
    *pn = p1;
    *pm = p0;
}

static const int64_t fac[7] = { 1, 1, 2, 6, 24, 120, 720 };

/* binaural_ab.py's sh at order 3 for one grid point: ACN, SN3D, no
 * Condon-Shortley phase; z, sqrt(1 - z^2) and the azimuth 2 pi j / 64 */
static void sh_point(fx z, fx cz, int j, fx *y)
{
    static const int dfac[4] = { 1, 1, 3, 15 };            /* (2m - 1)!! */
    for (int m = 0; m <= 3; m++) {
        fx p[4], pmm = FP_ONE;
        int n = 16 * ((m * j) % NA);
        for (int i = 0; i < m; i++)
            pmm = mulr(pmm, cz);
        p[m] = pmm * dfac[m];
        if (m < 3)
            p[m + 1] = nar(fp_w_mul(fp_w_muls(z, p[m]), fp_w_from_i64(2 * m + 1)), 48);
        for (int l = m + 2; l <= 3; l++)        /* (l - m) P_l = (2l - 1) z P_{l-1} - (l + m - 1) P_{l-2} */
            p[l] = to64(wdivr(fp_w_sub(fp_w_mul(fp_w_muls(z, p[l - 1]), fp_w_from_i64(2 * l - 1)),
                                       fp_w_muls(p[l - 2], (int64_t)(l + m - 1) << 48)),
                              fp_w_from_i64((int64_t)(l - m) << 48)));
        for (int l = m; l <= 3; l++) {
            /* sqrt((2 - [m = 0]) (l - m)! / (l + m)!) */
            fx norm = isqrt_w(wdivr(fp_w_shl(fp_w_from_i64((m ? 2 : 1) * fac[l - m]), 96),
                                    fp_w_from_i64(fac[l + m])));
            fx v = mulr(norm, p[l]);
            if (m == 0) {
                y[l * l + l] = v;
            } else {
                y[l * l + l + m] = mulr(v, cs[n]);
                y[l * l + l - m] = mulr(v, sn[n]);
            }
        }
    }
}

/* binaural_ab.py's grid: numpy's leggauss(32) rows, ascending in z, by 64
 * azimuths; each point's harmonics, and those times its weight */
static void grid_init(void)
{
    for (int i = 0; i < NZ / 2; i++) {
        /* the (i + 1)th root from the top, from cos(pi (i + 3/4) / (n + 1/2)) */
        fx x = fp_cos(divi(fp_w_muls(FP_PI, 4 * i + 3), 4 * NZ + 2)), pn, pm, a, b;
        for (int it = 0; it < 64; it++) {
            fx d;
            legendre(x, &pn, &pm);
            /* x -= P_n / P_n', P_n' = n (x P_n - P_{n-1}) / (x^2 - 1) */
            d = divq(fp_w_muls(pn, nar(fp_w_sub(fp_w_muls(x, x), wide(FP_ONE)), 48)), NZ * (mulr(x, pn) - pm));
            x -= d;
            if (d >= -1 && d <= 1)
                break;
        }
        legendre(x, &pn, &pm);
        /* w = 2 (1 - x^2) / (n P_{n-1})^2 */
        a = nar(fp_w_sub(wide(FP_ONE), fp_w_muls(x, x)), 48);
        b = NZ * pm;
        gz[NZ - 1 - i] = x;
        gz[i] = -x;
        gwt[NZ - 1 - i] = gwt[i] = divq(wide(2 * a), mulr(b, b));
    }
    for (int i = 0; i < NZ; i++) {
        fx w = mulr(gwt[i], FP_PI / 32);                        /* times 2 pi / 64 */
        gc[i] = isqrt_w(fp_w_sub(wide(FP_ONE), fp_w_muls(gz[i], gz[i])));
        for (int j = 0; j < NA; j++) {
            int q = i * NA + j;
            sh_point(gz[i], gc[i], j, Y[q]);
            for (int c = 0; c < CMAX; c++)
                WY[q][c] = mulr(Y[q][c], w);
        }
    }
}

/* P = (Y^T W Y)^-1 Y^T W, binaural_ab.py's np.linalg.solve(Y.T @ WY, WY.T),
 * by Cholesky: the Gram matrix's lower triangle, factored once */
static void proj_init(int order, fx (*P)[NQ])
{
    int C = (order + 1) * (order + 1);
    fx G[CMAX][CMAX], L[CMAX][CMAX];
    for (int a = 0; a < C; a++)
        for (int b = 0; b <= a; b++) {
            fp_w128 acc = fp_w_from_i64(0);
            for (int q = 0; q < NQ; q++)
                acc = fp_w_add(acc, fp_w_muls(Y[q][a], WY[q][b]));
            G[a][b] = nar(acc, 48);
        }
    for (int j = 0; j < C; j++) {
        fp_w128 acc = wide(G[j][j]);
        for (int k = 0; k < j; k++)
            acc = fp_w_sub(acc, fp_w_muls(L[j][k], L[j][k]));
        L[j][j] = isqrt_w(acc);
        for (int i = j + 1; i < C; i++) {
            acc = wide(G[i][j]);
            for (int k = 0; k < j; k++)
                acc = fp_w_sub(acc, fp_w_muls(L[i][k], L[j][k]));
            L[i][j] = divq(acc, L[j][j]);
        }
    }
    for (int q = 0; q < NQ; q++) {
        fx y[CMAX], x[CMAX];
        for (int i = 0; i < C; i++) {
            fp_w128 acc = wide(WY[q][i]);
            for (int k = 0; k < i; k++)
                acc = fp_w_sub(acc, fp_w_muls(L[i][k], y[k]));
            y[i] = divq(acc, L[i][i]);
        }
        for (int i = C - 1; i >= 0; i--) {
            fp_w128 acc = wide(y[i]);
            for (int k = i + 1; k < C; k++)
                acc = fp_w_sub(acc, fp_w_muls(L[k][i], x[k]));
            x[i] = divq(acc, L[i][i]);
        }
        for (int i = 0; i < C; i++)
            P[i][q] = x[i];
    }
}

/* binaural_ab.py's head and ear on the grid for one ear (0 left, 1
 * right) at the rate fs: |H| at every bin, H itself below KMAX */
static void head_init(int fs, int ear)
{
    fx kb = divi(fp_w_muls((int64_t)23 * fs, FP_ONE), 156800);
    for (int k = 0; k < NB; k++) {
        /* (1 + z) / ((1 + KB) + (1 - KB) z) = c (c - j KB s) / (c^2 + KB^2 s^2),
         * z = e^-jw, c = cos(w/2), s = sin(w/2) */
        fx c = cs[k], ks = mulr(kb, sn[k]);
        fx den = nar(fp_w_add(fp_w_muls(c, c), fp_w_muls(ks, ks)), 48);
        lpr[k] = divq(fp_w_muls(c, c), den);
        lpi[k] = -divq(fp_w_muls(ks, c), den);
    }
    for (int i = 0; i < NZ; i++)
        for (int j = 0; j < NA; j++) {
            int q = i * NA + j;
            /* the cosine of incidence: the head's axes are (-Y, X, Z) in AmbiX's
             * and the ears at (-+sin 100, cos 100, 0), so c cos(phi -+ 100 degrees) */
            fp_w128 dot = ear == 0 ? fp_w_add(fp_w_muls(cs[16 * j], cos100), fp_w_muls(sn[16 * j], sin100))
                                   : fp_w_sub(fp_w_muls(cs[16 * j], cos100), fp_w_muls(sn[16 * j], sin100));
            fx ci = mulr(gc[i], nar(dot, 48)), si, th, td, cb, sb;
            if (ci > FP_ONE)
                ci = FP_ONE;
            if (ci < -FP_ONE)
                ci = -FP_ONE;
            si = isqrt_w(fp_w_sub(wide(FP_ONE), fp_w_muls(ci, ci)));
            th = atan2w(fp_w_from_i64(si), fp_w_from_i64(ci));
            td = ci > 0 ? -ci : th - (FP_PI >> 1);
            ht[q] = divi(fp_w_muls(FP_ONE + td, fs), 3920);
            fp_sincos(divi(fp_w_muls(th, 6), 5), &cb, &sb);
            hb[q] = divi(fp_w_add(fp_w_muls(42, FP_ONE), fp_w_muls(38, cb)), 23);
        }
    for (int k = 0; k < NB; k++)
        for (int q = 0; q < NQ; q++) {
            fx b1 = FP_ONE - hb[q];
            fx A = hb[q] + mulr(b1, lpr[k]), B = mulr(b1, lpi[k]);
            hm[k][q] = isqrt_w(fp_w_asr(fp_w_add(fp_w_muls(A, A), fp_w_muls(B, B)), 1));
            if (k < KMAX) {
                /* e^-jwT, w T = 2 pi k T / 512: k T / 512 turns, less the nearest whole turn */
                fx turn = nar(fp_w_from_i64((int64_t)k * ht[q]), 9) & (FP_ONE - 1), ce, se, re, im;
                fp_w128 xr, xi;
                if (turn >= FP_HALF)
                    turn -= FP_ONE;
                fp_sincos(-mulr(turn, 2 * FP_PI), &ce, &se);
                xr = fp_w_sub(fp_w_muls(A, ce), fp_w_muls(B, se));
                xi = fp_w_add(fp_w_muls(A, se), fp_w_muls(B, ce));
                re = to64(fp_w_mulshr(xr, fp_w_from_i64(rhalf), 96, FP_RND_NEAR, &w_status));
                im = to64(fp_w_mulshr(xi, fp_w_from_i64(rhalf), 96, FP_RND_NEAR, &w_status));
                switch (k & 3) {                                /* e^-jwT0 = (-j)^k */
                case 0: hre[k][q] = re;  him[k][q] = im;  break;
                case 1: hre[k][q] = im;  him[k][q] = -re; break;
                case 2: hre[k][q] = -re; him[k][q] = -im; break;
                default: hre[k][q] = -im; him[k][q] = re; break;
                }
            }
        }
}

/* binaural_ab.py's magls and the cut in decoders, for the ear head_init
 * last set up: out[ACN][tap] in Q16.48; returns kc */
static int design(int order, int fs, fx out[][TAPS])
{
    int C = (order + 1) * (order + 1), kc;
    fx (*P)[NQ] = order == 1 ? P1 : P3;
    fx ph[6], step, sum = 0;
    /* the bin nearest N c / (2 pi a) = N 1960 / pi Hz */
    kc = (int)((to64(wdivr(fp_w_shl(fp_w_from_i64((int64_t)order * 1960 * NFFT), 96),
                           fp_w_muls(FP_PI, fs))) + FP_HALF) >> 48);
    if (kc < 6 || kc > KMAX)
        w_range = 1;
    for (int k = 0; k < kc; k++)
        for (int c = 0; c < C; c++) {
            fp_w128 ar = fp_w_from_i64(0), ai = fp_w_from_i64(0);
            for (int q = 0; q < NQ; q++) {
                ar = fp_w_add(ar, fp_w_muls(P[c][q], hre[k][q]));
                ai = fp_w_add(ai, fp_w_muls(P[c][q], him[k][q]));
            }
            Dr[k][c] = nar(ar, 48);
            Di[k][c] = nar(ai, 48);
        }
    /* the mean of the omni filter's last five phase steps below kc, as
     * np.mean(np.diff(np.unwrap(np.angle(D[:kc, 0])))[kc - 6:kc - 1]) */
    for (int t = 0; t < 6; t++)
        ph[t] = atan2w(fp_w_from_i64(Di[kc - 6 + t][0]), fp_w_from_i64(Dr[kc - 6 + t][0]));
    for (int t = 0; t < 5; t++) {
        fx d = ph[t + 1] - ph[t];
        if (d >= FP_PI || d <= -FP_PI) {                        /* np.unwrap's rule */
            fx m = (d + FP_PI) % (2 * FP_PI);
            if (m < 0)
                m += 2 * FP_PI;
            m -= FP_PI;
            d = (m == -FP_PI && d > 0) ? FP_PI : m;
        }
        sum += d;
    }
    step = divi(fp_w_from_i64(sum), 5);
    for (int k = kc; k < NB; k++) {
        for (int q = 0; q < NQ; q++) {
            fp_w128 ar = fp_w_from_i64(0), ai = fp_w_from_i64(0);
            fx cc, ss;
            for (int c = 0; c < C; c++) {
                ar = fp_w_add(ar, fp_w_muls(Y[q][c], Dr[k - 1][c]));
                ai = fp_w_add(ai, fp_w_muls(Y[q][c], Di[k - 1][c]));
            }
            fp_sincos(atan2w(ai, ar) + step, &cc, &ss);
            tr[q] = mulr(hm[k][q], cc);
            ti[q] = mulr(hm[k][q], ss);
        }
        for (int c = 0; c < C; c++) {
            fp_w128 ar = fp_w_from_i64(0), ai = fp_w_from_i64(0);
            for (int q = 0; q < NQ; q++) {
                ar = fp_w_add(ar, fp_w_muls(P[c][q], tr[q]));
                ai = fp_w_add(ai, fp_w_muls(P[c][q], ti[q]));
            }
            Dr[k][c] = nar(ar, 48);
            Di[k][c] = nar(ai, 48);
        }
    }
    /* np.fft.irfft(D, 512)[:256], the imaginary parts at bins 0 and 256
     * dropped as irfft drops them; then the half-Hann */
    for (int c = 0; c < C; c++)
        for (int n = 0; n < TAPS; n++) {
            fp_w128 acc = fp_w_add(wide(Dr[0][c]), wide((n & 1) ? -Dr[NB - 1][c] : Dr[NB - 1][c]));
            for (int k = 1; k < NB - 1; k++) {
                int a = 2 * ((k * n) & (NFFT - 1));
                fp_w128 t = fp_w_sub(fp_w_muls(Dr[k][c], cs[a]), fp_w_muls(Di[k][c], sn[a]));
                acc = fp_w_add(acc, fp_w_add(t, t));
            }
            out[c][n] = nar(acc, 48 + 9);
            if (n >= TAPS - TAPER)
                out[c][n] = mulr(out[c][n], win[n - (TAPS - TAPER)]);
        }
    return kc;
}

static int odd(int c)                   /* odd under left-right mirroring: m < 0 */
{
    int l = 0;
    while ((l + 1) * (l + 1) <= c)
        l++;
    return c < l * (l + 1);
}

static int64_t qtap(fx v) { return nar(fp_w_from_i64(v), 48 - SHIFT); }

/* the largest |tap| after rounding over the channels, and its channel;
 * with `least`, the smallest nonzero channel's largest |tap| instead; the
 * channels that round to zero throughout listed in `zero` */
static int64_t peak(int C, fx (*h)[TAPS], int least, int *where, char *zero)
{
    int64_t best = least ? INT64_MAX : 0;
    zero[0] = 0;
    for (int c = 0; c < C; c++) {
        int64_t top = 0;
        for (int n = 0; n < TAPS; n++) {
            int64_t v = qtap(h[c][n]);
            if (v < 0)
                v = -v;
            if (v > top)
                top = v;
        }
        if (top == 0)
            sprintf(zero + strlen(zero), "%s%d", zero[0] ? ", " : "", c);
        else if (least ? top < best : top > best) {
            best = top;
            *where = c;
        }
    }
    return best;
}

static void put_table(const char *name, int C, fx (*h0)[TAPS], fx (*h1)[TAPS], const char *note)
{
    printf("static const int16_t %s[2][%d][SP_BIN_TAPS] = {%s\n", name, C, note);
    for (int f = 0; f < 2; f++) {
        fx (*h)[TAPS] = f ? h1 : h0;
        printf("    {   /* %d Hz */\n", rates[f]);
        for (int c = 0; c < C; c++) {
            printf("        {   /* ACN %d */", c);
            for (int n = 0; n < TAPS; n++)
                printf("%s%d,", (n % 8) ? " " : "\n            ", (int)qtap(h[c][n]));
            printf("\n        },\n");
        }
        printf("    },\n");
    }
    printf("};\n");
}

int main(int argc, char **argv)
{
    int mode = 0;                       /* 0 the header, 1 q48, 2 mirror */
    if (argc == 2 && strcmp(argv[1], "q48") == 0)
        mode = 1;
    else if (argc == 2 && strcmp(argv[1], "mirror") == 0)
        mode = 2;
    else if (argc != 1) {
        fprintf(stderr, "usage: gen_binaural_int [q48 | mirror]\n");
        return 2;
    }
    trig_init();
    grid_init();
    proj_init(1, P1);
    proj_init(3, P3);
    for (int ear = 0; ear < (mode == 2 ? 2 : 1); ear++)
        for (int f = 0; f < 2; f++) {
            head_init(rates[f], ear);
            kcs[0][f] = design(1, rates[f], out1[ear][f]);
            kcs[1][f] = design(3, rates[f], out3[ear][f]);
        }
    if (w_status || w_range) {
        fprintf(stderr, "gen_binaural_int: arithmetic out of range (status %d, range %d)\n", w_status, w_range);
        return 1;
    }
    if (mode == 2) {
        int bad = 0;
        for (int f = 0; f < 2; f++)
            for (int o = 0; o < 2; o++) {
                int C = o ? CMAX : 4, nd = 0;
                for (int c = 0; c < C; c++)
                    for (int n = 0; n < TAPS; n++) {
                        fx l = o ? out3[0][f][c][n] : out1[0][f][c][n];
                        fx r = o ? out3[1][f][c][n] : out1[1][f][c][n];
                        if (r != (odd(c) ? -l : l))
                            nd++;
                    }
                printf("mirror order %d %d Hz: the right ear's design is the left's with ACN %s "
                       "negated: %s (%d of %d taps differ in Q16.48)\n", o ? 3 : 1, rates[f],
                       o ? "1, 4, 5, 9, 10, 11" : "1", nd ? "NO" : "yes", nd, C * TAPS);
                bad += nd;
            }
        return bad ? 1 : 0;
    }
    if (mode == 1) {
        for (int f = 0; f < 2; f++)
            for (int o = 0; o < 2; o++)
                for (int c = 0; c < (o ? CMAX : 4); c++) {
                    printf("Q %d %d %d", o ? 3 : 1, rates[f], c);
                    for (int n = 0; n < TAPS; n++)
                        printf(" %lld", (long long)(o ? out3[0][f][c][n] : out1[0][f][c][n]));
                    printf("\n");
                }
        return 0;
    }
    {
        int64_t pk[2][2], lo[2][2];
        int pc[2][2], lc[2][2];
        char zero[2][2][64];
        for (int f = 0; f < 2; f++) {
            pk[0][f] = peak(4, out1[0][f], 0, &pc[0][f], zero[0][f]);
            lo[0][f] = peak(4, out1[0][f], 1, &lc[0][f], zero[0][f]);
            pk[1][f] = peak(CMAX, out3[0][f], 0, &pc[1][f], zero[1][f]);
            lo[1][f] = peak(CMAX, out3[0][f], 1, &lc[1][f], zero[1][f]);
            for (int o = 0; o < 2; o++)
                if (pk[o][f] > 32767) {
                    fprintf(stderr, "gen_binaural_int: order %d at %d Hz peaks at %lld, outside Q2.14\n",
                            o ? 3 : 1, rates[f], (long long)pk[o][f]);
                    return 1;
                }
        }
        printf("/* sp_binaural_tables.h: the binaural decoder's filters , generated by\n"
               " * space/gen_binaural_int (space/gen_binaural_int.c and vendor/fp_math.h, integers\n"
               " * alone); src/host/music/binaural_ab.py design-check holds them to that script's\n"
               " * double design. Do not edit.\n"
               " *\n"
               " * The left ear's MagLS filters (Schoerkhuber, Zaunschirm and Hoeldrich 2018)\n"
               " * for the first- and third-order fields: ACN order, SN3D, AmbiX axes (X ahead,\n"
               " * Y left, Z up: X = y, Y = -x, Z = z in space/SPEC.md's axes).\n"
               " *   head    Brown and Duda's sphere: a = 0.0875 m, c = 343 m/s, the ears at\n"
               " *           +-100 degrees; the shadow alpha(theta) = 1.05 + 0.95 cos(1.2 theta)\n"
               " *           as a one-pole shelf (bilinear), equalised flat ahead; Woodworth's\n"
               " *           delay; 1/sqrt 2\n"
               " *   grid    32 Gauss-Legendre rows by 64 azimuths\n"
               " *   design  512 points, 128 samples of latency; the least-squares fit of the\n"
               " *           complex responses below bin kc, the bin nearest N c / (2 pi a),\n"
               " *           and of their magnitudes from kc, each bin's phase the last bin's\n"
               " *           decoded phase plus the omni filter's mean step below kc\n"
               " *   cut     256 taps, a half-Hann over the last 32; Q2.14, rounded half away\n"
               " *           from zero (three of the four designs peak above 1, out of\n"
               " *           Q1.15's reach)\n");
        for (int o = 0; o < 2; o++)
            for (int f = 0; f < 2; f++) {
                int kc = kcs[o][f], hz10 = (int)(((int64_t)kc * rates[f] * 10 + NFFT / 2) / NFFT);
                printf(" *   order %d at %d Hz: kc %d (%d.%d Hz); peak %lld (ACN %d), the smallest\n"
                       " *           nonzero channel's peak %lld (ACN %d); zero: ACN %s\n", o ? 3 : 1, rates[f],
                       kc, hz10 / 10, hz10 % 10, (long long)pk[o][f], pc[o][f], (long long)lo[o][f], lc[o][f],
                       zero[o][f]);
            }
        printf(" * The zero channels are the ones odd in Z: the head is symmetric up and down.\n"
               " * Each ear hears the sum over the channels of each channel convolved with its\n"
               " * filter; each coefficient is the tap's value times 2^SP_BIN_SHIFT.\n"
               " */\n"
               "#ifndef SP_BINAURAL_TABLES_H\n#define SP_BINAURAL_TABLES_H\n#include <stdint.h>\n"
               "#define SP_BIN_TAPS 256\n"
               "#define SP_BIN_SHIFT 14                 /* coefficients Q2.14 */\n"
               "static const uint32_t sp_bin_fs[2] = { 44100, 48000 };\n"
               "/* right ear = left negated for m<0 channels: ACN 1, 4, 5, 9, 10, 11 */\n");
        put_table("sp_bin_o1", 4, out1[0][0], out1[0][1], "   /* [fs][ACN][tap], left ear */");
        put_table("sp_bin_o3", CMAX, out3[0][0], out3[0][1], "");
        printf("#endif\n");
    }
    return 0;
}
