#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Listening tools: measure a recording or a render (a host tool: floats are
allowed here, never in the library; , ). It reads a 16-bit PCM WAV,
mono or stereo, at any rate; convert anything else first:

    afconvert -f WAVE -d LEI16 song.mp3 song.wav           # macOS
    ffmpeg -i song.mp3 -c:a pcm_s16le song.wav             # anywhere

    python3 -B tools/music/analyze.py overview song.wav [--start S] [--length S]
    python3 -B tools/music/analyze.py grid     song.wav [--bpm LO-HI] [--block 8 | --sections 1-8,9-24]
    python3 -B tools/music/analyze.py harmony  song.wav [--bpm B] [--first-bar T]
    python3 -B tools/music/analyze.py spectro  song.wav OUT.png --bars 1-4 [--bpm B] [--first-bar T]
    python3 -B tools/music/analyze.py steps    render.wav --step S [--n N] [--t0 T]
    python3 -B tools/music/analyze.py loudness render.wav
    python3 -B tools/music/analyze.py loudness-check
    python3 -B tools/music/analyze.py playgain render.wav [COPY.wav]
    python3 -B tools/music/analyze.py describe RECIPE [--note MILLIHZ] [--vel Q15] [--seconds S] [--json [FILE]]
    python3 -B tools/music/analyze.py describe --all [--json [FILE]] [--out data/descriptors.tsv]
    python3 -B tools/music/analyze.py describe --selftest
    python3 -B tools/music/analyze.py search   [KEY=LO..HI ...] [--tag TAG ...] [--tsv FILE]

overview  loudness, stereo width, band shares, centroid and onset rate per
          window; tempo candidates per 20 s; the key per 10 s.
grid      the exact tempo, the beat and bar origin, a map of band energy per
          bar, and per block of bars the swing of its sixteenths and its
          two-bar step pattern per band.
harmony   a chord per half bar, and the bass note per sixteenth with counts
          and intervals.
spectro   log-frequency spectrograms (30-600 Hz and 150 Hz-16 kHz) with the
          bar, beat and sixteenth grid drawn on; needs matplotlib.
steps     per step of a staircase render: A-weighted and raw RMS over its
          sounding 50 ms windows, the spectral centroid, the share above 2 kHz.
          The steps start at --t0 (the segment's start by default) and must
          lie inside the segment.
loudness  the integrated loudness and the loudest 3 s (ITU-R BS.1770-4; a
          mono file counts twice, as it plays on two speakers), the true and
          sample peaks, and the largest mean over one second (the offset). A
          segment is metered as a file of its own: the filters start at rest.
loudness-check
          meters synthetic files and compares them with ffmpeg's ebur128 (if
          ffmpeg is on the PATH), to 0.01 LU; exit 1 on a miss.
playgain  the gain play.sh plays a file at : toward -16 LUFS
          integrated, but never so high that the true peak passes -1 dBTP;
          with COPY.wav, also writes the file at that gain. One line, no
          header.
describe  renders one note of a recipe with the harness  and measures
          its brightness, attack, decay, sustain, release, noisiness,
          inharmonicity, odd/even balance and peak; --all gives every
          recipe as a TSV (descriptors.tsv), --selftest checks the measures
          on a known recipe (exit 1 on a miss). It needs compose.py beside
          this file and runs make for the harness first, as compose.py
          does; --json prints JSON instead of the table, or writes its optional FILE.
search    the recipes of descriptors.tsv whose descriptors lie in every
          KEY=LO..HI range (LO or HI may be left out) and whose tags line in
          recipes.txt carries every --tag, sorted by the first KEY.

Every command but loudness-check, playgain, describe and search takes
--start/--length (a segment, in seconds); those and playgain take --json
FILE (the measurements, for other tools). Times print in the file's own
clock.
--bpm is a fixed tempo or a search range; without
--first-bar the grid is found from the onsets. --beats and --sub set the
beats per bar and the steps per beat (4 and 4 by default; 12 and 1 for a
piece written in eighths of 12/8). Everything needs numpy. README.md in this
directory, Listening tools, says how each quantity is measured and how far
to trust it.
"""
import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

NAMES = 'C C# D Eb E F F# G Ab A Bb B'.split()
BANDS = (('kick', 30, 100), ('body', 150, 400), ('pres', 1000, 5000), ('air', 7000, 16000))
DEC = 32                                   # onset envelope rate: fs/32, 0.7 ms at 44.1 kHz
MAJ = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])   # Krumhansl-Kessler
MIN = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def itd_ild(L, R, f, fs):
    """The ITD (us, from the cross-correlation below 1.5 kHz, within 48 frames, to 1/16 sample) and the ILD (dB in
    three bands) of the ears' spectra L and R (..., bins) at the frequencies f; right leading or louder is +."""
    # Preserve the original fixed-frame search: 1 ms at 48 kHz.
    up, span = 16, 48
    c = np.fft.irfft(L * np.conj(R) * (f <= 1500), 2 * (L.shape[-1] - 1) * up, axis=-1)
    c = np.concatenate((c[..., -span * up:], c[..., :span * up + 1]), -1)
    band = [(f >= lo) & (f < hi) for lo, hi in ((200, 1500), (1500, 6000), (6000, 16000))]
    return ((np.argmax(c, -1) - span * up) / up / fs * 1e6,
            np.stack([10 * np.log10((np.abs(R[..., m]) ** 2).sum(-1) / (np.abs(L[..., m]) ** 2).sum(-1)) for m in band], -1))


