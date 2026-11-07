#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Binaural A/B : an excerpt of a piece in four versions for headphones, the double prototype that
the integer field and decoder are later checked against , and a probe of elevation in six. A host
tool: floats are allowed here, never in the library .

    python3 -B tools/music/binaural_ab.py [ITEM[@START] ...] [--length S] [--out DIR] [--ambix]
    python3 -B tools/music/binaural_ab.py design-check HEADER [--directions] [--head]

ITEM is a stereo piece whose merged record and render are in out/music (PIECE.tl and PIECE.wav,
from play.sh or compose.py), or one of two made here, whole whatever START and --length say: bursts,
pink noise from eight directions, then a circle (29.4 s); elevation, pink noise up and down a column
straight ahead, round Brown and Duda's cone 60 degrees to the right, along an arc ahead, and up the
column again with the noise cut at 4 kHz (36.1 s). START is the excerpt's start in seconds, 0 by
default; --length is its length, 30 s by default. With no ITEM it renders the set that README.md,
Binaural A/B, lists. Each item gives these 16-bit WAVs in --out, out/binaural by default:

  ITEM-pan.wav   the stereo module as it is: the harness's render (for bursts and elevation, space.c's
                 integer pan done here)
  ITEM-head.wav  a spherical head (Brown and Duda 1998): per voice and block, each ear's head shadow
                 as a first-order shelf and its delay by Woodworth's formula, equalised flat for a
                 source straight ahead
  ITEM-amb1.wav  the voices in a first-order ambisonic field (ACN/SN3D), decoded to the ears by a
                 MagLS decoder designed on that head (not for elevation)
  ITEM-amb3.wav  the same at third order

and for elevation three more:

  elevation-pinna.wav       the head with Brown and Duda's pinna ahead of the ears' shelves: per voice
                            and block, a sparse FIR of the direct path and five echoes whose delays
                            follow the direction (their eq. 8 and Table I, with the D of their
                            listeners PB and NH)
  elevation-pinna-rd.wav    the same with listener RD's D, the one parameter they fitted per listener
  elevation-amb3-pinna.wav  amb3 decoded by filters designed on the head with that pinna (PB and NH)

