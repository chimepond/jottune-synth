#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Restriction-profile checks against a caller-supplied built player. These are
// browser/viewport tests, not relay summarization, Claude or physical-phone tests.
// Usage: node tests/agent_handoff.mjs PLAYER_URL [--browser chromium|webkit] [--executable PATH] [--channel chrome]
import assert from 'node:assert/strict';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {parseDocument} from '../web/tl-document.mjs';

const args=process.argv.slice(2), base=args.shift(); let type='chromium', executablePath, channel;
while(args.length) {
  const option=args.shift();
  if(option==='--browser') type=args.shift();
  else if(option==='--executable') executablePath=args.shift();
  else if(option==='--channel') channel=args.shift();
  else throw Error('Unknown handoff test option.');
}
assert(base && ['http:','https:'].includes(new URL(base).protocol), 'Supply a built player HTTP(S) URL.');
assert(['chromium','webkit'].includes(type), 'Choose chromium or webkit.');
const module=process.env.PLAYWRIGHT_MODULE || 'playwright';
const playwright=await import(path.isAbsolute(module) ? pathToFileURL(module).href : module);
const browser=await playwright[type].launch({headless:true,...(type==='chromium' ? {args:['--mute-audio']} : {}),
  ...(executablePath ? {executablePath} : {}),...(channel ? {channel} : {})});
const status=page=>page.locator('#status').textContent();
const audioCount=page=>page.evaluate(()=>window.__handoffAudioContexts);
async function mobileLayout(page,phase) {
  const width=page.viewportSize().width;
  const layout=await page.evaluate(async()=>{
    await document.fonts?.ready;
    const selectors='.app-header,.app-header .brand,.resource-nav a,#section-toggle,.downloads button,#prepare-input,#agent-report,#agent-report-manual,#record,#file,#url-form input,#url-form button';
    return {viewport:window.innerWidth,scrollWidth:Math.max(document.documentElement.scrollWidth,document.body.scrollWidth),
      controls:[...document.querySelectorAll(selectors)].filter(node=>node.getClientRects().length && getComputedStyle(node).visibility!=='hidden')
        .map(node=>{const box=node.getBoundingClientRect();return {name:node.id || node.textContent.trim().slice(0,40) || node.tagName,left:box.left,right:box.right};})};
  });
  assert(Math.abs(layout.viewport-width)<=1,`${phase}: mobile layout viewport widened from ${width} to ${layout.viewport}`);
  assert(layout.scrollWidth<=width+1,`${phase}: document overflows ${width}px by ${layout.scrollWidth-width}px`);
  for(const control of layout.controls) assert(control.left>=-1 && control.right<=width+1,
    `${phase}: ${control.name} extends beyond the ${width}px viewport (${control.left}..${control.right})`);
}
const fixture=title=>({version:1,fs:48000,metadata:{title,license:'CC0-1.0'},
  clock:{bars:2,meter:[{bar:1,quarters:4}],tempo:[{bar:1,bpm:120}],ramp:'step',tail_ns:500000000},
  declarations:'space stereo\nblock 64\nnear 100\nrecipe lead marimba\n',streams:['lead'],voices:['lead'],
  events:[{kind:'pos',at:0,stream:'lead',voice:0,x_mm:0,y_mm:1000,z_mm:0},
    ...['C5','E5','G5'].map((note,index)=>({kind:'note',at:index*2,duration:1,stream:'lead',voice:0,pitch:{note},velocity_q15:22000})),
    {kind:'off',at:7,stream:'lead',voice:0}]}); // Deliberate legal unmatched-off advisory.
const upload=(page,score,name='handoff.score.json')=>page.locator('#file').setInputFiles({name,mimeType:'application/json',buffer:Buffer.from(JSON.stringify(score))});
async function openDetails(page,id) {
  if(await page.locator('#'+id).getAttribute('open')===null) await page.locator('#'+id+' > summary').click();
}
async function ready(page,title) {
  await page.waitForFunction(()=>!document.getElementById('play').disabled && document.getElementById('cancel').hidden &&
    /^(Ready|Verified)/.test(document.getElementById('verdict').textContent));
  if(title) assert.equal((await facts(page))['Song title'],title);
}
const facts=page=>page.locator('#facts').evaluate(node=>Object.fromEntries([...node.querySelectorAll('dt')]
  .map(term=>[term.textContent,term.nextElementSibling.textContent])));
