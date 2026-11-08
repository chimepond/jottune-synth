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
const browser=await playwright[type].launch({headless:true,...(type==='chromium'?{args:['--mute-audio']}:{}),...(executablePath?{executablePath}:{})});
try{
  for(const viewport of [{width:1440,height:900},{width:1280,height:650},{width:390,height:844},{width:320,height:568},{width:734,height:343,mobile:true},{width:720,height:450}]){
    const mobile=viewport.width<500||viewport.mobile===true,compact=viewport.width<1100;
    const context=await browser.newContext({viewport:{width:viewport.width,height:viewport.height},acceptDownloads:true,serviceWorkers:'allow',
      hasTouch:mobile,...(type==='firefox'?{}:{isMobile:mobile})});
    // Keep tests silent in every browser while leaving rendered PCM untouched.
    await context.addInitScript(()=>{
      const Original=window.AudioContext||window.webkitAudioContext;if(!Original)return;
      class QuietAudioContext extends Original {
        constructor(...args){super(...args);const mute=this.createGain();mute.gain.value=0;mute.connect(this.destination);
          Object.defineProperty(this,'destination',{get:()=>mute});}
      }
      window.AudioContext=QuietAudioContext;if(window.webkitAudioContext)window.webkitAudioContext=QuietAudioContext;
    });
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
    // Every engine sizes the filter and profile selects like the search field, and rail text stays readable.
    const fields=await page.evaluate(()=>{
      const shown=node=>node.getClientRects().length>0,height=node=>Math.round(node.getBoundingClientRect().height),rail=[];
      const walker=document.createTreeWalker(document.querySelector('#section-nav'),NodeFilter.SHOW_TEXT);
      for(let node;(node=walker.nextNode());)if(node.textContent.trim()&&shown(node.parentElement))rail.push(parseFloat(getComputedStyle(node.parentElement).fontSize));
      return {search:height(document.querySelector('#song-search')),rail,
        selects:[...document.querySelectorAll('.library-filters select,.profile-controls select')].filter(shown).map(node=>[node.id,height(node)]),
        sharing:document.querySelectorAll('#section-nav a[data-section="publication-panel"]').length};
    });
    assert.ok(fields.selects.length>=5&&fields.selects.every(([,height])=>Math.abs(height-fields.search)<=1),
      'Filter and profile selects match the search field: '+JSON.stringify(fields.selects)+' vs '+fields.search);
    if(!compact)assert.ok(fields.rail.length>10&&Math.min(...fields.rail)>=12,'Rail text is at least 12px: '+fields.rail);
    assert.equal(fields.sharing,0,'The standalone page has no Sharing section link');
    assert.equal(await page.getByRole('group',{name:'Playback controls',exact:true}).count(),1,'The Player transport is one named group');
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
      if(!compact)assert.equal(await page.getByRole('button',{name:/^Load song: /}).count(),1,'Sections Load names the selected song after its visible text');
      await page.locator('#song-load').click();
      await page.waitForFunction(()=>!document.querySelector('#play').disabled);
      const loaded=await page.evaluate(()=>({sha:IntFM.result().recordSha256,channels:IntFM.result().channels,
        action:document.querySelector('#play').textContent}));
      assert.equal(loaded.sha,saved.record_sha256,'Load prepares the selected saved version');
      assert.equal(loaded.channels,saved.technical.channels);
      assert.equal(loaded.action,'Play','Loading a Favorite remains silent');
      console.log(`PASS ${type} ${viewport.width}x${viewport.height}: Favorite Load matches saved SHA and channels without autoplay`);
    }

    const rowLayout=await page.locator('#song-select').evaluate(node=>({height:node.clientHeight,rows:[...node.querySelectorAll('.song-choice')].map(row=>row.getBoundingClientRect().height)}));
    assert.ok(rowLayout.rows.every(height=>height>=24),'Every song row has a24px target');
    assert.ok(rowLayout.rows.reduce((a,b)=>a+b,0)<=rowLayout.height+1,'All eight compact rows remain visible');
    if(viewport.height<=500){
      assert.equal(await page.locator('.app-header').evaluate(node=>getComputedStyle(node).position),'static');
      await page.locator('#details-panel').evaluate(node=>{node.open=true;for(const child of node.querySelectorAll('details'))child.open=true;});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'Expanded analysis options fit short windows');
      // The header scrolls away here, so the dock's Sections button keeps every section reachable.
      await page.evaluate(()=>scrollTo(0,1500));
      await page.locator('#dock-sections').click();
      assert.equal(await page.locator('#dock-sections').getAttribute('aria-expanded'),'true');
      await page.locator('#section-nav a[data-section="source-panel"]').click();
      await page.waitForFunction(()=>document.querySelector('#section-nav').hidden&&document.activeElement?.id==='source-summary');
      await page.waitForFunction(()=>Math.abs(document.querySelector('#source-panel').getBoundingClientRect().top)<=60,null,{timeout:5000})
        .catch(()=>assert.fail('The dock menu jumps to its section'));
      await page.locator('#dock-sections').focus();await page.keyboard.press('Enter');
      assert.equal(await page.locator('#section-nav').isVisible(),true);
      await page.keyboard.press('Escape');
      await page.waitForFunction(()=>document.querySelector('#section-nav').hidden&&document.activeElement?.id==='dock-sections');
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
    const controls=compact?'#mobile-play':'#rail-play';
    await page.locator(controls).focus();await page.keyboard.press('Space');
    await page.waitForFunction(()=>document.querySelector('#play').textContent==='Pause');
    assert.equal(await page.evaluate(()=>document.activeElement.id),controls.slice(1));
    await page.waitForFunction(()=>Number(document.querySelector('#seek').value)>0);
    await page.keyboard.press('Space');
    await page.waitForFunction(()=>document.querySelector('#play').textContent==='Resume');
    await page.locator('#player-panel').evaluate(node=>node.scrollIntoView({block:'start',behavior:'instant'}));
    for(const selector of ['#play','#play-right','#stop',...(compact?['#choose-song','#mobile-play']:['#rail-load','#rail-play'])]){
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
    if(viewport.width<=480){
      // Phone type scale: body copy at least 14px and other text at least 13px, except the brand subtitle, key letters and chart ticks.
      const phone=await page.evaluate(()=>{
        const small=new Set(),walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),range=document.createRange();
        for(let node;(node=walker.nextNode());){
          const parent=node.parentElement;if(!node.textContent.trim()||parent.closest('.brand,.instrument-key,.instrument-pitch-axis,.instrument-db-axis,.instrument-spectrum-axis,.instrument-roll-times,svg'))continue;
          range.selectNodeContents(node);const style=getComputedStyle(parent),size=parseFloat(style.fontSize);
          if(size<13&&style.visibility!=='hidden'&&[...range.getClientRects()].some(rect=>rect.width>1&&rect.height>1))
            small.add(`${parent.tagName.toLowerCase()}${parent.id?'#'+parent.id:''}${parent.className?'.'+parent.className:''} "${node.textContent.trim().slice(0,24)}"@${size}`);
        }
        return {small:[...small],body:['.agent-entry','.profile-help'].map(selector=>parseFloat(getComputedStyle(document.querySelector(selector)).fontSize))};
      });
      assert.deepEqual(phone.small,[],'Phone text is at least 13px');
      assert.ok(phone.body.every(size=>size>=14),'Phone body copy is at least 14px');
    }
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    const [download]=await Promise.all([page.waitForEvent('download'),page.locator('#tl-download').click()]);
    assert.equal(await fs.readFile(await download.path(),'utf8'),take.canonical);
    const shared=await page.evaluate(async()=>{
      const row=(await(await fetch(new URL('catalog/featured-1.json',document.baseURI))).json()).items[1];
      const url=new URL(document.baseURI);url.hash=new URLSearchParams({song:row.path,sha:row.record_sha256,catalog:'catalog/featured-1.json'}).toString();
      return {url:url.href,title:row.title};
    });
    await page.goto(shared.url);await page.waitForFunction(()=>IntFM.result()?.verdict==='valid');
    await page.waitForFunction(title=>document.querySelector('#selected-song-title').textContent===title,shared.title);
    assert.equal(await page.locator('#loaded-song').textContent(),shared.title);
    assert.equal(await page.locator('#rail-selected-song').textContent(),shared.title);
    assert.equal(await page.locator('#play').textContent(),'Play','Incoming links stay silent');
    if(viewport.width===1440){
      const lateTitle=await page.evaluate(async()=>{
        const index=await(await fetch(new URL('catalog/index.json',document.baseURI))).json();
        let path=index.categories.find(item=>item.id==='all').first_page;const titles=[];
        while(path){const page=await(await fetch(new URL(path,document.baseURI))).json();titles.push(...page.items.map(item=>item.title));path=page.next;}
        return titles.find((title,i)=>i>=96&&titles.findIndex(other=>other.includes(title))===i)||null;
      });
      if(lateTitle){await page.locator('#song-search').fill(lateTitle);await page.waitForFunction(title=>document.querySelector('#selected-song-title').textContent===title,lateTitle);
        assert.match(await page.locator('#library-status').textContent(),/Results [1-9]/);}
    }
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
  {
    // Hosted pages add a sharing section after the footer; the section menu lists it and the scroll spy follows it.
    const context=await browser.newContext({viewport:{width:1440,height:900},serviceWorkers:'block'}),page=await context.newPage(),entry=new URL(base);
    await page.route(url=>url.origin===entry.origin&&[entry.pathname,new URL('index.html',entry).pathname].includes(url.pathname),async route=>{
      const response=await route.fetch();
      await route.fulfill({response,body:(await response.text()).replace('</main>','<section class="panel" aria-label="Song sharing">'+
        '<button type="button" aria-controls="publication-panel">Sharing options</button></section><details id="publication-panel" class="panel">'+
        '<summary id="publication-summary" tabindex="-1">Sharing</summary><p>Test panel.</p></details><div style="height:1200px"></div></main>')});
    });
    await page.goto(base);await page.waitForFunction(()=>typeof window.IntFM?.result==='function');
    const current=section=>page.waitForFunction(id=>document.querySelector(`#section-nav a[data-section="${id}"]`).getAttribute('aria-current')==='location',section);
    const link=page.locator('#section-nav a[data-section="publication-panel"]');
    assert.equal(await link.isVisible(),true,'A hosted sharing panel gets a Sharing section link');
    await page.locator('#publication-panel').evaluate(node=>node.previousElementSibling.scrollIntoView({block:'start',behavior:'instant'}));
    await current('publication-panel');
    await page.evaluate(()=>scrollTo(0,0));await current('library-panel');
    await link.click();
    await page.waitForFunction(()=>document.querySelector('#publication-panel').open&&document.activeElement?.id==='publication-summary');
    await current('publication-panel');
    await context.close();
    console.log(`PASS ${type} 1440x900: a hosted sharing panel has a Sharing section link and scroll-spy coverage`);
  }
  if(type==='chromium'){
    // A notched phone: scrolled content never shows in the band above the sticky header.
    const context=await browser.newContext({viewport:{width:390,height:844},serviceWorkers:'block',hasTouch:true,isMobile:true}),page=await context.newPage();
    await page.goto(base);await page.waitForFunction(()=>typeof window.IntFM?.result==='function');
    const cdp=await context.newCDPSession(page);
    const inset=await cdp.send('Emulation.setSafeAreaInsetsOverride',{insets:{top:59,topMax:59,bottom:34,bottomMax:34,left:0,leftMax:0,right:0,rightMax:0}}).then(()=>true,()=>false);
    if(inset){
      const band=await page.evaluate(async()=>{scrollTo(0,1400);await new Promise(resolve=>setTimeout(resolve,300));
        return [10,30,50].map(y=>Boolean(document.elementFromPoint(195,y)?.closest('.app-header')));});
      assert.deepEqual(band,[true,true,true],'The safe-area band above the header is painted');
      console.log(`PASS ${type} 390x844: the top safe area stays painted while scrolled`);
    }else console.log(`SKIP ${type} 390x844: no safe-area override in this Chromium, so the painted band is not checked`);
    await context.close();
  }
}finally{await browser.close();}
