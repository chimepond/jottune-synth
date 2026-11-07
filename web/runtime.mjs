// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import { WASI, File, OpenFile, PreopenDirectory, ConsoleStdout } from './vendor/browser_wasi_shim/index.js';
const encoder = new TextEncoder();
const decoder = new TextDecoder();
function join(chunks) {
  const out = new Uint8Array(chunks.reduce((n, part) => n + part.length, 0));
  let offset = 0;
  for (const chunk of chunks) { out.set(chunk, offset); offset += chunk.length; }
  return out;
}
export async function runCLI(module, args, files) {
  const stdout = [], stderr = [];
  let outputBytes = 0;
  const collect = (target, data) => {
    outputBytes += data.length;
    if (outputBytes > 16 * 1024 * 1024) throw Error("Engine output exceeds the 16 MiB browser limit.");
    target.push(data.slice());
  };
  const entries = new Map(Object.entries(files).map(([name, value]) =>
    [name, new File(typeof value === 'string' ? encoder.encode(value) : value)]));
  entries.set('canonical.tl', new File(new Uint8Array()));
  const wasi = new WASI(['harness', ...args], [], [new OpenFile(new File(new Uint8Array())),
    new ConsoleStdout(data => collect(stdout, data)),
    new ConsoleStdout(data => collect(stderr, data)), new PreopenDirectory('.', entries)], { debug: false });
  const instance = await WebAssembly.instantiate(module, { wasi_snapshot_preview1: wasi.wasiImport });
  const code = wasi.start(instance);
  const api = instance.exports;
  let pcm = null;
  if (api.fm_pcm_samples && api.fm_pcm_samples()) {
    pcm = new Int16Array(api.memory.buffer, api.fm_pcm_ptr(), api.fm_pcm_samples()).slice();
  }
  return { code, stdout: decoder.decode(join(stdout)), stderr: decoder.decode(join(stderr)),
    canonical: entries.get('canonical.tl').data.slice(), pcm,
    sampleRate: api.fm_pcm_rate?.() || 0, channels: api.fm_pcm_channels?.() || 0 };
}
// These are host result rows, not timeline syntax. All integers stay decimal strings.
export function row(line) {
  return Object.fromEntries(line.trim().split(/\s+/).slice(1).map(item => {
    const index = item.indexOf('='); return [item.slice(0, index), item.slice(index + 1)];
  }));
}
export function verdict(result) {
  if (/engine .*is not this build/.test(result.stderr)) return 'mismatch: engine build not available';
  if (result.code === 3 || /match=NO/.test(result.stdout)) return 'mismatch';
  return result.code === 0 ? 'valid' : 'refused';
}
