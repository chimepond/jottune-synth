#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Exercise native parameter metadata; this is a test mutator, not a TL validator.

Bounds and form selectors come only from harness --params/--gen-params.
The native reader accepts every baseline before a single field is mutated.
No WAV is written. Runtime records, listings and JSON evidence live in --out.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

NUMERIC = re.compile(r"[ui](8|16|32|64)\Z")
EDGE = re.compile(r"([^>]+)>([^:]+):([^:]+)(?::([^:]+))?\Z")
PART = {"FROM": 0, "TO": 1, "WEIGHT": 2, "KIND": 3}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def listing(text):
    rows = []
    for line in text.splitlines():
        words = line.split()
        require(words and all("=" in x for x in words[1:]), "malformed listing: " + line)
        row = dict(x.split("=", 1) for x in words[1:])
        require(len(row) == len(words) - 1, "duplicate listing key: " + line)
        row["row"] = words[0]
        rows.append(row)
    return rows


@dataclass
class Schema:
    rows: list
    generators: list

    def __post_init__(self):
        require(self.rows, "empty PARAMS listing")
        require(self.rows[0].get("version") == "1" and self.rows[0]["row"] == "PARAMS", "unsupported PARAMS version")
        self.statements = {r["id"]: r for r in self.rows if r["row"] in ("STATEMENT", "EVENT")}
        self.params = {r["id"]: r for r in self.rows if r["row"] == "PARAM"}
        self.forms = {(r["statement"], r["id"]): r for r in self.rows if r["row"] == "FORM"}
        self.kinds = {r["name"]: int(r["id"]) for r in self.generators if r["row"] == "GEN_KIND"}
        self.gen_params = [r for r in self.generators if r["row"] == "GEN_PARAM"]
        require(len(self.params) == int(self.rows[-1]["count"]), "PARAM count/ID mismatch")
        require(len(self.statements) == int(self.rows[-1]["statements"]), "STATEMENT count/ID mismatch")
        require(len(self.forms) == int(self.rows[-1]["forms"]), "FORM count/ID mismatch")
        require(len(self.gen_params) == int(self.generators[-1]["count"]), "GEN_PARAM count mismatch")
        require([r["id"] for r in self.rows if r["row"] == "FAMILY"] == ["generator"], "generator family must occur exactly once")
        self.by_statement = {s: [] for s in self.statements}
        for p in self.params.values():
            require(p["statement"] in self.statements, "unknown statement: " + p["id"])
            require(p["form"] == "all" or (p["statement"], p["form"]) in self.forms, "unknown form: " + p["id"])
            require(p["mutable"] in ("0", "1") and int(p["scale"]) > 0, "invalid mutability/scale: " + p["id"])
            require(NUMERIC.fullmatch(p["type"]) or p["type"] in ("enum", "external", "hex16", "name", "token"), "unknown type: " + p["id"])
            if NUMERIC.fullmatch(p["type"]):
                low, high = int(p["lo"]), int(p["hi"])
                bits = int(p["type"][1:])
                signed = p["type"][0] == "i"
                require(-(1 << (bits - 1)) <= low if signed else low >= 0, "low does not fit type: " + p["id"])
                require(low <= high <= (1 << (bits - int(signed))) - 1, "high does not fit type: " + p["id"])
            self.by_statement[p["statement"]].append(p)

    def statement(self, tokens):
        event = bool(tokens and re.fullmatch(r"[+-]?\d+", tokens[0]))
        matches = [s for s in self.statements.values() if (s["row"] == "EVENT") == event and len(tokens) > int(s["word_position"]) and s["name"] == tokens[int(s["word_position"])]]
        require(len(matches) == 1, "statement inventory has no unique row for: " + " ".join(tokens[:5]))
        return matches[0], int(matches[0]["word_position"])

    def positions(self, p, tokens):
        pos = p["position"]
        if pos == "last":
            return [(len(tokens) - 1, None)]
        if pos == "6+slot":
            kind = self.kinds[tokens[2]]
            return [(6 + int(r["slot"][1:]), None) for r in self.gen_params if re.fullmatch(r"p[0-3]", r["slot"]) and int(r["kinds"]) & (1 << (kind - 1))]
        m = re.fullmatch(r"(\d+)(\+)?(?:\.(\w+))?", pos)
        require(m, "unsupported position: " + pos)
        first, repeated, component = int(m[1]), m[2], m[3]
        return [(i, component) for i in (range(first, len(tokens)) if repeated else [first]) if i < len(tokens)]

    @staticmethod
    def value(tokens, slot):
        index, part = slot
        token = tokens[index]
        if part in PART:
            match = EDGE.fullmatch(token)
            require(match, "malformed accepted edge: " + token)
            return match.groups()[PART[part]]
        if part:
            key, value = token.split("=", 1)
            require(key == part, "metadata key does not match accepted token: " + token)
            return value
        return token

    def selected(self, p, tokens, slot):
        if p["form"] == "all":
            return True
        form = self.forms[p["statement"], p["form"]]
        if form["match"] == "count":
            return str(len(tokens)) in form["values"].split("|")
        selectors = self.positions({"position": form["position"]}, tokens)
        if "+." in form["position"]:
            selectors = [s for s in selectors if s[0] == slot[0]]
        require(len(selectors) == 1, "ambiguous form selector: " + p["id"])
        value = self.value(tokens, selectors[0])
        choices = form["values"].split("|")
        if form["match"] == "prefix":
            return value is not None and any(value.startswith(c) for c in choices)
        require(form["match"] in ("exact", "default_or_exact"), "unknown match kind")
        return value in choices or (value is None and form["match"] == "default_or_exact")

    def fields(self, tokens, complete=True):
        statement, word = self.statement(tokens)
        fields = []
        owners = {}
        for p in self.by_statement[statement["id"]]:
            for slot in self.positions(p, tokens):
                if self.selected(p, tokens, slot):
                    fields.append((p, slot))
                    if self.value(tokens, slot) is not None:
                        require(slot not in owners, "duplicate physical token owner: " + p["id"])
                        owners[slot] = p["id"]
        if complete:
            expected = set()
            for i, token in enumerate(tokens):
                if i == word:
                    continue
                if statement["name"] == "route" and i >= 4:
                    match = EDGE.fullmatch(token)
                    require(match, "malformed accepted edge")
                    expected.update((i, part) for part, ordinal in PART.items() if match.groups()[ordinal] is not None)
                elif statement["name"] == "engine" and i >= 2:
                    expected.add((i, token.split("=", 1)[0]))
                else:
                    expected.add((i, None))
            require(set(owners) == expected, "physical token inventory differs for " + statement["id"] + ": missing=" + str(sorted(expected - set(owners))) + " extra=" + str(sorted(set(owners) - expected)))
        return fields


