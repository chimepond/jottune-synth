#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Interface checks against a built static player: failed links, library search
// and offline status. Playwright and a browser are supplied by the caller.
// Usage: node tests/browser_ui.mjs PLAYER_URL [--browser chromium|firefox|webkit] [--executable PATH] [--channel chrome]
import assert from 'node:assert/strict';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const args = process.argv.slice(2), base = args.shift(); let type = 'chromium', executablePath, channel;
while (args.length) {
  const option = args.shift();
  if (option === '--browser') type = args.shift(); else if (option === '--executable') executablePath = args.shift();
  else if (option === '--channel') channel = args.shift();
  else throw Error('Unknown browser test option.');
}
assert.ok(base && ['http:', 'https:'].includes(new URL(base).protocol), 'Usage: node tests/browser_ui.mjs PLAYER_URL [--browser TYPE]');
const module = process.env.PLAYWRIGHT_MODULE || 'playwright';
const playwright = await import(path.isAbsolute(module) ? pathToFileURL(module).href : module);
const browser = await playwright[type].launch({headless:true, ...(type === 'chromium' ? {args:['--mute-audio']} : {}), ...(executablePath ? {executablePath} : {}), ...(channel ? {channel} : {})});
const text = (page, id) => page.evaluate(id => document.getElementById(id).textContent.replace(/\s+/g, ' ').trim(), id);

async function withPage(name, run, {serviceWorkers = 'block', setup} = {}) {
  const context = await browser.newContext({viewport:{width:1280, height:800}, serviceWorkers});
  await setup?.(context);
  await context.addInitScript(() => { // Silent output; rendered PCM is unchanged.
    const Original = window.AudioContext || window.webkitAudioContext; if (!Original) return;
    class Quiet extends Original {
      constructor(...a) { super(...a); const mute = this.createGain(); mute.gain.value = 0; mute.connect(this.destination);
        Object.defineProperty(this, 'destination', {get:() => mute}); }
    }
    window.AudioContext = Quiet; if (window.webkitAudioContext) window.webkitAudioContext = Quiet;
  });
  const page = await context.newPage(), errors = [];
  page.setDefaultTimeout(60000); page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(base);
    await page.waitForFunction(() => typeof window.IntFM?.result === 'function');
    await run(page, context);
    assert.deepEqual(errors, []);
    console.log(`PASS ${type}: ${name}`);
  } finally { await context.close(); }
}

// UI-10: a failed link keeps the loaded song and explains the problem plainly.
await withPage('a failed link keeps the current song and shows a plain message', async page => {
  await page.waitForFunction(() => document.querySelector('#song-select input[type=radio]'));
  await page.locator('#song-select input[type=radio]').first().check();
  await page.locator('#song-load').click();
  await page.waitForFunction(() => !document.querySelector('#play').disabled);
  const before = await page.evaluate(() => ({record:document.getElementById('record').value, sha:IntFM.result().recordSha256,
    title:document.getElementById('selected-song-title').textContent, path:document.getElementById('library-path').textContent}));
  for (const [hash, expected] of [['#r=' + '0'.repeat(64), /not available/], ['#v1.%%%', /incomplete or damaged/],
    ['#song=songs%2Fmissing.tl&sha=' + '0'.repeat(64), /not available/]]) {
    await page.evaluate(hash => { location.hash = hash; }, hash);
    await page.waitForFunction(() => /unchanged/.test(document.getElementById('status').textContent));
    const status = await text(page, 'status');
    assert.match(status, expected); assert.match(status, /The current song is unchanged\.$/);
    assert.doesNotMatch(status, /\b404\b|returned|request|atob|decode/i);
    assert.equal(await text(page, 'rail-status'), status);
    const after = await page.evaluate(() => ({record:document.getElementById('record').value, sha:IntFM.result()?.recordSha256,
      title:document.getElementById('selected-song-title').textContent, path:document.getElementById('library-path').textContent,
      play:document.getElementById('play').disabled}));
    assert.deepEqual({...after, play:undefined}, {...before, play:undefined}); assert.equal(after.play, false);
    await page.evaluate(() => { history.replaceState(null, '', location.pathname); document.getElementById('status').textContent = ''; });
  }
});

// UI-3: a synthetic 100-song catalog served in place of the packaged one. Search
// continues past the first four-page chunk, counts use singular forms, genre
// choices are words, and a chosen collection narrows the genre choices.
const template = (await (await fetch(new URL('catalog/all-1.json', base))).json()).items[0].variants[0];
const titles = Array.from({length:100}, (_, index) => [3, 40, 41, 70, 99].includes(index) ? `Lantern ${index}`
  : index === 50 ? 'Solo piece' : `Song ${index}`);
const rows = titles.map((title, index) => {
  const genre = index === 10 ? 'dub-reggae' : 'warehouse-techno', path = `songs/synthetic-${index}.tl`;
  const variant = {kind:'standard', id:`examples/synthetic-${index}.tl`, path, bytes:template.bytes, duration_seconds:template.duration_seconds,
    record_sha256:template.record_sha256, technical:template.technical,
    discovery:{record_sha256:template.record_sha256, source:'music/library.json', metadata:{genre:{value:genre, source:'retained-score'}}}};
  return {title, collection:index === 10 ? 'Tiny' : 'Big', revision:'Original mix', id:variant.id, variant:'standard', path,
    bytes:variant.bytes, record_sha256:variant.record_sha256, duration_seconds:variant.duration_seconds, technical:variant.technical, variants:[variant]};
});
const groups = [['all', 'All songs', rows], ['featured', 'Favorites', []], ['collection-1', 'Big', rows.filter(row => row.collection === 'Big')],
  ['collection-2', 'Tiny', rows.filter(row => row.collection === 'Tiny')], ['genre-1', 'Genre: dub-reggae', [rows[10]]],
  ['genre-2', 'Genre: warehouse-techno', rows.filter((row, index) => index !== 10)]];
