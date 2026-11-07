#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Independent seek budget, publication and remeasurement acceptance."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback


def need(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Trace:
    """Count actual native process attempts, independently of budget replies."""
    def __init__(self, check, fault=None, cached=False):
        self.check, self.fault, self.cached = check, fault, cached
        self.calls, self.measures, self.cache = [], [], {}

    def __enter__(self):
        self.run, self.measure = self.check.fm.subprocess.run, self.check.fm.measure_patch
        self.check.fm.subprocess.run = self.invoke
        self.check.fm.measure_patch = self.measurement
        return self

    def __exit__(self, *unused):
        self.check.fm.subprocess.run, self.check.fm.measure_patch = self.run, self.measure

    def invoke(self, command, *args, **kwargs):
        words = list(map(str, command)) if isinstance(command, (tuple, list)) else []
        native = bool(words and words[0] == str(self.check.harness) and "--play" in words)
        entry = None
        if native:
            files = words[words.index("--") + 1:] if "--" in words else []
            entry = dict(inputs=[sha(Path(path).read_bytes()) for path in files if Path(path).is_file()],
                         wav="--wav" in words, ordinal=len(self.calls) + 1)
            self.calls.append(entry)
            if self.fault == len(self.calls):
                result = subprocess.CompletedProcess(command, 5, "", "injected native host failure\n")
                entry["exit"] = 5
                return result
        result = self.run(command, *args, **kwargs)
        if entry is not None:
            entry["exit"] = result.returncode
        return result

    def measurement(self, native, accepted, patch, *args, **kwargs):
        start = len(self.calls)
        key = (accepted["record_sha256"], patch,
               kwargs.get("note", args[0] if args else None),
               kwargs.get("fs", args[1] if len(args) > 1 else None))
        cached = self.cached and key in self.cache
        try:
            if cached:
                result = copy.deepcopy(self.cache[key])
            else:
                result = self.measure(native, accepted, patch, *args, **kwargs)
                self.cache[key] = copy.deepcopy(result)
            return result
        finally:
            self.measures.append(dict(record_sha256=key[0], attempts=len(self.calls) - start,
                                      start=start, cached=cached))


class Checks:
    def __init__(self, args):
        self.repo, self.out, self.harness = args.repo.resolve(), args.out.resolve(), args.harness.resolve()
        need(not self.out.is_relative_to(self.repo), "--out must be outside repository")
        self.out.mkdir(parents=True, exist_ok=True)
        spec = importlib.util.spec_from_file_location("fm_seek_acceptance", self.repo / "tools/fm.py")
        self.fm = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.fm
        spec.loader.exec_module(self.fm)
        self.native = self.fm.Native(self.harness)
        self.quiet = self.repo / "tests/golden/48000/quiet.tl"
        self.ks = self.repo / "tests/golden/48000/kspluck.tl"
        self.baseline = self.fm.native_render(self.native, [self.quiet])
        self.patch = "quiet"
        self.baseline_measure = self.fm.measure_patch(self.native, self.baseline, self.patch)
        need(self.baseline_measure["measured"], "reference baseline is unmeasurable")
        self.exact = {"peak_dbfs": self.baseline_measure["values"]["peak_dbfs"]}
        field = next((row, value) for row in self.baseline["_rows"] for value in row["fields"]
                     if row["id"] == "patch" and row["raw"]["OP"] == 0 and value["name"] == "LEVEL_DB10")
        row, value = field
        step = int(next(p["scale"] for p in self.native.metadata.params if p["id"] == value["id"]))
        path = self.out / "known-improvement.tl"
        path.write_bytes(self.fm.edit_canonical(self.baseline, [dict(row=row["line_index"], token=value["token"],
                          component=value["component"], value=value["raw"] + step)]))
        candidate = self.fm.native_render(self.native, [path])
        measured = self.fm.measure_patch(self.native, candidate, self.patch)
        self.target = {"peak_dbfs": measured["values"]["peak_dbfs"]}
        need(self.target != self.exact, "known native level edit did not change measured target")
        self.results = []
        self.sources = {name: sha((self.repo / name).read_bytes()) for name in (
            "tools/fm.py", "tools/catalog.py", "tools/music/analyze.py", "tools/music/compose.py",
            "src/host/params.def", "tests/seek/check.py")}
        self.binary = sha(self.harness.read_bytes())

    def case(self, name, function):
        start = time.monotonic()
        try:
            evidence = function()
            self.results.append(dict(name=name, pass_=True, evidence=evidence, seconds=round(time.monotonic() - start, 6)))
        except (Exception, SystemExit) as error:
            self.results.append(dict(name=name, pass_=False, error=type(error).__name__ + ": " + str(error),
                                     traceback=traceback.format_exc(), seconds=round(time.monotonic() - start, 6)))
            print("SEEK fail " + name + ": " + str(error)[:1000], flush=True)

    def seek(self, label, budget, *, target=None, source=None, fault=None, cached=False, directory=None, **kwargs):
        directory = directory or self.out / label
        with Trace(self, fault=fault, cached=cached) as trace:
            result = self.fm.seek(target or self.target, source=source or self.quiet,
                                  log=directory, budget=budget, harness=self.harness, detail="full", **kwargs)
        need(result.get("version") == 1, "missing versioned seek reply")
        json.dumps(result, allow_nan=False)
        consumed = result.get("consumed_renders", 0)
        need(consumed == len(trace.calls), "reported budget differs from actual native attempts")
        need(result.get("render_attempts", consumed) == consumed, "attempt aliases differ")
        if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 0:
            need(consumed <= budget, "budget exceeded")
        if "render_counts" in result:
            need(sum(result["render_counts"].values()) == consumed, "phase counts differ from actual native attempts")
        if result.get("accepted") is not None:
            accepted = result["accepted"]
            need(Path(result["path"]).read_bytes() and sha(Path(result["path"]).read_bytes()) == accepted["record_sha256"], "published record identity differs")
            rows = self.fm.log_show(directory, detail="full")
            need(rows["ok"] and rows["rows"][-1]["record_sha256"] == accepted["record_sha256"], "accepted state is not last durable log row")
        self.last_trace = trace
        return result, trace

    def rows(self, label):
        directory = self.out / label
        if not directory.exists():
            # An unretained budget-limited run leaves no log directory. Do not
            # turn the new missing-log refusal into a successful empty replay.
            need(not (directory / "log.tsv").exists(), "absent destination contains a log")
            return []
        result = self.fm.log_show(directory, detail="full")
        need(result["ok"], str(result))
        return result["rows"]

    def early_budgets(self):
        evidence = []
        for budget in (0, 1, 2):
            result, trace = self.seek("early-" + str(budget), budget)
            need(result["ok"] and result["stop_reason"] == "budget" and result["reached"] is None
                 and result["accepted"] is None and not self.rows("early-" + str(budget)), "incomplete baseline was published")
            need(len(trace.calls) == budget, "baseline stopped before its allowed attempts")
            evidence.append(dict(budget=budget, consumed=len(trace.calls), accepted=None))
        return evidence

    def reached(self):
        result, trace = self.seek("reached", 10000, target=self.exact)
        need(result["ok"] and result["reached"] is True and result["stop_reason"] == "reached"
             and not result["sweeps"] and len(self.rows("reached")) == 1, "already-reached baseline searched or failed publication")
        self.baseline_cost = len(trace.calls)
        need(self.baseline_cost == 3, "file baseline is not one canonical + two note renders")
        return dict(consumed=self.baseline_cost, target=self.exact, record_sha256=result["accepted"]["record_sha256"])

    def full_improvement(self):
        result, trace = self.seek("improved", 10000)
        need(result["ok"] and result["reached"] is True and result["stop_reason"] == "reached", "known reachable target was not reached")
        need(len(result["history"]) == 2 and len(result["sweeps"]) == 1 and result["sweeps"][0]["complete"], "expected one full sweep and one verified improvement")
        first, final = result["history"]
        need(final["distance"] < first["distance"] and final["parent_record_sha256"] == first["record_sha256"], "winner is not strict improvement with explicit parent")
        verified = [measurement for measurement in trace.measures if measurement["record_sha256"] == final["record_sha256"]]
        need(len(verified) == 2 and all(measurement["attempts"] == 2 for measurement in verified), "candidate did not receive two independent note-render pairs")
        need(result["render_counts"].get("verify_source") == 1 and result["render_counts"].get("verify_measurement") == 2
             and result["render_counts"].get("verify_parent") == 1, "verification/parent attempts omitted")
        need(len(self.rows("improved")) == 2, "accepted take count differs")
        self.full, self.full_trace, self.full_cost = result, trace, len(trace.calls)
        return dict(consumed=self.full_cost, target=self.target, history=result["history"], sweeps=result["sweeps"],
                    attempts=trace.calls, measurements=trace.measures)

    def interrupted(self):
        evidence = []
        for budget in (self.baseline_cost, self.baseline_cost + 1, self.full_cost - 4,
                       self.full_cost - 3, self.full_cost - 2, self.full_cost - 1):
            label = "cutoff-" + str(budget)
            result, trace = self.seek(label, budget)
            need(result["ok"] and result["stop_reason"] == "budget" and result["reached"] is False,
                 "budget interruption was classified incorrectly")
            need(len(trace.calls) == budget and len(self.rows(label)) == 1
                 and result["accepted"]["record_sha256"] == self.full["history"][0]["record_sha256"], "partial sweep or verification published a winner")
            evidence.append(dict(budget=budget, consumed=len(trace.calls), phases=result["render_counts"], sweeps=result["sweeps"]))
        return evidence

    def deterministic(self):
        result, trace = self.seek("repeat", self.full_cost)
        need(result["reached"] is True and len(trace.calls) == self.full_cost, "exact successful budget did not reproduce")
        def stable(value):
            if isinstance(value, dict):
                return {key: stable(item) for key, item in value.items() if key not in ("record", "elapsed_seconds", "utc")}
            if isinstance(value, list):
                return [stable(item) for item in value]
            return value
        need(stable(result["history"]) == stable(self.full["history"]) and result["sweeps"] == self.full["sweeps"], "deterministic winner/tie order changed")
        need(trace.calls == self.full_trace.calls, "native input/hash/exit attempt sequence changed")
        return dict(consumed=len(trace.calls), record_sha256=result["accepted"]["record_sha256"], equal=True)

    def explicit_parent(self):
        directory = self.out / "external-parent"
        parent = self.fm.render([self.quiet], harness=self.harness, log=directory)
        need(parent["ok"], "parent setup failed")
        result, trace = self.seek("parent-blocked", self.baseline_cost, target=self.exact, directory=directory, parent_record=parent["record_sha256"])
        need(result["accepted"] is None and result["reached"] is None and result["stop_reason"] == "budget"
             and len(self.rows("external-parent")) == 1, "incomplete parent verification published baseline")
        allowed, complete = self.seek("parent-allowed", self.baseline_cost + 1, target=self.exact, directory=directory, parent_record=parent["record_sha256"])
        need(allowed["reached"] is True and allowed["accepted"]["parent_record_sha256"] == parent["record_sha256"]
             and len(self.rows("external-parent")) == 2, "explicit parent was lost or not budgeted")
        return dict(blocked_attempts=len(trace.calls), complete_attempts=len(complete.calls))

    def faults(self):
        evidence = []
        for index in (1, self.full_cost):
            label = "host-" + str(index)
            result, trace = self.seek(label, self.full_cost, fault=index)
            need(result["ok"] is False and result["exit"] == 5 and result["stop_reason"] == "host_error", "native host failure swallowed")
            need(len(trace.calls) == index and len(self.rows(label)) == (0 if index == 1 else 1), "host failure published incomplete state or miscounted")
            evidence.append(dict(failed_attempt=index, consumed=len(trace.calls), exit=5))
        path = self.out / "mismatch.tl"
        path.write_text("\n".join("hash 0000000000000000" if line.startswith("hash ") else line for line in self.baseline["canonical"].splitlines()) + "\n")
        result, trace = self.seek("source-mismatch", 10000, source=path)
        need(result["ok"] is False and result["exit"] == 3 and result["accepted"] is None
             and len(trace.calls) == 1 and not trace.measures, "expected-hash mismatch seeded a search")
        evidence.append(dict(source_mismatch=3, consumed=1))
        return evidence

    def cached_measurement_mutant(self):
        result, trace = self.seek("cached-mutant", self.full_cost, cached=True)
        need(any(measurement["cached"] and measurement["attempts"] == 0 for measurement in trace.measures), "skipped-remeasurement mutant was not exercised")
        need(result["ok"] is False and result["stop_reason"] == "verification_mismatch"
             and len(self.rows("cached-mutant")) == 1, "cached proposal bypassed independent remeasurement")
        return dict(mutant="cache selected record note measurement", rejected=True,
                    consumed=len(trace.calls), verification=result.get("verification"))

    def missing_measurement(self):
        edits = [dict(row=row["line_index"], token=field["token"], component=field["component"], value=65535)
                 for row in self.baseline["_rows"] for field in row["fields"]
                 if row["id"] == "patch" and field["name"] == "LEVEL_DB10"]
        path = self.out / "silent-written.tl"
        path.write_bytes(self.fm.edit_canonical(self.baseline, edits))
        result, trace = self.seek("undefined", 10000, source=path)
        need(result["ok"] and result["reached"] is None and result["stop_reason"] == "undefined_measurement"
             and result.get("missing") == ["peak_dbfs"] and not result["sweeps"], "missing descriptor became a success/failure target claim")
        return dict(consumed=len(trace.calls), reached=None, missing=result.get("missing"), accepted=result["accepted"] is not None)

    def invalid_inputs(self):
        evidence = []
        for index, arguments in enumerate((dict(budget=True), dict(budget=-1), dict(budget=1.5),
                dict(budget=100, tol={"unknown": 1}), dict(budget=100, tol={"peak_dbfs": True}),
                dict(budget=100, tol={"peak_dbfs": -1}), dict(budget=100, tol={"peak_dbfs": float("inf")}),
                dict(budget=100, target={"peak_dbfs": 1e308}, tol={"peak_dbfs": 1e308}))):
            result, trace = self.seek("invalid-" + str(index), **arguments)
            need(result["ok"] is False and result["exit"] == 2 and not trace.calls, "invalid budget/tolerance began audio or lost usage2")
            evidence.append(dict(case=index, exit=2, consumed=0))
        ranked = self.fm.suggest({"attack_ms": 1e308}, harness=self.harness, detail="full")
        need(ranked["ok"] and ranked["nearest"] and all(math.isfinite(row["distance"]) for row in ranked["nearest"]), "large finite target caused unstructured distance overflow")
        return dict(refusals=evidence, finite_large_target=True)

    def tolerance(self):
        # Zero +/- abs(measured) reproduces the exact floating endpoint;
        # subtracting two decimal descriptors need not reassociate exactly.
        tolerance = abs(self.exact["peak_dbfs"])
        result, trace = self.seek("tolerance", 10000, target={"peak_dbfs": 0}, tol={"peak_dbfs": tolerance})
        need(result["reached"] is True and result["accepted"]["distance"] > 0 and not result["sweeps"], "tolerance changed the objective or missed inclusive boundary")
        return dict(tolerance=tolerance, original_distance=result["accepted"]["distance"], consumed=len(trace.calls))

    def coupled_refusals(self):
        target = {"peak_dbfs": -100}
        with Trace(self) as reference:
            proposed = self.fm.suggest(target, source=self.ks, probe=True, harness=self.harness, detail="full")
        need(proposed["ok"], "native coupled-reference probe failed")
        refused = sum(call["exit"] == 1 for call in reference.calls)
        need(refused > 0, "coupled-reference fixture exercises no native refusals")
        result, trace = self.seek("coupled", len(reference.calls), source=self.ks, target=target)
        need(result["ok"] and len(result["sweeps"]) == 1 and result["sweeps"][0]["complete"],
             "complete coupled sweep did not fit independently measured budget")
        need(len(trace.calls) == len(reference.calls) and result["sweeps"][0]["refused"] == refused
             and sum(call["exit"] == 1 for call in trace.calls) == refused,
             "native coupled refusals were dropped from attempt accounting")
        need(len(result["history"]) == 1 and len(self.rows("coupled")) == 1,
             "coupled sweep published an unverified candidate")
        return dict(attempts=len(trace.calls), native_refusals=refused, sweep=result["sweeps"][0])

    def derived_overflow(self):
        analysis = self.fm.analysis_module()
        path = self.out / "small-span.tsv"
        values = dict(self.baseline_measure["values"], tags="", note_hz="440")
        rows = []
        for name, peak in (("near-zero", "0"), ("tiny", "1e-300")):
            values.update(name=name, peak_dbfs=peak)
            rows.append("\t".join(str(values.get(key, "")) if values.get(key) is not None else ""
                                  for key in analysis.DESC_COLUMNS))
        path.write_text("\t".join(analysis.DESC_COLUMNS) + "\n" + "\n".join(rows) + "\n")
        result, trace = self.seek("derived-overflow", 100, target={"peak_dbfs": 1e308}, tsv=[str(path)])
        need(result["ok"] is False and result["exit"] == 1 and result["stop_reason"] == "refused"
             and "nonfinite normalized descriptor distance" in result["message"],
             "finite derived-distance overflow lost structured refusal")
        need(len(trace.calls) == 3 and result["accepted"] is None and not self.rows("derived-overflow"),
             "distance overflow lost actual attempt count or published a baseline")
        return dict(exit=result["exit"], consumed=len(trace.calls), scale=result["scales"]["peak_dbfs"])

    def execute(self):
        self.case("budgets-zero-one-two", self.early_budgets)
        self.case("already-reached-baseline", self.reached)
        self.case("known-improvement-full-verification", self.full_improvement)
        if hasattr(self, "full_cost"):
            self.case("sweep-verification-parent-budget-interruptions", self.interrupted)
            self.case("exact-budget-deterministic-repeat", self.deterministic)
            self.case("explicit-baseline-parent-budget", self.explicit_parent)
            self.case("host-failures-and-source-hash-mismatch", self.faults)
            self.case("skipped-remeasurement-mutant", self.cached_measurement_mutant)
        self.case("undefined-target-measurement", self.missing_measurement)
        self.case("invalid-budget-tolerance-and-distance-overflow", self.invalid_inputs)
        self.case("tolerance-reached-original-objective", self.tolerance)
        self.case("native-coupled-refusals-charge-budget", self.coupled_refusals)
        self.case("finite-derived-distance-overflow", self.derived_overflow)
        need(self.binary == sha(self.harness.read_bytes()), "native binary changed during test")
        need(all(value == sha((self.repo / name).read_bytes()) for name, value in self.sources.items()), "source changed during test")
        return dict(_license=["Copyright (c) 2026 Nenad Micic", "SPDX-License-Identifier: Apache-2.0"],
                    platform=platform.platform(), python=sys.version.split()[0], harness_sha256=self.binary,
                    source_hashes=self.sources, passed=sum(row["pass_"] for row in self.results),
                    failed=sum(not row["pass_"] for row in self.results), tests=self.results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--harness", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    checks = Checks(args)
    report = checks.execute()
    encoded = json.dumps(report, indent=2, allow_nan=False).replace(str(checks.out), "<out>").replace(str(checks.repo), "<repo>")
    (checks.out / "results.json").write_text(encoded + "\n")
    print("SEEK {} passed={} failed={}".format("ok" if not report["failed"] else "failed", report["passed"], report["failed"]))
    return int(bool(report["failed"]))


if __name__ == "__main__":
    sys.exit(main())
