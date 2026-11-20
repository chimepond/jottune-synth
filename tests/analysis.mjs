#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Usage: node tests/analysis.mjs [SOURCE_ROOT] [ANALYSIS_WASM]
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
import {analyzePCM, analysisSectionFrames, diagnoseEvents, createDryAnalyzer, analysisLimits} from '../web/analysis.mjs';
import {analyzeDryVoices} from '../web/analysis-runtime.mjs';
const own = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const root = path.resolve(process.argv[2] || own);
const companionPath = path.resolve(process.argv[3] || path.join(own, 'build/wasm/analysis.wasm'));
const {runCLI, row} = await import(pathToFileURL(path.join(root, 'web/runtime.mjs')));
let checks = 0;
const near = (a, b, tolerance = 1e-7) => {
  if (a === null || b === null) assert.equal(a, b);
  else assert.ok(Math.abs(a - b) <= tolerance, `${a} differs from ${b} by ${Math.abs(a - b)}`);
  ++checks;
};
const python = `import sys,json,base64,importlib.util,numpy as np
s=importlib.util.spec_from_file_location('analysis_reference',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=json.load(sys.stdin);x=np.frombuffer(base64.b64decode(r['pcm']),dtype='<i2').reshape(-1,r['channels']).astype(float)/32768
result=m.meter(tuple(x[:,i] for i in range(r['channels'])),r['fs'])
print(json.dumps(result,allow_nan=False))`;
function reference(pcm, fsHz, channels) {
  const reply = spawnSync('python3', ['-B', '-I', '-c', python, path.join(root,'tools/music/analyze.py')],
    {input:JSON.stringify({pcm:Buffer.from(pcm.buffer,pcm.byteOffset,pcm.byteLength).toString('base64'),fs:fsHz,channels}),
      encoding:'utf8', maxBuffer:8 * 1024 * 1024});
  assert.equal(reply.status, 0, reply.stderr); return JSON.parse(reply.stdout);
}
function signal(fsHz, channels, seconds, kind) {
  const pcm = new Int16Array(Math.round(fsHz * seconds) * channels);
  for (let n = 0; n < pcm.length / channels; ++n) for (let c = 0; c < channels; ++c) {
    let x = .13 * Math.sin(2 * Math.PI * (c ? 377 : 997) * n / fsHz);
    if (kind === 'burst' && !(n / fsHz >= .9 && n / fsHz < 1.15)) x = 0;
    if (kind === 'silence') x = 0;
    if (kind === 'dc') x = .1;
    if (kind === 'quiet') x *= .000001;
    if (kind === 'contour') x *= n / fsHz < 1 ? .001 : n / fsHz < 2 ? .3 : 1;
    pcm[n * channels + c] = Math.round(x * 32768);
  }
  return pcm;
}
for (const fsHz of [44100,48000]) for (const channels of [1,2]) for (const kind of ['sine','burst','silence','dc','contour']) {
  const pcm = signal(fsHz,channels,3.2,kind), before = crypto.createHash('sha256').update(pcm).digest('hex');
  const actual = await analyzePCM({pcm,sampleRate:fsHz,channels},{windowSeconds:1,
    sections:[{name:'middle',startSeconds:1,endSeconds:2}]});
  const expected = reference(pcm,fsHz,channels);
  near(actual.loudness.integrated_lufs,expected.integrated);
  near(actual.loudness.short_term_max_lufs,expected.short_term_max,1e-5);
  near(actual.sample_peak_dbfs,expected.sample_peak);
  assert.equal(actual.loudness.dual_mono,channels === 1);
  near(actual.loudness.true_peak_dbtp,expected.true_peak,.02);
  assert.equal(crypto.createHash('sha256').update(pcm).digest('hex'),before);
  assert.equal(actual.sections[0].frames,fsHz); ++checks;
}
for (const seconds of [0,.1,.399,.4,2.99]) {
  const actual = await analyzePCM({pcm:signal(48000,1,seconds,'sine'),sampleRate:48000,channels:1});
  assert.equal(actual.loudness.short_term_max_lufs,null);
  if (seconds < .4) assert.equal(actual.loudness.integrated_lufs,null); ++checks;
}
// Exact end in a long record. Multiplying even a legal endpoint by fs can
// produce a fractional roundoff, adding a phantom frame if passed to ceil.
{
  const fs = 48000, frames = 9831230;
  assert.deepEqual(analysisSectionFrames({name:'coda',startSeconds:204},0,fs,frames),
    {first:9792000,last:frames,name:'coda'});
  assert.throws(() => analysisSectionFrames({name:'coda',startSeconds:204,endSeconds:204.82},1,fs,frames),
    /section 2 \("coda"\).*204.82.*9831230 frames at 48000 Hz.*Omit endSeconds/);
  for (const n of [7,59,77,299,4095]) {
    assert.equal(analysisSectionFrames({endSeconds:n / fs},0,fs,n).last,n);
    assert.equal(analysisSectionFrames({},0,fs,n).last,n);
  }
  assert.throws(() => analysisSectionFrames({name:'empty',startSeconds:.00101,endSeconds:.001011},0,fs,frames),/empty.*inside/);
  checks += 13;
}
// Two bin-centred tones occupy different bands and stereo positions. An
// overlapping section in the same FFT block must not borrow its neighbours.
{
  const fs = 8192, n = 2048, pcm = new Int16Array(2 * 2 * n);
  for (let i = 0; i < 2 * n; i++) {
    const sample = Math.round(9000 * Math.sin(2 * Math.PI * (i < n ? 128 : 2048) * i / fs));
    pcm[2*i] = sample; pcm[2*i+1] = i < n ? sample : -sample;
  }
  const sections = [{name:'first',endSeconds:n/fs},{name:'rest',startSeconds:n/fs},
    {name:'partial',startSeconds:.1,endSeconds:.3},{name:'whole'}];
  const result = await analyzePCM({pcm,sampleRate:fs,channels:2},{sections,bands:true,truePeak:false});
  const [first,rest,partial,all] = result.sections;
  near(first.stereo.side_energy_fraction,0); near(rest.stereo.side_energy_fraction,1);
  near(first.bands[1].energy_fraction,1,1e-6); near(rest.bands[3].energy_fraction,1,1e-6);
  near(first.bands[1].mean_square_fs2,.5 * (9000 / 32768) ** 2,1e-6);
  near(partial.bands.reduce((sum,band)=>sum+(band.energy_fraction ?? 0),0),1);
  assert.deepEqual(result.bands,all.bands);
  const alone = await analyzePCM({pcm,sampleRate:fs,channels:2},{sections:[sections[2]],bands:true,truePeak:false});
  assert.deepEqual(alone.sections[0].bands,partial.bands);
  const silent = await analyzePCM({pcm:new Int16Array(32),sampleRate:fs,channels:1},{bands:true,truePeak:false});
  assert(silent.bands.every(band => band.energy_fraction === null)); assert.equal(silent.ending.below_threshold_from_frame,0);
  checks += 4;
}
{
  const fs = 8000, pcm = new Int16Array(fs);
  pcm[2000] = 33; pcm[7999] = -32;
  const source = {pcm,sampleRate:fs,channels:1, info:{fs,voices:1,frames:fs,events:2},
    events:[{kind:'on',voice:0,time_ns:0,freq_millihz:440000},{kind:'off',voice:0,time_ns:125000000}]};
  const {ending} = await analyzePCM(source,{truePeak:false});
  assert.equal(ending.last_above_threshold_frame,2000); assert.equal(ending.below_threshold_from_frame,2001);
  assert.equal(ending.final_window_frames,800); near(ending.final_window_peak_dbfs,20*Math.log10(32/32768));
  near(ending.final_window_rms_dbfs,20*Math.log10(32/(Math.sqrt(800)*32768)));
  const diagnostics = diagnoseEvents(source,ending);
  assert.equal(diagnostics.final_gate_release_frame,1000); near(diagnostics.mix_settle_after_final_release_seconds,1001/fs);
  for (const incomplete of [
    {...source,eventsTruncated:true}, {...source,eventsError:'display limit'},
    {...source,events:source.events.slice(0,1)}, {...source,events:[null,null]},
    {...source,events:[source.events[0],{...source.events[1],kind:'unknown'}]},
    {...source,events:[source.events[0],{...source.events[1],time_ns:null}]},
    {...source,events:[source.events[0],{...source.events[1],frame:1001}]},
    {...source,info:{...source.info,frames:1000}}
  ]) {
    const refused=diagnoseEvents(incomplete,ending);
    assert.equal(refused.status,'unavailable'); assert.equal(refused.final_gate_release_frame,null);
    assert.equal(refused.mix_settle_after_final_release_seconds,null); assert.equal(refused.voices,undefined);
  }
  const legacy={info:{fs:8000,voices:1},events:[
    {kind:'on',voice:0,time_ns:0},{kind:'off',voice:0,time_ns:125000000}]};
  assert.equal(diagnoseEvents(legacy,ending).status,'available');
  assert.equal(diagnoseEvents({...legacy,info:{...legacy.info,events:3}},ending).status,'unavailable');
  assert.equal(diagnoseEvents({...source,info:{...source.info,events:1},
    events:[source.events[0],{...source.events[1],lane:0}]}).final_gate_release_frame,1000);
  pcm[7999] = -33;
  assert.equal((await analyzePCM(source,{truePeak:false})).ending.below_threshold_from_frame,null);
  checks += 5;
}
// Named section peaks share whole-record interpolation context, including
// selections across the FFT core boundary and overlapping/silent sections.
{
  const rate = 48000, pcm = new Int16Array(rate * 3 * 2);
  const impulse = 57343;
  pcm[2 * impulse + 1] = 24000;
  pcm[2 * (rate * 2)] = 12000;
  const sections = [
    {name:'whole',startSeconds:0,endSeconds:3},
    {name:'right',startSeconds:(impulse - 8) / rate,endSeconds:(impulse + 8) / rate},
    {name:'after',startSeconds:(impulse + .9) / rate,endSeconds:(impulse + 1.9) / rate},
    {name:'left',startSeconds:1.9,endSeconds:2.1},
    {name:'overlap',startSeconds:1,endSeconds:2.5},
  ];
  const measured = await analyzePCM({pcm,sampleRate:rate,channels:2},{sections});
  const [whole,right,after,left,overlap] = measured.sections;
  for (const key of ['true_peak_dbtp','true_peak_frame','true_peak_seconds','true_peak_channel','true_peak_method']) {
    assert.equal(whole[key],measured.loudness[key]); ++checks;
  }
  assert.equal(right.true_peak_channel,1); assert.equal(left.true_peak_channel,0);
  near(right.true_peak_dbtp,20 * Math.log10(24000 / 32768),.001);
  near(left.true_peak_dbtp,20 * Math.log10(12000 / 32768),.001);
  near(overlap.true_peak_dbtp,right.true_peak_dbtp);
  assert.equal(after.sample_peak,0);
  // The ideal isolated impulse has a nonzero sinc lobe at n+1.5; cropping
  // before interpolation would incorrectly produce digital silence here.
  assert.ok(after.true_peak_dbtp > -18 && after.true_peak_dbtp < -14);
  assert.equal(after.true_peak_channel,1);
  for (const section of measured.sections) {
    assert.ok(section.true_peak_frame >= section.start_frame && section.true_peak_frame < section.end_frame);
    near(section.true_peak_seconds,section.true_peak_frame / rate);
  }
  const withoutSections = await analyzePCM({pcm,sampleRate:rate,channels:2});
  assert.deepEqual(measured.loudness,withoutSections.loudness);
  for (const [channels,truePeak,reason] of [[2,false,/disabled/],[4,true,/Raw field/]]) {
    const unavailable = await analyzePCM({pcm:new Int16Array(rate * channels),sampleRate:rate,channels},
      {truePeak,sections:[{startSeconds:0,endSeconds:1}]});
    assert.equal(unavailable.sections[0].true_peak_dbtp,null);
    assert.match(unavailable.sections[0].true_peak_reason,reason); checks += 2;
  }
  const silence = await analyzePCM({pcm:new Int16Array(rate),sampleRate:rate,channels:1},
    {sections:[{startSeconds:0,endSeconds:1}]});
  assert.equal(silence.sections[0].true_peak_dbtp,null);
  assert.equal(silence.sections[0].true_peak_reason,null); checks += 14;
}
const antiphasic = new Int16Array([1000,-1000,2000,-2000,0,0,0,0]);
const stereo = await analyzePCM({pcm:antiphasic,sampleRate:48000,channels:2});
near(stereo.stereo.correlation,-1); near(stereo.trailing_digital_silence_seconds,2 / 48000);
// Independent sample-domain M/S reference, including DC, one silent channel,
// identical/opposite channels, arbitrary pairs and all-zero silence.
for (const samples of [[1000,1000,-2000,-2000], [1000,-1000,-2000,2000],
  [32767,0,-32768,0], [3000,1000,3000,1000], [7000,4000,-5000,9000], [0,0,0,0]]) {
  const pcm = new Int16Array(samples), frames = pcm.length / 2;
  const actual = (await analyzePCM({pcm,sampleRate:48000,channels:2},{truePeak:false})).stereo;
  let mid = 0, side = 0;
  for (let frame = 0; frame < frames; ++frame) {
    mid += ((pcm[2 * frame] + pcm[2 * frame + 1]) / 65536) ** 2 / frames;
    side += ((pcm[2 * frame] - pcm[2 * frame + 1]) / 65536) ** 2 / frames;
  }
  near(actual.mid_mean_square_fs2,mid,1e-15); near(actual.side_mean_square_fs2,side,1e-15);
  near(actual.mid_rms_dbfs,mid ? 10 * Math.log10(mid) : null);
  near(actual.side_rms_dbfs,side ? 10 * Math.log10(side) : null);
  near(actual.side_energy_fraction,mid + side ? side / (mid + side) : null);
  assert.match(actual.mid_side_method,/includes DC/); assert.deepEqual([...pcm],samples); checks += 2;
}
const fields = await analyzePCM({pcm:new Int16Array(400),sampleRate:48000,channels:4});
assert.equal(fields.loudness.integrated_lufs,null); assert.match(fields.loudness.reason,/Raw field/); ++checks;
for (const bad of [{pcm:new Float32Array(4),sampleRate:48000,channels:1},
  {pcm:new Int16Array(3),sampleRate:48000,channels:2}, {pcm:new Int16Array(3),sampleRate:0,channels:1}]) {
  await assert.rejects(analyzePCM(bad)); ++checks;
}
const small = {pcm:signal(48000,1,1,'sine'),sampleRate:48000,channels:1};
await assert.rejects(analyzePCM(small,{sections:[{startSeconds:1,endSeconds:2}]}));
await assert.rejects(analyzePCM(small,{windowSeconds:0}));
const abort = new AbortController(); setTimeout(() => abort.abort(),0);
await assert.rejects(analyzePCM(small,{signal:abort.signal,yieldEveryFrames:256}),{name:'AbortError'}); checks += 3;
let cancellations = 0;
await assert.rejects(analyzePCM({pcm:signal(48000,1,.01,'sine'),sampleRate:48000,channels:1},
  {shouldCancel:() => ++cancellations > 3}),{name:'AbortError'});
