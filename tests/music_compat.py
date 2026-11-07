#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic; SPDX-License-Identifier: Apache-2.0
"""Verify complete example records and an explicitly selected song collection."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def check(harness, record):
    with tempfile.TemporaryDirectory(prefix='jottune-replay-') as folder:
        output = Path(folder) / 'canonical.tl'
        first = subprocess.run([str(harness), '--play', str(record), '--merged', str(output)],
                               capture_output=True, text=True)
        assert first.returncode == 0, (record.name, first.stderr)
        assert 'match=yes' in first.stdout and 'block_indep=yes' in first.stdout, record.name
        assert output.read_bytes() == record.read_bytes(), record.name
        second = subprocess.run([str(harness), '--play', str(output)], capture_output=True, text=True)
        assert second.returncode == 0 and second.stdout == first.stdout, record.name
    return record.name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    assert 1 <= args.jobs <= 32
    records = sorted((ROOT / 'examples').glob('*.tl'))
    collection = os.environ.get('JOTTUNE_TUNES_ROOT')
    if collection:
        directory = Path(collection).resolve() / 'records'
        assert directory.is_dir(), 'Selected collection is unavailable.'
        records += sorted(directory.glob('*.tl'))
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        count = sum(1 for _ in executor.map(lambda record: check(args.harness.resolve(),record),records))
    print('MUSIC_COMPAT %d complete records verified; external collection %s' %
          (count, 'selected' if collection else 'not selected'))


if __name__ == '__main__':
    main()
