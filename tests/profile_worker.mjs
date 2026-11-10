// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Focused worker orchestration contracts; native behavior is keys_profile.py.
// Add --integration to exercise the current packaged Worker/Wasm against the
// native harness: node --expose-gc tests/profile_worker.mjs --integration
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {parseDocument,preserveDocument,bodySha256,documentSha256} from '../web/tl-document.mjs';
import {readFileSync} from 'node:fs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import {options, checkRecord, checkInspection, verification} from '../web/policy.mjs';
import {row, verdict} from '../web/runtime.mjs';
import {validateSpatial} from '../web/spatial.mjs';
import {compileScore,selectScoreStreams} from '../web/authoring.mjs';

const bytes = text => new TextEncoder().encode(text);
const digest = async data => Buffer.from(await webcrypto.subtle.digest('SHA-256', data)).toString('hex');
const original = 'original fixture\n', canonicalSource = 'canonical original fixture\n', derivedText = 'canonical derived fixture\n';
const profile = {halfWidth:60,mirror:false};
const inspect = (extra = '') => 'INSPECT length_ns=500000000 pcm_bytes=96000 render_bytes_lower_bound=192000 once_render_bytes_lower_bound=96000 hash_present=1 keys_profile=1 ' + extra + '\n';
const summary = 'PROFILE half_width=60 mirror=0 level_policy=strict target_met=yes peak_dbfs=-1.51 clipped=0 carrier_delta_db10=-17 passes=2 limited_by_carrier_bounds=0 legal_low_delta_db10=-43 legal_high_delta_db10=1157 last_requested_delta_db10=-17 last_applied_delta_db10=-17\n';
const sourceSummary = 'PROFILE_SOURCE hash=123456789abcdef0 hash_present=1 match=yes block_indep=yes\n';
const output = (stdout = '', canonical = '', code = 0, stderr = '') => ({code,stdout,stderr,canonical:bytes(canonical),pcm:null});
const successes = () => [
  output(inspect()),
  {...output('PLAY hash=123456789abcdef0 match=yes block_indep=unchecked\n', canonicalSource), pcm:new Int16Array([1,2])},
  output(sourceSummary + summary, derivedText),
  output(inspect()),
  {...output('PLAY hash=fedcba9876543210 match=yes block_indep=yes\nMETRICS peak=27540\n', derivedText), pcm:new Int16Array([3,4]),sampleRate:48000,channels:2},
  output('EVENT time_ns=0 kind=on\n'),
];

