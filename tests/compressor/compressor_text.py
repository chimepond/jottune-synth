#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Targeted native compressor/duck grammar, canonical rewrite and identity.

Run: python3 -B tests/compressor/compressor_text.py [HARNESS]
Generated records/logs stay under out; no WAVs or corpus render.
"""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
harness = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else root / 'build/s12e10p1v1/harness'
out = root / 'out/compressor'
out.mkdir(parents=True, exist_ok=True)
base = '''timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1000000
streams fx
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 tone
'''
checks = refused = 0
with tempfile.TemporaryDirectory(prefix='compressor-text-', dir=out) as tmp:
    tmp = Path(tmp)
    def run(text, expected=0, merged=False):
        global checks, refused
        path = tmp / 'input.tl'
        path.write_text(text, encoding='utf-8')
        dest = tmp / 'merged.tl'
        if dest.exists(): dest.unlink()
        command = [str(harness), '--play']
        if merged: command += ['--merged', str(dest)]
        command.append(str(path))
        r = subprocess.run(command, capture_output=True, text=True)
        if r.returncode != expected:
            raise SystemExit('COMPTEXT unexpected exit %d/%d\n%s\n%s' % (r.returncode, expected, r.stdout, r.stderr))
        if expected:
            if 'PLAY ' in r.stdout or dest.exists():
                raise SystemExit('COMPTEXT refusal produced output')
            refused += 1
        elif 'block_indep=yes' not in r.stdout:
            raise SystemExit('COMPTEXT missing successful render: ' + r.stdout)
        checks += 1
        return r.stdout, dest.read_text(encoding='utf-8') if dest.exists() else ''
    def row(values): return 'compressor ' + ' '.join(map(str, values)) + '\n'
    middle = [120, 40, 5, 120, 60]
    bounds = [(0, 600), (10, 200), (0, 100), (1, 5000), (0, 240)]
    for field, (lo, hi) in enumerate(bounds):
        for value in (lo, hi):
            values = middle[:]; values[field] = value
            _, text = run(base + row(values), merged=True)
            if row(values) not in text: raise SystemExit('COMPTEXT lost compressor header')
        for value in (lo-1, hi+1, '1.5', 'nan', '4294967296'):
            values = middle[:]; values[field] = value
            run(base + row(values), 1, merged=True)
    for suffix in ('', '120', '120 40 5 120', '120 40 5 120 60 0'):
        run(base + 'compressor ' + suffix + '\n', 1, merged=True)
    run(base + row(middle)*2, 1, merged=True)
    for value in (0, 1, 32768):
        _, text = run(base + 'limiter 10 100\n' + row(middle) + '0 fx 0 send duck %d\n' % value, merged=True)
        if text.index('compressor ') >= text.index('limiter ') or 'send duck %d' % value not in text:
            raise SystemExit('COMPTEXT canonical header/send order')
        _, replay = run(text, merged=True)
        if text != replay: raise SystemExit('COMPTEXT merged replay changed text')
    for value in (-1, 32769, '1.5', 'nan'):
        run(base + row(middle) + '0 fx 0 send duck %s\n' % value, 1, merged=True)
    for value in (0, 32768):
        run(base + '0 fx 0 send duck %d\n' % value, 1, merged=True)
    # Same latency in both records: limiter stays present. Each of the
    # three tones is -24dB; their maximum sum is below the -12dB threshold.
    original = (root/'tests/golden/fx/limiter.tl').read_text(encoding='utf-8')
    original = '\n'.join(line for line in original.splitlines() if not line.startswith(('#', 'hash ')))+'\n'
    original = original.replace('length_ns 1600000000', 'length_ns 2000000000')
    original = original.replace('patch tone 0 65536 0 ', 'patch tone 0 65536 240 ')
    for fs in (48000, 44100):
        record = original.replace('fs 48000', 'fs %d' % fs)
        dry, _ = run(record)
        comp, _ = run(record.replace('limiter 10 100', 'compressor 120 40 5 120 0\nlimiter 10 100'))
        ah = re.search(r'hash=([0-9a-f]{16})', dry)
        bh = re.search(r'hash=([0-9a-f]{16})', comp)
        if not ah or not bh or ah.group(1) != bh.group(1):
            raise SystemExit('COMPTEXT below-threshold identity failed')
        print('COMPIDENTITY fs=%d hash=%s limiter=both makeup=0' % (fs, ah.group(1)))
print('COMPTEXT ok checks=%d refusals=%d' % (checks, refused))
