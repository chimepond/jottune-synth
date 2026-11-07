#!/usr/bin/env bash
# Copyright (c) 2026 Nenad Micic; SPDX-License-Identifier: Apache-2.0
# Verify and play complete TL files in order, or render them with -n.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
renderer="$repo/build/s12e10p1v1/harness"
output="$repo/build/listening"
listen=true
if [[ ${1:-} == -n ]]; then listen=false; shift; fi
if [[ $# == 0 || ${1:-} == --help || ${1:-} == -h ]]; then
    printf '%s\n' 'Usage: tools/music/play.sh [-n] SONG.tl [SONG.tl ...]' \
        'Each argument is one complete song. -n verifies and renders without playing.'
    exit 0
fi
if [[ ! -x "$renderer" ]]; then
    printf '%s\n' 'Build the renderer first with make.' >&2
    exit 1
fi
mkdir -p "$output"
for song in "$@"; do
    [[ -f "$song" ]] || { printf 'Song unavailable: %s\n' "$song" >&2; exit 1; }
    directory=$(mktemp -d "$output/song.XXXXXX")
    "$renderer" --play "$song" --wav "$directory/audio.wav" --merged "$directory/song.tl"
    printf 'Rendered: %s\n' "$directory/audio.wav"
    if $listen; then
        if command -v afplay >/dev/null 2>&1; then afplay "$directory/audio.wav"
        elif command -v aplay >/dev/null 2>&1; then aplay "$directory/audio.wav"
        else printf '%s\n' 'No system player found; open the rendered WAV to listen.'; fi
    fi
done
