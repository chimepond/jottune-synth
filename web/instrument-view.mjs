// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// A display of native event commands and cached PCM; this module never owns audio.
import {createCursorSpectrum, nativeTimeline, pianoRollGeometry} from './media.mjs';
import {createSpatialView} from './spatial-view.mjs';
const WINDOW_FRAMES = 512;
const COLORS = ['#eabf6c', '#87d7b3', '#95c6ee', '#d9a8ed', '#eea896', '#d6db8a'];
const NOTES = ['C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B'];
const SVG = 'http://www.w3.org/2000/svg';
const EMPTY_GATE = Object.freeze({gate: false, frequency: null, velocity: null, patch: null});

function upperBound(events, seconds, inclusive = true) {
  let lo = 0, hi = events.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (events[mid].seconds < seconds || (inclusive && events[mid].seconds === seconds)) lo = mid + 1; else hi = mid;
  }
  return lo;
}

export function noteLabel(frequency) {
  if (!(frequency > 0)) return null;
  const midi = Math.round(69 + 12 * Math.log2(frequency / 440));
  const pitchClass = ((midi % 12) + 12) % 12;
  return {pitchClass, name: NOTES[pitchClass] + (Math.floor(midi / 12) - 1)};
}

// Index once. Playback and backwards seeks use binary searches, never timeline scans.
export function instrumentModel(result, {spatialSampler = null, spatialWarning = null} = {}) {
  const pcm = result?.pcm, channels = Number(result?.channels), sampleRate = Number(result?.sampleRate);
  if (!(pcm instanceof Int16Array) || !Number.isInteger(channels) || channels < 1 || channels > 16
      || !Number.isInteger(sampleRate) || sampleRate < 1 || !pcm.length || pcm.length % channels)
    throw Error('A complete PCM render is required for the instrument display.');
  const route = Number(result.info?.route || 0);
  const frames = pcm.length / channels, raw = route === 1 || route === 2;
  const channelLabels = Array.from({length: channels}, (_, channel) => raw ? `Coefficient ${channel}`
    : channels === 1 ? 'Mono' : channels === 2 ? ['Left', 'Right'][channel] : `Channel ${channel + 1}`);
  const routeLabel = raw ? `Raw field · ${channels} coefficients` : route === 3 ? 'Binaural · left / right'
    : route === 4 ? 'Pan-law stereo' : channels === 2 ? 'Stereo' : channels === 1 ? 'Mono' : `${channels} channels`;
  const model = {...nativeTimeline(result, frames / sampleRate), pcm, channels, sampleRate, frames,
    raw, channelLabels, routeLabel, spatial:null, spatialSampler:null, spatialPaths:[],
    spatialExtent:1200, spatialWarning:spatialWarning || result.spatialError || null};
  for (const voice of model.voices) for (const point of voice.commands)
    model.spatialExtent = Math.max(model.spatialExtent,Math.hypot(point.x,point.y,point.z)*1.15);
  const metadata = result.spatial;
  if (metadata && spatialSampler) {
    const valid = metadata.format === 1 && metadata.frames === frames && metadata.voices === model.voices.length
      && [32,64,128].includes(metadata.block) && Number.isInteger(metadata.near) && metadata.near > 0
      && Array.isArray(metadata.paths) && metadata.paths.length > 0 && metadata.paths.length <= 20000
      && typeof spatialSampler.positionsAt === 'function' && typeof spatialSampler.yawAt === 'function';
    if (valid && metadata.paths.every(point => point && Number.isInteger(point.voice) && model.voices[point.voice]
        && Number.isInteger(point.sample) && point.sample >= 0 && (point.pen === 0 || point.pen === 1)
        && ['x','y','z'].every(key => Number.isInteger(point[key]) && Math.abs(point[key]) <= 16777216))) {
      model.spatial = metadata;model.spatialSampler = spatialSampler;
      model.spatialPaths = model.voices.map(() => []);model.spatialExtent = Math.max(1200,metadata.near*1.5);
      for (const point of metadata.paths) {
        model.spatialPaths[point.voice].push(point);
        model.spatialExtent = Math.max(model.spatialExtent,Math.hypot(point.x,point.y,point.z)*1.15);
      }
    } else model.spatialWarning = 'Native spatial data does not match this render.';
  }
  return model;
}

