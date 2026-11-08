#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Download this one file from a JotTune player. Everything else is fetched
// from its explicit static manifest; composition/rendering never sends a score.
import fs from 'node:fs/promises';
import {constants, readSync, lstatSync, openSync, closeSync, fstatSync} from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {createHash, randomUUID} from 'node:crypto';
import {pathToFileURL, fileURLToPath} from 'node:url';
import {Worker, isMainThread, parentPort, workerData} from 'node:worker_threads';
import {registerHooks} from 'node:module';

const MAX_FILE = 64 * 1024 * 1024, MAX_TOTAL = 128 * 1024 * 1024;
const MAX_MANIFEST = 2 * 1024 * 1024, MAX_CATALOG = 2 * 1024 * 1024, MAX_INPUT = 1000000;
const SHA = /^[a-f0-9]{64}$/, ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const decoder = new TextDecoder('utf-8', {fatal:true});
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const json = value => JSON.stringify(value, null, 2) + '\n';
const fail = message => { throw Error(message); };

export function siteURL(value) {
  const base = new URL(value);
  if ((base.protocol !== 'https:' && !(base.protocol === 'http:' && ['localhost','127.0.0.1','[::1]'].includes(base.hostname))) ||
      base.username || base.password || base.search || base.hash || /[%\\]/.test(base.pathname))
    fail('Use a clean HTTPS player prefix (HTTP is allowed only on loopback).');
  if (!base.pathname.endsWith('/')) base.pathname += '/';
  return base;
}

export function assetPath(value) {
  if (typeof value !== 'string' || value.length > 512 ||
      !/^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(value) ||
      value.split('/').some(part => !part || part === '.' || part === '..' || part.startsWith('.')))
    fail('Manifest contains an unsafe asset path.');
  return value;
}

export function parseManifest(bytes) {
  if (bytes.length > MAX_MANIFEST) fail('Manifest is too large.');
  const value = JSON.parse(decoder.decode(bytes));
  if (value?.format !== 1 || value.entry !== 'agent.mjs' || value.registry !== 'engines.json' ||
      !Array.isArray(value.files) || !value.files.length || value.files.length > 4096)
    fail('Unsupported agent manifest.');
  const files = new Map(), folded = new Set();
  for (const entry of value.files) {
    const name = assetPath(entry.path), lower = name.toLowerCase();
    if (folded.has(lower) || !SHA.test(entry.sha256 || '') || !Number.isSafeInteger(entry.bytes) || entry.bytes < 0 || entry.bytes > MAX_FILE)
      fail('Invalid or duplicate manifest entry.');
    files.set(name, {path:name,sha256:entry.sha256,bytes:entry.bytes}); folded.add(lower);
  }
  for (const name of [value.entry,value.registry,'worker.mjs','runtime.mjs','policy.mjs','media.mjs','links.mjs'])
    if (!files.has(name)) fail('Manifest lacks required asset: ' + name);
  return {value,files};
}

async function download(url, maximum, milliseconds = 30000) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), milliseconds);
  try {
    const response = await fetch(url, {redirect:'error',signal:controller.signal,cache:'no-store'});
    if (!response.ok) fail('Static request failed with HTTP ' + response.status + '.');
    if (Number(response.headers.get('content-length')) > maximum) fail('Download exceeds its byte limit.');
    const chunks = []; let size = 0;
    for await (const chunk of response.body) {
      size += chunk.length;
      if (size > maximum) { controller.abort(); fail('Download exceeds its byte limit.'); }
      chunks.push(chunk);
    }
    return Buffer.concat(chunks, size);
  } finally { clearTimeout(timer); }
}

