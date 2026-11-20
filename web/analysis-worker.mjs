// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import {analyzePCM, diagnoseEvents, analysisLimits} from './analysis.mjs';

const base = new URL('.', import.meta.url);
const decoder = new TextDecoder('utf-8', {fatal:true});
const sha256 = async bytes => [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
  .map(byte => byte.toString(16).padStart(2, '0')).join('');
const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const unavailable = error => ({status:'unavailable', scope:'voice_out_pre_fx', reason:error.message});

async function companionBytes(name, maximum) {
  // Fixed sibling paths only. Neither a record nor a message selects executable
  // URLs. Bound optional downloads so missing companions preserve PCM results.
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
  try { return await readCompanion(name, maximum, controller.signal); }
  finally { clearTimeout(timer); }
}

async function readCompanion(name, maximum, signal) {
  const response = await fetch(new URL(name, base), {redirect:'error', credentials:'same-origin', signal});
  if (!response.ok) throw Error('Source-analysis companion is unavailable.');
  const declared = response.headers.get('content-length');
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > maximum)) {
    await response.body?.cancel();
    throw Error('Source-analysis companion exceeds its byte limit.');
  }
  if (!response.body) throw Error('Source-analysis companion has no response body.');
  const reader = response.body.getReader(), chunks = [];
  let size = 0;
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maximum) throw Error('Source-analysis companion exceeds its byte limit.');
      chunks.push(value);
    }
  } catch (error) { await reader.cancel().catch(() => {}); throw error; }
  finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {bytes.set(chunk, offset); offset += chunk.byteLength;}
  return bytes;
}

// Input/output are structured-clone values. Do not transfer the caller's cached
// PCM or canonical buffers into this worker: analysis must leave them usable.
// The parent must terminate this worker on cancellation or deadline. A timer in
// this worker cannot interrupt synchronous native verification or Wasm execution.
export async function analyzeResult({result, options = {}, engine, baseURL} = {}, phase = () => {}) {
  if (baseURL !== undefined && new URL(baseURL, base).href !== base.href)
    throw Error('Analysis assets must use this application directory.');
  const pcm = await analyzePCM(result, {windowSeconds:options.windowSeconds, sections:options.sections,
    truePeak:options.truePeak, bands:options.bands});
  const report = {pcm, events:diagnoseEvents(result, pcm.ending)};
  if (options.dryVoices === undefined || options.dryVoices === false) return report;
  try {
    const voices = options.dryVoices === true ? undefined : options.dryVoices;
    if (voices !== undefined && (!Array.isArray(voices) || !voices.length || voices.length > 64 ||
        new Set(voices).size !== voices.length || voices.some(voice => !Number.isInteger(voice) || voice < 0 || voice > 63)))
      throw Error('Select distinct native voice indices or dryVoices:true.');
    const maxBytes = options.dryMaxBytes ?? analysisLimits.defaultDryBytes;
    const maxMilliseconds = options.dryTimeoutMs ?? 10000;
    if (!Number.isSafeInteger(maxBytes) || maxBytes < 44 || maxBytes > analysisLimits.dryBytes)
      throw Error('Invalid dry byte budget.');
    if (!Number.isSafeInteger(maxMilliseconds) || maxMilliseconds < 1 || maxMilliseconds > 300000)
      throw Error('Dry analysis deadline must be 1 to 300000 milliseconds.');
    if (!engine || engine.withdrawn || engine.withdrawn_at || !digest(engine.browser_sha256) || !digest(engine.library_sha256) ||
        (result.engineId !== undefined && result.engineId !== engine.id) ||
        (result.rendererRelease !== undefined && result.rendererRelease !== (engine.release_id || null)))
      throw Error('Source-analysis companion needs the selected render engine.');
    if (!(result.canonical instanceof Uint8Array) || !result.canonical.length || result.canonical.byteLength > 1000000)
      throw Error('A bounded canonical record is required for dry analysis.');
    const recordText = decoder.decode(result.canonical);
    const descriptor = JSON.parse(decoder.decode(await companionBytes('analysis-view.json', 65536)));
    if (descriptor.format !== 1 || !digest(descriptor.wasm_sha256) ||
        descriptor.browser_sha256 !== engine.browser_sha256 || descriptor.library_sha256 !== engine.library_sha256)
      throw Error('Source-analysis companion does not match the selected renderer.');
    const bytes = await companionBytes('analysis-view.wasm', 64 * 1024 * 1024);
    if (await sha256(bytes) !== descriptor.wasm_sha256) throw Error('Source-analysis companion checksum mismatch.');
    const module = await WebAssembly.compile(bytes);
    const {analyzeDryVoices} = await import('./analysis-runtime.mjs');
    phase({phase:'dry', timeoutMs:maxMilliseconds, partial:report});
    report.dry = await analyzeDryVoices({module, recordText, info:result.info, events:result.events, pcmHash:result.pcmHash},
      {voices, maxBytes, maxMilliseconds});
  } catch (error) { report.dry = unavailable(error); }
  return report;
}

self.onmessage = async ({data}) => {
  try { self.postMessage({result:await analyzeResult(data, message => self.postMessage(message))}); }
  catch (error) { self.postMessage({error:error.message}); }
};
