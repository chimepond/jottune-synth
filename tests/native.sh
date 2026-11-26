#!/usr/bin/env bash
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
# Native, portable and UBSan regression checks, reference comparisons,
# canonical records, parser refusals, tool self-tests and integer-only audits.
set -u
SECONDS=0
unset MAKEFLAGS MFLAGS MAKELEVEL

here=$(cd "$(dirname "$0")" && pwd)
cd "$here/.." || exit 1
tmp=$(mktemp -d "${TMPDIR:-/tmp}/fm-check.XXXXXX") || exit 1
trap 'rm -rf "$tmp"' EXIT

passed=0 failed=0 skipped=0
skipped_names=
line()   { if [ -n "${3-}" ]; then printf 'CHECK %-40s %-4s %s\n' "$1" "$2" "$3"; else printf 'CHECK %-40s %s\n' "$1" "$2"; fi; }
ok()     { passed=$((passed + 1));   line "$1" ok "${2-}"; }
fail()   { failed=$((failed + 1));   line "$1" FAIL "${2-}"; }
skip()   {
    skipped=$((skipped + 1))
    skipped_names="${skipped_names}${skipped_names:+, }$1"
    line "$1" SKIP "${2-}"
}
indent() { sed 's/^/      /'; }
finish() {
    echo "CHECK time $SECONDS s"
    if [ "$skipped" -gt 0 ]; then
        echo "CHECK skipped: $skipped_names"
        if [ "${CHECK_STRICT:-0}" = 1 ]; then
            echo "CHECK FAIL strict: $skipped skipped ($passed passed, $skipped skipped)"
            exit 1
        fi
    fi
    if [ $failed -eq 0 ]; then
        echo "CHECK ok ($passed passed, $skipped skipped)"
        exit 0
    fi
    echo "CHECK FAIL $failed ($passed passed, $skipped skipped)"
    exit 1
}

# standalone generators have their own independent reference and
# detector suite. Full statistical/map acceptance is a separate full run.
# Quick/all includes at least 499 rows without cross compilers.
if python3 -B tests/generators/check.py --profile quick --target all > "$tmp/gen.log" 2>&1 &&
   gen_rows=$(sed -n 's/^GEN ok (\([0-9][0-9]*\) rows)$/\1/p' "$tmp/gen.log") &&
   [ "$gen_rows" -ge 499 ] 2>/dev/null; then
    ok "generator quick suite" "$(tail -n 1 "$tmp/gen.log")"
else
    fail "generator quick suite" "standalone suite failed or incomplete"
    indent < "$tmp/gen.log"
fi

knobs="SINE_BITS=12 EXP_BITS=10 PHASE64=1 ENV64=1"
defs="-DFM_SINE_BITS=12 -DFM_EXP_BITS=10 -DFM_PHASE64=1 -DFM_ENV64=1 -Ibuild/s12e10p1v1"
builds="s12e10p1v1 s12e10p1v1-portable s12e10p1v1-ubsan"
main=build/s12e10p1v1/harness
echo "CHECK host $(uname -s) $(uname -m), ${CC:-cc}: $("${CC:-cc}" --version 2>&1 | head -n 1)"

# Informational provenance does not add checks or skips.
source_head=$(git -C "$here/.." rev-parse --short HEAD 2>/dev/null || echo 'no git')
source_dirty=$(git -C "$here/.." status --porcelain -- src vendor 2>/dev/null | wc -l | tr -d ' ')
echo "CHECK source $source_head dirty=$source_dirty"
tool_version() {
    if command -v "$1" > /dev/null 2>&1; then
        "$1" --version 2>&1 | head -n 1
    else printf 'absent'; fi
}
rv_cc=riscv64-elf-gcc
if ! command -v "$rv_cc" > /dev/null 2>&1; then rv_cc=riscv64-unknown-elf-gcc; fi
numpy=no
if python3 -B -c 'import numpy' > /dev/null 2>&1; then numpy=yes; fi
echo "CHECK tools python3=$(tool_version python3); make=$(tool_version make); arm-none-eabi-gcc=$(tool_version arm-none-eabi-gcc); riscv=$(tool_version "$rv_cc"); numpy=$numpy"

# make -B: every object from these flags, whatever an earlier make left
build() { # NAME MAKE-ARGUMENTS...
    local name=$1
    shift
    if make -B -s "$@" > "$tmp/make.log" 2>&1; then
        ok "build $name"
        indent < "$tmp/make.log"
    else
        fail "build $name" "make $*"
        indent < "$tmp/make.log"
        return 1
    fi
}
build s12e10p1v1 $knobs || finish
build s12e10p1v1-portable $knobs EXTRA=-DFP_MATH_FORCE_PORTABLE VARIANT=-portable || finish
build s12e10p1v1-ubsan $knobs OPT="-O1 -g -fsanitize=undefined -fno-sanitize-recover=all" VARIANT=-ubsan || finish

# discovery and scalar/dependent boundaries share native
# metadata; the checker mutates accepted records, never validates them itself.
for b in $builds; do
    for fs in 48000 44100; do
        python3 -B tests/params_check.py --harness "build/$b/harness" --repo . \
            --fs "$fs" --out "$tmp/params-$b-$fs" > "$tmp/params.log" 2>&1
        rc=$?
        if [ $rc -eq 0 ] && grep -q '^PARAMCHECK ok ' "$tmp/params.log"; then
            ok "params $b $fs" "$(tail -n 1 "$tmp/params.log")"
        else
            fail "params $b $fs" "exit $rc or missing complete result"
            indent < "$tmp/params.log"
        fi
    done
done

# formant ranges, slot order and complete-header target prerequisites.
for b in $builds; do
    if python3 -B - "build/$b/harness" "$tmp/formant-session" > "$tmp/formant-session.log" 2>&1 <<'PY'
import pathlib,subprocess,re,sys,itertools
h=pathlib.Path(sys.argv[1]).resolve();tmp=pathlib.Path(sys.argv[2]);tmp.mkdir(exist_ok=True);count=0
base=pathlib.Path('tests/golden/48000/sine.tl').read_text()
def run(text,needle=None,fail=False):
 global count
 src=tmp/'in.tl';merged=tmp/'out.tl';src.write_text(text);p=subprocess.run([h,'--play',src,'--merged',merged],text=True,capture_output=True)
 assert p.returncode==(1 if fail else 0),(p.returncode,p.stdout,p.stderr,text)
 if needle:assert needle in (p.stderr if fail else merged.read_text()),(needle,p.stderr,p.stdout)
 count+=1;return p,merged.read_text() if not fail else ''
def free(s):return re.sub(r'^hash .*\n','',s,flags=re.M)
for slot in range(3):
 for center,q,level in [(1,500,0),(4294967295,16000,1200),(500000,707,60)]:
  line=f'formant sine {slot} {center} {q} {level}\n';p,m=run(free(base)+line,line);run(m)
for line,needle in [('formant sine','formant takes'),('formant sine 0 500000 707 0 extra','formant takes'),('formant sine -1 1 500 0','SLOT'),('formant sine 3 1 500 0','SLOT'),('formant sine x 1 500 0','SLOT'),('formant sine 0 0 500 0','CENTER'),('formant sine 0 -1 500 0','CENTER'),('formant sine 0 4294967296 500 0','CENTER'),('formant sine 0 1.5 500 0','CENTER'),('formant sine 0 1 499 0','Q_MILLI'),('formant sine 0 1 16001 0','Q_MILLI'),('formant sine 0 1 500 -1','LEVEL'),('formant sine 0 1 500 1201','LEVEL'),('formant sine 0 1 500 0\nformant sine 0 2 500 0','second formant'),('formant absent 0 1 500 0','lacks operator')]:run(free(base)+line+'\n',needle,True)
for target,limit in [('formant_center',2147483647),('formant_gain',1200)]:
 for slot in range(3):
  for amount in (-limit,0,limit):
   text=f'mod sine 0 ctl0 {target}{slot} {amount} 0\nformant sine {slot} 500000 707 0\n'+free(base)
   p,m=run(text,f'mod sine 0 ctl0 {target}{slot} {amount} 0\n');run(m)
  for amount in (-limit-1,limit+1):run(free(base)+f'formant sine {slot} 500000 707 0\nmod sine 0 ctl0 {target}{slot} {amount}\n','AMOUNT',True)
  for amount in (0,1):run(free(base)+f'mod sine 0 ctl0 {target}{slot} {amount}\n','needs enabled formant',True)
 for suffix in ('','3','-1','00','x'):run(free(base)+f'mod sine 0 ctl0 {target}{suffix} 1\n','unavailable',True)
# The largest signed center sum is exercised through both ctl endpoints.
for value in (-32768,32768):
 text=free(base)+'formant sine 0 500000 707 0\n'+''.join(f'mod sine {i} ctl0 formant_center0 2147483647 0\n' for i in range(8));text=text.replace('0 golden 0 on',f'0 golden 0 ctl 0 {value}\n0 golden 0 on');p,m=run(text);run(m)
for fs in (48000,44100):
 for name in ('formant3','formantmove'):
  text=pathlib.Path(f'tests/golden/{fs}/{name}.tl').read_text();p,m=run(text);assert text==m and 'match=yes block_indep=yes clipped=0' in p.stdout
  for perm in itertools.permutations(range(3)):
   rows=[]
   for line in text.splitlines():
    tokens=line.split()
    if tokens and tokens[0]=='formant':tokens[2]=str(perm[int(tokens[2])]);line=' '.join(tokens)
    elif tokens and tokens[0]=='mod' and tokens[4].startswith('formant_'):tokens[4]=tokens[4][:-1]+str(perm[int(tokens[4][-1])]);line=' '.join(tokens)
    rows.append(line)
   p,m=run('\n'.join(rows)+'\n');assert 'match=yes block_indep=yes clipped=0' in p.stdout
  # Complete-header order is independent of canonical declaration position.
  rows=text.splitlines();formants=[x for x in rows if x.startswith('formant ')];p,m=run('\n'.join(formants+[x for x in rows if not x.startswith('formant ')])+'\n');assert text==m
 text=pathlib.Path(f'tests/golden/{fs}/formant3.tl').read_text();src=tmp/'in.tl';src.write_text(re.sub(r'^formant .*\n','',text,flags=re.M));p=subprocess.run([h,'--play',src],text=True,capture_output=True);assert p.returncode==3 and 'match=NO' in p.stdout;count+=1
print(f'FORMANTSESSION checks={count} failures=0')
PY
    then ok "formant session $b" "$(tail -n 1 "$tmp/formant-session.log")"
    else fail "formant session $b" "formant ranges, order or targets"; tail -n 12 "$tmp/formant-session.log" | indent; fi
done

# static coordinates retain all corners and validate complete headers.
for b in $builds; do
    if python3 -B - "build/$b/harness" "$tmp/vector-session" > "$tmp/vector-session.log" 2>&1 <<'PY'
import pathlib,subprocess,re,sys
h=pathlib.Path(sys.argv[1]).resolve();tmp=pathlib.Path(sys.argv[2]);tmp.mkdir(exist_ok=True);count=0
base=pathlib.Path('tests/golden/vector/vectorcorners-48000.tl').read_text()
def run(text,needle=None,fail=False):
 global count
 src=tmp/'in.tl';merged=tmp/'out.tl';src.write_text(text);p=subprocess.run([h,'--play',src,'--merged',merged],text=True,capture_output=True)
 assert p.returncode==(1 if fail else 0),(p.returncode,p.stdout,p.stderr,text)
 if needle:assert needle in (p.stderr if fail else merged.read_text()),(needle,p.stderr,p.stdout)
 count+=1;return p,merged.read_text() if not fail else ''
def free(s):return re.sub(r'^hash .*\n','',s,flags=re.M)
def coord(x,y):return re.sub(r'^vector corner0 .*$',f'vector corner0 {x} {y}',free(base),flags=re.M)
for x,y in [(0,0),(32768,0),(0,32768),(32768,32768),(16384,16384),(1,32767)]:
 p,m=run(coord(x,y),f'vector corner0 {x} {y}\n');run(m)
# All declarations may precede their patch, with full-header prerequisites.
rows=free(base).splitlines();vectors=[x for x in rows if x.startswith('vector ')];p,m=run('\n'.join(vectors+[x for x in rows if not x.startswith('vector ')])+'\n');assert all(x in m for x in vectors)
for text,needle in [(coord(-1,0),"X_Q15 '-1' is not 0 to 32768"),(coord(32769,0),"X_Q15 '32769' is not 0 to 32768"),(coord(0,-1),"Y_Q15 '-1' is not 0 to 32768"),(coord(0,32769),"Y_Q15 '32769' is not 0 to 32768"),(coord('1.5',0),"X_Q15 '1.5' is not 0 to 32768"),(coord(0,'x'),"Y_Q15 'x' is not 0 to 32768"),(re.sub(r'^vector corner0 .*$', 'vector corner0',free(base),flags=re.M),'vector takes'),(coord(0,'0 extra'),'vector takes'),(free(base)+'vector corner0 1 1\n','second vector'),(free(base)+'vector absent 0 0\n','vector needs route'),(re.sub(r'^route corner0 .*\n','',free(base),flags=re.M),'needs a route statement'),(free(base).replace('route corner0 4 15','route corner0 4 1'),'vector needs route')]:run(text,needle,True)
small=pathlib.Path('tests/golden/48000/sine.tl').read_text();run(free(small)+'route sine 2 3\nvector sine 0 0\n','vector needs route',True)
for fs in (48000,44100):
 text=pathlib.Path(f'tests/golden/vector/vectorcorners-{fs}.tl').read_text();p,m=run(text);assert text==m and 'match=yes block_indep=yes clipped=0' in p.stdout
 # Exact corner outputs equal selecting the corresponding sole carrier.
 twin=re.sub(r'^vector .*\n','',text,flags=re.M)
 for i in range(4):twin=twin.replace(f'route corner{i} 4 15',f'route corner{i} 4 {1<<i}')
 p,m=run(twin);assert 'match=yes block_indep=yes clipped=0' in p.stdout
 # Removing the opt-in while retaining all four carriers must alter PCM.
 src=tmp/'in.tl';src.write_text(re.sub(r'^vector .*\n','',text,flags=re.M));p=subprocess.run([h,'--play',src],text=True,capture_output=True);assert p.returncode==3 and 'match=NO' in p.stdout;count+=1

for target in ('vector_x','vector_y'):
 for amount in (-32768,0,32768):
  p,m=run(free(base)+f'mod corner0 0 ctl0 {target} {amount} 0\n',f'mod corner0 0 ctl0 {target} {amount} 0\n');run(m)
 for amount in (-32769,32769):run(free(base)+f'mod corner0 0 ctl0 {target} {amount}\n','AMOUNT',True)
 for amount in (0,1):run(free(small)+f'mod sine 0 ctl0 {target} {amount}\n','needs enabled vector',True)
 for suffix in ('0','1','_','x'):run(free(base)+f'mod corner0 0 ctl0 {target}{suffix} 1\n','unavailable',True)
# Complete-header order and active-source prerequisites remain enforced.
text=free(base);p,m=run('mod corner0 0 ctl0 vector_x 12345 15\n'+text);run(m)
for fs in (48000,44100):
 text=pathlib.Path(f'tests/golden/{fs}/vectorxy.tl').read_text();p,m=run(text);assert text==m and 'match=yes block_indep=yes clipped=0' in p.stdout
 src=tmp/'in.tl';src.write_text(re.sub(r'^mod .*\n','',text,flags=re.M));p=subprocess.run([h,'--play',src],text=True,capture_output=True);assert p.returncode==3 and 'match=NO' in p.stdout;count+=1
print(f'VECTORMOVESESSION checks={count} failures=0')
PY
    then ok "vector session $b" "$(tail -n 1 "$tmp/vector-session.log")"
    else fail "vector session $b" "static coordinates or route validation"; tail -n 12 "$tmp/vector-session.log" | indent; fi
done

# explicit identity, complete-header validation and opt-in sensitivity.
for b in $builds; do
    if python3 -B - "build/$b/harness" "$tmp/pd-session" > "$tmp/pd-session.log" 2>&1 <<'PY'
import pathlib,subprocess,re,sys
h=pathlib.Path(sys.argv[1]).resolve();tmp=pathlib.Path(sys.argv[2]);tmp.mkdir(exist_ok=True);count=0
base=pathlib.Path('tests/golden/48000/sine.tl').read_text();name='sine'
def run(text,needle=None,fail=False):
 global count
 src=tmp/'in.tl';merged=tmp/'out.tl';src.write_text(text);p=subprocess.run([h,'--play',src,'--merged',merged],text=True,capture_output=True)
 assert p.returncode==(1 if fail else 0),(p.returncode,p.stdout,p.stderr,text)
 if needle:assert needle in (p.stderr if fail else merged.read_text()),(needle,p.stderr,p.stdout)
 count+=1;return p,merged.read_text() if not fail else ''
def free(s):return re.sub(r'^hash .*\n','',s,flags=re.M)
for knee in (1,8192,16384,32767):
 p,m=run(free(base)+f'pd sine 0 {knee}\n',f'pd sine 0 {knee}\n');run(m)
# Complete-header ordering permits PD and target before their patch.
p,m=run('pd sine 0 16384\nmod sine 0 ctl0 pd_knee0 -32767 0\n'+free(base),'mod sine 0 ctl0 pd_knee0 -32767 0\n');run(m)
for op in range(4):
 text=free(pathlib.Path('tests/golden/48000/route4.tl').read_text()) if pathlib.Path('tests/golden/48000/route4.tl').exists() else None
 if text:
  patch=re.search(r'^patch (\S+) ',text,re.M).group(1);run(text+f'pd {patch} {op} 16384\nmod {patch} 0 ctl0 pd_knee{op} 32767 0\n')
for line,needle in [('pd sine','pd takes'),('pd sine 0 1 extra','pd takes'),('pd sine -1 1','OP'),('pd sine 4 1','OP'),('pd sine x 1','OP'),('pd sine 0 0','KNEE_Q15'),('pd sine 0 32768','KNEE_Q15'),('pd sine 0 -1','KNEE_Q15'),('pd sine 0 1.5','KNEE_Q15'),('pd sine 2 1','which the patch lacks'),('pd sine 0 1\npd sine 0 2','second pd'),('pd absent 0 1','lacks operator'),('mod sine 0 ctl0 pd_knee0 0','needs enabled sine pd'),('pd sine 0 1\nmod sine 0 ctl0 pd_knee0 32768','AMOUNT'),('pd sine 0 1\nmod sine 0 ctl0 pd_knee0 -32768','AMOUNT'),('pd sine 0 1\nmod sine 0 ctl0 pd_knee2 1','targets absent operator'),('pd sine 0 1\nmod sine 0 ctl0 pd_knee 1','unavailable'),('pd sine 0 1\nmod sine 0 ctl0 pd_knee4 1','unavailable'),('pd sine 0 1\nmod sine 0 ctl0 pd_knee00 1','unavailable')]:run(free(base)+line+'\n',needle,True)
for wave in range(1,12):
 lines=free(base).splitlines();ix=next(i for i,l in enumerate(lines) if l.startswith('patch sine 0 '));tokens=lines[ix].split();tokens=(tokens+['0']*3)[:15];tokens[14]=str(wave);lines[ix]=' '.join(tokens)
 run('\n'.join(lines)+'\npd sine 0 16384\n','needs sine WAVE 0',True)
# Every previous voice/audio record with identity on every sine operator
# keeps its pinned hash; PD is not silently omitted by canonicalization.
for fs in (48000,44100):
 for record in sorted(pathlib.Path(f'tests/golden/{fs}').glob('*.tl')):
  if record.stem in ('pdknee','pdmorph','pdmin','pdmax'):continue
  text=record.read_text();added=[];wt={tuple(l.split()[1:3]) for l in text.splitlines() if l.startswith(('wtuse ','ksource '))}
  for line in text.splitlines():
   tokens=line.split()
   if tokens and tokens[0]=='patch' and (len(tokens)<=14 or int(tokens[14])==0) and tuple(tokens[1:3]) not in wt:added.append(f'pd {tokens[1]} {tokens[2]} 16384')
  p,m=run(text+'\n'+'\n'.join(added)+'\n');assert 'match=yes block_indep=yes' in p.stdout
  assert all(row in m for row in added)
 for name in ('pdknee','pdmorph','pdmin','pdmax'):
  text=pathlib.Path(f'tests/golden/{fs}/{name}.tl').read_text();p,m=run(text);assert text==m and 'clipped=0' in p.stdout
