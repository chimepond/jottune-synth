// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
const encoder = new TextEncoder();
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));
const noteColors = ['#eabf6c', '#87d7b3', '#95c6ee', '#d9a8ed', '#eea896', '#d6db8a'];
const numeric = value => value !== undefined && value !== null && value !== '' && Number.isFinite(Number(value));
// Describes native command gates, never the waveform or musical quality. The
// native listing already orders authored rows, followed by generated rows.
export function songStructure(result) {
  const base = {version:1, scope:'native_gate_commands'};
  const unavailable = reason => ({...base, status:'unavailable', reason});
  const uint = (value, max) => (typeof value === 'number' && Number.isSafeInteger(value)
    || typeof value === 'string' && /^\d+$/.test(value)) && Number.isSafeInteger(Number(value))
    && Number(value) >= 0 && Number(value) <= max;
  const info = result?.info, events = result?.events;
  if (!info || !uint(info.fs, 0xffffffff) || !Number(info.fs) || !uint(info.frames, 0xffffffff)
      || !Number(info.frames) || !uint(info.voices,64) || !Number(info.voices))
    return unavailable('Native sample rate, frame count and allocated voices are required.');
  if (result.eventsError || result.eventsTruncated || !Array.isArray(events))
    return unavailable('Complete native note events are unavailable.');
  if (events.length > 20000) return unavailable('Song structure exceeds the 20,000-event limit; no partial profile was computed.');
  const fs = Number(info.fs), frames = Number(info.frames), voices = Number(info.voices), ordered = [];
  if (info.events !== undefined && (!uint(info.events, 20000)
      || events.filter(event => event?.lane === undefined).length !== Number(info.events)))
    return unavailable('Native event count differs from the complete listing.');
  for (const event of events) {
    if (!event || !['on','off','pitch','pos','move','control'].includes(event.kind)
        || !uint(event.voice,voices - 1) || !uint(event.time_ns, Number.MAX_SAFE_INTEGER))
      return unavailable('Native events have incomplete or unsupported fields.');
    const frame = Number(BigInt(event.time_ns) * BigInt(fs) / 1000000000n);
    if (frame >= frames || (event.frame !== undefined && (!uint(event.frame,frames - 1) || Number(event.frame) !== frame))
        || (['on','pitch'].includes(event.kind) && !uint(event.freq_millihz,0xffffffff)))
      return unavailable('Native event timing or commanded frequency is invalid.');
    ordered.push({event, frame});
  }
  // Stable sorting keeps native tie order: authored commands apply before a
  // generated tick at the same frame. Do not count transient same-frame overlap.
  ordered.sort((a,b) => a.frame - b.frame);
  const held = new Array(voices).fill(false), active = new Set();
  let attacks = 0, open = 0, maximum = 0, gateFrames = 0, noGateFrames = 0, previous = 0;
  let low = null, high = null;
  const span = end => {
    const length = end - previous;
    if (length) {gateFrames += length * open; if (!open) noGateFrames += length; maximum = Math.max(maximum,open);}
    previous = end;
  };
  for (const {event,frame} of ordered) {
    span(frame);
    const voice = Number(event.voice);
    if (event.kind === 'on') {
      attacks++;active.add(voice);
      if (!held[voice]) {held[voice] = true;open++;}
    } else if (event.kind === 'off' && held[voice]) {held[voice] = false;open--;}
    if (event.kind === 'on' || event.kind === 'pitch' && held[voice]) {
      const frequency = Number(event.freq_millihz);
      low = low === null ? frequency : Math.min(low,frequency);
      high = high === null ? frequency : Math.max(high,frequency);
    }
  }
  span(frames);
  return {...base,status:'available',sample_rate_hz:fs,frames,duration_seconds:frames / fs,
    note_attacks:attacks,attacks_per_second:attacks * fs / frames,
    active_voices:active.size,allocated_voices:voices,
    commanded_frequency_hz:{minimum:low === null ? null : low / 1000,maximum:high === null ? null : high / 1000},
    maximum_open_gates:maximum,mean_open_gates:gateFrames / frames,no_gate_seconds:noGateFrames / fs};
}