class Audio:
    """A segment of a 16-bit WAV: L, R, mid and side as floats in [-1, 1)."""

    def __init__(self, path, start=0.0, length=None):
        w = wave.open(path)
        if w.getsampwidth() != 2:
            sys.exit('analyze.py: %s is not 16-bit PCM; convert it first (see --help)' % path)
        fs, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        if ch not in (1, 2):
            sys.exit('analyze.py: %s has %d channels; only mono and stereo are measured' % (path, ch))
        a = min(max(int(round(start * fs)), 0), n)
        m = n - a if length is None else min(n - a, int(round(length * fs)))
        if m <= 0:
            sys.exit('analyze.py: the segment is empty; %s has %.3f s' % (path, n / fs))
        w.setpos(a)
        x = np.frombuffer(w.readframes(m), dtype='<i2').reshape(-1, ch).astype(np.float64) / 32768.0
        self.fs, self.t0, self.channels = fs, a / fs, ch
        self.L = x[:, 0]
        self.R = x[:, 1] if ch > 1 else x[:, 0]
        self.mid = 0.5 * (self.L + self.R)
        self.side = 0.5 * (self.L - self.R)
        self.dur = len(self.mid) / fs
        self.peak = float(np.max(np.abs(x))) if len(x) else 0.0

    def clock(self, t):
        """A segment time as m:ss.s of the file (0:00.0 for a time before its start)."""
        t = max(0.0, self.t0 + t)
        return '%d:%04.1f' % (int(t // 60), t % 60)


def db(p):
    return 10 * np.log10(np.maximum(p, 1e-20))


def stft(sig, fs, nfft, hop):
    """Magnitudes (frames x bins), the bin frequencies and the frame centres in seconds."""
    nfr = 1 + (len(sig) - nfft) // hop
    if nfr < 1:
        sys.exit('analyze.py: the segment is shorter than %d samples' % nfft)
    win = np.hanning(nfft)
    out = np.empty((nfr, nfft // 2 + 1), dtype=np.float32)
    for i0 in range(0, nfr, 1024):
        i1 = min(nfr, i0 + 1024)
        idx = np.arange(i0, i1)[:, None] * hop + np.arange(nfft)[None, :]
        out[i0:i1] = np.abs(np.fft.rfft(sig[idx] * win, axis=1))
    return out, np.fft.rfftfreq(nfft, 1.0 / fs), (np.arange(nfr) * hop + nfft / 2) / fs


class Onsets:
    """Per band: the energy envelope and its onset strength, the rise of its log."""

    def __init__(self, au):
        X = np.fft.rfft(au.mid)
        fx = np.fft.rfftfreq(len(au.mid), 1.0 / au.fs)
        self.fe = au.fs / DEC
        self.E, self.O = {}, {}
        for nm, lo, hi in BANDS:
            sig = np.fft.irfft(X * ((fx >= lo) & (fx < hi)), len(au.mid))
            e = sig[:len(sig) // DEC * DEC] ** 2
            e = e.reshape(-1, DEC).mean(axis=1)
            k = max(1, int(0.003 * self.fe))
            e = np.convolve(e, np.ones(k) / k, mode='same')
            le = np.log10(e + 1e-10)
            self.E[nm] = e
            self.O[nm] = np.maximum(np.diff(le, prepend=le[0]), 0)
        self.O['all'] = sum(self.O[nm] / (np.mean(self.O[nm]) + 1e-12) for nm, _, _ in BANDS)
        self.t = np.arange(len(self.O['all'])) / self.fe

    def coarse(self, o, d):
        """o summed over blocks of d, with the block centres."""
        n = len(o) // d * d
        return o[:n].reshape(-1, d).sum(axis=1), (np.arange(n // d) * d + (d - 1) / 2) / self.fe


def dft(o, t, f):
    """Magnitude and phase of the mean-removed envelope o at frequency f."""
    z = np.dot(o - o.mean(), np.exp(-2j * np.pi * f * t))
    return abs(z), np.angle(z)


def tempo_scan(o, t, lo, hi, step, harmonics=(1,)):
    """The DFT magnitude of the mean-removed envelope o at each candidate beat rate, summed over
    the harmonics (2 and 4: the eighth and sixteenth rates), from lo up to (not including) hi."""
    bpms = np.arange(lo, hi - step / 2, step)
    e = o - o.mean()
    harmonics = tuple(harmonics)
    if not len(bpms) or not harmonics:
        return bpms, np.zeros(len(bpms))
    # Candidate-major pairs preserve the original phase expression's operation
    # order. One in-place exponential matrix is at most 64 MiB; very long
    # envelopes also split in time rather than allocating an oversized column.
    phases = (-2j * np.pi * np.asarray(harmonics)[None, :] * bpms[:, None] / 60.0).ravel()
    elements = (64 * 1024 * 1024) // np.dtype(np.complex128).itemsize
    samples = min(max(1, len(t)), elements)
    pairs = max(1, elements // samples)
    values = np.zeros(len(phases), dtype=np.complex128)
    for start in range(0, len(phases), pairs):
        stop = min(start + pairs, len(phases))
        for first in range(0, len(t), samples):
            last = first + samples
            matrix = phases[start:stop, None] * t[None, first:last]
            np.exp(matrix, out=matrix)
            values[start:stop] += matrix @ e[first:last]
    mags = np.abs(values).reshape(len(bpms), len(harmonics)).sum(axis=1)
    return bpms, mags


def top_distinct(bpms, mags, k=4, apart=1.0):
    picks = []
    for i in np.argsort(-mags):
        if all(abs(bpms[i] - bpms[j]) > apart for j in picks):
            picks.append(i)
        if len(picks) == k:
            break
    return [(float(bpms[i]), float(mags[i] / mags[picks[0]])) for i in picks]


def chroma(S, f, lo, hi, weighted, power):
    """Energy per pitch class (0 = C) per frame: each bin goes to its nearest pitch class, weighted
    (if asked) by its closeness to the semitone centre; power sums |X|^2, otherwise |X|."""
    sel = (f >= lo) & (f <= hi)
    m = 69 + 12 * np.log2(f[sel] / 440.0)
    pc = np.round(m).astype(int) % 12
    wgt = np.clip(1 - np.abs(m - np.round(m)) / 0.35, 0, 1) if weighted else np.ones(len(m))
    P = S[:, sel].astype(np.float64) ** (2 if power else 1)
    C = np.zeros((S.shape[0], 12))
    for q in range(12):
        k = pc == q
        C[:, q] = (P[:, k] * wgt[k]).sum(axis=1)
    return C


def keys_of(ch):
    c = np.sqrt(ch)
    if np.ptp(c) == 0:
        return [(None, name + ' ' + mode) for name in NAMES for mode in ('major', 'minor')]
    res = []
    for r in range(12):
        res.append((float(np.corrcoef(c, np.roll(MAJ, r))[0, 1]), NAMES[r] + ' major'))
        res.append((float(np.corrcoef(c, np.roll(MIN, r))[0, 1]), NAMES[r] + ' minor'))
    res.sort(reverse=True)
    return res


def find_grid(au, on, bpm_arg, first_bar=None, beats=4):
    """The tempo (fixed or searched), the beat origin and the bar origin, in segment seconds."""
    if '-' in bpm_arg:
        o = on.O['all']
        lo, hi = (float(v) for v in bpm_arg.split('-'))
        c8, t8 = on.coarse(o, 8)
        bpms, mags = tempo_scan(c8, t8, lo, hi, 0.05, (1, 2, 4))
        b0 = bpms[np.argmax(mags)]
        c2, t2 = on.coarse(o, 2)
        bpms, mags = tempo_scan(c2, t2, b0 - 0.6, b0 + 0.6, 0.002, (1, 2, 4))
        bpm = float(bpms[np.argmax(mags)])
    else:
        bpm = float(bpm_arg)
    beat = 60.0 / bpm
    bar = beats * beat
    if first_bar is not None:
        t_bar = first_bar - au.t0
        return dict(bpm=bpm, beat=beat, bar=bar, first_beat=t_bar % beat, first_bar=t_bar % bar, found=False)
    # the eighth grid: the phase of the eighth-note rate, moved to the most onset within a tenth of a beat
    # (the all-band onsets folded onto one beat in 96 bins, smoothed over 5, a bin plus the bin half a
    # beat on), since an eighth-note swing pulls the phase late
    _, ph = dft(on.O['all'], on.t, 2.0 / beat)
    c0 = int(round((-ph / (2 * np.pi)) % 1.0 * 48))
    h = np.zeros(96)
    np.add.at(h, (on.t / beat % 1.0 * 96).astype(int) % 96, on.O['all'])
    hs = sum(np.roll(h, d) * (3 - abs(d)) for d in range(-2, 3))
    pair = hs[:48] + hs[48:]
    i = max(range(c0 - 9, c0 + 10), key=lambda c: pair[c % 48])
    a, b, c = pair[(i - 1) % 48], pair[i % 48], pair[(i + 1) % 48]
    t8 = ((i + (0.5 * (a - c) / (a - 2 * b + c) if a - 2 * b + c != 0 else 0.0)) / 96 * beat) % (beat / 2)
    # of the two eighths, the beat: the log ratio of their all-band onset, plus a quarter of the log ratio
    # of the kick band's energy rise, so that a kick on the beat outvotes off-beat hats
    pos = ((on.t - t8) / beat) % 1.0
    rise = np.maximum(np.diff(on.E['kick'], prepend=on.E['kick'][0]), 0)
    ons = [on.O['all'][np.abs((pos - q + 0.5) % 1.0 - 0.5) < 0.05].sum() for q in (0.0, 0.5)]
    kik = [rise[(pos - q + 0.03) % 1.0 < 0.18].sum() for q in (0.0, 0.5)]
    vote = float(np.log(ons[0] / ons[1]) + 0.25 * np.log(kik[0] / kik[1]))
    t_beat = t8 + (0.0 if vote >= 0 else beat / 2)
    # the bar: per beat, how far its chroma (180-1500 Hz) and its bass chroma (35-200 Hz) moved from the
    # beat before, and the kick-band energy in its first quarter; each summed per beat of the bar and
    # scaled to sum 1, and the three added: beat 1 scores most
    S, f, t = stft(au.mid, au.fs, 8192, 1024)
    nb = int((au.dur - t_beat) // beat)
    k = np.floor((t - t_beat) / beat).astype(int)
    sel = (k >= 0) & (k < nb)
    score = np.zeros(beats)
    for lo, hi in ((180, 1500), (35, 200)):
        M = np.zeros((nb, 12))
        np.add.at(M, k[sel], chroma(S, f, lo, hi, True, False)[sel])
        M = np.sqrt(M)
        n = np.linalg.norm(M, axis=1) + 1e-12
        v = np.zeros(beats)
        np.add.at(v, np.arange(1, nb) % beats, 1 - np.sum(M[1:] * M[:-1], axis=1) / (n[1:] * n[:-1]))
        score += v / max(v.sum(), 1e-12)
    q = (on.t - t_beat) / beat
    first = (q >= 0) & (q < nb) & (q % 1.0 < 0.25)
    v = np.zeros(beats)
    np.add.at(v, q[first].astype(int) % beats, on.E['kick'][first])
    score += v / max(v.sum(), 1e-12)
    # an origin up to a tenth of a beat before the segment's start stays there, so a render's bar 1 is bar 1
    t_bar = (t_beat + int(np.argmax(score)) * beat + 0.1 * beat) % bar - 0.1 * beat
    t_beat = (t_beat + 0.1 * beat) % beat - 0.1 * beat
    return dict(bpm=bpm, beat=beat, bar=bar, first_beat=t_beat, first_bar=t_bar, found=True,
                beat_vote=abs(vote), bar_scores=[float(s) for s in np.roll(score, -int(np.argmax(score)))])


def flux_peaks(flux, fps):
    """Onset picks, in seconds: local maxima of the flux above 1.5 times a 2 s moving median, 60 ms apart."""
    k = int(fps)
    med = np.repeat([np.median(flux[max(0, i - k):i + k]) for i in range(0, len(flux), 8)], 8)[:len(flux)]
    out, last = [], -1e9
    for i in range(1, len(flux) - 1):
        if flux[i] > flux[i - 1] and flux[i] >= flux[i + 1] and flux[i] > 1.5 * med[i] + 1e-9 and (i - last) / fps >= 0.06:
            out.append(i)
            last = i
    return np.array(out) / fps


# ---------------------------------------------------------------- commands

def cmd_overview(au, a):
    S, f, t = stft(au.mid, au.fs, 2048, 256)
    fps = au.fs / 256
    LS = np.log1p(1000 * S)                   # onset strength: the positive log-spectral flux, all bins
    flux = np.concatenate([[0], np.maximum(LS[1:] - LS[:-1], 0).sum(axis=1)])
    del LS
    P = S.astype(np.float64) ** 2
    bw = (('sub', 20, 60), ('bass', 60, 250), ('lowmid', 250, 1000), ('mid', 1000, 4000), ('high', 4000, 16000))
    band = {nm: P[:, (f >= lo) & (f < hi)].sum(axis=1) for nm, lo, hi in bw}
    tot = P[:, (f >= 20) & (f < 16000)].sum(axis=1)
    cent = (P * f).sum(axis=1) / np.maximum(P.sum(axis=1), 1e-20)
    ons = flux_peaks(flux, fps)
    W = a.window
    print('== per %g s: mid RMS dBFS, side-mid dB, L/R correlation, band share of the total dB, centroid Hz, onsets/s' % W)
    print('%-8s %6s %6s %5s | %6s %6s %6s %6s %6s | %6s %5s' % ('time', 'rms', 's-m', 'corr', *[b[0] for b in bw], 'cent', 'ons'))
    rows = []
    count = max(1, int(au.dur // W))
    for k in range(count):
        end = au.dur if k == count - 1 else (k + 1) * W
        length = end - k * W
        i0 = int(k * W * au.fs)
        i1 = len(au.mid) if k == count - 1 else int(end * au.fs)
        rm = db(np.mean(au.mid[i0:i1] ** 2))
        sm = db(np.mean(au.side[i0:i1] ** 2)) - rm if au.channels > 1 else None
        left, right = au.L[i0:i1], au.R[i0:i1]
        cc = (None if np.ptp(left) == 0 or np.ptp(right) == 0 else
              float(np.corrcoef(left, right)[0, 1]) if au.channels > 1 else 1.0)
        sel = (t >= k * W) & (t < end)
        tt = max(tot[sel].sum(), 1e-20)
        shares = [float(db(band[nm][sel].sum() / tt)) for nm, _, _ in bw]
        ce = float(np.sum(cent[sel] * tot[sel]) / tt)
        rate = float(np.sum((ons >= k * W) & (ons < end)) / length)
        rows.append(dict(t=au.t0 + k * W, length=length, rms=float(rm), side_mid=sm, corr=cc, shares=shares, centroid=ce, onsets=rate))
        print('%-8s %6.1f %6s %5s | %6.1f %6.1f %6.1f %6.1f %6.1f | %6.0f %5.1f'
              % (au.clock(k * W), rm, 'mono' if sm is None else '%.1f' % sm, '-' if cc is None else '%.2f' % cc, *shares, ce, rate))
    rms = float(db(np.mean(au.mid ** 2)))
    pk = float(20 * np.log10(max(au.peak, 1e-10)))
    print('\n== whole segment: peak %.2f dBFS, mid RMS %.2f dBFS, crest %.1f dB, %.1f onsets/s'
          % (pk, rms, pk - rms, len(ons) / au.dur))
    print('\n== tempo per 20 s (the four strongest beat rates of the flux, relative)')
    tf = np.arange(len(flux)) / fps
    tempo = []
    count = max(1, int(au.dur // 20))
    for k in range(count):
        end = au.dur if k == count - 1 else 20 * (k + 1)
        sel = slice(int(20 * k * fps), None if k == count - 1 else int(end * fps))
        cand = top_distinct(*tempo_scan(flux[sel], tf[sel], 70, 180, 0.02))
        tempo.append(dict(t=au.t0 + 20 * k, length=end - 20 * k, candidates=cand))
        print('%-8s ' % au.clock(20 * k) + '  '.join('%6.2f (%.2f)' % c for c in cand))
    cand = top_distinct(*tempo_scan(flux, tf, 70, 180, 0.02))
    print('%-8s ' % 'all' + '  '.join('%6.2f (%.2f)' % c for c in cand))
    print('\n== key per 10 s (Krumhansl-Kessler profiles on the 60-2000 Hz chroma) and the chroma, 0-9')
    SK, fk, tk = stft(au.mid, au.fs, 16384, 4096)
    CH = chroma(SK, fk, 60, 2000, weighted=False, power=True)
    keys = []
    count = max(1, int(au.dur // 10))
    for k in range(count):
        end = au.dur if k == count - 1 else 10 * (k + 1)
        ch = CH[(tk >= 10 * k) & (tk < end)].sum(axis=0)
        ks = keys_of(ch)
        v = np.sqrt(ch)
        v = np.round(9 * v / max(v.max(), 1e-20)).astype(int)
        keys.append(dict(t=au.t0 + 10 * k, length=end - 10 * k, keys=ks[:3], chroma=[float(q) for q in ch]))
        print('%-8s %s   %s' % (au.clock(10 * k), '  '.join('%-9s %s' % (nm, '-' if c is None else '%.2f' % c) for c, nm in ks[:3]),
                                 ' '.join('%s%d' % (NAMES[q], v[q]) for q in range(12))))
    ks = keys_of(CH.sum(axis=0))
    print('%-8s %s' % ('all', '  '.join('%-9s %s' % (nm, '-' if c is None else '%.2f' % c) for c, nm in ks[:4])))
    return dict(windows=rows, peak_dbfs=pk, rms_dbfs=rms, tempo=tempo, tempo_all=cand, key=keys, key_all=ks[:4])


def spark(h):
    s = ' .:-=+*#%@'
    v = h / max(h.max(), 1e-12)
    return ''.join(s[min(9, int(round(9 * q)))] for q in v)


def blocks_of(a, nbars):
    if a.sections:
        out = []
        for part in a.sections.split(','):
            b0, b1 = (int(v) for v in part.split('-'))
            out.append((b0, min(b1, nbars)))
        return out
    return [(b0, min(nbars, b0 + a.block - 1)) for b0 in range(1, nbars + 1, a.block)]


def cmd_grid(au, a):
    on = Onsets(au)
    g = find_grid(au, on, a.bpm or '80-180', a.first_bar, a.beats)
    beat, bar = g['beat'], g['bar']
    print('tempo %.3f bpm: beat %.4f s, bar of %d beats %.4f s' % (g['bpm'], beat, a.beats, bar))
    if '-' in (a.bpm or '-'):
        o = on.O['all']
        c2, t2 = on.coarse(o, 2)
        drift = []
        for w0 in np.arange(0, au.dur - 20, 40):
            sel = (t2 >= w0) & (t2 < w0 + 40)
            bp, mg = tempo_scan(c2[sel], t2[sel], g['bpm'] - 1, g['bpm'] + 1, 0.01, (1, 4))
            drift.append(float(bp[np.argmax(mg)]))
            print('  %s-%s: %.2f bpm' % (au.clock(w0), au.clock(min(au.dur, w0 + 40)), drift[-1]))
    t_bar = g['first_bar']
    nbars = int((au.dur - t_bar) // bar)
    print('beat origin %s, first bar %s (%s)' % (au.clock(g['first_beat']), au.clock(t_bar),
                                                 'found from the onsets' if g['found'] else 'given'))
    if g['found']:
        print('  the beat outvotes the off-beat by %.2f; the beats of the bar score %s from the one'
              % (g['beat_vote'], ' '.join('%.2f' % s for s in g['bar_scores'])))
        print('  the bar origin can sit a beat or two off, and on off-beat-heavy music the beat half a beat:')
        print('  check both by ear or in spectro, then pass --first-bar')
    print('\n== per bar: band energy dB below each band\'s loudest bar (kick, body, pres, air), side/mid dB, # the kick')
    rows = []
    for b in range(nbars):
        t0 = t_bar + b * bar
        sel = (on.t >= t0) & (on.t < t0 + bar)
        r = [float(db(np.mean(on.E[nm][sel]))) for nm, _, _ in BANDS]
        i0, i1 = max(0, int(t0 * au.fs)), int((t0 + bar) * au.fs)
        sm = float(db(np.mean(au.side[i0:i1] ** 2)) - db(np.mean(au.mid[i0:i1] ** 2))) if au.channels > 1 else None
        rows.append(r + [sm])
    ref = np.max(np.array([r[:4] for r in rows]), axis=0) if rows else np.zeros(4)
    for b, r in enumerate(rows):
        rel = np.array(r[:4]) - ref
        print('bar %3d %-8s %6.1f %6.1f %6.1f %6.1f  sm %6s  %s' % (b + 1, au.clock(t_bar + b * bar), *rel,
              'mono' if r[4] is None else '%.1f' % r[4], '#' * int(max(0, 20 + rel[0]))))
    NB = 48

    def hist_beat(o, ta, tb):
        """Onset strength folded onto one beat, each frame in the bin nearest its place."""
        sel = (on.t >= ta) & (on.t < tb)
        pos = np.round(((on.t[sel] - g['first_beat']) / beat) % 1.0 * NB).astype(int) % NB
        h = np.zeros(NB)
        np.add.at(h, pos, o[sel])
        return h

    def peak_pos(h, lo, hi):
        """The highest local maximum of h from bin lo to hi (wrapping), refined by a parabola
        through it and its neighbours, in beats, and its height. A window holding only the
        slope of a peak outside it has no maximum: its middle, height 0."""
        best = None
        for q in range(lo, hi + 1):
            a, b, c = h[(q - 1) % NB], h[q % NB], h[(q + 1) % NB]
            if b > 0 and b >= a and b >= c and (best is None or b > best[1]):
                best = (q, b, a, c)
        if best is None:
            return 0.5 * (lo + hi) / NB, 0.0
        q, b, a, c = best
        den = a - 2 * b + c
        return (q + (0.5 * (a - c) / den if den != 0 else 0.0)) / NB, float(b)

    def swing_of(h):
        """The four sixteenths' places in beats, each half beat's swing (its second sixteenth's
        place as a % of its eighth) with its weight, and the swing, their weighted mean. A
        straight sixteenth is looked for within 3 bins of its place (a sixteenth is 12 bins), a
        swung one from 2 bins before its straight place to 2 short of the next, since swing
        only delays. A half's weight is its peak's height above the histogram's median, as a
        fraction of the highest bin's. It counts if at least 0.15 and a third of the other
        half's: a section that never plays one of them leaves its window to stray onsets. If
        neither counts, there is no swing to measure (None)."""
        med = float(np.median(h))
        span = max(float(h.max()) - med, 1e-12)
        (p0, _), (p2, _) = peak_pos(h, -3, 3), peak_pos(h, 21, 27)
        (p1, h1), (p3, h3) = peak_pos(h, 10, 22), peak_pos(h, 34, 46)
        s1 = 100 * (p1 - p0) / max(p2 - p0, 1e-9)
        s3 = 100 * (p3 - p2) / max(1 + p0 - p2, 1e-9)
        r1, r3 = max(0.0, (h1 - med) / span), max(0.0, (h3 - med) / span)
        w1 = r1 if r1 >= 0.15 and r1 >= r3 / 3 else 0.0
        w3 = r3 if r3 >= 0.15 and r3 >= r1 / 3 else 0.0
        sw = (w1 * s1 + w3 * s3) / (w1 + w3) if w1 + w3 > 0 else None
        return [p0, p1, p2, p3], [[s1, w1], [s3, w3]], sw

    SUB = 12

    def steps2(o, b0, b1):
        nst = 2 * a.beats * a.sub                  # the steps of two bars
        nbin = nst * SUB
        h = np.zeros(nbin)
        for b in range(b0 - 1, b1 - 1, 2):
            t0 = t_bar + b * bar
            sel = (on.t >= t0) & (on.t < t0 + 2 * bar)
            pos = ((on.t[sel] - t0) / (2 * bar) * nbin).astype(int) % nbin
            np.add.at(h, pos, o[sel])
        s = np.array([h[[(i * SUB + d) % nbin for d in range(-3, 8)]].max() for i in range(nst)])
        base = np.median(s)
        v = np.clip((s - base) / max(s.max() - base, 1e-12), 0, 1)
        return np.round(9 * v).astype(int)

    print('\n== per block: onset histogram over one beat (48 bins; | marks the straight sixteenths), the measured')
    print('   sixteenth positions in beats, each half beat\'s swing x its weight, the swing (the second sixteenth')
    print('   of an eighth as a % of it; 50 straight, 67 triplet), and the two-bar step pattern per band (0-9')
    print('   above the block\'s median step)')
    print('                   ' + ''.join('|' if q % 12 == 0 else ' ' for q in range(NB)))
    blocks = []
    for b0, b1 in blocks_of(a, nbars):
        ta, tb = t_bar + (b0 - 1) * bar, t_bar + b1 * bar
        ha, hl = hist_beat(on.O['air'], ta, tb), hist_beat(on.O['all'], ta, tb)
        print('bars %3d-%3d  air %s' % (b0, b1, spark(ha)))
        print('             all %s' % spark(hl))
        pa = pl = sw_a = sw_l = hfa = hfl = None
        if a.sub == 4:                          # the swing of sixteenths: four steps to the beat
            pa, hfa, sw_a = swing_of(ha)
            pl, hfl, sw_l = swing_of(hl)
            for nm, ps, hf, sw in (('air', pa, hfa, sw_a), ('all', pl, hfl, sw_l)):
                print('   %s: sixteenths %s, halves %s -> swing %s' % (
                    nm, ' '.join('%.3f' % p for p in ps), ', '.join('%.0f%% x %.2f' % tuple(x) for x in hf),
                    'none measurable' if sw is None else '%.0f%%' % sw))
        pats = {}
        for nm, _, _ in BANDS:
            v = steps2(on.O[nm], b0, b1)
            pats[nm] = [int(q) for q in v]
            print('   %-5s %s' % (nm, ''.join(('|' if i % a.sub == 0 else '') + ('.' if q == 0 else str(q)) for i, q in enumerate(v))))
        blocks.append(dict(bars=[b0, b1], sixteenths_air=pa, sixteenths_all=pl, swing_air=sw_a, swing_all=sw_l,
                           halves_air=hfa, halves_all=hfl, steps=pats))
    return dict(grid=g, bars=rows, blocks=blocks)


def chord_templates():
    T = {}
    for r in range(12):
        for nm, iv in (('', (0, 4, 7)), ('m', (0, 3, 7)), ('sus4', (0, 5, 7)), ('sus2', (0, 2, 7)),
                       ('7', (0, 4, 7, 10)), ('m7', (0, 3, 7, 10)), ('maj7', (0, 4, 7, 11)), ('dim', (0, 3, 6)),
                       ('5', (0, 7)), ('add9', (0, 2, 4, 7)), ('m9', (0, 2, 3, 7, 10))):
            v = np.zeros(12)
            v[[(r + i) % 12 for i in iv]] = 1
            T[NAMES[r] + nm] = v - v.mean()
    return T


def cmd_harmony(au, a):
    on = Onsets(au)
    g = find_grid(au, on, a.bpm or '80-180', a.first_bar, a.beats)
    beat, bar, t_bar = g['beat'], g['bar'], g['first_bar']
    nbars = int((au.dur - t_bar) // bar)
    print('grid: %.3f bpm, first bar %s' % (g['bpm'], au.clock(t_bar)))
    S, f, t = stft(au.mid, au.fs, 8192, 512)
    CH = chroma(S, f, 180, 1500, weighted=True, power=False)
    T = chord_templates()
    print('\n== chord per half bar: the two best templates on the 180-1500 Hz chroma (correlation), and the four')
    print('   strongest pitch classes')
    chords = []
    for b in range(nbars):
        cells = []
        for h in range(2):
            t0 = t_bar + b * bar + h * bar / 2
            ch = CH[(t >= t0) & (t < t0 + bar / 2)].sum(axis=0)
            c = np.sqrt(ch)
            c = c - c.mean()
            sc = sorted(((float(np.dot(c, v) / (np.linalg.norm(c) * np.linalg.norm(v) + 1e-12)), nm) for nm, v in T.items()), reverse=True)
            top = [NAMES[q] for q in np.argsort(-ch)[:4]]
            cells.append(dict(best=sc[0][1], score=sc[0][0], second=sc[1][1], top=top))
        chords.append(cells)
        print('bar %3d %-8s %s' % (b + 1, au.clock(t_bar + b * bar), '  |  '.join(
            '%-6s(%.2f) %-6s [%s]' % (c['best'], c['score'], c['second'], ' '.join(c['top'])) for c in cells)))
    # bass: the strongest 35-200 Hz peak per frame, kept when stable and tonal, then the median per step
    sel = (f >= 35) & (f <= 200)
    fb, Pb = f[sel], S[:, sel].astype(np.float64)
    j = np.argmax(Pb, axis=1)
    rows = np.arange(len(j))
    jj = np.clip(j, 1, Pb.shape[1] - 2)
    la, lb, lc = (np.log(Pb[rows, jj + d] + 1e-12) for d in (-1, 0, 1))
    den = la - 2 * lb + lc
    off = np.where((j == jj) & (den != 0), 0.5 * (la - lc) / np.where(den == 0, 1, den), 0.0)
    mf = 69 + 12 * np.log2((fb[j] + off * (f[1] - f[0])) / 440.0)
    tonal = Pb[rows, j] / (Pb.mean(axis=1) + 1e-12)
    stable = np.zeros(len(j), bool)
    stable[1:-1] = (np.abs(mf[1:-1] - mf[:-2]) < 0.4) & (np.abs(mf[2:] - mf[1:-1]) < 0.4) & (tonal[1:-1] > 6)
    step = beat / a.sub
    nst = int((au.dur - t_bar) // step)
    seq = []
    for k in range(nst):
        s = (t >= t_bar + k * step) & (t < t_bar + (k + 1) * step) & stable
        seq.append(int(round(float(np.median(mf[s])))) if s.sum() >= 2 else None)
    nm = lambda m: NAMES[m % 12] + str(m // 12 - 1)
    counts = {}
    for m in seq:
        if m is not None:
            counts[nm(m)] = counts.get(nm(m), 0) + 1
    ivs, prev = {}, None
    for m in seq:
        if m is not None:
            if prev is not None and m != prev:
                ivs[m - prev] = ivs.get(m - prev, 0) + 1
            prev = m
    held = sum(m is not None for m in seq)
    print('\n== bass: a stable pitch on %d of %d steps; notes by count' % (held, nst))
    print('  ' + '  '.join('%s %d' % kv for kv in sorted(counts.items(), key=lambda kv: -kv[1])[:14]))
    print('== bass: intervals between successive different notes, semitones: count')
    print('  ' + '  '.join('%+d: %d' % kv for kv in sorted(ivs.items(), key=lambda kv: -kv[1])[:16]))
    print('== bass line, the first two bars of each block (- no stable pitch)')
    lines = []
    for b0, b1 in blocks_of(a, nbars):
        k0 = (b0 - 1) * a.sub * a.beats
        row = seq[k0:k0 + 2 * a.sub * a.beats]
        lines.append(dict(bars=[b0, b0 + 1], notes=[None if m is None else nm(m) for m in row]))
        print('bars %3d-%3d: %s' % (b0, b0 + 1, ' '.join(('|' if i % a.sub == 0 else '') + ('%-3s' % nm(m) if m is not None else ' - ')
                                                       for i, m in enumerate(row))))
    return dict(grid=g, chords=chords, bass_counts=counts, bass_intervals={str(k): v for k, v in ivs.items()},
                bass_held=held, bass_steps=nst, bass_lines=lines)


def cmd_spectro(au, a):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    on = Onsets(au) if a.first_bar is None or not a.bpm or '-' in a.bpm else None
    g = find_grid(au, on, a.bpm or '80-180', a.first_bar, a.beats)
    beat, bar = g['beat'], g['bar']
    b0, b1 = (int(v) for v in a.bars.split('-'))
    ta, tb = g['first_bar'] + (b0 - 1) * bar, min(au.dur, g['first_bar'] + b1 * bar)
    fig, axes = plt.subplots(2, 1, figsize=(22, 13), gridspec_kw={'height_ratios': [1, 1.4]})
    for ax, (nfft, hop, flo, fhi, title) in zip(axes, ((8192, 256, 30, 600, 'low, 30-600 Hz, nfft 8192'),
                                                       (1024, 128, 150, min(16000, au.fs / 2), 'full, 150 Hz-16 kHz, nfft 1024'))):
        i0 = max(0, int(ta * au.fs) - nfft // 2)
        i1 = min(len(au.mid), int(tb * au.fs) + nfft // 2)
        S, f, t = stft(au.mid[i0:i1], au.fs, nfft, hop)
        t = t + i0 / au.fs
        D = 20 * np.log10(S.T + 1e-9)
        sel = (f >= flo) & (f <= fhi)
        vmax = D[sel].max()
        ax.pcolormesh(t - ta, f[sel], D[sel], shading='auto', cmap='magma', vmin=vmax - 70, vmax=vmax)
        ax.set_yscale('log')
        ax.set_ylim(flo, fhi)
        ax.set_xlim(0, tb - ta)
        sub = beat / a.sub
        for k in range(int(round((tb - ta) / sub)) + 1):
            lw, al = (1.6, 0.9) if k % (a.sub * a.beats) == 0 else ((0.8, 0.6) if k % a.sub == 0 else (0.4, 0.3))
            ax.axvline(k * sub, color='cyan', lw=lw, alpha=al)
        if flo < 100:
            q = NAMES.index(a.note)
            for m in range(24, 84):
                fr = 440 * 2 ** ((m - 69) / 12)
                if m % 12 == q and flo <= fr <= fhi:
                    ax.axhline(fr, color='lime', lw=0.5, alpha=0.5)
                    ax.text(0.002, fr, '%s%d' % (a.note, m // 12 - 1), color='lime', fontsize=8)
        nb = int(round((tb - ta) / beat))
        ax.set_xticks([k * beat for k in range(nb + 1)])
        ax.set_xticklabels(['%d.%d' % (b0 + k // a.beats, k % a.beats + 1) for k in range(nb + 1)], fontsize=7)
        ax.set_title('bars %d-%d from %s, %.2f bpm: %s' % (b0, b1, au.clock(ta), g['bpm'], title))
    plt.tight_layout()
    plt.savefig(a.out, dpi=72)
    print('wrote %s: bars %d-%d, %s to %s' % (a.out, b0, b1, au.clock(ta), au.clock(tb)))
    return dict(grid=g, png=a.out, bars=[b0, b1])


def cmd_steps(au, a):
    x = au.mid
    fs = au.fs
    t0 = au.t0 if a.t0 is None else a.t0              # the first step, seconds in the file
    if int((t0 - au.t0) * fs) < 0 or int((t0 - au.t0 + a.n * a.step) * fs) > len(x):
        sys.exit('analyze.py: steps from %.3f to %.3f s fall outside the segment, %.3f to %.3f s'
                 % (t0, t0 + a.n * a.step, au.t0, au.t0 + au.dur))
    f = np.fft.rfftfreq(len(x), 1.0 / fs)
    f2 = f * f                                        # the A-weighting curve (IEC 61672), 0 dB at 1 kHz
    ra = (12194.0 ** 2 * f2 * f2) / ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194.0 ** 2))
    xa = np.fft.irfft(np.fft.rfft(x) * ra * 10 ** (2.0 / 20), len(x))
    m = int(fs * 0.05)
    N = 4096
    win = np.hanning(N)
    fq = np.fft.rfftfreq(N, 1.0 / fs)
    print('step  from-to s        A-weighted  raw RMS   (dB over the sounding 50 ms windows)  centroid  above 2 kHz')
    out = []
    for k in range(a.n):
        i0, i1 = int((t0 - au.t0 + k * a.step) * fs), int((t0 - au.t0 + (k + 1) * a.step) * fs)
        seg, raw = xa[i0:i1], x[i0:i1]
        e = (seg[:len(seg) // m * m].reshape(-1, m) ** 2).mean(axis=1)
        r = (raw[:len(raw) // m * m].reshape(-1, m) ** 2).mean(axis=1)
        keep = r > 1e-6
        P = np.zeros(len(fq))
        for i in range(0, len(raw) - N, N // 2):
            fr = raw[i:i + N]
            if np.sqrt(np.mean(fr ** 2)) >= 10 ** (-50 / 20):
                P += np.abs(np.fft.rfft(fr * win)) ** 2
        cen = float((fq * P).sum() / max(P.sum(), 1e-20))
        hi = float(P[fq > 2000].sum() / max(P.sum(), 1e-20))
        aw = float(db(e[keep].mean())) if keep.any() else None
        rw = float(db(r[keep].mean())) if keep.any() else None
        out.append(dict(step=k + 1, a_weighted=aw, raw=rw, centroid=cen, above_2k=hi))
        print('%4d  %6.2f-%6.2f   %9s %9s %42.0f Hz  %5.1f%%' % (k + 1, au.t0 + i0 / fs, au.t0 + i1 / fs,
              '-' if aw is None else '%.2f' % aw, '-' if rw is None else '%.2f' % rw, cen, 100 * hi))
    return dict(steps=out)


def kfilters(fs):
    """BS.1770-4's two K-weighting biquads, (b, a) for the high shelf and then the high pass. These are
    libebur128's constants: at 48 kHz they give the standard's coefficients, and they hold at any rate."""
    K, Q = np.tan(np.pi * 1681.974450955533 / fs), 0.7071752369554196      # the high shelf, +4 dB
    Vh = 10 ** (3.999843853973347 / 20)
    Vb = Vh ** 0.4996667741545416
    a0 = 1 + K / Q + K * K
    shelf = (((Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0),
             (1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0))
    K, Q = np.tan(np.pi * 38.13547087602444 / fs), 0.5003270373238773       # the high pass
    a0 = 1 + K / Q + K * K
    return shelf, ((1.0, -2.0, 1.0), (1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0))


def kweighted(c, fs):
    """c through the K-weighting from rest, as a streaming meter filters it, by one FFT. A second of
    zeros after c keeps the product linear: the response's slowest part, the high pass's double pole,
    decays with a time constant of 4.2 ms, so after 1 s it is below 1e-100 of its peak and the end of
    c no longer reaches its start."""
    n = len(c)
    N = n + int(fs)
    z = np.exp(-2j * np.pi * np.fft.rfftfreq(N))
    H = 1.0
    for b, a in kfilters(fs):
        H = H * (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return np.fft.irfft(np.fft.rfft(c, N) * H, N)[:n]


def biquad(x, b, a):
    """x through one biquad from rest, sample by sample: slow, the reference for kweighted."""
    y, x1, x2, y1, y2 = np.empty(len(x)), 0.0, 0.0, 0.0, 0.0
    for i, v in enumerate(x.tolist()):
        y[i] = o = b[0] * v + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        x2, x1, y2, y1 = x1, v, y1, o
    return y


def true_peak(c, over=4, blk=1 << 16, pad=4096):
    """The largest magnitude of c upsampled `over` times by FFT, in blocks with pad samples of margin."""
    n = len(c)
    z = np.concatenate((np.zeros(pad), c, np.zeros(pad)))
    best = 0.0
    for i in range(0, n, blk):
        seg = z[i:i + blk + 2 * pad]
        X = np.fft.rfft(seg)
        if len(seg) % 2 == 0:
            X[-1] *= 0.5                              # the old Nyquist bin counts once at the new length
        y = np.fft.irfft(X, over * len(seg)) * over
        best = max(best, float(np.abs(y[over * pad:over * (pad + min(blk, n - i))]).max()))
    return best


def meter(chans, fs):
    """BS.1770-4 loudness of one or two channels of floats in [-1, 1), the filters started from rest: the
    integrated loudness, the loudest 3 s, the true and sample peaks and the largest mean over one second.
    One channel counts twice, as it plays on two speakers. loudness prints it; binaural_ab.py matches with it."""
    n = len(chans[0])
    g = 2.0 if len(chans) == 1 else 1.0               # dual mono: two speakers
    sums = [np.r_[0.0, np.cumsum(kweighted(c, fs) ** 2)] for c in chans]

    def blocks(sec):                                  # K-weighted power summed over the speakers, every 100 ms
        N, H = int(round(sec * fs)), int(round(0.1 * fs))
        i = np.arange(0, n - N + 1, H)
        return g * sum((s[i + N] - s[i]) / N for s in sums)

    def lufs(p):
        return -0.691 + 10 * np.log10(np.maximum(p, 1e-20))

    def dbfs(v):
        return float(20 * np.log10(v)) if v > 0 else None

    b = blocks(0.4)
    b = b[lufs(b) > -70]                              # the absolute gate
    if len(b):
        b = b[lufs(b) > lufs(b.mean()) - 10]          # the relative gate
    st = blocks(3.0)
    off = None
    if n >= fs:
        off = max(float(np.abs(s[fs:] - s[:-fs]).max()) / fs for s in (np.r_[0.0, np.cumsum(c)] for c in chans))
    peak = max(float(np.abs(c).max()) for c in chans) if n else 0.0
    return dict(integrated=float(lufs(b.mean())) if len(b) else None,
                short_term_max=float(lufs(st.max())) if len(st) and st.max() > 0 else None,
                true_peak=dbfs(max(peak, max(true_peak(c) for c in chans))), sample_peak=dbfs(peak),
                offset=None if off is None else dbfs(off), offset_percent=None if off is None else 100 * off,
                dual_mono=len(chans) == 1)


def cmd_loudness(au, a):
    res = meter((au.L, au.R) if au.channels > 1 else (au.L,), au.fs)
    print('loudness, ITU-R BS.1770-4%s' % ('' if au.channels > 1 else ': mono counts twice, as it plays on two speakers'))
    for nm, k, unit, note in (('integrated', 'integrated', 'LUFS', 'blocks of 400 ms every 100 ms, gated at -70 LUFS'
                                                                   ' and 10 LU under their mean'),
                              ('short-term', 'short_term_max', 'LUFS', 'the loudest 3 s'),
                              ('true peak', 'true_peak', 'dBTP', '4x oversampled'),
                              ('sample peak', 'sample_peak', 'dBFS', ''),
                              ('offset', 'offset', 'dBFS', 'the largest mean over 1 s' if res['offset_percent'] is None else
                               'the largest mean over 1 s, %.3f%% of full scale' % res['offset_percent'])):
        print('  %-12s %7s %-4s  %s' % (nm, '-' if res[k] is None else '%.2f' % res[k], unit, note))
    return res


PLAY_LUFS, PLAY_CEILING = -16.0, -1.0              #  (A): play.sh plays toward -16 LUFS under -1 dBTP


def cmd_playgain(au, a):
    """The gain play.sh plays a file at ( (A)): toward PLAY_LUFS integrated, but never so high that the
    true peak passes PLAY_CEILING; 0 for a file with no block above the gates. With OUT, also a copy of the
    file at that gain, rounded to 16 bits: what play.sh plays, never a record."""
    res = meter((au.L, au.R) if au.channels > 1 else (au.L,), au.fs)
    gain, capped = 0.0, False
    if res['integrated'] is not None:
        gain = PLAY_LUFS - res['integrated']
        if res['true_peak'] is not None and gain > PLAY_CEILING - res['true_peak']:
            gain, capped = PLAY_CEILING - res['true_peak'], True
    if a.out:
        x = np.stack((au.L, au.R), axis=1) if au.channels > 1 else au.L[:, None]
        y = np.clip(np.round(x * 32768.0 * 10 ** (gain / 20)), -32768, 32767).astype('<i2')
        with wave.open(a.out, 'wb') as w:
            w.setnchannels(au.channels)
            w.setsampwidth(2)
            w.setframerate(au.fs)
            w.writeframes(y.tobytes())
    if res['integrated'] is None:
        print('gain +0.00 dB: no block above the gates, so the file plays as rendered')
    else:
        print('gain %+.2f dB%s (integrated %.2f LUFS, true peak %.2f dBTP; toward %g LUFS, ceiling %g dBTP)'
              % (gain, ', held by the ceiling' if capped else '', res['integrated'], res['true_peak'],
                 PLAY_LUFS, PLAY_CEILING))
    return dict(res, gain=gain, capped=capped)


def cmd_loudness_check(a):
    """loudness on synthetic files, against ffmpeg's ebur128 if it is on the PATH. Exit 1 on a miss."""
    ff = shutil.which('ffmpeg')
    tol = 0.01                                        # ebur128 gates on a histogram of 0.01 LU steps
    misses, worst = 0, 0.0

    def pcm(kind, fs):                                # 4 s of 16-bit samples, frames x channels
        t = np.arange(4 * fs) / fs
        sine = 0.1 * np.sin(2 * np.pi * 997 * t)      # -20 dBFS
        x = {'sine': sine, 'dc': np.full(len(t), 0.1), 'step': np.where(t >= 0.01, 0.1, 0.0),
             'burst': np.where((t >= 2) & (t < 2.1), sine, 0.0), 'gate': np.where(t < 2, sine, 0.1 * sine),
             'left': np.stack((sine, 0 * t), axis=1), 'silence': 0 * t, 'four': np.stack((0 * t,) * 3 + (sine,), axis=1)}[kind]
        return np.round(x * 32768).astype('<i2').reshape(len(t), -1)

    def write(path, x, fs):
        with wave.open(path, 'wb') as w:
            w.setnchannels(x.shape[1])
            w.setsampwidth(2)
            w.setframerate(fs)
            w.writeframes(x.tobytes())

    print('loudness-check: the integrated loudness of 4 s files (mono counts twice), against %s' %
          (subprocess.run([ff, '-version'], capture_output=True, text=True).stdout.split(' Copyright')[0]
           if ff else 'nothing: ffmpeg is not on the PATH'))
    print('  %6s  %-38s %8s %8s %6s' % ('fs', 'signal', 'here', 'ebur128', 'diff'))
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'x.wav')
        for kind, fs, what in (('sine', 44100, '997 Hz sine, -20 dBFS'), ('sine', 48000, '997 Hz sine, -20 dBFS'),
                               ('sine', 96000, '997 Hz sine, -20 dBFS'), ('dc', 44100, 'DC 0.1 from the first sample'),
                               ('dc', 48000, 'DC 0.1 from the first sample'), ('dc', 96000, 'DC 0.1 from the first sample'),
                               ('step', 48000, '10 ms of silence, then DC 0.1'),
                               ('burst', 48000, 'a 100 ms burst of the sine at 2 s'),
                               ('gate', 48000, 'the sine for 2 s, then 20 dB down'),
                               ('left', 48000, 'stereo: the sine left, silence right'), ('silence', 48000, 'silence')):
            write(path, pcm(kind, fs), fs)
            with contextlib.redirect_stdout(io.StringIO()):
                here = cmd_loudness(Audio(path), a)['integrated']
            there = diff = None
            if ff:
                p = subprocess.run([ff, '-hide_banner', '-nostats', '-i', path, '-af',
                                    'ebur128=dualmono=true:metadata=1,ametadata=mode=print', '-f', 'null', '-'],
                                   capture_output=True, text=True)
                v = re.findall(r'lavfi\.r128\.I=(\S+)', p.stderr)
                there = float(v[-1]) if p.returncode == 0 and v else None
                if there is not None and there <= -70:
                    there = None                      # ebur128's -70: no block passed the gates
                diff = None if (here is None) != (there is None) else abs(here - there) if here is not None else 0.0
                if diff is None or diff > tol:
                    misses += 1
                else:
                    worst = max(worst, diff)
            print('  %6d  %-38s %8s %8s %6s%s' % (fs, what, '-' if here is None else '%.3f' % here,
                                                 '-' if there is None else '%.3f' % there,
                                                 '' if diff is None else '%.3f' % diff,
                                                 ' MISS' if ff and (diff is None or diff > tol) else ''))
        print('  the K-weighting by FFT against the two biquads run from rest, sample by sample, on 1 s of DC 0.1 plus the sine:')
        for fs in (44100, 48000, 96000):
            x = (pcm('dc', fs) + pcm('sine', fs))[:fs, 0] / 32768.0
            y = x
            for b, a_ in kfilters(fs):
                y = biquad(y, b, a_)
            e = float(np.abs(kweighted(x, fs) - y).max() / np.abs(y).max())
            misses += e > 1e-9
            print('  %6d  largest difference %.1e of the peak%s' % (fs, e, ' MISS' if e > 1e-9 else ''))
        write(path, pcm('four', 48000), 48000)
        try:
            Audio(path)
            refused = 'accepted'
        except SystemExit as e:
            refused = str(e)
        misses += 'only mono and stereo' not in refused
        print('  a 4-channel file: %s' % refused.replace(path, 'x.wav'))
    print('loudness-check: %s' % ('%d missed' % misses if misses else 'ok' + (
        ', every reading within %.3f LU of ebur128 (tolerance %.2f)' % (worst, tol) if ff else ', ebur128 not compared')))
    return 1 if misses else 0


# ---------------------------------------------------------------------------
# Patch descriptors and search . describe renders one note of a recipe with the
# harness and measures it; search filters the descriptors of every recipe, kept in
# descriptors.tsv. The constants are the measurement constants ; README.md, Patch
# descriptors and search, says what each measure means and how it is taken.
DESC_FS = 48000                    # Hz, the render's rate
DESC_HOLD = 6.0                    # s from the note-on to the note-off (--seconds)
DESC_TAIL = 4.0                    # s rendered after the note-off, for the release
DESC_LEVEL = 10                    # level_db10 of the measured render: the carrier 1 dB below full scale
                                   # (several carriers: their amplitudes summed, render_note)
DESC_NOTE = 440000                 # mHz, when the recipe's note names no pitch ("at 50 Hz")
DESC_VEL = 32767                   # Q15, full velocity: the recipe's levels and indexes as written
ENV_HOP = 0.001                    # s between level frames
ENV_WIN = 0.002                    # s: a level frame spans the fewest whole periods of the note this long
FLOOR_DBFS = -100.0                # a level below this reads as this
PEAK_TOL = 0.5                     # dB: the attack ends where the level first comes this close to the peak
DECAY_DROP = 20.0                  # dB below the peak where the decay ends, unless the plateau is higher
RELEASE_DROP = 60.0                # dB below the peak where the release ends
SEG_DROP = 40.0                    # dB below the peak where the measured part ends, if before the note-off
SUSTAIN_SPAN = 0.1                 # s before the note-off whose median level is the sustain
SUSTAIN_MAX = 90.0                 # dB: a sustain this deep or deeper (silent by the note-off) reads as this
FLAT_NFFT, FLAT_HOP = 2048, 512    # samples: the frames the flatness is taken over
SPEC_NFFT, SPEC_HOP = 16384, 4096  # samples: the frames of the averaged spectrum
BAND = (20.0, 20000.0)             # Hz: the band every spectral measure reads
PEAK_RANGE = 50.0                  # dB: inharmonicity weighs the spectral peaks this close to the strongest
OE_FLOOR = 40.0                    # dB below the band's power: added to both sides of the odd/even ratio
SELFTEST = 'drone'                 # a soft 1:1 tone with a 4 s attack and a 0 dB sustain
DESC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'out', 'describe')
DESC_TSV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'descriptors.tsv')
DESC_COLUMNS = ('name', 'tags', 'note_hz', 'brightness_hz', 'attack_ms', 'decay_ms', 'sustain_db', 'release_ms',
                'noisiness', 'inharmonicity', 'odd_even_db', 'peak_dbfs')
DESC_FORMAT = {'brightness_hz': '%.0f', 'attack_ms': '%.0f', 'decay_ms': '%.0f', 'sustain_db': '%.1f',
               'release_ms': '%.0f', 'noisiness': '%.3f', 'inharmonicity': '%.3f', 'odd_even_db': '%.1f',
               'peak_dbfs': '%.2f'}
DESC_MEANING = {
    'note_hz': 'the note played',
    'brightness_hz': 'spectral centroid of the measured part, 20 Hz to 20 kHz',
    'attack_ms': 'note-on to the first level within 0.5 dB of the peak',
    'decay_ms': "attack's end to 20 dB below the peak, or to within 0.5 dB of the sustain if that is higher",
    'sustain_db': 'median level over the last 100 ms of the hold, dB below the peak (90: silent)',
    'release_ms': 'note-off to 60 dB below the peak (0: that low by the note-off)',
    'noisiness': 'spectral flatness, 0 for a pure tone, 1 for white noise',
    'inharmonicity': "spectral peaks' distance from the note's harmonics, 0 to 1",
    'odd_even_db': 'power of harmonics 3, 5, 7 ... over that of 2, 4, 6 ..., dB',
    'peak_dbfs': 'sample peak of the recipe at its own level',
}


def music_compose():
    """compose.py beside this file, for its recipes, its timeline rows and its harness."""
    sys.dont_write_bytecode = True
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import compose
    return compose


def recipe_metadata(cp):
    """Live tags and optional description pitches from recipes.txt .
    Neither becomes part of the raw patch or its recipe result."""
    tags, pitches, name = {n: [] for n in cp.RECIPES}, {}, None
    with open(cp.RECIPE_FILE) as fh:
        for n, line in enumerate(fh, 1):
            w = line.split()
            if not w or w[0].startswith('#'):
                continue
            if w[0] == 'recipe':
                name = w[1]
            elif w[0] == 'tags':
                for t in w[1:]:
                    if t not in tags[name]:
                        tags[name].append(t)
            elif w[0] == 'describe_note':
                if name in pitches:
                    sys.exit('analyze.py: recipes.txt:%d: a second describe_note for %s' % (n, name))
                if len(w) != 2 or not re.fullmatch(r'[0-9]+', w[1]) or not int(BAND[0] * 1000) <= int(w[1]) < DESC_FS * 500:
                    sys.exit('analyze.py: recipes.txt:%d: describe_note takes integer mHz from %d (%g Hz) to %d'
                             % (n, int(BAND[0] * 1000), BAND[0], DESC_FS * 500 - 1))
                pitches[name] = int(w[1])
    return tags, pitches


def recipe_tags(cp):
    """{recipe: [tag, ...]}, with the vocabulary owned by recipes.txt alone."""
    return recipe_metadata(cp)[0]


def default_note(text, explicit=None):
    """Explicit describe_note, then the prose pitch ("at 50 Hz", "at 1.8 kHz";
    "at 540 and 800 Hz" gives the first), in mHz; otherwise DESC_NOTE."""
    if explicit is not None:
        return explicit
    m = re.search(r'\bat (\d+(?:\.\d+)?)(?: and \d+(?:\.\d+)?)? (k?Hz)\b', text)
    if not m:
        return DESC_NOTE
    return int(round(float(m.group(1)) * (1e6 if m.group(2) == 'kHz' else 1e3)))


def render_note(cp, name, mhz, vel, hold_ns, level, wav, tl):
    """Write one note of a recipe as a timeline (tl), play it with the harness, optionally into
    wav, and return the PLAY line's fields and the line. level, if not None, replaces the
    carrier's level_db10, and a routed recipe's other carriers move by the same step, as
    finish moves them (compose.py's patch_lines, ). With several carriers the step is
    the smallest whole one at which their amplitudes, summed, stay at level's or below, so the
    sum cannot clip (provisional, ); for one carrier that is level itself."""
    car, mod, _, *extra = cp.recipe(name)
    if level is not None and extra and 'ops' in extra[0]:
        lv = [o[1] for i, o in enumerate([car, mod] + extra[0]['more']) if extra[0]['carriers'] >> i & 1]
        if len(lv) > 1:
            level = car[1] + math.ceil(round(200 * math.log10(sum(10 ** (-x / 200) for x in lv)) + level, 9))
    prows = cp.patch_headers('d', car, mod, '', *extra, level=level, base=car[1])
    rows = (['timeline draft1', 'engine ' + cp.ENGINE, 'fs %d' % DESC_FS,
             'length_ns %d' % (hold_ns + int(round(DESC_TAIL * 1e9))), 'streams d'] + prows
            + ['voice 0 d', '0 d 0 on %d %d' % (mhz, vel), '%d d 0 off' % hold_ns])
    with open(tl, 'w') as fh:
        fh.write('\n'.join(rows) + '\n')
    rc, kv, line = cp.play([tl], wav)
    if rc:
        sys.exit('analyze.py: the harness exited %d: %s' % (rc, line))
    return kv, line


def frames_power(seg, fs, nfft, hop):
    """The bin frequencies and the power of each Hann frame of nfft samples, hop apart, over
    seg with nfft - hop zeros before it and nfft - hop more after it, rounded up to a whole
    hop. Every sample then lies in nfft / hop frames, once at each step of the window, and
    weighs alike (the squared windows sum to 1.5 within 0.06 %): a strike at the segment's
    start counts as fully as its middle, and a segment shorter than a frame fills several."""
    lead = nfft - hop
    pad = np.concatenate((np.zeros(lead), seg, np.zeros(lead + (-len(seg)) % hop)))
    idx = np.arange(1 + (len(pad) - nfft) // hop)[:, None] * hop + np.arange(nfft)[None, :]
    return np.fft.rfftfreq(nfft, 1.0 / fs), np.abs(np.fft.rfft(pad[idx] * np.hanning(nfft), axis=1)) ** 2


def note_levels(x, fs, f0):
    """Levels in dBFS every ENV_HOP: each the largest |sample| over the fewest whole periods of
    f0 that last ENV_WIN, centred on its time. A carrier's sine reaches its envelope within a
    period, noise within a few ms, so this follows the envelope, and an index that moves the
    power of folded sidebands does not move it. For description notes at or above
    BAND[0], the window is at most fs / BAND[0] samples (2400 at 48 kHz)."""
    hop = int(round(ENV_HOP * fs))
    win = max(1, int(round(np.ceil(ENV_WIN * f0) * fs / f0)))
    pad = np.concatenate((np.zeros(win), np.abs(x), np.zeros(win)))
    start = np.arange(len(x) // hop + 1) * hop + win - win // 2
    view = np.lib.stride_tricks.sliding_window_view(pad, win)
    env = np.concatenate([view[start[i:i + 2048]].max(axis=1) for i in range(0, len(start), 2048)])
    return 20 * np.log10(np.maximum(env, 10 ** (FLOOR_DBFS / 20))), hop


def measure_note(x, fs, f0, n_off):
    """The descriptors of one note: x the render (floats), f0 the note in Hz, n_off the
    note-off's sample."""
    lev, hop = note_levels(x, fs, f0)
    t = np.arange(len(lev)) * (1000.0 * hop / fs)                  # ms after the note-on
    i_off = min(n_off // hop, len(lev) - 1)
    peak = float(lev[:i_off + 1].max())
    if peak < FLOOR_DBFS + 30:
        sys.exit('analyze.py: the note is silent (its level peaks at %.1f dBFS)' % peak)
    i_pk = int(np.argmax(lev[:i_off + 1] >= peak - PEAK_TOL))
    span = max(1, int(round(SUSTAIN_SPAN * fs / hop)))
    sus = float(np.median(lev[max(i_pk, i_off - span):i_off])) if i_off > i_pk else float(lev[i_off])
    after = lev[i_pk:i_off + 1]
    down = np.nonzero(after <= peak - DECAY_DROP)[0]
    if not len(down):
        down = np.nonzero(after <= sus + PEAK_TOL)[0]
    decay = t[i_pk + down[0]] - t[i_pk] if len(down) else t[i_off] - t[i_pk]
    gone = np.nonzero(lev[i_off:] <= peak - RELEASE_DROP)[0]
    release = t[i_off + gone[0]] - t[i_off] if len(gone) else None
    # the measured part: from the attack's end to the note-off, or to SEG_DROP below the peak if
    # sooner, and at least 1024 samples
    end = np.nonzero(after <= peak - SEG_DROP)[0]
    a = i_pk * hop
    seg = x[a:min(len(x), max((i_pk + end[0] if len(end) else i_off) * hop, a + 1024))]
    f, P = frames_power(seg, fs, SPEC_NFFT, SPEC_HOP)
    P = P.mean(axis=0)
    band = (f >= BAND[0]) & (f <= min(BAND[1], fs / 2))
    fb, Pb = f[band], P[band]
    total = float(Pb.sum())
    bright = float((fb * Pb).sum() / total)
    # noisiness: the flatness (geometric over arithmetic mean) of each frame's power, weighted by
    # that power, over the flatness a frame of white noise has on average, e^-gamma
    ff, Q = frames_power(seg, fs, FLAT_NFFT, FLAT_HOP)
    Q = Q[:, (ff >= BAND[0]) & (ff <= min(BAND[1], fs / 2))]
    Q = Q + np.maximum(Q.max(axis=1, keepdims=True), 1e-30) * 1e-12
    flat = np.exp(np.log(Q).mean(axis=1)) / Q.mean(axis=1)
    weight = Q.sum(axis=1)
    noisy = min(1.0, float((flat * weight).sum() / weight.sum()) / float(np.exp(-np.euler_gamma)))
    # inharmonicity: each spectral peak (its frequency refined on a parabola through the dB of
    # its bin and the two beside it) is 0 on a harmonic of f0 and 1 halfway between two, weighted
    # by its power
    L = 10 * np.log10(Pb + total * 1e-20)
    k = np.nonzero((Pb[1:-1] > Pb[:-2]) & (Pb[1:-1] >= Pb[2:]) & (Pb[1:-1] >= Pb.max() * 10 ** (-PEAK_RANGE / 10)))[0] + 1
    den = L[k - 1] - 2 * L[k] + L[k + 1]
    shift = np.where(den < 0, 0.5 * (L[k - 1] - L[k + 1]) / np.where(den < 0, den, -1.0), 0.0)
    h = (fb[k] + shift * (f[1] - f[0])) / f0
    inharm = float((Pb[k] * 2 * np.abs(h - np.round(h))).sum() / Pb[k].sum())
    # odd/even: the power within a quarter of f0 of harmonics 3, 5, 7 ... over 2, 4, 6 ...
    kmax = int(min(BAND[1], fs / 2) // f0)
    oe = None
    if kmax >= 3:
        hb = fb / f0
        kb = np.round(hb)
        near = (np.abs(hb - kb) <= 0.25) & (kb >= 2) & (kb <= kmax)
        floor = total * 10 ** (-OE_FLOOR / 10)
        oe = 10 * np.log10((Pb[near & (kb % 2 == 1)].sum() + floor) / (Pb[near & (kb % 2 == 0)].sum() + floor))
        oe = float(oe)
    return {'brightness_hz': bright, 'attack_ms': float(t[i_pk]), 'decay_ms': float(decay),
            'sustain_db': min(SUSTAIN_MAX, peak - sus), 'release_ms': None if release is None else float(release),
            'noisiness': noisy, 'inharmonicity': inharm, 'odd_even_db': oe}


def describe_one(cp, tags, name, mhz, vel, hold):
    """Render one note of a recipe twice, as written (for its peak) and with its carrier at
    DESC_LEVEL (for everything else, so a quiet recipe keeps its range above the floor), and
    measure it. The timeline and the WAV live under out/describe only while it runs."""
    hold_ns = int(round(hold * 1e9))
    os.makedirs(DESC_DIR, exist_ok=True)
    base = os.path.join(DESC_DIR, 'describe-%d' % os.getpid())
    tl, wav = base + '.tl', base + '.wav'
    try:
        kv, line = render_note(cp, name, mhz, vel, hold_ns, None, None, tl)
        kv2, line2 = render_note(cp, name, mhz, vel, hold_ns, DESC_LEVEL, wav, tl)
        au = Audio(wav)
        d = measure_note(au.mid, au.fs, mhz / 1000.0, hold_ns * au.fs // 10 ** 9)
    finally:
        for p in (tl, wav):
            with contextlib.suppress(FileNotFoundError):
                os.remove(p)
    out = {'name': name, 'tags': tags.get(name, []), 'note_hz': mhz / 1000.0, 'vel': vel, 'hold_s': hold}
    out.update(d)
    out.update(peak_dbfs=float(kv['peak_dbfs']), play=line, play_measured=line2)
    return out


def desc_cell(col, v):
    """One descriptor as the TSV and the tables write it; - for none."""
    if col == 'name':
        return v
    if col == 'tags':
        return ','.join(v) or '-'
    if v is None:
        return '-'
    if col == 'note_hz':
        return ('%.3f' % v).rstrip('0').rstrip('.')
    s = DESC_FORMAT[col] % v
    return s.lstrip('-') if float(s) == 0 else s


def print_table(header, rows, left=(0, 1)):
    """Columns padded to their widest cell: the left ones flush left, the rest flush right."""
    w = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]
    for r in [header] + rows:
        print('  '.join(c.ljust(w[i]) if i in left else c.rjust(w[i]) for i, c in enumerate(r)).rstrip())


def describe_selftest(cp, tags):
    """Two runs of SELFTEST at A4: the centroid near the note, the attack near the carrier's
    a, the sustain near its s, the release near (60 - s) / 96.3 x r, and the runs the same."""
    c = dict(cp.OP_ARGS, **cp.RECIPES[SELFTEST][0])
    runs = [describe_one(cp, tags, SELFTEST, DESC_NOTE, DESC_VEL, DESC_HOLD) for _ in range(2)]
    d, f0 = runs[0], DESC_NOTE / 1000.0
    rel = (RELEASE_DROP - c['s']) / 96.3 * c['r']
    print('selftest: %s at %g Hz, velocity %d, held %g s' % (SELFTEST, f0, DESC_VEL, DESC_HOLD))
    print('  ' + d['play'])
    checks = [
        (abs(d['brightness_hz'] - f0) <= 0.15 * f0,
         'brightness %.0f Hz is within 15 %% of the note, %g Hz' % (d['brightness_hz'], f0)),
        (abs(d['attack_ms'] - c['a']) <= 0.10 * c['a'],
         "attack %.0f ms is within 10 %% of the carrier's a, %d ms" % (d['attack_ms'], c['a'])),
        (abs(d['sustain_db'] - c['s']) <= 1.0,
         "sustain %.1f dB is within 1 dB of the carrier's s, %g dB" % (d['sustain_db'], c['s'])),
        (d['release_ms'] is not None and abs(d['release_ms'] - rel) <= 0.10 * rel,
         'release %s ms is within 10 %% of (60 - s) / 96.3 x r, %.0f ms' % (desc_cell('release_ms', d['release_ms']), rel)),
        (runs[0] == runs[1], 'two runs give the same descriptors and the same hashes (as written %s, measured %s)'
         % (d['play'].split()[1], d['play_measured'].split()[1])),
    ]
    for ok, text in checks:
        print(('PASS ' if ok else 'FAIL ') + text)
    passed = sum(ok for ok, _ in checks)
    print('selftest: %s, %d of %d' % ('PASS' if passed == len(checks) else 'FAIL', passed, len(checks)))
    return 0 if passed == len(checks) else 1


def cmd_describe(argv):
    ap = argparse.ArgumentParser(prog='analyze.py describe', description='Render one note of a recipe with the '
                                 'harness and measure it ; README.md, Patch descriptors and search.')
    ap.add_argument('recipe', nargs='?', help='a recipe of recipes.txt')
    ap.add_argument('--all', action='store_true', help='every recipe, as a TSV (or JSON with --json)')
    ap.add_argument('--selftest', action='store_true', help='check the measures on %s' % SELFTEST)
    ap.add_argument('--note', type=int, help='the note in mHz (default: describe_note, then the recipe\'s '
                    'prose pitch, else %d)' % DESC_NOTE)
    ap.add_argument('--vel', type=int, default=DESC_VEL, help='the velocity, Q15 (default %d)' % DESC_VEL)
    ap.add_argument('--seconds', type=float, default=DESC_HOLD, help='how long the key is held (default %g)' % DESC_HOLD)
    ap.add_argument('--json', nargs='?', const=True, metavar='FILE',
                    help='print JSON instead of a table or TSV, or write it to FILE')
    ap.add_argument('--out', help='--all: write the TSV to this file')
    a = ap.parse_args(argv)
    if a.recipe is None and not a.all and not a.selftest and isinstance(a.json, str):
        # Preserve the old option-first spelling: describe --json RECIPE.
        a.recipe, a.json = a.json, True
    if (a.recipe is not None) + a.all + a.selftest != 1:
        ap.error('give a RECIPE, --all or --selftest')
    if a.out and not a.all:
        ap.error('--out goes with --all')
    if a.selftest and (a.note is not None or a.vel != DESC_VEL or a.seconds != DESC_HOLD or a.json):
        ap.error('--selftest takes no other option')
    if a.note is not None and not int(BAND[0] * 1000) <= a.note < DESC_FS * 500:
        ap.error('--note is in mHz, %d (%g Hz) to %d; 55 Hz is 55000'
                 % (int(BAND[0] * 1000), BAND[0], DESC_FS * 500 - 1))
    if not 0 <= a.vel <= 32767:
        ap.error('--vel is 0 to 32767')
    if not 0.05 <= a.seconds <= 60:
        ap.error('--seconds is 0.05 to 60')
    cp = music_compose()
    if a.recipe is not None and a.recipe not in cp.RECIPES:
        ap.error('no recipe %r; compose.py --recipes lists them' % a.recipe)
    tags, pitches = recipe_metadata(cp)
    cp.build()                                      # make, as compose.py runs it: a rebuild only when stale
    try:
        if a.selftest:
            return describe_selftest(cp, tags)
        names = list(cp.RECIPES) if a.all else [a.recipe]
        res = [describe_one(cp, tags, n, a.note if a.note is not None else default_note(cp.RECIPES[n][2], pitches.get(n)),
                            a.vel, a.seconds) for n in names]
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(DESC_DIR)
    if a.out:
        with open(a.out, 'w') as fh:
            fh.write('\t'.join(DESC_COLUMNS) + '\n')
            for d in res:
                fh.write('\t'.join(desc_cell(c, d[c]) for c in DESC_COLUMNS) + '\n')
        print('wrote %s: %d recipes' % (a.out, len(res)))
    if a.json:
        text = json.dumps(jsonable(res if a.all else res[0]), indent=1, allow_nan=False) + '\n'
        if a.json is True:
            print(text, end='')
        else:
            with open(a.json, 'w') as fh:
                fh.write(text)
    elif a.all and not a.out:
        print('\t'.join(DESC_COLUMNS))
        for d in res:
            print('\t'.join(desc_cell(c, d[c]) for c in DESC_COLUMNS))
    elif not a.all:
        d = res[0]
        print('%s: %s Hz, velocity %d, held %g s; tags %s' % (d['name'], desc_cell('note_hz', d['note_hz']), d['vel'],
                                                           d['hold_s'], desc_cell('tags', d['tags'])))
        print('  as written:           ' + d['play'])
        print('  measured (1 dB down): ' + d['play_measured'])
        print_table(['descriptor', 'value', 'meaning'], [[c, desc_cell(c, d[c]), DESC_MEANING[c]]
                                                         for c in DESC_COLUMNS[3:]], left=(0, 2))
    return 0


CATALOG_COLUMNS = DESC_COLUMNS + ('source', 'record', 'patch')


def descriptor_number(text):
    """Finite descriptor value, or None for the existing unavailable marker."""
    try:
        value = float(text)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def load_descriptor_tables(paths, live=None):
    """One TSV reader for search and the agent catalog. This reads no timeline."""
    live = recipe_tags(music_compose()) if live is None else live
    result, catalog, comments = [], False, []
    for path in paths:
        with open(path, encoding='utf-8') as source:
            lines = []
            metadata = None
            for n, line in enumerate(source, 1):
                if not line.strip():
                    continue
                if line.startswith('#'):
                    comments.append(line.rstrip('\n'))
                    if line.startswith('# catalog '):
                        try:
                            metadata = json.loads(line[len('# catalog '):])
                        except (ValueError, TypeError) as error:
                            raise ValueError('%s:%d: invalid catalog provenance' % (path, n)) from error
                    continue
                lines.append((n, line.rstrip('\r\n').split('\t')))
        if not lines:
            raise ValueError('%s: empty; describe --all --out writes the descriptors' % path)
        columns = tuple(lines[0][1])
        if columns not in (DESC_COLUMNS, CATALOG_COLUMNS):
            raise ValueError('%s:%d: header is neither recipe nor catalog columns' % (path, lines[0][0]))
        is_catalog = columns == CATALOG_COLUMNS
        catalog |= is_catalog
        for n, cells in lines[1:]:
            if len(cells) != len(columns):
                raise ValueError('%s:%d: %d fields, not %d' % (path, n, len(cells), len(columns)))
            row = dict(zip(columns, cells))
            for key in DESC_COLUMNS[2:]:
                if row[key] != '-' and descriptor_number(row[key]) is None:
                    raise ValueError('%s:%d: %r is neither a finite number nor -' % (path, n, row[key]))
            if is_catalog:
                if row['source'] not in ('golden', 'piece'):
                    raise ValueError('%s:%d: invalid catalog source' % (path, n))
                row['tags'] = '-'
            else:
                row.update(source='recipes', record='', patch=row['name'])
                row['tags'] = desc_cell('tags', live.get(row['name'], ()))
            row['_table'], row['_metadata'] = str(path), metadata
            result.append(row)
    return dict(rows=result, columns=CATALOG_COLUMNS if catalog else DESC_COLUMNS,
                catalog=catalog, comments=comments)


def search_descriptors(paths, ranges=(), tags=(), live=None):
    """Filter the shared tables; keep legacy recipe ordering and live tags."""
    live = recipe_tags(music_compose()) if live is None else live
    vocabulary = sorted({tag for values in live.values() for tag in values})
    for tag in tags:
        if tag not in vocabulary:
            raise ValueError('no tag %r; the tags are %s' % (tag, ' '.join(vocabulary)))
    conditions = []
    for text in ranges:
        key, eq, span = text.partition('=')
        low, dots, high = span.partition('..')
        if not eq or not dots or key not in DESC_COLUMNS[2:]:
            raise ValueError('%r is not KEY=LO..HI with KEY one of %s' % (text, ', '.join(DESC_COLUMNS[2:])))
        low, high = (descriptor_number(low) if low else -math.inf), (descriptor_number(high) if high else math.inf)
        if low is None or high is None:
            raise ValueError('%r: LO and HI are finite numbers, or left out for an open end' % text)
        if low > high:
            raise ValueError('%r: LO is above HI' % text)
        conditions.append((key, low, high))
    table = load_descriptor_tables(paths, live)
    def keep(row):
        if tags and (row['source'] != 'recipes' or any(tag not in live.get(row['name'], ()) for tag in tags)):
            return False
        return all(descriptor_number(row[key]) is not None and low <= float(row[key]) <= high
                   for key, low, high in conditions)
    hits = [row for row in table['rows'] if keep(row)]
    if conditions:
        hits.sort(key=lambda row: (float(row[conditions[0][0]]), row['name'], row['source'], row['record'], row['patch']))
    return dict(table, hits=hits)


def cmd_search(argv):
    ap = argparse.ArgumentParser(prog='analyze.py search', description='Search recipe and catalog descriptor tables. '
                                 'KEY=LO..HI accepts open ends; tags are live recipe tags.')
    ap.add_argument('ranges', nargs='*', metavar='KEY=LO..HI')
    ap.add_argument('--tag', action='append', default=[], help='required live recipe tag (repeatable)')
    ap.add_argument('--tsv', action='append', help='descriptor table (repeatable; default descriptors.tsv)')
    a = ap.parse_args(argv)
    try:
        result = search_descriptors(a.tsv or [DESC_TSV], a.ranges, a.tag)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        ap.error(str(error))
    if result['hits']:
        print_table(result['columns'], [[row[key] for key in result['columns']] for row in result['hits']])
    print('%d of %d %s' % (len(result['hits']), len(result['rows']), 'rows' if result['catalog'] else 'recipes'))
    return 0


def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return float(o) if np.isfinite(o) else None
    if isinstance(o, np.integer):
        return o.item()
    return o


TRANSCRIBE_DEFAULTS = dict(min_hz=55.0, max_hz=1760.0, onset_dbfs=-60.0,
                           gap_ms=40.0, min_note_ms=20.0, onset_refine_ms=5.0,
                           pitch_candidates=4)
# Duration cap read from the WAV header before any sample is read. Analysis holds
# about 185 MB per minute of mono 48 kHz PCM16, so 300 s stays near 1 GB and the
# hard bound of 1200 s near 4 GB; the first stage targets short separated notes.
TRANSCRIBE_MAX_SECONDS, TRANSCRIBE_MAX_SECONDS_LIMIT = 300.0, 1200.0
NATIVE_PATCH_LIMIT = 32


def transcription_wav_header(path):
    """Walk RIFF chunks up to data, checking every declared size against the file.

    CPython's wave trusts declared sizes: a 0xFFFFFFFF data size asks for a 4 GiB
    read buffer, and an odd chunk without its pad byte raises a bare RuntimeError.
    Returns the declared data size in bytes; reads no samples.
    """
    size = os.stat(path).st_size
    with open(path, 'rb') as stream:
        head = stream.read(12)
        if len(head) < 12:
            raise ValueError('truncated WAV header: the file has only %d bytes' % len(head))
        if head[:4] != b'RIFF':
            raise ValueError('file does not start with RIFF id')
        if head[8:] != b'WAVE':
            raise ValueError('not a WAVE file')
        offset, have_fmt, odd = 12, False, None
        printable = lambda name: all(32 <= byte < 127 for byte in name)
        while True:
            stream.seek(offset)
            header = stream.read(8)
            if len(header) < 8:
                raise ValueError('truncated WAV header: fmt chunk and/or data chunk missing before byte %d of %d'
                                 % (offset, size))
            name, length = header[:4], struct.unpack('<I', header[4:])[0]
            if odd is not None:  # A missing pad byte shifts the next chunk id back by one byte.
                stream.seek(offset - 1)
                shifted = stream.read(4)
                if shifted in (b'fmt ', b'data') or not printable(name) and printable(shifted):
                    raise ValueError('malformed WAV: the odd-sized %r chunk at byte %d is missing its pad byte'
                                     % (odd[0], odd[1]))
            remaining = size - offset - 8
            label = name.decode('latin-1')
            if name == b'data':
                if not have_fmt:
                    raise ValueError('data chunk before fmt chunk')
                if length > remaining:
                    raise ValueError('truncated WAV: data chunk declares %d bytes but only %d remain in the file'
                                     % (length, remaining))
                return length
            if length > remaining:
                raise ValueError('truncated or malformed WAV: %r chunk at byte %d declares %d bytes but only %d remain '
                                 'in the file' % (label, offset, length, remaining))
            if name == b'fmt ':
                if length < 16:
                    raise ValueError('truncated WAV header: fmt chunk has %d bytes; PCM needs 16' % length)
                have_fmt = True
            odd = (label, offset) if length % 2 else None
            offset += 8 + length + length % 2


def transcription_file_digest(path):
    """SHA-256 of a file read in bounded blocks, so hashing never holds a second copy."""
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def transcription_regions(au, *, onset_dbfs, gap_ms, min_note_ms, onset_refine_ms, split_onsets=False,
                           split_flux_floor=0.15, split_drop_db=6.0, split_energy_ms=20.0, max_notes=64):
    """Audible regions on the existing 32-frame grid, not physical key holds."""
    x, fs = au.mid, au.fs
    padded = np.pad(np.abs(x), (0, (-len(x)) % DEC))
    active = padded.reshape(-1, DEC).max(axis=1) >= 10 ** (onset_dbfs / 20)
    changes = np.diff(np.r_[False, active, False].astype(np.int8))
    raw = list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))
    merged = []
    for start, end in raw:
        if merged and (start - merged[-1][1]) * DEC * 1000 < gap_ms * fs:
            merged[-1][1] = int(end)
        else:
            merged.append([int(start), int(end)])
    regions, omitted = [], []
    onsets = Onsets(au) if len(x) >= DEC * 8 and onset_refine_ms else None
    radius = onset_refine_ms * fs / 1000
    for first, last in merged:
        a, b = first * DEC, min(last * DEC, len(x))
        if (b - a) * 1000 < min_note_ms * fs:
            omitted.append(dict(start_frame=a, end_frame=b, reason='shorter than min_note_ms'))
            continue
        refined = a
        if onsets is not None:
            times = np.arange(len(onsets.O['all'])) * DEC
            indices = np.flatnonzero((times >= max(0, a - radius)) &
                                     (times <= min(b - 1, a + radius)))
            if len(indices):
                refined = int(times[indices[np.argmax(onsets.O['all'][indices])]])
        regions.append(dict(gate_start_frame=a, start_frame=refined, end_frame=b))
    if len(regions) > max_notes:
        raise ValueError('first transcription stage supports at most %d audible regions' % max_notes)
    if split_onsets:
        candidates = transcription_split_candidates(au, regions, split_flux_floor, split_drop_db, split_energy_ms)
        split = []
        for region, detection in zip(regions, candidates):
            starts = [region['start_frame']]
            accepted = [dict(method='existing audible-region start', spectral_candidates=len(detection))]
            for candidate in detection:
                frame = candidate['frame']
                if ((frame - starts[-1]) * 1000 >= max(min_note_ms, 60) * fs
                        and (region['end_frame'] - frame) * 1000 >= min_note_ms * fs):
                    starts.append(int(frame))
                    accepted.append(candidate)
            ends = starts[1:] + [region['end_frame']]
            split.extend(dict(region, start_frame=first, end_frame=last,
                              audible_region_start_frame=region['start_frame'],
                              audible_region_end_frame=region['end_frame'],
                              split_index=index, split_count=len(starts), split_detection=accepted[index])
                         for index, (first, last) in enumerate(zip(starts, ends)))
        regions = split
    if len(regions) > max_notes:
        raise ValueError('first transcription stage supports at most %d audible regions' % max_notes)
    return regions, omitted


def transcription_split_candidates(au, regions, floor, drop_db, energy_ms):
    """Existing spectral flux with local strength and actual-data release rejection."""
    nfft, hop = 2048, 256
    if not regions or len(au.mid) < nfft:
        return [[] for _ in regions]
    spectrum, _, _ = stft(au.mid, au.fs, nfft, hop)
    logged = np.log1p(1000 * spectrum)
    flux = np.r_[0, np.maximum(logged[1:] - logged[:-1], 0).sum(axis=1)]
    times = np.arange(len(flux)) * hop + nfft - hop
    context = int(au.fs / hop)  # the existing picker's nominal +/-1-second median context
    width = max(1, int(min(energy_ms, len(au.mid) * 1000 / au.fs) * au.fs / 1000))
    energy = np.r_[0, np.cumsum(au.mid * au.mid)]
    groups = []
    for region in regions:
        indices = np.flatnonzero((times >= region['start_frame']) & (times < region['end_frame']))
        if not len(indices):
            groups.append([])
            continue
        original = flux[indices]
        masked = original.copy()
        evidence = []
        for j, index in enumerate(indices):
            frame = int(times[index])
            a, b = max(0, frame - width), min(len(au.mid), frame + width)
            before, after = frame - a, b - frame
            pre = float((energy[frame] - energy[a]) / before) if before else 0.0
            post = float((energy[b] - energy[frame]) / after) if after else 0.0
            lo, hi = max(0, j - context), min(len(indices), j + context)
            maximum = float(original[lo:hi].max())
            if (not before or not after or maximum <= 0 or (pre == 0 and post == 0)
                    or original[j] < floor * maximum or post < pre * 10 ** (-drop_db / 10)):
                masked[j] = 0
            evidence.append(dict(frame=frame, flux=float(original[j]), local_max=maximum,
                                 context_start_frame=int(times[indices[lo]]),
                                 context_end_frame=int(times[indices[hi - 1]]) + hop,
                                 energy_before_frames=before, energy_after_frames=after,
                                 energy_before=pre, energy_after=post))
        picks = np.rint(flux_peaks(masked, au.fs / hop) * au.fs / hop).astype(int)
        groups.append([evidence[index] for index in picks])
    return groups


def transcription_pitches(segment, fs, min_hz, max_hz, limit):
    """Strongest chroma class, then bounded same-class spectral peak hypotheses."""
    frequencies, frames = frames_power(segment, fs, SPEC_NFFT, SPEC_HOP)
    power = frames.mean(axis=0)
    classes = chroma(frames, frequencies, min_hz, max_hz, True, False).sum(axis=0)
    if not np.any(classes):
        raise ValueError('region has no measured pitch-class energy in the selected band')
    pitch_class = int(np.argmax(classes))
    indices = np.flatnonzero((power[1:-1] > power[:-2]) &
                             (power[1:-1] >= power[2:])) + 1
    logpower = 10 * np.log10(np.maximum(power, np.finfo(float).tiny))
    candidates = []
    for k in indices:
        denominator = logpower[k - 1] - 2 * logpower[k] + logpower[k + 1]
        shift = (0.5 * (logpower[k - 1] - logpower[k + 1]) / denominator
                 if denominator < 0 else 0.0)
        hz = float(frequencies[k] + shift * (frequencies[1] - frequencies[0]))
        if min_hz <= hz <= max_hz and int(round(69 + 12 * math.log2(hz / 440))) % 12 == pitch_class:
            candidates.append(dict(frequency_millihz=int(round(hz * 1000)),
                                   power=float(power[k])))
    candidates.sort(key=lambda row: (-row['power'], row['frequency_millihz']))
    unique = []
    for candidate in candidates:
        if candidate['frequency_millihz'] not in [row['frequency_millihz'] for row in unique]:
            unique.append(candidate)
        if len(unique) == limit:
            break
    if not unique:
        raise ValueError('no spectral peak supports the strongest pitch class in the selected band')
    return dict(pitch_class=pitch_class, pitch_class_name=NAMES[pitch_class],
                chroma=classes.tolist(), candidates=unique)


def transcription_recipe(segment, fs, frequency, rows, limit=1):
    """Nearest measured recipe using finite-column spans; missing values stay explicit."""
    try:
        measured = measure_note(segment, fs, frequency / 1000, len(segment) - 1)
    except SystemExit as error:
        raise ValueError(str(error)) from error
    measured = jsonable(measured)
    spans = {}
    for key in measured:
        values = [descriptor_number(row[key]) for row in rows]
        finite = [value for value in values if value is not None]
        if finite and max(finite) > min(finite):
            spans[key] = max(finite) - min(finite)
    ranked = []
    for row in rows:
        keys = [key for key in spans if measured[key] is not None
                and descriptor_number(row[key]) is not None]
        if keys:
            distance = sum(((measured[key] - float(row[key])) / spans[key]) ** 2
                           for key in keys) / len(keys)
            ranked.append((distance, row['name'], keys))
    if not ranked:
        raise ValueError('no recipe has comparable finite descriptor measurements')
    distance, recipe, keys = min(ranked)
    shortlist = [dict(recipe=name, distance=value, columns=columns,
                      missing_columns=sorted(set(measured) - set(columns)))
                 for value, name, columns in sorted(ranked)[:limit]]
    return recipe, dict(method='descriptor finite-span mean squared distance',
                        distance=distance, measured=measured, columns=keys,
                        missing_columns=sorted(set(measured) - set(keys)), spans=spans,
                        shortlist=shortlist, available_recipes=len(ranked),
                        uncertainty='descriptors use estimated audible end as note-off')


def transcription_alignment(samples, bank, start, end, radius, window):
    """Score integer onsets on one fixed observed interval, never cropped per lag."""
    first, last = max(0, start - radius), min(end, start + window)
    observed = np.asarray(samples[first:last], dtype=np.float64)
    length = len(observed)
    onsets = np.arange(first, min(end - 1, start + radius) + 1, dtype=np.int64)
    leads = onsets - first
    if not length or not len(leads):
        raise ValueError('alignment interval is empty')
    if onsets[-1] >= last:
        raise ValueError('alignment window must extend beyond the latest candidate onset')
    nfft = 1 << (2 * length - 1).bit_length()
    spectrum = np.fft.rfft(observed, nfft)
    candidates = []
    for index, pcm in enumerate(bank.samples):
        template = pcm[:length].astype(np.float64)
        if len(template) < length:
            raise ValueError('alignment window exceeds the rendered template')
        correlation = np.fft.irfft(spectrum * np.conj(np.fft.rfft(template, nfft)), nfft)[leads]
        energy = np.cumsum(template * template)[length - leads - 1]
        if np.any(energy == 0):
            raise ValueError('alignment candidate has zero template energy')
        scores = correlation / np.sqrt(energy)
        best = int(np.argmax(scores))  # ascending onsets preserve the earliest exact tie
        lead = int(leads[best])
        fitted = np.r_[np.zeros(lead), template[:length - lead]]
        level = float(correlation[best] / energy[best])
        candidates.append(dict(index=index, start_frame=int(onsets[best]), score=float(scores[best]),
                               level=level, residual_rms=float(np.sqrt(np.mean((observed - level * fitted) ** 2))),
                               shifted_template_score=transcription_score(observed, level * fitted)))
    selected = min(candidates, key=lambda row: (-row['score'],
                   bank.identity['pitches_millihz'][row['index']], row['start_frame']))
    return dict(selected, coarse_start_frame=start, shift_frames=selected['start_frame'] - start,
                radius_frames=radius, window_start_frame=first, window_end_frame=last,
                observed_frames=length, candidate_onsets=len(onsets),
                window_scope='first bounded interval; attack ranking may miss sustained timbre',
                template_padding='zero before candidate onset; identical source interval for every candidate',
                source_padding='none; interval is clipped to available source frames',
                end_policy='audible end unchanged; hold is end minus selected onset')


def transcription_score(source, reconstructed):
    """Unaligned, unscaled PCM error; both signals are zero-padded to the longer one."""
    length = max(len(source), len(reconstructed))
    x = np.pad(np.asarray(source, dtype=np.float64), (0, length - len(source)))
    y = np.pad(np.asarray(reconstructed, dtype=np.float64), (0, length - len(reconstructed)))
    error = float(np.dot(x - y, x - y))
    energy = float(np.dot(x, x))
    same = bool(np.array_equal(x, y))
    return dict(frames=length, source_frames=len(source), reconstructed_frames=len(reconstructed),
                padding='zero-pad both signals to the longer length; no shift or gain alignment',
                source_energy=energy, squared_error=error,
                snr_db=10 * math.log10(energy / error) if energy and error else None,
                snr_reason='finite' if energy and error else ('zero padded error' if error == 0 else 'silent source'),
                exact_pcm=same and len(source) == len(reconstructed), zero_padded_equal=same,
                rms_error=float(math.sqrt(error / length)) if length else 0.0)


def transcription_integer_search(first_value, last_value, evaluate, grid_points, refine_points,
                                 refine_rounds, *, field, tie_key, label, unit, seeds=()):
    """Shared bounded integer search; evaluate measures candidates without source labels."""
    tested = {}
    def grid(first, last, points):
        return sorted({first + i * (last - first) // (points - 1) for i in range(points)})
    def collect(values):
        for value in values:
            if value not in tested:
                tested[value] = dict(evaluate(value), **{field: value})
    def best():
        valid = [row for row in tested.values() if row['valid']]
        if not valid:
            raise ValueError('all %s hypotheses have zero energy or nonpositive fit' % label)
        return min(valid, key=lambda row: (row['squared_error'], tie_key(row[field])))
    def bracket(winner):
        keys = sorted(tested)
        index = keys.index(winner[field])
        return keys[max(0, index - 1)], keys[min(len(keys) - 1, index + 1)]
    collect(grid(first_value, last_value, grid_points))
    collect(seeds)
    rounds, reason = 0, 'round cap'
    for _ in range(refine_rounds):
        first, last = bracket(best())
        if last - first <= 1:
            reason = 'one-%s bracket' % unit
            break
        candidates = [hold for hold in grid(first, last, refine_points) if hold not in tested]
        if not candidates:
            reason = 'no new candidates'
            break
        collect(candidates)
        rounds += 1
    winner = best()
    first, last = bracket(winner)
    return dict(selected=winner, candidates=[tested[key] for key in sorted(tested)],
                search_range=[first_value, last_value], bracket=[first, last],
                bracket_width=last - first, rounds_completed=rounds, stop_reason=reason,
                candidate_requests=len(tested),
                candidate_request_limit=grid_points + len(set(seeds)) + refine_rounds * (refine_points - 2))


def transcription_hold_search(max_hold, evaluate, grid_points, refine_points, refine_rounds):
    result = transcription_integer_search(1, max_hold, evaluate, grid_points, refine_points, refine_rounds,
                                          field='hold_frames', tie_key=lambda value: value,
                                          label='hold', unit='frame')
    result['search_range_frames'] = result.pop('search_range')
    result['bracket_frames'] = result.pop('bracket')
    result['bracket_width_frames'] = result.pop('bracket_width')
    result['limitation'] = 'bounded heuristic search; physical note-off and global minimum are not guaranteed'
    return result


def transcription_pitch_bounds(frequency, cents, min_hz, max_hz):
    radius = [math.ceil(frequency * 2 ** (-cents / 1200)), math.floor(frequency * 2 ** (cents / 1200))]
    band = [max(1, round(min_hz * 1000)), min(0xffffffff, round(max_hz * 1000))]
    bounds = [max(radius[0], band[0]), min(radius[1], band[1])]
    if not bounds[0] <= frequency <= bounds[1]:
        raise ValueError('selected quantized pitch is outside the configured pitch-refinement bounds')
    return dict(bounds_millihz=bounds, radius_bounds_millihz=radius, configured_bounds_millihz=band,
                clipped_low=bounds[0] != radius[0], clipped_high=bounds[1] != radius[1])


def transcription_template_fit(observed, bank):
    """Trace invalid hypotheses separately from template acquisition failures."""
    import channel
    row = dict(template_key=bank.key)
    if not np.any(bank.samples[0, :len(observed)]):
        return dict(row, valid=False, reason='zero template energy')
    fit = channel.match(observed, bank)
    if not math.isfinite(fit['level']) or fit['level'] <= 0:
        return dict(row, valid=False, reason='nonpositive fit', fit=fit)
    residual = observed - fit['level'] * bank.samples[0, :len(observed)]
    return dict(row, valid=True, fit=fit, squared_error=float(residual @ residual))


def transcription_pooled_piece(notes, *, cp, fs, length_ns, directory, max_notes):
    """Serialize final hypotheses with exact patch groups and existing idle allocation.

    Only ordinary two-operator recipes are reused. Every other recipe still
    gets fresh voices; a new Piece prevents inference metadata from leaking.
    This function writes nothing, including on a capacity refusal.
    """
    if not notes or len(notes) > max_notes:
        raise ValueError('pooling requires 1..%d final notes' % max_notes)
    groups, indices = {}, set()
    for note in notes:
        index, start, end = (note[key] for key in ('index', 'start_ns', 'estimated_off_ns'))
        if (any(type(value) is not int for value in (index, start, end)) or index in indices
                or not 0 <= start < end < length_ns or end * fs // 1000000000 <= start * fs // 1000000000):
            raise ValueError('pooled notes require unique integer indices and positive in-bounds sample holds')
        indices.add(index)
        level = note['carrier_level_db10']
        if type(level) is not int:
            raise ValueError('pooled carrier level must be an integer')
        key = note['recipe'], level
        groups.setdefault(key, []).append(note)
    if len(groups) > 32:
        raise ValueError('pooled transcription needs %d patches; native limit is 32' % len(groups))

    def fresh():
        piece = cp.Piece('transcription', 120, 4,
                         ['Approximate mono transcription; audible ends are not physical note-offs.'], fs=fs)
        piece.dir, piece.length_ns = str(directory), length_ns
        piece.stream('notes', 'estimated audible regions')
        return piece

    plans, total = [], 0
    for number, ((recipe, level), members) in enumerate(groups.items()):
        patch = 'pool%d' % number
        definition = cp.recipe(recipe)
        car, mod, _, *extras = definition
        extra = extras[0] if extras else {}
        trial = fresh()
        trial.patch(patch, *definition)
        cp.carrier_attenuation_levels(patch, trial.patches[patch], trial.routes.get(patch, (None, None)),
                                      level, 0, vector=patch in trial.vectors, purpose='transcription pooling')
        reasons = []
        if extra:
            reasons.append('recipe extras require fresh voices')
        if any(len(operator) > 15 and operator[15] for operator in (car, mod)):
            reasons.append('free_run requires fresh voices')
        if any(len(operator) > 12 and operator[12] for operator in (car, mod)):
            reasons.append('rate_scale is outside the reuse subset')
        ordered = sorted(members, key=lambda note: (note['start_ns'], note['index']))
        capacity, release, trials = len(ordered), None, 0
        if not reasons:
            for count in range(1, min(len(ordered), 64) + 1):
                trial = fresh()
                trial.patch(patch, *definition)
                ring = trial.perf_voices(count, patch, None, 'transcription pooling')
                trials += 1
                try:
                    for note in ordered:
                        ring.get(note['start_ns'], note['estimated_off_ns'])
                except ValueError:
                    continue
                capacity, release = count, ring.rel
                break
            else:
                raise ValueError('pooled patch %s needs more than 64 simultaneous or releasing voices' % patch)
        total += capacity
        if total > 64:
            raise ValueError('pooled transcription needs more than 64 total voices, including fresh fallback')
        headers = cp.patch_headers('patch', car, mod, '', *extras, level=level, base=car[1])
        identity = hashlib.sha256(('\n'.join(headers) + '\n').encode()).hexdigest()
        plans.append(dict(patch=patch, recipe=recipe, carrier_level_db10=level, patch_sha256=identity,
                          reuse_eligible=not reasons, fallback_reasons=reasons, note_count=len(ordered),
                          capacity=capacity, release_window_ns=release, allocation_trials=trials,
                          members=ordered, definition=definition))
    piece, levels, reported, mapping = fresh(), {}, [], {}
    for plan in plans:
        patch = plan['patch']
        piece.patch(patch, *plan['definition'])
        levels[patch] = plan['carrier_level_db10']
        if plan['reuse_eligible']:
            ring = piece.perf_voices(plan['capacity'], patch, None, 'transcription pooling')
            voices = list(ring.v)
        else:
            voices = piece.add_voices(patch, plan['capacity'], '')
        for number, note in enumerate(plan['members']):
            voice = (ring.get(note['start_ns'], note['estimated_off_ns'])
                     if plan['reuse_eligible'] else voices[number])
            piece.on_f('notes', note['start_ns'], voice, note['frequency_millihz'], note['velocity_q15'], '')
            piece.off('notes', note['estimated_off_ns'], voice)
            mapping[note['index']] = dict(patch=patch, voice=voice)
        reported.append(dict((key, value) for key, value in plan.items() if key not in ('members', 'definition')))
        reported[-1]['voices'] = voices
    return piece, levels, dict(groups=reported, note_mapping=mapping, note_count=len(notes), max_notes=max_notes,
        patch_count=len(plans), patch_limit=32, voice_count=total, voice_limit=64,
        patch_identity='SHA256 of existing patch_headers with neutral name, no comment and final carrier level; not audio identity')


def transcribe(wav, out, *, cp, recipes=None, split_onsets=False, recipe_candidates=1,
               align_ms=0.0, align_window_ms=100.0, split_flux_floor=0.15,
               split_drop_db=6.0, split_energy_ms=20.0, fit_hold=False,
               hold_grid_points=17, hold_refine_points=9, hold_refine_rounds=4,
               hold_tail_ms=1500.0, pitch_refine_cents=0.0, pitch_refine_rounds=4,
               pool_voices=False, max_notes=64, max_seconds=TRANSCRIBE_MAX_SECONDS, **thresholds):
    """Recover an approximate mono record using an explicitly selected child renderer.

    This host routine never builds or selects a binary. The CLI below selects
    compose.H in its own process; shared services must launch that isolated CLI.
    """
    import channel
    parameters = dict(TRANSCRIBE_DEFAULTS)
    if set(thresholds) - set(parameters):
        raise ValueError('unknown transcription threshold')
    parameters.update(thresholds)
    for key, value in parameters.items():
        if key == 'pitch_candidates':
            continue
        try:
            finite = math.isfinite(value)
        except (TypeError, OverflowError):
            finite = False
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite:
            raise ValueError(key + ' must be a finite number')
    if not (0 < parameters['min_hz'] < parameters['max_hz'] <= 22050):
        raise ValueError('frequency bounds must satisfy 0 < min_hz < max_hz <= 22050')
    if not -100 <= parameters['onset_dbfs'] <= 0:
        raise ValueError('onset_dbfs must be -100 to 0')
    if parameters['gap_ms'] < 0 or parameters['min_note_ms'] <= 0 or parameters['onset_refine_ms'] < 0:
        raise ValueError('gap/refinement must be nonnegative; minimum note duration must be positive')
    if type(parameters['pitch_candidates']) is not int or parameters['pitch_candidates'] < 1:
        raise ValueError('pitch_candidates must be a positive integer')
    if type(split_onsets) is not bool or type(fit_hold) is not bool or type(pool_voices) is not bool:
        raise ValueError('split_onsets, fit_hold and pool_voices must be booleans')
    if type(max_notes) is not int or not 1 <= max_notes <= 4096 or (max_notes > 64 and not pool_voices):
        raise ValueError('max_notes must be an integer1..4096; values above64 require pool_voices')
    if (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
            or not 0 < max_seconds <= TRANSCRIBE_MAX_SECONDS_LIMIT):
        raise ValueError('max_seconds must be above 0 and at most %g' % TRANSCRIBE_MAX_SECONDS_LIMIT)
    if type(recipe_candidates) is not int or not 1 <= recipe_candidates <= len(cp.RECIPES):
        raise ValueError('recipe_candidates must be a positive integer no larger than the recipe inventory')
    for key, value in (('align_ms', align_ms), ('align_window_ms', align_window_ms),
                       ('split_flux_floor', split_flux_floor), ('split_drop_db', split_drop_db),
                       ('split_energy_ms', split_energy_ms), ('hold_tail_ms', hold_tail_ms),
                       ('pitch_refine_cents', pitch_refine_cents)):
        try:
            finite = math.isfinite(value)
        except (TypeError, OverflowError):
            finite = False
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite:
            raise ValueError(key + ' must be finite')
    if not 0 <= align_ms <= parameters['onset_refine_ms']:
        raise ValueError('align_ms must be nonnegative and no larger than onset_refine_ms')
    if align_window_ms <= 0 or (not align_ms and align_window_ms != 100):
        raise ValueError('align_window_ms must be positive; a nondefault window requires alignment')
    if recipes is not None and recipe_candidates != 1:
        raise ValueError('recipe_candidates is for descriptor shortlists, not explicit recipes')
    if not 0 <= split_flux_floor <= 1 or split_drop_db < 0 or split_energy_ms <= 0:
        raise ValueError('split floor must be 0..1, drop nonnegative and energy duration positive')
    if not split_onsets and (split_flux_floor, split_drop_db, split_energy_ms) != (0.15, 6, 20):
        raise ValueError('nondefault split thresholds require split_onsets')
    for key, value, maximum in (('hold_grid_points', hold_grid_points, 65),
                                 ('hold_refine_points', hold_refine_points, 33)):
        if type(value) is not int or not 3 <= value <= maximum or value % 2 != 1:
            raise ValueError('%s must be an odd integer from3 to%d' % (key, maximum))
    if type(hold_refine_rounds) is not int or not 0 <= hold_refine_rounds <= 8:
        raise ValueError('hold_refine_rounds must be an integer from0 to8')
    if hold_tail_ms <= 0:
        raise ValueError('hold_tail_ms must be positive')
    if not fit_hold and (hold_grid_points, hold_refine_points, hold_refine_rounds, hold_tail_ms) != (17, 9, 4, 1500):
        raise ValueError('nondefault hold settings require fit_hold')
    if not 0 <= pitch_refine_cents <= 100 or (pitch_refine_cents > 0 and not fit_hold):
        raise ValueError('pitch_refine_cents must be0..100; a positive radius requires fit_hold')
    if type(pitch_refine_rounds) is not int or not 0 <= pitch_refine_rounds <= 8:
        raise ValueError('pitch_refine_rounds must be an integer from0 to8')
    if not pitch_refine_cents and pitch_refine_rounds != 4:
        raise ValueError('nondefault pitch_refine_rounds requires a positive pitch radius')
    names = sorted(set(recipes)) if recipes is not None else None
    if names is not None and (not names or any(name not in cp.RECIPES for name in names)):
        raise ValueError('recipes must name at least one existing recipe')
    source, destination, binary = Path(wav), Path(out), Path(cp.H)
    if not binary.is_file():
        raise ValueError('an explicit already-built harness is required')
    binary_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    implementation_paths = (Path(__file__), Path(channel.__file__), Path(cp.__file__),
                            Path(music_compose().RECIPE_FILE))
    implementation = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in implementation_paths}
    descriptor_digest = hashlib.sha256(Path(DESC_TSV).read_bytes()).hexdigest() if names is None else None
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('transcription output must be a new or empty directory')
    if source.resolve() == destination.resolve() or destination.resolve() in source.resolve().parents:
        raise ValueError('transcription output must not contain or overwrite its source')
    data_bytes = transcription_wav_header(source)
    try:
        with wave.open(str(source), 'rb') as reader:
            if (reader.getnchannels() != 1 or reader.getsampwidth() != 2 or reader.getcomptype() != 'NONE'
                    or reader.getframerate() not in (44100, 48000)):
                raise ValueError('first transcription stage requires mono PCM16 at 44100 or 48000 Hz')
            seconds = data_bytes / 2 / reader.getframerate()
    except (EOFError, RuntimeError) as error:  # Raised only by wave's header parser here.
        raise ValueError('malformed WAV header (%s)' % (str(error) or type(error).__name__)) from error
    if seconds > max_seconds:
        raise ValueError('transcription source is %.1f s, over the %g s cap; pass --max-seconds up to %g to raise it'
                         % (seconds, max_seconds, TRANSCRIBE_MAX_SECONDS_LIMIT))
    source_digest = transcription_file_digest(source)
    fs, samples = channel.read_wav(source)
    if len(samples) < DEC:
        raise ValueError('transcription source is shorter than one 32-frame envelope bin')
    if parameters['max_hz'] >= fs / 2:
        raise ValueError('max_hz must be below the source Nyquist frequency')
    au = Audio(str(source))
    regions, omitted = transcription_regions(au, split_onsets=split_onsets, split_flux_floor=split_flux_floor,
        split_drop_db=split_drop_db, split_energy_ms=split_energy_ms, max_notes=max_notes, **{key: parameters[key] for key in
        ('onset_dbfs', 'gap_ms', 'min_note_ms', 'onset_refine_ms')})
    if not regions:
        raise ValueError('no audible region meets the selected thresholds')
    if not pool_voices and len(regions) > NATIVE_PATCH_LIMIT:
        raise ValueError('unpooled transcription writes one patch per note; %d audible regions exceed the native '
                         '%d-patch limit; add --pool-voices to reuse patches, or transcribe a shorter excerpt'
                         % (len(regions), NATIVE_PATCH_LIMIT))
    config_run = subprocess.run([str(binary), '--retrig', '--fs', str(fs)], capture_output=True, text=True)
    configurations = [line for line in config_run.stdout.splitlines() if line.startswith('CONFIG ')]
    if config_run.returncode or len(configurations) != 1:
        raise ValueError('native CONFIG acquisition failed')
    config = channel._configuration(configurations[0], fs, cp.ENGINE)
    if hashlib.sha256(binary.read_bytes()).hexdigest() != binary_digest:
        raise ValueError('native executable changed during CONFIG acquisition')
    descriptor_rows = None if names is not None else [row for row in load_descriptor_tables([DESC_TSV])['rows']
                                                    if row['name'] in cp.RECIPES]
    destination.mkdir(parents=True, exist_ok=True)
    to_ns = lambda frame: (int(frame) * 1000000000 + fs - 1) // fs
    piece = cp.Piece('transcription', 120, 4,
                     ['Approximate mono transcription; audible ends are not physical note-offs.'], fs=fs)
    piece.dir = str(destination)
    piece.length_ns = to_ns(len(samples) + 3 * fs // 2)
    piece.stream('notes', 'estimated audible regions')
    notes, levels, template_renders = [], {}, 0
    with tempfile.TemporaryDirectory(prefix='transcription-') as temporary:
        for index, region in enumerate(regions):
            first, last = region['start_frame'], region['end_frame']
            segment = au.mid[first:last]
            pitch = transcription_pitches(segment, fs, parameters['min_hz'], parameters['max_hz'],
                                           parameters['pitch_candidates'])
            hypotheses = pitch['candidates']
            descriptor = None
            if names is None:
                recipe, descriptor = transcription_recipe(segment, fs, hypotheses[0]['frequency_millihz'],
                                                           descriptor_rows, recipe_candidates)
                choices = [row['recipe'] for row in descriptor['shortlist']]
                frequencies = (hypotheses[0]['frequency_millihz'],) if recipe_candidates == 1 else tuple(
                    sorted(row['frequency_millihz'] for row in hypotheses))
            else:
                choices = names
                frequencies = tuple(sorted(row['frequency_millihz'] for row in hypotheses))
            start_ns, end_ns = to_ns(first), to_ns(last)
            radius_frames = int(min(align_ms, len(samples) * 1000 / fs) * fs / 1000)
            window_frames = max(1, int(min(align_window_ms, (last - first) * 1000 / fs) * fs / 1000))
            ranked = []
            for recipe in choices:
                try:
                    bank = channel.templates((), 25, config_line=config, fs=fs, recipe_name=recipe,
                                             pitches_millihz=frequencies, offset_ns=start_ns,
                                             hold_ns=end_ns - start_ns, cache_dir=temporary)
                    fit = (transcription_alignment(samples, bank, first, last, radius_frames, window_frames) if align_ms
                           else channel.match(samples[first:last], bank))
                except (ValueError, OSError, SystemExit) as error:
                    raise ValueError('region %d recipe %s: %s' % (index, recipe, error)) from error
                if bank.identity['binary_sha256'] != binary_digest:
                    raise ValueError('native executable changed during template work')
                template_renders += len(frequencies)
                ranked.append((fit, recipe, frequencies[fit['index']], bank.key))
            fit, recipe, frequency, key = min(ranked, key=lambda row: (-row[0]['score'], row[1], row[2],
                                                                      row[0].get('start_frame', first)))
            alignment = None
            if align_ms:
                alignment = fit
                first = fit['start_frame']
                start_ns = to_ns(first)
                try:
                    bank = channel.templates((), 25, config_line=config, fs=fs, recipe_name=recipe,
                                             pitches_millihz=(frequency,), offset_ns=start_ns,
                                             hold_ns=end_ns - start_ns, cache_dir=temporary)
                    fit = channel.match(samples[first:last], bank)
                except (ValueError, OSError, SystemExit) as error:
                    raise ValueError('region %d selected recipe %s: %s' % (index, recipe, error)) from error
                template_renders += 1
                key = bank.key
            hold_fit = None
            if fit_hold:
                next_start = regions[index + 1]['start_frame'] if index + 1 < len(regions) else len(samples)
                tail_frames = int(min(hold_tail_ms, len(samples) * 1000 / fs) * fs / 1000)
                observed_end = min(next_start, len(samples), last + tail_frames)
                observed = samples[first:observed_end].astype(np.float64)
                def hold_template(held_frames, carrier_level=None, candidate_frequency=None):
                    nonlocal template_renders
                    hz_milli = frequency if candidate_frequency is None else candidate_frequency
                    try:
                        candidate = channel.templates((), 25, config_line=config, fs=fs, recipe_name=recipe,
                            pitches_millihz=(hz_milli,), offset_ns=start_ns,
                            hold_ns=to_ns(first + held_frames) - start_ns,
                            minimum_frames=len(observed), carrier_level_db10=carrier_level, cache_dir=temporary)
                        if candidate.identity['binary_sha256'] != binary_digest:
                            raise ValueError('native executable changed during hold search')
                    except (ValueError, OSError, SystemExit) as error:
                        raise ValueError('region %d recipe %s hold %d frames pitch %d mHz: %s' %
                                         (index, recipe, held_frames, hz_milli, error)) from error
                    template_renders += 1
                    return candidate
                def evaluate_hold(held_frames):
                    candidate = hold_template(held_frames)
                    return dict(transcription_template_fit(observed, candidate),
                                hold_ns=to_ns(first + held_frames) - start_ns)
                try:
                    hold_fit = transcription_hold_search(last - first, evaluate_hold, hold_grid_points,
                                                          hold_refine_points, hold_refine_rounds)
                except ValueError as error:
                    raise ValueError('region %d recipe %s hold search: %s' % (index, recipe, error)) from error
                chosen = hold_fit['selected']
                fit, key = chosen['fit'], chosen['template_key']
                end_ns = to_ns(first + chosen['hold_frames'])
                hold_fit.update(window_start_frame=first, window_end_frame=observed_end,
                    observed_frames=len(observed), audible_end_frame=last, source_padding='none',
                    bounded_tail_frames=tail_frames,
                    clipped_at_source_end=observed_end == len(samples),
                    clipped_at_next_region=index + 1 < len(regions) and observed_end == next_start,
                    next_coarse_onset_frame=next_start if index + 1 < len(regions) else None,
                    ambiguity='inaudible releases and source truncation can conceal note-off; '
                              'other releases or an early next attack can contaminate this interval')
            if not math.isfinite(fit['level']) or fit['level'] <= 0:
                raise ValueError('region %d has no positive template level fit' % index)
            patch = 'note%d' % index
            piece.patch(patch, *cp.recipe(recipe))
            delta = int(round(-200 * math.log10(fit['level'])))
            level = piece.patches[patch][0][1] + delta
            # Reuse the writer's carrier bounds/no-clamp proof, including routes.
            cp.carrier_attenuation_levels(patch, piece.patches[patch],
                piece.routes.get(patch, (None, None)), level, 0,
                vector=patch in piece.vectors, purpose='transcription')
            pitch_fit = None
            if hold_fit is not None:
                quantized = hold_template(hold_fit['selected']['hold_frames'], level)
                hold_fit['quantized_level'] = dict(carrier_level_db10=level, template_key=quantized.key,
                    template_render_requests=1,
                    score=transcription_score(observed, quantized.samples[0, :len(observed)]))
                if pitch_refine_cents:
                    initial_frequency = frequency
                    bounds = transcription_pitch_bounds(frequency, pitch_refine_cents,
                                                         parameters['min_hz'], parameters['max_hz'])
                    def evaluate_pitch(candidate_frequency):
                        candidate = hold_template(hold_fit['selected']['hold_frames'],
                                                  candidate_frequency=candidate_frequency)
                        return transcription_template_fit(observed, candidate)
                    try:
                        pitch_fit = transcription_integer_search(*bounds['bounds_millihz'], evaluate_pitch,
                            17, 9, pitch_refine_rounds, field='frequency_millihz',
                            tie_key=lambda value: (abs(value - initial_frequency), value),
                            label='pitch', unit='mHz', seeds=(initial_frequency,))
                    except ValueError as error:
                        raise ValueError('region %d recipe %s pitch refinement: %s' % (index, recipe, error)) from error
                    selected = pitch_fit['selected']
                    refined_delta = int(round(-200 * math.log10(selected['fit']['level'])))
                    refined_level = piece.patches[patch][0][1] + refined_delta
                    cp.carrier_attenuation_levels(patch, piece.patches[patch],
                        piece.routes.get(patch, (None, None)), refined_level, 0,
                        vector=patch in piece.vectors, purpose='pitch refinement')
                    refined = hold_template(hold_fit['selected']['hold_frames'], refined_level,
                                             selected['frequency_millihz'])
                    refined_score = transcription_score(observed, refined.samples[0, :len(observed)])
                    baseline = hold_fit['quantized_level']
                    adopted = refined_score['squared_error'] < baseline['score']['squared_error']
                    if adopted:
                        frequency, fit, key = selected['frequency_millihz'], selected['fit'], selected['template_key']
                        level, delta = refined_level, refined_delta
                    pitch_fit.update(bounds, initial_frequency_millihz=initial_frequency,
                        final_frequency_millihz=frequency, adopted=adopted,
                        decision='strictly lower actual quantized error' if adopted else 'retain baseline: no strict improvement',
                        window_start_frame=first, window_end_frame=observed_end, observed_frames=len(observed),
                        source_padding='none', hold_frames=hold_fit['selected']['hold_frames'],
                        grid_points=17, refine_points=9,
                        baseline_quantized=dict(baseline, frequency_millihz=initial_frequency),
                        candidate_quantized=dict(carrier_level_db10=refined_level, template_key=refined.key,
                            frequency_millihz=selected['frequency_millihz'], template_render_requests=1, score=refined_score),
                        limitation='single pitch pass at fixed hold; local error improvement need not improve the whole record')
            if not pool_voices:
                voice = piece.add_voices(patch, 1, '')[0]
                levels[patch] = level
                piece.on_f('notes', start_ns, voice, frequency, 32767, '')
                piece.off('notes', end_ns, voice)
            notes.append(dict(region, start_frame=first, index=index, start_ns=start_ns, estimated_off_ns=end_ns,
                              recipe=recipe, frequency_millihz=frequency, velocity_q15=32767,
                              carrier_level_db10=level, carrier_delta_db10=delta,
                              pitch=pitch, descriptor_match=descriptor, template_match=fit, alignment=alignment,
                              template_key=key, hypotheses_tested=len(choices) * len(frequencies),
                              hold_fit=hold_fit, pitch_fit=pitch_fit))
    pooling = None
    if pool_voices:
        piece, levels, pooling = transcription_pooled_piece(notes, cp=cp, fs=fs, length_ns=piece.length_ns,
                                                            directory=destination, max_notes=max_notes)
    piece.write(levels_db10=levels)
    canonical, rendered, replayed = (destination / name for name in ('canonical.tl', 'rerender.wav', 'replay.wav'))
    rc, values, line = cp.play(piece.files(), str(rendered), str(canonical))
    if rc or values.get('block_indep') != 'yes' or values.get('clipped') != '0':
        raise ValueError('transcription render failed: ' + line)
    replay_rc, replay_values, replay_line = cp.play([str(canonical)], str(replayed))
    if (replay_rc or replay_values.get('match') != 'yes' or replay_values.get('block_indep') != 'yes'
            or replay_values.get('clipped') != '0' or replay_values.get('hash') != values.get('hash')):
        raise ValueError('transcription canonical replay failed: ' + replay_line)
    render_fs, reconstructed = channel.read_wav(rendered)
    replay_fs, replay_samples = channel.read_wav(replayed)
    if render_fs != fs or replay_fs != fs or not np.array_equal(reconstructed, replay_samples):
        raise ValueError('canonical replay PCM differs from reconstruction')
    for note in notes:
        start, end = note['start_frame'], note['end_frame']
        note['score'] = transcription_score(samples[start:end], reconstructed[start:end])
    if transcription_file_digest(source) != source_digest:
        raise ValueError('source WAV changed during transcription')
    if hashlib.sha256(binary.read_bytes()).hexdigest() != binary_digest:
        raise ValueError('native executable changed during transcription')
    if any(hashlib.sha256(path.read_bytes()).hexdigest() != implementation[path.name] for path in implementation_paths):
        raise ValueError('implementation source changed during transcription')
    if names is None and hashlib.sha256(Path(DESC_TSV).read_bytes()).hexdigest() != descriptor_digest:
        raise ValueError('descriptor table changed during transcription')
    artifact_names = [Path(path).name for path in piece.files()] + ['canonical.tl', 'rerender.wav', 'replay.wav']
    artifacts = {name: dict(sha256=hashlib.sha256((destination / name).read_bytes()).hexdigest(),
                            bytes=(destination / name).stat().st_size) for name in artifact_names}
    report = dict(version=1, mode='approximate_mono', parameters=dict(parameters, split_onsets=split_onsets,
                  recipe_candidates=recipe_candidates, align_ms=align_ms, align_window_ms=align_window_ms,
                  split_flux_floor=split_flux_floor, split_drop_db=split_drop_db, split_energy_ms=split_energy_ms,
                  fit_hold=fit_hold, hold_grid_points=hold_grid_points, hold_refine_points=hold_refine_points,
                  hold_refine_rounds=hold_refine_rounds, hold_tail_ms=hold_tail_ms,
                  pitch_refine_cents=pitch_refine_cents, pitch_refine_rounds=pitch_refine_rounds,
                  pool_voices=pool_voices, max_notes=max_notes),
                  split_method=dict(nfft=2048, hop=256, timestamp='i*256+2048-256', median_ratio=1.5,
                                    minimum_spacing_ms=60, local_context_half_frames=int(fs / 256) * 256,
                                    source_padding='none', short_input='fewer than2048 frames keeps unsplit regions',
                                    limitation='local loud attacks can hide quieter attacks in the same context')
                  if split_onsets else None,
                  source=dict(sha256=source_digest, fs=fs, channels=1, frames=len(samples)),
                  implementation=implementation,
                  native=dict(sha256=binary_digest, config=config,
                              config_invocations=1, template_render_requests=template_renders,
                              record_invocations=2),
                  recipes=names, notes=notes, omitted_regions=omitted, artifacts=artifacts, pooling=pooling,
                  descriptor_table_sha256=descriptor_digest,
                  render=dict(exit=rc, play=values, line=line),
                  replay=dict(exit=replay_rc, play=replay_values, line=replay_line, exact_pcm=True),
                  score=transcription_score(samples, reconstructed),
                  limitations=['octave ambiguity', 'chords and overlapping sources', 'unsplit repeated notes',
                               'effects are not recovered', 'velocity versus carrier level is unresolved',
                               'audible end is not physical note-off', 'spatial and tracker stages are absent',
                               'full regression music acceptance has not been established'])
    (destination / 'report.json').write_text(json.dumps(jsonable(report), indent=2, allow_nan=False) + '\n')
    return report


def transcribe_parser(*, allow_abbrev=True):
    parser = argparse.ArgumentParser(prog='analyze.py transcribe',
                                     description='Approximate separated mono notes; writes TL, WAV and scored report.',
                                     allow_abbrev=allow_abbrev)
    parser.add_argument('wav')
    parser.add_argument('--out', required=True)
    parser.add_argument('--harness', help='explicit already-built renderer; never runs make')
    parser.add_argument('--recipes', nargs='+', help='candidate recipes; omitted uses descriptor nearest recipe')
    parser.add_argument('--split-onsets', action='store_true', help='split audible regions using the existing onset picker')
    parser.add_argument('--split-flux-floor', type=float, default=0.15, help='relative local spectral flux floor; default0.15')
    parser.add_argument('--split-drop-db', type=float, default=6, help='maximum energy fall for a split; default6dB')
    parser.add_argument('--split-energy-ms', type=float, default=20, help='actual-data energy windows; default20ms')
    parser.add_argument('--recipe-candidates', type=int, default=1, help='descriptor shortlist size; default 1')
    parser.add_argument('--align-ms', type=float, default=0, help='integer-sample template onset search radius; default 0')
    parser.add_argument('--align-window-ms', type=float, default=100, help='bounded attack fit window; default 100')
    parser.add_argument('--fit-hold', action='store_true', help='fit bounded note durations from observed PCM')
    parser.add_argument('--hold-grid-points', type=int, default=17, help='odd coarse search size3..65; default17')
    parser.add_argument('--hold-refine-points', type=int, default=9, help='odd refinement size3..33; default9')
    parser.add_argument('--hold-refine-rounds', type=int, default=4, help='refinement round cap0..8; default4')
    parser.add_argument('--hold-tail-ms', type=float, default=1500, help='additional actual-source fit context; default1500')
    parser.add_argument('--pitch-refine-cents', type=float, default=0, help='pitch radius0..100 cents; positive requires fit-hold')
    parser.add_argument('--pitch-refine-rounds', type=int, default=4, help='pitch refinement round cap0..8; default4')
    parser.add_argument('--pool-voices', action='store_true', help='deduplicate exact patches and reuse eligible idle voices')
    parser.add_argument('--max-notes', type=int, default=64, help='accepted region cap1..4096; above64 requires pool-voices')
    parser.add_argument('--max-seconds', type=float, default=TRANSCRIBE_MAX_SECONDS,
                        help='source duration cap read from the WAV header; default %g, at most %g'
                             % (TRANSCRIBE_MAX_SECONDS, TRANSCRIBE_MAX_SECONDS_LIMIT))
    for key, default in TRANSCRIBE_DEFAULTS.items():
        parser.add_argument('--' + key.replace('_', '-'), type=int if key == 'pitch_candidates' else float,
                            default=default)
    return parser


def cmd_transcribe(argv):
    parser = transcribe_parser()
    args = parser.parse_args(argv)
    cp = music_compose()
    if args.harness:
        cp.H = str(Path(args.harness).resolve())  # This command is an isolated analysis child.
    try:
        report = transcribe(args.wav, args.out, cp=cp, recipes=args.recipes, split_onsets=args.split_onsets,
                            recipe_candidates=args.recipe_candidates, align_ms=args.align_ms,
                            align_window_ms=args.align_window_ms, split_flux_floor=args.split_flux_floor,
                            split_drop_db=args.split_drop_db, split_energy_ms=args.split_energy_ms,
                            fit_hold=args.fit_hold, hold_grid_points=args.hold_grid_points,
                            hold_refine_points=args.hold_refine_points, hold_refine_rounds=args.hold_refine_rounds,
                            hold_tail_ms=args.hold_tail_ms,
                            pitch_refine_cents=args.pitch_refine_cents, pitch_refine_rounds=args.pitch_refine_rounds,
                            pool_voices=args.pool_voices, max_notes=args.max_notes, max_seconds=args.max_seconds,
                            **{key: getattr(args, key) for key in TRANSCRIBE_DEFAULTS})
    except (OSError, ValueError, wave.Error, EOFError) as error:
        parser.error(str(error) or 'malformed or truncated WAV input (%s)' % type(error).__name__)
    except MemoryError:
        parser.exit(5, 'analyze.py transcribe: out of memory; use a shorter source or a lower --max-seconds\n')
    print(json.dumps(jsonable(report), indent=2, allow_nan=False))
    return 0


def main():
    if sys.argv[1:2] in (['describe'], ['search'], ['transcribe']):
        sys.exit({'describe': cmd_describe, 'search': cmd_search,
                  'transcribe': cmd_transcribe}[sys.argv[1]](sys.argv[2:]))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=('overview', 'grid', 'harmony', 'spectro', 'steps', 'loudness', 'loudness-check',
                                        'playgain'))
    ap.add_argument('wav', nargs='?')
    ap.add_argument('out', nargs='?', help='spectro: the PNG to write; playgain: the copy at the gain')
    ap.add_argument('--start', type=float, default=0.0, help='segment start, seconds')
    ap.add_argument('--length', type=float, default=None, help='segment length, seconds')
    ap.add_argument('--json', help='also write the measurements to this file')
    ap.add_argument('--window', type=float, default=4.0, help='overview: window, seconds')
    ap.add_argument('--bpm', help='a fixed tempo (126) or a search range (80-180)')
    ap.add_argument('--first-bar', type=float, default=None, help='the start of a bar, seconds in the file')
    ap.add_argument('--beats', type=int, default=4, help='beats per bar')
    ap.add_argument('--sub', type=int, default=4, help='steps per beat: the spectro grid, the grid step patterns, the bass steps')
    ap.add_argument('--block', type=int, default=8, help='grid, harmony: bars per block')
    ap.add_argument('--sections', help='grid, harmony: blocks as bar ranges, 1-8,9-24')
    ap.add_argument('--bars', default='1-4', help='spectro: the bars to draw')
    ap.add_argument('--note', default='C', choices=NAMES, help='spectro: the note to mark in the low panel')
    ap.add_argument('--t0', type=float, default=None, help='steps: the first step, seconds in the file (the segment start)')
    ap.add_argument('--step', type=float, default=None, help='steps: step length, seconds')
    ap.add_argument('--n', type=int, default=8, help='steps: number of steps')
    a = ap.parse_args()
    if a.command == 'loudness-check':
        sys.exit(cmd_loudness_check(a))
    if not a.wav:
        ap.error('%s needs a WAV' % a.command)
    if a.command == 'spectro' and not a.out:
        ap.error('spectro needs OUT.png')
    if a.command == 'steps' and (a.step is None or a.step <= 0 or a.n < 1):
        ap.error('steps needs --step above 0 and --n of 1 or more')
    if a.length is not None and a.length <= 0:
        ap.error('--length must be above 0')
    if a.command == 'playgain' and (a.start or a.length is not None):
        ap.error('playgain meters the whole file, as play.sh plays it')
    if a.command == 'playgain' and a.out and os.path.abspath(a.out) == os.path.abspath(a.wav):
        ap.error('playgain writes its copy beside the file, never over it')
    au = Audio(a.wav, a.start, a.length)
    if a.command != 'playgain':                       # play.sh prints playgain's one line as it is
        print('%s: %d Hz, %d channel%s, %s to %s' % (a.wav.split('/')[-1], au.fs, au.channels,
                                                   '' if au.channels == 1 else 's', au.clock(0), au.clock(au.dur)))
    res = {'overview': cmd_overview, 'grid': cmd_grid, 'harmony': cmd_harmony, 'spectro': cmd_spectro,
           'steps': cmd_steps, 'loudness': cmd_loudness, 'playgain': cmd_playgain}[a.command](au, a)
    if a.json:
        with open(a.json, 'w') as fh:
            json.dump(jsonable(dict(command=a.command, start=au.t0, length=au.dur, result=res)), fh, indent=1, allow_nan=False)
        print('wrote', a.json)


if __name__ == '__main__':
    main()
