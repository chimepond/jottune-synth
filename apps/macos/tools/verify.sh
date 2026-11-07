#!/bin/sh
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
#
# verify.sh: `make verify`. For each record, and for two music pools
# (session.tl first, then the rest in glob order, as check.sh passes them),
# the bridge's hash at every chunk pattern (fms_verify) and `harness --play`'s
# hash, one line each; then two live takes recorded through the host's audio
# callback, replayed by the bridge and by the harness; the generator
# lifecycle fixtures; the dropped-statement labels of Instrument import
# (fms_verify --dropped); and the live MIDI translator : check (a), its events against midi2tl.py, with
# its two mutants caught (midi_check.py); check (b), a scripted MIDI stream
# under a live player, taken and replayed by the bridge and the harness; and
# the pitch scan (fms_verify --midi-scan). Exit 1 on any mismatch.
#   verify.sh FMS_VERIFY HARNESS BUILD_DIR MUSIC_DIR RECORD...
V=$1; H=$2; B=$3; M=$4; shift 4
bad=0
one() {   # one pool: the bridge, then the harness
    f=$(basename "$(dirname "$1")")/$(basename "$1")
    out=$("$V" "$@") || bad=1
    fh=$(echo "$out" | sed -n 's/.* hash=\([0-9a-f]*\) .*/\1/p')
    hp=$("$H" --play "$@" 2>&1)
    hh=$(echo "$hp" | sed -n 's/^PLAY hash=\([0-9a-f]*\) .*/\1/p')
    m=$(echo "$out" | sed -n 's/.* match=\([a-zA-Z-]*\) chunks_equal=\([a-zA-Z]*\) .*/match=\1 chunks_equal=\2/p')
    if [ -n "$fh" ] && [ "$fh" = "$hh" ]; then same=yes; else same=NO; bad=1; fi
    printf '%-28s bridge=%s harness=%s same=%s %s\n' "$f" "$fh" "$hh" "$same" "$m"
}
nrec=0; nsame=0
for r in "$@"; do   # the selected goldens (VSET)
    nrec=$((nrec + 1)); b0=$bad; bad=0
    one "$r"
    [ $bad = 0 ] && nsame=$((nsame + 1)); [ $b0 = 1 ] && bad=1
done
echo "goldens: $nsame of $nrec records match the harness at every chunk pattern"
for record in "$(dirname "$M")/examples/quench-standard.tl" "$(dirname "$M")/examples/quench-keys.tl"; do
    one "$record"
