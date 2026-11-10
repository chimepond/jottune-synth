// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import {parseDocument,composeDocument,validateMetadata} from './tl-document.mjs';
// Score-v1 authoring adapter. Contracts: tools/score.py and tools/music/compose.py.
// This writes TL; it never reads TL or substitutes for native validation.
const encoder = new TextEncoder();
const MAX = Number.MAX_SAFE_INTEGER;
const OWNED = new Set(['timeline', 'engine', 'fs', 'length_ns', 'streams', 'voice', 'hash']);
const FIELDS = Object.freeze({
  note: ['voice', 'duration', 'pitch', 'velocity_q15'], on: ['voice', 'pitch', 'velocity_q15'],
  off: ['voice'], pitch: ['voice', 'pitch'], ctl: ['voice', 'input', 'value_q15'],
  filter: ['voice', 'cutoff_millihz', 'q_milli'], send: ['voice', 'bus', 'level_q15'],
  crush: ['voice', 'bits', 'rate_div'], pos: ['voice', 'x_mm', 'y_mm', 'z_mm'],
  move: ['voice', 'duration', 'x_mm', 'y_mm', 'z_mm'], yaw: ['yaw_mdeg'],
  gen: ['id', 'param', 'value'], lane: ['id', 'enabled'], mark: ['text'],
});
export class ScoreError extends Error {
  constructor(message, pointer = '', details = {}) {
    super(message); this.name = 'ScoreError'; this.pointer = pointer; this.details = details;
  }
}
function requireValue(ok, message, pointer = '', details = {}) {
  if (!ok) throw new ScoreError(message, pointer, details);
}
const address = (base, key) => base + '/' + String(key).replace(/~/g, '~0').replace(/\//g, '~1');
const object = value => value !== null && typeof value === 'object' &&
  [Object.prototype, null].includes(Object.getPrototypeOf(value));
function text(value, pointer, {single = false, nonempty = false} = {}) {
  requireValue(typeof value === 'string', 'Must be a string.', pointer);
  requireValue(!value.includes('\0'), 'Text must be valid UTF-8 and contain no NUL.', pointer);
  for (let i = 0; i < value.length; i++) {
    const unit = value.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(++i);
      requireValue(next >= 0xdc00 && next <= 0xdfff, 'Text must be valid UTF-8.', pointer);
    } else requireValue(unit < 0xdc00 || unit > 0xdfff, 'Text must be valid UTF-8.', pointer);
  }
  requireValue(encoder.encode(value).length <= 1048576, 'Text exceeds 1 MiB.', pointer);
  requireValue(!single || !/[\r\n]/.test(value), 'Must be a single-line string.', pointer);
  requireValue(!nonempty || value.length > 0, 'Must be nonempty.', pointer);
  return value;
}
function closed(value, required, pointer, optional = []) {
  requireValue(object(value), 'Must be an object.', pointer);
  for (const key of Object.keys(value)) requireValue(required.includes(key) || optional.includes(key), 'Unknown score field.', address(pointer, key));
  for (const key of required) requireValue(Object.hasOwn(value, key), 'Required score field is missing.', address(pointer, key));
}
function integer(value, pointer, low = -MAX, high = MAX) {
  requireValue(Number.isSafeInteger(value), 'Must be an exact safe integer; use native TL for larger integer fields.', pointer);
  requireValue(value >= low && value <= high, 'Integer is outside the allowed range.', pointer, {minimum: low, maximum: high});
  return value;
}
function identifier(value, pointer) {
  text(value, pointer);
  requireValue(/^[A-Za-z_][A-Za-z0-9_-]*$/.test(value), 'Must be an identifier.', pointer);
  return value;
}
function list(value, pointer, low, high) {
  requireValue(Array.isArray(value) && value.length >= low && value.length <= high, 'Array length is outside the allowed range.', pointer, {minimum: low, maximum: high});
}
function snapshot(value) {
  const ancestors = new Set();
  function visit(item, pointer, depth) {
    requireValue(depth <= 128, 'Score JSON is nested too deeply.', pointer);
    if (typeof item === 'string') return text(item, pointer);
    if (item === null || typeof item === 'boolean') return item;
    if (typeof item === 'number') {
      requireValue(Number.isFinite(item), 'JSON numbers must be finite.', pointer); return item;
    }
    requireValue(Array.isArray(item) || object(item), 'Score values must be JSON values.', pointer);
    requireValue(!ancestors.has(item), 'Score JSON must not contain cycles.', pointer); ancestors.add(item);
    const result = Array.isArray(item) ? item.map((v, i) => visit(v, address(pointer, i), depth + 1)) : Object.fromEntries(
      Object.keys(item).sort().map(key => [text(key, pointer), visit(item[key], address(pointer, key), depth + 1)]));
    ancestors.delete(item); return result;
  }
  const result = visit(value, '', 0);
  requireValue(encoder.encode(JSON.stringify(result)).length <= 1048576, 'Score JSON exceeds 1 MiB.');
  return result;
}
function gcd(a, b) { a = a < 0n ? -a : a; while (b) [a, b] = [b, a % b]; return a; }
function fraction(n, d = 1n) {
  if (d < 0n) { n = -n; d = -d; }
  const divisor = gcd(n, d); return {n: n / divisor, d: d / divisor};
}
const add = (a, b) => fraction(a.n * b.d + b.n * a.d, a.d * b.d);
const neg = a => ({n: -a.n, d: a.d});
const sub = (a, b) => add(a, neg(b));
const mul = (a, b) => fraction(a.n * b.n, a.d * b.d);
const div = (a, b) => fraction(a.n * b.d, a.d * b.n);
const cmp = (a, b) => a.n * b.d < b.n * a.d ? -1 : a.n * b.d > b.n * a.d ? 1 : 0;
const zero = fraction(0n), whole = value => fraction(BigInt(value));
const spelling = a => a.d === 1n ? String(a.n) : a.n + '/' + a.d;
function rational(value, pointer) {
  let n, d;
  if (typeof value === 'number') { integer(value, pointer); n = BigInt(value); d = 1n; }
  else {
    requireValue(typeof value === 'string' && value.length <= 22 && /^-?(0|[1-9][0-9]*)\/[1-9][0-9]*$/.test(value),
      'Must be an integer or a bounded numerator/denominator rational.', pointer);
    [n, d] = value.split('/').map(BigInt);
  }
  requireValue(n >= -BigInt(MAX) && n <= BigInt(MAX) && d <= 4096n, 'Rational exceeds numerator or denominator bounds.', pointer);
  return fraction(n, d);
}
// The musical clock rounds nearest, ties away from zero; it is not Math.round.
function round(a) { return a.n < 0n ? -round(neg(a)) : (2n * a.n + a.d) / (2n * a.d); }
function clockOf(clock) {
  closed(clock, ['bars', 'meter', 'tempo', 'ramp', 'tail_ns'], '/clock');
  const bars = integer(clock.bars, '/clock/bars', 1, 65536);
  integer(clock.tail_ns, '/clock/tail_ns', 0, 600000000000);
  requireValue(['step', 'linear'].includes(clock.ramp), 'Ramp must be step or linear.', '/clock/ramp');
  function anchors(name, field, maxBar) {
    list(clock[name], '/clock/' + name, 1, 128);
    let previous = 0;
    return clock[name].map((row, i) => {
      const pointer = '/clock/' + name + '/' + i;
      closed(row, ['bar', field], pointer);
      integer(row.bar, pointer + '/bar', 1, maxBar);
      requireValue(row.bar > previous && (i !== 0 || row.bar === 1), 'Map anchors must begin at bar 1 and strictly increase.', pointer + '/bar');
      const value = rational(row[field], pointer + '/' + field);
      requireValue(value.n > 0n, 'Must be a positive rational.', pointer + '/' + field);
      previous = row.bar; return {bar: row.bar, value};
    });
  }
  const meter = anchors('meter', 'quarters', bars), tempo = anchors('tempo', 'bpm', bars + 1);
  function atBar(bar) {
    let total = zero, previous = 1, width = meter[0].value;
    for (const row of meter.slice(1)) {
      if (row.bar >= bar) break;
      total = add(total, mul(whole(row.bar - previous), width)); previous = row.bar; width = row.value;
    }
    return add(total, mul(whole(bar - previous), width));
  }
  const extent = atBar(bars + 1);
  requireValue(extent.n > 0n && cmp(extent, whole(65536)) <= 0, 'Quarter extent must be positive and at most 65536.', '/clock/meter');
  const points = tempo.map(row => ({at: atBar(row.bar), duration: div(whole(60000000000), row.value)}));
  if (cmp(points.at(-1).at, extent) < 0) points.push({at: extent, duration: points.at(-1).duration});
  const segments = []; let elapsed = zero;
  const time = (segment, at) => {
    const offset = sub(at, segment.at);
    return add(segment.elapsed, add(mul(segment.duration, offset), div(mul(segment.slope, mul(offset, offset)), whole(2))));
  };
  for (let i = 0; i < points.length - 1; i++) {
    const first = points[i], next = points[i + 1];
    const segment = {...first, end: next.at, elapsed, slope: clock.ramp === 'linear' ? div(sub(next.duration, first.duration), sub(next.at, first.at)) : zero};
    segments.push(segment); elapsed = time(segment, next.at);
  }
  function atQuarter(at) {
    requireValue(cmp(at, zero) >= 0 && cmp(at, extent) <= 0, 'Time is outside the musical extent.', '/clock');
    let segment = segments[0];
    for (const next of segments) { if (cmp(at, next.at) < 0) break; segment = next; }
    return Number(round(time(segment, at)));
  }
  const length = round(elapsed) + BigInt(clock.tail_ns);
  requireValue(length > 0n && length <= 600000000000n, 'Length must be positive and at most 600e9 ns.', '/clock');
  return {extent, length: Number(length), atQuarter};
}

