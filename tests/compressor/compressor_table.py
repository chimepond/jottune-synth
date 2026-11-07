#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Derive and check  constants with Decimal70; no source is written."""
from decimal import Decimal, localcontext, ROUND_HALF_UP
import hashlib
from pathlib import Path
import re
import struct

root = Path(__file__).resolve().parents[2]
source = (root / "src/fx/fx.c").read_text(encoding="utf-8")
with localcontext() as ctx:
    ctx.prec = 70
    ctx.rounding = ROUND_HALF_UP
    d = Decimal
    log = [int((32768 * (1 + d(i) / 64).ln() / d(2).ln()).to_integral_value()) for i in range(64)]
    atten = [int((d(2)**31 * d(10)**(-d(2)**i / 200)).to_integral_value()) for i in range(11)]
    scale = int((65536 * 200 * d(2).log10()).to_integral_value())
digest = hashlib.sha256(struct.pack("<64H", *log)).hexdigest()
if digest != "16bbbf13b60f1247928545e9ae84c756d34ae575b6cabeea483a380dcd491a9f":
    raise SystemExit("COMPTABLE failed: derived table digest")
for name, expected in (("FX_LOG2", log), ("FX_DB10", atten)):
    body = re.search(r"\b" + name + r"\[\d+\]\s*=\s*\{(.*?)\};", source, re.S)
    if not body:
        raise SystemExit("COMPTABLE failed: missing " + name)
    text = re.sub(r"/\*.*?\*/", "", body.group(1), flags=re.S)
    actual = [int(word.strip().rstrip("u")) for word in text.split(",") if word.strip()]
    if actual != expected:
        raise SystemExit("COMPTABLE failed: " + name)
if scale != 3945660 or atten[-1] != 16290:
    raise SystemExit("COMPTABLE failed: derived scalar")
print("COMPTABLE ok log_entries=64 attenuation_entries=11 scale=%d sha256=%s" % (scale, digest))
