#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""MIDI conversion checks using the independent existing SMF reader.

Run after make: python3 tests/midi_export.py
Set JOTTUNE_TEST_HARNESS to test another already-built native renderer.
No external Python packages, persisted audio or device I/O are required.
"""
import copy
from decimal import localcontext
from fractions import Fraction
from pathlib import Path
import os
import tempfile
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "tools/music")]
import fm
import midi_export
import midi2tl
import interchange
from export_schema import SCHEMA


def row(identity, section, **raw):
    return dict(id=identity, section=section, raw=raw)


def event(kind, time=0, voice=0, **values):
    return row("event_" + kind, "event", T_NS=time, STREAM="s", VOICE=voice, **values)


def note(time=0, voice=0, frequency=440000, velocity=32767):
    return event("on", time, voice, FREQ_MILLIHZ=frequency, VELOCITY_Q15=velocity)


def source(events=None, voices=1, length=1_000_000_000, extra=()):
    rows = [row("header_length_ns", "header", NS=length),
            row("header_streams", "header", NAME="s"),
            row("patch", "patch", PATCH="tone", OP=0)]
    rows += [row("header_voice", "header", INDEX=v, PATCH="tone") for v in range(voices)]
    rows += list(extra) + (events if events is not None else [note(), event("off", 500_000_000)])
    return fm.RenderReply(ok=True, exit=0, _rows=rows)


class MidiChecks(unittest.TestCase):
    def decoded(self, accepted=None, **kwargs):
        if accepted is None:
            accepted = source()
        data, report = midi_export.encode(accepted, channels=kwargs.pop("channels", {0: 1}), **kwargs)
        info, events, tempos = midi2tl.parse_smf(data)
        self.assertEqual(info["trailing"], 0)
        self.assertEqual(info["no_end"], 0)
        self.assertEqual(info["after_end"], 0)
        self.assertEqual(info["header_tracks"], info["tracks"])
        self.assertEqual(info["format"], report["midi_format"])
        self.assertEqual(tempos, [(0, 0, 0, report["tempo_us"])])
        return data, report, info, events

    def rejects(self, accepted=None, **kwargs):
        with self.assertRaises(fm.ToolError):
            midi_export.encode(accepted if accepted is not None else source(),
                               channels=kwargs.pop("channels", {0: 1}), **kwargs)

    def test_type1_notes_tempo_track_and_independent_time(self):
        _, report, info, events = self.decoded()
        self.assertEqual(info["tracks"], 2)
        notes = [e for e in events if e[3] in (0x80, 0x90)]
        self.assertEqual([(e[0], e[3], e[4], e[5], e[6]) for e in notes],
                         [(0, 0x90, 0, 69, 127), (960, 0x80, 0, 69, 0)])
        clock = midi2tl.TempoMap(info, [(0, 0, 0, 500000)])
        self.assertEqual(clock.ns(notes[1][0]), 500_000_000)
        self.assertEqual(report["counts"]["notes"], 1)
        self.assertEqual(report["end_tick"], 1920)

    def test_type0_preserves_equal_tick_cross_voice_order(self):
        accepted = source([note(0, 1), note(0, 0), event("off", 500_000_000, 0),
                           event("off", 500_000_000, 1)], voices=2)
        _, report, info, events = self.decoded(accepted, channels={0: 4, 1: 2}, midi_format=0)
        self.assertEqual(info["tracks"], 1)
        self.assertEqual([(e[3], e[4]) for e in events if e[3] in (0x80, 0x90)],
                         [(0x90, 1), (0x90, 3), (0x80, 3), (0x80, 1)])
        self.assertEqual([r["voice"] for r in report["event_trace"] if r["kind"] == "note_on"], [1, 0])

    def test_type1_names_and_explicit_rpn_bend_setup(self):
        data, report, _, events = self.decoded(channels={0: 10}, bend_range=12)
        self.assertIn(b"Voice 0: tone", data)
        setup = [(e[4], e[5], e[6]) for e in events if e[3] == 0xb0]
        self.assertEqual(setup, [(9, 101, 0), (9, 100, 0), (9, 6, 12),
                                (9, 38, 0), (9, 101, 127), (9, 100, 127)])
        self.assertFalse(any(e[3] == 0xc0 for e in events))
        self.assertEqual(report["channel_map"][0]["channel"], 10)

    def test_absolute_rounding_half_up_and_error_fraction(self):
        accepted = source([note(500), event("off", 1500)], length=2500)
        _, report, _, events = self.decoded(accepted, ppqn=1000, tempo_us=1000)
        self.assertEqual([(r["tick"], r["error_ns_numerator"], r["error_ns_denominator"])
                          for r in report["timing"]["rows"]], [(1, 500, 1), (2, 500, 1), (3, 500, 1)])
        self.assertEqual([e[0] for e in events if e[3] in (0x80, 0x90)], [1, 2])

    def test_quantization_keeps_order_when_distinct_times_collapse(self):
        accepted = source([note(1), event("off", 2), note(3), event("off", 4)])
        _, _, _, events = self.decoded(accepted, midi_format=0)
        self.assertEqual([(e[0], e[3]) for e in events if e[3] in (0x80, 0x90)],
                         [(0, 0x90), (0, 0x80), (0, 0x90), (0, 0x80)])

    def test_pitch_bends_keep_base_note_in_release(self):
        accepted = source([note(), event("off", 100_000_000),
                           event("pitch", 200_000_000, FREQ_MILLIHZ=466164)])
        _, report, _, events = self.decoded(accepted)
        self.assertEqual(report["pitch"]["rows"][1]["base_key"], 69)
        self.assertAlmostEqual(report["pitch"]["rows"][1]["signed_bend"], 4096, delta=1)
        self.assertEqual(len([e for e in events if e[3] == 0x90]), 1)
        self.assertEqual(report["counts"]["pitch_events"], 1)

    def test_held_retrigger_off_then_bend_then_on(self):
        accepted = source([note(), note(100_000_000, frequency=523251)])
        _, report, _, _ = self.decoded(accepted)
        retrigger = [r for r in report["event_trace"] if r["source_index"] == 1]
        self.assertEqual([r["kind"] for r in retrigger], ["note_off", "pitch_bend", "note_on"])
        self.assertEqual(retrigger[0]["bytes"][1], 69)
        self.assertEqual(retrigger[-1]["bytes"][1], 72)
        self.assertEqual(report["counts"]["retrigger_note_offs"], 1)
        self.assertEqual(report["counts"]["end_note_offs"], 1)

    def test_endpoint_off_and_eot_match_endpoint(self):
        _, report, info, events = self.decoded(source([note()]))
        self.assertEqual([e[0] for e in events if e[3] == 0x80], [report["end_tick"]])
        self.assertEqual(info["end_tick"], report["end_tick"])

    def test_velocity_minimum_quantization_and_native_clamp(self):
        accepted = source([note(velocity=1), event("off", 100),
                           note(200, velocity=65535), event("off", 300)])
        _, report, _, events = self.decoded(accepted)
        self.assertEqual([e[6] for e in events if e[3] == 0x90], [1, 127])
        self.assertEqual(report["velocity"]["rows"][1]["effective_q15"], 32767)
        self.assertTrue(report["velocity"]["rows"][1]["native_clamped"])
        self.assertEqual(report["velocity"]["rows"][0]["imported_q15"], 32767 // 127)
        self.rejects(source([note(velocity=0)]))

    def test_unsupported_known_rows_reported_and_not_emitted(self):
        extra = [row("filter", "patch", PATCH="tone"), row("gain", "fx", DB10=-60),
                 row("wtdata", "asset", BANK="w")]
        events = [note()] + [event(kind, 100) for kind in ("ctl", "filter", "send", "pos", "move", "yaw")]
        _, report, _, decoded = self.decoded(source(events, extra=extra))
        identifiers = {r["id"] for r in report["losses"]}
        self.assertTrue({"filter", "gain", "wtdata", "event_ctl", "event_filter", "event_send", "event_pos", "event_move", "event_yaw"} <= identifiers)
        self.assertEqual(len([e for e in decoded if e[3] == 0xb0]), 6)
        self.assertTrue(report["requires_accept_loss"])
        self.assertFalse(report["audio_equivalent"])

    def test_generator_and_unknown_schema_refuse(self):
        for bad in (row("gen", "generator"), row("event_gen", "event"),
                    row("event_lane", "event"), row("event_future", "event"),
                    row("future", "patch"), row("header_future", "header")):
            with self.subTest(bad=bad):
                self.rejects(source(extra=[bad]))
        expanded = note()
        expanded["raw"]["FUTURE_FIELD"] = 1
        self.rejects(source([expanded]))
        missing = note()
        del missing["raw"]["FREQ_MILLIHZ"]
        self.rejects(source([missing]))

    def test_mapping_refusals_and_inactive_known_mapping(self):
        for mapping in ({}, {0: 0}, {0: 17}, {0: True}, {True: 1}, {1: 1}, {"0": 1}, []):
            with self.subTest(mapping=mapping):
                self.rejects(channels=mapping)
        self.rejects(source(voices=2), channels={0: 1, 1: 1})
        _, report, _, _ = self.decoded(source(voices=2), channels={0: 1, 1: 2})
        self.assertEqual(len(report["channel_map"]), 2)

    def test_argument_bounds_refuse_bool_and_illegal_values(self):
        for key, values in dict(ppqn=(0, 32768, True), tempo_us=(0, 0x1000000, True),
                                midi_format=(-1, 2, False), bend_range=(0, 128, 0.5, True)).items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.rejects(**{key: value})

    def test_native_failure_and_bad_order_refuse(self):
        for status in (1, 2, 3, 4, 5):
            accepted = source()
            accepted["exit"] = status
            self.rejects(accepted)
        self.rejects(source([note(100), event("off", 99)]))
        self.rejects(source([note(1_000_000_000)]))

    def test_pitch_outside_range_and_bend_asymmetry(self):
        for frequency in (0, 1, 0xffffffff):
            self.rejects(source([note(frequency=frequency)]))
        self.rejects(source([note(), event("pitch", 100, FREQ_MILLIHZ=880000)]))
        with localcontext() as context:
            context.prec = 50
            self.assertEqual(midi_export._pitch(220000, 12, 69)[1], 0)
            with self.assertRaises(fm.ToolError):
                midi_export._pitch(880000, 12, 69)

    def test_pitch_before_note_and_extra_off_reported(self):
        events = [event("pitch", FREQ_MILLIHZ=440000), event("off", 100),
                  note(200), event("off", 300), event("off", 400)]
        _, report, _, _ = self.decoded(source(events))
        self.assertEqual(report["counts"]["ignored_pitch_before_note"], 1)
        self.assertEqual(report["counts"]["ignored_off_without_held_note"], 2)

    def test_vlq_bounds_and_native_duration_overflow_refuse(self):
        for value, expected in ((0, b"\0"), (127, b"\x7f"), (128, b"\x81\0"),
                                (0x0fffffff, b"\xff\xff\xff\x7f")):
            self.assertEqual(midi_export._vlq(value), expected)
        for value in (-1, 0x10000000):
            with self.assertRaises(fm.ToolError):
                midi_export._vlq(value)
        self.rejects(source(length=0xffffffffffffffff), ppqn=32767, tempo_us=1)

    def test_source_immutable_and_deterministic(self):
        accepted = source([note(frequency=445000), event("pitch", 1, FREQ_MILLIHZ=440000)])
        original = copy.deepcopy(accepted)
        first = midi_export.encode(accepted, channels={0: 7})
        second = midi_export.encode(accepted, channels={0: 7})
        self.assertEqual(first, second)
        self.assertEqual(accepted, original)
        with localcontext() as context:
            context.prec = 5
            self.assertEqual(first, midi_export.encode(accepted, channels={0: 7}))

    def test_trace_matches_independent_channel_decoder(self):
        for midi_format in (0, 1):
            _, report, _, events = self.decoded(source([note(), note(100, 1),
                event("pitch", 200, 1, FREQ_MILLIHZ=445000)], voices=2),
                channels={0: 16, 1: 3}, midi_format=midi_format)
            expected = []
            for r in sorted(enumerate(report["event_trace"]), key=lambda pair: (pair[1]["track"], pair[0])):
                row_ = r[1]
                status, d1, d2 = row_["bytes"]
                expected.append((row_["tick"], row_["track"], status & 0xf0, status & 15, d1, d2))
            self.assertEqual([(e[0], e[1], e[3], e[4], e[5], e[6]) for e in events], expected)


class NativeMidiChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native = fm.Native(os.environ.get("JOTTUNE_TEST_HARNESS", fm.DEFAULT_HARNESS))

    def test_native_descriptors_have_explicit_export_classification(self):
        classified = {(section, identity) for section, identities in SCHEMA.items() for identity in identities}
        native = {(statement["section"], identity)
                  for identity, statement in self.native.metadata.statements.items()
                  if statement["section"] != "generator" and identity not in ("event_gen", "event_lane")}
        self.assertEqual(native, classified, "Review new native semantics before allowing external conversion")
        for identity, fields in midi_export.INTERPRETED_FIELDS.items():
            self.assertEqual(fields, {p["name"] for p in self.native.metadata.by_statement[identity]})

    def test_new_fx_and_field_records_keep_notes_and_report_dropped_automation(self):
        for name in ("crush.tl", "crush-stereo.tl", "crush-44100.tl", "field-bin1-48000.tl"):
            with self.subTest(fixture=name):
                accepted = interchange.accepted_source(ROOT / "tests/golden/fx" / name, native=self.native)
                notes = [r["raw"] for r in accepted["_rows"] if r["id"] == "event_on"]
                voices = {r["raw"]["INDEX"]: r["raw"]["INDEX"] + 1
                          for r in accepted["_rows"] if r["id"] == "header_voice"}
                expected_crush = sum(r["id"] == "event_crush" for r in accepted["_rows"])
                for midi_format in (0, 1):
                    data, report = midi_export.encode(accepted, channels=voices, midi_format=midi_format)
                    info, events, tempos = midi2tl.parse_smf(data)
                    self.assertEqual(info["format"], midi_format)
                    self.assertEqual(info["trailing"], 0)
                    self.assertEqual(info["no_end"], 0)
                    decoded_notes = [e for e in events if e[3] == 0x90]
                    self.assertEqual(len(decoded_notes), len(notes))
                    clock = midi2tl.TempoMap(info, tempos)
                    half_tick = Fraction(report["tempo_us"] * 1000, 2 * report["ppqn"])
                    # Compare the decoded wire event times with native input,
                    # without reading the writer's event trace as the oracle.
                    for voice, channel in voices.items():
                        original = [r["T_NS"] for r in notes if r["VOICE"] == voice]
                        decoded = [clock.ns(e[0]) for e in decoded_notes if e[4] == channel - 1]
                        self.assertEqual(len(decoded), len(original))
                        for actual, wanted in zip(decoded, original):
                            self.assertLessEqual(abs(actual - wanted), half_tick + 1)
                    losses = {r["id"]: r for r in report["losses"]}
                    self.assertEqual(losses["crush"]["count"], 1)
                    self.assertEqual(losses["event_crush"]["count"], expected_crush)
                    self.assertFalse(report["audio_equivalent"])
                    if name.startswith("field"):
                        self.assertTrue({"event_move", "event_yaw", "event_send", "header_pinna"} <= losses.keys())

    def test_materializing_midi_requires_loss_acceptance_and_preserves_source(self):
        source_path = ROOT / "tests/golden/fx/crush.tl"
        original = source_path.read_bytes()
        options = dict(profile="midi", harness=self.native.path, channels={0: 1, 1: 2})
        with tempfile.TemporaryDirectory(prefix="jottune-midi-") as directory:
            out = Path(directory) / "bundle"
            dry = interchange.export(source_path, **options)
            self.assertTrue(dry["ok"], dry)
            self.assertTrue(dry["dry_run"])
            self.assertFalse(dry["published"])
            refused = interchange.export(source_path, out=out, **options)
            self.assertFalse(refused["ok"])
            self.assertIn("accept_loss", refused["message"])
            self.assertFalse(out.exists())
            exported = interchange.export(source_path, out=out, accept_loss=True, **options)
            self.assertTrue(exported["ok"], exported)
            self.assertTrue(exported["published"])
            self.assertTrue(interchange.verify(out)["ok"])
            data = (out / "performance.mid").read_bytes()
            info, events, _ = midi2tl.parse_smf(data)
            self.assertEqual(info["format"], 1)
            self.assertEqual(sum(e[3] == 0x90 for e in events), 4)
            replay = fm.native_render(self.native, [out / "source.tl"])
            self.assertEqual(replay["exit"], 0)
            refused = interchange.export(source_path, out=out, accept_loss=True, **options)
            self.assertFalse(refused["ok"])
            self.assertEqual((out / "performance.mid").read_bytes(), data)
        self.assertEqual(source_path.read_bytes(), original)

    def test_generated_and_hash_mismatched_inputs_publish_nothing(self):
        with tempfile.TemporaryDirectory(prefix="jottune-midi-refusal-") as directory:
            out = Path(directory) / "bundle"
            generated = ROOT / "tests/golden/gen/genpink-44100.tl"
            result = interchange.export(generated, profile="midi", harness=self.native.path,
                                         channels={0: 1}, out=out, accept_loss=True)
            self.assertFalse(result["ok"])
            self.assertIn("generators", result["message"])
            self.assertFalse(out.exists())
            invalid = Path(directory) / "wrong-hash.tl"
            invalid.write_text((ROOT / "tests/golden/fx/crush.tl").read_text().replace(
                "hash 95d0cfbb8792eb0a", "hash 0000000000000000"))
            result = interchange.export(invalid, profile="midi", harness=self.native.path,
                                         channels={0: 1, 1: 2}, out=out, accept_loss=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["exit"], 3)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