export function songStructureText(profile) {
  if (profile.status !== 'available') return 'Song structure unavailable: ' + profile.reason;
  const range = profile.commanded_frequency_hz;
  return `${profile.duration_seconds.toFixed(3)} s · ${profile.note_attacks} note attack${profile.note_attacks === 1 ? '' : 's'} (${profile.attacks_per_second.toFixed(2)}/s) · `
    + `${profile.active_voices} active / ${profile.allocated_voices} allocated voices. `
    + `Commanded frequency: ${range.minimum === null ? 'none' : `${range.minimum}–${range.maximum} Hz`}. `
    + `Open gates: ${profile.mean_open_gates.toFixed(2)} mean, ${profile.maximum_open_gates} maximum. `
    + `No open gates: ${profile.no_gate_seconds.toFixed(3)} s (not measured silence).`;
}

// Shared display index of native events. No TL parsing, audio synthesis or inferred DSP state.
export function nativeTimeline(result, duration = Number(result.info?.length_ns) / 1e9) {
  const count = Number(result.info?.voices);
  const voices = Array.from({length: Number.isInteger(count) && count >= 1 && count <= 64 ? count : 0},
    (_, voice) => ({voice, gates: [], commands: []}));
  const model = {duration, voices, eventError: null, extent: 1000, notes: [], marks: [], streams: []};
  if (result.eventsError || !Array.isArray(result.events) || !voices.length) {
    model.eventError = 'Native note and spatial events are unavailable. PCM remains available.';
    return model;
  }
  if (result.events.length > 20000) {
    model.eventError = 'Note and spatial display exceeds the 20,000-event limit. PCM remains available.';
    return model;
  }
  const events = [];
  for (const event of result.events) {
    if (!['on', 'off', 'pitch', 'pos', 'move'].includes(event.kind)) continue;
    const voice = Number(event.voice), ns = Number(event.time_ns);
    if (!Number.isInteger(voice) || !voices[voice] || !numeric(event.time_ns) || ns < 0 || !Number.isSafeInteger(ns)
      || (['on', 'pitch'].includes(event.kind) && (!numeric(event.freq_millihz) || Number(event.freq_millihz) < 0))
      || (['pos', 'move'].includes(event.kind) && !['x', 'y', 'z'].every(key => numeric(event[key])))
      || (event.kind === 'move' && (!numeric(event.duration_ns) || Number(event.duration_ns) < 0))) {
      model.eventError = 'Native events have incomplete display fields. PCM remains available.';
      return model;
    }
    events.push({...event, voice, seconds: ns / 1e9});
  }
  events.sort((a, b) => a.seconds - b.seconds);
  for (const event of events) {
    const voice = voices[event.voice];
    if (event.kind === 'pos' || event.kind === 'move') {
      const command = {...event, x: Number(event.x), y: Number(event.y), z: Number(event.z),
        duration: event.kind === 'move' ? Number(event.duration_ns) / 1e9 : 0};
      voice.commands.push(command);
      model.extent = Math.max(model.extent, Math.abs(command.x), Math.abs(command.y));
    } else {
      const previous = voice.gates.at(-1) || {gate: false, frequency: null, velocity: null, patch: null};
      voice.gates.push({...previous, seconds: event.seconds,
        gate: event.kind === 'off' ? false : event.kind === 'on' ? true : previous.gate,
        frequency: event.kind === 'off' ? previous.frequency : Number(event.freq_millihz) / 1000,
        stream: event.kind === 'on' ? String(event.stream ?? (event.lane !== undefined ? 'Generator '+event.lane : 'Voice '+event.voice)) : previous.stream,
        velocity: event.kind === 'on' ? Number(event.velocity) : previous.velocity,
        patch: event.kind === 'on' ? event.patch || 'Generated note' : previous.patch});
    }
  }
  // The macOS roll's gate semantics: retriggers and pitch changes split a bar;
  // off closes it, and an open gate ends at the rendered record boundary.
  for (const voice of voices) for (let i = 0; i < voice.gates.length; i++) {
    const gate = voice.gates[i], end = Math.min(model.duration, voice.gates[i + 1]?.seconds ?? model.duration);
    if (gate.gate && gate.frequency > 0 && end > gate.seconds)
      model.notes.push({voice: voice.voice, start: gate.seconds, end, frequency: gate.frequency, stream: gate.stream, patch: gate.patch, velocity: gate.velocity});
  }
  model.streams = [...new Set(model.notes.map(note => note.stream))];
  model.marks = result.events.filter(event => !['on','off','pitch'].includes(event.kind) && numeric(event.time_ns)
    && Number(event.time_ns) >= 0 && Number(event.time_ns) / 1e9 <= model.duration)
    .map(event => ({seconds: Number(event.time_ns) / 1e9, kind: event.kind}));
  return model;
}