async function discoverExamples(context,count,seed) {
  const catalogUrl = new URL('songs.json',context.site).href;
  const catalog = JSON.parse(decoder.decode(await download(catalogUrl,MAX_CATALOG)));
  const text = value => typeof value === 'string' && value.length > 0 && value.length <= 512 && !/[\x00-\x1f\x7f]/.test(value);
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  // Songs contributed through tunes carry creator-declared labels and made_by, with the model pair
  // when one was declared. The archived songs carry retained-score labels and only the model pair.
  const SOURCES = ['retained-score','creator-declared'];
  const validProvenance = value => object(value) && Object.keys(value).length > 0 &&
    Object.keys(value).every(key => ['made_by','model','model_source'].includes(key)) &&
    (value.made_by === undefined || ['agent','human','mixed'].includes(value.made_by)) &&
    ('model' in value) === ('model_source' in value) &&
    (value.model === undefined || (text(value.model) && ['client-reported','user-stated','unknown'].includes(value.model_source)));
  if (catalog?.version !== 1 || !Array.isArray(catalog.songs) || catalog.songs.length > 10000)
    fail('Unsupported or oversized song catalog.');
  const ids = new Set(), candidates = [];
  for (const song of catalog.songs) {
    if (!text(song?.id) || ids.has(song.id) || !text(song.title) || !text(song.collection) ||
        (song.revision !== undefined && !text(song.revision)) || !Array.isArray(song.variants) || !song.variants.length || song.variants.length > 32)
      fail('Invalid or duplicate song catalog entry.');
    ids.add(song.id);
    const kinds = new Set(), usable = [];
    for (const variant of song.variants) {
      if (!text(variant?.kind) || kinds.has(variant.kind) || typeof variant.path !== 'string' ||
          !/^songs\/(?:[A-Za-z0-9._-]+\/)*[A-Za-z0-9._-]+\.tl$/.test(variant.path) ||
          !Number.isSafeInteger(variant.bytes) || variant.bytes < 0 || !Number.isFinite(variant.duration_seconds) || variant.duration_seconds < 0 ||
          (variant.record_sha256 !== undefined && !SHA.test(variant.record_sha256))) fail('Invalid song catalog variant.');
      assetPath(variant.path);kinds.add(variant.kind);
      if (variant.bytes > 0 && variant.bytes <= MAX_INPUT && variant.duration_seconds > 0 && variant.duration_seconds <= 300) usable.push(variant);
    }
    usable.sort((a,b) => (a.kind === 'standard' ? 0 : 1) - (b.kind === 'standard' ? 0 : 1) || (a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : 0));
    if (!usable.length) continue;
    const variant = usable[0], discovery = variant.discovery, metadata = discovery?.metadata ?? {}, provenance = discovery?.provenance;
    if (!object(metadata) || (discovery !== undefined && (!object(discovery) || discovery.record_sha256 !== variant.record_sha256)) ||
        (metadata.genre !== undefined && (!text(metadata.genre?.value) || !SOURCES.includes(metadata.genre.source))) ||
        (metadata.tempo !== undefined && (!Number.isFinite(metadata.tempo?.min_bpm) || !Number.isFinite(metadata.tempo?.max_bpm) ||
          metadata.tempo.min_bpm <= 0 || metadata.tempo.max_bpm < metadata.tempo.min_bpm || !SOURCES.includes(metadata.tempo.source))) ||
        (provenance !== undefined && !validProvenance(provenance)))
      fail('Invalid song catalog metadata.');
    const known = {};
    if (metadata.genre) known.genre = {value:metadata.genre.value,source:metadata.genre.source};
    if (metadata.tempo) known.tempo = {min_bpm:metadata.tempo.min_bpm,max_bpm:metadata.tempo.max_bpm,source:metadata.tempo.source};
    candidates.push({rank:hash('jottune-examples-v1\0' + seed + '\0' + song.id),song:{id:song.id,title:song.title,collection:song.collection,
      ...(song.revision ? {revision:song.revision} : {}),variant:variant.kind,url:new URL(variant.path,context.site).href,
      bytes:variant.bytes,durationSeconds:variant.duration_seconds,...(variant.record_sha256 ? {recordSha256:variant.record_sha256} : {}),
      metadata:known,...(provenance ? {provenance:{...(provenance.made_by ? {made_by:provenance.made_by} : {}),
        ...(provenance.model ? {model:provenance.model,model_source:provenance.model_source} : {})}} : {})}});
  }
  candidates.sort((a,b) => a.rank < b.rank ? -1 : a.rank > b.rank ? 1 : a.song.id < b.song.id ? -1 : a.song.id > b.song.id ? 1 : 0);
  return {seed,requested:count,eligibleSongs:candidates.length,catalogUrl,songs:candidates.slice(0,count).map(item => item.song),
    note:'Optional random references from the current catalog, not quality rankings or a prescribed style. No song files were downloaded.'};
}

// The user-selected cache root may have ordinary OS aliases in its ancestors.
// Its leaf, and every directory/file below the resolved root, must not be links.
async function directory(root, relative = '') {
  let current = root;
  for (const part of relative ? relative.split('/') : []) {
    current = path.join(current, part);
    try { await fs.mkdir(current, {mode:0o700}); } catch (error) { if (error.code !== 'EEXIST') throw error; }
    const st = await fs.lstat(current);
    if (!st.isDirectory() || st.isSymbolicLink()) fail('Cache directory is not a real directory.');
  }
  return current;
}

async function readRegular(file, maximum) {
  const handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const st = await handle.stat();
    if (!st.isFile() || st.size > maximum) fail('Expected a bounded regular file.');
    const bytes = Buffer.alloc(Math.min(st.size,maximum) + 1);
    let length = 0;
    while (length < bytes.length) {
      const read = await handle.read(bytes,length,bytes.length-length,null);
      if (!read.bytesRead) break;
      length += read.bytesRead;
    }
    if (length > st.size || length > maximum) fail('File grew while being read.');
    return bytes.subarray(0,length);
  } finally { await handle.close(); }
}

