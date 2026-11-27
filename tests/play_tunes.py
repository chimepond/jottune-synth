#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Exercise direct listening with native WAV renders and a silent ffplay substitute."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PlaylistPlayback(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "music"
        self.source.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.log = self.root / "played.jsonl"
        self.log.touch()
        player = self.root / "ffplay"
        player.write_text("#!" + sys.executable + "\n" + r'''
import io, json, os, stat, sys, wave
from pathlib import Path

if not stat.S_ISFIFO(os.fstat(sys.stdin.fileno()).st_mode):
    sys.exit("expected a WAV pipe")
if any(Path(os.environ["TMPDIR"]).rglob("*.wav")):
    sys.exit("unexpected temporary WAV")
data = sys.stdin.buffer.read()
if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
    sys.exit("ffplay received no RIFF/WAVE")
with wave.open(io.BytesIO(data), "rb") as wav:
    frames = wav.getnframes()
    pcm = wav.readframes(frames)
    if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 48000):
        sys.exit("wrong WAV shape")
    if len(pcm) != frames * 2 or not any(pcm):
        sys.exit("silent or truncated WAV")
with open(os.environ["PLAYBACK_TEST_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(frames) + "\n")
if os.environ.get("PLAYBACK_TEST_FAIL"):
    print("intentional player failure", file=sys.stderr)
    sys.exit(int(os.environ["PLAYBACK_TEST_FAIL"]))
''', encoding="utf-8")
        player.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"],
                        TMPDIR=str(self.scratch), PLAYBACK_TEST_LOG=str(self.log))

        # Durations identify the WAV heard by the player. The file order differs
        # from both the catalog's song order and the first song's variant order.
        lines = (ROOT / "web/example.tl").read_text(encoding="utf-8").splitlines()
        for filename, ms in [("a.tl", 10), ("w.tl", 20), ("z.tl", 30),
                             ("b.tl", 40), ("a loose piece.tl", 50)]:
            text = "\n".join(line for line in lines if not line.startswith("hash "))
            text = text.replace("length_ns 2000000000", f"length_ns {ms * 1000000}")
            text = text.replace("1000000000 golden 0 off", "5000000 golden 0 off")
            (self.source / filename).write_text(text + "\n", encoding="utf-8")
        songs = [
            {"id": "z-song", "title": "First", "collection": "Studies",
             "variants": [{"kind": "standard", "paths": ["z.tl"]},
                          {"kind": "keys", "paths": ["w.tl"]}]},
            {"id": "a-song", "title": "Second", "collection": "Studies",
             "variants": [{"kind": "standard", "paths": ["a.tl"]}]},
        ]
        (self.source / "library.json").write_text(json.dumps({"songs": songs}), encoding="utf-8")
        self.ordered = [ms * 48 for ms in (30, 20, 10, 50, 40)]
        self.before = self.files()

    def files(self):
        return {path.relative_to(self.root) for path in self.root.rglob("*")}

    def listen(self, *options):
        command = [sys.executable, str(ROOT / "tools/render_tunes.py"), str(self.source),
                   "--harness", str(ROOT / "build/s12e10p1v1/harness"), *options]
        return subprocess.run(command, cwd=self.root, env=self.env, text=True,
                              capture_output=True, timeout=30)

    def played(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_catalog_order_then_sorted_loose_files(self):
        result = self.listen("--play")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.played(), self.ordered)
        self.assertEqual(self.files(), self.before)

    def test_shuffle_plays_every_record_once(self):
        result = self.listen("--play", "--shuffle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(self.played(), self.ordered)
        self.assertEqual(self.files(), self.before)

    def test_incompatible_options_do_not_play_or_export(self):
        for options in [("--shuffle",), ("--play", "-o", "out"), ("--play", "--format", "wav"),
                        ("--play", "--format", "mp3"), ("--play", "--piano-roll"), ("--play", "-j", "2")]:
            with self.subTest(options=options):
                result = self.listen(*options)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(self.played(), [])
                self.assertEqual(self.files(), self.before)

    def test_absent_player_is_reported(self):
        self.env["PATH"] = str(self.scratch)
        result = self.listen("--play")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ffplay", result.stderr.lower())
        self.assertEqual(self.played(), [])
        self.assertEqual(self.files(), self.before)

    def test_player_failure_returns_nonzero_and_cleans_up(self):
        # Some player builds report an audio-device error with a zero exit code.
        for code in ("1", "0"):
            with self.subTest(code=code):
                self.log.write_text("", encoding="utf-8")
                self.env["PLAYBACK_TEST_FAIL"] = code
                result = self.listen("--play")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("intentional player failure", result.stderr)
                self.assertTrue(self.played())
                self.assertEqual(self.files(), self.before)

    def test_failed_render_does_not_send_bad_audio_to_player(self):
        (self.source / "z.tl").write_text("invalid timeline\n", encoding="utf-8")
        result = self.listen("--play")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("z.tl", result.stderr)
        self.assertNotIn(self.ordered[0], self.played())
        self.assertEqual(self.files(), self.before)


if __name__ == "__main__":
    unittest.main()
