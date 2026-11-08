// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import {createInstrumentView, instrumentModel, instrumentSnapshot, noteLabel} from '../web/instrument-view.mjs';
import {createCursorSpectrum} from '../web/media.mjs';
import {projectSpatial} from '../web/spatial-view.mjs';

const event = (seconds, voice, kind, fields = {}) => ({time_ns: String(seconds * 1e9), voice: String(voice), kind, ...fields});
const fixture = () => ({verdict: 'valid', pcm: new Int16Array(4096 * 2), channels: 2, sampleRate: 1024,
  info: {voices: '2', route: '3'}, events: [
    event(0, 0, 'on', {freq_millihz: '440000', velocity: '20000', patch: 'bell'}),
    event(0, 0, 'pos', {x: '-1000', y: '0', z: '0'}),
    event(.5, 0, 'pitch', {freq_millihz: '880000'}),
    event(1, 0, 'move', {x: '1000', y: '0', z: '500', duration_ns: '2000000000'}),
    event(1, 1, 'on', {freq_millihz: '261626', velocity: '12000', lane: '0', frame: '1024'}),
    event(2, 0, 'off'),
    event(2, 0, 'move', {x: '0', y: '1000', z: '0', duration_ns: '500000000'}),
    event(3, 1, 'off'),
  ]});

test('native gate and pitch state is correct at boundaries and after backward seeks', () => {
  const result = fixture(), copy = JSON.stringify(result.events), model = instrumentModel(result);
  let state = instrumentSnapshot(model, .5);
  assert.equal(state.voices[0].gate.gate, true);assert.equal(state.voices[0].gate.frequency, 880);
  assert.equal(state.voices[1].gate.gate, false);
  state = instrumentSnapshot(model, 2);
  assert.equal(state.voices[0].gate.gate, false);assert.equal(state.voices[1].gate.gate, true);
  state = instrumentSnapshot(model, .25);
  assert.equal(state.voices[0].gate.frequency, 440);assert.equal(state.voices[0].gate.gate, true);
  assert.equal(state.voices[1].gate.gate, false);assert.equal(JSON.stringify(result.events), copy);
  assert.equal(noteLabel(440).name, 'A4');assert.equal(noteLabel(261.626).name, 'C4');
});

test('equal-time native order is retained and pitch after note-off does not reopen a gate', () => {
  const result = fixture();result.events = [
    event(0, 0, 'on', {freq_millihz: '440000', velocity: '0'}), event(0, 0, 'off'),
    event(.1, 0, 'pitch', {freq_millihz: '660000'}),
    event(.2, 0, 'off'), event(.2, 0, 'on', {freq_millihz: '220000', velocity: '1'}),
  ];
  const model = instrumentModel(result);
  assert.equal(instrumentSnapshot(model, .1).voices[0].gate.gate, false);
  assert.equal(instrumentSnapshot(model, .1).voices[0].gate.frequency, 660);
  assert.equal(instrumentSnapshot(model, .2).voices[0].gate.gate, true);
});

test('piano roll splits pitch changes and retriggers while retaining stream and rendered boundaries', () => {
  const result=fixture();result.events=[
    event(0,0,'on',{freq_millihz:'440000',stream:'lead'}),
    event(.5,0,'pitch',{freq_millihz:'880000',stream:'control'}),
    event(1,0,'on',{freq_millihz:'220000',stream:'bass'}),event(2,0,'off'),
    event(3,1,'on',{freq_millihz:'330000',stream:'lead'}),
  ];
  const model=instrumentModel(result);
  assert.deepEqual(model.notes.map(({start,end,frequency,stream})=>[start,end,frequency,stream]),
    [[0,.5,440,'lead'],[.5,1,880,'lead'],[1,2,220,'bass'],[3,4,330,'lead']]);
  const app=page();app.view.setResult(result);
  const bars=app.get('instrument-roll-notes').children.filter(node=>node.tagName==='rect');
  assert.equal(bars.length,4);
  assert.equal(bars[0].getAttribute('x'),'0');assert.equal(bars[3].getAttribute('x'),'750');
  assert.ok(Number(bars[1].getAttribute('y'))<Number(bars[0].getAttribute('y')),'higher pitch is higher vertically');
  assert.equal(bars[0].getAttribute('fill'),bars[3].getAttribute('fill'),'stream colour crosses voice boundaries');
  app.view.update(3,false);assert.equal(app.get('instrument-playhead').getAttribute('x1'),'750');
  app.view.update(1,false);assert.equal(app.get('instrument-playhead').getAttribute('x1'),'250');
});