export function pianoRollGeometry(model) {
  let low = Infinity, high = -Infinity;
  for (const note of model.notes) {const pitch = Math.log2(note.frequency);low = Math.min(low, pitch);high = Math.max(high, pitch);}
  low = Number.isFinite(low) ? low - .5 : 6;high = Number.isFinite(high) ? Math.max(high + .5, low + 1) : 10;
  const span = high - low, height = Math.max(2, Math.min(6, 200 / (span * 12))), ticks = [];
  for (let octave = Math.ceil(low); octave <= high; octave += Math.max(1, Math.ceil(span / 8))) {
    const y = 200 - (octave - low) / span * 200, hz = 2 ** octave;
    ticks.push({y, label: hz >= 1000 ? `${(hz / 1000).toFixed(hz < 10000 ? 1 : 0)}k` : hz.toFixed(hz < 1 ? 2 : 0)});
  }
  const bars = model.notes.map(note => ({...note, x: note.start / model.duration * 1000,
    y: 200 - (Math.log2(note.frequency) - low) / span * 200 - height / 2,
    width: Math.max(1, (note.end - note.start) / model.duration * 1000), height,
    color: noteColors[model.streams.indexOf(note.stream) % noteColors.length]}));
  // Note-name guides share the exact logarithmic geometry with the Hz axis.
  // They are display labels in twelve-tone equal temperament, not retuning.
  const noteTicks = [], lowestMidi = 69 + 12 * (low - Math.log2(440));
  for (let midi = Math.ceil(lowestMidi / 12) * 12; ; midi += 12 * Math.max(1, Math.ceil(span / 8))) {
    const pitch = Math.log2(440) + (midi - 69) / 12;
    if (pitch > high) break;
    noteTicks.push({y:200 - (pitch - low) / span * 200, label:`C${midi / 12 - 1}`});
  }
  return {ticks, noteTicks, bars};
}

export function toSvg(result) {
  if (!result.info) throw Error('Render a readable record first.');
  const voices = Number(result.info.voices), duration = Number(result.info.length_ns) / 1e9;
  if (!Number.isInteger(voices) || voices < 1 || voices > 64 || !Number.isFinite(duration) || duration <= 0)
    throw Error('Native event dimensions are unavailable.');
  const model = nativeTimeline(result);
  if (model.eventError) throw Error(model.eventError);
  const geometry = pianoRollGeometry(model), positions = model.voices.flatMap(voice => voice.commands);
  const height = positions.length ? 620 : 346;
  // The outer SVG has no fixed viewBox: text keeps its readable pixel size while
  // nested plots use the available width. It works both inline and as a saved SVG.
  let svg = `<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="${height}" role="img" aria-label="JotTune note timeline and spatial command targets"><title>JotTune Timeline</title><desc>Native note gates: time horizontally and frequency vertically. Pitch commands and retriggers split gates. Colours identify streams; opacity shows velocity. Gates are not release tails or audible pitch modulation. Spatial targets are commands, not interpolated motion or a measured sound image.</desc><rect width="100%" height="100%" fill="#101a20"/><g fill="#a9c0cc" font-family="system-ui, sans-serif" font-size="12"><text x="12" y="23">Note gates · frequency (Hz)</text>`;
  for (const tick of geometry.ticks)
    svg += `<text x="9%" y="${55 + tick.y + 4}" text-anchor="end" font-size="11">${tick.label}</text>`;
  svg += '</g><svg class="timeline-roll-plot" x="10%" y="55" width="88%" height="214" viewBox="0 0 1000 214" preserveAspectRatio="none">';
  for (const tick of geometry.ticks)
    svg += `<path d="M0 ${tick.y}H1000" stroke="#304650" stroke-width=".6"/>`;
  for (const note of geometry.bars) {
    const alpha = Math.max(.25, Math.min(1, .25 + .75 * Number(note.velocity || 0) / 32767));
    const title = `${note.patch} · stream ${note.stream} · voice ${note.voice} · ${note.frequency} Hz · velocity ${note.velocity} · ${note.start.toFixed(3)}–${note.end.toFixed(3)} s`;
    svg += `<rect data-voice="${note.voice}" data-start="${note.start}" data-end="${note.end}" x="${note.x}" y="${note.y}" width="${note.width}" height="${note.height}" rx="1" fill="${note.color}" opacity="${alpha.toFixed(3)}"><title>${escape(title)}</title></rect>`;
  }
  svg += '<rect x="0" y="201" width="1000" height="13" fill="#283941"/>';
  const markColors = {pos:'#76d8e3',move:'#70c5ad',send:'#d9a8ed',filter:'#eead74',yaw:'#eabf6c'};
  for (const mark of model.marks)
    svg += `<rect x="${mark.seconds / duration * 1000}" y="203" width="1.5" height="9" fill="${markColors[mark.kind] || '#a6b6be'}"><title>${escape(mark.kind)} · ${mark.seconds.toFixed(3)} s</title></rect>`;
  svg += '</svg><g fill="#a9c0cc" font-family="system-ui, sans-serif" font-size="11">';
  for (let i = 0; i <= 4; i++)
    svg += `<text x="${10 + i * 22}%" y="287" text-anchor="${i === 0 ? 'start' : i === 4 ? 'end' : 'middle'}">${i === 0 ? '0 s' : (duration * i / 4).toFixed(duration < 10 ? 2 : 1)+' s'}</text>`;
  svg += '<text x="12" y="312">Colours: streams · bottom strip: commands.</text><text x="12" y="331">Gate view only; hover notes for details.</text></g>';
  if (!model.notes.length)
    svg += '<text x="50%" y="145" text-anchor="middle" fill="#a9c0cc" font-family="system-ui" font-size="12">No note gates in this record.</text>';
  if (positions.length) {
    const extent = Math.max(1, ...positions.flatMap(event => [Math.abs(event.x), Math.abs(event.y)]));
    const scale = 90 / extent;
    svg += `<g fill="#a9c0cc" font-family="system-ui, sans-serif" font-size="11"><text x="12" y="362">Spatial command targets · voice colours</text><text x="12" y="381">Top view (mm), not interpolated motion.</text><text x="12" y="608">Extent ±${extent} mm · x right / y ahead · z in tooltips.</text></g><svg class="timeline-space-plot" x="0" y="390" width="100%" height="200" viewBox="-130 -100 260 200"><path d="M-100 0H100 M0 -95V95" stroke="#537369"/><g fill="#a9c0cc" font-family="system-ui" font-size="11"><text x="108" y="4">x →</text><text x="5" y="-89">y ↑</text></g>`;
    for (const event of positions) {
      const title = `${event.kind} · voice ${event.voice} · ${event.time_ns} ns · duration ${event.duration_ns || 0} ns · x=${event.x} y=${event.y} z=${event.z} mm`;
      svg += `<circle cx="${(event.x * scale).toFixed(3)}" cy="${(-event.y * scale).toFixed(3)}" r="${event.kind === 'move' ? 5 : 3}" fill="${noteColors[event.voice % noteColors.length]}" stroke="#101a20"><title>${escape(title)}</title></circle>`;
    }
    svg += '</svg>';
  }
  return svg + '</svg>';
}

