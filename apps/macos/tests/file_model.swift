// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Actual app write-back path: metadata identity plus small edited records.
import Foundation

@main struct FilesCheck {
    @MainActor static func main() async {
        var failures = 0, patches = 0, records = 0
        let input = Array(CommandLine.arguments.dropFirst())
        guard input.count >= 2, input[0] == "--params",
              let raw = try? String(contentsOfFile: input[1], encoding: .utf8), let parameters = NativeParams(raw) else {
            print("FILES FAIL explicit parameter catalog required"); exit(1)
        }
        func check(_ condition: Bool, _ label: String) {
            if !condition { failures += 1; print("FILES FAIL \(label)") }
        }
        func read(_ paths: [String], _ index: UInt32 = 0) throws -> PatchLines {
            var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            guard let p = InstrPatchIO.withCStrings(paths, { fms_player_open($0, Int32(paths.count), &err, err.count) }) else { throw InstrError(instrCStr(err)) }
            defer { fms_player_close(p) }
            var info = fms_info_t(); fms_player_info(p, &info)
            guard let patch = PatchLines.read(p, index, fs: info.fs, parameters: parameters) else { throw InstrError("missing patch") }
            return patch
        }
        func untouched(_ bytes: Data, _ name: String) -> Data {
            PatchWriteback.lines(bytes).filter { line in
                let t = line.tokens
                return t.first != "hash" && !(t.count > 1 && PatchWriteback.scoped.contains(t[0]) && t[1] == name)
            }.reduce(into: Data()) { $0.append($1.bytes) }
        }
        let args = Array(input.dropFirst(2))
        guard args.count > 1 else { print("FILES FAIL arguments"); exit(1) }
        let output = URL(fileURLWithPath: args[0], isDirectory: true)
        do { try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true) }
        catch { print("FILES FAIL output directory"); exit(1) }
        for path in args.dropFirst() {
            do {
                let bytes = try Data(contentsOf: URL(fileURLWithPath: path))
                var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
                guard let p = InstrPatchIO.withCStrings([path], { fms_player_open($0, 1, &err, err.count) }) else { throw InstrError(instrCStr(err)) }
                defer { fms_player_close(p) }
                var info = fms_info_t(); fms_player_info(p, &info)
                for i in 0..<info.npatches {
                    guard let patch = PatchLines.read(p, i, fs: info.fs, parameters: parameters) else { throw InstrError("patch extraction") }
                    let original = PatchOrigin(paths: [path], patchIndex: Int(i), bytes: bytes)
                    let candidate = try PatchWriteback.prepare(original, patch: patch)
                    check(candidate.files == [bytes] && candidate.target == nil, "no-edit byte identity")
                    patches += 1
                }
                if path.contains("/recipes/") { check(fms_player_is_patch_file(p) == 1, "recipe patch-file classification") }
                records += 1
            } catch { check(false, "\(URL(fileURLWithPath: path).lastPathComponent): \(error)") }
        }
        do {
            guard let sine = args.first(where: { $0.hasSuffix("/48000/sine.tl") }) else { throw InstrError("sine fixture missing") }
            let data = try Data(contentsOf: URL(fileURLWithPath: sine))
            // Split one accepted record into header and events, matching music
            // pools. Non-patch bytes include CRLF, blank lines and a UTF8 comment.
            var header = Data("# preserve café bytes\r\n\r\n".utf8), events = Data()
            for line in PatchWriteback.lines(data) {
                if line.tokens.first?.first?.isNumber == true { events.append(line.bytes) }
                else { header.append(line.bytes) }
            }
            let a = output.appendingPathComponent("pool-header.tl"), b = output.appendingPathComponent("pool-events.tl")
            try header.write(to: a); try events.write(to: b)
            let paths = [a.path, b.path]
            var patch = try read(paths); let name = patch.name
            var op = patch.op(0); op.level_db10 += 10; patch.setOp(0, op); patch.validate()
            let origin = PatchOrigin(paths: paths, patchIndex: 0, files: [header, events])
            let candidate = try PatchWriteback.prepare(origin, patch: patch)
            check(candidate.target == 0 && candidate.files[1] == events, "single-write pool target")
            let sealed = try await candidate.sealed()
            check(untouched(sealed.files[0], name) == untouched(header, name) && sealed.files[1] == events, "other bytes unchanged after hash")
            check(untouched(sealed.files[0] + Data("# lost-byte control\n".utf8), name) != untouched(header, name), "outside-byte mutation rejected")
            // A change to any pool member during the render blocks publication.
            let external = events + Data("# changed while rendering\n".utf8); try external.write(to: b)
            var refused = false
            do { try sealed.publish() } catch { refused = true }
            check(refused && (try? Data(contentsOf: a)) == header && (try? Data(contentsOf: b)) == external, "pool conflict preserves both files")
            try events.write(to: b); try sealed.publish()
            check((try? Data(contentsOf: a)) == sealed.files[0], "atomic single-file publication")
            let noEdit = try PatchWriteback.prepare(PatchOrigin(paths: paths, patchIndex: 0, files: sealed.files), patch: read(paths))
            check(noEdit.target == nil && noEdit.files == sealed.files, "post-save identity")
            // The same splice retains spatial headers and every pos/move event.
            guard let spatial = args.first(where: { $0.contains("/space/") && $0.hasSuffix("/panlaw.tl") }) else { throw InstrError("spatial fixture missing") }
            let spaceBytes = try Data(contentsOf: URL(fileURLWithPath: spatial))
            var spacePatch = try read([spatial]); var spaceOp = spacePatch.op(0)
            spaceOp.level_db10 += 1; spacePatch.setOp(0, spaceOp)
            let spatialCandidate = try PatchWriteback.prepare(PatchOrigin(paths: [spatial], patchIndex: 0, bytes: spaceBytes), patch: spacePatch)
            let spatialSealed = try await spatialCandidate.sealed()
            check(untouched(spatialSealed.files[0], spacePatch.name) == untouched(spaceBytes, spacePatch.name), "spatial headers and events unchanged")
            // Copy the result for harness replay; never publish to the source.
            try spatialSealed.files[0].write(to: output.appendingPathComponent("spatial.tl"))
            // Shared context and two-target writes are not implemented.
            var bankEdit = patch; _ = bankEdit.addStatement("harmonic")
            refused = false
            do { _ = try PatchWriteback.prepare(PatchOrigin(paths: paths, patchIndex: 0, files: sealed.files), patch: bankEdit) }
            catch { refused = String(describing: error).contains("cannot change shared bank/source context") }
            check(refused && (try? Data(contentsOf: a)) == sealed.files[0]
                  && (try? Data(contentsOf: b)) == events, "unsupported shared context preserves all files")
            var hash = Data(), split = Data()
            for line in PatchWriteback.lines(sealed.files[0]) {
                if line.tokens.first == "hash" { hash.append(line.bytes) } else { split.append(line.bytes) }
            }
            let c = output.appendingPathComponent("split-header.tl"), d = output.appendingPathComponent("split-events.tl")
            try split.write(to: c); try (events + hash).write(to: d)
            var splitPatch = try read([c.path, d.path]); op = splitPatch.op(0); op.level_db10 += 10; splitPatch.setOp(0, op)
            refused = false
            do { _ = try PatchWriteback.prepare(PatchOrigin(paths: [c.path, d.path], patchIndex: 0, files: [split, events + hash]), patch: splitPatch) }
            catch { refused = String(describing: error).contains("cannot update multiple source files") }
            check(refused && (try? Data(contentsOf: c)) == split && (try? Data(contentsOf: d)) == events + hash, "unsupported two-target write preserves files")
        } catch { check(false, "edited record: \(error)") }
        if failures == 0 { print("FILES ok records=\(records) patches=\(patches) noedit=byte-identical pool=single-write holds=2 controls=3") }
        else { print("FILES FAIL count=\(failures)") }
        exit(failures == 0 ? 0 : 1)
    }
}
