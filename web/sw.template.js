// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
const RELEASE='development'; // Replaced by the local packaging command.
const BASE=new URL('./',self.location.href), PREFIX='intfm-workbench-'+encodeURIComponent(BASE.pathname)+'-';
const CACHE=PREFIX+RELEASE, ENGINES='intfm-engines-'+encodeURIComponent(BASE.pathname);
self.addEventListener('install',event=>event.waitUntil((async()=>{const list=await(await fetch(new URL('precache.json',BASE),{cache:'no-store'})).json();await(await caches.open(CACHE)).addAll(list.map(url=>new Request(new URL(url,BASE),{cache:'reload'})));await self.skipWaiting();})()));
self.addEventListener('activate',event=>event.waitUntil((async()=>{for(const key of await caches.keys())if(key.startsWith(PREFIX)&&key!==CACHE)await caches.delete(key);await self.clients.claim();})()));
self.addEventListener('fetch',event=>{
  const url=new URL(event.request.url);
  if(event.request.method!=='GET'||url.origin!==BASE.origin||!url.pathname.startsWith(BASE.pathname))return;
  // Live service records and API state are never retained by the app cache.
  const relative=url.pathname.slice(BASE.pathname.length);
  if(/^(api|r|s)\//.test(relative))return;
  event.respondWith((async()=>{
    const engine=relative.startsWith('engines/'), cache=await caches.open(engine?ENGINES:CACHE);
    const cached=await cache.match(event.request);
    if(relative==='engines.json'){
      try{const response=await fetch(event.request);if(response.ok)await cache.put(event.request,response.clone());return response;}
      catch(error){if(cached)return cached;throw error;}
    }
    if(cached){
      // Precache follows HTML redirects, but navigations use redirect:manual.
      // Return the document bytes without the followed-redirect metadata.
      if(event.request.mode==='navigate'&&cached.redirected)
        return new Response(cached.body,{status:cached.status,statusText:cached.statusText,headers:cached.headers});
      return cached;
    }
    const response=await fetch(event.request);
    if(response.ok&&engine)await cache.put(event.request,response.clone());
    return response;
  })());
});