const KEYS = ['model','model_source','client','client_version','made_by','utc_day','tokens_in','tokens_out','prompt_id','licence'];
export function provenance(value = {}) {
  const output = {};
  for (const [key, item] of Object.entries(value)) {
    if (!KEYS.includes(key)) throw Error('Unsupported provenance key: ' + key);
    if (item === '' || item === null || item === undefined) continue;
    const text = String(item);
    if (!/^[A-Za-z0-9._:+/@-]{1,160}$/.test(text)) throw Error('Invalid provenance value: ' + key);
    if (['tokens_in','tokens_out'].includes(key) && !/^\d+$/.test(text)) throw Error('Token counts must be nonnegative integers.');
    if (key === 'model_source' && !['client-reported','user-stated','unknown'].includes(text)) throw Error('Invalid model source.');
    if (key === 'made_by' && !['agent','human','mixed'].includes(text)) throw Error('Invalid author kind.');
    if (key === 'utc_day' && !/^\d{4}-\d{2}-\d{2}$/.test(text)) throw Error('Use YYYY-MM-DD for utc_day.');
    output[key] = text;
  }
  return output;
}
function infoChunk(key, value) {
  const bytes = encoder.encode(value + '\0');
  const out = new Uint8Array(8 + bytes.length + bytes.length % 2);
  out.set(encoder.encode(key)); new DataView(out.buffer).setUint32(4, bytes.length, true); out.set(bytes,8);
  return out;
}
export function toWav(result, metadata = {}) {
  if (!result.pcm) throw Error('No rendered PCM is available.');
  const tags = provenance(metadata);
  const comments = JSON.stringify({ record_sha256: result.recordSha256, pcm_hash: result.pcmHash, renderer_release: result.rendererRelease || null, ...tags });
  const chunks = [infoChunk('ISFT', 'JotTune Synth ' + result.engineIdentity + ' build=' + result.engineId),
    infoChunk('ICMT', comments), infoChunk('ICRD', tags.utc_day || new Date().toISOString().slice(0,10))];
  const infoSize = 4 + chunks.reduce((sum,c) => sum + c.length,0);
  const pcmSize = result.pcm.byteLength;
  const out = new Uint8Array(44 + pcmSize + 8 + infoSize), view = new DataView(out.buffer);
  const str = (pos, text) => out.set(encoder.encode(text),pos);
  str(0,'RIFF'); view.setUint32(4,out.length-8,true); str(8,'WAVE');str(12,'fmt ');view.setUint32(16,16,true);
  view.setUint16(20,1,true);view.setUint16(22,result.channels,true);view.setUint32(24,result.sampleRate,true);
  view.setUint32(28,result.sampleRate*result.channels*2,true);view.setUint16(32,result.channels*2,true);view.setUint16(34,16,true);
  str(36,'data');view.setUint32(40,pcmSize,true);
  for(let i=0;i<result.pcm.length;i++)view.setInt16(44+i*2,result.pcm[i],true);
  let offset=44+pcmSize;str(offset,'LIST');view.setUint32(offset+4,infoSize,true);str(offset+8,'INFO');offset+=12;
  for(const chunk of chunks){out.set(chunk,offset);offset+=chunk.length;}
  return new Blob([out], {type:'audio/wav'});
}
const spectrumColumns = 1320;
const spectrumFloor = -90;
const spectrumCeiling = 0;
const spectrumBands = [
  {name: 'Low range', fftSize: 8192, minimumHz: 30, maximumHz: 600, rows: 260},
  {name: 'Full range', fftSize: 1024, minimumHz: 150, maximumHz: 16000, rows: 400},
];