async function paste(page,text) {
  await openDetails(page,'source-panel'); await page.locator('#record').fill(text); await page.locator('#prepare-input').click();
}
function compact(report,text) {
  assert(Buffer.byteLength(text)<65536,'The short-fixture report must not contain raw PCM.');
  for(const key of ['pcm','canonical','stdout','events']) assert(!Object.hasOwn(report.song || {},key),'Raw result field: '+key);
  const visit=value=>{
    if(!value || typeof value!=='object') return;
    assert(!(Array.isArray(value) && value.length>32 && value.every(item=>typeof item==='number')),'Raw numeric sample array in report.');
    assert(Object.keys(value).filter(key=>/^\d+$/.test(key)).length<1024,'Stringified typed sample array in report.');
    for(const child of Object.values(value)) visit(child);
  };
  visit(report);
}
async function report(page) {
  await page.locator('#agent-report').click();
  await page.locator('#agent-report-manual').waitFor({state:'visible'});
  assert.match(await status(page),/Clipboard access is unavailable/);
  const text=await page.locator('#agent-report-manual').inputValue(), value=JSON.parse(text);
  compact(value,text); return value;
}
async function canonicalDownload(page) {
  const pending=page.waitForEvent('download'); await page.locator('#tl-download').click(); const download=await pending;
  assert.match(download.suggestedFilename(),/\.tl$/); assert.equal(await download.failure(),null);
  const stream=await download.createReadStream(), chunks=[];
  for await(const chunk of stream) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
}
async function damagedLink(page,{missingCompression=false}={}) {
  const before={record:await page.locator('#record').inputValue(),facts:await facts(page),verdict:await page.locator('#verdict').textContent()};
  await openDetails(page,'source-panel');
  const url=new URL(base); url.hash='v1.%%%';
  await page.locator('#url').fill(url.href); await page.locator('#url-form button').click();
  await page.waitForFunction(()=>/The current song is unchanged\./.test(document.getElementById('status').textContent));
  assert.match(await status(page),missingCompression ? /cannot open compressed song links.*\.tl file/ : /incomplete or damaged.*fragment characters.*new link or the \.tl file/);
  assert.deepEqual({record:await page.locator('#record').inputValue(),facts:await facts(page),verdict:await page.locator('#verdict').textContent()},before);
  assert(await page.locator('#play').isEnabled());
}
async function withPage(name,run,{reader=false,missingCompression=false,delayFile=false,intercept,width=390}={}) {
  const context=await browser.newContext({viewport:{width,height:844},hasTouch:true,isMobile:true,
    javaScriptEnabled:!reader,serviceWorkers:'block',acceptDownloads:true});
  const forbidden=[],errors=[],methods=new Set();
  context.on('request',request=>{if(/^https?:/.test(request.url())) { methods.add(request.method()); if(!['GET','HEAD'].includes(request.method())) forbidden.push(request.method()); }});
  await context.route('**/*',async route=>{
    if(/^https?:/.test(route.request().url()) && !['GET','HEAD'].includes(route.request().method())) return route.abort('blockedbyclient');
    if(await intercept?.(route)) return;
    return route.continue();
  });
  if(!reader) await context.addInitScript(({missingCompression,delayFile})=>{
    window.__handoffAudioContexts=0;
    const Original=window.AudioContext || window.webkitAudioContext;
    if(Original) {
      class Quiet extends Original {
        constructor(...args) {super(...args);window.__handoffAudioContexts++;const mute=this.createGain();mute.gain.value=0;mute.connect(this.destination);
          Object.defineProperty(this,'destination',{get:()=>mute});}
      }
      window.AudioContext=Quiet;if(window.webkitAudioContext) window.webkitAudioContext=Quiet;
    }
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new DOMException('Denied by test profile','NotAllowedError');}}});
    if(missingCompression) for(const name of ['CompressionStream','DecompressionStream']) Object.defineProperty(window,name,{configurable:true,value:undefined});
    if(delayFile) {
      const original=File.prototype.arrayBuffer;
      File.prototype.arrayBuffer=async function(...args) {const value=await original.apply(this,args);
        if(this.name==='late.score.json') await new Promise(resolve=>setTimeout(resolve,1000));return value;};
    }
  },{missingCompression,delayFile});
  const page=await context.newPage();page.setDefaultTimeout(45000);page.on('pageerror',error=>errors.push(error.message));
  try {
    await page.goto(base,{waitUntil:'domcontentloaded'});
    if(!reader) await page.waitForFunction(()=>typeof document.getElementById('prepare-input')?.onclick==='function');
    await run(page,context);
    assert.deepEqual(forbidden,[],'HTTP methods must remain GET/HEAD.');assert.deepEqual(errors,[],'Unexpected page errors.');
    console.log(`PASS ${type}: ${name} (HTTP ${[...methods].sort().join('/')}; service workers blocked)`);
  } catch(error) {
    if(errors.length) error.message+='\nPage errors: '+errors.join('; ');
    throw error;
  } finally {await context.close();}
}

