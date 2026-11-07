# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Public geometry API checks plus actual seed/log regressions; no audio device."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

bridge, lib, work = map(lambda s: Path(s).resolve(), sys.argv[1:4])
# Desktop imports are thin adapters; mutate the shared implementation.
shared = bridge.parents[2] / 'bindings/native'
if shared.is_dir():
    bridge = shared
work.mkdir(parents=True, exist_ok=True)
source = (bridge / 'jottune_bridge.c').read_text()
(work / 'jottune_bridge.h').write_bytes((bridge / 'jottune_bridge.h').read_bytes())
(work / 'geometry-probe.c').write_bytes(Path(__file__).with_name('geometry_probe.c').read_bytes())
cc = shlex.split(os.environ.get('CC', 'cc'))
flags = ['-std=c99', '-O1', '-g', '-Wall', '-Wextra', '-D_GNU_SOURCE',
         '-DFM_SINE_BITS=12', '-DFM_EXP_BITS=10', '-DFM_PHASE64=1', '-DFM_ENV64=1',
         '-fsanitize=undefined', '-fno-sanitize-recover=all', '-ffunction-sections', '-fdata-sections',
         '-DSP_BINAURAL_H="' + str(lib / 'sp_binaural_tables.h') + '"',
         '-I' + str(bridge), '-I' + str(lib.parent.parent / 'src/host'), '-I' + str(lib)]
objects = [str(lib / (x + '.o')) for x in ('fm_a1', 'space', 'fx', 'field', 'gen')]
link = ['-Wl,-dead_strip'] if sys.platform == 'darwin' else ['-Wl,--gc-sections', '-lm']

def run(name, text, succeeds):
    (work / 'fmspike-geometry.c').write_text(text)
    exe = work / name
    subprocess.run(cc + flags + [str(work / 'geometry-probe.c')] + objects + link + ['-pthread', '-o', str(exe)], check=True)
    result = subprocess.run([str(exe), str(work / (name + '.tl'))], text=True, capture_output=True, timeout=30)
    if (result.returncode == 0) != succeeds:
        print(result.stdout, end=''); print(result.stderr, end='', file=sys.stderr)
        raise SystemExit('GEOMETRY_API unexpected result: ' + name)
    if succeeds:
        print(result.stdout, end='')
    elif 'GEOMETRY assertion' not in result.stderr:
        raise SystemExit('GEOMETRY_API unrelated control failure: ' + name + '\n' + result.stderr)
    else:
        print('GEOMETRY_CONTROL ' + name + ' caught ' + result.stderr.strip())

run('geometry-positive', source, True)
a = 'for (i = 0; i < p->seed_count; i++) fms_apply(p, &p->seed[i]);'
assert source.count(a) == 2
run('drop-seed', source.replace(a, '/* deliberate regression: omit seed application */'), False)
a = 'h->nlog = p->seed_count;'
assert source.count(a) == 1
run('drop-prefix', source.replace(a, 'h->nlog = 0; /* deliberate regression */'), False)
(work / 'fmspike-geometry.c').write_text(source)
