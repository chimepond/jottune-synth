#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Render every .tl in a ChimePond tunes checkout or HTTPS Git repository."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import wave


TUNES = "https://github.com/chimepond/tunes.git"
SYNTH = "https://github.com/chimepond/jottune-synth.git"
RELEASE = "878cb40d6ee903cfa42e325258577709743148db"  # v0.1.0
MP3_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)


def run(*args, env=None):
    result = subprocess.run(args, capture_output=True, text=True, errors="replace", env=env)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or
                           f"{args[0]} exited with status {result.returncode}")
    return result.stdout.strip()


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def publish(staged, target, sha, replace_sha=None):
    """Install a complete file, or adopt it after an interrupted manifest update."""
    try:
        os.link(staged, target)
    except FileExistsError:
        if target.resolve() != target or not target.is_file():
            raise ValueError(f"Expected a regular output file: {target}")
        existing = digest(target)
        if existing == sha:
            return
        if existing != replace_sha:
            raise ValueError(f"Refusing to overwrite existing file: {target}")
        os.replace(staged, target)


def snapshot(source, target, sha):
    shutil.copyfile(source, target)
    if digest(target) != sha:
        raise ValueError("Timeline changed during export")


def get_source(source, scratch):
    if source.startswith("https://"):
        print(f"Fetching {source}", flush=True)
        checkout = scratch / "tunes"
        run("git", "clone", "--depth", "1", "--", source, checkout)
        return checkout
    checkout = Path(source).expanduser().resolve()
    if not checkout.is_dir():
        raise ValueError(f"Expected a local directory or HTTPS Git URL: {source}")
    return checkout


def get_renderer(harness, scratch):
    if harness:
        return harness.expanduser().resolve(strict=True)
    checkout = Path(__file__).resolve().parents[1]
    binary = Path("build/s12e10p1v1/harness")
    if (checkout / "src/host/harness.c").is_file() and (checkout / "Makefile").is_file():
        run("make", "-s", "-j2", "-C", checkout, "all")
        return checkout / binary
    print("Building JotTune Synth v0.1.0", flush=True)
    synth = scratch / "synth"
    run("git", "clone", "--depth", "1", "--branch", "v0.1.0", "--", SYNTH, synth)
    if run("git", "-C", synth, "rev-parse", "HEAD") != RELEASE:
        raise ValueError("The renderer release tag does not match its pinned commit")
    run("make", "-s", "-j2", "-C", synth, "all")
    return synth / binary


@dataclass(frozen=True)
class Track:
    source: Path
    relative: str
    output: Path
    title: str
    collection: str
    document_sha256: str | None
    picture_title: str
    profile: str


def tracks(root, fmt):
    catalog = {}
    library = root / "library.json"
    if library.is_file() and not library.is_symlink():
        for song in json.loads(library.read_text(encoding="utf-8"))["songs"]:
            for variant in song["variants"]:
                name = song["id"] + "--" + variant["kind"]
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
                    raise ValueError(f"Invalid catalog filename: {name!r}")
                for rel in variant["paths"]:
                    if rel in catalog:
                        raise ValueError(f"Multiple catalog entries for {rel}")
                    profile = {"standard": "Standard", "keys": "Keys ±30°",
                               "mirror": "Keys mirror ±30°"}.get(variant["kind"], variant["kind"])
                    catalog[rel] = (
                        name + "." + fmt, f"{song['title']} ({variant['kind']})", song["collection"],
                        variant.get("discovery", {}).get("document_sha256"), song["title"], profile,
                    )
    result = []
    seen = set()
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git" and not (Path(directory) / d).is_symlink()]
        for filename in files:
            if not filename.lower().endswith(".tl"):
                continue
            tl = Path(directory) / filename
            if tl.is_symlink() or not tl.is_file():
                raise ValueError(f"Expected a regular TL file: {tl}")
            relative = tl.relative_to(root)
            name, title, collection, doc_sha, picture_title, profile = catalog.pop(
                relative.as_posix(), (relative.with_suffix("." + fmt), tl.stem, "", None, tl.stem, "Original")
            )
            output = Path(name)
            if output in seen:
                raise ValueError(f"Duplicate output path: {output}")
            seen.add(output)
            result.append(Track(tl, relative.as_posix(), output, title, collection, doc_sha, picture_title, profile))
    if catalog:
        raise ValueError(f"Catalog references missing TL files, including {next(iter(catalog))}")
    if not result:
        raise ValueError(f"No .tl files found under {root}")
    return sorted(result, key=lambda item: item.relative)