export function instrumentSnapshot(model, seconds, {peaks: includePeaks = true} = {}) {
  const time = Math.max(0, Math.min(model.duration, Number.isFinite(seconds) ? seconds : 0));
  const frame = Math.min(model.frames - 1, Math.floor(time * model.sampleRate));
  const start = Math.max(0, frame - WINDOW_FRAMES / 2), end = Math.min(model.frames, start + WINDOW_FRAMES);
  const peaks = new Array(model.channels).fill(0);
  if (includePeaks) for (let i = start; i < end; i++) for (let channel = 0; channel < model.channels; channel++)
    peaks[channel] = Math.max(peaks[channel], Math.abs(model.pcm[i * model.channels + channel]) / 32768);
  let spatial = null;
  if (model.spatialSampler) {
    try {
      const positions = model.spatialSampler.positionsAt(frame), yaw = model.spatialSampler.yawAt(frame);
      if (!Array.isArray(positions) || positions.length !== model.voices.length || !Number.isInteger(yaw)
          || positions.some(position => !position || !['x','y','z'].every(key => Number.isInteger(position[key]) && Math.abs(position[key]) <= 16777216)))
        throw Error('Incomplete native positions');
      spatial = {positions,yaw,frame:Math.floor(frame/model.spatial.block)*model.spatial.block};
    } catch {
      model.spatialSampler = null;model.spatialWarning = 'Native position sampling is unavailable.';
    }
  }
  return {time, frame, start, end, peaks, spatial, voices: model.voices.map(voice => ({
    voice: voice.voice,
    gate: model.eventError ? EMPTY_GATE : voice.gates[upperBound(voice.gates, time) - 1] || EMPTY_GATE,
    command: model.eventError ? null : voice.commands[upperBound(voice.commands, time) - 1] || null,
    position: spatial?.positions[voice.voice] || null,
  }))};
}