--integer adds the integer field route (space/field.c; docs/INSTRUMENT.md, sections 11 to 16), rendered by the
harness that --harness names (the default build's by default): ITEM-int-amb3.wav, the 16 channels of
space amb3 decoded here by amb3's filters, and for elevation ITEM-int-amb3-pinna.wav, Brown and Duda's
pinna per voice (PB and NH's D) ahead of the field; from a harness built with a table (make
BINAURAL_H=PATH), ITEM-int-binaural.wav (and elevation-int-binaural-pinna.wav) as well, the harness's own
decoder. A piece renders its own voices on the route, a made item its stem through the harness's
--stems; the integer field is printed against this script's double one.

The voices are rendered one by one, two per run of the harness (one panned hard left, the other hard
right), and placed block by block where space/space.c places them; the pan version rebuilt from these
stems, against PIECE.wav, shows that they are exact. The versions are faded in and out and matched in
loudness to the pan version (analyze.py's meter, BS.1770-4); if one would peak above -1 dBTP, all of
them go down together. --ambix also writes ITEM-ambix.wav, the third-order field (16 channels, 24 bits,
ACN/SN3D, at amb3's gain or below), for other decoders. Everything needs numpy.

design-check holds HEADER, the sp_binaural_tables.h that space/gen_binaural_int writes from integers alone (or
what its q48 mode prints), to the double design of amb1 and amb3 at 44.1 and 48 kHz: each tap within half a
step, and the decoded magnitude from 50 Hz to 20 kHz within 0.1 dB at 62 directions (exit 1 otherwise; the
gate, check.sh, runs it). --directions prints the error at each direction; --head compares the decoded
responses with the head itself: the magnitude by band and elevation, the ITD and the ILD.
"""
import argparse
import contextlib
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

from analyze import itd_ild, meter

SPIKE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RENDERS = os.path.join(SPIKE, 'out', 'music')
HARNESS = os.path.join(SPIKE, 'build', 's12e10p1v1', 'harness')
ITEMS = ('bursts', 'elevation', 'rivet-line-keys@30', 'rivet-line-keys-lin90@30', 'dubplate@25',
         'crossing@35', 'wind-rose-stereo-rotation@18')
MADE = ('bursts', 'elevation')               # the items made here, not read from out/music
FS = 48000
MARGIN = 0.5                                 # s rendered ahead of the excerpt, where the filters settle
FADE_IN, FADE_OUT = 0.05, 0.2                # s
CEILING = -1.0                               # dBTP

# The head (Brown and Duda 1998): radius, speed of sound, the ears at +-100 degrees, and the shadow's
# alpha(theta) = (1 + a_min / 2) + (1 - a_min / 2) cos(theta 180 / 150), a_min = 0.1.
A_HEAD, C_SOUND = 0.0875, 343.0
A_C = A_HEAD / C_SOUND                       # 255 us
EAR = math.radians(100)
EARS = np.array([[-math.sin(EAR), math.cos(EAR), 0.0],
                 [math.sin(EAR), math.cos(EAR), 0.0]])     # left, right; x right, y ahead, z up


def alpha(theta):
    return 1.05 + 0.95 * np.cos(1.2 * theta)


ALPHA_F = float(alpha(EAR))                  # 0.575: either ear, a source straight ahead
KB = FS * ALPHA_F * A_C                      # 2 fs tau, tau = alpha_f a / 2c: the shelf's pole, bilinear
TAPS, KAISER = 32, 7.0                       # the delays' windowed sinc
D0 = TAPS // 2 - 1                           # 15 samples added to every delay, so that the sinc is causal
PAD = 2 * TAPS                               # zeros ahead of a delay line: the longest delay, 46.5, reads tap 62
T0 = 128                                     # the decoders' latency: their pre-ringing fits, so the cut is small
NFFT, DTAPS = 512, 256                       # the decoders' design length and their length
GRID = (32, 64)                              # Gauss-Legendre rows (elevations) and columns (azimuths)
NF = 4800                                    # the notch checks' FFT length: 10 Hz steps

# Brown and Duda's pinna (their eq. 8 and Table I): the direct path and five echoes, n = 2 to 6, of
# amplitude RHO, delayed tau_n = A_n cos(theta / 2) sin(D_n (90 - phi)) + B_n samples at 44.1 kHz, where
# theta is the lateral angle and phi the elevation round the ears' axis (0 ahead, 90 up). It holds for
# the front half, |phi| <= 90. D is the part they fitted to each listener.
PINNA_RHO = np.array([0.5, -1.0, 0.5, -0.25, 0.25])
PINNA_A = np.array([1.0, 5.0, 5.0, 5.0, 5.0])
PINNA_B = np.array([2.0, 4.0, 7.0, 11.0, 13.0])
D_PB = np.array([1.0, 0.5, 0.5, 0.5, 0.5])   # their listeners PB and NH
D_RD = np.array([0.85, 0.35, 0.35, 0.35, 0.35])           # their listener RD
PINNA_TAPS = 21                              # the longest delay, 18 samples at 44.1 kHz, is 19.6 here
LOW_CUT = 4000.0                             # Hz: the elevation item's last column, without the notches


def lowpass_ir(n=256):
    """The shelf's fixed part, a one-pole low pass by the bilinear transform: its first n samples, which
    hold it to 1e-30."""
    p = (KB - 1) / (KB + 1)
    h = np.empty(n)
    h[0] = 1 / (1 + KB)
    h[1:] = (1 + p) / (1 + KB) * p ** np.arange(n - 1)
    return h


def lowpass(w):
    z = np.exp(-1j * w)
    return (1 + z) / ((1 + KB) + (1 - KB) * z)


def head(u):
    """Each ear's (left, right) high-frequency gain beta against a source straight ahead, and its delay in
    samples (0 facing the ear, 31.5 opposite), for directions u (3, ...): x right, y ahead, z up. A u
    shorter than 1, a source inside `near`, slides toward the centre of the head as the pan's ux does."""
    c = np.clip(np.tensordot(EARS, u, 1), -1.0, 1.0)      # the cosines of incidence at each ear
    th = np.arccos(c)
    td = np.where(th < np.pi / 2, -c, th - np.pi / 2)     # Woodworth, in a/c, against the head's centre
    return alpha(th) / ALPHA_F, (1 + td) * A_C * FS


def ear(beta, T, w):
    """The ear's response at the angular frequencies w: the shelf beta + (1 - beta) lowpass, the delay T
    plus T0, and 1/sqrt 2, the pan's gain straight ahead. Shape (..., len(w))."""
    return ((beta[..., None] + (1 - beta[..., None]) * lowpass(w)) *
            np.exp(-1j * w * (T[..., None] + T0)) / math.sqrt(2))


def pinna_fir(u, D):
    """Brown and Duda's pinna at directions u (3, k), the same for both ears, as a sparse FIR (PINNA_TAPS,
    k): 1 at tap 0, and each echo's RHO shared between the two taps round its delay by linear
    interpolation, as they did it. Behind (phi beyond +-90 degrees), outside the model, only the direct
    path: the bare head."""
    v = u / np.maximum(np.sqrt((u * u).sum(0)), 1e-9)
    th, ph = np.arcsin(np.clip(v[0], -1.0, 1.0)), np.arctan2(v[2], v[1])
    tau = (PINNA_A[:, None] * np.cos(th / 2) * np.sin(D[:, None] * (np.pi / 2 - ph)) + PINNA_B[:, None]) * FS / 44100
    i = np.floor(tau).astype(np.int64)
    k = np.arange(u.shape[1])
    h = np.zeros((PINNA_TAPS, u.shape[1]))
    h[0] = 1.0
    for n in range(5):
        h[i[n], k] += PINNA_RHO[n] * (1 - (tau[n] - i[n]))
        h[i[n] + 1, k] += PINNA_RHO[n] * (tau[n] - i[n])
    back = np.abs(ph) > np.pi / 2
    h[:, back] = 0.0
    h[0, back] = 1.0
    return h


def pinna_resp(u, D, w):
    """The pinna's response (k, len(w)) at directions u (3, k) and angular frequencies w."""
    return pinna_fir(u, D).T @ np.exp(-1j * np.outer(np.arange(PINNA_TAPS), w))


def pinna(x, u, D, K):
    """x (blocks x K samples) through the pinna at the directions u (3, blocks + 1), each tap's weight
    ramped over the block from the last boundary's to this one's: the same as fading from the last
    boundary's FIR to this one's."""
    h = pinna_fir(u, D)
    y = np.zeros(len(x))
    for k in np.flatnonzero(np.abs(h).max(1)):
        y[k:] += ramp(h[k], K)[k:] * x[:len(x) - k]
    return y


def sh(order, u):
    """Real spherical harmonics to `order` (ACN, SN3D, no Condon-Shortley phase) at u (3, ...) in AmbiX
    axes: X ahead, Y left, Z up. Order l scales with |u|^l, the homogeneous polynomial, so a source
    inside `near` fades to the omni channel as the pan's ux fades to the centre."""
    rho = np.sqrt((u * u).sum(0))
    v = u / np.where(rho > 0, rho, 1.0)
    phi, z = np.arctan2(v[1], v[0]), v[2]
    cz = np.sqrt(np.maximum(0.0, 1 - z * z))
    out = np.empty(((order + 1) ** 2,) + rho.shape)
    for m in range(order + 1):
        P = [np.prod(np.arange(1.0, 2 * m, 2)) * cz ** m]                     # P_m^m
        for l in range(m + 1, order + 1):
            P.append(((2 * l - 1) * z * P[-1] - (l + m - 1) * (P[-2] if l > m + 1 else 0)) / (l - m))
        for l in range(m, order + 1):
            y = math.sqrt((2 - (m == 0)) * math.factorial(l - m) / math.factorial(l + m)) * P[l - m] * rho ** l
            if m == 0:
                out[l * l + l] = y
            else:
                out[l * l + l + m] = y * np.cos(m * phi)
                out[l * l + l - m] = y * np.sin(m * phi)
    return out


def mirror(order):
    """+1 for the channels even under left-right mirroring (m >= 0), -1 for the odd ones (m < 0)."""
    return np.array([1.0 if c >= math.isqrt(c) * (math.isqrt(c) + 1) else -1.0 for c in range((order + 1) ** 2)])


def ambix(u):
    """Head axes (x right, y ahead, z up) to AmbiX axes (X ahead, Y left, Z up)."""
    return np.stack((u[1], -u[0], u[2]))


def grid():
    """The Gauss-Legendre grid in AmbiX axes, (3, Q), and its weights, which sum to 4 pi. It is mirror
    symmetric: each azimuth's negative is on it."""
    z, wz = np.polynomial.legendre.leggauss(GRID[0])
    ph = 2 * np.pi * np.arange(GRID[1]) / GRID[1]
    z, ph = np.repeat(z, GRID[1]), np.tile(ph, GRID[0])
    c = np.sqrt(1 - z * z)
    return np.stack((c * np.cos(ph), c * np.sin(ph), z)), np.repeat(wz, GRID[1]) * 2 * np.pi / GRID[1]


def magls(order, U, w, H):
    """The left ear's MagLS decoder (Schoerkhuber, Zaunschirm and Hoeldrich 2018; spaudiopy's magls_bin
    with hf_cont='avg'): below kr = N, f = N c / (2 pi a), the weighted least-squares fit of the complex
    responses H (bins, Q) on the grid U (3, Q); above it the fit of their magnitudes, each bin's phase the
    last bin's decoded phase plus the mean phase step of the omni filter's last five bins below. Returns
    the filters (NFFT, channels) and the first magnitude bin."""
    Y = sh(order, U).T                                     # (Q, channels)
    WY = Y * w[:, None]
    P = np.linalg.solve(Y.T @ WY, WY.T)                    # (channels, Q)
    f = np.arange(NFFT // 2 + 1) * FS / NFFT
    kc = int(np.argmin(np.abs(f - order * C_SOUND / (2 * np.pi * A_HEAD))))
    D = np.empty((len(f), Y.shape[1]), complex)
    D[:kc] = H[:kc] @ P.T
    step = np.mean(np.diff(np.unwrap(np.angle(D[:kc, 0])))[kc - 6:kc - 1])
    for k in range(kc, len(f)):
        D[k] = (np.abs(H[k]) * np.exp(1j * (np.angle(Y @ D[k - 1]) + step))) @ P.T
    return np.fft.irfft(D, NFFT, axis=0), kc


def decoders(pinna=False):
    """MagLS decoders cut to DTAPS taps with a half-Hann over the last 32: amb1 and amb3 on the head, and
    with `pinna` amb3-, amb5- and amb7-pinna on the head with PB and NH's pinna ahead of the ears, over
    the model's front half, and the bare head behind. Returns {name: (filters (DTAPS, channels), first
    magnitude bin, stats, order)} and the largest error of SN3D's orthogonality on the grid. The stats:
    the energy outside the best window of 64 and of 128 taps (dB), for a shorter decoder; how far the cut
    moves the decoded power at any direction of the grid; the decoded power against its head's, rms and
    95th percentile, in sixth octaves from 100 Hz and from 4 kHz to 16 kHz; and from 4 to 16 kHz in 10 Hz
    steps, which resolve the notches, the decoded magnitude's mean |error| and 95th percentile against
    its head's over the grid's front half."""
    U, w = grid()
    Y = sh(3, U)
    G = (Y * w) @ Y.T
    norm = 4 * np.pi / (2 * np.floor(np.sqrt(np.arange(16))) + 1)
    ortho = float(np.abs(G - np.diag(norm)).max())
    uh = np.stack((-U[1], U[0], U[2]))                     # the grid in the head's axes
    b, T = head(uh)

    def target(wv, D, q=slice(None)):                       # the left ear's responses (bins, points)
        H = ear(b[0, q], T[0, q], wv).T
        return H if D is None else H * pinna_resp(uh[:, q], D, wv).T
    f = np.arange(NFFT // 2 + 1) * FS / NFFT
    c6 = 100 * 2 ** (np.arange(45) / 6)
    S = np.array([(f > c * 2 ** (-1 / 12)) & (f <= c * 2 ** (1 / 12)) for c in c6], float)
    c6 = c6[S.sum(1) > 0]
    S = S[S.sum(1) > 0]
    S /= S.sum(1, keepdims=True)                           # sixth octaves from 100 Hz, those holding a bin
    kf = np.arange(4000 * NF // FS, 16000 * NF // FS + 1)  # 4 to 16 kHz in 10 Hz steps
    front = uh[1] > 0.05
    out = {}
    designs = [('amb1', 1, None), ('amb3', 3, None)]
    if pinna:
        designs += [('amb%d-pinna' % order, order, D_PB) for order in (3, 5, 7)]
    for name, order, D in designs:
        H = target(2 * np.pi * np.arange(NFFT // 2 + 1) / NFFT, D)
        h, kc = magls(order, U, w, H)
        e = (h ** 2).sum(1)
        c = np.cumsum(np.r_[0.0, e, e])                    # the windows wrap around, as the design does
        outside = {n: float(10 * np.log10(1 - (c[n:n + NFFT] - c[:NFFT]).max() / e.sum())) for n in (64, 128)}
        cut = h[:DTAPS].copy()
        cut[-32:] *= np.hanning(64)[32:, None]
        y = sh(order, U)
        full, kept = (S @ np.abs(np.fft.rfft(x, NFFT, axis=0) @ y) ** 2 for x in (h, cut))
        err = 10 * np.log10(kept / (S @ np.abs(H) ** 2))
        fine = np.abs(20 * np.log10(np.abs(np.fft.rfft(cut, NF, axis=0)[kf] @ y[:, front]) /
                                    np.abs(target(2 * np.pi * kf / NF, D, front))))
        hi = err[c6 >= 4000]
        out[name] = cut, kc, dict(outside=outside, cut=float(np.abs(10 * np.log10(kept / full)).max()),
                                  rms=float(np.sqrt(np.mean(err ** 2))), p95=float(np.percentile(np.abs(err), 95)),
                                  rms4=float(np.sqrt(np.mean(hi ** 2))), p954=float(np.percentile(np.abs(hi), 95)),
                                  front=float(fine.mean()), front95=float(np.percentile(fine, 95))), order
    return out, ortho


@contextlib.contextmanager
def rate(fs):
    """The rate FS, and the shelf's pole KB with it, at fs inside the block: head, magls and,
    through KB, ear read them."""
    global FS, KB
    saved = FS, KB
    FS, KB = fs, fs * ALPHA_F * A_C
    try:
        yield
    finally:
        FS, KB = saved


def design(order, fs, side=0):
    """decoders' MagLS filters on the bare head at the rate fs for the left ear (side 0) or the right (1):
    the cut taps (DTAPS, channels) and the first magnitude bin. space/gen_binaural_int.c computes the same
    in integers ."""
    with rate(fs):
        U, w = grid()
        b, T = head(np.stack((-U[1], U[0], U[2])))
        h, kc = magls(order, U, w, ear(b[side], T[side], 2 * np.pi * np.arange(NFFT // 2 + 1) / NFFT).T)
    cut = h[:DTAPS].copy()
    cut[-32:] *= np.hanning(64)[32:, None]
    return cut, kc


def read_tables(path):
    """sp_binaural_tables.h as gen_binaural_int prints it, or the Q16.48 taps its q48 mode prints (lines
    Q ORDER FS ACN TAPS...): {order: taps (rates, channels, taps), int64}, the rates and the taps' shift."""
    with open(path) as fh:
        text = fh.read()
    if text.startswith('Q '):
        rows = [ln.split() for ln in text.splitlines()]
        rates = sorted({int(r[2]) for r in rows})
        return ({order: np.array([[[int(v) for v in r[4:]] for r in rows if int(r[1]) == order and int(r[2]) == fs]
                                  for fs in rates], np.int64) for order in (1, 3)}, rates, 48)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    define = dict(re.findall(r'#define\s+(SP_BIN_\w+)\s+(\d+)', text))
    rates = [int(v) for v in re.findall(r'\d+', re.search(r'sp_bin_fs\[\d+\]\s*=\s*\{([^}]*)\}', text).group(1))]
    out = {}
    for order in (1, 3):
        m = re.search(r'sp_bin_o%d\[\d+\]\[(\d+)\]\[SP_BIN_TAPS\]\s*=\s*\{(.*?)\};' % order, text, re.S)
        out[order] = np.array([int(v) for v in re.findall(r'-?\d+', m.group(2))], np.int64).reshape(
            len(rates), int(m.group(1)), int(define['SP_BIN_TAPS']))
    return out, rates, int(define['SP_BIN_SHIFT'])


def test_grid(el=(-60, -30, 0, 30, 60)):
    """design-check's directions in the head's axes: the elevations el by the azimuths 0 to 330 in steps of
    30 (0 ahead, 90 to the right), then the poles. Returns u (3, k), az and el in degrees."""
    e, a = (v.ravel() for v in np.meshgrid(el, np.arange(0, 360, 30), indexing='ij'))
    return direction(np.r_[a, 0, 0], np.r_[e, 90, -90])


def direction(az, el):
    """Unit vectors (3, k) in the head's axes at the azimuths and elevations az, el (degrees); and those."""
    a, e = np.radians(az), np.radians(el)
    return np.stack((np.cos(e) * np.sin(a), np.cos(e) * np.cos(a), np.sin(e))), np.asarray(az), np.asarray(el)


# design-check's acceptance criterion , measured 2026-10-04 and accepted as built: each tap
# within half a step of the double design's, plus TAP_TOL for the arithmetic (measured: 1e-11 before the
# rounding), so that the table is that design rounded to the nearest step; the right ear's double design the
# left's mirrored within TAP_TOL; and the decoded magnitude from 50 Hz to 20 kHz on the test grid within DB_TOL
# of the double design's (measured: 0.07 dB at most, the rounding to Q2.14).
TAP_TOL, DB_TOL = 1e-9, 0.1
BANDS = ((50, 500), (500, 1500), (1500, 4000), (4000, 8000), (8000, 16000), (16000, 20000))


def design_check(argv):
    """binaural_ab.py design-check HEADER [--directions] [--head]: gen_binaural_int's integer filters against
    the double design, per order and rate (and with --directions per direction); exit 1 above a tolerance."""
    ap = argparse.ArgumentParser(prog='binaural_ab.py design-check',
                                 description='gen_binaural_int\'s filters () against the double design')
    ap.add_argument('header', help='sp_binaural_tables.h, or what gen_binaural_int q48 prints')
    ap.add_argument('--directions', action='store_true', help='the magnitude error at each direction')
    ap.add_argument('--head', action='store_true', help='also the decoded responses against the head itself')
    a = ap.parse_args(argv)
    tables, rates, shift = read_tables(a.header)
    u, az, el = test_grid()
    print('design-check: %s against the double design (taps with %d fractional bits); the decoded magnitude from '
          '50 Hz to 20 kHz at %d directions' % (os.path.basename(a.header), shift, len(az)))
    bad = []
    for order in (1, 3):
        y = sh(order, ambix(u))
        for i, fs in enumerate(rates):
            cut, kc = design(order, fs)
            right, _ = design(order, fs, side=1)
            q = tables[order][i].T / 2.0 ** shift                  # (taps, channels)
            tap, mir = np.abs(q - cut).max(), np.abs(right - mirror(order) * cut).max()
            f = np.arange(NF // 2 + 1) * fs / NF
            band = (f >= 50) & (f <= 20000)
            Hd, Hq = ((np.fft.rfft(x, NF, axis=0) @ y)[band] for x in (cut, q))
            e = np.abs(20 * np.log10(np.abs(Hq) / np.abs(Hd)))       # (bins, directions)
            k, d = np.unravel_index(e.argmax(), e.shape)
            fails = [what for what, over in (('taps', tap > 0.5 / 2 ** shift + TAP_TOL), ('mirror', mir > TAP_TOL),
                                             ('magnitude', e.max() > DB_TOL)) if over]
            name = 'order %d at %d Hz' % (order, fs)
            bad += ['%s (%s)' % (name, ', '.join(fails))] if fails else []
            print('  %-5s %s, kc %d: taps within %.4f steps (%.2e) of the double design; the decoded magnitude within '
                  '%.3g dB (rms %.3g)' % ('FAIL' if fails else 'ok', name, kc, tap * 2 ** shift, tap, e.max(),
                                          np.sqrt(np.mean(e ** 2))))
            print('        worst at az %d el %d, %.0f Hz; the right ear\'s double design is the left\'s mirrored within '
                  '%.0e%s' % (az[d], el[d], f[band][k], mir, '; over: ' + ', '.join(fails) if fails else ''))
            if a.directions:
                worst = e.max(0)
                print('        largest |error| (dB) at each direction; az %s' % ' '.join('%6d' % v for v in az[:12]))
                for r in range(0, len(az) - 2, 12):
                    print('        el %3d %s' % (el[r], ' '.join('%6.4f' % v for v in worst[r:r + 12])))
                print('        the poles, el 90 and -90: %.4f %.4f' % tuple(worst[-2:]))
    print('design-check %s: 4 designs; taps within half a step + %.0e, magnitude within %.2f dB (the acceptance criterion, )%s'
          % ('FAIL' if bad else 'ok', TAP_TOL, DB_TOL, ': ' + '; '.join(bad) if bad else ''))
    if a.head:
        head_check(tables, rates, shift)
    return 1 if bad else 0


def head_check(tables, rates, shift):
    """The integer filters' decoded responses against the head itself (head and ear, the design's
    target): the left ear's magnitude error by band and elevation, over the test grid's azimuths; and the ITD
    and ILD (itd_ild) round the horizon and up the column at az 90."""
    u, az, el = test_grid()
    w = 2 * np.pi * np.arange(NF // 2 + 1) / NF
    ui, azi, eli = direction(np.r_[np.arange(0, 181, 30), [90] * 5], np.r_[[0] * 7, -60, -30, 30, 60, 90])
    wi = 2 * np.pi * np.arange(NFFT // 2 + 1) / NFFT
    for order in (1, 3):
        y, yi = sh(order, ambix(u)), sh(order, ambix(ui))
        for i, fs in enumerate(rates):
            h = tables[order][i].T / 2.0 ** shift
            f = w * fs / (2 * np.pi)
            with rate(fs):
                b, T = head(u)
                e = 20 * np.log10(np.abs(np.fft.rfft(h, NF, axis=0) @ y) / np.abs(ear(b[0], T[0], w).T))
                bi, Ti = head(ui)
                Hh = ear(bi, Ti, wi)                               # (2, directions, bins)
                Hd = np.fft.rfft(h, NFFT, axis=0)
                cues = [itd_ild(L, R, wi * fs / (2 * np.pi), FS) for L, R in (
                    (Hh[0], Hh[1]), ((Hd @ yi).T, (Hd @ (mirror(order)[:, None] * yi)).T))]
            inband = (f >= 50) & (f <= 20000)
            print('order %d at %d Hz against the head, left ear: the decoded magnitude\'s error (dB), rms and mean over '
                  'the band and the 12 azimuths; all %.2f rms' % (order, fs, np.sqrt(np.mean(e[inband] ** 2))))
            print('  %6s %s' % ('el', ' '.join('%13s' % ('%g-%g kHz' % (lo / 1000, hi / 1000)) for lo, hi in BANDS)))
            for e0 in (-60, -30, 0, 30, 60, 90):
                d = np.abs(el) == 90 if e0 == 90 else el == e0
                cells = [e[(f >= lo) & (f < hi)][:, d] for lo, hi in BANDS]
                print('  %6s %s' % ('poles' if e0 == 90 else e0, ' '.join(
                    '  %5.2f %+5.2f' % (np.sqrt(np.mean(c ** 2)), c.mean()) for c in cells)))
            l1, g = np.abs(h).sum(0), np.abs(h @ y).sum(0)          # each channel's L1 norm; one source's, per direction
            k = g.argmax()
            print('  headroom: the channels\' L1 norms sum to %.3f (ACN %d\'s the largest, %.3f), the largest tap %.4f; '
                  'one source\'s decoded response at most %.3f in L1 over the test grid (az %d el %d)'
                  % (l1.sum(), l1.argmax(), l1.max(), np.abs(h).max(), g.max(), az[k], el[k]))
            print('  ITD (us) and ILD (dB, 0.2-1.5 / 1.5-6 / 6-16 kHz) of the head, then of the decoder; right leading '
                  'or louder is +')
            for j in range(len(azi)):
                print('  az %3d el %3d   %5.0f %5.0f   %5.1f %5.1f %5.1f   %5.1f %5.1f %5.1f' % (
                    azi[j], eli[j], cues[0][0][j], cues[1][0][j], *cues[0][1][j], *cues[1][1][j]))


def fir_error():
    """The delays' worst errors against the ideal fractional delay, magnitude (dB) and delay (samples),
    over fractions in steps of 1/100 and frequencies to 20 kHz."""
    d = 20 + np.arange(100) / 100
    I, h = fir(d)
    w = 2 * np.pi * np.arange(100, 20001, 100) / FS
    k = (I - D0)[:, None] + np.arange(TAPS)
    e = np.einsum('dt,dtw->dw', h, np.exp(-1j * k[:, :, None] * w)) * np.exp(1j * w * d[:, None])
    return float(np.abs(20 * np.log10(np.abs(e))).max()), float((np.abs(np.angle(e)) / w).max())


def accuracy(dec):
    """The head's and the decoders amb1's and amb3's ITD and ILD (itd_ild) at eight azimuths on the
    horizon, 0 ahead and 90 to the right."""
    w = 2 * np.pi * np.arange(NFFT // 2 + 1) / NFFT
    az = np.radians(np.arange(0, 360, 45))
    u = np.stack((np.sin(az), np.cos(az), 0 * az))
    b, T = head(u)
    resp = {'head': ear(b, T, w)}                          # (2, 8, bins)
    for name in ('amb1', 'amb3'):
        h, _, _, order = dec[name]
        Hd = np.fft.rfft(h, NFFT, axis=0)
        y = sh(order, ambix(u))
        resp[name] = np.stack(((Hd @ y).T, (Hd @ (mirror(order)[:, None] * y)).T))
    return np.degrees(az), {k: itd_ild(H[0], H[1], w * FS / (2 * np.pi), FS) for k, H in resp.items()}


def notch(H, f):
    """The deepest notch of the responses H (..., bins) at the frequencies f from 4 to 16 kHz: its
    frequency and its depth in dB."""
    band = (f >= 4000) & (f <= 16000)
    db = 20 * np.log10(np.abs(H[..., band]) + 1e-12)
    k = np.argmin(db, -1)
    return f[band][k], np.take_along_axis(db, k[..., None], -1)[..., 0]


def transfer(x, y, n=1024):
    """y's response over x's (n // 2 + 1 bins): Welch's cross spectrum over x's power, Hann windows of n
    samples overlapping by half."""
    win = np.hanning(n)
    X, Y = (np.array([np.fft.rfft(win * s[i:i + n]) for i in range(0, len(s) - n + 1, n // 2)]) for s in (x, y))
    return (np.conj(X) * Y).sum(0) / (np.abs(X) ** 2).sum(0)


def pinna_check(dec, u):
    """At unit directions u (3, k), from the analytic responses in 10 Hz steps: the directions as theta and
    phi round the ears' axis and as az and el (degrees); the head's treble at the right ear, 20 log10 beta;
    the deepest notch from 4 to 16 kHz (Hz, dB) of the pinna with PB and NH's D and with RD's; the range of
    PB and NH's pinna below LOW_CUT (dB); and amb3-pinna's pinna part at the right ear, its response over
    amb3's, with its deepest notch and its mean |error| against PB and NH's pinna from 4 to 16 kHz (dB)."""
    f = np.arange(NF // 2 + 1) * FS / NF
    w = 2 * np.pi * np.arange(NF // 2 + 1) / NF
    beta, _ = head(u)
    pb, rd = pinna_resp(u, D_PB, w), pinna_resp(u, D_RD, w)
    low = 20 * np.log10(np.abs(pb[:, f < LOW_CUT]))
    y = mirror(3)[:, None] * sh(3, ambix(u))
    part = ((np.fft.rfft(dec['amb3-pinna'][0], NF, axis=0) @ y) / (np.fft.rfft(dec['amb3'][0], NF, axis=0) @ y)).T
    band = (f >= 4000) & (f <= 16000)
    err = np.abs(20 * np.log10(np.abs(part[:, band]) / np.abs(pb[:, band]))).mean(1)
    return dict(th=np.degrees(np.arcsin(u[0])), ph=np.degrees(np.arctan2(u[2], u[1])),
                az=np.degrees(np.arctan2(u[0], u[1])), el=np.degrees(np.arcsin(u[2])),
                hf=20 * np.log10(beta[1]), pb=notch(pb, f), rd=notch(rd, f), low=(low.min(1), low.max(1)),
                amb=notch(part, f), err=err)


def coord_at(b0, b1, b, p0, p1):
    """space.c's sp_coord_at, one coordinate at the boundaries b: p1 once b reaches b1 or when there is no
    move, p0 up to b0, else p0 + trunc((p1 - p0)(b - b0) / (b1 - b0))."""
    b = np.asarray(b, np.int64)
    if b1 <= b0:
        return np.full(b.shape, p1, np.int64)
    d = p1 - p0
    q = (abs(d) * (b - b0)) // (b1 - b0)
    return np.where(b >= b1, p1, np.where(b <= b0, p0, p0 + (q if d >= 0 else -q)))


def isqrt(v):
    """floor(sqrt(v)) of int64 values below 2^52."""
    r = np.floor(np.sqrt(v.astype(np.float64))).astype(np.int64)
    r -= r * r > v
    r += (r + 1) * (r + 1) <= v
    return r


def targets(P, near):
    """space.c's sp_targets on positions P (..., 3) in mm: the gains (left, right) in Q1.15, int64."""
    x, y, z = P[..., 0], P[..., 1], P[..., 2]
    r = isqrt(x * x + y * y + z * z)
    ux = (np.abs(x) << 30) // np.maximum(r, near)
    ux = np.where(x < 0, -ux, ux)
    gd = (1000 << 15) // np.maximum(r, 1000)
    return np.stack([(isqrt(((1 << 30) + s * ux) >> 1) * gd + (1 << 14)) >> 15 for s in (-1, 1)])


def pan(stems, G, K, n):
    """space.c's mix from the stems and the gains at each boundary G (2, voices, nb): over block b the gains
    ramp from the last boundary's (the first's in block 0), (gp K + (gt - gp)(j + 1)) >> log2 K; then
    (sum of stem gain + 2^14) >> 15 per channel, saturated. Returns (2, n) int16."""
    acc = np.zeros((2, n), np.int64)
    j = np.arange(1, K + 1)
    for v, s in stems.items():
        gt = G[:, v]
        gp = np.concatenate((gt[:, :1], gt[:, :-1]), 1)
        g = ((gp[..., None] * K + (gt - gp)[..., None] * j) // K).reshape(2, -1)
        acc += s.astype(np.int64) * g[:, :n]
    return np.clip((acc + (1 << 14)) >> 15, -32768, 32767).astype(np.int16)


def read_wav(path):
    with wave.open(path) as w:
        if w.getsampwidth() != 2:
            sys.exit('binaural_ab.py: %s is not 16-bit PCM' % path)
        x = np.frombuffer(w.readframes(w.getnframes()), '<i2').reshape(-1, w.getnchannels())
        return x, w.getframerate()


def write_wav(path, x, bits=16):
    """x (channels, n), floats at full scale 1, rounded to 16 or 24 bits; returns what was written as
    floats, for the meter."""
    s = float(1 << (bits - 1))
    q = np.ascontiguousarray(np.clip(np.round(x.T * s), -s, s - 1).astype('<i4'))
    with wave.open(path, 'wb') as w:
        w.setnchannels(x.shape[0])
        w.setsampwidth(bits // 8)
        w.setframerate(FS)
        w.writeframes(q.astype('<i2').tobytes() if bits == 16 else q.view(np.uint8).reshape(-1, 4)[:, :3].tobytes())
    return q.T / s


class Record:
    """A merged record (harness --merged): the header's lines and the events, in the order they apply."""

    def __init__(self, path):
        self.header, self.events = [], []
        with open(path) as fh:
            for line in fh:
                t = line.split()
                if t:
                    (self.events if t[0].isdigit() else self.header).append(t)
        h = {t[0]: t[1:] for t in self.header}
        if h.get('space') != ['stereo']:
            sys.exit('binaural_ab.py: %s has no space statement; only a stereo piece has positions' % path)
        self.fs, self.block, self.near = int(h['fs'][0]), int(h['block'][0]), int(h['near'][0])
        self.n = int(h['length_ns'][0]) * self.fs // 10 ** 9
        self.streams = h['streams']
        self.voices = sum(t[0] == 'voice' for t in self.header)
        self.sounding = sorted({int(t[2]) for t in self.events if t[3] == 'on'})

    def stems(self, tmp):
        """Each sounding voice alone in 16 bits, {voice: int16 array}, and the clipped count: two voices a
        run of the harness, at (-1000, 0, 0) and (1000, 0, 0) with near 100, where the pan's gains are
        32768 and 0, so that each channel is (q30 + 2^14) >> 15 of one voice. The voices' own pos and
        move are dropped."""
        head = ['near 100' if t[0] == 'near' else ' '.join(t) for t in self.header if t[0] != 'hash']
        tl, wav = os.path.join(tmp, 'stems.tl'), os.path.join(tmp, 'stems.wav')
        out, clipped = {}, 0
        for i in range(0, len(self.sounding), 2):
            pair = self.sounding[i:i + 2]
            lines = head + ['0 %s %d pos %d 0 0' % (self.streams[0], v, x) for v, x in zip(pair, (-1000, 1000))]
            lines += [' '.join(t) for t in self.events if int(t[2]) in pair and t[3] not in ('pos', 'move')]
            with open(tl, 'w') as fh:
                fh.write('\n'.join(lines) + '\n')
            r = subprocess.run([HARNESS, '--play', tl, '--wav', wav], capture_output=True, text=True)
            if r.returncode:
                sys.exit('binaural_ab.py: the harness refused a stem record: %s%s' % (r.stdout, r.stderr))
            clipped += int(re.search(r'clipped=(\d+)', r.stdout).group(1))
            x, _ = read_wav(wav)
            for c, v in enumerate(pair):
                out[v] = x[:, c].copy()
        return out, clipped

    def positions(self, nb):
        """Each voice's position at the boundaries 0 to nb - 1 as space.c holds it, int64 (voices, nb, 3):
        (0, 1000, 0) at first; a pos from the first boundary at or after its sample; a move from there in
        a straight line, from where the voice is then to its end at the first boundary at or after the
        move's end . Of two events on a voice before one boundary, the second wins."""
        K = self.block
        segs = [[(0, (0, 1000, 0), (0, 1000, 0), 0, 0)] for _ in range(self.voices)]
        for t in self.events:
            if t[3] not in ('pos', 'move'):
                continue
            ns, v, p = int(t[0]), int(t[2]), tuple(int(c) for c in t[4:7])
            b = -(-(ns * self.fs // 10 ** 9) // K)
            if t[3] == 'pos':
                new = (b, p, p, 0, 0)
            else:
                _, q0, q1, b0, b1 = segs[v][-1]
                new = (b, tuple(int(coord_at(b0, b1, b, q0[c], q1[c])) for c in range(3)), p, b,
                       -(-((ns + int(t[7])) * self.fs // 10 ** 9) // K))
            if segs[v][-1][0] == b:
                segs[v][-1] = new
            else:
                segs[v].append(new)
        P = np.empty((self.voices, nb, 3), np.int64)
        for v, ss in enumerate(segs):
            for i, (b, q0, q1, b0, b1) in enumerate(ss):
                e = min(ss[i + 1][0] if i + 1 < len(ss) else nb, nb)
                for c in range(3):
                    P[v, b:e, c] = coord_at(b0, b1, np.arange(b, e), q0[c], q1[c])
        return P


def pink(n, top=20000):
    """n samples of pink noise (seeded), 20 Hz to `top`, at -20 dBFS rms."""
    X = np.fft.rfft(np.random.default_rng(53).standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / FS)
    noise = np.fft.irfft(X * np.where((f >= 20) & (f <= top), 1 / np.sqrt(np.maximum(f, 20)), 0), n)
    return noise * (0.1 / np.sqrt(np.mean(noise ** 2)))


def gate(t, a, b, ramp=0.01):
    """1 on [a, b) at the times t, with raised-cosine ramps inside."""
    g = np.clip(np.minimum(t - a, b - t) / ramp, 0, 1)
    return np.sin(0.5 * np.pi * g) ** 2


def bursts():
    """Pink noise as one voice: three 250 ms bursts from each of eight azimuths at 1 m (0 ahead, then
    clockwise in steps of 45), the move in the pause between, then a circle at 1 m in 16 s. Returns the
    stem (int16), the positions at the boundaries of 64 samples (1, nb, 3), the sample count and the
    eight middle bursts, inside their ramps, as sample ranges."""
    lead, burst, gap, pause, circle, ramp = 0.5, 0.25, 0.1, 0.6, 16.0, 0.01
    per = 3 * burst + 2 * gap + pause
    c0, c1 = lead + 8 * per, lead + 8 * per + circle
    n = int(round((c1 + lead) * FS))
    t = np.arange(n) / FS
    noise = pink(n)
    env = gate(t, c0, c1) + sum(gate(t, lead + i * per + k * (burst + gap), lead + i * per + k * (burst + gap) + burst)
                                for i in range(8) for k in range(3))
    stem = np.round(noise * env * 32768).astype(np.int16)
    tb = np.arange((n + 2 * T0) // 64 + 4) * 64 / FS
    az = np.where(tb < c0 - pause / 2, 45 * np.clip(np.floor((tb - lead + pause / 2) / per), 0, 7),
                  360 * np.clip((tb - c0) / circle, 0, 1))
    P = np.stack((np.round(1000 * np.sin(np.radians(az))), np.round(1000 * np.cos(np.radians(az))), 0 * az), -1)
    mid = [lead + i * per + burst + gap for i in range(8)]
    return stem, P.astype(np.int64)[None], n, [(int((a + ramp) * FS), int((a + burst - ramp) * FS)) for a in mid]


def elevation():
    """Pink noise as one voice at 1 m, three 250 ms bursts at each station as in bursts, the move in the
    pause between, with theta the lateral angle and phi the elevation round the ears' axis: up and down a
    column straight ahead (theta 0, phi -45, 0, 45, 90, 45, 0, -45), up the cone 60 degrees to the right
    (theta 60, phi -45, 0, 45, 90), along an arc ahead from phi -45 over to 90 and back in 10 s, and up
    the column again with the noise cut at LOW_CUT, its rms kept; a pause more between these four.
    Returns the stem (int16), the positions at the boundaries of 64 samples (1, nb, 3), the sample count,
    and the middle burst at each of the first eight stations, inside its ramps, as sample ranges."""
    lead, burst, gap, pause, arc, ramp = 0.5, 0.25, 0.1, 0.6, 10.0, 0.01
    per = 3 * burst + 2 * gap + pause
    col, cone = [(0, p) for p in (-45, 0, 45, 90)], [(60, p) for p in (-45, 0, 45, 90)]
    t, parts = lead, []                                     # (first sound, last sound, theta, phi, cut)
    for sec, cut in ((col + col[-2::-1], False), (cone, False), (None, False), (col, True)):
        if sec is None:
            parts.append((t, t + arc, 0, None, cut))        # the arc
            t += arc + pause
        for th, ph in sec or ():
            parts.append((t, t + per - pause, th, ph, cut))
            t += per
        t += pause
    n = int(round((t - pause) * FS))
    tt = np.arange(n) / FS
    env = np.zeros((2, n))                                  # the full noise's, the cut noise's
    for a0, a1, _, ph, cut in parts:
        env[int(cut)] += gate(tt, a0, a1) if ph is None else sum(
            gate(tt, a0 + k * (burst + gap), a0 + k * (burst + gap) + burst) for k in range(3))
    stem = np.round((pink(n) * env[0] + pink(n, LOW_CUT) * env[1]) * 32768).astype(np.int16)
    tb = np.arange((n + 2 * T0) // 64 + 4) * 64 / FS
    a0, a1 = np.array([p[0] for p in parts]), np.array([p[1] for p in parts])
    j = np.searchsorted((a1[:-1] + a0[1:]) / 2, tb, side='right')      # the part each boundary belongs to
    i_arc = [p[3] for p in parts].index(None)
    s = np.clip((tb - a0[i_arc]) / arc, 0, 1)
    th = np.radians(np.array([p[2] for p in parts], float)[j])
    ph = np.radians(np.where(j == i_arc, -45 + 135 * (1 - np.abs(2 * s - 1)),
                             np.array([-45 if p[3] is None else p[3] for p in parts], float)[j]))
    P = np.round(1000 * np.stack((np.sin(th), np.cos(th) * np.cos(ph), np.cos(th) * np.sin(ph)), -1))
    return stem, P.astype(np.int64)[None], n, [(int((p[0] + burst + gap + ramp) * FS),
                                                int((p[0] + 2 * burst + gap - ramp) * FS)) for p in parts[:4] + parts[7:11]]


def ramp(p, K):
    """Per-sample values from values p (nbw + 1) at the boundaries, the first being the one before the
    window: over block i, from p[i] to p[i + 1] in K steps, as space.c ramps its gains."""
    return (p[:-1, None] + (p[1:] - p[:-1])[:, None] * (np.arange(1, K + 1) / K)).ravel()


def fftconv(x, h):
    """The first len(x) samples of x convolved with h."""
    N = 1 << (len(x) + len(h) - 2).bit_length()
    return np.fft.irfft(np.fft.rfft(x, N) * np.fft.rfft(h, N), N)[:len(x)]


def fir(d):
    """For delays d (k,), 15 to 46.5 samples: the integer parts I and the Kaiser-windowed sincs (k, TAPS),
    tap i at delay I - 15 + i, each summing to 1."""
    I = np.floor(d).astype(np.int64)
    t = np.arange(TAPS) - D0 - (d - I)[:, None]
    h = np.sinc(t) * np.i0(KAISER * np.sqrt(np.maximum(0.0, 1 - (t / (TAPS // 2)) ** 2)))
    return I, h / h.sum(1, keepdims=True)


def fdelay(m, dprev, dcur, K):
    """m (blocks x K samples) through a delay line read at two delays per block, the last boundary's fading
    out and this one's fading in over the block. A run of blocks whose delay stays put is one convolution."""
    nb = len(dcur)
    mp = np.concatenate((np.zeros(PAD), m))
    y = np.empty(nb * K)
    still = dprev == dcur
    e = np.flatnonzero(np.diff(np.r_[0, still.astype(np.int8), 0]))
    for i0, i1 in zip(e[::2], e[1::2]):
        I, h = fir(dcur[i0:i0 + 1])
        full = np.zeros(PAD)
        full[I[0] - D0:I[0] + TAPS - D0] = h[0]
        y[i0 * K:i1 * K] = np.convolve(mp[i0 * K + 1:i1 * K + PAD], full, 'valid')
    r = np.arange(1, K + 1) / K
    moving = np.flatnonzero(~still)
    for c in range(0, len(moving), 2048):
        blk = moving[c:c + 2048]
        n = blk[:, None] * K + np.arange(K)
        out = 0
        for d, wgt in ((dprev[blk], 1 - r), (dcur[blk], r)):
            I, h = fir(d)
            idx = n[:, :, None] + PAD + D0 - I[:, None, None] - np.arange(TAPS)
            out = out + wgt * np.einsum('bkt,bt->bk', mp[idx], h)
        y[n] = out
    return y


def decode(field, h, sign):
    """Two ears from an ambisonic field (channels, n) by the left ear's filters h (taps, channels): the left
    is E + O and the right E - O, E and O the filtered channels even and odd under mirroring."""
    n = field.shape[1]
    N = 1 << (n + len(h) - 2).bit_length()
    E = O = 0
    for c in range(field.shape[0]):
        X = np.fft.rfft(field[c], N) * np.fft.rfft(h[:, c], N)
        if sign[c] > 0:
            E = E + X
        else:
            O = O + X
    e, o = np.fft.irfft(E, N)[:n], np.fft.irfft(O, N)[:n]
    return np.stack((e + o, e - o))


def render(stems, P, near, K, s0, s1, heads, decs):
    """The head versions `heads` ({name: the pinna's D, or None for the bare head}) and the ambisonic
    versions `decs` ({name: (filters, order)}) of samples [s0, s1), {name: (2, n)}, and the field to the
    highest order, as floats at full scale 1, rendered from MARGIN s before s0: per voice and boundary,
    the pinna's taps, the head's shelf, delay and the pan's distance gain, and the field's SN3D gains
    times that distance gain, each ramped over the block as space.c ramps its gains."""
    W0 = max(s0 - int(MARGIN * FS), 0) // K * K
    W1 = -(-(s1 + 2 * T0) // K) * K
    nbw, bA = (W1 - W0) // K, W0 // K
    idx = np.r_[max(bA - 1, 0), bA:bA + nbw]                # the boundary before the window, then its own
    lp_ir = lowpass_ir()
    out = {k: np.zeros((2, W1 - W0)) for k in heads}
    order = max(o for _, o in decs.values())
    field = np.zeros(((order + 1) ** 2, W1 - W0))
    for v, s in stems.items():
        x = np.zeros(W1 - W0)
        seg = s[W0:W1]
        x[:len(seg)] = seg / 32768.0
        if not x.any():
            continue
        Pv = P[v, idx]
        r = isqrt((Pv * Pv).sum(1))
        gd = ((1000 << 15) // np.maximum(r, 1000)) / 32768.0
        u = (Pv / np.maximum(np.sqrt((Pv * Pv).sum(1).astype(float)), near)[:, None]).T
        beta, T = head(u)
        for k, D in heads.items():
            xp = x if D is None else pinna(x, u, D, K)
            lp = fftconv(xp, lp_ir)
            for e in (0, 1):
                m = ramp(gd * beta[e] / math.sqrt(2), K) * xp + ramp(gd * (1 - beta[e]) / math.sqrt(2), K) * lp
                out[k][e] += fdelay(m, T[e, :-1] + D0, T[e, 1:] + D0, K)
        g = sh(order, ambix(u)) * gd
        for c in range(len(field)):
            field[c] += ramp(g[c], K) * x
    a = s0 - W0
    vs = {k: y[:, a + D0:a + D0 + s1 - s0] for k, y in out.items()}
    for k, (h, o) in decs.items():
        vs[k] = decode(field[:(o + 1) ** 2], h, mirror(o))[:, a + T0:a + T0 + s1 - s0]
    return vs, field[:, a:a + s1 - s0]


def int_versions(rec, stems, P, K, near, s0, s1, h3, field, harness, pinna):
    """The integer field route (space/field.c; docs/INSTRUMENT.md, sections 11 to 16) rendered by the harness,
    as versions of samples [s0, s1) at full scale 1: int-amb3, the 16 channels of space amb3 decoded here
    by amb3's filters h3; with pinna, int-amb3-pinna, Brown and Duda's pinna per voice ahead of the field
    (pinna pbnh, PB and NH's D); and int-binaural (int-binaural-pinna), the harness's own decoder, when its
    build has a table. A piece (rec, its merged record) renders its own voices on the route to render's
    window end; a made item (rec None) renders its stem through --stems, one voice at its positions P, a
    pos at each boundary where it moves. Prints the integer field against render's double one, field,
    in 16-bit steps."""
    W0 = max(s0 - int(MARGIN * FS), 0) // K * K
    W1 = -(-(s1 + 2 * T0) // K) * K
    a, length_ns = s0 - W0, -(-W1 * 10 ** 9 // FS)
    if rec is None:
        from compose import ENGINE
        head = ['timeline draft2', 'engine ' + ENGINE, 'fs %d' % FS, 'length_ns %d' % length_ns, 'space stereo',
                'block %d' % K, 'near %d' % near, 'streams made', 'patch sine 0 65536 0 0 0 256 5 500 200 800',
                'patch sine 1 65536 0 0 0 0 5 500 200 800', 'voice 0 sine']
        pv = P[0, :W1 // K]
        events = ['%d made 0 pos %d %d %d' % ((-(-b * K * 10 ** 9 // FS),) + tuple(pv[b]))
                  for b in range(len(pv)) if b == 0 or (pv[b] != pv[b - 1]).any()]
    else:
        head = ['length_ns %d' % length_ns if t[0] == 'length_ns' else ' '.join(t) for t in rec.header if t[0] != 'hash']
        events = [' '.join(t) for t in rec.events if int(t[0]) < length_ns]
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        tl, wav, sw = (os.path.join(tmp, f) for f in ('int.tl', 'int.wav', 'stem.wav'))
        extra = []
        if rec is None:
            with wave.open(sw, 'wb') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(FS)
                w.writeframes(stems[0].astype('<i2').tobytes())
            extra = ['--stems', sw]
        for route in ('amb3', 'binaural'):
            for pin in (False, True) if pinna else (False,):
                k = 'int-%s%s' % (route, '-pinna' if pin else '')
                lines = ['space %s%s' % (route, '\npinna pbnh' if pin else '') if ln.startswith('space ') else ln
                         for ln in head]
                with open(tl, 'w') as fh:
                    fh.write('\n'.join(lines + events) + '\n')
                r = subprocess.run([harness, '--play', tl, '--wav', wav] + extra, capture_output=True, text=True)
                if 'no binaural table' in r.stdout + r.stderr:
                    print('  %s: none, this harness has no table (make BINAURAL_H=PATH, then --harness)' % k)
                    break
                if r.returncode:
                    sys.exit('binaural_ab.py: the harness refused %s: %s%s' % (k, r.stdout, r.stderr))
                kv = dict(x.split('=', 1) for x in r.stdout[r.stdout.index('PLAY '):].split('\n')[0].split()[1:])
                x, _ = read_wav(wav)
                note = ''
                if route == 'amb3':
                    out[k] = decode(x[W0:W1].T / 32768.0, h3, mirror(3))[:, a + T0:a + T0 + s1 - s0]
                    if not pin:
                        d = x[s0:s1].T - field * 32768
                        note = '; its field against the double one: worst %.2f, rms %.3f 16-bit steps' % (
                            np.abs(d).max(), np.sqrt(np.mean(d * d)))
                else:
                    out[k] = x[s0 + T0:s1 + T0].T / 32768.0
                print('  %s: hash %s block_indep=%s clipped=%s%s' % (k, kv['hash'], kv['block_indep'], kv['clipped'], note))
    return out


def fade(x):
    n = x.shape[-1]
    a, b = int(FADE_IN * FS), int(FADE_OUT * FS)
    w = np.ones(n)
    w[:a] = np.sin(0.5 * np.pi * (np.arange(a) + 0.5) / a) ** 2
    w[n - b:] = np.cos(0.5 * np.pi * (np.arange(b) + 0.5) / b) ** 2
    return x * w


def main():
    if sys.argv[1:2] == ['design-check']:
        sys.exit(design_check(sys.argv[2:]))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('items', nargs='*', metavar='ITEM[@START]')
    ap.add_argument('--length', type=float, default=30.0, help='excerpt length, seconds')
    ap.add_argument('--out', default=os.path.join(SPIKE, 'out', 'binaural'), help='the directory for the WAVs')
    ap.add_argument('--ambix', action='store_true', help='also write the third-order field')
    ap.add_argument('--integer', action='store_true', help='also render the integer field route by the harness')
    ap.add_argument('--harness', default=HARNESS, help='the harness for --integer; one with a table adds int-binaural')
    a = ap.parse_args()
    if a.length <= 0:
        ap.error('--length must be above 0')
    items = []
    for it in a.items or ITEMS:
        name, _, at = it.partition('@')
        try:
            items.append((name, float(at) if at else 0.0))
        except ValueError:
            ap.error('%s: START is seconds' % it)
    missing = [nm for nm, _ in items if nm not in MADE and not all(
        os.path.exists(os.path.join(RENDERS, nm + ext)) for ext in ('.tl', '.wav'))]
    if missing:
        sys.exit('binaural_ab.py: no render of %s in out/music; render it first: tools/music/play.sh -n '
                 'PIECE, or python3 -B tools/music/compose.py --profile=NAME PIECE-keys for PIECE-keys-NAME'
                 % ', '.join(missing))
    if (a.integer or any(nm not in MADE for nm, _ in items)) and subprocess.run(
            ['make', '-C', SPIKE, '-s', 'SINE_BITS=12', 'EXP_BITS=10', 'PHASE64=1', 'ENV64=1']).returncode:
        sys.exit('binaural_ab.py: the harness did not build')
    os.makedirs(a.out, exist_ok=True)

    dec, ortho = decoders(pinna=any(nm == 'elevation' for nm, _ in items))
    print('decoders: MagLS on the head, a %d x %d Gauss-Legendre grid (SN3D orthogonal on it to %.0e), %d-tap '
          'design cut to %d taps, latency %d' % (GRID + (ortho, NFFT, DTAPS, T0)))
    if 'amb3-pinna' in dec:
        print('  the -pinna decoders on the head with PB and NH\'s pinna ahead of the ears over the model\'s front '
              'half, the bare head behind')
    for k, (h, kc, st, order) in dec.items():
        print('  %s: order %d, complex fit below %.0f Hz, magnitudes above; energy outside the best 64 taps %.1f dB, '
              'the best 128 %.1f dB' % (k, order, kc * FS / NFFT, st['outside'][64], st['outside'][128]))
        print('    the cut to %d taps moves the decoded power by %.2f dB at most; against its head %.2f dB rms, 95%% '
              'within %.2f dB (sixth octaves, 100 Hz to 16 kHz, the grid), from 4 kHz %.2f and %.2f dB'
              % (DTAPS, st['cut'], st['rms'], st['p95'], st['rms4'], st['p954']))
        print('    4 to 16 kHz in 10 Hz steps, the grid\'s front half: the decoded magnitude within %.2f dB of its '
              'head\'s on average, 95%% within %.2f dB' % (st['front'], st['front95']))
    mag, dly = fir_error()
    print('delays: a %d-tap Kaiser-windowed sinc (beta %g), within %.3f dB and %.4f samples of the ideal '
          'fractional delay to 20 kHz' % (TAPS, KAISER, mag, dly))
    az, acc = accuracy(dec)
    print('ITD (us) and ILD (dB, 0.2-1.5 / 1.5-6 / 6-16 kHz) of the head and the decoders; right leading or louder is +')
    print('  %4s  %24s  %s' % ('az', 'ITD head  amb1  amb3', '  '.join('%-17s' % ('ILD ' + k) for k in acc)))
    for i, d in enumerate(az):
        print('  %4.0f  %8.0f %5.0f %5.0f  %s' % ((d,) + tuple(acc[k][0][i] for k in acc) + ('  '.join(
            ' '.join('%5.1f' % x for x in acc[k][1][i]) for k in acc),)))

    for name, start in items:
        t_start = time.time()
        if name in MADE:
            stem, P, n, mid = bursts() if name == 'bursts' else elevation()
            K, near, stems = 64, 100, {0: stem}
            rec = None
            ref = pan(stems, targets(P, near), K, n)
            s0, s1 = 0, n
            print('== %s: pink noise, %.1f s; the pan is space.c\'s, done here' % (name, n / FS))
        else:
            rec = Record(os.path.join(RENDERS, name + '.tl'))
            x, fs = read_wav(os.path.join(RENDERS, name + '.wav'))
            if fs != FS or rec.fs != FS or len(x) != rec.n or x.shape[1] != 2:
                sys.exit('binaural_ab.py: %s: the record and the render disagree, or not 48 kHz stereo' % name)
            K, near, n = rec.block, rec.near, rec.n
            s0 = int(round(start * FS))
            s1 = min(n, s0 + int(round(a.length * FS)))
            if s1 - s0 < FS:
                sys.exit('binaural_ab.py: %s is %.1f s long; start %.1f leaves under a second' % (name, n / FS, start))
            with tempfile.TemporaryDirectory() as tmp:
                stems, clipped = rec.stems(tmp)
            P = rec.positions((n + 2 * T0) // K + 2)
            ref = x.T
            worst = int(np.abs(pan(stems, targets(P, near), K, n).astype(np.int32) - ref).max())
            bound = -(-len(stems) // 2) + 1
            print('== %s @%.1f s for %.1f s: %d sounding voices of %d, %d runs of the harness, %d clipped' %
                  (name, s0 / FS, (s1 - s0) / FS, len(stems), rec.voices, -(-len(stems) // 2), clipped))
            print('  the stems and space.c\'s gains rebuild %s.wav (%d samples) to within %d (bound %d): %s' %
                  (name, n, worst, bound, 'ok' if worst <= bound and not clipped else 'MISS'))
        if name == 'elevation':
            heads, decs = {'head': None, 'pinna': D_PB, 'pinna-rd': D_RD}, ('amb3', 'amb3-pinna')
            p = P[0, [(b0 + b1) // 2 // K for b0, b1 in mid]].astype(float)
            pc = pinna_check(dec, (p / np.sqrt((p * p).sum(1, keepdims=True))).T)
            print('  the first eight stations, analytic, in 10 Hz steps: theta and phi round the ears\' axis; az and el; '
                  'the head\'s treble at the right ear;\n  the deepest notch from 4 to 16 kHz of the pinna with PB and '
                  'NH\'s D and with RD\'s; PB and NH\'s pinna below %.0f kHz;\n  amb3-pinna over amb3 at the right ear, '
                  'its deepest notch and its mean |error| against PB and NH\'s pinna from 4 to 16 kHz' % (LOW_CUT / 1000))
            print('  %4s %4s %6s %6s %9s  %-16s %-16s %-14s %-16s %s' % ('th', 'ph', 'az', 'el', 'treble', 'PB/NH notch',
                                                                    'RD notch', 'below 4 kHz', 'amb3-pinna', '|err|'))
            for i in range(len(mid)):
                print('  %4.0f %4.0f %6.1f %6.1f %+6.2f dB  %5.0f %5.1f dB   %5.0f %5.1f dB   %+4.1f..%+4.1f dB  '
                      '%5.0f %5.1f dB   %.2f dB' % tuple(v[i] for v in (
                          pc['th'], pc['ph'], pc['az'], pc['el'], pc['hf'], pc['pb'][0], pc['pb'][1], pc['rd'][0],
                          pc['rd'][1], pc['low'][0], pc['low'][1], pc['amb'][0], pc['amb'][1], pc['err'])))
        else:
            heads, decs = {'head': None}, ('amb1', 'amb3')
        vs, field = render(stems, P, near, K, s0, s1, heads, {k: (dec[k][0], dec[k][3]) for k in decs})
        if a.integer:
            vs.update(int_versions(rec, stems, P, K, near, s0, s1, dec['amb3'][0], field, a.harness, name == 'elevation'))
        versions = {'pan': ref[:, s0:s1] / 32768.0, **vs}
        versions = {k: fade(v) for k, v in versions.items()}
        loud = {k: meter(tuple(v), FS) for k, v in versions.items()}
        gain = {k: 10 ** ((loud['pan']['integrated'] - loud[k]['integrated']) / 20) for k in versions}
        top = max(loud[k]['true_peak'] + 20 * math.log10(gain[k]) for k in versions)
        common = 10 ** ((CEILING - top) / 20) if top > CEILING else 1.0
        wn = max(5, max(map(len, versions)))
        print('  %-*s %8s %8s %7s %7s' % (wn, '', 'gain dB', 'LUFS', 'dBTP', 'S/M dB'))
        written = {}
        for k, v in versions.items():
            path = os.path.join(a.out, '%s-%s.wav' % (name, k))
            y = written[k] = write_wav(path, v * gain[k] * common)
            m = meter(tuple(y), FS)
            sm = 10 * math.log10(np.sum((y[0] - y[1]) ** 2) / max(np.sum((y[0] + y[1]) ** 2), 1e-30))
            print('  %-*s %+8.2f %8.2f %7.2f %7.1f' % (wn, k, 20 * math.log10(gain[k] * common), m['integrated'],
                                                     m['true_peak'], sm))
        if name == 'bursts':                              # the render path end to end, against the table
            print('  measured on the files, the middle burst of each direction: ITD (us) and ILD (dB, 3 bands)')
            ks = ('head', 'amb1', 'amb3') + tuple(k for k in written if k.startswith('int-'))
            print('  %4s  %s' % ('az', '  '.join('%-23s' % k for k in ks)))
            for d, (b0, b1) in zip(az, mid):
                f = np.fft.rfftfreq(2 * (b1 - b0), 1 / FS)
                cells = []
                for k in ks:
                    itd, ild = itd_ild(*np.fft.rfft(written[k][:, b0:b1], 2 * (b1 - b0)), f, FS)
                    cells.append('%5.0f %5.1f %5.1f %5.1f' % ((itd,) + tuple(ild)))
                print('  %4.0f  %s' % (d, '  '.join(cells)))
        if name == 'elevation':                           # the pinna's notches end to end, against the analytic ones
            f = np.fft.rfftfreq(1024, 1 / FS)
            print('  measured on the files, the middle burst at each station: the deepest notch from 4 to 16 kHz of each '
                  'pinna version over\n  its version without, at the right ear (Welch, Hann windows of 1024 samples)')
            pairs = [('pinna', 'head'), ('pinna-rd', 'head'), ('amb3-pinna', 'amb3')] + [
                (k + '-pinna', k) for k in ('int-amb3', 'int-binaural') if k + '-pinna' in written]
            print('  %4s %4s  %s' % ('th', 'ph', ''.join('%-19s' % kp for kp, _ in pairs)))
            for i, (b0, b1) in enumerate(mid):
                cells = []
                for kp, k0 in pairs:
                    fq, db = notch(transfer(written[k0][1, b0:b1], written[kp][1, b0:b1]) * gain[k0] / gain[kp], f)
                    cells.append('%5.0f %5.1f dB       ' % (fq, db))
                print('  %4.0f %4.0f  %s' % (pc['th'][i], pc['ph'][i], ''.join(cells).rstrip()))
        if a.ambix:
            g = gain['amb3'] * common
            g = min(g, 0.999 / max(float(np.abs(fade(field)).max()), 1e-30))
            write_wav(os.path.join(a.out, '%s-ambix.wav' % name), fade(field) * g, bits=24)
            print('  ambix: the third-order field at %+.2f dB' % (20 * math.log10(g)))
        print('  wrote %s/%s-{%s}.wav in %.1f s' % (os.path.relpath(a.out), name, ','.join(versions) +
                                                     (',ambix' if a.ambix else ''), time.time() - t_start))


if __name__ == '__main__':
    main()
