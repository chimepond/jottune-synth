// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// Recipes.swift: where the Instrument tab's patches come from. A recipe is a
// small record the Makefile writes from recipes.txt; its patch 0 is read by
// the harness's own parser through the bridge, never by a parser of our own.

import Foundation
import SwiftUI

/// One recipe record under build/recipes.
struct RecipeEntry: Identifiable, Hashable, Sendable {
    let id: String      // the recipe's name (file name without .tl)
    let url: URL
    var error: String? = nil
}

/// Where the patch on the Instrument tab came from.
enum InstrSource: Hashable {
    case builtin
    case recipe(String)
    case record(Int)
    case file(String, Int)
}

@MainActor final class RecipeLibrary: ObservableObject {
    static var toolsAvailable: Bool {
        let plist = Bundle.main.infoDictionary ?? [:]
        return ["FMSRecipeTool", "FMSMusicDir", "FMSHarness"].allSatisfy {
            guard let path = plist[$0] as? String, !path.isEmpty else { return false }
            return FileManager.default.fileExists(atPath: path)
        }
    }
    @Published private(set) var entries: [RecipeEntry] = []
    @Published var loadError: String?
    @Published private(set) var importing = false
    private var importedDirectory: URL?

    deinit { if let dir = importedDirectory { try? FileManager.default.removeItem(at: dir) } }

    /// Only the existing composer owns recipe grammar and expansion. The
    /// chosen source is read-only; generated records live in a private folder.
    func importFile(_ source: URL) async {
        guard !importing else { return }
        importing = true; defer { importing = false }
        let plist = Bundle.main.infoDictionary ?? [:]
        let keys = ["FMSRecipeTool", "FMSMusicDir", "FMSHarness", "FMSEngineLine"]
        let args = keys.compactMap { plist[$0] as? String }
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("fmspike-recipes-\(UUID().uuidString)", isDirectory: true)
        let result: [RecipeEntry] = await Task.detached(priority: .userInitiated) {
            struct Row: Decodable { let name: String; let file: String?; let error: String? }
            do {
                guard args.count == keys.count else { throw InstrError("Recipe import paths are missing from this app build.") }
                try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                let run = Process(), pipe = Pipe()
                run.executableURL = URL(fileURLWithPath: "/usr/bin/env")
                run.arguments = ["python3", "-B", "-I", args[0], args[1], args[2], args[3], dir.path, source.path]
                run.standardOutput = pipe; run.standardError = pipe
                try run.run()
                let output = pipe.fileHandleForReading.readDataToEndOfFile(); run.waitUntilExit()
                guard run.terminationStatus == 0 else {
                    throw InstrError(String(data: output, encoding: .utf8) ?? "Recipe import process failed.")
                }
                let rows = try JSONDecoder().decode([Row].self, from: Data(contentsOf: dir.appendingPathComponent("manifest.json")))
                return rows.map { RecipeEntry(id: $0.name, url: dir.appendingPathComponent($0.file ?? "failed"), error: $0.error) }
            } catch { return [RecipeEntry(id: source.lastPathComponent, url: source, error: String(describing: error))] }
        }.value
        if let old = importedDirectory { try? FileManager.default.removeItem(at: old) }
        importedDirectory = dir; entries = result
        let failures = result.filter { $0.error != nil }.count
        loadError = failures == 0 ? nil : "\(failures) recipe import error(s); select a marked entry for its diagnostic."
    }

    func refresh(_ dir: URL) {
        if importing { return }
        if let old = importedDirectory { try? FileManager.default.removeItem(at: old); importedDirectory = nil }
        loadError = nil
        let fm = FileManager.default
        let names = (try? fm.contentsOfDirectory(atPath: dir.path)) ?? []
        entries = names.filter { $0.hasSuffix(".tl") }.sorted().map {
            RecipeEntry(id: String($0.dropLast(3)), url: dir.appendingPathComponent($0))
        }
    }
}

/// A selected patch keeps its complete source pool and parser index. A later
/// live arm must not lose this origin or silently turn it into another file.
struct PatchOrigin: Equatable {
    let paths: [String]
    let patchIndex: Int
    var patchFile = false
    var bytes: Data?
    var files: [Data]?
}

