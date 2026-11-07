<!-- Copyright (c) 2026 Nenad Micic; SPDX-License-Identifier: Apache-2.0 -->
# JotTune Synth for macOS

The native player requires Apple Silicon, macOS 14 or later, Xcode command-line
tools, GNU Make and Python 3.10+ for build-time recipe expansion. Run from the repository root:

```sh
make -C apps/macos all
make -C apps/macos run
apps/macos/tlplay examples/bell.tl
```

Records imports one complete TL, retains it in a local collection, prepares audio
silently and offers playback, seeking, live views and export. Finder opening and
drag/drop follow the same import path. Removing a retained tune removes the app's
copy. Instrument provides sound editing, recipes, keyboard/MIDI input, apply/reset
and take recording.

Full-record rendering supports generator banks. Live instrument headers currently
refuse generator banks. Raw ambisonic coefficients remain available for inspection
and export; listening requires a supported stereo or binaural derivation.

## Standalone application

```sh
make -C apps/macos standalone
make -C apps/macos standalone-check
open 'apps/macos/build/JotTune Synth.app'
```

The standalone bundle includes recipes and licences and needs no Python runtime
or source checkout. The development build uses an ad-hoc signature. Configure
release identity, Developer ID signing and notarization before distributing a
signed application. Source builds do not establish distribution acceptance.

## Validation

```sh
make -C apps/macos verify
make -C apps/macos uitest
```

The UI checks launch the built app in offline mode; macOS may register its bundle
and create preferences. Build/test outputs are written below apps/macos/build.
The automated checks compare native output, canonical records, recipes and model
behavior. Device acceptance additionally covers audible playback, import/export,
MIDI, route changes, interruptions and VoiceOver. The app is a local file player;
online sharing and accounts belong to the separate ChimePond platform.
