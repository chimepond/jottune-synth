// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import { runCLI, row, verdict } from './runtime.mjs';
import { options, checkRecord, checkInspection, verification } from './policy.mjs';
import {inspectSpatial,validateSpatial} from './spatial.mjs';
import {compileScore,selectScoreStreams} from './authoring.mjs';
import {parseDocument,preserveDocument,bodySha256,documentSha256} from './tl-document.mjs';
const hex = bytes => [...new Uint8Array(bytes)].map(x => x.toString(16).padStart(2, '0')).join('');

function spatialWorker(engine, recordText, inspection, timeoutMs) {
  return new Promise((resolve, reject) => {
    // Native projection is synchronous once instantiated. Its own fetch timer
    // cannot interrupt that work, so the audio worker owns this deadline.
    const worker = new Worker(new URL(import.meta.url), {type:'module'});
    let settled = false;
    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      worker.terminate();
      if (error) reject(error); else resolve(result);
    };
    const timer = setTimeout(() => finish(Error('Native spatial view timed out.')), timeoutMs);
    worker.onmessage = ({data}) => {
      if (settled) return;
      try {
        if (data?.type === 'spatial-error') throw Error(data.error || 'Native spatial view is unavailable.');
        if (data?.type !== 'spatial-result') throw Error('Native spatial view returned an invalid response.');
        const projection = data.projection;
        validateSpatial(projection?.spatial, inspection);
        if (projection.spatial && !(projection.spatialModule instanceof WebAssembly.Module))
          throw Error('Native spatial view module is unavailable.');
        finish(null, projection);
      } catch (error) { finish(error); }
    };
    worker.onerror = event => {
      event.preventDefault?.();
      finish(Error('Native spatial view worker failed.'));
    };
    worker.onmessageerror = () => finish(Error('Native spatial view response could not be read.'));
    try { worker.postMessage({operation:'spatial-only', engine, recordText, inspection}); }
    catch (error) { finish(error); }
  });
}