/** Exact score-v1 pitch conversion. Integer MIDI values use the existing compiler's generated table. */
export function pitchMillihz(value, pointer = '/pitch') {
  requireValue(object(value) && Object.keys(value).length === 1, 'Pitch must contain exactly one route.', pointer);
  if (Object.hasOwn(value, 'millihz')) return integer(value.millihz, pointer + '/millihz');
  let midi;
  if (Object.hasOwn(value, 'midi')) midi = integer(value.midi, pointer + '/midi', 0, 127);
  else {
    requireValue(Object.hasOwn(value, 'note'), 'Unknown pitch route.', pointer);
    text(value.note, pointer + '/note', {single: true});
    const match = /^([A-G])([#b]?)(-?\d)$/.exec(value.note);
    requireValue(match, 'Malformed note spelling.', pointer + '/note');
    midi = 12 * (Number(match[3]) + 1) + ({C:0,D:2,E:4,F:5,G:7,A:9,B:11})[match[1]] + ({'#':1,b:-1,'':0})[match[2]];
    integer(midi, pointer + '/note', 0, 127);
  }
  return MIDI_MILLIHZ[midi];
}

// Python op() rounds the binary floating-point level*10 to nearest, ties even.
function levelDb10(db) {
  const value = Math.min(120, Math.max(0, db)) * 10, lower = Math.floor(value), remainder = value - lower;
  return lower + Number(remainder > .5 || (remainder === .5 && lower % 2 === 1));
}
function signedDelta(value) {
  // Recipe comments use the existing Python `%+g` six-significant-digit form.
  const negative = value < 0 || Object.is(value, -0), magnitude = Math.abs(value);
  const [mantissa, exponentText] = magnitude.toExponential(5).split('e'), exponent = Number(exponentText);
  const shown = exponent < -4 || exponent >= 6
    ? mantissa.replace(/\.?0+$/, '') + 'e' + (exponent < 0 ? '-' : '+') + String(Math.abs(exponent)).padStart(2, '0')
    : Number(magnitude.toPrecision(6)).toString();
  return (negative ? '-' : '+') + shown;
}

/** Expand an existing preset with Python-compatible carrier attenuation deltas. */
export function recipe(name, patch, tweaks = {}) {
  text(name, '/recipe', {single:true, nonempty:true});
  patch ??= name.replace(/[^A-Za-z0-9_-]/g, '_');
  requireValue(Object.hasOwn(RECIPE_DATA, name), 'Unknown hosted recipe. Use the listed presets or complete native declarations.', '/declarations');
  requireValue(typeof patch === 'string' && /^[A-Za-z0-9_.-]{1,31}$/.test(patch), 'Recipe patch name must be 1–31 ASCII letters, digits, dots, underscores or hyphens.', '/declarations');
  const entry = RECIPE_DATA[name], allowed = new Set(entry.carriers.map(carrier => carrier.tweak));
  requireValue(object(tweaks), 'Recipe level tweaks must be an object.', '/declarations');
  for (const [key, delta] of Object.entries(tweaks)) {
    requireValue(allowed.has(key), 'Only named level tweaks of this recipe’s carriers are supported.', '/declarations', {tweak:key, allowed:[...allowed]});
    requireValue(typeof delta === 'number' && Number.isFinite(delta), 'Recipe level delta must be finite dB attenuation.', '/declarations', {tweak:key});
  }
  const said = Object.entries(tweaks).map(([key, delta]) => key + ' ' + signedDelta(delta)).join(', ');
  return entry.rows.map(template => {
    let row = template;
    for (const carrier of entry.carriers)
      row = row.replaceAll('{level:' + carrier.operator + '}', String(levelDb10(carrier.attenuationDb + (tweaks[carrier.tweak] ?? 0))).padEnd(10));
    if (said) row = row.replace('# recipe ' + name + ':', '# recipe ' + name + ' (' + said + '):');
    return row.replaceAll('{patch:8}', patch.padEnd(8)).replaceAll('{patch}', patch);
  }).join('\n') + '\n';
}

/** Signed dB gain across every carrier, preserving their balance without clamps. */
export function recipeGain(name, patch, gainDb) {
  recipe(name, patch); // Validate names before reading generated carrier metadata.
  requireValue(typeof gainDb === 'number' && Number.isFinite(gainDb) && Number.isSafeInteger(gainDb * 10),
    'Recipe gain must be a finite multiple of 0.1 dB.', '/declarations');
  const carriers = RECIPE_DATA[name].carriers;
  requireValue(carriers.every(carrier => {
    const level = levelDb10(carrier.attenuationDb) - gainDb * 10;
    return level >= 0 && level <= 1200;
  }), 'Requested gain would exceed a carrier’s 0–120 dB attenuation range; no levels were clamped.', '/declarations');
  return gainDb === 0 ? recipe(name, patch) : recipe(name, patch,
    Object.fromEntries(carriers.map(carrier => [carrier.tweak, -gainDb])));
}
export function listRecipes() {
  return Object.entries(RECIPE_DATA).map(([id, entry]) => ({id, description: entry.description,
    carriers:entry.carriers.map(carrier => ({...carrier}))}));
}
export function authoringDescription() {
  return {version: 1, source: 'tools/score.py score version 1', eventKinds: Object.keys(FIELDS),
    limits: {sourceBytes:1048576, authoredBytes:4194304, events:8192, quarters:65536, seconds:600, streams:16, voices:64},
    clock: 'Exact rational quarters; step or linear ns/quarter; cumulative nearest-nanosecond ties away from zero.',
    recipes: listRecipes(), recipeLevels: {tweaks:'Per-carrier c_level/m_level/o2_level/o3_level dB attenuation deltas; positive is quieter; Python clamp and round rules.',
      gain:'recipeGain(name,patch,gainDb): signed gain for all carriers in 0.1 dB steps; refuses clamping.'},
    nativeValidation: 'Always required. No native render or TL parsing occurs here.',
    unsupported: ['Score versions 2 and 3', 'Other recipe tweaks and templates; use expanded native declarations', 'Unsafe JavaScript integer fields; use native TL']};
}

/** Compile score v1 without network, audio, Python, or native parser duplication.
 * engine is the selected renderer's ENGINE identity (without the ENGINE prefix).
 * statements may be native STATEMENT rows for that renderer; their section/name
 * only constrain declaration ownership, never native fields or synthesis rules.
 */
// Score-source selection, never a TL parser or an additive stem extractor.
// Callers must also validate the complete original with the native reader
// before rendering a selection; authoring validation cannot validate raw DSP
// declarations or native parameter limits.
export function selectScoreStreams(input, selection, options = {}) {
  compileScore(input, options);
  const score = snapshot(input);
  requireValue(object(selection), 'A stream selection must be an object.', '/selection');
  const keys = Object.keys(selection);
  requireValue(keys.length === 1 && ['soloStreams', 'muteStreams'].includes(keys[0]),
    'Choose either soloStreams or muteStreams.', '/selection');
  const key = keys[0], names = selection[key];
  list(names, '/selection/' + key, 1, 16);
  names.forEach((name, index) => {
    identifier(name, '/selection/' + key + '/' + index);
    requireValue(score.streams.includes(name), 'Selected stream is not declared.', '/selection/' + key + '/' + index);
  });
  requireValue(new Set(names).size === names.length, 'Selected streams must be distinct.', '/selection/' + key);
  // Generator lanes can create notes independently of score events. Do not
  // imply that removing authored gates silences those voices.
  requireValue(!score.declarations.split('\n').some(line => /^gen(?:_|\s|$)/.test(line.split('#')[0].trim())) &&
    !score.events.some(event => ['gen', 'lane'].includes(event.kind)),
    'Stream selection does not support generator declarations or events. Retain the full score.', '/selection');
  const chosen = new Set(names), keep = new Set(score.streams.filter(name => key === 'soloStreams' ? chosen.has(name) : !chosen.has(name)));
  requireValue(keep.size > 0, 'Selection would mute every stream.', '/selection');
  const gates = new Set(['note', 'on', 'off']), sides = new Map();
  for (const event of score.events) if (gates.has(event.kind)) {
    const side = keep.has(event.stream) ? 1 : 2;
    sides.set(event.voice, (sides.get(event.voice) || 0) | side);
  }
  requireValue(![...sides.values()].includes(3),
    'Selected and muted streams share a voice gate; assign separate voices before selecting streams.', '/selection');
  requireValue(score.events.some(event => ['note', 'on'].includes(event.kind) && keep.has(event.stream)),
    'Selection contains no authored note-on gates.', '/selection');
  const before = score.events.length;
  const eventIndices = [];
  score.events = score.events.filter((event, index) => {
    const retained = !gates.has(event.kind) || keep.has(event.stream);
    if (retained) eventIndices.push(index);
    return retained;
  });
  const authored = compileScore(score, options);
  for (const row of authored.sourceMap)
    row.pointer = row.pointer.replace(/^\/events\/(\d+)(?=\/|$)/, (_, index) => '/events/' + eventIndices[Number(index)]);
  for (const row of authored.advisories) for (const name of ['event_index','previous_event_index','active_event_index'])
    if (Object.hasOwn(row, name)) row[name] = eventIndices[row[name]];
  return {score, authored, selection: {mode:key === 'soloStreams' ? 'solo' : 'mute', requestedStreams:[...names],
    soundingStreams:score.streams.filter(name => keep.has(name)), removedGateEvents:before - score.events.length,
    scope:'score_stream_rerender', nativeValidation:'not_run', sourceMap:'original_score',
    note:'All declarations, controls, duration and effects are retained. This is a solo/mute rerender, not additive stems of the original mix; shared dynamics and effects may respond differently.'}};
}

export function compileScore(input, {engine, statements} = {}) {
  const score = snapshot(input);
  requireValue(object(score) && score.version === 1, 'Only score version 1 is supported; versions 2 and 3 require the local compiler.', '/version');
  closed(score, ['version', 'fs', 'clock', 'declarations', 'streams', 'voices', 'events'], '', ['name', 'about', 'metadata']);
  if (Object.hasOwn(score,'metadata')) validateMetadata(score.metadata);
  requireValue([44100, 48000].includes(score.fs), 'fs must be 44100 or 48000.', '/fs');
  text(engine, '/engine', {single: true, nonempty: true});
  requireValue(!engine.includes('#'), 'Engine identity must be the selected renderer descriptor.', '/engine');
  if (Object.hasOwn(score, 'name')) text(score.name, '/name', {single:true, nonempty:true});
  if (Object.hasOwn(score, 'about')) {
    list(score.about, '/about', 0, 1048576); score.about.forEach((line, i) => text(line, '/about/' + i, {single:true}));
  }
  for (const [name, max] of [['streams',16],['voices',64]]) {
    list(score[name], '/' + name, 1, max); score[name].forEach((v, i) => identifier(v, '/' + name + '/' + i));
  }
  requireValue(new Set(score.streams).size === score.streams.length, 'Streams must be distinct.', '/streams');
  const clock = clockOf(score.clock);
  const declarations = text(score.declarations, '/declarations').replaceAll('\r\n', '\n');
  requireValue(!declarations.split('\n').some(line=>/^[ \t]*#[ \t]*(?:jottune-tl|end-jottune-tl)\b/.test(line)), 'Metadata belongs in score.metadata, before the native timeline.', '/declarations');
  requireValue(!declarations.includes('\r'), 'Declarations contain a lone CR.', '/declarations');
  if (statements !== undefined) requireValue(Array.isArray(statements), 'statements must be native STATEMENT rows.', '/statements');
  const allowed = new Set(statements === undefined ? DECLARATIONS : statements.filter(row => row.row === 'STATEMENT' && row.section !== 'event').map(row => row.name));
  let recipeCount = 0, expanded = '';
  const declarationLines = declarations.match(/[^\n]*\n|[^\n]+$/g) || [];
  declarationLines.forEach((line, index) => {
    const commentAt = line.indexOf('#'), body = commentAt < 0 ? line : line.slice(0, commentAt);
    const words = body.trim().split(/\s+/).filter(Boolean), keyword = words[0];
    if (keyword) requireValue(!OWNED.has(keyword) && (allowed.has(keyword) || keyword === 'recipe'),
      'Declaration keyword is unknown or adapter-owned.', '/declarations', {declaration_line:index + 1, keyword});
    if (keyword !== 'recipe') { expanded += line; return; }
    requireValue(words.length >= 3, 'Expected recipe PATCH PRESET [CARRIER_LEVEL=DELTA ...].', '/declarations', {declaration_line:index + 1});
    const tweaks = {};
    for (const word of words.slice(3)) {
      const match = /^(c_level|m_level|o2_level|o3_level)=([-+]?\d+(?:\.\d+)?)$/.exec(word);
      requireValue(match && !Object.hasOwn(tweaks, match[1]), 'Expected a distinct named carrier level tweak with a decimal dB delta.', '/declarations', {declaration_line:index + 1});
      tweaks[match[1]] = Number(match[2]);
    }
    const comment = commentAt < 0 ? '' : line.slice(commentAt + 1).trim();
    if (comment) expanded += '# ' + comment + '\n';
    expanded += recipe(words[2], words[1], tweaks); recipeCount++;
  });
  list(score.events, '/events', 0, 8192);
  const decoded = score.events.map((event, index) => {
    const pointer = '/events/' + index;
    requireValue(object(event) && Object.hasOwn(FIELDS, event.kind), 'Unknown event kind.', pointer + '/kind');
    closed(event, ['kind', 'at', 'stream', ...FIELDS[event.kind]], pointer);
    identifier(event.stream, pointer + '/stream');
    requireValue(score.streams.includes(event.stream), 'Stream is not declared.', pointer + '/stream');
    const at = rational(event.at, pointer + '/at');
    requireValue(cmp(at, zero) >= 0 && cmp(at, clock.extent) <= 0, 'Event time is outside the musical extent.', pointer + '/at');
    const result = {...event, at, pointer, index};
    for (const key of FIELDS[event.kind]) {
      const value = event[key], path = pointer + '/' + key;
      if (key === 'voice') integer(value, path, 0, score.voices.length - 1);
      else if (key === 'duration') {
        result.duration = rational(value, path);
        requireValue(result.duration.n > 0n && cmp(add(at, result.duration), clock.extent) <= 0, 'Duration must be positive and its endpoint inside the musical extent.', path);
      } else if (key === 'pitch') result.pitch_millihz = pitchMillihz(value, path);
      else if (key === 'param') identifier(value, path);
      else if (key === 'bus') requireValue(['chorus','delay','reverb','duck','flanger'].includes(value), 'Unknown send bus.', path);
      else if (key === 'enabled') integer(value, path, 0, 1);
      else if (key === 'bits') integer(value, path, 1, 24);
      else if (key === 'rate_div') integer(value, path, 1, 64);
      else if (key === 'text') text(value, path, {single:true});
      else integer(value, path);
    }
    return result;
  });
  const lines = [], sourceMap = [], schedule = []; let bytes = 0;
  function emit(line, pointer, role, context = {}) {
    bytes += encoder.encode(line + '\n').length;
    requireValue(bytes <= 4194304, 'Authored TL exceeds 4 MiB.', pointer);
    lines.push(line); sourceMap.push({line:lines.length, pointer, role, ...context});
  }
  if (Object.hasOwn(score,'metadata')) {
    const header=parseDocument(composeDocument('timeline draft2\n',score.metadata)).header;
    header.trimEnd().split('\n').forEach(line=>emit(line,'/metadata','metadata'));
  }
  emit('timeline draft2', '/version', 'owned'); emit('engine ' + engine, '', 'owned_engine');
  emit('fs ' + score.fs, '/fs', 'owned'); emit('length_ns ' + clock.length, '/clock', 'owned');
  emit('streams ' + score.streams.join(' '), '/streams', 'owned');
  if (Object.hasOwn(score, 'name')) emit('# ' + score.name, '/name', 'comment');
  (score.about || []).forEach((line, i) => emit('# ' + line, '/about/' + i, 'comment'));
  const expandedLines = expanded.split('\n'); if (expandedLines.at(-1) === '') expandedLines.pop();
  expandedLines.forEach((line, i) => emit(line, '/declarations', 'declaration', {expanded_line:i + 1}));
  score.voices.forEach((patch, i) => emit('voice ' + i + ' ' + patch, '/voices/' + i, 'owned'));
  function scheduled(event, at, kind, generatedOff = false) {
    schedule.push({event_index:event.index, at_ns:at, stream_index:score.streams.indexOf(event.stream),
      source_order:schedule.length, voice:event.voice, kind, generated_off:generatedOff});
  }
  for (const event of decoded) {
    const {kind, stream, pointer} = event, at = clock.atQuarter(event.at);
    if (kind === 'mark') { emit('# ' + at + ' ' + stream + ' ' + event.text, pointer, 'mark'); continue; }
    let command;
    if (kind === 'gen') command = 'gen ' + event.id + ' ' + event.param + ' ' + event.value;
    else if (kind === 'lane') command = 'lane ' + event.id + ' enable ' + event.enabled;
    else if (kind === 'yaw') command = '0 yaw ' + event.yaw_mdeg;
    else {
      let args;
      if (['note','on','pitch'].includes(kind)) args = [event.pitch_millihz, ...(kind === 'pitch' ? [] : [event.velocity_q15])];
      else if (['pos','move'].includes(kind)) args = [event.x_mm,event.y_mm,event.z_mm, ...(kind === 'move' ? [clock.atQuarter(add(event.at,event.duration)) - at] : [])];
      else args = FIELDS[kind].filter(field => field !== 'voice').map(field => event[field]);
      command = event.voice + ' ' + (kind === 'note' ? 'on' : kind) + (args.length ? ' ' + args.join(' ') : '');
    }
    emit(at + ' ' + stream + ' ' + command, pointer, kind === 'note' ? 'on' : kind);
    scheduled(event, at, kind === 'note' ? 'on' : kind);
    if (kind === 'note') {
      const end = clock.atQuarter(add(event.at, event.duration));
      emit(end + ' ' + stream + ' ' + event.voice + ' off', pointer, 'off'); scheduled(event, end, 'off', true);
    }
  }
  schedule.sort((a,b) => a.at_ns - b.at_ns || a.stream_index - b.stream_index || a.source_order - b.source_order);
  const active = Array(score.voices.length).fill(null), lastOn = active.slice(), advisories = [];
  const warn = (kind, row, context = {}) => advisories.push({kind, event_index:row.event_index, at_ns:row.at_ns,
    stream_index:row.stream_index, voice:row.voice, generated_off:row.generated_off, ...context});
  for (const row of schedule) {
    if (!['on','off'].includes(row.kind)) continue;
    const previous = active[row.voice];
    if (row.kind === 'on') {
      if (previous) warn('retrigger', row, {previous_event_index:previous.event_index});
      if (lastOn[row.voice]?.at_ns === row.at_ns) warn('simultaneous_on', row, {previous_event_index:lastOn[row.voice].event_index});
      active[row.voice] = lastOn[row.voice] = row;
    } else {
      if (!previous) warn(row.generated_off ? 'note_off_without_active_gate' : 'explicit_off_without_active_gate', row);
      else if (row.generated_off && previous.event_index !== row.event_index) warn('earlier_note_off_closes_retriggered_gate', row, {active_event_index:previous.event_index});
      active[row.voice] = null;
    }
  }
  for (const row of active) if (row) warn('held_gate_at_authored_end', {...row, at_ns:clock.atQuarter(clock.extent)}, {at_selection_end:true});
  const draftText = lines.join('\n') + '\n';
  return {draftText, authored_tl:draftText, sourceMap, line_map:sourceMap, advisories,
    sourceVersion:1, fs:score.fs, length_ns:clock.length, quarter_extent:spelling(clock.extent),
    authored_tl_bytes:bytes, counts:{input_events:score.events.length, authored_event_rows:schedule.length,
      authored_lines:lines.length, expanded_declaration_lines:expandedLines.length, recipes:recipeCount,
      streams:score.streams.length, voices:score.voices.length},
    coverage:{scope:'authored_source_only', native_validation:'not_run', generator_activity:'unknown',
      gate_advice:'not_envelope_activity_or_audibility', verification:'unverified_draft'}};
}

// Generated below from the existing Python compiler and native parameter rows.
const MIDI_MILLIHZ = Object.freeze([8176,8662,9177,9723,10301,10913,11562,12250,12978,13750,14568,15434,16352,17324,18354,19445,20602,21827,23125,24500,25957,27500,29135,30868,32703,34648,36708,38891,41203,43654,46249,48999,51913,55000,58270,61735,65406,69296,73416,77782,82407,87307,92499,97999,103826,110000,116541,123471,130813,138591,146832,155563,164814,174614,184997,195998,207652,220000,233082,246942,261626,277183,293665,311127,329628,349228,369994,391995,415305,440000,466164,493883,523251,554365,587330,622254,659255,698456,739989,783991,830609,880000,932328,987767,1046502,1108731,1174659,1244508,1318510,1396913,1479978,1567982,1661219,1760000,1864655,1975533,2093005,2217461,2349318,2489016,2637020,2793826,2959955,3135963,3322438,3520000,3729310,3951066,4186009,4434922,4698636,4978032,5274041,5587652,5919911,6271927,6644875,7040000,7458620,7902133,8372018,8869844,9397273,9956063,10548082,11175303,11839822,12543854]);
const DECLARATIONS = Object.freeze(["patch","route","filter","shape","amp","lfo","peg","fenv","eg","ks","kstype","fbtype","egloop","noise","pd","vector","formant","ksource","wtbank","wthash","harmonic","wtdata","wtuse","mod","space","block","near","order","pinna","yaw","panlaw","dcblock","chorus","flanger","delay","pingpong","reverb","compressor","limiter","softclip","gain","dither","crush","decodable","gen_clock","gen","gen_axiom","gen_rule","gen_lane"]);
// Default recipes and carrier metadata generated from compose.py RECIPES and
// patch_headers. Level placeholders replace only generated carrier fields; no
// submitted native declaration is parsed or rewritten by recipe level helpers.
// Wavetable presets needing shared bank reconciliation use the local expander.
const RECIPE_DATA = {
  "epiano": {
    "description": "electric piano, 1:1; velocity opens the index, which decays faster than the level (waltz)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":17}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            180         2         2400     300          350         # recipe epiano: electric piano, 1:1; velocity opens the index, which decays faster than the level (waltz)",
      "patch {patch:8} 1  65536     0          1638      0            220         1         700      240          350"
    ]
  },
  "ep-body": {
    "description": "electric-piano body, 1:1, velocity opens the index; layer ep-tine on the same key (nocturne)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":14}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         2         8000     500          450         # recipe ep-body: electric-piano body, 1:1, velocity opens the index; layer ep-tine on the same key (nocturne)",
      "patch {patch:8} 1  65536     0          1126      0            230         1         1400     140          450"
    ]
  },
  "ep-tine": {
    "description": "electric-piano tine: the modulator at 14 times the note, a metallic strike that dies away (nocturne)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":24}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            256         1         1400     1200         300         # recipe ep-tine: electric-piano tine: the modulator at 14 times the note, a metallic strike that dies away (nocturne)",
      "patch {patch:8} 1  917504    0          1536      0            220         1         600      1200         200"
    ]
  },
  "harpsichord": {
    "description": "1:1, a fed-back modulator, no touch sensitivity (invention)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":14}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            0           1         9000     1200         160         # recipe harpsichord: 1:1, a fed-back modulator, no touch sensitivity (invention)",
      "patch {patch:8} 1  65536     0          2048      1536         0           1         2200     90           160"
    ]
  },
  "harp": {
    "description": "1:1 pluck (canon)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":18}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            180         2         1800     1200         600         # recipe harp: 1:1 pluck (canon)",
      "patch {patch:8} 1  65536     0          1229      0            200         1         400      400          400"
    ]
  },
  "organ": {
    "description": "1:1; the index starts 5 dB high and settles in 45 ms, a key click (shuffle)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":17}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            64          4         60       0            45          # recipe organ: 1:1; the index starts 5 dB high and settles in 45 ms, a key click (shuffle)",
      "patch {patch:8} 1  65536     0          1024      307          64          1         45       50           45"
    ]
  },
  "marimba": {
    "description": "1:3, a short bright strike (clockwork)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":11}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         1         1100     1200         250         # recipe marimba: 1:3, a short bright strike (clockwork)",
      "patch {patch:8} 1  196608    0          2458      0            200         1         80       600          100"
    ]
  },
  "celesta": {
    "description": "1:4, odd harmonics fading into a pure ring (first-light)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":10}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            160         2         3500     1200         1800        # recipe celesta: 1:4, odd harmonics fading into a pure ring (first-light)",
      "patch {patch:8} 1  262144    0          2253      0            200         1         900      400          1200"
    ]
  },
  "bell": {
    "description": "1:1.4 (Chowning), the index decaying with the ring (first-light)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":12}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         2         6000     1200         3000        # recipe bell: 1:1.4 (Chowning), the index decaying with the ring (first-light)",
      "patch {patch:8} 1  91750     0          5120      0            160         1         4000     600          3000"
    ]
  },
  "tooth": {
    "description": "a steel tooth, 1:3.5, the index falling 30 dB in 250 ms, then a near-pure ring (music-box)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":14}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            160         1         3500     1200         400         # recipe tooth: a steel tooth, 1:3.5, the index falling 30 dB in 250 ms, then a near-pure ring (music-box)",
      "patch {patch:8} 1  229376    0          2662      0            128         1         250      300          400"
    ]
  },
  "star": {
    "description": "1:3, a glassy ping, bright for 180 ms (night-sky)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":18}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         1         1200     1200         600         # recipe star: 1:3, a glassy ping, bright for 180 ms (night-sky)",
      "patch {patch:8} 1  196608    0          1638      0            160         1         180      360          400"
    ]
  },
  "planet-1.4": {
    "description": "a long bell, 1:1.4 (Chowning), the index falling with the ring (night-sky)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":12}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         2         12000    1200         4000        # recipe planet-1.4: a long bell, 1:1.4 (Chowning), the index falling with the ring (night-sky)",
      "patch {patch:8} 1  91750     0          4096      0            128         1         9000     360          4000"
    ]
  },
  "planet-2.76": {
    "description": "a bell, 1:2.76, the second mode of a free bar (night-sky)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":14}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         2         9000     1200         3000        # recipe planet-2.76: a bell, 1:2.76, the second mode of a free bar (night-sky)",
      "patch {patch:8} 1  180879    0          2560      0            128         1         6000     300          3000"
    ]
  },
  "planet-3.17": {
    "description": "a bell, 1:3.17 (night-sky)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":16}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         2         7000     1200         3000        # recipe planet-3.17: a bell, 1:3.17 (night-sky)",
      "patch {patch:8} 1  207749    0          2048      0            128         1         4000     300          3000"
    ]
  },
  "flute": {
    "description": "a soft lead, 1:1, low index, a breath of feedback (waltz)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":10}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            100         45        500      30           200         # recipe flute: a soft lead, 1:1, low index, a breath of feedback (waltz)",
      "patch {patch:8} 1  65536     0          717       358          150         30        600      60           200"
    ]
  },
  "hollow": {
    "description": "clarinet-like: 1:2 gives odd harmonics (canon)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":13}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            100         35        400      20           250         # recipe hollow: clarinet-like: 1:2 gives odd harmonics (canon)",
      "patch {patch:8} 1  131072    0          1331      0            160         25        800      40           250"
    ]
  },
  "harmonica": {
    "description": "1:1, a reedy fed-back modulator; 0 dB sustain on both operators (shuffle)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":12}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         35        100      0            90          # recipe harmonica: 1:1, a reedy fed-back modulator; 0 dB sustain on both operators (shuffle)",
      "patch {patch:8} 1  65536     0          1536      1126         160         20        100      0            90"
    ]
  },
  "glide": {
    "description": "1:2 at a low index; 0 dB sustain on both operators, so a glide is seamless (night-sky's comet)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":24}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            256         250       100      0            2500        # recipe glide: 1:2 at a low index; 0 dB sustain on both operators, so a glide is seamless (night-sky's comet)",
      "patch {patch:8} 1  131072    0          717       0            0           250       100      0            2500"
    ]
  },
  "brass": {
    "description": "1:1, the index rising and falling with the level; velocity opens it. The fundamental cancels near index 1.8, so sustain above 2.4 or below 1.3 (Chowning's trumpet; fanfare)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":18}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         40        200      20           150         # recipe brass: 1:1, the index rising and falling with the level; velocity opens it. The fundamental cancels near index 1.8, so sustain above 2.4 or below 1.3 (Chowning's trumpet; fanfare)",
      "patch {patch:8} 1  65536     0          4096      0            160         50        240      10           150"
    ]
  },
  "oboe": {
    "description": "a double reed: the carrier at 3 times the note, the modulator at 1, harmonics 2 to 4 strongest. The index must rise no slower than the level, or the attack sounds the carrier's pitch (aria)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":16}],
    "rows": [
      "patch {patch:8} 0  196608    {level:0} 0         0            100         60        300      10           120         # recipe oboe: a double reed: the carrier at 3 times the note, the modulator at 1, harmonics 2 to 4 strongest. The index must rise no slower than the level, or the attack sounds the carrier's pitch (aria)",
      "patch {patch:8} 1  65536     0          2048      0            160         40        300      15           120"
    ]
  },
  "bassoon": {
    "description": "the carrier at 4 times the note, the modulator at 1: a nasal band over harmonics 2 to 6, the fundamental weak (aria)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":14}],
    "rows": [
      "patch {patch:8} 0  262144    {level:0} 0         0            100         50        300      10           120         # recipe bassoon: the carrier at 4 times the note, the modulator at 1: a nasal band over harmonics 2 to 6, the fundamental weak (aria)",
      "patch {patch:8} 1  65536     0          2048      0            160         40        300      15           120"
    ]
  },
  "clarinet": {
    "description": "the carrier at 3, the modulator at 2: odd harmonics; the index falls from 4 to 2 as the level rises (Chowning; aria)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":15}],
    "rows": [
      "patch {patch:8} 0  196608    {level:0} 0         0            100         70        200      0            110         # recipe clarinet: the carrier at 3, the modulator at 2: odd harmonics; the index falls from 4 to 2 as the level rises (Chowning; aria)",
      "patch {patch:8} 1  131072    0          4096      0            128         5         150      60           110"
    ]
  },
  "vox": {
    "description": "a sung vowel's body, 1:1; sing vox-formant with it: same times, pitch and velocity (Chowning; aria)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":16}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         90        100      0            260         # recipe vox: a sung vowel's body, 1:1; sing vox-formant with it: same times, pitch and velocity (Chowning; aria)",
      "patch {patch:8} 1  65536     0          1638      0            200         160       100      0            260"
    ]
  },
  "vox-formant": {
    "description": "a sung vowel's formant, 14 dB under vox: the carrier on the harmonic nearest 2.4 kHz, the 4th for D5; move c_ratio with the note (Chowning; aria)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":30}],
    "rows": [
      "patch {patch:8} 0  262144    {level:0} 0         0            128         90        100      0            260         # recipe vox-formant: a sung vowel's formant, 14 dB under vox: the carrier on the harmonic nearest 2.4 kHz, the 4th for D5; move c_ratio with the note (Chowning; aria)",
      "patch {patch:8} 1  65536     0          819       0            200         160       100      0            260"
    ]
  },
  "warm-pad": {
    "description": "1:1, the modulator 0.15 % sharp for a slow shimmer (first-light)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":22}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            96          700       1500     30           1600        # recipe warm-pad: 1:1, the modulator 0.15 % sharp for a slow shimmer (first-light)",
      "patch {patch:8} 1  65634     0          1126      461          64          900       2500     60           1800"
    ]
  },
  "night-pad": {
    "description": "1:1, the index blooming after the level; play detuned pairs (night-sky)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":30}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            0           3000      2500     30           3500        # recipe night-pad: 1:1, the index blooming after the level; play detuned pairs (night-sky)",
      "patch {patch:8} 1  65536     0          922       205          0           4000      3000     60           3500"
    ]
  },
  "drone": {
    "description": "a soft 1:1 tone, its second harmonic 9 dB down (night-sky)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":25}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            0           4000      100      0            5000        # recipe drone: a soft 1:1 tone, its second harmonic 9 dB down (night-sky)",
      "patch {patch:8} 1  65536     0          614       0            0           4000      100      0            5000"
    ]
  },
  "strings": {
    "description": "1:1 with feedback, harmonics falling like a sawtooth's; a slow bow. Play each note on three voices 6 cents apart (aria)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":24}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            96          350       800      15           600         # recipe strings: 1:1 with feedback, harmonics falling like a sawtooth's; a slow bow. Play each note on three voices 6 cents apart (aria)",
      "patch {patch:8} 1  65536     0          1638      819          160         450       900      30           600"
    ]
  },
  "round-bass": {
    "description": "1:1 at a low index (first-light)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":13}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         30        3000     100          900         # recipe round-bass: 1:1 at a low index (first-light)",
      "patch {patch:8} 1  65536     0          717       0            128         10        1500     120          900"
    ]
  },
  "plucked-bass": {
    "description": "1:1 with feedback, the index falling fast (waltz)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":12}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         3         900      90           150         # recipe plucked-bass: 1:1 with feedback, the index falling fast (waltz)",
      "patch {patch:8} 1  65536     0          1843      717          160         2         250      180          150"
    ]
  },
  "motor-bass": {
    "description": "1:1 with feedback, a snappy index (clockwork)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":12}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         2         400      120          90          # recipe motor-bass: 1:1 with feedback, a snappy index (clockwork)",
      "patch {patch:8} 1  65536     0          2458      1024         180         1         120      240          90"
    ]
  },
  "boogie-bass": {
    "description": "1:1, the index falling fast after the pluck (shuffle)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":9}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         4         700      100          70          # recipe boogie-bass: 1:1, the index falling fast after the pluck (shuffle)",
      "patch {patch:8} 1  65536     0          1331      0            160         1         160      160          70"
    ]
  },
  "square": {
    "description": "1:2 fed back, odd harmonics (arcade)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":12}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            0           2         100      0            30          # recipe square: 1:2 fed back, odd harmonics (arcade)",
      "patch {patch:8} 1  131072    0          1331      922          0           1         100      0            30"
    ]
  },
  "saw": {
    "description": "1:1 fed back, saw-like (arcade)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":15}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            0           1         100      0            25          # recipe saw: 1:1 fed back, saw-like (arcade)",
      "patch {patch:8} 1  65536     0          1024      1229         0           1         100      0            25"
    ]
  },
  "triangle": {
    "description": "1:2 at a low index (arcade)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":8}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            0           2         100      0            25          # recipe triangle: 1:2 at a low index (arcade)",
      "patch {patch:8} 1  131072    0          307       0            0           1         100      0            25"
    ]
  },
  "kick": {
    "description": "at 50 Hz, a 1:2 modulator whose index collapses in 60 ms (shuffle)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":8}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         1         320      1200         60          # recipe kick: at 50 Hz, a 1:2 modulator whose index collapses in 60 ms (shuffle)",
      "patch {patch:8} 1  131072    0          5120      0            128         1         60       1200         40"
    ]
  },
  "snare-wires": {
    "description": "at 1.8 kHz; the modulator holds, so the noise stays noise as it decays (shuffle)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":8}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         1         450      1200         60          # recipe snare-wires: at 1.8 kHz; the modulator holds, so the noise stays noise as it decays (shuffle)",
      "patch {patch:8} 1  92406     0          3072      3584         0           1         100      0            60"
    ]
  },
  "snare-body": {
    "description": "at 190 Hz, 1:1.5, a short thud (shuffle)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":8}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            180         1         110      1200         50          # recipe snare-body: at 190 Hz, 1:1.5, a short thud (shuffle)",
      "patch {patch:8} 1  98304     0          1434      0            128         1         50       1200         40"
    ]
  },
  "hat": {
    "description": "a closed hi-hat at 7 kHz, damped after 40 ms (shuffle)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":15}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         1         160      1200         25          # recipe hat: a closed hi-hat at 7 kHz, damped after 40 ms (shuffle)",
      "patch {patch:8} 1  92406     0          2560      3584         0           1         100      0            25"
    ]
  },
  "open-hat": {
    "description": "at 7 kHz, ringing until choked (shuffle)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":16}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         1         1500     1200         40          # recipe open-hat: at 7 kHz, ringing until choked (shuffle)",
      "patch {patch:8} 1  92406     0          2560      3584         0           1         100      0            40"
    ]
  },
  "crash": {
    "description": "at 5 kHz, about 24 dB a second (shuffle)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":12}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         1         5000     1200         700         # recipe crash: at 5 kHz, about 24 dB a second (shuffle)",
      "patch {patch:8} 1  92406     0          3072      3584         0           1         100      0            700"
    ]
  },
  "pin-click": {
    "description": "at 2.6 kHz, a breath of noise (music-box)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":36}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            0           1         20       1200         10          # recipe pin-click: at 2.6 kHz, a breath of noise (music-box)",
      "patch {patch:8} 1  92406     0          2560      3584         0           1         100      0            10"
    ]
  },
  "noise-tail": {
    "description": "noise that follows a pitch curve; the modulator holds (night-sky's comet tail)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":38}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            0           400       100      0            1500        # recipe noise-tail: noise that follows a pitch curve; the modulator holds (night-sky's comet tail)",
      "patch {patch:8} 1  92406     0          3072      3584         0           1         100      0            1500"
    ]
  },
  "tick": {
    "description": "at 5.2 kHz, over in 35 ms (clockwork)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":16}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         1         35       1200         30          # recipe tick: at 5.2 kHz, over in 35 ms (clockwork)",
      "patch {patch:8} 1  92406     0          3072      3584         0           1         40       1200         30"
    ]
  },
  "tock": {
    "description": "at 1.3 kHz, the tick lower and longer (clockwork)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":14}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         1         70       1200         50          # recipe tock: at 1.3 kHz, the tick lower and longer (clockwork)",
      "patch {patch:8} 1  92406     0          2560      3072         0           1         80       1200         50"
    ]
  },
  "wood-drum": {
    "description": "80:55 (Chowning), the index falling from 25 rad within a few ms (fiesta)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":14}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            160         1         450      1200         80          # recipe wood-drum: 80:55 (Chowning), the index falling from 25 rad within a few ms (fiesta)",
      "patch {patch:8} 1  45056     0          25600     0            96          1         30       1200         20"
    ]
  },
  "tom": {
    "description": "1:2 at a low index; 0 dB sustain, so a curve can sweep it, and the release is the decay (fiesta)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":12}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         1         100      0            650         # recipe tom: 1:2 at a low index; 0 dB sustain, so a curve can sweep it, and the release is the decay (fiesta)",
      "patch {patch:8} 1  131072    0          819       0            128         1         100      0            400"
    ]
  },
  "timpani": {
    "description": "1:1.5, an octave-low thump in the strike that fades into the tone; retrigger it for a roll (fanfare)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":12}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            220         2         3500     1200         500         # recipe timpani: 1:1.5, an octave-low thump in the strike that fades into the tone; retrigger it for a roll (fanfare)",
      "patch {patch:8} 1  98304     0          1229      0            160         1         300      180          400"
    ]
  },
  "clap": {
    "description": "at 1.2 kHz, short bursts 10 ms apart, then a held one for the tail (fiesta)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":10}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         1         450      1200         6           # recipe clap: at 1.2 kHz, short bursts 10 ms apart, then a held one for the tail (fiesta)",
      "patch {patch:8} 1  92406     0          2867      3584         0           1         100      0            40"
    ]
  },
  "cowbell": {
    "description": "the square wave, struck; play two voices together at 540 and 800 Hz (fiesta)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":16}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            160         1         600      1200         50          # recipe cowbell: the square wave, struck; play two voices together at 540 and 800 Hz (fiesta)",
      "patch {patch:8} 1  131072    0          1331      922          100         1         180      90           50"
    ]
  },
  "ep-4op": {
    "description": "ep-body and ep-tine in one patch of four operators, two pairs side by side (carriers 0 and 2), one voice per key",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":14},{"operator":2,"tweak":"o2_level","attenuationDb":24}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         2         8000     500          450         # recipe ep-4op: ep-body and ep-tine in one patch of four operators, two pairs side by side (carriers 0 and 2), one voice per key",
      "patch {patch:8} 1  65536     0          1126      0            230         1         1400     140          450",
      "patch {patch:8} 2  65536     {level:2} 0         0            256         1         1400     1200         300",
      "patch {patch:8} 3  917504    0          1536      0            220         1         600      1200         200",
      "route {patch} 4 5 1>0:256 3>2:256"
    ]
  },
  "sub-bass": {
    "description": "two carriers and no edge: the note, its feedback rounding it towards a saw, over a sine an octave down; for notes from A1 up",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":14},{"operator":1,"tweak":"m_level","attenuationDb":17}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         819          128         3         1200     80           160         # recipe sub-bass: two carriers and no edge: the note, its feedback rounding it towards a saw, over a sine an octave down; for notes from A1 up",
      "patch {patch:8} 1  32768     {level:1} 0         0            96          6         2500     60           200",
      "route {patch} 2 3"
    ]
  },
  "snare-3op": {
    "description": "at 190 Hz: snare-body's thud, operator 2 on the carrier, and the wires from a noise carrier (wave 4) stepping at twice 32 times the note, about 12 kHz, in one voice suitable for one-note audition",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":10},{"operator":1,"tweak":"m_level","attenuationDb":12}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            180         1         110      1200         50          # recipe snare-3op: at 190 Hz: snare-body's thud, operator 2 on the carrier, and the wires from a noise carrier (wave 4) stepping at twice 32 times the note, about 12 kHz, in one voice suitable for one-note audition",
      "patch {patch:8} 1  2097152   {level:1} 0         0            200         1         300      1200         60         0                0             4",
      "patch {patch:8} 2  98304     0          1434      0            128         1         50       1200         40",
      "route {patch} 3 3 2>0:256"
    ]
  },
  "hat-fixed": {
    "description": "hat with both operators fixed, the carrier at 7 kHz and the modulator 1.41 times that, whatever the note, so in a keys version the note only places it; the modulator's phase runs on across hits from silence (free; its feedback still starts clear), so the hits need not start alike (manifold)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":15}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         1         160      1200         25         0                0             0    0          7000000  # recipe hat-fixed: hat with both operators fixed, the carrier at 7 kHz and the modulator 1.41 times that, whatever the note, so in a keys version the note only places it; the modulator's phase runs on across hits from silence (free; its feedback still starts clear), so the hits need not start alike (manifold)",
      "patch {patch:8} 1  92406     0          2560      3584         0           1         100      0            25         0                0             0    0          7000000       0         1"
    ]
  },
  "clav-rs": {
    "description": "a 1:3 clavinet-like pluck whose envelopes shorten up the keyboard: rate scaling 3 on both operators, so every time is as written at A0, about 0.62 of it at G2, 0.48 at G3 and 0.37 at G4 (manifold)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":16}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            200         1         1600     1200         90         0                0             0    3  # recipe clav-rs: a 1:3 clavinet-like pluck whose envelopes shorten up the keyboard: rate scaling 3 on both operators, so every time is as written at A0, about 0.62 of it at G2, 0.48 at G3 and 0.37 at G4 (manifold)",
      "patch {patch:8} 1  196608    0          1843      0            230         1         400      120          90         0                0             0    3"
    ]
  },
  "kick-drop": {
    "description": "at 50 Hz, a sine falling one octave in 90 ms; no noise source (drum-study)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":14}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         1         230      1200         45          # recipe kick-drop: at 50 Hz, a sine falling one octave in 90 ms; no noise source (drum-study)",
      "patch {patch:8} 1  65536     0          0         0            0           1         1        1200         1",
      "peg {patch} 1200 0 0 90 0 0 -1200 40 1"
    ]
  },
  "snare-white": {
    "description": "at 185 Hz, a short sine body plus white noise clocked at 14 kHz (drum-study)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":19},{"operator":1,"tweak":"m_level","attenuationDb":20}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            160         1         85       1200         35          # recipe snare-white: at 185 Hz, a short sine body plus white noise clocked at 14 kHz (drum-study)",
      "patch {patch:8} 1  65536     {level:1} 0         0            160         1         190      1200         35         0                0             4    0          7000000",
      "route {patch} 2 3"
    ]
  },
  "clap-white": {
    "description": "three short white-noise strikes 14 ms apart, then a longer fourth strike; repeated strikes are timeline events, not a patch sequencer (drum-study)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":19}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            128         1         140      1200         4          0                0             4    0          8000000  # recipe clap-white: three short white-noise strikes 14 ms apart, then a longer fourth strike; repeated strikes are timeline events, not a patch sequencer (drum-study)",
      "patch {patch:8} 1  65536     0          0         0            0           1         1        1200         1"
    ]
  },
  "hat-ring": {
    "description": "white noise through two ring edges from fixed inharmonic sines; the two modulating envelopes also close the strike (drum-study)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":17}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            160         1         180      1200         25         0                0             4    0          9000000  # recipe hat-ring: white noise through two ring edges from fixed inharmonic sines; the two modulating envelopes also close the strike (drum-study)",
      "patch {patch:8} 1  65536     0          0         0            0           1         250      0            25         0                0             0    0          3431000",
      "patch {patch:8} 2  65536     0          0         0            0           1         250      0            25         0                0             0    0          5711000",
      "route {patch} 3 1 1>0:256:ring 2>0:256:ring"
    ]
  },
  "warm-pad-x": {
    "description": "warm-pad with wheel vibrato, pressure brightness and slide brightness (regression EXPR)",
    "carriers": [{"operator":0,"tweak":"c_level","attenuationDb":22}],
    "rows": [
      "patch {patch:8} 0  65536     {level:0} 0         0            96          700       1500     30           1600        # recipe warm-pad-x: warm-pad with wheel vibrato, pressure brightness and slide brightness (regression EXPR)",
      "patch {patch:8} 1  65634     0          1126      461          64          900       2500     60           1800",
      "lfo {patch} 0 5000 0 0 0 0 0",
      "mod {patch} 0 ctl0 lfo_pitch 50 3",
      "mod {patch} 1 ctl1 index1 1024 3",
      "mod {patch} 2 ctl4 index1 2048 3"
    ]
  }
};
