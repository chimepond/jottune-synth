#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// node tests/analysis_worker.mjs [SOURCE_ROOT] [ANALYSIS_WASM]
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {Worker, isMainThread, parentPort, workerData} from 'node:worker_threads';
import {runCLI, row} from '../web/runtime.mjs';

const own = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
if (!isMainThread) {
  const {descriptor, wasm, behavior} = workerData;
  const base = new URL('../web/', import.meta.url);
  globalThis.self = {postMessage:message => parentPort.postMessage(message)};
  globalThis.fetch = async (input, options) => {
    const url = new URL(input);
    assert.equal(url.origin, base.origin);
    assert.equal(new URL('.', url).href, base.href);
    assert.equal(options.redirect, 'error');
    parentPort.postMessage({requested:url.href});
    if (behavior === 'stalled') return new Promise((_, reject) =>
      options.signal.addEventListener('abort', () => reject(Error('Download aborted.')), {once:true}));
    if (behavior === 'missing') return new Response('', {status:404});
    if (url.href === new URL('analysis-view.json', base).href) {
      if (behavior === 'oversized') return new Response(' '.repeat(65537));
      if (behavior === 'utf8') return new Response(new Uint8Array([255]));
      return new Response(JSON.stringify(descriptor));
    }
    assert.equal(url.href, new URL('analysis-view.wasm', base).href);
    if (behavior === 'oversized-wasm') return new Response('', {headers:{'content-length':String(64 * 1024 * 1024 + 1)}});
    return new Response(wasm);
  };
  await import('../web/analysis-worker.mjs');
  parentPort.on('message', data => self.onmessage({data}));
} else {
  const root = path.resolve(process.argv[2] || own);
  const companion = path.resolve(process.argv[3] || path.join(root, 'build/wasm/analysis.wasm'));
  const descriptor = JSON.parse(fs.readFileSync(path.join(path.dirname(companion), 'analysis.json')));
  const wasm = fs.readFileSync(companion);
  const registry = JSON.parse(fs.readFileSync(path.join(root, 'web/engines.json')));
  const engine = registry.engines.find(entry => entry.id === registry.default);
  const kept = await WebAssembly.compile(fs.readFileSync(path.join(root, 'web', engine.browser_wasm)));
  const text = fs.readFileSync(path.join(root, 'tests/golden/48000/sine.tl'), 'utf8');
  const files = {'record.tl':text};
  const inspected = await runCLI(kept, ['--inspect', 'record.tl'], files);
  const rendered = await runCLI(kept, ['--play', 'record.tl', '--merged', 'canonical.tl'], files);
  const listing = await runCLI(kept, ['--events', 'record.tl'], files);
  for (const reply of [inspected, rendered, listing]) assert.equal(reply.code, 0, reply.stderr);
  const result = {...rendered, engineId:engine.id, rendererRelease:engine.release_id,
    info:row(inspected.stdout.split('\n').find(line => line.startsWith('INSPECT '))),
    pcmHash:row(rendered.stdout.split('\n').find(line => line.startsWith('PLAY '))).hash,
    events:listing.stdout.split('\n').filter(line => /^(EVENT|GEN_EVENT) /.test(line)).map(row)};
  const beforePCM = result.pcm.slice(), beforeCanonical = result.canonical.slice();
  let checks = 0;
  async function run(data = {}, fixture = {}) {
    const worker = new Worker(new URL(import.meta.url), {workerData:{descriptor, wasm, ...fixture}});
    const requests = [], phases = [];
    return new Promise((resolve, reject) => {
      let finished = false;
      const finish = async (error, answer) => {
        if (finished) return;
        finished = true; clearTimeout(timer); await worker.terminate();
        if (error) reject(error); else resolve({...answer, requests, phases});
      };
      const timer = setTimeout(() => finish(Error('Analysis worker test timed out.')), 30000);
      worker.once('error', error => finish(error));
      worker.on('message', message => {
        if (message.requested) requests.push(message.requested);
        else if (message.phase) {
          phases.push(message);
          if (fixture.terminateOnDry) finish(null, {result:message.partial, terminated:true});
        } else finish(null, message);
      });
      worker.postMessage({result, engine, ...data}); // No transfer: cached buffers remain usable.
    });
  }
  const ordinary = await run({options:{windowSeconds:1, sections:[{name:'opening',startSeconds:0,endSeconds:1}]}});
  assert.equal(ordinary.result.pcm.status, 'available');
  assert.equal(ordinary.result.events.status, 'available');
  assert.equal(ordinary.result.pcm.sections[0].frames, 48000);
  assert.equal(ordinary.result.dry, undefined); assert.deepEqual(ordinary.requests, []); ++checks;

  const fast = await run({options:{bands:true,truePeak:false,sections:[{name:'to end'}]}});
  const pcmReport = fast.result.pcm;
  assert.deepEqual(pcmReport.sections[0].bands,pcmReport.bands);
  assert.equal(pcmReport.bands.length,5); assert.equal(pcmReport.sections[0].end_frame,pcmReport.frames);
  assert.match(pcmReport.sections[0].true_peak_reason,/disabled/); ++checks;
  const complete = await run({options:{dryVoices:true, dryTimeoutMs:30000}});
  assert.equal(complete.result.dry.status, 'available', complete.result.dry.reason);
  assert.equal(complete.result.dry.verified_mix_pcm_hash, result.pcmHash);
  assert.equal(complete.result.dry.stored_audio_bytes, 0);
  assert.equal(complete.phases.length, 1); assert.equal(complete.phases[0].timeoutMs, 30000);
  assert.equal(complete.phases[0].partial.dry, undefined);
  assert.deepEqual(complete.phases[0].partial.pcm, complete.result.pcm);
  assert.equal(complete.requests.length, 2); ++checks;

  const selected = await run({options:{dryVoices:[0]}});
  assert.equal(selected.result.dry.status, 'available', selected.result.dry.reason);
  assert.deepEqual(selected.result.dry.voices.map(voice => voice.voice), [0]); ++checks;
  for (const field of ['browser_sha256', 'library_sha256', 'wasm_sha256']) {
    const failure = await run({options:{dryVoices:true}}, {descriptor:{...descriptor, [field]:'0'.repeat(64)}});
    assert.equal(failure.result.pcm.status, 'available'); assert.equal(failure.result.dry.status, 'unavailable');
    assert.match(failure.result.dry.reason, /match|checksum/); assert.equal(failure.phases.length, 0); ++checks;
  }
  for (const behavior of ['missing', 'oversized', 'oversized-wasm', 'utf8', 'stalled']) {
    const failure = await run({options:{dryVoices:true}}, {behavior});
    assert.equal(failure.result.pcm.status, 'available'); assert.equal(failure.result.dry.status, 'unavailable');
    assert.equal(failure.phases.length, 0); ++checks;
  }
  for (const options of [{dryVoices:[]}, {dryVoices:[0,0]}, {dryVoices:true,dryTimeoutMs:0},
    {dryVoices:true,dryMaxBytes:43}, {dryVoices:true,dryMaxBytes:44}]) {
    const failure = await run({options});
    assert.equal(failure.result.pcm.status, 'available'); assert.equal(failure.result.dry.status, 'unavailable'); ++checks;
  }
  const wrongHash = await run({result:{...result,pcmHash:'0'.repeat(16)},options:{dryVoices:true}});
  assert.equal(wrongHash.result.dry.status, 'unavailable'); assert.match(wrongHash.result.dry.reason, /differs/); ++checks;
  const wrongEngine = await run({engine:{...engine,id:'other'},options:{dryVoices:true}});
  assert.equal(wrongEngine.result.dry.status, 'unavailable'); assert.deepEqual(wrongEngine.requests, []); ++checks;
  const invalidCanonical = await run({result:{...result,canonical:new Uint8Array([255])},options:{dryVoices:true}});
  assert.equal(invalidCanonical.result.dry.status, 'unavailable'); assert.deepEqual(invalidCanonical.requests, []); ++checks;
  const external = await run({baseURL:'https://example.invalid/',options:{dryVoices:true}});
  assert.match(external.error, /application directory/); assert.deepEqual(external.requests, []); ++checks;
  const malformed = await run({result:{...result,pcm:new Float32Array(8)}});
  assert.match(malformed.error, /signed 16-bit/); ++checks;
  const terminated = await run({options:{dryVoices:true}}, {terminateOnDry:true});
  assert.equal(terminated.terminated, true); assert.equal(terminated.result.pcm.status, 'available');
  assert.equal(terminated.result.events.status, 'available'); ++checks;
  assert.deepEqual(result.pcm, beforePCM); assert.deepEqual(result.canonical, beforeCanonical); ++checks;
  console.log(`analysis worker: ${checks} groups passed; cached PCM, pinned native companion, bounded failures and dry-phase termination`);
}
