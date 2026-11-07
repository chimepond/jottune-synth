#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""The measurements behind README.md's Patch descriptors and search .

    python3 -B tools/music/checks/describe_checks.py times [--tsv FILE]
    python3 -B tools/music/checks/describe_checks.py groups [--tsv FILE]
    python3 -B tools/music/checks/describe_checks.py windows
    python3 -B tools/music/checks/describe_checks.py bursts
    python3 -B tools/music/checks/describe_checks.py noise
    python3 -B tools/music/checks/describe_checks.py start
    python3 -B tools/music/checks/describe_checks.py peaks [RECIPE]
    python3 -B tools/music/checks/describe_checks.py parts
    python3 -B tools/music/checks/describe_checks.py refusals
    python3 -B tools/music/checks/describe_checks.py json
    python3 -B tools/music/checks/describe_checks.py tempo
    python3 -B tools/music/checks/describe_checks.py all

These checks measure analyze.py describe and search, with
thirteen scripts kept in the lane's session scratch; they are gathered here
as one command each (checks/README.md maps the old names). Each command
prints what it measured beside the number it backs, as README.md beside
this directory prints it under Patch descriptors and search (the quotes
below are README.md's words, its minus signs written -); refusals and
windows also pass or fail. The script finds music from its own place,
imports analyze.py and compose.py from there and needs numpy. times and
groups read descriptors.tsv (--tsv: another file describe --all wrote) and
recipes.txt, and render nothing. noise, start, peaks and parts render notes
as describe renders its measured note (the carriers 1 dB below full scale,
summed, at full velocity, the recipe's default note, held 6 s): they run
compose.build first, which makes build/s12e10p1v1/harness at the
12/10 build (make -s SINE_BITS=12 EXP_BITS=10 PHASE64=1 ENV64=1)
when a source is newer, and keep each note's timeline and WAV in
out/checks only while it is measured. The times below are one Mac's,
with the harness built.

times    Attack, decay, sustain and release of each recipe in the TSV beside
         the engine's formulas (README, How each is measured): the attack
         0.944 a; the decay 20 d / s + 0.056 a when s is above 20,
         (s - 0.5) d / s + 0.056 a below, 0 at s = 0; the release
         (60 - s) r / 96.3, s the carrier's level at the note-off,
         min(s, s (6000 - a) / d); then the residuals by group, from 250 Hz
         up and below. Backs "drone's 4000 ms reads 3777, glide's 250 reads
         236, flute's 45 reads 42"; "epiano, a = 2, d = 2400 and s = 30:
         1600, reads 1602", flute "419, reads 421", night-pad "2251, reads
         2251"; ep-body "reads 37.2 after 6 s"; epiano "109, reads 110",
         glide "1558, reads 1556", ep-body "105, reads 106"; "the 14 decays
         toward a sustain (0 < s < 120) by -1.2 to +2.8 ms (at most 1.8 %:
         brass, 152 by the formula, reads 155), the 22 percussive decays
         (s = 120) by -1.4 to +2.9 ms, the attacks by -1.0 to +1.2 ms and
         the releases by -5.3 to +2.0 ms (drone, 3115, reads 3110); attacks
         of 1 to 2 ms read 0 to 2"; the kick "decays in 53 ms by the formula
         and reads 66; snare-body ... in 18 ms and reads 23". A recipe of
         several carriers  sums their envelopes, so it is named and
         left out. About 0.3 s.
groups   Each recipe's inharmonicity, odd/even, noisiness and brightness
         from the TSV beside all ratios and feedback, whole-number ratios
         apart, then the groups README names. Backs "Whole-number ratios
         read at most 0.001, the kick aside; the 1:1.4, 1:2.76 and 1:3.17
         bells 0.318 to 0.585; noise 0.273 to 0.472"; timpani "reads 0.148";
         the kick "reads 0.107"; "tooth's 1:3.5 reads 0.217"; the
         odd-harmonic recipes "+21 to +35", the soft 1:1 tones "-11 to -17",
         "brass -7.4, plucked-bass -7.8, motor-bass -5.8, saw -4.6,
         harpsichord -3.2"; "the 1:2.76 bell reads -26.3"; noisiness
         "noise-tail's ... (noisiness 0.101), ... clap's (0.160), ...
         snare-wires' (0.294), ... tock's (0.277) and ... tick's (0.658)",
         "pin-click, hat, open-hat and crash (0.815 to 0.917)"; "marimba
         reads 699 Hz, wood-drum 998 and celesta 1483"; a "-" odd/even for
         hat and open-hat. About 0.3 s.
windows  The weight each sample of a part gets in the averaged spectra, the
         sum of the squared Hann windows of the frames holding it under
         frames_power's padding (a frame less a hop of zeros before, as many
         after, rounded up to a whole hop), for 16384/4096 and 2048/512 and
         parts of 1 sample to 6 s; each frame count is checked against
         analyze.frames_power (exit 1 if one differs). Backs "the squared
         windows sum to 1.5 within 0.06 %", "a part shorter than a frame
         fills 4 to 7 frames" and "A part shorter than a frame (pin-click
         and tick, 1024 samples; tock, 1248; snare-body, 2016) lies in 5 to
         7 frames". About 0.3 s.
bursts   Noisiness, brightness and inharmonicity that measure_note reads
         from 1024, 2048, 4096 and 24000 samples of a 440 Hz sine, a 5 kHz
         sine and white noise (seed 1) at the start of 1 s of silence.
         Backs "1024 samples of white noise read 0.98, and 1024 of a 440 Hz
         sine 0.002". About 0.4 s.
noise    For each recipe tagged noise: where 90 % of its measured part's
         power lies (5 % to 95 % of the cumulative power) as a share of
         20 Hz to 20 kHz, the noisiness over that band alone, the share of
         power in lines 10 dB above the median of the 33 bins around them
         and in a frame's 10 strongest bins, both averaged weighted by power
         over the 2048-sample frames noisiness reads; first the 10 strongest
         bins' share in 10 s of white noise (seed 7). Backs "Every `noise`
         recipe spreads 90 % of its power over 81 to 92 % of 20 Hz to
         20 kHz, but the 10 strongest of a frame's 853 bins hold ... 64 % of
         noise-tail's power ..., 54 % of clap's ..., 52 % of snare-wires'
         ..., 38 % of tock's ... and 24 % of tick's ..., against 11 to 18 %
         for pin-click, hat, open-hat and crash ... and 6.4 % for white
         noise". Nine notes: about 1 s.
start    Nine recipes as describe measures them beside the same with their
         measured part's first 2 ms faded in (a raised cosine), and kick's,
         snare-body's and timpani's inharmonicity with the part starting 0,
         10, 30 and 60 ms later (- where the part is shorter than that):
         whether the cut at the attack's end drives the low notes'
         inharmonicity. Backs the kick "reads 0.107 ... and without its
         first 10 ms it reads 0.041"; timpani "reads 0.148"; "marimba reads
         699 Hz, wood-drum 998 and celesta 1483, and tooth's 1:3.5 reads
         0.217". Nine notes: about 0.7 s.
peaks    The spectral peaks inharmonicity weighs for one recipe (default
         kick): the part's length, the number of peaks within 50 dB of the
         strongest, the 12 that add most to the score and the 8 heaviest,
         each with its frequency, harmonic number h, score 2|h - round(h)|,
         power weight and level. Backs the kick (1:2 at 50 Hz) "reads 0.107:
         its strike spreads power between harmonics only 50 Hz apart". One
         note: about 0.3 s.
parts    Each recipe's measured part as describe takes it, by describing all
         50 with frames_power watched: the 16 shortest, the longest and the
         frames each fills at 2048/512 and 16384/4096. Backs "pin-click and
         tick, 1024 samples; tock, 1248; snare-body, 2016" and "the part at
         least 1024 samples". 100 renders (two a recipe, as describe --all):
         about 3 s.
refusals Runs analyze.py describe and search with 32 bad arguments or
         malformed --tsv files (made from descriptors.tsv in a temporary
         directory under out/checks), each of which must exit 2, and
         one good search, which must exit 0, printing each case's exit
         status and last line of output; exit 1 if any case exits otherwise
         or describe --out without --all writes its file. Backs describe "exits ... 2 on a bad option
         or an unknown recipe" and search "exits ... 2 on a malformed range,
         a bound that is not a finite number, LO above HI, an unknown key or
         tag, or a `--tsv` that is missing, empty or malformed, naming
         the file and the line". 33 runs of analyze.py: about 3 s.
json     Strict JSON from overview/loudness on mono/stereo silence then sines;
         null correlation/key scores and finite non-silent output ;
         all overview row families cover 16 s and 3 s through the end .
tempo    Blocked tempo_scan against the original DFT loop on a 60 s 120 bpm
         click envelope and 8 s seeded random envelope: identical candidates
         and argmax, relative max-norm error 1e-9; prints both timings .
all      Each command in turn (peaks for the kick), including json and tempo.

Exit 0 when every command ran (and refusals and windows passed); 1 when a
refusal case or a frame count is wrong; 2 on a usage error.
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave

sys.dont_write_bytecode = True
CHECKS = os.path.dirname(os.path.abspath(__file__))
MUSIC = os.path.dirname(CHECKS)
SPIKE = os.path.dirname(os.path.dirname(MUSIC))
ROOT = SPIKE
OUT = os.path.join(SPIKE, 'out', 'checks')           # git ignores out
if MUSIC not in sys.path:
    sys.path.insert(0, MUSIC)
import numpy as np                                     # noqa: E402
import analyze as an                                   # noqa: E402
cp = an.music_compose()


def usage(msg=None):
    sys.stderr.write((('describe_checks.py: %s\n\n' % msg) if msg else '') + __doc__)
    sys.exit(2)


def tsv_rows(argv):
    """{recipe: {column: cell}} from descriptors.tsv, or --tsv FILE, in the file's order."""
    path = an.DESC_TSV
    if argv[:1] == ['--tsv'] and len(argv) == 2:
        path = argv[1]
    elif argv:
        usage('times and groups take only --tsv FILE')
    with open(path) as fh:
        rows = [line.rstrip('\n').split('\t') for line in fh if line.strip()]
    return {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}


def num(cell):
    return None if cell == '-' else float(cell)


def operators(name):
    """A recipe's operators as op arguments, defaults filled in: the carrier, the modulator
    and a routed recipe's operators 2 and up ; and its carrier mask, 1 for a pair."""
    r = cp.RECIPES[name]
    routing = r[3] if len(r) > 3 else {}
    return [dict(cp.OP_ARGS, **o) for o in [r[0], r[1]] + list(routing.get('more', ()))], routing.get('carriers', 1)


BUILT = []


def render(name):
    """One note of a recipe as describe's measured render: (samples, fs, note in Hz, note-off sample)."""
    if not BUILT:
        cp.build()
        BUILT.append(True)
    mhz = an.default_note(cp.RECIPES[name][2], an.recipe_metadata(cp)[1].get(name))
    hold_ns = int(round(an.DESC_HOLD * 1e9))
    os.makedirs(OUT, exist_ok=True)
    base = os.path.join(OUT, 'describe-checks-%d' % os.getpid())
    tl, wav = base + '.tl', base + '.wav'
    try:
        an.render_note(cp, name, mhz, an.DESC_VEL, hold_ns, an.DESC_LEVEL, wav, tl)
        au = an.Audio(wav)
    finally:
        for p in (tl, wav):
            with contextlib.suppress(FileNotFoundError):
                os.remove(p)
    return au.mid, au.fs, mhz / 1000.0, hold_ns * au.fs // 10 ** 9


def part(x, fs, f0, n_off):
    """The measured part as measure_note takes it: from the attack's end to the note-off, or to
    40 dB below the peak if sooner, and at least 1024 samples."""
    lev, hop = an.note_levels(x, fs, f0)
    i_off = min(n_off // hop, len(lev) - 1)
    peak = float(lev[:i_off + 1].max())
    i_pk = int(np.argmax(lev[:i_off + 1] >= peak - an.PEAK_TOL))
    end = np.nonzero(lev[i_pk:i_off + 1] <= peak - an.SEG_DROP)[0]
    a = i_pk * hop
    return x[a:min(len(x), max((i_pk + end[0] if len(end) else i_off) * hop, a + 1024))]


def in_band(f, fs):
    return (f >= an.BAND[0]) & (f <= min(an.BAND[1], fs / 2))


# ---------------------------------------------------------------------------
# times and groups: descriptors.tsv beside the recipes, no render.

def cmd_times(argv):
    rows = tsv_rows(argv)
    k = 1 - 10 ** (-an.PEAK_TOL / 20)                 # the attack ends 0.5 dB below the peak, at 0.944 a
    hold = an.DESC_HOLD * 1000
    groups, short, several = {}, [], []
    print('%-13s %6s %5s %6s %5s %5s | %8s %5s | %8s %5s %6s %8s | %8s %5s | %7s' % (
        'recipe', 'note', 'a', 'd', 's', 'r', 'attack', 'reads', 'decay', 'reads', 'resid', 'no .056a',
        'release', 'reads', 'sustain'))
    for name, r in rows.items():
        ops, carriers = operators(name)
        if carriers & (carriers - 1):                # several carriers: their envelopes sum
            several.append(name)
            continue
        c = ops[0]
        a, d, s, rel = c['a'], c['d'], c['s'], c['r']
        band = 'below250' if num(r['note_hz']) < 250 else '250up'
        att, att_read = (1 - k) * a, num(r['attack_ms'])
        groups.setdefault(('attack', band), []).append((name, att_read - att, att))
        if a <= 2:
            short.append((att_read, name, a))
        dec, dec_read, tag = 0.0, num(r['decay_ms']), ''
        if s > 0:
            dec = (an.DECAY_DROP * d / s if s > an.DECAY_DROP else (s - an.PEAK_TOL) * d / s) + k * a
            if a + dec < hold:
                groups.setdefault(('decay s<120' if s < 120 else 'decay s=120', band), []).append(
                    (name, dec_read - dec, dec))
            else:
                tag = '  (decay past the note-off)'
        else:
            tag = '  (s = 0: no decay)'
        s_off = min(s, s * (hold - a) / d) if d else s
        relf = max(0.0, (an.RELEASE_DROP - s_off) * rel / 96.3)
        if r['release_ms'] != '-':
            groups.setdefault(('release', band), []).append((name, num(r['release_ms']) - relf, relf))
        print('%-13s %6s %5d %6d %5g %5d | %8.1f %5s | %8.1f %5s %+6.1f %+8.1f | %8.1f %5s | %7s%s' % (
            name, r['note_hz'], a, d, s, rel, att, r['attack_ms'], dec, r['decay_ms'], dec_read - dec,
            dec_read - (dec - k * a if s > 0 else 0.0), relf, r['release_ms'], r['sustain_db'], tag))
    if several:
        print('left out, as several carriers sum their envelopes: %s' % ', '.join(several))
    print()
    print('residuals, read minus formula, by group (a frame is one period below 250 Hz):')
    for key in sorted(groups):
        g = groups[key]
        lo, hi = min(g, key=lambda x: x[1]), max(g, key=lambda x: x[1])
        pct = max(g, key=lambda x: abs(x[1]) / x[2] if x[2] else 0)
        print('  %-12s %-9s n=%2d  %+.1f (%s) to %+.1f (%s) ms; largest share: %s, %.1f %% of %.0f' % (
            key[0], key[1], len(g), lo[1], lo[0], hi[1], hi[0], pct[0],
            100 * abs(pct[1]) / pct[2] if pct[2] else 0, pct[2]))
    if short:
        print('  attacks of 1 to 2 ms (%d recipes) read %.0f (%s) to %.0f (%s) ms' % (
            len(short), min(short)[0], min(short)[1], max(short)[0], max(short)[1]))
    print('README: the 14 decays toward a sustain by -1.2 to +2.8 ms (at most 1.8 %: brass, 152 by the formula, '
          'reads 155), the 22 percussive decays by -1.4 to +2.9 ms, the attacks by -1.0 to +1.2 ms and the releases '
          'by -5.3 to +2.0 ms (drone, 3115, reads 3110); attacks of 1 to 2 ms read 0 to 2; kick 53, reads 66; '
          'snare-body 18, reads 23; drone 4000 reads 3777, glide 250 reads 236, flute 45 reads 42; epiano 1600, '
          'reads 1602; flute 419, reads 421; night-pad 2251, reads 2251; epiano 109, reads 110; glide 1558, '
          'reads 1556; ep-body 105, reads 106; ep-body sustain 37.2')
    return 0


GROUPS = (   # (column, what README calls them, recipes or a rule, what README prints)
    ('inharmonicity', 'whole-number ratios, the kick aside', 'whole', 'at most 0.001'),
    ('inharmonicity', 'the 1:1.4, 1:2.76 and 1:3.17 bells', ('bell', 'planet-1.4', 'planet-2.76', 'planet-3.17'),
     '0.318 to 0.585'),
    ('inharmonicity', 'noise (the noise tag)', 'noise', '0.273 to 0.472'),
    ('inharmonicity', "timpani's octave-low thump (1:1.5)", ('timpani',), '0.148'),
    ('inharmonicity', 'the kick (1:2 at 50 Hz)', ('kick',), '0.107'),
    ('inharmonicity', "tooth's 1:3.5", ('tooth',), '0.217'),
    ('odd_even_db', 'the odd-harmonic recipes', ('hollow', 'clarinet', 'square', 'celesta', 'triangle', 'ep-tine',
                                                 'glide', 'tom', 'cowbell'), '+21 to +35'),
    ('odd_even_db', 'the soft 1:1 tones', ('epiano', 'harp', 'organ', 'flute', 'drone', 'round-bass',
                                           'boogie-bass'), '-11 to -17'),
    ('odd_even_db', 'brighter or harder fed-back 1:1 tones', ('brass', 'plucked-bass', 'motor-bass', 'saw',
                                                              'harpsichord'), 'each',
     'brass -7.4, plucked-bass -7.8, motor-bass -5.8, saw -4.6, harpsichord -3.2'),
    ('odd_even_db', 'the 1:2.76 bell', ('planet-2.76',), '-26.3'),
    ('odd_even_db', 'above 6.67 kHz', ('hat', 'open-hat'), 'each', '- for hat and open-hat'),
    ('noisiness', 'noise whose power gathers in lines', ('noise-tail', 'clap', 'snare-wires', 'tock', 'tick'),
     'each', 'noise-tail 0.101, clap 0.160, snare-wires 0.294, tock 0.277, tick 0.658'),
    ('noisiness', 'pin-click, hat, open-hat and crash', ('pin-click', 'hat', 'open-hat', 'crash'), '0.815 to 0.917'),
    ('brightness_hz', 'struck notes, the strike by its share', ('marimba', 'wood-drum', 'celesta'), 'each',
     'marimba 699, wood-drum 998, celesta 1483'),
    ('sustain_db', 'still falling at the note-off', ('ep-body',), '37.2'),
)


def cmd_groups(argv):
    rows = tsv_rows(argv)
    tags = an.recipe_tags(cp)
    whole = {}
    out = []
    for name, r in rows.items():
        ops, _ = operators(name)
        whole[name] = all(float(o['ratio']).is_integer() for o in ops)
        out.append((whole[name], num(r['inharmonicity']), name, ':'.join('%g' % o['ratio'] for o in ops),
                    max(o['fb'] for o in ops), r))        # the largest feedback: sub-bass's is on its carrier
    for w, inh, name, ratios, fb, r in sorted(out, key=lambda o: o[:3]):
        print('%-5s inharm %.3f  %-12s %s fb %g  note %s  odd/even %s  noisy %s  bright %s' % (
            'whole' if w else 'other', inh, name, ratios, fb, r['note_hz'], r['odd_even_db'], r['noisiness'],
            r['brightness_hz']))
    print()
    print('the groups README names (TSV cells; README as it prints them):')
    for g in GROUPS:
        col, label, who, readme = g[0], g[1], g[2], g[-1]
        if who == 'whole':
            who = [n for n in rows if whole[n] and n != 'kick']
        elif who == 'noise':
            who = [n for n in rows if 'noise' in tags.get(n, [])]
        if len(g) == 5 or len(who) == 1:
            got = ', '.join('%s %s' % (n, rows[n][col]) if len(who) > 1 else rows[n][col] for n in who)
        else:
            vals = sorted((num(rows[n][col]), n) for n in who)
            got = '%s (%s) to %s (%s), %d recipes' % (rows[vals[0][1]][col], vals[0][1], rows[vals[-1][1]][col],
                                                      vals[-1][1], len(vals))
        print('  %-13s %s: %s; README: %s' % (col, label, got, readme))
    return 0


# ---------------------------------------------------------------------------
# windows and bursts: analyze.py's framing and measure_note on made-up signals, no render.

def cmd_windows(argv):
    bad = 0
    for nfft, hop in ((an.SPEC_NFFT, an.SPEC_HOP), (an.FLAT_NFFT, an.FLAT_HOP)):
        lead, w2 = nfft - hop, np.hanning(nfft) ** 2
        lo, hi = 2.0, 0.0
        for n in sorted({1, 1024, 1248, 1500, 2016, hop - 1, hop, hop + 1, nfft - 1, nfft, 3 * nfft + 777,
                         int(an.DESC_HOLD * an.DESC_FS)}):
            size = 2 * lead + n + (-n) % hop           # frames_power's padding
            frames = 1 + (size - nfft) // hop
            wt = np.zeros(size)
            for i in range(frames):
                wt[i * hop:i * hop + nfft] += w2
            s = wt[lead:lead + n]
            got = an.frames_power(np.ones(n), an.DESC_FS, nfft, hop)[1].shape[0]
            bad += got != frames
            lo, hi = min(lo, s.min()), max(hi, s.max())
            print('%5d/%-4d part %6d samples: %3d frames (frames_power %3d%s)  weight %.6f to %.6f' % (
                nfft, hop, n, frames, got, '' if got == frames else ', DIFFERENT', s.min(), s.max()))
        few = 3 + -(-1 // hop)
        many = 3 + -(-nfft // hop)
        print('%5d/%-4d weights %.6f to %.6f: within %.3f %% of 1.5; a part of 1 to %d samples fills %d to %d '
              'frames' % (nfft, hop, lo, hi, 100 * max(1.5 - lo, hi - 1.5) / 1.5, nfft, few, many))
    print('README: the squared windows sum to 1.5 within 0.06 %; a part shorter than a frame fills 4 to 7 frames '
          '(16384/4096); pin-click and tick, 1024 samples; tock, 1248; snare-body, 2016: 5 to 7 frames (2048/512)')
    if bad:
        print('FAIL: %d frame counts differ from frames_power' % bad)
    return 1 if bad else 0


def cmd_bursts(argv):
    fs = an.DESC_FS
    rng = np.random.default_rng(1)
    for kind in ('sine 440', 'sine 5000', 'noise'):
        for n in (1024, 2048, 4096, 24000):
            x = np.zeros(fs)
            if kind == 'noise':
                x[:n] = 0.5 * rng.standard_normal(n)
            else:
                x[:n] = 0.5 * np.sin(2 * np.pi * float(kind.split()[1]) * np.arange(n) / fs)
            d = an.measure_note(x, fs, 440.0, fs - 1)
            print('%-9s %5d samples: noisiness %.3f  brightness %6.0f Hz  inharmonicity %.3f' % (
                kind, n, d['noisiness'], d['brightness_hz'], d['inharmonicity']))
    print('README: 1024 samples of white noise read 0.98, and 1024 of a 440 Hz sine 0.002')
    return 0


# ---------------------------------------------------------------------------
# noise, start, peaks and parts: notes rendered by the harness.

def cmd_noise(argv):
    rng = np.random.default_rng(7)
    ff, Q = an.frames_power(rng.standard_normal(10 * an.DESC_FS), an.DESC_FS, an.FLAT_NFFT, an.FLAT_HOP)
    b = in_band(ff, an.DESC_FS)
    Q = Q[:, b]
    w = Q.sum(axis=1)
    top = np.sort(Q, axis=1)[:, ::-1][:, :10].sum(axis=1) / w
    print('white noise, 10 s: %d bins a frame in the band; the 10 strongest hold %.1f %% of the power' % (
        b.sum(), 100 * (top * w).sum() / w.sum()))
    tags = an.recipe_tags(cp)
    widths, tops = [], []
    for name in [n for n in cp.RECIPES if 'noise' in tags[n]]:
        x, fs, f0, n_off = render(name)
        seg = part(x, fs, f0, n_off)
        ff, Q = an.frames_power(seg, fs, an.FLAT_NFFT, an.FLAT_HOP)
        b = in_band(ff, fs)
        fb, Q = ff[b], Q[:, b]
        P = Q.mean(axis=0)
        c = np.cumsum(P) / P.sum()
        lo, hi = fb[np.searchsorted(c, 0.05)], fb[np.searchsorted(c, 0.95)]
        width = 100 * (hi - lo) / (an.BAND[1] - an.BAND[0])
        sub = Q[:, (fb >= lo) & (fb <= hi)]            # noisiness over that band alone, scaled as noisiness is
        sub = sub + np.maximum(sub.max(axis=1, keepdims=True), 1e-30) * 1e-12
        flat = np.exp(np.log(sub).mean(axis=1)) / sub.mean(axis=1)
        ws = sub.sum(axis=1)
        nsub = min(1.0, float((flat * ws).sum() / ws.sum()) / float(np.exp(-np.euler_gamma)))
        w = Q.sum(axis=1)
        lines, strong = [], []
        for q in Q:                                    # lines: 10 dB over the median of the 33 bins around
            med = np.median(np.lib.stride_tricks.sliding_window_view(np.pad(q, 16, mode='edge'), 33), axis=1)
            lines.append(q[q > med * 10].sum() / max(q.sum(), 1e-30))
            strong.append(np.sort(q)[::-1][:10].sum() / max(q.sum(), 1e-30))
        share_lines = 100 * (np.array(lines) * w).sum() / w.sum()
        share_top = 100 * (np.array(strong) * w).sum() / w.sum()
        widths.append((width, name))
        tops.append((share_top, name))
        print('%-12s %5.0f Hz  part %6d  90 %% of the power %5.0f to %5.0f Hz, %4.1f %% of the band  noisiness '
              'there %.3f  in lines %5.1f %%  in the 10 strongest bins %5.1f %%' % (
                  name, f0, len(seg), lo, hi, width, nsub, share_lines, share_top))
    print('90 %% of the power over %.1f (%s) to %.1f (%s) %% of the band; the 10 strongest bins hold %s' % (
        min(widths)[0], min(widths)[1], max(widths)[0], max(widths)[1],
        ', '.join('%s %.1f' % (n, t) for t, n in sorted(tops, reverse=True))))
    print("README: 90 % of its power over 81 to 92 % of 20 Hz to 20 kHz; of a frame's 853 bins the 10 strongest hold "
          "64 % of noise-tail's power, 54 % of clap's, 52 % of snare-wires', 38 % of tock's and 24 % of tick's, "
          "11 to 18 % for pin-click, hat, open-hat and crash and 6.4 % for white noise")
    return 0


def cmd_start(argv):
    orig = an.frames_power
    mode = {'skip': 0, 'fade': False}

    def framed(seg, fs, nfft, hop):
        seg = seg[mode['skip']:]
        if mode['fade']:
            n = int(0.002 * fs)
            seg = seg.copy()
            seg[:n] *= 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)
        return orig(seg, fs, nfft, hop)

    an.frames_power = framed
    late = []
    try:
        for name in ('kick', 'snare-body', 'timpani', 'tooth', 'marimba', 'wood-drum', 'celesta', 'flute', 'drone'):
            x, fs, f0, n_off = render(name)
            res = []
            for fade in (False, True):
                mode['fade'] = fade
                res.append(an.measure_note(x, fs, f0, n_off))
            mode['fade'] = False
            oe = ['-' if d['odd_even_db'] is None else '%.1f' % d['odd_even_db'] for d in res]
            print('%-11s %5.0f Hz  inharm %.3f -> %.3f faded   bright %5.0f -> %5.0f   odd/even %s -> %s   '
                  'noisy %.3f -> %.3f' % (name, f0, res[0]['inharmonicity'], res[1]['inharmonicity'],
                                          res[0]['brightness_hz'], res[1]['brightness_hz'], oe[0], oe[1],
                                          res[0]['noisiness'], res[1]['noisiness']))
            if name in ('kick', 'snare-body', 'timpani'):
                n_part, cells = len(part(x, fs, f0, n_off)), []
                for ms in (0, 10, 30, 60):
                    mode['skip'] = ms * fs // 1000
                    cells.append('%2d ms %s' % (ms, '-' if mode['skip'] >= n_part else
                                                '%.3f' % an.measure_note(x, fs, f0, n_off)['inharmonicity']))
                    mode['skip'] = 0
                late.append('%-11s part %5d samples; inharmonicity with it starting later: %s' % (
                    name, n_part, '   '.join(cells)))
    finally:
        an.frames_power = orig
    print('\n'.join(late))
    print("README: the kick reads 0.107, without its first 10 ms 0.041; timpani 0.148; marimba 699 Hz, wood-drum 998 "
          "and celesta 1483; tooth's 1:3.5 0.217")
    return 0


def cmd_peaks(argv):
    if len(argv) > 1:
        usage('peaks takes one RECIPE')
    name = argv[0] if argv else 'kick'
    if name not in cp.RECIPES:
        usage('no recipe %r' % name)
    x, fs, f0, n_off = render(name)
    seg = part(x, fs, f0, n_off)
    f, P = an.frames_power(seg, fs, an.SPEC_NFFT, an.SPEC_HOP)
    P = P.mean(axis=0)
    b = in_band(f, fs)
    fb, Pb = f[b], P[b]
    L = 10 * np.log10(Pb + Pb.sum() * 1e-20)
    k = np.nonzero((Pb[1:-1] > Pb[:-2]) & (Pb[1:-1] >= Pb[2:]) &
                   (Pb[1:-1] >= Pb.max() * 10 ** (-an.PEAK_RANGE / 10)))[0] + 1
    den = L[k - 1] - 2 * L[k] + L[k + 1]
    shift = np.where(den < 0, 0.5 * (L[k - 1] - L[k + 1]) / np.where(den < 0, den, -1.0), 0.0)
    h = (fb[k] + shift * (f[1] - f[0])) / f0
    score = 2 * np.abs(h - np.round(h))
    w = Pb[k] / Pb[k].sum()
    print('%s at %g Hz: part %d samples; %d peaks within %g dB; inharmonicity %.3f' % (
        name, f0, len(seg), len(k), an.PEAK_RANGE, (w * score).sum()))
    print('the 12 that add most to the score:')
    for j in np.argsort(-w * score)[:12]:
        print('  %8.1f Hz  h %6.3f  score %.3f  weight %.4f  level %6.1f dB' % (
            fb[k[j]], h[j], score[j], w[j], L[k[j]] - L.max()))
    print('the 8 heaviest:')
    for j in np.argsort(-w)[:8]:
        print('  %8.1f Hz  h %6.3f  score %.3f  weight %.4f' % (fb[k[j]], h[j], score[j], w[j]))
    print('README: the kick (1:2 at 50 Hz, its index gone in 60 ms) reads 0.107: its strike spreads power between '
          'harmonics only 50 Hz apart')
    return 0


def cmd_parts(argv):
    if not BUILT:
        cp.build()
        BUILT.append(True)
    tags = an.recipe_tags(cp)
    orig, seen = an.frames_power, []

    def spy(seg, fs, nfft, hop):
        f, P = orig(seg, fs, nfft, hop)
        seen.append((nfft, len(seg), P.shape[0]))
        return f, P

    an.frames_power = spy
    out = []
    try:
        for name in cp.RECIPES:
            del seen[:]
            an.describe_one(cp, tags, name, an.default_note(cp.RECIPES[name][2], an.recipe_metadata(cp)[1].get(name)),
                            an.DESC_VEL, an.DESC_HOLD)
            frames = {nfft: n for nfft, _, n in seen}
            out.append((seen[0][1], name, frames[an.FLAT_NFFT], frames[an.SPEC_NFFT]))
    finally:
        an.frames_power = orig
        with contextlib.suppress(OSError):
            os.rmdir(an.DESC_DIR)                      # describe_one leaves it; describe removes it
    out.sort()
    for n, name, flat, spec in out[:16]:
        print('%-12s %7d samples %8.1f ms  %3d frames of %d, %2d of %d' % (
            name, n, 1000.0 * n / an.DESC_FS, flat, an.FLAT_NFFT, spec, an.SPEC_NFFT))
    n, name, flat, spec = out[-1]
    print('longest: %s, %d samples, %.1f ms' % (name, n, 1000.0 * n / an.DESC_FS))
    print('README: pin-click and tick, 1024 samples; tock, 1248; snare-body, 2016: 5 to 7 frames of 2048')
    return 0


# ---------------------------------------------------------------------------
# refusals: analyze.py describe and search, run as a user runs them.

def cmd_refusals(argv):
    os.makedirs(OUT, exist_ok=True)
    fix = tempfile.mkdtemp(prefix='refusals-', dir=OUT)
    try:
        return refusals(fix)
    finally:
        shutil.rmtree(fix, ignore_errors=True)


def refusals(fix):
    with open(an.DESC_TSV) as fh:
        lines = fh.read().splitlines(True)

    def put(name, text, mode='w'):
        p = os.path.join(fix, name)
        with open(p, mode) as fh:
            fh.write(text)
        return p

    def cell(i, j, value):
        cells = lines[i].split('\t')
        cells[j] = value
        return ''.join(lines[:i]) + '\t'.join(cells) + ''.join(lines[i + 1:])

    never = os.path.join(fix, 'never.tsv')
    cases = [
        ('describe, no arguments', ['describe']),
        ('describe, RECIPE and --all', ['describe', 'flute', '--all']),
        ('describe, --selftest and --all', ['describe', '--selftest', '--all']),
        ('describe, --out without --all', ['describe', 'flute', '--out', never]),
        ('describe, --selftest --json', ['describe', '--selftest', '--json']),
        ('describe, --selftest --vel 100', ['describe', '--selftest', '--vel', '100']),
        ('describe, --note 0', ['describe', 'flute', '--note', '0']),
        ('describe, --note 55', ['describe', 'drone', '--note', '55']),
        ('describe, --note 19999', ['describe', 'drone', '--note', '19999']),
        ('describe, --note 1.5', ['describe', 'flute', '--note', '1.5']),
        ('describe, --vel 40000', ['describe', 'flute', '--vel', '40000']),
        ('describe, --seconds 100', ['describe', 'flute', '--seconds', '100']),
        ('describe, --seconds nan', ['describe', 'flute', '--seconds', 'nan']),
        ('describe, unknown recipe', ['describe', 'nope']),
        ('search, missing --tsv', ['search', '--tsv', os.path.join(fix, 'missing.tsv')]),
        ('search, --tsv a directory', ['search', '--tsv', fix]),
        ('search, empty --tsv', ['search', '--tsv', put('empty.tsv', '')]),
        ('search, blank-lines --tsv', ['search', '--tsv', put('blank.tsv', '\n\n')]),
        ('search, wrong header', ['search', '--tsv', put('header.tsv', lines[0].replace('noisiness', 'noise') +
                                                         ''.join(lines[1:]))]),
        ('search, wrong field count', ['search', '--tsv', put('fields.tsv', ''.join(lines[:5]) +
                                                              lines[5].rstrip('\n') + '\textra\n' +
                                                              ''.join(lines[6:]))]),
        ('search, non-numeric cell', ['search', '--tsv', put('cell.tsv', cell(7, 8, 'abc'))]),
        ('search, nan cell', ['search', '--tsv', put('nancell.tsv', cell(9, 3, 'nan'))]),
        ('search, not UTF-8', ['search', '--tsv', put('binary.tsv', b'\xff\xfe\x00name\n', 'wb')]),
        ('search, KEY=nan..', ['search', 'noisiness=nan..']),
        ('search, KEY=..inf', ['search', 'noisiness=..inf']),
        ('search, KEY=-inf..1', ['search', 'noisiness=-inf..1']),
        ('search, KEY=abc..', ['search', 'noisiness=abc..']),
        ('search, unknown key', ['search', 'loudness=1..2']),
        ('search, name as key', ['search', 'name=1..2']),
        ('search, no ..', ['search', 'noisiness=0.5']),
        ('search, unknown --tag', ['search', '--tag', 'gong']),
        ('search, LO above HI', ['search', 'noisiness=0.9..0.1']),
        ('search, a good one (control)', ['search', 'noisiness=0.9..']),
    ]
    wrong = []
    for label, args in cases:
        want = 0 if 'control' in label else 2
        r = subprocess.run([sys.executable, '-B', os.path.join(MUSIC, 'analyze.py')] + args, cwd=ROOT,
                           capture_output=True, text=True)
        last = (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or [''])[-1]
        last = last.replace(fix + os.sep, 'TMP/').replace(fix, 'TMP').replace(ROOT + os.sep, '')
        ok = r.returncode == want
        print('%s %-30s rc=%d  %s' % ('ok  ' if ok else 'FAIL', label, r.returncode, last))
        if not ok:
            wrong.append(label)
    print('%d of %d cases exit as they must (2 for each refusal, 0 for the control)%s' % (
        len(cases) - len(wrong), len(cases), '' if not wrong else '; wrong: ' + ', '.join(wrong)))
    if os.path.exists(never):
        print('FAIL: describe --out without --all wrote its file')
        wrong.append(never)
    return 1 if wrong else 0


def cmd_json(argv):
    """/: strict JSON, null silence and complete last-window coverage."""
    if argv:
        usage('json takes no arguments')
    os.makedirs(OUT, exist_ok=True)
    failures = []

    def constant(token):
        raise ValueError('non-finite JSON token ' + token)

    expected_keys = [(None, name + ' ' + mode)
                     for name in an.NAMES for mode in ('major', 'minor')]
    for values in (np.zeros(12), np.ones(12)):
        if an.keys_of(values) != expected_keys:
            failures.append('constant chroma order/scores')
    mapped = an.jsonable({'values': [float('nan'), float('inf'), np.float32('-inf'),
                                    np.float64(1.25), np.int64(3)]})
    if mapped != {'values': [None, None, None, 1.25, 3]}:
        failures.append('defensive non-finite conversion')
    try:
        json.loads(json.dumps(mapped, allow_nan=False), parse_constant=constant)
    except (ValueError, TypeError):
        failures.append('defensive strict JSON')
    with tempfile.TemporaryDirectory(prefix='json-', dir=OUT) as temporary:
        fs = 48000
        t = np.arange(4 * fs) / fs
        left = np.concatenate((np.zeros(12 * fs), 12000 * np.sin(2 * np.pi * 440 * t)))
        right = np.concatenate((np.zeros(12 * fs), 10000 * np.sin(2 * np.pi * 660 * t)))
        for duration in (16, 3):
            for channels in (2, 1):
                path = os.path.join(temporary, f'input-{duration}-{channels}.wav')
                samples = np.column_stack((left, right)) if channels == 2 else left
                samples = samples[-duration * fs:]
                with wave.open(path, 'wb') as wav:
                    wav.setnchannels(channels)
                    wav.setsampwidth(2)
                    wav.setframerate(fs)
                    wav.writeframes(np.rint(samples).astype('<i2').tobytes())
                for command in ('overview', 'loudness'):
                    output = os.path.join(temporary, f'{command}-{duration}-{channels}.json')
                    run = subprocess.run([sys.executable, '-B', os.path.join(MUSIC, 'analyze.py'),
                                          command, path, '--json', output],
                                         cwd=ROOT, capture_output=True, text=True)
                    label = f'{command} channels={channels} duration={duration}s'
                    try:
                        if run.returncode:
                            raise ValueError(f'command exit {run.returncode}')
                        with open(output) as fh:
                            text = fh.read()
                        if re.search(r'\b(?:NaN|Infinity)\b', text):
                            raise ValueError('non-finite literal')
                        result = json.loads(text, parse_constant=constant)['result']
                        if command == 'overview':
                            if duration == 16 and any(row['corr'] is not None for row in result['windows'][:3]):
                                raise ValueError('silent correlation is not null')
                            for family, width in (('windows', 4), ('tempo', 20), ('key', 10)):
                                rows = result[family]
                                if len(rows) != max(1, duration // width):
                                    raise ValueError(f'{family}: wrong window count')
                                for k, row in enumerate(rows):
                                    end = duration if k == len(rows) - 1 else (k + 1) * width
                                    if row['t'] != k * width or row['length'] != end - k * width:
                                        raise ValueError(f'{family}: wrong start/length')
                                if rows[-1]['t'] + rows[-1]['length'] != duration:
                                    raise ValueError(f'{family}: final window misses the end')
                            if not np.isfinite(result['windows'][-1]['corr']):
                                raise ValueError('signal correlation is not finite')
                            if any(score is None for score, _ in result['key'][-1]['keys']):
                                raise ValueError('final key window misses the signal')
                            if 'nan' in run.stdout.lower():
                                raise ValueError('non-finite displayed score')
                        print(f'JSON ok {label}: strict parse' +
                              ('; windows/tempo/key reach the end' if command == 'overview' else ''))
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        failures.append(label + ': ' + str(error))
    for failure in failures:
        print('JSON FAIL ' + failure)
    print('JSON checks ' + ('FAIL' if failures else 'ok'))
    return 1 if failures else 0


def cmd_tempo(argv):
    if argv:
        usage('tempo takes no arguments')

    def original(o, t, lo, hi, step, harmonics):
        bpms = np.arange(lo, hi - step / 2, step)
        e = o - o.mean()
        mags = np.array([sum(abs(np.dot(e, np.exp(-2j * np.pi * h * b / 60.0 * t)))
                             for h in harmonics) for b in bpms])
        return bpms, mags

    fe = 400
    click_t = np.arange(60 * fe) / fe
    click = np.exp(-(click_t % 0.5) / 0.015)
    random_t = np.arange(8 * fe) / fe
    random = np.random.RandomState(47).random_sample(len(random_t))
    cases = [('click', click, click_t), ('random', random, random_t)]
    wrong = 0
    for name, o, t in cases:
        for harmonics in ((1,), (1, 2, 4)):
            args = (o, t, 70, 180, 0.2, harmonics)
            start = time.perf_counter()
            want_b, want = original(*args)
            old_time = time.perf_counter() - start
            sizes = []
            exponential = np.exp
            def watch(matrix, *args, **kwargs):
                sizes.append(matrix.nbytes)
                return exponential(matrix, *args, **kwargs)
            start = time.perf_counter()
            np.exp = watch
            try:
                got_b, got = an.tempo_scan(*args)
            finally:
                np.exp = exponential
            new_time = time.perf_counter() - start
            absolute = np.max(np.abs(got - want))
            relative = absolute / max(np.max(np.abs(want)), np.finfo(float).tiny)
            pointwise = np.abs(got - want) / np.maximum(np.abs(want), np.finfo(float).tiny)
            worst = np.argmax(pointwise)
            ok = (np.array_equal(got_b, want_b) and np.isfinite(got).all() and
                  np.argmax(got) == np.argmax(want) and relative <= 1e-9 and
                  sizes and max(sizes) <= 64 * 1024 * 1024)
            if name == 'click':
                ok = ok and abs(got_b[np.argmax(got)] - 120) < 1e-9
            wrong += not ok
            print('%s tempo %s harmonics=%s argmax=%.6f relative_maxnorm=%.3g absolute=%.3g pointwise_max=%.3g reference_at_max=%.3g old=%.6fs new=%.6fs matrix_bytes=%d blocks=%d' %
                  ('ok' if ok else 'FAIL', name, ','.join(map(str, harmonics)),
                   got_b[np.argmax(got)], relative, absolute, pointwise[worst], want[worst],
                   old_time, new_time, max(sizes), len(sizes)))
    print('tempo checks %s: %d/4' % ('ok' if not wrong else 'FAIL', 4 - wrong))
    return int(bool(wrong))


COMMANDS = {'times': cmd_times, 'groups': cmd_groups, 'windows': cmd_windows, 'bursts': cmd_bursts,
            'noise': cmd_noise, 'start': cmd_start, 'peaks': cmd_peaks, 'parts': cmd_parts,
            'refusals': cmd_refusals, 'json': cmd_json, 'tempo': cmd_tempo}


def main(argv):
    if not argv or argv[0] not in list(COMMANDS) + ['all']:
        usage('a command is one of %s, all' % ', '.join(COMMANDS) if argv else None)
    try:
        if argv[0] != 'all':
            return COMMANDS[argv[0]](argv[1:])
        if argv[1:]:
            usage('all takes no arguments')
        rc = 0
        for name, fn in COMMANDS.items():
            print('== %s' % name)
            rc = max(rc, fn([]))
            print()
        return rc
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(OUT)                              # only when empty


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
