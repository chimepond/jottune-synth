// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Host measurements only. Never modifies samples or interprets TL statements.
import {fftPlan, transformFFT, songStructure} from './media.mjs';
export const analysisLimits = Object.freeze({pcmBytes:128 * 1024 * 1024, seconds:300,
  sections:32, events:20000, dryBytes:2 * 1024 * 1024 * 1024, defaultDryBytes:128 * 1024 * 1024});
const db = x => x > 0 ? 20 * Math.log10(x) : null;
const lu = x => x > 0 ? -0.691 + 10 * Math.log10(x) : null;
const requireValue = (ok, text) => {if (!ok) throw Error(text);};
function integer(value, low, high, name) {
  requireValue(Number.isSafeInteger(value) && value >= low && value <= high, `Invalid ${name}.`);
  return value;
}
function cancelled(options) {
  if (options.signal?.aborted || options.shouldCancel?.()) {
    const error = Error('Analysis cancelled.'); error.name = 'AbortError'; throw error;
  }
}
const pause = () => new Promise(resolve => setTimeout(resolve, 0));

// Same rate-dependent coefficients as tools/music/analyze.py:kfilters.
export function kFilters(fs) {
  integer(fs, 8000, 192000, 'sample rate');
  let K = Math.tan(Math.PI * 1681.974450955533 / fs), Q = 0.7071752369554196;
  const Vh = 10 ** (3.999843853973347 / 20), Vb = Vh ** 0.4996667741545416;
  let a0 = 1 + K / Q + K * K;
  const shelf = [(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0,
    (Vh - Vb * K / Q + K * K) / a0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0];
  K = Math.tan(Math.PI * 38.13547087602444 / fs); Q = 0.5003270373238773;
  a0 = 1 + K / Q + K * K;
  return [shelf, [1, -2, 1, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]];
}
class Filter {
  constructor(coefficients) {this.b = coefficients; this.x1 = this.x2 = this.y1 = this.y2 = 0;}
  push(x) {
    const b = this.b, y = b[0] * x + b[1] * this.x1 + b[2] * this.x2 - b[3] * this.y1 - b[4] * this.y2;
    this.x2 = this.x1; this.x1 = x; this.y2 = this.y1; this.y1 = y;
    return y;
  }
}
function weighting(fs, channels) {
  const coefficients = kFilters(fs);
  return Array.from({length:channels}, () => coefficients.map(b => new Filter(b)));
}
// Prefix snapshots at hop boundaries avoid storing the filtered waveform. A
// 400 ms/3 s end need not coincide with a hop at unusual integer sample rates.
class Loudness {
  constructor(fs) {
    this.hop = Math.round(fs * .1); this.moment = Math.round(fs * .4); this.short = Math.round(fs * 3);
    this.prefix = new Float64Array(Math.ceil(this.short / this.hop) + 1);
    this.frames = 0; this.energy = 0; this.blocks = []; this.shortMax = 0;
  }
  push(power) {
    this.energy += power; const n = ++this.frames;
    if (n >= this.moment && (n - this.moment) % this.hop === 0) {
      const start = (n - this.moment) / this.hop;
      this.blocks.push(Math.max(0, (this.energy - this.prefix[start % this.prefix.length]) / this.moment));
    }
    if (n >= this.short && (n - this.short) % this.hop === 0) {
      const start = (n - this.short) / this.hop;
      this.shortMax = Math.max(this.shortMax, (this.energy - this.prefix[start % this.prefix.length]) / this.short);
    }
    if (n % this.hop === 0) this.prefix[n / this.hop % this.prefix.length] = this.energy;
  }
  result() {
    const absolute = 10 ** ((-70 + .691) / 10);
    let blocks = this.blocks.filter(power => power > absolute);
    if (blocks.length) {
      const relative = blocks.reduce((a, b) => a + b, 0) / blocks.length / 10;
      blocks = blocks.filter(power => power > relative);
    }
    return {integrated_lufs: blocks.length ? lu(blocks.reduce((a, b) => a + b, 0) / blocks.length) : null,
      short_term_max_lufs:lu(this.shortMax), mean_k_weighted_lufs:lu(this.frames ? this.energy / this.frames : 0),
      integrated_blocks:blocks.length};
  }
}
function accumulator(channels, fs, firstFrame, lastFrame, name, loudness) {
  return {name, firstFrame, lastFrame, frames:0, cross:0, peak:0, peakFrame:null, peakChannel:null,
    channels:Array.from({length:channels}, () => ({peak:0, peakFrame:null, sum:0, squares:0, rails:0})),
    meter:loudness ? new Loudness(fs) : null};
}
function add(acc, samples, frame, power, divisor, rail = false) {
  ++acc.frames;
  for (let c = 0; c < samples.length; ++c) {
    const x = samples[c], magnitude = Math.abs(x), row = acc.channels[c];
    if (magnitude > row.peak) {row.peak = magnitude; row.peakFrame = frame;}
    if (magnitude > acc.peak) {acc.peak = magnitude; acc.peakFrame = frame; acc.peakChannel = c;}
    row.sum += x; row.squares += x * x;
    if (rail && (x === -divisor || x === divisor - 1)) ++row.rails;
  }
  acc.meter?.push(power);
}
function summarize(acc, fs, divisor = 32768) {
  const n = acc.frames, per_channel = acc.channels.map((c, index) => ({channel:index,
    sample_peak: c.peak, sample_peak_dbfs:db(c.peak / divisor), peak_frame:c.peakFrame,
    peak_seconds:c.peakFrame === null ? null : c.peakFrame / fs,
    rms_dbfs:db(n ? Math.sqrt(c.squares / n) / divisor : 0),
    dc_mean:n ? c.sum / n / divisor : null, dc_dbfs:db(n ? Math.abs(c.sum / n) / divisor : 0),
    full_scale_samples:c.rails}));
  const energy = acc.channels.reduce((sum, c) => sum + c.squares, 0);
  return {name:acc.name, start_frame:acc.firstFrame, end_frame:acc.lastFrame,
    start_seconds:acc.firstFrame / fs, end_seconds:acc.lastFrame / fs, frames:n,
    sample_peak:acc.peak, sample_peak_dbfs:db(acc.peak / divisor), peak_frame:acc.peakFrame,
    peak_seconds:acc.peakFrame === null ? null : acc.peakFrame / fs, peak_channel:acc.peakChannel,
    rms_dbfs:db(n ? Math.sqrt(energy / (n * acc.channels.length)) / divisor : 0),
    full_scale_samples:per_channel.reduce((sum, c) => sum + c.full_scale_samples, 0),
    ...(acc.bands ? {bands:acc.bands} : {}), per_channel, loudness:acc.meter?.result() ?? null};
}

// Band-limited 4x interpolation estimate, using the existing radix-two FFT.
// Each block includes 4096 original frames on either side; only its central
// 57344 frames are measured. Different block lengths from the Python meter
// mean this is explicitly an estimate, not a claim of bit-identical meters.
async function truePeak(pcm, fs, channels, peak, peakFrame, peakChannel, options, sections = []) {
  const method = '4x FFT interpolation estimate; 65536-point input, 4096-frame margins, zero outside record';
  const values = (best, frame, channel) => ({true_peak_dbtp:db(best), true_peak_method:method,
    true_peak_frame:frame, true_peak_seconds:frame === null ? null : frame / fs,
    true_peak_channel:channel, true_peak_reason:null});
  const selected = sections.map(section => ({firstFrame:section.firstFrame,lastFrame:section.lastFrame,
    best:section.peak / 32768,frame:section.peakFrame,channel:section.peakChannel}));
  if (!peak) return {...values(0,null,null),sections:selected.map(() => values(0,null,null))};
  const size = 65536, pad = 4096, block = size - 2 * pad, over = 4;
  const input = fftPlan(size), output = fftPlan(size * over), frames = pcm.length / channels;
  let best = peak / 32768, bestFrame = peakFrame, bestChannel = peakChannel;
  for (let channel = 0; channel < channels; ++channel) for (let start = 0; start < frames; start += block) {
    cancelled(options);
    let nonzero = false;
    for (let i = 0; i < size; ++i) {
      const frame = start + i - pad;
      const value = frame >= 0 && frame < frames ? pcm[frame * channels + channel] / 32768 : 0;
      input.real[input.reverse[i]] = value; if (value) nonzero = true;
    }
    if (!nonzero) continue;
    input.imaginary.fill(0); transformFFT(input);
    output.real.fill(0); output.imaginary.fill(0);
    output.real[0] = input.real[0];
    for (let k = 1; k <= size / 2; ++k) {
      const factor = k === size / 2 ? .5 : 1, real = input.real[k] * factor;
      const imaginary = k === size / 2 ? 0 : input.imaginary[k];
      const positive = output.reverse[k], negative = output.reverse[output.size - k];
      output.real[positive] = output.real[negative] = real;
      output.imaginary[positive] = imaginary; output.imaginary[negative] = -imaginary;
    }
    transformFFT(output, {inverse:true});
    const end = over * (pad + Math.min(block, frames - start));
    // Split at selection boundaries and scan each interpolated sample only once.
    // Sections retain the full record's interpolation context at their edges.
    const active = selected.filter(section => section.lastFrame > start && section.firstFrame < start + block);
    const edges = new Set([over * pad,end]);
    for (const section of active) for (const frame of [section.firstFrame,section.lastFrame])
      edges.add(Math.max(over * pad,Math.min(end,over * (pad + frame - start))));
    const boundaries = [...edges].sort((a,b) => a - b);
    for (let part = 1; part < boundaries.length; ++part) {
      const first = boundaries[part - 1], last = boundaries[part];
      let maximum = 0, maximumFrame = null;
      for (let i = first; i < last; ++i) {
        const magnitude = Math.abs(output.real[i] * over);
        if (magnitude > maximum) {maximum = magnitude; maximumFrame = start + i / over - pad;}
      }
      if (maximum > best) {best = maximum; bestFrame = maximumFrame; bestChannel = channel;}
      const firstFrame = start + first / over - pad, lastFrame = start + last / over - pad;
      for (const section of active) if (firstFrame >= section.firstFrame && lastFrame <= section.lastFrame && maximum > section.best) {
        section.best = maximum; section.frame = maximumFrame; section.channel = channel;
      }
    }
    await pause(); cancelled(options);
  }
  return {...values(best,bestFrame,bestChannel),sections:selected.map(section => values(section.best,section.frame,section.channel))};
}

// Non-overlapping FFT blocks on the record grid. Shared full blocks are
// transformed once even if selections overlap. A partial selection boundary
// uses just its own frames, with the remainder zero-padded.
async function measureBands(pcm, fs, channels, selections, options) {
  const size = 2048, plan = fftPlan(size), edges = [20,60,250,1000,4000,16000];
  const names = ['sub','bass','lowmid','mid','high'];
  const bins = edges.map(hz => Math.min(size / 2 + 1, Math.ceil(hz * size / fs)));
  const totals = selections.map(() => new Float64Array(names.length));
  const frames = pcm.length / channels;
  for (let start = 0; start < frames; start += size) {
    const cache = new Map();
    for (const [index,section] of selections.entries()) {
      const first = Math.max(start, section.firstFrame), last = Math.min(start + size, section.lastFrame);
      if (first >= last) continue;
      const key = (first - start) * (size + 1) + last - start;
      let sums = cache.get(key);
      if (!sums) {
        sums = new Float64Array(names.length);
        for (let c = 0; c < channels; ++c) {
          plan.real.fill(0); plan.imaginary.fill(0);
          for (let frame = first; frame < last; ++frame)
            plan.real[plan.reverse[frame - start]] = pcm[frame * channels + c] / 32768;
          transformFFT(plan);
          for (let b = 0; b < names.length; ++b) for (let k = bins[b]; k < bins[b + 1]; ++k)
            sums[b] += (k === size / 2 ? 1 : 2) * (plan.real[k] ** 2 + plan.imaginary[k] ** 2);
        }
        cache.set(key, sums);
      }
      for (let b = 0; b < sums.length; ++b) totals[index][b] += sums[b];
    }
    if (start % (64 * size) === 0) {await pause(); cancelled(options);}
  }
  selections.forEach((section,index) => {
    const sum = totals[index].reduce((a,b) => a + b, 0);
    const scale = size * (section.lastFrame - section.firstFrame) * channels;
    section.bands = names.map((name,b) => ({name, minimum_hz:edges[b], maximum_hz:edges[b + 1],
      mean_square_fs2:scale && bins[b] < bins[b + 1] ? totals[index][b] / scale : null,
      energy_fraction:sum && bins[b] < bins[b + 1] ? totals[index][b] / sum : null}));
  });
}

function stereoSummary(acc, scope) {
  const {frames,cross} = acc;
  if (acc.channels.length !== 2 || !frames) return null;
  const [l, r] = acc.channels, cov = cross - l.sum * r.sum / frames;
  const left = Math.max(0, l.squares - l.sum ** 2 / frames), right = Math.max(0, r.squares - r.sum ** 2 / frames);
  const scale = 4 * frames * 32768 ** 2;
  const mid = Math.max(0, (l.squares + r.squares + 2 * cross) / scale);
  const side = Math.max(0, (l.squares + r.squares - 2 * cross) / scale);
  return {correlation:left && right ? Math.max(-1, Math.min(1, cov / Math.sqrt(left * right))) : null,
    left_minus_right_db:l.squares && r.squares ? 10 * Math.log10(l.squares / r.squares) : null,
    mid_mean_square_fs2:mid, side_mean_square_fs2:side,
    mid_rms_dbfs:db(Math.sqrt(mid)), side_rms_dbfs:db(Math.sqrt(side)),
    side_energy_fraction:mid + side ? side / (mid + side) : null,
    mid_side_method:'M=(L+R)/2; S=(L-R)/2; includes DC; ' + scope};
}

// The open endpoint is the exact integer frame count, even when converting
// frames/fs back to frames would round up by one in binary floating point.
export function analysisSectionFrames(section, index, fs, frames) {
  requireValue(section && typeof section === 'object' && !Array.isArray(section), 'Each section must be an object.');
  requireValue(Object.keys(section).every(k => ['name','startSeconds','endSeconds'].includes(k)),
    'Analysis section '+(index+1)+' accepts name, startSeconds and endSeconds.');
  requireValue(['startSeconds','endSeconds'].every(k => section[k] === undefined || Number.isFinite(section[k])),
    'Section endpoints must be finite seconds. Omit endSeconds to use the exact record end.');
  requireValue(section.name === undefined || (typeof section.name === 'string' && section.name.length <= 120), 'Invalid section name.');
  const name = section.name ?? `section-${index + 1}`;
  const duration = frames / fs;
  const start = section.startSeconds ?? 0, end = section.endSeconds ?? duration;
  const first = Math.ceil(start * fs), last = end === duration ? frames : Math.ceil(end * fs);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end > duration || last <= first)
    throw Error(`Analysis section ${index + 1} (${JSON.stringify(name)}) must select frames inside the record. `
      + `Requested ${start} to ${end} s. The exact record end is ${frames} frames at ${fs} Hz (${duration} s). Omit endSeconds to use it.`);
  return {name, first, last};
}

