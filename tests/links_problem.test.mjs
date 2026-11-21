// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// A link that cannot be opened is described in plain language; the thrown error
// keeps its technical detail and its classification.
import assert from 'node:assert/strict';
import test from 'node:test';
import {resolveLink, linkProblem} from '../web/links.mjs';
import {limits} from '../web/policy.mjs';
import {deflateSync} from 'node:zlib';

const base = 'https://player.invalid/preview/';
async function failure(link, respond) {
  const original = globalThis.fetch;
  globalThis.fetch = async () => respond();
  try { await resolveLink(link, base); }
  catch (error) { return error; }
  finally { globalThis.fetch = original; }
  assert.fail('The link unexpectedly resolved.');
}

test('a missing record is reported as not available, not as an HTTP status', async () => {
  const error = await failure(base + '#r=' + '0'.repeat(64), () => new Response('', {status:404}));
  assert.match(error.message, /returned 404/);
  assert.equal(error.status, 404);
  const text = linkProblem(error);
  assert.match(text, /not available/); assert.doesNotMatch(text, /404|request|Record/);
});

test('a withdrawn or blocked song is distinguished from a temporary check failure', () => {
  assert.match(linkProblem({status:410}), /withdrawn, removed or expired/);
  assert.match(linkProblem({status:451}), /policy decision/);
  for (const error of [{unreachable:true},{status:503}])
    assert.doesNotMatch(linkProblem(error),/withdrawn|removed|policy decision/);
});

test('a server failure and an unreachable network get their own plain messages', async () => {
  const server = await failure(base + 'r/' + 'a'.repeat(64), () => new Response('', {status:503}));
  assert.match(linkProblem(server), /could not be fetched just now/);
  const offline = await failure(base + '#r=' + 'b'.repeat(64), () => { throw new TypeError('Failed to fetch'); });
  assert.equal(offline.unreachable, true);
  assert.match(linkProblem(offline), /could not be reached.*connection/);
});

test('a malformed link is described as incomplete or damaged', async () => {
  for (const link of [base + '#song=songs%2Fone.tl&sha=no', base + '#v1.' + '%%%', base + '#song=x&song=y']) {
    const error = await failure(link, () => new Response('unused'));
    assert.match(linkProblem(error), /incomplete or damaged/);
  }
});

test('an aborted request is not relabelled as unreachable', async () => {
  const error = await failure(base + '#r=' + 'c'.repeat(64), () => { throw new DOMException('stop', 'AbortError'); });
  assert.equal(error.name, 'AbortError'); assert.equal(error.unreachable, undefined);
});

test('a complete portable link reports the decoded song size limit', async () => {
  const encoded = deflateSync(Buffer.alloc(limits.recordBytes + 1, 65)).toString('base64url');
  const error = await failure(base + '#v1.' + encoded);
  assert.match(linkProblem(error), /1 MB input limit/);
});