test('cursor FFT retains channel, time, amplitude and PCM identity including DC and Nyquist', () => {
  const rate=48000,frames=rate*2,pcm=new Int16Array(frames*2), first=1500,second=3000;
  for(let i=0;i<frames;i++) {
    pcm[i*2]=Math.round(16384*Math.sin(2*Math.PI*(i<rate?first:second)*i/rate));
    pcm[i*2+1]=-pcm[i*2];
  }
  const original=pcm.slice(), analyze=createCursorSpectrum({pcm,channels:2,sampleRate:rate});
  const peak=analysis=>Array.from(analysis.dbfs).reduce((best,value,bin)=>value>analysis.dbfs[best]?bin:best,0);
  let analysis=analyze(.5,0);assert.equal(peak(analysis)*analysis.binHz,first);
  assert.ok(Math.abs(analysis.dbfs[64]+6.0206)<.01);
  analysis=analyze(.5,1);assert.equal(peak(analysis)*analysis.binHz,first);
  assert.ok(Math.abs(analysis.dbfs[64]+6.0206)<.01,'antiphase right channel must not cancel');
  analysis=analyze(1.5,0);assert.equal(peak(analysis)*analysis.binHz,second);
  assert.deepEqual([analysis.frame,analysis.start,analysis.end],[72000,70976,73024]);
  assert.deepEqual(pcm,original);
  for(const nyquist of [false,true]) {
    const tone=new Int16Array(4096).fill(16384);if(nyquist)for(let i=1;i<tone.length;i+=2)tone[i]=-16384;
    const edge=createCursorSpectrum({pcm:tone,channels:1,sampleRate:rate})(2048/rate);
    assert.ok(Math.abs(edge.dbfs[nyquist?1024:0]+6.0206)<.01,'DC/Nyquist must not receive doubled amplitude');
  }
  const silent=createCursorSpectrum({pcm:new Int16Array(1),channels:1,sampleRate:rate})(0);
  assert.ok(silent.dbfs.every(value=>value===-120));
  assert.throws(()=>analyze(0,2),/channel/);assert.throws(()=>analyze(NaN,0),/cursor/);
});

test('hidden and unchecked panels perform no PCM analysis and resume without replacing audio', () => {
  const result=fixture();let reads=0,eventReads=0;
  result.pcm=new Proxy(result.pcm,{get(target,key){if(/^\d+$/.test(String(key)))reads++;return Reflect.get(target,key,target);}});
  const events=result.events;Object.defineProperty(result,'events',{get(){eventReads++;return events;}});
  const app=page();app.view.setVisible(false);const drawings=app.drawing.length;
  app.view.setResult(result);app.view.update(1,true);
  assert.equal(reads,0);assert.equal(eventReads,0);assert.equal(app.drawing.length,drawings);
  app.view.setVisible(true);assert.ok(reads>0);assert.ok(eventReads>0);
  for(const panel of ['scope','spectrum','peaks']) {const toggle=app.get('instrument-show-'+panel);toggle.checked=false;toggle.change();}
  reads=0;app.view.update(1.5,true);assert.equal(reads,0,'all PCM panels off means zero sample reads');
  assert.equal(app.get('instrument-playhead').dataset.time,'1.5','native roll still follows transport');
  const spectrum=app.get('instrument-show-spectrum');spectrum.checked=true;spectrum.change();
  reads=0;app.view.update(2,true);assert.equal(reads,4096,'Both reads one bounded FFT window for each independent channel');
  assert.equal(app.get('instrument-spectrum').dataset.frame,'2048');
  reads=0;app.get('instrument-spectrum-right').click();assert.equal(reads,2048,'Right alone reads one FFT window');
  assert.equal(app.get('instrument-spectrum').dataset.channel,'1');
  app.view.setVisible(false);reads=0;app.view.update(3,true);assert.equal(reads,0);
  app.view.setResult({...fixture(),verdict:'refused'});assert.equal(reads,0);
  app.view.setVisible(true);assert.match(app.get('instrument-status').textContent,/valid record/);
  assert.equal(app.get('instrument-spectrum-curve').getAttribute('d'),'');
});

