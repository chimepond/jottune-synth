<!-- Copyright (c) 2026 Nenad Micic -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Offline agent bundles

A bundle lets the official Node client compose, render, analyze, verify and
export on its first run without contacting a player. It uses Node 22.15+ and
built-in modules only. This is a local packaging primitive; no npm package or
GitHub release is published by these commands.

Build from a trusted staged player containing the current client and its
official `agent-manifest.json`. Choose a new output directory:

```sh
node tools/agent_bundle.mjs --source STAGED_PLAYER_DIR --out NEW_BUNDLE_DIR
```

The default kept renderer is selected unless `--release ID` names another kept
release actually present in the stage. Development, withdrawn and missing
releases are refused. The output must be outside the source tree and must not
exist already.

The directory contains the unchanged official manifest, selected renderer,
shared modules, exact documentation, editable starter score and the two pinned
teaching fixtures `starter.tl`/`example.tl`. These fixtures retain the ordinary
cache contract; they are not a bundled music catalog. It excludes
the song catalog/library, contributed songs, deployment configuration, receipts, cache metadata
and other files absent from the manifest. `agent-bundle.json` contains only the
bundle format, manifest checksum and selected release. No player address is
embedded by the builder. Keep the entire directory together when attaching or
packaging it.

Use a supplied player base only for local cache identity and exported links:

```sh
node agent.mjs "$SITE" compose song.score.json --bundle BUNDLE_DIR --offline \
  --cache ./cache --mode listen --analysis --out audition --formats tl,json,link
node agent.mjs "$SITE" verify audition.tl --offline --cache ./cache \
  --out finished --formats tl,json,link
```

`--bundle` verifies all selected assets, then populates the ordinary cache and
binds it to the caller's `SITE`. It implies offline execution even without
`--offline`; errors never fall back to HTTP. `SITE` must be a clean player base
with its trailing slash. Link exports use that address as text, without fetching
it. A different player base can import the same neutral bundle into its own
cache. Later `--offline` commands can use that complete cache without the bundle.

Running the `agent.mjs` inside a bundle detects adjacent `agent-bundle.json`,
so no `--bundle` flag is needed. Explicit `--bundle` takes precedence. To refresh
online, use a separate client outside a bundle and omit `--offline`/`--bundle`.
An explicit missing release always refuses; another installed release is never
substituted. Bundle mode cannot fetch catalog examples. This standalone client
has no upload command.

All offline results report `offline:true` and `revocationChecked:false`.
Checksums establish consistency with the supplied manifest, not independent
authenticity or current revocation status. Obtain the client and bundle from a
trusted source. The execution worker rechecks cached imports and assets using
the same loader as ordinary setup; no alternative Wasm host is introduced.

Run focused checks with:

```sh
node --test tests/agent_bundle.test.mjs
node tests/agent_client.mjs
AGENT_BUNDLE_SOURCE=STAGED_PLAYER_DIR node --test tests/agent_bundle.test.mjs
```

The last command includes actual kept-runtime composition, cached-PCM analysis,
saved-TL verification and TL/JSON/link export from an empty cache. Every HTTP
attempt is blocked and recorded as a failure. Unit checks also cover adjacent
discovery, corruption, missing assets, symlinks, release refusal, upload-command
refusal and importing the same bundle for two player bases. The actual render check is skipped unless
`AGENT_BUNDLE_SOURCE` is supplied.
