#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Independent exact-integer reference for design-generative.md sections 4–5.

This host model has no dependency on the C implementation. Source.update
performs one raw update; session clocks, DIV, smoothing and lane ownership
are separate from the recurrence. Seeds are required. All scalar defaults
below come from the design, with signed division explicitly distinguished
from floor division.

State-vector encoding (independent of the production snapshot format):
all integers little endian; prefix u8 kind, u32 PRNG word, i32 raw X/Y.
Payload by kind: white empty; pink u16 counter, i32 sum, 16 i16 rows;
brown i64 position, u64 reflections; multi u8 layer count, that many u32
positions, 256 i16 motif values; logistic i64 X; henon i64 X/Y, u64 clamps;
sequences u32 next step, u32 length, u32 effective rotation, then, only for
fibonacci/lsystem, length ASCII symbols. Immutable scalar parameters are
the separately named vector profile. Vectors include state at initialization
and after each raw update. This encoding includes exact map state, not just
the rounded output. FNV-1a-64 hashes the concatenated state records.
"""

import argparse
import struct

U32 = (1 << 32) - 1
ONE = 1 << 62
Q48 = 1 << 48
ZERO_SEED = 0x6D2B79F5
SEEDS = (1, 2, 3, 0x12345678, 0x9E3779B9, U32, 0, ZERO_SEED)
KINDS = ("white", "pink", "brown", "multi", "logistic", "henon",
         "euclid", "thue", "cantor", "fibonacci", "lsystem")
CHECKPOINTS = (0, 1, 63, 64, 65, 4095, 4096)


def bounded(value, lo, hi, field):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError("%s must be an integer in [%d,%d]" % (field, lo, hi))
    return value


def trunc0(n, d):
    """Exact C99 signed division for a positive divisor."""
    if d <= 0:
        raise ValueError("divisor must be positive")
    return n // d if n >= 0 else -((-n) // d)


def clamp_q15(value):
    return max(-32768, min(32767, value))


def normalize_seed(seed):
    return bounded(seed, 0, U32, "seed") or ZERO_SEED


def xorshift(word):
    word ^= (word << 13) & U32
    word ^= word >> 17
    return (word ^ (word << 5)) & U32


def q15(word):
    return (word >> 16) - 32768


def smooth(raw, published, slew):
    bounded(slew, 0, 512, "slew")
    return raw if not slew else published + max(-slew, min(slew, raw - published))


def quantize(cents, mask):
    """Greatest enabled semitone not above cents; mask zero is bypass."""
    bounded(mask, 0, 4095, "mask")
    if not mask:
        return cents
    semitone = cents // 100
    for _ in range(12):
        if mask & (1 << (semitone % 12)):
            return semitone * 100
        semitone -= 1
    raise AssertionError("nonzero mask has an enabled pitch class")


def expand_word(axiom, rules, depth):
    """Expand A..H rules, checking every intermediate generation's cap."""
    bounded(depth, 0, 16, "depth")
    if not isinstance(axiom, str) or not 1 <= len(axiom) <= 32:
        raise ValueError("axiom length must be 1..32")
    if any(c not in "ABCDEFGH" for c in axiom):
        raise ValueError("invalid axiom symbol")
    if not isinstance(rules, dict) or len(rules) > 8:
        raise ValueError("rules must be a dictionary with at most eight entries")
    clean = {}
    for symbol, word in rules.items():
        if not isinstance(symbol, str) or len(symbol) != 1 or symbol not in "ABCDEFGH":
            raise ValueError("invalid rule symbol")
        if not isinstance(word, str):
            raise ValueError("rule word must be text")
        word = "" if word == "-" else word
        if len(word) > 32 or any(c not in "ABCDEFGH" for c in word):
            raise ValueError("invalid rule word")
        clean[symbol] = word
    word = axiom
    for _ in range(depth):
        length = sum(len(clean.get(c, c)) for c in word)
        if length > 16384:
            raise ValueError("intermediate word exceeds 16384 symbols")
        word = "".join(clean.get(c, c) for c in word)
    if not word:
        raise ValueError("empty final word")
    return word


def fibonacci_word(depth=12):
    return expand_word("A", {"A": "AB", "B": "A"}, depth)