test('spatial targets use the latest command, never invented interpolation or yaw', () => {
  const result = fixture();result.events.push(event(1.5, 0, 'control', {level: '90000'}));
  const model = instrumentModel(result);
  assert.equal(instrumentSnapshot(model, .9).voices[0].command.x, -1000);
  const command = instrumentSnapshot(model, 1.5).voices[0].command;
  assert.deepEqual([command.x, command.y, command.z, command.duration], [1000, 0, 500, 2]);
  assert.equal(instrumentSnapshot(model, 2).voices[0].command.y, 1000);
  assert.equal(instrumentSnapshot(model, .1).voices[0].command.x, -1000);
});

function spatialFixture() {
  const result=fixture(), calls=[];
  result.spatial={format:1,block:64,near:500,initialYaw:0,frames:4096,voices:2,paths:[
    {voice:0,sample:0,pen:0,x:-1000,y:0,z:0},
    {voice:0,sample:1024,pen:1,x:400,y:300,z:500},
    {voice:0,sample:2048,pen:0,x:-200,y:500,z:0},
    {voice:1,sample:0,pen:0,x:0,y:1000,z:0},
    {voice:1,sample:3072,pen:1,x:800,y:1000,z:500},
  ]};
  // Distinct supplied positions prove the view delegates timing/rounding to its
  // native sampler. Native sampler arithmetic has its own engine-backed tests.
  const spatialSampler={positionsAt(frame) {
    calls.push(frame);return [{x:frame-1000,y:300,z:500},{x:frame+17,y:1000,z:500}];
  },yawAt(frame) {return frame>1024 ? 90000 : 0;}};
  return {result,spatialSampler,calls};
}
const descendants = node => [node,...node.children.flatMap(descendants)];
const spatialDot = (app,voice) => descendants(app.get('instrument-space')).find(node => node.dataset.targetVoice===String(voice));

test('spatial snapshots use the supplied native sampler at the playback frame, including closed gates and backwards seeks',()=>{
  const {result,spatialSampler,calls}=spatialFixture(),pcm=result.pcm.slice(),metadata=JSON.stringify(result.spatial);
  const model=instrumentModel(result,{spatialSampler});
  const first=instrumentSnapshot(model,1.375),later=instrumentSnapshot(model,2.375),back=instrumentSnapshot(model,.125);
  assert.deepEqual(calls,[1408,2432,128]);
  assert.deepEqual(first.voices[0].position,{x:408,y:300,z:500});
  assert.equal(first.voices[0].command.x,1000,'a move destination is not the current native position');
  assert.equal(later.voices[0].gate.gate,false);assert.equal(later.voices[0].position.x,1432);
  assert.equal(back.voices[1].gate.gate,false);assert.equal(back.voices[1].position.x,145,'voices without an authored open gate still have native positions');
  assert.equal(first.spatial.yaw,90000);assert.equal(back.spatial.yaw,0);
  instrumentSnapshot(model,1.4);assert.equal(calls.at(-1),1433,'the native sampler owns block rounding');
  assert.deepEqual(result.pcm,pcm);assert.equal(JSON.stringify(result.spatial),metadata);
});

