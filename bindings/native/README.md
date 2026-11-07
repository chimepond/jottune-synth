<!-- Copyright (c) 2026 Nenad Micic; SPDX-License-Identifier: Apache-2.0 -->
# Native host bridge

`jottune_bridge.h` exposes the `fms_*` interface for complete TL records and live
instrument controls. The bridge uses the production reader and all five engine
modules. Host allocation, file access and diagnostics stay outside the integer
synthesis library.

- Import complete bytes with an explicit length. Opening validates syntax and
  dimensions; checked rendering also verifies a saved PCM hash when present.
- One worker owns a player. Cancellation is atomic; cancel and join before
  replacing a player or freeing its buffers.
- Render interleaved PCM16 at the TL source rate. Device resampling occurs after
  rendering and is excluded from the saved PCM hash.
- Export canonical TL only after a complete successful render. Size queries
  include the terminating NUL; insufficient buffers remain untouched.
- Keys transformations derive a new player from the original. They do not
  accumulate and preserve notes, timing and effects.
- Preserve every raw ambisonic coefficient. Speaker playback needs a real
  stereo/binaural derivation rather than duplicating a coefficient channel.

The `fms_*` names and C structure contract are shared by desktop and mobile
clients. Consult the header for exact types, ownership and status values.

## Build

```sh
make -C bindings/native check link-smoke
```

This builds `build/native/libjottune_native.a`. Table generators run with HOSTCC;
engine and bridge sources compile with the target compiler. For Apple targets:

```sh
make -C bindings/native SDK=iphoneos ARCH=arm64
make -C bindings/native SDK=iphonesimulator ARCH=arm64
```

Android builds supply the NDK compiler and target flags explicitly. Platform file
pickers, audio sessions and document permissions are responsibilities of the
client. Complete record playback does not imply every live instrument control is
implemented in every client.
