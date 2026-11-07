#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Build a read-only spatial companion, bound to freshly rebuilt renderer bytes.

This creates build/wasm/spatial.wasm and spatial.json. Packaging may copy those
to web/spatial-view.wasm and web/spatial-view.json without changing any kept
engine archive. Unsupported archived engines retain their audio behavior.
"""
import os
from pathlib import Path
import shlex
import subprocess
import sys
from package_web import digest, write_json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/wasm'


def main():
    # Rebuilding both inputs proves parser AND spatial ABI compatibility. The
    # display companion is never selected by the coarser engine identity text.
    subprocess.run([sys.executable, ROOT / 'tools/build_wasm.py'], cwd=ROOT, check=True)
    flags = ['-std=c99', '-ffp-contract=off', '-O2', '-Wall', '-Wextra', '-Wshadow',
             '-DFM_SINE_BITS=12', '-DFM_EXP_BITS=10', '-DFM_PHASE64=1', '-DFM_ENV64=1',
             '-Isrc/host', '-Ibuild/s12e10p1v1', '-I.',
             '-DSP_BINAURAL_H="build/s12e10p1v1/sp_binaural_tables.h"']
    objects = [OUT / ('wasi_' + stem + '.o') for stem in ('fm_a1', 'gen', 'space', 'fx', 'field')]
    command = shlex.split(os.environ.get('ZIGCC', 'zig cc'))
    subprocess.run(command + ['-target', 'wasm32-wasi'] + flags + [
        '-D_GNU_SOURCE', '-include', 'tools/wasm/compat.h', 'tools/wasm/spatial.c',
        'src/host/ref_double.c', 'src/host/fx_ref.c', 'src/host/field_ref.c',
        'tools/wasm/tmpfile.c', *map(str, objects), '-D_WASI_EMULATED_SIGNAL',
        '-lwasi-emulated-signal', '-lm', '-Wl,--strip-all', '-Wl,--max-memory=268435456',
        '-o', str(OUT / 'spatial.wasm')], cwd=ROOT, check=True)
    write_json(OUT / 'spatial.json', {'format': 1, 'wasm_sha256': digest(OUT / 'spatial.wasm'),
               'browser_sha256': digest(OUT / 'browser.wasm'),
               'library_sha256': digest(OUT / 'library.wasm')})
    print('Spatial companion:', (OUT / 'spatial.wasm').stat().st_size, 'bytes')


if __name__ == '__main__':
    main()
