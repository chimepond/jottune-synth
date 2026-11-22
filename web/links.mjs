// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import {limits, options} from './policy.mjs';

class LinkSizeError extends Error {}

async function boundedText(response) {
  if (!response.ok) throw Object.assign(Error('Record request returned ' + response.status), {status:response.status});
  if (Number(response.headers.get('content-length')) > limits.recordBytes) throw new LinkSizeError('Record exceeds the 1 MB input limit.');
  const reader = response.body.getReader(), chunks = [];
  let size = 0;
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.length;
    if (size > limits.recordBytes) { await reader.cancel(); throw new LinkSizeError('Record exceeds the 1 MB input limit.'); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return new TextDecoder('utf-8', {fatal:true,ignoreBOM:true}).decode(bytes);
}

function httpURL(value, base) {
  const url = new URL(value, base);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password)
    throw Error('Use an HTTP or HTTPS record URL without embedded credentials.');
  return url;
}

// A request that never reached a response is marked so the page can say so plainly.
async function request(url, init) {
  try { return await fetch(url, init); }
  catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw Object.assign(Error(error?.message || 'Record request failed.'), {unreachable:true});
  }
}

export async function fetchRecord(value, base, {signal} = {}) {
  return boundedText(await request(httpURL(value, base), {signal}));
}

