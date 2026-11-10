#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Portable TL framing/version guards and byte/PCM identity through native APIs.

Build the ordinary harness first. Set HARNESS or NATIVE_LIBRARY to select an
explicit build. The bridge check links a small consumer when its archive exists.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(os.environ.get("HARNESS", ROOT / "build/s12e10p1v1/harness")).resolve()
NATIVE = Path(os.environ.get("NATIVE_LIBRARY", ROOT / "build/native/libjottune_native.a")).resolve()
HEADER = b'# jottune-tl 1 1\n# {"title":"Portable test","made_by":"agent"}\n# end-jottune-tl\n'
BODY = (ROOT / "tests/golden/48000/sine.tl").read_bytes()

PROBE = r'''#include "jottune_bridge.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int save(fms_player_t *p, const char *path, int body)
{
    size_t n = body ? fms_player_record_body_text(p, NULL, 0) : fms_player_record_text(p, NULL, 0);
    char *text = n ? (char *)malloc(n) : NULL;
    FILE *f;
    char untouched = 'x';
    if (!text) return 1;
    if ((body ? fms_player_record_body_text(p, &untouched, 1) : fms_player_record_text(p, &untouched, 1)) != n || untouched != 'x') return 2;
    if ((body ? fms_player_record_body_text(p, text, n) : fms_player_record_text(p, text, n)) != n) return 3;
    f = fopen(path, "wb");
    if (!f) return 4;
    if (fwrite(text, 1, n - 1, f) != n - 1 || fclose(f)) return 5;
    free(text);
    return 0;
}

int main(int argc, char **argv)
{
    FILE *f;
    long length;
    char *text, err[FMS_ERR_LEN];
    int16_t pcm[256 * FMS_MAX_CH];
    uint32_t written;
    fms_player_t *p = NULL, *derived = NULL;
    int rc;
    if (argc != 4) return 2;
    f = fopen(argv[1], "rb");
    if (!f || fseek(f, 0, SEEK_END) || (length = ftell(f)) < 0 || fseek(f, 0, SEEK_SET)) return 2;
    text = (char *)malloc((size_t)length + 1);
    if (!text || fread(text, 1, (size_t)length, f) != (size_t)length || fclose(f)) return 2;
    rc = fms_player_import(text, (size_t)length, NULL, &p, err, sizeof err);
    /* The complete imported header must own its bytes after import returns. */
    memset(text, 'x', (size_t)length); free(text);
    if (rc) { fprintf(stderr, "%s\n", err); return rc; }
    if (fms_player_record_text(p, NULL, 0) || fms_player_record_body_text(p, NULL, 0)) return 8;
    do {
        rc = fms_player_render_checked(p, pcm, 256, NULL, &written, err, sizeof err);
        if (rc) { fprintf(stderr, "%s\n", err); fms_player_close(p); return rc; }
    } while (written);
    printf("BRIDGE hash=%016llx\n", (unsigned long long)fms_player_hash(p));
    rc = save(p, argv[2], 0) || save(p, argv[3], 1);
    if (!rc) rc = fms_player_keys(p, 60, 0, 1, NULL, &derived, err, sizeof err);
    fms_player_close(p);
    if (!rc && derived) {
        size_t n;
        char *derived_text;
        do {
            rc = fms_player_render_checked(derived, pcm, 256, NULL, &written, err, sizeof err);
        } while (!rc && written);
        n = fms_player_record_text(derived, NULL, 0);
        derived_text = n ? (char *)malloc(n) : NULL;
        if (!derived_text) rc = 9;
        else {
            if (fms_player_record_text(derived, derived_text, n) != n || strncmp(derived_text, "timeline ", 9)) rc = 10;
            free(derived_text);
        }
    }
    fms_player_close(derived);
    return rc;
}
'''


def invoke(*args):
    return subprocess.run([str(HARNESS), *map(str, args)], capture_output=True, timeout=30)


def fill(data, target):
    """Pad valid native text with ordinary comments to an exact byte boundary."""
    while len(data) < target:
        n = min(1023, target - len(data))
        data += (b"#" + b"x" * (n - 2) + b"\n") if n >= 2 else b"\n"
    return data


