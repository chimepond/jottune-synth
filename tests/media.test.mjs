// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Numerical display tests; audio rendering remains the native engine's job.
import assert from 'node:assert/strict';
import test from 'node:test';
import {analyzeSpectrogram, spectrogram, nativeTimeline, pianoRollGeometry, toSvg, songStructure, songStructureText} from '../web/media.mjs';
import {songPreview} from '../web/song-preview.mjs';
import {diagnoseEvents} from '../web/analysis.mjs';

const rate = 48000;
const structureInput = (events = [], extra = {}) => ({info:{fs:'10',frames:'20',voices:'3'},events,...extra});
test('song structure counts attacks and sample-frame gate occupancy without quantizing pitch or rendering', () => {
  const result = structureInput([
    {kind:'on',voice:'0',time_ns:'0',freq_millihz:'440250'},
    {kind:'pitch',voice:'0',time_ns:'200000000',freq_millihz:'880125'},
    {kind:'on',voice:'0',time_ns:'300000000',freq_millihz:'220500'},
    {kind:'pitch',voice:'2',time_ns:'600000000',freq_millihz:'9999999'},
    {kind:'off',voice:'0',time_ns:'1000000000'},
    {kind:'on',voice:'1',time_ns:'500000000',frame:'5',lane:'0',freq_millihz:'330000'},
    {kind:'off',voice:'1',time_ns:'1500000000',frame:'15',lane:'0'},
  ]);
  result.info.events='5';
  const before = JSON.stringify(result), profile = songStructure(result);
  assert.deepEqual(profile, {version:1,status:'available',scope:'native_gate_commands',sample_rate_hz:10,frames:20,
    duration_seconds:2,note_attacks:3,attacks_per_second:1.5,active_voices:2,allocated_voices:3,
    commanded_frequency_hz:{minimum:220.5,maximum:880.125},maximum_open_gates:2,mean_open_gates:1,no_gate_seconds:.5});
  assert.equal(JSON.stringify(result),before);
  assert.deepEqual(diagnoseEvents(result).structure,profile,'Audio diagnostics reuse the same native structure');
  assert.match(songStructureText(profile),/3 note attacks.*220.5–880.125 Hz.*not measured silence/);
});

test('same-frame commands retain native ordering without transient overlap or false rest time', () => {
  const result = structureInput([
    {kind:'on',voice:0,time_ns:'0',freq_millihz:440000},
    // Both commands map to frame zero. The native authored listing order wins.
    {kind:'off',voice:0,time_ns:'1'},
    {kind:'on',voice:1,time_ns:'99999999',freq_millihz:0},
    {kind:'off',voice:1,time_ns:'100000000'},
    // Generated commands apply after authored commands at their sample frame.
    {kind:'on',voice:1,time_ns:'100000000',frame:1,lane:0,freq_millihz:261626},
  ]);
  const profile=songStructure(result);
  assert.equal(profile.note_attacks,3);assert.equal(profile.maximum_open_gates,1);
  assert.equal(profile.mean_open_gates,1);assert.equal(profile.no_gate_seconds,0);
  assert.deepEqual(profile.commanded_frequency_hz,{minimum:0,maximum:440});
});

test('empty gates and subframe on/off pairs describe commands rather than sound', () => {
  const empty=songStructure(structureInput());
  assert.equal(empty.note_attacks,0);assert.equal(empty.active_voices,0);
  assert.equal(empty.mean_open_gates,0);assert.equal(empty.maximum_open_gates,0);assert.equal(empty.no_gate_seconds,2);
  assert.deepEqual(empty.commanded_frequency_hz,{minimum:null,maximum:null});
  const pulse=songStructure(structureInput([
    {kind:'on',voice:0,time_ns:1,freq_millihz:440000},{kind:'off',voice:0,time_ns:2}]));
  assert.equal(pulse.note_attacks,1);assert.equal(pulse.active_voices,1);
  assert.equal(pulse.maximum_open_gates,0);assert.equal(pulse.no_gate_seconds,2);
  const generated=songStructure(structureInput([
    {kind:'on',voice:0,frame:1,lane:0,time_ns:'22676',freq_millihz:440000}],
    {info:{fs:44100,frames:44100,voices:1,events:0}}));
  assert.equal(generated.status,'available');assert.equal(generated.no_gate_seconds,1/44100);
  assert.equal(generated.mean_open_gates,44099/44100);
});

