#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
""" native reader/writer checks; the harness path is the only argument."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
harness = Path(sys.argv[1]).resolve()
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
out = root / 'out/flanger'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='text-', dir=out) as td:
    directory = Path(td)
    record = directory / 'input.tl'
    merged = directory / 'merged.tl'
    def run(header, events='', fs=48000, bad=False, source=None):
        global checks, refusals
        text = base.format(header=header, events=events).replace('fs 48000', 'fs '+str(fs)) if source is None else source
        record.write_text(text, encoding='utf-8')
        p = subprocess.run([str(harness), '--play', str(record), '--merged', str(merged)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        checks += 1
        if bad:
            refusals += 1
            if p.returncode != 1 or re.search(r'^PLAY ', p.stdout, re.M):
                raise SystemExit('FLANGERTEXT expected refusal: '+repr(header)+'\n'+p.stdout)
            return None
        if p.returncode or 'block_indep=yes clipped=0' not in p.stdout:
            raise SystemExit('FLANGERTEXT expected success: '+repr(header)+'\n'+p.stdout)
        result = merged.read_text(encoding='utf-8')
        q = subprocess.run([str(harness), '--play', str(merged)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        checks += 1
        if q.returncode or 'match=yes block_indep=yes clipped=0' not in q.stdout:
            raise SystemExit('FLANGERTEXT canonical replay failed\n'+q.stdout)
        return result, re.search(r'^PLAY hash=([0-9a-f]{16})',p.stdout,re.M).group(1)
    tuples = [(100,100,0,0,0),(10000,10000,2000,29491,1),(10000,100,0,29491,0),
              (2000,1000,300,20000,0),(2000,1000,0,20000,1),(125,125,1,1,1)]
    for fs in (8000,44100,48000,192000):
        for fields in tuples:
            header='flanger '+' '.join(map(str,fields))
            result,_=run(header,'0 f 0 send flanger 32768',fs)
            checks += 1
            if header+'\n' not in result or '0 f 0 send flanger 32768\n' not in result:
                raise SystemExit('FLANGERTEXT header/event roundtrip dropped fields')
    good = ['2000','1000','300','20000','0']
    for i, choices in enumerate((('99','10001','-1','4294967296'),('99','2001','10001','-1'),
                                 ('2001','-1','4294967296'),('29492','-1','4294967296'),('2','-1','4294967296'))):
        for value in choices:
            fields=good.copy();fields[i]=value;run('flanger '+' '.join(fields),bad=True)
    for fields in ('','2000','2000 1000 300 20000','2000 1000 300 20000 0 1','2000 1000 300.0 20000 0','2000 1000 nan 20000 0'):
        run('flanger '+fields,bad=True)
    run('flanger '+' '.join(good)+'\nflanger '+' '.join(good),bad=True)
    run('', '0 f 0 send flanger 0',bad=True)
    for level in ('-1','32769','4294967296','1.5'):
        run('flanger '+' '.join(good),'0 f 0 send flanger '+level,bad=True)
    run('flanger '+' '.join(good),'0 f 0 send nosuchbus 0',bad=True)
    old, oldhash=run('')
    for header in ('flanger '+' '.join(good),'chorus 15000 3000 800\nflanger '+' '.join(good)):
        _, newhash=run(header)
        checks += 1
        if newhash != oldhash: raise SystemExit('FLANGERTEXT no-send identity failed')
    header='flanger '+' '.join(good)+'\nchorus 15000 3000 800'
    ordered,_=run(header)
    checks += 1
    if ordered.index('chorus ') > ordered.index('flanger '): raise SystemExit('FLANGERTEXT canonical order failed')
print('FLANGERTEXT checks=%d refusals=%d ok' % (checks,refusals))