const documents = {'catalog/index.json':{version:1, page_size:24, total_songs:rows.length, categories:groups.map(([id, title, items]) =>
  ({id, title, count:items.length, first_page:items.length ? `catalog/${id}-1.json` : null}))}};
for (const [id, , items] of groups) for (let number = 1; (number - 1) * 24 < items.length; number++)
  documents[`catalog/${id}-${number}.json`] = {version:1, category:id, total:items.length, items:items.slice((number - 1) * 24, number * 24),
    next:number * 24 < items.length ? `catalog/${id}-${number + 1}.json` : null};
const serveCatalog = context => context.route(url => url.pathname.includes('/catalog/'), route => {
  const key = new URL(route.request().url()).pathname.replace(/^.*\/(catalog\/)/, '$1');
  return documents[key] ? route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(documents[key])}) : route.fulfill({status:404, body:''});
});
await withPage('search fills a result page across chunks, with readable genres narrowed by collection', async page => {
  const settled = () => page.waitForFunction(() => /in this source selection|More songs may match|of \d+ songs?$/.test(document.getElementById('library-status').textContent));
  await settled();
  const genres = () => page.locator('#song-genre option').evaluateAll(nodes => nodes.map(node => node.textContent));
  assert.deepEqual(await genres(), ['All genres', 'Dub reggae', 'Warehouse techno', 'Unknown']);
  await page.locator('#song-search').fill('lantern'); await settled();
  assert.match(await text(page, 'library-status'), /^Results 1–5\. Searched 100 of 100 songs\. 5 matches in this source selection\.$/);
  assert.equal(await page.locator('#song-select input[type=radio]').count(), 5);
  await page.locator('#song-search').fill('solo'); await settled();
  assert.match(await text(page, 'library-status'), /^Results 1–1\. Searched 100 of 100 songs\. 1 match in this source selection\.$/);
  await page.locator('#song-search').fill(''); await settled();
  await page.locator('#song-collection').selectOption('Tiny');
  await page.waitForFunction(() => document.querySelectorAll('#song-genre option').length === 3);
  assert.deepEqual(await genres(), ['All genres', 'Dub reggae', 'Unknown']);
  assert.match(await text(page, 'library-status'), /1 match in this source selection/);
  await page.locator('#song-genre').selectOption('value:dub-reggae'); await settled();
  assert.match(await text(page, 'library-path'), /Genre: Dub reggae/);
  await page.locator('#library-clear').click();
  await page.waitForFunction(() => document.querySelectorAll('#song-genre option').length === 4);
}, {setup:serveCatalog});

// UI-6: the first visit installs the offline app once per document; in Chromium a
// held installation reads "still running" after 15 s and then "ready". An offline
// reload still opens the example and plays it.
let held = null, release, shellFetches = 0;
await withPage('offline status follows a late installation and offline reload plays', async (page, context) => {
  const offlineStatus = () => text(page, 'offline-status');
  if (held) {
    await page.waitForTimeout(16000);
    assert.match(await offlineStatus(), /^Offline setup is still running; online playback is available now\.$/);
    release();
  }
  await page.waitForFunction(() => /Offline app is ready/.test(document.getElementById('offline-status').textContent), null, {timeout:90000});
  const keys = await page.evaluate(async () => {
    const name = (await caches.keys()).find(key => key.startsWith('intfm-workbench-'));
    return (await (await caches.open(name)).keys()).map(request => new URL(request.url).pathname.replace(/^.*\//, '') || './');
  });
  assert.ok(keys.includes('./') && keys.includes('index.html'), 'Both document keys are cached');
  if (held) assert.equal(shellFetches, 1, 'The worker fetches the document once for ./ and index.html');
  await page.evaluate(() => document.getElementById('example').click());
  await page.waitForFunction(() => IntFM.result()?.verdict === 'valid' && !document.getElementById('play').disabled);
  const sha = await page.evaluate(() => IntFM.result().pcmHash);
  await context.setOffline(true); await page.reload();
  await page.waitForFunction(() => typeof window.IntFM?.result === 'function');
  await page.evaluate(() => document.getElementById('example').click());
  await page.waitForFunction(() => IntFM.result()?.verdict === 'valid' && !document.getElementById('play').disabled);
  assert.equal(await page.evaluate(() => IntFM.result().pcmHash), sha);
  await page.locator('#play').click();
  await page.waitForFunction(() => document.getElementById('play').textContent === 'Pause');
  await context.setOffline(false);
}, {serviceWorkers:'allow', setup:async context => {
  if (type !== 'chromium') return; // Only Chromium routes service-worker requests.
  held = new Promise(resolve => { release = resolve; });
  await context.route(url => /\/(index\.html)?$/.test(url.pathname) || url.pathname.endsWith('/style.css'), async route => {
    if (!route.request().serviceWorker()) return route.continue();
    if (!route.request().url().endsWith('.css')) shellFetches++;
    else await held;
    return route.continue();
  });
}});

console.log(`BROWSER_UI ok ${type}`);
await browser.close();
