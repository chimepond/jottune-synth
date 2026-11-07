// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import fs from 'node:fs';
import readline from 'node:readline';
import {runCLI} from '../web/runtime.mjs';
const module = await WebAssembly.compile(fs.readFileSync(process.argv[2]));
for await (const line of readline.createInterface({input:process.stdin})) {
  try { const request=JSON.parse(line);const result=await runCLI(module,request.args,request.files);console.log(JSON.stringify({code:result.code,stdout:result.stdout,stderr:result.stderr})); }
  catch(error){console.log(JSON.stringify({code:-999,stdout:'',stderr:String(error)}));}
  globalThis.gc?.();
}
