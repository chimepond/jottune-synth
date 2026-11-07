<!-- Copyright (c) 2026 Nenad Micic -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Interfaces

The core API is declared in `src/host/fm_a1.h`; spatial, field, effects and
generator APIs are in their respective `src/` headers. Callers allocate state
and buffers, initialize voices and supply signed integer control values. The
mono voice output is signed PCM16; the separate spatial module provides stereo,
binaural and field output. All state belongs to the caller.

The host `harness --play FILE... --metrics` accepts one complete TL record or an
ordered session/stream pool. `--merged PATH` writes canonical text with the
rendered PCM hash. `--wav PATH` writes audio. Exit 0 means the requested native
checks pass; exit 3 means the declared hash differs. Other failures include
line-numbered reader diagnostics, block dependence, unsupported modes and I/O.
Do not treat a mismatch's rewritten canonical hash as independent verification.

`--play-once FILE...` is the listener path: one render, the same PCM/hash as
the first verify render, and `block_indep=unchecked`. It does not certify block
independence. `--play` remains the two-pass verification path. A record without
a hash can render as a draft but is not a verified song; replay the resulting
single canonical TL with its saved hash before publishing. `--engine` reports
the compiled engine descriptor without reading a song.

`src/host/browser.c` is a thin host wrapper over that reader and renderer. Its
`--inspect` mode reports parsed dimensions and allocation lower bounds;
`--events` emits authored events plus the existing deterministic generator
manifest. Time and size values are decimal strings on the JavaScript surface.
The optional PCM capture hook leaves ordinary CLI behavior unchanged.

`--inspect` also reports `hash_present`, `expected_hash` and
`once_render_bytes_lower_bound`. The native reader supplies these fields; browser
code must not infer them by parsing TL statements.

`IntFM.render(text)` keeps the verification default. Passing
`{mode: 'listen'}` selects the one-render path. An explicit `release` selects
one trusted registry entry; a missing or withdrawn entry is an error, never an
invitation to substitute another build. Full verification requires a declared
hash match and checked block independence. A listener hash match is reported
separately from its unchecked block independence.

The format-1 engine registry distinguishes development entries from kept
releases. Kept entries use an explicit `YYYY-MM-DD-shortcommit` release
ID, bind `source_commit` to the public source revision, and keep the matching
Wasm, runtime module and shim together. A withdrawn entry retains its ID and
withdrawal date. Development entries use `dev-<digest>` and cannot claim release
acceptance. Neither an engine-line match nor a song-supplied URL selects code.

An exchange can mount the player at any path prefix. Under that prefix,
`r/<sha256>` resolves `api/records/<sha256>` and `s/<id>` resolves
`api/submissions/<id>`. Both return
`{record_sha256, renderer_release, record_url}`; extra service metadata is
optional. The player checks downloaded TL bytes against `record_sha256` and
uses the named trusted release. Downloading and replaying the TL alone remains
supported. Existing `#r=` and `#v1.` links remain readable. Service-worker scope
and relative resource URLs stay inside the mount point.

## Paged source library

Under a user-selected HTTPS service prefix, `catalog/index.json` describes
the curated source library. It is separate from the exchange's uploaded-song
`api/index`. No account, upload or automatic engine download is involved.

The version-1 manifest has `page_size:24`, `total_songs` and `categories`.
Each category contains `id`, `title`, `count` and `first_page` (a relative path,
or null for an empty category). Each page contains `version:1`, `category`,
`total`, at most 24 `items`, and `next` (relative path or null). Resolve every
path against the service prefix, not the page's own directory. Category IDs
belong to that manifest; refresh the manifest when changing deployments.

Each item has `id` (the unique saved-variant ID), `title`, `collection`, `revision`, `variant`, `path`,
`bytes`, `record_sha256`, `duration_seconds` and `technical`. The technical
object retains native-inspected `source`, `channels`, `route`, `allocated_voices`
and `events`. Optional `metadata`, `provenance` and editorial `note` describe
only the exact selected variant. Featured and Synth showcase use their explicit
editorial order; general browsing prefers a saved Keys variant, then Standard.
Collections and available genre/model declarations form additional categories;
unknown values are not guessed or transferred between profiles.

Clients fetch one category page at a time and TL bytes only after selection.
Bound JSON responses to 256 KiB, online TL responses to 1,000,000 bytes, and
reject redirects or paths escaping the chosen origin/prefix. Verify declared
TL byte count and SHA-256 before the existing native import. Native validation
and saved PCM hashes remain authoritative; loading a downloaded song is silent
until Play. The catalog references existing complete TLs and stores no copies.

`tools/export_library.py` produces these pages alongside the validated export.
`--refresh-catalog` regenerates pages from a checked existing export without
rendering or modifying TLs. `--check-output` also detects incomplete or stale
browsing pages. A synthetic 10,000-song check exercises complete pagination and
response bounds; it is not an HTTP load test or a musical-quality ranking.

Browser functions, iframe protocol, provenance keys and resource limits are
specified in [the browser guide](../web/browser-guide.txt). Browser diagrams are
read-only. There is no second JavaScript TL parser, timeline editor, server
rendering, account service or dataset publishing in this repository.

Freestanding Wasm exports the C functions, linear memory, `__data_end` and
`__heap_base`. Embedders use the matching wasm32 C structure layouts in the
headers and place caller-owned, aligned state above `__heap_base`; this is a
low-level library ABI, not a second TL player or a JavaScript allocation API.