try {
  await withPage('JavaScript-disabled reader discovers exact quick card and score/docs links',async(page,context)=>{
    for(const name of ['agent-quick.txt','agent-start.txt','authoring.txt','starter.score.json','analysis.txt','browser-guide.txt']) {
      const link=page.locator(`a[href="${name}"]`).first();assert.equal(await link.count(),1,'Missing direct reference: '+name);
      const response=await context.request.get(new URL(await link.getAttribute('href'),base).href);
      assert.equal(response.status(),200,name);const bytes=await response.body();assert(bytes.length>0,name);
      if(name==='agent-quick.txt') {assert(bytes.length<=2048,'Quick card exceeds 2 KiB.');assert.match(bytes.toString(),/"version":1/);}
      if(name==='starter.score.json') assert.equal(JSON.parse(bytes).version,1);
    }
    assert(await page.locator('#play').isDisabled(),'A reader-only page has no prepared audio.');
  },{reader:true});

  await withPage('390×844 touch handoff: file/paste, report, canonical metadata, recovery and explicit Play',async page=>{
    assert.equal(await audioCount(page),0);await mobileLayout(page,'390px initial');
    await upload(page,fixture('File handoff fixture'));await ready(page,'File handoff fixture');assert.equal(await audioCount(page),0);
    await mobileLayout(page,'390px prepared score');
    const initial=await report(page);assert.equal(initial.song.title,'File handoff fixture');await mobileLayout(page,'390px manual report');
    assert(initial.song.authoring.counts.input_events>0);assert(initial.song.authoring.advisories.length>0);
    assert.match(initial.analysisStatus,/Not run/);
    await openDetails(page,'details-panel');
    const measurements=page.locator('#details-panel details').filter({has:page.locator('#analyze-audio')});
    if(await measurements.getAttribute('open')===null) await measurements.locator(':scope > summary').click();
    await page.locator('#analyze-audio').click();
    await page.waitForFunction(()=>/^Analysis complete\./.test(document.getElementById('analysis-status').textContent));
    const measured=await report(page);assert.equal(measured.analysis.recordSha256,measured.song.recordSha256);
    assert.equal(typeof measured.analysis.pcm.sample_peak,'number');assert.equal(await audioCount(page),0);
    await openDetails(page,'source-panel');await page.locator('#verify-saved').click();
    await page.waitForFunction(()=>/^Verified/.test(document.getElementById('verdict').textContent));
    const verified=await report(page);assert.equal(verified.song.verified,true);assert.equal(verified.song.play.match,'yes');assert.equal(verified.song.play.block_indep,'yes');
    const saved=parseDocument(await canonicalDownload(page));assert.equal(saved.metadata.title,'File handoff fixture');assert.equal(saved.metadata.license,'CC0-1.0');assert.match(saved.body,/^hash [a-f0-9]{16}$/m);
    await paste(page,JSON.stringify(fixture('Pasted handoff fixture')));await ready(page,'Pasted handoff fixture');assert.equal(await audioCount(page),0);
    assert.equal((await report(page)).song.title,'Pasted handoff fixture');
    for(const [input,expected] of [['{',/incomplete or invalid/],[JSON.stringify({...fixture('Unsupported version'),version:3}),/version/i]]) {
      await paste(page,input);await page.waitForFunction(()=>!document.getElementById('agent-report').disabled);
      assert.match(await status(page),expected);const failure=await report(page);assert(failure.inputError.message);
      if(input.includes('Unsupported version')) assert.equal(failure.inputError.pointer,'/version');
      assert(await page.locator('#play').isDisabled());assert.equal(await audioCount(page),0);
    }
    await paste(page,JSON.stringify(fixture('Recovered handoff fixture')));await ready(page,'Recovered handoff fixture');
    await damagedLink(page);assert.equal(await audioCount(page),0);
    await page.locator('#play').tap();await page.waitForFunction(()=>window.__handoffAudioContexts>0);
    await page.waitForFunction(()=>document.getElementById('play').textContent==='Pause');await page.locator('#stop').tap();
  });

  await withPage('320px layout fits initial page, prepared score and manual report',async page=>{
    await mobileLayout(page,'320px initial');
    await upload(page,fixture('Narrow handoff fixture'));await ready(page,'Narrow handoff fixture');
    await mobileLayout(page,'320px prepared score');assert.equal(await audioCount(page),0);
    assert.equal((await report(page)).song.title,'Narrow handoff fixture');
    await mobileLayout(page,'320px manual report');assert.equal(await audioCount(page),0);
  },{width:320});

  await withPage('late local file read cannot replace a newer score',async page=>{
    await upload(page,fixture('Obsolete handoff fixture'),'late.score.json');
    await upload(page,fixture('Current handoff fixture'),'current.score.json');await ready(page,'Current handoff fixture');
    await page.waitForTimeout(1200);assert.equal((await facts(page))['Song title'],'Current handoff fixture');
    assert.equal((await report(page)).song.title,'Current handoff fixture');assert.equal(await audioCount(page),0);
  },{delayFile:true});

  let release,held=false,signalHeld;
  const started=new Promise(resolve=>{signalHeld=resolve;});const gate=new Promise(resolve=>{release=resolve;});
  try {
    await withPage('cancelling a held renderer download stays silent and permits recovery',async page=>{
      await upload(page,fixture('Cancelled handoff fixture'));
      let timer;try {await Promise.race([started,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('No renderer download reached the cancellation fixture.')),15000);})]);} finally {clearTimeout(timer);}
      await page.locator('#cancel').click();await page.waitForFunction(()=>/cancelled/i.test(document.getElementById('status').textContent));
      assert(await page.locator('#play').isDisabled());assert.equal(await audioCount(page),0);release();
      await paste(page,JSON.stringify(fixture('After cancellation fixture')));await ready(page,'After cancellation fixture');
      assert.equal((await report(page)).song.title,'After cancellation fixture');assert.equal(await audioCount(page),0);
    },{intercept:async route=>{
      if(!held && /\.wasm(?:\?|$)/.test(route.request().url())) {held=true;signalHeld();await gate;try {await route.continue();} catch {} return true;}
      return false;
    }});
  } finally {release();}

  await withPage('missing compression support gives file fallback and preserves prepared audio',async page=>{
    await upload(page,fixture('Compression fallback fixture'));await ready(page,'Compression fallback fixture');
    await damagedLink(page,{missingCompression:true});assert.equal(await audioCount(page),0);
  },{missingCompression:true});
  console.log(`AGENT_HANDOFF ok ${type}: reader and touch restriction profiles; no assistant relay or physical-device claim`);
} finally {await browser.close();}