class NativeDocument(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jottune-document-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "source.tl"

    def export(self, data, wav=False):
        self.source.write_bytes(data)
        full = self.directory / "full.tl"
        body = self.directory / "body.tl"
        args = ["--play", self.source, "--merged", full, "--merged-body", body]
        if wav:
            args += ["--wav", self.directory / "out.wav"]
        result = invoke(*args)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn(b"match=yes block_indep=yes", result.stdout)
        return full.read_bytes(), body.read_bytes(), result.stdout

    def refuse(self, data, needle=None):
        self.source.write_bytes(data)
        full = self.directory / "refused.tl"
        full.write_bytes(b"preserved destination")
        result = invoke("--play", self.source, "--merged", full)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"PLAY ", result.stdout)
        self.assertEqual(full.read_bytes(), b"preserved destination")
        if needle:
            self.assertIn(needle, result.stderr)

    def test_version_discovery(self):
        result = invoke("--tl-format")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"TL_FORMAT tl_version=1 engine_version=1\n")

    def test_legacy_and_document_have_identical_body_and_pcm(self):
        full, body, legacy = self.export(BODY, wav=True)
        pcm = (self.directory / "out.wav").read_bytes()
        self.assertEqual(full, BODY)
        self.assertEqual(body, BODY)
        full, body, portable = self.export(HEADER + BODY, wav=True)
        self.assertEqual(full, HEADER + BODY)
        self.assertEqual(body, BODY)
        self.assertEqual((self.directory / "out.wav").read_bytes(), pcm)
        self.assertEqual(re.search(rb" hash=([a-f0-9]{16})", legacy)[1],
                         re.search(rb" hash=([a-f0-9]{16})", portable)[1])
        self.assertEqual(hashlib.sha256(body).digest(), hashlib.sha256(BODY).digest())
        self.assertNotEqual(hashlib.sha256(full).digest(), hashlib.sha256(body).digest())

    def test_crlf_header_preserved_and_body_canonical(self):
        header = HEADER.replace(b"\n", b"\r\n")
        full, body, _ = self.export(header + BODY.replace(b"\n", b"\r\n"))
        self.assertEqual(full, header + BODY)
        self.assertEqual(body, BODY)

    def test_metadata_only_changes_keep_body_identity(self):
        full, body, _ = self.export(HEADER.replace(b"Portable test", b"A changed title") + BODY)
        self.assertEqual(body, BODY)
        self.assertIn(b"A changed title", full)

    def test_future_overflow_and_malformed_versions_refused_before_render(self):
        for versions in (b"2 1", b"1 2", b"4294967295 1", b"1 4294967295",
                         b"4294967296 1", b"1 4294967296", b"99999999999999999999999 1",
                         b"0 1", b"1 0", b"01 1", b"1 01", b"-1 1", b"+1 1",
                         b"1.0 1", b"1 1 ", b"1  1", b"1", b"1\t1"):
            with self.subTest(versions=versions):
                self.refuse(HEADER.replace(b"1 1", versions, 1) + BODY)

    def test_reserved_markers_and_framing_refused(self):
        malformed = [b"\n" + HEADER + BODY, BODY + HEADER,
                     HEADER + HEADER + BODY, HEADER.replace(b"# jottune-tl", b"#jottune-tl", 1) + BODY,
                     HEADER.replace(b"# jottune-tl", b" # jottune-tl", 1) + BODY,
                     HEADER.replace(b"# jottune-tl", b"\r# jottune-tl", 1) + BODY,
                     HEADER.replace(b"# end-jottune-tl\n", b"") + BODY,
                     HEADER.replace(b'# {"title"', b'{"title"') + BODY,
                     HEADER.replace(b"# end-jottune-tl", b"# end-jottune-tl trailing") + BODY,
                     b"# end-jottune-tl\n" + BODY,
                     b"# jottune-tl 1 1\n# end-jottune-tl\n" + BODY,
                     b"\xef\xbb\xbf" + HEADER + BODY,
                     HEADER.replace(b"Portable test", b"Portable\0test") + BODY]
        for data in malformed:
            with self.subTest(data=data[:80]):
                self.refuse(data)

    def test_line_bounds_include_metadata_and_accept_crlf(self):
        # JSON syntax belongs to shared hosts; native owns framing/byte bounds.
        for newline in (b"\n", b"\r\n"):
            data = b"# jottune-tl 1 1" + newline + b"# " + b"x" * 1020 + newline + b"# end-jottune-tl" + newline + BODY
            full, body, _ = self.export(data)
            self.assertEqual(body, BODY)
            self.assertTrue(full.startswith(data[:-len(BODY)]))
            self.refuse(data.replace(b"x" * 1020, b"x" * 1021), b"line longer")

    def test_portable_byte_bound_and_large_legacy_remain_distinct(self):
        full, _, _ = self.export(fill(HEADER + BODY, 1_000_000))
        self.assertEqual(full, HEADER + BODY)
        self.refuse(fill(HEADER + BODY, 1_000_001), b"exceeds 1000000 bytes")
        full, body, _ = self.export(fill(BODY, 1_000_001))
        self.assertEqual(full, BODY)
        self.assertEqual(body, BODY)

    def test_pool_has_one_first_header_and_aggregate_byte_bound(self):
        self.source.write_bytes(HEADER + BODY)
        extra = self.directory / "extra.tl"
        extra.write_bytes(b"# additional source\n")
        full = self.directory / "pool.tl"
        result = invoke("--play", self.source, extra, "--merged", full)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(full.read_bytes(), HEADER + BODY)
        extra.write_bytes(HEADER)
        self.assertNotEqual(invoke("--inspect", self.source, extra).returncode, 0)
        self.source.write_bytes(b"# earlier input\n")
        extra.write_bytes(HEADER + BODY)
        self.assertNotEqual(invoke("--inspect", self.source, extra).returncode, 0)
        self.source.write_bytes(HEADER + BODY)
        extra.write_bytes(fill(b"", 1_000_000))
        self.assertNotEqual(invoke("--inspect", self.source, extra).returncode, 0)

    def test_keys_derivation_does_not_inherit_metadata(self):
        self.source.write_bytes(HEADER + BODY)
        output = self.directory / "keys.tl"
        result = invoke("--keys-profile", "60", "--keys-carrier-bound", "--merged", output, self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.read_bytes().startswith(b"timeline "))
        self.assertNotIn(b"jottune-tl", output.read_bytes())
        self.assertEqual(invoke("--play", output).returncode, 0)

    def test_body_output_collision_guards(self):
        self.source.write_bytes(HEADER + BODY)
        output = self.directory / "alias.tl"
        result = invoke("--play", self.source, "--merged", output, "--merged-body", output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())
        result = invoke("--play", self.source, "--merged-body", self.source)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.source.read_bytes(), HEADER + BODY)

    @unittest.skipUnless(NATIVE.is_file(), "build bindings/native to exercise the bridge")
    def test_bridge_retains_owned_header_and_exports_body(self):
        probe = self.directory / "probe.c"
        probe.write_text(PROBE)
        binary = self.directory / "probe"
        compile_result = subprocess.run([os.environ.get("CC", "cc"), "-std=c99", "-Wall", "-Wextra",
            "-I" + str(ROOT / "bindings/native"), str(probe), str(NATIVE), "-lm", "-o", str(binary)],
            capture_output=True, timeout=60)
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr.decode(errors="replace"))
        for data in (BODY, HEADER + BODY, HEADER.replace(b"\n", b"\r\n") + BODY):
            self.source.write_bytes(data)
            full = self.directory / "bridge-full.tl"
            body = self.directory / "bridge-body.tl"
            result = subprocess.run([str(binary), str(self.source), str(full), str(body)], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertEqual(full.read_bytes(), data)
            self.assertEqual(body.read_bytes(), BODY)
            # Importing an exported document owns a new independent header.
            self.source.write_bytes(full.read_bytes())
            result = subprocess.run([str(binary), str(self.source), str(full), str(body)], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.source.write_bytes(HEADER.replace(b"1 1", b"1 2", 1) + BODY)
        result = subprocess.run([str(binary), str(self.source), str(full), str(body)], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"unsupported TL/engine requirement", result.stderr)



class CompanionABI(unittest.TestCase):
    def test_analysis_companion_uses_current_native_play_interface(self):
        result = subprocess.run([os.environ.get("CC", "cc"), "-std=c99", "-fsyntax-only", "-Isrc/host", "-Ibuild/s12e10p1v1", "-I.",
            '-DSP_BINAURAL_H="build/s12e10p1v1/sp_binaural_tables.h"', "tools/wasm/analysis.c"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == "__main__":
    unittest.main()
