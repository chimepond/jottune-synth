// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Differential contract checks against the maintained Python score compiler.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {compileScore, selectScoreStreams, pitchMillihz, recipe, recipeGain, listRecipes, ScoreError} from '../web/authoring.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourceRoot = process.env.JOTTUNE_SOURCE_ROOT || root;
const harness = process.env.JOTTUNE_HARNESS || path.join(sourceRoot, 'build/s12e10p1v1/harness');
const engine = fs.readFileSync(path.join(sourceRoot, 'web/reference/params.txt'), 'utf8').split('\n')[0].split(' engine=')[1];
const declarations = 'space stereo\nblock 64\nnear 100\npatch tone 0 65536 200 0 0 256 2 100 100 50\npatch tone 1 65536 0 0 0 0 2 100 100 50\n';
const base = () => ({version:1, fs:48000, name:'Exact clock', about:['Authored score, not a TL reader.'],
  clock:{bars:2,meter:[{bar:1,quarters:4}],tempo:[{bar:1,bpm:120}],ramp:'step',tail_ns:200000000},
  declarations, streams:['first','second'], voices:['tone','tone'], events:[]});
const note = (at=0, duration=1, voice=0) => ({kind:'note',at,duration,voice,stream:'first',pitch:{note:'A4'},velocity_q15:22000});
{
  const score = base(); score.events = [note(),{...note(1,1,1),stream:'second'},
    {kind:'pos',at:0,stream:'second',voice:0,x_mm:-500,y_mm:1000,z_mm:0},
    {kind:'send',at:0,stream:'second',voice:0,bus:'delay',level_q15:22000}];
  const before = JSON.stringify(score), solo = selectScoreStreams(score,{soloStreams:['first']},{engine});
  assert.equal(JSON.stringify(score),before);
  assert.deepEqual(solo.score.events,[score.events[0],...score.events.slice(2)]);
  assert.deepEqual(solo.authored.sourceMap.filter(row=>row.role==='pos').map(row=>row.pointer),['/events/2']);
  assert.deepEqual(solo.authored.sourceMap.filter(row=>row.role==='send').map(row=>row.pointer),['/events/3']);
  assert.equal(solo.selection.sourceMap,'original_score');
  assert.deepEqual(solo.score.clock,score.clock); assert.equal(solo.score.declarations,score.declarations);
  assert.deepEqual(selectScoreStreams(score,{muteStreams:['second']},{engine}).score,solo.score);
  for (const selection of [{},{soloStreams:[]},{soloStreams:['missing']},{soloStreams:['first','first']},
    {soloStreams:['first'],muteStreams:['second']},{muteStreams:['first','second']}])
    assert.throws(()=>selectScoreStreams(score,selection,{engine}),ScoreError);
  const shared = structuredClone(score);shared.events[1].voice=0;
  assert.throws(()=>selectScoreStreams(shared,{soloStreams:['first']},{engine}),/share a voice gate/);
  const invalid = structuredClone(score);invalid.events[1].pitch={unknown:1};
  assert.throws(()=>selectScoreStreams(invalid,{soloStreams:['first']},{engine}),ScoreError);
  for (const declaration of ['gen_clock 480\n','gen_lane 0 1 - - - 1 6900 0 32768 1 4095\n']) {
    const generated=structuredClone(score); generated.declarations+=declaration;
    assert.throws(()=>selectScoreStreams(generated,{soloStreams:['first']},{engine}),/generator/);
  }
  const lane=structuredClone(score);lane.events.push({kind:'lane',at:0,stream:'second',id:0,enabled:1});
  assert.throws(()=>selectScoreStreams(lane,{soloStreams:['first']},{engine}),/generator/);
}
const cases = [];
for (const ramp of ['step','linear']) {
  for (const quarters of [4, '7/2', '1/4096']) {
    const s = base(); s.clock = {bars:3,meter:[{bar:1,quarters},{bar:3,quarters:3}],
      tempo:[{bar:1,bpm:96},{bar:2,bpm:'281/3'},{bar:4,bpm:137}],ramp,tail_ns:200000000};
    s.events = [note(0,'1/4096'),note('1/4096','1/4096',1), {kind:'mark',at:'1/4096',stream:'first',text:'Phrase boundary'}];
    cases.push(s);
  }
}
// Equal-time stream order and generated stale offs are deliberately not repaired.
const gates = base(); gates.events = [note(0,3),note(1,3),{kind:'on',at:1,voice:0,stream:'second',pitch:{midi:72},velocity_q15:18000},
  {kind:'off',at:5,voice:1,stream:'first'}, {kind:'on',at:6,voice:1,stream:'first',pitch:{millihz:330000},velocity_q15:22000}];