function spectrumInput(result) {
  const {pcm, channels, sampleRate} = result || {};
  if (!(pcm instanceof Int16Array) || !Number.isInteger(channels) || channels < 1 || channels > 16
      || !Number.isInteger(sampleRate) || sampleRate < 1 || pcm.length === 0 || pcm.length % channels)
    throw Error('A complete interleaved PCM render is required for the spectrogram.');
  const frames = pcm.length / channels;
  if (frames / sampleRate > 300) throw Error('Spectrogram exceeds the 300-second browser profile.');
  return {pcm, channels, sampleRate, frames};
}

export function fftPlan(size) {
  if (!Number.isInteger(size) || size < 2 || size > 1048576 || (size & (size - 1)))
    throw Error('FFT size must be a power of two between 2 and 1048576.');
  const real = new Float64Array(size), imaginary = new Float64Array(size);
  const reverse = new Uint32Array(size), window = new Float64Array(size);
  const cosine = new Float64Array(size / 2), sine = new Float64Array(size / 2);
  for (let i = 1; i < size; i++) reverse[i] = (reverse[i >>> 1] >>> 1) | ((i & 1) * size / 2);
  for (let i = 0; i < size; i++) window[i] = .5 - .5 * Math.cos(2 * Math.PI * i / (size - 1));
  for (let i = 0; i < size / 2; i++) {
    cosine[i] = Math.cos(2 * Math.PI * i / size);
    sine[i] = -Math.sin(2 * Math.PI * i / size);
  }
  return {size, real, imaginary, reverse, window, cosine, sine};
}

// Input arrays are in the plan's bit-reversed order; output is natural order.
// Shared by cursor/spectrogram displays and optional oversampled peak analysis.
export function transformFFT(plan, {inverse = false} = {}) {
  const {size, real, imaginary, cosine, sine} = plan;
  const direction = inverse ? -1 : 1;
  for (let length = 2; length <= size; length *= 2) {
    const half = length / 2, step = size / length;
    for (let begin = 0; begin < size; begin += length) {
      for (let j = 0; j < half; j++) {
        const k = j * step, a = begin + j, b = a + half;
        const re = cosine[k] * real[b] - direction * sine[k] * imaginary[b];
        const im = direction * sine[k] * real[b] + cosine[k] * imaginary[b];
        real[b] = real[a] - re; imaginary[b] = imaginary[a] - im;
        real[a] += re; imaginary[a] += im;
      }
    }
  }
  if (inverse) for (let i = 0; i < size; i++) { real[i] /= size; imaginary[i] /= size; }
  return plan;
}

