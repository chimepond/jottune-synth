// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// A visit reads bounded static pages, never the complete song manifest. Cursors
// retain positions, not old result pages; the caller can continue empty chunks.
import {documentSha256} from './tl-document.mjs';
const documentLimit = 262144;
const pagePath = value => typeof value === 'string' && /^catalog\/[a-z0-9-]+-\d+\.json$/.test(value);
const check = (condition, message = 'Invalid song catalog.') => {if (!condition) throw Error(message);};
const count = value => Number.isSafeInteger(value) && value >= 0 && value <= 1000000;
const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
export async function checkCatalogDocument(text, variant) {
  const nested=variant.discovery?.document_sha256;
  check(nested === undefined || digest(nested), 'Invalid song document checksum.');
  check(variant.document_sha256 === undefined || digest(variant.document_sha256), 'Invalid song document checksum.');
  check(variant.document_sha256 === undefined || nested === undefined || variant.document_sha256 === nested,
    'Song document checksums disagree.');
  const expected=variant.document_sha256 ?? nested ?? variant.record_sha256;
  if (expected !== undefined) {
    check(digest(expected), 'Invalid song document checksum.');
    check(await documentSha256(text) === expected,
      'Selected song document checksum does not match the catalog.');
  }
  return text;
}
export async function catalogJSON(base, path, signal) {
  check(path === 'catalog/index.json' || path === 'catalog/summary.json' || pagePath(path));
  const response = await fetch(new URL(path, base), {signal});
  if (!response.ok) throw Error('Song catalog is unavailable.');
  check(Number(response.headers?.get('content-length') || 0) <= documentLimit, 'Catalog page is too large.');
  const reader = response.body?.getReader();
  let text;
  if (reader) {
    const chunks = []; let bytes = 0;
    try {
      while (true) {
        signal?.throwIfAborted();
        const part = await reader.read(); if (part.done) break;
        bytes += part.value.byteLength;
        if (bytes > documentLimit) {await reader.cancel(); throw Error('Catalog page is too large.');}
        chunks.push(part.value);
      }
      const data = new Uint8Array(bytes); let offset = 0;
      for (const chunk of chunks) {data.set(chunk, offset); offset += chunk.byteLength;}
      text = new TextDecoder('utf-8', {fatal:true}).decode(data);
    } finally {reader.releaseLock();}
  } else {
    text = await response.text();
    check(new TextEncoder().encode(text).length <= documentLimit, 'Catalog page is too large.');
  }
  signal?.throwIfAborted();
  return JSON.parse(text);
}
export function createCatalog(base, validDiscovery) {
  let index, opening;
  const cache = new Map();
  function normalize(row) {
    check(row && typeof row.id === 'string' && row.id && typeof row.title === 'string'
      && typeof row.collection === 'string' && (row.revision === undefined || typeof row.revision === 'string'));
    // Flat defaults remain readable on older packaged catalogs.
    let variants = row.variants || [{kind:row.variant, id:row.id, path:row.path, bytes:row.bytes,
      duration_seconds:row.duration_seconds, record_sha256:row.record_sha256, technical:row.technical,
      ...(row.document_sha256 !== undefined ? {document_sha256:row.document_sha256} : {}),
      ...((row.metadata || row.provenance) ? {discovery:{record_sha256:row.record_sha256, source:'catalog',
        ...(row.metadata ? {metadata:row.metadata} : {}), ...(row.provenance ? {provenance:row.provenance} : {})}} : {})}];
    check(Array.isArray(variants) && variants.length >= 1 && variants.length <= 3);
    variants=variants.map(variant=>{
      const top=variant?.document_sha256,nested=variant?.discovery?.document_sha256;
      check((top===undefined || digest(top)) && (nested===undefined || digest(nested))
        && (top===undefined || nested===undefined || top===nested));
      return top===undefined && nested!==undefined ? {...variant,document_sha256:nested} : variant;
    });
    const kinds = new Set();
    for (const variant of variants) {
      check(variant && ['standard','keys','mirror'].includes(variant.kind) && !kinds.has(variant.kind)
        && typeof variant.path === 'string' && /^songs\/(?:[A-Za-z0-9._-]+\/)*[A-Za-z0-9._-]+\.tl$/.test(variant.path)
        && !variant.path.split('/').some(part => part === '.' || part === '..')
        && Number.isSafeInteger(variant.bytes) && variant.bytes >= 0 && Number.isFinite(variant.duration_seconds) && variant.duration_seconds >= 0
        && (variant.document_sha256 === undefined || digest(variant.document_sha256))
        && validDiscovery(variant));
      kinds.add(variant.kind);
    }
    check(kinds.has(row.variant));
    const selected = variants.find(variant => variant.kind === row.variant);
    check(selected.path === row.path && selected.record_sha256 === row.record_sha256
      && (row.document_sha256 === undefined || selected.document_sha256 === row.document_sha256));
    return {...row, id:variants.find(item => item.kind === 'standard')?.path || variants[0].path,
      defaultKind:row.variant, variants};
  }
  async function readIndex(signal) {
    const value = await catalogJSON(base, 'catalog/index.json', signal);
    check(value?.version === 1 && value.page_size === 24 && count(value.total_songs)
      && Array.isArray(value.categories) && value.categories.length <= 2048);
    const ids = new Set();
    for (const category of value.categories) {
      check(category && /^[a-z0-9-]+$/.test(category.id) && !ids.has(category.id)
        && typeof category.title === 'string' && count(category.count)
        && (category.first_page === null ? category.count === 0 : category.first_page === `catalog/${category.id}-1.json`));
      ids.add(category.id);
    }
    check(ids.has('all') && value.categories.find(category => category.id === 'all').count === value.total_songs);
    index = value; return value;
  }
  function open(signal) {
    opening ||= readIndex(signal).catch(error => {opening = null; throw error;});
    return opening;
  }
  async function page(path, category, signal) {
    if (cache.has(path)) return cache.get(path);
    const value = await catalogJSON(base, path, signal);
    const number = Number(path.match(/-(\d+)\.json$/)[1]);
    check(value?.version === 1 && value.category === category.id && value.total === category.count
      && Array.isArray(value.items) && value.items.length === Math.min(24, category.count - (number - 1) * 24)
      && value.next === (number * 24 < category.count ? `catalog/${category.id}-${number + 1}.json` : null));
    const rows = value.items.map(row => ({...normalize(row), catalogPage:path}));
    check(new Set(rows.map(row => row.id)).size === rows.length);
    const result = {...value, items:rows};
    signal?.throwIfAborted();
    cache.set(path, result);
    if (cache.size > 4) cache.delete(cache.keys().next().value);
    return result;
  }
  function findIn(rows, recordPath, sha) {
    for (const song of rows) {
      const variant = song.variants.find(item => (!recordPath || item.path === recordPath) && item.record_sha256 === sha);
      if (variant) return {song, variant};
    }
    return null;
  }
  function byHash(rows, sha) {
    const found = new Map();
    for (const song of rows) for (const variant of song.variants)
      if (variant.record_sha256 === sha) found.set(`${song.id}\n${variant.kind}`, {song, variant});
    return [...found.values()];
  }
  // A legacy or renamed library path names its catalogue song only when the hash
  // identifies one song, preferring the linked page; a shared hash is not guessed.
  function aliasIn(preferred, recordPath, sha) {
    const rows = [...cache.values()].flatMap(value => value.items);
    if (rows.some(song => song.variants.some(item => item.path === recordPath))) return null;
    const near = byHash(preferred, sha), found = near.length ? near : byHash(rows, sha);
    return found.length === 1 ? found[0] : null;
  }
  function cached(recordPath, sha, preferred = []) {
    for (const value of cache.values()) {
      const found = findIn(value.items, recordPath, sha); if (found) return found;
    }
    return recordPath ? aliasIn(preferred, recordPath, sha) : null;
  }
  async function lookup(hint, recordPath, sha, signal) {
    const value = await open(); signal?.throwIfAborted();
    const match = typeof hint === 'string' && hint.match(/^catalog\/([a-z0-9-]+)-([1-9][0-9]*)\.json$/);
    check(match, 'Catalog page hint is unavailable.');
    const category = value.categories.find(item => item.id === match[1]);
    check(category && Number(match[2]) <= Math.ceil(category.count / 24), 'Catalog page hint is unavailable.');
    const result = await page(hint, category, signal); signal?.throwIfAborted();
    return findIn(result.items, recordPath, sha) || cached(recordPath, sha, result.items);
  }
  async function browse(category, matches, cursor = null, signal, want = 8) {
    check(index && index.categories.includes(category));
    const start = cursor || {path:category.first_page, offset:0, examined:0, matched:0};
    let path = start.path, offset = start.offset, examined = start.examined, matched = start.matched;
    let pagesRead = 0, pageNumber = path ? Number(path.match(/-(\d+)\.json$/)[1]) : 0;
    const items = [];
    // At most four static requests per explicit browse step, even for no-match searches.
    while (path && items.length < want && pagesRead < 4) {
      signal?.throwIfAborted();
      const value = await page(path, category, signal); pagesRead++;
      pageNumber = Number(path.match(/-(\d+)\.json$/)[1]);
      while (offset < value.items.length && items.length < want) {
        const song = value.items[offset++]; examined++;
        const variants = matches(song);
        if (variants.length) {items.push({song, variants}); matched++;}
      }
      if (offset === value.items.length) {path = value.next; offset = 0;}
    }
    return {items, start, next:path ? {path, offset, examined, matched} : null, examined, matched,
      pageNumber, pageCount:Math.ceil(category.count / 24), total:category.count};
  }
  return {open, browse, cached, lookup};
}