done
n=0; nr=0
for r in "$B"/recipes/*.tl; do   # the recipe records: the bridge against their hash statements
    [ -f "$r" ] || continue
    nr=$((nr + 1))
    if "$V" "$r" | grep -q 'match=yes chunks_equal=yes'; then n=$((n + 1)); else bad=1; echo "recipe $r: NO"; fi
done
echo "recipes: $n of $nr records match at every chunk pattern"
want=$(grep -c '^recipe ' "$(dirname "$M")/data/recipes.txt")   # Every recipe is built.
[ "$nr" = "$want" ] || { bad=1; echo "recipes: $nr records made, recipes.txt has $want"; }
# Every valid golden, including families outside the rendered VSET, and every
# recipe: metadata only. Bad fixtures deliberately are not loadable records.
G=$(dirname "$M")/tests/golden
set --
while IFS= read -r r; do set -- "$@" "$r"; done <<EOF
$(find "$G" -name '*.tl' ! -path '*/bad/*' -type f | LC_ALL=C sort)
EOF
for r in "$B"/recipes/*.tl; do [ ! -f "$r" ] || set -- "$@" "$r"; done
"$V" --lines "$B/lines-roundtrip.tl" "$@" || bad=1
"$B/lines_model" --params "$B/params.txt" "$@" || bad=1
"$B/files_model" --params "$B/params.txt" "$B/files-check" "$@" || bad=1
files_replay() {
    label=$1; shift
    if hp=$("$@" 2>&1); then
        echo "$label: $hp"
        echo "$hp" | grep -q '^PLAY .*match=yes block_indep=yes'
    else
        echo "$label: command failed: $hp"
        return 1
    fi
}
files_replay "FILES replay" "$H" --play "$B/files-check/pool-header.tl" "$B/files-check/pool-events.tl" || bad=1
files_replay "FILES spatial replay" "$H" --play "$B/files-check/spatial.tl" || bad=1
if files_replay "FILES exit control" sh -c 'echo "PLAY hash=0123456789abcdef match=yes block_indep=yes"; exit 7' > "$B/files-replay-negative.log" 2>&1; then
    bad=1; echo "FILES control FAIL: success-looking output hid a failing process"
else
    echo "FILES control ok: success-looking PLAY with exit 7 is refused"
fi
if "$V" --lines-drop "$B/lines-negative.tl" "$G/48000/shapehard.tl" > "$B/lines-negative.log" 2>&1; then
    bad=1; echo "LINES control FAIL: dropped shape passed"
elif grep -q 'LINES mismatch.*dropped=shape' "$B/lines-negative.log"; then
    echo "LINES control ok: dropping a written shape line fails the byte comparison"
else
    bad=1; cat "$B/lines-negative.log"
fi
mkdir -p "$B/takes"
for mode in mono stereo binaural; do
    t="$B/takes/live-$mode.tl"
    case $mode in
        mono) out=$("$V" --live "$t") || bad=1;;
        stereo) out=$("$V" --live "$t" --stereo) || bad=1;;
        binaural) out=$("$V" --live "$t" --binaural) || bad=1;;
    esac
    echo "$out"
    echo "$out" | grep -q '^LIVE swap ok$' || bad=1
    case $mode in
        mono) a=8d8fd1ed39e13fdd;; stereo) a=421db8c5e68c6de2;; binaural) a=d99d7d691b5804d0;;
    esac   # Fixed anchors include the two-ear binaural take.
    echo "$out" | grep -q " take=$a " || { bad=1; echo "LIVE $mode: take hash is not the anchor $a"; }
    hp=$("$H" --play "$t" 2>&1)
    echo "  harness: $hp"
    echo "$hp" | grep -q 'match=yes block_indep=yes' || bad=1
done
"$V" --space-bounds || bad=1
python3 -B "$(dirname "$0")/../tests/geometry_api.py" "$(dirname "$0")/../bridge" "$(dirname "$H")" "$B/geometry-api" || bad=1
python3 -B "$(dirname "$0")/../tests/profile_api.py" "$(dirname "$0")/../bridge" "$(dirname "$H")" "$B/profile-api" "$G" || bad=1
for order in 1 3; do
    "$V" --live-values "$G/space/binaural/binaural$order.tl" "$B/takes/live-values$order.tl" "$order" || bad=1
    files_replay "LIVE values$order replay" "$H" --play "$B/takes/live-values$order.tl" || bad=1
done
for field in order yaw pinna; do
    order=1; [ "$field" != pinna ] || order=3
    if "$V" --live-values "$G/space/binaural/binaural$order.tl" "$B/takes/live-values-negative.tl" "$order" "$field" > "$B/space-$field-negative.log" 2>&1; then
        bad=1; echo "SPACE control FAIL: dropped $field passed"
    elif grep -q "pcm=NO dropped=$field" "$B/space-$field-negative.log"; then
        echo "SPACE control ok: dropping imported $field changes the plain-rendered prefix"
    else bad=1; cat "$B/space-$field-negative.log"; fi
done
"$V" --live-move "$B/takes/live-move.tl" || bad=1
files_replay "LIVE move replay" "$H" --play "$B/takes/live-move.tl" || bad=1
python3 -B "$(dirname "$0")/../tests/take_api.py" "$(dirname "$0")/../bridge" "$(dirname "$H")" "$B/take-api" || bad=1
"$V" --gen "$B/takes/gen" || bad=1
"$V" --dropped "$B/takes/dropped.tl" "$G" || bad=1
one "$B/takes/gen-tail.tl"
one "$B/takes/gen-stereo.tl"
python3 -B -I "$(dirname "$0")/midi_check.py" "$(dirname "$M")/tools/music" "$B/takes/midi" "$V" "${V}_m1" "${V}_m2" "${V}_m3" "${V}_m4" || bad=1
python3 -B "$(dirname "$0")/../tests/ctl_api.py" "$(dirname "$0")/../bridge" "$B/ctl-api" --controls || bad=1
t="$B/takes/midi-live.tl"
out=$("$V" --midi-live "$t") || bad=1
echo "$out"
echo "$out" | grep -q ' take=f482aae0eae8882e ' || { bad=1; echo "MIDILIVE: take hash is not the anchor f482aae0eae8882e"; }
hp=$("$H" --play "$t" 2>&1)
echo "  harness: $hp"
echo "$hp" | grep -q 'match=yes block_indep=yes' || bad=1
# : the native offline writer and the harness write exactly the same
# PCM16 file, including stereo/binaural interleaving and the rate/header.
mkdir -p "$B/wav-check"
wav_pair() {
    label=$1; file=$2
    if "$V" --wav "$B/wav-check/app.wav" "$file" > "$B/wav-check/$label-native.log" 2>&1 \
       && "$H" --play "$file" --wav "$B/wav-check/harness.wav" > "$B/wav-check/$label-harness.log" 2>&1 \
       && cmp -s "$B/wav-check/app.wav" "$B/wav-check/harness.wav"; then
        echo "WAV ok $label byte_identical=yes reset=yes"
    else bad=1; echo "WAV FAIL $label"; cat "$B/wav-check/$label-native.log"; fi
    rm -f "$B/wav-check/app.wav" "$B/wav-check/harness.wav"
}
for name in live-mono live-stereo midi-live; do wav_pair "$name" "$B/takes/$name.tl"; done
wav_pair binaural "$G/space/binaural/binaural3.tl"
wav_pair rate44100 "$G/44100/bell.tl"
printf 'previous WAV bytes' > "$B/wav-check/previous"
cp "$B/wav-check/previous" "$B/wav-check/refused.wav"
sed 's/^hash .*/hash 0000000000000000/' "$B/takes/live-mono.tl" > "$B/wav-check/wrong-hash.tl"
if "$V" --wav "$B/wav-check/refused.wav" "$B/wav-check/wrong-hash.tl" > "$B/wav-check/hash-refusal.log" 2>&1; then
    bad=1; echo "WAV hash control FAIL: wrong hash accepted"
