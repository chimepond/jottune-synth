// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Run: node tests/browser_contract.mjs
// DOM, Worker and canvas mocks exercise interface contracts, not browser acceptance.
import assert from 'node:assert/strict';
import {setImmediate as nextTurn} from 'node:timers/promises';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {createHash} from 'node:crypto';
import {toWav, toSvg, provenance, analyzeSpectrogram, fftPlan, transformFFT} from '../web/media.mjs';
import {limits, checkRecord, checkInspection, selectEngine} from '../web/policy.mjs';
import {resolveLink, portableLink, libraryLink, isRecordLink, linkProblem} from '../web/links.mjs';
import {checkCatalogDocument as checkCatalogDocumentActual} from '../web/catalog.mjs';
import {composeDocument,bodySha256,documentSha256} from '../web/tl-document.mjs';

const tests = [];
const test = (name, run) => tests.push({name, run});
test('Shared FFT supports accurate forward/inverse analysis without accepting invalid sizes', () => {
  for (const n of [2, 16, 2048]) {
    const plan = fftPlan(n), input = Array.from({length:n}, (_, i) => Math.sin(i*.37)+Math.cos(i*.13));
    for (let i=0;i<n;i++) plan.real[plan.reverse[i]]=input[i];
    transformFFT(plan);
    const real=plan.real.slice(), imaginary=plan.imaginary.slice();
    for (let i=0;i<n;i++) {plan.real[plan.reverse[i]]=real[i];plan.imaginary[plan.reverse[i]]=imaginary[i];}
    transformFFT(plan,{inverse:true});
    for (let i=0;i<n;i++) {assert.ok(Math.abs(plan.real[i]-input[i])<1e-10);assert.ok(Math.abs(plan.imaginary[i])<1e-10);}
  }
  for (const n of [0,-1,3,1.5,2097152]) assert.throws(()=>fftPlan(n));
});
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
};
const validResult = (extra = {}) => ({
  verdict: 'valid', canonical: 'canonical fixture\n',
  recordSha256: 'a'.repeat(64), pcmHash: '0123456789abcdef',
  engineIdentity: 'engine-test', engineId: 'build-test',
  stdout: 'PLAY match=yes block_indep=yes\nMETRICS peak=12000', stderr: '',
  sampleRate: 48000, channels: 2, pcm: new Int16Array([1000, -1000, 0, 0]),
  info: {route: '0', voices: '1', length_ns: '1000000000', pcm_bytes: '8'},
  play: {peak_dbfs: '-6.00', clipped: '0', block_indep: 'yes', match: 'yes'},
  metrics: {peak: '12000'}, events: [], ...extra,
});

function catalogPages(catalog = {version:1,songs:[]}) {
  const documents = {}, categories = [];
  const row = (song, variant, note) => ({id:variant.id || song.id, title:song.title, collection:song.collection,
    revision:song.revision, variant:variant.kind, ...variant, variants:song.variants, note});
  const all = catalog.songs.map(song => row(song, song.variants.find(v => v.kind === 'keys') || song.variants[0]));
  const groups = ['featured','showcase'].map(key => [key,key,catalog.songs.flatMap(song => song.variants
    .filter(v => v.discovery?.[key]).map(v => row(song,v,v.discovery[key].note)))
    .sort((a,b) => a.discovery[key].order - b.discovery[key].order)]);
  groups.push(['all','Browse all',all]);
  for (const [i,name] of [...new Set(all.map(row => row.collection))].entries()) groups.push(['collection-'+i,name,all.filter(row => row.collection === name)]);
  for (const [field,prefix] of [['genre','Genre: '],['model','Model: ']]) {
    const values = new Map();
    for (const song of catalog.songs) for (const variant of song.variants) {
      const value = field === 'genre' ? variant.discovery?.metadata?.genre?.value : variant.discovery?.provenance?.model;
      if (value) {if (!values.has(value)) values.set(value,[]);values.get(value).push(row(song,variant));}
    }
    let i=0;for (const [value,rows] of values) groups.push([field+'-'+i++,prefix+value,rows]);
  }
  for (const [id,title,items] of groups) {
    const path = number => 'catalog/'+id+'-'+number+'.json';
    categories.push({id,title,count:items.length,first_page:items.length ? path(1) : null});
    for (let offset=0;offset<items.length;offset+=24) documents[path(offset/24+1)] = {
      version:1,category:id,total:items.length,items:items.slice(offset,offset+24),next:offset+24<items.length ? path(offset/24+2) : null};
  }
  documents['catalog/index.json']={version:1,page_size:24,total_songs:all.length,categories};
  return documents;
}