function transformWindow(plan, input, center, channel = 0) {
  const {size, real, imaginary, reverse, window} = plan;
  const start = center - size / 2;
  let gain = 0;
  for (let i = 0; i < size; i++) {
    const frame = start + i, present = frame >= 0 && frame < input.frames;
    real[reverse[i]] = present ? input.pcm[frame * input.channels + channel] / 32768 * window[i] : 0;
    imaginary[i] = 0;
    if (present) gain += window[i];
  }
  transformFFT(plan);
  return gain;
}

// One bounded cursor window, separate from full-song spectrogram analysis.
// Reuse the plan and output buffer per view; never mix channels or change PCM.
export function createCursorSpectrum(result) {
  const input = spectrumInput(result), plan = fftPlan(2048);
  const dbfs = new Float32Array(plan.size / 2 + 1);
  return (seconds, channel = 0) => {
    if (!Number.isFinite(seconds) || !Number.isInteger(channel) || channel < 0 || channel >= input.channels)
      throw Error('Invalid spectrum cursor or channel.');
    const frame = Math.max(0, Math.min(input.frames - 1, Math.floor(seconds * input.sampleRate)));
    const gain = transformWindow(plan, input, frame, channel);
    for (let bin = 0; bin < dbfs.length; bin++) {
      const amplitude = Math.hypot(plan.real[bin], plan.imaginary[bin]) / gain * (bin === 0 || bin === plan.size / 2 ? 1 : 2);
      dbfs[bin] = amplitude > 0 ? Math.max(-120, 20 * Math.log10(amplitude)) : -120;
    }
    return {dbfs, frame, channel, fftSize: plan.size, binHz: input.sampleRate / plan.size,
      start: Math.max(0, frame - plan.size / 2), end: Math.min(input.frames, frame + plan.size / 2)};
  };
}

// Pure host-side display analysis, also used by the dedicated worker. Every
// sample is covered by centered, 50%-overlapped Hann windows; edge windows are
// zero-padded and calibrated by the actual in-record window gain. A displayed
// time/frequency cell keeps the greatest sinusoidal-bin power touching it.
// This preserves short events when many STFT windows share one display column.
export function analyzeSpectrogram(result, onProgress = () => {}) {
  const input = spectrumInput(result), columns = spectrumColumns;
  let completed = 0;
  const total = spectrumBands.reduce((sum, band) => sum + Math.ceil((input.frames - 1) / (band.fftSize / 2)) + 1, 0);
  onProgress(0);
  const panels = spectrumBands.map(band => {
    const {fftSize, rows} = band, hopSize = fftSize / 2;
    const maximumHz = Math.min(band.maximumHz, input.sampleRate / 2);
    const minimumHz = Math.min(band.minimumHz, maximumHz / 16);
    const logRange = Math.log(maximumHz / minimumHz);
    const starts = new Uint32Array(rows), ends = new Uint32Array(rows);
    for (let y = 0; y < rows; y++) {
      const top = maximumHz * Math.exp(-y / rows * logRange);
      const bottom = maximumHz * Math.exp(-(y + 1) / rows * logRange);
      let first = Math.ceil(bottom * fftSize / input.sampleRate);
      let last = Math.floor(top * fftSize / input.sampleRate);
      if (last < first) first = last = Math.round(Math.sqrt(top * bottom) * fftSize / input.sampleRate);
      starts[y] = Math.max(1, Math.min(fftSize / 2, first));
      ends[y] = Math.max(starts[y], Math.min(fftSize / 2, last));
    }
    const powers = new Float32Array(columns * rows), bins = new Float64Array(fftSize / 2 + 1);
    const plan = fftPlan(fftSize), windowCount = Math.ceil((input.frames - 1) / hopSize) + 1;
    const firstBin = Math.min(...starts), lastBin = Math.max(...ends);
    for (let index = 0; index < windowCount; index++) {
      const center = Math.min(index * hopSize, input.frames - 1);
      const previous = Math.min((index - 1) * hopSize, input.frames - 1);
      const next = Math.min((index + 1) * hopSize, input.frames - 1);
      const firstColumn = Math.max(0, Math.floor((index ? (previous + center) / 2 : 0) / input.frames * columns));
      const lastColumn = Math.min(columns, Math.ceil((index + 1 < windowCount ? (center + next) / 2 : input.frames) / input.frames * columns));
      const gain = transformWindow(plan, input, center), factor = 4 / (gain * gain);
      for (let bin = firstBin; bin <= lastBin; bin++) {
        const re = plan.real[bin], im = plan.imaginary[bin];
        bins[bin] = (re * re + im * im) * factor * (bin === fftSize / 2 ? .25 : 1);
      }
      for (let y = 0; y < rows; y++) {
        let power = 0;
        for (let bin = starts[y]; bin <= ends[y]; bin++) power = Math.max(power, bins[bin]);
        const row = y * columns;
        for (let x = firstColumn; x < lastColumn; x++) if (power > powers[row + x]) powers[row + x] = power;
      }
      completed++;
      if (completed % 128 === 0 || completed === total) onProgress(completed / total);
    }
    let peakDbfs = -Infinity;
    for (let i = 0; i < powers.length; i++) {
      const db = powers[i] > 0 ? 10 * Math.log10(powers[i]) : -Infinity;
      powers[i] = db;
      peakDbfs = Math.max(peakDbfs, db);
    }
    return {name: band.name, fftSize, hopSize, minimumHz, maximumHz, rows, columns, windowCount, peakDbfs, dbfs: powers};
  });
  return {columns, durationSeconds: input.frames / input.sampleRate, sampleRate: input.sampleRate,
    frames: input.frames, floorDbfs: spectrumFloor, ceilingDbfs: spectrumCeiling,
    channel: 1, aggregation: 'maximum sinusoidal-bin power per time/frequency cell', panels};
}

