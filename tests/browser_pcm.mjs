// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Small Node/Wasm integration check; does not establish browser UI acceptance.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {runCLI, row} from '../web/runtime.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const module=await WebAssembly.compile(await fs.readFile(path.join(root,'build/wasm/browser.wasm')));
const library=await WebAssembly.compile(await fs.readFile(path.join(root,'build/wasm/library.wasm')));
assert.equal(WebAssembly.Module.imports(library).length,0);
const exports=new Set(WebAssembly.Module.exports(library).map(item=>item.name));
for(const name of ['memory','fm_voice_init','fm_render_i16','fm_golden_hash','__heap_base','__data_end'])assert(exports.has(name),name);
const files=['tests/golden/fx/chain.tl','tests/golden/space/pan.tl','tests/golden/space/amb3-yaw.tl','tests/golden/gen/genambient-48000.tl'];
const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'fm-browser-pcm-'));
try {
  for(const file of files){
    const text=await fs.readFile(path.join(root,file),'utf8');
    await fs.writeFile(path.join(temporary,'record.tl'),text);
    const native=spawnSync(path.join(root,'build/s12e10p1v1/harness'),['--play','record.tl','--wav','native.wav','--merged','native.tl','--metrics'],{cwd:temporary,encoding:'utf8'});
    assert.equal(native.status,0,native.stderr);
    const result=await runCLI(module,['--play','record.tl','--merged','canonical.tl','--metrics'],{'record.tl':text});
    assert.equal(result.code,native.status);assert.equal(result.stdout,native.stdout);assert.equal(result.stderr,native.stderr);
    const wav=await fs.readFile(path.join(temporary,'native.wav'));
    assert.deepEqual(Buffer.from(result.pcm.buffer,result.pcm.byteOffset,result.pcm.byteLength),wav.subarray(44),file+' PCM memory');
    assert.deepEqual(Buffer.from(result.canonical),await fs.readFile(path.join(temporary,'native.tl')),file+' canonical text');
    const inspected=await runCLI(module,['--events','record.tl'],{'record.tl':text});assert.equal(inspected.code,0,inspected.stderr);
    const info=row(inspected.stdout.split('\n').find(line=>line.startsWith('INSPECT ')));
    assert.equal(Number(info.pcm_bytes),result.pcm.byteLength);
    assert.equal(Number(info.channels),result.channels);
    if(file.includes('amb3'))assert.equal(info.route,'2');
    if(file.includes('/gen/'))assert.match(inspected.stdout,/^GEN_EVENT /m);
    assert.equal(info.hash_present,'1');
    assert.equal(BigInt(info.render_bytes_lower_bound)-BigInt(info.once_render_bytes_lower_bound),BigInt(info.pcm_bytes));
    const once=await runCLI(module,['--play-once','record.tl','--merged','canonical.tl','--metrics'],{'record.tl':text});
    assert.equal(once.code,0,once.stderr);
    assert.equal(row(once.stdout.split('\n').find(line=>line.startsWith('PLAY '))).block_indep,'unchecked');
    assert.equal(once.stdout.replace('block_indep=unchecked','block_indep=yes'),result.stdout);
    assert.equal(once.stderr,result.stderr);
    assert.deepEqual(once.pcm,result.pcm,file+' listener/verify PCM');
    assert.deepEqual(once.canonical,result.canonical,file+' listener/verify canonical text');
    console.log('PASS',file,'native/listener/verify PCM, canonical bytes and parsed event listing');
    globalThis.gc?.();
  }
} finally {await fs.rm(temporary,{recursive:true,force:true});}
console.log('BROWSER_PCM 4/4 records in listener and verify modes; library imports=0 and allocation boundaries exported');