enum InstrPatchIO {
    /// Runs body with the strings as a C array of C strings.
    static func withCStrings<R>(_ strs: [String], _ body: (UnsafePointer<UnsafePointer<CChar>?>) -> R) -> R {
        let dup = strs.map { strdup($0) }
        defer { dup.forEach { free($0) } }
        let ptrs: [UnsafePointer<CChar>?] = dup.map { UnsafePointer($0) }
        return ptrs.withUnsafeBufferPointer { body($0.baseAddress!) }
    }

    /// Opens the files as one pool and reads every line of patch 0. Parser
    /// diagnostics use the bridge's serialized capture, off the audio thread.
    /// The third value is the canonical round-trip label check (instrDropped).
    @MainActor static func loadPatch0(paths: [String]) -> Result<(PatchLines, String, String), InstrError> {
        var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        let p = withCStrings(paths) { fms_player_open($0, Int32(paths.count), &err, err.count) }
        guard let p else { return .failure(InstrError(instrCStr(err))) }
        defer { fms_player_close(p) }
        var info = fms_info_t()
        fms_player_info(p, &info)
        guard let patch = PatchLines.read(p, 0, fs: info.fs) else { return .failure(InstrError("the record has no patch 0")) }
        if patch.hasGenerator { return .failure(InstrError("Generator-bound patches are read-only in Records; live bank editing is not implemented.")) }
        return .success((patch, patch.name, instrDropped(p, 0)))
    }

    /// Join accepted token rows under the chosen name; no validation or capture.
    @MainActor static func patchText(_ patch: PatchLines, name: String) -> String {
        let text = patch.text
        let n = fms_patch_text(text, name, nil, 0)
        guard n > 0 else { return "(patch name refused)" }
        var b = [CChar](repeating: 0, count: n)
        return fms_patch_text(text, name, &b, n) == n ? instrCStr(b) : "(patch text refused)"
    }

    /// An export may change only rows named exactly by its pending comments.
    /// Compare accepted token rows, not a second interpretation of the grammar.
    static func checkRecipeExport(original: PatchLines, replay: PatchLines, recipe: String, authored: PatchLines? = nil) throws -> Int {
        let pending = recipe.split(separator: "\n").filter { $0.hasPrefix("# pending: ") }.map { String($0.dropFirst(11)) }
        let before = original.rows.filter(\.enabled), after = replay.rows.filter(\.enabled)
        let source = (authored ?? original).rows.filter(\.enabled)
        func groups(_ rows: [PatchLines.Line]) -> [String: [String]] {
            Dictionary(grouping: rows, by: { PatchWriteback.key($0.tokens) }).mapValues { $0.map(\.text).sorted() }
        }
        let old = groups(before), new = groups(after), raw = groups(source)
        var changed = Set(old.keys).union(new.keys).filter { old[$0] != new[$0] }
        // An authored row omitted even by native canonical output still keeps
        // its exact payload, rather than disappearing from the recipe export.
        changed.formUnion(Set(raw.keys).subtracting(old.keys))
        guard changed.allSatisfy({ raw[$0] != nil }) else { throw InstrError("Recipe re-import added an unexpected statement. No export was written.") }
        let expected = source.filter { changed.contains(PatchWriteback.key($0.tokens)) }.map(\.text)
        guard pending == expected else {
            throw InstrError("Recipe re-import changes a statement without its exact original pending comment. No export was written.")
        }
        return pending.count
    }