test('incomplete, oversized and invalid event listings never return partial song profiles', () => {
  const event={kind:'on',voice:0,time_ns:0,freq_millihz:440000};
  for(const input of [undefined, structureInput([], {info:{fs:0,frames:20,voices:1}}),
    structureInput([], {eventsError:'listing failed'}), structureInput([], {eventsTruncated:true}),
    structureInput(Array(20001).fill(event)), structureInput([event],{info:{fs:10,frames:20,voices:3,events:2}}),
    structureInput([{...event,voice:3}]), structureInput([{...event,time_ns:'2000000000'}]),
    structureInput([{...event,time_ns:''}]), structureInput([{...event,freq_millihz:'NaN'}]),
    structureInput([{...event,time_ns:'100000000',frame:2,lane:0}]), structureInput([{...event,kind:'unsupported'}])]) {
    const profile=songStructure(input);assert.equal(profile.status,'unavailable');
    assert.deepEqual(diagnoseEvents(input).structure,profile);
    assert.equal(profile.note_attacks,undefined);assert.equal(profile.mean_open_gates,undefined);
    assert.match(songStructureText(profile),/unavailable/);
  }
});

function tone(seconds = 1) {
  return Int16Array.from({length: Math.round(seconds * rate)}, (_, i) =>
    Math.round(8192 * Math.sin(2 * Math.PI * 375 * i / rate) + 4096 * Math.sin(2 * Math.PI * 1500 * i / rate)));
}
function level(panel, frequency, column = Math.floor(panel.columns / 2)) {
  const row = Math.min(panel.rows - 1, Math.max(0,
    Math.floor(Math.log(panel.maximumHz / frequency) / Math.log(panel.maximumHz / panel.minimumHz) * panel.rows)));
  return panel.dbfs[row * panel.columns + column];
}
function columnPeak(panel, column) {
  let peak = -Infinity;
  for (let row = 0; row < panel.rows; row++) peak = Math.max(peak, panel.dbfs[row * panel.columns + column]);
  return peak;
}

test('known bin-centred tones retain frequency and calibrated dBFS amplitude', () => {
  const pcm = tone(), before = pcm.slice();
  const analysis = analyzeSpectrogram({pcm, channels: 1, sampleRate: rate});
  const [low, full] = analysis.panels;
  assert.equal(low.minimumHz, 30); assert.equal(low.maximumHz, 600);
  assert.equal(full.minimumHz, 150); assert.equal(full.maximumHz, 16000);
  assert(Math.abs(level(low, 375) - 20 * Math.log10(.25)) < .05);
  assert(Math.abs(level(full, 1500) - 20 * Math.log10(.125)) < .05);
  assert(level(low, 100) < -70, 'A distant empty band must not become the tone');
  assert.equal(analysis.durationSeconds, 1);
  assert.deepEqual(pcm, before);
});

test('short events and the final sample survive full-coverage time aggregation', () => {
  const pcm = new Int16Array(4 * rate), onset = Math.round(3.703 * rate);
  for (let i = 0; i < 100; i++) pcm[onset + i] = Math.round(16000 * Math.sin(2 * Math.PI * 1500 * i / rate));
  pcm[pcm.length - 1] = 16384;
  const full = analyzeSpectrogram({pcm, channels: 1, sampleRate: rate}).panels[1];
  const at = Math.floor(onset / pcm.length * full.columns);
  assert(columnPeak(full, at) > -35, 'A 2 ms event must not fall between sampled windows');
  assert(columnPeak(full, full.columns - 1) > -65, 'The final sample must be covered');
  for (let x = 0; x < Math.floor(3 / 4 * full.columns); x++) assert.equal(columnPeak(full, x), -Infinity);
  assert.equal(full.hopSize, full.fftSize / 2);
});

