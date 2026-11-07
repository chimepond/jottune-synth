#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Native Keys projection, gain policy, source protection and standalone replay.

Uses the production TL reader/writer and generator; fixture row assertions are
not an alternate application parser. No audio or generated records are retained.
"""
import argparse
import math
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASE = '''timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 500000000
streams a b
patch tone 0 65536 60 0 0 256 1 0 0 1
patch tone 1 65536 0 0 0 0 1 0 0 1
voice 0 tone
0 a 0 on 20000 32767
80000000 a 0 off
100000000 b 0 on 261626 32767
180000000 b 0 off
200000000 a 0 on 2000000 32767
250000000 b 0 pitch 1000000
280000000 a 0 off
'''


def fields(text, prefix):
    return dict(token.split('=', 1) for token in next(line for line in text.splitlines()
                if line.startswith(prefix + ' ')).split()[1:] if '=' in token)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--harness', type=Path, default=ROOT / 'build/s12e10p1v1/harness')
    args = ap.parse_args()
    harness = args.harness.resolve()
    groups = []
    with tempfile.TemporaryDirectory(prefix='keys-profile-') as temporary:
        directory = Path(temporary)
        serial = 0

        def run(*arguments, code=0):
            result = subprocess.run([str(harness), *map(str, arguments)], capture_output=True, text=True, timeout=60)
            assert result.returncode == code, (arguments, result.returncode, result.stdout, result.stderr)
            return result

        def draft(text):
            nonlocal serial
            serial += 1
            path = directory / f'source-{serial}.tl'
            path.write_text(text)
            return path

        def pin(text):
            path = draft(text)
            canonical = directory / f'canonical-{serial}.tl'
            run('--play', path, '--merged', canonical)
            return canonical

        def derive(path, width=60, mirror=False, bounded=False, code=0):
            nonlocal serial
            serial += 1
            destination = directory / f'keys-{serial}.tl'
            before = path.read_bytes()
            command = ['--keys-profile', width, '--merged', destination]
            if mirror:
                command += ['--keys-mirror']
            if bounded:
                command += ['--keys-carrier-bound']
            result = run(*command, '--', path, code=code)
            assert path.read_bytes() == before
            if code:
                assert not destination.exists(), 'Refused profiles cannot publish a derived song'
                return result, destination
            summary = fields(result.stdout, 'PROFILE')
            replay = fields(run('--play', destination).stdout, 'PLAY')
            assert replay['match'] == replay['block_indep'] == 'yes'
            assert replay['hash'] == summary['hash'] and replay['clipped'] == '0'
            assert replay['channels'] == '2'
            return result, destination

        original = pin(BASE)
        for width in (30, 45, 60, 75, 90):
            hashes = []
            for mirror in (False, True):
                result, derived = derive(original, width, mirror)
                summary = fields(result.stdout, 'PROFILE')
                assert summary['target_met'] == 'yes' and summary['manual_positions'] == '3'
                assert fields(result.stdout, 'PROFILE_SOURCE')['match'] == 'yes'
                rows = [line.split() for line in derived.read_text().splitlines()]
                positions = [tuple(map(int, row[4:])) for row in rows if len(row) > 3 and row[3] == 'pos']
                x, y = round(1000 * math.sin(math.radians(width))), round(1000 * math.cos(math.radians(width)))
                sign = -1 if mirror else 1
                assert positions == [(-sign*x, y, 0), (0, 1000, 0), (sign*x, y, 0)], positions
                for event in BASE.splitlines():
                    if event[:1].isdigit():
                        assert event in derived.read_text(), event
                hashes.append(summary['hash'])
            assert hashes[0] != hashes[1], 'Mirroring actual off-centre notes must change stereo PCM'
        groups.append('five half-widths, mirror, pitch preservation and isolated replay')

        routed = ROOT / 'tests/golden/48000/route4.tl'
        result, derived = derive(routed)
        delta = int(fields(result.stdout, 'PROFILE')['carrier_delta_db10'])
        source_patches = [line.split() for line in routed.read_text().splitlines() if line.startswith('patch ')]
        result_patches = [line.split() for line in derived.read_text().splitlines() if line.startswith('patch ')]
        for before, after in zip(source_patches, result_patches):
            assert int(after[4]) - int(before[4]) == (delta if int(before[2]) in (0, 2) else 0)
            assert before[:4] + before[5:] == after[:4] + after[5:]
        groups.append('one common shift across actual routed carriers only')

        generator = (ROOT / 'tests/golden/gen/gencoprime-48000.tl').read_text()
        generator = re.sub(r'^hash .*\n', '', generator, flags=re.M).replace('6000000000', '1000000000')
        generator += '100000000 golden lane 1 enable 0\n200000000 golden lane 1 enable 1\n300000000 golden gen 0 reset 42\n'
        generated_source = pin(generator)
        result, derived = derive(generated_source, bounded=True)
        before = run('--gen-events', generated_source).stdout
        after = run('--gen-events', derived).stdout
        assert before == after, 'The native generated schedule must remain byte-identical'
        assert int(fields(result.stdout, 'PROFILE')['generated_positions']) == sum('kind=on ' in line for line in before.splitlines()) > 0
        for line in generator.splitlines():
            if line.startswith(('gen ', 'gen_lane ', 'gen_clock ')) or ' golden lane ' in line or ' golden gen ' in line:
                assert line in derived.read_text(), line
        groups.append('generated onsets and mutable generator controls preserve their native schedule')

        sparse = generator.replace('gen 0 ', 'gen 5 ').replace('gen_lane 0 0 0 ', 'gen_lane 5 0 5 ')
        sparse = sparse.replace('golden gen 0 reset', 'golden gen 5 reset')
        sparse_source = pin(sparse)
        _, derived = derive(sparse_source, bounded=True)
        assert run('--gen-events', sparse_source).stdout == run('--gen-events', derived).stdout
        groups.append('sparse generator source/lane IDs survive projection without repacking')

        for name in ('field-bin1-48000.tl', 'field-amb3-44100.tl'):
            source = ROOT / 'tests/golden/fx' / name
            _, derived = derive(source, bounded=True)
            text = derived.read_text()
            assert 'space stereo\n' in text
            assert not re.search(r'^(order|pinna|yaw|panlaw) ', text, re.M)
            assert not re.search(r'^\d+ \S+ \S+ (move|yaw) ', text, re.M)
            for line in source.read_text().splitlines():
                if line.startswith(('chorus ', 'flanger ', 'pingpong ', 'reverb ', 'compressor ', 'limiter ', 'crush', 'gain ')) or ' send ' in line:
                    assert line in text
        groups.append('binaural/AmbiX become stereo while retaining effects and sends')

        for name in ('kspluck.tl', 'wtmorph.tl'):
            source = ROOT / 'tests/golden/48000' / name
            _, derived = derive(source, bounded=True)
            for line in source.read_text().splitlines():
                if line.startswith(('ksource ', 'wtbank ', 'wtdata ', 'wthash ', 'mod ')):
                    assert line in derived.read_text()
        groups.append('Karplus–Strong buffers and embedded wavetable assets survive re-rendering')

        quiet = pin(BASE.replace('65536 60 0 0 256', '65536 0 0 0 256').replace(' 32767', ' 1000'))
        refused, _ = derive(quiet, code=1)
        assert 'carrier shift exceeds' in refused.stderr
        result, derived = derive(quiet, bounded=True)
        summary = fields(result.stdout, 'PROFILE')
        assert summary['target_met'] == 'no' and summary['limited_by_carrier_bounds'] == '1'
        assert summary['legal_low_delta_db10'] == '0' and summary['last_applied_delta_db10'] == '0'
        assert float(summary['peak_dbfs']) < -2 and 'PROFILE_LIMIT patch=tone operator=0 level_db10=0' in result.stdout
        groups.append('strict refusal and explicit finite quiet carrier-bound result with limiting carrier')

        wrong = draft(re.sub(r'^hash .*$', 'hash 0000000000000000', original.read_text(), flags=re.M))
        result, _ = derive(wrong, code=3)
        assert 'match=NO' in result.stdout and 'PROFILE half_width' not in result.stdout
        unhashed = draft(re.sub(r'^hash .*\n', '', original.read_text(), flags=re.M))
        result, _ = derive(unhashed)
        assert 'hash_present=0 match=unchecked block_indep=yes' in result.stdout
        groups.append('saved source mismatch refused and unhashed source never misrepresented')

        conflict = ROOT / 'tests/golden/fx/decodable.tl'
        result, _ = derive(conflict, code=1)
        assert 'decodable' in result.stderr and 'pos' in result.stderr
        silent = draft(BASE.replace(' 32767', ' 0'))
        result, _ = derive(silent, bounded=True, code=1)
        assert 'silent' in result.stderr
        huge = draft(BASE.replace('length_ns 500000000', 'length_ns 60000000000000'))
        result, _ = derive(huge, code=1)
        assert 'stereo PCM dimensions' in result.stderr
        groups.append('decodable constraints, silence and impossible stereo allocation refuse before publication')

        before = original.read_bytes()
        for destination in (original, directory / 'hardlink.tl', directory / 'symlink.tl'):
            if destination.name == 'hardlink.tl':
                destination.hardlink_to(original)
            elif destination.name == 'symlink.tl':
                destination.symlink_to(original)
            result = run('--keys-profile', 60, '--merged', destination, original, code=1)
            assert 'aliases' in result.stderr
            assert original.read_bytes() == before
        occupied = draft('Keep this file.\n')
        run('--keys-profile', 60, '--merged', occupied, original, code=1)
        assert occupied.read_text() == 'Keep this file.\n'
        empty = draft('')
        run('--keys-profile', 60, '--merged', empty, original)
        assert fields(run('--play', empty).stdout, 'PLAY')['match'] == 'yes'
        run('--keys-profile', 55, '--merged', directory/'bad.tl', original, code=2)
        groups.append('input aliases and existing outputs protected; empty virtual output and CLI validation work')

        # Keep the retained music inventory independent of these derived results.
        assert fields(run('--play', ROOT/'music/preview-lantern.tl').stdout, 'PLAY')['hash'] == '1d8aaefce6b3b5b0'
        assert fields(run('--inspect', original).stdout, 'INSPECT')['keys_profile'] == '1'
        groups.append('retained song hash and native capability advertisement')
    for group in groups:
        print('PASS', group)
    print(f'{len(groups)} native Keys profile groups passed')


if __name__ == '__main__':
    main()
