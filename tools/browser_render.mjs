#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Node smoke bridge for the browser worker contract. This is not a browser test.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const [input,output]=process.argv.slice(2);
if(!input){console.error('Usage: node tools/browser_render.mjs RECORD.tl [CANONICAL.tl]');process.exit(2);}
const registry=JSON.parse(await fs.readFile(path.join(root,'web/engines.json'),'utf8'));
const engine=registry.engines.find(x=>x.id===registry.default);
globalThis.fetch=async value=>{const file=path.resolve(root,'web',String(value));if(!file.startsWith(path.join(root,'web')+path.sep))throw Error('Only packaged local assets are allowed.');return new Response(await fs.readFile(file));};
let result;
globalThis.self={postMessage:value=>{result=value;}};
await import('../web/worker.mjs');
await self.onmessage({data:{recordText:await fs.readFile(input,'utf8'),engine}});
if(output&&result.verdict==='valid')await fs.writeFile(output,result.canonical,{flag:'wx'});
console.log(JSON.stringify({verdict:result.verdict,recordSha256:result.recordSha256,pcmHash:result.pcmHash,engineIdentity:result.engineIdentity,
  info:result.info,play:result.play,metrics:result.metrics,events:result.events?.length,stderr:result.stderr},null,2));
process.exitCode=result.verdict==='valid'?0:1;