let pageNumber = 0;
async function withPage(run, configuration = {}) {
  const catalogDocuments = configuration.catalogDocuments || catalogPages(configuration.catalog);
  const registry = deferred(), workers = [], spectrumWorkers = [], analysisWorkers = [], copied = [], downloaded = [], requests = [];
  const timers = new Map(), timerDelays = new Map(), intervals = new Map(), animations = new Map(), globals = new Map();
  let timerID = 0, audioCreations = 0, registryReady = false;
  const audioEvents = [], audioContexts = [], audioBuffers = [], audioSources = [], pageEvents = [], listeners = new Map();
  const visualCalls = [], observers = [], sectionJumps = [];
  const install = (name, value) => {
    globals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, {value, writable: true, configurable: true});
  };
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag; this.value = ''; this.disabled = false;
      this.hidden = false; this.checked = false; this.children = []; this.dataset = {};
      this.textContent = ''; this.innerHTML = ''; this.className = '';
      this.listeners = new Map(); this.attributes = new Map(); this.images = []; this.labels = [];
      this.classList = {add() {}, remove() {}};
    }
    addEventListener(type, listener) {
      this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
    }
    dispatch(type, event = {}) {
      for (const listener of this.listeners.get(type) || []) listener(event);
    }
    get files() {return this.fileFixtures || [];}
    set files(values) {this.fileFixtures=values.map(file=>file.arrayBuffer ? file : {...file,
      arrayBuffer:async()=>new TextEncoder().encode(await file.text()).buffer});}
    replaceChildren(...children) { this.children = children; }
    append(...children) { this.children.push(...children); }
    prepend(...children) { this.children.unshift(...children); }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    focus() { document.activeElement = this; }
    scrollIntoView(options) { this.scrolled = options; }
    getContext(kind) {
      assert.equal(kind, '2d');
      return {createImageData: (width, height) => ({data: new Uint8ClampedArray(width * height * 4)}),
        putImageData: image => this.images.push(image), fillText: text => this.labels.push(text),
        fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}};
    }
    toBlob(callback) { callback(new Blob(['PNG fixture'], {type: 'image/png'})); }
    click() { if (!this.disabled) return this.onclick?.({target: this}); }
  }
  const filenames = [];
  const originalElementClick = Element.prototype.click;
  Element.prototype.click = function() {if (this.download) filenames.push(this.download); return originalElementClick.call(this);};
  const ids = [...readFileSync(new URL('../web/index.html', import.meta.url), 'utf8').matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  const elements = new Map(ids.map(id => [id, new Element()]));
  const get = id => {
    assert(elements.has(id), 'Unexpected element: ' + id);
    return elements.get(id);
  };
  get('auto-move').checked = configuration.autoMove ?? true;
  get('library-panel').open = true;
  class WorkerMock {
    constructor(url, options) {
      this.analysis = url.pathname.endsWith('/web/analysis-worker.mjs');
      this.spectrum = url.pathname.endsWith('/web/spectrum-worker.mjs');
      assert.equal(this.analysis || this.spectrum || url.pathname.endsWith('/web/worker.mjs'), true);
      assert.equal(options.type, 'module');
      this.terminated = false; (this.analysis ? analysisWorkers : this.spectrum ? spectrumWorkers : workers).push(this);
    }
    postMessage(message, transfer) { this.request = this.spectrum ? structuredClone(message, {transfer}) : message; }
    terminate() { this.terminated = true; }
    reply(data) {
      assert.equal(this.terminated, false, 'A terminated worker cannot send a reply');
      this.onmessage({data});
    }
  }
  const originalCreateURL = URL.createObjectURL, originalRevokeURL = URL.revokeObjectURL;
  URL.createObjectURL = blob => { downloaded.push(blob); return 'blob:contract-fixture'; };
  URL.revokeObjectURL = () => {};
  // Legacy UI fixtures use repeated-letter native-identity sentinels. Their
  // headerless transport boundary is mocked; real checksum cases below use the
  // actual helper, including every portable document and explicit full checksum.
  install('__checkCatalogDocument',async(text,variant)=>{
    if(variant.document_sha256===undefined && variant.discovery?.document_sha256===undefined
      && /^([a-f0-9])\1{63}$/.test(variant.record_sha256 || '') && !checkRecord(text).header)return text;
    return checkCatalogDocumentActual(text,variant);
  });
  install('document', {getElementById: get, createElement: tag => new Element(tag), hidden:false,
    addEventListener: (type, listener) => listeners.set('document:'+type, listener)});
  if (configuration.documentModelContext) document.modelContext = configuration.documentModelContext;
  install('__sectionChanged', undefined);
  install('__sectionNavigation', {jump: id => {
    sectionJumps.push(id);
    const focus = {'player-panel':'player-heading','live-views':'views-heading','library-panel':'library-summary'}[id];
    if (id === 'library-panel') get(id).open = true;
    globalThis.__sectionChanged?.(id);
    if (focus) get(focus).focus();
  }});
  if (configuration.observer) install('IntersectionObserver', class {
    constructor(callback) {this.callback=callback;observers.push(this);}
    observe(target) {this.target=target;}
  });
  install('__createInstrumentView', (container, config) => ({
    setResult: (result, options) => visualCalls.push(['result', result, options]),
    update: (seconds, playing) => visualCalls.push(['update', seconds, playing]),
    clear: () => visualCalls.push(['clear']),
    setVisible: visible => visualCalls.push(['visible', visible]),
  }));
  install('window', {addEventListener: (type, listener) => listeners.set(type, listener), dispatchEvent: event => pageEvents.push(event)});
  install('CustomEvent', class {constructor(type, value) {this.type = type; this.detail = value.detail;}});
  install('FormData', class extends FormData {constructor() {super();}}); // The mock provenance form has no entries.
  install('location', new URL(configuration.href || 'https://player.invalid/'));
  const navigation = {onLine:configuration.online ?? true, clipboard: {writeText: async text => {if(configuration.clipboardDenied)throw Error('Permission denied'); copied.push(text); }}};
  if (configuration.navigatorModelContext) navigation.modelContext = configuration.navigatorModelContext;
  if (configuration.serviceWorker) navigation.serviceWorker = configuration.serviceWorker;
  if (configuration.serviceWorkerAccess) Object.defineProperty(navigation,'serviceWorker',configuration.serviceWorkerAccess);
  if (configuration.audioSession) Object.defineProperty(navigation, 'audioSession', configuration.audioSession);
  if (configuration.mediaSession) {
    navigation.mediaSession = configuration.mediaSession;
    install('MediaMetadata', class {constructor(init) {Object.assign(this, init);}});
  }
  if (configuration.share) Object.assign(navigation, configuration.share);
  if (configuration.coarse !== undefined) install('matchMedia', query => ({matches: query === '(pointer: coarse)' && configuration.coarse}));
  install('navigator', navigation);
  install('Worker', WorkerMock);
  install('AudioContext', class {
    constructor() {
      audioCreations++; audioEvents.push('construct'); this.currentTime = 0; this.destination = {};
      this.state = configuration.audioResume ? 'suspended' : 'running'; audioContexts.push(this);
    }
    resume() {
      audioEvents.push('resume');
      return Promise.resolve(configuration.audioResume?.(this)).then(() => { this.state = configuration.audioResumeState || 'running'; });
    }
    createBuffer(channels, frames, rate) {
      const samples = Array.from({length: channels}, () => new Float32Array(frames));
      const buffer = {duration: frames / rate, getChannelData: channel => samples[channel]};
      audioBuffers.push(buffer); return buffer;
    }
    createBufferSource() {
      const node = {connected: false, stopped: false, startArgs: null,
        connect() { this.connected = true; }, disconnect() { this.connected = false; },
        stop() { this.stopped = true; }, start(...args) { this.startArgs = args; audioEvents.push('start'); }};
      audioSources.push(node); return node;
    }
  });
  install('fetch', async (url, fetchOptions = {}) => {
    const parsed = new URL(String(url));
    requests.push(parsed.href);
    if (parsed.pathname.endsWith('/engines.json')) return registry.promise;
    assert(!parsed.pathname.endsWith('/songs.json'), 'The browser must not fetch the full source manifest');
    const fixture = configuration.network?.[parsed.pathname + parsed.search] ?? configuration.network?.[parsed.pathname];
    const catalogPath = parsed.pathname.match(/catalog\/[^/]+\.json$/)?.[0];
    if (catalogPath && fixture === undefined) return new Response(JSON.stringify(catalogDocuments[catalogPath]), {status:configuration.catalogStatus || 200});
    assert.notEqual(fixture, undefined, 'Unexpected network request: ' + parsed.pathname);
    if (typeof fixture === 'function') return fixture(fetchOptions);
    return new Response(typeof fixture === 'string' ? fixture : JSON.stringify(fixture));
  });
  install('setTimeout', (fn, delay) => { const id = ++timerID; timers.set(id, fn); timerDelays.set(id,delay); return id; });
  install('clearTimeout', id => {timers.delete(id);timerDelays.delete(id);});
  install('setInterval', fn => { const id = ++timerID; intervals.set(id, fn); return id; });
  install('requestAnimationFrame', fn => { const id = ++timerID; animations.set(id, fn); return id; });
  install('cancelAnimationFrame', id => animations.delete(id));
  const ready = value => {
    if (!registryReady) {
      registryReady = true;
      registry.resolve({ok: true, json: async () => value || {default: 'test', engines: [{id: 'test'}]}});
    }
  };
  try {
    // Execute the real app with a browser-like module URL while importing its
    // actual dependencies. Relative asset URLs must stay inside the mount path.
    const source = readFileSync(new URL('../web/app.mjs', import.meta.url), 'utf8')
      // The display has its own numerical/DOM and actual-browser acceptance.
      .replace("import {createCatalog, catalogJSON, checkCatalogDocument} from './catalog.mjs';", "import {createCatalog, catalogJSON} from './catalog.mjs'; const checkCatalogDocument=globalThis.__checkCatalogDocument;")
      .replace("import {createInstrumentView} from './instrument-view.mjs';", 'const createInstrumentView = globalThis.__createInstrumentView;')
      // Section layout, focus and scrolling have actual-browser acceptance;
      // this harness isolates playback/rendering from the browser layout APIs.
      .replace('prepareSectionNavigation();', 'sectionNavigation = globalThis.__sectionNavigation; globalThis.__sectionChanged = currentSectionChanged;')
      .replace(/from '\.\/([^']+)'/g, (_, path) => 'from ' + JSON.stringify(new URL('../web/' + path, import.meta.url).href))
      .replaceAll('import.meta.url', JSON.stringify('https://player.invalid/web/app.mjs'));
    await import('data:text/javascript;base64,' + Buffer.from(source + '\n// page ' + (++pageNumber)).toString('base64'));
    await nextTurn();
    await run({get, workers, spectrumWorkers, analysisWorkers, ready, api: window.IntFM, copied, downloaded, filenames, timers,
      audioEvents, audioContexts, audioBuffers, audioSources, pageEvents, listeners, requests, visualCalls, sectionJumps,
      setSection: id => globalThis.__sectionChanged(id),
      animationCount: () => animations.size,
      advance: seconds => {
        for (const context of audioContexts) context.currentTime += seconds;
        for (const tick of intervals.values()) tick();
        for (const [id, draw] of [...animations]) { animations.delete(id); draw((audioContexts[0]?.currentTime || 0) * 1000); }
      },
      setHidden: value => {document.hidden=value;listeners.get('document:visibilitychange')?.();},
      setIntersecting: (value, ratio = value ? 1 : 0) => {for (const observer of observers) observer.callback([{target:observer.target,isIntersecting:value,intersectionRatio:ratio}]);},
      expire: delay => {for (const [id,fn] of [...timers]) if (timerDelays.get(id) === delay) fn();},
      audioCreations: () => audioCreations});
  } finally {
    URL.createObjectURL = originalCreateURL; URL.revokeObjectURL = originalRevokeURL;
    for (const [name, descriptor] of globals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
}

function songRows(page) {
  return page.get('song-select').children.filter(row => row.className === 'song-choice').map(row => ({
    row, radio:row.children.find(child => child.type === 'radio'),
    text:row.children.find(child => child.className === 'song-choice-label'),
  }));
}
function selectedSongId(page) {
  const checked = songRows(page).filter(({radio}) => radio.checked);
  assert(checked.length <= 1, 'The inline radio group must have one selected identity at most');
  return checked[0]?.radio.value || '';
}
function selectSong(page, id) {
  assert.equal(page.get('song-select').disabled, false, 'User selection requires an enabled radio group');
  const rows = songRows(page), chosen = rows.find(({radio}) => radio.value === id);
  assert(chosen, 'Expected a visible song choice: ' + id);
  // Model native radio-group selection before its change event. Keyboard and
  // touch behavior are exercised separately in actual browsers.
  for (const {radio} of rows) radio.checked = radio === chosen.radio;
  chosen.radio.dispatch('change', {target:chosen.radio});
}

async function complete(page, text = 'authored fixture', result = validResult()) {
  page.ready();
  const promise = page.api.render(text);
  await nextTurn();
  page.workers.at(-1).reply(result);
  assert.equal(await promise, result);
  return result;
}
const starterScore = () => JSON.parse(readFileSync(new URL('../web/starter.score.json',import.meta.url),'utf8'));
const toolValue = response => JSON.parse(response.content[0].text);
function withModelTools(run, config = {}) {
  const tools = new Map();
  return withPage(page => run(page, tools), {...config, documentModelContext:{registerTool:tool => tools.set(tool.name,tool)}});
}
test('WebMCP reports unsupported, pending, failed and successful registrations independently', async () => {
  await withPage(page => {
    assert.deepEqual(page.api.describe().webmcp,{intfm_render:'unsupported',intfm_compose:'unsupported'});
    assert.match(page.get('webmcp-status').textContent,/intfm_compose unsupported/);
  });
  for (const failsAsync of [false,true]) {
    const pending = deferred();
    await withPage(async page => {
      assert.deepEqual(page.api.describe().webmcp,{intfm_render:'failed',intfm_compose:'registering'});
      assert.match(page.get('webmcp-status').textContent,/intfm_compose registering/);
      pending.resolve(); await nextTurn();
      assert.deepEqual(page.api.describe().webmcp,{intfm_render:'failed',intfm_compose:'registered'});
      assert.match(page.get('webmcp-status').textContent,/intfm_render failed.*intfm_compose registered/);
    }, {navigatorModelContext:{registerTool(tool) {
      if (tool.name === 'intfm_compose') return pending.promise;
      if (failsAsync) return Promise.reject(Error('policy'));
      throw Error('policy');
    }}});
  }
});
const toolRelease = {id:'kept-a', release_id:'kept-a', identity:'int-fm-synth 1'};
async function finishToolScore(page, tool, canonical, options = {}) {
  const score = options.score ?? starterScore();
  const pending = tool.execute({score,release:options.release ?? toolRelease.id});
  await nextTurn();
  const first = page.workers.at(-1);
  assert.equal(first.request.mode,'listen');
  assert.equal(first.request.engine.id,options.release ?? toolRelease.id);
  if (options.expectedDraft) assert.equal(first.request.recordText,options.expectedDraft);
  const documentSha256 = createHash('sha256').update(canonical).digest('hex');
  const fixture = {canonical,documentSha256,recordSha256:documentSha256,rendererRelease:toolRelease.id,metadata:score.metadata,...options.fixture};
  first.reply(validResult({...fixture, play:{match:'-',block_indep:'unchecked'}}));
  await nextTurn();
  const replay = page.workers.at(-1);
  assert.notEqual(replay,first); assert.equal(replay.request.mode,'verify');
  assert.equal(replay.request.engine.id,first.request.engine.id);
  assert.equal(replay.request.recordText,new TextDecoder().decode(canonical));
  replay.reply(validResult({...fixture,...options.replay}));
  return pending;
}
test('WebMCP composes, verifies and exports exact canonical TL and pinned URL through registered tools alone', () => withModelTools(async (page, tools) => {
  assert.deepEqual([...tools.keys()],['intfm_render','intfm_compose']);
  const tool = tools.get('intfm_compose');
  page.ready({default:toolRelease.id,engines:[toolRelease]});
  for (const large of [false,true]) {
    const noise = Array.from({length:220}, (_,i) => createHash('sha256').update(String(i)).digest('hex')).join('\n# ');
    const canonical = new TextEncoder().encode('# A tune 🎶\n' + (large ? '# '+noise+'\n' : '') + 'hash 0123456789abcdef\n');
    const response = await finishToolScore(page,tool,canonical), result = toolValue(response);
    assert.equal(response.isError,undefined); assert.equal(result.title,'A first conversation');
    assert.equal(result.summary.verified,true); assert.equal(result.summary.play.block_indep,'yes');
    assert.equal(result.summary.pcm,undefined); assert.equal(result.summary.authoring.advisories,undefined);
    assert.equal(typeof result.summary.authoring.advisoryCount,'number');
    const {documentSha256} = result.tl;
    assert.equal(result.summary.documentSha256,documentSha256);
    async function retrieve(format, chunk) {
      const bytes = [];
      let offset = 0;
      do {
        chunk ||= toolValue(await tool.execute({documentSha256,format,offset}))[format];
        const buffer = Buffer.from(chunk.base64,'base64');
        assert.equal(chunk.offset,offset); assert.equal(chunk.documentSha256,documentSha256);
        assert.equal(chunk.verified,true); assert.ok(buffer.length <= 3000);
        assert.equal(createHash('sha256').update(buffer).digest('hex'),chunk.chunkSha256);
        bytes.push(buffer);
        if (chunk.nextOffset === null) break;
        offset += buffer.length;
        assert.equal(chunk.nextOffset,offset); chunk = null;
      } while (true);
      const value = Buffer.concat(bytes);
      assert.equal(value.length,chunk.totalBytes); assert.equal(createHash('sha256').update(value).digest('hex'),chunk.payloadSha256);
      return value;
    }
    assert.deepEqual(await retrieve('tl',result.tl),Buffer.from(canonical));
    const url = (await retrieve('link')).toString();
    assert.equal(Buffer.byteLength(url),result.urlBytes); assert.equal(result.url,large ? null : url);
    assert.equal(new URL(url).searchParams.get('release'),toolRelease.release_id); assert.match(new URL(url).hash,/^#v1\./);
    assert.equal((await resolveLink(url,new URL('https://player.invalid/web/'))).recordText,new TextDecoder().decode(canonical));
    page.api.clear();
    const stale = await tool.execute({documentSha256,offset:0});
    assert.equal(stale.isError,true); assert.match(toolValue(stale).error,/no longer active/);
  }
  assert.equal(page.audioCreations(),0);
  assert.equal(page.downloaded.length,0);
}));
test('WebMCP replaces an equivalent pasted score with its own editable source and carries advisories through replay', () => withModelTools(async (page,tools) => {
  page.ready({default:toolRelease.id,engines:[toolRelease]});
  const previous = starterScore();
  const note = previous.events.find(event => event.kind === 'note');
  previous.events.push({...note}); // Intentional overlap generates a retrigger advisory.
  const score = structuredClone(previous);
  score.events[0].at = '0/1'; // Equivalent musical time, distinct editable JSON.
  const canonical = new TextEncoder().encode('# saved canonical fixture\nhash 0123456789abcdef\n');
  page.get('record').value = JSON.stringify(previous);
  const preparing = page.get('prepare-input').click(); await nextTurn();
  const draft = page.workers.at(-1).request.recordText;
  page.workers.at(-1).reply(validResult({canonical,metadata:previous.metadata})); await preparing;
  const response = await finishToolScore(page,tools.get('intfm_compose'),canonical,{score,expectedDraft:draft});
  assert.equal(response.isError,undefined);
  assert.equal(page.get('score-download').hidden,false);
  page.get('score-download').click();
  assert.deepEqual(JSON.parse(await page.downloaded.at(-1).text()),score);
  assert.match(page.filenames.at(-1),/A first conversation-source\.score\.json$/);
  await page.get('agent-report').click();
  const report = JSON.parse(page.copied.at(-1));
  assert.equal(report.song.title,score.metadata.title); assert.equal(report.song.verified,true);
  assert.ok(report.song.authoring.advisory_count > 0);
  assert.equal(report.song.authoring.advisory_count,toolValue(response).summary.authoring.advisoryCount);
}));
test('WebMCP returns score pointers, native refusals and file-only results for development renderers', () => withModelTools(async (page,tools) => {
  const tool = tools.get('intfm_compose'), dev = {id:'dev',identity:toolRelease.identity};
  page.ready({default:toolRelease.id,engines:[toolRelease,dev]});
  for (const input of [{},{score:'JSON text'},{score:{},offset:2},{documentSha256:'missing',offset:0}]) {
    const response = await tool.execute(input);
    assert.equal(response.isError,true); assert.match(toolValue(response).error,/Pass \{score/);
  }
  const invalid = await tool.execute({score:{...starterScore(),fs:1}});
  assert.deepEqual(toolValue(invalid),{error:'fs must be 44100 or 48000.',pointer:'/fs'});
  const missing = await tool.execute({score:starterScore(),release:'withdrawn-or-missing'});
  assert.match(toolValue(missing).error,/not available/); assert.equal(page.workers.length,0);
  const refused = tool.execute({score:starterScore()});
  await nextTurn(); page.workers.at(-1).reply(validResult({verdict:'refused',stderr:'native input error'}));
  assert.equal(toolValue(await refused).error,'native input error'); assert.equal(page.workers.length,1);
  const canonical = new TextEncoder().encode('hash 0123456789abcdef\n');
  const mismatch = await finishToolScore(page,tool,canonical,{replay:{pcmHash:'different'}});
  assert.equal(mismatch.isError,true); assert.equal(toolValue(mismatch).tl,undefined);
  const response = await finishToolScore(page,tool,canonical,{release:dev.id,fixture:{rendererRelease:null,engineId:dev.id,development:true}});
  const result = toolValue(response); assert.equal(response.isError,undefined);
  assert.equal(result.url,null); assert.match(result.urlError,/no kept release/); assert.equal(result.urlBytes,undefined);
  assert.equal(result.tl.verified,true);
  const link = await tool.execute({documentSha256:result.tl.documentSha256,offset:0,format:'link'});
  assert.match(toolValue(link).error,/unavailable/);
}));
test('WebMCP cancellation and supersession leave a newer page render in control', () => withModelTools(async (page,tools) => {
  const tool = tools.get('intfm_compose'), score = starterScore();
  const early = new AbortController(), waiting = tool.execute({score},{signal:early.signal});
  early.abort(); await assert.rejects(waiting,/abort/i);
  const stale = tool.execute({score}); page.api.clear();
  page.ready({default:toolRelease.id,engines:[toolRelease]});
  assert.match(toolValue(await stale).error,/cancelled/); assert.equal(page.workers.length,0);
  const controller = new AbortController(), cancelled = tool.execute({score},{signal:controller.signal});
  await nextTurn(); controller.abort(); await assert.rejects(cancelled,/abort/i);
  assert.equal(page.workers[0].terminated,true);
  const previous = new AbortController(), superseded = tool.execute({score},{signal:previous.signal});
  await nextTurn();
  const current = tools.get('intfm_render').execute({recordText:'current TL',mode:'listen'});
  previous.abort(); await assert.rejects(superseded,/abort/i); await nextTurn();
  const worker = page.workers.at(-1);
  assert.equal(worker.request.recordText,'current TL'); assert.equal(worker.terminated,false);
  worker.reply(validResult()); const result = toolValue(await current);
  assert.equal(result.verdict,'valid'); assert.equal(result.mode,'listen'); assert.equal(result.verified,false);
}));
test('Musical score compilation selects exact release without rendering or playing', () => withPage(async page => {
  page.ready({default:'kept-a',engines:[{id:'kept-a',release_id:'kept-a',identity:'int-fm-synth 1'}]});
  const score=JSON.parse(readFileSync(new URL('../web/starter.score.json',import.meta.url),'utf8'));
  const compiled=await page.api.compose(score,{release:'kept-a'});
  assert.equal(compiled.rendererRelease,'kept-a');assert.ok(compiled.draftText.includes('int-fm-synth 1'));
  assert.ok(compiled.sourceMap.length);assert.equal(page.workers.length,0);assert.equal(page.audioCreations(),0);
  assert.equal(page.api.recipes().length,60);
  assert.equal(page.api.recipe('flute','wind',{c_level:6}),page.api.recipeGain('flute','wind',-6));
  await assert.rejects(page.api.compose(score,{release:'missing'}),/not available/);
}));
test('Score stream selection uses a cancellable native inspection worker and preserves prepared audio', () => withPage(async page => {
  page.ready({default:'test',engines:[{id:'test',identity:'int-fm-synth 1'}]});
  const current=await complete(page), score=JSON.parse(readFileSync(new URL('../web/starter.score.json',import.meta.url),'utf8'));
  const pending=page.api.compose(score,{soloStreams:[score.streams[0]]});await nextTurn();
  const worker=page.workers.at(-1);
  assert.equal(worker.request.operation,'compose-selection');assert.equal(worker.request.engine.id,'test');
  assert.deepEqual(worker.request.selection,{soloStreams:[score.streams[0]]});
  assert.deepEqual(worker.request.score,score);
  worker.reply({type:'composition',verdict:'valid',draftText:'selected draft',coverage:{verification:'unverified_draft'}});
  const chosen=await pending;assert.equal(chosen.draftText,'selected draft');assert(worker.terminated);
  assert.equal(page.api.result(),current);assert.equal(page.audioCreations(),0);
  await assert.rejects(page.api.compose(score,{soloStreams:[],muteStreams:[]}),/either/);
  const stopped=page.api.compose(score,{muteStreams:[score.streams[0]]});await nextTurn();
  page.api.cancel();await assert.rejects(stopped,/cancelled/);assert(page.workers.at(-1).terminated);
  const invalid=page.api.compose(score,{soloStreams:['unknown']});await nextTurn();
  page.workers.at(-1).reply({verdict:'refused',code:2,stderr:'Selected stream is not declared.'});
  await assert.rejects(invalid,/not declared/);assert.equal(page.api.result(),current);
}));
test('Score selection freezes source and cancels before the registry becomes available', () => withPage(async page => {
  const score=JSON.parse(readFileSync(new URL('../web/starter.score.json',import.meta.url),'utf8'));
  const selection=[score.streams[0]], original=JSON.stringify(score);
  const first=page.api.compose(score,{soloStreams:selection});
  page.api.cancel();await assert.rejects(first,/cancelled/);
  const next=page.api.compose(score,{soloStreams:selection});
  score.events.length=0;selection[0]='changed';
  page.ready({default:'test',engines:[{id:'test',identity:'int-fm-synth 1'}]});await nextTurn();
  assert.equal(page.workers.length,1);
  assert.equal(JSON.stringify(page.workers[0].request.score),original);
  assert.notEqual(page.workers[0].request.selection.soloStreams[0],'changed');
  page.api.cancel();await assert.rejects(next,/cancelled/);
}));
test('An older pending score selection cannot supersede a newer render', () => withPage(async page => {
  const score=JSON.parse(readFileSync(new URL('../web/starter.score.json',import.meta.url),'utf8'));
  const older=page.api.compose(score,{soloStreams:[score.streams[0]]});
  const newer=page.api.render('current song');await assert.rejects(older,/cancelled/);
  page.ready();await nextTurn();assert.equal(page.workers.length,1);
  assert.equal(page.workers[0].request.operation,undefined);assert.equal(page.workers[0].request.recordText,'current song');
  page.workers[0].reply(validResult());await newer;
}));
test('Cached audio analysis retains buffers and reports exact result identity', () => withPage(async page => {
  const result=await complete(page,'saved',validResult({engineId:'test',rendererRelease:'test'}));
  const before=result.pcm.slice(), promise=page.api.analyze(result,{windowSeconds:4});await nextTurn();
  const worker=page.analysisWorkers.at(-1);assert.equal(worker.request.result.pcm,result.pcm);
  worker.reply({result:{pcm:{sample_peak_dbfs:-6},events:{voices:[]}}});
  const report=await promise;assert.equal(report.recordSha256,result.recordSha256);assert.deepEqual(result.pcm,before);
  assert.equal(page.api.result(),result);assert.equal(page.audioCreations(),0);assert.equal(worker.terminated,true);
  assert.equal(page.get('analysis-download').disabled,false);
}));
test('clipboard reports native levels immediately and bound completed measurements without changing analysis',()=>withPage(async page=>{
  const rows=Array.from({length:24},(_,i)=>i);
  const result=await complete(page,'score',validResult({engineId:'test',
    play:{match:'yes',block_indep:'yes',peak:'12000',peak_dbfs:'-8.73',rms_dbfs:'-20.00',clipped:'0',samples:'2'},
    authoring:{counts:{voices:1},advisories:rows.map(i=>({kind:'retrigger',event_index:i}))}}));
  async function copy() {
    await page.get('agent-report').click();
    return JSON.parse(page.copied.at(-1));
  }
  const before=await copy();
  assert.equal(before.song.play.peak,'12000');assert.equal(before.song.play.peak_dbfs,'-8.73');
  assert.equal(before.song.play.clipped,'0');assert.equal(before.song.play.samples,'2');
  assert.match(before.analysisStatus,/Not run.*LUFS/);assert(!before.analysis);
  assert.equal(before.song.authoring.advisory_count,24);assert.equal(before.song.authoring.advisories.length,8);
  assert.equal(page.api.summary().authoring.advisories.length,24);assert.equal(page.analysisWorkers.length,0);
  const pending=page.api.analyze(result);await nextTurn();
  const worker=page.analysisWorkers.at(-1);
  assert.match((await copy()).analysisStatus,/running/);assert.equal(worker.terminated,false);
  const section={name:'Hook',start_seconds:1,end_seconds:2,rms_dbfs:-17,sample_peak_dbfs:-3,
    true_peak_dbtp:-2.8,loudness:{integrated_lufs:-15},per_channel:rows};
  const stream={stream:'bells',voices:[0,1],peak_db_q30_unity:-5,rms_db_q30_unity:-18,loudness:{integrated_lufs:-19}};
  const raw={pcm:{sample_peak:12000,full_scale_samples:0,trailing_digital_silence_seconds:0.25,
    loudness:{integrated_lufs:-15,true_peak_dbtp:-2.8},stereo:{correlation:0.4},per_channel:rows,
    sections:rows.map(()=>section),windows:rows.map(()=>section)},events:{voices:rows},
    dry:{scope:'voice_out_pre_fx',status:'available',streams:rows.map(()=>stream),voices:rows}};
  worker.reply({result:raw});const full=await pending, measured=(await copy()).analysis;
  assert.equal(measured.recordSha256,result.recordSha256);assert.equal(measured.pcm.sample_peak,12000);
  assert.equal(measured.pcm.trailing_digital_silence_seconds,0.25);assert.equal(measured.pcm.stereo.correlation,0.4);
  assert.equal(measured.pcm.loudness.integrated_lufs,-15);assert.equal(measured.pcm.loudness.true_peak_dbtp,-2.8);
  assert.equal(measured.pcm.window_count,24);assert.equal(measured.pcm.section_count,24);
  assert.equal(measured.pcm.sections.length,8);assert.equal(measured.pcm.sections[0].integrated_lufs,-15);
  assert.equal(measured.dry.voice_count,24);assert.equal(measured.dry.stream_count,24);
  assert.equal(measured.dry.streams.length,8);assert.equal(measured.dry.streams[0].dual_mono_integrated_lufs,-19);
  for(const key of ['windows','per_channel','events','voices'])assert(!page.copied.at(-1).includes('"'+key+'": ['));
  assert(Buffer.byteLength(page.copied.at(-1))<9000);
  page.get('analysis-download').click();
  assert.deepEqual(JSON.parse(await page.downloaded.at(-1).text()),full);assert.equal(full.pcm.windows.length,24);
  assert.equal(page.analysisWorkers.length,1);
}));
test('Edits cancel pending analysis and discard its display', () => withPage(async page => {
  const result=await complete(page,'saved',validResult({engineId:'test'}));
  const pending=page.api.analyze(result);await nextTurn();const worker=page.analysisWorkers.at(-1);
  page.get('record').value='new draft';page.get('record').dispatch('input');
  await assert.rejects(pending,/cancelled/);assert.equal(worker.terminated,true);assert.equal(page.api.result() ?? null,null);
}));
test('Concurrent analysis during registry wait admits only the latest request', () => withPage(async page => {
  const result=validResult({engineId:'test'}), first=page.api.analyze(result), rejected=assert.rejects(first,/cancelled/);
  const second=page.api.analyze(result);page.ready();await nextTurn();await rejected;
  assert.equal(page.analysisWorkers.length,1);page.analysisWorkers[0].reply({result:{pcm:{},events:{}}});await second;
}));
test('Dry worker deadline preserves completed final PCM analysis', () => withPage(async page => {
  const result=await complete(page,'saved',validResult({engineId:'test'}));
  const pending=page.api.analyze(result,{dryVoices:[0],dryTimeoutMs:1000});await nextTurn();
  const worker=page.analysisWorkers.at(-1);worker.reply({phase:'dry',timeoutMs:1000,partial:{pcm:{sample_peak_dbfs:-6},events:{}}});
  page.expire(1000);const report=await pending;assert.equal(report.pcm.sample_peak_dbfs,-6);
  assert.equal(report.dry.status,'unavailable');assert.equal(worker.terminated,true);assert.equal(page.api.result(),result);
}));
test('Overall timeout after dry phase retains completed PCM and event measurements', () => withPage(async page => {
  const result=await complete(page,'saved',validResult({engineId:'test'}));
  const pending=page.api.analyze(result,{dryVoices:[0],dryTimeoutMs:10000,timeoutMs:1000});await nextTurn();
  const worker=page.analysisWorkers.at(-1);worker.reply({phase:'dry',timeoutMs:10000,partial:{pcm:{sample_peak_dbfs:-8},events:{}}});
  page.expire(1000);const report=await pending;assert.equal(report.pcm.sample_peak_dbfs,-8);
  assert.equal(report.dry.status,'unavailable');assert.equal(worker.terminated,true);assert.equal(page.api.result(),result);
}));
test('Visible analysis controls use named section and dry-source API options', () => withPage(async page => {
  await complete(page,'saved',validResult({engineId:'test'}));
  page.get('analysis-window').value='2';page.get('analysis-dry').checked=true;page.get('analysis-section').checked=true;
  page.get('analysis-section-name').value='Hook';page.get('analysis-section-start').value='0';page.get('analysis-section-end').value='.5';
  const pending=page.get('analyze-audio').onclick();await nextTurn();const worker=page.analysisWorkers.at(-1);
  assert.deepEqual(worker.request.options,{windowSeconds:2,dryVoices:true,sections:[{name:'Hook',startSeconds:0,endSeconds:.5}]});
  worker.reply({result:{pcm:{windows:[],sections:[{name:'Hook',start_seconds:0,end_seconds:.5,rms_dbfs:-10,true_peak_dbtp:-3,loudness:{integrated_lufs:-12}}],loudness:{true_peak_dbtp:-2}},events:{},dry:{status:'unavailable',reason:'Bounded test'}}});
  await pending;assert.match(page.get('analysis-output').textContent,/Hook/);assert.match(page.get('analysis-output').textContent,/Estimated true peak: -2.00/);
  assert.match(page.get('analysis-output').textContent,/Hook .* -3.00 dBTP estimated true peak/);
  assert.match(page.get('analysis-output').textContent,/Dry sources unavailable: Bounded test/);assert.equal(page.get('analysis-download').disabled,false);
}));
async function prepared(page, pending, text, result = validResult({play: {match: 'yes', block_indep: 'unchecked'}})) {
  page.ready(); await nextTurn();
  const worker = page.workers.at(-1);
  assert(worker && !worker.terminated, 'Loading a record must prepare playback');
  assert.equal(worker.request.mode, 'listen');
  assert.equal(worker.request.recordText, text);
  assert.equal(page.get('play').disabled, true, 'Play must wait for the render result');
  assert.equal(page.audioCreations(), 0, 'Preparing playback must not acquire audio');
  worker.reply(result); await pending; await nextTurn();
  return page.api.result();
}
function invalidActions(page) {
  for (const id of ['play', 'share', 'link-download', 'show-qr', 'tl-download']) {
    assert.equal(page.get(id).disabled, true, id + ' must be disabled');
    page.get(id).click();
  }
  assert.equal(page.audioCreations(), 0);
  assert.equal(page.copied.length, 0);
  assert.equal(page.downloaded.length, 0);
}

function installingService() {
  const service = new EventTarget(), worker = new EventTarget();
  worker.state = 'installing'; service.controller = null;
  service.register = async () => ({installing:worker});
  service.activate = (scriptURL = 'https://player.invalid/web/sw.js') => {
    service.controller = {scriptURL};service.dispatchEvent(new Event('controllerchange'));
  };
  service.fail = () => {worker.state = 'redundant';worker.dispatchEvent(new Event('statechange'));};
  return service;
}

test('optional offline installation never blocks first rendering or claims an uncached renderer', async () => {
  const service = installingService();
  await withPage(async page => {
    page.ready(); const pending = page.api.listen('cold first song'); await nextTurn();
    assert.equal(page.workers.length,1,'Rendering starts before optional precaching finishes');
    service.activate('https://player.invalid/other/sw.js');await nextTurn();
    assert.equal(page.workers.length,1,'A different worker cannot restart the render');
    service.activate();await nextTurn();
    page.workers[0].reply(validResult());await pending;
    assert.match(page.get('offline-status').textContent,/Used renderers may be cached/);
    await complete(page,'controlled song');
    assert.match(page.get('offline-status').textContent,/Used renderers may be cached/);
    const staleReply = page.workers[1].onmessage;
    const next = page.api.listen('refused song');await nextTurn();
    staleReply({data:validResult()});
    assert.match(page.get('offline-status').textContent,/Used renderers may be cached/);
    page.workers[2].reply({verdict:'refused',code:2,stderr:'Unavailable renderer'});await next;
    assert.match(page.get('offline-status').textContent,/Used renderers may be cached/);
  }, {serviceWorker:service});
});

test('denied service worker access leaves online playback usable', () => withPage(async page => {
  await complete(page);
  assert.match(page.get('offline-status').textContent,/unavailable; online/);
}, {serviceWorkerAccess:{get() {throw new DOMException('Denied','SecurityError');}}}));

test('cancellation and replacement remain immediate during offline installation', async () => {
  const service = installingService();
  await withPage(async page => {
    page.ready(); const first = page.api.listen('cancelled song');
    const refused = assert.rejects(first,/cancelled/);page.api.cancel();await refused;
    const second = page.api.listen('replaced song'), replaced = assert.rejects(second,/cancelled/);
    const latest = page.api.listen('latest song');await replaced;
    service.activate();await nextTurn();
    assert.equal(page.workers.length,1);assert.equal(page.workers[0].request.recordText,'latest song');
    page.workers[0].reply(validResult());await latest;
  }, {serviceWorker:service});
});

test('an installed worker that does not control the page asks for a reload instead of waiting forever', async () => {
  const service = installingService(), worker = new EventTarget();
  worker.state = 'activated'; service.register = async () => ({active:worker});
  await withPage(async page => {
    page.ready();const pending = page.api.listen('hard reload song');await nextTurn();
    assert.match(page.get('offline-status').textContent,/installed; reload the page/);
    page.expire(15000);await nextTurn();
    assert.match(page.get('offline-status').textContent,/installed; reload the page/);
    page.workers[0].reply(validResult());await pending;
    service.activate();await nextTurn();
    assert.match(page.get('offline-status').textContent,/Offline app is ready/);
  }, {serviceWorker:service});
});

for (const reason of ['rejected','redundant','stalled']) test(`offline setup ${reason} falls back online without claiming cached playback`, async () => {
  const service = installingService();
  if (reason === 'rejected') service.register = async () => {throw Error('Registration unavailable');};
  await withPage(async page => {
    page.ready();const pending = page.api.listen('online song');await nextTurn();
    if (reason === 'redundant') service.fail();
    if (reason === 'stalled') page.expire(15000);
    await nextTurn();assert.equal(page.workers.length,1);
    page.workers[0].reply(validResult());await pending;
    // A slow installation is still running, not unavailable, and its late outcome is shown (UI-6).
    assert.match(page.get('offline-status').textContent,reason==='stalled'?/still running; online playback is available/:/unavailable; online/);
    service.activate();await nextTurn();
    assert.equal(page.workers.length,1,'Late installation must not restart or relabel the uncontrolled render');
    assert.match(page.get('offline-status').textContent,reason==='stalled'?/Offline app is ready/:/unavailable; online/);
    await complete(page,'later controlled song');
    assert.match(page.get('offline-status').textContent,/Used renderers may be cached/);
  }, {serviceWorker:service});
});

test('a render deadline while startup is pending cannot launch a late worker', async () => {
  const service = installingService();
  await withPage(async page => {
    const pending = page.api.listen('expired song'), refused = assert.rejects(pending,/120-second/);
    page.expire(120000);await refused;
    page.ready();service.activate();await nextTurn();
    assert.equal(page.workers.length,0);
  }, {serviceWorker:service});
});

test('pending registry starts only the latest simultaneous render', () => withPage(async page => {
  let firstSettled = false;
  const first = page.api.render('first');
  const superseded = assert.rejects(first, /cancelled/i).then(() => { firstSettled = true; });
  const second = page.api.render('second');
  await nextTurn();
  const settledBeforeRegistry = firstSettled;
  assert.equal(page.workers.length, 0);
  page.ready(); await nextTurn();
  await superseded;
  assert.equal(page.workers.length, 1);
  assert.equal(page.workers[0].request.recordText, 'second');
  page.workers[0].reply(validResult()); await second;
  assert.equal(page.timers.size, 0);
  assert.equal(settledBeforeRegistry, true, 'Superseded render kept waiting for engines.json');
}));

test('active render supersession terminates and settles the previous call', () => withPage(async page => {
  page.ready();
  const first = page.api.render('first');
  const superseded = assert.rejects(first, /cancelled/i);
  await nextTurn();
  const previous = page.workers[0];
  const second = page.api.render('second');
  await superseded; await nextTurn();
  assert.equal(previous.terminated, true);
  assert.equal(page.workers.length, 2);
  const cancelled = assert.rejects(second, /cancelled/i);
  page.get('cancel').click(); await cancelled;
  assert.equal(page.workers[1].terminated, true);
  assert.equal(page.timers.size, 0);
}));

test('cancel settles a call even while the engine registry is pending', () => withPage(async page => {
  let settled = false;
  const result = page.api.render('pending').then(
    () => { settled = true; return {resolved: true}; },
    error => { settled = true; return {error}; });
  page.get('cancel').click(); await nextTurn();
  const settledBeforeRegistry = settled;
  // Resolve the fixture after observing cancellation so no pending work leaks to another test.
  page.ready(); await nextTurn();
  assert.match((await result).error?.message || '', /cancelled/i);
  assert.equal(page.workers.length, 0);
  assert.equal(settledBeforeRegistry, true, 'Cancel left the API call waiting for engines.json');
}));

test('agent render updates the visible result and export controls', () => withPage(async page => {
  const result = await complete(page);
  assert.equal(page.get('record').value, 'authored fixture');
  assert.equal(page.get('verdict').textContent, 'Verified · ready to play');
  assert.equal(page.get('verdict').getAttribute('title'), 'Verified');
  assert.equal(page.get('status').textContent, 'Ready. Press Play to listen.');
  assert.equal(page.workers[0].request.mode, 'verify');
  assert.equal(result.verified, true);
  assert.equal(page.pageEvents.at(-1).detail, result);
  assert.match(page.get('raw').textContent, /PLAY match=yes/);
  assert.match(page.get('timeline').innerHTML, /^<svg /);
  assert(page.get('facts').children.some(child => child.textContent === result.recordSha256));
  for (const id of ['play', 'share', 'link-download', 'show-qr', 'tl-download']) assert.equal(page.get(id).disabled, false);
}));

test('local portable and legacy files show compatibility versions and declared metadata in Facts',async()=>{
  for(const portable of [true,false]) {
    const body='timeline draft1\n',metadata={title:'Visible document',collection:'Examples',author:'Composer',license:'CC-BY-4.0',utc_day:'2026-11-10',prompt_id:'fixture/1',renderer_release:'reported-release'};
    const text=portable?composeDocument(body,metadata):body;
    await withPage(async page=>{
      const blob=new Blob([text]);page.get('file').files=[{size:blob.size,name:'version-check.tl',arrayBuffer:()=>blob.arrayBuffer()}];
      const result=validResult({canonical:new TextEncoder().encode(text),tlVersion:1,engineCompatibilityVersion:1,
        documentHeader:portable?'present':'legacy-headerless',metadata:portable?metadata:{},play:{match:'yes',block_indep:'unchecked'}});
      await prepared(page,page.get('file').onchange(),text,result);
      const facts=page.get('facts').children.map(child=>child.textContent).join('\n');
      assert.match(facts,/TL version\n1/);assert.match(facts,/Engine compatibility version\n1/);
      assert.match(facts,portable?/Document header\nPortable metadata/:/1 \(legacy, inferred\)\nEngine compatibility version\n1\nDocument header\nLegacy headerless/);
      if(portable)for(const value of Object.values(metadata))assert.ok(facts.includes(value),value);
      assert.equal(page.get('loaded-song').textContent,portable ? metadata.title : 'version-check');
      assert.equal(page.get('score-download').hidden,true);
      assert.equal(page.get('play').disabled,false);assert.equal(page.audioCreations(),0);
    });
  }
});

test('catalog loading and note inspection reject a changed metadata checksum before native work',async()=>{
  const body='timeline draft1\n',original=composeDocument(body,{title:'Expected declaration'}),altered=composeDocument(body,{title:'Changed declaration'});
  const catalog=largeCatalog(1),variant=catalog.songs[0].variants[0];
  variant.record_sha256=await bodySha256(original);variant.document_sha256=await documentSha256(original);
  variant.bytes=new TextEncoder().encode(original).length;
  variant.discovery={record_sha256:variant.record_sha256,document_sha256:variant.document_sha256,source:'library.json'};
  assert.equal(await bodySha256(altered),variant.record_sha256);
  await withPage(async page=>{
    page.ready();await page.get('song-load').onclick();
    assert.equal(page.workers.length,0);assert.equal(page.api.result()??null,null);
    assert.match(page.get('status').textContent,/document checksum does not match/);assert.equal(page.get('play').disabled,true);
    await page.get('song-preview').onclick();
    assert.equal(page.workers.length,0);assert.match(page.get('song-preview-status').textContent,/document checksum does not match/);
  },{catalog,network:{['/web/'+variant.path]:altered}});
});

test('verification budget refusals leave prepared listening audio intact and disable known impossible actions',()=>withPage(async page=>{
  const value=validResult({info:{route:'0',voices:'1',length_ns:'286480000000',pcm_bytes:'55004160',render_bytes_lower_bound:'116222544'}});
  await complete(page,'large listened song',value);
  assert.equal(page.get('play').disabled,false);
  for(const id of ['render','verify-saved','profile-apply'])assert.equal(page.get(id).disabled,true);
  await assert.rejects(page.api.verifySaved(),/fixed budget/);
  assert.equal(page.api.result(),value);assert.equal(page.get('play').disabled,false);
}));

test('editing text invalidates playback and canonical sharing', () => withPage(async page => {
  await complete(page);
  page.get('record').value = 'changed fixture'; page.get('record').dispatch('input');
  invalidActions(page);
  assert.equal(page.get('verdict').textContent, 'Not ready');
  assert.equal(page.get('facts').children.length, 0);
}));

test('intentional editing preserves a selected retained renderer', () => withPage(async page => {
  page.ready({default:'default',engines:[{id:'default'},{id:'kept',release_id:'2026-10-25-abcdef0'}]});
  let pending=page.api.verify('saved original',{release:'2026-10-25-abcdef0'});
  await nextTurn();page.workers.at(-1).reply(validResult());await pending;
  page.get('record').value='edited draft';page.get('record').dispatch('input');
  pending=page.api.verify(page.get('record').value);await nextTurn();
  assert.equal(page.workers.at(-1).request.engine.id,'kept');
  page.workers.at(-1).reply(validResult({play:{match:'-',block_indep:'yes'}}));await pending;
  assert.equal(page.api.result().verified,false);
}));

test('unfinished or refused draft text can be saved without rendering or uploading', () => withPage(async page => {
  const text='# Ω — unfinished\nnot a valid song\n';
  page.get('record').value=text;page.get('record').dispatch('input');
  page.get('draft-download').click();
  assert.equal(await page.downloaded[0].text(),text);
  assert.equal(page.workers.length,0);assert.equal(page.audioCreations(),0);
  const result=await complete(page,text,{verdict:'refused',code:2,stderr:'Native syntax error'});
  assert.equal(result.verdict,'refused');
  page.get('draft-download').click();
  assert.equal(await page.downloaded[1].text(),text);
  assert.equal(page.get('tl-download').disabled,true);
}));

test('saved-render verification explicitly replays canonical bytes on the same renderer', () => withPage(async page => {
  page.ready({default:'default',engines:[{id:'default'},{id:'kept',release_id:'2026-10-25-abcdef0'}]});
  let pending=page.api.verify('hashless draft',{release:'2026-10-25-abcdef0'});await nextTurn();
  const canonical=new TextEncoder().encode('new saved canonical');
  page.workers.at(-1).reply(validResult({canonical,engineId:'kept',rendererRelease:'2026-10-25-abcdef0',play:{match:'-',block_indep:'yes'}}));await pending;
  assert.equal(page.get('verify-saved').disabled,false);
  pending=page.api.verifySaved();await nextTurn();
  assert.equal(page.workers.at(-1).request.recordText,'new saved canonical');
  assert.equal(page.workers.at(-1).request.engine.id,'kept');
  page.workers.at(-1).reply(validResult({canonical}));await pending;
  assert.equal(page.api.result().verified,true);assert.equal(page.get('verify-saved').disabled,true);
  assert.equal(page.audioCreations(),0);
}));

test('a failed hash assertion cannot be resealed by saved-render verification', () => withPage(async page => {
  await complete(page,'mismatched saved song',{verdict:'mismatch',code:3,canonical:new Uint8Array(),stderr:'match=NO'});
  assert.equal(page.get('verify-saved').disabled,true);
  await assert.rejects(page.api.verifySaved(),/valid draft/);
  assert.equal(page.workers.length,1);
}));

test('optional native view failures preserve audio and keep compiled modules private', () => withPage(async page => {
  const module = await WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0]));
  const input = validResult({spatial:{}, spatialModule:module});
  const pcm = input.pcm, canonical = input.canonical;
  const before = Date.now();
  const result = await complete(page, 'spatial fixture', input);
  const deadline = page.workers[0].request.spatialDeadlineMs;
  assert(deadline >= before + limits.renderSeconds*1000 && deadline <= Date.now() + limits.renderSeconds*1000);
  assert.equal(result.verdict, 'valid');
  assert.equal(result.pcm, pcm); assert.equal(result.canonical, canonical);
  assert.equal(Object.hasOwn(result, 'spatialModule'), false);
  assert.equal(Object.hasOwn(page.api.result(), 'spatialModule'), false);
  assert.equal(Object.hasOwn(page.pageEvents.at(-1).detail, 'spatialModule'), false);
  const visual = page.visualCalls.filter(row => row[0] === 'result').at(-1)[2];
  assert.equal(visual.spatialSampler, null);
  assert.match(visual.spatialWarning, /metadata is invalid/);
  assert.equal(page.get('play').disabled, false);
  assert.equal(page.get('wav-download').disabled, false);
}));

