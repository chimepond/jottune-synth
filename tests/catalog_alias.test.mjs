// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Linked-song lookup: an exact path wins; a legacy path names its catalogue song
// only when the hash identifies one song; an ambiguous or unknown hash stays unnamed.
import assert from 'node:assert/strict';
import test from 'node:test';
import {createCatalog} from '../web/catalog.mjs';

const sha = name => name.repeat(64).slice(0, 64);
const song = (id, title, collection, hashes) => {
  const variants = ['standard', 'keys'].map((kind, index) => ({kind, id:`examples/${id}-${kind}.tl`,
    path:`songs/${id}-${kind}.tl`, bytes:10, duration_seconds:1, record_sha256:hashes[index]}));
  return {id:`examples/${id}-keys.tl`, title, collection, variant:'keys', path:variants[1].path,
    record_sha256:hashes[1], variants};
};
const A = song('alpha', 'Alpha', 'One', [sha('1'), sha('2')]);
const B = song('bravo', 'Bravo', 'Two', [sha('3'), sha('4')]);
const C = song('charlie', 'Charlie', 'One', [sha('5'), sha('d')]);
const D = song('delta', 'Delta', 'Two', [sha('6'), sha('d')]);
const pages = {all:[A, B, C, D], featured:[A, C]};
const documents = {
  'catalog/index.json': {version:1, page_size:24, total_songs:4, categories:Object.entries(pages).map(([id, items]) =>
    ({id, title:id, count:items.length, first_page:`catalog/${id}-1.json`}))},
  ...Object.fromEntries(Object.entries(pages).map(([id, items]) =>
    [`catalog/${id}-1.json`, {version:1, category:id, total:items.length, items, next:null}])),
};
globalThis.fetch = async url => {
  const value = documents[new URL(url).pathname.slice(1)];
  return value ? new Response(JSON.stringify(value)) : new Response('', {status:404});
};
const fresh = () => createCatalog('http://catalog.invalid/', () => true);
const named = found => found && `${found.song.title}/${found.variant.kind}`;

test('an exact path and hash select that song', async () => {
  const catalog = fresh();
  assert.equal(named(await catalog.lookup('catalog/all-1.json', 'songs/alpha-keys.tl', sha('2'))), 'Alpha/keys');
  assert.equal(named(catalog.cached('songs/bravo-standard.tl', sha('3'))), 'Bravo/standard');
});

test('a legacy alias path with a unique catalogue hash selects that song', async () => {
  const catalog = fresh();
  assert.equal(named(await catalog.lookup('catalog/all-1.json', 'songs/music-alpha-keys.tl', sha('2'))), 'Alpha/keys');
  assert.equal(named(catalog.cached('songs/music-bravo-keys.tl', sha('4'))), 'Bravo/keys');
});

test('a hash shared by several songs is not guessed unless the linked page has one', async () => {
  const catalog = fresh();
  assert.equal(await catalog.lookup('catalog/all-1.json', 'songs/music-charlie-keys.tl', sha('d')), null);
  assert.equal(catalog.cached('songs/music-charlie-keys.tl', sha('d')), null);
  assert.equal(named(await catalog.lookup('catalog/featured-1.json', 'songs/music-charlie-keys.tl', sha('d'))), 'Charlie/keys');
  // An exact path still decides between songs that share a hash.
  assert.equal(named(catalog.cached('songs/delta-keys.tl', sha('d'))), 'Delta/keys');
});

test('a wrong hash selects nothing, for exact and alias paths', async () => {
  const catalog = fresh();
  assert.equal(await catalog.lookup('catalog/all-1.json', 'songs/alpha-keys.tl', sha('f')), null);
  assert.equal(await catalog.lookup('catalog/all-1.json', 'songs/music-alpha-keys.tl', sha('f')), null);
  // A current catalogue path with another song's hash is not re-labelled as that song.
  assert.equal(catalog.cached('songs/alpha-keys.tl', sha('4')), null);
});

test('a link to the wrong collection page selects only songs already read', async () => {
  const catalog = fresh();
  assert.equal(await catalog.lookup('catalog/featured-1.json', 'songs/bravo-keys.tl', sha('4')), null);
  assert.equal(await catalog.lookup('catalog/featured-1.json', 'songs/music-bravo-keys.tl', sha('4')), null);
  await catalog.lookup('catalog/all-1.json', 'songs/alpha-keys.tl', sha('2'));
  assert.equal(named(await catalog.lookup('catalog/featured-1.json', 'songs/music-bravo-keys.tl', sha('4'))), 'Bravo/keys');
  await assert.rejects(catalog.lookup('catalog/missing-1.json', 'songs/alpha-keys.tl', sha('2')), /hint is unavailable/);
});