async function atomicWrite(file, bytes, force = false) {
  const parent = await fs.realpath(path.dirname(file)), target = path.join(parent,path.basename(file));
  let previous;
  try { previous = await fs.lstat(target); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (previous && (!force || !previous.isFile() || previous.isSymbolicLink() || previous.nlink !== 1))
    fail('Output exists or is unsafe: ' + file);
  const temporary = path.join(parent,'.jottune-' + randomUUID() + '.tmp');
  const handle = await fs.open(temporary,'wx',0o600);
  try {
    try { await handle.writeFile(bytes); await handle.sync(); } finally { await handle.close(); }
    if (force) await fs.rename(temporary,target);
    else await fs.link(temporary,target); // atomic no-replace publication
  } finally { await fs.rm(temporary,{force:true}); }
}

function selectedFiles(files, engine) {
  const selected = [...files.values()].filter(entry =>
    entry.path.startsWith('engines/') ? entry.path.startsWith('engines/' + engine.id + '/') :
      !entry.path.startsWith('songs/') && !entry.path.startsWith('catalog/'));
  if (selected.reduce((sum,entry) => sum + entry.bytes,0) > MAX_TOTAL) fail('Selected agent package exceeds 128 MiB.');
  return selected;
}

export async function setup({site,cache,offline = false,release,allowDevelopment = false}) {
  const deadline = Date.now() + 120000;
  const base = siteURL(site), requestedRoot = path.resolve(cache || path.join(os.homedir(),'.cache','jottune-agent'));
  await fs.mkdir(requestedRoot,{recursive:true,mode:0o700});
  if ((await fs.lstat(requestedRoot)).isSymbolicLink()) fail('Cache root must not be a symlink.');
  const root = await fs.realpath(requestedRoot), siteRoot = await directory(root,hash(base.href));
  let manifestBytes, previous;
  if (offline) {
    previous = JSON.parse(decoder.decode(await readRegular(path.join(siteRoot,'current.json'),MAX_MANIFEST * 2 + 4096)));
    if (previous.site !== base.href || typeof previous.manifest !== 'string' || !SHA.test(previous.manifestSha256 || '')) fail('Offline cache identity differs.');
    manifestBytes = Buffer.from(previous.manifest,'utf8');
    if (hash(manifestBytes) !== previous.manifestSha256) fail('Offline manifest checksum differs.');
  } else manifestBytes = await download(new URL('agent-manifest.json',base),MAX_MANIFEST);
  const {files,value} = parseManifest(manifestBytes), manifestSha256 = hash(manifestBytes);
  const folder = await directory(siteRoot,manifestSha256);
  async function obtain(entry) {
    if (Date.now() >= deadline) fail('Agent setup exceeded its 120-second deadline.');
    const parts = entry.path.split('/'); parts.pop();
    await directory(folder,parts.join('/'));
    const target = path.join(folder,entry.path);
    let bytes;
    try { bytes = await readRegular(target,entry.bytes); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (bytes && (bytes.length !== entry.bytes || hash(bytes) !== entry.sha256)) fail('Cached asset checksum differs: ' + entry.path);
    if (!bytes) {
      if (offline) fail('Offline asset is missing: ' + entry.path);
      bytes = await download(new URL(entry.path,base),entry.bytes,Math.min(30000,deadline-Date.now()));
      if (bytes.length !== entry.bytes || hash(bytes) !== entry.sha256) fail('Downloaded asset checksum differs: ' + entry.path);
      try { await atomicWrite(target,bytes); }
      catch (error) {
        if (error.code !== 'EEXIST') throw error;
        const concurrent = await readRegular(target,entry.bytes);
        if (!concurrent.equals(bytes)) fail('Concurrent cache asset differs.');
      }
    }
    return bytes;
  }
  const registry = JSON.parse(decoder.decode(await obtain(files.get(value.registry))));
  const id = release ?? (offline ? previous.release : registry.default);
  const matches = (registry.engines || []).filter(entry => entry.id === id || entry.release_id === id);
  if (matches.length !== 1) fail('Requested renderer is unavailable; no fallback was selected.');
  const engine = matches[0];
  const kept = engine.development === false && engine.release_id === engine.id;
  const development = allowDevelopment === true && engine.development === true && engine.release_id === null;
  if (!ID.test(engine.id) || engine.withdrawn || engine.withdrawn_at || typeof engine.identity !== 'string' || !engine.identity ||
      (!kept && !development)) fail('Select an available kept renderer release; source builds require --allow-development.');
  for (const [name,digest] of [['browser_wasm','browser_sha256'],['runtime_module','runtime_sha256']]) {
    const entry = files.get(engine[name]);
    if (!entry || !entry.path.startsWith('engines/' + engine.id + '/') || entry.sha256 !== engine[digest])
      fail('Manifest and renderer registry disagree.');
  }
  const entries = selectedFiles(files,engine);
  for (const entry of entries) await obtain(entry);
  if (!offline) await atomicWrite(path.join(siteRoot,'current.json'),json({site:base.href,release:engine.id,
    manifestSha256,manifest:decoder.decode(manifestBytes)}),true);
  return {site:base.href,folder,engine,manifestSha256,entries,offline};
}

// Recheck at module loading time, including transitive/static/dynamic imports.
// Hashing only a root module before import does not cover its dependencies.
function installVerifiedLoader(context) {
  const files = new Map(context.entries.map(entry => [path.resolve(context.folder,entry.path),entry]));
  const checked = file => {
    const entry = files.get(file);
    if (!entry) fail('Import or asset is absent from the selected manifest.');
    let current = context.folder;
    if (!lstatSync(current).isDirectory() || lstatSync(current).isSymbolicLink()) fail('Unsafe cache root.');
    for (const part of entry.path.split('/').slice(0,-1)) {
      current = path.join(current,part);
      const st = lstatSync(current); if (!st.isDirectory() || st.isSymbolicLink()) fail('Unsafe cache directory.');
    }
    const fd = openSync(file,constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const st = fstatSync(fd);
      if (!st.isFile() || st.size !== entry.bytes) fail('Cached artifact size differs.');
      const bytes = Buffer.alloc(entry.bytes + 1);
      let length = 0, count;
      while (length < bytes.length && (count = readSync(fd,bytes,length,bytes.length-length,null))) length += count;
      if (length !== entry.bytes) fail('Cached artifact size changed.');
      const exact = bytes.subarray(0,length);
      if (hash(exact) !== entry.sha256) fail('Cached artifact checksum differs: ' + entry.path);
      return exact;
    } finally { closeSync(fd); }
  };
  registerHooks({
    resolve(specifier,context_,next) {
      const resolved = next(specifier,context_);
      if (!resolved.url.startsWith('file:') || !files.has(fileURLToPath(resolved.url))) fail('Import is outside the verified package.');
      return resolved;
    },
    load(url) {
      const bytes = checked(fileURLToPath(url));
      return {format:'module',source:decoder.decode(bytes),shortCircuit:true};
    }
  });
  globalThis.fetch = async value => {
    const url = new URL(value,pathToFileURL(context.folder + path.sep));
    if (url.protocol !== 'file:' || url.search || url.hash) fail('Rendering may access only verified cached assets.');
    return new Response(checked(fileURLToPath(url)));
  };
  return name => import(pathToFileURL(path.join(context.folder,assetPath(name))).href);
}

async function workerMain({context,command,input,options:opts}) {
  const load = installVerifiedLoader(context);
  if (command === 'discover') {
    const author = context.entries.some(e => e.path === 'authoring.mjs') ? await load('authoring.mjs') : null;
    const {recipes,...authoring} = author?.authoringDescription() || {};
    return {...executionContext(context),renderer:context.engine,cache:context.folder,
      authoring:author ? authoring : undefined,recipes:author?.listRecipes(),
      commands:['setup','discover','compose','render','verify','analyze','export'],
      references:context.entries.filter(e => e.path.endsWith('.txt'))
        .map(e => new URL(e.path,context.site).href)};
  }
  let inspectedModule, inspectedRuntime;
  const inspect = async (text, label = 'Input') => {
    const {checkRecord,checkInspection} = await load('policy.mjs');
    const {row} = await load('runtime.mjs');
    checkRecord(text);
    inspectedRuntime ||= (await load(context.engine.runtime_module || 'runtime.mjs')).runCLI;
    inspectedModule ||= await WebAssembly.compile(await (await fetch(context.engine.browser_wasm)).arrayBuffer());
    const answer = await inspectedRuntime(inspectedModule,['--inspect','record.tl'],{'record.tl':text});
    if (answer.code) fail(label + ' failed native validation: ' + answer.stderr.slice(0,1000));
    const lines = answer.stdout.split('\n'), identity = lines.find(line => line.startsWith('ENGINE '))?.slice(7);
    if (identity !== context.engine.identity) fail('Inspected engine identity differs from the selected release.');
    const info = row(lines.find(line => line.startsWith('INSPECT ')));
    checkInspection(info,opts.mode ?? 'verify');
    return info;
  };
  let recordText = input, authored, selection;
  if (command === 'compose') {
    const {compileScore,selectScoreStreams} = await load('authoring.mjs');
    const score = JSON.parse(input), compileOptions = {engine:context.engine.identity};
    authored = compileScore(score,compileOptions);
    if (opts.soloStreams || opts.muteStreams) {
      // Validate every original event with the kept native reader, including
      // events the selection would remove. No original audio is rendered.
      await inspect(authored.draftText,'Original score');
      const selected = selectScoreStreams(score,opts.soloStreams ? {soloStreams:opts.soloStreams} : {muteStreams:opts.muteStreams},compileOptions);
      selection = {...selected.selection,nativeValidation:'original_inspected'};
      authored = selected.authored;
    }
    recordText = authored.draftText;
  }
  // Native geometry is cheap to inspect; reject invalid selections before any
  // audio pass. Do not infer duration, rate or voices by parsing TL text here.
  let dryUnavailable;
  if (opts.sections || opts.dryVoices) {
    const info = await inspect(recordText), fs = Number(info.fs), frames = Number(info.frames);
    const duration = frames / fs, voices = Number(info.voices);
    if (opts.sections?.some(section => {
      const start = section.startSeconds ?? 0, end = section.endSeconds ?? duration;
      return end > duration || Math.ceil(end * fs) <= Math.ceil(start * fs);
    })) fail('Analysis section must select frames inside the record.');
    if (opts.dryVoices?.some(voice => voice >= voices)) fail('Selected dry voice does not exist in the record.');
    if (opts.dryVoices) {
      const {analysisLimits} = await load('analysis.mjs');
      const bytes = frames * voices * 4, budget = opts.dryMaxBytes ?? analysisLimits.defaultDryBytes;
      if (bytes > budget) dryUnavailable = {status:'unavailable',scope:'voice_out_pre_fx',requiredBytes:bytes,
        reason:`Dry source analysis needs ${bytes} bytes; budget is ${budget}. ${bytes <= analysisLimits.dryBytes ? `Retry with --dry-max-bytes ${bytes} if resources permit.` : `This exceeds the ${analysisLimits.dryBytes}-byte maximum; shorten the source or reduce its allocated voices.`} Selecting fewer voices does not reduce native output.`,
        limits:{bytes:budget,milliseconds:Math.min(opts.dryTimeoutMs ?? 10000,opts.timeoutMs ?? 120000)}};
    }
  }
  let rendered;
  const mode = opts.mode ?? 'verify';
  globalThis.self = {postMessage:value => { rendered = value; }};
  await load('worker.mjs');
  const run = async text => {
    rendered = null;
    await self.onmessage({data:{recordText:text,engine:context.engine,mode,spatialView:false}});
    return rendered;
  };
  let result = await run(recordText), renderInvocations = 1, canonicalReplay = false;
  if (mode === 'verify' && result.verdict === 'valid' && result.info?.hash_present !== '1') {
    const expected = result.pcmHash, original = result.pcm;
    result = await run(decoder.decode(result.canonical)); renderInvocations++; canonicalReplay = true;
    if (result.verdict === 'valid' && (result.pcmHash !== expected || !Buffer.from(original.buffer,original.byteOffset,original.byteLength)
      .equals(Buffer.from(result.pcm.buffer,result.pcm.byteOffset,result.pcm.byteLength))))
      fail('Canonical replay differs from the authored render.');
  }
  result.execution = {renderInvocations,renderPasses:(mode === 'listen' ? 1 : 2) * renderInvocations,canonicalReplay};
  Object.assign(result,executionContext(context));
  if (authored) result.authoring = {sourceMap:authored.sourceMap,advisories:authored.advisories,counts:authored.counts,
    ...(selection ? {selection} : {})};
  if (result.verdict !== 'valid' || (mode === 'verify' && !result.verified)) return {result:report(result),artifacts:{}};
  if (opts.analyze || command === 'analyze') {
    try {
      const {analyzePCM,diagnoseEvents} = await load('analysis.mjs');
      result.analysis = await analyzePCM(result,{windowSeconds:opts.windowSeconds ?? 4,sections:opts.sections});
      result.diagnostics = diagnoseEvents(result);
    } catch (error) { result.analysis = {status:'unavailable',reason:error.message}; }
  }
  if (dryUnavailable) result.dryAnalysis = dryUnavailable;
  else if (opts.dryVoices) {
    try {
      const descriptor = await (await fetch('analysis-view.json')).json();
      const bytes = await (await fetch('analysis-view.wasm')).arrayBuffer();
      if (descriptor.format !== 1 || descriptor.wasm_sha256 !== hash(new Uint8Array(bytes)) ||
          descriptor.browser_sha256 !== context.engine.browser_sha256 || descriptor.library_sha256 !== context.engine.library_sha256)
        fail('Source-analysis companion does not match the selected kept renderer.');
      const {analyzeDryVoices} = await load('analysis-runtime.mjs');
      result.dryAnalysis = await analyzeDryVoices({module:await WebAssembly.compile(bytes),recordText:decoder.decode(result.canonical),
        info:result.info,events:result.events,pcmHash:result.pcmHash},
      {voices:opts.dryVoices,maxBytes:opts.dryMaxBytes,maxMilliseconds:Math.min(opts.dryTimeoutMs ?? 10000,opts.timeoutMs ?? 120000)});
    } catch (error) { result.dryAnalysis = {status:'unavailable',reason:error.message,scope:'voice_out_pre_fx'}; }
  }
  const artifacts = {}, formats = opts.formats;
  const media = await load('media.mjs');
  if (formats.includes('tl')) artifacts.tl = result.canonical;
  if (formats.includes('wav')) artifacts.wav = new Uint8Array(await media.toWav(result,opts.provenance || {}).arrayBuffer());
  if (formats.includes('svg')) artifacts.svg = media.toSvg(result);
  if (formats.includes('link')) artifacts['link.txt'] = await (await load('links.mjs')).portableLink(result.canonical,context.site,result.rendererRelease) + '\n';
  const summary = report(result);
  if (formats.includes('json')) artifacts['verification.json'] = json(summary);
  return {result:summary,artifacts};
}

function executionContext(context) {
  return {site:context.site,release:context.engine.id,manifestSha256:context.manifestSha256,
    development:context.engine.development,offline:context.offline,revocationChecked:!context.offline,outputFiles:[]};
}

function report(result) {
  const {pcm,canonical,spatialModule,events,stdout,...summary} = result;
  return {...summary,canonicalBytes:canonical?.byteLength || 0,eventCount:events?.length || 0,
    quality:{clipped:result.play?.clipped ?? null,silent:result.play ? Number(result.play.peak) === 0 : null},
    nativeStdout:stdout};
}

export function execute(context,command,input,options = {}) {
  validateOptions(command,options);
  const timeoutMs = options.timeoutMs ?? 120000;
  return new Promise((resolve,reject) => {
    const worker = new Worker(new URL(import.meta.url),{workerData:{agentWorker:true,context,command,input,
      options:{formats:[],...options}},resourceLimits:{maxOldGenerationSizeMb:128,maxYoungGenerationSizeMb:16}});
    let settled = false;
    const finish = (error,value) => {
      if (settled) return; settled = true; clearTimeout(timer);
      worker.terminate().then(() => error ? reject(error) : resolve(value),reject);
    };
    const timer = setTimeout(() => finish(Error('Rendering or analysis exceeded its deadline; no output was written.')),timeoutMs);
    worker.once('message',message => finish(message.error ? Error(message.error) : null,message.value));
    worker.once('error',error => finish(error));
    worker.once('exit',code => { if (!settled) finish(Error('Renderer worker exited before completion (' + code + ').')); });
  });
}

function validateOptions(command,options) {
  if (!['setup','discover','compose','render','verify','analyze','export'].includes(command)) fail('Unsupported local command.');
  if (options.soloStreams !== undefined || options.muteStreams !== undefined) {
    if (command !== 'compose') fail('Stream selection belongs only to compose with a score-v1 source.');
    if (options.soloStreams !== undefined && options.muteStreams !== undefined) fail('Choose either --solo-streams or --mute-streams.');
    const names = options.soloStreams ?? options.muteStreams;
    if (!Array.isArray(names) || !names.length || names.length > 16 || new Set(names).size !== names.length ||
        names.some(name => typeof name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_-]*$/.test(name)))
      fail('Stream selection needs a JSON array of distinct stream identifiers.');
  }
  const timeoutMs = options.timeoutMs ?? 120000;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120000) fail('Deadline must be 1..120000 ms (default 120000).');
  if (options.mode !== undefined && !['listen','verify'].includes(options.mode)) fail('Mode must be listen or verify.');
  if (command === 'verify' && options.mode === 'listen') fail('This command requires full verification.');
  if (options.windowSeconds !== undefined && (!Number.isFinite(options.windowSeconds) || options.windowSeconds < .1 || options.windowSeconds > 300))
    fail('Analysis window must be 0.1 to 300 seconds.');
  if (options.sections !== undefined && (!Array.isArray(options.sections) || options.sections.length > 32 || options.sections.some(section =>
    !section || typeof section !== 'object' || Array.isArray(section) || Object.keys(section).some(key => !['name','startSeconds','endSeconds'].includes(key)) ||
    (section.name !== undefined && (typeof section.name !== 'string' || section.name.length > 120)) ||
    !Number.isFinite(section.startSeconds ?? 0) || (section.startSeconds ?? 0) < 0 ||
    (section.endSeconds !== undefined && (!Number.isFinite(section.endSeconds) || (section.startSeconds ?? 0) >= section.endSeconds)))))
    fail('Sections require up to 32 named finite startSeconds/endSeconds intervals.');
  if (options.dryVoices !== undefined && (!Array.isArray(options.dryVoices) || !options.dryVoices.length || options.dryVoices.length > 64 ||
      new Set(options.dryVoices).size !== options.dryVoices.length || options.dryVoices.some(v => !Number.isInteger(v) || v < 0 || v > 63)))
    fail('Dry voices must be distinct indices from 0 to 63.');
  if (options.dryMaxBytes !== undefined && (!Number.isSafeInteger(options.dryMaxBytes) || options.dryMaxBytes < 44 || options.dryMaxBytes > 2 ** 31))
    fail('Dry byte budget must be 44..2147483648.');
  if (options.dryTimeoutMs !== undefined && (!Number.isSafeInteger(options.dryTimeoutMs) || options.dryTimeoutMs < 1 || options.dryTimeoutMs > 120000))
    fail('Dry deadline must be 1..120000 ms, inside the total worker deadline.');
  if (options.dryTimeoutMs !== undefined && options.dryTimeoutMs > timeoutMs) fail('Dry deadline must not exceed the total worker deadline.');
}