assert.equal(cancellations,4); checks += 2; // cancellation during the interpolation pass
const eventResult = {info:{voices:'1',fs:'48000'},events:[
  {kind:'on',voice:'0',time_ns:'0',stream:'lead',patch:'tone'},
  {kind:'on',voice:'0',time_ns:'100000000',stream:'lead',patch:'tone'},
  {kind:'off',voice:'0',time_ns:'100000001'}]};
const diagnostic = diagnoseEvents(eventResult);
assert.deepEqual(diagnostic.warnings.map(x => x.code),['on_then_off_same_frame','held_voice_retrigger']);
assert.equal(diagnostic.sends.status,'unavailable'); checks += 2;
assert.equal(diagnoseEvents({...eventResult,events:[{kind:'on',voice:'9',time_ns:'0'}]}).status,'unavailable'); ++checks;
// Interpolation can exceed sample peaks; compare separately from loudness.
let maximumPeakDifference = 0;
for (const fsHz of [44100,48000]) for (const kind of ['high','near-nyquist','impulse','alternating','dc-step']) {
  const pcm = new Int16Array(Math.round(fsHz * 1.6) * 2);
  for (let frame = 0; frame < pcm.length / 2; ++frame) {
    let x = .6 * Math.sin(2 * Math.PI * (kind === 'near-nyquist' ? .499 : .45) * frame + .3);
    if (kind === 'impulse') x = frame === Math.round(fsHz * .8) ? .9 : 0;
    if (kind === 'alternating') x = .7 * (frame % 2 ? 1 : -1);
    if (kind === 'dc-step') x = frame > fsHz * .8 ? .5 : -.5;
    pcm[frame * 2] = Math.round(x * 32768); pcm[frame * 2 + 1] = -pcm[frame * 2];
  }
  const actual = await analyzePCM({pcm,sampleRate:fsHz,channels:2});
  const expected = reference(pcm,fsHz,2);
  maximumPeakDifference = Math.max(maximumPeakDifference,Math.abs(actual.loudness.true_peak_dbtp - expected.true_peak));
  // Exact-Nyquist alternating samples at EOF have a strong dependence on
  // finite FFT block length. Keep that adversarial difference visible.
  near(actual.loudness.true_peak_dbtp,expected.true_peak,kind === 'alternating' ? 1.3 : .05);
  assert.ok(actual.loudness.true_peak_dbtp >= actual.sample_peak_dbfs - 1e-12); ++checks;
}