test('tone changes retain their time location instead of mixing across the song', () => {
  const pcm = new Int16Array(rate * 2);
  for (let i = 0; i < pcm.length; i++) {
    const frequency = i < rate ? 375 : 1500;
    pcm[i] = Math.round(8192 * Math.sin(2 * Math.PI * frequency * i / rate));
  }
  const full = analyzeSpectrogram({pcm, channels: 1, sampleRate: rate}).panels[1];
  const early = Math.floor(full.columns / 4), late = Math.floor(full.columns * 3 / 4);
  assert(level(full, 375, early) > -13); assert(level(full, 1500, early) < -60);
  assert(level(full, 1500, late) > -13); assert(level(full, 375, late) < -60);
});

test('channel one preserves antiphase energy and excludes right-only audio', () => {
  const mono = tone(.25), stereo = new Int16Array(mono.length * 2);
  for (let i = 0; i < mono.length; i++) { stereo[2 * i] = mono[i]; stereo[2 * i + 1] = -mono[i]; }
  const before = stereo.slice();
  const left = analyzeSpectrogram({pcm: mono, channels: 1, sampleRate: rate});
  const antiphase = analyzeSpectrogram({pcm: stereo, channels: 2, sampleRate: rate});
  for (let i = 0; i < left.panels.length; i++) assert.deepEqual(antiphase.panels[i].dbfs, left.panels[i].dbfs);
  assert.deepEqual(stereo, before);
  for (let i = 0; i < mono.length; i++) stereo[2 * i] = 0;
  const rightOnly = analyzeSpectrogram({pcm: stereo, channels: 2, sampleRate: rate});
  for (const panel of rightOnly.panels) assert(panel.dbfs.every(value => value === -Infinity));
});

test('analysis validates PCM dimensions and respects Nyquist and duration limits', () => {
  for (const value of [undefined, {pcm: []}, {pcm: new Int16Array(3), channels: 2, sampleRate: rate},
    {pcm: new Int16Array(2), channels: 0, sampleRate: rate},
    {pcm: new Int16Array(2), channels: 1, sampleRate: 0}]) assert.throws(() => analyzeSpectrogram(value), /complete interleaved PCM/);
  assert.throws(() => analyzeSpectrogram({pcm: new Int16Array(240001), channels: 1, sampleRate: 800}), /300-second/);
  const analysis = analyzeSpectrogram({pcm: new Int16Array(800), channels: 1, sampleRate: 800});
  for (const panel of analysis.panels) {
    assert(panel.maximumHz <= 400); assert(panel.minimumHz > 0); assert(panel.minimumHz < panel.maximumHz);
  }
});

function canvas() {
  const value = {width: 0, height: 0, images: [], labels: []};
  value.getContext = kind => {
    assert.equal(kind, '2d');
    return {createImageData: (width, height) => ({width, height, data: new Uint8ClampedArray(width * height * 4)}),
      putImageData: (image, x, y) => value.images.push({image, x, y}),
      fillText: text => value.labels.push(text), fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}};
  };
  return value;
}
function workers(t, automatic = true) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'Worker'), instances = [];
  class Worker {
    constructor(url, options) {
      assert(url.pathname.endsWith('/web/spectrum-worker.mjs')); assert.equal(options.type, 'module');
      this.terminated = false; instances.push(this);
    }
    postMessage(input, transfer) {
      assert.equal(transfer.length, 1); assert.equal(transfer[0], input.pcm.buffer);
      this.message = structuredClone(input, {transfer});
      if (automatic) queueMicrotask(() => {
        if (this.terminated) return;
        const analysis = analyzeSpectrogram(this.message, fraction => this.onmessage({data: {type: 'progress', fraction}}));
        if (!this.terminated) this.onmessage({data: {type: 'result', analysis}});
      });
    }
    terminate() { this.terminated = true; }
  }
  Object.defineProperty(globalThis, 'Worker', {value: Worker, configurable: true, writable: true});
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'Worker', previous);
    else delete globalThis.Worker;
  });
  return instances;
}

