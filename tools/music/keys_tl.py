#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
""": derive ordinary-stereo Keys from an ordered native TL source pool.

No builds, audio files, generator reimplementation, or source rewrites. Run as
a standalone CLI: the existing generator adapter's temporary files are directed
into the fresh owned work directory for the duration of this operation.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("keys_fm", ROOT / "tools/fm.py")
fm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fm)
generator = fm.generator_module()
compose = fm.compose_module()
PLAY_SECONDS = 120.0
PASSES = 8
SPATIAL_HEADERS = {"space", "order", "pinna", "yaw", "panlaw"}
SPATIAL_EVENTS = {"pos", "move", "yaw"}


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def checked_path(value):
    """Reject known aliases before resolving; never follow a symlink ancestor."""
    lexical = Path(value).absolute()
    for ancestor in (*reversed(lexical.parents), lexical):
        fm.require(not ancestor.is_symlink(), "symlink path or ancestor: " + str(ancestor), 2)
    return Path(os.path.abspath(lexical))


def bounded_text(path):
    fm.require(path.is_file() and not path.is_symlink(), "canonical file is missing or not regular", 5)
    with path.open("rb") as handle:
        raw = handle.read(generator.MAX_INPUT + 1)
    fm.require(len(raw) <= generator.MAX_INPUT, "candidate/canonical exceeds 4MiB", 2)
    fm.tl_document_module().parse_document(raw)
    return raw.decode("utf-8")


def event(words):
    return len(words) >= 4 and words[0].isdigit()


def position(time_ns, stream, voice, frequency, mirror):
    xyz = compose.keys_pos(compose.midi_of(int(frequency)), r=1000,
                           mirror=mirror, profile="lin30")
    return " ".join(map(str, (time_ns, stream, voice, "pos", *xyz)))


def project(canonical, manifest, mirror=False):
    """A narrow projection of native canonical text; native owns TL validation."""
    document = fm.tl_document_module().parse_document(canonical.encode("utf-8"))
    # An automatic arrangement does not declare the source's exact-record
    # attribution, licence or other descriptive metadata for its new body.
    canonical = document["body"].decode("utf-8")
    lines, removed, streams = [], [], None
    manual_count = 0
    for number, line in enumerate(canonical.splitlines(), 1 + document["header"].count(b"\n")):
        words = line.split()
        if not words:
            lines.append(line)
            continue
        key = words[0]
        if key == "timeline":
            lines.extend(["timeline draft2", "space stereo"])
        elif key in SPATIAL_HEADERS or key == "hash" or (event(words) and words[3] in SPATIAL_EVENTS):
            removed.append(dict(line=number, text=line, reason="inherited_hash" if key == "hash" else "spatial_layout"))
        else:
            if key == "streams":
                streams = words[1:]
            if event(words) and words[3] == "on":
                lines.append(position(words[0], words[1], words[2], words[4], mirror))
                manual_count += 1
            lines.append(line)
    fm.require(bool(streams), "canonical source has no declared stream", 5)
    generated_count = 0
    for line in manifest.splitlines()[1:-1]:
        match = generator._EVENT.fullmatch(line)
        fm.require(match is not None, "validated manifest contains an invalid event", 5)
        if match[5] == "on":
            # Native explicit events precede the generator bank. The first
            # declared stream matches compose.keys_version's convention.
            lines.append(position(match[2], streams[0], match[4], match[6], mirror))
            generated_count += 1
    text = "\n".join(lines) + "\n"
    fm.require(len(text.encode("utf-8")) <= generator.MAX_INPUT, "candidate exceeds 4MiB", 2)
    return text, dict(removed_rows=removed, manual_positions=manual_count,
                      generated_positions=generated_count, generated_stream=streams[0],
                      profile="lin30", radius_mm=1000, mirror=mirror)


def shift_carriers(text, delta):
    """Shift every actual carrier by one common dB10 delta, or refuse all."""
    rows = [line.split() for line in text.splitlines()]
    masks = {words[1]: int(words[3]) for words in rows if words[:1] == ["route"]}
    shifted, changes = [], []
    for line, words in zip(text.splitlines(), rows):
        if words[:1] == ["patch"] and masks.get(words[1], 1) & (1 << int(words[2])):
            before = int(words[4])
            after = before + delta
            fm.require(0 <= after <= 1200,
                       "carrier shift exceeds 0..1200: %s op%s %d %+d" %
                       (words[1], words[2], before, delta), 2)
            words[4] = str(after)
            line = " ".join(words)
            changes.append(dict(patch=words[1], op=int(words[2]), before=before, after=after))
        shifted.append(line)
    fm.require(bool(changes), "canonical source has no declared carriers", 5)
    return "\n".join(shifted) + "\n", changes


def level_delta(fields):
    peak, clipped = fields.get("peak_dbfs"), fields.get("clipped")
    fm.require(type(peak) in (int, float) and math.isfinite(peak) and fields.get("peak", 1) != 0,
               "silence or invalid/nonfinite native peak", 2)
    fm.require(type(clipped) is int and clipped >= 0, "invalid native clipped count", 5)
    if clipped == 0 and abs(peak - compose.TARGET) <= compose.TOL:
        return None
    return round((peak - compose.TARGET) * 10) if clipped == 0 else 15


def carrier_bound_step(text, fields, requested):
    """: one legal common step; only a measured quiet lower bound may pass."""
    _, carriers = shift_carriers(text, 0)
    low = -min(row["before"] for row in carriers)
    high = 1200 - max(row["before"] for row in carriers)
    applied = None if requested is None else max(low, min(high, requested))
    limiting = [dict(patch=row["patch"], op=row["op"], level_db10=row["before"])
                for row in carriers if (requested is not None and requested < 0 and row["before"] == -low)
                or (requested is not None and requested > 0 and row["before"] == 1200 - high)]
    # level_delta has already rejected silence/nonfinite/invalid reports. Repeat
    # its finite/nonzero preconditions here so this helper cannot grant a policy
    # exception independently of that validation.
    peak = fields.get("peak_dbfs")
    below = (type(peak) in (int, float) and math.isfinite(peak) and fields.get("peak", 0) > 0
             and fields.get("clipped") == 0 and peak < compose.TARGET - compose.TOL
             and low == 0 and requested is not None and requested < 0 and applied == 0)
    return dict(requested_delta_db10=requested, applied_delta_db10=applied,
                legal_delta_db10=[low, high], limiting_carriers=limiting,
                limited_by_carrier_bounds=requested != applied,
                below_target_at_carrier_bound=below)


def publish(path, data):
    """Publish complete bytes without overwriting an existing destination."""
    checked_path(path)
    fd, temporary = tempfile.mkstemp(prefix=".keys-publish-", dir=path.parent)
    published = None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            info = os.fstat(handle.fileno())
        os.link(temporary, path)  # Atomic no-replace, including racing creators.
        published = dict(device=info.st_dev, inode=info.st_ino, sha256=fm.sha(data))
    finally:
        try:
            os.unlink(temporary)
        except OSError as error:
            if published is None:
                raise
            # Publication already succeeded. A retained temporary link is a
            # cleanup warning, not a false failed-publication outcome.
            published["cleanup_warning"] = dict(path=temporary, message=str(error))
    return published


def retain_result(owned, result, output=None, publication=None):
    """If the final receipt fails, retract only our unchanged published inode."""
    try:
        dump(owned / "result.json", result)
    except OSError as error:
        if result.get("message") is not None:
            result["operation_error"] = dict(stage=result.get("failure_stage"), message=result["message"])
        result.update(ok=False, exit=5, failure_stage="receipt", message="cannot retain result: " + str(error),
                      receipt_error=type(error).__name__)
        if publication is not None:
            try:
                checked_path(output)
                info = output.lstat()
                result["destination_state"] = dict(present=True, device=info.st_dev, inode=info.st_ino,
                                                   bytes=info.st_size, regular=stat.S_ISREG(info.st_mode))
                fm.require(stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) ==
                           (publication["device"], publication["inode"]) and
                           fm.sha(output.read_bytes()) == publication["sha256"],
                           "published output changed; preserved after receipt failure", 5)
                output.unlink()
                result.update(publication_rolled_back=True, published=False, output=None)
                result["destination_state"] = dict(present=False)
            except (OSError, fm.ToolError) as rollback_error:
                result["publication_rollback_error"] = str(rollback_error)
        # A transient failure may permit the failure receipt. If the disk is
        # still full, the returned/CLI JSON remains the explicit failure report.
        try:
            dump(owned / "result.json", result)
        except OSError:
            pass


def convert(files, *, harness, out, work, mirror=False, level_policy="strict"):
    result = dict(version=1, contract="", ok=False, exit=5, failure_stage="preflight",
                  message=None, inputs=[], native=None, commands=[], manifests=[], passes=[],
                  output=None, published=False, level_policy=level_policy,
                  limits=dict(files=generator.MAX_FILES, input_bytes=generator.MAX_INPUT,
                  candidate_bytes=generator.MAX_INPUT, stdout_bytes=generator.MAX_STDOUT,
                  stderr_bytes=generator.MAX_STDERR, play_child_seconds=PLAY_SECONDS,
                  manifest_child_seconds=generator.CHILD_SECONDS, passes=PASSES,
                  total_memory_guarantee=False))
    owned = None
    publication = None
    output = None
    previous_tempdir = tempfile.tempdir
    runner = generator.BoundedRunner(fm)
    result["play_processes"] = runner.processes
    remaining = PLAY_SECONDS

    def play(paths, merged, phase):
        nonlocal remaining
        result["failure_stage"] = phase
        expected_header = fm.document_header(paths)
        body_path = merged.with_suffix(".body.tl")
        args = ["--play", "--merged", merged, "--merged-body", body_path, "--", *paths]
        command = dict(phase=phase, argv=[str(native), *map(str, args)],
                       cwd=str(Path.cwd()), remaining_child_seconds=remaining)
        result["commands"].append(command)
        runner.deadline = time.monotonic() + remaining
        started = time.monotonic()
        try:
            response = runner.run(native, args, result["native"])
        finally:
            elapsed = time.monotonic() - started
            remaining -= elapsed
            command["elapsed_seconds"] = elapsed
        command["returncode"] = response.returncode
        fm.require(response.returncode == 0, "native " + phase + " refused", response.returncode)
        fields = fm.play_fields(response.stdout)
        fm.require(fields.get("block_indep") is True and fields.get("match") is not False,
                   "native replay or block independence failed", 5)
        fm.verify_native_document(fm.read_native_document(merged), fm.read_native_document(body_path), expected_header)
        # PLAY fields may contain -inf for silence; raw captures retain that
        # spelling while JSON records use the toolkit's finite projection.
        command["play"] = fm.clean(fields)
        return fields

    def manifest(path, phase):
        result["failure_stage"] = phase
        runner_type = generator.BoundedRunner

        class RecordedManifestRunner(runner_type):
            def run(self, executable, args, identity):
                command = dict(phase=phase, argv=[str(executable), *map(str, args)], cwd=str(Path.cwd()))
                result["commands"].append(command)
                started = time.monotonic()
                try:
                    response = super().run(executable, args, identity)
                    command["returncode"] = response.returncode
                    return response
                finally:
                    command["elapsed_seconds"] = time.monotonic() - started

        # This standalone operation already owns tempfile.tempdir. Decorate the
        # existing adapter runner only to retain exact argv, with no policy or
        # native scheduling change, and restore its class even on host failure.
        generator.BoundedRunner = RecordedManifestRunner
        try:
            full = generator.execute(fm, [str(path)], harness=str(native))
        finally:
            generator.BoundedRunner = runner_type
        result["manifests"].append(dict(phase=phase, result=full))
        fm.require(full["ok"], "native " + phase + " refused: " + str(full.get("message")), full["exit"])
        fm.require(full["native"]["sha256"] == result["native"]["sha256"], "native executable changed", 5)
        generator.summarize(fm, full)
        text = generator._summary_capture(fm, full, "--gen-events")
        (owned / (phase + ".txt")).write_text(text, encoding="utf-8")
        result["native"].update(engine=full["native"].get("engine"))
        return text

    try:
        fm.require(level_policy in ("strict", "carrier-bound"), "unknown level policy", 2)
        fm.require(1 <= len(files) <= generator.MAX_FILES, "supply 1..17 source files", 2)
        sources = [checked_path(path) for path in files]
        native, output, destination = map(checked_path, (harness, out, work))
        fm.require(native.is_file() and os.access(native, os.X_OK), "explicit native executable is unavailable", 2)
        fm.require(not output.exists() and not destination.exists(), "output and work destinations must be fresh", 2)
        fm.require(output.parent.is_dir() and destination.parent.is_dir(), "destination parents must already exist", 2)
        fm.require(output != destination and destination not in output.parents,
                   "output must be separate from the owned work directory", 2)
        identities = []
        for path in sources:
            info = path.stat()
            fm.require(stat.S_ISREG(info.st_mode), "source is not a regular file", 2)
            identities.append((info.st_dev, info.st_ino))
        fm.require(len(set(identities)) == len(identities), "source aliases are not allowed", 2)
        destination.mkdir()
        owned = destination
        dump(owned / "ownership.json", dict(contract="", work=str(owned), output=str(output)))
        (owned / "inputs").mkdir()
        (owned / "temporary").mkdir()
        tempfile.tempdir = str(owned / "temporary")
        result["native"] = dict(path=str(native), sha256=fm.sha(native.read_bytes()))
        result["sources"] = {str(path.relative_to(ROOT)): fm.sha(path.read_bytes()) for path in
                             (Path(__file__), ROOT / "tools/music/compose.py", ROOT / "tools/fm.py", ROOT / "tools/generator.py")}
        result["failure_stage"] = "snapshot"
        snapshots = generator._snapshots(fm, owned / "inputs", sources, None, result["inputs"])
        source_canonical = owned / "source.canonical.tl"
        original = play(snapshots, source_canonical, "source")
        result["source_play"] = fm.clean(original)
        source_manifest = manifest(source_canonical, "source-manifest")
        candidate, projection = project(bounded_text(source_canonical), source_manifest, mirror)
        result["projection"] = projection
        for attempt in range(PASSES):
            candidate_path = owned / ("candidate-%02d.tl" % attempt)
            canonical = owned / ("canonical-%02d.tl" % attempt)
            fm.require(len(candidate.encode("utf-8")) <= generator.MAX_INPUT, "candidate exceeds 4MiB", 2)
            candidate_path.write_text(candidate, encoding="utf-8")
            fields = play([candidate_path], canonical, "level-%02d" % attempt)
            row = dict(attempt=attempt, play=fm.clean(fields), candidate_sha256=fm.sha(candidate_path.read_bytes()))
            result["passes"].append(row)
            result["failure_stage"] = "levelling"
            delta = level_delta(fields)
            row["next_delta_db10"] = delta
            if level_policy == "carrier-bound":
                step = carrier_bound_step(candidate, fields, delta)
                row["carrier_bound_step"] = step
                delta = step["applied_delta_db10"]
                row["next_delta_db10"] = delta
                if step["below_target_at_carrier_bound"]:
                    row["next_delta_db10"] = None
                    result["levelling"] = dict(policy=level_policy, contract="",
                        status="below_target_at_carrier_bound", target_met=False,
                        target_dbfs=compose.TARGET, tolerance_db=compose.TOL,
                        actual_peak_dbfs=fields["peak_dbfs"], **step)
                    break
                if delta is None:
                    result["levelling"] = dict(policy=level_policy, contract="", status="target_met",
                        target_met=True, target_dbfs=compose.TARGET, tolerance_db=compose.TOL,
                        actual_peak_dbfs=fields["peak_dbfs"], **step)
                else:
                    fm.require(delta != 0, "carrier bound prevents another legal common shift; target not accepted", 2)
            if delta is None:
                break
            fm.require(attempt + 1 < PASSES, "levels did not settle in eight passes", 2)
            candidate, changes = shift_carriers(candidate, delta)
            row["carrier_changes"] = changes
        replay_path = owned / "replay.tl"
        replay = play([canonical], replay_path, "replay")
        fm.require(replay.get("match") is True and replay.get("hash") == fields.get("hash"),
                   "final exact PCM replay failed", 5)
        fm.require(canonical.read_bytes() == replay_path.read_bytes(), "canonical text changed on replay", 5)
        fm.require(manifest(canonical, "final-manifest") == source_manifest,
                   "generated schedule changed during Keys projection", 5)
        result["failure_stage"] = "publication"
        for path, identity in zip(sources, result["inputs"]):
            checked_path(path)
            fm.require(path.stat().st_size == identity["bytes"] and fm.sha(path.read_bytes()) == identity["sha256"],
                       "original source changed during operation", 5)
        fm.require(fm.sha(native.read_bytes()) == result["native"]["sha256"], "native executable changed", 5)
        raw = canonical.read_bytes()
        # Exercise receipt creation before publishing. The final receipt below
        # replaces this pending record; a failed final write retracts our output.
        result["pending_output"] = dict(path=str(output), bytes=len(raw), sha256=fm.sha(raw))
        dump(owned / "result.json", result)
        publication = publish(output, raw)
        result.update(ok=True, exit=0, failure_stage=None, published=True, output=dict(path=str(output), bytes=len(raw),
                      sha256=fm.sha(raw), pcm_hash=replay["hash"], identity_kind="verified_at_publication"),
                      remaining_play_child_seconds=remaining)
        result.pop("pending_output")
        if publication.get("cleanup_warning"):
            result["cleanup_warning"] = publication["cleanup_warning"]
    except (fm.ToolError, OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        result.update(exit=error.exit if isinstance(error, fm.ToolError) else 5,
                      message=str(error), error_type=type(error).__name__)
    finally:
        tempfile.tempdir = previous_tempdir
        if owned is not None:
            retain_result(owned, result, output, publication)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--mirror", action="store_true")
    parser.add_argument("--level-policy", choices=("strict", "carrier-bound"), default="strict",
                        help="strict target by default; explicit carrier-bound may publish unclipped quiet boundary output")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()
    result = convert(args.files, harness=args.harness, out=args.out, work=args.work, mirror=args.mirror,
                     level_policy=args.level_policy)
    summary_keys = ("ok", "exit", "failure_stage", "message", "output", "published", "receipt_error",
                    "publication_rolled_back", "publication_rollback_error", "cleanup_warning", "operation_error", "destination_state",
                    "level_policy", "levelling")
    print(json.dumps({key: result[key] for key in summary_keys if key in result}, allow_nan=False))
    return result["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
