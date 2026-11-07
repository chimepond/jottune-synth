#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Independent, suite-first generator validation .

The detector profile tests deliberately bad fixtures. It is never evidence
that an absent production generator passed. Full statistical constants are
the frozen design protocol, not parameters selected from measured output.
"""
import argparse
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time

import reference

SEEDS = (1, 2, 3, 0x12345678, 0x9e3779b9, 0xffffffff, 0, 0x6d2b79f5)
KINDS = ('white', 'pink', 'brown', 'multi', 'euclid', 'thue', 'cantor',
         'fibonacci', 'lsystem', 'logistic', 'henon')
PROTOCOL = dict(warmup=1 << 20, samples=1 << 22, window=1 << 18,
                windows=31, overlap=0.5, hann='symmetric', bands=60,
                band_first_exponent=-13, bands_per_octave=6,
                averaging='linear', bin_membership='2^(30+k)<=j^6<2^(31+k)')
# Flags of the three gen builds (the library object adds
# -mgeneral-regs-only) and make knobs of the soak's three harness builds;
# the JSON report records both.
COMMON_FLAGS = ('-std=c99', '-pedantic', '-Wall', '-Wextra', '-Wshadow', '-O2')
UBSAN_FLAGS = ('-O1', '-g', '-fsanitize=undefined', '-fno-sanitize-recover=all')
TARGET_FLAGS = dict(host=(), portable=('-DFP_MATH_FORCE_PORTABLE',),
                    ubsan=('-DFP_MATH_FORCE_PORTABLE',) + UBSAN_FLAGS)
SOAK_MAKE = ('SINE_BITS=12', 'EXP_BITS=10', 'PHASE64=1', 'ENV64=1')
SOAK_TARGET_MAKE = dict(host=(), portable=('EXTRA=-DFP_MATH_FORCE_PORTABLE',),
                        ubsan=('EXTRA=-DFP_MATH_FORCE_PORTABLE', 'OPT=' + ' '.join(UBSAN_FLAGS)))


def fnv(data):
    value = 0xcbf29ce484222325
    for byte in data:
        value = ((value ^ byte) * 0x100000001b3) & 0xffffffffffffffff
    return f'{value:016x}'


class Report:
    def __init__(self):
        self.rows = []

    def add(self, metric, value, threshold, passed=None, *, status=None,
            source='suite', seed=None, count=0, target='host', artifact=b'', required=True):
        row = dict(target=target, source=source, seed=seed, samples=count,
                   metric=metric, value=value, threshold=threshold,
                   status=status or ('PASS' if passed else 'FAIL'),
                   artifact_hash=fnv(artifact), required=required)
        self.rows.append(row)
        print(f"{row['status']:13} {target:8} {source:10} {metric}: "
              f"{value} [required {threshold}]", flush=True)

    def complete(self):
        return all(row['status'] == 'PASS' for row in self.rows if row['required'])


def exact(actual, expected):
    """Compare full integer states, not just quantized outputs or hashes."""
    return actual == expected


def bounded(values, low, high):
    return bool(values) and all(low <= value <= high for value in values)


def smooth(values, slew):
    previous = 0
    for value in values:
        if abs(value - previous) > slew:
            return False
        previous = value
    return 1 <= slew <= 512


def scale_ok(before, after, mask):
    return (before - 1200 < after <= before and after % 100 == 0
            and bool(mask & (1 << ((after // 100) % 12))))


def cycle_ok(states, gap=1000000, hash_key=hash):
    """Buckets retain exact tuples; a hash collision is not a cycle.

    This small-fixture implementation is also the detector control oracle.
    Full C streams use the same exact equality with a packed state table to
    remain below the design's 256 MiB per-profile limit.
    """
    seen = {}
    count = 0
    for index, state in enumerate(states):
        state = tuple(state)
        bucket = seen.setdefault(hash_key(state), {})
        if state in bucket and 0 < index - bucket[state] < gap:
            return False
        bucket[state] = index
        count += 1
    return count > 1


#  (3), drift review L11-1: negative controls fed to the C detector
# (probe.c chaos) itself, each tripping exactly one of its checks, so that
# removing that check lets the map pass and fails the suite
# The independent detector tests exercise both cases.
CHAOS_CONTROLS = (
    # Logistic r=3.2 settles on an exact 2-cycle: varied output, no clamps.
    ('short-cycle', 'logistic', (16 * reference.Q48 // 5,),
     'FAIL by a repeat gap<1M alone (nonconstant, zero clamps)'),
    # Henon a=1/8, b=1 runs 2M aperiodic, varied states, but clamps.
    ('clamped-map', 'henon', (reference.Q48 // 8, reference.Q48),
     'FAIL by clamps>0 alone (2M states, no repeat, nonconstant)'))


def chaos_control_ok(name, returncode, output):
    """True when the chaos line rejects the control by its own check alone."""
    fields = dict(re.findall(r'(\w+)=(-?\w+)', output))
    try:
        samples, low, high, clamps = (int(fields[key]) for key in ('samples', 'minimum', 'maximum', 'clamps'))
    except (KeyError, ValueError):
        return False
    if returncode != 1 or fields.get('result') != 'FAIL' or low >= high:
        return False
    if name == 'short-cycle':
        return samples < 2000000 and clamps == 0
    return samples == 2000000 and clamps > 0


def spectral_metadata(metadata):
    return metadata == PROTOCOL


def ols(xs, ys):
    if len(xs) != len(ys) or len(xs) < 2:
        raise ValueError('insufficient fit points')
    xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - xm) ** 2 for x in xs)
    if denominator == 0:
        raise ValueError('constant fit coordinate')
    return sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / denominator


def band_fit(powers, metadata=PROTOCOL):
    if not spectral_metadata(metadata) or len(powers) != 60:
        raise ValueError('wrong spectral protocol')
    if any(not math.isfinite(p) or p <= 0 for p in powers):
        raise ValueError('nonpositive or nonfinite band power')
    centers = [(-13 + (k + .5) / 6) * math.log10(2) for k in range(60)]
    return ols(centers, [math.log10(p) for p in powers])


def spectrum(samples):
    """Exact full-profile Welch membership; streamed window accumulation."""
    import numpy as np
    if len(samples) != PROTOCOL['samples']:
        raise ValueError('wrong spectral sample count')
    n = PROTOCOL['window']
    window = .5 - .5 * np.cos(2 * np.pi * np.arange(n) / (n - 1))
    norm = np.sum(window * window)
    power = np.zeros(n // 2 + 1, dtype=np.float64)
    if np.max(samples) == np.min(samples):
        raise ValueError('constant spectral record')
    for i in range(31):
        data = np.asarray(samples[i * (n // 2): i * (n // 2) + n], dtype=np.float64)
        data -= np.mean(data)
        part = np.abs(np.fft.rfft(data * window)) ** 2 / norm
        part[1:-1] *= 2
        power += part / 31
    sums, counts = [0.] * 60, [0] * 60
    for j in range(1, n // 2 + 1):
        sixth = j ** 6
        # Exact powers-of-two bounds replace floating log band selection.
        k = sixth.bit_length() - 1 - 30
        if 0 <= k < 60:
            assert (1 << (30 + k)) <= sixth < (1 << (31 + k))
            sums[k] += float(power[j])
            counts[k] += 1
    if any(count == 0 for count in counts):
        raise ValueError('empty full-profile band')
    means = [total / count for total, count in zip(sums, counts)]
    return band_fit(means), means, [float(value) for value in power[1:32]]


def detector_tests(report):
    """Bad fixtures must be refused by the same measurement predicates."""
    def control(name, rejected):
        report.add(name, 'rejected' if rejected else 'accepted', 'reject', rejected,
                   source='detectors')

    for slope in (0, -1, -2):
        powers = [2 ** ((-13 + (k + .5) / 6) * slope) for k in range(60)]
        measured = band_fit(powers)
        report.add(f'fit-{slope}', measured, f'{slope} within 1e-12',
                   abs(measured - slope) < 1e-12, source='detectors')
    for name, powers in [('constant-spectrum', [0.] * 60),
                         ('nonfinite-spectrum', [float('nan')] * 60)]:
        try:
            band_fit(powers)
        except ValueError:
            control(name, True)
        else:
            control(name, False)
    control('white-as-pink', abs(band_fit([1.] * 60) + 1) > .15)
    wrong = dict(PROTOCOL, band_first_exponent=-12)
    control('wrong-frequency-band', not spectral_metadata(wrong))
    control('signed-overflow', not bounded([0, 32768], -32768, 32767))
    # With ONE=16, x=15, delta=3: reflection gives14; wrapping gives-14.
    control('wrap-instead-of-reflect', not exact(-14, 14))
    control('logistic-fixed-point', not cycle_ok([(0,)] * 18))
    control('planted-17-cycle', not cycle_ok([(i % 17,) for i in range(35)]))
    report.add('colliding-hash', 'distinct states retained', 'accept',
               cycle_ok([(i, i * 3) for i in range(35)], hash_key=lambda _: 0),
               source='detectors')
    control('euclidean-rotation', not exact('01001010', '00100101'))
    control('fibonacci-symbol', not exact('0100101001011', '0100101001001'))
    control('omitted-production', not exact('AA', 'ABA'))
    expected = (17, 4567, 1234, 7, ((4, 'off'),))
    for index, name in enumerate(('counter', 'random-word', 'smoother', 'row', 'pending-off')):
        changed = list(expected)
        changed[index] = None
        control(f'snapshot-missing-{name}', not exact(tuple(changed), expected))
    control('scale-step', not scale_ok(6100, 6200, 1 << 2))
    control('slew-513', not smooth([512, 1025], 512))
    control('once-per-block', not exact([0, 7, 14], [0, 64, 128]))
    control('reseed-on-note', not exact([7, 9, 7], [7, 9, 12]))
    report.add('slew-startup', [512, 1024, 512], 'delta<=512',
               smooth([512, 1024, 512], 512), source='detectors')


def run(command, **kwargs):
    result = subprocess.run([str(x) for x in command], capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{' '.join(map(str, command))}: exit {result.returncode}: "
                           + result.stderr.decode(errors='replace')[-4000:])
    return result


HOST_IMPORTS = {'memcpy', 'memmove', 'memset', 'bzero', '__stack_chk_fail', '__stack_chk_guard'}
#  (2),  in 's form: each cross target's exact used set,
# measured on gcc 13.2 and 16.1. gen.c divides signed 64-bit values as
# unsigned magnitudes (quotient), so as in the engine's lists  no
# list admits __aeabi_ldivmod or __divdi3; the signed-division control
# must show that relocation and fail.
CROSS_IMPORTS = dict(
    m0={'__aeabi_idiv', '__aeabi_idivmod', '__aeabi_lmul', '__aeabi_uidiv',
        '__aeabi_uidivmod', '__aeabi_uldivmod', 'memcpy', 'memset'},
    m4={'__aeabi_uldivmod', 'memcpy', 'memset'},
    rv32={'__udivdi3', '__umoddi3', 'memcpy', 'memset'})
SIGNED_HELPER = dict(m0='__aeabi_ldivmod', m4='__aeabi_ldivmod', rv32='__divdi3')


def imports(obj, nm='nm', objdump=None):
    undefined = {line.split()[-1] for line in run([nm, '-u', obj]).stdout.decode().splitlines() if line.split()}
    if objdump:
        dump = run([objdump, '-r', obj]).stdout.decode()
        if not dump.strip():
            raise RuntimeError('empty successful relocation dump')
        relocated = {re.sub(r'[+-]0x[0-9a-fA-F]+$', '', match)
                     for match in re.findall(r'^\S+\s+R_\S+\s+(\S+)', dump, re.M)}
        undefined &= relocated
    elif sys.platform == 'darwin':
        undefined = {symbol[1:] if symbol.startswith('_') else symbol for symbol in undefined}
    return undefined


def mutable_symbols(obj, nm='nm', darwin=False):
    if darwin:
        return [line for line in run([nm, '-m', obj]).stdout.decode().splitlines()
                if re.search(r'\(__DATA,__(?:data|bss|common)\)|\(common\)', line)]
    return [line for line in run([nm, obj]).stdout.decode().splitlines()
            if re.search(r'\s[bBdDgGsScC]\s', line)]


def build_targets(directory, selected, report, full):
    here = Path(__file__).resolve().parents[2] / 'src/gen'
    cc = shlex.split(os.environ.get('CC', 'cc'))
    common = list(COMMON_FLAGS)
    targets = ('host', 'portable', 'ubsan') if selected == 'all' else (selected,)
    binaries = {}
    for target in targets:
        extra = list(TARGET_FLAGS[target])
        obj = directory / f'{target}.o'
        run(cc + common + extra + ['-mgeneral-regs-only', '-c', here / 'gen.c', '-o', obj])
        binary = directory / f'probe-{target}'
        run(cc + common + extra + [here / 'probe.c', obj, '-o', binary])
        binaries[target] = binary
        # Sanitizer instrumentation intentionally imports host diagnostics;
        # audit the matching uninstrumented portable library separately.
        audit = obj
        if target == 'ubsan':
            audit = directory / 'ubsan-audit.o'
            run(cc + common + ['-DFP_MATH_FORCE_PORTABLE', '-mgeneral-regs-only', '-c', here / 'gen.c', '-o', audit])
        used = imports(audit)
        globals_ = mutable_symbols(audit, darwin=sys.platform == 'darwin')
        report.add('integer-imports', sorted(used), sorted(HOST_IMPORTS), used <= HOST_IMPORTS, target=target)
        report.add('mutable-globals', globals_, 'none', not globals_, target=target)
        result = run([binary, 'sizes']).stdout.decode().strip()
        sizes = {key: int(value) for key, value in re.findall(r'(\w+)=(\d+)', result)}
        report.add('memory', sizes, 'source<=768, bank<=16384, mutable<=81920',
                   sizes['source'] <= 768 and sizes['bank'] <= 16384 and sizes['mutable'] <= 81920,
                   target=target)
        for command, metric in [('params-selftest', 'shared-parameter-descriptors'),
                                ('selftest', 'state-refusals-continuation'),
                                ('lifecycle', 'lifecycle-and-snapshot-controls'),
                                ('euclidean', 'exhaustive-euclidean')]:
            result = run([binary, command]).stdout.decode().strip()
            report.add(metric, result, 'zero failures', 'failures=0' in result, target=target)
    # Alter the shared data itself: the API comparison must detect drift,
    # rather than merely confirm that two consumers read the same row.
    descriptors = (here / 'params.def').read_text()
    descriptor_controls = {
        'default': ('q15, 1, 1, 16)', 'q15, 1, 1, 17)'),
        'bound': ('i64, 1, 4096, q15', 'i64, 1, 4097, q15'),
        'mutability': ('q15, 1, 1, 16)', 'q15, 0, 1, 16)'),
        'slot': ('GEN_PARAM(16, step, 4, p0', 'GEN_PARAM(16, step, 4, p1'),
        'kind': ('GEN_KIND(2, pink)', 'GEN_KIND(12, pink)'),
        'field-id': ('GEN_PARAM(16, step, 4', 'GEN_PARAM(15, step, 4')}
    for name, (old, new) in descriptor_controls.items():
        if descriptors.count(old) != 1:
            raise RuntimeError('descriptor control no longer identifies exactly one field: '+name)
        path = directory / f'bad-params-{name}.def'
        path.write_text(descriptors.replace(old, new))
        binary = directory / f'bad-params-{name}'
        # Compile the probe against the altered table, keeping the actual
        # production core and its frozen public API unchanged.
        run(cc + common + ['-DGEN_PARAMS_FILE="'+str(path)+'"', here / 'probe.c',
                           here / 'gen.c', '-o', binary])
        result = subprocess.run([str(binary), 'params-selftest'], capture_output=True)
        rejected = result.returncode == 1 and b'GEN_PARAMS checks=' in result.stdout and b'failures=0' not in result.stdout
        report.add('descriptor-control-'+name, result.stdout.decode().strip(),
                   'detect altered shared descriptor', rejected)
    # Controls exercise compiler, actual imports, and actual writable symbols.
    # local-double and libm are 's second and third controls, as check.sh
    # writes them (the table keeps the libm call from folding to a constant).
    controls = {
        'fp': 'double forbidden_fp(double x) { return x + 1.0; }\n',
        'heap': '#include <stddef.h>\nextern void *malloc(size_t);\nvoid *forbidden_heap(void) { return malloc(1); }\n',
        'global': 'int forbidden_global; int use_global(void) { return ++forbidden_global; }\n',
        'local-double': 'int forbidden_local(int x) { double d = x; d *= 1.5; return (int)d; }\n',
        'libm': ('double sin(double);\nlong lround(double);\n'
                 'static const double t[4] = { 0.25, 0.5, 1.0, 2.0 };\n'
                 'long forbidden_libm(unsigned i) { return lround(sin(t[i & 3u])); }\n')}
    for name, source in controls.items():
        path = directory / f'bad-{name}.c'; path.write_text(source)
        obj = path.with_suffix('.o')
        result = subprocess.run(cc + common + ['-mgeneral-regs-only', '-c', str(path), '-o', str(obj)], capture_output=True)
        rejected = result.returncode != 0
        if not rejected:
            rejected = bool(imports(obj) - HOST_IMPORTS or mutable_symbols(obj, darwin=sys.platform == 'darwin'))
        report.add(f'constraint-control-{name}', 'rejected' if rejected else 'accepted', 'reject', rejected)
    cross = [
        ('m0', 'arm-none-eabi-gcc', 'arm-none-eabi-nm', ('arm-none-eabi-objdump',),
         ['-mcpu=cortex-m0plus', '-mthumb', '-mfloat-abi=soft']),
        ('m4', 'arm-none-eabi-gcc', 'arm-none-eabi-nm', ('arm-none-eabi-objdump',),
         ['-mcpu=cortex-m4', '-mthumb', '-mfloat-abi=soft']),
        ('rv32', 'riscv64-elf-gcc', 'riscv64-elf-nm', ('riscv64-elf-objdump', 'riscv64-unknown-elf-objdump'),
         ['-march=rv32imac', '-mabi=ilp32'])]
    for target, compiler, nm, dumps, flags in cross:
        if target == 'rv32' and not shutil.which(compiler):
            compiler, nm = 'riscv64-unknown-elf-gcc', 'riscv64-unknown-elf-nm'
        dump = next((tool for tool in dumps if shutil.which(tool)), None)
        if not shutil.which(compiler) or not shutil.which(nm) or not dump:
            report.add('integer-imports', 'toolchain unavailable', 'relocation-filtered audit', status='DEFERRED',
                       target=target, required=full and selected == 'all')
            continue
        obj = directory / f'{target}.o'
        preflight = directory / f'{target}-headers.c'
        preflight.write_text('#include <stdint.h>\n#include <stddef.h>\nuint32_t headers(uint32_t x) { return x; }\n')
        flags = ['-ffreestanding'] + flags
        result = subprocess.run([compiler] + common + flags + ['-c', str(preflight), '-o', str(obj)], capture_output=True)
        if result.returncode:
            report.add('integer-imports', 'toolchain headers unavailable: ' + result.stderr.decode(errors='replace')[-1000:],
                       'usable cross compiler', status='DEFERRED', target=target, required=full and selected == 'all')
            continue
        run([compiler] + common + flags + ['-DFP_MATH_FORCE_PORTABLE', '-c', here / 'gen.c', '-o', obj])
        used = imports(obj, nm, dump)
        allowed = CROSS_IMPORTS[target]
        report.add('integer-imports', sorted(used), sorted(allowed), used <= allowed, target=target)
        globals_ = mutable_symbols(obj, nm)
        report.add('mutable-globals', globals_, 'none', not globals_, target=target)
        for name in controls:
            path = directory / f'bad-{name}.c'
            result = subprocess.run([compiler]+common+flags+['-c',str(path),'-o',str(obj)],capture_output=True)
            rejected = result.returncode != 0
            if not rejected:
                rejected = bool(imports(obj,nm,dump)-allowed or mutable_symbols(obj,nm))
            report.add(f'constraint-control-{name}', 'rejected' if rejected else 'accepted', 'reject', rejected, target=target)
        # 's fourth control: a real signed 64-bit division must compile,
        # show its helper in both nm -u and the relocations, and fall outside.
        path = directory / 'bad-signed-division.c'
        path.write_text('#include <stdint.h>\nint64_t forbidden_division(int64_t a, int64_t b) { return a / b; }\n')
        result = subprocess.run([compiler]+common+flags+['-c',str(path),'-o',str(obj)],capture_output=True)
        extra = sorted(imports(obj, nm, dump) - allowed) if result.returncode == 0 else ['does not compile']
        report.add('constraint-control-signed-division', extra, 'relocated ' + SIGNED_HELPER[target] + ' outside the list',
                   SIGNED_HELPER[target] in extra, target=target)
    return binaries


PARAMS = dict(brown=('step_q15',), multi=('p', 'k', 'octaves', 'gain_q15'),
              logistic=('r_q48',), henon=('a_q48', 'b_q48'), euclid=('k', 'n', 'rot'),
              thue=('length', 'rot'), cantor=('length', 'rot'), fibonacci=('depth', 'rot'), lsystem=('rot',))


def model(kind, seed, params):
    extra = dict(axiom='AB', rules={'A': 'AB', 'B': 'A'}, depth=5) if kind == 'lsystem' else {}
    return reference.Source(kind, seed, **dict(zip(PARAMS.get(kind, ()), params)), **extra)


def raw_vector(kind, seed, params, count=4096):
    source = model(kind, seed, params)
    data = bytearray(source.state_bytes())
    for _ in range(count):
        source.update()
        data.extend(source.state_bytes())
    return bytes(data)


def parameter_fixtures(kind, count):
    """Literal reproducible valid sets; shared fixture PRNG is independent of sources."""
    word = 0x243f6a88
    def choose(low, high):
        nonlocal word
        word = reference.xorshift(word)
        value = word
        if high - low + 1 > 1 << 32:
            word = reference.xorshift(word)
            value = (value << 32) | word
        return low + value % (high - low + 1)
    for _ in range(count):
        if kind == 'brown': p = [choose(1, 4096)]
        elif kind == 'multi':
            p, k, octaves = choose(2, 65536), choose(2, 16), choose(1, 8)
            while p * k ** (octaves - 1) > 0xffffffff: octaves -= 1
            p = [p, k, octaves, choose(0, 32768)]
        elif kind == 'logistic': p = [choose(0, 4 << 48)]
        elif kind == 'henon': p = [choose(0, 2 << 48), choose(-(1 << 48), 1 << 48)]
        elif kind == 'euclid':
            n = choose(1, 65535); p = [choose(0, n), n, choose(0, n - 1)]
        elif kind in ('thue', 'cantor'):
            n = choose(1, 65535); p = [n, choose(0, n - 1)]
        elif kind == 'fibonacci':
            depth = choose(0, 16); p = [depth, choose(0, len(reference.fibonacci_word(depth)) - 1)]
        elif kind == 'lsystem': p = [choose(0, 20)]
        else: p = []
        yield dict(seed=choose(0, 0xffffffff), parameters=p)


def scalar_endpoints():
    """Each scalar's default/min/max and immediate invalid neighbours.

    Length-dependent limits use a valid companion configuration; joint
    period overflow is a separate refusal rather than an invalid fixture.
    """
    q = 1 << 48
    base = dict(brown=[16], multi=[256, 2, 6, 24576], logistic=[(399*q+50)//100],
                henon=[(7*q+2)//5, (3*q+5)//10], euclid=[3,8,0],
                thue=[256,0], cantor=[256,0], fibonacci=[12,0], lsystem=[0])
    bounds = dict(brown=[(1,4096)], multi=[(2,65536),(2,16),(1,8),(0,32768)],
                  logistic=[(0,4*q)], henon=[(0,2*q),(-q,q)],
                  euclid=[(0,8),(1,65535),(0,7)], thue=[(1,65535),(0,255)],
                  cantor=[(1,65535),(0,255)], fibonacci=[(0,16),(0,376)], lsystem=[(0,20)])
    for kind in reference.KINDS:
        yield kind, [], True
        for field, (low, high) in enumerate(bounds.get(kind, [])):
            for value, valid in [(low,True), (high,True), (low-1,False), (high+1,False)]:
                params = base[kind].copy()
                if kind == 'multi': params[2] = 1
                if kind == 'euclid' and field == 1: params[0] = 0
                params[field] = value
                yield kind, params, valid
        # Every otherwise-unused scalar is explicitly required to remain0.
        used = len(base.get(kind, []))
        for field in range(used, 4):
            params = base.get(kind, []).copy() + [0] * (4-used)
            params[field] = 1
            yield kind, params, False
    yield 'multi', [65535,16,5,32768], True
    yield 'multi', [65536,16,5,32768], False
    yield 'henon', [2*q,q], True
    yield 'henon', [2*q,-q], True
    yield 'euclid', [65535,65535,65534], True


def edge_profiles(binaries, profile, report):
    fixtures = list(scalar_endpoints())
    good = {target: True for target in binaries}
    for kind, params, valid in fixtures:
        expected = raw_vector(kind, 0xffffffff, params) if valid else None
        for target, binary in binaries.items():
            result = subprocess.run([str(x) for x in [binary, 'vector', reference.KINDS.index(kind)+1,
                                                       0xffffffff, 4096] + params], capture_output=True)
            passed = result.returncode == 0 and result.stdout == expected if valid else result.returncode == 2 and not result.stdout
            if not passed: print(f'ENDPOINT mismatch {target} {kind} {params} valid={valid}: {result.stderr!r}', flush=True)
            good[target] &= passed
    for target, passed in good.items():
        report.add('scalar-endpoints-and-neighbours', len(fixtures), 'exact4096 updates or explicit refusal', passed,
                   target=target, artifact=json.dumps(fixtures).encode())
    # Exhaust all short A/B rules (including deletion and missing rules).
    words = []
    for rule_a, rule_b in itertools.product((None, '', 'A', 'B', 'AA', 'AB', 'BA', 'BB'), repeat=2):
        rules = {key:value for key,value in [('A',rule_a),('B',rule_b)] if value is not None}
        for depth in range(4): words.append(('AB', rules, depth))
    words += [(symbol, {}, 16) for symbol in 'ABCDEFGH']
    words += [('A', {'A':'AA'},14), ('AB', {'A':'AA'},14), ('A', {'A':'B'*32,'B':''},4)]
    random_word = 0x243f6a88
    def choose(n):
        nonlocal random_word
        random_word = reference.xorshift(random_word)
        return random_word % n
    for _ in range(256 if profile == 'full' else 16):
        axiom = ''.join('ABCDEFGH'[choose(8)] for _ in range(1+choose(8)))
        rules = {symbol: ''.join('ABCDEFGH'[choose(8)] for _ in range(choose(5)))
                 for symbol in 'ABCDEFGH' if choose(2)}
        words.append((axiom,rules,choose(7)))
    good = {target: True for target in binaries}
    for axiom, rules, depth in words:
        try: expected = reference.expand_word(axiom, rules, depth).encode()
        except ValueError: expected = None
        args = [f'{symbol}={rhs or "-"}' for symbol,rhs in rules.items()]
        for target, binary in binaries.items():
            result = subprocess.run([str(binary),'word',axiom,str(depth)] + args,capture_output=True)
            good[target] &= (result.returncode == 2 and not result.stdout) if expected is None else (result.returncode == 0 and result.stdout == expected)
    malformed = [('', '0'), ('I','0'), ('A','17'), ('A','-1'), ('A'*33,'0'),
                 ('A','1','A=I'), ('A','1','A=A','A=B'), ('A','1','I=A'), ('A','1','A='+'A'*33)]
    for target,binary in binaries.items():
        for args in malformed:
            result = subprocess.run([str(binary),'word']+list(args),capture_output=True)
            good[target] &= result.returncode == 2 and not result.stdout
        report.add('word-expansion-and-refusals', len(words)+len(malformed), 'independent exact expansion/refusal', good[target],
                   target=target, artifact=json.dumps(words).encode())
    for name, kind, params, threshold in CHAOS_CONTROLS:
        for target, binary in binaries.items():
            result = subprocess.run([str(x) for x in [binary, 'chaos', reference.KINDS.index(kind) + 1,
                                                       SEEDS[0], *params]], capture_output=True)
            output = result.stdout.decode().strip()
            report.add('chaos-control-' + name, output or result.stderr.decode().strip(), threshold,
                       chaos_control_ok(name, result.returncode, output), source=kind, seed=SEEDS[0],
                       target=target, count=2000000)
    return [dict(kind=kind, parameters=params, valid=valid, group='endpoint') for kind,params,valid in fixtures] + [dict(axiom=a,rules=r,depth=d,group='word') for a,r,d in words]


def scenario_reference():
    sources = [model(kind, i + 1, [1, 4, 0] if kind == 'euclid' else [])
               for i, kind in enumerate(reference.KINDS)]
    raw = [(0, 0)] * 11; published = [(0, 0)] * 11
    pending = {}; enabled = True; slew = 512
    lane_enabled, active, next_step, off = True, False, 0, 0
    output = bytearray()
    for tick in range(4096):
        if tick == 7: pending['enabled'] = False
        if tick == 20: pending['enabled'] = True
        if tick == 24: pending['reset'] = 12345
        if tick == 31: pending['slew'] = 17
        if tick == 13: lane_enabled = False
        if tick == 17: lane_enabled = True; next_step = tick
        for i, source in enumerate(sources):
            due = tick % (6 if i in (0, 6) else 1) == 0
            if i == 0 and due:
                enabled = pending.get('enabled', enabled); slew = pending.get('slew', slew)
                if 'reset' in pending:
                    source = sources[0] = model('white', pending['reset'], [])
                    raw[0] = (0, 0)
                pending.clear()
            if due and (i or enabled): raw[i] = source.update()
            if not i and not enabled: raw[i] = (0, 0)
            published[i] = tuple(reference.smooth(raw[i][axis], published[i][axis], slew if i == 0 else 0)
                                 for axis in range(2))
        events = []
        hit = published[6][0] > 0
        if active and (not lane_enabled or off <= tick or (next_step <= tick and hit)):
            events.append((7, 0, 0, 0, 0)); active = False; off = 0
        if lane_enabled and next_step <= tick:
            next_step = tick + 6
            if hit:
                cents = reference.quantize(6900 + reference.trunc0(2400 * published[1][0], 32768), 0xab5)
                events.append((7, 0, 1, cents, 32767)); active = True; off = tick + 6
        output.extend(struct.pack('<QB', tick * 64, len(events)))
        for xy in published + [(0, 0)] * 5: output.extend(struct.pack('<ii', *xy))
        for event in events: output.extend(struct.pack('<HBBiH', *event))
    return bytes(output)


def exact_profiles(binaries, profile, report):
    checks = reference.selftest()
    report.add('independent-reference', checks, 'all formula/prefix/word/rounding checks', checks > 0)
    expected_scenario = scenario_reference()
    snapshots = {}
    resumes = {}
    for target, binary in binaries.items():
        scenario = run([binary, 'scenario']).stdout
        report.add('clock-lanes-pending-slew', scenario == expected_scenario, 'exact independent 4096 ticks',
                   scenario == expected_scenario, target=target, count=4096, artifact=scenario)
        snapshots[target] = run([binary, 'checkpoint']).stdout
    for target, binary in binaries.items():
        for origin, snapshot in snapshots.items():
            resumes[target, origin] = run([binary, 'resume'], input=snapshot).stdout
    first = next(iter(resumes.values()))
    report.add('cross-build-restore', len(resumes), 'every continuation and final snapshot identical',
               all(value == first for value in resumes.values()), count=4096, artifact=first)
    literal_fixtures = []
    for kind in reference.KINDS:
        kind_id = reference.KINDS.index(kind) + 1
        panel = {}
        for seed in SEEDS:
            expected = raw_vector(kind, seed, [])
            panel[seed] = expected
            for target, binary in binaries.items():
                actual = run([binary, 'vector', kind_id, seed, 4096]).stdout
                report.add('canonical-exact-state', actual == expected, 'all4097 full recurrence records equal',
                           actual == expected, source=kind, seed=seed, target=target, count=4096, artifact=actual)
        report.add('zero-seed-alias', panel[0] == panel[0x6d2b79f5], 'intentional equality',
                   panel[0] == panel[0x6d2b79f5], source=kind)
        if kind in ('white', 'pink', 'brown', 'multi', 'logistic', 'henon'):
            distinct = []
            for seed in SEEDS[:6]:
                source = model(kind, seed, [])
                distinct.append(tuple(source.update() for _ in range(4096)))
            report.add('seed-distinction', len(set(distinct)), 'six distinct raw output vectors',
                       len(set(distinct)) == 6, source=kind)
        good = {target: True for target in binaries}
        fixtures = list(parameter_fixtures(kind, 256 if profile == 'full' else 8))
        constant = []
        for fixture_index, fixture in enumerate(fixtures):
            params, seed = fixture['parameters'], fixture['seed']
            source = model(kind, seed, params)
            tail = [source.update() for _ in range(4096)][-1024:]
            if len(set(tail)) == 1:
                constant.append(fixture_index)
            literal_fixtures.append(dict(kind=kind, **fixture))
            expected = raw_vector(kind, seed, params)
            for target, binary in binaries.items():
                actual = run([binary, 'vector', kind_id, seed, 4096] + params).stdout
                good[target] &= actual == expected
        for target, passed in good.items():
            report.add('parameter-vectors', len(fixtures), '4096 exact updates per literal fixture', passed,
                       source=kind, target=target, count=4096 * len(fixtures),
                       artifact=json.dumps(fixtures, separators=(',', ':')).encode())
        report.add('fixture-liveness', constant,
                   'reported fixture indices with constant last1024 updates',
                   status='INFO', required=False, source=kind, count=4096 * len(fixtures))
    return literal_fixtures


def quick_map_liveness(binaries, report):
    # : bounded quick diagnostic, separate from the full chaos protocol.
    for kind in ('logistic', 'henon'):
        kind_id = reference.KINDS.index(kind) + 1
        for seed in SEEDS:
            for target, binary in binaries.items():
                stream = run([binary, 'stream', kind_id, seed, 65536, 0]).stdout
                if len(stream) != 65536 * 8:
                    raise RuntimeError('incomplete quick map stream')
                values = set(struct.iter_unpack('<ii', stream[-4096 * 8:]))
                report.add('quick-map-liveness', len(values),
                           'more than one raw output pair in last4096 of65536',
                           len(values) > 1, source=kind, seed=seed, target=target,
                           count=65536, artifact=stream)


def statistical_profiles(binaries, profile, report):
    try:
        import numpy as np
    except ImportError:
        report.add('spectral-library', 'NumPy unavailable', 'NumPy for fixed Welch protocol', status='DEFERRED')
        return
    report.add('analysis-library', np.__version__, 'reported NumPy version', True)
    first_target, first_binary = next(iter(binaries.items()))
    count = 1 << (22 if profile == 'full' else 18)
    warmup = 1 << 20 if profile == 'full' else 0
    for kind, expected, tolerance in [('white', 0, .15), ('pink', -1, .15), ('brown', -2, .20)]:
        slopes = []
        for seed in SEEDS[:4]:
            actual = run([first_binary, 'stream', reference.KINDS.index(kind) + 1, seed, count, warmup])
            stream = actual.stdout
            for target, binary in binaries.items():
                if target == first_target: continue
                other = run([binary, 'stream', reference.KINDS.index(kind) + 1, seed, count, warmup]).stdout
                report.add('spectral-stream-equality', other == stream, f'exact {first_target} raw stream', other == stream,
                           source=kind, seed=seed, count=count, target=target)
            values = np.frombuffer(stream, dtype='<i4').reshape(-1, 2)[:, 0]
            if profile == 'full':
                measured, bands, lower = spectrum(values)
                report.add('spectral-slope', measured, f'{expected} +/- {tolerance}', abs(measured - expected) <= tolerance,
                           source=kind, seed=seed, count=count, artifact=np.asarray(bands, dtype='<f8').tobytes())
                if kind == 'brown':
                    report.add('lower-band-power', lower, 'reported bins1..31 below fixed fit band; positive finite powers',
                               all(math.isfinite(value) and value > 0 for value in lower),
                               source=kind, seed=seed, count=count, artifact=np.asarray(lower,dtype='<f8').tobytes())
            else:
                n = 1 << 14
                window = .5 - .5 * np.cos(2 * np.pi * np.arange(n) / (n - 1))
                powers = np.zeros(n // 2 + 1)
                for i in range(31):
                    segment = values[i*(n//2):i*(n//2)+n].astype(float)
                    segment -= np.mean(segment)
                    part = np.abs(np.fft.rfft(segment*window))**2 / np.sum(window**2)
                    part[1:-1] *= 2
                    powers += part / 31
                xs, ys = [], []
                for k in range(60):
                    bins = [j for j in range(1, n//8 + 1) if (1 << (6+k)) <= j**6 < (1 << (7+k))]
                    if bins:
                        power = float(np.mean(powers[bins]))
                        if power <= 0: raise ValueError('constant quick spectrum')
                        xs.append((-13+(k+.5)/6)*math.log10(2)); ys.append(math.log10(power))
                measured = ols(xs, ys)
                report.add('quick-slope-diagnostic', measured, 'finite diagnostic; full slope gate not evaluated',
                           math.isfinite(measured), source=kind, seed=seed, count=count)
            slopes.append(measured)
            if kind == 'brown':
                report.add('reflections', actual.stderr.decode().strip(), 'reported expected boundary events', True,
                           source=kind, seed=seed, count=count)
        report.add('slope-median', float(np.median(slopes)), 'reported; every full seed must pass individually', True, source=kind)
    for seed in SEEDS[:4]:
        values = run([first_binary, 'stream', reference.KINDS.index('multi') + 1, seed, 131071, 65536]).stdout
        x = np.frombuffer(values, dtype='<i4').reshape(-1, 2)[:, 0]
        corr = float(np.corrcoef(x[:65536], x[::2])[0, 1])
        report.add('dilation-correlation', corr, '>=0.5, fixed65536 warmup and65536 paired samples',
                   math.isfinite(corr) and corr >= .5, source='multi', seed=seed, count=65536)
    if profile == 'full':
        for kind in ('logistic', 'henon'):
            for seed in SEEDS:
                hashes = []
                for target, binary in binaries.items():
                    result = subprocess.run([str(binary), 'chaos', str(reference.KINDS.index(kind)+1), str(seed)], capture_output=True)
                    output = result.stdout.decode().strip()
                    match = re.search(r'hash=([0-9a-f]{16})', output)
                    if match: hashes.append(match.group(1))
                    report.add('exact-chaotic-profile', output or result.stderr.decode().strip(),
                               '2M full states; no gap<1M; nonconstant; zero clamps', result.returncode == 0,
                               source=kind, seed=seed, target=target, count=2000000)
                report.add('chaotic-stream-equality', hashes, 'identical canonical full-state hashes',
                           len(hashes) == len(binaries) and len(set(hashes)) == 1,
                           source=kind, seed=seed, count=2000000)
    else:
        report.add('chaotic-profile', 'quick profile does not run2M-state searches', 'full profile required',
                   status='DEFERRED', required=False)


def soak_rows(output, fs, seconds):
    """Strict output contract; compare stable rows separately from host timing."""
    groups = {name: [] for name in ('GENSOAK', 'GENSOAK_SOURCE', 'GENSOAK_MINUTE', 'GENSOAK_SNAPSHOT')}
    for line in output.splitlines():
        words = line.split()
        if not words or words[0] not in groups:
            raise ValueError('unexpected integrated soak output: ' + line)
        row = {}
        for word in words[1:]:
            key, value = word.split('=', 1)
            if key in row:
                raise ValueError('duplicate soak field: ' + key)
            row[key] = value
        groups[words[0]].append(row)
    if len(groups['GENSOAK']) != 1:
        raise ValueError('soak requires exactly one final summary')
    final = groups['GENSOAK'][0]
    pins = dict(fs=fs, samples=fs*seconds, sources=16, voices=8,
                ticks=(fs*seconds+63)//64, failures=0, clipped=0, pcm_bytes=128)
    if any(int(final[key]) != value for key, value in pins.items()):
        raise ValueError('soak summary count/bound mismatch')
    if int(final['events']) != int(final['ons'])+int(final['offs']):
        raise ValueError('soak event accounting mismatch')
    if not all(int(final[key]) > 0 for key in ('ons', 'offs', 'resting_lane_ticks', 'retriggers', 'resets')):
        raise ValueError('soak lacks required lifecycle coverage')
    if not 0 < int(final['peak']) < 32768 or not re.fullmatch('[0-9a-f]{16}', final['hash']):
        raise ValueError('soak hash/peak malformed')
    if final['timer'] != 'process_cpu' or int(final['clocks_per_second']) <= 0:
        raise ValueError('soak timer contract differs')
    for key in ('tick_mean_us', 'tick_max_us', 'init_us', 'restore_max_us'):
        if not math.isfinite(float(final[key])) or float(final[key]) < 0:
            raise ValueError('nonfinite or negative CPU timer')
    sources = groups['GENSOAK_SOURCE']
    if [int(row['id']) for row in sources] != list(range(16)) or set(int(row['kind']) for row in sources) != set(range(1, 12)):
        raise ValueError('soak must exercise16 sources and every kind')
    for row in sources:
        if int(row['updates']) <= 0:
            raise ValueError('source never updated')
        for axis in ('x','y'):
            if not -32768 <= int(row[axis+'_min']) <= int(row[axis+'_max']) <= 32767:
                raise ValueError('source outside Q15 bounds')
        if int(row['kind']) == 3:
            if seconds >= 120 and int(row['counter']) <= 0:
                raise ValueError('Brownian reflection stress was not exercised')
        elif int(row['counter']) != 0:
            raise ValueError('unexpected recurrence clamp count')
    minutes = groups['GENSOAK_MINUTE']
    if [int(row['minute']) for row in minutes] != list(range(1, seconds//60+1)):
        raise ValueError('missing, duplicate or unordered minute hash')
    if any(not re.fullmatch('[0-9a-f]{16}', row['hash']) for row in minutes):
        raise ValueError('malformed minute hash')
    snapshots = groups['GENSOAK_SNAPSHOT']
    expected = [((second*fs+63)//64)*64 for second in (0,1,61,601,1801,3599) if second < seconds]
    if [int(row['frame']) for row in snapshots] != expected or int(final['snapshots']) != len(expected):
        raise ValueError('snapshot schedule differs')
    if any(row['match'] != 'yes' or int(row['continuation_ticks']) != 64 or int(row['bytes']) <= 0 for row in snapshots):
        raise ValueError('snapshot continuation failed')
    stable_final = {k:v for k,v in final.items() if k not in ('clocks_per_second','tick_mean_us','tick_max_us','init_us','restore_max_us')}
    return dict(summary=stable_final, sources=sources, minutes=minutes, snapshots=snapshots), final


def soak_timeline(text, seconds):
    """Use the one pinned scenario; truncate only its duration/events for preflight."""
    if seconds == 3600:
        return text
    rows = []
    for line in text.splitlines():
        if line.startswith('hash '):
            continue
        if line.startswith('length_ns '):
            line = f'length_ns {seconds*1000000000}'
        if line and line[0].isdigit() and int(line.split()[0]) >= seconds*1000000000:
            continue
        rows.append(line)
    return '\n'.join(rows)+'\n'


def measured_child(command, log):
    """wait4 supplies this child's RSS, not a cumulative prior child maximum."""
    started = time.monotonic()
    with log.open('wb') as stream:
        child = subprocess.Popen([str(x) for x in command], stdout=stream, stderr=subprocess.STDOUT)
        _, status, usage = os.wait4(child.pid, 0)
        child.returncode = os.waitstatus_to_exitcode(status)
    measured = dict(wall_seconds=time.monotonic()-started, user_seconds=usage.ru_utime,
                    system_seconds=usage.ru_stime,
                    peak_rss_bytes=int(usage.ru_maxrss)*(1 if sys.platform == 'darwin' else 1024),
                    log_bytes=log.stat().st_size)
    if child.returncode:
        raise RuntimeError(f'{command[0]} exited{child.returncode}: {log.read_text()[-4000:]}')
    return log.read_text(), measured


