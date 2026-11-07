#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Independent regression behavior checks; all generated artifacts stay in --out."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import csv
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
import wave


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def need(condition, message):
    if not condition:
        raise AssertionError(message)


def okay(reply, code=0):
    need(isinstance(reply, dict) and reply.get("version") == 1, "missing versioned reply")
    need(reply.get("ok") is True and reply.get("exit") == code, str(reply)[:2000])
    json.dumps(reply, allow_nan=False)
    return reply


class Checks:
    def __init__(self, args):
        self.args = args
        self.repo, self.out, self.harness = args.repo.resolve(), args.out.resolve(), args.harness.resolve()
        need(not self.out.is_relative_to(self.repo), "--out must be outside repository")
        self.out.mkdir(parents=True, exist_ok=True)
        spec = importlib.util.spec_from_file_location("fm_acceptance", self.repo / "tools/fm.py")
        self.fm = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.fm
        spec.loader.exec_module(self.fm)
        self.results = []
        self.source_hash = digest(self.repo / "tools/fm.py")
        self.source_hashes = {name: digest(self.repo / name) for name in (
            "tools/fm.py", "tools/music/analyze.py", "tools/music/compose.py",
            "tests/toolkit/check.py")}
        self.binary_hash = digest(self.harness)
        self.native = self.fm.Native(self.harness)
        self.metadata = self.native.metadata
        self.log = self.out / "takes"
        self.inputs = self.out / "inputs"
        self.inputs.mkdir(exist_ok=True)
        self.quiet = self.repo / "tests/golden/48000/quiet.tl"
        self.canonical = okay(self.fm.describe(self.quiet, harness=self.harness, detail="full"))["canonical"]
        self.a = self.fixture("a", level=0)
        self.b = self.fixture("b", level=1)
        self.c = self.fixture("c", level=2)

    def fixture(self, name, level, rate=48000, silent=True):
        lines = [l.split() for l in self.canonical.splitlines() if l.strip()]
        result = []
        for line in lines:
            if line[0] == "hash":
                continue
            if line[0] == "fs":
                line[1] = str(rate)
            if line[0] == "length_ns" and silent:
                line[1] = str((480 * 1000000000 + rate - 1) // rate)
            if line[0] == "patch" and line[2] == "0":
                line[4] = str(level)
            if line[0][0].isdigit() and silent:
                if line[3] != "on":
                    continue
                line[0], line[5] = "0", "0"
            result.append(" ".join(line))
        path = self.inputs / (name + ".tl")
        path.write_text("\n".join(result) + "\n")
        return path

    def case(self, name, fn):
        start = time.monotonic()
        try:
            evidence = fn()
            self.results.append(dict(name=name, pass_=True, evidence=evidence, seconds=round(time.monotonic() - start, 6)))
        except Exception as error:
            self.results.append(dict(name=name, pass_=False, error=type(error).__name__ + ": " + str(error), traceback=traceback.format_exc(), seconds=round(time.monotonic() - start, 6)))
            print("TOOLKIT fail " + name + ": " + str(error)[:600], flush=True)

    def ownership(self, reply):
        rows = reply["rows"]
        lines = [l.split() for l in reply["canonical"].splitlines() if l.strip()]
        need(len(rows) == len(lines), "canonical line omitted/duplicated")
        statements = self.metadata.statements
        total = 0
        for tokens, row in zip(lines, rows):
            descriptor = statements[row["id"]]
            word = int(descriptor["word_position"])
            need(tokens[word] == row["statement"], "wrong statement dispatch")
            expected = {}
            for position, token in enumerate(tokens):
                if position == word:
                    continue
                edge = re.fullmatch(r"([^>]+)>([^:]+):([^:]+)(?::([^:]+))?", token)
                if row["statement"] == "route" and position >= 4:
                    need(edge, "canonical edge malformed")
                    expected.update(((position, part), value) for part, value in zip(("FROM", "TO", "WEIGHT", "KIND"), edge.groups()) if value is not None)
                elif row["statement"] == "engine" and position >= 2:
                    key, value = token.split("=", 1)
                    expected[position, key] = value
                else:
                    expected[position, None] = token
            actual = {}
            for field in row["fields"]:
                key = field["token"], field["component"]
                need(key not in actual, "duplicate physical leaf")
                actual[key] = str(field["raw"])
            need(actual == expected, "physical values differ for " + row["id"])
            total += len(expected)
        return total

    def golden(self, path):
        reply = okay(self.fm.describe(path, harness=self.harness, detail="full"))
        need(reply["record"] is None and reply["wav"] is None, "temporary artifact path leaked")
        need(reply["record_sha256"] == hashlib.sha256(reply["canonical"].encode()).hexdigest(), "canonical identity differs")
        count = self.ownership(reply)
        comparison = okay(self.fm.compare(path, path, harness=self.harness))
        need(comparison["same_hash"] is True and comparison["same_record"] is True, "self-comparison identity failed")
        need(not any(comparison["record"].values()), "self-comparison changed a field")
        for key, delta in comparison["play"].items():
            need(delta[0] == delta[1] and (delta[2] == 0 or delta[0] is None and delta[2] is None), "self delta is nonzero: " + key)
        return dict(record_sha256=reply["record_sha256"], leaves=count, hash=reply["hash"])

    def metadata_omission(self):
        rows = [copy.deepcopy(r) for r in self.metadata.rows if r.get("id") != "patch_level_db10"]
        rows[-1]["count"] = str(int(rows[-1]["count"]) - 1)
        caught = False
        try:
            self.fm.Metadata(rows, self.metadata.generators).canonical(self.canonical.encode())
        except self.fm.ToolError:
            caught = True
        need(caught, "adjusted-count PARAM omission escaped completeness")
        return dict(mutation="remove patch_level_db10 and adjust count", rejected=True)

    def asymmetric_compare(self):
        left = self.fixture("audible-a", level=0, silent=False)
        right = self.fixture("audible-b", level=60, silent=False)
        a = okay(self.fm.describe(left, harness=self.harness, detail="full"))
        b = okay(self.fm.describe(right, harness=self.harness, detail="full"))
        ab = okay(self.fm.compare(left, right, harness=self.harness))
        ba = okay(self.fm.compare(right, left, harness=self.harness))
        need(len(ab["record"]["changed"]) == 1 and ab["record"]["changed"][0]["field"] == "LEVEL_DB10", "single field edit did not produce exactly one change")
        need(not ab["record"]["added"] and not ab["record"]["removed"], "single field edit changed identity")
        def verify(delta):
            expected = [a["peak_dbfs"], b["peak_dbfs"], b["peak_dbfs"] - a["peak_dbfs"]]
            need(delta == expected and expected[2] != 0, "delta orientation/units differ: " + repr(delta))
        verify(ab["play"]["peak_dbfs"])
        need(ba["play"]["peak_dbfs"][2] == -ab["play"]["peak_dbfs"][2], "reversed pair did not negate delta")
        swapped = [ab["play"]["peak_dbfs"][1], ab["play"]["peak_dbfs"][0], -ab["play"]["peak_dbfs"][2]]
        caught = False
        try:
            verify(swapped)
        except AssertionError:
            caught = True
        need(caught, "swapped delta mutant escaped asymmetric oracle")
        return dict(delta=ab["play"]["peak_dbfs"], swapped_mutant_rejected=True)

    def checks_semantics(self):
        path = self.inputs / "wrong-hash.tl"
        path.write_text("\n".join("hash 0000000000000000" if line.startswith("hash ") else line for line in self.canonical.splitlines()) + "\n")
        failed = okay(self.fm.compare(path, self.quiet, harness=self.harness), 3)
        need(failed["checks_pass"] is False, "side mismatch lost from aggregate measured verdict")
        unchecked = okay(self.fm.render([self.a], harness=self.harness, out=self.out / "unchecked"))
        need(unchecked["match"] is None and unchecked["checks_pass"] is None, "absent expected hash passed")
        unknown = okay(self.fm.render([self.quiet], harness=self.harness, out=self.out / "unknown-target", target={"not_measured": (0, 1)}))
        need(unknown["checks_pass"] is None, "missing target measurement passed")
        outside = okay(self.fm.render([self.quiet], harness=self.harness, out=self.out / "failed-target", target={"peak_dbfs": (-200, -199)}))
        need(outside["checks_pass"] is False, "measured target failure passed")
        return dict(mismatch=False, absent_hash=None, unmeasured=None, measured_failure=False)

    def log_identities(self):
        self.first = okay(self.fm.render([self.a], harness=self.harness, log=self.log, wav=True, note_text="tabs\tand\nlines"))
        record = Path(self.first["record"])
        before = record.stat()
        repeated = okay(self.fm.render([self.a], harness=self.harness, log=self.log, wav=True))
        after = record.stat()
        need(repeated["record_sha256"] == self.first["record_sha256"] and (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns), "identical record was not reused")
        self.second = okay(self.fm.render([self.b], harness=self.harness, log=self.log, wav=True))
        need(self.first["hash"] == self.second["hash"] and self.first["record_sha256"] != self.second["record_sha256"], "equal-audio distinct records were lost")
        child = okay(self.fm.render([self.c], harness=self.harness, log=self.log, parent_record=self.first["record_sha256"]))
        need(child["parent_record_sha256"] == self.first["record_sha256"] and child["parent"] == self.first["hash"], "explicit parent differs")
        rows = okay(self.fm.log_show(self.log, detail="full"))["rows"]
        need(len(rows) == 4 and rows[0]["note"] == "tabs\tand\nlines", "append/note roundtrip failed")
        change = json.loads(rows[-1]["changed"])["changed"]
        need(len(change) == 1 and change[0]["a"] == 0 and change[0]["b"] == 2, "changed used latest rather than exact parent")
        replay = okay(self.fm.log_replay(self.log, harness=self.harness))
        need(len(replay["takes"]) == 4 and all(t["match"] is True for t in replay["takes"]), "separate take replay failed")
        return dict(takes=4, equal_audio=self.first["hash"], distinct_records=[self.first["record_sha256"], self.second["record_sha256"]])

    def wav_identities(self):
        lower = self.fixture("same-pcm-other-rate", level=0, rate=44100)
        a = okay(self.fm.render([self.a], harness=self.harness, log=self.log, wav=True))
        b = okay(self.fm.render([lower], harness=self.harness, log=self.log, wav=True))
        def pcm(path):
            with wave.open(str(path), "rb") as w:
                return w.readframes(w.getnframes()), w.getframerate()
        x, fx = pcm(a["wav"])
        y, fy = pcm(b["wav"])
        need(x == y and fx != fy and a["hash"] == b["hash"], "setup did not preserve PCM at differing rate")
        need(a["wav_sha256"] != b["wav_sha256"] and a["wav"] != b["wav"], "whole-WAV identity omitted header")
        return dict(frames=a["samples"], rates=[fx, fy], wavs=[a["wav_sha256"], b["wav_sha256"]])

    def cli(self, *args):
        run = subprocess.run([sys.executable, "-B", str(self.repo / "tools/fm.py"), "--harness", str(self.harness), "--json", *map(str, args)], text=True, capture_output=True, timeout=60)
        need(run.stdout.strip(), "CLI gave no structured response: " + run.stderr)
        value = json.loads(run.stdout)
        need(run.returncode == value["exit"], "process/reply exit mismatch")
        return value

    def restart(self):
        result = okay(self.cli("log", "replay", self.log))
        need(len(result["takes"]) == len(okay(self.fm.log_show(self.log, detail="full"))["rows"]), "restart lost take records")
        return dict(takes=len(result["takes"]))

    def corruptions(self):
        checks = []
        for kind in ("missing-record", "corrupt-record", "corrupt-wav", "log-audio-hash", "log-fs", "truncated-log"):
            directory = self.out / kind
            shutil.copytree(self.log, directory)
            rows = okay(self.fm.log_show(directory, detail="full"))["rows"]
            row = rows[0]
            record = directory / "records" / (row["record_sha256"] + ".tl")
            if kind == "missing-record":
                record.unlink()
            elif kind == "corrupt-record":
                record.write_bytes(record.read_bytes() + b"# changed bytes\n")
            elif kind == "corrupt-wav":
                wav = directory / "sounds" / (row["wav_sha256"] + ".wav")
                wav.write_bytes(wav.read_bytes() + b"changed")
            else:
                if kind in ("log-audio-hash", "log-fs"):
                    # With no child rows, native identity validation must catch
                    # this; a parent-lineage check cannot mask the defect.
                    rows = rows[:1]
                if kind == "log-audio-hash":
                    rows[0]["hash"] = "0000000000000000"
                if kind == "log-fs":
                    rows[0]["fs"] = "12345"
                text = io.StringIO(newline="")
                writer = csv.DictWriter(text, fieldnames=self.fm.LOG_COLUMNS, delimiter="\t", lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
                if kind == "truncated-log":
                    text = io.StringIO("\t".join(self.fm.LOG_COLUMNS) + "\n1\t1\n")
                (directory / "log.tsv").write_text(text.getvalue())
            result = self.fm.log_replay(directory, harness=self.harness)
            need(result.get("ok") is False and result.get("exit") == 5, kind + " failed to return structured host failure: " + str(result)[:700])
            checks.append(kind)
        return dict(rejected=checks)

    def three_golden_log(self):
        directory = self.out / "golden-takes"
        hashes = []
        for name in ("quiet", "bell", "tremolo"):
            path = self.repo / "tests/golden/48000" / (name + ".tl")
            result = okay(self.fm.render([path], harness=self.harness, log=directory))
            hashes.append(result["hash"])
        replay = okay(self.cli("log", "replay", directory))
        need([take["hash"] for take in replay["takes"]] == hashes
             and all(take["match"] is True for take in replay["takes"]), "three-golden log replay differs")
        return dict(goldens=["quiet", "bell", "tremolo"], hashes=hashes)

    def concurrent(self):
        count = len(okay(self.fm.log_show(self.log, detail="full"))["rows"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(lambda _: self.cli("render", self.a, "--log", self.log), range(4)))
        for output in outputs:
            okay(output)
        rows = okay(self.fm.log_show(self.log, detail="full"))["rows"]
        need(len(rows) == count + 4 and [int(r["take"]) for r in rows] == list(range(1, count + 5)), "concurrent append lost/duplicated sequence")
        return dict(processes=4, takes=len(rows))

    def interrupted(self):
        evidence = []
        for phase in ("before", "after"):
            directory = self.out / ("interrupt-" + phase)
            shutil.copytree(self.log, directory)
            count = len(okay(self.fm.log_show(directory, detail="full"))["rows"])
            code = '''import importlib.util,sys,os
from pathlib import Path
spec=importlib.util.spec_from_file_location("fm_crash",sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
real=m.os.replace
phase=sys.argv[5]
def replace(source,target):
 if Path(target).name=="log.tsv" and phase=="before": os._exit(93)
 real(source,target)
 if Path(target).name=="log.tsv" and phase=="after": os._exit(93)
m.os.replace=replace
m.render([sys.argv[3]],harness=Path(sys.argv[2]),log=Path(sys.argv[4]))
'''
            result = subprocess.run([sys.executable, "-B", "-c", code, str(self.repo / "tools/fm.py"), str(self.harness), str(self.c), str(directory), phase], capture_output=True, text=True, timeout=60)
            need(result.returncode == 93, "fault injection did not interrupt log publication: " + result.stderr)
            visible = okay(self.fm.log_show(directory, detail="full"))["rows"]
            need(len(visible) == count + (phase == "after"), "interrupted publication exposed partial transaction")
            recovered = okay(self.fm.render([self.c], harness=self.harness, log=directory))
            replay = okay(self.fm.log_replay(directory, harness=self.harness))
            need(len(replay["takes"]) == len(visible) + 1, "interrupted log did not recover")
            evidence.append(dict(phase=phase, visible_takes=len(visible), recovered_take=recovered["take"]))
        return evidence

    def bad_wav(self):
        path = self.out / "bad.wav"
        path.write_bytes(b"RIFF1234")
        reply = self.fm.compare(path, path, harness=self.harness)
        need(reply.get("ok") is False and reply.get("exit") == 1, "invalid WAV escaped structured input refusal")
        return dict(exit=reply["exit"])

    def metrics(self):
        result = okay(self.fm.render([self.quiet], harness=self.harness, out=self.out / "metrics"))
        need("rms" in result and "crest" in result, "native integer metrics omitted")
        return {key: result[key] for key in ("rms", "crest")}

    def invalid_target_parity(self):
        evidence = []
        for item in ("peak_dbfs=word", "peak_dbfs=1..word", "peak_dbfs=NaN"):
            result = self.fm.public(lambda: self.fm.targets([item]))()
            cli = self.cli("compare", self.quiet, self.quiet, "--target", item)
            need(result.get("ok") is False and result.get("exit") == 2,
                 "public target refusal lost usage exit2: " + str(result))
            need(cli.get("ok") is False and cli.get("exit") == result["exit"],
                 "CLI/public invalid target exit differs")
            evidence.append(dict(target=item, exit=2))
        return evidence

    def measurement_host_failure(self):
        native_run = self.fm.Native.run
        injected = []
        def run(native, args):
            if "--play" in args and "--wav" not in args:
                injected.append(True)
                return subprocess.CompletedProcess(args, 5, "", "injected host failure during isolated measurement\n")
            return native_run(native, args)
        self.fm.Native.run = run
        try:
            result = self.fm.describe(self.quiet, harness=self.harness, detail="full", measure=True)
        finally:
            self.fm.Native.run = native_run
        need(injected, "measurement failure injection was not reached")
        need(result.get("ok") is False and result.get("exit") == 5, "measurement swallowed native host failure: " + str(result)[:800])
        return dict(exit=5, injected_calls=len(injected))

    def mixed_wav(self):
        rendered = okay(self.fm.render([self.quiet], harness=self.harness, out=self.out / "mix-comparison", wav=True))
        mixed = okay(self.fm.compare(self.quiet, rendered["wav"], harness=self.harness))
        need(mixed["same_hash"] is True and mixed["same_record"] is None, "mixed PCM identity differs or fabricates record identity")
        paired = okay(self.fm.compare(rendered["wav"], rendered["wav"], harness=self.harness, measure=True, note=880000))
        need(paired["same_hash"] is True and paired["same_record"] is None, "WAV self identity failed")
        need(paired.get("whole_mix") is not None, "WAV measurement deltas absent")
        profile = paired["a"].get("note_profile", paired["a"].get("whole_mix", {}).get("note_profile"))
        need(profile and profile["note_millihz"] == 880000 and profile["kind"] == "external_wav_note", "external note assumptions missing")
        return dict(hash=rendered["hash"], profile=profile)

    def unavailable_wav_notes(self):
        evidence = []
        for frames in (2, 48000):
            path = self.out / ("silent-" + str(frames) + ".wav")
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(48000)
                audio.writeframes(b"\0\0" * frames)
            reply = okay(self.fm.compare(path, path, harness=self.harness, measure=True, note=440000))
            for side in (reply["a"], reply["b"]):
                need(side["note_measured"] is False and side["note_descriptors"] is None
                     and side["note_reason"], "silent/short WAV fabricated note descriptors")
                need(side["descriptors"]["integrated"] is None, "silent WAV fabricated integrated loudness")
            need(reply["checks_pass"] is None, "undefined measurements passed applicable checks")
            evidence.append(dict(frames=frames, note_measured=False, reason=reply["a"]["note_reason"]))
        return evidence

    def replay_hash_mismatch(self):
        directory = self.out / "replay-mismatch"
        original = okay(self.fm.render([self.a], harness=self.harness, log=directory))
        rows = okay(self.fm.log_show(directory, detail="full"))["rows"]
        row = rows[0]
        old_record = Path(original["record"])
        expected = "0000000000000000"
        need(expected != original["hash"], "mismatch setup accidentally matches")
        data = old_record.read_bytes().replace(("hash " + original["hash"]).encode(), ("hash " + expected).encode())
        record_sha = hashlib.sha256(data).hexdigest()
        need(record_sha != original["record_sha256"], "expected hash was not mutated")
        (directory / "records" / (record_sha + ".tl")).write_bytes(data)
        row["record_sha256"], row["hash"] = record_sha, expected
        text = io.StringIO(newline="")
        writer = csv.DictWriter(text, fieldnames=self.fm.LOG_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        (directory / "log.tsv").write_text(text.getvalue())
        okay(self.fm.log_show(directory))
        replay = okay(self.fm.log_replay(directory, harness=self.harness), 3)
        need(len(replay["takes"]) == 1 and replay["takes"][0]["match"] is False
             and replay["takes"][0]["exit"] == 3, "replay lost native expected-hash mismatch")
        need(replay["takes"][0]["hash"] == original["hash"], "replay replaced actual audio identity")
        return dict(stored_record_sha256=record_sha, expected_hash=expected,
                    actual_hash=original["hash"], exit=3)

    def geometry_case(self, name, points, expected):
        path = self.inputs / ("geometry-" + name + ".tl")
        text = self.a.read_text().replace("timeline draft1", "timeline draft2")
        text += "space stereo\n" + "".join("0 golden 0 pos %d %d %d\n" % point for point in points)
        path.write_text(text)
        reply = okay(self.fm.describe(path, harness=self.harness, detail="full"))
        voice = reply["space"]["voices"]["0"]
        if expected is None:
            need(voice["extent_deg"] is None, "undefined waypoint produced finite extent")
            need(any(value for key, value in voice.items() if "reason" in key), "undefined extent lacks reason")
        else:
            need(abs(voice["extent_deg"] - expected) < 1e-9, "declared-place unwrapped extent differs: " + str(voice))
        if name in ("origin", "pole"):
            need(voice["first_pos"]["azimuth_deg"] is None, "undefined azimuth was invented")
        return dict(extent_deg=voice["extent_deg"], first_pos=voice["first_pos"])

    def spatial_golden(self, path):
        reply = okay(self.fm.describe(path, harness=self.harness, detail="full"))
        self.ownership(reply)
        spatial = reply.get("space")
        count = 0
        if spatial:
            for voice in spatial["voices"].values():
                point = voice["first_pos"]
                if not point:
                    continue
                x, y = point["x"], point["y"]
                if x or y:
                    need(abs(point["azimuth_deg"] - math.degrees(math.atan2(x, y))) <= 0.1, "spatial azimuth disagrees with position inverse")
                    count += 1
        return dict(voices_checked=count, hash=reply["hash"])

    def binaural_measure(self, path):
        reply = okay(self.fm.describe(path, harness=self.harness, detail="full", measure=True))
        image = reply["space"]["image"]
        need(image is not None and image["fs"] == reply["fs"] and image["window_frames"] == max(1, round(reply["fs"] / 10)), "image rate/window provenance missing")
        need(sum(w["frames"] for w in image["windows"]) == reply["samples"], "image windows omit/duplicate samples")
        offset = 0
        for window in image["windows"]:
            need(window["start_frame"] == offset, "image window overlap/gap")
            offset += window["frames"]
        need(reply["space"]["per_voice_image"] is None and reply["space"]["per_voice_image_reason"], "per-voice image claimed without output")
        return dict(fs=image["fs"], window_frames=image["window_frames"], windows=image["windows"], descriptors=reply["descriptors"])

    def recipe_measure(self):
        reply = okay(self.fm.describe("bell", harness=self.harness, detail="full", measure=True))
        descriptor = reply["descriptors"]["bell"]
        need(descriptor["measured"] and descriptor["profile"]["kind"] == "isolated_patch_note", "recipe isolated measure absent")
        need(descriptor["written_hash"] and descriptor["measured_hash"], "two-render provenance missing")
        analysis, compose = self.fm.analysis_module(), self.fm.compose_module()
        analysis.DESC_DIR = str(self.out / "reference-describe")
        original_play = compose.play
        def selected_native(files, wav=None, merged=None):
            args = ["--play"]
            if wav:
                args += ["--wav", wav]
            if merged:
                args += ["--merged", merged]
            result = self.native.run([*args, "--", *files])
            fields = self.fm.play_fields(result.stdout)
            line = next(line for line in result.stdout.splitlines() if line.startswith("PLAY "))
            return result.returncode, fields, line
        compose.play = selected_native
        try:
            tags, _ = analysis.recipe_metadata(compose)
            reference = analysis.describe_one(compose, tags, "bell", descriptor["profile"]["note_millihz"], analysis.DESC_VEL, analysis.DESC_HOLD)
        finally:
            compose.play = original_play
        for field, value in descriptor["values"].items():
            need(value == reference[field], "recipe differs from existing D83 protocol: " + field + " " + repr((value, reference[field])))
        return dict(profile=descriptor["profile"], values=descriptor["values"], exact_existing_describe=True)

    def underscore_names(self):
        accepted = okay(self.fm.describe(self.quiet, harness=self.harness, detail="full"))
        original = next(iter(accepted["patches"]))
        evidence = []
        for name in ("_lead", "_rows"):
            lines = [line.split() for line in accepted["canonical"].splitlines()]
            for row, tokens in zip(accepted["rows"], lines):
                for field in row["fields"]:
                    if field["name"] == "PATCH" and field["raw"] == original:
                        tokens[field["token"]] = name
            path = self.inputs / (name + ".tl")
            path.write_text("".join(" ".join(line) + "\n" for line in lines))
            result = okay(self.fm.describe(path, harness=self.harness, detail="full", measure=True))
            need(name in result["patches"] and name in result["descriptors"],
                 "valid underscore patch group disappeared")
            need(result["descriptors"][name]["measured"] is True,
                 "underscore patch lost its requested measurement")
            need(any(row["raw"].get("PATCH") == name for row in result["rows"]),
                 "underscore raw patch name changed")
            evidence.append(dict(name=name, hash=result["hash"], measured=True))
        raw = {"_lead": {"_rows": "literal", "nested": {"_record_bytes": "literal"}}}
        need(self.fm.clean(raw) == raw, "ordinary caller keys were treated as internal metadata")
        return evidence

    def text_ingress(self):
        evidence = []
        for fs in (48000, 44100):
            path = self.fixture("text-" + str(fs), level=0, rate=fs)
            file_reply = okay(self.fm.render([path], harness=self.harness,
                                            out=self.out / ("file-" + str(fs)), wav=True))
            text_reply = okay(self.fm.render(text=path.read_text(), harness=self.harness,
                                            out=self.out / ("text-" + str(fs)), wav=True))
            checked = okay(self.fm.check_patch(text=path.read_text(), harness=self.harness))
            for key in ("canonical", "record_sha256", "hash", "fs", "samples", "metrics", "checks"):
                need(file_reply[key] == text_reply[key] == checked[key], "text/file check differs: " + key)
            need(file_reply["wav_sha256"] == text_reply["wav_sha256"], "text changed whole WAV")
            need(Path(file_reply["wav"]).read_bytes() == Path(text_reply["wav"]).read_bytes(), "text changed WAV bytes")
            evidence.append(dict(fs=fs, record_sha256=text_reply["record_sha256"], wav_sha256=text_reply["wav_sha256"]))
        fragment = okay(self.fm.load_patch("bell", patch="text_patch"))["text"]
        fragment_file = self.inputs / "fragment.tl"
        fragment_file.write_text(fragment)
        a = okay(self.fm.check_patch([fragment_file], patch_name="text_patch", harness=self.harness))
        b = okay(self.fm.check_patch(text=fragment, patch_name="text_patch", harness=self.harness))
        need(a["record_sha256"] == b["record_sha256"] and a["hash"] == b["hash"], "fragment submission changed native output")
        rejected_dir = self.out / "rejected-text"
        bad = self.fm.render(text="timeline draft1\nfs 0\n", harness=self.harness, out=rejected_dir, wav=True)
        need(bad.get("ok") is False and bad["exit"] == 1 and not rejected_dir.exists(), "invalid text published a take")
        refusal = bad["refusals"][0]
        need(refusal["file"] == "input.tl" and refusal["line"] == 2 and refusal["field"] == "HZ",
             "text refusal lost logical filename/line/native field")
        need("fm-input-" not in json.dumps(bad), "text refusal leaked ephemeral input path")
        cli = okay(self.cli("render", "--text", self.a.read_text(), "--out", self.out / "text-cli", "--wav"))
        direct = okay(self.fm.check_patch(text=self.a.read_text(), harness=self.harness))
        need(cli["record_sha256"] == direct["record_sha256"], "CLI text render differs")
        cli_check = okay(self.cli("check_patch", "--text", fragment, "--patch-name", "text_patch"))
        need(cli_check["record_sha256"] == b["record_sha256"], "CLI text fragment differs")
        return dict(rates=evidence, named_fragment=True, logical_refusal=refusal, invalid_text_published=False)

    def artifact_chunks(self):
        directory = self.out / "chunk-source"
        rendered = okay(self.fm.render([self.a], harness=self.harness, out=directory, wav=True))
        evidence = []
        for kind in ("record", "wav"):
            expected = Path(rendered[kind]).read_bytes()
            digest_value = rendered[kind + "_sha256"]
            chunks, offset = [], 0
            while True:
                chunk = okay(self.fm.artifact_read(directory, kind, digest_value, offset=offset, length=97))
                data = base64.b64decode(chunk["data_base64"], validate=True)
                need(chunk["sha256"] == digest_value and chunk["size"] == len(expected), "chunk lost whole-file identity")
                need(chunk["offset"] == offset and chunk["length"] == len(data) <= 97, "wrong bounded chunk extent")
                chunks.append(data)
                offset += len(data)
                need(chunk["eof"] == (offset == len(expected)), "wrong chunk EOF")
                if chunk["eof"]:
                    break
                need(data, "nonterminal empty chunk")
            joined = b"".join(chunks)
            need(joined == expected and hashlib.sha256(joined).hexdigest() == digest_value, "chunks did not reconstruct artifact")
            if kind == "wav":
                with wave.open(io.BytesIO(joined), "rb") as got, wave.open(io.BytesIO(expected), "rb") as want:
                    need(got.getparams() == want.getparams() and got.readframes(got.getnframes()) == want.readframes(want.getnframes()), "WAV samples changed")
            eof = okay(self.fm.artifact_read(directory, kind, digest_value, offset=len(expected)))
            need(eof["length"] == 0 and eof["data_base64"] == "" and eof["eof"], "EOF offset failed")
            too_far = self.fm.artifact_read(directory, kind, digest_value, offset=len(expected) + 1)
            need(too_far.get("ok") is False and too_far["exit"] == 2, "out-of-bounds offset accepted")
            cli = okay(self.cli("artifact_read", directory, kind, digest_value, "--length", 7))
            need(base64.b64decode(cli["data_base64"]) == expected[:7], "CLI artifact bytes differ")
            evidence.append(dict(kind=kind, size=len(expected), sha256=digest_value, chunks=len(chunks)))
        path = Path(rendered["wav"])
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        corrupt = self.fm.artifact_read(directory, "wav", rendered["wav_sha256"], length=1)
        need(corrupt.get("ok") is False and corrupt["exit"] == 5 and "data_base64" not in corrupt,
             "chunk escaped whole-artifact corruption verification")
        missing = self.fm.artifact_read(directory, "record", "0" * 64)
        need(missing.get("ok") is False and missing["exit"] == 5, "missing artifact lost host exit5")
        return dict(artifacts=evidence, corruption_outside_requested_chunk_rejected=True, missing_exit=5)

    def absent_empty_logs(self):
        missing, no_log, empty = (self.out / name for name in ("absent-log", "directory-no-log", "empty-log"))
        no_log.mkdir()
        empty.mkdir()
        (empty / "log.tsv").write_text("\t".join(self.fm.LOG_COLUMNS) + "\n")
        before = list(no_log.iterdir())
        native = self.fm.Native
        attempted = []
        def forbidden(*args, **kwargs):
            attempted.append(True)
            raise AssertionError("invalid/empty log constructed native renderer")
        self.fm.Native = forbidden
        try:
            for directory in (missing, no_log):
                for operation in (self.fm.log_show, self.fm.log_replay):
                    reply = operation(directory)
                    need(reply.get("ok") is False and reply["exit"] == 5, "absent log reported success")
            shown = okay(self.fm.log_show(empty))
            need(shown["count"] == 0 and shown["rows"] == [], "empty existing log show failed")
            replay = self.fm.log_replay(empty)
            need(replay.get("ok") is False and replay["exit"] == 2 and replay["count"] == 0,
                 "empty log replay claimed successful evidence")
        finally:
            self.fm.Native = native
        need(not attempted and not missing.exists() and list(no_log.iterdir()) == before,
             "missing log created files or invoked native")
        return dict(native_attempts=0, missing_created=False, missing_exit=5, empty_replay_exit=2)

    def take_log_projection(self):
        directory = self.out / "take-projection"
        okay(self.fm.render([self.a], harness=self.harness, log=directory, note_text="retained\n" * 800))
        original = okay(self.fm.log_show(directory, detail="full"))
        need(set(original) == {"version", "ok", "exit", "count", "rows", "text"}, "full take reply changed shape")
        compact = okay(self.fm.log_show(directory))
        raw = (directory / "log.tsv").read_bytes()
        need(compact["index_sha256"] == digest(directory / "log.tsv") and compact["count"] == 1
             and compact["text"] is None and compact["rows"][0]["note"] is None
             and compact["rows"][0]["omitted"] == ["note", "changed", "space"],
             "take projection lost identity or omission disclosure")
        require_bytes = self.bounded(compact)
        need(require_bytes <= 15360, "take projection consumed receipt reserve")
        chunk = okay(self.fm.artifact_read(directory, "take_log", compact["index_sha256"]))
        need(base64.b64decode(chunk["data_base64"], validate=True) == raw and chunk["eof"], "take TSV bytes changed")
        existing, attempted = self.fm.existing_log, []
        def forbidden(*args, **kwargs):
            attempted.append(True)
            raise AssertionError("invalid take options performed I/O")
        options = [dict(offset=value) for value in (True, 1.0, "0", None, -1)]
        options += [dict(limit=value) for value in (True, 1.0, "20", None, 0, 101)]
        options += [dict(expected_index_sha256=True), dict(expected_index_sha256="A" * 64),
                    dict(detail="full", offset=1), dict(detail="full", limit=1),
                    dict(detail="full", expected_index_sha256="0" * 64), dict(kind="operations", detail="full")]
        self.fm.existing_log = forbidden
        try:
            for arguments in options:
                bad = self.fm.log_show(directory, **arguments)
                need(bad["exit"] == 2, "invalid take option lost usage2")
        finally:
            self.fm.existing_log = existing
        need(not attempted, "invalid take options resolved a log")
        # Externally authored scalar metadata is not silently truncated. It
        # remains accessible in full mode, while an unbounded row refuses.
        changed = copy.deepcopy(original["rows"])
        changed[0]["utc"] = "x" * 20000
        text = io.StringIO(newline="")
        writer = csv.DictWriter(text, fieldnames=self.fm.LOG_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(changed)
        (directory / "log.tsv").write_text(text.getvalue())
        need(self.fm.log_show(directory)["exit"] == 2, "oversized projected metadata exceeded cap")
        need(okay(self.fm.log_show(directory, detail="full"))["rows"] == changed, "full mode truncated scalar metadata")
        need(self.fm.log_show(directory, offset=10 ** 20000)["exit"] == 2, "unrepresentable paging metadata lost usage2")
        return dict(compact_bytes=require_bytes, preserved_TSV_bytes=len(raw), prework_refusals=len(options),
                    metadata_overflow_exit=2, legacy_full_unchanged=True)

    def utf8_artifact_options(self):
        directory = self.out / "utf8-artifacts"
        (directory / "receipts").mkdir(parents=True)
        raw = b"readable prefix\n" + b"x" * 100 + b"\xff"
        identity = hashlib.sha256(raw).hexdigest()
        (directory / "receipts" / (identity + ".json")).write_bytes(raw)
        legacy = okay(self.fm.artifact_read(directory, "receipt", identity))
        need(base64.b64decode(legacy["data_base64"], validate=True) == raw and "encoding" not in legacy,
             "legacy artifact mode gained text validation or fields")
        bad = self.fm.artifact_read(directory, "receipt", identity, encoding="utf8", length=1)
        need(bad["exit"] == 5 and "text" not in bad, "UTF8 validation did not cover the whole artifact")
        original, attempted = Path.open, []
        def forbidden(*args, **kwargs):
            attempted.append(True)
            raise AssertionError("invalid UTF8 option opened an artifact")
        options = [dict(encoding=value) for value in (True, None, 1, "unknown")]
        options += [dict(encoding="utf8", length=value) for value in (True, 1.0, "1", None, 0, 8193)]
        options += [dict(encoding="utf8", offset=value) for value in (True, 1.0, "0", None, -1)]
        options += [dict(encoding="utf8", kind="wav"), dict(encoding="utf8", kind="png")]
        Path.open = forbidden
        try:
            for change in options:
                arguments = dict(directory=directory, kind="receipt", digest=identity)
                arguments.update(change)
                need(self.fm.artifact_read(**arguments)["exit"] == 2, "invalid UTF8 option lost usage2")
        finally:
            Path.open = original
        need(not attempted, "invalid UTF8 arguments performed I/O")
        return dict(prework_refusals=len(options), invalid_UTF8_outside_fragment_exit=5,
                    legacy_binary_bytes_preserved=True)

    def strict_options(self):
        native, attempted = self.fm.Native, []
        def forbidden(*args, **kwargs):
            attempted.append(True)
            raise AssertionError("invalid option reached native")
        calls = []
        for bad in (1, "false", 0.0, None):
            calls.extend((lambda bad=bad: self.fm.render([self.a], wav=bad, out=self.out / "invalid-option"),
                          lambda bad=bad: self.fm.describe(self.a, measure=bad),
                          lambda bad=bad: self.fm.compare(self.a, self.a, measure=bad)))
        for bad in (True, "48000", 48000.0):
            calls.extend((lambda bad=bad: self.fm.check_patch([self.a], fs=bad),
                          lambda bad=bad: self.fm.describe(self.a, note=bad),
                          lambda bad=bad: self.fm.compare(self.a, self.a, note=bad),
                          lambda bad=bad: self.fm.artifact_read(self.out, "record", "0" * 64, offset=bad),
                          lambda bad=bad: self.fm.artifact_read(self.out, "record", "0" * 64, length=bad)))
        for bad in (0, -1):
            for measured in (False, True):
                calls.extend((lambda bad=bad, measured=measured: self.fm.describe(self.a, note=bad, measure=measured),
                              lambda bad=bad, measured=measured: self.fm.compare(self.a, self.a, note=bad, measure=measured)))
        calls.extend((lambda: self.fm.render([self.a], text=self.a.read_text(), out=self.out / "invalid-option"),
                      lambda: self.fm.check_patch(), lambda: self.fm.check_patch(text=" \n"),
                      lambda: self.fm.check_patch(text=123), lambda: self.fm.check_patch(files=str(self.a)),
                      lambda: self.fm.describe(self.a, detail="unknown"),
                      lambda: self.fm.compare(self.a, self.a, detail="unknown")))
        self.fm.Native = forbidden
        try:
            for call in calls:
                reply = call()
                need(reply.get("ok") is False and reply["exit"] == 2, "invalid option lost usage2: " + str(reply))
        finally:
            self.fm.Native = native
        need(not attempted and not (self.out / "invalid-option").exists(), "invalid option produced side effects")
        duplicate = self.fm.public(lambda: self.fm.targets(["peak_dbfs=-10", "peak_dbfs=-20"]))()
        cli = self.cli("render", self.a, "--out", self.out / "duplicate-target", "--target", "peak_dbfs=-10", "--target", "peak_dbfs=-20")
        for reply in (duplicate, cli):
            need(reply.get("ok") is False and reply["exit"] == 2 and "duplicate" in reply["message"], "duplicate target silently replaced requested check")
        need(not (self.out / "duplicate-target").exists(), "duplicate target retained a take")
        return dict(invalid_options=len(calls), native_attempts=0, duplicate_target_exit=2)

    def description_detail(self):
        full = okay(self.fm.describe(self.quiet, harness=self.harness, measure=True, detail="full"))
        summary = okay(self.fm.describe(self.quiet, harness=self.harness, measure=True, detail="summary"))
        for key in ("canonical", "record_sha256", "hash", "metrics", "descriptors", "space", "voices", "whole_mix"):
            need(summary[key] == full[key], "summary changed measured content: " + key)
        need(summary["rows"] is None and summary["events"] is None and summary["detail"] == "summary",
             "summary omitted detail without explicit nulls/mode")
        for key, count in summary["description_counts"].items():
            need(count == len(full.get(key) or []), "summary count differs: " + key)
        keys = ("id", "statement", "line_index", "raw", "human")
        for name, rows in summary["patches"].items():
            need(rows == [{key: row[key] for key in keys} for row in full["patches"][name]], "summary patch projection lost values")
        need(summary["omitted"] == ["rows", "events", "field_metadata"], "summary omissions not disclosed")
        cli = okay(self.cli("describe", self.quiet, "--detail", "full"))
        need(cli["rows"] == full["rows"], "CLI full detail lost canonical token ownership")
        return dict(counts=summary["description_counts"], summary_bytes=len(json.dumps(summary)),
                    full_bytes=len(json.dumps(full)), measured_values_equal=True)

    def comparison_detail(self):
        left = self.fixture("comparison-detail-a", level=0, silent=False)
        right = self.fixture("comparison-detail-b", level=60, silent=False)
        for path, x in ((left, -500), (right, 500)):
            path.write_text(path.read_text().replace("timeline draft1", "timeline draft2")
                            + "space stereo\n0 golden 0 pos %d 1000 0\n" % x)
        target = {"a.peak_dbfs": (-120, 0)}
        full = okay(self.fm.compare(left, right, harness=self.harness, measure=True, detail="full", target=target))
        summary = okay(self.fm.compare(left, right, harness=self.harness, measure=True, target=target, detail="summary"))
        for key in full.keys() - {"a", "b"}:
            need(summary[key] == full[key], "comparison summary changed computed result: " + key)
        need(full["play"]["peak_dbfs"][2] != 0 and full["record"]["changed"], "comparison parity fixture has no difference")
        need(next(c for c in full["checks"] if c["name"] == "a.peak_dbfs")["status"] == "pass",
             "comparison parity fixture did not exercise a measured target check")
        compact_keys = ("id", "statement", "line_index", "raw", "human")
        for side in ("a", "b"):
            original, projected = full[side], summary[side]
            for key in ("canonical", "record_sha256", "hash", "metrics", "descriptors", "whole_mix"):
                need(original[key] == projected[key], "comparison side lost measurement or identity: " + key)
            need(projected["rows"] is None and projected["events"] is None
                 and projected["description_counts"]["rows"] == len(original["rows"]), "comparison did not project detailed rows")
            for key in original["space"].keys() - {"header"}:
                need(projected["space"][key] == original["space"][key], "spatial measurement changed: " + key)
            need(projected["space"]["header"] == [{key: row[key] for key in compact_keys}
                 for row in original["space"]["header"]], "spatial header values lost in projection")
        cli = okay(self.cli("compare", left, right, "--measure", "--detail", "full"))
        need(cli["a"]["rows"] == full["a"]["rows"] and cli["record"] == full["record"], "CLI comparison full detail differs")
        rendered = okay(self.fm.render([left], harness=self.harness, out=self.out / "detail-mixed", wav=True))
        mixed_full = okay(self.fm.compare(left, rendered["wav"], harness=self.harness, measure=True, detail="full"))
        mixed_summary = okay(self.fm.compare(left, rendered["wav"], harness=self.harness, measure=True, detail="summary"))
        for key in mixed_full.keys() - {"a", "b"}:
            need(mixed_summary[key] == mixed_full[key], "mixed WAV summary changed computed result: " + key)
        need({k: v for k, v in mixed_summary["b"].items() if k != "detail"}
             == {k: v for k, v in mixed_full["b"].items() if k != "detail"}, "WAV side gained fabricated TL metadata")
        return dict(summary_bytes=len(json.dumps(summary)), full_bytes=len(json.dumps(full)),
                    delta=full["play"]["peak_dbfs"], changed_fields=len(full["record"]["changed"]),
                    spatial_measurements_equal=True, mixed_WAV_equal=True)

    @staticmethod
    def document(reply):
        return json.dumps(reply, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")

    @staticmethod
    def bounded(reply):
        size = len(json.dumps(reply, ensure_ascii=True, indent=2, allow_nan=False).encode("utf-8"))
        need(size <= 16384, "reply exceeds conservative JSON byte budget: " + str(size))
        return size

    def compact_measurements(self):
        left = self.fixture("compact-stereo", level=0, silent=False)
        left.write_text(left.read_text().replace("timeline draft1", "timeline draft2")
                        + "space stereo\n0 golden 0 pos -500 1000 0\n")
        right = self.inputs / "compact-stereo-quiet.tl"
        right.write_text(left.read_text() + "gain -60\n")
        rendered = okay(self.fm.render([left], harness=self.harness, out=self.out / "compact-wav", wav=True))
        evidence = []
        for a, b in ((left, right), (rendered["wav"], rendered["wav"]),
                     (left, rendered["wav"]), (self.quiet, rendered["wav"]), (rendered["wav"], self.quiet)):
            target = {"a.peak_dbfs": (-120, 0), "unavailable": (0, 1), "b.peak_dbfs": (-200, -199)}
            full = okay(self.fm.compare(a, b, harness=self.harness, measure=True, detail="full", target=target))
            compact = okay(self.fm.compare(a, b, harness=self.harness, measure=True, target=target))
            size = self.bounded(compact)
            need(compact["detail"] == "compact" and compact["checks_pass"] is False, "compact lost failed check")
            need(compact["full_result"] == dict(sha256=hashlib.sha256(self.document(full)).hexdigest(),
                                               size=len(self.document(full)), detail="full"), "full result identity differs")
            for key in ("same_hash", "same_record", "checks", "play", "record"):
                if compact.get(key) is not None:
                    need(compact[key] == full[key], "compact changed retained value: " + key)
            need(compact["check_counts"] == {s: sum(c["status"] == s for c in full["checks"])
                                              for s in ("pass", "fail", "unchecked")}, "check counts differ")
            for side in ("a", "b"):
                for key in ("hash", "record_sha256", "wav_sha256", "fs", "samples", "channels"):
                    need(compact[side].get(key) == full[side].get(key), "compact changed identity: " + key)
                for key in ("peak_dbfs", "rms_dbfs", "metrics", "descriptors"):
                    if compact[side].get(key) is not None:
                        need(compact[side][key] == full[side][key], "compact changed scalar measures")
            def images(obj):
                if isinstance(obj, dict):
                    for key, value in obj.items():
                        if key == "windows":
                            yield obj
                        yield from images(value)
                elif isinstance(obj, list):
                    for value in obj:
                        yield from images(value)
            for image in images(compact):
                need(image["windows"] is None and image["window_count"] == 30,
                     "stereo image windows escaped projection or count changed")
            evidence.append(dict(pair=[str(a), str(b)], compact_bytes=size, full_bytes=len(self.document(full))))
        default = okay(self.fm.describe(left, harness=self.harness, measure=True))
        need(default["canonical"] is None and default["description_counts"]["events"] == 3,
             "compact describe lost native event count")
        self.bounded(default)
        return evidence

    def result_pages(self):
        directory = self.inputs / "piece-\u00e9-\U0001f3b5"
        directory.mkdir()
        (directory / "session.tl").write_text(self.a.read_text())
        operations = ((self.fm.describe, (directory,)), (self.fm.compare, (self.a, self.b)))
        evidence = []
        for operation, sources in operations:
            full = okay(operation(*sources, harness=self.harness, detail="full"))
            document = self.document(full)
            expected = hashlib.sha256(document).hexdigest()
            joined, offset, pages = bytearray(), 0, 0
            while True:
                page = okay(operation(*sources, harness=self.harness, detail="page", offset=offset,
                                      length=8192, expected_result_sha256=expected))
                self.bounded(page)
                need(page["result_sha256"] == expected and page["total_bytes"] == len(document), "page identity differs")
                data = page["text"].encode("utf-8")
                need(page["length"] == len(data) <= 8192 and page["offset"] == offset, "page extent differs")
                joined.extend(data)
                offset += len(data)
                pages += 1
                if page["eof"]:
                    need(page["next_offset"] is None, "EOF has a next page")
                    break
                need(data and page["next_offset"] == offset, "result page stalled")
            need(bytes(joined) == document and json.loads(joined) == full, "pages did not reconstruct exact full result")
            eof = okay(operation(*sources, harness=self.harness, detail="page", offset=len(document)))
            need(eof["eof"] and eof["text"] == "" and eof["length"] == 0, "empty EOF failed")
            mismatch = operation(*sources, harness=self.harness, detail="page", expected_result_sha256="0" * 64)
            need(mismatch["exit"] == 2 and "text" not in mismatch, "wrong page identity leaked bytes")
            if operation == self.fm.describe:
                position = document.index("\U0001f3b5".encode("utf-8"))
                for bad_offset, length in ((position + 1, 8), (position, 1), (len(document) + 1, 8)):
                    bad = operation(*sources, harness=self.harness, detail="page", offset=bad_offset, length=length)
                    need(bad["exit"] == 2 and "text" not in bad, "invalid UTF-8 page accepted")
                (directory / "session.tl").write_text(self.b.read_text())
                changed = operation(*sources, harness=self.harness, detail="page", expected_result_sha256=expected)
                need(changed["exit"] == 2 and "text" not in changed, "changed source joined old pages")
            evidence.append(dict(operation=operation.__name__, bytes=len(document), pages=pages, sha256=expected))
        # Escape expansion must shorten pages while retaining all UTF-8 bytes.
        synthetic = dict(version=1, ok=True, exit=3, value='\U0001f3b5\\"\n' * 2500)
        expected = self.document(synthetic)
        joined, offset, pages = bytearray(), 0, 0
        while offset < len(expected):
            page = okay(self.fm.result_detail(synthetic, "page", offset, 8192, None), 3)
            self.bounded(page)
            joined.extend(page["text"].encode("utf-8"))
            offset += page["length"]
            pages += 1
        need(bytes(joined) == expected and pages > 3, "escaped JSON page stress did not reconstruct")
        return dict(native=evidence, escaped_pages=pages, preserved_hash_mismatch_exit=3)

    def compact_growth(self):
        evidence = []
        for duration, events, patches in ((1, 2, 1), (6, 2, 1), (1, 302, 1), (1, 2, 8)):
            name = "growth-{}-{}-{}".format(duration, events, patches)
            lines = ["timeline draft2", "engine " + self.metadata.engine, "fs 48000",
                     "length_ns " + str(duration * 10 ** 9), "streams notes", "space stereo"]
            for patch in range(patches):
                lines.extend((f"patch p{patch} 0 65536 180 0 0 256 2 2500 900 300",
                              f"patch p{patch} 1 131072 0 0 0 0 1 1200 600 400"))
            lines += ["voice 0 p0", "0 notes 0 pos -500 1000 0"]
            for event in range(events // 2):
                start = event * 2000000
                lines += [f"{start} notes 0 on 440000 24000", f"{start + 1000000} notes 0 off"]
            left, right = self.inputs / (name + ".tl"), self.inputs / (name + "-edited.tl")
            left.write_text("\n".join(lines) + "\n")
            right.write_text(left.read_text().replace(" 24000\n", " 22000\n"))
            full = okay(self.fm.compare(left, right, harness=self.harness, detail="full", measure=True))
            compact = okay(self.fm.compare(left, right, harness=self.harness, measure=True))
            size = self.bounded(compact)
            need(compact["record_counts"]["changed"] == events // 2, "dense diff count changed")
            need(compact["a"]["description_counts"]["patches"] == patches, "patch count changed")
            need(compact["a"]["hash"] == full["a"]["hash"] and compact["checks_pass"] == full["checks_pass"],
                 "growth projection changed computed result")
            evidence.append(dict(duration_s=duration, events=events, patches=patches,
                                 compact_bytes=size, full_bytes=len(self.document(full)), omissions=compact["omitted"]))
        full = okay(self.fm.compare(self.a, self.b, harness=self.harness, detail="full"))
        full["a"]["native"]["path"] = "\U0001f3b5" * 30000
        full["checks"] += [dict(name="check" + str(i), status="unchecked", actual=None, expected=True) for i in range(500)]
        full["checks_pass"] = None
        compact = self.fm.result_detail(full, "compact", 0, 4096, None)
        size = self.bounded(compact)
        need(compact["omitted"] == ["full_result"] and compact["checks_pass"] is None
             and compact["check_counts"]["unchecked"] >= 500, "minimal receipt lost unresolved checks")
        need("\U0001f3b5" not in json.dumps(compact, ensure_ascii=False), "fallback echoed unbounded metadata")
        return dict(independent_growth=evidence, fallback_bytes=size, fallback_check_counts=compact["check_counts"])

    def result_options(self):
        native, attempted = self.fm.Native, []
        def forbidden(*args, **kwargs):
            attempted.append(True)
            raise AssertionError("invalid result options invoked native")
        calls = []
        for operation, sources in ((self.fm.describe, (self.a,)), (self.fm.compare, (self.a, self.b))):
            for field in ("offset", "length"):
                for bad in (True, "1", 1.0, None):
                    calls.append(lambda operation=operation, sources=sources, field=field, bad=bad:
                                 operation(*sources, detail="page", **{field: bad}))
            for options in (dict(detail="page", offset=-1), dict(detail="page", length=0),
                            dict(detail="page", length=8193), dict(detail="page", expected_result_sha256="A" * 64),
                            dict(detail="page", expected_result_sha256=True), dict(length=8),
                            dict(detail="full", offset=1), dict(expected_result_sha256="0" * 64)):
                calls.append(lambda operation=operation, sources=sources, options=options: operation(*sources, **options))
        destination = self.out / "forbidden-view-output"
        for detail in ("compact", "page"):
            calls.append(lambda detail=detail: self.fm.describe(self.a, detail=detail, out=destination))
        self.fm.Native = forbidden
        try:
            for call in calls:
                result = call()
                need(result["exit"] == 2 and result["ok"] is False, "invalid view arguments did not refuse2")
        finally:
            self.fm.Native = native
        need(not attempted and not destination.exists(), "view refusal produced side effects")
        return dict(refusals=len(calls), native_calls=0, output_created=False)

    def window_fixture(self, name, *, rate=48000, duration=3050000000, x=-500):
        path = self.fixture(name, level=0, rate=rate, silent=False)
        text = re.sub(r"(?m)^length_ns \d+$", "length_ns " + str(duration), path.read_text())
        path.write_text(text.replace("timeline draft1", "timeline draft2")
                        + "space stereo\n0 golden 0 pos %d 1000 0\n" % x)
        return path

    def window_selections(self):
        left = self.window_fixture("window-selection-a")
        right = self.window_fixture("window-selection-b", x=500)
        full = okay(self.fm.describe(left, harness=self.harness, measure=True, detail="full"))
        document = self.document(full)
        identity = hashlib.sha256(document).hexdigest()
        selected = okay(self.fm.describe(left, harness=self.harness, measure=True, detail="windows",
                                         start_ns=100000000, end_ns=300000000,
                                         expected_result_sha256=identity))
        need(selected["full_result"] == dict(sha256=identity, size=len(document), detail="full"),
             "selection identity differs from exact full result")
        need([row["window_index"] for row in selected["rows"]] == [1, 2], "half-open endpoints selected wrong windows")
        need([row["window"] for row in selected["rows"]] == full["whole_mix"]["image"]["windows"][1:3],
             "window projection changed values")
        need(selected["checks_scope"] == "full_result" and selected["checks_pass"] == full["checks_pass"],
             "selected reply changed or mis-scoped checks")
        partial = okay(self.fm.describe(left, harness=self.harness, measure=True, detail="windows",
                                        start_ns=2999999999, end_ns=3040000000))
        need([row["window_index"] for row in partial["rows"]] == [29, 30]
             and partial["rows"][-1]["window"]["frames"] == 2400,
             "selection trimmed/recomputed whole partial-tail windows")
        empty = okay(self.fm.describe(left, harness=self.harness, measure=True, detail="windows", start_ns=4000000000))
        mono = okay(self.fm.describe(self.quiet, harness=self.harness, measure=True, detail="windows"))
        need(empty["source"]["image"]["available"] and empty["source"]["image"]["reason"] is None
             and not empty["rows"] and empty["selected_rows"] == 0 and empty["eof"], "valid empty selection was unavailable")
        need(not mono["source"]["image"]["available"] and mono["source"]["image"]["reason"] == "mono_has_no_image"
             and not mono["rows"] and mono["eof"], "mono image was fabricated or mislabeled")
        target = {"a.peak_dbfs": (-200, -199), "missing_measurement": (0, 1)}
        comparison = okay(self.fm.compare(left, right, harness=self.harness, measure=True, detail="full", target=target))
        pair = okay(self.fm.compare(left, right, harness=self.harness, measure=True, detail="windows", target=target,
                                   start_ns=100000000, end_ns=300000000))
        need(pair["checks_pass"] is False and pair["check_counts"] ==
             {status: sum(c["status"] == status for c in comparison["checks"]) for status in ("pass", "fail", "unchecked")},
             "selection hid failed/unchecked full-result checks")
        for row in pair["rows"]:
            index = row["window_index"]
            need(row["a"] == comparison["a"]["whole_mix"]["image"]["windows"][index]
                 and row["b"] == comparison["b"]["whole_mix"]["image"]["windows"][index]
                 and row["delta"] == comparison["whole_mix"]["image"]["windows"][index]
                 and row["delta_reason"] is None, "aligned selection changed existing delta")
        return dict(selected_indexes=[1, 2], partial_tail_frames=2400, full_result_sha256=identity,
                    check_counts=pair["check_counts"], empty_distinct_from_mono=True)

    def window_geometry(self):
        left = self.window_fixture("window-geometry-a")
        rate = self.window_fixture("window-geometry-rate", rate=44100)
        shorter = self.window_fixture("window-geometry-shorter", duration=3025000000)
        rendered = okay(self.fm.render([left], harness=self.harness, out=self.out / "window-wav", wav=True))
        evidence = []
        for a, b, reason in ((left, rate, "unequal_geometry"), (left, shorter, "unequal_geometry"),
                             (self.quiet, left, "missing_or_unselected"), (left, self.quiet, "missing_or_unselected"),
                             (left, rendered["wav"], None), (rendered["wav"], rendered["wav"], None)):
            selected = okay(self.fm.compare(a, b, harness=self.harness, measure=True, detail="windows",
                                            start_ns=3000000000))
            need(selected["selected_rows"] == 1 and len(selected["rows"]) == 1, "tail selection did not return one union row")
            row = selected["rows"][0]
            need(row["window_index"] == 30 and row["delta_reason"] == reason, "unequal/missing image reason differs")
            need((row["delta"] is None) == (reason is not None), "unaligned comparison fabricated a delta")
            if reason == "missing_or_unselected":
                need((row["a"] is None) != (row["b"] is None), "missing side was padded with invented window")
            evidence.append(dict(a_fs=selected["a"]["fs"], b_fs=selected["b"]["fs"], delta_reason=reason))
        return evidence

    def window_pages(self):
        left = self.window_fixture("window-page-a")
        right = self.window_fixture("window-page-b", x=500)
        full = okay(self.fm.compare(left, right, harness=self.harness, measure=True, detail="full"))
        identity = hashlib.sha256(self.document(full)).hexdigest()
        joined, offset, sizes = [], 0, []
        while True:
            page = okay(self.fm.compare(left, right, harness=self.harness, measure=True, detail="windows",
                                       window_offset=offset, window_limit=32, expected_result_sha256=identity))
            size = self.bounded(page)
            need(size <= 15360, "windows projection used reserved receipt bytes")
            sizes.append(size)
            need(page["window_offset"] == offset and page["returned_count"] == len(page["rows"])
                 and page["selected_rows"] == 31, "window page counts differ")
            joined.extend(page["rows"])
            offset += page["returned_count"]
            if page["eof"]:
                need(page["next_window_offset"] is None, "EOF window page has next offset")
                break
            need(page["rows"] and page["next_window_offset"] == offset, "window pagination stalled")
        need([row["window_index"] for row in joined] == list(range(31)) and len(sizes) > 1,
             "bounded row pages omitted/duplicated windows")
        need(all(row["delta"] == full["whole_mix"]["image"]["windows"][row["window_index"]] for row in joined),
             "paged windows changed complete delta values")
        try:
            self.fm.result_detail(full, "windows", 0, 4096, None, start_ns=10 ** 20000)
            raise AssertionError("unrepresentable window metadata exceeded response budget")
        except self.fm.ToolError as error:
            need(error.exit == 2, "window metadata overflow lost usage2")
        eof = okay(self.fm.compare(left, right, harness=self.harness, measure=True, detail="windows", window_offset=31))
        need(eof["eof"] and not eof["rows"] and eof["returned_count"] == 0, "empty window EOF failed")
        for options in (dict(window_offset=32), dict(expected_result_sha256="0" * 64)):
            bad = self.fm.compare(left, right, harness=self.harness, measure=True, detail="windows", **options)
            need(bad["exit"] == 2 and "rows" not in bad, "invalid window offset/digest leaked rows")
        right.write_text(right.read_text() + "gain -60\n")
        bad = self.fm.compare(left, right, harness=self.harness, measure=True, detail="windows", expected_result_sha256=identity)
        need(bad["exit"] == 2 and "rows" not in bad, "changed full result joined an old window receipt")
        # A native expected-hash failure is still a fully computed result.
        right.write_text(right.read_text() + "hash 0000000000000000\n")
        mismatch = okay(self.fm.describe(right, harness=self.harness, measure=True, detail="windows"), 3)
        need(mismatch["checks_pass"] is False and mismatch["check_counts"]["fail"] == 1,
             "window projection hid native expected-hash failure")
        return dict(rows=len(joined), pages=len(sizes), page_bytes=sizes, expected_hash_failure_exit=3)

    def window_options(self):
        native, attempted = self.fm.Native, []
        def forbidden(*args, **kwargs):
            attempted.append(True)
            raise AssertionError("invalid window options invoked native")
        options = []
        for field in ("start_ns", "end_ns", "window_offset", "window_limit"):
            for bad in (True, "1", 1.0):
                options.append({field: bad})
        for field in ("start_ns", "window_offset", "window_limit"):
            options.append({field: None})
        options += [dict(start_ns=-1), dict(end_ns=0), dict(start_ns=2, end_ns=1), dict(window_offset=-1),
                    dict(window_limit=0), dict(window_limit=33), dict(expected_result_sha256="A" * 64),
                    dict(expected_result_sha256=True), dict(offset=1), dict(length=8192), dict(note=True),
                    dict(note=0), dict(measure=False), dict(measure=1)]
        calls = []
        for operation, sources in ((self.fm.describe, (self.a,)), (self.fm.compare, (self.a, self.b))):
            for change in options:
                arguments = dict(detail="windows", measure=True, **{})
                arguments.update(change)
                calls.append((operation, sources, arguments))
            for change in (dict(start_ns=1), dict(end_ns=1), dict(window_offset=1), dict(window_limit=1)):
                calls.append((operation, sources, change))
        destination = self.out / "forbidden-window-out"
        calls.append((self.fm.describe, (self.a,), dict(detail="windows", measure=True, out=destination)))
        self.fm.Native = forbidden
        try:
            for operation, sources, arguments in calls:
                reply = operation(*sources, **arguments)
                need(reply["exit"] == 2 and reply["ok"] is False, "invalid window option did not refuse2")
        finally:
            self.fm.Native = native
        need(not attempted and not destination.exists(), "invalid window request produced side effects")
        return dict(refusals=len(calls), native_calls=0, output_created=False)

    def view_cli(self):
        base = [sys.executable, "-B", str(self.repo / "tools/fm.py"), "--harness", str(self.harness), "describe", str(self.a)]
        text = subprocess.run(base, capture_output=True, text=True, check=True).stdout
        full = okay(self.fm.describe(self.a, harness=self.harness, detail="full"))
        need(text == full["canonical"], "non-JSON default describe changed canonical output")
        compact = subprocess.run(base + ["--detail", "compact"], capture_output=True, text=True, check=True).stdout
        need(json.loads(compact) == self.fm.describe(self.a, harness=self.harness), "explicit compact without --json was ignored")
        page = okay(self.cli("describe", self.a, "--detail", "page", "--length", 127))
        need(page == self.fm.describe(self.a, harness=self.harness, detail="page", length=127), "CLI result page differs")
        need(okay(self.cli("compare", self.a, self.b)) == self.fm.compare(self.a, self.b, harness=self.harness),
             "CLI compare default differs")
        retained = okay(self.fm.describe(self.a, harness=self.harness, detail="summary", out=self.out / "legacy-describe"))
        need(Path(retained["record"]).read_text() == retained["canonical"], "explicit legacy describe retention changed")
        return dict(canonical_bytes=len(text.encode()), explicit_compact=True, page_bytes=page["length"], legacy_retention=True)

    def png_delivery(self):
        wav = okay(self.fm.render([self.quiet], harness=self.harness, out=self.out / "plot-audio", wav=True))["wav"]
        output = self.out / "new-plot-parent" / "nested" / "plot.PNG"
        compact = okay(self.fm.analyze(wav, mode="spectro", out=output))
        need(compact["png_base64"] is None and compact["omitted"] == ["png_base64"], "compact plot returned binary text")
        data = output.read_bytes()
        need(compact["png_bytes"] == len(data) and compact["png_sha256"] == digest(output), "PNG receipt differs")
        refused = self.fm.analyze(wav, mode="spectro", out=output, detail="full")
        need(refused["exit"] == 2 and output.read_bytes() == data, "plot reuse overwrote an existing artifact")
        full_output = output.parent / "full.PNG"
        full = okay(self.fm.analyze(wav, mode="spectro", out=full_output, detail="full"))
        need(base64.b64decode(full["png_base64"], validate=True) == data, "full PNG payload changed")
        need(set(full) == {"version", "ok", "exit", "stdout", "stderr", "png", "png_sha256", "png_base64", "mime_type"},
             "explicit full gained compact fields")
        for key in set(full) - {"png_base64", "png", "stdout"}:
            need(compact[key] == full[key], "compact plot changed legacy metadata: " + key)
        offset, joined, chunks = 0, bytearray(), 0
        while offset < len(data):
            chunk = okay(self.fm.artifact_read(**compact["artifact"], offset=offset))
            block = base64.b64decode(chunk["data_base64"], validate=True)
            need(chunk["length"] == len(block) <= 8192 and chunk["mime_type"] == "image/png", "PNG default chunk differs")
            self.bounded(chunk)
            joined.extend(block)
            offset += len(block)
            chunks += 1
        need(bytes(joined) == data and hashlib.sha256(joined).hexdigest() == full["png_sha256"], "PNG chunks lost bytes")
        eof = okay(self.fm.artifact_read(**compact["artifact"], offset=len(data)))
        need(eof["eof"] and eof["data_base64"] == "", "PNG empty EOF failed")
        larger = okay(self.fm.artifact_read(**compact["artifact"], length=65536))
        need(larger["length"] == min(len(data), 65536), "explicit larger artifact chunk was reduced")
        cli = okay(self.cli("artifact_read", output.parent, "png", full["png_sha256"], "--filename", output.name))
        need(base64.b64decode(cli["data_base64"]) == data[:8192], "CLI default PNG chunk differs")
        for filename in (None, "", "../plot.png", "/plot.png", "a/b.png", "a\\b.png", "bad\0.png", "plot.jpg", True):
            bad = self.fm.artifact_read(output.parent, "png", full["png_sha256"], filename=filename)
            need(bad["exit"] == 2 and "data_base64" not in bad, "unsafe PNG filename accepted")
        for kind in ("record", "wav"):
            bad = self.fm.artifact_read(output.parent, kind, full["png_sha256"], filename=output.name)
            need(bad["exit"] == 2, "filename accepted for SHA-addressed old kind")
        output.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        bad = self.fm.artifact_read(**compact["artifact"], length=1)
        need(bad["exit"] == 5 and "data_base64" not in bad, "PNG corruption outside chunk escaped")
        output.write_bytes(b"not a PNG")
        bad = self.fm.artifact_read(output.parent, "png", digest(output), filename=output.name)
        need(bad["exit"] == 5 and "data_base64" not in bad, "verified non-PNG bytes escaped")
        output.write_bytes(data)
        existing, attempts = self.fm.existing_command, []
        def forbidden(*args):
            attempts.append(True)
            raise AssertionError("bad plot destination invoked analyzer")
        blocked = self.out / "plot-parent-file"
        blocked.write_text("file")
        invalid = self.out / "invalid-plot-parent"
        self.fm.existing_command = forbidden
        try:
            for destination in (blocked / "plot.png", output.parent / "directory.png"):
                if destination.parent == output.parent:
                    destination.mkdir()
                bad = self.fm.analyze(wav, mode="spectro", out=destination)
                need(bad["exit"] == (2 if destination.is_dir() else 5), "plot parent/destination refusal differs")
            for options in (dict(out=invalid / "bad.jpg"), dict(out=invalid / "plot.png", detail="unknown"),
                            dict(out=invalid / "plot.png", mode="unknown")):
                arguments = dict(mode="spectro", **{})
                arguments.update(options)
                bad = self.fm.analyze(wav, **arguments)
                need(bad["exit"] == 2, "invalid plot option lost usage2")
        finally:
            self.fm.existing_command = existing
        need(not attempts and not invalid.exists(), "invalid plot options produced side effects")
        failure = self.fm.analyze(self.out / "absent.wav", mode="spectro", out=output.parent / "absent.PNG")
        need(failure["ok"] is False and failure["exit"] == 1 and "artifact" not in failure,
             "unrelated analyzer failure was relabeled or published an artifact")
        return dict(png_bytes=len(data), chunks=chunks, compact_bytes=len(self.document(compact)),
                    full_bytes=len(self.document(full)), parent_refusal_calls=0)

    def execute(self):
        self.case("window-selection-values-boundaries-and-check-scope", self.window_selections)
        self.case("window-geometry-mono-and-mixed-inputs", self.window_geometry)
        self.case("window-pagination-cap-identity-and-hash-failure", self.window_pages)
        self.case("window-arguments-before-work", self.window_options)
        self.case("compact-measured-projection-and-checks", self.compact_measurements)
        self.case("bounded-result-pages-and-identity", self.result_pages)
        self.case("compact-independent-growth-and-fallback", self.compact_growth)
        self.case("result-view-arguments-before-work", self.result_options)
        self.case("describe-CLI-legacy-and-explicit-views", self.view_cli)
        self.case("PNG-delivery-and-parent-failures", self.png_delivery)
        self.case("text-file-ingress-and-native-refusals", self.text_ingress)
        self.case("verified-artifact-chunks", self.artifact_chunks)
        self.case("absent-empty-log-no-render", self.absent_empty_logs)
        self.case("bounded-take-log-projection-and-strict-options", self.take_log_projection)
        self.case("UTF8-artifact-arguments-and-whole-file-validation", self.utf8_artifact_options)
        self.case("strict-options-and-duplicate-targets", self.strict_options)
        self.case("summary-full-description-parity", self.description_detail)
        self.case("summary-full-comparison-delta-parity", self.comparison_detail)
        self.case("bad-wav-structured-refusal", self.bad_wav)
        self.case("expected-hash-and-target-checks", self.checks_semantics)
        self.case("native-metrics", self.metrics)
        self.case("invalid-target-CLI-public-parity", self.invalid_target_parity)
        self.case("measurement-host-failure", self.measurement_host_failure)
        self.case("mixed-and-wav-measurements", self.mixed_wav)
        self.case("silent-short-WAV-unavailable-notes", self.unavailable_wav_notes)
        self.case("recipe-D83-measurement-parity", self.recipe_measure)
        self.case("underscore-patch-groups-and-raw-keys", self.underscore_names)
        self.case("metadata-omission-mutant", self.metadata_omission)
        self.case("asymmetric-deltas-and-swap-mutant", self.asymmetric_compare)
        for fs in (48000, 44100):
            for path in sorted((self.repo / "tests/golden" / str(fs)).glob("*.tl")):
                self.case("golden-" + str(fs) + "-" + path.stem, lambda path=path: self.golden(path))
        for path in sorted((self.repo / "tests/golden/gen").glob("*.tl")):
            self.case("generator-" + path.stem, lambda path=path: self.golden(path))
        for path in sorted((self.repo / "tests/golden/space").rglob("*.tl")):
            if "bad" not in path.parts:
                self.case("spatial-" + path.stem, lambda path=path: self.spatial_golden(path))
        for path in sorted((self.repo / "tests/golden/space/binaural").glob("*.tl")):
            self.case("image-" + path.stem, lambda path=path: self.binaural_measure(path))
        geometry = (
            ("rear-wrap", [(100, -1000, 0), (-100, -1000, 0)], 2 * math.degrees(math.atan2(100, 1000))),
            ("closed-loop", [(0, 1000, 0), (1000, 0, 0), (0, -1000, 0), (-1000, 0, 0), (0, 1000, 0)], 360),
            ("antipodal-tie", [(0, 1000, 0), (0, -1000, 0), (1000, 0, 0)], 270),
            ("origin", [(0, 0, 0), (1000, 0, 0)], None),
            ("pole", [(0, 0, 1000), (1000, 0, 0)], None),
        )
        for name, points, expected in geometry:
            self.case("geometry-" + name, lambda name=name, points=points, expected=expected: self.geometry_case(name, points, expected))
        self.case("log-identities-and-explicit-parent", self.log_identities)
        self.case("three-golden-log-replay", self.three_golden_log)
        self.case("replay-native-hash-mismatch-precedence", self.replay_hash_mismatch)
        if hasattr(self, "first"):
            self.case("whole-wav-identity", self.wav_identities)
            self.case("restart-and-replay", self.restart)
            self.case("corrupt-missing-artifacts-and-log", self.corruptions)
            self.case("concurrent-append", self.concurrent)
            self.case("interrupted-publication-recovery", self.interrupted)
        need(self.binary_hash == digest(self.harness), "native binary changed during tests")
        need(all(value == digest(self.repo / name) for name, value in self.source_hashes.items()),
             "tested source changed during tests")
        return dict(_license=["Copyright (c) 2026 Nenad Micic", "SPDX-License-Identifier: Apache-2.0"],
                    platform=platform.platform(), python=sys.version.split()[0],
                    harness_sha256=self.binary_hash, source_sha256=self.source_hash,
                    source_hashes=self.source_hashes,
                    source_changed_during_run=self.source_hash != digest(self.repo / "tools/fm.py"),
                    passed=sum(r["pass_"] for r in self.results), failed=sum(not r["pass_"] for r in self.results), tests=self.results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--harness", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    checks = Checks(args)
    report = checks.execute()
    # Runtime paths are local artifacts, not part of a reproducible receipt.
    encoded = json.dumps(report, indent=2, allow_nan=False).replace(str(checks.out), "<out>").replace(str(checks.repo), "<repo>")
    (checks.out / "results.json").write_text(encoded + "\n")
    print("TOOLKIT {} passed={} failed={}".format("ok" if not report["failed"] else "failed", report["passed"], report["failed"]))
    return int(bool(report["failed"]))


if __name__ == "__main__":
    sys.exit(main())