test('spatial scenes show native paths, jumps, elevation and moving voices without changing transport',()=>{
  const {result,spatialSampler}=spatialFixture(),app=page();app.view.setResult(result,{spatialSampler});
  app.view.update(1.375,false);
  assert.equal(app.get('instrument-space').dataset.positionKind,'native');
  assert.equal(app.get('instrument-space').dataset.projection,'top','Space opens in Top view');
  assert.equal(app.get('instrument-space-top').getAttribute('aria-pressed'),'true');
  const first=spatialDot(app,0),other=spatialDot(app,1), firstX=first.getAttribute('cx');
  assert.equal(first.dataset.x,'408');assert.equal(other.dataset.x,'1425');
  assert.match(app.get('instrument-space-summary').textContent,/yaw 90.0°/);
  const paths=app.get('instrument-space-paths').children;
  assert.ok(paths.some(path=>path.dataset.pathKind==='motion' && path.getAttribute('d')));
  assert.ok(paths.some(path=>path.dataset.pathKind==='jump' && path.getAttribute('d') && path.getAttribute('stroke-dasharray')));
  app.view.update(2.375,true);assert.equal(first.dataset.x,'1432');assert.notEqual(first.getAttribute('cx'),firstX);
  assert.equal(app.get('instrument-voices').children[0].dataset.gate,'closed');
  assert.equal(first.getAttribute('display'),undefined,'closed gate never hides a current native position');
  assert.match(app.get('instrument-detail').textContent,/Native position \(1432, 300, 500\)/);
  const top=first.getAttribute('cy');app.get('instrument-space-3d').click();
  assert.notEqual(first.getAttribute('cy'),top);assert.equal(app.get('instrument-space').dataset.projection,'3d');
  app.get('instrument-space-top').click();assert.equal(first.getAttribute('cy'),top);
  assert.equal(app.get('instrument-space-top').getAttribute('aria-pressed'),'true');
  app.view.update(.125,false);assert.equal(first.dataset.x,'-872');assert.equal(other.dataset.x,'145');
  other.click();assert.equal(app.container.dataset.selectedVoice,'1');assert.deepEqual(app.seeks,[]);
  assert.deepEqual(projectSpatial({x:0,y:0,z:500},1000,'top'),[200,155]);
  assert.notEqual(projectSpatial({x:0,y:0,z:500},1000,'3d')[1],155);
  assert.notEqual(projectSpatial({x:0,y:1000,z:0},1000,'3d')[0],200,'front and up axes remain distinct in 3D');
});

test('clustered spatial voices keep one stable edge label and exact native positions in both projections',()=>{
  const result=fixture();result.info.voices='64';result.events=[];
  result.spatial={format:1,block:64,near:500,initialYaw:0,frames:4096,voices:64,
    paths:Array.from({length:64},(_,voice)=>({voice,sample:0,pen:0,x:0,y:0,z:0}))};
  const spatialSampler={positionsAt(frame){return Array.from({length:64},()=>({x:Math.floor(frame/64),y:20,z:30}));},yawAt(){return 0;}};
  const pcm=result.pcm.slice(),metadata=JSON.stringify(result.spatial),app=page();
  app.view.setResult(result,{spatialSampler});app.view.update(1,false);
  const model=instrumentModel(result,{spatialSampler}),label=app.get('instrument-space-label'),leader=app.get('instrument-space-leader');
  const anchor=[label.getAttribute('x'),label.getAttribute('y')];
  assert.equal(app.get('instrument-space-selection').getAttribute('pointer-events'),'none','The label overlay cannot intercept dot clicks');
  for(const mode of ['top','3d']) {
    app.get('instrument-space-'+mode).click();
    const expected=projectSpatial({x:16,y:20,z:30},model.spatialExtent,mode);
    for(let voice=0;voice<64;voice++) {
      const dot=spatialDot(app,voice);
      assert.deepEqual([Number(dot.getAttribute('cx')),Number(dot.getAttribute('cy'))],expected,'Labels never displace coincident native dots');
    }
    const ids=descendants(app.get('instrument-space')).filter(node=>node.tagName==='text' && /^(?:Voice )?\d+$/.test(node.textContent));
    assert.deepEqual(ids,[label],'Dense scenes contain one voice ID, not a cluster of overlapping numbers');
    app.get('instrument-voices').children[63].click();
    assert.equal(label.textContent,'Voice 63');assert.deepEqual([label.getAttribute('x'),label.getAttribute('y')],anchor);
    assert.deepEqual([Number(leader.getAttribute('x2')),Number(leader.getAttribute('y2'))],expected);
    assert.equal(leader.getAttribute('stroke'),spatialDot(app,63).getAttribute('fill'));
    assert.match(app.get('instrument-space').getAttribute('aria-label'),/Voice 63.*edge label/);
    spatialDot(app,0).click();assert.equal(label.textContent,'Voice 0');
  }
  app.view.update(2,false);
  assert.equal(spatialDot(app,0).dataset.x,'32');assert.deepEqual([label.getAttribute('x'),label.getAttribute('y')],anchor);
  assert.equal(leader.getAttribute('x2'),spatialDot(app,0).getAttribute('cx'));
  assert.equal(leader.getAttribute('y2'),spatialDot(app,0).getAttribute('cy'));
  assert.deepEqual(app.seeks,[]);assert.deepEqual(result.pcm,pcm);assert.equal(JSON.stringify(result.spatial),metadata);
});