# Opt-in must alter an audible fixture; dropping it detects missing reader use.
text=pathlib.Path('tests/golden/48000/pdknee.tl').read_text();src=tmp/'in.tl';src.write_text(re.sub(r'^pd .*\n','',text,flags=re.M));p=subprocess.run([h,'--play',src],text=True,capture_output=True);assert p.returncode==3 and 'match=NO' in p.stdout;count+=1
print(f'PDSESSION checks={count} failures=0')
PY
    then ok "phase distortion session $b" "$(tail -n 1 "$tmp/pd-session.log")"
    else fail "phase distortion session $b" "identity, canonical replay or refusal"; tail -n 12 "$tmp/pd-session.log" | indent; fi
done

# wavetable statements. Canonical replay, harmonic authoring written
# back as samples and hash, header order, unused banks, refusals, and the
# hash's use: a changed sample is refused, and its new hash plays to exit 3.
for b in $builds; do
    if python3 -B - "build/$b/harness" "$tmp/wt-session" > "$tmp/wt-session.log" 2>&1 <<'PY'
import pathlib,subprocess,re,sys
h=pathlib.Path(sys.argv[1]).resolve();tmp=pathlib.Path(sys.argv[2]);tmp.mkdir(exist_ok=True);count=0
def run(text,needle=None,fail=False):
 global count
 src=tmp/'in.tl';merged=tmp/'out.tl';src.write_text(text);p=subprocess.run([h,'--play',src,'--merged',merged],text=True,capture_output=True)
 assert p.returncode==(1 if fail else 0),(p.returncode,p.stdout,p.stderr,text[:400])
 if needle:assert needle in (p.stderr if fail else merged.read_text()),(needle,p.stderr,p.stdout)
 count+=1;return p,merged.read_text() if not fail else ''
def free(s):return re.sub(r'^hash .*\n','',s,flags=re.M)
def drop(s,kinds):return re.sub(r'^(%s) .*\n'%'|'.join(kinds),'',s,flags=re.M)
harm='harmonic additive 0 1 0 0\nharmonic additive 0 3 120 32768\nharmonic additive 0 2 60 16384\n'
for fs in (48000,44100):
 for name in ('wtframe','wtmorph','wtbands','additive'):
  text=pathlib.Path(f'tests/golden/{fs}/{name}.tl').read_text();p,m=run(text);assert text==m and 'match=yes block_indep=yes clipped=0' in p.stdout
 g=pathlib.Path(f'tests/golden/{fs}/additive.tl').read_text();authored=drop(g,['wtdata','wthash']).replace('wtbank additive 1 2048 12\n','wtbank additive 1 2048 12\n'+harm)
 p,m=run(authored);assert m==g and 'match=yes' in p.stdout
 p,m=run(authored.replace(harm,harm+re.search(r'^wthash .*\n',g,re.M).group(0)));assert m==g
 g=pathlib.Path(f'tests/golden/{fs}/wtframe.tl').read_text()
 p,m=run('wtuse wtframe 0 wtframe 0\n'+drop(g,['wtuse']));assert m==g
 p,m=run(g.replace('voice 0','wtbank spare 1 32 12\nharmonic spare 0 5 0 0\nvoice 0'),'wthash spare ');assert 'match=yes' in p.stdout
base=free(pathlib.Path('tests/golden/48000/wtframe.tl').read_text());sine=free(pathlib.Path('tests/golden/48000/sine.tl').read_text())
first=re.search(r'^wtdata .*\n',base,re.M).group(0)
wave=re.sub(r'^(patch wtframe 0 .*)$',lambda x:' '.join((x.group(1).split()+['0']*4)[:14]+['1']),base,count=1,flags=re.M)
cases=[('harmonic wtframe 0 1 0 0','mixes harmonic and wtdata'),('wtdata wtframe 0 0 0 5','second sample at BAND 0 FRAME 0 index 0'),
 ('wthash wtframe 0123456789abcdef','second wthash'),('wtbank wtframe 1 32 12','second wtbank'),('wtbank x 0 256 12',"FRAMES '0' is not 1 to 8"),('wtbank x 9 256 12',"FRAMES '9' is not 1 to 8"),
 ('wtbank x 1 100 12','LENGTH 100'),('wtbank x 1 16 12',"LENGTH '16' is not 32 to 2048"),('wtbank x 1 4096 12',"LENGTH '4096' is not 32 to 2048"),('wtbank x 1 256 11','BANDS 11'),('wtbank x 1 256','wtbank takes'),
 ('wtbank x 1 32 12','bank x has no wtdata or harmonic'),('wthash y 0123456789abcdef','names bank y before its wtbank'),
 ('wtdata wtframe 0 0 250 1 2 3 4 5 6 7','pass LENGTH 256'),('wtdata wtframe 12 0 0 1',"BAND '12' is not 0 to 11"),('wtdata wtframe 0 2 0 1','FRAME 2'),
 ('wtdata wtframe 0 0 0 32768',"SAMPLE '32768' is not -32768 to 32767"),('wtdata wtframe 0 0 0 -32769',"SAMPLE '-32769' is not -32768 to 32767"),('wtdata wtframe 0 0 0','wtdata takes'),('wtdata wtframe 0 0 0'+' 1'*17,'wtdata takes'),
 ('wtbank x 1 32 12\nharmonic x 0 0 0 0',"NUMBER '0' is not 1 to 1024"),('wtbank x 1 32 12\nharmonic x 0 1025 0 0',"NUMBER '1025' is not 1 to 1024"),('wtbank x 1 32 12\nharmonic x 0 1 1201 0',"LEVEL_DB10 '1201' is not 0 to 1200"),
 ('wtbank x 1 32 12\nharmonic x 0 1 0 65536',"PHASE_U16 '65536' is not 0 to 65535"),('wtbank x 1 32 12\nharmonic x 1 1 0 0','FRAME 1'),('wtbank x 1 32 12\nharmonic x 0 1 0 0\nharmonic x 0 1 60 0','second harmonic FRAME 0 NUMBER 1'),
 ('wtbank x 1 32 12\nharmonic x 0 1 0 0\nwtdata x 0 0 0 1','mixes harmonic and wtdata'),('wtbank x 1 32 12\nharmonic x 0 1 0 0\nwthash x 0123456789abcdef','does not match its content'),
 ('wtuse wtframe 2 wtframe 0','which the patch lacks'),('wtuse wtframe 0 wtframe 0','second wtuse statement for operator 0'),
 ('wtuse wtframe 1 wtframe 32769',"POSITION_Q15 '32769' is not 0 to 32768"),('wtuse wtframe 4 wtframe 0',"OP '4' is not 0 to 3"),('wtuse wtframe 0 wtframe','wtuse takes'),('pd wtframe 0 16384','has both pd and wtuse'),
 (''.join(f'wtbank b{i} 1 32 12\nharmonic b{i} 0 1 0 0\n' for i in range(8)).rstrip('\n'),'more than 8 banks'),
 ('mod wtframe 1 ctl1 wt_pos1 1 0','needs an enabled wavetable'),('mod wtframe 1 ctl1 wt_pos2 1 0','absent operator'),('mod wtframe 1 ctl1 wt_pos4 1 0','unavailable'),
 ('mod wtframe 1 ctl1 wt_pos0 32769 0','AMOUNT'),('mod wtframe 1 ctl1 wt_pos0 -32769 0','AMOUNT')]
for line,needle in cases:run(base+line+'\n',needle,True)
run(base.replace(first,''),'lacks the sample at BAND 0 FRAME 0 index 0',True)
run(drop(base,['wthash']),'wtdata without a wthash',True)
run(wave,'needs sine WAVE 0',True)
run(drop(base,['wtuse'])+'wtuse wtframe 0 nobank 0\n','names no bank nobank',True)
run(sine+'mod sine 0 ctl0 wt_pos0 1 0\n','needs an enabled wavetable',True)
# The hash guards the samples, and the samples are used: one changed sample
# of band 6, the band a 220 Hz note reads at 48 kHz, is refused; with its
# content hash it plays, to another audio hash.
line=re.search(r'^wtdata wtframe 6 0 0 .*\n',base,re.M).group(0);t=line.split();t[6]=str(int(t[6])+8000);edit=' '.join(t)+'\n'
changed=base.replace(line,edit);assert changed!=base
src=tmp/'in.tl';src.write_text(changed);p=subprocess.run([h,'--play',src],text=True,capture_output=True)
assert p.returncode==1 and 'does not match its content' in p.stderr,(p.returncode,p.stderr);count+=1
content=re.search(r'its content ([0-9a-f]{16})',p.stderr).group(1);golden=pathlib.Path('tests/golden/48000/wtframe.tl').read_text()
src.write_text(golden.replace(line,edit).replace(re.search(r'^wthash .*$',golden,re.M).group(0),'wthash wtframe '+content))
p=subprocess.run([h,'--play',src],text=True,capture_output=True);assert p.returncode==3 and 'match=NO' in p.stdout,(p.returncode,p.stdout,p.stderr);count+=1
print(f'WTSESSION checks={count} failures=0')
PY
    then ok "wavetable session $b" "$(tail -n 1 "$tmp/wt-session.log")"
    else fail "wavetable session $b" "canonical replay, harmonic authoring, refusal or hash"; tail -n 12 "$tmp/wt-session.log" | indent; fi
done

# Karplus-Strong statements. Canonical replay, seed 0 written as its
# canonical word, refusals of what the loop cannot take (PD, a wavetable,
# feedback, modulation in, sync out), PM out accepted, and the statement's
# use: without it the record plays to exit 3.
for b in $builds; do
    if python3 -B - "build/$b/harness" "$tmp/ks-session" > "$tmp/ks-session.log" 2>&1 <<'PY'
import pathlib,subprocess,re,sys
h=pathlib.Path(sys.argv[1]).resolve();tmp=pathlib.Path(sys.argv[2]);tmp.mkdir(exist_ok=True);count=0
def run(text,needle=None,fail=False):
 global count
 src=tmp/'in.tl';merged=tmp/'out.tl';src.write_text(text);p=subprocess.run([h,'--play',src,'--merged',merged],text=True,capture_output=True)
 assert p.returncode==(1 if fail else 0),(p.returncode,p.stdout,p.stderr,text[:400])
 if needle:assert needle in (p.stderr if fail else merged.read_text()),(needle,p.stderr,p.stdout)
 count+=1;return p,merged.read_text() if not fail else ''
def free(s):return re.sub(r'^hash .*\n','',s,flags=re.M)
def field(s,op,i,v):
 def put(x):t=x.group(1).split();t+=['0']*(i+1-len(t));t[i]=v;return ' '.join(t)
 return re.sub(r'^(patch kspluck %d .*)$'%op,put,s,count=1,flags=re.M)
for fs in (48000,44100):
 for name in ('kspluck','ksbend'):
  text=pathlib.Path(f'tests/golden/{fs}/{name}.tl').read_text();p,m=run(text);assert text==m and 'match=yes block_indep=yes clipped=0' in p.stdout
base=free(pathlib.Path('tests/golden/48000/kspluck.tl').read_text());line='ksource kspluck 0 32700 7 2048\n';assert line in base
# Seed 0 is written as the canonical word 0x9E3779B9 and plays as it does.
p,zero=run(base.replace(line,'ksource kspluck 0 32700 0 2048\n'),'ksource kspluck 0 32700 2654435769 2048\n')
p,word=run(base.replace(line,'ksource kspluck 0 32700 2654435769 2048\n'));assert zero==word
p,m=run(zero);assert m==zero and 'match=yes' in p.stdout
wt=free(pathlib.Path('tests/golden/48000/wtframe.tl').read_text())
cases=[('ksource kspluck 4 32700 7 2048',"OP '4' is not 0 to 3"),('ksource kspluck 1 32768 7 2048',"LOSS_Q15 '32768' is not 0 to 32767"),('ksource kspluck 1 32700 4294967296 2048',"SEED '4294967296' is not 0 to 4294967295"),
 ('ksource kspluck 1 32700 7 100','BUFFER_SAMPLES 100'),('ksource kspluck 1 32700 7 32',"BUFFER_SAMPLES '32' is not 64 to 8192"),('ksource kspluck 1 32700 7 16384',"BUFFER_SAMPLES '16384' is not 64 to 8192"),
 ('ksource kspluck 0 32700 7','ksource takes'),('ksource kspluck 0 32700 7 2048 1','ksource takes'),
 ('ksource kspluck 0 32700 7 2048','second ksource statement for operator 0'),('ksource kspluck 2 32700 7 2048','which the patch lacks'),
 ('pd kspluck 0 16384','has both pd and ksource'),('route kspluck 2 1 1>0:256','takes no route edge'),('route kspluck 2 1 1>0:128:am','takes no route edge'),
 ('route kspluck 2 3 0>1:256:sync','drives no sync')]
for extra,needle in cases:run(base+extra+'\n',needle,True)
run(field(base,0,14,'1'),'needs sine WAVE 0',True)
run(field(base,0,6,'64'),'takes no FEEDBACK',True)
run(field(base,1,5,'512'),"operator 1's INDEX must be 0",True)
run(wt+'ksource wtframe 0 32700 7 2048\n','has both wtuse and ksource',True)
# Routed, the loop may still modulate another operator (PM out is heard).
p,m=run(field(base,0,5,'256')+'route kspluck 2 3 0>1:256\n','route kspluck 2 3 0>1:256')
# The statement is read and used: without it the record plays a sine (exit 3).
src=tmp/'in.tl';src.write_text(pathlib.Path('tests/golden/48000/kspluck.tl').read_text().replace(line,''))
p=subprocess.run([h,'--play',src],text=True,capture_output=True);assert p.returncode==3 and 'match=NO' in p.stdout,(p.returncode,p.stdout,p.stderr);count+=1
print(f'KSSESSION checks={count} failures=0')
PY
    then ok "karplus-strong session $b" "$(tail -n 1 "$tmp/ks-session.log")"
    else fail "karplus-strong session $b" "canonical replay, seed, refusal or use"; tail -n 12 "$tmp/ks-session.log" | indent; fi
done

# The explicit hour profile is separate; routine checks stream only two seconds.
for b in $builds; do
    if python3 -B - "build/$b/harness" "$tmp/soak-short" > "$tmp/soak-short.log" 2>&1 <<'PY'
import sys,pathlib,subprocess,re
sys.path.insert(0,str(pathlib.Path('tests/generators').resolve()))
import check
h=pathlib.Path(sys.argv[1]).resolve();temp=pathlib.Path(sys.argv[2]);temp.mkdir(exist_ok=True);count=0
for fs in (48000,44100):
 text=pathlib.Path(f'tests/golden/gen/soak/gensoak-{fs}.tl').read_text();src=temp/f'{fs}.tl';src.write_text(check.soak_timeline(text,2))
 result=subprocess.run([h,'--gen-soak',src],check=True,text=True,capture_output=True);rows,_=check.soak_rows(result.stdout,fs,2);count+=1
 played=subprocess.run([h,'--play',src],check=True,text=True,capture_output=True)
 assert f"hash={rows['summary']['hash']} " in played.stdout;count+=1
 for label,changed in [('bound',result.stdout.replace('failures=0','failures=1')),('source',result.stdout.replace('sources=16','sources=15')),('snapshot',result.stdout.replace('match=yes','match=no'))]:
  try:check.soak_rows(changed,fs,2)
  except (ValueError,KeyError):count+=1
  else:raise AssertionError(label)
 for edit in ('no-bank','stereo','effects'):
  bad=text
  if edit=='no-bank':bad=pathlib.Path(f'tests/golden/{fs}/sine.tl').read_text()
  elif edit=='stereo':bad=bad.replace('timeline draft1','timeline draft2\nspace stereo\nblock 64\nnear 100')
  else:bad=bad.replace('streams soak','dcblock 20000\nstreams soak')
  src.write_text(check.soak_timeline(bad,2));p=subprocess.run([h,'--gen-soak',src],text=True,capture_output=True)
  assert p.returncode==1 and '--gen-soak needs a mono generator session without effects' in p.stderr,(edit,p.stderr);count+=1
 # Sparse IDs use the core bank's ID-indexed state while definitions are compact.
 sparse=pathlib.Path(f'tests/golden/gen/genpink-{fs}.tl').read_text()
 sparse=sparse.replace('gen 0 pink','gen 15 pink').replace('gen.0.x','gen.15.x').replace(' gen 0 reset ',' gen 15 reset ')
 src.write_text(check.soak_timeline(sparse,2))
 p=subprocess.run([h,'--gen-soak',src],check=True,text=True,capture_output=True)
 assert 'GENSOAK_SOURCE id=15 kind=2 ' in p.stdout and p.stdout.count('GENSOAK_SOURCE ')==1;count+=1
 played=subprocess.run([h,'--play',src],check=True,text=True,capture_output=True)
 assert re.search(r' hash=([0-9a-f]{16}) ',p.stdout).group(1)==re.search(r' hash=([0-9a-f]{16}) ',played.stdout).group(1);count+=1
print(f'GENSOAK_SHORT checks={count} failures=0')
PY
    then ok "generator soak short $b" "$(tail -n 1 "$tmp/soak-short.log")"
    else fail "generator soak short $b" "streamed audio or refusal control"; tail -n 12 "$tmp/soak-short.log" | indent; fi
done

# Shared oracle requirements: full and LFO modes use the same summaries.
rate_counts() {
    case "$1" in
        48000) delay_checks=335 decay2_checks=475 pink_checks=13772335 mod_checks=1179949 formant_checks=817985 sync_ends=10 ;;
        44100) delay_checks=311 decay2_checks=439 pink_checks=13771943 mod_checks=1084021 formant_checks=817993 sync_ends=7 ;;
        *) return 1 ;;
    esac
}
bounds_ok() {
    local log=$1 fs=$2 phase=$3 required
    local delay_checks decay2_checks pink_checks mod_checks formant_checks sync_ends
    rate_counts "$fs" || return 1
    case "$phase" in 0|1) ;; *) return 1 ;; esac
    while IFS= read -r required; do
        if ! grep -Eq "$required" "$log"; then
            printf 'bound missing or changed: %s\n' "$required"
            return 1
        fi
    done <<EOF
^FORMANTBOUND fs=$fs phase64=$phase checks=$formant_checks failures=0 final_tail=[1-9][0-9]* downstream_ring=[1-9][0-9]* sum_clamps=[1-9][0-9]* Q500_v3_ic1_ic2=[0-9]+,[0-9]+,[0-9]+ Q16000_v3_ic1_ic2=[0-9]+,[0-9]+,[0-9]+$
^VECTORMOVE fs=$fs phase64=$phase checks=114048 failures=0$
^VECTORBOUND fs=$fs phase64=$phase checks=1085269 failures=0 max_ideal_q30=[0-9.]+$
^PDBOUND fs=$fs phase64=$phase checks=1479401 failures=0 max_sine_q30=[0-9.]+$
^GENADAPTER fs=$fs phase64=$phase checks=60632 failures=0$
^GENCACHE fs=$fs phase64=$phase checks=16887 failures=0$
^WIDEBOUND fs=$fs phase64=$phase checks=255 failures=0$
^MODBOUND fs=$fs phase64=$phase checks=870089 failures=0$
^INERTSLOT fs=$fs phase64=$phase cases=13 failures=0$
^MODEVENT fs=$fs phase64=$phase checks=90 failures=0$
^MODPARTITION fs=$fs phase64=$phase checks=$mod_checks failures=0 blocks=1,7,64,257,seeded$
^MODPRECISION fs=$fs phase64=$phase cap=256 checks=7700 old_failures=[0-9]+ outside_refusals=3850 maximum=[0-9.]+ failures=0$
^MODNOTE checks=3108 differences=5 outside_refusals=2072 fails=0$
^CASCADEBOUND fs=$fs phase64=$phase checks=3407237 failures=0$
^SHAPEBOUND fs=$fs phase64=$phase checks=2969450 failures=0 joins=1821 exact_joins=156 ring=[1-9][0-9]* asym=58427 pre_ring=[1-9][0-9]* max_gain_q24=[0-9.]+ max_cubic_q30=[0-9.]+$
^AMBOUND fs=$fs phase64=$phase checks=443269 failures=0 max_ideal_q30=[0-9.]+$
^SYNCBOUND fs=$fs phase64=$phase checks=1299489 failures=0 ends=$sync_ends$
^BINDBOUND fs=$fs phase64=$phase checks=183 failures=0$
^WTBOUND fs=$fs phase64=$phase checks=165 failures=0 assets=10b620b2942bd596 gen_err_lsb=[0-9.]+ render_err_lsb=[0-9.]+$
^KSBOUND fs=$fs phase64=$phase checks=46 failures=0 interior=[0-9]+ clamped=[0-9]+ max_tune=[^ ]+ max_cents=[0-9.]+ residual_q30=[0-9]+$
^JUMPBOUND fs=$fs phase64=$phase exp_bits=10 checks=24748 failures=0$
^COLOURBOUND fs=$fs phase64=$phase checks=2503274 failures=0$
^PINKBOUND fs=$fs phase64=$phase checks=$pink_checks failures=0$
^WAVEBOUND fs=$fs phase64=$phase sine_bits=12 checks=37626 failures=0$
^KSLEFTBOUND fs=$fs phase64=$phase checks=23325 changed=120 failures=0$
^KSFBBOUND fs=$fs phase64=$phase checks=21087 failures=0 boosted=1280 saturated=376 levels=2816 identity=8000 models=576$
^DECAY2BOUND fs=$fs phase64=$phase env64=1 checks=$decay2_checks failures=0$
^DELAYBOUND fs=$fs phase64=$phase checks=$delay_checks failures=0$
^FREEBOUND fs=$fs phase64=$phase checks=420 failures=0$
EOF
}