    /// Validate the represented subset in the actual composer and native
    /// renderer before publishing a recipe. The selected model is untouched.
    @MainActor static func exportRecipe(_ patch: PatchLines, name: String, to target: URL) async throws -> Int {
        var authored = PatchLines(patchText(patch, name: name), fs: patch.fs); authored.validate()
        if let diagnostic = authored.diagnostic { throw InstrError(diagnostic) }
        let plist = Bundle.main.infoDictionary ?? [:]
        let keys = ["FMSRecipeTool", "FMSMusicDir", "FMSHarness", "FMSEngineLine"]
        let args = keys.compactMap { plist[$0] as? String }
        guard args.count == keys.count else { throw InstrError("Recipe export paths are missing from this app build.") }
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("fmspike-export-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let record = dir.appendingPathComponent("source.tl")
        try patchRecordBody(authored, name: name).write(to: record, atomically: true, encoding: .utf8)
        var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        guard let player = withCStrings([record.path], { fms_player_open($0, 1, &err, err.count) }) else { throw InstrError(instrCStr(err)) }
        let canonical = PatchLines.read(player, 0, fs: patch.fs); fms_player_close(player)
        guard let original = canonical else { throw InstrError("The export source has no patch.") }
        let source = dir.appendingPathComponent("patch-lines.txt")
        try original.text.write(to: source, atomically: true, encoding: .utf8)
        try await Task.detached(priority: .userInitiated) {
            let run = Process(), pipe = Pipe()
            run.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            run.arguments = ["python3", "-B", "-I", args[0], "--export", args[1], args[2], args[3], dir.path, source.path, name]
            run.standardOutput = pipe; run.standardError = pipe
            try run.run(); let output = pipe.fileHandleForReading.readDataToEndOfFile(); run.waitUntilExit()
            guard run.terminationStatus == 0 else {
                throw InstrError(String(data: output, encoding: .utf8) ?? "Recipe export process failed.")
            }
        }.value
        let candidate = try String(contentsOf: dir.appendingPathComponent("export-recipes.txt"), encoding: .utf8)
        let claims = candidate.split(separator: "\n").filter { $0.hasPrefix("# pending: ") }.map { String($0.dropFirst(11)) }
        guard claims.allSatisfy({ claim in original.rows.contains { $0.text == claim } }) else { throw InstrError("Recipe export has an unknown pending statement.") }
        var pendingKeys = Set(original.rows.filter { claims.contains($0.text) }.map { PatchWriteback.key($0.tokens) })
        pendingKeys.formUnion(Set(authored.rows.map { PatchWriteback.key($0.tokens) }).subtracting(original.rows.map { PatchWriteback.key($0.tokens) }))
        let pending = authored.rows.filter { $0.enabled && pendingKeys.contains(PatchWriteback.key($0.tokens)) }.map { "# pending: " + $0.text }
        let body = candidate.split(separator: "\n").filter { !$0.hasPrefix("# pending: ") }.map(String.init)
        let recipe = (body + pending).joined(separator: "\n") + "\n"
        let preview = dir.appendingPathComponent("preview").appendingPathComponent(name + ".tl")
        switch loadPatch0(paths: [preview.path]) {
        case .failure(let error): throw error
        case .success(let (replay, _, _)):
            let pending = try checkRecipeExport(original: original, replay: replay, recipe: recipe, authored: authored)
            try recipe.write(to: target, atomically: true, encoding: .utf8)
            return pending
        }
    }

    /// Save a timeline plus its native offline WAV. Each file is atomic,
    /// but the pair is not a multi-file transaction: a companion failure
    /// reports the already-saved timeline and preserves any old WAV.
    static func saveTake(_ text: String, to url: URL) async -> String? {
        let wav = url.deletingPathExtension().appendingPathExtension("wav")
        guard url.pathExtension.lowercased() != "wav",
              url.standardizedFileURL.resolvingSymlinksInPath() != wav.standardizedFileURL.resolvingSymlinksInPath() else {
            return "Timeline and WAV paths collide; choose a timeline filename such as take.tl."
        }
        return await Task.detached(priority: .userInitiated) { () -> String? in
            let temp = FileManager.default.temporaryDirectory.appendingPathComponent("fmspike-save-\(UUID().uuidString).tl")
            do { try text.write(to: temp, atomically: true, encoding: .utf8) }
            catch { return "save: \(error.localizedDescription)" }
            defer { try? FileManager.default.removeItem(at: temp) }
            var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            guard let player = withCStrings([temp.path], { fms_player_open($0, 1, &err, err.count) }) else {
                return "save: " + instrCStr(err)
            }
            defer { fms_player_close(player) }
            var info = fms_info_t(); fms_player_info(player, &info)
            guard info.have_hash != 0 else { return "save: the take has no hash line" }
            do { try text.write(to: url, atomically: true, encoding: .utf8) }
            catch { return "save: \(error.localizedDescription)" }
            if fms_player_write_wav(player, wav.path, &err, err.count) != 0 {
                return "Timeline saved; WAV failed: " + instrCStr(err)
            }
            return nil
        }.value
    }

    /// Render a record completely, inspecting status before resetting its state.
    /// The legacy C render_all helper rewinds before returning and clears the
    /// error flag; its returned zero cannot distinguish failure from a hash.
    /// Call only off the audio thread with exclusive ownership of the player.
    static func renderChecked(_ player: OpaquePointer?, chunk: UInt32) throws -> (hash: UInt64, clipped: UInt32) {
        var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        guard fms_player_seek(player, 0, &err, err.count) == 0 else {
            throw InstrError("Record render refused: " + instrCStr(err))
        }
        defer { fms_player_rewind(player) }
        var info = fms_info_t(); fms_player_info(player, &info)
        guard info.channels > 0 else { throw InstrError("Record render has no output channels.") }
        // A native-accepted empty record is complete without allocating a buffer.
        if info.frames == 0 {
            guard fms_player_error(player) == 0, fms_player_position(player) == 0 else {
                throw InstrError("Empty record render did not complete.")
            }
            return (fms_player_hash(player), fms_player_clipped(player))
        }
        let frames = chunk == 0 ? info.frames : min(chunk, info.frames)
        guard let frameCount = Int(exactly: frames), let channels = Int(exactly: info.channels) else {
            throw InstrError("Record render buffer dimensions overflow.")
        }
        let (samples, sampleOverflow) = frameCount.multipliedReportingOverflow(by: channels)
        let (bytes, byteOverflow) = samples.multipliedReportingOverflow(by: MemoryLayout<Int16>.stride)
        guard !sampleOverflow, !byteOverflow, samples > 0, bytes > 0 else {
            throw InstrError("Record render buffer size overflows.")
        }
        guard let storage = calloc(1, bytes) else { throw InstrError("Record render buffer allocation failed.") }
        defer { free(storage) }
        let buffer = storage.bindMemory(to: Int16.self, capacity: samples)
        var completed: UInt32 = 0
        while completed < info.frames {
            let want = min(frames, info.frames - completed)
            let got = fms_player_render(player, buffer, want)
            let status = fms_player_error(player)
            guard status == 0, got == want, fms_player_position(player) == completed + want else {
                throw InstrError("Record render failed: code=\(status), frame=\(fms_player_position(player))/\(info.frames), returned=\(got)/\(want).")
            }
            completed += want
        }
        guard fms_player_error(player) == 0, fms_player_position(player) == info.frames else {
            throw InstrError("Record render did not complete.")
        }
        return (fms_player_hash(player), fms_player_clipped(player))
    }

    /// Writes a take (or any record text) to a temp file, opens it (main
    /// thread, see loadPatch0), renders it whole off the main thread and
    /// compares the hash with its hash line.
    @MainActor static func verifyText(_ text: String) async -> String {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("fmspike-take-\(UUID().uuidString).tl")
        do { try text.write(to: url, atomically: true, encoding: .utf8) } catch {
            return "could not write \(url.lastPathComponent): \(error.localizedDescription)"
        }
        defer { try? FileManager.default.removeItem(at: url) }
        var expected: UInt64?
        for line in text.split(separator: "\n") {
            let t = line.split(separator: " ")
            if t.count == 2, t[0] == "hash" { expected = UInt64(t[1], radix: 16) }
        }
        var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        guard let p = withCStrings([url.path], { fms_player_open($0, 1, &err, err.count) }) else {
            return "refused: " + instrCStr(err)
        }
        let box = InstrPlayerBox(p)
        do {
            let (h, clipped) = try await Task.detached(priority: .userInitiated) {
                defer { fms_player_close(box.p) }
                return try renderChecked(box.p, chunk: 4096)
            }.value
            let hs = String(format: "%016llx", h)
            guard let expected else { return "rendered hash \(hs), but the take has no hash line" }
            return expected == h ? "match: \(hs) (clipped \(clipped))"
                                 : "MISMATCH: rendered \(hs), take says \(String(format: "%016llx", expected))"
        } catch { return "render refused: \(error)" }
    }

    /// The existing recipe-record form, at the patch's source sample rate.
    /// Spatial/effect/event state belongs to a record and is not copied here.
    @MainActor static func patchFileBody(_ patch: PatchLines, name: String) throws -> String {
        if let diagnostic = patch.diagnostic { throw InstrError(diagnostic) }
        if patch.hasGenerator { throw InstrError("Generator-bound patches remain read-only in Records.") }
        return patchRecordBody(patch, name: name)
    }

    /// Shared one-patch form; export also uses it privately for metadata-only
    /// canonicalization, including source context that may become pending.
    @MainActor private static func patchRecordBody(_ patch: PatchLines, name: String) -> String {
        var engine = [CChar](repeating: 0, count: 160); fms_engine_line(&engine, engine.count)
        return "timeline draft2\nengine \(instrCStr(engine))\nfs \(patch.fs)\nlength_ns 2500000000\nspace off\nstreams main\n"
            + patchText(patch, name: name) + "voice 0 \(name)\n0 main 0 on 261626 26000\n1500000000 main 0 off\n"
    }

    /// Validate and render a private candidate before the caller publishes it.
    /// No WAV is produced, and a render error cannot become a hash statement.
    static func sealRecord(_ body: String) async throws -> String {
        try await Task.detached(priority: .userInitiated) {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("fmspike-patch-\(UUID().uuidString).tl")
            try Data(body.utf8).write(to: url, options: .atomic)
            defer { try? FileManager.default.removeItem(at: url) }
            var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            guard let p = withCStrings([url.path], { fms_player_open($0, 1, &err, err.count) }) else { throw InstrError(instrCStr(err)) }
            defer { fms_player_close(p) }
            let (hash, _) = try renderChecked(p, chunk: 4096)
            return body + "hash " + String(format: "%016llx", hash) + "\n"
        }.value
    }

    /// A plain operator: ratio 1, full level, no modulation, a short ADSR.
    static func plainOp() -> fms_op_t {
        var o = fms_op_t()
        o.ratio_q16 = 65536
        o.vel_sens_q8 = 128
        o.attack_ms = 5; o.decay_ms = 400; o.sustain_db10 = 120; o.release_ms = 300
        return o
    }

    /// The built-in 2-operator patch: ratio 1:1, modulator index 2 rad decaying.
    static func defaultPatch() -> PatchLines {
        PatchLines("patch default 0 65536 60 0 0 128 5 400 120 300\npatch default 1 65536 0 2048 0 128 2 600 240 300\n")
    }

}

/// A player handed to one background task, which closes it.
struct InstrPlayerBox: @unchecked Sendable {
    let p: OpaquePointer
    init(_ p: OpaquePointer) { self.p = p }
}

/// The statements of patch i of p that its PatchLines copy loses, in whole
/// or in part (fms_player_patch_dropped), space separated; "" when the round trip is
/// exact. Writes temp files: call off the audio thread.
func instrDropped(_ p: OpaquePointer, _ i: UInt32) -> String {
    var buf = [CChar](repeating: 0, count: 256)
    let n = fms_player_patch_dropped(p, i, &buf, buf.count)
    if n < 0 { return "?" }   // the check failed (instrDropLabel says so)
    return n == 0 ? "" : instrCStr(buf)
}

/// An accepted patch round-trips exactly. A failure is a build/validation
/// problem, never permission to audition a partially copied patch.
func instrDropLabel(_ dropped: String) -> String? {
    if dropped.isEmpty { return nil }
    if dropped == "?" { return "Patch line validation failed. Rebuild against the record's engine and inspect its diagnostic." }
    return "Patch line round trip changed \(dropped.split(separator: " ").joined(separator: ", ")). Rebuild against the record's engine."
}

/// A NUL-terminated C buffer as a String.
func instrCStr(_ a: [CChar]) -> String {
    let bytes = a.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }
    return String(decoding: bytes, as: UTF8.self)
}