async function execute(responses, request = {profile}, configure = {}) {
  const calls = [], messages = [], workers = [], fakeModule = bytes('module fixture');
  const self = {postMessage: value => messages.push(value)};
  const engine = {id:'dev-test', identity:'test',browser_wasm:'engines/dev-test/browser.wasm',browser_sha256:await digest(fakeModule),
    library_wasm:'engines/dev-test/library.wasm'};
  const source = readFileSync(new URL('../web/worker.mjs', import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '')
    .replaceAll('import.meta.url', JSON.stringify('https://player.invalid/prefix/web/worker.mjs'));
  const context = {parseDocument,preserveDocument,bodySha256,documentSha256,self,options,checkRecord,checkInspection,verification,row,verdict,
    compileScore,selectScoreStreams,validateSpatial,inspectSpatial:async()=>({spatial:null,spatialModule:null,spatialError:null}),
    TextEncoder,TextDecoder,URL,crypto:webcrypto, Uint8Array,setTimeout,clearTimeout,
    WebAssembly:{Module:WebAssembly.Module,compile: async () => ({testModule:true})},
    fetch:async url => {
      assert.equal(url, engine.browser_wasm);
      return {ok:true,arrayBuffer:async () => fakeModule.buffer};
    },
    runCLI:async (module, args, files) => {
      calls.push({args:[...args],files});
      assert.equal(module.testModule, true);
      assert(responses.length, 'Unexpected native command: ' + args.join(' '));
      return responses.shift();
    }, ...configure};
  context.Worker ??= class {
    constructor(url,options) {
      assert.equal(String(url),'https://player.invalid/prefix/web/worker.mjs');
      assert.equal(options.type,'module');
      this.terminated=false;workers.push(this);
    }
    postMessage(data) {
      this.request=data;
      if(configure.workerBehavior) return configure.workerBehavior(this,data);
      const child={postMessage:value=>{if(!this.terminated)this.onmessage({data:value});}};
      vm.runInNewContext(source,{...context,self:child});
      void child.onmessage({data});
    }
    terminate() {this.terminated=true;}
  };
  vm.runInNewContext(source, context);
  await self.onmessage({data:{recordText:original,engine,...request}});
  assert.equal(messages.length, 1);
  return {result:messages[0],calls,workers};
}

test('profile options validate width, mirror and explicit gain policy without changing defaults', () => {
  assert.deepEqual(options(), {mode:'verify',release:undefined});
  assert.deepEqual(options({profile}), {mode:'verify',release:undefined,profile:{...profile,levelPolicy:'strict'}});
  for (const value of [null,{}, {...profile,halfWidth:55},{...profile,mirror:'yes'}, {...profile,extra:true}, {...profile,levelPolicy:'auto'}])
    assert.throws(() => options({profile:value}));
  assert.equal(options({profile:{...profile,levelPolicy:'carrier-bound'}}).profile.levelPolicy,'carrier-bound');
});

test('song previews inspect native notes without rendering, canonicalizing or creating playback workers', async () => {
  const {result,calls,workers} = await execute([output(inspect()),output('EVENT time_ns=0 kind=on\nGEN_EVENT time_ns=1 kind=off\n')],{operation:'inspect'});
  assert.deepEqual(calls.map(call=>call.args[0]),['--inspect','--events']);
  assert.equal(result.type,'inspection'); assert.equal(result.verdict,'valid');
  assert.equal(result.events.length,2); assert.equal(result.events[1].kind,'off');
  assert.equal(result.sourceRecordSha256,await digest(bytes(original)));
  for(const key of ['pcm','canonical','verified','pcmHash']) assert.equal(result[key],undefined);
  assert.equal(workers.length,0);
});

test('worker exposes portable compatibility versions and explicitly inferred legacy status',async()=>{
  const portable='# jottune-tl 1 1\n# {"title":"Versioned fixture","author":"Composer"}\n# end-jottune-tl\n'+original;
  const rendered=await execute([output(inspect()),
    {...output('PLAY hash=123456789abcdef0 match=yes block_indep=yes\n',canonicalSource),pcm:new Int16Array([1,2]),sampleRate:48000,channels:1},
    output('EVENT time_ns=0 kind=on\n')],{recordText:portable,mode:'verify',spatialView:false});
  assert.equal(rendered.result.tlVersion,1);assert.equal(rendered.result.engineCompatibilityVersion,1);
  assert.equal(rendered.result.documentHeader,'present');assert.equal(rendered.result.metadata.title,'Versioned fixture');
  assert.equal(parseDocument(rendered.result.canonical).header,parseDocument(portable).header);
  assert.ok(rendered.calls.every(call=>!parseDocument(call.files['record.tl']).header),'Native receives body-only TL');
  const inspected=await execute([output(inspect()),output('EVENT time_ns=0 kind=on\n')],{operation:'inspect',recordText:portable});
  assert.equal(inspected.result.tlVersion,1);assert.equal(inspected.result.engineCompatibilityVersion,1);
  assert.equal(inspected.result.documentHeader,'present');assert.equal(inspected.result.metadata.author,'Composer');
  const legacy=await execute([output(inspect()),output('EVENT time_ns=0 kind=on\n')],{operation:'inspect'});
  assert.equal(legacy.result.tlVersion,1);assert.equal(legacy.result.engineCompatibilityVersion,1);
  assert.equal(legacy.result.documentHeader,'legacy-headerless');
});

test('note previews refuse invalid notes, excessive events and profile mutation', async () => {
  const invalid=await execute([output(inspect()),output('', '', 2, 'Invalid events')],{operation:'inspect'});
  assert.equal(invalid.result.verdict,'refused'); assert.equal(invalid.result.events,undefined);
  const large=await execute([output(inspect()),output('EVENT time_ns=0 kind=on\n'.repeat(20001))],{operation:'inspect'});
  assert.equal(large.result.verdict,'refused'); assert.match(large.result.stderr,/20,000-event/);
  const mutated=await execute([],{operation:'inspect',profile});
  assert.equal(mutated.calls.length,0); assert.match(mutated.result.stderr,/cannot apply/);
});

test('score selection inspects original and selected native TL without rendering PCM', async () => {
  const score = {version:1,fs:48000,clock:{bars:1,meter:[{bar:1,quarters:4}],tempo:[{bar:1,bpm:120}],ramp:'step',tail_ns:0},
    declarations:'recipe tone flute\n',streams:['lead','bass'],voices:['tone','tone'],events:[
      {kind:'note',at:0,duration:1,stream:'bass',voice:1,pitch:{note:'A2'},velocity_q15:20000},
      {kind:'note',at:0,duration:1,stream:'lead',voice:0,pitch:{note:'A4'},velocity_q15:20000},
      {kind:'pos',at:0,stream:'bass',voice:0,x_mm:-500,y_mm:1000,z_mm:0}]};
  const input = JSON.stringify(score), request = {operation:'compose-selection',score,selection:{soloStreams:['lead']}};
  const {result,calls} = await execute([output(inspect()),output(inspect())],request);
  assert.equal(result.verdict,'valid');assert.equal(result.type,'composition');
  assert.equal(result.selection.nativeValidation,'original_and_selection_inspected');
  assert.equal(result.coverage.verification,'unverified_draft');assert.equal(result.pcm,undefined);
  assert.deepEqual(calls.map(call=>call.args[0]),['--inspect','--inspect']);
  assert.match(calls[0].files['record.tl'],/bass 1 on/);assert.doesNotMatch(calls[1].files['record.tl'],/bass 1 on/);
  assert.match(result.draftText,/bass 0 pos -500 1000 0/);
  assert.equal(result.sourceMap.find(row=>row.role==='on').pointer,'/events/1');
  assert.equal(JSON.stringify(score),input);
  const refused=await execute([output('', '', 2, 'Invalid original event')],request);
  assert.equal(refused.result.verdict,'refused');assert.equal(refused.calls.length,1);assert.equal(refused.result.draftText,undefined);
  const selectedRefused=await execute([output(inspect()),output('', '', 2, 'Invalid selected draft')],request);
  assert.equal(selectedRefused.result.verdict,'refused');assert.equal(selectedRefused.result.draftText,undefined);
});

test('profile verifies original identity separately then fully verifies the standalone derivative', async () => {
  const {result,calls} = await execute(successes(), {profile,mode:'listen'});
  assert.equal(result.verdict,'valid');
  assert.equal(result.mode,'verify');
  assert.equal(result.verified,true);
  assert.equal(result.sourceRecordSha256,await digest(bytes(canonicalSource)));
  assert.equal(result.recordSha256,await digest(bytes(derivedText)));
  assert.notEqual(result.sourceRecordSha256,result.recordSha256);
  assert.equal(result.profile.sourceMatched,true);
  assert.equal(result.profile.targetMet,true);
  assert.deepEqual(calls.map(call => call.args[0]), ['--inspect','--play-once','--keys-profile','--inspect','--play','--events']);
  assert(calls.slice(0,3).every(call => call.files['record.tl'] === original));
  assert(calls.slice(3).every(call => call.files['record.tl'] === derivedText));
  assert.deepEqual(calls[2].args,['--keys-profile','60','--merged','canonical.tl','--','record.tl']);
  assert.equal(result.pcm[0],3);
});

test('explicit quiet carrier-bound mode returns limiting carrier metadata', async () => {
  const responses = successes();
  responses[2].stdout = sourceSummary + summary.replace('mirror=0','mirror=1').replace('level_policy=strict','level_policy=carrier-bound')
    .replace('target_met=yes','target_met=no').replace('peak_dbfs=-1.51','peak_dbfs=-22.50')
    .replace('limited_by_carrier_bounds=0','limited_by_carrier_bounds=1').replace('legal_low_delta_db10=-43','legal_low_delta_db10=0')
    + 'PROFILE_LIMIT patch=quiet operator=0 level_db10=0\n';
  const {result,calls} = await execute(responses,{profile:{...profile,mirror:true,levelPolicy:'carrier-bound'}});
  assert.equal(result.verdict,'valid');
  assert.equal(result.profile.targetMet,false);
  assert.equal(result.profile.limitedByCarrierBounds,true);
  assert.equal(result.profile.legalDeltaDb10[0],0);
  assert.equal(result.profile.limitingCarriers[0].patch,'quiet');
  assert.equal(result.profile.limitingCarriers[0].operator,0);
  assert(calls[2].args.includes('--keys-mirror') && calls[2].args.includes('--keys-carrier-bound'));
});

test('old renderer refuses a profile without retrying a different build', async () => {
  const {result,calls} = await execute([output(inspect().replace('keys_profile=1','keys_profile=0'))]);
  assert.equal(result.verdict,'refused');
  assert.match(result.stderr,/does not support/);
  assert.equal(calls.length,1);
});

test('source mismatch exposes neither transformed output nor playable PCM', async () => {
  const responses = successes().slice(0,2);
  responses[1].code=3;
  responses[1].stdout='PLAY match=NO\n';
  const {result,calls} = await execute(responses);
  assert.equal(result.verdict,'mismatch');
  assert.equal(result.canonical.length,0);
  assert.equal(result.pcm,null);
  assert.equal(calls.length,2);
});

test('native projection refusal cannot accidentally return its original canonical bytes', async () => {
  const responses = successes().slice(0,3);
  responses[2]=output(sourceSummary,canonicalSource,1,'carrier shift exceeds 0..1200');
  const {result,calls} = await execute(responses);
  assert.equal(result.verdict,'refused');
  assert.equal(result.canonical.length,0);
  assert.equal(result.pcm,null);
  assert.equal(calls.length,3);
});

test('unhashed original is described honestly although the derived record has its own verified hash', async () => {
  const responses = successes();
  responses[0].stdout=inspect().replace('hash_present=1','hash_present=0');
  responses[2].stdout=responses[2].stdout.replace('hash_present=1 match=yes','hash_present=0 match=unchecked');
  const {result} = await execute(responses);
  assert.equal(result.verified,true);
  assert.equal(result.profile.sourceHashPresent,false);
  assert.equal(result.profile.sourceMatched,null);
  assert.equal(result.profile.sourceBlockIndependent,true);
});

test('source and derived admission limits still apply and block later native commands', async () => {
  const source = await execute([output(inspect('length_ns=301000000000'))]);
  assert.equal(source.result.verdict,'refused');
  assert.match(source.result.stderr,/300-second/);
  assert.equal(source.calls.length,1);
  const responses=successes().slice(0,4);
  responses[3].stdout=inspect('render_bytes_lower_bound=268435456');
  const derived=await execute(responses);
  assert.equal(derived.result.verdict,'refused');
  assert.match(derived.result.stderr,/Too large/);
  assert.equal(derived.calls.length,4);
  const oversize=successes().slice(0,3);
  oversize[2].canonical=bytes('x'.repeat(1000001));
  const large=await execute(oversize);
  assert.equal(large.result.verdict,'refused');
  assert.match(large.result.stderr,/1 MB/);
});

test('missing capability data, inconsistent profile summaries and missing derived hash refuse', async () => {
  for (const change of [responses => {responses[2].stdout=sourceSummary;},
      responses => {responses[2].stdout=sourceSummary+summary.replace('half_width=60','half_width=30');},
      responses => {responses[2].stdout=sourceSummary+summary.replace('target_met=yes','target_met=no');},
      responses => {responses[3].stdout=inspect().replace('hash_present=1','hash_present=0');}]) {
    const responses=successes(); change(responses);
    const {result,calls}=await execute(responses);
    assert.equal(result.verdict,'refused');
    assert(calls.length<=4);
  }
});

test('ordinary listen continues using one-pass playback without profile transformation', async () => {
  const all=successes();
  all[4].stdout=all[4].stdout.replace('block_indep=yes','block_indep=unchecked');
  const {result,calls}=await execute([all[0],all[4],all[5]],{mode:'listen'});
  assert.equal(result.mode,'listen');
  assert.equal(result.verified,false);
  assert.equal(result.verification,'checksum-matched');
  assert.equal(result.profile,undefined);
  assert.deepEqual(calls.map(call=>call.args[0]),['--inspect','--play-once','--events']);
});

test('wrong or corrupt optional spatial metadata preserves valid PCM and canonical TL',async()=>{
  const spatial={format:1,block:64,near:100,frames:24000,voices:1,fs:48000,channels:2,route:0,initialYaw:0,
    layout:{bytes:48,b0:24,b1:28},paths:[{voice:0,sample:0,pen:0,x:0,y:1000,z:0}],
    states:[[0,0,0,1000,0,0,1000,0,0,0]],yaws:[]};
  for(const change of [value=>value.frames++,value=>value.voices++,value=>value.paths[0].x=NaN,value=>value.layout.bytes=1]) {
    const all=successes(), invalid=structuredClone(spatial);change(invalid);
    all[0].stdout=inspect('frames=24000 voices=1 fs=48000 channels=2 route=0');
    const {result}=await execute([all[0],all[4],all[5]],{mode:'listen'},
      {inspectSpatial:async()=>({spatial:invalid,spatialModule:{},spatialError:null})});
    assert.equal(result.verdict,'valid');assert.equal(result.spatial,null);assert(result.spatialError);
    assert.deepEqual(result.pcm,new Int16Array([3,4]));assert.deepEqual(result.canonical,bytes(derivedText));
  }
});

test('exhausted optional view budget immediately returns valid rendered audio',async()=>{
  const all=successes();
  const {result,workers}=await execute([all[0],all[4],all[5]],{mode:'listen',spatialDeadlineMs:0},
    {inspectSpatial:async()=>{assert.fail('Expired view must not start a companion request.');}});
  assert.equal(result.verdict,'valid');assert.equal(result.spatial,null);
  assert.match(result.spatialError,/budget exhausted/);assert.deepEqual(result.pcm,new Int16Array([3,4]));
  assert.equal(workers.length,0);
});

test('a successful isolated view uses the derived TL and terminates its child',async()=>{
  const spatial={format:1,block:64,near:100,frames:24000,voices:1,fs:48000,channels:2,route:0,initialYaw:0,
    layout:{bytes:48,b0:24,b1:28},paths:[{voice:0,sample:0,pen:0,x:0,y:1000,z:0}],
    states:[[0,0,0,1000,0,0,1000,0,0,0]],yaws:[]};
  const module=await WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0]));
  const all=successes();all[3].stdout=inspect('frames=24000 voices=1 fs=48000 channels=2 route=0');
  let projected;
  const {result,workers}=await execute(all,{profile},{inspectSpatial:async(engine,text,url)=>{
    projected=text;assert.equal(String(url),'https://player.invalid/prefix/web/engines/dev-test/library.wasm');
    return {spatial,spatialModule:module,spatialError:null};
  }});
  assert.equal(result.verdict,'valid');assert.equal(projected,derivedText);
  assert.equal(result.spatial,spatial);assert.equal(result.spatialModule,module);
  assert.equal(workers.length,1);assert.equal(workers[0].terminated,true);
  assert.equal(workers[0].request.operation,'spatial-only');
  assert.deepEqual(result.pcm,new Int16Array([3,4]));assert.deepEqual(result.canonical,bytes(derivedText));
});

