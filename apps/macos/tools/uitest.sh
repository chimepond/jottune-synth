#!/bin/sh
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
#
# uitest.sh: `make uitest` . Runs the app in script mode
# on SCRIPT (no audio device, offline at fixed frames), then replays every
# take it wrote in `harness --play`, which must match each take's hash. Then
# the positive controls: the same script with one control's setter dropped
# (FMS_UITEST_DROP, as if its binding were removed) must fail on that control,
# an operator field and a statement field  in turn. Last, the stereo
# picture's rows  against analyze.py overview on the same
# frames, and a comparator control: a 1.25 s window must not match.
#   uitest.sh APP_BINARY HARNESS SCRIPT OUT_DIR ANALYZE_PY
A=$1; H=$2; S=$3; O=$4; Z=$5
bad=0
rm -rf "$O"
mkdir -p "$O"
FMS_SCRIPT="$S" FMS_SCRIPT_OUT="$O/run" "$A" > "$O/run.txt" 2>&1 || bad=1
grep -E '^(FAIL|UITEST|RAWEDIT|FILTER_PARAMS|STATEMENT_PARAMS|OPERATOR_PARAMS|RECENT|IMPORT|EXPORT|FXHEADER|MIDIMAP|MPEUI|STEREO_NULL|WAVSAVE|SEEKUI|RECOVERY|LOCALFILES|SPACEUI|SPACEIMPORT|SPACEPREF|DEVICE_RATE|PATCHUNDO|PATCHAB|FAVORITES|LEARN|COMPRESSOR|FLANGER_PROFILE|CRUSHER|LIBRARY)' "$O/run.txt"
echo "steps: $(grep -c . "$O/run/log.txt") logged, $(grep -c '^check .* ok$' "$O/run/log.txt") checks ok"
n=0; nm=0; nc=0
for t in "$O/run"/*.tl "$O/run/checks"/*.tl; do
    [ -f "$t" ] || continue
    n=$((n + 1))
    case "$t" in "$O/run/checks/"*) nc=$((nc + 1));; esac
    if "$H" --play "$t" 2>&1 | grep -q 'match=yes'; then nm=$((nm + 1)); else bad=1; echo "take $t: harness NO"; fi
done
printf 'librarycheck\n' > "$O/library-controls.txt"
for control in library.search library.default library.variant record.playurl; do
    FMS_SCRIPT="$O/library-controls.txt" FMS_SCRIPT_OUT="$O/drop-$control" FMS_UITEST_DROP=$control "$A" > "$O/drop-$control.txt" 2>&1
    code=$?
    case "$control" in
        library.search) expected='^FAIL LIBRARY search ';;
        library.default) expected='^FAIL LIBRARY Keys default loads native record$';;
        library.variant) expected='^FAIL LIBRARY variant Standard accepted$';;
        record.playurl) expected='^FAIL LIBRARY play URL starts accepted load$';;
    esac
    if [ "$code" = 1 ] && grep -q "$expected" "$O/drop-$control.txt"; then
        echo "positive control ok: dropping $control fails its shared action"
    else bad=1; echo "positive control FAIL: dropping $control exited $code"; fi
done
echo "takes: $nm of $n replay in harness --play to their hash"
# Save's actual companion files, not a separate test-only writer.
for name in take full midi; do
    if "$H" --play "$O/run/$name.tl" --wav "$O/run/$name.expected.wav" > "$O/wav-$name.log" 2>&1 \
       && cmp -s "$O/run/$name.wav" "$O/run/$name.expected.wav"; then
        echo "WAV_SAVE $name byte_identical=yes"
    else bad=1; echo "WAV_SAVE $name FAIL"; fi
    rm -f "$O/run/$name.expected.wav"
done
[ "$n" -gt 0 ] || bad=1
checks=$(grep -c '^check .* ok$' "$O/run/log.txt")
if [ "$nc" -eq "$((2 * checks))" ] && [ "$checks" -gt 0 ]; then
    echo "check takes: $nc files preserve both takes of $checks checks"
else
    bad=1; echo "check takes FAIL: $nc files for $checks checks"
fi
# Two NAME-VALUE checks must keep distinct before/after pairs. Both are
# existing shape.position=pre actions at different script lines.
repeat_count=$(find "$O/run/checks" -name '*-shape.position-pre-?.tl' | wc -l | tr -d ' ')
if [ "$repeat_count" -eq 4 ]; then
    echo "check takes control ok: repeated shape.position=pre retains four files"
else
    bad=1; echo "check takes control FAIL: repeated shape.position=pre has $repeat_count files"
fi
sed -n '/^# ---- asymmetric shape and pre-filter placement/,$p' "$S" > "$O/instrument-controls.txt"
sed -n '/^# ---- per-channel controller maps/,$p' "$S" > "$O/midi-controls.txt"
sed -n '/^# ---- spatial header controls and live geometry/,$p' "$S" > "$O/space-controls.txt"
for d in op1.index lfo.mask_pitch mod.0.amount record.openurl record.seek filter.cutoff filter.q lfo.delay peg.l1 fenv.depth shape.kind shape.position amp.attack amp.decay amp.sustain amp.release egloop.0.op kstype.0.op kstype.0.target kstype.0.right kstype.2.left fbtype.1.op fbtype.1.type fx.gain fx.gain.db10 fx.dither fx.dither.seed fx.pingpong fx.pingpong.time fx.pingpong.feedback fx.pingpong.lowpass fx.pingpong.highpass midi.ctl.1 midi.ctl.2 midi.ctl.3 midi.ctl.4 midi.ctl.5 midi.ctl.6 midi.ctl.7 midi.ctl.8 midi.ctl.9 midi.ctl.10 midi.ctl.11 midi.ctl.12 midi.ctl.13 midi.ctl.14 midi.ctl.15 midi.ctl.16 midi.mpe space.field space.block space.near space.order space.panlaw.enabled space.panlaw space.pinna space.pinna.0 space.pinna.1 space.pinna.2 space.pinna.3 space.pinna.4 space.yaw.enabled space.yaw pos.x pos.y pos.z pos.voice pos.duration live.yaw fx.compressor fx.compressor.threshold fx.compressor.ratio fx.compressor.attack fx.compressor.release fx.compressor.makeup send.duck fx.flanger fx.flanger.base fx.flanger.depth fx.flanger.rate fx.flanger.feedback fx.flanger.invert send.flanger fx.decodable fx.crush live.crush.bits live.crush.rate_div; do
    control_script=$S
    case "$d" in shape.*|amp.*|egloop.*|kstype.*|fbtype.*|fx.*|send.duck|send.flanger|live.crush.*) control_script=$O/instrument-controls.txt;; esac
    case "$d" in midi.ctl.*|midi.mpe) control_script=$O/midi-controls.txt;; esac
    case "$d" in space.*|pos.*|live.yaw) control_script=$O/space-controls.txt;; esac
    FMS_SCRIPT="$control_script" FMS_SCRIPT_OUT="$O/drop-$d" FMS_UITEST_DROP=$d "$A" > "$O/drop-$d.txt" 2>&1
    code=$?
    if [ $code = 1 ] && { grep -q "^FAIL check $d .*hook=SAME" "$O/drop-$d.txt" || { [ "$d" = record.openurl ] && grep -q "^FAIL expect title" "$O/drop-$d.txt"; } || { [ "$d" = record.seek ] && grep -q "^FAIL SEEKUI missing request/busy refusal" "$O/drop-$d.txt"; }; }; then
        echo "positive control ok: dropping $d's setter fails $(grep -c '^FAIL' "$O/drop-$d.txt") step(s)"
    else
        bad=1; echo "positive control FAIL: dropping $d's setter exited $code"
    fi
done
printf 'spaceimportcheck\n' > "$O/import-controls.txt"
FMS_SCRIPT="$O/import-controls.txt" FMS_SCRIPT_OUT="$O/drop-values" FMS_UITEST_DROP=values "$A" > "$O/drop-values.txt" 2>&1
code=$?
if [ "$code" = 1 ] && grep -q '^FAIL SPACEIMPORT defaults' "$O/drop-values.txt"; then
    echo "positive control ok: dropping values setter fails header/defaults restoration"
else bad=1; echo "positive control FAIL: dropping values setter exited $code"; fi
printf 'journalcheck\n' > "$O/journal-controls.txt"
FMS_SCRIPT="$O/journal-controls.txt" FMS_SCRIPT_OUT="$O/drop-journal" FMS_UITEST_DROP=patch.journal "$A" > "$O/drop-journal.txt" 2>&1
code=$?
if [ "$code" = 1 ] && grep -q '^FAIL PATCHUNDO missing journal entry or failed undo$' "$O/drop-journal.txt"; then
    echo 'positive control ok: skipping patch journal fails undo'
else bad=1; echo "positive control FAIL: skipped patch journal exited $code"; fi
# Missing favorite Space restoration must fail the real load path.
printf 'favoritecheck\n' > "$O/favorite-controls.txt"
FMS_SCRIPT="$O/favorite-controls.txt" FMS_SCRIPT_OUT="$O/drop-favorite" FMS_UITEST_DROP=favorite.space "$A" > "$O/drop-favorite.txt" 2>&1
code=$?
if [ "$code" = 1 ] && grep -q '^FAIL FAVORITES restore favorite independent of import switch$' "$O/drop-favorite.txt"; then
    echo 'positive control ok: dropping favorite Space restoration fails load'
else bad=1; echo "positive control FAIL: favorite Space restoration exited $code"; fi
# Removing the actual atomic Learn commit must fail staged source/map checks.
printf 'learncheck\n' > "$O/learn-controls.txt"
FMS_SCRIPT="$O/learn-controls.txt" FMS_SCRIPT_OUT="$O/drop-learn" FMS_UITEST_DROP=learn.commit "$A" > "$O/drop-learn.txt" 2>&1
code=$?
if [ "$code" = 1 ] && grep -q '^FAIL LEARN stages source and map$' "$O/drop-learn.txt"; then
    echo 'positive control ok: dropping Learn commit fails source/map staging'
else bad=1; echo "positive control FAIL: Learn commit exited $code"; fi
# An actual unasked installation request is the device-policy control. Build
# only a second Swift app, reusing this suite's exact native objects. It runs
# solely in script mode with FakeDeviceRate; it cannot write hardware.
rate_app="$O/RateMutant.app"
printf 'devicecheck\n' > "$O/rate-controls.txt"
if make -C "$(dirname "$0")/.." APP="$rate_app" SWIFT_DEFS='-D FMS_RATE_MUTANT' "$rate_app/Contents/MacOS/IntegerFM" > "$O/rate-build.log" 2>&1; then
    FMS_SCRIPT="$O/rate-controls.txt" FMS_SCRIPT_OUT="$O/drop-rate" "$rate_app/Contents/MacOS/IntegerFM" > "$O/drop-rate.txt" 2>&1
    code=$?
    if [ "$code" = 1 ] && grep -q '^FAIL DEVICE_RATE automatic write before explicit click$' "$O/drop-rate.txt"; then
        echo 'positive control ok: unasked device switch fails before explicit click'
    else bad=1; echo "positive control FAIL: unasked device switch exited $code"; fi
else bad=1; echo 'positive control FAIL: device mutation did not build'; fi
# stereo_cmp JSON TSV: analyze.py's windows against the app's rows, to 1e-6
# in start, actual length, side-mid and correlation (JSON null against TSV nan).
stereo_cmp() {
    python3 -B - "$1" "$2" <<'PY'
import json, math, sys
w = json.load(open(sys.argv[1]))['result']['windows']
r = [l.split('\t') for l in open(sys.argv[2]).read().split('\n') if l]
ok = len(w) == len(r) and len(r) > 0
for a, b in zip(w, r):
    if len(b) != 4:
        ok = False
        continue
    for key, y in zip(('t', 'length', 'side_mid', 'corr'), b):
        x, y = a[key], float(y)
        if key == 'corr' and x is None:
            ok = ok and math.isnan(y)
        else:
            ok = ok and x is not None and math.isfinite(float(x)) and math.isfinite(y) and abs(float(x) - y) <= 1e-6
print('rows=%d windows=%d' % (len(r), len(w)))
sys.exit(0 if ok else 1)
PY
}
if [ ! -f "$O/run/panlaw.tsv" ] || [ ! -f "$Z" ]; then
    bad=1; echo "stereo: no panlaw.tsv from the script, or no analyze.py at $Z"
elif ! python3 -B -c 'import numpy' 2>/dev/null; then
    echo "stereo: SKIPPED, python3 has no numpy for analyze.py"
else
    for spec in panlaw:1 panlaw4:4 silence:4; do
        name=${spec%:*}; window=${spec#*:}
        if python3 -B "$Z" overview "$O/run/$name.wav" --window "$window" --json "$O/$name.json" > /dev/null 2>&1 \
           && c=$(stereo_cmp "$O/$name.json" "$O/run/$name.tsv"); then
            echo "STEREO ok $name $c, as analyze.py overview --window $window"
        else bad=1; echo "STEREO FAIL $name"; fi
    done
    if ! python3 -B "$Z" overview "$O/run/panlaw.wav" --window 1.25 --json "$O/panlaw-125.json" > /dev/null 2>&1; then
        bad=1; echo "stereo comparator control FAIL: analyzer failed"
    elif c=$(stereo_cmp "$O/panlaw-125.json" "$O/run/panlaw.tsv"); then
        bad=1; echo "stereo comparator control FAIL: a 1.25 s window matched ($c)"
    else echo "stereo comparator control ok: a 1.25 s window does not match ($c)"; fi
    # Both endpoints and the undefined/zero distinction must be compared.
    awk 'BEGIN { FS=OFS="\t" } NR==2 { $2=1 } { print }' "$O/run/panlaw.tsv" > "$O/stereo-wrong-length.tsv"
    awk 'BEGIN { FS=OFS="\t" } { $4=0; print }' "$O/run/silence.tsv" > "$O/stereo-wrong-null.tsv"
    if stereo_cmp "$O/panlaw.json" "$O/stereo-wrong-length.tsv" > /dev/null; then
        bad=1; echo "stereo length control FAIL"
    else echo "stereo length control ok: dropped final remainder refused"; fi
    if stereo_cmp "$O/silence.json" "$O/stereo-wrong-null.tsv" > /dev/null; then
        bad=1; echo "stereo null control FAIL"
    else echo "stereo null control ok: numeric zero is not undefined correlation"; fi
fi
if [ $bad = 0 ]; then echo "UITEST ok"; else echo "UITEST FAIL"; fi
exit $bad