def encode(encoder, wav, output, track, rate):
    target_rate = min(MP3_RATES, key=lambda r: abs(r - rate))
    if Path(encoder).name == "lame":
        # LAME converts ID3 text using its C locale, even with --id3v2-utf16.
        env = dict(os.environ, LC_ALL="en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8")
        run(encoder, "--silent", "-V", "2", "--resample", str(target_rate / 1000),
            "--add-id3v2", "--id3v2-utf16", "--tt", track.title, "--tl", track.collection,
            wav, output, env=env)
    else:
        run(encoder, "-nostdin", "-v", "error", "-i", wav, "-map", "0:a:0",
            "-c:a", "libmp3lame", "-q:a", "2", "-ar", str(target_rate),
            "-metadata", f"title={track.title}", "-metadata", f"album={track.collection}", output)
    return target_rate


def render(track, out, renderer, encoder, recipe, previous):
    target = out / track.output
    key = track.output.as_posix()
    if target.resolve() != target:
        raise ValueError(f"Output path crosses a symbolic link: {key}")
    source_hash = digest(track.source)
    if track.document_sha256 is not None and source_hash != track.document_sha256:
        raise ValueError("TL bytes differ from the catalog document_sha256")
    old = previous.get(key)
    refresh_tags = False
    if old and (old["source_sha256"] != source_hash or old["recipe"] != recipe):
        raise ValueError(f"{key} has a different source or recipe; choose a new output folder")
    if target.exists() and old:
        same_tags = old["title"] == track.title and old.get("collection") == track.collection
        if digest(target) == old["output_sha256"]:
            if not encoder or same_tags:
                return key, dict(old, source=track.relative, title=track.title,
                                 collection=track.collection), "reused"
            # Legacy MP3 manifests omit the collection. Re-encode to verify both tags.
            refresh_tags = True
        elif not encoder or same_tags:
            raise ValueError(f"{key} differs from its manifest; refusing to overwrite it")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".render-", dir=target.parent) as name:
        wav = Path(name) / "track.wav"
        tl = Path(name) / "input.tl"
        snapshot(track.source, tl, source_hash)
        report = run(renderer, "--play-once", "--wav", wav, "--", tl)
        line = next((s for s in report.splitlines() if s.startswith("PLAY ")), "")
        fields = dict(pair.split("=", 1) for pair in line.split()[1:] if "=" in pair)
        if fields.get("match") not in ("yes", "-"):
            raise RuntimeError(f"Unexpected native render report: {report}")
        with wave.open(str(wav), "rb") as reader:
            channels, width, rate, frames = reader.getparams()[:4]
        # The native CLI only prints channels= for multichannel records.
        native_shape = (int(fields.get("channels", 1)), int(fields["fs"]), int(fields["samples"]))
        if width != 2 or (channels, rate, frames) != native_shape:
            raise ValueError("Rendered WAV disagrees with the native report")
        output, output_rate = wav, rate
        if encoder:
            if channels > 2:
                raise ValueError(f"Use --format wav to preserve this {channels}-channel timeline")
            output = Path(name) / "track.mp3"
            output_rate = encode(encoder, wav, output, track, rate)
        output_hash = digest(output)
        publish(output, target, output_hash, old["output_sha256"] if refresh_tags else None)
        return key, dict(source=track.relative, title=track.title, collection=track.collection,
                         source_sha256=source_hash,
                         pcm_hash=fields["hash"], saved_hash_match=fields["match"],
                         source_frames=frames, source_rate=rate, channels=channels,
                         output_rate=output_rate, output_sha256=output_hash, recipe=recipe), "wrote"