struct InstrError: Error, CustomStringConvertible {
    let description: String
    init(_ s: String) { description = s }
}

/// Byte-preserving editing of already accepted records. This recognizes only
/// statement identity (word, patch name, optional operator/slot); field syntax
/// and all semantic validity remain the harness parser's responsibility.
struct PatchWriteback: Sendable {
    static let context: Set<String> = ["gen_clock", "gen", "gen_axiom", "gen_rule", "wtbank", "wtdata", "wthash", "harmonic"]
    static let keyed: Set<String> = ["patch", "mod", "eg", "ks", "noise", "pd", "formant", "wtuse", "ksource", "kstype", "fbtype", "egloop"]
    static let scoped: Set<String> = keyed.union(["route", "filter", "shape", "amp", "lfo", "peg", "fenv", "vector"])
    let paths: [String]
    let original: [Data]
    var files: [Data]
    let target: Int?

    struct ByteLine {
        let bytes: Data
        var tokens: [String] {
            let body = bytes.prefix { $0 != 35 }
            return String(decoding: body, as: UTF8.self).split(whereSeparator: { $0.isWhitespace }).map(String.init)
        }
        var newline: Data { bytes.suffix(2) == Data([13, 10]) ? Data([13, 10]) : Data([10]) }
        func replacingHash(_ value: String) -> Data {
            // Replace only the accepted value token, leaving indentation,
            // spacing, trailing comment and original line ending untouched.
            let a = Array(bytes); var i = 0, ranges: [Range<Int>] = []
            while i < a.count && a[i] != 35 {
                if a[i] == 32 || a[i] == 9 || a[i] == 13 || a[i] == 10 { i += 1; continue }
                let start = i
                while i < a.count && ![UInt8(32),9,13,10,35].contains(a[i]) { i += 1 }
                ranges.append(start..<i)
            }
            guard ranges.count >= 2 else { return bytes }
            return Data(a[..<ranges[1].lowerBound]) + Data(value.utf8) + Data(a[ranges[1].upperBound...])
        }
    }
    static func lines(_ data: Data) -> [ByteLine] {
        var out: [ByteLine] = [], start = data.startIndex
        for i in data.indices where data[i] == 10 {
            out.append(ByteLine(bytes: data.subdata(in: start..<(i + 1)))); start = i + 1
        }
        if start < data.endIndex { out.append(ByteLine(bytes: data.subdata(in: start..<data.endIndex))) }
        return out
    }
    static func key(_ tokens: [String]) -> String {
        let kind = tokens.first ?? ""
        return keyed.contains(kind) && tokens.count > 2 ? kind + "." + tokens[2] : kind
    }

