#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Run against a built static player. Playwright and a browser are supplied by the caller.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const args=process.argv.slice(2),base=args.shift();let type='chromium',executablePath;
while(args.length){const option=args.shift();
  if(option==='--browser')type=args.shift();else if(option==='--executable')executablePath=args.shift();
  else throw Error('Unknown browser test option.');
}
assert.ok(base&&['http:','https:'].includes(new URL(base).protocol),
  'Usage: node tests/browser.mjs PLAYER_URL [--browser chromium|firefox|webkit] [--executable PATH]');
assert.ok(['chromium','firefox','webkit'].includes(type));
const module=process.env.PLAYWRIGHT_MODULE||'playwright';
const playwright=await import(path.isAbsolute(module)?pathToFileURL(module).href:module);
const browser=await playwright[type].launch({headless:true,...(executablePath?{executablePath}:{})});
try{
  for(const viewport of [{width:1440,height:900},{width:1280,height:650},{width:390,height:844},{width:320,height:568}]){
    const mobile=viewport.width<500;
    const context=await browser.newContext({viewport,acceptDownloads:true,serviceWorkers:'allow',
      hasTouch:mobile,...(type==='firefox'?{}:{isMobile:mobile})});
    const page=await context.newPage(),errors=[],requests=[];
    page.setDefaultTimeout(60000);
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>requests.push({method:request.method(),url:request.url()}));
    await page.goto(base);
    await page.waitForFunction(()=>typeof window.IntFM?.result==='function').catch(error=>{
      throw Error('Player startup failed: '+(errors.join('; ')||error.message));
    });
    await page.evaluate(()=>document.fonts.ready);
    assert.equal(await page.locator('#publication-panel,#operator-panel,#publish-open').count(),0);
    const initial=await page.evaluate(()=>({result:IntFM.result(),wide:document.documentElement.scrollWidth>innerWidth,
      brand:getComputedStyle(document.querySelector('.brand')).fontFamily,
      prose:getComputedStyle(document.querySelector('#library-summary>.small')).fontFamily}));
    assert.ok(initial.result==null,'Opening the player does not render audio');assert.equal(initial.wide,false);
    assert.match(initial.brand,/Michroma/);assert.doesNotMatch(initial.prose,/Michroma/);
    await page.waitForFunction(()=>document.querySelector('#song-select input[type=radio]'));
    const songs=page.locator('#song-select input[type=radio]');
    const favoriteCount=await songs.count();
    for(let index=0;index<favoriteCount;index++) {
      await songs.nth(index).check();
      const kinds=await page.locator('#song-variant option').evaluateAll(nodes=>nodes.map(node=>node.value));
      const expected=kinds.includes('keys')?'keys':kinds.includes('standard')?'standard':kinds[0];
      assert.equal(await page.locator('#song-variant').inputValue(),expected,'New song selects Keys when available');
    }
    assert.equal(await page.evaluate(()=>IntFM.result() ?? null),null,'Browsing Favorites remains silent');
    console.log(`PASS ${type} ${viewport.width}x${viewport.height}: ${favoriteCount} Favorites use the expected saved profile`);
    if(viewport.width===1440 || viewport.width===390) {
      await songs.first().check();
      const saved=await page.evaluate(async()=>{
        const catalog=await(await fetch(new URL('catalog/featured-1.json',document.baseURI))).json();
        return catalog.items[0].variants.find(variant=>variant.kind===document.querySelector('#song-variant').value);
      });
      await page.locator('#song-load').click();
      await page.waitForFunction(()=>!document.querySelector('#play').disabled);
      const loaded=await page.evaluate(()=>({sha:IntFM.result().recordSha256,channels:IntFM.result().channels,
        action:document.querySelector('#play').textContent}));
      assert.equal(loaded.sha,saved.record_sha256,'Load prepares the selected saved version');
      assert.equal(loaded.channels,saved.technical.channels);
      assert.equal(loaded.action,'Play','Loading a Favorite remains silent');
      console.log(`PASS ${type} ${viewport.width}x${viewport.height}: Favorite Load matches saved SHA and channels without autoplay`);
    }

    const take=await page.evaluate(async()=>{
      const score=await(await fetch(new URL('starter.score.json',document.baseURI))).json();
      const compiled=await IntFM.compose(score),listened=await IntFM.listen(compiled.draftText,{release:compiled.rendererRelease});
      if(listened.verdict!=='valid')throw Error(listened.stderr||'Draft render failed.');
      const result=await IntFM.verify(new TextDecoder().decode(listened.canonical),{release:compiled.rendererRelease});
      return {verdict:result.verdict,verified:result.verified,pcmHash:result.pcmHash,
        canonical:new TextDecoder().decode(result.canonical),release:result.rendererRelease||result.engineId,
        wavBytes:IntFM.toWav(result).size,svg:IntFM.toSvg(result).startsWith('<svg')};
    });
    assert.equal(take.verdict,'valid');assert.equal(take.verified,true);
    assert.ok(take.wavBytes>44&&take.svg);
    const controls=mobile?'#mobile-play':'#rail-play';
    await page.locator(controls).click();
    await page.waitForFunction(()=>document.querySelector('#play').textContent==='Pause');
    await page.locator(controls).click();
    await page.waitForFunction(()=>document.querySelector('#play').textContent==='Resume');
    await page.locator('#player-panel').evaluate(node=>node.scrollIntoView({block:'start',behavior:'instant'}));
    for(const selector of ['#play','#play-right','#stop',...(mobile?['#choose-song','#mobile-play']:['#rail-load','#rail-play'])]){
      const fit=await page.locator(selector).evaluate(node=>{
        const range=document.createRange();range.selectNodeContents(node);
        const style=getComputedStyle(node),rect=node.getBoundingClientRect();
        return {width:range.getBoundingClientRect().width,available:node.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight),height:rect.height};
      });
      assert.ok(fit.width<=fit.available+1,selector+' must display its complete label');
      if(mobile)assert.ok(fit.height>=44,selector+' must retain its touch target');
    }
    await page.locator('#live-views').evaluate(node=>node.scrollIntoView({block:'start',behavior:'instant'}));
    const views=await page.locator('.view-controls button').evaluateAll(nodes=>nodes.map(node=>node.getBoundingClientRect().y));
    const rows=[...new Set(views.map(y=>Math.round(y)))];
    if(viewport.width===390)assert.equal(rows.length,1,'Phone display controls fit one row');
    if(viewport.width===320){
      assert.ok(rows.length<=2,'Small phone display controls use at most two rows');
      if(rows.length===2)assert.deepEqual(rows.map(y=>views.filter(v=>Math.round(v)===y).length),[2,2]);
    }
    const plot=await page.locator('#instrument-roll-plot').boundingBox();
    assert.ok(plot.height>=200,'Typography must not shrink the piano roll');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    const [download]=await Promise.all([page.waitForEvent('download'),page.locator('#tl-download').click()]);
    assert.equal(await fs.readFile(await download.path(),'utf8'),take.canonical);
    await page.waitForFunction(()=>Boolean(navigator.serviceWorker.controller));
    await context.setOffline(true);await page.reload();
    await page.waitForFunction(()=>typeof window.IntFM?.result==='function');
    const offline=await page.evaluate(async({canonical,release})=>{
      const result=await IntFM.listen(canonical,{release});return {verdict:result.verdict,pcmHash:result.pcmHash};
    },take);
    assert.equal(offline.verdict,'valid');assert.equal(offline.pcmHash,take.pcmHash);
    assert.deepEqual(errors,[]);
    assert.ok(requests.every(request=>request.method==='GET'&&!new URL(request.url).pathname.includes('/api/')),
      'Standalone composition and playback never contact an upload or operator API');
    await context.close();
    console.log(`PASS ${type} ${viewport.width}x${viewport.height}: compose, verify, transport, full labels, visual size, exact TL and offline replay`);
  }
}finally{await browser.close();}