def capture_roll(worker, track, entry, out, image, labels):
    target = out / image
    with tempfile.TemporaryDirectory(prefix=".roll-", dir=target.parent) as folder:
        tl, staged = Path(folder) / "input.tl", Path(folder) / "roll.png"
        snapshot(track.source, tl, entry["source_sha256"])
        request = dict(source=str(tl), output=str(staged), **labels)
        try:
            worker.stdin.write(json.dumps(request) + "\n")
            worker.stdin.flush()
        except BrokenPipeError as error:
            raise RuntimeError("Piano-roll renderer stopped") from error
        line = worker.stdout.readline()
        if not line:
            raise RuntimeError("Piano-roll renderer stopped before returning a result")
        response = json.loads(line)
        if "error" in response:
            raise ValueError(response["error"])
        sha = digest(staged)
        info = dict(path=image.as_posix(), sha256=sha, labels=labels,
                    source_sha256=entry["source_sha256"], bars=response["bars"],
                    playhead_seconds=response["playhead_seconds"])
        publish(staged, target, sha, entry.get("piano_roll", {}).get("sha256"))
        entry["piano_roll"] = info


def piano_rolls(batch, out, renderer, records):
    pending = []
    failed = reused = 0
    for track in batch:
        key = track.output.as_posix()
        if key not in records:
            continue
        entry = records[key]
        image = track.output.with_suffix(".png")
        target = out / image
        labels = dict(title=track.picture_title, collection=track.collection, profile=track.profile)
        old = entry.get("piano_roll", {})
        try:
            if target.resolve() != target:
                raise ValueError("Output path crosses a symbolic link")
            if (old.get("labels") == labels and old.get("source_sha256") == entry["source_sha256"]
                    and target.is_file() and digest(target) == old["sha256"]):
                reused += 1
                continue
            pending.append((track, entry, image, labels))
        except (OSError, KeyError, ValueError) as error:
            print(f"FAILED {image}: {error}", file=sys.stderr, flush=True)
            failed += 1

    written = processed = 0
    if pending:
        helper = Path(__file__).with_name("piano_roll.mjs")
        try:
            with subprocess.Popen(
                ["node", helper, renderer], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, encoding="utf-8",
            ) as worker:
                for track, entry, image, labels in pending:
                    try:
                        capture_roll(worker, track, entry, out, image, labels)
                        written += 1
                    except (OSError, KeyError, ValueError) as error:
                        print(f"FAILED {image}: {error}", file=sys.stderr, flush=True)
                        failed += 1
                    processed += 1
                    if processed % 25 == 0:
                        print(f"{processed}/{len(pending)} images processed", flush=True)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Image export failed: {error}", file=sys.stderr, flush=True)
            failed += len(pending) - processed
    print(f"Piano rolls: {written} exported, {reused} reused, {failed} failed", flush=True)
    return failed