test('clear removes player text and result and announces invalidation', () => withPage(async page => {
  await complete(page);
  page.api.clear();
  assert.equal(page.api.result(), null); assert.equal(page.get('record').value, '');
  assert.equal(page.pageEvents.at(-1).type, 'intfm-invalidate');
  assert.equal(page.pageEvents.at(-1).detail, null);
  invalidActions(page);
}));

test('clearing during portable-link preparation cannot show, copy or download the old song', () => withPage(async page => {
  await complete(page);
  const qr=page.get('show-qr').onclick(), copy=page.get('share').onclick(), save=page.get('link-download').onclick();
  page.api.clear();
  await Promise.all([qr,copy,save]);
  assert.equal(page.downloaded.length,0);
  assert.equal(page.api.result() ?? null,null);
  assert.equal(page.copied.length,0);
  assert.equal(page.get('status').textContent,'Record cleared.');
  assert.equal(page.get('show-qr').disabled,true);
}));

test('opening a file prepares one render silently and enables Play on completion', () => withPage(async page => {
  await complete(page);
  const file = deferred();
  page.get('library-panel').open = true;
  page.get('file').value = 'selected file';
  page.get('file').files = [{size: 20, name: 'new.tl', text: () => file.promise}];
  const loading = page.get('file').onchange();
  assert.equal(page.get('file').value, '', 'Allow reimporting the same file after an edit');
  assert.equal(page.get('library-panel').open, true, 'Loading preserves the expanded chooser');
  assert.equal(document.activeElement, page.get('player-heading'));
  invalidActions(page);
  file.resolve('new file fixture');
  const result = await prepared(page, loading, 'new file fixture');
  assert.equal(page.get('record').value, 'new file fixture');
  assert.equal(result.verified, false); assert.equal(result.play.block_indep, 'unchecked');
  assert.equal(page.get('play').disabled, false);
  assert.equal(page.audioCreations(), 0);
  await page.get('play').click();
  assert.deepEqual(page.audioEvents, ['construct', 'resume', 'start']);
}));

const songCatalog = {version: 1, songs: [
  {id: 'first', title: '<First song>', collection: 'Electronic', revision: 'Later take', variants: [
    {kind: 'keys', path: 'songs/first-keys.tl', bytes: 22, duration_seconds: 42},
    {kind: 'standard', path: 'songs/first.tl', bytes: 20, duration_seconds: 40}]},
  {id: 'second', title: 'Second song', collection: 'Acoustic', revision: 'Final', variants: [
    {kind: 'standard', path: 'songs/second.tl', bytes: 24, duration_seconds: 50}]},
]};
test('standalone UI keeps service controls and requests outside the local instrument',async () => {
  const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  for(const name of ['publication-panel','publish-open','shared-open','operator-panel','deployment.mjs'])
    assert.ok(!html.includes(name));
  await withPage(async page=>{
    assert.ok(page.requests.every(request=>!String(request).includes('/api/')));
    assert.ok(!Object.hasOwn(window.IntFM.describe().references,'publishing'));
  });
});

const curatedCatalog = () => {
  const catalog = structuredClone(songCatalog);
  const first = catalog.songs[0].variants[1], second = catalog.songs[1].variants[0];
  first.record_sha256 = 'a'.repeat(64);
  first.discovery = {record_sha256:first.record_sha256, source:'music/first/score.tl',
    metadata:{genre:{value:'ambient',source:'retained-score'}, tempo:{min_bpm:110,max_bpm:174,source:'retained-score'}},
    featured:{order:2,note:'A selected changing-tempo arrangement.'},showcase:{order:1,note:'A sourced tempo range.'}};
  second.record_sha256 = 'b'.repeat(64);
  second.discovery = {record_sha256:second.record_sha256,source:'music/second/score.tl',
    metadata:{tempo:{min_bpm:90,max_bpm:90,source:'retained-score'}},
    featured:{order:1,note:'Start here.'}, provenance:{model:'declared-model',model_source:'user-stated'}};
  return catalog;
};
test('Favorites retain editorial order and prefer Keys without inheriting Original metadata', () => withPage(async page => {
  assert.deepEqual(songRows(page).map(({radio}) => radio.value), ['songs/second.tl','songs/first.tl']);
  assert.equal(selectedSongId(page), 'songs/second.tl');
  assert.equal(page.get('song-variant').value, 'standard');
  assert.match(page.get('song-discovery').textContent, /Start here.*Genre: Unknown.*90 BPM.*declared-model \(user-stated\)/);
  selectSong(page,'songs/first.tl');
  assert.equal(selectedSongId(page), 'songs/first.tl');
  assert.equal(page.get('song-variant').value, 'keys');
  assert.match(page.get('song-discovery').textContent, /Genre: Unknown\. Tempo: Unknown\. Model: Unknown/);
  assert.equal(songRows(page)[1].text.textContent, '<First song> · 0:42');
  page.get('song-variant').value = 'standard'; page.get('song-variant').onchange();
  assert.match(page.get('song-discovery').textContent, /A selected changing-tempo arrangement.*110–174 BPM range.*Model: Unknown/);
  await page.get('library-all').click();
  assert.equal(page.get('song-variant').value, 'standard', 'Same-song explicit version is preserved');
  selectSong(page,'songs/second.tl');
  selectSong(page,'songs/first.tl');
  assert.equal(page.get('song-variant').value, 'keys');
  assert.match(page.get('song-discovery').textContent, /Genre: Unknown\. Tempo: Unknown\. Model: Unknown/);
  assert.equal(page.workers.length, 0);
}, {catalog:curatedCatalog()}));

test('contributed made_by provenance loads with or without a model; malformed provenance is refused', async () => {
  // tunes/tools/check_pr.py writes made_by for every contribution and adds the model pair when one was declared.
  const contributed = provenance => {
    const catalog = curatedCatalog();
    catalog.songs[0].variants[1].discovery.provenance = {made_by:'human'};
    catalog.songs[1].variants[0].discovery.provenance = provenance;
    return catalog;
  };
  await withPage(async page => {
    assert.doesNotMatch(page.get('library-status').textContent, /unavailable/);
    assert.match(page.get('song-discovery').textContent, /Start here.*declared-model \(user-stated\)/);
    selectSong(page,'songs/first.tl');
    page.get('song-variant').value = 'standard'; page.get('song-variant').onchange();
    assert.match(page.get('song-discovery').textContent, /A selected changing-tempo arrangement.*Model: Unknown/);
  }, {catalog:contributed({made_by:'agent',model:'declared-model',model_source:'user-stated'})});
  for (const provenance of [{}, {made_by:'robot'}, {made_by:'human',model:'declared-model'}, {made_by:'human',prompt:'extra'}])
    await withPage(async page => assert.match(page.get('library-status').textContent, /unavailable/), {catalog:contributed(provenance)});
});

test('metadata filters select matching variants with Unknown and tempo ranges kept distinct', () => withPage(async page => {
  await page.get('library-all').click();
  page.get('song-tempo').value = 'range'; await page.get('song-tempo').onchange();
  assert.equal(selectedSongId(page), 'songs/first.tl');
  assert.equal(page.get('song-variant').value, 'standard');
  page.get('song-tempo').value = 'unknown'; await page.get('song-tempo').onchange();
  assert.equal(page.get('song-variant').value, 'keys');
  page.get('song-tempo').value = ''; await page.get('song-tempo').onchange();
  page.get('song-genre').value = 'value:ambient'; await page.get('song-genre').onchange();
  assert.equal(page.get('song-variant').value, 'standard');
  page.get('song-genre').value = ''; await page.get('song-genre').onchange();
  page.get('song-model').value = 'value:declared-model'; await page.get('song-model').onchange();
  assert.equal(selectedSongId(page), 'songs/second.tl');
  page.get('song-model').value = ''; await page.get('song-model').onchange();
  page.get('song-collection').value = 'Electronic'; await page.get('song-collection').onchange();
  assert.equal(selectedSongId(page), 'songs/first.tl');
  assert.match(page.get('library-status').textContent, /1 match in this source selection/);
}, {catalog:curatedCatalog()}));

test('global filters and search leave Favorites while explicit Favorites and clearing preserve their scope', async () => {
  const catalog=curatedCatalog(),outside=catalog.songs[1];
  delete outside.variants[0].discovery.featured;
  outside.variants[0].discovery.metadata.genre={value:'folk',source:'creator-declared'};
  outside.variants.push({kind:'keys',path:'songs/second-keys.tl',bytes:24,duration_seconds:50});
  for(const [field,value] of [['model','value:declared-model'],['collection','Acoustic'],['genre','value:folk'],['tempo','constant'],['search','Second']]) {
    await withPage(async page=>{
      assert.equal(page.get('library-featured').getAttribute('aria-pressed'),'true');
      assert.equal(selectedSongId(page),'songs/first.tl');
      await complete(page,'kept loaded song',transportResult());await page.get('play').click();
      const loaded=page.api.result(),source=page.audioSources.at(-1),workers=page.workers.length;
      page.get('song-'+field).value=value;
      if(field==='search') {page.get('song-search').dispatch('input');await nextTurn();}
      else await page.get('song-'+field).onchange();
      assert.equal(page.get('library-all').getAttribute('aria-pressed'),'true');
      assert.equal(page.get('library-featured').getAttribute('aria-pressed'),'false');
      assert.match(page.get('library-path').textContent,/Source library › Browse all/);
      assert.equal(selectedSongId(page),'songs/second.tl');
      if(field==='model') {
        assert.equal(page.get('song-variant').value,'standard');
        assert.deepEqual(page.get('song-variant').children.map(option=>option.value),['standard'],'Original attribution never spreads to Keys');
      }
      await page.get('library-featured').click();
      assert.equal(page.get('song-'+field).value,value,'Explicit Favorites retains the criterion');
      assert.equal(page.get('song-load').disabled,true);
      assert.match(page.get('selected-song-title').textContent,/No matching favorites/);
      assert.match(page.get('song-detail').textContent,/Browse all.*same filters/);
      page.get('song-'+field).value='';
      if(field==='search') {page.get('song-search').dispatch('input');await nextTurn();}
      else await page.get('song-'+field).onchange();
      assert.equal(page.get('library-featured').getAttribute('aria-pressed'),'true','Clearing one criterion retains Favorites');
      assert.equal(selectedSongId(page),'songs/first.tl');
      await page.get('library-clear').click();
      assert.equal(page.get('library-featured').getAttribute('aria-pressed'),'true');
      assert.equal(selectedSongId(page),'songs/first.tl');
      await page.get('library-all').click();await page.get('library-clear').click();
      assert.equal(page.get('library-all').getAttribute('aria-pressed'),'true');
      assert.equal(page.api.result(),loaded);assert.equal(source.stopped,false);
      assert.equal(page.get('play').textContent,'Pause');assert.equal(page.workers.length,workers);
    },{catalog});
  }
});

test('old source catalogs keep Unknown labels available for filtering', () => withPage(async page => {
  assert.equal(page.get('library-all').getAttribute('aria-pressed'), 'true');
  for (const field of ['genre','tempo','model']) assert.equal(page.get('song-' + field + '-field').hidden, false);
  assert.match(page.get('song-discovery').textContent, /Genre: Unknown.*Tempo: Unknown.*Model: Unknown/);
}, {catalog:songCatalog}));

test('discovery cannot attach labels to a different canonical variant', async () => {
  const catalog = curatedCatalog();
  catalog.songs[0].variants[1].discovery.record_sha256 = 'c'.repeat(64);
  await withPage(async page => {
    assert.match(page.get('library-status').textContent, /unavailable/);
    assert.equal(page.workers.length, 0);
  }, {catalog});
});

test('source discovery refuses unsupported labels, unbounded tempo and invalid native facts', async () => {
  for (const mutate of [
    variant => {variant.discovery.quality_score = 99;},
    variant => {variant.discovery.metadata.genre.value = '<invented>';},
    variant => {variant.discovery.metadata.tempo.max_bpm = Number.MAX_SAFE_INTEGER + 1;},
    variant => {variant.discovery.featured.order = 0;},
    variant => {variant.technical = {source:'native-inspect',route:99,channels:2,events:1,allocated_voices:1};},
  ]) {
    const catalog = curatedCatalog(); mutate(catalog.songs[0].variants[1]);
    await withPage(async page => assert.match(page.get('library-status').textContent, /unavailable/), {catalog});
  }
});

test('loading a catalog variant checks the declared canonical SHA before enabling playback', () => withPage(async page => {
  page.ready();
  const pending = page.get('song-load').click(); await nextTurn();
  page.workers[0].reply(validResult({recordSha256:'c'.repeat(64)})); await pending;
  assert.match(page.api.result().verdict, /mismatch/);
  assert.equal(page.get('play').disabled, true); assert.equal(page.get('tl-download').disabled, true);
}, {catalog:curatedCatalog(),network:{'/web/songs/second.tl':'different canonical fixture'}}));

test('song catalog defaults to Keys when available and otherwise Standard', () => withPage(async page => {
  assert.equal(selectedSongId(page), 'songs/first.tl');
  assert.equal(page.get('song-variant').value, 'keys');
  assert.equal(songRows(page)[0].text.textContent, '<First song> · 0:42');
  assert.equal(songRows(page)[0].text.innerHTML, '');
  assert.match(page.get('library-status').textContent, /1–2 of 2/);
  page.get('song-search').value = 'acoustic'; page.get('song-search').dispatch('input'); await nextTurn();
  assert.equal(selectedSongId(page), 'songs/second.tl');
  assert.equal(page.get('song-variant').value, 'standard');
  assert.match(page.get('library-status').textContent, /1 match in this source selection/);
  page.get('song-search').value = 'no such song'; page.get('song-search').dispatch('input'); await nextTurn();
  assert.equal(page.get('song-load').disabled, true);
  page.get('song-search').value = 'later take'; page.get('song-search').dispatch('input'); await nextTurn();
  assert.equal(selectedSongId(page), 'songs/first.tl');
  assert.equal(page.get('song-variant').value, 'keys');
  page.get('song-variant').value = 'standard'; page.get('song-variant').onchange();
  page.get('song-search').value = 'Electronic'; page.get('song-search').dispatch('input'); await nextTurn();
  assert.equal(page.get('song-variant').value, 'standard', 'Filtering the same song preserves an explicit choice');
  assert.equal(page.requests.filter(url => url.endsWith('/songs.json')).length, 0);
  assert.equal(page.requests.filter(url => url.endsWith('/catalog/index.json')).length, 1);
  assert.equal(page.requests.filter(url => url.endsWith('.tl')).length, 0);
  assert.equal(page.workers.length, 0); assert.equal(page.audioCreations(), 0);
}, {catalog: songCatalog}));

test('duplicate human song labels retain distinct catalog identities', () => {
  const catalog = structuredClone(songCatalog), duplicate = structuredClone(catalog.songs[0]);
  duplicate.id = 'another-take'; for (const v of duplicate.variants) v.path = v.path.replace('first','another-take'); catalog.songs.push(duplicate);
  return withPage(async page => {
    const choices = songRows(page);
    assert.match(choices[0].text.textContent, /\[songs\/first.tl\]$/);
    assert.match(choices[2].text.textContent, /\[songs\/another-take.tl\]$/);
    assert.notEqual(choices[0].radio.value, choices[2].radio.value);
  }, {catalog});
});

test('inline radio selection ignores unchecked, disabled and detached rows without loading audio', async()=>{
  const catalog=curatedCatalog(),documents=catalogPages(catalog),pending=deferred();
  await withPage(async page=>{
    const initial=songRows(page),unchecked=initial[1].radio;
    assert(initial.every(({row,radio,text})=>row.tagName==='label' && radio.type==='radio'
      && radio.name==='song-choice' && text.tagName==='span'));
    assert.equal(initial.filter(({radio})=>radio.checked).length,1);
    unchecked.dispatch('change',{target:unchecked});
    assert.equal(selectedSongId(page),'songs/second.tl');
    const loading=page.get('library-all').click();await nextTurn();
    assert.equal(page.get('song-select').disabled,true);assert.equal(page.get('song-select').getAttribute('aria-busy'),'true');
    unchecked.checked=true;unchecked.dispatch('change',{target:unchecked});unchecked.checked=false;
    assert.equal(page.get('selected-song-title').textContent,'Second song','A disabled group cannot change app selection');
    pending.resolve(new Response(JSON.stringify(documents['catalog/all-1.json'])));await loading;
    assert.equal(page.get('song-select').disabled,false);assert.equal(page.get('song-select').getAttribute('aria-busy'),'false');
    selectSong(page,'songs/first.tl');
    const detached=initial[0].radio;detached.checked=true;detached.dispatch('change',{target:detached});
    assert.equal(selectedSongId(page),'songs/first.tl');assert.equal(page.get('selected-song-title').textContent,'<First song>');
    assert.equal(songRows(page).filter(({radio})=>radio.checked).length,1);
    assert.equal(page.requests.filter(url=>url.endsWith('.tl')).length,0);assert.equal(page.workers.length,0);assert.equal(page.audioCreations(),0);
  },{catalog,network:{'/web/catalog/all-1.json':()=>pending.promise}});
});

test('song version loads and prepares playback without starting audio', () => withPage(async page => {
  await complete(page);
  page.get('song-variant').value = 'keys'; page.get('song-variant').onchange();
  const loading = page.get('song-load').click();
  await prepared(page, loading, 'selected Keys record');
  assert.equal(page.get('record').value, 'selected Keys record');
  assert(page.requests.includes('https://player.invalid/web/songs/first-keys.tl'));
  assert.equal(page.workers.length, 2);
  assert.equal(page.get('play').disabled, false); assert.equal(page.audioCreations(), 0);
}, {catalog: songCatalog, network: {'/web/songs/first-keys.tl': 'selected Keys record'}}));

test('a late song response cannot replace newer text or an opened file', () => {
  let record = deferred();
  return withPage(async page => {
    const first = page.get('song-load').click();
    page.get('record').value = 'user edit'; page.get('record').dispatch('input');
    record.resolve(new Response('old song')); await first;
    assert.equal(page.get('record').value, 'user edit');
    record = deferred();
    const second = page.get('song-load').click();
    page.get('file').files = [{size: 12, name: 'mine.tl', text: async () => 'new file'}];
    await prepared(page, page.get('file').onchange(), 'new file');
    const latestResult = page.api.result(), latestStatus = page.get('status').textContent;
    record.resolve(new Response('old song')); await second;
    assert.equal(page.get('record').value, 'new file');
    assert.equal(page.api.result(), latestResult); assert.equal(page.get('status').textContent, latestStatus);
  }, {catalog: songCatalog, network: {'/web/songs/first-keys.tl': () => record.promise}});
});

test('selecting another song discards a pending prior song response', () => {
  const record = deferred();
  return withPage(async page => {
    const first = page.get('song-load').click();
    selectSong(page,'songs/second.tl');
    await prepared(page, page.get('song-load').click(), 'second record');
    const latestResult = page.api.result(), latestStatus = page.get('status').textContent;
    record.resolve(new Response('old song')); await first;
    assert.equal(page.get('record').value, 'second record');
    assert.equal(page.api.result(), latestResult); assert.equal(page.get('status').textContent, latestStatus);
  }, {catalog: songCatalog, network: {'/web/songs/first-keys.tl': () => record.promise, '/web/songs/second.tl': 'second record'}});
});

test('unavailable song catalog leaves local file loading usable', () => withPage(async page => {
  assert.match(page.get('library-status').textContent, /unavailable/);
  page.get('file').files = [{size: 12, name: 'mine.tl', text: async () => 'local record'}];
  await prepared(page, page.get('file').onchange(), 'local record');
  assert.equal(page.get('record').value, 'local record');
}, {catalogStatus: 404}));

test('song catalog refuses paths outside its own songs folder', async () => {
  for (const path of ['https://outside.invalid/song.tl', 'songs/../private.tl', 'songs/%2e%2e/private.tl', '/songs/first.tl', 'songs/first.tl?url=elsewhere']) {
    const catalog = structuredClone(songCatalog); catalog.songs[0].variants[0].path = path;
    await withPage(async page => {
      assert.match(page.get('library-status').textContent, /unavailable/);
      assert.equal(page.requests.length, 3);
    }, {catalog});
  }
});

function largeCatalog(size) {
  return {version:1,songs:Array.from({length:size},(_,index)=>({id:'song-'+index,title:'Song '+index,collection:'Collection',
    variants:[{kind:'standard',path:`songs/song-${index}.tl`,bytes:25,duration_seconds:30}]}))};
}
test('bounded browsing shows eight results, retains the loaded identity and restores Previous', () => withPage(async page => {
  assert.equal(songRows(page).length,8);
  assert.match(page.get('library-status').textContent,/1–8 of 70/);
  await prepared(page,page.get('song-load').click(),'song zero');
  const result=page.api.result(); await page.get('play').click();
  const source=page.audioSources.at(-1), count=page.workers.length;
  await page.get('library-next').click();
  assert.equal(selectedSongId(page),'songs/song-8.tl');
  assert.equal(page.get('loaded-song').textContent,'Song 0');
  assert.equal(page.get('mobile-song').textContent,'Song 0');
  assert.equal(page.get('mobile-play').textContent,'Pause');
  assert.equal(page.api.result(),result); assert.equal(source.stopped,false); assert.equal(page.workers.length,count);
  await page.get('library-previous').click();
  assert.equal(selectedSongId(page),'songs/song-0.tl');
  assert.equal(page.requests.filter(url=>url.endsWith('/catalog/all-1.json')).length,1,'Previous reuses bounded cached pages');
  page.get('tl-download').click(); assert.equal(page.filenames.at(-1),'Song 0-standard.tl');
}, {catalog:largeCatalog(70),network:{'/web/songs/song-0.tl':'song zero'}}));

