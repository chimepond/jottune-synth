#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""fm.py wav2tl: draft banner, JSON fields, provenance sidecar, --strict, fallback, no overwrite."""
import hashlib
import importlib.util
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
BANNER = 'wav2tl: DRAFT approximate mono transcription'
TWO_NOTES = '''timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams golden
patch bell 0 65536 0 0 0 256 2 2500 900 300
patch bell 1 229376 0 8192 0 0 1 1200 600 400
voice 0 bell
0 golden 0 on 523250 32767
600000000 golden 0 off
900000000 golden 0 on 659260 32767
1500000000 golden 0 off
'''
PROVENANCE = {'version', 'derived', 'draft', 'mode', 'source_sha256', 'source_bytes', 'source_name', 'tool',
              'tool_version', 'engine', 'native_sha256', 'implementation', 'options', 'snr_db', 'limitations',
              'warnings'}
try:
    import numpy  # noqa: F401  The transcriber itself needs NumPy.
    NUMPY = True
except ImportError:
    NUMPY = False


def fm(*arguments):
    return subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tools/fm.py'), '--harness', str(HARNESS),
                           *map(str, arguments)], capture_output=True, text=True, timeout=300)


@unittest.skipUnless(HARNESS.is_file() and NUMPY, 'needs NumPy and the native harness (%s); run make all' % HARNESS)
class Wav2Tl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = Path(tempfile.mkdtemp(prefix='wav2tl-cli-'))
        source = cls.directory / 'two.tl'
        source.write_text(TWO_NOTES, encoding='utf-8')
        cls.wav = cls.directory / 'two.wav'
        played = fm('tl2wav', source, '-o', cls.wav)
        assert played.returncode == 0, played.stderr
        cls.digest = hashlib.sha256(cls.wav.read_bytes()).hexdigest()
        cls.piece = cls.directory / 'draft'
        cls.run_json = fm('wav2tl', cls.wav, '-o', cls.piece, '--json')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, True)

    def test_round_trip_piece_renders_back_with_tl2wav(self):
        self.assertEqual(self.run_json.returncode, 0, self.run_json.stderr)
        for name in ('session.tl', 'canonical.tl', 'report.json', 'provenance.json'):
            self.assertTrue((self.piece / name).is_file(), name)
        self.assertTrue((self.piece / 'session.tl').read_text().startswith('# Approximate mono transcription'))
        back = fm('tl2wav', self.piece, '-o', self.directory / 'back.wav', '--json')
        self.assertEqual(back.returncode, 0, back.stderr)
        reply = json.loads(back.stdout)
        self.assertGreater(reply['peak'], 0)
        self.assertGreater(reply['events'], 0)

    def test_json_reply_is_marked_draft(self):
        self.assertTrue(self.run_json.stderr.startswith(BANNER), self.run_json.stderr)
        self.assertIn('polyphony and chords unsupported', self.run_json.stderr.splitlines()[0])
        reply = json.loads(self.run_json.stdout)
        self.assertIs(reply['draft'], True)
        self.assertEqual(reply['operation'], 'wav2tl')
        self.assertIsInstance(reply['snr_db'], float)
        self.assertIn('SNR %.2f dB' % reply['snr_db'], self.run_json.stderr)
        self.assertIsInstance(reply['warnings'], list)
        self.assertTrue(any(item.startswith('mono only') for item in reply['limitations']))
        self.assertEqual(reply['recipe_candidates'], 8)
        self.assertIsNone(reply['fallback'])
        self.assertTrue(reply['published'])
        self.assertEqual(reply['provenance_file'], 'provenance.json')

    def test_provenance_sidecar_fields_and_source_hash(self):
        text = (self.piece / 'provenance.json').read_text(encoding='utf-8')
        record = json.loads(text)
        self.assertEqual(set(record), PROVENANCE)
        self.assertEqual(record['derived'], 'audio-transcription')
        self.assertEqual(record['source_sha256'], self.digest)
        self.assertEqual(record['source_bytes'], self.wav.stat().st_size)
        self.assertEqual(record['source_name'], 'two.wav')
        self.assertEqual(record['tool'], 'fm.py wav2tl')
        version = subprocess.run([str(HARNESS), '--version'], capture_output=True, text=True).stdout.splitlines()[0]
        self.assertEqual(record['tool_version'], version.strip())
        self.assertTrue(TWO_NOTES.splitlines()[1].endswith(record['engine']))
        self.assertEqual(record['options']['recipe_candidates'], 8)
        self.assertEqual(record['native_sha256'], hashlib.sha256(HARNESS.read_bytes()).hexdigest())
        # Basename and digests only: no directories of this host.
        self.assertNotIn(str(self.directory), text)
        self.assertNotIn(str(ROOT), text)
        self.assertNotIn(str(Path.home()), text)

    def test_refuses_to_overwrite(self):
        before = sorted(path.name for path in self.piece.iterdir())
        again = fm('wav2tl', self.wav, '-o', self.piece)
        self.assertEqual(again.returncode, 2)
        self.assertTrue(again.stderr.startswith(BANNER))
        self.assertIn('refusing to overwrite', again.stderr)
        self.assertEqual(sorted(path.name for path in self.piece.iterdir()), before)
        existing = self.directory / 'existing.txt'
        existing.write_text('keep\n')
        self.assertEqual(fm('wav2tl', self.wav, '-o', existing).returncode, 2)
        self.assertEqual(existing.read_text(), 'keep\n')

    def test_strict_fails_below_threshold_and_publishes_nothing(self):
        out = self.directory / 'strict-fail'
        refused = fm('wav2tl', self.wav, '-o', out, '--strict', '--min-snr-db', '40', '--json')
        self.assertEqual(refused.returncode, 4, refused.stderr)
        reply = json.loads(refused.stdout)
        self.assertFalse(reply['ok'])
        self.assertIn('strict draft check failed', reply['message'])
        self.assertTrue(any('below 40 dB' in item for item in reply['warnings']))
        self.assertFalse(os.path.lexists(out))
        self.assertEqual([path.name for path in self.directory.iterdir() if 'pending' in path.name], [])
        # Without --strict the same threshold is only a warning.
        warned = fm('wav2tl', self.wav, '-o', self.directory / 'warned', '--min-snr-db', '40')
        self.assertEqual(warned.returncode, 0, warned.stderr)
        self.assertIn('warning: whole-signal SNR', warned.stderr)
        passed = fm('wav2tl', self.wav, '-o', self.directory / 'strict-pass', '--strict', '--min-snr-db', '-100')
        self.assertEqual(passed.returncode, 0, passed.stderr)

    def test_explicit_shortlist_is_kept_and_plain_transcribe_is_unchanged(self):
        out = self.directory / 'one'
        reply = json.loads(fm('wav2tl', self.wav, '-o', out, '--option=--recipe-candidates', '--option=1',
                              '--json').stdout)
        self.assertTrue(reply['ok'], reply.get('message'))
        self.assertIsNone(reply['recipe_candidates'])
        self.assertEqual(json.loads((out / 'provenance.json').read_text())['options']['recipe_candidates'], 1)
        plain = fm('transcribe', self.wav, '--out', self.directory / 'plain', '--json')
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertNotIn('draft', json.loads(plain.stdout))
        self.assertFalse((self.directory / 'plain/provenance.json').exists())