test('spatial selection hides absent targets and clears stale identification on replacement',()=>{
  const app=page();app.view.setResult(fixture());
  const selection=app.get('instrument-space-selection'),label=app.get('instrument-space-label');
  assert.equal(label.textContent,'Voice 0');
  app.get('instrument-voices').children[1].click();
  assert.equal(selection.getAttribute('display'),'none');assert.equal(label.textContent,'');
  assert.match(app.get('instrument-space').getAttribute('aria-label'),/Voice 1 has no position/);
  app.get('instrument-space-top').click();assert.equal(selection.getAttribute('display'),'none');
  app.get('instrument-voices').children[0].click();assert.equal(selection.getAttribute('display'),undefined);
  app.view.setResult({...fixture(),events:[]});
  assert.equal(selection.getAttribute('display'),'none');assert.equal(label.textContent,'');
  app.view.clear();assert.equal(selection.getAttribute('display'),'none');assert.equal(label.textContent,'');
  assert.equal(app.get('instrument-space').getAttribute('aria-label'),'No spatial data loaded.');
});

test('late native spatial data preserves selected voice and cursor, defers hidden work and clears on replacement',()=>{
  const {result,spatialSampler,calls}=spatialFixture(),app=page();app.view.setResult(result);
  app.get('instrument-voices').children[1].click();app.view.update(2,false);
  app.view.setResult(result,{spatialSampler});
  assert.equal(app.container.dataset.selectedVoice,'1');assert.equal(app.container.dataset.time,'2');
  assert.equal(spatialDot(app,1).dataset.x,'2065');
  app.view.setVisible(false);calls.length=0;app.view.update(3,true);assert.equal(calls.length,0);
  app.view.setResult(result,{spatialSampler});assert.equal(calls.length,0);
  assert.equal(app.get('instrument-space-label').textContent,'','A hidden replacement defers label work as well as sampling');
  app.view.setVisible(true);assert.ok(calls.length);assert.equal(app.container.dataset.time,'3');
  assert.equal(app.get('instrument-space-label').textContent,'Voice 1');
  assert.equal(app.container.dataset.selectedVoice,'1');
  app.view.clear();assert.equal(app.get('instrument-space-paths').children.length,0);
  assert.equal(spatialDot(app,1),undefined);assert.deepEqual(app.get('instrument-space').dataset,{});
  assert.equal(app.get('instrument-space-label').textContent,'');
  app.view.setResult(fixture());assert.equal(app.get('instrument-space').dataset.positionKind,'target');
  assert.match(app.get('instrument-space-note').textContent,/not current positions/);
});

test('unavailable or invalid native spatial data falls back honestly without discarding audio',()=>{
  for(const failure of ['mismatch','throws','incomplete']) {
    const {result,spatialSampler}=spatialFixture(),app=page();
    if(failure==='mismatch')result.spatial.frames++;
    if(failure==='throws')spatialSampler.positionsAt=()=>{throw Error('unavailable');};
    if(failure==='incomplete')spatialSampler.positionsAt=()=>[{x:0,y:0,z:0}];
    const model=instrumentModel(result,{spatialSampler}),snapshot=instrumentSnapshot(model,1.5);
    assert.equal(model.pcm,result.pcm);assert.equal(snapshot.spatial,null);assert.equal(snapshot.voices[0].position,null);
    app.view.setResult(result,{spatialSampler});app.view.update(1.5,false);
    assert.equal(spatialDot(app,0).dataset.positionKind,'target');assert.equal(spatialDot(app,0).dataset.x,'1000');
    assert.match(app.get('instrument-space-note').textContent,/Showing command targets only/);
    assert.match(app.get('instrument-status').textContent,/commanded targets/);
    assert.equal(app.get('instrument-meters').children.length,2);
  }
  const {result,spatialSampler}=spatialFixture(),app=page({reduced:true});
  app.view.setResult(result,{spatialSampler});const x=spatialDot(app,0).dataset.x;
  app.view.update(1,true);assert.equal(spatialDot(app,0).dataset.x,x,'reduced motion holds spatial animation during playback');
  app.view.update(1,false);assert.equal(spatialDot(app,0).dataset.x,'24','paused inspection still follows the cursor');
});