const spectrumPalette = [
  [0, 0, 4], [28, 12, 67], [81, 18, 123], [133, 34, 129],
  [185, 54, 121], [230, 90, 100], [251, 171, 91], [252, 253, 191],
];
function spectrumColor(db) {
  const fraction = Math.max(0, Math.min(1, (db - spectrumFloor) / (spectrumCeiling - spectrumFloor)));
  const position = fraction * (spectrumPalette.length - 1), lower = Math.floor(position), mix = position - lower;
  return spectrumPalette[lower].map((value, channel) => Math.round(value * (1 - mix)
    + spectrumPalette[Math.min(lower + 1, spectrumPalette.length - 1)][channel] * mix));
}
function frequencyLabel(value) {
  if (value >= 1000) return (value / 1000).toLocaleString('en-US', {maximumFractionDigits: 1}) + 'k';
  return value.toLocaleString('en-US', {maximumFractionDigits: value < 1 ? 3 : 0});
}
function timeTicks(duration) {
  const raw = duration / 8, power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].find(value => value * power >= raw) * power;
  const ticks = [0];
  for (let value = step; value < duration - step / 10; value += step) ticks.push(value);
  ticks.push(duration);
  return ticks;
}
function drawSpectrogram(analysis, canvas, channels) {
  canvas.width = 1600; canvas.height = 1000;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw Error('A 2D canvas is required for the spectrogram.');
  ctx.fillStyle = '#101821'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#f2eee7'; ctx.font = '600 30px system-ui, sans-serif';
  ctx.fillText('Rendered spectrum', 100, 43);
  ctx.font = '18px system-ui, sans-serif'; ctx.fillStyle = '#bac4ce';
  ctx.fillText(`Channel 1 of ${channels} · ${analysis.sampleRate.toLocaleString('en-US')} Hz · ${analysis.durationSeconds.toFixed(3)} seconds`, 100, 75);
  ctx.fillText('Logarithmic frequency · calibrated bin amplitude', 850, 75);
  for (let index = 0; index < analysis.panels.length; index++) {
    const panel = analysis.panels[index], left = 100, top = index ? 490 : 130;
    const image = ctx.createImageData(panel.columns, panel.rows);
    for (let i = 0; i < panel.dbfs.length; i++) {
      const color = spectrumColor(panel.dbfs[i]), offset = i * 4;
      image.data[offset] = color[0]; image.data[offset + 1] = color[1]; image.data[offset + 2] = color[2]; image.data[offset + 3] = 255;
    }
    ctx.putImageData(image, left, top);
    ctx.fillStyle = '#f2eee7'; ctx.font = '600 20px system-ui, sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(`${panel.name} · ${frequencyLabel(panel.minimumHz)}–${frequencyLabel(panel.maximumHz)} Hz · ${panel.fftSize}-point FFT`, left, top - 17);
    const frequencyTicks = [panel.minimumHz, ...[30, 50, 100, 150, 250, 500, 1000, 2000, 4000, 8000, 16000]
      .filter(value => value > panel.minimumHz * 1.15 && value < panel.maximumHz / 1.15), panel.maximumHz];
    ctx.font = '17px system-ui, sans-serif';
    for (const frequency of frequencyTicks) {
      const y = top + Math.log(panel.maximumHz / frequency) / Math.log(panel.maximumHz / panel.minimumHz) * panel.rows;
      ctx.strokeStyle = 'rgba(190,220,229,.2)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + panel.columns, y); ctx.stroke();
      ctx.fillStyle = '#bac4ce'; ctx.textAlign = 'right'; ctx.fillText(frequencyLabel(frequency), left - 12, y + 5);
    }
    const ticks = timeTicks(analysis.durationSeconds);
    for (let i = 0; i < ticks.length; i++) {
      const seconds = ticks[i], x = left + seconds / analysis.durationSeconds * panel.columns;
      ctx.strokeStyle = 'rgba(107,211,226,.25)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + panel.rows); ctx.stroke();
      ctx.fillStyle = '#bac4ce'; ctx.textAlign = i === 0 ? 'left' : i === ticks.length - 1 ? 'right' : 'center';
      ctx.fillText(seconds.toFixed(analysis.durationSeconds < 10 ? 2 : 1), x, top + panel.rows + 26);
    }
    ctx.textAlign = 'left'; ctx.fillStyle = '#bac4ce'; ctx.fillText('Hz', 39, top - 17);
    ctx.textAlign = 'right'; ctx.fillText('Time (s)', left + panel.columns, top + panel.rows + 52);
    const barLeft = 1460, barWidth = 22;
    for (let y = 0; y < panel.rows; y++) {
      const db = spectrumCeiling - y / Math.max(1, panel.rows - 1) * (spectrumCeiling - spectrumFloor);
      ctx.fillStyle = `rgb(${spectrumColor(db).join(',')})`; ctx.fillRect(barLeft, top + y, barWidth, 1);
    }
    ctx.textAlign = 'left'; ctx.fillStyle = '#bac4ce'; ctx.font = '16px system-ui, sans-serif';
    ctx.fillText('dBFS', barLeft - 1, top - 17);
    for (const db of [0, -30, -60, -90]) {
      const y = top + (spectrumCeiling - db) / (spectrumCeiling - spectrumFloor) * panel.rows;
      ctx.fillText(String(db), barLeft + barWidth + 8, y + 5);
    }
  }
  ctx.fillStyle = '#bac4ce'; ctx.textAlign = 'left'; ctx.font = '16px system-ui, sans-serif';
  ctx.fillText('50% overlapping Hann windows · strongest bin/window per pixel · 90 dB colour range (0 dBFS and above saturate)', 100, 965);
  ctx.fillText('Display only: no stereo summing, inferred tempo or changes to rendered audio.', 100, 988);
}