    @MainActor static func prepare(_ origin: PatchOrigin, patch: PatchLines) throws -> PatchWriteback {
        guard !origin.paths.isEmpty else { throw InstrError("Open the source record before Write Back.") }
        let original = try origin.paths.map { try Data(contentsOf: URL(fileURLWithPath: $0)) }
        guard let expected = origin.files ?? origin.bytes.map({ [$0] }), original == expected else {
            throw InstrError("The source files changed; reopen the record before Write Back.")
        }
        var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        guard let player = InstrPatchIO.withCStrings(origin.paths, { fms_player_open($0, Int32(origin.paths.count), &err, err.count) }) else {
            throw InstrError(instrCStr(err))
        }
        defer { fms_player_close(player) }
        var info = fms_info_t(); fms_player_info(player, &info)
        guard origin.patchIndex >= 0, let before = PatchLines.read(player, UInt32(origin.patchIndex), fs: info.fs) else {
            throw InstrError("The selected patch is no longer in the record.")
        }
        if let diagnostic = patch.diagnostic { throw InstrError(diagnostic) }
        let named = InstrPatchIO.patchText(patch, name: before.name)
        guard fms_patch_lines_check(named, named.utf8.count, info.fs, &err, err.count) == 0 else { throw InstrError(instrCStr(err)) }
        let after = PatchLines(named, fs: info.fs)
        let oldContext = before.rows.filter { context.contains($0.statement) }
        let newContext = after.rows.filter { context.contains($0.statement) }
        guard oldContext == newContext else {
            throw InstrError("Write Back cannot change shared bank/source context. Save As keeps the complete patch in a new file.")
        }
        if before.text == after.text { return PatchWriteback(paths: origin.paths, original: original, files: original, target: nil) }
        var oldRows: [String: String] = [:], newRows: [String: String] = [:]
        for row in before.rows where !context.contains(row.statement) { oldRows[key(row.tokens)] = row.text }
        for row in after.rows where !context.contains(row.statement) {
            guard scoped.contains(row.statement), newRows[key(row.tokens)] == nil else { throw InstrError("This statement cannot be spliced unambiguously; rebuild the app before Write Back.") }
            newRows[key(row.tokens)] = row.text
        }
        let changed = Set(oldRows.keys).union(newRows.keys).filter { oldRows[$0] != newRows[$0] }
        var buffers = original.map(lines), written: Set<String> = [], definitionFile: Int?
        var hashFile: Int?
        for file in buffers.indices {
            var out: [ByteLine] = []
            for line in buffers[file] {
                let t = line.tokens
                if t.first == "hash" { hashFile = file }
                if t.count >= 2 && scoped.contains(t[0]) && t[1] == before.name {
                    if definitionFile == nil { definitionFile = file }
                    let k = key(t)
                    if changed.contains(k) {
                        if let replacement = newRows[k], written.insert(k).inserted {
                            out.append(ByteLine(bytes: Data(replacement.utf8) + line.newline))
                        }
                        continue
                    }
                }
                out.append(line)
            }
            buffers[file] = out
        }
        guard let definitionFile else { throw InstrError("The original patch lines were not found.") }
        let additions = after.rows.filter { changed.contains(key($0.tokens)) && !written.contains(key($0.tokens)) && !context.contains($0.statement) }
        if !additions.isEmpty {
            // New header statements follow existing asset declarations and
            // precede the first accepted event (or final hash/end).
            let at = buffers[definitionFile].firstIndex { line in
                guard let word = line.tokens.first else { return false }
                return word == "hash" || word.first?.isNumber == true
            } ?? buffers[definitionFile].endIndex
            var extra = additions.map { ByteLine(bytes: Data(($0.text + "\n").utf8)) }
            if at > 0 && buffers[definitionFile][at - 1].bytes.last != 10 { extra.insert(ByteLine(bytes: Data([10])), at: 0) }
            buffers[definitionFile].insert(contentsOf: extra, at: at)
        }
        let files = buffers.map { $0.reduce(into: Data()) { $0.append($1.bytes) } }
        var affected = Set(files.indices.filter { files[$0] != original[$0] })
        affected.insert(hashFile ?? definitionFile)
        guard affected.count == 1, let target = affected.first else {
            throw InstrError("Write Back cannot update multiple source files. No files were changed; use Save As for a complete patch.")
        }
        return PatchWriteback(paths: origin.paths, original: original, files: files, target: target)
    }

