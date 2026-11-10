#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Portable framing/metadata, identity and host round-trip regression checks."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import tl_document as tl
import fm
import interchange

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--harness", type=Path)
ARGS, REMAINING = parser.parse_known_args()

BODY = b"timeline draft1\n# ordinary body comment\n"
META = dict(title="Été", collection="Private collection", author="Composer", made_by="agent",
            genre="ambient/drone", tempo=dict(min_bpm=90, max_bpm=120.5),
            model=dict(id="reported/model-v1", source="client-reported"), utc_day="2024-02-29",
            license="CC-BY-4.0", renderer_release="2026-11-10-abcdef0", prompt_id="suite:one")


def document(payload):
    return b"# jottune-tl 1 1\n# " + payload + b"\n# end-jottune-tl\n" + BODY


class Documents(unittest.TestCase):
    def refused(self, data):
        with self.assertRaises(tl.DocumentError):
            tl.parse_document(data)

    def test_headerless_bytes_and_identity_are_unchanged(self):
        parsed = tl.parse_document(BODY)
        self.assertEqual(parsed, dict(body=BODY, header=b"", metadata={}, tl_version=1, engine_version=1))
        self.assertEqual(tl.body_sha256(BODY), hashlib.sha256(BODY).hexdigest())

    def test_empty_and_header_only_documents_are_rejected(self):
        for data in (b"",b"# jottune-tl 1 1\n# {}\n# end-jottune-tl",
                     b"# jottune-tl 1 1\n# {}\n# end-jottune-tl\n"):
            with self.subTest(data=data):
                self.refused(data)
        with self.assertRaises(tl.DocumentError):
            tl.compose_document(b"",{})

    def test_complete_metadata_and_raw_crlf_header_are_preserved(self):
        data = tl.compose_document(BODY, META).replace(b"\n", b"\r\n")
        parsed = tl.parse_document(data)
        self.assertEqual(parsed["metadata"], META)
        self.assertEqual(parsed["header"] + parsed["body"], data)
        self.assertEqual(parsed["body"], BODY.replace(b"\n", b"\r\n"))

    def test_metadata_only_changes_keep_body_identity(self):
        a, b = tl.compose_document(BODY, {}), tl.compose_document(BODY, META)
        self.assertEqual(tl.body_sha256(a), tl.body_sha256(b))
        self.assertNotEqual(hashlib.sha256(a).digest(), hashlib.sha256(b).digest())
        self.assertEqual(tl.parse_document(a)["metadata"], {})

    def test_nullable_unknowns_are_explicit(self):
        meta = dict(genre=None, tempo=None, model=None)
        self.assertEqual(tl.parse_document(tl.compose_document(BODY, meta))["metadata"], meta)
        for key in ("title", "author", "license", "made_by"):
            with self.subTest(key=key), self.assertRaises(tl.DocumentError):
                tl.compose_document(BODY, {key: None})

    def test_duplicate_unknown_nested_and_nonobject_json_are_rejected(self):
        for payload in (b'{"title":"A","title":"B"}', b'{"extra":1}', b'[]', b'null',
                        b'{"model":{"id":"x","id":"y","source":"user-stated"}}',
                        b'{"model":{"id":"x","source":"user-stated","extra":0}}',
                        b'{"tempo":{"min_bpm":1,"max_bpm":2,"extra":0}}',
                        b'{"tempo":{"min_bpm":NaN,"max_bpm":2}}', b'{"title":'):
            with self.subTest(payload=payload):
                self.refused(document(payload))

    def test_bytes_bom_nul_utf8_and_total_size_are_bounded(self):
        for data in (b"\xef\xbb\xbf" + BODY, BODY + b"\0", BODY + b"\xff", b"a" * 1_000_001):
            with self.subTest(data=data[:30]):
                self.refused(data)
        large = (b"#" * 999 + b"\n") * 1000
        self.assertEqual(len(large), 1_000_000)
        self.assertEqual(tl.parse_document(large)["body"], large)

    def test_physical_line_limits_count_utf8_bytes_and_crlf_content(self):
        self.assertEqual(tl.parse_document(b"x" * 1022 + b"\r\n")["body"], b"x" * 1022 + b"\r\n")
        for data in (b"x" * 1023 + b"\n", b"x" * 1023, "é".encode() * 512 + b"\n"):
            self.refused(data)

    def test_versions_are_positive_uint32_and_supported(self):
        for versions in ("0 1", "1 0", "2 1", "1 2", "4294967295 1", "4294967296 1",
                         "18446744073709551617 1", "01 1", "1 01", "+1 1", "1 -1"):
            self.refused(document(b"{}").replace(b"1 1", versions.encode(), 1))
        for value in (0, -1, True, 2, 4294967296):
            with self.subTest(value=value), self.assertRaises(tl.DocumentError):
                tl.compose_document(BODY, {}, tl_version=value)

    def test_recognized_malformed_repeated_and_misplaced_markers_are_errors(self):
        for marker in (b"#jottune-tl 1 1", b"#\tjottune-tl 1 1", b" # jottune-tl 1 1", b"\r# jottune-tl 1 2", b"#\rjottune-tl 1 2",
                       b"# jottune-tl", b"# jottune-tl 1 1 extra", b"# end-jottune-tl", b"#end-jottune-tl"):
            self.refused(marker + b"\n" + BODY)
            self.refused(BODY + marker + b"\n")
        self.refused(b"# jottune-tl 1 1\n# {}\n")
        self.refused(b"# jottune-tl 1 1\n{}\n# end-jottune-tl\n" + BODY)
        self.refused(tl.compose_document(BODY, {}) + b"# end-jottune-tl\n")
        with self.assertRaises(tl.DocumentError):
            tl.compose_document(tl.compose_document(BODY, {}), {})

    def test_text_labels_dates_and_numeric_limits_are_validated(self):
        cases = [dict(title="e\u0301"), dict(title=" padded"), dict(title="a\u2028b"),
                 dict(author="a\tb"), dict(title="\ud800"), dict(title="x" * 61),
                 dict(collection="x" * 201), dict(made_by="unknown"), dict(genre="space label"),
                 dict(license="©"), dict(prompt_id="free prompt here"), dict(renderer_release="bad/release"),
                 dict(utc_day="2025-02-29"), dict(utc_day="2024-2-29"),
                 dict(model=dict(id="x", source="inferred")), dict(tempo=dict(min_bpm=True,max_bpm=2)),
                 dict(tempo=dict(min_bpm=2,max_bpm=1)), dict(tempo=dict(min_bpm=0,max_bpm=1)),
                 dict(tempo=dict(min_bpm=1,max_bpm=9007199254740992)),
                 dict(tempo=dict(min_bpm=1,max_bpm=float("inf")))]
        for meta in cases:
            with self.subTest(meta=repr(meta)), self.assertRaises(tl.DocumentError):
                tl.compose_document(BODY, meta)
        self.assertEqual(tl.validate_metadata(dict(title="🎵" * 60))["title"], "🎵" * 60)

    def test_parser_does_not_claim_to_validate_music_grammar(self):
        body = b"invalid native music syntax\n"
        self.assertEqual(tl.parse_document(tl.compose_document(body, {}))["body"], body)

    def test_bounded_file_reader_rejects_nonregular_inputs_without_blocking(self):
        with tempfile.TemporaryDirectory(prefix="tl-bounded-") as temporary:
            root=Path(temporary)
            fifo=root/"input.fifo"
            os.mkfifo(fifo)
            with self.assertRaises(tl.DocumentError):
                tl.read_bounded(fifo)
            with self.assertRaises((tl.DocumentError,OSError)):
                tl.read_bounded(root)

    def test_cli_pack_validate_body_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="tl-document-") as temporary:
            root = Path(temporary)
            body, meta, packed, extracted = (root / name for name in ("body.tl", "meta.json", "packed.tl", "exact.tl"))
            body.write_bytes(BODY)
            meta.write_text(json.dumps(META))
            command = [sys.executable, "-B", str(ROOT / "tools/tl_document.py")]
            def run(*args):
                return subprocess.run([*command, *map(str,args)], capture_output=True, text=True)
            self.assertEqual(run("pack",body,"--metadata",meta,"--out",packed).returncode,0)
            validation = run("validate",packed)
            self.assertEqual(validation.returncode,0, validation.stderr)
            self.assertEqual(json.loads(validation.stdout)["record_sha256"], tl.body_sha256(BODY))
            self.assertEqual(run("body",packed,"--out",extracted).returncode,0)
            self.assertEqual(extracted.read_bytes(),BODY)
            original = packed.read_bytes()
            self.assertEqual(run("pack",body,"--metadata",meta,"--out",packed).returncode,2)
            self.assertEqual(packed.read_bytes(), original)
            self.assertEqual(run("body",packed,"--out",body).returncode,2)
            self.assertEqual(body.read_bytes(),BODY)


