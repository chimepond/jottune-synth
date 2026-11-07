// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import {WASI, File, OpenFile, PreopenDirectory, ConsoleStdout} from './vendor/browser_wasi_shim/index.js';
import {createDryAnalyzer, analysisLimits} from './analysis.mjs';
import {checkInspection, checkRecord} from './policy.mjs';
const decode = new TextDecoder();
const row = line => Object.fromEntries((line || '').trim().split(/\s+/).slice(1).map(word => {
  const at = word.indexOf('='); return [word.slice(0, at), word.slice(at + 1)];
}));

// The caller verifies analysis-view.json/analysis-view.wasm against the selected
// engine before supplying this module. Run inside a killable worker: a JS timer
// cannot interrupt the synchronous native verification preceding its writes.
export async function analyzeDryVoices({module, recordText, info, events, pcmHash}, options = {}) {
  const scope = 'voice_out_pre_fx';
  try {
    checkRecord(recordText); checkInspection(info, 'verify');
    if (!(module instanceof WebAssembly.Module)) throw Error('Compatible source-analysis companion is unavailable.');
    if (!/^[a-f0-9]{16}$/.test(pcmHash || '')) throw Error('A completed kept-render PCM hash is required.');
    const started = performance.now(), maxMilliseconds = options.maxMilliseconds ?? 10000;
    if (!Number.isFinite(maxMilliseconds) || maxMilliseconds < 1 || maxMilliseconds > 300000)
      throw Error('Dry analysis deadline must be 1 to 300000 milliseconds.');
    const budget = options.maxBytes ?? analysisLimits.defaultDryBytes;
    const check = () => {
      if (performance.now() - started > maxMilliseconds) throw Error('Dry analysis exceeded its time budget.');
      return options.signal?.aborted || options.shouldCancel?.();
    };
    const analyzer = createDryAnalyzer(info, events, {...options, maxBytes:budget, shouldCancel:check});
    const output = {stdout:[], stderr:[]}; let textBytes = 0, dryOpened = false;
    const collect = key => data => {
      if (check()) throw Error('Dry analysis cancelled.');
      textBytes += data.length; if (textBytes > 1024 * 1024) throw Error('Analysis diagnostics exceed 1 MiB.');
      output[key].push(data.slice());
    };
    const entries = new Map([['record.tl', new File(new TextEncoder().encode(recordText), {readonly:true})]]);
    const directory = new PreopenDirectory('.', entries), open = directory.path_open.bind(directory);
    directory.path_open = (...args) => {
      const opened = open(...args);
      if (args[1] === 'dry.wav.tmp' && opened.fd_obj) {
        if (dryOpened) throw Error('Analysis attempted a second dry output.'); dryOpened = true;
        let written = 0;
        const fd = opened.fd_obj;
        // Keep the native temporary-file/rename path while retaining no audio
        // in its virtual inode. Only sequential writes are accepted.
        fd.fd_write = bytes => {
          if (fd.file_pos !== BigInt(written)) throw Error('Dry output is not append-only.');
          analyzer.feed(bytes); written += bytes.length; fd.file_pos = BigInt(written);
          return {ret:0, nwritten:bytes.length};
        };
        fd.fd_pwrite = () => {throw Error('Dry output cannot use positioned writes.');};
        fd.fd_allocate = () => {throw Error('Dry output cannot allocate a complete audio file.');};
        fd.fd_filestat_set_size = () => {throw Error('Dry output cannot resize a complete audio file.');};
      }
      return opened;
    };
    const wasi = new WASI(['analysis', 'record.tl', String(budget)], [], [new OpenFile(new File([])),
      new ConsoleStdout(collect('stdout')), new ConsoleStdout(collect('stderr')), directory], {debug:false});
    const instance = await WebAssembly.instantiate(module, {wasi_snapshot_preview1:wasi.wasiImport});
    if (check()) throw Error('Dry analysis cancelled.');
    const code = wasi.start(instance);
    const text = key => output[key].map(part => decode.decode(part)).join('');
    const stdout = text('stdout'), stderr = text('stderr');
    if (code) throw Error(stderr.trim() || `Source-analysis companion exited ${code}.`);
    if (check()) throw Error('Dry analysis cancelled.');
    const plays = stdout.split('\n').filter(line => line.startsWith('PLAY '));
    const stems = stdout.split('\n').filter(line => line.startsWith('STEMS '));
    if (plays.length !== 1 || stems.length !== 1 || !dryOpened) throw Error('Native analysis protocol is incomplete.');
    const play = row(plays[0]);
    if (play.hash !== pcmHash || play.block_indep !== 'yes' || !['yes','-'].includes(play.match) ||
        play.fs !== String(info.fs) || play.samples !== String(info.frames) || play.voices !== String(info.voices) ||
        Number(play.channels ?? 1) !== Number(info.channels))
      throw Error('Analysis full-mix verification differs from the kept renderer.');
    const result = analyzer.finish(row(stems[0]));
    const buses = stdout.split('\n').filter(line => line.startsWith('ANALYSIS_BUS ')).map(row);
    if (buses.length !== 4 || new Set(buses.map(bus => bus.bus)).size !== 4 ||
        buses.some(bus => !['chorus','delay','reverb','flanger'].includes(bus.bus) ||
          !['0','1'].includes(bus.configured) || !/^\d+$/.test(bus.nonzero_send_events)))
      throw Error('Native effect-send diagnostics are incomplete.');
    return {...result, verified_mix_pcm_hash:play.hash, elapsed_milliseconds:performance.now() - started,
      sends:{status:'available', scope:'native_configuration_and_events', buses},
      warnings:buses.filter(bus => bus.configured === '1' && bus.nonzero_send_events === '0').map(bus =>
        ({code:'effect_without_sends', bus:bus.bus, message:'This audible effect bus is declared but has no nonzero native send events.'})),
      limits:{bytes:budget, milliseconds:maxMilliseconds}, stored_audio_bytes:0};
  } catch (error) {
    return {status:'unavailable', scope, reason:error.message,
      limits:{bytes:options.maxBytes ?? analysisLimits.defaultDryBytes, milliseconds:options.maxMilliseconds ?? 10000}};
  }
}
