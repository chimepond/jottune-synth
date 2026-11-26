<!-- Copyright (c) 2026 Nenad Micic; SPDX-License-Identifier: Apache-2.0 -->
# JotTune Synth

JotTune Synth 0.1.0 beta is a deterministic integer synthesizer with a readable song format.
One complete `.tl` file contains the sounds, notes, effects and spatial controls
needed to reproduce a record. The same engine runs in the command-line tools,
the browser player and the macOS application.

The instrument combines FM/PM, additive and subtractive techniques, wavetables,
physical-model voices, seeded generators and spatial effects. Agents can compose,
inspect, revise and render through the documented tools; people can play files,
inspect the piano roll and fine-tune sounds in the native instrument.

## Build and play

Use a C99 compiler, Make and Python 3.10 or later:

```sh
make
build/s12e10p1v1/harness --version
build/s12e10p1v1/harness --play examples/bell.tl --wav build/bell.wav
make quick
```

To render a piece directory or several TL files to a new WAV, use
`python3 tools/fm.py tl2wav IN -o OUT.wav`. It and the command-line-only
approximate `wav2tl` draft tool are described in [DECODING.md](docs/DECODING.md).

`--play` verifies the saved render and block independence. `--play-once` prepares
one render for listening without claiming block independence. Host-side tools
may use floating point; the five synthesis modules use integer arithmetic.

## Browser

Install Zig with a WASI toolchain, then build and serve the static player.
The local preview command uses Python 3.11 or later:

```sh
make web
python3 -m http.server 8080 --bind 127.0.0.1 --protocol HTTP/1.1 --directory web
```

Open `http://localhost:8080/`. Loading is silent; press Play to listen. The player
supports local files, listening profiles, live views, analysis, TL/WAV/SVG export
and portable song links. Rendering runs on the listener's device.

The included Favorites contain eight completed songs and their saved versions.
The player defaults to Keys ±30° where available, otherwise Original. To browse
the full separate song collection, select its checkout explicitly:

```sh
make songs TUNES_ROOT=../tunes SONG_CATALOG=../tunes/library.json
```

## macOS

On Apple Silicon with macOS 14 or later and Xcode command-line tools:

```sh
make -C apps/macos run
make -C apps/macos standalone
```

The native application retains imported TL files locally, plays full records,
shows live views and provides an editable instrument. Distribution signing and
notarization are separate from building the source.

## Documentation

- [Instrument and authoring guide](docs/INSTRUMENT.md)
- [Native and browser APIs](docs/API.md)
- [Build and validation](docs/BUILD.md)
- [MIDI, WAV and SFZ interchange](docs/INTERCHANGE.md)
- [Render and decode: tl2wav, wav2tl and MIDI import](docs/DECODING.md)
- [Shared native bridge](bindings/native/README.md)

The [JotTune MCP repository](https://github.com/chimepond/jottune-mcp) provides
the local interface for agent clients. Start with the [included songs](examples/)
or listen to the wider collection on [ChimePond](https://chimepond.com).
The standalone player also works offline.

See [security and rights reporting](SECURITY.md) and [code contributions](CONTRIBUTING.md).

## Project and contact

JotTune and ChimePond are part of Nenad Micic’s [independent software and digital hobby project](https://www.nmic.app/).
Visit the [JotTune project site](https://jottune.app/).
For help, email [support@chimepond.com](mailto:support@chimepond.com).
To contact Nenad about the project, email [chimepond@nmic.app](mailto:chimepond@nmic.app). Find ChimePond on
[GitHub](https://github.com/chimepond) and [Hugging Face](https://huggingface.co/chimepond).

## Licence

JotTune Synth © 2026 Nenad Micic.

Code and included examples are Apache-2.0 unless a file states otherwise.
See [NOTICE](NOTICE) for adapted third-party tables. Third-party components
retain their own notices, including the Michroma font's
SIL Open Font License. Separately submitted music retains its declared terms.

JotTune Synth was developed with AI assistance.

## Portable song metadata

A song can carry a bounded, versioned metadata header in the same readable TL
file. Existing headerless records remain supported. The musical body determines
the song ID; changing descriptive metadata changes the document checksum without
creating another song. Both current compatibility counters are 1. Newer required
TL or engine versions are refused, alongside the existing exact engine and saved
audio checks. See [the portable TL format](docs/TL-FORMAT.md).

```sh
python3 -I -B tools/tl_document.py validate song.tl
python3 -I -B tools/tl_document.py pack body.tl --metadata song.json --out portable.tl
```

The metadata JSON is optional input while authoring; the resulting portable TL
needs no sidecar to play or retain those declarations. Score JSON accepts the
same optional `metadata` object. `--merged` exports a complete document; the
explicit `--merged-body` option exports native music only. Publication consent,
moderation and withdrawal authority remain separate platform records.
