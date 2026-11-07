#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Complete local interchange bundles and hostile archive refusal checks."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import fm
import interchange

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--harness", type=Path)
ARGS, REMAINING = parser.parse_known_args()


class Bundles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="jottune-interchange-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.native = fm.Native(ARGS.harness)
        cls.source = ROOT / "tests/golden/48000/bell.tl"
        cls.seed = cls.root / "seed.zip"
        cls.result = interchange.export(cls.source, harness=ARGS.harness, profile="sfz", key=60,
                                        out=cls.seed, container="zip")
        assert cls.result["ok"], cls.result
        with zipfile.ZipFile(cls.seed) as archive:
            cls.members = {name:archive.read(name) for name in archive.namelist()}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def archive(self, members=None, name="candidate.zip", compression=zipfile.ZIP_DEFLATED):
        path = self.directory / name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "x", compression=compression) as archive:
                for member, content in (self.members.items() if members is None else members):
                    archive.writestr(member, content)
        return path

    def refused(self, path):
        result = interchange.verify(path)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["exit"], 5, result)
        return result

    def test_zip_is_one_complete_portable_song_with_exact_native_audio(self):
        result = interchange.verify(self.seed, expected_manifest_sha256=self.result["bundle"]["manifest_sha256"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["artifacts_verified"], 4)
        self.assertFalse(result["external_playback_verified"])
        self.assertEqual(set(self.members), {"source.tl", "audio.wav", "instrument.sfz", "README.txt", "manifest.json"})
        accepted = fm.native_render(self.native, [self.source], want_wav=True)
        self.assertEqual(self.members["source.tl"], accepted["_record_bytes"])
        self.assertEqual(self.members["audio.wav"], accepted["_wav_bytes"])
        self.assertIn(b"sample=audio.wav key=60", self.members["instrument.sfz"])
        self.assertEqual(self.result["bundle"]["bytes"], self.seed.stat().st_size)
        self.assertEqual(self.result["bundle"]["sha256"], fm.sha(self.seed.read_bytes()))

    def test_directory_compatibility_and_extract_then_verify(self):
        output = self.directory / "directory"
        result = interchange.export(self.source, harness=ARGS.harness, profile="sfz", key=60, out=output)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["bundle"]["directory"], str(output.resolve()))
        self.assertEqual({p.name:p.read_bytes() for p in output.iterdir()}, self.members)
        extracted = self.directory / "extracted"
        with zipfile.ZipFile(self.seed) as archive:
            archive.extractall(extracted)
        self.assertTrue(interchange.verify(extracted)["ok"])
        # Legacy directory verification covers declared members only.
        (extracted / "local-note.txt").write_text("Unverified listener note")
        self.assertTrue(interchange.verify(extracted)["ok"])

    def test_sampled_patch_zip_contains_every_cell_and_its_replay_source(self):
        source = ROOT / "tests/golden/fx/crush.tl"
        accepted = fm.native_render(self.native, [source])
        patch = next(row["raw"]["PATCH"] for row in accepted["_rows"] if row["id"] == "patch")
        output = self.directory / "bank.zip"
        result = interchange.export(source, harness=ARGS.harness, profile="sfz-bank", patch=patch,
            notes=[60,72], velocities=[127], hold_ms=20, tail_ms=10, max_samples=2,
            accept_loss=True, out=output, container="zip")
        self.assertTrue(result["ok"], result)
        self.assertTrue(interchange.verify(output)["ok"])
        self.assertIn("crush", result["report"]["samples"]["dropped_context"])
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(len(archive.namelist()), 8)
            for cell in result["report"]["samples"]["cells"]:
                source = self.directory / cell["record"]
                source.write_bytes(archive.read(cell["record"]))
                replay = fm.native_render(self.native, [source], want_wav=True)
                self.assertEqual(replay["_wav_bytes"], archive.read(cell["wav"]))

    def test_whole_song_zip_preserves_current_effect_and_field_audio(self):
        for fixture in ("fx/crush.tl", "fx/field-bin1-48000.tl"):
            with self.subTest(fixture=fixture):
                source = ROOT / "tests/golden" / fixture
                output = self.directory / (source.stem+".zip")
                result = interchange.export(source, harness=ARGS.harness, profile="sfz", key=60,
                                            out=output, container="zip")
                self.assertTrue(result["ok"], result)
                self.assertTrue(interchange.verify(output)["ok"])
                accepted = fm.native_render(self.native, [source], want_wav=True)
                with zipfile.ZipFile(output) as archive:
                    self.assertEqual(archive.read("audio.wav"), accepted["_wav_bytes"])
                    self.assertEqual(archive.read("source.tl"), accepted["_record_bytes"])

    def test_sampled_zip_requires_explicit_loss_acceptance(self):
        output = self.directory / "refused.zip"
        result = interchange.export(self.source, harness=ARGS.harness, profile="sfz-bank", patch="bell",
            notes=[60], velocities=[127], hold_ms=20, tail_ms=10, max_samples=1,
            out=output, container="zip")
        self.assertFalse(result["ok"])
        self.assertFalse(result["published"])
        self.assertTrue(result["report"]["losses"])
        self.assertFalse(output.exists())

    def test_missing_extra_and_duplicate_zip_members_are_refused(self):
        cases = [list(self.members.items())[1:],
                 [*self.members.items(), ("unused.wav", b"extra")],
                 [*self.members.items(), ("audio.wav", self.members["audio.wav"])]]
        for index, members in enumerate(cases):
            with self.subTest(index=index):
                self.refused(self.archive(members, str(index)+".zip"))

    def test_unsafe_and_nonregular_members_are_refused_without_extraction(self):
        for index, name in enumerate(("../escape", "/absolute.wav", "folder/audio.wav", "C:\\audio.wav", "folder/")):
            with self.subTest(name=name):
                self.refused(self.archive([*self.members.items(), (name, b"bad")], str(index)+".zip"))
        link = zipfile.ZipInfo("link.wav")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.refused(self.archive([*self.members.items(), (link, b"audio.wav")], "link.zip"))
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["0.zip","1.zip","2.zip","3.zip","4.zip","link.zip"])

    def test_truncated_and_corrupt_audio_archives_are_refused(self):
        truncated = self.directory / "truncated.zip"
        truncated.write_bytes(self.seed.read_bytes()[:-15])
        self.refused(truncated)
        damaged = self.archive(compression=zipfile.ZIP_STORED)
        data = bytearray(damaged.read_bytes())
        with zipfile.ZipFile(damaged) as archive:
            info = archive.getinfo("audio.wav")
            start = info.header_offset
        name_size, extra_size = struct.unpack_from("<HH", data, start+26)
        data[start+30+name_size+extra_size+44] ^= 0xff
        damaged.write_bytes(data)
        self.refused(damaged)

    def test_encrypted_unsupported_and_excessive_archive_members_are_refused(self):
        self.refused(self.archive(name="bzip2.zip", compression=zipfile.ZIP_BZIP2))
        encrypted = self.archive(name="encrypted.zip")
        data = bytearray(encrypted.read_bytes())
        central = struct.unpack_from("<I", data, len(data)-6)[0]
        flags = struct.unpack_from("<H", data, central+8)[0]
        struct.pack_into("<H", data, central+8, flags | 1)
        encrypted.write_bytes(data)
        self.refused(encrypted)
        from sample_export import MAX_CELLS
        entries = [(f"sample{i}.wav", b"") for i in range(2*MAX_CELLS+5)]
        self.refused(self.archive(entries, "excessive.zip"))

    def test_maximum_cell_count_fits_the_archive_member_bound(self):
        # Only exercise ZIP framing here; actual sample rendering/replay is
        # checked above and in sample_export.py. No 256-render test is needed.
        from sample_export import MAX_CELLS
        members = [("manifest.json", b"{}")] + [(f"sample{i}.wav", b"") for i in range(2*MAX_CELLS+3)]
        path = self.archive(members, "maximum.zip")
        with interchange.bundle_members(path) as (opened, names, sizes):
            self.assertEqual(len(names), 516)
            self.assertEqual(len(sizes), 516)
            with opened("manifest.json") as source:
                self.assertEqual(source.read(), b"{}")

    def test_manifest_identity_sizes_and_json_are_checked(self):
        self.assertFalse(interchange.verify(self.seed, expected_manifest_sha256="0"*64)["ok"])
        for index, encoded in enumerate((b'{"version":1,"version":1}', b'{"x":NaN}', b'not-json')):
            changed = {**self.members, "manifest.json":encoded}
            self.refused(self.archive(changed.items(), str(index)+".zip"))
        manifest = json.loads(self.members["manifest.json"])
        manifest["artifacts"]["audio.wav"]["bytes"] += 1
        changed = {**self.members, "manifest.json":interchange.json_bytes(manifest)}
        self.refused(self.archive(changed.items(), "size.zip"))
        with mock.patch.object(fm, "TEXT_PARENT_LIMIT", 8):
            self.refused(self.seed)

    def test_sfz_cannot_refer_to_a_missing_sample_even_with_rewritten_identity(self):
        changed = {**self.members, "instrument.sfz":self.members["instrument.sfz"].replace(b"audio.wav", b"other.wav")}
        manifest = json.loads(changed["manifest.json"])
        manifest["artifacts"]["instrument.sfz"] = dict(bytes=len(changed["instrument.sfz"]), sha256=fm.sha(changed["instrument.sfz"]))
        changed["manifest.json"] = interchange.json_bytes(manifest)
        self.refused(self.archive(changed.items()))

    def test_symlink_input_and_output_are_refused(self):
        source = self.directory / "linked.zip"
        source.symlink_to(self.seed)
        self.assertFalse(interchange.verify(source)["ok"])
        target = self.directory / "target.zip"
        target.write_bytes(b"untouched")
        destination = self.directory / "output.zip"
        destination.symlink_to(target)
        result = interchange.export(self.source, harness=ARGS.harness, profile="sfz", key=60,
                                    out=destination, container="zip")
        self.assertFalse(result["ok"])
        self.assertEqual(target.read_bytes(), b"untouched")

    def test_existing_destination_and_late_collision_are_never_overwritten(self):
        output = self.directory / "existing.zip"
        output.write_bytes(b"keep")
        result = interchange.export(self.source, harness=ARGS.harness, profile="sfz", key=60,
                                    out=output, container="zip")
        self.assertFalse(result["ok"])
        self.assertEqual(output.read_bytes(), b"keep")
        target = self.directory / "late.zip"
        original = interchange.os.link
        def collide(source, destination):
            Path(destination).write_bytes(b"other writer")
            return original(source, destination)
        with mock.patch.object(interchange.os, "link", side_effect=collide):
            result = interchange.export(self.source, harness=ARGS.harness, profile="sfz", key=60,
                                        out=target, container="zip")
        self.assertFalse(result["ok"])
        self.assertEqual(target.read_bytes(), b"other writer")
        self.assertFalse(list(self.directory.glob(".interchange-*")))

    def test_zip_dry_run_and_invalid_container_never_create_output(self):
        with mock.patch.object(interchange, "publish", side_effect=AssertionError("unexpected write")):
            result = interchange.export(self.source, harness=ARGS.harness, profile="sfz", key=60, container="zip")
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["dry_run"])
            self.assertFalse(result["published"])
        with mock.patch.object(fm, "Native", side_effect=AssertionError("unexpected native work")):
            result = interchange.export(self.source, profile="sfz", key=60, container="tar")
            self.assertFalse(result["ok"])
            self.assertEqual(result["exit"], 2)

    def test_cli_zip_export_and_verify_use_the_same_contract(self):
        output = self.directory / "cli.zip"
        harness = ["--harness", str(ARGS.harness)] if ARGS.harness else []
        with redirect_stdout(io.StringIO()) as reply:
            code = interchange.main([*harness, "sfz", str(self.source), "--key", "60", "--container", "zip", "--out", str(output)])
        self.assertEqual(code, 0, reply.getvalue())
        self.assertEqual(json.loads(reply.getvalue())["bundle"]["archive"], str(output.resolve()))
        with redirect_stdout(io.StringIO()) as reply:
            code = interchange.main(["verify", str(output)])
        self.assertEqual(code, 0, reply.getvalue())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *REMAINING])