test('async drawing labels both panels and transfers only a separate channel-one copy', async t => {
  const instances = workers(t), mono = tone(.25), pcm = new Int16Array(mono.length * 2);
  for (let i = 0; i < mono.length; i++) { pcm[2 * i] = mono[i]; pcm[2 * i + 1] = -mono[i]; }
  const before = pcm.slice(), target = canvas(), progress = [];
  const pending = spectrogram({pcm, channels: 2, sampleRate: rate}, target, {onProgress: fraction => progress.push(fraction)});
  assert.equal(target.images.length, 0, 'The initiating UI task must not do the FFT/drawing');
  const metadata = await pending;
  assert.equal(instances.length, 1); assert(instances[0].terminated);
  assert.equal(instances[0].message.channels, 1); assert.deepEqual(instances[0].message.pcm, mono);
  assert.deepEqual(pcm, before); assert.equal(pcm.byteLength, before.byteLength);
  assert.equal(metadata.width, 1600); assert.equal(metadata.height, 1000);
  assert.equal(metadata.channel, 1); assert.equal(metadata.sourceChannels, 2);
  assert.equal(target.images.length, 2);
  assert(target.labels.some(label => /Channel 1 of 2/.test(label)));
  assert(target.labels.includes('Time (s)')); assert(target.labels.includes('dBFS'));
  assert(target.labels.some(label => /90 dB/.test(label)));
  assert.equal(progress[0], 0); assert.equal(progress.at(-1), 1);
  assert(progress.every((value, index) => index === 0 || value >= progress[index - 1]));
  assert(metadata.panels.every(panel => !Object.hasOwn(panel, 'dbfs')), 'Return compact metadata, not duplicate grids');
});

test('abort terminates analysis and ignores queued late drawing', async t => {
  const instances = workers(t, false), target = canvas(), controller = new AbortController();
  const result = {pcm: tone(.1), channels: 1, sampleRate: rate};
  const pending = spectrogram(result, target, {signal: controller.signal});
  controller.abort(); await assert.rejects(pending, {name: 'AbortError'});
  assert(instances[0].terminated);
  instances[0].onmessage({data: {type: 'result', analysis: analyzeSpectrogram(result)}});
  assert.equal(target.images.length, 0); assert.equal(target.width, 0);
  await assert.rejects(spectrogram(result, target, {signal: controller.signal}), {name: 'AbortError'});
  assert.equal(instances.length, 1, 'Already aborted requests must not start a worker');
});

test('worker errors terminate analysis without exporting a new image', async t => {
  const instances = workers(t, false), target = canvas();
  const pending = spectrogram({pcm: tone(.1), channels: 1, sampleRate: rate}, target);
  instances[0].onerror({message: 'worker failed'});
  await assert.rejects(pending, /worker failed/);
  assert(instances[0].terminated); assert.equal(target.images.length, 0);
});

test('the full 300-second profile has continuous coverage and bounded display storage', t => {
  const pcm = new Int16Array(rate * 300), cycle = tone(128 / rate);
  for (let i = 0; i < pcm.length; i++) pcm[i] = cycle[i % cycle.length];
  const started = performance.now();
  const analysis = analyzeSpectrogram({pcm, channels: 1, sampleRate: rate});
  const elapsed = performance.now() - started;
  assert.equal(analysis.durationSeconds, 300);
  assert.deepEqual(analysis.panels.map(panel => panel.windowCount), [3517, 28126]);
  assert.equal(analysis.panels.reduce((bytes, panel) => bytes + panel.dbfs.byteLength, 0), 3484800);
  for (const panel of analysis.panels) {
    assert.equal(panel.hopSize, panel.fftSize / 2);
    for (let x = 0; x < panel.columns; x++) assert(columnPeak(panel, x) > -20, 'Every time column must contain the persistent tone');
  }
  t.diagnostic(`Full 300-second, 48 kHz dense PCM analysis: ${Math.round(elapsed)} ms; 3,484,800 bytes of display grids.`);
});


