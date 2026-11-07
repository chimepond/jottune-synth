#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""DX7 voices to recipe blocks, an approximate translation (, ,
), never emulator-exact. A host tool: standard library only, floats
allowed .

    python3 -B tools/music/dx7_import.py BANK.syx [--voice N] [--ops 4] [--out FILE | --into FILE] [--name PREFIX]
    python3 -B tools/music/dx7_import.py --selftest

It reads a 32-voice bank (4104 bytes), a single voice (163 bytes) or a
single voice without its SysEx frame (155 bytes), checks the frame and the
checksum, and writes one block per voice (or the voice N) in recipes.txt's
format, which compose.py's loader takes, recipe plays and --expand
expands . The block is the voice reduced to --ops operators
(default 4, FM_OPS): a route line (routing, ; the grammar, ), the
carrier and modulator lines as engine operators 0 and 1 and an op line
for each operator from 2 up, with each operator's rate scaling, fixed
frequency and key sync as rs, fixed and free (operator extension, , ); a voice
left with one operator is a pair with a silent modulator. The `# pair:`
lines hold the voice's 2-operator form, taken from the whole voice so
that --ops never changes them. A pitch LFO maps to LFO ; amplitude
modulation and the pitch envelope remain `# pending:` lines. The note line names the voice,
and the comment lines under it every approximation and every dropped
operator: compose.py writes the notes as the comment on a patch line,
which the harness takes up to 1022 bytes. algorithms.txt beside this
file holds the routings and the reduce rule; README.md beside it, DX7
import, has the mapping table.
--into FILE appends the blocks to an existing recipes file instead :
a name the file already holds, or one given earlier in the same import,
takes the first free suffix, -2, -3 and so on, in bank order; the file with
the import appended is loaded through compose.py's loader before anything
is appended and must read the file's own recipes unchanged, and the file's
existing text is never rewritten. A file name written into a comment has its
line breaks escaped. --out is unchanged; an empty --out or --into is refused.
Exit 0 when the blocks are written, 1 for an input it refuses, 2 for a
usage error.

--selftest builds a synthetic bank, checks the translation against values
worked out by hand, writes the blocks under out/dx7_selftest, loads
them with compose.py's loader, expands one, renders one note of a routed
block through the harness, prints PASS or FAIL and exits 0 or 1.
"""
import argparse
import decimal
import functools
import os
import re
import subprocess
import sys
import tempfile

from compose import NUMERIC_PREC

MUSIC =os.path.dirname(os.path.abspath(__file__))
SPIKE = os.path.dirname(os.path.dirname(MUSIC))
ALGORITHMS = os.path.join(SPIKE, 'data', 'algorithms.txt')

# The DX7's published level table: an output level of 0 to 19 to its
# internal level; from 20 up the internal level is the output level plus 28,
# so 99 gives 127. One internal step is 0.75 dB .
LEVEL_TABLE = (0, 5, 9, 13, 17, 20, 23, 25, 27, 29, 31, 33, 35, 37, 39, 41, 42, 43, 45, 46)
DB_STEP = 0.75
# The DX7's published rate and level tables, as an EG in internal steps: EG
# level L at output level OL sits at (table(L) >> 1) * 2 + table(OL) - 133
# steps, never below 0.5, so L 99 at OL 99 is FULL; the rate R moves it by
# (4 + q % 4) * 2^(q // 4 - 13) steps a block, q = R * 41 >> 6, one block
# being 64 samples at 44.1 kHz; a fall is straight in steps (in dB), a rise
# starts at JUMP at the latest and is that step times 16 - (octave of the
# level, 8 steps an octave), quick at first and slower near the top.
FULL = 120
JUMP = 53.625
BLOCKS_PER_S = 44100 / 64
RELEASE_DB = 96.3            # the engine's release: ms per 96.3 dB of fall
# Decimal pi, rounded with the same fixed context as the pitch helpers.
# Host constants only; no binary libm result feeds a written field.
with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
    PI = decimal.Decimal('3.141592653589793238462643383279502884197169399375105820974944592307816406286')
    INDEX_FULL = 4 * PI       # modulator peak at full scale, radians
    FB_FULL = 2 * PI          # feedback 7 peak, halved at each lower step
HEADROOM_DB = 12.0           # carriers sit this far below full scale, plus 20 log10(carriers)
DETUNE_CENTS = 1.0           # detune 0..14: (detune - 7) cents
LIN_DB_OCT = 4 * 329 / 4096 * DB_STEP    # -LIN right depth 1: dB per octave above the break
EXP_SHARE = 1 / 6            # -EXP: the line through the DX7's curve 3 octaves above the break
# The DX7's pitch EG level table: level 0..99 to 128ths of 4 octaves, 50 = 0.
PEG_TABLE = (-128, -116, -104, -95, -85, -76, -68, -61, -56, -52, -49, -46, -43,
             -41, -39, -37, -35, -33, -32, -31, -30, -29, -28, -27, -26, -25, -24,
             -23, -22, -21, -20, -19, -18, -17, -16, -15, -14, -13, -12, -11, -10,
             -9, -8, -7, -6, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
             11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
             28, 29, 30, 31, 32, 33, 34, 35, 38, 40, 43, 46, 49, 53, 58, 65, 73,
             82, 92, 103, 115, 127)
PEG_CENTS = 4800 / 128
# The LFO's pitch modulation by msfa's scaling (; README's pinned source trace):
# the peak deviation is (depth * 165 >> 6) * PMS_TABLE[sensitivity] / 65536
# octave, so depth 99 at sensitivity 7 is about an octave (1190.6 cents).
PMS_TABLE = (0, 10, 20, 33, 55, 92, 153, 255)
LFO_WAVES = ('triangle', 'saw down', 'saw up', 'square', 'sine', 'sample and hold')
LFO_TO_W3 = (1, 3, 2, 4, 0, 5)           # LFO's waves: 0 sine, 1 triangle, 2 saw up, 3 saw down, 4 square, 5 S&H
LFO_FS, LFO_CTL = 48000, 64             # regressionb's fixed reference, independent of the render rate
LFO_UNIT = 33587                        # round-half-up(64 * 25190424 / 48000), msfa lfo.cc:26-28
LFO_MAX_MS = 60000                      # LFO's delay/fade bounds; refuse, never clamp
LOOPS = {4: 4, 6: 5}                     # dx7-4 and dx7-6: the carrier whose output the DX7 feeds back to OP6
MAX_MS = 65535                           # compose.py's LIMITS for a, d and r
MAX_DB = 120.0
FM_OPS = 4                               # the engine's operators : --ops's default, and the most
                                         # carriers the carrier line's level counts

# The unpacked voice (155 bytes): 21 bytes an operator, OP6 first, then the voice's.
OP_FIELDS = ('r1', 'r2', 'r3', 'r4', 'l1', 'l2', 'l3', 'l4', 'bp', 'ld', 'rd', 'lc', 'rc',
             'rs', 'ams', 'kvs', 'ol', 'mode', 'coarse', 'fine', 'det')
OP_MAX = (99,) * 11 + (3, 3, 7, 3, 7, 99, 1, 31, 99, 14)
VOICE_FIELDS = ('pr1', 'pr2', 'pr3', 'pr4', 'pl1', 'pl2', 'pl3', 'pl4', 'alg', 'fb', 'oks',
                'speed', 'delay', 'pmd', 'amd', 'sync', 'wave', 'pms', 'transpose')
VOICE_MAX = (99,) * 8 + (31, 7, 1, 99, 99, 99, 99, 1, 5, 7, 48)


def die(msg, code=1):
    sys.stderr.write('dx7_import.py: %s\n' % msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# SysEx: the frame, the checksum and the packed bank format.

def unpack(b):
    """A bank's 128-byte packed voice as the 155 values of the unpacked one."""
    v = []
    for k in range(6):
        o = b[17 * k:17 * k + 17]
        v += list(o[0:11]) + [o[11] & 3, o[11] >> 2 & 3, o[12] & 7, o[13] & 3, o[13] >> 2 & 7,
                              o[14], o[15] & 1, o[15] >> 1 & 31, o[16], o[12] >> 3 & 15]
    return v + list(b[102:110]) + [b[110] & 31, b[111] & 7, b[111] >> 3 & 1, b[112], b[113], b[114],
                                   b[115], b[116] & 1, b[116] >> 1 & 7, b[116] >> 4 & 7, b[117]] + list(b[118:128])


def pack(v):
    """The 155 values of an unpacked voice as a bank's 128 bytes (the self-test's banks)."""
    out = []
    for k in range(6):
        o = v[21 * k:21 * k + 21]
        lc, rc, rs, ams, kvs, ol, mode, coarse, fine, det = o[11:21]
        out += list(o[0:11]) + [lc | rc << 2, rs | det << 3, ams | kvs << 2, ol, mode | coarse << 1, fine]
    p = v[126:]
    out += list(p[0:8]) + [p[8], p[9] | p[10] << 3, p[11], p[12], p[13], p[14],
                           p[15] | p[16] << 1 | p[17] << 4, p[18]] + list(p[19:29])
    return bytes(out)


def frame(data):
    """data (4096 bytes: a bank, 155: a voice) in its SysEx frame with its checksum."""
    head = (9, 0x20, 0x00) if len(data) == 4096 else (0, 0x01, 0x1B)
    return bytes((0xF0, 0x43, 0x00) + head) + bytes(data) + bytes(((-sum(data)) & 0x7F, 0xF7))


