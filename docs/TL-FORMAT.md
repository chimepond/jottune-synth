<!-- Copyright (c) 2026 Nenad Micic -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Portable JotTune Timeline documents

A TL document is one playable text file: an optional metadata header followed by
one native TL music body. Existing headerless files remain valid and unchanged.
The header is descriptive data, never instructions for an agent or executable code.

## Header and compatibility

The first line, when present, is exactly `# jottune-tl 1 1`: the first number is
the TL document/grammar compatibility version; the second is the required synth
engine compatibility version. Both are unsigned 32-bit decimal integers in
1..4294967295. Current readers support version 1 of each. Refuse unsupported
higher requirements before rendering; never clamp, discard or downgrade them.
The header ends with `# end-jottune-tl`. Between those markers every line starts
with `# ` and together forms one JSON object. Duplicate keys and unknown fields
are refused. A recognized malformed, repeated, misplaced or unterminated header
is an error, not an ordinary comment. LF and CRLF input are accepted. Preserve
supported header contents through full-document import/export; body-only export
is an explicit identity/internal operation.

```text
# jottune-tl 1 1
# {
#   "title": "Example composition",
#   "made_by": "agent",
#   "model": {"id": "reported-model/version", "source": "client-reported"}
# }
# end-jottune-tl
```

Headerless files imply the legacy version-1 contract. The existing `timeline draft1`/`timeline draft2` grammar and exact `engine` descriptor/table matching remain
authoritative; version <= maximum alone never proves compatibility. Future TL
syntax or changed rendering semantics require the applicable increasing counter
and corresponding native grammar/engine identity changes, with old-version
replay tests. Documentation-only changes do not bump compatibility. Renderer
release IDs and package semantic versions remain separate identifiers. Old
binaries that ignored comments cannot retroactively enforce this header.

## Metadata fields and limits

All fields are optional; omission/null means unknown where specified below.
No author, model, licence or genre is inferred. Limits reuse existing catalog
and publication field bounds; a header-bearing portable file remains at most 1,000,000 UTF-8
bytes, without BOM or NUL. Every newline-terminated line is at most 1022 content
bytes, including `# `, matching the native reader. Fixed allowed fields bound
field count; arrays, arbitrary extension objects and free prompts are refused.

| Field | Accepted value |
| --- | --- |
| title | Nonempty NFC text, at most 60 Unicode characters |
| collection, author | Nonempty NFC text, at most 200 Unicode characters |
| made_by | agent, human or mixed |
| genre | null or existing ASCII label grammar `[A-Za-z0-9._:+/@-]`, 1..160 characters |
| tempo | null or exactly {min_bpm,max_bpm}, finite positive numbers, min <= max, <= 9007199254740991 |
| model | null or exactly {id,source}; id is the same 1..160 label grammar, source client-reported or user-stated |
| utc_day | Valid YYYY-MM-DD calendar date |
| license | Nonempty 1..160 ASCII label; a declaration, not verified authority |
| renderer_release | Existing release-token grammar `[A-Za-z0-9._-]`, 1..128 characters |
| prompt_id | Nonempty 1..160 ASCII label, never a free prompt |

Text must have no leading/trailing whitespace, NUL, control, surrogate, line or
paragraph separator characters. Normalized non-ASCII text remains readable.
Metadata is validated by the shared Python/JavaScript document helpers and
the native macOS host; the
native engine enforces framing, line/size limits and compatibility requirements.

## Identity and authority

`record_sha256`/song ID is SHA-256 of the exact canonical native music body,
excluding this header. Native `hash` is still the saved rendered-PCM check.
A full-file SHA-256, when needed for transport, covers the complete final document
and is a separate value; do not put that checksum inside the bytes it hashes.
Metadata-only edits never mint another public song or grant control over one.
Existing record IDs, URLs, receipts, reservations and stored legacy bytes remain
unchanged. New portable exports may add headers without renaming the song ID.

Explicit full-document export retains validated metadata; explicit canonical-body
export omits it. Metadata does not override native body semantics. When an
external catalog/submission supplies the same descriptive field, require agreement
rather than silently choosing one value; absent fields retain the legacy external
fallback. Editors may deliberately replace metadata for a new local export.
Catalogs and indexes can be derived from embedded declarations. Editorial choices
remain catalog/platform data.

Publication consent and its acceptance time, withdrawal credentials, moderation,
verification authority, private reports, account data and popularity remain
outside the TL. A licence field never supplies publication consent or relicenses
old archive records. Model attribution remains self-reported for that exact
record; automatic Keys/Mirror transforms emit a body-only record rather than inherit
unverified exact-record attribution. Explicit source revisions retain their
user-supplied declarations until the author updates them.
