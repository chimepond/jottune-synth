<!-- Copyright (c) 2026 Nenad Micic; SPDX-License-Identifier: Apache-2.0 -->
# Building JotTune Synth

The native tools require a C99 compiler, Make and Python 3.10+. Browser builds
also require Zig's WASI toolchain. Optional analysis uses NumPy; PNG generation
uses Matplotlib. Those packages are host dependencies and are not linked into
the integer synthesis library.

```sh
make
make portable
make ubsan
make quick
make check
make -C bindings/native check link-smoke
```

`make check` runs numerical, parser, compatibility and negative-input checks.
The optional complete song collection is selected through `JOTTUNE_TUNES_ROOT`
for corpus replay. A missing optional collection is reported explicitly.
`make check-interchange` exercises MIDI, native WAV and sampled SFZ exports.

## Browser builds

```sh
make web
python3 -m http.server 8080 --directory web
```

All player assets are relative, so the generated `web/` tree can be served from
an origin root or a directory. It needs no account, database or remote rendering
service. Use localhost for development and HTTPS for service-worker offline use.

The browser and Node client use a renderer registry. Development builds receive
development identities. Kept renderer archives preserve their original bytes,
runtime and licences; adding a new build never changes an existing archive.
Renderer withdrawal marks an existing identity unavailable rather than replacing
its audio under the same name. Source-rate PCM hashes exclude device resampling.

Serve the built tree and run optional real-browser acceptance with an installed
Playwright module and browser:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs make check-browser PLAYER_URL=http://localhost:8080/
```

Phone-size browser emulation checks layout and interaction. Physical Safari,
audio routes, screen readers and background playback need device acceptance.

## Native clients

The macOS source lives in `apps/macos`; the reusable full-record bridge lives
in `bindings/native`. iOS and Android consume that bridge from a separately
selected synth checkout. Platform audio adapters own playback and resampling;
the bridge owns validation, rendering, cancellation and canonical export.

See each client README for its toolchain and current supported behavior.
Source builds do not imply signed distribution or store approval.
