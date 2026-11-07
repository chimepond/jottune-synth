# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Parity check: the bridge's MIDI translator (fms_midi.c)
against midi2tl.py, event by event, for the same messages.

usage: python3 -B -I midi_check.py MUSIC_DIR WORK_DIR FMS_VERIFY [MUTANT...]

MUSIC_DIR holds midi2tl.py and compose.py (the app's
export). One fixture of messages: all 128 keys at velocities 1, 64 and 127;
ten keys held over eight voices (steals); pitch bends at raw 0, 8192, 16383
and a sweep under the default range and under two ranges RPN 0 sets, with
the RPN edge cases; the sustain pedal holding, restriking and letting go;
CC 74 in steps; programs, aftertouch, pressure and other controllers; a
second channel; and, sent to the translator only (a MIDI file cannot hold
them), MIDI clock, transport and a SysEx. midi2tl.py converts the messages
as a MIDI file (500 ticks a quarter at 500000 us: a tick is 1 ms) under two
configs: A, --map 1=bell --filter 1=lp:1000:0.707 --rpn; B, --map 1=bell.
fms_verify --midi feeds them to the translator as UMP. At each ns both sides'
events, sorted, must be equal (kind, voice, mHz, value), and so must their
counts. The messages are 2 ms or more apart, every note lasts 2 ms or more
and ends before the file does, so midi2tl.py's merges, lengthening and
unreleased ends never act; the check asserts that.

