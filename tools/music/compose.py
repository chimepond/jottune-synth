#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Reusable musical-time composition, patches, recipes and performance helpers.

Host tools may use floating point; the synthesis library remains integer-only.
Use Piece and the score adapter to construct complete standalone TL records.

    python3 tools/music/compose.py --recipes
    python3 tools/music/compose.py --expand INPUT.tl OUTPUT.tl
    python3 tools/music/compose.py --selftest-perf
    python3 tools/music/compose.py --selftest-recipes
"""
import copy
import decimal
import functools
import fcntl
from fractions import Fraction
import inspect
import os
import re
import subprocess
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
SPIKE = os.path.dirname(os.path.dirname(TOOLS))
MUSIC = os.path.join(SPIKE, 'music')
DATA = os.path.join(SPIKE, 'data')
OUT = os.path.join(SPIKE, 'out', 'music')
H = os.path.join(SPIKE, 'build', 's12e10p1v1', 'harness')
ENGINE = 'a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2'
FX_BUSES = ('chorus', 'delay', 'reverb', 'duck', 'flanger')  # FX_BUS_BIT / FX_BUS_NAME row order
TARGET = -1.5          # dBFS peak
TOL = 0.5
ROW = 'patch %-8s %d  %-9d %-10d %-9d %-12d %-11d %-9d %-8d %-12d %-10d%s'   # a timeline patch line
HEAD = '#     name     op ratio_q16 level_db10 index_q10 feedback_q10 vel_sens_q8 attack_ms decay_ms sustain_db10 release_ms'
KS_HEAD = ' ks_break_millihz ks_slope_db10'

NOTE = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
OP_WAVES = tuple(range(12))  #  and the staged operator extension wave codes


def midi(n):
    m = re.fullmatch(r'([A-G])([#b]?)(-?\d)', n)
    return 12 * (int(m.group(3)) + 1) + NOTE[m.group(1)] + {'#': 1, 'b': -1, '': 0}[m.group(2)]


NUMERIC_PREC = 50               # : same precision as the Scala reader


def decimal_value(value):
    """Keep exact fractions; a float supplies its shortest decimal spelling."""
    if hasattr(value, 'numerator') and hasattr(value, 'denominator'):
        return decimal.Decimal(value.numerator) / decimal.Decimal(value.denominator)
    return decimal.Decimal(str(value))


def millihz(value):
    """Hz to integer mHz, rounded half up with fixed decimal precision."""
    with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        return int((decimal_value(value) * 1000).to_integral_value(rounding=decimal.ROUND_HALF_UP))


def pitch_scale(value, steps, divisions):
    """Scale a pitch by 2^(steps/divisions), independently of the binary libm."""
    with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        exponent = decimal_value(steps) / divisions
        scale = (decimal.Decimal(2) ** int(exponent) if exponent == int(exponent)
                 else (decimal.Decimal(2).ln() * exponent).exp())
        return decimal_value(value) * scale


def half_up(value):
    """A portable value to the nearest integer, half up : integer mHz from mHz."""
    with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        return int(decimal_value(value).to_integral_value(rounding=decimal.ROUND_HALF_UP))


def log_between(lo, hi, u):
    """lo x (hi/lo)^u, the value u of the way from lo to hi on a logarithmic scale,
    independently of the binary libm ."""
    with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        lo = decimal_value(lo)
        return lo * ((decimal_value(hi) / lo).ln() * decimal_value(u)).exp()


def mhz(n):
    return mhz_m(midi(n))


NAMES = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']


@functools.lru_cache(maxsize=8192)
def mhz_m(m):
    """Millihertz of a fractional MIDI number, rounded half up."""
    with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        hz = pitch_scale(440, decimal_value(m) - 69, 12)
        return millihz(hz)


def label(m):
    """Nearest note name of a fractional MIDI number, with cents when off it."""
    k = round(m)
    c = round((m - k) * 100)
    name = NAMES[k % 12] + str(k // 12 - 1)
    return name if c == 0 else '%s%+dc' % (name, c)


class Rng:
    """A fixed 32-bit LCG, so humanised times and twinkles are the same on every run."""

    def __init__(self, seed):
        self.x = seed & 0xffffffff

    def next(self):
        self.x = (self.x * 1664525 + 1013904223) & 0xffffffff
        return self.x / 4294967296.0


PP, P, MP, MF, F, FF = 9000, 14000, 19000, 24000, 28000, 32767


def op(ratio=1.0, level=0.0, index=0.0, fb=0.0, vel=0, a=1, d=100, s=0.0, r=100, ks_break=0.0, ks_slope=0.0, wave=0,
       rs=0, fixed=0.0, phase=0.0, free=0):
    """ units: level and s in dB below peak, index and fb in radians.
    ks_break (Hz) and ks_slope (dB per octave) are the modulator's index key
    scaling : notes above ks_break get an index ks_slope dB smaller per
    octave, 120 dB at most; 0 is off, and the carrier ignores both. wave
    (, operator extension): 0 sine, 1 half, 2 absolute, 3 quarter, 4 noise,
    5 alternating sine, 6 camel, 7 square, 8 log-saw, 9 pink, 10 low-pass and 11 high-pass noise.
    rs, fixed, phase
    and free are the operator extras : rs, 0 to
    7, shortens every envelope segment by 2^(-oct x rs / 8) for a note oct
    octaves above A0 (0 to 8); fixed (Hz, 0 off) is the operator's frequency
    before its ratio, whatever the note; phase (degrees, 0 to 359.994 as in
    LIMITS: a recipe delta stops at either end, never wrapping) is where it
    starts at a note-on from silence; free 1 continues phase and noise from
    the last note but freezes while idle. free 2 advances across idle time in
    closed form and takes its initial phase at voice init . The list
    has 12 values, or 16 when one of those four is set."""
    with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        o = [round(ratio * 65536), round(level * 10), round(index * 1024), round(fb * 1024),
             vel, a, d, round(s * 10), r, millihz(ks_break), round(ks_slope * 10), wave]
        w2 = [rs, millihz(fixed), round(phase * 65536 / 360), free]
    return o + w2 if any(w2) else o


def patch_row(name, i, o, comment=''):
    """A timeline patch line: 9 values, then the optional ones up to the last
    that is not 0, key scaling as a pair, as the harness writes them: key
    scaling's 2 only when it is set, the wave as a 12th only when it is not 0
    (key scaling then written as 0 0), and the operator extras rate_scale,
    fixed_millihz, phase_u16 and free_run as the 13th to 16th ."""
    tail = list(o[9:16])
    while tail and not tail[-1]:
        tail.pop()
    if len(tail) == 1:
        tail.append(0)
    ks = ''.join(' %-*d' % wx for wx in zip((16, 13, 4, 10, 13, 9), tail[:-1])) + (' %d' % tail[-1] if tail else '')
    return (ROW % (name, i, *o[:9], ks + comment)).rstrip()


LFO_FIELDS = ('WAVE', 'RATE_MILLIHZ', 'DELAY_MS', 'FADE_MS', 'PITCH_CENTS', 'LEVEL_DB10',
              'INDEX_Q10', 'PHASE_U16', 'MASK_PITCH', 'MASK_LEVEL', 'MASK_INDEX', 'FREE')
LFO_LIMITS = ((0, 6), (10, 50000), (0, 60000), (0, 60000), (0, 1200), (0, 1200),
              (0, 65535), (0, 65535), (0, 15), (0, 15), (0, 15), (0, 2))


def lfo_values(values, ops=None):
    """Validate seven LFO fields, eleven with phase/masks, or twelve with FREE .
    ops, when known, also bounds the three masks to the patch's operators."""
    if len(values) not in (7, 11, 12):
        raise ValueError('lfo takes WAVE RATE_MILLIHZ DELAY_MS FADE_MS PITCH_CENTS LEVEL_DB10 INDEX_Q10, '
                         'then PHASE_U16 MASK_PITCH MASK_LEVEL MASK_INDEX [FREE] or none')
    for name, (lo, hi), value in zip(LFO_FIELDS, LFO_LIMITS, values):
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError('lfo %s %s is not a whole number from %d to %d' % (name, value, lo, hi))
    if len(values) == 12 and values[11] not in (0, 2):
        raise ValueError('lfo FREE is 0 (key sync) or 2 (continuous free running)')
    if ops is not None:
        for name, mask in zip(LFO_FIELDS[8:11], values[8:11]):
            if mask >= 1 << ops:
                raise ValueError('lfo %s %d names an operator the %d-operator patch lacks' % (name, mask, ops))
    return tuple(values)


def lfo_row(name, *values):
    """An LFO statement in the harness's integer units, preserving omitted phase/masks."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
        raise ValueError('an lfo patch name is 1 to 31 of A-Z a-z 0-9 _ - .')
    return 'lfo %s %s' % (name, ' '.join(map(str, lfo_values(values))))


def peg_values(values, ops=None):
    """Validate eight PEG level/time fields and an optional mask .
    With ops known, resolve an omitted mask and check it names only present operators."""
    if len(values) not in (8, 9):
        raise ValueError('peg takes L1 T1 L2 T2 L3 T3 L4 T4 and an optional MASK')
    for i, value in enumerate(values[:8]):
        lo, hi = (-4800, 4800) if i % 2 == 0 else (0, 60000)
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError('peg %s%d %s is not a whole number from %d to %d' %
                             ('L' if i % 2 == 0 else 'T', i // 2 + 1, value, lo, hi))
    if len(values) == 9 and (type(values[8]) is not int or not 1 <= values[8] <= 15):
        raise ValueError('peg MASK %s is not a whole number from 1 to 15' % values[8])
    if ops is not None:
        if type(ops) is not int or not 2 <= ops <= 4:
            raise ValueError('peg needs 2 to 4 operators')
        if len(values) == 8:
            values = tuple(values) + ((1 << ops) - 1,)
        elif values[8] >= 1 << ops:
            raise ValueError('peg MASK %d names an operator the %d-operator patch lacks' % (values[8], ops))
    return tuple(values)


def peg_row(name, *values, ops=2):
    """A PEG statement in raw integer units, always with its resolved operator mask."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
        raise ValueError('a peg patch name is 1 to 31 of A-Z a-z 0-9 _ - .')
    return 'peg %s %s' % (name, ' '.join(map(str, peg_values(values, ops))))


FENV_FIELDS = ('ATTACK_MS', 'DECAY_MS', 'SUSTAIN_PERMILLE', 'RELEASE_MS',
               'DEPTH_CENTS', 'TRACK_PERMILLE', 'REF_MILLIHZ')
FENV_LIMITS = ((0, 60000), (0, 60000), (0, 1000), (0, 60000),
               (-9600, 9600), (0, 2000), (1, 4294967295))


def fenv_values(values):
    """Validate six or seven raw filter-envelope fields and resolve the reference ."""
    if len(values) not in (6, 7):
        raise ValueError('fenv takes ATTACK_MS DECAY_MS SUSTAIN_PERMILLE RELEASE_MS '
                         'DEPTH_CENTS TRACK_PERMILLE and an optional REF_MILLIHZ')
    for name, (lo, hi), value in zip(FENV_FIELDS, FENV_LIMITS, values):
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError('fenv %s %s is not a whole number from %d to %d' % (name, value, lo, hi))
    return tuple(values) + ((261626,) if len(values) == 6 else ())


def fenv_row(name, *values):
    """A filter-envelope statement in raw integer units, always with its reference."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
        raise ValueError('an fenv patch name is 1 to 31 of A-Z a-z 0-9 _ - .')
    return 'fenv %s %s' % (name, ' '.join(map(str, fenv_values(values))))


def eg_values(values, ops=None, sustain1=None):
    """Validate filter's OP DELAY_MS DECAY2_MS SUSTAIN2_DB10 in raw integer units."""
    if len(values) != 4:
        raise ValueError('eg takes OP DELAY_MS DECAY2_MS SUSTAIN2_DB10')
    for name, value, hi in zip(('OP', 'DELAY_MS', 'DECAY2_MS', 'SUSTAIN2_DB10'), values, (3, 60000, 60000, 1200)):
        if type(value) is not int or not 0 <= value <= hi:
            raise ValueError('eg %s %s is not a whole number from 0 to %d' % (name, value, hi))
    if ops is not None and values[0] >= ops:
        raise ValueError('eg OP %d names an operator the %d-operator patch lacks' % (values[0], ops))
    if sustain1 is not None and any(values[2:]) and values[3] < min(1200, max(0, sustain1)):
        raise ValueError('eg SUSTAIN2_DB10 %d is below the operator sustain %d' % (values[3], min(1200, max(0, sustain1))))
    return tuple(values)


def eg_row(name, *values):
    """An operator envelope extension; writers omit an all-zero extension."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
        raise ValueError('an eg patch name is 1 to 31 of A-Z a-z 0-9 _ - .')
    return 'eg %s %s' % (name, ' '.join(map(str, eg_values(values))))


def ks_values(values, ops=None, routing=None):
    """Validate filter's OP LO_SLOPE_DB10: the index slope below the operator's break."""
    if len(values) != 2:
        raise ValueError('ks takes OP LO_SLOPE_DB10')
    for name, value, hi in zip(('OP', 'LO_SLOPE_DB10'), values, (3, 1200)):
        if type(value) is not int or not 0 <= value <= hi:
            raise ValueError('ks %s %s is not a whole number from 0 to %d' % (name, value, hi))
    if ops is not None and values[0] >= ops:
        raise ValueError('ks OP %d names an operator the %d-operator patch lacks' % (values[0], ops))
    if values[1] and routing and any(kind != 'pm' for _, _, _, kind in map(edge_values, routing['edges'])):
        if not any(f == values[0] and kind == 'pm' for f, _, _, kind in map(edge_values, routing['edges'])):
            raise ValueError('ks OP %d has no outgoing PM edge in the amplitude route' % values[0])
    return tuple(values)


def ks_row(name, *values):
    """A left index-slope extension; writers omit zero slope."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
        raise ValueError('a ks patch name is 1 to 31 of A-Z a-z 0-9 _ - .')
    return 'ks %s %s' % (name, ' '.join(map(str, ks_values(values))))


def noise_values(values, ops=None, wave=None):
    """Validate operator extension OP SHIFT; a present statement requires a coloured-noise operator."""
    if len(values) != 2:
        raise ValueError('noise takes OP SHIFT')
    for name, value, lo, hi in zip(('OP', 'SHIFT'), values, (0, 1), (3, 15)):
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError('noise %s %s is not a whole number from %d to %d' % (name, value, lo, hi))
    if ops is not None and values[0] >= ops:
        raise ValueError('noise OP %d names an operator the %d-operator patch lacks' % (values[0], ops))
    if wave is not None and wave not in (10, 11):
        raise ValueError('noise OP %d needs wave 10 or 11, not %s' % (values[0], wave))
    return tuple(values)


def noise_row(name, *values):
    """A coloured-noise shift; canonical writers omit the default SHIFT 4."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
        raise ValueError('a noise patch name is 1 to 31 of A-Z a-z 0-9 _ - .')
    return 'noise %s %s' % (name, ' '.join(map(str, noise_values(values))))


def formant_values(values):
    """Enabled formant slot in raw mHz, Q/1000 and attenuation dB/10."""
    if len(values) != 4 or any(type(v) is not int for v in values):
        raise ValueError('formant takes integer SLOT CENTER_MILLIHZ Q_MILLI LEVEL_DB10')
    slot, center, q, level = values
    if not 0 <= slot < 3 or not 1 <= center <= 4294967295 or not 500 <= q <= 16000 or not 0 <= level <= 1200:
        raise ValueError('formant needs SLOT 0..2, CENTER 1..4294967295, Q 500..16000 and LEVEL 0..1200')
    return tuple(values)


def formant_row(name, *values):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
        raise ValueError('invalid formant patch name')
    return 'formant %s %d %d %d %d' % (name, *formant_values(values))


# /: host harmonic descriptions; samples and hashes stay native.
HARMONIC_FORMS = ('bl-saw', 'bl-square', 'bl-triangle')


def wtbank_values(bank, frames, length, harmonics):
    if not isinstance(bank, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', bank):
        raise ValueError('wavetable bank name needs 1..31 ASCII letters, digits, _ . -')
    if type(frames) is not int or not 1 <= frames <= 8:
        raise ValueError('wavetable %s: frames must be an integer 1..8' % bank)
    if type(length) is not int or not 32 <= length <= 2048 or length & (length-1):
        raise ValueError('wavetable %s: length must be a power of two from 32..2048' % bank)
    if not isinstance(harmonics, (list, tuple)) or not harmonics:
        raise ValueError('wavetable %s: harmonics must be a nonempty list or tuple' % bank)
    rows, seen = [], set()
    for row in harmonics:
        if not isinstance(row, (list, tuple)) or len(row) != 4 or any(type(x) is not int for x in row):
            raise ValueError('wavetable %s: each harmonic needs four integers' % bank)
        frame, number, level, phase = row
        if not 0 <= frame < frames or not 1 <= number <= 1024 or not 0 <= level <= 1200 or not 0 <= phase <= 65535:
            raise ValueError('wavetable %s: harmonic frame/number/level/phase out of range' % bank)
        if (frame, number) in seen:
            raise ValueError('wavetable %s: duplicate harmonic (%d, %d)' % (bank, frame, number))
        seen.add((frame, number)); rows.append(tuple(row))
    return frames, length, tuple(sorted(rows))


def harmonic_form(form, *, length=2048):
    """One-frame band-limited saw/square/triangle harmonic descriptions .
    Decimal raw levels and phases feed the native bank generator, never a Python
    sample/hash clone. This shapes timbre before placement; no pos/move changes.
    TX81Z wave codes remain refused: the cited manual is not an exact shape law.
    """
    if form not in HARMONIC_FORMS:
        raise ValueError('unknown harmonic form %r' % (form,))
    wtbank_values(form, 1, length, ((0, 1, 0, 0),))
    triangle = form == 'bl-triangle'
    stop = min(length//2, 1001) if triangle else length//2
    with decimal.localcontext(decimal.Context(prec=50, rounding=decimal.ROUND_HALF_UP)):
        return tuple((0, h, int((decimal.Decimal(400 if triangle else 200) * decimal.Decimal(h).log10()).to_integral_value()),
                      32768 if triangle and (h//2) % 2 else 0)
                     for h in range(1, stop, 1 if form == 'bl-saw' else 2))


def fmx_harmonics(kind, *, skirt, resonance=0, length=2048):
    """'s own All/Odd/Res harmonic law, FM-X-like, not Yamaha's.
    Skirt sets width2**skirt; Res peaks at resonance+1. Decimal levels feed
    the native harmonic bank writer. This changes no spatial position.
    """
    if kind not in ('all', 'odd', 'res'):
        raise ValueError('fmx kind must be all, odd or res')
    if type(skirt) is not int or not 0 <= skirt <= 7:
        raise ValueError('fmx skirt must be an integer 0..7')
    if type(resonance) is not int or not 0 <= resonance <= 99:
        raise ValueError('fmx resonance must be an integer 0..99')
    if kind != 'res' and resonance:
        raise ValueError('fmx resonance is only available for res')
    wtbank_values('fmx-'+kind, 1, length, ((0,1,0,0),))
    width, top, center = 1 << skirt, length//2-1, resonance+1
    if kind == 'res' and center > top:
        raise ValueError('fmx res center exceeds the bank harmonic limit')
    if kind == 'all':
        harmonics = range(1, min(width, top)+1)
    elif kind == 'odd':
        harmonics = range(1, min(2*width-1, top)+1, 2)
    else:
        harmonics = range(max(1, center-width+1), min(top, center+width-1)+1)
    with decimal.localcontext(decimal.Context(prec=50, rounding=decimal.ROUND_HALF_UP)):
        return tuple((0, h, int((decimal.Decimal(200)*decimal.Decimal(
            1+abs(h-center) if kind == 'res' else h).log10()).to_integral_value()), 0)
                     for h in harmonics)


RECIPE_WT_FORMS = HARMONIC_FORMS + ('fmx-all', 'fmx-odd', 'fmx-res')


def recipe_harmonics(form):
    if form in HARMONIC_FORMS:
        return harmonic_form(form)
    if form not in RECIPE_WT_FORMS:
        raise ValueError('unknown harmonic form %r' % (form,))
    kind = form[4:]
    return fmx_harmonics(kind, skirt=3, resonance=4 if kind == 'res' else 0)


def wtbank_rows(bank, definition):
    frames, length, harmonics = wtbank_values(bank, *definition)
    return ['wtbank %s %d %d 12' % (bank, frames, length)] + [
        'harmonic %s %d %d %d %d' % (bank, *row) for row in harmonics]


def wtuse_values(patch, operator, bank, position, operators, extra, banks):
    if not isinstance(patch, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', patch):
        raise ValueError('wtuse needs a native patch name')
    if type(operator) is not int or not 0 <= operator < min(len(operators), extra.get('ops', len(operators))):
        raise ValueError('patch %s: wtuse operator must be a present integer operator' % patch)
    if not isinstance(bank, str) or bank not in banks:
        raise ValueError('patch %s: wtuse needs a declared bank %r' % (patch, bank))
    if type(position) is not int or not 0 <= position <= 32768:
        raise ValueError('patch %s: wtuse position must be an integer 0..32768' % patch)
    op = operators[operator]
    if len(op) > 11 and op[11] != 0:
        raise ValueError('patch %s operator %d: wtuse needs sine wave 0' % (patch, operator))
    for kind in ('pd', 'ksource'):
        if operator in extra.get(kind, {}):
            raise ValueError('patch %s operator %d: wtuse conflicts with %s' % (patch, operator, kind))
    return bank, position


def wt_metadata(patch, operators, extra):
    banks, uses = {}, {}
    if not isinstance(extra.get('wtbanks', {}), dict) or not isinstance(extra.get('wtuse', {}), dict):
        raise ValueError('patch %s: wavetable metadata needs bank and operator maps' % patch)
    for bank, fields in extra.get('wtbanks', {}).items():
        if not isinstance(fields, (tuple, list)) or len(fields) != 3:
            raise ValueError('patch %s: invalid wavetable definition' % patch)
        banks[bank] = wtbank_values(bank, *fields)
    if len(banks) > 8:
        raise ValueError('patch %s: at most 8 wavetable banks' % patch)
    for i, fields in extra.get('wtuse', {}).items():
        if not isinstance(fields, (tuple, list)) or len(fields) != 2:
            raise ValueError('patch %s: invalid wtuse binding' % patch)
        uses[i] = wtuse_values(patch, i, *fields, operators, extra, banks)
    return banks, uses


def ksource_values(patch, operator, loss, seed, buffer_samples, operators, extra):
    """Validate the existing native source binding, in raw units ."""
    if not isinstance(patch, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', patch):
        raise ValueError('ksource needs a native patch name')
    ops = extra.get('ops', len(operators))
    if type(operator) is not int or not 0 <= operator < min(len(operators), ops):
        raise ValueError('patch %s: ksource operator must be a present integer operator' % patch)
    for label, value, low, high in (('LOSS_Q15', loss, 0, 32767),
                                   ('SEED', seed, 0, 4294967295),
                                   ('BUFFER_SAMPLES', buffer_samples, 64, 8192)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('ksource %s must be an integer %d..%d' % (label, low, high))
    if buffer_samples & (buffer_samples - 1):
        raise ValueError('ksource BUFFER_SAMPLES must be a power of two')
    op = operators[operator]
    if len(op) > 11 and op[11] != 0:
        raise ValueError('patch %s operator %d: ksource needs sine wave 0' % (patch, operator))
    for kind in ('pd', 'wtuse'):
        if operator in extra.get(kind, {}):
            raise ValueError('patch %s operator %d: ksource conflicts with %s' % (patch, operator, kind))
    if op[3]:
        raise ValueError('patch %s operator %d: ksource takes no feedback' % (patch, operator))
    if 'ops' not in extra and operator == 0 and operators[1][2]:
        raise ValueError('patch %s: ksource operator 0 needs operator 1 index zero' % patch)
    for source, destination, _, kind in map(edge_values, extra.get('edges', ())):
        if destination == operator:
            raise ValueError('patch %s operator %d: ksource takes no incoming route edge' % (patch, operator))
        if source == operator and kind == 'sync':
            raise ValueError('patch %s operator %d: ksource drives no sync edge' % (patch, operator))
    return loss, seed, buffer_samples


def vector_values(values, ops=None, carriers=None):
    """Static four-carrier coordinates; (0,0) remains an enabled vector patch."""
    if len(values) != 2 or any(type(v) is not int or not 0 <= v <= 32768 for v in values):
        raise ValueError('vector takes integer X and Y from 0 to 32768')
    if ops is not None and (ops != 4 or carriers != 15):
        raise ValueError('vector needs a routed four-operator patch with carrier mask 15')
    return tuple(values)


def vector_row(name, *values):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,30}',name):
        raise ValueError('invalid vector patch name')
    return 'vector %s %d %d' % (name,*vector_values(values))


def pd_values(values, ops=None, wave=None):
    """Enabled sine phase distortion; identity 16384 remains an explicit setting."""
    if len(values) != 2:
        raise ValueError('pd takes OP KNEE')
    for value,name,low,high in zip(values,('OP','KNEE'),(0,1),(3,32767)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('pd %s must be an integer from %d to %d' % (name,low,high))
    if ops is not None and values[0] >= ops:
        raise ValueError('pd names an absent operator')
    if wave is not None and wave != 0:
        raise ValueError('pd needs a sine operator')
    return tuple(values)


def pd_row(name, *values):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,30}',name):
        raise ValueError('invalid pd patch name')
    return 'pd %s %d %d' % (name,*pd_values(values))


def shape_values(values):
    """Validate DRIVE_DB10 KIND [pre]; absence alone bypasses the shaper."""
    if len(values) not in (2, 3) or values[1] not in ('hard', 'cubic', 'fold', 'asym') or (
            len(values) == 3 and values[2] != 'pre'):
        raise ValueError('shape takes DRIVE_DB10 KIND [pre], KIND hard, cubic, fold or asym')
    if type(values[0]) is not int or not 0 <= values[0] <= 240:
        raise ValueError('shape DRIVE_DB10 is a whole number from 0 to 240')
    return tuple(values)


def shape_row(name, *values):
    """A shaper statement, optionally before the formant/filter; only absence is bypass."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
        raise ValueError('a shape patch name is 1 to 31 of A-Z a-z 0-9 _ - .')
    fields = shape_values(values)
    return 'shape %s %d %s' % (name, *fields[:2]) + (' pre' if len(fields) == 3 else '')


def amp_values(values):
    """: every explicit four-field tuple enables the amp, including all zero."""
    if len(values) != 4 or any(type(v) is not int or not 0 <= v <= 65535 for v in values):
        raise ValueError('amp takes four integers 0 to 65535: ATTACK_MS DECAY_MS SUSTAIN_DB10 RELEASE_MS')
    return tuple(values)


def amp_row(name, *values):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
        raise ValueError('invalid amp patch name')
    return 'amp %s %d %d %d %d' % (name, *amp_values(values))


def op_ext_values(kind, values, ops=4):
    """/ fields; defaults stay valid inputs and are omitted by the row writer."""
    count = {'kstype': 4, 'fbtype': 2, 'egloop': 1}[kind]
    if len(values) != count or type(values[0]) is not int or not 0 <= values[0] < ops:
        raise ValueError('%s needs an existing integer operator and %d fields' % (kind, count - 1))
    if kind == 'kstype' and (values[1] not in ('index', 'level', 'both') or
                             values[2] not in ('-', '+') or values[3] not in ('-', '+')):
        raise ValueError('kstype takes OP index|level|both RIGHT LEFT, each side - or +')
    if kind == 'fbtype' and values[1] not in ('classic', 'pre', 'square', 'presquare'):
        raise ValueError('fbtype takes OP classic|pre|square|presquare')
    return tuple(values)


def op_ext_row(kind, name, *values, ops=4):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
        raise ValueError('invalid %s patch name' % kind)
    fields = op_ext_values(kind, values, ops)
    if (kind == 'kstype' and fields[1:] == ('index', '-', '-')) or (
            kind == 'fbtype' and fields[1:] == ('classic',)):
        return ''
    return ' '.join((kind, name, *(str(v) for v in fields)))


def filter_values(values):
    """Validate raw filter fields; optional stage 1 normalizes to the legacy triple."""
    if len(values) not in (3, 4) or values[0] not in ('lp', 'bp', 'hp', 'notch'):
        raise ValueError('filter takes MODE CUTOFF_MILLIHZ Q_MILLI [STAGES], MODE lp, bp, hp or notch')
    for name, value, hi in zip(('CUTOFF_MILLIHZ', 'Q_MILLI'), values[1:3], (4294967295, 65535)):
        if type(value) is not int or not 0 <= value <= hi:
            raise ValueError('filter %s %s is not a whole number from 0 to %d' % (name, value, hi))
    stages = values[3] if len(values) == 4 else 1
    if type(stages) is not int or stages not in (1, 2):
        raise ValueError('filter STAGES is 1 or 2')
    return tuple(values[:3]) + ((2,) if stages == 2 else ())


def filter_row(name, *values):
    """Keep old raw filter formatting; only a second stage appends the literal x2."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
        raise ValueError('a filter patch name is 1 to 31 of A-Z a-z 0-9 _ - .')
    fields = filter_values(values)
    return 'filter %-8s %-5s %8d %5d' % (name, *fields[:3]) + (' x2' if len(fields) == 4 else '')


# ---------------------------------------------------------------------------
# Recipes: named patches to start from, read once from recipes.txt beside this
# file . Each is the op arguments of its carrier and its modulator, and
# a note that ends with the piece that tested it. recipe gives one as it is
# or moved by deltas, and a timeline's recipe statement expands to its patch
# lines (expand_timeline); op still writes any patch at all.
RECIPE_FILE = os.path.join(DATA, 'recipes.txt')


def shown(path):
    """path as an error message gives it: from the working directory when it lies below it."""
    rel = os.path.relpath(path)
    return path if rel.startswith('..') else rel


def edge_values(edge, ops=None):
    """Normalize legacy PM triples or operator extension and  kind-bearing entries to (FROM, TO, WEIGHT, KIND)."""
    if len(edge) not in (3, 4):
        raise ValueError('a route edge takes FROM, TO, WEIGHT and an optional kind')
    f, t, w = edge[:3]
    kind = edge[3] if len(edge) == 4 else 'pm'
    if kind not in ('pm', 'am', 'ring', 'sync'):
        raise ValueError('route edge kind must be pm, am, ring or sync')
    if any(type(i) is not int or not 0 <= i < 4 for i in (f, t)):
        raise ValueError('route edge operators must be integers from 0 to 3')
    if ops is not None and (f >= ops or t >= ops):
        raise ValueError('route edge %d>%d names an operator at or above OPS %d' % (f, t, ops))
    if f == t:
        raise ValueError('route edge %d>%d feeds an operator to itself, which its fb does' % (f, t))
    if kind == 'sync' and (type(w) is not int or w != 256):
        raise ValueError('route edge %d>%d: a sync WEIGHT is 256' % (f, t))
    bound = 1024 if kind == 'pm' else 256
    if type(w) is not int or not 1 <= w <= bound:
        raise ValueError('route edge %d>%d: WEIGHT is 1 to %d, in 1/256 (256 is unity)' % (f, t, bound))
    return f, t, w, kind


def edge_text(edge, ops=None):
    """Canonical route token; PM keeps its original bytes without a suffix."""
    f, t, w, kind = edge_values(edge, ops)
    return '%d>%d:%d' % (f, t, w) + (':' + kind if kind != 'pm' else '')


MOD_AMOUNTS = {'pitch': 4800, 'level': 1200, 'index': 65535, 'cutoff': 9600,
               'q': 15500, 'lfo_rate': 49990, 'lfo_pitch': 1200,
               'lfo_level': 1200, 'lfo_index': 65535, 'pd_knee': 32767,
               'vector_x': 32768, 'vector_y': 32768,
               'formant_center': 2147483647, 'formant_gain': 1200, 'wt_pos': 32768}
MOD_SOURCES = ('lfo', 'peg', 'fenv', 'vel', 'note') + tuple('eg%d' % i for i in range(4)) + tuple('ctl%d' % i for i in range(8))


GEN_Q48 = 1 << 48


def load_gen_descriptors(path):
    """Read the shared host X-macro data as literals, without eval or a native build."""
    with open(path, encoding='utf-8') as f:
        source = re.sub(r'/\*.*?\*/','',f.read(),flags=re.S)
    kinds,rows,ids = {},[],set()
    for line in source.splitlines():
        line = line.strip()
        if not line: continue
        kind = re.fullmatch(r'GEN_KIND\(\s*([0-9]+)\s*,\s*([a-z_]+)\s*\)',line)
        if kind:
            id,name = int(kind[1]),kind[2]
            if not 1 <= id <= 16 or name in kinds or id in kinds.values():
                raise ValueError('invalid duplicate generator kind descriptor')
            kinds[name] = id
            continue
        row = re.fullmatch(r'GEN_PARAM\(([^()]*)\)',line)
        if not row:
            raise ValueError('invalid generator descriptor line')
        fields = [field.strip() for field in row[1].split(',')]
        if len(fields) != 11 or any(not re.fullmatch(r'-?[0-9]+',fields[i]) for i in (0,2,5,6,8,9,10)) or any(
                not re.fullmatch(r'[a-z][a-z0-9_]*',fields[i]) for i in (1,3,4,7)):
            raise ValueError('generator descriptors require literal integers and identifiers')
        id,name,mask,slot,type_,low,high,unit,mutable,has_default,default = fields
        row = dict(id=int(id),name=name,mask=int(mask),slot=slot,type=type_,low=int(low),high=int(high),
                   unit=unit,mutable=int(mutable),has_default=int(has_default),default=int(default))
        if row['id'] in ids or row['low'] > row['high'] or row['mutable'] not in (0,1) or row['has_default'] not in (0,1):
            raise ValueError('invalid generator descriptor bounds/flags/ID')
        if row['has_default'] and not row['low'] <= row['default'] <= row['high']:
            raise ValueError('generator descriptor default is outside its bounds')
        ids.add(row['id']);rows.append(row)
    masks = sum(1 << (id-1) for id in kinds.values())
    if not kinds or any(row['mask'] < 0 or row['mask'] & ~masks for row in rows):
        raise ValueError('generator descriptor refers to an unknown kind')
    for kind,id in kinds.items():
        active = [r for r in rows if r['mask'] & (1 << (id-1))]
        if len({r['name'] for r in active}) != len(active) or len({r['slot'] for r in active}) != len(active):
            raise ValueError('duplicate generator parameter descriptor for '+kind)
    return kinds,tuple(rows)


GEN_KINDS,GEN_DESCRIPTORS = load_gen_descriptors(os.path.join(SPIKE,'src','gen','params.def'))
GEN_PARAMS = {kind:{r['name']:r for r in GEN_DESCRIPTORS if r['mask'] & (1 << (id-1))}
              for kind,id in GEN_KINDS.items()}
GEN_META = {r['name']:r for r in GEN_DESCRIPTORS if not r['mask']}
# Complete scalar fields ordered by their core p[] slot; all writer consumers share the table.
GEN_FIELDS = {kind:tuple((r['name'],r['default'],r['low'],r['high']) for r in
                        sorted(rows.values(),key=lambda r:r['slot']) if re.fullmatch(r'p[0-3]',r['slot']))
              for kind,rows in GEN_PARAMS.items()}
GEN_MUTABLE = {kind:tuple(name for name,_,_,_ in fields if GEN_PARAMS[kind][name]['mutable'])
               for kind,fields in GEN_FIELDS.items()}


def gen_int(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('%s must be an integer from %d to %d' % (field,low,high))
    return value


def gen_bound(value, field, kind=None):
    row = GEN_META[field] if kind is None else GEN_PARAMS[kind][field]
    return gen_int(value,row['low'],row['high'],'gen '+field)


def gen_word(word, field):
    """Validate public A..H text using the shared descriptor's length bounds."""
    row = GEN_META[field]
    value = '' if word == '-' and row['low'] == 0 else word
    if not isinstance(value,str) or not row['low'] <= len(value) <= row['high'] or (
            value and not re.fullmatch(r'[A-H]+',value)) or word == '':
        raise ValueError('gen %s needs %d..%d symbols A..H%s' %
                         (field,row['low'],row['high'],' (use - for deletion)' if row['low'] == 0 else ''))
    return word


def gen_ref(source):
    """Public modulation matrix bank reference, or None for another source spelling."""
    match = re.fullmatch(r'gen\.([0-9]+)\.([xy])', source) if isinstance(source,str) else None
    if match and GEN_META['source_id']['low'] <= int(match[1]) <= GEN_META['source_id']['high']:
        return int(match[1]), match[2]
    return None


def gen_values(id, kind, seed, div=1, slew=0, **fields):
    """A source header's complete scalar config; C owns recurrence and text expansion."""
    gen_bound(id,'source_id')
    if not isinstance(kind,str) or kind not in GEN_FIELDS:
        raise ValueError('unknown or unimplemented generator kind %r' % kind)
    gen_bound(seed,'seed',kind)
    gen_bound(div,'div',kind); gen_bound(slew,'slew',kind)
    accepted = {name for name,_,_,_ in GEN_FIELDS[kind]}
    if kind == 'euclid': accepted.add('parent')
    if set(fields) - accepted:
        raise ValueError('gen %s has no fields %s' % (kind,','.join(sorted(set(fields)-accepted))))
    values = tuple(gen_int(fields.get(name,default),low,high,'gen '+name) for name,default,low,high in GEN_FIELDS[kind])
    if kind == 'euclid' and (values[0] > values[1] or values[2] >= values[1]):
        raise ValueError('gen euclid needs K <= N and ROT < N')
    if kind in ('thue','cantor') and values[1] >= values[0]:
        raise ValueError('gen %s needs ROT < LENGTH' % kind)
    if kind == 'multi' and values[0] * values[1] ** (values[2]-1) > 4294967295:
        raise ValueError('gen multi longest period exceeds uint32')
    parent = fields.get('parent')
    if parent is not None:
        gen_bound(parent,'parent')
        if parent < 0 or parent >= id:
            raise ValueError('gen parent must be a smaller source ID; use None for absence')
    return kind, seed, div, slew, values, parent


def gen_row(id, config):
    kind,seed,div,slew,values,parent = config
    return 'gen %d %s %d %d %d' % (id,kind,seed,div,slew) + ''.join(' %d' % v for v in values) + (
        (' -' if parent is None else ' %d' % parent) if kind == 'euclid' else '')


def gen_lane_values(id, voice, gate, pitch, accent, step_ticks, base_cents, span_cents, velocity, gate_ticks, scale_mask):
    gen_bound(id,'lane_id'); gen_bound(voice,'voice')
    for value in (gate,pitch,accent):
        if value is not None:
            gen_bound(value,'lane_source')
            if value < 0: raise ValueError('use None for an absent lane source')
    for name,value in (('step_ticks',step_ticks),('gate_ticks',gate_ticks),('base_cents',base_cents),
                       ('span_cents',span_cents),('velocity',velocity),('scale_mask',scale_mask)):
        gen_bound(value,name)
    if gate_ticks > step_ticks:
        raise ValueError('gen_lane GATE_TICKS must not exceed STEP_TICKS')
    if base_cents-span_cents-(1199 if scale_mask else 0) < -4800 or base_cents+span_cents > 14400:
        raise ValueError('gen_lane raw and quantized pitch extrema must stay in -4800..14400 cents')
    return voice,gate,pitch,accent,step_ticks,base_cents,span_cents,velocity,gate_ticks,scale_mask


def gen_lane_row(id, values):
    return 'gen_lane %d %s' % (id,' '.join('-' if v is None else str(v) for v in values))


def gen_param_value(kind, param, value):
    """Static event bounds; C validates the ordered complete pending configuration."""
    if not isinstance(param,str):
        raise ValueError('generator parameter must be a name')
    row = GEN_PARAMS.get(kind,{}).get(param)
    if row is None or not row['mutable']:
        raise ValueError('gen %s parameter %r is unknown or immutable' % (kind,param))
    return gen_bound(value,param,kind)


def mod_values(values):
    """One modulation matrix slot in raw units, with an explicit smoothing shift (default 3)."""
    if len(values) not in (4, 5):
        raise ValueError('mod takes SLOT SOURCE TARGET AMOUNT [SHIFT]')
    slot, source, target, amount = values[:4]
    shift = values[4] if len(values) == 5 else 3
    if type(slot) is not int or not 0 <= slot < 8:
        raise ValueError('mod SLOT must be an integer from 0 to 7')
    bank_ref = gen_ref(source)
    if source not in MOD_SOURCES and bank_ref is None:
        raise ValueError('mod source must be lfo, peg, fenv, eg0..3, vel, note, ctl0..7 or gen.ID.x/y')
    if bank_ref is not None:
        source = 'gen.%d.%s' % bank_ref
    match = re.fullmatch(r'(pitch|level|index|pd_knee|wt_pos)([0-3])', target) if isinstance(target, str) else None
    formant = re.fullmatch(r'(formant_center|formant_gain)([0-2])', target) if isinstance(target, str) else None
    family = match[1] if match else formant[1] if formant else target
    if not isinstance(family, str) or family not in MOD_AMOUNTS or (family in ('pitch', 'level', 'index', 'pd_knee', 'wt_pos') and not match) or (family in ('formant_center', 'formant_gain') and not formant):
        raise ValueError('mod target must be pitchN, levelN, indexN, pd_kneeN, wt_posN, vector_x/y, formant_centerN, formant_gainN, cutoff, q or an lfo parameter')
    bound = MOD_AMOUNTS[family]
    if type(amount) is not int or not -bound <= amount <= bound:
        raise ValueError('mod %s AMOUNT must be an integer from %d to %d' % (target, -bound, bound))
    if type(shift) is not int or not 0 <= shift <= 15:
        raise ValueError('mod SHIFT must be an integer from 0 to 15')
    return slot, source, target, amount, shift


def mod_settings(settings, extra, fs=None):
    """Validate final patch features, including zero slots; rate bounds need the final fs."""
    if not settings:
        return {}
    out = {}
    ops, carriers = extra.get('ops', 2), extra.get('carriers', 1)
    pm = {f for f, _, _, kind in map(edge_values, extra['edges']) if kind == 'pm'} if 'ops' in extra else {1}
    lfo = extra.get('lfo', ())
    peg = extra.get('peg', ())
    fenv = extra.get('fenv', ())
    for slot, fields in settings.items():
        _, source, target, amount, shift = mod_values((slot, *fields))
        if source.startswith('eg') and int(source[2:]) >= ops:
            raise ValueError('mod source %s needs a present operator' % source)
        if source == 'lfo' or target.startswith('lfo_'):
            if not lfo or not lfo[1]:
                raise ValueError('mod %s needs a real LFO with nonzero rate' % (source if source == 'lfo' else target))
        if source == 'peg' and not any(peg[i] for i in (0, 2, 4, 6) if i < len(peg)):
            raise ValueError('mod peg source needs a nonzero PEG level')
        if source == 'fenv' and ('filter' not in extra or not fenv or not (fenv[4] or fenv[5])):
            raise ValueError('mod fenv source needs a filter and nonzero depth or tracking')
        if target in ('cutoff', 'q') and 'filter' not in extra:
            raise ValueError('mod %s target needs a filter' % target)
        if target in ('vector_x', 'vector_y'):
            if 'vector' not in extra:
                raise ValueError('mod %s target needs enabled vector' % target)
            vector_values(extra['vector'],ops,carriers)
        formant = re.fullmatch(r'(formant_center|formant_gain)([0-2])', target)
        if formant:
            i = int(formant[2])
            if i not in extra.get('formant', {}):
                raise ValueError('mod %s target needs an enabled formant slot' % target)
            formant_values((i, *extra['formant'][i]))
        match = re.fullmatch(r'(pitch|level|index|pd_knee|wt_pos)([0-3])', target)
        if match:
            family, i = match[1], int(match[2])
            if i >= ops:
                raise ValueError('mod target %s needs a present operator' % target)
            if family == 'pd_knee' and i not in extra.get('pd', {}):
                raise ValueError('mod target %s needs enabled sine pd' % target)
            if family == 'wt_pos' and i not in extra.get('wtuse', {}):
                raise ValueError('mod target %s needs an enabled wavetable binding' % target)
            if family == 'level' and not (carriers >> i & 1):
                raise ValueError('mod target %s needs a carrier' % target)
            if family == 'index' and i not in pm:
                raise ValueError('mod target %s needs an outgoing PM edge' % target)
        out[slot] = (source, target, amount, shift)
    rates = [abs(amount) for _, target, amount, _ in out.values() if target == 'lfo_rate' and amount]
    if fs is not None and rates:
        upper = min(50000, min(50000, max(10, lfo[1])) + sum(rates))
        if upper * 64 >= 500 * fs:
            raise ValueError('mod lfo_rate reachable upper bound violates the control-rate Nyquist')
    return out


def mod_row(name, *values):
    """Canonical matrix row: preserve a zero amount and spell out the shift."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
        raise ValueError('mod patch name is invalid')
    return 'mod %s %d %s %s %d %d' % (name, *mod_values(values))


def ordered_events(events):
    """Ctl/gen/lane opts its stream/time group into call order; old groups keep their bytes.
    A keys-generated position shares its note's sequence and precedes that note."""
    ctl_times = {e[0] for e in events if e[4].startswith(('ctl ','gen ','lane '))}
    return sorted(events, key=lambda e: (e[0], e[2], e[1]) if e[0] in ctl_times else e)


def macro_knob(k, bipolar=False, curve=None):
    """Map one integer knob through one optional integer-Q15 host curve ."""
    if type(k) is not int or not 0 <= k <= 127 or type(bipolar) is not bool:
        raise ValueError('macro knob needs integer 0..127 and boolean bipolar')
    if curve is not None and not callable(curve):
        raise ValueError('macro curve must be one callable on the integer Q15 value')
    q = ((k - 64) * 32768 + 32) // 64 if bipolar else (k * 32768 + 63) // 127
    q = curve(q) if curve is not None else q
    if type(q) is not int or not (-32768 if bipolar else 0) <= q <= 32768:
        raise ValueError('macro curve must return an integer in its Q15 range')
    return q


def _macro_name(name):
    if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 or c == '#' for c in name):
        raise ValueError('macro display name must be nonempty text without controls or #')
    return name


def _macro_target(target, operators, extra):
    """Reject targets which cannot affect this patch, including fixed or muted ops."""
    if target == 'lfo_pitch' and 'lfo' in extra:
        lfo = extra['lfo']
        mask = lfo[8] if len(lfo) >= 11 else (1 << len(operators)) - 1
        if not any(mask >> i & 1 and i not in extra.get('muted', ()) and not (len(o) > 13 and o[13])
                   for i, o in enumerate(operators)):
            raise ValueError('macro target lfo_pitch changes nothing: no relative operator in its mask')
    m = re.fullmatch(r'(pitch|level|index)([0-3])', target)
    if m:
        family, i = m[1], int(m[2])
        if i >= len(operators):
            raise ValueError('macro target %s needs a present operator' % target)
        side = ('c', 'm', 'o2', 'o3')[i]
        why = ignored(side, 'ratio' if family == 'pitch' else family, extra)
        if why:
            raise ValueError('macro target %s changes nothing; %s' % (target, why))
        if family == 'pitch' and len(operators[i]) > 13 and operators[i][13]:
            raise ValueError('macro target %s changes nothing on a fixed-frequency operator' % target)


def macro_budget(settings, definitions, additions, operators, extra, fs=None):
    """Pure, shared preflight for all eight slots and inputs; publish only its result.
    A raw ctl route reserves its input. A named macro may own several slots on
    its one input; explicit ctl events/locks then control that same input.
    Names, raw routes, controllers and later crossfades must share this check.
    """
    mods = mod_settings(settings, extra, fs)
    macros = {name: dict(value) for name, value in definitions.items()}
    owned, inputs, macro_inputs = {}, {}, {}
    for name, spec in macros.items():
        _macro_name(name)
        spec['slots'] = tuple(spec['slots'])
        spec['targets'] = tuple(tuple(t) for t in spec['targets'])
        if type(spec['bipolar']) is not bool or (spec['curve'] is not None and not callable(spec['curve'])):
            raise ValueError('macro %s: invalid knob mapping' % name)
        n = spec['input']
        if type(n) is not int or not 0 <= n < 8 or n in inputs:
            raise ValueError('macro %s: colliding ctl input %s' % (name, n))
        inputs[n] = name
        macro_inputs[n] = name
        for slot, target in zip(spec['slots'], spec['targets']):
            if slot in owned or mods.get(slot) != ('ctl%d' % n, *target):
                raise ValueError('macro %s: slot %s ownership changed' % (name, slot))
            owned[slot] = name
            _macro_target(target[0], operators, extra)
        if not 1 <= len(spec['slots']) == len(spec['targets']) <= 8:
            raise ValueError('macro %s needs one to eight targets' % name)
    for slot, (source, _, _, _) in mods.items():
        if source.startswith('ctl'):
            n = int(source[3:])
            if n in macro_inputs and owned.get(slot) != macro_inputs[n]:
                raise ValueError('macro %s: ctl%d collides with raw slot %d' % (macro_inputs[n], n, slot))
            if slot not in owned:
                inputs[n] = 'raw slot %d' % slot
    for name, n, targets, bipolar, curve in additions:
        _macro_name(name)
        if name in macros:
            raise ValueError('macro %s is already defined' % name)
        if type(n) is not int or not 0 <= n < 8:
            raise ValueError('macro %s: ctl input must be integer 0..7' % name)
        if n in inputs:
            raise ValueError('macro %s: ctl%d collides with %s' % (name, n, inputs[n]))
        if type(bipolar) is not bool or (curve is not None and not callable(curve)):
            raise ValueError('macro %s: boolean bipolar and one callable curve required' % name)
        if not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= 8:
            raise ValueError('macro %s needs one to eight targets' % name)
        free = [i for i in range(8) if i not in mods]
        if len(targets) > len(free):
            raise ValueError('macro %s: eight-slot budget exceeded (%d existing, %d requested)' %
                             (name, len(mods), len(targets)))
        normalized = []
        for slot, fields in zip(free, targets):
            if not isinstance(fields, (list, tuple)) or len(fields) not in (2, 3):
                raise ValueError('macro %s: target is (TARGET, AMOUNT[, SHIFT])' % name)
            target, amount = fields[:2]
            shift = fields[2] if len(fields) == 3 else 3
            values = mod_values((slot, 'ctl%d' % n, target, amount, shift))
            _macro_target(target, operators, extra)
            mods[slot] = values[1:]
            normalized.append(values[2:])
        try:
            mod_settings(mods, extra, fs)
        except ValueError as e:
            raise ValueError('macro %s: %s' % (name, e)) from None
        macros[name] = dict(input=n, targets=tuple(normalized), slots=tuple(free[:len(targets)]),
                            bipolar=bipolar, curve=curve)
        inputs[n] = name
    return mods, macros


class RecipeBook(dict):
    """Recipe mapping with a separate, host-only template catalogue."""


def _template_blocks(lines, fail):
    """Extract explicit template/end blocks, preserving source line numbers."""
    templates, skipped, current, case, macro = {}, set(), None, None, None
    for n, line in enumerate(lines, 1):
        w = line.split()
        if not w or w[0].startswith('#'):
            continue
        if w[0] == 'template':
            if current is not None or len(w) != 2 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', w[1]) or w[1] in templates:
                fail(n, 'template needs a new token name outside another template')
            current = w[1]
            templates[current] = []
            case = macro = None
        elif current is None:
            continue
        elif w[0] == 'end':
            if len(w) != 1 or not templates[current] or any(not c['macros'] for c in templates[current]):
                fail(n, 'template end needs nonempty cases and macros')
            for c in templates[current]:
                if any(not m['targets'] for m in c['macros']):
                    fail(n, 'template macro needs at least one target')
            current = case = macro = None
        elif w[0] == 'case':
            if len(w) != 2 or w[1] not in ('vector', 'filter', 'default') or any(c['when'] == w[1] for c in templates[current]):
                fail(n, 'template case is vector, filter or default, once each')
            if templates[current] and templates[current][-1]['when'] == 'default':
                fail(n, 'template default case must be last')
            case = dict(when=w[1], macros=[])
            templates[current].append(case)
            macro = None
        elif case is None:
            fail(n, 'template statements need a case first')
        elif w[0] == 'macro':
            if len(w) != 4 or not w[2].isdigit() or not 0 <= int(w[2]) < 8 or w[3] not in ('unipolar', 'bipolar'):
                fail(n, 'template macro is NAME INPUT unipolar|bipolar')
            try:
                _macro_name(w[1])
            except ValueError as e:
                fail(n, str(e))
            if any(m['name'] == w[1] or m['input'] == int(w[2]) for m in case['macros']):
                fail(n, 'template macro %s collides with a name or input' % w[1])
            macro = dict(name=w[1], input=int(w[2]), bipolar=w[3] == 'bipolar', targets=[])
            case['macros'].append(macro)
        elif w[0] == 'target':
            if macro is None or len(w) not in (3, 4) or not re.fullmatch(r'-?\d+', w[2]) or (len(w) == 4 and not w[3].isdigit()):
                fail(n, 'template target is TARGET AMOUNT [SHIFT] after a macro')
            family, dot, role = w[1].partition('.')
            if dot and (family not in ('pitch', 'index', 'level') or role not in ('modulators', 'mod0', 'mod1', 'car0', 'car1', 'brightest')):
                fail(n, 'unknown template target role %s' % w[1])
            try:
                fields = mod_values((0, 'ctl0', family+'0' if dot else family, int(w[2]), int(w[3]) if len(w) == 4 else 3))
            except ValueError as e:
                fail(n, str(e))
            macro['targets'].append((w[1], fields[3], fields[4]))
        elif w[0] == 'lfo':
            try:
                if 'lfo' in case or any(not x.isdigit() for x in w[1:]):
                    raise ValueError('template has duplicate or noninteger lfo')
                case['lfo'] = lfo_values(tuple(map(int, w[1:])))
            except ValueError as e:
                fail(n, str(e))
        else:
            fail(n, 'unknown template statement %s' % w[0])
        skipped.add(n)
    if current is not None:
        fail(len(lines), 'template %s has no end' % current)
    return templates, skipped


def macro_template(name, templates, operators, extra, fs=None):
    """Resolve recipe-data roles, then preflight the complete selected case."""
    if name not in templates:
        raise ValueError('no macro template %s' % name)
    cases = templates[name]
    case = next((c for c in cases if c['when'] == 'default' or c['when'] in extra), None)
    if case is None:
        raise ValueError('template %s does not fit the patch' % name)
    pending = dict(extra)
    if 'lfo' in case:
        if 'lfo' in extra:
            raise ValueError('template %s would replace an existing LFO' % name)
        pending['lfo'] = lfo_values(case['lfo'], len(operators))
    carriers = [i for i in range(len(operators)) if extra.get('carriers', 1) >> i & 1]
    modulators = sorted({f for f, _, _, kind in map(edge_values, extra['edges']) if kind == 'pm'}) if 'ops' in extra else [1]
    modulators = [i for i in modulators if i not in extra.get('muted', ())]
    additions = []
    for spec in case['macros']:
        targets = []
        for target, amount, shift in spec['targets']:
            family, dot, role = target.partition('.')
            if not dot:
                selected = [target]
            elif role == 'modulators':
                selected = [family+str(i) for i in modulators]
            elif role in ('mod0', 'mod1', 'car0', 'car1'):
                group = modulators if role.startswith('mod') else carriers
                if len(group) != 2:
                    raise ValueError('template %s: %s needs exactly two %s' % (name, target, 'modulators' if role.startswith('mod') else 'carriers'))
                selected = [family+str(group[int(role[-1])])]
            elif role == 'brightest' and modulators:
                selected = [family+str(max(modulators, key=lambda i: (operators[i][0], -i)))]
            else:
                raise ValueError('template %s: target role %s does not fit' % (name, target))
            if not selected:
                raise ValueError('template %s: %s has no target' % (name, target))
            targets.extend((t, amount, shift) for t in selected)
        additions.append((spec['name'], spec['input'], targets, spec['bipolar'], None))
    try:
        mods, macros = macro_budget(extra.get('mod', {}), extra.get('macros', {}), additions,
                                    operators, pending, fs)
    except ValueError as e:
        raise ValueError('template %s: %s' % (name, e)) from None
    pending.update(mod=mods, macros=macros)
    return pending


def load_recipes(path):
    """The recipes in path as {name: (carrier, modulator, note)}, in the file's order,
    each operator a dict of op arguments; a routed recipe  has a fourth item,
    {'ops': OPS, 'carriers': MASK, 'edges': [(FROM, TO, WEIGHT), ...], 'more': [the
    dicts of operators 2 to OPS - 1]}. AM/ring and sync edges append their kind string to
    the legacy PM three-tuple; a sync edge's weight is 256 . An LFO adds 'lfo': a tuple of the seven or
    eleven raw integer fields, or twelve with FREE, to that fourth item (also on
    a pair). A PEG adds
    'peg': nine raw integer fields, with its omitted mask resolved at block end.
    A filter adds 'filter': (mode, cutoff_millihz, q_milli), appending 2 for a
    two-stage cascade; its optional
    filter envelope adds 'fenv': seven raw fields, with reference 261626 when omitted.
    operator extension shape adds 'shape': (drive_db10, kind), present even at zero drive.
    filter adds 'eg': {operator: (delay_ms, decay2_ms, sustain2_db10)}; enabled
    second decay must finish at or below the operator's sustain gain. filter also
    adds 'ks': {operator: left_slope_db10}. operator extension adds 'noise': {operator: shift},
    accepted only on waves 10 and 11; a default shift of 4 normalizes away when written.
    Static vector adds 'vector': (x,y), enabled even at (0,0).
    Formant adds 'formant': {slot: (center_millihz,q_milli,level_db10)}.
    PD adds 'pd': {operator: knee}, enabled even at identity 16384.
    modulation matrix adds 'mod': {slot: (source, target, amount, shift)}, with default shift 3.
    Host template/end blocks live separately in result.templates; use NAME in a
    recipe resolves one complete case into raw mod/LFO headers and host-only
    macros ownership metadata. Neither template nor macro is engine grammar.
    A recipe is a block: `recipe NAME`, one
    `carrier` and one `modulator` line of KEY=VALUE pairs, and `note` lines, joined
    by a space; a routed one adds a `route OPS CARRIERS FROM>TO:WEIGHT[:pm|am|ring|sync] ...` line, the
    route statement of  without its patch name, and an `op N` line of KEY=VALUE
    pairs for each operator N from 2 to OPS - 1 (the carrier line is operator 0, the
    modulator line operator 1). One each of `filter`, `shape`, `lfo`, `peg` and `fenv` takes the harness's fields without
    the patch name; masks are checked after the whole block, so it can precede a
    route. A filter envelope requires a filter in the completed block. Its `tags` lines, words for analyze.py search --tag
    , are skipped here; a line starting with # is a comment. A KEY is any
    argument of op but one the engine ignores on that operator (ignored); a VALUE
    is a plain decimal number within its LIMITS range, whole where op's default is,
    and keeps its type. The first fault exits with the file and the line."""
    params = inspect.signature(op).parameters
    where = shown(path)
    most = 4                                    # FM_OPS, the engine's operators (fm_a1.h)
    recipes, block = RecipeBook(), None                   # block: [name, first line, {kind: value}, {kind: line}]

    def fail(n, msg):
        sys.exit('compose.py: %s:%d: %s' % (where, n, msg))

    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    templates, template_lines = _template_blocks(lines, fail)
    recipes.templates = templates

    def close():
        if block:
            name, n, parts, at = block
            for kind in ('carrier', 'modulator', 'note'):
                if kind not in parts:
                    fail(n, 'recipe %s has no %s line' % (name, kind))
            routing, more = parts.get('route'), sorted(k for k in parts if isinstance(k, int))
            if more and not routing:
                fail(at[more[0]], 'recipe %s: an op line needs a route line' % name)
            if routing:
                ops = routing['ops']
                for i in more:
                    if i >= ops:
                        fail(at[i], 'recipe %s: op %d is at or above the route\'s OPS %d' % (name, i, ops))
                for i in range(2, ops):
                    if i not in parts:
                        fail(at['route'], 'recipe %s: route OPS %d wants an op %d line' % (name, ops, i))
                reach, grew = routing['carriers'], True     # the operators a carrier hears
                while grew:
                    grew = False
                    for f, t, _, _ in map(edge_values, routing['edges']):
                        if reach >> t & 1 and not reach >> f & 1:
                            reach, grew = reach | 1 << f, True
                for i in range(ops):
                    if not reach >> i & 1:
                        fail(at['route'], 'recipe %s: operator %d reaches no carrier, so it changes nothing' % (name, i))
                routing = dict(routing, more=[parts[i] for i in range(2, ops)])
            for kind in ['carrier', 'modulator'] + more:
                for key in parts[kind]:
                    why = ignored(kind[0] if kind in ('carrier', 'modulator') else 'o%d' % kind, key, routing)
                    if why:
                        fail(at[kind], '%s\'s %s changes nothing; %s' % (label(kind), key, why))
            extra = dict(routing) if routing else {}
            if 'filter' in parts:
                extra['filter'] = parts['filter']
            if 'shape' in parts:
                extra['shape'] = parts['shape']
            if 'amp' in parts:
                extra['amp'] = parts['amp']
            for kind in ('kstype', 'fbtype', 'egloop'):
                if kind in parts:
                    extra[kind] = {}
                    for i, values in parts[kind].items():
                        try:
                            fields = op_ext_values(kind, (i, *values), routing['ops'] if routing else 2)
                            if op_ext_row(kind, name, *fields):
                                extra[kind][i] = fields[1:]
                        except ValueError as e:
                            fail(at[(kind, i)], str(e))
            if 'lfo' in parts:
                try:
                    extra['lfo'] = lfo_values(parts['lfo'], routing['ops'] if routing else 2)
                except ValueError as e:
                    fail(at['lfo'], str(e))
            if 'peg' in parts:
                try:
                    extra['peg'] = peg_values(parts['peg'], routing['ops'] if routing else 2)
                except ValueError as e:
                    fail(at['peg'], str(e))
            if 'fenv' in parts:
                if 'filter' not in parts:
                    fail(at['fenv'], 'recipe %s: fenv needs a filter statement' % name)
                extra['fenv'] = parts['fenv']
            if 'eg' in parts:
                extra['eg'] = {}
                for i, values in parts['eg'].items():
                    try:
                        fields = eg_values((i, *values), routing['ops'] if routing else 2)
                        args = parts['carrier' if i == 0 else 'modulator' if i == 1 else i]
                        extra['eg'][i] = eg_values(fields, sustain1=op(**args)[7])[1:]
                    except ValueError as e:
                        fail(at[('eg', i)], str(e))
            if 'ks' in parts:
                extra['ks'] = {}
                for i, value in parts['ks'].items():
                    try:
                        extra['ks'][i] = ks_values((i, value), routing['ops'] if routing else 2, routing)[1]
                    except ValueError as e:
                        fail(at[('ks', i)], str(e))
            if 'noise' in parts:
                extra['noise'] = {}
                for i, value in parts['noise'].items():
                    try:
                        fields = noise_values((i, value), routing['ops'] if routing else 2)
                        args = parts['carrier' if i == 0 else 'modulator' if i == 1 else i]
                        extra['noise'][i] = noise_values(fields, wave=args.get('wave', 0))[1]
                    except ValueError as e:
                        fail(at[('noise', i)], str(e))
            if 'vector' in parts:
                try:
                    extra['vector'] = vector_values(parts['vector'],routing['ops'] if routing else 2,
                                                    routing['carriers'] if routing else 1)
                except ValueError as e:
                    fail(at['vector'],str(e))
            if 'formant' in parts:
                extra['formant'] = dict(parts['formant'])
            if 'pd' in parts:
                extra['pd'] = {}
                for i,knee in parts['pd'].items():
                    try:
                        fields = pd_values((i,knee),routing['ops'] if routing else 2)
                        args = parts['carrier' if i == 0 else 'modulator' if i == 1 else i]
                        extra['pd'][i] = pd_values(fields,wave=args.get('wave',0))[1]
                    except ValueError as e:
                        fail(at[('pd',i)],str(e))
            if 'wtuse' in parts:
                extra['wtbanks'] = {form: (1, 2048, recipe_harmonics(form))
                                    for form, _ in parts['wtuse'].values()}
                extra['wtuse'] = dict(parts['wtuse'])
                try:
                    wt_metadata(name, [op(**parts['carrier']), op(**parts['modulator'])] +
                                [op(**parts[i]) for i in more], extra)
                except ValueError as e:
                    fail(at['wtuse'], str(e))
            if 'mod' in parts:
                extra['mod'] = {}
                for slot, fields in parts['mod'].items():
                    try:
                        extra['mod'].update(mod_settings({slot: fields}, extra))
                    except ValueError as e:
                        fail(at[('mod', slot)], str(e))
            for template in parts.get('use', ()):
                try:
                    operators = [op(**parts['carrier']), op(**parts['modulator'])] + [op(**parts[i]) for i in more]
                    extra = macro_template(template, templates, operators, extra)
                except ValueError as e:
                    fail(at['use'], 'recipe %s: %s' % (name, e))
            recipes[name] = (parts['carrier'], parts['modulator'], ' '.join(parts['note'])) + ((extra,) if extra else ())

    def label(kind):
        return 'the ' + kind if kind in ('carrier', 'modulator') else 'operator %d' % kind

    def route(n, words):
        if len(words) < 2:
            fail(n, 'a route line is `route OPS CARRIERS FROM>TO:WEIGHT ...`')
        if not re.fullmatch(r'\d', words[0]) or not 2 <= int(words[0]) <= most:
            fail(n, 'route OPS %s: OPS is 2 to %d, the operators' % (words[0], most))
        ops = int(words[0])
        top = (1 << ops) - 1
        if not re.fullmatch(r'\d+', words[1]) or not 1 <= int(words[1]) <= top or not int(words[1]) & 1:
            fail(n, 'route CARRIERS %s: the carrier mask is odd, 1 to %d: operator 0 is always a carrier, '
                    'the level the leveller moves' % (words[1], top))
        edges = {}
        for word in words[2:]:
            m = re.fullmatch(r'(\d)>(\d):(\d+)(?::(pm|am|ring|sync))?', word)
            if not m:
                fail(n, 'route edge %r is not FROM>TO:WEIGHT[:pm|am|ring|sync]' % word)
            try:
                fields = edge_values((*map(int, m.groups()[:3]), m.group(4) or 'pm'), ops)
            except ValueError as e:
                fail(n, str(e))
            f, t, w, kind = fields
            if (f, t) in edges:
                fail(n, 'route edge %d>%d is listed twice' % (f, t))
            edges[f, t] = fields[:3] if kind == 'pm' else fields
        return {'ops': ops, 'carriers': int(words[1]), 'edges': [e for _, e in sorted(edges.items())]}

    def operator(n, side, words):
        args = {}
        for word in words:
            key, eq, text = word.partition('=')
            if not eq:
                fail(n, '%r is not KEY=VALUE' % word)
            if key not in params:
                fail(n, 'op() has no argument %r; its arguments are %s' % (key, ', '.join(params)))
            if key in args:
                fail(n, '%s sets %s twice' % (label(side), key))
            whole = isinstance(params[key].default, int)
            if re.fullmatch(r'-?\d+', text):
                value = int(text)
            elif re.fullmatch(r'-?\d+\.\d+', text) and not whole:
                value = float(text)
            else:
                fail(n, '%s=%s: %s takes %s' % (key, text, key, 'a whole number' if whole
                                                 else 'a decimal number, such as 3 or 2.5'))
            lo, hi = LIMITS.get(key, (value, value))
            if not lo <= value <= hi:
                fail(n, '%s=%s is outside its range, %s to %s' % (key, text, lo, hi))
            if key == 'wave' and value not in OP_WAVES:
                fail(n, 'wave=%s is not supported; choose %s' % (text, ', '.join(map(str, OP_WAVES))))
            args[key] = value
        return args

    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            if n in template_lines:
                continue
            words = line.split()
            if not words or words[0].startswith('#'):
                continue
            kind = words[0]
            if kind == 'recipe':
                close()
                if len(words) != 2 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', words[1]):
                    fail(n, 'a recipe line is `recipe NAME`, NAME of letters, digits and . _ -')
                if words[1] in recipes:
                    fail(n, 'recipe %s is defined twice' % words[1])
                block = [words[1], n, {}, {}]
            elif kind not in ('carrier', 'modulator', 'op', 'route', 'shape', 'amp', 'kstype', 'fbtype', 'egloop', 'filter', 'lfo', 'peg', 'fenv', 'eg', 'ks', 'noise', 'pd', 'vector', 'formant', 'wtuse', 'mod', 'use', 'note', 'tags', 'describe_note'):
                fail(n, 'a line starts with recipe, carrier, modulator, op, route, filter, shape, amp, kstype, fbtype, egloop, lfo, peg, fenv, eg, ks, noise, pd, vector, formant, wtuse, mod, note, tags, describe_note or #, not %r' % kind)
            elif block is None:
                fail(n, 'a %s line before the first recipe line' % kind)
            elif kind == 'use':
                if len(words) != 2:
                    fail(n, 'use takes one macro template name')
                block[2].setdefault('use', []).append(words[1])
                block[3]['use'] = n
            elif kind in ('tags', 'describe_note'):
                pass                                # analyze.py owns descriptive metadata; recipe takes none
            elif kind == 'note':
                if len(words) == 1:
                    fail(n, 'an empty note line')
                block[2].setdefault('note', []).append(line.split(None, 1)[1].strip())
            elif kind == 'op':                      # operators 2 and up of a routed recipe
                if len(words) < 2 or not re.fullmatch(r'\d', words[1]) or not 2 <= int(words[1]) < most:
                    fail(n, 'an op line is `op N KEY=VALUE ...` for operator 2 to %d; operator 0 is the carrier '
                            'line and 1 the modulator line' % (most - 1))
                if int(words[1]) in block[2]:
                    fail(n, 'recipe %s has a second op %s line' % (block[0], words[1]))
                block[2][int(words[1])] = operator(n, int(words[1]), words[2:])
                block[3][int(words[1])] = n
            elif kind == 'wtuse':
                if (len(words) != 4 or not re.fullmatch(r'[0-9]+', words[1]) or
                        not re.fullmatch(r'[0-9]+', words[3])):
                    fail(n, 'wtuse takes OP FORM POSITION_Q15 with integer operator and position')
                i, form, position = int(words[1]), words[2], int(words[3])
                if form not in RECIPE_WT_FORMS:
                    fail(n, 'unknown harmonic form %r' % form)
                if i in block[2].get(kind, {}):
                    fail(n, 'recipe %s has a second wtuse for operator %d' % (block[0], i))
                block[2].setdefault(kind, {})[i] = (form, position)
                block[3][kind] = n
            elif kind == 'mod':
                if len(words) not in (5, 6) or not re.fullmatch(r'[0-9]+', words[1]) or not re.fullmatch(r'-?[0-9]+', words[4]) or (
                        len(words) == 6 and not re.fullmatch(r'[0-9]+', words[5])):
                    fail(n, 'mod takes SLOT SOURCE TARGET AMOUNT [SHIFT], amount signed and slot/shift unsigned integers')
                try:
                    fields = mod_values((int(words[1]), words[2], words[3], int(words[4])) +
                                        ((int(words[5]),) if len(words) == 6 else ()))
                except ValueError as e:
                    fail(n, str(e))
                slot = fields[0]
                if slot in block[2].get(kind, {}):
                    fail(n, 'recipe %s has a second mod line for slot %d' % (block[0], slot))
                block[2].setdefault(kind, {})[slot] = fields[1:]
                block[3][(kind, slot)] = n
            elif kind == 'formant':
                if any(not re.fullmatch(r'[0-9]+', w) for w in words[1:]):
                    fail(n, 'formant values are unsigned decimal integers')
                try:
                    fields = formant_values(tuple(map(int, words[1:])))
                except ValueError as e:
                    fail(n, str(e))
                slot = fields[0]
                if slot in block[2].get(kind, {}):
                    fail(n, 'recipe %s has a second formant line for slot %d' % (block[0], slot))
                block[2].setdefault(kind, {})[slot] = fields[1:]
                block[3][(kind, slot)] = n
            elif kind in ('kstype', 'fbtype', 'egloop'):
                if len(words) < 2 or not re.fullmatch(r'[0-9]+', words[1]):
                    fail(n, '%s starts with an unsigned operator number' % kind)
                try:
                    fields = op_ext_values(kind, (int(words[1]), *words[2:]))
                except ValueError as e:
                    fail(n, str(e))
                i = fields[0]
                if i in block[2].get(kind, {}):
                    fail(n, 'recipe %s has a second %s line for operator %d' % (block[0], kind, i))
                block[2].setdefault(kind, {})[i] = fields[1:]
                block[3][(kind, i)] = n
            elif kind in ('eg', 'ks', 'noise', 'pd'):
                if any(not re.fullmatch(r'[0-9]+', w) for w in words[1:]):
                    fail(n, '%s values are unsigned decimal integers' % kind)
                try:
                    values = {'eg': eg_values, 'ks': ks_values, 'noise': noise_values, 'pd': pd_values}[kind](tuple(map(int, words[1:])))
                except ValueError as e:
                    fail(n, str(e))
                i = values[0]
                if i in block[2].get(kind, {}):
                    fail(n, 'recipe %s has a second %s line for operator %d' % (block[0], kind, i))
                block[2].setdefault(kind, {})[i] = values[1:] if kind == 'eg' else values[1]
                block[3][(kind, i)] = n
            elif kind in block[2]:
                fail(n, 'recipe %s has a second %s line' % (block[0], kind))
            elif kind == 'route':
                block[2][kind], block[3][kind] = route(n, words[1:]), n
            elif kind == 'filter':
                if len(words) not in (4, 5) or any(not re.fullmatch(r'[0-9]+', w) for w in words[2:4]) or (
                        len(words) == 5 and words[4] != 'x2'):
                    fail(n, 'filter takes MODE CUTOFF_MILLIHZ Q_MILLI [x2], cutoff and Q unsigned decimal integers')
                try:
                    fields = (words[1], *map(int, words[2:4])) + ((2,) if len(words) == 5 else ())
                    block[2][kind], block[3][kind] = filter_values(fields), n
                except ValueError as e:
                    fail(n, str(e))
            elif kind == 'vector':
                if any(not re.fullmatch(r'[0-9]+',w) for w in words[1:]):
                    fail(n,'vector fields must be unsigned decimal integers')
                try:
                    block[2][kind],block[3][kind] = vector_values(tuple(map(int,words[1:]))),n
                except ValueError as e:
                    fail(n,str(e))
            elif kind == 'amp':
                if any(not re.fullmatch(r'[0-9]+', w) for w in words[1:]):
                    fail(n, 'amp fields are unsigned decimal integers')
                try:
                    block[2][kind], block[3][kind] = amp_values(tuple(map(int, words[1:]))), n
                except ValueError as e:
                    fail(n, str(e))
            elif kind == 'shape':
                if len(words) not in (3, 4) or not re.fullmatch(r'[0-9]+', words[1]):
                    fail(n, 'shape takes unsigned DRIVE_DB10 KIND [pre], KIND hard, cubic, fold or asym')
                try:
                    block[2][kind], block[3][kind] = shape_values((int(words[1]), *words[2:])), n
                except ValueError as e:
                    fail(n, str(e))
            elif kind == 'lfo':
                if any(not re.fullmatch(r'[0-9]+', w) for w in words[1:]):
                    fail(n, 'lfo values are unsigned decimal integers')
                try:
                    block[2][kind], block[3][kind] = lfo_values(tuple(map(int, words[1:]))), n
                except ValueError as e:
                    fail(n, str(e))
            elif kind == 'peg':
                if any(not re.fullmatch(r'-?[0-9]+' if i < 8 and i % 2 == 0 else r'[0-9]+', w)
                       for i, w in enumerate(words[1:])):
                    fail(n, 'peg levels are signed decimal integers; times and MASK are unsigned')
                try:
                    block[2][kind], block[3][kind] = peg_values(tuple(map(int, words[1:]))), n
                except ValueError as e:
                    fail(n, str(e))
            elif kind == 'fenv':
                if any(not re.fullmatch(r'-?[0-9]+' if i == 4 else r'[0-9]+', w)
                       for i, w in enumerate(words[1:])):
                    fail(n, 'fenv depth is a signed decimal integer; other fields are unsigned')
                try:
                    block[2][kind], block[3][kind] = fenv_values(tuple(map(int, words[1:]))), n
                except ValueError as e:
                    fail(n, str(e))
            else:
                block[2][kind], block[3][kind] = operator(n, kind, words[1:]), n
    close()
    if not recipes:
        sys.exit('compose.py: %s: no recipes' % where)
    return recipes


OP_ARGS = {k: v.default for k, v in inspect.signature(op).parameters.items()}
LIMITS = {'ratio': (0.0, 65535.99), 'level': (0.0, 120.0), 'index': (0.0, 63.999), 'fb': (0.0, 63.999),
          'vel': (0, 256), 'a': (0, 65535), 'd': (0, 65535), 's': (0.0, 120.0), 'r': (0, 65535),
          'ks_break': (0.0, 4294967.295), 'ks_slope': (0.0, 6553.5),
          'rs': (0, 7), 'fixed': (0.0, 4294967.295), 'phase': (0.0, 359.994), 'free': (0, 2)}   # the last 4: the operator extras
UNUSED = ('c_index', 'c_fb', 'm_level', 'c_ks_break', 'c_ks_slope')   # the engine reads no such field on that operator


def ignored(side, arg, routing=None):
    """Why the engine ignores op argument arg on the operator side ('c', 'm', 'o2' or
    'o3') of a recipe with this route (a routed recipe's fourth item; None for the
    carrier and modulator pair), or '' when it reads it. The pair's carrier reads no
    index, fb or key scaling and its modulator no level (UNUSED); under a route
    (fm_a1.h) an operator's level counts where it is a carrier, its index and key
    scaling on its outgoing PM edges only, and its fb always."""
    i = {'c': 0, 'm': 1}.get(side) if side in ('c', 'm') else int(side[1:])
    if routing and i in routing.get('muted', ()):
        return 'operator %d is muted' % i
    if routing is None or 'ops' not in routing:
        return 'the engine ignores that field' if side + '_' + arg in UNUSED else ''
    if arg == 'level' and not routing['carriers'] >> i & 1:
        return 'operator %d is no carrier of the route' % i
    if arg in ('index', 'ks_break', 'ks_slope') and not any(f == i and kind == 'pm' for f, _, _, kind in map(edge_values, routing['edges'])):
        return 'operator %d modulates no operator in the route through a PM edge' % i
    return ''


RECIPES = load_recipes(RECIPE_FILE)          # once, when compose.py loads
MACRO_TEMPLATES = RECIPES.templates


TABLE_FILE = os.path.join(DATA, 'tables.txt')
TABLE_ARGS = ('ratio', 'level', 'index', 'fb', 'vel', 'a', 'd', 's', 'r')   # the arguments a scope may name
TABLE_SIDES = ('c', 'm', 'o2', 'o3')


def load_tables(path):
    """tables.txt (, regression): {'scale': {NAME: MASK}, 'preset': {NAME: AMOUNT},
    'scope': {NAME: (ARG, ...)}, 'span': {ARG: UNITS}, 'field': [NAME, ...] by stream number},
    whole numbers only. A fault stops compose.py with the file's name and the line."""
    t = {'scale': {}, 'preset': {}, 'scope': {}, 'span': {}, 'field': []}
    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            w = line.split('#')[0].split()
            if not w:
                continue
            def fail(why):
                sys.exit('compose.py: %s:%d: %s' % (shown(path), n, why))
            kind, rest = w[0], w[1:]
            if kind == 'scale' and len(rest) == 2 and re.fullmatch('[a-z0-9-]+', rest[0]) and rest[1].isdigit():
                if not 1 <= int(rest[1]) <= 4095 or rest[0] in t['scale'] or int(rest[1]) in t['scale'].values():
                    fail('a scale is a new name and a new mask, 1 to 4095')
                t['scale'][rest[0]] = int(rest[1])
            elif kind == 'preset' and len(rest) == 2 and re.fullmatch('[a-z]+', rest[0]) and rest[1].isdigit():
                if not int(rest[1]) <= 100 or rest[0] in t['preset']:
                    fail('a preset is a new name and an amount, 0 to 100')
                t['preset'][rest[0]] = int(rest[1])
            elif kind == 'scope' and len(rest) >= 2 and re.fullmatch('[a-z]+', rest[0]):
                if rest[0] in t['scope'] or any(a not in TABLE_ARGS for a in rest[1:]) or len(set(rest[1:])) < len(rest) - 1:
                    fail('a scope is a new name and distinct arguments of %s' % ', '.join(TABLE_ARGS))
                t['scope'][rest[0]] = tuple(rest[1:])
            elif kind == 'span' and len(rest) == 2 and rest[1].isdigit():
                if rest[0] not in ('level', 's', 'index', 'fb', 'vel') or rest[0] in t['span'] or not int(rest[1]):
                    fail('a span is level, s, index, fb or vel, once, and a whole number above 0')
                t['span'][rest[0]] = int(rest[1])
            elif kind == 'field' and len(rest) == 2 and rest[0].isdigit():
                side, _, arg = rest[1].partition('_')
                if int(rest[0]) != len(t['field']) or side not in TABLE_SIDES or arg not in TABLE_ARGS \
                        or rest[1] in t['field']:
                    fail('field %d is next, a new SIDE_ARG of %s and %s' % (len(t['field']), TABLE_SIDES, TABLE_ARGS))
                t['field'].append(rest[1])
            else:
                fail('no statement %r; the statements are scale, preset, scope, span and field' % ' '.join(w))
    return t


TABLES = load_tables(TABLE_FILE)            # once, when compose.py loads


def _carson_fields(name, operators, extra, request, tweaks):
    """ ideal-only finite-domain Carson search; native deviations are measured.
    No gain-table clone or alias-free/native-conservative promise. Pitch events,
    PEG/LFO/modulation matrix pitch/index, feedback, shaper and aggregate route spectra are outside
    this per-edge heuristic. All arithmetic selecting raw fields is Fraction/Decimal.
    """
    def fail(message):
        raise TypeError('recipe %s: Carson %s' % (name, message))
    if not isinstance(request, (tuple, list)) or len(request) != 2:
        fail('needs (fs, onsets_millihz)')
    fs, onsets = request
    if type(fs) is not int or not 1 <= fs <= 4294967295:
        fail('fs must be an integer 1..4294967295')
    if not isinstance(onsets, (tuple, list)) or not onsets or any(
            type(f) is not int or not 1 <= f <= 4294967295 for f in onsets):
        fail('onsets must be a nonempty list/tuple of integers 1..4294967295')
    onsets = tuple(sorted(set(onsets)))
    edges = list(map(edge_values, extra['edges'])) if 'ops' in extra else [(1,0,256,'pm')]
    if any(kind != 'pm' for _, _, _, kind in edges):
        fail('only PM edges are supported; AM/ring/sync refuse')
    if not edges:
        fail('needs a PM edge')
    checked = {i for source, target, _, _ in edges for i in (source, target)}
    for i in sorted(checked):
        fields = operators[i]
        if fields[0] <= 0:
            fail('operator%d needs a positive relative ratio' % i)
        if len(fields) > 13 and fields[13]:
            fail('operator%d fixed frequency is unsupported' % i)
        if len(fields) > 11 and fields[11]:
            fail('operator%d needs sine wave0' % i)
        for kind in ('wtuse', 'ksource', 'pd'):
            if i in extra.get(kind, {}):
                fail('operator%d has %s binding' % (i, kind))
    coefficients = {}
    for source, target, weight, _ in edges:
        c = Fraction(operators[target][0], 65536)
        m = Fraction(operators[source][0], 65536)
        index = Fraction(operators[source][2], 1024)*Fraction(weight,256)
        coefficients.setdefault(source, []).append((target,c,m,index))
    edge = 450*fs
    result = {}
    for source, pairs in sorted(coefficients.items()):
        fields = operators[source]
        side = ('c','m')[source] if source < 2 else 'o%d'%source
        if any(key in tweaks for key in (side+'_ks_break',side+'_ks_slope')):
            fail('operator%d has an explicit key-scaling tweak' % source)
        if any(fields[9:11]):
            fail('operator%d already has key scaling' % source)
        if source in extra.get('ks', {}):
            fail('operator%d has left-slope metadata' % source)
        if tuple(extra.get('kstype', {}).get(source, ('index','-','-'))) != ('index','-','-'):
            fail('operator%d has nondefault kstype' % source)
        breaks = [Fraction(edge,1)/(c+m*(index+1)) for _,c,m,index in pairs]
        breakpoint = min(4294967295, *(value.numerator//value.denominator for value in breaks))
        if breakpoint < 1:
            fail('operator%d breakpoint is below1mHz' % source)
        for f in onsets:
            for target,c,m,index in pairs:
                if f*(c+m) > edge:
                    fail('operator%d->%d onset%d cannot meet the ideal edge even at zero index' % (source,target,f))
        with decimal.localcontext(decimal.Context(prec=50, rounding=decimal.ROUND_HALF_UP)):
            D = decimal.Decimal
            def value(fraction):
                return D(fraction.numerator)/fraction.denominator
            dpairs = [(target,value(c),value(m),value(index)) for target,c,m,index in pairs]
            logs = [(f,(D(f)/breakpoint).ln()/D(2).ln() if f>breakpoint else D(0)) for f in onsets]
            for slope in range(1201):
                valid = True
                for f, logarithm in logs:
                    gain = D(10)**(-min(D(1200),D(slope)*logarithm)/200) if slope and logarithm else D(1)
                    for target,c,m,index in dpairs:
                        if D(f)*(c+m*(index*gain+1)) > edge:
                            valid = False
                            failed = target,f
                            break
                    if not valid:
                        break
                if valid:
                    result[source] = (breakpoint,slope)
                    break
            else:
                fail('operator%d->%d onset%d has no slope in0..1200 meeting the ideal edge' %
                     (source,*failed))
    return result, fs, onsets


def recipe(name, time=1.0, *, mute=(), solo=None, templates=(), carson=None, **tweaks):
    """A recipe as (carrier, modulator, comment, extra), for p.patch(pname, *recipe(...))
    . mute=(ops...) removes carriers from the mask and zeroes each muted
    operator's index/fb; outgoing non-PM edges and an empty carrier mask refuse.
    solo=OP removes all other carriers, leaving the historical operator0 level
    reference in place. Tweaks on muted operators refuse. templates=(names...)
    applies recipe-file macro templates after the normal deltas, atomically.
    extra holds a routed recipe's route ('ops', 'carriers', 'edges') and
    operators 2 and up ('more'), and/or raw control fields ('filter', 'lfo', 'peg',
    'fenv', ); it is {} for a recipe with none. These fields take no deltas or time scaling.
    carson=(fs,onsets_millihz) opts into an ideal-only finite-domain
    per-PM-edge heuristic, with Fraction/Decimal-derived key-scaling fields. None preserves
    every old byte. Native gain/index approximations may exceed the ideal edge;
    no alias-free or native-conservative guarantee is made. Pitch/controllers,
    feedback, shaper and aggregate routes are outside it. This changes no position.
    time scales a, d and r of every operator. A tweak
    c_ARG or m_ARG, where ARG is an op argument, adds its value to that
    argument of the carrier or the modulator, and o2_ARG or o3_ARG to that of a
    routed recipe's operator 2 or 3, in op units: dB for level and s
    (below peak, so + is quieter), radians for index and fb, ms for a, d and r,
    1/256 for vel, Hz for ks_break and dB per octave for ks_slope (no recipe
    sets key scaling, so m_ks_break=698.46, m_ks_slope=12 turns it on above
    F5), and for the operator extras steps for rs, Hz for fixed, degrees for phase
    and 0, 1 or 2 for free. c_cents, m_cents, o2_cents and o3_cents detune that
    operator's ratio, and wF_T adds to the weight of a routed recipe's edge F>T,
    in 1/256 (256 is unity). A field the engine ignores there (ignored) takes
    no tweak, nor does a sync edge's weight, which is 256. A value beyond its
    field's range stops at the end of it, a weight at 1 or 1024 for PM and 1 or
    256 for AM/ring. A Fraction delta (randomize_scoped's) is added exactly, from the
    field's decimal spelling; an int or float delta is added as before."""
    from fractions import Fraction
    car, mod, text, *metadata = RECIPES[name]
    metadata = metadata[0] if metadata else {}
    metadata = dict(metadata)
    routing = metadata if 'ops' in metadata else None
    count = metadata.get('ops', 2)
    if not isinstance(mute, (list, tuple)) or any(type(i) is not int or not 0 <= i < count for i in mute) or len(set(mute)) != len(mute):
        raise TypeError('recipe %s: mute needs distinct present operator numbers' % name)
    muted = set(mute)
    carriers = metadata.get('carriers', 1)
    if solo is not None:
        if type(solo) is not int or not 0 <= solo < count or not carriers >> solo & 1 or solo in muted:
            raise TypeError('recipe %s: solo needs an unmuted carrier' % name)
        muted.update(i for i in range(count) if carriers >> i & 1 and i != solo)
    if muted:
        remaining = carriers & ~sum(1 << i for i in muted)
        if not remaining:
            raise TypeError('recipe %s: mute/solo leaves no carrier' % name)
        if 'vector' in metadata and remaining != 15:
            raise TypeError('recipe %s: muting a vector carrier would disable its exact four-carrier route' % name)
        for f, _, _, kind in map(edge_values, metadata.get('edges', ())):
            if f in muted and kind != 'pm':
                raise TypeError('recipe %s: mute operator %d cannot silence an outgoing %s edge with index/fb' % (name, f, kind))
        metadata['muted'] = tuple(sorted(muted))
        if routing:
            metadata['carriers'] = remaining
        routing = metadata
    ops = {'c': dict(OP_ARGS, **car), 'm': dict(OP_ARGS, **mod)}
    weights, sync_weights = {}, set()
    if routing and 'ops' in routing:
        ops.update(('o%d' % i, dict(OP_ARGS, **args)) for i, args in enumerate(routing['more'], 2))
        weights = {'w%d_%d' % (f, t): w for f, t, w, _ in map(edge_values, routing['edges'])}
        sync_weights = {'w%d_%d' % (f, t) for f, t, _, kind in map(edge_values, routing['edges']) if kind == 'sync'}
    for i in muted:
        side = ('c', 'm', 'o2', 'o3')[i]
        ops[side]['index'] = ops[side]['fb'] = 0
    for o in ops.values():
        for k in 'adr':
            o[k] *= time
    for key, delta in tweaks.items():
        if key in sync_weights:
            raise TypeError('recipe %s: %s changes nothing; a sync WEIGHT is 256' % (name, key))
        if key in weights:
            if any(int(x) in muted for x in key[1:].split('_')):
                raise TypeError('recipe %s: %s changes nothing; its edge touches a muted operator' % (name, key))
            weights[key] += delta
            continue
        side, _, arg = key.partition('_')
        if side not in ops or (arg not in OP_ARGS and arg != 'cents'):
            raise TypeError('recipe %s: no tweak %s' % (name, key))
        why = ignored(side, arg, routing)
        if why:
            raise TypeError('recipe %s: %s changes nothing; %s' % (name, key, why))
        if arg != 'cents' and isinstance(delta, Fraction):  # randomize_scoped's exact deltas stay exact
            ops[side][arg] = Fraction(decimal_value(ops[side][arg])) + delta
        elif arg != 'cents':
            ops[side][arg] += delta
    for side, o in ops.items():
        o['ratio'] = pitch_scale(o['ratio'], tweaks.get(side + '_cents', 0), 1200)
        if o['wave'] not in OP_WAVES:
            raise TypeError('recipe %s: %s wave %s is not supported; choose %s' %
                            (name, side, o['wave'], ', '.join(map(str, OP_WAVES))))
        for k, (lo, hi) in LIMITS.items():
            o[k] = min(hi, max(lo, o[k]))
            if isinstance(lo, int):
                o[k] = round(o[k])
    said = (['time %g' % time] if time != 1.0 else []) + ['%s %+g' % kv for kv in tweaks.items()]
    if muted:
        said.append('mute ' + ','.join(map(str, sorted(muted))))
    if solo is not None:
        said.append('solo %d' % solo)
    comment = 'recipe %s%s: %s' % (name, ' (%s)' % ', '.join(said) if said else '', text)
    extra = {}
    if routing and 'ops' in routing:
        edges = []
        for f, t, _, kind in map(edge_values, routing['edges']):
            weight = min(1024 if kind == 'pm' else 256, max(1, round(weights['w%d_%d' % (f, t)])))
            fields = (f, t, weight, kind)
            edges.append(fields[:3] if kind == 'pm' else fields)
        extra = {'ops': routing['ops'], 'carriers': routing['carriers'], 'edges': edges,
                 'more': [op(**ops['o%d' % i]) for i in range(2, routing['ops'])]}
    for kind in ('filter', 'shape', 'amp', 'lfo', 'peg', 'fenv', 'vector'):
        if kind in metadata:
            extra[kind] = metadata[kind]
    for kind in ('kstype', 'fbtype', 'egloop'):
        if kind in metadata:
            extra[kind] = dict(metadata[kind])
    if 'eg' in metadata:
        extra['eg'] = {i: (min(60000, max(0, round(delay * time))),
                           min(60000, max(0, round(decay2 * time))), sustain2)
                       for i, (delay, decay2, sustain2) in metadata['eg'].items()}
        for i, values in extra['eg'].items():
            try:
                eg_values((i, *values), sustain1=round(ops['c' if i == 0 else 'm' if i == 1 else 'o%d' % i]['s'] * 10))
            except ValueError as e:
                raise TypeError('recipe %s: %s' % (name, e)) from None
    if 'ks' in metadata:
        extra['ks'] = dict(metadata['ks'])
        for i, value in extra['ks'].items():
            try:
                ks_values((i, value), routing=routing)
            except ValueError as e:
                raise TypeError('recipe %s: %s' % (name, e)) from None
    if 'noise' in metadata:
        extra['noise'] = dict(metadata['noise'])
        for i, value in extra['noise'].items():
            try:
                noise_values((i, value), wave=ops['c' if i == 0 else 'm' if i == 1 else 'o%d' % i]['wave'])
            except ValueError as e:
                raise TypeError('recipe %s: %s' % (name, e)) from None
    if 'vector' in extra:
        vector_values(extra['vector'],extra.get('ops',2),extra.get('carriers',1))
    if 'formant' in metadata:
        extra['formant'] = {i: formant_values((i, *fields))[1:] for i, fields in metadata['formant'].items()}
    if 'pd' in metadata:
        extra['pd'] = dict(metadata['pd'])
        for i,knee in extra['pd'].items():
            try:
                pd_values((i,knee),wave=ops['c' if i == 0 else 'm' if i == 1 else 'o%d' % i]['wave'])
            except ValueError as e:
                raise TypeError('recipe %s: %s' % (name,e)) from None
    if 'wtbanks' in metadata or 'wtuse' in metadata:
        extra['wtbanks'], extra['wtuse'] = wt_metadata(name, [op(**ops[k]) for k in ops], dict(extra,
            wtbanks=metadata.get('wtbanks', {}), wtuse=metadata.get('wtuse', {})))
    if muted:
        extra['muted'] = tuple(sorted(muted))
    if 'mod' in metadata:
        extra['mod'] = mod_settings(metadata['mod'], extra)
    if 'macros' in metadata:
        extra['macros'] = {n: dict(v) for n, v in metadata['macros'].items()}
        macro_budget(extra.get('mod', {}), extra['macros'], [],
                     [op(**ops[k]) for k in ops], extra)
    if not isinstance(templates, (tuple, list)) or any(not isinstance(t, str) for t in templates):
        raise TypeError('recipe %s: templates needs a list or tuple of names' % name)
    for template in templates:
        extra = macro_template(template, MACRO_TEMPLATES, [op(**ops[k]) for k in ops], extra)
    raw = [op(**ops[k]) for k in ops]
    if carson is not None:
        settings, fs, onsets = _carson_fields(name, raw, extra, carson, tweaks)
        for i, (breakpoint, slope) in settings.items():
            raw[i][9:11] = [breakpoint, slope]
        if 'ops' in extra:
            extra['more'] = raw[2:]
        comment += '; Carson ideal fs=%d onsets_millihz=%s' % (fs, ','.join(map(str,onsets)))
    return raw[0], raw[1], comment, extra


def randomize(name, seed, ranges):
    """Deltas for recipe(name, **deltas), drawn at random but the same for the same seed
    : ranges maps a tweak that recipe takes, c_ARG or m_ARG for an ARG of LIMITS or
    c_cents or m_cents, to (lo, hi) in its units, and each delta is drawn evenly from lo
    to hi by the fixed LCG (Rng) seeded from mix32(seed), one draw a name in the sorted
    order of the names, never from Python's global random state . A delta that would
    take its field past LIMITS stops at the limit, so the patch comment tells the delta
    that took effect; an integer field's delta is a whole number. A routed recipe's
    o2_ARG and o3_ARG draw the same way; its weights (wF_T) take no range."""
    car, mod, _, *routing = RECIPES[name]
    routing = routing[0] if routing else None
    base = {'c': dict(OP_ARGS, **car), 'm': dict(OP_ARGS, **mod)}
    if routing and 'ops' in routing:
        base.update(('o%d' % i, dict(OP_ARGS, **args)) for i, args in enumerate(routing['more'], 2))
    rng, out = Rng(mix32(seed)), {}
    for key in sorted(ranges):
        lo, hi = ranges[key]
        side, _, arg = key.partition('_')
        if side not in base or (arg not in LIMITS and arg != 'cents') or ignored(side, arg, routing) or not lo <= hi:
            raise ValueError('randomize %s: %s (%r, %r) is no range of a tweak that recipe() takes' % (name, key, lo, hi))
        d = lo + (hi - lo) * rng.next()
        if arg == 'cents':                          # the ratio stays within its field
            ratio = base[side]['ratio']
            if ratio > 0:
                with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
                    limit = decimal_value(LIMITS['ratio'][1]) / decimal_value(ratio)
                    d = min(d, float(1200 * limit.ln() / decimal.Decimal(2).ln()))
        else:
            a, b = LIMITS[arg]
            v = base[side][arg]
            d = min(b - v, max(a - v, d))
            if isinstance(a, int):
                d = int(round(d))
        out[key] = d
    return out


SCOPED_UNITS = {'level': 10, 's': 10, 'index': 1024, 'fb': 1024, 'vel': 1}   # op's record units, tables.txt spans


def randomize_scoped(name, seed, scope='all', amount=None, preset=None, ratios=None, lock=()):
    """Deltas for recipe(name, **deltas) at time 1.0, drawn from the seed within a scope.
    scope is a scope of tables.txt: 'operators' (ratio, level, index, fb, vel), 'envelopes'
    (a, d, s, r) or 'all' (both). Algorithm, filter and matrix randomization are
    unsupported. amount is a whole percent, 0 to 100, or preset a preset of tables.txt
    (mild 15, aggressive 60); neither: mild. Each field of the scope that the recipe's engine
    reads (ignored) and that lock does not name (a tweak such as 'c_level', or an argument
    such as 'level' for every operator) draws one u = x / 2^32 from its own stream,
    Rng(mix32(mix32(seed) + k)) with k its number in tables.txt's append-only field list, so
    adding a field moves no other field's delta. In record units (SCOPED_UNITS): level, s,
    index, fb and vel move by (2u - 1) x amount / 100 x span (tables.txt), rounded half up;
    a, d and r scale by 2^((2u - 1) x amount / 100) through pitch_scale , rounded half
    up; each result is clamped to LIMITS. A ratio moves only with ratios, a set of ratios
    (ints, Fractions, Decimals or decimal strings, 0 to 65535.99; a float is refused), and
    an amount above 0: it becomes the sorted set's entry floor(u x len). Only whole numbers,
    Fractions and Decimals are used, never a float or math . Returns {tweak: delta}
    for the fields that move, in field order, each delta an int or a Fraction, so amount 0
    gives {} and recipe writes the recipe as it is, ratios and routing included."""
    from fractions import Fraction
    what = 'randomize_scoped %s' % (name,)
    if name not in RECIPES:
        raise ValueError('%s: no recipe of that name' % what)
    if type(seed) is not int:
        raise ValueError('%s: the seed is a whole number, not %r' % (what, seed))
    if scope == 'algorithms':
        raise ValueError("%s: algorithm randomization is unsupported; recipe routing remains explicit" % what)
    if scope in ('filter', 'matrix'):
        raise ValueError('%s: randomization scope %r is unsupported' % (what, scope))
    if scope not in TABLES['scope']:
        raise ValueError('%s: no scope %r; the scopes are %s' % (what, scope, ', '.join(TABLES['scope'])))
    if amount is not None and preset is not None:
        raise ValueError('%s: give an amount or a preset, not both' % what)
    if amount is None:
        if preset is None:
            preset = 'mild'
        if preset not in TABLES['preset']:
            raise ValueError('%s: no preset %r; the presets are %s' % (what, preset, ', '.join(TABLES['preset'])))
        amount = TABLES['preset'][preset]
    if type(amount) is not int or not 0 <= amount <= 100:
        raise ValueError('%s: the amount is a whole percent, 0 to 100, not %r' % (what, amount))
    choices = None
    if ratios is not None:
        try:                                                # each element's type first: an equal int hides 2.0 or True from set
            ok = isinstance(ratios, (list, tuple, set, frozenset)) and all(
                isinstance(r, (int, str, decimal.Decimal, Fraction)) and not isinstance(r, bool) for r in ratios)
            choices = sorted({Fraction(r) for r in ratios}) if ok else None
            ok = ok and choices and len(choices) == len(set(ratios))
        except (ValueError, TypeError, OverflowError, decimal.InvalidOperation):
            ok = False
        if not ok or not 0 <= choices[0] or choices[-1] > Fraction(decimal_value(LIMITS['ratio'][1])):
            raise ValueError('%s: ratios is a set of ratios 0 to %s as ints, Fractions, Decimals or decimal '
                             'strings, not %r' % (what, LIMITS['ratio'][1], ratios))
    lock = (lock,) if isinstance(lock, str) else tuple(lock)
    for k in lock:
        if k not in TABLES['field'] and k not in TABLE_ARGS:
            raise ValueError('%s: lock names %r, which is no field (%s, ...) and no argument of %s'
                             % (what, k, ', '.join(TABLES['field'][:3]), ', '.join(TABLE_ARGS)))
    car, mod, _, *meta = RECIPES[name]
    meta = meta[0] if meta else {}
    routing = meta if 'ops' in meta else None
    base = {'c': dict(OP_ARGS, **car), 'm': dict(OP_ARGS, **mod)}
    if routing:
        base.update(('o%d' % i, dict(OP_ARGS, **args)) for i, args in enumerate(routing['more'], 2))
    out, top = {}, 1 << 32
    for k, field in enumerate(TABLES['field']):
        side, _, arg = field.partition('_')
        if (arg not in TABLES['scope'][scope] or side not in base or ignored(side, arg, routing)
                or field in lock or arg in lock or not amount or (arg == 'ratio' and choices is None)):
            continue
        x = (mix32((mix32(seed) + k) & 0xffffffff) * 1664525 + 1013904223) & 0xffffffff   # Rng(...).next x 2^32
        v = Fraction(decimal_value(base[side][arg]))
        if arg == 'ratio':
            new = choices[x * len(choices) >> 32]
        elif arg in ('a', 'd', 'r'):
            lo, hi = LIMITS[arg]
            new = min(hi, max(lo, half_up(pitch_scale(v, (2 * x - top) * amount, top * 100))))
        else:
            unit, (lo, hi) = SCOPED_UNITS[arg], LIMITS[arg]
            base_u = half_up(v * unit)
            move = half_up(Fraction((2 * x - top) * amount * TABLES['span'][arg], top * 100))
            lo_u = -(-Fraction(decimal_value(lo)) * unit // 1)
            hi_u = Fraction(decimal_value(hi)) * unit // 1
            new_u = min(hi_u, max(lo_u, base_u + move))
            if new_u == base_u:
                continue
            new = Fraction(new_u, unit)
        delta = Fraction(new) - v
        if delta:
            out[field] = int(delta) if delta.denominator == 1 else delta
    return out


def op_text(args):
    """The op call for these arguments, as the pieces write it."""
    lead = [repr(float(args.get('ratio', 1.0)))] + ([repr(args['level'])] if 'level' in args else [])
    return 'op(%s)' % ', '.join(lead + ['%s=%r' % (k, args[k]) for k in OP_ARGS if k in args and k not in ('ratio', 'level')])


def list_recipes():
    """Every recipe as op calls and as timeline patch lines, which a hand-made .tl can take;
    a routed recipe's operators 2 and up and its route statement follow ."""
    for name, (car, mod, text, *routing) in RECIPES.items():
        print('# %s: %s\n#   %s\n#   %s' % (name, text, op_text(car), op_text(mod)))
        for args in routing[0].get('more', ()) if routing else ():
            print('#   %s' % op_text(args))
        car, mod, _, *extra = recipe(name)
        print('\n'.join(patch_headers(name, car, mod, '', *extra)))


def patch_lines(name, car, mod, comment='', extra=None, level=None, base=None):
    """The timeline lines of p.patch(name, car, mod, comment, extra) as (rows, route): the
    patch rows of operator 0, with the comment, and operator 1, then a routed recipe's
    operators 2 and up (extra, recipe's fourth item), and its route statement, '' for
    none . Filter, LFO, PEG and fenv metadata are written by patch_headers, not this pair of
    results. level, when given, is operator 0's level in dB/10, and the route's
    other carriers move by as much as it moves from base, their level at p.patch: the
    leveller shifts every carrier by one amount (finish)."""
    shift = level - base if level is not None and base is not None else 0
    rows = []
    for i, o in enumerate([car, mod] + (list(extra.get('more', ())) if extra else [])):
        o = list(o)
        if i == 0 and level is not None:
            o[1] = level
        elif i and extra and extra.get('carriers', 1) >> i & 1:
            o[1] = min(1200, max(0, o[1] + shift))
        rows.append(patch_row(name, i, o, ('  # ' + comment) if i == 0 and comment else ''))
    route = ''
    if extra and 'ops' in extra:
        route = 'route %s %d %d' % (name, extra['ops'], extra['carriers']) + ''.join(' ' + edge_text(e, extra['ops']) for e in extra['edges'])
    return rows, route


def patch_headers(name, car, mod, comment='', extra=None, level=None, base=None):
    """All patch headers, with shape after filter and before the control extensions."""
    rows, route = patch_lines(name, car, mod, comment, extra, level, base)
    if route:
        rows.append(route)
    if extra and 'filter' in extra:
        rows.append(filter_row(name, *extra['filter']))
    if extra and 'shape' in extra:
        rows.append(shape_row(name, *extra['shape']))
    if extra and 'amp' in extra:
        rows.append(amp_row(name, *extra['amp']))
    if extra and 'lfo' in extra:
        rows.append(lfo_row(name, *lfo_values(extra['lfo'], extra.get('ops', 2))))
    if extra and 'peg' in extra:
        rows.append(peg_row(name, *extra['peg'], ops=extra.get('ops', 2)))
    if extra and 'fenv' in extra:
        if 'filter' not in extra:
            raise ValueError('patch %s: fenv needs a filter statement' % name)
        rows.append(fenv_row(name, *extra['fenv']))
    if extra:
        for slot, fields in sorted(mod_settings(extra.get('mod', {}), extra).items()):
            rows.append(mod_row(name, slot, *fields))
        for i, values in sorted(extra.get('eg', {}).items()):
            fields = eg_values((i, *values), extra.get('ops', 2))
            eg_values(fields, sustain1=([car, mod] + list(extra.get('more', ())))[i][7])
            if any(fields[1:]):
                rows.append(eg_row(name, *fields))
        for i, value in sorted(extra.get('ks', {}).items()):
            fields = ks_values((i, value), extra.get('ops', 2), extra if 'ops' in extra else None)
            if value:
                rows.append(ks_row(name, *fields))
        for i in range(extra.get('ops', 2)):
            for kind in ('kstype', 'fbtype', 'egloop'):
                if i in extra.get(kind, {}):
                    row = op_ext_row(kind, name, i, *extra[kind][i], ops=extra.get('ops', 2))
                    if row:
                        rows.append(row)
        for kind in ('kstype', 'fbtype', 'egloop'):
            for i, fields in extra.get(kind, {}).items():
                op_ext_values(kind, (i, *fields), extra.get('ops', 2))
        for i, value in sorted(extra.get('noise', {}).items()):
            fields = noise_values((i, value), extra.get('ops', 2))
            o = ([car, mod] + list(extra.get('more', ())))[i]
            noise_values(fields, wave=o[11] if len(o) > 11 else 0)
            if value != 4:
                rows.append(noise_row(name, *fields))
        for i,knee in sorted(extra.get('pd',{}).items()):
            fields = pd_values((i,knee),extra.get('ops',2))
            o = ([car,mod]+list(extra.get('more',())))[i]
            pd_values(fields,wave=o[11] if len(o)>11 else 0)
            rows.append(pd_row(name,*fields))
        if 'vector' in extra:
            rows.append(vector_row(name,*vector_values(extra['vector'],extra.get('ops',2),extra.get('carriers',1))))
        for slot, fields in sorted(extra.get('formant', {}).items()):
            rows.append(formant_row(name, slot, *fields))
        banks, uses = wt_metadata(name, [car, mod] + list(extra.get('more', ())), extra)
        for bank, definition in banks.items():
            rows.extend(wtbank_rows(bank, definition))
        for i, (bank, position) in sorted(uses.items()):
            rows.append('wtuse %s %d %s %d' % (name, i, bank, position))
    return rows


# ---------------------------------------------------------------------------
# The recipe statement : a timeline written by hand may name a recipe where
# its patch lines go, `recipe PATCH RECIPE [time=X] [TWEAK=DELTA ...]`, TWEAK any
# tweak recipe takes (c_ARG, m_ARG, o2_ARG, o3_ARG, the _cents ones and wF_T),
# moved as recipe moves it. compose.py --expand IN OUT writes IN with each
# statement replaced by the raw patch lines compose.py writes for
# p.patch(PATCH, *recipe(RECIPE, ...)), two, or a routed recipe's operators and its
# route statement : the saved record holds only raw patches,
# so a replay never needs recipes.txt , and raw patch lines stay accepted.
TWEAK_VALUE = re.compile(r'[-+]?\d+(\.\d+)?$')    # a delta or a time in a statement


def tl_document_module():
    """Use the shared portable document codec without changing module search paths."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("compose_tl_document", os.path.join(SPIKE, 'tools', 'tl_document.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expand_timeline(src, dst):
    """Copy the timeline src to dst, every recipe statement expanded and every other
    line, raw patches among them, as it is. A statement's own # comment goes on a
    comment line above its patch lines. The first fault exits with src's name and
    the line. Returns the number of statements expanded."""
    if os.path.abspath(src) == os.path.abspath(dst):
        sys.exit('compose.py: --expand writes OUT beside IN, never over it')
    where, out, done = shown(src), [], 0
    document = tl_document_module()
    try:
        raw = document.read_bounded(src)
        document.parse_document(raw)
    except ValueError as error:
        sys.exit('compose.py: %s:1: %s' % (where, error))
    parts = raw.decode('utf-8').split('\n')
    source_lines = [line + '\n' for line in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    # Inspect only names generated by recipe metadata; legacy raw expansion is unchanged.
    raw_banks = {}
    for n, line in enumerate(source_lines, 1):
        words = line.partition('#')[0].split()
        if len(words) > 1 and words[0] in ('wtbank', 'harmonic', 'wtdata', 'wthash'):
            raw_banks.setdefault(words[1], []).append((n, words))
    emitted = {}

    def bank_plan(bank, definition, line_number):
        if bank in emitted:
            if emitted[bank] != definition:
                raise ValueError('wavetable %s: conflicting generated definition' % bank)
            return False
        raw = raw_banks.get(bank, [])
        if raw:
            if any(n >= line_number for n, _ in raw):
                raise ValueError('wavetable %s: forward or incomplete raw definition' % bank)
            headers = [w for _, w in raw if w[0] == 'wtbank']
            if len(headers) != 1 or len(headers[0]) != 5 or headers[0][4] != '12':
                raise ValueError('wavetable %s: conflicting or incomplete raw header' % bank)
            if any(w[0] == 'wtdata' for _, w in raw):
                raise ValueError('wavetable %s: raw wtdata cannot satisfy a generated harmonic bank' % bank)
            harmonic = [w for _, w in raw if w[0] == 'harmonic']
            if any(len(w) != 6 for w in harmonic):
                raise ValueError('wavetable %s: incomplete raw harmonic definition' % bank)
            try:
                actual = wtbank_values(bank, int(headers[0][2]), int(headers[0][3]),
                                       [tuple(map(int, w[2:])) for w in harmonic])
            except ValueError as e:
                raise ValueError('wavetable %s: invalid raw definition: %s' % (bank, e)) from None
            header_line = next(n for n, w in raw if w[0] == 'wtbank')
            if any(n < header_line for n, w in raw if w[0] == 'harmonic') or actual != definition:
                raise ValueError('wavetable %s: conflicting or unordered raw definition' % bank)
        elif len(set(raw_banks) | set(emitted) | {bank}) > 8:
            raise ValueError('wavetable %s: at most 8 banks' % bank)
        emitted[bank] = definition
        return not raw

    for n, line in enumerate(source_lines, 1):
        body, _, note = line.partition('#')
        words = body.split()
        if words[:1] != ['recipe']:
            out.append(line)
            continue
        if len(words) < 3:
            sys.exit('compose.py: %s:%d: a statement is recipe PATCH RECIPE [time=X] [TWEAK=DELTA ...], '
                     'TWEAK any tweak recipe() takes' % (where, n))
        pname, name, kw = words[1], words[2], {}
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', pname):
            sys.exit('compose.py: %s:%d: a patch name is 1 to 31 of A-Z a-z 0-9 _ - .' % (where, n))
        if name not in RECIPES:
            sys.exit('compose.py: %s:%d: no recipe %r; compose.py --recipes lists them' % (where, n, name))
        for word in words[3:]:
            key, eq, text = word.partition('=')
            if not eq or not TWEAK_VALUE.match(text):
                sys.exit('compose.py: %s:%d: %r is not KEY=NUMBER' % (where, n, word))
            if key in kw:
                sys.exit('compose.py: %s:%d: %s is set twice' % (where, n, key))
            kw[key] = float(text) if '.' in text else int(text)
        if kw.get('time', 1) <= 0:
            sys.exit('compose.py: %s:%d: time scales the envelope times, so it is above 0' % (where, n))
        try:
            values = recipe(name, **kw)
            rows = patch_headers(pname, *values)
            omit = {bank for bank, definition in values[3].get('wtbanks', {}).items()
                    if not bank_plan(bank, definition, n)}
            rows = [row for row in rows if not (row.split()[0] in ('wtbank', 'harmonic') and
                                               row.split()[1] in omit)]
        except (TypeError, ValueError) as e:
            sys.exit('compose.py: %s:%d: %s' % (where, n, e))
        if note.strip():
            out.append('# %s\n' % note.strip())
        out.extend(row + '\n' for row in rows)
        done += 1
    try:
        document.parse_document(''.join(out).encode('utf-8'))
    except ValueError as error:
        sys.exit('compose.py: %s:1: %s' % (where, error))
    with open(dst, 'w', encoding='utf-8', newline='\n') as f:
        f.writelines(out)
    return done


class Pool:
    """The composer's own voice choice (the library has none, ): a voice
    free by the note-on, the one free longest; else the one freed first."""

    def __init__(self, voices, release_ms):
        self.v = list(voices)
        self.busy = {v: -1 for v in self.v}
        self.rel = release_ms * 1_000_000
        self.steals = 0

    def get(self, on, off):
        free = [v for v in self.v if self.busy[v] <= on]
        self.steals += not free
        v = min(free or self.v, key=lambda x: (self.busy[x], x))
        self.busy[v] = off + self.rel
        return v


def mix32(x):
    """A 32-bit integer hash (murmur3's finaliser), so that nearby seeds start the LCG far
    apart: the performance helpers seed Rng through it ."""
    x &= 0xffffffff
    x ^= x >> 16
    x = (x * 0x85ebca6b) & 0xffffffff
    x ^= x >> 13
    x = (x * 0xc2b2ae35) & 0xffffffff
    return x ^ (x >> 16)


def step_value(x, i, default):
    """A performance helper's argument at step i: one value for every step, a list or tuple
    taken by step number (cycled), or a dict of step number to value, default elsewhere;
    None is default everywhere. An empty list or tuple gives no step a value: ValueError."""
    if x is None:
        return default
    if isinstance(x, dict):
        return x.get(i, default)
    if isinstance(x, (list, tuple)):
        if not x:
            raise ValueError('step %d takes its value from an empty list: a per-step argument is one value, a '
                             'list of one or more (cycled) or a dict of step number to value' % i)
        return x[i % len(x)]
    return x


def cond_holds(c, p, fill, *, pre=False, nei=False):
    """Whether a conditional trig's condition c holds on pass p,
    passes counted from 0 and fill the set of pass numbers that are fills: None always;
    'first' p = 0; 'not-first' p > 0; 'A:B' (2 <= B <= 8, 1 <= A <= B; A and B one digit
    each) p mod B = A - 1, pass A of every B; 'fill' p in fill; 'not-fill' p not in fill.
    PRE reads the latest earlier conditional step's played bit in this pass;
    NEI reads the supplied earlier call's same global step. Contexts are bools.
    Integers only. Any other value: ValueError."""
    if type(pre) is not bool or type(nei) is not bool:
        raise ValueError('condition pre and nei contexts must be booleans')
    if c is None:
        return True
    if isinstance(c, str):
        if c in ('pre', 'not-pre', 'nei'):
            return nei if c == 'nei' else pre if c == 'pre' else not pre
        if c == 'first':
            return p == 0
        if c == 'not-first':
            return p > 0
        if c == 'fill':
            return p in fill
        if c == 'not-fill':
            return p not in fill
        if re.fullmatch('[1-8]:[2-8]', c) and int(c[0]) <= int(c[2]):
            return p % int(c[2]) == int(c[0]) - 1
    raise ValueError('no condition %r; the conditions are None, first, not-first, A:B (2 <= B <= 8, 1 <= A <= B), '
                     'fill, not-fill, pre, not-pre and nei' % (c,))


SLIDE_POINTS = 8                                            # : a slide's points a step, provisional (P row of regression)


def ctl_locks(lock, what):
    """A steps 'ctl' lock  as [(N, VALUE_Q15)] in rising N: (N, VALUE_Q15), or a list
    or tuple of 1 to 8 such pairs, N 0 to 7 once each and VALUE_Q15 -32768 to 32768, both
    whole numbers (int, not bool), the bounds of Piece.ctl. Anything else: ValueError, so a
    ninth lock on a step (a repeated N) is refused."""
    def pair(x):
        return (isinstance(x, (list, tuple)) and len(x) == 2 and all(type(y) is int for y in x)
                and 0 <= x[0] <= 7 and -32768 <= x[1] <= 32768)
    one = isinstance(lock, (list, tuple)) and len(lock) == 2 and all(type(y) is int for y in lock)
    pairs = [lock] if one else list(lock) if isinstance(lock, (list, tuple)) else []
    if not pairs or len(pairs) > 8 or not all(pair(x) for x in pairs) or len(set(x[0] for x in pairs)) != len(pairs):
        raise ValueError('%s: ctl lock %r; a ctl lock is (N, VALUE_Q15) or a list of up to 8 of them, N 0 to 7 '
                         'once each and VALUE_Q15 -32768 to 32768, as integers' % (what, lock))
    return sorted((x[0], x[1]) for x in pairs)

def _arp_ordered(given, mode, octaves):
    """Order (note, opaque payload) pairs; equal notes keep the caller's order."""
    held = sorted(given, key=lambda e: e[0])       # stable: equal notes keep their given order
    up = [(m + 12 * o, hv) for o in range(int(octaves)) for m, hv in held]
    order = {'up': up, 'down': up[::-1], 'updown': up + up[-2:0:-1], 'downup': up[::-1] + up[1:-1],
             'played': [(m + 12 * o, hv) for o in range(int(octaves)) for m, hv in given],
             'chord': up, 'random': up}[mode]
    return held, up, order


def _arp_key_steps(presses, steps, *, mode, octaves, latch, seed):
    """Select held presses without a Piece, voice allocation or event writes.

    Inputs are already validated: presses=(on_ns, off_ns, note, velocity, id),
    steps=(on_ns, opaque step payload) in onset order. Yield
    (on_ns, step payload, [(note, velocity, press id), ...]). The private LCG
    starts from seed itself; callers apply their existing seed mixing first.
    Neither input is changed. Release groups precede press groups; latch
    retains the complete final pre-release set, never previously released keys.
    """
    transitions, values = [], {}
    for k, (on, off, note, velocity, identity) in enumerate(presses):
        transitions.extend(((on, 1, k), (off, 0, k)))
        values[k] = (note, (velocity, identity))
    transitions.sort()
    rng, given, down, cursor = Rng(seed), [], {}, 0
    for i, (t0, payload) in enumerate(steps):
        while cursor < len(transitions) and transitions[cursor][0] <= t0:
            stamp = transitions[cursor][0]
            while cursor < len(transitions) and transitions[cursor][:2] == (stamp, 0):
                del down[transitions[cursor][2]]
                cursor += 1
            if down or not latch:
                given = list(down.values())
            while cursor < len(transitions) and transitions[cursor][:2] == (stamp, 1):
                down[transitions[cursor][2]] = values[transitions[cursor][2]]
                given = list(down.values())
                cursor += 1
        held, up, order = _arp_ordered(given, mode, octaves)
        if mode == 'random':
            draw = rng.next()                        # one draw per step, including an empty held set
            selected = [up[int(draw * len(up))]] if up else []
        elif mode == 'chord':
            selected = [(m + 12 * (i % int(octaves)), source) for m, source in held]
        else:
            selected = [order[i % len(order)]] if order else []
        yield t0, payload, [(m, source[0], source[1]) for m, source in selected]


def chord_shape(notes):
    """Chord memory: a list of notes (names or MIDI numbers)
    stored as its shape, sorted(set(n - min(notes))), a tuple of whole semitone offsets
    rising from 0, at most 8 of them, for Piece.chord. An empty list, offsets that are not
    whole semitones or more than 8 distinct offsets: ValueError."""
    ms = [as_midi(n) for n in notes]
    offs = sorted(set(m - min(ms) for m in ms)) if ms else []
    if not offs or len(offs) > 8 or any(o != int(o) for o in offs):
        raise ValueError('a chord shape is 1 to 8 distinct whole semitone offsets; %r gives %r' % (notes, offs))
    return tuple(int(o) for o in offs)


def as_midi(n):
    """A note name or a MIDI number (a fraction allowed) as a MIDI number."""
    return midi(n) if isinstance(n, str) else float(n)


def scale_mask(scale):
    """The scale lock's (MASK, ROOT) for scale: a preset name of
    tables.txt or a mask 0 to 4095 (bit k the pitch class root + k; 0 is off), alone with
    root 0 or as a pair (NAME or MASK, ROOT), ROOT 0 to 11 (C = 0). Anything else: ValueError."""
    sc, root = scale if isinstance(scale, (tuple, list)) and len(scale) == 2 else (scale, 0)
    mask = TABLES['scale'].get(sc) if isinstance(sc, str) else sc
    if type(mask) is not int or not 0 <= mask <= 4095 or type(root) is not int or not 0 <= root <= 11:
        raise ValueError('scale %r: a scale is a name of tables.txt (%s, ...) or a mask 0 to 4095, alone or as '
                         '(scale, root) with root 0 to 11' % (scale, ', '.join(list(TABLES['scale'])[:3])))
    return mask, root


def scale_lock(notes, scale, root=0):
    """The scale lock: each note (a name or a whole MIDI number,
    0 to 127) to the largest n' <= n whose bit ((n' - root) mod 12) of the mask is set, the
    snap down of gen_quantize over the mask rotated by root: gen_quantize(100 n, rot(mask,
    root)) / 100. scale is a preset name of tables.txt or a mask, 0 off (scale_mask). A list
    or tuple gives a list, one note one int. A note outside 0 to 127, not whole, or whose
    snap would fall below 0: ValueError."""
    mask, root = scale_mask((scale, root))
    def one(n):
        m = midi(n) if isinstance(n, str) else n            # checked as given, before any float (59.99...9 is no 60)
        try:
            whole = 0 <= m <= 127 and m == int(m)
        except (TypeError, ValueError, OverflowError, ArithmeticError):
            whole = False
        if not whole:
            raise ValueError('scale_lock: the note %r is not a whole MIDI number 0 to 127' % (n,))
        m = int(m)
        if mask:
            while not mask >> ((m - root) % 12) & 1:
                m -= 1
            if m < 0:
                raise ValueError('scale_lock: the note %r would snap below 0' % (n,))
        return m
    return [one(n) for n in notes] if isinstance(notes, (list, tuple)) else one(notes)


def scale_locked(scale, what):
    """A helper's scale= argument as a note function: None the note as it is, else the scale
    lock of scale (a name or a mask, or (scale, root); scale_mask), as a float MIDI number,
    checked before the helper writes anything."""
    if scale is None:
        return lambda m: m
    try:
        mask, root = scale_mask(scale)
    except ValueError as e:
        raise ValueError('%s: %s' % (what, e)) from None
    def one(m):
        try:
            return float(scale_lock(m, mask, root))
        except ValueError as e:
            raise ValueError('%s: %s' % (what, e)) from None
    return one


class RoundRobin:
    """The performance helpers' voice choice : the voices in turn, passing over one
    still busy at the note-on; when every voice is busy it stops with an error instead of
    stealing one. Notes must come in time order. A voice is busy from its note-on until
    window(release_ms) after its note-off. With release_ms its longest carrier's release (the
    helpers' default), that is after the release from any level has ended, so a voice
    handed back is idle and its next note-on starts it from silence, never as a
    retrigger; a shorter release_ms, 0 included, can hand back a voice still releasing."""

    def __init__(self, voices, release_ms):
        self.v = list(voices)
        self.busy = {v: -1 for v in self.v}
        self.rel = self.window(release_ms)
        self.k = 0

    @staticmethod
    def window(release_ms):
        """How long in ns a voice stays busy after its note-off: ceil(1.25 x release_ms)
        + 1 ms. The engine's release falls 16 octaves (96.3 dB) per release_ms and goes
        idle 20 octaves (120.4 dB) down, so it lasts 1.25 x release_ms from the peak and
        less from any lower level: (120.4 - s) / 96.3 x release_ms from a sustain s dB
        down, less again from part way up an attack (the music README, Envelope). The
        1 ms covers the rounding of times and of the release rate to samples at the
        music build (ENV64, )."""
        import math
        return (math.ceil(1.25 * release_ms) + 1) * 1_000_000

    def get(self, on, off):
        n = len(self.v)
        for i in range(n):
            v = self.v[(self.k + i) % n]
            if self.busy[v] <= on:
                self.k = (self.k + i + 1) % n
                self.busy[v] = off + self.rel
                return v
        raise ValueError('all %d voices %s still sound or release at %d ns: give the helper more voices, a '
                         'shorter gate or a shorter release_ms' % (n, self.v, on))


def carrier_attenuation_ctl(attenuation, purpose="crossfade"):
    """Exact inverse of the modulation matrix amount1200 contribution for integer dB10."""
    if type(attenuation) is not int or not 0 <= attenuation <= 1200:
        raise ValueError('%s attenuation must be an integer0..1200' % purpose)
    return (attenuation * 32768 + 1199) // 1200


def carrier_attenuation_settings(name, operators, extra, reserved, fs, *, purpose="crossfade", owned=None):
    """Pure eligibility and lowest-free slot/input allocation ."""
    def refuse(reason):
        raise ValueError('%s patch %s: %s' % (purpose, name, reason))
    for kind in ('shape', 'filter', 'fenv', 'formant', 'amp'):
        if kind in extra:
            refuse('%s is outside the proved carrier-level path%s' %
                   (kind, ' ()' if kind == 'amp' else ''))
    mask = extra.get('carriers', 1)
    carriers = [i for i in range(len(operators)) if mask >> i & 1]
    mods = mod_settings(extra.get('mod', {}), extra, fs)
    if owned is not None:
        n, rows = owned
        slots = [slot for slot, _ in rows]
        expected = [('ctl%d' % n, 'level%d' % i, 1200, 0) for i in carriers]
        if ([fields for _, fields in rows] != expected or len(set(slots)) != len(slots)
                or any(mods.get(slot) != fields for slot, fields in rows)):
            refuse('owned carrier slots or topology changed')
        mods = {slot: fields for slot, fields in mods.items() if slot not in slots}
        if n in reserved or any(source == 'ctl%d' % n for source, _, _, _ in mods.values()):
            refuse('owned ctl%d conflicts with authored controls' % n)
    macro_budget(mods, extra.get('macros', {}), [], operators, extra, fs)
    lfo = lfo_values(extra['lfo'], len(operators)) if 'lfo' in extra else None
    lfo_level = bool(lfo and lfo[1] and ((lfo[9] if len(lfo) >= 11 else (1 << len(operators)) - 1) & mask))
    if lfo_level and (lfo[5] or any(target == 'lfo_level' and amount
                                  for _, target, amount, _ in mods.values())):
        refuse('active carrier LFO level modulation')
    for i in carriers:
        if any(target == 'level%d' % i and amount for _, target, amount, _ in mods.values()):
            refuse('existing level%d modulation' % i)
        if extra.get('kstype', {}).get(i, ('index', '-', '-'))[0] in ('level', 'both'):
            if operators[i][10] or extra.get('ks', {}).get(i, 0):
                refuse('operator%d level key scaling' % i)
    used = set(reserved)
    used.update(int(source[3:]) for source, _, _, _ in mods.values() if source.startswith('ctl'))
    used.update(spec['input'] for spec in extra.get('macros', {}).values())
    inputs, slots = [n for n in range(8) if n not in used], [n for n in range(8) if n not in mods]
    if owned is not None:
        inputs, slots = [owned[0]], [slot for slot, _ in owned[1]]
    if not inputs:
        refuse('no free ctl input')
    if len(slots) < len(carriers):
        refuse('needs %d free mod slots, has %d' % (len(carriers), len(slots)))
    n = inputs[0]
    for slot, i in zip(slots, carriers):
        mods[slot] = ('ctl%d' % n, 'level%d' % i, 1200, 0)
    result = dict(extra, mod=mods)
    macro_budget(mods, result.get('macros', {}), [], operators, result, fs)
    return result, n



def carrier_attenuation_levels(name, patch, route, level, maximum, *, vector=False, purpose='crossfade'):
    """ actual writer-level/no-clamp proof, shared by MIDI and unison."""
    car, mod, comment = patch
    extra, base = route
    rows, _ = patch_lines(name, car, mod, comment, extra, level, base)
    operators = [car, mod] + (list(extra.get('more', ())) if extra else [])
    carriers = [i for i in range(len(operators)) if (extra or {}).get('carriers', 1) >> i & 1]
    lower = 0 if vector else (0, 1, 36, 61)[len(carriers) - 1]
    shift = level - base if level is not None and base is not None else 0
    for i in carriers:
        wanted = level if i == 0 and level is not None else operators[i][1] + (shift if i else 0)
        written = int(rows[i].split('#', 1)[0].split()[4])
        if written != wanted or not lower <= written <= 1200 - maximum:
            raise ValueError('%s patch %s: carrier%d written level%d must be unclamped and'
                             ' in %d..%d dB10 (maximum attenuation%d)' %
                             (purpose, name, i, written, lower, 1200 - maximum, maximum))


def unison_attenuation(n):
    """ opt-in 20*log10(N) dB, in integer dB10, isolated Decimal50."""
    if type(n) is not int or not 1 <= n <= 8:
        raise ValueError('unison compensation needs integer n1..8')
    with decimal.localcontext(decimal.Context(prec=50, rounding=decimal.ROUND_HALF_UP)):
        return int((200 * decimal.Decimal(n).log10()).to_integral_value(rounding=decimal.ROUND_HALF_UP))


def _perf_atomic(helper):
    """Validate a whole arp/steps call on detached scheduling state before publishing it.
    The helpers only append voices/events and change their round robins; patch/header
    dictionaries and the caller's note/lock arguments are read-only. Their Rng is local
    to each call, so a refusal also leaves a later seeded call at the same first draw.
    Existing lists, rings and busy dictionaries keep their identities on success."""
    import functools

    @functools.wraps(helper)
    def checked(self, *args, **kwargs):
        import copy
        pending = copy.copy(self)
        pending.voices = list(self.voices)
        pending.ev = {s: list(events) for s, events in self.ev.items()}
        if hasattr(self, 'rings'):
            pending.rings = {}
            for key, ring in self.rings.items():
                staged = copy.copy(ring)
                staged.v, staged.busy = list(ring.v), dict(ring.busy)
                pending.rings[key] = staged
        result = helper(pending, *args, **kwargs)
        self.voices.extend(pending.voices[len(self.voices):])
        for s, events in self.ev.items():
            events.extend(pending.ev[s][len(events):])
        self.seq = pending.seq
        if hasattr(pending, 'rings'):
            if not hasattr(self, 'rings'):
                self.rings = {}
            for key, staged in pending.rings.items():
                if key not in self.rings:
                    self.rings[key] = staged
                else:
                    ring = self.rings[key]
                    ring.busy.clear()
                    ring.busy.update(staged.busy)
                    ring.rel, ring.k = staged.rel, staged.k
        return result
    return checked


def _morph_value(a, b, u):
    """ interpolation in raw integer units; signed ties round upward."""
    if any(type(v) is not int for v in (a, b, u)) or not 0 <= u <= 32768:
        raise ValueError('morph needs integer endpoints and coordinate 0..32768')
    return (a * (32768 - u) + b * u + 16384) >> 15


class ComposeError(ValueError):
    """A named authoring error in the opt-in tempo and meter helpers."""


def _beat_fraction(value, name, allow_float=False):
    kinds = (int, Fraction, float) if allow_float else (int, Fraction)
    if type(value) not in kinds:
        raise ComposeError('%s must be an int or Fraction, not %s' % (name, type(value).__name__))
    try:
        return Fraction(value)
    except (ValueError, OverflowError):
        raise ComposeError('%s must be finite' % name) from None


def _tempo_round(value):
    """Nearest whole unit, ties away from zero, using integers only."""
    n, d = value.numerator, value.denominator
    return (2 * n + d) // (2 * d) if n >= 0 else -((-2 * n + d) // (2 * d))


def tempo_mhz(bpm, beats_per_cycle):
    """LFO mHz from exact BPM and quarter-note beats/cycle; ints/Fractions only.
    Refuse the exact rate outside 10..50000 before rounding half up. Rounding
    drift is (exact_mHz - returned_mHz)/10 cycles per 100 seconds, at most 0.05.
    Key sync realigns at each note; this computes a rate, not a live tempo
    follower. It moves no position; tempo-synced pan is regression's autopan .
    """
    bpm = _beat_fraction(bpm, 'bpm')
    beats_per_cycle = _beat_fraction(beats_per_cycle, 'beats_per_cycle')
    if bpm <= 0 or beats_per_cycle <= 0:
        raise ComposeError('bpm and beats_per_cycle must be positive')
    exact = 1000 * bpm / (60 * beats_per_cycle)
    if not 10 <= exact <= 50000:
        raise ComposeError('tempo LFO rate %s mHz outside 10..50000' % exact)
    return _tempo_round(exact)


class Piece:
    def __init__(self, name, bpm, beats, about, fs=48000, space=None, block=64, near=100, levels=None):
        """space='stereo' writes a draft2 record for the spatial module (docs/INSTRUMENT.md):
        pos and move events place voices, block and near are its header values. levels fixes
        the carrier levels (dB/10 per patch) instead of levelling to TARGET, so a
        spatial version of a piece keeps the mono piece's levels ."""
        self.dir = os.path.join(MUSIC, name)    # where its records go; a profile render goes under OUT
        assert 60_000_000_000 % bpm == 0
        assert space in (None, 'stereo') and block in (32, 64, 128) and 1 <= near <= 1 << 24
        self.name, self.bpm, self.beats, self.about, self.fs = name, bpm, beats, about, fs
        self.space, self.block, self.near, self.levels = space, block, near, levels
        self.beat_ns = 60_000_000_000 // bpm
        self.streams, self.roles = [], {}
        self.patches = {}      # name -> [car, mod, comment]
        self.voices = []       # (patch, comment)
        self.ev = {}           # stream -> [(t, kind, seq, voice, text)]
        self.marks = {}        # stream -> {t: text}
        self.seq = 0
        self.length_ns = None
        self.cum = None        # tempo map: start of each beat in ns
        self.fxh = {}          # the effects' header statements (fx), each written only when present
        self.filters = {}      # patch name -> (mode, cutoff_millihz, q_milli),
        self.gen_ctl = None
        self.generators, self.gen_axioms, self.gen_rules, self.gen_lanes = {}, {}, {}, {}
        self.macros = {}       # patch -> display name -> ctl/target/slot ownership, host-only
        self.snapshots = {}    # display name -> voice -> ctl -> raw Q15, host-only
        self.muted = {}        # patch -> explicitly muted operators, host-only
        self.mods = {}         # patch name -> {slot: (source, target, amount, shift)}, modulation matrix
        self.shapes = {}       # patch name -> (drive_db10, kind[, 'pre']),
        self.amps = {}         # patch name -> four enabled raw amp fields
        self.kstypes = {}      # patch -> operator -> (target, right, left), nondefault only
        self.fbtypes = {}      # patch -> operator -> (kind,), nondefault only
        self.egloops = {}      # patch -> operator -> , enabled by presence
        self.recipe_filters = set()  # filters installed by patch metadata; explicit filter calls override them
        self.routes = {}       # patch name -> (extra, operator 0's level at p.patch): a routed recipe's
        self.lfos = {}         # patch name -> seven, eleven or twelve raw integer LFO fields
        self.pegs = {}         # patch name -> nine raw integer PEG fields
        self.formants = {}    # patch -> {slot: enabled (center_millihz,q_milli,level_db10)}
        self.vectors = {}     # patch -> enabled static (x,y) coordinates
        self.wavetables = {}   # bank -> immutable (frames, length, harmonic tuples),
        self.wtuses = {}       # patch -> operator -> (bank, position_q15)
        self.ksources = {}     # patch -> operator -> (loss_q15, seed, buffer_samples),
        self.pds = {}         # patch -> {operator: enabled knee 1..32767}; absence disables
        self.noise_shifts = {} # patch name -> {operator: nondefault colour SHIFT} (operator extension)
        self.left_slopes = {}  # patch name -> {operator: left index slope_db10} (filter)
        self.egs = {}          # patch name -> {operator: (delay_ms, decay2_ms, sustain2_db10)} (filter)
        self.fenvs = {}        # patch name -> seven raw integer filter-envelope fields

    def meter(self, changes):
        """Set bar meters in quarter-note beats: (2, Fraction(7, 2)) starts 7/8 at bar 2.
        Bars are whole and 1-based; set meter before tempo_map. The initial meter is
        self.beats when no bar 1 row is supplied. Does not move existing events."""
        if hasattr(self, '_tempo_segments'):
            raise ComposeError('meter must be set before tempo_map')
        rows = []
        for row in changes:
            if not isinstance(row, (tuple, list)) or len(row) != 2:
                raise ComposeError('meter takes (bar, beats) pairs')
            bar, beats = row
            if type(bar) is not int or bar < 1 or (rows and bar <= rows[-1][0]):
                raise ComposeError('meter bars must be positive integers in increasing order')
            beats = _beat_fraction(beats, 'meter beats')
            if beats <= 0:
                raise ComposeError('meter beats must be positive')
            rows.append((bar, beats))
        if not rows or rows[0][0] != 1:
            initial = _beat_fraction(self.beats, 'initial meter beats')
            if initial <= 0:
                raise ComposeError('initial meter beats must be positive')
            rows.insert(0, (1, initial))
        self._meter_rows = tuple(rows)

    def _bar_beat(self, bar):
        if type(bar) is not int or bar < 1:
            raise ComposeError('tempo/meter bar must be a positive integer')
        rows = getattr(self, '_meter_rows', None)
        if rows is None:
            initial = _beat_fraction(self.beats, 'initial meter beats')
            if initial <= 0:
                raise ComposeError('initial meter beats must be positive')
            rows = ((1, initial),)
        total, previous, beats = Fraction(0), 1, rows[0][1]
        for start, value in rows[1:]:
            if start >= bar:
                break
            total += (start - previous) * beats
            previous, beats = start, value
        return total + (bar - previous) * beats

    @staticmethod
    def _segment_time(segment, k):
        start, end, duration, slope, elapsed = segment
        offset = k - start
        return elapsed + duration * offset + slope * offset * offset / 2

    def tempo_map(self, changes, ramp='step', *, bars):
        """Exact quarter-note timing, sized to bars. Linear interpolates beat duration
        (ns per quarter), not BPM, between bar anchors and integrates it rationally.
        Step holds the previous duration. Round cumulative times half up once; no
        per-beat rounding accumulates. Existing tempo cannot be combined with this.
        self.cum holds whole-quarter boundaries plus a fractional final boundary,
        if any. t/at_beat evaluate the exact curve, including fractional meters.
        Time queries retain finite float compatibility; new map inputs require
        ints/Fractions. Timing only: placement is unchanged ."""
        if self.cum is not None and not hasattr(self, '_tempo_segments'):
            raise ComposeError('tempo_map cannot be combined with tempo')
        if ramp not in ('step', 'linear'):
            raise ComposeError('tempo_map ramp must be step or linear')
        if type(bars) is not int or bars < 1:
            raise ComposeError('tempo_map bars must be a positive integer')
        rows = []
        for row in changes:
            if not isinstance(row, (tuple, list)) or len(row) != 2:
                raise ComposeError('tempo_map takes (bar, bpm) pairs')
            bar, bpm = row
            if type(bar) is not int or not 1 <= bar <= bars + 1 or (rows and bar <= rows[-1][0]):
                raise ComposeError('tempo_map bars must rise from 1 through bars+1')
            bpm = _beat_fraction(bpm, 'tempo_map bpm')
            if bpm <= 0:
                raise ComposeError('tempo_map bpm must be positive')
            rows.append((bar, bpm))
        if not rows or rows[0][0] != 1:
            raise ComposeError('tempo_map needs a bar 1 tempo')
        cap = self._bar_beat(bars + 1)
        points = [(self._bar_beat(bar), Fraction(60_000_000_000, 1) / bpm) for bar, bpm in rows]
        if points[-1][0] < cap:
            points.append((cap, points[-1][1]))
        segments, elapsed = [], Fraction(0)
        for (start, duration), (end, next_duration) in zip(points, points[1:]):
            slope = (next_duration - duration) / (end - start) if ramp == 'linear' else Fraction(0)
            segment = (start, end, duration, slope, elapsed)
            segments.append(segment)
            elapsed = self._segment_time(segment, end)
        grid = [Fraction(k) for k in range(int(cap) + 1)]
        if grid[-1] != cap:
            grid.append(cap)
        cum, index = [], 0
        for k in grid:
            while index + 1 < len(segments) and k >= segments[index][1]:
                index += 1
            cum.append(_tempo_round(self._segment_time(segments[index], k)))
        self._tempo_segments, self._tempo_cap = tuple(segments), cap
        self._tempo_rows, self._tempo_ramp, self._tempo_bars = tuple(rows), ramp, bars
        self.cum = cum

    def _map_time(self, k):
        from bisect import bisect_right
        k = _beat_fraction(k, 'beat', allow_float=True)
        if k > self._tempo_cap:
            raise ComposeError('tempo_map beat %s is past its %s-beat cap' % (k, self._tempo_cap))
        if k < 0:
            return _tempo_round(k * self._tempo_segments[0][2])
        i = max(0, bisect_right([s[0] for s in self._tempo_segments], k) - 1)
        return _tempo_round(self._segment_time(self._tempo_segments[i], k))

    def _tempo_bpm(self, at):
        """BPM at an explicitly named zero-based quarter-note beat, without mutation."""
        from bisect import bisect_right
        at = _beat_fraction(at, 'at beat')
        if at < 0:
            raise ComposeError('at beat must be nonnegative')
        if hasattr(self, '_tempo_segments'):
            if at >= self._tempo_cap:
                raise ComposeError('at beat must precede tempo_map cap %s' % self._tempo_cap)
            i = max(0, bisect_right([s[0] for s in self._tempo_segments], at) - 1)
            start, end, duration, slope, elapsed = self._tempo_segments[i]
            return Fraction(60_000_000_000, 1) / (duration + slope * (at - start))
        if self.cum is not None:
            i = int(at)
            if i >= len(self.cum) - 1:
                raise ComposeError('at beat must precede tempo cap %d' % (len(self.cum) - 1))
            duration = self.cum[i + 1] - self.cum[i]
            if duration <= 0:
                raise ComposeError('tempo beat length must be positive')
            return Fraction(60_000_000_000, duration)
        bpm = _beat_fraction(self.bpm, 'bpm')
        if bpm <= 0:
            raise ComposeError('bpm must be positive')
        return bpm

    def _note_beats(self, value, at):
        """Whole-note fractions, optional dot or T, or N bars in the meter at at."""
        if not isinstance(value, str):
            raise ComposeError('note value must be a string, not %s' % type(value).__name__)
        m = re.fullmatch(r'([1-9][0-9]*)/([1-9][0-9]*)([.T]?)', value)
        if m:
            beats = 4 * Fraction(int(m[1]), int(m[2]))
            return beats * {'': Fraction(1), '.': Fraction(3, 2), 'T': Fraction(2, 3)}[m[3]]
        m = re.fullmatch(r'([1-9][0-9]*) bars?', value)
        if m:
            beats = _beat_fraction(self.beats, 'meter beats')
            for bar, count in getattr(self, '_meter_rows', ()):
                if self._bar_beat(bar) <= at:
                    beats = count
                else:
                    break
            if beats <= 0:
                raise ComposeError('meter beats must be positive')
            return int(m[1]) * beats
        raise ComposeError('note value %r: use N/D, N/D., N/DT or N bars' % value)

    def beats_us(self, value, *, at=0):
        """Note value to whole microseconds at an explicit note-on beat; round half up.
        Values are whole-note fractions (1/4), dotted (1/8.), triplet (1/8T),
        or N bars in the current meter. A quarter is one beat; meters count
        quarters, so 7/8 is 7/2. This is a duration at at, not an interval that
        follows later tempo/meter changes. FX callers enforce their own range."""
        bpm = self._tempo_bpm(at)
        return _tempo_round(60_000_000 * self._note_beats(value, at) / bpm)

    def beats_mhz(self, value, *, at=0, free=0):
        """Note value to LFO mHz at an explicit note-on beat, with tempo_mhz's drift.
        FREE2 under either map refuses. The old map uses its whole-ns beat
        duration; tempo_map uses its exact local beat duration. This is a pure
        query: one patch still has one rate, so callers must not claim changing
        note-on tempos without separately authored compatible patches. No patch
        cloning, ctl route or held-note tracking is installed by this helper.
        It sets no position; regression's autopan owns tempo-synced pan ."""
        if type(free) is not int or free not in (0, 2):
            raise ComposeError('tempo FREE must be 0 or 2')
        if free == 2 and self.cum is not None:
            raise ComposeError('FREE2 under a tempo map is refused')
        bpm = self._tempo_bpm(at)
        return tempo_mhz(bpm, self._note_beats(value, at))

    def tempo(self, beat_len, *, beats=4096):
        """Legacy whole-ns beat lengths, unchanged; beats= extends the default 4096 cap."""
        if hasattr(self, '_tempo_segments'):
            raise ComposeError('tempo cannot be combined with tempo_map')
        if type(beats) is not int or beats < 1:
            raise ComposeError('tempo beats must be a positive integer')
        cum = [0]
        for k in range(beats):
            cum.append(cum[-1] + int(round(beat_len(k))))
        self.cum = cum

    def t(self, bar, beat=1):
        if hasattr(self, '_meter_rows') or hasattr(self, '_tempo_segments'):
            k = self._bar_beat(bar) + _beat_fraction(beat, 'beat', allow_float=True) - 1
        else:
            k = (bar - 1) * self.beats + (beat - 1)
        if hasattr(self, '_tempo_segments'):
            return self._map_time(k)
        if self.cum is not None:
            cap = len(self.cum) - 1
            if k > cap:
                raise ComposeError('tempo beat %s is past its %d-beat cap' % (k, cap))
            if k == cap:
                return self.cum[-1]
            i = int(k // 1)
            return self.cum[i] + int(round((k - i) * (self.cum[i + 1] - self.cum[i])))
        x = k * self.beat_ns
        assert abs(x - round(x)) < 1e-6
        return int(round(x))

    def stream(self, name, role):
        self.streams.append(name)
        self.roles[name] = role
        self.ev[name] = []
        self.marks[name] = {}

    def patch(self, name, car, mod, comment, extra=None):
        """Install a recipe; new bank definitions and bindings publish atomically."""
        if extra and ('wtbanks' in extra or 'wtuse' in extra):
            banks, uses = wt_metadata(name, [car, mod] + list(extra.get('more', ())), extra)
            pending = copy.copy(self)
            headers = set(self._morph_maps.values()) | {'patches', 'routes', 'recipe_filters', 'wavetables', 'wtuses', 'ksources'}
            for key in headers:
                setattr(pending, key, copy.deepcopy(getattr(self, key)))
            clean = {k: v for k, v in extra.items() if k not in ('wtbanks', 'wtuse')}
            pending._patch_raw(name, car, mod, comment, clean, validation_extra=extra)
            pending.wtuses.pop(name, None)
            for bank, (frames, length, harmonics) in banks.items():
                pending.wavetable(bank, frames, length, harmonics=harmonics)
            for i, (bank, position) in uses.items():
                pending.wtuse(name, i, bank, position)
            for key in headers:
                setattr(self, key, getattr(pending, key))
        else:
            self._patch_raw(name, car, mod, comment, extra)
            self.wtuses.pop(name, None)

    def _patch_raw(self, name, car, mod, comment, extra=None, *, validation_extra=None):
        """p.patch(name, *recipe(...)): optional extra adds operators 2 and up, a route,
        and/or filter, LFO, PEG or filter envelope . Replacing a patch also
        replaces that metadata. Without new filter metadata, an independently set
        filter stays in place; with it, the new recipe's filter takes over."""
        if extra and 'macros' in extra:
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
                raise ValueError('macro recipe needs a token patch name')
            macro_budget(extra.get('mod', {}), extra['macros'], [],
                         [car, mod] + list(extra.get('more', ())),
                         extra if validation_extra is None else validation_extra, self.fs)
        self.ksources.pop(name, None)
        self.macros.pop(name, None)
        self.muted.pop(name, None)
        if extra and 'muted' in extra:
            self.muted[name] = tuple(extra['muted'])
        self.patches[name] = [car, mod, comment]
        if extra and 'ops' in extra:
            self.routes[name] = (extra, car[1])
        else:
            self.routes.pop(name, None)
        if extra and 'filter' in extra:
            self.filters[name] = filter_values(extra['filter'])
            self.recipe_filters.add(name)
        elif name in self.recipe_filters:
            self.filters.pop(name, None)
            self.recipe_filters.discard(name)
        if extra and 'shape' in extra:
            self.shapes[name] = shape_values(extra['shape'])
        else:
            self.shapes.pop(name, None)
        if extra and 'amp' in extra:
            self.amp(name, *extra['amp'])
        else:
            self.amps.pop(name, None)
        for kind, mapping in (('kstype', self.kstypes), ('fbtype', self.fbtypes), ('egloop', self.egloops)):
            mapping.pop(name, None)
            for i, fields in (extra or {}).get(kind, {}).items():
                getattr(self, kind)(name, i, *fields)
        if extra and 'lfo' in extra:
            self.lfo(name, *extra['lfo'])
        else:
            self.lfos.pop(name, None)
        if extra and 'peg' in extra:
            self.peg(name, *extra['peg'])
        else:
            self.pegs.pop(name, None)
        if extra and 'fenv' in extra:
            self.fenv(name, *extra['fenv'])
        else:
            self.fenvs.pop(name, None)
        self.mods.pop(name, None)
        for slot, fields in (extra or {}).get('mod', {}).items():
            self.mod(name, slot, *fields)
        self.egs.pop(name, None)
        for i, values in (extra or {}).get('eg', {}).items():
            self.eg(name, i, *values)
        self.left_slopes.pop(name, None)
        for i, value in (extra or {}).get('ks', {}).items():
            self.ks(name, i, value)
        self.formants.pop(name, None)
        for slot, fields in (extra or {}).get('formant', {}).items():
            self.formant(name, slot, *fields)
        self.vectors.pop(name,None)
        if extra and 'vector' in extra:
            self.vector(name,*extra['vector'])
        self.pds.pop(name, None)
        for i,knee in (extra or {}).get('pd',{}).items():
            self.pd(name,i,knee)
        self.noise_shifts.pop(name, None)
        for i, value in (extra or {}).get('noise', {}).items():
            self.noise(name, i, value)

        if extra and 'macros' in extra:
            self.macros[name] = {n: dict(v) for n, v in extra['macros'].items()}

    # The field table is : these maps contain the authoritative live headers,
    # not the recipe metadata retained inside routes after later setter calls.
    _morph_maps = {'filter': 'filters', 'shape': 'shapes', 'amp': 'amps',
                   'lfo': 'lfos', 'peg': 'pegs', 'fenv': 'fenvs', 'mod': 'mods',
                   'eg': 'egs', 'ks': 'left_slopes', 'noise': 'noise_shifts',
                   'pd': 'pds', 'vector': 'vectors', 'formant': 'formants',
                   'kstype': 'kstypes', 'fbtype': 'fbtypes', 'egloop': 'egloops',
                   'macros': 'macros', 'muted': 'muted'}

    def wavetable(self, bank, frames, length, *, harmonics):
        """Declare an immutable native harmonic bank; identical definitions deduplicate.
        Banks shape timbre before placement and move no position .
        """
        definition = wtbank_values(bank, frames, length, harmonics)
        if bank in self.wavetables:
            if self.wavetables[bank] != definition:
                raise ValueError('wavetable %s: conflicting definition' % bank)
            return
        if len(self.wavetables) >= 8:
            raise ValueError('wavetable %s: at most 8 banks' % bank)
        self.wavetables[bank] = definition

    def wtuse(self, patch, operator, bank, position_q15):
        """Bind a declared bank to a present sine operator; explicit zero is retained."""
        if not isinstance(patch, str) or patch not in self.patches:
            raise ValueError('wtuse needs a defined patch: %r' % (patch,))
        extra = dict(self.routes.get(patch, ({}, 0))[0], pd=self.pds.get(patch, {}))
        extra['ksource'] = dict(extra.get('ksource', {}))
        extra['ksource'].update(self.ksources.get(patch, {}))
        binding = wtuse_values(patch, operator, bank, position_q15,
                               self._macro_operators(patch), extra, self.wavetables)
        self.wtuses.setdefault(patch, {})[operator] = binding

    def ksource(self, patch, operator, loss_q15, seed, buffer_samples):
        """Set a native Karplus–Strong source; each voice owns its own buffer.

        This is unrelated to ks, the key-scaling helper. All values use
        explicit native units; no source bank or voice is created here.
        """
        if not isinstance(patch, str) or patch not in self.patches:
            raise ValueError('ksource needs a defined patch')
        fields = ksource_values(patch, operator, loss_q15, seed, buffer_samples,
                                self._macro_operators(patch), self._mod_features(patch))
        self.ksources.setdefault(patch, {})[operator] = fields

    def _ks_validate(self):
        for patch, settings in self.ksources.items():
            if patch not in self.patches:
                raise ValueError('ksource needs a defined patch')
            for operator, fields in settings.items():
                if not isinstance(fields, (list, tuple)) or len(fields) != 3:
                    raise ValueError('patch %s: invalid ksource binding' % patch)
                ksource_values(patch, operator, *fields, self._macro_operators(patch),
                               self._mod_features(patch))

    def _wt_validate(self):
        if len(self.wavetables) > 8:
            raise ValueError('at most 8 wavetable banks')
        for bank, definition in self.wavetables.items():
            if not isinstance(definition, (list, tuple)) or len(definition) != 3:
                raise ValueError('wavetable %s: invalid definition' % bank)
            wtbank_values(bank, *definition)
        for patch, uses in self.wtuses.items():
            if patch not in self.patches:
                raise ValueError('wtuse needs a defined patch: %r' % patch)
            extra = dict(self.routes.get(patch, ({}, 0))[0], pd=self.pds.get(patch, {}))
            extra['ksource'] = dict(extra.get('ksource', {}))
            extra['ksource'].update(self.ksources.get(patch, {}))
            for i, fields in uses.items():
                if not isinstance(fields, (list, tuple)) or len(fields) != 2:
                    raise ValueError('patch %s: invalid wtuse binding' % patch)
                wtuse_values(patch, i, *fields, self._macro_operators(patch), extra, self.wavetables)
        for patch, settings in self.mods.items():
            positions = {slot: fields for slot, fields in settings.items()
                         if len(fields) >= 2 and isinstance(fields[1], str) and fields[1].startswith('wt_pos')}
            if positions:
                if patch not in self.patches:
                    raise ValueError('wt_pos modulation needs a defined patch')
                mod_settings(positions, self._mod_features(patch), self.fs)

    def _patch_morph_source(self, name):
        if not isinstance(name, str) or name not in self.patches:
            raise ValueError('patch morph needs a defined source patch: %r' % (name,))
        if name in getattr(self, '_unison_compensation', {}):
            raise ValueError('patch %s: compensation-owned morph source is refused' % name)
        if self.wtuses.get(name):
            raise ValueError('patch %s: WT-bound morph source needs an asset field class ()' % name)
        if self.ksources.get(name):
            raise ValueError('patch %s: KS-bound morph source needs an asset field class ()' % name)
        car, mod, comment = copy.deepcopy(self.patches[name])
        extra, base = self.routes.get(name, ({}, car[1]))
        if not isinstance(extra, dict) or type(base) is not int or not 0 <= base <= 65535:
            raise ValueError('patch morph needs valid stored route metadata and base level')
        unknown = set(extra) - {'ops', 'carriers', 'edges', 'more'} - set(self._morph_maps)
        if unknown:
            raise ValueError('patch morph has unknown route metadata: %s' % sorted(unknown, key=str))
        # Match patch_lines(level=car[1], base=stored_base), including its clamp.
        # A keys version may retain the old base after changing operator zero.
        route = {k: copy.deepcopy(extra[k]) for k in ('ops', 'carriers', 'edges', 'more') if k in extra}
        operators = [car, mod] + list(route.get('more', ()))
        for key, attr in self._morph_maps.items():
            mapping = getattr(self, attr)
            if name in mapping:
                route[key] = copy.deepcopy(mapping[name])
        result = (operators, comment, route, name in self.recipe_filters)
        self._patch_morph_validate(result)
        operators = [list(o) for o in operators]
        if 'ops' in route:
            for i in range(1, len(operators)):
                if route['carriers'] >> i & 1:
                    operators[i][1] = min(1200, max(0, operators[i][1] + car[1] - base))
            route['more'] = operators[2:]
        result = (operators, comment, route, name in self.recipe_filters)
        self._patch_morph_validate(result)
        return result

    def _patch_morph_validate(self, result):
        operators, comment, extra, _ = result
        ops = extra.get('ops', 2)
        if type(ops) is not int or not 2 <= ops <= 4 or len(operators) != ops:
            raise ValueError('patch morph needs exactly two to four routed operators')
        if not isinstance(comment, str) or '\n' in comment or '\r' in comment:
            raise ValueError('patch morph comment must fit one line')
        for o in operators:
            if not isinstance(o, (tuple, list)) or len(o) not in (9, 11, 12, 13, 14, 15, 16):
                raise ValueError('patch morph operator takes 9 or 11 to 16 raw fields')
            for i, value in enumerate(o):
                bound = {0: 4294967295, 9: 4294967295, 11: 11, 12: 7,
                         13: 4294967295, 15: 2}.get(i, 65535)
                if type(value) is not int or not 0 <= value <= bound:
                    raise ValueError('patch morph operator field %d is outside 0..%d' % (i, bound))
        if 'ops' in extra:
            carriers = extra.get('carriers')
            if type(carriers) is not int or not 1 <= carriers < 1 << ops:
                raise ValueError('patch morph needs a nonempty present-operator carrier mask')
            pairs = []
            for edge in extra.get('edges', ()):
                f, t, _, _ = edge_values(edge, ops)
                if (f, t) in pairs:
                    raise ValueError('patch morph has duplicate route edges')
                pairs.append((f, t))
            if 'edges' not in extra or len(pairs) > 12:
                raise ValueError('patch morph needs a complete route')
        elif any(k in extra for k in ('carriers', 'edges', 'more')):
            raise ValueError('patch morph needs a complete route')
        # These existing pure validators include coupled prerequisites: PD wave,
        # vector carriers, EG sustain, FENV/filter, and every matrix source/target.
        patch_headers('morph', *operators[:2], comment, extra)
        if 'lfo' in extra and extra['lfo'][1] * 64 >= 500 * self.fs:
            raise ValueError('patch morph LFO violates the control-rate Nyquist')
        mod_settings(extra.get('mod', {}), extra, self.fs)
        if 'macros' in extra:
            macro_budget(extra.get('mod', {}), extra['macros'], [], operators, extra, self.fs)
        if any(gen_ref(fields[0]) for fields in extra.get('mod', {}).values()):
            context = copy.copy(self)
            context.mods = {'morph': extra['mod']}
            context.ev, context.gen_lanes = {}, {}  # validate bank, not unfinished record timing
            context._gen_validate()
        for i in extra.get('muted', ()):
            if type(i) is not int or not 0 <= i < ops or extra.get('carriers', 1) >> i & 1 or any(operators[i][2:4]):
                raise ValueError('patch morph selected muted metadata does not fit its operator')

    @staticmethod
    def _patch_morph_route(result):
        extra = result[2]
        if 'ops' not in extra:
            return None
        return extra['ops'], extra['carriers'], tuple(edge_values(e, extra['ops']) for e in extra['edges'])

    def _patch_blend(self, a, b, u):
        _morph_value(0, 0, u)  # strict coordinate validation, also at endpoints
        if len(a[0]) != len(b[0]) or self._patch_morph_route(a) != self._patch_morph_route(b):
            raise ValueError('patch morph sources need identical operator counts and complete routes')
        selected = a if u < 16384 else b
        if u in (0, 32768):
            return copy.deepcopy(selected)
        operators, comment, extra, recipe_filter = copy.deepcopy(selected)
        for i, (ao, bo) in enumerate(zip(a[0], b[0])):
            av, bv = list(ao) + [0]*(16-len(ao)), list(bo) + [0]*(16-len(bo))
            operators[i] = [(_morph_value(av[k], bv[k], u) if k not in (11, 12, 15) and
                             (k != 13 or av[k] and bv[k]) else (av[k] if u < 16384 else bv[k]))
                            for k in range(16)]
        if 'ops' in extra:
            extra['more'] = operators[2:]

        def fields(kind, av, bv):
            av, bv = tuple(av), tuple(bv)
            if kind == 'filter':
                av += (1,) if len(av) == 3 else ()
                bv += (1,) if len(bv) == 3 else ()
                continuous = {1, 2}
            elif kind == 'shape':
                return (_morph_value(av[0], bv[0], u),) + (av if u < 16384 else bv)[1:]
            elif kind == 'lfo':
                mask = (1 << len(operators)) - 1
                av += (0, mask, mask, mask) if len(av) == 7 else ()
                bv += (0, mask, mask, mask) if len(bv) == 7 else ()
                av += (0,) if len(av) == 11 else ()
                bv += (0,) if len(bv) == 11 else ()
                continuous = set(range(1, 8))
            elif kind == 'peg':
                av, bv = peg_values(av, len(operators)), peg_values(bv, len(operators))
                continuous = set(range(8))
            elif kind == 'fenv':
                av, bv = fenv_values(av), fenv_values(bv)
                continuous = set(range(7))
            elif kind == 'mod':
                continuous = {2}
            else:
                continuous = set(range(len(av)))
            return tuple(_morph_value(x, y, u) if k in continuous else (x if u < 16384 else y)
                         for k, (x, y) in enumerate(zip(av, bv)))

        indexed = {'mod', 'eg', 'ks', 'noise', 'pd', 'formant', 'kstype', 'fbtype', 'egloop'}
        discrete = {'noise', 'kstype', 'fbtype', 'egloop', 'macros', 'muted'}
        for kind in self._morph_maps:
            if kind not in a[2] or kind not in b[2] or kind in discrete:
                continue  # selected side owns optional presence and discrete metadata
            av, bv = a[2][kind], b[2][kind]
            if kind in indexed:
                values = copy.deepcopy(extra[kind])
                for i in set(av) & set(bv):
                    values[i] = _morph_value(av[i], bv[i], u) if kind in ('ks', 'pd') else fields(kind, av[i], bv[i])
                extra[kind] = values
            else:
                extra[kind] = fields(kind, av, bv)
        result = (operators, comment, extra, recipe_filter)
        self._patch_morph_validate(result)
        return result

    def _patch_morph_publish(self, name, result):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name):
            raise ValueError('patch morph destination is a 1 to 31 character patch token')
        if name in self.patches:
            raise ValueError('patch morph destination already exists: %s' % name)
        if len(self.patches) >= 32:
            raise ValueError('patch morph exceeds the native 32-patch budget')
        self._patch_morph_validate(result)
        operators, comment, extra, recipe_filter = copy.deepcopy(result)
        patch_headers(name, *operators[:2], comment, extra)
        # All work that can refuse precedes publication; no old map or source
        # payload is replaced. Feature setters must not normalize exact endpoints.
        headers = {attr: extra[key] for key, attr in self._morph_maps.items() if key in extra}
        route = {k: extra[k] for k in ('ops', 'carriers', 'edges', 'more') if k in extra}
        self.patches[name] = [*operators[:2], comment]
        if route:
            self.routes[name] = (route, operators[0][1])
        for attr, values in headers.items():
            getattr(self, attr)[name] = values
        if recipe_filter:
            self.recipe_filters.add(name)

    def patch_morph(self, name, a, b, u):
        """Create a new static patch from equal-route A/B using raw Q15 u .
        Continuous integers round as (A*(32768-u)+B*u+16384)>>15; selectors
        and optional presence take B at16384. Endpoints keep written fields and
        comments, with the destination name substituted. Validation is atomic.
        Partial performance state: this moves static patch fields, never pos or
        move. Bind a separate voice to each result; existing voices keep their
        own positions. No engine event or automatic voice allocation is added.
        """
        result = self._patch_blend(self._patch_morph_source(a), self._patch_morph_source(b), u)
        self._patch_morph_publish(name, result)

    def patch_square(self, name, a, b, c, d, x, y):
        """Four static corners A/B/C/D: interpolate AB and CD in x, then in y.
        Every interpolation rounds independently; corner order is significant.
        Like patch_morph, this leaves positions and voice allocation to the piece.
        """
        _morph_value(0, 0, x)
        _morph_value(0, 0, y)
        sources = [self._patch_morph_source(n) for n in (a, b, c, d)]
        if any(self._patch_morph_route(s) != self._patch_morph_route(sources[0]) or
               len(s[0]) != len(sources[0][0]) for s in sources[1:]):
            raise ValueError('patch square sources need identical operator counts and complete routes')
        ab = self._patch_blend(sources[0], sources[1], x)
        cd = self._patch_blend(sources[2], sources[3], x)
        self._patch_morph_publish(name, self._patch_blend(ab, cd, y))

    def gen_clock(self, ctl=64):
        """Opt into the fixed session generator grid; an explicit empty bank is legal."""
        gen_bound(ctl,'clock')
        if ctl != 64: raise ValueError('gen_clock must match FM_CTL 64')
        if self.gen_ctl is not None:
            raise ValueError('gen_clock is defined twice')
        self.gen_ctl = ctl

    def gen(self, id, kind, seed, div=1, slew=0, **fields):
        """Define one session source; seed is explicit and repeated IDs are refused."""
        config = gen_values(id,kind,seed,div,slew,**fields)
        if id in self.generators:
            raise ValueError('gen ID %d is defined twice' % id)
        self.generators[id] = config

    def gen_axiom(self, id, depth, word):
        gen_bound(id,'source_id'); gen_bound(depth,'axiom_depth')
        gen_word(word,'axiom_word')
        if id in self.gen_axioms:
            raise ValueError('gen_axiom ID %d is defined twice' % id)
        self.gen_axioms[id] = (depth,word)

    def gen_rule(self, id, symbol, word):
        gen_bound(id,'source_id')
        gen_word(symbol,'rule_symbol'); gen_word(word,'rule_word')
        if symbol in self.gen_rules.get(id,{}):
            raise ValueError('gen_rule ID %d symbol %s is defined twice' % (id,symbol))
        self.gen_rules.setdefault(id,{})[symbol] = word

    def gen_lane(self, id, voice, gate=None, pitch=None, accent=None, *, step_ticks,
                 base_cents, span_cents, velocity, gate_ticks, scale_mask=0):
        values = gen_lane_values(id,voice,gate,pitch,accent,step_ticks,base_cents,
                                 span_cents,velocity,gate_ticks,scale_mask)
        if id in self.gen_lanes:
            raise ValueError('gen_lane ID %d is defined twice' % id)
        if any(fields[0] == voice for fields in self.gen_lanes.values()):
            raise ValueError('gen_lane voice %d is reserved twice' % voice)
        self.gen_lanes[id] = values

    def _gen_event(self, s, t, text, comment):
        if s not in self.ev or type(t) is not int or not 0 <= t <= 18446744073709551615:
            raise ValueError('generator event needs a defined stream and uint64 time')
        self.seq += 1
        self.ev[s].append((t,4,self.seq,None,text,comment))

    def gen_set(self, s, t, id, param, value, text=''):
        """Queue a source change at its next due tick; pending cross-field checks are native."""
        gen_bound(id,'source_id')
        if id not in self.generators:
            raise ValueError('gen event needs a defined source')
        gen_param_value(self.generators[id][0],param,value)
        self._gen_event(s,t,'gen %d %s %d' % (id,param,value),text)

    def lane_enable(self, s, t, id, enabled, text=''):
        gen_bound(id,'lane_id'); gen_bound(enabled,'enabled','white')
        if id not in self.gen_lanes:
            raise ValueError('lane event needs a defined lane')
        self._gen_event(s,t,'lane %d enable %d' % (id,enabled),text)

    def _gen_validate(self):
        refs = [gen_ref(fields[0]) for slots in self.mods.values() for fields in slots.values() if gen_ref(fields[0])]
        if (self.generators or self.gen_axioms or self.gen_rules or self.gen_lanes or refs or
                any(e[4].startswith(('gen ','lane ')) for events in self.ev.values() for e in events)) and self.gen_ctl != 64:
            raise ValueError('generators, lanes, references and events require gen_clock 64')
        if self.gen_ctl is None:
            return
        gen_int(self.gen_ctl,64,64,'gen_clock CTL')
        for id,config in self.generators.items():
            kind,seed,div,slew,values,parent = config
            fields = dict(zip((f[0] for f in GEN_FIELDS[kind]),values))
            if kind == 'euclid': fields['parent'] = parent
            gen_values(id,kind,seed,div,slew,**fields)
            if parent is not None and parent not in self.generators:
                raise ValueError('gen parent %d is undefined' % parent)
            if kind == 'lsystem' and id not in self.gen_axioms:
                raise ValueError('lsystem %d needs gen_axiom' % id)
        if sum(config[0] in ('fibonacci','lsystem') for config in self.generators.values()) > 2:
            raise ValueError('at most two fibonacci/lsystem word sources are supported')
        for id in set(self.gen_axioms) | set(self.gen_rules):
            if id not in self.generators or self.generators[id][0] != 'lsystem':
                raise ValueError('gen_axiom/gen_rule needs an lsystem source')
        for id,axis in refs:
            if id not in self.generators:
                raise ValueError('mod generator %d is undefined' % id)
            if axis == 'y' and self.generators[id][0] != 'henon':
                raise ValueError('only henon admits a generator y reference')
        reserved = set()
        for id,values in self.gen_lanes.items():
            gen_lane_values(id,*values)
            voice = values[0]
            if voice >= len(self.voices) or self.voices[voice][0] not in self.patches:
                raise ValueError('gen_lane needs an existing voice and patch')
            if voice in reserved:
                raise ValueError('gen_lane voice is reserved twice')
            reserved.add(voice)
            for source in values[1:4]:
                if source is not None and source not in self.generators:
                    raise ValueError('gen_lane source %d is undefined' % source)
        for events in self.ev.values():
            for t,_,_,voice,text,_ in events:
                words = text.split()
                if voice in reserved and words[0] in ('on','off','pitch'):
                    raise ValueError('manual on/off/pitch conflicts with a gen_lane reserved voice')
                if words[0] in ('gen','lane') and not 0 <= t < self.length_ns:
                    raise ValueError('generator event lies outside the record length')

    def _macro_operators(self, patch):
        return list(self.patches[patch][:2]) + list(self.routes.get(patch, ({}, 0))[0].get('more', ()))

    def _macro_publish(self, patch, extra):
        # The complete candidate has passed macro_budget before any mutation.
        if 'lfo' in extra and patch not in self.lfos:
            self.lfo(patch, *extra['lfo'])
        for slot, fields in extra['mod'].items():
            if self.mods.get(patch, {}).get(slot) != fields:
                self.mod(patch, slot, *fields)
        self.macros[patch] = extra['macros']

    def macro(self, patch, name, n, targets, bipolar=False, curve=None):
        """One named ctl input drives 1..8 (TARGET, AMOUNT[, SHIFT]) rows.
        A bounded macro layer, not the spec's 8x8: all uses share eight slots
        and eight inputs. One optional host curve receives/returns integer Q15.
        Macros move timbre through modulation matrix, never position; pos/move still place it
        identically in mono, keys and space stereo records .
        """
        if patch not in self.patches or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', patch):
            raise ValueError('macro needs a defined token patch name: %s' % patch)
        extra = self._mod_features(patch)
        mods, macros = macro_budget(self.mods.get(patch, {}), self.macros.get(patch, {}),
                                    [(name, n, targets, bipolar, curve)],
                                    self._macro_operators(patch), extra, self.fs)
        self._macro_publish(patch, dict(extra, mod=mods, macros=macros))

    def template(self, patch, name):
        """Apply one recipe-file template atomically; no existing LFO is replaced."""
        if patch not in self.patches or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', patch):
            raise ValueError('template needs a defined token patch name: %s' % patch)
        extra = dict(self._mod_features(patch), mod=self.mods.get(patch, {}), macros=self.macros.get(patch, {}))
        pending = macro_template(name, MACRO_TEMPLATES, self._macro_operators(patch), extra, self.fs)
        self._macro_publish(patch, pending)

    def macro_set(self, s, t, v, name, knob, text=''):
        """Set a voice's named macro using its integer knob map, through ctl."""
        if not isinstance(s, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', s):
            raise ValueError('macro_set needs a token stream name')
        if type(v) is not int or not 0 <= v < len(self.voices):
            raise ValueError('macro_set needs a defined voice')
        patch = self.voices[v][0]
        if name not in self.macros.get(patch, {}):
            raise ValueError('patch %s has no macro %s' % (patch, name))
        spec = self.macros[patch][name]
        value = macro_knob(knob, spec['bipolar'], spec['curve'])
        self.ctl(s, t, v, spec['input'], value, text)

    def mod(self, patch, slot, source, target, amount, shift=3):
        """Set/replace a raw modulation matrix slot, including amount zero. Features validate at write.

        Smoothed slots start at 0 on a note-on from silence; a full-scale
        constant source takes about 67 ticks at SHIFT 3. Use shift=0 for a
        static source that should act at once. Retriggers retain smoothing.
        """
        if patch not in self.patches:
            raise ValueError('mod needs a defined patch: %s' % patch)
        fields = mod_values((slot, source, target, amount, shift))
        if self.macros.get(patch):
            candidate = dict(self.mods.get(patch, {}))
            candidate[slot] = fields[1:]
            macro_budget(candidate, self.macros[patch], [], self._macro_operators(patch),
                         self._mod_features(patch), self.fs)
        self.mods.setdefault(patch, {})[slot] = fields[1:]

    def _mod_features(self, name):
        extra = dict(self.routes[name][0]) if name in self.routes else {}
        for key, mapping in (('filter', self.filters), ('lfo', self.lfos),
                             ('peg', self.pegs), ('fenv', self.fenvs), ('pd', self.pds),
                             ('vector', self.vectors), ('formant', self.formants),
                             ('wtuse', self.wtuses)):
            if name in mapping:
                extra[key] = mapping[name]
            else:
                extra.pop(key, None)
        if name in self.muted:
            extra['muted'] = self.muted[name]
        return extra

    def shape(self, patch, drive_db10, kind, pre=False):
        """Set a shaper; pre=True puts it before the formant/filter. Another call replaces it."""
        if patch not in self.patches:
            raise ValueError('shape needs a defined patch: %s' % patch)
        if type(pre) is not bool:
            raise ValueError('shape pre must be True or False')
        self.shapes[patch] = shape_values((drive_db10, kind) + (('pre',) if pre else ()))

    def amp(self, patch, attack_ms, decay_ms, sustain_db10, release_ms):
        """Enable/replace the voice amp; zero fields still enable it. Patch replacement removes it."""
        if patch not in self.patches:
            raise ValueError('amp needs a defined patch: %s' % patch)
        self.amps[patch] = amp_values((attack_ms, decay_ms, sustain_db10, release_ms))

    def _op_ext(self, kind, patch, operator, *values):
        if patch not in self.patches:
            raise ValueError('%s needs a defined patch: %s' % (kind, patch))
        ops = self.routes[patch][0]['ops'] if patch in self.routes else 2
        fields = op_ext_values(kind, (operator, *values), ops)
        mapping = {'kstype': self.kstypes, 'fbtype': self.fbtypes, 'egloop': self.egloops}[kind]
        if op_ext_row(kind, patch, *fields, ops=ops):
            mapping.setdefault(patch, {})[operator] = fields[1:]
        elif patch in mapping:
            mapping[patch].pop(operator, None)
            if not mapping[patch]:
                del mapping[patch]

    def kstype(self, patch, operator, target='index', right='-', left='-'):
        """Set key scaling's target and sides; the index/-/- default removes its row."""
        self._op_ext('kstype', patch, operator, target, right, left)

    def fbtype(self, patch, operator, kind='classic'):
        """Set the feedback type; classic removes its row."""
        self._op_ext('fbtype', patch, operator, kind)

    def egloop(self, patch, operator):
        """Enable the operator envelope loop; patch replacement removes it."""
        self._op_ext('egloop', patch, operator)

    def eg(self, patch, operator, delay_ms, decay2_ms=0, sustain2_db10=0):
        """Set one operator's filter envelope extension; zero removes it, another call replaces it."""
        if patch not in self.patches:
            raise ValueError('eg needs a defined patch: %s' % patch)
        ops = self.routes[patch][0]['ops'] if patch in self.routes else 2
        i, *values = eg_values((operator, delay_ms, decay2_ms, sustain2_db10), ops)
        every = list(self.patches[patch][:2]) + list(self.routes[patch][0]['more'] if patch in self.routes else [])
        eg_values((i, *values), sustain1=every[i][7])
        if any(values):
            self.egs.setdefault(patch, {})[i] = tuple(values)
        elif patch in self.egs:
            self.egs[patch].pop(i, None)
            if not self.egs[patch]:
                del self.egs[patch]

    def ks(self, patch, operator, lo_slope_db10):
        """Set a left index slope in raw dB/10 per octave; zero removes that operator's slope."""
        if patch not in self.patches:
            raise ValueError('ks needs a defined patch: %s' % patch)
        ops = self.routes[patch][0]['ops'] if patch in self.routes else 2
        i, value = ks_values((operator, lo_slope_db10), ops, self.routes[patch][0] if patch in self.routes else None)
        if value:
            self.left_slopes.setdefault(patch, {})[i] = value
        elif patch in self.left_slopes:
            self.left_slopes[patch].pop(i, None)
            if not self.left_slopes[patch]:
                del self.left_slopes[patch]

    def formant(self, patch, slot, center_millihz, q_milli, level_db10):
        """Set/replace an enabled bank slot; every explicit valid row remains present."""
        if patch not in self.patches:
            raise ValueError('formant needs a defined patch')
        fields = formant_values((slot, center_millihz, q_milli, level_db10))
        self.formants.setdefault(patch, {})[slot] = fields[1:]

    def _formant_validate(self):
        for name, settings in self.formants.items():
            if name not in self.patches:
                raise ValueError('formant needs a defined patch')
            for slot, fields in settings.items():
                formant_values((slot, *fields))

    def vector(self, patch, x, y):
        """Set/replace static coordinates on a routed four-carrier patch."""
        if patch not in self.patches:
            raise ValueError('vector needs a defined patch')
        route = self.routes[patch][0] if patch in self.routes else {}
        self.vectors[patch] = vector_values((x,y),route.get('ops',2),route.get('carriers',1))

    def _vector_validate(self):
        for name,values in self.vectors.items():
            route = self.routes[name][0] if name in self.routes else {}
            vector_values(values,route.get('ops',2),route.get('carriers',1))

    def pd(self, patch, operator, knee):
        """Set or replace enabled sine PD; explicit identity remains present."""
        if patch not in self.patches:
            raise ValueError('pd needs a defined patch')
        ops = self.routes[patch][0]['ops'] if patch in self.routes else 2
        i,value = pd_values((operator,knee),ops)
        every = list(self.patches[patch][:2])+list(self.routes[patch][0]['more'] if patch in self.routes else [])
        o = every[i]
        pd_values((i,value),wave=o[11] if len(o)>11 else 0)
        if i in self.ksources.get(patch, {}):
            raise ValueError('patch %s operator %d: pd conflicts with ksource' % (patch, i))
        self.pds.setdefault(patch,{})[i] = value

    def _pd_validate(self):
        for name,settings in self.pds.items():
            ops = self.routes[name][0]['ops'] if name in self.routes else 2
            every = list(self.patches[name][:2])+list(self.routes[name][0]['more'] if name in self.routes else [])
            for i,knee in settings.items():
                pd_values((i,knee),ops)
                o = every[i]
                pd_values((i,knee),wave=o[11] if len(o)>11 else 0)

    def noise(self, patch, operator, shift):
        """Set a coloured-noise operator's SHIFT 1..15; default 4 removes its statement."""
        if patch not in self.patches:
            raise ValueError('noise needs a defined patch: %s' % patch)
        ops = self.routes[patch][0]['ops'] if patch in self.routes else 2
        i, value = noise_values((operator, shift), ops)
        every = list(self.patches[patch][:2]) + list(self.routes[patch][0]['more'] if patch in self.routes else [])
        o = every[i]
        noise_values((i, value), wave=o[11] if len(o) > 11 else 0)
        if value != 4:
            self.noise_shifts.setdefault(patch, {})[i] = value
        elif patch in self.noise_shifts:
            self.noise_shifts[patch].pop(i, None)
            if not self.noise_shifts[patch]:
                del self.noise_shifts[patch]

    def lfo(self, patch, wave, rate_millihz, delay_ms, fade_ms, pitch_cents, level_db10, index_q10,
            phase_u16=None, mask_pitch=None, mask_level=None, mask_index=None, free=None):
        """Set a defined patch's LFO in raw timeline units. Phase and masks are all four
        or none; omitted masks cover every operator. Optional free=0/2 follows the
        complete phase/mask group. Calling again replaces the LFO."""
        if patch not in self.patches:
            raise ValueError('lfo needs a defined patch: %s' % patch)
        tail = (phase_u16, mask_pitch, mask_level, mask_index)
        values = (wave, rate_millihz, delay_ms, fade_ms, pitch_cents, level_db10, index_q10)
        if any(v is not None for v in tail) or free is not None:
            values += tail
        if free is not None:
            values += (free,)
        ops = self.routes[patch][0]['ops'] if patch in self.routes else 2
        self.lfos[patch] = lfo_values(values, ops)

    def peg(self, patch, l1, t1, l2, t2, l3, t3, l4, t4, mask=None):
        """Set a defined patch's PEG in cents and ms; omitted mask means all operators.
        Calling again replaces the PEG. Fixed-frequency operators ignore its offset."""
        if patch not in self.patches:
            raise ValueError('peg needs a defined patch: %s' % patch)
        values = (l1, t1, l2, t2, l3, t3, l4, t4) + (() if mask is None else (mask,))
        ops = self.routes[patch][0]['ops'] if patch in self.routes else 2
        self.pegs[patch] = peg_values(values, ops)

    def fenv(self, patch, attack_ms, decay_ms, sustain_permille, release_ms, depth_cents,
             track_permille, ref_millihz=261626):
        """Set a defined patch's filter envelope in raw timeline units; replace any old one.
        The patch needs a filter before write_session, so filter may follow this call."""
        if patch not in self.patches:
            raise ValueError('fenv needs a defined patch: %s' % patch)
        self.fenvs[patch] = fenv_values((attack_ms, decay_ms, sustain_permille, release_ms,
                                        depth_cents, track_permille, ref_millihz))

    def add_voices(self, patch, n, comment):
        first = len(self.voices)
        for i in range(n):
            self.voices.append((patch, comment if i == 0 else ''))
        return list(range(first, first + n))

    def on(self, s, t, v, note, vel):
        self.seq += 1
        self.ev[s].append((t, 1, self.seq, v, 'on %7d %5d' % (mhz(note), vel), note))

    def on_f(self, s, t, v, f_mhz, vel, text):
        self.seq += 1
        self.ev[s].append((t, 1, self.seq, v, 'on %7d %5d' % (f_mhz, vel), text))

    def pitch_f(self, s, t, v, f_mhz, text):
        """A pitch event : the voice's frequency, and nothing else, changes.
        At one ns it follows any note-on there, so it sets that note's frequency."""
        self.seq += 1
        self.ev[s].append((t, 2, self.seq, v, 'pitch %7d' % f_mhz, text))

    def ctl(self, s, t, v, n, value_q15, text=''):
        """Latch a per-voice controller at t; any tied event group keeps call order."""
        if s not in self.ev or type(t) is not int or t < 0 or type(v) is not int or not 0 <= v < len(self.voices):
            raise ValueError('ctl needs a defined stream/voice and nonnegative integer time')
        if self.voices[v][0] not in self.patches:
            raise ValueError('ctl needs a voice with a defined patch')
        if type(n) is not int or not 0 <= n < 8 or type(value_q15) is not int or not -32768 <= value_q15 <= 32768:
            raise ValueError('ctl needs N 0..7 and VALUE_Q15 -32768..32768 as integers')
        self._compensation_ctl_check(v, n)
        self.seq += 1
        self.ev[s].append((t, 4, self.seq, v, 'ctl %d %d' % (n, value_q15), text))

    @_perf_atomic
    def ctl_lane(self, s, v, n, points, text=''):
        """A motion lane : controller input n of voice v through points, each (t_ns,
        VALUE_Q15) or (t_ns, VALUE_Q15, TEXT), the times whole ns that never fall, written
        as plain ctl events in list order (at one ns too); TEXT, else text, is each event's
        .tl comment. Piece.ctl checks each point; a refusal writes none (_perf_atomic).
        Piece.ctl's default stays: an input reads 0 until its first ctl. A lane called
        before a steps call writes what that call's ctl locks, restores and slides
        write, event for event, when the lane holds those events at their times, except
        the order within one ns when a restore or slide point shares that ns with another
        input's lock ( (9))."""
        pts = list(points) if isinstance(points, (list, tuple)) else []
        if (not pts or any(not isinstance(x, (list, tuple)) or len(x) not in (2, 3) or type(x[0]) is not int
                           for x in pts) or any(a[0] > b[0] for a, b in zip(pts, pts[1:]))):
            raise ValueError('ctl_lane on %s, voice %r, ctl %r: points is a list of one or more (t_ns, VALUE_Q15) '
                             'or (t_ns, VALUE_Q15, TEXT), the times whole ns that never fall' % (s, v, n))
        for x in pts:
            self.ctl(s, x[0], v, n, x[1], x[2] if len(x) == 3 else text)

    @_perf_atomic
    def ctl_mseg(self, stream, voice, n, points, *, anchor, loop=None, release_at=None):
        """Expand a finite host ctl lane, not the held engine MSEG source .
        Points are (relative integer ns, raw Q15), starting at0 and increasing.
        Exactly one existing modulation matrix slot must read ctlN, including an amount0 slot;
        no route is allocated. Each interval has SLIDE_POINTS subdivisions and
         integer interpolation. Shared endpoints occur once; floor-time
        ties keep call order. Loop(i,j) repeats through an explicit absolute
        release_at, then plays the tail with its original interval durations.
        A partial interval retains scheduled samples strictly before release
        and interpolates the release value there. Completed seams choose j;
        release at the initial loop start chooses i. Unequal seams write end
        then restart. No new cycle starts at release. Known/final record bounds
        follow perf_event/writer. The complete lane publishes atomically through
        ctl_lane. It never changes pos/move, in mono or spatial records .
        """
        if (not isinstance(stream, str) or stream not in self.ev or
                type(voice) is not int or not 0 <= voice < len(self.voices) or
                self.voices[voice][0] not in self.patches or
                type(n) is not int or not 0 <= n < 8):
            raise ValueError('ctl_mseg needs a defined stream/voice/patch and integer ctl 0..7')
        if type(anchor) is not int or anchor < 0:
            raise ValueError('ctl_mseg anchor must be nonnegative integer ns')
        if (not isinstance(points, (list, tuple)) or not points or
                any(not isinstance(x, (list, tuple)) or len(x) != 2 or
                    type(x[0]) is not int or x[0] < 0 or type(x[1]) is not int or
                    not -32768 <= x[1] <= 32768 for x in points) or
                points[0][0] != 0 or any(a[0] >= b[0] for a, b in zip(points, points[1:]))):
            raise ValueError('ctl_mseg points need increasing integer ns from0 and integer Q15 -32768..32768')
        if loop is None:
            if release_at is not None:
                raise ValueError('ctl_mseg release_at needs a loop')
            final = anchor + points[-1][0]
        else:
            if (not isinstance(loop, (list, tuple)) or len(loop) != 2 or
                    any(type(x) is not int for x in loop) or not 0 <= loop[0] < loop[1] < len(points)):
                raise ValueError('ctl_mseg loop needs integer indices 0 <= i < j < len(points)')
            i, j = loop
            if type(release_at) is not int or release_at < anchor + points[i][0]:
                raise ValueError('ctl_mseg release_at must be integer ns at or after the initial loop start')
            final = release_at + points[-1][0] - points[j][0]
        self.perf_event('ctl_mseg', anchor)
        self.perf_event('ctl_mseg', final)
        patch = self.voices[voice][0]
        try:
            settings, _ = macro_budget(self.mods.get(patch, {}), self.macros.get(patch, {}), [],
                                       self._macro_operators(patch), self._mod_features(patch), self.fs)
        except ValueError as e:
            raise ValueError('ctl_mseg patch %s ctl%d: %s' % (patch, n, e)) from None
        slots = sorted(k for k, row in settings.items() if row[0] == 'ctl%d' % n)
        if len(slots) != 1:
            raise ValueError('ctl_mseg patch %s ctl%d needs exactly one modulation matrix slot; found %d: %s' %
                             (patch, n, len(slots), slots))
        lane = [(anchor, points[0][1])]

        def interval(t0, a, t1, b, stop=None):
            # Keep the original schedule, even when release cuts this interval.
            for k in range(1, SLIDE_POINTS + 1):
                t = t0 + (t1 - t0) * k // SLIDE_POINTS
                if stop is not None and t >= stop:
                    break
                lane.append((t, _morph_value(a, b, 32768 * k // SLIDE_POINTS)))
            if stop is not None:
                lane.append((stop, _morph_value(a, b, 32768 * (stop - t0) // (t1 - t0))))

        if loop is None:
            for a, b in zip(points, points[1:]):
                interval(anchor + a[0], a[1], anchor + b[0], b[1])
        else:
            for a, b in zip(points[:i], points[1:i+1]):
                interval(anchor + a[0], a[1], anchor + b[0], b[1])
            t = anchor + points[i][0]
            while t < release_at:
                for k in range(i, j):
                    a, b = points[k], points[k+1]
                    end = t + b[0] - a[0]
                    if release_at <= end:
                        interval(t, a[1], end, b[1], release_at)
                        t = release_at
                        break
                    interval(t, a[1], end, b[1])
                    t = end
                if t < release_at and points[j][1] != points[i][1]:
                    lane.append((t, points[i][1]))
            value = lane[-1][1]
            for a, b in zip(points[j:], points[j+1:]):
                end = t + b[0] - a[0]
                interval(t, value, end, b[1])
                t, value = end, b[1]
        self.ctl_lane(stream, voice, n, lane)

    def _snapshot_values(self, name, values):
        try:
            _macro_name(name)
        except ValueError as e:
            raise ValueError('snapshot %r: %s' % (name, e)) from None
        if not isinstance(values, dict):
            raise ValueError('snapshot %s needs a voice-to-controller map' % name)
        copied = {}
        for v, inputs in values.items():
            if (type(v) is not int or not 0 <= v < len(self.voices) or
                    self.voices[v][0] not in self.patches):
                raise ValueError('snapshot %s needs a defined voice and patch: %r' % (name, v))
            if not isinstance(inputs, dict):
                raise ValueError('snapshot %s voice %d needs a controller map' % (name, v))
            copied[v] = {}
            for n, value in inputs.items():
                if type(n) is not int or not 0 <= n < 8 or type(value) is not int or not -32768 <= value <= 32768:
                    raise ValueError('snapshot %s voice %d needs integer ctl 0..7 and Q15 -32768..32768' % (name, v))
                self._compensation_ctl_check(v, n)
                copied[v][n] = value
        return copied

    def snapshot(self, name, values):
        """Define one of at most eight named maps {voice: {ctl: Q15}} .
        Values are copied; names cannot be replaced. Missing inputs have no default.
        This covers only ctl state, not pitch/filter/gen inputs or pos/move. Snapshots
        and morphs never move a voice; each voice keeps its own spatial events .
        """
        copied = self._snapshot_values(name, values)
        if name in self.snapshots:
            raise ValueError('snapshot %s is defined twice' % name)
        if len(self.snapshots) >= 8:
            raise ValueError('snapshot %s exceeds the eight-snapshot song budget' % name)
        self.snapshots[name] = copied

    def _snapshot_get(self, name):
        if not isinstance(name, str) or name not in self.snapshots:
            raise ValueError('snapshot %r is not defined' % (name,))
        return self._snapshot_values(name, self.snapshots[name])

    def _snapshot_time(self, stream, t):
        if not isinstance(stream, str) or stream not in self.ev or type(t) is not int or t < 0:
            raise ValueError('snapshot recall/morph needs a defined stream and nonnegative integer time')
        self.perf_event('snapshot recall/morph', t)

    @_perf_atomic
    def recall(self, stream, t, name, text=''):
        """Recall listed values in rising voice/input order, atomically through ctl.
        A known length cap is checked now; the final writer checks the completed record.
        """
        values = self._snapshot_get(name)
        self._snapshot_time(stream, t)
        for v in sorted(values):
            for n in sorted(values[v]):
                self.ctl(stream, t, v, n, values[v][n], text)

    @_perf_atomic
    def snapshot_morph(self, stream, a, b, t0, t1, *, steps, text=''):
        """Recall a, then ramp only differing ctl inputs to b (, partial state).
        Identical voice/input keysets are required: no missing value is invented.
        N=SLIDE_POINTS*steps segments use floor-divided times/Q15 positions and the
         integer formula, including both endpoints. Equal inputs emit once.
        Duplicate times retain ctl_lane call order. A known length is checked now;
        the final writer checks the completed record. No pos/move state is touched.
        """
        left, right = self._snapshot_get(a), self._snapshot_get(b)
        if left.keys() != right.keys() or any(left[v].keys() != right[v].keys() for v in left):
            raise ValueError('snapshot morph %s to %s needs identical voice/input keysets' % (a, b))
        self._snapshot_time(stream, t0)
        self._snapshot_time(stream, t1)
        if t1 <= t0 or type(steps) is not int or steps <= 0:
            raise ValueError('snapshot morph needs t1 > t0 and positive integer steps')
        count = SLIDE_POINTS * steps
        for v in sorted(left):
            for n in sorted(left[v]):
                lo, hi = left[v][n], right[v][n]
                points = ([(t0, lo)] if lo == hi else
                          [(t0 + (t1 - t0) * q // count,
                            _morph_value(lo, hi, 32768 * q // count))
                           for q in range(count + 1)])
                self.ctl_lane(stream, v, n, points, text)

    def curve(self, s, v, t0, t1, pitch, vel, step_ns, cents=1.0, bend=False):
        """One note whose pitch follows pitch(u), u in ns since t0, as fractional MIDI:
        a note-on at t0, then a change on the same voice at each step where the
        pitch has moved by at least `cents` since the last one. With bend, each
        change is a pitch event : envelopes, levels and index run on, so any
        patch bends. Without it, each is a retriggering note-on, as the pieces
        before  were written: phases continue, and it is seamless only when
        every operator of the patch sustains at 0 dB and vel repeats."""
        last = pitch(0)
        self.on_f(s, t0, v, mhz_m(last), vel, label(last))
        t = t0 + step_ns
        while t < t1:
            m = pitch(t - t0)
            if abs(m - last) * 100 >= cents:
                if bend:
                    self.pitch_f(s, t, v, mhz_m(m), label(m))
                else:
                    self.on_f(s, t, v, mhz_m(m), vel, label(m))
                last = m
            t += step_ns

    def filter(self, patch, mode, hz, q, stages=1):
        """'s voice filter on a defined patch: mode 'lp', 'bp', 'hp' or 'notch', the
        cutoff in Hz and the resonance Q, set again at each note-on of its voices. The
        engine clamps the cutoff to fs/2400 .. 0.45 fs and Q to 0.5 .. 16.
        stages=2 selects two identical filters in series; stages=1 keeps the old triple."""
        c, qm = millihz(hz), int(round(q * 1000))
        assert mode in ('lp', 'bp', 'hp', 'notch') and patch in self.patches
        assert 0 <= c < 1 << 32 and 0 <= qm < 1 << 16
        self.filters[patch] = filter_values((mode, c, qm, stages))
        self.recipe_filters.discard(patch)

    def cutoff(self, s, t, v, hz, q, text=''):
        """A filter event : the voice's cutoff in Hz and Q from t until its next
        note-on; its patch needs a filter. At one ns it follows a note-on there."""
        c, qm = millihz(hz), int(round(q * 1000))
        assert self.voices[v][0] in self.filters and 0 <= c < 1 << 32 and 0 <= qm < 1 << 16
        self.seq += 1
        self.ev[s].append((t, 3, self.seq, v, 'filter %8d %5d' % (c, qm), text))

    def off(self, s, t, v):
        self.seq += 1
        self.ev[s].append((t, 0, self.seq, v, 'off', ''))

    def fx(self, dcblock=None, chorus=None, delay=None, reverb=None, limiter=None, softclip=False,
           gain=None, dither=None, pingpong=None, at=0, compressor=None, flanger=None, decodable=False,
           crush=False):
        """The effects (docs/INSTRUMENT.md,  and ), in draft1 and draft2, with the record's
        values: dcblock FC_MILLIHZ; chorus (BASE_US, DEPTH_US, RATE_MILLIHZ); delay (TIME_US,
        FEEDBACK_Q15, LOWPASS_HZ); reverb (ROOM_PERMILLE, DAMP_PERMILLE); limiter
        (THRESHOLD_DB10, RELEASE_MS); softclip True; gain GAIN_DB10, the master gain in tenths
        of a dB, -600 to 240; dither SEED, 1 to 4294967295 (, regression). A part left out
        stays off, and with none the record is as before. The harness checks the ranges
        (docs/INSTRUMENT.md, section 7); gain and dither are checked here too, as integers.
        pingpong (TIME_US, FEEDBACK_Q15, LOWPASS_HZ, HIGHPASS_HZ) uses delay's send,
        requires ordinary stereo or a field route without panlaw; mutually exclusive with delay, across calls.
        A note-value string may replace delay/pingpong TIME_US or chorus RATE_MILLIHZ;
        at is the explicit quarter-note beat to sample, not live tempo tracking.
        Exact converted values outside the field's range refuse before rounding.
        compressor (THRESHOLD_DB10, RATIO_TENTHS, ATTACK_MS, RELEASE_MS, MAKEUP_DB10)
        takes integers in 0..600, 10..200, 0..100, 1..5000 and 0..240; it links all
        master channels after placement and enables the silent pre-pan duck send.
        flanger (BASE_US, DEPTH_US, RATE_MILLIHZ, FEEDBACK_Q15, INVERT) takes
        100..10000, 100..BASE_US, 0..2000, 0..29491 and 0/1; one wet line,
        taps a quarter cycle apart. Placement affects only its dry source.
        It admits mono, ordinary stereo and field routes; PANLAW stays refused.
        decodable True writes the valueless  reader profile; bool only.
        The native reader enforces its effect/shape/fixed-position restrictions.
        crush True enables the per-voice bit/rate insert ; bool only.
        Use crush for its next-block settings, initially 24/1 exact bypass.
        The native reader admits mono/stereo/field routes and refuses decodable.
        False leaves either earlier declaration alone; placement is unchanged."""
        if type(decodable) is not bool:
            raise ValueError('decodable takes bool only')
        if type(crush) is not bool:
            raise ValueError('crush takes bool only')
        note_tempo = False
        converted = {}
        for name, values, field, size, lo, hi in (('delay', delay, 0, 3, 50000, 1000000),
                ('pingpong', pingpong, 0, 4, 50000, 1000000), ('chorus', chorus, 2, 3, 100, 3000)):
            if isinstance(values, (tuple, list)) and len(values) > field and isinstance(values[field], str):
                if len(values) != size:
                    raise ComposeError('fx %s takes %d fields' % (name, size))
                bpm = self._tempo_bpm(at)
                beats = self._note_beats(values[field], at)
                exact = 1000 * bpm / (60 * beats) if name == 'chorus' else 60_000_000 * beats / bpm
                if not lo <= exact <= hi:
                    raise ComposeError('fx %s note value gives %s outside %d..%d' % (name, exact, lo, hi))
                values = list(values)
                values[field] = _tempo_round(exact)
                note_tempo = True
            converted[name] = values
        delay, pingpong, chorus = (converted[name] for name in ('delay', 'pingpong', 'chorus'))
        if (pingpong is not None and (delay is not None or 'delay' in self.fxh)) or (
                delay is not None and 'pingpong' in self.fxh):
            raise ValueError('delay and pingpong share the delay bus: one of them')
        if pingpong is not None:
            if self.space not in ('stereo', 'amb1', 'amb3', 'binaural') or any(k == 'panlaw' for k, _ in getattr(self, 'fieldh', ())):
                raise ValueError('pingpong needs ordinary stereo or a field route without panlaw')
            if not isinstance(pingpong, (tuple, list)) or len(pingpong) != 4 or any(
                    type(v) is not int or not lo <= v <= hi for v, lo, hi in
                    zip(pingpong, (50000, 0, 20, 0), (1000000, 29491, 20000, 2000))):
                raise ValueError('pingpong takes four integers: TIME_US 50000..1000000, FEEDBACK_Q15 0..29491, '
                                 'LOWPASS_HZ 20..20000, HIGHPASS_HZ 0..2000')
        if compressor is not None:
            if not isinstance(compressor, (tuple, list)) or len(compressor) != 5 or any(
                    type(v) is not int or not lo <= v <= hi for v, lo, hi in
                    zip(compressor, (0, 10, 0, 1, 0), (600, 200, 100, 5000, 240))):
                raise ValueError('compressor takes five integers: THRESHOLD_DB10 0..600, '
                                 'RATIO_TENTHS 10..200, ATTACK_MS 0..100, RELEASE_MS 1..5000, '
                                 'MAKEUP_DB10 0..240')
        if flanger is not None:
            if self.space not in (None, 'stereo', 'amb1', 'amb3', 'binaural') or any(k == 'panlaw' for k, _ in getattr(self, 'fieldh', ())):
                raise ValueError('flanger needs mono, ordinary stereo or a field route without panlaw')
            if not isinstance(flanger, (tuple, list)) or len(flanger) != 5 or any(
                    type(v) is not int or not lo <= v <= hi for v, lo, hi in
                    zip(flanger, (100, 100, 0, 0, 0), (10000, 10000, 2000, 29491, 1))) or flanger[1] > flanger[0]:
                raise ValueError('flanger takes five integers: BASE_US 100..10000, DEPTH_US 100..BASE_US, '
                                 'RATE_MILLIHZ 0..2000, FEEDBACK_Q15 0..29491 and INVERT 0/1')
        for k, val, lo, hi in (('gain', gain, -600, 240), ('dither', dither, 1, (1 << 32) - 1)):
            if val is not None and (type(val) is not int or not lo <= val <= hi):
                raise ValueError('fx %s %r: an integer from %d to %d' % (k, val, lo, hi))
        updates = {}
        for k, val in (('dcblock', dcblock), ('chorus', chorus), ('flanger', flanger), ('delay', delay), ('pingpong', pingpong), ('reverb', reverb), ('compressor', compressor), ('limiter', limiter)):
            if val is not None:
                updates[k] = (val,) if isinstance(val, int) else tuple(val)
        if softclip:
            updates['softclip'] = ()
        if decodable:
            updates['decodable'] = ()
        if crush:
            updates['crush'] = ()
        for k, val in (('gain', gain), ('dither', dither)):
            if val is not None:
                updates[k] = (val,)
        self.fxh.update(updates)
        if note_tempo:
            self._tempo_comment = self._tempo_bpm(at)

    def crush(self, s, t, v, bits, div, text=''):
        """Set a voice's bit/rate insert at the first FX block boundary at or after t.

        Requires fx(crush=True), a declared stream/voice, and integer bits1..24,
        div1..64. The initial 24/1 pair bypasses exactly; otherwise unsigned Q2.30
        masking floors negative samples and holds each value for div frames.
        The last pending pair wins. A changed pair resets the hold countdown;
        an identical pair, note-off or retrigger does not. This is not a send.
        """
        if 'crush' not in self.fxh:
            raise ValueError('crush event requires fx(crush=True)')
        if s not in self.streams:
            raise ValueError('crush event needs a declared stream')
        for name, val, lo, hi in (('voice', v, 0, min(63, len(self.voices) - 1)),
                                  ('bits', bits, 1, 24), ('div', div, 1, 64)):
            if type(val) is not int or not lo <= val <= hi:
                raise ValueError('crush %s needs an integer %d..%d' % (name, lo, hi))
        self.seq += 1
        self.ev[s].append((t, 0, self.seq, v, 'crush %d %d' % (bits, div), text))

    def send(self, s, t, v, bus, level, text=''):
        """The voice's send to chorus, delay, reverb, duck or flanger, whose effect fx turned on:
        level Q1.15, 0 to 32768, taken before the pan, from the first block boundary at or
        after t, ramped over the block from there (docs/INSTRUMENT.md, section 3). Every send starts
        at 0. Duck requires compressor; its pre-pan sum drives the linked detector,
        including configured silent senders, without an audible return."""
        if bus == 'duck' and ('compressor' not in self.fxh or type(level) is not int or not 0 <= level <= 32768):
            raise ValueError('duck send requires compressor and integer LEVEL_Q15 0..32768')
        if bus == 'flanger' and ('flanger' not in self.fxh or type(level) is not int or not 0 <= level <= 32768):
            raise ValueError('flanger send requires flanger and integer LEVEL_Q15 0..32768')
        assert bus in FX_BUSES and (bus in self.fxh or bus == 'delay' and 'pingpong' in self.fxh or bus == 'duck' and 'compressor' in self.fxh) and 0 <= level <= 32768
        self.seq += 1
        self.ev[s].append((t, 0, self.seq, v, 'send %s %d' % (bus, level), text))

    def pos(self, s, t, v, x, y, z=0, text=''):
        """The voice's position in mm from the first block boundary at or after t
        (docs/INSTRUMENT.md, section 4): x right, y forward, z up, the listener at the
        origin, ending any move. At one ns it sorts before that voice's note-on, but
        the note starts on its own sample while the gains ramp from the voice's
        previous place over the block from that boundary: at 48 kHz and K = 64, a
        note and a pos at 1 ms start at sample 48 in the previous place and reach
        this one at sample 127. Only a pos at 0 applies at once. Draft2 only."""
        assert self.space and all(abs(c) <= 1 << 24 for c in (x, y, z))
        self.seq += 1
        self.ev[s].append((t, 0, self.seq, v, 'pos %d %d %d' % (x, y, z), text))

    def move(self, s, t, v, dur_ns, x, y, z=0, text=''):
        """A move (docs/INSTRUMENT.md, section 4, ): from where the voice is at the first
        block boundary at or after t, in a straight line in mm to (x, y, z), reached at
        the first boundary at or after t + dur_ns; the next pos or move on the voice
        replaces it. dur_ns is 1 or more; the end may lie past length_ns, where the move
        is cut. Draft2 only."""
        assert self.space and dur_ns >= 1 and all(abs(c) <= 1 << 24 for c in (x, y, z))
        self.seq += 1
        self.ev[s].append((t, 0, self.seq, v, 'move %d %d %d %d' % (x, y, z, dur_ns), text))

    @staticmethod
    def _spatial_int(value, lo, hi, name):
        if type(value) is not int or not lo <= value <= hi:
            raise ComposeError('%s must be an integer %d..%d' % (name, lo, hi))
        return value

    @staticmethod
    def _spatial_round(n, d):
        """Signed rational nearest integer, ties away from zero ."""
        return (1 if n >= 0 else -1) * ((2 * abs(n) + d) // (2 * d))

    def _spatial_span(self, stream, voice, t0, t1):
        if not self.space:
            raise ComposeError('spatial helpers require a spatial Piece')
        if stream not in self.streams:
            raise ComposeError('spatial helper needs an existing stream')
        self._spatial_int(voice, 0, min(63, len(self.voices) - 1), 'voice')
        self._spatial_int(self.fs, 8000, 192000, 'fs')
        if self.block not in (32, 64, 128):
            raise ComposeError('spatial helper block must be 32, 64 or 128')
        self._spatial_int(t0, 0, (1 << 64) - 1, 't0')
        self._spatial_int(t1, t0 + 1, (1 << 64) - 1, 't1')
        if t1 * self.fs // 1_000_000_000 >= 1 << 32:
            raise ComposeError('spatial helper end sample must be below 2^32')
        if self.length_ns is not None and t1 > self.length_ns:
            raise ComposeError('spatial helper end exceeds length_ns')

    def _spatial_xyz(self, xyz):
        return tuple(self._spatial_int(c, -(1 << 24), 1 << 24, 'coordinate') for c in xyz)

    def _spatial_anchor(self, stream, voice, t):
        """Already authored geometry in  insertion order, on the native grid.

        This query belongs only to the new spatial helpers; it does not alter
        steps restoration . Later competing events still apply normally.
        """
        def boundary(ns):
            sample = ns * self.fs // 1_000_000_000
            return (sample + self.block - 1) // self.block
        start = target = (0, 1000, 0)
        b0 = b1 = 0
        def at(b):
            if b1 <= b0 or b >= b1:
                return target
            if b <= b0:
                return start
            return tuple(a + (1 if z >= a else -1) * (abs(z - a) * (b - b0) // (b1 - b0))
                         for a, z in zip(start, target))
        rows, found = [], False
        stop_stream = self.streams.index(stream)
        for si, name in enumerate(self.streams):
            for order, event in enumerate(ordered_events(self.ev[name])):
                if event[3] != voice or not event[4].startswith(('pos ', 'move ')):
                    continue
                if event[0] < t or event[0] == t and si <= stop_stream:
                    rows.append((event[0], si, order, event[4]))
        for ns, _, _, text in sorted(rows):
            words = text.split()
            if len(words) != (4 if words[0] == 'pos' else 5):
                raise ComposeError('existing spatial event has malformed coordinates')
            self._spatial_int(ns, 0, (1 << 64) - 1, 'existing geometry time')
            xyz = self._spatial_xyz(tuple(int(v) for v in words[1:4]))
            if words[0] == 'pos':
                start = target = xyz
                b0 = b1 = 0
            else:
                dur = int(words[4])
                self._spatial_int(dur, 1, (1 << 64) - 1 - ns, 'existing move duration')
                if (ns + dur) * self.fs // 1_000_000_000 >= 1 << 32:
                    raise ComposeError('existing move end sample must be below 2^32')
                b = boundary(ns)
                start, target = at(b), xyz
                b0, b1 = b, boundary(ns + dur)
            found = True
        return at(boundary(t)), found

    def _spatial_periodic(self, stream, voice, t0, t1, rate, endpoints, start):
        """One cumulative rational grid, clipped only at its final segment."""
        self._spatial_int(rate, 1, 50000, 'rate_mhz')
        denominator = rate * len(endpoints)
        # First j whose rounded cumulative endpoint reaches duration.
        count = ((2 * (t1 - t0) - 1) * denominator + 2_000_000_000_000 - 1) // 2_000_000_000_000
        if count > 65536:
            raise ComposeError('spatial helper exceeds 65536 segments')
        when, xyz = t0, start
        for j in range(1, count + 1):
            full = t0 + self._spatial_round(j * 1_000_000_000_000, denominator)
            end = min(full, t1)
            target = endpoints[(j - 1) % len(endpoints)]
            if end <= when:
                raise ComposeError('spatial helper segment has zero duration')
            if end != full:
                target = tuple(a + self._spatial_round((z - a) * (end - when), full - when)
                               for a, z in zip(xyz, target))
            self.move(stream, when, voice, end - when, *self._spatial_xyz(target))
            when, xyz = end, target

    @_perf_atomic
    def autopan(self, stream, voice, t0, t1, rate_mhz, width_mm, shape='tri', depth_mm=1000):
        """Write native pos/move events: triangle2 or sampled-sine8 segments/cycle.

        Starts from existing native geometry, otherwise (-width,depth,0).
        Cumulative ns boundaries round once, and the final segment ends at t1.
        Positions remain timeline events, never matrix targets (/).
        The existing block grid and speed lint apply; competing events can override.
        """
        self._spatial_span(stream, voice, t0, t1)
        self._spatial_int(width_mm, 0, 1 << 24, 'width_mm')
        self._spatial_int(depth_mm, 0, 1 << 24, 'depth_mm')
        if shape not in ('tri', 'sine'):
            raise ComposeError('autopan shape must be tri or sine')
        diagonal = self._spatial_round(width_mm * 759250125, 1 << 30)
        xs = (width_mm, -width_mm) if shape == 'tri' else (
            -diagonal, 0, diagonal, width_mm, diagonal, 0, -diagonal, -width_mm)
        anchor, found = self._spatial_anchor(stream, voice, t0)
        if not found:
            anchor = (-width_mm, depth_mm, 0)
            self.pos(stream, t0, voice, *anchor)
        self._spatial_periodic(stream, voice, t0, t1, rate_mhz,
                               [(x, depth_mm, 0) for x in xs], anchor)

    @_perf_atomic
    def pos_mod(self, stream, voice, t0, t1, source, rate_mhz, depth_mm, axis='x'):
        """Move about current native geometry using tri/sine or offset-ns/Q15 pairs.

        A breakpoint envelope covers [0,t1-t0], starts (0,0), and uses rate=None.
        Other coordinates retain their anchor. Native block timing and speed lint
        apply. Positions stay timeline events, never matrix targets .
        """
        self._spatial_span(stream, voice, t0, t1)
        self._spatial_int(depth_mm, 0, 1 << 24, 'depth_mm')
        if axis not in ('x', 'y', 'z'):
            raise ComposeError('pos_mod axis must be x, y or z')
        dim = 'xyz'.index(axis)
        anchor, found = self._spatial_anchor(stream, voice, t0)
        def placed(value, denominator):
            xyz = list(anchor)
            xyz[dim] += self._spatial_round(depth_mm * value, denominator)
            return self._spatial_xyz(xyz)
        if isinstance(source, str):
            if source not in ('tri', 'sine'):
                raise ComposeError('pos_mod source must be tri, sine or breakpoint pairs')
            u, d = 1 << 30, 759250125
            values = (u, 0, -u, 0) if source == 'tri' else (d, u, d, 0, -d, -u, -d, 0)
            endpoints = [placed(value, u) for value in values]
            if not found:
                self.pos(stream, t0, voice, *anchor)
            self._spatial_periodic(stream, voice, t0, t1, rate_mhz, endpoints, anchor)
        else:
            if rate_mhz is not None or not isinstance(source, (list, tuple)) or len(source) < 2:
                raise ComposeError('pos_mod envelope needs breakpoint pairs and rate_mhz=None')
            points = []
            for row in source:
                if not isinstance(row, (list, tuple)) or len(row) != 2:
                    raise ComposeError('pos_mod envelope rows are (offset_ns,value_q15)')
                ns = self._spatial_int(row[0], 0, t1 - t0, 'envelope offset_ns')
                value = self._spatial_int(row[1], -32768, 32768, 'envelope value_q15')
                if points and ns <= points[-1][0]:
                    raise ComposeError('pos_mod envelope offsets must rise')
                points.append((ns, placed(value, 32768)))
            if source[0][0] != 0 or source[0][1] != 0 or points[-1][0] != t1 - t0:
                raise ComposeError('pos_mod envelope must cover the span and start (0,0)')
            if not found:
                self.pos(stream, t0, voice, *anchor)
            for (a, _), (b, xyz) in zip(points, points[1:]):
                self.move(stream, t0 + a, voice, b - a, *xyz)

    def unison(self, stream, voices, t, midi, n, cents, width_mm, *, vel=MF, compensate=False):
        """ unison; compensate=True reserves carrier slots and writes per-on ctl.

        Compensation is 20*log10(n) dB before the proved carrier output path.
        Ordinary notes on the same patch get zero resets in the writer view.
        False on an unowned Piece retains the original helper/events exactly.
        """
        if type(compensate) is not bool:
            raise ComposeError('unison compensate must be a bool')
        if not compensate:
            return self._unison(stream, voices, t, midi, n, cents, width_mm, vel=vel)
        self._compensation_validate()
        pending = copy.copy(self)
        pending.ev = {s: list(events) for s, events in self.ev.items()}
        pending.mods = {name: dict(rows) for name, rows in self.mods.items()}
        pending._unison_compensation = copy.deepcopy(getattr(self, '_unison_compensation', {}))
        pending._unison_notes = copy.deepcopy(getattr(self, '_unison_notes', {}))
        result = pending._unison(stream, voices, t, midi, n, cents, width_mm, vel=vel)
        name = pending.voices[result[0]][0]
        if name not in pending.patches:
            raise ComposeError('unison compensation needs a defined patch: %s' % name)
        old = pending._unison_compensation.get(name)
        extra, input_ = carrier_attenuation_settings(
            name, pending._macro_operators(name), pending._compensation_features(name),
            pending._compensation_reserved(name), pending.fs, purpose='unison compensation',
            owned=None if old is None else (old['input'], old['rows']))
        rows = tuple((slot, fields) for slot, fields in sorted(extra['mod'].items())
                     if fields[0] == 'ctl%d' % input_)
        attenuation = unison_attenuation(n)
        pending.mods[name] = extra['mod']
        pending._unison_compensation[name] = dict(input=input_, rows=rows,
            maximum=max(attenuation, old['maximum'] if old else 0))
        for event in pending.ev[stream][len(self.ev[stream]):]:
            if event[1] == 1:
                pending._unison_notes[(stream, event[2])] = (event, name, input_, rows, attenuation)
        pending._compensation_validate()
        # No refusal follows publication; existing event/voice/list handles survive.
        for s, events in self.ev.items():
            events.extend(pending.ev[s][len(events):])
        if name in self.mods:
            self.mods[name].clear(); self.mods[name].update(pending.mods[name])
        else:
            self.mods[name] = pending.mods[name]
        self._unison_compensation = pending._unison_compensation
        self._unison_notes = pending._unison_notes
        self.seq = pending.seq
        return result

    def _compensation_features(self, name):
        extra = dict(self.routes.get(name, ({}, None))[0])
        for key, attr in self._morph_maps.items():
            mapping = getattr(self, attr)
            if name in mapping:
                extra[key] = mapping[name]
            else:
                extra.pop(key, None)
        return extra

    def _compensation_ctl_check(self, voice, input_):
        name = self.voices[voice][0]
        owned = getattr(self, '_unison_compensation', {}).get(name)
        if owned is not None and input_ == owned['input']:
            raise ComposeError('unison compensation patch %s: ctl%d is reserved' % (name, input_))

    def _compensation_reserved(self, name):
        used = set()
        for events in self.ev.values():
            for _, _, _, voice, text, _ in events:
                if (voice is not None and 0 <= voice < len(self.voices)
                        and self.voices[voice][0] == name and text.split('#', 1)[0].split()[:1] == ['ctl']):
                    try:
                        words = text.split('#', 1)[0].split()
                        if len(words) != 3 or not 0 <= int(words[1]) < 8:
                            raise ValueError
                        used.add(int(words[1]))
                    except ValueError:
                        raise ComposeError('unison compensation patch %s: invalid authored ctl' % name) from None
        for label, values in self.snapshots.items():
            for voice, inputs in self._snapshot_values(label, values).items():
                if self.voices[voice][0] == name:
                    used.update(inputs)
        return used

    def _compensation_validate(self, levels_db10=None):
        owners = getattr(self, '_unison_compensation', {})
        if not owners:
            return
        for events in self.ev.values():
            for event in events:
                _, kind, _, voice, text, _ = event
                if voice is not None and 0 <= voice < len(self.voices) and self.voices[voice][0] in owners:
                    words = text.split('#', 1)[0].split()
                    if '\n' in text or '\r' in text or (kind == 1) != (words[:1] == ['on']):
                        raise ComposeError('unison compensation patch %s: raw event kind/text mismatch or multiline text' %
                                           self.voices[voice][0])
        for name, spec in owners.items():
            if name not in self.patches:
                raise ComposeError('unison compensation patch %s is missing' % name)
            carrier_attenuation_settings(
                name, self._macro_operators(name), self._compensation_features(name),
                self._compensation_reserved(name), self.fs, purpose='unison compensation',
                owned=(spec['input'], spec['rows']))
            level = self.patches[name][0][1] if levels_db10 is None else levels_db10[name]
            carrier_attenuation_levels(name, self.patches[name], self.routes.get(name, (None, None)),
                                       level, spec['maximum'], vector=name in self.vectors,
                                       purpose='unison compensation')
        annotated = getattr(self, '_unison_notes', {})
        found = {}
        for stream, events in self.ev.items():
            for event in events:
                if event[1] == 1 and (stream, event[2]) in annotated:
                    found.setdefault((stream, event[2]), []).append(event)
        lane_voices = {fields[0] for fields in self.gen_lanes.values()}
        for key, (event, name, input_, rows, attenuation) in annotated.items():
            voice = event[3]
            spec = owners.get(name)
            if (found.get(key) != [event] or not 0 <= voice < len(self.voices)
                    or self.voices[voice][0] != name or spec is None
                    or (input_, rows) != (spec['input'], spec['rows'])
                    or not 0 <= attenuation <= spec['maximum']):
                raise ComposeError('unison compensation patch %s: annotated note or ownership changed' % name)
            if voice in lane_voices:
                raise ComposeError('unison compensation patch %s: manual on conflicts with a gen_lane reserved voice' % name)

    def _compensation_events(self, stream):
        """Insert resets AFTER resolving legacy ordering; never mutate/re-sort ev."""
        events = ordered_events(self.ev[stream])
        owners = getattr(self, '_unison_compensation', {})
        if not owners:
            return events
        result = []
        notes = getattr(self, '_unison_notes', {})
        for event in events:
            t, kind, seq, voice, _, _ = event
            owned = owners.get(self.voices[voice][0]) if kind == 1 and voice is not None else None
            if owned is not None:
                annotation = notes.get((stream, seq))
                attenuation = annotation[4] if annotation is not None else 0
                result.append((t, 4, seq, voice, 'ctl %d %d' %
                               (owned['input'], carrier_attenuation_ctl(attenuation)),
                               'unison compensation %d dB10' % attenuation))
            result.append(event)
        return result

    @_perf_atomic
    def _unison(self, stream, voices, t, midi, n, cents, width_mm, *, vel=MF):
        """Start n1..8 manually supplied voices, low detune leftmost .

        Write fixed positions around unchanged keys_pos, then note-ons. Return
        the voice tuple; the caller writes offs. No allocation/stealing policy.
        Keys versions retain their existing pitch-based placement .
        """
        self._spatial_int(n, 1, 8, 'n')
        if not isinstance(voices, (list, tuple)) or len(voices) != n:
            raise ComposeError('unison needs exactly n existing voices')
        for voice in voices:
            self._spatial_span(stream, voice, t, t + 1 if type(t) is int else t)
        if len(set(voices)) != n or len({self.voices[v][0] for v in voices}) != 1:
            raise ComposeError('unison needs distinct voices of one patch')
        self._spatial_int(midi, 0, 127, 'midi')
        self._spatial_int(width_mm, 0, 1 << 24, 'width_mm')
        self._spatial_int(vel, 0, 32767, 'velocity')
        if type(cents) not in (int, Fraction, decimal.Decimal, str):
            raise ComposeError('unison cents must be finite exact int/Fraction/Decimal/text')
        try:
            spread = Fraction(decimal.Decimal(cents) if isinstance(cents, str) else cents)
        except (ValueError, TypeError, OverflowError, ZeroDivisionError, decimal.InvalidOperation):
            raise ComposeError('unison cents must be finite exact int/Fraction/Decimal/text')
        if not 0 <= spread <= 1200:
            raise ComposeError('unison cents must be 0..1200')
        center = keys_pos(midi)
        for i, voice in enumerate(voices):
            detune = Fraction(0) if n == 1 else -spread / 2 + i * spread / (n - 1)
            offset = 0 if n == 1 else self._spatial_round(-width_mm * (n - 1) + 2 * width_mm * i, n - 1)
            xyz = self._spatial_xyz((center[0] + offset, center[1], center[2]))
            frequency = mhz_m(Fraction(midi) + detune / 100)
            self._spatial_int(frequency, 1, (1 << 32) - 1, 'frequency_millihz')
            self.pos(stream, t, voice, *xyz)
            self.on_f(stream, t, voice, frequency, vel, 'unison %d of %d' % (i + 1, n))
        return tuple(voices)

    def path(self, s, v, points, text=''):
        """The voice along a polyline: points are (t, x, y[, z]) in ns and mm with rising
        t, the voice at the first from its t, then moving in a straight line to each
        next point, reaching it at its t. A pos at the first point, then one move per
        segment: each ends where the next starts, at the same boundary, so the joins are
        seamless. circle_points makes the points of a circle."""
        pts = [(q[0], q[1], q[2], q[3] if len(q) > 3 else 0) for q in points]
        assert all(b[0] > a[0] for a, b in zip(pts, pts[1:])), 'path times must rise'
        self.pos(s, pts[0][0], v, *pts[0][1:], text=text)
        for a, b in zip(pts, pts[1:]):
            self.move(s, a[0], v, b[0] - a[0], *b[1:])

    def field(self, route, order=None, pinna=None, yaw=None, panlaw=None):
        """The field routes (docs/INSTRUMENT.md, sections 11 to 16; , ), set after
        construction: route 'amb1' or 'amb3' (4 or 16 channels, AmbiX), 'binaural' (two
        ears, from a build with a filter table: make BINAURAL_H=PATH), or 'stereo' with
        panlaw W (1 to 32768, Q1.15). order 1 or 3 for binaural; pinna 'off', 'pbnh', 'rd'
        or five D in thousandths; yaw the listener's turn in millidegrees, clockwise. Each
        is written only when given. Draft2 only."""
        assert route in ('stereo', 'amb1', 'amb3', 'binaural')
        assert order is None or (route == 'binaural' and order in (1, 3))
        assert pinna is None or (route != 'stereo' and (pinna in ('off', 'pbnh', 'rd') or
                                                         (len(pinna) == 5 and all(0 <= d <= 1000 for d in pinna))))
        assert yaw is None or (route != 'stereo' and abs(yaw) <= 360000)
        assert panlaw is None or (route == 'stereo' and 1 <= panlaw <= 32768)
        self.space = route
        self.fieldh = [(k, val if isinstance(val, (str, int)) else ' '.join('%d' % d for d in val))
                       for k, val in (('order', order), ('pinna', pinna), ('yaw', yaw), ('panlaw', panlaw))
                       if val is not None]

    def yaw(self, s, t, mdeg, text=''):
        """The listener's turn (docs/INSTRUMENT.md, section 13), absolute, in millidegrees
        clockwise, -360000 to 360000, from the first block boundary at or after t: a source
        at azimuth a is heard at a - yaw. The event is the listener's, so it names voice 0.
        Needs field route amb1, amb3 or binaural."""
        assert self.space in ('amb1', 'amb3', 'binaural') and abs(mdeg) <= 360000
        self.seq += 1
        self.ev[s].append((t, 0, self.seq, 0, 'yaw %d' % mdeg, text))

    def note(self, s, pool, t0, t1, note, vel):
        v = pool.get(t0, t1)
        self.on(s, t0, v, note, vel)
        self.off(s, t1, v)
        return v

    def mark(self, s, t, text):
        self.marks[s].setdefault(t, text)

    def stroke(self, s, v, t, prims, dur_ns, pen=None):
        """One pen-down stroke : prims, primitives joined end to end (line, arc,
        curve; see Drawing), drawn from t in dur_ns at a steady page speed as one note at
        the pen's velocity on voice v, or on a voice of the Pool v. A pos at t places the
        note at the first point, moves carry it across (width is pan), pitch events up
        and down (height is pitch), and an off at t + dur_ns lifts the pen. A dot is a
        stroke of its own: a note of dur_ns in one place. Returns t + dur_ns."""
        import bisect
        pen = pen or Pen()
        t1 = t + dur_ns
        if isinstance(v, Pool):
            v = v.get(t, t1)
        if prims[0][0] == 'dot':
            assert len(prims) == 1, 'a dot is a stroke of its own'
            u, w = prims[0][1]
            assert abs(u) <= 1 + 1e-9 and abs(w) <= 1 + 1e-9, 'the dot lies off the page at %s' % (prims[0][1],)
            m = pen.pitch(w)
            self.pos(s, t, v, *pen.place(u), text='%+.1f degrees' % pen.angle(u))
            self.on_f(s, t, v, mhz_m(m), pen.vel, label(m))
            self.off(s, t1, v)
            return t1
        pts, dist, joins = trace(prims)
        length = dist[-1]
        assert length > 0 and dur_ns > 0, 'a stroke of no length or no time; draw a dot'
        at = {t + int(round(dur_ns * d / length)) for d in joins}   # the corners' times
        times = sorted({t, t1} | at | set(range(t + pen.step_ns, t1, pen.step_ns)))
        uv = []
        for tt in times:                                            # the pen's page point at each time
            d = length * (tt - t) / dur_ns
            i = min(max(bisect.bisect_right(dist, d) - 1, 0), len(dist) - 2)
            f = (d - dist[i]) / (dist[i + 1] - dist[i]) if dist[i + 1] > dist[i] else 0.0
            uv.append((pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f))
        ms = [pen.pitch(q[1]) for q in uv]
        ang = [pen.angle(q[0]) for q in uv]
        where = [pen.place(q[0]) for q in uv]
        self.pos(s, t, v, *where[0], text='%+.1f degrees' % ang[0])
        self.on_f(s, t, v, mhz_m(ms[0]), pen.vel, label(ms[0]))
        last = ms[0]
        for tt, m in zip(times[1:-1], ms[1:-1]):                    # the height: a bend, struck again at a join with strike
            if pen.strike and tt in at:
                self.on_f(s, tt, v, mhz_m(m), pen.vel, label(m))
            elif abs(m - last) * 100 >= pen.cents:
                self.pitch_f(s, tt, v, mhz_m(m), label(m))
            else:
                continue
            last = m
        import math

        def deg(q):                                                 # a written place's pan angle, as the lint takes it
            return math.degrees(math.asin(q[0] / max(math.sqrt(q[0] ** 2 + q[1] ** 2 + q[2] ** 2), self.near)))
        brk = [0]                                                   # the width: a move to each break
        for i in range(1, len(times)):
            if i < len(times) - 1:
                d0, d1 = ang[i] - ang[i - 1], ang[i + 1] - ang[i]
                turns = (d0 > 1e-9, d0 < -1e-9) != (d1 > 1e-9, d1 < -1e-9)   # starts, stops or turns back
                if not (turns or times[i] in at or abs(ang[i + 1] - ang[brk[-1]]) > pen.seg_deg + 1e-9):
                    continue
            brk.append(i)
        for i, j in zip(brk, brk[1:]):
            if where[j] != where[i]:
                self.move(s, times[i], v, times[j] - times[i], *where[j])
                speed = abs(deg(where[j]) - deg(where[i])) * 1e9 / (times[j] - times[i])   # in whole mm, as written
                self.pen_top = max(getattr(self, 'pen_top', (0.0, 0, '', 0)), (speed, times[i], s, v))
        self.off(s, t1, v)
        return t1

    def draw(self, s, v, t, strokes, dur_ns=None, pen=None):
        """A figure: strokes, each a list of primitives, drawn one after another from t
        with the pen's gap between two, in dur_ns in all (the pen's glyph_ns if None):
        each dot takes the pen's dot_ns, and the other strokes share the rest by their
        page length, so the pen keeps one speed. Returns the end: t + dur_ns, or the
        last dot's off for a figure of dots alone."""
        pen = pen or Pen()
        dur_ns = pen.glyph_ns if dur_ns is None else dur_ns
        lengths = [0.0 if q[0][0] == 'dot' else trace(q)[1][-1] for q in strokes]
        rest = dur_ns - (len(strokes) - 1) * pen.gap_ns - lengths.count(0.0) * pen.dot_ns
        total, done = sum(lengths), 0.0
        assert total == 0 or rest > 0, 'the gaps and dots leave no time for the strokes'
        for k, (q, ln) in enumerate(zip(strokes, lengths)):
            t += pen.gap_ns if k else 0
            if ln == 0:
                t = self.stroke(s, v, t, q, pen.dot_ns, pen)
                continue
            a = int(round(rest * done / total))
            done += ln
            t = self.stroke(s, v, t, q, int(round(rest * done / total)) - a, pen)
        return t

    def glyph(self, s, v, t, ch, dur_ns=None, pen=None, box=(-1, -1, 1, 1)):
        """The letter, digit or full stop ch of GLYPHS drawn by draw, its cell fitted to
        the page box (u0, v0, u1, v1), the whole page by default; a mark names it.
        Returns the end."""
        self.mark(s, t, 'the pen draws %s' % ch)
        return self.draw(s, v, t, [[fit(q, box) for q in k] for k in GLYPHS[ch]], dur_ns, pen)

    # -- performance helpers  -------------------------------------
    # They write ordinary events through the writers above, so a record holds
    # nothing new, and every random choice comes from perf_rng .
    def at_beat(self, k):
        """The time in ns of beat k, counted from 0 at the piece's start, a fraction
        allowed: p.t's rule, the tempo map's when there is one, rounded to the ns; a beat
        before 0 takes the first beat's length."""
        if hasattr(self, '_tempo_segments'):
            return self._map_time(k)
        if self.cum is None:
            return int(round(k * self.beat_ns))
        cap = len(self.cum) - 1
        if k > cap:
            raise ComposeError('tempo beat %s is past its %d-beat cap' % (k, cap))
        if k == cap:
            return self.cum[-1]
        if k < 0:
            return int(round(k * (self.cum[1] - self.cum[0])))
        i = int(k // 1)
        return self.cum[i] + int(round((k - i) * (self.cum[i + 1] - self.cum[i])))

    def perf_voices(self, voices, patch, release_ms, what):
        """The voices of a performance helper's call and the RoundRobin that shares them
        : voices, a list of voice numbers of one patch (patch, when given, must be
        theirs), or a number, that many new voices of patch. The calls that name the same
        voices in the same order share one round robin, so their notes must come in time
        order; a list that shares a voice with an earlier call's list without being that
        list stops with an error, as the two round robins could not see each other's
        notes. A voice stays busy until RoundRobin.window(release_ms) after its note-off;
        release_ms is its patch's longest carrier release unless given."""
        if isinstance(voices, int):
            if patch not in self.patches:
                raise ValueError('%s: a number of voices needs a defined patch, not %r' % (what, patch))
            voices = self.add_voices(patch, voices, what)
        voices = list(voices)
        if not voices or len(set(voices)) != len(voices) or any(not 0 <= v < len(self.voices) for v in voices):
            raise ValueError('%s: no such voices %r' % (what, voices))
        pats = sorted({self.voices[v][0] for v in voices})
        if len(pats) != 1 or (patch is not None and pats != [patch]):
            raise ValueError('%s: the voices %r play %s, not one patch%s' % (
                what, voices, ', '.join(pats), '' if patch is None else ' (%s)' % patch))
        if release_ms is None:
            car, mod, _ = self.patches[pats[0]]
            route = self.routes.get(pats[0], ({}, 0))[0]
            ops = [car, mod] + list(route.get('more', ()))
            release_ms = max(o[8] for i, o in enumerate(ops) if route.get('carriers', 1) >> i & 1)
        if not hasattr(self, 'rings'):
            self.rings = {}
        key = tuple(voices)
        for k in self.rings:
            if k != key and set(k) & set(key):
                raise ValueError('%s: the voices %r share a voice with the voices %r of an earlier helper call: name '
                                 'the same list in the same order, or voices of their own' % (what, voices, list(k)))
        ring = self.rings.setdefault(key, RoundRobin(voices, release_ms))
        ring.rel = RoundRobin.window(release_ms)
        return ring

    def perf_rng(self, seed):
        """A performance helper's generator : the fixed LCG (Rng) seeded from the piece's
        seed (p.seed, 0 when unset) plus the call's seed (0 when None) through mix32, never
        Python's global random state ."""
        return Rng(mix32(getattr(self, 'seed', 0) + (seed or 0)))

    def perf_event(self, what, t):
        """A performance helper's event time: within the piece, when length_ns is set."""
        if t < 0 or (self.length_ns is not None and t >= self.length_ns):
            raise ValueError('%s: an event at %d ns lies outside the piece (length_ns %s)' % (what, t, self.length_ns))

    @_perf_atomic
    def arp(self, s, voices, notes=None, start_beat=None, beats=None, rate=None, mode='up', octaves=1, gate=0.8,
            vel=MF, patch=None, seed=None, swing=50, release_ms=None, scale=None, keys=None, latch=False):
        """An arpeggiator  on stream s: the held notes (names or MIDI numbers) sorted
        from the lowest and repeated octaves times an octave apart, one a step from
        start_beat (beats from 0 at the piece's start, a fraction allowed) for beats, a
        step every rate beats, each sounding gate of its step (more than 1 lets it ring
        into the next). mode: 'up'; 'down'; 'updown', up then down without playing the
        top or the bottom twice in a row; 'downup', its mirror, down then up
        (up[::-1] + up[1:-1]); 'played', the notes in the order given, octaves stacked as
        for up; 'chord', every held note at each step, step i raised by 12 x (i mod
        octaves), one voice each with the same on and off, in order from the lowest;
        'random', each step one of up's notes drawn by perf_rng(seed). vel is one
        velocity, a list taken by step number (cycled) or a dict of step number to
        velocity (MF elsewhere), or 'held': the notes are then (note, velocity) pairs and
        each note plays its own velocity in every octave and every mode . swing,
        in percent (50 straight), makes every odd step late by (swing - 50) / 50 of a
        step. The times follow the tempo map. voices, patch and release_ms: see
        perf_voices; each note takes the next voice in turn that is no longer busy, and
        the call stops with an error when none is free rather than take a busy one, so a
        chord step with more notes than free voices is refused. scale (; None, the
        default, writes what the call wrote before it) is a scale lock (scale_mask: a name
        of tables.txt or a mask, or (scale, root)) applied to each note as it sounds, after
        the sort, the octaves and the mode, so the order and the steps stay; notes that
        snap together keep their own voices, ons and offs.

        keys , instead of notes, is a list of (on_beat, off_beat, note, velocity)
        presses. Exactly one of notes and keys is required; keys=[] is all rests. Each
        press needs 0 <= on_ns < off_ns after at_beat; it may extend beyond this call.
        At the swung step onset t0, on_ns <= t0 < off_ns holds the press. vel='held'
        uses that press's velocity; other vel sources override it. 'played' follows
        press-time order, ties in input order; duplicate notes remain separate presses.
        Partial releases remove keys. With latch=True, when the last keys release the
        immediately preceding nonempty set stays; simultaneous releases are one group,
        so releasing a whole chord together retains that chord. The next press after
        all keys are up starts a new set, even between steps. At one ns all releases
        precede all presses, then the step samples the set: an on exactly at t0 is in,
        an off exactly at t0 is out (or latched when it empties the set). The step index
        never restarts; an empty set rests but still draws once in random mode.
        latch must be a bool, and True without keys is refused. keys=None preserves the
        static call. keys/latch change which notes sound and when, not placement: pos,
        move and keys-version places follow the same rules as for notes.
        Returns [(on_ns, off_ns, voice, midi)], a chord step's notes from the lowest,
        midi the sounding note."""
        import math
        what = 'arp on %s' % s
        lock = scale_locked(scale, what)
        if mode not in ('up', 'down', 'updown', 'downup', 'played', 'chord', 'random'):
            raise ValueError('%s: no mode %r; the modes are up, down, updown, downup, played, chord and random'
                             % (what, mode))
        if (notes is None) == (keys is None):
            raise ValueError('%s: give exactly one of notes or keys' % what)
        if type(latch) is not bool or (latch and keys is None):
            raise ValueError('%s: latch must be a bool; latch=True needs keys' % what)
        if start_beat is None or beats is None or rate is None:
            raise ValueError('%s: needs start_beat, beats and rate' % what)
        if (keys is None and not notes) or octaves < 1 or rate <= 0 or beats <= 0 or not 0 < swing < 100:
            raise ValueError('%s: needs notes, octaves 1 or more, rate and beats above 0 and swing 0 to 100' % what)
        presses = []
        if keys is not None:
            if not isinstance(keys, (tuple, list)):
                raise ValueError('%s: keys is a list of (on_beat, off_beat, note, velocity) presses' % what)
            for k, key in enumerate(keys):
                if not isinstance(key, (tuple, list)) or len(key) != 4:
                    raise ValueError('%s: keys[%d] needs (on_beat, off_beat, note, velocity)' % (what, k))
                on, off, note, hv = key
                try:
                    on, off, m, hv = self.at_beat(on), self.at_beat(off), as_midi(note), int(hv)
                except (TypeError, ValueError, OverflowError, IndexError) as e:
                    raise ValueError('%s: keys[%d]: %s' % (what, k, e)) from e
                if not 0 <= on < off or not math.isfinite(m) or not 0 <= hv <= 32767:
                    raise ValueError('%s: keys[%d] needs 0 <= on_ns < off_ns, a finite note and velocity 0..32767'
                                     % (what, k))
                presses.append((on, off, m, hv if vel == 'held' else None, k))
                for o in range(int(octaves)):
                    lock(m + 12 * o)
            given = []
        elif vel == 'held':
            pairs = [isinstance(n, (tuple, list)) for n in notes]
            if not all(pairs) or any(len(n) != 2 for n in notes):
                raise ValueError("%s: vel='held' takes the notes as (note, velocity) pairs, not %r" % (what, notes))
            given = [(as_midi(n), int(hv)) for n, hv in notes]
            if any(not 0 <= hv <= 32767 for _, hv in given):
                raise ValueError('%s: a held velocity lies outside 0 to 32767: %r' % (what, notes))
        elif any(isinstance(n, (tuple, list)) for n in notes):
            raise ValueError("%s: notes given as (note, velocity) pairs need vel='held'" % what)
        else:
            given = [(as_midi(n), None) for n in notes]

        held, up, order = _arp_ordered(given, mode, octaves)
        for m, _ in up:
            lock(m)                                             # every sounding note locks, before any event
        rng = self.perf_rng(seed)
        ring = self.perf_voices(voices, patch, release_ms, what)
        def timings():
            for i in range(int(math.ceil(beats / rate - 1e-9))):
                g, sv = step_value(gate, i, 0.8), None if vel == 'held' else int(step_value(vel, i, MF))
                b = start_beat + i * rate + (rate * (swing - 50) / 50 if i % 2 else 0)
                t0, t1 = self.at_beat(b), self.at_beat(b + g * rate)
                yield t0, (i, t1, g, sv)

        def selections():
            if keys is not None:
                yield from _arp_key_steps(presses, timings(), mode=mode, octaves=octaves,
                                          latch=latch, seed=rng.x)
                return
            for t0, payload in timings():
                i = payload[0]
                if mode == 'random':
                    draw = rng.next()                    # the unchanged static-note stream
                    at = [up[int(draw * len(up))]] if up else []
                elif mode == 'chord':
                    at = [(m + 12 * (i % int(octaves)), hv) for m, hv in held]
                else:
                    at = [order[i % len(order)]] if order else []
                yield t0, payload, [(m, hv, None) for m, hv in at]

        out = []
        for t0, (i, t1, g, sv), at in selections():
            for m, hv, _ in at:
                m = lock(m)
                v = sv if hv is None else hv
                if not t0 < t1 or not 0 <= v <= 32767:
                    raise ValueError('%s: step %d has gate %r and velocity %r' % (what, i, g, v))
                self.perf_event(what, t0)
                self.perf_event(what, t1)
                voice = ring.get(t0, t1)
                self.on_f(s, t0, voice, mhz_m(m), v, label(m))
                self.off(s, t1, voice)
                out.append((t0, t1, voice, m))
        return out

    @_perf_atomic
    def steps(self, s, voices, pattern, start_beat, step_beats, notes, vel=MF, prob=None, ratchet=None, micro=None,
              seed=None, locks=None, gate=0.8, swing=50, patch=None, release_ms=None, text=None, passes=1, pass0=0,
              cond=None, fill=(), slide=None, scale=None, nei=None):
        """A step sequencer  on stream s. pattern: a string, x a step that plays and .
        a rest (spaces and | are left out, for reading), or a list, each true item a step
        that plays. Step i sits at start_beat + i * step_beats (beats from 0 at the piece's
        start, a fraction allowed), every odd step late by (swing - 50) / 50 of a step
        (swing in percent, 50 straight); the times follow the tempo map. notes, vel, prob,
        ratchet, micro and gate are each one value for every step, a list taken by step
        number (cycled) or a dict of step number to value (the default elsewhere):
          notes    the step's note, a name or a MIDI number;
          vel      its velocity (MF);
          prob     the chance, 0 to 1, that it plays (1): one draw of perf_rng(seed) for
                   every step of the pattern, rests included, so the same seeds decide
                   the same steps, and a change to one step's chance moves no other's;
          ratchet  1 to 4 hits spread evenly over the step (1);
          micro    a shift in beats, + late and - early, at most half a step either way
                   (0); the hits of a step that would start before 0 or end at or past
                   length_ns move inside it, and one that cannot stay within half a step
                   of its place stops with an error;
          gate     the part of the step, or of a ratchet's share of it, that each hit
                   sounds (0.8); more than 1 lets it ring into the next.
        locks, a dict of step number to {kind: value}, are that step's parameter locks,
        written with each of its hits: 'pitch': a note, as a pitch event at the note-on,
        so that the note sounds there; 'filter': (hz, q), a filter event at the note-on
        (the patch needs a filter); 'send': (bus, level), a send before the note-on, and
        the voice's previous level on that bus (0 when none) again at the end of the
        voice's busy window (see perf_voices); 'pos': (x, y) or (x, y, z) in mm, a pos
        before the note-on, and the voice's previous place again at the end of the busy
        window (draft2 only); 'ctl': (N, VALUE_Q15), or a list of up to 8 such pairs, N 0 to 7
        once each (ctl_locks), modulation matrix controller events : each a ctl at the note-on called
        before it and before the step's other locks, in rising N, so the call order of
        ordered_events puts it first, and the voice's previous value on input N (the last ctl
        N on that voice at or before the note-on among the events written before this call,
        0 when none, the engine's start) again at the end of the busy window, after the send
        and pos restores. A restore is unconditional: any later event on that input applies
        in time order and, at the same ns, in call order, so a later hit of this call on the
        voice, reused at the restore's ns, follows it and its lock wins. The pitch and filter
        locks last until the voice's next note-on. slide, one value, a list by step or a dict
        (False), is True on a step whose ctl locks glide to the next hit of the call (time
        order), which must lock every one of those inputs and play on the same voice: with
        a at step g and b at step g', k = SLIDE_POINTS x (g' - g), point q of k (q = 1 to
        k - 1) is a + sign(b - a) x floor(q x |b - a| / k) at t + floor(q x (t' - t) / k) ns,
        fm_glide_t's rule (fm_a1.h), the next hit's lock supplying b; a sliding step has one
        hit and no restore, and its points are called after its locks, before its on. A ctl
        lock equals a ctl_lane called before this call at the hits' times. voices, patch and
        release_ms as for arp. text names the hits in the .tl comments (the note by default).
        Conditional trigs: the pattern plays passes times back to
        back, passes p = pass0 .. pass0 + passes - 1 (pass0 continues a count from an earlier
        call). Step i of pass p is global step g = (p - pass0) x len(pattern) + i: g takes the
        place, the swing parity and the micro rule above, and the .tl comments and the return
        name it. The per-step arguments (notes, vel, prob, ratchet, micro, gate, locks, cond)
        are taken by i, so every pass repeats the pattern's values. cond, one value, a list by
        step or a dict, is each step's condition (cond_holds): None, 'first', 'not-first',
        'A:B', 'fill', 'not-fill', 'pre', 'not-pre' or 'nei'; fill names fill passes.
        PRE starts false each pass and records the last earlier conditional step's
        played bit; unconditional steps leave it unchanged. A rest or a rejected
        conditional step records false, and ratchets count once. NEI snapshots
        an earlier steps return's global steps, ignoring duplicate ratchets;
        a nei condition needs this explicit source. A step
        plays when it is a hit, its condition holds and its draw is below its prob; the draws
        are one for every (pass, step), pass-major, taken before and regardless of the hit,
        the condition or a rest, so one pass with no cond writes what the call wrote before
        conditions. scale (; None, the default, writes what the call wrote before it) is
        a scale lock (scale_mask) applied to each step's note and pitch lock as it sounds, the
        .tl comments naming the sounding note. Returns [(on_ns, off_ns, voice, midi, step)] in
        time order, step = g, midi the sounding note."""
        what = 'steps on %s' % s
        lock = scale_locked(scale, what)
        if isinstance(pattern, str):
            bad = sorted(set(pattern) - set('x.| '))
            if bad:
                raise ValueError('%s: the pattern %r holds %s; a pattern is x, ., | and spaces' % (what, pattern, ''.join(bad)))
            hits = [c == 'x' for c in pattern if c in 'x.']
        else:
            hits = [bool(c) for c in pattern]
        if not hits or step_beats <= 0 or not 0 < swing < 100:
            raise ValueError('%s: needs a pattern, step_beats above 0 and swing 0 to 100' % what)
        if (type(passes) is not int or passes < 1 or type(pass0) is not int or pass0 < 0 or isinstance(fill, str)
                or any(type(f) is not int or f < 0 for f in fill)):
            raise ValueError('%s: passes is a whole number 1 or more, pass0 one 0 or more and fill a set of pass '
                             'numbers, not %r, %r and %r' % (what, passes, pass0, fill))
        fill = frozenset(fill)
        conditions = list(cond.values() if isinstance(cond, dict) else cond if isinstance(cond, (list, tuple)) else [cond])
        for c in conditions:
            cond_holds(c, 0, fill)                                  # every condition spelled right, before any draw
        if 'nei' in conditions and nei is None:
            raise ValueError('%s: nei condition needs an earlier steps() return' % what)
        neighbors = set()
        if nei is not None:
            try:
                source = list(nei)
            except TypeError:
                raise ValueError('%s: nei needs earlier five-field steps() rows' % what) from None
            for row in source:
                if (not isinstance(row, (list, tuple)) or len(row) != 5
                        or type(row[4]) is not int or row[4] < 0):
                    raise ValueError('%s: nei rows need five fields and a nonnegative integer global step' % what)
                neighbors.add(row[4])
        locks = locks or {}
        if not isinstance(locks, dict) or any(not isinstance(i, int) or not isinstance(k, dict) for i, k in locks.items()):
            raise ValueError('%s: locks is a dict of step number to {kind: value}' % what)
        ctls = {}                                                   # step -> its ctl locks [(n, value)], n rising
        for i, lk in sorted(locks.items()):
            for kind in lk:
                if kind not in ('pitch', 'filter', 'send', 'pos', 'ctl'):
                    raise ValueError('%s: step %d locks %r; the locks are pitch, filter, send, pos and ctl'
                                     % (what, i, kind))
            if 'ctl' in lk:
                ctls[i] = ctl_locks(lk['ctl'], '%s: step %d' % (what, i))
            if 'send' in lk and lk['send'][0] not in self.fxh:
                raise ValueError('%s: step %d sends to %r, an effect fx has not turned on' % (what, i, lk['send'][0]))
            if 'pos' in lk and not self.space:
                raise ValueError('%s: step %d locks pos, which needs a draft2 piece (space=)' % (what, i))
        rng = self.perf_rng(seed)
        ring = self.perf_voices(voices, patch, release_ms, what)
        if any('filter' in lk for lk in locks.values()) and self.voices[ring.v[0]][0] not in self.filters:
            raise ValueError('%s: a filter lock needs a filter on patch %s (p.filter)' % (what, self.voices[ring.v[0]][0]))
        n0 = {st: len(self.ev[st]) for st in self.streams}          # the events before this call
        last = self.length_ns - 1 if self.length_ns is not None else None
        plan = []
        pre = False
        for st in range(passes * len(hits)):                       # st is the global step g, pass-major
            p, i = pass0 + st // len(hits), st % len(hits)          # its pass and its step in the pattern
            if i == 0:
                pre = False                                      # reset at every pass, including pass0
            on = hits[i]
            draw = rng.next()                                       # one draw a (pass, step), rests included
            pr = step_value(prob, i, 1.0)
            if not 0 <= pr <= 1:
                raise ValueError('%s: step %d has prob %r, not 0 to 1' % (what, st, pr))
            condition = step_value(cond, i, None)
            played = on and draw < pr and cond_holds(condition, p, fill, pre=pre, nei=st in neighbors)
            if condition is not None:
                pre = bool(played)                               # conditional rests/rejections also replace PRE
            if not played:
                continue
            note = step_value(notes, i, None)
            r, g, v = step_value(ratchet, i, 1), step_value(gate, i, 0.8), int(step_value(vel, i, MF))
            if note is None or r not in (1, 2, 3, 4) or g <= 0 or not 0 <= v <= 32767:
                raise ValueError('%s: step %d has note %r, ratchet %r, gate %r and velocity %r' % (what, st, note, r, g, v))
            sl = step_value(slide, i, False)
            if type(sl) is not bool or (sl and (i not in ctls or r != 1)):
                raise ValueError('%s: step %d has slide %r; a slide is True or False, and a step that slides has '
                                 'ctl locks and one hit' % (what, st, sl))
            m, half = lock(as_midi(note)), step_beats / 2
            mic = max(-half, min(half, step_value(micro, i, 0.0)))
            grid = start_beat + st * step_beats + (step_beats * (swing - 50) / 50 if st % 2 else 0)
            sub = step_beats / r
            ons = [self.at_beat(grid + mic + j * sub) for j in range(r)]
            offs = [self.at_beat(grid + mic + (j + g) * sub) for j in range(r)]
            shift = max(0, -ons[0])                                 # into the piece, whole ns
            if last is not None:
                shift = min(shift, last - max(offs))
            lo, hi = max(0, self.at_beat(grid - half)), self.at_beat(grid + half)
            if not lo <= ons[0] + shift <= hi or any(a >= b for a, b in zip(ons, offs)):
                raise ValueError('%s: step %d cannot sound within half a step of its place and inside the piece'
                                 % (what, st))
            name = '%s, step %d' % (text or label(m), st)
            for j in range(r):
                plan.append((ons[j] + shift, st, j, offs[j] + shift, m, v,
                             name + (', hit %d of %d' % (j + 1, r) if r > 1 else ''), i, sl))

        def before(voice, t, heads):
            """voice's last event at or before t starting with one of heads, among the events
            written before this call, or None."""
            best = None
            for st in self.streams:
                for e in self.ev[st][:n0[st]]:
                    if e[3] == voice and e[0] <= t and e[4].startswith(heads) and (best is None or e[:3] > best[:3]):
                        best = e
            return best

        out, plan, sliding = [], sorted(plan), None
        for h, (t0, st, j, t1, m, v, name, i, sl) in enumerate(plan):
            self.perf_event(what, t0)
            self.perf_event(what, t1)
            voice = ring.get(t0, t1)
            lk, free = locks.get(i, {}), t1 + ring.rel
            undo = self.length_ns is None or free < self.length_ns
            if sliding is not None and sliding != voice:
                raise ValueError('%s: step %d slides to step %d on another voice (%d, then %d); a slide stays on '
                                 'one voice' % (what, plan[h - 1][1], st, sliding, voice))
            for n, value in ctls.get(i, ()):                       # : the ctl locks, called before the on
                self.ctl(s, t0, voice, n, value, '%s, ctl %d lock' % (name, n))
            sliding = None
            if sl:                                                  # to the next hit's locks, fm_glide_t's rule
                if h + 1 == len(plan):
                    raise ValueError('%s: step %d slides, but no later hit of this call follows it' % (what, st))
                u0, ust, ui = plan[h + 1][0], plan[h + 1][1], plan[h + 1][7]
                to = dict(ctls.get(ui, ()))
                if ust <= st or any(n not in to for n, _ in ctls[i]):
                    raise ValueError('%s: step %d slides ctl %s to step %d, which does not lock them all or '
                                     'does not follow it' % (what, st, ' '.join(str(n) for n, _ in ctls[i]), ust))
                k = SLIDE_POINTS * (ust - st)
                for n, a in ctls[i]:
                    d = to[n] - a
                    for q in range(1, k):
                        self.ctl(s, t0 + q * (u0 - t0) // k, voice, n, a + (q * abs(d) // k) * (1 if d >= 0 else -1),
                                 '%s, ctl %d slide %d of %d' % (name, n, q, k))
                sliding = voice
            if 'pos' in lk:
                was_pos = before(voice, t0, ('pos ', 'move '))
                if was_pos is None:
                    raise ValueError('%s: step %d locks pos on voice %d, which has no place to go back to: place '
                                     'it with p.pos first' % (what, i, voice))
                self.pos(s, t0, voice, *lk['pos'], text='%s, pos lock' % name)
            if 'send' in lk:
                bus, level = lk['send']
                was_send = before(voice, t0, ('send %s ' % bus,))
                self.send(s, t0, voice, bus, level, '%s, send lock' % name)
            self.on_f(s, t0, voice, mhz_m(m), v, name)
            if 'pitch' in lk:
                pm = lock(as_midi(lk['pitch']))
                self.pitch_f(s, t0, voice, mhz_m(pm), '%s, pitch lock %s' % (name, label(pm)))
            if 'filter' in lk:
                self.cutoff(s, t0, voice, *lk['filter'], text='%s, filter lock' % name)
            self.off(s, t1, voice)
            if undo and 'send' in lk:
                self.send(s, free, voice, bus, int(was_send[4].split()[2]) if was_send else 0,
                          '%s, send lock released' % name)
            if undo and 'pos' in lk:
                self.pos(s, free, voice, *map(int, was_pos[4].split()[1:4]), text='%s, pos lock released' % name)
            for n, _ in ctls.get(i, ()) if undo and not sl else ():  # the value before the call (0 when none)
                was = before(voice, t0, ('ctl %d ' % n,))
                self.ctl(s, free, voice, n, int(was[4].split()[2]) if was else 0, '%s, ctl %d lock released' % (name, n))
            out.append((t0, t1, voice, m, st))
        return out

    @_perf_atomic
    def chord(self, s, voices, key, t, dur, vel, shape, strum_ms=0, direction='up', patch=None, release_ms=None,
              scale=None):
        """A chord from chord memory, strummed, on
        stream s: key + offset for each offset of shape, a chord_shape (1 to 8 whole
        semitone offsets rising from 0), every note within MIDI 0..127. t and dur are whole
        ns. strum_ms, 0 to 200 in whole ns (an int, or a decimal string or Decimal such as
        '12.5'; a float is refused, ), is the strum's total spread: note k of n in strum
        order, from the lowest for direction 'up' and from the highest for 'down', sounds at
        t + (2 k S + (n - 1)) div (2 (n - 1)) ns with S = strum_ms x 10^6 (rounded half up;
        one note at t), and every off falls at t + dur, unshifted, so the chord releases
        together. A strum whose last onset, t + S, reaches t + dur is refused. vel is one
        velocity or a list of n in strum order. voices, patch and release_ms: see
        perf_voices; each note takes the next free voice in turn, and the call is refused,
        writing nothing, when too few are free. The piece-local chord(..., roll=) and strum
        helpers, a gap per note, are other functions. scale (; None, the default,
        writes what the call wrote before it) is a scale lock (scale_mask) applied to each
        note of key + shape; notes that snap together keep their own voices and strum
        onsets. Returns [(on_ns, off_ns, voice, midi)] in strum order, midi the sounding
        note."""
        from fractions import Fraction
        what = 'chord on %s' % s
        lock = scale_locked(scale, what)
        if type(t) is not int or type(dur) is not int or t < 0 or dur <= 0:
            raise ValueError('%s: t and dur are whole ns, t 0 or more and dur above 0, not %r and %r' % (what, t, dur))
        if (not isinstance(shape, (list, tuple)) or not shape or any(type(o) is not int for o in shape)
                or tuple(shape) != chord_shape(shape)):
            raise ValueError('%s: the shape is 1 to 8 whole semitone offsets rising from 0 (chord_shape), not %r'
                             % (what, shape))
        if direction not in ('up', 'down'):
            raise ValueError("%s: no direction %r; a strum goes 'up' or 'down'" % (what, direction))
        try:
            S = Fraction(strum_ms) * 1_000_000 if isinstance(strum_ms, (int, str, decimal.Decimal)) \
                and not isinstance(strum_ms, bool) else None
        except (ValueError, ZeroDivisionError, OverflowError):
            S = None
        if S is None or S.denominator != 1 or not 0 <= S <= 200_000_000:
            raise ValueError('%s: strum_ms runs from 0 to 200 in whole ns, as an int or a decimal string, not %r'
                             % (what, strum_ms))
        S, n = int(S), len(shape)
        vels = list(vel) if isinstance(vel, (list, tuple)) else [vel] * n
        if len(vels) != n or any(type(v) is not int or not 0 <= v <= 32767 for v in vels):
            raise ValueError('%s: vel is one velocity or a list of %d in strum order, each 0 to 32767, not %r'
                             % (what, n, vel))
        ms = [as_midi(key) + o for o in shape]
        if any(not 0 <= m <= 127 for m in ms):
            raise ValueError('%s: key %r and shape %r give notes outside MIDI 0 to 127' % (what, key, shape))
        ms = [lock(m) for m in ms]
        if n > 1 and S >= dur:
            raise ValueError("%s: the strum's last onset, %d ns after t, reaches the release at t + dur, %d ns after t"
                             % (what, S, dur))
        ring = self.perf_voices(voices, patch, release_ms, what)
        out = []
        for k, (m, v) in enumerate(zip(ms if direction == 'up' else ms[::-1], vels)):
            t0, t1 = t + ((2 * k * S + (n - 1)) // (2 * (n - 1)) if n > 1 else 0), t + dur
            self.perf_event(what, t0)
            self.perf_event(what, t1)
            voice = ring.get(t0, t1)
            self.on_f(s, t0, voice, mhz_m(m), v, label(m))
            self.off(s, t1, voice)
            out.append((t0, t1, voice, m))
        return out

    # -- files --------------------------------------------------------
    def write(self, levels_db10=None):
        self._compensation_validate(levels_db10)
        self._gen_validate()
        self._pd_validate()
        self._vector_validate()
        self._formant_validate()
        self._wt_validate()
        self._ks_validate()
        d = self.dir
        os.makedirs(d, exist_ok=True)
        for s in self.streams:
            events = self._compensation_events(s)
            with open(os.path.join(d, s + '.tl'), 'w', encoding='utf-8', newline='\n') as f:
                f.write('# %s, stream "%s": %s\n' % (self.name, s, self.roles[s]))
                kinds = ['T_NS STREAM VOICE on FREQ_MILLIHZ VELOCITY_Q15']
                if any(e[1] == 2 for e in self.ev[s]):
                    kinds.append('T_NS STREAM VOICE pitch FREQ_MILLIHZ')
                if any(e[4].startswith('yaw ') for e in self.ev[s]):
                    kinds.append('T_NS STREAM 0 yaw MILLIDEGREES')
                if any(e[4].startswith('pos ') for e in self.ev[s]):
                    kinds.append('T_NS STREAM VOICE pos X_MM Y_MM Z_MM')
                if any(e[4].startswith('move ') for e in self.ev[s]):
                    kinds.append('T_NS STREAM VOICE move X_MM Y_MM Z_MM DUR_NS')
                if any(e[4].startswith('send ') for e in self.ev[s]):
                    kinds.append('T_NS STREAM VOICE send BUS LEVEL_Q15')
                if any(e[4].startswith('crush ') for e in self.ev[s]):
                    kinds.append('T_NS STREAM VOICE crush BITS RATE_DIV')
                if any(e[1] == 3 for e in self.ev[s]):
                    kinds.append('T_NS STREAM VOICE filter CUTOFF_MILLIHZ Q_MILLI')
                if any(e[4].startswith('ctl ') for e in events):
                    kinds.append('T_NS STREAM VOICE ctl N VALUE_Q15')
                if any(e[4].startswith('gen ') for e in self.ev[s]):
                    kinds.append('T_NS STREAM gen ID PARAM VALUE')
                if any(e[4].startswith('lane ') for e in self.ev[s]):
                    kinds.append('T_NS STREAM lane ID enable VALUE')
                kinds.append('T_NS STREAM VOICE off')
                f.write('# Lines: %s.\n' % ', or '.join(kinds) if len(kinds) == 2
                        else '# Lines: %s,\n# or %s.\n' % (', '.join(kinds[:-1]), kinds[-1]))
                if self.cum is None:
                    f.write('# %d bpm: a beat is %d ns, a bar %d ns.\n' % (self.bpm, self.beat_ns, self.beat_ns * self.beats))
                else:
                    f.write('# %d bpm (a beat %d ns) with a tempo map; times are rounded to the ns.\n' % (self.bpm, self.beat_ns))
                marks = sorted(self.marks[s].items())
                for t, kind, seq, v, text, nm in events:
                    while marks and marks[0][0] <= t:
                        f.write('# %s\n' % marks.pop(0)[1])
                    line = ('%12d %s %s' % (t,s,text) if v is None else '%12d %s %2d %s' % (t,s,v,text))
                    f.write(line + ('   # %s\n' % nm if nm else '\n'))
        self.write_session(levels_db10)

    def write_session(self, levels_db10=None, hash_=None):
        self._compensation_validate(levels_db10)
        self._gen_validate()
        self._pd_validate()
        self._vector_validate()
        self._formant_validate()
        self._wt_validate()
        self._ks_validate()
        d = self.dir
        for name in self.fenvs:
            if name not in self.filters:
                raise ValueError('patch %s: fenv needs a filter statement' % name)
        for name, definitions in self.macros.items():
            macro_budget(self.mods.get(name, {}), definitions, [], self._macro_operators(name),
                         self._mod_features(name), self.fs)
        mods = {name: mod_settings(self.mods[name], self._mod_features(name), self.fs)
                for name in self.patches if name in self.mods}
        with open(os.path.join(d, 'session.tl'), 'w', encoding='utf-8', newline='\n') as f:
            for line in self.about:
                f.write('# %s\n' % line)
            if hasattr(self, '_tempo_rows'):
                f.write('# tempo %s\n' % self._tempo_rows[0][1])
            elif hasattr(self, '_tempo_comment'):
                f.write('# tempo %s\n' % self._tempo_comment)
            if d.startswith(MUSIC):
                f.write('# Render and play: tools/music/play.sh %s\n' % self.name)
            else:
                f.write('# An unpinned profile render (compose.py --profile=%s), not a committed record.\n' % PROFILE)
            f.write('timeline draft%d\n' % (2 if self.space else 1))
            f.write('engine %s\n' % ENGINE)
            f.write('fs %d\n' % self.fs)
            if self.gen_ctl is not None:
                f.write('gen_clock %d\n' % self.gen_ctl)
            f.write('length_ns %d\n' % self.length_ns)
            if self.space:
                f.write('space %s\nblock %d\nnear %d\n' % (self.space, self.block, self.near))
                f.write(''.join('%s %s\n' % kv for kv in getattr(self, 'fieldh', ())))   # field's, when present
            for k in ('dcblock', 'chorus', 'flanger', 'delay', 'pingpong', 'reverb', 'compressor', 'limiter', 'softclip', 'gain', 'dither', 'crush', 'decodable'):   # the effects, each when present
                if k in self.fxh:
                    f.write(' '.join([k] + ['%d' % x for x in self.fxh[k]]) + '\n')
            f.write('streams %s\n' % ' '.join(self.streams))
            for id,config in sorted(self.generators.items()):
                f.write(gen_row(id,config)+'\n')
                if id in self.gen_axioms:
                    f.write('gen_axiom %d %d %s\n' % (id,*self.gen_axioms[id]))
                for symbol,word in sorted(self.gen_rules.get(id,{}).items()):
                    f.write('gen_rule %d %s %s\n' % (id,symbol,word))
            every = [o for name, (car, mod, _) in self.patches.items()
                     for o in [car, mod] + list(self.routes[name][0]['more'] if name in self.routes else [])]
            ks, wv, w2 = (any(any(o[k:]) for o in every) for k in (9, 11, 12))
            f.write(HEAD + (KS_HEAD if ks else '') + (' wave' if wv else '') +
                    (' rate_scale fixed_millihz phase_u16 free_run' if w2 else '') + '\n')
            routes = []
            for name, (car, mod, comment) in self.patches.items():
                extra, base = self.routes.get(name, (None, None))
                rows, route = patch_lines(name, car, mod, comment, extra,
                                          car[1] if levels_db10 is None else levels_db10[name], base)
                f.write(''.join(row + '\n' for row in rows))
                routes += [route + '\n'] if route else []
            f.writelines(routes)                # after every patch, as the harness writes them
            for name, values in self.filters.items():
                f.write(filter_row(name, *values) + '\n')
            for name, values in self.shapes.items():
                f.write(shape_row(name, *values) + '\n')
            for name, values in self.amps.items():
                f.write(amp_row(name, *values) + '\n')
            for name, values in self.lfos.items():
                f.write(lfo_row(name, *values) + '\n')
            for name, values in self.pegs.items():
                ops = self.routes[name][0]['ops'] if name in self.routes else 2
                f.write(peg_row(name, *values, ops=ops) + '\n')
            for name, values in self.fenvs.items():
                f.write(fenv_row(name, *values) + '\n')
            for name, settings in mods.items():
                for slot, fields in sorted(settings.items()):
                    f.write(mod_row(name, slot, *fields) + '\n')
            for name, settings in self.egs.items():
                ops = self.routes[name][0]['ops'] if name in self.routes else 2
                for i, values in sorted(settings.items()):
                    fields = eg_values((i, *values), ops)
                    every = list(self.patches[name][:2]) + list(self.routes[name][0]['more'] if name in self.routes else [])
                    eg_values(fields, sustain1=every[i][7])
                    if any(fields[1:]):
                        f.write(eg_row(name, *fields) + '\n')
            for name, settings in self.left_slopes.items():
                ops = self.routes[name][0]['ops'] if name in self.routes else 2
                for i, value in sorted(settings.items()):
                    fields = ks_values((i, value), ops, self.routes[name][0] if name in self.routes else None)
                    if value:
                        f.write(ks_row(name, *fields) + '\n')
            for name in self.patches:
                ops = self.routes[name][0]['ops'] if name in self.routes else 2
                for i in range(ops):
                    for kind, mapping in (('kstype', self.kstypes), ('fbtype', self.fbtypes), ('egloop', self.egloops)):
                        if i in mapping.get(name, {}):
                            row = op_ext_row(kind, name, i, *mapping[name][i], ops=ops)
                            if row:
                                f.write(row + '\n')
            for name, settings in self.noise_shifts.items():
                ops = self.routes[name][0]['ops'] if name in self.routes else 2
                for i, value in sorted(settings.items()):
                    fields = noise_values((i, value), ops)
                    o = (list(self.patches[name][:2]) + list(self.routes[name][0]['more'] if name in self.routes else []))[i]
                    noise_values(fields, wave=o[11] if len(o) > 11 else 0)
                    if value != 4:
                        f.write(noise_row(name, *fields) + '\n')
            for name in self.patches:
                for i,knee in sorted(self.pds.get(name,{}).items()):
                    f.write(pd_row(name,i,knee)+'\n')
            for name in self.patches:
                if name in self.vectors:
                    f.write(vector_row(name,*self.vectors[name])+'\n')
            for name in self.patches:
                for slot, fields in sorted(self.formants.get(name, {}).items()):
                    f.write(formant_row(name, slot, *fields)+'\n')
            for bank, definition in self.wavetables.items():
                f.writelines(row + '\n' for row in wtbank_rows(bank, definition))
            for name in self.patches:
                for i, (bank, position) in sorted(self.wtuses.get(name, {}).items()):
                    f.write('wtuse %s %d %s %d\n' % (name, i, bank, position))
                for i, fields in sorted(self.ksources.get(name, {}).items()):
                    f.write('ksource %s %d %d %d %d\n' % (name, i, *fields))
            for i, (pname, comment) in enumerate(self.voices):
                f.write(('voice %2d %-8s%s' % (i, pname, ('  # ' + comment) if comment else '')).rstrip() + '\n')
            for id,values in sorted(self.gen_lanes.items()):
                f.write(gen_lane_row(id,values)+'\n')
            if hash_:
                f.write('hash %s\n' % hash_)

    def files(self):
        d = self.dir
        return [os.path.join(d, 'session.tl')] + [os.path.join(d, s + '.tl') for s in self.streams]


def play(files, wav=None, merged=None):
    import contextlib
    import tempfile
    document = tl_document_module()
    try:
        headers = [document.parse_document(document.read_bounded(path))['header'] for path in files]
        if any(headers[1:]):
            raise ValueError('TL document header must occur in the first input file')
        expected_header = headers[0] if headers else b''
        with tempfile.TemporaryDirectory(prefix='compose-body-') if merged else contextlib.nullcontext() as temporary:
            cmd = [H, '--play'] + files
            if wav:
                cmd += ['--wav', wav]
            if merged:
                body_path = os.path.join(temporary, 'body.tl')
                cmd += ['--merged', merged, '--merged-body', body_path]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if merged and r.returncode in (0, 3):
                output = document.parse_document(document.read_bounded(merged))
                body = document.read_bounded(body_path)
                if output['header'] != expected_header or output['body'] != body or document.parse_document(body)['header']:
                    raise ValueError('native TL document/header/body outputs disagree')
    except ValueError as error:
        sys.exit('compose.py: %s' % error)
    line = [l for l in r.stdout.splitlines() if l.startswith('PLAY')]
    if not line:
        sys.exit('harness failed: %s %s' % (r.stdout, r.stderr))
    kv = dict(tok.split('=', 1) for tok in line[0].split()[1:])
    return r.returncode, kv, line[0]


def solo(p):
    """Render each stream alone (no hash) and print its RMS and peak, for balancing. A stereo
    stream's powers sum over its channels, each A-weighted alone, and its peak is the louder
    channel's."""
    import wave
    import numpy as np
    d = p.dir
    sess = os.path.join(OUT, p.name + '.solo-session.tl')
    with open(os.path.join(d, 'session.tl'), encoding='utf-8') as f:
        lines = [l for l in f if not l.startswith('hash ')]
    with open(sess, 'w', encoding='utf-8', newline='\n') as f:
        f.writelines(lines)
    for st in p.streams:
        wav = os.path.join(OUT, '%s.solo-%s.wav' % (p.name, st))
        rc, kv, line = play([sess, os.path.join(d, st + '.tl')], wav)
        w = wave.open(wav)
        ch = w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(float).reshape(-1, ch).T / 32768
        power = lambda y: (y * y).mean(axis=1).sum()        # summed over the channels
        act = np.any(np.abs(x) > 1e-4, axis=0)              # the frames where a channel sounds
        # A-weighted rms (IEC 61672 curve applied in the frequency domain), a rough loudness
        f = np.fft.rfftfreq(x.shape[1], 1.0 / w.getframerate())
        f2 = f * f
        ra = (12194.0 ** 2 * f2 * f2) / ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194.0 ** 2))
        xa = np.fft.irfft(np.fft.rfft(x, axis=1) * ra * 10 ** (2.0 / 20), x.shape[1], axis=1)
        n = int(w.getframerate() * 0.05)                    # 50 ms windows
        e = (xa[:, :xa.shape[1] // n * n].reshape(ch, -1, n) ** 2).mean(axis=2).sum(axis=0)
        e = e[e > 1e-9]
        print('    solo %-10s peak %6.2f dBFS  rms %6.2f  while sounding %6.2f  A-weighted %6.2f  A-weighted 50 ms p90 %6.2f dB' % (
            st, 20 * np.log10(np.abs(x).max() + 1e-12), 10 * np.log10(power(x) + 1e-20),
            10 * np.log10(power(x[:, act]) + 1e-20) if act.any() else -200,
            10 * np.log10(power(xa) + 1e-20), 10 * np.log10(np.percentile(e, 90)) if len(e) else -200))


def lint_speed(p, limit_deg_s=None):
    """docs/INSTRUMENT.md, section 7: warn when a pos or move swings a sounding voice's pan
    angle faster than limit_deg_s, else the piece's own limit p.lint_deg_s (a drawing
    piece's, ), else 3 degrees a second: a pos by its change over the time since the
    voice's last pos or move ended (a jump into a move in flight is infinitely fast), a
    move by its change over its duration; except a pos or move at the ns of that voice's
    note-on. The angle along a move is taken as linear in time, near enough for a
    lint. A piece that draws also hears its pen's fastest move, the note-ons' included."""
    import math
    if limit_deg_s is None:
        limit_deg_s = getattr(p, 'lint_deg_s', 3.0)
    warned = 0
    for s in p.streams:
        last = {}                                      # voice -> (t0, angle0, t1, angle1) of its last pos or move
        ons = {(e[0], e[3]) for e in p.ev[s] if e[1] == 1}
        sounding = {}                                  # voice -> on since
        for t, kind, seq, v, text, nm in ordered_events(p.ev[s]):
            if kind == 1:
                sounding[v] = t
            elif text == 'off':
                sounding.pop(v, None)
            elif text.startswith('pos ') or text.startswith('move '):
                w = text.split()
                x, y, z = map(int, w[1:4])
                dur = int(w[4]) if w[0] == 'move' else 0
                r = math.sqrt(x * x + y * y + z * z)
                ang = math.degrees(math.asin(x / max(r, p.near)))
                t0, a0, t1, a1 = last.get(v, (t, ang, t, ang))
                if t < t1:                                                     # a move in flight: where it is at t
                    a1, t1 = a0 + (a1 - a0) * (t - t0) / (t1 - t0), t
                if v in last and v in sounding and (t, v) not in ons:
                    dt = (dur if dur else t - t1) / 1e9
                    rate = abs(ang - a1) / dt if dt > 0 else float('inf')
                    if rate > limit_deg_s:
                        warned += 1
                        if warned <= 5:
                            print('  lint: %s voice %d moves %.1f deg/s at %d ns' % (s, v, rate, t))
                last[v] = (t, a1, t + dur, ang)
    if warned:
        print('  lint: %d pos or move events move a sounding voice faster than %g deg/s' % (warned, limit_deg_s))
    if getattr(p, 'pen_top', None):
        print('  pen: the fastest move %.1f deg/s at %d ns (%s voice %d); the limit %g deg/s' % (*p.pen_top, limit_deg_s))


def finish(p):
    """Keep ordinary output unchanged; validate compensated renders before publication."""
    global OUT
    if not getattr(p, '_unison_compensation', {}):
        _finish(p)
        return
    import shutil
    import tempfile
    destination, output = p.dir, OUT
    try:
        with tempfile.TemporaryDirectory(prefix='compose-unison-') as tmp:
            p.dir, OUT = os.path.join(tmp, 'record'), os.path.join(tmp, 'renders')
            os.makedirs(OUT)
            levels, hash_ = _finish(p)
            p._compensation_validate(levels)
            staged = OUT
            p.dir, OUT = destination, output
            p.write(levels)
            p.write_session(levels, hash_)
            os.makedirs(OUT, exist_ok=True)
            stem = p.name + ('' if p.space else '-mono')
            for suffix in ('.wav', '.tl'):
                shutil.copyfile(os.path.join(staged, stem + suffix), os.path.join(OUT, stem + suffix))
    finally:
        p.dir, OUT = destination, output


def _finish(p):
    levels = {n: c[0][1] for n, c in p.patches.items()}
    if p.levels is not None:                          # fixed levels: one pass, no clipping allowed
        levels = dict(p.levels)
        p.write(levels)
        rc, kv, line = play(p.files())
        print('  fixed levels: %s' % line)
        assert int(kv['clipped']) == 0, 'clipped at the fixed levels'
    else:
        for it in range(8):
            p.write(levels)
            rc, kv, line = play(p.files())
            peak = float(kv['peak_dbfs'])
            clipped = int(kv['clipped'])
            print('  level pass %d: %s' % (it, line))
            if clipped == 0 and abs(peak - TARGET) <= TOL:
                break
            delta = round((peak - TARGET) * 10) if clipped == 0 else 15
            levels = {n: max(0, l + delta) for n, l in levels.items()}
        else:
            sys.exit('%s: levels did not settle' % p.name)
    if p.space:
        lint_speed(p)
    p.write_session(levels, kv['hash'])
    stem = p.name + ('' if p.space else '-mono')      # a mono piece's outputs say so
    wav = os.path.join(OUT, stem + '.wav')
    merged = os.path.join(OUT, stem + '.tl')
    rc, kv, line = play(p.files(), wav, merged)
    print('  record: rc=%d %s' % (rc, line))
    rc2, kv2, line2 = play([merged])
    print('  replay: rc=%d %s' % (rc2, line2))
    assert rc == 0 and rc2 == 0 and kv['hash'] == kv2['hash']
    return levels, kv['hash']


# ---------------------------------------------------------------------------









def jitter(rng, ms):
    """A deterministic human offset in ns, uniform in +-ms."""
    return int(round((rng.next() - 0.5) * 2 * ms * 1_000_000))





def bent(m, dur, scoop=None, bend=None, vib=None, fall=None):
    """A pitch curve in fractional MIDI over a note of dur ns:
    scoop (semitones, ns): start below and rise to m;
    bend (semitones, start ns, ns): rise from m by semitones;
    vib (cents, hz, delay ns, ramp ns): a sine vibrato after delay;
    fall (semitones, ns): drop by semitones over the last ns."""
    import math

    def f(u):
        x = m
        if scoop:
            k, n = scoop
            x -= k * max(0.0, 1 - u / n)
        if bend:
            k, st, n = bend
            x += k * min(1.0, max(0.0, (u - st) / n))
        if vib:
            c, hz, dl, rp = vib
            if u > dl:
                x += c / 100 * min(1.0, (u - dl) / rp) * math.sin(2 * math.pi * hz * (u - dl) / 1e9)
        if fall:
            k, n = fall
            x -= k * min(1.0, max(0.0, (u - (dur - n)) / n)) ** 2
        return x
    return f




def tokens(line):
    """'NOTE[:sixteenths]' tokens, r a rest: [(start, sixteenths, note or None)], and the total."""
    out, pos = [], 0.0
    for tok in line.split():
        name, _, ln = tok.partition(':')
        ln = float(ln) if ln else 1.0
        out.append((pos, ln, None if name == 'r' else name))
        pos += ln
    return out, pos


# (bar, harmony, right hand, left hand), one bar of sixteenths each
INVENTION = [
    (1, 'Dm, A7, Dm', 'r D5 F5 A5 D6 C#6 D6 A5 Bb5 G5 E5 C#5 D5:2 A4:2', 'r:16'),
    (2, 'Dm, A7, Dm', 'A5:.25 G5:.25 A5:2.5 G5 F5:4 E5:4 F5:2 E5 D5',
     'r D3 F3 A3 D4 C#4 D4 A3 Bb3 G3 E3 C#3 D3:2 A2:2'),
    (3, 'Gm, C', 'D6 C6 Bb5 A5 Bb5 G5 D5 Bb4 G5 F5 E5 D5 E5 C5 G4 E4',
     'G2:2 Bb2:2 D3:2 Bb2:2 C3:2 E3:2 G3:2 E3:2'),
    (4, 'F, Bb, C7', 'C6 Bb5 A5 G5 F5 C5 A4 C5 D5 F5 Bb5 F5 E5 G5 C6 Bb5',
     'F2:2 C3:2 A2:2 F2:2 Bb2:2 D3:2 C3:2 C2:2'),
    (5, 'F, C7, F', 'r F5 A5 C6 F6 E6 F6 C6 D6 Bb5 G5 E5 F5:2 C5:2', 'C3:3 Bb2 A2:4 G2:4 A2:2 G2 F2'),
    (6, 'Dm, Bm7b5, E7', 'F5 A5 D6 A5 F5 D5 B4 D5 B5 G#5 E5 G#5 D6 B5 G#5 F5',
     'D3:2 F2:2 B2:2 D3:2 E3:2 G#2:2 E2:2 G#2:2'),
    (7, 'Am, E7, Am', 'E5:.25 D5:.25 E5:2.5 D5 C5:4 B4:4 C5:2 B4 A4',
     'r A2 C3 E3 A3 G#3 A3 E3 F3 D3 B2 G#2 A2:2 E2:2'),
    (8, 'Dm, G7', 'F5:2 E5:2 F5:2 D5:2 B4:2 D5:2 F5:2 D5:2', 'D3 F3 A3 F3 D4 C4 D4 A3 G3 B3 D4 B3 G3 F3 G3 D3'),
    (9, 'C, F7', 'E5:2 D5:2 C5:2 E5:2 A4:2 C5:2 Eb5:2 C5:2', 'C3 E3 G3 E3 C4 B3 C4 G3 F3 A3 C4 A3 F3 Eb3 F3 C3'),
    (10, 'Bb, Edim7, A7', 'D5:2 C5:2 D5:2 F5:2 G5:2 E5:2 C#5:2 E5:2',
     'Bb2 D3 F3 D3 Bb3 A3 Bb3 F3 E3 G3 Bb3 G3 A2 C#3 E3 A3'),
    (11, 'Dm, A7, Dm: the return', 'r D5 F5 A5 D6 C#6 D6 A5 Bb5 G5 E5 C#5 D5:2 A5:2', 'A3:3 G3 F3:4 E3:4 F3:2 E3 D3'),
    (12, 'Gm, Em7b5, A7: the trill', 'Bb5 A5 G5 F5 E5 G5 Bb5 D6 C#6 Bb5 G5 E5 '
     'F5:.5 E5:.5 F5:.5 E5:.5 F5:.5 E5:.5 D5:.5 E5:.5', 'G2:2 D3:2 Bb2:2 G2:2 A2:4 A1:4'),
]


















# The fanfare's brass, part by part: {bar: (velocity at the downbeat, velocity at
# the bar's end, 'NOTE:ticks' tokens)}, a tick a twelfth of a beat (50 ms), 48 to
# the bar. Bar 12 repeats bar 6; bar 14, the held chord, is in fanfare. The
# trombones play the chorale (bars 10 and 11) mp, not p: at p their index would
# sit near 2 rad, where 1:1 FM loses its fundamental.
FANFARE = {
    'T1': {2: (F, F, 'Bb4:12 F4:4 Bb4:4 D5:4 F5:24'), 3: (F, F, 'G5:18 F5:6 Eb5:12 D5:12'),
           4: (F, F, 'C5:12 C5:4 D5:4 Eb5:4 F5:12 A4:12'), 5: (F, F, 'Bb4:24 r:24'),
           6: (FF, FF, 'Bb4:12 F4:4 Bb4:4 D5:4 F5:24'), 7: (FF, FF, 'G5:18 F5:6 Eb5:12 D5:12'),
           8: (FF, FF, 'Eb5:12 Eb5:4 F5:4 G5:4 A5:12 Bb5:12'), 9: (FF, FF, 'A5:24 r:24'),
           13: (FF, FF, 'G5:12 F5:4 G5:4 A5:4 Bb5:12 A5:12')},
    'T2': {6: (F, F, 'F4:12 D4:4 F4:4 Bb4:4 D5:24'), 7: (F, F, 'Eb5:18 D5:6 C5:12 Bb4:12'),
           8: (F, F, 'C5:12 C5:4 D5:4 Eb5:4 F5:12 F5:12'), 9: (F, F, 'F5:24 r:24'),
           13: (F, F, 'Eb5:12 D5:4 Eb5:4 F5:4 F5:12 F5:12')},
    'H1': {3: (MP, MP, 'Bb3:48'), 4: (MP, MP, 'Bb3:24 A3:24'), 5: (MP, F, 'Bb3:24 C4:4 C4:4 C4:4 C4:4 C4:4 C4:4'),
           6: (F, F, 'D4:24 F4:24'), 7: (F, F, 'G4:24 F4:24'), 8: (F, F, 'G4:24 F4:24'), 9: (F, F, 'F4:24 r:24'),
           10: (P, P, 'Bb4:24 G4:24'), 11: (P, MF, 'G4:24 A4:24'), 13: (F, F, 'G4:24 A4:24')},
    'H2': {3: (MP, MP, 'G3:24 F3:24'), 4: (MP, MP, 'G3:24 F3:24'), 5: (MP, F, 'F3:24 A3:4 A3:4 A3:4 A3:4 A3:4 A3:4'),
           6: (F, F, 'Bb3:24 D4:24'), 7: (F, F, 'Eb4:24 D4:24'), 8: (F, F, 'Eb4:24 C4:24'), 9: (F, F, 'C4:24 r:24'),
           10: (P, P, 'D4:24 Eb4:24'), 11: (P, MF, 'Eb4:48'), 13: (F, F, 'Eb4:48')},
    'B1': {2: (F, F, 'F3:12 r:36'), 3: (MP, MP, 'Eb3:24 D3:24'), 4: (MP, MP, 'Eb3:48'),
           5: (MP, F, 'D3:24 Eb3:4 Eb3:4 Eb3:4 Eb3:4 Eb3:4 Eb3:4'), 6: (FF, FF, 'D3:12 D3:12 F3:24'),
           7: (FF, FF, 'Eb3:24 F3:24'), 8: (FF, FF, 'Eb3:24 C3:12 Eb3:12'), 9: (FF, FF, 'C3:24 r:24'),
           10: (MP, MP, 'G3:24 Bb3:24'), 11: (MP, MF, 'Bb3:24 C4:24'), 13: (FF, FF, 'Eb3:24 Eb3:24')},
    'B2': {2: (F, F, 'Bb2:12 r:36'), 3: (MP, MP, 'Bb2:48'), 4: (MP, MP, 'G2:24 A2:24'),
           5: (MP, F, 'Bb2:24 A2:4 A2:4 A2:4 A2:4 A2:4 A2:4'), 6: (FF, FF, 'Bb2:12 Bb2:12 D3:24'),
           7: (FF, FF, 'G2:24 Bb2:24'), 8: (FF, FF, 'G2:24 A2:24'), 9: (FF, FF, 'A2:24 r:24'),
           10: (MP, MP, 'G2:24 Eb3:24'), 11: (MP, MF, 'C3:24 F2:24'), 13: (FF, FF, 'G2:24 A2:24')},
    'TU': {2: (F, F, 'Bb1:12 r:36'), 3: (MP, MP, 'Eb2:24 D2:24'), 4: (MP, MP, 'C2:24 F2:24'),
           5: (MP, F, 'Bb1:24 F1:24'), 6: (FF, FF, 'Bb1:12 Bb1:12 Bb1:24'), 7: (FF, FF, 'Eb2:24 D2:24'),
           8: (FF, FF, 'C2:24 F2:24'), 9: (FF, FF, 'F2:24 r:24'), 10: (P, P, 'G1:24 Eb2:24'),
           11: (P, MF, 'C2:24 F1:24'), 13: (FF, FF, 'Eb2:24 F2:24')},
}




# The aria's melodies, bar by bar: 'NOTE:eighths' tokens, r a rest, eight
# eighths to the bar. Bar 12, the held chord, is in aria.
ARIA = {
    'voice': {2: 'r:6 B4:2', 3: 'E5:4 D5:2 C5 B4', 4: 'C5:2 A4:2 C5:2 B4:2', 5: 'B4:3 C5 D5:2 A4:2',
              6: 'G4:3 A4 B4:4', 7: 'E5:2 G5:2 D5:2 B4:2', 8: 'C5:2 E5:2 D#5:4', 9: 'E5:3 D5 E5:2 F#5:2',
              10: 'G5:4 E5:2 C5:2', 11: 'E5:4 D#5:4'},
    'oboe': {1: 'E5:4 D5:2 C5 B4', 2: 'C5:2 A4:2 B4:2 r:2', 3: 'r:2 G4 A4 B4:2 r:2', 4: 'r:4 F#4:4', 5: 'r:8',
             6: 'r:4 D5 E5 F#5 D5', 7: 'r:8', 8: 'r:4 B4:2 A4:2', 9: 'G4:3 F#4 G4:2 A4:2', 10: 'E5:4 C5:2 A4:2',
             11: 'B4:4 A4:4'},
}
# By half bar, bars 1 to 11: (harmony, bass, the strings' upper notes, the
# clarinet's four eighths from bar 5).
ARIA_HARMONY = [
    ('Em', 'E3', 'G3 B3 E4', ''), ('Bm/D', 'D3', 'F#3 B3 D4', ''), ('Am/C', 'C3', 'A3 C4 E4', ''),
    ('B', 'B2', 'F#3 B3 D#4', ''), ('Em', 'E3', 'G3 B3 E4', ''), ('Bm/D', 'D3', 'F#3 B3 D4', ''),
    ('Am/C', 'C3', 'A3 C4 E4', ''), ('B', 'B2', 'F#3 B3 D#4', ''),
    ('G', 'G2', 'G3 B3 D4', 'G3 D4 B3 D4'), ('D/F#', 'F#2', 'F#3 A3 D4', 'F#3 D4 A3 D4'),
    ('Em', 'E2', 'G3 B3 E4', 'G3 E4 B3 E4'), ('Bm/D', 'D2', 'F#3 B3 D4', 'F#3 D4 B3 D4'),
    ('C', 'C3', 'G3 C4 E4', 'G3 E4 C4 E4'), ('G/B', 'B2', 'G3 B3 D4', 'G3 D4 B3 D4'),
    ('Am', 'A2', 'A3 C4 E4', 'A3 E4 C4 E4'), ('B7', 'B2', 'F#3 A3 D#4', 'F#3 D#4 A3 D#4'),
    ('Em', 'E3', 'G3 B3 E4', 'G3 E4 B3 E4'), ('Em/D', 'D3', 'G3 B3 E4', 'G3 E4 B3 D4'),
    ('C', 'C3', 'G3 C4 E4', 'G3 E4 C4 E4'), ('Am', 'A2', 'A3 C4 E4', 'A3 E4 C4 E4'),
    ('B7sus4', 'B2', 'F#3 A3 E4', 'F#3 E4 A3 E4'), ('B7', 'B2', 'F#3 A3 D#4', 'F#3 D#4 A3 D#4'),
]




# The fiesta's parts, bar by bar: 'NOTE:eighths' tokens, r a rest, eight
# eighths to the bar; the log drum's are sixteenths, sixteen to the bar. A
# brass token is MELODY/HARMONY, and the trombone doubles the melody an
# octave down. Bars 9 to 12 repeat bars 5 to 8 where a part has no line.
FIESTA = {
    'marimba': {5: 'E5 r C5 E5 r C5 A4 F5', 6: 'r D5 F5 r A5 F5 r G#5', 7: 'E5 r D5 E5 r B4 G#4 C5',
                8: 'r A4 C5 r E5 C5 r E5', 15: 'F5 r D5 E5 r D5 B4 A4', 16: 'r A4 C5 r B4 D5 r G#4'},
    'brass': {9: 'r E5/C5 r E5/C5 r C5/A4 E5/C5:2', 10: 'r F5/D5 r F5/D5 r D5/A4 A5/F5:2',
              11: 'r G#5/E5 r G#5/E5 r E5/B4 D5/B4:2', 12: 'r E5/C5 r D5/B4 r C5/A4 A4/E4 r',
              15: 'r F5/D5 r F5/D5 r G#5/E5 B5/G#5:2', 16: 'r A5/F5 r A5/F5 r B5/G#5 G#5/E5:2'},
    'log': {9: 'A4:3 A4:3 C5:2 r:2 D5:2 C5:2 A4:2', 10: 'r:2 E4:2 G4:2 A4:2 r:2 G4:2 E4:2 D4:2',
            13: 'A4 C5 D5 E5 D5:2 C5:2 A4:3 G4:3 E4:2', 14: 'A4:2 A4 C5 A4:2 E4 A3 r:8'},
}
# The bass tumbao: (the note on the and of 2, the note on 4, tied into the
# next bar); a trailing '.' makes a note short.
FIESTA_BASS = {4: (None, 'A1'), 5: ('E2', 'D2'), 6: ('A1', 'E2'), 7: ('B1', 'A1'), 8: ('E2', 'A1'),
               12: ('E2', 'A1.'), 14: (None, 'D2'), 15: ('E2', 'F2'), 16: ('E2', 'E2.')}




def smooth(k):
    """0 to 1 over k in 0..1, easing in and out (smoothstep)."""
    k = min(1.0, max(0.0, k))
    return k * k * (3 - 2 * k)


def pcs(names):
    """Pitch classes, 0 to 11, of note names without octaves ('F# A# C#')."""
    return {(NOTE[n[0]] + {'#': 1, 'b': -1}.get(n[1:], 0)) % 12 for n in names.split()}








# ---------------------------------------------------------------------------
# Profiles : the frame a placement is drawn in, named by its shape and half
# width. A place runs from -1 (the left edge) through 0 (ahead) to +1 (the right
# edge); linH maps it to H degrees a side linearly, tanhH spreads the middle and
# eases the edges (H * tanh(1.5 x) / tanh(1.5)); H is 5, 10, ... 90. The keys
# versions of every piece go through KEYS_PROFILE; --profile= renders them
# through another (docs/INSTRUMENT.md, section 7).
PROFILES = ['%s%d' % (shape, h) for shape in ('lin', 'tanh') for h in range(5, 95, 5)]
KEYS_PROFILE = 'lin30'                  # the keys default
PROFILE = KEYS_PROFILE                  # what keys_version renders through; --profile= sets another
TANH_K = 1.5                            # the tanh profiles' knee, the research summary's k


def profile_angle(x, profile=None):
    """The azimuth in degrees of the place x (-1 the left edge, 0 ahead, +1 the right
    edge; clamped) in a profile, PROFILE when none is named."""
    import math
    profile = profile or PROFILE
    assert profile in PROFILES, profile
    shape, h = re.match(r'(lin|tanh)(\d+)$', profile).groups()
    x = max(-1.0, min(1.0, x))
    if shape == 'lin':
        return int(h) * x
    return int(h) * math.tanh(TANH_K * x) / math.tanh(TANH_K)


def profile_pos(x, profile=None, mirror=False, r=1000):
    """The position in mm of the place x in a profile (profile_angle), r mm away on
    the front arc, or of its reflection with mirror: for placing a voice by hand in
    a profile's frame."""
    import math
    th = math.radians(-profile_angle(x, profile) if mirror else profile_angle(x, profile))
    return int(round(r * math.sin(th))), int(round(r * math.cos(th))), 0


def keys_angle(m, profile=None):
    """The keys mapping's angle in degrees for MIDI note m (fractional allowed): the
    place (m - 60) / 32 in the profile, so E1 (28) and below sit at the left edge,
    middle C ahead, G#6 (92) and above at the right edge: 30/32 of a degree a
    semitone in the default lin30."""
    return profile_angle((m - 60) / 32.0, profile)


def keys_pos(m, r=1000, mirror=False, profile=None):
    """docs/INSTRUMENT.md, section 7, the keys mapping: MIDI note m on the keys line
    (keys_angle), high notes right, as the player faces a piano. With mirror the
    line is reflected: high notes left, as the audience faces the piano
    (keys-mirror)."""
    return profile_pos((m - 60) / 32.0, profile, mirror, r)


@functools.lru_cache(maxsize=8192)
def midi_of(f_mhz):
    """The fractional MIDI number of a frequency in mHz (A4 = 440000 is 69), a float
    from decimal logarithms, independently of the binary libm ; cached as mhz_m is."""
    with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        return float(69 + 12 * (decimal_value(f_mhz) / 440000).ln() / decimal.Decimal(2).ln())


def mono_levels(name):
    """The carrier levels, dB/10 per patch, in the committed session.tl of piece name:
    what its leveller settled on, or its fixed levels. A mono piece's rotation version
    keeps them ; every keys version is levelled from them (,  B)."""
    levels = {}
    with open(os.path.join(MUSIC, name, 'session.tl'), encoding='utf-8') as f:
        for line in f:
            w = line.split()
            if w[:1] == ['patch'] and w[2] == '0':
                levels[w[1]] = int(w[4])
    return levels


def generated_events(piece):
    """Stream the native adapter's manifest; no Python generator or pitch recurrence."""
    import copy
    import tempfile
    build()
    os.makedirs(os.path.join(SPIKE,'out'),exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gen-events-',dir=os.path.join(SPIKE,'out')) as tmp:
        probe = copy.copy(piece)
        probe.dir = tmp
        probe.write()
        with tempfile.TemporaryFile(mode='w+t') as errors:
            proc = subprocess.Popen([H,'--gen-events',*probe.files()],stdout=subprocess.PIPE,
                                    stderr=errors,text=True)
            count,started,ended,previous = 0,False,False,None
            try:
                for line in proc.stdout:
                    line = line.rstrip('\n')
                    if not started:
                        if line != 'GEN_EVENTS version=1 fs=%d ctl=64' % piece.fs:
                            raise ValueError('invalid native generator manifest header')
                        started = True
                        continue
                    finish = re.fullmatch(r'GEN_EVENTS count=([0-9]+)',line)
                    if finish and not ended:
                        if int(finish[1]) != count:
                            raise ValueError('native generator manifest count differs')
                        ended = True
                        continue
                    event = re.fullmatch(r'GEN_EVENT frame=([0-9]+) time_ns=([0-9]+) lane=([0-9]+) voice=([0-9]+) '
                                         r'kind=(on|off) freq_millihz=([0-9]+) velocity=([0-9]+)',line)
                    if ended or not event:
                        raise ValueError('invalid native generator manifest event')
                    frame,t,lane,voice = map(int,event.groups()[:4])
                    kind = event[5]
                    freq,velocity = int(event[6]),int(event[7])
                    gen_int(frame,0,18446744073709551615,'manifest frame')
                    gen_int(t,0,18446744073709551615,'manifest time')
                    if (frame % 64 or t != (frame*1000000000+piece.fs-1)//piece.fs or
                            t*piece.fs//1000000000 != frame or t >= piece.length_ns):
                        raise ValueError('native generator manifest time does not match its frame')
                    if lane not in piece.gen_lanes or voice != piece.gen_lanes[lane][0]:
                        raise ValueError('native generator manifest lane/voice differs')
                    key = frame,0 if kind == 'off' else 1,lane
                    if previous is not None and key <= previous:
                        raise ValueError('native generator manifest order differs')
                    previous = key
                    if kind == 'off':
                        if freq or velocity:
                            raise ValueError('native generator manifest off fields must be zero')
                    else:
                        gen_int(freq,1,4294967295,'manifest frequency')
                        gen_int(velocity,1,32767,'manifest velocity')
                    count += 1
                    yield frame,t,lane,voice,kind,freq,velocity
                rc = proc.wait()
                errors.seek(0)
                if rc:
                    raise ValueError('native generator manifest failed: '+errors.read(4096).strip())
                if not started or not ended:
                    raise ValueError('native generator manifest is incomplete')
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdout.close()


def keys_version(make, mirror=False):
    """The stereo version of the piece that make builds, by the keys mapping (,
    , ): the same streams, patches, voices and note events, plus a pos at
    every note-on that places the note by its pitch on the keys line of the profile
    PROFILE, or on its mirror image. A pitch event moves a note's pitch but not its
    place. Every version is levelled from its piece's committed levels: the balance
    stays, and the peak goes back to TARGET ( B, 2026-10-11, superseding 's
    rule that a mono piece's version kept the mono levels, under which the keys line,
    which puts each note 1 m away at its pitch's place, left the versions under the
    mono peak by the amounts the keys tables of docs/INSTRUMENT.md record). A piece
    written for the module alone also loses its own pos and move events .
    Returns the builder of PIECE-keys or PIECE-keys-mirror; through a profile other
    than the default, of PIECE-keys-PROFILE or PIECE-keys-mirror-PROFILE, whose
    records go under out/music/profiles, unpinned, for listening."""
    def build():
        m = make()
        spatial = m.space is not None                   # written for the module alone
        profile = PROFILE
        name = m.name + ('-keys-mirror' if mirror else '-keys') + ('' if profile == KEYS_PROFILE else '-' + profile)
        h = re.match(r'(lin|tanh)(\d+)$', profile).group(2)
        spread = ', the middle spread by tanh' if profile.startswith('tanh') else ''
        about = ['%s: %s\'s notes through the stereo layout of docs/INSTRUMENT.md,' % (name, m.name)]
        own = ['in place of %s\'s own pos and move events, at its committed balance, levelled' % m.name,
               '(): the places, motion and levels told below are %s\'s.' % m.name]
        if mirror:
            about += ['each note placed by its pitch on the keys line, mirrored (keys-mirror in profile',
                      '%s%s: E1 %s degrees right, middle C ahead, G#6 %s degrees left),' % (profile, spread, h, h)]
            about += own if spatial else ['levelled from %s\'s committed levels ().' % m.name]
        elif spatial:
            about += ['each note placed by its pitch on the keys line (the keys mapping in profile %s%s:' % (profile, spread),
                      'E1 %s degrees left, middle C ahead, G#6 %s degrees right),' % (h, h)] + own
        else:
            about += ['each note placed by its pitch on the keys line (the keys mapping in profile %s%s:' % (profile, spread),
                      'E1 %s degrees left, middle C ahead, G#6 %s degrees right), levelled from %s\'s committed levels ().' % (h, h, m.name)]
        told = m.about if spatial else m.about[1:]     # a spatial piece's first line runs on into its second
        p = Piece(name, m.bpm, m.beats, about + told, fs=m.fs, space='stereo', block=m.block, near=m.near)
        if profile != KEYS_PROFILE:
            p.dir = os.path.join(OUT, 'profiles', name)
        p.streams, p.roles, p.patches, p.voices = list(m.streams), dict(m.roles), dict(m.patches), list(m.voices)
        for key in ('_meter_rows', '_tempo_segments', '_tempo_cap', '_tempo_rows', '_tempo_ramp',
                    '_tempo_bars', '_tempo_comment'):
            if hasattr(m, key):
                setattr(p, key, getattr(m, key))       # immutable exact map/metadata
        committed = mono_levels(m.name)                 # the leveller starts from the committed balance ( B)
        p.patches = {n: [[car[0], committed[n]] + list(car[2:]), mod, comment]
                     for n, (car, mod, comment) in m.patches.items()}
        p.routes = dict(m.routes)                       # a routed recipe's operators and route
        p.gen_ctl = m.gen_ctl
        p.generators, p.gen_axioms, p.gen_lanes = dict(m.generators), dict(m.gen_axioms), dict(m.gen_lanes)
        p.gen_rules = {id:dict(rules) for id,rules in m.gen_rules.items()}
        p.mods = {name: dict(settings) for name, settings in m.mods.items()}
        for key in ('_unison_compensation', '_unison_notes'):
            if hasattr(m, key):
                setattr(p, key, copy.deepcopy(getattr(m, key)))
        p.macros = {name: {n: dict(v) for n, v in definitions.items()} for name, definitions in m.macros.items()}
        p.muted = dict(m.muted)
        p.snapshots = {name: {v: dict(inputs) for v, inputs in values.items()}
                       for name, values in m.snapshots.items()}
        p.shapes = dict(m.shapes)                     # shaping follows the patch
        p.amps = dict(m.amps)
        p.kstypes = {name: dict(settings) for name, settings in m.kstypes.items()}
        p.fbtypes = {name: dict(settings) for name, settings in m.fbtypes.items()}
        p.egloops = {name: dict(settings) for name, settings in m.egloops.items()}
        p.lfos = dict(m.lfos)                           # the LFO is part of the patch
        p.pegs = dict(m.pegs)                           # the PEG is part of the patch
        p.egs = {name: dict(settings) for name, settings in m.egs.items()}
        p.left_slopes = {name: dict(settings) for name, settings in m.left_slopes.items()}
        p.formants = {name: dict(settings) for name, settings in m.formants.items()}
        p.vectors = dict(m.vectors)
        p.pds = {name:dict(settings) for name,settings in m.pds.items()}
        p.wavetables = dict(m.wavetables)
        p.wtuses = {name: dict(settings) for name, settings in m.wtuses.items()}
        p.ksources = {name: dict(settings) for name, settings in m.ksources.items()}
        p.noise_shifts = {name: dict(settings) for name, settings in m.noise_shifts.items()}
        p.fenvs = dict(m.fenvs)                         # filter envelopes keep their required filters
        p.recipe_filters = set(m.recipe_filters)
        p.filters = dict(m.filters)                   # every patch filter follows its keys version
        p.fxh = dict(m.fxh)                            # effect/send events need the same effects headers
        p.marks, p.cum, p.length_ns, p.seq = {s: dict(m.marks[s]) for s in m.streams}, m.cum, m.length_ns, m.seq
        if hasattr(m, '_tempo_segments'):
            p.cum = list(m.cum)
        for s in m.streams:
            p.ev[s] = [e for e in m.ev[s] if not e[4].startswith(('pos ', 'move '))]
            for t, kind, seq, v, text, nm in m.ev[s]:
                if kind == 1:                                   # a note-on: place the note
                    n = midi_of(int(text.split()[1]))
                    x, y, z = keys_pos(n, mirror=mirror, profile=profile)
                    a = keys_angle(n, profile)
                    p.ev[s].append((t, 0, seq, v, 'pos %d %d %d' % (x, y, z),
                                    '%s at %+.1f degrees' % (label(n), -a if mirror else a)))
        if m.gen_lanes:
            if not p.streams:
                raise ValueError('generated keys positions need a declared stream')
            for _,t,_,v,kind,freq,_ in generated_events(m):
                if kind == 'on':
                    n = midi_of(freq)
                    x,y,z = keys_pos(n,mirror=mirror,profile=profile)
                    a = keys_angle(n,profile)
                    p.pos(p.streams[0],t,v,x,y,z,text='%s at %+.1f degrees (generated)' %
                          (label(n),-a if mirror else a))
        return p
    return build


# ---------------------------------------------------------------------------
# Profile renders of the pieces written for the module ( (b)): --profile=
# redraws such a piece from the profile its own placements were drawn in, its
# home, into another, as PIECE-PROFILE under out/music/profiles, unpinned,
# for listening. Placements written in plain mm are drawn in lin90, where the
# place is the pan angle over 90 degrees (the compass, crossing's walk);
# dubplate and two-clocks drew theirs in lin30, all within 30 degrees of ahead;
# longhand its pen in lin45, the pen's default .
HOME = {'wind-rose-stereo-rotation': 'lin90', 'dubplate': 'lin30', 'two-clocks': 'lin30', 'crossing': 'lin90',
        'longhand': 'lin45'}
SEG_DEG = 2.0                           # a redrawn move is cut into one move per this much of its turn, rounded up


def profile_place(a, profile):
    """The place, -1 to +1, at the azimuth a degrees of a profile: profile_angle
    inverted, clamped at the edges."""
    import math
    shape, h = re.match(r'(lin|tanh)(\d+)$', profile).groups()
    u = max(-1.0, min(1.0, a / int(h)))
    return u if shape == 'lin' else math.atanh(u * math.tanh(TANH_K)) / TANH_K


def redraw(x, y, z, home, profile):
    """The position (x, y, z) in mm, drawn in the profile home, drawn in profile
    instead: the same distance, height and side (ahead or behind), its pan angle
    asin(x / r) (docs/INSTRUMENT.md, section 2) taken to its place in home and from
    there to the angle of that place in profile. Returns whole mm."""
    import math
    r = math.sqrt(x * x + y * y + z * z)
    if r == 0:
        return 0, 0, 0
    a = math.degrees(math.asin(max(-1.0, min(1.0, x / r))))
    assert abs(a) <= int(re.match(r'(lin|tanh)(\d+)$', home).group(2)) + 0.05, \
        '(%d, %d, %d) lies %.1f degrees off ahead, outside its home profile %s' % (x, y, z, a, home)
    nx = r * math.sin(math.radians(profile_angle(profile_place(a, home), profile)))
    ny = math.copysign(math.sqrt(max(0.0, r * r - nx * nx - z * z)), y if y else 1.0)
    return int(round(nx)), int(round(ny)), int(round(z))


def profile_version(make, home):
    """The builder of PIECE-PROFILE: the piece written for the module that make
    builds, at its own level rule, with every pos and move redrawn from its home
    profile into PROFILE (redraw). A move becomes a chain of moves, as Piece.path
    writes one, between equally spaced points of the original line, one for each
    SEG_DEG of its turn of azimuth, rounded up, so the voice follows the redrawn line
    rather than a chord across it. In its home
    profile the piece is unchanged. A pos or move into a move in flight is refused."""
    import math

    def build():
        p = make()
        if PROFILE != home:                                 # a drawing's pen speeds hold in its home only; the lint measures the redrawn moves
            p.pen_top = None
        name = '%s-%s' % (p.name, PROFILE)
        p.about = ['%s: %s with its placements, drawn in profile %s, redrawn in %s ( (b)):' % (name, p.name, home, PROFILE),
                   'the same distances, heights and sides, each pan angle moved to the same place in %s.' % PROFILE] + p.about
        p.name, p.dir = name, os.path.join(OUT, 'profiles', name)
        for s in p.streams:
            ev, at = [], {}                                 # at: voice -> (end ns, end position) of its last pos or move
            for t, kind, seq, v, text, nm in ordered_events(p.ev[s]):
                w = text.split()
                if w[0] not in ('pos', 'move'):
                    ev.append((t, kind, seq, v, text, nm))
                    continue
                q = tuple(map(int, w[1:4]))
                t1, b = at.get(v, (0, (0, 1000, 0)))       # a voice with no pos or move sits 1 m ahead
                assert t >= t1, '%s, voice %d: a pos or move at %d ns into a move in flight' % (p.name, v, t)
                if w[0] == 'pos':
                    x, y, z = redraw(*q, home, PROFILE)
                    a = math.degrees(math.asin(x / max(1.0, math.sqrt(x * x + y * y + z * z))))
                    ev.append((t, kind, seq, v, 'pos %d %d %d' % (x, y, z),
                               '%+.1f degrees%s' % (a, '; in %s, %s' % (home, nm) if nm else '')))
                    at[v] = (t, q)
                    continue
                dur = int(w[4])
                turn = abs((math.degrees(math.atan2(q[0], q[1]) - math.atan2(b[0], b[1])) + 180) % 360 - 180)
                k = 1 if PROFILE == home else max(1, min(dur, math.ceil(turn / SEG_DEG)))
                for i in range(1, k + 1):
                    ta, tb = t + (dur * (i - 1) + k // 2) // k, t + (dur * i + k // 2) // k
                    if i > 1 and ta >= p.length_ns:             # the module cuts a move at the end
                        break
                    x, y, z = redraw(*[b[j] + (q[j] - b[j]) * i / k for j in range(3)], home, PROFILE)
                    if i > 1:
                        p.seq += 1
                    ev.append((ta, kind, seq if i == 1 else p.seq, v, 'move %d %d %d %d' % (x, y, z, tb - ta),
                               nm if i == 1 else ''))
                at[v] = (t + dur, q)
            p.ev[s] = ev
        return p
    return build


# ---------------------------------------------------------------------------
# Drawing : a voice draws on a page whose width is pan and whose
# height is pitch, time being the pen. A page point (u, v) runs from -1 to +1,
# left to right and bottom to top: u is a place in the pen's profile on its arc
# (profile_pos), v a pitch from low to high. A primitive is a line, an arc, a
# quadratic or cubic Bezier curve or a dot; a stroke is primitives joined end to
# end, drawn as one note (Piece.stroke); a figure is strokes with gaps between
# (Piece.draw); GLYPHS draws the letters and digits (Piece.glyph). Every
# convention is a setting of the pen, 's recommendation the default.
PEN = {'profile': 'lin45',            # the frame's width: u = -1 and +1 are 45 degrees left and right
       'r': 1000,                     # on the 1 m arc, z = 0: height is pitch, never z
       'low': 60, 'high': 84,         # v = -1 is C4, v = +1 C6, continuous: 25 chromatic rows; low above high turns the page over
       'rows': None,                  # or semitones of the octave above low, e.g. (0, 2, 4, 7, 9): the pitch snaps to them
       'mirror': False,               # the page's left is the listener's left; True is the mirror, a training variant only
       'glyph_ns': 2_000_000_000,     # a letter in 2 s, its gaps included
       'gap_ns': 125_000_000,         # pen up between two strokes
       'dot_ns': 125_000_000,         # a dot's note
       'vel': MP,                     # thickness; the colour is the voice's patch
       'strike': False,               # one note a stroke; True strikes it again at each join of its primitives
       'step_ns': 10_000_000,         # the pen's sampling: a pitch event at a step where it has moved by cents
       'cents': 5.0,
       'seg_deg': 2.0}                # a move turns this far at most, the sampling allowing
PEN_SOUND = ('warm-pad', {'c_a': -660, 'm_a': -860, 'c_r': -1520, 'm_r': -1720})   # 40 ms attack, 80 ms release


class Pen:
    """The drawing's conventions: PEN's settings, any changed by keyword, so
    Pen(glyph_ns=4_000_000_000) draws at half speed and Pen(mirror=True) mirrored."""

    def __init__(self, **kw):
        bad = [k for k in kw if k not in PEN]
        if bad:
            raise TypeError('no pen setting %s' % ', '.join(bad))
        self.__dict__.update(PEN, **kw)
        assert self.profile in PROFILES, self.profile

    def pitch(self, v):
        """The fractional MIDI note at height v: linear in semitones, or snapped to rows."""
        m = self.low + (v + 1) / 2 * (self.high - self.low)
        if self.rows:
            m = min((n for n in range(int(m) - 12, int(m) + 14) if (n - self.low) % 12 in self.rows),
                    key=lambda n: (abs(n - m), n))
        return m

    def angle(self, u):
        """The pan angle in degrees at width u, right positive."""
        return -profile_angle(u, self.profile) if self.mirror else profile_angle(u, self.profile)

    def place(self, u):
        """The position in mm at width u, on the pen's arc."""
        return profile_pos(u, self.profile, self.mirror, self.r)


def pen_sound(**tweak):
    """The default pen's patch, for p.patch(name, *pen_sound): warm-pad with a
    40 ms attack and an 80 ms release, which ends within 100 ms, before the gap.
    tweak adds recipe deltas to PEN_SOUND's: pen_sound(c_level=-2) is 2 dB louder."""
    deltas = dict(PEN_SOUND[1])
    for k, d in tweak.items():
        deltas[k] = deltas.get(k, 0) + d
    return recipe(PEN_SOUND[0], **deltas)


def line(a, b):
    """A straight line on the page from a to b, each (u, v)."""
    return ('line', tuple(a), tuple(b))


def arc(c, r, a0, a1):
    """An arc of the circle about c of radius r, or of the ellipse of radii (rx, ry),
    from a0 to a1 degrees (0 right, 90 up): counter-clockwise when a1 > a0, clockwise
    when a1 < a0; a whole turn is a circle."""
    return ('arc', tuple(c), tuple(r) if isinstance(r, tuple) else (r, r), a0, a1)


def curve(*pts):
    """A Bezier curve from its first point to its last: three points make it
    quadratic, four cubic."""
    assert len(pts) in (3, 4), 'a curve has three or four points'
    return ('curve',) + tuple(tuple(q) for q in pts)


def dot(q):
    """A dot at q: a note in one place, a stroke of its own."""
    return ('dot', tuple(q))


def poly(*pts):
    """The lines of a polyline through pts: a stroke with corners."""
    return [line(a, b) for a, b in zip(pts, pts[1:])]


def prim_at(q, x):
    """The page point at x, 0 to 1, along primitive q."""
    import math
    if q[0] == 'line':
        return tuple(a + (b - a) * x for a, b in zip(q[1], q[2]))
    if q[0] == 'arc':
        (cu, cv), (rx, ry), a0, a1 = q[1:]
        a = math.radians(a0 + (a1 - a0) * x)
        return cu + rx * math.cos(a), cv + ry * math.sin(a)
    if q[0] == 'curve':
        n = len(q) - 2
        w = [math.comb(n, k) * (1 - x) ** (n - k) * x ** k for k in range(n + 1)]
        return sum(c * p[0] for c, p in zip(w, q[1:])), sum(c * p[1] for c, p in zip(w, q[1:]))
    return q[1]


def trace(prims, n=128):
    """A stroke as a polyline: its page points (a line's two, n more along any other
    primitive), the page distance along it to each, and the distance at each join of
    two primitives. The primitives must join end to end and stay on the page."""
    import math
    pts, dist, joins = [prim_at(prims[0], 0.0)], [0.0], []
    for k, q in enumerate(prims):
        assert q[0] != 'dot', 'a dot is a stroke of its own'
        assert math.dist(prim_at(q, 0.0), pts[-1]) < 1e-9, \
            'primitive %d starts at %s, not where the one before ended, %s' % (k, prim_at(q, 0.0), pts[-1])
        if k:
            joins.append(dist[-1])
        steps = 1 if q[0] == 'line' else n
        for i in range(1, steps + 1):
            x = prim_at(q, i / steps)
            assert all(abs(c) <= 1 + 1e-9 for c in x), 'the stroke leaves the page at %s' % (x,)
            dist.append(dist[-1] + math.dist(x, pts[-1]))
            pts.append(x)
    return pts, dist, joins


def fit(q, box):
    """Primitive q drawn in the cell -1 to +1 each way, moved into the page box
    (u0, v0, u1, v1)."""
    u0, v0, u1, v1 = box
    sx, sy = (u1 - u0) / 2, (v1 - v0) / 2

    def f(p):
        return u0 + (p[0] + 1) * sx, v0 + (p[1] + 1) * sy
    if q[0] == 'arc':
        return ('arc', f(q[1]), (q[2][0] * sx, q[2][1] * sy), q[3], q[4])
    return (q[0],) + tuple(f(p) for p in q[1:])


# The letters, digits and full stop: each a list of strokes in the order a hand
# prints them, each stroke in the direction it is written (stems from the top,
# round letters from the top right counter-clockwise), in the cell -1 to +1.
GLYPHS = {
    'A': [poly((-1, -1), (0, 1), (1, -1)), [line((-0.5, 0), (0.5, 0))]],
    'B': [[line((-1, 1), (-1, -1))],
          [line((-1, 1), (0.3, 1)), arc((0.3, 0.5), 0.5, 90, -90), line((0.3, 0), (-1, 0))],
          [line((-1, 0), (0.4, 0)), arc((0.4, -0.5), 0.5, 90, -90), line((0.4, -1), (-1, -1))]],
    'C': [[arc((0, 0), 1, 45, 315)]],
    'D': [[line((-1, 1), (-1, -1))], [line((-1, 1), (0, 1)), arc((0, 0), 1, 90, -90), line((0, -1), (-1, -1))]],
    'E': [poly((1, 1), (-1, 1), (-1, -1), (1, -1)), [line((-1, 0), (0.6, 0))]],
    'F': [poly((1, 1), (-1, 1), (-1, -1)), [line((-1, 0), (0.6, 0))]],
    'G': [[arc((0, 0), 1, 45, 360), line((1, 0), (0.2, 0))]],
    'H': [[line((-1, 1), (-1, -1))], [line((1, 1), (1, -1))], [line((-1, 0), (1, 0))]],
    'I': [[line((0, 1), (0, -1))], [line((-0.5, 1), (0.5, 1))], [line((-0.5, -1), (0.5, -1))]],
    'J': [[line((0.5, 1), (0.5, -0.5)), arc((0, -0.5), 0.5, 0, -180)]],
    'K': [[line((-1, 1), (-1, -1))], poly((1, 1), (-1, 0), (1, -1))],
    'L': [poly((-1, 1), (-1, -1), (1, -1))],
    'M': [poly((-1, -1), (-1, 1), (0, 0), (1, 1), (1, -1))],
    'N': [poly((-1, -1), (-1, 1), (1, -1), (1, 1))],
    'O': [[arc((0, 0), 1, 90, 450)]],
    'P': [[line((-1, 1), (-1, -1))], [line((-1, 1), (0.3, 1)), arc((0.3, 0.5), 0.5, 90, -90), line((0.3, 0), (-1, 0))]],
    'Q': [[arc((0, 0), 1, 90, 450)], [line((0.3, -0.3), (1, -1))]],
    'R': [[line((-1, 1), (-1, -1))], [line((-1, 1), (0.3, 1)), arc((0.3, 0.5), 0.5, 90, -90), line((0.3, 0), (-1, 0))],
          [line((0, 0), (1, -1))]],
    'S': [[arc((0, 0.5), (0.9, 0.5), 20, 270), arc((0, -0.5), (0.9, 0.5), 90, -160)]],
    'T': [[line((0, 1), (0, -1))], [line((-1, 1), (1, 1))]],
    'U': [[line((-1, 1), (-1, -0.2)), arc((0, -0.2), (1, 0.8), 180, 360), line((1, -0.2), (1, 1))]],
    'V': [poly((-1, 1), (0, -1), (1, 1))],
    'W': [poly((-1, 1), (-0.5, -1), (0, 0.3), (0.5, -1), (1, 1))],
    'X': [[line((-1, 1), (1, -1))], [line((1, 1), (-1, -1))]],
    'Y': [poly((-1, 1), (0, 0), (1, 1)), [line((0, 0), (0, -1))]],
    'Z': [poly((-1, 1), (1, 1), (-1, -1), (1, -1))],
    '0': [[arc((0, 0), (0.6, 1), 90, 450)]],
    '1': [poly((-0.4, 0.6), (0, 1), (0, -1))],
    '2': [[curve((-0.8, 0.5), (-0.6, 1.1), (0.9, 1.1), (0.6, 0.2)), line((0.6, 0.2), (-0.8, -1)), line((-0.8, -1), (0.8, -1))]],
    '3': [[arc((0, 0.5), (0.7, 0.5), 150, -90), arc((0, -0.5), (0.8, 0.5), 90, -150)]],
    '4': [poly((0.3, 1), (-0.8, -0.3), (0.8, -0.3)), [line((0.4, 0.6), (0.4, -1))]],
    '5': [[line((-0.6, 1), (-0.7, 0.1)), curve((-0.7, 0.1), (0.6, 0.6), (1.4, -1.0), (-0.8, -0.9))], [line((-0.6, 1), (0.7, 1))]],
    '6': [[curve((0.5, 1), (-0.9, 0.6), (-0.9, -1), (0, -1)), arc((0, -0.45), (0.7, 0.55), 270, 540)]],
    '7': [poly((-0.8, 1), (0.8, 1), (-0.2, -1))],
    '8': [[arc((0, 0.5), (0.6, 0.5), 90, 270), arc((0, -0.5), (0.75, 0.5), 90, -270), arc((0, 0.5), (0.6, 0.5), 270, 450)]],
    '9': [[arc((0, 0.45), (0.7, 0.55), 0, 360), line((0.7, 0.45), (0.7, -1))]],
    '.': [[dot((0, -1))]]}


def circle_points(t0, period_ns, cx, cy, r, laps=1, segments=32, phase_deg=0.0, clockwise=True, z=0):
    """The points of a circle for Piece.path (docs/INSTRUMENT.md, section 7): laps laps of
    radius r mm about (cx, cy, z) from t0, one lap every period_ns, as a polygon of
    segments sides; the angle runs from phase_deg clockwise seen from above (0 is
    ahead, 90 hard right), or the other way. A lap ends where it starts, so laps
    chain; the last point is the first of the next lap. The times round to the ns."""
    import math
    pts = []
    for i in range(laps * segments + 1):
        a = math.radians(phase_deg + (1 if clockwise else -1) * 360.0 * i / segments)
        pts.append((t0 + (i * period_ns + segments // 2) // segments,
                    cx + int(round(r * math.sin(a))), cy + int(round(r * math.cos(a))), z))
    return pts


# docs/INSTRUMENT.md, section 7: the 16 compass points 1 m away, (x, y) in mm,
# 22.5 degrees apart clockwise from north (ahead); east is hard right.
COMPASS = {'n': (0, 1000), 'nne': (383, 924), 'ne': (707, 707), 'ene': (924, 383),
           'e': (1000, 0), 'ese': (924, -383), 'se': (707, -707), 'sse': (383, -924),
           's': (0, -1000), 'ssw': (-383, -924), 'sw': (-707, -707), 'wsw': (-924, -383),
           'w': (-1000, 0), 'wnw': (-924, 383), 'nw': (-707, 707), 'nnw': (-383, 924)}




def tune(p, s, pool, t0, unit, line, vel, gap=25_000_000, glide=60_000_000, step=10_000_000):
    """Play a tokens line from t0, unit ns a unit, on voices from pool: 'NOTE:units',
    r a rest, '~NOTE' a slide. A note ends gap ns before the end of its units; a slide
    keeps the sounding voice of the note before it, which runs up to it, and moves it
    to the new note by pitch events  over glide ns, step ns apart. vel is a
    velocity or vel(k, pos, note) for the k-th note. Returns [(t_on, voice, note)]."""
    toks, total = tokens(line)
    notes = [(t0 + int(round(pos * unit)), t0 + int(round((pos + ln) * unit)), n.lstrip('~'), n.startswith('~'))
             for pos, ln, n in toks if n is not None]
    out, i, k = [], 0, 0
    while i < len(notes):
        j = i
        while j + 1 < len(notes) and notes[j + 1][3] and notes[j + 1][0] == notes[j][1]:
            j += 1
        start, end = notes[i][0], max(notes[j][1] - gap, notes[j][0] + gap)
        v = pool.get(start, end)
        n0 = notes[i][2]
        p.on(s, start, v, n0, vel(k, (start - t0) / unit, n0) if callable(vel) else vel)
        out.append((start, v, n0))
        for a, b in zip(notes[i:j], notes[i + 1:j + 1]):
            ma, mb = midi(a[2]), midi(b[2])
            for q in range(1, glide // step + 1):
                m = ma + (mb - ma) * smooth(q * step / glide)
                p.pitch_f(s, b[0] + q * step, v, mhz_m(m), label(m))
        p.off(s, end, v)
        i, k = j + 1, k + 1
    return out


def chord_notes(names, lo, n):
    """The n lowest notes from MIDI lo up whose pitch classes are in names ('C E G')."""
    pc = pcs(names)
    return [m for m in range(lo, lo + 48) if m % 12 in pc][:n]






# The groovebox game pieces (2026-10-01): the other eight levels, each a live set on
# one FM groovebox: six tracks of one synth (the hook up its staircase, a bouncy
# staccato bass, a kick swept down in pitch with a clap and a crash, metallic hats and
# chirps, detuned stabs and a pad, and foley), two-bar loops, sections made by muting
# and unmuting tracks, and a gap before each change.
class Groove:
    """The groovebox kit those pieces share: the patches, voices and players of five of
    a piece's six streams, bass, drums, perc, chords and foley; the piece writes its
    hook. Build it after the hook's voices, so those come first, add the piece's own
    foley voices, then call streams. Once a piece that uses it is committed, change
    the kit only by adding methods, or keyword arguments whose defaults write what it
    wrote before: every such piece's hash depends on it."""
    MS = 1_000_000
    KICK_MS = (0, 3, 6, 10, 15, 21, 28, 37, 48, 62, 80, 110)    # the kick's pitch events

    def __init__(self, p, rng, kick, pads=8, stabs=8, bass=None, stab=None, pad=None):
        """kick: the note the kick lands on, the key's tonic or fifth near 40 to 65 Hz.
        bass, a (carrier, modulator, comment) triple, replaces the bass patch, stab
        one the stab patch, and pad one the pad patch."""
        self.p, self.rng, self.kick_m = p, rng, midi(kick)
        p.patch('bass', *(bass or (op(1.0, 9, vel=150, a=2, d=300, s=12, r=40),
                                   op(1.0, index=2.2, fb=0.6, vel=170, a=1, d=90, s=22, r=40),
                                   'the bass: 1:1 fed back 0.6 rad, the index plucked down 22 dB in 90 ms, played staccato')))
        p.patch('kick', op(1.0, 5, vel=128, a=1, d=600, s=120, r=60),
                op(1.0, index=2.5, vel=96, a=1, d=40, s=120, r=30),
                'the kick: a sine swept down two octaves onto its note by pitch events, a 1:1 click as it starts')
        p.patch('clap', *recipe('clap', c_level=2))
        p.patch('crash', *recipe('crash', c_level=2))
        p.patch('hat', op(1.0, 14, vel=200, a=1, d=110, s=120, r=25),
                op(1.41, index=3.0, fb=1.3, a=1, d=100, s=0, r=25),
                'the closed hat: 1:1.41 at 3 rad fed back 1.3 rad, more metal than noise')
        p.patch('open', op(1.0, 15, vel=200, a=1, d=1100, s=120, r=40),
                op(1.41, index=3.0, fb=1.3, a=1, d=100, s=0, r=40),
                'the open hat: the closed hat ringing on, choked by the next one')
        p.patch('chirp', op(1.0, 15, vel=160, a=1, d=160, s=120, r=30),
                op(2.0, index=1.2, vel=120, a=1, d=60, s=18, r=30),
                'a chirp: a 1:2 blip whose pitch falls onto its note by pitch events')
        p.patch('stab', *(stab or (op(1.0, 19, vel=150, a=2, d=500, s=14, r=80),
                                   op(1.0, index=1.7, fb=0.9, vel=180, a=1, d=140, s=12, r=80),
                                   'a stab: 1:1 fed back 0.9 rad, the index plucked; each note on two voices 7 cents '
                                   'apart, which beat')))
        if pads:
            p.patch('pad', *(pad or recipe('warm-pad', c_level=2, c_r=-1000, m_r=-1200)))
        p.patch('crackle', *recipe('pin-click', c_level=-2))
        p.patch('riser', *recipe('noise-tail', c_level=-4, c_a=2600, c_r=-1100))
        self.bass = Pool(p.add_voices('bass', 3, 'the bass'), 40)
        self.kickv, self.clapv, self.crashv, self.hatv, self.openv = [p.add_voices(n, 1, 'the ' + t)[0] for n, t in (
            ('kick', 'kick'), ('clap', 'clap'), ('crash', 'crash'), ('hat', 'closed hat'), ('open', 'open hat'))]
        self.chirps = Pool(p.add_voices('chirp', 2, 'chirps'), 30)
        self.stabs = Pool(p.add_voices('stab', stabs, 'the stabs'), 80)
        self.pads = Pool(p.add_voices('pad', pads, 'the pad'), 600) if pads else None
        self.crackv = p.add_voices('crackle', 1, 'crackle')[0]
        self.riserv = p.add_voices('riser', 1, 'the riser')[0]

    def streams(self, hook, foley, drums='', perc='', chords='', bass=''):
        """Declare the six streams; hook and foley say what the piece put on them, and
        drums, perc, chords and bass, when given, what it added to those (', the snare 40')."""
        p = self.p
        p.stream('hook', hook)
        p.stream('bass', 'the bass on voices %d to %d%s' % (self.bass.v[0], self.bass.v[-1], bass))
        p.stream('drums', 'the kick %d, the clap %d, the crash %d%s' % (self.kickv, self.clapv, self.crashv, drums))
        p.stream('perc', 'the closed hat %d, the open hat %d, chirps %d and %d%s' % (
            self.hatv, self.openv, *self.chirps.v, perc))
        p.stream('chords', 'stabs on voices %d to %d%s%s' % (self.stabs.v[0], self.stabs.v[-1], ', the pad on %d to %d' % (
            self.pads.v[0], self.pads.v[-1]) if self.pads else '', chords))
        p.stream('foley', 'crackle %d, the riser %d, %s' % (self.crackv, self.riserv, foley))

    def check(self, *pools):
        """No pool of the kit's, nor any of pools, took a sounding voice."""
        for pool in (self.bass, self.chirps, self.stabs) + ((self.pads,) if self.pads else ()) + pools:
            assert not pool.steals, pool.v

    def vary(self, vel):
        return min(FF, int(vel * (0.94 + 0.12 * self.rng.next())))

    def hit(self, s, v, t, f_mhz, vel, dur, text):
        self.p.on_f(s, t, v, f_mhz, vel, text)
        self.p.off(s, t + dur, v)

    def kick(self, t, vel, dur_ms=300, drop=4.0, tau_ms=15.0, note=None):
        """The kick: a note-on at drop times its note, then pitch events at KICK_MS easing
        down to it with a time constant of tau_ms, the last one on the note. note, a
        name, lands this kick elsewhere than the kit's note, as after a key change. The
        eased pitches are decimal, half up to the mHz . The note-on keeps the
        half-even round of one correctly rounded binary product, which is portable; half
        up would move cupola, whose first kicks (G1, drop 1.5) land on 73498.5 mHz."""
        km = self.kick_m if note is None else midi(note)
        p, v, f1 = self.p, self.kickv, mhz_m(km)
        p.on_f('drums', t, v, round(f1 * drop), vel, 'kick')
        with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
            d, tau = decimal_value(drop), decimal_value(tau_ms)
            fs = [half_up(f1 * (1 + (d - 1) * (-decimal_value(ms) / tau).exp())) for ms in self.KICK_MS[1:-1]]
        for ms, f in zip(self.KICK_MS[1:-1], fs):
            p.pitch_f('drums', t + ms * self.MS, v, f, label(midi_of(f)))
        p.pitch_f('drums', t + self.KICK_MS[-1] * self.MS, v, f1, label(km))
        p.off('drums', t + dur_ms * self.MS, v)

    def clap(self, t, vel):
        """Three bursts 10 ms apart, then a tail."""
        for j in range(3):
            self.hit('drums', self.clapv, t + j * 10 * self.MS, 1_200_000, self.vary(vel), 4 * self.MS, 'clap')
        self.hit('drums', self.clapv, t + 30 * self.MS, 1_200_000, vel, 250 * self.MS, 'clap tail')

    def crash(self, t, vel, dur_ms=2000):
        self.hit('drums', self.crashv, t, 5_000_000, vel, dur_ms * self.MS, 'crash')

    def hat(self, t, vel):
        self.hit('perc', self.hatv, t, 7_000_000, vel, 40 * self.MS, 'hat')

    def open_hat(self, t, vel, until):
        """The open hat from t, choked at until, where the next closed hat plays."""
        self.hit('perc', self.openv, t, 7_000_000, vel, until - t, 'open hat')

    def chirp(self, t, m, vel, drop=9, ms=36, dur_ms=110, step_ms=4):
        """A chirp onto MIDI m: from drop semitones above it (below, when drop < 0),
        pitch events step_ms apart easing out onto m over ms."""
        p = self.p
        v = self.chirps.get(t, t + dur_ms * self.MS)
        p.on_f('perc', t, v, mhz_m(m + drop), vel, '%s, a chirp onto %s' % (label(m + drop), label(m)))
        n = ms // step_ms
        for q in range(1, n + 1):
            w = 1 - q / n
            x = m + drop * (w * w)                         # a product, not libm's pow
            p.pitch_f('perc', t + q * step_ms * self.MS, v, mhz_m(x), label(x))
        p.off('perc', t + dur_ms * self.MS, v)

    def bass_note(self, t, m, vel, dur_ms):
        v = self.bass.get(t, t + dur_ms * self.MS)
        self.p.on_f('bass', t, v, mhz_m(m), vel, label(m))
        self.p.off('bass', t + dur_ms * self.MS, v)

    def bass_bend(self, t, m, vel, dur_ms, moves=(), step_ms=5):
        """A bass note that bends while it sounds: MIDI m from t for dur_ms, and each
        (at_ms, to, ms) of moves eases it from where it is to MIDI to, from at_ms after
        t over ms, by pitch events  step_ms apart: a scoop into the note, a slide
        to the next, a fall at its end. Returns its voice."""
        p = self.p
        v = self.bass.get(t, t + dur_ms * self.MS)
        p.on_f('bass', t, v, mhz_m(m), vel, label(m))
        cur = m
        for at_ms, to, ms in moves:
            n = max(1, ms // step_ms)
            for q in range(1, n + 1):
                x = cur + (to - cur) * smooth(q / n)
                p.pitch_f('bass', t + (at_ms + q * step_ms) * self.MS, v, mhz_m(x), label(x))
            cur = to
        p.off('bass', t + dur_ms * self.MS, v)
        return v

    def stab(self, t, notes, vel, dur_ms=150, cents=7.0):
        """Each MIDI note of notes on two voices, cents flat and sharp, the voices 1 ms apart."""
        p, k = self.p, 0
        for m in notes:
            for c in (-cents, cents):
                tt = t + k * self.MS
                v = self.stabs.get(tt, tt + dur_ms * self.MS)
                p.on_f('chords', tt, v, mhz_m(m + c / 100), vel, label(m + c / 100))
                p.off('chords', tt + dur_ms * self.MS, v)
                k += 1

    def pad(self, t0, t1, notes, vel):
        """A chord on the pad from t0 to t1, its notes 3 ms apart from the bottom."""
        for j, m in enumerate(notes):
            v = self.pads.get(t0, t1)
            self.p.on_f('chords', t0 + 3 * j * self.MS, v, mhz_m(m), vel, label(m))
            self.p.off('chords', t1, v)

    def crackle(self, t0, t1, per_s, vel, lo=1500, hi=6000):
        """Clicks from t0 to t1, about per_s a second at random times, none closer than
        20 ms, each at a random pitch from lo to hi Hz and a random velocity up to vel."""
        import math
        t = t0
        while True:
            t += max(20 * self.MS, int(-math.log(1 - self.rng.next()) / per_s * 1e9))
            if t + 12 * self.MS >= t1:
                break
            f = log_between(lo, hi, self.rng.next())
            self.hit('foley', self.crackv, t, millihz(f), int(vel * (0.3 + 0.7 * self.rng.next())), 12 * self.MS,
                     'crackle')

    def riser(self, t0, t1, f0, f1, vel):
        """Noise swelling in from t0, its pitch eased up from f0 to f1 Hz by t1, where it ends."""
        a, b = midi_of(1000 * f0), midi_of(1000 * f1)
        self.p.curve('foley', self.riserv, t0, t1, lambda u: a + (b - a) * smooth(u / (t1 - t0)), vel, 40 * self.MS,
                     cents=5.0, bend=True)
        self.p.off('foley', t1, self.riserv)



















































































































































































def cond_lab(fs=48000, scale=False):
    """regression's fixture ( part A): conditional trigs, the new arp modes, held
    velocities and strummed chords, at 48000 Hz and as cond-lab-44100. scale=True is
    regression's fixture , cond-lab-scale: the same piece with its bass, stabs,
    piano and harp through the scale lock of A minor pentatonic (scale=, root 9)."""
    base = 'cond-lab-scale' if scale else 'cond-lab'
    name = base if fs == 48000 else '%s-%d' % (base, fs)
    SC = ('minor-pentatonic', 9) if scale else None          # A C D E G; None writes cond-lab as before
    text = [
        '%s: a groove in A Dorian on the groove helpers of  part A. 100 bpm, 4/4, 4 bars and a' % name,
        'last hit, %d Hz. The drums are one steps() call a part over 4 passes, bar 4 the fill: the kick' % fs,
        'skips the a of 1 in bar 1 (not-first), drops its third beat in the fill and adds a pickup on the',
        'a of 4 in bars 2 and 4 (A:B 2:2); the snare, its body and its wires on one seed, comes in at',
        'bar 2 (not-first) and rolls on the last two 16ths of the fill; the closed hats on every 16th,',
        'the e and the a at 85 percent, a ratchet of two on the a of 3, the last 16th silent in the fill;',
        'the ghost notes at even odds, two of them on alternate passes. The bass, the plucked-bass',
        'recipe, is one call a bar that carries the pass count on (pass0), its octave pop on bars 2 and',
        '4 (2:2). The clav stabs are the arpeggiator in chord mode, the whole chord on every offbeat;',
        'the electric piano arpeggiates down and up (downup) in bars 1 and 2 and in the order played',
        '(played) in bars 3 and 4, each note at its own velocity (vel held). The harp strums D9 down',
        'over 40 ms on the and of 4 and Am9 up over 120 ms at the last hit (Piece.chord, the spread a',
        'total; every note of a chord released together). Every chance comes from the piece\'s seed,',
        '2710. No recipe is new, there is no new DSP, and the helpers write only on and off events.']
    if scale:
        text += ['regression (): the bass, the stabs, the piano and the harp go through the scale lock of A',
                 'minor pentatonic (A C D E G, root 9), each note snapped down to the scale as it sounds: F# to',
                 'E and B to A, so the D stab plays C E E A, two notes on one pitch on their own voices, and D9',
                 'strums D E C E. The drums and the hats are not locked. Every other event is cond-lab\'s.']
    p = Piece(name, 100, 4, text, fs=fs)
    p.seed = 2710                                             # every helper's chances start from it
    MS = 1_000_000
    p.length_ns = p.t(6)                                      # 4 bars and a last hit; set first, so the helpers check
    p.patch('kick', *recipe('kick'))
    p.patch('body', *recipe('snare-body'))
    p.patch('wires', *recipe('snare-wires', c_level=6))
    p.patch('ghost', *recipe('snare-wires', c_level=14))
    p.patch('hat', *recipe('hat'))
    p.patch('bass', *recipe('plucked-bass'))
    p.patch('clav', *recipe('ep-body', c_level=4))
    p.patch('ep', *recipe('epiano', c_level=2))
    p.patch('harp', *recipe('harp', c_level=2))
    kick = p.add_voices('kick', 3, 'the kick')
    body = p.add_voices('body', 3, 'the snare body')
    wires = p.add_voices('wires', 3, 'the snare wires')
    hats = p.add_voices('hat', 3, 'the closed hats')
    ghost = p.add_voices('ghost', 2, 'the ghost notes')
    bass = p.add_voices('bass', 3, 'the bass')
    clav = p.add_voices('clav', 8, 'the stabs, four notes an offbeat')
    keys = p.add_voices('ep', 4, 'the electric piano')
    harp = p.add_voices('harp', 10, 'the strummed harp')
    p.stream('drums', 'the kick on voices %d to %d, the snare body %d to %d and its wires %d to %d'
             % (kick[0], kick[-1], body[0], body[-1], wires[0], wires[-1]))
    p.stream('perc', 'the closed hats on voices %d to %d, the ghost notes %d and %d' % (hats[0], hats[-1], *ghost))
    p.stream('bass', 'the bass on voices %d to %d' % (bass[0], bass[-1]))
    p.stream('keys', 'the stabs on voices %d to %d, the electric piano %d to %d' % (clav[0], clav[-1], keys[0], keys[-1]))
    p.stream('harp', 'the strummed harp on voices %d to %d' % (harp[0], harp[-1]))
    for bar, what in ((1, 'the kick, the hats, the ghost notes, the bass, the stabs and the piano down and up'),
                      (2, 'the snare comes in'), (3, 'the piano in the order played, its own velocities'),
                      (4, 'the fill, and D9 strummed down'), (5, 'the last hit, Am9 strummed up')):
        for s in p.streams:
            p.mark(s, p.t(bar), 'bar %d: %s' % (bar, what))
    SW = 56                                                   # the 16ths' swing, in percent
    FILL = (3,)                                               # pass 3, bar 4, is the fill
    p.steps('drums', kick, 'x..x|..x.|x...|..xx', 0, 0.25, 'G1', vel={0: F, 3: MP, 6: MF, 8: F, 14: MF, 15: MP},
            cond={3: 'not-first', 8: 'not-fill', 15: '2:2'}, passes=4, fill=FILL, seed=1, gate=2.0, swing=SW, text='kick')
    sn = dict(vel={4: F, 12: F, 14: MP, 15: MF}, micro={4: 0.02, 12: 0.03}, cond={4: 'not-first', 12: 'not-first',
              14: 'fill', 15: 'fill'}, passes=4, fill=FILL, seed=2, gate=1.5, swing=SW)
    p.steps('drums', body, '....|x...|....|x.xx', 0, 0.25, 'F#3', text='snare body', **sn)
    p.steps('drums', wires, '....|x...|....|x.xx', 0, 0.25, 'A6', text='snare wires', **sn)
    p.steps('perc', hats, 'xxxx|xxxx|xxxx|xxxx', 0, 0.25, 'A8', vel=[MF, P, MP, P], prob=[1.0, 0.85],
            micro=[0.0, 0.008, -0.004, 0.006], ratchet={11: 2}, cond={15: 'not-fill'}, passes=4, fill=FILL,
            seed=3, gate=0.3, swing=SW, text='hat')
    p.steps('perc', ghost, '..x.|.x.x|..x.|.x..', 0, 0.25, 'A6', vel=[P, PP], prob=0.5, micro=-0.01,
            cond={2: '1:2', 13: '2:2'}, passes=4, seed=4, gate=0.5, swing=SW, text='ghost')
    CH = {1: 'am', 2: 'd', 3: 'am', 4: 'd'}
    BASS = {'am': {0: 'A1', 3: 'A1', 6: 'C2', 8: 'E2', 11: 'G2', 13: 'A2'},
            'd': {0: 'D2', 3: 'D2', 6: 'F#2', 8: 'A2', 11: 'C3', 13: 'D3'}}
    STAB = {'am': ('C3', 'E3', 'G3', 'B3'), 'd': ('C3', 'E3', 'F#3', 'A3')}
    ARP = {'am': ('E4', 'C4', 'B4', 'G4'), 'd': ('F#4', 'C4', 'A4', 'E4')}
    HELD = {'am': [('E4', MF), ('C4', PP), ('B4', MP), ('G4', P)], 'd': [('A4', MF), ('C4', PP), ('F#4', MP), ('E4', P)]}
    for bar in range(1, 5):
        b0, ch = 4 * (bar - 1), CH[bar]
        p.steps('bass', bass, 'x..x|..x.|x..x|.x..', b0, 0.25, BASS[ch], vel={0: F, 3: MP, 6: MF, 8: MF, 11: MP, 13: MF},
                cond={13: '2:2'}, pass0=bar - 1, seed=5, gate=0.6, swing=SW, scale=SC)
        p.arp('keys', clav, STAB[ch], b0 + 0.5, 4, 1, mode='chord', gate=0.3, vel=MP, scale=SC)
        if bar <= 2:
            p.arp('keys', keys, ARP[ch], b0, 4, 0.5, mode='downup', gate=0.8, vel=[MF, P, MP, P], scale=SC)
        else:
            p.arp('keys', keys, HELD[ch], b0, 4, 0.5, mode='played', gate=0.8, vel='held', scale=SC)
    p.chord('harp', harp, 'D3', p.t(4, 4.5), p.t(5) - p.t(4, 4.5), [MF, MP, MP, P], chord_shape(['D3', 'F#3', 'C4', 'E4']),
            strum_ms=40, direction='down', scale=SC)
    b0 = 4 * 4                                                # 5: the last hit
    p.steps('drums', kick, 'x', b0, 0.25, 'G1', vel=F, gate=2.0, text='kick')
    p.steps('bass', bass, 'x', b0, 0.25, 'A1', vel=MF, gate=6.0, scale=SC)
    p.chord('harp', harp, 'A2', p.t(5), 2000 * MS, [MF, MF, MP, MP, P, P], chord_shape(['A2', 'E3', 'G3', 'C4', 'E4', 'B4']),
            strum_ms=120, direction='up', scale=SC)
    return p


LOCK_ACC = 'X.xX..Xx.xX.X.x.'                                 # lock-lanes' accents a bar: X full, x 3 dB and . 9 dB down
LOCK_SLIDE = (7, 15, 23, 31, 38, 47, 55, 62)                  # its steps that slide to the next


def lock_lanes_locks():
    """lock-lanes' 8 ctl locks for each of its 64 steps (regression, ): {step: [(N,
    VALUE_Q15)] for N 0 to 7}, whole numbers. ctl0 the cutoff, ctl1 Q, ctl2 the
    modulator's index, ctl3 the carrier's level, ctl4 and ctl5 the carrier's and the
    modulator's pitch, ctl6 the LFO's index depth and ctl7 its rate, by the accent of the
    step (LOCK_ACC); bar 3 opens the filter a little more; a sliding step (LOCK_SLIDE)
    scoops 50 cents below its note and glides to the next step's values."""
    table = {'X': (20480, 10923, 1024, 0, 8192, 16384), 'x': (13653, 5461, 512, -5461, 4096, 8192),
             '.': (6827, 2731, 192, -16384, 0, 8192)}
    out = {}
    for g in range(64):
        cut, q, index, level, wobble, rate = table[LOCK_ACC[g % 16]]
        bend = -1365 if g in LOCK_SLIDE else 0
        cut += 2731 if g // 16 == 2 else 0
        out[g] = list(enumerate((cut, q, index, level, bend, bend + 55, wobble, rate)))
    return out


def lock_lanes(fs=48000, via='steps'):
    """regression's fixture : 64 sixteenths of a mono FM bass with 8 ctl locks a step,
    at 48000 Hz and as lock-lanes-44100. via='lanes' writes the same ctl events as
    motion lanes (Piece.ctl_lane) before a lockless steps call: the same record, byte
    for byte (compose.py --selftest-perf, the regression suite)."""
    name = 'lock-lanes' if fs == 48000 else 'lock-lanes-%d' % fs
    p = Piece(name, 100, 4, [
        '%s: a funk bass in A minor whose every step locks all eight modulation matrix controllers . 100' % name,
        'bpm, 4/4, 4 bars of sixteenths and a last note, %d Hz. The bass, the plucked-bass recipe' % fs,
        'with a 40 ms release through a low-pass at 320 Hz and a 4 Hz LFO, is one steps() call of 64',
        'hits on one voice, the sixteenths swung to 54 percent: each step locks ctl0 the cutoff (up',
        'to 4 octaves), ctl1 the resonance, ctl2 the modulator\'s index, ctl3 the carrier\'s level',
        '(its ghosts 9 dB down), ctl4 and ctl5 the two operators\' pitch (the modulator 2 cents',
        'sharp, for warmth), ctl6 the LFO\'s index depth and ctl7 its rate, by the accent of the',
        'step; bar 3, on D, opens the filter further. Eight steps slide: their locks scoop the note',
        '50 cents flat and glide to the next step\'s, eight points a step by fm_glide_t\'s rule; every',
        'other step sets its controllers back to 0 at the end of its busy window. The kick, the',
        'snare and its ghosts and the closed hats are lockless steps() calls over 4 passes. No',
        'recipe is new, there is no new DSP, and the controller events are plain ctl.'],
        fs=fs)
    p.seed = 2810
    p.length_ns = p.t(6)                                      # 4 bars and a last note; set first, so the helpers check
    REL, SW = 40, 54                                          # the bass's carrier release in ms, the 16ths' swing
    p.patch('kick', *recipe('kick'))
    p.patch('body', *recipe('snare-body'))
    p.patch('wires', *recipe('snare-wires', c_level=6))
    p.patch('ghost', *recipe('snare-wires', c_level=14))
    p.patch('hat', *recipe('hat'))
    p.patch('bass', *recipe('plucked-bass', c_r=REL - 150))  # 150 ms down to 40, so a voice frees in a sixteenth
    p.filter('bass', 'lp', 320, 0.8)
    p.lfo('bass', 0, 4000, 0, 0, 0, 0, 0)
    for slot, (target, amount, shift) in enumerate((('cutoff', 4800, 3), ('q', 6000, 3), ('index1', 65535, 0),
                                                    ('level0', 180, 3), ('pitch0', 1200, 0), ('pitch1', 1200, 0),
                                                    ('lfo_index', 1024, 3), ('lfo_rate', 16000, 3))):
        p.mod('bass', slot, 'ctl%d' % slot, target, amount, shift)
    kick = p.add_voices('kick', 3, 'the kick')
    body = p.add_voices('body', 3, 'the snare body')
    wires = p.add_voices('wires', 3, 'the snare wires')
    hats = p.add_voices('hat', 3, 'the closed hats')
    ghost = p.add_voices('ghost', 2, 'the ghost notes')
    bass = p.add_voices('bass', 1, 'the bass, one voice, its 8 controllers locked every step')
    p.stream('drums', 'the kick on voices %d to %d, the snare body %d to %d and its wires %d to %d'
             % (kick[0], kick[-1], body[0], body[-1], wires[0], wires[-1]))
    p.stream('perc', 'the closed hats on voices %d to %d, the ghost notes %d and %d' % (hats[0], hats[-1], *ghost))
    p.stream('bass', 'the bass on voice %d, with its ctl locks, slides and restores' % bass[0])
    for bar, what in ((1, 'the groove on A'), (2, 'the groove again'), (3, 'on D, the filter further open'),
                      (4, 'back on A, the turnaround'), (5, 'the last note')):
        for s in p.streams:
            p.mark(s, p.t(bar), 'bar %d: %s' % (bar, what))
    p.steps('drums', kick, 'x..x|....|x.x.|....', 0, 0.25, 'G1', vel={0: F, 3: MP, 8: MF, 10: MF},
            passes=4, seed=1, gate=2.0, swing=SW, text='kick')
    sn = dict(vel={4: F, 12: F}, micro={4: 0.02, 12: 0.03}, passes=4, seed=2, gate=1.5, swing=SW)
    p.steps('drums', body, '....|x...|....|x...', 0, 0.25, 'F#3', text='snare body', **sn)
    p.steps('drums', wires, '....|x...|....|x...', 0, 0.25, 'A6', text='snare wires', **sn)
    p.steps('perc', hats, 'xxxx|xxxx|xxxx|xxxx', 0, 0.25, 'A8', vel=[MF, P, MP, P], prob=[1.0, 0.85],
            micro=[0.0, 0.008, -0.004, 0.006], passes=4, seed=3, gate=0.3, swing=SW, text='hat')
    p.steps('perc', ghost, '..x.|...x|..x.|.x.x', 0, 0.25, 'A6', vel=[P, PP], prob=0.6, micro=-0.01,
            passes=4, seed=4, gate=0.5, swing=SW, text='ghost')
    A = ['A1', 'A1', 'A2', 'A1', 'G1', 'A1', 'C2', 'A1', 'A1', 'E2', 'A1', 'G2', 'A1', 'A1', 'C2', 'D2']
    D = ['D2', 'D2', 'D3', 'D2', 'C2', 'D2', 'F2', 'D2', 'D2', 'A2', 'D2', 'C3', 'D2', 'D2', 'F2', 'G2']
    T = ['A1', 'A1', 'A2', 'A1', 'G1', 'A1', 'C2', 'A1', 'E2', 'E2', 'G2', 'E2', 'A1', 'C2', 'D2', 'E2']
    notes, vel = A + A + D + T, [{'X': F, 'x': MF, '.': MP}[c] for c in LOCK_ACC]
    locks = lock_lanes_locks()
    bass_call = dict(vel=vel, gate=0.5, swing=SW, release_ms=REL, text='bass')
    if via == 'steps':
        p.steps('bass', bass, 'x' * 64, 0, 0.25, notes, locks={g: {'ctl': lk} for g, lk in locks.items()},
                slide={g: True for g in LOCK_SLIDE}, **bass_call)
    elif via == 'lanes':                                      # the same events, one lane an input, before the call
        hits = [(p.at_beat(g / 4 + (0.25 * (SW - 50) / 50 if g % 2 else 0)),
                 p.at_beat(g / 4 + (0.25 * (SW - 50) / 50 if g % 2 else 0) + 0.5 * 0.25)) for g in range(64)]
        free = RoundRobin.window(REL)
        for n in range(8):
            pts = []
            for g, (t0, t1) in enumerate(hits):
                a = locks[g][n][1]
                pts.append((t0, a, 'bass, step %d, ctl %d lock' % (g, n)))
                if g in LOCK_SLIDE:
                    u0, d = hits[g + 1][0], locks[g + 1][n][1] - a
                    pts += [(t0 + k * (u0 - t0) // 8, a + (k * abs(d) // 8) * (1 if d >= 0 else -1),
                             'bass, step %d, ctl %d slide %d of 8' % (g, n, k)) for k in range(1, 8)]
                elif t1 + free < p.length_ns:
                    pts.append((t1 + free, 0, 'bass, step %d, ctl %d lock released' % (g, n)))
            p.ctl_lane('bass', bass[0], n, pts)
        p.steps('bass', bass, 'x' * 64, 0, 0.25, notes, **bass_call)
    else:
        raise ValueError('lock_lanes: via is steps or lanes, not %r' % (via,))
    b0 = 4 * 4                                                # 5: the last note, lockless, its controllers at 0
    p.steps('drums', kick, 'x', b0, 0.25, 'G1', vel=F, gate=2.0, text='kick')
    p.steps('bass', bass, 'x', b0, 1, 'A1', vel=MF, gate=3.0, release_ms=REL, text='bass')
    return p

def _selftest_carson(ok):
    saved = dict(RECIPES)
    try:
        RECIPES['carson-test'] = ({'ratio':1}, {'ratio':1,'index':8}, 'independent rational case')
        plain=recipe('carson-test')
        ok(plain==recipe('carson-test',carson=None) and plain[1][9:11]==[0,0], 'Carson remains off by default')
        original=copy.deepcopy(RECIPES)
        for fs,breakpoint in ((48000,2160000),(44100,1984500)):
            note=2*breakpoint
            guarded=recipe('carson-test',carson=(fs,[note,1,note]))
            ok(guarded[1][9:11]==[breakpoint,86], 'Carson exact rational breakpoint and smallest slope86')
            ok(guarded[0]==plain[0] and guarded[1][:9]==plain[1][:9] and guarded[1][11:]==plain[1][11:] and
               guarded[3]==plain[3], 'Carson changes only source key-scaling fields and comment')
            ok(guarded[2].endswith('fs=%d onsets_millihz=1,%d'%(fs,note)), 'Carson finite domain sorts and deduplicates')
            # At twice the break, log2 is exactly1. Edge <=E iff gain <=3/8.
            with decimal.localcontext(decimal.Context(prec=50,rounding=decimal.ROUND_HALF_UP)):
                D=decimal.Decimal
                ok(D(10)**(-D(86)/200)<=D(3)/8<D(10)**(-D(85)/200), 'independent predecessor inequality for slope86')
            with decimal.localcontext() as ctx:
                ctx.prec=3;ctx.rounding=decimal.ROUND_FLOOR
                ok(recipe('carson-test',carson=(fs,[note]))[1][9:11]==[breakpoint,86], 'Carson ignores ambient Decimal context')
        ok(RECIPES==original, 'Carson never mutates recipe definitions')
        RECIPES['carson-unit'] = ({'ratio':1}, {'ratio':1,'index':1}, 'equality case')
        equality=recipe('carson-unit',carson=(48000,[7200000]))
        ok(equality[1][9:11]==[7200000,0], 'Carson ideal equality remains accepted; native deviation is measured separately')
        operators=[op(),op(index=8)]
        def bad(request=(48000,[4320000]), extra=None, fields=None, tweaks=None, text=''):
            try:_carson_fields('check',fields or operators,extra or {},request,tweaks or {})
            except TypeError as e:return str(e).startswith('recipe check: Carson ') and text in str(e)
            return False
        for request,text in [(True,'needs'),((48000,),'needs'),((True,[1]),'fs'),((0,[1]),'fs'),
                             ((4294967296,[1]),'fs'),((48000,[]),'onsets'),((48000,'1'),'onsets'),
                             ((48000,[True]),'onsets'),((48000,[0]),'onsets'),((48000,[1.0]),'onsets'),
                             ((48000,[4294967296]),'onsets')]:
            ok(bad(request=request,text=text),'Carson strict request refusal '+text)
        for i in (0,1):
            for key,value,text in [('wave',1,'sine'),('fixed',440,'fixed'),('ratio',0,'positive')]:
                fields=[op(),op(index=8)];fields[i]=op(**{key:value})
                ok(bad(fields=fields,text='operator%d'%i) and bad(fields=fields,text=text), 'Carson checked pair scope '+key)
            for kind in ('wtuse','pd','ksource'):
                ok(bad(extra={kind:{i:1}},text='operator%d has %s'%(i,kind)), 'Carson source/target binding refusal '+kind)
        for field in ('ks_break','ks_slope'):
            ok(bad(fields=[op(),op(index=8,**{field:1})],text='already has key scaling'), 'Carson preserves authored key scaling')
            ok(bad(tweaks={'m_'+field:0},text='explicit key-scaling tweak'), 'Carson explicit zero delta refuses')
        ok(bad(extra={'ks':{1:0}},text='left-slope'),'Carson refuses even explicit zero left-slope metadata')
        ok(bad(extra={'kstype':{1:('level','-','-')}},text='kstype'),'Carson refuses level key-scaling mode')
        ok(_carson_fields('check',operators,{'kstype':{1:('index','-','-')}},(48000,[4320000]),{})[0]=={1:(2160000,86)},
           'explicit default index key-scaling mode remains allowed')
        for kind in ('am','ring','sync'):
            ok(bad(extra={'ops':2,'edges':[(1,0,256,kind)]},text='only PM'),'Carson refuses '+kind)
        ok(bad(extra={'ops':2,'edges':[]},text='needs a PM'),'Carson no-PM recipe refuses')
        ok(bad(request=(48000,[12000000]),text='operator1->0 onset12000000 cannot'), 'Carson impossible onset names pair/onset')
        ok(bad(request=(48000,[10800000]),text='operator1->0 onset10800000 has no slope'), 'Carson finite1200 search can refuse')
        ok(bad(request=(1,[1]),fields=[op(ratio=65535),op(ratio=65535,index=8)],text='below1'), 'Carson sub-mHz breakpoint refuses')
        ok(_carson_fields('check',[op(ratio=1/65536),op(ratio=1/65536)],{},(4294967295,[4294967295]),{})[0][1]==(4294967295,0),
           'Carson clamps only the raw uint32 breakpoint maximum')
        RECIPES['carson-route']=({'ratio':1},{'ratio':2},'two independently checked edges',
                                {'ops':3,'carriers':3,'edges':[(2,0,256),(2,1,128)],'more':[{'ratio':1,'index':8}]})
        routed=recipe('carson-route',carson=(48000,[4320000]))
        ok(routed[3]['more'][0][9:11]==[2160000,86] and routed[0][9:11]==[0,0] and routed[1][9:11]==[0,0],
           'Carson multi-edge source uses strictest pair and changes only its fields')
        before=copy.deepcopy(RECIPES)
        try:recipe('bl-saw',carson=(48000,[440000]))
        except TypeError as e:ok('wtuse' in str(e) and RECIPES==before,'Carson recipe rejects WT with no mutation')
        else:ok(False,'Carson WT recipe must refuse')
    finally:
        RECIPES.clear();RECIPES.update(saved)
    print('selftest-perf: ok Carson ideal-only finite domain: exact fields, minimal slope, scope and named refusals')


def _selftest_fmx(ok):
    import tempfile
    from pathlib import Path
    ok(fmx_harmonics('all',skirt=0)==((0,1,0,0),), 'fmx All skirt0 is the fundamental')
    ok(fmx_harmonics('odd',skirt=0)==((0,1,0,0),), 'fmx Odd skirt0 is the fundamental')
    ok(fmx_harmonics('res',skirt=0,resonance=99)==((0,100,0,0),), 'fmx Res skirt0 picks exact center100')
    ok([r[1] for r in fmx_harmonics('all',skirt=7)]==list(range(1,129)), 'fmx All maximum skirt count')
    ok([r[1] for r in fmx_harmonics('odd',skirt=7)]==list(range(1,256,2)), 'fmx Odd maximum skirt/count/set')
    ok(fmx_harmonics('all',skirt=2)==((0,1,0,0),(0,2,60,0),(0,3,95,0),(0,4,120,0)), 'fmx raw attenuation values')
    resonant=fmx_harmonics('res',skirt=2,resonance=4)
    ok(resonant==((0,2,120,0),(0,3,95,0),(0,4,60,0),(0,5,0,0),(0,6,60,0),(0,7,95,0),(0,8,120,0)),
       'fmx Res center, symmetry and width are exact')
    ok(fmx_harmonics('res',skirt=7,resonance=0,length=32)[-1][1]==15,
       'fmx Res truncates width at native length')
    ok(len(fmx_harmonics('all',skirt=7,length=32))==15 and len(fmx_harmonics('odd',skirt=7,length=32))==8,
       'fmx All/Odd truncate at native length')
    for length in (32,64,128,256,512,1024,2048):
        for skirt in (0,7):
            for kind in ('all','odd','res'):
                rows=fmx_harmonics(kind,skirt=skirt,length=length)
                ok(wtbank_values('test',1,length,rows)==(1,length,rows),'fmx all endpoints obey native bank bounds')
    for kind, kw, text in [('x',{'skirt':0},'kind'),('all',{'skirt':True},'skirt'),
                           ('all',{'skirt':-1},'skirt'),('all',{'skirt':8},'skirt'),
                           ('all',{'skirt':0,'resonance':1},'only'),('odd',{'skirt':0,'resonance':99},'only'),
                           ('res',{'skirt':0,'resonance':True},'resonance'),('res',{'skirt':0,'resonance':100},'resonance'),
                           ('res',{'skirt':0,'resonance':-1},'resonance'),('res',{'skirt':0,'resonance':99,'length':128},'center'),
                           ('all',{'skirt':0,'length':31},'length'),('all',{'skirt':0,'length':True},'length')]:
        try:fmx_harmonics(kind,**kw)
        except ValueError as e:ok(text in str(e),'fmx named refusal '+text)
        else:ok(False,'fmx invalid input must refuse')
    with decimal.localcontext() as ctx:
        ctx.prec=2;ctx.rounding=decimal.ROUND_CEILING
        ok(fmx_harmonics('res',skirt=2,resonance=4)==resonant,'fmx raw values ignore ambient Decimal context')
    with tempfile.TemporaryDirectory(prefix='fmx-recipes-') as tmp:
        src=Path(tmp)/'recipes.txt'
        for form in ('fmx-all','fmx-odd','fmx-res'):
            src.write_text('recipe test\ncarrier\nmodulator\nwtuse 0 '+form+' 0\nnote own FM-X-like law\n',encoding='utf-8')
            book=load_recipes(src)
            expected=fmx_harmonics(form[4:],skirt=3,resonance=4 if form=='fmx-res' else 0)
            ok(book['test'][3]['wtbanks']=={form:(1,2048,expected)} and
               book['test'][3]['wtuse']=={0:(form,0)}, 'fmx recipe form defaults '+form)
    print('selftest-perf: ok FM-X-like forms: declared All/Odd/Res law, strict bounds and recipe defaults')


def _selftest_wavetables(ok):
    """ pure-authoring checks; the native additive oracle is separate."""
    import tempfile
    from pathlib import Path
    global MUSIC, PROFILE

    def piece():
        p = Piece('wt-test', 120, 4, ['bank test'])
        p.patch('p', op(level=20), op(), 'test')
        p.stream('a', 'test'); p.add_voices('p', 1, '')
        p.length_ns = 100000000
        p.on_f('a', 0, 0, 220000, 32767, ''); p.off('a', 50000000, 0)
        return p

    def refusal(p, fn, text):
        before = copy.deepcopy(p.__dict__)
        try:
            fn()
        except (ValueError, TypeError) as e:
            return text in str(e) and p.__dict__ == before
        return False

    p = piece(); rows = [[1, 1024, 1200, 65535], [0, 1, 0, 0]]
    ok(p.wavetable('bank', 2, 32, harmonics=rows) is None, 'wavetable returns None')
    definition = p.wavetables['bank']; rows[1][2] = 100
    ok(definition == (2, 32, ((0, 1, 0, 0), (1, 1024, 1200, 65535))), 'bank rows are copied and sorted')
    old = copy.deepcopy(p.__dict__)
    p.wavetable('bank', *definition[:2], harmonics=list(reversed(definition[2])))
    ok(p.__dict__ == old, 'identical normalized bank deduplicates')
    for bank, frames, length, harmonics, message in [
            ('', 1, 32, [(0,1,0,0)], 'name'), ('a'*32, 1, 32, [(0,1,0,0)], 'name'),
            ('é', 1, 32, [(0,1,0,0)], 'name'), ('a b', 1, 32, [(0,1,0,0)], 'name'),
            ('x', 0, 32, [(0,1,0,0)], 'frames'), ('x', 9, 32, [(0,1,0,0)], 'frames'),
            ('x', True, 32, [(0,1,0,0)], 'frames'), ('x', 1, 31, [(0,1,0,0)], 'length'),
            ('x', 1, 64.0, [(0,1,0,0)], 'length'), ('x', 1, 33, [(0,1,0,0)], 'length'),
            ('x', 1, 4096, [(0,1,0,0)], 'length'), ('x', 1, 32, [], 'nonempty'),
            ('x', 1, 32, [(0,1,0)], 'four integers'), ('x', 1, 32, [(0,True,0,0)], 'four integers'),
            ('x', 1, 32, [(1,1,0,0)], 'range'), ('x', 1, 32, [(0,0,0,0)], 'range'),
            ('x', 1, 32, [(0,1025,0,0)], 'range'), ('x', 1, 32, [(0,1,-1,0)], 'range'),
            ('x', 1, 32, [(0,1,1201,0)], 'range'), ('x', 1, 32, [(0,1,0,-1)], 'range'),
            ('x', 1, 32, [(0,1,0,65536)], 'range'), ('x', 1, 32, [(0,1,0,0)]*2, 'duplicate'),
            ('bank', 1, 32, [(0,1,0,0)], 'conflicting')]:
        ok(refusal(p, lambda:p.wavetable(bank,frames,length,harmonics=harmonics), message),
           'wavetable bound/refusal is atomic: '+message)
    for n in range(7): p.wavetable('extra%d'%n, 1, 32, harmonics=((0,1,0,0),))
    ok(refusal(p, lambda:p.wavetable('ninth',1,32,harmonics=((0,1,0,0),)), '8 banks'), 'ninth bank is atomic')
    ok(p.wtuse('p',0,'bank',0) is None, 'wtuse returns None and retains zero')
    old = copy.deepcopy(p.__dict__); p.wtuse('p',0,'bank',0)
    ok(p.__dict__ == old, 'duplicate binding deduplicates')
    p.wtuse('p',0,'extra0',32768)
    ok(p.wtuses == {'p': {0: ('extra0',32768)}}, 'binding replacement has one operator row')
    for args, message in [(('missing',0,'bank',0),'defined'), (('p',2,'bank',0),'present'),
                          (('p',True,'bank',0),'present'), (('p',0,'missing',0),'declared'),
                          (('p',0,'bank',-1),'position'), (('p',0,'bank',32769),'position'),
                          (('p',0,'bank',True),'position')]:
        ok(refusal(p,lambda:p.wtuse(*args),message),'wtuse bound/refusal is atomic: '+message)
    p.pd('p',1,16384)
    ok(refusal(p,lambda:p.wtuse('p',1,'bank',0),'pd'),'PD and WT conflict by name')
    p.patch('wave',op(wave=1),op(),'')
    ok(refusal(p,lambda:p.wtuse('wave',0,'bank',0),'sine'),'WT needs sine')
    p.routes['p'] = ({'ops':1,'more':[],'carriers':1,'edges':[]},200)
    ok(refusal(p,lambda:p.wtuse('p',1,'bank',0),'present'),'route OPS limits present operators')
    p.routes['p'][0]['ksource'] = {0:(1,)}
    ok(refusal(p,lambda:p.wtuse('p',0,'bank',0),'ksource'),'KS asset metadata conflicts by name')
    p = piece(); p.patch('plain',op(),op(),'')
    p.wavetable('bank',1,32,harmonics=((0,1,0,0),));p.wtuse('p',0,'bank',0)
    for u in (0,16384,32768):
        ok(refusal(p,lambda:p.patch_morph('new','p','plain',u),'patch p: WT-bound'), 'WT morph endpoint/interior refuses atomically')
    ok(refusal(p,lambda:p.patch_square('new','plain','plain','plain','p',0,0),'patch p: WT-bound'), 'WT square validates unselected corner')
    p.patch_morph('plain-copy','plain','plain',0)
    ok('plain-copy' in p.patches,'unrelated bank does not block plain morph')
    p.patch('p',op(),op(),'replacement')
    ok(not p.wtuses and 'bank' in p.wavetables,'patch replacement clears binding but keeps bank')
    saw=harmonic_form('bl-saw');square=harmonic_form('bl-square');triangle=harmonic_form('bl-triangle')
    ok(len(saw)==1023 and saw[1]==(0,2,60,0) and saw[-1][1]==1023, 'saw uses exact integer harmonic limits and Decimal levels')
    ok(len(square)==512 and square[1]==(0,3,95,0), 'square uses odd harmonics')
    ok(len(triangle)==500 and triangle[:3]==((0,1,0,0),(0,3,191,32768),(0,5,280,0)) and
       triangle[-1][1]==999, 'triangle phases alternate and stop before level exceeds1200')
    with decimal.localcontext() as ctx:
        ctx.prec=2;ctx.rounding=decimal.ROUND_FLOOR
        ok(harmonic_form('bl-triangle')==triangle,'harmonic raw values ignore ambient Decimal context')
    for form in HARMONIC_FORMS:
        for length in (32,64,128,256,512,1024,2048):
            hs=harmonic_form(form,length=length)
            ok(all(1<=r[1]<length//2 and 0<=r[2]<=1200 for r in hs),'form obeys native length/level limits')
    for form,length in (('unknown',32),('bl-saw',31),('bl-square',True)):
        ok(refusal(p,lambda:harmonic_form(form,length=length),''),'invalid form/length refuses')
    p = piece(); p.wavetable('bl-saw',1,32,harmonics=((0,1,0,0),))
    ok(refusal(p,lambda:p.patch('p',*recipe('bl-saw')),'conflicting'),'recipe conflicting bank leaves all headers/events intact')
    p = piece(); handles=(p.ev,p.voices,p.marks);p.patch('p',*recipe('bl-saw'))
    ok((p.ev,p.voices,p.marks)==handles and all(a is b for a,b in zip((p.ev,p.voices,p.marks),handles)),
       'recipe bank staging preserves event/voice/mark handles')
    ok(p.wtuses=={'p':{0:('bl-saw',0)}} and len(p.wavetables)==1,'recipe installs bank and binding')
    try: recipe('bl-saw',c_wave=1)
    except ValueError as e: ok('sine' in str(e),'recipe delta cannot invalidate WT sine source')
    else: ok(False,'recipe delta WT conflict must refuse')
    music, profile = MUSIC, PROFILE
    with tempfile.TemporaryDirectory(prefix='wt-authoring-') as tmp:
        try:
            MUSIC=tmp
            p=piece();p.patch('p',*recipe('bl-triangle'));p.write()
            session=Path(p.files()[0]).read_text(encoding='utf-8')
            ok(session.count('wtbank ')==1 and session.count('wtuse p 0 bl-triangle 0')==1 and
               session.index('wtbank ')<session.index('wtuse ')<session.index('voice '), 'session bank order and explicit zero')
            for mirror in (False,True):
                q=keys_version(lambda:p,mirror=mirror)()
                ok(q.wavetables==p.wavetables and q.wtuses==p.wtuses,'keys and mirror preserve bank/binding')
                q.wtuses['p'].clear()
                ok(bool(p.wtuses['p']),'keys binding maps are detached')
            PROFILE='lin60';q=profile_version(lambda:copy.deepcopy(p),'lin30')()
            ok(q.wavetables==p.wavetables and q.wtuses==p.wtuses,'profile retains bank/binding')
            p.pds['p']={0:16384}
            ok(refusal(p,p.write,'pd') and Path(p.files()[0]).read_text(encoding='utf-8')==session,
               'late PD conflict refuses before touching record files')
            p.pds.clear();p.patches['p'][0][11]=1
            ok(refusal(p,p.write,'sine') and Path(p.files()[0]).read_text(encoding='utf-8')==session,
               'late wave conflict refuses before touching record files')
            src,dst=Path(tmp)/'in',Path(tmp)/'out'
            def expand(text):
                src.write_text(text,encoding='utf-8');expand_timeline(src,dst)
                return dst.read_text(encoding='utf-8')
            raw='# raw stays exact\nfs 48000\n'
            one=expand(raw+'recipe p bl-square\nrecipe q bl-square\n')
            ok(one.startswith(raw) and one.count('wtbank bl-square ')==1 and one.count('wtuse ')==2,
               'expansion emits shared generated bank once before use')
            bankrows='\n'.join(wtbank_rows('bl-square',(1,2048,square)))+'\n'
            text=expand(bankrows+'recipe p bl-square\n')
            ok(text.startswith(bankrows) and text.count('wtbank ')==1,'identical preceding raw harmonic bank satisfies generated name')
            for bad in (bankrows.replace('2048','1024',1)+'recipe p bl-square\n',
                        'recipe p bl-square\n'+bankrows,
                        'wtbank bl-square 1 2048 12\nrecipe p bl-square\n',
                        bankrows+'wtdata bl-square 0 0 0\nrecipe p bl-square\n',
                        'wtbank bl-square 1 2048 12\nrecipe p bl-square\n'+bankrows.split('\n',1)[1]):
                dst.write_text('destination sentinel',encoding='utf-8');src.write_text(bad,encoding='utf-8')
                try:expand_timeline(src,dst)
                except SystemExit as e:ok('wavetable bl-square:' in str(e) and dst.read_text()=='destination sentinel',
                                          'raw bank collision/order refusal preserves destination')
                else:ok(False,'raw bank collision must refuse')
            baseline=raw+'\n'.join(patch_headers('p',*recipe('epiano')))+'\n'
            ok(expand(raw+'recipe p epiano\n')==baseline,'expansion without banks keeps original bytes')
            for row in ('wtuse 0 unknown 0','wtuse 2 bl-saw 0','wtuse 0 bl-saw 32769',
                        'wtuse 0 bl-saw 0\nwtuse 0 bl-saw 0','wtuse 0 bl-saw 0\npd 0 16384'):
                src.write_text('recipe x\ncarrier\nmodulator\n'+row+'\nnote x\n',encoding='utf-8')
                try:load_recipes(src)
                except SystemExit as e:ok(bool(str(e)),'recipe WT refusal: '+row)
                else:ok(False,'recipe WT invalid row must refuse')
        finally:
            MUSIC,PROFILE=music,profile
    print('selftest-perf: ok harmonic banks: immutable definitions, atomic bindings, forms, copies, morph refusals and expansion preflight')


def _selftest_source_helpers(ok):
    """: source bindings and shared WT modulation; no native or WAV work."""
    import tempfile
    from pathlib import Path
    global MUSIC, PROFILE

    def piece():
        p = Piece('source-helper-test', 120, 4, ['explicit source test'])
        p.patch('p', op(level=20), op(index=0), 'test')
        p.stream('a', 'test'); p.add_voices('p', 1, '')
        p.length_ns = 100000000
        p.on_f('a', 0, 0, 220000, 32767, ''); p.off('a', 50000000, 0)
        return p

    def refuses(p, fn, text):
        before = copy.deepcopy(p.__dict__)
        try:
            fn()
        except ValueError as error:
            return text in str(error) and p.__dict__ == before
        return False

    p = piece()
    ok(p.ksource('p', 0, 32700, 7, 2048) is None, 'KS setter returns None')
    ok(p.ksources == {'p': {0: (32700, 7, 2048)}}, 'KS source uses explicit raw fields')
    for loss in (0, 32767):
        for seed in (0, 4294967295):
            for size in (64, 128, 256, 512, 1024, 2048, 4096, 8192):
                p.ksource('p', 0, loss, seed, size)
                ok(p.ksources['p'][0] == (loss, seed, size), 'KS native scalar endpoints')
    args = ('p', 0, 32700, 7, 2048)
    for index, values, message in ((0, ('missing', None, []), 'defined'),
                                  (1, (-1, 2, True, 0.0), 'present'),
                                  (2, (-1, 32768, True, 0.0), 'LOSS'),
                                  (3, (-1, 4294967296, True, 0.0), 'SEED'),
                                  (4, (0, 63, 8193, True, 64.0), 'BUFFER')):
        for value in values:
            changed = list(args); changed[index] = value
            ok(refuses(p, lambda:p.ksource(*changed), message), 'KS strict scalar refusal is atomic')
    ok(refuses(p, lambda:p.ksource('p', 0, 1, 1, 65), 'power of two'), 'KS buffer power refuses atomically')
    p.patch('plain', op(), op(), '')
    for amount in (0, 16384, 32768):
        ok(refuses(p, lambda:p.patch_morph('new', 'p', 'plain', amount), 'KS-bound'), 'KS morph source refuses at endpoints and interior')
    ok(refuses(p, lambda:p.patch_square('new', 'plain', 'plain', 'plain', 'p', 0, 0), 'KS-bound'),
       'KS unselected morph corner refuses')
    p.wavetable('bank', 1, 32, harmonics=((0, 1, 0, 0),))
    ok(refuses(p, lambda:p.wtuse('p', 0, 'bank', 0), 'ksource'), 'WT rejects KS source atomically')
    ok(refuses(p, lambda:p.pd('p', 0, 16384), 'ksource'), 'PD rejects KS source atomically')
    p.patch('p', op(), op(), 'replacement')
    ok(not p.ksources, 'patch replacement clears KS source')
    for kind in ('pd', 'wtuse', 'wave', 'feedback', 'index', 'incoming', 'sync'):
        p = piece()
        if kind == 'pd': p.pd('p', 0, 16384)
        elif kind == 'wtuse':
            p.wavetable('bank', 1, 32, harmonics=((0, 1, 0, 0),)); p.wtuse('p', 0, 'bank', 0)
        elif kind == 'wave': p.patches['p'][0][11] = 1
        elif kind == 'feedback': p.patches['p'][0][3] = 1
        elif kind == 'index': p.patches['p'][1][2] = 1
        else: p.routes['p'] = ({'ops': 2, 'carriers': 1, 'more': [],
                               'edges': [(1, 0, 256)] if kind == 'incoming' else [(0, 1, 256, 'sync')]}, 200)
        ok(refuses(p, lambda:p.ksource('p', 0, 32700, 7, 2048), kind), 'KS source conflict refuses atomically: '+kind)
    for amount in (-32768, 0, 32768):
        for i in range(4):
            ok(mod_values((0, 'ctl0', 'wt_pos%d' % i, amount, 0))[3] == amount, 'WT position target scalar endpoints')
    for target in ('wt_pos', 'wt_pos4', 'wt_pos00', 'wt_pos-1'):
        ok(refuses(piece(), lambda:mod_values((0, 'ctl0', target, 0)), 'target'), 'WT exact target spelling')
    for amount in (-32769, 32769, True, 0.0):
        ok(refuses(piece(), lambda:mod_values((0, 'ctl0', 'wt_pos0', amount)), 'AMOUNT'), 'WT amount bounds/types')
    music, profile = MUSIC, PROFILE
    with tempfile.TemporaryDirectory(prefix='source-helper-') as temporary:
        try:
            MUSIC = temporary
            p = piece(); p.ksource('p', 0, 32700, 7, 2048); p.ks('p', 1, 30); p.write()
            source = Path(p.files()[0]).read_bytes()
            ok(b'ksource p 0 32700 7 2048\n' in source and b'ks p' in source,
               'KS source and key scaling are separate headers')
            for mirror in (False, True):
                q = keys_version(lambda:p, mirror=mirror)()
                ok(q.ksources == p.ksources, 'keys and mirror preserve KS source')
                q.ksources['p'].clear()
                ok(bool(p.ksources['p']), 'keys KS bindings are detached')
            PROFILE = 'lin60'; q = profile_version(lambda:copy.deepcopy(p), 'lin30')()
            ok(q.ksources == p.ksources, 'profile preserves KS source')
            p.patches['p'][0][3] = 1
            saved = {Path(f).name: Path(f).read_bytes() for f in p.files()}
            ok(refuses(p, p.write, 'feedback') and saved == {Path(f).name: Path(f).read_bytes() for f in p.files()},
               'late KS conflict preserves all destination files')
            p = piece(); p.patch('p', *recipe('bl-saw'))
            p.macro('p', 'position', 0, [('wt_pos0', 32768, 0)])
            p.write(); source = Path(p.files()[0]).read_text()
            ok('mod p 0 ctl0 wt_pos0 32768 0\n' in source and source.count('wtbank ') == 1,
               'WT helper macro writes one bound bank')
            car, mod, comment, extra = recipe('bl-saw')
            extra = dict(extra, mod=copy.deepcopy(p.mods['p']), macros=copy.deepcopy(p.macros['p']))
            p.patch('p', car, mod, comment, extra); p.write()
            ok(p.mods['p'][0] == ('ctl0', 'wt_pos0', 32768, 0) and len(p.wavetables) == 1,
               'WT recipe macro retains binding validation context')
            source = Path(p.files()[0]).read_bytes(); p.wtuses.clear()
            saved = {Path(f).name: Path(f).read_bytes() for f in p.files()}
            ok(refuses(p, p.write, 'wavetable binding') and saved == {Path(f).name: Path(f).read_bytes() for f in p.files()},
               'missing WT modulation binding refuses before file changes')
            path = Path(temporary) / 'recipes.txt'
            path.write_text('recipe test\ncarrier\nmodulator\nwtuse 0 bl-saw 0\nmod 0 ctl0 wt_pos0 32768 0\nnote test\n')
            book = load_recipes(path)
            car, mod, comment, extra = book['test']
            p = piece(); p.patch('p', op(**car), op(**mod), comment, extra); p.write()
            ok(p.mods['p'][0] == ('ctl0', 'wt_pos0', 32768, 0), 'recipe parser shares WT modulation support')
        finally:
            MUSIC, PROFILE = music, profile
    print('selftest-perf: ok KS source and WT position: strict raw fields, bindings, copies, replacement, macros and atomic refusals')


def _selftest_neighbor_conditions(ok):
    """ PRE/NEI literal schedules, pass resets and preflight atomicity."""
    import copy
    def piece():
        p=Piece('conditions-test',120,4,[])
        p.patch('p',op(level=30),op(index=0),'')
        p.stream('a','');p.add_voices('p',2,'');p.length_ns=20_000_000_000
        return p
    def run(pattern='xxxx', **kw):
        p=piece()
        return p,p.steps('a',[0],pattern,0,1,60,release_ms=0,**kw)
    def gs(rows):return [r[4] for r in rows]
    for pre in (False,True):
        for neighbor in (False,True):
            ok([cond_holds(c,0,(),pre=pre,nei=neighbor) for c in ('pre','not-pre','nei')]
               ==[pre,not pre,neighbor],'PRE/NEI direct boolean truth table')
    for context in ({'pre':0},{'nei':1},{'pre':None},{'nei':[]}):
        try:cond_holds(None,0,(),**context)
        except ValueError:pass
        else:ok(False,'PRE/NEI rejects non-boolean context')
        ok(True,'PRE/NEI rejects non-boolean context')
    ok(gs(run(cond=['pre','not-pre','pre',None])[1])==[1,2,3], 'PRE initial false and conditional-chain history')
    ok(gs(run(cond=['first',None,'pre','not-pre'])[1])==[0,1,2], 'PRE unconditional step preserves conditional bit')
    ok(gs(run(pattern='x.xx',cond=['first',None,'pre','not-pre'])[1])==[0,2], 'PRE unconditional rest preserves conditional bit')
    ok(gs(run(pattern='x.xx',cond=['first','pre','pre','not-pre'])[1])==[0,3], 'PRE conditional rest records false')
    ok(gs(run(prob=[1,0,1,1],cond=['first','pre','pre','not-pre'])[1])==[0,3], 'PRE probability rejection records false')
    ok(gs(run(cond=['first','not-first','pre','not-pre'])[1])==[0,3], 'PRE condition rejection records false')
    ok(gs(run(cond=['pre','not-pre',None,'pre'],passes=2,pass0=7)[1])==[1,2,3,5,6,7], 'PRE resets each pass despite pass0 continuation')
    ok(gs(run(cond=['not-pre','pre','not-pre','pre'],ratchet=[2,3,1,1])[1])==[0,0,1,1,1], 'PRE ratchet records one played step')
    source=[(0,1,0,60,0),(2,3,0,61,0),(4,5,0,62,2),(6,7,0,63,6)]
    original=copy.deepcopy(source)
    ok(gs(run(cond='nei',nei=source,passes=2,pass0=9)[1])==[0,2,6], 'NEI matches global step with duplicate ratchets and pass0')
    ok(source==original,'NEI leaves source rows unchanged')
    ok(gs(run(cond='nei',nei=[])[1])==[], 'NEI explicit empty source rests')
    p=piece(); earlier=p.steps('a',[0],'x.x.',0,1,60,release_ms=0,ratchet=2)
    later=p.steps('a',[1],'xxxx',8,1,64,release_ms=0,cond='nei',nei=earlier)
    ok(gs(later)==[0,2] and later[0][0]==4_000_000_000, 'NEI earlier return matches logical step across different timestamps')
    invalid=[None,0,True,'abc',[(0,1,0,60)],[(0,1,0,60,0,1)],[(0,1,0,60,True)],
             [(0,1,0,60,1.0)],[(0,1,0,60,-1)],[(0,1,0,60,'0')]]
    for bad in invalid:
        p=piece();calls=[];original_rng=Piece.perf_rng
        def observed_rng(self,seed):
            calls.append(seed)
            return original_rng(self,seed)
        ring=p.perf_voices([0],None,0,'test');ring_before=copy.deepcopy(ring.__dict__)
        before=copy.deepcopy((p.ev,p.seq))
        Piece.perf_rng=observed_rng
        try:
            try:p.steps('a',ring,'xxxx',0,1,60,cond='nei',nei=bad,seed=11)
            except ValueError:pass
            else:ok(False,'NEI malformed source refused')
        finally:Piece.perf_rng=original_rng
        ok((p.ev,p.seq)==before and not calls and ring.__dict__==ring_before,
           'NEI malformed source atomic before RNG/ring publication')
    p,all_rows=run(pattern='xxxxxxxx',seed=17,prob=.5)
    q,selected=run(pattern='xxxxxxxx',seed=17,prob=.5,cond='nei',nei=[(0,1,0,60,g) for g in (0,2,4,6)])
    ok(gs(selected)==[g for g in gs(all_rows) if g in (0,2,4,6)], 'NEI preserves probability draws of rejected steps')
    draws=[];original_next=Rng.next
    def observed_next(self):
        draws.append(1)
        return original_next(self)
    Rng.next=observed_next
    try:
        p=piece();p.steps('a',[0],'....',0,1,60,cond='nei',nei=[],release_ms=0)
    finally:Rng.next=original_next
    ok(len(draws)==4,'PRE/NEI rests take exactly one draw per step')
    print('selftest-perf: ok PRE/NEI boolean context, conditional history, passes, ratchets, logical neighbors and atomic refusals')


def _selftest_spatial(ok):
    """ authoring boundaries; native correspondence has a separate probe."""
    import contextlib
    import io
    def piece(space='stereo', fs=48000, block=64):
        p = Piece('spatial-check', 120, 4, [], space=space, fs=fs, block=block)
        p.patch('pl', op(1, 30), op(index=0), 'spatial check')
        p.stream('a', 'first'); p.stream('b', 'second')
        p.add_voices('pl', 8, '')
        p.length_ns = 20_000_000_000
        return p
    def moves(p, stream='a'):
        return [(e[0], e[3], *map(int, e[4].split()[1:])) for e in p.ev[stream] if e[4].startswith('move ')]
    def positions(p):
        return [(e[3], *map(int, e[4].split()[1:])) for e in p.ev['a'] if e[4].startswith('pos ')]
    def reject(p, call, label):
        before = copy.deepcopy(p.__dict__)
        try:
            call()
        except (ComposeError, ValueError):
            ok(p.__dict__ == before, 'spatial atomic refusal '+label)
        else:
            ok(False, 'spatial must refuse '+label)
    with decimal.localcontext(decimal.Context(prec=50)):
        diagonal = int(((decimal.Decimal(1)/2).sqrt()*(1 << 30)).to_integral_value(rounding=decimal.ROUND_HALF_UP))
    ok(diagonal == 759250125, 'spatial sine diagonal independently derived')
    for shape, segments in [('tri', 2), ('sine', 8)]:
        for duration, cycles in [(1_000_000_000, Fraction(1)), (2_500_000_000, Fraction(5,2)), (10_000_000_000, Fraction(10))]:
            p=piece();p.autopan('a',0,0,duration,1000,1000,shape)
            rows=moves(p)
            ok(len(rows)==cycles*segments, 'autopan exact segment count '+shape+str(cycles))
            ok(rows[0][0]==0 and rows[-1][0]+rows[-1][-1]==duration, 'autopan exact span')
            ok(all(a[0]+a[-1]==b[0] for a,b in zip(rows,rows[1:])), 'autopan contiguous cumulative segments')
            ok(positions(p)==[(0,-1000,1000,0)], 'autopan missing geometry seeded once')
    p=piece();p.autopan('a',0,0,1_000_000_000,1000,1000)
    ok([r[2:5] for r in moves(p)]==[(1000,1000,0),(-1000,1000,0)], 'autopan triangle endpoint polarity')
    p=piece();p.autopan('a',0,0,1_000_000_000,1000,1000,'sine')
    ok([r[2] for r in moves(p)]==[-707,0,707,1000,707,0,-707,-1000], 'autopan eight sine endpoints')
    p=piece();p.autopan('a',0,0,625_000_000,1000,1000)
    ok(moves(p)==[(0,0,1000,1000,0,500_000_000),(500_000_000,0,500,1000,0,125_000_000)], 'autopan clipped final segment target and duration')
    p=piece();p.pos('a',0,0,100,200,300);p.autopan('a',0,0,250_000_000,1000,1000)
    ok(moves(p)==[(0,0,550,600,150,250_000_000)], 'autopan partial first segment uses actual existing anchor')
    p=piece();p.autopan('a',0,17,2_000_000_017,3000,1000)
    rows=moves(p)
    expected=[17+(j*1_000_000_000+3)//6 for j in range(13)]
    ok([r[0] for r in rows]+[rows[-1][0]+rows[-1][-1]]==expected, 'autopan nonintegral period has no accumulated drift')
    for a,b in [(1,2),(-1,2),(3,2),(-3,2),(2,3),(-2,3)]:
        ok(Piece._spatial_round(a,b)=={(1,2):1,(-1,2):-1,(3,2):2,(-3,2):-2,(2,3):1,(-2,3):-1}[(a,b)], 'spatial signed half rounding')
    for axis in 'xyz':
        p=piece();p.pos('a',0,0,100,200,300);p.pos_mod('a',0,0,1_000_000_000,'tri',1000,80,axis)
        expected=[]
        for value in [80,0,-80,0]:
            xyz=[100,200,300];xyz['xyz'.index(axis)]+=value;expected.append(tuple(xyz))
        ok([r[2:5] for r in moves(p)]==expected, 'pos_mod centered symmetric triangle '+axis)
    p=piece();p.pos_mod('a',0,0,1_000_000_000,'sine',1000,1000)
    ok([r[2] for r in moves(p)]==[707,1000,707,0,-707,-1000,-707,0], 'pos_mod sine from center toward positive')
    ok(positions(p)==[(0,0,1000,0)], 'pos_mod seeds native default')
    p=piece();p.pos_mod('a',0,11,111,[(0,0),(20,16384),(60,-16384),(100,0)],None,3,'z')
    ok(moves(p)==[(11,0,0,1000,2,20),(31,0,0,1000,-2,40),(71,0,0,1000,0,40)], 'pos_mod envelope signed ties and exact durations')
    for fs in (44100,48000):
        for block in (32,64,128):
            p=piece(fs=fs,block=block)
            p.pos('a',0,0,-100,1000,50)
            # Block boundaries 1, 11 and query6, deliberately off-grid times.
            ns=lambda sample:(sample*1_000_000_000+fs-1)//fs
            p.move('a',ns(1),0,ns(10*block+1)-ns(1),900,-1000,-50)
            query=ns(5*block+1)
            ok(p._spatial_anchor('a',0,query)==((400,0,0),True), 'spatial native-grid midpoint '+str((fs,block)))
            p.move('a',query,0,ns(9*block+1)-query,-800,1200,300)
            ok(p._spatial_anchor('a',0,ns(7*block+1))==((-200,600,150),True), 'spatial interrupted move native grid')
            p.pos('b',query,0,1,2,3)
            ok(p._spatial_anchor('a',0,query)==((400,0,0),True), 'later stream at insertion ns excluded')
            ok(p._spatial_anchor('b',0,query)==((1,2,3),True), 'same-stream prior pos wins at insertion ns')
            p.pos('a',query,0,7,8,9)
            ok(p._spatial_anchor('b',0,query)==((1,2,3),True), 'declared stream order outranks global append order')
    center=keys_pos(69)
    for n in range(1,9):
        p=piece();result=p.unison('a',list(range(n)),123,69,n,'25.5',1000)
        ps=positions(p);ons=[e for e in p.ev['a'] if e[1]==1]
        ok(result==tuple(range(n)) and len(ps)==len(ons)==n, 'unison explicit count/return '+str(n))
        ok([r[0] for r in ps]==list(range(n)), 'unison preserves voice order')
        ok(ps[0][1]==center[0]-(1000 if n>1 else 0) and ps[-1][1]==center[0]+(1000 if n>1 else 0), 'unison exact width endpoints')
        ok(all(a[1]<b[1] for a,b in zip(ps,ps[1:])), 'unison increasing left-to-right placements')
        detunes=[Fraction(0)] if n==1 else [-Fraction(51,4)+i*Fraction(51,2*(n-1)) for i in range(n)]
        ok([int(e[4].split()[1]) for e in ons]==[mhz_m(Fraction(69)+c/100) for c in detunes], 'unison one final exact pitch rounding')
        ok(all(e[0]==123 and e[4].endswith(' 24000') for e in ons), 'unison common onset/default velocity')
    for cents in [0,Fraction(51,2),decimal.Decimal('25.5'),'25.5',1200]:
        p=piece();p.unison('a',[0,1],0,69,2,cents,1,vel=32767)
        ok(len(p.ev['a'])==4, 'unison accepted exact cents form')
    q=piece();q.unison('a',[0],0,69,1,1200,1<<24)
    ok(positions(q)==[(0,*center)], 'single unison copy ignores width/detune')
    for kwargs in [{'rate_mhz':0},{'rate_mhz':50001},{'rate_mhz':True},{'rate_mhz':1.0},
                   {'width_mm':-1},{'width_mm':True},{'width_mm':(1<<24)+1},
                   {'depth_mm':-1},{'shape':'square'},{'t0':-1},{'t1':0},{'t0':True},
                   {'t1':float('nan')},{'t1':21_000_000_000},{'voice':8},{'voice':True},
                   {'stream':'missing'}]:
        p=piece();args=dict(stream='a',voice=0,t0=0,t1=1_000_000_000,rate_mhz=1000,width_mm=100)
        args.update(kwargs);reject(p,lambda:p.autopan(**args),'autopan '+str(kwargs))
    p=piece(space=None);reject(p,lambda:p.autopan('a',0,0,1_000_000_000,1000,1),'mono autopan')
    p=piece(space=None);reject(p,lambda:p.pos_mod('a',0,0,100,'tri',1000,1),'mono pos_mod')
    p=piece(space=None);reject(p,lambda:p.unison('a',[0],0,69,1,0,0),'mono unison')
    p=piece();p.add_voices('pl',57,'')
    reject(p,lambda:p.autopan('a',64,0,100,1000,1),'native voice64')
    p=piece();p.length_ns=2_000_000_000_000
    reject(p,lambda:p.autopan('a',0,0,p.length_ns,50000,1),'periodic segment cap')
    p=piece();p.fs=1;reject(p,lambda:p.autopan('a',0,0,100,1,1),'native fs')
    p=piece();p.block=1;reject(p,lambda:p.autopan('a',0,0,100,1,1),'native block')
    p=piece();p.length_ns=None;reject(p,lambda:p.autopan('a',0,0,10**15,1,1),'native end sample bound')
    for source,rate in [([],None),([(0,0)],None),([(0,0),(100,1)],1),([(1,0),(100,1)],None),
                        ([(0,1),(100,1)],None),([(0,0),(99,1)],None),([(0,0),(0,1),(100,1)],None),
                        ([(0,0),(100,32769)],None),([(0,0),(100,True)],None),([(0,0),(100,)],None),('random',1000)]:
        p=piece();reject(p,lambda:p.pos_mod('a',0,0,100,source,rate,1),'envelope/source')
    p=piece();reject(p,lambda:p.pos_mod('a',0,0,100,'tri',1000,1,'w'),'axis')
    p=piece();p.pos('a',0,0,1<<24,0,0)
    reject(p,lambda:p.pos_mod('a',0,0,100,[(0,0),(50,-1),(100,32768)],None,1),'late out-of-bounds envelope')
    for changes in [{'n':0},{'n':9},{'n':True},{'voices':[0,0]},{'voices':[0]},{'voices':[0,8]},
                    {'midi':-1},{'midi':128},{'midi':69.0},{'cents':True},{'cents':1.0},{'cents':'1/3'},
                    {'cents':'NaN'},{'cents':decimal.Decimal('Infinity')},{'cents':-1},{'cents':1201},
                    {'vel':-1},{'vel':32768},{'vel':True},{'width_mm':-1},{'t':True}]:
        p=piece();args=dict(stream='a',voices=[0,1],t=0,midi=69,n=2,cents=20,width_mm=100)
        args.update(changes);reject(p,lambda:p.unison(**args),'unison '+str(changes))
    p=piece();p.voices[1]=('different','')
    reject(p,lambda:p.unison('a',[0,1],0,69,2,20,100),'mixed patch voices')
    p=piece();reject(p,lambda:p.unison('a',[0,1],0,100,2,20,1<<24),'late unison coordinate overflow')
    for rate,duration,warns in [(1000,2_000_000_000,True),(10,200_000_000_000,False)]:
        p=piece();p.length_ns=duration+1;p.on_f('a',0,0,440000,24000,'')
        p.autopan('a',0,0,duration,rate,1000)
        out=io.StringIO()
        with contextlib.redirect_stdout(out):lint_speed(p)
        ok(('faster than' in out.getvalue())==warns, 'spatial unchanged fast/slow speed lint')
    print('selftest-perf: ok spatial helper timing, phases, geometry, exact unison and atomic refusals')


def _selftest_compensation(ok):
    """ shared carrier ownership/reset proof; native records are separate."""
    import contextlib
    import io
    import tempfile
    from pathlib import Path

    def piece():
        p = Piece('compensation-check', 120, 4, [], space='stereo')
        p.patch('p', op(1, 30), op(index=0), 'compensation check')
        p.stream('a', 'first'); p.stream('b', 'second')
        p.add_voices('p', 8, ''); p.length_ns = 2_000_000_000
        return p

    def start(p, n=2, t=0, **kw):
        return p.unison('a', list(range(n)), t, 69, n, 0, 0, compensate=True, **kw)

    def refuse(p, call, message):
        before = copy.deepcopy(p.__dict__)
        try:
            call()
        except ValueError as e:
            ok(message in str(e), 'compensation named refusal '+message)
            ok(p.__dict__ == before, 'compensation refusal preserves complete Piece state')
        else:
            ok(False, 'compensation must refuse '+message)

    def ctls(p, stream='a'):
        return [(e[0], e[3], e[4]) for e in p._compensation_events(stream) if e[4].startswith('ctl ')]

    literal_a = [0, 60, 95, 120, 140, 156, 169, 181]
    literal_c = [0, 1639, 2595, 3277, 3823, 4260, 4615, 4943]
    ok([unison_attenuation(n) for n in range(1,9)] == literal_a, 'compensation coefficient literal 200log10 N')
    ok([carrier_attenuation_ctl(a) for a in literal_a] == literal_c, 'compensation ceil ctl literals')
    with decimal.localcontext(decimal.Context(prec=3, rounding=decimal.ROUND_DOWN)):
        ok([unison_attenuation(n) for n in range(1,9)] == literal_a, 'compensation isolates ambient Decimal precision and rounding')
    for n in range(1,9):
        p=piece(); returned=start(p,n)
        ok(returned==tuple(range(n)), 'compensation preserves unison voice return')
        ok(p.mods=={'p': {0: ('ctl0','level0',1200,0)}}, 'compensation installs exactly one carrier slot')
        ok(ctls(p)==[(0,v,'ctl 0 %d' % literal_c[n-1]) for v in range(n)], 'compensation exact per-copy controls')
        ok(not any(e[4].startswith('ctl ') for e in p.ev['a']), 'compensation keeps raw event storage unchanged')
        view=p._compensation_events('a')
        ok(all(view[i-1][4]=='ctl 0 %d' % literal_c[n-1] for i,e in enumerate(view) if e[1]==1),
           'compensation ctl precedes every on')
    for value in (0,1,None,'true',[]):
        p=piece();refuse(p,lambda:p.unison('a',[0],0,69,1,0,0,compensate=value),'must be a bool')
    p,q=piece(),piece()
    p.unison('a',[0,1],0,69,2,20,100)
    q.unison('a',[0,1],0,69,2,20,100,compensate=False)
    ok(p.__dict__==q.__dict__ and not hasattr(p,'_unison_compensation'), 'compensation default False exact state identity')
    p=piece();voice_handle=p.voices;events_handle=p.ev['a'];mods_handle=p.mods
    p.on_f('a',1_000_000_000,0,440000,24000,'already future')
    p.on('a',0,0,'A4',24000);p.off('a',0,0)
    old_order=ordered_events(p.ev['a'])
    start(p,2,100_000_000)
    p.on_f('a',1_500_000_000,0,440000,24000,'later ordinary')
    p.unison('a',[0],1_600_000_000,69,1,0,0)
    ok(p.voices is voice_handle and p.ev['a'] is events_handle and p.mods is mods_handle,
       'compensation preserves existing mutable handles')
    ok(ctls(p)==[(0,0,'ctl 0 0'),(100_000_000,0,'ctl 0 1639'),(100_000_000,1,'ctl 0 1639'),
                  (1_000_000_000,0,'ctl 0 0'),(1_500_000_000,0,'ctl 0 0'),(1_600_000_000,0,'ctl 0 0')],
       'compensation ordinary zero resets cover earlier/future/later/False notes')
    ok([e for e in p._compensation_events('a') if e in old_order]==old_order,
       'compensation insertion preserves legacy off-before-on relative order')
    p.on_f('b',100_000_000,0,550000,24000,'later stream')
    ok(ctls(p,'b')==[(100_000_000,0,'ctl 0 0')], 'compensation same-voice later-stream on resets independently')
    before=copy.deepcopy(p.__dict__)
    ok(p._compensation_events('a')==p._compensation_events('a') and p.__dict__==before,
       'compensation event view is idempotent and read only')
    start(p,3,200_000_000);start(p,2,300_000_000)
    ok(p._unison_compensation['p']['maximum']==95 and p.mods=={'p':{0:('ctl0','level0',1200,0)}},
       'compensation repeated calls reuse slots and retain maximum')
    ok([v for t,_,v in ctls(p) if t in (200_000_000,300_000_000)]==['ctl 0 2595']*3+['ctl 0 1639']*2,
       'compensation separate calls retain their own attenuation')
    # Patch-wide controller ownership includes generated voices and unscheduled snapshots.
    for style in ('raw','snapshot','ctl'):
        p=piece();p.gen_clock();p.gen_lane(0,7,step_ticks=64,base_cents=6900,span_cents=0,velocity=24000,gate_ticks=32)
        if style=='raw':p.ev['b'].append((1_900_000_000,4,99,7,'ctl 0 0',''))
        elif style=='snapshot':p.snapshot('future',{7:{0:0}})
        else:p.ctl('b',1_900_000_000,7,0,0)
        start(p)
        ok(p._unison_compensation['p']['input']==1, 'compensation reserves generated-voice '+style+' ctl0')
        refuse(p,lambda:p.ctl('b',0,7,1,0),'ctl1 is reserved')
        refuse(p,lambda:p.snapshot('collision',{7:{1:0}}),'ctl1 is reserved')
        p.ev['b'].append((1_800_000_000,4,100,7,'ctl 1 0',''))
        refuse(p,p._compensation_validate,'owned ctl1 conflicts')
    p=piece();p.mod('p',0,'ctl3','pitch0',0,0);p.mod('p',7,'ctl0','index1',0,0)
    p.snapshot('reserved',{0:{1:0}});start(p)
    ok(p._unison_compensation['p']['input']==2 and p._unison_compensation['p']['rows']==((1,('ctl2','level0',1200,0)),),
       'compensation zero rows reserve slots/inputs and lowest free allocation is exact')
    del p.mods['p'][7];start(p,t=200_000_000)
    ok(p._unison_compensation['p']['input']==2, 'compensation never reallocates newly freed lower input')
    p=piece();p.macro('p','existing',0,[('pitch0',100,0)]);start(p)
    ok(p._unison_compensation['p']['input']==1, 'compensation respects named macro input')
    refuse(p,lambda:p.macro('p','new',1,[('pitch0',1)]),'collides')
    p=piece();p.patch('other',op(),op(),'');p.add_voices('other',1,'');p.ctl('b',0,8,0,0);start(p)
    ok(p._unison_compensation['p']['input']==0, 'compensation ignores unrelated patch ctl number')
    for use_slots in (False,True):
        p=piece()
        if use_slots:
            for i in range(8):p.mod('p',i,'vel','pitch0',0)
        else:
            for i in range(8):p.ctl('b',0,7,i,0)
        refuse(p,lambda:start(p,1),'free mod slots' if use_slots else 'free ctl input')
    p=piece();p.gen_clock();p.gen(15,'white',1);p.gen(5,'henon',2)
    p.mod('p',0,'gen.15.x','pitch0',50,0);p.mod('p',7,'gen.5.y','index1',128,3)
    p.gen_lane(0,7,pitch=15,step_ticks=64,base_cents=6900,span_cents=100,velocity=24000,gate_ticks=32)
    start(p)
    ok(p.mods['p']=={0:('gen.15.x','pitch0',50,0),7:('gen.5.y','index1',128,3),1:('ctl0','level0',1200,0)},
       'compensation preserves sparse generator bindings and separate ctl0 cache')
    for target in ('level0','lfo_level'):
        q=piece();q.gen_clock();q.gen(15,'white',1)
        if target=='lfo_level':q.lfo('p',0,1000,0,0,0,0,0)
        q.mod('p',7,'gen.15.x',target,1,0)
        refuse(q,lambda:start(q),'level')
    q=piece();q.gen_clock();q.gen(15,'white',1);q.mod('p',7,'gen.15.x','level0',0,0);start(q)
    ok(7 in q.mods['p'] and 0 in q.mods['p'], 'compensation reserves inert generator level slot')
    refuse(q,lambda:q.mod('p',6,'gen.15.x','ctl0',1,0),'mod target')
    p=piece();p.gen_clock();p.gen_lane(0,0,step_ticks=64,base_cents=6900,span_cents=0,velocity=24000,gate_ticks=32)
    refuse(p,lambda:start(p),'gen_lane reserved voice')
    for kind, text in [(4, 'on 440000 24000'), (1, 'off'), (4, 'ctl 0 0\non 440000 24000')]:
        p=piece();start(p);p.ev['b'].append((100,kind,99,7,text,''))
        refuse(p,p._compensation_validate,'raw event kind/text')
    p=piece();p.ev['b'].append((100,4,99,7,'  ctl 0 0 # raw whitespace',''));start(p)
    ok(p._unison_compensation['p']['input']==1, 'compensation raw native first token reserves whitespace-prefixed ctl')
    p.ev['b'].append((101,4,100,7,'  ctl 1 0 # raw whitespace',''))
    refuse(p,p._compensation_validate,'owned ctl1')
    # Shared  eligibility reads current headers, not recipe snapshots.
    for kind, action in (
        ('shape',lambda p:p.shape('p',0,'hard')),('amp',lambda p:p.amp('p',0,0,0,0)),
        ('filter',lambda p:p.filter('p',mode='lp',hz=1000,q=1)),
        ('formant',lambda p:p.formant('p',0,1000000,1000,0)),
        ('level0',lambda p:p.mod('p',7,'vel','level0',1,0)),
        ('LFO',lambda p:p.lfo('p',0,1000,0,0,0,1,0)),
    ):
        p=piece();action(p);refuse(p,lambda:start(p),kind)
    p=piece();p.kstype('p',0,'level');p.patches['p'][0][10]=1
    refuse(p,lambda:start(p),'key scaling')
    p=piece();p.kstype('p',0,'both');p.left_slopes['p']={0:1}
    refuse(p,lambda:start(p),'key scaling')
    p=piece();p.lfo('p',0,1000,0,0,0,10,0,0,3,0,3);start(p)
    ok(bool(p._unison_compensation),'compensation preserves explicit inactive LFO carrier mask')
    # Existing ownership, rows, on identity and original patch all remain binding.
    for change in ('owned-row','extra-source','topology','missing-on','duplicate-on','changed-on','unowned-remap','owned-remap','shape'):
        p=piece();start(p)
        if change=='owned-row':p.mods['p'][0]=('ctl0','level0',1199,0)
        elif change=='extra-source':p.mods['p'][7]=('ctl0','pitch0',1,0)
        elif change=='topology':p.routes['p']=({'ops':2,'carriers':3,'more':[],'edges':[]},300)
        elif change=='missing-on':p.ev['a'].pop(1)
        elif change=='duplicate-on':p.ev['a'].append(p.ev['a'][1])
        elif change=='changed-on':p.ev['a'][1]=(*p.ev['a'][1][:4],'on 450000 24000','')
        elif change in ('unowned-remap','owned-remap'):
            p.patch('other',op(1,30),op(index=0),'');p.add_voices('other',1,'')
            if change=='owned-remap':p.unison('a',[8],500_000_000,69,1,0,0,compensate=True)
            p.voices[0]=('other','')
        else:p.shape('p',0,'hard')
        refuse(p,lambda:start(p,t=1_000_000_000),'compensation')
        refuse(p,p._compensation_validate,'compensation')
    p=piece();start(p)
    refuse(p,lambda:p.patch_morph('m','p','p',0),'compensation-owned')
    refuse(p,lambda:p.patch_square('m','p','p','p','p',0,0),'compensation-owned')
    p=piece();refuse(p,lambda:p.unison('a',[0,1],0,100,2,0,1<<24,compensate=True),'coordinate')
    p=piece();p.patches['p'][0][1]=1140;start(p)
    refuse(p,lambda:start(p,3,100),'unclamped')
    p=piece();p.routes['p']=({'ops':2,'carriers':3,'more':[],'edges':[]},300)
    p.patches['p'][1][1]=310;start(p)
    p._compensation_validate({'p':1})
    refuse(p,lambda:p._compensation_validate({'p':0}),'unclamped')
    p.patches['p'][1][1]=1199
    refuse(p,lambda:p._compensation_validate({'p':400}),'unclamped')
    # Actual serialization, copies, and validation-before-publication without audio.
    with tempfile.TemporaryDirectory(prefix='compose-compensation-check-') as tmp:
        p=piece();p.dir=str(Path(tmp)/'record');start(p);p.off('a',200_000_000,0);p.off('a',200_000_000,1)
        p.write();saved={f.name:f.read_bytes() for f in Path(p.dir).iterdir()};state=copy.deepcopy(p.__dict__)
        p.write();ok(saved=={f.name:f.read_bytes() for f in Path(p.dir).iterdir()} and p.__dict__==state,
                     'compensation repeated writer bytes/state exact')
        rows=Path(p.dir,'a.tl').read_text().splitlines()
        ok(any('VOICE ctl' in row for row in rows),'compensation writer comments advertise emitted ctl grammar')
        raw=[line.split('#')[0].split() for line in rows if line and not line.startswith('#')]
        ok(all(raw[i-1][3:]==['ctl','0','1639'] for i,row in enumerate(raw) if row[3]=='on'),
           'compensation serialized ctl-before-on is literal')
        old_levels=globals()['mono_levels'];old_profile=globals()['PROFILE']
        try:
            globals()['mono_levels']=lambda _: {'p':300}
            k=keys_version(lambda:p)();k.dir=str(Path(tmp)/'keys');k.write()
            ok(k._unison_notes==p._unison_notes and k._unison_notes is not p._unison_notes and
               k._unison_compensation==p._unison_compensation and k._unison_compensation is not p._unison_compensation,
               'compensation keys copy private maps independently')
            ok(ctls(k)==ctls(p),'compensation keys copy emits each ctl exactly once')
            globals()['PROFILE']='lin45'
            profile=profile_version(lambda:copy.deepcopy(p),'lin30')()
            ok(ctls(profile)==ctls(p),'compensation profile preserves annotated on identities')
        finally:
            globals()['mono_levels']=old_levels;globals()['PROFILE']=old_profile
        p.shape('p',0,'hard');refuse(p,p.write,'shape')
        ok(saved=={f.name:f.read_bytes() for f in Path(p.dir).iterdir()}, 'compensation late header refusal preserves existing files')
        q=piece();q.dir=str(Path(tmp)/'absent');start(q);q.ev['b'].append((0,4,99,7,'ctl 0 0',''))
        refuse(q,q.write,'owned ctl0');ok(not Path(q.dir).exists(),'compensation raw-control refusal creates no destination')
        # First private pass is writable; second leveller pass violates B+a<=1200.
        saved_play,saved_out=globals()['play'],globals()['OUT'];calls=[]
        def fake_play(*args):
            calls.append(args)
            return 0,{'peak_dbfs':'20','clipped':'0','hash':'0000000000000001'},'PLAY setup-controlled level pass'
        try:
            globals()['play']=fake_play;globals()['OUT']=str(Path(tmp)/'outputs')
            Path(globals()['OUT']).mkdir();Path(globals()['OUT'],'sentinel').write_text('original')
            q=piece();q.patches['p'][0][1]=1000;q.dir=str(Path(tmp)/'destination');start(q)
            Path(q.dir).mkdir();Path(q.dir,'sentinel').write_text('original')
            dest=q.dir;output=globals()['OUT']
            with contextlib.redirect_stdout(io.StringIO()):
                refuse(q,lambda:finish(q),'unclamped')
            ok(len(calls)==1 and q.dir==dest and globals()['OUT']==output,
               'compensation late leveller refusal restores dir/output after exactly one private pass')
            ok(list(Path(dest).iterdir())==[Path(dest,'sentinel')] and Path(dest,'sentinel').read_text()=='original'
               and list(Path(output).iterdir())==[Path(output,'sentinel')],
               'compensation late leveller refusal preserves destination and output bytes')
        finally:
            globals()['play']=saved_play;globals()['OUT']=saved_out
    print('selftest-perf: ok unison compensation: shared proof, owned controllers, resets, sparse generators, copies and atomic publication')


def selftest_perf():
    """compose.py --selftest-perf: the performance helpers' checks , on pieces built
    in memory plus temporary writer checks, with no harness or renders. The drawing
    helpers remain guarded by corpus event identity. Prints a line a group; exits
    at the first failure."""
    import random
    from fractions import Fraction
    state = random.getstate()
    done = [0]
    B = 500_000_000                                         # a beat at 120 bpm
    W = 64_000_000                                          # the test pluck's busy window: ceil(1.25 x 50) + 1 ms

    def ok(cond, what):
        if not cond:
            sys.exit('selftest-perf: FAILED: %s' % what)
        done[0] += 1

    _selftest_neighbor_conditions(ok)
    _selftest_spatial(ok)
    _selftest_compensation(ok)
    _selftest_carson(ok)
    _selftest_wavetables(ok)
    _selftest_source_helpers(ok)
    _selftest_fmx(ok)

    def raises(exc, f, *a, **kw):
        try:
            f(*a, **kw)
        except exc as e:
            return str(e) or exc.__name__
        return ''

    def piece(**kw):
        p = Piece('perf-test', 120, 4, ['perf-test: the performance helpers\' checks'], **kw)
        p.patch('pl', op(1.0, 20, a=1, d=200, s=120, r=50), op(1.0, index=1.0, a=1, d=100, s=10, r=50), 'a test pluck')
        p.stream('a', 'the test stream')
        p.length_ns = p.t(9)
        return p

    def clean(p, rel):
        """no note-on on a voice still sounding, or within rel ns of its note-off"""
        free = {}
        for t, kind, seq, v, text, nm in sorted(p.ev['a']):
            if kind == 1:
                if free.get(v, -1) is None or free.get(v, -1) > t:
                    return False
                free[v] = None
            elif text == 'off':
                free[v] = t + rel
        return True

    # : snapshots are explicit per-voice ctl maps, never inferred live state.
    import copy
    q = piece(); v0, v1 = q.add_voices('pl', 2, '')
    values = {v1: {7: -32768, 1: 42}, v0: {0: -1}}
    q.snapshot('A', values)
    values[v1][7] = 123; values[v0][2] = 9
    ok(q.snapshots['A'] == {v1: {7: -32768, 1: 42}, v0: {0: -1}}, 'snapshot copies nested caller maps')
    q.recall('a', 100, 'A', 'recall')
    ok([(e[0], e[3], e[4], e[5]) for e in q.ev['a']] ==
       [(100, v0, 'ctl 0 -1', 'recall'), (100, v1, 'ctl 1 42', 'recall'),
        (100, v1, 'ctl 7 -32768', 'recall')], 'snapshot recall is exact and ordered by voice/input')
    q.snapshot('B', {v0: {0: 0}, v1: {1: 42, 7: 32768}})
    q.ev['a'].clear(); q.seq = 0
    q.snapshot_morph('a', 'A', 'B', 100, 260, steps=2)
    events = ordered_events(q.ev['a'])
    expected = []
    for j in range(17):
        expected.append((100 + 10*j, v0, 'ctl 0 %d' % (-1 if j < 8 else 0)))
        if j == 0: expected.append((100, v1, 'ctl 1 42'))
        expected.append((100 + 10*j, v1, 'ctl 7 %d' % (-32768 + 4096*j)))
    ok([(e[0], e[3], e[4]) for e in events] == expected,
       'snapshot morph has exact signed ramps, endpoints, ordering and unchanged-input count')
    for u in (0, 32768):
        chosen = q.snapshots['A' if u == 0 else 'B']
        blended = {v: {n: _morph_value(q.snapshots['A'][v][n], q.snapshots['B'][v][n], u)
                       for n in q.snapshots['A'][v]} for v in q.snapshots['A']}
        ok(blended == chosen, 'snapshot morph endpoint equals the complete recall map')
    ok([_morph_value(-1, 0, u) for u in (0, 16383, 16384, 32768)] == [-1, -1, 0, 0],
       'snapshot signed half tie rounds upward')
    q.ev['a'].clear(); q.seq = 0
    q.snapshot_morph('a', 'A', 'B', 0, 1, steps=1)
    ok([e[0] for e in q.ev['a'] if e[3] == v0] == [0]*8+[1],
       'snapshot morph keeps all points when integer times coincide')
    q.snapshot('C', {v0: {0: 7}})
    q.snapshot('D', {v0: {1: 7}, v1: {1: 42, 7: 32768}})
    for badcall in (lambda: q.recall([], 0, 'A'),
                    lambda: q.snapshot_morph([], 'A', 'B', 0, 10, steps=1),
                    lambda: q.recall('missing', 0, 'A'), lambda: q.recall('a', 0.0, 'A'),
                    lambda: q.recall('a', True, 'A'), lambda: q.recall('a', -1, 'A'),
                    lambda: q.recall('a', q.length_ns, 'A'), lambda: q.recall('a', 0, 'missing'),
                    lambda: q.snapshot_morph('a', 'A', 'C', 0, 10, steps=1),
                    lambda: q.snapshot_morph('a', 'A', 'D', 0, 10, steps=1),
                    lambda: q.snapshot_morph('a', 'A', 'B', 0, 0, steps=1),
                    lambda: q.snapshot_morph('a', 'A', 'B', 0, 10, steps=0),
                    lambda: q.snapshot_morph('a', 'A', 'B', 0, 10, steps=True),
                    lambda: q.snapshot_morph('a', 'A', 'B', 0, 10, steps=1.0),
                    lambda: q.snapshot_morph('a', 'A', 'B', 0, q.length_ns, steps=1)):
        before = copy.deepcopy(q.__dict__)
        ok(bool(raises(ValueError, badcall)) and q.__dict__ == before, 'snapshot refused call is atomic')
    for bad in ({True: {0: 0}}, {2: {0: 0}}, {0: {True: 0}}, {0: {8: 0}},
                {0: {0: True}}, {0: {0: 32769}}, {0: {0: -32769}}, {0: {0: 0.0}}, {0: []}, []):
        before = copy.deepcopy(q.snapshots)
        ok(bool(raises(ValueError, q.snapshot, 'invalid', bad)) and q.snapshots == before,
           'snapshot rejects malformed maps without publishing')
    ok('defined twice' in raises(ValueError, q.snapshot, 'A', {}), 'snapshot duplicate refuses by name')
    for name in ('', '  ', 'bad#name', 'bad\nname'):
        ok(bool(raises(ValueError, q.snapshot, name, {})), 'snapshot name follows macro display validation')
    for name in ('E', 'F', 'G', 'H'): q.snapshot(name, {})
    ok('ninth' in raises(ValueError, q.snapshot, 'ninth', {}) and len(q.snapshots) == 8,
       'snapshot ninth definition refuses by name')
    q.length_ns = None
    q.recall('a', 123, 'A')
    ok(q.ev['a'][-1][0] == 123, 'snapshot authoring permits an unknown final length')
    q.snapshots['A'][v1][7] = 32769
    before = copy.deepcopy(q.__dict__)
    ok(bool(raises(ValueError, q.recall, 'a', 0, 'A')) and q.__dict__ == before,
       'snapshot recall rechecks stored values before any event')
    q = piece(space='stereo'); v = q.add_voices('pl', 1, '')[0]
    q.pos('a', 0, v, 100, 1000, 0); positions = list(q.ev['a'])
    q.snapshot('place', {v: {0: 32768}}); q.recall('a', 1, 'place')
    ok(q.ev['a'][:1] == positions and q.ev['a'][-1][4] == 'ctl 0 32768',
       'snapshot recall leaves spatial position events unchanged')
    saved_mono_levels = globals()['mono_levels']
    try:
        globals()['mono_levels'] = lambda _: {'pl': q.patches['pl'][0][1]}
        for mirror in (False, True):
            keyed = keys_version(lambda: q, mirror=mirror)()
            ok(keyed.snapshots == q.snapshots and keyed.snapshots is not q.snapshots and
               keyed.snapshots['place'][v] is not q.snapshots['place'][v],
               'keys/mirror copies retain independent snapshot maps')
            keyed.recall('a', 2, 'place')
            ok(keyed.ev['a'][-1][4] == 'ctl 0 32768' and q.ev['a'][-1][0] == 1,
               'keys/mirror snapshot recall does not mutate its source piece')
    finally:
        globals()['mono_levels'] = saved_mono_levels
    print('selftest-perf: ok snapshots: copied maps, eight-name budget, exact recall/ramps, signed ties and atomic refusals')

    # /: raw field classes, exact written endpoints and detached publication.
    def morph_piece():
        q = piece()
        for name, side in (('A', 0), ('B', 1)):
            operators = [op(level=20, s=0), op(index=1, fixed=220+220*side),
                         op(level=24, wave=10+side), op(level=30, wave=side*5)]
            operators = [o + [0]*(16-len(o)) for o in operators]
            operators[0][0] += side*3
            operators[0][13] = side*440000
            operators[0][14:16] = [side*65535, side*2]
            operators[3][12] = side*7
            extra = {'ops': 4, 'carriers': 15, 'edges': [(1, 0, 256)], 'more': operators[2:],
                     'filter': ('hp' if side else 'lp', 1000000+side*2000000, 500+side*1000, 2 if side else 1),
                     'shape': (side*200, 'fold', 'pre') if side else (0, 'hard'),
                     'amp': (10+side*2, 20+side*4, 30+side*6, 40+side*8),
                     'lfo': (1, 3000, 2, 4, 6, 8, 10, 65535, 1, 2, 4, 2) if side else (0, 1000, 0, 0, 0, 0, 0),
                     'peg': (-101+side*201, 2+side*2, -3, 4, 5, 6, 7, 8, 2 if side else 1),
                     'fenv': (2+side*2, 4, 500, 6, -101+side*201, 1000, 261626),
                     'eg': {0: (2+side*2, 4+side*2, 100+side*2)},
                     'ks': {1: 2+side*2}, 'noise': {2: 3+side*4}, 'pd': {0: 8192+side*16384},
                     'vector': (side*32768, 32768-side*32768),
                     'formant': {0: (1000000+side*2000000, 500+side*1000, 20+side*40)},
                     'mod': {0: ('ctl0', 'pitch1', -101+side*201, side*5),
                             1: ('lfo', 'cutoff', 100+side*200, 3)},
                     'kstype': {1: ('both', '+', '-') if side else ('level', '-', '+')},
                     'fbtype': {1: ('presquare',) if side else ('pre',)}, 'egloop': {1: ()}}
            q.patch(name, *operators[:2], 'source '+name, extra)
        return q

    def morph_rows(q, name, substitute='result'):
        operators, comment, extra, _ = q._patch_morph_source(name)
        return patch_headers(substitute, *operators[:2], comment, extra)

    q = morph_piece()
    for u, source in ((0, 'A'), (32768, 'B')):
        old = copy.deepcopy(q.__dict__)
        q.patch_morph('end'+source, 'A', 'B', u)
        ok(morph_rows(q, 'end'+source) == morph_rows(q, source), 'patch morph exact fields/comments at '+source)
        ok(all(getattr(q, k)[source] == old[k][source] for k in ('patches', 'routes', 'lfos', 'mods')),
           'patch morph endpoint leaves source payload intact')
    for u in (16383, 16384):
        q.patch_morph('mid%d' % u, 'A', 'B', u)
        n = 'mid%d' % u
        side = int(u >= 16384)
        operators = q._macro_operators(n)
        ok([operators[0][13], operators[0][15], operators[2][11], operators[3][11:13]] ==
           [440000*side, side*2, 10+side, [side*5, side*7]], 'operator presence/wave/FREE/rate-scale switch exactly at16384')
        ok(operators[1][13] == (220000*(32768-u)+440000*u+16384)//32768,
           'two enabled fixed frequencies interpolate in raw mHz')
        ok(q.filters[n][0] == ('lp', 'hp')[side] and q.filters[n][-1] == (1, 2)[side] and
           q.shapes[n][1:] == (('hard',), ('fold', 'pre'))[side], 'filter mode/stages and shaper kind/pre select side')
        ok(q.lfos[n][0] == side and q.lfos[n][8:] == ((15,15,15,0), (1,2,4,2))[side] and
           q.pegs[n][8] == (1,2)[side], 'native LFO trailing defaults and discrete masks; PEG mask switches')
        ok(q.noise_shifts[n] == {2: (3,7)[side]} and q.kstypes[n] == q.kstypes[('A','B')[side]] and
           q.fbtypes[n] == q.fbtypes[('A','B')[side]] and q.egloops[n] == {1: ()}, 'indexed discrete extensions select side')
        expected = lambda a,b: (a*(32768-u)+b*u+16384)//32768
        ok(q.amps[n] == tuple(expected(a,b) for a,b in zip((10,20,30,40),(12,24,36,48))) and
           q.pds[n] == {0: expected(8192,24576)} and q.vectors[n] == (expected(0,32768),expected(32768,0)),
           'amp/PD/vector use raw integer interpolation')
        ok(q.formants[n] == {0: (expected(1000000,3000000),expected(500,1500),expected(20,60))} and
           q.egs[n] == {0: (expected(2,4),expected(4,6),expected(100,102))} and q.left_slopes[n] == {1: expected(2,4)},
           'formant and both EG/key-slope extensions interpolate')
        ok(q.mods[n][0] == ('ctl0','pitch1',expected(-101,100),side*5) and
           q.pegs[n][0] == expected(-101,100) and q.fenvs[n][4] == expected(-101,100),
           'signed PEG/FENV/mod fields round with the same half-up law')
    q = morph_piece()
    q.amps.pop('B'); q.formants['B'].pop(0); q.formants['B'][2] = (4000000,2000,80)
    for u, source in ((16383,'A'),(16384,'B')):
        q.patch_morph('optional%d' % u, 'A','B',u)
        n = 'optional%d' % u
        ok((n in q.amps) == (source in q.amps) and q.formants[n] == q.formants[source],
           'one-sided optional headers/indexed entries copy whole or stay absent')
    q = morph_piece()
    q.patches['A'][0][1] += 37
    q.filters['A'] = ('notch', 2345678, 900)  # explicit setter ownership, separate from route extra
    q.recipe_filters.discard('A')
    rawrows = patch_lines('result', *q.patches['A'], q.routes['A'][0], q.patches['A'][0][1], q.routes['A'][1])[0]
    q.patch_morph('shifted', 'A','B',0)
    ok(morph_rows(q,'shifted')[:4] == rawrows and q._macro_operators('shifted')[1][1] == q.patches['A'][1][1]+37,
       'endpoint reproduces writer carrier adjustment from stored routed base')
    ok(q.filters['shifted'] == ('notch',2345678,900) and 'shifted' not in q.recipe_filters,
       'endpoint uses live feature maps and preserves explicit filter ownership')
    q.patch('shifted', op(), op(), '')
    ok(q.filters['shifted'] == ('notch',2345678,900), 'later patch replacement still preserves explicit filter')
    q = piece()
    for n, fb in zip('ABCD',(0,0,0,2)):
        o = op(); o[3] = fb
        q.patch(n,o,op(),'corner '+n)
    for x,y,n in ((0,0,'A'),(32768,0,'B'),(0,32768,'C'),(32768,32768,'D')):
        dest = 'corner'+n
        q.patch_square(dest,'A','B','C','D',x,y)
        ok(morph_rows(q,dest) == morph_rows(q,n), 'square exact corner '+n)
    q.patch_square('xy','A','B','C','D',10000,20000)
    q.patch_square('yx','A','C','B','D',20000,10000)
    ok(q.patches['xy'][0][3] == 1 and q.patches['yx'][0][3] == 0,
       'square rounds AB/CD in x then y: hand-worked 0/0/0/2 gives1, reverse gives0')
    def morph_refusal(q, fn, word):
        old = copy.deepcopy(q.__dict__)
        ids = {k:id(v) for k,v in q.__dict__.items()}
        message = raises(ValueError, fn)
        return word in message and q.__dict__ == old and all(id(getattr(q,k)) == v for k,v in ids.items())
    for u in (-1,32769,True,0.0):
        q = morph_piece()
        ok(morph_refusal(q,lambda:q.patch_morph('bad','A','B',u),'integer'), 'bad morph coordinate refuses without mutation')
    for name in ('A','', 'bad name', 'x'*32):
        q = morph_piece()
        ok(morph_refusal(q,lambda:q.patch_morph(name,'A','B',0),'destination'), 'bad destination refuses without mutation')
    q = morph_piece()
    ok(morph_refusal(q,lambda:q.patch_morph('bad','missing','B',0),'defined'), 'undefined morph source refuses')
    for change in ('count','carriers','weight','order','unknown','uint','wave','bool','nyquist','pd'):
        q = morph_piece()
        if change == 'count':
            q.patch('B',op(),op(),'')
        elif change == 'carriers':
            q.routes['B'][0]['carriers'] = 7
            q.vectors.pop('B')
        elif change == 'weight':
            q.routes['B'][0]['edges'] = [(1,0,255)]
        elif change == 'order':
            q.routes['A'][0]['edges'] += [(3,2,256)]
            q.routes['B'][0]['edges'] = [(3,2,256),(1,0,256)]
        elif change == 'unknown':
            q.routes['B'][0]['wtuse'] = 'unimplemented'
        elif change in ('uint','wave','bool'):
            q.patches['B'][0][{'uint':0,'wave':11,'bool':1}[change]] = {'uint':1<<32,'wave':12,'bool':True}[change]
        elif change == 'nyquist':
            q.fs = 100
        else:
            q.patches['B'][0][11] = 1
        ok(morph_refusal(q,lambda:q.patch_morph('bad','A','B',0),''), 'morph source refuses '+change+' even at endpoint')
    q = morph_piece()
    q.routes['B'][0]['edges'] = [(1,0,256,'pm')]
    q.patch_morph('pm','A','B',32768)
    ok(morph_rows(q,'pm') == morph_rows(q,'B'), 'legacy PM triples and explicit PM have the same complete route')
    q = piece()
    q.patch('A',*recipe('warm-pad-x')); q.patch('B',*recipe('warm-pad-x'))
    q.patch_morph('same','A','B',16384)
    ok(q.macros['same'] == q.macros['B'] and q.macros['same'] is not q.macros['B'], 'selected macro definitions are independent')
    slot = next(iter(q.mods['B']))
    source,target,amount,shift = q.mods['B'][slot]
    q.mods['B'][slot] = (source,target,amount+2,shift)
    for spec in q.macros['B'].values():
        if slot in spec['slots']:
            targets = list(spec['targets']); j = spec['slots'].index(slot)
            targets[j] = (target,amount+2,shift); spec['targets'] = tuple(targets)
    ok(morph_refusal(q,lambda:q.patch_morph('bad','A','B',16384),'ownership'), 'interior macro-owned amount cannot silently change selected metadata')
    q = piece(); q.patch('A',op(),op(),''); q.patch('B',op(),op(),'')
    q.mods['A'] = {0: ('gen.0.x','pitch0',1,3)}
    ok(morph_refusal(q,lambda:q.patch_morph('bad','A','B',0),'gen_clock'), 'generator-bound source needs its bank context')
    q.gen_clock(); q.gen(0,'white',1)
    q.patch_morph('bank','A','B',0)
    ok(q.mods['bank'] == q.mods['A'] and q.generators[0][0] == 'white', 'generator context is retained without cloned source evaluation')
    q.mods['A'][0] = ('gen.0.y','pitch0',1,3)
    ok(morph_refusal(q,lambda:q.patch_morph('bad','A','B',0),'henon'), 'scalar generator y remains refused')
    q = piece(); q.patch('A',op(),op(),''); q.patch('B',op(),op(),'')
    for i in range(32-len(q.patches)):
        q.patch('p%d'%i,op(),op(),'')
    ok(morph_refusal(q,lambda:q.patch_morph('bad','A','B',0),'32-patch'), 'native patch cap refuses before publication')
    q = piece(); q.patch('A',op()[:9],op()[:9],'short'); q.patch('B',op(),op(),'long')
    q.patch_morph('short','A','B',0); q.patch_morph('middle','A','B',16384)
    ok(len(q.patches['short'][0]) == 9 and len(q.patches['middle'][0]) == 16 and
       morph_rows(q,'short') == morph_rows(q,'A'), 'short raw endpoints stay exact; interior uses native zero tails')
    q = piece(); q.patch('A',op(),op(),''); q.patch('B',op(),op(),'')
    for n,level in (('A',-1),('B',1)):
        q.peg(n,level,1,0,1,0,1,0,1)
        q.mod(n,0,'peg','pitch0',1)
    ok(morph_refusal(q,lambda:q.patch_morph('bad','A','B',16384),'nonzero PEG'),
       'valid endpoints whose PEG cancels to zero refuse the invalid interior source')
    q = morph_piece()
    ok(morph_refusal(q,lambda:q.patch_morph('_bad','A','B',0),'mod patch name'),
       'destination also obeys existing header writer token restrictions')
    for x,y in ((True,0),(0,32769)):
        ok(morph_refusal(q,lambda:q.patch_square('bad','A','B','A','B',x,y),'integer'),
           'square refuses invalid coordinates atomically')
    q.patch('C',op(),op(),'incompatible fourth corner')
    ok(morph_refusal(q,lambda:q.patch_square('bad','A','B','A','C',0,0),'identical'),
       'square validates even a currently unselected incompatible corner')
    print('selftest-perf: ok patch morphs: raw fields, exact endpoints, optional metadata, x-then-y corners and atomic refusals')

    # : host ctl-MSEG, with a literal independent time/value schedule.
    def mseg_piece(**kw):
        q = piece(**kw); v = q.add_voices('pl', 1, '')[0]
        q.mod('pl', 3, 'ctl2', 'pitch0', 1200, 0)
        return q, v

    def mseg_rows(q):
        return [(e[0], int(e[4].split()[2])) for e in q.ev['a'] if e[4].startswith('ctl ')]

    q, v = mseg_piece()
    q.ctl_mseg('a', v, 2, [(0, -32768), (80, 32768), (160, 0)], anchor=100)
    expected = [(100+10*k, -32768+8192*k) for k in range(9)]
    expected += [(180+10*k, 32768-4096*k) for k in range(1, 9)]
    ok(mseg_rows(q) == expected, 'ctl_mseg exact breakpoints, subdivisions and event count')
    q, v = mseg_piece()
    q.ctl_mseg('a', v, 2, [(0, -1), (1, 0), (2, 1)], anchor=5)
    ok(mseg_rows(q) == [(5,-1)]*4+[(5,0)]*4+[(6,0)]*4+[(6,1)]*4+[(7,1)],
       'ctl_mseg signed half ties and coincident-time order')
    q, v = mseg_piece(); q.ctl_mseg('a', v, 2, [(0, 42)], anchor=123)
    ok(mseg_rows(q) == [(123,42)], 'ctl_mseg single-point constant lane')
    points = [(0, 0), (80, 8000), (160, -8000), (240, 16000), (320, 0)]
    q, v = mseg_piece(); q.ctl_mseg('a', v, 2, points, anchor=100, loop=(1,3), release_at=500)
    expected = [(100+10*k,1000*k) for k in range(9)]
    for start in (180,340):
        if start == 340: expected.append((340,8000))
        expected += [(start+10*k,8000-2000*k) for k in range(1,9)]
        expected += [(start+80+10*k,-8000+3000*k) for k in range(1,9)]
    expected += [(500+10*k,16000-2000*k) for k in range(1,9)]
    ok(mseg_rows(q) == expected and len(expected) == 50,
       'ctl_mseg prefix, exact two-loop count, unequal seam and tail schedule')
    ok([x for x in mseg_rows(q) if x[0] == 500] == [(500,16000)],
       'ctl_mseg does not restart a loop at exact release')
    q, v = mseg_piece(); q.ctl_mseg('a', v, 2, points, anchor=100, loop=(1,3), release_at=265)
    expected = [(100+10*k,1000*k) for k in range(9)]
    expected += [(180+10*k,8000-2000*k) for k in range(1,9)]
    expected += [(265,-6500)]
    expected += list(zip(range(275,346,10),[-5687,-4875,-4062,-3250,-2437,-1625,-812,0]))
    ok(mseg_rows(q) == expected and len(expected) == 26,
       'ctl_mseg partial release never overruns; tail starts at released value')
    q, v = mseg_piece(); q.ctl_mseg('a', v, 2, [(0,-32768),(3,32768),(11,0)],
                                 anchor=0,loop=(0,1),release_at=1)
    ok(mseg_rows(q)[:4] == [(0,-32768),(0,-24576),(0,-16384),(1,-10924)] and
       len(mseg_rows(q)) == 12 and mseg_rows(q)[-1] == (9,0),
       'ctl_mseg partial release floors Q15 before signed interpolation')
    q, v = mseg_piece(); q.ctl_mseg('a', v, 2, points, anchor=100, loop=(1,3), release_at=180)
    ok(mseg_rows(q) == [(100+10*k,1000*k) for k in range(9)] +
       [(180+10*k,8000-1000*k) for k in range(1,9)],
       'ctl_mseg release at initial loop start selects i and preserves tail duration')
    q, v = mseg_piece(); q.ctl_mseg('a', v, 2, [(0,0),(8,8),(16,0)],
                                 anchor=0,loop=(0,2),release_at=32)
    ok(len(mseg_rows(q)) == 33 and [x for x in mseg_rows(q) if x[0] == 16] == [(16,0)] and
       mseg_rows(q)[-1] == (32,0), 'ctl_mseg equal loop endpoints share the seam and release has no tail')
    q, v = mseg_piece(); q.mod('pl',3,'ctl2','pitch0',0,0)
    q.ctl_mseg('a',v,2,[(0,7)],anchor=0)
    ok(mseg_rows(q) == [(0,7)], 'ctl_mseg explicit zero-amount modulation matrix row is a binding')
    q, v = mseg_piece(); q.mods.clear(); q.macro('pl','one target',2,[('pitch0',1200,0)])
    q.ctl_mseg('a',v,2,[(0,9)],anchor=0)
    ok(mseg_rows(q) == [(0,9)] and len(q.mods['pl']) == 1, 'ctl_mseg reuses a validated one-slot macro without allocation')
    q, v = mseg_piece(space='stereo'); q.pos('a',0,v,100,1000,0); positions = list(q.ev['a'])
    q.ctl_mseg('a',v,2,[(0,0),(80,100)],anchor=0)
    ok(q.ev['a'][:1] == positions, 'ctl_mseg leaves spatial events unchanged')
    q, v = mseg_piece(); q.ctl('a',0,v,2,-123)
    bad_points = ([], [(1,0)], [(0,0),(0,1)], [(0,0),(2,1),(1,2)], [(False,0)],
                  [(0,True)], [(0,0.0)], [(0,-32769)], [(0,32769)], [(0,0),(1.0,1)],
                  [(0,0),(1,1,2)], [(0,0),(-1,1)], 'points')
    for pts in bad_points:
        before = copy.deepcopy(q.__dict__)
        ok(bool(raises(ValueError,q.ctl_mseg,'a',v,2,pts,anchor=0)) and q.__dict__ == before,
           'ctl_mseg malformed or falling points refuse atomically')
    for kw in ({'anchor':True},{'anchor':-1},{'anchor':0.0},{'anchor':0,'release_at':3},
               {'anchor':0,'loop':(0,1)}, {'anchor':0,'loop':(0,1),'release_at':True},
               {'anchor':10,'loop':(0,1),'release_at':9},
               {'anchor':0,'loop':(1,1),'release_at':8}, {'anchor':0,'loop':(0,2),'release_at':8},
               {'anchor':0,'loop':(False,1),'release_at':8}, {'anchor':0,'loop':(0,),'release_at':8},
               {'anchor':0,'loop':(1,0),'release_at':8}, {'anchor':q.length_ns-8}):
        before = copy.deepcopy(q.__dict__)
        ok(bool(raises(ValueError,q.ctl_mseg,'a',v,2,[(0,0),(8,8)],**kw)) and q.__dict__ == before,
           'ctl_mseg malformed timing/loop or record endpoint refuses atomically')
    for s, voice, n in (([],v,2),('missing',v,2),('a',True,2),('a',v+1,2),('a',v,True),('a',v,8)):
        before = copy.deepcopy(q.__dict__)
        ok(bool(raises(ValueError,q.ctl_mseg,s,voice,n,[(0,0)],anchor=0)) and q.__dict__ == before,
           'ctl_mseg invalid stream/voice/input refuses atomically')
    q, v = mseg_piece()
    for count in (0,2):
        q.mods['pl'] = {} if count == 0 else {0:('ctl2','pitch0',1,0),1:('ctl2','pitch1',1,0)}
        before = copy.deepcopy(q.__dict__)
        err = raises(ValueError,q.ctl_mseg,'a',v,2,[(0,0)],anchor=0)
        ok('patch pl ctl2' in err and 'found %d'%count in err and q.__dict__ == before,
           'ctl_mseg missing/multiple modulation matrix bindings refuse by name and count')
    q, v = mseg_piece(); q.mods['pl'][3] = ('ctl2','cutoff',0,0)
    before = copy.deepcopy(q.__dict__)
    ok('filter' in raises(ValueError,q.ctl_mseg,'a',v,2,[(0,0)],anchor=0) and q.__dict__ == before,
       'ctl_mseg validates zero-amount target prerequisites before events')
    q, v = mseg_piece(); q.mods.clear(); q.macro('pl','owned',2,[('pitch0',1200,0)])
    q.mods['pl'][0] = ('ctl2','pitch0',1199,0); before = copy.deepcopy(q.__dict__)
    ok('ownership' in raises(ValueError,q.ctl_mseg,'a',v,2,[(0,0)],anchor=0) and q.__dict__ == before,
       'ctl_mseg revalidates macro ownership atomically')
    q, v = mseg_piece(); q.length_ns = 10; before = copy.deepcopy(q.__dict__)
    ok('outside' in raises(ValueError,q.ctl_mseg,'a',v,2,[(0,0),(4,4),(8,0)],
                          anchor=0,loop=(0,1),release_at=6) and q.__dict__ == before,
       'ctl_mseg validates the full release tail against known record length')
    q.length_ns = None; q.ctl_mseg('a',v,2,[(0,0),(8,8)],anchor=123)
    ok(mseg_rows(q)[-1] == (131,8), 'ctl_mseg permits authoring before final record length')
    print('selftest-perf: ok ctl_mseg: exact breakpoints/loops/releases/tails, one-slot binding and atomic refusals')

    # F50: direct specification checks of the legacy authoring helpers.
    pool = Pool([7, 3, 5], 0)
    selected = [pool.get(on, off) for on, off in
                ((0, 20), (0, 10), (0, 30), (5, 50), (30, 60), (30, 70), (30, 80))]
    ok(selected == [3, 5, 7, 5, 3, 7, 5], 'Pool ties, earliest-free stealing and longest-free reuse')
    ok(pool.steals == 2 and pool.busy == {7: 70, 3: 60, 5: 80}, 'Pool counts only actual steals')
    pool = Pool([1], 2)
    ok(pool.get(0, 10) == pool.get(2_000_010, 2_000_020) == 1 and
       pool.steals == 0 and pool.busy[1] == 4_000_020, 'Pool release boundary is free at equality')
    print('selftest-perf: ok Pool: free duration, tie order, steal order/count and release boundary')

    for bend in (False, True):
        q = piece(); v = q.add_voices('pl', 1, '')[0]
        seen = []
        def pitch(t):
            seen.append(t)
            return 69 + Fraction(t, 2000)
        q.curve('a', v, 100, 150, pitch, 24000, 10, cents=1, bend=bend)
        ok(seen == [0, 10, 20, 30, 40], 'curve samples each step and excludes the end')
        ok([e[0] for e in q.ev['a']] == [100, 120, 140], 'curve accumulates sub-cent steps and admits equality')
        ok([e[4].split()[0] for e in q.ev['a']] == ['on'] + (['pitch'] if bend else ['on']) * 2,
           'curve bend changes pitch without retrigger; ordinary curve retriggers')
        ok([int(e[4].split()[1]) for e in q.ev['a']] ==
           [mhz_m(69), mhz_m(Fraction(6901, 100)), mhz_m(Fraction(3451, 50))], 'curve writes specified pitches')
    print('selftest-perf: ok curve: sample boundary, cents threshold and bend event kinds')

    q = piece(); q.tempo(lambda k: (Fraction(5, 2), Fraction(7, 2), Fraction(9, 2))[k], beats=3)
    ok(q.cum == [0, 2, 6, 10], 'legacy tempo rounds each beat half-even before accumulation')
    ok([q.at_beat(k) for k in (Fraction(1, 4), Fraction(3, 4), Fraction(3, 2), 3)] == [0, 2, 4, 10],
       'legacy at_beat interpolates rounded cumulative boundaries')
    ok([q.at_beat(k) for k in (Fraction(-1, 4), Fraction(-3, 4), -2)] == [0, -2, -4],
       'legacy negative at_beat extrapolates the first rounded beat')
    print('selftest-perf: ok legacy tempo: half-even beat rounding, interpolation and negative beats')

    import contextlib
    import io
    import tempfile
    with tempfile.TemporaryDirectory(prefix='perf-header-') as tmp:
        q = piece(); q.field('amb1'); q.dir = tmp
        v, reserved = q.add_voices('pl', 2, '')
        q.filter('pl', 'lp', 1000, 1); q.fx(delay=(300000, 12000, 4000))
        q.gen_clock(); q.gen(0, 'white', 42)
        q.gen_lane(0, reserved, pitch=0, step_ticks=64, base_cents=6000,
                   span_cents=1200, velocity=24000, gate_ticks=32)
        actions = [
            ('pitch FREQ_MILLIHZ', lambda: q.pitch_f('a', 0, v, 440000, '')),
            ('yaw MILLIDEGREES', lambda: q.yaw('a', 0, 0)),
            ('pos X_MM Y_MM Z_MM', lambda: q.pos('a', 0, v, 0, 1000)),
            ('move X_MM Y_MM Z_MM DUR_NS', lambda: q.move('a', 0, v, 10, 1, 1000)),
            ('send BUS LEVEL_Q15', lambda: q.send('a', 0, v, 'delay', 10000)),
            ('filter CUTOFF_MILLIHZ Q_MILLI', lambda: q.cutoff('a', 0, v, 900, 1)),
            ('ctl N VALUE_Q15', lambda: q.ctl('a', 0, v, 0, 1000)),
            ('gen ID PARAM VALUE', lambda: q.gen_set('a', 0, 0, 'reset', 17)),
            ('lane ID enable VALUE', lambda: q.lane_enable('a', 0, 0, 0)),
        ]
        def header():
            q.write()
            with open(q.files()[1], encoding='utf-8') as f:
                return ''.join(line for line in f if line.startswith(('# Lines:', '# or')))
        ok(header() == '# Lines: T_NS STREAM VOICE on FREQ_MILLIHZ VELOCITY_Q15, or T_NS STREAM VOICE off.\n',
           'writer baseline header names on and off exactly')
        for word, action in actions:
            q.ev['a'].clear(); action(); text = header()
            prefix = 'T_NS STREAM ' + ('' if word.startswith(('gen ', 'lane ')) else '0 ' if word.startswith('yaw ') else 'VOICE ')
            ok(text == '# Lines: T_NS STREAM VOICE on FREQ_MILLIHZ VELOCITY_Q15, ' + prefix + word +
               ',\n# or T_NS STREAM VOICE off.\n', 'writer header names only its ' + word.split()[0] + ' event')
        q.ev['a'].clear()
        for _, action in actions: action()
        text = header()
        positions = [text.index(word) for word, _ in actions]
        ok(positions == sorted(positions) and text.count('T_NS STREAM') == 11,
           'writer header includes every event kind once in canonical order')
    print('selftest-perf: ok writer headers: base, all nine optional event kinds and combined order')

    saved_play = globals()['play']
    def scripted_finish(peaks, expected, failure=False):
        q = piece()
        writes, sessions, calls = [], [], []
        q.write = lambda levels: writes.append(dict(levels))
        q.write_session = lambda levels, hash_: sessions.append((dict(levels), hash_))
        q.files = lambda: ['scripted-input.tl']
        def scripted(files, wav=None, merged=None):
            i = len(calls); calls.append((files, wav, merged))
            peak, clips = peaks[min(i, len(peaks) - 1)]
            kv = dict(peak_dbfs=str(peak), clipped=str(clips), hash='0123456789abcdef')
            return 0, kv, 'PLAY ' + ' '.join(k+'='+v for k, v in kv.items())
        globals()['play'] = scripted
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                if failure:
                    message = raises(SystemExit, finish, q)
                else:
                    finish(q); message = ''
        finally:
            globals()['play'] = saved_play
        ok([row['pl'] for row in writes] == expected, 'finish level sequence: %s' % expected)
        ok(len(calls) == len(peaks) + (0 if failure else 2), 'finish expected level passes then record/replay')
        ok((message == 'perf-test: levels did not settle' and not sessions) if failure else
           (not message and sessions == [({'pl': expected[-1]}, '0123456789abcdef')]),
           'finish refuses after eight passes or writes only the settled level/hash')
    scripted_finish([(-6, 0), (-2.75, 0), (-1.5, 0)], [200, 155, 143])
    scripted_finish([(0, 1), (-1.5, 0)], [200, 215])
    scripted_finish([(-31.5, 0), (-1.5, 0)], [200, 0])
    scripted_finish([(-9, 0)] * 8, [200, 125, 50, 0, 0, 0, 0, 0], failure=True)
    print('selftest-perf: ok finish: pass count, half-even deltas, clipping15, level floor and eight-pass refusal')

    # regression: bounded host macros share the raw modulation matrix slots and inputs.
    import copy
    p = piece()
    v = p.add_voices('pl', 1, '')[0]
    p.macro('pl', 'Brightness', 2, [('index1', 2048, 0), ('pitch1', 1200)])
    p.macro_set('a', 0, v, 'Brightness', 127)
    raw = piece()
    rv = raw.add_voices('pl', 1, '')[0]
    raw.mod('pl', 0, 'ctl2', 'index1', 2048, 0)
    raw.mod('pl', 1, 'ctl2', 'pitch1', 1200)
    raw.ctl('a', 0, rv, 2, 32768)
    ok(p.mods == raw.mods and p.ev == raw.ev and
       [mod_row('pl', k, *x) for k, x in p.mods['pl'].items()] ==
       ['mod pl 0 ctl2 index1 2048 0', 'mod pl 1 ctl2 pitch1 1200 3'],
       'macro expansion equals hand-written mod and ctl bytes')
    for kw in (dict(knob=128), dict(s='bad name'), dict(v=99), dict(name='missing')):
        before = copy.deepcopy(p.__dict__)
        call = dict(s='a', t=0, v=v, name='Brightness', knob=64)
        call.update(kw)
        ok(raises(ValueError, p.macro_set, **call) and p.__dict__ == before, 'macro_set refusal leaves events and sequence unchanged')
    ok([macro_knob(k) for k in (0, 1, 64, 127)] == [0, 258, 16513, 32768] and
       [macro_knob(k, True) for k in (0, 1, 64, 127)] == [-32768, -32256, 0, 32256],
       'macro integer maps pin endpoints, half-up interior and asymmetric bipolar127')
    ok(all(macro_knob(k) == (k*32768+63)//127 for k in range(128)), 'all unipolar knob values exact')
    ok(macro_knob(127, curve=lambda q: q*q//32768) == 32768 and
       macro_knob(64, True, lambda q: q) == 0, 'one host curve receives integer Q15')
    for k in (-1, 128, 1.0, True, '1'):
        ok(raises(ValueError, macro_knob, k), 'macro knob refuses %r' % k)
    for curve in (5, lambda q: 1.0, lambda q: True, lambda q: -1, lambda q: 32769):
        ok(raises(ValueError, macro_knob, 0, curve=curve), 'macro curve type/range refusal')
    def untouched(p, call, word):
        before = copy.deepcopy(p.__dict__)
        mods, macros, events = p.mods, p.macros, p.ev
        message = raises(ValueError, call)
        return word in message and p.__dict__ == before and p.mods is mods and p.macros is macros and p.ev is events
    ok(untouched(p, lambda: p.macro('pl', 'Collision', 2, [('pitch0', 3)]), 'Collision'),
       'macro input collision is named and checked before any mutation')
    ok(untouched(p, lambda: p.macro('pl', 'Brightness', 3, [('pitch0', 3)]), 'already'),
       'duplicate macro name refuses atomically')
    ok(untouched(p, lambda: p.mod('pl', 0, 'ctl3', 'index1', 1), 'ownership'),
       'raw overwrite cannot detach an owned macro slot')
    ok(untouched(p, lambda: p.mod('pl', 7, 'ctl2', 'pitch0', 1), 'collides'),
       'raw controller route cannot collide with a macro input')
    full = piece()
    for slot in range(7):
        full.mod('pl', slot, 'vel', 'pitch0', 1)
    full.macro('pl', 'Eighth', 7, [('pitch0', 1)])
    ok(len(full.mods['pl']) == 8, 'seven other routes and one macro use exactly eight slots')
    ok(untouched(full, lambda: full.macro('pl', 'Ninth', 6, [('pitch0', 1)]), 'Ninth'),
       'ninth slot is refused by name without dropping targets')
    rawinputs = piece()
    rawinputs.mod('pl', 0, 'ctl6', 'pitch0', 1)
    rawinputs.mod('pl', 1, 'ctl6', 'index1', 1)
    ok(untouched(rawinputs, lambda: rawinputs.macro('pl', 'Shared', 6, [('pitch0', 1)]), 'ctl6'),
       'all raw slots reserve their shared input')
    rawinputs.macro('pl', 'Separate', 5, [('pitch0', 1)])
    ok(len(rawinputs.mods['pl']) == 3, 'existing shared raw input remains valid on a disjoint addition')
    labels = piece()
    labels.mod('pl', 0, 'ctl4', 'pitch0', 1)
    labels.macro('pl', 'raw slot 0', 3, [('pitch0', 1)])
    labels.mod('pl', 2, 'ctl4', 'index1', 1)
    ok(labels.mods['pl'] == {0: ('ctl4', 'pitch0', 1, 3),
                            1: ('ctl3', 'pitch0', 1, 3),
                            2: ('ctl4', 'index1', 1, 3)} and
       labels.macros['pl']['raw slot 0']['input'] == 3,
       'macro display name matching a raw label does not claim that raw input')
    ok(untouched(labels, lambda: labels.macro('pl', 'Taken raw input', 4, [('pitch0', 1)]), 'ctl4'),
       'raw input stays reserved despite a matching macro display name')
    ok(untouched(labels, lambda: labels.mod('pl', 3, 'ctl3', 'pitch0', 1), 'collides'),
       'actual macro input stays reserved despite a raw-label display name')
    for name, n, targets in [('Bad input', 8, [('pitch0', 1)]), ('Bad target', 0, [('level1', 1)]),
                              ('Empty', 0, []), ('Too many', 0, [('pitch0', 1)]*9),
                              ('Partial', 0, [('pitch0', 1), ('cutoff', 1)]),
                              ('Bad amount', 0, [('pitch0', 4801)]), ('Bad shift', 0, [('pitch0', 1, 16)])]:
        q = piece()
        ok(untouched(q, lambda: q.macro('pl', name, n, targets), ''), 'macro preflight '+name)
    q = piece()
    q.patches['pl'][1] = op(index=1, fixed=440)
    ok(untouched(q, lambda: q.template('pl', 'HARM'), 'fixed-frequency'), 'fixed pitch macro refused')
    q.patches['pl'][0] = op(level=20, fixed=220)
    ok(untouched(q, lambda: q.template('pl', 'EXPR'), 'no relative operator'),
       'EXPR refuses an LFO pitch target ignored by every fixed operator')
    q = piece()
    ok(untouched(q, lambda: q.template('pl', 'DTUN'), 'exactly two'), 'DTUN refuses one modulator')
    q = piece()
    q.template('pl', 'HARM')
    ok(untouched(q, lambda: q.template('pl', 'EXPR'), 'ctl0'), 'HARM/EXPR collision before LFO or slot mutation')
    q = piece()
    q.template('pl', 'Timbre')
    q.template('pl', 'EXPR')
    ok(len(q.mods['pl']) == 4 and q.lfos['pl'][:7] == (0, 5000, 0, 0, 0, 0, 0),
       'Timbre plus EXPR fits; missing LFO added with zero direct depths')
    q = piece()
    q.lfo('pl', 0, 3000, 0, 0, 0, 0, 0)
    ok(untouched(q, lambda: q.template('pl', 'EXPR'), 'existing LFO'), 'EXPR cannot overwrite an LFO')
    four = {'ops': 4, 'carriers': 5, 'edges': [(1, 0, 256), (3, 2, 256)],
            'more': [op(level=20), op(ratio=3, index=1)]}
    q = piece()
    q.patch('four', op(level=20), op(index=1), '', four)
    for name in ('HARM', 'DTUN', 'MIX', 'Timbre'):
        q.template('four', name)
    ok(len(q.mods['four']) == 8 and [x[2] for x in q.mods['four'].values()] ==
       [1200, 1200, 25, -25, 120, -120, 2048, 2048], 'four templates exactly fill eight slots')
    q = piece()
    q.patch('vec', op(level=20), op(level=20), '', dict(four, carriers=15, edges=[], vector=(0, 0)))
    q.template('vec', 'MIX')
    ok(q.mods['vec'] == {0: ('ctl2', 'vector_x', 32768, 3)}, 'MIX chooses the explicit vector case')
    q = piece()
    q.filter('pl', 'lp', 1000, 0.7)
    q.template('pl', 'EXPR')
    ok(q.mods['pl'][2] == ('ctl4', 'cutoff', 1200, 3), 'EXPR filter case selects cutoff')
    a = recipe('warm-pad-x')
    ok(a[:2] == recipe('warm-pad')[:2] and len(a[3]['mod']) == 3 and
       {v[0] for v in a[3]['mod'].values()} == {'ctl0', 'ctl1', 'ctl4'}, 'warm-pad-x appends EXPR without changing its operators')
    q = piece()
    q.patch('expr', *a)
    ok(untouched(q, lambda: q.patch('bad name', *a), 'token patch'), 'macro recipe refuses an invalid patch token atomically')
    ok(set(q.macros['expr']) == {'Wheel', 'Pressure', 'Slide'}, 'recipe macro metadata reaches Piece')
    q.patch('expr', *recipe('warm-pad'))
    ok('expr' not in q.macros and 'expr' not in q.mods and 'expr' not in q.lfos, 'replacement clears macro ownership and LFO')
    muted = recipe('warm-pad', mute=(1,))
    ok(muted[1][2:4] == [0, 0] and muted[0] == recipe('warm-pad')[0], 'muted PM operator index and feedback zero')
    ok('muted' in raises(TypeError, recipe, 'warm-pad', mute=(1,), m_ratio=1), 'ignored refuses a tweak on muted operator')
    ok('no carrier' in raises(TypeError, recipe, 'warm-pad', mute=(0,)), 'muting pair carrier is refused')
    original = RECIPES.get('macro-test')
    try:
        RECIPES['macro-test'] = ({'level': 20}, {'index': 1}, 'macro test',
                                {'ops': 4, 'carriers': 5, 'edges': [(1, 0, 256), (3, 2, 256)],
                                 'more': [{'level': 24}, {'index': 1}]})
        m = recipe('macro-test', solo=2)
        ok(m[3]['carriers'] == 4 and m[0][1] == 200 and m[3]['more'][0][1] == 240,
           'solo removes other carrier; operator0 remains historical level reference')
        ok('solo' in raises(TypeError, recipe, 'macro-test', solo=1), 'solo needs a carrier')
        ok('muted operator' in raises(TypeError, recipe, 'macro-test', mute=(1,), w1_0=1),
           'edge tweak touching a muted operator is refused')
        RECIPES['macro-test'][3]['edges'] = [(1, 0, 128, 'am'), (3, 2, 256)]
        ok('outgoing am' in raises(TypeError, recipe, 'macro-test', mute=(1,)), 'AM muting cannot claim silence through index')
    finally:
        if original is None:
            del RECIPES['macro-test']
        else:
            RECIPES['macro-test'] = original
    print('selftest-perf: ok bounded macros: exact raw rows, maps, shared budgets, atomic refusals, templates and mute/solo')

    # regression part1: hand-integrated exact timing, before any rate helper uses it.
    p = piece()
    p.meter([(2, Fraction(7, 2)), (4, 4)])
    ok([p.t(bar) for bar in (1, 2, 3, 4, 5)] == [0, 2*B*2, 15*B//2, 11*B, 15*B],
       'meter: 4/4, two7/8 bars, then4/4 keep exact quarter-note bar starts')
    p.tempo_map([(1, 120), (3, 60)], bars=4)
    ok(p.t(3) == p.at_beat(Fraction(15, 2)) == 3_750_000_000 and
       p.at_beat(7) == 3_500_000_000 and p.at_beat(8) == 4_250_000_000 and
       p.t(5) == 11_250_000_000,
       'step tempo at fractional meter anchor: integrate each side, do not interpolate rounded cum')
    snapshot = repr(p.__dict__)
    ok('before tempo_map' in raises(ComposeError, p.meter, [(1, 3)]) and repr(p.__dict__) == snapshot,
       'meter replacement after tempo_map refuses atomically')
    ok('combined' in raises(ComposeError, p.tempo, lambda k: B), 'legacy tempo after tempo_map refused')
    p = piece()
    p.tempo_map([(1, 120), (2, 60)], ramp='linear', bars=1)
    ok(p.cum == [0, 562_500_000, 1_250_000_000, 2_062_500_000, 3_000_000_000],
       'linear beat-duration ramp: integrate500ms to1000ms over4quarters, total3s')
    ok(p.at_beat(Fraction(1, 3)) == 173_611_111 and p.t(2) == 3_000_000_000,
       'linear fractional-time integral rounded once; endpoint admitted')
    ok('4-beat cap' in raises(ComposeError, p.at_beat, Fraction(9, 2)) and
       '4-beat cap' in raises(ComposeError, p.t, 3), 'new tempo cap names its extent')
    p = piece()
    p.meter([(1, Fraction(7, 2))])
    p.tempo_map([(1, 120), (2, 60)], ramp='linear', bars=1)
    ok(p.at_beat(1) == 571_428_571 and p.t(2) == p.cum[-1] == 2_625_000_000,
       'linear7/8 endpoint: average750ms times7/2quarters, with no snapping')
    ok('7/2-beat cap' in raises(ComposeError, p.at_beat, 4), 'fractional map cap refused by name')
    p = piece()
    p.tempo_map([(1, 90)], bars=2)
    ok(p.cum == [0, 666_666_667, 1_333_333_333, 2_000_000_000, 2_666_666_667,
                 3_333_333_333, 4_000_000_000, 4_666_666_667, 5_333_333_333],
       '90bpm: round the rational cumulative sum, not each beat')
    p.tempo_map([(1, Fraction(120_000_000_000, 1_000_000_001))], bars=1)
    ok(p.cum[:3] == [0, 500_000_001, 1_000_000_001], 'whole-ns boundary tie rounds half up once')
    p = piece()
    p.tempo_map([(1, 120)], bars=1250)
    ok(len(p.cum) == 5001 and p.at_beat(5000) == 2_500_000_000_000,
       'new tempo map spans5000beats without legacy4096cap')
    p = piece()
    p.tempo(lambda k: B, beats=5000)
    ok(len(p.cum) == 5001 and p.t(1251) == p.at_beat(5000) == 5000*B,
       'legacy tempo explicit5000cap and exact endpoint')
    ok('5000-beat cap' in raises(ComposeError, p.at_beat, 5001) and
       '5000-beat cap' in raises(ComposeError, p.t, 1252), 'legacy tempo past-cap errors name cap')
    ok('combined' in raises(ComposeError, p.tempo_map, [(1, 120)], bars=1),
       'tempo_map after legacy tempo refused')
    for changes, kw, word in (([(1, 120.0)], dict(bars=1), 'bpm'),
                             ([(2, 120)], dict(bars=2), 'bar 1'),
                             ([(1, 120), (1, 60)], dict(bars=1), 'rise'),
                             ([(1, 120)], dict(bars=1, ramp='curve'), 'ramp'),
                             ([(1, 0)], dict(bars=1), 'positive')):
        p = piece()
        snapshot = repr(p.__dict__)
        ok(word in raises(ComposeError, p.tempo_map, changes, **kw) and repr(p.__dict__) == snapshot,
           'tempo_map refusal is named and atomic: ' + word)
    for changes in ([(1, 3.5)], [(0, 4)], [(2, 4), (1, 3)], [(1, 0)]):
        p = piece()
        snapshot = repr(p.__dict__)
        ok('meter' in raises(ComposeError, p.meter, changes) and repr(p.__dict__) == snapshot,
           'meter refusal is named and atomic')
    print('selftest-perf: ok tempo maps: exact cumulative and fractional-meter timing, linear duration, '
          'half-up boundaries, cap extension and atomic refusals')

    # regression exact rate/note-value queries; no events or patch state are changed.
    ok(tempo_mhz(120, 1) == 2000 and tempo_mhz(125, Fraction(1, 4)) == 8333,
       'tempo LFO120quarter2000 and125sixteenth8333')
    ok(tempo_mhz(12, 16) == 13, 'tempo LFO12.5mHz tie rounds half up')
    ok(Fraction(1000 * 125, 60) / Fraction(1, 4) - tempo_mhz(125, Fraction(1, 4)) == Fraction(1, 3),
       'tempo LFO125sixteenth exact drift is1/30cycles per 100 seconds')
    ok('outside' in raises(ComposeError, tempo_mhz, 200, Fraction(1, 16)) and
       'outside' in raises(ComposeError, tempo_mhz, 19, 32),
       'tempo LFO high and low exact rates refused before rounding')
    ok(tempo_mhz(Fraction(96, 5), 32) == 10 and tempo_mhz(Fraction(375, 2), Fraction(1, 16)) == 50000,
       'tempo LFO exact endpoints10 and50000 accepted')
    for bpm, beats, name in ((120.0, 1, 'bpm'), (120, .25, 'beats_per_cycle'),
                             (True, 1, 'bpm'), (120, False, 'beats_per_cycle')):
        ok(name in raises(ComposeError, tempo_mhz, bpm, beats), 'tempo argument refused by name: ' + name)
    p = piece()
    before = repr(p.__dict__)
    ok([p.beats_us(v) for v in ('1/4', '3/16', '3/32', '1/8.', '1/8T')] ==
       [500000, 375000, 187500, 375000, 166667], 'note values: exact quarter, dotted and triplet microseconds')
    ok(p.beats_mhz('1/4') == 2000 and p.beats_mhz('1/8T') == 6000 and
       p.beats_mhz('1/4', free=2) == 2000 and repr(p.__dict__) == before,
       'constant-tempo queries are pure, FREE2 allowed without a map')
    p = Piece('tempo-test', 125, 4, [])
    ok(p.beats_us('1/16') == 120000 and p.beats_mhz('1/16') == 8333, '125bpm note values')
    p = Piece('tempo-test', 12, 4, [])
    ok(p.beats_mhz('4 bars') == 13, '4bars at12bpm is the12.5mHz half-up tie')
    p = piece()
    p.tempo_map([(1, 19)], bars=1)
    ok('outside' in raises(ComposeError, p.beats_mhz, '8 bars'), '8bars at19bpm refuses before rounding')
    p = piece()
    p.tempo(lambda k: B if k == 0 else 800_000_000)
    ok(p.beats_mhz('1/4', at=0) == 2000 and p.beats_mhz('1/4', at=Fraction(3, 2)) == 1250,
       'legacy map query uses whole-ns duration of the containing beat')
    ok('FREE2' in raises(ComposeError, p.beats_mhz, '1/4', free=2), 'legacy tempo map refuses FREE2')
    p = piece()
    p.meter([(2, Fraction(7, 2))])
    p.tempo_map([(1, 120), (2, 60)], ramp='linear', bars=2)
    ok(p.beats_mhz('1/4', at=2) == 1333 and p.beats_us('1 bar', at=4) == 3500000,
       'linear map uses exact local750ms duration; bars use current7/8 meter')
    ok('FREE2' in raises(ComposeError, p.beats_mhz, '1/4', at=2, free=2), 'new tempo map refuses FREE2')
    for value, kw, word in (('1/4', dict(at=.5), 'at beat'), ('1/4', dict(free=1), 'FREE'),
                            ('1/4', dict(at=8), 'cap'), (1.0, {}, 'note value'), ('1/0', {}, 'note value')):
        ok(word in raises(ComposeError, p.beats_mhz, value, **kw), 'note query refusal: ' + word)
    print('selftest-perf: ok tempo rates: exact fractions, half-up ties, pre-round bounds, '
          'dotted/triplet/bar values, explicit map queries and FREE2 refusals')

    # regression FX note values keep the existing raw header grammar and ranges.
    p = piece()
    p.fx(delay=('3/16', 8000, 5000), chorus=(15000, 3000, '1/4'))
    ok(p.fxh['delay'] == (375000, 8000, 5000) and p.fxh['chorus'] == (15000, 3000, 2000),
       'FX note values: dotted eighth375ms and quarter-rate2Hz at120bpm')
    ok(p._tempo_comment == 120, 'explicit FX note values opt in to tempo metadata')
    p = piece()
    p.fx(delay=('3/32', 8000, 5000))
    ok(p.fxh['delay'][0] == 187500, 'FX dotted sixteenth matches187500us raw delay')
    p = piece(space='stereo')
    p.fx(pingpong=('1/4', 8000, 5000, 100))
    ok(p.fxh['pingpong'] == (500000, 8000, 5000, 100), 'pingpong TIME accepts a note value')
    p = piece()
    p.fx(delay=(187500, 8000, 5000))
    ok(not hasattr(p, '_tempo_comment') and p.fxh['delay'] == (187500, 8000, 5000),
       'legacy raw FX unchanged with no new tempo metadata')
    for kwargs, word in ((dict(delay=('1/1', 8000, 5000)), 'delay'),
                         (dict(delay=('1/64', 8000, 5000)), 'delay'),
                         (dict(chorus=(15000, 3000, '1/8')), 'chorus'),
                         (dict(chorus=(15000, 3000, '8 bars')), 'chorus'),
                         (dict(pingpong=('1/4', 8000, 5000, 100)), 'stereo'),
                         (dict(delay=('1/4', 8000)), 'fields'),
                         (dict(delay=('1/4', 8000, 5000), gain=9999), 'gain')):
        p = piece()
        snapshot = repr(p.__dict__)
        ok(word in raises(ValueError, p.fx, **kwargs) and repr(p.__dict__) == snapshot,
           'FX note-value refusal is atomic: ' + word)
    p = piece()
    p.tempo_map([(1, 120), (2, 60)], ramp='linear', bars=2)
    p.fx(delay=('1/4', 8000, 5000), at=2)
    ok(p.fxh['delay'][0] == 750000, 'FX value samples the explicit map beat, without live retiming')
    for bpm, kwargs in ((Fraction(150_000_000, 124_999), dict(delay=('1/4', 8000, 5000))),
                        (Fraction(747, 125), dict(chorus=(15000, 3000, '1/4')))):
        p = piece()
        p.tempo_map([(1, bpm)], bars=1)
        snapshot = repr(p.__dict__)
        ok('outside' in raises(ComposeError, p.fx, **kwargs) and repr(p.__dict__) == snapshot,
           'FX exact subminimum refuses even when nearest integer reaches the allowed boundary')
    print('selftest-perf: ok FX note values: delay/pingpong times and chorus rate, exact bounds, '
          'raw compatibility and atomic refusal')

    # arp: the notes, times, gates, voices and events of each mode
    want = {'up': [48, 52, 55, 60, 64, 67, 48, 52], 'down': [67, 64, 60, 55, 52, 48, 67, 64],
            'updown': [48, 52, 55, 60, 64, 67, 64, 60]}
    for mode, notes in want.items():
        p = piece()
        vs = p.add_voices('pl', 2, 'the arp')
        got = p.arp('a', vs, ['G3', 'C3', 'E3'], 4, 2, 0.25, mode=mode, octaves=2, seed=5)
        ok([g[3] for g in got] == notes, 'arp %s plays %s' % (mode, [g[3] for g in got]))
        ok([g[0] for g in got] == [4 * B + i * B // 4 for i in range(8)], 'arp %s step times' % mode)
        ok(all(g[1] - g[0] == B // 4 * 8 // 10 for g in got), 'arp %s gate 0.8' % mode)
        ok([g[2] for g in got] == [vs[i % 2] for i in range(8)] and clean(p, W), 'arp %s voices in turn' % mode)
        kinds = [e[4].split()[0] for e in sorted(p.ev['a'])]
        ok(len(kinds) == 16 and kinds.count('on') == 8 and kinds.count('off') == 8, 'arp %s events' % mode)
        ok([e[4] for e in sorted(p.ev['a']) if e[1] == 1] == ['on %7d %5d' % (mhz_m(m), MF) for m in notes],
           'arp %s note-ons in order' % mode)
    print('selftest-perf: ok arp up, down and updown: 8 steps each, 16 events, in order, voices in turn, no retrigger')

    def rand(seed, pseed=0):
        p = piece()
        p.seed = pseed
        return [g[3] for g in p.arp('a', p.add_voices('pl', 2, 'the arp'), ['G3', 'C3', 'E3'], 0, 4, 0.25,
                                    mode='random', octaves=2, seed=seed)]
    r5 = rand(5)
    ok(len(r5) == 16 and set(r5) <= set(want['up']) and len(set(r5)) > 2, 'arp random draws %s' % r5)
    ok(rand(5) == r5 and rand(6) != r5 and rand(5, pseed=1) == rand(6), 'arp random is seeded: piece seed + seed')
    print('selftest-perf: ok arp random: seed 5 twice the same, seed 6 differs, piece seed 1 + seed 5 = seed 6')

    p = piece()
    ok(raises(ValueError, p.arp, 'a', p.add_voices('pl', 1, 'one'), ['C3'], 0, 1, 0.25), 'one busy voice stops arp')
    p = piece()
    ok(len(p.arp('a', p.add_voices('pl', 1, 'one'), ['C3'], 0, 1, 0.25, release_ms=0)) == 4, 'release_ms=0 frees it')
    p = piece()
    v3 = p.add_voices('pl', 3, 'three')
    p.arp('a', v3[:2], ['C3', 'E3'], 0, 1, 0.25, gate=1.2)
    msg = raises(ValueError, p.steps, 'a', v3[1:], 'xxxx', 0, 0.25, ['G3'] * 4, gate=1.6)
    ok(repr(v3[1:]) in msg and repr(v3[:2]) in msg and raises(ValueError, p.arp, 'a', v3[1::-1], ['C3'], 2, 1, 0.25)
       and len(p.ev['a']) == 8 and len(p.steps('a', v3[2:], 'x', 2, 0.25, 'G3')) == 1,
       'voice lists that overlap refused: %s' % msg)
    p = piece()
    ok(raises(ValueError, p.arp, 'a', p.add_voices('pl', 2, 'a'), ['C3'], 0, 1, 0.25, mode='sideways'), 'an unknown mode')
    p = piece()
    p.tempo(lambda k: 500e6 + 20e6 * k)                     # each beat 20 ms longer than the last
    got = p.arp('a', p.add_voices('pl', 3, 'the arp'), ['C3', 'E3'], 2, 2, 0.5, swing=62)
    ok([g[0] for g in got] == [p.at_beat(2 + i * 0.5 + (0.12 if i % 2 else 0)) for i in range(4)], 'arp tempo and swing')
    ok(p.at_beat(2) == p.t(1, 3) and p.at_beat(4.5) == p.t(2, 1.5) and got[1][0] == p.cum[2] + round(0.62 * 540e6),
       'at_beat follows the tempo map')
    print('selftest-perf: ok arp voices: one busy voice raises, release_ms=0 frees it, overlapping voice lists refused; '
          'tempo map and swing 62 follow at_beat')

    # arp: downup, played, chord and the held velocity source
    ORDERS = {('E3',): {'downup': [64, 52], 'played': [52, 64]},
              ('G3', 'C3'): {'downup': [67, 60, 55, 48, 55, 60], 'played': [55, 48, 67, 60]},
              ('G3', 'C3', 'E3'): {'downup': [67, 64, 60, 55, 52, 48, 52, 55, 60, 64],
                                   'played': [55, 48, 52, 67, 60, 64]}}
    for given, modes in ORDERS.items():
        for mode, notes in modes.items():
            p = piece()
            got = p.arp('a', p.add_voices('pl', 2, 'the arp'), list(given), 0, len(notes) * 0.5, 0.25, mode=mode,
                        octaves=2)
            ok([g[3] for g in got] == notes * 2 and clean(p, W), 'arp %s of %s over 2 octaves plays %s'
               % (mode, given, [g[3] for g in got]))
    R0 = RoundRobin.window(0)                               # release_ms=0: busy 1 ms past the off
    p = piece()
    v3 = p.add_voices('pl', 3, 'the chord')
    got = p.arp('a', v3, ['G3', 'C3', 'E3'], 0, 1, 0.25, mode='chord', octaves=2, release_ms=0)
    ok([g[3] for g in got] == [48, 52, 55, 60, 64, 67] * 2, 'arp chord: every held note, octave by step')
    ok(all(len({g[2] for g in got[k:k + 3]}) == 3 and len({g[:2] for g in got[k:k + 3]}) == 1
           and got[k][0] == k // 3 * B // 4 for k in range(0, 12, 3)), 'arp chord: three voices a step, one on and off')
    ok(len(p.ev['a']) == 24 and clean(p, R0), 'arp chord: 12 notes, 24 events, no retrigger')
    for nv, rel in ((2, 0), (3, None)):
        q = piece()
        ok(raises(ValueError, q.arp, 'a', q.add_voices('pl', nv, 'short'), ['G3', 'C3', 'E3'], 0, 1, 0.25,
                  mode='chord', release_ms=rel) and not q.ev['a'],
           'arp chord with %d voices, release_ms %s, refused by the voice ring, nothing written' % (nv, rel))
    HELD = [('G3', 300), ('C3', 100), ('E3', 200)]
    VOF = {48: 100, 52: 200, 55: 300, 60: 100, 64: 200, 67: 300}
    for mode in ('up', 'down', 'updown', 'downup', 'played', 'random', 'chord'):
        p, q = piece(), piece()
        got = p.arp('a', p.add_voices('pl', 3, 'held'), HELD, 0, 2, 0.25, mode=mode, octaves=2, vel='held',
                    seed=5, release_ms=0)
        ref = q.arp('a', q.add_voices('pl', 3, 'held'), [n for n, _ in HELD], 0, 2, 0.25, mode=mode, octaves=2,
                    seed=5, release_ms=0)
        ons = {(e[0], e[3]): e[4] for e in p.ev['a'] if e[1] == 1}
        ok(got == ref and all(ons[g[0], g[2]] == 'on %7d %5d' % (mhz_m(g[3]), VOF[g[3]]) for g in got),
           "arp %s, vel='held': the plain call's notes, each at its own velocity" % mode)
    q = piece()
    qv = q.add_voices('pl', 2, 'refused')
    for bad in (dict(notes=['C3'], vel='held'), dict(notes=[('C3', 100)]), dict(notes=[('C3', 100)], vel=MF),
                dict(notes=[('C3', 40000)], vel='held'), dict(notes=[('C3', 1, 2)], vel='held'),
                dict(notes=[('C3', 100), 'E3'], vel='held'), dict(notes=['C3'], mode='upward')):
        ok(raises(ValueError, q.arp, 'a', qv, start_beat=0, beats=1, rate=0.25, **bad), 'arp refuses %s' % bad)
    ok(not q.ev['a'], 'a refused arp writes nothing')
    print('selftest-perf: ok arp downup and played for 1 to 3 notes over 2 octaves; chord mode, 3 voices a step, '
          'refused when short; held velocities in all seven modes; refusals')

    # regression step 1: changing held keys, with physical releases and optional latch.
    def key_run(keys, **kw):
        p = piece()
        vs = p.add_voices('pl', 3, 'keys')
        options = dict(start_beat=0, beats=7, rate=1, gate=0.25, release_ms=0)
        options.update(kw)
        return p, p.arp('a', vs, keys=keys, **options)

    def key_want(steps):
        """Hand-specified step sets, expanded only into their expected event tuples."""
        out = []
        for beat, notes in steps:
            for note in notes:
                out.append((beat * B, beat * B + B // 4, len(out) % 3, note))
        return out

    KEYS = [(0, 2, 'C3', 100), (1, 3, 'E3', 200), (4, 4.5, 'G3', 300),
            (4.75, 5.25, 'D3', 400)]
    p, got = key_run(KEYS, mode='chord', vel='held')
    ok(got == key_want([(0, [48]), (1, [48, 52]), (2, [52]), (4, [55]), (5, [50])]),
       'keys: press, add, partial release, empty rest and new set; exact on/off/voice/midi')
    p, got = key_run(KEYS, mode='chord', vel='held', latch=True)
    ok(got == key_want([(0, [48]), (1, [48, 52]), (2, [52]), (3, [52]),
                       (4, [55]), (5, [50]), (6, [50])]),
       'latch: partial release removes C; final release holds E; new press replaces the latched set')
    ons = [e[4] for e in p.ev['a'] if e[1] == 1]
    ok(ons == ['on %7d %5d' % (mhz_m(m), hv) for m, hv in
               [(48, 100), (48, 100), (52, 200), (52, 200), (52, 200),
                (55, 300), (50, 400), (50, 400)]],
       'latched keys keep the velocity of their own press')
    p, got = key_run([(0, 1, 'C3', 100)], latch=True, beats=3)
    ok(got == key_want([(0, [48]), (1, [48]), (2, [48])]),
       'latch retains a single key on its first release')
    p, got = key_run([(0, 1, 'C3', 100), (0, 1, 'E3', 200)], latch=True, mode='chord', beats=3)
    ok(got == key_want([(0, [48, 52]), (1, [48, 52]), (2, [48, 52])]),
       'simultaneous final releases retain the whole pre-release chord')
    for latched in (False, True):
        p, got = key_run([(0, 1, 'C3', 100), (1, 2, 'G3', 300)], latch=latched, beats=2)
        ok(got == key_want([(0, [48]), (1, [55])]),
           'release then press at the same onset: old key out, new key in; latch=%s' % latched)
    p, got = key_run([(0, 1, 'C3', 100)], beats=2)
    ok(got == key_want([(0, [48])]), 'release exactly at onset is absent without latch')
    p, got = key_run([(1, 2, 'C3', 100)], beats=2)
    ok(got == key_want([(1, [48])]), 'press exactly at onset is present; earlier step rests')
    p, got = key_run([(0.1, 0.2, 'C3', 100), (0.3, 0.4, 'E3', 200)], latch=True, beats=2)
    ok(got == key_want([(1, [52])]), 'latch processes press/release/new-set transitions between steps')
    p, got = key_run([(0, 1, 'C3', 100), (0, 3, 'E3', 200), (1, 3, 'G3', 300)],
                    latch=True, mode='chord', beats=3)
    ok(got == key_want([(0, [48, 52]), (1, [52, 55]), (2, [52, 55])]),
       'same-time partial release and added press retain only the physically down keys')
    p, got = key_run([(2, 5, 'G3', 300), (0, 5, 'C3', 100), (1, 5, 'E3', 200)],
                    start_beat=3, beats=1, mode='played', vel='held')
    ok(got == key_want([(3, [48])]), 'played follows press time, not unsorted input position')
    p, got = key_run([(0, 1, 'C3', 100), (0, 1, 'C3', 200)], mode='chord', vel='held', beats=1)
    ok(got == key_want([(0, [48, 48])]) and
       [int(e[4].split()[2]) for e in p.ev['a'] if e[1] == 1] == [100, 200],
       'equal-pitch presses keep separate voices and velocities in stable input order')
    p, got = key_run([(0, 4, 'C3', 100), (1, 4, 'E3', 200), (2, 4, 'G3', 300)], beats=4)
    ok(got == key_want([(0, [48]), (1, [52]), (2, [55]), (3, [48])]),
       'step index continues modulo each changed held-set length')

    # Endpoint membership uses rounded nanoseconds, not the original beat values.
    from fractions import Fraction
    close = Fraction(1_000_000_001, 1_000_000_000)
    p, got = key_run([(0, close, 'C3', 100), (close, 2, 'E3', 200)], beats=2)
    ok(p.at_beat(close) == B and got == key_want([(0, [48]), (1, [52])]),
       'key endpoints round to ns before onset membership is tested')
    p = piece()
    p.tempo(lambda k: B + 20_000_000 * k)
    got = p.arp('a', p.add_voices('pl', 1, 'keys'), start_beat=1, beats=1, rate=0.5,
                gate=0.25, swing=62, keys=[(1.6, 1.64, 'C3', 100)], release_ms=0)
    ok(got == [(p.at_beat(1.62), p.at_beat(1.745), 0, 48)],
       'key membership samples the swung onset through the tempo map; release does not cut its gate')

    for mode in ('up', 'down', 'updown', 'downup', 'played', 'random', 'chord'):
        for velocity in ('held', 1234, [1000, 2000]):
            p, q = piece(), piece()
            pv, qv = p.add_voices('pl', 3, 'same'), q.add_voices('pl', 3, 'same')
            options = dict(start_beat=0, beats=2, rate=0.25, mode=mode, octaves=2, gate=0.3,
                           vel=velocity, seed=5, release_ms=0, swing=62, scale=('major', 2))
            p.arp('a', pv, HELD if velocity == 'held' else [n for n, _ in HELD], **options)
            q.arp('a', qv, keys=[(0, 3, n, hv) for n, hv in HELD], **options)
            ok(p.ev == q.ev and p.voices == q.voices,
               'keys matching static notes: exact %s events, velocity=%r, octaves/swing/scale' % (mode, velocity))
    p, got = key_run([], beats=3)
    ok(got == [] and not p.ev['a'], 'an empty held set rests in up mode')
    p, got = key_run([], beats=3, mode='random')
    ok(got == [] and not p.ev['a'], 'an empty held set rests in random mode')
    p, got = key_run([(1, 8, n, hv) for n, hv in HELD], beats=8, mode='random', seed=5)
    q = piece()
    ref = q.arp('a', q.add_voices('pl', 3, 'reference'), [n for n, _ in HELD], 0, 8, 1,
                mode='random', gate=0.25, release_ms=0, seed=5)
    ok([g[3] for g in got] == [g[3] for g in ref[1:]] and
       [g[3] for g in ref[:-1]] != [g[3] for g in ref[1:]],
       'a random empty step consumes its draw: subsequent notes match the static stream after that draw')
    q = piece()
    qv = q.add_voices('pl', 3, 'refused')
    for bad in (dict(), dict(notes=['C3'], keys=[]), dict(notes=['C3'], latch=True),
                dict(keys=[], latch=1), dict(keys='C3'), dict(keys=[(0, 1, 'C3')]),
                dict(keys=[(-1, 1, 'C3', 100)]), dict(keys=[(1, 1, 'C3', 100)]),
                dict(keys=[(2, 1, 'C3', 100)]), dict(keys=[(0, Fraction(1, 10**12), 'C3', 100)]),
                dict(keys=[(0, 1, 'C3', -1)]), dict(keys=[(0, 1, 'C3', 32768)]),
                dict(keys=[(0, 1, float('nan'), 100)]), dict(keys=[(0, 1, 60.5, 100)], scale='major')):
        msg = raises(ValueError, q.arp, 'a', qv, start_beat=0, beats=1, rate=0.25, **bad)
        ok(msg and 'arp on a' in msg, 'keys named refusal: %r' % bad)
    ok(not q.ev['a'], 'all refused keys calls write nothing')
    print('selftest-perf: ok changing keys and latch: exact transitions, last held set, tempo/swing/ns, '
          'all seven modes match static events, rests draw, held velocities and named refusals')

    # chord memory and strum : chord_shape and Piece.chord, the spread a total
    from fractions import Fraction
    ok(chord_shape(['E3', 'G3', 'C3']) == (0, 4, 7) and chord_shape([60, 72, 60, 64]) == (0, 4, 12)
       and chord_shape(['C3']) == (0,) and chord_shape(list(range(60, 68))) == tuple(range(8)),
       'chord_shape stores sorted(set(n - min(notes))), 1 to 8 offsets')
    ok(all(raises(ValueError, chord_shape, bad) for bad in ([], list(range(60, 69)), [60, 60.5])),
       'chord_shape refuses no notes, 9 offsets and a fraction of a semitone')
    T0, DUR = 1_000_000_000, 400_000_000
    for n in range(1, 9):
        shape = tuple(range(0, 2 * n, 2))
        for ms in (0, 1, 199, 200):
            S = ms * 1_000_000
            want = [T0 + (int(Fraction(k * S, n - 1) + Fraction(1, 2)) if n > 1 else 0) for k in range(n)]
            for d in ('up', 'down'):
                p = piece()
                got = p.chord('a', p.add_voices('pl', 8, 'chord'), 'C3', T0, DUR, MF, shape, strum_ms=ms, direction=d)
                ok([g[0] for g in got] == want and (n == 1 or got[-1][0] - got[0][0] == S),
                   'strum of %d notes over %d ms %s: onsets %s, the last S after the first' % (n, ms, d, got))
                ok(all(g[1] == T0 + DUR for g in got) and len({g[2] for g in got}) == n and len(p.ev['a']) == 2 * n,
                   'strum of %d notes over %d ms %s: every off at t + dur, a voice each' % (n, ms, d))
                ok([g[3] for g in got] == [48 + o for o in (shape if d == 'up' else shape[::-1])],
                   'strum of %d notes %s in strum order' % (n, d))
    p = piece()
    got = p.chord('a', p.add_voices('pl', 7, 'chord'), 'C3', 0, DUR, MF, tuple(range(7)), strum_ms=200)
    ok([g[0] for g in got] == [0, 33333333, 66666667, 100000000, 133333333, 166666667, 200000000],
       'strummed chord: 7 notes over 200 ms')
    p = piece()
    got = p.chord('a', p.add_voices('pl', 3, 'chord'), 'C3', 0, DUR, [100, 200, 300], (0, 4, 7), strum_ms='12.5',
                  direction='down')
    ons = {(e[0], e[3]): e[4] for e in p.ev['a'] if e[1] == 1}
    ok([g[0] for g in got] == [0, 6250000, 12500000] and [ons[g[0], g[2]] for g in got]
       == ['on %7d %5d' % (mhz_m(m), v) for m, v in ((55, 100), (52, 200), (48, 300))],
       'a velocity list in strum order, a decimal-string strum of 12.5 ms')
    p = piece()
    ok(len(p.chord('a', p.add_voices('pl', 3, 'chord'), 'C3', 0, 100_000_001, MF, (0, 4, 7), strum_ms=100)) == 3
       and len(p.chord('a', p.add_voices('pl', 1, 'one'), 60, 0, 1, MF, (0,), strum_ms=200)) == 1
       and len(p.chord('a', p.add_voices('pl', 2, 'tiny'), 60, 0, 2, MF, (0, 7), strum_ms=decimal.Decimal('0.000001')))
       == 2, 'a last onset 1 ns before the release plays; one note has no spread; a 1 ns strum')
    q = piece()
    qv = q.add_voices('pl', 2, 'refused')
    base = dict(s='a', voices=qv, key='C3', t=0, dur=DUR, vel=MF, shape=(0, 7))
    for bad in (dict(shape=tuple(range(9))), dict(shape=(4, 7)), dict(shape=(0, 7, 4)), dict(shape=(0, 4.0)),
                dict(shape=()), dict(shape='C3'), dict(key='G9'), dict(key=-1), dict(strum_ms=201),
                dict(strum_ms=-1), dict(strum_ms=12.5), dict(strum_ms=True), dict(strum_ms='0.0000001'),
                dict(strum_ms='nan'), dict(strum_ms=decimal.Decimal('Infinity')), dict(direction='sideways'),
                dict(vel=[MF, MF, MF]), dict(vel=40000), dict(vel=64.0), dict(strum_ms=100, dur=100_000_000),
                dict(t=0.0), dict(dur=0), dict(dur=1e8), dict(shape=(0, 4, 7))):
        ok(raises(ValueError, q.chord, **dict(base, **bad)), 'chord refuses %s' % bad)
    ok(not q.ev['a'], 'a refused chord writes nothing')
    print('selftest-perf: ok chord: shapes, strum onsets for 1 to 8 notes at 0, 1, 199 and 200 ms up and down, '
          'offs at t + dur, the worked values, velocities in strum order, the release threshold, refusals')

    # The default budget follows carriers only, including later routed operators.
    p = piece()
    p.patch('routed', op(1.0, 20, r=50), op(2.0, index=1.0, r=3000), 'release-mask test',
            {'ops': 3, 'carriers': 5, 'edges': [(1, 0, 256)], 'more': [op(1.0, 30, r=600)]})
    vs = p.add_voices('routed', 1, 'the routed carrier')
    ring = p.perf_voices(vs, None, None, 'release test')
    ok(ring.rel == RoundRobin.window(600), 'the longest carrier sets the default, not operator 0 or a modulator')
    ring.get(0, B)
    ok(raises(ValueError, ring.get, B + RoundRobin.window(50), B * 2),
       'the later carrier still reserves the voice after operator 0 would be idle')
    ok(ring.get(B + RoundRobin.window(600), B * 2) == vs[0], 'the longest-carrier window expires exactly')
    ok(p.perf_voices(vs, None, 0, 'release override').rel == RoundRobin.window(0),
       'an explicit release budget still overrides every carrier')
    print('selftest-perf: ok routed release: carrier mask, longest release, boundary and explicit override')

    # steps: prob, seeded, one draw a step
    def hits(seed, prob, pseed=0, pattern='x' * 16):
        p = piece()
        p.seed = pseed
        return [g[4] for g in p.steps('a', p.add_voices('pl', 2, 'steps'), pattern, 0, 0.25, 'C3', prob=prob, seed=seed)]
    h3 = hits(3, 0.5)
    ok(0 < len(h3) < 16 and hits(3, 0.5) == h3 and hits(4, 0.5) != h3, 'prob 0.5 seeded: %s' % h3)
    ok(hits(3, 1.0) == list(range(16)) and hits(3, 0.0) == [] and hits(3, {0: 0.0}) == list(range(1, 16)), 'prob 1, 0, dict')
    ok(hits(3, 0.5, pseed=1) == hits(4, 0.5), 'prob: piece seed + seed')
    ok(hits(3, 0.5, pattern='x.' * 8) == [i for i in h3 if i % 2 == 0], 'one draw a step, rests included')
    half = {i: 0.5 for i in range(16)}
    half[5] = 1.0
    ok([i for i in hits(3, half) if i != 5] == [i for i in h3 if i != 5], 'one step\'s chance moves no other')
    print('selftest-perf: ok steps prob: seed 3 plays steps %s twice, seed 4 others; one draw a step' % h3)

    # steps: conditional trigs : passes, pass0, cond and fill
    def cond_steps(pattern, **kw):
        p = piece()
        return p, p.steps('a', p.add_voices('pl', 2, 'steps'), pattern, 0, 0.25, 'C3', **kw)
    for b in range(2, 9):
        for a in range(1, b + 1):
            c, want = '%d:%d' % (a, b), [q for q in range(16) if q % b == a - 1]
            ok([q for q in range(16) if cond_holds(c, q, frozenset())] == want, 'cond %s truth table' % c)
            got = [g[4] for g in cond_steps('x', passes=16, cond=c)[1]]
            ok(got == want, 'steps cond %s over 16 passes plays %s' % (c, got))
    FILL = (3, 7, 15)
    table = {None: list(range(16)), 'first': [0], 'not-first': list(range(1, 16)), 'fill': list(FILL),
             'not-fill': [q for q in range(16) if q not in FILL]}
    for c, want in table.items():
        ok([q for q in range(16) if cond_holds(c, q, frozenset(FILL))] == want, 'cond %s truth table' % c)
        got = [g[4] for g in cond_steps('x', passes=16, cond=c, fill=FILL)[1]]
        ok(got == want, 'steps cond %s with fill %s plays %s' % (c, FILL, got))
    p, got = cond_steps('x.', passes=4, pass0=3, cond='1:2')
    ok([g[4] for g in got] == [2, 6] and [g[0] for g in got] == [p.at_beat(0.5), p.at_beat(1.5)],
       'pass0 3: passes 4 and 6 of 3 to 6 play, at global steps 2 and 6: %s' % got)
    p, got = cond_steps('x.', passes=3, cond=['not-first', None], locks={0: {'pitch': 'G3'}})
    ok([g[4] for g in got] == [2, 4] and sum(e[1] == 2 for e in p.ev['a']) == 2,
       'a cond and a lock by pattern step repeat in every pass')
    q = piece()
    qv = q.add_voices('pl', 1, 's')
    for bad in (dict(cond='0:2'), dict(cond='3:2'), dict(cond='1:9'), dict(cond='1:1'), dict(cond='sometimes'),
                dict(cond=2), dict(cond={0: 'firsts'}), dict(cond=['first', '9:9']), dict(passes=0), dict(passes=True),
                dict(passes=1.0), dict(pass0=-1), dict(fill='3'), dict(fill=(-1,)), dict(fill=(1.0,))):
        ok(raises(ValueError, q.steps, 'a', qv, 'x', 0, 1, 'C3', **bad), 'steps refuses %s' % bad)
    ok(not q.ev['a'], 'a refused condition writes nothing')
    # the draw law: one draw a (pass, step), pass-major, before the hit and the condition; swing parity by g
    LAW = dict(prob=[0.5, 0.5, 0.7], notes=['C3', 'E3', 'G3'], vel=[MF, P, MP], swing=60, seed=3)

    def law(pattern, **kw):
        """the events of one steps call: time, kind, text and comment (voices and sequence numbers apart)"""
        p = piece()
        p.steps('a', p.add_voices('pl', 3, 'steps'), pattern, 0, 0.25, LAW['notes'], prob=LAW['prob'],
                vel=LAW['vel'], swing=LAW['swing'], seed=LAW['seed'], **kw)
        return [(e[0], e[1], e[4], e[5]) for e in sorted(p.ev['a'])]
    once = law('xx.' * 4)
    ok(law('xx.', passes=4) == once and len(once) > 8, 'four passes write what the pattern four times over writes')
    ok(law('xx.', passes=4, pass0=0, cond=None, fill=()) == law('xx.' * 4, passes=1, pass0=0)
       == law('xx.', passes=4, cond='not-fill') == once, 'the defaults and an always-true cond change nothing')
    later = [e for e in once if e[0] >= piece().at_beat(0.75)]
    ok(law('xx.', passes=4, cond='not-first') == later and len(later) < len(once),
       'a false condition takes its draw: passes 1 to 3 play as they do after pass 0')
    print('selftest-perf: ok steps conditions: A:B for B 2 to 8 over 16 passes, first, not-first, fill and '
          'not-fill, pass0, refusals; one draw a (pass, step), swing by the global step')

    # steps: ratchets
    p = piece()
    got = p.steps('a', p.add_voices('pl', 4, 'steps'), 'x...x...x...x...', 0, 0.25, 'C3', ratchet={0: 2, 4: 3, 8: 4})
    ok([sum(g[4] == i for g in got) for i in (0, 4, 8, 12)] == [2, 3, 4, 1], 'ratchet hit counts')
    ok([g[0] for g in got if g[4] == 8] == [2 * B + j * B // 16 for j in range(4)], 'ratchet 4: a 64th apart')
    ok(all(abs(g[1] - g[0] - B / 15) <= 1 for g in got if g[4] == 4), 'ratchet 3: each hit 0.8 of a third of the step')
    p = piece()
    ok(raises(ValueError, p.steps, 'a', p.add_voices('pl', 1, 's'), 'x', 0, 0.25, 'C3', ratchet=5), 'ratchet 5 refused')
    p = piece()
    e1 = p.add_voices('pl', 1, 's')
    ok(all(raises(ValueError, p.steps, 'a', e1, 'x', 0, 0.25, 'C3', **{k: []})
           for k in ('vel', 'prob', 'ratchet', 'micro', 'gate'))
       and raises(ValueError, p.arp, 'a', e1, ['C3'], 0, 1, 0.25, vel=[]) and not p.ev['a'], 'an empty list refused')
    print('selftest-perf: ok steps ratchet: 2, 3, 4 and 1 hits, a ratchet of 4 a 64th apart; '
          'ratchet 5 and empty lists refused')

    # steps: micro timing, within half a step and inside the piece
    p = piece()
    p.length_ns = 2 * B
    got = p.steps('a', p.add_voices('pl', 4, 'steps'), 'xxxxxxxx', 0, 0.25, 'C3',
                  micro=[-0.9, 0.9, -0.05, 0.05, 0.3, -0.3, 0.0, 0.9])
    ok(len(got) == 8 and all(abs(g[0] - g[4] * B // 4) <= B // 8 for g in got), 'micro within half a step')
    ok(got[0][0] == 0 and max(g[1] for g in got) == 2 * B - 1, 'micro moved inside 0 .. length_ns')
    ok([g[0] for g in got if g[4] in (2, 3, 6)] == [2 * B // 4 - B // 20, 3 * B // 4 + B // 20, 6 * B // 4], 'micro shifts')
    p = piece()
    p.length_ns = B
    ok(raises(ValueError, p.steps, 'a', p.add_voices('pl', 1, 's'), 'x', 0, 0.25, 'C3', gate=8.0),
       'a step that cannot fit is refused')
    print('selftest-perf: ok steps micro: every hit within half a step of its place, the first at 0, the last off at length_ns - 1')

    # steps: locks
    p = piece(space='stereo')
    p.fx(reverb=(500, 300))
    p.filter('pl', 'lp', 2000, 0.7)
    vs = p.add_voices('pl', 2, 'steps')
    for v in vs:
        p.pos('a', 0, v, 0, 1000)
    got = p.steps('a', vs, 'xxxx', 4, 1, 'C3', gate=0.5, locks={0: {'pitch': 'G3'}, 1: {'filter': (800, 2.0)},
                                                                 2: {'send': ('reverb', 16000)}, 3: {'pos': (700, 700)}})
    ev = sorted(p.ev['a'])
    at = [[e[4].split()[0] for e in ev if e[0] == g[0] and e[3] == g[2]] for g in got]
    ok(at == [['on', 'pitch'], ['on', 'filter'], ['send', 'on'], ['pos', 'on']], 'lock kinds %s' % at)
    ok([e[4] for e in ev if e[0] == got[0][0] and e[1] == 2] == ['pitch %7d' % mhz('G3')], 'the pitch lock')
    ok([e[4] for e in ev if e[0] == got[1][0] and e[1] == 3] == ['filter   800000  2000'], 'the filter lock')
    back = [(e[0] - g[1], e[3], e[4]) for g in got[2:] for e in ev if e[0] > g[1] and e[3] == g[2] and e[1] == 0
            and e[4] != 'off']
    ok(back == [(W, got[2][2], 'send reverb 0'), (W, got[3][2], 'pos 0 1000 0')], 'lock undo at the busy window %s' % back)
    q = piece()
    qv = q.add_voices('pl', 1, 's')
    ok(raises(ValueError, q.steps, 'a', qv, 'x', 0, 1, 'C3', locks={0: {'ctl': 1}}), 'a ctl lock that is no pair')
    ok(raises(ValueError, q.steps, 'a', qv, 'x', 0, 1, 'C3', locks={0: {'cutoff': 1}}), 'an unknown lock')
    ok(raises(ValueError, q.steps, 'a', qv, 'x', 0, 1, 'C3', locks={0: {'filter': (800, 1.0)}}), 'a filter lock, no filter')
    ok(not q.ev['a'], 'a refused call writes nothing')
    q = piece(space='stereo')
    ok(raises(ValueError, q.steps, 'a', q.add_voices('pl', 1, 's'), 'x', 0, 1, 'C3', locks={0: {'pos': (1, 1)}}),
       'a pos lock needs a place to go back to')
    print('selftest-perf: ok steps locks: pitch, filter, send and pos write their events, '
          'send and pos set back 64 ms after the off; a ctl lock that is no pair refused')

    # A failed helper call publishes neither its reservations nor any earlier events.
    def perf_state(p):
        rings = {key: (list(ring.v), dict(ring.busy), ring.rel, ring.k) for key, ring in p.rings.items()} \
                if hasattr(p, 'rings') else None
        return (list(p.voices), {s: list(ev) for s, ev in p.ev.items()}, p.seq, rings,
                dict(p.filters), dict(p.fxh))

    def refused_call(p, call, what):
        before, failed = perf_state(p), False
        try:
            call()
        except (ValueError, AssertionError, KeyError, TypeError, AttributeError):
            failed = True
        ok(failed and perf_state(p) == before, 'atomic refusal: ' + what)

    p = piece()
    refused_call(p, lambda: p.arp('a', 2, ['C3'], 0, 1, 0.25, patch='pl', vel=[MF, 40000]),
                 'late velocity does not publish new voices, ring, events or sequence')
    p = piece()
    kp = [(0, 1, 'C3', 100)]
    refused_call(p, lambda: p.arp('a', 2, keys=kp, latch=True, start_beat=0, beats=1, rate=0.25,
                                 patch='pl', vel=[MF, 40000]),
                 'keys with a late velocity failure publish no voices, ring, events or sequence')
    ok(kp == [(0, 1, 'C3', 100)], 'a refused keys call leaves the caller-owned press list untouched')
    p = piece()
    refused_call(p, lambda: p.arp('a', 1, keys=[(0, 1, 'C3', 100), (0, 1, 'E3', 200)],
                                 start_beat=0, beats=1, rate=0.25, mode='chord', patch='pl'),
                 'a held chord exhausting voices after its first note publishes nothing')
    p = piece()
    refused_call(p, lambda: p.arp('a', 2, keys=[(0, 1, 'C3', 100), (2, 3, 'E3', 40000)],
                                 start_beat=0, beats=1, rate=0.25, patch='pl'),
                 'an invalid later press outside the arp span is validated before publishing')
    vs = p.add_voices('pl', 2, 'atomic calls')
    ring = p.perf_voices(vs, None, None, 'atomic test')
    refs = (p.voices, p.ev['a'], p.rings, ring, ring.busy)
    refused_call(p, lambda: p.arp('a', vs, ['C3'], 0, 1, 0.25, vel=[MF, 40000], release_ms=0),
                 'an existing ring keeps its release budget and cursor on failure')
    refused_call(p, lambda: p.arp('missing', vs, ['C3'], 0, 1, 0.25), 'an unknown stream')
    refused_call(p, lambda: p.arp('a', [vs[0]], ['C3'], 0, 1, 0.25), 'an overlapping voice list')
    got = p.arp('a', vs, ['C3', 'E3'], 0, 1, 0.25, mode='random', seed=7)
    q = piece()
    qv = q.add_voices('pl', 2, 'atomic calls')
    q.perf_voices(qv, None, None, 'atomic test')
    want = q.arp('a', qv, ['C3', 'E3'], 0, 1, 0.25, mode='random', seed=7)
    ok(got == want and perf_state(p) == perf_state(q), 'a corrected retry keeps the fresh call seed and event order')
    ok(p.voices is refs[0] and p.ev['a'] is refs[1] and p.rings is refs[2] and
       p.rings[tuple(vs)] is refs[3] and ring.busy is refs[4], 'successful calls preserve caller-held scheduling handles')
    got.clear()
    ok(perf_state(p) == perf_state(q), 'the returned note list is independent of published events and reservations')
    p = piece()
    vs = p.add_voices('pl', 1, 'busy call')
    refused_call(p, lambda: p.arp('a', vs, ['C3'], 0, 1, 0.25), 'voice exhaustion after the first note')
    p = piece(space='stereo')
    p.fx(reverb=(500, 300))
    p.filter('pl', 'lp', 2000, 0.7)
    vs = p.add_voices('pl', 2, 'locked calls')
    refused_call(p, lambda: p.steps('a', vs, 'xx', 0, 1, 'C3', locks={1: {'pos': (1, 1)}}),
                 'a later pos lock without a prior place')
    for v in vs:
        p.pos('a', 0, v, 0, 1000)
    for kind, value in [('pos', (1 << 25, 1)), ('send', ('reverb', 32769)),
                        ('filter', (800, 100)), ('pitch', 'not-a-note')]:
        refused_call(p, lambda kind=kind, value=value: p.steps('a', vs, 'xx', 0, 1, 'C3', locks={1: {kind: value}}),
                     'a malformed late %s lock' % kind)
    refused_call(p, lambda: p.steps('a', 2, 'xx', 0, 1, 'C3', patch='pl', prob=[1, 2]),
                 'a late probability does not publish new voices')
    ok(random.getstate() == state, 'failed and successful helpers leave the global random state untouched')
    print('selftest-perf: ok atomic calls: late failures, new/existing rings, locks, budgets, retries and stable handles')

    # steps: ctl locks, their restores and slides ; ctl_lane
    def lines(p):
        return [(e[0], e[3], e[4], e[5]) for e in ordered_events(p.ev['a'])]

    def ctl_vals(p, n):
        return [(x[0], int(x[2].split()[2]), x[3]) for x in lines(p) if x[2].startswith('ctl %d ' % n)]

    p, q = piece(), piece()
    pv, qv = p.add_voices('pl', 1, 's'), q.add_voices('pl', 1, 's')
    p.ctl('a', 0, pv[0], 3, 777, 'before')
    got = p.steps('a', pv, 'x.x.', 0, 1, 'C3', gate=0.5, text='b',
                  locks={0: {'ctl': [(3, 1000), (0, -2000)]}, 2: {'ctl': (3, 32768)}})
    q.ctl('a', 0, qv[0], 3, 777, 'before')                  # the same, written by hand before a lockless call
    for (t0, t1, v, m, st), lk, back in zip(got, ([(0, -2000), (3, 1000)], [(3, 32768)]), ({0: 0, 3: 777}, {3: 777})):
        for n, value in lk:
            q.ctl('a', t0, v, n, value, 'b, step %d, ctl %d lock' % (st, n))
        for n, value in lk:
            q.ctl('a', t1 + W, v, n, back[n], 'b, step %d, ctl %d lock released' % (st, n))
    q.steps('a', qv, 'x.x.', 0, 1, 'C3', gate=0.5, text='b')
    ok(lines(p) == lines(q), 'ctl locks equal hand-written ctl calls before the on and the restores')
    ok([x[2].split()[0] for x in lines(p) if x[0] == 0] == ['ctl', 'ctl', 'ctl', 'on']
       and [x[2] for x in lines(p) if x[0] == 0][:3] == ['ctl 3 777', 'ctl 0 -2000', 'ctl 3 1000'],
       'the ctl locks precede their on: %s' % [x[2] for x in lines(p) if x[0] == 0])
    ok([(x[0], x[2]) for x in lines(p) if x[3].endswith('released')] ==
       [(got[0][1] + W, 'ctl 0 0'), (got[0][1] + W, 'ctl 3 777'), (got[1][1] + W, 'ctl 3 777')],
       'the restores: the value before the call, 0 when none, at the busy window\'s end')
    p = piece()
    pv = p.add_voices('pl', 1, 's')
    got = p.steps('a', pv, 'xx.x', 0, 1 / 3, 'C3', gate=0.5, text='b', slide={0: True, 1: True},
                  locks={0: {'ctl': [(2, 0), (5, 5)]}, 1: {'ctl': [(2, 7), (5, -6)]}, 3: {'ctl': [(2, 7), (5, 9)]}})
    (a0, a1, _, _, _), (b0, b1, _, _, _), (c0, c1, _, _, _) = got
    ok((b0 - a0) % 8 and (c0 - b0) % 16, 'the slide times need the floor rule')
    want = {2: [0, 0, 1, 2, 3, 4, 5, 6, 7] + [7] * 15 + [7, 0],
            5: [5, 4, 3, 1, 0, -1, -3, -4, -6] + list(range(-6, 9)) + [9, 0]}
    for n in (2, 5):
        ok([x[1] for x in ctl_vals(p, n)] == want[n], 'ctl %d slides %s' % (n, [x[1] for x in ctl_vals(p, n)]))
        ok([x[0] for x in ctl_vals(p, n)] == [a0] + [a0 + k * (b0 - a0) // 8 for k in range(1, 8)] + [b0]
           + [b0 + k * (c0 - b0) // 16 for k in range(1, 16)] + [c0, c1 + W], 'ctl %d slide times' % n)
        ok([x[2] for x in ctl_vals(p, n)][1:3] == ['b, step 0, ctl %d slide 1 of 8' % n,
                                                   'b, step 0, ctl %d slide 2 of 8' % n], 'slide comments')
    p = piece()
    pv = p.add_voices('pl', 1, 's')
    eight = [(n, 4096 * n - 32768) for n in range(7, -1, -1)]
    p.steps('a', pv, 'x', 0, 1, 'C3', text='b', locks={0: {'ctl': eight}})
    ok([x[2] for x in lines(p) if x[0] == 0][:8] == ['ctl %d %d' % (n, 4096 * n - 32768) for n in range(8)]
       and [x[2].split()[0] for x in lines(p) if x[0] == 0][8:] == ['on']
       and sum(x[3].endswith('released') for x in lines(p)) == 8, '8 ctl locks on a step, in rising N, and 8 restores')
    for bad in (eight + [(3, 1)], [(0, 1), (0, 2)], (8, 1), (0, 32769), (True, 1), [(0, 1.5)], [], 'cutoff'):
        refused_call(p, lambda bad=bad: p.steps('a', pv, 'x', 4, 1, 'C3', locks={0: {'ctl': bad}}),
                     'the ctl lock %r' % (bad,))
    two = p.add_voices('pl', 2, 's2')
    for args, kw in ((('xx',), dict(slide={1: True}, locks={0: {'ctl': (1, 1)}, 1: {'ctl': (1, 2)}})),
                     (('xx',), dict(slide={0: True}, locks={0: {'ctl': [(1, 1), (2, 2)]}, 1: {'ctl': (1, 5)}})),
                     (('xx',), dict(slide={0: True}, ratchet={0: 2}, locks={0: {'ctl': (1, 1)}, 1: {'ctl': (1, 2)}})),
                     (('xx',), dict(slide={0: True}, locks={1: {'ctl': (1, 2)}})),
                     (('xx',), dict(slide='yes', locks={0: {'ctl': (1, 1)}, 1: {'ctl': (1, 2)}}))):
        refused_call(p, lambda args=args, kw=kw: p.steps('a', pv, args[0], 4, 1, 'C3', **kw), 'the slide %r' % kw)
    refused_call(p, lambda: p.steps('a', two, 'xx', 4, 1, 'C3', slide={0: True},
                                    locks={0: {'ctl': (1, 1)}, 1: {'ctl': (1, 2)}}), 'a slide to another voice')
    p, q = piece(), piece()
    pv, qv = p.add_voices('pl', 1, 's'), q.add_voices('pl', 1, 's')
    p.ctl_lane('a', pv[0], 4, [(0, 1), (10, -1, 'down'), (10, 2)], 'lane')
    for t, value, nm in ((0, 1, 'lane'), (10, -1, 'down'), (10, 2, 'lane')):
        q.ctl('a', t, qv[0], 4, value, nm)
    ok(lines(p) == lines(q), 'a lane is plain ctl events in list order')
    for bad in ([(20, 1), (10, 1)], [(20, 1), (30, 32769)], [(20, 1), (30, 1, 'x', 'y')], [], [(0.5, 1)]):
        refused_call(p, lambda bad=bad: p.ctl_lane('a', pv[0], 4, bad), 'the lane %r' % (bad,))
    refused_call(p, lambda: p.ctl_lane('a', pv[0], 8, [(0, 1)]), 'a lane on ctl 8')
    for fs in (48000, 44100):                               # the fixture: steps and lanes write the same lines
        a, b = lock_lanes(fs), lock_lanes(fs, via='lanes')
        la = {s: [(e[0], e[3], e[4], e[5]) for e in ordered_events(a.ev[s])] for s in a.streams}
        ok(la == {s: [(e[0], e[3], e[4], e[5]) for e in ordered_events(b.ev[s])] for s in b.streams}
           and sum(x[2].startswith('ctl ') for x in la['bass']) == 64 * 8 + 56 * 8 + 8 * 7 * 8,
           'lock-lanes at %d Hz: steps() and lanes write the same 1408 ctl events and lines' % fs)
    print('selftest-perf: ok ctl locks (): the hand-written ctl calls before the on, restores to the value '
          'before the call (0 when none), slides by the floor rule (8 points a step, the last the next lock, '
          'down included), 8 locks a step and a ninth refused, slide and lane refusals, lanes as plain ctl; '
          'lock-lanes the same from steps() and from lanes at 48000 and 44100 Hz')

    # randomize
    R = {'m_index': (-0.8, 0.8), 'c_d': (-500, 500), 'm_cents': (-7, 7), 'c_level': (-2, 3)}
    d7 = randomize('epiano', 7, R)
    ok(randomize('epiano', 7, R) == d7 and randomize('epiano', 8, R) != d7, 'randomize is seeded')
    ok(sorted(d7) == sorted(R) and all(R[k][0] <= d7[k] <= R[k][1] for k in R) and isinstance(d7['c_d'], int),
       'randomize within the ranges, c_d whole')
    recipe('epiano', **d7)
    big = randomize('epiano', 7, {'m_index': (100, 200), 'c_level': (-90, -50), 'c_vel': (500, 900), 'm_cents': (9e5, 1e6)})
    car, mod, _, _ = recipe('epiano', **big)
    ok(mod[2] == 65535 and car[1] == 0 and car[4] == 256 and big['c_vel'] == 76 and mod[0] < 1 << 32,
       'randomize clamped to LIMITS: %s' % big)
    for bad in ({'c_index': (0, 1)}, {'x_level': (0, 1)}, {'c_wave': (0, 1)}, {'c_d': (5, 1)}):
        ok(raises(ValueError, randomize, 'epiano', 7, bad), 'randomize refuses %s' % bad)
    ok(random.getstate() == state, 'the global random state is untouched')
    print('selftest-perf: ok randomize: seed 7 twice the same, seed 8 differs, clamped to LIMITS: %s'
          % ', '.join('%s %+.4g' % kv for kv in sorted(d7.items())))

    # Scale-lock regression.
    import importlib.util
    from fractions import Fraction
    spec = importlib.util.spec_from_file_location('gen_reference', os.path.join(SPIKE, 'tests', 'generators', 'reference.py'))
    gen_ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen_ref)
    masks = TABLES['scale']
    ok(len(masks) == 30 and len(set(masks.values())) == 30 and all(1 <= m <= 4095 for m in masks.values()),
       'the 30 scale masks of tables.txt are distinct, 1 to 4095')
    same = 0
    for sc, mask in masks.items():
        for root in range(12):
            rot = ((mask << root) | (mask >> (12 - root))) & 4095
            for key in range(128):
                want = gen_ref.quantize(100 * key, rot)
                try:
                    got = scale_lock(key, sc, root)
                except ValueError:
                    got = None
                same += got == (want // 100 if want >= 0 else None)
    ok(same == 30 * 12 * 128, 'the lock equals gen_quantize(100 n, rot(mask, root)) / 100 for every preset, root '
       'and key, %d of %d' % (same, 30 * 12 * 128))
    ok(scale_lock(list(range(60, 73)), 'major', 2) == [59, 61, 62, 62, 64, 64, 66, 67, 67, 69, 69, 71, 71]
       and scale_lock(list(range(60, 73)), 2741, 2) == scale_lock(tuple(range(60, 73)), 'major', 2),
       'D major: keys 60 to 72 give 59 61 62 62 64 64 66 67 67 69 69 71 71')
    ok(scale_lock('C#4', 'major') == 60 and scale_lock(61.0, 0, 5) == 61 and scale_lock([0], 'major') == [0],
       'a name, a whole float and mask 0 (off)')
    for bad in ((0, 'major', 2), (0, 'minor-pentatonic', 1), (128, 'major', 0), (-1, 'major', 0), (60.5, 'major', 0),
                (60, 'nope', 0), (60, 4096, 0), (60, 'major', 12), (60, 'major', -1), (60, True, 0), (60, 2741.0, 0),
                (decimal.Decimal('59.9999999999999999'), 'major', 0), (decimal.Decimal('NaN'), 'major', 0),
                (Fraction(121, 2), 'major', 0), (None, 'major', 0)):
        ok(raises(ValueError, scale_lock, *bad), 'scale_lock refuses %r' % (bad,))
    p, q = piece(), piece()
    pv, qv = p.add_voices('pl', 4, 's'), q.add_voices('pl', 4, 's')
    pa = p.arp('a', pv, ['C4', 'E4', 'G4'], 0, 4, 0.5, mode='updown', octaves=2)
    qa = q.arp('a', qv, ['C4', 'E4', 'G4'], 0, 4, 0.5, mode='updown', octaves=2, scale=None)
    ok(lines(p) == lines(q) and pa == qa, 'arp with scale=None writes what it wrote before')
    p = piece()
    pv = p.add_voices('pl', 4, 's')
    got = p.arp('a', pv, ['C4', 'F#4', 'B4'], 0, 3, 0.5, mode='up', octaves=2, scale=('minor-pentatonic', 9))
    ok([m for _, _, _, m in got] == [60.0, 64.0, 69.0, 72.0, 76.0, 81.0] and all(type(m) is float for *_, m in got),
       'arp scale=: C F# B in A minor pentatonic, two octaves, sound C E A C E A')
    p = piece()
    pv = p.add_voices('pl', 8, 's')
    got = p.arp('a', pv, ['C4', 'C#4', 'D4'], 0, 1, 1, mode='chord', scale='major')
    ok([m for _, _, _, m in got] == [60.0, 60.0, 62.0] and len({v for _, _, v, _ in got}) == 3,
       'arp scale=, chord mode: C C# D in C major sound C C D, the two Cs on their own voices')
    p, q = piece(), piece()
    pv, qv = p.add_voices('pl', 2, 's'), q.add_voices('pl', 2, 's')
    kw = dict(locks={1: {'pitch': 'F#4'}}, vel=[MF, P])
    pa, qa = p.steps('a', pv, 'xx', 0, 1, ['C4', 'D#4'], **kw), q.steps('a', qv, 'xx', 0, 1, ['C4', 'D#4'], scale=None, **kw)
    ok(lines(p) == lines(q) and pa == qa, 'steps with scale=None writes what it wrote before')
    p = piece()
    pv = p.add_voices('pl', 2, 's')
    got = p.steps('a', pv, 'xx', 0, 1, ['C#4', 'D#4'], locks={1: {'pitch': 'F#4'}}, scale=('major', 0))
    pitch = [x for x in lines(p) if x[2].startswith('pitch ')]
    ok([m for _, _, _, m, _ in got] == [60.0, 62.0] and len(pitch) == 1 and pitch[0][2].split()[1] == str(mhz_m(65.0))
       and 'pitch lock F4' in pitch[0][3] and any('C4, step 0' in x[3] for x in lines(p)),
       'steps scale=: the notes and the pitch lock snap down, the comments name the sounding notes')
    p, q = piece(), piece()
    pv, qv = p.add_voices('pl', 4, 's'), q.add_voices('pl', 4, 's')
    p.chord('a', pv, 'D3', 0, B, MF, chord_shape(['D3', 'F#3', 'C4', 'E4']), strum_ms=40)
    q.chord('a', qv, 'D3', 0, B, MF, chord_shape(['D3', 'F#3', 'C4', 'E4']), strum_ms=40, scale=None)
    ok(lines(p) == lines(q), 'chord with scale=None writes what it wrote before')
    p = piece()
    pv = p.add_voices('pl', 4, 's')
    got = p.chord('a', pv, 'D3', 0, B, MF, chord_shape(['D3', 'F#3', 'C4', 'E4']), strum_ms=40, direction='down',
                  scale=('minor-pentatonic', 9))
    ok([m for _, _, _, m in got] == [64.0, 60.0, 52.0, 50.0] and len({v for _, _, v, _ in got}) == 4
       and len({t0 for t0, _, _, _ in got}) == 4, 'chord scale=: D9 strummed down in A minor pentatonic sounds '
       'E C E D, each note its own voice and onset')
    p = piece()
    pv = p.add_voices('pl', 4, 's')
    got = p.chord('a', pv, 'C3', 0, B, [MF, MP, P, PP], (0, 4, 6, 9), strum_ms=30, scale=('minor-pentatonic', 9))
    ok([m for _, _, _, m in got] == [48.0, 52.0, 52.0, 57.0] and len({v for _, _, v, _ in got}) == 4
       and len({t0 for t0, _, _, _ in got}) == 4 and [x[2].split()[2] for x in lines(p) if x[2].startswith('on ')]
       == [str(v) for v in (MF, MP, P, PP)], 'chord scale=: C E F# A sounds C E E A, the two Es on their own voices, '
       'onsets and velocities')
    p = piece()
    pv = p.add_voices('pl', 4, 's')
    for bad in ('nope', 4096, ('major', 12), ('major',), 1.5):
        refused_call(p, lambda bad=bad: p.arp('a', pv, ['C4'], 0, 1, 1, scale=bad), 'arp scale=%r' % (bad,))
        refused_call(p, lambda bad=bad: p.steps('a', pv, 'x', 0, 1, 'C4', scale=bad), 'steps scale=%r' % (bad,))
        refused_call(p, lambda bad=bad: p.chord('a', pv, 'C4', 0, B, MF, (0, 4), scale=bad), 'chord scale=%r' % (bad,))
    refused_call(p, lambda: p.arp('a', pv, [60.5], 0, 1, 1, scale='major'), 'arp scale= on a fractional note')
    refused_call(p, lambda: p.steps('a', pv, 'x', 0, 1, 'C4', locks={0: {'pitch': 60.5}}, scale='major'),
                 'steps scale= on a fractional pitch lock')
    refused_call(p, lambda: p.arp('a', pv, [0], 0, 1, 1, scale=('major', 2)), 'arp scale= snapping below 0')
    for fs in (48000, 44100):                               # the fixture: only the locked notes move
        a, b = cond_lab(fs), cond_lab(fs, scale=True)
        la = {st: [e for e in ordered_events(a.ev[st])] for st in a.streams}
        lb = {st: [e for e in ordered_events(b.ev[st])] for st in b.streams}
        moved = sum(x[4] != y[4] for st in la for x, y in zip(la[st], lb[st]))
        ok(all(len(la[st]) == len(lb[st]) for st in la) and moved == 29
           and all(x[:4] == y[:4] for st in la for x, y in zip(la[st], lb[st]))
           and all(x == y for st in ('drums', 'perc') for x, y in zip(la[st], lb[st])),
           'cond-lab-scale at %d Hz: cond-lab\'s events, 29 notes moved (%d), the drums and hats unmoved' % (fs, moved))
    print('selftest-perf: ok scale lock (): the 30 masks distinct; every preset, root and key as gen_quantize '
          '(%d); D major 59 61 62 62 64 64 66 67 67 69 69 71 71; refusals; scale= on arp, steps (notes and pitch '
          'locks) and chord, None unchanged, snapped notes on their own voices; cond-lab-scale moves 29 notes' % same)

    # Scoped-randomization regression.
    routed = [n for n, r in RECIPES.items() if len(r) > 3 and 'ops' in r[3]]
    SET = (1, 2, 3, 4, 7)
    zero = all(randomize_scoped(n, sd, sc, amount=0, ratios=SET) == {} for n in RECIPES for sd in (0, 7)
               for sc in TABLES['scope'])
    ok(zero, 'amount 0 gives no delta for any recipe or scope, ratios included')
    d7 = randomize_scoped('epiano', 7, 'all', preset='aggressive', ratios=SET)
    ok(randomize_scoped('epiano', 7, 'all', preset='aggressive', ratios=SET) == d7
       and randomize_scoped('epiano', 8, 'all', preset='aggressive', ratios=SET) != d7, 'randomize_scoped is seeded')
    ok(randomize_scoped('epiano', 7) == randomize_scoped('epiano', 7, 'all', 15)
       and randomize_scoped('epiano', 7, preset='aggressive') == randomize_scoped('epiano', 7, amount=60),
       'the presets: mild 15 (the default) and aggressive 60')
    parts, oracle, typed = 0, 0, 0
    for n in RECIPES:
        for sd in (1, 7, 99, -5, 1 << 40):
            al = randomize_scoped(n, sd, 'all', amount=100, ratios=SET)
            op_ = randomize_scoped(n, sd, 'operators', amount=100, ratios=SET)
            env = randomize_scoped(n, sd, 'envelopes', amount=100, ratios=SET)
            parts += al == dict(op_, **env) and list(al) == [f for f in TABLES['field'] if f in al]
            typed += all(type(d) in (int, Fraction) for d in al.values()) \
                and not raises((TypeError, ValueError), recipe, n, **al)   # every delta one recipe takes
            for f, d in al.items():                         # the oracle: Rng(mix32(mix32(seed) + k)), one draw
                side, _, arg = f.partition('_')
                u = Fraction(Rng(mix32(mix32(sd) + TABLES['field'].index(f))).next())
                b0 = Fraction(decimal_value(dict(OP_ARGS, **(RECIPES[n][0] if side == 'c' else RECIPES[n][1] if side == 'm'
                                                              else RECIPES[n][3]['more'][int(side[1:]) - 2]))[arg]))
                if arg == 'ratio':
                    oracle += b0 + d == SET[int(u * len(SET))]
                elif arg in 'adr':
                    lo_t, hi_t = half_up(pitch_scale(b0, -100, 100)), half_up(pitch_scale(b0, 100, 100))
                    oracle += min(LIMITS[arg][1], lo_t) <= b0 + d <= min(LIMITS[arg][1], hi_t) \
                        and b0 + d == min(LIMITS[arg][1], half_up(pitch_scale(b0, (2 * u - 1) * 100, 100)))
                else:
                    unit = SCOPED_UNITS[arg]
                    want = half_up(b0 * unit) + half_up((2 * u - 1) * TABLES['span'][arg])
                    lim = (LIMITS[arg][0] * unit, int(LIMITS[arg][1] * unit))
                    oracle += (b0 + d) * unit == min(lim[1], max(lim[0], want))
    total = sum(len(randomize_scoped(n, sd, 'all', amount=100, ratios=SET)) for n in RECIPES
                for sd in (1, 7, 99, -5, 1 << 40))
    ok(parts == typed == 5 * len(RECIPES), 'all is operators and envelopes, in field order, ints and Fractions only, '
       'each a delta recipe() takes')
    ok(oracle == total and total > 1000, 'each delta from its own stream Rng(mix32(mix32(seed) + k)): %d of %d'
       % (oracle, total))
    lk = randomize_scoped('epiano', 7, 'all', amount=60, ratios=SET, lock=('c_level', 'd'))
    ok(lk == {f: v for f, v in d7.items() if f != 'c_level' and not f.endswith('_d')} and 'c_level' in d7
       and 'c_d' in d7, 'lock: c_level and every d stay, every other field draws what it drew')
    saved = list(TABLES['field'])
    try:
        TABLES['field'][:] = saved[:-1]                     # a demonstration only: one field fewer at the end;
        # a shared stream would pass it too, so the oracle and lock checks above prove each field its own stream
        fewer = {n: randomize_scoped(n, 7, 'all', amount=100, ratios=SET) for n in routed}
    finally:
        TABLES['field'][:] = saved
    ok(all(fewer[n] == {f: v for f, v in randomize_scoped(n, 7, 'all', amount=100, ratios=SET).items()
                        if f != saved[-1]} for n in routed),
       'appending field %s leaves every other delta (a demonstration; the oracle and lock checks prove the '
       'streams)' % saved[-1])
    car, mod, _, _ = recipe('epiano', **randomize_scoped('epiano', 3, 'operators', amount=100, ratios=SET))
    ok(car[0] in [r * 65536 for r in SET] and mod[0] in [r * 65536 for r in SET], 'ratios come from the set')
    fine = decimal.Decimal('0.500007629394531251')             # half a Q16 step above 32768.5: a float sum loses it
    ok(recipe('epiano', **randomize_scoped('epiano', 7, 'operators', amount=100, ratios=(str(fine),)))[0][0]
       == op(ratio=fine)[0] == 32769, 'a ratio from the set reaches recipe() exactly (32769, not 32768)')
    ok('ratio' not in ''.join(randomize_scoped('epiano', 3, 'all', amount=100)), 'no ratios set, no ratio moves')
    ok(not any(f.endswith(('c_index', 'c_fb', 'm_level')) for n in RECIPES if n not in routed
               for f in randomize_scoped(n, 3, 'all', amount=100)), 'no field the engine ignores draws')
    for kw in (dict(scope='algorithms'), dict(scope='filter'), dict(scope='matrix'), dict(scope='nope'),
               dict(amount=101), dict(amount=-1), dict(amount=1.5), dict(amount=True), dict(amount='15'),
               dict(amount=15, preset='mild'), dict(preset='wild'), dict(lock=('x_level',)), dict(lock='nope'),
               dict(ratios=(1.5,)), dict(ratios=()), dict(ratios=(True,)), dict(ratios=(-1,)), dict(ratios=(70000,)),
               dict(ratios=(1, '1')), dict(ratios='12'), dict(ratios=(2, 2.0)), dict(ratios=(1, True)),
               dict(ratios=[decimal.Decimal('Infinity')]), dict(ratios=[decimal.Decimal('NaN')]), dict(ratios=['inf'])):
        ok(raises(ValueError, randomize_scoped, 'epiano', 7, **kw), 'randomize_scoped refuses %s' % kw)
    ok('algorithm randomization is unsupported' in raises(ValueError, randomize_scoped, 'epiano', 7, 'algorithms'),
       'algorithm randomization reports its unsupported scope')
    ok(raises(ValueError, randomize_scoped, 'nope', 7) and raises(ValueError, randomize_scoped, 'epiano', 7.0),
       'randomize_scoped refuses an unknown recipe and a float seed')
    ok(random.getstate() == state, 'the global random state is untouched')
    print('selftest-perf: ok randomize_scoped (, ): amount 0 none; mild 15, aggressive 60; all = operators '
          '+ envelopes; %d deltas from their own streams; lock; a field appended moves none (shown); ratios from the set, exact; '
          'refusals; seed 7 aggressive: %s' % (total, ', '.join('%s %s' % kv for kv in d7.items())))
    # : opt-in compressor/duck authoring, with early atomic refusals.
    for fields in ((0, 10, 0, 1, 0), (600, 200, 100, 5000, 240), (120, 40, 5, 120, 60)):
        q = piece(); q.fx(compressor=list(fields))
        ok(q.fxh == {'compressor': fields}, 'compressor fields retain integer units')
        v = q.add_voices('pl', 1, '')[0]
        q.send('a', 0, v, 'duck', 32768)
        ok(q.ev['a'][-1][4] == 'send duck 32768', 'duck uses ordinary pre-pan send event')
    for field, low, high in ((0, 0, 600), (1, 10, 200), (2, 0, 100), (3, 1, 5000), (4, 0, 240)):
        for value in (low-1, high+1, True, 1.0, '1'):
            q = piece(); q.fx(limiter=(10, 100)); before = copy.deepcopy(q.fxh)
            values = [120, 40, 5, 120, 60]; values[field] = value
            ok(raises(ValueError, q.fx, dcblock=5000, compressor=values) and q.fxh == before,
               'invalid compressor field refuses before storing any FX')
    for values in ((), (120, 40), (120, 40, 5, 120, 60, 1), 120, '120 40 5 120 60'):
        q = piece()
        ok(raises(ValueError, q.fx, compressor=values) and not q.fxh, 'compressor arity/type refusal is atomic')
    for configured in (False, True):
        q = piece(); v = q.add_voices('pl', 1, '')[0]
        if configured: q.fx(compressor=(120, 40, 5, 120, 60))
        for level in ((-1, 32769, True, 1.0, '1') if configured else (0, 32768)):
            before = (q.seq, copy.deepcopy(q.ev))
            ok(raises(ValueError, q.send, 'a', 0, v, 'duck', level) and (q.seq, q.ev) == before,
               'invalid or absent duck send refuses before an event')
    q = piece(); q.fx(compressor=None)
    ok(not q.fxh, 'omitted compressor remains absent')
    print('selftest-perf: ok compressor: all field bounds, atomic refusal, duck prerequisites and events')
    # : opt-in causal flanger, with all new validation before mutation.
    for fields in ((100, 100, 0, 0, 0), (10000, 10000, 2000, 29491, 1), (2000, 1000, 300, 20000, 0)):
        q = piece(); q.fx(flanger=list(fields))
        ok(q.fxh == {'flanger': fields}, 'flanger fields retain integer units')
        v = q.add_voices('pl', 1, '')[0]; q.send('a', 0, v, 'flanger', 32768)
        ok(q.ev['a'][-1][4] == 'send flanger 32768', 'flanger uses ordinary pre-pan send event')
    for field, low, high in ((0, 100, 10000), (1, 100, 10000), (2, 0, 2000), (3, 0, 29491), (4, 0, 1)):
        for value in (low-1, high+1, True, 1.0, '1'):
            q = piece(); q.fx(limiter=(10, 100)); before = copy.deepcopy(q.fxh)
            values = [2000, 1000, 300, 20000, 0]; values[field] = value
            ok(raises(ValueError, q.fx, dcblock=5000, flanger=values) and q.fxh == before,
               'invalid flanger field refuses before storing any FX')
    for values in ((), (2000, 1000), (2000, 1000, 300, 20000, 0, 1), 120, '2000 1000 300 20000 0', (100, 101, 0, 0, 0)):
        q = piece()
        ok(raises(ValueError, q.fx, flanger=values) and not q.fxh, 'flanger tuple/depth refusal is atomic')
    for configured in (False, True):
        q = piece(); v = q.add_voices('pl', 1, '')[0]
        if configured: q.fx(flanger=(2000, 1000, 300, 20000, 0))
        for level in ((-1, 32769, True, 1.0, '1') if configured else (0, 32768)):
            before = (q.seq, copy.deepcopy(q.ev))
            ok(raises(ValueError, q.send, 'a', 0, v, 'flanger', level) and (q.seq, q.ev) == before,
               'invalid or absent flanger send refuses before an event')
    for route in ('amb1', 'amb3', 'binaural'):
        q = piece(); q.space = route
        q.fx(flanger=(2000, 1000, 300, 20000, 0))
        ok(q.fxh == {'flanger': (2000, 1000, 300, 20000, 0)}, 'field flanger admitted by ')
    q = piece(); q.space = 'stereo'; q.fieldh = [('panlaw', (0,))]
    ok(raises(ValueError, q.fx, flanger=(2000, 1000, 300, 20000, 0)) and not q.fxh, 'panlaw flanger remains held')
    q = piece(); q.fx(flanger=None)
    ok(not q.fxh, 'omitted flanger remains absent')
    print('selftest-perf: ok flanger: bounds, depth, tuple, atomic refusal, send and placement')

    # : a declaration is bool-only, persistent and has no implicit FX.
    q = piece(); q.fx()
    ok(not q.fxh, 'omitted decodable keeps the old empty header')
    q.fx(decodable=False)
    ok(not q.fxh, 'false decodable keeps the old empty header')
    q.fx(decodable=True)
    ok(q.fxh == {'decodable': ()}, 'decodable is a valueless declaration')
    q.fx(decodable=False)
    ok(q.fxh == {'decodable': ()}, 'false does not remove an earlier declaration')
    q.fx(gain=-60, decodable=True)
    ok(q.fxh == {'decodable': (), 'gain': (-60,)}, 'declared profile retains ordinary FX headers')
    for value in (None, 0, 1, '', 'true', (), [], 0.0):
        before = copy.deepcopy(q.fxh)
        ok(raises(ValueError, q.fx, dcblock=5000, decodable=value) and q.fxh == before,
           'non-bool decodable refuses before mutating any header')
    print('selftest-perf: ok decodable: bool, valueless header, persistence and atomic refusal')

    # : explicit crusher header and ordinary per-voice point events.
    q = piece(); q.fx(crush=False)
    ok(not q.fxh, 'omitted/false crusher keeps the old empty header')
    q.fx(crush=True); q.fx(crush=False)
    ok(q.fxh == {'crush': ()}, 'crusher declaration is valueless and persistent')
    v = q.add_voices('pl', 1, '')[0]
    for bits, div in ((1, 1), (24, 64), (24, 1), (6, 7)):
        seq = q.seq
        q.crush('a', 123, v, bits, div, text='explicit settings')
        ok(q.ev['a'][-1] == (123, 0, seq + 1, v, 'crush %d %d' % (bits, div), 'explicit settings'),
           'crusher event retains units, time, priority and text')
    for value in (None, 0, 1, '', 'true', (), [], 0.0):
        before = copy.deepcopy(q.fxh)
        ok(raises(ValueError, q.fx, dcblock=5000, crush=value) and q.fxh == before,
           'non-bool crusher refuses before mutating any header')
    for args in [('missing', 0, v, 6, 7), ('a', 0, -1, 6, 7), ('a', 0, 1, 6, 7),
                 ('a', 0, True, 6, 7), ('a', 0, 0.0, 6, 7)] + [
                     ('a', 0, v, value, 7) for value in (0, 25, True, 1.0, '1')] + [
                     ('a', 0, v, 6, value) for value in (0, 65, True, 1.0, '1')]:
        before = (q.seq, copy.deepcopy(q.ev))
        ok(raises(ValueError, q.crush, *args) and (q.seq, q.ev) == before,
           'crusher stream/voice/integer refusal is atomic')
    q = piece(); v = q.add_voices('pl', 1, '')[0]
    before = (q.seq, copy.deepcopy(q.ev))
    ok(raises(ValueError, q.crush, 'a', 0, v, 24, 1) and (q.seq, q.ev) == before,
       'even bypass crusher event requires the declaration')
    print('selftest-perf: ok crusher: bool header, persistence, integer bounds, point events and atomic refusals')
    print('selftest-perf: %d checks ok' % done[0])


SELFTEST_RECIPES = """# compose.py --selftest-recipes writes this file under out and loads it
recipe t2-routed
route 4 5 3>2:300 1>0:256 2>0:128
carrier level=12 fb=0.25 vel=200 a=2 d=900 s=40 r=300
modulator ratio=2.0 index=1.2 vel=128 a=1 d=500 s=20 r=200 ks_break=440.0 ks_slope=6.0
op 2 ratio=1.0 level=18 index=0.8 vel=100 a=1 d=700 s=30 r=250 wave=1
op 3 ratio=7.0 index=2.0 fb=0.5 a=1 d=300 s=60 r=150
note every line kind: operators 2 and 3, a route with operator 2 both a carrier and a modulator
note (the self-test's, )
tags keys layer
recipe t2-w2
carrier level=14 a=2 d=600 s=30 r=200 rs=2 phase=90.0
modulator index=1.0 a=1 d=400 s=20 r=200 fixed=110.0 free=1
note a pair with the operator extras (the self-test's, )
"""


def selftest_recipes():
    """compose.py --selftest-recipes: the checks of the recipe lines and deltas of
    , on SELFTEST_RECIPES and faulty blocks written to a scratch directory under
    out, deleted at the end: a block with every line kind loads and each fault
    stops the loader at its line; every delta moves its field and no other; a routed
    patch's lines parse in the harness (one note, rc 0) as the harness writes them back;
    the leveller's shift moves every carrier; a keys version keeps the route; and
    --expand writes the writer's lines, which render to the same hash; and the operator
    extras (rs, fixed, phase, free; , ) render, change the hash and come back
    from the harness as written. Control metadata round-trips through pair and routed
    recipes, expansion, keys versions and paired-rate LFO/envelope goldens. Prints a line a group;
    exits at the first failure."""
    import shutil
    import tempfile
    import hashlib
    from pathlib import Path
    global MUSIC
    done = [0]

    def ok(cond, what):
        if not cond:
            sys.exit('selftest-recipes: FAILED: %s' % what)
        done[0] += 1

    def raises(exc, f, *a, **kw):
        try:
            f(*a, **kw)
        except exc as e:
            return str(e) or exc.__name__
        return ''

    def fault(text):
        """the loader's message for a recipe file holding text; '' when it loads"""
        with open(os.path.join(tmp, 'fault.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        try:
            load_recipes(os.path.join(tmp, 'fault.txt'))
        except SystemExit as e:
            return str(e.code)
        return ''

    def harness(files, merged=None, chunk=None):
        """one render as (rc, the PLAY line's fields or {}, stderr), never exiting"""
        r = subprocess.run([H, '--play'] + files + (['--merged', merged] if merged else []) +
                           (['--chunk', chunk] if chunk else []),
                           capture_output=True, text=True)
        line = [l for l in r.stdout.splitlines() if l.startswith('PLAY')]
        return r.returncode, dict(t.split('=', 1) for t in line[0].split()[1:]) if line else {}, r.stderr.strip()

    def one_note(name, *patch):
        """a piece of one A2 for a beat on a patch of these p.patch arguments, written under tmp"""
        p = Piece(name, 120, 4, ['%s: one note of the recipe self-test ' % name])
        p.dir = os.path.join(tmp, name)
        p.patch('t', *patch)
        p.stream('a', 'the note')
        v = p.add_voices('t', 1, 'the voice')[0]
        p.on('a', 0, v, 'A2', 24000)
        p.off('a', p.t(1, 2), v)
        p.length_ns = p.t(2)
        p.write()
        return p

    def lines(path, kinds=('patch', 'route')):
        with open(path, encoding='utf-8') as f:
            return [l.rstrip('\n') for l in f if l.split()[:1] and l.split()[0] in kinds]

    def values(ls):
        return [l.split('#')[0].split() for l in ls]

    build()
    os.makedirs(os.path.join(SPIKE, 'out'), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='selftest-recipes-', dir=os.path.join(SPIKE, 'out'))
    saved, music = dict(RECIPES), MUSIC
    try:
        # : the complete guide sources are independent literal controls
        # for helper serialization and exact native/random-block replay.
        guide = (Path(SPIKE) / 'docs' / 'INSTRUMENT.md').read_text(encoding='utf-8')
        section = guide.split('### Complete KS and wavetable examples\n', 1)[1].split('### Text authoring', 1)[0]
        literals = re.findall(r'```text\n(.*?)```', section, re.S)
        ok(len(literals) == 2, 'guide supplies exactly the KS and WT complete controls')
        for fs in (44100, 48000):
            for kind, literal in zip(('ks', 'wt'), literals):
                base = Path(tmp) / ('source-%s-%d' % (kind, fs)); base.mkdir()
                p = Piece(kind, 120, 4, [' explicit source control'], fs=fs)
                p.dir = str(base / 'helper'); p.length_ns = 2000000000
                name = 'kspluck' if kind == 'ks' else 'tone'
                stream = 'golden' if kind == 'ks' else 'notes'
                p.patch(name, [65536,180,0,0,0,5,100,0,800 if kind == 'ks' else 300],
                        [65536,0,0,0,0,5,100,0,300], '')
                p.stream(stream, 'control'); p.add_voices(name, 1, '')
                if kind == 'ks':
                    p.ksource(name, 0, 32700, 7, 2048)
                    p.on_f(stream, 0, 0, 110000, 32767, ''); p.off(stream, 900000000, 0)
                else:
                    p.wavetable('bank', 2, 32, harmonics=((0,1,0,0), (1,2,0,0)))
                    p.wtuse(name, 0, 'bank', 0); p.mod(name, 0, 'ctl0', 'wt_pos0', 32768, shift=0)
                    p.ctl(stream, 0, 0, 0, 0); p.on_f(stream, 0, 0, 220000, 32767, '')
                    p.ctl(stream, 500000000, 0, 0, 32768); p.off(stream, 1000000000, 0)
                p.write()
                literal = literal.replace('fs 48000\n', 'fs %d\n' % fs)
                source = base / 'literal.score.tl'; source.write_text(literal, encoding='utf-8')
                helper, native, replay = (base / (name + '.tl') for name in ('helper', 'literal', 'replay'))
                a, av, ae = harness(p.files(), str(helper))
                b, bv, be = harness([str(source)], str(native))
                r, rv, re_ = harness([str(native)], str(replay), 'random')
                ok(a == b == r == 0, 'KS/WT helper/literal/replay accept: ' + ae + be + re_)
                ok(helper.read_bytes() == native.read_bytes() == replay.read_bytes(),
                   'KS/WT helper/literal/replay canonical bytes agree')
                ok(av['hash'] == bv['hash'] == rv['hash'] and rv['match'] == 'yes' and
                   all(v['block_indep'] == 'yes' and v['clipped'] == '0' for v in (av,bv,rv)),
                   'KS/WT PCM agrees, replays exactly and stays unclipped')
                if kind == 'ks':
                    ok((Path(SPIKE) / 'tests' / 'golden' / str(fs) / 'kspluck.tl').read_bytes() == native.read_bytes(),
                       'KS helper is byte-identical to retained golden')
                bad = (literal.replace('ksource kspluck 0', 'ksource kspluck 4') if kind == 'ks' else
                       literal.replace('wtuse tone 0 bank 0', 'wtuse tone 0 missing 0'))
                invalid = base / 'invalid.tl'; source.write_text(bad, encoding='utf-8')
                rc, _, _ = harness([str(source)], str(invalid))
                ok(rc == 1 and not invalid.exists(), 'invalid KS operator/WT bank refuses without output')
                print('selftest-recipes: ok source %s/%d hash=%s record_sha256=%s helper=literal=random-replay' %
                      (kind, fs, bv['hash'], hashlib.sha256(native.read_bytes()).hexdigest()))
        # F49: text bytes are independent of the process's default encoding.
        utf8_script = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
import compose as cp
p = cp.Piece('utf8-check', 120, 4, ['Greek \u0394 text'])
p.dir = sys.argv[2]
p.patch('bell', *cp.recipe('bell'))
p.stream('a', 'one note')
v = p.add_voices('bell', 1, 'voice')[0]
p.on('a', 0, v, 'A4', 24000)
p.off('a', 250000000, v)
p.length_ns = 500000000
p.write()
"""
        utf8_files = []
        for utf8 in ('0', '1'):
            dest = os.path.join(tmp, 'encoding-' + utf8)
            env = dict(os.environ, LC_ALL='C', PYTHONUTF8=utf8, PYTHONCOERCECLOCALE='0')
            result = subprocess.run([sys.executable, '-B', '-c', utf8_script,
                                     os.path.dirname(os.path.abspath(__file__)), dest],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ok(result.returncode == 0, 'UTF-8 child mode%s: %r' % (utf8, result.stderr))
            utf8_files.append({name: open(os.path.join(dest, name), 'rb').read()
                               for name in sorted(os.listdir(dest))})
        ok(utf8_files[0] == utf8_files[1] and b'\xce\x94' in utf8_files[0]['session.tl'] and
           all(b'\r\n' not in data for data in utf8_files[0].values()),
           'Greek about text has identical UTF-8/LF bytes in C and UTF-8 modes')
        print('selftest-recipes: ok UTF-8: C and UTF-8 modes write identical Greek about text')

        # regression recipe template grammar and exact expansion use the ordinary loader.
        macro_text = '''template Test
case default
macro Bright 5 unipolar
target index.modulators 2048 0
end
recipe macro-loaded
carrier level=20
modulator index=1
use Test
note macro loader control
'''
        ok(fault(macro_text) == '', 'template is host data and use compiles at recipe close')
        loaded = load_recipes(os.path.join(tmp, 'fault.txt'))
        ok(loaded['macro-loaded'][3]['mod'] == {0: ('ctl5', 'index1', 2048, 0)}, 'template raw mod expansion exact')
        for old, new, word in (('end\n', '', 'unknown template'), ('index.modulators', 'index.unknown', 'role'),
                               ('2048 0', '999999 0', 'AMOUNT'), ('2048 0', '2048 16', 'SHIFT'),
                               ('use Test', 'use Missing', 'no macro template'),
                               ('macro Bright 5', 'macro Bright 8', 'INPUT'),
                               ('target index.modulators 2048 0\n', '', 'at least one target'),
                               ('use Test', 'use Test\nuse Test', 'already defined')):
            ok(word in fault(macro_text.replace(old, new)), 'template fault names '+word)
        expr = one_note('macro-expr', *recipe('warm-pad-x'))
        ev = expr.add_voices('t', 1, 'extra expression voice')[0]
        expr.macro_set('a', 0, ev, 'Wheel', 127)
        expr.macro_set('a', 0, ev, 'Pressure', 64)
        expr.macro_set('a', 0, ev, 'Slide', 32)
        expr.on('a', 0, ev, 'A3', 24000)
        expr.off('a', 500000000, ev)
        expr.write()
        rc, kv, err = harness(expr.files(), os.path.join(tmp, 'expr-merged.tl'))
        ok(rc == 0 and kv.get('block_indep') == 'yes' and kv.get('clipped') == '0', 'EXPR recipe renders: '+err)
        rc, replay, err = harness([os.path.join(tmp, 'expr-merged.tl')])
        ok(rc == 0 and replay.get('match') == 'yes' and replay.get('hash') == kv.get('hash'), 'EXPR canonical raw record replays')
        print('selftest-recipes: ok macro templates: grammar, named faults, exact expansion and EXPR raw replay')

        # load
        with open(os.path.join(tmp, 'recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(SELFTEST_RECIPES)
        R = load_recipes(os.path.join(tmp, 'recipes.txt'))
        ok(list(R) == ['t2-routed', 't2-w2'], 'the blocks load in order: %s' % list(R))
        car, mod, note, routing = R['t2-routed']
        ok(routing == {'ops': 4, 'carriers': 5, 'edges': [(1, 0, 256), (2, 0, 128), (3, 2, 300)],
                       'more': [dict(ratio=1.0, level=18, index=0.8, vel=100, a=1, d=700, s=30, r=250, wave=1),
                                dict(ratio=7.0, index=2.0, fb=0.5, a=1, d=300, s=60, r=150)]},
           'the route and op lines load, the edges by FROM then TO: %s' % routing)
        ok(car['fb'] == 0.25 and mod['ks_slope'] == 6.0 and note.endswith('modulator (the self-test\'s, )'),
           'the carrier\'s fb and the modulator\'s key scaling load under a route, the notes join')
        ok(len(R['t2-w2']) == 3 and R['t2-w2'][0]['phase'] == 90.0 and R['t2-w2'][1]['free'] == 1,
           'a pair stays a 3-tuple and takes the operator extras')
        pair = 'recipe x\ncarrier level=10\nmodulator index=1.0\nnote n\n'
        routed = 'recipe x\n%s\ncarrier level=10\nmodulator index=1.0\n%snote n\n'
        for text, n, said in (
                (pair.replace('note', 'op 2 level=3\nnote'), 4, 'an op line needs a route line'),
                (pair.replace('note', 'op 1 index=1.0\nnote'), 4, 'operator 0 is the carrier line and 1 the modulator line'),
                (routed % ('route 3 1 1>0:256', ''), 2, 'route OPS 3 wants an op 2 line'),
                (routed % ('route 3 1 1>0:256 2>0:256', 'op 2 index=1.0\nop 3 index=1.0\n'), 6, 'op 3 is at or above'),
                (routed % ('route 5 1', ''), 2, 'OPS is 2 to 4'),
                (routed % ('route 2 2 0>1:256', ''), 2, 'the carrier mask is odd'),
                (routed % ('route 2 1 1>1:256', ''), 2, 'feeds an operator to itself'),
                (routed % ('route 2 1 1>0:1025', ''), 2, 'WEIGHT is 1 to 1024'),
                (routed % ('route 2 1 1>0:0', ''), 2, 'WEIGHT is 1 to 1024'),
                (routed % ('route 2 1 1>0:256 1>0:128', ''), 2, 'listed twice'),
                (routed % ('route 2 1 2>0:256', ''), 2, 'at or above OPS 2'),
                (routed % ('route 2 1 1-0:256', ''), 2, 'is not FROM>TO:WEIGHT'),
                (routed % ('route 2 1 1>0:256\nroute 2 1 1>0:256', ''), 3, 'a second route line'),
                (routed % ('route 3 1 1>0:256', 'op 2 index=1.0\n'), 2, 'operator 2 reaches no carrier'),
                (routed % ('route 3 1 1>0:256 2>1:256', 'op 2 level=3 index=1.0\n'), 5,
                 'operator 2\'s level changes nothing; operator 2 is no carrier of the route'),
                (routed % ('route 3 5 1>0:256', 'op 2 level=3 index=1.0\n'), 5,
                 'operator 2\'s index changes nothing; operator 2 modulates no operator in the route'),
                (routed.replace('index=1.0', 'level=6 index=1.0') % ('route 2 3', ''), 4,
                 'the modulator\'s index changes nothing; operator 1 modulates no operator in the route'),
                (pair.replace('level=10', 'level=10 index=1.0'), 2, 'the carrier\'s index changes nothing; the engine ignores that field'),
                (pair.replace('index=1.0', 'index=1.0 index=2.0'), 3, 'the modulator sets index twice'),
                (pair.replace('level=10', 'level=10 phase=360.0'), 2, 'phase=360.0 is outside its range'),
                (pair.replace('level=10', 'level=10 rs=1.5'), 2, 'rs takes a whole number'),
                (pair.replace('level=10', 'level=10 rs=8'), 2, 'rs=8 is outside its range'),
                (pair.replace('level=10', 'level=10 fixed=4294967.296'), 2, 'fixed=4294967.296 is outside its range'),
                (pair.replace('level=10', 'level=10 free=3'), 2, 'free=3 is outside its range'),
                (pair.replace('level=10', 'level=10 wave=12'), 2, 'wave=12 is not supported'),
                (pair.replace('note', 'lfo 1\nnote'), 4, 'lfo takes WAVE RATE_MILLIHZ')):
            msg = fault(text)
            ok('fault.txt:%d: ' % n in msg and said in msg, 'line %d should fail with %r; got %r' % (n, said, msg))
        ok(fault(routed.replace('index=1.0', 'level=6') % ('route 2 3', '')) == '',
           'two carriers and no edge load, the modulator\'s level counting')
        print('selftest-recipes: ok load: every line kind of %s; 26 faults stop at their lines' % 't2-routed')

        # deltas
        RECIPES.update(R)
        idx = {k: i for i, k in enumerate(OP_ARGS)}
        pad = lambda o: list(o) + [0] * (16 - len(o))
        car, mod, comment, extra = recipe('t2-routed')
        base = [car, mod] + extra['more']
        ok(comment.startswith('recipe t2-routed: every line kind') and len(extra['more']) == 2 and
           all(len(o) == 12 for o in base), 'a routed recipe is (car, mod, comment, extra), 12 values an operator')
        step = dict(ratio=0.5, level=3, index=0.5, fb=0.25, vel=10, a=5, d=50, s=3, r=20, ks_break=100.0,
                    ks_slope=3.0, wave=1, rs=3, fixed=55.0, phase=45.0, free=1)
        ok(sorted(step) == sorted(OP_ARGS), 'a step for every op() argument')
        moved, refused = 0, []
        for i, side in enumerate(('c', 'm', 'o2', 'o3')):
            for arg, d in step.items():
                key = '%s_%s' % (side, arg)
                if ignored(side, arg, routing):
                    ok('changes nothing' in raises(TypeError, recipe, 't2-routed', **{key: d}), '%s is refused' % key)
                    refused.append(key)
                    continue
                got = recipe('t2-routed', **{key: d})
                diff = [(j, k) for j, (a, b) in enumerate(zip([got[0], got[1]] + got[3]['more'], base))
                        for k in range(16) if pad(a)[k] != pad(b)[k]]
                ok(diff == [(i, idx[arg])] and got[3]['edges'] == extra['edges'],
                   '%s=%s moves operator %d field %d alone: %s' % (key, d, i, idx[arg], diff))
                moved += 1
        ok(refused == ['c_index', 'c_ks_break', 'c_ks_slope', 'm_level', 'o3_level'], 'the ignored fields: %s' % refused)
        got = recipe('t2-routed', time=2.0, o3_cents=1200, w3_2=100, w1_0=-1000, w2_0=5000)
        ok(got[3]['more'][1][0] == 2 * base[3][0], 'o3_cents=1200 doubles operator 3\'s ratio')
        ok(all(o[k] == 2 * b[k] for o, b in zip([got[0], got[1]] + got[3]['more'], base) for k in (5, 6, 8)),
           'time=2 doubles every operator\'s a, d and r')
        ok(got[3]['edges'] == [(1, 0, 1), (2, 0, 1024), (3, 2, 400)], 'wF_T moves a weight, held to 1 to 1024: %s' % got[3]['edges'])
        ok(got[2].startswith('recipe t2-routed (time 2, o3_cents +1200, w3_2 +100, w1_0 -1000, w2_0 +5000): '),
           'the comment tells the deltas: %s' % got[2])
        ok('no tweak w0_1' in raises(TypeError, recipe, 't2-routed', w0_1=10), 'a weight needs its edge')
        ok('no tweak o2_level' in raises(TypeError, recipe, 't2-w2', o2_level=1), 'a pair has no operator 2')
        ok('m_level changes nothing; the engine ignores that field' in raises(TypeError, recipe, 't2-w2', m_level=1),
           'a pair refuses what the engine ignores, as before')
        c2, m2, _, _ = recipe('t2-w2', c_phase=300.0, c_rs=9, m_free=-5)
        ok(len(c2) == 16 and c2[12:] == [7, 0, 65535, 0] and m2[12:] == [0, 110000, 0, 0],
           'the operator extras\' deltas stop at LIMITS: %s %s' % (c2[12:], m2[12:]))
        d7 = randomize('t2-routed', 7, {'o2_level': (1.0, 2.0), 'o3_cents': (-5, 5)})
        ok(sorted(d7) == ['o2_level', 'o3_cents'] and recipe('t2-routed', **d7)[3]['more'][0][1] != base[2][1],
           'randomize draws o2_ and o3_ deltas: %s' % d7)
        ok(raises(ValueError, randomize, 't2-routed', 7, {'o3_level': (0, 1)}) and
           raises(ValueError, randomize, 't2-routed', 7, {'w3_2': (0, 1)}), 'randomize refuses o3_level and a weight')
        print('selftest-recipes: ok deltas: %d move their field alone, %d refused (%s); cents, time and weights'
              % (moved, len(refused), ', '.join(refused)))

        # the writer and the harness
        p = one_note('t2-routed', *recipe('t2-routed'))
        rc, kv, err = harness(p.files(), os.path.join(tmp, 'merged.tl'))
        ok(rc == 0 and kv, 'the routed patch renders: rc=%d %s' % (rc, err))
        mine = lines(p.files()[0])
        ok([l.split()[:3] for l in mine] == [['patch', 't', '0'], ['patch', 't', '1'], ['patch', 't', '2'],
                                             ['patch', 't', '3'], ['route', 't', '4']] and
           mine[-1] == 'route t 4 5 1>0:256 2>0:128 3>2:300', 'four patch rows, then the route: %s' % mine)
        ok(values(mine) == values(lines(os.path.join(tmp, 'merged.tl'))), 'the harness writes the same lines back')
        q = one_note('t2-pair', car, mod, comment)
        rc2, kv2, err2 = harness(q.files())
        ok(rc2 == 0 and kv2 and kv2['hash'] != kv['hash'], 'the route changes the render: %s, %s' % (kv.get('hash'), kv2.get('hash')))
        ok(values(lines(q.files()[0])) == values(mine[:2]) and not q.routes, 'the pair writes operators 0 and 1 alone')
        p.write({'t': car[1] + 30})
        got = [int(v[4]) for v in values(lines(p.files()[0], ('patch',)))]
        ok(got == [car[1] + 30, mod[1], extra['more'][0][1] + 30, extra['more'][1][1]],
           'the leveller\'s shift moves every carrier of the route by one amount: %s' % got)
        print('selftest-recipes: ok writer: rc=0 hash=%s, the harness writes the lines back, the 2-op reduction '
              'hash=%s, a level shift moves operators 0 and 2' % (kv['hash'], kv2['hash']))

        # a keys version keeps the route
        MUSIC = tmp
        make = lambda: one_note('t2-keys', *recipe('t2-routed'))
        m = make()                                  # the committed record mono_levels reads
        k = keys_version(make)()
        k.write()
        rc3, kv3, err3 = harness(k.files())
        ok(k.routes == m.routes and lines(k.files()[0], ('route',)) == ['route t 4 5 1>0:256 2>0:128 3>2:300'] and
           rc3 == 0, 'the keys version keeps the route and renders: rc=%d %s' % (rc3, err3))
        MUSIC = music
        print('selftest-recipes: ok keys version: the route kept, rc=0 hash=%s' % kv3['hash'])

        # --expand
        kw = dict(o2_level=3, w3_2=-44, m_cents=-7)
        p = one_note('t2-expand', *recipe('t2-routed', **kw))
        with open(p.files()[0], encoding='utf-8') as f:
            text = [l for l in f if l.split()[:1] not in (['patch'], ['route'])]
        at = next(i for i, l in enumerate(text) if l.startswith('voice '))
        text.insert(at, 'recipe t t2-routed o2_level=3 w3_2=-44 m_cents=-7  # the routed test recipe, moved\n')
        src, dst = os.path.join(tmp, 'expand.tl.in'), os.path.join(tmp, 'expand.tl')
        with open(src, 'w', encoding='utf-8', newline='\n') as f:
            f.writelines(text)
        ok(expand_timeline(src, dst) == 1, 'one statement expanded')
        ok(lines(dst) == lines(p.files()[0]), '--expand writes the writer\'s lines: %s' % lines(dst))
        rc4, kv4, err4 = harness([dst, p.files()[1]])
        rc5, kv5, _ = harness(p.files())
        ok(rc4 == 0 and kv4 and kv4['hash'] == kv5['hash'], 'the expanded record renders as the piece: rc=%d %s' % (rc4, err4))
        print('selftest-recipes: ok expand: the statement gives the writer\'s 5 lines, rc=0 hash=%s both ways' % kv4['hash'])

        # the operator extras
        car, mod, comment, _ = recipe('t2-w2')
        w = one_note('t2-w2', car, mod, comment)
        rc6, kv6, err6 = harness(w.files(), os.path.join(tmp, 'w2.tl'))
        ok(rc6 == 0 and kv6, 'the harness takes a patch line of 16 values: rc=%d %s'
           % (rc6, err6.splitlines()[0].replace(tmp + os.sep, '')[:100] if err6 else 'no PLAY line'))
        u = one_note('t2-w2-off', car[:12], mod[:12], comment)
        rc7, kv7, _ = harness(u.files())
        ok(rc7 == 0 and kv7 and kv7['hash'] != kv6['hash'], 'the operator extras change the render')
        ok(values(lines(w.files()[0])) == values(lines(os.path.join(tmp, 'w2.tl'))),
           'the harness writes the 16-value rows back: %s' % lines(w.files()[0]))
        print('selftest-recipes: ok extras: rs, fixed, phase and free render, rc=0 hash=%s (without them %s)'
              % (kv6['hash'], kv7['hash']))

        # LFO metadata is independent of routing; the recipe grammar uses raw timeline units.
        lfo_short = '0 6000 300 500 30 0 0'
        lfo_full = '1 4000 100 0 20 60 512 16384 15 5 10'
        pair_lfo = pair.replace('recipe x', 'recipe t3-lfo').replace('note n', 'lfo ' + lfo_short + '\nnote n')
        routed_lfo = SELFTEST_RECIPES.split('recipe t2-w2')[0].replace('recipe t2-routed',
                            'recipe t3-routed-lfo\nlfo ' + lfo_full)
        with open(os.path.join(tmp, 'lfo-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(pair_lfo + routed_lfo)
        L = load_recipes(os.path.join(tmp, 'lfo-recipes.txt'))
        ok(L['t3-lfo'][3] == {'lfo': tuple(map(int, lfo_short.split()))}, 'a pair carries only LFO metadata')
        ok(L['t3-routed-lfo'][3]['lfo'] == tuple(map(int, lfo_full.split())) and
           L['t3-routed-lfo'][3]['ops'] == 4, 'an LFO before its route validates against all four operators')
        bad_lfos = ['0 9 0 0 0 0 0', '7 1000 0 0 0 0 0', '0 50001 0 0 0 0 0',
                    '0 1000 60001 0 0 0 0', '0 1000 0 60001 0 0 0', '0 1000 0 0 1201 0 0',
                    '0 1000 0 0 0 1201 0', '0 1000 0 0 0 0 65536', '0 1000 0 0 0 0 0 65536 3 3 3',
                    '0 1000 0 0 0 0 0 0 16 3 3', '0 1000 0 0 0 0 0 0 4 3 3',
                    '0 1000 0 0 0 0 0 0 3 4 3', '0 1000 0 0 0 0 0 0 3 3 4',
                    '0 1000 0 0 0 0 0 0', '0 1000 0 0 0 0 0 0 3 3',
                    '0 1000 0 0 0 0 0 0 3 3 3 0 0', '0 -1000 0 0 0 0 0', '0 1.0 0 0 0 0 0',
                    '0 1000 0 0 0 0 0 0 3 3 3 1', '0 1000 0 0 0 0 0 0 3 3 3 3']
        for value in bad_lfos:
            msg = fault(pair.replace('note n', 'lfo ' + value + '\nnote n'))
            ok('fault.txt:4: ' in msg and 'lfo' in msg, 'bad LFO fields fail at their line: %s (%s)' % (value, msg))
        ok('a second lfo line' in fault(pair.replace('note n', 'lfo ' + lfo_short + '\nlfo ' + lfo_short + '\nnote n')),
           'a recipe refuses duplicate LFO lines')
        RECIPES.update(L)
        ok(recipe('t3-lfo', time=2, m_index=0.5)[3] == L['t3-lfo'][3], 'operator deltas and time leave the LFO alone')
        ok(randomize('t3-lfo', 7, {'m_index': (0, 1)}) == randomize('epiano', 7, {'m_index': (0, 1)}),
           'a pair with LFO metadata accepts operator randomization')
        for name in L:
            p = one_note(name, *recipe(name))
            rc, kv, err = harness(p.files(), os.path.join(tmp, name + '-merged.tl'))
            ok(rc == 0 and kv.get('block_indep') == 'yes', '%s renders with LFO: rc=%d %s' % (name, rc, err))
            authored = lines(p.files()[0], ('lfo',))
            ok(authored == [lfo_row('t', *L[name][3]['lfo'])], 'Piece writes the recipe LFO once')
            merged_values = values(lines(os.path.join(tmp, name + '-merged.tl'), ('lfo',)))[0][2:]
            expected = list(L[name][3]['lfo'])
            if len(expected) == 7:
                expected += [0, 3, 3, 3]
            ok(merged_values == list(map(str, expected)), 'the reader fills omitted phase and masks only')
            with open(p.files()[0], encoding='utf-8') as f:
                text = [line for line in f if line.split()[:1] not in (['patch'], ['route'], ['lfo'])]
            text.append('recipe t %s\n' % name)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.writelines(text)
            ok(expand_timeline(src, dst) == 1 and
               lines(dst, ('patch', 'route', 'lfo')) == lines(p.files()[0], ('patch', 'route', 'lfo')),
               'expand writes the same operators, route and LFO for %s' % name)
            erc, ekv, err = harness([dst, p.files()[1]])
            ok(erc == 0 and ekv.get('hash') == kv['hash'], 'expanded LFO recipe keeps its rendered hash')
            p.lfo('t', *map(int, lfo_short.split()))
            p.write()
            ok(len(lines(p.files()[0], ('lfo',))) == 1 and p.lfos['t'] == tuple(map(int, lfo_short.split())),
               'Piece.lfo replaces a recipe LFO')
            ok(raises(ValueError, p.lfo, 'missing', *map(int, lfo_short.split())), 'Piece.lfo refuses an unknown patch')
            ok(raises(ValueError, p.lfo, 't', *map(int, lfo_short.split()), phase_u16=1),
               'Piece.lfo refuses a partial phase/mask group')
        MUSIC = tmp
        make = lambda: one_note('t3-lfo-keys', *recipe('t3-lfo'))
        make()
        k = keys_version(make)()
        k.write()
        ok(k.lfos == {'t': tuple(map(int, lfo_short.split()))} and len(lines(k.files()[0], ('lfo',))) == 1,
           'a keys version preserves the LFO')
        MUSIC = music
        print('selftest-recipes: ok LFO: pair and routed recipes, 21 refusals, expand, defaults, keys and writer replacement')

        # FREE 2 is opt-in on both operators and the LFO; an explicit LFO FREE 0 is
        # preserved by the composer and omitted by the harness's canonical writer.
        free_fields = (5, 1700, 13, 29, 41, 0, 0, 16384, 3, 0, 0)
        free_blocks = []
        for mode in (0, 2):
            free_blocks.append(pair.replace('recipe x', 'recipe free%d' % mode)
                .replace('level=10', 'level=10 phase=90 free=%d' % mode)
                .replace('index=1.0', 'index=1.0 phase=135 free=%d' % mode)
                .replace('note n', 'lfo ' + ' '.join(map(str, free_fields + (mode,))) + '\nnote n'))
        with open(os.path.join(tmp, 'free-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(''.join(free_blocks))
        FR = load_recipes(os.path.join(tmp, 'free-recipes.txt'))
        RECIPES.update(FR)
        free_hashes = []
        for mode in (0, 2):
            name = 'free%d' % mode
            car, mod, comment, extra = recipe(name)
            ok(car[15] == mod[15] == mode and extra['lfo'] == free_fields + (mode,),
               'recipe keeps the operator and LFO FREE values')
            p = one_note(name, car, mod, comment, extra)
            p.length_ns = 2_000_000_000
            p.on('a', 1_200_000_000, 0, 'A2', 24000)
            p.off('a', 1_500_000_000, 0)
            p.lfo('t', *free_fields, free=mode)
            p.write()
            authored = lines(p.files()[0], ('lfo',))
            ok(authored == [lfo_row('t', *free_fields, mode)], 'Piece.lfo writes explicit FREE unchanged')
            ok(raises(ValueError, p.lfo, 't', *free_fields[:7], free=mode),
               'FREE requires the complete phase/mask group')
            merged = os.path.join(tmp, name + '-merged.tl')
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes', 'FREE %d idle-gap render: %s' % (mode, err))
            expected = free_fields + ((2,) if mode else ())
            ok(values(lines(merged, ('lfo',))) == [['lfo', 't'] + list(map(str, expected))],
               'the canonical record appends only nondefault LFO FREE 2')
            erc, ekv, err = harness([merged])
            ok(erc == 0 and ekv.get('match') == 'yes' and ekv.get('hash') == kv.get('hash'),
               'the FREE idle-gap record replays its hash')
            free_hashes.append(kv.get('hash'))
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('patch', 'route', 'lfo')) == lines(p.files()[0], ('patch', 'route', 'lfo')),
               'recipe expansion preserves FREE operators and LFO')
        ok(free_hashes[0] != free_hashes[1], 'FREE 2 changes the idle-gap render from key sync')
        ok(recipe('free2', c_free=9)[0][15] == 2, 'operator FREE deltas clamp at the new value 2')
        MUSIC = tmp
        make = lambda: one_note('free2-keys', *recipe('free2'))
        make()
        k = keys_version(make)()
        k.write()
        ok(k.lfos['t'] == free_fields + (2,) and k.patches['t'][0][15] == k.patches['t'][1][15] == 2,
           'keys preserve operator and LFO FREE 2')
        MUSIC = music
        print('selftest-recipes: ok FREE 2: recipe, writer, canonical replay, idle gap, expansion, defaults and keys')

        # Re-serialize each LFO golden LFO through Piece.lfo and its session writer, then replay
        # the original record with only that line replaced. No WAV or golden file is written.
        for fs in (48000, 44100):
            for name in ('vibrato', 'tremolo', 'wobble', 'lfosh'):
                golden = os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl')
                with open(golden, encoding='utf-8') as f:
                    text = f.readlines()
                words = next(line.split() for line in text if line.startswith('lfo '))
                p = Piece('t3-golden', 120, 4, [], fs=fs)
                p.dir = os.path.join(tmp, 't3-golden')
                patch_ops = [list(map(int, line.split()[3:])) for line in text if line.startswith('patch ')]
                routing = next((line.split() for line in text if line.startswith('route ')), None)
                extra = None
                if routing:
                    extra = {'ops': int(routing[2]), 'carriers': int(routing[3]), 'more': patch_ops[2:],
                             'edges': [tuple(map(int, re.split('[>:]', edge))) for edge in routing[4:]]}
                p.patch(words[1], patch_ops[0], patch_ops[1], '', extra)
                p.lfo(words[1], *map(int, words[2:]))
                p.length_ns = 1_000_000_000
                p.write()
                rewritten = lines(p.files()[0], ('lfo',))[0]
                ok(rewritten == ' '.join(words), '%s/%s: writer preserves every LFO field' % (fs, name))
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    f.writelines(rewritten + '\n' if line.startswith('lfo ') else line for line in text)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%s/%s: golden LFO replays: rc=%d %s' % (fs, name, rc, err))
                print('selftest-recipes: ok LFO golden %d/%s hash=%s match=yes block_indep=yes' % (fs, name, kv['hash']))

        # PEG metadata uses raw cents/ms and resolves its optional mask after the route.
        peg_short = '0 80 -120 200 0 100 2400 120'
        peg_full = '50 400 -30 150 0 100 -1200 240 5'
        pair_peg = pair.replace('recipe x', 'recipe w4-peg').replace('note n', 'peg ' + peg_short + '\nnote n')
        routed_peg = routed_lfo.replace('recipe t3-routed-lfo', 'recipe w4-routed-peg\npeg ' + peg_full)
        with open(os.path.join(tmp, 'peg-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(pair_peg + routed_peg)
        G = load_recipes(os.path.join(tmp, 'peg-recipes.txt'))
        ok(G['w4-peg'][3] == {'peg': tuple(map(int, peg_short.split())) + (3,)},
           'a pair carries only PEG metadata, with its omitted mask resolved')
        ok(G['w4-routed-peg'][3]['peg'] == tuple(map(int, peg_full.split())) and
           G['w4-routed-peg'][3]['ops'] == 4 and G['w4-routed-peg'][3]['lfo'] == tuple(map(int, lfo_full.split())),
           'PEG and LFO before their route validate against all four operators')
        ok(peg_values((0, 0) * 4, 4) == (0, 0) * 4 + (15,), 'four operators get an omitted mask of 15')
        ok(peg_values((-4800, 0, 4800, 60000) * 2 + (3,), 2) == (-4800, 0, 4800, 60000) * 2 + (3,),
           'PEG levels and times accept both endpoints')
        bad_pegs = [peg_short + ' 0', peg_short + ' 16', peg_short + ' 4', peg_short + ' -1',
                    peg_short + ' 3 3', '0 80 -120 200 0 100 2400',
                    '+0 80 -120 200 0 100 2400 120', '0.0 80 -120 200 0 100 2400 120',
                    '０ 80 -120 200 0 100 2400 120']
        for i in range(8):
            for value in ((-4801, 4801) if i % 2 == 0 else (-1, 60001)):
                fields = peg_short.split()
                fields[i] = str(value)
                bad_pegs.append(' '.join(fields))
        for value in bad_pegs:
            msg = fault(pair.replace('note n', 'peg ' + value + '\nnote n'))
            ok('fault.txt:4: ' in msg and 'peg' in msg, 'bad PEG fields fail at their line: %s (%s)' % (value, msg))
        ok('a second peg line' in fault(pair.replace('note n', 'peg ' + peg_short + '\npeg ' + peg_short + '\nnote n')),
           'a recipe refuses duplicate PEG lines')
        ok(raises(ValueError, peg_values, (True, 0) + (0, 0) * 3, 2), 'PEG rejects bool as an integer field')
        ok(raises(ValueError, peg_row, 'bad name', *map(int, peg_short.split())), 'PEG writer refuses a bad patch name')
        RECIPES.update(G)
        ok(recipe('w4-peg', time=2, m_index=0.5)[3] == G['w4-peg'][3],
           'operator deltas and time leave the PEG alone')
        ok(randomize('w4-peg', 7, {'m_index': (0, 1)}) == randomize('epiano', 7, {'m_index': (0, 1)}),
           'a pair with PEG metadata accepts operator randomization')
        for name in G:
            p = one_note(name, *recipe(name))
            ops = G[name][3].get('ops', 2)
            rc, kv, err = harness(p.files(), os.path.join(tmp, name + '-merged.tl'))
            ok(rc == 0 and kv.get('block_indep') == 'yes', '%s renders with PEG: rc=%d %s' % (name, rc, err))
            authored = lines(p.files()[0], ('peg',))
            ok(authored == [peg_row('t', *G[name][3]['peg'], ops=ops)], 'Piece writes every PEG field once')
            ok(values(authored) == values(lines(os.path.join(tmp, name + '-merged.tl'), ('peg',))),
               'the reader and writer preserve every PEG field')
            headers = lines(p.files()[0], ('patch', 'route', 'lfo', 'peg'))
            with open(p.files()[0], encoding='utf-8') as f:
                text = [line for line in f if line.split()[:1] not in (['patch'], ['route'], ['lfo'], ['peg'])]
            text.append('recipe t %s\n' % name)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.writelines(text)
            ok(expand_timeline(src, dst) == 1 and lines(dst, ('patch', 'route', 'lfo', 'peg')) == headers,
               'expand writes the same operators, route, LFO and PEG for %s' % name)
            erc, ekv, err = harness([dst, p.files()[1]])
            ok(erc == 0 and ekv.get('hash') == kv['hash'], 'expanded PEG recipe keeps its rendered hash')
            p.peg('t', *map(int, peg_short.split()))
            p.write()
            ok(len(lines(p.files()[0], ('peg',))) == 1 and
               p.pegs['t'] == tuple(map(int, peg_short.split())) + ((1 << ops) - 1,),
               'Piece.peg replaces a recipe PEG and resolves its omitted mask')
            ok(raises(ValueError, p.peg, 'missing', *map(int, peg_short.split())), 'Piece.peg refuses an unknown patch')
            ok(raises(ValueError, p.peg, 't', *map(int, peg_short.split()), mask=1 << ops),
               'Piece.peg refuses a mask naming a missing operator')
            p.patch('t', *recipe('epiano'))
            p.write()
            ok('t' not in p.pegs and not lines(p.files()[0], ('peg', 'lfo', 'route')),
               'replacing the patch removes its previous PEG, LFO and route metadata')
        MUSIC = tmp
        make = lambda: one_note('w4-peg-keys', *recipe('w4-peg'))
        make()
        k = keys_version(make)()
        k.write()
        ok(k.pegs == {'t': G['w4-peg'][3]['peg']} and len(lines(k.files()[0], ('peg',))) == 1,
           'a keys version preserves the PEG')
        MUSIC = music
        print('selftest-recipes: ok PEG: pair, routed and LFO recipes, 26 refusals, expand, masks, keys and replacement')

        # Rewrite only the PEG through the public helper and session writer; the saved
        # golden schedule and hash still have to replay exactly, at both supported rates.
        for fs in (48000, 44100):
            for name in ('pegdrop', 'pegslide'):
                with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                words = next(line.split() for line in text if line.startswith('peg '))
                p = Piece('w4-golden', 120, 4, [], fs=fs)
                p.dir = os.path.join(tmp, 'w4-golden')
                patch_ops = [list(map(int, line.split()[3:])) for line in text if line.startswith('patch ')]
                routing = next((line.split() for line in text if line.startswith('route ')), None)
                extra = None
                if routing:
                    extra = {'ops': int(routing[2]), 'carriers': int(routing[3]), 'more': patch_ops[2:],
                             'edges': [tuple(map(int, re.split('[>:]', edge))) for edge in routing[4:]]}
                p.patch(words[1], patch_ops[0], patch_ops[1], '', extra)
                p.peg(words[1], *map(int, words[2:]))
                p.length_ns = 1_000_000_000
                p.write()
                rewritten = lines(p.files()[0], ('peg',))[0]
                ok(rewritten == ' '.join(words), '%s/%s: writer preserves every PEG field' % (fs, name))
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    f.writelines(rewritten + '\n' if line.startswith('peg ') else line for line in text)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%s/%s: golden PEG replays: rc=%d %s' % (fs, name, rc, err))
                print('selftest-recipes: ok PEG golden %d/%s hash=%s match=yes block_indep=yes' % (fs, name, kv['hash']))

        # A filter envelope carries its filter in a recipe; validation waits for the
        # completed block, and the writer resolves the reference frequency once.
        fenv_short = '20 600 250 200 4800 1000'
        fenv_full = '0 0 1000 0 -2400 2000 440000'
        filter_text = 'filter lp 300000 1500'
        pair_fenv = pair.replace('recipe x', 'recipe w4-fenv').replace('note n',
                         'fenv ' + fenv_short + '\n' + filter_text + '\nnote n')
        routed_fenv = routed_peg.replace('recipe w4-routed-peg', 'recipe w4-routed-fenv\nfenv ' +
                                         fenv_full + '\n' + filter_text)
        filter_only = pair.replace('recipe x', 'recipe w4-filter').replace('note n', filter_text + '\nnote n')
        with open(os.path.join(tmp, 'fenv-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(pair_fenv + routed_fenv + filter_only)
        F = load_recipes(os.path.join(tmp, 'fenv-recipes.txt'))
        default_fenv = tuple(map(int, fenv_short.split())) + (261626,)
        ok(F['w4-fenv'][3] == {'filter': ('lp', 300000, 1500), 'fenv': default_fenv},
           'fenv before its filter loads with the default reference')
        ok(F['w4-routed-fenv'][3]['fenv'] == tuple(map(int, fenv_full.split())) and
           all(k in F['w4-routed-fenv'][3] for k in ('ops', 'filter', 'lfo', 'peg')),
           'filter envelope metadata coexists with route, filter, LFO and PEG')
        ok(F['w4-filter'][3] == {'filter': ('lp', 300000, 1500)}, 'a filter-only recipe has no invented route or fenv')
        for depth in (-9600, 9600):
            endpoints = (0, 60000, 1000, 60000, depth, 2000, 4294967295)
            ok(fenv_values(endpoints) == endpoints, 'fenv accepts field endpoints and signed depth %d' % depth)
        ok(fenv_values((0, 0, 0, 0, 0, 0, 1)) == (0, 0, 0, 0, 0, 0, 1), 'fenv accepts minimum reference')
        bad_fenvs = ['20 600 250 200 4800', fenv_short + ' 261626 1',
                     '20 600 250 200 +4800 1000', '20 600 250 200 4800.0 1000',
                     '２０ 600 250 200 4800 1000']
        for i, (lo, hi) in enumerate(FENV_LIMITS):
            for value in (lo - 1, hi + 1):
                fields = list(default_fenv)
                fields[i] = value
                bad_fenvs.append(' '.join(map(str, fields)))
        for value in bad_fenvs:
            msg = fault(pair.replace('note n', 'fenv ' + value + '\n' + filter_text + '\nnote n'))
            ok('fault.txt:4: ' in msg and 'fenv' in msg, 'bad fenv fields fail at their line: %s (%s)' % (value, msg))
        ok('a second fenv line' in fault(pair_fenv.replace('note n', 'fenv ' + fenv_short + '\nnote n')),
           'a recipe refuses duplicate fenv lines')
        msg = fault(pair.replace('note n', 'fenv ' + fenv_short + '\nnote n'))
        ok('fault.txt:4: ' in msg and 'fenv needs a filter statement' in msg,
           'a recipe without its fenv filter fails at the fenv line')
        for value in ('off 300000 1500', 'lp -1 1500', 'lp 4294967296 1500', 'lp 300000 -1',
                      'lp 300000 65536', 'lp 300000', 'lp 300000 1500 0', 'lp 300.0 1500'):
            msg = fault(pair.replace('note n', 'filter ' + value + '\nnote n'))
            ok('fault.txt:4: ' in msg and 'filter' in msg, 'bad filter fields fail at their line: %s (%s)' % (value, msg))
        ok('a second filter line' in fault(filter_only.replace('note n', filter_text + '\nnote n')),
           'a recipe refuses duplicate filters')
        ok(raises(ValueError, fenv_values, (True,) + default_fenv[1:]), 'fenv rejects bool as an integer field')
        ok(raises(ValueError, fenv_row, 'bad name', *default_fenv), 'fenv writer refuses a bad patch name')
        ok(raises(ValueError, patch_headers, 't', *recipe('epiano')[:3], extra={'fenv': default_fenv}),
           'standalone patch headers require the fenv filter')
        RECIPES.update(F)
        ok(recipe('w4-fenv', time=2, m_index=0.5)[3] == F['w4-fenv'][3],
           'operator deltas and time leave filter/fenv fields alone')
        ok(randomize('w4-fenv', 7, {'m_index': (0, 1)}) == randomize('epiano', 7, {'m_index': (0, 1)}),
           'filter/fenv-only metadata accepts operator randomization')
        header_kinds = ('patch', 'route', 'filter', 'lfo', 'peg', 'fenv')
        for name in F:
            p = one_note(name, *recipe(name))
            rc, kv, err = harness(p.files(), os.path.join(tmp, name + '-merged.tl'))
            ok(rc == 0 and kv.get('block_indep') == 'yes', '%s renders with filter/fenv: rc=%d %s' % (name, rc, err))
            authored = lines(p.files()[0], header_kinds)
            ok(values(authored) == values(lines(os.path.join(tmp, name + '-merged.tl'), header_kinds)),
               'the reader and writer preserve all filter/fenv headers')
            with open(p.files()[0], encoding='utf-8') as f:
                text = [line for line in f if not line.split() or line.split()[0] not in header_kinds]
            text.append('recipe t %s\n' % name)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.writelines(text)
            ok(expand_timeline(src, dst) == 1 and lines(dst, header_kinds) == authored,
               'expand writes exactly the filter/fenv piece headers')
            erc, ekv, err = harness([dst, p.files()[1]])
            ok(erc == 0 and ekv.get('hash') == kv['hash'], 'expanded filter/fenv recipe keeps its render hash')
            p.fenv('t', *map(int, fenv_full.split()))
            p.write()
            ok(lines(p.files()[0], ('fenv',)) == [fenv_row('t', *map(int, fenv_full.split()))],
               'Piece.fenv replaces a recipe envelope or adds one to a filter-only recipe')
            p.filter('t', 'hp', 800, 0.75)
            p.write()
            ok(lines(p.files()[0], ('filter',)) == [filter_row('t', 'hp', 800000, 750)] and
               't' not in p.recipe_filters, 'an explicit filter overrides the recipe exactly once')
            p.patch('t', *recipe('epiano'))
            ok(p.filters['t'] == ('hp', 800000, 750) and 't' not in p.fenvs,
               'patch replacement keeps an independent filter but removes old fenv metadata')
            p.patch('t', *recipe(name))
            ok(p.filters['t'] == ('lp', 300000, 1500) and 't' in p.recipe_filters,
               'a later filter-bearing recipe replaces an earlier manual filter')
            p.patch('t', *recipe('epiano'))
            ok('t' not in p.filters and 't' not in p.recipe_filters,
               'patch replacement removes its previous recipe-owned filter')
        p = one_note('w4-late-filter', *recipe('epiano'))
        ok(raises(ValueError, p.fenv, 'missing', *default_fenv), 'Piece.fenv refuses an unknown patch')
        p.fenv('t', *default_fenv)
        ok('fenv needs a filter statement' in raises(ValueError, p.write_session),
           'Piece requires the filter when writing the completed patch')
        p.filter('t', 'lp', 300, 1.5)
        p.write()
        ok(len(lines(p.files()[0], ('fenv', 'filter'))) == 2, 'a filter setter after fenv satisfies final validation')
        MUSIC = tmp
        for name in F:
            make = lambda name=name: one_note(name + '-keys', *recipe(name))
            make()
            k = keys_version(make)()
            k.write()
            ok(k.filters == {'t': ('lp', 300000, 1500)} and k.recipe_filters == {'t'} and
               k.fenvs == ({'t': F[name][3]['fenv']} if 'fenv' in F[name][3] else {}),
               'keys preserve recipe-owned filters and fenv metadata')
        def manual_fenv_keys():
            p = one_note('w4-manual-fenv-keys', *recipe('epiano'))
            p.fenv('t', *default_fenv)
            p.filter('t', 'lp', 300, 1.5)
            return p
        make = manual_fenv_keys
        make()
        k = keys_version(make)()
        k.write()
        ok(k.filters == {'t': ('lp', 300000, 1500)} and not k.recipe_filters and k.fenvs == {'t': default_fenv},
           'keys preserve a manual filter required by fenv')
        def manual_headers_keys():
            p = one_note('keys-manual-headers', *recipe('epiano'))
            p.filter('t', 'hp', 800, 0.75)
            p.fx(dcblock=5000, chorus=(15000, 3000, 800), delay=(150000, 8192, 6000),
                 reverb=(500, 500), limiter=(10, 100), softclip=True, gain=-30, dither=12345)
            p.send('a', 0, 0, 'reverb', 4000)
            p.write()
            return p
        m = manual_headers_keys()
        header_kinds = ('filter', 'dcblock', 'chorus', 'flanger', 'delay', 'pingpong', 'reverb', 'compressor', 'limiter', 'softclip', 'gain', 'dither', 'crush', 'decodable')
        ok(m.fxh['gain'] == (-30,) and m.fxh['dither'] == (12345,) and
           values(lines(m.files()[0], ('softclip', 'gain', 'dither'))) == [['softclip'], ['gain', '-30'], ['dither', '12345']],
           'fx writes gain then dither after softclip')
        for bad in (dict(gain=241), dict(gain=-601), dict(gain=1.0), dict(gain=True), dict(dither=0),
                    dict(dither=1 << 32), dict(dither='7')):
            q = Piece('fx-refusal', 120, 4, ['fx refusal'])
            try:
                q.fx(dcblock=5000, **bad)
                ok(False, 'fx refuses %r' % bad)
            except ValueError:
                ok(not q.fxh, 'fx refuses %r before storing any part' % bad)
        for mirror in (False, True):
            k = keys_version(manual_headers_keys, mirror=mirror)()
            k.write()
            ok(k.filters == m.filters and not k.recipe_filters and k.fxh == m.fxh and
               values(lines(k.files()[0], header_kinds)) == values(lines(m.files()[0], header_kinds)),
               'keys and mirror preserve every manual filter and effects header')
            rc, kv, err = harness(k.files())
            ok(rc == 0 and kv.get('block_indep') == 'yes' and kv.get('clipped') == '0',
               'keys manual filters and effects render: rc=%d %s' % (rc, err))
            k.filters.clear()
            k.fxh.clear()
            ok(bool(m.filters) and bool(m.fxh), 'keys header dictionaries do not alias the source')
        MUSIC = music
        print('selftest-recipes: ok fenv: raw filter metadata, pair/routed recipes, bounds, refusals, expansion, overrides and keys')
        print('selftest-recipes: ok keys: manual filters and every effects header, keys and mirror render unclipped')

        for fs in (48000, 44100):
            for name in ('fenvpluck', 'ftrack'):
                with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                words = next(line.split() for line in text if line.startswith('fenv '))
                fw = next(line.split() for line in text if line.startswith('filter '))
                p = Piece('w4b-golden', 120, 4, [], fs=fs)
                p.dir = os.path.join(tmp, 'w4b-golden')
                patch_ops = [list(map(int, line.split()[3:])) for line in text if line.startswith('patch ')]
                routing = next((line.split() for line in text if line.startswith('route ')), None)
                extra = {'filter': (fw[2], *map(int, fw[3:]))}
                if routing:
                    extra.update(ops=int(routing[2]), carriers=int(routing[3]), more=patch_ops[2:],
                                 edges=[tuple(map(int, re.split('[>:]', edge))) for edge in routing[4:]])
                p.patch(words[1], patch_ops[0], patch_ops[1], '', extra)
                p.fenv(words[1], *map(int, words[2:]))
                p.length_ns = 1_000_000_000
                p.write()
                rewritten = lines(p.files()[0], ('fenv',))[0]
                ok(rewritten == ' '.join(words), '%s/%s: writer preserves every fenv field' % (fs, name))
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    f.writelines(rewritten + '\n' if line.startswith('fenv ') else line for line in text)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%s/%s: golden fenv replays: rc=%d %s' % (fs, name, rc, err))
                print('selftest-recipes: ok fenv golden %d/%s hash=%s match=yes block_indep=yes' % (fs, name, kv['hash']))
        # filter delay metadata reaches every recipe writer.
        delayed = pair.replace('recipe x', 'recipe w5-delay\neg 1 60 0 0\neg 0 30 0 0')
        routed_delay = SELFTEST_RECIPES.split('recipe t2-w2')[0].replace(
            'recipe t2-routed', 'recipe w5-routed\neg 3 45 0 0')
        with open(os.path.join(tmp, 'eg-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(delayed + routed_delay)
        E = load_recipes(os.path.join(tmp, 'eg-recipes.txt'))
        RECIPES.update(E)
        ok(E['w5-delay'][3] == {'eg': {1: (60, 0, 0), 0: (30, 0, 0)}} and
           E['w5-routed'][3]['eg'] == {3: (45, 0, 0)}, 'EG before patch/route resolves operators at block end')
        for fields in ('', '0', '0 30 0', '0 30 0 0 0', '-1 30 0 0', '4 30 0 0',
                       '2 30 0 0', '0 -1 0 0', '0 60001 0 0',
                       '+0 30 0 0', '0 30.0 0 0', '０ 30 0 0'):
            ok(bool(fault(pair.replace('note n', 'eg ' + fields + '\nnote n'))), 'invalid EG refused: ' + fields)
        ok('second eg' in fault(delayed.replace('note n', 'eg 0 0 0 0\nnote n')),
           'even a zero duplicate EG is refused')
        ok(eg_values((3, 60000, 0, 0), 4) == (3, 60000, 0, 0), 'EG accepts the upper delay and operator bounds')
        ok(bool(raises(ValueError, eg_values, (True, 0, 0, 0))) and
           bool(raises(ValueError, eg_row, 'bad name', 0, 30, 0, 0)), 'EG Python helpers reject bool and bad names')
        ok(recipe('w5-delay', time=0.15)[3]['eg'] == {1: (9, 0, 0), 0: (4, 0, 0)} and
           recipe('w5-delay', time=10000)[3]['eg'] == {1: (60000, 0, 0), 0: (60000, 0, 0)},
           'recipe time scales delay with existing half-even rounding and a 60000ms clamp')
        for name in ('w5-delay', 'w5-routed'):
            p = one_note(name, *recipe(name))
            expected = [eg_row('t', i, *v) for i, v in sorted(E[name][3]['eg'].items())]
            ok(lines(p.files()[0], ('eg',)) == expected, name + ': session writes EG in operator order')
            merged = os.path.join(tmp, 'eg-merged.tl')
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and lines(merged, ('eg',)) == expected,
               name + ': delay renders and canonical EG agrees: ' + err)
            rc2, kv2, err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes', name + ': merged delay record replays: ' + err2)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('patch', 'route', 'eg')) == lines(p.files()[0], ('patch', 'route', 'eg')),
               name + ': expansion preserves delay metadata')
            buf = __import__('io').StringIO()
            with __import__('contextlib').redirect_stdout(buf):
                list_recipes()
            ok(all(row in buf.getvalue() for row in (eg_row(name, i, *v) for i, v in E[name][3]['eg'].items())),
               name + ': recipe listing includes EG')
        p = one_note('w5-eg-zero', *recipe('epiano'))
        base = open(p.files()[0], encoding='utf-8').read()
        rc, plain, err = harness(p.files())
        p.eg('t', 0, 50)
        p.eg('t', 0, 70)
        p.write()
        ok(lines(p.files()[0], ('eg',)) == ['eg t 0 70 0 0'], 'Piece.eg replaces one operator setting')
        before = dict(p.egs['t'])
        ok(bool(raises(ValueError, p.eg, 't', 2, 10)) and p.egs['t'] == before and
           bool(raises(ValueError, p.eg, 'absent', 0, 10)), 'invalid Piece.eg leaves its old setting unchanged')
        p.eg('t', 0, 0)
        p.write()
        rc, zero, err = harness(p.files())
        ok(open(p.files()[0], encoding='utf-8').read() == base and zero.get('hash') == plain.get('hash') and not p.egs,
           'zero EG removes metadata, preserving exact old header and PCM')
        p.eg('t', 1, 10)
        p.patch('t', *recipe('epiano'))
        ok(not p.egs, 'patch replacement removes old EG metadata')
        MUSIC = tmp
        def delayed_keys():
            return one_note('w5-eg-keys', *recipe('w5-delay'))
        m = delayed_keys()
        for mirror in (False, True):
            k = keys_version(delayed_keys, mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok(k.egs == m.egs and rc == 0 and kv.get('block_indep') == 'yes',
               'keys and mirror preserve delay and render: ' + err)
            k.egs['t'].clear()
            ok(bool(m.egs['t']), 'keys EG dictionaries do not alias their source')
        MUSIC = music
        for fs in (48000, 44100):
            with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), 'egdelay.tl'), encoding='utf-8') as f:
                text = f.readlines()
            original = [line.strip() for line in text if line.startswith('eg ')]
            rewritten = [eg_row(row.split()[1], *map(int, row.split()[2:])) for row in original]
            ok(rewritten == original, '%d/egdelay: writer preserves every golden EG field' % fs)
            with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                f.writelines(eg_row(line.split()[1], *map(int, line.split()[2:])) + '\n'
                             if line.startswith('eg ') else line for line in text)
            rc, kv, err = harness([dst])
            ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
               '%d/egdelay: rewritten golden replays: %s' % (fs, err))
        print('selftest-recipes: ok filter delay: load, bounds, time, expansion, listing, keys, zero identity and both goldens')
        # filter second decay: ordering is checked after all operator fields and after tweaks.
        d2pair = pair.replace('level=10', 'level=10 s=12').replace('index=1.0', 'index=1.0 s=18')
        d2text = d2pair.replace('recipe x', 'recipe w5-decay\neg 1 10 300 600\neg 0 0 240 480')
        jumptext = d2pair.replace('recipe x', 'recipe w5-jump\neg 0 0 0 480\neg 1 0 120 180')
        d2route = SELFTEST_RECIPES.split('recipe t2-w2')[0].replace('recipe t2-routed',
                                                                 'recipe w5-decay-route\neg 3 0 240 750')
        with open(os.path.join(tmp, 'decay2-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(d2text + jumptext + d2route)
        D = load_recipes(os.path.join(tmp, 'decay2-recipes.txt'))
        RECIPES.update(D)
        ok(D['w5-decay'][3]['eg'] == {1: (10, 300, 600), 0: (0, 240, 480)} and
           D['w5-jump'][3]['eg'] == {0: (0, 0, 480), 1: (0, 120, 180)},
           'timed, instantaneous and equal-level second decay load before operator sustains')
        for fields in ('0 0 -1 480', '0 0 60001 480', '0 0 100 -1', '0 0 100 1201',
                       '0 0 100 119', '0 0 0 119', '0 0 1 0'):
            msg = fault(d2pair.replace('recipe x', 'recipe x\neg ' + fields))
            ok(bool(msg), 'invalid second decay refused: ' + fields)
        ok(eg_values((3, 60000, 60000, 1200), 4, 65535) == (3, 60000, 60000, 1200),
           'second decay accepts limits and compares against clamped sustain1')
        ok(bool(raises(ValueError, eg_values, (0, 0, 1, 1199), 2, 65535)),
           'second decay below clamped sustain1 is refused')
        ok(eg_values((0, 30, 0, 0), 2, 1200) == (0, 30, 0, 0),
           'delay alone stays legal with nonzero sustain1')
        ok(recipe('w5-decay', time=0.5)[3]['eg'] == {1: (5, 150, 600), 0: (0, 120, 480)} and
           recipe('w5-decay', time=10000)[3]['eg'][1] == (60000, 60000, 600),
           'time scales delay and decay2, clamps times and preserves sustain2')
        ok(bool(raises(TypeError, recipe, 'w5-decay', c_s=37)) and
           recipe('w5-decay', c_s=-6)[3]['eg'][0] == (0, 240, 480),
           'recipe sustain deltas are checked against the unchanged second target')
        with open(src, 'w', encoding='utf-8', newline='\n') as f:
            f.write('recipe t w5-decay c_s=37\n')
        ok('SUSTAIN2' in raises(SystemExit, expand_timeline, src, dst),
           'an incompatible sustain delta is refused by expansion with a source location')
        for name in ('w5-decay', 'w5-jump', 'w5-decay-route'):
            p = one_note(name, *recipe(name))
            expected = [eg_row('t', i, *v) for i, v in sorted(D[name][3]['eg'].items())]
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and lines(merged, ('eg',)) == expected,
               name + ': canonical second decay renders: ' + err)
            rc2, kv2, err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes', name + ': second decay replays: ' + err2)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s time=0.5\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('eg',)) == [eg_row('t', i, *v) for i, v in sorted(recipe(name, time=0.5)[3]['eg'].items())],
               name + ': expansion includes scaled second decay')
        p = one_note('w5-decay-setter', *recipe('w5-decay'))
        before = dict(p.egs['t'])
        ok(bool(raises(ValueError, p.eg, 't', 0, 0, 100, 119)) and p.egs['t'] == before,
           'invalid second-decay replacement keeps the old setting')
        ok(bool(raises(ValueError, patch_headers, 'x', *recipe('epiano')[:3],
                       extra={'eg': {0: (0, 10, 1)}})), 'patch_headers refuses a second target above sustain gain')
        MUSIC = tmp
        def decay_keys():
            return one_note('w5-decay-keys', *recipe('w5-decay'))
        m = decay_keys()
        for mirror in (False, True):
            k = keys_version(decay_keys, mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok(k.egs == m.egs and rc == 0 and kv.get('block_indep') == 'yes',
               'keys and mirror preserve second decay and render: ' + err)
        MUSIC = music
        for fs in (48000, 44100):
            for name in ('egdecay2', 'egjump'):
                with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    f.writelines(eg_row(line.split()[1], *map(int, line.split()[2:])) + '\n'
                                 if line.startswith('eg ') else line for line in text)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%d/%s: rewritten second-decay golden replays: %s' % (fs, name, err))
        print('selftest-recipes: ok filter second decay: ordered targets, instant/equal cases, time, deltas, keys and goldens')
        # filter left index scaling shares each operator's existing break point.
        kstext = pair.replace('recipe x', 'recipe w5-left\nks 1 120\neg 0 30 0 0').replace(
            'index=1.0', 'index=1.0 ks_break=440.0 ks_slope=6.0')
        ksroute = SELFTEST_RECIPES.split('recipe t2-w2')[0].replace('recipe t2-routed',
                                                                 'recipe w5-left-route\nks 3 120\nks 0 60')
        with open(os.path.join(tmp, 'ks-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(kstext + ksroute)
        K = load_recipes(os.path.join(tmp, 'ks-recipes.txt'))
        RECIPES.update(K)
        ok(K['w5-left'][3] == {'eg': {0: (30, 0, 0)}, 'ks': {1: 120}} and
           K['w5-left-route'][3]['ks'] == {3: 120, 0: 60}, 'left slopes resolve after pair and routed operators')
        for fields in ('', '1', '1 120 0', '-1 120', '4 120', '2 120', '1 -1', '1 1201',
                       '+1 120', '1 120.0', '１ 120'):
            ok(bool(fault(pair.replace('note n', 'ks ' + fields + '\nnote n'))), 'invalid left slope refused: ' + fields)
        ok('second ks' in fault(kstext.replace('note n', 'ks 1 0\nnote n')), 'duplicate zero left slope is refused')
        ok(ks_values((3, 1200), 4) == (3, 1200) and ks_values((0, 0), 2) == (0, 0),
           'left slope accepts both bounds')
        ok(bool(raises(ValueError, ks_values, (1, True))) and bool(raises(ValueError, ks_row, 'bad name', 1, 120)),
           'left slope Python helpers reject bool and bad names')
        ok(recipe('w5-left', time=2)[3]['ks'] == {1: 120} and recipe('w5-left', m_ks_break=-220)[3]['ks'] == {1: 120},
           'time and right-side key-scaling deltas preserve the left slope')
        for name in ('w5-left', 'w5-left-route'):
            p = one_note(name, *recipe(name))
            expected = [ks_row('t', i, v) for i, v in sorted(K[name][3]['ks'].items())]
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and lines(merged, ('ks',)) == expected,
               name + ': left slopes render and canonical order agrees: ' + err)
            rc2, kv2, err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes', name + ': left slope merged record replays: ' + err2)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('patch', 'route', 'eg', 'ks')) == lines(p.files()[0], ('patch', 'route', 'eg', 'ks')),
               name + ': expansion preserves EG and KS')
            buf = __import__('io').StringIO()
            with __import__('contextlib').redirect_stdout(buf):
                list_recipes()
            ok(all(row in buf.getvalue() for row in (ks_row(name, i, v) for i, v in K[name][3]['ks'].items())),
               name + ': listing includes left slopes')
        p = one_note('w5-left-zero', *recipe('epiano'))
        base = open(p.files()[0], encoding='utf-8').read()
        rc, plain, err = harness(p.files())
        p.ks('t', 1, 60)
        p.ks('t', 1, 120)
        p.write()
        ok(lines(p.files()[0], ('ks',)) == ['ks t 1 120'], 'Piece.ks replaces one operator setting')
        before = dict(p.left_slopes['t'])
        ok(bool(raises(ValueError, p.ks, 't', 2, 120)) and p.left_slopes['t'] == before and
           bool(raises(ValueError, p.ks, 'absent', 0, 120)), 'invalid Piece.ks preserves the old setting')
        p.ks('t', 1, 0)
        p.write()
        rc, zero, err = harness(p.files())
        ok(open(p.files()[0], encoding='utf-8').read() == base and zero.get('hash') == plain.get('hash') and not p.left_slopes,
           'zero KS preserves exact old header and PCM')
        p.ks('t', 1, 120)
        p.patch('t', *recipe('epiano'))
        ok(not p.left_slopes, 'patch replacement removes old left slopes')
        p = one_note('w5-extension-order', *recipe('w5-left'))
        rows = [line.split()[0] for line in open(p.files()[0], encoding='utf-8') if line.startswith(('eg ', 'ks ', 'voice '))]
        ok(rows == ['eg', 'ks', 'voice'], 'EG precedes KS, then voices')
        MUSIC = tmp
        def left_keys():
            return one_note('w5-left-keys', *recipe('w5-left'))
        m = left_keys()
        for mirror in (False, True):
            k = keys_version(left_keys, mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok(k.left_slopes == m.left_slopes and k.egs == m.egs and rc == 0 and kv.get('block_indep') == 'yes',
               'keys and mirror preserve both extensions and render: ' + err)
            k.left_slopes['t'].clear()
            ok(bool(m.left_slopes['t']), 'keys left-slope dictionaries do not alias the source')
        MUSIC = music
        for fs in (48000, 44100):
            with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), 'ksleft.tl'), encoding='utf-8') as f:
                text = f.readlines()
            with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                f.writelines(ks_row(line.split()[1], *map(int, line.split()[2:])) + '\n'
                             if line.startswith('ks ') else line for line in text)
            rc, kv, err = harness([dst])
            ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
               '%d/ksleft: rewritten golden replays: %s' % (fs, err))
        print('selftest-recipes: ok filter left slope: bounds, duplicates, metadata, replacement, zero identity, keys and goldens')
        # operator extension phase shapes: the loader and recipe deltas share the staged set.
        wave_text = ''.join(pair.replace('recipe x', 'recipe w6-wave%d' % wave).replace('level=10', 'level=10 wave=%d' % wave)
                            for wave in (5, 6))
        wave_route = SELFTEST_RECIPES.split('recipe t2-w2')[0].replace('recipe t2-routed', 'recipe w6-wave-route').replace(
            'wave=1', 'wave=5').replace('ratio=7.0 index=2.0', 'ratio=7.0 index=2.0 wave=6')
        with open(os.path.join(tmp, 'wave-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(wave_text + wave_route)
        W = load_recipes(os.path.join(tmp, 'wave-recipes.txt'))
        RECIPES.update(W)
        ok([recipe('w6-wave%d' % w)[0][11] for w in (5, 6)] == [5, 6] and
           [o[11] for o in recipe('w6-wave-route')[3]['more']] == [5, 6],
           'alternating and camel load for pair and routed operators')
        ok(recipe('epiano', c_wave=5, m_wave=6)[0][11] == 5 and
           recipe('w6-wave-route', o2_wave=1, o3_wave=-1)[3]['more'][0][11] == 6,
           'wave deltas can select each new phase shape')
        for wave in (-1, 12, 13, 14, 65535, 5.5):
            ok(bool(fault(pair.replace('level=10', 'level=10 wave=%s' % wave))) and
               bool(raises(TypeError, recipe, 'epiano', c_wave=wave)), 'unsupported wave loads/deltas refused: %s' % wave)
        for name in ('w6-wave5', 'w6-wave6', 'w6-wave-route'):
            p = one_note(name, *recipe(name))
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and
               values(lines(merged, ('patch',))) == values(lines(p.files()[0], ('patch',))),
               name + ': wave field renders and canonical patch agrees: ' + err)
            rc2, kv2, err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes', name + ': phase-shape record replays: ' + err2)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('patch', 'route')) == lines(p.files()[0], ('patch', 'route')),
               name + ': expansion preserves waveform codes')
        MUSIC = tmp
        def wave_keys():
            return one_note('w6-wave-keys', *recipe('w6-wave6'))
        m = wave_keys()
        for mirror in (False, True):
            k = keys_version(wave_keys, mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok(k.patches['t'][0][11] == 6 and rc == 0 and kv.get('block_indep') == 'yes',
               'keys and mirror preserve the new waveform and render: ' + err)
        MUSIC = music
        for fs in (48000, 44100):
            for name in ('alternating', 'camel'):
                with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    f.writelines(patch_row(line.split()[1], int(line.split()[2]), list(map(int, line.split()[3:]))) + '\n'
                                 if line.startswith('patch ') else line for line in text)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%d/%s: rewritten phase-shape golden replays: %s' % (fs, name, err))
        print('selftest-recipes: ok operator extension alternating/camel: loader/delta bounds, route, expansion, keys and goldens')
        # operator extension pink uses only the existing WAVE/FREE fields, with no noise parameter line.
        pinktext = ''.join(pair.replace('recipe x', 'recipe w6-pink%d' % free).replace(
            'level=10', 'level=10 wave=9 free=%d r=20' % free).replace('index=1.0', 'index=1.0 r=20')
                           for free in (0, 1, 2))
        pinkroute = SELFTEST_RECIPES.split('recipe t2-w2')[0].replace('recipe t2-routed', 'recipe w6-pink-route').replace(
            'wave=1', 'wave=9')
        with open(os.path.join(tmp, 'pink-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(pinktext + pinkroute)
        P = load_recipes(os.path.join(tmp, 'pink-recipes.txt'))
        RECIPES.update(P)
        ok(all(len(P['w6-pink%d' % free]) == 3 for free in (0, 1, 2)) and
           recipe('w6-pink-route')[3]['more'][0][11] == 9, 'pink needs no extra metadata for pair or route')
        ok(recipe('epiano', c_wave=9)[0][11] == 9 and recipe('w6-pink-route', o2_wave=-3)[3]['more'][0][11] == 6,
           'recipe deltas select pink and move it back to a phase shape')
        ok(bool(fault(pair.replace('level=10', 'level=10 wave=9').replace('note n', 'noise 0 4\nnote n'))),
           'pink has no noise parameter statement')
        pink_hashes = []
        for free in (0, 1, 2):
            name = 'w6-pink%d' % free
            p = one_note(name, *recipe(name))
            p.on('a', 800_000_000, 0, 'A2', 24000)
            p.off('a', 900_000_000, 0)
            p.write()
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and
               values(lines(merged, ('patch',))) == values(lines(p.files()[0], ('patch',))),
               name + ': pink FREE field renders and canonical patch agrees: ' + err)
            rc2, kv2, err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes', name + ': pink idle-gap record replays: ' + err2)
            pink_hashes.append(kv.get('hash'))
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('patch',)) == lines(p.files()[0], ('patch',)), name + ': expansion preserves pink and FREE')
        ok(len(set(pink_hashes)) == 3, 'pink FREE0/1/2 produce distinct idle-gap records')
        p = one_note('w6-pink-route', *recipe('w6-pink-route'))
        rc, kv, err = harness(p.files())
        ok(rc == 0 and kv.get('block_indep') == 'yes', 'routed pink renders: ' + err)
        MUSIC = tmp
        def pink_keys():
            return one_note('w6-pink-keys', *recipe('w6-pink2'))
        pink_keys()
        for mirror in (False, True):
            k = keys_version(pink_keys, mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok(k.patches['t'][0][11:16:4] == [9, 2] and rc == 0 and kv.get('block_indep') == 'yes',
               'keys and mirror preserve pink FREE2 and render: ' + err)
        MUSIC = music
        for fs in (48000, 44100):
            for name in ('pink', 'pinkfree'):
                with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    f.writelines(patch_row(line.split()[1], int(line.split()[2]), list(map(int, line.split()[3:]))) + '\n'
                                 if line.startswith('patch ') else line for line in text)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%d/%s: rewritten pink golden replays: %s' % (fs, name, err))
        print('selftest-recipes: ok operator extension pink: wave/delta, route, FREE0/1/2 idle gap, expansion, keys and goldens')
        # operator extension coloured noise: default omission never bypasses wave/presence validation.
        colourtext = ''.join(pair.replace('recipe x', 'recipe w6-colour%d\nnoise 0 7' % wave).replace(
            'level=10', 'level=10 wave=%d' % wave) for wave in (10, 11))
        colourroute = SELFTEST_RECIPES.split('recipe t2-w2')[0].replace('recipe t2-routed',
            'recipe w6-colour-route\nnoise 3 15\nnoise 2 1').replace('wave=1', 'wave=10').replace(
            'ratio=7.0 index=2.0', 'ratio=7.0 index=2.0 wave=11')
        with open(os.path.join(tmp, 'colour-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(colourtext + colourroute)
        C = load_recipes(os.path.join(tmp, 'colour-recipes.txt'))
        RECIPES.update(C)
        ok(C['w6-colour10'][3]['noise'] == {0: 7} and C['w6-colour-route'][3]['noise'] == {3: 15, 2: 1},
           'colour metadata validates after pair/routed waves and retains the authored operator order')
        for fields in ('', '0', '0 4 4', '-1 4', '4 4', '2 4', '0 0', '0 16', '+0 4', '0 4.0', '０ 4'):
            ok(bool(fault(pair.replace('level=10', 'level=10 wave=10').replace('note n', 'noise ' + fields + '\nnote n'))),
               'invalid noise statement refused: ' + fields)
        for wave in (0, 4, 5, 6, 9):
            ok(bool(fault(pair.replace('level=10', 'level=10 wave=%d' % wave).replace('note n', 'noise 0 4\nnote n'))),
               'even default noise SHIFT is refused on wave%d' % wave)
        ok('second noise' in fault(colourtext.split('recipe w6-colour11')[0].replace('note n', 'noise 0 4\nnote n')),
           'default noise SHIFT still counts for duplicate detection')
        ok(noise_values((3, 15), 4, 11) == (3, 15) and noise_values((0, 1), 2, 10) == (0, 1),
           'noise SHIFT and operator bounds are accepted')
        ok(bool(raises(ValueError, noise_values, (0, True))) and bool(raises(ValueError, noise_row, 'bad name', 0, 4)),
           'noise Python helpers reject bool and bad names')
        ok(recipe('w6-colour10', time=2)[3]['noise'] == {0: 7} and recipe('w6-colour10', c_wave=1)[0][11] == 11,
           'time preserves SHIFT and colour wave deltas keep metadata')
        ok(bool(raises(TypeError, recipe, 'w6-colour10', c_wave=-5)),
           'a wave delta cannot leave colour-only metadata on a phase shape')
        with open(src, 'w', encoding='utf-8', newline='\n') as f:
            f.write('recipe t w6-colour10 c_wave=-5\n')
        ok('needs wave 10 or 11' in raises(SystemExit, expand_timeline, src, dst),
           'expansion reports an incompatible colour wave delta at its source')
        for name in ('w6-colour10', 'w6-colour11', 'w6-colour-route'):
            p = one_note(name, *recipe(name))
            expected = [noise_row('t', i, v) for i, v in sorted(C[name][3]['noise'].items())]
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and lines(merged, ('noise',)) == expected,
               name + ': colour shift renders and canonical order agrees: ' + err)
            rc2, kv2, err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes', name + ': colour record replays: ' + err2)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('patch', 'route', 'noise')) == lines(p.files()[0], ('patch', 'route', 'noise')),
               name + ': expansion preserves noise SHIFT')
        p = one_note('w6-colour-default', *recipe('epiano', c_wave=10))
        base = open(p.files()[0], encoding='utf-8').read()
        rc, plain, err = harness(p.files())
        p.noise('t', 0, 1)
        p.noise('t', 0, 15)
        p.write()
        ok(lines(p.files()[0], ('noise',)) == ['noise t 0 15'], 'Piece.noise replaces one operator setting')
        before = dict(p.noise_shifts['t'])
        ok(bool(raises(ValueError, p.noise, 't', 1, 4)) and p.noise_shifts['t'] == before and
           bool(raises(ValueError, p.noise, 't', 2, 4)) and bool(raises(ValueError, p.noise, 'absent', 0, 4)),
           'invalid Piece.noise preserves its previous setting, including a default-value request')
        p.noise('t', 0, 4)
        p.write()
        rc, default, err = harness(p.files())
        ok(open(p.files()[0], encoding='utf-8').read() == base and default.get('hash') == plain.get('hash') and not p.noise_shifts,
           'explicit default SHIFT restores exact absent-statement header and PCM')
        base_fields = recipe('epiano', c_wave=10)[:3]
        ok(patch_headers('t', *base_fields, extra={'noise': {0: 4}}) == patch_headers('t', *base_fields),
           'patch_headers omits explicit default noise SHIFT')
        ok(bool(raises(ValueError, patch_headers, 't', *recipe('epiano')[:3], extra={'noise': {0: 4}})),
           'patch_headers validates the wave even when default SHIFT would be omitted')
        p.noise('t', 0, 7)
        p.eg('t', 0, 30)
        p.ks('t', 1, 120)
        p.write()
        rows = [line.split()[0] for line in open(p.files()[0], encoding='utf-8') if line.startswith(('eg ', 'ks ', 'noise ', 'voice '))]
        ok(rows == ['eg', 'ks', 'noise', 'voice'], 'EG and KS precede noise, then voices')
        p.patch('t', *recipe('epiano'))
        ok(not p.noise_shifts, 'patch replacement removes old colour metadata')
        MUSIC = tmp
        def colour_keys():
            return one_note('w6-colour-keys', *recipe('w6-colour10'))
        m = colour_keys()
        for mirror in (False, True):
            k = keys_version(colour_keys, mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok(k.noise_shifts == m.noise_shifts and rc == 0 and kv.get('block_indep') == 'yes',
               'keys and mirror preserve colour metadata and render: ' + err)
            k.noise_shifts['t'].clear()
            ok(bool(m.noise_shifts['t']), 'keys colour dictionaries do not alias the source')
        MUSIC = music
        for fs in (48000, 44100):
            for name in ('colourlp', 'colourhp', 'colourfree'):
                with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    for line in text:
                        w = line.split()
                        f.write(patch_row(w[1], int(w[2]), list(map(int, w[3:]))) + '\n' if w[:1] == ['patch'] else
                                noise_row(w[1], *map(int, w[2:])) + '\n' if w[:1] == ['noise'] else line)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%d/%s: rewritten colour golden replays: %s' % (fs, name, err))
        print('selftest-recipes: ok operator extension colour: metadata, final-wave bounds, deltas, default identity, order, keys and goldens')
        # operator extension square/log-saw: the same field after the independent PM-jump gate.
        jumptext = ''.join(pair.replace('recipe x', 'recipe w6-jump%d' % wave).replace('level=10', 'level=10 wave=%d' % wave)
                           for wave in (7, 8))
        jumproute = SELFTEST_RECIPES.split('recipe t2-w2')[0].replace('recipe t2-routed', 'recipe w6-jump-route').replace(
            'wave=1', 'wave=7').replace('ratio=7.0 index=2.0', 'ratio=7.0 index=2.0 wave=8')
        with open(os.path.join(tmp, 'jump-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(jumptext + jumproute)
        J = load_recipes(os.path.join(tmp, 'jump-recipes.txt'))
        RECIPES.update(J)
        ok([recipe('w6-jump%d' % w)[0][11] for w in (7, 8)] == [7, 8] and
           [o[11] for o in recipe('w6-jump-route')[3]['more']] == [7, 8],
           'square/log-saw load in pair and routed operators')
        ok(recipe('epiano', c_wave=7, m_wave=8)[0][11] == 7 and
           recipe('w6-jump-route', o2_wave=1, o3_wave=-1)[3]['more'][0][11] == 8,
           'deltas reach all new shapes and move between square/log-saw')
        ok(all(recipe('epiano', c_wave=w)[0][11] == w for w in range(12)),
           'all twelve wave codes survive delta validation unchanged')
        for wave in (7, 8):
            ok(bool(fault(pair.replace('level=10', 'level=10 wave=%d' % wave).replace('note n', 'noise 0 4\nnote n'))),
               'square/log-saw cannot carry a colour parameter')
        for name in ('w6-jump7', 'w6-jump8', 'w6-jump-route'):
            p = one_note(name, *recipe(name))
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and
               values(lines(merged, ('patch', 'route'))) == values(lines(p.files()[0], ('patch', 'route'))),
               name + ': jump waveform renders and canonical headers agree: ' + err)
            rc2, kv2, err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes', name + ': jump waveform record replays: ' + err2)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('patch', 'route')) == lines(p.files()[0], ('patch', 'route')),
               name + ': expansion preserves jump waveform codes')
        MUSIC = tmp
        def jump_keys():
            return one_note('w6-jump-keys', *recipe('w6-jump-route'))
        jump_keys()
        for mirror in (False, True):
            k = keys_version(jump_keys, mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok([o[11] for o in k.routes['t'][0]['more']] == [7, 8] and rc == 0 and kv.get('block_indep') == 'yes',
               'keys and mirror preserve square/log-saw and render: ' + err)
        MUSIC = music
        for fs in (48000, 44100):
            for name in ('square', 'logsaw'):
                with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    f.writelines(patch_row(line.split()[1], int(line.split()[2]), list(map(int, line.split()[3:]))) + '\n'
                                 if line.startswith('patch ') else line for line in text)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%d/%s: rewritten jump waveform golden replays: %s' % (fs, name, err))
        print('selftest-recipes: ok operator extension square/log-saw: pair, route, delta bounds, expansion, keys and goldens')
        # operator extension: explicit amplitude edge kinds, with legacy PM triples and bytes retained.
        amtext = '''recipe w6b-am
route 2 1 1>0:256:am
carrier level=12 a=2 r=30
modulator ratio=1.37 a=3 r=30
note amplitude edge
'''
        ringtext = amtext.replace('w6b-am', 'w6b-ring').replace(':am', ':ring')
        mixedtext = '''recipe w6b-mixed
ks 2 120
route 4 1 3>2:128:ring 2>0:256:pm 1>0:192:am 0>3:64:am
carrier level=18 a=2 r=30
modulator ratio=1.37 a=3 r=30
op 2 ratio=2.0 index=1.5 fb=0.25 ks_break=440 ks_slope=6 a=4 r=30
op 3 ratio=0.5 a=5 r=30
note mixed edges with delayed back edge
'''
        with open(os.path.join(tmp, 'am-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(amtext + ringtext + mixedtext)
        A = load_recipes(os.path.join(tmp, 'am-recipes.txt'))
        RECIPES.update(A)
        ok(A['w6b-am'][3]['edges'] == [(1, 0, 256, 'am')] and
           A['w6b-ring'][3]['edges'] == [(1, 0, 256, 'ring')] and
           A['w6b-mixed'][3]['edges'] == [(0, 3, 64, 'am'), (1, 0, 192, 'am'), (2, 0, 256), (3, 2, 128, 'ring')],
           'amplitude kinds add one tuple field; explicit PM normalizes to triples in source/target order')
        ok(edge_values((1, 0, 256)) == (1, 0, 256, 'pm') and edge_text((1, 0, 256, 'pm')) == '1>0:256' and
           edge_text((1, 0, 256, 'ring')) == '1>0:256:ring', 'one edge normalizer preserves old PM tokens')
        for bad in ((1, 0), (1, 1, 256), (1, 0, True), (True, 0, 256), (1, 0, 256, 'nope'),
                    (1, 0, 257, 'am'), (1, 0, 257, 'ring'), (4, 0, 256)):
            ok(bool(raises(ValueError, edge_values, bad)), 'invalid raw edge refused: %r' % (bad,))
        for token in ('1>0:257:am', '1>0:257:ring', '1>0:0:am', '1>0:0:ring', '1>0:256:SYNC',
                      '1>0:256:AM', '1>0:256:nope', '1>0:256:am:x', '1>0:256:', '1>0:+1:am', '1>0:1.5:am',
                      '1>1:256:ring', '2>0:256:am', '1>0:256:am 1>0:256:ring', '1>0:256:pm 1>0:256:am'):
            ok(bool(fault(amtext.replace('1>0:256:am', token))), 'invalid amplitude route refused: ' + token)
        for kind in ('am', 'ring'):
            for arg in ('index=1', 'ks_break=440', 'ks_slope=12', 'level=6'):
                ok('changes nothing' in fault(amtext.replace(':am', ':' + kind).replace('ratio=1.37', 'ratio=1.37 ' + arg)),
                   kind + ' source refuses unused recipe field ' + arg)
            for arg in ('m_index', 'm_ks_break', 'm_ks_slope', 'm_level'):
                ok(bool(raises(TypeError, recipe, 'w6b-' + kind, **{arg: 1})), kind + ' source refuses unused delta ' + arg)
        ok(recipe('w6b-mixed', o2_index=1, o2_ks_slope=2)[3]['more'][0][2] == 2560,
           'an outgoing PM source still accepts index and right key-scaling deltas')
        ok(recipe('w6b-am', w1_0=1000)[3]['edges'] == [(1, 0, 256, 'am')] and
           recipe('w6b-ring', w1_0=-1000)[3]['edges'] == [(1, 0, 1, 'ring')] and
           recipe('w6b-mixed', w2_0=1000)[3]['edges'][2] == (2, 0, 1024),
           'weight deltas clamp amplitude edges to 1..256 and PM to 1..1024')
        ok(fault(amtext.replace('note amplitude edge', 'ks 1 0\nnote amplitude edge')) == '' and
           'no outgoing PM' in fault(amtext.replace('note amplitude edge', 'ks 1 120\nnote amplitude edge')) and
           'no outgoing PM' in fault(mixedtext.replace('ks 2 120', 'ks 0 120')),
           'zero left slope stays omitted, nonzero left slope needs outgoing PM in an amplitude route')
        p = one_note('w6b-left-control', *recipe('w6b-am'))
        ok(bool(raises(ValueError, p.ks, 't', 1, 120)) and not p.left_slopes,
           'Piece.ks refuses an amplitude-only source before mutation')
        p.ks('t', 1, 0)
        raw = dict(recipe('w6b-am')[3], ks={1: 120})
        ok(bool(raises(ValueError, patch_headers, 't', *recipe('w6b-am')[:3], raw)),
           'raw header metadata cannot bypass amplitude-route left-slope validation')
        for name in ('w6b-am', 'w6b-ring', 'w6b-mixed'):
            p = one_note(name, *recipe(name))
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and
               values(lines(merged, ('patch', 'route', 'ks'))) == values(lines(p.files()[0], ('patch', 'route', 'ks'))),
               name + ': amplitude route renders and canonical headers agree: ' + err)
            rc2, kv2, err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes', name + ': amplitude-edge record replays: ' + err2)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('patch', 'route', 'ks')) == lines(p.files()[0], ('patch', 'route', 'ks')),
               name + ': expansion preserves amplitude edge kinds')
        # The source's raw index and carrier level cannot affect an amplitude-only edge.
        p = one_note('w6b-source-fields', *recipe('w6b-am'))
        rc, basekv, err = harness(p.files())
        p.patches['t'][1][1:3] = [600, 65000]
        p.write()
        rc2, changedkv, err2 = harness(p.files())
        ok(rc == rc2 == 0 and basekv.get('hash') == changedkv.get('hash'),
           'AM-only source index and noncarrier level leave rendered PCM unchanged')
        # Explicit :pm has the exact old data and canonical bytes.
        with open(os.path.join(tmp, 'pm-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(SELFTEST_RECIPES.replace('3>2:300', '3>2:300:pm').replace('1>0:256', '1>0:256:pm'))
        pm = load_recipes(os.path.join(tmp, 'pm-recipes.txt'))
        ok(pm == R, 'explicit PM tokens load as the same legacy recipe dictionaries')
        MUSIC = tmp
        def amp_keys():
            return one_note('w6b-am-keys', *recipe('w6b-mixed'))
        source = amp_keys()
        for mirror in (False, True):
            k = keys_version(amp_keys, mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok(k.routes['t'][0]['edges'] == source.routes['t'][0]['edges'] and rc == 0 and kv.get('block_indep') == 'yes',
               'keys and mirror preserve mixed edge kinds and render: ' + err)
        MUSIC = music
        for fs in (48000, 44100):
            for name in ('amedge', 'ringedge', 'ampback'):
                with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    for line in text:
                        words = line.split()
                        if words[:1] == ['route']:
                            tokens = []
                            for token in words[4:]:
                                parts = re.split('[>:]', token)
                                tokens.append(edge_text((*map(int, parts[:3]), *(parts[3:] or ['pm'])), int(words[2])))
                            f.write(' '.join(words[:4] + tokens) + '\n')
                        else:
                            f.write(line)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%d/%s: rewritten amplitude-edge golden replays: %s' % (fs, name, err))
        print('selftest-recipes: ok operator extension AM/ring: kinds, PM-only fields, left slopes, weight bounds, expansion, keys and goldens')
        # : hard sync is a fourth edge kind; its weight is exactly 256 and takes no delta.
        synctext = amtext.replace('w6b-am', 'd92-sync').replace(':am', ':sync').replace('amplitude edge', 'hard sync edge')
        with open(os.path.join(tmp, 'sync-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(synctext)
        S = load_recipes(os.path.join(tmp, 'sync-recipes.txt'))
        RECIPES.update(S)
        ok(S['d92-sync'][3]['edges'] == [(1, 0, 256, 'sync')] and
           edge_values((1, 0, 256, 'sync')) == (1, 0, 256, 'sync') and edge_text((1, 0, 256, 'sync')) == '1>0:256:sync',
           'sync adds one tuple field and writes its suffix')
        for bad in ((1, 0, 255, 'sync'), (1, 0, 257, 'sync'), (1, 0, 1, 'sync'), (1, 0, True, 'sync'),
                    (1, 1, 256, 'sync'), (1, 0, 256, 'SYNC')):
            ok(bool(raises(ValueError, edge_values, bad)), 'invalid raw sync edge refused: %r' % (bad,))
        for token in ('1>0:255:sync', '1>0:257:sync', '1>0:1:sync', '1>0:256:SYNC', '1>1:256:sync',
                      '2>0:256:sync', '1>0:256:sync:x', '1>0:256:sync 1>0:256:am'):
            ok(bool(fault(synctext.replace('1>0:256:sync', token))), 'invalid sync route refused: ' + token)
        ok('a sync WEIGHT is 256' in raises(TypeError, recipe, 'd92-sync', w1_0=1) and
           'changes nothing' in raises(TypeError, recipe, 'd92-sync', m_index=1),
           'a sync weight and a sync-only source index take no delta')
        p = one_note('d92-sync', *recipe('d92-sync'))
        rc, kv, err = harness(p.files(), merged)
        ok(rc == 0 and kv.get('block_indep') == 'yes' and
           values(lines(merged, ('patch', 'route'))) == values(lines(p.files()[0], ('patch', 'route'))),
           'd92-sync: sync route renders and canonical headers agree: ' + err)
        rc2, kv2, err2 = harness([merged])
        ok(rc2 == 0 and kv2.get('match') == 'yes', 'd92-sync: sync-edge record replays: ' + err2)
        with open(src, 'w', encoding='utf-8', newline='\n') as f:
            f.write('recipe t d92-sync\n')
        expand_timeline(src, dst)
        ok(lines(dst, ('patch', 'route')) == lines(p.files()[0], ('patch', 'route')),
           'd92-sync: expansion preserves the sync kind')
        MUSIC = tmp
        def sync_keys():
            return one_note('d92-sync-keys', *recipe('d92-sync'))
        source = sync_keys()
        for mirror in (False, True):
            k = keys_version(sync_keys, mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok(k.routes['t'][0]['edges'] == source.routes['t'][0]['edges'] == [(1, 0, 256, 'sync')] and
               rc == 0 and kv.get('block_indep') == 'yes',
               'keys and mirror preserve the sync kind and render: ' + err)
        MUSIC = music
        for fs in (48000, 44100):
            for name in ('syncturn', 'syncback'):
                with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    for line in text:
                        words = line.split()
                        if words[:1] == ['route']:
                            tokens = []
                            for token in words[4:]:
                                parts = re.split('[>:]', token)
                                tokens.append(edge_text((*map(int, parts[:3]), *(parts[3:] or ['pm'])), int(words[2])))
                            f.write(' '.join(words[:4] + tokens) + '\n')
                        else:
                            f.write(line)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%d/%s: rewritten sync-edge golden replays: %s' % (fs, name, err))
        print('selftest-recipes: ok  sync: kind, fixed weight, refusals, render, expansion, keys and goldens')
        #  writer follow-up: the shared delay bus and its fixed stereo taps.
        p = one_note('followup-pingpong', *recipe('epiano'))
        p.space = 'stereo'
        p.fx(pingpong=(50000, 0, 20, 0), gain=-60)
        p.send('a', 0, 0, 'delay', 8192)
        p.write()
        rc, kv, err = harness(p.files(), merged)
        ok(rc == 0 and kv.get('block_indep') == 'yes' and
           lines(p.files()[0], ('pingpong', 'gain')) == ['pingpong 50000 0 20 0', 'gain -60'] and
           lines(merged, ('pingpong',)) == ['pingpong 50000 0 20 0'], 'pingpong headers/send parse and render: ' + err)
        old = dict(p.fxh)
        ok(bool(raises(ValueError, p.fx, delay=(50000, 0, 20))) and p.fxh == old,
           'delay cannot follow pingpong in another call')
        q = Piece('followup-pingpong-refuse', 120, 4, [], space='stereo')
        ok(bool(raises(ValueError, q.fx, delay=(50000, 0, 20), pingpong=(50000, 0, 20, 0))) and not q.fxh,
           'delay and pingpong together are refused before mutation')
        q.fx(delay=(50000, 0, 20))
        old = dict(q.fxh)
        ok(bool(raises(ValueError, q.fx, pingpong=(50000, 0, 20, 0))) and q.fxh == old,
           'pingpong cannot follow delay in another call')
        for layout in (None, 'off'):
            q = Piece('followup-layout', 120, 4, [])
            q.space = layout
            ok(bool(raises(ValueError, q.fx, pingpong=(50000, 0, 20, 0))) and not q.fxh,
               'pingpong refuses mono')
        q = Piece('followup-bounds', 120, 4, [], space='stereo')
        q.fx(pingpong=(1000000, 29491, 20000, 2000))
        for bad in ((49999, 0, 20, 0), (1000001, 0, 20, 0), (50000, 29492, 20, 0),
                    (50000, 0, 19, 0), (50000, 0, 20001, 0), (50000, 0, 20, 2001),
                    (50000, 0, 20, -1), (50000, False, 20, 0), (50000, 0, 20), 'bad'):
            old = dict(q.fxh)
            ok(bool(raises(ValueError, q.fx, pingpong=bad)) and q.fxh == old, 'pingpong bounds are atomic')
        for layout in ('amb1', 'amb3', 'binaural'):
            q = one_note('followup-field-' + layout, *recipe('epiano'))
            q.field(layout)
            q.fx(pingpong=(50000, 0, 20, 0), flanger=(1000, 500, 100, 0, 0), crush=True, gain=-60)
            q.send('a', 0, 0, 'delay', 8192)
            q.send('a', 0, 0, 'flanger', 8192)
            q.crush('a', 0, 0, 12, 3)
            q.write()
            rc, kv, err = harness(q.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and
               len(lines(merged, ('pingpong', 'flanger', 'crush'))) == 3,
               'field FX authoring and canonical replay: ' + layout + ' ' + err)
            if layout != 'binaural':
                q.fx(limiter=(10, 50))
                q.write()
                rc, _, _ = harness(q.files())
                ok(rc == 1, 'raw field nonlinear master remains refused natively: ' + layout)
        q = Piece('followup-panlaw', 120, 4, [])
        q.field('stereo', panlaw=32768)
        for kw in (dict(pingpong=(50000, 0, 20, 0)), dict(flanger=(1000, 500, 100, 0, 0))):
            ok(bool(raises(ValueError, q.fx, **kw)) and not q.fxh, 'PANLAW FX authoring remains refused')
        print('selftest-recipes: ok pingpong writer: shared bus, cross-call refusal, range and layout checks')

        # // writer follow-ups: amp is header-only while  is open.
        ext_text = pair.replace('recipe x', 'recipe followup-ext').replace('note n',
            'shape 0 asym pre\namp 0 65535 0 65535\nkstype 0 level - +\nfbtype 0 square\negloop 0\n'
            'kstype 1 both + -\nfbtype 1 presquare\negloop 1\nnote n')
        with open(os.path.join(tmp, 'followup-ext.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(ext_text)
        RECIPES.update(load_recipes(os.path.join(tmp, 'followup-ext.txt')))
        def ext_headers():
            p = Piece('followup-ext', 120, 4, ['Header-only amp and operator extension check'])
            p.dir = os.path.join(tmp, p.name)
            p.patch('t', *recipe('followup-ext'))
            p.stream('a', 'No note events while  holds amp audio fixtures')
            p.add_voices('t', 1, 'the header-only voice')
            p.length_ns = 1000000
            p.write()
            return p
        p = ext_headers()
        kinds = ('shape', 'amp', 'kstype', 'fbtype', 'egloop')
        expected = ['shape t 0 asym pre', 'amp t 0 65535 0 65535', 'kstype t 0 level - +',
                    'fbtype t 0 square', 'egloop t 0', 'kstype t 1 both + -', 'fbtype t 1 presquare', 'egloop t 1']
        rc, kv, err = harness(p.files(), merged)
        ok(rc == 0 and kv.get('events') == '0' and lines(p.files()[0], kinds) == expected and
           lines(merged, kinds) == expected, 'extension headers parse and keep canonical order without notes: ' + err)
        ok([x for x in patch_headers('t', *recipe('followup-ext')) if x.split()[0] in kinds] == expected,
           'patch_headers preserves amp and operator extensions')
        for row in ('amp t 0 0 0', 'amp t 65536 0 0 0', 'amp t -1 0 0 0',
                    'egloop t 2', 'fbtype t 1 bad', 'kstype t 1 both x -'):
            authored = open(p.files()[0], encoding='utf-8').read()
            body = '\n'.join(line for line in authored.splitlines() if line.split()[:1] != [row.split()[0]])
            with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                f.write(body + '\n' + row + '\n')
            rc, _, _ = harness([dst])
            ok(rc == 1, 'reader rejects malformed extension without note events: ' + row)
        p.amp('t', 0, 0, 0, 0)
        p.kstype('t', 0)
        p.fbtype('t', 0)
        p.kstype('t', 1)
        p.fbtype('t', 1)
        p.write()
        ok(lines(p.files()[0], ('amp', 'kstype', 'fbtype')) == ['amp t 0 0 0 0'],
           'zero amp stays enabled and kstype/fbtype defaults clear the rows')
        ok(op_ext_row('kstype', 't', 1, 'index', '-', '-') == '' and
           op_ext_row('fbtype', 't', 1, 'classic') == '', 'default extension row helpers omit defaults')
        for bad in ((-1, 0, 0, 0), (0, 65536, 0, 0), (0, 0, True, 0), (0, 0, 0, 0.0)):
            old = dict(p.amps)
            ok(bool(raises(ValueError, p.amp, 't', *bad)) and p.amps == old, 'invalid amp is atomic')
        for kind, fields in (('egloop', (2,)), ('egloop', (True,)), ('fbtype', (0, 'unknown')),
                             ('kstype', (0, 'level', 'linear', '-')), ('kstype', (3, 'both', '-', '-'))):
            ok(bool(raises(ValueError, getattr(p, kind), 't', *fields)), 'invalid operator extension refused')
        for row in ('amp 0 0 0', 'amp 65536 0 0 0', 'amp -1 0 0 0', 'egloop 2',
                    'egloop 0 1', 'fbtype 0 bad', 'kstype 0 level x -'):
            ok(bool(fault(pair.replace('note n', row + '\nnote n'))), 'invalid extension recipe: ' + row)
        for row in ('amp 0 0 0 0', 'egloop 0', 'fbtype 0 classic', 'kstype 0 index - -'):
            ok(bool(fault(pair.replace('note n', row + '\n' + row + '\nnote n'))),
               'duplicate extension recipe refused: ' + row)
        MUSIC = tmp
        source = ext_headers()
        for mirror in (False, True):
            k = keys_version(lambda: source, mirror=mirror)()
            k.write()
            ok(lines(k.files()[0], kinds) == expected and k.amps == source.amps,
               'keys preserve extension headers without rendering amp audio')
            k.egloops['t'].clear()
            ok(bool(source.egloops['t']), 'extension maps do not alias keys')
        MUSIC = music
        p.patch('t', *recipe('epiano'))
        p.write()
        ok(not p.amps and not p.kstypes and not p.fbtypes and not p.egloops and not p.shapes,
           'patch replacement removes every old extension')
        q = Piece('followup-four-operators', 120, 4, [])
        q.patch('r', *recipe('t2-routed'))
        q.kstype('r', 3, 'both', '+', '+')
        q.fbtype('r', 3, 'pre')
        q.egloop('r', 3)
        ok(q.kstypes['r'][3] == ('both', '+', '+') and q.fbtypes['r'][3] == ('pre',) and
           q.egloops['r'][3] == (), 'routed operator 3 accepts every extension')
        print('selftest-recipes: ok amp/egloop/kstype/fbtype writers: header-only roundtrip, defaults, bounds and keys')

        #  writer follow-up: all kinds and both placements, without changing old tuples.
        for kind in ('hard', 'cubic', 'fold', 'asym'):
            for pre in (False, True):
                fields = (60, kind) + (('pre',) if pre else ())
                row = 'shape t 60 ' + kind + (' pre' if pre else '')
                text = pair.replace('recipe x', 'recipe followup-shape').replace('note n', row.replace(' t ', ' ') + '\nnote n')
                with open(os.path.join(tmp, 'followup-shape.txt'), 'w', encoding='utf-8', newline='\n') as f:
                    f.write(text)
                loaded = load_recipes(os.path.join(tmp, 'followup-shape.txt'))
                RECIPES.update(loaded)
                ok(loaded['followup-shape'][3]['shape'] == fields, 'shape recipe preserves optional pre')
                p = one_note('followup-shape', *recipe('followup-shape'))
                p.shape('t', 60, kind, pre=pre)
                p.write()
                rc, kv, err = harness(p.files(), merged)
                ok(p.shapes['t'] == fields and shape_row('t', *fields) == row and
                   lines(p.files()[0], ('shape',)) == [row] and rc == 0 and
                   lines(merged, ('shape',)) == [row], 'shape kinds and placements round-trip: ' + err)
                ok(row in patch_headers('t', *recipe('followup-shape')), 'shape expansion preserves placement')
        for fields in ((0, 'pre', 'asym'), (241, 'asym'), (0, 'asym', 'post'), (0, 'asym', True)):
            ok(bool(raises(ValueError, shape_values, fields)), 'shape refuses malformed kind/position or drive')
        ok(bool(fault(pair.replace('note n', 'shape 0 pre asym\nnote n'))), 'recipe rejects pre before asym')
        ok(bool(raises(ValueError, p.shape, 't', 0, 'asym', pre=1)), 'shape pre refuses non-bools')
        print('selftest-recipes: ok shape follow-up: asym/pre APIs, recipes, expansion and canonical order')

        # operator extension post-filter shape is active at zero drive; absence alone bypasses it.
        shapetext = ''.join(pair.replace('recipe x', 'recipe w6b-shape-' + kind + '\nshape 60 ' + kind).replace(
            'level=10', 'level=30') for kind in ('hard', 'cubic', 'fold'))
        shaperoute = SELFTEST_RECIPES.split('recipe t2-w2')[0].replace('recipe t2-routed', 'recipe w6b-shape-route\nshape 0 cubic')
        with open(os.path.join(tmp, 'shape-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(shapetext + shaperoute)
        S = load_recipes(os.path.join(tmp, 'shape-recipes.txt'))
        RECIPES.update(S)
        ok(all(S['w6b-shape-' + kind][3]['shape'] == (60, kind) for kind in ('hard', 'cubic', 'fold')) and
           S['w6b-shape-route'][3]['shape'] == (0, 'cubic'), 'pair and routed shape metadata keep drive and kind')
        for fields in ('', '0', 'hard 0', '-1 hard', '+1 hard', '1.5 hard', '241 hard', '0 off',
                       '0 0', '0 HARD', '0 cubic extra', '１ hard'):
            ok(bool(fault(pair.replace('note n', 'shape ' + fields + '\nnote n'))), 'invalid shape recipe refused: ' + fields)
        ok('second shape' in fault(pair.replace('note n', 'shape 0 hard\nshape 0 hard\nnote n')),
           'duplicate zero-drive shapes are refused')
        ok(shape_values((0, 'hard')) == (0, 'hard') and shape_values((240, 'fold')) == (240, 'fold') and
           shape_row('s', 0, 'cubic') == 'shape s 0 cubic', 'shape endpoints and zero-drive statement remain explicit')
        ok(bool(raises(ValueError, shape_values, (True, 'hard'))) and
           bool(raises(ValueError, shape_row, 'bad name', 0, 'hard')), 'shape raw helpers reject bool and invalid names')
        ok(recipe('w6b-shape-cubic', time=2, c_level=1)[3]['shape'] == (60, 'cubic') and
           bool(raises(TypeError, recipe, 'w6b-shape-cubic', shape_drive=1)),
           'operator/time deltas preserve static shape metadata and do not invent shape deltas')
        for name in ('w6b-shape-hard', 'w6b-shape-cubic', 'w6b-shape-fold', 'w6b-shape-route'):
            p = one_note(name, *recipe(name))
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and
               lines(merged, ('shape',)) == lines(p.files()[0], ('shape',)),
               name + ': shape renders and canonical headers agree: ' + err)
            rc2, kv2, err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes', name + ': shape record replays: ' + err2)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('patch', 'route', 'shape')) == lines(p.files()[0], ('patch', 'route', 'shape')),
               name + ': expansion preserves shape metadata')
        p = one_note('w6b-shape-zero', *recipe('epiano'))
        base = open(p.files()[0], encoding='utf-8').read()
        rc, plain, err = harness(p.files())
        p.shape('t', 0, 'cubic')
        p.write()
        rc2, shaped, err2 = harness(p.files())
        ok(rc == rc2 == 0 and plain.get('hash') != shaped.get('hash') and lines(p.files()[0], ('shape',)) == ['shape t 0 cubic'],
           'zero drive remains an active cubic shaper and changes PCM')
        before = dict(p.shapes)
        ok(bool(raises(ValueError, p.shape, 'absent', 0, 'hard')) and bool(raises(ValueError, p.shape, 't', 241, 'hard')) and
           p.shapes == before, 'bad shape calls leave the existing setting unchanged')
        p.shape('t', 30, 'fold')
        ok(p.shapes == {'t': (30, 'fold')}, 'a second shape call replaces the first')
        p.patch('t', *recipe('epiano'))
        p.write()
        rc, restored, err = harness(p.files())
        ok(not p.shapes and open(p.files()[0], encoding='utf-8').read() == base and restored.get('hash') == plain.get('hash'),
           'replacing shaped patch with unshaped recipe restores exact header and PCM')
        p.filter('t', 'lp', 1200, .707)
        p.lfo('t', 0, 3000, 0, 0, 12, 0, 0)
        p.shape('t', 30, 'cubic')
        p.write()
        order = [line.split()[0] for line in lines(p.files()[0], ('filter', 'shape', 'lfo', 'voice'))]
        ok(order == ['filter', 'shape', 'lfo', 'voice'], 'session shape follows filters and precedes modulation headers')
        car, mod, text, extra = recipe('w6b-shape-hard')
        rows = patch_headers('t', car, mod, text, dict(extra, filter=('lp', 1200000, 707), lfo=(0,3000,0,0,12,0,0)))
        order = [line.split()[0] for line in rows if line.split()[0] in ('filter', 'shape', 'lfo')]
        ok(order == ['filter', 'shape', 'lfo'], 'recipe headers use the same filter/shape/modulation order')
        MUSIC = tmp
        def shape_keys():
            return one_note('w6b-shape-keys', *recipe('w6b-shape-cubic'))
        source = shape_keys()
        for mirror in (False, True):
            k = keys_version(shape_keys, mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok(k.shapes == source.shapes and rc == 0 and kv.get('block_indep') == 'yes',
               'keys and mirror preserve shape and render: ' + err)
            k.shapes.clear()
            ok(bool(source.shapes), 'keys shape map does not alias the source map')
        MUSIC = music
        for fs in (48000, 44100):
            for name in ('shapehard', 'shapecubic', 'shapefold1'):
                with open(os.path.join(SPIKE, 'tests', 'golden', str(fs), name + '.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                    for line in text:
                        words = line.split()
                        f.write(shape_row(words[1], int(words[2]), words[3]) + '\n' if words[:1] == ['shape'] else line)
                rc, kv, err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%d/%s: rewritten shaper golden replays: %s' % (fs, name, err))
        print('selftest-recipes: ok operator extension shape: bounds, zero-drive activity, replacement, header order, expansion, keys and goldens')
        # operator extension cascades append only x2; single-stage metadata and bytes stay exact.
        cascadetext = ''.join(pair.replace('recipe x', 'recipe w6b-cascade-' + mode + '\nfilter ' + mode + ' 1200000 707 x2')
                              for mode in ('lp', 'bp', 'hp', 'notch'))
        cascaderoute = SELFTEST_RECIPES.split('recipe t2-w2')[0].replace('recipe t2-routed', '''recipe w6b-cascade-route
fenv 20 150 500 80 1200 500
shape 60 fold
filter lp 1200000 707 x2''')
        with open(os.path.join(tmp, 'cascade-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(cascadetext + cascaderoute)
        C = load_recipes(os.path.join(tmp, 'cascade-recipes.txt'))
        RECIPES.update(C)
        ok(all(C['w6b-cascade-' + mode][3]['filter'] == (mode, 1200000, 707, 2) for mode in ('lp','bp','hp','notch')) and
           C['w6b-cascade-route'][3]['fenv'] == (20,150,500,80,1200,500,261626),
           'cascade recipe metadata appends stage2 and coexists with filter envelope and shape')
        for tail in ('2', '1', 'x1', 'x3', 'X2', 'x2 x2', 'x2 extra'):
            ok(bool(fault(pair.replace('note n', 'filter lp 1200000 707 ' + tail + '\nnote n'))),
               'only one literal x2 filter suffix is accepted: ' + tail)
        ok('second filter' in fault(pair.replace('note n', 'filter lp 1200000 707 x2\nfilter lp 1200000 707\nnote n')),
           'a cascade and a single filter are still duplicate statements')
        ok(filter_values(('lp',1200000,707,1)) == ('lp',1200000,707) and
           filter_values(('lp',1200000,707,2)) == ('lp',1200000,707,2) and
           filter_row('t','lp',1200000,707,1) == filter_row('t','lp',1200000,707),
           'explicit stage1 normalizes to the legacy metadata triple and exact row')
        for stage in (0,3,True,'x2',2.0):
            ok(bool(raises(ValueError, filter_values, ('lp',1200000,707,stage))), 'invalid raw filter stages refused: %r' % stage)
        ok(recipe('w6b-cascade-route', time=2, c_level=1)[3]['filter'] == ('lp',1200000,707,2),
           'operator and time deltas leave the filter stage count unchanged')
        for name in ('w6b-cascade-lp','w6b-cascade-bp','w6b-cascade-hp','w6b-cascade-notch','w6b-cascade-route'):
            p = one_note(name, *recipe(name))
            p.cutoff('a',100_000_000,0,700,.8)
            p.write()
            rc, kv, err = harness(p.files(), merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and
               values(lines(merged, ('filter','shape','fenv'))) == values(lines(p.files()[0], ('filter','shape','fenv'))),
               name + ': cascade and filter event render with matching canonical headers: ' + err)
            rc2, kv2, err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes', name + ': cascade/filter-event record replays: ' + err2)
            with open(src, 'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src, dst)
            ok(lines(dst, ('patch','route','filter','shape','fenv')) == lines(p.files()[0], ('patch','route','filter','shape','fenv')),
               name + ': expansion preserves the cascade and other metadata')
        p = one_note('w6b-cascade-default', *recipe('epiano'))
        p.filter('t','lp',1200,.707)
        p.write()
        base = open(p.files()[0], encoding='utf-8').read()
        rc, plain, err = harness(p.files())
        p.filter('t','lp',1200,.707,stages=1)
        p.write()
        rc2, explicit, err2 = harness(p.files())
        ok(rc == rc2 == 0 and p.filters['t'] == ('lp',1200000,707) and open(p.files()[0], encoding='utf-8').read() == base and
           explicit.get('hash') == plain.get('hash'), 'explicit stage1 preserves the old header and PCM')
        p.filter('t','lp',1200,.707,stages=2)
        p.write()
        rc, doubled, err = harness(p.files())
        ok(rc == 0 and doubled.get('hash') != plain.get('hash') and p.filters['t'] == ('lp',1200000,707,2),
           'stage2 is opt-in and changes filtered PCM')
        before = dict(p.filters)
        ok(bool(raises(ValueError, p.filter, 't','lp',1200,.707,stages=3)) and p.filters == before,
           'bad stage count is refused before mutation')
        p.patch('t',*recipe('epiano'))
        ok(p.filters['t'] == ('lp',1200000,707,2), 'a manually selected cascade survives recipe replacement')
        p.patch('t',*recipe('w6b-cascade-lp'))
        p.filter('t','hp',800,.75)
        ok(p.filters['t'] == ('hp',800000,750) and 't' not in p.recipe_filters,
           'an explicit old-style filter override returns to its single-stage default')
        MUSIC = tmp
        def cascade_keys():
            return one_note('w6b-cascade-keys', *recipe('w6b-cascade-route'))
        source = cascade_keys()
        for mirror in (False, True):
            k = keys_version(cascade_keys,mirror=mirror)()
            k.write()
            rc, kv, err = harness(k.files())
            ok(k.filters == source.filters and k.fenvs == source.fenvs and k.shapes == source.shapes and
               rc == 0 and kv.get('block_indep') == 'yes', 'keys/mirror preserve cascade, envelope and shape: ' + err)
        MUSIC = music
        for fs in (48000,44100):
            for name in ('filtertwo','shapefold'):
                with open(os.path.join(SPIKE,'tests','golden',str(fs),name+'.tl'), encoding='utf-8') as f:
                    text=f.readlines()
                with open(dst,'w', encoding='utf-8', newline='\n') as f:
                    for line in text:
                        words=line.split()
                        if words[:1] == ['filter']:
                            fields=(words[2],int(words[3]),int(words[4])) + ((2,) if len(words)==6 else ())
                            f.write(filter_row(words[1],*fields)+'\n')
                        else:
                            f.write(line)
                rc,kv,err=harness([dst])
                ok(rc==0 and kv.get('match')=='yes' and kv.get('block_indep')=='yes',
                   '%d/%s: rewritten cascade golden replays: %s' % (fs,name,err))
        print('selftest-recipes: ok operator extension cascade: suffix, metadata, stage1 identity, filter events, overrides, keys and goldens')
        # modulation matrix uses final patch prerequisites and preserves old event ordering by default.
        matrix_features = '''filter lp 1200000 707 x2
shape 30 cubic
lfo 0 3000 0 0 0 0 0
peg 1200 100 0 100 0 100 0 100
fenv 20 100 500 100 0 500'''
        matrix_slots = '''mod 7 ctl7 lfo_rate -1000
mod 2 note cutoff 1200 4
mod 0 ctl0 pitch0 1200 0
mod 1 vel level0 60 3
mod 3 peg index1 500 0
mod 4 fenv q 500 2
mod 5 lfo lfo_pitch 25 0
mod 6 eg1 lfo_index 500 3'''
        matrix_text = pair.replace('recipe x', 'recipe w7-matrix\n' + matrix_slots + '\n' + matrix_features)
        matrix_text += '''recipe w7-matrix-route
mod 2 eg3 level2 -120 1
mod 1 ctl2 index2 1000 0
mod 0 eg2 pitch3 -1200
route 4 5 1>0:256:am 2>0:128 3>2:256:ring
carrier level=10
modulator ratio=2
op 2 ratio=3 level=20 index=1
op 3 ratio=4
note mixed route matrix
recipe w7-matrix-zero
mod 7 eg1 index1 0
mod 0 ctl0 pitch0 0 0
carrier level=10
modulator index=1
note explicit zero matrix
'''
        with open(os.path.join(tmp, 'matrix-recipes.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(matrix_text)
        M = load_recipes(os.path.join(tmp, 'matrix-recipes.txt'))
        RECIPES.update(M)
        ok(M['w7-matrix'][3]['mod'][7] == ('ctl7','lfo_rate',-1000,3) and
           M['w7-matrix-zero'][3]['mod'][0] == ('ctl0','pitch0',0,0),
           'matrix slots precede their prerequisites, resolve default shift and retain explicit zero')
        extra = recipe('w7-matrix')[3]
        for source in MOD_SOURCES:
            ok(mod_settings({0:(source,'pitch0',4800,15)}, dict(extra,ops=4,carriers=5,edges=[(1,0,256),(3,2,256)]))[0][0] == source,
               'valid matrix source ' + source)
        for target, bound in MOD_AMOUNTS.items():
            target += '1' if target == 'index' else '0' if target in ('pitch','level','pd_knee','wt_pos','formant_center','formant_gain') else ''
            for amount in (-bound,0,bound):
                ok(mod_values((7,'ctl7',target,amount,15))[3] == amount,
                   'matrix amount endpoint %s %d' % (target,amount))
            for amount in (-bound-1,bound+1):
                ok(bool(raises(ValueError,mod_values,(0,'vel',target,amount))),
                   'matrix amount outside endpoint %s %d' % (target,amount))
        for row in ('mod 8 ctl0 pitch0 1', 'mod -1 ctl0 pitch0 1', 'mod 0 gen0 pitch0 1',
                    'mod 0 ctl8 pitch0 1', 'mod 0 eg4 pitch0 1', 'mod 0 ctl0 pitch4 1',
                    'mod 0 ctl0 off 0', 'mod 0 ctl0 wt_pos 1', 'mod 0 ctl0 pitch0 1 -1',
                    'mod 0 ctl0 pitch0 1 16', 'mod 0 ctl0 pitch0 1.0', 'mod 0 ctl0 pitch0 1 3 extra',
                    'mod 0 ctl0 pitch0', 'mod 0 ctl0 pitch0 0\nmod 0 ctl1 pitch0 1'):
            ok(bool(fault(pair.replace('note n',row+'\nnote n'))), 'bad matrix row refused: '+row)
        for row in ('mod 0 lfo pitch0 0', 'mod 0 peg pitch0 0', 'mod 0 fenv pitch0 0',
                    'mod 0 eg2 pitch0 0', 'mod 0 vel pitch2 0', 'mod 0 vel level1 0',
                    'mod 0 vel index0 0', 'mod 0 vel cutoff 0', 'mod 0 vel q 0',
                    'mod 0 ctl0 lfo_rate 0', 'mod 0 ctl0 lfo_pitch 0', 'mod 0 ctl0 lfo_level 0',
                    'mod 0 ctl0 lfo_index 0'):
            ok(bool(fault(pair.replace('note n',row+'\nnote n'))), 'zero matrix slot still requires features: '+row)
        ok(':5:' in fault(pair.replace('note n','mod 0 ctl0 pitch0 1\nmod 1 lfo pitch0 1\nnote n')),
           'final matrix prerequisite error names the failing slot line')
        ok(bool(fault(pair.replace('note n','peg 0 100 0 100 0 100 0 100\nmod 0 peg pitch0 1\nnote n'))) and
           bool(fault(pair.replace('note n','filter lp 1200000 707\nfenv 20 100 500 100 0 0\nmod 0 fenv pitch0 1\nnote n'))),
           'times-only PEG and inactive FENV do not qualify as sources')
        bad_route = matrix_text.split('recipe w7-matrix-route')[1].split('recipe w7-matrix-zero')[0]
        ok(bool(fault('recipe bad\n'+bad_route.replace('ctl2 index2','ctl2 index1'))),
           'AM source index target refused while outgoing PM index target is accepted')
        for fields in ((True,'ctl0','pitch0',1), (0,'ctl0','pitch0',1.0), (0,'ctl0','pitch0',1,True),
                       (0,'ctl0','pitch0',1,3.0), (0,'ctl0',[],1)):
            ok(bool(raises(ValueError,mod_values,fields)), 'matrix raw fields refuse nonintegers or invalid names')
        ok(bool(raises(ValueError,mod_row,'bad name',0,'ctl0','pitch0',1)), 'matrix writer refuses invalid patch names')
        rate = dict(extra, lfo=(0,3000,0,0,0,0,0))
        ok(mod_settings({0:('ctl0','lfo_rate',1000,0)},rate,513)[0][2] == 1000 and
           bool(raises(ValueError,mod_settings,{0:('ctl0','lfo_rate',1000,0)},rate,512)),
           'reachable LFO rate bound is strict at its exact Nyquist neighbor')
        ok(mod_settings({0:('ctl0','lfo_rate',0,0)},rate,512)[0][2] == 0 and
           bool(raises(ValueError,mod_settings,{0:('ctl0','lfo_rate',-1000,0),1:('ctl1','lfo_rate',-1000,0)},rate,640)),
           'zero rate slots do not expand the bound and absolute slot amounts sum before it')
        ok(recipe('w7-matrix',time=2,c_level=1)[3]['mod'] == extra['mod'] and
           bool(raises(TypeError,recipe,'w7-matrix',mod_amount=1)),
           'operator and time deltas preserve static matrix metadata, with no invented matrix delta')
        moved = recipe('w7-matrix')[3]
        moved['mod'].clear()
        ok(bool(RECIPES['w7-matrix'][3]['mod']), 'recipe matrix maps do not alias stored metadata')
        for name in ('w7-matrix','w7-matrix-route','w7-matrix-zero'):
            p = one_note(name,*recipe(name))
            p.ctl('a',100_000_000,0,0,32768)
            p.ctl('a',300_000_000,0,0,-32768)
            p.write()
            rc,kv,err = harness(p.files(),merged)
            ok(rc == 0 and kv.get('block_indep') == 'yes' and
               lines(merged,('mod',)) == lines(p.files()[0],('mod',)),
               name+': matrix headers and ctl events render canonically: '+err)
            rc2,kv2,err2 = harness([merged])
            ok(rc2 == 0 and kv2.get('match') == 'yes',name+': matrix merged record replays: '+err2)
            with open(src,'w', encoding='utf-8', newline='\n') as f:
                f.write('recipe t %s\n' % name)
            expand_timeline(src,dst)
            ok(lines(dst,('mod',)) == lines(p.files()[0],('mod',)),name+': expansion preserves all matrix slots')
        p = one_note('w7-replace',*recipe('epiano'))
        p.mod('t',0,'ctl0','pitch0',1200,0)
        p.ctl('a',100_000_000,0,0,32768)
        p.write()
        rc,active,err = harness(p.files())
        before = {name:dict(slots) for name,slots in p.mods.items()}
        ok(bool(raises(ValueError,p.mod,'absent',0,'ctl0','pitch0',1)) and
           bool(raises(ValueError,p.mod,'t',0,'ctl0','pitch0',4801)) and p.mods == before,
           'invalid mod calls preserve the existing slot')
        p.mod('t',0,'ctl0','pitch0',0)
        p.write()
        rc2,zero,err2 = harness(p.files())
        ok(rc == rc2 == 0 and active.get('hash') != zero.get('hash') and
           lines(p.files()[0],('mod',)) == ['mod t 0 ctl0 pitch0 0 3'],
           'mod replaces the slot and an explicit zero disables its audible offset')
        p.patch('t',*recipe('epiano'))
        p.write()
        rc,absent,err = harness(p.files())
        ok(not p.mods and rc == 0 and absent.get('hash') == zero.get('hash'),
           'patch replacement clears matrix slots, preserving zero-slot PCM and ctl on any defined voice')
        p.mod('t',0,'lfo','pitch0',1200)
        ok(bool(raises(ValueError,p.write_session)), 'missing final LFO prerequisite is refused')
        p.lfo('t',0,3000,0,0,0,0,0)
        p.write()
        rc,kv,err = harness(p.files())
        ok(rc == 0, 'mod may precede a zero-direct-depth real LFO: '+err)
        p = one_note('w7-fixed',*recipe('epiano',c_fixed=440))
        rc,fixed,err = harness(p.files())
        p.mod('t',0,'vel','pitch0',4800,0)
        p.write()
        rc2,fixedmod,err2 = harness(p.files())
        ok(rc == rc2 == 0 and fixed.get('hash') == fixedmod.get('hash'), 'fixed pitch targets are accepted and ignored')
        p = one_note('w7-order',*recipe('epiano'))
        p.mod('t',0,'ctl0','level0',1200,0)
        def tied(before):
            p.ev['a'] = []
            p.seq = 0
            if before: p.ctl('a',0,0,0,32768)
            p.on('a',0,0,'A2',24000)
            if not before: p.ctl('a',0,0,0,32768)
            p.off('a',500_000_000,0)
            p.write()
            rows = [line.split('#')[0].split()[3] for line in open(p.files()[1], encoding='utf-8') if line.strip() and not line.startswith('#')]
            rc,kv,err = harness(p.files(),merged)
            return rc,kv,err,rows
        rc,a,err,rows = tied(True)
        rc2,b,err2,rows2 = tied(False)
        ok(rc == rc2 == 0 and rows[:2] == ['ctl','on'] and rows2[:2] == ['on','ctl'] and a.get('hash') != b.get('hash'),
           'ctl/on call order survives writing and changes the note-on source snapshot')
        p.ev['a'] = []; p.seq = 0
        p.on('a',0,0,'A2',24000); p.off('a',0,0)
        p.ctl('a',100,0,0,1); p.on('a',100,0,'A2',24000); p.off('a',100,0)
        ok([e[4].split()[0] for e in ordered_events(p.ev['a'])] == ['off','on','ctl','on','off'],
           'only the group containing ctl opts into insertion order, including off events')
        old = list(p.ev['a']); seq = p.seq
        for args in (('missing',0,0,0,0),('a',0,1,0,0),('a',-1,0,0,0),('a',0,0,8,0),
                     ('a',0,0,0,32769),('a',0,0,0,-32769),('a',0,0,0,True)):
            ok(bool(raises(ValueError,p.ctl,*args)) and p.ev['a'] == old and p.seq == seq,
               'invalid ctl call refuses without adding an event: %r' % (args,))
        p = one_note('w7-all-headers',*recipe('w7-matrix'))
        p.eg('t',0,1); p.ks('t',1,10)
        p.write()
        kinds = [line.split()[0] for line in lines(p.files()[0],('filter','shape','lfo','peg','fenv','mod','eg','ks','voice'))]
        ok(kinds == ['filter','shape','lfo','peg','fenv']+['mod']*8+['eg','ks','voice'],
           'matrix headers follow fenv and precede EG/KS in ascending slot order')
        MUSIC = tmp
        def matrix_keys():
            q = one_note('w7-keys',*recipe('w7-matrix'))
            q.ev['a'] = []; q.seq = 0
            q.ctl('a',0,0,0,32768); q.on('a',0,0,'A2',24000); q.off('a',500_000_000,0)
            q.write()
            return q
        source = matrix_keys()
        for mirror in (False,True):
            k = keys_version(matrix_keys,mirror=mirror)()
            k.write()
            rc,kv,err = harness(k.files())
            order = [e[4].split()[0] for e in ordered_events(k.ev['a']) if e[0] == 0]
            ok(k.mods == source.mods and order == ['ctl','pos','on'] and rc == 0 and kv.get('block_indep') == 'yes',
               'keys/mirror preserve matrix and ctl tie order, with generated pos before on: '+err)
            k.mods['t'].clear()
            ok(bool(source.mods['t']), 'keys slot maps do not alias their source')
        MUSIC = music
        p = one_note('w7-patch-order',*recipe('epiano'))
        p.patch('z',*recipe('epiano')); p.patch('a',*recipe('epiano'))
        p.mod('a',7,'ctl0','pitch0',0); p.mod('z',7,'ctl0','pitch0',0); p.mod('z',0,'ctl1','pitch1',0)
        p.write()
        ok([row.split()[1:3] for row in lines(p.files()[0],('mod',))] == [['z','0'],['z','7'],['a','7']],
           'session matrix rows follow patch-definition order then ascending slot, independent of mod call order')
        for fs in (48000,44100):
            for name in ('modctl','modsmooth','modmany','modfilter','modlfoself','modenv'):
                with open(os.path.join(SPIKE,'tests','golden',str(fs),name+'.tl'), encoding='utf-8') as f:
                    text = f.readlines()
                with open(dst,'w', encoding='utf-8', newline='\n') as f:
                    for line in text:
                        words = line.split()
                        if words[:1] == ['mod']:
                            fields = (int(words[2]),words[3],words[4],int(words[5])) + ((int(words[6]),) if len(words)==7 else ())
                            f.write(mod_row(words[1],*fields)+'\n')
                        else:
                            f.write(line)
                rc,kv,err = harness([dst])
                ok(rc == 0 and kv.get('match') == 'yes' and kv.get('block_indep') == 'yes',
                   '%d/%s: rewritten matrix golden replays: %s' % (fs,name,err))
        print('selftest-recipes: ok modulation matrix: slots, prerequisites, bounds, replacement, ctl order, expansion and keys')
        #  session headers and native bank bindings; no generator recurrence in Python.
        # Both host parsers consume this table; malformed metadata never becomes code.
        descriptor_path = os.path.join(SPIKE,'src','gen','params.def')
        with open(descriptor_path, encoding='utf-8') as f: descriptor_text = f.read()
        descriptor_copy = os.path.join(tmp,'params.def')
        for bad in ('__import__("os")', descriptor_text+'\nGEN_KIND(1, duplicate)\n',
                    descriptor_text+'\nGEN_PARAM(1, duplicate, 1, p0, i64, 0, 1, q15, 1, 1, 0)\n',
                    descriptor_text.replace('GEN_PARAM(16, step, 4','GEN_PARAM(16, step, 4096'),
                    descriptor_text.replace('i64, 1, 4096, q15','i64, 4097, 4096, q15'),
                    descriptor_text.replace('q15, 1, 1, 16)','q15, 1, 1, 4097)'),
                    descriptor_text.replace('GEN_PARAM(17, p, 8, p0','GEN_PARAM(17, k, 8, p1')):
            with open(descriptor_copy,'w', encoding='utf-8', newline='\n') as f: f.write(bad)
            ok(bool(raises(ValueError,load_gen_descriptors,descriptor_copy)),
               'malformed shared generator descriptors are refused')
        for row in GEN_DESCRIPTORS:
            if row['type'] == 'word':
                for n in (row['low'],row['high']):
                    word = 'A'*n if n else '-'
                    ok(gen_word(word,row['name']) == word, 'shared word descriptor accepts '+row['name'])
                ok(bool(raises(ValueError,gen_word,'A'*(row['high']+1),row['name'])),
                   'shared word descriptor refuses long '+row['name'])
                continue
            kind = next((kind for kind,id in GEN_KINDS.items() if row['mask'] & (1 << (id-1))),None)
            for value in (row['low'],row['high']):
                ok(gen_bound(value,row['name'],kind) == value, 'shared scalar descriptor endpoint '+row['name'])
            for value in (row['low']-1,row['high']+1,True,0.0):
                ok(bool(raises(ValueError,gen_bound,value,row['name'],kind)),
                   'shared scalar descriptor rejects neighbor/type '+row['name'])
        for kind,rows in GEN_PARAMS.items():
            for name,row in rows.items():
                for value in (row['low'],row['high']):
                    if row['mutable']:
                        ok(gen_param_value(kind,name,value) == value,'shared event mutability '+kind+'/'+name)
                    else:
                        ok(bool(raises(ValueError,gen_param_value,kind,name,value)),
                           'shared immutable event field '+kind+'/'+name)
        ok(gen_values(0,'white',0) == ('white',0,1,0,(),None) and
           gen_values(15,'henon',4294967295)[4] == ((7*GEN_Q48+2)//5,(3*GEN_Q48+5)//10),
           'generator defaults are complete integers and seeds remain explicit')
        for kind in GEN_FIELDS:
            config = gen_values(0,kind,1)
            ok(gen_row(0,config).startswith('gen 0 '+kind+' 1 1 0') and
               len(config[4]) == len(GEN_FIELDS[kind]), 'canonical defaults for '+kind)
        for args,kw in (((16,'white',1),{}),((0,'julia',1),{}),((0,'mandelbrot',1),{}),
                        ((0,'white',-1),{}),((0,'white',4294967296),{}),((0,'white',True),{}),
                        ((0,'white',1),{'div':0}),((0,'white',1),{'div':65536}),
                        ((0,'white',1),{'slew':513}),((0,'white',1),{'gain':1}),
                        ((0,'brown',1),{'step':0}),((0,'brown',1),{'step':4097}),
                        ((0,'multi',1),{'p':65536,'k':16,'octaves':8}),
                        ((0,'logistic',1),{'r':4*GEN_Q48+1}),((0,'henon',1),{'b':-GEN_Q48-1}),
                        ((0,'euclid',1),{'k':9,'n':8}),((0,'euclid',1),{'rot':8}),
                        ((0,'euclid',1),{'parent':0}),((0,'thue',1),{'length':1,'rot':1}),
                        ((0,'fibonacci',1),{'depth':17}),((0,'lsystem',1),{'rot':16384})):
            ok(bool(raises(ValueError,gen_values,*args,**kw)), 'bad generator header refused: %r %r' % (args,kw))
        for kind,params in GEN_MUTABLE.items():
            for param in params:
                field = next(f for f in GEN_FIELDS[kind] if f[0] == param)
                ok(gen_param_value(kind,param,field[1]) == field[1], 'mutable generator parameter '+kind+'/'+param)
        for kind,param,value in (('white','seed',1),('white','div',2),('multi','p',4),('multi','octaves',2),
                                 ('fibonacci','depth',3),('lsystem','rule',1),('henon','b',GEN_Q48+1),
                                 ('white','enabled',2),('pink','reset',-1),('brown','slew',-1)):
            ok(bool(raises(ValueError,gen_param_value,kind,param,value)), 'immutable or out-of-range gen event '+kind+'/'+param)
        ok(mod_values((0,'gen.015.y','pitch0',1))[1] == 'gen.15.y',
           'public generator references normalize numeric IDs without exposing raw input slots')
        for source in ('gen.16.x','gen.-1.x','gen.0.z','gen.0','gen0','gen.1.X'):
            ok(bool(raises(ValueError,mod_values,(0,source,'pitch0',0))), 'malformed/reserved generator source '+source)
        bank_recipe = pair.replace('recipe x','recipe gen-recipe\nmod 0 gen.5.y pitch0 1200 0\nmod 7 gen.0.x index1 512')
        with open(os.path.join(tmp,'bank-recipes.txt'),'w', encoding='utf-8', newline='\n') as f:
            f.write(bank_recipe)
        RECIPES.update(load_recipes(os.path.join(tmp,'bank-recipes.txt')))
        ok(recipe('gen-recipe')[3]['mod'][0][0] == 'gen.5.y', 'recipe generator references defer session-bank resolution')
        with open(src,'w', encoding='utf-8', newline='\n') as f:
            f.write('recipe t gen-recipe\n')
        expand_timeline(src,dst)
        ok(lines(dst,('mod',)) == ['mod t 0 gen.5.y pitch0 1200 0','mod t 7 gen.0.x index1 512 3'],
           'recipe expansion retains public source references and does not invent a bank or seed')
        def bank_piece(name, lanes=False):
            q = Piece(name,120,4,[name+': generator integration self-test'])
            q.dir = os.path.join(tmp,name); q.length_ns = 1_000_000_000
            q.patch('t',*recipe('gen-recipe')); q.stream('a','bank and notes'); q.add_voices('t',1,'voice')
            q.gen_clock()
            for id,kind in reversed(list(enumerate(GEN_FIELDS))):
                q.gen(id,kind,123+id,div=64 if kind == 'euclid' else 1)
            q.gen_axiom(10,4,'AB'); q.gen_rule(10,'B','A'); q.gen_rule(10,'A','AB')
            if lanes:
                q.gen_lane(0,0,gate=6,pitch=5,accent=1,step_ticks=64,base_cents=6000,
                           span_cents=1200,velocity=24000,gate_ticks=32,scale_mask=0)
            else:
                q.on('a',0,0,'A2',24000); q.off('a',500_000_000,0)
            return q
        q = bank_piece('gen-header')
        q.write()
        heads = lines(q.files()[0],('gen','gen_axiom','gen_rule'))
        ok([int(row.split()[1]) for row in heads if row.startswith('gen ')] == list(range(11)) and
           heads[-3:] == ['gen_axiom 10 4 AB','gen_rule 10 A AB','gen_rule 10 B A'],
           'bank headers sort source IDs and each source\'s rule symbols')
        snapshot = dict(q.generators)
        ok(bool(raises(ValueError,q.gen,0,'white',1)) and q.generators == snapshot and
           bool(raises(ValueError,q.gen_clock)) and bool(raises(ValueError,q.gen_axiom,10,4,'AB')) and
           bool(raises(ValueError,q.gen_rule,10,'A','AB')), 'duplicate bank definitions refuse without replacement')
        for f,args in ((q.gen_axiom,(1,17,'A')),(q.gen_axiom,(1,0,'')),(q.gen_axiom,(1,0,'I')),
                       (q.gen_rule,(1,'I','A')),(q.gen_rule,(1,'A','')),(q.gen_rule,(1,'A','a'))):
            ok(bool(raises(ValueError,f,*args)), 'bounded generator text syntax refused before mutation')
        for alter in ('clock','missing','scalar-y','parent','axiom','extra-word'):
            bad = bank_piece('gen-bad-'+alter)
            if alter == 'clock': bad.gen_ctl = None
            elif alter == 'missing': bad.generators.pop(5)
            elif alter == 'scalar-y': bad.mod('t',0,'gen.0.y','pitch0',0)
            elif alter == 'parent': bad.gen(15,'euclid',1,parent=14)
            elif alter == 'axiom': bad.gen_axioms.clear()
            else: bad.gen(15,'fibonacci',1)
            ok(bool(raises(ValueError,bad.write)), 'completed generator header refuses '+alter)
        for kwargs in ({'step_ticks':0},{'gate_ticks':65},{'velocity':0},{'span_cents':9601},
                       {'base_cents':-4800,'span_cents':0,'scale_mask':1},{'base_cents':14400,'span_cents':1}):
            args = dict(step_ticks=64,base_cents=6000,span_cents=1200,velocity=24000,gate_ticks=32)
            args.update(kwargs)
            ok(bool(raises(ValueError,q.gen_lane,0,0,**args)), 'lane range/pitch preflight '+repr(kwargs))
        lane = bank_piece('gen-lane',True)
        ok(bool(raises(ValueError,lane.gen_lane,1,0,step_ticks=64,base_cents=6900,span_cents=0,velocity=10000,gate_ticks=32)),
           'two lanes cannot reserve one voice')
        lane.on('a',0,0,'A2',24000)
        ok(bool(raises(ValueError,lane.write)), 'manual notes conflict with a reserved lane voice')
        lane.ev['a'].clear()
        lane.gen_set('a',200_000_000,0,'reset',42)
        lane.gen_set('a',300_000_000,0,'slew',16)
        lane.lane_enable('a',400_000_000,0,0)
        lane.lane_enable('a',600_000_000,0,1)
        old,seq = list(lane.ev['a']),lane.seq
        ok(bool(raises(ValueError,lane.gen_set,'a',0,0,'div',2)) and
           bool(raises(ValueError,lane.lane_enable,'a',0,1,0)) and
           bool(raises(ValueError,lane.gen_set,'missing',0,0,'reset',2)) and
           lane.ev['a'] == old and lane.seq == seq, 'invalid bank events preserve events and sequence')
        # Send every public mutable spelling through both writer and native reader.
        for id,config in sorted(q.generators.items()):
            for param,row in GEN_PARAMS[config[0]].items():
                if row['mutable']:
                    q.gen_set('a',150_000_000,id,param,row['default'] if row['has_default'] else config[1])
        for p in (q,lane):
            for fs in (48000,44100):
                p.fs = fs
                p.write()
                rc,kv,err = harness(p.files(),merged)
                ok(rc == 0 and kv.get('block_indep') == 'yes', p.name+': generator bank renders: '+err)
                rc2,kv2,err2 = harness([merged])
                ok(rc2 == 0 and kv2.get('match') == 'yes',p.name+': live bank merged record replays: '+err2)
                ok(values(lines(merged,('gen_clock','gen','gen_axiom','gen_rule','gen_lane','mod'))) ==
                   values(lines(p.files()[0],('gen_clock','gen','gen_axiom','gen_rule','gen_lane','mod'))),
                   p.name+': canonical bank headers retain public bindings')
        ok(any(' a gen 0 reset 42' in row for row in open(lane.files()[1], encoding='utf-8')) and
           any(' a lane 0 enable 0' in row for row in open(lane.files()[1], encoding='utf-8')),
           'session runtime events have no synthetic VOICE field')
        manifest = list(generated_events(lane))
        ok(bool(manifest) and any(e[4] == 'on' for e in manifest) and any(e[4] == 'off' for e in manifest),
           'native manifest streams generated lane events with checked timing')
        MUSIC = tmp
        for fs in (48000,44100):
            source = bank_piece('gen-keys',True); source.fs = fs; source.write()
            def bank_keys():
                q = bank_piece('gen-keys',True); q.fs = fs
                return q
            for mirror in (False,True):
                k = keys_version(bank_keys,mirror=mirror)()
                k.write()
                events = list(generated_events(source))
                ons = [e for e in events if e[4] == 'on']
                positions = [e for e in k.ev['a'] if e[4].startswith('pos ')]
                ok(k.generators == source.generators and k.gen_lanes == source.gen_lanes and
                   len(positions) == len(ons) and not any(e[1] == 1 for e in k.ev['a']),
                   'lane keys preserve live bank and lanes, adding only generated-note positions')
                ok(all(pos[0] == event[1] and tuple(map(int,pos[4].split()[1:])) ==
                       keys_pos(midi_of(event[5]),mirror=mirror) for pos,event in zip(positions,ons)),
                   'native integer pitches/times determine every generated keys position')
                rc,kv,err = harness(k.files())
                ok(rc == 0 and kv.get('block_indep') == 'yes', 'generated keys/mirror render: '+err)
                k.gen_rules[10].clear()
                ok(bool(source.gen_rules[10]), 'keys generator rule maps do not alias the original')
        source = bank_piece('gen-source-keys'); source.write()
        def source_keys(): return bank_piece('gen-source-keys')
        k = keys_version(source_keys)(); k.write()
        rc,kv,err = harness(k.files())
        ok(k.generators == source.generators and not k.gen_lanes and rc == 0,
           'source-only keys propagate bank headers with their ordinary explicit notes: '+err)
        MUSIC = music
        empty = one_note('gen-empty-clock',*recipe('epiano'))
        rc,plain,err = harness(empty.files())
        empty.gen_clock(); empty.write()
        rc2,clocked,err2 = harness(empty.files())
        ok(rc == rc2 == 0 and plain.get('hash') == clocked.get('hash'), 'explicit empty generator bank preserves PCM')
        table = subprocess.run([H,'--gen-params'],capture_output=True,text=True)
        expected = ['GEN_PARAMS version=1'] + ['GEN_KIND id=%d name=%s' % (id,kind) for kind,id in GEN_KINDS.items()]
        expected += ['GEN_PARAM id={id} name={name} kinds={mask} slot={slot} type={type} min={low} max={high} '
                     'unit={unit} mutable={mutable} has_default={has_default} default={default}'.format(**r)
                     for r in GEN_DESCRIPTORS]
        expected += ['GEN_PARAMS count=%d' % len(GEN_DESCRIPTORS)]
        ok(table.returncode == 0 and table.stdout.splitlines() == expected,
           'native public descriptor export matches every Python field, kind and default')
        print('selftest-recipes: ok generator binding: headers, references, lanes, runtime events, native manifest and live keys')
        #  sine phase distortion: explicit identity and completed-source checks.
        for i,knee in ((0,1),(1,32767),(3,16384)):
            ok(pd_values((i,knee),4,0) == (i,knee), 'PD operator/knee endpoint')
        for args in (((),),((0,),),((0,0),),((0,32768),),((-1,1),),((4,1),),
                     ((0,1.0),),((True,1),),((2,1),2),((0,1),2,1)):
            ok(bool(raises(ValueError,pd_values,*args)), 'PD malformed/range/source refusal')
        pdtext = pair.replace('recipe x','recipe pd-identity\npd 0 16384')
        pdtext += pair.replace('recipe x','recipe pd-knee\npd 0 8192\nmod 0 ctl0 pd_knee0 8192 3')
        pdtext += SELFTEST_RECIPES.split('recipe t2-w2')[0].replace(
            'recipe t2-routed','recipe pd-route\npd 3 1\npd 0 32767\nmod 0 ctl0 pd_knee3 -32767 0')
        with open(os.path.join(tmp,'pd-recipes.txt'),'w', encoding='utf-8', newline='\n') as f: f.write(pdtext)
        PD = load_recipes(os.path.join(tmp,'pd-recipes.txt')); RECIPES.update(PD)
        ok(PD['pd-identity'][3]['pd'] == {0:16384} and PD['pd-route'][3]['pd'] == {0:32767,3:1},
           'PD metadata retains identity and operators defined later')
        for row in ('pd 0 0','pd 0 32768','pd 2 1','pd 0 1.5','pd 0','pd 0 1 extra',
                    'pd 0 1\npd 0 2','mod 0 ctl0 pd_knee0 0','mod 0 ctl0 pd_knee4 1',
                    'mod 0 ctl0 vector_x 1'):
            ok(bool(fault(pair.replace('note n',row+'\nnote n'))), 'PD bad recipe row: '+row)
        ok(bool(fault(pair.replace('recipe x','recipe x\npd 0 16384').replace('carrier ','carrier wave=1 '))),
           'PD completed recipe refuses non-sine waveform')
        ok(bool(raises(TypeError,recipe,'pd-identity',c_wave=1)) and
           bool(raises(TypeError,recipe,'pd-route',o3_wave=9)), 'PD conflicts after pair/routed recipe deltas')
        ok(recipe('pd-identity',time=2,c_r=20)[3]['pd'] == {0:16384},
           'PD raw knee is unaffected by envelope scaling/deltas')
        p = one_note('pd-method',*recipe('epiano')); p.pd('t',1,1); p.pd('t',0,16384); p.write()
        ok(lines(p.files()[0],('pd',)) == ['pd t 0 16384','pd t 1 1'], 'PD canonical operator ordering')
        p.pd('t',0,8192); before = {name:dict(fields) for name,fields in p.pds.items()}
        ok(bool(raises(ValueError,p.pd,'t',2,1)) and bool(raises(ValueError,p.pd,'t',0,0)) and p.pds == before,
           'PD rejected method preserves earlier settings')
        p.patch('t',*recipe('epiano'))
        ok(not p.pds, 'patch replacement removes manually set PD')
        p.patch('t',*recipe('pd-identity')); p.mod('t',0,'ctl0','pd_knee0',0)
        p.write()
        ok(p.pds['t'] == {0:16384} and bool(lines(p.files()[0],('mod',))),
           'enabled identity satisfies even a zero PD target slot')
        p.patches['t'][0] = list(p.patches['t'][0]); p.patches['t'][0][11] = 9
        ok(bool(raises(ValueError,p.write)), 'completed Piece refuses changed PD source before write')
        with open(src,'w', encoding='utf-8', newline='\n') as f: f.write('recipe t pd-route\n')
        expand_timeline(src,dst)
        ok(lines(dst,('pd',)) == ['pd t 0 32767','pd t 3 1'], 'recipe expansion carries enabled PD headers')
        MUSIC = tmp
        def pd_keys(): return one_note('pd-keys',*recipe('pd-route'))
        source = pd_keys()
        pd_key_records = []
        for mirror in (False,True):
            k = keys_version(pd_keys,mirror=mirror)(); k.write(); pd_key_records.append(k)
            ok(k.pds == source.pds and k.mods == source.mods, 'keys/mirror preserve PD and knee slots')
            k.pds['t'][0] = 16384
            ok(source.pds['t'][0] == 32767, 'keys PD maps are detached')
            k.pds['t'][0] = 32767
        MUSIC = music
        # PD native integration follows the preceding authoring-only preflight.
        for fs in (48000,44100):
            p = one_note('pd-render',*recipe('epiano')); p.fs = fs; p.write()
            rc,plain,err = harness(p.files())
            p.pd('t',0,16384); p.write()
            rc2,identity,err2 = harness(p.files(),merged)
            ok(rc == rc2 == 0 and plain.get('hash') == identity.get('hash'), 'PD explicit midpoint PCM identity')
            rc3,replay,err3 = harness([merged])
            ok(rc3 == 0 and replay.get('match') == 'yes' and 'pd t 0 16384' in lines(merged,('pd',)),
               'PD identity remains canonical and replays')
            p.pd('t',0,8192); p.mod('t',0,'ctl0','pd_knee0',8192,3)
            p.ctl('a',0,0,0,-32768); p.ctl('a',400_000_000,0,0,32768); p.write()
            rc,warped,err = harness(p.files(),merged)
            ok(rc == 0 and warped.get('block_indep') == 'yes' and warped.get('hash') != plain.get('hash'),
               'PD knee and modulation matrix motion change PCM: '+err)
            rc,replay,err = harness([merged])
            ok(rc == 0 and replay.get('match') == 'yes', 'PD motion canonical replay: '+err)
        for k in pd_key_records:
            rc,kv,err = harness(k.files())
            ok(rc == 0 and kv.get('block_indep') == 'yes', 'PD keys/mirror render: '+err)
        for fs in (48000,44100):
            for name in ('pdknee','pdmorph'):
                with open(os.path.join(SPIKE,'tests','golden',str(fs),name+'.tl'), encoding='utf-8') as f: text=f.readlines()
                with open(dst,'w', encoding='utf-8', newline='\n') as f:
                    for line in text:
                        words=line.split()
                        f.write(pd_row(words[1],*map(int,words[2:]))+'\n' if words[:1] == ['pd'] else line)
                rc,kv,err=harness([dst])
                ok(rc==0 and kv.get('match')=='yes' and kv.get('block_indep')=='yes',
                   '%d/%s: rewritten PD golden replays: %s' % (fs,name,err))
        print('selftest-recipes: ok PD: identity, final sine checks, knee slots, replacement, expansion, keys and goldens')
        #  static vector base coordinates remain valid with moving targets.
        for xy in ((0,0),(32768,0),(0,32768),(32768,32768),(16384,16384)):
            ok(vector_values(xy,4,15) == xy,'static vector endpoint/center')
        for values in ((),(0,),(0,0,0),(-1,0),(0,32769),(True,0),(0,0.0)):
            ok(bool(raises(ValueError,vector_values,values)), 'static vector syntax/range refusal')
        for ops,carriers in ((2,1),(3,7),(4,1),(4,7),(4,0)):
            ok(bool(raises(ValueError,vector_values,(0,0),ops,carriers)), 'static vector route prerequisite')
        vector_text = """recipe vector-zero
vector 0 0
pd 0 16384
route 4 15
carrier ratio=1 level=24 a=0 d=0 s=0 r=50
modulator ratio=2 level=24 a=0 d=0 s=0 r=50
op 2 ratio=3 level=24 a=0 d=0 s=0 r=50
op 3 ratio=4 level=24 a=0 d=0 s=0 r=50
note Four distinct sine corners, static origin.
"""
        with open(os.path.join(tmp,'vector-recipes.txt'),'w', encoding='utf-8', newline='\n') as f:f.write(vector_text)
        VR = load_recipes(os.path.join(tmp,'vector-recipes.txt'));RECIPES.update(VR)
        ok(recipe('vector-zero')[3]['vector'] == (0,0) and recipe('vector-zero',time=2,c_level=1)[3]['vector'] == (0,0),
           'enabled vector origin survives recipe expansion and envelope/level deltas')
        for text in (vector_text.replace('vector 0 0','vector 0 0\nvector 1 2'),
                     vector_text.replace('vector 0 0','vector 1.0 0'),
                     vector_text.replace('vector 0 0','vector 0 32769'),
                     vector_text.replace('route 4 15','route 4 5 1>0:256 3>2:256'),
                     pair.replace('note n','vector 0 0\nnote n'),
                     vector_text.replace('pd 0 16384','mod 0 ctl0 vector_x 32769'),
                     vector_text.replace('pd 0 16384','mod 0 ctl0 vector_y -32769')):
            ok(bool(fault(text)), 'static vector completed recipe/coordinate target bounds')
        p = one_note('vector-method',*recipe('vector-zero'))
        ok(lines(p.files()[0],('pd','vector')) == ['pd t 0 16384','vector t 0 0'],
           'static vector canonical origin follows PD')
        p.vector('t',32768,32768); before=dict(p.vectors)
        ok(bool(raises(ValueError,p.vector,'t',-1,0)) and p.vectors==before,
           'invalid vector replacement preserves coordinates')
        p.patch('t',*recipe('epiano'))
        ok(not p.vectors and bool(raises(ValueError,p.vector,'t',0,0)), 'patch replacement clears vector and pair rejects it')
        p.patch('t',*recipe('vector-zero'));p.routes['t'][0]['carriers']=7
        ok(bool(raises(ValueError,p.write)), 'completed Piece validates the current vector route')
        with open(src,'w', encoding='utf-8', newline='\n') as f:f.write('recipe t vector-zero\n')
        expand_timeline(src,dst)
        ok(lines(dst,('vector',)) == ['vector t 0 0'], 'recipe text expansion retains enabled vector origin')
        MUSIC=tmp
        def vector_keys():return one_note('vector-keys',*recipe('vector-zero'))
        source=vector_keys();vector_key_records=[]
        for mirror in (False,True):
            k=keys_version(vector_keys,mirror=mirror)();k.write();vector_key_records.append(k)
            ok(k.vectors==source.vectors and k.pds==source.pds,'keys/mirror preserve vector and PD composition')
            k.vectors['t']=(32768,32768)
            ok(source.vectors['t']==(0,0),'keys vector maps are detached')
            k.vectors['t']=(0,0)
        MUSIC=music
        # Moving vector uses the matrix's existing slots and controller events.
        for target in ('vector_x','vector_y'):
            for amount in (-32768,0,32768):
                ok(mod_values((0,'ctl0',target,amount)) == (0,'ctl0',target,amount,3),
                   'moving vector signed amount/zero endpoint')
            for amount in (-32769,32769,True,1.0):
                ok(bool(raises(ValueError,mod_values,(0,'ctl0',target,amount))),
                   'moving vector amount type/range refusal')
        for target in ('vector','vector_x0','vector_y1'):
            ok(bool(raises(ValueError,mod_values,(0,'ctl0',target,1))), 'moving vector exact target spelling')
        moving_text = vector_text.replace('recipe vector-zero','recipe vector-moving').replace(
            'vector 0 0','mod 1 ctl1 vector_y -32768 0\nmod 0 ctl0 vector_x 0\nvector 0 0')
        with open(os.path.join(tmp,'vector-moving.txt'),'w', encoding='utf-8', newline='\n') as f:f.write(moving_text)
        VM=load_recipes(os.path.join(tmp,'vector-moving.txt'));RECIPES.update(VM)
        ok(recipe('vector-moving',time=2,m_wave=9)[3]['mod']=={1:('ctl1','vector_y',-32768,0),0:('ctl0','vector_x',0,3)},
           'moving recipe header order and operator deltas preserve matrix coordinates')
        for text in (moving_text.replace('vector 0 0\n',''),
                     moving_text.replace('route 4 15','route 4 7'),
                     pair.replace('note n','mod 0 ctl0 vector_x 0\nnote n')):
            ok(bool(fault(text)), 'moving vector final enabled/four-carrier prerequisites including zero')
        p=one_note('vector-moving-method',*recipe('vector-moving'))
        ok(lines(p.files()[0],('mod','vector'))==['mod t 0 ctl0 vector_x 0 3','mod t 1 ctl1 vector_y -32768 0','vector t 0 0'],
           'moving vector canonical slot ordering retains zero before coordinates')
        p.mod('t',0,'ctl0','vector_x',32768,3);p.ctl('a',0,0,0,-32768);p.ctl('a',200_000_000,0,0,32768);p.write()
        saved_vector=p.vectors.pop('t')
        ok(bool(raises(ValueError,p.write)), 'moving Piece refuses lost vector enable before writing')
        p.vectors['t']=saved_vector;p.routes['t'][0]['carriers']=7
        ok(bool(raises(ValueError,p.write)), 'moving Piece refuses changed carrier route')
        p.patch('t',*recipe('vector-zero'))
        ok(not p.mods and p.vectors['t']==(0,0), 'patch replacement clears moving slots but keeps supplied vector')
        with open(src,'w', encoding='utf-8', newline='\n') as f:f.write('recipe t vector-moving\n')
        expand_timeline(src,dst)
        ok(lines(dst,('mod','vector'))==['mod t 0 ctl0 vector_x 0 3','mod t 1 ctl1 vector_y -32768 0','vector t 0 0'],
           'moving recipe text expansion carries exact base and slots')
        MUSIC=tmp
        def moving_keys():
            p=one_note('moving-keys',*recipe('vector-moving'));p.ctl('a',0,0,1,32768);return p
        source=moving_keys();moving_key_records=[]
        for mirror in (False,True):
            k=keys_version(moving_keys,mirror=mirror)();k.write();moving_key_records.append(k)
            ok(k.mods==source.mods and k.vectors==source.vectors and
               [e[4] for e in k.ev['a'] if e[4].startswith('ctl ')]==['ctl 1 32768'],
               'keys/mirror preserve moving slots, coordinate headers and ctl ordering')
        MUSIC=music
        # Static vector native integration follows authoring-only preflight.
        for fs in (48000,44100):
            p=one_note('vector-render',*recipe('vector-zero'));p.fs=fs
            hashes=[]
            for x,y in ((0,0),(32768,0),(0,32768),(32768,32768),(16384,16384)):
                p.vector('t',x,y);p.write()
                rc,kv,err=harness(p.files(),merged)
                ok(rc==0 and kv.get('block_indep')=='yes','static vector corner/center renders: '+err)
                hashes.append(kv.get('hash'))
                rc,replay,err=harness([merged])
                ok(rc==0 and replay.get('match')=='yes','static vector canonical replay: '+err)
            ok(len(set(hashes))==5,'distinct fixed-coordinate sources and center produce distinct PCM')
            with open(os.path.join(SPIKE,'tests','golden','vector','vectorcorners-%d.tl' % fs), encoding='utf-8') as f:text=f.readlines()
            with open(dst,'w', encoding='utf-8', newline='\n') as f:
                for line in text:
                    words=line.split()
                    f.write(vector_row(words[1],*map(int,words[2:]))+'\n' if words[:1]==['vector'] else line)
            rc,kv,err=harness([dst])
            ok(rc==0 and kv.get('match')=='yes' and kv.get('block_indep')=='yes','rewritten static vector golden: '+err)
        for k in vector_key_records:
            rc,kv,err=harness(k.files())
            ok(rc==0 and kv.get('block_indep')=='yes','static vector keys/mirror render: '+err)
        print('selftest-recipes: ok static vector: route, origin, bounds, replacement, expansion, keys and goldens')
        # Moving-vector native integration follows authoring-only preflight.
        for fs in (48000,44100):
            p=one_note('vector-moving-render',*recipe('vector-zero'));p.fs=fs;p.write()
            rc,plain,err=harness(p.files())
            p.mod('t',0,'ctl0','vector_x',32768,3);p.mod('t',1,'ctl1','vector_y',32768,0)
            p.ctl('a',0,0,0,32768);p.ctl('a',250_000_000,0,1,32768)
            p.ctl('a',500_000_000,0,0,-32768);p.write()
            rc2,moving,err2=harness(p.files(),merged)
            ok(rc==rc2==0 and moving.get('block_indep')=='yes' and plain.get('hash')!=moving.get('hash'),
               'moving vector ctl path changes PCM with native clamp/smoothing: '+err+err2)
            rc,replay,err=harness([merged])
            ok(rc==0 and replay.get('match')=='yes','moving vector canonical replay: '+err)
            with open(os.path.join(SPIKE,'tests','golden',str(fs),'vectorxy.tl'), encoding='utf-8') as f:text=f.readlines()
            with open(dst,'w', encoding='utf-8', newline='\n') as f:
                for line in text:
                    words=line.split()
                    f.write(mod_row(words[1],int(words[2]),words[3],words[4],*map(int,words[5:]))+'\n' if words[:1]==['mod'] else line)
            rc,kv,err=harness([dst])
            ok(rc==0 and kv.get('match')=='yes' and kv.get('block_indep')=='yes','rewritten moving vector golden: '+err)
        for k in moving_key_records:
            rc,kv,err=harness(k.files())
            ok(rc==0 and kv.get('block_indep')=='yes','moving vector keys/mirror render: '+err)
        print('selftest-recipes: ok moving vector: signed amounts, prerequisites, zero slots, canonical order, keys and goldens')

        #  fixed formant banks and their modulation matrix targets.
        for fields in ((0,1,500,0),(2,4294967295,16000,1200),(1,1500000,3000,120)):
            ok(formant_values(fields)==fields, 'formant slot/raw boundary accepted')
        for fields in ((),(0,1,500),(0,1,500,0,0),(-1,1,500,0),(3,1,500,0),
                       (0,0,500,0),(0,4294967296,500,0),(0,1,499,0),(0,1,16001,0),
                       (0,1,500,-1),(0,1,500,1201),(True,1,500,0),(0,1.0,500,0)):
            ok(bool(raises(ValueError,formant_values,fields)), 'formant syntax/type/range refusal')
        formant_text = pair.replace('recipe x','recipe formant-bank').replace('note n',
            'mod 2 ctl2 formant_gain2 0\nmod 0 ctl0 formant_center1 300000 0\n'
            'formant 2 2500000 4000 180\nformant 0 500000 2000 60\nformant 1 1500000 3000 120\nnote n')
        with open(os.path.join(tmp,'formant-recipes.txt'),'w', encoding='utf-8', newline='\n') as f:f.write(formant_text)
        FB=load_recipes(os.path.join(tmp,'formant-recipes.txt'));RECIPES.update(FB)
        formant_extra=recipe('formant-bank')[3]
        ok(formant_extra['formant']=={0:(500000,2000,60),1:(1500000,3000,120),2:(2500000,4000,180)},
           'formant recipe allows arbitrary slot/header order')
        ok(recipe('formant-bank',time=2,c_wave=9,m_index=1)[3]['formant']==formant_extra['formant'],
           'formant metadata survives operator/time deltas independently of source wave')
        for text in (formant_text.replace('formant 0 500000 2000 60','formant 0 0 2000 60'),
                     formant_text.replace('formant 0 500000 2000 60','formant 0 500000 2000 60\nformant 0 500000 2000 60'),
                     formant_text.replace('formant 0 500000 2000 60','formant 3 500000 2000 60'),
                     formant_text.replace('formant 2 2500000 4000 180\n',''),
                     formant_text.replace('formant 1 1500000 3000 120','formant 1 1500000.0 3000 120')):
            ok(bool(fault(text)), 'formant completed recipe/duplicate/zero-target prerequisite refusal')
        for target in ('formant_center','formant_gain','formant_center3','formant_gain-1','formant_center00'):
            ok(bool(raises(ValueError,mod_values,(0,'ctl0',target,0))), 'formant exact target slot spelling')
        for target,bound in (('formant_center2',2147483647),('formant_gain0',1200)):
            for amount in (-bound,0,bound):
                ok(mod_values((0,'ctl0',target,amount))[3]==amount, 'formant signed target endpoint')
            for amount in (-bound-1,bound+1,True,1.0):
                ok(bool(raises(ValueError,mod_values,(0,'ctl0',target,amount))), 'formant amount range/type refusal')
            ok(bool(raises(ValueError,mod_settings,{0:('ctl0',target,0,3)},{})),
               'formant zero amount still requires enabled target slot')
        p=one_note('formant-method',*recipe('formant-bank'))
        ok(lines(p.files()[0],('mod','formant'))==[
            'mod t 0 ctl0 formant_center1 300000 0','mod t 2 ctl2 formant_gain2 0 3',
            'formant t 0 500000 2000 60','formant t 1 1500000 3000 120','formant t 2 2500000 4000 180'],
           'formant canonical slot ordering preserves zero matrix amount')
        before=dict(p.formants['t'])
        ok(bool(raises(ValueError,p.formant,'t',0,0,500,0)) and p.formants['t']==before and
           bool(raises(ValueError,p.formant,'absent',0,1,500,0)), 'formant invalid replacement preserves bank')
        p.formant('t',0,1,500,0);p.write()
        ok('formant t 0 1 500 0' in lines(p.files()[0],('formant',)), 'zero attenuation remains enabled')
        del p.formants['t'][2]
        ok(bool(raises(ValueError,p.write)), 'formant completed Piece refuses a removed target slot')
        p.patch('t',*recipe('epiano'))
        ok(not p.formants and not p.mods, 'patch replacement clears formant metadata and slots')
        p.patch('t',*recipe('vector-zero'));p.formant('t',0,500000,2000,60);p.write()
        ok(lines(p.files()[0],('pd','vector','formant'))==['pd t 0 16384','vector t 0 0','formant t 0 500000 2000 60'],
           'formant canonical headers follow PD/vector')
        p.formants['t'][0]=(1,499,0)
        ok(bool(raises(ValueError,p.write)), 'formant manually corrupted metadata refused before output')
        with open(src,'w', encoding='utf-8', newline='\n') as f:f.write('recipe t formant-bank time=2 c_wave=9\n')
        expand_timeline(src,dst)
        ok(lines(dst,('formant',))==['formant t 0 500000 2000 60','formant t 1 1500000 3000 120','formant t 2 2500000 4000 180'],
           'formant recipe text expansion preserves bank')
        MUSIC=tmp
        def formant_keys():
            p=one_note('formant-keys',*recipe('formant-bank'));p.ctl('a',200_000_000,0,0,32768);return p
        source=formant_keys();formant_key_records=[]
        for mirror in (False,True):
            k=keys_version(formant_keys,mirror=mirror)();k.write();formant_key_records.append(k)
            ok(k.formants==source.formants and k.mods==source.mods,
               'keys/mirror preserve formant banks and matrix metadata')
            k.formants['t'][0]=(1,500,0)
            ok(source.formants['t'][0]==(500000,2000,60), 'keys bank dictionaries are detached')
            k.formants['t'][0]=(500000,2000,60)
        MUSIC=music
        # Formant native integration follows authoring-only preflight.
        for fs in (48000,44100):
            p=one_note('formant-render',*recipe('formant-bank',c_level=20));p.fs=fs;p.write()
            rc,plain,err=harness(p.files())
            p.ctl('a',100_000_000,0,0,32768);p.mod('t',2,'ctl2','formant_gain2',1200,3)
            p.ctl('a',200_000_000,0,2,32768);p.write()
            rc2,moving,err2=harness(p.files(),merged)
            ok(rc==rc2==0 and moving.get('block_indep')=='yes' and plain.get('hash')!=moving.get('hash'),
               'formant matrix center/gain controls change PCM: '+err+err2)
            rc,replay,err=harness([merged])
            ok(rc==0 and replay.get('match')=='yes', 'formant canonical replay: '+err)
            for name in ('formant3','formantmove'):
                with open(os.path.join(SPIKE,'tests','golden',str(fs),name+'.tl'), encoding='utf-8') as f:text=f.readlines()
                with open(dst,'w', encoding='utf-8', newline='\n') as f:
                    for line in text:
                        words=line.split()
                        f.write(formant_row(words[1],*map(int,words[2:]))+'\n' if words[:1]==['formant'] else line)
                rc,kv,err=harness([dst])
                ok(rc==0 and kv.get('match')=='yes' and kv.get('block_indep')=='yes',
                   'rewritten formant golden %s/%d: %s' % (name,fs,err))
        for k in formant_key_records:
            rc,kv,err=harness(k.files())
            ok(rc==0 and kv.get('block_indep')=='yes', 'formant keys/mirror render: '+err)
        print('selftest-recipes: ok formant: ranges, duplicates, target slots, replacement, expansion, keys and goldens')

    finally:
        RECIPES.clear()
        RECIPES.update(saved)
        MUSIC = music
        shutil.rmtree(tmp, ignore_errors=True)
    print('selftest-recipes: %d checks ok' % done[0])




def build():
    """The harness the engine line pins ; make rebuilds it only when stale."""
    os.makedirs(os.path.join(SPIKE, 'build'), exist_ok=True)
    with open(os.path.join(SPIKE, 'build', '.lock'), 'a', encoding='utf-8', newline='\n') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if subprocess.run(['make', '-C', SPIKE, '-s', 'SINE_BITS=12', 'EXP_BITS=10', 'PHASE64=1', 'ENV64=1']).returncode:
                sys.exit('compose.py: the harness did not build')
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)



if __name__ == '__main__':
    args = sys.argv[1:]
    if args == ['--recipes']:
        list_recipes()
    elif args == ['--selftest-perf']:
        selftest_perf()
    elif args == ['--selftest-recipes']:
        selftest_recipes()
    elif len(args) == 3 and args[0] == '--expand':
        count = expand_timeline(args[1], args[2])
        print('Expanded %d recipe statements.' % count)
    else:
        print(__doc__)
        sys.exit(0 if not args or args == ['--help'] else 2)