test('both song jog rails move whole result pages without changing the prepared song', () => withPage(async page => {
  await complete(page,'kept song',transportResult());await page.get('play').click();
  const result=page.api.result(), source=page.audioSources.at(-1), renders=page.workers.length;
  assert.equal(page.get('song-step-up-left').disabled,true);assert.equal(page.get('song-step-up-right').disabled,true);
  selectSong(page,'songs/song-3.tl');
  assert.equal(page.get('song-step-up-left').disabled,true,'No previous page, even when a later song is selected');
  await page.get('song-step-down-right').click();assert.equal(selectedSongId(page),'songs/song-8.tl');
  await page.get('song-step-down-left').click();assert.equal(selectedSongId(page),'songs/song-16.tl');
  assert.equal(songRows(page).length,1);
  assert.equal(page.get('song-step-down-left').disabled,true);assert.equal(page.get('song-step-down-right').disabled,true);
  await page.get('song-step-down-right').click();assert.equal(selectedSongId(page),'songs/song-16.tl');
  await page.get('song-step-up-left').click();assert.equal(selectedSongId(page),'songs/song-8.tl');
  await page.get('song-step-up-right').click();assert.equal(selectedSongId(page),'songs/song-0.tl');
  assert.equal(page.requests.filter(url=>url.endsWith('/catalog/all-1.json')).length,1);
  assert.equal(page.api.result(),result);assert.equal(source.stopped,false);assert.equal(page.workers.length,renders);
  assert.equal(page.requests.filter(url=>url.endsWith('.tl')).length,0);
}, {catalog:largeCatalog(17)}));

test('native song keys stay native while both dial wheels page without burst requests', () => withPage(async page => {
  let prevented=0;
  const key=key=>({key,preventDefault(){prevented++;}});
  page.get('song-select').dispatch('keydown',key('ArrowDown'));assert.equal(prevented,0,'Native movement inside the list is untouched');
  selectSong(page,'songs/song-7.tl');
  page.get('song-select').dispatch('keydown',key('ArrowDown'));await nextTurn();
  assert.equal(prevented,0);assert.equal(selectedSongId(page),'songs/song-7.tl','The native list boundary does not change pages');
  selectSong(page,'songs/song-0.tl');
  page.get('song-select').dispatch('keydown',key('ArrowUp'));await nextTurn();
  assert.equal(prevented,0);assert.equal(selectedSongId(page),'songs/song-0.tl');
  const wheel={deltaY:80,preventDefault(){prevented++;}};
  page.get('song-jog-left').dispatch('wheel',wheel);await nextTurn();
  page.get('song-jog-right').dispatch('wheel',wheel);await nextTurn();
  assert.equal(selectedSongId(page),'songs/song-8.tl','A wheel burst advances one page across both rails');
  page.expire(120);page.get('song-jog-right').dispatch('wheel',wheel);await nextTurn();
  assert.equal(selectedSongId(page),'songs/song-16.tl');
  const beforeZoom=prevented;
  page.get('song-jog-right').dispatch('wheel',{...wheel,ctrlKey:true});assert.equal(prevented,beforeZoom,'Zoom stays native');
  page.expire(120);page.get('song-jog-right').dispatch('wheel',wheel);await nextTurn();
  assert.equal(selectedSongId(page),'songs/song-16.tl');assert.equal(prevented,beforeZoom+1,'The end boundary consumes dial scrolling without advancing');
  assert.equal(page.get('song-step-down-left').disabled,true);assert.equal(page.get('song-step-down-right').disabled,true);
  page.get('song-jog-left').dispatch('wheel',{...wheel,deltaY:-80});await nextTurn();
  assert.equal(selectedSongId(page),'songs/song-8.tl');
  assert.equal(page.requests.filter(url=>url.endsWith('/catalog/all-1.json')).length,1);
  assert.equal(page.workers.length,0);assert.equal(page.audioCreations(),0);
}, {catalog:largeCatalog(17)}));

test('a late jog page cannot override a new search or strand its controls', async () => {
  const catalog=largeCatalog(40),documents=catalogPages(catalog),pending=deferred();let signal;
  await withPage(async page=>{
    await page.get('library-next').click();await page.get('library-next').click();
    selectSong(page,'songs/song-23.tl');
    const old=page.get('song-step-down-right').click();await nextTurn();
    assert.equal(page.get('song-step-down-left').disabled,true);
    assert.equal(page.get('song-step-up-left').disabled,true);assert.equal(page.get('library-next').disabled,true);
    await page.get('song-step-down-left').click();await page.get('song-step-up-right').click();
    assert.equal(page.requests.filter(url=>url.endsWith('/catalog/all-2.json')).length,1,'Busy rails cannot duplicate or replace the pending page request');
    page.get('song-search').value='Song 1';page.get('song-search').dispatch('input');await nextTurn();
    assert.equal(signal.aborted,true);assert.equal(selectedSongId(page),'songs/song-1.tl');
    assert.equal(page.get('song-step-down-right').disabled,false);
    pending.resolve(new Response(JSON.stringify(documents['catalog/all-2.json'])));await old;
    assert.equal(selectedSongId(page),'songs/song-1.tl');assert.equal(page.workers.length,0);
  },{catalog,network:{'/web/catalog/all-2.json':options=>{signal=options.signal;return pending.promise;}}});
});

test('a failed jog fetch keeps an honest error and Clear filters restores usable controls', async () => {
  await withPage(async page=>{
    await page.get('library-next').click();await page.get('library-next').click();
    selectSong(page,'songs/song-23.tl');
    await page.get('song-step-down-left').click();
    assert.match(page.get('library-status').textContent,/unavailable/);
    assert.equal(page.get('song-step-down-left').disabled,true);assert.equal(page.get('song-load-right').disabled,true);
    await page.get('library-clear').click();
    assert.equal(page.get('song-step-down-right').disabled,false);assert.equal(page.get('song-load-right').disabled,false);
  },{catalog:largeCatalog(40),network:{'/web/catalog/all-2.json':()=>new Response('unavailable',{status:503})}});
});

