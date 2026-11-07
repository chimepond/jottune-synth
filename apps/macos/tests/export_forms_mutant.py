#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Require the actual complete-bank comparator bypass to fail the custom-bank control."""
from pathlib import Path
import subprocess
import sys
import tempfile

helper=Path(sys.argv[1]);text=helper.read_text(encoding='utf-8')
anchor='if original == expected:'
if text.count(anchor)!=1:raise SystemExit('bank comparator mutation anchor differs')
with tempfile.TemporaryDirectory(prefix='export-bank-mutant-') as temp:
    mutant=Path(temp)/'make_recipes.py';mutant.write_text(text.replace(anchor,'if True:'),encoding='utf-8')
    r=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('export_forms.py')),str(mutant),*sys.argv[2:5],
                      '--negative-only'],text=True,capture_output=True)
if r.returncode==0 or 'EXPORT_FORMS FAILED: same-name custom bank pending rows' not in r.stdout+r.stderr:
    raise SystemExit('complete-bank comparator bypass not detected: '+r.stdout+r.stderr)
print('EXPORT_FORM_MUTANTS 1/1 detected: complete native bank comparator bypass')