function warningSummary(rows = []) {
  const groups = new Map();
  for (const row of rows) {
    const code = row.code || row.kind || 'advisory';
    const group = groups.get(code) || {code,occurrences:0,message:row.message};
    group.occurrences += Number.isSafeInteger(row.count) && row.count > 0 ? row.count : 1;
    groups.set(code,group);
  }
  return {warningCount:rows.length,warnings:[...groups.values()]};
}

function terminalDiscovery(result) {
  const {site,release,manifestSha256,offline,revocationChecked,cache,outputFiles,commands} = result;
  return {site,release,manifestSha256,offline,revocationChecked,cache,outputFiles,
    renderer:{id:result.renderer?.id,identity:result.renderer?.identity,development:result.renderer?.development},commands,
    authoring:{version:result.authoring?.version,eventKinds:result.authoring?.eventKinds,
      limits:result.authoring?.limits,recipeLevels:result.authoring?.recipeLevels,unsupported:result.authoring?.unsupported},
    recipeCount:result.recipes?.length || 0,recipeIds:result.recipes?.map(recipe => recipe.id) || [],
    references:result.references?.filter(url => /\/(?:agent-start|authoring|analysis|native-reference|recipes)\.txt$/.test(url)),
    ...(result.examples ? {examples:result.examples} : {}),
    details:'Use --detail full or --out STEM --formats json for complete recipe descriptions, carrier fields and references.'};
}