# ,  and  on the golden cases; the builds agree line for line
for fs in 48000 44100; do
    for b in $builds; do
        out=$tmp/gate.$b.$fs
        build/$b/harness --fs $fs --check > "$out" 2>&1
        rc=$?
        # These three full-only summaries exercise complete cases or adoption
        # checks that --check lfo does not run: vector corners, EG adoption,
        # and saturated routed carrier summation.
        if [ $rc -eq 0 ] && [ "$(tail -n 1 "$out")" = "GATE ok" ] && [ "$(grep -c '^RESULT' "$out")" = 108 ] \
                && bounds_ok "$out" "$fs" 1 \
                && grep -Eq "^VECTORCORNERS phase=1 fs=$fs hash=[0-9a-f]{16} loss_db=[0-9.]+ peak_lsb=[0-9.]+ clipped=0 corner_diff=0$" "$out" \
                && grep -Eq "^EGADOPT fs=$fs adopted=3 count_fail=0 loss_db=[0-9.]+ peak_err_lsb=[0-9.]+ failures=0$" "$out" \
                && grep -Eq "^HOSTSKIP fs=$fs cases=109 diffs=0 state_diffs=0 failures=0$" "$out" \
                && grep -Eq '^ROUTE-SAT routeclp .*positive=[1-9][0-9]* negative=[1-9][0-9]* pcm_clips=0 clamp_fail=0$' "$out"; then
            ok "gate $fs $b" "$(grep -c '^RESULT' "$out") cases"
        else
            fail "gate $fs $b" "exit $rc"
            grep -v -e '^RESULT' -e '^CONFIG' "$out" | indent
        fi
    done
    grep '^RESULT' "$tmp/gate.s12e10p1v1.$fs" > "$tmp/result.$fs"
    for b in s12e10p1v1-portable s12e10p1v1-ubsan; do
        if [ -s "$tmp/result.$fs" ] && grep '^RESULT' "$tmp/gate.$b.$fs" | cmp -s - "$tmp/result.$fs"; then
            ok "same RESULT $fs $b"
        else
            fail "same RESULT $fs $b" "differs from s12e10p1v1"
        fi
    done
done

# , , ,  and : four LFO, phase, two FREE2, nine wave, vectorxy, two wavetable and two Karplus-Strong cases meet the
# same criteria at either phase width. AM/ring and sync bounds also run at both widths. Earlier LFO cases stay in the full gate;
# their existing 32-bit phase precision is outside 's loss bound.
lfo_knobs="SINE_BITS=12 EXP_BITS=10 PHASE64=0 ENV64=1"
lfo_builds="s12e10p0v1-lfo s12e10p0v1-lfo-portable s12e10p0v1-lfo-ubsan"
build s12e10p0v1-lfo $lfo_knobs VARIANT=-lfo || finish
build s12e10p0v1-lfo-portable $lfo_knobs EXTRA=-DFP_MATH_FORCE_PORTABLE VARIANT=-lfo-portable || finish
build s12e10p0v1-lfo-ubsan $lfo_knobs OPT="-O1 -g -fsanitize=undefined -fno-sanitize-recover=all" VARIANT=-lfo-ubsan || finish
for fs in 48000 44100; do
    for b in $builds $lfo_builds; do
        out=$tmp/lfo.$b.$fs
        build/$b/harness --fs $fs --check lfo > "$out" 2>&1
        rc=$?
        case "$b" in s12e10p0*) phase=0 ;; *) phase=1 ;; esac
        if [ $rc -eq 0 ] && [ "$(tail -n 1 "$out")" = "CHECK lfo ok" ] && ! grep -qx 'GATE ok' "$out" && [ "$(grep -c '^RESULT' "$out")" = 21 ] \
                && bounds_ok "$out" "$fs" "$phase"; then
            ok "lfo subset $fs $b" "$(grep -c '^RESULT' "$out") cases"
        else
            fail "lfo subset $fs $b" "exit $rc"
            grep -v -e '^RESULT' -e '^CONFIG' "$out" | indent
        fi
    done
    grep '^RESULT' "$tmp/lfo.s12e10p0v1-lfo.$fs" > "$tmp/lfo.result.$fs"
    for b in s12e10p0v1-lfo-portable s12e10p0v1-lfo-ubsan; do
        if [ -s "$tmp/lfo.result.$fs" ] && grep '^RESULT' "$tmp/lfo.$b.$fs" | cmp -s - "$tmp/lfo.result.$fs"; then
            ok "same lfo RESULT $fs $b"
        else
            fail "same lfo RESULT $fs $b" "differs from s12e10p0v1-lfo"
        fi
    done
done
build/s12e10p0v1-lfo/harness --check > "$tmp/lfo.full.log" 2>&1
if [ $? -eq 2 ] && grep -q 'PHASE64=1' "$tmp/lfo.full.log"; then
    ok "full gate refuses phase32" "exit 2"
else
    fail "full gate refuses phase32" "--check accepted diagnostic build"
    indent < "$tmp/lfo.full.log"
fi

# raw peg clamps, zero-time release and the wider pitch base.
# The full phase64 --check above also runs these checks.
for fs in 48000 44100; do
    for b in $lfo_builds; do
        build/$b/harness --fs $fs --check peg-bounds > "$tmp/peg.bounds.log" 2>&1
        rc=$?
        if [ $rc -eq 0 ] && [ "$(tail -n 1 "$tmp/peg.bounds.log")" = "CHECK peg-bounds ok" ] && ! grep -qx 'GATE ok' "$tmp/peg.bounds.log"; then
            ok "peg bounds $fs $b"
        else fail "peg bounds $fs $b" "exit $rc"; indent < "$tmp/peg.bounds.log"; fi
    done
done

# raw filter-envelope clamps, track oracle and normalized bounds.
# Full phase64 --check includes this; exercise the other phase width too.
for fs in 48000 44100; do
    for b in $lfo_builds; do
        build/$b/harness --fs $fs --check fenv-bounds > "$tmp/fenv.bounds.log" 2>&1
        rc=$?
        if [ $rc -eq 0 ] && [ "$(tail -n 1 "$tmp/fenv.bounds.log")" = "CHECK fenv-bounds ok" ] && ! grep -qx 'GATE ok' "$tmp/fenv.bounds.log"; then
            ok "fenv bounds $fs $b"
        else fail "fenv bounds $fs $b" "exit $rc"; indent < "$tmp/fenv.bounds.log"; fi
    done
done

# retired standalone spellings are unknown options, never aliases.
for subset in lfo peg fenv; do
    suffix=bounds
    [ "$subset" != lfo ] || suffix=check
    "$main" "--$subset-$suffix" > "$tmp/check.old.log" 2>&1
    rc=$?
    if [ $rc -eq 2 ] && grep -q '^usage: harness ' "$tmp/check.old.log"; then
        ok "retired $subset option" "refused, exit 2"
    else fail "retired $subset option" "exit $rc"; indent < "$tmp/check.old.log"; fi
done

# named subsets, the full gate and emission require the approved clock.
# EXTRA already carries build defines; this isolated UBSan build uses 37.
if build s12e10p1v1-ctl37 $knobs EXTRA=-DFM_CTL=37 OPT="-O1 -g -fsanitize=undefined -fno-sanitize-recover=all" VARIANT=-ctl37; then
    clock_h=build/s12e10p1v1-ctl37/harness
    bad=
    for fs in 48000 44100; do
        emit_test="$tmp/clock-emit-$fs"
        if ! mkdir "$emit_test"; then bad="$bad $fs:mkdir"; continue; fi
        "$clock_h" --fs "$fs" --emit "$emit_test" > "$tmp/clock.emit.log" 2>&1
        rc=$?
        contents=$(ls -A "$emit_test")
        list_rc=$?
        [ "$rc" -eq 2 ] && [ "$list_rc" -eq 0 ] && [ -z "$contents" ] && \
            grep -qx -- '--emit needs FM_CTL=64; this build has FM_CTL=37' "$tmp/clock.emit.log" || bad="$bad $fs:$rc:$list_rc"
    done
    if [ -z "$bad" ]; then ok "clock refuses emit" "both rates, exit 2, directories empty"
    else fail "clock refuses emit" "$bad"; indent < "$tmp/clock.emit.log"; fi
    for subset in full lfo peg-bounds fenv-bounds; do
        bad=
        for fs in 48000 44100; do
            if [ "$subset" = full ]; then "$clock_h" --fs "$fs" --check > "$tmp/clock.log" 2>&1
            else "$clock_h" --fs "$fs" --check "$subset" > "$tmp/clock.log" 2>&1; fi
            rc=$?
            [ $rc -eq 2 ] && grep -q 'FM_CTL=64; this build has FM_CTL=37' "$tmp/clock.log" || bad="$bad $fs:$rc"
        done
        if [ -z "$bad" ]; then ok "clock refuses $subset check" "both rates, exit 2"
        else fail "clock refuses $subset check" "$bad"; indent < "$tmp/clock.log"; fi
    done
    for source in lfo peg fenv mod; do
        case $source in
            lfo) spec='lfo bell 0 6000 0 0 0 0 0' ;;
            mod) spec='mod bell 0 ctl0 pitch0 0' ;;
            peg) spec='peg bell 0 0 0 0 0 0 0 0' ;;
            fenv) spec='filter bell lp 300000 1500;fenv bell 0 0 0 0 0 0' ;;
        esac
        bad=
        for fs in 48000 44100; do
            awk -v r="$spec" '/^voice /{ k = split(r, a, ";"); for (i = 1; i <= k; i++) print a[i] } { print }' \
                tests/golden/$fs/bell.tl > "$tmp/clock.tl"
            "$clock_h" --play "$tmp/clock.tl" > "$tmp/clock.log" 2>&1
            rc=$?
            [ $rc -eq 1 ] && grep -q "$source statement needs FM_CTL=64; this build has FM_CTL=37" "$tmp/clock.log" || bad="$bad $fs:$rc"
        done
        if [ -z "$bad" ]; then ok "clock refuses $source record" "both rates, exit 1, source and clock named"
        else fail "clock refuses $source record" "$bad"; indent < "$tmp/clock.log"; fi
    done
    bad=
    for fs in 48000 44100; do
        "$clock_h" --play tests/golden/$fs/bell.tl > "$tmp/clock.log" 2>&1
        rc=$?
        [ $rc -eq 0 ] && grep -q 'match=yes' "$tmp/clock.log" || bad="$bad $fs:$rc"
    done
    if [ -z "$bad" ]; then ok "clock permits plain record" "bell hashes unchanged at both rates"
    else fail "clock permits plain record" "$bad"; indent < "$tmp/clock.log"; fi
fi

#  controller events retain the same record clock contract without slots.
if [ -x "${clock_h-}" ]; then
    awk '/^0 golden / { print "0 golden 0 ctl 0 32768" } { print }' tests/golden/48000/bell.tl > "$tmp/clock-ctl.tl"
    "$clock_h" --play "$tmp/clock-ctl.tl" > "$tmp/clock-ctl.log" 2>&1
    rc=$?
    if [ $rc -eq 1 ] && grep -q 'ctl event needs FM_CTL=64; this build has FM_CTL=37' "$tmp/clock-ctl.log"; then
        ok "clock refuses ctl event" "exit 1, event and clock named"
    else fail "clock refuses ctl event" "exit $rc"; indent < "$tmp/clock-ctl.log"; fi
fi

# widened rate*FM_CTL < 500*fs, independently of header order.
# At the approved sample rates the absolute 50000 mHz bound binds first;
# below/at/above control Nyquist still have distinct refusal messages.
for b in $builds; do
    bad= n=0
    while read fs rate expected; do
        for late in 0 1; do
            n=$((n + 1))
            awk -v fs="$fs" -v rate="$rate" -v late="$late" '
                /^fs / { if (!late) print "fs " fs; next }
                /^length_ns / { print "length_ns " (fs == 1 ? 1000000000 : fs == 4294967295 ? 1 : 1000000); next }
                /^hash / || /^[0-9]/ { next }
                /^voice / { print "lfo bell 0 " rate " 0 0 0 0 0"; if (late) print "fs " fs }
                { print }
                END { print "0 golden 0 on 523250 32767" }
            ' tests/golden/48000/bell.tl > "$tmp/clock.rate.tl"
            build/$b/harness --play "$tmp/clock.rate.tl" > "$tmp/clock.rate.log" 2>&1
            rc=$?
            case $expected in
                accept) [ $rc -eq 0 ] || bad="$bad $fs/$rate/$late:$rc" ;;
                range) [ $rc -eq 1 ] && grep -q "RATE_MILLIHZ '$rate' is not 10 to 50000" "$tmp/clock.rate.log" || bad="$bad $fs/$rate/$late:$rc" ;;
                nyquist) [ $rc -eq 1 ] && grep -q "RATE_MILLIHZ $rate reaches the control Nyquist at fs $fs with FM_CTL=64" "$tmp/clock.rate.log" || bad="$bad $fs/$rate/$late:$rc" ;;
            esac
        done
    done <<'RATES'
48000 50000 accept
48000 374999 range
48000 375000 nyquist
48000 375001 nyquist
44100 50000 accept
44100 344530 range
44100 344531 range
44100 344532 nyquist
3200 24999 accept
3200 25000 nyquist
3200 25001 nyquist
6400 49999 accept
6400 50000 nyquist
6401 50000 accept
1 10 nyquist
48000 4294967295 nyquist
4294967295 50000 accept
4294967295 4294967295 range
RATES
    if [ -z "$bad" ]; then ok "lfo clock rate $b" "$n records: bounds, messages, header order and wide products"
    else fail "lfo clock rate $b" "$bad"; indent < "$tmp/clock.rate.log"; fi
done

# The cases as timelines: --emit writes the committed records
for fs in 48000 44100; do
    for b in $builds; do
        d=$tmp/emit.$b.$fs
        mkdir "$d"
        build/$b/harness --fs $fs --emit "$d" > "$tmp/emit.log" 2>&1
        rc=$?
        if [ $rc -ne 0 ]; then
            fail "emit $fs $b" "exit $rc"
            tail -n 5 "$tmp/emit.log" | indent
        elif diff -r "tests/golden/$fs" "$d" > "$tmp/diff.log" 2>&1; then
            ok "emit $fs $b" "= tests/golden/$fs"
        else
            fail "emit $fs $b" "differs from tests/golden/$fs"
            head -n 20 "$tmp/diff.log" | indent
        fi
    done
done

# Each record replays to its hash (exit 3 otherwise), with its integer
# METRICS line (PLAN  step 1), which the three builds must print
# alike
for b in $builds; do
    : > "$tmp/metrics.$b"
    t=0
    for dir in tests/golden/48000 tests/golden/44100 tests/golden/vector tests/golden/gen tests/golden/edge tests/golden/space tests/golden/fx; do
        n=0 bad=
        for f in "$dir"/*.tl; do
            n=$((n + 1)) t=$((t + 1))
            build/$b/harness --play "$f" --metrics > "$tmp/play.log" 2>&1 || bad="$bad ${f##*/}"
            awk -v f="$f" '/^METRICS /{ print f, $0 }' "$tmp/play.log" >> "$tmp/metrics.$b"
        done
        if [ -z "$bad" ]; then ok "replay $dir $b" "$n records"
        else fail "replay $dir $b" "differs:$bad"; fi
    done
done
bad=
for b in $builds; do cmp -s "$tmp/metrics.s12e10p1v1" "$tmp/metrics.$b" || bad="$bad $b"; done
m=$(grep -c '^tests/golden/.* METRICS peak=[0-9]* rms=[0-9]*\.[0-9][0-9][0-9] crest=' "$tmp/metrics.s12e10p1v1")
if [ -z "$bad" ] && [ "$m" -eq "$t" ]; then ok "play metrics" "$t records, the same on the 3 builds"
else fail "play metrics" "$m of $t lines; differs from s12e10p1v1:$bad"; fi

# F-9: fixed and seeded call partitions across stereo, fields and effects.
for chunk in 7 65 128 257 480 random; do
    bad= n=0
    for f in tests/golden/space/*.tl tests/golden/space/binaural/*.tl tests/golden/fx/*.tl; do
        n=$((n + 1))
        "$main" --play "$f" --chunk "$chunk" > "$tmp/chunk.log" 2>&1
        rc=$?
        if [ "$rc" -ne 0 ] || ! grep -q '^PLAY .*match=yes block_indep=yes ' "$tmp/chunk.log"; then
            bad="$bad $f:$rc"
        fi
    done
    if [ -z "$bad" ]; then ok "play chunk $chunk" "$n records"
    else fail "play chunk $chunk" "differs:$bad"; fi
done

# bell and retrig through a unity route replay to their committed
# hashes, the routed path being the fixed one bit for bit; weight 255
# changes the hash (exit 3), so the route line is read and used
for b in $builds; do
    bad=
    for fs in 48000 44100; do
        for p in bell retrig; do
            for w in 256 255; do
                f=$tmp/route.$p.$fs.$w.tl
                awk -v r="route $p 2 1 1>0:$w" '/^voice /{ print r } { print }' "tests/golden/$fs/$p.tl" > "$f"
                build/$b/harness --play "$f" > "$tmp/route.log" 2>&1
                rc=$?
                if [ $w = 256 ]; then want=0; else want=3; fi
                grep -q "^route $p 2 1 1>0:$w\$" "$f" && [ $rc -eq $want ] || bad="$bad $p.$fs.$w:$rc"
            done
        done
    done
    if [ -z "$bad" ]; then ok "route unity $b" "bell retrig at 48000 44100: hash; weight 255: exit 3"
    else fail "route unity $b" "differs:$bad"; fi
done

# an explicit PM suffix normalizes away in every routed record;
# AM/ring suffixes remain. Old all-PM canonical records stay byte exact.
for b in $builds; do
    h=build/$b/harness
    bad= n=0
    for fs in 48000 44100; do
        for src in tests/golden/$fs/*.tl; do
            grep -q '^route ' "$src" || continue
            n=$((n + 1))
            awk '/^route /{ for (i=5;i<=NF;i++) if (split($i,a,":") == 2) $i=$i ":pm" } { print }' "$src" > "$tmp/pm.tl"
            "$h" --play "$tmp/pm.tl" --merged "$tmp/pm-merged.tl" > "$tmp/pm.log" 2>&1
            rc=$?
            if [ $rc -ne 0 ] || ! grep -q 'match=yes block_indep=yes' "$tmp/pm.log" || ! cmp -s "$src" "$tmp/pm-merged.tl"; then
                bad="$bad $src:$rc"
            fi
        done
    done
    if [ -z "$bad" ]; then ok "PM suffix $b" "$n routed records: canonical and hash"
    else fail "PM suffix $b" "differs:$bad"; fi
done

#  and : each tests/golden/48000 and tests/golden/44100 record with every
# patch line padded with zeros to 16 values (RS, FIXED_MILLIHZ, PHASE_U16
# and FREE default to 0; existing extras preserved) replays to its hash, the extras at zero being
# the path without them bit for bit; bell with RS 1 does not (exit 3), so
# the padded values are read and used
for b in $builds; do
    n=0 bad=
    for fs in 48000 44100; do
        for f in tests/golden/$fs/*.tl; do
            n=$((n + 1))
            g=$tmp/zero.$fs.${f##*/}
            awk '/^patch /{ while (NF < 19) $(NF + 1) = 0 } { print }' "$f" > "$g"
            build/$b/harness --play "$g" > "$tmp/zero.log" 2>&1
            rc=$?
            awk '/^patch / && NF != 19 { e = 1 } END { exit e }' "$g" && [ $rc -eq 0 ] || bad="$bad ${f##*/}.$fs:$rc"
        done
        g=$tmp/zero.$fs.rs1.tl
        awk '/^patch bell 1 /{ while (NF < 15) $(NF + 1) = 0; $16 = 1 } { print }' "tests/golden/$fs/bell.tl" > "$g"
        build/$b/harness --play "$g" > "$tmp/zero.log" 2>&1
        rc=$?
        grep -q '^patch bell 1 .* 0 0 0 1$' "$g" && [ $rc -eq 3 ] || bad="$bad bell.rs1.$fs:$rc"
    done
    if [ -z "$bad" ]; then ok "zero extras $b" "$n records padded to 16 values: hash; bell RS 1: exit 3"
    else fail "zero extras $b" "differs:$bad"; fi