class Source:
    """One independent raw source, with explicit exact recurrence state."""

    def __init__(self, kind, seed, **params):
        if kind not in KINDS:
            raise ValueError("unknown source kind")
        self.kind = kind
        self.seed = normalize_seed(seed)
        self.word = self.seed
        self.raw = (0, 0)
        self.params = dict(params)

        def param(name, default, lo, hi):
            return bounded(self.params.pop(name, default), lo, hi, name)

        if kind == "pink":
            self.rows = [q15(self.draw()) for _ in range(16)]
            self.total = sum(self.rows)
            self.counter = 0
        elif kind == "brown":
            self.step_q15 = param("step_q15", 16, 1, 4096)
            self.x = (self.draw() - (1 << 31)) * (1 << 31)
            self.reflections = 0
        elif kind == "multi":
            p = param("p", 256, 2, 65536)
            k = param("k", 2, 2, 16)
            octaves = param("octaves", 6, 1, 8)
            gain = param("gain_q15", 24576, 0, 32768)
            self.periods = [p * k ** i for i in range(octaves)]
            if self.periods[-1] > U32:
                raise ValueError("largest period exceeds UINT32_MAX")
            self.weights = [32768]
            for _ in range(1, octaves):
                self.weights.append(self.weights[-1] * gain // 32768)
            self.positions = [0] * octaves
            self.motif = [q15(self.draw()) for _ in range(256)]
        elif kind == "logistic":
            self.r_q48 = param("r_q48", (399 * Q48 + 50) // 100, 0, 4 * Q48)
            first, second = self.draw(), self.draw()
            self.x = 1 + ((first << 32) | second) % (ONE - 1)
        elif kind == "henon":
            self.a_q48 = param("a_q48", (7 * Q48 + 2) // 5, 0, 2 * Q48)
            self.b_q48 = param("b_q48", (3 * Q48 + 5) // 10, -Q48, Q48)
            self.x = trunc0(q15(self.draw()) * Q48, 327680)
            self.y = self.clamps = 0
        elif kind in ("euclid", "thue", "cantor", "fibonacci", "lsystem"):
            if kind == "euclid":
                self.length = param("n", 8, 1, 65535)
                self.k = param("k", 3, 0, self.length)
            elif kind in ("thue", "cantor"):
                self.length = param("length", 256, 1, 65535)
            elif kind == "fibonacci":
                self.text = fibonacci_word(param("depth", 12, 0, 16))
                self.length = len(self.text)
            else:
                depth = param("depth", 0, 0, 16)
                if "axiom" not in self.params:
                    raise ValueError("lsystem requires an axiom")
                self.text = expand_word(self.params.pop("axiom"),
                                        self.params.pop("rules", {}), depth)
                self.length = len(self.text)
            rot = param("rot", 0, 0, self.length - 1)
            self.rotation = (self.seed % self.length + rot) % self.length
            self.step = 0
        if self.params:
            raise ValueError("unknown parameter: " + next(iter(self.params)))

    def draw(self):
        self.word = xorshift(self.word)
        return self.word

    def update(self, parent=32767):
        kind, y = self.kind, 0
        if kind == "white":
            x = q15(self.draw())
        elif kind == "pink":
            self.counter = (self.counter + 1) % 65536
            if self.counter:
                row = (self.counter & -self.counter).bit_length() - 1
                value = q15(self.draw())
                self.total += value - self.rows[row]
                self.rows[row] = value
            x = trunc0(self.total + q15(self.draw()), 17)
        elif kind == "brown":
            first, second = self.draw(), self.draw()
            delta = ((first >> 16) - (second >> 16)) * self.step_q15 * (1 << 31)
            z = self.x + delta
            if z > ONE:
                self.x = 2 * ONE - z
                self.reflections += 1
            elif z < -ONE:
                self.x = -2 * ONE - z
                self.reflections += 1
            else:
                self.x = z
            x = clamp_q15(trunc0(self.x, 1 << 47))
        elif kind == "multi":
            weighted = 0
            for i, period in enumerate(self.periods):
                index, remainder = divmod(256 * self.positions[i], period)
                a, b = self.motif[index], self.motif[(index + 1) % 256]
                value = a + trunc0((b - a) * remainder, period)
                weighted += value * self.weights[i]
                self.positions[i] = (self.positions[i] + 1) % period
            x = trunc0(weighted, sum(self.weights))
        elif kind == "logistic":
            u = self.x * (ONE - self.x) // ONE
            self.x = self.r_q48 * u // Q48
            x = clamp_q15(self.x // (1 << 46) - 32768)
        elif kind == "henon":
            square = self.x * self.x // Q48
            nx = Q48 - self.a_q48 * square // Q48 + self.y
            ny = self.b_q48 * self.x // Q48
            self.clamps += int(abs(nx) > 4 * Q48) + int(abs(ny) > 4 * Q48)
            self.x = max(-4 * Q48, min(4 * Q48, nx))
            self.y = max(-4 * Q48, min(4 * Q48, ny))
            x = clamp_q15(trunc0(self.x, 1 << 35))
            y = clamp_q15(trunc0(self.y, 1 << 35))
        else:
            j = (self.step + self.rotation) % self.length
            if kind == "euclid":
                hit = ((j + 1) * self.k // self.length - j * self.k // self.length)
                x = 32767 if hit and parent > 0 else 0
            elif kind == "thue":
                x = 32767 * (bin(j).count("1") % 2)
            elif kind == "cantor":
                hit = True
                while j:
                    j, digit = divmod(j, 3)
                    if digit == 1:
                        hit = False
                        break
                x = 32767 if hit else 0
            elif kind == "fibonacci":
                x = 32767 if self.text[j] == "B" else 0
            else:
                x = (ord(self.text[j]) - ord("A")) * 65535 // 7 - 32768
            self.step = (self.step + 1) % self.length
        self.raw = (x, y)
        return self.raw

    def state(self):
        """Named exact checkpoint; lists become immutable tuples."""
        result = {"word": self.word, "raw": self.raw}
        fields = {
            "white": (), "pink": ("counter", "total", "rows"),
            "brown": ("x", "reflections"), "multi": ("positions", "motif"),
            "logistic": ("x",), "henon": ("x", "y", "clamps"),
        }.get(self.kind, ("step", "length", "rotation"))
        for field in fields:
            value = getattr(self, field)
            result[field] = tuple(value) if isinstance(value, list) else value
        if self.kind in ("fibonacci", "lsystem"):
            result["text"] = self.text
        return result

    def state_bytes(self):
        result = struct.pack("<BIii", KINDS.index(self.kind) + 1, self.word, *self.raw)
        if self.kind == "pink":
            result += struct.pack("<Hi16h", self.counter, self.total, *self.rows)
        elif self.kind == "brown":
            result += struct.pack("<qQ", self.x, self.reflections)
        elif self.kind == "multi":
            result += struct.pack("<B", len(self.positions))
            result += struct.pack("<%dI256h" % len(self.positions),
                                  *self.positions, *self.motif)
        elif self.kind == "logistic":
            result += struct.pack("<q", self.x)
        elif self.kind == "henon":
            result += struct.pack("<qqQ", self.x, self.y, self.clamps)
        elif self.kind != "white":
            result += struct.pack("<III", self.step, self.length, self.rotation)
            if self.kind in ("fibonacci", "lsystem"):
                result += self.text.encode("ascii")
        return result


def fnv(data, value=0xCBF29CE484222325):
    for byte in data:
        value = ((value ^ byte) * 0x100000001B3) & ((1 << 64) - 1)
    return value


def vector_hash(kind, seed=1, count=4096, **params):
    source = Source(kind, seed, **params)
    value = fnv(source.state_bytes())
    for _ in range(count):
        source.update()
        value = fnv(source.state_bytes(), value)
    return "%016x" % value


# Filled only from this arbitrary-precision model after its formula checks.
CANONICAL_HASHES = {
    "white": "0fb0c86236259fef", "pink": "8eac794cb20621df",
    "brown": "306d48e8179bdccd", "multi": "8b684e70ce4b6374",
    "logistic": "205097aa2dae4243", "henon": "34b713781cab9d7d",
    "euclid": "3d31742d1dc3a3de", "thue": "60c193989351d400",
    "cantor": "e7bf41dfb127e15b", "fibonacci": "7f6dbb32bd6d9120",
    "lsystem": "4a6639cf770603f7",
}


def selftest():
    checks = 0

    def check(condition, label):
        nonlocal checks
        checks += 1
        if not condition:
            raise AssertionError(label)

    word = 1
    for expected in (270369, 67634689, 2647435461, 307599695, 2398689233):
        word = xorshift(word)
        check(word == expected, "xorshift independent prefix")
    check(trunc0(-7, 3) == -2 and -7 // 3 == -3, "signed rounding")
    check(q15(0) == -32768 and q15(U32) == 32767, "Q15 endpoints")
    check(fnv(b"hello") == 0xA430D84680AABD0B, "FNV standard vector")
    check(normalize_seed(0) == ZERO_SEED, "zero normalization")
    for kind, params, prefix in (
            ("euclid", {"n": 8, "k": 3, "rot": 7}, "00100101"),
            ("thue", {"length": 16, "rot": 15}, "0110100110010110"),
            ("cantor", {"length": 27, "rot": 26}, "101000101000000000101000101"),
            ("fibonacci", {"depth": 6, "rot": 20}, "0100101001001")):
        source = Source(kind, 1, **params)
        actual = "".join("1" if source.update()[0] else "0" for _ in prefix)
        check(actual == prefix, kind + " independently specified prefix")
    check(expand_word("ABCDEFGH", {}, 16) == "ABCDEFGH", "missing rules copy")
    check(expand_word("AB", {"A": "-"}, 1) == "B", "rule deletion")
    check(len(expand_word("A", {"A": "AA"}, 14)) == 16384, "exact word cap")
    for thunk in (lambda: expand_word("A", {"A": "AA"}, 15),
                  lambda: expand_word("AB", {"A": "AA"}, 14),
                  lambda: expand_word("A", {"A": "-"}, 1),
                  lambda: expand_word("I", {}, 0),
                  lambda: expand_word("A", {"A": "B" * 33}, 1),
                  lambda: expand_word("A", {"A": "B" * 32, "B": "B" * 32}, 3),
                  lambda: Source("multi", 1, p=65536, k=16, octaves=8),
                  lambda: Source("brown", 1, step_q15=0),
                  lambda: Source("logistic", 1, r_q48=4 * Q48 + 1)):
        try:
            thunk()
        except ValueError:
            check(True, "invalid input refused")
        else:
            check(False, "invalid input accepted")
    for cents in range(-4900, 14501, 37):
        for mask in (1, 0x800, 0xAB5, 4095):
            snapped = quantize(cents, mask)
            check(0 <= cents - snapped <= 1199 and
                  bool(mask & (1 << ((snapped // 100) % 12))), "scale down/bounds")
    check(smooth(-32768, 0, 512) == -512 and smooth(32767, 0, 512) == 512,
          "startup slew")
    pink = Source("pink", 1)
    pink.counter = 65535
    before = pink.word
    pink.update()
    check(pink.word == xorshift(before) and pink.counter == 0, "pink wrap one draw")
    brown = Source("brown", 1, step_q15=4096)
    brown.x, brown.word = ONE, 67634689
    brown.update()
    delta = ((2647435461 >> 16) - (307599695 >> 16)) * 4096 * (1 << 31)
    check(brown.x == ONE - delta and brown.reflections == 1, "upper reflection")
    brown.x, brown.word = -ONE, 1
    brown.update()
    delta = ((270369 >> 16) - (67634689 >> 16)) * 4096 * (1 << 31)
    check(brown.x == -ONE - delta and brown.reflections == 2, "lower reflection")
    logistic = Source("logistic", 1, r_q48=4 * Q48)
    logistic.x = ONE // 2
    check(logistic.update()[0] == 32767 and logistic.x == ONE,
          "logistic exact maximum")
    check(logistic.update()[0] == -32768 and logistic.x == 0,
          "logistic endpoint fixed point")
    logistic.r_q48, logistic.x = Q48 + 1, ONE // 2 + 1
    logistic.update()
    check(logistic.x == ONE // 4 + 4094, "logistic two separate floors")
    henon = Source("henon", 1, a_q48=2 * Q48, b_q48=-Q48)
    henon.x, henon.y = 4 * Q48, -4 * Q48
    henon.update()
    check(henon.x == -4 * Q48 and henon.y == -4 * Q48 and henon.clamps == 1,
          "henon simultaneous old state and per-coordinate clamp")
    henon.a_q48, henon.b_q48, henon.x, henon.y = 0, Q48 // 2, -1, 0
    henon.update()
    check(henon.y == -1 and henon.x == Q48, "negative product rounds down")
    euclid = Source("euclid", 1)
    euclid.update(parent=0)
    check(euclid.raw == (0, 0) and euclid.step == 1, "gated child still advances")
    for kind, expected in CANONICAL_HASHES.items():
        params = {"axiom": "AB", "rules": {"A": "AB", "B": "A"}, "depth": 5} \
            if kind == "lsystem" else {}
        check(vector_hash(kind, **params) == expected, kind + " 4096-state hash")
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hashes", action="store_true", help="print canonical vector hashes")
    args = parser.parse_args()
    count = selftest()
    print("REFERENCE ok %d checks" % count)
    if args.hashes:
        for kind in KINDS:
            params = {"axiom": "AB", "rules": {"A": "AB", "B": "A"}, "depth": 5} \
                if kind == "lsystem" else {}
            print(kind, vector_hash(kind, **params))


if __name__ == "__main__":
    main()
