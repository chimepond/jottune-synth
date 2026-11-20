#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Real Wasm, loopback HTTP and a temporary cache; no external network or builds.
import assert from 'node:assert/strict';
import {composeDocument,parseDocument,bodySha256} from '../web/tl-document.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {setup as setupKept,execute,parseManifest,assetPath,siteURL} from '../web/agent.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const web = path.resolve(process.env.JOTTUNE_TEST_WEB || path.join(root,'web'));
const own = path.join(root,'web','agent.mjs');
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const scratch = await fs.mkdtemp(path.join(os.tmpdir(),'jottune-agent-test-'));
const assets = new Map(), requests = [];
let manifest, behavior = {}, passed = 0, development = false;
const setup = options => setupKept({...options,allowDevelopment:development});
const exampleCatalog = {version:1,songs:[
  {id:'alpha',title:'Alpha',collection:'Tests',variants:[
    {kind:'keys',path:'songs/alpha-keys.tl',bytes:100,duration_seconds:24},
    {kind:'standard',path:'songs/alpha.tl',bytes:100,duration_seconds:24,record_sha256:'a'.repeat(64),document_sha256:'b'.repeat(64),
      discovery:{record_sha256:'a'.repeat(64),document_sha256:'b'.repeat(64),metadata:{genre:{value:'test-style',source:'retained-score'},
        tempo:{min_bpm:96,max_bpm:108,source:'retained-score'}}}}]},
  {id:'beta',title:'Beta',collection:'Tests',variants:[
    {kind:'standard',path:'songs/beta.tl',bytes:1000001,duration_seconds:24},
    {kind:'keys',path:'songs/beta-keys.tl',bytes:100,duration_seconds:24}]},
  {id:'gamma',title:'Gamma',collection:'Tests',variants:[{kind:'standard',path:'songs/gamma.tl',bytes:100,duration_seconds:301}]},
  {id:'delta',title:'Delta',collection:'Tests',variants:[{kind:'standard',path:'songs/delta.tl',bytes:100,duration_seconds:24}]}
]};
let songsCatalog = exampleCatalog;
const check = async (name,fn) => { await fn(); passed++; console.log('PASS ' + name); };
async function tree(directory,prefix) {
  for (const entry of await fs.readdir(directory,{withFileTypes:true})) {
    const source = path.join(directory,entry.name), target = prefix + entry.name;
    if (entry.isDirectory()) await tree(source,target + '/');
    else if (entry.isFile()) assets.set(target,await fs.readFile(source));
  }
}
async function manifestNow() {
  manifest = Buffer.from(JSON.stringify({format:1,entry:'agent.mjs',registry:'engines.json',
    files:[...assets].map(([name,bytes]) => ({path:name,sha256:sha(bytes),bytes:bytes.length}))}));
}
async function cli(...args) {
  return runNode(own,...args);
}
async function runNode(script,...args) {
  if (development) args.push('--allow-development');
  return new Promise((resolve,reject) => {
    const child = spawn(process.execPath,[script,...args],{stdio:['ignore','pipe','pipe']});
    let stdout = '', stderr = '';
    child.stdout.on('data',data => { stdout += data; }); child.stderr.on('data',data => { stderr += data; });
    child.on('error',reject); child.on('close',code => resolve({code,stdout,stderr}));
  });
}
const server = http.createServer((request,response) => {
  requests.push({method:request.method,path:request.url});
  if (!request.url.startsWith('/preview/')) { response.writeHead(404).end(); return; }
  const name = request.url.slice('/preview/'.length);
  if (behavior.redirect === name) { response.writeHead(302,{location:'/elsewhere'}).end(); return; }
  if (name === 'songs.json') { response.writeHead(200,{'content-type':'application/json'}).end(JSON.stringify(songsCatalog)); return; }
  const bytes = name === 'agent-manifest.json' ? manifest : assets.get(name);
  if (!bytes) { response.writeHead(404).end(); return; }
  response.writeHead(200,{'content-type':'application/octet-stream'});
  response.end(behavior.corrupt === name ? Buffer.from('corrupt') : bytes);
});
try {
  const registry = JSON.parse(await fs.readFile(path.join(web,'engines.json'),'utf8'));
  const engine = registry.engines.find(entry => entry.id === registry.default);
  assert.ok(engine && !engine.withdrawn,'Tests require an available renderer.');
  development = engine.development === true;
  assets.set('engines.json',Buffer.from(JSON.stringify({format:1,default:engine.id,engines:[engine]})));
  for (const name of ['worker.mjs','runtime.mjs','policy.mjs','media.mjs','links.mjs','spatial.mjs','tl-document.mjs'])
    assets.set(name,await fs.readFile(path.join(root,'web',name)));
  assets.set('agent.mjs',await fs.readFile(own));
  assets.set('reference/recipes.txt',await fs.readFile(path.join(root,'data/recipes.txt')));
  for (const name of ['authoring.mjs','analysis.mjs','analysis-runtime.mjs','analysis-view.json','analysis-view.wasm']) {
    try { assets.set(name,await fs.readFile(path.join(root,'web',name))); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      try { assets.set(name,await fs.readFile(path.join(web,name))); } catch (missing) { if (missing.code !== 'ENOENT') throw missing; }
    }
  }
  if (process.env.JOTTUNE_TEST_ANALYSIS) {
    const lane = path.resolve(process.env.JOTTUNE_TEST_ANALYSIS);
    for (const name of ['analysis.mjs','analysis-runtime.mjs']) assets.set(name,await fs.readFile(path.join(lane,'web',name)));
    for (const extension of ['json','wasm']) assets.set('analysis-view.' + extension,await fs.readFile(path.join(lane,'build','wasm','analysis.' + extension)));
  }
  await tree(path.join(root,'web/vendor/browser_wasi_shim'),'vendor/browser_wasi_shim/');
  await tree(path.join(web,'engines',engine.id),'engines/' + engine.id + '/');
  await manifestNow();
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const site = 'http://127.0.0.1:' + server.address().port + '/preview/';
  const cache = path.join(scratch,'cache');
  const example = await fs.readFile(path.join(web,'example.tl'),'utf8');
  let context;
  await check('paths, duplicate entries and URL authorities refuse',async () => {
    for (const name of ['../escape','/absolute','a//b','a/../b','a/%2f/b','a\\b','a/.hidden']) assert.throws(() => assetPath(name));
    for (const value of ['http://example.com/','https://u:p@example.com/','https://example.com/?q=1']) assert.throws(() => siteURL(value));
    const bad = JSON.parse(manifest); bad.files.push(bad.files[0]);
    assert.throws(() => parseManifest(Buffer.from(JSON.stringify(bad))),/duplicate/);
  });
  await check('source renderers require explicit development opt-in',async () => {
    if (development) await assert.rejects(setupKept({site,cache:path.join(scratch,'kept-only')}),/allow-development/);
  });
  await check('fresh static setup pins the selected renderer package',async () => {
    context = await setup({site,cache});
    assert.equal(context.engine.id,engine.id); assert.ok(context.entries.length > 10);
    assert.ok(requests.every(row => row.method === 'GET' && row.path.startsWith('/preview/')));
  });
  await check('downloaded bootstrap runs through script and parent aliases',async () => {
    const parent = path.join(scratch,'bootstrap-parent'), script = path.join(scratch,'downloaded-client.mjs');
    await fs.symlink(path.dirname(own),parent); await fs.symlink(own,script);
    for (const file of [path.join(parent,'agent.mjs'),script]) {
      const answer = await runNode(file,site,'setup','--cache',cache,'--offline');
      assert.equal(answer.code,0,answer.stderr);
      assert.equal(JSON.parse(answer.stdout).ready,true);
    }
  });
  await check('saved TL uses production verification and unchanged canonical bytes',async () => {
    const answer = await execute(context,'verify',example,{formats:['tl','json','wav','svg','link']});
    assert.equal(answer.result.verified,true); assert.equal(answer.result.pcmHash,'e76dc5bad8783650');
    assert.equal(answer.result.execution.renderPasses,2);
    assert.equal(answer.result.execution.renderInvocations,1); assert.equal(answer.result.execution.canonicalReplay,false);
    assert.equal(answer.result.play.match,'yes'); assert.equal(answer.result.play.block_indep,'yes');
    assert.equal(Buffer.from(answer.artifacts.tl).toString(),example);
    assert.equal(Buffer.from(answer.artifacts.wav).subarray(0,4).toString(),'RIFF');
    assert.match(answer.artifacts.svg,/<svg/); assert.match(answer.artifacts['link.txt'],/#v1\./);
    const saved = JSON.parse(answer.artifacts['verification.json']);
    assert.equal(saved.verified,true);
    assert.equal(answer.result.tlVersion,1);assert.equal(answer.result.engineCompatibilityVersion,1);
    assert.equal(answer.result.documentHeader,parseDocument(example).header?'present':'legacy-headerless');
    assert.equal(saved.site,site); assert.equal(saved.release,engine.id); assert.equal(saved.manifestSha256,context.manifestSha256);
    assert.equal(saved.offline,false); assert.equal(saved.revocationChecked,true); assert.deepEqual(saved.outputFiles,[]);
  });
  await check('portable TL metadata survives verified client export with stable body identity',async()=>{
    const metadata={title:'Portable client check',made_by:'agent',model:{id:'fixture/v1',source:'client-reported'}};
    const portable=composeDocument(parseDocument(example).body,metadata),answer=await execute(context,'verify',portable,{formats:['tl','wav','json']});
    assert.equal(answer.result.verified,true);assert.equal(answer.result.recordSha256,await bodySha256(example));
    assert.equal(answer.result.documentSha256,sha(Buffer.from(portable)));assert.deepEqual(answer.result.metadata,metadata);
    assert.equal(answer.result.tlVersion,1);assert.equal(answer.result.engineCompatibilityVersion,1);
    assert.equal(answer.result.documentHeader,'present');
    assert.equal(Buffer.from(answer.artifacts.tl).toString(),portable);
    assert.match(Buffer.from(answer.artifacts.wav).toString('utf8'),/song_metadata/);
    const future=await execute(context,'verify',portable.replace('# jottune-tl 1 1','# jottune-tl 1 2'),{formats:['tl']});
    assert.equal(future.result.verdict,'refused');assert.equal(future.artifacts.tl,undefined);
  });
  await check('draft canonical replay reports four real passes',async () => {
    const answer = await execute(context,'verify',example.replace(/^hash .*\n/m,''),{formats:['tl']});
    assert.equal(answer.result.verified,true); assert.equal(answer.result.execution.renderPasses,4);
    assert.equal(answer.result.execution.canonicalReplay,true);
    assert.equal(answer.result.execution.renderInvocations,2);
  });
  await check('listening uses one pass and never claims verification',async () => {
    const answer = await execute(context,'render',example,{mode:'listen',formats:['tl']});
    assert.equal(answer.result.spatialError,null);assert.equal(answer.result.spatialStatus.status,'not-requested');
    assert.equal(answer.result.verdict,'valid'); assert.equal(answer.result.verified,false);
    assert.equal(answer.result.execution.renderPasses,1); assert.equal(answer.result.play.block_indep,'unchecked');
    assert.equal(answer.result.execution.renderInvocations,1); assert.equal(answer.result.execution.canonicalReplay,false);
    assert.ok(answer.artifacts.tl.length);
    assert.throws(() => execute(context,'verify',example,{mode:'listen'}),/full verification/);
  });
  await check('optional analysis preserves PCM exports and invalid analysis options refuse',async () => {
    const answer = await execute(context,'analyze',example,{formats:['tl'],dryVoices:[0]});
    assert.equal(answer.result.verified,true); assert.ok(answer.artifacts.tl.length);
    assert.ok(['available','unavailable'].includes(answer.result.analysis.status));
    assert.ok(['available','unavailable'].includes(answer.result.dryAnalysis.status));
    if (assets.has('analysis.mjs')) assert.equal(answer.result.analysis.status,'available',JSON.stringify(answer.result.analysis));
    if (assets.has('analysis-view.wasm') && assets.has('analysis-runtime.mjs')) {
      assert.equal(answer.result.dryAnalysis.status,'available',JSON.stringify(answer.result.dryAnalysis));
      assert.equal(answer.result.dryAnalysis.verified_mix_pcm_hash,answer.result.pcmHash);
      assert.deepEqual(answer.result.dryAnalysis.voices.map(row => row.voice),[0]);
      const bounded = await execute(context,'analyze',example,{formats:['tl'],dryVoices:[0],dryMaxBytes:44});
      assert.equal(bounded.result.verified,true); assert.equal(bounded.result.dryAnalysis.status,'unavailable');
      assert.ok(bounded.result.dryAnalysis.requiredBytes > 44); assert.match(bounded.result.dryAnalysis.reason,/--dry-max-bytes/);
      assert.ok(bounded.artifacts.tl.length);
    }
    assert.throws(() => execute(context,'analyze',example,{windowSeconds:NaN}),/window/);
    assert.throws(() => execute(context,'analyze',example,{sections:[{startSeconds:1,endSeconds:0}]}),/Sections/);
    assert.throws(() => execute(context,'analyze',example,{dryVoices:[0,0]}),/distinct/);
  });
  if (assets.has('authoring.mjs')) await check('shipped authoring composes and verifies the starter without Python or custom tooling',async () => {
    const score = await fs.readFile(path.join(web,'starter.score.json'),'utf8');
    const answer = await execute(context,'compose',score,{formats:['tl']});
    assert.equal(answer.result.verified,true); assert.equal(answer.result.execution.canonicalReplay,true);
    assert.ok(answer.result.authoring.sourceMap); assert.ok(answer.artifacts.tl.length);
    const discovery = await execute(context,'discover',null);
    assert.ok(discovery.recipes.length >= 50);
    assert.equal(discovery.authoring.recipes,undefined); assert.equal(discovery.cache,context.folder);
  });
  if (assets.has('authoring.mjs')) await check('offline compose shows the rejected field and quoted fraction',async () => {
    const score = JSON.parse(await fs.readFile(path.join(web,'starter.score.json'),'utf8'));
    const index = score.events.findIndex(event => event.kind === 'note');
    score.events[index].duration = 0.5;
    const source = JSON.stringify(score), pointer = '/events/' + index + '/duration';
    await assert.rejects(execute(context,'compose',source), {pointer, message:/"1\/2"/});
    const file = path.join(scratch,'fraction.score.json');
    await fs.writeFile(file,source);
    const answer = await cli(site,'compose',file,'--cache',cache,'--offline','--out',path.join(scratch,'fraction'));
    assert.equal(answer.code,2);
    assert.ok(answer.stderr.includes(pointer),answer.stderr);
    assert.match(answer.stderr,/"1\/2"/);
  });
  if (assets.has('authoring.mjs')) await check('score stream solo and mute retain wet effects and controls with native original validation',async () => {
    const score = {version:1,fs:48000,clock:{bars:1,meter:[{bar:1,quarters:4}],tempo:[{bar:1,bpm:120}],ramp:'step',tail_ns:1000000000},
      declarations:'space stereo\nblock 64\nnear 100\ndelay 200000 16000 8000\nrecipe tone flute\n',
      streams:['lead','bass'],voices:['tone','tone'],events:[
        {kind:'pos',at:0,stream:'bass',voice:0,x_mm:-300,y_mm:1000,z_mm:0},
        {kind:'send',at:0,stream:'bass',voice:0,bus:'delay',level_q15:30000},
        {kind:'note',at:0,duration:'1/4',stream:'lead',voice:0,pitch:{note:'A4'},velocity_q15:24000},
        {kind:'note',at:0,duration:'1/4',stream:'bass',voice:1,pitch:{note:'A2'},velocity_q15:24000}]};
    const input = JSON.stringify(score), full = await execute(context,'compose',input,{formats:['tl','wav']});
    const solo = await execute(context,'compose',input,{formats:['tl','wav'],soloStreams:['lead']});
    const mute = await execute(context,'compose',input,{formats:['tl','wav'],muteStreams:['bass']});
    assert.equal(solo.result.verified,true);assert.equal(mute.result.verified,true);
    assert.equal(solo.result.authoring.selection.nativeValidation,'original_inspected');
    assert.equal(solo.result.authoring.selection.removedGateEvents,1);
    assert.deepEqual(solo.artifacts.tl,mute.artifacts.tl);assert.deepEqual(solo.artifacts.wav,mute.artifacts.wav);
    assert.notEqual(solo.result.pcmHash,full.result.pcmHash);
    const text = new TextDecoder().decode(solo.artifacts.tl);
    assert.match(text,/delay 200000 16000 8000/);assert.match(text,/bass 0 pos -300 1000 0/);
    assert.match(text,/bass 0 send delay 30000/);assert.doesNotMatch(text,/bass 1 on/);
    const dry = structuredClone(score);dry.events[1].level_q15=0;
    const noSend = await execute(context,'compose',JSON.stringify(dry),{formats:['wav'],soloStreams:['lead']});
    assert.notEqual(solo.result.pcmHash,noSend.result.pcmHash,'Post-FX send changes the rerender PCM');
    const wetPCM = new DataView(solo.artifacts.wav.buffer,solo.artifacts.wav.byteOffset), dryPCM = new DataView(noSend.artifacts.wav.buffer,noSend.artifacts.wav.byteOffset);
    let differenceEnergy=0;
    for(let sample=48000;sample<96000;sample++) differenceEnergy+=(wetPCM.getInt16(44+sample*2,true)-dryPCM.getInt16(44+sample*2,true))**2;
    assert.ok(Math.sqrt(differenceEnergy/48000)>8,'Retained send changes the tail by more than quantization noise');
    assert.equal(JSON.stringify(score),input);
    const file=path.join(scratch,'parts.score.json'), stem=path.join(scratch,'solo-part');await fs.writeFile(file,input);
    const selected=await cli(site,'compose',file,'--cache',cache,'--offline','--solo-streams','["lead"]','--out',stem);
    assert.equal(selected.code,0,selected.stderr);assert.equal(JSON.parse(selected.stdout).authoring.selection.mode,'solo');
    assert.deepEqual(await fs.readFile(stem+'.tl'),Buffer.from(solo.artifacts.tl));
    for (const args of [['--solo-streams','["lead"]','--mute-streams','["bass"]'],['--solo-streams','[]']])
      assert.equal((await cli(site,'compose',file,'--cache',cache,'--offline','--out',stem+'-bad',...args)).code,2);
    const invalid=structuredClone(score);invalid.events[3].velocity_q15=100000;
    await assert.rejects(execute(context,'compose',JSON.stringify(invalid),{soloStreams:['lead']}),/Original score failed native validation/);
    const shared=structuredClone(score);shared.events[3].voice=0;
    await assert.rejects(execute(context,'compose',JSON.stringify(shared),{soloStreams:['lead']}),/share a voice gate/);
    assert.throws(()=>execute(context,'verify',example,{soloStreams:['lead']}),/only to compose/);
    assert.ok(requests.every(row=>row.method==='GET'));
  });
  await check('discovery JSON exports once, preserves execution context and refuses irrelevant options',async () => {
    const stem = path.join(scratch,'discovery'), before = requests.length;
    const answer = await cli(site,'discover','--cache',cache,'--offline','--out',stem,'--formats','json');
    assert.equal(answer.code,0,answer.stderr);
    const saved = JSON.parse(await fs.readFile(stem + '.discovery.json','utf8'));
    const compact=JSON.parse(answer.stdout);
    for (const key of ['site','release','manifestSha256','offline','revocationChecked','cache','outputFiles']) assert.deepEqual(compact[key],saved[key]);
    assert.equal(compact.recipeCount,saved.recipes.length);assert.deepEqual(compact.recipeIds,saved.recipes.map(recipe=>recipe.id));
    assert.ok(compact.references.includes(site+'reference/recipes.txt'));
    assert.equal(compact.recipes,undefined);assert.ok(answer.stdout.length<10000);
    const full=await cli(site,'discover','--cache',cache,'--offline','--detail','full');
    assert.deepEqual(JSON.parse(full.stdout).recipes,saved.recipes);
    assert.deepEqual(saved.outputFiles,[stem + '.discovery.json']);
    assert.equal(saved.site,site); assert.equal(saved.release,engine.id); assert.equal(saved.manifestSha256,context.manifestSha256);
    assert.equal(saved.offline,true); assert.equal(saved.revocationChecked,false); assert.equal(saved.cache,context.folder);
    assert.equal(requests.length,before);
    assert.equal((await cli(site,'discover','--cache',cache,'--offline','--out',stem)).code,2);
    assert.deepEqual(JSON.parse(await fs.readFile(stem + '.discovery.json','utf8')),saved);
    for (const args of [['--formats','wav','--out',stem],['--analysis'],['--formats','json']]) {
      const refused = await cli(site,'discover',...args);
      assert.equal(refused.code,2); assert.match(refused.stderr,/Discover/);
    }
    assert.equal(requests.length,before);
  });
  await check('optional examples select distinct usable songs with reproducible seeds and no song downloads',async () => {
    const before = requests.length, stem = path.join(scratch,'example-discovery');
    const ordinary = await cli(site,'discover','--cache',cache);
    assert.equal(ordinary.code,0,ordinary.stderr); assert.equal(JSON.parse(ordinary.stdout).examples,undefined);
    assert.equal(requests.slice(before).filter(row => row.path === '/preview/songs.json').length,0);
    const selected = await cli(site,'discover','--cache',cache,'--examples','3','--example-seed','test-seed','--out',stem);
    assert.equal(selected.code,0,selected.stderr);
    const examples = JSON.parse(selected.stdout).examples;
    assert.equal(examples.seed,'test-seed'); assert.equal(examples.requested,3); assert.equal(examples.eligibleSongs,3);
    assert.equal(examples.catalogUrl,site + 'songs.json');
    assert.deepEqual(new Set(examples.songs.map(song => song.id)),new Set(['alpha','beta','delta']));
    const alpha = examples.songs.find(song => song.id === 'alpha'), beta = examples.songs.find(song => song.id === 'beta');
    assert.equal(alpha.variant,'standard'); assert.equal(alpha.url,site + 'songs/alpha.tl');
    assert.equal(alpha.recordSha256,'a'.repeat(64));assert.equal(alpha.documentSha256,'b'.repeat(64));
    assert.equal(alpha.metadata.genre.value,'test-style'); assert.equal(alpha.metadata.genre.source,'retained-score');
    assert.equal(alpha.metadata.tempo.max_bpm,108); assert.equal(beta.variant,'keys'); assert.deepEqual(beta.metadata,{});
    assert.deepEqual(JSON.parse(await fs.readFile(stem + '.discovery.json','utf8')).examples,examples);
    songsCatalog = {version:1,songs:[...exampleCatalog.songs].reverse().map(song => ({...song,variants:[...song.variants].reverse()}))};
    const repeated = await cli(site,'discover','--cache',cache,'--examples','3','--example-seed','test-seed','--detail','full');
    assert.equal(repeated.code,0,repeated.stderr); assert.deepEqual(JSON.parse(repeated.stdout).examples,examples);
    songsCatalog = exampleCatalog;
    const fresh = await cli(site,'discover','--cache',cache,'--examples','1');
    assert.equal(fresh.code,0,fresh.stderr);
    const random = JSON.parse(fresh.stdout).examples;
    assert.match(random.seed,/^[a-f0-9-]{36}$/); assert.equal(random.songs.length,1);
    const replay = await cli(site,'discover','--cache',cache,'--examples','1','--example-seed',random.seed);
    assert.equal(replay.code,0,replay.stderr); assert.deepEqual(JSON.parse(replay.stdout).examples,random);
    const another = await cli(site,'discover','--cache',cache,'--examples','1');
    assert.equal(another.code,0,another.stderr); assert.notEqual(JSON.parse(another.stdout).examples.seed,random.seed);
    assert.equal(requests.slice(before).filter(row => row.path === '/preview/songs.json').length,5);
    assert.ok(requests.slice(before).every(row => row.method === 'GET' && !row.path.startsWith('/preview/songs/')));
  });
  await check('example option bounds and offline use refuse before network access',async () => {
    const before = requests.length;
    for (const args of [['--examples','0'],['--examples','11'],['--examples','1.5'],['--examples','0x2'],
      ['--example-seed','without-count'],['--examples','1','--example-seed','x'.repeat(129)],
      ['--examples','1','--example-seed','line\nbreak'],['--examples','1','--offline']]) {
      const refused = await cli(site,'discover','--cache',cache,...args);
      assert.equal(refused.code,2); assert.match(refused.stderr,/example|Example/);
    }
    assert.equal((await cli(site,'setup','--examples','1')).code,2);
    assert.equal(requests.length,before);
  });
  await check('example catalog validates identities, paths and size and handles no eligible songs',async () => {
    const baseline = structuredClone(exampleCatalog);
    for (const change of [
      catalog => {catalog.version=2;},
      catalog => {catalog.songs.push(catalog.songs[0]);},
      catalog => {catalog.songs[0].title=42;},
      catalog => {catalog.songs[0].variants[0].path='songs/../escape.tl';},
      catalog => {catalog.songs[0].variants[0].path='https://elsewhere.test/song.tl';},
      catalog => {catalog.songs[0].variants[0].path='songs/a.tl?redirect=1';},
      catalog => {catalog.songs[0].variants[0].bytes=-1;},
      catalog => {catalog.padding='x'.repeat(2 * 1024 * 1024);}
    ]) {
      songsCatalog = structuredClone(baseline);change(songsCatalog);
      const refused = await cli(site,'discover','--cache',cache,'--examples','1');
      assert.equal(refused.code,2); assert.match(refused.stderr,/catalog|asset path|byte limit/);
    }
    songsCatalog = {version:1,songs:[baseline.songs[2]]};
    const empty = await cli(site,'discover','--cache',cache,'--examples','10');
    assert.equal(empty.code,0,empty.stderr);
    assert.equal(JSON.parse(empty.stdout).examples.eligibleSongs,0); assert.deepEqual(JSON.parse(empty.stdout).examples.songs,[]);
    // Songs contributed through tunes carry creator-declared labels and made_by, with the model pair when one was declared.
    songsCatalog = structuredClone(exampleCatalog);
    const delta = songsCatalog.songs[3].variants[0];
    delta.record_sha256 = 'd'.repeat(64);
    delta.discovery = {record_sha256:delta.record_sha256,metadata:{genre:{value:'test-style',source:'creator-declared'},
      tempo:{min_bpm:90,max_bpm:90,source:'creator-declared'}},provenance:{made_by:'human'}};
    songsCatalog.songs[0].variants[1].discovery.provenance = {made_by:'agent',model:'declared-model',model_source:'user-stated'};
    const contributed = await cli(site,'discover','--cache',cache,'--examples','3','--example-seed','test-seed');
    assert.equal(contributed.code,0,contributed.stderr);
    const found = id => JSON.parse(contributed.stdout).examples.songs.find(song => song.id === id);
    assert.deepEqual(found('delta').metadata,{genre:{value:'test-style',source:'creator-declared'},
      tempo:{min_bpm:90,max_bpm:90,source:'creator-declared'}});
    assert.deepEqual(found('delta').provenance,{made_by:'human'});
    assert.deepEqual(found('alpha').provenance,{made_by:'agent',model:'declared-model',model_source:'user-stated'});
    for (const provenance of [{},{made_by:'robot'},{made_by:'human',model:'declared-model'},{made_by:'human',prompt:'extra'}]) {
      delta.discovery.provenance = provenance;
      const refused = await cli(site,'discover','--cache',cache,'--examples','1');
      assert.equal(refused.code,2); assert.match(refused.stderr,/catalog/);
    }
    songsCatalog = exampleCatalog;
  });
  await check('native analysis admission rejects invalid sections and voices before the audio worker',async () => {
    const original = assets.get('worker.mjs');
    assets.set('worker.mjs',Buffer.from("throw Error('Audio worker reached');")); await manifestNow();
    try {
      const checked = await setup({site,cache});
      await assert.rejects(execute(checked,'analyze',example,{sections:[{startSeconds:0,endSeconds:300}]}),/section.*inside/);
      await assert.rejects(execute(checked,'analyze',example,{dryVoices:[63]}),/voice does not exist/);
      await assert.rejects(execute(checked,'analyze',example,{sections:[{startSeconds:0,endSeconds:.01}]}),/Audio worker reached/);
    } finally {assets.set('worker.mjs',original); await manifestNow(); await setup({site,cache});}
  });
  await check('output collision and source alias refuse before network or rendering',async () => {
    const input=path.join(scratch,'preflight.tl'), stem=path.join(scratch,'preflight-output');
    await fs.writeFile(input,example); await fs.writeFile(stem+'.verification.json','keep');
    const before=requests.length;
    const collision=await cli(site,'verify',input,'--cache',cache,'--out',stem,'--formats','json');
    assert.equal(collision.code,2); assert.match(collision.stderr,/Output exists/); assert.equal(requests.length,before);
    const source=await cli(site,'verify',input,'--cache',cache,'--out',input.slice(0,-3),'--formats','tl','--force');
    assert.equal(source.code,2); assert.match(source.stderr,/replace the source/); assert.equal(requests.length,before);
    assert.equal(await fs.readFile(stem+'.verification.json','utf8'),'keep'); assert.equal(await fs.readFile(input,'utf8'),example);
  });
  await check('render and dry deadlines fail before fetching assets',async () => {
    const before = requests.length;
    for (const args of [['--timeout-ms','180000'],['--timeout-ms','0'],['--timeout-ms','500','--dry-timeout-ms','1000']]) {
      const refused = await cli(site,'verify',path.join(scratch,'not-read.tl'),...args);
      assert.equal(refused.code,2); assert.match(refused.stderr,/deadline/i);
    }
    assert.equal(requests.length,before);
    const help = await cli('--help'); assert.match(help.stdout,/1\.\.120000 \(default 120000\)/);
  });
  await check('saved hash mismatch retains native failure and produces no artifacts',async () => {
    const answer = await execute(context,'verify',example.replace('hash e76dc5bad8783650','hash 0000000000000000'),{formats:['tl']});
    assert.equal(answer.result.code,3); assert.notEqual(answer.result.verified,true); assert.deepEqual(answer.artifacts,{});
  });
  await check('worker deadline interrupts synchronous native work',async () => {
    await assert.rejects(execute(context,'verify',example,{timeoutMs:1}),/deadline/);
  });
  await check('explicit offline mode makes no HTTP requests',async () => {
    const before = requests.length;
    const offline = await setup({site,cache,offline:true});
    const answer = await execute(offline,'verify',example,{formats:['json']});
    assert.equal(answer.result.verified,true); assert.equal(requests.length,before);
    const saved = JSON.parse(answer.artifacts['verification.json']);
    assert.equal(saved.offline,true); assert.equal(saved.revocationChecked,false); assert.equal(saved.manifestSha256,context.manifestSha256);
    await assert.rejects(setup({site,cache:path.join(scratch,'empty'),offline:true}));
  });
  await check('missing requested release has no default fallback',async () => {
    await assert.rejects(setup({site,cache,release:'missing'}),/unavailable/);
  });
  await check('corrupt cached transitive import is rejected at import time',async () => {
    const entry = context.entries.find(entry => entry.path.endsWith('/vendor/browser_wasi_shim/debug.js'));
    const file = path.join(context.folder,entry.path), original = await fs.readFile(file);
    await fs.writeFile(file,Buffer.alloc(original.length,32));
    const refused = await execute(context,'verify',example);
    assert.notEqual(refused.result.verified,true); assert.match(refused.result.stderr,/checksum/);
    await fs.writeFile(file,original);
  });
  await check('cache symlink cannot escape the selected package',async () => {
    const file = path.join(context.folder,'media.mjs'), bytes = await fs.readFile(file), outside = path.join(scratch,'outside.mjs');
    await fs.writeFile(outside,bytes); await fs.unlink(file); await fs.symlink(outside,file);
    await assert.rejects(execute(context,'verify',example,{formats:['wav']}));
    await fs.unlink(file); await fs.writeFile(file,bytes);
  });
  await check('download checksum and redirect failures refuse',async () => {
    behavior.corrupt = 'worker.mjs';
    await assert.rejects(setup({site,cache:path.join(scratch,'bad-download')}),/checksum/);
    behavior = {redirect:'engines.json'};
    await assert.rejects(setup({site,cache:path.join(scratch,'redirect')})); behavior = {};
  });
  await check('withdrawn_at is refused even when withdrawn boolean is false',async () => {
    const original = assets.get('engines.json'), changed = JSON.parse(original);
    changed.engines[0].withdrawn_at = '2026-10-01';
    assets.set('engines.json',Buffer.from(JSON.stringify(changed))); await manifestNow();
    await assert.rejects(setup({site,cache:path.join(scratch,'withdrawn')}),/available kept/);
    assets.set('engines.json',original); await manifestNow();
  });
  await check('CLI exports exclusive files and rejects malformed UTF8',async () => {
    const input = path.join(scratch,'input.tl'), stem = path.join(scratch,'output with spaces');
    await fs.writeFile(input,example);
    const first = await cli(site,'verify',input,'--cache',cache,'--offline','--out',stem,'--formats','tl,json','--detail','full');
    assert.equal(first.code,0,first.stderr);
    const before = await fs.readFile(stem + '.tl');
    const saved = JSON.parse(await fs.readFile(stem + '.verification.json','utf8'));
    assert.deepEqual(saved,JSON.parse(first.stdout));
    assert.equal(saved.site,site); assert.equal(saved.release,engine.id); assert.equal(saved.manifestSha256,context.manifestSha256);
    assert.equal(saved.offline,true); assert.equal(saved.revocationChecked,false);
    assert.equal(saved.recordSha256,sha(before)); assert.equal(saved.pcmHash,'e76dc5bad8783650');
    assert.deepEqual(saved.outputFiles,[stem + '.tl',stem + '.verification.json']);
    const second = await cli(site,'verify',input,'--cache',cache,'--offline','--out',stem);
    assert.equal(second.code,2); assert.match(second.stderr,/exists/); assert.deepEqual(await fs.readFile(stem + '.tl'),before);
    await fs.writeFile(input,Buffer.from([0xff]));
    assert.equal((await cli(site,'verify',input,'--cache',cache,'--offline')).code,2);
  });
  await check('compact analysis keeps warnings and optional failures while JSON retains every voice',async () => {
    const input = path.join(scratch,'many-voices.tl'), stem = path.join(scratch,'analysis-report');
    const text = example.replace(/^hash .*\n/m,'').replace('voice 0 sine',Array.from({length:32},(_,i) => 'voice ' + i + ' sine').join('\n'))
      .replace('0 golden 0 on 440000 32767','0 golden 0 on 440000 32767\n0 golden 1 on 440000 10000\n0 golden 2 on 440000 10000\n0 golden 2 off\n100000000 golden 1 on 440000 10000\n200000000 golden 1 off');
    await fs.writeFile(input,text);
    const answer = await cli(site,'analyze',input,'--cache',cache,'--out',stem,'--dry-voices','[0]','--dry-max-bytes','44');
    assert.equal(answer.code,0,answer.stderr);
    const compact = JSON.parse(answer.stdout), saved = JSON.parse(await fs.readFile(stem + '.verification.json','utf8'));
    assert.ok(Buffer.byteLength(answer.stdout) < 7000); assert.equal(compact.diagnostics.voices,undefined);
    assert.equal(compact.diagnostics.voiceCount,32); assert.equal(saved.diagnostics.voices.length,32);
    assert.ok(compact.diagnostics.warnings.some(row => row.code === 'held_voice_retrigger'));
    assert.ok(compact.diagnostics.warnings.some(row => row.code === 'on_then_off_same_frame'));
    assert.equal(compact.dryAnalysis.status,'unavailable'); assert.ok(compact.dryAnalysis.reason);
    assert.equal(compact.offline,false); assert.equal(compact.revocationChecked,true);
    assert.equal(saved.manifestSha256,compact.manifestSha256); assert.deepEqual(saved.outputFiles,compact.outputFiles);
    assert.equal(saved.recordSha256,sha(await fs.readFile(stem + '.tl'))); assert.equal(saved.pcmHash,compact.pcmHash);
    assert.ok(saved.nativeStdout); assert.equal(compact.nativeStdout,undefined);
  });
  await check('force cannot replace a source through a symlinked parent alias',async () => {
    const real = path.join(scratch,'real'), alias = path.join(scratch,'alias');
    await fs.mkdir(real); await fs.symlink(real,alias);
    const file = path.join(real,'song.tl'); await fs.writeFile(file,example);
    const refused = await cli(site,'verify',file,'--cache',cache,'--offline','--out',path.join(alias,'song'),'--force','--formats','tl');
    assert.equal(refused.code,2); assert.match(refused.stderr,/replace the source/);
    assert.equal(await fs.readFile(file,'utf8'),example);
  });
  await check('local client refuses upload commands and performs no service request',async () => {
    const before=requests.length;
    for(const command of ['publish','prepare-publish']) {
      const answer=await cli(site,command,'song.tl');
      assert.equal(answer.code,2);
      assert.throws(()=>execute(context,command,'ignored',{formats:[]}),/Unsupported local command/);
    }
    assert.equal(requests.length,before);
    assert.ok(requests.every(request=>request.method==='GET'&&!request.path.includes('/api/')));
  });
  console.log(passed + ' agent client groups passed.');
} finally {
  await new Promise(resolve => server.close(resolve));
  await fs.rm(scratch,{recursive:true,force:true});
}