cases.push(gates);
const kinds = base(); kinds.events = [note(),
  {kind:'on',at:2,stream:'second',voice:1,pitch:{midi:60},velocity_q15:21000},
  {kind:'off',at:3,stream:'second',voice:1},
  {kind:'pitch',at:'1/2',stream:'first',voice:0,pitch:{millihz:493883}},
  {kind:'ctl',at:0,stream:'first',voice:0,input:2,value_q15:-12000},
  {kind:'filter',at:0,stream:'first',voice:0,cutoff_millihz:1600000,q_milli:707},
  {kind:'send',at:0,stream:'first',voice:0,bus:'flanger',level_q15:2000},
  {kind:'crush',at:0,stream:'first',voice:0,bits:12,rate_div:2},
  {kind:'pos',at:0,stream:'first',voice:0,x_mm:-500,y_mm:1000,z_mm:20},
  {kind:'move',at:1,stream:'first',voice:0,duration:'3/2',x_mm:500,y_mm:1000,z_mm:-20},
  {kind:'yaw',at:0,stream:'first',yaw_mdeg:-90000},
  {kind:'gen',at:0,stream:'first',id:0,param:'rate',value:300},
  {kind:'lane',at:0,stream:'first',id:0,enabled:1},
  {kind:'mark',at:4,stream:'first',text:'Native owns validation 🎵'}];
cases.push(kinds);
const ties=base();ties.clock.tempo=[{bar:1,bpm:120000000000}];ties.events=[note(1,1),note(3,1,1)];cases.push(ties);
// Rational maps with different anchors exercise cumulative integration, not repeated beat rounding.
for (let i = 0; i < 36; i++) {
  const s = base(); s.clock = {bars:4,meter:[{bar:1,quarters:'7/2'},{bar:3,quarters:3}],
    tempo:[{bar:1,bpm:91+i},{bar:3,bpm:`${300+i}/3`},{bar:5,bpm:127-i}],ramp:i%2?'linear':'step',tail_ns:i};
  s.events = [note(`${i}/37`,'7/11'),note('9/2','3/7',1),note('25/2','1/2')]; cases.push(s);
}
for (const {id} of listRecipes()) {
  const s=base(); s.name='Recipe '+id; s.declarations='recipe tone '+id+' # chosen unchanged preset\n'; s.events=[note()]; cases.push(s);
}
// Every generated carrier mapping is checked against the Python recipe data,
// including routed two-/three-/four-operator presets and round/clamp boundaries.
for (const {id,carriers} of listRecipes()) for (const delta of [6,-6,0,.05,.15,-.05,1000,-1000]) {
  const s=base();s.name='Recipe level '+id;
  s.declarations='recipe tone '+id+' '+carriers.map(c=>c.tweak+'='+delta).join(' ')+'\n';
  s.events=[note()];cases.push(s);
}
for (const delta of ['0.049999999999999','0.050000000000001','0.0000001','-0.0000001','1234567','-1234567']) {
  const s=base();s.declarations='recipe tone flute c_level='+delta+'\n';s.events=[note()];cases.push(s);
}
const starter = JSON.parse(fs.readFileSync(path.join(root,'web/starter.score.json'),'utf8')); cases.push(starter);
const python = String.raw`
import importlib.util,json,pathlib,sys
root=pathlib.Path(sys.argv[1])
def module(name,path):
 spec=importlib.util.spec_from_file_location(name,path);out=importlib.util.module_from_spec(spec);sys.modules[name]=out;spec.loader.exec_module(out);return out
cp=module('composer',root/'tools/music/compose.py');sc=module('score',root/'tools/score.py')
statements={}
for line in (root/'web/reference/params.txt').read_text().splitlines():
 if line.startswith('STATEMENT '):
  row=dict(word.split('=',1) for word in line.split()[1:]);row['row']='STATEMENT';statements[row['id']]=row
out=[]
for source in json.load(sys.stdin):
 lowered=sc.lower_score(sc.validate_score(source),composer=cp,engine=sys.argv[2],statements=statements)
 view=sc.inspect_source(source,composer=cp)
 out.append(dict(authored_tl=lowered['authored_tl'],line_map=lowered['line_map'],counts=lowered['counts'],length_ns=lowered['length_ns'],quarter_extent=lowered['quarter_extent'],advisories=view['advisories']))
carriers={}
for name in cp.RECIPES:
 c,m,_,extra=cp.recipe(name);source=cp.RECIPES[name];meta=source[3] if len(source)>3 else {}
 ops=[source[0],source[1]]+meta.get('more',[])
 carriers[name]=[dict(operator=i,tweak=('c','m','o2','o3')[i]+'_level',attenuationDb=op.get('level',0.0))
                 for i,op in enumerate(ops) if extra.get('carriers',1)>>i&1]
print(json.dumps(dict(cases=out,pitches=[cp.mhz_m(i) for i in range(128)],carriers=carriers)))
`;
const oracle = spawnSync(process.env.PYTHON || 'python3',['-B','-c',python,sourceRoot,engine],
  {input:JSON.stringify(cases),encoding:'utf8',maxBuffer:8*1024*1024});