export async function analyzePCM(result, options = {}) {
  cancelled(options);
  const {pcm, sampleRate:fs, channels} = result || {};
  requireValue(pcm instanceof Int16Array, 'Analysis requires final signed 16-bit PCM.');
  integer(fs, 8000, 192000, 'sample rate'); integer(channels, 1, 16, 'channel count');
  requireValue(pcm.length % channels === 0, 'PCM length does not match its channel count.');
  const frames = pcm.length / channels;
  requireValue(pcm.byteLength <= analysisLimits.pcmBytes && frames <= fs * analysisLimits.seconds,
    'PCM analysis exceeds the 128 MiB or 300-second work limit.');
  const windowSeconds = options.windowSeconds ?? 4;
  requireValue(Number.isFinite(windowSeconds) && windowSeconds >= .1 && windowSeconds <= 300, 'Window seconds must be 0.1 to 300.');
  const step = Math.max(1, Math.round(windowSeconds * fs));
  const every = integer(options.yieldEveryFrames ?? 32768, 256, 1048576, 'analysis yield interval');
  const selections = options.sections ?? [];
  requireValue(options.bands === undefined || typeof options.bands === 'boolean', 'bands must be a boolean.');
  requireValue(options.truePeak === undefined || typeof options.truePeak === 'boolean', 'truePeak must be a boolean.');
  requireValue(Array.isArray(selections) && selections.length <= analysisLimits.sections, 'At most 32 analysis sections are supported.');
  // One/two components are loudspeaker/ear channels. Raw ambisonic components
  // are not assigned a perceptual loudness or an inferred speaker downmix.
  const audible = channels <= 2, filters = audible ? weighting(fs, channels) : null;
  const full = accumulator(channels, fs, 0, frames, 'whole', audible);
  const windows = Array.from({length:Math.ceil(frames / step)}, (_, i) =>
    accumulator(channels, fs, i * step, Math.min(frames, (i + 1) * step), `window-${i + 1}`, audible));
  const sections = selections.map((section, i) => {
    const {name,first,last} = analysisSectionFrames(section, i, fs, frames);
    return accumulator(channels, fs, first, last, name, audible);
  });
  if (options.bands && audible) await measureBands(pcm, fs, channels, [full, ...sections], options);
  const samples = new Float64Array(channels);
  const endWindow = accumulator(channels, fs, Math.max(0, frames - Math.round(.1 * fs)), frames, 'ending', false);
  const threshold = 32768 * .001; // -60 dBFS in amplitude.
  let lastNonzero = -1, lastAboveThreshold = -1;
  for (let frame = 0; frame < frames; ++frame) {
    let power = 0;
    for (let c = 0; c < channels; ++c) {
      const x = samples[c] = pcm[frame * channels + c];
      if (x) lastNonzero = frame;
      if (Math.abs(x) > threshold) lastAboveThreshold = frame;
      if (filters) {const y = filters[c][1].push(filters[c][0].push(x / 32768)); power += y * y;}
    }
    if (channels === 1) power *= 2; // Existing public meter/playback convention: dual mono.
    const cross = channels === 2 ? samples[0] * samples[1] : 0;
    full.cross += cross;
    add(full, samples, frame, power, 32768, true);
    add(windows[Math.floor(frame / step)], samples, frame, power, 32768, true);
    for (const section of sections) if (frame >= section.firstFrame && frame < section.lastFrame) {
      section.cross += cross;
      add(section, samples, frame, power, 32768, true);
    }
    if (frame >= endWindow.firstFrame) add(endWindow, samples, frame, power, 32768, true);
    if ((frame + 1) % every === 0) {cancelled(options); await pause(); cancelled(options);}
  }
  cancelled(options);
  const whole = summarize(full, fs), loudness = whole.loudness;
  let interpolated = {true_peak_dbtp:null, true_peak_method:null, true_peak_frame:null,
    true_peak_seconds:null, true_peak_channel:null,
    true_peak_reason:audible ? 'Oversampled peak analysis was disabled.' : 'Raw field components have no speaker true-peak assignment.'};
  let sectionPeaks = sections.map(() => ({...interpolated}));
  if (audible && options.truePeak !== false) {
    try {
      const {sections:peaks,...wholePeak} = await truePeak(pcm, fs, channels, full.peak, full.peakFrame, full.peakChannel, options, sections);
      interpolated = wholePeak; sectionPeaks = peaks;
    }
    catch (error) {
      if (error.name === 'AbortError') throw error;
      interpolated.true_peak_reason = error.message;
      sectionPeaks = sections.map(() => ({...interpolated}));
    }
  }
  return {...whole, status:'available', scope:'final_pcm', sample_rate:fs, channels,
    duration_seconds:frames / fs, loudness:{integrated_lufs:loudness?.integrated_lufs ?? null,
      short_term_max_lufs:loudness?.short_term_max_lufs ?? null,
      mean_k_weighted_lufs:loudness?.mean_k_weighted_lufs ?? null,
      integrated_blocks:loudness?.integrated_blocks ?? 0, dual_mono:channels === 1,
      method:audible ? 'BS.1770-4 K weighting from frame zero; 400 ms/100 ms, -70 LUFS and -10 LU gates' : null,
      reason:audible ? null : 'Raw field components have no speaker loudness assignment.',
      ...interpolated},
    stereo:stereoSummary(full, 'whole record'),
    ending:{threshold_dbfs:-60, last_above_threshold_frame:lastAboveThreshold < 0 ? null : lastAboveThreshold,
      below_threshold_from_frame:lastAboveThreshold + 1 < frames ? lastAboveThreshold + 1 : null,
      final_window_frames:endWindow.frames, final_window_peak_dbfs:db(endWindow.peak / 32768),
      final_window_rms_dbfs:summarize(endWindow, fs).rms_dbfs},
    trailing_digital_silence_seconds:(frames - 1 - lastNonzero) / fs,
    windows:windows.map(window => summarize(window, fs)), sections:sections.map((section,index) => ({...summarize(section, fs),stereo:stereoSummary(section, 'section'),...sectionPeaks[index]})),
    units:{samples:'PCM16 LSB', level:'dBFS', loudness:'LUFS', time:'seconds', channel:'zero based'},
    notes:['Window/section K weighting retains complete preceding filter history; gates apply within each selection.',
      'Full-scale samples count PCM rails, not internal clipping or limiter activity.',
      'Digital silence means exact zero; release tails and musical quality are not inferred.']};
}