class Fallback(unittest.TestCase):
    """The 8-recipe shortlist falls back to the analyzer default only after a decoder refusal."""

    def setUp(self):
        sys.modules.pop('fm', None)
        spec = importlib.util.spec_from_file_location('fm', ROOT / 'tools/fm.py')
        self.fm = sys.modules['fm'] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fm)
        spec = importlib.util.spec_from_file_location('decode', ROOT / 'tools/decode.py')
        self.decode = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.decode)
        self.addCleanup(sys.modules.pop, 'fm', None)
        self.directory = Path(tempfile.mkdtemp(prefix='wav2tl-fallback-'))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.wav = self.directory / 'in.wav'
        self.wav.write_bytes(b'RIFF')
        self.calls = []

    def fake(self, *replies):
        replies = list(replies)

        def transcribe(source, out, **kwargs):
            self.calls.append(kwargs)
            return replies.pop(0)
        self.fm.transcribe = transcribe

    def test_refused_wide_run_retries_with_default_shortlist(self):
        refused = dict(ok=False, exit=2, stop_reason='error', published=False, message='no positive template level fit',
                       costs=dict(total_attempts=9))
        done = dict(ok=True, exit=0, published=True, score=dict(snr_db=1.5), limitations=['octave ambiguity'],
                    draft=dict(warnings=[], snr_db=1.5, provenance={}, file='provenance.json'))
        self.fake(refused, done)
        reply = self.decode.wav2tl(self.wav, self.directory / 'out', options=['--split-onsets'], native_budget=20)
        self.assertEqual([call['options'] for call in self.calls],
                         [['--recipe-candidates', '8', '--split-onsets'], ['--split-onsets']])
        self.assertEqual([call['native_budget'] for call in self.calls], [20, 11])
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['fallback']['reason'], 'no positive template level fit')
        self.assertIsNone(reply['recipe_candidates'])
        self.assertTrue(any('8-recipe shortlist was refused' in item for item in reply['warnings']))

    def test_no_fallback_after_budget_stop_header_refusal_or_explicit_recipes(self):
        budget = dict(ok=False, exit=0, stop_reason='native_budget', published=False, costs=dict(total_attempts=5))
        header = dict(ok=False, exit=2, stop_reason='error', published=False, costs=dict(total_attempts=0))
        self.fake(budget, header)
        self.decode.wav2tl(self.wav, self.directory / 'a', native_budget=5)
        self.decode.wav2tl(self.wav, self.directory / 'b')
        self.assertEqual(len(self.calls), 2)
        refused = dict(ok=False, exit=2, stop_reason='error', published=False, costs=dict(total_attempts=9))
        self.calls.clear()
        self.fake(refused)
        reply = self.decode.wav2tl(self.wav, self.directory / 'c', options=['--recipes', 'bell'])
        self.assertEqual([call['options'] for call in self.calls], [['--recipes', 'bell']])
        self.assertIsNone(reply['fallback'])


if __name__ == '__main__':
    unittest.main()