assert.equal(oracle.status,0,oracle.stderr);
const expected = JSON.parse(oracle.stdout);
for (let i=0;i<128;i++) assert.equal(pitchMillihz({midi:i}),expected.pitches[i]);
assert.equal(pitchMillihz({note:'Cb4'}),pitchMillihz({note:'B3'}));
for (let i=0;i<cases.length;i++) {
  const got=compileScore(cases[i],{engine});
  for (const key of ['authored_tl','line_map','counts','length_ns','quarter_extent','advisories'])
    assert.deepEqual(got[key],expected.cases[i][key],`case ${i} ${key}`);
  assert.equal(got.coverage.native_validation,'not_run');
}
const bad = change => {const score=base();change(score);assert.throws(()=>compileScore(score,{engine}),ScoreError);};
bad(s=>{s.version=2;s.phrases=[];}); bad(s=>{s.version=3;s.regions=[];});
bad(s=>{s.clock.ramp='exponential';}); bad(s=>{s.clock.tempo[0].bar=2;});
bad(s=>{s.clock.meter[0].quarters='1/4097';}); bad(s=>{s.events=[note(0,0)];});
bad(s=>{s.events=[note(7,2)];}); bad(s=>{s.events=[note(0.5,1)];});
bad(s=>{s.events=[{...note(),velocity_q15:9007199254740992}];});
bad(s=>{s.events=[{...note(),pitch:{note:'a4'}}];}); bad(s=>{s.events=[{...note(),pitch:{note:'G#9'}}];});
bad(s=>{s.events=[{...note(),kind:'chord'}];}); bad(s=>{s.streams=['first','first'];});
bad(s=>{s.about=['\ud800'];}); bad(s=>{s.declarations+='\r';});
bad(s=>{s.about=['\udc00'];});bad(s=>{s.clock.tail_ns=600000000000;});
bad(s=>{s.about=['x'.repeat(1048576)];});bad(s=>{s.events=Array.from({length:8193},()=>note());});
for (const row of ['hash 0','voice 5 tone','0 first 0 off','recipe tone unknown','recipe tone epiano time=2']) bad(s=>{s.declarations=row;});
for (const row of ['recipe tone flute m_level=6','recipe tone ep-4op m_level=6','recipe tone ep-4op o3_level=6',
  'recipe tone flute c_level=6 c_level=3','recipe tone flute c_level=1e2','recipe tone flute c_level=NaN'])
  bad(s=>{s.declarations=row;});
