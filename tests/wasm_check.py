#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Compare native CLI bytes with independent V8/WASI and wasmtime executions."""
import argparse
from pathlib import Path
import json
import subprocess
import tempfile
import wasmtime
from music_compat import music_cases, expected_play
ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--quick', action='store_true')
p.add_argument('--start', type=int, default=0)
p.add_argument('--exclude-prefix', default='')
p.add_argument('--only-prefix', default='')
p.add_argument('--shard', type=int, default=0)
p.add_argument('--shards', type=int, default=1)
args = p.parse_args()
if args.start < 0 or args.shards < 1 or not 0 <= args.shard < args.shards:
    p.error("require start >= 0 and 0 <= shard < shards")
jobs = []
for path in sorted((ROOT/'tests/golden').rglob('*.tl')):
    if 'soak' in path.parts:
        continue
    # This old refusal fixture became valid when field effects were enabled;
    # native.sh already checks it as a positive case. It has no pinned hash.
    kind = 'draft' if path == ROOT/'tests/golden/fx/bad/space-field.tl' else 'refused' if 'bad' in path.parts else 'record'
    jobs.append(([path], kind))
print('The two one-hour generator streaming fixtures are excluded from full-buffer browser replay.', flush=True)
# Every standalone retained record, and each pool as one job; stream fragments
# are inputs of their session and never treated as independent records.
jobs.extend(music_cases(ROOT))
if args.exclude_prefix:
    jobs = [job for job in jobs if not job[0][0].relative_to(ROOT).as_posix().startswith(args.exclude_prefix)]
if args.only_prefix:
    jobs = [job for job in jobs if job[0][0].relative_to(ROOT).as_posix().startswith(args.only_prefix)]
if args.quick:
    paths = ('48000/sine.tl', 'fx/chain.tl', 'space/bad/block-48.tl')
    jobs = [([ROOT/'tests/golden'/path], 'refused' if '/bad/' in path else 'record') for path in paths]
selected = [(i, job) for i, job in enumerate(jobs) if i >= args.start and i % args.shards == args.shard]
if not selected:
    p.error('selection contains no regression cases')
engine = wasmtime.Engine()
module = wasmtime.Module.from_file(engine, str(ROOT / 'build/wasm/harness.wasm'))
linker = wasmtime.Linker(engine)
linker.define_wasi()
node = subprocess.Popen(['node', '--expose-gc', str(ROOT/'tests/wasm_runner.mjs'), str(ROOT/'build/wasm/harness.wasm')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
counts = {name:dict(identical=0, mismatch_alike=0, refused_alike=0, different=0, crashed=0) for name in ('wasmtime','v8')}
compatibility_failures = 0
def projection(code, stdout, stderr):
    return code, '\n'.join(line for line in stdout.splitlines() if line.startswith(('PLAY ', 'METRICS '))), stderr
with tempfile.TemporaryDirectory(prefix='fm-wasm-check-') as folder:
    folder = Path(folder)
    for completed, (index, (paths, kind)) in enumerate(selected, start=1):
        files = {f'record{i}.tl':path.read_text() for i,path in enumerate(paths)}
        for name, text in files.items(): (folder/name).write_text(text)
        cli = ['--play', *files, '--metrics']
        native = subprocess.run([str(ROOT/'build/s12e10p1v1/harness'), *cli],cwd=folder,capture_output=True,text=True)
        expected = projection(native.returncode,native.stdout,native.stderr)
        try:
            if kind == 'refused':
                assert native.returncode in (1, 3), 'deliberate negative fixture was accepted or failed as a host error'
            else:
                expected_play(native.returncode, native.stdout, kind)
        except AssertionError as error:
            compatibility_failures += 1
            print('COMPATIBILITY FAIL', paths[0].relative_to(ROOT), str(error), flush=True)
        config = wasmtime.WasiConfig();config.argv=['harness',*cli];config.preopen_dir(str(folder),'.')
        config.stdout_file=str(folder/'stdout');config.stderr_file=str(folder/'stderr')
        store=wasmtime.Store(engine);store.set_wasi(config)
        code=0
        try: linker.instantiate(store,module).exports(store)['_start'](store)
        except wasmtime.ExitTrap as error: code=error.code
        except wasmtime.Trap as error: code=-999
        outputs={'wasmtime':projection(code,(folder/'stdout').read_text(),(folder/'stderr').read_text())}
        store.close()
        node.stdin.write(json.dumps(dict(args=cli,files=files))+'\n');node.stdin.flush()
        result=json.loads(node.stdout.readline());outputs['v8']=projection(result['code'],result['stdout'],result['stderr'])
        for name, actual in outputs.items():
            category=('crashed' if actual[0]<0 else 'different' if actual!=expected
                      else 'mismatch_alike' if kind=='mismatch' else 'identical' if actual[1] else 'refused_alike')
            counts[name][category]+=1
            if category in ('different','crashed'):
                print(name, category, paths[0].relative_to(ROOT), 'native=',expected,'wasm=',actual,flush=True)
        if completed%10==0:print('Compared',completed,'/',len(selected),'shard',args.shard,'job',index,counts,flush=True)
node.stdin.close()
node_status = node.wait()
if node_status:
    compatibility_failures += 1
    print('V8 runner failed with exit', node_status)
print(json.dumps(dict(records=len(selected),start=args.start,shard=args.shard,shards=args.shards,
                     compatibility_failures=compatibility_failures,results=counts),indent=2))
raise SystemExit(bool(compatibility_failures) or any(row['different'] or row['crashed'] for row in counts.values()))