export function diagnoseEvents(result, ending) {
  const structure = songStructure(result);
  const unavailable = reason => ({status:'unavailable', scope:'native_event_gates', reason, structure,
    final_gate_release_frame:null, mix_settle_after_final_release_seconds:null});
  const uint = (value, maximum = Number.MAX_SAFE_INTEGER) =>
    (typeof value === 'number' || typeof value === 'string' && /^\d+$/.test(value)) &&
    Number.isSafeInteger(Number(value)) && Number(value) >= 0 && Number(value) <= maximum;
  const info = result?.info, events = result?.events;
  const count = Number(info?.voices), fs = Number(result?.sampleRate ?? info?.fs);
  if (!Array.isArray(events) || result.eventsError || result.eventsTruncated || events.length > analysisLimits.events ||
      !uint(info?.voices,64) || !count || !uint(fs,0xffffffff) || !fs ||
      (info.fs !== undefined && (!uint(info.fs,0xffffffff) || Number(info.fs) !== fs)) ||
      (info.frames !== undefined && (!uint(info.frames,0xffffffff) || !Number(info.frames))))
    return unavailable('Complete native events and valid voice geometry are required.');
  // Older callers may provide gate times without frame totals or pitch fields.
  // An explicit native count covers authored rows only; named lanes are generated.
  if (info.events !== undefined && (!uint(info.events,analysisLimits.events) ||
      events.filter(event => event?.lane === undefined).length !== Number(info.events)))
    return unavailable('Native event count differs from the complete listing.');
  const voices = Array.from({length:count}, (_, voice) => ({voice, streams:new Set(), patches:new Set(),
    onsets:0, offs:0, held_retriggers:0, same_frame_on_off:0, first_on_seconds:null, last_on_seconds:null,
    explicit_position_before_first_on:false, held:false, positioned:false, lastOnFrame:null}));
  const streams = new Map();
  // Native authored rows precede generated rows within the same sample frame.
  const ordered = [];
  for (const event of events) {
    if (!event || !['on','off','pos','move','pitch','control'].includes(event.kind) ||
        !uint(event.voice,count-1) || !uint(event.time_ns) ||
        (['on','pitch'].includes(event.kind) && event.freq_millihz !== undefined && !uint(event.freq_millihz,0xffffffff)))
      return unavailable('Incomplete or unsupported native event fields.');
    const ns = Number(event.time_ns), frame = Number(BigInt(ns) * BigInt(fs) / 1000000000n);
    if (!Number.isSafeInteger(frame) || (info.frames !== undefined && frame >= Number(info.frames)) ||
        (event.frame !== undefined && (!uint(event.frame) || Number(event.frame) !== frame)))
      return unavailable('Native event timing is inconsistent with the recording.');
    ordered.push({...event, voice:Number(event.voice), ns, frame});
  }
  ordered.sort((a, b) => a.frame - b.frame);
  let finalReleaseFrame = null;
  for (const event of ordered) {
    const voice = voices[event.voice], frame = event.frame;
    if (event.kind === 'pos' || event.kind === 'move') voice.positioned = true;
    if (event.kind === 'on') {
      const stream = String(event.stream ?? (event.lane === undefined ? 'unknown' : `generator-${event.lane}`));
      voice.streams.add(stream); if (event.patch) voice.patches.add(event.patch);
      if (voice.first_on_seconds === null) {
        voice.first_on_seconds = event.ns / 1e9; voice.explicit_position_before_first_on = voice.positioned;
      }
      voice.last_on_seconds = event.ns / 1e9; ++voice.onsets;
      if (voice.held) ++voice.held_retriggers;
      voice.held = true; voice.lastOnFrame = frame;
      if (!streams.has(stream)) streams.set(stream, {stream, voices:new Set(), onsets:0});
      streams.get(stream).voices.add(event.voice); ++streams.get(stream).onsets;
    } else if (event.kind === 'off') {
      if (voice.held) finalReleaseFrame = frame;
      ++voice.offs; if (voice.held && voice.lastOnFrame === frame) ++voice.same_frame_on_off;
      voice.held = false;
    }
  }
  const warnings = [];
  for (const voice of voices) {
    if (voice.same_frame_on_off) warnings.push({code:'on_then_off_same_frame', voice:voice.voice,
      count:voice.same_frame_on_off, message:'A native note-on is followed by note-off in the same sample frame.'});
    if (voice.held_retriggers) warnings.push({code:'held_voice_retrigger', voice:voice.voice,
      count:voice.held_retriggers, message:'Note-on retriggers a voice whose previous gate is still on.'});
    if (voice.held) warnings.push({code:'gate_at_end', voice:voice.voice, message:'The last native gate remains on at the record end.'});
  }
  if (structure.status !== 'available' || voices.some(voice => voice.held)) finalReleaseFrame = null;
  const settledFrame = ending?.below_threshold_from_frame;
  return {status:'available', scope:'native_event_gates', structure, final_gate_release_frame:finalReleaseFrame,
    mix_settle_after_final_release_seconds:finalReleaseFrame !== null && Number.isInteger(settledFrame)
      ? Math.max(0, settledFrame - finalReleaseFrame) / fs : null,
    voices:voices.map(({held,positioned,lastOnFrame,...voice}) =>
    ({...voice, streams:[...voice.streams], patches:[...voice.patches], gate_at_end:held})),
    streams:[...streams.values()].map(stream => ({...stream, voices:[...stream.voices]})), warnings,
    sends:{status:'unavailable', reason:'This native event export labels sends as generic control rows; bus identity is not exposed.'},
    release_tails:{status:'unavailable', reason:'Native gate events do not identify release-envelope completion or audible tail theft.'}};
}

