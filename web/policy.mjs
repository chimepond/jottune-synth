// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Browser admission consumes native inspection results, never TL statements.
export const limits = Object.freeze({recordBytes: 1000000, durationSeconds: 300,
  outputBytes: 16777216, renderSeconds: 120, drawingEvents: 20000,
  wasmMemoryBytes: 268435456, fragmentCharacters: 12 * 1024 * 1024});

export function options(value = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('Render options must be an object.');
  const mode = value.mode ?? 'verify';
  if (!['listen', 'verify'].includes(mode)) throw Error('Mode must be listen or verify.');
  const release = value.release;
  if (release !== undefined && (typeof release !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(release)))
    throw Error('Invalid renderer release identifier.');
  const valueProfile = value.profile;
  if (valueProfile === undefined) return {mode, release};
  if (!valueProfile || typeof valueProfile !== 'object' || Array.isArray(valueProfile) ||
      Object.keys(valueProfile).some(key => !['halfWidth', 'mirror', 'levelPolicy'].includes(key)) ||
      ![30, 45, 60, 75, 90].includes(valueProfile.halfWidth) || typeof valueProfile.mirror !== 'boolean')
    throw Error('Keys profile requires halfWidth 30, 45, 60, 75 or 90 degrees and a boolean mirror setting.');
  const levelPolicy = valueProfile.levelPolicy ?? 'strict';
  if (!['strict', 'carrier-bound'].includes(levelPolicy)) throw Error('Unknown Keys level policy.');
  return {mode, release, profile:{halfWidth:valueProfile.halfWidth, mirror:valueProfile.mirror, levelPolicy}};
}

export function selectEngine(registry, release) {
  const requested = release ?? registry.default;
  const matches = (registry.engines || []).filter(engine => engine.id === requested || engine.release_id === requested);
  if (matches.length !== 1) throw Error('Renderer build not available: ' + String(requested));
  const engine = matches[0];
  if (engine.withdrawn || engine.withdrawn_at) throw Error('Renderer build has been withdrawn: ' + String(requested));
  return engine;
}

export function checkRecord(text) {
  if (typeof text !== 'string') throw Error('A TL record must be text.');
  if (new TextEncoder().encode(text).length > limits.recordBytes)
    throw Error('Record exceeds the 1 MB browser input limit. Use the local CLI for larger records.');
}

export function checkInspection(info, mode = 'verify') {
  if (BigInt(info.length_ns) > BigInt(limits.durationSeconds) * 1000000000n)
    throw Error('Record exceeds the 300-second browser profile. Use the local CLI for longer records.');
  const lower = BigInt(mode === 'listen' ? info.once_render_bytes_lower_bound ?? info.render_bytes_lower_bound : info.render_bytes_lower_bound);
  const pcmBytes = BigInt(info.pcm_bytes);
  if (lower > 192n * 1024n * 1024n || lower + pcmBytes * 3n > BigInt(limits.wasmMemoryBytes))
    throw Error('Too large for this device: estimated rendering and playback storage exceed this player’s budget.');
}

export function verification(result) {
  const matched = result.verdict === 'valid' && result.play?.match === 'yes';
  const verified = matched && result.mode !== 'listen' && result.play?.block_indep === 'yes';
  return {verified, verification: verified ? 'verified' : matched ? 'checksum-matched' : 'unverified'};
}
