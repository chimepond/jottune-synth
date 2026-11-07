# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Native profile readers and live lifecycle, with real regressions; no device."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

bridge, lib, work, golden = map(lambda s: Path(s).resolve(), sys.argv[1:5])
# Desktop imports are thin adapters; mutate the shared implementation.
shared = bridge.parents[2] / 'bindings/native'
if shared.is_dir():
    bridge = shared
work.mkdir(parents=True, exist_ok=True)
source = (bridge / 'jottune_bridge.c').read_text()
(work / 'jottune_bridge.h').write_bytes((bridge / 'jottune_bridge.h').read_bytes())
(work / 'profile-probe.c').write_bytes(Path(__file__).with_name('profile_probe.c').read_bytes())
cc = shlex.split(os.environ.get('CC', 'cc'))
flags = ['-std=c99', '-O1', '-g', '-Wall', '-Wextra', '-D_GNU_SOURCE',
         '-DFM_SINE_BITS=12', '-DFM_EXP_BITS=10', '-DFM_PHASE64=1', '-DFM_ENV64=1',
         '-fsanitize=undefined', '-fno-sanitize-recover=all', '-ffunction-sections', '-fdata-sections',
         '-DSP_BINAURAL_H="' + str(lib / 'sp_binaural_tables.h') + '"',
         '-I' + str(bridge), '-I' + str(lib.parent.parent / 'src/host'), '-I' + str(lib)]
objects = [str(lib / (x + '.o')) for x in ('fm_a1', 'space', 'fx', 'field', 'gen')]
link = ['-Wl,-dead_strip'] if sys.platform == 'darwin' else ['-Wl,--gc-sections', '-lm']

def run(name, text, succeeds, mode=[]):
    (work / 'fmspike-profile.c').write_text(text)
    exe = work / name
    subprocess.run(cc + flags + [str(work / 'profile-probe.c')] + objects + link + ['-pthread', '-o', str(exe)], check=True)
    result = subprocess.run([str(exe), str(work / (name + '.tl')), str(golden)] + mode, text=True, capture_output=True, timeout=30)
    if (result.returncode == 0) != succeeds:
        print(result.stdout, end=''); print(result.stderr, end='', file=sys.stderr)
        raise SystemExit('PROFILE_API unexpected result: ' + name)
    if succeeds:
        print(result.stdout, end='')
    elif 'PROFILE assertion' not in result.stderr:
        raise SystemExit('PROFILE_API unrelated control failure: ' + name + '\n' + result.stderr)
    else:
        print('PROFILE_CONTROL ' + name + ' caught ' + result.stderr.strip())

run('profile-positive', source, True)
decodable_check = '    if (!rc) rc = tl_check_decodable(p->tl);'
live_start = source.index('fms_player_t *fms_player_open_live(')
live_end = source.index('\n}', live_start)
assert source[live_start:live_end].count(decodable_check) == 1
controls = [
    ('record-check', decodable_check, '', 0),
    ('live-check', decodable_check, '', source[:live_start].count(decodable_check)),
    ('patch-scope', 'tl->fx.on || tl->decodable || tl->gen_lanes', 'tl->fx.on || tl->gen_lanes', 0),
    ('eventful-live', 'if (!rc && p->tl->decodable && p->tl->nev)', 'if (0)', 0),
    ('live-profile', 'if (tl->decodable && (in->type', 'if (0 && (in->type', 0),
    ('position-mask', 'p->positioned |= UINT64_C(1) << e->voice;', '(void)0;', 0),
    ('rewind-mask', 'p->positioned = 0;', '/* deliberate missing reset */', 0),
]
for name, old, new, occurrence in controls:
    parts = source.split(old)
    assert len(parts) > occurrence + 1, name
    mutated = old.join(parts[:occurrence + 1]) + new + old.join(parts[occurrence + 1:])
    run(name, mutated, False)
# The patch-only scope rejects the flag before complete-reader validation.
# Remove only that earlier guard to exercise the later defense independently;
# removing its actual tl_check_decodable call must then accept the bad shape.
scoped = source.replace('tl->fx.on || tl->decodable || tl->gen_lanes', 'tl->fx.on || tl->gen_lanes')
run('patch-complete-positive', scoped, True, ['patch-complete'])
a = '        if (!rc) rc = tl_check_decodable(tl);'
assert scoped.count(a) == 1
run('patch-complete-check', scoped.replace(a, ''), False, ['patch-complete'])
(work / 'fmspike-profile.c').write_text(source)
