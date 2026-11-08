<!-- Copyright (c) 2026 Nenad Micic -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# JotTune Synth: compose, render, inspect, revise

The source checkout includes complete songs in [the example library](../music/library.json).
Use the macOS app's **Records → Songs** to browse those files. Keys is the default
where available; Standard preserves each original spatial route. The larger
ChimePond Tunes collection is maintained separately; the
[project guide](../README.md#browser) explains how to build its browser catalog.
`apps/macos/tlplay PATH.tl` opens a standalone TL in the built app.

## Start from a player URL without MCP

On a hosted player, read the [agent quickstart](../web/agent-start.txt) first.
The same page provides the downloadable Node client, score-v1 authoring,
60 native recipes, the complete native TL reference, local Wasm rendering,
LUFS/section/dry-source analysis and TL/WAV/SVG exports. No source checkout,
Python installation or MCP server is needed for that workflow. The
[authoring guide](../web/authoring.txt) explains exact musical time and sound
parameters; the [analysis guide](../web/analysis.txt) defines measurement limits.
A canonical replay verifies the saved song. Publication remains a separate,
explicit-consent step. The local MCP and richer source tools below are optional.

## Start through MCP

JotTune MCP runs locally over stdio. The `jottune-mcp` npm package wraps these
same tools: `jottune-mcp doctor` checks your Python dependencies and native
renderer, and `jottune-mcp guide` prints this guide. Set `JOTTUNE_PYTHON` to your
Python environment and run `jottune-mcp build` explicitly, or pass `--harness`
with an existing renderer. Installation does not build or download an engine.
Use `jottune-mcp config --workspace DIR` to print a local client configuration
whose relative tool paths resolve in that existing session directory. Tool output
paths remain explicit; the workspace is not a security boundary. `requirements`
prints the full SDK/analysis/plot dependency file. See the
`chimepond/jottune-mcp` package guide for installation and complete artifact
delivery. Tool names, `fm://` resource identifiers and the `.tl` format remain
stable.
The npm package contains no song TL files or corpus catalog. Compose original
music offline with the included instrument recipes, or render a standalone TL
downloaded from the song library. No tool downloads songs automatically.
Default `search` and `suggest` use recipe descriptors; to search your own songs,
call `catalog_build(records=[...], out=...)` and select that table with `tsv`.

Use `reference` for this introduction in bounded pages. Follow `next_offset`
until `eof=true`; each reply identifies the complete source and selected text
by SHA256. `reference(section="index")` lists this guide's headings;
`reference(section="Effects, sends and decodable profile")` reads that topic.
For exact native fields use `reference(source="params",section="send")` or
`section="delay"`; omit section for the parameter index. Full resources and
`params` remain bulk exports for clients that can read large responses.
Read `Target metrics and availability`, `Operation work and files`,
`Identities and revision parents`, or `Classified musical examples` with
the same `reference(section=...)` call when choosing the next operation.

The operator supplies the server, compiled renderer and an explicit output
directory. You can compose using only MCP: no shell, repository access or
separate file-writing tool is required. Tools have no hidden current patch.

### Complete MCP text example

Here is a complete two-voice text example with an actual delay send. Supply
this string as `render.text`; it is a short starting example, not a template
restriction. The `engine` line must match the supplied renderer's discovery.
Times are nanoseconds, pitch is millihertz, and `gain` is tenths of a dB.

```text
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1600000000
streams notes
delay 180000 8192 6000
gain 0
patch chime 0 65536 180 0 0 256 2 2500 900 300
patch chime 1 229376 0 8192 0 0 1 1200 600 400
patch bass 0 65536 240 0 0 256 5 500 200 800
patch bass 1 65536 0 0 0 0 5 500 200 800
voice 0 chime
voice 1 bass
0 notes 0 send delay 8192
0 notes 0 on 523250 26000
0 notes 1 on 130813 24000
300000000 notes 0 off
500000000 notes 0 on 659250 24000
800000000 notes 0 off
800000000 notes 1 off
```

For an effect check, render a second version with its send set to zero and
compare the relevant measurements. Declaring a bus without a nonzero send
leaves the voice dry. Exact replay proves reproducibility; a changed hash
alone does not prove that the intended musical change occurred. Keep each
original composition and its revisions as TL plus editable score/text.

### MCP workflow and revisions

1. Choose sounds with `search` or `suggest`; use their `page.next_offset`
   to continue. Results default to20 entries, with `limit` from1 to100.
   `record_path` is the usable catalog-file path; `record` is its identity.
   `reference(source="recipes")` accepts only index/all; use `load_patch`
   for a named recipe. `load_patch(name,patch)` expands it to text only:
   it neither saves a file nor establishes a current patch. Full is the
   unchanged default and can be large. For generated-asset recipes, start
   with `load_patch(name="bl-saw",patch="lead",detail="page",length=4096)`.
   Follow `next_offset` with `expected_sha256` set to the first page's
   `sha256`, concatenate `text` to EOF and verify the complete SHA256 before
   inserting it in a score. There is no hidden session or current instrument.
2. Author a complete timeline as text and call
   `render(text=SCORE,log=OUTPUT_DIRECTORY,wav=true,target=["clipped=0"])`.
   out and log are directories; log appends accepted takes.
   Supply `text` or existing `files`, never both. `check_patch(text=SCORE)`
   can validate the same input first. Native validation owns the grammar.
3. Keep the returned `canonical`, `record`, `record_sha256`, `wav`,
   `wav_sha256` and audio `hash`. These identify different things: TL bytes,
   the complete WAV file and PCM respectively. The first unhashed source has
   no expected-hash comparison; `checks_pass=null` can mean unchecked. An
   accepted render still has `ok=true,exit=0`; inspect each check's status.
4. Revise your score, retaining the original. For a6dB quieter version change
   the master `gain 0` to `gain -60`; when editing canonical text, remove its
   old `hash` line. Render into the same explicit log with
   `parent_record=ORIGINAL_RECORD_SHA256`; that parent must already be in the
   same log. A separate log cannot refer to it as its parent. Omitted means
   no parent. `compare(a=ORIGINAL_RECORD_PATH,
   b=REVISED_RECORD_PATH)` reports numeric deltas as `[a,b,b-a]` under `play`
   (for example `play.rms_dbfs`); use peak/RMS
   to verify the requested gain, not merely that hashes differ. Small PCM
   rounding differences are expected. `measure=true` adds acoustic measures.
5. `log_replay(directory=OUTPUT_DIRECTORY)` independently checks every saved
   take, including after reconnection. Inspect the count and each take's
   match/block-independence result. Missing or empty replay logs are errors.
6. Local clients can use the returned WAV path. For transport of actual bytes,
   call `artifact_read(directory=OUTPUT_DIRECTORY,kind="wav",digest=WAV_SHA256,
   offset=0)` and continue at `offset+length` until `eof=true`. The default
   chunk is8192 bytes; each call verifies the entire retained artifact.
   The client decodes/concatenates `data_base64` chunks and verifies the whole
   SHA256. For readable TL text without a decoder, use `kind="record",encoding="utf8"`
   and follow `next_offset` until EOF. The size, MIME type
   and full digest accompany every chunk; a path alone is not a download.
   Creating a file, fully retrieving it and actually listening are separate
   evidence. A prefix read does not establish complete delivery.

Recipe pages use UTF8 byte offsets and lengths, default4096 and maximum8192.
Reply `length` is delivered bytes; follow `next_offset`, since escape-aware
sizing can shorten a page. A page may end inside a TL line. Guarded offset0
restarts are allowed; offset at EOF returns an empty page. A digest mismatch,
invalid UTF8 boundary or a length too small for the next codepoint refuses2
without text. Nondefault page options require `detail="page"`.
Each page expands the complete recipe once again, without native execution,
builds or WAVs. The guard prevents mixing changed text; it does not freeze
dependencies, recover an old version or attest native validity. Page outcomes
are bounded to15360bytes of escaped, indented public JSON; the duplicated SDK
envelope is larger and has no universal client-size guarantee. Request smaller
pages when needed. Dispatched failures preserve statuses and explicitly omit
oversized diagnostics. Runtime helper/UTF8 failures report host5, while the
existing expander's semantic errors retain their status. Transport and receipt
setup errors are outside the dispatched-outcome guarantee.

Loading pages creates no implicit store. Existing shared Python callers can
opt into generic receipts, which retain only that projected page/error and
leave `full_result` null. Each page is a new expansion and receipt operation.
Receipt-completion failure reports outer host5, unknown receipt state and the
known inner page outcome, with explicit omissions if necessary. CLI/MCP
loading exposes no new receipt arguments. Assemble and verify all patch text
before including it in a score or submitting it to `check_patch`.

For long scores, explicitly request `detail="compact"` on `render` or
`check_patch`. Their default remains the legacy full reply. Compact replies
keep execution status, native/audio/record identities, geometry, levels and
checks, but set `canonical=null` with `canonical_bytes` and named `omitted`
paths. If checks are too large, `check_counts` preserves pass/fail/unchecked
totals. A successful retained render has `published=true` and an `artifact`
argument object for verified UTF8 record reads. `check_patch` does not retain
a record: use `receipt_dir` on that original call when you need its complete
outcome later. Both operations then retain exact full JSON under
`full_result_sha256`; read it with
`artifact_read(directory=RECEIPT_DIRECTORY,kind="receipt",digest=FULL_RESULT_SHA256,encoding="utf8")`.
Follow `next_offset` through EOF and verify the digest. This retrieval does
not rerender. Without receipts, the full-result hash identifies omitted data
but does not promise it was saved. Receipt publication failure is host exit5
with the completed operation under `operation_result` and retention marked
unknown; inspect that inner publication status before retrying.

`describe` and `compare` default to `detail="compact"`: computed verdicts,
identities, counts and measurements that fit a16384-byte JSON budget. Bulk
fields are null with explicit `omitted` paths; larger replies omit more whole
groups. For costly measurements, opt into `receipt_dir` on the initial call:
its response receipt retains the complete result, with the same full-result
SHA used by paging. Read that receipt's `full_result_sha256` through
`artifact_read(kind="receipt",encoding="utf8")`; no native work repeats.
Without a retained result, use `detail="page",expected_result_sha256=full_result.sha256` with
the same input/measurement arguments, follow `next_offset` to EOF, and verify
the digest to retrieve the complete full-result JSON. Each page recomputes
the operation; there is no cache. Page length defaults4096 UTF-8 bytes
(1..8192), and offset must be a codepoint boundary. Explicit summary/full
remain unbounded legacy modes: summary includes canonical text and all
measurements; full also includes all event/native-field rows. describe out
is an artifact directory, not a JSON filename, and requires explicit
summary/full. Computed checks and deltas are unchanged.

For a small spatial interval use `detail="windows",measure=true,start_ns=...,
end_ns=...`. Integer nanoseconds select whole overlapping existing100ms windows;
they do not crop or remeasure audio. Omitted end means through EOF. Follow
`next_window_offset` with `window_limit`1..32 (default8), guarding the full-result
SHA. `checks_scope="full_result"` keeps complete checks separate from this
selection. Compare deltas exist only for identical sample rate/start/frame count;
other pairs explain their missing delta. Mono has no image; an out-of-range
stereo selection is empty. Window values are ITD/ILD/side-mid/correlation, not
RMS/peak or per-voice measurements.

### Keep a TL-only composition session

Use `render(text=SCORE,log=TAKE_DIRECTORY,wav=false,detail="compact",target=["clipped=0"])`
to synthesize and retain a canonical timeline without creating a WAV. The
reply still reports the PCM hash, peak/RMS, per-channel levels, clipping
and block independence. Save its record SHA256 and explicit take directory.
The editable source should omit `hash`; the canonical record contains the
expected audio hash. Retain both when developing a song.

For a revision, call `compare(a=ORIGINAL_RECORD,b=REVISED_RECORD,measure=false)`.
This still renders both records and reports whole-render level deltas and
native-field changes; it does not compute acoustic descriptors or ITD/ILD.
`check_patch` and `describe(measure=false)` also synthesize the input, so
they are not free syntax checks. A successful `render` already validates its
complete input. Replay the retained batch with
`log_replay(directory=...,detail="compact",receipt_dir=EXPERIMENT_DIRECTORY)`
after reconnecting; each take is rendered separately, without creating WAVs.

For sound selection without generating audio files, use `search`,
`load_patch`, bounded `reference`, or `suggest(probe=false)` with `among`
omitted. The last operation ranks existing descriptor rows without rendering.
`probe_plan` renders the source for validation but writes no WAV. Read the
complete saved TL using `artifact_read(kind="record",encoding="utf8")`,
following `next_offset` to EOF with its record SHA256.

Avoid `measure=true`, `measure_section`, `suggest(probe=true)`, `suggest(among=...)`, `seek`,
`catalog_build`, `find_patch(mode="describe")` and non-dry MIDI import when
the session must create no WAVs: those operations need temporary or retained
audio files. MIDI `dry_run=true` remains text-only. Exporting TL-only take and
receipt roots preserves a TL-only bundle; exporting a root that already
references WAVs copies those WAVs as well.

CLI equivalents omit `--wav` and `--measure`:

```sh
python3 -B tools/fm.py --harness "$HARNESS" render "$WORK/score.tl" \
  --log "$WORK/takes" --target clipped=0 --detail compact --json
python3 -B tools/fm.py --harness "$HARNESS" log replay "$WORK/takes" --json
```

Exact replay establishes reproducibility. Levels and clipping describe the
rendered signal; they do not establish musical quality or perceived spatial
location. Use ordinary stereo for session FX, and a separate binaural
arrangement with no FX headers for a headphone scene.

### Preserve and resume an experiment

Opt into `receipt_dir=EXPERIMENT_DIRECTORY` on render/describe/compare/search/
suggest/seek/probe_plan/check_patch/analyze/midi_import/log_replay (CLI `log replay`). Add `receipt_purpose` to explain intent and,
for a later step, `receipt_parent=PRIOR_REQUEST_SHA256`. Receipt parent and take
parent_record are different identities. Requests preserve original text, targets,
tolerances, source references and tool/native identities; responses preserve
failures, measurements and verdicts. Accepted take logs remain separate.
Replay defaults to its complete unbounded take list. Use explicit
`detail="compact"` (CLI `log replay DIR --detail compact`) for bounded status
and returned-row counts. With receipts, `receipt.full_result_sha256` identifies
the exact complete outcome for `artifact_read(kind="receipt",encoding="utf8")`;
reading it does not replay again. Without receipts, a full-result digest does
not promise retained bytes. An aborted replay can lose prefix evidence, so its
counts are unknown. Hash mismatch preserves `ok=true,exit=3`; inspect both.
Receipt completion failure reports outer host5 with the inner replay outcome
and unknown retention. Exceptional runtime serialization failure preserves
known inner status/counts but marks exact full evidence unavailable.

To resume without conversation history, call
`log_show(directory=EXPERIMENT_DIRECTORY,kind="operations")`. Follow
`page.next_offset` with `expected_index_sha256`; a changed journal requires a
fresh page sequence. Reconstruct the chosen request/response using
`artifact_read(directory=EXPERIMENT_DIRECTORY,kind="receipt",digest=SHA256,encoding="utf8")`.
This returns readable text directly; no shell or base64 decoder is needed.
Follow `next_offset` with the same digest through EOF. Pages are exact UTF8
byte fragments, not necessarily standalone JSON; inspect or join all relevant
pages before claiming complete evidence. Complete SHA and UTF8 are verified
before every text reply, including bytes outside the requested fragment.
The response envelope contains the exact public response excluding its own
receipt metadata, plus full_result_sha256 when complete costly evidence exists.
Read that digest through the same reader. Verify complete chunks and inspect
execution, requested targets and reached/checks separately before continuing.

Suggest/seek default to compact replies with explicit omitted bulk histories.
With receipt_dir, their full result is saved once before projection. Retrieving
it costs no additional renders. `detail="full"` instead executes a **new**
operation and returns an unbounded result; it is not a fetch. A new seek starts
from an explicit source and budget, never an invisible saved sweep.

Pending receipts mean outcome unknown after interruption; do not assume failure
or retry side effects. Completion storage failure is host5 and includes the
original operation_result. Receipts do not back up external input files: retain
or transfer their referenced sources and take artifacts too. Schema/protocol and
CLI-parser failures before operation dispatch are outside receipt coverage.

To move retained work, call `session_export(sources=[EXPERIMENT_DIRECTORY,
TAKE_DIRECTORY],out=NEW_BUNDLE_DIRECTORY)`. Select1..32 roots explicitly;
the destination must not exist. This snapshots their indexes, referenced
receipt/full-result JSON, canonical TLs and every referenced WAV. It preserves
original bytes and paths inside requests. External input files and native
binaries are not captured; pending operations remain unknown.

After transfer, call `session_verify(directory=BUNDLE_DIRECTORY,
expected_manifest_sha256=EXPORTED_SHA256)`. It verifies every declared artifact
and the journal/take links without rendering or writing. Read the returned
`artifact` with `encoding="utf8"` to retrieve the complete manifest. Its root
mapping gives each original root and its relative `roots/root-NNNN` location.
Use that mapped directory with existing `log_show` or explicit `log_replay`;
replay uses the configured native runtime. Manifest retrieval alone verifies
the manifest's bytes, not the whole bundle. Extra undeclared files are outside
the verified scope. CLI equivalents are `session_export ROOT... --out DIR`
and `session_verify DIR [--expected-manifest-sha256 SHA256]`.

`analyze(wav,mode="spectro",out=PNG_PATH)` creates missing parent directories
and returns compact metadata plus exact `artifact_read` arguments in
`artifact`. PNG retrieval uses `kind="png"` and a single .png filename.
Follow chunks to EOF and verify the full digest. Explicit `detail="full"`
returns the original unbounded inline PNG. Retrieving PNG bytes does not
establish that the model viewed them. Ordinary overview/loudness are unchanged.
Start artifact reads with the8192-byte default. UTF8 pages always accept
length1..8192. Typed `authored_tl` and `generator_manifest` views also limit
base64 pages to8192; other base64 artifacts, including `score`, accept up
to1048576 bytes.
Base64 and protocol overhead enlarge the response. Increase an ordinary base64
page only within a known client delivery limit; a large tool-list reply does not
increase the UTF8 limit. Follow each returned next_offset to a verified EOF.
An allowed larger request can succeed at the server yet overflow its client.
The preview plots inferred bars1-4 from an estimated first bar; it can omit an
intro or end before EOF. Inspect the span in stdout. MCP currently exposes no
bar/time selection; those options belong to the standalone analyzer CLI.

The native timeline grammar below is directly usable through MCP text inputs.
Python composition helpers and harness-only options are labeled separately;
they require those external interfaces and are not additional MCP tools.
`reference(source="params",section="index")` lists statement selectors;
public names such as `send` and IDs such as `event_send` select native fields,
bounds, units and dependencies. `params` and `fm://params` retain the bulk listing. Unknown
argument names and invalid JSON types are refusals; missing required fields
or malformed MCP requests can instead be protocol errors. Correct the request
and retry; never infer that an ignored or unchecked setting was applied.

## Exporting WAV, MIDI and SFZ

Keep the complete canonical TL as the editable original. For other players,
call `interchange_inspect(source="song.tl")` to validate/render the source and
inventory possible conversions. A candidate is not a successful export;
`interchange_export` with no `out` performs the actual conversion as a dry run.
Source is one local TL/recipe path or an ordered list of local input paths.
Nothing downloads a song, builds an engine or sends MIDI to a device.

Examples for a song using native voices 0 and 1:

```text
interchange_export(source="song.tl", profile="wav")
interchange_export(source="song.tl", profile="midi", channels=["0=1","1=2"])
interchange_export(source="song.tl", profile="midi", channels=["0=1","1=2"], accept_loss=true, out="song-midi.zip", container="zip")
interchange_export(source="song.tl", profile="sfz", key=60, out="song-sfz.zip", container="zip")
interchange_verify(bundle="song-sfz.zip")
```

`out` must be a new destination. Default `container="directory"` retains a
directory; `container="zip"` delivers one complete archive. `published=true`
means local output files, not online publication. The bundle includes original
canonical TL, required format/WAV files, instructions and the complete report
in `manifest.json`. `interchange_verify` checks the retained bytes and declared
dependencies after copying it, without native rendering or archive extraction.
Optionally supply `expected_manifest_sha256` from the export response.

For MCP-only delivery, choose a `.zip` output and pass the returned
`delivery.archive` object directly to `artifact_read`, adding `offset` and
`length` for each page. Decode `data_base64`, advance by the returned `length`
until `eof`, then check the assembled SHA256 against its `digest`. The default
page is 8192 bytes; every call verifies the complete file without rendering.
Programmatic clients can request up to 1048576 bytes per base64 page to reduce
repeated full-file reads; keep model-facing pages small.
Directory outputs instead supply `delivery` locators for the manifest, source
TL and primary MIDI/SFZ/WAV. Reading an SFZ alone does not deliver its samples;
use ZIP for a portable complete bank. `kind="sfz"` also allows
`encoding="utf8"`; `midi` and `zip` use base64. File reading verifies identity;
`interchange_verify` separately verifies bundle structure and dependencies.

WAV preserves native mono/stereo PCM. MIDI preserves approximate performance,
not synth patches, effects or spatial sound: assign every active native voice
an explicit distinct channel 1..16, review losses, then pass `accept_loss=true`
when saving. Defaults are MIDI format 1, PPQN 960, tempo 500000 microseconds per
quarter and a two-semitone bend range. Generators are not flattened.
Whole-song `sfz` uses the required single trigger `key`. `sfz-bank` instead
requires `patch`, `notes`, `velocities`, `hold_ms`, `tail_ms` and `max_samples`;
its fixed mono note samples omit performance, session effects and spatial
context, so saving also requires `accept_loss=true`.

Export replies default to `detail="compact"`: loss categories/counts, maximum
quantization errors, output paths and sampling budgets remain, while
`omitted_counts` names omitted event/cell/member details. `detail="full"`
returns the complete report; a dry-run full request repeats conversion.
The published manifest always retains that full report. These tools do not
accept operation-receipt arguments. Equivalent shell commands begin with
`jottune-mcp interchange`; its `--help` and the [interchange guide](INTERCHANGE.md)
describe all existing flags. No DAW, sampler or hardware playback is certified
by bundle verification. DX7 export is unavailable.

## Target metrics and availability

There are two target vocabularies. `search` and `find_patch(mode="search")`
filter descriptor tables; `suggest` and `seek` optimize the nine descriptors
below. `note_hz` is searchable but is not an optimization target. Descriptor
measurements describe the stated isolated-note profile, not a whole arrangement.

| Descriptor | Unit and meaning |
|---|---|
| `brightness_hz` | Hz; spectral centroid within the measured 20 Hz–20 kHz band |
| `attack_ms` | ms from note-on to within 0.5 dB of peak |
| `decay_ms` | ms from attack end to 20 dB below peak, or within 0.5 dB of higher sustain |
| `sustain_db` | positive dB below peak over the hold's final 100 ms; 90 means silent |
| `release_ms` | ms from note-off to 60 dB below peak; 0 means already that low |
| `noisiness` | spectral flatness, 0..1; not an audible-noise verdict |
| `inharmonicity` | peak distance from note harmonics, 0..1 |
| `odd_even_db` | dB ratio of odd harmonics 3,5,… to even harmonics 2,4,… |
| `peak_dbfs` | sample peak at the patch's written level; other note measures use the declared normalization |

These are measured outcomes, not synthesis parameters with similar names.
For example, descriptor `release_ms` measures time to 60 dB below the measured
peak; `p.amp(...,release_ms)` configures the final envelope's release rate.
The rendered waveform, other envelopes and measurement threshold determine
the measured time. Do not copy descriptor values into patch fields expecting
an exact inverse. Descriptor `sustain_db` is measured dB, while native
`SUSTAIN_DB10` uses tenths of a dB.

Example: `suggest(target=["brightness_hz=1200..2400"],probe=false)` searches
existing measurements. `search(ranges=["note_hz=440..440"])` filters their
probe pitch in Hz; a `note` argument to measurement tools instead uses mHz.
Always compare measurement profiles before treating descriptor differences
as a change in sound. Missing/unmeasurable values do not meet a target.

`render`, `compose` and `compare` instead check numeric dictionary paths in
their complete result, using `KEY=VALUE` or `KEY=LOW..HIGH`. This is not a
closed descriptor list: a path must actually resolve to a finite number.
Booleans, null, strings and array indexes are not numeric targets. Unknown
paths remain **unchecked**, not passed or silently renamed.

| Operation / example target | Available evidence |
|---|---|
| `render` / `compose`: `clipped=0`, `peak_dbfs=..-3`, `rms_dbfs=-40..-10` | Whole native render; final PCM clipping count and native printed dBFS levels |
| `compare` with TL: `a.clipped=0`, `b.clipped=0`, `b.peak_dbfs=..-3` | Each complete native operand; bare `clipped=0` has no top-level match |
| `compare` with WAV: `a.peak_dbfs=..-3`, `b.rms_dbfs=-40..-10` | Original-file PCM levels; native clipping counts are unavailable |
| `compare(measure=true)`: `b.descriptors.PATCH.values.brightness_hz=1200..2400` | Measurable isolated patch named PATCH from a TL operand; replace PATCH with its actual key |

Comparison deltas such as `play.rms_dbfs` are arrays `[before,after,delta]`,
so `play.rms_dbfs.2` is not a supported numeric path. Read the delta and
target a measured operand. WAV silence has null dBFS; native displayed
values use their own printed precision. Waveform equality, requested checks,
publication and listening are separate outcomes. `checks_pass=false` does
not undo an otherwise accepted render or remove its saved take.

`measure_section` reports final-output `combined` and `per_channel` statistics:
integer `peak_lsb`, exact energy `sum_squares` as a decimal string, and
`rms_milli_lsb` rounded down to one-thousandth of a PCM unit. Its dBFS values
are host calculations, null for silence. It has no target argument; inspect
these results separately from its full-record native checks. Combined stereo
statistics pool samples, without downmixing. Four/sixteen-channel field output
reports component energy, not decoded ears, per-voice contribution or loudness.

## Operation work and files

The table describes normal successful bodies, not observed counters or a
time/memory bound. Native discovery is normally two children (`--params`,
`--gen-params`); each native play also runs its existing block-independence
passes. Child calls and DSP passes are different units. Refusals can happen
before or after discovery/render/publication: an unknown parameter-reference
section still runs both listing children. Partial failures retain only
the work actually reported. Optional receipts add request/result files,
including failed attempts, separately from the body writes below.

| Operation / mode | Native work and PCM | WAV and retained files |
|---|---|---|
| `params`; `reference` native sources | 2 children; no PCM | No WAV or body artifacts; every reference page repeats both listings |
| `reference` guide/recipes/reference-options | No native work or PCM | No body artifacts; re-read/rebuild each page. MIDI/transcribe options run one Python help child |
| `load_patch` | No native work or PCM; recipe expansion only | Temporary text files, no retained patch; each page re-expands |
| `search`; `find_patch(mode="search")`; `suggest(probe=false,among=null)` | No native synthesis; table analysis | No WAV/body publication; find_patch runs a Python child |
| `check_patch`; `render(wav=false)`; `compose` | Normally 2 discovery + 1 full play; synthesis occurs even without WAV | Temporary input/canonical TL. Render/compose retain canonical TL and requested logs; check_patch does not |
| `render(wav=true)` | Same play count | Full temporary WAV, then retained WAV with record/log |
| `describe(measure=false)`; `probe_plan` | 2 discovery + source resolution: 1 play for file/recipe, 2 for piece directory | Temporary canonical TL, no WAV; describe `out` retains a record; probe_plan retains no candidate |
| `describe(measure=true)` | Full source and isolated-note renders per patch; variable | Temporary full-source and normalized-note WAVs; optional `out` retains the source result |
| `compare(measure=false)` | 2 discovery + 1 play per TL operand; WAV operands are read, not synthesized | Temporary canonical TL for TL operands; no newly written WAV |
| `compare(measure=true)` | Adds isolated-note renders for TL patches and audio analysis; variable | Temporary WAVs for TL operands/isolated notes; no ordinary body publication |
| `find_patch(mode="describe")`; `catalog_build` | Native note measurement through a Python worker or catalog loop; variable | Temporary WAVs; catalog_build writes its explicit table |
| `gen_events` | 2 discovery + 1 schedule child; no PCM | Input snapshots/temporary files, no WAV/take; page/summary calls recompute the complete schedule |
| `score_inspect`; `score_revise` | No native synthesis; structured-source validation, expansion and comparison | Required receipts retain exact source/results; no WAV or accepted native take |
| `measure_section` | 2 discovery + 1 complete native play; all preceding DSP state runs | Full temporary WAV and full WAV in host memory; no persistent WAV. A small section does not bound render time, RAM or disk |
| `measure_sections` | 2 discovery + 1 complete native play for1..32 ordered windows; two render passes | No audio file; bounded host chunks. Native has two full PCM arrays.512MiB final-PCM admission and120s child/capture budget; no global RSS/filesystem bound |
| `measure_voice_sections` | 2 discovery + 1 play; two full-mix passes plus one dry pass;1..64 explicit voices ×1..32 windows | No audio file; exact pre-FX Q30. All declared voices transported;512MiB each final/dry representation and120s cooperative child/capture/analysis budget |
| `suggest(probe=true)` or `suggest(among=...)`; `seek` | Candidate/retained-take synthesis and analysis; variable, including when probe=false with among | Temporary WAVs; only seek publishes measured baseline/improvements. Inspect render_attempts/consumed_renders and probe_plan bounds |
| `log_replay` | 2 discovery + 1 play per stored take, until failure; new synthesis | Temporary canonical TL, no WAV or new take |
| `log_show`; `artifact_read` | No native work; verification and journal/artifact reads | No new WAV; existing lock handling can write a lock file. Artifact pages verify the complete artifact each time |
| `session_export`; `session_verify` | No synthesis/replay | Export copies selected retained stores, possibly including existing WAVs; verify checks bytes, not audio |
| `interchange_inspect`; `interchange_export` | Native source validation renders even for dry runs; sampled banks add one verified render per explicit cell | WAV/SFZ conversion uses temporary audio; only explicit new `out` retains a complete directory/ZIP. Compact replies omit per-event/per-cell detail; the output manifest retains it |
| `interchange_verify` | No native work or replay | Reads directory/ZIP bytes and format dependencies without extraction or new files; receiver playback remains untested |
| `midi_import(dry_run=true)` | One Python child, no synthesis | Reports conversion plan; no output directory. Non-dry conversion renders and retains its declared outputs |
| `transcribe` | One Python worker, audio analysis and optional template/refinement/replay native calls | Writes accepted TL/report; preview may write WAV. Read options and actual budget counters |
| `analyze` | One Python analysis child; no native synthesis | Reads supplied WAV; spectro writes the requested PNG, other modes return metrics |

Explicit `parent_record` verification adds a native play when a take is
published. Compact delivery does not reduce computation. Re-requesting
describe/compare pages recomputes measurements; retained receipt artifact
pages do not. A TL-only session must avoid measured modes, retained-take
suggestions and other rows that create temporary WAVs. No-WAV does not mean
no PCM, no temporary files, no CPU work or no memory use. Use actual counters
where provided; do not infer a session total from operations lacking them.

## Identities and revision parents

| Returned identity | Identifies | Correct use |
|---|---|---|
| `source_sha256` from compose | Canonical JSON of original structured intent | Check the exact editable intent; not a canonical TL file hash |
| `expanded_source_sha256` | Expanded version1 score after explicit phrases | Compare lowering input; does not replace original phrase intent |
| `record_sha256` | Canonical native TL bytes | `parent_record` in the same take log; record artifact retrieval |
| `hash` | Native PCM FNV-1a64 | Replay/audio comparison; not a SHA256 artifact locator |
| `wav_sha256` | Complete WAV file bytes | WAV artifact retrieval; not the PCM hash |
| `receipt.request_sha256` | One journaled operation request/attempt | `receipt_parent` in the same operation journal |
| `receipt.response_sha256` | Receipt response wrapper | Retrieve the outcome; never use as receipt_parent |
| `receipt.full_result_sha256` | Complete result JSON retained separately | Retrieve omitted evidence without rerunning the operation |
| `selected_sha256`, `source_sha256` from reference | Selected and whole reference text | Compare pages from that reference source; these are a different namespace from compose source intent |
| `manifest_sha256`, `input_set_sha256` from gen_events | Exact native schedule bytes and ordered input snapshot identity | Guard/compare the corresponding evidence, not song ancestry |

For a revision in the same stores, set `parent_record=FIRST.record_sha256`
and `receipt_parent=FIRST.receipt.request_sha256`; use the original `log`
and `receipt_dir`. Neither response SHA nor full-result SHA substitutes for
the request SHA. `attempt` is a journal ordinal, not a portable identity.
A saved take and an operation receipt are independent: inspect publication
and receipt state. Pending or unknown receipts do not establish completion.
After restart discover stores with `log_show`, retrieve by exact digest, and
verify complete bytes. An external source path alone is not a project backup.

## Musical-time scores

`compose(score=OBJECT,log=TAKES,receipt_dir=OPERATIONS)` accepts an explicit
musical-time score and publishes a native-validated take without creating a WAV.
It uses the existing recipe expansion and rational clock. This is useful when
you want notes and spans in quarters instead of calculating nanoseconds. Supply
every stream and voice explicitly; the adapter does not allocate voices or
repair overlapping notes. The following complete score plays a short phrase:

```json
{
  "version": 1,
  "fs": 48000,
  "name": "Clock seed",
  "clock": {
    "bars": 1,
    "meter": [{"bar": 1, "quarters": 4}],
    "tempo": [{"bar": 1, "bpm": 120}],
    "ramp": "step",
    "tail_ns": 300000000
  },
  "declarations": "recipe keys epiano c_level=24\n",
  "streams": ["melody"],
  "voices": ["keys"],
  "events": [
    {"kind": "note", "at": 0, "duration": "3/4", "stream": "melody", "voice": 0, "pitch": {"note": "C4"}, "velocity_q15": 20000},
    {"kind": "note", "at": 1, "duration": "3/4", "stream": "melody", "voice": 0, "pitch": {"note": "E4"}, "velocity_q15": 18000},
    {"kind": "note", "at": 2, "duration": "3/2", "stream": "melody", "voice": 0, "pitch": {"note": "G4"}, "velocity_q15": 19000}
  ]
}
```

Times are zero-based quarters, using nonboolean integers or exact fractions
such as `"3/2"`. Decimal strings/floats are refused. Fraction denominators are
at most4096 and absolute numerators at most2^53-1. Each meter/tempo map starts
at bar1 with strictly increasing anchors. Tempo may have an endpoint anchor
at bars+1; `linear` interpolates beat duration, not BPM. Tail is render-only;
musical events and their endpoints must lie in the mapped extent. An event
at length with zero tail retains the native refusal. Positive spans may round
to zero nanoseconds; no minimum duration is inserted.

The closed discovery schema lists `note`, `on`, `off`, `pitch`, `ctl`, `filter`,
`send`, `crush`, `pos`, `move`, `yaw`, `gen`, `lane` and comment-only `mark` events.
Pitch is exactly one of `{note:"C4"}`, `{midi:60}`, `{millihz:261626}`. Note/MIDI
convenience values cover MIDI0..127; direct mHz is validated natively. Voice
indices refer to `voices`; stream names refer to `streams`. Native merges time,
stream declaration order and same-stream input order. A note expands to on/off
in source order, so a later retrigger can still be released by an earlier span's
off. Use explicit events and ordering when ownership matters.

`declarations` accepts native non-event rows and recipes, excluding adapter-owned
timeline/engine/fs/length_ns/streams/voice/hash rows. Native owns field domains,
FX routing and coupled validation. Expansion runs once for the complete block,
preserving recipe asset deduplication. Generator clocks, divisors, slew and lane
tick values keep their native units; tempo changes never rescale them.

Bounds are1MiB canonical score JSON,8192 event objects,16384 authored native
event rows,4MiB expanded TL,16 streams,64 voices,128 anchors per map,
65536 quarters and600 seconds, at44100 or48000Hz. All text is UTF8; score text
rejects NUL and descriptive fields reject newlines. Unknown fields are errors.
These are service bounds, not native limits or wall-clock cost guarantees.

The default compact result keeps source/authored/canonical/PCM identities,
actual `native_calls`, checks and `publication_state`. A normal first take uses
two discovery calls and one candidate play. A valid `parent_record` from the
same log adds one parent play. Input errors can cost fewer calls; internal DSP
passes are not counted as separate calls. Failed user targets retain a valid
take; the first expected-hash check is unchecked. `detail="full"` is unbounded.

Both detail modes retain the complete result in the required receipt store.
Use `artifact_read(directory=OPERATIONS,kind="receipt",digest=FULL_RESULT_SHA,
encoding="utf8")` to assemble it without new synthesis. It contains expanded
`authored_tl` and `line_map`; exact event pointers and coarse expanded declaration
lines support native error recovery. The request receipt retains the original
score. Argument/transport/UTF8/size failures before setup have no receipt.
Publication is `not_attempted`, `complete`, or `unknown` when storage can have
partially completed. Receipt publication can fail after a valid take exists;
inspect pending evidence before retrying. Dependency snapshots identify disk
bytes before/after work; they do not attest already loaded daemon code.

CLI: `python3 -B tools/fm.py compose --score score.json --log TAKES
--receipt-dir OPERATIONS --json`. CLI rejects duplicate JSON keys. Save editable
TL and canonical records when retaining compositions in this repository.

Version2 can reuse literal phrases through the same operation. For example:

```json
{
  "version": 2, "fs": 48000, "name": "Repeated clock seed",
  "clock": {"bars": 2, "meter": [{"bar": 1, "quarters": 4}],
            "tempo": [{"bar": 1, "bpm": 120}], "ramp": "step", "tail_ns": 300000000},
  "declarations": "recipe keys epiano c_level=24\n",
  "streams": ["melody"], "voices": ["keys"],
  "phrases": [{"id": "rise", "events": [
    {"kind": "note", "at": 0, "duration": "3/4", "stream": "melody", "voice": 0, "pitch": {"note": "C4"}, "velocity_q15": 20000},
    {"kind": "note", "at": 1, "duration": "3/4", "stream": "melody", "voice": 0, "pitch": {"note": "E4"}, "velocity_q15": 18000},
    {"kind": "note", "at": 2, "duration": "3/2", "stream": "melody", "voice": 0, "pitch": {"note": "G4"}, "velocity_q15": 19000}
  ]}],
  "events": [{"kind": "phrase", "phrase": "rise", "at": 0,
              "repeat": {"count": 2, "every": 4}}]
}
```

Phrase body times are relative quarters. A placement adds its `at`; finite
repetitions add `repeat_index * every`. Omit repeat for one placement. Repeat
count is2..8192; every is positive. Ordinary events may appear beside placements,
and bodies support the same ordinary event forms, including FX and spatial
controls. Voices and streams remain global and explicit. Bodies cannot invoke
other phrases; no transposition, sorting, deduplication or implicit state reset
occurs. Expansion order is top-level item, repetition, body event; native merge
ordering still applies. Overlapping repeats remain your authored performance.

Definitions and top-level items are each bounded to8192, with at most8192 total
body-event plus top-level nodes,8192 expanded events and16384 authored rows.
Original JSON, expanded version1 JSON and complete event-origin JSON each have
a1MiB limit. Derived onsets must fit the same exact rational bounds. Unused
bodies still receive shape/ownership/relative-endpoint validation. Full evidence
retains original `source_sha256`, separate `expanded_source_sha256`, expanded
source, `expansion_counts` and `event_origins`; line maps and errors identify
original definitions, placements and repetitions. Compact replies omit those
bulk arrays; retrieve the existing full receipt without composing again.

### Explicit transforms, chords, automation and regions

Version3 keeps the same clock, declarations, voices and native validation, and
adds finite source expansions. Version1/version2 retain their original rules.
This complete score repeats a compressed, transposed phrase, then plays an
explicit two-voice chord while sampling a controller:

```json
{
  "version": 3, "fs": 48000, "name": "Transformed clock seed",
  "clock": {"bars": 2, "meter": [{"bar": 1, "quarters": 4}],
            "tempo": [{"bar": 1, "bpm": 120}], "ramp": "step", "tail_ns": 300000000},
  "declarations": "recipe keys epiano c_level=30\nmod keys 0 ctl0 pitch0 100 3\n",
  "streams": ["melody"], "voices": ["keys", "keys"],
  "phrases": [{"id": "rise", "events": [
    {"kind": "note", "at": 0, "duration": 1, "stream": "melody", "voice": 0,
     "pitch": {"midi": 60}, "velocity_q15": 16000},
    {"kind": "note", "at": 2, "duration": 1, "stream": "melody", "voice": 0,
     "pitch": {"midi": 67}, "velocity_q15": 16000}
  ]}],
  "events": [
    {"kind": "phrase", "phrase": "rise", "at": 0, "repeat": {"count": 2, "every": 2},
     "transform": {"time_scale": "1/2", "pitch": {"domain": "midi", "semitones": 12}}},
    {"kind": "chord", "at": 4, "duration": 3, "stream": "melody", "notes": [
      {"voice": 0, "pitch": {"midi": 60}, "velocity_q15": 16000},
      {"voice": 1, "pitch": {"midi": 67}, "velocity_q15": 16000}]},
    {"kind": "automation", "at": 4, "stream": "melody", "voice": 0, "input": 0,
     "points": [{"offset": 0, "value_q15": 0}, {"offset": 1, "value_q15": 32768},
                {"offset": 3, "value_q15": 0}],
     "every": "1/8", "interpolation": "linear", "rounding": "nearest_even", "end": "include"}
  ],
  "regions": [{"id": "question", "start": 0, "end": 4},
              {"id": "answer", "start": 4, "end": 8}]
}
```

For phrase placement at A, repeat r, spacing E and scale S, onset is
`A+r*E+S*body_onset`; durations multiply by S. Repeat spacing does not scale.
MIDI transforms require explicit MIDI pitches: optional `invert_about` reflects
first, then `semitones` transposes; results outside0..127 refuse. Alternatively,
`{"domain":"millihz","ratio":"3/2","rounding":"nearest_even"}` scales explicit
mHz pitches with exact rational arithmetic and ties-to-even integer rounding.
Named notes remain usable without a pitch transform. No inferred key, automatic
harmonization or voice allocation occurs. Chords need1..64 distinct explicit
voices; their member order is retained.

Automation targets existing `ctl` inputs0..7, with values−32768..32768. Bind
the controller to a native modulation target in declarations, as above. Points
start at offset0 and strictly increase. Sample every positive rational interval
while offset is less than the final point; `end:"include"` adds that endpoint
once, even off-grid. `hold` selects the preceding point; `linear` interpolates
exactly before nearest-even rounding. Off-grid intermediate knots add no sample.
Phrase time scaling applies after local sampling. An included endpoint at the
render end still receives the ordinary native refusal; explicitly exclude it
or provide a sufficient tail. No hidden clipping or tail extension occurs.

Optional regions are up to128 unique named half-open quarter intervals, which
may overlap. Inspection summarizes onsets separately from intersecting spans,
pitch ranges, explicit spatial events and phrase occurrences. Revision compares
regions by ID without inventing note identity. Regions do not change sound.
Compact replies omit these bulk tables explicitly; full receipts retain them.

Version3 retains the8192 expanded-event/16384-row and1MiB source/expanded-source
bounds. Chord members, automation points and regions count toward8192 source
nodes, including unused definitions. Joint event-origin/phrase-occurrence JSON
is limited to1MiB. Transformed coordinates retain existing rational bounds;
compression cannot legalize an invalid original phrase. Long controller curves
can still exceed the expanded-event limit.

## Editing and recovering scores

An editable score and its canonical native TL have different identities. Keep
both: the score retains musical intent and phrases; TL is exact native replay.
After `compose`, `score_inspect` or `score_revise` completes its receipt,
`reply.receipt.source_artifact` contains `{kind:"score",digest,filename}`.
Add `directory` equal to that call's explicit `receipt_dir`. The resulting
object works unchanged as `source_ref`, or as `artifact_read` arguments.
For readable pages, add `encoding:"utf8"` to the reader call only. Follow
`next_offset` to EOF, hash the assembled bytes against `digest`, then parse JSON.
The filename's SHA identifies the containing receipt, not the source itself.

After a receipted `compose`, an optional `reply.receipt.text_artifact` gives
`{kind:"authored_tl",digest,filename,size}` for the exact lowered TL. This can
remain useful when native validation refused the composition; it is not an
accepted canonical take. To call `artifact_read`, remove `size`, add `directory`
equal to that call's explicit `receipt_dir`, and retain its exact filename.
Use `encoding:"utf8"`, follow `next_offset` to EOF and verify the selected
digest. This locator is for text retrieval; it does not replace the editable
score's `source_ref`.

The following is a generic Python-client sequence. `score` is an explicit
version1/version2/version3 object such as the complete examples above; directory variables
are caller-selected paths. MCP uses the same operation names and arguments.

```python
first = fm.compose(score, log=takes, receipt_dir=operations)
assert first["exit"] == 0 and first["receipt"]["state"] == "complete"
source_ref = {"directory": str(operations), **first["receipt"]["source_artifact"]}
view = fm.score_inspect(source_ref=source_ref, start=0, end=4,
                        receipt_dir=operations)
# Change only the first event's velocity in this example. Supply the exact
# current value, never a remembered guess; inspect/recover source if necessary.
revision = fm.score_revise(source_ref=source_ref,
    expected_source_sha256=first["source_sha256"], pointer="/events/0/velocity_q15",
    expected=score["events"][0]["velocity_q15"], value=12000,
    receipt_dir=operations)
assert revision["exit"] == 0 and revision["receipt"]["state"] == "complete"
revised_ref = {"directory": str(operations), **revision["receipt"]["source_artifact"]}
child = fm.compose(source_ref=revised_ref, log=takes, receipt_dir=operations,
                   parent_record=first["record_sha256"])
```

`score_inspect` selects a half-open quarter range, reporting bar/ns endpoints,
authored per-voice/stream counts and gate advisories using the complete preceding
history. An intersecting note span differs from a note onset. Native bindings,
generated activity and audible contribution remain unknown; inspection executes
no native child. Raw TL can still be rendered/described, but is not reconstructed
into editable musical-time intent. Structurally invalid sources or invalid pitch
spellings must first be recovered and corrected as complete inline objects.

`score_revise` replaces one existing RFC6901 pointer after exact source SHA and
old-value checks. Escape `~` as `~0` and `/` as `~1` in object keys. Replace arrays
explicitly to insert/delete; replace `""` for the complete root. A phrase
definition edit affects all its invocations; a placement edit affects its
repeats. To edit one repeat, explicitly replace its placement with separate
placements or literal events. There is no implicit detachment or voice repair.
Differences retain exact added/removed occurrences and report timing/order/context
changes separately; they neither infer note identity nor prove audio equivalence.
Revision performs zero native work and publishes no take. Native validity comes
from the following compose call. Stale edits may leave diagnostic operation
receipts, but cannot publish a revised take.

Both views default to compact, retaining complete source/event/origin/advisory/
diff evidence in existing receipts. Retrieve `full_result_sha256` through
`artifact_read(kind="receipt")` without recomputing. Explicit `detail="full"`
can exceed a client's display limit. After restart, use
`log_show(directory=operations,kind="operations")` to discover exact source and
result locators; do not infer them from conversation history. Every projected
source page verifies its complete parent receipt and exact source hash, so it
uses zero native calls but still rereads that parent. The supported Python
client helper `from tools.recover import recover_artifacts` accepts kind=score,
authored_tl and generator_manifest
for budgeted verified delivery through an already connected MCP client; it is
not a server tool. Its plan, byte/call limits and example are in
`tools/README.md`, under "Reusable score phrases and client recovery". Other MCP clients
can use the same `artifact_read` pages directly. Export the explicit take and
receipt roots with `session_export`, verify the bundle and relocate `directory`
using its manifest. Every new complete result freezes the source it used, even
when its input reference came from a different store. Pending/unknown receipt
publication is not evidence that an artifact is retrievable.

For that Python helper, `client` is your connected SDK client; `destination`
is a new local directory whose parent exists. Choose explicit delivery budgets:

```python
from tools.recover import recover_artifacts
delivery = await recover_artifacts(
    client, [{"name": "score.json", **source_ref}], destination,
    max_artifact_bytes=artifact_limit, max_total_bytes=total_limit,
    max_calls=call_limit)
```

Inspect `ok`, `failure`, `counts` and `manifest`: each completed file is hashed
before publication without replacement. Budgets count returned bytes and helper
calls, not server I/O or SDK memory. There is no retry/resume or new connection.
For a text_artifact helper entry, add `name` and `directory` to the locator;
the helper accepts its `size` field. Plans containing authored_tl or
generator_manifest use page_bytes<=8192. Both new reader kinds accept requested
pages1..8192 bytes in UTF8 or base64, with a15360-byte serialized public reply
limit including optional receipt metadata. Their parent receipt must fit64MiB
and selected text16MiB. Every page verifies and parses that entire parent,
without native execution or new audio work.

CLI equivalents use `score-inspect`, `score-revise` and compose's `--source-ref`
JSON file alternative to `--score`. Revision's `--expected` and `--value` are JSON
files; the pointer and expected source SHA are explicit flags.

## Measuring the changed section

Use `measure_section(files=[canonical_path],start_ns=...,end_ns=...,
receipt_dir=operations)` before and after an edit. CLI uses `measure-section`.
Compare selected `combined`/`per_channel` peak and energy, and selected PCM SHA.
Change the selection intentionally; an edit outside it need not alter it.

Measurement renders the complete session from frame0, preserving delay, filter,
generator and effect history, then selects final signed16 PCM. It normally uses
three native children and two block render passes, creates/reads a full temporary
WAV, and deletes it. Small selections do not cap CPU, RAM or disk. No persistent
WAV or take is published. Optional receipts preserve complete outcomes so later
reads do not render again. No receipt means no implicit result store.

Time bounds are half-open nanoseconds. Each sample timestamp is frame_index/fs;
ceil endpoints intersect the native physical EOF, reported explicitly by raw and
realized frame bounds and `end_clipped`. End defaults to declared length; empty
or beyond-record selections refuse. Sum of squared signed16 values is an exact
decimal string; RMS is floor milli-LSB, with dBFS null for silence. Combined
energy is over interleaved components: mono1, stereo/binaural2 or field4/16.
There is no downmix, per-voice isolation, localization or perceptual loudness
claim. Native checks apply to the full record and expected-hash mismatch remains
exit3. Numerical change and actual listening are separate evidence.

For an explicit no-audio-file path, use the ordered batch operation:

```python
measured = fm.measure_sections(
    files=[canonical_path],
    sections=[{"start_ns": 0, "end_ns": 1000000000},
              {"start_ns": 2000000000, "end_ns": 3000000000}],
    receipt_dir=operations)
```

MCP accepts the same `sections` array; CLI uses `measure-sections FILE...`
and `--sections-json` with the array as JSON text. Each of1..32 objects admits
only `start_ns` (default0) and `end_ns` (defaultnull, declared end); `{}` selects
the whole record. Repeated, overlapping and out-of-order windows remain in
request order. One invalid window refuses the batch. All statistics and native
checks have the same meanings above; one full render pair serves the batch.

The native writer sends PCM16 WAV framing through a pipe, without creating a
regular audio file. Host analysis retains bounded chunks and accumulators;
native still allocates two full PCM arrays. The opt-in limit is512MiB for one
final PCM buffer (up to1GiB for the pair), with a120s aggregate child/capture
deadline and bounded cleanup effort. These limits do not bound total process
memory, filesystem latency or receipt storage. A small window still needs the
whole session's history. Compact output explicitly omits per-channel detail,
then the entire section array if needed; it never hides a successful prefix.
Use receipts to recover full evidence. Without receipts there is no retained
result behind a digest alone. Existing `measure_section` remains unchanged.


For explicit dry-voice diagnostics, select native indices rather than inferring
ownership from a final mix:

```python
dry = fm.measure_voice_sections(
    files=[canonical_path], voices=[0, 2],
    sections=[{"start_ns": 0, "end_ns": 1000000000}, {}],
    receipt_dir=operations)
```

MCP has the same required arrays; CLI uses `measure-voice-sections FILE...`
with `--voices-json` and `--sections-json`. Choose1..64 distinct indices0..63;
all selected voices must exist. Voice mappings retain native patch names.
Each of1..32 sections preserves full history and request order. Per-voice
peak, exact squared energy, floor milli-RMS and selected-word SHA256 describe
signed Q2.30 samples at `voice_out_pre_fx`: after voice filters/formants/shaping/
amp, before crusher, spatial/pinna, sends, wet buses and master. The dB reference
is2^30, not32768. Above-unity dry values are possible; they are not a final
clipping, masking, audibility or localization verdict. Shared nonlinear FX
contributions and per-voice wet images remain separate capabilities.

The two full-mix verification passes still run, then one sequential dry pass.
`dry_block_indep=unchecked` honestly describes that single dry pass. All declared
voices travel in a bounded PCM32/Q30 pipe even when only one is selected; no
regular audio file or full Python PCM is retained. Each final/dry payload must
fit512MiB, which allows about43.69s at48kHz with64 voices. The cooperative120s
child/capture/analysis deadline may overrun by a bounded step and does not bound
all process memory or filesystem/receipt latency. Planned pass counts, verified
completed stages, actual launches/attempts and accepted measurements are separate
facts. A completed mix or dry stage can survive a later failure. Compact output
may omit all detailed rows; retain receipts for complete recoverable evidence.

## Instrument overview

This is the guide for composing music with JotTune Synth.
It describes the implemented interfaces. You can write an
independent Python score or a text timeline, render it offline, change
the sound and replay the exact result. You do not need to add a function
to `compose.py`, register a piece or change the engine.

The voice instrument has two to four operators, explicit routing,
envelopes, modulation, filters and hybrid sources. The host adds recipes,
musical time, sequencing, controller automation, seeded generators,
spatial movement and effects. A patch makes sound; a voice is one instance
of a patch; a stream orders events; a session owns their definitions.
The result to keep is the canonical merged text record, its source score
and seed values, build identity, measurements and PCM hash. A WAV is a
render of that record, not a replacement for it.

The repertoire is larger than the set of valid combinations. Missing
prerequisites, exhausted modulation slots, incompatible source bindings
and ambiguous ownership are errors. Fix the musical request; do not
disable validation or change the engine to make it pass.

Contents: [setup](#start-with-an-existing-renderer),
[shared CLI and MCP](#shared-cli-and-mcp),
[complete experiment](#a-complete-external-output-experiment),
[score API](#build-a-score-with-piece),
[time and sequencing](#time-rhythm-and-deterministic-variation),
[patches](#patches-recipes-and-the-synthesis-repertoire),
[modulation](#modulation-macros-and-state-morphs),
[generators](#session-generators-and-live-lanes),
[space and FX](#spatial-performance-and-effects),
[MIDI](#midi-imports-and-text-exports),
[verification](#inspect-revise-and-hand-off),
[native grammar and ranges](#native-instrument-and-timeline-reference).

## Start with an existing renderer

Use Python 3 and a **prebuilt native harness supplied for this checkout**.
The approved music configuration is sine table 12 bits, exponent table
10 bits, 64-bit phase and envelope. Binaural rendering additionally needs
the matching head-filter tables. Do not silently use a binary from a
different revision. An app build may contain an exported matching harness
at `build/s12e10p1v1/harness`. Rebuild it after changing native sources.

Set the two paths explicitly, from the repository root:

```sh
export REPO="$PWD"
export HARNESS="/absolute/path/to/the/supplied/harness"
export WORK="$(mktemp -d /tmp/fm-instrument.XXXXXX)"
test -f "$REPO/tools/music/compose.py" && test -x "$HARNESS"
printf 'Outputs: %s\n' "$WORK"
```

The supplied binary is a prerequisite. If it is missing or incompatible,
report that; the following workflow does not build it. Python authoring,
recipe expansion and MIDI dry runs use the standard library. WAV analysis
needs numpy; spectrograms additionally need matplotlib. Do not run Python
with `-O`: some older authoring checks use assertions. The native reader
is the final validation authority even when Python accepts a score.

If you are explicitly preparing a fresh checkout, install a C99 compiler,
make and Python 3, then the optional approved host build is:

```sh
make -C "$REPO" all
export HARNESS="$REPO/build/s12e10p1v1/harness"
```

This separate setup step writes build artifacts in the checkout, including
generated tables; it does not render the corpus or play audio. Plain
`make all` uses the approved configuration. The external-output experiment below
assumes setup is already complete. See [the README](../README.md) for backend and sanitizer builds.

These discovery commands neither build nor render:

```sh
python3 -B "$REPO/tools/music/compose.py" --recipes > "$WORK/recipes.txt"
python3 -B "$REPO/tools/music/analyze.py" search --tag piano
"$HARNESS" --gen-params > "$WORK/generator-parameters.txt"
```

The recipe listing includes complete expanded banks and can be long.
There are currently 63 recipes, all covered by the committed descriptor
search. Descriptors measure the default single note and controls; they
do not describe every expressive performance or parameter combination.
`--gen-params` is a generator-specific descriptor
table, not a complete synth schema. Harness `--help` prints usage to stdout and exits successfully;
`--version` reports the public synth version.

## Shared CLI and MCP

The host tools require Python3.10 or later; the older composer remains
usable with Python3.8. Select the approved native executable explicitly.
The CLI does not build or replace it:

```sh
python3 -B tools/fm.py --harness "$HARNESS" params --json
python3 -B tools/fm.py --harness "$HARNESS" describe bell --measure --json
python3 -B tools/fm.py --harness "$HARNESS" render "$WORK/merged.tl" \
  --log "$WORK/takes" --wav --target clipped=0 --json
python3 -B tools/fm.py --harness "$HARNESS" compare "$WORK/before.tl" \
  "$WORK/after.tl" --measure --json
python3 -B tools/fm.py --harness "$HARNESS" log replay "$WORK/takes" --json
python3 -B tools/fm.py search brightness_hz=1000..5000 --json
python3 -B tools/fm.py --harness "$HARNESS" suggest \
  --target brightness_hz=2000..3000 --from bell --probe --json
```

For these file-based CLI examples, create the referenced input files first using the experiment below or your
own score. `describe` also accepts a canonical TL or explicit piece directory;
a directory resolves its native-declared stream siblings. `--note` is mHz.
Without `--measure`, descriptions expose native fields without note analysis.
Whole-mix spatial images use100ms windows with actual start/frame counts.
Positive ITD means the right channel leads; positive ILD means it is louder.
ILD is10 log10(right/left band power), over200–1500,1500–6000 and6000–16000Hz
with upper boundaries excluded. ITD uses frequencies through1500Hz and a
fixed search of±48 frames, interpolated to1/16 sample. Periodic signals can
give ambiguous peaks; no estimator-confidence value is currently supplied.
ITD/ILD are null for windows shorter than96 frames or an entirely zero ear;
undefined band ratios also become null. Correlation is null for a constant
ear. Side/mid is10 log10(side power/mid power), with side=(L−R)/2 and
mid=(L+R)/2; either zero power makes it null. Null is not0dB or a failed render.
These are whole-mix signal statistics. WAV acoustic analysis accepts only mono
or stereo, so AmbiX retains render channel levels and replay without this
analysis. Per-voice acoustic images remain unavailable in this interface.
`params` supplies native names, scalar bounds, units, divisor scales and
coupled-check identifiers. Scalar bounds alone cannot validate combinations.
`search` covers recipes and a committed catalog when present; catalog rebuilds
are explicit. `suggest` ranks measured descriptor targets; `--probe` opts into
native field experiments and reports their render cost. `--among LOGDIR`
ranks comparable retained takes and explains excluded measurement profiles.
It does not automatically edit or publish a sound.
A probe move's `accepted:true` means native validation accepted the candidate,
not that a revision was durably retained. Inspect measurable/reason as well.
Only seek's top-level accepted state, verified history and take log identify
published revisions. Probe has no render-budget argument and its work is not
reused by a later seek; seek starts a new explicitly budgeted search.
Before choosing that budget, call `probe_plan(source=SOURCE,patch=PATCH)`
(CLI `probe_plan SOURCE [--patch PATCH]`). It validates the source, reports
the renders that planning actually used, counts eligible candidates and gives
upper render bounds for a fresh probe, baseline publication and one improving
seek sweep. The probe bound applies to descriptor tables with `among=None`;
ranking retained takes has additional costs outside this estimate. Optional
`parent_record` adds parent-verification cost but is not itself verified here.
No candidate runs or measurements occur during planning. These are structural
bounds, not runtime estimates or a promise to reach the target; planning work
is not reused by the following operation. Include `note` when overriding pitch.
For a bounded automatic revision, use `seek --target KEY=V --from SOURCE
--log DIR --budget N [--tol KEY=T]`. It logs only a fully measured baseline
and independently remeasured strict improvements. Every native render,
including refusals and parent validation, counts toward N. Tolerance is in
the target descriptor's units, default zero. Inspect `reached`,
`stop_reason`, `consumed_renders`, final `path` and measurements; a budget
stop does not mean the target was achieved. No partial sweep wins. The log
retains accepted settings for replay and explicit continuation.

Every JSON reply has `version:1`, `ok` and `exit`. Execution and desired
outcome are separate: a render hash mismatch retains exit3 and `ok:true`;
`seek` refuses that source with exit3 and `ok:false` because it cannot seed
a verified search.
`checks` explain pass/fail/unchecked and `checks_pass` is null when evidence
is missing. A target uses `KEY=V` or `KEY=LO..HI`; missing measurements never
pass. Keys follow numeric dictionary paths in the full result. For `render`,
use `clipped=0`; for `compare` of timelines, use both `a.clipped=0` and `b.clipped=0`.
WAV operands do not expose native clipping counts, so these targets remain
unchecked for WAVs even when other audio measurements are requested.
A bare `clipped=0` in a comparison remains unchecked because that top-level
field is absent. Delta arrays cannot be targeted through numeric indexes.
Native range refusals include file/line, field, token and bounds.
`nearest_validated:false` means a suggested outer-bound clamp still needs
native validation. Host failures are exit5. Numeric comparison deltas are
`[before,after,after-before]`; check their direction against the musical goal.
A failed host target does not reject a native-successful render:
`ok:true, exit:0, checks_pass:false` can accompany retained artifacts and an
appended take. The take log keeps identities, lineage and metrics, but not the
requested target definitions or their verdicts. Opt into operation receipts or
retain the tool response when those checks matter.

Take logs retain canonical records by SHA256 and optional whole WAV files by
SHA256, with native PCM FNV reported separately. `--parent-record SHA256`
links a revision explicitly; omission means no parent. Distinct settings
with identical audio remain distinct records. Each replay is a fresh native
invocation. Copy a log together with every referenced record and WAV; replay
verifies all those artifacts. A TL-only archive can instead be rendered into
a new log without `--wav`. This accepted-take log does not record rejected GUI actions.
`log_show(directory=TAKE_DIRECTORY)` verifies the entire accepted-take log
before returning bounded compact row pages. Follow `page.next_offset` with
the same `expected_index_sha256` from the first reply; changes require fresh
discovery. `count` is the complete take count, not the current page size.
Rows keep identities and metrics; note/diff/space annotations are explicitly
omitted. For complete annotations, use the returned `artifact` kind/digest
with `artifact_read(directory=TAKE_DIRECTORY,kind="take_log",digest=SHA256,encoding="utf8")`
and assemble verified chunks through EOF. `detail="full"` preserves the old
unbounded rows and TSV text; it can exceed a client limit. Plain CLI
`log show` still prints the full TSV; JSON defaults to compact pages.
Retain composed songs and their editable scores in your workspace. WAV files can
be regenerated from a complete TL.

The separate `@chimepond/jottune-mcp` package provides the optional MCP transport.
Select this synth checkout explicitly and print the client configuration:

```sh
export JOTTUNE_SYNTH_ROOT=/path/to/jottune-synth
jottune-mcp doctor --harness "$HARNESS"
jottune-mcp config --workspace "$WORK" --harness "$HARNESS"
```

Use the printed stdio configuration in your agent client. Tools
use the same arguments and versioned replies as the CLI: `reference`, `render`, `params`,
`describe`, `compare`, `log_show`, `log_replay`, `catalog_build`, `search`,
`suggest`, `seek`, `probe_plan`, `session_export`, `session_verify`, `midi_import`, `transcribe`, `artifact_read`,
plus `analyze`, `check_patch`, `find_patch` and `load_patch`.
`check_patch(files=... or text=...,patch_name=...,fs=...)` can wrap bare patch lines in a silent native
context; omit patch_name for complete input pools. `load_patch(name,patch)`
expands the existing recipe through the composer. `find_patch` preserves the
original analyzer's search/describe output. `analyze` offers overview,
loudness and spectro; spectro requires an explicit PNG output path.
Use `reference` for bounded pages or read `fm://instrument` for this exact guide, `fm://params` and
`fm://gen-params` for native discovery, `fm://recipes` and
`fm://descriptors` for existing data, and `fm://catalog` when the committed
catalog exists. Calls keep no current patch or session; paths, parents and
output destinations are explicit. MCP transport and its regression tests live
in the separate `chimepond/jottune-mcp` repository.

## A complete external-output experiment

This writes two separate 0.8-second scores, renders an A4, changes only
carrier attenuation by +6 dB, and replays both canonical records with
different block partitions. Nothing is played through hardware. All
created files are beneath `$WORK`.

```sh
cat > "$WORK/score.py" <<'PY'
import os
import pathlib
import subprocess
import sys

repo = pathlib.Path(os.environ['REPO'])
out = pathlib.Path(os.environ['WORK'])
harness = os.environ['HARNESS']
sys.path.insert(0, str(repo / 'tools' / 'music'))
import compose as c

for name, attenuation_delta in [('base', 0), ('quieter', 6)]:
    p = c.Piece(name, 120, 4, ['One A4; compare carrier attenuation'])
    p.dir = str(out / name)       # Set BEFORE write: the default is in the repo.
    p.stream('notes', 'electric piano')
    p.patch('sound', *c.recipe('epiano', c_level=attenuation_delta))
    voice = p.add_voices('sound', 1, 'one note')[0]
    p.on('notes', 0, voice, 'A4', 32767)
    p.off('notes', 400_000_000, voice)
    p.length_ns = 800_000_000
    p.write()                  # Text only: no build, render or automatic levelling.
    record = out / (name + '.record.tl')
    commands = [
        [harness, '--play', *p.files(), '--metrics', '--wav',
         str(out / (name + '.wav')), '--merged', str(record)],
        [harness, '--play', str(record), '--chunk', 'random', '--metrics'],
    ]
    for i, command in enumerate(commands):
        result = subprocess.run(command, text=True, capture_output=True)
        (out / (name + '.' + str(i) + '.log')).write_text(
            'argv=' + repr(command) + '\nexit=' + str(result.returncode) +
            '\n' + result.stdout + result.stderr, encoding='utf-8')
        print(result.stdout, end='')
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            raise SystemExit(result.returncode)
PY
python3 -B "$WORK/score.py"
python3 -B "$REPO/tools/music/analyze.py" overview "$WORK/base.wav" \
  --window 1 --json "$WORK/base.json"
python3 -B "$REPO/tools/music/analyze.py" overview "$WORK/quieter.wav" \
  --window 1 --json "$WORK/quieter.json"
```

Read both PLAY lines. Require successful exits, replay `match=yes`,
`block_indep=yes` and `clipped=0`. The first unpinned source need not report
a comparison with a prior hash; the merged record contains the computed
hash used by replay. The two hashes should differ. The quieter file's
RMS and peak should each be about 6 dB lower; PCM rounding means the
difference is not exactly 6.000000 dB. This checks a specified effect,
not merely that some bytes changed. Do not use a short tone's inferred
tempo, chord or onset count as a musical-quality verdict.

To make a phrase, replace the one note with successive on/off pairs,
keeping each off before the next on on that voice and leaving a release
tail before `length_ns`. Add voices for overlapping notes. Keep this
external score as the editable source; edits to its generated `.tl` files
will be replaced on the next write.

## Units, clocks and ordering

| Value | Meaning |
|---|---|
| Event time | Integer nanoseconds from session start; `0 <= t < length_ns`. |
| `on_f` / raw pitch | Integer millihertz: A4 is `440000`. |
| `p.on` note | Note-name string such as `C#4`; `midi('A4')` is 69 and `mhz('A4')` is 440000. For a MIDI number use `p.on_f(..., mhz_m(69), velocity, 'A4')`. Performance helpers such as `arp` separately accept their documented MIDI-number inputs. |
| Velocity | Q15, normally 1..32767; 32767 is full scale. |
| Raw level/sustain | Attenuation in tenths of a dB: 200 is 20 dB down. Larger is quieter. |
| `op` / recipe level delta | Whole dB units, potentially fractional: `c_level=6` adds 6 dB attenuation. |
| Raw index/feedback | Q10 radians: 1024 is one radian. `op` and recipe deltas use radians. |
| Raw ratio / phase | Q16 ratio; unsigned 16-bit turns. `op` uses a ratio and phase in degrees. |
| Controller | Signed Q15 in -32768..32768; eight persistent inputs per voice, initially zero. |
| Send | 0..32768, with 32768 unity; initially zero. |
| Position | Millimetres; x right, y ahead, z up. Yaw is clockwise millidegrees. |
| Python `filter` / `cutoff` | Hz and ordinary Q; raw rows use mHz and Q times 1000. |
| LFO rate | Millihertz: 5000 means 5 Hz. Delay/fade and envelope times are milliseconds. |
| FX master gain | Signed tenths of a dB of gain, unlike operator attenuation. |

Native events are resolved by timestamp, stream declaration order and
input order. Supply the session first, then stream files in declared
order; `p.files()` does this. On a voice at the same time, off then on
releases the old note and starts the new one; on then off can silence it.
A pitch event changes a sounding note without retriggering. On an idle
voice it produces no immediate sound, but can affect later phase with
FREE 2; do not treat idle pitch as universally inert. Any on to an occupied
voice retriggers it, including during release.

Ordinary Python event groups sort off, placement/send, on, then
pitch/filter. A same-stream/time group containing ctl, gen or lane events
preserves call order instead. Call `ctl` before `on` when the onset must
see that value. Controllers persist through notes; they are not impulses.
Do not use direct edits to `p.ev` as an alternative public API.

Note events land on sample frames. Spatial position, moves and sends
take effect at the first spatial block boundary at or after their time;
the native gain ramp has its own block timing. Voice modulation uses its
control grid. A session generator has a separate 64-frame bank clock.
These clocks can share a timestamp without sharing update order.

At a generator boundary: explicit events run first, then the bank ticks,
all generator caches publish, generated offs precede generated ons,
then the frame renders. An explicit on sees the previous cache, initially
zero; a generated on sees the new cache. Do not flatten generated lanes
into ordinary on events and expect identical audio.

## Build a score with `Piece`

Import `compose` as in the quickstart. Its import reads data tables but
does not build or render. Start with
`Piece(name, bpm, beats_per_bar, about_list, fs=48000, space=None,
block=64, near=100, levels=None)`; use simple identifier-like names.
Immediately assign `p.dir` to a fresh external directory.

1. Declare streams with `p.stream(name, role)`.
2. Define patches with `p.patch(name, *recipe(...))` or with `op` arrays.
3. Allocate at most 64 native voices with
   `voices = p.add_voices(patch, count, comment)`.
4. Author events in chronological order for each allocator.
5. Set `p.length_ns`, including releases and desired effect tails.
6. Call `p.write()` and render `p.files()` through the explicit harness.

`write` does not level the music or hash it; the renderer does not
silently choose a musical balance. Start carriers well below 0 dB, measure
the mix, then revise patch levels. `write(levels_db10={patch: raw_level})`
provides explicit carrier-level overrides; for routed patches operator
zero remains the reference used for relative carrier levels. Compensation
and MIDI crossfade paths additionally validate the final written levels.
Writing does not delete obsolete stream files, so use a fresh directory
for a renamed score or stream set and pass `p.files()`, not a broad glob.

Basic event calls are:

```python
p.on('notes', t, voice, 'C4', 24000)
p.on_f('notes', t, voice, 261626, 24000, 'exact mHz')
p.pitch_f('notes', t, voice, 293665, 'bend without retrigger')
p.off('notes', t, voice)
p.ctl('notes', t, voice, 2, 16384)
p.ctl_lane('notes', voice, 2, [(t0, 0), (t1, 32768)])
p.mark('notes', t, 'section A')
```

`Pool(voices, release_ms)` with `p.note(stream, pool, t0, t1, note, vel)`
allocates a voice and returns it; this allocator can steal. The performance
helpers instead share a `RoundRobin`: they skip voices still in their
conservative release window and refuse when all are busy. Their default
release comes from the longest carrier release. Add voices, reduce overlap
or shorten the authored release instead of hiding the refusal.

`p.curve(stream, voice, t0, t1, pitch, vel, step_ns, cents=1.0,
bend=True)` takes a callable returning fractional MIDI from elapsed ns.
It starts a note and emits pitch changes beyond the threshold. It writes
no off; add one. Its default `bend=False` emits retriggering ons.

## Time, rhythm and deterministic variation

The nominal constructor BPM must divide 60,000,000,000; 60, 75, 80, 100,
120, 125 and 150 work. Use 120 nominally and install an exact map for
other tempos. `p.t(bar, beat=1)` is one-based; `p.at_beat(k)` and
performance start/rate arguments use zero-based quarter-note beats.

```python
from fractions import Fraction
p.meter([(1, 4), (3, Fraction(7, 2))])   # 4/4, then 7/8
p.tempo_map([(1, 90), (3, 120)], ramp='linear', bars=4)
```

Set meter before tempo. A linear map interpolates **beat duration**, not
BPM; step maps use `ramp='step'`. Cumulative rational durations are rounded
once at queried boundaries. Install maps before events; changing a map
does not move old events. Legacy `p.tempo(beat_len, beats=4096)` accepts a
callable giving each beat's ns duration and cannot combine with tempo_map.

`p.beats_us('1/8.', at=0)` and `p.beats_mhz('1/8T', at=0)` give a dotted
eighth's duration in microseconds and a triplet eighth's rate in mHz at
that beat. Whole-note fractions, dotted values, uppercase `T` triplets
and strings such as `'8 bars'` are accepted; float note values are not.
These are queries: pass their results into an effect or LFO. They do not
make a held LFO follow later tempo changes; note-value queries with FREE 2
under a tempo map are refused.

The performance helpers are atomic on refused calls, including their
event lists, voice allocation and random state:

| Helper | Inputs and behaviour |
|---|---|
| `p.arp(s, voices, notes, start_beat, beats, rate, ...)` | Modes `up`, `down`, `updown`, `downup`, `played`, `random`, `chord`; `octaves`, `gate`, `vel`, `seed`, `swing`, `patch`, `release_ms`, `scale`. Gate is a fraction of a step; values above one deliberately overlap. Swing 50 is straight. Returns `(on_ns,off_ns,voice,midi)` rows. |
| `p.arp(..., keys=presses, notes=None, latch=False)` | Presses are `(on_beat,off_beat,note,velocity)`. Holds satisfy on <= step < off; simultaneous releases precede presses. With latch, only the immediately preceding held set survives the final release. Partial releases remove notes. `vel='held'` uses press velocities. Random mode draws even on empty steps. |
| `p.steps(s, voices, pattern, start_beat, step_beats, notes, ...)` | `x` hits, `.` rests, `\|` visual separator. Per-step velocity/probability/ratchet/micro/locks; scalars, cycling lists and step dictionaries where specified. Ratchets are 1..4; micro offsets are bounded to half a step. Use a fixed seed for reproducibility. |
| `p.chord(s, voices, key, t, dur, vel, shape, strum_ms=0, direction='up', ...)` | One chord at ns time; shape is 1..8 whole-semitone offsets rising from zero. `chord_shape(['C4','E4','G4'])` produces `(0,4,7)`. Direction is `up` or `down`; total strum span is 0..200 ms, an integer or exact decimal string. All offs remain at t+dur. |

For example, after defining `p`, stream and four suitable voices:

```python
hits = p.arp('notes', voices, ['C4', 'E4', 'G4'],
             start_beat=0, beats=4, rate=Fraction(1, 2),
             mode='updown', gate=0.5, vel=24000, seed=17)
```

Steps add `passes`, `pass0`, `cond`, `fill`, `slide`, `scale` and `nei`.
Conditions include `first`, `not-first`, `A:B` (B from 2 to 8),
`fill`, `not-fill`, `pre`, `not-pre` and `nei`. PRE resets false each
pass and records the preceding conditional result, including conditional
rests. NEI reads an explicitly supplied earlier lane result on the global
step grid; empty or filtered-out steps are false. Neither reads future
lanes. Rest and condition handling retain the specified random draws.

Locks can set `pitch`, `filter=(hz,q)`, `send=(bus,q15)`,
`pos=(x,y[,z])`, and `ctl=(input,q15)` or up to eight controller pairs.
Controller slides emit eight subdivisions between hits. Restoration uses
the helper's release/ownership rules; do not assume general restoration
of an unknown prior value authored in another stream.

`scale_lock(notes, 'major', root=0)` snaps whole MIDI notes downward to
the allowed pitch class. Helpers also accept `scale=('minor', 2)` or a
12-bit mask and root. Mask zero disables locking. Available preset names
are major, minor, harmonic-minor, melodic-minor, dorian, phrygian, lydian,
mixolydian, locrian, major-pentatonic, minor-pentatonic, blues, major-blues,
whole-tone, diminished-hw, diminished-wh, harmonic-major,
phrygian-dominant, lydian-dominant, altered, hungarian-minor,
double-harmonic, neapolitan-minor, neapolitan-major, hirajoshi, in-sen,
suspended-pentatonic, bebop-dominant, bebop-major and chromatic.

`randomize(recipe_name, seed, {delta_name:(lo,hi), ...})` returns recipe
deltas. `randomize_scoped(name, seed, scope='all', amount=None,
preset=None, ratios=None, lock=)` provides bounded `operators`,
`envelopes` or `all` variation. Presets are `mild` (15%) and `aggressive`
(60%); explicit amount is 0..100. Ratios change only when an explicit
allowed ratio set is supplied. Locks name individual prefixed fields or
an operator argument. Pass the returned dictionary into `recipe`.
Algorithm randomization is held; filter/matrix scopes are not implemented.

## Patches, recipes and the synthesis repertoire

`recipe(name, time=1.0, *, mute=, solo=None, templates=,
carson=None, **deltas)` returns four values for `p.patch`. Every call
gets its own patch data. Deltas add to, rather than replace, the recipe's
values. Prefixes are `c_` (operator 0), `m_` (1), `o2_` and `o3_`.
`c_cents` detunes a ratio; `wF_T` changes an existing routed edge's Q8
weight. `time` scales operator attack, decay and release, not LFO,
filter-envelope or other metadata. Normal deltas clamp at field bounds;
ignored or incompatible fields are refused. Do not interpret a successful
clamp as the originally requested numeric value.

`op(ratio=1, level=0, index=0, fb=0, vel=0, a=1, d=100, s=0,
r=100, ks_break=0, ks_slope=0, wave=0, rs=0, fixed=0, phase=0,
free=0)` constructs one raw operator array. Attack/decay/release are ms;
level/sustain are dB attenuation; index/feedback radians; key breakpoint
and fixed frequency Hz; key slope dB/octave; velocity sensitivity 0..256.
Use `p.patch('p', op(level=24), op(index=2), 'my pair')` for a simple
modulator-to-carrier pair. Routing metadata belongs in the optional fourth
patch value; use a routed recipe or the native route grammar below for
more elaborate graphs instead of guessing the metadata dictionary.

Wave codes are 0 sine, 1 half sine, 2 absolute sine, 3 quarter sine,
4 white noise, 5 alternating sine, 6 camel, 7 square, 8 log-saw,
9 pink noise, 10 low-pass noise and 11 high-pass noise. Code 12 is not
implemented. Wave 8 is not an ideal linear saw. Operator FREE 0 key-syncs
phase on an onset from silence; ordinary retriggers preserve operator
phase. FREE 1 holds phase/noise across idle without advancing, and FREE 2
advances across idle; LFO FREE supports 0 and 2, not 1.

| Family | Existing authoring interface and compatibility |
|---|---|
| PM/FM, AM, ring and sync | Explicit two-to-four-operator routes; carrier mask selects outputs. Index/key scaling affect outgoing PM edges, not arbitrary edge kinds. Sync weight is fixed. Feedback is an operator property. |
| Envelopes | `p.eg(patch,op,delay_ms,decay2_ms=0,sustain2_db10=0)`; `p.egloop(patch,op)`; `p.amp(patch,attack_ms,decay_ms,sustain_db10,release_ms)`; `p.peg(patch,l1,t1,l2,t2,l3,t3,l4,t4,mask=None)`. |
| Scaling and feedback variants | `p.ks(patch,op,left_slope_db10)`, `p.kstype(patch,op,target='index',right='-',left='-')`, `p.fbtype(patch,op,kind='classic')`. **`p.ks` is key scaling, not the Karplus–Strong source.** Native source grammar is a separate capability. |
| Filters | `p.filter(patch,'lp'\|'bp'\|'hp'\|'notch',hz,q,stages=1)`; `stages=2` cascades. Runtime `p.cutoff(s,t,v,hz,q)`. `p.fenv(patch,a_ms,d_ms,sustain_permille,r_ms,depth_cents,track_permille,ref_millihz=261626)` requires a filter. |
| LFO | `p.lfo(patch,wave,rate_millihz,delay_ms,fade_ms,pitch_cents,level_db10,index_q10,phase_u16=None,mask_pitch=None,mask_level=None,mask_index=None,free=None)`. `wave` is a numeric code in this Python helper too: 0 sine, 1 triangle, 2 saw up, 3 saw down, 4 square, 5 sample-and-hold, 6 smoothed random. Phase/masks are an all-or-none group; FREE needs that group. One LFO per voice. |
| Shaping | `p.shape(patch,drive_db10,kind,pre=False)` uses `hard`, `cubic`, `fold` or `asym` and optional pre placement. Zero drive still enables the shaper; do not treat it as absent. |
| Phase distortion | `p.pd(patch,op,knee)` for a present sine operator, knee 1..32767. Matrix `pd_kneeN` moves it. Explicit identity remains an enabled header. |
| Vector | `p.vector(patch,x,y)`, coordinates 0..32768; requires an explicit four-operator route with all four carriers. Matrix targets `vector_x`/`vector_y` move the coordinates. |
| Formants | `p.formant(patch,slot,center_millihz,q_milli,level_db10)`; three slots 0..2. Targets `formant_centerN` and `formant_gainN` require that slot. Slots feed the ordinary downstream filter and retain their tails. |
| Coloured noise | Waves 10/11 use `p.noise(patch,op,shift)` with shift 1..15, default 4. This header is invalid on other waves. |
| Wavetable/additive | Session banks via `p.wavetable` and operator bindings via `p.wtuse`; details below. Native banks also accept explicit integer frames. |
| Plucked strings | `p.ksource(patch,op,loss_q15,seed,buffer_samples)` binds the existing Karplus–Strong source. Loss 0..32767, uint32 seed, power-of-two buffer 64..8192; all explicit integers. Each voice gets its own buffer; there is no named KS bank. `p.ks` remains key scaling. |

`mute=(operators...)` removes their carrier bits and zeroes their PM
index/feedback. Muting a source with outgoing non-PM edges is refused,
as is removing all carriers. `solo=operator` retains one existing
carrier. Do not mute a required vector corner. Metadata such as filters,
LFOs and envelopes is copied with recipes; changing an operator does not
automatically make that metadata compatible.

### Wavetable forms and banks

The ready recipes `bl-saw`, `bl-square`, `bl-triangle` and `warm-pad-x`
provide bounded harmonic-bank timbres. The latter uses the instrument's
own FM-X-style forms, not hardware emulation. Recipe `wtuse OP FORM POS`
supports `bl-saw`, `bl-square`, `bl-triangle`, `fmx-all`, `fmx-odd` and
`fmx-res`; POS is Q15 frame position. These forms have pinned harmonic
laws and emit complete bank assets into the record.

For an independent additive bank:

```python
p.wavetable('partials', 1, 256, harmonics=[
    (0, 1, 0, 0),       # frame, harmonic number, attenuation dB10, phase u16
    (0, 3, 120, 0),
])
p.wtuse('sound', 0, 'partials', 0)
```

There are at most eight banks, 1..8 frames each, power-of-two lengths
32..2048, harmonic numbers 1..1024, attenuation 0..1200 and phase
0..65535. Duplicate frame/harmonic pairs are errors. Matching repeated
bank definitions deduplicate; conflicting ones refuse. Bindings require
present sine operators without conflicting PD/KS source bindings.
Bank declarations and bindings are both necessary. An expanded canonical
record carries data and hashes; retaining only `wtuse` is not a portable
patch export.

`recipe(..., carson=(fs, onset_millihz_list))` is an optional ideal-only
finite-domain PM key-scaling heuristic. It requires supported sine pairs
without WT/KS/PD or AM/ring/sync routes. It does **not** prove native
alias-free output: measured gain-table deviations, feedback, aggregate
chains, changing controllers and downstream shaping are outside its
claim. The bandlimited-form names likewise do not guarantee that later
PM, distortion or resampling cannot introduce aliases.

### Complete KS and wavetable examples

These complete small native sources can be supplied as `render.text` or
`check_patch.text`. Use the discovered engine line. They are isolated controls,
not developed songs, and declaration alone establishes no audible contribution.
Assigning a patch to a voice and scheduling its note are explicit steps.

```text
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams golden
patch kspluck 0 65536 180 0 0 0 5 100 0 800
patch kspluck 1 65536 0 0 0 0 5 100 0 300
ksource kspluck 0 32700 7 2048
voice 0 kspluck
0 golden 0 on 110000 32767
900000000 golden 0 off
```

This is the retained `kspluck` golden control without its expected hash.
The helper binding is `p.ksource('kspluck',0,32700,7,2048)`. With the same
patch/voice already defined, `p.on('golden',0,0,'A2',32767)` uses a note name;
`p.on_f('golden',0,0,110000,32767,'A2')` uses integer mHz. Choose one onset.
KS needs a sine operator, no PD/WT/feedback or incoming route edge, and no
outgoing sync edge. Fixed-path carrier 0 also requires modulator index zero.
The helper checks these conditions again before writing; native validation
remains authoritative. Replacing the patch clears its KS binding; supported
keys/profile copies retain it. Asset-dependent patch morphing is refused.

```text
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams notes
patch tone 0 65536 180 0 0 0 5 100 0 300
patch tone 1 65536 0 0 0 0 5 100 0 300
wtbank bank 2 32 12
harmonic bank 0 1 0 0
harmonic bank 1 2 0 0
wtuse tone 0 bank 0
mod tone 0 ctl0 wt_pos0 32768 0
voice 0 tone
0 notes 0 ctl 0 0
0 notes 0 on 220000 32767
500000000 notes 0 ctl 0 32768
1000000000 notes 0 off
```

The corresponding source helpers are
`p.wavetable('bank',2,32,harmonics=((0,1,0,0),(1,2,0,0)))`,
`p.wtuse('tone',0,'bank',0)` and
`p.mod('tone',0,'ctl0','wt_pos0',32768,shift=0)`.
`p.ctl('notes',500_000_000,0,0,32768)` moves the held voice to the second
frame. `ctl0` is a modulation source name; the event keyword is `ctl`, followed
by controller index 0. `ctl0` is never an event keyword. WT and KS bind timbre
before spatial placement; neither changes source coordinates.

### Text authoring without Python score code

A `recipe` statement is authoring syntax, not native record syntax.
Save an input ending in `.tl.in`, then expand to a different output:

```sh
cat > "$WORK/tone.tl.in" <<'TL'
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 800000000
streams notes
recipe sound epiano c_level=6
voice 0 sound
0 notes 0 on 440000 32767
400000000 notes 0 off
TL
python3 -B "$REPO/tools/music/compose.py" --expand \
  "$WORK/tone.tl.in" "$WORK/tone.tl"
"$HARNESS" --play "$WORK/tone.tl" --metrics \
  --wav "$WORK/tone.wav" --merged "$WORK/tone.record.tl"
```

Expansion is text-only and does not auto-build. It preserves raw rows
and expands recipe rows with the same patch metadata and bank checks.
Consequently native-only features can coexist with recipe-created patches
in an external authoring timeline. Keep referenced generator definitions
and asset banks; expansion does not invent them.

## Modulation, macros and state morphs

Each patch has eight matrix slots. Add one with
`p.mod(patch, slot, source, target, amount, shift=3)`; slot is 0..7.
Source values are signed Q15; amount uses the target's units. Shift zero
is immediate, other shifts smooth at the control clock. Zero-amount
rows still require valid prerequisites and occupy their authored slot.

Sources are `lfo`, `peg`, `fenv`, `eg0`..`eg3`, `vel`, `note`,
`ctl0`..`ctl7`, and declared `gen.ID.x`/`gen.ID.y`. LFO needs a nonzero
rate, even if direct depths are zero; PEG needs a nonzero level; FENV
needs a filter and nonzero depth or tracking. EG needs a present operator.
Python helper targets include `pitchN` (cents), `levelN` (dB10 attenuation), `indexN`
(Q10), `cutoff` (cents), `q`, `lfo_rate`, `lfo_pitch`, `lfo_level`,
`lfo_index`, `pd_kneeN`, `vector_x`, `vector_y`, `formant_centerN` and
`formant_gainN`. Level targets require carriers; index targets require
outgoing PM; specialized targets require the corresponding enabled feature.
There is no spatial-position matrix target: write spatial events instead.

`wt_posN` is supported by native timelines and the shared `p.mod`, macro
and recipe-mod helpers, amount -32768..32768 in Q15 frame-position units.
It requires an enabled wavetable on operator N with WAVE=0 and PD disabled;
the bare spelling `wt_pos` is invalid. Recipe expansion preserves raw
non-recipe rows too. The complete example above shows the bank, binding,
controller and sounding voice together.

```python
p.mod('sound', 0, 'ctl2', 'index1', 2048, shift=0)
p.ctl('notes', 0, voice, 2, 0)
p.on('notes', 0, voice, 'A4', 24000)
p.ctl('notes', 150_000_000, voice, 2, 32768)  # Change the held note.
```

Macros share those same eight slots and inputs, rather than adding another
matrix. `p.macro(patch,name,input,targets,bipolar=False,curve=None)`
takes 1..8 `(target,amount[,shift])` rows and reserves the lowest free
slots. `p.macro_set(stream,t,voice,name,knob)` takes a knob 0..127.
Unipolar maps endpoints to 0/32768; bipolar maps 64 to zero and endpoints
to -32768/32256. An optional callable maps integer Q15 to bounded integer
Q15 on the host. The record contains ordinary mod/ctl rows, not Python.
Macro installation also refuses `pitchN` on fixed or muted operators,
`levelN`/`indexN` on muted operators, and `lfo_pitch` when its mask has
no unmuted relative-frequency operator.

`p.template(patch,name)` or `recipe(...,templates=(name,))` installs a
fixed template: HARM uses ctl0 for modulator pitch; DTUN uses bipolar
ctl1 for two opposing modulator pitches; MIX uses ctl2 for vector x or
two-carrier attenuation; Timbre uses ctl3 for index; EXPR uses ctl0/1/4
for wheel/pressure/slide targets. EXPR may add its default 5-Hz LFO only
when absent. Templates require their intended patch topology. They may
conflict with one another: HARM and EXPR both own ctl0. Raw zero routes,
existing macros and compensation reservations count toward collisions.

Snapshots store only explicitly supplied controller state:

```python
p.snapshot('soft', {voice: {2: 0}})
p.snapshot('bright', {voice: {2: 32768}})
p.recall('notes', 0, 'soft')
p.snapshot_morph('notes', 'soft', 'bright', 0, 300_000_000, steps=4)
```

At most eight snapshot names; values are copied and cannot be redefined.
Morphs require identical voice/input keysets, with no inferred missing
values. They emit eight subdivisions per step, including endpoints.
They do not snapshot pitch, patch selection, filters, generators or space.
`p.ctl_mseg(s,v,n,points,anchor=t,loop=None,release_at=None)` expands
host breakpoint envelopes into ctl rows. Points are increasing
`(offset_ns,Q15)` pairs starting at offset zero; exactly one existing
matrix slot must read that input, including zero-amount routes. A loop
uses point-index pair `(i,j)` and needs an explicit finite release time;
release preserves the current interpolated value then traverses the tail.
This is not an engine MSEG oscillator or a new matrix source.

`p.patch_morph(new,a,b,u)` and `p.patch_square(new,a,b,c,d,x,y)` create
static new patches from existing ones, with Q15 coordinates 0..32768.
Route topology and carrier mask must agree; continuous raw fields blend
under the pinned per-header classes while discrete fields select a
corner. Endpoints retain their source fields. Optional headers use
native defaults only for interior results. Wavetable-bound corners,
compensation-owned patches and unknown metadata refuse. These operations
do not morph a currently sounding voice; assign the new patch to a voice.

## Session generators and live lanes

Call `p.gen_clock` (64 frames), then
`p.gen(id,kind,seed,div=1,slew=0,**fields)`. IDs are 0..15 and seeds are
explicit unsigned 32-bit integers. DIV 1..65535 sets the source update
divider; SLEW 0..512 bounds output change each bank tick. No Python clone
of the native recurrence is used. Source, clock, lane and rule duplicates
are refused.

| Kind | Optional fields and defaults | Mutable kind fields |
|---|---|---|
| white, pink | none | none |
| brown | step=16, Q15 | step |
| multi | p=256, k=2, octaves=6, gain=24576 | gain |
| logistic | r=1123085157075517, Q48 | r |
| henon | a=394064967394918, b=84442493013197, Q48 | a, b |
| euclid | k=3, n=8, rot=0, parent=None | k, n, rot |
| thue, cantor | length=256, rot=0 | length, rot |
| fibonacci | depth=12, rot=0 | rot |
| lsystem | rot=0; axiom required | rot |

The emitted `--gen-params` table supplies exact scalar ranges and types
from the same descriptor file Python consumes. Coupled constraints such
as k<=n and rotation/expanded-word length are additionally checked by the
core. Euclid parents must be existing smaller source IDs. L-systems use
`p.gen_axiom(id,depth,word)` and `p.gen_rule(id,symbol,word)`: depth
0..16, words of 1..32 uppercase A–H symbols, `'-'` for rule deletion.
Unspecified rules retain their symbol. At most two Fibonacci/L-system
word sources coexist.

Matrix references use `gen.ID.x`; only Hénon offers `.y`. Each matrix
slot binds its own cache, so multiple slots can read one source. The
bank is session-owned even when the mod row comes from a recipe.

```python
p.gen_clock(64)
p.gen(0, 'euclid', 17, k=3, n=8)
p.gen(1, 'brown', 23, step=128, slew=4)
p.mod('sound', 0, 'gen.1.x', 'index1', 1024)
p.gen_lane(0, voice, gate=0, pitch=1,
           step_ticks=188, base_cents=6000, span_cents=700,
           velocity=24000, gate_ticks=90, scale_mask=2741)
```

This example replaces manual on/off/pitch events for that voice. Lane
IDs 0..7 each reserve one voice. Gate/pitch/accent source arguments are
optional IDs reading X: absent gate always hits, absent pitch adds zero,
absent accent gives full accent. Step ticks are 1..65535; gate ticks
1..step ticks; span 0..9600 cents; velocity 1..32767. A4 is MIDI cents
6900. Raw and quantized pitch extrema must remain -4800..14400 cents,
including downward scale snapping. Filter, ctl and placement events on a
reserved voice remain possible; manual on/off/pitch do not.

`p.gen_set(s,t,id,'reset',seed)` or `'enabled'`, `'slew'` and the mutable
fields above schedules a change; `p.lane_enable(s,t,id,0|1)` enables or
disables a lane. Source changes apply on its next due tick, lane changes
on the next bank tick. Reset preserves the published smoother value and
divider grid. For exact emitted event inspection, the native command is
`harness --gen-events SESSION STREAM...`; it reports a versioned manifest,
not a flattened replacement score. Its timestamps must round-trip to the
same native frame.

Through MCP, call `gen_events(text=complete_timeline, receipt_dir=explicit_dir)`
or supply an ordered `files` list. CLI uses `gen-events FILE...` or
`gen-events --text TEXT`, with the existing optional receipt arguments.
This runs native metadata discovery and the actual generated-event manifest;
it creates no PCM, WAV or accepted take and does not expand recipe shorthand.
It normally attempts three children, reported separately as discovery and
manifest costs. A zero-row schedule can still have active modulation sources.
Use the emitted pitch/timing rows to inspect behavior; they do not establish
audibility or replace the original live generator timeline.

Default `detail="page"` returns a bounded UTF8 page. Follow `next_offset` with
`expected_manifest_sha256` until `eof`, then verify the assembled manifest
SHA256. Default length is4096bytes, maximum8192; rows can split across pages.
Every new `gen_events` page call repeats native work. For long schedules,
prefer the optional receipt's `text_artifact` locator with
`kind:"generator_manifest"`: remove `size`, add the explicit receipt directory,
and retrieve the exact manifest through `artifact_read`. This locator is
available only for a complete captured successful native manifest; a later
delivery-guard refusal can still retain it. The page/cap rules above apply.
If the convenience locator is omitted, retrieve `full_result_sha256` through
`artifact_read(kind="receipt", encoding="utf8")`, assemble and verify the
complete JSON, then extract the manifest process's `stdout.text` and verify
its manifest SHA. Artifact retrieval requires no native executable or new
generator work. It still reads/verifies the retained file.

For an activity overview, use `gen_events(text=complete_timeline,
detail="summary",start_ns=0,end_ns=4000000000,receipt_dir=explicit_dir)` or CLI
`gen-events FILE --detail summary --start-ns 0 --end-ns 4000000000 --json`.
Bounds select the half-open interval [start,end) in printed native nanoseconds;
omit end for the remaining schedule, or use equal bounds for an empty selection.
Summary requires default offset0/length4096; page mode requires no time bounds.

The summary contains per-observed-lane ON/OFF counts, distinct ON pitches, pitch
and velocity ranges, and first/last ON with original event ordinal/frame/time.
Every manifest row is validated before selection. OFF zeros do not affect ON
ranges. Lanes observed outside the window remain listed with zero selected
counts/null ON statistics. A wholly empty manifest lists no lanes; neither case
proves inactivity or inaudibility. Authored TL events are outside this summary.
The whole schedule still executes with the usual three native children and no
PCM/WAV. Page and summary preserve the same full computational evidence format;
optional receipts avoid native work when recovering it later.

Inputs are limited to17 ordered regular-file occurrences and4MiB total bytes;
each occurrence is snapshotted separately, including repeated files and
symlinks to regular files. Each child capture allows16MiB stdout/64KiB stderr;
the selected operation has one30-second child deadline plus bounded cleanup.
Filesystem time is outside that deadline. Partial output retains captured
identity and actual process status, with complete manifest identity absent.
Native/host failures precede requested page guards. The15360-byte public JSON
budget differs from the larger SDK envelope; optional receipt metadata is
reserved within the public budget. The full receipt preserves exact captures
and omitted diagnostics. Distinguish manifest, input-set and full-result hashes.

## Spatial performance and effects

Construct with `space='stereo'` for spatial events. `block` is 32, 64 or
128; `near` clamps the ordinary-stereo pan denominator in mm, not the
one-metre distance-gain reference. `p.pos(s,t,v,x,y,z=0)`
places a voice. `p.move(s,t,v,duration_ns,x,y,z=0)` moves linearly from
its current native geometry; a subsequent pos/move replaces the move.
`p.path(s,v,[(t,x,y,z),...])` makes a joined polyline.

`p.autopan(s,v,t0,t1,rate_mhz,width_mm,shape='tri',depth_mm=1000)`
uses two triangle or eight sampled-sine segments per cycle. Without prior
geometry it starts at `(-width,depth,0)`; otherwise it starts from the
actual position. `p.pos_mod(s,v,t0,t1,source,rate_mhz,depth_mm,axis='x')`
moves around the current anchor using `'tri'`, `'sine'` or breakpoint
`(offset_ns,Q15)` pairs. Breakpoints cover the whole span, start `(0,0)`
and require `rate_mhz=None`. These are finite event sequences, not audio
rate position modulation. Native block quantization applies. The composer's
3-degrees-per-second speed lint is advisory; the engine has no such limit.

`p.unison(s,voices,t,midi,n,cents,width_mm,vel=24000,compensate=False)`
starts 1..8 distinct voices of one patch, lowest detune leftmost around
the note's keys position. It returns the used voice tuple; add offs and
manage reuse yourself. With compensation, each copy gets 1/N amplitude
through validated carrier attenuation, rounded from `200*log10(N)` raw
dB10. It reserves matrix slots and an input and inserts zero resets for
later ordinary ons. Raw ctl writes, snapshot ownership conflicts, changed
voice-to-patch assignments and unsupported nonlinear carrier paths refuse.
Do not mutate compensated note tuples or their owned patch/control maps.
This safety rule is stricter than uncompensated unison.

`keys_pos(midi,mirror=False,profile=None)` gives a pitch's position on a
one-metre arc: default `lin30`, middle C ahead, low left/high right.
Profiles are `lin5`..`lin90` and `tanh5`..`tanh90` in five-degree steps.
The suffix is a half-width: `lin30` spans −30° to +30° (60° total),
while `lin60` spans −60° to +60°. The keyboard place is `(midi-60)/32`,
clamped to −1..1: E1 (28) and below at the left edge, C4 (60) ahead,
G♯6 (92) and above at the right edge. Mirror reverses the two sides.
`keys_version(maker,mirror=False)` and `profile_version(maker,home)` are
existing registered-piece transformations; their default output and level
lookups follow repository conventions. For external scores, assigning
positions directly is simpler. Generated-lane keys transformations require
the native manifest and preserve live generator headers/runtime events.

The browser's Listening profile computes linear widths ±30°, ±45°, ±60°,
±75° and ±90° locally through the native host, including optional mirror.
It replaces authored spatial placement/motion with Keys stereo; it preserves
the note schedule and effects and adjusts carrier levels together toward
−1.5 dBFS ±0.5 dB. It is ordinary stereo, not binaural decoding. The loaded
source stays available through As loaded; a computed version downloads as
one complete canonical TL with its own hash. Saved library versions remain
unchanged. See [the browser guide](../web/browser-guide.txt) for the profile
API, source identity and verification limits.

`p.field('amb1')` emits four AmbiX channels; `'amb3'` emits sixteen.
`p.field('binaural',order=3,pinna='off',yaw=0)` renders two ears with
matching built-in tables. Pinna choices are `off`, `pbnh`, `rd`, or five
integer depths 0..1000. `p.yaw(s,t,mdeg)` turns the listener for field
routes. `p.field('stereo',panlaw=W)` selects W 1..32768. Do not confuse
ordinary stereo, AmbiX and binaural; they have different channel meanings.

Drawing maps page coordinates `(u,v)` in -1..1 to pan and pitch, emitting
ordinary pitch/spatial events. `line(a,b)`, `arc(center,radius,a0,a1)`
(angles in degrees), quadratic/cubic `curve(*points)`, `dot(point)` and
`poly(*points)` create primitives; poly returns a list of lines.
`p.stroke(s,v,t,primitives,duration_ns,pen=None)` draws connected
primitives as one note and supplies its off. `p.draw(s,v,t,strokes,
dur_ns=None,pen=None)` draws lists of primitive lists with pen-up gaps.
`p.glyph(s,v,t,char,dur_ns=None,pen=None,box=(-1,-1,1,1))` draws an
uppercase letter, digit or full stop. Each returns its end time.
`Pen` defaults to a lin45 one-metre arc, C4..C6, two seconds per glyph,
125-ms gaps/dots and 10-ms pitch sampling; keyword settings can change
profile, low/high notes, velocity, timing, scale rows and mirror.

Here is an independent drawing followed by a small physical circle. It
uses the same `$REPO`, `$WORK` and `$HARNESS` environment as the quickstart:

```sh
python3 -B - <<'PY'
import os, pathlib, subprocess, sys
out = pathlib.Path(os.environ['WORK'])
sys.path.insert(0, str(pathlib.Path(os.environ['REPO']) / 'tools' / 'music'))
import compose as c
p = c.Piece('drawing', 120, 4, ['A rising stroke, then a circle'], space='stereo')
p.dir = str(out / 'drawing')
p.stream('pen', 'one spatial pen')
p.patch('ink', *c.pen_sound())
v = p.add_voices('ink', 1, 'pen')[0]
p.stroke('pen', v, 0, [c.line((-0.2, -0.2), (0.2, 0.2))], 1_000_000_000)
p.path('pen', v, c.circle_points(1_200_000_000, 2_000_000_000,
                                0, 1000, 200, segments=32))
p.on('pen', 1_200_000_000, v, 'A4', 20000)
p.off('pen', 3_200_000_000, v)
p.length_ns = 3_600_000_000
p.write()
h = os.environ['HARNESS']
record = str(out / 'drawing.record.tl')
subprocess.run([h, '--play', *p.files(), '--metrics', '--merged', record,
                '--wav', str(out / 'drawing.wav')], check=True)
subprocess.run([h, '--play', record, '--chunk', 'random'], check=True)
PY
```

`circle_points(t0,period_ns,cx,cy,r,laps=1,segments=32,
phase_deg=0,clockwise=True,z=0)` returns native `(t,x,y,z)` path points.
Its coordinates/radius are mm; its angle zero is ahead. Page drawing's
height is pitch, whereas a physical circle's z is height in space.

`p.fx(...)` accepts native-valued arguments `dcblock`, `chorus`, `delay`,
`reverb`, `limiter`, `softclip`, `gain`, `dither`, `pingpong`, `compressor`,
`flanger`, `crush` and `decodable`. Effects omitted on a later call retain previous
settings; omission is not deletion. Enable an effect before sending with
`p.send(s,t,v,bus,level)`. Send buses are chorus, delay, reverb, flanger
and duck; duck requires the compressor and drives its detector rather
than becoming a new audible return. Ping-pong uses the delay bus and requires
ordinary stereo or an approved field/binaural route; PANLAW remains refused.

For example `p.fx(delay=(250000,8192,8000))` declares a 250-ms delay,
quarter-scale feedback and 8-kHz low-pass; call
`p.send('notes',0,voice,'delay',8192)` before the onset. Ensure the
record includes the desired tail. FX parameters have native ranges and
compatibility checks; the native grammar below lists the complete tuples.
`decodable=True` selects a restrictive accepted profile. It does not prove
invertibility, no clipping or successful message decoding. The alternative
hall reverb is held after a failed measured decay-time acceptance; do not
substitute it for the implemented reverb.

`p.fx(crush=True)` enables the per-voice crusher insert. Then
`p.crush(s,t,v,bits,div)` sets integer bits1..24 and rate division1..64.
For example, `p.crush('notes',0,voice,8,7)` selects an8-bit setting held for
seven frames. In a structured score, the ordinary event is
`{"kind":"crush","at":0,"stream":"notes","voice":0,"bits":8,"rate_div":7}`
within the existing event list; include `crush` in the session
declarations. This works in score versions1,2 and3. It needs no send bus.
The initial24/1 pair bypasses exactly. Other pairs mask the raw Q2.30 sample
(flooring negative values) and hold it before dry positioning and all sends.
Changes apply at the first FX block boundary at or after the event; the last
pending pair wins. A changed pair resets the hold countdown; identical pairs,
note-offs and retriggers do not. A held sample may outlast engine silence by
up to div−1 frames. The `decodable` profile refuses the crusher header even
at 24/1. Mono, ordinary stereo and field/binaural routes admit the insert
before spatial encoding and sends . PANLAW with FX remains refused;
raw amb1/amb3 still refuse nonlinear masters and dither.

## MIDI, imports and text exports

MIDI import is documented in [MIDI import](DECODING.md#midi-import), which
ships with the source checkout.

DX7 import is an approximate projection into supported operators and
envelopes, with explicit dropped/approximated information. It is not a
lossless DX7 emulator. DX7 export is held because discarded source words
cannot be recovered uniquely. Patch text export must retain asset banks
and generator context or explicitly refuse unsupported portability; a
patch's operator lines alone are often incomplete.

The import command `python3 -B "$REPO/tools/music/dx7_import.py"
/path/to/bank.syx --voice 1 --ops 4 --out "$WORK/imported-recipes.txt"`
writes recipe blocks without rendering. It accepts a bank or individual
voice, with an optional 1-based bank voice selection. Its output is a
recipe-definition file, not a native timeline; `--into` appends to an
existing recipe file and should not target the repository during an
external experiment. There is currently no `compose --recipe-file` CLI
option to select an independent recipe library. For this guide's simplest
workflow, keep using bundled recipes or direct `op`/native patch headers.

## Classified musical examples

The `examples/` directory contains complete canonical records, including
[Quench](../examples/quench-standard.tl), [Rivet Line](../examples/rivet-line-standard.tl) and
[Wind Rose](../examples/wind-rose-standard.tl). The separate `chimepond/tunes` repository
contains the full song library and its descriptive metadata. The library is
optional; rendering an individual TL does not depend on it.

Canonical TL preserves resolved synthesis instructions, but cannot recover
original named phrases or JSON authoring intent. Keep the editable score
alongside a record when you need to revise it.

## Inspect, revise and hand off

Use `analyze.py overview FILE --json OUT` for short-window measurements;
`loudness` for level summaries; `grid` for tempo/swing/step estimates;
`harmony` for coarse harmony/bass summaries; `spectro FILE OUT.png` for
plots; `steps` for staircase experiments. Use explicit external output
paths for JSON, images and gain-adjusted copies. Mono-only or undefined
measurements may be JSON null. Short/quiet/noisy examples do not support
the same musical inferences as a long tonal mix.

`fm.py compare` and `suggest` support measured revision. Compare two renders with the same
analysis settings and a stated hypothesis: e.g. lower RMS after attenuation,
stronger high-frequency energy after increased PM index, changed stereo
balance after placement. A byte change alone proves neither direction nor
musical value. Keep the unchanged control render beside the revision.

Native parse errors report file/line and the rejected condition, with
nonzero exit. Authoring may raise ValueError, ComposeError, assertions or
an unknown-recipe error before rendering. Save stdout/stderr and argv.
An accepted raw control can still be inert: carrier index on a simple
pair, a zero send or a staged patch edit that never reaches a new note.
Matrix rows without prerequisites and sends to an
undeclared effect are refused; they are not accepted inert controls. Check
the actual canonical rows, route and event timing before changing values
again. A correct renderer can also produce clipped music; reduce levels
or density and rerender instead of replacing its hash with an unexplained
value.

Keep the editable score, seeds and canonical TL with its PCM hash. Verify
replay and inspect clipping and measurements relevant to the change. Temporary
WAVs can be regenerated. Explain intentional changes to an established piece;
do not silently replace its expected hash.

## Current interface boundaries

Python authoring and text timelines feed the native parser through
`tools/fm.py`; MCP calls the same functions. The native parameter table
supplies discovery and units. The macOS app auditions records and exposes
patch controls. Its live patch lifecycle differs from offline rendering;
use the canonical record for reproducibility.

Whole-mix and dry-voice measurements are available. Per-voice spatial images,
continuous streaming text input, physical acoustic communication and broad
transcription of arbitrary music are outside the supported workflow.
The optional transcription commands are approximate and restricted; consult
their option discovery before using them.

## Version and maintenance

Keep this guide and runnable examples aligned with syntax, defaults, units
and side effects in the implementation. The native validator is authoritative.
The source lives in [the composer](../tools/music/compose.py),
[recipe data](../data/recipes.txt), [musical tables](../data/tables.txt),
[MIDI converter](../tools/music/midi2tl.py), [analysis](../tools/music/analyze.py)
and [native reader](../src/host/harness.c).

## Native instrument and timeline reference

This section describes the implemented timeline reader, not proposed engine families. Text examples below are fragments to combine with the enclosing session header and voice/events shown here. Preserve a generated record with its engine identity and replay hash after validating a composition.

The approved composition build is `SINE_BITS=12 EXP_BITS=10 PHASE64=1 ENV64=1`, with `FM_FB_AVG=1` and `FM_CTL=64`. These are the Makefile defaults. Work at 48000 or 44100 Hz unless deliberately testing another supported rate. The record's `engine` line must exactly match the executable, including its table hash; obtain it from that build's emitted record or the composer, never invent it. Source: [Makefile](../Makefile), [reader and CLI](../src/host/harness.c).

### Units and session skeleton

All numbers are decimal integers except the explicitly named 16-character lowercase hexadecimal hashes. Signed fields accept an optional minus, not a leading plus. `#` starts a comment. Names are 1–31 ASCII letters, digits, `_`, `-` or `.`. There is no quoted-string syntax.

Native TL schedules events in nanoseconds; it contains no authored BPM, meter,
bar or beat map. Musical-time score clocks compile those choices into event
times. Keep the editable score when revising in beats or bars; a TL alone does
not uniquely recover the original tempo/meter. Optional catalog tempo labels
are descriptive metadata, not native timing declarations.

| Unit | Meaning |
|---|---|
| `MILLIHZ` | 1000 = 1 Hz; A4 = 440000 |
| `DB10` in level/sustain/threshold | Positive attenuation: 60 = 6 dB below the reference; 0 is loudest. Shape drive, compressor makeup and signed master gain are exceptions explicitly identified below. |
| `RATIO_Q16` | 65536 = 1:1; 131072 = 2:1 |
| index / feedback `Q10` | 1024 = 1 radian of phase modulation |
| send / vector / table position / modulation `Q15` | 32768 = 1; signed controls span −32768..32768. Note velocity's effective maximum is 32767. Spatial coordinates instead use mm. |
| `PHASE_U16` | 65536 units per turn; 16384 is a quarter turn |
| `Q_MILLI` | 1000 = resonance Q of 1 |
| `PERMILLE` | 1000 = 1 |
| `NS`, `MS`, `US` | Nanoseconds, milliseconds, microseconds; do not exchange them. |

A complete one-file session has this shape (`ENGINE_ID_FROM_CURRENT_BUILD` is a placeholder, not accepted text):

```text
timeline draft1
engine ENGINE_ID_FROM_CURRENT_BUILD
fs 48000
length_ns 2000000000
streams lead
patch tone 0 65536 180 0 0 256 5 300 120 300
patch tone 1 131072 0 2048 0 256 5 300 180 300
voice 0 tone
0 lead 0 on 440000 32767
1200000000 lead 0 off
```

Without `route`, operator 0 is the carrier and operator 1 modulates it. The example's carrier attenuation is 18 dB; it leaves useful mixing headroom. Operator 1's level and operator 0's index/feedback are unused on this fixed path. `length_ns` includes the desired release/effect tail; the renderer cuts at that duration. `hash HEX` is optional for a new composition and is a replay assertion when present.

Headers may be in `session.tl` with events in stream files, or combined in one file. Read all files as one pool: headers are resolved before events. Limits are 17 input files, 16 stream names, 32 patches, 64 voices, 24 tokens per line, and a 1024-byte input-line buffer. Voice indices must be contiguous from 0; every referenced patch/operator/bank must exist. Duplicate singleton headers, duplicate operator/slot definitions, unknown keywords, and unsupported combinations are errors.

`fs` accepts 1..4294967295, subject to the feature-specific limits below. The duration must fit checked unsigned multiplication, produce 1..4294967295 frames, and obey tighter channel-count limits. An ordinary stereo render is limited to 2147483647 frames; field routes require `frames × channels <= 4294967295`.

Events sort by **nanosecond timestamp, declared stream order, then input file/line order**. Conversion to a sample uses `floor(T_NS × fs / 1e9)`; every event must precede the final frame. At equal timestamps, author the intended order explicitly: an off before its replacement on, and a control latch before an on when the new note must capture that value. Do not indiscriminately reorder `ctl` events. Stream names do not reserve voices.

### Operator patches, waves and routing

```text
patch NAME OP RATIO_Q16 LEVEL_DB10 INDEX_Q10 FEEDBACK_Q10 VEL_SENS_Q8 ATTACK_MS DECAY_MS SUSTAIN_DB10 RELEASE_MS [KS_BREAK_MILLIHZ KS_SLOPE_DB10 [WAVE [RATE_SCALE [FIXED_MILLIHZ [PHASE_U16 [FREE]]]]]]
route PATCH OPS CARRIERS [FROM>TO:WEIGHT[:pm|am|ring|sync] ...]
noise PATCH OP SHIFT
```

The nine basic values are required. Extras are positional: write both key-scaling fields before `WAVE`, and fill earlier optional positions with zero to reach a later field. There are 9 or 11–16 values after `OP`, never 10. Omitted extras are zero.

| Field | Accepted text / effective behavior |
|---|---|
| OP | 0..3; fixed path requires 0 and 1, routed path requires every 0..OPS−1 exactly once |
| RATIO_Q16, KS_BREAK_MILLIHZ, FIXED_MILLIHZ | 0..4294967295 |
| LEVEL_DB10, SUSTAIN_DB10 | Text 0..65535; effective attenuation clamps at 1200 (120 dB) |
| INDEX_Q10, FEEDBACK_Q10 | 0..65535 |
| VEL_SENS_Q8 | Text 0..65535; effective 0..256, with 0 ignoring velocity and 256 fully tracking |
| ATTACK_MS, DECAY_MS, RELEASE_MS | 0..65535; rounded sample counts have at least one sample, so zero is not a zero-sample operator segment |
| KS_SLOPE_DB10 | Text 0..65535; effective attenuation is bounded at 1200. Right-side dB per octave above the break; zero disables it. |
| WAVE | 0 sine; 1 half-sine; 2 absolute-sine; 3 quarter-sine; 4 white noise; 5 alternating sine; 6 camel; 7 square; 8 eight-octave log-saw; 9 pink noise; 10 low-pass noise; 11 high-pass noise |
| RATE_SCALE | 0..7. Segment durations shorten with the note's pitch; 0 disables scaling. Pitch events do not recalculate these captured durations. |
| FIXED_MILLIHZ | Zero follows the note; nonzero is the frequency **before the ratio**. Pitch events, PEG/LFO pitch and matrix pitch do not bend a fixed operator; key/rate scaling still use the note. |
| PHASE_U16 | 0..65535 at initialization / key-synced start |
| FREE | 0 key sync; 1 preserve phase/noise but freeze while idle; 2 continue through rendered idle time. Retriggers preserve operator phase in all three modes. |
| noise SHIFT | 1..15, default 4; only legal on waves 10/11. One-pole coefficient is `2^-SHIFT`, not a frequency in Hz. |

Attack is linear in amplitude; decay/release are linear in attenuation. Release time is the peak-to−96 dB slope, so releasing from a quieter level finishes sooner. A retrigger in an ongoing attack retains its attack slope; do not assume every on resets the waveform or every envelope to zero. Ratio/frequency paths ultimately apply their native frequency clamps. Noise waveforms ignore incoming PM and self-feedback, but their outputs can still modulate other operators. The non-sine waves, hard sync and nonlinear shaping are not a blanket alias-free oscillator promise.

Noise waves 4/9/10/11 draw new random values when their natural operator phase
crosses a half-turn, not at every sample. Note frequency and ratio therefore
change the noise clock; FIXED_MILLIHZ holds its base frequency across notes.
Incoming PM/self-feedback does not clock it. `noise PATCH OP SHIFT` only shapes
waves 10/11; it neither selects a waveform nor changes white/pink noise.

`LEVEL_DB10` is source attenuation: nominal linear gain is
`10^(-min(LEVEL_DB10,1200)/200)`, implemented with fixed-point tables. It does
not specify final dBFS or LUFS. The output depends on oscillator shape and
modulation, envelope, velocity, carrier summing, voice processing, placement,
sends/returns and master processing. Voices sum without automatic normalization.
For one dry centered ordinary-stereo sine, placement adds about −3.01 dB per
channel and sine RMS is another −3.01 dB below sample peak. Lower frequencies,
brief envelopes and pauses change measured loudness independently of LEVEL.

Velocity multiplies both output level and modulation index, with approximate
gain `1 - min(VEL_SENS_Q8,256)/256 * (1 - min(velocity,32767)/32767)`.
Integer rounding applies. Half velocity is about −6.02 dB for sensitivity 256,
or −3.25 dB for 160; the latter's zero-velocity gain is 0.375 (−8.52 dB).
These describe that multiplier, not a guarantee about whole-patch loudness
when modulation also changes the spectrum.

A reproducible level calibration uses fs 48000, two seconds, `space stereo`,
`block 64`, `near 100`, no FX or other voices, and these declarations/events
with the selected renderer's engine header and `streams p`:

```text
patch s 0 65536 100 0 0 0 1 1 0 100
patch s 1 65536 0 0 0 0 1 1 0 100
voice 0 s
0 p 0 pos 0 1000 0
0 p 0 on 1000000 32767
```

For the current engine, the 0.1..1.9-second interval measures approximately
−13.01 dBFS sample peak and −16.02 dBFS RMS per channel, and −13.00 integrated
LUFS over the stereo pair with the supplied analysis meter. Moving to
`(1000,0,0)` gives silence left and about −10.00 dBFS peak right; moving to
`(0,2000,0)` gives about −19.03 dBFS peak per channel. This calibration is
not a loudness target for compositions; measure the actual mix and time span.

`route` permits 2..4 operators and a nonzero carrier bitmask 1..`(1<<OPS)-1` (bit 0 is operator 0). Zero edges is valid additive mixing. Each ordered pair occurs once; self-edges are refused because `FEEDBACK_Q10` controls self-feedback. Edge weights are Q8.8: 256 = unity. PM allows 1..1024; AM/ring 1..256; sync requires exactly 256. Omitted kind means PM.

Operators evaluate from the highest index down. A higher-index source affects a lower target this sample; a lower-index back edge uses delayed history (sync uses the delayed natural-wrap flag). PM uses the source's index; AM/ring do not. Source carrier level affects its direct output, not its outgoing modulation. Hard sync resets the target phase on the source's natural wrap and adds neither PM nor gain. Carrier summation has finite saturation headroom; a quieter final int16 mix cannot undo an earlier carrier-sum saturation.

For example, four additive corners need `route pad 4 15` and four operator rows. A two-operator amplitude-modulated carrier uses `route bell 2 1 1>0:192:am`; change `am` to `ring` for bipolar multiplication, or use `1>0:256:sync` for hard sync. Raising an operator that used to be a carrier into a PM source requires a nonzero `INDEX_Q10`.

Source: [operator and route contracts](../src/host/fm_a1.h), `tl_header_route`, `tl_header_noise`, `tl_header` in [harness](../src/host/harness.c).

### Envelopes, key scaling and feedback variants

```text
eg PATCH OP DELAY_MS DECAY2_MS SUSTAIN2_DB10
egloop PATCH OP
ks PATCH OP LO_SLOPE_DB10
kstype PATCH OP index|level|both -|+ -|+
fbtype PATCH OP classic|pre|square|presquare
amp PATCH ATTACK_MS DECAY_MS SUSTAIN_DB10 RELEASE_MS
peg PATCH L1 T1 L2 T2 L3 T3 L4 T4 [MASK]
fenv PATCH ATTACK_MS DECAY_MS SUSTAIN_PERMILLE RELEASE_MS DEPTH_CENTS TRACK_PERMILLE [REF_MILLIHZ]
```

- `eg`: delay and second-decay times 0..60000 ms, sustain2 0..1200 dB10. An active second decay must end at attenuation at least the first sustain's effective attenuation. Delay emits exact zero while oscillator/control clocks still advance. `egloop` repeats attack/decay/second-decay while held; it does not repeat the initial delay and stops looping after note-off.
- `ks`: left-side slope 0..1200 dB10/octave below the patch's break. `kstype` specifies target then **right sign, left sign**; `-` attenuates and `+` boosts. Default `index - -` normalizes away. These are linear-in-octave laws, not additional exponential-curve options.
- `fbtype`: `classic` uses the ordinary post-envelope feedback history; `pre` uses pre-envelope history; `square` squares the ordinary feedback source; `presquare` combines both. Default `classic` normalizes away.
- `amp`: an optional final voice envelope, separate from the operators. All four text fields accept 0..65535; sustain clamps at 1200. Omission bypasses it; an all-zero row still enables it. **Enabled non-IDLE amps keep the voice active through release .** Keep rendering while active even when carriers are silent. Retrigger retains the amp's current gain; this is not a fresh-voice reset or an audibility predicate. Existing end thresholds are unchanged.
- `peg`: levels −4800..4800 cents; each time 0..60000 ms. From silence it starts at **L4**, goes to L1, L2, L3, holds L3, and after off returns to L4. Times round to control ticks; zero can jump. Explicit mask is 1..15 and may only name present operators; omitted mask covers all. All-zero levels disable the envelope. Example downward drum sweep: `peg kick 0 45 0 0 0 0 2400 0 1` starts two octaves high and falls over 45 ms.
- `fenv`: times 0..60000 ms, sustain 0..1000 permille, depth −9600..9600 cents, key track 0..2000 permille, optional reference 1..4294967295 mHz (default 261626). Requires `filter`; depth and track both zero make it inactive. Tracking captures the note at on, not each subsequent pitch event.

A slow operator repeat can be authored with basic A/D/S/R = `100 250 180 200`, `eg pad 0 0 500 360`, and `egloop pad 0`. Add `ks pad 0 60` plus `kstype pad 0 both - +` only when that left-side boost and both-target scaling are intended.

Source: `tl_header_eg`, `tl_header_egloop`, `tl_header_ks`, `tl_header_kstype`, `tl_header_fbtype`, `tl_header_amp`, `tl_header_peg`, `tl_header_fenv` in [harness](../src/host/harness.c); [engine public contracts](../src/host/fm_a1.h).

### Filter, shaper, PD, formants, vector, wavetable and plucked strings

| Statement | Units, constraints and default |
|---|---|
| `filter PATCH lp\|bp\|hp\|notch CUTOFF_MILLIHZ Q_MILLI [x2]` | Text cutoff 0..4294967295, Q 0..65535; effective cutoff `fs/2400`..`0.45*fs` Hz and Q 500..16000. Omit row to bypass. `x2` cascades two identical stages; absent is one. Note-on restores patch cutoff/Q; runtime filter events last until the next on. |
| `shape PATCH DRIVE_DB10 hard\|cubic\|fold\|asym [pre]` | Positive drive 0..240 dB10. Zero drive still applies the selected curve; omission alone bypasses. Default after the formant/filter path; `pre` before it. |
| `pd PATCH OP KNEE_Q15` | 1..32767; 16384 is exact identity. Requires sine WAVE 0, and excludes WT/KS on that operator. |
| `formant PATCH SLOT CENTER_MILLIHZ Q_MILLI LEVEL_DB10` | SLOT 0..2, center 1..4294967295 with native cutoff clamp, Q 500..16000, attenuation 0..1200. Up to three parallel BP formants before the final voice filter. |
| `vector PATCH X_Q15 Y_Q15` | Each 0..32768. Requires `route PATCH 4 15`; bilinear blend of four post-amplitude/output corners, not a fifth oscillator. X blends corners 0/1 and 2/3, then Y blends those rows. |
| `ksource PATCH OP LOSS_Q15 SEED BUFFER_SAMPLES` | Loss 0..32767, seed 0..4294967295, power-of-two buffer 64..8192. Requires sine WAVE 0 and no PD, WT, feedback or incoming route edges. Fixed-path carrier KS also requires operator 1 INDEX=0. A KS source cannot drive sync. |

Karplus–Strong source memory belongs to each voice/operator and is bound by the renderer; larger buffers permit lower pitches. Its source is not an arbitrary PCM sample player. For a pluck, use the base two-op tone with modulator index zero, then `ksource tone 0 32600 7 2048`; the operator's envelope still shapes it.

`LOSS_Q15/32768` is retained loop gain, so a larger value generally extends
the pluck. Every note-on refills the bound buffer with xorshift noise. A start
from silence seeds the PRNG using the patch seed xor the operator salt;
retriggering a sounding voice continues its PRNG state and refills the buffer.
`pitch` retunes its delay without refilling. FREE does not preserve a KS buffer
across note-ons. Preserve the preceding performance when comparing retriggers.

A vowel-like patch can add:

```text
formant tone 0 700000 3000 60
formant tone 1 1200000 5000 120
formant tone 2 2500000 6000 180
filter tone lp 6000000 707 x2
shape tone 60 asym pre
mod tone 0 ctl0 formant_center1 500000 3
```

The control moves the second formant by up to ±500 Hz. Alternatively, on a sine patch without WT/KS, `pd tone 0 8192` plus `mod tone 0 ctl0 pd_knee0 12000 3` creates a moving PD timbre. These are alternative fragments if their mod slot numbers collide.

Wavetables and additive banks use:

```text
wtbank BANK FRAMES LENGTH 12
harmonic BANK FRAME NUMBER LEVEL_DB10 PHASE_U16
wtdata BANK BAND FRAME START SAMPLE [SAMPLE ...]
wthash BANK HEX
wtuse PATCH OP BANK POSITION_Q15
```

At most 8 banks; FRAMES 1..8; LENGTH a power of two 32..2048; exactly 12 mip bands. A bank uses **either** harmonic rows or complete raw data, never both. Define `wtbank` before its data/hash rows. `wtuse` can precede the bank but must resolve by validation, on sine WAVE 0 without PD/KS. Position is 0..32768 over all frames.

Harmonic NUMBER is 1..1024, level is attenuation 0..1200 dB10, phase 0..65535; each `(FRAME, NUMBER)` occurs once. The host deterministically expands bands. `wthash` is optional for harmonic input and checked if present. Raw `wtdata` has 1..16 samples per row, each −32768..32767; BAND 0..11, valid FRAME/START, no missing/duplicate sample, and a required matching `wthash`. The canonical output includes the verified content hash.

A small two-frame bank and moving binding:

```text
wtbank morph 2 256 12
harmonic morph 0 1 60 0
harmonic morph 1 1 60 0
harmonic morph 1 3 120 0
harmonic morph 1 5 180 0
wtuse tone 0 morph 0
mod tone 0 ctl0 wt_pos0 32768 3
```

Send positive `ctl0` from zero to 32768 to reach the second frame. The host's band-limited saw/square/pulse/triangle/form helpers expand to this native bank representation; there is no native `wtframe` or arbitrary formula statement. Source: `tl_header_wt`, `tl_check_wt`, `tl_check_header` in [harness](../src/host/harness.c); [asset contracts](../src/host/fm_a1.h).

### LFO and eight-slot modulation matrix

```text
lfo PATCH WAVE RATE_MILLIHZ DELAY_MS FADE_MS PITCH_CENTS LEVEL_DB10 INDEX_Q10 [PHASE_U16 MASK_PITCH MASK_LEVEL MASK_INDEX [FREE]]
mod PATCH SLOT SOURCE TARGET AMOUNT [SHIFT]
```

One LFO per voice. Waves: 0 sine, 1 triangle, 2 saw up, 3 saw down, 4 square, 5 sample-and-hold, 6 smoothed random. Rate 10..50000 mHz; delay/fade 0..60000 ms; pitch depth 0..1200 cents; level attenuation depth 0..1200 dB10; index depth 0..65535 Q10. Optional phase 0..65535 and all three masks must be supplied together; omitted masks cover all present operators, explicit zero masks select none. Masks may not name absent operators. Optional LFO FREE is **0 or 2**, never 1; FREE2 runs phase/grid in idle while delay/fade restart on each on. Omit the LFO row to disable it.

`lfo`, `peg`, `fenv`, `mod` and `ctl` require this timeline build's `FM_CTL=64`. LFO rate must also satisfy the strict control Nyquist inequality `RATE_MILLIHZ * FM_CTL < 500 * fs`; the 50000 mHz ceiling remains in force.

Matrix SLOT is 0..7, at most one row each. Sources are `lfo`, `peg`, `fenv`, `eg0`..`eg3`, `vel`, `note`, `ctl0`..`ctl7`, or `gen.ID.x` / `gen.ID.y`. The generator form requires a declared source; `.y` requires Hénon. Every source uses the engine's normalized Q15 control value; AMOUNT is the target offset at source +32768, with negative amounts reversing direction. `SHIFT` is 0..15, default 3; 0 applies without smoothing. Smoothing begins at zero on a start from silence and persists through retriggers. LFO is bipolar (with its delay/fade); PEG normalizes ±4800 cents to ±32768; FENV and EG gain are 0..32768; velocity is captured and normalized to 0..32768. `note` is the note-on pitch relative to A4, ±9600 cents mapped to ±32768 with a clamp. Ctl and generator caches supply their signed Q15 values.

| Target spelling | AMOUNT range / units | Prerequisite |
|---|---|---|
| `pitchN` | ±4800 cents | Present operator; fixed operators do not bend |
| `levelN` | ±1200 attenuation dB10 | Carrier operator |
| `indexN` | ±65535 Q10 | Operator with an outgoing PM edge (fixed pair: modulator 1) |
| `cutoff` | ±9600 cents | Voice filter |
| `q` | ±15500 Q-milli | Voice filter |
| `lfo_rate` | ±49990 mHz | LFO |
| `lfo_pitch` | ±1200 cents | LFO |
| `lfo_level` | ±1200 dB10 | LFO |
| `lfo_index` | ±65535 Q10 | LFO |
| `wt_posN` | ±32768 Q15 | WT on operator N |
| `pd_kneeN` | ±32767 Q15 | PD on operator N |
| `vector_x`, `vector_y` | ±32768 Q15 | Vector |
| `formant_centerN` | ±2147483647 mHz | Enabled formant N, N=0..2 |
| `formant_gainN` | ±1200 attenuation dB10 | Enabled formant N, N=0..2 |

Other N suffixes mean present operators 0..3. Feature prerequisites are checked even for AMOUNT=0. `peg` source needs nonzero levels; `fenv` source needs an enabled filter envelope/tracking; LFO source needs an LFO. Target sums are applied with the target's native clamp. LFO-target changes take effect on the next tick. All active rate slots together must pass the conservative bound `min(50000, base_rate + sum(abs(amount))) * FM_CTL < 500*fs`.

A controlled low-pass opening:

```text
filter tone lp 800000 1200
mod tone 0 ctl0 cutoff 2400 3
0 lead 0 ctl 0 0
0 lead 0 on 220000 32767
300000000 lead 0 ctl 0 32768
```

A four-corner motion uses `vector pad 16384 16384`, `mod pad 0 ctl0 vector_x 16384 3`, `mod pad 1 ctl1 vector_y 16384 3`; signed controls then cover both axes. Source: compact `TL_MOD_SOURCE`, `TL_MOD_TARGET`, `TL_MOD_LIMIT`, `tl_check_mod` tables in [harness](../src/host/harness.c).

### Seeded generator bank and note lanes

The bank is a control/sequencing system, separate from audio oscillators. Its descriptor table is [src/gen/params.def](../src/gen/params.def), also emitted by `harness --gen-params`. There are at most 16 sources (IDs 0..15), 8 lanes (IDs 0..7) and 2 text-word sources; expanded words have capacity 16384 symbols.

```text
gen_clock CTL
gen ID KIND SEED DIV SLEW KIND_FIELDS...
gen_axiom ID DEPTH WORD
gen_rule ID SYMBOL WORD_OR_DASH
gen_lane ID VOICE GATE_SOURCE PITCH_SOURCE ACCENT_SOURCE STEP_TICKS BASE_CENTS SPAN_CENTS VELOCITY GATE_TICKS SCALE_MASK
T_NS STREAM gen ID PARAM VALUE
T_NS STREAM lane ID enable 0|1
```

`gen_clock` accepts 1..4096 but must equal the executable's `FM_CTL` (normally 64). It is mandatory for any generator declaration/binding. SEED is 0..4294967295; DIV 1..65535 ticks; SLEW 0..512 Q15 units/tick (0 bypasses slew). Text `gen` requires all kind fields; defaults below describe the authoring API/default configuration, not optional positional text tokens. Sources start enabled. Common runtime PARAM names are `enabled`, `slew`, `reset` (the new seed); DIV, kind and structural dimensions are immutable.

| KIND | Required KIND_FIELDS in order; bounds; defaults | Additional runtime fields |
|---|---|---|
| `white`, `pink` | None | None |
| `brown` | STEP 1..4096, default 16 | `step` |
| `multi` | P 2..65536 (256), K 2..16 (2), OCTAVES 1..8 (6), GAIN_Q15 0..32768 (24576); `P*K^(OCTAVES-1)` must fit uint32 | `gain` |
| `logistic` | R_Q48 0..1125899906842624, default 1123085157075517 | `r` |
| `henon` | A_Q48 0..562949953421312 (394064967394918), B_Q48 −281474976710656..281474976710656 (84442493013197) | `a`, `b` |
| `euclid` | K 0..N (3), N 1..65535 (8), ROT 0..N−1 (0), PARENT `-` or an existing lower source ID | `k`, `n`, `rot` |
| `thue`, `cantor` | LENGTH 1..65535 (256), ROT 0..LENGTH−1 (0) | `length`, `rot` |
| `fibonacci` | DEPTH 0..16 (12), ROT 0..expanded-length−1 (0), at most 16383 | `rot` |
| `lsystem` | ROT 0..expanded-length−1 (0), at most 16383; requires its text definition | `rot` |

Only Euclidean children can have a parent. `gen_axiom` depth 0..16, word 1..32 symbols A–H. `gen_rule` symbol A–H, replacement 0..32 symbols; `-` means empty. Missing rule preserves the symbol. Capacity, duplicate IDs/rules, undefined dependencies and coupled bounds are refused. Runtime coupled changes are validated by the bank, not merely against independent table minima/maxima. Source changes/reset queue until that source's next DIV tick, so order coupled edits to keep the complete pending configuration valid. Disable freezes recurrence and targets zero while slew continues; reset preserves the published slew value and session grid. Lane enable/disable applies at the next bank tick.

Lane source references are `-` or a declared source ID. Lane VOICE must exist and cannot be reserved by another lane. STEP/GATE_TICKS are 1..65535 and GATE_TICKS must not exceed STEP_TICKS; BASE_CENTS −4800..14400 is **absolute MIDI cents** (6900 = A4), SPAN_CENTS 0..9600; velocity 1..32767; SCALE_MASK 0..4095, bit 0 = C, zero disables quantization. The reachable pitch range must remain within −4800..14400 cents: validate `BASE−SPAN−1199` when scaling is enabled (otherwise `BASE−SPAN`) and `BASE+SPAN`. Scale quantization snaps downward. Out-of-range configurations are refused rather than silently clipped. A reserved voice refuses manual `on`, `off` and `pitch`, but can receive appropriate control/spatial/send events.

At a bank boundary: explicit timeline events run first, then the bank ticks, publishes caches, and emits generated offs/ons. Therefore an explicit on at frame zero sees the initial zero generator cache; a generated lane on sees that tick's new cache. Do not flatten a bank into guessed note events when exact cache timing matters. `--gen-events` exposes the actual deterministic schedule; generated event timestamps use checked ceil conversion and refuse a frame with no exact nanosecond representation.

A reproducible sparse note lane, assuming voice 0 and stream `lead` already exist:

```text
gen_clock 64
gen 0 euclid 7 1 0 3 8 0 -
gen 1 brown 9 1 16 16
gen_lane 0 0 0 1 - 188 6000 1200 24000 120 2741
mod tone 0 gen.1.x cutoff 1200 3
1000000000 lead gen 1 step 32
1500000000 lead lane 0 enable 0
```

This also requires `filter tone ...` for the shown target; omit manual note events on voice 0. The scale mask 2741 selects C-major pitch classes. Sources: `tl_header_gen`, `tl_check_gen`, `tl_event_gen` in [harness](../src/host/harness.c), [generator contract](#session-generators-and-live-lanes), [descriptor table](../src/gen/params.def).

### All ordinary event forms

```text
T_NS STREAM VOICE on FREQ_MILLIHZ VELOCITY_Q15
T_NS STREAM VOICE off
T_NS STREAM VOICE pitch FREQ_MILLIHZ
T_NS STREAM VOICE filter CUTOFF_MILLIHZ Q_MILLI
T_NS STREAM VOICE ctl N VALUE_Q15
T_NS STREAM VOICE send chorus|delay|reverb|duck|flanger LEVEL_Q15
T_NS STREAM VOICE crush BITS RATE_DIV
T_NS STREAM VOICE pos X_MM Y_MM Z_MM
T_NS STREAM VOICE move X_MM Y_MM Z_MM DUR_NS
T_NS STREAM 0 yaw MDEG
```

On/pitch frequency accepts 0..4294967295 mHz; on velocity text 0..65535, effective clamp 32767. Pitch changes relative oscillator frequency without restarting phase/envelopes. Filter event uses the same accepted/effective bounds as its header and requires a filter. Ctl N 0..7, value −32768..32768, latched per voice. Send level 0..32768 requires the bus's FX header. Crush requires the valueless `crush` header, BITS 1..24 and RATE_DIV 1..64; it is a per-voice insert before dry positioning and sends. Spatial events require draft2; their ranges follow below. There are no runtime patch-replacement, route-edit or arbitrary native parameter-name events: host helpers lower supported automation to these events or declare another patch/voice.

Session generator events use `T_NS STREAM gen ID PARAM VALUE` and
`T_NS STREAM lane ID enable VALUE`: `gen`/`lane` replace the voice token.
Their accepted fields and lifecycle are in `Session generators and live lanes`.
For ordinary controller events write `ctl 0 VALUE`, not `ctl0 VALUE`;
`ctl0` belongs in a modulation-source declaration. The native parameter
reference lists each exact event statement, including its word position.

### Effects, sends and decodable profile

These singleton session headers are opt-in; omission disables a stage. Mono (`space off`/draft1) and ordinary stereo accept the current chain at fs 8000..192000. FX also admits binaural and raw amb1/amb3 routes, subject to their own rate/table bounds. Binaural accepts the current master chain; raw amb1/amb3 accepts gain/DC plus source crusher and audible buses, and refuses compressor/duck, limiter, softclip and dither. PANLAW still refuses all FX.

| Exact header form | Bounds, in positional order |
|---|---|
| `dcblock FC_MILLIHZ` | 1..100000 |
| `chorus BASE_US DEPTH_US RATE_MILLIHZ` | 7000..25000; 1000..5000; 100..3000 |
| `flanger BASE_US DEPTH_US RATE_MILLIHZ FEEDBACK_Q15 INVERT` | 100..10000; 100..BASE_US; 0..2000; 0..29491; 0 or 1 |
| `delay TIME_US FEEDBACK_Q15 LOWPASS_HZ` | 50000..1000000; 0..29491; 20..20000 |
| `pingpong TIME_US FEEDBACK_Q15 LOWPASS_HZ HIGHPASS_HZ` | Same first three; HP 0..2000 (0 disables HP); ordinary stereo or field routes; mutually exclusive with delay |
| `reverb ROOM_PERMILLE DAMP_PERMILLE` | Each 0..1000 |
| `compressor THRESHOLD_DB10 RATIO_TENTHS ATTACK_MS RELEASE_MS MAKEUP_DB10` | 0..600 attenuation; 10..200 (40 = 4:1); 0..100; 1..5000; 0..240 positive makeup |
| `limiter THRESHOLD_DB10 RELEASE_MS` | 0..200 attenuation; 1..5000 |
| `softclip` | No values; cubic soft clipping, not an oversampling option |
| `gain GAIN_DB10` | Signed −600..240; positive boosts, negative attenuates |
| `dither SEED` | 1..4294967295; deterministic TPDF |
| `crush` | No values; enables per-voice mask/hold inserts, initially exact 24/1 bypass; runtime `crush BITS RATE_DIV` selects each voice's settings |
| `decodable` | No values; reader admission profile, not an FX stage |

Four audible buses are chorus, delay, reverb, flanger. Duck is a compressor detector with **no audible return**. Ping-pong uses `send delay`, not `send pingpong`. All sends start at zero: declaring a bus alone leaves every voice dry. Events at frame zero set initial levels without a fade; later changes apply from the next block boundary and ramp across that block. Sends are taken before pan; dry source position does not move the fixed wet return image. Flanger rate 0 is a static comb; a positive rate moves its two taps. Feedback remains causal with at least one sample of delay.

Returns join dry audio, followed by master gain → DC blocker → compressor → limiter → softclip/plain rounding with optional dither. Compressor and limiter share one block of lookahead rather than adding two blocks. Makeup applies below threshold too. Neither compression nor a quiet master result proves that no earlier stage saturated.

The limiter bounds sample magnitude at its own output using interpolated safe
gain, rather than hard clipping each sample. `limiter 18 100` means a nominal
−1.8 dBFS threshold and 100 ms release. The fixed-point threshold and final PCM
rounding can differ slightly from the decimal threshold; softclip and dither
follow it. This is not a final true-peak ceiling: intersample reconstruction
can exceed sample peak, and the downstream softclip has small-signal gain.

Canonical header order is serialization order, not processing order. Reordering
headers does not rearrange that chain. Optional dither follows softclip, before
final rounding and saturation.

Without a selected duck send, the compressor detects the largest absolute
signal across channels after returns, gain and DC blocking, before makeup.
A nonzero previous or new duck-send target selects the pre-pan duck sum for
the whole block, replacing program detection. A silent sender still selects
duck; silence does not switch detection back to the mix. The detector takes
the absolute signed sum, so opposing senders can cancel. One shared gain
controls both stereo channels.

Master softclip implements the clamped cubic `1.5x - 0.5x³`: input is limited
to ±1, with output endpoints scaled to ±32767 PCM units using integer products
and final rounding. Its small-signal gain is approximately +3.52 dB, rather
than unity. Since it follows the limiter, it can raise output above the
limiter's selected threshold. The FX `clipped` counter counts final rounded
values exceeding the int16 range; it does not count softclip flattening,
earlier saturation or samples merely equal to an output rail.

| Audible bus | Ordinary-stereo return routing |
|---|---|
| Chorus | Shared mono delay line; right modulation phase is 90° ahead of left. |
| Flanger | Shared mono line with the same 90° phase offset. Only the left tap feeds feedback; `INVERT` negates feedback, not the emitted wet signal. |
| Delay | The same delayed return is added to both channels. |
| Reverb | The same mono input feeds separate left/right networks with different line lengths. |
| Ping-pong | Left tap at T, right at 2T; far-tap feedback recirculates through the filtering loop. |

Mono uses one return channel; ping-pong requires ordinary stereo or a field route. Sustained input and
feedback tails can overlap, so ping-pong need not have exclusively alternating
channel activity. These describe routing, not perceived width or location.

Field FX uses eight dedicated virtual return sources: left/right for chorus,
flanger, delay (plain or ping-pong) and reverb. They stay at world azimuths −30°
and +30°, radius 1 m; voice motion changes the dry source, while listener yaw
rotates dry and wet together. Each return has separate pinna history. Sends
are captured after source crusher and before the sender's pinna. No voice IDs
are consumed by returns. Raw fields process only gain/DC after encoding;
binaural processes its master on wide decoded ears before one final rounding.
These are routing facts, not a perceptual localization guarantee. Custom
binaural tables with FX require `sum(abs(taps)) <= 2^(table_shift+6)-1`,
as checked by `sp_field_attach_fx` in the [field API](../src/space/field.h).

For a dry/wet stereo pluck with ducking:

```text
timeline draft2
space stereo
block 64
near 100
pingpong 187500 16000 5000 80
flanger 2000 1000 300 18000 0
compressor 120 40 5 120 0
gain -60
0 lead 0 send delay 8192
0 lead 0 send flanger 4096
0 drums 1 send duck 32768
```

Define `drums` in streams and voice 1 as the kick; retain the regular note events. This shows the header/send pairing, not a whole session. Leave tail time after the last note.

`decodable` refuses limiter, softclip, compressor, chorus, crush even at 24/1, nonzero-rate flanger, **every shape including zero drive**, field routes, every move, and repeated or nonzero-time pos for any voice. At most one pos per voice at exactly 0 ns is allowed; ordinary fixed stereo placement is accepted. Static flanger, delay/ping-pong, reverb, DC, gain and dither are not rejected by this current profile. Admission does **not** prove absence of clipping, invertibility, message recovery or transcription accuracy.

Sources: `tl_header_fx` and `tl_check_decodable` in [harness](../src/host/harness.c), [FX public configuration](../src/fx/fx.h), [FX implementation](../src/fx/fx.c), [FX SPEC](#effects-sends-and-decodable-profile).

### Spatial rendering

```text
timeline draft2
space off|stereo|amb1|amb3|binaural
block 32|64|128
near RADIUS_MM
order 1|3
pinna off|pbnh|rd
pinna D0 D1 D2 D3 D4
yaw MDEG
panlaw WIDTH_Q15
```

`space` defaults off; block defaults 64 and near defaults 100 mm (accepted 1..16777216). `stereo` is the ordinary two-channel distance/pan route; its panning does not add an inter-ear delay. `amb1` and `amb3` produce 4 and 16 AmbiX ACN/SN3D channels. Binaural produces two channels; `order` is legal only there and defaults 3. It needs the compiled decoder table and its 44100/48000 sample rates. The standard Makefile generates the table; an alternative build without it refuses binaural.

For ordinary `stereo` without `panlaw`, the settled dry voice gains approximate:

```text
r = sqrt(x*x + y*y + z*z)              # millimetres
u = x / max(r, near)
g = 1000 / max(r, 1000)
left_gain = g * sqrt((1-u)/2)
right_gain = g * sqrt((1+u)/2)
```

The implementation uses integer square roots, division and Q15 gains. `near`
only limits the pan denominator; distance gain stays unity through 1 m and
falls by about 6.02 dB per radius doubling beyond it. Center gives about
0.707 per channel; `(1000,0,0)` is fully right. `(450,880,0)` has `u≈0.456`,
so its pan is much less extreme. Changing y or z changes both radius and the
ratio of x to radius. Gains ramp per sample between spatial block boundaries.
These formulas are not binaural, AmbiX or PANLAW formulas. Sends are pre-pan;
their return images are listed in the FX table and do not follow voice motion.
Only plain delay has the same mono return in both ordinary-stereo channels.
Actual mix width also depends on voice content, timing and effect history;
coordinates or one source's gains alone do not establish it.

`pinna` and `yaw` headers require amb1/amb3/binaural, not ordinary stereo. Pinna defaults off; custom five D values are each 0..1000. Non-off pinna requires fs<=48000. Yaw defaults 0, range −360000..360000 millidegrees; runtime listener yaw uses voice 0 only. `panlaw` is an alternative stereo field path, width 1..32768 Q15, which also excludes FX.

Binaural and raw amb1/amb3 admit the fixed-return FX described above.
Raw fields admit gain/DC and source crusher but refuse nonlinear masters
and dither; PANLAW rejects every FX header.
Set source levels before rendering and inspect peak/clipping for each route;
the same patch can have different output levels across routes. No automatic
headroom threshold or per-voice localization verdict is supplied.

Coordinate axes are x right, y forward, z up, in millimetres. Each coordinate is −16777216..16777216; initial position is `(0,1000,0)`. Position/move updates follow the spatial block grid. Move starts from the position at its application boundary and reaches the destination at the first boundary at or after `T_NS+DUR_NS`; a move may extend beyond the record and be cut there. Duration is positive and checked so its endpoint remains below 2^32 samples.

For a binaural orbit fragment (no FX headers):

```text
space binaural
order 3
pinna pbnh
yaw 0
0 lead 0 pos -1500 1500 500
500000000 lead 0 move 1500 1500 500 1000000000
1000000000 lead 0 yaw 15000
```

`harness --play --stems WAV ...` can feed an external 16-bit PCM stem per voice through amb1/amb3/binaural/panlaw at the session rate. Short stems are zero-padded and long stems truncated. This is a host spatial test/input facility, not a PCM oscillator in the synth.

Sources: `tl_header_field`, `tl_check_field`, `tl_event_field`, `tl_load_stems` in [harness](../src/host/harness.c), [space API](../src/space/space.h), [field API](../src/space/field.h).

### Discovery, validation and limits of this surface

Existing native commands, using the setup variables above. Substitute the
actual input files in your external working directory:

```sh
"$HARNESS" --params
"$HARNESS" --params-check
"$HARNESS" --gen-params
mkdir -p "$WORK/engine-examples"
"$HARNESS" --emit "$WORK/engine-examples"
"$HARNESS" --play --metrics --wav "$WORK/out.wav" --merged "$WORK/replay.tl" \
  -- "$WORK/session.tl" "$WORK/lead.tl" "$WORK/drums.tl"
"$HARNESS" --play --chunk random -- "$WORK/replay.tl"
"$HARNESS" --gen-events -- "$WORK/session.tl" "$WORK/lead.tl"
"$HARNESS" --fxref -- "$WORK/session.tl" "$WORK/lead.tl"
```

`--emit` emits the engine's fixed test examples; it is not a complete patch-parameter catalog. `--gen-params` is the actual 11-kind/37-field generator descriptor export. `--params` exposes 68 statements/events and 287 fields from the native `src/host/params.def`, with 22 selector forms and a reference to the generator table. Each row supplies physical token positions, native field names, scalar bounds, units, divisor scales, effective clamps and named coupled checks. Human values are raw divided by scale. Nonnumeric bounds are `none`; a scalar bound alone does not validate coupled settings. `--params-check` exercises actual canonical writer coverage. Numeric refusals identify the statement/patch/operator or event context and field; other native refusals retain their text. The stateless MCP adapter serves this same native listing. The compact sources for maintaining this reference are the native parameter inventories, `fm_a1.h` and the complete header/event validators. The sections [Patches, recipes and the synthesis repertoire](#patches-recipes-and-the-synthesis-repertoire), [Shared CLI and MCP](#shared-cli-and-mcp) and [Inspect, revise and hand off](#inspect-revise-and-hand-off) document the host conveniences lowered to this repertoire; they are not extra native statement families.

The served `reference/params.txt` contains whitespace-separated `key=value`
metadata rows, not TL declarations. Decode its row kinds as follows:

| Row | Fields and interpretation |
|---|---|
| `PARAMS` | Opening version/engine identity; closing count/statements/forms summary. |
| `STATEMENT`, `EVENT` | `id name section once min_values max_values word_position`. Counts exclude the keyword and event prefix; −1 means variable maximum. They are outer bounds, not permission for every intervening arity. |
| `PARAM` | `id statement name position type lo hi unit human scale clamp default mutable depends checker choices form`. `statement` joins the statement ID; numeric human value is raw/scale. `lo`/`hi` are accepted outer bounds, distinct from effective clamps and coupled checks. |
| `FORM` | `statement id position match values checker` selects a token layout. Match kinds are `exact`, `prefix`, `count`, `default_or_exact`; values are pipe-separated. `count` uses full-line token count. |
| `FAMILY` | `id path checker` links the separate generator inventory instead of duplicating it. |

`position` counts zero-based tokens in the full TL line: header keyword at 0,
ordinary event keyword at 3, gen/lane keyword at 2. `N+` repeats from N;
`N+.PART` selects a component of a packed route edge; `N.KEY` a key=value
component. `form=all` is unconditional. A named form must match uniquely;
prefix selectors describe canonical stems, not acceptance of arbitrary suffixes.
`default=token:...` applies to an omitted optional token, `statement:...` to
an omitted declaration, and `initial:...` to initial runtime state. These do
not permit skipping operands in a declaration you do write. `required`,
`context` and `external` retain their named meaning. `mutable=1` identifies an
existing timed event; 0 does not describe every live API or modulation path.
The native reader remains the authority for valid names, arities and combinations.

Canonical TL is a semantic serialization, not a text-preserving editor. It
removes authored comments/spacing, orders headers, emits normalized/default
fields and sorts scheduled events by their defined order. Keep the editable
score/source separately. Use native validation or the native descriptor when
editing fields; do not assume a regular expression for an authored row still
matches its canonical form. `listen` can render a hashless draft and return
canonical text containing its computed audio hash; that alone is not an
independent replay check. Verify that returned canonical record separately.

A new record can omit hash, render and canonicalize, then replay the merged record with its asserted hash. Check the command exit status **and** reported hash/chunk/clipping results. Native exit codes are 0 success, 1 bad timeline/block dependence/format limit, 2 CLI or unsupported mode, 3 recorded-hash mismatch, 4 failed reference/gate criterion, and 5 host I/O or allocation failure. A successful PLAY line followed by a failed requested write still exits 5; fix the host/path instead of modifying valid musical input. `--play --ref` is not universal: it refuses unsupported stereo/no-FX-field/generator-without-FX comparisons. Field FX runs an independent extra native/model comparison, with `FIELD_FXREF` reporting completed work; any native field clipping conservatively refuses reference certification with status 2. Completed PLAY output remains available. Valid clipped native playback is still supported. Use the mode's actual acceptance scope rather than interpreting a missing reference comparison as success.

Implemented repertoire includes FM/PM, additive routing/banks, AM/ring/sync, the twelve waves/noises, envelopes/loops/key scaling, PD, formants, vector, wavetable and KS, modulation matrix controls/generator binding, the FX table including per-voice crusher, and all spatial routes above. Drum recipes are synthesized from these sources; they are not sample playback. Do not advertise six-operator chip emulation, arbitrary PCM oscillators, a second LFO, matrix feedback/curve/via targets, EQ or hall as current features. Crusher works in mono, ordinary stereo and field routes before placement and sends. EQ and hall remain unimplemented. Field-route FX uses the fixed returns and master restrictions described above. The final voice amp envelope is implemented; future amp-dependent features need their own acceptance. Acoustic message encoding experiments exist elsewhere, but the WAV decoder/channel-recovery acceptance remains separate. Replay, recoverable messages and approximate transcription are different claims.