Each MUTANT (fms_verify built with FMS_MIDI_MUTANT=1, a wrong velocity map,
or =2, the first note-off dropped, or =3, the default wheel mapped to ctl7, or =4, the wrong member bend range) must differ from midi2tl.py.
Configs C and D cover  default and explicit sources on an all-input patch,
with independent hand values and ctl-before-on/off checks; E/F cover one MPE
zone at48000/44100 with the six documented exclusions in mpe_fixture. No audio is rendered.
Exit 0 when every comparison holds and every mutant is caught.
"""
import collections
import os
import subprocess
import sys

ON, OFF, CC, PB = 0x90, 0x80, 0xB0, 0xE0


def fixture():
    """(t ms, message bytes, in the MIDI file too) in time order, 2 ms or more apart."""
    msgs, clock = [], [10]

    def at(b, smf=True, gap=2):
        msgs.append((clock[0], list(b), smf))
        clock[0] += gap

    def bends(values):
        for v in values:
            at([PB, v & 0x7F, v >> 7])

    sweep = [0, 8192, 16383] + list(range(0, 16384, 1024)) + [16383, 12000, 8192]
    # 1. every key at velocities 1, 64 and 127, one at a time; every third
    #    note-off a note-on at velocity 0
    for vel in (1, 64, 127):
        for key in range(128):
            at([ON, key, vel])
            at([ON, key, 0] if key % 3 == 0 else [OFF, key, 64])
    clock[0] += 3100                                   # past the bell's 3000 ms release: free voices
    # 2. ten keys held over eight voices, released out of order
    keys = (48, 50, 52, 53, 55, 57, 59, 60, 62, 64)
    for k, key in enumerate(keys):
        at([ON, key, 40 + 8 * k])
    for key in (50, 64, 48, 60, 52, 62, 53, 59, 55, 57):
        at([OFF, key, 0])
    # 3. bends over a held chord: the default range, then RPN 0 at 12
    #    semitones, then at 24 semitones 50 cents
    for key in (60, 64, 67):
        at([ON, key, 96])
    bends(sweep)
    at([CC, 101, 0]); at([CC, 100, 0]); at([CC, 6, 12])
    bends(sweep)
    at([CC, 6, 24]); at([CC, 38, 50])
    bends(sweep)
    bends([3000]); at([ON, 72, 90]); at([OFF, 72, 0])  # a note struck while bent
    at([CC, 38, 120])                                  # cents above 99 are 99
    at([CC, 99, 1]); at([CC, 98, 2]); at([CC, 6, 5])   # an NRPN: its data entry is ignored
    at([CC, 101, 0]); at([CC, 6, 3])                   # RPN (0, 127): not RPN 0
    at([CC, 100, 0]); at([CC, 6, 2])                   # RPN 0 again: 2 semitones
    bends([8192, 16383, 8192])
    for key in (64, 60, 67):
        at([OFF, key, 0])
    # 4. the pedal: notes held past their note-offs, a restrike, a second
    #    down, an orphan note-off, more held notes than voices, then up
    at([CC, 64, 127])
    at([ON, 60, 80]); at([OFF, 60, 0]); at([ON, 62, 80]); at([OFF, 62, 0])
    at([ON, 60, 100])                                  # 60 again under the pedal
    at([CC, 64, 100]); at([OFF, 60, 0]); at([OFF, 61, 0])
    for key in range(70, 80):
        at([ON, key, 70]); at([OFF, key, 0])
    at([CC, 64, 63])                                   # up: every kept note ends
    at([CC, 64, 64]); at([ON, 65, 50]); at([OFF, 65, 0]); at([CC, 64, 0])
    # 5. CC 74 in steps over a held chord, then a note struck after a step
    at([ON, 48, 90]); at([ON, 55, 90])
    for v in (0, 32, 64, 96, 127, 127, 64, 100):
        at([CC, 74, v])
    at([ON, 67, 90]); at([CC, 74, 64])
    for key in (48, 55, 67):
        at([OFF, key, 0])
    # 6. the rest: counted and ignored
    at([0xC0, 5]); at([0xA0, 60, 30]); at([0xD0, 40]); at([CC, 1, 64]); at([CC, 7, 100])
    at([0x91, 60, 100]); at([0xE1, 0, 0x50]); at([0x81, 60, 0])
    at([0xF8], smf=False); at([0xFA], smf=False); at([0xFC], smf=False)
    at([0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7], smf=False)
    # 7. the widest range RPN 0 sets (127 semitones 99 cents): key 127 bent
    #    to the top lies above 32 bits of mHz (clamped)
    at([CC, 101, 0]); at([CC, 100, 0]); at([CC, 6, 127]); at([CC, 38, 99])
    at([ON, 127, 64]); bends([16383, 0, 8192]); at([OFF, 127, 0])
    return msgs, clock[0] + 10


def smf_of(m2t, msgs, end):
    track = [(0, m2t.tempo(500000))] + [(t, b) for t, b, smf in msgs if smf] + [(end, m2t.EOT)]
    return m2t.smf_bytes(0, 500, [track])


def reference(m2t, msgs, end, argv, channel=0):
    conv = m2t.Conversion(smf_of(m2t, msgs, end), m2t.cfg_of(*argv))
    ev = collections.defaultdict(list)
    for t, kind, _, k, value, _ in conv.out[channel]:
        if kind == 0:
            ev[t].append(('off', k, 0, 0))
        elif kind == 1:
            ev[t].append(('on', k, value[0], value[1]))
        elif kind == 2:
            ev[t].append(('pitch', k, value, 0))
        elif kind == 4:
            ev[t].append(('ctl', k, value[0], value[1]))
        else:
            ev[t].append(('filter', k, value[0], value[1]))
    pools = list(conv.pools[channel].values())
    return conv, ev, pools[0].rel_ms


def translate(binary, path):
    run = subprocess.run([binary, '--midi', path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True)
    if run.returncode:
        raise RuntimeError('%s --midi: rc=%d %s' % (binary, run.returncode, run.stderr.strip()))
    ev, stats = collections.defaultdict(list), {}
    for line in run.stdout.splitlines():
        w = line.split()
        if w[0] == 'stats':
            stats = dict((k, int(v)) for k, v in (x.split('=') for x in w[1:]))
        else:
            ev[int(w[0])].append((w[1], int(w[2]), int(w[3]), int(w[4])))
    return ev, stats


def first_difference(a, b):
    for t in sorted(set(a) | set(b)):
        x, y = sorted(a.get(t, [])), sorted(b.get(t, []))
        if x != y:
            return t, x, y
    return None


def ctl_fixture():
    """ source values, >=2 ms apart except different-source same-time pressure.
    No MPE, merges, lengthening, EOF ends or multi-channel pool differences.
    """
    values = [
        [CC, 1, 64], [CC, 2, 50], [CC, 11, 100], [ON, 60, 100],
        [CC, 33, 100], [CC, 1, 11], [ON, 64, 90], [0xD0, 40],
        [0xA0, 60, 30], [0xA0, 64, 20], [0xD0, 60], [ON, 67, 100],
        [CC, 99, 1], [CC, 98, 2], [CC, 38, 5], [CC, 6, 3], [CC, 38, 5],
        [CC, 98, 3], [CC, 6, 9], [CC, 38, 100], [CC, 98, 2], [CC, 38, 7],
        [CC, 101, 0], [CC, 6, 12], [CC, 100, 0], [CC, 6, 2], [CC, 38, 50],
        [CC, 99, 127], [CC, 98, 127], [CC, 6, 7], [CC, 96, 1], [CC, 97, 1],
        [CC, 74, 127], [OFF, 60, 100], [CC, 1, 67], [OFF, 64, 127], [ON, 67, 0],
        [CC, 64, 127], [ON, 60, 100], [OFF, 60, 30], [ON, 60, 90], [OFF, 60, 70],
        [ON, 64, 100], [OFF, 64, 90], [CC, 64, 0],
        [ON, 70, 90], [ON, 72, 90], [ON, 74, 90], [ON, 76, 90],
        [OFF, 70, 0], [OFF, 72, 10], [OFF, 74, 20], [OFF, 76, 30],
        [0xD0, 0], [0xA0, 76, 127], [CC, 1, 0], [CC, 11, 0],
        [ON, 60, 1], [OFF, 60, 0]]
    msgs = [(10 + 2 * i, b, True) for i, b in enumerate(values)]
    # Different sources at one time: pressure then poly pressure must keep file order.
    msgs[8] = (msgs[7][0], msgs[8][1], True)
    return msgs, msgs[-1][0] + 20


def ctl_checks(m2t, work, binary, mutants, caught):
    """C default and D every explicit source, plus hand values and event ordering."""
    recipe = 'recipe app-ctl-check\n' + ''.join('mod %d ctl%d index1 100 0\n' % (i, i) for i in range(8))
    recipe += 'carrier level=10 r=10\nmodulator index=1 r=10\nnote live controller parity fixture\n'
    path = os.path.join(work, 'ctl-recipes.txt')
    with open(path, 'w') as f:
        f.write(recipe)
    added = m2t.compose.load_recipes(path)
    m2t.compose.RECIPES.update(added)
    msgs, end = ctl_fixture()
    explicit = 'cc1+:0,nrpn1.2:2,nrpn1.3:2,pressure:1,polyat:1,relvel:5,rand:12345:6,cc74:7,cc11:3,cc2:4'
    bad = 0
    try:
        for name, spec in [('C', 'default'), ('D', explicit)]:
            args = ['--map', '1=app-ctl-check', '--voices', '3', '--rpn', '--ctl', '1=' + spec]
            conv, ref, rel = reference(m2t, msgs, end, args)
            st = conv.st[0]
            path = os.path.join(work, 'midi-%s.txt' % name)
            with open(path, 'w') as f:
                f.write('config 3 %d 200 1 0 0 1 ff\nctl 1 %s\n' % (rel, spec))
                for t, b, _ in msgs:
                    f.write('%d %s\n' % (t * 1000000, ' '.join('%02X' % x for x in b)))
            got, stats = translate(binary, path)
            diff = first_difference(ref, got)
            keys = ('notes', 'orphan_offs', 'restruck', 'pedal_downs', 'pedal_held', 'steals', 'release_steals',
                    'rpn_selects', 'rpn_ranges', 'aftertouch', 'pressure', 'ctl_messages', 'ctl_events')
            counts = [k for k in keys if stats.get(k) != st[k]]
            counts += ['other_ccs'] if stats['other_ccs'] != sum(conv.ccs[0].values()) else []
            unused = [k for k in ('unreleased', 'short', 'skipped', 'moves_dropped') if st[k]]
            unused += ['ctl merged'] if st['ctl_messages'] != st['ctl_kept'] else []
            # The converter appends some offs after allocation; compare the ctl/on
            # subsequence per voice at each timestamp explicitly. Pedal releases
            # of different voices are independent and may arrive in either order.
            orders = []
            for t in ref:
                for voice in range(3):
                    expected = [e for e in ref[t] if e[0] in ('ctl', 'on') and e[1] == voice]
                    actual = [e for e in got.get(t, []) if e[0] in ('ctl', 'on') and e[1] == voice]
                    if expected != actual:
                        orders.append((t, voice, expected, actual))
            order = bool(orders)
            for events in got.values():
                for i, event in enumerate(events):
                    if event[0] == 'off':
                        # A following note-on may reset ctl5 after this off; only
                        # an unretriggered post-off relvel would violate ordering.
                        tail = events[i + 1:]
                        if not any(e[0] == 'on' and e[1] == event[1] for e in tail):
                            order |= any(e[0] == 'ctl' and e[1] == event[1] and e[2] == 5 for e in tail)
            # Independent  values at the first note, including the seed stream.
            first = [e for e in got[16000000] if e[0] == 'ctl']
            hand = ([('ctl', 0, 0, 16513), ('ctl', 0, 2, 12901), ('ctl', 0, 3, 25802), ('ctl', 0, 6, -12020)]
                    if name == 'C' else [('ctl', 0, 0, 16385), ('ctl', 0, 3, 25802), ('ctl', 0, 4, 12901),
                                         ('ctl', 0, 6, 18149)])
            ok = not diff and not counts and not unused and not order and first == hand
            print('MIDICHECK config=%s --ctl 1=%s messages=%d reference=%d translator=%d ctl=%d order=%s hand=%s match=%s'
                  % (name, spec, len(msgs), sum(map(len, ref.values())), sum(map(len, got.values())),
                     stats['ctl_events'], 'yes' if not order else 'NO', 'yes' if first == hand else 'NO',
                     'yes' if ok else 'NO'))
            if not ok:
                print('  difference=%s counts=%s excluded=%s first=%s orders=%s' % (diff, counts, unused, first, orders[:2]))
            bad |= not ok
            for mutant in mutants:
                if mutant in caught:
                    continue
                mgot, _ = translate(mutant, path)
                mdiff = first_difference(ref, mgot)
                if mdiff:
                    caught.add(mutant)
                    print('MIDICHECK mutant %s caught=yes (config=%s first difference at %d ns)'
                          % (os.path.basename(mutant), name, mdiff[0]))
    finally:
        for name in added:
            del m2t.compose.RECIPES[name]
    return bad


def mpe_fixture(upper=False):
    """Port existing supported paths;  exclusions are deliberate:
    no member left on at EOF; no member expression after key-up;
    no member CC64; no master pressure/CC74; no unmapped Scala
    keys; no specification-conformance or complete-conformance claim. The live
    <1ms merge/short-note differences are excluded too. One zone owns pool.
    """
    values = [
        [0xD1, 30], [0xB1, 74, 90], [0xE1, 0, 96], [0xE0, 0, 80],
        [0x91, 60, 100], [0x92, 64, 90], [0xE2, 0, 32], [0xE0, 96, 93],
        [0xD1, 100], [0xB2, 74, 40], [0x91, 67, 80], [0xE1, 24, 117], [0xE0, 88, 54],
        [0xB1, 101, 0], [0xB1, 100, 0], [0xB1, 6, 24],
        [0xB2, 101, 0], [0xB2, 100, 0], [0xB2, 38, 50],
        [0xE1, 16, 78], [0xE2, 112, 46],
        [0xB0, 101, 0], [0xB0, 100, 0], [0xB0, 6, 3], [0xE0, 16, 78],
        [0xB0, 1, 64], [0xD1, 60], [0xB1, 74, 70],
        [0xB1, 100, 6], [0xB1, 6, 2], [0xB1, 38, 5],
        [0x81, 60, 100], [0x82, 64, 100], [0x81, 67, 100],
        [0xB0, 64, 127], [0x91, 60, 80], [0x81, 60, 20],
        [0x91, 60, 90], [0x81, 60, 30], [0xB0, 64, 0],
        [0x98, 50, 100], [0x88, 50, 100]]
    if upper:
        values = [[(b[0] & 0xF0) | (15 - (b[0] & 15) if (b[0] & 15) < 3 else 8)] + b[1:] for b in values]
    msgs = [(10 + 2 * i, b, True) for i, b in enumerate(values)]
    return msgs, msgs[-1][0] + 30


def mpe_checks(m2t, work, binary, mutants, caught):
    path = os.path.join(work, 'mpe-recipes.txt')
    with open(path, 'w') as f:
        f.write('recipe app-mpe-check\nmod 0 ctl0 index1 100 0\nmod 1 ctl1 index1 100 0\n'
                'mod 2 ctl4 index1 100 0\ncarrier level=10 r=10\nmodulator index=1 r=10\n'
                'note live single-zone parity, documented exclusions in fixture docstring\n')
    added = m2t.compose.load_recipes(path); m2t.compose.RECIPES.update(added)
    bad = 0
    try:
        for name, zone, master, fs in [('E', 'lower:2', 0, 48000), ('F', 'upper:2', 15, 44100)]:
            msgs, end = mpe_fixture(master == 15)
            args = ['--map', '%d=app-mpe-check' % (master + 1), '--voices', '4', '--mpe', zone,
                    '--ctl', '%d=cc1:0' % (master + 1), '--fs', str(fs)]
            conv, ref, rel = reference(m2t, msgs, end, args, master)
            path = os.path.join(work, 'midi-%s.txt' % name)
            with open(path, 'w') as f:
                f.write('config 4 %d 200 0 0 0 %x 13\nctl %d cc1:0\nmpe %s\n' %
                        (rel, 1 << master, master + 1, zone))
                for t, b, _ in msgs:
                    f.write('%d %s\n' % (t * 1000000, ' '.join('%02X' % x for x in b)))
            got, stats = translate(binary, path)
            diff = first_difference(ref, got)
            channels = (0, 1, 2) if master == 0 else (15, 14, 13)
            keys = ('notes', 'orphan_offs', 'restruck', 'pedal_downs', 'pedal_held', 'steals', 'release_steals',
                    'rpn_selects', 'rpn_ranges', 'aftertouch', 'pressure', 'ctl_messages', 'ctl_events',
                    'bends', 'pitch_events', 'mpe_notes', 'mpe_pressure', 'mpe_slides', 'mcm')
            want = {k: sum(conv.st[c][k] for c in channels) for k in keys}
            want.update(other_ccs=sum(sum(conv.ccs[c].values()) for c in channels), other_channels=2,
                        messages=len(msgs), note_offs=5, system=0, unknown=0, refused=0)
            counts = [k for k in want if stats.get(k) != want[k]]
            unused = [(c, k) for c in channels for k in ('unreleased', 'short', 'skipped', 'moves_dropped') if conv.st[c][k]]
            unused += [(c, 'bend merged') for c in channels if conv.st[c]['bends'] != conv.st[c]['bends_kept']]
            order = any([e for e in ref[t] if e[0] in ('ctl', 'on') and e[1] == v] !=
                        [e for e in got.get(t, []) if e[0] in ('ctl', 'on') and e[1] == v]
                        for t in ref for v in range(4))
            # --fs changes session metadata only ; no live-rate claim.
            piece = conv.piece('app-mpe-' + name.lower(), os.path.join(work, 'mpe-' + name), 'mpe.mid')
            piece.write()
            rate = piece.fs == fs and ('fs %d' % fs) in (open(os.path.join(work, 'mpe-' + name, 'session.tl')).read().splitlines())
            first = got[18000000]
            hand = first[:2] == [('ctl', 0, 1, 7740), ('ctl', 0, 4, 23221)] and first[2][0] == 'on'
            ok = not diff and not counts and not unused and not order and rate and hand
            print('MIDICHECK config=%s --mpe %s --fs %d messages=%d reference=%d translator=%d order=%s session=%s hand=%s match=%s'
                  % (name, zone, fs, len(msgs), sum(map(len, ref.values())), sum(map(len, got.values())),
                     'yes' if not order else 'NO', 'yes' if rate else 'NO', 'yes' if hand else 'NO', 'yes' if ok else 'NO'))
            if not ok:
                print('  difference=%s counts=%s excluded=%s first=%s' % (diff, [(k, want[k], stats.get(k)) for k in counts], unused, first))
            bad |= not ok
            for mutant in mutants:
                if mutant in caught:
                    continue
                mgot, _ = translate(mutant, path); mdiff = first_difference(ref, mgot)
                if mdiff:
                    caught.add(mutant)
                    print('MIDICHECK mutant %s caught=yes (config=%s first difference at %d ns)'
                          % (os.path.basename(mutant), name, mdiff[0]))
    finally:
        for name in added:
            del m2t.compose.RECIPES[name]
    return bad


def main(argv):
    if len(argv) < 4:
        print(__doc__.strip().splitlines()[2])
        return 2
    music, work, binary, mutants = argv[1], argv[2], argv[3], argv[4:]
    sys.path.insert(0, os.path.abspath(music))
    import midi2tl as m2t
    msgs, end = fixture()
    os.makedirs(work, exist_ok=True)
    configs = [('A', ['--map', '1=bell', '--filter', '1=lp:1000:0.707', '--rpn'], 1000000, 707, 1),
               ('B', ['--map', '1=bell'], 0, 0, 0)]
    nsys = sum(1 for _, b, _ in msgs if b[0] >= 0xF0)
    nch2 = sum(1 for _, b, _ in msgs if 0x80 <= b[0] < 0xF0 and b[0] & 0x0F)
    noffs = sum(1 for _, b, _ in msgs if b[0] == OFF or b[0] == ON and b[2] == 0)
    bad, caught = 0, set()
    for name, args, cut, q, rpn in configs:
        conv, ref, rel = reference(m2t, msgs, end, args)
        st, ccs = conv.st[0], conv.ccs[0]
        path = os.path.join(work, 'midi-%s.txt' % name)
        with open(path, 'w') as f:
            f.write('config 8 %d 200 %d %d %d 1\n' % (rel, rpn, cut, q))
            for t, b, _ in msgs:
                f.write('%d %s\n' % (t * 1000000, ' '.join('%02X' % x for x in b)))
        got, stats = translate(binary, path)
        diff = first_difference(ref, got)
        want = dict((k, st[k]) for k in ('notes', 'orphan_offs', 'restruck', 'pedal_downs', 'pedal_held', 'steals',
                                         'release_steals', 'bends', 'pitch_events', 'cc74', 'filter_events',
                                         'rpn_selects', 'rpn_ranges', 'programs', 'aftertouch', 'pressure'))
        want.update(other_ccs=sum(ccs.values()), clamped=conv.clamped, messages=len(msgs), note_offs=noffs,
                    other_channels=nch2, system=nsys, unknown=0, near_ties=0, refused=0)
        counts = [k for k in sorted(want) if stats.get(k) != want[k]]
        unused = [k for k in ('unreleased', 'short', 'skipped', 'moves_dropped') if st[k]]
        unused += ['%s merged' % k for k in ('bends', 'cc74') if st[k + '_kept'] != st[k]]
        nref = sum(len(v) for v in ref.values())
        ngot = sum(len(v) for v in got.values())
        ok = diff is None and not counts and not unused
        print('MIDICHECK config=%s %s messages=%d reference=%d translator=%d notes=%d steals=%d release_steals=%d '
              'pitch=%d filter=%d clamped=%d match=%s'
              % (name, ' '.join(args), len(msgs), nref, ngot, st['notes'], st['steals'], st['release_steals'],
                 st['pitch_events'], st['filter_events'], conv.clamped, 'yes' if ok else 'NO'))
        if diff:
            print('  first difference at %d ns:\n    midi2tl.py %s\n    translator %s' % diff)
        for k in counts:
            print('  count %s: midi2tl.py %d, translator %s' % (k, want[k], stats.get(k)))
        if unused:
            print('  the fixture met midi2tl.py rules it must avoid: %s' % ', '.join(unused))
        bad |= not ok
        if name != 'A':
            continue
        for mutant in mutants:
            mgot, _ = translate(mutant, path)
            mdiff = first_difference(ref, mgot)
            if not mdiff:
                continue
            caught.add(mutant)
            print('MIDICHECK mutant %s caught=%s%s' % (os.path.basename(mutant), 'yes' if mdiff else 'NO',
                                                     ' (first difference at %d ns)' % mdiff[0] if mdiff else ''))
            bad |= mdiff is None
    bad |= ctl_checks(m2t, work, binary, mutants, caught)
    bad |= mpe_checks(m2t, work, binary, mutants, caught)
    for mutant in mutants:
        if mutant not in caught:
            print('MIDICHECK mutant %s caught=NO' % os.path.basename(mutant))
            bad = 1
    print('MIDICHECK %s' % ('FAIL' if bad else 'ok'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