def integrated_soak(directory, report, seconds=3600):
    """Explicit heavy-host task: no WAV, bounded PCM, measured projections first."""
    here = Path(__file__).resolve().parents[2] / 'src/gen'
    spike = here.parent.parent
    if seconds != 3600:
        raise ValueError('integrated acceptance requires3600 seconds')
    available = shutil.disk_usage(directory).free
    report.add('soak-disk-preflight', available, 'at least2GiB free temporary disk', available >= 2*1024**3)
    if available < 2*1024**3:
        raise RuntimeError('insufficient temporary disk for full integrated soak')
    binaries = {}
    for target in ('host','portable','ubsan'):
        build = directory/f'audio-{target}'
        run(['make','-s','-C',spike,f'B={build}',*SOAK_MAKE,*SOAK_TARGET_MAKE[target]])
        binaries[target] = build/'harness'
    stable = {}
    for fs in (48000,44100):
        original = (spike/f'tests/golden/gen/soak/gensoak-{fs}.tl').read_text()
        expected_hash = re.search(r'^hash ([0-9a-f]{16})$',original,re.M).group(1)
        short = directory/f'soak-{fs}-2.tl';short.write_text(soak_timeline(original,2))
        preflight = directory/f'soak-{fs}-120.tl';preflight.write_text(soak_timeline(original,120))
        # UBSan voices, cache setters and bank adapter before any hour render.
        result = run([binaries['ubsan'],'--fs',fs,'--check','lfo']).stdout.decode()
        report.add('soak-ubsan-stress',result.splitlines()[-1],'CHECK lfo ok',result.endswith('CHECK lfo ok\n'),target='ubsan',artifact=result.encode())
        for target,binary in binaries.items():
            output, measured = measured_child([binary,'--gen-soak',short],directory/f'{target}-{fs}-short.log')
            short_rows,_ = soak_rows(output,fs,2)
            played = run([binary,'--play',short]).stdout.decode()
            match = re.search(r' hash=([0-9a-f]{16}) ',played)
            report.add('soak-stream-equivalence',short_rows['summary']['hash'],'ordinary --play exact hash',bool(match) and match.group(1)==short_rows['summary']['hash'],target=target,artifact=output.encode())
            output, measured = measured_child([binary,'--gen-soak',preflight],directory/f'{target}-{fs}-preflight.log')
            soak_rows(output,fs,120)
            if measured['peak_rss_bytes'] > 512*1024**2:
                raise RuntimeError('soak preflight exceeds512MiB RSS')
            measured['projected_hour_wall_seconds'] = measured['wall_seconds']*30
            report.add('soak-measured-projection',measured,'120s measured before3600s; RSS<=512MiB',measured['peak_rss_bytes']<=512*1024**2,target=target,artifact=output.encode())
        for target,binary in binaries.items():
            merged = directory/f'{target}-{fs}-canonical.tl'
            output, measured = measured_child([binary,'--gen-soak',spike/f'tests/golden/gen/soak/gensoak-{fs}.tl','--merged',merged],directory/f'{target}-{fs}-hour.log')
            rows, timings = soak_rows(output,fs,seconds)
            stable[fs,target] = rows
            measured['disk_bytes'] = sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())
            if measured['peak_rss_bytes'] > 512*1024**2 or measured['disk_bytes'] > 2*1024**3:
                raise RuntimeError('soak exceeds declared RSS/temporary-disk bounds')
            report.add('integrated-hour-soak',dict(rows=rows,timing=timings,resources=measured),
                       '60minute hashes,6snapshots,all source/lifecycle bounds,canonical pinned whole hash',
                       rows['summary']['hash']==expected_hash and merged.read_text()==original,
                       target=target,count=fs*seconds,artifact=output.encode())
            report.add('soak-resource-bounds',measured,'RSS<=512MiB; temporary files<=2GiB',
                       measured['peak_rss_bytes']<=512*1024**2 and measured['disk_bytes']<=2*1024**3,target=target)
        report.add('soak-backend-equality',fs,'native/portable/UBSan minute,whole,source,event,snapshot records equal',
                   stable[fs,'host']==stable[fs,'portable']==stable[fs,'ubsan'],count=fs*seconds)
    # These deliberate output defects must be refused by the acceptance reader.
    good = output
    defects = [re.sub(r'^GENSOAK_MINUTE minute=1 .*\n','',good,flags=re.M),
               good.replace('failures=0','failures=1',1),
               good.replace('sources=16','sources=15',1),
               good.replace('match=yes','match=no',1)]
    for i,bad in enumerate(defects):
        refused = False
        try: soak_rows(bad,44100,seconds)
        except (ValueError,KeyError): refused=True
        report.add(f'soak-output-control-{i}',refused,'altered output refused',refused)


