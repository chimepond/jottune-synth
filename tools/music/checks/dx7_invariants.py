#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""The invariants of dx7_import.py's translation, over every DX7 algorithm.

    python3 -B tools/music/checks/dx7_invariants.py

For each of the 32 DX7 algorithms it makes 301 voices: the first with
every operator at output level 99, the other 300 with each operator's
level drawn from 0, 1, 40, 70, 85 and 99 and the feedback, the LFO's
pitch depth, sensitivity and amplitude depth, the key sync, and each
operator's key scaling, rate scaling, fixed mode, coarse, fine, detune
and amplitude sensitivity varied, from
a generator seeded 20261012 plus the algorithm's index. It translates
each voice as the current dx7_import.py does at --ops 2, 3 and 4, then
checks each block against rules worked out here from algorithms.txt's
header and README.md's DX7 import section, not from the importer's code:

- the `# pair:` carrier and modulator lines are the same at --ops 2, 3
  and 4, and are the pair the 2-operator rule picks: the loudest carrier that sounds
  (of equals, one a sounding operator modulates, then the lower number)
  and the loudest sounding operator with an edge to it (the lower number
  of equals), or `ratio=1.0 index=0.0` when none; the modulator line
  carries feedback exactly when the voice has some and its operator is
  the feedback operator, the carrier line never; the carrier's level
  counts the carriers that sound, at most 4, in its 20 log10 term;
- the routing note names that pair, the kept operators and every
  dropped one with its reason, and says "from before the reduction"
  exactly when a pair operator lies outside the kept ones;
- the reduce rule's four steps: the kept and dropped operators split the
  six, at most --ops kept; every silent operator drops (when every
  carrier is silent, the lowest-numbered carrier stays); the kept
  carriers are the loudest that sound, the lower number of equals, at
  most --ops; modulators are kept by rising depth (edges to the nearest
  kept carrier), the louder then the lower number first, while room is
  left; one with no path drops; each kept modulator reaches a kept
  carrier along kept edges;
- the block (the grammar, ): for a single kept operator no route
  line, its carrier line and a silent modulator; otherwise a route line
  and the carrier, modulator, op 2 and op 3 lines, one per kept operator
  in the order the routing comment gives, numbered for the engine by taking the
  lowest-numbered operator whose kept targets are all numbered, OPS the
  count, CARRIERS the mask of the kept carriers' engine numbers, the kept
  edges exactly, each F>T with F above T at weight 256; carriers carry
  level and no index, modulators index and no level, and only the kept
  feedback operator carries fb;
- with every operator at 99, a silent modulator for dx7-32 only;
- the LFO's raw LFO fields when its rounded pitch depth is nonzero: rate,
  bounded delay/fade from an independent cycle-jump model, wave, phase,
  retained-operator pitch mask and sync-off FREE 2; no amplitude/index
  depth. AMD and shown AMS stay pending. The shown
  operators (the kept ones and the pair's) alone named in the notes and
  pending lines: the rate scaling and amplitude sensitivity lists are the
  shown operators with a nonzero value; one note line, the voice's, short
  enough for the patch line compose.py writes from it (1022 bytes);
- the operator extras  on every operator line of the block
  and the pair: rs= the voice's rate scaling when not 0; fixed= exactly on
  a fixed-mode operator, 10^(coarse mod 4 + fine/100) Hz to the
  millihertz, its ratio the detune alone; free=2 exactly when the voice's
  key sync is off; a silent modulator none of them;
- compose.py's loader (load_recipes) takes every block at --ops 2, 3 and
  4, each algorithm's blocks at one --ops in one file under
  out/checks, deleted after.

It backs README.md's DX7 import: "--ops never changes them" (What a block
holds and The reduce rule, README's backticks dropped here), the pair rule
and the silent modulator under What a block holds, the four steps under
The reduce rule, "every edge runs from a higher engine operator to a
lower one, in the same sample, at weight 256; CARRIERS is their mask", "a
silent modulator for algorithm 32 only" (The self-test, there for 32
voices at 99 with feedback 7; here for every voice at 99), and the cents
in the mapping table, "depth 99 at sensitivity 7 is 1190.6 cents ...; 5
at 2 is 4.4 cents", checked as well. It replaces the sweep and variant
check, kept in the lane's session scratch, which compared the importer
before and after its fix round; this one needs only the importer as it
stands. Standard library only, no harness: 9632 voices, 28896
translations, about 6 s on one Mac.

