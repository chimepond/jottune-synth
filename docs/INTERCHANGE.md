<!-- Copyright (c) 2026 Nenad Micic -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Export a song or sampled instrument

TL remains the complete editable original. Interchange exports use the native
reader and renderer; a saved-hash mismatch refuses conversion. No MIDI is sent,
no software is installed, and no song is uploaded. The tools use Python's
standard library and the built native renderer.

Run `make`, then inspect a complete song:

```sh
python3 tools/interchange.py inspect tests/golden/fx/crush.tl
```

The result lists candidate formats. A candidate still needs its actual dry run:
MIDI needs a channel map, and sampled instruments need an explicit patch and
sampling budget. Add `--harness PATH` before the command to select another built
renderer. An input without a saved audio hash is reported as unpinned.

| Profile | Preserves | Does not preserve |
| --- | --- | --- |
| `wav` | Native PCM16 audio, source sample rate and mono/stereo channels | Editable notes or synth settings inside the WAV |
| `midi` | Approximate explicit note performance, with timing/pitch/velocity errors reported | TL synthesis, effects, spatial movement, programs or original tempo/meter |
| `sfz` | One-key trigger for the entire native rendered song, including its recorded effects | A freely playable synth patch or adjustable note duration |
| `sfz-bank` | Explicit note/velocity samples of one native mono patch | Source performance, other voices, session effects, spatial context, loops or editable synthesis |

No output is written without `--out`. By default, output is a new directory.
Use `--container zip` to keep the complete bundle in one file. Existing outputs
are refused. Every bundle retains `source.tl`, format assets, `README.txt` and
`manifest.json`; the manifest contains the conversion report and file identities.
Keep those together. A ZIP must be unpacked before opening its `instrument.sfz`
in a sampler; copying that SFZ alone loses its WAV assets.

```sh
# Dry-run a whole-song trigger on MIDI key 60.
python3 tools/interchange.py sfz tests/golden/48000/bell.tl --key 60

# Save one portable file, preserving the exact native audio.
python3 tools/interchange.py sfz tests/golden/48000/bell.tl --key 60 \
  --container zip --out out/bell-song.zip

# Verify a ZIP or an unpacked bundle without rendering or extracting files.
python3 tools/interchange.py verify out/bell-song.zip

# Sample a small grid; the explicit loss acceptance is required for saving it.
python3 tools/interchange.py sfz-bank tests/golden/48000/bell.tl \
  --patch bell --notes 60 72 --velocities 63 127 \
  --hold-ms 200 --tail-ms 300 --max-samples 4 --accept-loss \
  --container zip --out out/bell-instrument.zip
```

Sample grids map only the chosen keys. Intermediate velocities choose the
nearest sampled layer, with ties going to the lower layer. Each trigger plays
the recorded hold and tail; no sustain loop or release articulation is inferred.
An explicit tail can truncate sound. Each cell includes its own canonical TL
for native replay. Grids allow at most 256 cells and 64 MiB of retained sample
assets, with a caller-supplied `--max-samples` bound.

MIDI requires a distinct, explicitly selected channel for every active voice.
Use channels 1–16; no drum channel, patch program or channel reuse is guessed.

```sh
python3 tools/interchange.py midi tests/golden/fx/crush.tl \
  --channel 0=1 --channel 1=2
python3 tools/interchange.py midi tests/golden/fx/crush.tl \
  --channel 0=1 --channel 1=2 --accept-loss --out out/crush-midi
```

The defaults are MIDI type 1, PPQN 960, tempo 500,000 microseconds per quarter
note and a two-semitone pitch-bend range. `--midi-format 0` selects one track.
The target must honor the pitch-bend range/RPN messages. Generators and unknown
native schemas refuse MIDI and sampled-patch export rather than silently losing
unclassified behavior. Known FX, including crusher automation, appear in the
loss report. Raw field audio requires an explicit native stereo/binaural render
before portable WAV or whole-song SFZ export; no implicit downmix occurs.

`--receiver-profile sfizz-ram` requests full sample loading with the optional
`hint_ram_based=1` extension. Other receivers may ignore it; it does not establish
a receiver memory bound. Receiver playback, gain and opcode handling still need
checking in each target player; the specific automated receivers below have
been exercised. Bundle verification checks complete members,
declared sizes/hashes and SFZ sample mappings; it does not certify external sound.
An independently retained `--expected-manifest-sha256` anchors the manifest.
ZIP verification rejects duplicate, unsafe, symlinked, encrypted, damaged or
undeclared members. Directory verification retains its declared-member scope;
unlisted local notes are outside that verification.