test('PCM windows and independent peaks use the cached signed samples exactly', () => {
  const result = fixture();result.pcm[1024 * 2] = 16384;result.pcm[1025 * 2 + 1] = -32768;
  const model = instrumentModel(result), snapshot = instrumentSnapshot(model, 1);
  assert.equal(model.pcm, result.pcm);
  assert.deepEqual([snapshot.frame, snapshot.start, snapshot.end], [1024, 768, 1280]);
  assert.deepEqual(snapshot.peaks, [.5, 1]);
  assert.deepEqual(instrumentSnapshot(model, 0).peaks, [0, 0]);
  assert.equal(instrumentSnapshot(model, -10).frame, 0);
  assert.equal(instrumentSnapshot(model, 100).frame, 4095);
  assert.deepEqual(instrumentSnapshot(model, 100).peaks, [0, 0]);
});

test('raw ambisonic routes retain coefficient labels instead of left/right audio claims', () => {
  for (const route of [1, 2]) {
    const result = fixture();result.info.route = String(route);result.channels = 4;
    const model = instrumentModel(result);
    assert.equal(model.raw, true);assert.match(model.routeLabel, /4 coefficients/);
    assert.deepEqual(model.channelLabels, ['Coefficient 0', 'Coefficient 1', 'Coefficient 2', 'Coefficient 3']);
  }
  assert.equal(instrumentModel(fixture()).routeLabel, 'Binaural · left / right');
});

test('oversized, failed or malformed event listings keep PCM but refuse partial event displays', () => {
  for (const change of [
    result => {result.eventsError = 'Native listing failed';},
    result => {result.events = new Array(20001).fill(event(0, 0, 'control'));},
    result => {result.events.push(event(2, 0, 'move', {x: 'bad', y: '0', z: '0', duration_ns: '1'}));},
  ]) {
    const result = fixture();change(result);const model = instrumentModel(result);
    assert.ok(model.eventError);assert.equal(model.pcm, result.pcm);
    assert.ok(instrumentSnapshot(model, 1).voices.every(voice => !voice.gate.gate && !voice.command));
    assert.ok(model.voices.every(voice => !voice.gates.length && !voice.commands.length));
  }
  assert.throws(() => instrumentModel({...fixture(), pcm: new Int16Array(3)}), /complete PCM/);
});

function page({reduced = false} = {}) {
  const ids = new Map(), drawing = [], strokes = [], media = {matches: reduced, addEventListener() {}};let document;
  class Element {
    constructor(tag) {
      this.tagName = tag;this.ownerDocument = document;this.children = [];this.dataset = {};
      this.attributes = {};this.textContent = '';this.listeners = new Map();this.disabled = false;
      this.style = {setProperty() {}};
      Object.defineProperty(this, 'id', {get: () => this._id, set: value => {this._id = value;ids.set(value, this);}});
    }
    append(...children) {this.children.push(...children);}
    prepend(...children) {this.children.unshift(...children);}
    replaceChildren(...children) {this.children = children;if(this.tagName==='select')this.value=children[0]?.value || '';}
    setAttribute(name, value) {this.attributes[name] = value;if (name === 'id') this.id = value;}
    getAttribute(name) {return this.attributes[name];}
    removeAttribute(name) {delete this.attributes[name];}
    addEventListener(name, callback) {this.listeners.set(name, callback);}
    click() {if (!this.disabled) this.listeners.get('click')?.();}
    change() {this.listeners.get('change')?.();}
    getContext() {return {clearRect: (...args) => drawing.push(args), beginPath() {}, moveTo() {}, lineTo() {}, stroke() {strokes.push(this.strokeStyle);}};}
  }
  document = {createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
    defaultView: {matchMedia: () => media}};
  const container = new Element('div'), seeks = [];
  const view = createInstrumentView(container, {onSeek: time => seeks.push(time)});
  return {view, container, seeks, drawing, strokes, media, get: id => ids.get(id)};
}

