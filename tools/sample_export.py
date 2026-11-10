#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Explicit native patch snapshots as an external SFZ sample grid.

The caller supplies an already accepted canonical record and publishes the
returned bytes. No source normalization, inferred loops, hardware I/O or sample
playback in the integer synthesizer is performed here.
"""
from collections import Counter
import io
from pathlib import Path
import re
import tempfile
import wave

import fm
from export_schema import SCHEMA


MAX_RETAINED_BYTES = 64 * 1024 * 1024
MAX_CELLS = 256
FRAME_MAX = 0xffffffff
UINT64_MAX = 0xffffffffffffffff
FRAMING_ALLOWANCE = 2048
PATCH_IDS = SCHEMA["patch"]
ASSET_IDS = SCHEMA["asset"]
KNOWN = SCHEMA


def _integer(value, low, high, name, status=2):
    fm.require(type(value) is int and low <= value <= high,
               f"{name} must be an integer in {low}..{high}", status)
    return value


def _choices(values, low, high, name):
    fm.require(isinstance(values, (list, tuple)) and values,
               name + " must be a nonempty list or tuple", 2)
    result = [_integer(v, low, high, name) for v in values]
    fm.require(len(set(result)) == len(result), name + " must not contain duplicates", 2)
    return sorted(result)


def velocity_regions(velocities):
    """Partition MIDI velocities1..127 around explicit representatives; lower tie."""
    values = _choices(velocities, 1, 127, "velocities")
    edges = [0] + [(a + b) // 2 for a, b in zip(values, values[1:])] + [127]
    return [dict(velocity=v, lovel=edges[i] + 1, hivel=edges[i + 1])
            for i, v in enumerate(values)]


def _selected(accepted, native, patch):
    fm.require(isinstance(accepted, dict) and accepted.get("ok") is True
               and type(accepted.get("exit")) is int and accepted["exit"] == 0,
               "sample export requires a successful native canonical render")
    identity = accepted.get("native", {})
    fm.require(isinstance(identity, dict) and all(identity.get(k) == native.identity.get(k)
               for k in ("sha256", "engine")), "source and selected native identities differ", 5)
    data = accepted.get("_record_bytes")
    fm.require(isinstance(data, bytes) and fm.body_sha256(data) == accepted.get("record_sha256"),
               "source canonical byte identity differs", 5)
    data = fm.tl_document_module().parse_document(data)["body"]
    rows = native.metadata.canonical(data)
    fm.require(rows == accepted.get("_rows"), "source canonical metadata differs", 5)
    fm.require(isinstance(patch, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,31}", patch)),
               "patch must name an existing native patch", 2)
    for row in rows:
        section, kind = row["section"], row["id"]
        fm.require(section != "generator" and kind not in ("event_gen", "event_lane"),
                   "sample export refuses generators and generator bindings")
        fm.require(section in KNOWN and kind in KNOWN[section],
                   f"sample export refuses unknown dependencies: {section}/{kind}")
        if kind == "mod":
            source = row["raw"]["SOURCE"]
            fm.require(isinstance(source, str) and not source.startswith("gen."),
                       "sample export refuses generator bindings")
            fm.require(re.fullmatch(r"lfo|peg|fenv|eg[0-3]|vel|note|ctl[0-7]", source) is not None,
                       "sample export refuses unknown modulation dependency: " + source)
    patches = [r for r in rows if r["section"] == "patch" and r["raw"]["PATCH"] == patch]
    fm.require(bool(patches), "selected patch does not exist: " + patch, 2)
    banks = {r["raw"]["BANK"] for r in patches if r["id"] == "wtuse"}
    assets = [r for r in rows if r["section"] == "asset" and r["raw"]["BANK"] in banks]
    fm.require(banks == {r["raw"]["BANK"] for r in assets if r["id"] == "wtbank"},
               "selected patch has an unresolved wavetable dependency")
    lines = data.decode("utf-8").splitlines()
    selected = sorted(patches + assets, key=lambda r: r["line_index"])
    body = "\n".join(lines[r["line_index"]] for r in selected) + "\n"
    timelines = [r for r in rows if r["id"] == "header_timeline"]
    engines = [r for r in rows if r["id"] == "header_engine"]
    fm.require(len(timelines) == len(engines) == 1, "source lacks unique native identity headers", 5)
    header = "\n".join(lines[r["line_index"]] for r in timelines + engines) + "\n"
    return rows, patches, banks, body, header


def _audio(data, fs, frames):
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            fm.require(audio.getcomptype() == "NONE" and audio.getsampwidth() == 2
                       and audio.getnchannels() == 1 and audio.getframerate() == fs
                       and audio.getnframes() == frames, "sample WAV geometry differs", 5)
            pcm = audio.readframes(frames)
    except (wave.Error, EOFError) as error:
        raise fm.ToolError("native sample WAV is invalid", 5) from error
    fm.require(len(pcm) == frames * 2, "native sample WAV is truncated", 5)
    return dict(sample_rate=fs, channels=1, sample_width_bytes=2, frames=frames,
                pcm_sha256=fm.sha(pcm), wav_sha256=fm.sha(data), bytes=len(data))


def sfz_text(cells):
    """Encode the declared fixed-root/velocity map used by bundle verification."""
    regions = []
    for cell in cells:
        note = _integer(cell.get("note"), 0, 127, "sample note", 5)
        low = _integer(cell.get("lovel"), 1, 127, "sample low velocity", 5)
        high = _integer(cell.get("hivel"), low, 127, "sample high velocity", 5)
        name = cell.get("wav")
        fm.require(isinstance(name, str) and re.fullmatch(r"n[0-9]{3}-v[0-9]{3}\.wav", name),
                   "invalid SFZ sample member", 5)
        regions.append(f"<region> sample={name} key={note} pitch_keycenter={note} "
                       f"lovel={low} hivel={high} amp_veltrack=0 loop_mode=one_shot")
    return ("// Fixed native note snapshots; exact keys; nearest velocity layer.\n" +
            "\n".join(regions) + "\n").encode("ascii")


def build(accepted, native, *, patch, notes, velocities, hold_ms, tail_ms, max_samples):
    """Return ({basename: bytes}, report); every grid argument is explicit.

    Each cell consumes one native render and retains its canonical TL and WAV.
    The parent must retain the complete returned set, report and source identity
    when publishing; this function never publishes an incomplete grid.
    """
    notes = _choices(notes, 0, 127, "notes")
    layers = velocity_regions(velocities)
    hold_ms = _integer(hold_ms, 1, UINT64_MAX // 1_000_000, "hold_ms")
    tail_ms = _integer(tail_ms, 1, UINT64_MAX // 1_000_000, "tail_ms")
    max_samples = _integer(max_samples, 1, MAX_CELLS, "max_samples")
    count = len(notes) * len(layers)
    fm.require(count <= max_samples, "sample grid exceeds explicit max_samples", 2)
    rows, patches, banks, body, identity_header = _selected(accepted, native, patch)
    fs = _integer(accepted.get("fs"), 1, FRAME_MAX, "source fs", 5)
    rates = [r["raw"].get("HZ") for r in rows if r["id"] == "header_fs"]
    fm.require(rates == [fs], "source sample-rate metadata differs", 5)
    hold_ns, length_ns = hold_ms * 1_000_000, (hold_ms + tail_ms) * 1_000_000
    fm.require(length_ns <= UINT64_MAX and length_ns * fs <= UINT64_MAX,
               "sample duration exceeds native nanosecond/rate bounds", 2)
    frames = length_ns * fs // 1_000_000_000
    fm.require(1 <= frames <= FRAME_MAX and 1 <= hold_ns * fs // 1_000_000_000 < frames,
               "sample hold/tail must leave positive native frames and an in-range note-off", 2)
    body_bytes = len(body.encode("utf-8"))
    predicted = count * (frames * 2 + 44 + body_bytes + FRAMING_ALLOWANCE)
    fm.require(predicted <= MAX_RETAINED_BYTES, "sample grid exceeds 64MiB retained-byte limit", 2)
    composer = fm.compose_module()
    frequencies = {note: composer.mhz_m(note) for note in notes}
    for note, frequency in frequencies.items():
        fm.require(frequency < fs * 500,
                   f"sample root {note} ({frequency}mHz) must be below source Nyquist ({fs * 500}mHz)", 2)
    files, cells, attempts = {}, [], 0
    retained = 0
    with tempfile.TemporaryDirectory(prefix="fm-samples-") as temporary:
        source = Path(temporary) / "sample.tl"
        for note in notes:
            frequency = frequencies[note]
            for layer in layers:
                velocity = layer["velocity"]
                native_velocity = velocity * 32767 // 127
                stem = f"n{note:03d}-v{velocity:03d}"
                record_name, wav_name = stem + ".tl", stem + ".wav"
                text = (identity_header + f"fs {fs}\nlength_ns {length_ns}\nstreams sample\n" + body +
                        f"voice 0 {patch}\n0 sample 0 on {frequency} {native_velocity}\n" +
                        f"{hold_ns} sample 0 off\n")
                source.write_text(text, encoding="utf-8")
                attempts += 1
                try:
                    result = fm.native_render(native, [source], want_wav=True)
                    fm.require(result["exit"] == 0, "sample cell failed native verification", result["exit"])
                    record, audio = result["_record_bytes"], result["_wav_bytes"]
                    fm.require(len(record) <= body_bytes + FRAMING_ALLOWANCE,
                               "native sample record exceeds its preflight framing allowance", 5)
                    audio_info = _audio(audio, fs, frames)
                    fm.require(result["fs"] == fs and result["channels"] == 1
                               and result["samples"] == frames,
                               "native sample metrics disagree with planned geometry", 5)
                    retained += len(record) + len(audio)
                    fm.require(retained <= MAX_RETAINED_BYTES, "sample grid exceeds retained-byte limit", 5)
                except fm.ToolError as error:
                    details = dict(error.details, sample_export=dict(native_render_attempts=attempts,
                        completed_samples=len(cells), failed_note=note, failed_velocity=velocity,
                        published=False))
                    raise fm.ToolError("sample cell " + stem + ": " + str(error), error.exit, **details) from error
                files[record_name], files[wav_name] = record, audio
                cells.append(dict(note=note, midi_velocity=velocity, native_velocity_q15=native_velocity,
                    frequency_millihz=frequency, lovel=layer["lovel"], hivel=layer["hivel"],
                    record=record_name, record_sha256=fm.sha(record), record_bytes=len(record),
                    wav=wav_name, hash=result["hash"], clipped=result.get("clipped"), **audio_info))
    files["instrument.sfz"] = sfz_text(cells)
    retained += len(files["instrument.sfz"])
    fm.require(retained <= MAX_RETAINED_BYTES, "sample grid exceeds retained-byte limit", 5)
    dropped = Counter(r["id"] for r in rows if r["section"] in ("event", "fx", "space"))
    controls = sorted({r["raw"]["SOURCE"] for r in patches
                       if r["id"] == "mod" and r["raw"]["SOURCE"].startswith("ctl")})
    report = dict(version=1, profile="sfz-bank", patch=patch, source=dict(
        native=accepted["native"], record_sha256=accepted["record_sha256"], hash=accepted.get("hash")),
        sample_rate=fs, notes=notes, velocities=[r["velocity"] for r in layers],
        velocity_regions=layers, hold_ms=hold_ms, tail_ms=tail_ms, length_ns=length_ns,
        frames_per_sample=frames, max_samples=max_samples, samples=len(cells),
        native_render_attempts=attempts, source_acceptance_renders_included=False,
        predicted_retained_bytes=predicted, retained_bytes=retained,
        retained_byte_limit=MAX_RETAINED_BYTES, assets=sorted(banks),
        controller_sources_at_initial_zero=controls, dropped_context=dict(sorted(dropped.items())),
        cells=cells, mode="one_shot", key_mapping="exact_roots_only",
        root_pitch_definition="native_base_frequency_not_measured_fundamental",
        velocity_mapping="nearest_representative_lower_tie", normalization=False,
        external_playback_verified=False, tail_complete=None,
        losses=[
            "Samples retain this patch's mono note snapshots, not editable FM synthesis or the source song.",
            "Source performance events, other voices, session FX and spatial context are omitted.",
            "Each trigger plays the recorded hold and tail; note-off is ignored by compliant one_shot players.",
            "No sustain loop or release articulation is inferred; the explicit tail may truncate remaining sound.",
            "Only selected MIDI keys are mapped; intermediate velocities select the nearest recorded layer with lower ties.",
            "No velocity scaling is added by SFZ; dynamics and timbre are baked at the recorded native Q15 velocity.",
            "Every cell starts a fresh native voice; controller inputs start at zero and source automation is omitted.",
            "Target sampler opcode handling, gain, interpolation, voice allocation and effects are not certified.",
        ])
    return files, report