DX7 export is unavailable: importing a preset does not preserve every original
word needed for an exact inverse. No hardware-preset or plug-in export is implied.

## Compare a round trip and actual receiver playback

`tests/receiver_roundtrip.py` tests the translation through an independent
receiver, rather than treating a valid container as proof of working playback.
It generates short controls and removes temporary audio on exit. No download,
installation, device playback or new runtime dependency is automatic.

```sh
# Four native TL -> MIDI type 0/1 -> TL -> WAV comparisons; standard library only.
python3 tests/receiver_roundtrip.py --harness build/s12e10p1v1/harness

# Also render through explicitly installed open-source receivers.
python3 tests/receiver_roundtrip.py --harness build/s12e10p1v1/harness \
  --sfizz-render /path/to/sfizz_render \
  --fluidsynth /path/to/fluidsynth --soundfont /path/to/instrument.sf2

# Optional existing mono WAV -> inferred TL -> WAV; requires NumPy.
python3 tests/receiver_roundtrip.py --harness build/s12e10p1v1/harness --transcribe
```

Each receiver option is independent; FluidSynth requires its SoundFont. An
omitted receiver is reported as **not run**. No hardware or listening verdict
is implied. The importer uses the same explicitly selected bell recipe, one
voice, RPN processing and a 200 ms tail; it performs no automatic level fitting.
Source labels are never supplied to the optional audio decoder: only the WAV
and the declared `marimba`/`organ` candidate list.

Current checked combinations and measured limitations:

| Route | Checked behavior | Observed difference |
| --- | --- | --- |
| TL -> MIDI -> TL, four cases | Same event counts/order, type 0/1 identical returned audio, exact native replay; mutated notes/timing/pitch/velocity are detected | Plain notes: zero timing/pitch difference and 1 Q15 velocity step. Fractional timing/bends: at most 0.133488 ms and 0.010489 cents in these controls |
| SFZ -> sfizz 1.2.3, 29 renders on Linux | Actual whole stereo song and sample-grid playback, velocity boundaries, one-shot key-up, relocation, MIDI 0/1; missing samples and changed opcodes alter playback and fail bundle verification | Explicit `sfizz-ram` profile. Receiver gain/pan/PCM conversion differs from the stored native WAV; no native PCM equality claim |
| MIDI -> FluidSynth 2.3.4 / TimGM6mb, four Linux renders | Types 0/1 produce identical audio; silence and octave changes produce expected different outputs | A generic SoundFont replaces the native timbre. Raw waveform similarity cannot certify the preserved musical performance |
| Mono TL -> WAV -> inferred TL -> WAV, two profiles at 48 kHz | Two separated notes recover both candidate recipes; each reconstructed TL replays exactly to its own WAV | Existing alignment/hold fitting improves unaligned whole-record SNR from 15.16 to 38.66 dB; largest estimated key-up error drops from 42.67 to 0.271 ms. This does not establish polyphonic/FX or arbitrary-patch inversion |

Waveform reports show both common-duration and zero-padded whole-record errors,
without hidden alignment, normalization or resampling. The plain MIDI control
has 84.25 dB common-window SNR, but 16.56 dB across the whole record because the
importer's additional 200 ms contains release audio. Small timing/pitch changes
can cause large phase-sensitive PCM errors; these numbers are measurements,
not listening scores or an adopted fidelity threshold.

SFZ sample controls additionally report a **separate** bounded lag/gain fit.
On the tested pair, excluding the first 64 frames, a one-frame lag, gain about
0.265688 and measured silence bias -1 leave 0.287–0.289 PCM16 RMS residual.
The unmodified comparison remains in the report; this is a hypothesis about
these samples, not a general playback correction. The sfizz RAM hint can print
an unrelated `hint_stealing` warning, retained in diagnostics. Generic streaming
and other sfizz versions need separate checks.

The optional players' upstream instructions are [sfizz rendering](https://github.com/sfztools/sfizz)
and [FluidSynth fast rendering](https://www.fluidsynth.org/wiki/UserManual/).
These test tools and SoundFonts are external dependencies, not bundled synth
features. Retained `source.tl` in an SFZ bundle is the original, not an inverse
recovered from audio. Compare note semantics as well as waveforms, stating which
musical information each intermediate format can preserve.