done

#H13: missing stream files are a diagnostic, never an exit-code change.
"$main" --play tests/fixtures/split-header.tl > "$tmp/piece-hint.out" 2> "$tmp/piece-hint.err"
hint_rc=$?
if [ "$hint_rc" -eq 3 ] &&
   grep -Eq '^harness: [0-9]+ streams declared and no events: a piece is a directory; pass DIR/session.tl and DIR/\*.tl \(tools/music/play.sh\)$' "$tmp/piece-hint.err" &&
   "$main" --play tests/golden/gen/gencoprime-48000.tl > "$tmp/piece-gen.out" 2> "$tmp/piece-gen.err" &&
   grep -q '^PLAY .* match=yes ' "$tmp/piece-gen.out" && [ ! -s "$tmp/piece-gen.err" ]; then
    ok "piece file hint" "session alone exits3 with hint; generated lanes match without hint"
else
    fail "piece file hint" "missing diagnostic, changed status or false generator hint"
fi

#H14: file/field modes must refuse options they do not consume.
bad= n=0
for mode in --play --fxref --gen-events --gen-soak --gen-params --field-test --field-cost; do
    for flag in --fs --out; do
        value=44100
        [ "$flag" != --out ] || value=$tmp/unused-output
        unused_args=("$mode" "$flag" "$value")
        case "$mode" in
            --play|--fxref|--gen-events|--gen-soak) unused_args+=(tests/golden/48000/abs.tl) ;;
        esac
        "$main" "${unused_args[@]}" > "$tmp/unused-option.out" 2> "$tmp/unused-option.err"
        rc=$?
        n=$((n + 1))
        if [ "$rc" -ne 2 ] || [ -s "$tmp/unused-option.out" ] ||
           ! grep -q '^usage: harness ' "$tmp/unused-option.err"; then
            bad="$bad $mode/$flag:$rc"
        fi
    done
done
if [ -z "$bad" ] && [ ! -e "$tmp/unused-output" ]; then ok "unused CLI options" "$n refusals before I/O"
else fail "unused CLI options" "differs:$bad"; fi

#H15: a terminator permits a leading-dash filename, without weakening
# ordinary option parsing.
cp tests/golden/48000/sine.tl "$tmp/-x.tl"
term_copy_rc=$?
(cd "$tmp" && "$here/../$main" --play -- -x.tl) > "$tmp/terminator.out" 2> "$tmp/terminator.err"
term_rc=$?
(cd "$tmp" && "$here/../$main" --play -x.tl) > "$tmp/no-terminator.out" 2> "$tmp/no-terminator.err"
plain_rc=$?
if [ "$term_copy_rc" -eq 0 ] && [ "$term_rc" -eq 0 ] &&
   grep -q '^PLAY .* match=yes block_indep=yes ' "$tmp/terminator.out" &&
   [ ! -s "$tmp/terminator.err" ] && [ "$plain_rc" -eq 2 ] &&
   [ ! -s "$tmp/no-terminator.out" ] && grep -q '^usage: harness ' "$tmp/no-terminator.err"; then
    ok "CLI terminator" "leading-dash record matches with --; refused without"
else
    fail "CLI terminator" "copy:$term_copy_rc with --:$term_rc without:$plain_rc"
fi

# Historical hashes and single-file playback are release contracts. Authored
# drafts are inspected separately; they cannot replace hash-pinned records.
for b in $builds; do
    if python3 -B tests/play_once.py --harness "build/$b/harness" > "$tmp/listener.$b.log" 2>&1; then
        ok "listener $b" "$(tail -n 1 "$tmp/listener.$b.log")"
    else
        fail "listener $b" "single-render/verification contract"
        indent < "$tmp/listener.$b.log"
    fi
    if python3 -B tests/music_compat.py --harness "build/$b/harness" --jobs "${CHECK_JOBS:-2}" > "$tmp/music.$b.log" 2>&1; then
        ok "music $b" "$(tail -n 1 "$tmp/music.$b.log")"
    else
        fail "music $b" "historical hash, canonical bytes or independent replay"
        indent < "$tmp/music.$b.log"
    fi
done

# Host tool self-tests (/H10), once after the music builds.
tools_check() {
    local name=$1 pattern=$2
    shift 2
    if python3 -B "$@" > "$tmp/tools-$name.log" 2>&1 &&
       grep -Eq "$pattern" "$tmp/tools-$name.log"; then
        ok "tools $name" "$(tail -n 1 "$tmp/tools-$name.log")"
    else
        fail "tools $name" "self-test failed or incomplete"
        indent < "$tmp/tools-$name.log"
    fi
}
if command -v python3 >/dev/null 2>&1; then
    tools_check perf '^selftest-perf: [0-9]+ checks ok$' tools/music/compose.py --selftest-perf
    tools_check recipes '^selftest-recipes: [0-9]+ checks ok$' tools/music/compose.py --selftest-recipes
    tools_check midi '^selftest: [0-9]+ checks passed$' tools/music/midi2tl.py --selftest
    tools_check dx7 '^PASS: [0-9]+ checks' tools/music/dx7_import.py --selftest
    tools_check dx7-invariants '^PASS: 0 failures$' tools/music/checks/dx7_invariants.py
else
    for name in perf recipes midi dx7 dx7-invariants; do
        skip "tools $name" "python3 not found"
    done
fi
# Input bounds , on the UBSan build
h=build/s12e10p1v1-ubsan/harness
bad=
for fs in 0 4294967296 -1 48k 4294967295 795364315; do
    "$h" --fs $fs > "$tmp/fs.log" 2>&1
    rc=$?
    [ $rc -eq 2 ] || bad="$bad $fs:$rc"
done
"$h" --fs > "$tmp/fs.log" 2>&1
rc=$?
[ $rc -eq 2 ] || bad="$bad none:$rc"
if [ -z "$bad" ]; then ok "fs refused" "0 2^32 -1 48k 2^32-1 795364315 none: exit 2"
else fail "fs refused" "not exit 2:$bad"; fi
"$h" --fs 1 > "$tmp/fs1.log" 2>&1
rc=$?
if [ $rc -eq 0 ]; then ok "fs 1" "the cases at 1 Hz, UBSan silent"
else fail "fs 1" "exit $rc"; tail -n 5 "$tmp/fs1.log" | indent; fi

#H16: byte diagnostics, including the final line without a newline.
printf 'timeline draft1\000hidden\n' > "$tmp/nul-line.tl"
printf '\357\273\277timeline draft1\n' > "$tmp/bom-line.tl"
{ cat tests/golden/48000/sine.tl; printf '%s\n' '-1 golden 0 off'; } > "$tmp/signed-time.tl"
{ cat tests/golden/48000/sine.tl; printf '# tail\000hidden'; } > "$tmp/nul-final.tl"
bad= n=0
for spec in "nul-line:NUL byte in line" \
            "bom-line:byte-order mark; save as UTF-8 without one" \
            "signed-time:T_NS '-1' is not 0 to 18446744073709551615" \
            "nul-final:NUL byte in line"; do
    "$h" --play "$tmp/${spec%%:*}.tl" > "$tmp/reader-byte.out" 2> "$tmp/reader-byte.err"
    rc=$?
    n=$((n + 1))
    if [ "$rc" -ne 1 ] || [ -s "$tmp/reader-byte.out" ] ||
       ! grep -Fq "${spec#*:}" "$tmp/reader-byte.err"; then
        bad="$bad ${spec%%:*}:$rc"
    fi
done
if [ -z "$bad" ]; then ok "reader byte diagnostics" "$n records: named refusals, exit1"
else fail "reader byte diagnostics" "differs:$bad"; fi

# Spatial records that the reader must refuse.
bad= n=0
for f in tests/golden/space/bad/*.tl; do
    n=$((n + 1))
    "$h" --play "$f" > "$tmp/space.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] || bad="$bad ${f##*/}:$rc"
done
if [ -z "$bad" ]; then ok "space refused" "$n records: exit 1"
else fail "space refused" "not exit 1:$bad"; fi

# Effects parser refusals and double-model comparisons on every build.
bad= n=0
for f in tests/golden/fx/bad/*.tl; do
    # This raw-field fixture is accepted; test its output separately below.
    [ "$f" = tests/golden/fx/bad/space-field.tl ] && continue
    n=$((n + 1))
    "$h" --play "$f" > "$tmp/fx.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] || bad="$bad ${f##*/}:$rc"
done
if [ -z "$bad" ]; then ok "fx refused" "$n records: exit 1"
else fail "fx refused" "not exit 1:$bad"; fi
for b in $builds; do
    n=0 bad=
    for f in tests/golden/fx/*.tl; do
        n=$((n + 1))
        build/$b/harness --fxref "$f" > "$tmp/fxref.log" 2>&1 || bad="$bad ${f##*/}"
    done
    if [ -z "$bad" ]; then ok "fxref $b" "$n records: reference criteria"
    else fail "fxref $b" "fails:$bad"; fi
done

# the formerly refused fixture is now an admitted raw amb1 FX route.
# Preserve its original bytes and require the native independent reference.
for b in $builds; do
    field_extra= field_opt=-O2
    case "$b" in
        *portable) field_extra=-DFP_MATH_FORCE_PORTABLE ;;
        *ubsan) field_opt="-O1 -g -fsanitize=undefined -fno-sanitize-recover=all" ;;
    esac
    if "${CC:-cc}" -std=c99 -pedantic -Wall -Wextra -Wshadow $field_opt $field_extra $defs -Isrc/host -I. -I"build/$b" \
        src/host/field_fx_check.c src/host/field_ref.c src/host/fx_ref.c src/host/ref_double.c \
        "build/$b/fm_a1.o" "build/$b/space.o" "build/$b/fx.o" "build/$b/field.o" \
        -lm -o "$tmp/field-fx-check" > "$tmp/field-fx.log" 2>&1 &&
       "$tmp/field-fx-check" >> "$tmp/field-fx.log" 2>&1 &&
       grep -q '^FIELD_FX ok checks=309178 failures=0 digest=1d6faef634ea298b$' "$tmp/field-fx.log" &&
       "build/$b/harness" --fxref tests/golden/fx/bad/space-field.tl >> "$tmp/field-fx.log" 2>&1; then
        ok "field FX $b" "independent arithmetic/state controls and former policy refusal"
        indent < "$tmp/field-fx.log"
    else fail "field FX $b" "arithmetic/state/reference failed"; indent < "$tmp/field-fx.log"; fi
done

# COMPRESSOR_BEGIN: reusable targeted acceptance block.
# exact table/raw controls, independent duck decomposition and grammar.
if python3 -B tests/compressor/compressor_table.py > "$tmp/compressor-table.log" 2>&1; then
    ok "compressor table" "Decimal70 table and conversion constants"
else fail "compressor table" "independent constants differ"; indent < "$tmp/compressor-table.log"; fi
for b in $builds; do
    comp_extra= comp_opt=-O2
    case "$b" in
        *portable) comp_extra=-DFP_MATH_FORCE_PORTABLE ;;
        *ubsan) comp_opt="-O1 -g -fsanitize=undefined -fno-sanitize-recover=all" ;;
    esac
    if "${CC:-cc}" -std=c99 -pedantic -Wall -Wextra -Wshadow -Wsign-conversion -Werror $comp_opt $comp_extra $defs -Isrc/host -I. \
        tests/compressor/compressor_probe.c "build/$b/fm_a1.o" -lm -o "$tmp/compressor-probe" > "$tmp/compressor.log" 2>&1 &&
       "$tmp/compressor-probe" >> "$tmp/compressor.log" 2>&1 &&
       grep -q '^COMPBOUND checks=19744518 failures=0$' "$tmp/compressor.log" &&
       python3 -B tests/compressor/compressor_text.py "build/$b/harness" >> "$tmp/compressor.log" 2>&1 &&
       grep -q '^COMPTEXT ok checks=56 refusals=36$' "$tmp/compressor.log" &&
       "${CC:-cc}" -std=c99 -pedantic -Wall -Wextra -Wshadow $comp_opt $comp_extra $defs -Isrc/host -I. \
        tests/compressor/duck_measure.c src/host/ref_double.c src/host/field_ref.c \
        "build/$b/gen.o" "build/$b/fm_a1.o" "build/$b/space.o" "build/$b/fx.o" "build/$b/field.o" \
        -lm -o "$tmp/duck-measure" >> "$tmp/compressor.log" 2>&1 &&
       "$tmp/duck-measure" tests/golden/fx/duck.tl >> "$tmp/compressor.log" 2>&1 &&
       "$tmp/duck-measure" tests/golden/fx/duck-44100.tl >> "$tmp/compressor.log" 2>&1 &&
       [ "$(grep -c '^DUCKBASS fs=' "$tmp/compressor.log")" -eq 2 ]; then
        ok "compressor bounds $b" "19744518 checks,56 text controls,paired bass reduction"
        indent < "$tmp/compressor.log"
    else fail "compressor bounds $b" "arithmetic/duck/grammar control failed"; indent < "$tmp/compressor.log"; fi
done
# COMPRESSOR_END

# FLANGER_BEGIN: exact independent line/lifecycle arithmetic and text controls.
for b in $builds; do
    flg_extra= flg_opt=-O2
    case "$b" in
        *portable) flg_extra=-DFP_MATH_FORCE_PORTABLE ;;
        *ubsan) flg_opt="-O1 -g -fsanitize=undefined -fno-sanitize-recover=all" ;;
    esac
    if "${CC:-cc}" -std=c99 -pedantic -Wall -Wextra -Wshadow -Werror $flg_opt $flg_extra $defs -Isrc/host -I. -Isrc/host -Isrc/fx \
        tests/flanger/flanger_probe.c "build/$b/fx.o" "build/$b/fm_a1.o" -o "$tmp/flanger-probe" > "$tmp/flanger.log" 2>&1 &&
       "$tmp/flanger-probe" >> "$tmp/flanger.log" 2>&1 &&
       grep -q '^FLANGERBOUND checks=3426430 failures=0$' "$tmp/flanger.log" &&
       python3 -B tests/flanger/flanger_text.py "build/$b/harness" >> "$tmp/flanger.log" 2>&1 &&
       grep -q '^FLANGERTEXT checks=113 refusals=30 ok$' "$tmp/flanger.log"; then
        ok "flanger bounds $b" "3426430 exact checks,113 text controls"
        indent < "$tmp/flanger.log"
    else fail "flanger bounds $b" "arithmetic/lifecycle/grammar control failed"; indent < "$tmp/flanger.log"; fi
done
# FLANGER_END

# DECODABLE_BEGIN
for b in $builds; do
    if python3 -B tests/profiles/profile_check.py "build/$b/harness" > "$tmp/decodable.log" 2>&1 &&
       grep -q '^DECODABLE checks=245 refusals=88 ok$' "$tmp/decodable.log"; then
        ok "decodable profile $b" "245 controls,88 refusals,all complete readers"
    else fail "decodable profile $b" "profile,atomic refusal or canonical replay failed"; indent < "$tmp/decodable.log"; fi
done
# DECODABLE_END

# 's exact bypass: `gain 0` written into each tests/golden/fx record that
# has no gain statement gives its committed hash, on every build; `gain 1`
# in softclip.tl moves it (exit 3), so the statement is read and used
for b in $builds; do
    n=0 bad=
    for f in tests/golden/fx/*.tl; do
        grep -q '^gain ' "$f" && continue
        n=$((n + 1))
        grep -q '^hash ' "$f" || { bad="$bad ${f##*/}:nohash"; continue; }
        awk '/^hash /{print "gain 0"} {print}' "$f" > "$tmp/gain0.tl"
        build/$b/harness --play "$tmp/gain0.tl" > "$tmp/gain0.log" 2>&1 || bad="$bad ${f##*/}"
    done
    awk '/^hash /{print "gain 1"} {print}' tests/golden/fx/softclip.tl > "$tmp/gain1.tl"
    build/$b/harness --play "$tmp/gain1.tl" > "$tmp/gain1.log" 2>&1
    rc=$?
    [ $rc -eq 3 ] || bad="$bad gain1:$rc"
    if [ -z "$bad" ]; then ok "fx gain 0 $b" "$n records: their hashes; gain 1 exit 3"
    else fail "fx gain 0 $b" "differs:$bad"; fi
done

# --play --ref (PLAN  step 1, ): 's comparison on a record,
# vectorcorners' four voices against their reference outputs summed; a
# record with effects through --fxref's model, its METRICS line per
# channel too; a stereo record without effects refused before rendering;
# 's edge record, whose feedback of 63.999 rad under an index of
# 65535 no reference follows, failing loss and peak (exit 4)
for b in $builds; do
    h=build/$b/harness bad=
    "$h" --play tests/golden/vector/vectorcorners-48000.tl --ref > "$tmp/ref.log" 2>&1
    rc=$?
    { [ $rc -eq 0 ] && grep -q '^REF .* d14=ok$' "$tmp/ref.log"; } || bad="$bad vectorcorners:$rc"
    "$h" --play tests/golden/fx/stereo.tl --ref --metrics > "$tmp/ref.log" 2>&1
    rc=$?
    { [ $rc -eq 0 ] && grep -q '^METRICS .* crest_ch=' "$tmp/ref.log" &&
        [ "$(tail -n 1 "$tmp/ref.log")" = 'FXREF ok' ]; } || bad="$bad stereo:$rc"
    "$h" --play tests/golden/space/pan.tl --ref > "$tmp/ref.log" 2> "$tmp/ref.err"
    rc=$?
    { [ $rc -eq 2 ] && [ ! -s "$tmp/ref.log" ] &&
        grep -q -e '^--ref: no double model covers a stereo record' "$tmp/ref.err"; } || bad="$bad pan:$rc"
    "$h" --play tests/golden/edge/edge-48000.tl --ref > "$tmp/ref.log" 2>&1
    rc=$?
    { [ $rc -eq 4 ] && grep -q '^REF .* d14=FAIL:loss,peak$' "$tmp/ref.log"; } || bad="$bad edge-48000:$rc"
    if [ -z "$bad" ]; then ok "play ref $b" "vectorcorners d14=ok, fx stereo FXREF ok, pan exit 2, edge-48000 exit 4"
    else fail "play ref $b" "not as expected:$bad"; fi
done

# The route statements and operator lines the reader refuses , each
# written before tests/golden/48000/bell.tl's voice line (';' starts a new line)
bad= n=0
for spec in 'patch bell 2 65536 0 0 0 0 5 500 200 800' \
        'route bell 2 1 1>0:256;patch bell 2 65536 0 0 0 0 5 500 200 800' \
        'patch bell 4 65536 0 0 0 0 5 500 200 800' 'route bell 3 1 2>1:256 1>0:256' \
        'route bell' 'route bell 1 1' 'route bell 5 1 1>0:256' \
        'route bell 2 0 1>0:256' 'route bell 2 4 1>0:256' 'route bell 2 1 1-0:256' \
        'route bell 2 1 2>0:256' 'route bell 2 1 1>1:256' 'route bell 2 1 1>0:0' \
        'route bell 2 1 1>0:1025' 'route bell 2 1 1>0:256 1>0:128' \
        'route bell 2 1 1>0:256;route bell 2 1 1>0:256' 'route nobody 2 1 1>0:256' \
        'route bell 2 1 1>0:257:am' 'route bell 2 1 1>0:257:ring' \
        'route bell 2 1 1>0:255:sync' 'route bell 2 1 1>0:257:sync' \
        'route bell 2 1 1>0:1:sync' 'route bell 2 1 1>1:256:sync' \
        'route bell 2 1 1>0:256:SYNC' 'route bell 2 1 2>0:256:sync' \
        'route bell 2 1 1>0:256:AM' \
        'route bell 2 1 1>0:256:' 'route bell 2 1 1>0:256:pm:am' \
        'route bell 2 1 1>0:256:am 1>0:128:ring' 'route bell 2 1 1>1:256:ring' \
        'route bell 2 1 2>0:256:am' 'route bell 2 1 1>0:0:ring' \
        'route bell 2 1 1>0:1025:pm'; do
    n=$((n + 1))
    awk -v r="$spec" '/^voice /{ k = split(r, a, ";"); for (i = 1; i <= k; i++) print a[i] } { print }' \
        tests/golden/48000/bell.tl > "$tmp/route.tl"
    "$h" --play "$tmp/route.tl" > "$tmp/route.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] || bad="$bad [$spec]:$rc"
done
if [ -z "$bad" ]; then ok "route refused" "$n lines: exit 1"
else fail "route refused" "not exit 1:$bad"; fi