export function createInstrumentView(container, {onSeek} = {}) {
  const document = container.ownerDocument;
  const make = (tag, id, text, className) => {
    const node = document.createElement(tag);
    if (id) node.id = id;
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const svg = (tag, attributes = {}) => {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    return node;
  };
  const root = make('div', null, null, 'instrument');
  const header = make('div', null, null, 'instrument-header');
  const route = make('strong', 'instrument-route', 'Live instrument');
  const clock = make('span', 'instrument-clock', '0.00 s');
  header.append(route, clock);
  const status = make('p', 'instrument-status', 'Load a song to inspect its audio, voices and spatial targets.', 'instrument-caption');
  status.setAttribute('role', 'status');
  const roll = make('section', 'instrument-roll', null, 'instrument-roll');
  roll.append(make('h3', null, 'Piano roll · frequency (Hz)'));
  const rollChart = make('div', null, null, 'instrument-roll-chart');
  const pitchAxis = make('div', 'instrument-pitch-axis', null, 'instrument-pitch-axis');
  pitchAxis.setAttribute('aria-hidden', 'true');
  const rollSvg = svg('svg', {id:'instrument-roll-plot', viewBox:'0 0 1000 214', preserveAspectRatio:'none', role:'img',
    'aria-label':'Native note gates: time left to right, pitch low to high'});
  const rollDescription = svg('desc'), rollNotes = svg('g', {id:'instrument-roll-notes'}), rollMarks = svg('g');
  const rollHead = svg('line', {id:'instrument-playhead', x1:0, x2:0, y1:0, y2:214, stroke:'#f4f8fa', 'stroke-width':1.5});
  rollSvg.append(rollDescription, rollNotes, rollMarks, rollHead);rollChart.append(pitchAxis, rollSvg);
  const rollTimes = make('div', 'instrument-roll-times', null, 'instrument-roll-times');
  const legend = make('div', 'instrument-roll-legend', null, 'instrument-roll-legend');
  roll.append(rollChart, rollTimes, legend, make('p', null,
    'Native gates, not release tails or audible pitch modulation. Colours identify streams; the bottom strip marks commands.', 'instrument-caption'));
  const toggles = make('div', null, null, 'instrument-panel-controls');
  toggles.setAttribute('role','group');toggles.setAttribute('aria-label','Live audio panels');
  const enabled = {};
  for (const name of ['Scope','Spectrum','Peaks']) {
    const key = name.toLowerCase(), label = make('label', null, name);
    const input = make('input', 'instrument-show-'+key);input.type = 'checkbox';input.checked = true;
    label.prepend(input);toggles.append(label);enabled[key] = input;
  }
  const audioGrid = make('div', null, null, 'instrument-audio-grid');
  const grid = make('div', null, null, 'instrument-grid');
  const audio = make('section', null, null, 'instrument-audio');
  audio.setAttribute('aria-label', 'Rendered PCM at playback cursor');
  const scopeTitle = make('h3', null, 'Scope');
  const scope = make('canvas', 'instrument-scope');
  scope.width = 720; scope.height = 176;
  scope.setAttribute('role', 'img');
  scope.setAttribute('aria-label', 'Rendered waveform at the playback cursor');
  const scopeNote = make('p', null, '512-frame window at the cursor; not a device-output measurement.', 'instrument-caption');
  const meters = make('div', 'instrument-meters');
  audio.append(scopeTitle, scope, scopeNote);
  const spectrumPanel = make('section', 'instrument-spectrum-panel', null, 'instrument-audio');
  spectrumPanel.append(make('h3', null, 'Spectrum'));
  const spectrumControls = make('div', 'instrument-spectrum-controls', null, 'instrument-spectrum-controls');
  spectrumControls.setAttribute('role','group');spectrumControls.setAttribute('aria-label','Spectrum channels');
  const spectrumButtons = new Map();
  for (const [mode,label] of [['both','Both'],['0','Left'],['1','Right']]) {
    const button = make('button', 'instrument-spectrum-'+(mode === '0' ? 'left' : mode === '1' ? 'right' : mode), label);
    button.type = 'button';button.disabled = true;button.setAttribute('aria-pressed',String(mode === 'both'));
    spectrumButtons.set(mode,button);spectrumControls.append(button);
  }
  const monoLabel = make('p', 'instrument-spectrum-mono', 'Mono', 'instrument-caption');
  const channelLabel = make('label', 'instrument-spectrum-channel-label', 'Channel '), channelSelect = make('select', 'instrument-spectrum-channel');
  channelLabel.append(channelSelect);
  const spectrumChart = make('div', null, null, 'instrument-spectrum-chart');
  const dbAxis = make('div', null, null, 'instrument-db-axis');
  for (const text of ['0','−30','−60','−90']) dbAxis.append(make('span', null, text));
  const spectrumSvg = svg('svg', {id:'instrument-spectrum', viewBox:'0 0 720 176', preserveAspectRatio:'none', role:'img',
    'aria-label':'Selected channel short-time spectrum, logarithmic frequency, −90 to 0 dBFS'});
  spectrumSvg.append(svg('path', {d:'M0 0H720M0 58.67H720M0 117.33H720M0 176H720', stroke:'#304650'}));
  const spectrumCurve = svg('path', {id:'instrument-spectrum-curve', fill:'none', stroke:COLORS[0], 'stroke-width':1.5});
  const spectrumSecond = svg('path', {id:'instrument-spectrum-curve-secondary', fill:'none', stroke:COLORS[1], 'stroke-width':1.5, 'stroke-dasharray':'4 2'});
  const spectrumCurves = [spectrumCurve,spectrumSecond];
  spectrumSvg.append(...spectrumCurves);spectrumChart.append(dbAxis, spectrumSvg);spectrumPanel.append(spectrumChart);
  const frequencyAxis = make('div', 'instrument-spectrum-axis', null, 'instrument-spectrum-axis');
  const spectrumNote = make('p', null, '2048-frame Hann window · dBFS · not device output or a full-song spectrogram.', 'instrument-caption');
  const spectrumFooter = make('div', null, null, 'instrument-spectrum-footer');
  spectrumFooter.append(spectrumControls, monoLabel, channelLabel);
  spectrumPanel.append(frequencyAxis, spectrumNote, spectrumFooter);
  const peaksPanel = make('section', 'instrument-peaks-panel', null, 'instrument-audio');
  peaksPanel.append(make('h3', null, 'Peaks'), meters, make('p', null,
    'Independent channel peaks in the current 512-frame PCM window; not device output.', 'instrument-caption'));
  audioGrid.append(audio, spectrumPanel, peaksPanel);
  const spatialView = createSpatialView(document,{colors:COLORS,onSelectVoice:voice => selectVoice(voice)});
  const navigation = make('div', null, null, 'instrument-navigation');
  const previous = make('button', 'instrument-prev', 'Previous target');
  const next = make('button', 'instrument-next', 'Next target');
  previous.type = next.type = 'button'; previous.disabled = next.disabled = true;
  navigation.append(previous, next);
  spatialView.element.append(navigation);grid.append(spatialView.element);
  const voiceTitle = make('h3', null, 'Voices · select to inspect');
  const voices = make('div', 'instrument-voices');voices.setAttribute('role', 'group');voices.setAttribute('aria-label', 'Select a voice');
  const detail = make('p', 'instrument-detail', 'No rendered voice selected.', 'instrument-detail');
  detail.setAttribute('tabindex', '0');
  const keyboardTitle = make('p', null, 'Pitch classes · all octaves combined · native note gates, not release tails', 'instrument-caption');
  const keyboard = make('div', 'instrument-keyboard');
  keyboard.setAttribute('role', 'img');keyboard.setAttribute('aria-label', 'No open note gates');
  const keys = NOTES.map((name, pitchClass) => {
    const key = make('span', null, name, `instrument-key${name.includes('♯') ? ' black' : ''}`);
    key.dataset.pitchClass = String(pitchClass);key.dataset.gate = 'closed';key.setAttribute('aria-hidden', 'true');
    keyboard.append(key);return key;
  });
  const voicePanel = make('section', null, null, 'instrument-voice-panel');
  voicePanel.append(voiceTitle, voices, detail, keyboardTitle, keyboard);grid.append(voicePanel);
  root.append(header, status, roll, toggles, audioGrid, grid);
  container.replaceChildren(root);
  const context = scope.getContext('2d');
  const reducedMotion = document.defaultView?.matchMedia?.('(prefers-reduced-motion: reduce)');
  let model = null, selected = 0, visible = true, lastTime = 0, lastPlaying = false;
  let pendingResult = null, analyzeSpectrum = null, spectrumFrame = null, spectrumMode = 'both', rollReady = false;
  let pendingSpatialOptions = {}, currentResult = null, preferredVoice = null;
  let voiceButtons = [], meterNodes = [], drawnFrame = null;
  const changeText = (node, text) => {if (node.textContent !== text) node.textContent = text;};

  function buildRoll() {
    if (!model || rollReady || !visible) return;
    rollReady = true;
    const geometry = pianoRollGeometry(model);
    for (const tick of geometry.ticks) {
      rollNotes.append(svg('path', {d:`M0 ${tick.y}H1000`, stroke:'#304650', 'stroke-width':.6}));
      const label = make('span', null, tick.label);
      label.style.top = (tick.y / 214 * 100)+'%';pitchAxis.append(label);
    }
    for (const note of geometry.bars) {
      const bar = svg('rect', {x:note.x, y:note.y, width:note.width, height:note.height, rx:1, fill:note.color});
      bar.dataset.voice = String(note.voice);bar.dataset.start = String(note.start);bar.dataset.end = String(note.end);
      const title = svg('title');title.textContent = `Stream ${note.stream} · voice ${note.voice} · ${note.frequency} Hz · ${note.start.toFixed(3)}–${note.end.toFixed(3)} s`;
      bar.append(title);rollNotes.append(bar);
    }
    const markColors = {pos:'#76d8e3',move:'#70c5ad',send:'#d9a8ed',filter:'#eead74',yaw:'#eabf6c'};
    rollMarks.append(svg('rect', {x:0,y:201,width:1000,height:13,fill:'#283941'}));
    for (const mark of model.marks) rollMarks.append(svg('rect', {x:mark.seconds / model.duration * 1000,
      y:203,width:1.5,height:9,fill:markColors[mark.kind] || '#a6b6be'}));
    for (let i = 0; i <= 4; i++) rollTimes.append(make('span', null, i === 0 ? '0 s' : (model.duration * i / 4).toFixed(model.duration < 10 ? 2 : 1)+' s'));
    for (const [i, stream] of model.streams.entries()) {
      const label = make('span', null, 'Stream '+stream);label.style.setProperty('--stream-color', COLORS[i % COLORS.length]);legend.append(label);
    }
    const kinds = [...new Set(model.marks.map(mark => mark.kind))];
    if (kinds.length) legend.append(make('span', null, 'Commands: '+kinds.join(', ')));
    rollDescription.textContent = model.eventError || `${model.notes.length} native note-gate segments, ${model.duration.toFixed(2)} seconds. Time is horizontal; frequency rises vertically. Pitch commands split notes. The white line is the playback cursor.`;
    if (!model.notes.length) legend.append(make('span', null, model.eventError ? 'Native events unavailable.' : 'No note gates in this record.'));
  }

  function drawSpectrum(snapshot) {
    const channels = spectrumMode === 'both' ? [0,1] : [Number(spectrumMode)], key = snapshot.frame+':'+channels.join(',');
    if (spectrumFrame === key) return;
    analyzeSpectrum ||= createCursorSpectrum(model);
    const maximum = model.sampleRate / 2, minimum = Math.min(20, maximum / 16), span = Math.log(maximum / minimum);
    for (let index = 0; index < spectrumCurves.length; index++) {
      const curve = spectrumCurves[index], channel = channels[index];
      if (channel === undefined) {curve.setAttribute('d','');delete curve.dataset.channel;continue;}
      // The analyzer reuses its FFT storage. Finish each independent channel's
      // curve before requesting the next one; never sum or average stereo PCM.
      const analysis = analyzeSpectrum(snapshot.time,channel), points = [];
      for (let bin = 1; bin < analysis.dbfs.length; bin++) {
        const hz = bin * analysis.binHz;if (hz < minimum) continue;
        points.push(`${points.length ? 'L' : 'M'}${(Math.log(hz / minimum) / span * 720).toFixed(2)} ${(Math.max(0, Math.min(90, -analysis.dbfs[bin])) / 90 * 176).toFixed(2)}`);
      }
      curve.setAttribute('d',points.join(''));curve.setAttribute('stroke',COLORS[channel % COLORS.length]);
      curve.setAttribute('stroke-dasharray',channel === 1 && model.channels === 2 && !model.raw ? '4 2' : 'none');
      curve.dataset.channel = String(channel);
      spectrumSvg.dataset.windowStart = String(analysis.start);spectrumSvg.dataset.windowEnd = String(analysis.end);
    }
    spectrumSvg.dataset.frame = String(snapshot.frame);spectrumSvg.dataset.channel = spectrumMode;
    spectrumSvg.setAttribute('aria-label',spectrumMode === 'both'
      ? 'Independent Left and Right spectra, matching Scope colours; logarithmic frequency, −90 to 0 dBFS'
      : `${model.channelLabels[channels[0]]} spectrum; logarithmic frequency, −90 to 0 dBFS`);
    spectrumFrame = key;
  }

  function panelSelection() {
    audio.hidden = !enabled.scope.checked;spectrumPanel.hidden = !enabled.spectrum.checked;peaksPanel.hidden = !enabled.peaks.checked;
    update(lastTime, lastPlaying, true);
  }
  for (const control of Object.values(enabled)) control.addEventListener('change', panelSelection);
  function selectSpectrum(mode) {
    spectrumMode = mode;spectrumFrame = null;
    for (const [value,button] of spectrumButtons) button.setAttribute('aria-pressed',String(value === mode));
    update(lastTime,lastPlaying,true);
  }
  for (const [mode,button] of spectrumButtons) button.addEventListener('click',() => selectSpectrum(mode));
  channelSelect.addEventListener('change', () => selectSpectrum(channelSelect.value || '0'));

  function drawScope(snapshot) {
    if (!context) return;
    context.clearRect(0, 0, scope.width, scope.height);
    context.strokeStyle = '#304650';context.lineWidth = 1;
    context.beginPath();
    for (const center of model?.channels === 1 ? [88] : [44,132]) {context.moveTo(0, center);context.lineTo(720, center);}
    context.stroke();
    if (!model || !snapshot) return;
    const displayed = Math.min(model.channels, 2);
    for (let channel = 0; channel < displayed; channel++) {
      const center = displayed === 1 ? 88 : channel * 88 + 44;
      context.strokeStyle = COLORS[channel];context.lineWidth = 1.4;context.beginPath();
      for (let frame = snapshot.start; frame < snapshot.end; frame++) {
        const x = (frame - snapshot.start) / Math.max(1, snapshot.end - snapshot.start - 1) * 720;
        const y = center - model.pcm[frame * model.channels + channel] / 32768 * (displayed === 1 ? 76 : 37);
        if (frame === snapshot.start) context.moveTo(x, y); else context.lineTo(x, y);
      }
      context.stroke();
    }
    drawnFrame = snapshot.frame;
    scope.dataset.frame = String(snapshot.frame);
    scope.dataset.windowStart = String(snapshot.start);scope.dataset.windowEnd = String(snapshot.end);
  }
  function selectVoice(voice) {
    if (!model || !model.voices[voice]) return;
    selected = voice;
    update(lastTime, lastPlaying, true);
  }
  function seekCommand(direction) {
    if (!model || typeof onSeek !== 'function') return;
    const commands = model.voices[selected]?.commands || [];
    const index = direction < 0 ? upperBound(commands, lastTime, false) - 1 : upperBound(commands, lastTime);
    if (commands[index] && commands[index].seconds <= model.duration) onSeek(commands[index].seconds);
  }
  previous.addEventListener('click', () => seekCommand(-1));
  next.addEventListener('click', () => seekCommand(1));
  function update(seconds, playing = false, force = false) {
    lastTime = Number.isFinite(seconds) ? seconds : 0;lastPlaying = Boolean(playing);
    if (!model || !visible) return;
    const snapshot = instrumentSnapshot(model, lastTime, {peaks: enabled.peaks.checked});
    buildRoll();
    container.dataset.time = String(snapshot.time);container.dataset.frame = String(snapshot.frame);
    container.dataset.playing = String(lastPlaying);container.dataset.selectedVoice = String(selected);
    changeText(clock, `${snapshot.time.toFixed(2)} s · ${lastPlaying ? 'playing' : 'paused'}`);
    const reduced = Boolean(reducedMotion?.matches);
    if (enabled.scope.checked && (force || !lastPlaying || !reduced || drawnFrame === null) && (force || drawnFrame !== snapshot.frame)) drawScope(snapshot);
    if (enabled.spectrum.checked && (force || !lastPlaying || !reduced || spectrumFrame === null)) drawSpectrum(snapshot);
    if (force || !lastPlaying || !reduced) {
      const x = snapshot.time / model.duration * 1000;rollHead.setAttribute('x1', String(x));rollHead.setAttribute('x2', String(x));
      rollHead.dataset.time = String(snapshot.time);
    }
    scope.dataset.motion = reduced && lastPlaying ? 'held' : 'live';
    changeText(scopeNote, `${model.channels > 2 ? 'Scope: first two channels. ' : ''}512-frame PCM window; not device output.${reduced ? ' Reduced motion: waveform holds during playback.' : ''}`);
    if (enabled.peaks.checked) for (let channel = 0; channel < model.channels; channel++) {
      const peak = snapshot.peaks[channel], db = peak ? `${(20 * Math.log10(peak)).toFixed(1)} dBFS` : '−∞ dBFS';
      meterNodes[channel].meter.value = peak;meterNodes[channel].meter.setAttribute('aria-valuetext', db);
      changeText(meterNodes[channel].value, db);
    }
    const classes = Array.from({length: 12}, () => []), open = [];
    for (const state of snapshot.voices) {
      const button = voiceButtons[state.voice], active = state.gate.gate;
      button.dataset.gate = active ? 'open' : 'closed';
      button.setAttribute('aria-pressed', String(state.voice === selected));
      const note = noteLabel(state.gate.frequency);
      button.setAttribute('aria-label', `Voice ${state.voice}, ${model.eventError ? 'events unavailable' : active ? 'gate open' : 'gate closed'}${active && note ? ', ' + note.name : ''}`);
      if (active && note) {classes[note.pitchClass].push(state.voice);open.push(`voice ${state.voice} ${note.name}`);}
    }
    if (force || !lastPlaying || !reduced) spatialView.update(snapshot,selected);
    changeText(status,model.eventError || (snapshot.spatial
      ? 'Voice lights show note gates; spatial positions and paths follow native playback.'
      : 'Voice lights show note gates. Spatial dots show commanded targets, not actual DSP positions.'));
    for (let i = 0; i < keys.length; i++) {
      keys[i].dataset.gate = classes[i].length ? 'open' : 'closed';
      keys[i].dataset.selected = String(classes[i].includes(selected));
      keys[i].title = `${NOTES[i]}: ${classes[i].length ? 'voices ' + classes[i].join(', ') : 'no open gates'}`;
    }
    keyboard.setAttribute('aria-label', model.eventError ? 'Note events unavailable' : open.length ? 'Open note gates: ' + open.join('; ') : 'No open note gates');
    const chosen = snapshot.voices[selected], gate = chosen?.gate || EMPTY_GATE, command = chosen?.command;
    const pitch = noteLabel(gate.frequency);
    const description = model.eventError ? 'Native events unavailable.' : `Voice ${selected} · gate ${gate.gate ? 'open' : 'closed'}${pitch ? ` · ${pitch.name} (nearest note), ${gate.frequency.toFixed(3)} Hz` : ''}${gate.patch ? ` · ${gate.patch}` : ''}.`;
    const position = command ? ` ${command.kind === 'move' ? 'Move target' : 'Position command'} (${command.x}, ${command.y}, ${command.z}) mm, issued at ${command.seconds.toFixed(3)} s${command.kind === 'move' ? ` over ${command.duration.toFixed(3)} s` : ''}.` : ' No spatial command has been issued for this voice.';
    const currentPosition = chosen?.position ? ` Native position (${chosen.position.x}, ${chosen.position.y}, ${chosen.position.z}) mm.` : '';
    changeText(detail, description + currentPosition + (model.eventError ? '' : position));
    const commands = model.voices[selected]?.commands || [], boundary = upperBound(commands, snapshot.time);
    previous.disabled = !onSeek || upperBound(commands, snapshot.time, false) === 0;
    next.disabled = !onSeek || boundary >= commands.length || commands[boundary].seconds > model.duration;
  }
  function clear() {
    model = null;pendingResult = null;analyzeSpectrum = null;spectrumFrame = null;rollReady = false;
    lastTime = 0;lastPlaying = false;selected = 0;drawnFrame = null;
    voiceButtons = [];meterNodes = [];pendingSpatialOptions = {};currentResult = null;preferredVoice = null;
    voices.replaceChildren();meters.replaceChildren();spatialView.clear();
    rollNotes.replaceChildren();rollMarks.replaceChildren();pitchAxis.replaceChildren();rollTimes.replaceChildren();legend.replaceChildren();
    rollDescription.textContent = 'No native note gates loaded.';rollHead.setAttribute('x1','0');rollHead.setAttribute('x2','0');delete rollHead.dataset.time;
    for (const curve of spectrumCurves) {curve.setAttribute('d','');delete curve.dataset.channel;}
    spectrumControls.hidden = true;monoLabel.hidden = true;channelLabel.hidden = true;
    for (const button of spectrumButtons.values()) button.disabled = true;
    spectrumMode = 'both';channelSelect.replaceChildren();frequencyAxis.replaceChildren();
    for (const key of ['frame','channel','windowStart','windowEnd']) delete spectrumSvg.dataset[key];
    for (const key of keys) {key.dataset.gate = 'closed';key.dataset.selected = 'false';key.title = '';}
    keyboard.setAttribute('aria-label', 'No open note gates');
    for (const key of ['time', 'frame', 'playing', 'selectedVoice']) delete container.dataset[key];
    for (const key of ['frame', 'windowStart', 'windowEnd', 'motion']) delete scope.dataset[key];
    previous.disabled = next.disabled = true;
    route.textContent = 'Live instrument';clock.textContent = '0.00 s';
    status.textContent = 'Load a song to inspect its audio, voices and spatial targets.';
    detail.textContent = 'No rendered voice selected.';
    scope.setAttribute('aria-label', 'Rendered waveform at the playback cursor');
    scopeNote.textContent = '512-frame window at the cursor; not a device-output measurement.';
    if (visible) drawScope(null);
  }
  function setResult(result,spatialOptions = {}) {
    const retainedVoice = result === currentResult ? selected : null;
    const retainedTime = retainedVoice === null ? 0 : lastTime, retainedPlaying = retainedVoice !== null && lastPlaying;
    clear();
    pendingResult = result;currentResult = result;pendingSpatialOptions = spatialOptions;preferredVoice = retainedVoice;
    lastTime = retainedTime;lastPlaying = retainedPlaying;
    if (visible) installResult();
  }
  function installResult() {
    const result = pendingResult;pendingResult = null;
    if (result?.verdict !== 'valid') {
      status.textContent = 'Render a valid record to inspect its audio and native events.';
      return;
    }
    try {model = instrumentModel(result,pendingSpatialOptions);} catch (error) {status.textContent = error.message;return;}
    spatialView.setModel(model);
    const stereo = model.channels === 2 && !model.raw;
    spectrumControls.hidden = !stereo;monoLabel.hidden = model.channels !== 1 || model.raw;
    channelLabel.hidden = stereo || (model.channels === 1 && !model.raw);
    spectrumMode = stereo ? 'both' : '0';
    for (const [mode,button] of spectrumButtons) {button.disabled = !stereo;button.setAttribute('aria-pressed',String(mode === spectrumMode));}
    spectrumNote.textContent = stereo
      ? '2048-frame Hann windows · Left gold, Right green/dashed · independent channels, never summed · dBFS, not device output.'
      : '2048-frame Hann window · dBFS · not device output or a full-song spectrogram.';
    route.textContent = model.routeLabel;
    status.textContent = model.eventError || (model.spatialSampler
      ? 'Voice lights show note gates; spatial positions and paths follow native playback.'
      : 'Voice lights show note gates. Spatial dots show commanded targets, not actual DSP positions.');
    scope.setAttribute('aria-label', `Rendered ${model.channelLabels.slice(0, 2).join(' / ')} waveform at the playback cursor`);
    const maximum = model.sampleRate / 2, minimum = Math.min(20, maximum / 16);
    for (let i = 0; i <= 3; i++) {
      const hz = minimum * (maximum / minimum) ** (i / 3);
      frequencyAxis.append(make('span', null, hz >= 1000 ? (hz / 1000).toFixed(1)+'k Hz' : Math.round(hz)+' Hz'));
    }
    for (let channel = 0; channel < model.channels; channel++) {
      const option = make('option', null, model.channelLabels[channel]);option.value = String(channel);channelSelect.append(option);
      const row = make('div', null, null, 'instrument-meter'), label = make('label', null, model.channelLabels[channel]);
      const meter = make('meter', `instrument-meter-${channel}`);meter.min = 0;meter.max = 1;meter.value = 0;
      meter.dataset.channel = String(channel);label.setAttribute('for', meter.id);
      const value = make('span', null, '−∞ dBFS');row.append(label, meter, value);meters.append(row);
      meterNodes.push({meter, value});
    }
    for (const voice of model.voices) {
      const button = make('button', null, String(voice.voice));button.type = 'button';button.dataset.voice = String(voice.voice);
      button.style.setProperty('--voice-color', COLORS[voice.voice % COLORS.length]);
      button.addEventListener('click', () => selectVoice(voice.voice));voices.append(button);voiceButtons.push(button);
    }
    if (model.voices.length) selectVoice(preferredVoice !== null && model.voices[preferredVoice] ? preferredVoice
      : model.voices.find(voice => voice.commands.length)?.voice || 0);
    else update(0, false, true);
  }
  function setVisible(value) {
    const wasVisible = visible;visible = Boolean(value);
    if (visible && (!wasVisible || pendingResult)) {
      if (!model) drawScope(null);
      if (pendingResult) installResult();
      update(lastTime, lastPlaying, true);
    }
  }
  reducedMotion?.addEventListener?.('change', () => {if (visible) update(lastTime, lastPlaying, true);});
  clear();
  return {setResult, update, clear, setVisible};
}