It prints each check's count of passes and failures, up to 10 failures
in all with their algorithm, voice and --ops, and exits 0 when none fails,
1 when one does and 2 on a usage error.
"""
import contextlib
import functools
import math
import os
import random
import re
import sys

sys.dont_write_bytecode = True
CHECKS = os.path.dirname(os.path.abspath(__file__))
MUSIC = os.path.dirname(CHECKS)
SPIKE = os.path.dirname(os.path.dirname(MUSIC))
OUT = os.path.join(SPIKE, 'out', 'checks')           # git ignores out
if MUSIC not in sys.path:
    sys.path.insert(0, MUSIC)
import dx7_import as dx                                # noqa: E402
import compose                                         # noqa: E402

SEED = 20261012
PATTERNS = 301                                        # voices an algorithm
LEVELS = (0, 1, 40, 70, 85, 99)
OPS = (2, 3, 4)
PMS = (0, 10, 20, 33, 55, 92, 153, 255)               # README's table, written out again here
PAIR_C, PAIR_M = '# pair: carrier ', '# pair: modulator '
SILENT = 'ratio=1.0 index=0.0'                        # a silent modulator's fields
ROUTING = '# routing: '
KINDS = ('carrier', 'modulator', 'op 2', 'op 3')      # the block's operator lines, engine operators 0 to 3


class Tally:
    def __init__(self):
        self.counts, self.fails, self.examples = {}, 0, []

    def check(self, label, ok, where, detail=''):
        c = self.counts.setdefault(label, [0, 0])
        c[0 if ok else 1] += 1
        if not ok:
            self.fails += 1
            if len(self.examples) < 10:
                self.examples.append('%s: %s%s' % (where, label, (': ' + detail) if detail else ''))


def voices(n):
    """dx7-(n+1)'s voices: the first with every operator at 99, the rest drawn."""
    rng = random.Random(SEED + n)
    for j in range(PATTERNS):
        p = [99] * 6 if j == 0 else [rng.choice(LEVELS) for _ in range(6)]
        v = dx.blank_voice('SWEEP')
        dx.set_voice(v, alg=n, fb=rng.randrange(8), pmd=rng.randrange(100), pms=rng.randrange(8),
                     amd=rng.choice((0, 0, 40)), oks=rng.randrange(2), speed=j % 100,
                     delay=j % 83, wave=j % 6, sync=j % 2)
        for o in range(1, 7):
            dx.set_op(v, o, ol=p[o - 1], rd=rng.choice((0, 0, 30)), ld=rng.choice((0, 0, 20)), rc=rng.randrange(4),
                      rs=rng.choice((0, 0, 1, 3, 7)), mode=rng.choice((0, 0, 0, 1)), ams=rng.choice((0, 0, 2)),
                      coarse=rng.randrange(32), fine=rng.choice((0, 0, 41, 99)), det=rng.randrange(15))
        yield j, dx.decode(v)