def decode(v):
    """155 unpacked values as a voice: {'ops': {1..6: {field: value}}, field: value,
    'name', 'clamped'}; a value past its range is clamped and listed."""
    voice, clamped = {'ops': {}}, []
    for k in range(6):
        n, o = 6 - k, {}
        for j, (f, hi) in enumerate(zip(OP_FIELDS, OP_MAX)):
            x = v[21 * k + j]
            if x > hi:
                clamped.append('OP%d %s %d to %d' % (n, f, x, hi))
                x = hi
            o[f] = x
        voice['ops'][n] = o
    for j, (f, hi) in enumerate(zip(VOICE_FIELDS, VOICE_MAX)):
        x = v[126 + j]
        if x > hi:
            clamped.append('%s %d to %d' % (f, x, hi))
            x = hi
        voice[f] = x
    voice['name'] = ''.join(chr(c) if 32 <= c < 127 else ' ' for c in v[145:155])
    voice['clamped'] = clamped
    return voice


def read_voices(data, label):
    """[(number, voice)] from a file's bytes, or exit 1 with the reason."""
    n = len(data)
    if n == 155:                                    # no frame, so no checksum to check
        if max(data) > 0x7F:
            die('%s: a data byte above 0x7F; a DX7 voice holds 7-bit bytes' % label)
        return [(1, decode(list(data)))]
    if n not in (4104, 163):
        die('%s is %d bytes; a DX7 bank is 4104 bytes, a single voice 163 (155 without its SysEx frame)'
            % (label, n))
    size, head = (4096, (9, 0x20, 0x00)) if n == 4104 else (155, (0, 0x01, 0x1B))
    if data[0] != 0xF0 or data[1] != 0x43 or data[2] & 0xF0 or tuple(data[3:6]) != head or data[-1] != 0xF7:
        die('%s: not a DX7 %s frame: it starts %s and ends %02X, where F0 43 0n %02X %02X %02X ... F7 is expected'
            % (label, 'bank' if n == 4104 else 'voice', ' '.join('%02X' % b for b in data[:6]), data[-1], *head))
    body, check = data[6:6 + size], data[6 + size]
    if max(body) > 0x7F:
        die('%s: a data byte above 0x7F; SysEx data is 7-bit' % label)
    if (sum(body) + check) & 0x7F:
        die('%s: checksum 0x%02X, where the data needs 0x%02X' % (label, check, (-sum(body)) & 0x7F))
    if n == 163:
        return [(1, decode(list(body)))]
    return [(i + 1, decode(unpack(body[128 * i:128 * i + 128]))) for i in range(32)]


# ---------------------------------------------------------------------------
# algorithms.txt and the reduce rule.

def load_algorithms(path=ALGORITHMS):
    """{name: {'ops', 'carriers', 'edges', 'feedback', 'note'}}; the first fault exits 1."""
    algs, cur, where = {}, None, os.path.basename(path)

    def fail(n, msg):
        die('%s:%d: %s' % (where, n, msg))

    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            words = line.split()
            if not words or words[0].startswith('#'):
                continue
            key, rest = words[0], words[1:]
            try:
                if key == 'algorithm':
                    if len(rest) != 1 or rest[0] in algs:
                        fail(n, 'an algorithm line is `algorithm NAME`, each NAME once')
                    cur = algs[rest[0]] = {'name': rest[0], 'edges': [], 'note': [], 'line': n}
                elif cur is None:
                    fail(n, 'a %s line before the first algorithm line' % key)
                elif key == 'ops':
                    cur['ops'] = int(rest[0])
                elif key == 'carriers':
                    cur['carriers'] = [int(w) for w in rest]
                elif key == 'edge':
                    cur['edges'] += [tuple(int(x) for x in w.split('>')) for w in rest]
                elif key == 'feedback':
                    cur['feedback'] = int(rest[0])
                elif key == 'note':
                    cur['note'].append(line.split(None, 1)[1].strip())
                else:
                    fail(n, 'a line starts with algorithm, ops, carriers, edge, feedback, note or #, not %r' % key)
            except (ValueError, IndexError):
                fail(n, 'a malformed %s line' % key)
    for a in algs.values():
        n, ops = a['line'], a.get('ops')
        if ops not in (4, 6) or 'carriers' not in a or 'feedback' not in a or not a['note']:
            fail(n, '%s needs ops 4 or 6, carriers, feedback and a note' % a['name'])
        every = set(range(1, ops + 1))
        if not a['carriers'] or not set(a['carriers']) <= every or len(set(a['carriers'])) != len(a['carriers']):
            fail(n, '%s: the carriers are operators 1 to %d, each once' % (a['name'], ops))
        if a['feedback'] not in every:
            fail(n, '%s: the feedback operator is 1 to %d' % (a['name'], ops))
        for e in a['edges']:
            if len(e) != 2 or e[0] == e[1] or not set(e) <= every or a['edges'].count(e) > 1:
                fail(n, '%s: an edge is FROM>TO, two operators of 1 to %d, each pair once' % (a['name'], ops))
            if e[0] in a['carriers']:
                fail(n, '%s: carrier OP%d modulates; no chip algorithm does' % (a['name'], e[0]))
        reach = set(a['carriers'])                  # every modulator must reach a carrier, and no edge loops
        for _ in range(ops):
            reach |= {f for f, t in a['edges'] if t in reach}
        if reach != every:
            fail(n, '%s: OP%s reach no carrier' % (a['name'], ', OP'.join(map(str, sorted(every - reach)))))
        if engine_order(sorted(every), a['edges']) is None:
            fail(n, '%s: the edges loop' % a['name'])
    return algs


def sounding(alg, ol):
    """The carriers at an output level above 0; when every carrier is at 0,
    the lowest-numbered one (the reduce rule's step 1)."""
    return [c for c in alg['carriers'] if ol[c] > 0] or [min(alg['carriers'])]


def pair_of(alg, ol):
    """The 2-operator lines' (carrier, modulator or None), taken from the whole
    voice before the reduction, so --ops never changes them: the loudest carrier
    that sounds (of equals, one that a sounding operator modulates, then the
    lower number) and the loudest sounding operator with an edge to it (the
    lower number of equals)."""
    into = lambda c: [f for f, t in alg['edges'] if t == c and ol[f] > 0]
    car = min(sounding(alg, ol), key=lambda c: (-ol[c], not into(c), c))
    mods = into(car)
    return car, (min(mods, key=lambda m: (-ol[m], m)) if mods else None)


def reduce_ops(alg, ol, limit):
    """The reduce rule (algorithms.txt's header): the kept operators, their edges,
    and {dropped operator: why}, for output levels ol {op: 0..99}."""
    ops, dropped = range(1, alg['ops'] + 1), {}
    loud = sounding(alg, ol)
    for o in ops:
        if ol[o] == 0 and o not in loud:
            dropped[o] = 'output level 0'
    cars = sorted(loud, key=lambda c: (-ol[c], c))
    for c in cars[limit:]:
        dropped[c] = 'a carrier past %d' % limit
    cars = cars[:limit]
    depth, frontier = {c: 0 for c in cars}, list(cars)
    while frontier:
        nxt = []
        for t in frontier:
            for f, to in alg['edges']:
                if to == t and f not in depth and f not in dropped:
                    depth[f] = depth[t] + 1
                    nxt.append(f)
        frontier = nxt
    mods = [o for o in ops if o not in dropped and o not in cars]
    for m in mods:
        if m not in depth:
            dropped[m] = 'no path to a kept carrier'
    order = sorted((m for m in mods if m in depth), key=lambda m: (depth[m], -ol[m], m))
    room = limit - len(cars)
    for m in order[room:]:
        dropped[m] = 'depth %d' % depth[m]
    kept = sorted(cars + order[:room])
    return kept, [(f, t) for f, t in alg['edges'] if f in kept and t in kept], dropped


def engine_order(kept, edges):
    """Engine op 0 upward: repeatedly the lowest-numbered operator left whose kept
    targets all have engine numbers, so every edge is a forward edge (the same
    sample). None when the edges loop."""
    order = []
    while len(order) < len(kept):
        ready = [x for x in kept if x not in order and all(t in order for f, t in edges if f == x)]
        if not ready:
            return None
        order.append(min(ready))
    return order


def route_words(order, carriers, edges):
    """The route statement after PATCH: OPS CARRIERS FROM>TO:256..., FROM then TO."""
    eng = {op: i for i, op in enumerate(order)}
    mask = sum(1 << eng[c] for c in carriers if c in eng)
    pairs = sorted((eng[f], eng[t]) for f, t in edges)
    return [str(len(order)), str(mask)] + ['%d>%d:256' % p for p in pairs]


# ---------------------------------------------------------------------------
# One operator: the DX7's numbers to op arguments.

def out_level(ol):
    """The DX7's published level table: output level to internal steps of 0.75 dB."""
    return LEVEL_TABLE[ol] if ol < 20 else ol + 28


def eg_level(l, ol):
    """EG level l at output level ol in internal steps; FULL = full scale."""
    return max(0.5, (out_level(l) >> 1) * 2 + out_level(ol) - 133)


def eg_step(rate):
    """Internal steps a block that rate moves a falling EG (the rate table)."""
    q = rate * 41 >> 6
    return (4 + (q & 3)) * 2.0 ** ((q >> 2) - 13)


def lfo_cents(depth, sens):
    """The LFO's peak pitch deviation in cents at pitch depth 0..99 and pitch
    sensitivity 0..7, by the source-traced msfa scaling (before integer rounding)."""
    return (depth * 165 >> 6) * PMS_TABLE[sens] * 1200 / 65536


def half_up(num, den):
    """Round a nonnegative rational to the nearest integer, ties upward."""
    return (2 * num + den) // (2 * den)


def lfo_rate(speed):
    """msfa's phase increment at the regressionb reference, in integer mHz."""
    sr = 1 if speed == 0 else (165 * speed) >> 6
    sr *= 11 if sr < 160 else 11 + ((sr - 160) >> 4)
    return half_up(LFO_UNIT * sr * LFO_FS * 1000, LFO_CTL * (1 << 32))


