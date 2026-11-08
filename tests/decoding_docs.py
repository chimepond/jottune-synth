#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""docs/DECODING.md matches the tl2wav/wav2tl CLI and stays out of the hosted web documents."""
import ast
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
DECODING = ROOT / 'docs/DECODING.md'


def hosted_documents():
    """HOSTED_DOCUMENTS from tools/package_web.py, read without importing it."""
    tree = ast.parse((ROOT / 'tools/package_web.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'HOSTED_DOCUMENTS' for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError('HOSTED_DOCUMENTS not found')


def options(command):
    """Long and short option strings from the subcommand's argparse help."""
    result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tools/fm.py'), command, '--help'],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    found = set(re.findall(r'(?<![\w-])(--?[a-z][a-z-]*)', result.stdout.split('options:', 1)[1]))
    return found - {'-h', '--help'}


class DecodingDocs(unittest.TestCase):
    def setUp(self):
        self.text = DECODING.read_text(encoding='utf-8')

    def test_every_cli_option_is_documented(self):
        for command in ('tl2wav', 'wav2tl'):
            missing = {name for name in options(command) if '`' + name not in self.text}
            self.assertFalse(missing, '%s options missing from docs/DECODING.md: %s' % (command, sorted(missing)))

    def test_both_directions_exit_codes_and_sidecar_are_documented(self):
        for heading in ('## tl2wav: exact render', '## wav2tl: approximate draft', '### Provenance sidecar',
                        '### Exit codes and limits', '## Approximate WAV transcription', '## MIDI import'):
            self.assertIn('\n' + heading + '\n', self.text)
        self.assertIn('harness --play FILES --wav', self.text)
        self.assertIn('not part of the web player or\nthe hosted service', self.text)
        for code in ('| 0 |', '| 2 |', '| 3 |', '| 4 |', '| 5 |'):
            self.assertIn(code, self.text)

    def test_decoder_guide_is_not_a_hosted_document(self):
        hosted = hosted_documents()
        self.assertNotIn('docs/DECODING.md', hosted)
        instrument = (ROOT / 'docs/INSTRUMENT.md').read_text(encoding='utf-8')
        self.assertIn('docs/INSTRUMENT.md', hosted)
        # The hosted instrument guide keeps the MIDI anchor as a pointer and no audio-import guide.
        self.assertNotIn('## Approximate WAV transcription', instrument)
        self.assertNotIn('wav2tl', instrument)
        self.assertIn('(DECODING.md#midi-import)', instrument)


if __name__ == '__main__':
    unittest.main()
