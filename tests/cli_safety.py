#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""CLI data-preservation and executable guide examples using the actual renderer."""
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(os.environ.get('JOTTUNE_TEST_HARNESS', ROOT / 'build/s12e10p1v1/harness')).resolve()


def run(*arguments):
    return subprocess.run([str(HARNESS), *map(str, arguments)], capture_output=True, text=True, timeout=30)


class CommandSafety(unittest.TestCase):
    def test_help_version_and_no_argument_are_read_only(self):
        for arguments in [(), ('--help',), ('-h',)]:
            result = run(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout)
            self.assertNotIn('RESULT', result.stdout)
        self.assertEqual(run('--version').stdout, 'JotTune Synth 0.1.0\n')

    def test_outputs_cannot_replace_input_or_each_other(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / 'song.tl'
            content = (ROOT / 'examples/bell.tl').read_bytes()
            source.write_bytes(content)
            symlink, hardlink = folder / 'link.tl', folder / 'hard.tl'
            symlink.symlink_to(source)
            os.link(source, hardlink)
            for mode in ['--play', '--play-once', '--keys-profile']:
                for destination in [source, symlink, hardlink]:
                    arguments = [mode] + (['30'] if mode == '--keys-profile' else [])
                    option = '--merged' if mode == '--keys-profile' else '--wav'
                    result = run(*arguments, source, option, destination)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('PLAY ', result.stdout)
                    self.assertEqual(source.read_bytes(), content)
            for option in ['--wav', '--merged']:
                result = run('--play', source, option, source)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(source.read_bytes(), content)
            output = folder / 'out'
            result = run('--play', source, '--wav', output, '--merged', output)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            result = run('--play', source, '--wav', folder / 'missing/out.wav')
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('PLAY ', result.stdout)

    def test_explicit_pcm_limit_precedes_render_and_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'out.wav'
            result = run('--play', ROOT / 'examples/bell.tl', '--pcm-limit', '1', '--wav', output)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('--pcm-limit:', result.stderr)
            self.assertNotIn('PLAY ', result.stdout)
            self.assertFalse(output.exists())

    def test_documented_complete_python_examples_execute(self):
        guide = (ROOT / 'docs/INSTRUMENT.md').read_text()
        scripts = re.findall(r"<<'PY'\n(.*?)^PY$", guide, flags=re.M | re.S)
        chosen = [body for body in scripts if 'attenuation_delta' in body or "c.Piece('drawing'" in body]
        self.assertEqual(len(chosen), 2)
        with tempfile.TemporaryDirectory() as temporary:
            environment = {**os.environ, 'REPO': str(ROOT), 'WORK': temporary, 'HARNESS': str(HARNESS)}
            for body in chosen:
                result = subprocess.run(['python3', '-B', '-c', body], env=environment,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('block_indep=yes', result.stdout)
            def rms(name):
                with wave.open(str(Path(temporary) / name), 'rb') as file:
                    samples = struct.unpack('<' + 'h' * file.getnframes(), file.readframes(file.getnframes()))
                    return math.sqrt(sum(value * value for value in samples) / len(samples))
            self.assertAlmostEqual(20 * math.log10(rms('base.wav') / rms('quieter.wav')), 6, delta=.02)


if __name__ == '__main__':
    unittest.main()
