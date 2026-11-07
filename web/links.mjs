// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import {limits, options} from './policy.mjs';

async function boundedText(response) {
  if (!response.ok) throw Error('Record request returned ' + response.status);
  if (Number(response.headers.get('content-length')) > limits.recordBytes) throw Error('Record exceeds the 1 MB input limit.');
  const reader = response.body.getReader(), chunks = [];
  let size = 0;
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.length;
    if (size > limits.recordBytes) { await reader.cancel(); throw Error('Record exceeds the 1 MB input limit.'); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return new TextDecoder('utf-8', {fatal: true}).decode(bytes);
}

function httpURL(value, base) {
  const url = new URL(value, base);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password)
    throw Error('Use an HTTP or HTTPS record URL without embedded credentials.');
  return url;
}

export async function fetchRecord(value, base, {signal} = {}) {
  return boundedText(await fetch(httpURL(value, base), {signal}));
}

export function isRecordLink(url) {
  return /\/(?:r\/[0-9a-f]{64}|s\/[A-Za-z0-9_-]{1,128})\/?$/.test(url.pathname)
    || /\/records\/[0-9a-f]{64}\.tl$/.test(url.pathname)
    || url.hash.startsWith('#v1.') || url.hash.startsWith('#song=') || /^#r=[0-9a-f]{64}$/.test(url.hash);
}

function libraryReference(path, sha) {
  if (typeof path !== 'string' || !/^songs\/[A-Za-z0-9][A-Za-z0-9._-]*\.tl$/.test(path)
      || typeof sha !== 'string' || !/^[a-f0-9]{64}$/.test(sha))
    throw Error('Use a saved library song with its exact content identity.');
  return {path, sha};
}
const catalogPageHint = value => typeof value === 'string' && /^catalog\/[a-z0-9-]+-[1-9][0-9]*\.json$/.test(value)
  && value.length <= 160 ? value : undefined;

// A reference to already hosted bytes. Local edits continue to use portableLink.
export function libraryLink(path, sha, base, release, catalogPage) {
  libraryReference(path, sha);
  const url = httpURL(base);
  url.search = ''; url.hash = new URLSearchParams({song:path, sha}).toString();
  if (catalogPage !== undefined) {
    if (!catalogPageHint(catalogPage)) throw Error('Invalid library catalog page hint.');
    url.hash = new URLSearchParams({song:path, sha, catalog:catalogPage}).toString();
  }
  if (release) url.searchParams.set('release', options({release}).release);
  return url.href;
}

export async function resolveLink(value, base) {
  const url = httpURL(value, base);
  const release = url.searchParams.has('release') ? options({release: url.searchParams.get('release')}).release : undefined;
  if (url.hash.startsWith('#song=')) {
    const fields = new URLSearchParams(url.hash.slice(1));
    if ([...fields.keys()].some(key => !['song','sha','catalog'].includes(key))
        || fields.getAll('song').length !== 1 || fields.getAll('sha').length !== 1 || fields.getAll('catalog').length > 1)
      throw Error('Invalid library song link.');
    const {path, sha} = libraryReference(fields.get('song'), fields.get('sha'));
    // This is only a bounded metadata hint. Invalid hints never change which
    // TL is fetched or prevent its independent saved-identity verification.
    return {recordText:await fetchRecord(path, new URL('./', url)), recordSha256:sha, release,
      libraryPath:path, catalogPage:catalogPageHint(fields.get('catalog'))};
  }
  if (url.hash.startsWith('#v1.')) {
    const encoded = url.hash.slice(4);
    if (encoded.length > limits.fragmentCharacters) throw Error('Record link is too large.');
    const bytes = Uint8Array.from(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')), character => character.charCodeAt(0));
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate'));
    return {recordText: await boundedText(new Response(stream)), release};
  }
  if (/^#r=[0-9a-f]{64}$/.test(url.hash)) {
    const recordSha256 = url.hash.slice(3);
    return {recordText: await fetchRecord('r/' + recordSha256 + '.tl', new URL('./', url)), recordSha256, release};
  }
  const staticRecord = url.pathname.match(/\/records\/([0-9a-f]{64})\.tl$/);
  if (staticRecord) return {recordText:await fetchRecord(url),recordSha256:staticRecord[1],release};
  const record = url.pathname.match(/^(.*\/)r\/([0-9a-f]{64})\/?$/);
  const submission = url.pathname.match(/^(.*\/)s\/([A-Za-z0-9_-]{1,128})\/?$/);
  if (!record && !submission) return {recordText: await fetchRecord(url), release};
  const serviceBase = new URL((record || submission)[1], url);
  const endpoint = new URL('api/' + (record ? 'records/' + record[2] : 'submissions/' + submission[2]), serviceBase);
  if (release) endpoint.searchParams.set('release', release);
  const descriptor = JSON.parse(await boundedText(await fetch(endpoint)));
  if (!descriptor || !/^[0-9a-f]{64}$/.test(descriptor.record_sha256)
      || typeof descriptor.record_url !== 'string' || typeof descriptor.renderer_release !== 'string')
    throw Error('The service did not return a complete record descriptor.');
  const namedRelease = options({release: descriptor.renderer_release}).release;
  if (record && record[2] !== descriptor.record_sha256) throw Error('Record descriptor differs from the requested content identity.');
  if (release && release !== namedRelease) throw Error('Record descriptor differs from the requested renderer release.');
  const recordURL = httpURL(descriptor.record_url, serviceBase);
  if (recordURL.origin !== endpoint.origin || !recordURL.pathname.startsWith(serviceBase.pathname))
    throw Error('Record descriptors must reference their own service origin and path prefix.');
  return {recordText: await fetchRecord(recordURL), recordSha256: descriptor.record_sha256,
    release: namedRelease, submissionId: submission?.[2]};
}

export async function portableLink(canonical, base, release) {
  const compressed = await new Response(new Blob([canonical]).stream().pipeThrough(new CompressionStream('deflate'))).arrayBuffer();
  let binary = '';
  for (const byte of new Uint8Array(compressed)) binary += String.fromCharCode(byte);
  const url = new URL(base);
  url.search = ''; url.hash = 'v1.' + btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  if (release) url.searchParams.set('release', options({release}).release);
  return url.href;
}