// Streaming dry Q2.30 consumer for native --stems-fd. It retains only filter
// state, 100 ms power blocks, summary accumulators and one partial frame.
export function createDryAnalyzer(info, events, options = {}) {
  const fs = integer(Number(info.fs), 8000, 192000, 'sample rate');
  const voices = integer(Number(info.voices), 1, 64, 'voice count');
  const frames = integer(Number(info.frames), 1, fs * analysisLimits.seconds, 'frame count');
  const maxBytes = integer(options.maxBytes ?? analysisLimits.defaultDryBytes, 44, analysisLimits.dryBytes, 'dry byte budget');
  const expectedBytes = frames * voices * 4;
  requireValue(expectedBytes <= maxBytes, `Dry source analysis needs ${expectedBytes} bytes; budget is ${maxBytes}.`);
  const diagnostics = diagnoseEvents({info, sampleRate:fs, events});
  const memberships = diagnostics.status === 'available' ? diagnostics.voices : [];
  const selected = options.voices ?? Array.from({length:voices}, (_, i) => i);
  requireValue(Array.isArray(selected) && selected.length && selected.length <= voices && new Set(selected).size === selected.length,
    'Select distinct native voice indices.');
  selected.forEach(voice => integer(voice, 0, voices - 1, 'voice index'));
  const filters = weighting(fs, selected.length), meters = selected.map(() => new Loudness(fs));
  const stats = selected.map(() => ({peak:0, peakFrame:null, squares:0, sum:0}));
  const groups = new Map();
  selected.forEach((voice, s) => {
    const streams = memberships[voice]?.streams;
    if (streams?.length === 1) {
      if (!groups.has(streams[0])) groups.set(streams[0], {stream:streams[0], indices:[], meter:new Loudness(fs),
        filters:weighting(fs, 1)[0], peak:0, peakFrame:null, squares:0});
      groups.get(streams[0]).indices.push(s);
    }
  });
  const normalized = new Float64Array(selected.length);
  let header = new Uint8Array(44), headerSize = 0, carry = new Uint8Array(), bytes = 0, seen = 0;
  let lo = 0x84222325, hi = 0xcbf29ce4, finished = false;
  function feed(chunk) {
    cancelled(options); requireValue(!finished && chunk instanceof Uint8Array, 'Expected an open dry byte stream.');
    requireValue(chunk.length <= 1024 * 1024, 'Dry chunk exceeds 1 MiB.');
    bytes += chunk.length; requireValue(bytes <= expectedBytes + 44, 'Dry stream exceeds inspected dimensions.');
    if (headerSize < 44) {
      const take = Math.min(44 - headerSize, chunk.length); header.set(chunk.subarray(0, take), headerSize);
      headerSize += take; chunk = chunk.subarray(take);
      if (headerSize < 44) return;
      const d = new DataView(header.buffer), text = new TextDecoder();
      requireValue(text.decode(header.subarray(0,4)) === 'RIFF' && d.getUint32(4,true) === expectedBytes + 36 &&
        text.decode(header.subarray(8,16)) === 'WAVEfmt ' && d.getUint32(16,true) === 16 && d.getUint16(20,true) === 1 &&
        d.getUint16(22,true) === voices && d.getUint32(24,true) === fs && d.getUint32(28,true) === fs * voices * 4 &&
        d.getUint16(32,true) === voices * 4 && d.getUint16(34,true) === 32 &&
        text.decode(header.subarray(36,40)) === 'data' && d.getUint32(40,true) === expectedBytes,
      'Native dry header differs from inspected geometry.');
    }
    for (const byte of chunk) {
      lo = (lo ^ byte) >>> 0;
      const carryProduct = Math.floor(lo * 435 / 4294967296);
      hi = (Math.imul(hi, 435) + Math.imul(lo, 256) + carryProduct) >>> 0;
      lo = Math.imul(lo, 435) >>> 0;
    }
    let data = chunk;
    if (carry.length) {data = new Uint8Array(carry.length + chunk.length); data.set(carry); data.set(chunk, carry.length);}
    const stride = voices * 4, complete = data.length - data.length % stride;
    const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
    for (let offset = 0; offset < complete; offset += stride, ++seen) {
      for (let s = 0; s < selected.length; ++s) {
        const raw = view.getInt32(offset + selected[s] * 4, true), x = normalized[s] = raw / 1073741824, stat = stats[s];
        if (Math.abs(raw) > stat.peak) {stat.peak = Math.abs(raw); stat.peakFrame = seen;}
        stat.squares += x * x; stat.sum += x;
        const y = filters[s][1].push(filters[s][0].push(x)); meters[s].push(2 * y * y);
      }
      for (const group of groups.values()) {
        let x = 0; for (const s of group.indices) x += normalized[s];
        if (Math.abs(x) > group.peak) {group.peak = Math.abs(x); group.peakFrame = seen;}
        group.squares += x * x;
        const y = group.filters[1].push(group.filters[0].push(x)); group.meter.push(2 * y * y);
      }
    }
    carry = data.slice(complete);
  }
  function finish(stems) {
    cancelled(options); requireValue(!finished, 'Dry stream is already finished.'); finished = true;
    const hash = hi.toString(16).padStart(8,'0') + lo.toString(16).padStart(8,'0');
    requireValue(headerSize === 44 && bytes === expectedBytes + 44 && seen === frames && !carry.length,
      'Native dry stream is incomplete.');
    requireValue(stems?.format === 's32le_q2_30' && stems.stage === 'voice_out_pre_fx' &&
      String(stems.fs) === String(fs) && String(stems.frames) === String(frames) && String(stems.voices) === String(voices) &&
      String(stems.bytes) === String(expectedBytes) && stems.hash === hash && stems.partition === '1' &&
      stems.passes === '1' && stems.block_indep === 'unchecked',
    'Native STEMS metadata or payload hash differs.');
    return {status:'available', scope:'voice_out_pre_fx', sample_rate:fs, frames, bytes:expectedBytes, hash,
      voices:selected.map((voice, s) => ({voice, streams:memberships[voice]?.streams ?? [], patches:memberships[voice]?.patches ?? [],
        peak_q30_lsb:stats[s].peak, peak_db_q30_unity:db(stats[s].peak / 1073741824), peak_frame:stats[s].peakFrame,
        rms_db_q30_unity:db(Math.sqrt(stats[s].squares / frames)), dc_mean:stats[s].sum / frames,
        loudness:{...meters[s].result(), dual_mono:true}})),
      streams:[...groups.values()].map(group => ({stream:group.stream, voices:group.indices.map(s => selected[s]),
        peak_db_q30_unity:db(group.peak), peak_frame:group.peakFrame,
        rms_db_q30_unity:db(Math.sqrt(group.squares / frames)), loudness:{...group.meter.result(), dual_mono:true}})),
      ungrouped_voices:selected.filter(voice => memberships[voice]?.streams.length !== 1),
      notes:['Dry voice output before source crusher, positioning, sends and master effects; not final audible stem levels.',
        'LUFS uses hypothetical dual-mono audition of each source, not its actual spatial image.',
        'Stream rows sum only selected voices with exactly one native note-on stream; ambiguous or unlabelled voices remain separate.',
        'Selected voices reduce analysis work; the native producer still emits all voices.']};
  }
  return {feed, finish, requiredBytes:expectedBytes, maxBytes};
}
