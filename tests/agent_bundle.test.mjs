// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {buildBundle,setup} from '../web/agent.mjs';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const client = fileURLToPath(new URL('../web/agent.mjs',import.meta.url));
const required = ['worker.mjs','runtime.mjs','policy.mjs','media.mjs','links.mjs','tl-document.mjs'];
async function scratch(run) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(),'agent-bundle-test-'));
  try { return await run(root); } finally { await fs.rm(root,{recursive:true,force:true}); }
}
async function fixture(root) {
  const source = path.join(root,'source'); await fs.mkdir(source);
  const assets = new Map(required.map(name => [name,Buffer.from('export {};\n')]));
  assets.set('agent.mjs',await fs.readFile(client));
  assets.set('engines/release-one/runtime.mjs',Buffer.from('export {};\n'));
  assets.set('engines/release-one/harness.wasm',Buffer.from('fixture wasm'));
  assets.set('authoring.txt',Buffer.from('Exact authoring documentation.\n'));
  assets.set('starter.score.json',Buffer.from('{"version":1}\n'));
  assets.set('starter.tl',Buffer.from('teaching fixture'));
  assets.set('example.tl',Buffer.from('teaching fixture'));
  assets.set('songs/omitted.tl',Buffer.from('not distributed'));
  assets.set('catalog/all-1.json',Buffer.from('{"private":"not distributed"}'));
  assets.set('engines/another-release/harness.wasm',Buffer.from('not selected'));
  const engine = {id:'release-one',release_id:'release-one',development:false,identity:'fixture engine',
    browser_wasm:'engines/release-one/harness.wasm',runtime_module:'engines/release-one/runtime.mjs'};
  engine.browser_sha256 = sha(assets.get(engine.browser_wasm)); engine.runtime_sha256 = sha(assets.get(engine.runtime_module));
  assets.set('engines.json',Buffer.from(JSON.stringify({default:engine.id,engines:[engine]})));
  const manifest = Buffer.from(JSON.stringify({format:1,entry:'agent.mjs',registry:'engines.json',
    files:[...assets].map(([name,bytes]) => ({path:name,bytes:bytes.length,sha256:sha(bytes)}))}));
  for (const [name,bytes] of assets) { await fs.mkdir(path.dirname(path.join(source,name)),{recursive:true}); await fs.writeFile(path.join(source,name),bytes); }
  await fs.writeFile(path.join(source,'agent-manifest.json'),manifest);
  await fs.writeFile(path.join(source,'current.json'),'{"site":"https://private.invalid/"}');
  await fs.writeFile(path.join(source,'deployment.json'),'{"credential":"do not copy"}');
  return {source,manifest,engine};
}
async function noHTTP(run) {
  const original = globalThis.fetch; let requests = 0;
  globalThis.fetch = async () => { requests++; throw Error('HTTP disabled for bundle test.'); };
  try { await run(); assert.equal(requests,0,'Bundle import must not attempt an HTTP request'); }
  finally { globalThis.fetch = original; }
}
async function cli(args,root,entry = client) {
  const marker = path.join(root,'http-attempts'), blocker = path.join(root,'block-http.mjs');
  await fs.writeFile(blocker,'import {appendFileSync} from "node:fs";\n' +
    'globalThis.fetch = async () => { appendFileSync(' + JSON.stringify(marker) + ',"blocked\\n"); throw Error("HTTP disabled for bundle test."); };\n');
  const result = await new Promise((resolve,reject) => {
    const child = spawn(process.execPath,['--import',blocker,entry,...args],{stdio:['ignore','pipe','pipe']});
    let stdout = '', stderr = '';
    child.stdout.on('data',value => { stdout += value; }); child.stderr.on('data',value => { stderr += value; });
    child.on('error',reject); child.on('close',code => resolve({code,stdout,stderr}));
  });
  await assert.rejects(fs.access(marker),{code:'ENOENT'});
  return result;
}