def save_manifest(report, manifest):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=manifest.parent, delete=False) as stream:
        json.dump(report, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")
    os.replace(stream.name, manifest)


def export(args, scratch, out):
    root = get_source(args.source, scratch)
    if out == root or root.is_relative_to(out):
        raise ValueError("Choose an output folder separate from the input tree")
    batch = tracks(root, args.format)
    encoder = None
    if args.format == "mp3":
        encoder = shutil.which("lame") or shutil.which("ffmpeg")
        if not encoder:
            raise ValueError("Install lame or FFmpeg with libmp3lame for MP3 output")
    renderer = get_renderer(args.harness, scratch)
    recipe = digest(renderer) + ":" + (digest(Path(encoder)) + ":vbr2" if encoder else "pcm16")
    source_commit = run("git", "-C", root, "rev-parse", "HEAD") if (root / ".git").exists() else None
    print(f"{len(batch)} timelines | {args.format.upper()} | {args.jobs} workers | {out}", flush=True)
    if source_commit:
        print(f"Source commit {source_commit}", flush=True)
    manifest = out / "manifest.json"
    if manifest.is_symlink():
        raise ValueError("Expected a regular manifest file")
    previous = {}
    if manifest.exists():
        prior = json.loads(manifest.read_text(encoding="utf-8"))
        previous = {**prior.get("last_verified", {}), **prior["tracks"]}
        if prior["format"] != args.format or previous.keys() - {t.output.as_posix() for t in batch}:
            raise ValueError("Output folder contains a different format or removed tracks; choose a new folder")
    records = {}
    failed = reused = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        pending = {pool.submit(render, t, out, renderer, encoder, recipe, previous): t for t in batch}
        for count, future in enumerate(as_completed(pending), 1):
            try:
                key, entry, status = future.result()
                if "piano_roll" in previous.get(key, {}):
                    entry["piano_roll"] = previous[key]["piano_roll"]
                records[key] = entry
                reused += status == "reused"
            except (OSError, RuntimeError, ValueError, KeyError, wave.Error) as error:
                failed += 1
                print(f"FAILED {pending[future].relative}: {error}", file=sys.stderr, flush=True)
            if count % 25 == 0 or count == len(batch):
                print(f"{count}/{len(batch)} processed; {failed} failed", flush=True)
    report = {"source": args.source, "source_commit": source_commit, "renderer": run(renderer, "--version"),
              "format": args.format, "inputs": len(batch), "failed": failed, "tracks": records}
    # Keep the last known digests across a failed rerun, without listing the
    # affected tracks as successful outputs of the current source revision.
    if failed:
        report["last_verified"] = {key: previous[key] for key in previous.keys() - records.keys()}
    print(f"{len(batch)-reused-failed} exported, {reused} reused, {failed} failed")
    save_manifest(report, manifest)
    if args.piano_roll:
        report["failed"] += piano_rolls(batch, out, renderer, records)
        save_manifest(report, manifest)
    return int(bool(report["failed"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""Examples:
  python3 render_tunes.py -o songs
  python3 render_tunes.py https://github.com/chimepond/tunes/ -o songs -j 4
  python3 render_tunes.py ./tunes --format wav -o wav
  python3 render_tunes.py ./tunes --piano-roll -o songs

Linux/macOS requirements: Python 3.10+, Git, Make and a C99 compiler.
MP3 uses LAME V2 or FFmpeg with libmp3lame. WAV preserves native PCM.
See tools/README.md for Node.js, Playwright and Chromium setup for PNGs.
Uses this checkout's renderer, or builds the pinned release when run standalone.
Use --harness to supply an existing binary. Reruns verify existing audio and
refresh changed catalog tags. Use a new folder for changed TL audio, removed
tracks, different renderer/encoder settings or a different format.""")
    parser.add_argument("source", nargs="?", default=TUNES, help="tunes repository URL or local directory")
    parser.add_argument("-o", "--out", type=Path, help="output folder (default: chimepond-mp3 or chimepond-wav)")
    parser.add_argument("--format", choices=["mp3", "wav"], default="mp3")
    parser.add_argument("--piano-roll", action="store_true", help="also write a 1200 × 1200 piano-roll PNG per track")
    parser.add_argument("-j", "--jobs", type=int, default=2, help="parallel tracks (default: 2)")
    parser.add_argument("--harness", type=Path, help="existing JotTune Synth harness")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    out = (args.out or Path("chimepond-" + args.format)).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"Another export is using {out}")
        with tempfile.TemporaryDirectory(prefix="chimepond-tunes-") as scratch:
            return export(args, Path(scratch), out)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        sys.exit(f"render_tunes: {exc}")
