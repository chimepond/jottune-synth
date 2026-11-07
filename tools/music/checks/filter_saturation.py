#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Measure the existing voice filter's Q2.30 state saturation .

Run from any directory; needs a C99 host compiler, no Python packages.
Build a temporary instrumented copy of the current engine, count its two
state clamps and input-difference clamp, and sweep sine level and Q.
Backs the  filter section: 1176 cases, 64 with state saturation and
78 with input-difference limiting. Runs in about 1.4 seconds on the Mac
(0.9 seconds on the Linux server). No library or record is edited.
The finite grid describes these stimuli; it does not establish a safe Q
ceiling for arbitrary music, transients, cutoff motion or cascades.
"""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import sys
import time


PROBE = r'''
#include <math.h>
#include <stdio.h>
#include <inttypes.h>
static uint64_t hits[3];
static int64_t raw_peak[3];
static int32_t probe_state(int64_t x, unsigned state)
{
    int64_t mag = x < 0 ? -x : x;
    if (mag > raw_peak[state]) raw_peak[state] = mag;
    if (x > 2147483647 || x < -2147483647) hits[state]++;
    return x > 2147483647 ? 2147483647 : x < -2147483647 ? -2147483647 : (int32_t)x;
}
#include "measured_engine.c"
int main(void)
{
    const uint32_t rates[] = {48000, 44100};
    const uint32_t cutoffs[] = {100000, 500000, 2000000, 8000000};
    const uint32_t qs[] = {500, 707, 1000, 2000, 4000, 8000, 16000};
    const unsigned levels[] = {0, 6, 12, 18, 24, 30, 36};
    const double ratios[] = {0.5, 1.0, 2.0};
    unsigned r, c, q, l, h, total = 0, saturated = 0, input_limited = 0;
    uint64_t control_hits = 0;
    printf("fs,cutoff_millihz,q_milli,attenuation_db,input_over_cutoff,ic1_clamps,ic2_clamps,v3_clamps,raw_ic1_peak,raw_ic2_peak\n");
    for (r = 0; r < sizeof rates / sizeof *rates; r++)
    for (c = 0; c < sizeof cutoffs / sizeof *cutoffs; c++)
    for (q = 0; q < sizeof qs / sizeof *qs; q++)
    for (l = 0; l < sizeof levels / sizeof *levels; l++)
    for (h = 0; h < sizeof ratios / sizeof *ratios; h++) {
        fm_voice_t v;
        fm_patch_t p = {0};
        uint32_t i, frames = rates[r];
        const double amp = exp2(-(double)levels[l] / 6.0205999132796239);
        const double step = 6.2831853071795864769 * (cutoffs[c] / 1000.0) * ratios[h] / rates[r];
        p.filter.mode = 1;
        p.filter.cutoff_millihz = cutoffs[c];
        p.filter.q_milli = qs[q];
        fm_voice_init(&v, &p, rates[r]);
        hits[0] = hits[1] = hits[2] = 0;
        raw_peak[0] = raw_peak[1] = raw_peak[2] = 0;
        for (i = 0; i < frames; i++) {
            const int32_t x = (int32_t)floor(1073741824.0 * amp * sin(step * i) + 0.5);
            (void)fm_svf_step(&v.svf, x);
        }
        total++;
        saturated += hits[0] != 0 || hits[1] != 0;
        input_limited += hits[2] != 0;
        if (r == 0 && c == 1 && q == 6 && l == 0 && h == 1)
            control_hits = hits[0] + hits[1];
        printf("%u,%u,%u,%u,%.1f,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRId64 ",%" PRId64 "\n",
               rates[r], cutoffs[c], qs[q], levels[l], ratios[h],
               hits[0], hits[1], hits[2], raw_peak[0], raw_peak[1]);
    }
    /* A high-Q driven control must expose actual clamps, while zero
     * input from zero state must remain zero for a complete second. */
    {
        fm_voice_t v; fm_patch_t p = {0}; uint32_t i;
        p.filter.mode = 1; p.filter.cutoff_millihz = 500000; p.filter.q_milli = 16000;
        fm_voice_init(&v, &p, 48000);
        hits[0] = hits[1] = hits[2] = 0; raw_peak[0] = raw_peak[1] = raw_peak[2] = 0;
        for (i = 0; i < 48000; i++) if (fm_svf_step(&v.svf, 0)) return 1;
        if (hits[0] || hits[1] || hits[2] || v.svf.ic1 || v.svf.ic2 || !control_hits) return 1;
    }
    fprintf(stderr, "FILTER_SATURATION cases=%u saturated=%u input_limited=%u controls=2/2\n", total, saturated, input_limited);
    return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cc', default=os.environ.get('CC', 'cc'),
                        help='host compiler command (default: CC or cc)')
    args = parser.parse_args()
    spike = Path(__file__).resolve().parents[3] / 'src/host'
    source = (spike / 'fm_a1.c').read_text()
    # Instrument only the two actual state stores. Do not reimplement the
    # recurrence or infer saturation merely from a state hitting its limit.
    for index, field in enumerate(('ic1', 'ic2')):
        old = f'f->{field} = fm_sat32(v{index + 1} + v{index + 1} - f->{field});'
        new = f'f->{field} = probe_state(v{index + 1} + v{index + 1} - f->{field}, {index});'
        if source.count(old) != 1:
            raise SystemExit(f'filter state store changed: inspect instrumentation for {field}')
        source = source.replace(old, new)
    old = 'const int32_t v3 = fm_sat32((int64_t)v0 - f->ic2);'
    if source.count(old) != 1:
        raise SystemExit('filter input difference changed: inspect instrumentation')
    source = source.replace(old, 'const int32_t v3 = probe_state((int64_t)v0 - f->ic2, 2);')
    cc = shlex.split(args.cc)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='fm-filter-sweep-') as directory:
        work = Path(directory)
        (work / 'measured_engine.c').write_text(source)
        (work / 'probe.c').write_text(PROBE)
        subprocess.run(cc + ['-std=c99', '-O2', str(spike / 'gen_tables.c'),
                            '-lm', '-o', str(work / 'tables')], check=True)
        with (work / 'fm_tables.h').open('w') as output:
            subprocess.run([str(work / 'tables'), '12', '10'], stdout=output, check=True)
        subprocess.run(cc + ['-std=c99', '-O2', '-DFM_SINE_BITS=12', '-DFM_EXP_BITS=10',
                            '-DFM_PHASE64=1', '-DFM_ENV64=1', '-I' + str(spike),
                            '-I' + str(work), str(work / 'probe.c'), '-lm',
                            '-o', str(work / 'probe')], check=True)
        subprocess.run([str(work / 'probe')], check=True)
    print(f'FILTER_SATURATION elapsed_seconds={time.monotonic() - started:.3f}',
          file=sys.stderr)


if __name__ == '__main__':
    main()