# 's RS above 7, 's PHASE_U16 above 65535 and FREE other than
# 0, 1 or 2, and a 17th value, each appended to tests/golden/48000/bell.tl's
# operator 1 (VALUES:MESSAGE): refused (exit 1) with its own message
bad= n=0
for spec in " 0 0 0 8:RS '8' is not 0 to 7" " 0 0 0 0 0 65536:PHASE_U16 '65536' is not 0 to 65535" " 0 0 0 0 0 0 3:FREE '3' is not 0 to 2" \
        ' 0 0 0 0 0 0 0 0:9 or 11 to 16 values'; do
    n=$((n + 1))
    awk -v x="${spec%%:*}" '/^patch bell 1 /{ $0 = $0 x } { print }' tests/golden/48000/bell.tl > "$tmp/extras.tl"
    "$h" --play "$tmp/extras.tl" > "$tmp/extras.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "${spec#*:}" "$tmp/extras.log" || bad="$bad [${spec%%:*}]:$rc"
done
if [ -z "$bad" ]; then ok "extras refused" "$n lines: exit 1, each its message"
else fail "extras refused" "not exit 1 with its message:$bad"; fi

#  accepts wave codes 0..11 after the PM-jump measurements;
# values outside that set remain refused.
bad= n=0
for wave in 12 65535 -1 +1 noninteger; do
    n=$((n + 1))
    awk -v w="$wave" '/^patch bell 0 /{ $0 = $0 " 0 0 " w } { print }' tests/golden/48000/bell.tl > "$tmp/wave.tl"
    "$h" --play "$tmp/wave.tl" > "$tmp/wave.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -Fq "WAVE '$wave' is not 0 to 11" "$tmp/wave.log" || bad="$bad $wave:$rc"
done
if [ -z "$bad" ]; then ok "wave refused" "$n values: exit 1, each its message"
else fail "wave refused" "$bad"; fi

# 's lfo statements the reader refuses, each written before
# tests/golden/48000/bell.tl's voice line (';' starts a new line; LINE:MESSAGE):
# refused (exit 1) with its own message
bad= n=0
for spec in 'lfo bell 0 6000 0 0 30 0 0;lfo bell 0 6000 0 0 30 0 0:second lfo statement' \
        "lfo bell 7 6000 0 0 30 0 0:WAVE '7' is not 0 to 6" "lfo bell 0 9 0 0 30 0 0:RATE_MILLIHZ '9' is not" \
        "lfo bell 0 50001 0 0 30 0 0:RATE_MILLIHZ '50001' is not" "lfo bell 0 6000 60001 0 30 0 0:DELAY_MS '60001' is not" \
        "lfo bell 0 6000 0 60001 30 0 0:FADE_MS '60001' is not" "lfo bell 0 6000 0 0 1201 0 0:PITCH_CENTS '1201' is not" \
        "lfo bell 0 6000 0 0 0 1201 0:LEVEL_DB10 '1201' is not" \
        'lfo bell 0 6000 0 0 30 0 0 0 4 0 0:MASK_PITCH names operator 2' \
        'lfo bell 0 6000 0 0 30 0 0 0 3 8 0:MASK_LEVEL names operator 3' \
        'lfo bell 0 6000 0 0 30 0 0 0 3 0 0 1:FREE 1 is not 0 or 2' \
        "lfo bell 0 6000 0 0 30 0 0 0 3 0 0 3:FREE '3' is not 0 to 2" \
        'lfo bell 0 6000 0 0 30 0 0 2:then PHASE_U16 MASK_PITCH MASK_LEVEL MASK_INDEX'; do
    n=$((n + 1))
    awk -v r="${spec%%:*}" '/^voice /{ k = split(r, a, ";"); for (i = 1; i <= k; i++) print a[i] } { print }' \
        tests/golden/48000/bell.tl > "$tmp/lfo.tl"
    "$h" --play "$tmp/lfo.tl" > "$tmp/lfo.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "${spec#*:}" "$tmp/lfo.log" || bad="$bad [${spec%%:*}]:$rc"
done
if [ -z "$bad" ]; then ok "lfo refused" "$n lines: exit 1, each its message"
else fail "lfo refused" "not exit 1 with its message:$bad"; fi

# an explicit zero FREE remains the old LFO, and canonical output
# omits zero while retaining FREE2. The ordinary replay stage covers both.
for b in $builds; do
    n=0 bad=
    for fs in 48000 44100; do
        for p in lforand freelfo; do
            src=tests/golden/$fs/$p.tl
            awk '/^lfo / && NF == 13 { $0 = $0 " 0" } { print }' "$src" > "$tmp/free.tl"
            build/$b/harness --play "$tmp/free.tl" --merged "$tmp/free.merged.tl" > "$tmp/free.log" 2>&1
            rc=$?
            n=$((n + 1))
            [ $rc -eq 0 ] && cmp -s "$src" "$tmp/free.merged.tl" || bad="$bad $p.$fs:$rc"
        done
    done
    if [ -z "$bad" ]; then ok "free canonical $b" "$n records: zero omitted, two retained"
    else fail "free canonical $b" "$bad"; indent < "$tmp/free.log"; fi
done

#  session adapter: one schema, live bank grammar, event equivalence,
# manifest timestamps, reservations and generator/fx clock integration.
for b in $builds; do
    if python3 - "build/$b/harness" "$tmp/gen-session-$b" > "$tmp/gen-session.log" 2>&1 <<'PYGEN'
import pathlib, re, subprocess, sys
h=pathlib.Path(sys.argv[1]).resolve(); work=pathlib.Path(sys.argv[2]); checks=0
work.mkdir(parents=True,exist_ok=True)
base=pathlib.Path('tests/golden/48000/sine.tl').read_text()
base='\n'.join(s for s in base.splitlines() if not s.startswith('hash ') and not s[:1].isdigit())+'\n'
base=base.replace('length_ns 2000000000','length_ns 100000000')
def run(text,mode='--play',ok=True,needle=None,args=()):
 global checks
 src=work/'input.tl';src.write_text(text)
 p=subprocess.run([str(h),mode,str(src),*map(str,args)],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 checks+=1
 if (p.returncode==0)!=ok or (needle and needle not in p.stdout+p.stderr):
  raise AssertionError((checks,mode,p.returncode,p.stdout,p.stderr,text))
 return p.stdout
clock='gen_clock 64\n'; bank=clock+'gen 0 white 7 1 0\n'
lane='gen_lane 0 0 - - - 1 6900 0 18000 1 0\n'
# The descriptor listing is an exact rendering of the one literal schema.
p=subprocess.run([str(h),'--gen-params'],check=True,text=True,stdout=subprocess.PIPE)
rows=[];kinds=[]
for line in pathlib.Path('src/gen/params.def').read_text().splitlines():
 if line.startswith('GEN_KIND('):
  id,name=[x.strip() for x in line[9:-1].split(',')];kinds.append(f'GEN_KIND id={id} name={name}')
 if line.startswith('GEN_PARAM('):
  id,name,mask,slot,typ,lo,hi,unit,mut,has,val=[x.strip() for x in line[10:-1].split(',')]
  rows.append(f'GEN_PARAM id={id} name={name} kinds={mask} slot={slot} type={typ} min={lo} max={hi} unit={unit} mutable={mut} has_default={has} default={val}')
assert p.stdout.splitlines()==['GEN_PARAMS version=1',*kinds,*rows,f'GEN_PARAMS count={len(rows)}'];checks+=1
# Every scalar kind and both Henon axes can bind any matrix input slot.
configs=['white 7 1 0','pink 8 2 16','brown 9 1 8 16','multi 10 1 16 256 2 6 24576','logistic 11 1 0 1123085157075517','henon 12 1 0 394064967394918 84442493013197','euclid 13 1 0 3 8 0 -','thue 14 1 0 256 0','cantor 15 1 0 256 0','fibonacci 16 1 0 12 0','lsystem 17 1 0 0']
for id,cfg in enumerate(configs):
 headers=clock+f'gen {id} {cfg}\n'
 if id==10:headers+='gen_rule 10 B -\ngen_rule 10 A AB\ngen_axiom 10 3 AB\n'
 for axis in ('xy' if id==5 else 'x'):
  run(base+headers+f'mod sine 7 gen.{id}.{axis} pitch0 1200 0\n0 golden 0 on 220000 12000\n',needle='block_indep=yes')
# Opt-in empty/zero bindings preserve old PCM, including explicit zero values.
plain=run(base+'0 golden 0 on 220000 12000\n');hash0=re.search(r'hash=(\w+)',plain).group(1)
for extra in (clock,bank,bank+'mod sine 0 gen.0.x pitch0 0 3\n'):
 assert re.search(r'hash=(\w+)',run(base+extra+'0 golden 0 on 220000 12000\n')).group(1)==hash0
# Header ordering, sparse IDs, empty rule, and runtime spelling survive merge.
merged=work/'merged.tl'
text=base+clock+'gen_rule 15 B -\ngen_axiom 15 3 AB\ngen 15 lsystem 1 2 3 0\ngen_rule 15 A AB\nmod sine 6 gen.15.x index1 -512\n0 golden 0 on 220000 12000\n1 golden gen 15 rot 1\n2 golden gen 15 reset 0\n3 golden gen 15 slew 512\n'
run(text,args=['--merged',merged]);canon=merged.read_text();run(canon,needle='match=yes block_indep=yes')
assert 'mod sine 6 gen.15.x index1 -512 3' in canon and 'gen_rule 15 B -' in canon
# A session with lanes and no generator-fed matrix expands to identical ordinary audio.
for fs in (48000,44100):
 record=pathlib.Path(f'tests/golden/gen/genlsystem-{fs}.tl').read_text()
 manifest=run(record,'--gen-events');events=[];prev=(-1,-1);count=0
 for line in manifest.splitlines()[1:-1]:
  parts=dict(x.split('=',1) for x in line.split()[1:]);frame=int(parts['frame']);t=int(parts['time_ns']);kind=parts['kind'];voice=int(parts['voice'])
  assert frame==t*fs//1000000000 and t==(frame*1000000000+fs-1)//fs
  order=(frame,0 if kind=='off' else 1);assert order>=prev;prev=order
  events.append(f'{t} golden {voice} '+(f"on {parts['freq_millihz']} {parts['velocity']}" if kind=='on' else 'off'));count+=1
 assert manifest.splitlines()[-1]==f'GEN_EVENTS count={count}' and count
 expanded='\n'.join(s for s in record.splitlines() if not s.startswith('gen') and not (s[:1].isdigit() and s.split()[2] in ('gen','lane')))+'\n'+'\n'.join(events)+'\n'
 run(expanded,needle='match=yes block_indep=yes')
# High sample rates keep live frame-clock behavior. Only emitted manifest
# events requiring impossible nanosecond spellings are refused.
hi=re.sub(r'^fs \d+$','fs 4294967295',base,flags=re.M);hi=re.sub(r'^length_ns \d+$','length_ns 1000',hi,flags=re.M)
run(hi+bank+'0 golden 0 on 220000 12000\n',needle='block_indep=yes')
run(hi+bank,'--gen-events',needle='GEN_EVENTS count=0')
run(hi+bank+lane+'0 golden lane 0 enable 0\n','--gen-events',needle='GEN_EVENTS count=0')
run(hi.replace('fs 4294967295','fs 2000000000')+bank+lane,'--gen-events',needle='GEN_EVENTS count=')
run(hi+bank+lane,needle='block_indep=yes')
run(hi+bank+lane,'--gen-events',ok=False,needle='no exact nanosecond timestamp')
# Effects and the independent effects-reference path use the same bank order.
run(base+bank+lane+'dcblock 20000\n',mode='--fxref',needle='FXREF ok')
# Authored invalid fields, references and reservations refuse, never clamp.
bad=[
 (clock+clock,'second gen_clock'),('gen_clock 37\n','differs'),('gen_clock 0\n','gen_clock'),
 ('gen 0 white 1 1 0\n','need gen_clock'),(bank+'gen 0 white 2 1 0\n','second gen'),
 (clock+'gen 16 white 1 1 0\n',"ID '16' is not 0 to 15"),(clock+'gen 0 orbit 1 1 0\n','unavailable'),
 (clock+'gen 0 white 4294967296 1 0\n','SEED'),(clock+'gen 0 white 1 0 0\n','DIV'),
 (clock+'gen 0 white 1 1 513\n','SLEW'),(clock+'gen 0 white 1 1 0 0\n','number of fields'),
 (clock+'gen 0 brown 1 1 0 0\n','step'),(clock+'gen 0 multi 1 1 0 65536 16 8 0\n','init refused'),
 (clock+'gen 0 logistic 1 1 0 1125899906842625\n',"r '1125899906842625' is not 0 to 1125899906842624"),(clock+'gen 0 henon 1 1 0 0 -281474976710657\n',"b '-281474976710657' is not -281474976710656 to 281474976710656"),
 (clock+'gen 0 euclid 1 1 0 9 8 0 -\n','init refused'),(clock+'gen 0 euclid 1 1 0 3 8 8 -\n','init refused'),
 (clock+'gen 0 euclid 1 1 0 3 8 0 0\n','init refused'),(clock+'gen 1 euclid 1 1 0 3 8 0 0\n','init refused'),
 (clock+'gen 0 fibonacci 1 1 0 17 0\n','depth'),(clock+'gen 0 lsystem 1 1 0 0\n','init refused'),
 (bank+'gen_axiom 0 1 A\n','init refused'),(bank+'gen_rule 0 A AB\n','lacks gen_axiom'),
 (clock+'gen 0 lsystem 1 1 0 0\ngen_axiom 0 1 I\n','gen_axiom'),
 (clock+'gen 0 lsystem 1 1 0 0\ngen_axiom 0 1 A\ngen_axiom 0 1 A\n','second gen_axiom'),
 (clock+'gen 0 lsystem 1 1 0 0\ngen_axiom 0 1 A\ngen_rule 0 A -\ngen_rule 0 A B\n','second gen_rule'),
 (clock+'gen 0 lsystem 1 1 0 0\ngen_axiom 0 16 A\ngen_rule 0 A AA\n','init refused'),
 (bank+'mod sine 0 gen.0.y pitch0 0\n','needs henon'),(bank+'mod sine 0 gen.15.x pitch0 0\n','undefined'),
 (bank+'mod sine 0 gen.16.x pitch0 0\n','ID out'),(bank+'mod sine 0 gen0 pitch0 0\n','generators use'),
 ('mod sine 0 gen.0.x pitch0 0\n','needs gen_clock'),
 (bank+lane+lane,'second gen_lane'),(bank+lane+'gen_lane 1 0 - - - 1 6900 0 10000 1 0\n','init refused'),
 (bank+'gen_lane 0 1 - - - 1 6900 0 10000 1 0\n','voice 1 is undefined'),
 (bank+'gen_lane 0 0 15 - - 1 6900 0 10000 1 0\n','init refused'),
 (bank+'gen_lane 0 0 - - - 1 6900 0 10000 2 0\n','init refused'),
 (bank+'gen_lane 0 0 - - - 1 -4800 0 10000 1 1\n','init refused'),
 (bank+'gen_lane 0 0 - - - 1 14400 1 10000 1 0\n','init refused'),
 (bank+lane+'0 golden 0 on 440000 12000\n','reserved'),(bank+lane+'0 golden 0 off\n','reserved'),
 (bank+lane+'0 golden 0 pitch 440000\n','reserved'),(bank+'0 golden gen 0 div 2\n','immutable'),
 (bank+'0 golden gen 0 reset -1\n',"reset '-1' is not 0 to 4294967295"),(bank+'0 golden gen 0 slew 513\n',"slew '513' is not 0 to 512"),
 (bank+'0 golden gen 15 reset 1\n','undefined'),(bank+'0 golden lane 0 enable 1\n','undefined'),
 (bank+lane+'0 golden lane 0 enable 2\n',"enabled '2' is not 0 to 1"),('0 golden gen 0 reset 1\n','needs gen_clock'),
 (bank+'0 golden gen 0 enabled 0.5\n',"enabled '0.5' is not 0 to 1"),
 (clock+'gen 0 euclid 1 1 0 3 8 0 -\n0 golden gen 0 n 2\n','refused')]
for suffix,needle in bad:run(base+suffix,ok=False,needle=needle)
# Schema-driven outside scalar endpoints are refused before core mutation.
for line in rows:
 d=dict(x.split('=',1) for x in line.split()[1:]);mask=int(d['kinds']);slot=d['slot']
 if not mask or not slot.startswith('p'):continue
 kind=(mask & -mask).bit_length();cfg=configs[kind-1].split();index=4+int(slot[1])
 for value in (int(d['min'])-1,int(d['max'])+1):
  changed=cfg[:];changed[index]=str(value)
  suffix=clock+'gen 0 '+' '.join(changed)+'\n'
  if kind==11:suffix+='gen_axiom 0 1 A\n'
  run(base+suffix,ok=False,needle=d['name'])
print(f'GENSESSION checks={checks} failures=0')
PYGEN
    then ok "generator session $b" "$(tail -n 1 "$tmp/gen-session.log")"
    else fail "generator session $b" "parser, manifest or event equivalence"; tail -n 12 "$tmp/gen-session.log" | indent; fi
done

# canonical sparse slots, signed controls and opt-in identity.
for b in $builds; do
    h=build/$b/harness
    bad= n=0
    for fs in 48000 44100; do
        for name in modctl modsmooth modmany modfilter modlfoself modenv; do
            n=$((n + 1)); src=tests/golden/$fs/$name.tl
            awk '/^mod / { mod[++m]=$0; next } { lines[++n]=$0 } END { for(i=m;i>0;i--) print mod[i]; for(i=1;i<=n;i++) print lines[i] }' "$src" > "$tmp/mod-early.tl"
            "$h" --play "$tmp/mod-early.tl" --merged "$tmp/mod-merged.tl" > "$tmp/mod.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] && cmp -s "$src" "$tmp/mod-merged.tl" || bad="$bad $name.$fs:$rc"
        done
        # Reversing a ctl/on pair at the same timestamp must change the sound.
        awk '/^0 golden 0 ctl / { ctl=$0; next } /^0 golden 0 on / { print; print ctl; next } { print }' tests/golden/$fs/modctl.tl > "$tmp/mod-order.tl"
        "$h" --play "$tmp/mod-order.tl" > "$tmp/mod.log" 2>&1
        rc=$?
        [ $rc -eq 3 ] && grep -q 'match=NO block_indep=yes' "$tmp/mod.log" || bad="$bad order.$fs:$rc"
        # An explicit zero remains authored, even though it cannot arm the clock.
        awk '/^voice / { print "mod bell 7 ctl7 pitch0 0" } { print }' tests/golden/$fs/bell.tl > "$tmp/mod-zero.tl"
        "$h" --play "$tmp/mod-zero.tl" --merged "$tmp/mod-zero-merged.tl" > "$tmp/mod.log" 2>&1
        rc=$?
        [ $rc -eq 0 ] && grep -qx 'mod bell 7 ctl7 pitch0 0 3' "$tmp/mod-zero-merged.tl" || bad="$bad zero.$fs:$rc"
        awk '!/^mod /' "$tmp/mod-zero-merged.tl" > "$tmp/mod-absent.tl"
        cmp -s tests/golden/$fs/bell.tl "$tmp/mod-absent.tl" || bad="$bad zero.bytes.$fs"
        # Controllers are accepted even without a matrix; they remain signed.
        awk '/^0 golden / { print "0 golden 0 ctl 7 -32768"; print "0 golden 0 ctl 7 32768" } { print }' tests/golden/$fs/bell.tl > "$tmp/mod-idle.tl"
        "$h" --play "$tmp/mod-idle.tl" --merged "$tmp/mod-idle-merged.tl" > "$tmp/mod.log" 2>&1
        rc=$?
        [ $rc -eq 0 ] && grep -qx '0 golden 0 ctl 7 -32768' "$tmp/mod-idle-merged.tl" || bad="$bad no.slots.$fs:$rc"
    done
    if [ -z "$bad" ]; then ok "matrix canonical $b" "$n records, early/sparse headers, signed controls, zero identity and tied-event order"
    else fail "matrix canonical $b" "$bad"; indent < "$tmp/mod.log"; fi

done

# Every earlier record tolerates an authored inactive slot, with exact PCM.
for b in $builds; do
    bad= n=0
    for fs in 48000 44100; do
        for src in tests/golden/$fs/*.tl; do
            case "$src" in */mod*.tl) continue ;; esac
            n=$((n + 1))
            awk '/^voice / { print "mod " $3 " 7 ctl7 pitch0 0 15" } { print }' "$src" > "$tmp/mod-all-zero.tl"
            build/$b/harness --play "$tmp/mod-all-zero.tl" > "$tmp/mod-all-zero.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] && grep -q 'match=yes block_indep=yes' "$tmp/mod-all-zero.log" || bad="$bad $src:$rc"
        done
    done
    if [ -z "$bad" ]; then ok "matrix zero slots $b" "$n earlier records preserve their hash"
    else fail "matrix zero slots $b" "$bad"; fi
done

# Every currently bound source and target has a valid textual spelling.
# Four operators include two carriers and two outgoing PM sources.
for b in $builds; do
    bad= n=0
    for kind in source target shift; do
        case $kind in
            source) values="lfo peg fenv eg0 eg1 eg2 eg3 vel note ctl0 ctl1 ctl2 ctl3 ctl4 ctl5 ctl6 ctl7" ;;
            target) values="pitch0 pitch1 pitch2 pitch3 level0 level2 index1 index3 cutoff q lfo_rate lfo_pitch lfo_level lfo_index" ;;
            shift) values="0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15" ;;
        esac
        for value in $values; do
            n=$((n + 1)); source=ctl0 target=pitch0 shift=3
            case $kind in source) source=$value ;; target) target=$value ;; shift) shift=$value ;; esac
            awk -v source="$source" -v target="$target" -v shift="$shift" '
                /^hash / { next }
                /^patch bell 0 / { print; $3=2; print; next }
                /^patch bell 1 / { print; $3=3; print; next }
                /^voice / {
                    print "route bell 4 5 1>0:256 3>2:256"
                    print "filter bell lp 1000000 707 x2"
                    print "lfo bell 1 3000 0 0 0 0 0"
                    print "peg bell 1200 200 0 300 0 0 -1200 150"
                    print "fenv bell 50 300 500 150 1200 0"
                    print "mod bell 7 " source " " target " -1 " shift
                }
                { print }
            ' tests/golden/48000/bell.tl > "$tmp/mod-valid.tl"
            build/$b/harness --play "$tmp/mod-valid.tl" --merged "$tmp/mod-valid-merged.tl" > "$tmp/mod-valid.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] && grep -qx "mod bell 7 $source $target -1 $shift" "$tmp/mod-valid-merged.tl" || bad="$bad $kind/$value:$rc"
        done
    done
    if [ -z "$bad" ]; then ok "matrix names $b" "$n records: all sources, targets and smoothing shifts"
    else fail "matrix names $b" "$bad"; indent < "$tmp/mod-valid.log"; fi
