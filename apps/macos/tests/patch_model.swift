// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// The actual token model, linked to the bridge; no audio or alternate grammar.
import Foundation

@main struct ModelCheck {
    static func main() {
        var patches = 0, bad = 0
        let args = Array(CommandLine.arguments.dropFirst())
        guard args.count >= 2, args[0] == "--params",
              let raw = try? String(contentsOfFile: args[1], encoding: .utf8), let parameters = NativeParams(raw) else {
            print("MODEL FAIL explicit parameter catalog required"); exit(1)
        }
        let paths = args.dropFirst(2)
        func check(_ condition: Bool, _ message: String) {
            if !condition { bad += 1; print("MODEL FAIL \(message)") }
        }
        for path in paths {
            var e = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            let p = path.withCString { c -> OpaquePointer? in
                var q: UnsafePointer<CChar>? = c
                return fms_player_open(&q, 1, &e, e.count)
            }
            guard let p else { check(false, "load \(URL(fileURLWithPath: path).lastPathComponent): \(String(cString: e))"); continue }
            var info = fms_info_t(); fms_player_info(p, &info)
            for i in 0..<info.npatches {
                guard var model = PatchLines.read(p, i, fs: info.fs, parameters: parameters) else { check(false, "extraction"); continue }
                let n = fms_player_patch_lines(p, i, nil, 0)
                var b = [CChar](repeating: 0, count: n)
                _ = fms_player_patch_lines(p, i, &b, n)
                let expected = String(cString: b)
                check(model.text == expected, "unchanged accepted lines")
                let namedCount = fms_patch_text(model.text, model.name, nil, 0)
                var named = [CChar](repeating: 0, count: namedCount)
                check(namedCount > 0 && fms_patch_text(model.text, model.name, &named, namedCount) == namedCount && String(cString: named) == expected, "pure formatter preserves accepted bytes")
                let renamedCount = fms_patch_text(model.text, "rekey", nil, 0)
                var renamed = [CChar](repeating: 0, count: renamedCount)
                _ = fms_patch_text(model.text, "rekey", &renamed, renamedCount)
                check(renamedCount > 0 && fms_patch_lines_check(renamed, renamedCount - 1, info.fs, &e, e.count) == 0, "rename keeps bank bindings")
                model.validate()
                check(model.diagnostic == nil, "accepted input \(model.diagnostic ?? "")")
                let other = model.rows.filter { $0.statement != "patch" }
                var op = model.op(0)
                model.setOp(0, op)
                check(model.text == expected, "no-op operator view preserves bytes")
                op.level_db10 = op.level_db10 >= 1200 ? op.level_db10 - 1 : op.level_db10 + 1
                model.setOp(0, op); model.validate()
                check(model.diagnostic == nil, "operator edit \(model.name): \(model.diagnostic ?? "")")
                check(model.rows.filter { $0.statement != "patch" } == other, "untyped rows survive operator edit")
                patches += 1
            }
            fms_player_close(p)
        }
        var p = PatchLines("patch probe 0 65536 0 0 0 0 0 0 0 0\npatch probe 1 65536 0 0 0 0 0 0 0 0\n", parameters: parameters)
        var hidden = p.op(0); hidden.level_db10 = 200
        p.setOp(2, hidden); p.validate()
        check(p.op(2).level_db10 == 200 && p.ops_defined == 3 && p.diagnostic == nil, "hidden operator retains state without enabling a route")
        p.lfo.rate_millihz = 5000; p.lfo.pitch_cents = 100
        p.lfo.rate_millihz = 0; p.validate()
        check(!p.text.contains("lfo ") && p.lfo.pitch_cents == 100 && p.diagnostic == nil, "disabled LFO keeps its values")
        p.lfo.rate_millihz = 5000; p.validate()
        check(p.text.contains("lfo ") && p.lfo.pitch_cents == 100 && p.diagnostic == nil, "LFO re-enable")
        p.fenv.depth_cents = 100; p.validate()
        check(p.diagnostic?.contains("needs a filter") == true, "refused intermediate edit retained")
        p.filter_mode = 1; p.filter_cutoff_millihz = 1000000; p.filter_q_milli = 707; p.validate()
        check(p.diagnostic == nil && p.fenv.depth_cents == 100, "dependent edit recovers")
        p.put("route", nil, ["route", "probe", "2", "1", "1>0:256:ring"])
        p.setRouteWeight(1, 0, 128); p.validate()
        check(p.tokens("route").contains("1>0:128:ring") && p.diagnostic == nil, "edge kind survives weight edit")
        var bank = PatchLines("patch banked 0 65536 0 0 0 0 0 0 0 0\npatch banked 1 65536 0 0 0 0 0 0 0 0\n", parameters: parameters)
        check(bank.addStatement("harmonic") == nil && bank.diagnostic == nil, "add authored harmonic bank")
        check(bank.addStatement("harmonic") == nil && bank.diagnostic == nil, "add second harmonic")
        let n = fms_patch_text(bank.text, "renamed", nil, 0)
        var renamed = [CChar](repeating: 0, count: n), e = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        _ = fms_patch_text(bank.text, "renamed", &renamed, n)
        check(n > 0 && fms_patch_lines_check(renamed, n - 1, bank.fs, &e, e.count) == 0, "harmonic bank identifiers survive patch rename")
        var recipient = PatchLines("patch recipient 0 65536 0 0 0 0 0 0 0 0\npatch recipient 1 65536 0 0 0 0 0 0 0 0\n", parameters: parameters)
        check(recipient.importBank("harmonics", from: bank) == nil && recipient.diagnostic == nil, "record bank picker imports only selected bank")
        check(recipient.bankNames == ["harmonics"] && !recipient.text.contains("patch banked"), "bank import preserves destination patch")
        check(recipient.addStatement("wtuse") == nil && recipient.diagnostic == nil, "imported bank binds through typed row")
        check(recipient.importBank("absent", from: bank) != nil, "missing bank refusal")
        let before = recipient.text
        check(recipient.setField("wtuse.0.position", "99999") && recipient.value(recipient.fields.first { $0.id == "wtuse.0.position" }!) == "32768" && recipient.diagnostic == nil, "typed outer clamp")
        check(recipient.text != before, "typed clamp changes row")
        check(recipient.setField("wtuse.0.bank", "absent") && recipient.diagnostic != nil, "typed refused value stays visible")
        check(recipient.setField("wtuse.0.bank", "harmonics") && recipient.diagnostic == nil, "typed refused value recovers")
        let releases = "patch release 0 65536 0 0 0 0 0 0 0 17\npatch release 1 65536 0 0 0 0 0 0 0 250\n"
        check(fms_midi_release_ms(releases, 48000) == 17, "fixed path MIDI release uses carrier0")
        check(fms_midi_release_ms(releases + "route release 2 3\n", 48000) == 250, "routed MIDI release uses longest carrier")
        check(fms_midi_release_ms(releases + "route release 2 1\n", 48000) == 17, "MIDI release excludes noncarrier")
        check(fms_midi_release_ms("invalid\n", 48000) == 0 && fms_midi_release_ms(nil, 48000) == 0, "invalid MIDI release input")
        let rawBase = "patch raw 0 65536 0 0 0 0 0 0 0 0\npatch raw 1 65536 0 0 0 0 0 0 0 0\n"
        for raw in ["filter", "filter raw", "route", "route raw", "route raw 4294967295 1", "patch", "patch raw", "patch raw 0"] {
            var draft = PatchLines(rawBase + (raw.hasPrefix("patch") ? "" : "amp raw 5 200 120 300\n"), parameters: parameters)
            draft.editRow(raw.hasPrefix("patch") ? 0 : 2, text: raw)
            let text = draft.text
            check(draft.diagnostic != nil, "partial raw diagnostic")
            _ = draft.name; _ = draft.ops_defined; _ = draft.filter_mode; _ = draft.filter_cutoff_millihz; _ = draft.filter_q_milli
            _ = draft.route_ops; _ = draft.route_carriers; _ = draft.lfo; _ = draft.peg; _ = draft.fenv
            for op in 0..<4 { _ = draft.op(op); for dest in 0..<4 { _ = draft.routeWeight(op, dest) } }
            for field in draft.fields { _ = draft.value(field) }
            check(draft.text == text, "reading partial projections preserves raw text")
            if raw.hasPrefix("filter") { draft.filter_mode = 1; draft.filter_cutoff_millihz = 1000000; draft.filter_q_milli = 707 }
            else if raw.hasPrefix("route") { draft.route_ops = 2; draft.route_carriers = 1; draft.setRouteWeight(1, 0, 256) }
            else { var op = draft.op(0); op.ratio_q16 = 65536; draft.setOp(0, op) }
            draft.validate(); check(draft.diagnostic == nil, "partial typed repair: \(raw)")
        }
        var stale = PatchLines(rawBase + "filter raw lp 1000000 707\n", parameters: parameters)
        let oldField = stale.fields[0]; stale.rows.removeLast()
        check(stale.value(oldField).isEmpty, "stale field projection is safe")
        let staleText = stale.text; stale.editRow(999, text: "filter")
        check(stale.text == staleText, "stale raw row binding is ignored")
        metadataControls(raw, parameters, check)
        metadataChecks(check)
        print("MODEL \(bad == 0 ? "ok" : "FAIL") records=\(paths.count) patches=\(patches) audio_frames=0")
        exit(bad == 0 ? 0 : 1)
    }