test('bundle builder copies only manifest-selected assets and cold imports into ordinary offline caches',() => scratch(async root => {
  const {source,manifest} = await fixture(root), bundle = path.join(root,'bundle');
  const built = await buildBundle({source,out:bundle});
  assert.equal(built.release,'release-one');
  assert.deepEqual(await fs.readFile(path.join(bundle,'agent-manifest.json')),manifest);
  for (const name of ['starter.tl','example.tl']) assert.equal(await fs.readFile(path.join(bundle,name),'utf8'),'teaching fixture');
  for (const name of ['songs','catalog','current.json','deployment.json','engines/another-release'])
    await assert.rejects(fs.access(path.join(bundle,name)),{code:'ENOENT'});
  const metadata = JSON.parse(await fs.readFile(path.join(bundle,'agent-bundle.json'),'utf8'));
  assert.deepEqual(Object.keys(metadata).sort(),['format','manifestSha256','release']);
  await noHTTP(async () => {
    for (const site of ['https://player.invalid/one/','https://player.invalid/two/']) {
      const cache = path.join(root,site.includes('/one/') ? 'cache-one' : 'cache-two');
      const imported = await setup({site,cache,bundle,offline:true});
      assert.equal(imported.offline,true); assert.equal(imported.site,site);
      assert.equal(imported.engine.id,'release-one');
      const saved = JSON.parse(await fs.readFile(path.join(path.dirname(imported.folder),'current.json'),'utf8'));
      assert.equal(saved.site,site); assert.equal(saved.release,'release-one');
      const warm = await setup({site,cache,offline:true});
      assert.equal(warm.manifestSha256,imported.manifestSha256); assert.equal(warm.offline,true);
    }
  });
  await assert.rejects(buildBundle({source,out:bundle}),{code:'EEXIST'});
}));

test('adjacent bundle metadata defaults the bundled CLI to cold offline setup',() => scratch(async root => {
  const {source} = await fixture(root), bundle = path.join(root,'bundle');
  await buildBundle({source,out:bundle});
  const result = await cli(['https://player.invalid/','setup','--cache',path.join(root,'cold')],root,path.join(bundle,'agent.mjs'));
  assert.equal(result.code,0,result.stderr);
  const report = JSON.parse(result.stdout);
  assert.equal(report.ready,true); assert.equal(report.offline,true); assert.equal(report.revocationChecked,false);
  const entry = path.join(bundle,'agent.mjs'), site = 'https://player.invalid/';
  const examples = await cli([site,'discover','--examples','1','--cache',path.join(root,'examples-cold')],root,entry);
  assert.equal(examples.code,2); assert.match(examples.stderr,/current online song catalog/);
  for (const command of ['prepare-publish','publish']) {
    const refused = await cli([site,command,'not-opened.tl'],root,entry);
    assert.equal(refused.code,2); assert.match(refused.stderr,/command syntax/);
  }
  assert(!(await fs.readFile(client,'utf8')).includes('api/publish'));

}));

test('standalone builder creates a neutral bundle through its local CLI',() => scratch(async root => {
  const {source} = await fixture(root), bundle = path.join(root,'bundle');
  const builder = fileURLToPath(new URL('../tools/agent_bundle.mjs',import.meta.url));
  const result = await cli(['--source',source,'--out',bundle],root,builder);
  assert.equal(result.code,0,result.stderr); assert.equal(JSON.parse(result.stdout).release,'release-one');
  assert(!(await fs.readFile(path.join(bundle,'agent-bundle.json'),'utf8')).includes('private.invalid'));
}));

test('bundle corruption, symlinks, missing files and unsupported releases refuse without HTTP fallback',() => scratch(async root => {
  const {source} = await fixture(root), bundle = path.join(root,'bundle');
  await buildBundle({source,out:bundle});
  const importing = extra => setup({site:'https://player.invalid/',cache:path.join(root,'cold'),bundle,...extra});
  await noHTTP(async () => {
    await assert.rejects(importing({release:'unavailable'}),/unavailable in this bundle/);
    const metadata = path.join(bundle,'agent-bundle.json'), originalMetadata = await fs.readFile(metadata);
    await fs.writeFile(metadata,'{"format":2}'); await assert.rejects(importing(),/Unsupported agent bundle/); await fs.writeFile(metadata,originalMetadata);
    const outsideMetadata = path.join(root,'metadata.json'); await fs.rename(metadata,outsideMetadata); await fs.symlink(outsideMetadata,metadata);
    await assert.rejects(importing(),/ELOOP|symlink/); await fs.rm(metadata); await fs.rename(outsideMetadata,metadata);
    const manifest = path.join(bundle,'agent-manifest.json'), originalManifest = await fs.readFile(manifest);
    await fs.appendFile(manifest,' '); await assert.rejects(importing(),/manifest checksum differs/); await fs.writeFile(manifest,originalManifest);
    const asset = path.join(bundle,'authoring.txt'), original = await fs.readFile(asset);
    await fs.writeFile(asset,Buffer.alloc(original.length,120)); await assert.rejects(importing(),/asset checksum differs/);
    await fs.rm(asset); await assert.rejects(importing(),{code:'ENOENT'});
    const linked = path.join(root,'linked.txt'); await fs.writeFile(linked,original); await fs.symlink(linked,asset);
    await assert.rejects(importing(),/ELOOP|symlink/); await fs.rm(asset); await fs.writeFile(asset,original);
    const engine = path.join(bundle,'engines','release-one'), real = path.join(root,'moved-engine');
    await fs.rename(engine,real); await fs.symlink(real,engine);
    await assert.rejects(importing(),/directory must not be a symlink/);
  });
  await assert.rejects(fs.access(path.join(root,'cold')),{code:'ENOENT'});
}));