// Plain-language text for a link that could not be opened. The thrown error
// keeps its technical detail for programmatic callers.
export function linkProblem(error) {
  if (error instanceof LinkSizeError) return error.message;
  if (error?.portableUnsupported) return 'This browser cannot open compressed song links. Ask for the .tl file and open it in the player.';
  if (Number.isSafeInteger(error?.portableCharacters))
    return 'This link is incomplete or damaged ('+error.portableCharacters+' fragment characters received). Ask for a new link or the .tl file.';
  if (error?.verificationStatus === 'pending')
    return 'This publication is waiting for independent technical verification. Its share link is not ready for playback. Check again later.';
  if (error?.verificationStatus === 'failed') {
    const reasons = {
      'silent-render':'the rendered audio is completely silent (silent-render)',
      'reader-refused':'the native reader refused the song',
      'profile-refused':'the song does not meet the publication profile',
      'hash-mismatch':'the rendered audio did not match its saved identity',
      'noncanonical':'the saved record was not canonical',
      'declared-mismatch':'the rendered audio did not match the declared audio facts',
      'render-timeout':'the independent render timed out',
      'render-failed':'the independent render failed',
    };
    return 'This publication failed independent technical verification' +
      (Object.hasOwn(reasons, error.verificationFailure) ? ' because ' + reasons[error.verificationFailure] : '') +
      '. Its share link is not available for playback.';
  }
  if (error?.status === 410) return 'This song is not available. It may have been withdrawn, removed or expired.';
  if (error?.status === 451) return 'This song is unavailable following a policy decision.';
  if (error?.status === 404)
    return 'This link points to a song that is not available. It may have been removed, or the link may be incomplete.';
  if (error?.status) return 'The linked song could not be fetched just now. Try the link again later.';
  if (error?.unreachable) return 'The linked song could not be reached. Check your connection and try the link again.';
  return 'This link is incomplete or damaged, so no song could be opened from it.';
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

export async function resolveLink(value, base, {signal} = {}) {
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
    return {recordText:await fetchRecord(path, new URL('./', url), {signal}), recordSha256:sha, release,
      libraryPath:path, catalogPage:catalogPageHint(fields.get('catalog'))};
  }
  if (url.hash.startsWith('#v1.')) {
    const encoded = url.hash.slice(4);
    if (encoded.length > limits.fragmentCharacters) throw new LinkSizeError('This song link exceeds the player’s link length limit. Ask for the .tl file.');
    if (typeof DecompressionStream !== 'function') {
      const error=Object.assign(Error(),{portableUnsupported:true}); error.message=linkProblem(error); throw error;
    }
    try {
      const bytes = Uint8Array.from(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')), character => character.charCodeAt(0));
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate'));
      return {recordText: await boundedText(new Response(stream)), release};
    } catch (cause) {
      if (cause instanceof LinkSizeError) throw cause;
      const error=Object.assign(Error(),{portableCharacters:url.hash.length});error.message=linkProblem(error);throw error;
    }
  }
  if (/^#r=[0-9a-f]{64}$/.test(url.hash)) {
    const recordSha256 = url.hash.slice(3);
    return {recordText: await fetchRecord('r/' + recordSha256 + '.tl', new URL('./', url), {signal}), recordSha256, release, publication:{}};
  }
  const staticRecord = url.pathname.match(/\/records\/([0-9a-f]{64})\.tl$/);
  if (staticRecord) return {recordText:await fetchRecord(url, undefined, {signal}),recordSha256:staticRecord[1],release};
  const record = url.pathname.match(/^(.*\/)r\/([0-9a-f]{64})\/?$/);
  const submission = url.pathname.match(/^(.*\/)s\/([A-Za-z0-9_-]{1,128})\/?$/);
  if (!record && !submission) return {recordText: await fetchRecord(url, undefined, {signal}), release};
  const serviceBase = new URL((record || submission)[1], url);
  const endpoint = new URL('api/' + (record ? 'records/' + record[2] : 'submissions/' + submission[2]), serviceBase);
  if (release) endpoint.searchParams.set('release', release);
  const descriptor = JSON.parse(await boundedText(await request(endpoint, {signal, cache:'no-store'})));
  if (signal?.aborted) throw new DOMException('Loading cancelled.', 'AbortError');
  if (!descriptor || !/^[0-9a-f]{64}$/.test(descriptor.record_sha256)
      || typeof descriptor.record_url !== 'string' || typeof descriptor.renderer_release !== 'string')
    throw Error('The service did not return a complete record descriptor.');
  const namedRelease = options({release: descriptor.renderer_release}).release;
  if (record && record[2] !== descriptor.record_sha256) throw Error('Record descriptor differs from the requested content identity.');
  if (release && release !== namedRelease) throw Error('Record descriptor differs from the requested renderer release.');
  const recordURL = httpURL(descriptor.record_url, serviceBase);
  if (recordURL.origin !== endpoint.origin || !recordURL.pathname.startsWith(serviceBase.pathname))
    throw Error('Record descriptors must reference their own service origin and path prefix.');
  const verificationStatus = descriptor.verification?.status;
  if (verificationStatus === 'pending' || verificationStatus === 'failed') {
    const error = Object.assign(Error(), {verificationStatus, verificationFailure:descriptor.verification.failure, recordSha256:descriptor.record_sha256, serviceBase:serviceBase.href});
    error.message = linkProblem(error); throw error;
  }
  if (verificationStatus !== undefined && verificationStatus !== 'verified')
    throw Error('The service returned an invalid verification status.');
  const publication = {};
  if (typeof descriptor.listed === 'boolean') publication.listed = descriptor.listed;
  if (['approved','declined','pending','not_required'].includes(descriptor.review?.status)) publication.reviewStatus = descriptor.review.status;
  return {recordText: await fetchRecord(recordURL, undefined, {signal}), recordSha256: descriptor.record_sha256, publication,
    release: namedRelease, submissionId: submission?.[2]};
}

export async function portableLink(canonical, base, release) {
  if (typeof CompressionStream !== 'function')
    throw Error('This browser cannot create compressed song links. Save and share the .tl file instead.');
  const compressed = await new Response(new Blob([canonical]).stream().pipeThrough(new CompressionStream('deflate'))).arrayBuffer();
  let binary = '';
  for (const byte of new Uint8Array(compressed)) binary += String.fromCharCode(byte);
  const url = new URL(base);
  url.search = ''; url.hash = 'v1.' + btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  if (release) url.searchParams.set('release', options({release}).release);
  return url.href;
}
