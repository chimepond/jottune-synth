// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import assert from 'node:assert/strict';
import test from 'node:test';
import {qrSvg, QR_MAX_BYTES} from '../web/qr.mjs';

const identity = '0123456789abcdef'.repeat(4);
const prefix = 'https://example.test/2026-10-26-0123456789abcdef/';
const link = prefix + 'r/' + identity + '?release=2026-10-26-abcdef0';

function finder(matrix, left, top) {
  for (let y = 0; y < 7; y++) for (let x = 0; x < 7; x++) {
    const dark = x === 0 || x === 6 || y === 0 || y === 6 ||
      (x >= 2 && x <= 4 && y >= 2 && y <= 4);
    assert.equal(matrix[top+y][left+x], dark, `Finder module ${left+x},${top+y}`);
  }
}

test('canonical song links retain their prefix, identity and kept release', () => {
  const qr = qrSvg(link);
  assert.equal(qr.url, link);
  assert.equal(qr.size, 17 + qr.version * 4);
  assert.ok(qr.version >= 1 && qr.version <= 40);
  assert.equal(qr.matrix.length, qr.size);
  assert.ok(qr.matrix.every(row => row.length === qr.size && row.every(value => typeof value === 'boolean')));
  finder(qr.matrix, 0, 0);
  finder(qr.matrix, qr.size - 7, 0);
  finder(qr.matrix, 0, qr.size - 7);
});

test('automatic version selection starts small and permits the complete byte-mode M capacity', () => {
  assert.equal(qrSvg('http://a').version, 1);
  const start = 'https://example.test/#';
  const maximum = start + 'a'.repeat(QR_MAX_BYTES - start.length);
  const qr = qrSvg(maximum);
  assert.equal(qr.url, maximum);
  assert.equal(qr.version, 40);
  assert.equal(qr.size, 177);
  assert.throws(() => qrSvg(maximum + 'a'), {name:'RangeError', message:/too large.*2331/});
  // URL normalization percent-encodes non-ASCII path bytes; count those too.
  assert.throws(() => qrSvg(start + 'é'.repeat(400)), {name:'RangeError'});
});

test('SVG supplies a white four-module quiet zone and only fixed safe markup', () => {
  const qr = qrSvg(prefix + '#<svg/onload=alert(1)>&"\'');
  assert.equal(qr.quietZone, 4);
  const edge = qr.size + 8;
  assert.ok(qr.svg.includes(`viewBox="0 0 ${edge} ${edge}"`));
  assert.ok(qr.svg.includes('<rect width="100%" height="100%" fill="#fff"/>'));
  assert.ok(qr.svg.includes('shape-rendering="crispEdges"'));
  assert.ok(qr.svg.includes('aria-label="Song link QR code"'));
  assert.ok(!qr.svg.includes('onload') && !qr.svg.includes(qr.url));
  const modules = [...qr.svg.matchAll(/M(\d+),(\d+)h1v1h-1z/g)];
  assert.equal(modules.length, qr.matrix.flat().filter(Boolean).length);
  for (const [, x, y] of modules) {
    assert.ok(Number(x) >= 4 && Number(x) < qr.size + 4);
    assert.ok(Number(y) >= 4 && Number(y) < qr.size + 4);
  }
});

test('portable fragments and Unicode URLs are normalized without losing their payload', () => {
  const fragment = '#tl=eJyrVkrLz1eyUkpKLFKqBQA_1-2';
  assert.equal(qrSvg(prefix + fragment).url, prefix + fragment);
  const unicode = 'https://münich.example/été?q=♪#résumé';
  assert.equal(qrSvg(unicode).url, new URL(unicode).href);
});

test('unsafe, incomplete and credential-bearing destinations are refused', () => {
  for (const value of [null, {}, '', '/r/' + identity, '//example.test/r/a',
    'data:text/plain,hello', 'javascript:alert(1)', 'file:///song.tl', 'ftp://example.test/a',
    'https:example.test', 'https://', 'https://user@example.test/', 'https://user:secret@example.test/',
    'https://@example.test/', 'https://example.test/\n#wrong', ' https://example.test/',
    'https://example.test/a b', 'https://example.test/\\different'])
    assert.throws(() => qrSvg(value), TypeError);
});

test('encoding is deterministic and callers cannot change a later result', () => {
  const first = qrSvg(link), again = qrSvg(link);
  assert.deepEqual(first, again);
  first.matrix[0][0] = false;
  assert.equal(qrSvg(link).matrix[0][0], true);
});