    static func metadataChecks(_ check: (Bool, String) -> Void) {
        var count = 0
        func test(_ accepted: Bool, _ label: String) { count += 1; check(accepted, "portable metadata: " + label) }
        func document(_ json: String, versions: String = "1 1") -> Data {
            Data("# jottune-tl \(versions)\n# \(json)\n# end-jottune-tl\ntimeline fixture\n".utf8)
        }
        func refuse(_ bytes: Data, _ label: String) {
            do { try TLDocumentMetadata.validate(bytes); test(false, label) }
            catch { test(true, label) }
        }
        let valid = document(#"{"title":"Été","collection":"Collection","author":"Composer","made_by":"agent","genre":"ambient/drone","tempo":{"min_bpm":90,"max_bpm":120.5},"model":{"id":"reported/model-v1","source":"client-reported"},"utc_day":"2024-02-29","license":"CC-BY-4.0","renderer_release":"2026-11-10-abcdef0","prompt_id":"suite:one"}"#)
        test((try? TLDocumentMetadata.validate(valid)) == true, "all supported declarations")
        test((try? TLDocumentMetadata.validate(Data(String(decoding: valid, as: UTF8.self).replacingOccurrences(of: "\n", with: "\r\n").utf8))) == true, "CRLF framing")
        test((try? TLDocumentMetadata.validate(document(#"{"genre":null,"tempo":null,"model":null}"#))) == true, "nullable unknowns")
        test((try? TLDocumentMetadata.validate(document("{}"))) == true, "empty metadata object")
        for (key, limit) in [("title", 60), ("collection", 200), ("author", 200)] {
            let accepted = #"{"\#(key)":"\#(String(repeating: "é", count: limit))"}"#
            test((try? TLDocumentMetadata.validate(document(accepted))) == true, "\(key) scalar boundary")
            refuse(document(#"{"\#(key)":"\#(String(repeating: "é", count: limit + 1))"}"#), "\(key) excessive scalars")
        }
        for json in [#"{"title":"e\u0301"}"#, #"{"title":" padded"}"#, #"{"title":"a\u2028b"}"#,
                     #"{"title":"a\u0000b"}"#, #"{"author":"a\tb"}"#, #"{"title":"\ud800"}"#,
                     #"{"title":null}"#, #"{"made_by":"unknown"}"#, #"{"genre":"space label"}"#,
                     #"{"license":"©"}"#, #"{"prompt_id":"free prompt"}"#, #"{"renderer_release":"bad/release"}"#,
                     #"{"utc_day":"2025-02-29"}"#, #"{"utc_day":"0000-01-01"}"#, #"{"utc_day":"1900-02-29"}"#,
                     #"{"utc_day":"2024-2-29"}"#, #"{"model":{"id":"x","source":"inferred"}}"#,
                     #"{"model":{"id":"x","source":"user-stated","extra":0}}"#,
                     #"{"tempo":{"min_bpm":true,"max_bpm":2}}"#, #"{"tempo":{"min_bpm":2,"max_bpm":1}}"#,
                     #"{"tempo":{"min_bpm":0,"max_bpm":1}}"#, #"{"tempo":{"min_bpm":1,"max_bpm":9007199254740992}}"#,
                     #"{"tempo":{"min_bpm":1,"max_bpm":1e999}}"#, #"{"extra":1}"#, "[]", "null", "{",
                     #"{"title":"x",}"#, #"{"model":{"id":"x","source":"user-stated",}}"#,
                     #"{"title":"A","title":"B"}"#, #"{"title":"A","ti\u0074le":"B"}"#,
                     #"{"model":{"id":"x","id":"y","source":"user-stated"}}"#,
                     #"{"tempo":{"min_bpm":1,"min_bpm":2,"max_bpm":3}}"#] {
            refuse(document(json), json)
        }
        for versions in ["0 1", "1 0", "2 1", "1 2", "4294967296 1", "1 4294967296", "99999999999999999999999 1", "01 1", "1 01", "+1 1", "1 -1"] {
            refuse(document("{}", versions: versions), "version " + versions)
        }
        refuse(Data("\n".utf8) + valid, "misplaced header")
        refuse(valid + valid, "repeated header")
        refuse(Data("# jottune-tl 1 1\n# {}\n".utf8), "unterminated header")
        refuse(Data(String(decoding: valid, as: UTF8.self).replacingOccurrences(of: "# jottune-tl", with: "#jottune-tl").utf8), "malformed reserved marker")
        refuse(valid + Data([0xff]), "invalid UTF-8 body")
        refuse(document("\u{feff}{}"), "BOM inside JSON payload")
        refuse(document("{\u{0b}}"), "non-JSON whitespace")
        refuse(valid + Data(repeating: 35, count: 1023), "physical line byte bound")
        refuse(valid + Data(repeating: 10, count: 1_000_000), "whole document byte bound")
        let family = String(repeating: "👨‍👩‍👧‍👦", count: 10)
        refuse(document(#"{"title":"\#(family)"}"#), "scalar count differs from grapheme count")
        test((try? TLDocumentMetadata.validate(Data(repeating: 10, count: 1_000_001))) == false, "larger headerless legacy input")
        let scratch = FileManager.default.temporaryDirectory.appendingPathComponent("jottune-schema-\(UUID().uuidString).tl")
        defer { try? FileManager.default.removeItem(at: scratch) }
        do {
            try document(#"{"title":"A","title":"B"}"#).write(to: scratch)
            var error = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            let player = TLDocumentMetadata.open(paths: [scratch.path], error: &error)
            test(player == nil && String(cString: error).contains("Duplicate"), "shared native-open gate rejects duplicate JSON")
            if let player { fms_player_close(player) }
        } catch { test(false, "temporary host-open fixture") }
        print("TL_METADATA_MODEL checks=\(count) duplicate_keys=refused schema=closed title=scalars versions=uint32 legacy=retained")
    }

    static func metadataControls(_ raw: String, _ catalog: NativeParams, _ check: (Bool, String) -> Void) {
        // Every migrated field is present in this projection fixture. The native
        // reader remains the validator; coupled sound constraints are not rebuilt.
        let fixture = """
        patch probe 0 65536 60 0 0 128 5 400 120 300
        patch probe 1 65536 0 2048 0 128 2 600 240 300
        shape probe 20 hard
        amp probe 5 200 120 300
        egloop probe 0
        kstype probe 0 index - +
        fbtype probe 0 classic
        mod probe 0 ctl0 pitch0 100 3
        eg probe 0 1 2 3
        ks probe 0 120
        noise probe 0 4
        pd probe 0 16384
        vector probe 100 200
        formant probe 0 1000000 707 20
        ksource probe 0 1000 1 64
        wtuse probe 0 bank 100
        harmonic bank 0 1 120 0
        """
        let base = PatchLines(fixture, parameters: catalog)
        let fields = base.fields.filter(\.metadataRequired)
        check(fields.count == 27 && catalog.missingNumericControls.isEmpty, "all 27 numeric mappings exist")
        var controls = 0
        for field in fields {
            let statement = base.rows[field.row].statement
            guard let metadata = catalog.numericField(statement: statement, token: field.token),
                  let range = field.range else { check(false, "numeric metadata missing: " + field.id); continue }
            check(field.native == metadata && field.unavailableReason == nil, "view and setter share metadata: " + field.id)
            let source = catalog.parameters.first { $0["id"] == metadata.id }!
            check(range == (Int(source["lo"])!...Int(source["hi"])!), "exact native range: " + field.id)
            check(metadata.help.contains(source["unit"]) && metadata.help.contains(source["scale"]) && metadata.help.contains(source["clamp"]), "native help: " + field.id)
            for (input, expected) in [(range.lowerBound - 1, range.lowerBound), (range.upperBound + 1, range.upperBound)] {
                var edited = base
                check(edited.setField(field.id, String(input)) && edited.rows[field.row].tokens[field.token] == String(expected), "native bound applied: " + field.id)
            }
            let lines = raw.split(separator: "\n").map(String.init)
            let index = lines.firstIndex { $0.hasPrefix("PARAM id=" + metadata.id + " ") }!
            var changed = lines
            changed[index] = lines[index].replacingOccurrences(of: " hi=" + source["hi"] + " ", with: " hi=" + String(range.upperBound - 1) + " ")
            guard let altered = NativeParams(changed.joined(separator: "\n") + "\n") else { check(false, "changed range catalog malformed"); continue }
            var edit = PatchLines(fixture, parameters: altered)
            let projected = edit.fields.first { $0.id == field.id }!
            check(projected.range?.upperBound == range.upperBound - 1 &&
                  edit.setField(field.id, String(range.upperBound)) &&
                  edit.rows[field.row].tokens[field.token] == String(range.upperBound - 1), "changed catalog controls view and setter: " + field.id)
            var missing = lines; missing.remove(at: index)
            missing[missing.count - 1] = "PARAMS count=\(catalog.parameters.count - 1) statements=\(catalog.statements.count) forms=\(catalog.forms.count)"
            guard let incomplete = NativeParams(missing.joined(separator: "\n") + "\n") else { check(false, "missing field catalog malformed"); continue }
            var refused = PatchLines(fixture, parameters: incomplete)
            let before = refused
            check(refused.fields.first { $0.id == field.id }?.unavailableReason != nil &&
                  !refused.setField(field.id, "1") && refused == before, "missing metadata refuses without edits: " + field.id)
            controls += 2
        }
        var unavailable = PatchLines(fixture, parameters: nil)
        check(unavailable == base, "catalog context does not change patch identity")
        check(unavailable.fields.filter(\.metadataRequired).allSatisfy { $0.unavailableReason != nil && $0.range == nil }, "missing catalog disables numeric controls")
        check(!unavailable.setField("shape.drive", "10"), "missing catalog refuses setter")
        check(unavailable.setField("shape.kind", "fold"), "other controls survive missing metadata")
        let shape = unavailable.rows.firstIndex { $0.statement == "shape" }!
        unavailable.editRow(shape, text: "shape probe 10 hard")
        check(unavailable.rows[shape].text == "shape probe 10 hard", "raw edit survives missing metadata")
        var accepted = PatchLines("patch probe 0 65536 60 0 0 128 5 400 120 300\npatch probe 1 65536 0 2048 0 128 2 600 240 300\nshape probe 20 hard\n", parameters: catalog)
        check(accepted.setField("shape.drive", "241") && accepted.diagnostic == nil, "bounded typed edit retains native acceptance")
        _ = accepted.setField("shape.drive", "invalid")
        check(accepted.diagnostic != nil && accepted.tokens("shape")[2] == "invalid", "native refusal preserves invalid text")
        print("PARAMS_MODEL mappings=\(fields.count) mutation_controls=\(controls) missing_catalog=refused raw_edit=available native_validation=retained")
    }
}
