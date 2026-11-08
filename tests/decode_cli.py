#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""fm.py tl2wav: exact WAV bytes, piece directories, silent refusal, no overwrite."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(os.environ.get('JOTTUNE_TEST_HARNESS', ROOT / 'build/s12e10p1v1/harness')).resolve()
SINE = ROOT / 'tests/golden/48000/sine.tl'
GENERATOR = ROOT / 'tests/golden/gen/gencoprime-48000.tl'
HINT = 'a piece is a directory'
KEYS = {'version', 'ok', 'exit', 'out', 'sha256', 'frames', 'sample_rate', 'channels', 'events', 'peak',
        'saved_hash', 'sources', 'warnings'}


def tl2wav(*arguments):
    return subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tools/fm.py'), '--harness', str(HARNESS),
                           'tl2wav', *map(str, arguments)], capture_output=True, text=True, timeout=120)


def harness_wav(files, out):
    result = subprocess.run([str(HARNESS), '--play', *map(str, files), '--wav', str(out)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return Path(out).read_bytes()


@unittest.skipUnless(HARNESS.is_file(), 'native harness missing (%s); run make all first' % HARNESS)
class Tl2Wav(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='decode-cli-'))
        self.addCleanup(shutil.rmtree, self.directory, True)
        lines = SINE.read_text().splitlines()
        self.piece = self.directory / 'piece'
        self.piece.mkdir()
        header = [line.replace('streams golden', 'streams notes') for line in lines[:8] if not line.startswith('hash ')]
        (self.piece / 'session.tl').write_text('\n'.join(header) + '\n')
        (self.piece / 'notes.tl').write_text('\n'.join(line.replace(' golden ', ' notes ') for line in lines[-2:]) + '\n')
        self.parts = [self.piece / 'session.tl', self.piece / 'notes.tl']

    def test_tl_file_matches_harness_bytes_and_saved_hash(self):
        expected = harness_wav([SINE], self.directory / 'harness.wav')
        out = self.directory / 'sine.wav'
        result = tl2wav(SINE, '-o', out, '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(out.read_bytes(), expected)
        reply = json.loads(result.stdout)
        self.assertEqual(set(reply), KEYS)
        self.assertEqual((reply['ok'], reply['exit'], reply['out'], reply['saved_hash'], reply['warnings']),
                         (True, 0, str(out.resolve()), 'match', []))
        self.assertEqual((reply['frames'], reply['sample_rate'], reply['channels'], reply['events']), (96000, 48000, 1, 2))
        self.assertGreater(reply['peak'], 0)
        self.assertEqual(reply['sha256'], hashlib.sha256(expected).hexdigest())

    def test_piece_directory_and_part_files_render_the_full_piece(self):
        expected = harness_wav(self.parts, self.directory / 'harness.wav')
        for name, inputs in (('dir.wav', [self.piece]), ('parts.wav', self.parts)):
            out = self.directory / name
            result = tl2wav(*inputs, '-o', out, '--json')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(out.read_bytes(), expected, name)
            reply = json.loads(result.stdout)
            self.assertEqual(reply['events'], 2)
            self.assertGreater(reply['peak'], 0)
            self.assertEqual(reply['sources'], [str(path.resolve()) for path in self.parts])
            self.assertEqual(reply['warnings'], [])

    def test_header_alone_is_refused_unless_allow_silent(self):
        out = self.directory / 'silent.wav'
        result = tl2wav(self.parts[0], '-o', out)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(out.exists())
        self.assertIn('render is silent', result.stderr)
        self.assertIn('pass the piece directory or all of its part files', result.stderr)
        self.assertIn('warning: harness:', result.stderr)
        result = tl2wav(self.parts[0], '-o', out, '--json')
        reply = json.loads(result.stdout)
        self.assertEqual((result.returncode, reply['ok'], reply['out'], reply['peak']), (2, False, None, 0))
        self.assertTrue(any(HINT in line for line in reply['warnings']))
        result = tl2wav(self.parts[0], '-o', out, '--allow-silent', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(out.read_bytes(), harness_wav(self.parts[:1], self.directory / 'harness.wav'))
        reply = json.loads(result.stdout)
        self.assertEqual((reply['ok'], reply['events'], reply['peak']), (True, 0, 0))
        self.assertTrue(any(HINT in line for line in reply['warnings']))

    def test_generator_only_song_without_events_is_not_silent(self):
        out = self.directory / 'generator.wav'
        result = tl2wav(GENERATOR, '-o', out, '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reply = json.loads(result.stdout)
        self.assertEqual((reply['events'], reply['saved_hash']), (0, 'match'))
        self.assertGreater(reply['peak'], 0)

    def test_existing_output_is_refused_and_untouched(self):
        out = self.directory / 'kept.wav'
        out.write_bytes(b'keep me')
        for extra in ((), ('--json',)):
            result = tl2wav(SINE, '-o', out, *extra)
            self.assertEqual(result.returncode, 2)
            self.assertIn('refusing to overwrite', result.stdout + result.stderr)
            self.assertEqual(out.read_bytes(), b'keep me')
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ['kept.wav', 'piece'])

    def test_saved_hash_mismatch_is_refused(self):
        changed = self.directory / 'changed.tl'
        changed.write_text(SINE.read_text().replace('hash e76dc5bad8783650', 'hash 0000000000000001'))
        out = self.directory / 'changed.wav'
        result = tl2wav(changed, '-o', out, '--json')
        reply = json.loads(result.stdout)
        self.assertEqual((result.returncode, reply['ok'], reply['saved_hash']), (3, False, 'mismatch'))
        self.assertFalse(out.exists())

    def test_bad_inputs_exit_2(self):
        out = self.directory / 'x.wav'
        for arguments in ((self.directory / 'missing.tl', '-o', out), (self.piece, SINE, '-o', out),
                          (SINE, '-o', self.directory / 'no/such/dir.wav')):
            result = tl2wav(*arguments, '--json')
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
