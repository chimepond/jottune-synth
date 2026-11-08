#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Build the CLI and freestanding synth library with Zig's WASI toolchain."""
import os
import json
import re
from pathlib import Path
import shlex
import subprocess
from package_web import source_commit, digest, write_json, toolchain_notices, MODULES

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/wasm'
OUT.mkdir(parents=True, exist_ok=True)
revision = source_commit(ROOT)
cc = shlex.split(os.environ.get('ZIGCC', 'zig cc'))
def run(args):
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True)
# Ignored headers and generator binaries are not evidence of a source build.
# Rebuild both from source even when their timestamps look current.
run(['make', '-B', 'build/s12e10p1v1/fm_tables.h', 'build/s12e10p1v1/sp_binaural_tables.h'])
flags = ['-std=c99', '-ffp-contract=off', '-O2', '-Wall', '-Wextra', '-Wshadow', '-DFM_SINE_BITS=12',
         '-DFM_EXP_BITS=10', '-DFM_PHASE64=1', '-DFM_ENV64=1', '-Isrc/host',
         '-Ibuild/s12e10p1v1', '-I.', '-DSP_BINAURAL_H="build/s12e10p1v1/sp_binaural_tables.h"']
sources = ['src/host/fm_a1.c', 'src/gen/gen.c', 'src/space/space.c', 'src/fx/fx.c', 'src/space/field.c']
objects = []
for src in sources:
    out = OUT / ('wasi_' + Path(src).stem + '.o')
    run(cc + ['-target', 'wasm32-wasi'] + flags + ['-Werror', '-Wsign-conversion', '-c', src, '-o', out])
    objects.append(out)
run(cc + ['-target', 'wasm32-wasi'] + flags + ['-include', 'tools/wasm/compat.h', 'src/host/harness.c',
    'src/host/ref_double.c', 'src/host/fx_ref.c', 'src/host/field_ref.c',
    'tools/wasm/tmpfile.c', *objects, '-D_WASI_EMULATED_SIGNAL', '-lwasi-emulated-signal', '-lm', '-Wl,--strip-all',
    '-Wl,--max-memory=268435456', '-o', OUT / 'harness.wasm'])
# Export public declarations explicitly: Zig's WASM linker ignores -rdynamic.
exports = set()
for header in ['src/host/fm_a1.h', 'src/gen/gen.h', 'src/fx/fx.h', 'src/space/space.h', 'src/space/field.h']:
    text = re.sub(r'/\*[\s\S]*?\*/', '', (ROOT / header).read_text())
    exports.update(re.findall(r'\b((?:fm|gen|fx|sp)_[a-zA-Z0-9_]+)\s*\([^;{}]*\)\s*;', text))
export_flags = ['-Wl,--export=' + name for name in sorted(exports)]
run(cc + ['-target', 'wasm32-freestanding', '-nostdlib', '-ffreestanding',
    '-fno-builtin', '-DFP_MATH_FORCE_PORTABLE'] + flags + sources + ['tools/wasm/memory.c',
    '-Wl,--no-entry', '-Wl,--export=__heap_base', '-Wl,--export=__data_end', *export_flags, '-Wl,--strip-all', '-Wl,--max-memory=268435456',
    '-o', OUT / 'library.wasm'])
run(cc + ['-target', 'wasm32-wasi'] + flags + ['-include', 'tools/wasm/compat.h',
    'src/host/browser.c', 'src/host/ref_double.c', 'src/host/fx_ref.c', 'src/host/field_ref.c',
    'tools/wasm/tmpfile.c', *objects, '-D_WASI_EMULATED_SIGNAL', '-lwasi-emulated-signal',
    '-lm', '-Wl,--strip-all', '-Wl,--max-memory=268435456', '-o', OUT / 'browser.wasm'])
for name in ('harness.wasm', 'browser.wasm', 'library.wasm'):
    print(name, (OUT / name).stat().st_size, 'bytes')

version_command = cc[:-1] + ['version'] if cc[-1] == 'cc' else cc + ['--version']
version = subprocess.check_output(version_command, text=True).strip().splitlines()[0]
toolchain = 'zig cc ' + version
# Copy notices from the exact installed compiler distribution, not a network
# download or an unrelated archive. Zig changed env output from JSON to ZON.
env_command = cc[:-1] + ['env'] if cc[-1] == 'cc' else None
if not env_command:
    raise SystemExit('Use Zig cc to collect its matching toolchain notices.')
environment = subprocess.check_output(env_command, text=True)
try:
    zig_env = json.loads(environment)
except json.JSONDecodeError:
    zig_env = {key: json.loads(value) for key, value in
               re.findall(r'\.(lib_dir|zig_exe)\s*=\s*("(?:[^"\\]|\\.)*")', environment)}
lib = Path(zig_env['lib_dir'])
zig_license = Path(zig_env['zig_exe']).resolve().parent.parent / 'LICENSE'
notice_dir = OUT / 'notices'
notice_dir.mkdir(exist_ok=True)
try:
    notices = toolchain_notices(lib, zig_license)
except (ValueError, OSError) as error:
    raise SystemExit(str(error))
for name, text in notices.items():
    (notice_dir / name).write_text(text)

(OUT / 'toolchain.txt').write_text(toolchain + '\n')
write_json(OUT / 'build.json', {
    'source_commit': revision if source_commit(ROOT) == revision else None,
    'toolchain': toolchain,
    'artifacts': {name: digest(OUT / name) for name in MODULES},
})
