<!-- Copyright (c) 2026 Nenad Micic; SPDX-License-Identifier: Apache-2.0 -->
# JotTune host tools

`fm.py` provides composition, guarded revision, native rendering, inspection,
measurement, artifact delivery and session recovery. The host tools use the same
TL reader and renderer as the native player. The detailed operation contract is
in [the instrument guide](../docs/INSTRUMENT.md).

`interchange.py` inspects and exports native WAV, MIDI and sampled SFZ bundles.
`music/compose.py` contains reusable patch, recipe and performance helpers;
`music/midi2tl.py` and `music/dx7_import.py` import supported external formats.
`music/analyze.py` supplies optional NumPy measurements and Matplotlib plots.

`build_wasm.py`, `build_spatial.py`, `build_analysis.py` and `package_web.py`
produce the static browser player from this checkout. `export_library.py`
validates and packages an explicitly selected catalog of complete TL songs.

The MCP transport and npm launcher are maintained in `chimepond/jottune-mcp`.

## Batch export

Run these commands from the synth checkout on Linux or macOS. The default
source is the public `chimepond/tunes` Git repository. You can also pass an
HTTPS Git URL or a local directory containing TL files.

```sh
python3 tools/render_tunes.py -o out/mp3
python3 tools/render_tunes.py ../tunes -o out/mp3 --piano-roll
python3 tools/render_tunes.py ../tunes -o out/wav --format wav -j 4
```

Audio export requires Python 3.10+, Make and a C99 compiler. The script builds
the native harness from this checkout. Supply `--harness PATH` to use an
existing build. Git is needed for a remote source. MP3 uses LAME V2 or FFmpeg
with libmp3lame. WAV preserves the source sample rate and channels.

With `--piano-roll`, each audio file gets a matching 1200 × 1200 PNG. The image
shows the song title, collection and saved variant alongside the piano roll.
The cursor sits inside a note near 38% of the piece when a note is available.
Install Node.js 22 or later, then set up the optional browser from the checkout:

```sh
npm install --no-save --package-lock=false playwright
npx playwright install chromium
```

On a minimal Linux system, `npx playwright install --with-deps chromium`
also installs the browser's OS libraries. To use an existing installation,
set `PLAYWRIGHT_MODULE` to its Playwright package path and optionally
`CHROME_BIN` to a Chromium or Chrome executable. The image renderer uses
the local native events and the player's drawing code. It works offline with
a local tunes directory.

Catalog tracks use `song-id--variant` filenames. Other TL files keep their
relative paths. Each output folder has a `manifest.json` with the source and
output hashes. Running the same command again verifies and reuses completed
files. You can add `--piano-roll` to an earlier audio export. Use separate
output directories for MP3 and WAV, and a fresh directory for a changed
timeline or encoder. Run `python3 tests/render_tunes.py --piano-roll` to
check audio, artwork and resume behavior with a small local fixture.