def records(text):
    return [line.split("#", 1)[0].split() for line in text.splitlines() if line.split("#", 1)[0].strip()]


def serialize(lines):
    return "".join(" ".join(line) + "\n" for line in lines)


def nohash(lines):
    return [line[:] for line in lines if line[0] != "hash"]


def changed(lines, line_number, slot, value):
    result = copy.deepcopy(lines)
    index, part = slot
    if part in PART:
        values = list(EDGE.fullmatch(result[line_number][index]).groups())
        values[PART[part]] = str(value)
        result[line_number][index] = values[0] + ">" + values[1] + ":" + values[2] + (":" + values[3] if values[3] is not None else "")
    elif part:
        result[line_number][index] = part + "=" + str(value)
    else:
        result[line_number][index] = str(value)
    require(sum(a != b for old, new in zip(lines, result) for a, b in zip(old, new)) <= 1, "mutation changes more than one token")
    return result


class Check:
    def __init__(self, args):
        self.args = args
        self.out = args.out.resolve()
        require(not self.out.is_relative_to(args.repo.resolve()), "--out must be outside the repository")
        self.out.mkdir(parents=True, exist_ok=True)
        self.results = []
        self.fixtures = []
        self.covered = {}
        self.statements = set()
        self.command_count = 0

    def run(self, command):
        self.command_count += 1
        return subprocess.run([str(self.args.harness), *map(str, command)], text=True, capture_output=True, timeout=60)

    def play(self, name, lines, merged=False):
        path = self.out / (name + ".tl")
        path.write_text(serialize(lines))
        command = ["--play"]
        canonical = self.out / (name + ".canonical.tl")
        if merged:
            command += ["--merged", canonical]
        command += [path]
        result = self.run(command)
        self.results.append({"name": name, "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        return result, canonical

    def accept(self, name, lines, origin):
        lines = nohash(lines)
        result, canonical = self.play("baseline-" + name, lines, True)
        require(result.returncode == 0, "baseline refused " + name + ": " + result.stderr)
        canonical_lines = records(canonical.read_text())
        replay, _ = self.play("baseline-" + name + "-replay", canonical_lines)
        require(replay.returncode == 0 and "match=yes" in replay.stdout, "canonical baseline replay failed: " + name + ": " + replay.stderr)
        for variant, fixture_lines in (("authored", lines), ("canonical", canonical_lines)):
            # Authored harmonic declarations disappear when the writer emits bank data.
            # Both variants must be inventoried; mutations use a hash-free accepted copy.
            for tokens in fixture_lines:
                statement, _ = self.schema.statement(tokens)
                self.statements.add(statement["id"])
                self.schema.fields(tokens)
            mutable = nohash(fixture_lines)
            fixture = {"name": name + "-" + variant, "lines": mutable, "origin": origin}
            self.fixtures.append(fixture)
            for line_number, tokens in enumerate(mutable):
                for p, slot in self.schema.fields(tokens):
                    if self.schema.value(tokens, slot) is not None:
                        self.covered.setdefault(p["id"], (fixture, line_number, slot))
        # HASH is audited in canonical output, but never changed by scalar probes.
        self.covered.setdefault("header_hash_hash", (None, None, None))
        return self.fixtures[-2]

    def fixture_candidates(self):
        candidates = []
        golden = self.args.repo / "tests/golden"
        for path in sorted(golden.rglob("*.tl")):
            if "bad" in path.parts or "soak" in path.parts or "edge" in path.parts:
                continue
            lines = records(path.read_text())
            rate = next((int(x[1]) for x in lines if x[0] == "fs"), None)
            if rate != self.args.fs:
                # Some FX/spatial fixtures have only one committed rate. A setup
                # at the requested rate is accepted natively before probing it.
                # The baseline source is recorded; this is not a golden hash run.
                if rate not in (48000, 44100):
                    continue
                if str(rate) in path.parts:
                    continue
                lines = [(["fs", str(self.args.fs)] if x[0] == "fs" else x) for x in lines]
            coverage = set()
            for tokens in lines:
                coverage.update(p["id"] for p, slot in self.schema.fields(tokens) if self.schema.value(tokens, slot) is not None)
            candidates.append((path, lines, coverage))
        return candidates

    def prepare(self):
        candidates = self.fixture_candidates()
        while candidates:
            candidates.sort(key=lambda c: (-len(c[2] - self.covered.keys()), len(serialize(c[1])), str(c[0])))
            path, lines, coverage = candidates.pop(0)
            if not coverage - self.covered.keys():
                break
            self.accept("golden-" + str(len(self.fixtures)), lines, str(path.relative_to(self.args.repo)))
        # Explicit existing authored alternatives which canonical output can omit.
        base = next(f for f in self.fixtures if any(t[0] == "patch" for t in f["lines"]))
        padded = copy.deepcopy(base["lines"])
        for line in padded:
            if line[0] == "patch":
                line += ["0"] * (19 - len(line))
        self.accept("patch-all-optional", padded, base["origin"] + ": explicit optional zero values")
        # Every amount form has an independently valid target/source setup.
        for p in self.schema.params.values():
            if p["statement"] != "mod" or p["name"] != "AMOUNT" or p["id"] in self.covered:
                continue
            form = self.schema.forms[p["statement"], p["form"]]
            stem = form["values"].split("|")[0]
            target = stem + ("0" if form["match"] == "prefix" else "")
            feature = "lfo" if stem.startswith("lfo_") else "formant" if stem.startswith("formant_") else "filter" if stem in ("cutoff", "q") else "vector" if stem.startswith("vector_") else "pd" if stem.startswith("pd_") else "wtuse" if stem.startswith("wt_") else "patch"
            source = next((f for f in self.fixtures if any(t[0] == feature for t in f["lines"])), None)
            require(source, "no feature fixture for " + p["id"])
            line = next(t for t in source["lines"] if t[0] == feature)
            setup = [t[:] for t in source["lines"] if t[0] != "mod"]
            setup.insert(5, ["mod", line[1], "0", "vel", target, "0", "3"])
            self.accept("form-" + p["form"], setup, source["origin"] + ": existing target form")
        # Harmonics are authoring input, then the writer serializes a complete bank.
        source = next(f for f in self.fixtures if any(t[0] == "wtbank" for t in f["lines"]))
        setup = [t[:] for t in source["lines"] if t[0] not in ("wtdata", "wthash")]
        banks = [t for t in setup if t[0] == "wtbank"]
        for bank in banks:
            for frame in range(int(bank[2])):
                setup.append(["harmonic", bank[1], str(frame), "1", "0", "0"])
        self.accept("harmonic-authoring", setup, source["origin"] + ": harmonic bank authoring")
        # All no-operand statements must occur, even though they contribute no PARAM.
        for path, lines, _ in self.fixture_candidates():
            ids = {self.schema.statement(t)[0]["id"] for t in lines}
            if ids - self.statements:
                self.accept("statements-" + str(len(self.fixtures)), lines, str(path.relative_to(self.args.repo)))
        missing = sorted(set(self.schema.params) - set(self.covered))
        require(not missing, "uncovered PARAM rows (no silent skips): " + ", ".join(missing))
        missing = sorted(set(self.schema.statements) - self.statements)
        require(not missing, "uncovered statements: " + ", ".join(missing))

    def scalar(self):
        count = 0
        for p in self.schema.params.values():
            if not NUMERIC.fullmatch(p["type"]):
                continue
            fixture, line_number, slot = self.covered[p["id"]]
            for side, value in (("high", int(p["hi"]) + 1), ("low", int(p["lo"]) - 1)):
                result, _ = self.play(p["id"] + "-" + side, changed(fixture["lines"], line_number, slot, value))
                require(self.range_refused(self.results[-1], p, value), p["id"] + " " + side + " expected reader exit1, got " + str(result.returncode) + ": " + result.stderr)
                count += 1
        return count

    def range_refused(self, result, p, value):
        statement = self.schema.statements[p["statement"]]["name"]
        pattern = r":\d+: " + re.escape(statement) + r" [^\n:]+: " + re.escape(p["name"]) + " '" + re.escape(str(value)) + "' is not " + re.escape(p["lo"]) + " to " + re.escape(p["hi"]) + r"(?:\n|$)"
        return result["exit"] == 1 and re.search(pattern, result["stderr"]) is not None

    def control(self, name, lines, expected):
        result, _ = self.play("control-" + name, nohash(lines))
        require(result.returncode == expected, name + " expected exit " + str(expected) + ", got " + str(result.returncode) + ": " + result.stderr)
        if expected == 0:
            require(result.stdout.startswith("PLAY ") and "block_indep=yes" in result.stdout, name + " did not complete native playback")
        return result

    def locate(self, param):
        fixture, line_number, slot = self.covered[param]
        return copy.deepcopy(fixture["lines"]), line_number, slot

    def dependency_controls(self):
        # These probe accepted boundaries and immediate coupled/discontinuous
        # neighbours within scalar outer bounds; metadata never evaluates them.
        source = next(f for f in self.fixtures if any(t[0] == "route" and t[2] == "2" for t in f["lines"]))
        lines = copy.deepcopy(source["lines"])
        i = next(n for n, t in enumerate(lines) if t[0] == "route" and t[2] == "2")
        slot = (3, None)
        ops = int(lines[i][2])
        self.control("carriers-ops-max", changed(lines, i, slot, (1 << ops) - 1), 0)
        self.control("carriers-ops-over", changed(lines, i, slot, 1 << ops), 1)
        for field, part in (("route_from", "FROM"), ("route_to", "TO")):
            lines, i, slot = self.locate(field)
            ops = int(lines[i][2])
            original = int(self.schema.value(lines[i], slot))
            other = int(self.schema.value(lines[i], (slot[0], "TO" if part == "FROM" else "FROM")))
            # A two-op edge uses exactly0/1; the existing accepted edge is its
            # positive boundary control, with both inequality failures tested.
            self.control(field + "-valid-edge", lines, 0)
            require(original < ops and original != other, "bad edge baseline")
            self.control(field + "-equal-ops", changed(lines, i, slot, ops), 1)
            self.control(field + "-self-edge", changed(lines, i, slot, other), 1)
        for field in ("lfo_mask_pitch", "lfo_mask_level", "lfo_mask_index", "peg_mask"):
            lines, i, slot = self.locate(field)
            patch = lines[i][1]
            ops = next((int(t[2]) for t in lines if t[0] == "route" and t[1] == patch), 2)
            self.control(field + "-ops-max", changed(lines, i, slot, (1 << ops) - 1), 0)
            self.control(field + "-ops-over", changed(lines, i, slot, 1 << ops), 1)
        lines, i, slot = self.locate("lfo_rate_millihz")
        lines = [t for t in lines if not (t[0] == "mod" and t[4] == "lfo_rate")]
        i = next(n for n, t in enumerate(lines) if t[0] == "lfo")
        # FM_CTL is obtained from the generator clock's compiled native default.
        clock = next(int(p["default"]) for p in self.schema.gen_params if p["name"] == "clock")
        # At the two music rates the scalar50000 cap precedes Nyquist.
        # A native-supported low-rate setup makes the strict coupled edge
        # reachable without changing either production bound.
        boundary_rate = 2048
        next(t for t in lines if t[0] == "fs")[1] = str(boundary_rate)
        boundary = (500 * boundary_rate + clock - 1) // clock
        self.control("lfo-strict-nyquist-below", changed(lines, i, slot, boundary - 1), 0)
        self.control("lfo-strict-nyquist-at", changed(lines, i, slot, boundary), 1)
        lines, i, slot = self.locate("flanger_depth_us")
        base = int(lines[i][1])
        self.control("flanger-depth-equals-base", changed(lines, i, slot, base), 0)
        self.control("flanger-depth-above-base", changed(lines, i, slot, base + 1), 1)
        lines, i, slot = self.locate("eg_sustain2_db10")
        patch, op = lines[i][1:3]
        first = next(t for t in lines if t[0] == "patch" and t[1:3] == [patch, op])
        first[10] = "120"
        lines[i][4] = "0"
        self.control("eg-discontinuous-zero", changed(lines, i, slot, 0), 0)
        self.control("eg-discontinuous-upper-start", changed(lines, i, slot, 120), 0)
        self.control("eg-discontinuous-hole-low", changed(lines, i, slot, 1), 1)
        self.control("eg-discontinuous-hole-high", changed(lines, i, slot, 119), 1)
        for field in ("harmonic_frame", "wtdata_frame"):
            lines, i, slot = self.locate(field)
            frames = int(next(t[2] for t in lines if t[0] == "wtbank" and t[1] == lines[i][1]))
            self.control(field + "-accepted", lines, 0)
            self.control(field + "-bank-end", changed(lines, i, slot, frames), 1)
        lines, i, slot = self.locate("wtdata_start")
        length = int(next(t[3] for t in lines if t[0] == "wtbank" and t[1] == lines[i][1]))
        # Complete bank data cannot independently move START without duplicate or
        # missing samples. The accepted last chunk is the positive end control.
        i = next(n for n, t in enumerate(lines) if t[0] == "wtdata" and int(t[4]) + len(t) - 5 == length)
        self.control("wtdata-start-complete-end", lines, 0)
        self.control("wtdata-start-cross-end", changed(lines, i, slot, int(lines[i][4]) + 1), 1)
        self.control("wtdata-start-at-length", changed(lines, i, slot, length), 1)
        # Every event family gets a representable last-frame positive and exact
        # end-frame negative; long event timestamps also exercise u64 parsing.
        for p in self.schema.params.values():
            if p["name"] != "T_NS":
                continue
            lines, i, slot = self.locate(p["id"])
            duration = int(next(t[1] for t in lines if t[0] == "length_ns"))
            frames = duration * self.args.fs // 1000000000
            first_outside = (frames * 1000000000 + self.args.fs - 1) // self.args.fs
            self.control(p["id"] + "-last-frame", changed(lines, i, slot, first_outside - 1), 0)
            self.control(p["id"] + "-end-frame", changed(lines, i, slot, first_outside), 1)
        lines, i, slot = self.locate("event_move_dur_ns")
        start = int(lines[i][0])
        first_outside = (((1 << 32) * 1000000000 + self.args.fs - 1) // self.args.fs) - start
        self.control("move-end-u32-max", changed(lines, i, slot, first_outside - 1), 0)
        self.control("move-end-u32-over", changed(lines, i, slot, first_outside), 1)
        for field, valid, invalid in (("lfo_free", 2, 1), ("header_block_frames", 64, 63), ("header_order_order", 3, 2)):
            lines, i, slot = self.locate(field)
            self.control(field + "-discrete-valid", changed(lines, i, slot, valid), 0)
            self.control(field + "-discrete-hole", changed(lines, i, slot, invalid), 1)
        # Generator bounds stay in --gen-params; the linked Euclid dependency
        # k<=n is deliberately tested separately from those outer bounds.
        source = next(f for f in self.fixtures if any(t[0] == "gen" and t[2] == "euclid" for t in f["lines"]))
        lines = copy.deepcopy(source["lines"])
        i = next(n for n, t in enumerate(lines) if t[0] == "gen" and t[2] == "euclid")
        n = int(lines[i][7])
        self.control("generator-linked-euclid-k-equals-n", changed(lines, i, (6, None), n), 0)
        self.control("generator-linked-euclid-k-above-n", changed(lines, i, (6, None), n + 1), 1)
        div = next(p for p in self.schema.gen_params if p["name"] == "div")
        self.control("generator-linked-div-min", changed(lines, i, (4, None), int(div["min"])), 0)
        self.control("generator-linked-div-below", changed(lines, i, (4, None), int(div["min"]) - 1), 1)

    def syntax_and_width_controls(self):
        # Complete authored alternatives cover omitted fields as well as each
        # historical discontinuity in the accepted argument counts.
        for word, filename, lengths, invalid, full in (
            ("patch", "quiet.tl", (12, 14, 15, 16, 17, 18, 19), (13, 20), 19),
            ("lfo", "vibrato.tl", (9, 13, 14), (10, 11, 12, 15), 14),
        ):
            path = self.args.repo / "tests/golden" / str(self.args.fs) / filename
            base = self.accept("arity-" + word, records(path.read_text()), str(path.relative_to(self.args.repo)))
            lines = copy.deepcopy(base["lines"])
            i = next(n for n, t in enumerate(lines) if t[0] == word)
            tokens = lines[i] + ["0"] * (full + 1 - len(lines[i]))
            for size in lengths + invalid:
                case = copy.deepcopy(lines)
                case[i] = tokens[:size]
                self.control(word + "-argc-" + str(size), case, 0 if size in lengths else 1)
        # These accepted endpoints guard raw-vs-effective distinctions and
        # widths which hashes of ordinary values alone cannot establish.
        fields = ("patch_level_db10", "patch_vel_sens_q8", "patch_index_q10",
                  "patch_feedback_q10", "patch_ratio_q16", "patch_sustain_db10",
                  "event_on_velocity_q15", "event_on_freq_millihz",
                  "event_ctl_value_q15", "gain_gain_db10", "ksource_seed")
        for field in fields:
            p = self.schema.params[field]
            lines, i, slot = self.locate(field)
            for edge in ("lo", "hi"):
                self.control(field + "-accepted-" + edge, changed(lines, i, slot, int(p[edge])), 0)
        # The rate of one LFO and all of its lfo_rate slots form a joint
        # domain. The signed amount contributes its magnitude to the guard.
        lines, i, slot = self.locate("mod_amount_lfo_rate")
        patch = lines[i][1]
        keep = lines[i]
        lines = [t for t in lines if not (t[0] == "mod" and t[4] == "lfo_rate" and t is not keep)]
        i = lines.index(keep)
        next(t for t in lines if t[0] == "fs")[1] = "2048"
        next(t for t in lines if t[0] == "lfo" and t[1] == patch)[3] = "1000"
        clock = next(int(p["default"]) for p in self.schema.gen_params if p["name"] == "clock")
        edge = (500 * 2048 + clock - 1) // clock - 1000
        for sign in (1, -1):
            self.control("lfo-rate-joint-inside-" + str(sign), changed(lines, i, slot, sign * (edge - 1)), 0)
            self.control("lfo-rate-joint-at-" + str(sign), changed(lines, i, slot, sign * edge), 1)

    def mutation_controls(self):
        # Counts are updated, so omission controls cannot pass merely because
        # a summary count disagrees. Actual accepted physical tokens expose it.
        fixture, i, _ = self.covered["patch_level_db10"]
        for name, predicate in (("missing-parameter", lambda r: r.get("id") == "patch_level_db10"), ("missing-statement", lambda r: r.get("id") == "shape" or r.get("statement") == "shape")):
            rows = [copy.deepcopy(r) for r in self.schema.rows if not predicate(r)]
            rows[-1]["count"] = str(sum(r["row"] == "PARAM" for r in rows))
            rows[-1]["statements"] = str(sum(r["row"] in ("STATEMENT", "EVENT") for r in rows))
            rows[-1]["forms"] = str(sum(r["row"] == "FORM" for r in rows))
            caught = False
            try:
                mutant = Schema(rows, self.schema.generators)
                for f in self.fixtures:
                    for tokens in f["lines"]:
                        mutant.fields(tokens)
            except AssertionError:
                caught = True
            require(caught, name + " mutant escaped inventory")
        # A field label typo must fail the very same reader oracle used above.
        p = self.schema.params["patch_level_db10"]
        result = next(r for r in self.results if r["name"] == p["id"] + "-high")
        wrong = dict(p, name="WRONG_FIELD")
        require(self.range_refused(result, p, int(p["hi"]) + 1), "real field label was not accepted by oracle")
        require(not self.range_refused(result, wrong, int(p["hi"]) + 1), "wrong-field mutant escaped the scalar oracle")
        rows = copy.deepcopy(self.schema.rows)
        next(r for r in rows if r.get("id") == "patch_level_db10")["position"] = "5"
        caught = False
        try:
            mutant = Schema(rows, self.schema.generators)
            mutant.fields(fixture["lines"][i])
        except AssertionError:
            caught = True
        require(caught, "wrong-position mutant escaped inventory")
        rows = copy.deepcopy(self.schema.rows)
        next(r for r in rows if r.get("id") == "patch")["word_position"] = "1"
        caught = False
        try:
            mutant = Schema(rows, self.schema.generators)
            mutant.fields(fixture["lines"][i])
        except AssertionError:
            caught = True
        require(caught, "wrong-keyword-position mutant escaped inventory")

    def grammar_inventory(self):
        source = (self.args.repo / "src/host/harness.c").read_text()
        begin = source.index("/* Draft grammar of the")
        end = source.index("static int tl_read", begin)
        grammar = source[begin:end]
        header = set(re.findall(r"^ \*   ([a-z][a-z_0-9]*)\b", grammar, re.M))
        events = set(re.findall(r"^ \*   T_NS STREAM (?:(?:VOICE|0) )?([a-z][a-z_0-9]*)\b", grammar, re.M))
        listed_header = {s["name"] for s in self.schema.statements.values() if s["row"] != "EVENT"}
        listed_events = {s["name"] for s in self.schema.statements.values() if s["row"] == "EVENT"}
        require(header == listed_header, "grammar header inventory difference: comment-only=" + str(sorted(header - listed_header)) + " listing-only=" + str(sorted(listed_header - header)))
        require(events == listed_events, "grammar event inventory difference: comment-only=" + str(sorted(events - listed_events)) + " listing-only=" + str(sorted(listed_events - events)))

    def canonical_identity(self):
        if self.args.baseline_harness is None:
            return None
        baseline = self.args.baseline_harness
        receipt = {"baseline_harness_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
                   "current_harness_sha256": self.harness_sha256, "rates": []}
        root = self.out / "identity"
        root.mkdir(exist_ok=True)
        for rate in (48000, 44100):
            outputs = []
            directories = []
            for label, executable in (("baseline", baseline), ("current", self.args.harness)):
                directory = root / (label + "-" + str(rate))
                directory.mkdir(exist_ok=True)
                result = subprocess.run([str(executable), "--fs", str(rate), "--emit", str(directory)], text=True, capture_output=True, timeout=60)
                self.command_count += 1
                (root / (label + "-" + str(rate) + ".stdout")).write_text(result.stdout)
                (root / (label + "-" + str(rate) + ".stderr")).write_text(result.stderr)
                require(result.returncode == 0, label + " canonical emit failed: " + result.stderr)
                outputs.append(result.stdout)
                directories.append(directory)
            require(outputs[0] == outputs[1], "canonical emit stdout changed at " + str(rate))
            names = sorted(p.name for p in directories[0].glob("*.tl"))
            require(names and names == sorted(p.name for p in directories[1].glob("*.tl")), "canonical filename inventory changed")
            rows = []
            for name in names:
                before = directories[0] / name
                after = directories[1] / name
                require(before.read_bytes() == after.read_bytes(), "canonical bytes changed: " + name)
                merged = root / "replayed.tl"
                result = self.run(["--play", "--merged", merged, after])
                require(result.returncode == 0 and "match=yes" in result.stdout and "block_indep=yes" in result.stdout, "canonical hash replay failed: " + name + ": " + result.stderr)
                require(merged.read_bytes() == after.read_bytes(), "canonical remerge bytes changed: " + name)
                rows.append({"name": name, "sha256": hashlib.sha256(after.read_bytes()).hexdigest(), "play": result.stdout.strip()})
            receipt["rates"].append({"fs": rate, "records": len(rows), "emit_stdout_sha256": hashlib.sha256(outputs[0].encode()).hexdigest(), "cases": rows})
        require(hashlib.sha256(baseline.read_bytes()).hexdigest() == receipt["baseline_harness_sha256"], "baseline executable changed during identity run")
        (self.out / "identity.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return {"records": sum(r["records"] for r in receipt["rates"]), "baseline_harness_sha256": receipt["baseline_harness_sha256"]}

    def execute(self):
        self.harness_sha256 = hashlib.sha256(self.args.harness.read_bytes()).hexdigest()
        params = self.run(["--params"])
        require(params.returncode == 0, "--params failed: " + params.stderr)
        gen = self.run(["--gen-params"])
        require(gen.returncode == 0, "--gen-params failed: " + gen.stderr)
        (self.out / "params.txt").write_text(params.stdout)
        (self.out / "gen-params.txt").write_text(gen.stdout)
        self.schema = Schema(listing(params.stdout), listing(gen.stdout))
        native = self.run(["--params-check"])
        (self.out / "params-check.txt").write_text(native.stdout + native.stderr)
        require(native.returncode == 0, "native --params-check failed: " + native.stderr)
        self.grammar_inventory()
        self.prepare()
        numeric_checks = self.scalar()
        self.dependency_controls()
        self.syntax_and_width_controls()
        self.mutation_controls()
        identity = self.canonical_identity()
        require(hashlib.sha256(self.args.harness.read_bytes()).hexdigest() == self.harness_sha256, "harness executable changed during checks")
        summary = {"fs": self.args.fs, "statements": len(self.schema.statements), "parameters": len(self.schema.params), "numeric_parameters": numeric_checks // 2, "scalar_refusals": numeric_checks, "dependency_controls": sum(r["name"].startswith("control-") for r in self.results), "inventory_mutants": 5, "accepted_fixture_variants": len(self.fixtures), "commands": self.command_count, "harness_sha256": self.harness_sha256, "identity": identity,
                   "source_sha256": {name: hashlib.sha256((self.args.repo / name).read_bytes()).hexdigest() for name in ("tests/params_check.py", "src/host/params.def", "src/host/harness.c", "src/gen/params.def")}, "params_sha256": hashlib.sha256(params.stdout.encode()).hexdigest(), "fixtures": [{k: v for k, v in f.items() if k != "lines"} for f in self.fixtures]}
        (self.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print("PARAMCHECK ok fs={fs} statements={statements} params={parameters} scalar={scalar_refusals} dependent={dependency_controls} mutants={inventory_mutants}".format(**summary))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--baseline-harness", type=Path, help="also prove both-rate canonical emit/replay identity against this executable")
    parser.add_argument("--fs", required=True, type=int, choices=(48000, 44100))
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.harness = args.harness.resolve()
    if args.baseline_harness is not None:
        args.baseline_harness = args.baseline_harness.resolve()
    check = Check(args)
    try:
        check.execute()
    except (AssertionError, KeyError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print("PARAMCHECK fail: " + str(error), file=sys.stderr)
        return 1
    finally:
        (check.out / "results.json").write_text(json.dumps(check.results, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