function terminalReport(result) {
  const pick = (value,names) => Object.fromEntries(names.filter(name => value?.[name] !== undefined).map(name => [name,value[name]]));
  const summary = {...pick(result,['verdict','code','verified','verification','mode','recordSha256','pcmHash','rendererRelease',
    'execution','site','release','development','manifestSha256','offline','revocationChecked','outputFiles','quality','canonicalBytes','eventCount',
    'spatialStatus','spatialError','eventsError'])};
  if (result.info) summary.dimensions = {sampleRate:result.sampleRate,channels:result.channels,
    frames:Number(result.info.frames),voices:Number(result.info.voices),durationSeconds:Number(result.info.frames) / result.sampleRate};
  if (result.play) summary.play = pick(result.play,['peak','peak_dbfs','rms_dbfs','clipped','match','block_indep']);
  if (result.stderr) summary.stderr = String(result.stderr).slice(0,1000);
  if (result.analysis) summary.analysis = {...pick(result.analysis,['status','scope','reason','sample_peak_dbfs','rms_dbfs',
    'trailing_digital_silence_seconds']),loudness:pick(result.analysis.loudness,['integrated_lufs','short_term_max_lufs',
    'true_peak_dbtp','true_peak_method','true_peak_reason']),windowCount:result.analysis.windows?.length,sectionCount:result.analysis.sections?.length};
  if (result.diagnostics) summary.diagnostics = {...pick(result.diagnostics,['status','scope','reason','sends','release_tails']),
    voiceCount:result.diagnostics.voices?.length,streamCount:result.diagnostics.streams?.length,...warningSummary(result.diagnostics.warnings)};
  if (result.dryAnalysis) summary.dryAnalysis = {...pick(result.dryAnalysis,['status','scope','reason','requiredBytes','verified_mix_pcm_hash','limits','notes']),
    voiceCount:result.dryAnalysis.voices?.length,streamCount:result.dryAnalysis.streams?.length,...warningSummary(result.dryAnalysis.warnings)};
  if (result.authoring) summary.authoring = {sourceMapLines:result.authoring.sourceMap?.length,counts:result.authoring.counts,
    ...(result.authoring.selection ? {selection:result.authoring.selection} : {}),
    ...warningSummary(result.authoring.advisories)};
  return {...summary,details:'Use --detail full or a JSON export for complete analyses, per-voice rows and native output.'};
}