    /// Validate/render the complete candidate pool, preserving event ordering.
    /// The caller publishes only after this returns and source bytes still match.
    func sealed() async throws -> PatchWriteback {
        guard let target else { return self }
        return try await Task.detached(priority: .userInitiated) {
            let dir = FileManager.default.temporaryDirectory.appendingPathComponent("fmspike-writeback-\(UUID().uuidString)")
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: false)
            defer { try? FileManager.default.removeItem(at: dir) }
            var scratch: [String] = []
            for (i, bytes) in files.enumerated() {
                let url = dir.appendingPathComponent("\(i).tl"); try bytes.write(to: url); scratch.append(url.path)
            }
            var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            guard let p = InstrPatchIO.withCStrings(scratch, { fms_player_open($0, Int32(scratch.count), &err, err.count) }) else {
                throw InstrError("Candidate refused; no source files changed: " + instrCStr(err))
            }
            defer { fms_player_close(p) }
            let (hash, _) = try InstrPatchIO.renderChecked(p, chunk: 4096)
            let hex = String(format: "%016llx", hash)
            var found = false, bytes = Data()
            for line in Self.lines(files[target]) {
                if line.tokens.first == "hash" { bytes.append(line.replacingHash(hex)); found = true }
                else { bytes.append(line.bytes) }
            }
            if !found {
                if !bytes.isEmpty && bytes.last != 10 { bytes.append(10) }
                bytes.append(Data("hash \(hex)\n".utf8))
            }
            var result = self; result.files[target] = bytes
            return result
        }.value
    }

    func publish() throws {
        let current = try paths.map { try Data(contentsOf: URL(fileURLWithPath: $0)) }
        guard current == original else { throw InstrError("The source changed while rendering; no files were written.") }
        if let target { try files[target].write(to: URL(fileURLWithPath: paths[target]), options: .atomic) }
    }
}