elif grep -q 'record hash does not match' "$B/wav-check/hash-refusal.log" \
     && cmp -s "$B/wav-check/previous" "$B/wav-check/refused.wav"; then
    echo "WAV hash control ok: wrong hash refused; previous bytes preserved"
else bad=1; echo "WAV hash control FAIL: diagnostic or preserved bytes"; fi
cp "$B/wav-check/previous" "$B/wav-check/refused.wav.tmp"
if "$V" --wav "$B/wav-check/refused.wav" "$B/takes/live-mono.tl" > "$B/wav-check/write-refusal.log" 2>&1; then
    bad=1; echo "WAV write control FAIL: occupied native temp path accepted"
elif grep -q 'refused.wav: cannot write: ' "$B/wav-check/write-refusal.log" \
     && cmp -s "$B/wav-check/previous" "$B/wav-check/refused.wav" \
     && cmp -s "$B/wav-check/previous" "$B/wav-check/refused.wav.tmp"; then
    echo "WAV write control ok: native error captured; previous bytes preserved"
else bad=1; echo "WAV write control FAIL: diagnostic or preserved bytes"; fi
rm -f "$B/wav-check/refused.wav" "$B/wav-check/refused.wav.tmp"
for file in "$G/48000/bell.tl" "$G/space/binaural/binaural3.tl" "$G/gen/genlsystem-48000.tl"; do
    "$V" --seek "$file" || bad=1
done
"$V" --midi-scan || bad=1
if [ $bad = 0 ]; then echo "VERIFY ok"; else echo "VERIFY FAIL"; fi
exit $bad