export async function spectrogram(result, canvas, {signal, onProgress} = {}) {
  const input = spectrumInput(result);
  if (signal?.aborted) throw new DOMException('Spectrogram cancelled.', 'AbortError');
  // Transfer only a new channel-1 copy. The rendered PCM remains usable for Play,
  // export and subsequent analysis; never transfer its underlying buffer.
  const pcm = input.channels === 1 ? input.pcm.slice() : new Int16Array(input.frames);
  if (input.channels !== 1) for (let frame = 0; frame < input.frames; frame++) pcm[frame] = input.pcm[frame * input.channels];
  return new Promise((resolve, reject) => {
    let worker, settled = false;
    const abort = () => finish(new DOMException('Spectrogram cancelled.', 'AbortError'));
    const finish = (error, value) => {
      if (settled) return;
      settled = true; signal?.removeEventListener('abort', abort); worker?.terminate();
      if (error) reject(error); else resolve(value);
    };
    try {
      worker = new Worker(new URL('spectrum-worker.mjs', import.meta.url), {type: 'module'});
      signal?.addEventListener('abort', abort, {once: true});
      worker.onerror = event => finish(Error(event.message || 'Spectrogram analysis failed.'));
      worker.onmessage = ({data}) => {
        if (settled) return;
        try {
          if (data.type === 'progress') { onProgress?.(data.fraction); return; }
          if (data.type === 'error') throw Error(data.message);
          if (data.type !== 'result') throw Error('Unexpected spectrogram worker response.');
          drawSpectrogram(data.analysis, canvas, input.channels);
          const {panels, ...metadata} = data.analysis;
          finish(null, {...metadata, sourceChannels: input.channels, width: canvas.width, height: canvas.height,
            panels: panels.map(({dbfs, ...panel}) => panel)});
        } catch (error) { finish(error); }
      };
      worker.postMessage({pcm, channels: 1, sampleRate: input.sampleRate}, [pcm.buffer]);
    } catch (error) { finish(error); }
  });
}
