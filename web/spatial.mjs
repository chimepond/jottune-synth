// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Native projection and native coordinate sampling. No TL grammar or movement
// interpolation lives here; an unsupported companion never blocks playback.
import {runCLI} from './runtime.mjs';

const LIMIT = 20000, MAX_COORD = 16777216;
const integer = (x, low, high) => Number.isSafeInteger(x) && x >= low && x <= high;
const fail = () => { throw Error('Spatial view metadata is invalid or exceeds its display limits.'); };
const hex = bytes => [...new Uint8Array(bytes)].map(x => x.toString(16).padStart(2, '0')).join('');

export function validateSpatial(value, inspection) {
  if (value === null) {
    if (inspection && Number(inspection.channels) !== 1) fail();
    return null;
  }
  if (!value || value.format !== 1 || ![32,64,128].includes(value.block) ||
      !integer(value.near,1,MAX_COORD) || !integer(value.frames,1,0xffffffff) ||
      !integer(value.voices,1,64) || !integer(value.initialYaw,-0x80000000,0x7fffffff) ||
      !integer(value.fs,1,0xffffffff) || !integer(value.channels,2,16) || !integer(value.route,0,4)) fail();
  if (inspection && ['frames','voices','fs','channels','route'].some(key=>value[key]!==Number(inspection[key])))
    throw Error('Spatial view metadata differs from the selected renderer inspection.');
  const {layout,paths,states,yaws} = value;
  if (!layout || !integer(layout.bytes,8,1024) ||
      !integer(layout.b0,0,layout.bytes-4) || !integer(layout.b1,0,layout.bytes-4) ||
      layout.b0 % 4 || layout.b1 % 4 || layout.b0 === layout.b1 ||
      !Array.isArray(paths) || paths.length > LIMIT ||
      !Array.isArray(states) || states.length > LIMIT+64 ||
      !Array.isArray(yaws) || yaws.length > LIMIT) fail();
  const frameMax = Math.ceil(value.frames/value.block)*value.block;
  const lastPath = Array(value.voices).fill(-1), lastState = [...lastPath];
  for (const p of paths) {
    if (!p || !integer(p.voice,0,value.voices-1) || !integer(p.sample,0,frameMax) ||
        p.sample % value.block || ![0,1].includes(p.pen) ||
        ![p.x,p.y,p.z].every(x => integer(x,-MAX_COORD,MAX_COORD)) ||
        p.sample < lastPath[p.voice]) fail();
    lastPath[p.voice] = p.sample;
  }
  for (const s of states) {
    if (!Array.isArray(s) || s.length !== 10 || !integer(s[0],0,value.voices-1) ||
        !integer(s[1],0,frameMax) || s[1] % value.block ||
        !s.slice(2,8).every(x => integer(x,-MAX_COORD,MAX_COORD)) ||
        !s.slice(8).every(x => integer(x,0,0xffffffff)) || s[1] < lastState[s[0]]) fail();
    if (lastState[s[0]] < 0 && s[1] !== 0) fail();
    lastState[s[0]] = s[1];
  }
  if (lastState.some(x => x < 0)) fail();
  let previous = -1;
  for (const y of yaws) {
    if (!y || !integer(y.sample,0,frameMax) || y.sample % value.block ||
        y.sample < previous || !integer(y.yaw,-0x80000000,0x7fffffff)) fail();
    previous = y.sample;
  }
  return value;
}

// Calls receive trusted installed URLs from worker.mjs; the manifest associates
// a display helper with exact parser/library artifacts, not an engine label.
export async function inspectSpatial(engine, recordText, libraryURL, fetcher = fetch, timeoutMs = 3000) {
  const controller = new AbortController();
  let timer;
  const timeout = new Promise((_,reject)=>{
    timer = setTimeout(()=>{
      controller.abort(); reject(Error('Native spatial view is unavailable: setup timed out.'));
    }, timeoutMs);
  });
  try { return await Promise.race([projectSpatial(engine,recordText,libraryURL,fetcher,controller.signal),timeout]); }
  finally { clearTimeout(timer); }
}

