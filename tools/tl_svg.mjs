#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Native validation/events and the shared browser drawing; no TL parser here.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {toSvg} from '../web/media.mjs';
import {row} from '../web/runtime.mjs';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const args = process.argv.slice(2);
let harness = process.env.JOTTUNE_HARNESS || path.join(root, 'build/s12e10p1v1/harness');
const option = args.indexOf('--harness');
if (option !== -1) { harness = args[option + 1]; args.splice(option, 2); }
try {
  if (args.length !== 2 || !harness) throw Error('Usage: node tools/tl_svg.mjs INPUT.tl OUTPUT.svg [--harness PATH]');
  const input = path.resolve(args[0]), output = path.resolve(args[1]);
  function native(mode) {
    const result = spawnSync(harness, [mode, '--', input], {encoding:'utf8', maxBuffer:16*1024*1024});
    if (result.error) throw result.error;
    if (result.status !== 0) throw Error(result.stderr.trim() || 'Native validation failed: ' + mode);
    return result.stdout.split('\n');
  }
  native('--play');
  const listing = native('--events');
  const info = listing.find(line => line.startsWith('INSPECT '));
  if (!info) throw Error('Native inspection returned no INSPECT row.');
  const events = listing.filter(line => /^(EVENT|GEN_EVENT) /.test(line)).map(row);
  fs.writeFileSync(output, toSvg({info:row(info), events}), {flag:'wx'});
  console.log(output);
} catch (error) { console.error('JotTune Timeline: ' + error.message); process.exitCode = 1; }
