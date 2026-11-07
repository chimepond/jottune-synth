# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
""" live-map public API controls, no audio. Build a private translator library.
Usage: python3 -B ctl_api.py BRIDGE_DIR WORK_DIR [--controls]
CC defaults to cc. CFLAGS may add sanitizers. No repository outputs are written.
"""
import ctypes as C
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

U32, U64, I32 = C.c_uint32, C.c_uint64, C.c_int32
CFG = 'nvoices stream channels release_ms bend_cents rpn cutoff_millihz q_milli ctl_reads'.split()
STATS = ('messages notes note_offs orphan_offs restruck pedal_downs pedal_held steals release_steals bends '
         'pitch_events cc74 filter_events rpn_selects rpn_ranges programs aftertouch pressure other_ccs '
         'other_channels system unknown clamped near_ties refused ctl_messages ctl_events '
         'mpe_notes mpe_pressure mpe_slides mcm').split()


class Config(C.Structure):
    _fields_ = [(k, U32) for k in CFG]


class Event(C.Structure):
    _fields_ = [('t_ns', U64)] + [(k, U32) for k in 'sample type voice stream freq_millihz value bus'.split()] + [
        (k, I32) for k in 'x y z'.split()] + [('dur_ns', U64)]


class Stats(C.Structure):
    _fields_ = [(k, U64) for k in STATS]