test('saved and inline note plots share native pitch, gate and stream geometry without PCM', () => {
  const result = {info:{voices:'64',length_ns:'4000000000'},events:[
    {kind:'on',time_ns:'0',voice:'0',freq_millihz:'440000',velocity:'12000',stream:'lead',patch:'<lead>'},
    {kind:'pitch',time_ns:'1000000000',voice:'0',freq_millihz:'880000'},
    {kind:'off',time_ns:'2000000000',voice:'0'},
    {kind:'pitch',time_ns:'2500000000',voice:'0',freq_millihz:'220000'},
    {kind:'on',time_ns:'3000000000',voice:'63',freq_millihz:'330000',velocity:'32767',stream:'lead'},
  ]};
  const before=JSON.stringify(result), model=nativeTimeline(result), geometry=pianoRollGeometry(model), svg=toSvg(result);
  assert.equal(JSON.stringify(result),before,'display indexing leaves native events untouched');
  assert.deepEqual(model.notes.map(({voice,start,end,frequency})=>[voice,start,end,frequency]),
    [[0,0,1,440],[0,1,2,880],[63,3,4,330]]);
  assert.ok(geometry.bars[1].y<geometry.bars[0].y);
  assert.equal(geometry.bars[0].color,geometry.bars[2].color);
  for(const bar of geometry.bars) {
    assert.ok(svg.includes(`x="${bar.x}" y="${bar.y}" width="${bar.width}" height="${bar.height}"`));
    assert.ok(svg.includes(`data-voice="${bar.voice}" data-start="${bar.start}" data-end="${bar.end}"`));
  }
  const outer=svg.slice(0,svg.indexOf('>'));
  assert.match(outer,/width="100%"/);assert.match(outer,/height="346"/);
  assert.doesNotMatch(outer,/viewBox/,'64 voices do not turn the document into a portrait image');
  assert.match(svg,/&lt;lead&gt;/);assert.doesNotMatch(svg,/<lead>/);
});

test('note export refuses incomplete native events rather than presenting a partial chart', () => {
  const base={info:{voices:'1',length_ns:'1000000000'},events:[]};
  for(const change of [
    {eventsError:'native listing failed'}, {events:undefined},
    {events:new Array(20001).fill({kind:'off',voice:'0',time_ns:'0'})},
    {events:[{kind:'on',voice:'7',time_ns:'0',freq_millihz:'440000'}]},
    {events:[{kind:'move',voice:'0',time_ns:'0',x:'bad',y:'0',z:'0',duration_ns:'1'}]},
    {events:[{kind:'pitch',voice:'0',time_ns:'0',freq_millihz:'NaN'}]},
  ]) assert.throws(()=>toSvg({...base,...change}),/unavailable|limit|incomplete/);
  assert.match(toSvg(base),/No note gates/);
});

test('selected note preview names pitches and detuning with an escaped visible stream key', () => {
  const result={info:{voices:'2',length_ns:'1000000000'},events:[
    {kind:'on',voice:'0',time_ns:'0',freq_millihz:String(Math.round(440000*2**(.25/12))),stream:'<lead>'},
    {kind:'on',voice:'1',time_ns:'0',freq_millihz:'261626',stream:'bass'}]};
  const before=JSON.stringify(result), preview=songPreview(result);
  assert.match(preview.text,/C4, A4 \(\+25¢\)/);
  assert.match(preview.text,/no audio was rendered or verified/);
  assert.match(preview.svg,/>C4<.*>C5</s);
  assert.match(preview.svg,/A4 \(\+25¢\)/);
  assert.match(preview.legend,/&lt;lead&gt;/);assert.doesNotMatch(preview.legend,/<lead>/);
  for(const bar of pianoRollGeometry(nativeTimeline(result)).bars) assert.ok(preview.legend.includes(bar.color));
  assert.equal(JSON.stringify(result),before,'Naming notes does not quantize or change native frequencies');
  const many=songPreview({info:{voices:'1',length_ns:'1000000000'},events:Array.from({length:40},(_,i)=>
    ({kind:'on',voice:'0',time_ns:String(i*20000000),freq_millihz:'440000',stream:`part${i}`}))});
  assert.equal((many.legend.match(/border-left-color/g)||[]).length,32);
  assert.match(many.legend,/8 more streams/);assert.doesNotMatch(many.text,/part39/);
});