async function projectSpatial(engine, recordText, libraryURL, fetcher, signal) {
  const get = async path => {
    if (signal.aborted) throw Error('Native spatial view is unavailable: setup timed out.');
    const response = await fetcher(path,{signal});
    if (!response.ok) throw Error('Native spatial view is unavailable for this renderer.');
    return response;
  };
  const manifestResponse = await get(new URL('./spatial-view.json',import.meta.url));
  const manifestText = await manifestResponse.text();
  if (manifestText.length > 4096) throw Error('Spatial companion manifest is too large.');
  const manifest = JSON.parse(manifestText);
  if (manifest.format !== 1 || !/^[0-9a-f]{64}$/.test(manifest.wasm_sha256 || '') ||
      manifest.browser_sha256 !== engine.browser_sha256 ||
      !/^[0-9a-f]{64}$/.test(engine.library_sha256 || '') ||
      manifest.library_sha256 !== engine.library_sha256)
    throw Error('Native spatial view is unavailable for this renderer version.');
  const helperBytes = await (await get(new URL('./spatial-view.wasm',import.meta.url))).arrayBuffer();
  if (helperBytes.byteLength > 4*1024*1024 ||
      hex(await crypto.subtle.digest('SHA-256',helperBytes)) !== manifest.wasm_sha256)
    throw Error('Spatial companion checksum mismatch.');
  const projected = await runCLI(await WebAssembly.compile(helperBytes), ['record.tl'], {'record.tl':recordText});
  if (projected.code) throw Error(projected.stderr.trim() || 'Native spatial projection is unavailable.');
  const spatial = validateSpatial(JSON.parse(projected.stdout));
  if (!spatial) return {spatial:null, spatialModule:null, spatialError:null};
  const libraryBytes = await (await get(libraryURL)).arrayBuffer();
  if (libraryBytes.byteLength > 4*1024*1024 ||
      hex(await crypto.subtle.digest('SHA-256',libraryBytes)) !== engine.library_sha256)
    throw Error('Spatial library checksum mismatch.');
  return {spatial, spatialModule:await WebAssembly.compile(libraryBytes), spatialError:null};
}

function latest(rows, frame, time) {
  let lo = 0, hi = rows.length;
  while (lo < hi) { const mid = (lo+hi)>>>1; if (time(rows[mid]) <= frame) lo = mid+1; else hi = mid; }
  return lo ? rows[lo-1] : null;
}

export function createSpatialSampler(metadata, module) {
  const data = validateSpatial(metadata);
  if (!data) return null;
  const native = new WebAssembly.Instance(module,{}).exports;
  if (!(native.memory instanceof WebAssembly.Memory) || typeof native.sp_coord_at !== 'function' || !native.__heap_base)
    throw Error('Native spatial sampler exports are unavailable.');
  const pointer = Number(native.__heap_base.value);
  if (!integer(pointer,0,268435456-data.layout.bytes)) fail();
  const needed = pointer+data.layout.bytes-native.memory.buffer.byteLength;
  if (needed > 0) native.memory.grow(Math.ceil(needed/65536));
  const memory = new DataView(native.memory.buffer);
  const byVoice = Array.from({length:data.voices},()=>[]);
  for (const s of data.states) byVoice[s[0]].push(s.slice());
  const frameAt = frame => Number.isFinite(frame) ? Math.max(0,Math.min(data.frames-1,Math.floor(frame))) : 0;
  return {
    positionsAt(frame) {
      const sample = frameAt(frame), block = Math.floor(sample/data.block);
      return byVoice.map(rows => {
        const state = latest(rows,sample,row=>row[1]);
        memory.setUint32(pointer+data.layout.b0,state[8],true);
        memory.setUint32(pointer+data.layout.b1,state[9],true);
        return {x:native.sp_coord_at(pointer,block,state[5],state[2]),
          y:native.sp_coord_at(pointer,block,state[6],state[3]),
          z:native.sp_coord_at(pointer,block,state[7],state[4])};
      });
    },
    yawAt(frame) { return latest(data.yaws,frameAt(frame),row=>row.sample)?.yaw ?? data.initialYaw; },
  };
}