@functools.lru_cache(maxsize=100)
def expected_times(delay):
    """Jump complete recurrence stages independently of the importer's tick loop.
    Source and endpoint rules: README's  trace, uint32 wrapping."""
    if delay == 0:
        return 0, 0
    a = 99 - delay
    a = (16 + a % 16) * 2 ** (1 + a // 16)
    inc1, inc2 = 33587 * a, 33587 * max(128, a // 128 * 128)
    state = ticks = 0
    first = None
    while True:
        steps = ((1 << 31) - state + inc1 - 1) // inc1
        state += steps * inc1
        ticks += steps
        if first is None:
            first = ticks
        steps = ((1 << 32) - state + inc2 - 1) // inc2
        state += steps * inc2 - (1 << 32)
        ticks += steps
        delay_ms, fade_ms = (first * 4 + 1) // 3, ((ticks - first) * 4 + 1) // 3
        if delay_ms > 60000 or fade_ms > 60000:
            return None
        if state < inc1:
            return delay_ms, fade_ms


def expected_lfo(voice, ops):
    depth = (1200 * (voice['pmd'] * 165 // 64) * PMS[voice['pms']] + 32768) // 65536
    if depth == 0:
        return None
    sr = max(1, 165 * voice['speed'] // 64)
    sr *= 11 + max(0, (sr - 160) // 16)
    den = 64 * 2 ** 32
    rate = (33587 * sr * 48000000 + den // 2) // den
    delay, fade = expected_times(voice['delay'])
    shape, phase = ((1, 16384), (3, 32768), (2, 32768), (4, 32768), (0, 32768), (5, 0))[voice['wave']]
    return [shape, rate, delay, fade, depth, 0, 0, phase, 2 ** ops - 1, 0, 0] + ([2] if not voice['sync'] else [])


def line_ops(t_lines, pair):
    """{line: DX7 operator} for the block's operator lines and the pair lines, from the
    routing comment's "the block's lines are ..." and "the pair lines are ..."; a silent
    modulator's line maps to None. None when the comment does not say."""
    text = ([x for x in t_lines if x.startswith(ROUTING)] or [''])[0]
    many = re.search(r"; the block's lines are (OP\d(?:, OP\d)*), engine operators 0 to (\d)", text)
    lone = re.search(r"; the block's lines are OP(\d) and a silent modulator", text)
    order = ops_named(many.group(1)) if many else [int(lone.group(1)), None] if lone else None
    body = [x for x in t_lines if x.split(' ', 1)[0] in ('carrier', 'modulator', 'op')]
    pl = [x for x in t_lines if x.startswith((PAIR_C, PAIR_M))]
    if order is None or len(body) != len(order) or len(pl) != 2:
        return None
    return list(zip(body, order)) + list(zip(pl, pair))


def field(line, key):
    got = re.search(r' %s=(\S+)' % key, line)
    return got.group(1) if got else None


def expected_pair(alg, ol):
    """The 2-operator rule, as algorithms.txt's header words it."""
    sounding = [c for c in alg['carriers'] if ol[c] > 0] or [min(alg['carriers'])]
    top = max(ol[c] for c in sounding)
    equal = [c for c in sounding if ol[c] == top]
    modulated = [c for c in equal if any(t == c and ol[f] > 0 for f, t in alg['edges'])]
    car = min(modulated or equal)
    mods = [f for f, t in alg['edges'] if t == car and ol[f] > 0]
    if not mods:
        return car, None, sounding
    loud = max(ol[f] for f in mods)
    return car, min(f for f in mods if ol[f] == loud), sounding


def expected_reduce(alg, ol, limit):
    """The reduce rule's steps, as algorithms.txt's header words them: (step-1 drops,
    kept carriers, carriers past the limit, {modulator: depth}, kept modulators)."""
    every = range(1, alg['ops'] + 1)
    all_silent = all(ol[c] == 0 for c in alg['carriers'])
    step1 = {o for o in every if ol[o] == 0} - ({min(alg['carriers'])} if all_silent else set())
    cars = sorted((c for c in alg['carriers'] if c not in step1), key=lambda c: (-ol[c], c))
    kept_c, past = cars[:limit], cars[limit:]
    left = set(every) - step1 - set(past)
    depth, changed = {c: 0 for c in kept_c}, True      # shortest path by relaxation, not a search
    while changed:
        changed = False
        for f, t in alg['edges']:
            if f in left and t in depth and depth[t] + 1 < depth.get(f, len(every) + 1):
                depth[f] = depth[t] + 1
                changed = True
    mods = sorted((o for o in left if o not in alg['carriers'] and o in depth), key=lambda o: (depth[o], -ol[o], o))
    room = limit - len(kept_c)
    return step1, kept_c, past, depth, mods[:room], mods[room:], all_silent


def ops_named(text):
    return [int(x) for x in re.findall(r'OP(\d)', text)]


def check_voice(t, algs, n, j, voice):
    alg = algs['dx7-%d' % (n + 1)]
    vo = voice['ops']
    ol = {o: vo[o]['ol'] for o in vo}
    every = set(range(1, alg['ops'] + 1))
    res = {k: dx.translate(1, voice, algs, 's', 'x', k, True) for k in OPS}
    lines = {k: res[k]['text'].splitlines() for k in OPS}
    car_line = {k: [x for x in lines[k] if x.startswith(PAIR_C)] for k in OPS}
    mod_line = {k: [x for x in lines[k] if x.startswith(PAIR_M)] for k in OPS}
    where = 'dx7-%d voice %d' % (n + 1, j)
    c, m, sounding = expected_pair(alg, ol)

    # the 2-operator lines: one form whatever --ops says, by the rule
    t.check('pair lines the same at --ops 2, 3 and 4',
            car_line[2] == car_line[3] == car_line[4] and mod_line[2] == mod_line[3] == mod_line[4]
            and len(car_line[2]) == len(mod_line[2]) == 1 and res[2]['pair'] == res[3]['pair'] == res[4]['pair'],
            where)
    t.check('pair by the 2-operator rule', res[2]['pair'] == (c, m), where,
            'got %s, the rule gives %s' % (res[2]['pair'], (c, m)))
    cl, ml = car_line[2][0] if car_line[2] else '', mod_line[2][0] if mod_line[2] else ''
    t.check('silent modulator line exactly when no sounding operator modulates the carrier',
            (ml == PAIR_M + SILENT) == (m is None), where, ml)
    t.check('modulator line carries fb exactly when the voice has feedback on its operator',
            ('fb=' in ml) == (voice['fb'] > 0 and m == alg['feedback']), where, ml)
    t.check('carrier line carries no fb', 'fb=' not in cl, where, cl)
    level = min(dx.MAX_DB, float(dx.operator(vo[c], True, 1, 0)['args']['level']) + 20 * math.log10(min(len(sounding), 4)))
    got = re.search(r' level=(\S+)', cl)
    t.check("carrier level counts the sounding carriers, at most 4",
            bool(got) and got.group(1) == dx.value_text('level', level), where,
            '%s, the rule gives level=%s' % (cl, dx.value_text('level', level)))
    if j == 0:
        t.check('every operator at 99: a silent modulator for dx7-32 only', (m is None) == (n + 1 == 32), where,
                'pair %s' % ((c, m),))

    for k in OPS:
        r, w = res[k], '%s --ops %d' % (where, k)
        kept, dropped = r['kept'], r['dropped']
        step1, kept_c, past, depth, kept_m, deep, all_silent = expected_reduce(alg, ol, k)
        t.check('kept and dropped split the operators, at most --ops kept',
                set(kept) | set(dropped) == every and not set(kept) & set(dropped) and len(kept) <= k
                and kept == sorted(kept), w, 'kept %s dropped %s' % (kept, sorted(dropped)))
        t.check('step 1: the silent operators drop (all carriers silent: the lowest-numbered stays)',
                {o for o, why in dropped.items() if why == 'output level 0'} == step1, w,
                'dropped %s' % dropped)
        k_cars = [o for o in kept if o in alg['carriers']]
        t.check('step 2: the kept carriers are the loudest sounding ones, at most --ops',
                k_cars == sorted(kept_c) and all(dropped.get(o) == 'a carrier past %d' % k for o in past), w,
                'kept carriers %s, the rule gives %s' % (k_cars, sorted(kept_c)))
        t.check('kept carriers sound (all carriers silent: the lowest-numbered alone)',
                k_cars == [min(alg['carriers'])] if all_silent else all(ol[o] > 0 for o in k_cars), w,
                'kept carriers %s' % k_cars)
        k_mods = [o for o in kept if o not in alg['carriers']]
        t.check('step 3: modulators kept by rising depth, louder, lower number, while room is left',
                k_mods == sorted(kept_m) and all(dropped.get(o) == 'depth %d' % depth[o] for o in deep), w,
                'kept modulators %s, the rule gives %s' % (k_mods, sorted(kept_m)))
        nopath = every - step1 - set(alg['carriers']) - set(depth)
        t.check('step 4: an operator with no path to a kept carrier drops',
                all(dropped.get(o) == 'no path to a kept carrier' for o in nopath)
                and {o for o, why in dropped.items() if why == 'no path to a kept carrier'} == nopath, w,
                'dropped %s' % dropped)
        k_edges = [(f, to) for f, to in alg['edges'] if f in kept and to in kept]
        reach = set(k_cars)
        for _ in kept:
            reach |= {f for f, to in k_edges if to in reach}
        t.check('each kept modulator reaches a kept carrier along kept edges', set(kept) <= reach, w)

        # the routing note
        note = [x for x in lines[k] if x.startswith(ROUTING)]
        text = note[0] if len(note) == 1 else ''
        pk = re.search(r'; kept (OP\d(?:, OP\d)*)', text)
        pd = re.search(r'; dropped (.*?) by the reduce rule', text)
        named_d = dict((int(o), why) for o, why in re.findall(r'OP(\d) \(([^)]*)\)', pd.group(1))) if pd else {}
        t.check('routing note lists the kept and dropped operators exactly',
                bool(pk) and ops_named(pk.group(1)) == kept and named_d == dropped and bool(pd) == bool(dropped), w,
                text)
        pp = re.search(r'the pair lines are OP(\d) and (?:OP(\d)|a silent modulator, OP(\d) having none)', text)
        t.check('routing note names the pair',
                bool(pp) and (int(pp.group(1)), int(pp.group(2)) if pp.group(2) else None) == (c, m)
                and (pp.group(3) is None or int(pp.group(3)) == c), w, text)
        off = [o for o in (c, m) if o and o not in kept]
        fb_ = re.search(r' \((OP\d(?:, OP\d)*) from before the reduction\)', text)
        t.check('"from before the reduction" exactly when a pair operator lies outside the kept ones',
                (ops_named(fb_.group(1)) if fb_ else []) == off and text.count('from before the reduction') == bool(off),
                w, text)

        # what the notes and pending lines name
        shown = set(kept) | {o for o in (c, m) if o}
        named = set()
        for x in lines[k]:
            if x.startswith(('note ', '# ')) and not x.startswith(('# DX7 voice ', ROUTING, '# pair: ')) \
                    and 'clamped on reading' not in x:
                named |= set(ops_named(x))
        t.check('notes, comments and pending lines name only shown operators (the kept and the pair)', named <= shown,
                w, 'named %s, shown %s' % (sorted(named), sorted(shown)))
        t.check('one note line, the voice\'s',
                [x[:20] for x in lines[k] if x.startswith('note ')] == ['note      DX7 voice '], w)
        rs = [x for x in lines[k] if x.startswith('# rate scaling: ')]
        want = [(o, vo[o]['rs']) for o in sorted(shown) if vo[o]['rs']]
        got = [(int(o), int(v)) for o, v in re.findall(r'OP(\d) (\d)', rs[0].split(' as rs', 1)[0])] if rs else []
        t.check('rate scaling comment lists the shown operators with RS', got == want and len(rs) == bool(want), w,
                '%s, want %s' % (rs, want))
        each = line_ops(lines[k], (c, m))
        t.check("the routing comment maps the block's and the pair's lines to operators", each is not None, w)
        for x, o in each or ():
            p = vo[o] if o else None
            t.check('rs= is the voice\'s rate scaling on each operator line, none at 0 or on a silent modulator',
                    field(x, 'rs') == (str(p['rs']) if p and p['rs'] else None), w, x)
            hz = round(10 ** ((p['coarse'] & 3) + p['fine'] / 100) * 1000) / 1000 if p and p['mode'] else None
            t.check('fixed= exactly on a fixed-mode operator, 10^(coarse mod 4 + fine/100) Hz to the millihertz',
                    field(x, 'fixed') == (dx.value_text('fixed', hz) if hz else None), w, x)
            if hz:
                det = 2 ** ((p['det'] - 7) * dx.DETUNE_CENTS / 1200)
                t.check("a fixed-mode operator's ratio is its detune alone", field(x, 'ratio') == dx.value_text(
                    'ratio', det), w, '%s, detune %d' % (x, p['det']))
            t.check('free=2 on each operator line exactly when key sync is off, none on a silent modulator',
                    field(x, 'free') == ('2' if p and not voice['oks'] else None), w, x)
        lfo = [x for x in lines[k] if x.startswith('lfo ')]
        expected = expected_lfo(voice, len(kept))
        t.check('pitch LFO fields match source model, no synthetic AMD',
                [list(map(int, x.split()[1:])) for x in lfo] == ([expected] if expected else []), w,
                '%s; expected %s' % (lfo, expected))
        ams = [(o, vo[o]['ams']) for o in sorted(shown) if vo[o]['ams']]
        pending = [x for x in lines[k] if x.startswith('# pending: amplitude modulation')]
        t.check('amplitude pending exactly for AMD or shown AMS', len(pending) == bool(voice['amd'] or ams), w)
        comments = [x for x in lines[k] if x.startswith('# lfo:')]
        t.check('LFO source comment exactly for PMD or PMS', len(comments) == bool(voice['pmd'] or voice['pms']), w)
        if comments:
            cents = re.search(r'a peak deviation of (\S+) cents', comments[0])
            want_c = '%.1f' % ((voice['pmd'] * 165 >> 6) * PMS[voice['pms']] * 1200 / 65536)
            t.check('lfo cents (depth * 165 >> 6) * table[sensitivity] * 1200 / 65536',
                    bool(cents) and cents.group(1) == want_c, w,
                    '%s, want %s' % (cents.group(1) if cents else None, want_c))
        if pending:
            sens = re.search(r'amplitude depth (\d+) at sensitivity (.*?);', pending[0])
            got = [(int(o), int(v)) for o, v in re.findall(r'OP(\d) (\d)', sens.group(2))] if sens else None
            t.check('lfo amplitude sensitivities list the shown operators with AMS',
                    bool(sens) and int(sens.group(1)) == voice['amd'] and got == ams
                    and (sens.group(2) == '0') == (not ams), w, pending[0])
        check_route(t, w, alg, voice, kept, k_cars, k_edges, r['route'], lines[k])
    return res


def check_route(t, w, alg, voice, kept, k_cars, k_edges, route, lines):
    """The route line and the operator lines of one block (the grammar, )."""
    body = [x for x in lines if x.split(' ', 1)[0] in ('carrier', 'modulator', 'op')]
    rline = [x for x in lines if x.startswith('route ')]
    text = ([x for x in lines if x.startswith(ROUTING)] or [''])[0]
    many = re.search(r"; the block's lines are (OP\d(?:, OP\d)*), engine operators 0 to (\d)", text)
    if len(kept) == 1:
        t.check('a single kept operator: no route line, its carrier line and a silent modulator',
                route is None and not rline and len(body) == 2 and body[0].startswith('carrier ')
                and body[1] == 'modulator ' + SILENT and 'level=' in body[0], w, ' | '.join(body))
        return
    order = ops_named(many.group(1)) if many else []
    ok = route is not None and rline == ['route     ' + ' '.join(route)] and sorted(order) == kept \
        and int(many.group(2)) == len(kept) - 1 and [re.match(r'(carrier|modulator|op \d) ', x).group(1)
                                                       if re.match(r'(carrier|modulator|op \d) ', x) else None
                                                       for x in body] == list(KINDS[:len(kept)])
    t.check('a route line, then one line per kept operator: carrier, modulator, op 2, op 3', ok, w,
            ' | '.join([text[-80:]] + rline + [x[:12] for x in body]))
    if not ok:
        return
    routed = [(o, ' ' + x.split(' ', 1)[1].strip()) for o, x in zip(order, body)]
    eng = {o: i for i, o in enumerate(order)}
    lowest = True
    for i, o in enumerate(order):
        ready = [x for x in kept if x not in order[:i] and all(to in order[:i] for f, to in k_edges if f == x)]
        lowest = lowest and bool(ready) and o == min(ready)
    t.check('engine numbers: the lowest-numbered operator whose kept targets are numbered', lowest, w,
            'order %s' % order)
    words = [re.fullmatch(r'(\d+)>(\d+):256', x) for x in route[2:]]
    pairs = [(int(x.group(1)), int(x.group(2))) for x in words if x]
    t.check('route: OPS the count, CARRIERS the mask, the kept edges each F>T, F above T, weight 256',
            route[0] == str(len(kept)) and route[1] == str(sum(1 << eng[o] for o in k_cars))
            and all(words) and pairs == sorted(pairs) and all(f > to for f, to in pairs)
            and pairs == sorted((eng[f], eng[to]) for f, to in k_edges), w, ' '.join(route))
    fb_op = alg['feedback'] if voice['fb'] and alg['feedback'] in kept else None
    for o, args in routed:
        is_c = o in alg['carriers']
        t.check('block carriers carry level and no index, modulators index and no level',
                (' level=' in args) == is_c and (' index=' in args) == (not is_c), w, args)
        t.check('block fb only on the kept feedback operator', (' fb=' in args) == (o == fb_op), w, args)


def load_blocks(t, n, blocks):
    """compose.py's loader on one algorithm's blocks at --ops 2, 3 and 4, a file each, renamed apart."""
    os.makedirs(OUT, exist_ok=True)
    count = 0
    for k in OPS:
        path = os.path.join(OUT, 'dx7_invariants-dx7-%d-ops%d.txt' % (n + 1, k))
        names, text = [], []
        for j, res in blocks:
            r = res[k]
            names.append('x-dx7-%d-%d' % (n + 1, j))
            text.append(r['text'].replace('recipe %s\n' % r['name'], 'recipe %s\n' % names[-1], 1))
        with open(path, 'w') as fh:
            fh.write('\n\n'.join(text) + '\n')
        try:
            try:
                loaded = compose.load_recipes(path)
                detail = 'loaded %d of %d' % (len(loaded), len(names))
            except SystemExit as e:
                loaded, detail = {}, str(e.code)
        finally:
            os.remove(path)
        t.check('compose.load_recipes loads every block at --ops 2, 3 and 4', list(loaded) == names,
                'dx7-%d --ops %d' % (n + 1, k), detail)
        saved = compose.RECIPES
        try:
            for alias, (_, result) in zip(names, blocks):
                if alias not in loaded:
                    continue
                name = result[k]['name']
                compose.RECIPES = {name: loaded[alias]}
                rows, _ = compose.patch_lines(name, *compose.recipe(name))
                sizes = [len(row.encode('utf-8')) for row in rows]
                t.check('actual patch lines fit the reader\'s 1022-byte limit',
                        bool(sizes) and max(sizes) <= 1022,
                        '%s --ops %d' % (name, k), 'bytes %s' % sizes)
        finally:
            compose.RECIPES = saved
        count += len(loaded)
    return count


def main(argv):
    if argv:
        sys.stderr.write('dx7_invariants.py: it takes no arguments\n\n' + __doc__)
        return 2
    algs = dx.load_algorithms()
    t = Tally()
    silent = beyond = loaded = 0
    for delay in range(100):
        want = expected_times(delay)
        try:
            got = dx.lfo_times(delay)
        except ValueError:
            got = None
        t.check('all delay settings match independent recurrence or bounds refusal', got == want,
                'delay %d' % delay, '%s; expected %s' % (got, want))
    for depth, sens, readme in ((99, 7, '1190.6'), (5, 2, '4.4')):
        mine, theirs = '%.1f' % ((depth * 165 >> 6) * PMS[sens] * 1200 / 65536), '%.1f' % dx.lfo_cents(depth, sens)
        t.check("README's lfo cents, 1190.6 at depth 99 and sensitivity 7 and 4.4 at 5 and 2, here and by lfo_cents",
                mine == theirs == readme, 'depth %d sensitivity %d' % (depth, sens), '%s and %s' % (mine, theirs))
    try:
        for n in range(32):
            blocks = []
            for j, voice in voices(n):
                r = check_voice(t, algs, n, j, voice)
                silent += r[2]['pair'][1] is None
                beyond += any(o and o not in r[2]['kept'] for o in r[2]['pair'])
                blocks.append((j, r))
            loaded += load_blocks(t, n, blocks)
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(OUT)                              # only when empty
    print('dx7_invariants.py: 32 algorithms x %d voices x --ops 2, 3 and 4: %d translations' % (
        PATTERNS, 32 * PATTERNS * 3))
    for label, (good, bad) in t.counts.items():
        print('%s %6d of %6d  %s' % ('ok  ' if not bad else 'FAIL', good, good + bad, label))
    print('voices with a silent modulator: %d of %d; whose pair lies partly outside the block at --ops 2: %d'
          % (silent, 32 * PATTERNS, beyond))
    print('compose.load_recipes: %d of %d blocks at --ops 2, 3 and 4 loaded' % (loaded, 32 * PATTERNS * 3))
    for e in t.examples:
        print('  ' + e)
    print('%s: %d failures' % ('FAIL' if t.fails else 'PASS', t.fails))
    return 1 if t.fails else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
