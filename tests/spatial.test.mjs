// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Run after tools/build_spatial.py; --oracle PATH adds actual native playback.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import fs from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {Worker as NodeWorker} from 'node:worker_threads';
import {runCLI} from '../web/runtime.mjs';
import {validateSpatial,createSpatialSampler,inspectSpatial} from '../web/spatial.mjs';

const helperBytes = await fs.readFile(new URL('../build/wasm/spatial.wasm',import.meta.url));
const libraryBytes = await fs.readFile(new URL('../build/wasm/library.wasm',import.meta.url));
const helper = await WebAssembly.compile(helperBytes), library = await WebAssembly.compile(libraryBytes);
const manifest = JSON.parse(await fs.readFile(new URL('../build/wasm/spatial.json',import.meta.url),'utf8'));
const engine = {...manifest,id:'test',library_sha256:manifest.library_sha256};
const fixture = name => fs.readFile(new URL('../tests/golden/'+name,import.meta.url),'utf8');
async function project(text) {
  const result = await runCLI(helper,['record.tl'],{'record.tl':text});
  assert.equal(result.code,0,result.stderr);
  return validateSpatial(JSON.parse(result.stdout));
}
const movement = await project(await fixture('space/move.tl'));

test('native projection preserves interrupted moves, jumps and exact initial defaults', () => {
  const sampler = createSpatialSampler(movement,library);
  assert.deepEqual(sampler.positionsAt(0),[{x:-1000,y:0,z:0},{x:0,y:1000,z:0}]);
  assert.equal(sampler.positionsAt(12031)[0].x,-1000);
  assert.equal(sampler.positionsAt(12032)[0].x,-1000);
  assert.equal(sampler.positionsAt(38400)[0].x,98);
  assert.deepEqual(sampler.positionsAt(62400)[0],{x:0,y:0,z:1000});
  assert.deepEqual(sampler.positionsAt(67200)[0],{x:500,y:500,z:0});
  assert(movement.paths.some(p=>p.sample===38400 && p.x===98 && p.pen===1));
  assert(movement.paths.some(p=>p.sample===62400 && p.pen===0));
  assert.deepEqual(sampler.positionsAt(Infinity),sampler.positionsAt(0));
});

test('mono has no invented positional model, while field keeps initial yaw and native timing',async()=>{
  assert.equal(await project(await fixture('44100/bell.tl')),null);
  const field = await project(await fixture('space/amb3-yaw.tl'));
  const sampler = createSpatialSampler(field,library);
  assert.equal(field.initialYaw,30000);
  assert.equal(sampler.yawAt(12031),30000);
  assert.equal(sampler.yawAt(12032),90000);
});

test('invalid dimensions, coordinates, ordering and ABI metadata are refused',()=>{
  const changes = [d=>d.voices=65,d=>d.block=48,d=>d.near=0,d=>d.frames=NaN,
    d=>d.paths[0].x=Infinity,d=>d.paths[0].voice=64,d=>d.paths[0].sample=1,
    d=>d.states[0][1]=64,d=>d.states[0][2]=16777217,d=>d.states=[],
    d=>d.layout.bytes=4,d=>d.layout.b0=d.layout.b1,d=>d.layout.b0=1,
    d=>d.paths=Array(20001).fill(d.paths[0]),d=>d.yaws=[{sample:1,yaw:0}]];
  for (const change of changes) {const value=structuredClone(movement);change(value);assert.throws(()=>validateSpatial(value));}
  const inspection=Object.fromEntries(['frames','voices','fs','channels','route'].map(key=>[key,String(movement[key])]));
  assert.equal(validateSpatial(movement,inspection),movement);
  for (const key of Object.keys(inspection)) assert.throws(()=>validateSpatial(movement,{...inspection,[key]:Number(inspection[key])+1}),/differs/);
  assert.throws(()=>validateSpatial(null,inspection));
});

test('companion checks both renderer byte identities and module bytes',async()=>{
  const text = await fixture('space/move.tl');
  const fetcher = async url => {
    const name = String(url).split('/').at(-1);
    const data = name==='spatial-view.json' ? JSON.stringify(manifest) : name==='spatial-view.wasm' ? helperBytes : libraryBytes;
    return new Response(data);
  };
  const result = await inspectSpatial(engine,text,'library.wasm',fetcher);
  assert.equal(result.spatial.frames,96000);
  assert.equal(createSpatialSampler(result.spatial,result.spatialModule).positionsAt(38400)[0].x,98);
  for (const key of ['browser_sha256','library_sha256'])
    await assert.rejects(inspectSpatial({...engine,[key]:'0'.repeat(64)},text,'library.wasm',fetcher),/renderer version/);
  await assert.rejects(inspectSpatial(engine,text,'library.wasm',async()=>new Response('',{status:404})),/unavailable/);
  await assert.rejects(inspectSpatial(engine,text,'library.wasm',async url=>String(url).endsWith('.wasm')?new Response(new Uint8Array([1])):fetcher(url)),/checksum/);
});

test('stalled optional fetch stops within a short deadline even when abort is ignored',async()=>{
  let signal;
  await assert.rejects(inspectSpatial(engine,'unused','library.wasm',async(_url,options)=>{
    signal=options.signal;return new Promise(()=>{});
  },20),/timed out/);
  assert.equal(signal.aborted,true);
});

