#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""tables_oracle.py — external numeric validation tool.

Compares the libm and the integer table generators with 60-digit decimal
values at every size the generators accept (sine 2..16 bits, exp2 1..16).
Per size it prints the mismatch counts, the integer route's worst Q16.48
error before rounding, and the smallest distance of an exact value from a
Q2.30 rounding boundary, in Q16.48 units. Where the worst error is below
the smallest distance, the integer rounding cannot differ from the exact.

usage: python3 tables_oracle.py GEN_TABLES GEN_TABLES_INT
"""
import re
import subprocess
import sys
from decimal import Decimal as D, getcontext, ROUND_FLOOR

getcontext().prec = 60
FAR = D(10 ** 9)


def dec_pi():
    getcontext().prec += 2
    lasts, t, s, n, na, d, da = 0, D(3), 3, 1, 0, 0, 24
    while s != lasts:
        lasts = s
        n, na = n + na, na + 8
        d, da = d + da, da + 32
        t = (t * n) / d
        s += t
    getcontext().prec -= 2
    return +s


def dec_sin(x):
    getcontext().prec += 2
    i, lasts, s, fact, num, sign = 1, 0, x, 1, x, 1
    while s != lasts:
        lasts = s
        i += 2
        fact *= i * (i - 1)
        num *= x * x
        sign *= -1
        s += num / fact * sign
    getcontext().prec -= 2
    return +s


PI = dec_pi()
LN2 = D(2).ln()
Q30 = D(2) ** 30
Q48 = D(2) ** 48
# Finest grids; every smaller size is a subsample: sine quadrant at N = 2^16, exp2 at E = 2^16.
SIN = [dec_sin(PI * i / 32768) for i in range(16384 + 1)]
EXP = [(-(D(k) / 65536) * LN2).exp() for k in range(65536 + 1)]


def rnd(v):
    """llround of a nonnegative value: floor(v + 1/2). No exact ties occur here."""
    return int((v + D("0.5")).to_integral_value(rounding=ROUND_FLOOR))


def margin48(v):
    """Distance of v * 2^30 from the nearest half-integer, in Q16.48 units."""
    x = v * Q30
    return abs(x - x.to_integral_value(rounding=ROUND_FLOOR) - D("0.5")) * 2 ** 18


def run(gen, *args):
    return subprocess.run([gen] + [str(a) for a in args], capture_output=True, text=True, check=True).stdout


def tables(gen, sb, eb):
    out = run(gen, sb, eb)
    def arr(name):
        body = re.search(name + r"\[\d+\] = \{([^}]*)\}", out).group(1)
        return [int(t) for t in body.replace("\n", " ").split(",") if t.strip()]
    return arr("fm_sine_tbl"), arr("fm_exp2_tbl")


def q48(gen, sb, eb):
    s, e = {}, {}
    for line in run(gen, sb, eb, "q48").splitlines():
        t, i, v = line.split()
        (s if t == "S" else e)[int(i)] = int(v)
    return s, e


def report(name, bits, exact, orc, lib, itb, full_diff, i48):
    last = len(exact) - 1
    bl = sum(1 for i in range(last + 1) if lib[i] != orc[i])
    bi = sum(1 for i in range(last + 1) if itb[i] != orc[i])
    err = max(abs(D(i48[i]) - exact[i] * Q48) for i in range(last + 1))
    # The end points are exact Q2.30 integers (0 or 2^30 for sine, 2^30 and 2^29 for exp2).
    mar = min((margin48(exact[i]) for i in range(1, last)), default=FAR)
    print(f"  {name} {bits:2d} {last + 1:6d} {bl:4d} {bi:4d} {full_diff:4d} {float(err):8.2f} {float(mar):10.2f}")
    return bl, bi, err, mar


def main():
    glibm, gint = sys.argv[1], sys.argv[2]
    tot_l = tot_i = tot_d = 0
    worst, least = D(0), FAR
    print("table bits entries libm!=exact int!=exact libm!=int max_err48 min_margin48")
    for sb in range(2, 17):
        n4 = (1 << sb) // 4
        ls, _ = tables(glibm, sb, 8)
        its, _ = tables(gint, sb, 8)
        s48, _ = q48(gint, sb, 8)
        exact = [SIN[i * (16384 // n4)] for i in range(n4 + 1)]
        orc = [min(rnd(v * Q30), 1 << 30) for v in exact]
        dif = sum(1 for a, b in zip(ls, its) if a != b)   # whole table, mirrored quadrants included
        bl, bi, err, mar = report("sine", sb, exact, orc, ls, its, dif, s48)
        tot_l, tot_i, tot_d = tot_l + bl, tot_i + bi, tot_d + dif
        worst, least = max(worst, err), min(least, mar)
    for eb in range(1, 17):
        e = 1 << eb
        _, le = tables(glibm, 11, eb)
        _, ie = tables(gint, 11, eb)
        _, e48 = q48(gint, 11, eb)
        exact = [EXP[k * (65536 // e)] for k in range(e + 1)]
        orc = [rnd(v * Q30) for v in exact]
        dif = sum(1 for a, b in zip(le, ie) if a != b)
        bl, bi, err, mar = report("exp2", eb, exact, orc, le, ie, dif, e48)
        tot_l, tot_i, tot_d = tot_l + bl, tot_i + bi, tot_d + dif
        worst, least = max(worst, err), min(least, mar)
    print(f"TOTAL libm!=exact {tot_l} int!=exact {tot_i} libm!=int {tot_d}"
          f" max_err48 {float(worst):.2f} min_margin48 {float(least):.2f}")


if __name__ == "__main__":
    main()