def main():
    bridge, work = [Path(x).resolve() for x in sys.argv[1:3]]
    # Copied MIDI mutants need a standalone header, not the desktop adapter.
    shared = bridge.parents[2] / 'bindings/native'
    header = (shared if shared.is_dir() else bridge) / 'jottune_bridge.h'
    controls = sys.argv[3:] == ['--controls']
    if sys.argv[3:] and not controls:
        raise ValueError('expected optional --controls')
    work.mkdir(parents=True, exist_ok=True)
    wrapper = work / 'ctl_layout.c'
    # Every field offset is checked before the first ctypes call consuming a struct.
    exprs = ['sizeof(fms_midi_config_t)'] + ['offsetof(fms_midi_config_t,%s)' % k for k in CFG]
    exprs += ['sizeof(fms_event_t)'] + ['offsetof(fms_event_t,%s)' % k for k, _ in Event._fields_]
    exprs += ['sizeof(fms_midi_stats_t)'] + ['offsetof(fms_midi_stats_t,%s)' % k for k in STATS]
    exprs += ['FMS_EV_ON', 'FMS_EV_OFF', 'FMS_EV_OTHER', 'FMS_EV_CTL', 'FMS_MIDI_OUT']
    wrapper.write_text('/* Copyright (c) 2026 Nenad Micic; SPDX-License-Identifier: Apache-2.0 */\n'
                       '#include "jottune_bridge.h"\nsize_t ctl_layout(size_t i) {\n'
                       'static const size_t v[] = {' + ','.join(exprs) + '};\n'
                       'return i < sizeof(v)/sizeof(v[0]) ? v[i] : (size_t)-1; }\n')
    libpath = work / ('ctl_api.dylib' if sys.platform == 'darwin' else 'ctl_api.so')
    command = shlex.split(os.environ.get('CC', 'cc')) + ['-std=c99', '-O2', '-Wall', '-Wextra', '-Werror', '-fPIC']
    command += shlex.split(os.environ.get('CFLAGS', ''))
    command += ['-dynamiclib' if sys.platform == 'darwin' else '-shared', '-I' + str(bridge),
                str(wrapper), str(bridge / 'fms_midi.c'), '-o', str(libpath)]
    subprocess.run(command, check=True)
    lib = C.CDLL(str(libpath)); lib.ctl_layout.argtypes = [C.c_size_t]; lib.ctl_layout.restype = C.c_size_t
    want = [C.sizeof(Config)] + [getattr(Config, k).offset for k in CFG]
    want += [C.sizeof(Event)] + [getattr(Event, k).offset for k, _ in Event._fields_]
    want += [C.sizeof(Stats)] + [getattr(Stats, k).offset for k in STATS] + [1, 2, 9, 10, 138]
    assert [lib.ctl_layout(i) for i in range(len(want))] == want, 'ctypes/header layout or enum mismatch'
    lib.fms_midi_create.argtypes = [C.POINTER(Config)]; lib.fms_midi_create.restype = C.c_void_p
    lib.fms_midi_destroy.argtypes = [C.c_void_p]
    lib.fms_midi_reset.argtypes = [C.c_void_p]
    lib.fms_midi_set_ctl.argtypes = [C.c_void_p, U32, C.c_char_p, C.c_char_p, C.c_size_t]
    lib.fms_midi_set_ctl.restype = C.c_int
    lib.fms_midi_set_mpe.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p, C.c_size_t]
    lib.fms_midi_set_mpe.restype = C.c_int
    lib.fms_midi_ump.argtypes = [C.c_void_p, U64, C.POINTER(U32), U32, C.POINTER(Event), U32, C.POINTER(U32)]
    lib.fms_midi_ump.restype = U32
    lib.fms_midi_stats.argtypes = [C.c_void_p, C.POINTER(Stats)]
    lib.fms_midi_external.argtypes = [C.c_void_p, U64, U32, C.c_int]
    checks, refs, machines = [len(want)], [0], []

    def check(value, why):
        checks[0] += 1
        if not value:
            raise AssertionError(why)

    def create(reads=255, nv=3, cut=0, chans=3):
        cfg = Config(nv, 0, chans, 10, 200, 1, cut, 707, reads)
        m = lib.fms_midi_create(C.byref(cfg)); check(bool(m), 'create'); machines.append(m); return m

    def setup(m, spec, channel=0, valid=True):
        err = C.create_string_buffer(180)
        ret = lib.fms_midi_set_ctl(m, channel, spec.encode(), err, len(err))
        check((ret == 0 and not err.value) if valid else (ret == -1 and bool(err.value)), 'setup ' + spec)
        refs[0] += not valid

    def stats(m):
        st = Stats(); lib.fms_midi_stats(m, C.byref(st)); return {k: getattr(st, k) for k in STATS}

    def message(m, t, status, a=0, b=0, cap=138):
        w, used = U32(0x20000000 | status << 16 | a << 8 | b), U32()
        out = (Event * 140)()
        for e in out:
            e.type = 0x76543210
        n = lib.fms_midi_ump(m, t, C.byref(w), 1, out, cap, C.byref(used))
        check(n <= cap and used.value == 1 and out[n].type == 0x76543210, 'capacity sentinel')
        return [tuple(getattr(e, k) for k, _ in Event._fields_) for e in out[:n]]

    def ctls(events):
        return [(e[3], e[7], e[8]) for e in events if e[2] == 10]  # voice,input,x

    try:
        m = create(); setup(m, 'default')
        invalid = ['cc0:0', 'cc120:0', 'cc6:0', 'cc38:0', 'cc64:0', 'cc96:0', 'cc101:0', 'cc32+:0',
                   'cc6+:0', 'nrpn128.0:0', 'nrpn0.128:0', 'nrpn127.127:0', 'rand:0:0',
                   'rand:4294967296:0', 'rand:00000000001:0', 'cc1:8', 'cc1:-1', 'cc1:00', 'cc1:0,',
                   ',cc1:0', 'cc1 :0', 'cc1:0,,cc2:1', 'cc1:0,cc1:1', 'cc1+:0,cc33:1',
                   'cc33:1,cc1+:0', 'rand:1:0,rand:2:1', 'rand:1:0,pressure:0',
                   'pressure:0,rand:1:0', 'default,cc1:7', 'nrpn', 'nrpn1', 'nrpn1.', 'rand:', 'pressure',
                   'CC1:0', 'cc0001:0', 'defaultx']
        for spec in invalid:
            setup(m, spec, valid=False)
        setup(m, 'default', 16, False)
        setup(m, 'default', 2, False)
        # Every failed setup left the original map intact.
        check(ctls(message(m, 1, 0x90, 60, 100)) == [(0, 6, -12020)], 'setup atomicity')
        setup(m, 'off', valid=False)
        lib.fms_midi_reset(m)
        check(ctls(message(m, 1, 0x90, 60, 100)) == [(0, 6, -12020)], 'reset preserves map and seed')
        lib.fms_midi_reset(m); setup(m, 'off'); check(not ctls(message(m, 1, 0x90, 60, 100, cap=5)), 'map off')
        lib.fms_midi_reset(m); setup(m, ''); setup(m, 'default,cc7:7')
        # Setup after external consumption is refused, including its time-zero call.
        lib.fms_midi_external(m, 0, 0, 1); setup(m, 'off', valid=False)
        lib.fms_midi_reset(m); lib.fms_midi_external(m, 0, 0, 0); setup(m, 'off', valid=False)
        mfilter = create(cut=1000000); setup(mfilter, 'cc74:0', valid=False)
        badcfg = Config(3, 0, 1, 10, 200, 1, 0, 0, 256)
        check(not lib.fms_midi_create(C.byref(badcfg)), 'high read-mask bit refused')
        # Capacity refusal must not advance last_t, RNG, channel or voice latches.
        for rejected in [(0x90, 70, 100), (0xB0, 1, 127), (0xD0, 50, 0), (0x80, 60, 100)]:
            a, b = create(), create(); setup(a, 'default'); setup(b, 'default')
            check(message(a, 10, 0x90, 60, 100) == message(b, 10, 0x90, 60, 100), 'capacity prefix')
            check(message(a, 100000000, *rejected, cap=15) == [], 'mapped undersize refused')
            for t, msg in [(20, (0xB0, 1, 50)), (30, (0x90, 64, 80)), (40, (0x80, 60, 30)), (50, (0x90, 67, 90))]:
                check(message(a, t, *msg) == message(b, t, *msg), 'capacity continuation state')
            sa, sb = stats(a), stats(b)
            check(sa.pop('messages') == sb.pop('messages') + 1 and sa.pop('refused') == sb.pop('refused') + 1
                  and sa == sb, 'only legacy diagnostic counters change')
        unread = create(reads=0); setup(unread, 'default')
        check(len(message(unread, 1, 0x90, 60, 100, cap=5)) == 1, 'unread map legacy capacity')
        # Exact endpoints and every value of seven- and fourteen-bit normalization.
        m = create(nv=1); setup(m, 'cc1:0,cc2+:1'); message(m, 1, 0x90, 60, 100)
        old = 0
        for v in range(128):
            wantv = (v * 32768 + 63) // 127
            check(ctls(message(m, v + 2, 0xB0, 1, v)) == ([] if wantv == old else [(0, 0, wantv)]), 'q7')
            old = wantv
        old = 0
        for v in range(16384):
            hi = v >> 7
            if not v & 127:
                wantv = ((hi * 128) * 32768 + 8191) // 16383
                check(ctls(message(m, v + 1000, 0xB0, 2, hi)) == ([] if wantv == old else [(0, 1, wantv)]), 'wide MSB clears LSB')
                old = wantv
            wantv = (v * 32768 + 8191) // 16383
            check(ctls(message(m, v + 1000, 0xB0, 34, v & 127)) == ([] if wantv == old else [(0, 1, wantv)]), 'q14')
            old = wantv
        # Reset clears both source assembly banks, channel values and voice latches.
        a, b = create(), create()
        spec = 'cc1+:0,nrpn1.2:2,rand:12345:6'
        setup(a, spec); setup(b, spec)
        transcript = [(0xB0, 33, 100), (0x90, 60, 100), (0xB0, 1, 11),
                      (0xB0, 99, 1), (0xB0, 98, 2), (0xB0, 38, 7),
                      (0xB0, 6, 9), (0x80, 60, 100), (0x90, 64, 80)]
        for t, msg in enumerate(transcript):
            message(a, t + 1, *msg)
        lib.fms_midi_reset(a)
        for t, msg in enumerate(transcript):
            check(message(a, t + 1, *msg) == message(b, t + 1, *msg), 'reset entire controller continuation')
        check(stats(a) == stats(b), 'reset all diagnostic counts')
        # Independent per-channel seeds.
        a, b = create(), create(); setup(a, 'default'); setup(a, 'default', 1); setup(b, 'default')
        message(a, 1, 0x91, 50, 100)
        for i in range(12):
            xa = [e[2] for e in ctls(message(a, i + 2, 0x90, 60, 100)) if e[1] == 6]
            xb = [e[2] for e in ctls(message(b, i + 2, 0x90, 60, 100)) if e[1] == 6]
            check(xa == xb, 'channel seed independence')
        # All 64 held voices can emit release velocity plus off at pedal-up.
        m = create(nv=64); setup(m, 'relvel:5')
        message(m, 1, 0xB0, 64, 127)
        for key in range(64):
            message(m, 2 + key * 2, 0x90, key, 100); message(m, 3 + key * 2, 0x80, key, 100)
        result = message(m, 200, 0xB0, 64, 0)
        check(len(result) == 128 and all(result[i][2] == 10 and result[i + 1][2] == 2 for i in range(0, 128, 2)),
              '64-voice release capacity and ordering')
        ctl_count, ctl_refusals = checks[0], refs[0]
        mpe_refusals = 0

        def mpe(m, spec, valid=True):
            nonlocal mpe_refusals
            err = C.create_string_buffer(160)
            ret = lib.fms_midi_set_mpe(m, spec.encode(), err, len(err))
            check((ret == 0 and not err.value) if valid else (ret == -1 and bool(err.value)), 'mpe setup ' + spec)
            mpe_refusals += not valid

        # Every lower/upper member count; only the selected zone owns the pool.
        for upper in (False, True):
            for n in range(1, 16):
                m = create(reads=0, chans=0xFFFF)
                mpe(m, ('upper:' if upper else 'lower:') + str(n))
                master = 15 if upper else 0
                member = 15 - n if upper else n
                check(len(message(m, 1, 0x90 | member, 69, 100)) == 1, 'edge member accepted')
                if n < 15:
                    outside = member - 1 if upper else member + 1
                    check(message(m, 2, 0x90 | outside, 69, 100) == [] and stats(m)['other_channels'] == 1,
                          'outside zone ignored')
                lib.fms_midi_reset(m)
                check(len(message(m, 1, 0x90 | master, 69, 100)) == 1, 'master accepted after reset')
                mpe(m, 'off', False)
        m = create(chans=1)
        mpe(m, 'lower:2')
        for spec in ['lower:0', 'lower:16', 'upper:0', 'upper:16', 'lower:-1', 'lower:+1', 'lower:',
                     'middle:2', 'lower:1,upper:1', 'lower:1 lower:2', 'lower:1x', 'lower:99999999999', 'upper:2']:
            mpe(m, spec, False)
        check(len(message(m, 1, 0x91, 69, 100)) == 1, 'failed zone setups preserve prior zone')
        lib.fms_midi_reset(m); mpe(m, 'off')
        check(message(m, 1, 0x91, 69, 100) == [], 'off restores original channel mask')
        # Conflicting maps are refused in both setup orders, independently of reads.
        for spec in ('default', 'pressure:1', 'cc1:4', 'rand:7:1'):
            m = create(reads=0); setup(m, spec); mpe(m, 'lower:1', False)
            message(m, 1, 0x91, 69, 100)
            check(stats(m)['mpe_notes'] == 0, 'failed map conflict leaves MPE off')
            m = create(reads=0); mpe(m, 'lower:1'); setup(m, spec, valid=False)
        m = create(); setup(m, 'cc1:0', 1); mpe(m, 'lower:1', False)
        m = create(); mpe(m, 'lower:1'); setup(m, 'cc1:0', 1, False)
        setup(m, 'cc1:0'); setup(m, 'off', 1)
        # Rejected MPE note must not consume cached pressure, time or pool state.
        a, b = create(), create(); mpe(a, 'lower:2'); mpe(b, 'lower:2')
        for m in (a, b):
            message(m, 10, 0xD1, 30); message(m, 20, 0xB1, 74, 90)
        check(message(a, 100000000, 0x91, 69, 100, cap=15) == [], 'MPE capacity refusal')
        check(message(a, 30, 0x91, 69, 100) == message(b, 30, 0x91, 69, 100), 'MPE refusal continuation')
        # Shared member range, exact master+member addition, and reset continuation.
        a, b = create(), create(); mpe(a, 'lower:2'); mpe(b, 'lower:2')
        sequence = [(0xE1, 0, 96), (0x91, 69, 100), (0xB1, 101, 0), (0xB1, 100, 0), (0xB1, 6, 24),
                    (0xB2, 101, 0), (0xB2, 100, 0), (0xB2, 38, 50), (0xE2, 0, 96), (0x92, 69, 100),
                    (0xD1, 30), (0xB1, 74, 90), (0xE0, 0, 96)]
        first = []
        for t, msg in enumerate(sequence):
            first.append(message(a, 2 + t * 2, *msg))
        check(first[1][-1][5] == 1760000, 'member default48 st half bend is two octaves')
        from decimal import Decimal, localcontext, ROUND_HALF_UP
        with localcontext() as ctx:
            ctx.prec = 60
            expected = int((Decimal(440000) * (Decimal(2) ** (Decimal('12.25') / 12))).to_integral_value(rounding=ROUND_HALF_UP))
        check(first[9][-1][5] == expected, 'CC38 on second member keeps shared24 semitones')
        lib.fms_midi_reset(a)
        for t, msg in enumerate(sequence):
            check(message(a, 2 + t * 2, *msg) == message(b, 2 + t * 2, *msg), 'MPE reset exact continuation')
        check(stats(a) == stats(b), 'MPE reset counts')
        # Centered member bends equal a single plain channel, including master bends.
        for upper in (False, True):
            a, b = create(chans=0xFFFF), create(chans=0xFFFF)
            mpe(a, 'upper:2' if upper else 'lower:2')
            master = 15 if upper else 0
            first, second = (14, 13) if upper else (1, 2)
            sequence = [(0x90 | first, 60, 90), (0x90 | second, 64, 100),
                        (0xE0 | first, 0, 64), (0xE0 | second, 0, 64),
                        (0xE0 | master, 0, 96), (0x80 | first, 60, 40), (0x80 | second, 64, 50)]
            for t, (status, x, y) in enumerate(sequence):
                # Member-centered messages do not reset the plain master bend;
                # both occur before the master's own bend in this control.
                check(message(a, t + 1, status, x, y) == message(b, t + 1, status & 0xF0, x, y),
                      'centered zone equals plain channel')
        # CC74 goes only to member ctl4 even with an ordinary cutoff configured.
        m = create(cut=1000000); mpe(m, 'lower:1')
        message(m, 1, 0xB1, 74, 90)
        result = message(m, 2, 0x91, 69, 100)
        check(ctls(result) == [(0, 4, 23221)] and all(e[2] != 6 for e in result), 'MPE slide never filter')
        # External keyboard ownership makes a stale latest-member token inert.
        lib.fms_midi_external(m, 3, 0, 1)
        check(message(m, 4, 0xD1, 90) == [], 'external takeover detaches member expression')
        print('MIDIMPE_API checks=%d refusals=%d zones=30 reset=yes capacity=yes owner_paths=excluded ok' %
              (checks[0] - ctl_count, mpe_refusals))
        print('MIDICTL_API checks=%d refusals=%d layout=yes reset=yes capacity=yes q7=128 q14=16384 ok' % (ctl_count, ctl_refusals))
    finally:
        for m in machines:
            lib.fms_midi_destroy(m)

    if controls:
        source = ((shared if shared.is_dir() else bridge) / 'fms_midi.c').read_text()
        mutations = {
            'reset-seed': ('m->map[c].rng = m->map[c].seed;', '/* negative: retain random stream */'),
            'capacity': ('(m->mapped || m->zone_mask) && m->cfg.ctl_reads ? 2 * m->cfg.nvoices + 10 : m->cfg.nvoices + 2',
                         'm->cfg.nvoices + 2'),
            'wide-reset': ('src->msb = (uint8_t)b; src->lsb = 0;',
                           'src->msb = (uint8_t)b; /* negative: retain low byte */'),
            'setup-atomic': ('invalid:\n    free(next.src);',
                             'invalid:\n    m->map[c].count = 0;\n    free(next.src);'),
            'zone-filter': ('m->zone_mask ? m->zone_mask : m->cfg.channels', 'm->cfg.channels'),
            'zone-range': ('if (i != m->master && (m->zone_mask & (1u << i))) m->ch[i].range = ch->range;',
                           'if (i == c) m->ch[i].range = ch->range;'),
            'zone-atomic': ('    for (c = 0; c < 16; c++) {\n        if (!(mask & (1u << c)))',
                            '    m->zone_mask = mask;\n    for (c = 0; c < 16; c++) {\n        if (!(mask & (1u << c)))')}
        for name, (old, new) in mutations.items():
            assert source.count(old) == 1, (name, 'mutation anchor changed')
            dest = work / ('mutant-' + name)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(header, dest / 'jottune_bridge.h')
            (dest / 'fms_midi.c').write_text(source.replace(old, new))
            run = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), str(dest), str(dest / 'build')],
                                 text=True, capture_output=True)
            last = run.stderr.strip().splitlines()[-1] if run.stderr.strip() else ''
            assert run.returncode == 1 and last.startswith('AssertionError:'), (name, run.returncode, run.stdout, run.stderr)
            print('MIDICTL_CONTROL %s exit=1 %s caught=yes' % (name, last))


if __name__ == '__main__':
    main()
