// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
const RELEASE='development'; // Replaced by the local packaging command.
const BASE=new URL('./',self.location.href), PREFIX='intfm-workbench-'+encodeURIComponent(BASE.pathname)+'-';
const CACHE=PREFIX+RELEASE, ENGINES='intfm-engines-'+encodeURIComponent(BASE.pathname);
const unavailable=()=>new Response('Not available offline. Reconnect or open a saved TL file.',{status:503,headers:{'Content-Type':'text/plain; charset=utf-8','Cache-Control':'no-store'}});
// './' and index.html are one document: it is fetched once and stored under both
// keys. no-cache revalidates files the page has just loaded instead of downloading
// them again, and still never stores a stale copy.
self.addEventListener('install',event=>event.waitUntil((async()=>{const list=await(await fetch(new URL('precache.json',BASE),{cache:'no-store'})).json(),cache=await caches.open(CACHE);
  const shared=list.includes('./')&&list.includes('index.html');
  await cache.addAll(list.filter(url=>!shared||url!=='index.html').map(url=>new Request(new URL(url,BASE),{cache:'no-cache'})));
  if(shared)await cache.put(new URL('index.html',BASE),await cache.match(new URL('./',BASE)));
  await self.skipWaiting();})()));
self.addEventListener('activate',event=>event.waitUntil((async()=>{for(const key of await caches.keys())if(key.startsWith(PREFIX)&&key!==CACHE)await caches.delete(key);await self.clients.claim();})()));
self.addEventListener('fetch',event=>{
  const url=new URL(event.request.url);
  if(event.request.method!=='GET'||url.origin!==BASE.origin||!url.pathname.startsWith(BASE.pathname))return;
  // Live service records and API state are never retained by the app cache.
  const relative=url.pathname.slice(BASE.pathname.length);
  if(/^(api|r|s)\//.test(relative))return;
  event.respondWith((async()=>{
    const engine=relative.startsWith('engines/');
    const playerDocument=event.request.mode==='navigate'&&(relative===''||relative==='index.html');
    let cache,cached;
    try{
      cache=await caches.open(engine?ENGINES:CACHE);
      cached=await cache.match(event.request,{ignoreSearch:playerDocument});
    }catch{/* The online player also works when browser storage is unavailable. */}
    if(engine&&cached)return cached;
    try{
      // A returning page can still have yesterday's worker. Revalidate its
      // mutable shell, modules and references on first use after a deploy.
      const revalidate=!engine&&(playerDocument||cached)&&event.request.cache==='default';
      const response=revalidate?await fetch(event.request,{cache:'no-cache'}):await fetch(event.request);
      if(response.ok&&cache&&(engine||relative==='engines.json'))
        await cache.put(event.request,response.clone()).catch(()=>{});
      return response;
    }catch{
      // Only the installer replaces the shell snapshot, so offline startup can
      // use the last complete install. A real HTTP error remains authoritative.
      if(!cached)return unavailable();
      // A navigation cannot use a followed-redirect response from the precache.
      if(event.request.mode==='navigate'&&cached.redirected)
        return new Response(cached.body,{status:cached.status,statusText:cached.statusText,headers:cached.headers});
      return cached;
    }
  })());
});