def revision(root):
    """HEAD and whether tracked files differ from it, or None when root is
    not itself a git work tree (an export, or a copy inside another tree)."""
    git = ['git', '-C', str(root)]
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')  # status never writes the index
    try:
        top = subprocess.run(git + ['rev-parse', '--show-toplevel'], capture_output=True, env=env)
        if top.returncode or Path(top.stdout.decode().strip()).resolve() != root:
            return None
        head = subprocess.run(git + ['rev-parse', 'HEAD'], capture_output=True, env=env, check=True)
        status = subprocess.run(git + ['status', '--porcelain', '--untracked-files=no'],
                                capture_output=True, env=env, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return dict(commit=head.stdout.decode().strip(), dirty=bool(status.stdout.strip()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('quick', 'full'), default='quick')
    parser.add_argument('--target', choices=('host', 'portable', 'ubsan', 'all'), default='all')
    parser.add_argument('--detectors', action='store_true')
    parser.add_argument('--json', type=Path)
    parser.add_argument('--soak-seconds', type=int)
    args = parser.parse_args(argv)
    if args.soak_seconds is not None and (args.detectors or args.soak_seconds != 3600 or args.profile != 'full' or args.target != 'all'):
        parser.error('the3600-second soak requires --profile full --target all')
    report = Report()
    started = time.monotonic()
    fixtures = []
    detector_tests(report)
    if not args.detectors:
        if not (Path(__file__).resolve().parents[2] / 'src/gen/gen.c').exists():
            for source in KINDS:
                report.add('production-exact-vectors', 'no production implementation',
                           '4096 exact updates and state checkpoints', status='UNIMPLEMENTED', source=source)
        else:
            try:
                with tempfile.TemporaryDirectory(prefix='intfm-gen-') as temporary:
                    binaries = build_targets(Path(temporary), args.target, report, args.profile == 'full')
                    fixtures = exact_profiles(binaries, args.profile, report)
                    fixtures += edge_profiles(binaries, args.profile, report)
                    if args.profile == 'quick':
                        quick_map_liveness(binaries, report)
                    statistical_profiles(binaries, args.profile, report)
            except (RuntimeError, ValueError, OSError) as error:
                report.add('suite-execution', str(error), 'complete requested profile', False)
        if args.soak_seconds:
            try:
                with tempfile.TemporaryDirectory(prefix='intfm-soak-') as temporary:
                    integrated_soak(Path(temporary), report, args.soak_seconds)
            except (RuntimeError, ValueError, OSError, KeyError) as error:
                report.add('integrated-hour-soak', str(error), 'complete requested integration', False)
    if args.json:
        root = Path(__file__).resolve().parents[2]
        data = dict(schema=1, profile='detectors' if args.detectors else args.profile,
                    target=args.target, revision=revision(root), platform=platform.platform(),
                    python=sys.version, seeds=SEEDS, spectral_protocol=PROTOCOL,
                    source_sha256={path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in Path(__file__).parent.iterdir() if path.suffix in ('.c','.h','.py','.def')},
                    compiler=shlex.split(os.environ.get('CC','cc')),
                    compiler_version=run(shlex.split(os.environ.get('CC','cc'))+['--version']).stdout.decode().splitlines()[0],
                    elapsed_seconds=time.monotonic()-started, fixtures=fixtures, rows=report.rows)
        if not args.detectors:
            built = tuple(TARGET_FLAGS) if args.target == 'all' else (args.target,)
            data['compile_flags'] = {target: [*COMMON_FLAGS, *TARGET_FLAGS[target]] for target in built}
            data['library_flags'] = ['-mgeneral-regs-only']
        if args.soak_seconds:
            data['integration_make'] = {target: [*SOAK_MAKE, *knobs] for target, knobs in SOAK_TARGET_MAKE.items()}
            # The soak's make passes no FM_CTL, so its harnesses keep
            # fm_a1.h's default; --check lfo and gen_clock refuse any other.
            header = (root/'src/host/fm_a1.h').read_text()
            data['fm_ctl'] = int(re.search(r'^#define FM_CTL (\d+)', header, re.M).group(1))
            paths =['src/host/harness.c','src/host/fm_a1.c','src/host/fm_a1.h','src/host/ref_double.c','src/host/ref_double.h','Makefile','vendor/fp_math.h']
            paths += [f'tests/golden/gen/soak/gensoak-{fs}.tl' for fs in (48000,44100)]
            data['integration_sha256'] = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in paths}
        args.json.write_text(json.dumps(data, indent=2) + '\n')
    good = report.complete()
    print(f"GEN {'detectors ' if args.detectors else ''}{'ok' if good else 'incomplete'} "
          f"({len(report.rows)} rows)")
    return 0 if good else 1


if __name__ == '__main__':
    sys.exit(main())