@unittest.skipUnless(ARGS.harness, "pass --harness for native/Python integration")
class NativeIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native = fm.Native(ARGS.harness)
        cls.body = fm.native_render(cls.native,[ROOT / "tests/golden/48000/quiet.tl"])["_record_bytes"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tl-host-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "input.tl"
        self.source.write_bytes(tl.compose_document(self.body,META).replace(b"\n",b"\r\n"))

    def test_native_full_body_outputs_preserve_metadata_pcm_and_identity(self):
        portable = fm.native_render(self.native,[self.source],want_wav=True)
        legacy = fm.native_render(self.native,[ROOT / "tests/golden/48000/quiet.tl"],want_wav=True)
        self.assertEqual(portable["record_sha256"],legacy["record_sha256"])
        self.assertEqual(portable["_wav_bytes"],legacy["_wav_bytes"])
        self.assertEqual(portable["metadata"],META)
        self.assertEqual(tl.parse_document(portable["_record_bytes"])["header"],tl.parse_document(self.source.read_bytes())["header"])
        self.assertEqual(portable["document_sha256"],fm.sha(portable["_record_bytes"]))
        self.assertNotEqual(portable["document_sha256"],portable["record_sha256"])

    def test_retained_artifact_log_replay_and_session_keep_full_document(self):
        takes = self.root / "takes"
        reply = fm.render([self.source],log=takes,harness=ARGS.harness,detail="compact")
        self.assertTrue(reply["ok"],reply)
        retained = Path(reply["record"]).read_bytes()
        self.assertEqual(tl.parse_document(retained)["metadata"],META)
        self.assertEqual(fm.read_log(takes)[0]["record_sha256"],reply["record_sha256"])
        locator=reply["artifact"]
        self.assertEqual(locator["digest"],reply["document_sha256"])
        self.assertNotEqual(locator["digest"],reply["record_sha256"])
        self.assertEqual(locator["filename"],reply["record_sha256"]+".tl")
        self.assertEqual(Path(locator["directory"]),Path(reply["record"]).parent)
        offset,chunks=0,[]
        while True:
            page=fm.artifact_read(**locator,offset=offset,length=257)
            self.assertTrue(page["ok"],page)
            self.assertEqual(page["sha256"],locator["digest"])
            chunk=page["text"].encode("utf-8")
            chunks.append(chunk)
            offset+=len(chunk)
            if page["eof"]:
                self.assertEqual(offset,page["size"])
                break
        self.assertEqual(b"".join(chunks),retained)
        self.assertEqual(fm.sha(b"".join(chunks)),locator["digest"])
        replay = fm.log_replay(takes,harness=ARGS.harness)
        self.assertTrue(replay["ok"],replay)
        bundle = fm.session_export([takes],out=self.root / "session")
        self.assertTrue(bundle["ok"],bundle)
        self.assertTrue(fm.session_verify(self.root / "session")["ok"])

    def test_legacy_artifact_locator_keeps_its_existing_shape(self):
        takes=self.root/"legacy"
        reply=fm.render([ROOT/"tests/golden/48000/quiet.tl"],log=takes,harness=ARGS.harness,detail="compact")
        self.assertTrue(reply["ok"],reply)
        self.assertEqual(reply["artifact"],dict(directory=str(takes),kind="record",digest=reply["record_sha256"],encoding="utf8"))
        page=fm.artifact_read(**reply["artifact"])
        self.assertTrue(page["ok"],page)
        self.assertEqual(fm.sha(page["text"].encode()),reply["artifact"]["digest"])

    def test_old_artifact_locator_refuses_metadata_changes_at_same_body_name(self):
        reply=fm.render([self.source],log=self.root/"takes",harness=ARGS.harness,detail="compact")
        self.assertTrue(reply["ok"],reply)
        path=Path(reply["record"])
        original=tl.parse_document(path.read_bytes())
        path.write_bytes(tl.compose_document(original["body"],dict(META,title="Updated metadata")))
        self.assertEqual(tl.body_sha256(path.read_bytes()),reply["record_sha256"])
        page=fm.artifact_read(**reply["artifact"])
        self.assertFalse(page["ok"],page)
        self.assertEqual(page["exit"],5)

    def test_canonical_revision_preserves_header(self):
        accepted = fm.native_render(self.native,[self.source])
        row = next(row for row in accepted["_rows"] if row["id"] == "header_fs")
        field = row["fields"][0]
        revised = fm.edit_canonical(accepted,[dict(row=row["line_index"], token=field["token"],value=48000)])
        self.assertEqual(tl.parse_document(revised)["header"],tl.parse_document(self.source.read_bytes())["header"])
        path = self.root / "revised.tl"
        path.write_bytes(revised)
        self.assertEqual(fm.native_render(self.native,[path])["record_sha256"],accepted["record_sha256"])

    def test_native_output_header_or_body_identity_mismatch_is_refused(self):
        with self.assertRaisesRegex(fm.ToolError,"preserve"):
            fm.verify_native_document(self.body,self.body,b"required header\n")
        with self.assertRaisesRegex(fm.ToolError,"outputs disagree"):
            fm.verify_native_document(self.body,self.body+b"\n",b"")

    def test_interchange_verifies_body_identity_and_full_document_checksum(self):
        output=self.root / "bundle"
        result=interchange.export(self.source,harness=ARGS.harness,profile="wav",out=output)
        self.assertTrue(result["ok"],result)
        self.assertEqual(tl.parse_document((output/"source.tl").read_bytes())["metadata"],META)
        self.assertTrue(interchange.verify(output)["ok"])
        manifest=json.loads((output/"manifest.json").read_bytes())
        self.assertEqual(manifest["report"]["source"]["record_sha256"],tl.body_sha256(self.body))
        self.assertEqual(manifest["report"]["source"]["document_sha256"],fm.sha((output/"source.tl").read_bytes()))
        manifest["report"]["source"]["record_sha256"]=manifest["artifacts"]["source.tl"]["sha256"]
        (output/"manifest.json").write_text(json.dumps(manifest))
        self.assertFalse(interchange.verify(output)["ok"])

    def test_measurement_paths_preserve_metadata_and_body_identity(self):
        expected = tl.body_sha256(self.body)
        results = [fm.measure_module().execute(fm,[self.source],harness=ARGS.harness),
                   fm.measure_sections([self.source],harness=ARGS.harness,sections=[{}],detail="full"),
                   fm.measure_voice_sections([self.source],harness=ARGS.harness,voices=[0],sections=[{}],detail="full")]
        for reply in results:
            self.assertTrue(reply["ok"],reply)
            self.assertEqual(reply["record_sha256"],expected)
            self.assertEqual(tl.parse_document(reply["canonical"].encode())["metadata"],META)
            self.assertEqual(reply["document_sha256"],fm.sha(reply["canonical"].encode()))

    def test_sampled_export_retains_source_metadata_without_inventing_cell_attribution(self):
        result = interchange.export(self.source,harness=ARGS.harness,profile="sfz-bank",patch="quiet",
            notes=[60],velocities=[64],hold_ms=10,tail_ms=10,max_samples=1,accept_loss=True,out=self.root/"samples")
        self.assertTrue(result["ok"],result)
        self.assertEqual(tl.parse_document((self.root/"samples/source.tl").read_bytes())["metadata"],META)
        self.assertEqual(tl.parse_document((self.root/"samples/n060-v064.tl").read_bytes())["metadata"],{})

    def test_changed_metadata_never_overwrites_an_existing_body_addressed_artifact(self):
        takes = self.root/"takes"
        first = fm.render([self.source],log=takes,harness=ARGS.harness)
        self.assertTrue(first["ok"],first)
        original = Path(first["record"]).read_bytes()
        self.source.write_bytes(tl.compose_document(self.body,dict(META,title="Changed declaration")))
        changed = fm.render([self.source],log=takes,harness=ARGS.harness)
        self.assertFalse(changed["ok"],changed)
        self.assertEqual(Path(first["record"]).read_bytes(),original)
        self.assertEqual(len(fm.read_log(takes)),1)

    def test_direct_composer_play_and_recipe_expansion_preserve_raw_header(self):
        composer=fm.compose_module()
        composer.H=str(ARGS.harness.resolve())
        output=self.root/"composed.tl"
        status, fields, _=composer.play([str(self.source)],merged=str(output))
        self.assertEqual(status,0)
        self.assertEqual(tl.parse_document(output.read_bytes())["header"],tl.parse_document(self.source.read_bytes())["header"])
        expanded=self.root/"expanded.tl"
        composer.expand_timeline(str(self.source),str(expanded))
        self.assertEqual(expanded.read_bytes(),self.source.read_bytes())
        self.source.write_bytes(document(b'{"unknown":1}'))
        with self.assertRaises(SystemExit):
            composer.expand_timeline(str(self.source),str(self.root/"refused.tl"))
        self.assertFalse((self.root/"refused.tl").exists())

    def test_keys_and_mirror_derivations_omit_the_whole_header(self):
        spec=importlib.util.spec_from_file_location("tl_keys",ROOT/"tools/music/keys_tl.py")
        keys=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(keys)
        for mirror in (False,True):
            result=keys.convert([self.source],harness=ARGS.harness,out=self.root/("mirror.tl" if mirror else "keys.tl"),
                                work=self.root/("mirror-work" if mirror else "keys-work"),mirror=mirror,level_policy="carrier-bound")
            self.assertTrue(result["ok"],result)
            output=Path(result["output"]["path"]).read_bytes()
            self.assertEqual(tl.parse_document(output)["header"],b"")
            self.assertEqual(tl.parse_document(output)["metadata"],{})
        self.assertEqual(tl.parse_document(self.source.read_bytes())["metadata"],META)

    def test_generator_metadata_is_validated_before_native_work(self):
        self.source.write_bytes(document(b'{"unknown":1}'))
        result=fm.generator_module().execute(fm,[self.source],harness=ARGS.harness)
        self.assertFalse(result["ok"],result)
        self.assertEqual(result["exit"],2)
        self.assertEqual(result["native_calls"]["total"],0)

    def test_invalid_retained_metadata_is_a_storage_failure(self):
        takes=self.root/"takes"
        result=fm.render([self.source],log=takes,harness=ARGS.harness)
        self.assertTrue(result["ok"],result)
        path=Path(result["record"])
        path.write_bytes(document(b'{"unknown":1}').replace(BODY,self.body))
        with self.assertRaises(fm.ToolError) as failure:
            fm.read_log(takes)
        self.assertEqual(failure.exception.exit,5)
        read=fm.artifact_read(takes,"record",result["record_sha256"],encoding="utf8")
        self.assertFalse(read["ok"],read)
        self.assertEqual(read["exit"],5)

    def test_score_metadata_is_explicit_and_line_maps_include_header(self):
        score=dict(version=1,fs=48000,clock=dict(bars=1,meter=[dict(bar=1,quarters=4)],
            tempo=[dict(bar=1,bpm=120)],ramp="step",tail_ns=0),declarations="recipe tone bell\n",
            streams=["main"],voices=["tone"],events=[],metadata=copy.deepcopy(META))
        module=fm.score_module()
        lowered=module.lower_score(module.validate_score(score),composer=fm.compose_module(),
                                   engine=self.native.metadata.engine,statements=self.native.metadata.statements)
        parsed=tl.parse_document(lowered["authored_tl"].encode())
        self.assertEqual(parsed["metadata"],META)
        first_body=parsed["header"].count(b"\n")
        self.assertEqual(lowered["line_map"][first_body]["role"],"owned")
        self.assertEqual(lowered["line_map"][first_body]["line"],first_body+1)
        self.assertEqual(len(lowered["line_map"]),lowered["counts"]["authored_lines"])
        self.assertIn("metadata",module.score_schema()["oneOf"][0]["properties"])
        for bad in (dict(META,model={"id":"x","source":"inferred"}),dict(META,license=None)):
            with self.assertRaises(module.ScoreError):
                module.validate_score(dict(score,metadata=bad))
        with self.assertRaises(module.ScoreError):
            module.validate_score(dict(score,declarations="#jottune-tl 1 1\n"))
        published=fm.compose(score,log=self.root/"compose",receipt_dir=self.root/"receipts",
                             harness=ARGS.harness,detail="full")
        self.assertTrue(published["ok"],published)
        self.assertEqual(tl.parse_document(Path(published["record"]).read_bytes())["metadata"],META)
        self.assertEqual(published["artifact"]["digest"],published["document_sha256"])
        self.assertEqual(published["artifact"]["filename"],published["record_sha256"]+".tl")
        page=fm.artifact_read(**published["artifact"])
        self.assertTrue(page["ok"],page)
        self.assertEqual(fm.sha(page["text"].encode()),published["artifact"]["digest"])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0],*REMAINING])
