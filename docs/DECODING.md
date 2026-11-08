<!-- Copyright (c) 2026 Nenad Micic -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Render and decode: TL to WAV, WAV to TL

The two directions have different guarantees.

| Direction | Command | Guarantee |
|---|---|---|
| TL -> WAV | `python3 tools/fm.py tl2wav IN -o OUT.wav` | Exact and deterministic: the WAV has the same bytes as `harness --play FILES --wav` for the same files |
| WAV -> TL | `python3 tools/fm.py wav2tl IN.wav -o OUT_DIR` | Approximate: a draft mono piece to review, never a recovered original |

The C core is a deterministic renderer only; it does not transcribe. The
transcriber is host-side Python ([analysis](../tools/music/analyze.py), with
NumPy) that proposes TL and checks each proposal by rendering it through the
built harness. The `decodable` effects profile in the
[native reference](INSTRUMENT.md#effects-sends-and-decodable-profile) is an
admission rule for records, not a decoder.

`wav2tl` is a command-line command only. It is not part of the web player or
the hosted service, and this file is not packaged with the web player. The
underlying `transcribe` operation is also offered by `fm.py transcribe` and by
a local MCP client's `transcribe` tool; both run on the local machine.

The macOS app offers only the TL to WAV direction: Records' Export WAV… writes
the open record's WAV through the same native writer as take companions, with
the same bytes as `harness --play FILES --wav` for the record's files. Unlike
`tl2wav`, it does not refuse a silent render, and the standard save panel asks
before it replaces an existing file. The app has no WAV to TL command.

Commands below run from the repository root after `make all`. `fm.py` uses
`build/s12e10p1v1/harness` unless the global `--harness PATH` is given before
the subcommand. `$WORK` is any scratch directory, `$REPO` the repository root and `$HARNESS`
the built harness.

## How the commands relate

| Command | Input | Output | Notes |
|---|---|---|---|
| `harness --play FILES --wav OUT` | TL files in order | WAV, replaced if present | The native reference. A piece's `session.tl` alone renders silence (peak 0) with a stderr hint and exit 0 |
| `fm.py tl2wav IN -o OUT.wav` | TL files or a piece directory | New WAV | Same bytes as the harness; never overwrites; refuses silent output and a saved-hash mismatch |
| `fm.py render FILES --wav --out DIR` | TL files | Records and sounds under `DIR` | Takes files, not a directory. `session.tl` alone reports `ok: true`, `events: 0`, `peak: 0` |
| `interchange.py wav IN --out DIR` | TL files or a piece directory | New bundle (`audio.wav`, `source.tl`, `README.txt`, manifest), or `--container zip` | `audio.wav` has the same bytes as `tl2wav`; see [INTERCHANGE.md](INTERCHANGE.md) |
| `fm.py transcribe SOURCE --out DIR` | Mono PCM16 WAV | New directory with a piece, canonical TL, check WAVs and report | The adapter shared with MCP; see [Approximate WAV transcription](#approximate-wav-transcription) |
| `fm.py wav2tl IN.wav -o OUT_DIR` | Mono PCM16 WAV | The same directory plus `provenance.json` | `transcribe` with a wider recipe shortlist, a draft banner, warnings and `--strict` |
| `analyze.py transcribe` | Mono PCM16 WAV | Analyzer output | The analyzer that `transcribe` runs in an isolated child; prefer `fm.py` |

## tl2wav: exact render

```sh
python3 -B tools/fm.py tl2wav tests/golden/48000/sine.tl -o "$WORK/sine.wav"
build/s12e10p1v1/harness --play tests/golden/48000/sine.tl --wav "$WORK/sine-harness.wav"
cmp "$WORK/sine.wav" "$WORK/sine-harness.wav"
# A piece directory, such as the wav2tl output made below:
python3 -B tools/fm.py tl2wav "$WORK/draft" -o "$WORK/draft.wav" --json
```

Options: `-o`/`--out`/`--output OUT.wav` (required), `--allow-silent`,
`--json`.

`IN` is one TL file, several TL files in order, or one piece directory. For a
directory, its `session.tl` is read first and each declared stream's sibling
`STREAM.tl` follows. A declared stream without a file is reported as a warning.
`OUT` must not exist; an existing file is never replaced (exit 2). The parent
directory must exist. A saved `hash` statement is checked: `saved_hash` is
`match`, `absent` or `mismatch`, and a mismatch writes nothing (exit 3).

A render with peak 0 is refused (exit 2) unless `--allow-silent` is given. The
usual cause is a piece's `session.tl` passed alone: pass the piece directory or
all of its part files instead. Songs driven only by generators may report
`events=0` with audible output; they are accepted. Native warnings from the
renderer are repeated as `warning:` lines on stderr, or in `warnings` with
`--json`. `--json` prints `ok`, `exit`, `out`, `sha256` (of the WAV file),
`frames`, `sample_rate`, `channels`, `events`, `peak`, `saved_hash`, `sources`
and `warnings`. Every refusal prints `ok: false`, `exit` and `message`; a
silent or mismatched render also keeps the render fields, with `out` null.

| Exit | Meaning |
|---|---|
| 0 | WAV written |
| 2 | Refused before writing: missing input, output exists, missing parent directory, silent render |
| 3 | Rendered audio does not match the saved `hash`; nothing written |
| 5 | Harness missing or the WAV could not be written |
| other | The harness rejected the TL; its exit code is kept (for example 1 for an unknown statement) and the message names file and line |

## wav2tl: approximate draft

```sh
python3 -B tools/fm.py wav2tl "$WORK/sine.wav" -o "$WORK/draft"
python3 -B tools/fm.py tl2wav "$WORK/draft" -o "$WORK/draft.wav"
python3 -B tools/fm.py wav2tl "$WORK/sine.wav" -o "$WORK/checked" --strict --min-snr-db 20
python3 -B tools/fm.py wav2tl "$WORK/long.wav" -o "$WORK/long" --option=--max-seconds --option=600
```

Options: `-o`/`--out OUT_DIR` (required), `--strict`, `--min-snr-db DB`
(default 0), `--option TOKEN` (repeatable; one transcriber argv token, as for
`transcribe`), `--native-budget N`, `--json`. It needs NumPy, the native
harness and `data/descriptors.tsv`. It is a thin alias over `fm.py transcribe`:
the same isolated child, staging, lock, input caps and never-overwrite
publication.

The result is a draft to review before sharing, never a recovered original:
mono only (one note at a time; chords and polyphony are not transcribed), timbre
approximated by the nearest built-in recipes, and no effects, stereo or physical
note-offs. A one-line banner on stderr says so on every run, with the measured
whole-signal SNR when there is one. The SNR is unaligned and unscaled, so even a
TL-rendered round trip can score near 0 dB (the single sine above scores about
0.7 dB).

- Unless `--option` already names `--recipe-candidates` or `--recipes`, the
  descriptor shortlist is widened to 8 recipes. If that run is refused by the
  decoder after native calls (exit 2), it is retried once with the analyzer
  default of 1, sharing `--native-budget`; `fallback` and a warning record it.
- `OUT_DIR` must not exist (exit 2). It holds the piece (`session.tl` with the
  existing `# Approximate mono transcription` comment, and `notes.tl`),
  `canonical.tl`, `report.json`, the check WAVs (`rerender.wav`, `replay.wav`)
  and `provenance.json`.
- Warnings come from existing measurements only: whole-signal SNR below
  `--min-snr-db` (default 0 dB, a draft no closer than silence) and audible
  regions the analyzer omitted. There is no polyphony detector. Clipping is not
  a warning because a clipped reconstruction already fails the run.
- `--strict` turns any warning into exit 4 before publication: nothing is
  written. `--json` adds `draft: true`, `snr_db`, `limitations`, `warnings`,
  `provenance`, `provenance_file`, `recipe_candidates` and `fallback` to the
  transcribe reply.

### Provenance sidecar

`provenance.json` records `derived: "audio-transcription"`, `draft: true`,
`source_sha256`, `source_bytes`, `source_name` (basename only), `tool`,
`tool_version` (the harness `--version` line), `engine` (the record's engine
statement), `native_sha256`, digests of the tool files, the normalized
options, `snr_db`, `limitations` and `warnings`. It holds no directories or
times. Piece readers use only `session.tl` and its declared stream files, so
the sidecar does not change rendering. Keep it beside the piece when sharing a
draft so readers can tell it came from audio.

### Exit codes and limits

| Exit | Meaning |
|---|---|
| 0 | Draft published; or, with `complete: false` and `stop_reason: "budget"`, the native budget ran out and nothing was published |
| 2 | Refused input or output: output exists, not mono PCM16 at 44100 or 48000 Hz, malformed or truncated WAV, over the duration cap, too many regions without pooling, or no acceptable reconstruction |
| 4 | `--strict` and the draft has warnings; nothing written |
| 5 | Missing dependency (NumPy, harness), native failure, or out of memory |

Input caps, checked before samples are read where possible:

- mono PCM16 at 44100 or 48000 Hz only;
- duration at most `--max-seconds` (default 300, at most 1200), checked from
  the header;
- every RIFF chunk size up to `data` must fit in the file, and odd chunks need
  their pad byte;
- at most 32 audible regions without `--pool-voices`; `--max-notes` (default
  64, at most 4096) above 64 needs pooling; the global 32-patch and 64-voice
  limits still apply.

## Approximate WAV transcription

`transcribe(source,out,options=...,native_budget=None)` uses the mono transcriber in
an isolated process with the configured, already-built renderer. Input is
mono PCM16 at44100 or48000Hz; `out` must be a new directory. It writes editable
TLs, a canonical TL, reconstruction/replay WAVs and a complete report. Source
reconstruction is approximate; exact canonical replay is a separate check.
This does not establish arbitrary-song, chord, FX, spatial or tracker recovery.

Discover exact options through `reference(source="transcribe-options")`.
`options` is an argv list, not a shell command. Existing thresholds and defaults
are unchanged; input/output/harness/help are owned by the adapter. Explicit
recipes narrow matching; opt-in onset splitting, sample alignment, duration
fitting and pitch refinement support bounded experiments. For example:

```sh
python3 -B "$REPO/tools/fm.py" --harness "$HARNESS" transcribe \
  "$WORK/source.wav" --out "$WORK/transcription" \
  --option=--recipes --option=marimba --option=organ \
  --option=--align-ms --option=5 --option=--fit-hold \
  --option=--pitch-refine-cents --option=1 --native-budget 600 \
  --receipt-dir "$WORK/operations" --json
```

For longer recovered melodies, opt into `--pool-voices` and an explicit
`--max-notes N` (default64, maximum4096; values above64 require pooling).
Exact recipe/level pairs share patches. Eligible two-operator voices reuse
only after the existing release guard; stateful recipes retain fresh voices.
Global32-patch/64-voice limits still apply and refuse excess work. Pooling
changes allocation, not inferred notes or source-reconstruction accuracy;
the complete report records eligibility, capacities and note-to-voice mapping.

Without `--pool-voices` every note gets its own patch, so more than32 audible
regions are refused (exit2) before any native call or output; the message
names the region count and suggests `--pool-voices` or a shorter excerpt.

Input checks run on the WAV header before any sample is read. Every RIFF chunk
size up to `data` must fit in the actual file, so a truncated header, a size
such as `0xFFFFFFFF`, or an odd-sized chunk without its pad byte is refused with
exit2 and a message; a missing pad byte is not repaired. The source duration
cap is `--max-seconds` (default300, at most1200). Analysis holds about185MB
per minute of mono 48kHz audio, so the default stays near1GB and the upper
bound near4GB. A longer source is refused from its header. The adapter hashes
the source in bounded blocks; it still reads the whole file to hash it.
Running out of memory reports exit5 with a message instead of a crash.

Staging directories are named `.transcribe-pending-<tag>-*` beside `out`,
where the tag is derived from the output path. A run killed with SIGKILL can
leave one; the next transcription to the same `out` removes it while holding
that output's lock, so a live run is never touched. Untagged staging directories
from older versions, or ones left for an `out` that is never retried, stay
until removed by hand. The per-output lock file `.session-export-*.lock` is
removed when a publish finishes; one killed mid-publish can remain and is
reused or removed by the next publish to that output.

`native_budget` (CLI `--native-budget`) is an optional nonnegative integer
cap on actual CONFIG/play attempts, including failed invocations. It is an
adapter argument, not an `options` token. A normal exhausted-budget reply has
`ok:true, exit:0, complete:false, published:false, stop_reason:"budget"`.
Check `complete` before using artifacts. No partial score is published, and
a higher-budget retry starts a new operation. Exact-budget completion succeeds
when no further call is needed; genuine native or dependency failures keep
their failure status. The cap does not bound host analysis time or template
requests, which can hit the cache. `budget_remaining` and `blocked_operation`
explain a stop; incomplete template-request counts remain null.

Completed compact replies also include `note_preview`: the first16 report
notes, original order, with onset/off times, pitch, recipe, carrier level and
existing local segment SNR. Inspect `shown`, `total` and `truncated`; use the
full report for all notes and detailed scores. A budget stop has no preview,
score or replay result. Large metadata may explicitly omit the preview;
completion, budget and publication fields remain available.

Opt into `receipt_dir` before a costly call to recover complete diagnostics
through verified receipt pages without rerunning the search. Compact replies
include the global unaligned error score, replay status, limitations and ready
artifact-reader arguments for canonical TL and reconstruction WAV. The complete
report also exists in the explicit output directory. Native invocation counts
are actual attempts; template requests can include cache hits. No convergence
or whole-song success follows from spending more calls, and no dry-run cost
estimate is supplied by this operation.

## MIDI import

Through MCP, call `reference(source="midi-options")` and follow `next_offset`
to read the existing converter's exact help. Then inspect an explicit file:

```json
{"source":"/path/to/input.mid","options":["--map","1=epiano","--voices","8"]}
```

Pass that object to `midi_import`. The default dry run performs no builds,
writes or native renders. To convert, add `dry_run:false` and a new `out`
directory. The server uses its configured, already-built harness. The result
identifies `record/session.tl`, its stream files, and canonical TL/WAV under
`renders/`; `record.path` and `wav.path` are relative to `output`.
`record.artifact` and `wav.artifact` contain ready `artifact_read` arguments:
pass the object with `encoding="utf8"` for the TL or use default base64 for
WAV bytes. These explicit outputs use a single typed filename; they need no
extra render or accepted-take log for retrieval. Existing
conversion, levelling and replay checks run before atomic publication. An
existing output is refused. These structured outputs belong outside the music
corpus; retaining a composition in the repository remains a separate step.
Options are separate argv tokens, with no shell interpretation. Input/output,
dry-run, selftest and help switches are adapter-owned; omit them from options.
For an unknown file, a dry run without mappings exposes `channel_summary`
even when conversion refuses. Its `notes` and `skipped` are existing converter
counters after musical policies, not an independent source-event count.
Map the reported channel explicitly and inspect again. A refusal remains a
refusal; reading channel information does not imply a successful conversion.

The shared CLI defaults to the same inspection:

```sh
python3 -B tools/fm.py midi_import /path/to/input.mid \
  --option=--map --option=1=epiano --option=--voices --option=8 --json
```

Add global `--harness "$HARNESS"` before the subcommand and `--render --out
"$WORK/imported"` after it for conversion. Compact replies retain the outcome,
counts, identities and output paths; `omitted` names verbose details. Opt into
`receipt_dir` to preserve complete dependencies, per-channel counts, original
options, diagnostics and observed native calls. Retrieve those full-result
bytes through `artifact_read(kind="receipt",digest=full_result_sha256)` without
repeating conversion. `published` reports whether this operation published an
output; a missing/malformed child response leaves publication state unknown.
Imported MIDI does not automatically create an accepted-take log.

MIDI conversion accepts Standard MIDI Files 0/1. This safe inspection
does not build, render or write:

```sh
python3 -B "$REPO/tools/music/midi2tl.py" /path/to/input.mid \
  --map '1=epiano' --voices 8 --dry-run
```

Channels in CLI mappings are 1-based. Relevant options are:

MPE is opt-in: `--mpe lower:N` uses master channel 1 and members 2..N+1;
`--mpe upper:N` uses master 16 and members 15 down to 16-N. N is 1..15.
Both may be supplied, separated by a comma or as repeated options, only
when zones do not overlap (at most 14 members total for two zones).
Give each master a plain `--map 1=RECIPE` or `--map 16=RECIPE`; member
channels cannot have separate maps/kits. A master cannot have key zones,
mono/unison/choke/random modes. Member pressure and slide reserve ctl1
and ctl4, so master `--ctl` must not map either (including `default`).
These are the current parsing contracts. Expression and lifecycle limitations
remain; this guide does not claim full MPE conformance.

| Area | Accepted options |
|---|---|
| Patches/layers | `--map 'CH=RECIPE[:STREAM][@LO-HI[/VLO-VHI[~]]] [ARG=DELTA ...]'`, `--program 'PROG=RECIPE [ARG=DELTA ...]'`, `--filter CH=MODE:HZ:Q`. Repeated compatible maps layer voices. |
| Allocation | `--voices N`, or bounded shared `--pool N` with `--reserve PATCH=N,...` and `--limit PATCH=N,...`. Limits include zero; incompatible policies refuse. |
| Expression | `--bend ST`, `--rpn`, `--ctl CH=SOURCE:N`, `--ctl CH=default`, `--mono CH=MODE[:PRIORITY]`, `--glide CH=MS`, `--unison CH=N:CENTS`. MIDI unison remains mono; spatial width is not a CLI option. |
| Tuning/variation | `--tuning FILE`, `--scala FILE.scl[:FILE.kbm]`, `--scale CH=NAME\|MASK:ROOT`, `--random CH=SEED:CENTS[:VEL]`. |
| Drums | `--kit 'CH=KEY:RECIPE[:HZ] [ARG=DELTA ...]'`, `--choke CH=KEY+KEY[+KEY...]`. |
| Arp/chord | `--arp CH=MODE:RATE[:OCT[:GATE]]`, `--chord CH=SHAPE[:STRUM_MS]`, `--arp-latch CH[,CH...]`, `--arp-seed CH=UINT32`. |
| Conditions | `--arp-cond CH=STEP:COND`, `--arp-nei CH=SOURCE_CH`; NEI depends on an earlier/lower channel and explicit source rows. |
| Output | `--fs HZ`, `--tail MS`, `--name NAME`, `--out DIR`, `--dry-run`. |

The `~` velocity crossfade requires explicit `/VLO-VHI`. Its attenuation
law uses a restricted carrier path with free controller ownership and
checks final leveller values; filters, formants, shapers and other unsafe
combinations refuse. It is not a generic amplitude guarantee for every
patch. Arp/chord expansion precedes seeded pitch/velocity variation;
distinct strummed onsets keep their own draw pair, while same-time layers
share it. Conditional skip counts count rejected strikes, not expanded
layer/unison copies. MPE parsing is implemented, but complete expression
conformance has not been established.

Mono MODE is `retrigger` or `legato`; priority is `last`, `low` or `high`.
MIDI arp modes match `Piece.arp`, but RATE is a whole-note fraction
`1/1`, `1/2`, `1/4`, `1/8`, `1/16` or `1/32`, with optional lowercase
`d`/`t` for dotted/triplet. This differs from Python's numeric quarter-note
step. The tick step must be integral and MIDI arp requires PPQN, not SMPTE.
Chord SHAPE is offsets joined by plus, e.g. `0+4+7`. Ordinary program
numbers are 1..128; unmapped channels, including channel 10, do not play.
MIDI `STRUM_MS` is the total chord span, as in `p.chord`: `0+4+7:20`
starts its three notes at 0, 10 and 20 ms relative to the input note-on;
all chord copies retain the source note's release time.
Controller sources include `ccN`, paired high-resolution `ccN+`,
`nrpnMSB.LSB`, `pressure`, `polyat`, `relvel` and `rand:SEED`.
Release-velocity mapping with arp is refused because generated gates have
no physical key-up byte. Always inspect the dry-run summary for ignored
events, skipped notes, steals and incompatible option diagnostics.

**Normal `midi2tl.py` execution calls `compose.build` and renders into
its repository-associated render area even with `--out /tmp/...`.** It is
not part of the no-repository-write quickstart. Use dry-run in this bounded
workflow; run a full conversion only in an explicitly disposable checkout
or after accepting its documented build/output effects. Similarly,
`compose.py NAME`, `--solo`, `--selftest-recipes`, `play.sh`, and
`analyze.py describe` build or render into conventional repository paths.
Do not assume an output argument redirects every intermediate artifact.