test('voice selection and command navigation seek native boundaries without owning playback', () => {
  const app = page();app.view.setResult(fixture());
  assert.equal(app.get('instrument-prev').disabled, true);
  app.get('instrument-next').click();assert.deepEqual(app.seeks, [1]);
  assert.equal(app.container.dataset.time, '0'); // Only the transport may move the cursor.
  app.view.update(1, true);app.get('instrument-prev').click();app.get('instrument-next').click();
  assert.deepEqual(app.seeks, [1, 0, 2]);
  const voices = app.get('instrument-voices').children;
  voices[1].click();assert.equal(app.container.dataset.selectedVoice, '1');
  assert.match(app.get('instrument-detail').textContent, /Voice 1 · gate open/);
  // The detail line is static text, not a Tab stop.
  assert.equal(app.get('instrument-detail').getAttribute('tabindex'), undefined);assert.equal(app.get('instrument-detail').tabIndex, undefined);
  assert.equal(app.get('instrument-prev').disabled, true);assert.equal(app.get('instrument-next').disabled, true);
  assert.equal(voices[1].getAttribute('aria-pressed'), 'true');
  assert.match(app.get('instrument-keyboard').getAttribute('aria-label'), /voice 1 C4/);
});

test('hidden views defer drawing and resume at the latest cursor; reduced motion holds waveforms', () => {
  const app = page({reduced: true});app.view.setResult(fixture());const drawn = app.drawing.length;
  app.view.update(1, true);
  assert.equal(app.drawing.length, drawn);assert.equal(app.get('instrument-scope').dataset.motion, 'held');
  assert.equal(app.get('instrument-voices').children[1].dataset.gate, 'open');
  app.view.setVisible(false);app.view.update(3, false);
  assert.equal(app.container.dataset.time, '1');assert.equal(app.drawing.length, drawn);
  app.view.setVisible(true);assert.equal(app.container.dataset.time, '3');
  assert.equal(app.get('instrument-scope').dataset.frame, '3072');
  assert.equal(app.get('instrument-voices').children[1].dataset.gate, 'closed');
});

test('clear and invalid replacement remove all old meters, voice, waveform and spatial state', () => {
  const app = page();app.view.setResult(fixture());app.view.update(1, false);
  assert.equal(app.get('instrument-meters').children.length, 2);
  assert.match(app.get('instrument-detail').textContent, /Move target/);
  app.view.clear();
  assert.deepEqual(app.container.dataset, {});assert.deepEqual(app.get('instrument-scope').dataset, {});
  assert.equal(app.get('instrument-meters').children.length, 0);assert.equal(app.get('instrument-voices').children.length, 0);
  assert.equal(app.get('instrument-prev').disabled, true);assert.equal(app.get('instrument-next').disabled, true);
  assert.ok(app.get('instrument-keyboard').children.every(key => key.dataset.gate === 'closed'));
  app.view.setResult(fixture());app.view.setResult({verdict: 'valid'});
  assert.match(app.get('instrument-status').textContent, /complete PCM/);
  assert.equal(app.get('instrument-meters').children.length, 0);assert.deepEqual(app.container.dataset, {});
});

test('failed verification clears the view even when the engine returns a PCM buffer', () => {
  const app = page();app.view.setResult(fixture());
  for (const verdict of ['mismatch', 'refused', undefined]) {
    app.view.setResult({...fixture(), verdict});
    assert.match(app.get('instrument-status').textContent, /valid record/);
    assert.equal(app.get('instrument-meters').children.length, 0);
    assert.equal(app.get('instrument-voices').children.length, 0);assert.deepEqual(app.container.dataset, {});
  }
});


