#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Checks on the tracked files of this checkout: shebang scripts check out with
LF endings, TL files are never converted, and no source cites a design note
that is not in the repository. Skipped outside a git checkout of this repository."""
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Its text goes into build/sp_binaural_tables.h, whose sha256 tests/native.sh pins.
PINNED = {'src/space/gen_binaural_int.c'}


def git(*args, check=True):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, check=check).stdout


class RepoHygiene(unittest.TestCase):
    def setUp(self):
        try:
            top = git('rev-parse', '--show-toplevel').strip()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest('not a git checkout')
        if pathlib.Path(top).resolve() != ROOT:
            self.skipTest('not the root of this repository')
        self.files = [name for name in git('ls-files', '-z').split('\0') if name]

    def attributes(self, names, *keys):
        found = {}
        for line in git('check-attr', *keys, '--', *names).splitlines():
            name, key, value = line.rsplit(': ', 2)
            found[(name, key)] = value
        return found

    def test_shebang_scripts_check_out_with_lf_endings(self):
        scripts = [name for name in self.files
                   if (ROOT / name).is_file() and (ROOT / name).read_bytes()[:2] == b'#!']
        self.assertGreaterEqual(len(scripts), 4)
        found = self.attributes(scripts, 'text', 'eol')
        for name in scripts:
            self.assertEqual(found[(name, 'eol')], 'lf', name)
            self.assertNotEqual(found[(name, 'text')], 'unset', name)

    def test_tl_files_keep_their_exact_bytes(self):
        records = [name for name in self.files if name.endswith('.tl')][:50]
        self.assertTrue(records)
        found = self.attributes(records, 'text')
        for name in records:
            self.assertEqual(found[(name, 'text')], 'unset', name)

    def test_no_source_cites_a_missing_design_note(self):
        hits = [line for line in git('grep', '-n', '-I', 'SPEC\\.md', check=False).splitlines()
                if line.split(':', 1)[0] not in PINNED]
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
