#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Native exact-bank export acceptance. Linux host only; no WAV output."""
from pathlib import Path
import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile

helper,music,harness,engine=sys.argv[1:5]
spec=importlib.util.spec_from_file_location('recipe_export',helper)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
c=m.load_composer(music);original_book=c.RECIPES
kinds=set(('patch route filter shape amp lfo peg fenv mod eg ks kstype fbtype egloop noise pd vector formant wtbank wtdata wthash wtuse ksource').split())
assets={'wtbank','wtdata','wthash'};checks=0;compared=0;forms=0


def ok(value,why):
    global checks
    if not value:raise SystemExit('EXPORT_FORMS FAILED: '+why)
    checks+=1


def canonical(path):
    merged=path.with_suffix('.merged.tl')
    r=subprocess.run([harness,'--play',str(path),'--merged',str(merged)],text=True,capture_output=True)
    ok(r.returncode==0 and 'block_indep=yes' in r.stdout,r.stdout+r.stderr)
    return [line for line in merged.read_text(encoding='utf-8').splitlines() if line and line.split()[0] in kinds]


def from_headers(tmp,rows):
    path=tmp/'source.tl'
    header=['timeline draft2','engine '+engine,'fs 48000','length_ns 1000000','space off','streams main']
    path.write_text('\n'.join(header+rows+['voice 0 test'])+'\n',encoding='utf-8')
    return canonical(path)


def export(tmp,rows):
    source=tmp/'patch-lines.txt';before='\n'.join(rows)+'\n';source.write_text(before,encoding='utf-8')
    with contextlib.redirect_stdout(io.StringIO()):
        target=Path(m.export_recipe(c,harness,engine,str(tmp/'out'),str(source),'test'))
    ok(source.read_text(encoding='utf-8')==before,'source bytes unchanged')
    text=target.read_text(encoding='utf-8')
    pending=[line[11:] for line in text.splitlines() if line.startswith('# pending: ')]
    replay=canonical(target.parent/'preview/test.tl')
    return text,pending,replay


with tempfile.TemporaryDirectory(prefix='export-forms-') as temporary:
    tmp=Path(temporary)
    if '--negative-only' not in sys.argv:
        for name in original_book:
            c.RECIPES=original_book
            source=tmp/'original.in';record=tmp/'original.tl'
            source.write_text(m.TEMPLATE.format(name=name,note='bundled acceptance',engine=engine,patch='test'),encoding='utf-8')
            c.expand_timeline(source,record);rows=canonical(record)
            text,pending,replay=export(tmp,rows)
            ok(not pending and replay==rows,'all bundled canonical rows: '+name)
            compared+=1
        for form in getattr(c,'RECIPE_WT_FORMS',c.HARMONIC_FORMS):
            definition=(1,2048,getattr(c,'recipe_harmonics',c.harmonic_form)(form))
            rows=from_headers(tmp,c.patch_headers('test',c.op(level=20),c.op())+
                c.wtbank_rows(form,definition)+['wtuse test 0 '+form+' 12345'])
            text,pending,replay=export(tmp,rows)
            ok(not pending and replay==rows and 'wtuse 0 '+form+' 12345\n' in text,'known form exact asset/binding: '+form)
            forms+=1
    # A matching name never proves sample equivalence: alter its first harmonic.
    harmonics=list(c.harmonic_form('bl-saw'));harmonics[0]=(0,1,5,0)
    rows=from_headers(tmp,c.patch_headers('test',c.op(level=20),c.op())+
        c.wtbank_rows('bl-saw',(1,2048,harmonics))+['wtuse test 0 bl-saw 0'])
    text,pending,replay=export(tmp,rows)
    expected=[line for line in rows if line.split()[0] in assets|{'wtuse'}]
    ok(pending==expected and 'wtuse 0 bl-saw 0\n' not in text,'same-name custom bank pending rows')
    ok(not any(line.split()[0] in assets|{'wtuse'} for line in replay),'custom asset is never silently replaced')
    if '--negative-only' not in sys.argv:
        for bank,used in [('unknown',True),('bl-triangle',False)]:
            definition=(1,32,((0,1,0,0),)) if used else (1,2048,c.harmonic_form(bank))
            rows=from_headers(tmp,c.patch_headers('test',c.op(level=20),c.op())+c.wtbank_rows(bank,definition)+
                (['wtuse test 0 '+bank+' 0'] if used else []))
            text,pending,replay=export(tmp,rows)
            expected=[line for line in rows if line.split()[0] in assets|{'wtuse'}]
            ok(pending==expected and bool(expected),'unknown/unused exact pending: '+bank)
        rows=from_headers(tmp,c.patch_headers('test',c.op(level=20),c.op())+
            c.wtbank_rows('bl-square',(1,2048,c.harmonic_form('bl-square')))+
            c.wtbank_rows('unused',(1,32,((0,1,0,0),)))+['wtuse test 0 bl-square 32768'])
        text,pending,replay=export(tmp,rows)
        ok(pending==[line for line in rows if line.split()[0] in assets and line.split()[1]=='unused'],
           'known bound bank carries independently of unrelated pending bank')
        ok([line for line in replay if line.split()[0] in assets|{'wtuse'}]==
           [line for line in rows if line.split()[0] in assets|{'wtuse'} and 'unused' not in line.split()],
           'mixed-bank native asset identity')
        fake=tmp/'failed-harness';fake.write_text('#!/bin/sh\nprintf "PLAY hash=0000000000000000 block_indep=yes\\n"\nexit 1\n',encoding='utf-8');fake.chmod(0o700)
        try:m.matched_recipe_banks(c,str(fake),engine,rows)
        except ValueError:ok(True,'native nonzero status refuses despite success-looking line')
        else:ok(False,'native nonzero status must refuse')
print('EXPORT_FORMS %d checks passed; recipes=%d forms=%d; exact pending controls; no WAV output'%(checks,compared,forms))
