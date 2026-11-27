#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// One {source, output, title, collection, profile} request per stdin line.
import {spawnSync} from 'node:child_process';
import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import path from 'node:path';
import {createInterface} from 'node:readline';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {row} from '../web/runtime.mjs';

const web = fileURLToPath(new URL('../web/', import.meta.url));
const origin = 'http://artwork.invalid';
const files = new Map([
  ['/style.css', 'text/css'],
  ['/instrument-view.mjs', 'text/javascript'],
  ['/spatial-view.mjs', 'text/javascript'],
  ['/media.mjs', 'text/javascript'],
  ['/fonts/Michroma-Regular.woff2', 'font/woff2'],
]);
const html = `<!doctype html><html lang="en"><meta charset="utf-8">
<link rel="stylesheet" href="/style.css"><style>
body {margin:0}
main {width:1200px;height:1200px;padding:80px 65px 66px;display:flex;flex-direction:column;
  color:#f6f3e8;background:#101a20;font:500 28px/1.35 Arial,sans-serif}
header, footer {display:flex;align-items:center;justify-content:space-between;gap:25px}
header {border-top:3px solid #94c8bb;padding-top:24px;color:#a4d2c6}
header span, footer span {text-align:right;overflow-wrap:anywhere}
h1 {margin:38px 0;flex:1;display:flex;align-items:center;font:600 160px/1.02 Georgia,serif;letter-spacing:-.048em}
.instrument {padding:0;font:15px/1.5 var(--font-body);zoom:1.35}
footer {padding-top:24px;margin-top:42px;border-top:1px solid #52656b;font-size:24px;color:#b7c8c8}
</style><main><header><b>ChimePond</b><span id="collection"></span></header>
<h1></h1><div class="instrument"></div><footer>JotTune Synth<span id="profile"></span></footer></main></html>`;

function nativeEvents(harness, source) {
  const run = spawnSync(harness, ['--events', '--', path.resolve(source)], {encoding:'utf8', maxBuffer:16*1024*1024});
  if (run.error) throw run.error;
  if (run.status !== 0) throw Error(run.stderr.trim() || 'Native event inspection failed');
  const lines = run.stdout.split('\n');
  const info = lines.find(line => line.startsWith('INSPECT '));
  if (!info) throw Error('Native event inspection returned no INSPECT row');
  return {info:row(info), events:lines.filter(line => /^(EVENT|GEN_EVENT) /.test(line)).map(row)};
}

async function render(page, harness, request) {
  for (const key of ['source','output','title','collection','profile'])
    if (typeof request?.[key] !== 'string') throw Error('Missing string field ' + key);
  const result = nativeEvents(harness, request.source);
  const evidence = await page.evaluate(async ({result, title, collection, profile}) => {
    const {nativeTimeline} = await import('/media.mjs');
    const {createPianoRoll} = await import('/instrument-view.mjs');
    const duration = Number(result.info.frames) / Number(result.info.fs);
    if (!Number.isFinite(duration) || duration <= 0) throw Error('Invalid native duration');
    const model = nativeTimeline(result, duration);
    if (model.eventError) throw Error(model.eventError);
    const roll = createPianoRoll(document);
    document.querySelector('.instrument').replaceChildren(roll.element);
    roll.draw(model);
    // Select the midpoint of the nearest real gate. An empty roll keeps its native legend.
    const target = duration * .38;
    let cursor = 0, distance = Infinity;
    for (const note of model.notes) {
      const midpoint = (note.start + note.end) / 2;
      if (Math.abs(midpoint-target) < distance) {
        distance = Math.abs(midpoint-target);
        cursor = midpoint;
      }
    }
    roll.setTime(cursor, duration);
    const card = document.querySelector('main'), heading = card.querySelector('h1');
    heading.textContent = title;
    card.querySelector('#collection').textContent = collection;
    card.querySelector('#profile').textContent = profile;
    heading.style.fontSize = '160px';
    await document.fonts.ready;
    for (let size=160; size>32 && (card.scrollHeight>1200 || card.scrollWidth>1200); size-=2)
      heading.style.fontSize = (size-2) + 'px';
    const footer = card.querySelector('footer').getBoundingClientRect();
    if (card.scrollHeight>1200 || card.scrollWidth>1200 || footer.bottom>1200) throw Error('Artwork content exceeds the canvas');
    return {bars:model.notes.length, playhead_seconds:cursor, width:1200, height:1200};
  }, {result, title:request.title, collection:request.collection, profile:request.profile});
  const png = await page.locator('main').screenshot({type:'png', animations:'disabled', scale:'css'});
  await fs.writeFile(request.output, png);
  return evidence;
}

async function main() {
  const [harness] = process.argv.slice(2);
  if (!harness || process.argv.length !== 3) throw Error('Usage: node tools/piano_roll.mjs HARNESS < requests.jsonl');
  const modulePath = createRequire(import.meta.url).resolve(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const playwright = await import(pathToFileURL(modulePath).href);
  const chromium = playwright.chromium || playwright.default.chromium;
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROME_BIN || undefined});
  try {
    const page = await browser.newPage({viewport:{width:1200,height:1200}, serviceWorkers:'block'});
    // All page requests resolve in memory from this checkout, including its bundled font.
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      if (url.pathname === '/') return route.fulfill({contentType:'text/html', body:html});
      const type = files.get(url.pathname);
      if (!type) return route.abort();
      await route.fulfill({contentType:type, path:path.join(web, url.pathname.slice(1))});
    });
    await page.goto(origin);
    for await (const line of createInterface({input:process.stdin, crlfDelay:Infinity})) {
      try {
        console.log(JSON.stringify(await render(page, harness, JSON.parse(line))));
      } catch (error) {
        console.log(JSON.stringify({error:error.message}));
      }
    }
  } finally {
    await browser.close();
  }
}
main().catch(error => {console.error(error.message); process.exitCode = 1;});