test('optional spatial metadata never changes successful worker PCM or canonical TL',async()=>{
  const previousFetch=globalThis.fetch, previousSelf=globalThis.self, previousWorker=globalThis.Worker;
  const browserBytes=await fs.readFile(new URL('../build/wasm/browser.wasm',import.meta.url));
  const text=await fixture('space/move.tl');
  let received, available=true, stall=false;
  const children=[];
  const selected={...engine,identity:'a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2',
    browser_wasm:'engines/test/browser.wasm',library_wasm:'engines/test/library.wasm'};
  try {
    // Adapt only the browser worker host. The child imports the production
    // module and executes its real spatial-only dispatch and native helper.
    globalThis.Worker=class {
      constructor(url,options) {
        assert.equal(options.type,'module');
        this.thread=new NodeWorker(`
          const {parentPort,workerData}=require('node:worker_threads');
          globalThis.self={postMessage:value=>parentPort.postMessage(value)};
          globalThis.fetch=async url=>{
            const name=String(url).split('/').at(-1);
            if(name==='spatial-view.json') return workerData.available
              ? new Response(workerData.manifest) : new Response('',{status:404});
            return new Response(name==='library.wasm'?workerData.library:workerData.helper);
          };
          if(workerData.stall) while(true) {}
          import(workerData.entry).then(()=>parentPort.on('message',data=>self.onmessage({data})));
        `,{eval:true,workerData:{entry:String(url),available,stall,manifest:JSON.stringify(manifest),
          library:libraryBytes,helper:helperBytes}});
        children.push(this);
        this.thread.on('message',data=>this.onmessage?.({data}));
        this.thread.on('error',error=>this.onerror?.({message:error.message,preventDefault(){}}));
        this.thread.on('messageerror',()=>this.onmessageerror?.({}));
      }
      postMessage(data) {this.thread.postMessage(data);}
      terminate() {this.terminated=true;this.finished=this.thread.terminate();}
    };
    globalThis.self={postMessage:value=>{received=value;}};
    globalThis.fetch=async url=>{
      const name=String(url).split('/').at(-1);
      if(name==='spatial-view.json') return available ? new Response(JSON.stringify(manifest)) : new Response('',{status:404});
      return new Response(name==='browser.wasm'?browserBytes:name==='library.wasm'?libraryBytes:helperBytes);
    };
    await import('../web/worker.mjs');
    await self.onmessage({data:{recordText:text,engine:selected,mode:'listen'}});
    const withView=received;
    assert.equal(withView.verdict,'valid',withView.stderr);
    assert.equal(withView.spatial.frames,96000);
    assert(withView.spatialModule instanceof WebAssembly.Module);
    available=false;
    await self.onmessage({data:{recordText:text,engine:selected,mode:'listen'}});
    assert.equal(received.verdict,'valid',received.stderr);
    assert.equal(received.spatial,null);
    assert.match(received.spatialError,/unavailable/);
    assert.deepEqual(received.pcm,withView.pcm);
    assert.deepEqual(received.canonical,withView.canonical);
    assert.equal(received.pcmHash,withView.pcmHash);
    // A busy native-style child cannot service timers or messages. Its parent
    // must still return already rendered audio before the outer deadline.
    stall=true;
    await self.onmessage({data:{recordText:text,engine:selected,mode:'listen',spatialDeadlineMs:Date.now()+1000}});
    assert.equal(received.verdict,'valid',received.stderr);
    assert.equal(received.spatial,null);assert.match(received.spatialError,/timed out/);
    assert.deepEqual(received.pcm,withView.pcm);assert.deepEqual(received.canonical,withView.canonical);
    assert.equal(received.pcmHash,withView.pcmHash);
    assert.equal(children.length,3);assert(children.every(child=>child.terminated));
  } finally {
    globalThis.fetch=previousFetch;globalThis.self=previousSelf;
    if(previousWorker===undefined)delete globalThis.Worker;else globalThis.Worker=previousWorker;
    for(const child of children) {if(!child.terminated)child.terminate();await child.finished;}
  }
});

const oracleIndex = process.argv.indexOf('--oracle');
if (oracleIndex >= 0) test('native playback agrees at every block boundary and its neighbors',async()=>{
  let comparisons=0;
  for (const name of ['space/move.tl','space/pan.tl','space/amb3-yaw.tl','space/binaural/binaural1.tl',
      'fx/stereo-master.tl','fx/crush-stereo.tl','fx/stereo-44100.tl']) {
    const sampler=createSpatialSampler(await project(await fixture(name)),library);
    const result=spawnSync(process.argv[oracleIndex+1],[new URL('../tests/golden/'+name,import.meta.url).pathname],{encoding:'utf8',maxBuffer:4*1024*1024});
    assert.equal(result.status,0,result.stderr);
    for(const row of result.stdout.trim().split('\n')) {
      const [frame,voice,x,y,z,yaw]=row.split(' ').map(Number);
      assert.deepEqual(sampler.positionsAt(frame)[voice],{x,y,z},name+' at '+frame+' voice '+voice);
      assert.equal(sampler.yawAt(frame),yaw);comparisons++;
    }
  }
  console.log('Native position/yaw comparisons:',comparisons);
});
