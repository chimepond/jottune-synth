#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Focused  checks against an explicit existing harness; no WAV or build."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
from unittest import mock

import keys_tl as keys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--work", required=True)
    args = parser.parse_args()
    work = keys.checked_path(args.work)
    work.mkdir()
    harness = keys.checked_path(args.harness)
    records = []

    def check(name, condition):
        records.append(dict(name=name, ok=bool(condition)))
        keys.dump(work / "checks.json", dict(ok=all(row["ok"] for row in records),
                   checks=records, assertions=len(records), native_sha256=keys.fm.sha(harness.read_bytes()),
                   converter_sha256=keys.fm.sha(Path(keys.__file__).read_bytes())))
        if not condition:
            raise AssertionError(name)

    def fixture(name, text):
        path = work / (name + ".source.tl")
        path.write_text(text, encoding="utf-8")
        return path

    def run(name, paths, mirror=False, level_policy="strict"):
        before = [path.read_bytes() for path in paths]
        result = keys.convert(paths, harness=harness, out=work / (name + ".tl"),
                              work=work / (name + ".work"), mirror=mirror, level_policy=level_policy)
        check(name + ": source originals unchanged", before == [path.read_bytes() for path in paths])
        check(name + ": failure never published", result["ok"] or not (work / (name + ".tl")).exists())
        check(name + ": retained outcome", (work / (name + ".work/result.json")).is_file())
        return result

    empty_manifest = "GEN_EVENTS version=1 fs=48000 ctl=64\nGEN_EVENTS count=0\n"
    canonical = ("timeline draft2\nspace binaural\norder 3\npinna pbnh\nyaw 12000\n"
                 "panlaw 32767 0 0\nnear 123\nblock 32\nstreams a b\n"
                 "pingpong 50000 6000 5000 100\nhash 1234\n"
                 "0 b 0 send delay 12\n0 b 0 pos 0 1000 0\n0 b 0 on 261626 30000\n"
                 "1 a 0 pitch 440000\n1 b 0 yaw 3000\n2 b 0 off\n"
                 "2 b 0 on 440000 30000\n3 b 0 move 1 2 3 4\n")
    projection, facts = keys.project(canonical, empty_manifest)
    projected_rows = projection.splitlines()
    check("projection drops selected headers and inherited hash", all(not row.startswith(tuple(k + " " for k in
          ("order", "pinna", "yaw", "panlaw", "hash"))) for row in projected_rows))
    check("projection preserves near/block/FX", all(row in projected_rows for row in
          ("near 123", "block 32", "pingpong 50000 6000 5000 100")))
    before_events = [row for row in canonical.splitlines() if keys.event(row.split()) and row.split()[3] not in keys.SPATIAL_EVENTS]
    after_events = [row for row in projected_rows if keys.event(row.split()) and row.split()[3] not in keys.SPATIAL_EVENTS]
    check("non-spatial event order and pitch unchanged", before_events == after_events)
    check("manual pos immediately precedes on", all(projected_rows[i - 1].split()[3] == "pos"
          and projected_rows[i - 1].split()[:3] == row.split()[:3]
          for i, row in enumerate(projected_rows) if keys.event(row.split()) and row.split()[3] == "on"))
    check("all nine removed source rows recorded", len(facts["removed_rows"]) == 9)
    check("lin30 endpoints and middle C", [keys.compose.keys_pos(m, profile="lin30") for m in (28, 60, 92)] ==
          [(-500, 866, 0), (0, 1000, 0), (500, 866, 0)])
    check("mirror reflects only x", keys.position(0, "a", 0, 440000, False).split()[4:] ==
          [str(-int(keys.position(0, "a", 0, 440000, True).split()[4])),
           *keys.position(0, "a", 0, 440000, True).split()[5:]])
    routed = "patch p 0 65536 10 0\npatch p 1 65536 70 0\npatch p 2 65536 30 0\nroute p 3 6\npatch q 0 65536 100 0\npatch q 1 65536 40 0\n"
    shifted, changes = keys.shift_carriers(routed, -20)
    check("actual route mask excludes operator zero", [(r["patch"], r["op"]) for r in changes] == [("p", 1), ("p", 2), ("q", 0)])
    check("modulator rows unchanged", "patch p 0 65536 10 0\n" in shifted and "patch q 1 65536 40 0\n" in shifted)
    for name, callback in (
        ("unused carrier bound refuses", lambda: keys.shift_carriers(routed, -31)),
        ("upper carrier bound refuses", lambda: keys.shift_carriers(routed, 1200)),
        ("silence refuses", lambda: keys.level_delta(dict(peak_dbfs=-float("inf"), peak=0, clipped=0))),
        ("NaN peak refuses", lambda: keys.level_delta(dict(peak_dbfs=float("nan"), clipped=0))),
    ):
        try:
            callback()
            refused = False
        except keys.fm.ToolError:
            refused = True
        check(name, refused)
    check("clipping uses existing 15dB10 increment", keys.level_delta(dict(peak_dbfs=0., clipped=1)) == 15)
    check("target tolerance reused", keys.level_delta(dict(peak_dbfs=-2., clipped=0)) is None)

    base = (keys.ROOT / "tests/golden/48000/route4.tl").read_text()
    base = "\n".join(row for row in base.splitlines() if not row.startswith("hash ")) + "\n"
    route_path = fixture("route", base.replace("600000000 golden 0 off", "200000000 golden 0 pitch 293665\n600000000 golden 0 off"))
    routed_result = run("route", [route_path])
    check("routed native conversion succeeds", routed_result["ok"])
    check("native routed uniform carrier shifts", all(len({r["after"] - r["before"] for r in p["carrier_changes"]}) == 1
          and {r["op"] for r in p["carrier_changes"]} == {0, 2}
          for p in routed_result["passes"] if "carrier_changes" in p))
    field = (keys.ROOT / "tests/golden/fx/field-bin1-48000.tl").read_text()
    field = "\n".join(row for row in field.splitlines() if not row.startswith("hash ")) + "\n"
    field = field.replace("gain 0", "gain 240").replace("on 220000 12000", "on 220000 32767").replace("on 330000 10000", "on 330000 32767")
    field_result = run("field", [fixture("field", field)])
    check("binaural all-FX field conversion succeeds", field_result["ok"])
    field_output = (work / "field.tl").read_text()
    check("field yaw removed and FX retained", " yaw " not in field_output and "\nyaw " not in field_output and
          all(row in field_output.splitlines() for row in ("space stereo", "near 100", "gain 240", "crush", "softclip")))
    generated = (keys.ROOT / "tests/golden/gen/gencoprime-44100.tl").read_text()
    generated = "\n".join(row for row in generated.splitlines() if not row.startswith("hash ")) + "\n"
    generated = generated.replace("length_ns 6000000000", "length_ns 600000000").replace("streams golden", "streams first golden")
    generated = generated.replace("voice 2 tagged", "voice 2 tagged\nvoice 3 tagged")
    generated += "0 golden 3 on 261626 22000\n100000000 golden 3 pitch 440000\n200000000 golden 3 off\n"
    generated += "100000000 golden gen 0 reset 17\n200000000 golden lane 1 enable 0\n300000000 golden lane 1 enable 1\n"
    gen_result = run("generated", [fixture("generated", generated)])
    check("native generator conversion succeeds", gen_result["ok"])
    gen_work = work / "generated.work"
    check("complete generated manifests byte equal", (gen_work / "source-manifest.txt").read_bytes() == (gen_work / "final-manifest.txt").read_bytes())
    gen_output = (work / "generated.tl").read_text()
    check("generator lanes and controls preserved", all(row in gen_output.splitlines() for row in generated.splitlines()
          if row.startswith(("gen ", "gen_lane ")) or " golden gen " in row or " golden lane " in row))
    check("no generated on/off flattening", [row for row in gen_output.splitlines() if keys.event(row.split()) and row.split()[3] in {"on", "off"}] ==
          [row for row in generated.splitlines() if keys.event(row.split()) and row.split()[3] in {"on", "off"}])
    check("every generated on has position", gen_result["projection"]["generated_positions"] > 0 and
          sum(" kind=on " in row for row in (gen_work / "source-manifest.txt").read_text().splitlines()) ==
          sum(" first " in row and " pos " in row for row in gen_output.splitlines()))
    expected_positions = []
    for row in (gen_work / "source-manifest.txt").read_text().splitlines()[1:-1]:
        match = keys.generator._EVENT.fullmatch(row)
        if match[5] == "on":
            expected_positions.append(keys.position(match[2], "first", match[4], match[6], False))
    check("native exact generated times/frequencies in first stream", expected_positions ==
          [row for row in gen_output.splitlines() if " first " in row and " pos " in row])

    bad_hash = fixture("bad-hash", base.replace("streams golden", "hash 0000000000000000\nstreams golden"))
    result = run("bad-hash", [bad_hash])
    check("original hash mismatch fatal before projection", result["exit"] == 3 and result["failure_stage"] == "source" and not result["manifests"])
    silent = "\n".join(row for row in base.splitlines() if not keys.event(row.split())) + "\n"
    result = run("silence", [fixture("silence", silent)])
    check("native silent output refused explicitly", not result["ok"] and result["failure_stage"] == "levelling")
    decodable = base.replace("streams golden", "decodable\nstreams golden") + "700000000 golden 0 on 440000 30000\n900000000 golden 0 off\n"
    result = run("decodable", [fixture("decodable", decodable)])
    check("native decodable position refusal preserved", not result["ok"] and result["failure_stage"] == "level-00" and
          "decodable: refused" in result["play_processes"][-1]["stderr"]["text"])
    existing = fixture("collision", "do not overwrite\n")
    result = keys.convert([route_path], harness=harness, out=existing, work=work / "collision.work")
    check("destination collision never overwritten", not result["ok"] and existing.read_text() == "do not overwrite\n" and not (work / "collision.work").exists())
    link = work / "alias"
    link.symlink_to(work, target_is_directory=True)
    result = keys.convert([route_path], harness=harness, out=link / "alias.tl", work=work / "alias.work")
    check("symlink ancestor refused before work", not result["ok"] and not (work / "alias.work").exists())
    result = keys.convert([route_path], harness=harness, out=link / ".." / "alias.tl", work=work / "alias-dotdot.work")
    check("symlink before dotdot refused before normalization", not result["ok"] and not (work / "alias-dotdot.work").exists())
    link.unlink()
    result = keys.convert([route_path, route_path], harness=harness, out=work / "duplicate.tl", work=work / "duplicate.work")
    check("source aliases refused before work", not result["ok"] and not (work / "duplicate.work").exists())

    fault_dir = work / "publication-faults"
    fault_dir.mkdir()
    payload = b"verified complete bytes\n"
    collision = fault_dir / "link-collision.tl"
    collision.write_bytes(b"prior owner\n")
    try:
        keys.publish(collision, payload)
        refused = False
    except FileExistsError:
        refused = True
    check("atomic link collision preserves prior owner", refused and collision.read_bytes() == b"prior owner\n")
    warning_output = fault_dir / "cleanup-warning.tl"
    with mock.patch.object(keys.os, "unlink", side_effect=PermissionError("injected unlink fault")):
        publication = keys.publish(warning_output, payload)
    check("post-link unlink failure is successful publication with warning", warning_output.read_bytes() == payload and
          "cleanup_warning" in publication)
    warning_publication = dict(publication)
    Path(publication["cleanup_warning"]["path"]).unlink()
    rollback_output = fault_dir / "rollback.tl"
    publication = keys.publish(rollback_output, payload)
    failure = dict(ok=True, exit=0, message=None, published=True, output=dict(path=str(rollback_output)))
    with mock.patch.object(keys, "dump", side_effect=OSError("injected full disk")):
        keys.retain_result(fault_dir, failure, rollback_output, publication)
    check("final receipt failure retracts only owned output", not rollback_output.exists() and not failure["ok"] and
          failure["published"] is False and failure["publication_rolled_back"] and failure["output"] is None)
    replacement = fault_dir / "replacement.tl"
    publication = keys.publish(replacement, payload)
    replacement.unlink()
    replacement.write_bytes(b"replacement owned by another operation\n")
    refused_result = dict(ok=True, exit=0, message=None, published=True, output=dict(path=str(replacement),
                          sha256=publication["sha256"], identity_kind="verified_at_publication"))
    with mock.patch.object(keys, "dump", side_effect=OSError("injected full disk")):
        keys.retain_result(fault_dir, refused_result, replacement, publication)
    check("rollback refuses changed destination and reports both errors", replacement.read_bytes() == b"replacement owned by another operation\n" and
          refused_result["published"] is True and "publication_rollback_error" in refused_result and "receipt_error" in refused_result)
    original_failure = dict(ok=False, exit=3, message="native hash mismatch", failure_stage="source", published=False)
    with mock.patch.object(keys, "dump", side_effect=OSError("injected full disk")):
        keys.retain_result(fault_dir, original_failure)
    check("receipt failure preserves earlier native failure", original_failure["operation_error"] ==
          dict(stage="source", message="native hash mismatch"))
    refusal_stream = io.StringIO()
    refused_result.update(failure_stage="receipt")
    with mock.patch.object(keys, "convert", return_value=refused_result), mock.patch.object(sys, "argv",
          ["keys_tl.py", "--harness", str(harness), "--out", "unused.tl", "--work", "unused", "source.tl"]), contextlib.redirect_stdout(refusal_stream):
        cli_exit = keys.main()
    cli_result = json.loads(refusal_stream.getvalue())
    check("CLI exposes persistent receipt and rollback errors with output state", cli_exit == 5 and
          all(cli_result[key] == refused_result[key] for key in
              ("published", "output", "receipt_error", "publication_rollback_error", "destination_state")))
    keys.dump(fault_dir / "fault-outcomes.json", dict(cleanup_warning=warning_publication, rollback=failure,
              replacement=refused_result, original_failure=original_failure))

    # Fault injection tests the eight-pass ceiling without claiming a physical
    # mix is nonconvergent: all eight actual renders still use the native binary.
    with mock.patch.object(keys, "level_delta", return_value=15):
        result = run("pass-ceiling", [route_path])
    check("eight-pass ceiling retains unsuccessful native attempts", not result["ok"] and len(result["passes"]) == 8
          and result["message"] == "levels did not settle in eight passes")

    #  is an explicit derived-record policy; strict controls above remain
    # unchanged. Negative master gain and unequal routed carriers expose the
    # common lower-bound limit without changing the master or carrier balance.
    quiet = base.replace("streams golden", "gain -60\nstreams golden")
    quiet = quiet.replace("patch route4 0 65536 60", "patch route4 0 65536 40")
    quiet = quiet.replace("patch route4 2 131072 60", "patch route4 2 131072 80")
    quiet_path = fixture("carrier-bound", quiet)
    strict_bound = run("carrier-bound-strict", [quiet_path])
    check("strict still refuses common lower-bound crossing", not strict_bound["ok"] and
          strict_bound["failure_stage"] == "levelling" and "carrier shift exceeds" in strict_bound["message"])
    for mirror in (False, True):
        name = "carrier-bound" + ("-mirror" if mirror else "")
        bounded = run(name, [quiet_path], mirror, "carrier-bound")
        check(name + ": explicit optional publication succeeds", bounded["ok"] and bounded["published"])
        measured = bounded["levelling"]
        check(name + ": below-target status is explicit and measured", measured["status"] == "below_target_at_carrier_bound"
              and measured["target_met"] is False and measured["actual_peak_dbfs"] < -2
              and bounded["passes"][-1]["play"]["clipped"] == 0)
        step = bounded["passes"][0]["carrier_bound_step"]
        check(name + ": one capped shared shift is rerendered", len(bounded["passes"]) == 2 and
              step["requested_delta_db10"] < -40 and step["applied_delta_db10"] == -40 and
              {r["after"] - r["before"] for r in bounded["passes"][0]["carrier_changes"]} == {-40})
        check(name + ": lower limiting carrier identified", measured["legal_delta_db10"][0] == 0 and
              measured["limiting_carriers"] == [dict(patch="route4", op=0, level_db10=0)])
        rows = (work / (name + ".tl")).read_text().splitlines()
        check(name + ": master gain and routed balance preserved", "gain -60" in rows and
              [int(row.split()[4]) for row in rows if row.startswith("patch route4 ") and row.split()[2] in {"0", "2"}] == [0, 40])
        cli_stream = io.StringIO()
        with mock.patch.object(keys, "convert", return_value=bounded), mock.patch.object(sys, "argv",
              ["keys_tl.py", "--harness", str(harness), "--out", "unused.tl", "--work", "unused",
               "--level-policy", "carrier-bound", "source.tl"]), contextlib.redirect_stdout(cli_stream):
            status = keys.main()
        check(name + ": CLI exposes explicit below-target outcome", status == 0 and
              json.loads(cli_stream.getvalue())["levelling"]["target_met"] is False)
    zero_carrier = "patch p 0 65536 0 0\npatch p 1 65536 0 0\n"
    for name, fields in (("clipping", dict(peak_dbfs=-3., peak=10000, clipped=1)),
                         ("above target", dict(peak_dbfs=-0.5, peak=20000, clipped=0)),
                         ("silence", dict(peak_dbfs=-10., peak=0, clipped=0)),
                         ("nonfinite", dict(peak_dbfs=float("nan"), peak=1, clipped=0))):
        check("carrier-bound cannot admit " + name, not keys.carrier_bound_step(zero_carrier, fields, -30)["below_target_at_carrier_bound"])
    check("carrier-bound cannot admit quiet output away from boundary", not keys.carrier_bound_step(
          zero_carrier.replace("65536 0 0", "65536 20 0"), dict(peak_dbfs=-8., peak=10000, clipped=0), -65)["below_target_at_carrier_bound"])
    zero_path = fixture("carrier-zero", quiet.replace("patch route4 0 65536 40", "patch route4 0 65536 0"))
    zero_result = run("carrier-zero", [zero_path], level_policy="carrier-bound")
    check("already-zero quiet carrier needs no fictitious shift", zero_result["ok"] and len(zero_result["passes"]) == 1 and
          zero_result["levelling"]["applied_delta_db10"] == 0 and not any("carrier_changes" in p for p in zero_result["passes"]))
    result = run("carrier-bound-silence", [fixture("carrier-bound-silence", silent)], level_policy="carrier-bound")
    check("optional mode retains actual silence refusal", not result["ok"] and "silence" in result["message"])
    above = base.replace("streams golden", "gain 240\nstreams golden")
    above = above.replace("voice 0 route4", "patch unused 0 65536 1200 0 0 0 1 50 0 20\n"
                          "patch unused 1 65536 0 0 0 0 1 50 0 20\nvoice 0 route4")
    result = run("carrier-bound-upper", [fixture("carrier-bound-upper", above)], level_policy="carrier-bound")
    check("optional mode refuses actual clipping at unused upper carrier bound", not result["ok"] and
          result["passes"][-1]["play"]["clipped"] > 0 and
          result["passes"][-1]["carrier_bound_step"]["applied_delta_db10"] == 0 and
          "carrier bound prevents" in result["message"])
    ordinary = run("carrier-bound-target", [route_path], level_policy="carrier-bound")
    check("optional mode reports normal target attainment", ordinary["ok"] and ordinary["levelling"]["target_met"] is True
          and ordinary["levelling"]["status"] == "target_met")
    check("optional mode produces identical TL when strict can converge", (work / "carrier-bound-target.tl").read_bytes() ==
          (work / "route.tl").read_bytes())
    with mock.patch.object(keys, "level_delta", return_value=15):
        result = run("carrier-bound-ceiling", [route_path], level_policy="carrier-bound")
    check("optional mode retains eight-pass nonconvergence refusal", not result["ok"] and len(result["passes"]) == 8)

    # Independent existing-record comparison: native canonicalize the committed
    # Keys records; do not call the old builder or modify its source files.
    standard = keys.ROOT / "music/waltz"
    source_pool = [standard / "session.tl", *sorted(path for path in standard.glob("*.tl") if path.name != "session.tl")]
    for mirror in (False, True):
        name = "legacy-waltz" + ("-mirror" if mirror else "")
        result = run(name, source_pool, mirror)
        check(name + ": conversion succeeds", result["ok"])
        original = keys.ROOT / ("music/waltz-keys" + ("-mirror" if mirror else ""))
        paths = [original / "session.tl", *sorted(path for path in original.glob("*.tl") if path.name != "session.tl")]
        runner = keys.generator.BoundedRunner(keys.fm)
        canonical = work / (name + ".committed-canonical.tl")
        argv = ["--play", "--merged", canonical, "--", *paths]
        response = runner.run(harness, argv, dict(sha256=keys.fm.sha(harness.read_bytes())))
        keys.dump(work / (name + ".comparison.json"), dict(argv=list(map(str, argv)), processes=runner.processes))
        check(name + ": committed original still matches", response.returncode == 0 and keys.fm.play_fields(response.stdout)["match"] is True)
        old_rows = canonical.read_text().splitlines()
        new_rows = (work / (name + ".tl")).read_text().splitlines()
        def is_pos(row):
            return keys.event(row.split()) and row.split()[3] == "pos"
        check(name + ": headers, levels and non-spatial order parity", [row for row in old_rows if not is_pos(row)] ==
              [row for row in new_rows if not is_pos(row)])
        check(name + ": exact position rows parity", sorted(row for row in old_rows if is_pos(row)) == sorted(row for row in new_rows if is_pos(row)))
        check(name + ": exact PCM parity", keys.fm.play_fields(response.stdout)["hash"] == result["output"]["pcm_hash"])
    check("no WAV created", not list(work.rglob("*.wav")))
    print(json.dumps(dict(ok=True, assertions=len(records), work=str(work))))


if __name__ == "__main__":
    main()
