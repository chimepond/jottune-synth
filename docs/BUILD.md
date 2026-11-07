<!-- Copyright (c) 2026 Nenad Micic; SPDX-License-Identifier: Apache-2.0 -->
# Building JotTune Synth

The native tools require a C99 compiler, GNU Make and Python 3.10+. Browser builds
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

Native CLI/bridge builds support macOS and Linux; the macOS application requires
Apple Silicon, macOS 14+ and Xcode command-line tools. The shared bridge targets
iOS 15+ and Android API 23+ when their SDK/NDK is selected; phone device acceptance
is separate. Windows native builds are not supported. Browser support targets
current Chromium and WebKit; actual Safari/phone acceptance remains explicit.

## Browser builds

```sh
make web
python3 -m http.server 8080 --bind 127.0.0.1 --protocol HTTP/1.1 --directory web
```

All player assets are relative, so the generated `web/` tree can be served from
an origin root or a directory. It needs no account, database or remote rendering
service. The local preview command uses Python 3.11 or later and persistent
HTTP connections. See [Python's server options](https://docs.python.org/3/library/http.server.html#command-line-interface).
Use localhost for development and HTTPS for service-worker offline use.

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

See [the macOS README](../apps/macos/README.md) and [the bridge README](../bindings/native/README.md)
for toolchains and current supported behavior.
Source builds do not imply signed distribution or store approval.

## Release preparation

The CLI reports `0.1.0` with `harness --version`. Freeze the final public source
before creating `v0.1.0` and stamping kept renderer archives or MCP packages.
Run the release checks against that exact source. Tagging, package publication
and hosted deployment remain explicit release operations.

`make wasm` collects wasi-libc, musl, Zig and LLVM runtime notices from its exact
installed Zig distribution into `build/wasm/notices/`. Browser packaging copies
those texts into new renderer archives and `web/notices/` for the root display
modules. Existing archives keep their immutable bytes. An explicitly supplied
`--notices-dir` must contain notices matching the archived build's toolchain.

The prepared GitHub workflow runs native regression, CLI/documented-example
checks, complete bridge linking and browser package lifecycle tests on Ubuntu
and macOS. Native regression reports optional targets not available on each
runner. Browser interaction and signed macOS distribution are not established by
that workflow. Its hosted execution remains a release acceptance step.

`make clean` removes build outputs. Generated browser files and archived renderers
are retained deliberately; a kept archive cannot always be reconstructed from
current source. Remove only known disposable generated assets when resetting a
local browser build.

After `make wasm`, `node --expose-gc tests/browser_pcm.mjs` compares native and
WASI PCM/canonical output, including normalized input/output collision refusals.
