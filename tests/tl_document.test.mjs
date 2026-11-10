// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {parseDocument,composeDocument,preserveDocument,bodySha256,documentSha256,validateMetadata} from '../web/tl-document.mjs';
const body='timeline draft2\nengine example\nhash 0123456789abcdef\n';
const wrap=json=>'# jottune-tl 1 1\n# '+json+'\n# end-jottune-tl\n'+body;

test('metadata edits preserve musical identity while complete document checksums change',async()=>{
  const a=composeDocument(body,{title:'Wind Rose',model:{id:'reported/v1',source:'client-reported'}});
  const b=composeDocument(body,{title:'Corrected title',model:{id:'reported/v2',source:'user-stated'}});
  assert.equal(await bodySha256(a),await bodySha256(b));assert.equal(await bodySha256(a),await documentSha256(body));
  assert.notEqual(await documentSha256(a),await documentSha256(b));
  assert.equal(parseDocument(a).body,body);assert.equal(preserveDocument(a,new TextEncoder().encode(body)),a);
  assert.equal(parseDocument(new TextEncoder().encode(a).buffer).metadata.title,'Wind Rose');
});
test('both uint32 requirements reject newer, malformed and overflow versions',()=>{
  for(const pair of ['2 1','1 2','4294967295 1','1 4294967295','4294967296 1','0 1','01 1','-1 1','1.0 1','1e0 1'])
    assert.throws(()=>parseDocument(wrap('{}').replace('1 1',pair)),/version|header/i,pair);
  assert.deepEqual([parseDocument(body).tlVersion,parseDocument(body).engineVersion],[1,1]);
});
test('reserved framing, duplicate keys and schema spoofing cannot become ordinary comments',()=>{
  for(const value of [wrap('{"title":"a","title":"b"}'),wrap('{"title":"a","\\u0074itle":"b"}'),
    wrap('{"model":{"id":"a","id":"b","source":"user-stated"}}'),wrap('{"__proto__":{}}'),wrap('{"consent":true}'),
    body+wrap('{}'), '# jottune-tlx 1 2\n'+body, body+'# end-jottune-tlx\n', '\r# jottune-tl 1 2\n'+body,wrap('{}').replace('# end-jottune-tl\n',''),wrap('{}').replace('# jottune-tl','#jottune-tl'),
    wrap('{}').replace('# {}','{}'),wrap('{}').replace('# end-jottune-tl','# jottune-tl 1 1')])assert.throws(()=>parseDocument(value));
});
test('closed fields have finite numeric, Unicode, date and size boundaries',()=>{
  const good={title:'音楽 🎵',author:'Composer',collection:'Examples',made_by:'mixed',genre:'experimental',
    tempo:{min_bpm:85.5,max_bpm:120},model:null,utc_day:'2026-11-10',license:'Apache-2.0',renderer_release:'2026-11-10-abcdef0',prompt_id:'benchmark/1'};
  assert.deepEqual(parseDocument(composeDocument(body,good)).metadata,good);
  for(const metadata of [{title:'x'.repeat(61)},{title:' x'},{title:'e\u0301'},{title:'\ud800'},{title:'x\ny'},
    {model:{id:'x',source:'guessed'}},{model:[]},{tempo:{min_bpm:1,max_bpm:Infinity}},{tempo:{min_bpm:2,max_bpm:1}},
    {utc_day:'2026-02-30'},{license:'x'.repeat(161)},{renderer_release:'x'.repeat(129)},{made_by:'unknown'},{prompt:'free text'}])
    assert.throws(()=>validateMetadata(metadata));
  assert.throws(()=>parseDocument('\uFEFF'+body));assert.throws(()=>parseDocument(body+'\0'));assert.throws(()=>parseDocument('x'.repeat(1000001)));
  assert.throws(()=>parseDocument(body+'# '+'x'.repeat(1022)+'\n'));
  assert.equal(parseDocument(wrap('{}').replaceAll('\n','\r\n')).metadata.constructor,Object);
});
test('Python and JavaScript agree on portable metadata acceptance and musical body bytes',async()=>{
  const valid=[{}, {title:'Wind Rose',made_by:'agent',genre:null,tempo:null,model:null},
    {title:'音楽 🎵',tempo:{min_bpm:90,max_bpm:120},model:{id:'m/v1',source:'client-reported'}}];
  const documents=valid.map(meta=>composeDocument(body,meta));
  documents.push('', '# jottune-tl 1 1\n# {}\n# end-jottune-tl\n', '# jottune-tlx 1 2\n'+body, wrap('{}').replace('1 1','1 2'),wrap('{"title":"a","title":"b"}'),wrap('{"title":"'+ 'x'.repeat(61)+'"}'));
  const script='import json,sys;sys.path.insert(0,sys.argv[1]);import tl_document as t\nfor s in json.load(sys.stdin):\n try:\n  p=t.parse_document(s.encode());print(json.dumps({"ok":True,"body":p["body"].decode(),"metadata":p["metadata"]},ensure_ascii=False))\n except ValueError:print(json.dumps({"ok":False}))';
  const answer=spawnSync('python3',['-I','-B','-c',script,new URL('../tools',import.meta.url).pathname],{input:JSON.stringify(documents),encoding:'utf8'});
  assert.equal(answer.status,0,answer.stderr);const outcomes=answer.stdout.trim().split('\n').map(JSON.parse);
  for(let i=0;i<documents.length;i++){let parsed;try{parsed=parseDocument(documents[i]);}catch{}
    assert.equal(outcomes[i].ok,!!parsed);if(parsed){assert.equal(outcomes[i].body,parsed.body);assert.deepEqual(outcomes[i].metadata,parsed.metadata);}}
});