const cycle=base(); cycle.about=[cycle]; assert.throws(()=>compileScore(cycle,{engine}),ScoreError);
const future=base();future.declarations+='future-native-field 1\n';
assert.throws(()=>compileScore(future,{engine}),ScoreError);
assert.match(compileScore(future,{engine,statements:[...['space','block','near','patch','future-native-field'].map(name=>({row:'STATEMENT',section:'header',name}))]}).draftText,/future-native-field 1/);
assert.equal(/^recipe /m.test(recipe('epiano','ep')),false);
for (const {id,carriers} of listRecipes()) {
  assert.deepEqual(carriers,expected.carriers[id],id+' carrier metadata');
  assert.equal(recipeGain(id,'tone',0),recipe(id,'tone'));
  assert.equal(recipeGain(id,'tone',-6),recipe(id,'tone',Object.fromEntries(carriers.map(c=>[c.tweak,6]))));
  const loudest=Math.min(...carriers.map(c=>c.attenuationDb)),quietest=Math.max(...carriers.map(c=>c.attenuationDb-120));
  assert.doesNotThrow(()=>recipeGain(id,'tone',loudest));assert.doesNotThrow(()=>recipeGain(id,'tone',quietest));
  assert.throws(()=>recipeGain(id,'tone',loudest+.1),ScoreError);
  assert.throws(()=>recipeGain(id,'tone',quietest-.1),ScoreError);
  assert.throws(()=>recipeGain(id,'tone',.05),ScoreError);
  for(const key of ['c_level','m_level','o2_level','o3_level'].filter(key=>!carriers.some(c=>c.tweak===key)))
    assert.throws(()=>recipe(id,'tone',{[key]:6}),ScoreError);
}
for (const tweaks of [{c_level:NaN},{c_level:Infinity},{c_level:'6'},{gain:6},{m_index:1},null])
  assert.throws(()=>recipe('flute','wind',tweaks),ScoreError);
const isolated=listRecipes();isolated[0].carriers[0].attenuationDb=999;
assert.notEqual(listRecipes()[0].carriers[0].attenuationDb,999);
assert.match(recipeGain('flute','wind',-6),/^patch wind\s+0\s+65536\s+160\b/m);
assert.equal(fs.readFileSync(path.join(root,'web/starter.tl'),'utf8'),compileScore(starter,{engine}).draftText);

assert.ok(fs.existsSync(harness),'Set JOTTUNE_HARNESS to the existing native renderer.');
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'jottune-authoring-'));
try {
  const run=args=>{const r=spawnSync(harness,args,{encoding:'utf8',maxBuffer:4*1024*1024});assert.equal(r.status,0,r.stderr+'\n'+r.stdout);return r;};
  const draft=path.join(tmp,'starter.tl'),canonical=path.join(tmp,'canonical.tl'),replay=path.join(tmp,'replay.tl');
  fs.writeFileSync(draft,compileScore(starter,{engine}).draftText);
  const initial=run(['--play',draft,'--merged',canonical,'--metrics']);
  assert.match(initial.stdout,/match=- /); assert.match(initial.stdout,/block_indep=yes/);
  const saved=run(['--play',canonical,'--merged',replay,'--metrics']);
  assert.match(saved.stdout,/match=yes/);assert.match(saved.stdout,/block_indep=yes/);assert.match(saved.stdout,/clipped=0\b/);
  assert.ok(Number(/peak=(\d+)/.exec(saved.stdout)[1])>0);assert.deepEqual(fs.readFileSync(canonical),fs.readFileSync(replay));
  const wrong=fs.readFileSync(canonical,'utf8').replace(/^hash .+$/m,'hash 0000000000000000');fs.writeFileSync(draft,wrong);
  const mismatch=spawnSync(harness,['--play',draft],{encoding:'utf8'});assert.equal(mismatch.status,3);assert.match(mismatch.stdout,/match=NO/);
  // Every hosted default preset is a native-accepted complete instrument.
  for(const {id} of listRecipes()) {
    const s=base();s.clock.bars=1;s.declarations='recipe tone '+id+'\n';s.events=[note()];
    fs.writeFileSync(draft,compileScore(s,{engine}).draftText);run(['--inspect',draft]);
    s.declarations=recipeGain(id,'tone',-6);
    fs.writeFileSync(draft,compileScore(s,{engine}).draftText);
    const gain=run(['--play',draft,'--merged',canonical]);assert.match(gain.stdout,/block_indep=yes/);
    if(['flute','ep-4op','sub-bass','snare-3op','snare-white'].includes(id)) {
      const replayed=run(['--play',canonical]);assert.match(replayed.stdout,/match=yes block_indep=yes/);
    }
  }
  console.log(`Authoring passed: ${cases.length} Python differential scores, 128 exact pitches, 60 carrier mappings and native gain renders, starter and routed gain replay, mismatch control.`);
} finally {fs.rmSync(tmp,{recursive:true,force:true});}
