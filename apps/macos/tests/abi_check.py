#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Syntax-check the bridge ABI guards and refuse a copied FMS_NAME=31 header.
Usage: abi_check.py BRIDGE_DIR APPROVED_LIB_DIR WORK_DIR
No library render/build or audio device is needed.
"""
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

bridge, lib, work = (Path(x).resolve() for x in sys.argv[1:])
# Desktop imports are thin adapters; mutate the shared ABI implementation.
shared = bridge.parents[2] / 'bindings/native'
if shared.is_dir():
    bridge = shared
work.mkdir(parents=True, exist_ok=True)
(work / 'jottune_bridge.c').write_bytes((bridge / 'jottune_bridge.c').read_bytes())
header = (bridge / 'jottune_bridge.h').read_text()
args = shlex.split(os.environ.get('CC', 'cc')) + [
    '-std=c99', '-D_GNU_SOURCE', '-fsyntax-only',
    '-DFM_SINE_BITS=12', '-DFM_EXP_BITS=10', '-DFM_PHASE64=1', '-DFM_ENV64=1',
    '-DSP_BINAURAL_H="' + str(lib / 'sp_binaural_tables.h') + '"',
    '-I' + str(lib.parent.parent / 'src/host'), '-I' + str(lib), str(work / 'jottune_bridge.c')]
(work / 'jottune_bridge.h').write_text(header)
positive = subprocess.run(args, text=True, capture_output=True)
(work / 'positive.log').write_text(positive.stdout + positive.stderr)
if positive.returncode:
    raise SystemExit('BRIDGE_ABI valid header refused\n' + positive.stderr)
mutant, count = re.subn(r'(#define FMS_NAME\s+)32\b', r'\g<1>31', header)
if count != 1:
    raise SystemExit('BRIDGE_ABI FMS_NAME control did not apply exactly once')
(work / 'jottune_bridge.h').write_text(mutant)
negative = subprocess.run(args, text=True, capture_output=True)
(work / 'negative.log').write_text(negative.stdout + negative.stderr)
(work / 'jottune_bridge.h').write_text(header)
if not negative.returncode or 'fms_chk_name' not in negative.stderr:
    raise SystemExit('BRIDGE_ABI FMS_NAME31 was not refused by its guard\n' + negative.stderr)
print('BRIDGE_ABI constants=5 syntax=yes FMS_NAME31=refused ok')