function assetURL(engine, path) {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(engine.id)) throw Error('Invalid installed engine identifier.');
  const base = new URL('./', import.meta.url), expected = new URL('engines/' + engine.id + '/', base);
  const url = new URL(path, base);
  if (url.origin !== base.origin || !url.pathname.startsWith(expected.pathname) || url.search || url.hash)
    throw Error('Engine assets must come from their installed registry directory.');
  return url;
}
async function selectedRuntime(engine) {
  if (!engine.runtime_module) return runCLI; // Existing development registries predate archived runtimes.
  const url = assetURL(engine, engine.runtime_module);
  if (!/^[0-9a-f]{64}$/.test(engine.runtime_sha256 || '')) throw Error('Archived runtime digest is missing.');
  const response = await fetch(engine.runtime_module);
  if (!response.ok) throw Error('Archived engine runtime is unavailable.');
  if (hex(await crypto.subtle.digest('SHA-256', await response.arrayBuffer())) !== engine.runtime_sha256)
    throw Error('Archived engine runtime checksum mismatch.');
  const runtime = await import(url.href);
  if (typeof runtime.runCLI !== 'function') throw Error('Archived runtime does not expose the renderer.');
  return runtime.runCLI;
}
self.onmessage = async ({ data }) => {
  if (data?.operation === 'spatial-only') {
    try {
      checkRecord(data.recordText);
      if (data.engine.withdrawn || data.engine.withdrawn_at) throw Error('Renderer build has been withdrawn.');
      const projection = await inspectSpatial(data.engine, data.recordText, assetURL(data.engine, data.engine.library_wasm));
      validateSpatial(projection.spatial, data.inspection);
      self.postMessage({type:'spatial-result', projection});
    } catch (error) { self.postMessage({type:'spatial-error', error:error.message}); }
    return;
  }
  try {
    const { engine } = data;
    let recordText = data.recordText;
    if (data.operation === 'compose-selection') recordText = compileScore(data.score,{engine:engine.identity}).draftText;
    const requested = options({mode: data.mode, profile: data.profile});
    const profile = requested.profile;
    if (data.operation === 'inspect' && profile) throw Error('Note previews cannot apply a listening profile.');
    const mode = data.operation === 'inspect' ? 'listen' : profile ? 'verify' : requested.mode;
    checkRecord(recordText);
    if (engine.withdrawn || engine.withdrawn_at) throw Error('Renderer build has been withdrawn.');
    assetURL(engine, engine.browser_wasm);
    const response = await fetch(engine.browser_wasm);
    if (!response.ok) throw Error('Engine build not available. Run make web.');
    const bytes = await response.arrayBuffer();
    if (hex(await crypto.subtle.digest('SHA-256', bytes)) !== engine.browser_sha256) throw Error('Engine module checksum mismatch.');
    const execute = await selectedRuntime(engine);
    const module = await WebAssembly.compile(bytes);
    const inspected = await execute(module, ['--inspect', 'record.tl'], { 'record.tl': recordText });
    if (inspected.code) { if (/out of memory|^oom$/im.test(inspected.stderr)) inspected.stderr='Too large for this device.\n'+inspected.stderr; self.postMessage({ ...inspected, verdict: verdict(inspected) }); return; }
    const inspectedLines = inspected.stdout.split('\n');
    const nativeIdentity = inspectedLines.find(line => line.startsWith('ENGINE '))?.slice(7);
    if (nativeIdentity && nativeIdentity !== engine.identity) throw Error('Installed registry identity differs from the engine.');
    let info = row(inspectedLines.find(x => x.startsWith('INSPECT ')));
    checkInspection(info, mode);
    if (data.operation === 'inspect') {
      const listing = await execute(module, ['--events', 'record.tl'], {'record.tl':recordText});
      if (listing.code) { self.postMessage({...listing, verdict:verdict(listing)}); return; }
      const events = listing.stdout.split('\n').filter(line => /^(EVENT|GEN_EVENT) /.test(line));
      if (events.length > 20000) throw Error('Note preview exceeds the 20,000-event display limit. Load the song to listen.');
      self.postMessage({type:'inspection', verdict:'valid', info, events:events.map(row), eventsError:null,
        sourceRecordSha256:await bodySha256(recordText),metadata:parseDocument(recordText).metadata,
        engineId:engine.id, engineIdentity:engine.identity});
      return;
    }
    if (data.operation === 'compose-selection') {
      if (profile) throw Error('Score stream selection cannot also apply a Keys profile.');
      const chosen = selectScoreStreams(data.score,data.selection,{engine:engine.identity});
      checkRecord(chosen.authored.draftText);
      const selected = await execute(module,['--inspect','record.tl'],{'record.tl':chosen.authored.draftText});
      if (selected.code) { self.postMessage({...selected,verdict:verdict(selected)}); return; }
      checkInspection(row(selected.stdout.split('\n').find(line => line.startsWith('INSPECT '))),mode);
      const validation = 'original_and_selection_inspected';
      self.postMessage({type:'composition',verdict:'valid',...chosen.authored,
        coverage:{...chosen.authored.coverage,native_validation:validation},
        selection:{...chosen.selection,nativeValidation:validation},
        rendererRelease:engine.release_id || engine.id,engineId:engine.id,engineIdentity:engine.identity});
      return;
    }
    let renderText = recordText, sourceRecordSha256 = null, profileResult = null;
    if (profile) {
      if (info.keys_profile !== '1') throw Error('This renderer does not support on-demand Keys profiles. Select a compatible renderer explicitly.');
      // Obtain the original canonical identity with the native writer. The
      // transform itself then verifies both original partitions before editing.
      const source = await execute(module, ['--play-once', 'record.tl', '--merged', 'canonical.tl'], {'record.tl':recordText});
      if (source.code) { self.postMessage({...source, canonical:new Uint8Array(), pcm:null, verdict:verdict(source)}); return; }
      sourceRecordSha256 = await bodySha256(source.canonical);
      source.pcm = null;
      const arguments_ = ['--keys-profile', String(profile.halfWidth), '--merged', 'canonical.tl'];
      if (profile.mirror) arguments_.push('--keys-mirror');
      if (profile.levelPolicy === 'carrier-bound') arguments_.push('--keys-carrier-bound');
      const derived = await execute(module, [...arguments_, '--', 'record.tl'], {'record.tl':recordText});
      if (derived.code) { self.postMessage({...derived, canonical:new Uint8Array(), pcm:null, verdict:verdict(derived)}); return; }
      renderText = new TextDecoder('utf-8', {fatal:true,ignoreBOM:true}).decode(derived.canonical);
      checkRecord(renderText);
      const summary = row(derived.stdout.split('\n').find(line => line.startsWith('PROFILE ')) || '');
      const sourceSummary = row(derived.stdout.split('\n').find(line => line.startsWith('PROFILE_SOURCE ')) || '');
      if (!derived.canonical.length || sourceSummary.block_indep !== 'yes' ||
          (info.hash_present === '1' && sourceSummary.match !== 'yes')) throw Error('Original song verification did not complete.');
      if (Number(summary.half_width) !== profile.halfWidth || summary.mirror !== String(Number(profile.mirror)) ||
          summary.level_policy !== profile.levelPolicy || !['yes', 'no'].includes(summary.target_met) ||
          !Number.isFinite(Number(summary.peak_dbfs)) || summary.clipped !== '0' ||
          !Number.isInteger(Number(summary.carrier_delta_db10)) || !/^[1-8]$/.test(summary.passes || '') ||
          !/^[0-9a-f]{16}$/.test(sourceSummary.hash || '') ||
          (summary.target_met === 'no' && (profile.levelPolicy !== 'carrier-bound' || Number(summary.peak_dbfs) >= -2)))
        throw Error('Derived Keys profile summary is incomplete or inconsistent.');
      profileResult = {...profile, targetMet:summary.target_met === 'yes', peakDbfs:Number(summary.peak_dbfs),
        carrierDeltaDb10:Number(summary.carrier_delta_db10), passes:Number(summary.passes),
        limitedByCarrierBounds:summary.limited_by_carrier_bounds === '1',
        legalDeltaDb10:[Number(summary.legal_low_delta_db10), Number(summary.legal_high_delta_db10)],
        lastRequestedDeltaDb10:Number(summary.last_requested_delta_db10),
        lastAppliedDeltaDb10:Number(summary.last_applied_delta_db10),
        limitingCarriers:derived.stdout.split('\n').filter(line => line.startsWith('PROFILE_LIMIT ')).map(line => {
          const limit = row(line); return {patch:limit.patch, operator:Number(limit.operator), levelDb10:Number(limit.level_db10)};
        }),
        sourcePcmHash:sourceSummary.hash, sourceHashPresent:info.hash_present === '1',
        sourceMatched:info.hash_present === '1' ? sourceSummary.match === 'yes' : null, sourceBlockIndependent:true};
      const derivedInspection = await execute(module, ['--inspect', 'record.tl'], {'record.tl':renderText});
      if (derivedInspection.code) { self.postMessage({...derivedInspection, verdict:verdict(derivedInspection)}); return; }
      info = row(derivedInspection.stdout.split('\n').find(line => line.startsWith('INSPECT ')) || '');
      checkInspection(info, mode);
      if (info.hash_present !== '1') throw Error('Derived song has no saved PCM hash.');
    }
    const result = await execute(module, [mode === 'listen' ? '--play-once' : '--play', 'record.tl', '--merged', 'canonical.tl', '--metrics'], { 'record.tl': renderText });
    if (/out of memory|^oom$/im.test(result.stderr)) result.stderr='Too large for this device.\n'+result.stderr;
    const listing = result.code === 0 ? await execute(module, ['--events', 'record.tl'], { 'record.tl': renderText }) : {code:0,stdout:'',stderr:''};
    const lines = result.stdout.trim().split('\n');
    const playLine = lines.find(x => x.startsWith('PLAY ')) || '';
    const metricsLine = lines.find(x => x.startsWith('METRICS ')) || '';
    const play = row(playLine), metrics = row(metricsLine);
    if (verdict(result) !== 'valid') result.canonical = new Uint8Array();
    if (result.canonical.length) result.canonical = new TextEncoder().encode(preserveDocument(renderText,result.canonical));
    const recordSha256 = result.canonical.length ? await bodySha256(result.canonical) : null;
    const documentSha = result.canonical.length ? await documentSha256(result.canonical) : null;
    const value = { ...result, verdict: verdict(result), mode, info, play, metrics, playLine, metricsLine,
      recordSha256, documentSha256:documentSha, metadata:parseDocument(renderText).metadata, pcmHash: play.hash || null, engineIdentity: engine.identity, engineId: engine.id,
      rendererRelease: engine.release_id || null, development: engine.development ?? !engine.release_id,
      ...(profile ? {profile:profileResult, sourceRecordSha256} : {}),
      events: listing.stdout.split('\n').filter(x => /^(EVENT|GEN_EVENT) /.test(x)).map(row),
      eventsError: listing.code ? listing.stderr : null };
    Object.assign(value, verification(value));
    if (value.verdict === 'valid' && data.spatialView === false) {
      Object.assign(value, {spatial:null, spatialModule:null, spatialError:null,
        spatialStatus:{status:'not-requested',reason:'Live native spatial paths are a browser view. CLI SVG exports native command targets.'}});
    } else if (value.verdict === 'valid') {
      try {
        // The caller may reserve this short optional phase inside its existing
        // render deadline. Do not risk already valid audio for drawing data.
        const remaining = Number.isFinite(data.spatialDeadlineMs) ? data.spatialDeadlineMs-Date.now()-250 : 3000;
        if (remaining <= 0) throw Error('Native spatial view is unavailable: render time budget exhausted.');
        const projection = await spatialWorker(engine, renderText, info, Math.min(3000,remaining));
        Object.assign(value, projection);
      } catch (error) {
        // Optional native drawing metadata must never change valid PCM/TL or
        // turn a renderer without a matching companion into a playback error.
        Object.assign(value, {spatial:null, spatialModule:null, spatialError:error.message});
      }
    }
    self.postMessage(value, result.pcm ? [result.pcm.buffer, result.canonical.buffer] : [result.canonical.buffer]);
  } catch (error) { self.postMessage({ verdict: 'refused', code: 2, stderr: error.message, stdout: '' }); }
};
