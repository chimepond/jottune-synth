#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Build an optional analysis companion without replacing any kept renderer.

Default builds refresh the ordinary Wasm outputs first. --reuse-objects uses an
existing build, relinks the ordinary browser and requires byte equality before
using those objects. --source-root supports a source-only worktree whose build
inputs live in another checkout; that checkout is read-only in reuse mode.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--out', type=Path, default=ROOT / 'build/wasm')
    parser.add_argument('--reuse-objects', action='store_true')
    args = parser.parse_args()
    root, out = args.source_root.resolve(), args.out.resolve()
    if not args.reuse_objects:
        subprocess.run([sys.executable, root / 'tools/build_wasm.py'], cwd=root, check=True)
    out.mkdir(parents=True, exist_ok=True)
    build = root / 'build/wasm'
    flags = ['-std=c99', '-O2', '-Wall', '-Wextra', '-Wshadow',
             '-DFM_SINE_BITS=12', '-DFM_EXP_BITS=10', '-DFM_PHASE64=1', '-DFM_ENV64=1',
             '-Isrc/host', '-Ibuild/s12e10p1v1', '-I.',
             '-DSP_BINAURAL_H="build/s12e10p1v1/sp_binaural_tables.h"']
    objects = [build / ('wasi_' + stem + '.o') for stem in ('fm_a1', 'gen', 'space', 'fx', 'field')]
    command = shlex.split(os.environ.get('ZIGCC', 'zig cc')) + ['-target', 'wasm32-wasi'] + flags
    shared = ['src/host/ref_double.c', 'src/host/fx_ref.c', 'src/host/field_ref.c',
              'tools/wasm/tmpfile.c', *map(str, objects), '-D_WASI_EMULATED_SIGNAL',
              '-lwasi-emulated-signal', '-lm', '-Wl,--strip-all', '-Wl,--max-memory=268435456']
    with tempfile.TemporaryDirectory(prefix='analysis-link-', dir=out) as scratch:
        rebuilt = Path(scratch) / 'browser.wasm'
        subprocess.run(command + ['-include', 'tools/wasm/compat.h', 'src/host/browser.c', *shared,
                                  '-o', str(rebuilt)], cwd=root, check=True)
        if digest(rebuilt) != digest(build / 'browser.wasm'):
            raise SystemExit('Reused objects/source do not reproduce the selected browser renderer; rebuild first.')
    subprocess.run(command + ['-include', 'tools/wasm/compat.h', str(ROOT / 'tools/wasm/analysis.c'),
                              *shared, '-o', str(out / 'analysis.wasm')], cwd=root, check=True)
    manifest = {'format': 1, 'wasm_sha256': digest(out / 'analysis.wasm'),
                'browser_sha256': digest(build / 'browser.wasm'),
                'library_sha256': digest(build / 'library.wasm')}
    (out / 'analysis.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Analysis companion:', (out / 'analysis.wasm').stat().st_size, 'bytes')


if __name__ == '__main__':
    main()