test('browsing past a loaded library song preserves its exact compact sharing identity', async () => {
  const catalog=largeCatalog(10);catalog.songs[0].variants[0].record_sha256='a'.repeat(64);
  await withPage(async page=>{
    await prepared(page,page.get('song-load').click(),'song zero');
    await page.get('library-next').click();await page.get('share').click();
    assert.match(page.copied.at(-1),/#song=songs%2Fsong-0.tl&sha=a{64}&catalog=catalog%2Fall-1.json$/);
    assert.equal(page.get('loaded-song').textContent,'Song 0');
  },{catalog,network:{'/web/songs/song-0.tl':'song zero'}});
});

test('sparse Unicode search continues bounded chunks until it finds an existing song', async () => {
  const catalog=largeCatalog(110);catalog.songs[105].title='夜の旅';
  await withPage(async page=>{
    page.get('song-search').value='夜の旅';page.get('song-search').dispatch('input');await nextTurn();
    assert.equal(page.get('song-load').disabled,false);
    assert.equal(page.get('selected-song-title').textContent,'夜の旅');
    assert.match(page.get('library-status').textContent,/1 match in this source selection/);
    assert.equal(page.requests.filter(url=>/catalog\/all-\d+.json$/.test(url)).length,5);
    assert.equal(page.get('library-next').disabled,true);assert.equal(page.workers.length,0);
    assert.equal(page.get('library-previous').disabled,true,'Empty scan chunks are not false result pages');
    await page.get('library-clear').click();
    assert.match(page.get('library-path').textContent,/Source library › Browse all$/);
  },{catalog});
});

test('a stale catalog fetch is aborted and cannot replace a later cached selection', async () => {
  const catalog=curatedCatalog(),documents=catalogPages(catalog),pending=deferred();let signal;
  await withPage(async page=>{
    const old=page.get('library-all').click();await nextTurn();
    await page.get('library-featured').click();
    assert.equal(signal.aborted,true);
    pending.resolve(new Response(JSON.stringify(documents['catalog/all-1.json'])));await old;
    assert.equal(selectedSongId(page),'songs/second.tl');
    assert.equal(page.get('library-featured').getAttribute('aria-pressed'),'true');
    page.get('song-search').value='First';page.get('song-search').dispatch('input');
    page.get('song-search').value='Second';page.get('song-search').dispatch('input');await nextTurn();
    assert.equal(selectedSongId(page),'songs/second.tl');
  },{catalog,network:{'/web/catalog/all-1.json':options=>{signal=options.signal;return pending.promise;}}});
});

test('a failed bounded page has an explicit local retry and never requests the full manifest', async () => {
  const catalog=largeCatalog(5),documents=catalogPages(catalog);let calls=0;
  await withPage(async page=>{
    assert.match(page.get('library-status').textContent,/unavailable/);assert.equal(page.get('song-load').disabled,true);
    await page.get('library-clear').click();
    assert.equal(songRows(page).length,5);assert.equal(page.get('song-load').disabled,false);
    assert.equal(calls,2);assert(!page.requests.some(url=>url.endsWith('/songs.json')));
  },{catalog,network:{'/web/catalog/all-1.json':()=>new Response(JSON.stringify(documents['catalog/all-1.json']),{status:++calls===1?503:200})}});
});

test('collection statistics retain unknowns and expose bounded declaration and duration counts without audio', async () => {
  const summary={version:1,scope:'source-library',selection:'standard',as_of:{sha256:'a'.repeat(64)},
    compositions:5,canonical_versions:12,duration_seconds:{known:5,denominator:5,total:80,mean:16,median:10,
      distribution:[{min_inclusive:0,max_exclusive:30,count:5}]},declarations:{
      genre:{known:4,unknown:1,denominator:5,breakdown:{items:[{value:'ambient',count:2}],omitted_labels:2,omitted_count:2}},
      tempo:{known:3,unknown:2,denominator:5,breakdown:{items:[{min_bpm:125,max_bpm:125,count:3}],omitted_labels:0}},
      model:{known:0,unknown:5,denominator:5,breakdown:{items:[],omitted_labels:0}}}};
  const documents=catalogPages(largeCatalog(summary.compositions));
  documents['catalog/index.json'].summary='catalog/summary.json';
  documents['catalog/summary.json']=summary;
  await withPage(async page=>{
    assert.equal(page.requests.some(url=>url.endsWith('/catalog/summary.json')),false);
    page.get('library-research').open=true;page.get('library-research').dispatch('toggle');await nextTurn();
    assert.match(page.get('library-statistics').textContent,/5 source compositions.*12 saved versions/);
    assert.match(page.get('library-duration-distribution').textContent,/0–<30 s: 5/);
    assert.match(page.get('library-declaration-counts').textContent,/2 further labels cover 2 songs/);
    assert.match(page.get('library-declaration-counts').textContent,/125 BPM: 3/);
    assert.match(page.get('library-declaration-counts').textContent,/model: no known declarations \(5 unknown\)/);
    assert.equal(page.workers.length,0);assert.equal(page.audioCreations(),0);
    page.get('library-research').dispatch('toggle');await nextTurn();
    assert.equal(page.requests.filter(url=>url.endsWith('/catalog/summary.json')).length,1);
  },{catalogDocuments:documents});
  const empty=structuredClone(summary);
  empty.compositions=empty.canonical_versions=0;
  empty.duration_seconds={known:0,denominator:0,total:0,mean:null,median:null,distribution:[]};
  for(const key of ['genre','tempo','model']) empty.declarations[key]={known:0,unknown:0,denominator:0,breakdown:{items:[]}};
  const emptyDocuments=catalogPages(largeCatalog(0));
  emptyDocuments['catalog/index.json'].summary='catalog/summary.json';emptyDocuments['catalog/summary.json']=empty;
  await withPage(async page=>{
    page.get('library-research').open=true;page.get('library-research').dispatch('toggle');await nextTurn();
    assert.match(page.get('library-statistics').textContent,/0 source compositions.*total 0.0 minutes, mean unavailable, median unavailable/);
    assert.equal(page.get('library-statistics').dataset.loaded,'true');
    assert.equal(page.get('song-load').disabled,true);assert.equal(page.workers.length,0);
  },{catalogDocuments:emptyDocuments});
});

const inspectedNotes = (extra={}) => ({type:'inspection',verdict:'valid',info:{fs:'48000',frames:'48000',events:'2',voices:'1',length_ns:'1000000000',route:'0',channels:'2'},
  events:[{kind:'on',voice:'0',time_ns:'0',freq_millihz:'440000',velocity:'12000',stream:'<lead>'},
    {kind:'off',voice:'0',time_ns:'500000000'}],eventsError:null,...extra});
test('selected native note inspection cannot render, replace or pause the loaded audio', () => withPage(async page=>{
  await complete(page,'playing source',transportResult());await page.get('play').click();
  const result=page.api.result(),source=page.audioSources.at(-1),renders=page.workers.length,audio=page.audioCreations();
  await page.get('song-preview').click();
  const worker=page.workers.at(-1);assert.equal(worker.request.operation,'inspect');assert.equal(worker.request.mode,undefined);
  worker.reply(inspectedNotes());
  assert.equal(page.api.result(),result);assert.equal(source.stopped,false);assert.equal(page.audioCreations(),audio);
  assert.equal(page.workers.length,renders+1);assert.equal(page.get('song-preview-roll').hidden,false);
  assert.match(page.get('song-preview-status').textContent,/First song.*Keys.*1 visible gate segment.*no audio was rendered or verified/);
  assert.match(page.get('song-preview-roll').innerHTML,/&lt;lead&gt;/);
  assert.match(page.get('song-preview-roll').innerHTML,/Selected song structure.*1 note attack.*No open gates: 0.500 s/s);
  assert.equal(page.get('record').value,'playing source');
}, {catalog:songCatalog,network:{'/web/songs/first-keys.tl':'selected notes'}}));

test('loaded song structure reuses native events and stays independent of inspected selection', () => withPage(async page=>{
  const native=inspectedNotes(), result=await complete(page,'loaded record',validResult({info:native.info,events:native.events}));
  assert.equal(result.structure.status,'available');assert.equal(result.structure.note_attacks,1);
  assert.equal(result.structure.no_gate_seconds,.5);
  const text=page.get('song-structure').textContent,workers=page.workers.length;
  assert.match(text,/1 note attack.*0.500 s/);assert.equal(page.audioCreations(),0);
  selectSong(page,'songs/second.tl');
  await page.get('song-preview').click();page.workers.at(-1).reply(inspectedNotes({events:[],info:{...native.info,events:'0'}}));
  assert.match(page.get('song-preview-roll').innerHTML,/Selected song structure.*0 note attacks/s);
  assert.equal(page.get('song-structure').textContent,text);assert.equal(page.api.result(),result);
  assert.equal(page.workers.length,workers+1,'Only the requested native inspection starts a worker');
  page.api.clear();assert.match(page.get('song-structure').textContent,/Load a song/);
  const missing=await complete(page,'missing events',validResult({info:native.info,eventsError:'listing failed'}));
  assert.equal(missing.structure.status,'unavailable');assert.equal(page.get('play').disabled,false);
  assert.match(page.get('song-structure').textContent,/unavailable/);
}, {catalog:songCatalog,network:{'/web/songs/second.tl':'selected notes'}}));

test('Inspect notes toggles to Hide notes and can reopen without disturbing playback', () => withPage(async page=>{
  await complete(page,'playing source',transportResult());await page.get('play').click();
  const result=page.api.result(),source=page.audioSources.at(-1),audio=page.audioCreations();
  await page.get('song-preview').click();page.workers.at(-1).reply(inspectedNotes());
  assert.equal(page.get('song-preview').textContent,'Hide notes');assert.equal(page.get('song-preview').getAttribute('aria-expanded'),'true');
  await page.get('song-preview').click();
  assert.equal(page.get('song-preview-roll').hidden,true);assert.equal(page.get('song-preview-roll').children.length,0);
  assert.equal(page.get('song-preview').textContent,'Inspect notes');assert.equal(page.get('song-preview').getAttribute('aria-expanded'),'false');
  assert.match(page.get('song-preview-status').textContent,/hidden/);
  await page.get('song-preview').click();page.workers.at(-1).reply(inspectedNotes());
  assert.equal(page.get('song-preview-roll').hidden,false);
  selectSong(page,'songs/second.tl');
  assert.equal(page.get('song-preview').textContent,'Inspect notes');assert.equal(page.get('song-preview').getAttribute('aria-expanded'),'false');
  assert.equal(page.api.result(),result);assert.equal(source.stopped,false);assert.equal(page.audioCreations(),audio);
}, {catalog:songCatalog,network:{'/web/songs/first-keys.tl':'selected notes'}}));

test('changing selection cancels note inspection and ignores a late result', () => withPage(async page=>{
  page.ready();await page.get('song-preview').click();const worker=page.workers.at(-1);
  selectSong(page,'songs/second.tl');
  assert(worker.terminated);worker.onmessage({data:inspectedNotes()});
  assert.equal(page.get('song-preview-roll').hidden,true);assert.equal(page.get('selected-song-title').textContent,'Second song');
  assert.equal(page.audioCreations(),0);assert.equal(page.api.result() ?? null,null);
}, {catalog:songCatalog,network:{'/web/songs/first-keys.tl':'selected notes'}}));

test('stale note fetches abort before worker creation and unavailable engines leave playback intact', async () => {
  const pending=deferred();let signal;
  await withPage(async page=>{
    page.ready();const preview=page.get('song-preview').click();await nextTurn();
    selectSong(page,'songs/second.tl');assert.equal(signal.aborted,true);
    pending.resolve(new Response('old notes'));await preview;assert.equal(page.workers.length,0);
  },{catalog:songCatalog,network:{'/web/songs/first-keys.tl':options=>{signal=options.signal;return pending.promise;}}});
  await withPage(async page=>{
    page.ready({default:'missing',engines:[]});await page.get('song-preview').click();
    assert.match(page.get('song-preview-status').textContent,/unavailable.*Playback is unchanged/);
    assert.equal(page.audioCreations(),0);assert.equal(page.workers.length,0);
  },{catalog:songCatalog,network:{'/web/songs/first-keys.tl':'notes'}});
});

test('first-use mobile Load prepares silently while the separate Play button needs prepared audio', async()=>{
  const record=deferred();
  await withPage(async page=>{
    assert.equal(page.get('choose-song').dataset.action,'load');
    assert.equal(page.get('choose-song').textContent,'Load song');assert.equal(page.get('choose-song').disabled,false);
    assert.equal(page.get('mobile-play').textContent,'Play');assert.equal(page.get('mobile-play').disabled,true);
    assert.equal(page.get('mobile-song').textContent,'<First song>');
    assert.match(page.get('mobile-version').textContent,/^Selected · Keys · saved ±30°$/);
    assert.equal(page.get('loaded-song').textContent,'No song loaded');
    page.get('mobile-play').click();assert.equal(page.requests.filter(url=>url.endsWith('.tl')).length,0);
    const pending=page.get('choose-song').click();await nextTurn();
    assert.equal(page.get('choose-song').textContent,'Loading…');assert.equal(page.get('choose-song').disabled,true);
    assert.equal(page.get('mobile-play').disabled,true);
    assert.equal(page.audioCreations(),0);assert.equal(page.workers.length,0);
    record.resolve(new Response('selected Keys record'));
    await prepared(page,pending,'selected Keys record');
    assert.equal(page.get('choose-song').textContent,'Load song');assert.equal(page.get('choose-song').disabled,false);
    assert.equal(page.get('mobile-play').textContent,'Play');assert.equal(page.get('mobile-play').disabled,false);
    assert.equal(page.get('loaded-song').textContent,'<First song>');
    assert.equal(page.get('mobile-version').textContent,'Loaded · Keys · saved ±30°');
    assert.equal(page.audioCreations(),0,'Loading never starts audio');
    await page.get('mobile-play').click();assert.equal(page.audioCreations(),1);
    assert.equal(page.get('mobile-play').textContent,'Pause');
  },{autoMove:false,catalog:songCatalog,network:{'/web/songs/first-keys.tl':()=>record.promise}});
});

test('Sections Load prepares the selected version silently and Play keeps the loaded identity while browsing', async()=>{
  const download=deferred();
  await withPage(async page=>{
    page.setSection('source-panel');
    assert.equal(page.get('rail-selected-song').textContent,'<First song>');
    assert.match(page.get('rail-selected-version').textContent,/Keys · saved ±30°/);
    assert.equal(page.get('rail-load').disabled,false);assert.equal(page.get('rail-play').disabled,true);
    page.get('rail-play').click();assert.equal(page.requests.filter(url=>url.endsWith('.tl')).length,0);
    const loading=page.get('rail-load').click();await nextTurn();
    assert.equal(page.get('rail-load').disabled,true);assert.equal(page.get('rail-play').disabled,true);
    assert.match(page.get('rail-status').textContent,/Loading/i);
    assert.equal(page.workers.length,0);assert.equal(page.audioCreations(),0);
    download.resolve(new Response('selected Keys record'));
    await prepared(page,loading,'selected Keys record',transportResult());
    assert.equal(page.get('rail-load').disabled,false);assert.equal(page.get('rail-play').disabled,false);
    assert.match(page.get('rail-loaded-song').textContent,/Loaded: <First song>.*Keys/);
    assert.equal(page.audioCreations(),0);
    const result=page.api.result(),requests=page.requests.length;
    await page.get('rail-play').click();page.advance(2);
    assert.equal(page.get('rail-play').textContent,'Pause');assert.equal(page.get('mobile-play').textContent,'Pause');
    assert.match(page.get('rail-status').textContent,/Playing/);
    assert.equal(page.get('rail-time').value,'0:02 / 0:10');
    selectSong(page,'songs/second.tl');
    assert.equal(page.get('rail-selected-song').textContent,'Second song');
    assert.match(page.get('rail-selected-version').textContent,/Original/);
    assert.match(page.get('rail-loaded-song').textContent,/Playing: <First song>.*Keys/);
    assert.equal(page.api.result(),result);assert.equal(page.requests.length,requests);
    page.get('rail-play').click();page.advance(0);
    assert.equal(page.get('rail-play').textContent,'Resume');assert.equal(page.get('play').textContent,'Resume');
    assert.match(page.get('rail-status').textContent,/Paused/);
    await page.get('mobile-play').click();page.advance(0);
    assert.equal(page.get('rail-play').textContent,'Pause');
    page.audioSources.at(-1).onended();page.advance(0);
    assert.equal(page.get('rail-play').textContent,'Play');assert.match(page.get('rail-status').textContent,/Finished/);
    assert.equal(page.get('rail-time').value,'0:10 / 0:10');
    assert.equal(page.get('rail-selected-song').textContent,'Second song');
    assert.equal(page.api.result(),result);assert.equal(page.requests.length,requests);assert.equal(page.workers.length,1);
  },{autoMove:false,catalog:songCatalog,network:{'/web/songs/first-keys.tl':()=>download.promise}});
});
test('Sections buttons start their accessible names with their visible text in every state', async()=>{
  const download=deferred();
  await withPage(async page=>{
    page.setSection('source-panel');
    const name=id=>[page.get(id).textContent,page.get(id).getAttribute('aria-label')];
    assert.deepEqual(name('rail-load'),['Load song','Load song: <First song>']);
    assert.deepEqual(name('rail-play'),['Play','Play']);
    const loading=page.get('rail-load').click();await nextTurn();
    assert.deepEqual(name('rail-load'),['Loading…','Loading…']);
    assert.deepEqual(name('rail-play'),['Loading…','Loading…']);
    download.resolve(new Response('selected Keys record'));
    page.ready();await nextTurn();
    assert.deepEqual(name('rail-load'),['Preparing…','Preparing…']);
    assert.deepEqual(name('rail-play'),['Rendering…','Rendering…']);
    page.workers.at(-1).reply(transportResult());await loading;await nextTurn();
    assert.deepEqual(name('rail-play'),['Play','Play <First song>']);
    selectSong(page,'songs/second.tl');
    assert.deepEqual(name('rail-load'),['Load song','Load song: Second song']);
  },{autoMove:false,catalog:songCatalog,network:{'/web/songs/first-keys.tl':()=>download.promise}});
});

test('outside Songs the sticky Choose action returns to the chooser without loading or playing', ()=>withPage(async page=>{
  page.setSection('live-views');
  assert.equal(page.get('choose-song').dataset.action,'choose');assert.equal(page.get('choose-song').textContent,'Choose song');
  page.get('library-panel').open=false;page.get('library-panel').dispatch('toggle');
  const requests=page.requests.length;
  page.get('choose-song').click();
  assert.equal(page.get('library-panel').open,true);assert.equal(document.activeElement,page.get('library-summary'));
  assert.equal(page.get('auto-move').checked,false);assert.equal(page.get('choose-song').dataset.action,'load');
  assert.equal(page.requests.length,requests);assert.equal(page.workers.length,0);assert.equal(page.audioCreations(),0);
  page.get('library-panel').open=false;page.get('library-panel').dispatch('toggle');
  assert.equal(page.get('choose-song').dataset.action,'choose','A closed Songs panel offers navigation, not a hidden selection action');
},{catalog:songCatalog}));

test('sticky Load stays disabled during catalog search and becomes Choose for empty or failed results', async()=>{
  for(const outcome of ['empty','error']) {
    const catalog=curatedCatalog(),documents=catalogPages(catalog),pending=deferred();
    await withPage(async page=>{
      page.get('song-search').value='no such song';
      const loading=page.get('library-all').click();await nextTurn();
      assert.equal(page.get('song-select').disabled,true);assert.equal(page.get('choose-song').dataset.action,'load');
      assert.equal(page.get('choose-song').disabled,true);
      assert.equal(page.get('rail-load').disabled,true);
      page.get('choose-song').click();assert.equal(page.requests.filter(url=>url.endsWith('.tl')).length,0);
      pending.resolve(new Response(JSON.stringify(documents['catalog/all-1.json']),{status:outcome==='error'?503:200}));await loading;
      assert.equal(songRows(page).length,0);assert.equal(selectedSongId(page),'');
      assert.equal(page.get('choose-song').dataset.action,'choose');assert.equal(page.get('choose-song').disabled,false);
      assert.equal(page.get('mobile-play').disabled,true);assert.equal(page.workers.length,0);assert.equal(page.audioCreations(),0);
      assert.equal(page.get('rail-load').disabled,true);assert.equal(page.get('rail-play').disabled,true);
    },{autoMove:false,catalog,network:{'/web/catalog/all-1.json':()=>pending.promise}});
  }
});

test('Auto navigation changes sticky Load to Choose while its silent preparation is pending', async()=>{
  const record=deferred();
  await withPage(async page=>{
    assert.equal(page.get('choose-song').dataset.action,'load');
    const loading=page.get('choose-song').click();await nextTurn();
    assert.deepEqual(page.sectionJumps,['player-panel']);
    assert.equal(page.get('choose-song').dataset.action,'choose');assert.equal(page.get('choose-song').disabled,false);
    assert.equal(page.get('mobile-play').disabled,true);
    page.get('choose-song').click();assert.equal(page.get('auto-move').checked,false);
    assert.equal(page.get('choose-song').dataset.action,'load');assert.equal(page.get('choose-song').disabled,true);
    record.resolve(new Response('selected Keys record'));await prepared(page,loading,'selected Keys record');
    assert.equal(page.get('choose-song').disabled,false);assert.equal(page.audioCreations(),0);
  },{catalog:songCatalog,network:{'/web/songs/first-keys.tl':()=>record.promise}});
});

test('changing selection aborts a pending sticky Load and permits a safe replacement', async()=>{
  const old=deferred();let signal;
  await withPage(async page=>{
    const first=page.get('choose-song').click();await nextTurn();
    assert.equal(page.get('choose-song').disabled,true);assert.equal(page.get('mobile-play').disabled,true);
    selectSong(page,'songs/second.tl');
    assert.equal(signal.aborted,true);assert.equal(page.get('choose-song').disabled,false);
    assert.equal(page.get('choose-song').textContent,'Load song');assert.equal(page.get('mobile-song').textContent,'Second song');
    await prepared(page,page.get('choose-song').click(),'second record');const result=page.api.result();
    old.resolve(new Response('late first record'));await first;
    assert.equal(page.api.result(),result);assert.equal(page.get('record').value,'second record');
    assert.equal(page.get('mobile-song').textContent,'Second song');assert.equal(page.get('mobile-play').textContent,'Play');
    assert.equal(page.workers.length,1);assert.equal(page.audioCreations(),0);
  },{autoMove:false,catalog:songCatalog,network:{'/web/songs/first-keys.tl':options=>{signal=options.signal;return old.promise;},'/web/songs/second.tl':'second record'}});
});

test('returning to the same selected version cannot revive its cancelled pending download', async()=>{
  const old=deferred();
  await withPage(async page=>{
    const first=page.get('choose-song').click();await nextTurn();
    page.get('song-variant').value='standard';page.get('song-variant').onchange();
    assert.equal(page.get('choose-song').disabled,false);assert.match(page.get('mobile-version').textContent,/Selected · Original/);
    page.get('song-variant').value='keys';page.get('song-variant').onchange();
    old.resolve(new Response('obsolete record'));await first;
    assert.equal(page.workers.length,0);assert.equal(page.get('choose-song').textContent,'Load song');
    assert.equal(page.get('choose-song').disabled,false);assert.equal(page.get('mobile-play').disabled,true);
    assert.equal(page.get('loaded-song').textContent,'No song loaded');assert.equal(page.audioCreations(),0);
  },{autoMove:false,catalog:songCatalog,network:{'/web/songs/first-keys.tl':()=>old.promise}});
});

test('browsing another radio preserves the prepared Play target and offers a separate sticky Load', ()=>withPage(async page=>{
  await prepared(page,page.get('choose-song').click(),'selected Keys record');
  const result=page.api.result(),requests=page.requests.length,workers=page.workers.length;
  selectSong(page,'songs/second.tl');
  assert.equal(page.get('selected-song-title').textContent,'Second song');
  assert.equal(page.get('mobile-song').textContent,'<First song>');
  assert.equal(page.get('mobile-version').textContent,'Loaded · Keys · saved ±30°');
  assert.equal(page.get('choose-song').dataset.action,'load');assert.equal(page.get('choose-song').disabled,false);
  assert.equal(page.get('mobile-play').textContent,'Play');
  await page.get('mobile-play').click();assert.equal(page.get('mobile-play').textContent,'Pause');
  assert.equal(page.api.result(),result);assert.equal(page.requests.length,requests);assert.equal(page.workers.length,workers);
  page.get('song-search').value='no matches';page.get('song-search').dispatch('input');await nextTurn();
  assert.equal(page.get('song-load').disabled,true);assert.equal(page.get('mobile-play').disabled,false);
  assert.equal(page.get('choose-song').dataset.action,'choose');
  page.get('mobile-play').click();assert.equal(page.get('mobile-play').textContent,'Play');
  assert.equal(page.api.result(),result);
},{autoMove:false,catalog:songCatalog,network:{'/web/songs/first-keys.tl':'selected Keys record'}}));

test('sticky Load replaces an older prepared song through the shared silent loader', ()=>withPage(async page=>{
  await prepared(page,page.get('choose-song').click(),'first record');const old=page.api.result();
  selectSong(page,'songs/second.tl');
  assert.equal(page.api.result(),old);assert.equal(page.get('loaded-song').textContent,'<First song>');
  await prepared(page,page.get('choose-song').click(),'second record',validResult({recordSha256:'b'.repeat(64)}));
  assert.notEqual(page.api.result(),old);assert.equal(page.get('record').value,'second record');
  assert.equal(page.get('loaded-song').textContent,'Second song');assert.equal(page.workers.length,2);
  assert.equal(page.audioCreations(),0);assert.equal(page.get('mobile-play').textContent,'Play');
},{autoMove:false,catalog:songCatalog,network:{'/web/songs/first-keys.tl':'first record','/web/songs/second.tl':'second record'}}));

for (const outcome of ['cancelled','refused','worker error']) test(`persistent preparation stays disabled while rendering and recovers after ${outcome}`, ()=>withPage(async page=>{
  const pending=page.get('rail-load').click();page.ready();await nextTurn();
  const worker=page.workers.at(-1);
  assert.equal(page.get('choose-song').textContent,'Rendering…');assert.equal(page.get('choose-song').disabled,true);
  assert.equal(page.get('mobile-play').disabled,true);
  assert.equal(page.get('rail-load').disabled,true);assert.equal(page.get('rail-play').disabled,true);
  assert.match(page.get('rail-status').textContent,/Prepar|Render/i);
  assert.equal(page.get('cancel').hidden,false,'The existing global Cancel remains available');
  selectSong(page,'songs/second.tl');
  assert.equal(page.get('choose-song').disabled,true);assert.equal(page.get('mobile-song').textContent,'<First song>');
  assert.equal(page.get('rail-selected-song').textContent,'Second song');assert.equal(page.get('rail-load').disabled,true);
  assert.match(page.get('mobile-version').textContent,/^Preparing/);
  if (outcome==='cancelled') page.get('cancel').click();
  else if (outcome==='refused') worker.reply(validResult({verdict:'refused',pcm:null,canonical:new Uint8Array(),stderr:'Native refusal'}));
  else worker.onerror({message:'Native worker error'});
  await pending;
  assert.equal(page.get('choose-song').textContent,'Load song');assert.equal(page.get('choose-song').disabled,false);
  assert.equal(page.get('mobile-play').disabled,true);
  assert.equal(page.get('mobile-song').textContent,'Second song');assert.match(page.get('mobile-version').textContent,/^Selected/);
  assert.equal(page.get('cancel').hidden,true);assert.equal(page.audioCreations(),0);
  page.advance(0);
  assert.equal(page.get('rail-load').disabled,false);assert.equal(page.get('rail-play').disabled,true);
  assert.equal(page.get('rail-status').textContent,page.get('status').textContent,'Sections retains the actual cancellation/refusal diagnostic');
},{autoMove:false,catalog:songCatalog,network:{'/web/songs/first-keys.tl':'selected Keys record'}}));

test('persistent Load recovers after a network failure without starting audio', async()=>{
  let attempt=0;
  await withPage(async page=>{
    await page.get('rail-load').click();page.advance(0);
    assert.match(page.get('status').textContent,/503/);
    assert.equal(page.get('choose-song').textContent,'Load song');assert.equal(page.get('choose-song').disabled,false);
    assert.equal(page.get('mobile-play').disabled,true);assert.equal(page.audioCreations(),0);assert.equal(page.workers.length,0);
    assert.equal(page.get('rail-load').disabled,false);assert.equal(page.get('rail-play').disabled,true);
    assert.match(page.get('rail-status').textContent,/503/);
    await prepared(page,page.get('choose-song').click(),'retry song');
    assert.equal(page.get('mobile-play').textContent,'Play');assert.equal(page.audioCreations(),0);
    assert.equal(page.get('rail-play').disabled,false);
  },{autoMove:false,catalog:songCatalog,network:{'/web/songs/first-keys.tl':()=>++attempt===1?new Response('unavailable',{status:503}):new Response('retry song')}});
});

test('sticky navigation needs no selection and mobile Play cannot replace prepared raw-channel audio', async()=>{
  await withPage(async page=>{
    assert.equal(page.get('choose-song').dataset.action,'choose');assert.equal(page.get('choose-song').disabled,false);
    assert.equal(page.get('mobile-play').textContent,'Play');assert.equal(page.get('mobile-play').disabled,true);
    assert.equal(page.get('mobile-version').textContent,'No song selected');
    assert.equal(page.get('rail-load').disabled,true);assert.equal(page.get('rail-play').disabled,true);
    page.get('choose-song').click();page.get('mobile-play').click();assert.equal(page.workers.length,0);
  });
  await withPage(async page=>{
    await complete(page,'raw field',validResult({channels:4,pcm:new Int16Array(8),info:{route:'1',voices:'1',length_ns:'1000000000',pcm_bytes:'16'}}));
    const result=page.api.result();selectSong(page,'songs/second.tl');
    assert.equal(page.get('mobile-play').textContent,'Play');assert.equal(page.get('mobile-play').disabled,true);
    assert.equal(page.get('rail-play').disabled,true);page.get('rail-play').click();
    page.get('mobile-play').click();assert.equal(page.api.result(),result);
    assert.match(page.get('mobile-version').textContent,/^Loaded/);assert.equal(page.workers.length,1);
    assert.equal(page.requests.filter(url=>url.endsWith('.tl')).length,0);
  },{catalog:songCatalog});
});

test('file saves use a safe title and mobile controls operate the same prepared transport', () => withPage(async page=>{
  page.get('file').files=[{name:'../A:night?.tl',size:9,text:async()=>'local file'}];
  await prepared(page,page.get('file').onchange(),'local file');
  page.get('tl-download').click();assert.equal(page.filenames.at(-1),'-A-night--saved.tl');
  assert.equal(page.get('loaded-song').textContent,'../A:night?');
  await page.get('mobile-play').click();assert.equal(page.get('play').textContent,'Pause');
  page.get('mobile-play').click();assert.equal(page.get('play').textContent,'Play');
  page.get('choose-song').click();assert.equal(page.get('library-panel').open,true);
  assert.equal(document.activeElement,page.get('library-summary'));
}));

test('dropping a file prepares playback without acquiring audio', () => withPage(async page => {
  const pending = page.get('dropzone').ondrop({preventDefault() {}, dataTransfer: {
    files: [{size: 12, name: 'dropped.tl', arrayBuffer: async () => new TextEncoder().encode('dropped record').buffer}]}});
  await prepared(page, pending, 'dropped record');
  assert.equal(page.get('play').disabled, false); assert.equal(page.audioCreations(), 0);
}));

test('loading the example prepares playback without acquiring audio', () => withPage(async page => {
  await prepared(page, page.get('example').click(), 'example record');
  assert.equal(page.get('play').disabled, false); assert.equal(page.audioCreations(), 0);
}, {network: {'/web/example.tl': 'example record'}}));

test('loading a URL through the form prepares playback without acquiring audio', () => withPage(async page => {
  page.get('url').value = 'https://player.invalid/tunes/one.tl';
  const pending = page.get('url-form').onsubmit({preventDefault() {}});
  await prepared(page, pending, 'URL record');
  assert.equal(page.get('play').disabled, false); assert.equal(page.audioCreations(), 0);
}, {network: {'/tunes/one.tl': 'URL record'}}));

test('opening a service song link prepares its named release without acquiring audio', () => withPage(async page => {
  const result = await prepared(page, undefined, 'linked record');
  assert.equal(page.workers[0].request.engine.id, 'test');
  assert.equal(result.recordSha256, 'a'.repeat(64));
  assert.equal(page.get('play').disabled, false); assert.equal(page.audioCreations(), 0);
}, {href: 'https://player.invalid/preview/r/' + 'a'.repeat(64), network: {
  ['/preview/api/records/' + 'a'.repeat(64)]: {record_sha256: 'a'.repeat(64), renderer_release: 'test', record_url: 'r/song.tl'},
  '/preview/r/song.tl': 'linked record',
}}));

test('a failed automatic preparation cannot retain previously playable audio', () => withPage(async page => {
  await complete(page);
  page.get('file').files = [{size: 12, name: 'bad.tl', text: async () => 'bad record'}];
  await prepared(page, page.get('file').onchange(), 'bad record', validResult({verdict: 'mismatch', pcm: undefined}));
  invalidActions(page);
  assert.match(page.get('verdict').textContent, /mismatch/);
}));

test('cancelled automatic preparation leaves Play disabled and no late result', () => withPage(async page => {
  page.ready();
  page.get('file').files = [{size: 12, name: 'cancelled.tl', text: async () => 'cancelled record'}];
  const loading = page.get('file').onchange(); await nextTurn();
  const worker = page.workers.at(-1);
  assert.equal(worker.request.mode, 'listen');
  page.get('cancel').click(); await loading;
  assert.equal(worker.terminated, true);
  worker.onmessage({data: validResult()}); await nextTurn();
  assert.equal(page.api.result(), null); invalidActions(page);
}));

test('example UI consumes preparation cancellation and preserves a newer loading status', () => withPage(async page => {
  page.ready();
  const first = page.get('example').click(); await nextTurn();
  const oldWorker = page.workers.at(-1);
  const current = page.api.listen('new draft'); await nextTurn();
  await assert.doesNotReject(first);
  assert.equal(oldWorker.terminated, true);
  assert.equal(page.get('status').textContent, 'Rendering once for listening…');
  page.workers.at(-1).reply(validResult()); await current;
  assert.equal(page.get('record').value, 'new draft');
}, {network: {'/web/example.tl': 'example record'}}));

test('a superseded automatic worker cannot replace the newer prepared song', () => withPage(async page => {
  page.ready();
  page.get('file').files = [{size: 12, name: 'first.tl', text: async () => 'first record'}];
  const first = page.get('file').onchange(); await nextTurn();
  const obsolete = page.workers.at(-1);
  page.get('file').files = [{size: 12, name: 'second.tl', text: async () => 'second record'}];
  const latest = await prepared(page, page.get('file').onchange(), 'second record');
  await first; const status = page.get('status').textContent;
  assert.equal(obsolete.terminated, true);
  obsolete.onmessage({data: validResult({pcmHash: 'obsolete'})}); await nextTurn();
  assert.equal(page.api.result(), latest);
  assert.equal(page.get('record').value, 'second record');
  assert.equal(page.get('status').textContent, status);
  assert.equal(page.get('play').disabled, false); assert.equal(page.audioCreations(), 0);
}));

test('mismatch cannot enable canonical downloads or record sharing', () => withPage(async page => {
  await complete(page, 'wrong hash', validResult({verdict: 'mismatch', pcmHash: '0123456789abcdef',
    play: {expected:'0000000000000000',match:'NO',block_indep:'yes'}}));
  invalidActions(page);
  assert.equal(page.get('verdict').textContent, 'mismatch');
  assert.match(page.get('status').textContent, /expected 0000000000000000, rendered 0123456789abcdef/);
  assert.match(page.get('status').textContent, /Reopen a known-good saved TL/);
  const details = page.get('facts').children.map(element => element.textContent).join('\n');
  assert.match(details, /Measurements below are diagnostic/);
  assert.match(details, /No accepted TL or WAV export/);
  assert.doesNotMatch(details, /bytes PCM WAV/);
}));

for (const [route, channels, label] of [['1', 4, 'amb1'], ['2', 16, 'amb3']]) {
  test(label + ' raw coefficients never enable speaker playback', () => withPage(async page => {
    const value = () => validResult({channels, pcm: new Int16Array(channels * 2),
      info: {route, voices: '1', length_ns: '1000000000', pcm_bytes: String(channels * 4)}});
    await complete(page, label, value());
    page.get('file').files = [{size: label.length, name: label + '.tl', text: async () => label}];
    const preparedValue = value(); preparedValue.play.block_indep = 'unchecked';
    await prepared(page, page.get('file').onchange(), label, preparedValue);
    assert.equal(page.get('play').disabled, true); assert.equal(page.get('stop').disabled, true);
    page.get('play').click(); assert.equal(page.audioCreations(), 0);
    assert.match(page.get('status').textContent, /speaker playback is unavailable/);
    assert.equal(page.get('wav-download').disabled, false, 'Raw PCM remains exportable');
  }));
}

test('listen API uses one render and never claims block independence or starts audio', () => withPage(async page => {
  page.ready();
  const pending = page.api.listen('listener record');
  await nextTurn();
  assert.equal(page.workers[0].request.mode, 'listen');
  page.workers[0].reply(validResult({play: {match: 'yes', block_indep: 'unchecked'}}));
  const result = await pending;
  assert.equal(result.verified, false); assert.equal(result.verification, 'checksum-matched');
  assert.match(page.get('verdict').getAttribute('title'), /block independence unchecked/);
  assert.equal(page.audioCreations(), 0);
}));

test('human Listen resumes audio in the gesture before waiting for rendering', () => withPage(async page => {
  page.get('record').value = 'listener record';
  const pending = page.get('listen').click();
  assert.deepEqual(page.audioEvents, ['construct', 'resume']);
  assert.equal(page.workers.length, 0);
  page.ready(); await nextTurn();
  page.workers[0].reply(validResult({play: {match: 'yes', block_indep: 'unchecked'}}));
  await pending;
  assert.deepEqual(page.audioEvents, ['construct', 'resume', 'start']);
  assert.equal(page.api.result().verified, false);
}));

test('explicit Play requests a playback session synchronously before opening audio', () => {
  const requests = [];
  let page;
  return withPage(async current => {
    page = current;
    await complete(page);
    assert.deepEqual(requests, []);
    const playing = page.get('play').click();
    assert.deepEqual(requests, [{type:'playback',audioEvents:[]}]);
    await playing;
    assert.deepEqual(page.audioEvents, ['construct','resume','start']);
  }, {audioSession:{value:{set type(type) { requests.push({type,audioEvents:[...page.audioEvents]}); }}}});
});

test('explicit Listen requests playback before any asynchronous render work', () => {
  const requests = [];
  let page;
  return withPage(async current => {
    page = current;
    page.get('record').value = 'listener record';
    const listening = page.get('listen').click();
    assert.deepEqual(requests, [{type:'playback',audioEvents:[],workers:0}]);
    page.ready(); await nextTurn();
    page.workers[0].reply(validResult()); await listening;
    assert.deepEqual(page.audioEvents, ['construct','resume','start']);
  }, {audioSession:{value:{set type(type) { requests.push({type,audioEvents:[...page.audioEvents],workers:page.workers.length}); }}}});
});

test('browsers without Audio Session still play through the existing Web Audio path', () => withPage(async page => {
  assert.equal('audioSession' in navigator, false);
  await complete(page); await page.get('play').click();
  assert.deepEqual(page.audioEvents, ['construct','resume','start']);
  assert.equal(page.get('status').textContent, 'Playing.');
}));

test('throwing Audio Session getter or setter cannot prevent playback', async () => {
  for (const audioSession of [
    {get() { throw Error('Session access denied'); }},
    {value:{set type(_) { throw Error('Session routing unavailable'); }}},
  ]) await withPage(async page => {
    await complete(page); await page.get('play').click();
    assert.deepEqual(page.audioEvents, ['construct','resume','start']);
    assert.equal(page.get('status').textContent, 'Playing.');
  }, {audioSession});
});

test('silent file loading and programmatic rendering never request an audio session', () => {
  let accesses = 0;
  return withPage(async page => {
    await complete(page);
    page.get('file').files = [{size:20,name:'local.tl',text:async () => 'local fixture'}];
    await prepared(page,page.get('file').onchange(),'local fixture');
    const listening=page.api.listen('programmatic fixture'); await nextTurn();
    page.workers.at(-1).reply(validResult()); await listening;
    assert.equal(accesses,0);
    assert.equal(page.audioCreations(),0);
  }, {audioSession:{get() { accesses++; return {type:'ambient'}; }}});
});

function transportResult() {
  const pcm = new Int16Array(20000); pcm[0] = -32768; pcm[1] = 32767;
  return validResult({sampleRate: 1000, channels: 2, pcm,
    info: {route: '0', voices: '1', length_ns: '10000000000', pcm_bytes: String(pcm.byteLength)}});
}

test('Auto move follows explicit mirrored Load and Play through natural finish without changing the chosen visual', () => withPage(async page=>{
  assert.equal(page.get('song-load-right').disabled,page.get('song-load').disabled);
  await prepared(page,page.get('song-load-right').click(),'first record',transportResult());
  assert.deepEqual(page.sectionJumps,['player-panel']);assert.equal(page.audioCreations(),0);
  assert.equal(page.get('play-right').disabled,false);assert.equal(page.get('play-right').textContent,'Play');
  page.get('view-off').click();await page.get('play-right').click();
  assert.deepEqual(page.sectionJumps,['player-panel','live-views']);assert.equal(page.get('play-right').textContent,'Pause');
  assert.equal(page.get('display-surface').getAttribute('data-view'),'off','Following the section does not turn visual work back on');
  page.audioSources.at(-1).onended();
  assert.deepEqual(page.sectionJumps,['player-panel','live-views','library-panel']);
  assert.equal(page.get('play-right').textContent,'Play');assert.equal(page.workers.length,1);
}, {catalog:songCatalog,network:{'/web/songs/first-keys.tl':'first record'}}));

test('Auto move off and manual Choose song preserve location and require explicit re-enabling', () => withPage(async page=>{
  await complete(page,'programmatic',transportResult());assert.deepEqual(page.sectionJumps,[],'Programmatic rendering cannot navigate');
  page.setSection('live-views');
  page.get('choose-song').click();assert.equal(page.get('auto-move').checked,false);
  await prepared(page,page.get('song-load-right').click(),'first record',transportResult());
  await page.get('play-right').click();page.audioSources.at(-1).onended();
  assert.deepEqual(page.sectionJumps,['library-panel']);
  page.get('auto-move').checked=true;page.get('auto-move').dispatch('change');
  assert.deepEqual(page.sectionJumps,['library-panel'],'Re-enabling does not move or start audio');
  await page.get('play-right').click();assert.deepEqual(page.sectionJumps,['library-panel','live-views']);
}, {catalog:songCatalog,network:{'/web/songs/first-keys.tl':'first record'}}));

test('Auto move announces an automatic switch-off and offers a one-step way back', () => withPage(async page=>{
  const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  assert.match(html,/<p id="auto-move-off" class="small" hidden>/,'The note starts hidden');
  assert.match(html,/<p id="auto-move-status" class="sr-only" role="status" aria-live="polite"><\/p>\s*<script type="module" src="app.mjs">/,
    'The announcement region sits outside the Sections menu, which is hidden on small screens');
  assert.equal(page.get('auto-move-status').textContent,'');
  page.setSection('live-views');page.get('choose-song').click();
  assert.equal(page.get('auto-move').checked,false);assert.deepEqual(page.sectionJumps,['library-panel']);
  assert.equal(page.get('auto-move-status').textContent,'Auto move off. Turn it back on in Sections.');
  assert.equal(page.get('auto-move-off').hidden,false,'The rail shows why the box is clear');
  page.setSection('live-views');page.get('choose-song').click();
  assert.equal(page.get('auto-move-status').textContent,'Auto move off. Turn it back on in Sections.','An already-off mode is not announced again');
  page.get('auto-move-on').click();
  assert.equal(page.get('auto-move').checked,true);assert.equal(page.get('auto-move-off').hidden,true);
  assert.equal(page.get('auto-move-status').textContent,'');
  assert.equal(document.activeElement,page.get('auto-move'),'Focus moves to the box when its note hides');
  assert.deepEqual(page.sectionJumps,['library-panel','library-panel'],'Turning it back on does not move');
  page.get('auto-move').checked=false;page.get('auto-move').dispatch('change');
  assert.equal(page.get('auto-move-off').hidden,true,'Turning it off yourself needs no notice');
  assert.equal(page.get('auto-move-status').textContent,'');
}));

test('rail text boxes are Tab stops only while their text is clipped', () => withPage(async page=>{
  const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  for (const id of ['rail-selection','rail-loaded-song','rail-status'])
    assert.doesNotMatch(html,new RegExp(`id="${id}"[^>]*tabindex`),'Static rail text starts outside the Tab order');
  const loaded=page.get('rail-loaded-song'),status=page.get('rail-status');
  loaded.clientHeight=48;loaded.scrollHeight=96;status.clientHeight=48;status.scrollHeight=48;
  await complete(page,'clipped fixture',transportResult());
  assert.equal(loaded.getAttribute('tabindex'),'0','Clipped text stays reachable for keyboard scrolling');
  assert.equal(status.getAttribute('tabindex'),null);
  loaded.scrollHeight=48;status.scrollHeight=96;
  await page.get('play').click();
  assert.equal(loaded.getAttribute('tabindex'),null,'Text that fits again leaves the Tab order');
  assert.equal(status.getAttribute('tabindex'),'0');
  status.focus();status.scrollHeight=48;page.advance(1);await page.get('play').click();
  assert.match(status.textContent,/Paused/);
  assert.equal(status.getAttribute('tabindex'),'0','A focused box keeps its Tab stop until focus leaves');
}));

test('an old audio-resume promise cannot move a manually changed section even after Auto move is re-enabled', async()=>{
  const resumed=deferred();
  await withPage(async page=>{
    await complete(page,'pending playback',transportResult());
    const playing=page.get('play-right').click();
    page.get('choose-song').click();
    page.get('auto-move').checked=true;page.get('auto-move').dispatch('change');
    resumed.resolve();await playing;
    assert.equal(page.audioSources.length,1,'The original explicit playback request can complete');
    assert.deepEqual(page.sectionJumps,['library-panel'],'Its old navigation intention cannot override manual navigation');
    page.audioSources[0].onended();assert.deepEqual(page.sectionJumps,['library-panel','library-panel'],'Re-enabled mode applies to the later natural finish');
  },{audioResume:()=>resumed.promise});
});

test('mirrored transport shares Pause and Resume while Stop, stale ends and looping do not return to Songs', () => withPage(async page=>{
  await complete(page,'transport',transportResult());await page.get('play-right').click();
  const staleEnd=page.audioSources[0].onended;
  page.advance(2);page.get('play-right').click();
  assert.equal(page.get('play').textContent,'Resume');assert.equal(page.get('play-right').textContent,'Resume');
  assert.deepEqual(page.sectionJumps,['live-views']);staleEnd();assert.deepEqual(page.sectionJumps,['live-views']);
  await page.get('play-right').click();const laterEnd=page.audioSources[1].onended;
  page.get('stop').click();laterEnd();assert.deepEqual(page.sectionJumps,['live-views','live-views']);
  page.get('loop').checked=true;await page.get('play-right').click();page.audioSources.at(-1).onended();
  assert.ok(!page.sectionJumps.includes('library-panel'));
  await complete(page,'refused',validResult({verdict:'refused',pcm:undefined}));
  assert.equal(page.get('play').disabled,true);assert.equal(page.get('play-right').disabled,true);
}));
test('Pause, Resume and Stop retain position and reuse the prepared PCM buffer', () => withPage(async page => {
  const result = await complete(page, 'transport fixture', transportResult()), before = result.pcm.slice();
  await page.get('play').click();
  assert.equal(page.get('play').textContent, 'Pause');
  assert.deepEqual(page.audioSources[0].startArgs, [0, 0]);
  page.advance(3);
  assert.equal(page.get('time').value, '0:03 / 0:10');
  assert.equal(page.get('seek').getAttribute('aria-valuetext'), '0:03 of 0:10');
  await page.get('play').click();
  assert(page.audioSources[0].stopped); assert.equal(page.audioSources[0].connected, false);
  assert.equal(page.get('play').textContent, 'Resume');
  page.advance(2); assert.equal(Number(page.get('seek').value), 3);
  await page.get('play').click();
  assert.deepEqual(page.audioSources[1].startArgs, [0, 3]);
  assert.equal(page.audioSources[1].buffer, page.audioSources[0].buffer);
  page.advance(2); assert.equal(Number(page.get('seek').value), 5);
  page.get('stop').click();
  assert.equal(Number(page.get('seek').value), 0); assert.equal(page.get('play').textContent, 'Play');
  await page.get('play').click(); assert.deepEqual(page.audioSources[2].startArgs, [0, 0]);
  assert.equal(page.audioBuffers.length, 1); assert.equal(page.workers.length, 1);
  assert.equal(page.audioBuffers[0].getChannelData(0)[0], -1);
  assert.equal(page.audioBuffers[0].getChannelData(1)[0], 32767 / 32768);
  assert.deepEqual(result.pcm, before);
}));

test('seeking before and during playback uses cached audio and natural end restarts cleanly', () => withPage(async page => {
  await complete(page, 'seek fixture', transportResult());
  page.get('seek').value = '4'; page.get('seek').oninput();
  assert.equal(page.audioCreations(), 0); assert.equal(page.get('play').textContent, 'Resume');
  await page.get('play').click(); assert.deepEqual(page.audioSources[0].startArgs, [0, 4]);
  page.advance(2); assert.equal(Number(page.get('seek').value), 6);
  page.get('seek').value = '1.25'; page.get('seek').oninput();
  assert(page.audioSources[0].stopped); assert.deepEqual(page.audioSources[1].startArgs, [0, 1.25]);
  page.get('seek').value = '10'; page.get('seek').oninput();
  assert(page.audioSources[1].stopped); assert.equal(page.audioSources.length, 2);
  assert.equal(page.get('play').textContent, 'Play');
  await page.get('play').click(); assert.deepEqual(page.audioSources[2].startArgs, [0, 0]);
  page.audioSources[2].onended();
  assert.equal(page.get('time').value, '0:10 / 0:10'); assert.equal(page.get('play').textContent, 'Play');
  await page.get('play').click(); assert.deepEqual(page.audioSources[3].startArgs, [0, 0]);
  assert.equal(page.audioBuffers.length, 1); assert.equal(page.workers.length, 1);
}));

test('loop position, toggle and seek-to-end preserve playback intent', () => withPage(async page => {
  await complete(page, 'loop fixture', transportResult());
  page.get('loop').checked = true; page.get('loop').onchange();
  await page.get('play').click(); assert.equal(page.audioSources[0].loop, true);
  page.advance(12); assert.equal(Number(page.get('seek').value), 2);
  page.get('loop').checked = false; page.get('loop').onchange();
  assert.equal(Number(page.get('seek').value), 2); assert.equal(page.audioSources[0].loop, false);
  page.advance(1); assert.equal(Number(page.get('seek').value), 3);
  page.get('loop').checked = true; page.get('loop').onchange();
  page.get('seek').value = '10'; page.get('seek').oninput();
  assert(page.audioSources[0].stopped);
  assert.deepEqual(page.audioSources[1].startArgs, [0, 0]); assert.equal(page.audioSources[1].loop, true);
  assert.equal(page.get('play').textContent, 'Pause');
  await page.get('play').click();
  page.get('seek').value = '10'; page.get('seek').oninput();
  assert.equal(page.audioSources.length, 2, 'Seeking while paused must remain paused, even with Loop enabled');
  assert.equal(Number(page.get('seek').value), 10);
  await page.get('play').click(); assert.deepEqual(page.audioSources[2].startArgs, [0, 0]);
  assert.equal(page.audioBuffers.length, 1);
}));

test('a new rendered record replaces the playback cache and stops old audio', () => withPage(async page => {
  await complete(page, 'first song', transportResult()); await page.get('play').click();
  const previous = page.audioSources[0], changed = transportResult(); changed.pcm[0] = 16384;
  await complete(page, 'second song', changed);
  assert(previous.stopped); assert.equal(page.get('time').value, '0:00 / 0:10');
  await page.get('play').click();
  assert.equal(page.audioBuffers.length, 2); assert.notEqual(page.audioSources[1].buffer, previous.buffer);
  assert.equal(page.audioBuffers[1].getChannelData(0)[0], .5);
}));

test('Stop prevents a pending audio resume from starting playback later', () => {
  const resumed = deferred();
  return withPage(async page => {
    await complete(page, 'resume fixture', transportResult());
    const playing = page.get('play').click(); assert.equal(page.audioSources.length, 0);
    page.get('stop').click(); resumed.resolve(); await playing;
    assert.equal(page.audioSources.length, 0); assert.equal(page.get('status').textContent, 'Stopped.');
    assert.equal(page.get('play').textContent, 'Play');
    await page.get('play').click(); assert.equal(page.audioSources.length, 1);
  }, {audioResume: () => resumed.promise});
});

test('a new load prevents an older pending audio resume from starting old audio', () => {
  const resumed = deferred();
  return withPage(async page => {
    await complete(page, 'old song', transportResult());
    const playing = page.get('play').click();
    await complete(page, 'new song', transportResult()); const status = page.get('status').textContent;
    resumed.resolve(); await playing;
    assert.equal(page.audioSources.length, 0); assert.equal(page.get('record').value, 'new song');
    assert.equal(page.get('status').textContent, status);
    await page.get('play').click(); assert.equal(page.audioSources.length, 1); assert.equal(page.audioBuffers.length, 1);
  }, {audioResume: () => resumed.promise});
});

test('Stop also suppresses delayed autoplay from the explicit Listen action', () => {
  const resumed = deferred();
  return withPage(async page => {
    page.ready(); page.get('record').value = 'edited song';
    const listening = page.get('listen').click(); await nextTurn();
    page.workers[0].reply(transportResult()); await nextTurn();
    assert.equal(page.get('stop').disabled, false);
    assert.equal(page.get('status').textContent, 'Waiting for audio. Press Play again if it stays silent.');
    page.get('stop').click(); resumed.resolve(); await listening;
    assert.equal(page.audioSources.length, 0); assert.equal(page.get('status').textContent, 'Stopped.');
  }, {audioResume: () => resumed.promise});
});

test('spectrogram analysis is lazy, preserves playback and caches the current image', () => withPage(async page => {
  const result = await complete(page, 'spectrum fixture', transportResult()), before = result.pcm.slice();
  assert.equal(page.spectrumWorkers.length, 0);
  await page.get('play').click();
  const source = page.audioSources[0], pending = page.get('view-spectrum').click();
  assert.equal(page.spectrumWorkers.length, 1); assert.equal(source.stopped, false);
  assert.equal(page.get('png-download').disabled, true);
  const worker = page.spectrumWorkers[0]; worker.reply({type: 'progress', fraction: .5});
  assert.match(page.get('spectrum-status').textContent, /50%/);
  worker.reply({type: 'result', analysis: analyzeSpectrogram(worker.request)}); await pending;
  assert.equal(page.get('png-download').disabled, false); assert.equal(page.get('spectrogram').hidden, false);
  assert.equal(page.get('spectrogram').images.length, 2); assert.equal(source.stopped, false);
  page.get('view-notes').click(); assert.equal(page.get('view-notes').getAttribute('aria-pressed'), 'true');
  await page.get('view-spectrum').click();
  assert.equal(page.spectrumWorkers.length, 1); assert.equal(page.get('spectrogram').images.length, 2);
  assert.deepEqual(result.pcm, before); assert.equal(page.workers.length, 1);
}));

test('Live display follows play, pause, seek and record invalidation without new rendering', () => withPage(async page => {
  const result = await complete(page, 'live fixture', transportResult());
  assert(page.visualCalls.some(call => call[0] === 'result' && call[1] === result));
  page.get('view-live').click();
  assert.equal(page.get('view-live').getAttribute('aria-pressed'), 'true');
  assert.equal(page.get('timeline').hidden, true);
  await page.get('play').click(); page.advance(1.25);
  assert.deepEqual(page.visualCalls.at(-1), ['update', 1.25, true]);
  await page.get('play').click(); page.advance(1);
  assert.deepEqual(page.visualCalls.at(-1), ['update', 1.25, false]);
  page.get('seek').value = '2'; page.get('seek').oninput();
  assert.deepEqual(page.visualCalls.at(-1), ['update', 2, false]);
  page.get('view-notes').click();
  assert(page.visualCalls.some(call => call[0] === 'visible' && call[1] === false));
  assert.equal(page.workers.length, 1);
  page.api.clear();
  assert(page.visualCalls.some(call => call[0] === 'clear'));
}));

test('live drawing follows animation frames without paused polling or duplicate transport updates', () => withPage(async page => {
  await complete(page,'animation clock',transportResult());
  const paused = page.visualCalls.length;
  page.advance(.2);assert.equal(page.visualCalls.length,paused);
  assert.equal(page.animationCount(),0);
  await page.get('play').click();assert.equal(page.animationCount(),1);
  page.advance(0);const first = page.visualCalls.length;
  page.advance(.01);assert.equal(page.visualCalls.length,first,'sub-frame ticks do not redraw');
  page.advance(.03);assert.equal(page.visualCalls.length,first+1,'one visual update, not another transport redraw');
  assert.deepEqual(page.visualCalls.at(-1),['update',.04,true]);
  await page.get('play').click();assert.equal(page.animationCount(),0);
  const stopped = page.visualCalls.length;
  page.advance(.2);assert.equal(page.visualCalls.length,stopped,'paused transport has no PCM analysis');
  await page.get('play').click();page.get('view-off').click();assert.equal(page.animationCount(),0);
}));

test('Off and background pages suspend live visualization while cached playback continues', () => withPage(async page => {
  await complete(page,'visual suspension',transportResult());await page.get('play').click();
  const audio=page.audioSources[0];page.get('view-off').click();
  assert.equal(page.get('live-panel').hidden,true);assert.equal(page.get('display-off').hidden,false);
  const offCalls=page.visualCalls.length;page.advance(.5);assert.equal(page.visualCalls.length,offCalls);
  assert.equal(audio.stopped,false);assert.equal(page.get('seek').value,'0.5');
  page.get('view-live').click();assert.deepEqual(page.visualCalls.at(-1),['update',.5,true]);
  page.setHidden(true);const hiddenCalls=page.visualCalls.length;page.advance(.5);
  assert.equal(page.visualCalls.length,hiddenCalls);assert.equal(audio.stopped,false);
  page.setHidden(false);assert.deepEqual(page.visualCalls.at(-1),['update',1,true]);
  assert.equal(page.workers.length,1);assert.equal(page.audioBuffers.length,1);
}));

test('whole Live region leaving the viewport stops sampling and resumes at the current audio clock', () => withPage(async page => {
  await complete(page,'intersection suspension',transportResult());
  assert(page.visualCalls.some(call=>call[0]==='update'),'Live starts normally before the first observer callback');
  await page.get('play').click();page.setIntersecting(false);const hiddenCalls=page.visualCalls.length;
  page.advance(1);assert.equal(page.visualCalls.length,hiddenCalls);assert.equal(page.audioSources[0].stopped,false);
  page.setIntersecting(true,0);assert.deepEqual(page.visualCalls.at(-1),['update',1,true],
    'Edge adjacency must resume: default zero threshold need not emit another event as overlap grows');
  page.get('view-off').click();page.setIntersecting(true);const offCalls=page.visualCalls.length;
  page.advance(.5);assert.equal(page.visualCalls.length,offCalls,'intersection cannot override explicit Off');
  page.get('view-live').click();page.setHidden(true);page.setIntersecting(true);const backgroundCalls=page.visualCalls.length;
  page.advance(.5);assert.equal(page.visualCalls.length,backgroundCalls,'intersection cannot override a hidden document');
  page.setHidden(false);assert.deepEqual(page.visualCalls.at(-1),['update',2,true]);
}, {observer:true}));

test('hidden or deselected spectrogram cancels work and ignores its stale completion', () => withPage(async page => {
  await complete(page,'spectrum suspension',transportResult());await page.get('play').click();
  let pending=page.get('view-spectrum').click(), first=page.spectrumWorkers.at(-1);
  page.setHidden(true);assert.equal(first.terminated,true);await pending;
  first.onmessage?.({data:{type:'result',analysis:analyzeSpectrogram(first.request)}});
  assert.equal(page.get('png-download').disabled,true);
  assert.equal(page.get('spectrogram').images.length,0);assert.equal(page.audioSources[0].stopped,false);
  page.setHidden(false);assert.equal(page.spectrumWorkers.length,2);
  const second=page.spectrumWorkers.at(-1);page.get('view-off').click();assert.equal(second.terminated,true);
  await nextTurn();assert.equal(page.get('png-download').disabled,true);
  page.advance(1);assert.equal(page.spectrumWorkers.length,2);
  pending=page.get('view-spectrum').click();const third=page.spectrumWorkers.at(-1);
  third.reply({type:'result',analysis:analyzeSpectrogram(third.request)});await pending;
  assert.equal(page.get('png-download').disabled,false);assert.equal(page.workers.length,1);
}));

test('computed profiles start from the loaded source, prepare silently and restore the original', () => withPage(async page => {
  await complete(page, 'original record');
  assert.equal(page.get('listening-profile').disabled, false);
  page.get('listening-profile').value = '60'; page.get('listening-profile').onchange();
  assert.match(page.get('profile-detail').textContent, /120° total spread.*selected/);
  let pending = page.get('profile-apply').click(); await nextTurn();
  assert.equal(page.workers.at(-1).request.recordText, 'original record');
  assert.equal(page.workers.at(-1).request.profile.halfWidth, 60);
  assert.equal(page.workers.at(-1).request.profile.mirror, false);
  page.workers.at(-1).reply(validResult({canonical:new TextEncoder().encode('derived sixty'), recordSha256:'b'.repeat(64),
    sourceRecordSha256:'a'.repeat(64)})); await pending;
  assert.equal(page.get('record').value, 'derived sixty');
  assert.equal(page.get('listening-profile').value, '60');
  assert.equal(page.audioCreations(), 0);
  page.get('listening-profile').value = '90'; page.get('profile-mirror').checked = true;
  pending = page.get('profile-apply').click(); await nextTurn();
  assert.equal(page.workers.at(-1).request.recordText, 'original record', 'Widths must not accumulate');
  assert.equal(page.workers.at(-1).request.profile.mirror, true);
  page.workers.at(-1).reply(validResult({canonical:new TextEncoder().encode('derived ninety'), recordSha256:'c'.repeat(64),
    sourceRecordSha256:'a'.repeat(64)})); await pending;
  page.get('listening-profile').value = 'original'; page.get('listening-profile').onchange();
  assert.equal(page.get('profile-mirror').disabled, true);
  pending = page.get('profile-apply').click(); await nextTurn();
  assert.equal(page.workers.at(-1).request.recordText, 'original record');
  assert.equal(page.workers.at(-1).request.profile, undefined);
  page.workers.at(-1).reply(validResult()); await pending;
  assert.equal(page.get('record').value, 'original record');
  assert.equal(page.get('listening-profile').value, 'original');
  assert.equal(page.audioCreations(), 0);
}));

test('clearing during profile computation discards the source and cannot restore stale audio', () => withPage(async page => {
  await complete(page);
  page.get('listening-profile').value = '45';
  const pending = page.get('profile-apply').click(); await nextTurn();
  const worker = page.workers.at(-1);
  page.api.clear(); await pending;
  assert(worker.terminated);
  assert.equal(page.get('profile-apply').disabled, true);
  assert.equal(page.get('record').value, '');
  assert.equal(page.api.result(), null);
}));

test('quiet profile recovery is explicit and labels a below-target result', () => withPage(async page => {
  await complete(page);
  assert.equal(page.get('profile-quiet').checked, false);
  page.get('listening-profile').value = '60'; page.get('listening-profile').onchange();
  let pending = page.get('profile-apply').click(); await nextTurn();
  assert.equal(page.workers.at(-1).request.profile.levelPolicy, 'strict');
  page.workers.at(-1).reply({verdict:'refused',code:2,stdout:'',stderr:'Carrier gain limit'}); await pending;
  assert.equal(page.get('status').textContent, 'Carrier gain limit');
  assert.equal(page.get('listening-profile').value, '60', 'Keep the refused choice ready for explicit recovery');
  assert.equal(page.get('profile-quiet').disabled, false);
  page.get('profile-quiet').checked = true; page.get('profile-quiet').onchange();
  pending = page.get('profile-apply').click(); await nextTurn();
  assert.equal(page.workers.at(-1).request.profile.levelPolicy, 'carrier-bound');
  page.workers.at(-1).reply(validResult({canonical:new TextEncoder().encode('quiet projection'),
    profile:{halfWidth:60,mirror:false,levelPolicy:'carrier-bound',targetMet:false}}));
  await pending;
  assert.equal(page.get('profile-quiet').checked, true);
  assert.match(page.get('profile-detail').textContent, /Below the normalization target/);
}));

for (const outcome of ['cancel', 'refusal']) test(`a profile ${outcome} cannot label the restored source as an older projection`, () => withPage(async page => {
  await complete(page, 'original profile source');
  page.get('listening-profile').value = '60';
  let pending = page.get('profile-apply').click(); await nextTurn();
  page.workers.at(-1).reply(validResult({canonical:new TextEncoder().encode('sixty degree projection'),recordSha256:'b'.repeat(64)}));
  await pending;
  page.get('listening-profile').value = '75';
  pending = page.get('profile-apply').click(); await nextTurn();
  if (outcome === 'cancel') page.api.cancel();
  else page.workers.at(-1).reply({verdict:'refused',code:2,stderr:'Profile refused',stdout:''});
  await pending;
  assert.equal(page.get('record').value, 'original profile source');
  pending = page.api.verify(page.get('record').value); await nextTurn();
  page.workers.at(-1).reply(validResult()); await pending;
  assert.equal(page.get('listening-profile').value, 'original');
  assert.match(page.get('profile-detail').textContent, /^Loaded version prepared/);
}));

test('cancelled spectrogram analysis cannot enable PNG and can be retried', () => withPage(async page => {
  await complete(page);
  const pending = page.get('view-spectrum').click(), previous = page.spectrumWorkers[0];
  page.get('spectrum-cancel').click(); await pending;
  assert(previous.terminated); assert.equal(page.get('png-download').disabled, true);
  assert.match(page.get('spectrum-status').textContent, /cancelled/);
  previous.onmessage({data: {type: 'result', analysis: analyzeSpectrogram(previous.request)}});
  assert.equal(page.get('spectrogram').images.length, 0);
  const retry = page.get('view-spectrum').click(), worker = page.spectrumWorkers[1];
  worker.reply({type: 'result', analysis: analyzeSpectrogram(worker.request)}); await retry;
  assert.equal(page.get('png-download').disabled, false);
}));

test('replacing a record aborts stale analysis and only the current spectrum can export', () => withPage(async page => {
  await complete(page, 'old spectrum');
  const pending = page.get('view-spectrum').click(), obsolete = page.spectrumWorkers[0];
  await complete(page, 'new spectrum'); await pending;
  assert(obsolete.terminated); assert.equal(page.get('png-download').disabled, true);
  obsolete.onmessage({data: {type: 'result', analysis: analyzeSpectrogram(obsolete.request)}});
  assert.equal(page.get('spectrogram').images.length, 0); assert.equal(page.get('png-download').disabled, true);
  const current = page.spectrumWorkers[1];
  current.reply({type: 'result', analysis: analyzeSpectrogram(current.request)}); await nextTurn();
  assert.equal(page.get('png-download').disabled, false); assert.equal(page.get('spectrogram').images.length, 2);
  assert.equal(page.get('record').value, 'new spectrum');
}));

test('unhashed drafts render but cannot acquire a verified label', () => withPage(async page => {
  const result = await complete(page, 'draft', validResult({play: {match: '-', block_indep: 'yes'}}));
  assert.equal(result.verified, false); assert.equal(result.verification, 'unverified');
  assert.match(page.get('verdict').getAttribute('title'), /expected hash not provided/);
  assert.equal(page.get('tl-download').disabled, false, 'Canonical output remains available for subsequent replay');
}));

test('explicit kept release selects that entry instead of the default', () => withPage(async page => {
  page.ready({default: 'default', engines: [{id: 'default'}, {id: 'kept', release_id: '2026-10-25-abcdef0'}]});
  const pending = page.api.render('record', {release: '2026-10-25-abcdef0'});
  await nextTurn(); assert.equal(page.workers[0].request.engine.id, 'kept');
  page.workers[0].reply(validResult()); await pending;
}));

for (const withdrawn of [false, true]) {
  test((withdrawn ? 'withdrawn' : 'missing') + ' explicit release never falls back', () => withPage(async page => {
    page.ready({default: 'default', engines: [{id: 'default'}, ...(withdrawn ? [{id: 'requested', withdrawn: true}] : [])]});
    const result = await page.api.render('record', {release: 'requested'});
    assert.match(result.verdict, /mismatch/); assert.equal(page.workers.length, 0);
    assert.equal(result.verified, false); assert.equal(page.get('share').disabled, true);
  }));
}

test('release query selects a trusted build for bare pasted TL', () => withPage(async page => {
  page.ready({default: 'default', engines: [{id: 'default'}, {id: 'requested'}]});
  const pending = page.api.render('record'); await nextTurn();
  assert.equal(page.workers[0].request.engine.id, 'requested');
  page.workers[0].reply(validResult()); await pending;
}, {href: 'https://player.invalid/preview/?release=requested'}));

test('prepareLink renders a named service record silently and preserves its expected identity', () => withPage(async page => {
  assert(page.api.describe().operations.includes('prepareLink'));
  page.ready({default: 'other', engines: [{id: 'other'}, {id: 'test'}]});
  const pending = page.api.prepareLink('https://player.invalid/preview/r/' + 'a'.repeat(64));
  const result = await prepared(page, pending, 'service record');
  assert.equal(page.workers.at(-1).request.engine.id, 'test');
  assert.equal(result.mode, 'listen'); assert.equal(page.get('status').textContent, 'Ready. Press Play to listen.');
  assert.equal(page.get('play').disabled, false); assert.equal(page.audioCreations(), 0);
  const mismatch = page.api.prepareLink('https://player.invalid/preview/r/' + 'a'.repeat(64));
  await nextTurn();
  page.workers.at(-1).reply(validResult({recordSha256: 'b'.repeat(64)}));
  assert.match((await mismatch).verdict, /record identity differs/);
  invalidActions(page);
}, {network: {
  ['/preview/api/records/' + 'a'.repeat(64)]: {record_sha256: 'a'.repeat(64), renderer_release: 'test', record_url: 'r/song.tl'},
  '/preview/r/song.tl': 'service record',
}}));

test('pending and failed hosted descriptors stop before protected raw downloads with clear safe status', async () => {
  const sha='a'.repeat(64),url='https://player.invalid/preview/r/'+sha;
  for (const [verification,expected] of [
    [{status:'pending'},/waiting for independent technical verification/],
    [{status:'failed',failure:'silent-render'},/completely silent \(silent-render\)/],
    [{status:'failed',failure:'reader-refused'},/native reader refused/],
    [{status:'failed',failure:'private native error secret'},/failed independent technical verification\. Its share link/],
  ]) await withPage(async page => {
    page.get('record').value='my current draft';
    await assert.rejects(page.api.loadLink(url),expected);
    assert.match(page.get('status').textContent,expected);
    assert(!page.get('status').textContent.includes('private native error secret'));
    assert.equal(page.get('record').value,'my current draft');
    assert.equal(page.workers.length,0);assert.equal(page.audioCreations(),0);
    assert(!page.requests.some(value=>value.endsWith('/preview/r/song.tl')));
  },{network:{['/preview/api/records/'+sha]:{record_sha256:sha,renderer_release:'test',record_url:'r/song.tl',
    verified:false,details_withheld:true,verification},'/preview/r/song.tl':()=>{throw Error('Protected raw bytes must not be requested');}}});
  const failed=Object.assign(Error('private raw error'),{verificationStatus:'failed',verificationFailure:'unknown-internal-error'});
  assert.doesNotMatch(linkProblem(failed),/private|unknown-internal/);
});

test('verified hosted links remain playable while approval and advisory warnings affect no direct download',()=>withPage(async page=>{
  const loaded=await page.api.loadLink('https://player.invalid/preview/r/'+'a'.repeat(64));
  assert.equal(loaded.recordText,'verified minimal song');
  assert(page.requests.some(url=>url.endsWith('/preview/r/song.tl')));
},{network:{['/preview/api/records/'+'a'.repeat(64)]:{record_sha256:'a'.repeat(64),renderer_release:'test',record_url:'r/song.tl',
  verified:true,listed:false,review:{required:true,status:'pending'},verification:{status:'verified',review_warnings:['low-note-count','clipping']}},
  '/preview/r/song.tl':'verified minimal song'}}));

test('an unknown shared song clears an unrelated library selection while leaving Play available',()=>withPage(async page=>{
  assert.notEqual(selectedSongId(page),'');
  await prepared(page,page.api.prepareLink('https://player.invalid/preview/r/'+'a'.repeat(64)),'service record');
  assert.equal(page.get('play').disabled,false);assert.equal(page.get('song-load').disabled,true);
  assert.equal(page.get('rail-load').disabled,true);assert.equal(selectedSongId(page),'');
  assert.equal(page.get('rail-selected-song').textContent,'No song selected');
},{catalog:curatedCatalog(),network:{['/preview/api/records/'+'a'.repeat(64)]:{record_sha256:'a'.repeat(64),renderer_release:'test',record_url:'r/song.tl'},'/preview/r/song.tl':'service record'}}));

test('denied guide copying exposes a direct manual guide link instead of rejecting silently',()=>withPage(async page=>{
  await page.get('copy-guide').click();
  assert.equal(page.get('guide-manual').hidden,false);assert.equal(page.copied.length,0);
  assert.match(page.get('status').textContent,/Clipboard access.*manually/);
},{clipboardDenied:true,network:{'/web/browser-guide.txt':'Browser guide text'}}));

test('a late prepareLink fetch cannot overwrite a newer edit or render it', async () => {
  const response = deferred();
  await withPage(async page => {
    const pending = page.api.prepareLink('https://player.invalid/song.tl');
    const rejected = assert.rejects(pending, /Loading cancelled/);
    await nextTurn();
    page.get('record').value = 'new draft'; page.get('record').dispatch('input');
    response.resolve(new Response('old service record')); await rejected;
    assert.equal(page.get('record').value, 'new draft');
    assert.equal(page.workers.length, 0); assert.equal(page.api.result(), null);
    assert.equal(page.audioCreations(), 0);
  }, {network: {'/song.tl': () => response.promise}});
});

test('a profile checks a linked source identity separately from its derived identity', () => withPage(async page => {
  page.ready();
  await prepared(page, page.api.prepareLink('https://player.invalid/preview/r/' + 'a'.repeat(64)), 'linked source');
  page.get('listening-profile').value = '60';
  let pending = page.get('profile-apply').click(); await nextTurn();
  page.workers.at(-1).reply(validResult({canonical:new TextEncoder().encode('computed linked song'),
    sourceRecordSha256:'a'.repeat(64), recordSha256:'b'.repeat(64)})); await pending;
  assert.equal(page.api.result().verdict, 'valid');
  assert.equal(page.api.result().recordSha256, 'b'.repeat(64));
  assert.equal(page.get('tl-download').disabled, false);
  page.get('listening-profile').value = '75';
  pending = page.get('profile-apply').click(); await nextTurn();
  page.workers.at(-1).reply(validResult({canonical:new TextEncoder().encode('wrong-source derivation'),
    sourceRecordSha256:'c'.repeat(64), recordSha256:'a'.repeat(64)})); await pending;
  assert.match(page.api.result().verdict, /record identity differs/);
  assert.equal(page.api.result().canonical.length, 0);
  assert.equal(page.get('tl-download').disabled, true);
}, {network:{
  ['/preview/api/records/' + 'a'.repeat(64)]:{record_sha256:'a'.repeat(64),renderer_release:'test',record_url:'r/song.tl'},
  '/preview/r/song.tl':'linked source',
}}));

test('prepareLink rejects cancelled rendering without leaving playable stale audio', () => withPage(async page => {
  page.ready();
  const pending = page.api.prepareLink('https://player.invalid/song.tl');
  const rejected = assert.rejects(pending, /Rendering cancelled/);
  await nextTurn(); const worker = page.workers.at(-1);
  page.get('cancel').click(); await rejected;
  assert.equal(worker.terminated, true); assert.equal(page.api.result(), null);
  invalidActions(page);
}, {network: {'/song.tl': 'service record'}}));

test('service links preserve prefix and expected canonical identity', () => withPage(async page => {
  const loaded = await page.api.loadLink('https://player.invalid/preview/r/' + 'a'.repeat(64));
  assert.equal(loaded.release, 'test'); assert.equal(loaded.recordText, 'service record');
  assert.equal(page.workers.length, 0, 'The programmatic loadLink API remains load-only');
  assert.equal(page.audioCreations(), 0);
  page.ready(); const pending = page.api.render(page.get('record').value); await nextTurn();
  assert.equal(page.workers[0].request.engine.id, 'test');
  page.workers[0].reply(validResult({recordSha256: 'b'.repeat(64)}));
  const result = await pending;
  assert.match(result.verdict, /record identity differs/); assert.equal(result.canonical.length, 0);
  assert.equal(page.get('share').disabled, true);
}, {network: {
  ['/preview/api/records/' + 'a'.repeat(64)]: {record_sha256: 'a'.repeat(64), renderer_release: 'test', record_url: 'r/song.tl'},
  '/preview/r/song.tl': 'service record',
}}));

test('submission descriptor cannot override requested release or escape prefix', () => withPage(async () => {
  await assert.rejects(resolveLink('https://player.invalid/preview/s/one?release=other'), /requested renderer release/);
  await assert.rejects(resolveLink('https://player.invalid/preview/s/escape'), /path prefix/);
}, {network: {
  '/preview/api/submissions/one?release=other': {record_sha256: 'a'.repeat(64), renderer_release: 'test', record_url: 'r/song.tl'},
  '/preview/api/submissions/escape': {record_sha256: 'a'.repeat(64), renderer_release: 'test', record_url: '/outside/song.tl'},
}}));

test('legacy deflate fragments round-trip without metadata or service requests', async () => {
  const text = 'timeline 1\n' + 'complete text fixture\n'.repeat(4);
  const link = await portableLink(new TextEncoder().encode(text), 'https://player.invalid/preview/', 'release-one');
  assert.match(link, /^https:\/\/player.invalid\/preview\/\?release=release-one#v1\./);
  const loaded = await resolveLink(link);
  assert.equal(loaded.recordText, text); assert.equal(loaded.release, 'release-one');
});

test('content-addressed static records retain their expected identity', () => withPage(async page => {
  const sha='a'.repeat(64),url='https://static.invalid/community/records/'+sha+'.tl';
  assert(isRecordLink(new URL(url)));
  const loaded=await resolveLink(url);
  assert.equal(loaded.recordText,'static complete song');assert.equal(loaded.recordSha256,sha);
  assert(page.requests.includes(url));
}, {network:{['/community/records/'+'a'.repeat(64)+'.tl']:'static complete song'}}));

test('compact library links preserve exact saved identity and renderer without embedding song text', () => withPage(async page => {
  const sha = 'a'.repeat(64), base = 'https://player.invalid/preview/';
  const url = libraryLink('songs/one-keys.tl', sha, base, 'release-one');
  assert(isRecordLink(new URL(url))); assert(url.length < 250);
  const loaded = await resolveLink(url);
  assert.equal(loaded.recordText, 'one complete song'); assert.equal(loaded.recordSha256, sha);
  assert.equal(loaded.release, 'release-one');
  assert(page.requests.includes(base + 'songs/one-keys.tl'));
  for (const path of ['../private.tl','songs/../private.tl','https://outside.test/a.tl','songs/a.tl?secret=x','songs/a.tl#part'])
    assert.throws(() => libraryLink(path,sha,base), /saved library/);
  await assert.rejects(resolveLink(url + '&sha=' + sha), /Invalid library/);
  await assert.rejects(resolveLink(base + '#song=songs%2Fone-keys.tl&sha=no'), /saved library/);
}, {network:{'/preview/songs/one-keys.tl':'one complete song'}}));

test('compact library metadata hints are bounded paths, never caller-provided display names', () => withPage(async page => {
  const base='https://player.invalid/web/',sha='a'.repeat(64);
  const url=libraryLink('songs/one.tl',sha,base,'test','catalog/all-2.json');
  const value=await resolveLink(url);
  assert.equal(value.libraryPath,'songs/one.tl');assert.equal(value.catalogPage,'catalog/all-2.json');
  assert.equal(value.recordSha256,sha);assert.equal(value.release,'test');
  const legacy=await resolveLink(libraryLink('songs/one.tl',sha,base));assert.equal(legacy.catalogPage,undefined);
  for (const hint of ['https://outside.invalid/catalog/all-1.json','catalog/../private.json','catalog/all-0.json','catalog/all-1.json?title=Fake']) {
    assert.throws(()=>libraryLink('songs/one.tl',sha,base,'test',hint),/catalog page hint/);
    const loaded=await resolveLink(libraryLink('songs/one.tl',sha,base)+'&catalog='+encodeURIComponent(hint));
    assert.equal(loaded.catalogPage,undefined);assert.equal(loaded.recordText,'linked song');
  }
  await assert.rejects(resolveLink(url+'&title=Forged'),/Invalid library/);
  await assert.rejects(resolveLink(url+'&catalog=catalog%2Fall-3.json'),/Invalid library/);
  assert(!page.requests.some(url=>url.includes('outside.invalid')));
}, {network:{'/web/songs/one.tl':'linked song'}}));

function coldLinkCatalog() {
  const catalog=largeCatalog(34),first=catalog.songs[0],target=catalog.songs[32];
  first.title='Preview song';first.variants[0].record_sha256='b'.repeat(64);
  first.variants[0].discovery={record_sha256:'b'.repeat(64),source:'music/preview.tl',featured:{order:1,note:'Start here.'}};
  target.title='Beyond Featured';target.variants[0].record_sha256='c'.repeat(64);
  target.variants.push({kind:'mirror',path:'songs/beyond-mirror.tl',duration_seconds:30,bytes:30,record_sha256:'a'.repeat(64)});
  return catalog;
}
const coldLinkURL = () => libraryLink('songs/beyond-mirror.tl','a'.repeat(64),'https://player.invalid/web/','test','catalog/all-2.json');
test('a cold compact link outside Featured restores exact saved title/version without autoplay', () => withPage(async page=>{
  const result=await prepared(page,undefined,'mirror song');
  assert.equal(result.recordSha256,'a'.repeat(64));assert.equal(result.pcmHash,'0123456789abcdef');
  assert.equal(page.get('loaded-song').textContent,'Beyond Featured');
  assert.equal(page.get('loaded-version').textContent,'Keys mirror · saved ±30°');
  assert.equal(page.get('selected-song-title').textContent,'Beyond Featured','The incoming song is surfaced as the selection');
  assert.equal(page.get('rail-selected-song').textContent,'Beyond Featured');
  assert.equal(page.get('song-variant').value,'mirror');
  assert.equal(songRows(page).length,1);
  assert.equal(page.get('play').textContent,'Play');assert.equal(page.audioCreations(),0);
  assert.deepEqual([...new Set(page.requests.filter(url=>url.includes('/catalog/')).map(url=>new URL(url).pathname))].sort(),
    ['/web/catalog/all-2.json','/web/catalog/featured-1.json','/web/catalog/index.json']);
  await page.get('share').click();assert.match(page.copied.at(-1),/&catalog=catalog%2Fall-2.json$/);
}, {href:coldLinkURL(),catalog:coldLinkCatalog(),network:{'/web/songs/beyond-mirror.tl':'mirror song'}}));

test('old two-field links recover metadata when the initial catalog page arrives after valid audio', async()=>{
  const catalog=curatedCatalog(),documents=catalogPages(catalog),pending=deferred();
  const href=libraryLink('songs/first.tl','a'.repeat(64),'https://player.invalid/web/','test');
  await withPage(async page=>{
    await prepared(page,undefined,'old link');assert.equal(page.get('loaded-song').textContent,'Linked song');
    pending.resolve(new Response(JSON.stringify(documents['catalog/featured-1.json'])));await nextTurn();
    assert.equal(page.get('loaded-song').textContent,'<First song>');assert.equal(page.get('loaded-version').textContent,'Original · saved');
    assert.equal(page.get('play').disabled,false);assert.equal(page.audioCreations(),0);
    assert.equal(page.requests.filter(url=>url.includes('/catalog/')).length,2,'Legacy recovery does not scan the library');
  },{href,catalog,network:{'/web/songs/first.tl':'old link','/web/catalog/featured-1.json':()=>pending.promise}});
});

test('missing or mismatched hinted metadata never blocks valid audio or attaches another version label', async()=>{
  for (const failure of ['unavailable','path','sha']) {
    const catalog=coldLinkCatalog(),documents=catalogPages(catalog);
    const response=documents['catalog/all-2.json'];
    // A renamed path keeps its label only when the verified hash names one song (UI-1);
    // a second song with that hash leaves the link unnamed.
    if (failure==='path') {response.items[8].variants[1].path='songs/another-mirror.tl';
      response.items[7].variants.push({kind:'mirror',path:'songs/other-mirror.tl',duration_seconds:30,bytes:30,record_sha256:'a'.repeat(64)});}
    if (failure==='sha') response.items[8].variants[1].record_sha256='d'.repeat(64);
    await withPage(async page=>{
      await prepared(page,undefined,'mirror song');assert.equal(page.api.result().verdict,'valid');
      assert.equal(page.get('play').disabled,false);assert.equal(page.get('loaded-song').textContent,'Linked song');
      assert.equal(page.get('loaded-version').textContent,'Linked saved version');assert.equal(page.audioCreations(),0);
      assert.equal(selectedSongId(page),'');assert.equal(page.get('song-load').disabled,true);assert.equal(page.get('rail-load').disabled,true);
    },{href:coldLinkURL(),catalog,network:{'/web/songs/beyond-mirror.tl':'mirror song',
      '/web/catalog/all-2.json':()=>new Response(JSON.stringify(response),{status:failure==='unavailable'?503:200})}});
  }
});

test('late full-file checksum mismatch cannot attach catalog labels to valid body-ID link playback',async()=>{
  const body='timeline draft1\n',saved=composeDocument(body,{title:'Expected metadata'}),received=composeDocument(body,{title:'Changed metadata'});
  assert.equal(await bodySha256(saved),await bodySha256(received));
  const catalog=coldLinkCatalog(),target=catalog.songs[32].variants[1],documents=catalogPages(catalog),pending=deferred();
  target.document_sha256=await documentSha256(saved);
  target.discovery={record_sha256:target.record_sha256,document_sha256:target.document_sha256,source:'library.json'};
  const response=catalogPages(catalog)['catalog/all-2.json'];
  await withPage(async page=>{
    await prepared(page,undefined,received);assert.equal(page.get('loaded-song').textContent,'Linked song');
    pending.resolve(new Response(JSON.stringify(response)));
    for(let i=0;i<20;i++)await nextTurn();
    assert.equal(page.get('loaded-song').textContent,'Linked song');
    assert.equal(page.get('loaded-version').textContent,'Linked saved version');
    assert.equal(page.get('play').disabled,false);assert.equal(page.api.result().verdict,'valid');
    assert.equal(selectedSongId(page),'');assert.equal(page.audioCreations(),0);
  },{href:coldLinkURL(),catalog,network:{'/web/songs/beyond-mirror.tl':received,'/web/catalog/all-2.json':()=>pending.promise}});
});

test('a renamed library path with a unique verified hash selects its catalogue song', async()=>{
  const catalog=coldLinkCatalog(),response=catalogPages(catalog)['catalog/all-2.json'];
  response.items[8].variants[1].path='songs/another-mirror.tl';
  await withPage(async page=>{
    await prepared(page,undefined,'mirror song');assert.equal(page.api.result().verdict,'valid');
    assert.equal(page.get('loaded-song').textContent,'Beyond Featured');
    assert.equal(page.get('selected-song-title').textContent,'Beyond Featured');
    assert.equal(page.get('song-variant').value,'mirror');
  },{href:coldLinkURL(),catalog,network:{'/web/songs/beyond-mirror.tl':'mirror song',
    '/web/catalog/all-2.json':()=>new Response(JSON.stringify(response))}});
});

test('late hinted metadata is aborted and cannot rename a replacement local song', async()=>{
  const catalog=coldLinkCatalog(),documents=catalogPages(catalog),pending=deferred();let signal;
  await withPage(async page=>{
    await prepared(page,undefined,'mirror song');assert.equal(page.get('loaded-song').textContent,'Linked song');
    page.get('file').files=[{name:'Replacement.tl',size:10,text:async()=>'replacement'}];
    await prepared(page,page.get('file').onchange(),'replacement',validResult({recordSha256:'f'.repeat(64)}));
    assert.equal(signal.aborted,true);
    pending.resolve(new Response(JSON.stringify(documents['catalog/all-2.json'])));await nextTurn();
    assert.equal(page.get('loaded-song').textContent,'Replacement');assert.equal(page.get('loaded-version').textContent,'Local file · as saved');
    assert.equal(page.get('record').value,'replacement');assert.equal(page.audioCreations(),0);
  },{href:coldLinkURL(),catalog,network:{'/web/songs/beyond-mirror.tl':'mirror song','/web/catalog/all-2.json':options=>{signal=options.signal;return pending.promise;}}});
});

test('importing known canonical bytes restores the catalog title without trusting the filename',()=>withPage(async page=>{
  page.get('file').files=[{name:'a'.repeat(64)+'.tl',size:10,text:async()=>'imported complete record'}];
  await prepared(page,page.get('file').onchange(),'imported complete record');
  assert.equal(page.get('loaded-song').textContent,'<First song>');
  assert.equal(document.title,'<First song> · JotTune Synth');
  assert.equal(page.audioCreations(),0);
},{catalog:curatedCatalog()}));

test('auto move leaves a visible transport focused so a second activation pauses',()=>withPage(async page=>{
  await complete(page,'keyboard song',transportResult());page.get('play').focus();
  await page.get('play').click();
  assert.equal(document.activeElement,page.get('rail-play'));
  assert.equal(page.get('rail-play').textContent,'Pause');
  page.advance(1);await page.get('rail-play').click();
  assert.equal(page.get('play').textContent,'Resume');
}));

test('sharing a saved library version uses its short link, while a modified result stays portable', () => withPage(async page => {
  await complete(page, 'library song', validResult({recordSha256:'a'.repeat(64)}));
  await page.get('share').click();
  assert.match(page.copied.at(-1), /#song=songs%2Ffirst-standard.tl&sha=a{64}&catalog=catalog%2Fall-1.json$/);
  await complete(page, 'changed song', validResult({recordSha256:'f'.repeat(64)}));
  await page.get('share').click();
  assert.match(page.copied.at(-1), /#v1\./);
}, {catalog:{version:1,songs:[{id:'one',title:'One',collection:'Local',variants:[{
  kind:'standard',path:'songs/first-standard.tl',duration_seconds:20,bytes:20,record_sha256:'a'.repeat(64)}]}]}}));

test('metadata-only edits export portable links that reopen the complete edited document',async()=>{
  const body='timeline draft1\n', published=composeDocument(body,{title:'Published version'});
  const edited=composeDocument(body,{title:'Edited title 🎵'}),recordSha256=await bodySha256(published);
  const catalog={version:1,songs:[{id:'one',title:'Published version',collection:'Local',variants:[{
    kind:'standard',path:'songs/first-standard.tl',duration_seconds:20,bytes:Buffer.byteLength(published),
    record_sha256:recordSha256,document_sha256:await documentSha256(published)}]}]};
  await withPage(async page=>{
    for(const [text,fragment] of [[published,'#song='],[edited,'#v1.']]) {
      const result=await complete(page,text,validResult({rendererRelease:'test',canonical:new TextEncoder().encode(text),
        recordSha256,documentSha256:await documentSha256(text)}));
      const url=await page.api.toLink(result),part=await page.api.exportChunk(result,{format:'link'});
      assert(new URL(url).hash.startsWith(fragment));assert.equal(part.nextOffset,null);
      assert.equal(Buffer.from(part.base64,'base64').toString('utf8'),url);
      const copies = page.copied.length;
      await page.get('link-download').click();
      const file = page.downloaded.at(-1);
      assert.equal(file.type, 'text/plain;charset=utf-8');
      assert.deepEqual(Buffer.from(await file.arrayBuffer()), Buffer.from(url + '\n'));
      assert.match(page.filenames.at(-1), /\.link\.txt$/);
      assert.equal(page.copied.length, copies);
      assert.equal(page.audioCreations(), 0);
      await page.get('share').click();assert.equal(page.copied.at(-1),url);
      const reopened=await resolveLink(url,document.baseURI);
      assert.equal(reopened.recordText,text);assert.equal(await documentSha256(reopened.recordText),result.documentSha256);
    }
  },{catalog,network:{'/web/songs/first-standard.tl':published}});
});

test('touch devices share the song link through the native sheet, and the clipboard stays the fallback', async () => {
  const catalog = {version:1,songs:[{id:'one',title:'One',collection:'Local',variants:[{
    kind:'standard',path:'songs/first-standard.tl',duration_seconds:20,bytes:20,record_sha256:'a'.repeat(64)}]}]};
  const shared = []; let failure = null;
  const share = {share: async data => { shared.push(data); if (failure) throw failure; }, canShare: data => !data.url.includes('#v1.')};
  await withPage(async page => {
    assert.equal(page.get('share').textContent, 'Share song link');
    await complete(page, 'library song', validResult({recordSha256:'a'.repeat(64)}));
    await page.get('share').click();
    assert.equal(shared.length, 1); assert.equal(shared[0].title, 'One · Original · saved');
    assert.match(shared[0].url, /#song=songs%2Ffirst-standard.tl&sha=a{64}&catalog=catalog%2Fall-1.json$/);
    assert.equal(page.copied.length, 0);
    assert.equal(page.get('status').textContent, 'Song link shared for this exact saved version. Nothing was published.');
    await page.get('link-download').click();
    assert.equal(shared.length, 1, 'Saving a URL file on touch devices leaves the share sheet closed');
    assert.equal(page.copied.length, 0);
    assert.equal(await page.downloaded.at(-1).text(), shared[0].url + '\n');
    failure = Object.assign(Error('Share canceled'), {name:'AbortError'});
    await page.get('share').click();
    assert.equal(page.copied.length, 0, 'Closing the sheet copies nothing');
    assert.equal(page.get('status').textContent, 'Sharing cancelled. Nothing was published.');
    failure = Object.assign(Error('Share refused'), {name:'NotAllowedError'});
    await page.get('share').click();
    assert.equal(page.copied.length, 1, 'Other share failures fall back to the clipboard');
    assert.equal(page.get('status').textContent, 'Song link copied for this exact saved version. Nothing was published.');
    await complete(page, 'changed song', validResult({recordSha256:'f'.repeat(64)}));
    await page.get('share').click();
    assert.equal(shared.length, 3, 'A link the sheet cannot carry is not offered to it');
    assert.match(page.copied.at(-1), /#v1\./);
    assert.equal(page.get('status').textContent, 'Portable record link copied. Nothing was published.');
  }, {coarse:true, share, catalog});
  await withPage(async page => {
    assert.notEqual(page.get('share').textContent, 'Share song link');
    await complete(page, 'library song', validResult({recordSha256:'a'.repeat(64)}));
    await page.get('share').click();
    assert.equal(shared.length, 3, 'Mouse and trackpad pointers keep the clipboard');
    assert.match(page.copied.at(-1), /#song=songs%2Ffirst-standard.tl&sha=a{64}/);
  }, {coarse:false, share, catalog});
});

test('Media Session mirrors the loaded song, and its actions use the same transport', async () => {
  const handlers = {}, session = {metadata:null, playbackState:'none', setActionHandler(action, handler) { handlers[action] = handler; }};
  await withPage(async page => {
    assert.deepEqual(Object.keys(handlers).sort(), ['pause', 'play', 'stop']);
    await prepared(page, page.get('song-load-right').click(), 'first record', transportResult());
    assert.equal(session.metadata.title, '<First song>'); assert.equal(session.metadata.album, 'JotTune Synth');
    assert.equal(session.playbackState, 'paused');
    const metadata = session.metadata; page.advance(1);
    assert.equal(session.metadata, metadata, 'Periodic updates keep the same metadata');
    handlers.play(); await nextTurn();
    assert.equal(page.audioSources.length, 1); assert.equal(page.get('play').textContent, 'Pause');
    assert.equal(session.playbackState, 'playing');
    handlers.play(); await nextTurn();
    assert.equal(page.audioSources.length, 1, 'Play while playing starts nothing');
    page.advance(1); handlers.pause();
    assert.equal(page.get('play').textContent, 'Resume'); assert.equal(session.playbackState, 'paused');
    handlers.pause(); assert.equal(page.get('play').textContent, 'Resume', 'Pause while paused does nothing');
    handlers.stop(); assert.equal(page.get('play').textContent, 'Play');
  }, {mediaSession:session, autoMove:false, catalog:songCatalog, network:{'/web/songs/first-keys.tl':'first record'}});
  const partial = {}, limited = {setActionHandler(action, handler) { if (action === 'stop') throw new TypeError('Unsupported'); partial[action] = handler; }};
  await withPage(async page => {
    assert.deepEqual(Object.keys(partial).sort(), ['pause', 'play'], 'An unsupported action leaves the others in place');
    await complete(page, 'programmatic', transportResult());
    assert.equal(limited.metadata.title, 'Local song'); assert.equal(limited.playbackState, 'paused');
    await page.get('play').click(); assert.equal(limited.playbackState, 'playing');
  }, {mediaSession:limited});
});

test('browser profile bounds UTF-8 bytes and native-inspected duration', () => {
  checkRecord(('x'.repeat(999)+'\n').repeat(limits.recordBytes/1000));
  assert.throws(() => checkRecord('x'.repeat(limits.recordBytes + 1)), /1 MB/);
  assert.throws(() => checkRecord('€'.repeat(400000)), /1 MB/);
  const info = {length_ns: '300000000000', pcm_bytes: '1000', render_bytes_lower_bound: '3000'};
  checkInspection(info);
  assert.throws(() => checkInspection({...info, length_ns: '300000000001'}), /300-second/);
  const large = {...info, pcm_bytes: String(16 * 1024 * 1024), render_bytes_lower_bound: String(240 * 1024 * 1024), once_render_bytes_lower_bound: String(120 * 1024 * 1024)};
  assert.throws(() => checkInspection(large, 'verify'), /Too large/);
  checkInspection(large, 'listen');
});

test('registry refuses duplicate release identifiers and dated withdrawals', () => {
  assert.throws(() => selectEngine({default: 'x', engines: [{id: 'a', release_id: 'x'}, {id: 'b', release_id: 'x'}]}), /not available/);
  assert.throws(() => selectEngine({default: 'x', engines: [{id: 'x', withdrawn_at: '2026-10-25'}]}), /withdrawn/);
});

test('spatial SVG draws native targets and escapes labels without implying an audio trajectory', () => {
  const svg = toSvg(validResult({events: [
    {kind: 'on', voice: '0', stream: 'a', patch: '<script>', time_ns: '0', freq_millihz: '440000', velocity: '16384'},
    {kind: 'pos', voice: '0', time_ns: '0', duration_ns: '0', x: '-1000', y: '0', z: '250'},
    {kind: 'move', voice: '0', time_ns: '100', duration_ns: '500000000', x: '1000', y: '0', z: '500'},
    {kind: 'off', voice: '0', time_ns: '900000000'},
  ]}));
  assert.match(svg, /Spatial command targets/); assert.match(svg, /not interpolated motion/);
  assert.match(svg, /x=-1000 y=0 z=250 mm/); assert.match(svg, /duration 500000000 ns/);
  assert.match(svg, /&lt;script&gt;/); assert.doesNotMatch(svg, /<script>/);
  assert.equal((svg.match(/<circle /g) || []).length, 2);
});

test('free prompt hashes cannot enter WAV provenance', () => {
  assert.throws(() => provenance({prompt_sha256: 'a'.repeat(64)}), /Unsupported provenance key/);
  assert.throws(() => toWav(validResult(), {prompt_sha256: 'a'.repeat(64)}), /Unsupported provenance key/);
  assert.equal(provenance({prompt_id: 'suite-v1-01'}).prompt_id, 'suite-v1-01');
});

test('service worker revalidates the player, retains offline snapshots and isolates archived engines', async () => {
  const handlers = new Map(), stores = new Map(), removed = [], fetched = [], cacheModes = [];
  const ownOld = 'intfm-workbench-%2Fpreview-a%2F-old';
  const other = 'intfm-workbench-%2Fpreview-b%2F-old';
  stores.set(ownOld, new Map()); stores.set(other, new Map());
  let offline = false, writable = true, storageAvailable = true;
  const caches = {
    keys: async () => [...stores.keys()],
    delete: async name => { removed.push(name); return stores.delete(name); },
    open: async name => {
      if (!storageAvailable) throw Error('Storage unavailable');
      if (!stores.has(name)) stores.set(name, new Map());
      const entries = stores.get(name);
      return {match: async (request, options) => {
          const key = new URL(request.url);
          if (options?.ignoreSearch) key.search = '';
          return entries.get(key.href)?.clone();
        },
        put: async (request, response) => {if(!writable)throw Error('Quota exceeded'); entries.set(request.url, response.clone()); }};
    },
  };
  vm.runInNewContext(readFileSync(new URL('../web/sw.template.js', import.meta.url), 'utf8'), {
    URL, Request, Response, caches,
    self: {location: new URL('https://player.invalid/preview-a/sw.js'),
      addEventListener: (name, listener) => handlers.set(name, listener), clients: {claim: async () => {}}},
    fetch: async (request, init) => {
      if (offline) throw Error('offline');
      cacheModes.push(init?.cache ?? request.cache);
      if (new URL(request.url).pathname.endsWith('/removed.txt')) return new Response('removed', {status:410});
      fetched.push(request.url); return new Response('revision-' + fetched.length);
    },
  });
  let activating;
  handlers.get('activate')({waitUntil: value => { activating = value; }}); await activating;
  assert.deepEqual(removed, [ownOld]); assert(stores.has(other));
  const request = (path, mode) => {
    let pending;
    const input = new Request('https://player.invalid' + path);
    if (mode) Object.defineProperty(input, 'mode', {value:mode});
    handlers.get('fetch')({request:input, respondWith: value => { pending = value; }});
    return pending;
  };
  for (const path of ['/sw.js', '/preview-b/engine.wasm', '/preview-a/api/records/id', '/preview-a/r/song.tl', '/preview-a/s/id'])
    assert.equal(request(path), undefined, path + ' must bypass this app cache');
  const enginePath = '/preview-a/engines/kept/vendor/browser_wasi_shim/index.js';
  assert.equal(await (await request(enginePath)).text(), 'revision-1');
  assert.equal(await (await request(enginePath)).text(), 'revision-1');
  assert.equal(fetched.length, 1, 'Archived runtime dependencies should work from the engine cache');
  assert.equal(await (await request('/preview-a/engines.json')).text(), 'revision-2');
  assert.equal(await (await request('/preview-a/engines.json')).text(), 'revision-3');
  offline = true;
  assert.equal(await (await request('/preview-a/engines.json')).text(), 'revision-3');
  for(const path of ['/preview-a/catalog/index.json','/preview-a/songs/not-cached.tl','/preview-a/engines/not-cached/browser.wasm']) {
    const missing=await request(path);assert.equal(missing.status,503);assert.match(await missing.text(),/Not available offline/);
    assert.equal(missing.headers.get('Cache-Control'),'no-store');
  }
  offline = false;writable = false;
  assert.equal(await (await request('/preview-a/engines/uncached-when-full/browser.wasm')).text(),'revision-4');
  storageAvailable = false;
  assert.equal(await (await request('/preview-a/engines/storage-denied/browser.wasm')).text(),'revision-5');

  // Deploy B while build A's worker and complete offline snapshot still control the page.
  writable = true; storageAvailable = true;
  const shell = await caches.open('intfm-workbench-%2Fpreview-a%2F-development');
  for (const name of ['', 'app.mjs', 'reference.txt', 'removed.txt'])
    await shell.put(new Request('https://player.invalid/preview-a/' + name), new Response('build A ' + name));
  const fresh = await request('/preview-a/?release=pin', 'navigate');
  assert.equal(await fresh.text(), 'revision-6');
  assert.equal(await (await request('/preview-a/app.mjs')).text(), 'revision-7');
  assert.equal(await (await request('/preview-a/reference.txt')).text(), 'revision-8');
  assert.deepEqual(cacheModes.slice(-3), ['no-cache','no-cache','no-cache']);
  assert.equal((await request('/preview-a/removed.txt')).status, 410);
  offline = true;
  assert.equal(await (await request('/preview-a/?release=pin', 'navigate')).text(), 'build A ');
  assert.equal(await (await request('/preview-a/app.mjs')).text(), 'build A app.mjs');
  assert.equal(await (await request('/preview-a/reference.txt')).text(), 'build A reference.txt');
});

function wavChunks(bytes, start = 12, end = bytes.length) {
  const chunks = new Map();
  for (let offset = start; offset < end;) {
    const id = bytes.toString('ascii', offset, offset + 4), size = bytes.readUInt32LE(offset + 4);
    assert(offset + 8 + size <= end, 'RIFF chunk exceeds its container');
    chunks.set(id, bytes.subarray(offset + 8, offset + 8 + size));
    offset += 8 + size + size % 2;
  }
  return chunks;
}
test('WAV INFO preserves exact PCM and adds no default licence', async () => {
  const pcm = new Int16Array([-32768, -257, -1, 0, 1, 255, 32767, 1234]);
  const before = pcm.slice(), expected = Buffer.alloc(pcm.length * 2);
  pcm.forEach((value, index) => expected.writeInt16LE(value, index * 2));
  const result = validResult({pcm});
  for (const metadata of [{}, {licence: 'CC-BY-4.0', utc_day: '2026-10-25'}]) {
    const bytes = Buffer.from(await toWav(result, metadata).arrayBuffer());
    assert.equal(bytes.toString('ascii', 0, 4), 'RIFF');
    assert.equal(bytes.readUInt32LE(4), bytes.length - 8);
    assert.equal(bytes.toString('ascii', 8, 12), 'WAVE');
    const chunks = wavChunks(bytes);
    assert.deepEqual(chunks.get('data'), expected);
    assert.equal(chunks.get('fmt ').readUInt16LE(2), result.channels);
    assert.equal(chunks.get('fmt ').readUInt32LE(4), result.sampleRate);
    const list = chunks.get('LIST'); assert.equal(list.toString('ascii', 0, 4), 'INFO');
    const tags = wavChunks(list, 4), text = id => tags.get(id).toString('utf8').replace(/\0$/, '');
    const comment = JSON.parse(text('ICMT'));
    assert.equal(comment.record_sha256, result.recordSha256);
    assert.equal(comment.pcm_hash, result.pcmHash);
    assert.equal(text('ISFT'), 'JotTune Synth engine-test build=build-test');
    assert.equal(Object.hasOwn(comment, 'licence'), Object.hasOwn(metadata, 'licence'));
    assert.equal(comment.licence, metadata.licence);
    if (metadata.utc_day) assert.equal(text('ICRD'), metadata.utc_day);
  }
  assert.deepEqual(pcm, before);
});

test('first-channel spectrum retains antiphase stereo energy', async () => {
  const {analyzeSpectrogram} = await import('../web/media.mjs');
  const mono = new Int16Array(512), stereo = new Int16Array(1024);
  for (let i = 0; i < mono.length; i++) {
    mono[i] = Math.round(12000 * Math.sin(2 * Math.PI * 8 * i / mono.length));
    stereo[2 * i] = mono[i]; stereo[2 * i + 1] = -mono[i];
  }
  const before = stereo.slice();
  const left = analyzeSpectrogram(validResult({channels: 1, pcm: mono}));
  const antiphase = analyzeSpectrogram(validResult({channels: 2, pcm: stereo}));
  for (let i = 0; i < left.panels.length; i++) assert.deepEqual(antiphase.panels[i].dbfs, left.panels[i].dbfs);
  assert(antiphase.panels.some(panel => panel.peakDbfs > -40));
  assert.deepEqual(stereo, before);
});

test('compact result helpers preserve identity and exactly match UI sharing without audio arrays',()=>withPage(async page=>{
  const result=await complete(page,'authored fixture',validResult({rendererRelease:'test',metadata:{title:'A compact song'},canonical:new TextEncoder().encode('canonical fixture\n')}));
  const report=page.api.summary();assert.equal(report.recordSha256,result.recordSha256);
  assert.equal(report.title,'A compact song');assert.equal(report.canonicalBytes,result.canonical.byteLength);
  assert(!Object.hasOwn(report,'pcm'));assert(!Object.hasOwn(report,'events'));assert(!Object.hasOwn(report,'canonical'));
  assert.doesNotThrow(()=>JSON.stringify(report));assert.equal(page.api.toText(),new TextDecoder().decode(result.canonical));
  const link=await page.api.toLink();await page.get('share').click();assert.equal(link,page.copied.at(-1));
  assert.equal(new URL(link).searchParams.get('release'),'test');assert.equal(new URL(await page.api.toLink(result,{pinRelease:false})).searchParams.has('release'),false);
  await assert.rejects(page.api.toLink(result,{unknown:true}),/pinRelease/);
  assert.equal(page.audioCreations(),0);
}));
test('chunk exports preserve exact TL and URL bytes and bind each reply to the saved document',()=>withPage(async page=>{
  const digest=bytes=>createHash('sha256').update(bytes).digest('hex');
  const canonical=new TextEncoder().encode('a'.repeat(5999)+'🎵'+ ' text'.repeat(1400)+'\n');
  const checksum=digest(canonical), result=await complete(page,'export fixture',validResult({canonical,documentSha256:checksum,rendererRelease:'test'}));
  assert(page.api.describe().operations.includes('exportChunk'));
  assert(page.api.describe().operationDetails.exportChunk);
  const url=await page.api.toLink(result), requests=page.requests.length;
  for(const [format,expected] of [['tl',Buffer.from(canonical)],['link',Buffer.from(url)]]) {
    const parts=[];let offset=0;
    do {
      const reply=await page.api.exportChunk(result,{format,offset,bytes:format==='tl'?6000:53});
      const bytes=Buffer.from(reply.base64,'base64');parts.push(bytes);
      assert.equal(reply.format,format);assert.equal(reply.offset,offset);assert.equal(reply.totalBytes,expected.length);
      assert.equal(reply.encoding,'base64');assert.equal(reply.documentSha256,checksum);
      assert.equal(reply.payloadSha256,digest(expected));assert.equal(reply.chunkSha256,digest(bytes));
      const end=offset+bytes.length;assert.equal(reply.nextOffset,end===expected.length?null:end);
      offset=reply.nextOffset;
    } while(offset!==null);
    assert.deepEqual(Buffer.concat(parts),expected);
    assert.notEqual(digest(Buffer.concat([...parts].reverse())),digest(expected),'Swapped chunks must fail the whole-payload checksum');
    const final=await page.api.exportChunk(result,{format,offset:expected.length});
    assert.equal(final.base64,''); assert.equal(final.nextOffset,null);assert.equal(final.chunkSha256,digest(Buffer.alloc(0)));
    await assert.rejects(page.api.exportChunk(result,{format,offset:expected.length+1}),/exceeds/);
  }
  const first=await page.api.exportChunk();assert.equal(Buffer.from(first.base64,'base64').length,6000);
  const damaged=Buffer.from(first.base64,'base64');damaged[0]^=1;assert.notEqual(digest(damaged),first.chunkSha256);
  for(const opts of [null,[],{format:'wav'},{offset:-1},{offset:0.5},{offset:Number.MAX_SAFE_INTEGER+1},{bytes:0},{bytes:7201},{bytes:'2'},{unexpected:true}])
    await assert.rejects(page.api.exportChunk(result,opts));
  await assert.rejects(page.api.exportChunk({...result,verdict:'refused'}),/valid song/);
  await assert.rejects(page.api.exportChunk({...result,documentSha256:null}),/checksum/);
  const modified=new Uint8Array(canonical);modified[0]^=1;
  await assert.rejects(page.api.exportChunk({...result,canonical:modified}),/SHA-256 differs/);
  assert.equal(page.requests.length,requests);assert.equal(page.downloaded.length,0);assert.equal(page.copied.length,0);assert.equal(page.audioCreations(),0);
}));
test('phone score file import and pasted score compile and prepare silently with advisory report',()=>withPage(async page=>{
  page.ready({default:'test',engines:[{id:'test',identity:'int-fm-synth 1'}]});
  const score=JSON.parse(readFileSync(new URL('../web/starter.score.json',import.meta.url),'utf8'));
  score.metadata={title:'Phone composition',license:'CC0-1.0'};
  page.get('file').files=[{name:'composition.score.json',size:JSON.stringify(score).length,text:async()=>JSON.stringify(score)}];
  const pending=page.get('file').onchange();await nextTurn();
  const worker=page.workers.at(-1);assert.equal(worker.request.mode,'listen');assert.match(worker.request.recordText,/# jottune-tl 1 1/);
  worker.reply(validResult({metadata:score.metadata}));await pending;
  assert.equal(page.api.result().authoring.counts.input_events,score.events.length);
  assert.equal(page.audioCreations(),0);assert.equal(page.get('play').disabled,false);
  await page.get('agent-report').click();const copied=JSON.parse(page.copied.at(-1));
  assert.equal(copied.song.title,'Phone composition');assert(!Object.hasOwn(copied.song,'pcm'));
  page.get('record').value=JSON.stringify(score);
  const pasted=page.get('prepare-input').click();await nextTurn();page.workers.at(-1).reply(validResult({metadata:score.metadata}));await pasted;
  assert.equal(page.audioCreations(),0);assert.equal(page.get('play').disabled,false);
}));
test('authored native refusals retain the failure status and source advisories',()=>withPage(async page=>{
  page.ready({default:'test',engines:[{id:'test',identity:'int-fm-synth 1'}]});
  const score=JSON.parse(readFileSync(new URL('../web/starter.score.json',import.meta.url),'utf8'));
  score.events.push({kind:'off',at:0,stream:score.streams[0],voice:0});
  page.get('record').value=JSON.stringify(score);
  const pending=page.get('prepare-input').click();await nextTurn();
  page.workers.at(-1).reply(validResult({verdict:'refused',stderr:'Too large for the browser profile.',canonical:new Uint8Array(),pcm:undefined}));
  await pending;assert.equal(page.api.result().verdict,'refused');assert(page.api.result().authoring.advisories.length>0);
  assert.equal(page.get('status').textContent,'Too large for the browser profile.');assert.equal(page.get('play').disabled,true);
  await page.get('agent-report').click();const report=JSON.parse(page.copied.at(-1));
  assert.equal(report.song.verdict,'refused');assert(report.song.authoring.advisories.length>0);assert.equal(page.audioCreations(),0);
}));
test('pasted scores keep the selected renderer for silent listening and verification',async()=>{
  for(const [control,mode] of [['prepare-input','listen'],['render','verify']]) await withPage(async page=>{
    page.ready({default:'new',engines:[{id:'old',identity:'int-fm-synth old'},{id:'new',identity:'int-fm-synth new'}]});
    const loaded=page.api.render('saved older song',{release:'old'});await nextTurn();
    page.workers.at(-1).reply(validResult({rendererRelease:'old',engineId:'old'}));await loaded;
    const score=JSON.parse(readFileSync(new URL('../web/starter.score.json',import.meta.url),'utf8'));
    page.get('record').value=JSON.stringify(score);page.get('record').dispatch('input');
    const pending=page.get(control).click();await nextTurn();const worker=page.workers.at(-1);
    assert.equal(worker.request.engine.id,'old');assert.equal(worker.request.mode,mode);
    assert.match(worker.request.recordText,/engine int-fm-synth old/);
    worker.reply(validResult({rendererRelease:'old',engineId:'old'}));await pending;
    assert.equal(page.api.result().rendererRelease,'old');assert.equal(page.audioCreations(),0);
  });
});
test('invalid score imports provide actionable pointer errors without native worker or audio',()=>withPage(async page=>{
  page.ready({default:'test',engines:[{id:'test',identity:'int-fm-synth 1'}]});
  for(const [text,message] of [['{',/incomplete or invalid/],['{"version":3}',/version/]]){
    page.get('record').value=text;await page.get('prepare-input').click();
    assert.match(page.get('status').textContent,message);assert.equal(page.workers.length,0);
    await page.get('agent-report').click();const report=JSON.parse(page.copied.at(-1));assert(report.inputError.message);
    if(text.includes('3'))assert.equal(report.inputError.pointer,'/version');
  }
  assert.equal(page.audioCreations(),0);
}));
test('editable score, title and export names survive verification of different canonical bytes',()=>withPage(async page=>{
  page.ready({default:'test',engines:[{id:'test',identity:'int-fm-synth 1'}]});
  const score=JSON.parse(readFileSync(new URL('../web/starter.score.json',import.meta.url),'utf8'));
  score.metadata={title:'Two lanterns',license:'CC0-1.0'};
  const source='  '+JSON.stringify(score,null,3)+'\r\n';
  // File input also exercises exact source preservation independent of textarea newline normalization.
  page.get('file').files=[new File([source],'untitled.score.json')];
  const preparing=page.get('file').onchange();await nextTurn();
  const draft=page.workers.at(-1).request.recordText;
  const saved=composeDocument('canonical fixture\n',score.metadata);
  const result={canonical:new TextEncoder().encode(saved),metadata:score.metadata,engineId:'test'};
  assert.notEqual(draft,saved);
  page.workers.at(-1).reply(validResult({...result,play:{match:'-',block_indep:'unchecked'}}));await preparing;
  function checkTitle() {
    assert.equal(page.get('loaded-song').textContent,'Two lanterns');
    assert.equal(page.get('mobile-song').textContent,'Two lanterns');
    assert.match(page.get('rail-loaded-song').textContent,/Loaded: Two lanterns/);
    assert.match(document.title,/^Two lanterns · /);
    for(const extension of ['tl','wav']) {
      page.get(extension+'-download').click();
      assert.equal(page.filenames.at(-1),'Two lanterns-saved.'+extension);
    }
    assert.equal(page.get('score-download').hidden,false);
    page.get('score-download').click();
    assert.equal(page.filenames.at(-1),'Two lanterns-source.score.json');
    assert.equal(page.downloaded.at(-1).type,'application/json');
  }
  checkTitle();assert.equal(await page.downloaded.at(-1).text(),source);
  const replay=page.get('verify-saved').click();await nextTurn();
  assert.equal(page.workers.at(-1).request.recordText,saved);
  page.workers.at(-1).reply(validResult(result));await replay;
  assert.equal(page.api.result().verified,true);
  checkTitle();assert.equal(await page.downloaded.at(-1).text(),source);
  const replacement=page.api.listen('different headerless song');await nextTurn();
  assert.equal(page.get('score-download').hidden,true);
  page.api.cancel();await assert.rejects(replacement,/cancelled/i);
  assert(!page.get('loaded-song').textContent.includes('Two lanterns'));
  assert(!document.title.includes('Two lanterns'));
  await complete(page,'unrelated refusal',{verdict:'refused',code:2,stderr:'Native syntax error'});
  assert.equal(page.get('score-download').hidden,true);assert.equal(page.get('tl-download').disabled,true);
  assert(!page.get('loaded-song').textContent.includes('Two lanterns'));
}));
test('portable linked TL uses its declared title and never offers an invented editable score',()=>withPage(async page=>{
  const title='From the record',text=composeDocument('canonical fixture\n',{title});
  const url=await portableLink(text,'https://player.invalid/');
  await page.api.loadLink(url);
  const pending=page.get('prepare-input').click();
  await prepared(page,pending,text,validResult({metadata:{title},play:{match:'yes',block_indep:'unchecked'}}));
  assert.equal(page.get('loaded-song').textContent,title);assert.match(document.title,/^From the record · /);
  assert.equal(page.get('score-download').hidden,true);
}));
test('an incomplete JSON file remains available as an exact, unverified score draft',()=>withPage(async page=>{
  const source='{\n  "version":';
  page.get('file').files=[new File([source],'unfinished.score.json')];
  await page.get('file').onchange();
  assert.match(page.get('status').textContent,/incomplete or invalid/);
  page.get('draft-download').click();
  assert.equal(page.filenames.at(-1),'draft-unverified.score.json');
  assert.equal(page.downloaded.at(-1).type,'application/json');assert.equal(await page.downloaded.at(-1).text(),source);
  assert.equal(page.get('score-download').hidden,true);assert.equal(page.workers.length,0);
}));
test('late file reads cannot replace a newer file and denied clipboard offers plain report text',()=>withPage(async page=>{
  const slow=deferred();page.ready();
  page.get('file').files=[{name:'old.tl',size:12,text:()=>slow.promise}];const first=page.get('file').onchange();
  page.get('file').files=[{name:'new.tl',size:12,text:async()=>'latest file'}];const latest=await prepared(page,page.get('file').onchange(),'latest file');
  slow.resolve('obsolete file');await first;assert.equal(page.api.result(),latest);assert.equal(page.get('record').value,'latest file');
  Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true});
  await page.get('agent-report').click();assert.equal(page.get('agent-report-manual').hidden,false);
  assert.equal(JSON.parse(page.get('agent-report-manual').value).song.recordSha256,latest.recordSha256);
}));

test('superseded Verify and delayed report clipboard errors cannot overwrite newer player state',()=>withPage(async page=>{
  page.ready();page.get('record').value='first input';
  const first=page.get('render').click();await nextTurn();
  const later=page.api.listen('later input');await nextTurn();
  await first;assert.equal(page.get('status').textContent,'Rendering once for listening…');
  page.workers.at(-1).reply(validResult());await later;
  let rejectClipboard;const clipboard=new Promise((resolve,reject)=>{rejectClipboard=reject;});Object.defineProperty(navigator,'clipboard',{value:{writeText:()=>clipboard},configurable:true});
  const copying=page.get('agent-report').click();page.api.clear();rejectClipboard(Error('denied'));await copying;
  assert.equal(page.get('status').textContent,'Record cleared.');assert.equal(page.get('agent-report-manual').hidden,true);
  assert.equal(page.get('agent-report-manual').value,'');
}));

let failures = 0;
for (const {name, run} of tests) {
  try { await run(); console.log('PASS ' + name); }
  catch (error) { failures++; console.error('FAIL ' + name + '\n' + error.stack); }
}
console.log(`BROWSER_CONTRACT ${tests.length - failures}/${tests.length} passed (Node mocks; no browser acceptance)`);
process.exitCode = failures ? 1 : 0;