const registry = JSON.parse(fs.readFileSync(path.join(root,'web/engines.json')));
const engine = registry.engines.find(e => e.id === registry.default);
const kept = await WebAssembly.compile(fs.readFileSync(path.join(root,'web',engine.browser_wasm)));
const companion = await WebAssembly.compile(fs.readFileSync(companionPath));
const manifest = JSON.parse(fs.readFileSync(path.join(path.dirname(companionPath),'analysis.json')));
assert.equal(manifest.browser_sha256,engine.browser_sha256);
assert.equal(manifest.library_sha256,engine.library_sha256);
assert.equal(manifest.wasm_sha256,crypto.createHash('sha256').update(fs.readFileSync(companionPath)).digest('hex')); checks += 3;
async function rendered(recordText) {
  const inspect = await runCLI(kept,['--inspect','record.tl'],{'record.tl':recordText});
  assert.equal(inspect.code,0,inspect.stderr);
  const info = row(inspect.stdout.split('\n').find(line => line.startsWith('INSPECT ')));
  const output = await runCLI(kept,['--play','record.tl','--merged','canonical.tl'],{'record.tl':recordText});
  assert.equal(output.code,0,output.stderr);
  const listing = await runCLI(kept,['--events','record.tl'],{'record.tl':recordText});
  const events = listing.stdout.split('\n').filter(line => /^(EVENT|GEN_EVENT) /.test(line)).map(row);
  return {...output,info,events,pcmHash:row(output.stdout.split('\n').find(line => line.startsWith('PLAY '))).hash,
    recordText, module:companion};
}
const fixture = fs.readFileSync(path.join(root,'tests/golden/fx/stereo.tl'),'utf8');
const result = await rendered(fixture);
const dry = await analyzeDryVoices(result,{maxMilliseconds:30000});
assert.equal(dry.status,'available',dry.reason); assert.equal(dry.voices.length,3); assert.equal(dry.streams.length,1);
assert.equal(dry.stored_audio_bytes,0); assert.equal(dry.verified_mix_pcm_hash,result.pcmHash); checks += 5;
assert.deepEqual(dry.warnings,[]); assert.equal(dry.sends.status,'available'); checks += 2;
// Independent native dry writer, with no persistent WAV or changes to the fixture.
const scratch = fs.mkdtempSync(path.join(os.tmpdir(),'analysis-check-'));
try {
  const native = spawnSync(path.join(root,'build/s12e10p1v1/harness'), ['--play',
    '--stems-out',path.join(scratch,'dry.wav'),'--pcm-limit',String(analysisLimits.defaultDryBytes),
    path.join(root,'tests/golden/fx/stereo.tl')],{encoding:'utf8',maxBuffer:1024*1024});
  assert.equal(native.status,0,native.stderr);
  const bytes = fs.readFileSync(path.join(scratch,'dry.wav'));
  const consumer = createDryAnalyzer(result.info,result.events);
  for (let i = 0; i < bytes.length; i += 137) consumer.feed(bytes.subarray(i,i+137));
  const expected = consumer.finish(row(native.stdout.split('\n').find(line => line.startsWith('STEMS '))));
  assert.equal(dry.hash,expected.hash); assert.deepEqual(dry.voices,expected.voices); assert.deepEqual(dry.streams,expected.streams); checks += 4;
  const wrong = createDryAnalyzer(result.info,result.events);
  for (let i = 0; i < bytes.length; i += 65536) wrong.feed(bytes.subarray(i,i+65536));
  assert.throws(() => wrong.finish({}),/STEMS/); ++checks;
  const corrupt = bytes.subarray(0,44).slice(); corrupt[0] = 0;
  assert.throws(() => createDryAnalyzer(result.info,result.events).feed(corrupt),/header/);
  assert.throws(() => createDryAnalyzer(result.info,result.events).finish({}),/incomplete/);
  assert.throws(() => createDryAnalyzer(result.info,result.events).feed(new Uint8Array(1048577)),/chunk/); checks += 3;
} finally {fs.rmSync(scratch,{recursive:true,force:true});}
for (const [input,options,pattern] of [
  [{...result,pcmHash:'0000000000000000'},{},/differs/],
  [result,{maxBytes:1024},/budget/],
  [result,{maxMilliseconds:.5},/deadline/],
  [result,{maxMilliseconds:1},/time budget/],
  [result,{shouldCancel:() => true},/cancelled/],
  [{...result,module:null},{},/unavailable/]]) {
  const failure = await analyzeDryVoices(input,options);
  assert.equal(failure.status,'unavailable'); assert.match(failure.reason,pattern); ++checks;
}
// A useful musical-length polyphonic run, with real native generators retained.
let music = fs.readFileSync(path.join(root,'tests/golden/gen/genambient-48000.tl'),'utf8')
  .replace(/^length_ns .*$/m,'length_ns 20000000000').replace(/^hash .*\n/m,'')
  .replace('120000000000 golden','4000000000 golden').replace('240000000000 golden','8000000000 golden')
  .replace('360000000000 golden','12000000000 golden').replace('384000000000 golden','12800000000 golden')
  .replace('480000000000 golden','16000000000 golden');
// Preserve every generator control in the shorter authored test schedule.
const generated = await rendered(music);
const generatedDry = await analyzeDryVoices(generated,{maxBytes:512*1024*1024,maxMilliseconds:60000});
assert.equal(generatedDry.status,'available',generatedDry.reason);
assert.ok(generatedDry.voices.some(voice => voice.loudness.integrated_lufs !== null));
assert.equal(generatedDry.verified_mix_pcm_hash,generated.pcmHash); checks += 3;
const unused = await rendered(fs.readFileSync(path.join(root,'tests/golden/fx/nosend-stereo.tl'),'utf8'));
const unusedDry = await analyzeDryVoices(unused,{maxMilliseconds:30000});
assert.equal(unusedDry.status,'available',unusedDry.reason); assert.ok(unusedDry.warnings.some(w => w.code === 'effect_without_sends')); checks += 2;
console.log(`analysis: ${checks} checks passed; Python loudness parity, native dry parity, 20-second generated music; peak estimate max difference ${maximumPeakDifference.toFixed(6)} dB`);