test('stalled child is terminated inside the remaining render budget without losing audio',async()=>{
  const all=successes(), delays=[];
  const {result,workers}=await execute([all[0],all[4],all[5]],{mode:'listen',spatialDeadlineMs:Date.now()+1000},
    {workerBehavior:()=>{},setTimeout:(callback,delay)=>{delays.push(delay);return setTimeout(callback,5);}});
  assert.equal(result.verdict,'valid');assert.match(result.spatialError,/timed out/);
  assert.equal(workers.length,1);assert.equal(workers[0].terminated,true);
  assert(delays[0]>0 && delays[0]<=750,'Parent must reserve time for returning audio.');
  assert.deepEqual(result.pcm,new Int16Array([3,4]));assert.deepEqual(result.canonical,bytes(derivedText));
  workers[0].onmessage({data:{type:'spatial-result',projection:{spatial:null,spatialModule:null,spatialError:null}}});
  assert.match(result.spatialError,/timed out/,'Late child result cannot replace the audio result.');
});

test('worker setup, runtime, clone and protocol failures preserve successful audio',async()=>{
  for(const configure of [
    {Worker:class{constructor(){throw Error('Worker unavailable.');}}},
    {workerBehavior:worker=>worker.onerror({preventDefault(){}})},
    {workerBehavior:worker=>worker.onmessageerror({})},
    {workerBehavior:()=>{throw Error('Cannot clone request.');}},
    {workerBehavior:worker=>worker.onmessage({data:{type:'unknown'}})},
    {workerBehavior:worker=>worker.onmessage({data:{type:'spatial-result',projection:null}})},
  ]) {
    const all=successes();
    const {result,workers}=await execute([all[0],all[4],all[5]],{mode:'listen'},configure);
    assert.equal(result.verdict,'valid');assert.equal(result.spatial,null);assert(result.spatialError);
    assert(workers.every(worker=>worker.terminated));
    assert.deepEqual(result.pcm,new Int16Array([3,4]));assert.deepEqual(result.canonical,bytes(derivedText));
  }
});