done

# Text refuses invalid rows even when AMOUNT is zero. Core raw clamps have
# their independent MODBOUND diagnostic; unbound generator aliases and unimplemented hybrid IDs stay refused.
h=build/s12e10p1v1/harness
bad= n=0
for spec in 'mod bell 8 ctl0 pitch0 1:SLOT' 'mod bell -1 ctl0 pitch0 1:SLOT' \
    'mod bell 0 ctl8 pitch0 1:SOURCE' 'mod bell 0 gen0 pitch0 1:generators use' \
    'mod bell 0 gen7 pitch0 0:generators use' 'mod bell 0 eg4 pitch0 1:SOURCE' \
    'mod bell 0 ctl0 pitch4 1:TARGET' 'mod bell 0 ctl0 off 0:TARGET' \
    'mod bell 0 ctl0 pd0 1:hybrid targets' 'mod bell 0 ctl0 wtpos0 1:hybrid targets' \
    'mod bell 0 ctl0 32 1:hybrid targets' 'mod bell 0 ctl0 37 0:hybrid targets' \
    'mod bell 0 ctl0 pitch0 4801:AMOUNT' 'mod bell 0 ctl0 pitch0 -4801:AMOUNT' \
    'mod bell 0 ctl0 level0 1201:AMOUNT' 'mod bell 0 ctl0 index1 65536:AMOUNT' \
    'mod bell 0 ctl0 cutoff 9601:AMOUNT' 'mod bell 0 ctl0 q 15501:AMOUNT' \
    'mod bell 0 ctl0 lfo_rate 49991:AMOUNT' 'mod bell 0 ctl0 lfo_pitch 1201:AMOUNT' \
    'mod bell 0 ctl0 lfo_level -1201:AMOUNT' 'mod bell 0 ctl0 lfo_index -65536:AMOUNT' \
    'mod bell 0 ctl0 pitch0 1 16:SHIFT' 'mod bell 0 ctl0 pitch0 1 -1:SHIFT' \
    'mod bell 0 ctl0 pitch0 1.5:AMOUNT' 'mod bell 0 ctl0 pitch0:mod takes' \
    'mod bell 0 ctl0 pitch0 1 3 extra:mod takes' \
    'mod bell 0 ctl0 pitch0 0;mod bell 0 ctl1 pitch0 0:second mod SLOT' \
    'mod bell 0 lfo pitch0 0:real lfo' 'mod bell 0 peg pitch0 0:active peg' \
    'peg bell 0 1 0 1 0 1 0 1;mod bell 0 peg pitch0 0:active peg' \
    'mod bell 0 fenv pitch0 0:active fenv' \
    'filter bell lp 1000000 707;fenv bell 1 1 500 1 0 0;mod bell 0 fenv pitch0 0:active fenv' \
    'mod bell 0 eg2 pitch0 0:absent eg operator' 'mod bell 0 ctl0 pitch2 0:absent operator' \
    'mod bell 0 ctl0 level1 0:not a carrier' 'mod bell 0 ctl0 index0 0:no outgoing PM' \
    'route bell 2 1 1>0:256:am;mod bell 0 ctl0 index1 0:no outgoing PM' \
    'route bell 2 1 1>0:256:ring;mod bell 0 ctl0 index1 0:no outgoing PM' \
    'route bell 2 1 1>0:256:sync;mod bell 0 ctl0 index1 0:no outgoing PM' \
    'mod bell 0 ctl0 cutoff 0:needs a filter' 'mod bell 0 ctl0 q 0:needs a filter' \
    'mod bell 0 ctl0 lfo_rate 0:real lfo' 'mod nobody 0 ctl0 pitch0 1:lacks operator'; do
    n=$((n + 1)); line=${spec%:*}; msg=${spec##*:}
    awk -v r="$line" '/^voice / { k=split(r,a,";"); for(i=1;i<=k;i++) print a[i] } { print }' tests/golden/48000/bell.tl > "$tmp/mod-bad.tl"
    "$h" --play "$tmp/mod-bad.tl" > "$tmp/mod-bad.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "$msg" "$tmp/mod-bad.log" || bad="$bad [$line]:$rc"
done
for line in 'ctl 8 0' 'ctl -1 0' 'ctl 0 32769' 'ctl 0 -32769' 'ctl 0 0 extra' 'ctl 0' 'ctl 0 .5'; do
    n=$((n + 1))
    awk -v r="$line" '/^0 golden / { print "0 golden 0 " r } { print }' tests/golden/48000/bell.tl > "$tmp/mod-bad.tl"
    "$h" --play "$tmp/mod-bad.tl" > "$tmp/mod-bad.log" 2>&1
    rc=$?
    case "$line" in
        'ctl 8 0') msg="N '8' is not 0 to 7" ;;
        'ctl -1 0') msg="N '-1' is not 0 to 7" ;;
        'ctl 0 32769') msg="VALUE_Q15 '32769' is not -32768 to 32768" ;;
        'ctl 0 -32769') msg="VALUE_Q15 '-32769' is not -32768 to 32768" ;;
        'ctl 0 .5') msg="VALUE_Q15 '.5' is not -32768 to 32768" ;;
        *) msg='ctl takes N 0 to 7' ;;
    esac
    [ $rc -eq 1 ] && grep -Fq "$msg" "$tmp/mod-bad.log" || bad="$bad [$line]:$rc"
done
if [ -z "$bad" ]; then ok "matrix refused" "$n lines: ranges, applicability, duplicates and reservations"
else fail "matrix refused" "$bad"; fi

# At fs128, the control Nyquist is exactly 1000mHz. Test combined slots
# below, at and above it, both signs and reordered headers.
for b in $builds; do
    bad= n=0
    for amount in 989 990 991 -989 -990 -991 0; do
        for late in 0 1; do
            n=$((n + 1))
            awk -v amount="$amount" -v late="$late" '
                BEGIN { if (!late) print "mod bell 7 ctl0 lfo_rate " amount " 0" }
                /^fs / { print "fs 128"; next } /^hash / || /^[0-9]/ { next }
                /^voice / { print "lfo bell 0 10 0 0 0 0 0"; if(late) print "mod bell 7 ctl0 lfo_rate " amount " 0" }
                { print } END { print "0 golden 0 on 523250 32767" }
            ' tests/golden/48000/bell.tl > "$tmp/mod-rate.tl"
            build/$b/harness --play "$tmp/mod-rate.tl" > "$tmp/mod-rate.log" 2>&1
            rc=$?
            case $amount in
                0|989|-989) [ $rc -eq 0 ] || bad="$bad $amount/$late:$rc" ;;
                *) [ $rc -eq 1 ] && grep -q 'mod reachable lfo_rate .* reaches the control Nyquist' "$tmp/mod-rate.log" || bad="$bad $amount/$late:$rc" ;;
            esac
        done
    done
    if [ -z "$bad" ]; then ok "matrix rate $b" "$n records: strict reachable bound, zero exemption and header order"
    else fail "matrix rate $b" "$bad"; fi
done

#  cascade: x2 survives canonical output, early headers and replay.
for b in $builds; do
    h=build/$b/harness
    bad= n=0
    for fs in 48000 44100; do
        for name in filtertwo shapefold; do
            src=tests/golden/$fs/$name.tl
            awk '/^filter /{ filter=$0; next } { lines[++n]=$0 } END { print filter; for(i=1;i<=n;i++) print lines[i] }' "$src" > "$tmp/cascade-early.tl"
            "$h" --play "$tmp/cascade-early.tl" --merged "$tmp/cascade-merged.tl" > "$tmp/cascade.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] && grep -q '^filter .* x2$' "$tmp/cascade-merged.tl" && cmp -s "$src" "$tmp/cascade-merged.tl" || bad="$bad $name.$fs:$rc"
            awk '/^filter /{ sub(/ x2$/,"",$0) } { print }' "$src" > "$tmp/cascade-one.tl"
            "$h" --play "$tmp/cascade-one.tl" > "$tmp/cascade.log" 2>&1
            rc=$?
            [ $rc -eq 3 ] || bad="$bad dropped.$name.$fs:$rc"
            n=$((n + 1))
        done
    done
    if [ -z "$bad" ]; then ok "cascade canonical $b" "$n records: early header, x2 retained, one stage differs"
    else fail "cascade canonical $b" "differs:$bad"; fi
