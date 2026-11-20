#!/usr/bin/env node
// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import fs from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {buildBundle} from '../web/agent.mjs';

export async function main(argv = process.argv.slice(2)) {
  if (!argv.length || argv.includes('--help')) {
    process.stdout.write('Build a deployment-neutral offline agent bundle.\n' +
      '  node tools/agent_bundle.mjs --source STAGED_PLAYER_DIR --out NEW_DIR [--release ID]\n');
    return 0;
  }
  const options = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i].slice(2);
    if (!argv[i].startsWith('--') || !['source','out','release'].includes(key) ||
        Object.hasOwn(options,key) || !argv[i + 1] || argv[i + 1].startsWith('--')) throw Error('Use --help for bundle syntax.');
    options[key] = argv[i + 1];
  }
  if (!options.source || !options.out) throw Error('Choose --source and a new --out directory.');
  process.stdout.write(JSON.stringify(await buildBundle(options),null,2) + '\n');
  return 0;
}

if (process.argv[1] && await fs.realpath(process.argv[1]).catch(() => null) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = await main(); }
  catch (error) { process.stderr.write('Agent bundle: ' + error.message + '\n'); process.exitCode = 2; }
}
