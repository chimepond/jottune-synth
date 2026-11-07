#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Offline, explicit synth-interchange profiles.

Authored timelines are accepted by the native renderer and inventoried by
fm.Metadata. This module neither parses authored TL nor sends MIDI to a device.
"""
from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import functools
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import wave
import zipfile
import zlib

import fm


def public(function):
    wrapped = fm.public(function)

    @functools.wraps(function)
    def call(*args, **kwargs):
        if any(key in kwargs for key in ("receipt_dir", "receipt_parent", "receipt_purpose")):
            return dict(version=1, ok=False, exit=2,
                        message="Interchange uses its bundle manifest; operation receipts are not integrated.")
        return wrapped(*args, **kwargs)
    return call


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True,
                       allow_nan=False) + "\n").encode("utf-8")


def accepted_source(source, harness=None, wav=False, *, native=None):
    native = native if native is not None else fm.Native(harness)
    result = fm.resolve_source(source, native, wav=wav)
    if result["exit"] != 0:
        raise fm.ToolError("source failed native verification; export refused", result["exit"],
                           native=native.identity, hash=result.get("hash"),
                           checks=result["checks"])
    return result


def source_identity(accepted):
    result = {k: accepted.get(k) for k in
              ("native", "record_sha256", "hash", "expected", "match", "block_indep",
               "fs", "channels", "samples", "clipped")}
    if "source_files" in accepted:
        result["source_files"] = accepted["source_files"]
        result["absent_sibling_streams"] = accepted["absent_sibling_streams"]
    result["completeness"] = ("expected audio hash matched" if accepted.get("expected") is not None
                               else "unpinned input; native acceptance does not establish completeness")
    return result


def inventory(accepted):
    described = fm.describe_rows(accepted)
    counts = Counter(r["id"] for r in described["events"])
    gen = [r["id"] for r in accepted["_rows"] if r["section"] == "generator"]
    return dict(voices=described["voices"], patches=sorted(described["patches"]),
                events=dict(sorted(counts.items())),
                patch_statements=sorted({r["id"] for p in described["patches"].values() for r in p}),
                fx_statements=sorted({r["id"] for r in described["fx"] or []}),
                spatial_statements=sorted({r["id"] for r in accepted["_rows"] if r["section"] == "space"}),
                generator_statements=sorted(set(gen)))


@public
def inspect(source, *, harness=None):
    accepted = accepted_source(source, harness)
    data = inventory(accepted)
    portable_audio = accepted["channels"] in (1, 2)
    generated = bool(data["generator_statements"])
    return dict(version=1, ok=True, exit=0, operation="interchange_inspect",
                source=source_identity(accepted), inventory=data,
                profiles={
                    "wav": dict(candidate=portable_audio, preserves="native PCM16 bytes",
                                reason=None if portable_audio else "field channels need an explicit receiver/decoder profile"),
                    "midi": dict(candidate=not generated, preserves="approximate explicit note performance",
                                 reason="requires an explicit unique voice/channel map and encoder validation" if not generated
                                 else "generator notes/modulation are not flattened by this profile"),
                    "sfz": dict(candidate=portable_audio, preserves="native PCM16 asset; whole-record one-shot trigger",
                                reason="requires an explicit trigger key; target playback is not certified"),
                    "sfz-bank": dict(candidate=not generated, preserves="sampled mono patch snapshots",
                                     reason="requires explicit patch, keys, velocity grid, hold/tail and render budget; drops session FX/space"),
                    "dx7": dict(candidate=False, reason=" holds the inverse mapping/acceptance contract")},
                original_tempo_meter_programs="not recoverable from canonical TL",
                external_playback_verified=False)


def wav_info(data):
    with wave.open(io.BytesIO(data), "rb") as audio:
        fm.require(audio.getcomptype() == "NONE" and audio.getsampwidth() == 2,
                   "portable profile requires native uncompressed PCM16", 1)
        fm.require(audio.getnchannels() in (1, 2),
                   "portable audio/SFZ refuses field channels; choose an explicit native stereo/binaural render", 1)
        info = dict(channels=audio.getnchannels(), sample_rate=audio.getframerate(),
                    sample_width_bytes=audio.getsampwidth(), frames=audio.getnframes())
        pcm = audio.readframes(audio.getnframes())
        fm.require(len(pcm) == info["frames"] * info["channels"] * 2,
                   "native WAV is truncated", 5)
        info["pcm_sha256"] = fm.sha(pcm)
        return info


def sfz_text(key):
    fm.integer(key, "key")
    fm.require(0 <= key <= 127, "SFZ trigger key must be 0..127", 2)
    return ("// Complete native render, one trigger key, no loop or patch conversion.\n"
            "<region> sample=audio.wav key=" + str(key) + " pitch_keycenter=" + str(key) +
            " amp_veltrack=0 loop_mode=one_shot\n").encode("ascii")


def member_name(name):
    return (isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name))
            and name not in (".", "..", "manifest.json"))


def publish(out, files, report, *, container="directory"):
    """Write one complete local bundle using the existing publisher lock."""
    fm.require(container in ("directory", "zip"), "container must be directory or zip", 2)
    fm.require(isinstance(out, (str, Path)) and str(out).strip(), "out must be a new destination", 2)
    selected = Path(out).expanduser()
    fm.require(not selected.is_symlink(), "output must not be a symlink", 2)
    destination = fm.workspace_path(selected)
    fm.require(all(member_name(name) for name in files), "invalid bundle member", 5)
    manifest = dict(version=1, profile=report["profile"], report=report,
                    artifacts={name: dict(bytes=len(data), sha256=fm.sha(data))
                               for name, data in sorted(files.items())})
    encoded = json_bytes(manifest)
    with fm.session_module().destination_lock(fm, destination):
        fm.require(not destination.exists() and not destination.is_symlink(),
                   "output already exists; select a new destination", 2)
        temporary = Path(tempfile.mkdtemp(prefix=".interchange-", dir=destination.parent))
        try:
            if container == "zip":
                archive = temporary / "bundle.zip"
                with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
                    for name, data in sorted({**files, "manifest.json": encoded}.items()):
                        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.create_system = 3
                        info.external_attr = (stat.S_IFREG | 0o644) << 16
                        info.compress_type = zipfile.ZIP_DEFLATED
                        output.writestr(info, data)
                verify_bundle(archive, expected_manifest_sha256=fm.sha(encoded))
                archive_identity = file_identity(archive)
                # A same-filesystem hard link is atomic and refuses even an
                # uncooperative writer's destination created after our check.
                try:
                    os.link(archive, destination)
                except FileExistsError as error:
                    raise fm.ToolError("output already exists; select a new destination", 2) from error
            else:
                for name, data in files.items():
                    (temporary / name).write_bytes(data)
                (temporary / "manifest.json").write_bytes(encoded)
                verify_bundle(temporary, expected_manifest_sha256=fm.sha(encoded))
                os.rename(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    result = dict(manifest_sha256=fm.sha(encoded), artifacts=manifest["artifacts"])
    if container == "zip":
        result.update(container="zip", archive=str(destination), **archive_identity)
    else:
        result["directory"] = str(destination)
    return result


def stream_identity(stream, expected_size=None):
    digest, size = hashlib.sha256(), 0
    while True:
        limit = 1024 * 1024 if expected_size is None else min(1024 * 1024, expected_size - size + 1)
        block = stream.read(limit)
        if not block:
            break
        size += len(block)
        fm.require(expected_size is None or size <= expected_size, "artifact exceeds declared size", 5)
        digest.update(block)
    return dict(bytes=size, sha256=digest.hexdigest())


def file_identity(path):
    with path.open("rb") as stream:
        return stream_identity(stream)


@contextlib.contextmanager
def bundle_members(path):
    """Open flat files without extracting archive paths or buffering audio."""
    fm.require(not path.is_symlink(), "bundle must not be a symlink", 2)
    if path.is_dir():
        def opened(name):
            member = path / name
            fm.require(not member.is_symlink() and member.is_file(), "missing or symlinked artifact: " + name, 5)
            return member.open("rb")
        yield opened, None, None
        return
    fm.require(path.is_file(), "bundle directory or ZIP missing", 5)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            from sample_export import MAX_CELLS
            entries = archive.infolist()
            fm.require(len(entries) <= 2 * MAX_CELLS + 4, "too many ZIP members", 5)
            names, sizes = set(), {}
            for entry in entries:
                name = entry.filename
                fm.require((name == "manifest.json" or member_name(name)) and name == entry.orig_filename,
                           "unsafe ZIP member", 5)
                fm.require(name not in names, "duplicate ZIP member: " + name, 5)
                fm.require(not entry.is_dir() and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG),
                           "ZIP members must be regular files", 5)
                fm.require(not entry.flag_bits & 1 and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                           "encrypted or unsupported ZIP member", 5)
                names.add(name)
                sizes[name] = entry.file_size
            fm.require("manifest.json" in names, "bundle manifest missing", 5)
            fm.require(sizes["manifest.json"] <= fm.TEXT_PARENT_LIMIT, "bundle manifest exceeds text limit", 5)
            yield archive.open, names, sizes
    except (zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError, zlib.error) as error:
        raise fm.ToolError("invalid or damaged ZIP bundle", 5) from error


def verify_bundle(directory, *, expected_manifest_sha256=None):
    """Verify a directory or ZIP without rendering or extracting member paths."""
    path = Path(directory).expanduser()
    fm.workspace_path(path)  # Check the actual expanded target before any open.
    with bundle_members(path) as (opened, names, sizes):
        with opened("manifest.json") as stream:
            data = stream.read(fm.TEXT_PARENT_LIMIT + 1)
        fm.require(len(data) <= fm.TEXT_PARENT_LIMIT, "bundle manifest exceeds text limit", 5)
        return verify_manifest(data, opened, names, sizes, expected_manifest_sha256)


def verify_manifest(data, opened, names, sizes, expected_manifest_sha256):
    if expected_manifest_sha256 is not None:
        fm.require(isinstance(expected_manifest_sha256, str) and
                   re.fullmatch("[0-9a-f]{64}", expected_manifest_sha256) is not None,
                   "expected manifest SHA256 must be 64 lowercase hex digits", 2)
        fm.require(fm.sha(data) == expected_manifest_sha256, "manifest identity mismatch", 5)
    def unique_pairs(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj

    def invalid_constant(_):
        raise ValueError("nonfinite JSON constant")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number

    try:
        manifest = json.loads(data, object_pairs_hook=unique_pairs, parse_constant=invalid_constant,
                              parse_float=finite_float)
    except (ValueError, UnicodeError) as error:
        raise fm.ToolError("invalid bundle manifest", 5) from error
    fm.require(isinstance(manifest, dict) and set(manifest) == {"version", "profile", "report", "artifacts"}
               and type(manifest["version"]) is int and manifest["version"] == 1
               and manifest["profile"] in ("wav", "midi", "sfz", "sfz-bank")
               and isinstance(manifest["report"], dict)
               and isinstance(manifest["artifacts"], dict), "invalid bundle manifest schema", 5)
    report = manifest["report"]
    source = report.get("source")
    fm.require(report.get("profile") == manifest["profile"] and type(report.get("version")) is int
               and report["version"] == 1 and isinstance(source, dict)
               and isinstance(source.get("record_sha256"), str)
               and re.fullmatch("[0-9a-f]{64}", source["record_sha256"]) is not None
               and isinstance(source.get("native"), dict)
               and isinstance(source["native"].get("sha256"), str)
               and re.fullmatch("[0-9a-f]{64}", source["native"]["sha256"]) is not None,
               "invalid bundle report/source identity", 5)
    required = {"source.tl", "README.txt"}
    if manifest["profile"] == "sfz-bank":
        cells = report.get("samples", {}).get("cells") if isinstance(report.get("samples"), dict) else None
        fm.require(isinstance(cells, list) and 1 <= len(cells) <= 256,
                   "sampled bank manifest needs its bounded cell list", 5)
        for cell in cells:
            fm.require(isinstance(cell, dict) and isinstance(cell.get("record"), str)
                       and isinstance(cell.get("wav"), str)
                       and re.fullmatch(r"n[0-9]{3}-v[0-9]{3}\.tl", cell["record"]) is not None
                       and cell["wav"] == cell["record"][:-3] + ".wav",
                       "invalid sampled bank member names", 5)
            fm.require(cell["record"] not in required and cell["wav"] not in required,
                       "duplicate sampled bank member", 5)
            required.update((cell["record"], cell["wav"]))
        required.add("instrument.sfz")
    else:
        required |= {"performance.mid"} if manifest["profile"] == "midi" else {"audio.wav"}
    if manifest["profile"] == "sfz":
        required.add("instrument.sfz")
    fm.require(set(manifest["artifacts"]) == required, "unexpected or missing bundle members", 5)
    if names is not None:
        fm.require(names == required | {"manifest.json"}, "unexpected or missing ZIP members", 5)
    for name, identity in manifest["artifacts"].items():
        fm.require(member_name(name) and isinstance(identity, dict) and set(identity) == {"bytes", "sha256"}
                   and type(identity["bytes"]) is int and identity["bytes"] >= 0
                   and isinstance(identity["sha256"], str)
                   and re.fullmatch("[0-9a-f]{64}", identity["sha256"]) is not None,
                   "invalid bundle artifact entry", 5)
        if sizes is not None:
            fm.require(sizes[name] == identity["bytes"], "ZIP member size disagrees with manifest: " + name, 5)
        with opened(name) as stream:
            actual = stream_identity(stream, identity["bytes"])
        fm.require(actual == identity,
                   "artifact identity mismatch: " + name, 5)
    fm.require(source["record_sha256"] == manifest["artifacts"]["source.tl"]["sha256"],
               "source record identity disagrees with artifact", 5)
    if manifest["profile"] == "sfz-bank":
        sample_source = report["samples"].get("source")
        fm.require(isinstance(sample_source, dict) and all(sample_source.get(k) == source.get(k)
                   for k in ("native", "record_sha256", "hash")),
                   "sampled bank source disagrees with bundle source", 5)
        fm.require(type(report["samples"].get("samples")) is int and report["samples"]["samples"] == len(cells),
                   "sampled bank count disagrees with cells", 5)
        for cell in cells:
            record, audio = manifest["artifacts"][cell["record"]], manifest["artifacts"][cell["wav"]]
            fm.require(cell.get("record_sha256") == record["sha256"] and cell.get("wav_sha256") == audio["sha256"]
                       and type(cell.get("record_bytes")) is int and cell["record_bytes"] == record["bytes"]
                       and type(cell.get("bytes")) is int and cell["bytes"] == audio["bytes"],
                       "sampled bank cell identity disagrees with artifact", 5)
    if manifest["profile"] in ("wav", "sfz"):
        fm.require(isinstance(report.get("audio"), dict)
                   and report["audio"].get("wav_sha256") == manifest["artifacts"]["audio.wav"]["sha256"],
                   "audio identity disagrees with artifact", 5)
    if manifest["profile"] in ("sfz", "sfz-bank"):
        if manifest["profile"] == "sfz-bank":
            from sample_export import sfz_text as bank_text
            expected_sfz = bank_text(cells)
        else:
            trigger = report.get("sfz", {}).get("trigger_key") if isinstance(report.get("sfz"), dict) else None
            fm.require(type(trigger) is int and 0 <= trigger <= 127, "invalid SFZ trigger report", 5)
            expected_sfz = sfz_text(trigger)
        if report.get("receiver_profile") is not None:
            receiver = report["receiver_profile"]
            fm.require(isinstance(receiver, dict) and receiver.get("name") == "sfizz-ram",
                       "unsupported SFZ receiver profile", 5)
            expected_sfz = b"<control> hint_ram_based=1\n" + expected_sfz
        fm.require(manifest["artifacts"]["instrument.sfz"]["bytes"] == len(expected_sfz),
                   "SFZ mapping differs from declared samples", 5)
        with opened("instrument.sfz") as stream:
            fm.require(stream.read(len(expected_sfz) + 1) == expected_sfz,
                       "SFZ mapping differs from declared samples", 5)
    return dict(version=1, ok=True, exit=0, operation="interchange_verify",
                profile=manifest["profile"], manifest_sha256=fm.sha(data),
                artifacts_verified=len(required), native_execution=False,
                external_playback_verified=False)


verify = public(verify_bundle)


@public
def export(source, *, profile, harness=None, out=None, key=None, channels=None,
           ppqn=960, tempo_us=500000, midi_format=1, bend_range=2, accept_loss=False,
           patch=None, notes=None, velocities=None, hold_ms=None, tail_ms=None, max_samples=None,
           receiver_profile=None, container="directory"):
    fm.require(profile in ("wav", "midi", "sfz", "sfz-bank"), "unknown export profile", 2)
    fm.require(container in ("directory", "zip"), "container must be directory or zip", 2)
    if receiver_profile is not None:
        fm.require(isinstance(receiver_profile, str) and receiver_profile == "sfizz-ram",
                   "receiver_profile must be sfizz-ram or absent", 2)
        fm.require(profile in ("sfz", "sfz-bank"), "receiver_profile requires an SFZ export", 2)
    fm.boolean(accept_loss, "accept_loss")
    if profile == "sfz":
        region = sfz_text(key)
    else:
        fm.require(key is None, "key is only for SFZ", 2)
    if profile != "midi":
        fm.require(channels is None and (ppqn, tempo_us, midi_format, bend_range) == (960, 500000, 1, 2),
                   "MIDI options require the MIDI profile", 2)
        fm.require(not accept_loss or profile == "sfz-bank", "accept_loss is only for MIDI or a sampled patch bank", 2)
    if profile != "sfz-bank":
        fm.require(all(x is None for x in (patch, notes, velocities, hold_ms, tail_ms, max_samples)),
                   "sample grid options require sfz-bank", 2)
    native = fm.Native(harness)
    accepted = accepted_source(source, wav=profile in ("wav", "sfz"), native=native)
    report = dict(version=1, profile=profile, source=source_identity(accepted),
                  inventory=inventory(accepted), external_playback_verified=False,
                  native_audio_preserved=profile in ("wav", "sfz"))
    files = {"source.tl": accepted["_record_bytes"]}
    if profile == "sfz-bank":
        import sample_export
        samples, details = sample_export.build(accepted, native, patch=patch, notes=notes,
                                               velocities=velocities, hold_ms=hold_ms,
                                               tail_ms=tail_ms, max_samples=max_samples)
        report["samples"] = details
        report["losses"] = details["losses"]
        report["losses_accepted"] = accept_loss
        if out is not None and not accept_loss:
            raise fm.ToolError("sampled patch drops performance/FX/spatial context; review the report and pass accept_loss", 2,
                               report=report, published=False)
        files.update(samples)
        help_text = ("Load instrument.sfz with all nNNN-vVVV.wav files beside it in an SFZ player.\n"
                     "Only the explicitly sampled MIDI keys are mapped. Velocity regions choose the nearest\n"
                     "recorded layer; a trigger plays that sample's fixed hold and tail (one_shot).\n"
                     "No loops, adjustable sustain, release samples or FM parameter editing are provided.\n"
                     "Session FX, spatial placement and the source song are omitted from these mono snapshots.\n"
                     "Each nNNN-vVVV.tl reproduces its own WAV with the recorded native engine.\n"
                     "Check the manifest's sampling budget, losses, clipping and target-player limitations.\n")
    elif profile == "midi":
        import midi_export
        performance, details = midi_export.encode(accepted, channels=channels, ppqn=ppqn,
                                                  tempo_us=tempo_us, midi_format=midi_format,
                                                  bend_range=bend_range)
        report["midi"] = details
        report["losses"] = details["losses"]
        report["losses_accepted"] = accept_loss
        if out is not None and details["losses"] and not accept_loss:
            raise fm.ToolError("MIDI carries performance, not TL sound; review the dry-run report and pass accept_loss", 2,
                               report=report, published=False)
        files["performance.mid"] = performance
        help_text = ("Load performance.mid in a DAW/sequencer, then route its explicit channels to instruments.\n"
                     "Assign target patches yourself; no program/bank selection or TL synth patch is embedded.\n"
                     "The receiver must honor the reported pitch-bend range/RPN0. Check same-note retriggers,\n"
                     "release articulation, quantization and all losses in manifest.json. This is not sound-equivalent.\n"
                     "A hardware synth without an SMF player needs an external sequencer or DAW. No MIDI is sent here.\n")
    else:
        audio = accepted["_wav_bytes"]
        info = wav_info(audio)
        fm.require(info["channels"] == accepted["channels"] and info["sample_rate"] == accepted["fs"],
                   "native WAV/header disagreement", 5)
        files["audio.wav"] = audio
        report["audio"] = dict(info, wav_sha256=fm.sha(audio), transformed=False)
        report["losses"] = ["Rendered audio does not expose editable notes, operators, automation or synthesis."]
        help_text = ("audio.wav is the unmodified native PCM16 render. Import it as audio in a DAW or compatible sampler.\n"
                     "Keep its original sample rate and channels when checking PCM identity. Playback devices may\n"
                     "resample or apply gain/effects. source.tl retains the editable native record for this engine.\n")
        if profile == "sfz":
            files["instrument.sfz"] = region
            report["sfz"] = dict(trigger_key=key, mode="one_shot", velocity_response="fixed",
                                 patch_conversion=False, region_count=1)
            report["losses"].append("A trigger plays the complete rendered record with its recorded hold/tail; no new note durations or timbres are synthesized.")
            help_text += ("Load instrument.sfz with audio.wav beside it in an SFZ player. Trigger MIDI key " + str(key) +
                          " at any nonzero velocity. Only this key is mapped. one_shot asks the player to ignore note-off;\n"
                          "hardware SFZ subsets may differ. No loops, pitch range, velocity layers or release samples\n"
                          "are inferred. This is a whole-record trigger, not a multisampled editable FM patch.\n")
    if receiver_profile is not None:
        receiver_control = b"<control> hint_ram_based=1\n"
        files["instrument.sfz"] = receiver_control + files["instrument.sfz"]
        if profile == "sfz-bank":
            report["samples"]["retained_bytes"] += len(receiver_control)
            report["samples"]["predicted_retained_bytes"] += len(receiver_control)
            fm.require(report["samples"]["retained_bytes"] <= report["samples"]["retained_byte_limit"],
                       "sample grid exceeds retained-byte limit with receiver profile", 2)
            fm.require(report["samples"]["predicted_retained_bytes"] <= report["samples"]["retained_byte_limit"],
                       "sample grid exceeds predicted retained-byte limit with receiver profile", 2)
        report["receiver_profile"] = dict(name=receiver_profile, opcode="hint_ram_based=1",
            full_ram_requested=True, wav_file_bytes=sum(len(data) for name, data in files.items() if name.endswith(".wav")))
        help_text += ("Receiver profile sfizz-ram requests full sample loading with the ARIA/sfizz\n"
                      "extension hint_ram_based=1. Reported WAV file bytes are not a receiver memory bound;\n"
                      "other players may ignore the hint. sfizz1.2.3 may also emit a hint_stealing warning\n"
                      "when reading this control; retain and inspect receiver diagnostics.\n")
    files["README.txt"] = help_text.encode("utf-8")
    reply = dict(version=1, ok=True, exit=0, operation="interchange_export", dry_run=out is None,
                 published=False, report=report)
    if out is not None:
        reply["bundle"] = publish(out, files, report, container=container)
        reply["published"] = True
    return reply


def channel_map(words):
    result = {}
    for word in words:
        fm.require(re.fullmatch(r"[0-9]+=[0-9]+", word) is not None,
                   "channel must be VOICE=CHANNEL (channel 1..16)", 2)
        voice, channel = map(int, word.split("="))
        fm.require(voice not in result, "duplicate voice channel mapping", 2)
        result[voice] = channel
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", type=Path, help="explicit built native harness; never auto-built")
    commands = parser.add_subparsers(dest="command", required=True)
    look = commands.add_parser("inspect", help="native-validate and inventory a TL, recipe or split piece")
    look.add_argument("source", nargs="+")
    for name in ("wav", "midi", "sfz", "sfz-bank"):
        command = commands.add_parser(name, help="dry-run by default; --out publishes a new bundle")
        command.add_argument("source", nargs="+")
        command.add_argument("--out", type=Path)
        command.add_argument("--container", choices=("directory", "zip"), default="directory",
                             help="new output directory (default) or one complete ZIP file")
        if name in ("sfz", "sfz-bank"):
            command.add_argument("--receiver-profile", choices=("sfizz-ram",))
        if name == "sfz":
            command.add_argument("--key", type=int, required=True)
        if name == "midi":
            command.add_argument("--channel", action="append", default=[], metavar="VOICE=CHANNEL")
            command.add_argument("--ppqn", type=int, default=960)
            command.add_argument("--tempo-us", type=int, default=500000)
            command.add_argument("--midi-format", type=int, choices=(0, 1), default=1)
            command.add_argument("--bend-range", type=int, default=2)
            command.add_argument("--accept-loss", action="store_true")
        if name == "sfz-bank":
            command.add_argument("--patch", required=True)
            command.add_argument("--notes", type=int, nargs="+", required=True)
            command.add_argument("--velocities", type=int, nargs="+", required=True)
            command.add_argument("--hold-ms", type=int, required=True)
            command.add_argument("--tail-ms", type=int, required=True)
            command.add_argument("--max-samples", type=int, required=True)
            command.add_argument("--accept-loss", action="store_true")
    check = commands.add_parser("verify", help="verify bundle identities without rendering")
    check.add_argument("directory", type=Path, help="bundle directory or ZIP file")
    check.add_argument("--expected-manifest-sha256")
    args = parser.parse_args(argv)
    if args.command == "verify":
        reply = verify(args.directory, expected_manifest_sha256=args.expected_manifest_sha256)
    else:
        source = args.source[0] if len(args.source) == 1 else args.source
        if args.command == "inspect":
            reply = inspect(source, harness=args.harness)
        else:
            options = dict(harness=args.harness, out=args.out, container=args.container)
            if args.command in ("sfz", "sfz-bank"):
                options["receiver_profile"] = args.receiver_profile
            if args.command == "sfz":
                options["key"] = args.key
            if args.command == "midi":
                try:
                    options["channels"] = channel_map(args.channel)
                except fm.ToolError as error:
                    parser.error(str(error))
                options.update(ppqn=args.ppqn, tempo_us=args.tempo_us, midi_format=args.midi_format,
                               bend_range=args.bend_range, accept_loss=args.accept_loss)
            if args.command == "sfz-bank":
                options.update(patch=args.patch, notes=args.notes, velocities=args.velocities,
                               hold_ms=args.hold_ms, tail_ms=args.tail_ms,
                               max_samples=args.max_samples, accept_loss=args.accept_loss)
            reply = export(source, profile=args.command, **options)
    print(json_bytes(reply).decode(), end="")
    return reply["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