test('bundle builder refuses development releases and source symlinks',() => scratch(async root => {
  const {source} = await fixture(root), out = path.join(root,'bundle');
  const link = path.join(root,'source-link'); await fs.symlink(source,link);
  await assert.rejects(buildBundle({source:link,out}),/real directory/);
  const file = path.join(source,'engines.json'), registry = JSON.parse(await fs.readFile(file,'utf8'));
  registry.engines[0].development = true; registry.engines[0].release_id = null;
  const bytes = Buffer.from(JSON.stringify(registry)); await fs.writeFile(file,bytes);
  const manifestFile = path.join(source,'agent-manifest.json'), manifest = JSON.parse(await fs.readFile(manifestFile,'utf8'));
  Object.assign(manifest.files.find(entry => entry.path === 'engines.json'),{bytes:bytes.length,sha256:sha(bytes)});
  await fs.writeFile(manifestFile,JSON.stringify(manifest));
  await assert.rejects(buildBundle({source,out}),/kept renderer release/);
  await assert.rejects(fs.access(out),{code:'ENOENT'});
}));

test('kept staged runtime composes, analyzes and verifies with a cold bundle and HTTP disabled',
  {skip:!process.env.AGENT_BUNDLE_SOURCE},() => scratch(async root => {
    const source = process.env.AGENT_BUNDLE_SOURCE, bundle = path.join(root,'bundle'), cache = path.join(root,'cold');
    await buildBundle({source,out:bundle});
    const score = path.join(root,'input.score.json');
    await fs.copyFile(path.join(bundle,'starter.score.json'),score);
    const site = 'https://player.invalid/player/', audition = path.join(root,'audition'), saved = path.join(root,'saved');
    let result = await cli([site,'compose',score,'--bundle',bundle,'--offline','--cache',cache,'--mode','listen','--analysis',
      '--out',audition,'--formats','tl,json,link'],root);
    assert.equal(result.code,0,result.stderr);
    const first = JSON.parse(await fs.readFile(audition + '.verification.json','utf8'));
    assert.equal(first.verdict,'valid'); assert.equal(first.verified,false); assert.equal(first.analysis.status,'available');
    assert.equal(first.offline,true); assert.equal(first.revocationChecked,false); assert.equal(first.play.block_indep,'unchecked');
    result = await cli([site,'verify',audition + '.tl','--offline','--cache',cache,'--out',saved,'--formats','tl,json,link'],root);
    assert.equal(result.code,0,result.stderr);
    const final = JSON.parse(await fs.readFile(saved + '.verification.json','utf8'));
    assert.equal(final.verified,true); assert.equal(final.play.match,'yes'); assert.equal(final.play.block_indep,'yes');
    assert.equal(final.offline,true); assert.equal(final.revocationChecked,false);
    assert.equal(final.pcmHash,first.pcmHash); assert.equal(final.recordSha256,first.recordSha256);
    assert.equal(final.rendererRelease,first.rendererRelease);
    assert.deepEqual(await fs.readFile(saved + '.tl'),await fs.readFile(audition + '.tl'));
    const link = new URL((await fs.readFile(saved + '.link.txt','utf8')).trim());
    assert.equal(link.origin,'https://player.invalid'); assert.equal(link.searchParams.get('release'),final.rendererRelease);
    assert.match(link.hash,/^#v1\./); assert.equal(final.outputFiles.length,3);
    const measured = path.join(root,'measured');
    result = await cli([site,'analyze',saved + '.tl','--bundle',bundle,'--cache',path.join(root,'analysis-cold'),
      '--dry-voices','[0]','--out',measured,'--formats','json'],root);
    assert.equal(result.code,0,result.stderr);
    const analysis = JSON.parse(await fs.readFile(measured + '.verification.json','utf8'));
    assert.equal(analysis.verified,true); assert.equal(analysis.analysis.status,'available');
    assert.equal(analysis.dryAnalysis.status,'available',analysis.dryAnalysis.reason);
    assert.equal(analysis.dryAnalysis.verified_mix_pcm_hash,final.pcmHash);
    assert.equal(analysis.offline,true); assert.equal(analysis.revocationChecked,false);
  }));