done
bad= n=0
for spec in 'filter bell lp 1000000 707 x1:stages must be x2' \
        'filter bell lp 1000000 707 0:stages must be x2' 'filter bell lp 1000000 707 1:stages must be x2' \
        'filter bell lp 1000000 707 2:stages must be x2' 'filter bell lp 1000000 707 X2:stages must be x2' \
        'filter bell lp 1000000 707 x2 x2:filter takes' 'filter bell lp 1000000 707 x2 extra:filter takes' \
        'filter bell lp 1000000 707 x2;filter bell lp 1000000 707:second filter' \
        'filter nobody lp 1000000 707 x2:lacks operator'; do
    line=${spec%%:*}; msg=${spec#*:}; n=$((n + 1))
    awk -v r="$line" '/^voice /{ k=split(r,a,";"); for(i=1;i<=k;i++) print a[i] } { print }' tests/golden/48000/bell.tl > "$tmp/cascade-bad.tl"
    "$h" --play "$tmp/cascade-bad.tl" > "$tmp/cascade-bad.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "$msg" "$tmp/cascade-bad.log" || bad="$bad [$line]:$rc"
done
if [ -z "$bad" ]; then ok "cascade refused" "$n lines: exit 1, each its message"
else fail "cascade refused" "not refused as expected:$bad"; fi

#  shape headers may precede operators; zero drive remains present.
for b in $builds; do
    h=build/$b/harness
    bad= n=0
    for fs in 48000 44100; do
        for name in shapehard shapecubic shapefold1 shapepre shapeprehard shapeprefold shapeasym shapeasympre; do
            src=tests/golden/$fs/$name.tl
            awk '/^shape /{ shape=$0; next } { lines[++n]=$0 } END { print shape; for(i=1;i<=n;i++) print lines[i] }' "$src" > "$tmp/shape-early.tl"
            "$h" --play "$tmp/shape-early.tl" --merged "$tmp/shape-merged.tl" > "$tmp/shape.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] && cmp -s "$src" "$tmp/shape-merged.tl" || bad="$bad $name.$fs:$rc"
            n=$((n + 1))
        done
        for kind in hard cubic fold asym; do
            awk -v k="$kind" '/^hash /{ next } /^shape /{ $3=0; $4=k } { print }' tests/golden/$fs/shapehard.tl > "$tmp/shape-zero.tl"
            "$h" --play "$tmp/shape-zero.tl" --merged "$tmp/shape-zero-merged.tl" > "$tmp/shape.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] && grep -qx "shape shapehard 0 $kind" "$tmp/shape-zero-merged.tl" || bad="$bad zero.$kind.$fs:$rc"
            "$h" --play "$tmp/shape-zero-merged.tl" > "$tmp/shape.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] && grep -q 'match=yes block_indep=yes' "$tmp/shape.log" || bad="$bad zero.replay.$kind.$fs:$rc"
            if [ "$kind" = cubic ]; then
                awk '!/^shape /' "$tmp/shape-zero-merged.tl" > "$tmp/shape-absent.tl"
                "$h" --play "$tmp/shape-absent.tl" > "$tmp/shape.log" 2>&1
                rc=$?
                [ $rc -eq 3 ] || bad="$bad zero.not.bypass.$fs:$rc"
            fi
            n=$((n + 1))
        done
    done
    if [ -z "$bad" ]; then ok "shape canonical $b" "$n records: early header and present zero drive"
    else fail "shape canonical $b" "differs:$bad"; fi
done
bad= n=0
for spec in 'shape bell 241 hard:DRIVE_DB10' 'shape bell -1 hard:DRIVE_DB10' \
        'shape bell 65535 hard:DRIVE_DB10' 'shape bell 0 bypass:KIND' 'shape bell 0 HARD:KIND' \
        'shape bell 0:shape takes' 'shape bell 0 hard extra:shape takes' \
        'shape bell 0 hard;shape bell 1 fold:second shape' 'shape nobody 0 hard:lacks operator' \
        'shape bell 0 1:KIND' 'shape bell 0 none:KIND' 'shape bell .5 cubic:DRIVE_DB10' \
        'shape bell 0 hard post:shape takes' 'shape bell 0 asym pre extra:shape takes' \
        'shape bell 0 pre:KIND' 'shape bell 0 asymmetric:KIND' 'shape bell 0 pre asym:KIND'; do
    line=${spec%%:*}; msg=${spec#*:}; n=$((n + 1))
    awk -v r="$line" '/^voice /{ k=split(r,a,";"); for(i=1;i<=k;i++) print a[i] } { print }' tests/golden/48000/bell.tl > "$tmp/shape-bad.tl"
    "$h" --play "$tmp/shape-bad.tl" > "$tmp/shape-bad.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "$msg" "$tmp/shape-bad.log" || bad="$bad [$line]:$rc"
done
if [ -z "$bad" ]; then ok "shape refused" "$n lines: exit 1, each its message"
else fail "shape refused" "not refused as expected:$bad"; fi

#   kstype and fbtype headers may precede the patch; the
# defaults (index - -, classic) replay to the same hash and normalize away.
for b in $builds; do
    h=build/$b/harness
    bad= n=0
    for fs in 48000 44100; do
        for name in kslevel fbsquare fbpre; do
            src=tests/golden/$fs/$name.tl
            awk '/^(kstype|fbtype) /{ hdr[++m]=$0; next } { lines[++n]=$0 } END { for(i=1;i<=m;i++) print hdr[i]; for(i=1;i<=n;i++) print lines[i] }' "$src" > "$tmp/ksfb-early.tl"
            "$h" --play "$tmp/ksfb-early.tl" --merged "$tmp/ksfb-merged.tl" > "$tmp/ksfb.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] && cmp -s "$src" "$tmp/ksfb-merged.tl" || bad="$bad $name.$fs:$rc"
            n=$((n + 1))
        done
        awk '/^voice /{ print "kstype bell 0 index - -"; print "fbtype bell 1 classic" } { print }' tests/golden/$fs/bell.tl > "$tmp/ksfb-default.tl"
        "$h" --play "$tmp/ksfb-default.tl" --merged "$tmp/ksfb-default-merged.tl" > "$tmp/ksfb.log" 2>&1
        rc=$?
        [ $rc -eq 0 ] && grep -q 'match=yes block_indep=yes' "$tmp/ksfb.log" && cmp -s tests/golden/$fs/bell.tl "$tmp/ksfb-default-merged.tl" || bad="$bad default.$fs:$rc"
        n=$((n + 1))
        awk '/^(kstype|fbtype) /{ next } { print }' tests/golden/$fs/fbpre.tl > "$tmp/ksfb-absent.tl"
        "$h" --play "$tmp/ksfb-absent.tl" > "$tmp/ksfb.log" 2>&1
        rc=$?
        [ $rc -eq 3 ] || bad="$bad absent.$fs:$rc"
        n=$((n + 1))
    done
    if [ -z "$bad" ]; then ok "ksfb canonical $b" "$n records: early headers, defaults normalize away, types matter"
    else fail "ksfb canonical $b" "differs:$bad"; fi
done
bad= n=0
for spec in 'kstype bell 0 index -:kstype takes' 'kstype bell 0 index - - x:kstype takes' \
        "kstype bell 4 level - -:OP '4' is not" 'kstype bell 0 pitch - -:TARGET pitch' \
        'kstype bell 0 Level - -:TARGET Level' 'kstype bell 0 level x -:RIGHT x' \
        'kstype bell 0 level - 1:LEFT 1' 'kstype bell 0 level - -;kstype bell 0 both + +:second kstype' \
        'kstype bell 2 level - -:kstype names operator 2' 'kstype nobody 0 level - -:lacks operator' \
        'fbtype bell 1:fbtype takes' 'fbtype bell 1 pre x:fbtype takes' 'fbtype bell 1 squared:TYPE squared' \
        'fbtype bell 1 2:TYPE 2' 'fbtype bell 1 pre;fbtype bell 1 square:second fbtype' \
        'fbtype bell 3 pre:fbtype names operator 3' 'fbtype nobody 1 pre:lacks operator'; do
    line=${spec%%:*}; msg=${spec#*:}; n=$((n + 1))
    awk -v r="$line" '/^voice /{ k=split(r,a,";"); for(i=1;i<=k;i++) print a[i] } { print }' tests/golden/48000/bell.tl > "$tmp/ksfb-bad.tl"
    "$h" --play "$tmp/ksfb-bad.tl" > "$tmp/ksfb-bad.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "$msg" "$tmp/ksfb-bad.log" || bad="$bad [$line]:$rc"
done
if [ -z "$bad" ]; then ok "ksfb refused" "$n lines: exit 1, each its message"
else fail "ksfb refused" "not refused as expected:$bad"; fi

#  and   an amp or egloop header may precede its patch;
# amp is written after the shapes, egloop after its operator's fbtype.
# Omitting either bypasses its envelope and changes the hash.
for b in $builds; do
    h=build/$b/harness
    bad= n=0
    for fs in 48000 44100; do
        for name in ampeg ampring ampretrig egloop; do
            src=tests/golden/$fs/$name.tl
            awk '/^(amp|egloop) /{ hdr[++m]=$0; next } { lines[++n]=$0 } END { for(i=1;i<=m;i++) print hdr[i]; for(i=1;i<=n;i++) print lines[i] }' "$src" > "$tmp/amp-early.tl"
            "$h" --play "$tmp/amp-early.tl" --merged "$tmp/amp-merged.tl" > "$tmp/amp.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] && cmp -s "$src" "$tmp/amp-merged.tl" || bad="$bad $name.$fs:$rc"
            n=$((n + 1))
            awk '/^(amp|egloop) /{ next } { print }' "$src" > "$tmp/amp-absent.tl"
            "$h" --play "$tmp/amp-absent.tl" > "$tmp/amp.log" 2>&1
            rc=$?
            [ $rc -eq 3 ] || bad="$bad absent.$name.$fs:$rc"
            n=$((n + 1))
        done
    done
    if [ -z "$bad" ]; then ok "amp egloop canonical $b" "$n records: early header, absent amp or egloop changes the hash"
    else fail "amp egloop canonical $b" "differs:$bad"; fi
done
bad= n=0
for spec in 'amp bell 5 200 120:amp takes' 'amp bell 5 200 120 300 x:amp takes' \
        "amp bell 5 200 120 65536:RELEASE_MS '65536' is not 0 to 65535" "amp bell x 200 120 300:ATTACK_MS 'x' is not 0 to 65535" \
        "amp bell -1 200 120 300:ATTACK_MS '-1' is not 0 to 65535" 'amp bell 5 200 120 300;amp bell 1 1 1 1:second amp' \
        'amp nobody 5 200 120 300:lacks operator' "amp b@d 5 200 120 300:bad patch name"; do
    line=${spec%%:*}; msg=${spec#*:}; n=$((n + 1))
    awk -v r="$line" '/^voice /{ k=split(r,a,";"); for(i=1;i<=k;i++) print a[i] } { print }' tests/golden/48000/bell.tl > "$tmp/amp-bad.tl"
    "$h" --play "$tmp/amp-bad.tl" > "$tmp/amp-bad.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "$msg" "$tmp/amp-bad.log" || bad="$bad [$line]:$rc"
done
if [ -z "$bad" ]; then ok "amp refused" "$n lines: exit 1, each its message"
else fail "amp refused" "not refused as expected:$bad"; fi
bad= n=0
for spec in 'egloop bell:egloop takes' 'egloop bell 1 2:egloop takes' "egloop bell 4:OP '4' is not" \
        "egloop bell x:OP 'x' is not" 'egloop bell 1;egloop bell 1:second egloop statement for operator 1' \
        'egloop bell 3:egloop names operator 3' 'egloop nobody 0:lacks operator' 'egloop b@d 0:bad patch name'; do
    line=${spec%%:*}; msg=${spec#*:}; n=$((n + 1))
    awk -v r="$line" '/^voice /{ k=split(r,a,";"); for(i=1;i<=k;i++) print a[i] } { print }' tests/golden/48000/bell.tl > "$tmp/egloop-bad.tl"
    "$h" --play "$tmp/egloop-bad.tl" > "$tmp/egloop-bad.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "$msg" "$tmp/egloop-bad.log" || bad="$bad [$line]:$rc"
done
if [ -z "$bad" ]; then ok "egloop refused" "$n lines: exit 1, each its message"
else fail "egloop refused" "not refused as expected:$bad"; fi

#  delay: zero extensions leave every older golden untouched. Nonzero
# eg headers may precede the patch, then canonicalize after  statements.
for b in $builds; do
    n=0 bad=
    for fs in 48000 44100; do
        for f in tests/golden/$fs/*.tl; do
            grep -q '^eg ' "$f" && continue
            awk '/^patch / { eg = eg "eg " $2 " " $3 " 0 0 0\n" }
                 /^voice / && !done { printf "%s", eg; done = 1 } { print }' "$f" > "$tmp/eg-zero.tl"
            build/$b/harness --play "$tmp/eg-zero.tl" --merged "$tmp/eg-merged.tl" > "$tmp/eg.log" 2>&1
            rc=$?
            n=$((n + 1))
            [ $rc -eq 0 ] && cmp -s "$f" "$tmp/eg-merged.tl" || bad="$bad ${f##*/}.$fs:$rc"
        done
        for p in egdelay egdecay2 egjump; do
            f=tests/golden/$fs/$p.tl
            awk '/^eg / { print }' "$f" > "$tmp/eg-first.tl"
            awk '!/^eg / { print }' "$f" >> "$tmp/eg-first.tl"
            build/$b/harness --play "$tmp/eg-first.tl" --merged "$tmp/eg-merged.tl" > "$tmp/eg.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] && cmp -s "$f" "$tmp/eg-merged.tl" || bad="$bad $p-first.$fs:$rc"
        done
    done
    if [ -z "$bad" ]; then ok "eg compatibility $b" "$n zero records; delay headers before patch"
    else fail "eg compatibility $b" "$bad"; indent < "$tmp/eg.log"; fi
done
bad= n=0
for spec in 'eg bell 0 10 0 0;eg bell 0 20 0 0:second eg statement for operator 0' \
        "eg bell 4 10 0 0:OP '4' is not" 'eg bell 2 10 0 0:eg names operator 2' \
        "eg bell 0 60001 0 0:DELAY_MS '60001' is not" "eg bell 0 -1 0 0:DELAY_MS '-1' is not" \
        "eg bell 0 +1 0 0:DELAY_MS '+1' is not" "eg bell 0 10 60001 400:DECAY2_MS '60001' is not" \
        "eg bell 0 10 0 1201:SUSTAIN2_DB10 '1201' is not" \
        'eg bell 0 10 1 0:SUSTAIN2_DB10 0 is below SUSTAIN1' \
        'eg bell 0 10 0 1:SUSTAIN2_DB10 1 is below SUSTAIN1' \
        'eg bell 0 10 0:eg takes PATCH' 'eg bell 0 10 0 0 0:eg takes PATCH' \
        'eg nobody 0 10 0 0:patch nobody lacks operator 0'; do
    n=$((n + 1))
    awk -v r="${spec%%:*}" '/^voice /{ k = split(r, a, ";"); for (i = 1; i <= k; i++) print a[i] } { print }' \
        tests/golden/48000/bell.tl > "$tmp/eg.tl"
    "$h" --play "$tmp/eg.tl" > "$tmp/eg.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "${spec#*:}" "$tmp/eg.log" || bad="$bad [${spec%%:*}]:$rc"
done
# Sustain ordering is checked after headers, including an eg before patch.
for fs in 48000 44100; do
    { echo 'eg bell 0 0 1 0'; cat "tests/golden/$fs/bell.tl"; } > "$tmp/eg-first-bad.tl"
    "$h" --play "$tmp/eg-first-bad.tl" > "$tmp/eg.log" 2>&1
    rc=$?
    n=$((n + 1))
    [ $rc -eq 1 ] && grep -q 'SUSTAIN2_DB10 0 is below SUSTAIN1' "$tmp/eg.log" || bad="$bad first.$fs:$rc"
done
if [ -z "$bad" ]; then ok "eg refused" "$n lines: exit 1, each its message"
else fail "eg refused" "$bad"; indent < "$tmp/eg.log"; fi

#  left index scaling: explicit zero slopes preserve all older records.
for b in $builds; do
    n=0 bad=
    for fs in 48000 44100; do
        for f in tests/golden/$fs/*.tl; do
            grep -q '^ks ' "$f" && continue
            awk '/^patch / { ks = ks "ks " $2 " " $3 " 0\n" }
                 /^voice / && !done { printf "%s", ks; done = 1 } { print }' "$f" > "$tmp/ks-zero.tl"
            build/$b/harness --play "$tmp/ks-zero.tl" --merged "$tmp/ks-merged.tl" > "$tmp/ks.log" 2>&1
            rc=$?
            n=$((n + 1))
            [ $rc -eq 0 ] && cmp -s "$f" "$tmp/ks-merged.tl" || bad="$bad ${f##*/}.$fs:$rc"
        done
        f=tests/golden/$fs/ksleft.tl
        awk '/^ks / { print }' "$f" > "$tmp/ks-first.tl"
        awk '!/^ks / { print }' "$f" >> "$tmp/ks-first.tl"
        build/$b/harness --play "$tmp/ks-first.tl" --merged "$tmp/ks-merged.tl" > "$tmp/ks.log" 2>&1
        rc=$?
        [ $rc -eq 0 ] && cmp -s "$f" "$tmp/ks-merged.tl" || bad="$bad ks-first.$fs:$rc"
    done
    if [ -z "$bad" ]; then ok "ks compatibility $b" "$n zero records; ks headers before patch"
    else fail "ks compatibility $b" "$bad"; indent < "$tmp/ks.log"; fi
done
bad= n=0
for spec in 'ks bell 1 120;ks bell 1 0:second ks statement for operator 1' \
        "ks bell 4 120:OP '4' is not" 'ks bell 2 120:ks names operator 2' \
        "ks bell 1 1201:LO_SLOPE_DB10 '1201' is not" "ks bell 1 -1:LO_SLOPE_DB10 '-1' is not" \
        "ks bell 1 +1:LO_SLOPE_DB10 '+1' is not" 'ks bell 1:ks takes PATCH' \
        'ks bell 1 120 0:ks takes PATCH' 'ks nobody 0 120:patch nobody lacks operator 0'; do
    n=$((n + 1))
    awk -v r="${spec%%:*}" '/^voice /{ k = split(r, a, ";"); for (i = 1; i <= k; i++) print a[i] } { print }' \
        tests/golden/48000/bell.tl > "$tmp/ks.tl"
    "$h" --play "$tmp/ks.tl" > "$tmp/ks.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "${spec#*:}" "$tmp/ks.log" || bad="$bad [${spec%%:*}]:$rc"
done
if [ -z "$bad" ]; then ok "ks refused" "$n lines: exit 1, each its message"
else fail "ks refused" "$bad"; indent < "$tmp/ks.log"; fi

#  colour: an explicit default normalizes away; nondefault headers may
# precede the patch. Duplicate detection is independent of normalization.
for b in $builds; do
    n=0 bad=
    for fs in 48000 44100; do
        for p in colourlp colourhp colourfree; do
            src=tests/golden/$fs/$p.tl
            if [ "$p" = colourhp ]; then
                awk '/^noise / { print }' "$src" > "$tmp/noise-first.tl"
            else
                echo "noise $p 0 4" > "$tmp/noise-first.tl"
            fi
            awk '!/^noise / { print }' "$src" >> "$tmp/noise-first.tl"
            build/$b/harness --play "$tmp/noise-first.tl" --merged "$tmp/noise-merged.tl" > "$tmp/noise.log" 2>&1
            rc=$?
            n=$((n + 1))
            [ $rc -eq 0 ] && cmp -s "$src" "$tmp/noise-merged.tl" || bad="$bad $p.$fs:$rc"
        done
    done
    if [ -z "$bad" ]; then ok "noise canonical $b" "$n records: default omitted, headers before patch"
    else fail "noise canonical $b" "$bad"; indent < "$tmp/noise.log"; fi
done
bad= n=0
for spec in 'noise colourlp 0 4;noise colourlp 0 4:second noise statement for operator 0' \
        "noise colourlp 4 4:OP '4' is not" 'noise colourlp 2 4:noise names operator 2' \
        "noise colourlp 0 0:SHIFT '0' is not" "noise colourlp 0 16:SHIFT '16' is not" \
        "noise colourlp 0 -1:SHIFT '-1' is not" "noise colourlp 0 +1:SHIFT '+1' is not" \
        "noise colourlp 0 noninteger:SHIFT 'noninteger' is not" 'noise colourlp 0:noise takes PATCH' \
        'noise colourlp 0 4 4:noise takes PATCH' 'noise nobody 0 4:noise operator 0 needs WAVE 10 or 11'; do
    n=$((n + 1))
    awk -v r="${spec%%:*}" '/^voice /{ k = split(r, a, ";"); for (i = 1; i <= k; i++) print a[i] } { print }' \
        tests/golden/48000/colourlp.tl > "$tmp/noise.tl"
    "$h" --play "$tmp/noise.tl" > "$tmp/noise.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "${spec#*:}" "$tmp/noise.log" || bad="$bad [${spec%%:*}]:$rc"
done
for wave in 0 4 9; do
    n=$((n + 1))
    awk -v w="$wave" '/^patch colourlp 0 / { $15 = w } /^voice / { print "noise colourlp 0 4" } { print }' \
        tests/golden/48000/colourlp.tl > "$tmp/noise.tl"
    "$h" --play "$tmp/noise.tl" > "$tmp/noise.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q 'noise operator 0 needs WAVE 10 or 11' "$tmp/noise.log" || bad="$bad wave$wave:$rc"
done
if [ -z "$bad" ]; then ok "noise refused" "$n lines: exit 1, each its message"
else fail "noise refused" "$bad"; fi

# seven peg refusals, fixed-only opt-in identity and sensitivity.
bad= n=0
for spec in 'peg bell 0 10 0 0 0 0 100 10;peg bell 0 10 0 0 0 0 100 10:second peg statement' \
        "peg bell 4801 10 0 0 0 0 0 10:L1 '4801' is not -4800 to 4800" "peg bell 0 10 0 0 0 0 -4801 10:L4 '-4801' is not -4800 to 4800" \
        "peg bell 0 60001 0 0 0 0 100 10:T1 '60001' is not 0 to 60000" 'peg bell 0 10 0 0 0 0 100 10 4:names operator 2' \
        'peg bell 0 10 0 0 0 0 100 10 0:is not 1 to 15' 'peg bell 0 10 0 0 0 0 100:peg takes'; do
    n=$((n + 1))
    awk -v r="${spec%%:*}" '/^voice /{ k = split(r, a, ";"); for (i = 1; i <= k; i++) print a[i] } { print }' \
        tests/golden/48000/bell.tl > "$tmp/peg.tl"
    "$main" --play "$tmp/peg.tl" > "$tmp/peg.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "${spec#*:}" "$tmp/peg.log" || bad="$bad [${spec%%:*}]:$rc"
done
if [ -z "$bad" ]; then ok "peg refused" "$n lines: exit 1, each its message"
else fail "peg refused" "not exit 1 with its message:$bad"; fi
for b in $builds; do
    bad=
    for fs in 48000 44100; do
        awk '/^voice /{ print "peg fixedop 1200 100 -1200 100 600 100 0 100 2" } { print }' \
            tests/golden/$fs/fixedop.tl > "$tmp/peg.fixed.tl"
        build/$b/harness --play "$tmp/peg.fixed.tl" > "$tmp/peg.log" 2>&1
        rc=$?
        [ $rc -eq 0 ] || bad="$bad fixed.$fs:$rc"
        awk '/^peg /{ $9 = -1199 } { print }' tests/golden/$fs/pegslide.tl > "$tmp/peg.changed.tl"
        build/$b/harness --play "$tmp/peg.changed.tl" > "$tmp/peg.log" 2>&1
        rc=$?
        [ $rc -eq 3 ] || bad="$bad changed.$fs:$rc"
    done
    if [ -z "$bad" ]; then ok "peg opt-in $b" "fixed-only hash; changed L4: exit 3 at both rates"
    else fail "peg opt-in $b" "differs:$bad"; fi
done

# eight reader refusals; a live envelope whose rounded cents stay
# zero must preserve static-filter PCM, including retriggers/filter events.
bad= n=0
for spec in 'filter bell lp 300000 1500;fenv bell 20 600 0 200 4800 0;fenv bell 20 600 0 200 4800 0:second fenv statement' \
        'filter bell lp 300000 1500;fenv bell 60001 600 0 200 4800 0:ATTACK_MS' \
        'filter bell lp 300000 1500;fenv bell 20 600 1001 200 4800 0:SUSTAIN_PERMILLE' \
        'filter bell lp 300000 1500;fenv bell 20 600 0 200 9601 0:DEPTH_CENTS' \
        'filter bell lp 300000 1500;fenv bell 20 600 0 200 4800 2001:TRACK_PERMILLE' \
        'filter bell lp 300000 1500;fenv bell 20 600 0 200 4800 0 0:REF_MILLIHZ' \
        'filter bell lp 300000 1500;fenv bell 20 600 0 200 4800:fenv takes' \
        'fenv bell 20 600 0 200 4800 0:needs a filter statement'; do
    n=$((n + 1))
    awk -v r="${spec%%:*}" '/^voice /{ k = split(r, a, ";"); for (i = 1; i <= k; i++) print a[i] } { print }' \
        tests/golden/48000/bell.tl > "$tmp/fenv.tl"
    "$main" --play "$tmp/fenv.tl" > "$tmp/fenv.log" 2>&1
    rc=$?
    [ $rc -eq 1 ] && grep -q "${spec#*:}" "$tmp/fenv.log" || bad="$bad [${spec%%:*}]:$rc"
done
if [ -z "$bad" ]; then ok "fenv refused" "$n lines: exit 1, each its message"
else fail "fenv refused" "not exit 1 with its message:$bad"; fi
for b in $builds; do
    bad=
    for fs in 48000 44100; do
        for name in lowpass fretrig sweep; do
            awk -v p="$name" '/^voice /{ print "fenv " p " 60000 60000 1000 60000 1 0" } { print }' \
                tests/golden/$fs/$name.tl > "$tmp/fenv.zero.tl"
            build/$b/harness --play "$tmp/fenv.zero.tl" > "$tmp/fenv.log" 2>&1
            rc=$?
            [ $rc -eq 0 ] || bad="$bad zero.$name.$fs:$rc"
        done
        awk '/^fenv /{ $8 = 999 } { print }' tests/golden/$fs/ftrack.tl > "$tmp/fenv.changed.tl"
        build/$b/harness --play "$tmp/fenv.changed.tl" > "$tmp/fenv.log" 2>&1
        rc=$?
        [ $rc -eq 3 ] || bad="$bad changed.$fs:$rc"
    done
    if [ -z "$bad" ]; then ok "fenv opt-in $b" "live-zero hashes; changed track: exit 3 at both rates"
    else fail "fenv opt-in $b" "differs:$bad"; fi
done

# WAV output: 44 header bytes and 2 per sample; refusals write nothing
res=$("$main" --play tests/golden/48000/sine.tl --wav "$tmp/sine.wav" 2>&1)
rc=$?
samples=$(echo "$res" | sed -n 's/.* samples=\([0-9]*\) .*/\1/p')
size=$(wc -c < "$tmp/sine.wav" 2>/dev/null | tr -d ' ')
if [ $rc -eq 0 ] && [ -n "$samples" ] && [ "$size" = $((44 + 2 * samples)) ]; then
    ok "wav" "$size bytes, $samples samples"
else
    fail "wav" "exit $rc, ${size:-no} bytes, ${samples:-?} samples"
fi
res=$("$main" --play tests/golden/space/pan.tl --wav "$tmp/pan.wav" 2>&1)
rc=$?
samples=$(echo "$res" | sed -n 's/.* samples=\([0-9]*\) .*/\1/p')
size=$(wc -c < "$tmp/pan.wav" 2>/dev/null | tr -d ' ')
chans=$(od -An -tu2 -j 22 -N 2 "$tmp/pan.wav" 2>/dev/null | tr -d ' ')
case $res in *" channels=2"|*" channels=2 "*) line2=yes ;; *) line2=no ;; esac
if [ $rc -eq 0 ] && [ -n "$samples" ] && [ "$size" = $((44 + 4 * samples)) ] && [ "$chans" = 2 ] && [ $line2 = yes ]; then
    ok "wav stereo" "$size bytes, $samples frames, 2 channels"
else
    fail "wav stereo" "exit $rc, ${size:-no} bytes, ${samples:-?} frames, ${chans:-?} channels, channels=2 on the PLAY line: $line2"
fi
# Raw field routes: a field WAV has its
# 4 or 16 channels of 2 bytes per frame; the double model's differential
# test, the decoder's exact test and the headroom test pass on every
# build; the binaural records replay with the build's table, or each is
# refused without one
for rc_ in amb1:4 amb3-yaw:16; do
    r=${rc_%:*} c=${rc_#*:}
    res=$("$main" --play "tests/golden/space/$r.tl" --wav "$tmp/$r.wav" 2>&1)
    rc=$?
    samples=$(echo "$res" | sed -n 's/.* samples=\([0-9]*\) .*/\1/p')
    size=$(wc -c < "$tmp/$r.wav" 2>/dev/null | tr -d ' ')
    chans=$(od -An -tu2 -j 22 -N 2 "$tmp/$r.wav" 2>/dev/null | tr -d ' ')
    case $res in *" channels=$c"|*" channels=$c "*) line2=yes ;; *) line2=no ;; esac
    if [ $rc -eq 0 ] && [ -n "$samples" ] && [ "$size" = $((44 + 2 * c * samples)) ] && [ "$chans" = "$c" ] && [ $line2 = yes ]; then
        ok "wav $r" "$size bytes, $samples frames, $c channels"
    else
        fail "wav $r" "exit $rc, ${size:-no} bytes, ${samples:-?} frames, ${chans:-?} channels, channels=$c on the PLAY line: $line2"
    fi
    rm -f "$tmp/$r.wav"
done
for b in $builds; do
    build/$b/harness --field-test > "$tmp/field.log" 2>&1
    rc=$?
    if [ $rc -eq 0 ] && [ "$(tail -n 1 "$tmp/field.log")" = "FIELD ok" ]; then
        ok "field test $b" "$(grep -c '^FIELD .* ok$' "$tmp/field.log") tests"
    else
        fail "field test $b" "exit $rc"
        grep -v ' ok$' "$tmp/field.log" | indent
    fi
done
for b in $builds; do
    n=0 played=0 refused=0 bad=
    for f in tests/golden/space/binaural/*.tl; do
        n=$((n + 1))
        build/$b/harness --play "$f" > "$tmp/bin.log" 2>&1
        rc=$?
        if [ $rc -eq 0 ]; then played=$((played + 1))
        elif [ $rc -eq 1 ] && grep -q 'no binaural table in this build' "$tmp/bin.log"; then refused=$((refused + 1))
        else bad="$bad ${f##*/}:$rc"; fi
    done
    if [ -n "$bad" ]; then fail "binaural $b" "differs:$bad"
    elif [ $played -eq $n ]; then ok "binaural $b" "$n records replay with the build's table"
    elif [ $refused -eq $n ]; then ok "binaural $b" "$n records refused: no table in this build"
    else fail "binaural $b" "$played replay, $refused refused"; fi
done
(ulimit -f 1; trap '' XFSZ; exec "$main" --play tests/golden/48000/sine.tl --wav "$tmp/cut.wav") > "$tmp/cut.log" 2>&1
rc=$?
if [ $rc -eq 5 ] && [ ! -e "$tmp/cut.wav" ] && [ ! -e "$tmp/cut.wav.tmp" ]; then
    ok "wav write error" "exit 5, no destination or temporary under a 1-block file size limit"
else fail "wav write error" "exit $rc or partial file remains"; indent < "$tmp/cut.log"; fi
(ulimit -f 1; trap '' XFSZ; exec "$main" --play tests/golden/48000/wtframe.tl --merged "$tmp/cut.tl") > "$tmp/cut-tl.log" 2>&1
rc=$?
if [ $rc -eq 5 ] && [ ! -e "$tmp/cut.tl" ] && [ ! -e "$tmp/cut.tl.tmp" ]; then
    ok "record write error" "exit 5, no destination or temporary under a 1-block file size limit"
else fail "record write error" "exit $rc or partial file remains"; indent < "$tmp/cut-tl.log"; fi
# a host path problem is distinct from a refused record on each build.
for b in $builds; do
    for mode in input stems wav merged; do
        case $mode in
            input) set -- --play "$tmp/absent.tl" ;;
            stems) set -- --play tests/golden/space/panlaw.tl --stems "$tmp/absent.wav" ;;
            wav) set -- --play tests/golden/48000/sine.tl --wav "$tmp/absent/out.wav" ;;
            merged) set -- --play tests/golden/48000/sine.tl --merged "$tmp/absent/out.tl" ;;
        esac
        build/$b/harness "$@" > "$tmp/host-error.log" 2>&1
        rc=$?
        if [ $rc -eq 5 ] && [ ! -e "$tmp/absent" ]; then
            ok "host $mode $b" "exit 5, no output artifact"
        else
            fail "host $mode $b" "exit $rc or output artifact"
            indent < "$tmp/host-error.log"
        fi
    done
