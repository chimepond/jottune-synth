#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Batch export regression checks. Add --piano-roll to exercise the PNG renderer."""

import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WITH_PICTURES = "--piano-roll" in sys.argv
if WITH_PICTURES:
    sys.argv.remove("--piano-roll")


class BatchExport(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "input"
        self.out = self.root / "output"
        track = self.source / "my pieces" / "A ♫.tl"
        track.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "web/example.tl", track)
        self.audio = self.out / "my pieces" / "A ♫.wav"

    def export(self, *options, fmt="wav", success=True):
        result = subprocess.run(
            [sys.executable, ROOT / "tools/render_tunes.py", self.source, "--format", fmt,
             "-o", self.out, *options], cwd=self.root, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode == 0, success, result.stderr + result.stdout)
        return json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))

    def test_partial_batch_can_resume(self):
        bad = self.source / "second.tl"
        bad.write_bytes(b"invalid timeline\n")
        partial = self.export(success=False)
        self.assertEqual(partial["failed"], 1)
        self.assertEqual(set(partial["tracks"]), {"my pieces/A ♫.wav"})
        modified = self.audio.stat().st_mtime_ns
        shutil.copyfile(ROOT / "web/example.tl", bad)
        complete = self.export()
        self.assertEqual(complete["failed"], 0)
        self.assertEqual(len(complete["tracks"]), 2)
        self.assertEqual(self.audio.stat().st_mtime_ns, modified)

    def test_repeated_failure_preserves_the_last_verified_digest(self):
        original = self.export()["tracks"]
        track = self.source / "my pieces" / "A ♫.tl"
        track.write_bytes(track.read_bytes() + b"\n")
        for _ in range(2):
            report = self.export(success=False)
            self.assertEqual(report["tracks"], {})
            self.assertEqual(report["last_verified"], original)

    @unittest.skipUnless(WITH_PICTURES, "enable PNG checks with --piano-roll")
    def test_add_images_to_existing_audio(self):
        self.export()
        audio_time = self.audio.stat().st_mtime_ns
        picture = self.audio.with_suffix(".png")
        self.assertFalse(picture.exists())
        report = self.export("--piano-roll")
        png = picture.read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (1200, 1200))
        image = report["tracks"]["my pieces/A ♫.wav"]["piano_roll"]
        self.assertEqual(image["sha256"], hashlib.sha256(png).hexdigest())
        self.assertGreater(image["bars"], 0)
        self.assertGreater(image["playhead_seconds"], 0)
        self.assertEqual(self.audio.stat().st_mtime_ns, audio_time)
        image_time = picture.stat().st_mtime_ns
        self.export("--piano-roll")
        self.assertEqual(picture.stat().st_mtime_ns, image_time)
        picture.write_bytes(b"custom artwork")
        self.export("--piano-roll", success=False)
        self.assertEqual(picture.read_bytes(), b"custom artwork")

    @unittest.skipUnless(WITH_PICTURES and (shutil.which("lame") or shutil.which("ffmpeg")),
                         "enable PNG checks and install an MP3 encoder")
    def test_recover_retagged_mp3_and_artwork(self):
        song = {"id": "melody", "title": "Before", "collection": "Studies",
                "variants": [{"kind": "keys", "paths": ["my pieces/A ♫.tl"]}]}
        catalog = self.source / "library.json"
        catalog.write_text(json.dumps({"songs": [song]}), encoding="utf-8")
        key = "melody--keys.mp3"
        before = self.export("--piano-roll", fmt="mp3")["tracks"][key]
        song["title"] = "Après"
        catalog.write_text(json.dumps({"songs": [song]}), encoding="utf-8")
        audio_only = self.export(fmt="mp3")
        updated = audio_only["tracks"][key]
        self.assertNotEqual(before["output_sha256"], updated["output_sha256"])
        self.assertEqual(before["piano_roll"], updated["piano_roll"])
        new_image = self.export("--piano-roll", fmt="mp3")["tracks"][key]["piano_roll"]
        self.assertNotEqual(before["piano_roll"]["sha256"], new_image["sha256"])
        # Simulate stopping between publishing the new PNG and saving its receipt.
        (self.out / "manifest.json").write_text(json.dumps(audio_only), encoding="utf-8")
        recovered = self.export("--piano-roll", fmt="mp3")
        self.assertEqual(recovered["tracks"][key]["piano_roll"], new_image)


if __name__ == "__main__":
    unittest.main()
