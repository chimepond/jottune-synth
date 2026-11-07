#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Force take ownership interleavings in a copied bridge; no audio device/WAV.
Usage: take_api.py BRIDGE_DIR APPROVED_LIB_DIR WORK_DIR
The shared native build supplies the harness and library objects.
"""
import os
from pathlib import Path
import shlex
import subprocess
import sys

bridge, lib, work = (Path(x).resolve() for x in sys.argv[1:])
# Desktop imports are thin adapters; mutate the shared implementation.
shared = bridge.parents[2] / 'bindings/native'
if shared.is_dir():
    bridge = shared
work.mkdir(parents=True, exist_ok=True)
source = (bridge / 'jottune_bridge.c').read_text()

def change(text, old, new):
    if text.count(old) != 1:
        raise SystemExit('TAKE_API instrument anchor is not unique: ' + old[:60])
    return text.replace(old, new, 1)

source = change(source, 'fms_host_t *fms_host_create(void)',
                'static void take_hook(int point);\nfms_host_t *fms_host_create(void)')
source = change(source, '    p = h->cur;\n    if (fms_take_change',
                '    p = h->cur;\n    take_hook(1);\n    if (fms_take_change')
source = change(source, '    if (fms_take_change(h, TAKE_PENDING, TAKE_CLAIMED)) {',
                '    if (fms_take_change(h, TAKE_PENDING, TAKE_CLAIMED)) {\n        take_hook(2);')
source = change(source, '        if (p && p->live && p == h->take_src) {',
                '        if (p && p->live && p == h->take_src) {\n            take_hook(3);')
source = change(source, '    if (prepared && p && p->live) {',
                '    if (prepared && p && p->live) {\n        take_hook(4);')
source = change(source, '    memcpy(&h->take_tl, h->ui_cur->tl, sizeof h->take_tl);',
                '    take_hook(5);\n    memcpy(&h->take_tl, h->ui_cur->tl, sizeof h->take_tl);')
source = change(source, '            fms_sleep_us(1);\n            continue;',
                '            take_hook(6);\n            fms_sleep_us(1);\n            continue;')
source = change(source, '                fms_player_rewind(p);',
                '                take_hook(7);\n                fms_player_rewind(p);')
(work / 'fmspike-take.c').write_text(source)
(work / 'jottune_bridge.h').write_bytes((bridge / 'jottune_bridge.h').read_bytes())
# The probe owns pthread synchronization; the shared bridge no longer needs it.
(work / 'take-probe.c').write_text('#include <pthread.h>\n' + Path(__file__).with_name('take_probe.c').read_text())
cc = shlex.split(os.environ.get('CC', 'cc'))
flags = ['-std=c99', '-O1', '-g', '-Wall', '-Wextra', '-D_GNU_SOURCE',
         '-DFM_SINE_BITS=12', '-DFM_EXP_BITS=10', '-DFM_PHASE64=1', '-DFM_ENV64=1',
         '-fsanitize=undefined', '-fno-sanitize-recover=all', '-ffunction-sections', '-fdata-sections',
         '-DSP_BINAURAL_H="' + str(lib / 'sp_binaural_tables.h') + '"',
         '-I' + str(bridge), '-I' + str(lib.parent.parent / 'src/host'), '-I' + str(lib)]
objects = [str(lib / (x + '.o')) for x in ('fm_a1', 'space', 'fx', 'field', 'gen')]
link = ['-Wl,-dead_strip'] if sys.platform == 'darwin' else ['-Wl,--gc-sections', '-lm']

def run(name, text, succeeds):
    (work / 'fmspike-take.c').write_text(text)
    exe = work / name
    subprocess.run(cc + flags + [str(work / 'take-probe.c')] + objects + link + ['-pthread', '-o', str(exe)], check=True)
    result = subprocess.run([str(exe), str(work / (name + '.tl'))], text=True, capture_output=True, timeout=20)
    if (result.returncode == 0) != succeeds:
        print(result.stdout, end=''); print(result.stderr, end='', file=sys.stderr)
        raise SystemExit('TAKE_API unexpected result: ' + name)
    if succeeds:
        print(result.stdout, end='')
    elif 'TAKE_API assertion' not in result.stderr:
        raise SystemExit('TAKE_API control failed for an unrelated reason: ' + name + '\n' + result.stderr)
    else:
        print('TAKE_CONTROL ' + name + ' caught')

run('take-positive', source, True)
# Each control changes actual ownership/publication code, not test expectations.
run('mismatch-stop', change(source,
    '        } else if (!fms_take_change(h, TAKE_CLAIMED, TAKE_PENDING)) {',
    '        } else if ((ST(h->take_state, TAKE_PENDING), 0)) {'), False)
run('claim-copy', change(source,
    '        !fms_take_change(h, TAKE_IDLE, TAKE_PREPARING)) return;',
    '        !(fms_take_change(h, TAKE_IDLE, TAKE_PREPARING) ||\n'
    '          fms_take_change(h, TAKE_CLAIMED, TAKE_PREPARING))) return;'), False)
run('swap-retry', change(source,
    '            fms_sleep_us(1);\n            continue;',
    '            fms_sleep_us(1);\n            break;'), False)
(work / 'fmspike-take.c').write_text(source)
