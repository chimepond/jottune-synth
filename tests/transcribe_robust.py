#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""WAV input robustness of the approximate transcriber (analyze.py transcribe, fm.py transcribe).

Inputs are small WAVs built with struct only. Malformed or oversized headers must give a clean
exit-2 refusal with a nonempty message before any sample is read, and no call may leave a lock
sidecar or a stale staging directory behind.
"""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(os.environ.get('JOTTUNE_TEST_HARNESS', ROOT / 'build/s12e10p1v1/harness')).resolve()
FS = 48000
try:
    import numpy  # noqa: F401  The transcriber itself needs NumPy.
    NUMPY = True
except ImportError:
    NUMPY = False


def chunk(name, payload, pad=True):
    data = name + struct.pack('<I', len(payload)) + payload
    return data + b'\0' if pad and len(payload) % 2 else data


def fmt(channels=1, rate=FS, bits=16):
    align = channels * bits // 8
    return chunk(b'fmt ', struct.pack('<HHIIHH', 1, channels, rate, rate * align, align, bits))


def riff(*chunks, size=None):
    body = b'WAVE' + b''.join(chunks)
    return b'RIFF' + struct.pack('<I', len(body) if size is None else size) + body


def tones(count=1, seconds=0.25, gap=0.12, hz=523.25):
    frames = []
    for index in range(count):
        frames += [0] * int(gap * FS)
        for n in range(int(seconds * FS)):
            t = n / FS
            frames.append(int(16000 * min(1.0, t / 0.005) * math.exp(-6 * t) * math.sin(2 * math.pi * hz * t)))
    frames += [0] * int(0.3 * FS)
    return struct.pack('<%dh' % len(frames), *frames)


def analyze(source, out, *options):
    return subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tools/music/analyze.py'), 'transcribe',
                           str(source), '--out', str(out), '--harness', str(HARNESS), *options],
                          capture_output=True, text=True, timeout=120)


def fm_transcribe(source, out, *options):
    result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tools/fm.py'), '--harness', str(HARNESS),
                             'transcribe', str(source), '--out', str(out), '--json',
                             *('--option=' + option for option in options)],
                            capture_output=True, text=True, timeout=120)
    return result.returncode, json.loads(result.stdout)


def session_module():
    spec = importlib.util.spec_from_file_location('session_under_test', ROOT / 'tools/session.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Api:
    @staticmethod
    def require(condition, message, code=2):
        if not condition:
            raise RuntimeError(message)


class DestinationLock(unittest.TestCase):
    def test_lock_excludes_and_leaves_no_sidecar(self):
        session = session_module()
        directory = Path(tempfile.mkdtemp(prefix='lock-'))
        self.addCleanup(shutil.rmtree, directory, True)
        destination, inside, peak, errors = directory / 'out', [0], [0], []

        def writer():
            try:
                for _ in range(20):
                    with session.destination_lock(Api, destination):
                        inside[0] += 1
                        peak[0] = max(peak[0], inside[0])
                        time.sleep(0.001)
                        inside[0] -= 1
            except Exception as error:  # Reported below; a thread cannot fail the test directly.
                errors.append(error)
        workers = [threading.Thread(target=writer) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(peak[0], 1, 'two holders of one destination lock')
        self.assertEqual(sorted(path.name for path in directory.iterdir()), [], 'lock sidecar left behind')


@unittest.skipUnless(NUMPY and HARNESS.is_file(), 'NumPy or native harness missing (%s); run make all' % HARNESS)
class TranscribeInput(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='transcribe-robust-'))
        self.addCleanup(shutil.rmtree, self.directory, True)

    def wav(self, name, data):
        path = self.directory / name
        path.write_bytes(data)
        return path

    def refused(self, source, needle, *options):
        result = analyze(source, self.directory / ('out-' + source.stem), *options)
        self.assertEqual(result.returncode, 2, result.stderr[-800:])
        self.assertNotIn('Traceback', result.stderr)
        message = result.stderr.strip().splitlines()[-1].split('error: ', 1)[-1]
        self.assertIn(needle, message)
        self.assertFalse((self.directory / ('out-' + source.stem)).exists())
        return message

    def test_truncated_header_has_a_message(self):
        good = riff(fmt(), chunk(b'data', tones()))
        self.refused(self.wav('trunc20.wav', good[:20]), 'truncated')

    def test_odd_chunk_without_pad_byte_is_a_clean_refusal(self):
        odd = b'INFO' + b'ICMT' + struct.pack('<I', 5) + b'odd!!'
        source = self.wav('odd.wav', riff(fmt(), chunk(b'LIST', odd, pad=False), chunk(b'data', tones())))
        self.assertIn('pad byte', self.refused(source, 'malformed WAV'))
        code, result = fm_transcribe(source, self.directory / 'fm-odd')
        self.assertEqual((code, result['exit']), (2, 2), result.get('message'))
        self.assertIn('pad byte', result['message'])
        padded = self.wav('padded.wav', riff(fmt(), chunk(b'LIST', odd), chunk(b'data', tones())))
        result = analyze(padded, self.directory / 'out-padded')
        self.assertEqual(result.returncode, 0, result.stderr[-800:])

    def test_declared_sizes_beyond_the_file_are_refused_before_reading(self):
        source = self.wav('huge.wav', riff(fmt(), b'data' + struct.pack('<I', 0xFFFFFFFF) + tones(),
                                           size=0xFFFFFFFF))
        self.assertIn('4294967295', self.refused(source, 'declares'))

    def test_duration_cap_is_checked_from_the_header(self):
        path = self.directory / 'long.wav'
        frames = 301 * FS
        with path.open('wb') as stream:  # Sparse: 301 s of declared and present silence.
            stream.write(riff(fmt(), b'data' + struct.pack('<I', 2 * frames))[:44])
            stream.truncate(44 + 2 * frames)
        self.assertIn('--max-seconds', self.refused(path, 'over the 300 s'))
        short = self.wav('short.wav', riff(fmt(), chunk(b'data', tones())))
        self.refused(short, 'over the 0.5 s', '--max-seconds', '0.5')
        self.refused(short, 'max_seconds must be', '--max-seconds', '1201')
        code, result = fm_transcribe(path, self.directory / 'fm-long')
        self.assertEqual((code, result['exit']), (2, 2), result.get('message'))
        self.assertIn('over the 300 s', result['message'])

    def test_unpooled_patch_limit_is_prechecked(self):
        source = self.wav('many.wav', riff(fmt(), chunk(b'data', tones(33, seconds=0.06, gap=0.1))))
        message = self.refused(source, '32-patch')
        self.assertIn('--pool-voices', message)
        self.assertIn('33', message)

    def test_valid_input_still_transcribes_and_fm_leaves_no_sidecars(self):
        source = self.wav('one.wav', riff(fmt(), chunk(b'data', tones())))
        destination = (self.directory / 'fm-one').resolve()
        tag = hashlib.sha256(str(destination).encode()).hexdigest()[:16]
        stale = self.directory / ('.transcribe-pending-%s-dead' % tag)
        (stale / 'partial').mkdir(parents=True)
        foreign = self.directory / '.transcribe-pending-legacy'
        foreign.mkdir()
        code, result = fm_transcribe(source, destination)
        self.assertEqual((code, result['exit'], result['complete']), (0, 0, True), result.get('message'))
        self.assertEqual(result['note_count'], 1)
        self.assertTrue((destination / 'canonical.tl').is_file())
        names = sorted(path.name for path in self.directory.iterdir() if path.name.startswith('.'))
        self.assertEqual(names, [foreign.name], 'stale pending dir kept, foreign dir removed, or lock sidecar left')
        code, result = fm_transcribe(self.wav('t.wav', riff(fmt(), chunk(b'data', tones()))[:20]),
                                     self.directory / 'fm-trunc')
        self.assertEqual(code, 2)
        self.assertIn('truncated', result['message'])
        self.assertEqual(sorted(path.name for path in self.directory.iterdir() if path.name.startswith('.')),
                         [foreign.name])


if __name__ == '__main__':
    unittest.main()
