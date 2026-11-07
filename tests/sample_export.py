#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Sample-grid contracts against an explicitly selected built native renderer.

Run: python3 tests/sample_export.py [--harness PATH]
Every generated cell is replayed natively; no external sampler is assumed.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import fm
import interchange
import sample_export

PARSER = argparse.ArgumentParser(add_help=False)
PARSER.add_argument("--harness", type=Path)
ARGS, REMAINING = PARSER.parse_known_args()


class Samples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native = fm.Native(ARGS.harness)
        cls.accepted = fm.native_render(cls.native, [ROOT / "tests/golden/48000/bell.tl"])
        cls.other_rate = fm.native_render(cls.native, [ROOT / "tests/golden/44100/bell.tl"])

    def build(self, accepted=None, **options):
        values = dict(patch="bell", notes=[69], velocities=[127], hold_ms=20, tail_ms=10, max_samples=1)
        values.update(options)
        return sample_export.build(self.accepted if accepted is None else accepted, self.native, **values)

    def refuses_before_render(self, accepted=None, **options):
        with mock.patch.object(fm, "native_render", side_effect=AssertionError("unexpected sample render")):
            with self.assertRaises(fm.ToolError) as caught:
                self.build(accepted, **options)
        return caught.exception

    def accept_text(self, extra="", fs=48000):
        text = self.accepted["_record_bytes"].decode()
        text = "\n".join(line for line in text.splitlines() if not line.startswith("hash ")) + "\n"
        text = text.replace("fs 48000\n", f"fs {fs}\n").replace("timeline draft1\n", "timeline draft2\n")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.tl"
            path.write_text(text + extra)
            return fm.native_render(self.native, [path])

    def regions(self, data):
        rows = []
        for line in data.decode("ascii").splitlines():
            if line.startswith("//"):
                continue
            tokens = line.split()
            self.assertEqual(tokens[0], "<region>")
            values = dict(token.split("=", 1) for token in tokens[1:])
            self.assertEqual(len(values), len(tokens) - 1)
            self.assertEqual(set(values), {"sample", "key", "pitch_keycenter", "lovel", "hivel",
                                           "amp_veltrack", "loop_mode"})
            self.assertRegex(values["sample"], r"^n[0-9]{3}-v[0-9]{3}\.wav$")
            self.assertEqual(values["loop_mode"], "one_shot")
            self.assertEqual(values["amp_veltrack"], "0")
            rows.append(values)
        return rows

    def test_every_cell_independent_native_replay_and_recorded_pitch_velocity(self):
        files, report = self.build(notes=[72, 69], velocities=[127, 63], max_samples=4)
        self.assertEqual(report["notes"], [69, 72])
        self.assertEqual(report["velocities"], [63, 127])
        self.assertEqual(report["samples"], report["native_render_attempts"])
        self.assertEqual(report["retained_bytes"], sum(map(len, files.values())))
        self.assertLessEqual(report["retained_bytes"], report["predicted_retained_bytes"])
        self.assertEqual(len(files), 9)
        source_patches = [r["raw"] for r in self.accepted["_rows"] if r["section"] == "patch"]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for cell in report["cells"]:
                record = files[cell["record"]]
                rows = self.native.metadata.canonical(record)
                self.assertEqual([r["raw"] for r in rows if r["section"] == "patch"], source_patches)
                events = [r for r in rows if r["section"] == "event"]
                self.assertEqual([r["id"] for r in events], ["event_on", "event_off"])
                self.assertEqual(events[0]["raw"]["FREQ_MILLIHZ"], {69: 440000, 72: 523251}[cell["note"]])
                self.assertEqual(events[0]["raw"]["VELOCITY_Q15"], cell["midi_velocity"] * 32767 // 127)
                self.assertEqual(events[1]["raw"]["T_NS"], 20_000_000)
                source, audio = directory / cell["record"], directory / cell["wav"]
                source.write_bytes(record)
                replay = self.native.run(["--play", "--wav", audio, "--", source])
                self.assertEqual(replay.returncode, 0, replay.stderr)
                self.assertIn("match=yes", replay.stdout)
                self.assertEqual(audio.read_bytes(), files[cell["wav"]])
                self.assertEqual(fm.sha(record), cell["record_sha256"])
                self.assertEqual(fm.sha(audio.read_bytes()), cell["wav_sha256"])

    def test_sfz_exact_key_and_nearest_velocity_coverage(self):
        files, report = self.build(notes=[60, 72], velocities=[21, 81, 127], max_samples=6)
        regions = self.regions(files["instrument.sfz"])
        for key in range(128):
            for velocity in range(1, 128):
                matches = [r for r in regions if int(r["key"]) == key
                           and int(r["lovel"]) <= velocity <= int(r["hivel"])]
                self.assertEqual(len(matches), 1 if key in (60, 72) else 0)
                if matches:
                    expected = min((21, 81, 127), key=lambda v: (abs(v - velocity), v))
                    self.assertEqual(matches[0]["sample"], f"n{key:03d}-v{expected:03d}.wav")
                    self.assertEqual(matches[0]["key"], matches[0]["pitch_keycenter"])
        self.assertEqual(report["velocity_regions"], [dict(velocity=21, lovel=1, hivel=51),
            dict(velocity=81, lovel=52, hivel=104), dict(velocity=127, lovel=105, hivel=127)])

    def test_alternate_rate_and_exact_frame_geometry(self):
        files, report = self.build(self.other_rate, hold_ms=13, tail_ms=8)
        self.assertEqual(report["sample_rate"], 44100)
        self.assertEqual(report["frames_per_sample"], 926)
        with wave.open(io.BytesIO(files[report["cells"][0]["wav"]]), "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()),
                             (44100, 1, 2, 926))

    def test_drops_song_fx_and_spatial_context_and_reports_loss(self):
        accepted = self.accept_text("gain -60\nspace stereo\n0 golden 0 pos 1000 1000 0\n")
        files, report = self.build(accepted)
        self.assertIn("gain", report["dropped_context"])
        self.assertIn("header_space", report["dropped_context"])
        self.assertIn("event_pos", report["dropped_context"])
        self.assertFalse(report["normalization"])
        self.assertIsNone(report["tail_complete"])
        self.assertFalse(report["external_playback_verified"])
        record = files[report["cells"][0]["record"]].decode()
        self.assertNotIn("gain ", record)
        self.assertNotIn("space stereo", record)
        self.assertIn("space off", record)
        self.assertNotIn(" pos ", record)
        self.assertEqual(report["cells"][0]["channels"], 1)

    def test_current_crusher_and_field_context_is_accepted_and_explicitly_dropped(self):
        for fixture in ("fx/crush.tl", "fx/field-bin1-48000.tl"):
            with self.subTest(fixture=fixture):
                accepted = fm.native_render(self.native, [ROOT / "tests/golden" / fixture])
                patch = next(row["raw"]["PATCH"] for row in accepted["_rows"] if row["id"] == "patch")
                files, report = self.build(accepted, patch=patch)
                dropped = {row["id"] for row in accepted["_rows"] if row["section"] in ("event", "fx", "space")}
                self.assertEqual(set(report["dropped_context"]), dropped)
                if fixture == "fx/crush.tl":
                    self.assertIn("crush", dropped)
                    self.assertIn("event_crush", dropped)
                cell = report["cells"][0]
                rows = self.native.metadata.canonical(files[cell["record"]])
                self.assertFalse(any(row["id"] in ("crush", "event_crush") for row in rows))
                self.assertEqual(cell["channels"], 1)

    def test_asset_closure_and_external_controls_at_zero(self):
        extra = ("wtbank wanted 1 32 12\nharmonic wanted 0 1 0 0\n"
                 "wtbank unused 1 32 12\nharmonic unused 0 2 0 0\n"
                 "wtuse bell 0 wanted 0\nmod bell 0 ctl0 level0 -60 0\n")
        accepted = self.accept_text(extra)
        files, report = self.build(accepted)
        self.assertEqual(report["assets"], ["wanted"])
        self.assertEqual(report["controller_sources_at_initial_zero"], ["ctl0"])
        record = files[report["cells"][0]["record"]]
        rows = self.native.metadata.canonical(record)
        self.assertEqual({r["raw"]["BANK"] for r in rows if r["section"] == "asset"}, {"wanted"})
        before = [r["raw"] for r in accepted["_rows"] if r["section"] == "asset" and r["raw"]["BANK"] == "wanted"]
        self.assertEqual([r["raw"] for r in rows if r["section"] == "asset"], before)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cell.tl"
            path.write_bytes(record)
            replay = fm.native_render(self.native, [path], want_wav=True)
            self.assertEqual(replay["exit"], 0)
            self.assertEqual(replay["_wav_bytes"], files[report["cells"][0]["wav"]])

    def test_unordered_arguments_produce_same_files(self):
        first, _ = self.build(notes=[60, 72], velocities=[31, 127], max_samples=4)
        second, _ = self.build(notes=[72, 60], velocities=[127, 31], max_samples=4)
        self.assertEqual(first, second)

    def test_invalid_arguments_and_budget_fail_before_native_cells(self):
        cases = [dict(notes=[]), dict(notes=[True]), dict(notes=[-1]), dict(notes=[128]),
            dict(notes=[60, 60]), dict(notes="60"), dict(velocities=[]), dict(velocities=[0]),
            dict(velocities=[128]), dict(velocities=[1, 1]), dict(velocities=[False]),
            dict(hold_ms=0), dict(hold_ms=1.5), dict(tail_ms=0), dict(tail_ms=True),
            dict(max_samples=0), dict(max_samples=257), dict(max_samples=True),
            dict(notes=[60, 61]), dict(hold_ms=1_000_000), dict(hold_ms=2**64),
            dict(patch="missing"), dict(patch="bell\nvoice")]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.refuses_before_render(**case).exit, 2)

    def test_native_duration_and_tail_frame_bounds(self):
        low = self.accept_text(fs=1)
        self.assertEqual(self.refuses_before_render(low, hold_ms=1, tail_ms=1).exit, 2)
        self.assertEqual(self.refuses_before_render(low, hold_ms=1000, tail_ms=1).exit, 2)
        short = self.accept_text(fs=100)
        self.assertIn("positive native frames", str(self.refuses_before_render(short, notes=[0],
                      hold_ms=1, tail_ms=10)))

    def test_root_at_or_above_source_nyquist_refuses_whole_grid_before_cells(self):
        low = self.accept_text(fs=2000)
        error = self.refuses_before_render(low, notes=[0, 127], max_samples=2)
        self.assertIn("source Nyquist", str(error))
        equal = self.accept_text(fs=880)
        self.assertIn("440000mHz", str(self.refuses_before_render(equal, notes=[69])))

    def test_source_identity_and_metadata_must_match(self):
        for key, value in (("exit", 3), ("ok", False), ("record_sha256", "0" * 64),
                           ("fs", 44100), ("_rows", [])):
            accepted = copy.deepcopy(self.accepted)
            accepted[key] = value
            with self.subTest(key=key):
                self.refuses_before_render(accepted)
        accepted = copy.deepcopy(self.accepted)
        accepted["native"]["sha256"] = "0" * 64
        self.assertEqual(self.refuses_before_render(accepted).exit, 5)

    def test_generators_refuse_before_cells(self):
        accepted = fm.native_render(self.native, [ROOT / "tests/golden/gen/genpink-48000.tl"])
        name = next(r["raw"]["PATCH"] for r in accepted["_rows"] if r["id"] == "patch")
        self.assertIn("generators", str(self.refuses_before_render(accepted, patch=name)))

    def test_unknown_structural_and_mod_dependencies_refuse(self):
        for bad_row in (dict(section="patch", id="future_patch", raw={}),
                        dict(section="asset", id="future_asset", raw={}),
                        dict(section="patch", id="mod", raw={"SOURCE": "gen.0.x"}),
                        dict(section="patch", id="mod", raw={"SOURCE": "external.0"})):
            accepted = copy.deepcopy(self.accepted)
            accepted["_rows"].append(bad_row)
            with mock.patch.object(self.native.metadata, "canonical", return_value=accepted["_rows"]):
                self.refuses_before_render(accepted)

    def test_native_failure_reports_partial_attempts_without_publication(self):
        original = fm.native_render
        attempts = []
        def fail_second(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 2:
                raise fm.ToolError("injected native failure", 5, original_reason="preserved")
            return original(*args, **kwargs)
        with mock.patch.object(fm, "native_render", side_effect=fail_second):
            with self.assertRaises(fm.ToolError) as caught:
                self.build(notes=[60, 61], max_samples=2)
        self.assertEqual(caught.exception.exit, 5)
        self.assertEqual(caught.exception.details["original_reason"], "preserved")
        details = caught.exception.details["sample_export"]
        self.assertEqual((details["native_render_attempts"], details["completed_samples"], details["published"]),
                         (2, 1, False))

    def test_native_budget_hook_counts_one_render_per_cell(self):
        calls = []
        self.native.budget_hook = lambda: calls.append(1)
        try:
            _, report = self.build(notes=[60, 62], velocities=[63, 127], max_samples=4)
        finally:
            self.native.budget_hook = None
        self.assertEqual(len(calls), 4)
        self.assertEqual(report["native_render_attempts"], 4)

    def test_receiver_profile_validation_and_cli_dispatch_before_native(self):
        invalid = [(profile, value) for profile in ("sfz", "sfz-bank")
                   for value in ("", "unknown", True, 1, [], {})]
        invalid += [(profile, "sfizz-ram") for profile in ("wav", "midi")]
        with mock.patch.object(fm, "Native", side_effect=AssertionError("unexpected native work")) as native:
            for profile, value in invalid:
                with self.subTest(profile=profile, value=value):
                    result = interchange.export("unused", profile=profile, receiver_profile=value)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["exit"], 2)
            for profile in ("wav", "midi"):
                with self.subTest(cli_profile=profile), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        interchange.main([profile, "unused", "--receiver-profile", "sfizz-ram"])
                    self.assertEqual(caught.exception.code, 2)
            native.assert_not_called()
        for profile, arguments in (("sfz", ["--key", "60"]),
            ("sfz-bank", ["--patch", "bell", "--notes", "60", "--velocities", "40",
                          "--hold-ms", "20", "--tail-ms", "10", "--max-samples", "1"])):
            with self.subTest(cli_profile=profile):
                with mock.patch.object(interchange, "export", return_value={"exit": 0}) as exported:
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(interchange.main([profile, "unused", *arguments,
                            "--receiver-profile", "sfizz-ram"]), 0)
                    self.assertEqual(exported.call_args.kwargs["receiver_profile"], "sfizz-ram")
                    self.assertEqual(exported.call_args.kwargs["profile"], profile)
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        interchange.main([profile, "unused", *arguments, "--receiver-profile", "unknown"])
                    self.assertEqual(caught.exception.code, 2)

    def test_receiver_profile_preserves_default_audio_and_canonical_assets(self):
        source = ROOT / "tests/golden/48000/bell.tl"
        before = source.read_bytes()
        profiles = [dict(profile="sfz", key=60), dict(profile="sfz-bank", patch="bell",
            notes=[60, 64], velocities=[40, 100], hold_ms=20, tail_ms=10, max_samples=4, accept_loss=True)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for options in profiles:
                with self.subTest(profile=options["profile"]):
                    default, selected = root / (options["profile"] + "-default"), root / (options["profile"] + "-ram")
                    baseline = interchange.export(source, harness=self.native.path, out=default, **options)
                    result = interchange.export(source, harness=self.native.path, out=selected,
                                                receiver_profile="sfizz-ram", **options)
                    self.assertTrue(baseline["ok"], baseline)
                    self.assertTrue(result["ok"], result)
                    self.assertNotIn("receiver_profile", baseline["report"])
                    self.assertEqual({path.name for path in default.iterdir()}, {path.name for path in selected.iterdir()})
                    wav_bytes = 0
                    for path in default.iterdir():
                        if path.suffix in (".tl", ".wav"):
                            self.assertEqual(path.read_bytes(), (selected / path.name).read_bytes())
                        if path.suffix == ".wav":
                            wav_bytes += path.stat().st_size
                    self.assertEqual((selected / "instrument.sfz").read_bytes(),
                                     b"<control> hint_ram_based=1\n" + (default / "instrument.sfz").read_bytes())
                    self.assertEqual(result["report"]["receiver_profile"], dict(name="sfizz-ram",
                        opcode="hint_ram_based=1", full_ram_requested=True, wav_file_bytes=wav_bytes))
                    without_profile = copy.deepcopy(result["report"])
                    without_profile.pop("receiver_profile")
                    if options["profile"] == "sfz-bank":
                        added = len(b"<control> hint_ram_based=1\n")
                        for field in ("retained_bytes", "predicted_retained_bytes"):
                            self.assertEqual(without_profile["samples"][field], baseline["report"]["samples"][field] + added)
                            without_profile["samples"][field] -= added
                        self.assertEqual(result["report"]["samples"]["retained_bytes"],
                            sum(path.stat().st_size for path in selected.iterdir()
                                if path.name.startswith("n") or path.name == "instrument.sfz"))
                    self.assertEqual(baseline["report"], without_profile)
                    self.assertTrue((selected / "README.txt").read_bytes().startswith((default / "README.txt").read_bytes()))
                    self.assertIn("not a receiver memory bound", (selected / "README.txt").read_text())
                    for directory, exported in ((default, baseline), (selected, result)):
                        self.assertTrue(interchange.verify(directory,
                            expected_manifest_sha256=exported["bundle"]["manifest_sha256"])["ok"])
            # Reuse the small bank's bytes while mocking only its size accounting;
            # boundary controls must not allocate or render a 64MiB sample grid.
            sample_files = {path.name: path.read_bytes() for path in default.iterdir()
                            if path.name.startswith("n") or path.name == "instrument.sfz"}
            for label, predicted_over, actual_over in (("exact-limit", 0, 0),
                    ("predicted-overflow", 1, 0), ("actual-overflow", 0, 1)):
                with self.subTest(boundary=label):
                    details = copy.deepcopy(baseline["report"]["samples"])
                    limit = details["retained_byte_limit"]
                    added = len(b"<control> hint_ram_based=1\n")
                    details["predicted_retained_bytes"] = limit - added + predicted_over
                    details["retained_bytes"] = limit - added + actual_over
                    destination = root / label
                    with mock.patch.object(fm, "Native", return_value=self.native), \
                         mock.patch.object(interchange, "accepted_source", return_value=self.accepted), \
                         mock.patch.object(self.native, "run", side_effect=AssertionError("unexpected native work")), \
                         mock.patch.object(sample_export, "build", return_value=(sample_files, details)), \
                         mock.patch.object(interchange, "publish", wraps=interchange.publish) as published:
                        result = interchange.export(source, harness=self.native.path, out=destination,
                                                    receiver_profile="sfizz-ram", **profiles[-1])
                    if predicted_over or actual_over:
                        self.assertFalse(result["ok"], result)
                        self.assertEqual(result["exit"], 2)
                        self.assertFalse(result.get("published", False))
                        self.assertFalse(destination.exists())
                        published.assert_not_called()
                    else:
                        self.assertTrue(result["ok"], result)
                        self.assertTrue(result["published"])
                        self.assertTrue(destination.is_dir())
                        published.assert_called_once()
                        for field in ("predicted_retained_bytes", "retained_bytes"):
                            self.assertEqual(result["report"]["samples"][field], limit)
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *REMAINING])