if (process.argv.includes('--integration')) test('packaged Worker/Wasm matches native manual and sparse-generator Keys/Mirror records', async () => {
  const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
  const harness=process.env.JOTTUNE_HARNESS || path.join(root,'build/s12e10p1v1/harness');
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'keys-worker-'));
  const savedFetch=globalThis.fetch, savedSelf=globalThis.self;
  const native=(...args)=>{
    const result=spawnSync(harness,args,{encoding:'utf8',timeout:60000});
    assert.equal(result.status,0,result.stderr || String(result.error));
    return result;
  };
  try {
    const registry=JSON.parse(await fs.readFile(path.join(root,'web/engines.json'),'utf8'));
    const engine=registry.engines.find(entry=>entry.id===registry.default);
    assert(engine,'Build the current web package before integration acceptance.');
    globalThis.fetch=async value=>{
      const asset=path.resolve(root,'web',String(value));
      assert(asset.startsWith(path.join(root,'web')+path.sep),'Only local packaged assets may be read');
      return new Response(await fs.readFile(asset));
    };
    let received;
    globalThis.self={postMessage: value=>{received=value;}};
    await import('../web/worker.mjs');
    const manual=(await fs.readFile(path.join(root,'tests/golden/48000/route4.tl'),'utf8'))
      .replace(/^hash .*\n/m,'').replace('on 261626 ','on 220000 ');
    const sparse=(await fs.readFile(path.join(root,'tests/golden/gen/gencoprime-48000.tl'),'utf8'))
      .replace(/^hash .*\n/m,'').replace('length_ns 6000000000','length_ns 1000000000')
      .replace('gen 0 ','gen 5 ').replace('gen_lane 0 0 0 ','gen_lane 5 0 5 ')
      +'100000000 golden lane 1 enable 0\n200000000 golden lane 1 enable 1\n300000000 golden gen 5 reset 42\n';
    for (const [name,text] of [['manual',manual],['sparse-generator',sparse]]) {
      const input=path.join(directory,name+'.draft.tl'), canonical=path.join(directory,name+'.tl');
      await fs.writeFile(input,text);
      native('--play',input,'--merged',canonical);
      const recordText=await fs.readFile(canonical,'utf8');
      const sourceHash=await digest(bytes(recordText));
      received=undefined;
      await self.onmessage({data:{recordText,engine,operation:'inspect'}});
      assert.equal(received?.type,'inspection',received?.stderr);
      assert.equal(received.sourceRecordSha256,sourceHash);
      assert.equal(received.pcm,undefined); assert.equal(received.canonical,undefined);
      const nativeEvents=native('--events',canonical).stdout.split('\n')
        .filter(line=>/^(EVENT|GEN_EVENT) /.test(line)).map(row);
      assert.deepEqual(received.events,nativeEvents,name+' native/Wasm preview events');
      for (const mirror of [false,true]) {
        const projected=path.join(directory,name+(mirror?'-mirror':'-keys')+'.tl');
        const args=['--keys-profile','60','--keys-carrier-bound','--merged',projected];
        if (mirror) args.push('--keys-mirror');
        const projection=native(...args,'--',canonical);
        const replay=native('--play',projected);
        received=undefined;
        await self.onmessage({data:{recordText,engine,mode:'listen',profile:{halfWidth:60,mirror,levelPolicy:'carrier-bound'}}});
        assert.equal(received?.verdict,'valid',received?.stderr);
        assert.equal(received.verified,true);
        assert.equal(received.mode,'verify');
        assert.equal(received.sourceRecordSha256,sourceHash);
        assert.equal(received.profile.sourceMatched,true);
        assert.deepEqual(Buffer.from(received.canonical),await fs.readFile(projected),name+' native/Wasm canonical bytes');
        const play=row(replay.stdout.split('\n').find(line=>line.startsWith('PLAY ')));
        assert.equal(received.pcmHash,play.hash,name+' native/Wasm PCM hash');
        assert.equal(received.profile.carrierDeltaDb10,Number(row(projection.stdout.split('\n').find(line=>line.startsWith('PROFILE '))).carrier_delta_db10));
        assert(received.pcm?.length>0);
        if (name==='sparse-generator') {
          assert(received.events.some(event=>event.kind==='on' && event.lane!==undefined));
          assert.equal(native('--gen-events',canonical).stdout,native('--gen-events',projected).stdout);
        }
        received=null;
        globalThis.gc?.();
      }
    }
  } finally {
    globalThis.fetch=savedFetch;
    if (savedSelf===undefined) delete globalThis.self;
    else globalThis.self=savedSelf;
    await fs.rm(directory,{recursive:true,force:true});
  }
});