done
for f in tests/golden/48000/sine.tl tests/golden/space/pan.tl tests/golden/gen/genambient-48000.tl; do
    "$main" --play "$f" --merged "$tmp/atomic.tl" > "$tmp/atomic.log" 2>&1
    rc=$?
    if [ $rc -eq 0 ] && grep -q 'match=yes' "$tmp/atomic.log" && [ ! -e "$tmp/atomic.tl.tmp" ]; then
        "$main" --play "$tmp/atomic.tl" > "$tmp/atomic-replay.log" 2>&1
        rc=$?
        if [ $rc -eq 0 ] && grep -q 'match=yes' "$tmp/atomic-replay.log"; then
            ok "atomic record $f" "merged replay match=yes"
        else
            fail "atomic record $f" "replay exit $rc or hash mismatch"
            indent < "$tmp/atomic-replay.log"
        fi
    else
        fail "atomic record $f" "write exit $rc, hash mismatch or temporary remains"
        indent < "$tmp/atomic.log"
    fi
    rm -f "$tmp/atomic.tl"
done
"$main" --play tests/golden/edge/edge-fsmax.tl --wav "$tmp/fsmax.wav" > "$tmp/fsmax.log" 2>&1
rc=$?
if [ $rc -eq 1 ] && [ ! -e "$tmp/fsmax.wav" ]; then ok "wav too fast" "fs 4294967295: exit 1, no file"
else fail "wav too fast" "exit $rc"; indent < "$tmp/fsmax.log"; fi

# The gate can fail: other knobs forced through it, and refused without that
if build s10e8p0v0-negctl SINE_BITS=10 EXP_BITS=8 PHASE64=0 ENV64=0 EXTRA=-DGATE_BUILD=1 VARIANT=-negctl; then
    build/s10e8p0v0-negctl/harness --check > "$tmp/neg.log" 2>&1
    rc=$?
    if [ $rc -eq 4 ] && grep -q '^GATE FAIL' "$tmp/neg.log"; then
        ok "gate negative control" "$(tail -n 1 "$tmp/neg.log"), exit 4"
    else
        fail "gate negative control" "exit $rc: the gate did not fail"
    fi
fi
if build s10e8p0v0 SINE_BITS=10 EXP_BITS=8 PHASE64=0 ENV64=0; then
    build/s10e8p0v0/harness --check > "$tmp/neg.log" 2>&1
    rc=$?
    if [ $rc -eq 2 ]; then ok "gate other knobs" "refused, exit 2"
    else fail "gate other knobs" "exit $rc"; fi
    build/s10e8p0v0/harness --check lfo > "$tmp/neg.log" 2>&1
    rc=$?
    if [ $rc -eq 2 ] && grep -q '^--check lfo needs' "$tmp/neg.log"; then
        ok "lfo subset other knobs" "refused, exit 2"
    else fail "lfo subset other knobs" "exit $rc"; fi
fi

# integer engine tables, the approved pin and the external libm oracle.
bad=
for b in $builds; do
    if ! cmp -s "build/$b/fm_tables.h" src/host/fm_tables_s12e10.h; then
        bad="$bad $b:header"
    fi
    for fs in 48000 44100; do
        if ! grep -q '^CONFIG .* table_hash=58021f1150d3c8d2 ' "$tmp/gate.$b.$fs"; then
            bad="$bad $b:$fs:CONFIG"
        fi
    done
done
if make -s $knobs build/s12e10p1v1/gen_tables > "$tmp/tables-build.log" 2>&1; then
    for sb in 8 9 10 11 12; do
        for eb in 8 10; do
            if build/s12e10p1v1/gen_tables "$sb" "$eb" > "$tmp/tables-libm.h" &&
               build/s12e10p1v1/gen_tables_int "$sb" "$eb" > "$tmp/tables-int.h" &&
               tail -n +2 "$tmp/tables-libm.h" > "$tmp/tables-libm-body.h" &&
               tail -n +2 "$tmp/tables-int.h" > "$tmp/tables-int-body.h" &&
               cmp -s "$tmp/tables-libm-body.h" "$tmp/tables-int-body.h"; then :
            else bad="$bad s${sb}e${eb}:libm"; fi
        done
    done
    if python3 -B tests/tables_oracle.py build/s12e10p1v1/gen_tables build/s12e10p1v1/gen_tables_int > "$tmp/tables-oracle.log" 2>&1 &&
       awk '($1 == "sine" && $2 == 12) || ($1 == "exp2" && $2 == 10) {
                rows++; if ($4 != 0 || $5 != 0 || $6 != 0) bad = 1
            } END { exit rows != 2 || bad }' "$tmp/tables-oracle.log"; then :
    else bad="$bad decimal-oracle"; indent < "$tmp/tables-oracle.log"; fi
    # One exp2 entry plus one must fail both the pin and the libm comparison.
    if python3 -B - src/host/fm_tables_s12e10.h "$tmp/tables-mutant.h" <<'PY'
import pathlib, sys
source = pathlib.Path(sys.argv[1]).read_text()
old = 'fm_exp2_tbl[1025] = {\n    1073741824,'
assert source.count(old) == 1
pathlib.Path(sys.argv[2]).write_text(source.replace(old, old.replace('1073741824', '1073741825')))
PY
    then
        if tail -n +2 "$tmp/tables-mutant.h" > "$tmp/tables-mutant-body.h"; then
            if cmp -s "$tmp/tables-mutant.h" src/host/fm_tables_s12e10.h; then pin_rc=0; else pin_rc=$?; fi
            if cmp -s "$tmp/tables-mutant-body.h" "$tmp/tables-libm-body.h"; then libm_rc=0; else libm_rc=$?; fi
            if [ "$pin_rc" -ne 1 ] || [ "$libm_rc" -ne 1 ]; then
                bad="$bad exp2-mutant-comparison:$pin_rc,$libm_rc"
            fi
        else bad="$bad exp2-mutant-extraction"; fi
    else bad="$bad exp2-mutant-not-applied"; fi
else
    bad="$bad libm-validator-build"
    indent < "$tmp/tables-build.log"
fi
if [ -z "$bad" ]; then
    ok "tables" "3 integer headers pinned; 10 libm pairs; decimal 12/10 exact; exp2 mutant caught; CONFIG hash 58021f1150d3c8d2"
else fail "tables" "$bad"; fi

# The binaural decoder's filters: gen_binaural_int prints the same
# sp_binaural_tables.h on the three builds, its sha256 pinned here; the right
# ear's design is the left's mirrored, bit for bit; and binaural_ab.py
# design-check holds the filters to that script's double design within its
# acceptance criterion, where python3 has numpy.
bin_sha256=01e21924b17bf7da48c92698e31dfbfdad91ff25ae98a2d7b8daec8ee0509f60
sha256=$(command -v sha256sum || echo shasum -a 256)
bintab() { # BUILD MAKE-ARGUMENTS...: build/BUILD/sp_binaural_tables.h's sha256
    local b=$1
    shift
    make -B -s $knobs "$@" build/$b/sp_binaural_tables.h > "$tmp/bin.log" 2>&1 &&
        $sha256 < build/$b/sp_binaural_tables.h | cut -c 1-64
}
bad=
for b in $builds; do
    case $b in
        *-portable) got=$(bintab $b EXTRA=-DFP_MATH_FORCE_PORTABLE VARIANT=-portable) ;;
        *-ubsan)    got=$(bintab $b OPT="-O1 -g -fsanitize=undefined -fno-sanitize-recover=all" VARIANT=-ubsan) ;;
        *)          got=$(bintab $b) ;;
    esac
    if [ "$got" != $bin_sha256 ]; then
        bad="$bad $b:${got:-no header}"
        indent < "$tmp/bin.log"
    fi
done
if [ -z "$bad" ]; then ok "binaural tables" "3 builds, sha256 ${bin_sha256:0:16}"
else fail "binaural tables" "not the pinned sha256:$bad"; fi
if build/s12e10p1v1/gen_binaural_int mirror > "$tmp/mirror.log" 2>&1; then
    ok "binaural mirror" "$(grep -c ': yes' "$tmp/mirror.log") designs, bit for bit"
else
    fail "binaural mirror" "exit $?"
    indent < "$tmp/mirror.log"
fi
if python3 -B -c 'import numpy' > /dev/null 2>&1; then
    python3 -B tools/music/binaural_ab.py design-check build/s12e10p1v1/sp_binaural_tables.h > "$tmp/design.log" 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then
        ok "binaural design" "$(tail -n 1 "$tmp/design.log" | sed 's/^design-check ok: //')"
        sed '$d' "$tmp/design.log" | indent
    else
        fail "binaural design" "design-check exit $rc"
        indent < "$tmp/design.log"
    fi
else
    skip "binaural design" "no python3 with numpy"
fi

# cross objects use undefined names that actually occur in
# relocations; host objects keep the reviewed nm-only check. Strip relocation
# addends and intersect with nm -u so locals and section names never become
# imports. Every tool must succeed; a failed or empty dump is not evidence.
printf '%s\n' 'double twice(double x) { return x * 2.0; }' > "$tmp/neg1.c"
printf '%s\n' 'int f(int x) { double d = x; d *= 1.5; return (int)d; }' > "$tmp/neg2.c"
printf '%s\n' 'double sin(double);' 'long lround(double);' \
    'static const double t[4] = { 0.25, 0.5, 1.0, 2.0 };' \
    'long g(unsigned i) { return lround(sin(t[i & 3u])); }' > "$tmp/neg3.c"
printf '%s\n' '#include <stdint.h>' \
    'int64_t f(int64_t a, int64_t b) { return a / b; }' > "$tmp/neg4.c"
printf '%s\n' 'static int counter; int f(void) { return ++counter; }' > "$tmp/neg5.c"

imports() { # LABEL CC NM SED ALLOW OBJDUMP SIGNED FLAGS...; '-' means host
    local label=$1 cc=$2 nm=$3 names=$4 allow=$5 dump=$6 signed=$7
    local src extra neg=0 what list controls=4 sources tool_error mutable object size_tool
    # Reviewed inter-module calls and freestanding-C memory primitives.
    # Allowed library calls and the compiler-generated memory primitive:
    # gcc 13 on x86-64 makes one of the accumulator zeroing loop at -O2; a freestanding-C primitive, no float.
    local lib="fm_render_q30 fm_voice_active fm_voice_needs_clock memset"
    # Field routing also calls spatial helpers.
    local sp="sp_coord_at sp_isqrt64 sp_stereo_init sp_udiv64_32"
    shift 7
    if ! command -v "$cc" > /dev/null 2>&1; then
        skip "imports $label" "no $cc here"
        return
    fi
    if ! command -v "$nm" > /dev/null 2>&1; then
        fail "imports $label" "no $nm here"
        return
    fi
    if [ "$dump" != - ] && ! command -v "$dump" > /dev/null 2>&1; then
        fail "imports $label" "no $dump here"
        return
    fi
    printf '%s\n' $allow | sort -u > "$tmp/allow"
    printf '%s\n' $allow $lib fx_voice_process fx_master | sort -u > "$tmp/allow_space"  # mutable insert/send hook and master; the const reader remains public
    printf '%s\n' $allow $lib | sort -u > "$tmp/allow_fx"
    printf '%s\n' $allow $lib $sp fx_voice_process fx_field_returns fx_master_post | sort -u > "$tmp/allow_field"
    sources=(src/host/fm_a1.c src/space/space.c src/fx/fx.c src/space/field.c "$tmp/neg1.c" "$tmp/neg2.c" "$tmp/neg3.c")
    if [ "$dump" != - ]; then sources+=("$tmp/neg4.c"); controls=5; fi
    sources+=("$tmp/neg5.c")
    for src in "${sources[@]}"; do
        rm -f "$tmp/obj.o" "$tmp/undef" "$tmp/relocs" "$tmp/mutable"
        tool_error= mutable=
        case $src in
            src/host/fm_a1.c)          what="imports $label" list=allow ;;
            src/space/space.c) what="imports $label space" list=allow_space ;;
            src/fx/fx.c)       what="imports $label fx" list=allow_fx ;;
            src/space/field.c) what="imports $label field" list=allow_field ;;
            *)                what= list=allow ;;
        esac
        if "$cc" "$@" -Isrc/host -I. -c "$src" -o "$tmp/obj.o" > "$tmp/cc.log" 2>&1; then
            if "$nm" -u "$tmp/obj.o" > "$tmp/nm.log" 2>&1; then
                awk 'NF { print $NF }' "$tmp/nm.log" | sed "$names" | sort -u > "$tmp/undef"
            else tool_error="$nm failed"; fi
            # The same writable-symbol rule as gen/check.py, including local
            # statics. A failed nm is not an empty (passing) symbol list.
            if [ "$dump" = - ] && [ "$(uname -s)" = Darwin ]; then
                if "$nm" -m "$tmp/obj.o" > "$tmp/symbols.log" 2>&1; then
                    awk '/\(__DATA,__(data|bss|common)\)|\(common\)/' "$tmp/symbols.log" > "$tmp/mutable"
                else tool_error="$nm defined-symbol check failed"; fi
            else
                if "$nm" "$tmp/obj.o" > "$tmp/symbols.log" 2>&1; then
                    awk '/[[:space:]][bBdDgGsScC][[:space:]]/' "$tmp/symbols.log" > "$tmp/mutable"
                else tool_error="$nm defined-symbol check failed"; fi
            fi
            if [ -z "$tool_error" ]; then
                mutable=$(awk 'NF { print $NF }' "$tmp/mutable" | sed "$names" | sort -u | tr '\n' ' ')
                mutable=${mutable% }
            fi
            if [ "$dump" != - ]; then
                if "$dump" -r "$tmp/obj.o" > "$tmp/dump.log" 2>&1 && [ -s "$tmp/dump.log" ]; then
                    awk '$2 ~ /^R_/ {print $3}' "$tmp/dump.log" | sed 's/[-+]0x.*//' | sort -u > "$tmp/relocs"
                    if [ -z "$tool_error" ]; then
                        comm -12 "$tmp/undef" "$tmp/relocs" > "$tmp/used"
                        mv "$tmp/used" "$tmp/undef"
                    fi
                else tool_error="$dump failed or returned an empty dump"; fi
            fi
            if [ -n "$tool_error" ]; then extra=tool-failed
            else extra=$(echo $(comm -23 "$tmp/undef" "$tmp/$list")); fi
        else extra=rejected; fi
        if [ "$extra" = tool-failed ]; then
            fail "${what:-import control $label}" "$tool_error"
        elif [ "$src" = "$tmp/neg5.c" ]; then
            # Compilation failure or a bad import cannot stand in for detecting
            # the actual writable static; this control must expose its counter.
            if [ "$extra" != rejected ] && [ -n "$mutable" ]; then
                neg=$((neg + 1))
                printf '      mutable control: %s\n' "$mutable"
            fi
        elif [ "$src" = "$tmp/neg4.c" ]; then
            # Unlike the floating controls, this must compile, expose the
            # signed helper relocation and be rejected by the used-name set.
            if [ "$extra" != rejected ] && [ -n "$extra" ] &&
               grep -qx "$signed" "$tmp/relocs" && grep -qx "$signed" "$tmp/undef"; then
                neg=$((neg + 1))
                printf '      signed control: relocation %s; outside list: %s\n' "$signed" "$extra"
            fi
        elif [ -z "$what" ]; then
            [ -n "$extra" ] && neg=$((neg + 1))
        elif [ "$extra" = rejected ]; then
            fail "$what" "${src##*/} does not compile"
            indent < "$tmp/cc.log"
        elif [ -n "$mutable" ]; then
            object=${src##*/}; object=${object%.c}.o
            fail "mutable $label $object" "$mutable"
        elif [ -z "$extra" ]; then
            ok "$what" "$(echo $(cat "$tmp/undef")) mutable none"
            # report object size without adding a gate or skip.
            size_tool=${nm%nm}size
            if command -v "$size_tool" > /dev/null 2>&1; then
                "$size_tool" "$tmp/obj.o" 2>/dev/null | tail -n 1 | indent
            fi
        else fail "$what" "outside the list: $extra"; fi
    done
    if [ $neg -eq $controls ]; then ok "import controls $label" "$controls of $controls fail"
    else fail "import controls $label" "$((controls - neg)) of $controls pass: the gate proves nothing"; fi
}

host="__divti3 __udivti3"
# On macOS clang writes field.c's zeroing loops as bzero, its name for memset.
case $(uname -s)/$(uname -m) in
    Darwin/arm64) imports arm64 "${CC:-cc}" nm 's/^_//;s/^bzero$/memset/' "$host" - - -std=c99 -O2 -mgeneral-regs-only $defs ;;
    Linux/x86_64) imports x86-64 "${CC:-cc}" nm '' "$host" - - -std=c99 -O2 -mgeneral-regs-only $defs ;;
    *)            skip "imports host" "no reviewed list for $(uname -s) $(uname -m)" ;;
esac
# Ignore signed helpers that GCC 13 declares without relocations.
# A real signed division now fails the fourth control on each cross target.
# memcpy (Cortex-M0+, RV32IMAC) copies the patch in fm_voice_init. GCC 16.1 also emits
# it on Cortex-M0+ for fp_math.h's 128-bit values in fm_exp2_q48, fm_set_inc_m and fp_mul, and on RV32IMAC eight calls.
rv_dump=riscv64-elf-objdump
if ! command -v "$rv_dump" > /dev/null 2>&1; then rv_dump=riscv64-unknown-elf-objdump; fi
imports cortex-m0+ arm-none-eabi-gcc arm-none-eabi-nm '' \
    "__aeabi_llsl __aeabi_llsr __aeabi_lmul __aeabi_uldivmod __clzdi2 __gnu_thumb1_case_uqi memcpy" \
    arm-none-eabi-objdump __aeabi_ldivmod -std=c99 -ffreestanding -mcpu=cortex-m0plus -mthumb -Os $defs
imports cortex-m4 arm-none-eabi-gcc arm-none-eabi-nm '' "__aeabi_uldivmod" \
    arm-none-eabi-objdump __aeabi_ldivmod -std=c99 -ffreestanding -mcpu=cortex-m4 -mthumb -Os $defs
imports rv32imac riscv64-elf-gcc riscv64-elf-nm '' "__ashldi3 __clzdi2 __lshrdi3 __udivdi3 memcpy" \
    "$rv_dump" __divdi3 -std=c99 -ffreestanding -march=rv32imac -mabi=ilp32 -Os $defs
# Count deferred generator import checks as one skip when cross compilers are absent.
deferred=$(grep -Ec '^DEFERRED +[^ ]+ +[^ ]+ +integer-imports:' "$tmp/gen.log")
if [ "$deferred" -gt 0 ]; then skip "gen cross imports" "$deferred targets deferred: no usable cross compiler"; fi
finish
