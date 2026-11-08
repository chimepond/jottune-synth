# Changelog

## Unreleased

- `tools/package_web.py` packages a kept release only with `--repository`, the
  https URL of the public source repository, and only when the source commit is
  in a remote-tracking branch (`--public-ref`, default `origin/main`) of a remote
  that fetches from that repository. The entry records the repository. The check
  reads the local repository only; fetch first.
- `tools/build_wasm.py` writes the wasi-libc and compiler-rt notices from the
  Zig distribution that built the module, including the cloudlibc and musl texts.
- Text files check out with LF endings on every platform; TL files keep their
  exact bytes. `tests/repo_hygiene.py` checks both.
- Six source comments no longer cite a design note that is not in the repository.
- `fm.py tl2wav IN -o OUT.wav` renders a TL file, several TL files or a piece
  directory to a new WAV with the same bytes as `harness --play --wav`. It never
  overwrites, refuses a saved-hash mismatch, and refuses a silent render (such as
  a piece's `session.tl` alone) unless `--allow-silent` is given.
- `fm.py wav2tl IN.wav -o OUT_DIR` is a command-line alias of `transcribe` that
  drafts an approximate mono piece. It widens the recipe shortlist to 8 with a
  fallback to 1, prints a draft banner with the measured SNR, writes a
  `provenance.json` sidecar (source sha256, basename, tool and engine), and with
  `--strict` exits 4 without publishing when the draft has warnings.
- Transcription checks WAV headers before reading samples: truncated headers,
  chunk sizes beyond the file and odd chunks without a pad byte are refused with
  exit 2 and a message, a new `--max-seconds` cap (default 300, at most 1200)
  refuses longer sources, and unpooled runs with more than 32 regions are refused
  before rendering. Killed runs' staging directories are swept on the next run
  to the same output, and publish lock files are removed after use.
- New `docs/DECODING.md` documents both directions, their options, exit codes,
  input caps and provenance sidecar. The audio transcription and MIDI import
  sections moved there from `docs/INSTRUMENT.md`, so the web player's hosted
  instrument guide no longer carries them; `wav2tl` stays a command-line tool.
- macOS app: Records has Export WAV… next to Export TL…. It writes the open
  record's WAV through the native take-companion writer, byte-identical to
  `harness --play --wav` for the record's files (checked by `make uitest`). The
  app does not transcribe WAV to TL.
- The library exporter, the web player and the agent client accept the provenance
  that the tunes repository writes for contributed songs: `made_by` (`agent`,
  `human` or `mixed`), alone or with the `model` and `model_source` pair.
  `discover --examples` also accepts `creator-declared` genre and tempo labels.
  Before, the first contributed song made the exporter and the player refuse the
  whole catalog, and `discover --examples` refused it at the first contributed
  song with a genre or tempo.
- `tools/fm.py` declares `API_VERSION = 1`, which `jottune-mcp doctor` reports and checks against
  the versions that server supports.

## 0.1.0 beta — prepared, not published

- Preserve eight Favorites and prefer saved Keys ±30° when selecting a song.
- Integer synthesis, generators, effects and spatial rendering.
- Standalone TL import, canonical export and deterministic replay.
- Command-line composition, inspection, analysis and MIDI/WAV/SFZ interchange.
- Static browser player with local rendering, profiles and live views.
- macOS player, local library and editable instrument.
- Shared C bridge for native applications.