const HELP = `JotTune URL client (Node 22.15+; built-in modules only)
  node agent.mjs SITE setup|discover [--release ID] [--cache DIR] [--offline]
  node agent.mjs SITE discover --out STEM [--formats json]
  node agent.mjs SITE discover --examples COUNT [--example-seed SEED]
  node agent.mjs SITE compose SCORE.json --out STEM
  node agent.mjs SITE compose SCORE.json --solo-streams '["lead"]' --out SOLO
  node agent.mjs SITE verify|render|analyze|export SONG.tl [--out STEM]
Options: --formats tl,json,wav,svg,link (default tl,json); --force; --timeout-ms N
         --mode verify|listen; --analysis; --sections JSON; --window-seconds N
         --dry-voices JSON --dry-max-bytes N --dry-timeout-ms N
         --provenance JSON; --detail compact|full (default compact)
Compose selection: --solo-streams JSON or --mute-streams JSON, never both.
Selection preserves declarations/effects, inspects the complete original, and
requires distinct voices for selected/muted note gates. Generators are refused.
A solo/mute rerender is a new performance, not additive stems of the mix.
Rendering and analysis run locally. Online commands fetch the static manifest
and missing runtime assets; discover --examples also fetches songs.json.
There is no upload command. Offline execution uses a complete verified cache;
current renderer revocation cannot be checked while offline.
Kept releases are required by default. For your own source build, explicitly
add --allow-development; reports label it as development, never as a kept release.
Cache defaults to ~/.cache/jottune-agent; --cache ./cache keeps downloads local.
Optional examples choose 1..10 distinct songs without downloading/rendering them.
--example-seed repeats the selection for the same catalog; normal discovery
chooses no examples. Catalog downloads are bounded to 2 MiB.
Render/analysis deadline in ms: 1..120000 (default 120000). The dry deadline must
fit within that total and defaults to the smaller of 10000 ms and the total.
Dry bytes = frames * ALL allocated voices * 4, default 134217728/max 2147483648.
Fewer selected dry voices reduce measurement work, not the producer's bytes.
A refused optional dry pass leaves valid TL/PCM exports available.
Verification checks saved hash and block independence. Drafts are replayed
canonically: two invocations/four native passes. Saved TL verification takes
one invocation/two passes. execution.canonicalReplay reports that extra draft
invocation; verified/play.match/play.block_indep report verification success.
Listen uses one pass and remains unverified.
Outputs are exclusive unless --force is explicit; input aliases are refused.
STEM.tl/.wav/.svg/.link.txt are selected exports; JSON is STEM.verification.json.
Discover writes STEM.discovery.json. The client never saves your editable score.
Console JSON is compact; --detail full has the complete saved-report schema.
Native live spatial paths are browser views; CLI SVG exports command targets.
Read agent-start.txt, authoring.txt and analysis.txt for examples and field units.
`;