test('spectrum follows the selected Scope channel colour and resets after a mono replacement', () => {
  const app=page();app.view.setResult(fixture());
  const curve=app.get('instrument-spectrum-curve');
  const [left,right]=app.strokes.slice(-2);
  assert.notEqual(left,right);assert.equal(curve.getAttribute('stroke'),left);
  app.get('instrument-spectrum-right').click();assert.equal(curve.getAttribute('stroke'),right);
  assert.equal(curve.getAttribute('stroke-dasharray'),'4 2','Right keeps its distinct stroke when shown alone');
  assert.equal(app.get('instrument-spectrum').dataset.channel,'1');
  app.get('instrument-spectrum-left').click();assert.equal(curve.getAttribute('stroke'),left);
  assert.equal(curve.getAttribute('stroke-dasharray'),'none');
  app.get('instrument-spectrum-right').click();
  app.view.setResult({...fixture(),channels:1,info:{voices:'2',route:'0'}});
  assert.equal(curve.getAttribute('stroke'),left);
  assert.equal(app.get('instrument-spectrum').dataset.channel,'0');
  assert.equal(app.get('instrument-spectrum-controls').hidden,true);
  assert.equal(app.get('instrument-spectrum-mono').hidden,false);
});

test('Both draws independent stereo spectra and preserves antiphase energy without changing PCM', () => {
  for (const antiphase of [false,true]) {
    const result=fixture();result.sampleRate=48000;result.pcm=new Int16Array(8192*2);
    for(let frame=0;frame<8192;frame++) {
      const left=Math.round(16000*Math.sin(2*Math.PI*1500*frame/48000));
      result.pcm[frame*2]=left;
      result.pcm[frame*2+1]=antiphase ? -left : Math.round(8000*Math.sin(2*Math.PI*3000*frame/48000));
    }
    const original=result.pcm.slice(), app=page();app.view.setResult(result);app.view.update(.05,false);
    const first=app.get('instrument-spectrum-curve'), second=app.get('instrument-spectrum-curve-secondary');
    const left=first.getAttribute('d'),right=second.getAttribute('d');
    assert.ok(left.length>100 && right.length>100);
    assert.equal(first.dataset.channel,'0');assert.equal(second.dataset.channel,'1');
    assert.equal(first.getAttribute('stroke'),'#eabf6c');assert.equal(second.getAttribute('stroke'),'#87d7b3');
    assert.equal(app.get('instrument-spectrum').dataset.channel,'both');
    assert.equal(app.get('instrument-spectrum-both').getAttribute('aria-pressed'),'true');
    assert.equal(app.get('instrument-spectrum-channel-label').hidden,true);
    if(antiphase) assert.equal(left,right,'Opposite phase retains two full spectra instead of cancellation');
    else assert.notEqual(left,right,'Different stereo frequencies remain distinct');
    app.get('instrument-spectrum-left').click();assert.equal(first.getAttribute('d'),left);assert.equal(second.getAttribute('d'),'');
    app.get('instrument-spectrum-right').click();assert.equal(first.getAttribute('d'),right);assert.equal(second.getAttribute('d'),'');
    app.get('instrument-spectrum-both').click();assert.equal(first.getAttribute('d'),left);assert.equal(second.getAttribute('d'),right);
    assert.deepEqual(result.pcm,original);assert.deepEqual(app.seeks,[]);
    app.view.clear();assert.equal(first.getAttribute('d'),'');assert.equal(second.getAttribute('d'),'');
  }
});

test('raw fields and multichannel audio retain explicit channel labels instead of stereo controls', () => {
  for (const route of ['1','2','0']) {
    const result={...fixture(),channels:4,info:{voices:'2',route}}, app=page();app.view.setResult(result);
    assert.equal(app.get('instrument-spectrum-controls').hidden,true);
    assert.equal(app.get('instrument-spectrum-mono').hidden,true);
    assert.equal(app.get('instrument-spectrum-channel-label').hidden,false);
    const select=app.get('instrument-spectrum-channel');
    assert.equal(select.children[3].textContent,route === '0' ? 'Channel 4' : 'Coefficient 3');
    select.value='3';select.change();assert.equal(app.get('instrument-spectrum').dataset.channel,'3');
    assert.equal(app.get('instrument-spectrum-curve-secondary').getAttribute('d'),'');
    assert.match(app.get('instrument-spectrum').getAttribute('aria-label'),route === '0' ? /Channel 4/ : /Coefficient 3/);
  }
});
