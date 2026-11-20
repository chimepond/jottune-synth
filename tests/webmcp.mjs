#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Usage: node tests/webmcp.mjs PLAYER_URL [--channel chrome | --executable PATH]
// Supply Playwright via PLAYWRIGHT_MODULE or an installed playwright package.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {on, once} from 'node:events';
import path from 'node:path';
import {parseArgs} from 'node:util';
import {pathToFileURL} from 'node:url';
import {inflateSync} from 'node:zlib';

const {values, positionals} = parseArgs({allowPositionals:true,
  options:{channel:{type:'string'}, executable:{type:'string'}}});
assert.equal(positionals.length, 1, 'Supply the URL of a staged player with a kept renderer.');
assert.ok(!(values.channel && values.executable), 'Choose one Chromium installation.');
const base = new URL(positionals[0]);
const module = process.env.PLAYWRIGHT_MODULE || 'playwright';
const {chromium} = await import(path.isAbsolute(module) ? pathToFileURL(module).href : module);
const browser = await chromium.launch({headless:true, args:['--enable-features=WebMCP', '--mute-audio'],
  ...(values.executable ? {executablePath:values.executable} : {channel:values.channel || 'chrome'})});
const sha = bytes => createHash('sha256').update(bytes).digest('hex');

try {
  const context = await browser.newContext({serviceWorkers:'block'});
  const page = await context.newPage(), session = await context.newCDPSession(page);
  assert.ok((await page.goto(base.href)).ok(), 'Open the staged player.');
  await page.waitForFunction(() => window.IntFM?.describe()?.webmcp?.intfm_compose === 'registered' &&
    window.IntFM.describe().webmcp.intfm_render === 'registered');
  const {frameTree} = await session.send('Page.getFrameTree');
  const tools = new Map();
  const added = on(session, 'WebMCP.toolsAdded', {signal:AbortSignal.timeout(30000)});
  await session.send('WebMCP.enable');
  for await (const [event] of added) {
    for (const tool of event.tools) if (tool.frameId === frameTree.frame.id) tools.set(tool.name, tool);
    if (tools.has('intfm_compose') && tools.has('intfm_render')) break;
  }
  assert.ok(tools.get('intfm_compose').inputSchema.properties.score);
  assert.ok(tools.get('intfm_render').inputSchema.properties.recordText);

  async function invoke(name, input) {
    const {toolId, frameId} = tools.get(name);
    const [[response], {invocationId}] = await Promise.all([
      once(session, 'WebMCP.toolResponded', {signal:AbortSignal.timeout(60000)}),
      session.send('WebMCP.invokeTool', {frameId, ...(toolId ? {toolId} : {toolName:name}), input})
    ]);
    assert.equal(response.invocationId, invocationId);
    assert.equal(response.status, 'Completed', response.errorText);
    assert.equal(response.output.content.length, 1);
    const [block] = response.output.content;
    assert.equal(block.type, 'text');
    assert.ok(!response.output.isError, block.text);
    return JSON.parse(block.text);
  }

  const response = await page.request.get(new URL('engines.json', base).href);
  assert.ok(response.ok(), 'Fetch the staged renderer registry.');
  const registry = await response.json();
  const kept = registry.engines.filter(row => row.release_id && !row.withdrawn && !row.withdrawn_at && !row.development);
  const requested = base.searchParams.get('release');
  const release = requested ?? kept.find(row => row.id === registry.default)?.id ?? kept.at(-1)?.id;
  assert.ok(kept.some(row => row.id === release), 'Supply a player with an available kept release.');
  const score = {version:1, fs:48000, metadata:{title:'Étude for WebMCP'},
    clock:{bars:1, meter:[{bar:1,quarters:4}], tempo:[{bar:1,bpm:120}], ramp:'step', tail_ns:150000000},
    declarations:'space stereo\nblock 64\nnear 100\nrecipe lead marimba\n', streams:['lead'], voices:['lead'],
    events:[{kind:'note', at:0, duration:'1/2', stream:'lead', voice:0, pitch:{note:'C5'}, velocity_q15:22000}]};
  const short = await invoke('intfm_compose', {score, release});
  assert.equal(short.summary.verified, true);
  assert.equal(short.title, score.metadata.title);
  const inline = new URL(short.url);
  assert.match(inline.hash, /^#v1\./);
  assert.equal(inline.searchParams.get('release'), release);
  assert.equal(short.tl.nextOffset, null);
  assert.deepEqual(inflateSync(Buffer.from(inline.hash.slice(4), 'base64url')), Buffer.from(short.tl.base64, 'base64'));

  // The larger score forces continuation of both the canonical TL and its compressed URL.
  score.events = Array.from({length:256}, (_, index) => ({kind:'note', at:index + '/64',
    duration:'1/64', stream:'lead', voice:0, pitch:{midi:48 + (index * 7 % 35)},
    velocity_q15:14000 + (index * 3911 % 16000)}));
  const {summary, tl, url, urlBytes} = await invoke('intfm_compose', {score, release});
  assert.equal(summary.verdict, 'valid');
  assert.equal(summary.verified, true);
  assert.equal(summary.play.match, 'yes');
  assert.equal(summary.play.block_indep, 'yes');
  assert.equal(summary.rendererRelease, release);
  assert.equal(url, null);
  assert.ok(urlBytes > 0);
  async function collect(format, first) {
    const {documentSha256} = summary, pieces = [];
    let offset = 0;
    do {
      const chunk = first ?? (await invoke('intfm_compose', {documentSha256, offset, format}))[format];
      first = undefined;
      assert.equal(chunk.format, format);
      assert.equal(chunk.encoding, 'base64');
      assert.equal(chunk.documentSha256, documentSha256);
      assert.equal(chunk.verified, true);
      assert.equal(chunk.totalBytes, format === 'tl' ? summary.canonicalBytes : urlBytes);
      assert.equal(chunk.offset, offset);
      const bytes = Buffer.from(chunk.base64, 'base64');
      assert.ok(bytes.length > 0 && bytes.length <= 3000);
      assert.equal(sha(bytes), chunk.chunkSha256);
      pieces.push(bytes);
      offset += bytes.length;
      assert.ok(offset <= chunk.totalBytes);
      assert.equal(chunk.nextOffset, offset === chunk.totalBytes ? null : offset);
      if (chunk.nextOffset === null) {
        assert.ok(pieces.length > 1, format + ' used multiple tool replies');
        const payload = Buffer.concat(pieces);
        assert.equal(sha(payload), chunk.payloadSha256);
        return payload;
      }
    } while (true);
  }
  const record = await collect('tl', tl);
  assert.equal(sha(record), summary.documentSha256);
  const link = new URL((await collect('link')).toString('utf8'));
  assert.equal(link.origin, base.origin);
  assert.equal(link.searchParams.get('release'), release);
  assert.match(link.hash, /^#v1\./);
  assert.deepEqual(inflateSync(Buffer.from(link.hash.slice(4), 'base64url')), record);

  const opened = await context.newPage();
  await opened.goto(link.href);
  await opened.waitForFunction(() => window.IntFM?.result()?.verdict === 'valid' && !document.getElementById('play').disabled,
    null, {timeout:60000});
  const replay = await opened.evaluate(() => window.IntFM.summary());
  for (const field of ['documentSha256', 'recordSha256', 'pcmHash', 'rendererRelease']) assert.equal(replay[field], summary[field], field);
  const rendered = await invoke('intfm_render', {recordText:record.toString('utf8'), release});
  assert.equal(rendered.verified, true);
  assert.equal(rendered.pcmHash, summary.pcmHash);
  assert.equal(rendered.recordSha256, summary.recordSha256);
  console.log(`PASS Chromium ${browser.version()} WebMCP discovery, compose, chunk checksums, pinned link playback and verified TL render (${record.length} TL bytes, ${urlBytes} URL bytes, release ${release}).`);
} finally { await browser.close(); }