async function checkOutputPaths(stem, suffixes, inputFile, force) {
  await fs.mkdir(path.dirname(stem),{recursive:true});
  const source = inputFile ? await fs.realpath(path.resolve(inputFile)) : null;
  const parent = await fs.realpath(path.dirname(stem));
  for (const suffix of suffixes) {
    const file = stem + '.' + suffix;
    if (source && path.join(parent,path.basename(file)) === source)
      fail('Export would replace the source input. Choose another stem.');
    try {
      const st = await fs.lstat(file);
      if (!force || !st.isFile() || st.isSymbolicLink() || st.nlink !== 1) fail('Output exists or is unsafe: ' + file);
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
}

export async function main(argv = process.argv.slice(2)) {
  if (!argv.length || argv.includes('--help') || argv[0] === 'help') { process.stdout.write(HELP); return 0; }
  const positional = [], opts = {};
  const valued = new Set(['release','cache','out','formats','timeout-ms','sections','window-seconds','provenance',
    'mode','detail','dry-voices','dry-max-bytes','dry-timeout-ms','examples','example-seed','solo-streams','mute-streams']);
  for (let i = 0; i < argv.length; i++) {
    if (!argv[i].startsWith('--')) { positional.push(argv[i]); continue; }
    const key = argv[i].slice(2);
    if (Object.hasOwn(opts,key)) fail('Duplicate option: --' + key);
    if (['offline','force','analysis','allow-development'].includes(key)) opts[key] = true;
    else if (valued.has(key) && i + 1 < argv.length && !argv[i+1].startsWith('--')) opts[key] = argv[++i];
    else fail('Unknown or incomplete option: --' + key);
  }
  const [site,command,inputFile] = positional;
  if (!site || !['setup','discover','compose','render','verify','analyze','export'].includes(command) || positional.length > 3)
    fail('Use --help for command syntax.');
  if (['setup','discover'].includes(command) ? inputFile : !inputFile) fail('Unexpected or missing input file.');
  if (['compose','export'].includes(command) && !opts.out) fail('This command requires --out STEM.');
  if (command !== 'discover' && (opts.examples !== undefined || opts['example-seed'] !== undefined)) fail('Example options belong only to discover.');
  if (opts['example-seed'] !== undefined && (opts.examples === undefined || !/^[\x20-\x7e]{1,128}$/.test(opts['example-seed'])))
    fail('An example seed requires --examples and 1..128 printable ASCII characters.');
  if (opts.examples !== undefined && (!/^[1-9][0-9]*$/.test(opts.examples) || Number(opts.examples) > 10)) fail('Example count must be 1..10.');
  if (opts.examples !== undefined && opts.offline) fail('Examples require the current online song catalog; omit --examples for offline discovery.');
  if (command === 'discover' && Object.keys(opts).some(key => !['release','cache','out','formats','force','timeout-ms','detail','offline','allow-development','examples','example-seed'].includes(key)))
    fail('Discover supports only release/cache, offline, deadline, detail, optional examples and JSON export options.');
  if (command === 'discover' && !opts.out && (opts.formats !== undefined || opts.force)) fail('Discover --formats and --force require --out STEM.');
  const formats = (opts.formats || (command === 'discover' ? 'json' : 'tl,json')).split(',');
  if (!formats.length || new Set(formats).size !== formats.length || formats.some(x => !['tl','json','wav','svg','link'].includes(x))) fail('Unknown or duplicate export format.');
  if (command === 'discover' && (formats.length !== 1 || formats[0] !== 'json')) fail('Discover exports JSON only.');
  const options = {formats:opts.out ? formats : [],timeoutMs:opts['timeout-ms'] === undefined ? 120000 : Number(opts['timeout-ms']),
    mode:opts.mode ?? 'verify',analyze:opts.analysis || command === 'analyze' || opts.sections !== undefined || opts['window-seconds'] !== undefined,
    ...(opts.sections ? {sections:JSON.parse(opts.sections)} : {}),
    ...(opts['window-seconds'] !== undefined ? {windowSeconds:Number(opts['window-seconds'])} : {}),
    ...(opts['dry-voices'] !== undefined ? {dryVoices:JSON.parse(opts['dry-voices'])} : {}),
    ...(opts['dry-max-bytes'] !== undefined ? {dryMaxBytes:Number(opts['dry-max-bytes'])} : {}),
    ...(opts['dry-timeout-ms'] !== undefined ? {dryTimeoutMs:Number(opts['dry-timeout-ms'])} : {}),
    ...(opts['solo-streams'] !== undefined ? {soloStreams:JSON.parse(opts['solo-streams'])} : {}),
    ...(opts['mute-streams'] !== undefined ? {muteStreams:JSON.parse(opts['mute-streams'])} : {}),
    ...(opts.provenance ? {provenance:JSON.parse(opts.provenance)} : {})};
  if (opts.detail !== undefined && !['compact','full'].includes(opts.detail)) fail('Detail must be compact or full.');
  validateOptions(command,options);
  const input = inputFile ? decoder.decode(await readRegular(path.resolve(inputFile),MAX_INPUT)) : null;
  if (opts.out && command !== 'setup') {
    const suffixes = command === 'discover' ? ['discovery.json'] : formats.map(format =>
      format === 'json' ? 'verification.json' : format === 'link' ? 'link.txt' : format);
    await checkOutputPaths(path.resolve(opts.out),suffixes,inputFile,opts.force);
  }
  const context = await setup({site,cache:opts.cache,release:opts.release,offline:opts.offline,allowDevelopment:opts['allow-development']});
  if (command === 'setup') { process.stdout.write(json({ready:true,renderer:context.engine.id,manifestSha256:context.manifestSha256,
    development:context.engine.development,offline:context.offline,revocationChecked:!context.offline,cachedFiles:context.entries.length,cache:context.folder})); return 0; }
  const examples = opts.examples === undefined ? null : await discoverExamples(context,Number(opts.examples),opts['example-seed'] ?? randomUUID());
  const answer = await execute(context,command,input,options);
  if (examples) answer.examples = examples;
  const discovering = command === 'discover';
  const result = discovering ? answer : answer.result;
  const artifacts = discovering ? (opts.out ? {'discovery.json':json(result)} : {}) : answer.artifacts;
  const reportSuffix = discovering ? 'discovery.json' : 'verification.json';
  const outputFiles = [];
  const successful = discovering || (result.verdict === 'valid' && (result.mode === 'listen' || result.verified));
  if (opts.out && successful) {
    const stem = path.resolve(opts.out);
    // Persist the report last so its output list describes completed exports.
    const suffixes = Object.keys(artifacts).filter(suffix => suffix !== reportSuffix);
    if (Object.hasOwn(artifacts,reportSuffix)) suffixes.push(reportSuffix);
    await checkOutputPaths(stem,suffixes,inputFile,opts.force);
    for (const suffix of suffixes) {
      const file = stem + '.' + suffix;
      const bytes = suffix === reportSuffix ? json({...result,outputFiles:[...outputFiles,file]}) : artifacts[suffix];
      try { await atomicWrite(file,bytes,opts.force); outputFiles.push(file); }
      catch (error) { fail('Export failed after writing ' + json(outputFiles).trim() + ': ' + error.message); }
    }
  }
  const full = {...result,outputFiles};
  process.stdout.write(json(opts.detail === 'full' ? full : discovering ? terminalDiscovery(full) : terminalReport(full)));
  return successful ? 0 : result.code || 1;
}

if (!isMainThread && workerData?.agentWorker) {
  try { parentPort.postMessage({value:await workerMain(workerData)}); }
  catch (error) { parentPort.postMessage({error:error.message}); }
} else if (process.argv[1] && await fs.realpath(process.argv[1]).catch(() => null) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = await main(); }
  catch (error) { process.stderr.write('JotTune: ' + error.message + '\n'); process.exitCode = 2; }
}
