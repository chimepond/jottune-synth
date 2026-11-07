#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Check listener PCM against full verification, including errors and pipes."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import wave

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ('48000/sine.tl', 'fx/chain.tl', 'space/pan.tl',
           'space/amb3-yaw.tl', 'space/binaural/binaural1.tl',
           'gen/gencoprime-48000.tl')


def row(text, kind):
    line = next(line for line in text.splitlines() if line.startswith(kind + ' '))
    return dict(item.split('=', 1) for item in line.split()[1:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness', type=Path, default=ROOT / 'build/s12e10p1v1/harness')
    args = parser.parse_args()
    harness = str(args.harness.resolve())
    checks = 0
    with tempfile.TemporaryDirectory(prefix='fm-listener-check-') as folder:
        temp = Path(folder)

        def run(*argv):
            return subprocess.run([harness, *map(str, argv)], cwd=temp,
                                  capture_output=True, text=True)

        def compare(record, extra=(), code=0):
            nonlocal checks
            verify = run('--play', record, '--metrics', '--wav', 'verify.wav',
                         '--merged', 'verify.tl', *extra)
            listen = run('--play-once', record, '--metrics', '--wav', 'listen.wav',
                         '--merged', 'listen.tl', *extra)
            assert verify.returncode == listen.returncode == code, (verify, listen)
            assert listen.stderr == verify.stderr, (verify.stderr, listen.stderr)
            assert listen.stdout.replace('block_indep=unchecked', 'block_indep=yes') == verify.stdout
            assert (temp / 'listen.wav').read_bytes() == (temp / 'verify.wav').read_bytes()
            assert (temp / 'listen.tl').read_bytes() == (temp / 'verify.tl').read_bytes()
            assert row(listen.stdout, 'PLAY')['block_indep'] == 'unchecked'
            checks += 1
            return listen

        descriptor = run('--engine')
        assert descriptor.returncode == 0 and descriptor.stdout.startswith('ENGINE a1 ')
        for name in RECORDS:
            record = ROOT / 'tests/golden' / name
            inspect = run('--inspect', record)
            assert inspect.returncode == 0, inspect.stderr
            info = row(inspect.stdout, 'INSPECT')
            assert info['hash_present'] == '1'
            assert int(info['render_bytes_lower_bound']) - int(info['once_render_bytes_lower_bound']) == int(info['pcm_bytes'])
            assert inspect.stdout.splitlines()[0] == descriptor.stdout.strip()
            assert row(compare(record).stdout, 'PLAY')['match'] == 'yes'

        source = ROOT / 'tests/golden/48000/sine.tl'
        expected = row(run('--inspect', source).stdout, 'INSPECT')['expected_hash']
        original = source.read_text()
        wrong = temp / 'wrong.tl'
        wrong.write_text(original.replace('hash ' + expected, 'hash 0000000000000000'))
        assert row(compare(wrong, code=3).stdout, 'PLAY')['match'] == 'NO'
        draft = temp / 'draft.tl'
        draft.write_text(original.replace('hash ' + expected + '\n', ''))
        info = row(run('--inspect', draft).stdout, 'INSPECT')
        assert info['hash_present'] == '0' and info['expected_hash'] == 'none'
        assert row(compare(draft).stdout, 'PLAY')['match'] == '-'
        assert run('--play-once', 'listen.tl').returncode == 0

        field = ROOT / 'tests/golden/space/binaural/binaural1.tl'
        info = row(run('--inspect', field).stdout, 'INSPECT')
        with wave.open(str(temp / 'stems.wav'), 'wb') as wav:
            wav.setparams((int(info['voices']), 2, int(info['fs']), 0, 'NONE', 'not compressed'))
            wav.writeframes(bytes(int(info['frames']) * int(info['voices']) * 2))
        compare(field, ('--stems', 'stems.wav'), code=3)

        for option in (('--ref',), ('--chunk', '1'), ('--chunk', 'random'),
                       ('--stems-out', 'dry.wav', '--pcm-limit', '1048576'),
                       ('--stems-fd', '3', '--pcm-limit', '1048576')):
            result = run('--play-once', source, *option)
            assert result.returncode == 2 and '--play-once refuses' in result.stderr
            assert not result.stdout
            checks += 1
        for argv in (('--play-once',), ('--play-once', '--play', source),
                     ('--inspect', source, '--metrics'), ('--engine', '--merged', 'unused.tl')):
            assert run(*argv).returncode == 2
            checks += 1

        compare(source)
        expected_wav = (temp / 'listen.wav').read_bytes()
        read_fd, write_fd = os.pipe()
        captured = []

        def drain():
            with os.fdopen(read_fd, 'rb') as stream:
                captured.append(stream.read())

        reader = threading.Thread(target=drain)
        reader.start()
        try:
            child = subprocess.Popen([harness, '--play-once', str(source), '--wav-fd',
                                      str(write_fd), '--pcm-limit', '1048576'],
                                     pass_fds=(write_fd,), cwd=temp,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        finally:
            os.close(write_fd)
        stdout, stderr = child.communicate(timeout=30)
        reader.join(timeout=5)
        assert not reader.is_alive() and child.returncode == 0 and not stderr
        assert row(stdout, 'PLAY')['block_indep'] == 'unchecked'
        assert captured == [expected_wav]
        checks += 1
    print('PLAY_ONCE ok', checks, 'checks: PCM, canonical bytes, inspection, refusals and pipe output')


if __name__ == '__main__':
    main()