@functools.lru_cache(maxsize=100)
def lfo_times(delay):
    """First positive and first full getdelay return, as delay/fade milliseconds.
    Follow uint32 recurrence literally, including its first-increment wrap test
    in stage two. Refuse when either LFO time exceeds its bound ."""
    if delay == 0:
        return 0, 0
    a = 99 - delay
    a = (16 + (a & 15)) << (1 + (a >> 4))
    inc1, inc2 = LFO_UNIT * a, LFO_UNIT * max(128, a & 0xff80)
    state, first = 0, None
    # Each stage must fit 60000 ms; one further control call proves refusal.
    for tick in range(1, 2 * LFO_MAX_MS * LFO_FS // (LFO_CTL * 1000) + 2):
        state = (state + (inc1 if state < 1 << 31 else inc2)) & 0xffffffff
        elapsed = half_up((tick - (first or 0)) * LFO_CTL * 1000, LFO_FS)
        if elapsed > LFO_MAX_MS:
            raise ValueError('LFO delay %d maps beyond LFO\'s %d ms %s bound' %
                             (delay, LFO_MAX_MS, 'fade' if first else 'delay'))
        if state < inc1:
            return half_up(first * LFO_CTL * 1000, LFO_FS), elapsed
        if first is None and state >= 1 << 31 and (state >> 7) & 0xffffff:
            first = tick
    raise ValueError('LFO delay %d has no full gain within LFO\'s time bounds' % delay)


def lfo_fields(voice, ops):
    """Pitch-only LFO fields, or no LFO when the rounded pitch depth is zero.
    AMD has no approved mapping. FREE 2 represents LFO sync-off ."""
    cents = half_up(1200 * ((voice['pmd'] * 165) >> 6) * PMS_TABLE[voice['pms']], 65536)
    if not cents:
        return None
    rate = lfo_rate(voice['speed'])
    delay, fade = lfo_times(voice['delay'])
    if not 10 <= rate <= 50000 or not 0 <= cents <= 1200:
        raise ValueError('LFO rate %d mHz or pitch %d cents is outside LFO bounds' % (rate, cents))
    wave = voice['wave']
    phase = 16384 if wave == 0 else (32768 if wave < 5 else 0)
    fields = (LFO_TO_W3[wave], rate, delay, fade, cents, 0, 0, phase, (1 << ops) - 1, 0, 0)
    return fields + ((2,) if not voice['sync'] else ())


def segment_ms(a, b, rate):
    """ms for the EG to go from level a to level b at rate (RS 0)."""
    step = eg_step(rate)
    if b <= a:
        return (a - b) / step / BLOCKS_PER_S * 1000
    x, blocks = max(a, JUMP), 0.0
    while x < b:                                    # a rise: per octave band of 8 steps
        k = int(x // 8)
        top = min(b, 8 * (k + 1))
        blocks += (top - x) / ((16 - k) * step)
        x = top
    return blocks / BLOCKS_PER_S * 1000


def ratio_of(o):
    """(the op's ratio, its fixed frequency in Hz or None): coarse 0 = 0.5, fine
    adds a hundredth of the coarse ratio a step, detune (DETUNE_CENTS a step from 7);
    a fixed op's 10^(coarse % 4 + fine / 100) Hz, its ratio the detune alone, as
    the engine plays FIXED_MILLIHZ times the ratio (: detune stays a ratio)."""
    with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        D = decimal.Decimal
        cents = D(2) ** (D(o['det'] - 7) * D(str(DETUNE_CENTS)) / 1200)
        fine = D(o['fine']) / 100
        if o['mode']:
            return cents, D(10) ** ((o['coarse'] & 3) + fine)
        return (D(o['coarse']) or D('0.5')) * (1 + fine) * cents, None


def operator(o, carrier, carriers, feedback, free=False):
    """One operator as op arguments (Decimal, floats and ints in op units) and the
    notes on it: {'args', 'top', 'clamped', 'l4'}. The operator extras  are set only where they are not 0: rs the DX7's rate scaling as it is,
    fixed a fixed-mode op's frequency in Hz (to the millihertz), free 2 when free
    (the voice's oscillator key sync off)."""
    lv = [eg_level(o['l%d' % i], o['ol']) for i in (1, 2, 3, 4)]
    rt = [o['r%d' % i] for i in (1, 2, 3, 4)]
    top = max(range(3), key=lambda i: (lv[i], -i))  # the first of the highest of L1 to L3
    t = [segment_ms(lv[3], lv[0], rt[0]), segment_ms(lv[0], lv[1], rt[1]), segment_ms(lv[1], lv[2], rt[2])]
    att = (FULL - lv[top]) * DB_STEP                 # the op's peak, dB below full scale
    a = max(1, round(sum(t[:top + 1])))
    d = round(sum(t[top + 1:]))
    r = round(RELEASE_DB / (eg_step(rt[3]) * DB_STEP * BLOCKS_PER_S) * 1000)
    clamped = [k for k, v in (('a', a), ('d', d), ('r', r)) if v > MAX_MS]
    with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        D = decimal.Decimal
        args = {'ratio': ratio_of(o)[0]}
        if carrier:
            args['level'] = D(str(att)) + D(str(HEADROOM_DB)) + 20 * D(carriers).log10()
            if args['level'] > MAX_DB:
                args['level'] = MAX_DB
                clamped.append('level')
        else:
            args['index'] = INDEX_FULL * D(10) ** (-D(str(att)) / 20)
        if feedback:
            args['fb'] = FB_FULL * D(2) ** (feedback - 7) * D(10) ** (-D(str(att)) / 20)
        args.update(vel=round(o['kvs'] * 256 / 7), a=min(a, MAX_MS), d=min(d, MAX_MS),
                    s=(lv[top] - lv[2]) * DB_STEP, r=min(r, MAX_MS))
        if not carrier and o['rd'] and o['rc'] in (0, 1):
            args['ks_break'] = D(440) * D(2) ** (D(o['bp'] + 21 - 69) / 12)
            args['ks_slope'] = o['rd'] * LIN_DB_OCT * (EXP_SHARE if o['rc'] else 1)
        if o['rs']:
            args['rs'] = o['rs']
        if o['mode']:
            args['fixed'] = D(round(ratio_of(o)[1] * 1000)) / 1000
        if free:
            args['free'] = 2
    return {'args': args, 'top': top, 'clamped': clamped, 'l4': o['l4'] and lv[3] > 0.5}


# ---------------------------------------------------------------------------
# Text: op arguments as recipes.txt writes them, exact to the engine's fields.

SCALE = {'ratio': (65536, 6), 'level': (10, 1), 's': (10, 1), 'index': (1024, 4), 'fb': (1024, 4),
         'ks_break': (1000, 3), 'ks_slope': (10, 1), 'fixed': (1000, 3)}
ORDER = ('ratio', 'level', 'index', 'fb', 'vel', 'a', 'd', 's', 'r', 'ks_break', 'ks_slope', 'rs', 'fixed', 'free')
SILENT = {'ratio': 1.0, 'index': 0.0}          # the modulator of a carrier that no sounding operator modulates


def value_text(key, value):
    """The shortest decimal, one place at least, that op turns back into the
    field the engine reads: ratio=4.23 for 3 * 1.41, not 4.229996."""
    if key not in SCALE:
        return '%d' % value
    with decimal.localcontext(decimal.Context(prec=NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        scale, places = SCALE[key]
        q = round(decimal.Decimal(str(value)) * scale)
        for p in range(1, places + 1):
            text = format(decimal.Decimal(q) / scale, '.%df' % p)
            if round(decimal.Decimal(text) * scale) == q:
                return text
    raise AssertionError((key, value))


def args_text(args, keys=ORDER):
    return ' '.join('%s=%s' % (k, value_text(k, args[k])) for k in keys if k in args)


def slug(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'voice'


def ops_list(ops):
    return ', '.join('OP%d' % o for o in ops)


def translate(number, voice, algs, source, prefix, limit, single):
    """One voice as a recipe block: {'name', 'text', 'route' (None for one operator), 'order' (the
    DX7 operators as engine operators 0 up), 'ops' (their op arguments), 'carrier' and 'modulator'
    (the pair lines'), ...}."""
    n_alg = voice['alg'] + 1
    alg = algs['dx7-%d' % n_alg]
    vo = voice['ops']
    ol = {o: vo[o]['ol'] for o in vo}
    kept, edges, dropped = reduce_ops(alg, ol, limit)
    order = engine_order(kept, edges)
    cars = [c for c in kept if c in alg['carriers']]
    free = not voice['oks']                         # key sync off: continuous FREE 2
    lone = len(kept) == 1                           # one operator: a pair with a silent modulator, no route
    fb_op = alg['feedback'] if voice['fb'] and alg['feedback'] in kept and not lone else None
    ops = {o: operator(vo[o], o in cars, len(cars), voice['fb'] if o == fb_op else 0, free) for o in kept}
    main_c, main_m = pair_of(alg, ol)
    fb_two = bool(voice['fb']) and main_m == alg['feedback']
    n_two = min(len(sounding(alg, ol)), FM_OPS)     # the carriers kept at 4 operators, whatever --ops says
    two = {main_c: operator(vo[main_c], True, n_two, 0, free)}
    if main_m:
        two[main_m] = operator(vo[main_m], False, n_two, voice['fb'] if fb_two else 0, free)
    info = dict(two)
    info.update(ops)
    shown = sorted(info)                            # the kept operators and the pair lines'
    name = '%s-%s%s' % (prefix, '' if single else '%02d-' % number, slug(voice['name']))
    route = route_words(order, cars, edges) if not lone else None
    vname = voice['name'].strip() or 'unnamed'

    notes = ['DX7 voice "%s", translated approximately by dx7_import.py (, ); untested by a piece' % vname]
    text = 'routing: algorithm %d' % n_alg
    text += ' as route %s' % ' '.join(route) if route else ', one operator, so no route'
    text += '; kept %s' % ops_list(kept)
    if dropped:
        text += '; dropped %s by the reduce rule' % ', '.join('OP%d (%s)' % (o, dropped[o]) for o in sorted(dropped))
    if route:
        text += "; the block's lines are %s, engine operators 0 to %d" % (ops_list(order), len(order) - 1)
    else:
        text += "; the block's lines are OP%d and a silent modulator" % kept[0]
    text += '; the pair lines are OP%d and %s' % (
        main_c, 'OP%d' % main_m if main_m else 'a silent modulator, OP%d having none' % main_c)
    off = [o for o in (main_c, main_m) if o and o not in kept]
    if off:
        text += ' (%s from before the reduction)' % ops_list(off)
    if voice['fb']:
        if n_alg in LOOPS and (fb_op or fb_two):
            text += "; the DX7's loop from OP%d back to OP6 as OP6's own feedback" % LOOPS[n_alg]
        if fb_op is None and alg['feedback'] in kept:
            text += ("; feedback %d on OP%d dropped: one operator plays as a pair, whose carrier has no fb"
                     % (voice['fb'], alg['feedback']))
        elif fb_op is None:
            text += '; feedback %d dropped with OP%d%s' % (
                voice['fb'], alg['feedback'], ' from the block, kept in the pair lines' if fb_two else '')
        elif not fb_two:
            text += "; OP%d's feedback plays in the block only, not in the pair lines" % fb_op
    notes.append(text)
    notes.append("levels: TL by the DX7's published level table (0.75 dB a step from 99, steeper below 20); "
                 "a modulator's index 4 pi rad and feedback 7 2 pi rad at TL 99, feedback halving a step; "
                 "carriers %g dB plus 20 log10(%d) below full scale" % (HEADROOM_DB, len(cars)))
    text = ("envelopes: the DX7's published rate and level tables at RS 0, folded: the segments up to the "
            "highest of L1 to L3 make the attack, straight in amplitude, the rest one straight line in dB to L3, "
            "and R4 the release, to silence")
    peaks = ['OP%d L%d' % (o, info[o]['top'] + 1) for o in shown if info[o]['top']]
    if peaks:
        text += '; the peak at %s' % ', '.join(peaks)
    l4 = [o for o in shown if info[o]['l4']]
    if l4:
        text += "; %s's L4 above silence taken as silence" % ops_list(l4)
    clamped = ['OP%d %s' % (o, k) for o in shown for k in info[o]['clamped']]
    if clamped:
        text += '; clamped to its range: %s' % ', '.join(clamped)
    notes.append(text)
    rs = [(o, vo[o]['rs']) for o in shown if vo[o]['rs']]
    if rs:
        notes.append("rate scaling: %s as rs, as it is (the engine's scale, : every segment 2^(rs/8) times "
                     "as fast an octave above A0, to A8); the times are the DX7's at RS 0, as at A0" % ', '.join(
                         'OP%d %d' % x for x in rs))
    text = 'pitch: coarse and fine to Q16.16, detune as %g cent a step from 7' % DETUNE_CENTS
    fixed = [(o, ratio_of(vo[o])[1]) for o in shown if vo[o]['mode']]
    if fixed:
        text += '; %s as fixed to the millihertz, its ratio the detune alone ()' % ', '.join(
            'OP%d fixed at %.3f Hz' % f for f in fixed)
    if voice['transpose'] != 24:
        text += '; transpose %+d semitones not kept: move the notes' % (voice['transpose'] - 24)
    notes.append(text)
    if free:
        notes.append("oscillators: key sync off as free=2 on every operator, advancing across rendered idle "
                     "samples (); voice-local phase, not a shared DX7 clock")
    ks = []
    for o in shown:
        p, a = vo[o], info[o]['args']
        if p['ld'] == 0 and p['rd'] == 0:
            continue
        if o in alg['carriers']:
            ks.append("OP%d's not kept (a carrier:  scales an index)" % o)
            continue
        if 'ks_slope' in a:
            ks.append('OP%d right %s %d as %s dB/oct above %s Hz' % (
                o, ('-LIN', '-EXP')[p['rc']], p['rd'], value_text('ks_slope', a['ks_slope']),
                value_text('ks_break', a['ks_break'])))
        elif p['rd']:
            ks.append("OP%d's right %s not kept ( only attenuates)" % (o, ('+EXP', '+LIN')[p['rc'] - 2]))
        if p['ld']:
            ks.append("OP%d's left side not kept ( has none)" % o)
    if ks:
        notes.append('key scaling: break point 39 = C3, the DX7\'s steps of 3 semitones as a straight slope, '
                     '-EXP as a sixth of the -LIN slope; ' + '; '.join(ks))
    notes.append("velocity: KVS to vel, 7 = 256, through the engine's velocity curve")
    if voice['clamped']:
        notes.append('clamped on reading: %s' % ', '.join(voice['clamped']))

    lines = ['# DX7 voice %d of %s, "%s": algorithm %d, feedback %d on OP%d; %d of 6 operators kept'
             % (number, one_line(source), vname, n_alg, voice['fb'], alg['feedback'], len(kept)),
             'recipe %s' % name]
    if route:                                       # the grammar : route, then operators 0 up
        lines.append('route     %s' % ' '.join(route))
        for i, o in enumerate(order):
            keys = [k for k in ORDER if not (o in cars and k in ('index', 'ks_break', 'ks_slope'))]
            lines.append('%-9s %s' % (('carrier', 'modulator', 'op 2', 'op 3')[i], args_text(ops[o]['args'], keys)))
    else:
        a = ops[kept[0]]['args']
        lines.append('carrier   %s' % args_text(a, [k for k in ORDER if k not in ('index', 'fb', 'ks_break', 'ks_slope')]))
        lines.append('modulator %s' % args_text(SILENT))
    try:
        lfo = lfo_fields(voice, len(kept))
    except ValueError as e:
        die('voice %d "%s": %s' % (number, vname, e))
    if lfo is not None:
        lines.append('lfo       ' + ' '.join(map(str, lfo)))
    # one note line: compose.py writes the joined notes as the comment on operator 0's patch line,
    # and the harness refuses a line longer than 1022 bytes; the rest are comments
    lines += ['note      %s' % notes[0]] + ['# %s' % t for t in notes[1:]]
    car = two[main_c]['args']
    mod = two[main_m]['args'] if main_m else SILENT
    lines.append('# pair: carrier   %s' % args_text(car, [k for k in ORDER if k not in ('index', 'fb', 'ks_break', 'ks_slope')]))
    lines.append('# pair: modulator %s' % args_text(mod, [k for k in ORDER if k != 'level']))
    ams = [(o, vo[o]['ams']) for o in shown if vo[o]['ams']]
    if voice['pmd'] or voice['pms']:
        lines.append("# lfo: the DX7's %s (LFO wave %d), speed %d, delay %d, pitch depth %d "
                     "at sensitivity %d, a peak deviation of %.1f cents by source-traced msfa scaling; "
                     "rounded pitch %d cents, key sync %s; phase, first-tick timing and random sequence approximate"
                     % (LFO_WAVES[voice['wave']], LFO_TO_W3[voice['wave']], voice['speed'], voice['delay'],
                        voice['pmd'], voice['pms'], lfo_cents(voice['pmd'], voice['pms']),
                        lfo[4] if lfo else 0, ('off', 'on')[voice['sync']]))
    if voice['amd'] or ams:
        lines.append('# pending: amplitude modulation (): amplitude depth %d at sensitivity %s; '
                     'DX7 units, no synthetic AMD mapping' %
                     (voice['amd'], ', '.join('OP%d %d' % x for x in ams) or '0'))
    peg = [voice['pl%d' % i] for i in (1, 2, 3, 4)]
    if any(p != 50 for p in peg):
        lines.append("# pending: peg (envelope, ): levels %s cents by the DX7's pitch EG level table (50 = 0 cents), "
                     "rates %s in DX7 units until envelope lands"
                     % (' '.join('%d' % round(PEG_TABLE[p] * PEG_CENTS) for p in peg),
                        ' '.join('%d' % voice['pr%d' % i] for i in (1, 2, 3, 4))))
    return {'name': name, 'text': '\n'.join(lines), 'route': route, 'order': order,
            'carrier': car, 'modulator': mod, 'pair': (main_c, main_m), 'kept': kept, 'dropped': dropped,
            'ops': ops, 'free': free}


def header(source, count):
    return ('# %d recipe block%s from %s by dx7_import.py (, , ): approximate translations of\n'
            '# DX7 voices, never emulator-exact. compose.py loads them (recipe(), --expand); README.md in\n'
            '# music, DX7 import, explains the route, operator, note, pair and pending lines.\n'
            % (count, '' if count == 1 else 's', one_line(source)))


def one_line(s):
    """s for a comment line: each character that could end the line (CR, LF and every other one
    that does not print) written as its Python escape, so a file name cannot add a statement."""
    return ''.join(c if c.isprintable() else repr(c)[1:-1] for c in s)


def convert(data, source, voice=None, limit=FM_OPS, prefix='dx7', algs=None):
    """The text of the blocks for data (a file's bytes), and the translations."""
    voices = read_voices(data, source)
    if voice is not None:
        if not 1 <= voice <= len(voices):
            die('%s holds voice%s 1%s; there is no voice %d'
                % (source, 's' if len(voices) > 1 else '', ' to %d' % len(voices) if len(voices) > 1 else '', voice))
        voices = [voices[voice - 1]]
    algs = algs or load_algorithms()
    single = len(data) != 4104
    done = [translate(n, v, algs, source, prefix, limit, single) for n, v in voices]
    return header(source, len(done)) + '\n' + '\n\n'.join(t['text'] for t in done) + '\n', done


# ---------------------------------------------------------------------------
# --into FILE : append the blocks to an existing recipes file. A name the
# file holds, or one given earlier in the same import, takes the first free
# suffix, in bank order; the whole file as it would be is loaded through
# compose.py's loader before a byte is appended, and the existing text is never
# rewritten. Every importer shares these helpers .

SUFFIX = '-%d'          # the collision suffix, -2, -3, ...: provisional (; regression's P row)


def free_names(taken, names):
    """names in bank order, each that collides with taken or with a name given
    earlier in this list renamed to NAME-2, NAME-3, ..., the first that is free."""
    seen, out = set(taken), []
    for name in names:
        new, k = name, 2
        while new in seen:
            new, k = name + SUFFIX % k, k + 1
        seen.add(new)
        out.append(new)
    return out


def recipes_in(path):
    """The recipes compose.py's loader reads from path, {name: definition} in the
    file's order. A file the loader refuses raises ValueError with the loader's message."""
    sys.dont_write_bytecode = True
    if MUSIC not in sys.path:
        sys.path.insert(0, MUSIC)
    import compose
    try:
        return compose.load_recipes(path)
    except SystemExit as e:
        raise ValueError(str(e))


def loaded_names(path):
    """The recipe names compose.py's loader reads from path, in the file's order."""
    return list(recipes_in(path))


def whole_file(text, names, held=None):
    """Load text, the target file with the import appended, through compose.py's
    loader in a temporary file, and check that it reads exactly names, in order,
    and each recipe of held ({name: definition}, the file as it was) unchanged."""
    fd, tmp = tempfile.mkstemp(prefix='into-', suffix='.txt')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        got = recipes_in(tmp)
    except ValueError as e:
        raise ValueError('the import is refused as a whole: %s' % str(e).replace(tmp, 'the file with it appended'))
    finally:
        os.remove(tmp)
    if list(got) != names:
        raise ValueError('the import is refused as a whole: the loader reads %d names, not the %d expected'
                         % (len(got), len(names)))
    for name in held or {}:
        if got[name] != held[name]:
            raise ValueError('the import is refused as a whole: it would change recipe %s, which the file holds'
                             % name)


def into(path, head, blocks, tool):
    """Append blocks (each {'name', 'text'} whose text holds the line `recipe NAME`
    once) under the comment lines head to the recipes file path. Returns the
    [(name, new name)] renames; raises ValueError, path untouched, on any fault."""
    if not os.path.isfile(path):
        raise ValueError('%s: --into appends to an existing recipes file; --out writes a new one' % path)
    with open(path, encoding='utf-8') as f:
        old = f.read()
    held = recipes_in(path)
    have = list(held)
    names = free_names(have, [b['name'] for b in blocks])
    texts = []
    for b, new in zip(blocks, names):
        line = 'recipe %s\n' % b['name']
        if b['text'].count(line) != 1:
            raise ValueError('block %s: no single `recipe %s` line' % (b['name'], b['name']))
        texts.append(b['text'].replace(line, 'recipe %s\n' % new, 1))
    renames = [(b['name'], new) for b, new in zip(blocks, names) if new != b['name']]
    head += ''.join('# --into: %s renamed %s, a name %s already held ()\n'
                    % (o, n, one_line(os.path.basename(path))) for o, n in renames)
    gap = '' if not old or old.endswith('\n\n') else '\n' if old.endswith('\n') else '\n\n'
    add = gap + head + '\n' + '\n\n'.join(texts) + '\n'
    whole_file(old + add, have + names, held)
    with open(path, 'a', encoding='utf-8', newline='\n') as f:
        f.write(add)
    sys.stderr.write('%s: %d block%s appended to %s, %d renamed\n'
                     % (tool, len(blocks), '' if len(blocks) == 1 else 's', path, len(renames)))
    return renames


# ---------------------------------------------------------------------------
# The self-test.

def blank_voice(name):
    """The unpacked values of the DX7's initial voice: OP1 alone at 99, algorithm 1."""
    v = []
    for n in (6, 5, 4, 3, 2, 1):
        v += [99, 99, 99, 99, 99, 99, 99, 0, 39, 0, 0, 0, 0, 0, 0, 0, 99 if n == 1 else 0, 0, 1, 0, 7]
    v += [99, 99, 99, 99, 50, 50, 50, 50, 0, 0, 1, 35, 0, 0, 0, 1, 0, 3, 24]
    return v + [ord(c) for c in name.ljust(10)[:10]]


def set_op(v, n, **kw):
    for k, x in kw.items():
        if k in ('r', 'l'):
            for i, y in enumerate(x):
                v[21 * (6 - n) + OP_FIELDS.index('%s%d' % (k, i + 1))] = y
        else:
            v[21 * (6 - n) + OP_FIELDS.index(k)] = x


def set_voice(v, **kw):
    for k, x in kw.items():
        if k in ('pr', 'pl'):
            for i, y in enumerate(x):
                v[126 + VOICE_FIELDS.index('%s%d' % (k, i + 1))] = y
        else:
            v[126 + VOICE_FIELDS.index(k)] = x


def selftest_voices():
    """Three voices worked out by hand in README.md's DX7 import section."""
    v1 = blank_voice('TEST STACK')                    # algorithm 1, every op sounding: OP5 and OP6 must go
    set_voice(v1, alg=0, fb=7, speed=30, delay=20, pmd=5, wave=4, pms=2, pl=(60, 50, 50, 50), pr=(90, 60, 99, 99))
    set_op(v1, 1, r=(95, 40, 30, 50), l=(99, 80, 0, 0), ol=99, kvs=3, rs=2, rd=20)
    set_op(v1, 2, r=(99, 50, 35, 50), l=(99, 70, 0, 0), ol=80, kvs=7, det=10, rd=40)
    set_op(v1, 3, r=(80, 30, 20, 40), l=(99, 85, 60, 0), ol=90, coarse=2, det=4)
    set_op(v1, 4, r=(99, 60, 40, 50), l=(99, 60, 30, 0), ol=75, coarse=3, fine=41)
    set_op(v1, 5, ol=70)
    set_op(v1, 6, ol=65)
    v2 = blank_voice('TEST FEEDBK')                   # algorithm 18, OP5 and OP6 silent, feedback 5 on OP3 kept
    set_voice(v2, alg=17, fb=5, oks=0, pms=0)       # key sync off: free=2 on every operator
    set_op(v2, 1, r=(99, 45, 30, 60), l=(99, 90, 70, 0), ol=99, kvs=2, rs=3)
    set_op(v2, 2, r=(99, 55, 40, 60), l=(99, 75, 50, 0), ol=78, kvs=5, rs=7)
    set_op(v2, 3, r=(99, 70, 30, 60), l=(99, 50, 40, 0), ol=72, coarse=2, det=9)
    set_op(v2, 4, r=(90, 40, 99, 60), l=(99, 99, 99, 0), ol=60, mode=1, coarse=2)
    v3 = blank_voice('TEST PAIRS')                    # algorithm 5: three carriers, one room left, a tie
    set_voice(v3, alg=4, fb=4, transpose=12)
    set_op(v3, 1, r=(60, 50, 40, 45), l=(80, 99, 70, 20), ol=95, coarse=0, fine=50, kvs=4)
    set_op(v3, 2, r=(99, 60, 50, 50), l=(99, 80, 60, 0), ol=85, det=0, bp=48, ld=30, rd=50, lc=3, rc=1)
    set_op(v3, 3, r=(99, 50, 50, 50), l=(99, 90, 80, 0), ol=88, det=14)
    set_op(v3, 4, ol=85)
    set_op(v3, 5, r=(99, 30, 20, 70), l=(99, 70, 50, 0), ol=85, coarse=4)
    set_op(v3, 6, ol=70)
    return [v1, v2, v3]


SELFTEST_HASH = 'dc75910be38443c4'   # voice 2's block, one note at the 12/10 build; an engine change that
                                     # moves the golden hashes moves it: re-pin it with the reason. Was
                                     # b0c031106ef1662b until T3b: OP4 fixed=100.0 in place of the ratio
                                     # 25050/65536 of C3 gave 7616b6227f237959, rs 3 and 7 on OP1 and OP2 this
                                     # one; FREE 2 starts this note at sample 0, with no idle gap


def selftest():
    out = os.path.join(SPIKE, 'out', 'dx7_selftest')
    os.makedirs(out, exist_ok=True)
    results = []

    def check(what, got, hand, how):
        if isinstance(hand, float):
            expected = decimal.Decimal(str(hand))
            tolerance = decimal.Decimal('1e-9') * max(1, abs(expected))
            ok = abs(decimal.Decimal(str(got)) - expected) < tolerance
        else:
            ok = got == hand
        results.append(ok)
        short = lambda x: str(x) if not ok or len(str(x)) <= 30 else 'the same, %d characters' % len(str(x))
        print('%-4s %-44s got %-14s hand %-14s %s' % ('ok' if ok else 'FAIL', what, short(got), short(hand), how))
        return ok

    def write(name, data):
        path = os.path.join(out, name)
        with open(path, 'wb' if isinstance(data, bytes) else 'w', encoding=None if isinstance(data, bytes) else 'utf-8', newline=None if isinstance(data, bytes) else '\n') as f:
            f.write(data)
        return path

    voices = selftest_voices()
    init = blank_voice('INIT VOICE')
    bank = frame(b''.join(pack(v) for v in voices + [init] * 29))
    print('dx7_import.py --selftest: a synthetic bank of 32 voices (3 worked by hand, 29 initial), %d bytes'
          % len(bank))
    check('pack then unpack', [unpack(pack(v)) for v in voices], voices, 'a bank voice round trip')
    print('-- the tables')
    check('TL 99 to dB below TL 99', (127 - out_level(99)) * DB_STEP, 0.0, '(99-99)*0.75')
    check('TL 50', (127 - out_level(50)) * DB_STEP, 36.75, '(99-50)*0.75')
    check('TL 10', (127 - out_level(10)) * DB_STEP, 72.0, '(127-31)*0.75, table(10) = 31')
    check('TL 0', (127 - out_level(0)) * DB_STEP, 95.25, '127*0.75')
    check('EG rate 50: steps a block', eg_step(50), 0.125, 'q = 50*41>>6 = 32: 4*2^(8-13)')
    check('R4 50 to r (ms per 96.3 dB)', round(RELEASE_DB / (eg_step(50) * DB_STEP * BLOCKS_PER_S) * 1000), 1491,
          '96.3/(0.125*0.75*689.0625) s')
    check('attack R1 50, L4 0 to L1 99, TL 99 (ms)', round(segment_ms(eg_level(0, 99), FULL, 50)), 173,
          '118.954 blocks: 14.869/0.125')
    check('KVS 3 to vel', round(3 * 256 / 7), 110, '3*256/7 = 109.7')
    check('LFO pitch depth 5, sensitivity 2 (cents)', '%.1f' % lfo_cents(5, 2), '4.4', '(5*165>>6)*20/65536 oct = 4.39')
    check('LFO pitch depth 99, sensitivity 7 (cents)', '%.1f' % lfo_cents(99, 7), '1190.6', '255*255/65536 octave')
    check('LFO rates at speed 0, 1, 30, 62, 63, 99', [lfo_rate(s) for s in (0, 1, 30, 62, 63, 99)],
          [65, 129, 4968, 10258, 10452, 23929], '33587 * sr * 48000 * 1000 / (64 * 2^32), half up')
    check('LFO half-up ties', [half_up(n, 2) for n in (1, 3, 5)], [1, 2, 3], 'never half-even')
    for delay, expected in ((0, (0, 0)), (1, (37, 37)), (20, (87, 95)), (50, (315, 332)),
                            (74, (853, 2184)), (82, (1255, 4504))):
        check('LFO delay %d to delay/fade ms' % delay, lfo_times(delay), expected,
              'literal uint32 recurrence, first positive/full returns')
    for delay in (83, 84, 99):
        try:
            lfo_times(delay)
            refused = False
        except ValueError as e:
            refused = '60000 ms fade bound' in str(e)
        check('LFO delay %d refused' % delay, refused, True, 'no LFO time clamp')
    lv = decode(voices[0])
    check('LFO integer pitch depth and complete fields', lfo_fields(lv, 4),
          (0, 4968, 87, 95, 4, 0, 0, 32768, 15, 0, 0), 'voice 1, no AMD')
    check('LFO six wave/phase mappings', [(lfo_fields(dict(lv, wave=w), 4)[0],
                                         lfo_fields(dict(lv, wave=w), 4)[7]) for w in range(6)],
          [(1, 16384), (3, 32768), (2, 32768), (4, 32768), (0, 32768), (5, 0)], 'regressionb phases')
    check('LFO sync off and lone-operator mask', lfo_fields(dict(lv, sync=0), 1)[8:],
          (1, 0, 0, 2), 'FREE 2 and no silent-modulator mask bit')
    check('LFO maximum mapped pitch', lfo_fields(dict(lv, pmd=99, pms=7), 4)[4], 1191, '1190.6 rounds up')
    check('LFO zero rounded pitch emits nothing', [lfo_fields(dict(lv, pmd=p, pms=s, amd=99), 4)
                                                for p, s in ((0, 7), (99, 0), (1, 1))],
          [None] * 3, 'no synthetic AMD or disabled LFO')
    try:
        lfo_fields(dict(lv, pmd=200, pms=7), 4)
        refused = False
    except ValueError as e:
        refused = 'outside LFO bounds' in str(e)
    check('out-of-range mapped pitch refused', refused, True, 'no depth clamp')
    algs = load_algorithms()
    _, done = convert(bank, 'bank.syx', algs=algs)
    t1, t2, t3 = done[0], done[1], done[2]
    print('-- voice 1, "TEST STACK": algorithm 1, every operator sounding, feedback 7 on OP6')
    check('kept', t1['kept'], [1, 2, 3, 4], 'carriers 1, 3; depth 1: 2, 4')
    check('dropped', t1['dropped'], {5: 'depth 2', 6: 'depth 3'}, 'OP5 depth 2, OP6 depth 3')
    check('route', ' '.join(t1['route']), '4 5 1>0:256 3>2:256', 'golden route4\'s routing')
    c, m = t1['ops'][1]['args'], t1['ops'][2]['args']
    check('OP1 level (dB)', value_text('level', c['level']), '18.0', '0 + 12 + 20 log10(2) = 18.02')
    check('OP1 attack R1 95 (ms)', c['a'], 1, '0.929 blocks = 1.35 ms')
    check('OP1 decay R2 40 + R3 30 (ms)', c['d'], 11443, '18 steps/0.0390625 + 101.5/0.013671875 blocks')
    check('OP1 sustain L3 0 (dB)', value_text('s', c['s']), '89.6', '(120-0.5)*0.75 = 89.625')
    check('OP2 index (rad)', value_text('index', m['index']), '2.437', '4 pi 10^(-14.25/20) = 2.43619: 2495/1024')
    check('OP2 ratio, detune 10', value_text('ratio', m['ratio']), '1.00174', '2^(3/1200) = 1.001734: 65650/65536')
    check('OP2 vel, KVS 7', m['vel'], 256, '7*256/7')
    check('OP2 key scaling slope', value_text('ks_slope', m['ks_slope']), '9.6', '40*1316/4096*0.75 = 9.64')
    check('OP2 key scaling break', value_text('ks_break', m['ks_break']), '261.626', 'BP 39 = C3 = MIDI 60')
    check('OP2 decay R2 50 + R3 35 (ms)', m['d'], 4814, '28/0.125 + 72.5/0.0234375 blocks')
    check('OP4 ratio 3 * 1.41', value_text('ratio', t1['ops'][4]['args']['ratio']), '4.23', 'coarse 3, fine 41: 277217/65536')
    check('lfo line: depth, sensitivity and cents', 'pitch depth 5 at sensitivity 2, a peak deviation of 4.4 cents'
          in t1['text'], True, 'the raw values kept beside the cents')
    print('-- voice 2, "TEST FEEDBK": algorithm 18, OP5 and OP6 silent, feedback 5 on OP3')
    check('dropped', t2['dropped'], {5: 'output level 0', 6: 'output level 0'}, 'silent first')
    check('route', ' '.join(t2['route']), '4 1 1>0:256 2>0:256 3>0:256', 'OP2, OP3, OP4 on OP1')
    o3, o4 = t2['ops'][3]['args'], t2['ops'][4]['args']
    check('OP3 feedback (rad)', value_text('fb', o3['fb']), '0.152', '2 pi 2^-2 10^(-20.25/20) = 0.15262: 156/1024')
    check('OP3 index (rad)', value_text('index', o3['index']), '1.221', '4 pi 10^(-20.25/20) = 1.22098: 1250/1024')
    check('OP4 fixed 100 Hz: ratio, fixed', (value_text('ratio', o4['ratio']), value_text('fixed', o4['fixed'])),
          ('1.0', '100.0'), '10^(2 + 0/100) Hz, detune 7: ratio 1')
    check('rs: OP1 3, OP2 7, OP3 and OP4 none', [t2['ops'][o]['args'].get('rs') for o in (1, 2, 3, 4)],
          [3, 7, None, None], 'the DX7 rate scaling as it is ()')
    check('key sync off: free=2 on every operator', [t2['ops'][o]['args'].get('free') for o in (1, 2, 3, 4)],
          [2, 2, 2, 2], 'oks 0 (, )')
    check('voice 2 block lines', [l.split()[0] for l in t2['text'].splitlines()[2:7]],
          ['route', 'carrier', 'modulator', 'op', 'op'], "the grammar: route, operators 0 to 3")
    check('voice 1: rs=2 on OP1 alone, key sync on: no free',
          ([o for o in t1['ops'] if 'rs' in t1['ops'][o]['args']], t1['ops'][1]['args']['rs'], 'free=' in t1['text']),
          ([1], 2, False), 'RS 2 on OP1 only; oks 1')
    check('voice 2 pair lines: rs, free', [l.split()[-2:] for l in t2['text'].splitlines() if l.startswith('# pair: ')],
          [['rs=3', 'free=2'], ['rs=7', 'free=2']], 'the pair: OP1 and OP2, from the whole voice')
    check('OP4 flat EG: d and s', (o4['d'], value_text('s', o4['s'])), (0, '0.0'), 'L1 = L2 = L3')
    check('OP4 index (rad)', value_text('index', o4['index']), '0.434', '4 pi 10^(-29.25/20) = 0.43323: 444/1024')
    print('-- voice 3, "TEST PAIRS": algorithm 5, three carriers, room for one modulator')
    check('dropped', t3['dropped'], {4: 'depth 1', 6: 'depth 1'}, 'OP2 and OP4 tie at 85: the lower stays')
    check('route', ' '.join(t3['route']), '4 13 1>0:256', 'carriers at engine ops 0, 2, 3')
    o1, o2 = t3['ops'][1]['args'], t3['ops'][2]['args']
    check('OP1 peak at L2: attack R1 60 + R2 50 (ms)', o1['a'], 98, '17.874 + 49.333 blocks')
    check('OP1 decay R3 40 (ms)', o1['d'], 1040, '28/0.0390625 blocks')
    check('OP1 sustain (dB)', value_text('s', o1['s']), '21.0', '(116-88)*0.75')
    check('OP1 level (dB)', value_text('level', o1['level']), '24.5', '3 + 12 + 20 log10(3) = 24.54')
    check('OP1 ratio, coarse 0 fine 50', value_text('ratio', o1['ratio']), '0.75', '0.5*1.5')
    check('OP1 release R4 45 (ms)', o1['r'], 2981, '96.3/(0.0625*0.75*689.0625) s')
    check('OP2 -EXP slope', value_text('ks_slope', o2['ks_slope']), '2.0', '50*1316/4096*0.75/6 = 2.008')
    check('OP2 break, BP 48', value_text('ks_break', o2['ks_break']), '440.0', 'MIDI 69')
    check('OP2 ratio, detune 0', value_text('ratio', o2['ratio']), '0.99597', '2^(-7/1200) = 0.995965: 65272/65536')
    print('-- the 2-operator lines: from the whole voice, the same at every --ops')
    at99 = []
    for n in range(32):                             # each algorithm with every operator at 99, feedback 7
        v = blank_voice('ALG %d' % (n + 1))
        set_voice(v, alg=n, fb=7)
        for o in range(1, 7):
            set_op(v, o, ol=99)
        at99.append(v)
    bank99 = frame(b''.join(pack(v) for v in at99))
    pair_lines = lambda t: [l for l in t['text'].splitlines() if l.startswith('# pair: ')]
    by_ops = {(src, k): convert(data, src, limit=k, algs=algs)[1]
              for src, data in (('bank99.syx', bank99), ('bank.syx', bank)) for k in (2, 3, 4)}
    differ = ['%s at --ops %d' % (t['name'], k) for src in ('bank99.syx', 'bank.syx') for k in (2, 3)
              for t, u in zip(by_ops[src, k], by_ops[src, 4]) if pair_lines(t) != pair_lines(u)]
    check('pair lines: --ops 2, 3 as 4', differ, [], '64 voices: the 32 algorithms at 99, the bank')
    four = by_ops['bank99.syx', 4]
    check('pairs at 99: algorithms 1, 21, 23, 31, 32', [four[n - 1]['pair'] for n in (1, 21, 23, 31, 32)],
          [(1, 2), (1, 3), (2, 3), (5, 6), (1, None)], 'of equal carriers, a modulated one first')
    check('a silent modulator at 99', [n + 1 for n, t in enumerate(four) if t['pair'][1] is None], [32],
          'only algorithm 32 has no edge')
    t = by_ops['bank99.syx', 2][1]
    check('algorithm 2, --ops 2: kept, modulator fb', (t['kept'], value_text('fb', t['modulator']['fb'])),
          ([1, 3], '6.283'), 'OP2 dropped from the block, its feedback 7 kept in the pair')
    print('-- the files')
    bank_path = write('bank.syx', bank)
    write('voice.syx', frame(voices[1]))
    write('short.syx', bank[:100])
    write('badsum.syx', bank[:-2] + bytes(((bank[-2] + 1) & 0x7F, 0xF7)))
    bad_lfo = list(voices[0])
    set_voice(bad_lfo, delay=83)
    write('badlfo.syx', frame(bad_lfo))
    py = [sys.executable, '-B', os.path.abspath(__file__)]
    recipes = os.path.join(out, 'bank.txt')
    r = subprocess.run(py + [bank_path, '--out', recipes], capture_output=True, text=True)
    check('bank to bank.txt: exit status', r.returncode, 0, r.stderr.strip() if r.returncode else 'the CLI, --out')
    write('voice155.syx', bytes(voices[1]))
    body = lambda text: text.split('\nrecipe ', 1)[1].split('\n', 1)[1].rstrip('\n')
    for single in ('voice.syx', 'voice155.syx'):
        r1 = subprocess.run(py + [os.path.join(out, single), '--name', 't3'], capture_output=True, text=True)
        check('%s reads as the bank\'s voice 2' % single, (r1.returncode, body(r1.stdout)), (0, body(t2['text'])),
              '%d bytes against the packed bank' % os.path.getsize(os.path.join(out, single)))
    for bad, word in (('short.syx', 'is 100 bytes'), ('badsum.syx', 'checksum'),
                      ('badlfo.syx', '60000 ms fade bound')):
        r = subprocess.run(py + [os.path.join(out, bad)], capture_output=True, text=True)
        check('%s refused' % bad, (r.returncode, word in r.stderr), (1, True), r.stderr.strip()[14:60])
    print('-- compose.py')
    sys.dont_write_bytecode = True
    sys.path.insert(0, MUSIC)
    try:
        import compose
        loaded = compose.load_recipes(recipes)
    except SystemExit as e:
        print('FAIL compose.py refused: %s' % e)
        return finish(out, results + [False])
    check('compose.load_recipes(bank.txt)', len(loaded), 32, 'one recipe a voice')
    try:
        loaded99 = [len(compose.load_recipes(write('bank99-%d.txt' % k, convert(bank99, 'bank99.syx', limit=k,
                                                                                 algs=algs)[0]))) for k in (2, 3, 4)]
    except SystemExit as e:
        print('FAIL compose.py refused a bank99 block: %s' % e)
        return finish(out, results + [False])
    check('compose.load_recipes(bank99), --ops 2, 3, 4', loaded99, [32] * 3, 'the 32 algorithms at 99: every route')
    try:
        pair = compose.load_recipes(write('pair.txt', 'recipe t3-pair\n%s\nnote voice 2\'s pair lines\n' % '\n'.join(
            l[len('# pair: '):] for l in pair_lines(t2))))
    except SystemExit as e:
        print('FAIL compose.py refused voice 2\'s pair lines as a block: %s' % e)
        return finish(out, results + [False])
    check('voice 2\'s pair lines as a block of their own', list(pair), ['t3-pair'], 'rs and free=2 on both load')
    print('-- --into ()')
    check('free_names: bank order', free_names({'a', 'b-2'}, ['a', 'a-2', 'b', 'c']), ['a-2', 'a-2-2', 'b', 'c'],
          'a: -2 free; a-2 now taken: -2-2; b and c free')
    check('free_names: the first free suffix', free_names({'b', 'b-2', 'b-4'}, ['b', 'b']), ['b-3', 'b-5'],
          'b-3 then b-5, past b-4')
    target = write('into.txt', open(recipes, encoding='utf-8').read())
    before = open(target, encoding='utf-8').read()
    names0 = list(loaded)
    runs = []
    for k in (1, 2):
        r = subprocess.run(py + [bank_path, '--into', target], capture_output=True, text=True)
        runs.append(r.returncode)
    after = open(target, encoding='utf-8').read()
    names = list(compose.load_recipes(target))
    check('--into twice into bank.txt: exit status', runs, [0, 0], 'the same bank appended twice')
    check('--into: the file\'s text is kept as it was', after.startswith(before), True, 'a prefix, byte for byte')
    check('--into: the names, in bank order', names, names0 + [n + '-2' for n in names0] + [n + '-3' for n in names0],
          '32, then 32 with -2, then 32 with -3')
    body = lambda text: [l for l in text.splitlines() if l.strip() and not l.startswith(('#', 'recipe '))]
    check('--into: the appended blocks are the --out blocks but their names', body(after[len(before):]),
          body(before) * 2, 'every line but the comments and the recipe lines, twice')
    r = subprocess.run(py + [os.path.join(out, 'voice.syx'), '--name', 't3', '--into', target], capture_output=True,
                       text=True)
    check('--into: a voice whose name is free keeps it', (r.returncode, list(compose.load_recipes(target))[-1]),
          (0, 't3-' + slug(decode(voices[1])['name'])), 'no suffix, no rename comment')
    kept = open(target, encoding='utf-8').read()
    for what, argv, code, word in (
            ('a bank with a refused voice', [os.path.join(out, 'badlfo.syx'), '--into', target], 1, 'fade bound'),
            ('a missing file', [bank_path, '--into', os.path.join(out, 'none.txt')], 1, 'existing recipes file'),
            ('--out with --into', [bank_path, '--out', os.path.join(out, 'x.txt'), '--into', target], 2, 'one of them'),
            ('an empty --into', [bank_path, '--into', ''], 2, 'not an empty one'),
            ('--out with an empty --into', [bank_path, '--out', target, '--into', ''], 2, 'one of them')):
        r = subprocess.run(py + argv, capture_output=True, text=True)
        check('--into refuses %s' % what, (r.returncode, word in r.stderr, open(target, encoding='utf-8').read() == kept),
              (code, True, True), 'the file unchanged; %s' % r.stderr.strip().splitlines()[-1][-50:])
    faulty = write('faulty.txt', 'recipe x\ncarrier ratio=1.0 level=20.0\nnote no modulator\n')
    r = subprocess.run(py + [bank_path, '--into', faulty], capture_output=True, text=True)
    check('--into refuses a file the loader refuses', (r.returncode, 'no modulator line' in r.stderr,
          open(faulty, encoding='utf-8').read()), (1, True, 'recipe x\ncarrier ratio=1.0 level=20.0\nnote no modulator\n'),
          'the file unchanged')
    good = {'name': 'ok-1', 'text': 'recipe ok-1\ncarrier ratio=1.0 level=20.0\nmodulator ratio=1.0 index=1.0\nnote a\n'}
    bad = {'name': 'ok-2', 'text': 'recipe ok-2\ncarrier ratio=1.0 level=20.0\nmodulator ratio=1.0 index=1.0\n'}
    small = write('small.txt', good['text'])
    try:
        into(small, '# selftest\n', [good, bad], 'selftest')
        refused = None
    except ValueError as e:
        refused = str(e)
    check('into(): a later block the loader refuses: nothing appended', (refused is not None and 'as a whole' in refused,
          open(small, encoding='utf-8').read()), (True, good['text']), 'the whole import is checked before the append')
    try:
        into(small, '# selftest\n', [good], 'selftest')
        got = list(compose.load_recipes(small))
    except (ValueError, SystemExit) as e:
        got = 'refused: %s' % e
    check('into(): the renamed block loads', got, ['ok-1', 'ok-1-2'], 'ok-1 held: ok-1-2, with its rename comment')
    odd = write('r\nfilter lp 1000000 707\n#.txt', good['text'])
    want = compose.load_recipes(odd)['ok-1']
    try:
        into(odd, '# selftest\n', [good], 'selftest')
        got = compose.load_recipes(odd)
        got = (list(got), got['ok-1'] == want, 'filter lp 1000000 707\n' in open(odd, encoding='utf-8').read())
    except (ValueError, SystemExit) as e:
        got = 'refused: %s' % e
    check('into(): a file name with a line break stays in its comment', got, (['ok-1', 'ok-1-2'], True, False),
          'ok-1 as it was; the name written r\\nfilter...')
    check('header(): a source name with a line break stays in its comment',
          header('v\nfilter lp 1000000 707\n#.syx', 1).count('\n'), header('v.syx', 1).count('\n'), 'three lines')
    named_text = convert(bank, 'v\nfilter lp 1000000 707\n#.syx', voice=1)[0]
    plain_text = convert(bank, 'v.syx', voice=1)[0]
    check('convert(): source stays on one comment line',
          (named_text.count('\n'), any(line.startswith('filter ') for line in named_text.splitlines())),
          (plain_text.count('\n'), False), 'a source filename cannot add a recipe statement')
    sneaky = dict(good, text='note joins the block before\n' + good['text'])
    small = write('small2.txt', good['text'])
    try:
        into(small, '# selftest\n', [sneaky], 'selftest')
        refused = None
    except ValueError as e:
        refused = str(e)
    check('into(): an import that would change a recipe the file holds', (refused is not None and 'change recipe ok-1'
          in refused, open(small, encoding='utf-8').read()), (True, good['text']), 'refused, nothing appended')
    compose.RECIPES = dict(compose.RECIPES, **loaded)    # the least invasive way: recipe reads RECIPES
    longest = max(len(l) for n in loaded for rows, route in [compose.patch_lines('t3voice', *compose.recipe(n))]
                  for l in rows + [route])
    check('the 32 blocks as patch lines: 1022 bytes', longest <= 1022, True, 'the harness\'s longest line; '
          'longest %d' % longest)
    car, mod, comment, extra = compose.recipe(t2['name'])
    check('recipe(): operators 0 to 3', [car, mod] + extra['more'],
          [compose.op(**t2['ops'][o]['args']) for o in t2['order']], 'OP1, OP2, OP3, OP4, extras and all')
    check('recipe(): rs, fixed, free of ops 0 to 3', [o[12:16] for o in [car, mod] + extra['more']],
          [[3, 0, 0, 2], [7, 0, 0, 2], [0, 0, 0, 2], [0, 100000, 0, 2]], 'RS, FIXED_MILLIHZ, PHASE_U16, FREE')
    check('recipe(): OP4 ratio under fixed', extra['more'][1][0], 65536, 'detune 7: 1.0 in Q16.16')
    src, dst = write('expand.tl.in', 'recipe t3voice %s\n' % t2['name']), os.path.join(out, 'expand.tl')
    try:
        compose.expand_timeline(src, dst)
    except SystemExit as e:
        print('FAIL --expand refused: %s' % e)
        return finish(out, results + [False])
    rows = [l.split()[0] for l in open(dst, encoding='utf-8') if l.startswith(('patch', 'route'))]
    check('--expand: patch and route lines', rows, ['patch'] * 4 + ['route'], 'operators 0 to 3, then the route')
    print('-- the harness')
    if subprocess.run(['make', '-C', SPIKE, '-s', 'SINE_BITS=12', 'EXP_BITS=10', 'PHASE64=1', 'ENV64=1']).returncode:
        print('FAIL the harness did not build')
        return finish(out, results + [False])
    tl = timeline(compose, 't3voice', t2)
    path = write('routed.tl', tl)
    play = subprocess.run([compose.H, '--play', path], capture_output=True, text=True)
    line = ([l for l in play.stdout.splitlines() if l.startswith('PLAY')] or [play.stderr.strip()])[0]
    print(line)
    kv = dict(w.split('=', 1) for w in line.split()[1:] if '=' in w)
    check('routed render: exit status', play.returncode, 0, 'route %s' % ' '.join(t2['route']))
    got = kv.get('hash')
    if SELFTEST_HASH:
        check('routed render: hash', got, SELFTEST_HASH, 'pinned')
    path = write('routed-pinned.tl', tl.replace('voice 0 t3voice\n', 'voice 0 t3voice\nhash %s\n' % got))
    again = subprocess.run([compose.H, '--play', path], capture_output=True, text=True)
    check('routed replay: match', ('match=yes' in again.stdout, again.returncode), (True, 0), 'the same hash again')
    for sync in (1, 0):
        vv = dict(decode(voices[0]), sync=sync, amd=99)
        mapped = translate(1, vv, algs, 'selftest', 'mapped%d' % sync, 4, True)
        compose.RECIPES.update(compose.load_recipes(write('mapped.txt', mapped['text'] + '\n')))
        test_tl = timeline(compose, 'mapped', mapped)
        check('mapped LFO %d is serialized in timeline' % sync, '\nlfo mapped ' in test_tl,
              True, 'patch_headers includes metadata')
        path = write('mapped.tl', test_tl)
        merged = os.path.join(out, 'mapped-merged.tl')
        played = subprocess.run([compose.H, '--play', path, '--merged', merged], capture_output=True, text=True)
        replay = subprocess.run([compose.H, '--play', merged], capture_output=True, text=True)
        check('mapped LFO %d renders and replays' % sync,
              (played.returncode, replay.returncode, 'block_indep=yes' in played.stdout, 'match=yes' in replay.stdout),
              (0, 0, True, True), 'key sync or FREE 2, AMD pending')
    bad = []
    for name in sorted(algs, key=lambda a: (a[:3], int(a.split('-')[1]))):
        alg = algs[name]
        kept, edges, _ = reduce_ops(alg, {o: 99 for o in range(1, alg['ops'] + 1)}, 4)
        order = engine_order(kept, edges)
        rows = [compose.patch_row('a', i, compose.op(1.0 + i, 20.0 if o in alg['carriers'] else 0.0,
                                                     0.0 if o in alg['carriers'] else 1.0, a=1, d=5, r=5))
                for i, o in enumerate(order)]
        words = route_words(order, alg['carriers'], edges)
        if any(int(w.split('>')[0]) <= int(w.split('>')[1].split(':')[0]) for w in words[2:]):
            bad.append(name + ' (a back edge)')
        text = TL_HEAD % (compose.ENGINE, 10000000, '\n'.join(rows) + '\nroute a %s' % ' '.join(words), 'a')
        p = subprocess.run([compose.H, '--play', write('alg.tl', text + '0 t3 0 on 261626 20000\n')],
                           capture_output=True, text=True)
        if p.returncode:
            bad.append(name)
    check('the 40 algorithms reduced: the harness takes each', bad, [], 'forward edges, rc 0')
    return finish(out, results)


TL_HEAD = '''timeline draft1
engine %s
fs 48000
length_ns %d
streams t3
%s
voice 0 %s
'''


def timeline(compose, pname, t):
    """One note of a translated voice's block as loaded: all patch headers as
    compose.py --expand writes them for p.patch(pname, *recipe(name))."""
    body = '\n'.join(compose.patch_headers(pname, *compose.recipe(t['name'])))
    return TL_HEAD % (compose.ENGINE, 1000000000, body, pname) + '0 t3 0 on 261626 24000\n600000000 t3 0 off\n'


def finish(out, results):
    failed = results.count(False)
    if failed:
        print('FAIL: %d of %d checks failed; the files stay in %s' % (failed, len(results), os.path.relpath(out)))
        return 1
    for name in sorted(os.listdir(out)):
        os.remove(os.path.join(out, name))
    os.rmdir(out)
    print('PASS: %d checks; %s removed' % (len(results), os.path.relpath(out)))
    return 0


def main():
    ap = argparse.ArgumentParser(prog='dx7_import.py', description='DX7 voices to recipe blocks (approximate).')
    ap.add_argument('file', nargs='?', help='a DX7 bank (4104 bytes) or voice (163 or 155 bytes)')
    ap.add_argument('--voice', type=int, help='only voice N (1 to 32 in a bank)')
    ap.add_argument('--ops', type=int, default=FM_OPS, choices=(2, 3, 4),
                    help='operators the block keeps (default 4, FM_OPS); the # pair: lines stay the same')
    ap.add_argument('--out', help='write the blocks here instead of to stdout')
    ap.add_argument('--into', metavar='FILE',
                    help='append the blocks to this existing recipes file, a name it holds taking -2, -3, ... '
                         '(); the whole import is checked first')
    ap.add_argument('--name', default='dx7', help='the recipe names\' prefix (default dx7)')
    ap.add_argument('--selftest', action='store_true', help='run the self-test')
    a = ap.parse_args()
    if a.selftest:
        if a.file or a.voice is not None or a.out is not None or a.into is not None:
            ap.error('--selftest takes no other argument')
        sys.exit(selftest())
    if a.out is not None and a.into is not None:
        ap.error('--out writes a new file and --into appends to one: give one of them')
    if a.out == '' or a.into == '':
        ap.error('--out and --into take a file name, not an empty one')
    if not a.file:
        ap.error('name a bank or a voice, or --selftest')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', a.name):
        ap.error('--name is letters, digits and . _ -, starting with a letter or digit')
    try:
        with open(a.file, 'rb') as f:
            data = f.read()
    except OSError as e:
        die('%s: %s' % (a.file, e.strerror))
    text, done = convert(data, os.path.basename(a.file), a.voice, a.ops, a.name)
    if a.into is not None:
        try:
            into(a.into, header(os.path.basename(a.file), len(done)), done, 'dx7_import.py')
        except ValueError as e:
            die(str(e))
    elif a.out is not None:
        with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        sys.stderr.write('dx7_import.py: %d block%s from %s into %s\n'
                         % (len(done), '' if len(done) == 1 else 's', os.path.basename(a.file), a.out))
    else:
        sys.stdout.write(text)


if __name__ == '__main__':
    main()
