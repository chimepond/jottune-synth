#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Exercise complete native readers and canonical profile preservation on Linux."""
from pathlib import Path
import argparse
import re
import subprocess
import tempfile

ap = argparse.ArgumentParser()
ap.add_argument('harness', type=Path)
a = ap.parse_args()
harness = a.harness.resolve()
root = Path(__file__).resolve().parents[2]
base = '''timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 30000000
streams f
patch sine 0 65536 0 0 0 256 0 0 0 0
patch sine 1 65536 0 0 0 0 0 0 0 0
voice 0 sine
{header}
0 f 0 on 440000 7000
{events}
'''
checks = refusals = 0
out = root / 'out/profiles'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='profile-', dir=out) as tmp:
    tmp = Path(tmp)
    record, merged, wave = (tmp / n for n in ('input.tl', 'merged.tl', 'output.wav'))

    def run(header='', events='', draft=1, fs=48000, error=None, command='--play', extra='', flag=True):
        global checks, refusals
        text = base.format(header=header+'\n'+('decodable' if flag else '')+'\n'+extra, events=events)
        text = text.replace('fs 48000', 'fs '+str(fs))
        if draft == 2:
            text = text.replace('timeline draft1', 'timeline draft2').replace('streams f', 'space stereo\nblock 64\nnear 100\nstreams f')
        record.write_text(text)
        merged.write_bytes(b'keep merged\n'); wave.write_bytes(b'keep wave\n')
        args = [str(harness), command, str(record)]
        if command == '--play': args += ['--merged', str(merged), '--wav', str(wave)]
        elif command == '--gen-soak': args += ['--merged', str(merged)]
        p = subprocess.run(args, text=True, capture_output=True)
        checks += 1
        if error is not None:
            refusals += 1
            assert p.returncode == 1 and error in p.stderr, (command, error, p.returncode, p.stdout, p.stderr)
            assert not p.stdout and merged.read_bytes() == b'keep merged\n' and wave.read_bytes() == b'keep wave\n', 'refusal published output'
            if error.startswith('timeline: decodable: refused '):
                assert p.stderr == error+'\n', (error, p.stderr)
            print('REFUSAL', command, p.stderr.strip().split(': ', 1)[-1])
            return p.stderr
        assert p.returncode == 0, (command, header, p.stdout, p.stderr)
        if command == '--play':
            assert 'block_indep=yes clipped=0' in p.stdout, p.stdout
            canonical = merged.read_text()
            assert canonical.count('\ndecodable\n') == int(flag)
            q = subprocess.run([str(harness), '--play', str(merged)], text=True, capture_output=True)
            checks += 1
            assert q.returncode == 0 and 'match=yes block_indep=yes clipped=0' in q.stdout, q.stdout+q.stderr
            return canonical, re.search(r'^PLAY hash=([0-9a-f]{16})', p.stdout, re.M)[1]
        return p.stdout

    refused = [('limiter 10 100', 'limiter'), ('softclip', 'softclip'),
               ('compressor 120 40 5 120 60', 'compressor'),
               ('chorus 15000 3000 800', 'chorus'),
               ('flanger 2000 1000 1 0 0', 'flanger (nonzero rate 1)'),
               ('flanger 2000 1000 2000 0 0', 'flanger (nonzero rate 2000)')]
    allowed = ['', 'dcblock 5000', 'gain -60', 'dither 12345', 'delay 150000 8192 6000',
               'reverb 500 500', 'flanger 2000 1000 0 20000 1']
    for fs in (48000, 44100):
        for draft in (1, 2):
            for header in allowed + (['pingpong 150000 8192 6000 100'] if draft == 2 else []):
                canon, got = run(header, draft=draft, fs=fs)
                old, want = run(header, draft=draft, fs=fs, flag=False)
                checks += 1
                assert got == want and canon.replace('decodable\n', '') == old, 'flag changed PCM or canonical content'
                if header: assert canon.index(header+'\n') < canon.index('decodable\n')
            for header, message in refused:
                run(header, draft=draft, fs=fs, error='timeline: decodable: refused '+message)
    for kind in ('hard', 'cubic', 'fold', 'asym'):
        for drive in (0, 1, 240):
            for position in ('', ' pre'):
                for patch in ('sine', 'unused'):
                    extra = '' if patch == 'sine' else 'patch unused 0 65536 0 0 0 256 0 0 0 0\npatch unused 1 65536 0 0 0 0 0 0 0 0'
                    run('shape %s %d %s%s' % (patch, drive, kind, position), extra=extra,
                        error='timeline: decodable: refused shape %s drive %d %s (curve applies even at zero drive)' % (patch, drive, kind))
    for events, message in [
        ('1 f 0 move 1000 2000 0 1000000', 'move (voice 0 at 1 ns)'),
        ('1 f 0 pos 1000 2000 0', 'pos (voice 0 at 1 ns; nonzero-time position in doubt)'),
        ('0 f 0 pos 1000 2000 0\n0 f 0 pos 1000 2000 0', 'pos (voice 0 at 0 ns; repeated position in doubt)'),
        ('0 f 0 pos 1000 2000 0\n100 f 0 pos 1000 2000 0', 'pos (voice 0 at 100 ns; repeated position in doubt)')]:
        run(events=events, draft=2, error='timeline: decodable: refused '+message)
    run(events='0 f 0 pos -100 100 0', draft=2)
    run('voice 1 sine', events='0 f 0 pos -100 100 0\n0 f 1 pos 100 100 0', draft=2)
    for field in ('amb1', 'amb3', 'binaural', 'panlaw'):
        text = base.format(header='decodable', events='').replace('timeline draft1', 'timeline draft2')
        statement = 'space stereo\npanlaw 32768' if field == 'panlaw' else 'space '+field
        record.write_text(text.replace('streams f', statement+'\nstreams f'))
        p = subprocess.run([str(harness), '--play', str(record)], text=True, capture_output=True)
        checks += 1; refusals += 1
        assert p.returncode == 1 and p.stderr == 'timeline: decodable: refused %s (field route in doubt)\n' % field, p.stderr
    # All four complete readers refuse the profile before any command-specific output.
    for command in ('--play', '--fxref', '--gen-events', '--gen-soak'):
        run('limiter 10 100', command=command, error='timeline: decodable: refused limiter')
    for command in ('--gen-events', '--gen-soak'):
        run('gen_clock 64\ngen 0 pink 12345 1 16', command=command)
    run('dcblock 5000', command='--fxref')
    # Insertion order, not timestamp order; all errors and both shapes survive.
    headers = 'chorus 15000 3000 800\nsoftclip\nlimiter 10 100\ncompressor 120 40 5 120 60\nflanger 2000 1000 300 0 0\nshape sine 0 cubic\nshape other 240 fold\npatch other 0 65536 0 0 0 256 0 0 0 0\npatch other 1 65536 0 0 0 0 0 0 0 0'
    events = '200 f 0 move 1 1 0 100\n100 f 0 move 2 2 0 100\n0 f 0 pos 0 100 0\n0 f 0 pos 0 100 0'
    run(headers, events, draft=2, error='timeline: decodable: refused limiter, softclip, compressor, chorus, flanger (nonzero rate 300), shape sine drive 0 cubic (curve applies even at zero drive), shape other drive 240 fold (curve applies even at zero drive), move (voice 0 at 200 ns), move (voice 0 at 100 ns), pos (voice 0 at 0 ns; repeated position in doubt)')
    for header, message in [('decodable', 'second decodable statement'), ('decodable 1', 'decodable takes no value')]:
        run(header, error=message)
    # The second input file carries the offender; checking only pass1 cannot catch it.
    record.write_text(base.format(header='decodable', events='').replace('timeline draft1', 'timeline draft2').replace('streams f', 'space stereo\nstreams f'))
    event_file = tmp/'events.tl'; event_file.write_text('1 f 0 move 0 100 0 1000\n')
    p = subprocess.run([str(harness), '--play', str(record), str(event_file)], text=True, capture_output=True)
    checks += 1; refusals += 1
    assert p.returncode == 1 and p.stderr == 'timeline: decodable: refused move (voice 0 at 1 ns)\n', p.stderr
print('DECODABLE checks=%d refusals=%d ok' % (checks, refusals))
