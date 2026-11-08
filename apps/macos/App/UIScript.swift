// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// UIScript.swift: the app entry point and offline script mode.
// FMS_SCRIPT=steps.txt runs the steps through
// the setters the controls call (InstrModel.set and press, the piano's
// VoiceAllocator) and through Engine, with no audio device: Engine renders
// through the host callback offline, at fixed frames, so every run is exact.
// Then the app quits: 0 when every step passed, 1 otherwise.
//
// Steps, one per line ('#' starts a comment):
//   open RECORD             a record under the repository root, as the Records list opens it
//   play                    Records' play
//   set NAME VALUE          a control by name (InstrModel.swift lists them);
//                           button.NAME presses a button (VALUE: its argument,
//                           a file name under the output folder for button.save)
//   note ON|OFF KEY [VEL]   the piano: a MIDI key, velocity Q15 (default: the slider's)
//   wait FRAMES|end         renders FRAMES (end: to the record's end); fails when the scope or the meters
//                           differ from the samples rendered
//   snap NAME [same|changed OTHER]
//                           draws the Instrument panels offscreen (ImageRenderer)
//                           to NAME.png, compared with OTHER as changed or not
//   hash [record]           logs the running hash and the hash since the last
//                           hash step; "record": it must equal the record's
//   expect WHAT VALUE       a control's value, or live, playing, recording,
//                           dirty, take, clipboard, recipes, dropped (the
//                           picked patch lost a statement) (0 or 1), advance
//                           (the frames the last wait moved), statements (the
//                           first words of the patch text, once each),
//                           label.device, label.audition, label.rearm (the
//                            labels the views show, 0 or 1)
//   journalcheck            bounded patch undo/redo, invalid/disabled rows and excluded state
//   rawcheck                incomplete raw rows: safe views and typed repair, no audio
//   librarycheck            song search/variants and explicit direct TL playback
//   devicecheck             fake-backend switch/restore/timeout and no-click controls
//   device RATE             the rate the output device runs at (offline there is
//                           none: 0), for label.device; snap draws the labels
//   flood N                 N live events on the armed player (voice 0's note
//                           on and off in turn), 512 at a time with 64 frames
//                           rendered between, so the queue never fills: fills
//                           a take's event log; expect
//                           take.full
//   midi HEX...             one MIDI 1.0 message, as live MIDI input gets it
//                           (MidiInput.feedBytes; shared MIDI translator);
//                           expect midi.messages, midi.notes, midi.events,
//                           midi.notlive (refused: no live player),
//                           midi.dropped, midi.refused (512 notes down or a cut packet),
//                           midi.ignored, midi.steals, midi.pitch, midi.filter
//   seekcheck               prefix/continuation, paused/play, invalid and stale-load controls
//   rewind                  Records' rewind; expect players.pending (players
//                           swapped out and not yet closed or rewound)
//   space NAME AZ EL [same|changed OTHER]
//                           draws the open record's 3D space view offscreen to
//                           NAME.png, turned AZ and tilted EL degrees;
//                           expect space.points (how many
//                           path points), space.path V (voice V's points, as
//                           pathText writes them) and space.pos V (x,y,z now)
//   stereo NAME [SECONDS]   renders the open two-channel record to its end
//                           through a second player, as the stereo picture
//                           does, with analyze.py's window (4 s) or SECONDS;
//                           writes NAME.wav (the frames) and NAME.tsv (start s,
//                           length s, side-mid dB, corr per window) for uitest.sh to set
//                           against analyze.py, and draws the goniometer and
//                           the strip to NAME.png; fails when the frames'
//                           hash is not the record's; expect stereo.rows
//   check NAME VALUE [same|inactive|any] [rms+|rms-|hf+|hf-|centroid+|centroid-] [retrig] [with NAME VALUE]
//                           two runs, without and with the step: arm, record,
//                           (retrig: the key for 25 ms and 25 ms of release first) the
//                           computer keys' A at the velocity slider's value,
//                           50 ms, the step for a live control (send, live.*,
//                           pos.x, pos.y) and the with step, 200 ms, key up, 100 ms, stop. Hook: the
//                           take read back from the bridge (header and events)
//                           differs. Sound: the frames after the step differ
//                           (same: equal; inactive: equal, and the editor dims
//                           the field; any: not checked), in the direction
//                           given. Record and display: each take's hash is the
//                           hash of the frames rendered, the scope and meters
//                           equal them; the takes go to checks/ for
//                           `harness --play`.
// Output (FMS_SCRIPT_OUT, default: beside the script in uitest-out): log.txt,
// the PNG snapshots, the takes. FMS_UITEST_DROP=NAME makes one control's
// setter do nothing, as if its binding were removed (the positive control).

import Accelerate
import AppKit
import CoreMIDI
import SwiftUI

@main
enum FMSpikeMain {
    static func main() {
        if CommandLine.arguments.dropFirst() == ["--device-probe"] {
            guard !Engine.offline else { print("DEVICE_PROBE refused in offline script mode; writes=0"); exit(1) }
            exit(HardwareDeviceRate.probe())
        }
        if Engine.offline {
            UIScript.main()
        } else {
            FMSpikeApp.main()
        }
    }
}

/// FNV-1a 64 over int16 samples, low byte first, as fm_golden_hash.
struct FNV64 {
    var h: UInt64 = 14695981039346656037
    mutating func add(_ s: [Int16]) {
        for v in s {
            let u = UInt16(bitPattern: v)
            h ^= UInt64(u & 0xFF); h = h &* 1099511628211
            h ^= UInt64(u >> 8); h = h &* 1099511628211
        }
    }
    static func of(_ s: [Int16]) -> UInt64 { var f = FNV64(); f.add(s); return f.h }
}

/// What one run of a check got.
private struct Probe {
    var take = ""
    var hook = ""             // the take without its hash line
    var all: [Int16] = []     // every frame of the take, interleaved
    var after: [Int16] = []   // the frames after the step
    var body: [Int16] = []    // 200 ms after the step, channel 0
    var display: String?
    var setError: String?
}

@MainActor final class UIScript {
    private let lines: [String]
    private let out: URL
    private var log: [String] = []
    private var failures = 0
    private var snaps: [String: Data] = [:]
    private var segment = FNV64()
    private var lastAdvance = 0
    private var m: InstrModel { InstrModel.shared }
    private var engine: Engine { Engine.shared }
    private let fs = 48000

    static func main() -> Never {
        let env = ProcessInfo.processInfo.environment
        let path = env["FMS_SCRIPT"] ?? ""
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        Task { @MainActor in
            let s = UIScript(path: path, out: env["FMS_SCRIPT_OUT"])
            let code = await s.run()
            exit(code)
        }
        app.run()
        exit(1)
    }

    init(path: String, out: String?) {
        let text = (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""
        lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        let dir = URL(fileURLWithPath: path).deletingLastPathComponent()
        self.out = out.map { URL(fileURLWithPath: $0, isDirectory: true) } ?? dir.appendingPathComponent("uitest-out")
        if text.isEmpty { failures += 1; say("FAIL cannot read the script \(path)") }
    }

    private func say(_ s: String) {
        log.append(s)
        print(s)
        fflush(stdout)
    }

    private func fail(_ s: String) { failures += 1; say("FAIL " + s) }

    func run() async -> Int32 {
        try? FileManager.default.createDirectory(at: out.appendingPathComponent("checks"), withIntermediateDirectories: true)
        // Fresh offline model mirrors first-session stereo. Explicitly choose
        // the historical mono baseline for the older independent controls.
        if m.get("space.field") != "stereo" { fail("SPACEPREF first session is not stereo") }
        m.set("stereo", "0")
        // What the Instrument tab does when it appears.
        m.press("rescan")
        m.snapshotRecord()
        let drop = m.dropped
        let line = Bundle.main.infoDictionary?["FMSEngineLine"] as? String ?? "?"
        say("UITEST engine=\(line) recipes=\(m.recipes.entries.count)" + (drop.isEmpty ? "" : " drop=\(drop)"))
        // Two snapshots thrown away: in every run seen (2026-10-17), the first
        // two renders of a state drew the spectrum's curve a few antialiased
        // pixels apart from the third and every later one, so "same" failed
        // and "changed" could pass on that alone.
        for _ in 0..<2 { _ = await snapshot() }
        for (n, raw) in lines.enumerated() {
            let line = raw.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init) ?? ""
            let t = line.split(separator: " ").map(String.init)
            guard let op = t.first else { continue }
            let before = failures
            await step(op, Array(t.dropFirst()), line: n + 1)
            if engine.errorText != nil && failures == before && op != "expect" {
                say("  note: error text: \(engine.errorText ?? "")")
            }
        }
        say(failures == 0 ? "UITEST ok" : "UITEST FAIL \(failures)")
        try? (log.joined(separator: "\n") + "\n").write(to: out.appendingPathComponent("log.txt"), atomically: true, encoding: .utf8)
        return failures == 0 ? 0 : 1
    }

    private func step(_ op: String, _ a: [String], line: Int) async {
        switch op {
        case "open":
            guard a.count == 1 else { return fail("line \(line): open RECORD") }
            engine.open(paths: [engine.recordsRoot.appendingPathComponent(a[0]).path], title: a[0])
            let want = engine.loadID
            for _ in 0..<6000 where engine.installedSerial < want { try? await Task.sleep(for: .milliseconds(5)) }
            m.snapshotRecord()   // the Instrument tab's onChange(of: engine.title)
            if let e = engine.errorText, engine.info == nil { return fail("open \(a[0]): \(e)") }
            let i = engine.info ?? fms_info_t()
            say("open \(a[0]) fs=\(i.fs) ch=\(i.channels) frames=\(i.frames) patches=\(engine.patches.count)")
        case "openurl":
            guard a.count == 1 else { return fail("line \(line): openurl RECORD|URL") }
            let url: URL
            if a[0].hasPrefix("link:") {
                var components = URLComponents(); components.scheme = RecordOpen.scheme; components.host = "open"
                components.queryItems = [URLQueryItem(name: "path", value: engine.recordsRoot.appendingPathComponent(String(a[0].dropFirst(5))).path)]
                guard let link = components.url else { return fail("openurl: malformed link") }; url = link
            } else if a[0].hasPrefix("fmspike:") {
                guard let link = URL(string: a[0]) else { return fail("openurl: malformed URL") }; url = link
            }
            else { url = engine.recordsRoot.appendingPathComponent(a[0]) }
            if let error = RecordOpen.open(url) { return fail("openurl: \(error)") }
            for _ in 0..<6000 where LocalRecords.shared.busy { try? await Task.sleep(for: .milliseconds(5)) }
            let want = engine.loadID
            for _ in 0..<6000 where engine.installedSerial < want { try? await Task.sleep(for: .milliseconds(5)) }
            m.snapshotRecord()
            say("openurl \(a[0]) title=\(engine.title) playing=\(engine.isPlaying ? 1 : 0)")
        case "openpatch":
            guard a.count == 2, let index = Int(a[1]) else { return fail("line \(line): openpatch RECORD INDEX") }
            let url = a[0].hasPrefix("out:") ? out.appendingPathComponent(String(a[0].dropFirst(4))) : engine.recordsRoot.appendingPathComponent(a[0])
            if let error = m.openPatch(url, index: index) { return fail("openpatch: \(error)") }
            say("openpatch \(a[0]) index=\(index) origin=\(m.origin?.patchIndex ?? -1)")
        case "exportcheck":
            let dir = out.appendingPathComponent("recipe-exports", isDirectory: true)
            do {
                try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                m.press("rescan")
                let entries = m.recipes.entries
                var compared = 0, peg = false
                for entry in entries {
                    m.pick(.recipe(entry.id)); let original = m.patch
                    let url = dir.appendingPathComponent(entry.id + ".txt")
                    if let error = await m.exportRecipe(to: url) { return fail("exportcheck \(entry.id): \(error)") }
                    await m.recipes.importFile(url)
                    guard m.recipes.entries.count == 1, m.recipes.entries[0].error == nil else { return fail("exportcheck: re-import \(entry.id)") }
                    m.pick(.recipe(entry.id))
                    guard m.patch.text == original.text else { return fail("exportcheck: canonical lines \(entry.id)") }
                    peg = peg || original.rows.contains { $0.statement == "peg" }; compared += 1
                    m.press("rescan")
                }
                guard !entries.isEmpty, compared == entries.count, peg else { return fail("exportcheck: complete bundled-recipe/PEG coverage") }
                m.pick(.builtin)
                var op0 = m.patch.op(0); op0.index_q10 = 17
                var ignored = m.patch; ignored.setOp(0, op0); ignored.validate()
                let url = dir.appendingPathComponent("pending.txt")
                let n = try await InstrPatchIO.exportRecipe(ignored, name: ignored.name, to: url)
                await m.recipes.importFile(url)
                guard m.recipes.entries.count == 1, m.recipes.entries[0].error == nil else { return fail("exportcheck: pending re-import") }
                let imported = m.recipes.entries[0]
                guard case .success(let (replay, _, _)) = InstrPatchIO.loadPatch0(paths: [imported.url.path]) else { return fail("exportcheck: pending preview") }
                let text = try String(contentsOf: url, encoding: .utf8)
                let removed = text.split(separator: "\n").filter { !$0.hasPrefix("# pending: ") }.joined(separator: "\n")
                var rejected = false
                do { _ = try InstrPatchIO.checkRecipeExport(original: ignored, replay: replay, recipe: removed) } catch { rejected = true }
                guard n == 1, rejected else { return fail("exportcheck: missing pending control") }
                for fixture in ["wtframe", "kspluck"] {
                    let path = engine.recordsRoot.appendingPathComponent("tests/golden/48000/" + fixture + ".tl")
                    if let error = m.openPatch(path, index: 0) { return fail("exportcheck: fixture \(error)") }
                    if fixture == "wtframe", let i = m.patch.index("mod", 0), let op = m.patch.index("patch", 0) {
                        let omitted = m.patch.rows[i].tokens.dropLast().joined(separator: " ")
                        m.patch.editRow(i, text: omitted)
                        m.patch.editRow(op, text: m.patch.rows[op].text + " 0 0")
                        guard m.patch.diagnostic == nil else { return fail("exportcheck: accepted optional-field fixture") }
                    }
                    let original = m.patch
                    let file = dir.appendingPathComponent(fixture + ".txt")
                    if let error = await m.exportRecipe(to: file) { return fail("exportcheck: valid lossy subset \(fixture): \(error)") }
                    let text = try String(contentsOf: file, encoding: .utf8)
                    let expected = original.rows.filter { ["wtbank", "wtdata", "wthash", "wtuse", "ksource"].contains($0.statement) || ($0.statement == "mod" && $0.tokens.contains("wt_pos0")) }
                    guard !expected.isEmpty, expected.allSatisfy({ text.split(separator: "\n").contains(Substring("# pending: " + $0.text)) }) else { return fail("exportcheck: \(fixture) pending source rows not exact") }
                }
                // This supported generator source depends on headers which the
                // recipe grammar cannot carry. Its partial preview must fail.
                let gen = engine.recordsRoot.appendingPathComponent("tests/golden/gen/genpink-48000.tl")
                var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
                guard let player = InstrPatchIO.withCStrings([gen.path], { fms_player_open($0, 1, &err, err.count) }) else { return fail("exportcheck: generator fixture open") }
                let genPatch = PatchLines.read(player, 0, fs: 48000); fms_player_close(player)
                guard let genPatch else { return fail("exportcheck: generator patch extraction") }
                let held = dir.appendingPathComponent("generator.txt"), sentinel = Data("preserve existing destination\n".utf8)
                try sentinel.write(to: held)
                var dependencyRefused = false
                do { _ = try await InstrPatchIO.exportRecipe(genPatch, name: genPatch.name, to: held) }
                catch { dependencyRefused = String(describing: error).contains("mod generator binding needs gen_clock") }
                guard dependencyRefused, try Data(contentsOf: held) == sentinel else { return fail("exportcheck: generator dependency must refuse without publication") }
                m.press("rescan")
                say("EXPORT ok recipes=\(compared) canonical_equal=yes peg=yes pending=exact dropped_pending=refused wt_ks_pending=exact original_optional_spelling=preserved generator_dependency=needs_gen_clock destination_bytes=unchanged")
            } catch { fail("exportcheck: \(error)") }
        case "importcheck":
            let source = engine.recordsRoot.appendingPathComponent("data/recipes.txt")
            do {
                let before = try Data(contentsOf: source)
                m.press("rescan")
                let expectedNames = Set(m.recipes.entries.map(\.id))
                await m.recipes.importFile(source)
                let imported = m.recipes.entries
                guard !expectedNames.isEmpty, imported.count == expectedNames.count,
                      Set(imported.map(\.id)) == expectedNames,
                      imported.allSatisfy({ $0.error == nil }) else { return fail("importcheck: expected every bundled recipe: \(m.recipes.loadError ?? "")") }
                for entry in imported {
                    let expected = engine.recipesDir.appendingPathComponent(entry.id + ".tl")
                    guard try Data(contentsOf: entry.url) == Data(contentsOf: expected) else { return fail("importcheck: build bytes differ for \(entry.id)") }
                }
                m.pick(.recipe("epiano"))
                guard m.patchName == "epiano", m.patch.diagnostic == nil else { return fail("importcheck: imported patch load") }
                let custom = out.appendingPathComponent("import-recipes.txt")
                let longName = String(repeating: "a", count: 300)
                try "recipe \(longName)\ncarrier\nmodulator\nnote failure fixture\nrecipe import-good\ncarrier\nmodulator\nnote accepted fixture\n".write(to: custom, atomically: true, encoding: .utf8)
                await m.recipes.importFile(custom)
                guard m.recipes.entries.count == 2, let failed = m.recipes.entries.first(where: { $0.error != nil }),
                      m.recipes.entries.contains(where: { $0.id == "import-good" && $0.error == nil }) else { return fail("importcheck: failed recipe missing beside good entry") }
                m.pick(.recipe(failed.id))
                guard engine.errorText == failed.error else { return fail("importcheck: failed entry diagnostic not visible") }
                engine.errorText = nil
                try "recipe malformed\ncarrier wave=99\nmodulator\nnote invalid wave\n".write(to: custom, atomically: true, encoding: .utf8)
                await m.recipes.importFile(custom)
                guard m.recipes.entries.count == 1, m.recipes.entries[0].error != nil,
                      try Data(contentsOf: source) == before else { return fail("importcheck: malformed source error or source changed") }
                m.press("rescan")
                say("IMPORT ok recipes=\(imported.count) byte_identical=yes failures=listed source_bytes=unchanged")
            } catch { fail("importcheck: \(error)") }
        case "recentcheck":
            let dir = out.appendingPathComponent("recent-fixtures")
            do {
                try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                let bytes = try Data(contentsOf: out.appendingPathComponent("saved-recipe.tl"))
                for i in 0..<13 {
                    let url = dir.appendingPathComponent("patch-\(i).tl"); try bytes.write(to: url)
                    if let error = m.pick("open:" + url.path) { return fail("recentcheck: \(error)") }
                }
                guard m.recentPaths.count == 10, URL(fileURLWithPath: m.recentPaths[0]).lastPathComponent == "patch-12.tl",
                      URL(fileURLWithPath: m.recentPaths[9]).lastPathComponent == "patch-3.tl" else { return fail("recentcheck: newest-ten ordering") }
                let old = m.recentPaths
                if m.pick("recent:-1") == nil || m.pick("recent:10") == nil || m.recentPaths != old { return fail("recentcheck: invalid index mutated list") }
                if let error = m.pick("recent:4") { return fail("recentcheck: \(error)") }
                guard m.recentPaths.count == 10, m.recentPaths[0] == old[4], Set(m.recentPaths).count == 10 else { return fail("recentcheck: deduplication") }
                say("RECENT ok newest=10 deduplicated=yes invalid_index=refused user_defaults=untouched")
            } catch { fail("recentcheck: \(error)") }
        case "writeback":
            guard a.count == 1, let origin = m.origin else { return fail("line \(line): writeback same|changed") }
            let before = try? origin.paths.map { try Data(contentsOf: URL(fileURLWithPath: $0)) }
            if let error = await m.writeBack() { return fail("writeback: \(error)") }
            let after = try? origin.paths.map { try Data(contentsOf: URL(fileURLWithPath: $0)) }
            let same = before == after
            guard before != nil, (a[0] == "same" ? same : !same) else { return fail("writeback identity expectation") }
            say("writeback \(a[0]) ok")
        case "wavsave":
            let dir = out.appendingPathComponent("wav-controls")
            do {
                try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                let sentinel = Data("previous companion".utf8)
                let collision = dir.appendingPathComponent("collision.wav")
                try sentinel.write(to: collision)
                let collisionError = await InstrPatchIO.saveTake(m.takeText, to: collision)
                guard collisionError?.contains("collide") == true, try Data(contentsOf: collision) == sentinel else {
                    return fail("WAVSAVE collision was published")
                }
                let target = dir.appendingPathComponent("partial.tl")
                let wav = dir.appendingPathComponent("partial.wav")
                let blocked = dir.appendingPathComponent("partial.wav.tmp")
                try sentinel.write(to: wav); try sentinel.write(to: blocked)
                let partial = await InstrPatchIO.saveTake(m.takeText, to: target)
                guard partial?.hasPrefix("Timeline saved; WAV failed: ") == true,
                      partial?.contains(wav.path + ": cannot write: ") == true,
                      try String(contentsOf: target, encoding: .utf8) == m.takeText,
                      try Data(contentsOf: wav) == sentinel, try Data(contentsOf: blocked) == sentinel else {
                    return fail("WAVSAVE partial failure lost its diagnostic or companion: \(partial ?? "no error")")
                }
                try FileManager.default.removeItem(at: blocked)
                guard await InstrPatchIO.saveTake(m.takeText, to: target) == nil,
                      try Data(contentsOf: wav) != sentinel else { return fail("WAVSAVE retry failed") }
                let wrong = m.takeText.split(separator: "\n", omittingEmptySubsequences: false).map {
                    $0.hasPrefix("hash ") ? "hash 0000000000000000" : String($0)
                }.joined(separator: "\n")
                try sentinel.write(to: wav)
                let mismatch = await InstrPatchIO.saveTake(wrong, to: target)
                guard mismatch?.contains("hash does not match") == true, try Data(contentsOf: wav) == sentinel else {
                    return fail("WAVSAVE wrong hash replaced its companion")
                }
                say("WAVSAVE ok collision=refused partial_error=native previous_companion=preserved retry=yes hash_refusal=yes")
            } catch { fail("WAVSAVE: \(error.localizedDescription)") }
        case "saveguard":
            guard let origin = m.origin, let path = origin.paths.first, origin.patchFile else { return fail("saveguard: no patch-file origin") }
            let url = URL(fileURLWithPath: path)
            guard let before = try? Data(contentsOf: url) else { return fail("saveguard: read") }
            let external = before + Data("# external edit\n".utf8)
            do { try external.write(to: url, options: .atomic) } catch { return fail("saveguard: write fixture") }
            let refused = await m.savePatch(to: url, replacingOrigin: true)
            let unchanged = (try? Data(contentsOf: url)) == external
            try? before.write(to: url, options: .atomic)
            if refused == nil || !unchanged { return fail("saveguard: external edit was overwritten") }
            say("saveguard external_edit=preserved refusal=yes")
        case "savepatch":
            guard a.count == 1 else { return fail("line \(line): savepatch FILE|origin") }
            let original = a[0] == "origin"
            guard !original || m.origin?.paths.first != nil else { return fail("savepatch: no origin") }
            let url = original ? URL(fileURLWithPath: m.origin!.paths[0]) : out.appendingPathComponent(a[0])
            if let error = await m.savePatch(to: url, replacingOrigin: original) { return fail("savepatch: \(error)") }
            let result = await InstrPatchIO.verifyText((try? String(contentsOf: url, encoding: .utf8)) ?? "")
            if !result.hasPrefix("match:") { return fail("savepatch replay: \(result)") }
            say("savepatch \(a[0]) \(result)")
        case "play":
            engine.play()
            say("play playing=\(engine.isPlaying ? 1 : 0)")
        case "set":
            guard a.count >= 2 || a.first?.hasPrefix("button.") == true else { return fail("line \(line): set NAME VALUE") }
            var v = a.dropFirst().joined(separator: " ")
            if a[0] == "button.save" { v = out.appendingPathComponent(v).path }
            if a[0] == "source" && v.hasPrefix("open:") {
                let path = String(v.dropFirst(5))
                let url = path.hasPrefix("out:") ? out.appendingPathComponent(String(path.dropFirst(4)))
                    : path.hasPrefix("/") ? URL(fileURLWithPath: path) : engine.recordsRoot.appendingPathComponent(path)
                v = "open:" + url.path
            }
            if let e = m.set(a[0], v) { return fail("set \(a.joined(separator: " ")): \(e)") }
            if a[0] == "button.save" {
                for _ in 0..<6000 where m.takeSave == "Saving..." { try? await Task.sleep(for: .milliseconds(5)) }
                if !m.takeSave.hasPrefix("Saved ") { return fail("save take: \(m.takeSave)") }
                let wavURL = URL(fileURLWithPath: v).deletingPathExtension().appendingPathExtension("wav")
                guard let bytes = try? Data(contentsOf: wavURL), bytes.count >= 44,
                      bytes.prefix(4) == Data("RIFF".utf8) else { return fail("save take: missing native WAV") }
            }
            if a[0] == "button.verify_take" {
                for _ in 0..<6000 where m.takeVerify.hasPrefix("rendering") { try? await Task.sleep(for: .milliseconds(5)) }
                say("set button.verify_take: \(m.takeVerify)")
                if !m.takeVerify.hasPrefix("match") { fail("verify take: \(m.takeVerify)") }
                return
            }
            say("set \(a.joined(separator: " ")) -> \(m.get(a[0]))")
        case "note":
            guard a.count >= 2, let key = Int(a[1]) else { return fail("line \(line): note ON|OFF KEY [VEL]") }
            if a[0] == "ON" {
                m.alloc.noteOn(key, velocityQ15: a.count > 2 ? UInt32(a[2]) ?? 0 : UInt32(m.velocity))
            } else {
                m.alloc.noteOff(key)
            }
            say("note \(a[0]) \(key) live=\(engine.isLive ? 1 : 0)")
        case "wait":
            let left = Int(engine.info?.frames ?? 0) - Int(engine.positionFrames)
            guard a.count == 1, let n = a[0] == "end" ? max(0, left) : Int(a[0]), n >= 0 else {
                return fail("line \(line): wait FRAMES|end")
            }
            let p0 = Int(engine.positionFrames)
            let b = engine.renderOffline(n)
            segment.add(b.samples)
            lastAdvance = Int(engine.positionFrames) - p0
            say("wait \(n) pos=\(engine.positionFrames) advance=\(lastAdvance)")
            if let d = b.display { fail("wait \(n): display: \(d)") }
        case "hash":
            let h = engine.runningHash
            say(String(format: "hash host=%016llx segment=%016llx", h, segment.h))
            if a.first == "record" {
                let i = engine.info ?? fms_info_t()
                if i.have_hash == 0 || i.hash_expected != h {
                    fail(String(format: "hash: host %016llx, the record's %016llx", h, i.hash_expected))
                }
            }
            segment = FNV64()
        case "modview":
            guard !a.isEmpty, let patch = engine.patches.first else { return fail("modview needs an open record with a patch") }
            let view = ModMatrixView(patch: patch).padding(12).frame(width: 760, height: 360)
                .background(Color.black).environment(\.colorScheme, .dark)
            picture(a[0], render(view), against: a.count >= 3 ? (a[1], a[2]) : nil)
        case "snap":
            guard !a.isEmpty else { return fail("line \(line): snap NAME") }
            let png = await snapshot()
            try? png.write(to: out.appendingPathComponent(a[0] + ".png"))
            snaps[a[0]] = png
            var note = "\(png.count) bytes"
            if a.count >= 3 {
                guard let other = snaps[a[2]] else { return fail("snap \(a[0]): no snap \(a[2])") }
                let changed = other != png
                note += changed ? ", changed from \(a[2])" : ", same as \(a[2])"
                if (a[1] == "changed") != changed { fail("snap \(a[0]): expected \(a[1]) against \(a[2])") }
            }
            say("snap \(a[0]): \(note)")
        case "expect":
            guard a.count >= 2 else { return fail("line \(line): expect WHAT VALUE") }
            let indexed = ["space.path", "space.pos"].contains(a[0]) && a.count >= 3
            let got = expectValue(indexed ? a[0] + " " + a[1] : a[0])
            let want = a[(indexed ? 2 : 1)...].joined(separator: " ")
            if got != want { fail("expect \(a[0]) \(want): got \(got)") } else { say("expect \(a[0]) \(want) ok") }
        case "rawcheck":
            rawEditChecks()
        case "fxcheck":
            fxHeaderChecks()
        case "compressorcheck":
            await compressorChecks()
        case "flangercheck":
            await flangerProfileChecks()
        case "crusherunarmedcheck":
            let old = (m.liveCrushBits, m.liveCrushRateDiv)
            if engine.isLive || m.set("live.crush.bits", "8") == nil || m.set("live.crush.rate_div", "4") == nil {
                fail("CRUSHER_UNARMED live values require an armed player")
            }
            m.press("record")
            if engine.isRecording || old.0 != m.liveCrushBits || old.1 != m.liveCrushRateDiv {
                fail("CRUSHER_UNARMED refused actions changed requested values")
            } else { say("CRUSHER_UNARMED ok") }
            engine.errorText = nil
        case "crushercheck":
            crusherChecks()
        case "librarycheck":
            await libraryChecks()
        case "spacecheck":
            spaceChecks()
        case "spaceimportcheck":
            await spaceImportChecks()
        case "spaceprefcheck":
            spacePreferenceChecks()
        case "midimapcheck":
            midiMapChecks()
        case "mpecheck":
            midiMPEChecks()
        case "check":
            await check(a, line: line)
        case "journalcheck":
            patchJournalChecks()
        case "comparecheck":
            await compareChecks()
        case "learncheck":
            await learnChecks()
        case "learnvirtual":
            await learnVirtualCheck()
        case "favoritecheck":
            await favoriteChecks()
        case "devicecheck":
            await deviceRateChecks()
        case "device":
            guard a.count == 1, let fs = UInt32(a[0]) else { return fail("line \(line): device RATE") }
            engine.simulateDeviceRate(fs)
            say("device \(fs)")
        case "flood":
            guard a.count == 1, let n = Int(a[0]), engine.isLive else { return fail("line \(line): flood N, with a live player") }
            var sent = 0
            while sent < n {
                let k = min(512, n - sent)
                for i in 0..<k {
                    if (sent + i) % 2 == 0 { engine.noteOn(voice: 0, freqMilliHz: 261_626, velocityQ15: 16384) } else { engine.noteOff(voice: 0) }
                }
                sent += k
                _ = engine.renderOffline(64)
            }
            say("flood \(n) pos=\(engine.positionFrames) recording=\(engine.isRecording ? 1 : 0)")
        case "midi":
            let b = a.compactMap { UInt8($0, radix: 16) }
            guard !b.isEmpty, b.count == a.count else { return fail("line \(line): midi HEX...") }
            let mi = MidiInput.shared
            mi.feedBytes(b)
            say("midi \(a.joined(separator: " ")) messages=\(mi.messages) events=\(mi.events) notlive=\(mi.notLive) live=\(engine.isLive ? 1 : 0)")
        case "seekcheck":
            await seekChecks()
        case "recoverycheck":
            await recoveryChecks()
        case "localfilescheck":
            await localFilesChecks(sourcePath: a.isEmpty ? nil : a.joined(separator: " "))
        case "rewind":
            engine.rewind()
            say("rewind pending=\(engine.pendingPlayers)")
        case "space":
            guard a.count == 3 || a.count == 5, let az = Double(a[1]), let el = Double(a[2]), let inf = engine.info else {
                return fail("line \(line): space NAME AZ EL [same|changed OTHER], with a record open")
            }
            let roll = RollData(events: engine.events, frames: inf.frames, nvoices: Int(inf.nvoices))
            let view = SpaceView3D(info: inf, pos: engine.voicePos, active: engine.voiceActive, voiceStream: roll.voiceStream,
                                   yawMdeg: roll.yaw(at: engine.positionFrames, initial: inf.yaw_mdeg),
                                   paths: engine.spacePaths, camera: SpaceCamera(az: az, el: el))
                .frame(width: 560, height: 300).background(Color.black).environment(\.colorScheme, .dark)
            picture(a[0], settled(view), against: a.count == 5 ? (a[3], a[4]) : nil)
        case "stereosilence":
            // Exercise actual player analysis and nil correlation, using the
            // loaded record's header without its sounding events or hash.
            guard engine.loadedPaths.count == 1 else { return fail("stereosilence needs one record") }
            do {
                let text = try String(contentsOfFile: engine.loadedPaths[0], encoding: .utf8)
                let header = text.components(separatedBy: "\n").filter {
                    let row = $0.trimmingCharacters(in: .whitespaces)
                    return !row.hasPrefix("hash ") && !(row.first?.isNumber ?? false)
                }.joined(separator: "\n")
                let dir = out.appendingPathComponent("stereo-controls")
                try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                let path = dir.appendingPathComponent("silence.tl")
                try header.write(to: path, atomically: true, encoding: .utf8)
                engine.open(paths: [path.path], title: "silence control")
                let want = engine.loadID
                for _ in 0..<6000 where engine.installedSerial < want { try? await Task.sleep(for: .milliseconds(5)) }
                guard engine.installedSerial >= want, engine.errorText == nil else { return fail("stereosilence refused") }
                await step("stereo", ["silence", "4"], line: line)
                guard let st = engine.stereo, st.rows.count == 1, st.rows[0].corr == nil,
                      st.rows[0].length == st.frames else { return fail("stereosilence did not cover every frame with nil correlation") }
                say("STEREO_NULL ok rows=1 correlation=nil full_length=yes")
            } catch { fail("stereosilence: \(error.localizedDescription)") }
        case "stereo":
            guard !a.isEmpty, a.count <= 2, let inf = engine.info else { return fail("line \(line): stereo NAME [SECONDS], with a record open") }
            let w = a.count == 2 ? UInt32((Double(a[1]) ?? 0) * Double(inf.fs)) : 0
            let (st, frames): (StereoStrip?, [Int16]) = await withCheckedContinuation { k in
                engine.computeStereo(windowFrames: w, keepSamples: true) { st, x in k.resume(returning: (st, x)) }
            }
            guard let st else { return fail("stereo \(a[0]): not a two-channel record") }
            if let e = st.error { return fail("stereo \(a[0]): \(e)") }
            try? wav(frames, fs: st.fs).write(to: out.appendingPathComponent(a[0] + ".wav"))
            let tsv = st.rows.map { r in
                String(format: "%.12g\t%.12g\t%.12g\t%@", Double(r.start) / Double(st.fs), Double(r.length) / Double(st.fs), r.sideMid, r.corr.map { String(format: "%.12g", $0) } ?? "nan")
            }
            try? (tsv.joined(separator: "\n") + "\n").write(to: out.appendingPathComponent(a[0] + ".tsv"), atomically: true, encoding: .utf8)
            let match = inf.have_hash == 0 ? "no hash" : (st.hash == inf.hash_expected ? "the record's" : "NOT the record's")
            say(String(format: "stereo %@: %u frames, window %u, %d rows, hash %016llx (%@)", a[0], st.frames, st.window, st.rows.count, st.hash, match))
            for (r, t) in zip(st.rows, tsv) { _ = r; say("  " + t) }
            if inf.have_hash != 0 && st.hash != inf.hash_expected { fail("stereo \(a[0]): the frames' hash is not the record's") }
            let view = HStack(alignment: .top, spacing: 12) {
                Goniometer(samples: engine.scope, channels: engine.scopeChannels).frame(width: 200, height: 216)
                StereoStripView(strip: st, position: engine.positionFrames).frame(width: 440, height: 216)
            }
            .padding(8).background(Color.black).environment(\.colorScheme, .dark)
            picture(a[0], render(view), against: nil)
        default:
            fail("line \(line): unknown step \(op)")
        }
    }

    private func libraryChecks() async {
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ reason: String) { checks += 1; if !ok { fail("LIBRARY \(reason)") } }
        func loaded() async -> Bool {
            for _ in 0..<6000 where LocalRecords.shared.busy { try? await Task.sleep(for: .milliseconds(5)) }
            let wanted = engine.loadID
            for _ in 0..<6000 where engine.installedSerial < wanted { try? await Task.sleep(for: .milliseconds(5)) }
            _ = engine.renderOffline(0)
            return engine.installedSerial == wanted && engine.info != nil && engine.errorText == nil
        }
        func link(_ host: String, _ path: String) -> URL {
            var c = URLComponents(); c.scheme = RecordOpen.scheme; c.host = host
            c.queryItems = [URLQueryItem(name: "path", value: path)]
            return c.url!
        }
        let browser = RecordBrowserModel()
        browser.reload()
        require(browser.category == .songs && browser.diagnostic == nil && !browser.songs.isEmpty, "published catalog available with Songs default")
        let publishedCount = browser.songs.count
        var callbackFrames = 0
        let sampleTitles = browser.songs.filter { $0.variant(.keys) != nil }.prefix(2).map(\.title)
        require(!sampleTitles.isEmpty, "catalog contains a Keys example")
        for title in sampleTitles {
            browser.search = title
            require(browser.filteredSongs.contains { $0.title == title }, "published title discoverable: \(title)")
            if let song = browser.filteredSongs.first(where: { $0.title == title }) {
                require(browser.chooseSong(song.id), "published song selected: \(title)")
                let keysLoaded = await loaded()
                require(keysLoaded && browser.selection.kind == .keys && !engine.isPlaying, "published Keys default loads: \(title)")
                engine.play(); _ = engine.renderOffline(64); callbackFrames += 64
                require(engine.isPlaying && engine.positionFrames == 64, "published Keys callback advances: \(title)")
                engine.stop()
                for kind in [SongVariantKind.standard, .mirror] where song.variant(kind) != nil {
                    require(browser.chooseVariant(kind), "published variant selected: \(title) \(kind.rawValue)")
                    let variantLoaded = await loaded()
                    require(variantLoaded && !engine.isPlaying && engine.loadedPaths == song.record(kind, root: engine.recordsRoot)?.paths,
                            "published variant loads: \(title) \(kind.rawValue)")
                    engine.play(); _ = engine.renderOffline(64); callbackFrames += 64
                    require(engine.isPlaying && engine.positionFrames == 64, "published variant callback advances: \(title) \(kind.rawValue)")
                    engine.stop()
                }
            }
        }
        // Bounded native fixtures exercise the same model/actions as the view;
        // the published catalog above remains independently file-validated.
        let paths = ["tests/golden/48000/sine.tl", "tests/golden/space/panlaw.tl", "tests/golden/space/pan.tl"]
        let directory = out.appendingPathComponent("library", isDirectory: true)
        let fixture = directory.appendingPathComponent("library-fixture.json")
        func variant(_ kind: String, _ id: String, _ index: Int) -> [String: Any] {
            ["kind": kind, "id": id, "paths": [paths[index]]]
        }
        let songs: [[String: Any]] = [
            ["id": "test/a", "title": "Library Fixture A", "collection": "First Collection", "revision": "Arrangement 2",
             "variants": [variant("standard", "test/a-standard", 0), variant("keys", "test/a-keys", 1), variant("mirror", "test/a-mirror", 2)]],
            ["id": "test/b", "title": "Library Fixture B", "collection": "Second Collection",
             "variants": [variant("standard", "test/b-standard", 0), variant("keys", "test/b-keys", 1)]]
        ]
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try JSONSerialization.data(withJSONObject: ["version": 1, "songs": songs], options: [.sortedKeys]).write(to: fixture)
            browser.reload(libraryURL: fixture)
            require(browser.diagnostic == nil && browser.songs.count == 2, "fixture catalog loaded")
            for query in ["fixture a", "FIRST collection", "test/a"] {
                browser.search = query
                require(browser.filteredSongs.map(\.id) == ["test/a"], "search \(query)")
            }
            browser.search = "no matching title"
            require(browser.filteredSongs.isEmpty && browser.songs.count == 2, "search empty shown count preserves total")
            browser.search = ""
            require(browser.filteredSongs.count == 2, "search reset count")
            require(browser.chooseSong("test/a"), "select first song")
            let firstLoaded = await loaded()
            require(firstLoaded && browser.selection.kind == .keys && engine.loadedPaths == [engine.recordsRoot.appendingPathComponent(paths[1]).path], "Keys default loads native record")
            require(engine.title == "Library Fixture A — Keys stereo" && !engine.isPlaying, "readable title and paused selection")
            require(browser.chooseVariant(.standard), "variant Standard accepted")
            let standardLoaded = await loaded()
            require(standardLoaded && engine.loadedPaths == [engine.recordsRoot.appendingPathComponent(paths[0]).path] && !engine.isPlaying, "variant Standard path")
            require(browser.chooseVariant(.mirror), "variant Mirror accepted")
            let mirrorLoaded = await loaded()
            require(mirrorLoaded && engine.loadedPaths == [engine.recordsRoot.appendingPathComponent(paths[2]).path], "variant Mirror path")
            var withoutMirror = songs[0]
            withoutMirror["variants"] = [variant("standard", "test/a-standard", 0), variant("keys", "test/a-keys", 1)]
            try JSONSerialization.data(withJSONObject: ["version": 1, "songs": [withoutMirror, songs[1]]]).write(to: fixture)
            let beforeReload = engine.loadID
            browser.reload(libraryURL: fixture)
            require(browser.selection.songID == nil && engine.loadID == beforeReload, "removed variant clears selection without changing playback")
            require(browser.chooseSong("test/b"), "select second song")
            let secondLoaded = await loaded()
            require(secondLoaded && browser.selection.kind == .keys && browser.selectedSong?.edition == "Published", "Keys reset for next song and default edition")
            let serial = engine.loadID
            require(!browser.chooseVariant(.mirror) && engine.loadID == serial, "absent mirror refuses without load")
            browser.category = .examples; browser.search = "tests/golden/48000/sine"
            require(browser.filteredRecords.contains { $0.id == paths[0] }, "examples remain searchable")
            browser.chooseExample(paths[0])
            let exampleLoaded = await loaded()
            require(exampleLoaded && engine.title == paths[0], "legacy example identity opens")
            browser.category = .recipes; browser.search = ""
            require(!browser.filteredRecords.isEmpty && browser.filteredRecords.allSatisfy { $0.group == "recipes" }, "recipes remain separate")
            browser.reload(libraryURL: directory.appendingPathComponent("absent-library.json"))
            require(browser.songs.isEmpty && browser.diagnostic != nil && !browser.examples.isEmpty, "missing catalog visibly refuses")
            var collision = songs[0]
            collision["variants"] = [variant("standard", paths[0], 0), variant("keys", "test/collision-keys", 1)]
            try JSONSerialization.data(withJSONObject: ["version": 1, "songs": [collision]]).write(to: fixture)
            browser.reload(libraryURL: fixture)
            require(browser.songs.isEmpty && browser.diagnostic?.contains("conflicts") == true, "reserved example identity refuses visibly")

            let source = engine.recordsRoot.appendingPathComponent(paths[0])
            let original = try Data(contentsOf: source)
            let direct = directory.appendingPathComponent("Direct é ' $() & # ?.tl")
            try original.write(to: direct)
            require(RecordOpen.open(direct) == nil, "direct file accepted")
            let directLoaded = await loaded()
            let retainedName = LocalRecordStore.digest(original) + ".tl"
            require(directLoaded && !engine.isPlaying && engine.loadedPaths.first.map { URL(fileURLWithPath: $0).lastPathComponent } == retainedName,
                    "file open retains copy and stays paused")
            require(RecordOpen.open(link("open", direct.path)) == nil, "open URL accepted")
            let openLoaded = await loaded()
            require(openLoaded && !engine.isPlaying, "open URL stays paused")
            require(RecordOpen.open(link("play", direct.path)) == nil, "play URL accepted")
            let playLoaded = await loaded()
            require(playLoaded && engine.isPlaying && engine.loadedPaths == [direct.standardizedFileURL.path], "play URL starts accepted load")
            _ = engine.renderOffline(64)
            callbackFrames += 64
            require(engine.positionFrames == 64, "play URL advances native callback")
            engine.stop()
            // Both requests are issued in one actor turn: the first install
            // cannot win before the later paused request supersedes it.
            require(RecordOpen.open(link("play", direct.path)) == nil, "stale play accepted initially")
            require(RecordOpen.open(source) == nil, "new paused open accepted")
            let supersedingLoaded = await loaded()
            require(supersedingLoaded && !engine.isPlaying && engine.loadedPaths.first.map { URL(fileURLWithPath: $0).lastPathComponent } == retainedName,
                    "superseded play cannot start later open")
            require(RecordOpen.open(link("play", direct.path)) == nil, "play before live arm accepted")
            m.arm()
            // The existing live-arm policy starts its own player. The older
            // file request must neither replace it nor leak into a later Open.
            for _ in 0..<5 { try? await Task.sleep(for: .milliseconds(1)) }
            require(engine.isLive && engine.isPlaying && engine.loadedPaths.isEmpty, "live arm supersedes pending play")
            require(RecordOpen.open(source) == nil, "paused open after live arm accepted")
            let afterLive = await loaded()
            require(afterLive && !engine.isLive && !engine.isPlaying, "live arm does not transfer autoplay to ordinary open")
            let invalid = directory.appendingPathComponent("invalid-play.tl")
            try Data("not a native record\n".utf8).write(to: invalid)
            require(RecordOpen.open(link("play", invalid.path)) == nil, "invalid TL reaches native reader")
            let refused = await loaded()
            require(!refused && engine.info == nil && engine.errorText != nil && !engine.isPlaying, "native refusal cannot autoplay")
            require(RecordOpen.open(source) == nil, "open after failed play accepted")
            let recovered = await loaded()
            require(recovered && !engine.isPlaying, "failed play cannot leak autoplay")
            for malformed in ["fmspike://play?path=relative.tl", "fmspike://play?path=/x.tl&path=/y.tl",
                              "fmspike://other?path=/x.tl", "fmspike://play/extra?path=/x.tl",
                              "fmspike://play?path=/x.tl#fragment", "fmspike://play?path=/x.wav"] {
                let old = engine.loadID
                require(RecordOpen.open(URL(string: malformed)!) != nil && engine.loadID == old, "malformed play URL refuses without load")
            }
            require(try Data(contentsOf: source) == original && Data(contentsOf: direct) == original, "direct playback preserves source bytes")
            engine.errorText = nil
        } catch { fail("LIBRARY fixture error: \(error.localizedDescription)") }
        if failures == before { say("LIBRARY checks=\(checks) published_songs=\(publishedCount) callback_frames=\(callbackFrames) ok") }
    }

    private func recoveryChecks() async {
        let before = failures
        var checks = 0
        func require(_ condition: Bool, _ label: String) { checks += 1; if !condition { fail("RECOVERY \(label)") } }
        let originalPaths = engine.loadedPaths, originalTitle = engine.title
        let originalFrame = engine.positionFrames, playing = engine.isPlaying
        func loaded() async {
            let serial = engine.loadID
            for _ in 0..<6000 where engine.installedSerial < serial { try? await Task.sleep(for: .milliseconds(5)) }
            _ = engine.renderOffline(0)
        }
        engine.open(paths: [engine.recordsRoot.appendingPathComponent("tests/golden/48000/bell.tl").path], title: "same record title")
        await loaded()
        var staleDelivered = false
        engine.verifyChunked { _, _, _ in staleDelivered = true }
        engine.open(paths: [engine.recordsRoot.appendingPathComponent("tests/golden/48000/sine.tl").path], title: "same record title")
        await loaded()
        let verified = await withCheckedContinuation { continuation in
            engine.verifyChunked { _, ok, _ in continuation.resume(returning: ok) }
        }
        require(verified && !staleDelivered, "verification belongs to installed record even with equal titles")
        engine.play(); _ = engine.renderOffline(64); engine.stop()
        let position = engine.positionFrames, hash = engine.runningHash
        engine.audioStartOverride = {
            throw NSError(domain: "OfflineAudio", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "injected device start failure"])
        }
        engine.play()
        _ = engine.renderOffline(64)
        require(!engine.isPlaying && engine.positionFrames == position && engine.runningHash == hash,
                "failed audio start leaves transport and native player paused")
        require(engine.errorText?.contains("injected device start failure") == true, "audio failure is visible")
        engine.audioStartOverride = {}
        engine.play()
        _ = engine.renderOffline(64)
        require(engine.isPlaying && engine.positionFrames == position + 64 && engine.errorText == nil,
                "retry resumes native rendering and clears audio failure")
        engine.stop(); engine.errorText = "Retained non-audio diagnostic"
        engine.play()
        require(engine.errorText == "Retained non-audio diagnostic", "successful retry preserves unrelated diagnostics")
        engine.audioStartOverride = nil; engine.errorText = nil
        engine.open(paths: originalPaths, title: originalTitle)
        await loaded()
        require(engine.seek(originalFrame) == nil, "restore seek accepted")
        for _ in 0..<6000 where engine.isSeeking {
            _ = engine.renderOffline(0); try? await Task.sleep(for: .milliseconds(5))
        }
        if playing { engine.play() } else { engine.stop() }
        if failures == before { say("RECOVERY ok checks=\(checks) verification=load-bound device_failure=paused retry=rendered physical_device=none") }
    }

    private func localFilesChecks(sourcePath: String? = nil) async {
        let before = failures
        var checks = 0
        func require(_ condition: Bool, _ label: String) { checks += 1; if !condition { fail("LOCALFILES \(label)") } }
        let originalPaths = engine.loadedPaths, originalTitle = engine.title
        let originalFrame = engine.positionFrames, playing = engine.isPlaying
        func loaded() async {
            let serial = engine.loadID
            for _ in 0..<6000 where engine.installedSerial < serial { try? await Task.sleep(for: .milliseconds(5)) }
            _ = engine.renderOffline(0)
        }
        let localDir = out.appendingPathComponent("local-files", isDirectory: true)
        try? FileManager.default.createDirectory(at: localDir, withIntermediateDirectories: true)
        let local = LocalRecords(directory: localDir.appendingPathComponent("imported-records", isDirectory: true))
        let source = sourcePath.map { URL(fileURLWithPath: $0) }
            ?? engine.recordsRoot.appendingPathComponent("tests/golden/48000/bell.tl")
        if sourcePath != nil {
            require(!engine.hasSourceLibrary && !RecipeLibrary.toolsAvailable, "standalone has no checkout/tool dependency")
            require((try? FileManager.default.contentsOfDirectory(atPath: engine.recipesDir.path).contains { $0.hasSuffix(".tl") }) == true,
                    "standalone contains prebuilt recipes")
        }
        do {
            let bytes = try Data(contentsOf: source)
            if let task = local.importFile(source) { await task.value } else { fail("LOCALFILES import did not start") }
            await loaded()
            require(engine.info?.hash_expected != nil && !engine.isPlaying && local.status.hasPrefix("Imported locally"), "import opens paused")
            require(try engine.loadedPaths.count == 1 && Data(contentsOf: URL(fileURLWithPath: engine.loadedPaths[0])) == bytes, "original bytes retained")
            require(local.records.count == 1, "one retained tune")
            engine.play()
            let rendered = engine.renderOffline(Int(engine.info?.frames ?? 0))
            require(engine.info?.have_hash == 1 && FNV64.of(rendered.samples) == engine.info?.hash_expected,
                    "retained song renders to saved PCM hash")
            engine.stop()
            if let task = local.importFile(source) { await task.value }; await loaded()
            require(local.records.count == 1, "duplicate import reuses copy")
            let serial = engine.loadID
            let destination = localDir.appendingPathComponent("local-export.tl")
            if let task = local.export(to: destination) { await task.value }
            require(try Data(contentsOf: destination) == bytes, "export preserves exact original")
            require(engine.loadID == serial && !engine.isPlaying, "export leaves transport unchanged")
            require(local.export(to: URL(fileURLWithPath: engine.loadedPaths[0])) == nil, "export cannot overwrite loaded source")
            // Export WAV…: uitest.sh compares these files with harness --play --wav.
            if let task = local.export(to: localDir.appendingPathComponent("local-export.wav"), wav: true) { await task.value }
            require(local.status == "Exported the record's offline WAV."
                    && (try? Data(contentsOf: localDir.appendingPathComponent("local-export.wav")).prefix(4)) == Data("RIFF".utf8),
                    "export WAV writes the native WAV: \(local.status)")
            require(engine.loadID == serial && !engine.isPlaying, "WAV export leaves transport unchanged")
            require(local.export(to: localDir.appendingPathComponent("local-export.aiff"), wav: true) == nil
                    && !FileManager.default.fileExists(atPath: localDir.appendingPathComponent("local-export.aiff").path),
                    "WAV export needs a .wav destination")
            require(local.export(to: URL(fileURLWithPath: engine.loadedPaths[0]), wav: true) == nil, "WAV export cannot overwrite loaded source")
            require(RecordOpen.open(URL(string: "https://example.invalid/song.tl")!) != nil && engine.loadID == serial, "internet URL refused before load")
            let pending = local.importFile(source); local.cancel(); await pending?.value
            require(!local.busy && engine.loadID == serial && local.status == "Cancelled.", "cancel cannot install stale import")
            let invalid = localDir.appendingPathComponent("invalid.tl")
            try Data("not a timeline".utf8).write(to: invalid)
            if let task = local.importFile(invalid) { await task.value }
            require(local.records.count == 1 && engine.loadID == serial && local.status.hasPrefix("Import refused"), "invalid import preserves library and current song")
            let reopened = LocalRecords(directory: localDir.appendingPathComponent("imported-records", isDirectory: true))
            require(reopened.records == local.records, "library survives reopening")
            reopened.openSelected(); await loaded()
            require(!engine.isPlaying && engine.info != nil, "saved copy opens paused")
            reopened.removeSelected()
            require(reopened.records.isEmpty, "remove retained copy")
            require(try Data(contentsOf: source) == bytes, "remove preserves original file")
            if sourcePath == nil {
                // A two-file piece (session.tl + notes.tl), built like tests/decode_cli.py.
                let lines = try String(contentsOf: engine.recordsRoot.appendingPathComponent("tests/golden/48000/sine.tl"), encoding: .utf8)
                    .split(separator: "\n").map(String.init)
                let piece = localDir.appendingPathComponent("piece", isDirectory: true)
                try FileManager.default.createDirectory(at: piece, withIntermediateDirectories: true)
                let header = lines.prefix(8).filter { !$0.hasPrefix("hash ") }.map { $0.replacingOccurrences(of: "streams golden", with: "streams notes") }
                let events = lines.filter { $0.contains(" golden ") }.map { $0.replacingOccurrences(of: " golden ", with: " notes ") }
                let session = piece.appendingPathComponent("session.tl"), notes = piece.appendingPathComponent("notes.tl")
                try (header.joined(separator: "\n") + "\n").write(to: session, atomically: true, encoding: .utf8)
                try (events.joined(separator: "\n") + "\n").write(to: notes, atomically: true, encoding: .utf8)
                engine.open(paths: [session.path, notes.path], title: "piece"); await loaded()
                require(engine.info != nil && engine.loadedPaths.count == 2 && events.count == 2, "two-file piece opens")
                if let task = local.export(to: localDir.appendingPathComponent("piece-export.wav"), wav: true) { await task.value }
                require(local.status == "Exported the record's offline WAV.", "two-file piece exports WAV: \(local.status)")
            }
        } catch { fail("LOCALFILES \(error)") }
        if !originalPaths.isEmpty {
        engine.open(paths: originalPaths, title: originalTitle); await loaded()
        if engine.seek(originalFrame) != nil { fail("LOCALFILES restore seek") }
        for _ in 0..<6000 where engine.isSeeking { _ = engine.renderOffline(0); try? await Task.sleep(for: .milliseconds(5)) }
        if playing { engine.play() } else { engine.stop() }
        }
        engine.errorText = nil; m.snapshotRecord()
        if failures == before { say("LOCALFILES ok checks=\(checks) network=none original=retained export=exact cancel=discarded") }
    }

    private func seekChecks() async {
        let originalPaths = engine.loadedPaths, originalTitle = engine.title
        let originalFrame = engine.positionFrames, originalPlaying = engine.isPlaying
        let fixtures = ["tests/golden/48000/bell.tl", "tests/golden/space/binaural/binaural3.tl", "tests/golden/gen/genlsystem-48000.tl"]
        func open(_ paths: [String], _ title: String) async -> Bool {
            engine.open(paths: paths, title: title)
            let serial = engine.loadID
            for _ in 0..<6000 where engine.installedSerial < serial { try? await Task.sleep(for: .milliseconds(5)) }
            _ = engine.renderOffline(0)
            return engine.installedSerial == serial && engine.info != nil
        }
        func finish() async -> Bool {
            for _ in 0..<6000 where engine.isSeeking {
                _ = engine.renderOffline(0)
                try? await Task.sleep(for: .milliseconds(5))
            }
            _ = engine.renderOffline(0)
            return !engine.isSeeking
        }
        // This oracle uses plain rendering only, never the seek entry point.
        func plain(_ paths: [String], _ frame: UInt32, _ following: UInt32) -> (UInt64, [Int16])? {
            var error = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            let cstrs = paths.map { strdup($0) }
            defer { cstrs.forEach { free($0) } }
            let player = cstrs.map { UnsafePointer($0) }.withUnsafeBufferPointer { buf in
                fms_player_open(buf.baseAddress, Int32(buf.count), &error, error.count)
            }
            guard let p = player else { return nil }
            defer { fms_player_close(p) }
            var info = fms_info_t(); fms_player_info(p, &info)
            var buf = [Int16](repeating: 0, count: 471 * Int(info.channels))
            var done: UInt32 = 0
            while done < frame {
                let n = min(471, frame - done)
                guard fms_player_render(p, &buf, n) == n else { return nil }
                done += n
            }
            let hash = fms_player_hash(p)
            var next = [Int16](repeating: 0, count: Int(following * info.channels))
            if following > 0, fms_player_render(p, &next, following) != following { return nil }
            return (hash, next)
        }
        var checks = 0
        for fixture in fixtures {
            let paths = [engine.recordsRoot.appendingPathComponent(fixture).path]
            guard await open(paths, fixture), let info = engine.info else { return fail("SEEKUI open") }
            let targets: [UInt32] = [0, 64, info.frames / 2, info.frames]
            for (i, target) in targets.enumerated() {
                if i % 2 == 0 { engine.stop() } else { engine.play() }
                let playing = engine.isPlaying
                let following = min(64, info.frames - target)
                guard let oracle = plain(paths, target, playing ? following : 0) else { return fail("SEEKUI oracle") }
                if let error = engine.seek(target) { return fail("SEEKUI refused: \(error)") }
                guard engine.isSeeking, engine.seek(target) != nil else { return fail("SEEKUI missing request/busy refusal") }
                guard await finish(), engine.positionFrames == target, engine.runningHash == oracle.0,
                      engine.isPlaying == playing else { return fail("SEEKUI prefix or play state") }
                let h = engine.runningHash, pos = engine.positionFrames
                guard engine.seek(info.frames + 1) != nil, engine.positionFrames == pos, engine.runningHash == h else {
                    return fail("SEEKUI invalid request changed state")
                }
                if playing {
                    let block = engine.renderOffline(Int(following))
                    guard block.samples == oracle.1, block.display == nil else { return fail("SEEKUI continuation") }
                } else {
                    _ = engine.renderOffline(1)
                    guard engine.positionFrames == target, engine.runningHash == h else { return fail("SEEKUI paused advancement") }
                }
                if fixture == fixtures[0], i == 2 {
                    let view = TransportCard(info: info).environmentObject(engine)
                        .padding(12).frame(width: 850).background(Color.black)
                        .environment(\.colorScheme, .dark)
                    picture("seek-transport", settled(view), against: nil)
                }
                checks += 1
            }
        }
        // Start the worker before changing the load serial. Its queued main
        // completion cannot run until this task yields, making this stale
        // completion control deterministic without a callback test hook.
        guard let info = engine.info, engine.seek(info.frames / 2) == nil else { return fail("SEEKUI stale setup") }
        _ = engine.renderOffline(0)
        let replacement = [engine.recordsRoot.appendingPathComponent(fixtures[0]).path]
        guard await open(replacement, "seek replacement"), await finish(), engine.loadedPaths == replacement else {
            return fail("SEEKUI stale completion replaced new record")
        }
        engine.play()
        guard let oracle = plain(replacement, 0, 64), engine.renderOffline(64).samples == oracle.1 else {
            return fail("SEEKUI replacement continuation")
        }
        _ = engine.renderOffline(0)
        guard engine.pendingPlayers == 0 else { return fail("SEEKUI retired player leaked") }
        // A refused live-arm candidate must preserve the sought record,
        // although it increments the general load serial while validating.
        guard let patch = engine.patches.first, engine.seek(65) == nil else { return fail("SEEKUI refused-arm setup") }
        _ = engine.renderOffline(0)
        var accepted: Bool?
        engine.openLive(patch: patch, name: "seek_refusal", voices: 1, stereo: false,
                        extra: "not_a_header 1") { accepted = $0 }
        guard accepted == false, await finish(), !engine.isLive, engine.loadedPaths == replacement,
              engine.positionFrames == 65, let prefix = plain(replacement, 65, 0),
              engine.runningHash == prefix.0, engine.isPlaying else { return fail("SEEKUI refused arm lost record") }
        engine.errorText = nil
        // Cancel while waiting for retirement, before tick can start work.
        // Offline live install is synchronous, so this path is deterministic.
        guard engine.seek(64) == nil, engine.isSeeking else { return fail("SEEKUI waiting setup") }
        accepted = nil
        engine.openLive(patch: patch, name: "seek_replacement", voices: 1, stereo: false,
                        extra: nil) { accepted = $0 }
        _ = engine.renderOffline(0)
        guard accepted == true, engine.isLive, !engine.isSeeking, engine.pendingPlayers == 0,
              engine.renderOffline(64).samples.allSatisfy({ $0 == 0 }) else { return fail("SEEKUI waiting cancellation") }
        guard await open(originalPaths, originalTitle), engine.seek(originalFrame) == nil, await finish() else {
            return fail("SEEKUI restore")
        }
        if originalPlaying { engine.play() } else { engine.stop() }
        m.snapshotRecord()
        say("SEEKUI ok positions=\(checks) paused=yes playing=yes invalid=unchanged busy=refused stale=discarded waiting=cancelled refused_arm=preserved retired=yes")
    }

    private func expectValue(_ what: String) -> String {
        switch what {
        case "recent.count": return String(m.recentPaths.count)
        case "title": return engine.title
        case "origin.patchfile": return m.origin?.patchFile == true ? "1" : "0"
        case "origin.index": return String(m.origin?.patchIndex ?? -1)
        case "origin.file": return m.origin?.paths.first.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "none"
        case "live": return engine.isLive ? "1" : "0"
        case "playing": return engine.isPlaying ? "1" : "0"
        case "recording": return engine.isRecording ? "1" : "0"
        case "dirty": return m.liveDirty ? "1" : "0"
        case "take": return m.takeText.isEmpty ? "0" : "1"
        case "recipes": return m.recipes.entries.isEmpty ? "0" : "1"
        case "advance": return String(lastAdvance)
        case "position": return String(engine.positionFrames)
        case "error": return engine.errorText == nil ? "0" : "1"
        case "clipboard":
            let board = InstrModel.board.string(forType: .string) ?? ""
            return board == InstrPatchIO.patchText(m.patch, name: m.safeName) ? "1" : "0"
        case "dropped": return m.dropNote == nil ? "0" : "1"
        case "statements":
            var seen: [String] = []
            for l in InstrPatchIO.patchText(m.patch, name: m.safeName).split(separator: "\n") {
                let w = String(l.split(separator: " ").first ?? "")
                if !w.isEmpty && !seen.contains(w) { seen.append(w) }
            }
            return seen.joined(separator: " ")
        case "label.device": return deviceRateLabel(recordFs: engine.info?.fs ?? 0, deviceFs: engine.deviceRate) == nil ? "0" : "1"
        case "label.audition": return auditionLabel(engine.info) == nil ? "0" : "1"
        case "label.rearm": return m.liveLabels.contains(rearmLabel(dirty: true) ?? "") ? "1" : "0"
        case "space.points": return String(engine.spacePaths.count)
        case "take.full": return engine.takeEndNote == nil ? "0" : "1"
        case "players.pending": return String(engine.pendingPlayers)
        case "stereo.rows": return engine.stereo.map { String($0.rows.count) } ?? "none"
        case _ where what.hasPrefix("midi."):
            let mi = MidiInput.shared, s = mi.stats
            switch what.dropFirst(5) {
            case "messages": return String(mi.messages)
            case "notes": return String(s.notes)
            case "events": return String(mi.events)
            case "notlive": return String(mi.notLive)
            case "dropped": return String(mi.dropped)
            case "refused": return String(s.refused)
            case "ignored": return String(s.programs + s.aftertouch + s.pressure + s.other_ccs + s.other_channels + s.system + s.unknown)
            case "steals": return String(s.steals + s.release_steals)
            case "pitch": return String(s.pitch_events)
            case "filter": return String(s.filter_events)
            default: return "none"
            }
        case _ where what.hasPrefix("space.path "):
            return pathText(engine.spacePaths, voice: UInt32(what.dropFirst(11)) ?? 0)
        case _ where what.hasPrefix("space.pos "):
            let v = Int(what.dropFirst(10)) ?? 0
            guard v < engine.voicePos.count else { return "none" }
            let p = engine.voicePos[v]
            return "\(p.x),\(p.y),\(p.z)"
        default: return m.get(what)
        }
    }

    /// The labels the views show for this state (TransportCard, the live box).
    private func labels() -> [String] {
        [auditionLabel(engine.info), deviceRateLabel(recordFs: engine.info?.fs ?? 0, deviceFs: engine.deviceRate),
         m.liveLabels.first { $0 == rearmLabel(dirty: true) }].compactMap { $0 }
    }

    // ---- checks ----

    private func midiMapChecks() {
        let saved = (m.patch, m.fx, m.space, m.midiMaps, m.midiMPE), failuresBefore = failures
        defer {
            (m.patch, m.fx, m.space, m.midiMaps, m.midiMPE) = saved
            m.arm()
        }
        let base = InstrPatchIO.defaultPatch()
        let prefix = base.text + "mod \(base.name) 0 "
        var masks = 0
        for input in 0..<8 {
            let lines = prefix + "ctl\(input) pitch0 1200 0\n"
            if fms_midi_ctl_reads(lines, 48000) != 1 << input { fail("midimapcheck ctl\(input) read mask") }
            masks += 1
        }
        for line in ["ctl0 pitch0 0 0", "eg0 pitch0 1200 0", "ctl9 pitch0 1200 0"] {
            if fms_midi_ctl_reads(prefix + line + "\n", 48000) != 0 { fail("midimapcheck inactive/invalid source \(line)") }
            masks += 1
        }
        m.patch = PatchLines(prefix + "ctl0 pitch0 1200 0\n")
        m.fx = InstrFx(); m.space = InstrSpace(); m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        m.midiMaps[0] = "default"
        m.arm()
        if let error = engine.errorText { return fail("midimapcheck valid arm: \(error)") }
        m.alloc.noteOn(60, velocityQ15: 26000)
        MidiInput.shared.feedBytes([0x90, 64, 100])
        _ = engine.renderOffline(64)
        let held = m.alloc.held, frame = engine.positionFrames
        let notes = MidiInput.shared.stats.notes, maps = m.armedMidiMaps
        m.midiMaps[0] = "cc999:0"
        m.arm()
        if engine.errorText == nil || m.midiMaps[0] != "cc999:0" || m.armedMidiMaps != maps ||
            m.alloc.held != held || engine.positionFrames != frame || MidiInput.shared.stats.notes != notes {
            fail("midimapcheck refused map changed active state")
        }
        m.midiMaps = maps
        m.fx.pingpongOn = true  // mono refuses after a valid translator is prepared
        m.arm()
        if engine.errorText == nil || m.armedMidiMaps != maps || m.alloc.held != held ||
            engine.positionFrames != frame || MidiInput.shared.stats.notes != notes {
            fail("midimapcheck refused player changed translator or held notes")
        }
        m.fx.pingpongOn = false
        m.arm()
        if engine.errorText != nil || MidiInput.shared.stats.notes != 0 || !m.alloc.held.isEmpty {
            fail("midimapcheck repair did not install both replacements")
        }
        if failures == failuresBefore {
            say("MIDIMAP ok masks=\(masks) atomic_map=yes atomic_player=yes held_notes=preserved repair=yes")
        }
    }

    //  exclusions: every note has an off; expression is sent only
    // while held; no member CC64, master pressure/CC74, Scala fallback,
    // or specification/conformance claim. Live stays 48 kHz.
    private func midiMPEChecks() {
        let saved = (m.patch, m.fx, m.space, m.midiMaps, m.midiMPE), failuresBefore = failures
        defer {
            (m.patch, m.fx, m.space, m.midiMaps, m.midiMPE) = saved
            m.arm()
        }
        let base = InstrPatchIO.defaultPatch()
        m.patch = PatchLines(base.text + "mod \(base.name) 0 ctl1 pitch0 1200 0\nmod \(base.name) 1 ctl4 pitch1 1200 0\n")
        m.fx = InstrFx(); m.space = InstrSpace()
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "lower:2"
        m.arm()
        if let error = engine.errorText { return fail("mpecheck valid arm: \(error)") }
        m.alloc.noteOn(60, velocityQ15: 26000)
        MidiInput.shared.feedBytes([0x91, 64, 100])
        _ = engine.renderOffline(64)
        let held = m.alloc.held, frame = engine.positionFrames, notes = MidiInput.shared.stats.notes
        let zone = m.armedMidiMPE, maps = m.armedMidiMaps
        for candidate in ["lower:2,upper:2", "lower:0", "upper:16"] {
            m.midiMPE = candidate; m.arm()
            if engine.errorText == nil || m.midiMPE != candidate || m.armedMidiMPE != zone ||
                m.armedMidiMaps != maps || m.alloc.held != held || engine.positionFrames != frame ||
                MidiInput.shared.stats.notes != notes {
                fail("mpecheck refused zone changed active state: \(candidate)")
            }
        }
        m.midiMPE = zone
        for (channel, map) in [(1, "cc1:0"), (0, "pressure:1"), (0, "cc74:4")] {
            m.midiMaps[channel] = map; m.arm()
            if engine.errorText == nil || m.midiMaps[channel] != map || m.armedMidiMPE != zone ||
                m.armedMidiMaps != maps || m.alloc.held != held || engine.positionFrames != frame ||
                MidiInput.shared.stats.notes != notes {
                fail("mpecheck conflicting map changed active state")
            }
            m.midiMaps = maps
        }
        // The failed candidates must not consume the active member's note.
        MidiInput.shared.feedBytes([0x81, 64, 64]); m.alloc.noteOff(60)
        m.arm()
        if engine.errorText != nil || m.armedMidiMPE != zone || !m.alloc.held.isEmpty || MidiInput.shared.stats.notes != 0 {
            fail("mpecheck repair failed")
        }
        let outside = MidiInput.shared.stats.other_channels
        MidiInput.shared.feedBytes([0x9F, 64, 100]); MidiInput.shared.feedBytes([0x8F, 64, 64])
        if MidiInput.shared.stats.notes != 0 || MidiInput.shared.stats.other_channels != outside + 2 {
            fail("mpecheck selected-zone filtering")
        }
        MidiInput.shared.feedBytes([0x91, 64, 100])
        _ = engine.renderOffline(64)
        MidiInput.shared.feedBytes([0xD1, 100]); MidiInput.shared.feedBytes([0xB1, 74, 96])
        _ = engine.renderOffline(64)
        MidiInput.shared.feedBytes([0x81, 64, 64])
        let stats = MidiInput.shared.stats
        if stats.mpe_notes != 1 || stats.mpe_pressure != 1 || stats.mpe_slides != 1 || stats.ctl_events < 2 {
            fail("mpecheck member expression")
        }
        if failures == failuresBefore {
            say("MPEUI ok zone=lower refusals=6 atomic=yes outside=filtered pressure=ctl1 slide=ctl4 live_fs=48000")
        }
    }

    // Use the production controls and native header reader, without rendering
    // or writing a refused candidate. No second effect grammar lives here.
    private func fxHeaderChecks() {
        let saved = m.fx, failuresBefore = failures
        defer { m.fx = saved }
        var checked = 0, refused = 0
        for (name, input, expected) in [
            ("fx.gain.db10", "-601", "-600"), ("fx.gain.db10", "241", "240"),
            ("fx.gain.db10", "0", "0"), ("fx.dither.seed", "0", "1"),
            ("fx.dither.seed", "4294967296", "4294967295"),
            ("fx.pingpong.time", "49999", "50000"), ("fx.pingpong.time", "1000001", "1000000"),
            ("fx.pingpong.feedback", "-1", "0"), ("fx.pingpong.feedback", "29492", "29491"),
            ("fx.pingpong.lowpass", "19", "20"), ("fx.pingpong.lowpass", "20001", "20000"),
            ("fx.pingpong.highpass", "-1", "0"), ("fx.pingpong.highpass", "2001", "2000"),
            ("fx.compressor.threshold", "-1", "0"), ("fx.compressor.threshold", "601", "600"),
            ("fx.compressor.ratio", "9", "10"), ("fx.compressor.ratio", "201", "200"),
            ("fx.compressor.attack", "-1", "0"), ("fx.compressor.attack", "101", "100"),
            ("fx.compressor.release", "0", "1"), ("fx.compressor.release", "5001", "5000"),
            ("fx.compressor.makeup", "-1", "0"), ("fx.compressor.makeup", "241", "240"),
            ("fx.flanger.base", "99", "100"), ("fx.flanger.base", "10001", "10000"),
            ("fx.flanger.depth", "99", "100"), ("fx.flanger.depth", "10001", "10000"),
            ("fx.flanger.rate", "-1", "0"), ("fx.flanger.rate", "2001", "2000"),
            ("fx.flanger.feedback", "-1", "0"), ("fx.flanger.feedback", "29492", "29491"),
            ("fx.flanger.invert", "-1", "0"), ("fx.flanger.invert", "2", "1"),
        ] {
            let error = m.set(name, input); checked += 1
            if error != nil || m.get(name) != expected { fail("fxcheck \(name) boundary \(input)") }
        }
        var f = InstrFx()
        if f.extra != nil { fail("fxcheck new effects must be off initially") }
        f.softclip = true; f.gainOn = true; f.ditherOn = true
        if f.extra != "softclip\ngain -120\ndither 4294967295\n" { fail("fxcheck header order/defaults") }
        f.pingpongOn = true
        if f.extra != "pingpong 375000 19661 3000 120\nsoftclip\ngain -120\ndither 4294967295\n" ||
            !f.busOn(1) { fail("fxcheck pingpong default/order/send bus") }
        f.delayOn = true
        if !(f.extra ?? "").hasPrefix("delay 300000 9830 6000\npingpong ") {
            fail("fxcheck conflicting controls must remain visible to the reader")
        }
        let patch = InstrPatchIO.defaultPatch().text
        func native(_ extra: String, _ space: String, _ accept: Bool, _ diagnostic: String = "") {
            let need = fms_live_header_text(patch, "fxcheck", 1, 48000, nil, extra, nil, 0)
            var buffer = [CChar](repeating: 0, count: max(need, 1))
            guard need > 0, fms_live_header_text(patch, "fxcheck", 1, 48000, nil, extra, &buffer, buffer.count) == need else {
                return fail("fxcheck header construction")
            }
            let header = String(cString: buffer).replacingOccurrences(of: "space off\n", with: space + "\n")
            var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            let player = fms_player_open_live(header, &err, err.count)
            checked += 1
            if let player { fms_player_close(player) } else { refused += 1 }
            if (player != nil) != accept || (!diagnostic.isEmpty && !String(cString: err).contains(diagnostic)) {
                fail("fxcheck native \(extra.trimmingCharacters(in: .whitespacesAndNewlines)) / \(space): \(String(cString: err))")
            }
        }
        for line in ["gain -600\n", "gain 240\n", "gain 0\n", "dither 1\n", "dither 4294967295\n"] {
            native(line, "space off", true); native(line, "space stereo", true)
        }
        for line in ["gain -601\n", "gain 241\n", "dither 0\n", "dither 4294967296\n"] {
            native(line, "space off", false)
        }
        for line in ["gain -120\n", "dither 4294967295\n", "pingpong 375000 19661 3000 120\n"] {
            native(line, "space binaural", true)
            for space in ["space amb1", "space amb3"] { native(line, space, false) } // Records only
            native(line, "space stereo\npanlaw 19661", false)

        }
        for line in ["pingpong 50000 0 20 0\n", "pingpong 1000000 29491 20000 2000\n"] {
            native(line, "space stereo", true)
        }
        for line in [
            "pingpong 49999 0 20 0\n", "pingpong 1000001 0 20 0\n",
            "pingpong 50000 -1 20 0\n", "pingpong 50000 29492 20 0\n",
            "pingpong 50000 0 19 0\n", "pingpong 50000 0 20001 0\n",
            "pingpong 50000 0 20 -1\n", "pingpong 50000 0 20 2001\n",
        ] { native(line, "space stereo", false) }
        native("pingpong 50000 0 20 0\n", "space off", false)
        for extra in ["delay 50000 0 20\npingpong 50000 0 20 0\n",
                      "pingpong 50000 0 20 0\ndelay 50000 0 20\n"] {
            native(extra, "space stereo", false, "delay and pingpong share the delay bus")
        }
        f = InstrFx(); f.compressorOn = true; f.limiterOn = true
        if f.extra != "compressor 120 40 5 120 60\nlimiter 10 200\n" || !f.busOn(3) {
            fail("fxcheck compressor defaults/order/duck bus")
        }
        if m.set("fx.compressor.unknown", "1") == nil || m.set("send.unknown", "1") == nil ||
            m.set("fx.compressor.ratio", "default") == nil {
            fail("fxcheck unknown control or nonnumeric compressor accepted")
        }
        for line in ["compressor 0 10 0 1 0\n", "compressor 600 200 100 5000 240\n"] {
            native(line, "space off", true); native(line, "space stereo", true)
        }
        for fields in ["601 40 5 120 60", "120 9 5 120 60", "120 201 5 120 60",
                       "120 40 101 120 60", "120 40 5 0 60", "120 40 5 5001 60", "120 40 5 120 241",
                       "-1 40 5 120 60", "120 40 5 120", "120 40 5 120 60 0", "default"] {
            native("compressor " + fields + "\n", "space off", false)
        }
        native("compressor 120 40 5 120 60\n", "space binaural", true)
        for space in ["space amb1", "space amb3", "space stereo\npanlaw 19661"] {
            native("compressor 120 40 5 120 60\n", space, false)
        }
        f = InstrFx(); f.flangerOn = true; f.decodable = true; f.unrepresented = ["retained_native_row"]
        if f.extra != "flanger 2000 1000 300 20000 0\nretained_native_row\ndecodable\n" || !f.busOn(4) {
            fail("fxcheck flanger defaults/order/profile last")
        }
        if m.set("fx.flanger.unknown", "1") == nil || m.set("fx.flanger.rate", "default") == nil {
            fail("fxcheck unknown/default flanger accepted")
        }
        for line in ["flanger 100 100 0 0 0\n", "flanger 10000 10000 2000 29491 1\n"] {
            native(line, "space off", true); native(line, "space stereo", true)
        }
        for fields in ["99 100 0 0 0", "10001 100 0 0 0", "2000 99 0 0 0", "10000 10001 0 0 0",
                       "2000 1000 -1 0 0", "2000 1000 2001 0 0", "2000 1000 0 -1 0", "2000 1000 0 29492 0",
                       "2000 1000 0 0 2", "100 101 0 0 0", "2000 1000 0 0", "2000 1000 0 0 0 0", "default"] {
            native("flanger " + fields + "\n", "space off", false)
        }
        native("flanger 2000 1000 0 20000 1\ndecodable\n", "space stereo", true)
        native("flanger 2000 1000 300 20000 1\ndecodable\n", "space stereo", false, "decodable")
        for space in ["space binaural", "space amb1", "space amb3", "space stereo\npanlaw 19661"] {
            native("decodable\n", space, false)
        }
        if failures == failuresBefore { say("FXHEADER ok checks=\(checked) refused=\(refused) order=compressor,limiter,softclip,gain,dither audio_frames=0") }
    }

    // : exercise actual named controls and native canonical events.
    private func crusherChecks() {
        let saved = (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE)
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("CRUSHER \(why)") } }
        defer {
            if engine.isRecording { m.press("stop") }
            (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE) = saved
            engine.errorText = nil; m.arm()
        }
        _ = m.pick("builtin"); m.space = InstrSpace(); m.fx = InstrFx()
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        m.arm(); _ = engine.renderOffline(1)
        require(m.set("live.crush.bits", "8") != nil && m.liveCrushBits == 24 && m.liveCrushRateDiv == 1,
                "disabled crusher refuses before requested values change")
        require(m.set("fx.crush", "1") == nil && m.fx.extra == "crush\n", "typed valueless header toggle")
        m.arm(); _ = engine.renderOffline(1)
        require(m.armedFx?.crushOn == true && m.liveCrushBits == 24 && m.liveCrushRateDiv == 1, "cold arm bypass")
        for (key, value) in [("live.crush.bits", "0"), ("live.crush.bits", "25"), ("live.crush.bits", "1.5"),
                             ("live.crush.rate_div", "0"), ("live.crush.rate_div", "65"), ("live.crush.rate_div", "x")] {
            require(m.set(key, value) != nil && m.liveCrushBits == 24 && m.liveCrushRateDiv == 1, "refuse invalid \(key)=\(value)")
        }
        require(m.set("live.crush.bits", "7") == nil && m.set("live.crush.rate_div", "5") == nil, "paired setter")
        // Deliberately no callback: these older queued edits must not cross Record.
        let held = (engine.positionFrames, engine.runningHash, m.armedFx)
        m.fx.decodable = true; m.arm()
        require(engine.errorText != nil && m.liveCrushBits == 7 && m.liveCrushRateDiv == 5 &&
                engine.positionFrames == held.0 && engine.runningHash == held.1 && m.armedFx == held.2,
                "failed rearm preserves requested values and player")
        m.fx.decodable = false; engine.errorText = nil
        m.press("record")
        require(engine.isRecording && m.liveCrushBits == 24 && m.liveCrushRateDiv == 1, "accepted Record resets bypass display")
        m.alloc.noteOn(60, velocityQ15: 26000); _ = engine.renderOffline(64)
        require(m.set("live.crush.bits", "6") == nil && m.set("live.crush.rate_div", "9") == nil, "recorded edits")
        m.press("record")
        require(m.liveCrushBits == 6 && m.liveCrushRateDiv == 9, "repeated Record preserves requested values")
        _ = engine.renderOffline(1024); m.alloc.noteOff(60); _ = engine.renderOffline(8192); m.press("stop")
        let take = m.takeText
        let rows = take.split(separator: "\n").map { $0.split(separator: " ").map(String.init) }
            .filter { $0.count == 6 && $0[3] == "crush" }
        require(rows.count == m.voices * 2, "two edits reach every armed voice")
        if rows.count == m.voices * 2 {
            for i in rows.indices {
                require(rows[i][2] == String(i % m.voices) && rows[i][4] == "6" &&
                        rows[i][5] == (i < m.voices ? "1" : "9"), "ascending voice order and full operand pair \(i)")
            }
        }
        let url = out.appendingPathComponent("crusher-live.tl")
        do { try take.write(to: url, atomically: true, encoding: .utf8) }
        catch { fail("CRUSHER retain take: \(error)") }
        var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        if let player = InstrPatchIO.withCStrings([url.path], { fms_player_open($0, 1, &err, err.count) }) {
            defer { fms_player_close(player) }
            let count = fms_player_events(player, nil, 0)
            var events = [fms_event_t](repeating: fms_event_t(), count: Int(count))
            _ = fms_player_events(player, &events, count)
            let crush = events.filter { $0.type == UInt32(FMS_EV_CRUSH) }
            require(crush.count == rows.count && crush.enumerated().allSatisfy {
                $0.element.value == 6 && $0.element.bus == ($0.offset < m.voices ? 1 : 9)
            }, "native event projection preserves both operands")
            require(InstrHeader.read(player)?.fx.crushOn == true, "canonical crusher header projection")
        } else { fail("CRUSHER retained take refused: \(String(cString: err))") }
        require(m.set("live.crush.bits", "8") == nil && m.set("live.crush.rate_div", "4") == nil, "queue before fresh rearm")
        m.arm(); m.press("record") // no preparatory callback drains the old pair
        require(m.liveCrushBits == 24 && m.liveCrushRateDiv == 1, "successful rearm bypass")
        m.alloc.noteOn(60, velocityQ15: 26000); _ = engine.renderOffline(256)
        m.alloc.noteOff(60); _ = engine.renderOffline(4096); m.press("stop")
        require(!m.takeText.split(separator: "\n").contains {
            let words = $0.split(separator: " "); return words.count == 6 && words[3] == "crush"
        }, "pre-rearm queued pair excluded from new take")
        do { try m.takeText.write(to: out.appendingPathComponent("crusher-rearm.tl"), atomically: true, encoding: .utf8) }
        catch { fail("CRUSHER retain rearm take: \(error)") }

        require(m.set("live.crush.bits", "7") == nil && m.set("live.crush.rate_div", "5") == nil, "set pair before cancel")
        _ = engine.renderOffline(1)
        m.press("record"); m.press("stop") // cancellation before callback claim
        require(!engine.isRecording && m.liveCrushBits == 7 && m.liveCrushRateDiv == 5,
                "canceled pending Record restores prior requested values")
        m.press("record")
        require(m.set("live.crush.bits", "8") == nil && m.set("live.crush.rate_div", "4") == nil, "edit after pending Record")
        m.press("stop")
        require(m.liveCrushBits == 8 && m.liveCrushRateDiv == 4, "later explicit edit supersedes cancellation backup")
        _ = engine.renderOffline(1)

        m.press("record"); m.press("pause"); _ = engine.renderOffline(1); m.press("stop")
        require(m.takeText.isEmpty && m.liveCrushBits == 24 && m.liveCrushRateDiv == 1,
                "started zero-frame take does not restore old pair from empty text")
        m.press("resume")
        require(m.set("live.crush.bits", "9") == nil && m.set("live.crush.rate_div", "6") == nil, "pair before canceled arm supersession")
        m.press("record"); m.arm()
        require(m.liveCrushBits == 24 && m.liveCrushRateDiv == 1, "successful arm supersedes pending take backup")
        m.press("record"); m.press("stop")
        require(m.liveCrushBits == 24 && m.liveCrushRateDiv == 1, "new cancellation cannot restore obsolete prior take values")
        if before == failures { say("CRUSHER ok checks=\(checks) voices=\(m.voices) take=crusher-live.tl") }
    }

    // Use native record metadata and the actual live/send/take path.
    // The slider continues to address every live voice; it is not a per-voice mixer.
    private func compressorChecks() async {
        let saved = (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE)
        let mode = m.get("values"), before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("COMPRESSOR \(why)") } }
        defer {
            _ = m.pick("builtin"); _ = m.set("values", mode)
            (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE) = saved
            engine.errorText = nil; m.arm()
        }
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        _ = m.set("values", "record")
        for name in ["compressor", "compressor-fast", "duck"] {
            for suffix in ["", "-44100"] {
                let url = engine.recordsRoot.appendingPathComponent("tests/golden/fx/" + name + suffix + ".tl")
                let original = try? Data(contentsOf: url)
                let row = name == "duck" ? "compressor 120 40 5 120 0\n" :
                    (name == "compressor-fast" ? "compressor 120 200 0 120 60\n" : "compressor 120 40 5 120 60\n")
                require(m.openPatch(url, index: 0) == nil, "native import \(name)\(suffix)")
                require(m.fx.compressorOn && (m.fx.extra ?? "").contains(row), "five-field readback \(name)\(suffix)")
                require(m.fx.busOn(3) && m.space.fieldName == (name == "duck" ? "stereo" : "off"), "Space/duck metadata")
                let expected = (m.fx, m.space)
                var nativeSpace = m.space.native, err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
                let n = fms_live_header_text(m.patch.text, "compressor_export", 1, m.patch.fs, &nativeSpace, m.fx.extra, nil, 0)
                var text = [CChar](repeating: 0, count: max(n, 1))
                require(n > 0 && fms_live_header_text(m.patch.text, "compressor_export", 1, m.patch.fs,
                                                     &nativeSpace, m.fx.extra, &text, n) == n, "native header export")
                if let player = fms_player_open_live(text, &err, err.count) {
                    let read = InstrHeader.read(player); fms_player_close(player)
                    require(read?.fx == expected.0 && read?.space == expected.1, "reopened Space/complete FX \(name)\(suffix)")
                } else { fail("COMPRESSOR reopened native header: \(String(cString: err))") }
                require((try? Data(contentsOf: url)) == original, "source bytes changed")
            }
        }
        _ = m.pick("builtin"); m.space = InstrSpace(); m.fx = InstrFx()
        m.fx.compressorOn = true; m.fx.compMakeup = 0; m.fx.limiterOn = true
        _ = engine.renderOffline(1) // drain earlier UI cleanup on its original player
        m.arm(); require(engine.errorText == nil && m.sends.count == 5 && m.sends.allSatisfy { $0 == 0 }, "arm/reset five buses")
        m.press("record"); _ = m.set("send.duck", "0"); m.alloc.noteOn(60, velocityQ15: 26000)
        var samples = engine.renderOffline(64).samples
        require(m.set("send.duck", "16384") == nil, "duck setter")
        samples += engine.renderOffline(256).samples
        // Unknown bus, out-of-range level and an absent audible bus must not
        // enter the resolved take. Queue acceptance is distinct from native application.
        for (bus, level) in [(4, 1), (255, 1), (3, 32769), (0, 1)] {
            var event = fms_event_t(); event.type = UInt32(FMS_EV_SEND)
            event.bus = UInt32(bus); event.value = UInt32(level)
            require(engine.push(event), "invalid event queue")
        }
        samples += engine.renderOffline(64).samples
        m.alloc.noteOff(60); samples += engine.renderOffline(4800).samples; m.press("stop")
        let take = m.takeText
        var sends: [[String]] = []
        for line in take.split(separator: "\n") {
            let tokens = line.split(separator: " ").map(String.init)
            if tokens.count > 3 && tokens[3] == "send" { sends.append(tokens) }
        }
        let initialSends = sends.filter { $0[0] == "0" }
        let changedSends = sends.filter { $0[0] == "1333334" } // ceil(64 * 1e9 / 48000)
        let validInitial = initialSends.allSatisfy { $0.count == 6 && $0[4] == "duck" && $0[5] == "0" }
        let validChanged = changedSends.allSatisfy { $0.count == 6 && $0[4] == "duck" && $0[5] == "16384" }
        require(sends.count == 2 * m.voices && initialSends.count == m.voices &&
                changedSends.count == m.voices && validInitial && validChanged, "explicit initial zero plus one valid duck row per voice")
        let addressed = Set(changedSends.compactMap { Int($0[2]) })
        require(addressed == Set(0..<m.voices), "all live voices addressed")
        require(takeHash(take) == FNV64.of(samples) && samples.contains { $0 != 0 }, "live nonzero hash")
        require((await InstrPatchIO.verifyText(take)).hasPrefix("match:"), "live replay")
        try? take.write(to: out.appendingPathComponent("compressor-duck-live.tl"), atomically: true, encoding: .utf8)
        m.fx.compressorOn = false; m.arm(); m.press("record")
        require(m.armedFx?.busOn(3) == false, "absent compressor disables slider")
        _ = m.set("send.duck", "32768"); _ = engine.renderOffline(64); m.press("stop")
        require(!m.takeText.split(separator: "\n").contains { $0.contains(" send ") }, "native absent-compressor send refusal")
        say("COMPRESSOR \(failures == before ? "ok" : "FAIL") checks=\(checks) goldens=6 fields=5 duck=all_voices native_refusals=yes header_reopen=yes replay=yes")
    }

    private func flangerProfileChecks() async {
        let saved = (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE)
        let mode = m.get("values"), before = failures
        let geometry = (m.pos, m.posZ, m.posTarget, m.moveDuration, m.liveYaw)
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("FLANGER_PROFILE \(why)") } }
        defer {
            _ = m.pick("builtin"); _ = m.set("values", mode)
            (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE) = saved
            m.posTarget = geometry.2; restoreGeometry(geometry.0, geometry.1, geometry.3, geometry.4)
            engine.errorText = nil; m.arm()
        }
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        m.posTarget = -1
        _ = m.set("values", "record")
        for name in ["flanger", "flanger-static", "decodable"] {
            for suffix in ["", "-44100"] {
                let url = engine.recordsRoot.appendingPathComponent("tests/golden/fx/" + name + suffix + ".tl")
                let original = try? Data(contentsOf: url)
                require(m.openPatch(url, index: 0) == nil, "native import \(name)\(suffix)")
                require(m.fx.decodable == (name == "decodable") && m.fx.flangerOn == (name != "decodable"), "profile and five-field readback")
                require(m.fx.flRate == (name == "flanger-static" ? 0 : 300) && m.fx.busOn(4) == (name != "decodable"), "rate/send metadata")
                let expected = (m.fx, m.space)
                var nativeSpace = m.space.native, err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
                let n = fms_live_header_text(m.patch.text, "flanger_export", 1, m.patch.fs, &nativeSpace, m.fx.extra, nil, 0)
                var text = [CChar](repeating: 0, count: max(n, 1))
                require(n > 0 && fms_live_header_text(m.patch.text, "flanger_export", 1, m.patch.fs,
                                                     &nativeSpace, m.fx.extra, &text, n) == n, "native header export")
                if let player = fms_player_open_live(text, &err, err.count) {
                    let read = InstrHeader.read(player); fms_player_close(player)
                    require(read?.fx == expected.0 && read?.space == expected.1, "reopened complete Space/FX")
                } else { fail("FLANGER_PROFILE reopen: \(String(cString: err))") }
                require((try? Data(contentsOf: url)) == original, "source bytes changed")
            }
        }
        _ = m.pick("builtin"); m.space = InstrSpace(); m.fx = InstrFx(); m.fx.flangerOn = true
        _ = engine.renderOffline(1) // drain earlier UI cleanup on its original player
        m.arm(); require(engine.errorText == nil && m.sends.count == 5 && m.sends.allSatisfy { $0 == 0 }, "arm/reset five buses")
        m.press("record"); _ = m.set("send.flanger", "0"); m.alloc.noteOn(60, velocityQ15: 26000)
        var samples = engine.renderOffline(64).samples
        require(m.set("send.flanger", "16384") == nil, "flanger setter")
        samples += engine.renderOffline(256).samples
        for (bus, level) in [(5, 1), (255, 1), (4, 32769), (3, 1)] {
            var event = fms_event_t(); event.type = UInt32(FMS_EV_SEND); event.bus = UInt32(bus); event.value = UInt32(level)
            require(engine.push(event), "invalid event queue")
        }
        samples += engine.renderOffline(64).samples
        m.alloc.noteOff(60); samples += engine.renderOffline(4800).samples; m.press("stop")
        let take = m.takeText
        let sends = take.split(separator: "\n").map { $0.split(separator: " ").map(String.init) }.filter { $0.count > 3 && $0[3] == "send" }
        let changed = sends.filter { $0[0] == "1333334" }
        require(sends.count == 2 * m.voices && changed.count == m.voices &&
                changed.allSatisfy { $0.count == 6 && $0[4] == "flanger" && $0[5] == "16384" }, "one flanger send per voice")
        require(Set(changed.compactMap { Int($0[2]) }) == Set(0..<m.voices), "all voices")
        require(takeHash(take) == FNV64.of(samples) && samples.contains { $0 != 0 }, "nonzero live hash")
        require((await InstrPatchIO.verifyText(take)).hasPrefix("match:"), "live flanger replay")
        try? take.write(to: out.appendingPathComponent("flanger-live.tl"), atomically: true, encoding: .utf8)
        // Staging a profile must not alter the active ordinary player.
        m.fx = InstrFx(); m.space.field = Int(FMS_SPACE_STEREO); m.arm()
        m.fx.decodable = true; _ = m.set("pos.duration", "1000000")
        require(m.setPos(CGPoint(x: 321, y: 1000)) == nil, "staged profile does not alter active plain player")
        _ = engine.renderOffline(64)
        _ = m.set("pos.duration", "0"); m.arm(); require(m.armedFx?.decodable == true && engine.errorText == nil, "arm profile")
        m.fx.decodable = false; _ = m.set("pos.duration", "1000000")
        let oldPos = m.pos
        require(m.setPos(CGPoint(x: 999, y: 1000)) != nil && m.pos == oldPos, "active profile refuses move before UI mutation")
        _ = m.set("pos.duration", "0"); m.press("record")
        require(m.setPos(CGPoint(x: -600, y: 1000)) == nil, "first zero-frame positions queued")
        m.alloc.noteOn(60, velocityQ15: 26000)
        samples = engine.renderOffline(64).samples
        let positioned = m.pos
        require(m.setPos(CGPoint(x: 888, y: 1000)) != nil && m.pos == positioned, "active profile refuses known nonzero frame")
        var event = fms_event_t(); event.type = UInt32(FMS_EV_POS); event.x = 999; event.y = 1000
        require(engine.push(event), "asynchronous native refusal queues")
        samples += engine.renderOffline(64).samples
        require(engine.errorText?.contains("Queue acceptance is not event application") == true, "callback refusal reaches UI diagnostic")
        m.alloc.noteOff(60); samples += engine.renderOffline(4800).samples; m.press("stop")
        let profileTake = m.takeText
        require(profileTake.contains("\ndecodable\n") && !profileTake.contains("pos 999 "), "refused event absent from take")
        let verifiedProfile = await InstrPatchIO.verifyText(profileTake)
        require(takeHash(profileTake) == FNV64.of(samples) && verifiedProfile.hasPrefix("match:"), "profile full replay")
        try? profileTake.write(to: out.appendingPathComponent("profile-live.tl"), atomically: true, encoding: .utf8)
        // Failed arm retains the already active profile and conflicting staged values.
        m.fx.decodable = true; m.fx.flangerOn = true; m.fx.flRate = 300
        let oldInfo = engine.info?.fs
        m.arm()
        require(engine.errorText?.contains("decodable") == true && engine.info?.fs == oldInfo &&
                m.armedFx?.flangerOn == false && m.fx.flangerOn && m.fx.decodable, "failed candidate preserves armed profile")
        _ = m.set("pos.duration", "0")
        say("FLANGER_PROFILE \(failures == before ? "ok" : "FAIL") checks=\(checks) goldens=6 fields=5 buses=5 staged_vs_armed=yes replay=yes")
    }

    // Exercise the actual raw setter, every typed projection and the editor
    // view while the harness rejects the row, then repair through UI setters.
    private func rawEditChecks() {
        let saved = m.patch, failuresBefore = failures
        defer { m.patch = saved }
        let base = "patch probe 0 65536 60 0 0 128 5 400 120 300\npatch probe 1 65536 0 2048 0 128 2 600 240 300\n"
        let cases = ["filter", "filter probe", "filter probe lp", "filter probe lp 1000000",
                     "route", "route probe", "route probe 2", "route probe 4294967295 1",
                     "patch", "patch probe", "patch probe 0", "patch probe 0 65536",
                     "amp probe", "amp probe 5", "egloop probe",
                     "kstype probe 0", "kstype probe 0 level", "fbtype probe 0"]
        for raw in cases {
            m.patch = PatchLines(base + (raw.hasPrefix("patch") ? "" : "amp probe 5 200 120 300\n"))
            m.patch.validate()
            if m.patch.diagnostic != nil { fail("rawcheck initial accepted row") }
            m.patch.editRow(raw.hasPrefix("patch") ? 0 : 2, text: raw)
            let unchanged = m.patch.text
            if m.patch.diagnostic == nil { fail("rawcheck expected diagnostic: \(raw)") }
            for op in 0..<Int(FMS_OPS) {
                for key in InstrCtl.op.keys { _ = m.get("op\(op).\(key)") }
                _ = instrInactive(m.patch, op); _ = opRows(m.patch.op(op))
            }
            for key in InstrCtl.stmt.keys { _ = m.get(key) }
            for key in ["route.ops", "route.carrier.0", "route.w.1.0", "filter.mode", "filter.cutoff", "filter.q"] { _ = m.get(key) }
            for field in m.patch.fields { _ = m.patch.value(field) }
            _ = instrStmtInactive(m.patch); _ = instrStmtProblems(m.patch)
            let view = PatchEditor(patch: Binding(get: { self.m.patch }, set: { self.m.patch = $0 }))
                .frame(width: 760).environment(\.colorScheme, .dark)
            if render(view).isEmpty { fail("rawcheck editor image empty") }
            if m.patch.text != unchanged || m.patch.diagnostic == nil { fail("rawcheck view changed rejected rows") }
            if raw.hasPrefix("filter") { _ = m.set("filter.mode", "1") }
            else if raw.hasPrefix("route") {
                _ = m.set("route.ops", "2"); _ = m.set("route.carrier.0", "1")
            } else if raw.hasPrefix("amp") {
                for (field, value) in [("attack", "5"), ("decay", "200"), ("sustain", "120"), ("release", "300")] {
                    _ = m.set("amp." + field, value)
                }
            } else if raw.hasPrefix("egloop") { _ = m.set("egloop.0.op", "0") }
            else if raw.hasPrefix("kstype") {
                _ = m.set("kstype.0.target", "level")
                _ = m.set("kstype.0.right", "-"); _ = m.set("kstype.0.left", "-")
            } else if raw.hasPrefix("fbtype") { _ = m.set("fbtype.0.type", "square") }
            else { _ = m.set("op0.ratio", "65536") }
            if let error = m.patch.diagnostic { fail("rawcheck repair \(raw): \(error)") }
        }
        filterMetadataChecks(base)
        statementMetadataChecks(base)
        operatorMetadataChecks(base)
        say("RAWEDIT \(failures == failuresBefore ? "ok" : "FAIL") cases=\(cases.count) projections=all typed_repair=yes audio_frames=0")
    }

    private func filterMetadataChecks(_ base: String) {
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("FILTER_PARAMS \(why)") } }
        guard let url = Bundle.main.url(forResource: "params", withExtension: "txt"),
              let raw = try? String(contentsOf: url, encoding: .utf8), let catalog = NativeParams(raw) else {
            return fail("FILTER_PARAMS missing valid bundled fixture")
        }
        let initial = base + "filter probe lp 2000000 707\n"
        let source = raw.split(separator: "\n").map(String.init)
        let playing = (engine.installedSerial, engine.positionFrames, engine.runningHash)
        for (key, token, upper) in [("filter.cutoff", 3, Int(UInt32.max)), ("filter.q", 4, 65535)] {
            m.patch = PatchLines(initial, parameters: catalog)
            guard let field = m.patch.filterField(key),
                  let metadata = catalog.numericField(statement: "filter", token: token),
                  let row = source.firstIndex(where: { $0.hasPrefix("PARAM id=" + field.id + " ") }) else {
                fail("FILTER_PARAMS missing mapping \(key)"); continue
            }
            require(field == metadata && field.range == 0...upper, "exact shared descriptor \(key)")
            require(m.set(key, "-1") == nil && m.get(key) == "0", "lower clamp \(key)")
            require(m.set(key, String(upper + 1)) == nil && m.get(key) == String(upper), "upper clamp \(key)")
            var changed = source
            changed[row] = changed[row].replacingOccurrences(of: " lo=0 ", with: " lo=1 ")
                .replacingOccurrences(of: " hi=\(upper) ", with: " hi=\(upper - 1) ")
            guard let altered = NativeParams(changed.joined(separator: "\n") + "\n") else {
                fail("FILTER_PARAMS invalid changed fixture \(key)"); continue
            }
            m.patch = PatchLines(initial, parameters: altered)
            require(m.patch.filterField(key)?.range == 1...(upper - 1), "changed view range \(key)")
            require(m.set(key, "0") == nil && m.get(key) == "1", "changed lower bound \(key)")
            require(m.set(key, String(upper)) == nil && m.get(key) == String(upper - 1), "changed upper bound \(key)")

            var missing = source; missing.remove(at: row)
            missing[missing.count - 1] = "PARAMS count=\(catalog.parameters.count - 1) statements=\(catalog.statements.count) forms=\(catalog.forms.count)"
            guard let incomplete = NativeParams(missing.joined(separator: "\n") + "\n") else {
                fail("FILTER_PARAMS invalid missing-entry fixture \(key)"); continue
            }
            m.patch = PatchLines(initial, parameters: catalog); m.resetPatchHistory()
            require(m.set(key, "1234") == nil && m.undoPatch() == nil && m.patchJournal.redo.count == 1,
                    "prepare retained redo \(key)")
            m.patch = PatchLines(m.patch.text, parameters: incomplete)
            let prior = m.patch
            let undo = m.patchJournal.undo.map { [$0.before, $0.after] }, redo = m.patchJournal.redo.map { [$0.before, $0.after] }
            var direct = prior
            require(prior.filterField(key) == nil && !InstrCtl.setPatch(&direct, key, 42) && direct == prior,
                    "direct missing setter refusal \(key)")
            require(m.set(key, "42") == NativeParams.unavailableMessage && m.patch == prior &&
                    m.patchJournal.undo.map { [$0.before, $0.after] } == undo &&
                    m.patchJournal.redo.map { [$0.before, $0.after] } == redo, "refusal preserves patch and history \(key)")
            let other = key == "filter.q" ? "filter.cutoff" : "filter.q"
            require(m.patch.filterField(other) != nil && m.set(other, "1000") == nil,
                    "other filter control remains available \(key)")
            let view = PatchEditor(patch: Binding(get: { self.m.patch }, set: { self.m.patch = $0 })).frame(width: 760)
            require(!render(view).isEmpty, "missing metadata view renders \(key)")
            if let index = m.patch.index("filter") { m.patch.editRow(index, text: "filter probe lp 900000 900") }
            require(m.patch.diagnostic == nil && m.patch.filter_q_milli == 900, "raw editing remains available \(key)")

            var oversized = source
            oversized[row] = oversized[row].replacingOccurrences(of: key == "filter.q" ? " type=u16 " : " type=u32 ", with: " type=u64 ")
                .replacingOccurrences(of: " hi=\(upper) ", with: " hi=4294967296 ")
            guard let wide = NativeParams(oversized.joined(separator: "\n") + "\n") else {
                fail("FILTER_PARAMS invalid wide fixture \(key)"); continue
            }
            var refused = PatchLines(initial, parameters: wide)
            let unchanged = refused
            require(refused.filterField(key) == nil && !InstrCtl.setPatch(&refused, key, 42) && refused == unchanged,
                    "unrepresentable metadata refuses \(key)")
        }
        m.patch = PatchLines(base, parameters: nil)
        require(m.set("filter.mode", "1") == nil && m.patch.filter_cutoff_millihz == 2000000 &&
                m.patch.filter_q_milli == 707, "mode and seeds survive missing catalog")
        require(m.set("filter.cutoff", "1000") != nil && m.set("filter.q", "1000") != nil,
                "missing catalog refuses only numeric edits")
        m.patch = PatchLines(base, parameters: catalog)
        require(m.set("filter.mode", "1") == nil && m.patch.filter_cutoff_millihz == 2000000 &&
                m.patch.filter_q_milli == 707, "mode retains existing seeds")
        require(m.set("filter.cutoff", "950000") == nil && m.set("filter.q", "1234") == nil &&
                m.set("filter.mode", "0") == nil && !m.patch.text.contains("filter "), "mode disables the row")
        require(m.set("filter.cutoff", "900000") == nil && !m.patch.text.contains("filter ") &&
                m.patch.filter_cutoff_millihz == 900000 && m.patch.filter_q_milli == 1234,
                "disabled row retains edited values")
        require(m.set("filter.mode", "1") == nil && m.patch.filter_cutoff_millihz == 900000 &&
                m.patch.filter_q_milli == 1234 && m.patch.diagnostic == nil, "re-enable retains values")
        require(engine.installedSerial == playing.0 && engine.positionFrames == playing.1 && engine.runningHash == playing.2,
                "staged metadata edits never arm or advance playback")
        say("FILTER_PARAMS \(failures == before ? "ok" : "FAIL") checks=\(checks) mappings=2 metadata_controls=6 refusal_history=unchanged disabled_values=retained seeds=unchanged audio_frames=0")
    }

    private func statementMetadataChecks(_ base: String) {
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("STATEMENT_PARAMS \(why)") } }
        guard let url = Bundle.main.url(forResource: "params", withExtension: "txt"),
              let raw = try? String(contentsOf: url, encoding: .utf8), let catalog = NativeParams(raw) else {
            return fail("STATEMENT_PARAMS missing valid bundled fixture")
        }
        let source = raw.split(separator: "\n").map(String.init)
        let initial = base + "filter probe lp 2000000 707\nlfo probe 0 5000 10 20 30 40 50 60 1 1 2 0\n" +
            "peg probe 100 200 -100 300 200 400 -200 500 1\nfenv probe 10 20 500 30 1200 1000 261626\n"
        let fixtures: [(String, Int, Int, Int)] = [
            ("lfo.delay", 4, 0, 60000), ("lfo.fade", 5, 0, 60000), ("lfo.pitch", 6, 0, 1200),
            ("lfo.level", 7, 0, 1200), ("lfo.index", 8, 0, 65535), ("lfo.phase", 9, 0, 65535),
            ("peg.l1", 2, -4800, 4800), ("peg.t1", 3, 0, 60000), ("peg.l2", 4, -4800, 4800),
            ("peg.t2", 5, 0, 60000), ("peg.l3", 6, -4800, 4800), ("peg.t3", 7, 0, 60000),
            ("peg.l4", 8, -4800, 4800), ("peg.t4", 9, 0, 60000),
            ("fenv.attack", 2, 0, 60000), ("fenv.decay", 3, 0, 60000), ("fenv.sustain", 4, 0, 1000),
            ("fenv.release", 5, 0, 60000), ("fenv.depth", 6, -9600, 9600), ("fenv.track", 7, 0, 2000)]
        require(Set(fixtures.map { $0.0 }) == Set(PatchLines.statementTokens.keys), "exact migrated key set")
        let playing = (engine.installedSerial, engine.positionFrames, engine.runningHash)
        var rendered = Set<String>()
        for (key, token, lower, upper) in fixtures {
            let statement = String(key.split(separator: ".")[0])
            m.patch = PatchLines(initial, parameters: catalog)
            guard let field = m.patch.statementField(key), let entry = InstrCtl.stmt[key],
                  let definition = catalog.parameters.first(where: { $0["id"] == field.id }),
                  let row = source.firstIndex(where: { $0.hasPrefix("PARAM id=" + field.id + " ") }) else {
                fail("STATEMENT_PARAMS missing mapping \(key)"); continue
            }
            require(entry.range == nil && field == catalog.numericField(statement: statement, token: token) &&
                    field.range == lower...upper, "exact shared descriptor \(key)")
            require(m.set(key, String(lower - 1)) == nil && m.get(key) == String(lower), "lower clamp \(key)")
            require(m.set(key, String(upper + 1)) == nil && m.get(key) == String(upper), "upper clamp \(key)")
            let low = lower < 0 ? lower - 1 : 1, high = upper + 1
            var changed = source
            changed[row] = changed[row].replacingOccurrences(of: " lo=\(lower) ", with: " lo=\(low) ")
                .replacingOccurrences(of: " hi=\(upper) ", with: " hi=\(high) ")
                .replacingOccurrences(of: " type=\(definition["type"]) ", with: lower < 0 ? " type=i64 " : " type=u64 ")
            guard let altered = NativeParams(changed.joined(separator: "\n") + "\n") else {
                fail("STATEMENT_PARAMS invalid changed fixture \(key)"); continue
            }
            m.patch = PatchLines(initial, parameters: altered)
            require(m.patch.statementField(key)?.range == low...high, "changed view range \(key)")
            require(m.set(key, String(low - 1)) == nil && m.get(key) == String(low), "changed lower bound \(key)")
            require(m.set(key, String(high + 1)) == nil && m.get(key) == String(high), "expanded upper bound \(key)")
            require(m.patch.diagnostic != nil, "native validation still rejects synthetic expanded value \(key)")

            var missing = source; missing.remove(at: row)
            missing[missing.count - 1] = "PARAMS count=\(catalog.parameters.count - 1) statements=\(catalog.statements.count) forms=\(catalog.forms.count)"
            guard let incomplete = NativeParams(missing.joined(separator: "\n") + "\n") else {
                fail("STATEMENT_PARAMS invalid missing-entry fixture \(key)"); continue
            }
            m.patch = PatchLines(initial, parameters: catalog); m.resetPatchHistory()
            require(m.set(key, "123") == nil && m.undoPatch() == nil && m.patchJournal.redo.count == 1,
                    "prepare retained redo \(key)")
            m.patch = PatchLines(m.patch.text, parameters: incomplete)
            let prior = m.patch
            let undo = m.patchJournal.undo.map { [$0.before, $0.after] }, redo = m.patchJournal.redo.map { [$0.before, $0.after] }
            var direct = prior
            require(prior.statementField(key) == nil && !InstrCtl.setPatch(&direct, key, 42) && direct == prior,
                    "direct missing setter refusal \(key)")
            require(m.set(key, "42") == NativeParams.unavailableMessage && m.patch == prior &&
                    m.patchJournal.undo.map { [$0.before, $0.after] } == undo &&
                    m.patchJournal.redo.map { [$0.before, $0.after] } == redo, "refusal preserves patch and history \(key)")
            if rendered.insert(statement).inserted {
                let view = PatchEditor(patch: Binding(get: { self.m.patch }, set: { self.m.patch = $0 })).frame(width: 760)
                require(!render(view).isEmpty, "missing metadata view renders \(statement)")
            }
            guard let index = m.patch.index(statement),
                  let original = initial.split(separator: "\n").first(where: { $0.hasPrefix(statement + " ") }) else {
                fail("STATEMENT_PARAMS missing raw fixture \(key)"); continue
            }
            m.patch.editRow(index, text: statement + " probe")
            require(m.patch.diagnostic != nil, "raw invalid text retained \(key)")
            m.patch.editRow(index, text: String(original))
            require(m.patch.diagnostic == nil, "raw recovery remains available \(key)")
        }
        m.patch = PatchLines(initial, parameters: nil)
        for (key, _, _, _) in fixtures {
            let prior = m.patch
            require(m.set(key, "42") == NativeParams.unavailableMessage && m.patch == prior, "absent catalog \(key)")
        }
        for (key, type, low, high) in [("lfo.delay", "u64", "0", "4294967296"),
                ("lfo.delay", "i64", "-1", "60000"), ("peg.l1", "i64", "-2147483649", "4800"),
                ("fenv.depth", "i64", "-9600", "2147483648")] {
            let normal = PatchLines(initial, parameters: catalog)
            guard let field = normal.statementField(key),
                  let row = source.firstIndex(where: { $0.hasPrefix("PARAM id=" + field.id + " ") }) else {
                fail("STATEMENT_PARAMS missing storage fixture \(key)"); continue
            }
            var oversized = source
            oversized[row] = source[row].split(separator: " ").map { word in
                if word.hasPrefix("type=") { return "type=" + type }
                if word.hasPrefix("lo=") { return "lo=" + low }
                if word.hasPrefix("hi=") { return "hi=" + high }
                return String(word)
            }.joined(separator: " ")
            guard let wide = NativeParams(oversized.joined(separator: "\n") + "\n") else {
                fail("STATEMENT_PARAMS invalid storage fixture \(key)"); continue
            }
            m.patch = PatchLines(initial, parameters: wide)
            let prior = m.patch
            var direct = prior
            require(prior.statementField(key) == nil && !InstrCtl.setPatch(&direct, key, 42) && direct == prior &&
                    m.set(key, "42") == NativeParams.unavailableMessage && m.patch == prior,
                    "unrepresentable bounds refuse \(key)")
        }
        m.patch = PatchLines(initial, parameters: nil)
        require(m.set("lfo.rate", "1") == nil && m.get("lfo.rate") == "10" &&
                m.set("lfo.rate", "0") == nil && !m.patch.text.contains("lfo "), "LFO rate alias unchanged")
        require(m.set("peg.mask", "0") == nil && m.patch.tokens("peg").count == 10, "PEG zero mask still omitted")
        require(m.set("fenv.ref", "0") == nil && m.patch.tokens("fenv").count == 8, "FENV zero reference still omitted")
        require(m.set("lfo.free_run", "99") == nil && m.get("lfo.free_run") == "0", "unmigrated enum policy unchanged")
        m.patch = PatchLines(initial, parameters: catalog)
        require(m.set("lfo.rate", "0") == nil && m.set("lfo.delay", "321") == nil &&
                !m.patch.text.contains("lfo ") && m.get("lfo.delay") == "321", "disabled LFO retains edits")
        require(m.set("lfo.rate", "5000") == nil && m.get("lfo.delay") == "321" &&
                m.patch.diagnostic == nil, "LFO re-enable retains values")
        require(m.set("filter.mode", "0") == nil && m.set("fenv.depth", "-1000") == nil &&
                m.get("fenv.depth") == "-1000", "inactive filter envelope retains edits")
        require(m.set("filter.mode", "1") == nil && m.get("fenv.depth") == "-1000" &&
                m.patch.diagnostic == nil, "filter re-enable retains envelope values")
        m.patch = PatchLines(base, parameters: catalog)
        for (key, _, _, _) in fixtures {
            require(m.get(key) == "0" && m.set(key, "0") == nil && m.patch.text == base,
                    "zero seeds preserve omitted statements \(key)")
        }
        require(engine.installedSerial == playing.0 && engine.positionFrames == playing.1 && engine.runningHash == playing.2,
                "staged metadata edits never arm or advance playback")
        say("STATEMENT_PARAMS \(failures == before ? "ok" : "FAIL") checks=\(checks) mappings=20 changed=20 missing=20 storage=4 refusal_history=unchanged aliases=unchanged audio_frames=0")
    }

    private func operatorMetadataChecks(_ base: String) {
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("OPERATOR_PARAMS \(why)") } }
        guard let url = Bundle.main.url(forResource: "params", withExtension: "txt"),
              let raw = try? String(contentsOf: url, encoding: .utf8), let catalog = NativeParams(raw) else {
            return fail("OPERATOR_PARAMS missing valid bundled fixture")
        }
        let source = raw.split(separator: "\n").map(String.init)
        let initial = base + "patch probe 2 65536 60 512 0 128 5 400 120 300\n" +
            "patch probe 3 65536 60 256 0 128 5 400 120 300\nroute probe 4 1 1>0:256\n"
        let fixtures: [(String, Int, Int)] = [("index", 5, 65535), ("feedback", 6, 65535),
            ("attack", 8, 65535), ("decay", 9, 65535), ("release", 11, 65535),
            ("ks_break", 12, Int(UInt32.max)), ("rate_scale", 15, 7),
            ("fixed", 16, Int(UInt32.max)), ("phase", 17, 65535)]
        require(Set(fixtures.map { $0.0 }) == Set(PatchLines.operatorTokens.keys), "exact migrated key set")
        let playing = (engine.installedSerial, engine.positionFrames, engine.runningHash)
        for (key, token, upper) in fixtures {
            let normal = PatchLines(initial, parameters: catalog)
            guard let descriptor = normal.operatorField(key), let entry = InstrCtl.op[key],
                  let row = source.firstIndex(where: { $0.hasPrefix("PARAM id=" + descriptor.id + " ") }) else {
                fail("OPERATOR_PARAMS missing mapping \(key)"); continue
            }
            require(entry.range == nil && descriptor == catalog.numericField(statement: "patch", token: token) &&
                    descriptor.range == 0...upper, "exact shared descriptor \(key)")
            // UInt32-wide fields shrink; every other field expands beyond its old
            // bound, proving the setter has no retained constant-range clamp.
            let high = upper == Int(UInt32.max) ? upper - 1 : upper + 1
            var changed = source
            changed[row] = source[row].split(separator: " ").map { word in
                if word.hasPrefix("type=") { return "type=u64" }
                if word.hasPrefix("lo=") { return "lo=1" }
                if word.hasPrefix("hi=") { return "hi=\(high)" }
                return String(word)
            }.joined(separator: " ")
            var missing = source; missing.remove(at: row)
            missing[missing.count - 1] = "PARAMS count=\(catalog.parameters.count - 1) statements=\(catalog.statements.count) forms=\(catalog.forms.count)"
            guard let altered = NativeParams(changed.joined(separator: "\n") + "\n"),
                  let incomplete = NativeParams(missing.joined(separator: "\n") + "\n") else {
                fail("OPERATOR_PARAMS invalid mutation fixture \(key)"); continue
            }
            for i in 0..<Int(FMS_OPS) {
                let name = "op\(i).\(key)"
                m.patch = normal
                let siblings = m.patch.rows.filter { $0.tokens.count <= 2 || $0.statement != "patch" || $0.tokens[2] != String(i) }
                require(m.set(name, "-1") == nil && m.get(name) == "0", "lower clamp \(name)")
                require(m.set(name, String(upper + 1)) == nil && m.get(name) == String(upper), "upper clamp \(name)")
                require(m.patch.rows.filter { $0.tokens.count <= 2 || $0.statement != "patch" || $0.tokens[2] != String(i) } == siblings,
                        "normal edit preserves siblings and route \(name)")
                m.patch = PatchLines(initial, parameters: altered)
                require(m.patch.operatorField(key)?.range == 1...high, "changed view range \(name)")
                require(m.set(name, "0") == nil && m.get(name) == "1", "changed lower bound \(name)")
                require(m.set(name, String(high + 1)) == nil && m.get(name) == String(high), "changed upper bound \(name)")
                require(m.patch.rows.filter { $0.tokens.count <= 2 || $0.statement != "patch" || $0.tokens[2] != String(i) } == siblings,
                        "changed edit preserves siblings and route \(name)")
                if high > upper { require(m.patch.diagnostic != nil, "native rejects synthetic expanded value \(name)") }
                m.patch = normal; m.resetPatchHistory()
                let different = m.get(name) == "0" ? "1" : "0"
                require(m.set(name, different) == nil && m.undoPatch() == nil && m.patchJournal.redo.count == 1,
                        "prepare retained redo \(name)")
                m.patch = PatchLines(m.patch.text, parameters: incomplete)
                let prior = m.patch
                let undo = m.patchJournal.undo.map { [$0.before, $0.after] }, redo = m.patchJournal.redo.map { [$0.before, $0.after] }
                var direct = prior
                require(prior.operatorField(key) == nil && !InstrCtl.setPatch(&direct, name, 42) && direct == prior,
                        "direct missing setter refusal \(name)")
                require(m.set(name, "42") == NativeParams.unavailableMessage && m.patch == prior &&
                        m.patchJournal.undo.map { [$0.before, $0.after] } == undo &&
                        m.patchJournal.redo.map { [$0.before, $0.after] } == redo, "refusal preserves patch and history \(name)")
                if i == 0 && ["index", "rate_scale"].contains(key) {
                    let view = PatchEditor(patch: Binding(get: { self.m.patch }, set: { self.m.patch = $0 })).frame(width: 760)
                    require(!render(view).isEmpty, "missing numeric/stepper view renders \(key)")
                }
                guard let at = m.patch.index("patch", i) else { fail("OPERATOR_PARAMS missing raw fixture \(name)"); continue }
                let original = m.patch.rows[at].text
                m.patch.editRow(at, text: "patch probe \(i)")
                require(m.patch.diagnostic != nil, "raw invalid retained \(name)")
                m.patch.editRow(at, text: original)
                require(m.patch.diagnostic == nil, "raw repair remains available \(name)")
            }
        }
        m.patch = PatchLines(initial, parameters: nil)
        for i in 0..<Int(FMS_OPS) {
            for (key, _, _) in fixtures {
                let prior = m.patch, name = "op\(i).\(key)"
                require(m.set(name, "42") == NativeParams.unavailableMessage && m.patch == prior, "absent catalog \(name)")
            }
        }
        for (key, type, low, high) in [("index", "u64", "0", "4294967296"), ("rate_scale", "i64", "-1", "7")] {
            let normal = PatchLines(initial, parameters: catalog)
            guard let field = normal.operatorField(key),
                  let row = source.firstIndex(where: { $0.hasPrefix("PARAM id=" + field.id + " ") }) else {
                fail("OPERATOR_PARAMS missing storage fixture \(key)"); continue
            }
            var oversized = source
            oversized[row] = source[row].split(separator: " ").map { word in
                if word.hasPrefix("type=") { return "type=" + type }
                if word.hasPrefix("lo=") { return "lo=" + low }
                if word.hasPrefix("hi=") { return "hi=" + high }
                return String(word)
            }.joined(separator: " ")
            guard let wide = NativeParams(oversized.joined(separator: "\n") + "\n") else {
                fail("OPERATOR_PARAMS invalid storage fixture \(key)"); continue
            }
            for i in 0..<Int(FMS_OPS) {
                m.patch = PatchLines(initial, parameters: wide)
                let prior = m.patch, name = "op\(i).\(key)"
                var direct = prior
                require(prior.operatorField(key) == nil && !InstrCtl.setPatch(&direct, name, 42) && direct == prior &&
                        m.set(name, "42") == NativeParams.unavailableMessage && m.patch == prior,
                        "unrepresentable bounds refuse \(name)")
            }
        }
        // Keep every legacy effective cap and enum/ratio policy, even without a catalog.
        m.patch = PatchLines(initial, parameters: nil)
        for i in 0..<Int(FMS_OPS) {
            for (key, high) in [("level", 1200), ("vel_sens", 256), ("sustain", 1200), ("ks_slope", 1200),
                                ("wave", 11), ("free_run", 2), ("ratio", Int(UInt32.max))] {
                let name = "op\(i).\(key)"
                require(m.set(name, String(high + 1)) == nil && m.get(name) == String(high) &&
                        m.set(name, "-1") == nil && m.get(name) == "0", "legacy caps/conversion boundary \(name)")
            }
        }
        for (key, token, _) in fixtures {
            m.patch = PatchLines(base, parameters: catalog)
            let prior = m.patch.text, old = m.get("op0." + key)
            require(m.set("op0." + key, old) == nil && m.patch.text == prior, "unchanged short header remains short \(key)")
            require(m.set("op0." + key, "1") == nil && m.patch.tokens("patch", 0).count == max(12, token == 12 ? 14 : token + 1),
                    "optional suffix completion \(key)")
            let full = "patch probe 0 65536 60 0 0 128 5 400 120 300 500000 120 3 1 220000 1234 2 retained-extra\n"
            m.patch = PatchLines(full + String(base.split(separator: "\n")[1]) + "\n", parameters: catalog)
            let original = m.patch.tokens("patch", 0)
            require(m.set("op0." + key, "2") == nil && m.patch.tokens("patch", 0).enumerated().allSatisfy {
                $0.offset == token ? $0.element == "2" : $0.element == original[$0.offset]
            } && m.patch.tokens("patch", 0).count == original.count, "long header preserves other fields/extras \(key)")
            m.patch = PatchLines(initial, parameters: catalog)
            guard let at = m.patch.index("patch", 3) else { fail("OPERATOR_PARAMS disabled fixture"); continue }
            m.patch.rows[at].enabled = false
            require(m.set("op3." + key, "2") == nil && !m.patch.rows[at].enabled && m.get("op3." + key) == "2",
                    "disabled operator retains edit \(key)")
            m.patch.rows[at].enabled = true; m.patch.validate()
            require(m.patch.diagnostic == nil && m.get("op3." + key) == "2", "raw re-enable retains edit \(key)")
        }
        require(engine.installedSerial == playing.0 && engine.positionFrames == playing.1 && engine.runningHash == playing.2,
                "staged metadata edits never arm or advance playback")
        say("OPERATOR_PARAMS \(failures == before ? "ok" : "FAIL") checks=\(checks) mappings=9 operators=4 changed=9 missing=9 storage=2 refusal_history=unchanged suffixes=preserved audio_frames=0")
    }

    // Learn uses the same packet feed as CoreMIDI; all ordinary controls below
    // stay deterministic/offline. The separate virtual test opens only its own
    // destination, never an external endpoint or audio device.
    private func learnChecks() async {
        let saved = (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE)
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("LEARN \(why)") } }
        defer {
            _ = m.pick("builtin"); (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE) = saved
            m.cancelLearn(); engine.errorText = nil; m.arm()
        }
        let midi = MidiInput.shared, base = InstrPatchIO.defaultPatch()
        let source = PatchLines(base.text + "mod \(base.name) 0 eg0 pitch0 1200 0\n")
        func sourceValue() -> String {
            m.patch.fields.first { $0.id == "mod.0.source" }.map { m.patch.value($0) } ?? "absent"
        }
        _ = m.pick("builtin"); m.fx = InstrFx(); m.space = InstrSpace()
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        require(m.startLearn(0) != nil && m.startLearn(8) != nil, "absent slot refused")
        m.patch = PatchLines(base.text + "mod \(base.name) 0 ctl2 pitch0 1200 0\n")
        m.midiMaps[3] = "cc7:2"; m.arm(); require(engine.errorText == nil, "old route arm")
        midi.feedBytes([0x93, 60, 100]); _ = engine.renderOffline(64)
        let armed = (m.armedPatch?.text, m.armedMidiMaps, engine.installedSerial)
        m.patch = source; m.midiMaps[3] = "off"; m.selectLearnInput(0, 2)
        require(m.startLearn(0) == nil && m.learnRequest == InstrLearnRequest(slot: 0, input: 2), "start request")
        require(m.startLearn(0) != nil, "second request refused")
        m.selectLearnInput(0, 7); require(m.learnInputs[0] == 2, "pending input is frozen")
        for words: [UInt32] in [[0x20D3_0020], [0x40B3_0700, 0], [0x40B3_0700],
                                [0x20B3_8700], [0x20B3_0780], [0x10F8_0000]] {
            midi.feed(words, ns: midi.now())
            require(m.learnRequest != nil && sourceValue() == "eg0", "ineligible packet leaves request")
        }
        let oldEvents = midi.stats.ctl_events, oldMessages = midi.messages
        midi.feedBytes([0xB3, 7, 100])
        require(m.learnRequest == nil && sourceValue() == "ctl2" && m.midiMaps[3] == "cc7:2", "stages source and map")
        require(midi.stats.ctl_events == oldEvents + 1 && midi.messages == oldMessages + 1, "trigger follows old route once")
        require(m.armedPatch?.text == armed.0 && m.armedMidiMaps == armed.1 && engine.installedSerial == armed.2,
                "learn does not arm")
        require(m.undoPatch() == nil && sourceValue() == "eg0" && m.midiMaps[3] == "cc7:2", "patch undo excludes map")
        require(m.redoPatch() == nil && sourceValue() == "ctl2", "patch redo")
        midi.feedBytes([0x83, 60, 64]); _ = engine.renderOffline(64)
        for original in ["cc7:2", " cc007:2 ", "cc1:0", "off", ""] {
            m.patch = source; m.midiMaps = Array(repeating: "off", count: 16); m.midiMaps[3] = original
            require(m.startLearn(0) == nil, "reuse/append start")
            midi.feedBytes([0xB3, 7, 64])
            let expected = original.contains("cc7:") || original.contains("cc007:") ? original :
                (original == "cc1:0" ? "cc1:0,cc7:2" : "cc7:2")
            require(m.learnRequest == nil && sourceValue() == "ctl2" && m.midiMaps[3] == expected, "preserve/reuse/append \(original)")
        }
        // Each refusal is consumed, visible and atomic. The old translator
        // still receives that one trigger, including when the staged map fails.
        for (map, channel, number, input, zone, filter) in [
            ("default", 0, 7, 2, "off", false), ("cc8:0, default", 0, 7, 2, "off", false),
            ("cc7:3", 0, 7, 2, "off", false), ("cc7+:0", 0, 7, 2, "off", false),
            ("cc7+:0", 0, 39, 2, "off", false), ("rand:7:2", 0, 7, 2, "off", false),
            ("off", 0, 74, 2, "off", true), ("off", 1, 7, 2, "lower:2", false),
            ("off", 0, 7, 1, "lower:2", false), ("off", 0, 7, 4, "lower:2", false),
            ("off", 0, 0, 2, "off", false), ("off", 0, 64, 2, "off", false),
            ("off", 0, 127, 2, "off", false), ("cc0007:2", 0, 7, 2, "off", false)] {
            m.patch = source; m.midiMaps = Array(repeating: "off", count: 16)
            m.midiMaps[channel] = map; m.midiMPE = zone
            if filter { _ = m.set("filter.mode", "1"); _ = m.set("filter.cutoff", "1000000") }
            m.selectLearnInput(0, input); require(m.startLearn(0) == nil, "refusal start")
            let state = m.patchState, maps = m.midiMaps, serial = engine.installedSerial, count = midi.messages
            midi.feedBytes([UInt8(0xB0 + channel), UInt8(number), 64])
            require(m.learnRequest == nil && m.learnStatus.hasPrefix("Learn refused:") && engine.errorText != nil,
                    "visible consumed refusal \(map)/\(number)/\(zone)")
            require(m.patchState == state && m.midiMaps == maps && engine.installedSerial == serial && midi.messages == count + 1,
                    "atomic refusal \(map)/\(number)/\(zone)")
        }
        // A refused proposed replacement must still use the armed route once.
        m.patch = PatchLines(base.text + "mod \(base.name) 0 ctl2 pitch0 1200 0\n")
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMaps[3] = "cc7:2"; m.midiMPE = "off"
        m.arm(); require(engine.errorText == nil, "isolated old route arm")
        midi.feedBytes([0x93, 60, 100]); _ = engine.renderOffline(1)
        m.patch = source; m.midiMaps[3] = "cc7:3"; m.selectLearnInput(0, 2)
        require(m.startLearn(0) == nil, "old route refusal start")
        let beforeRefusedTrigger = midi.stats.ctl_events
        midi.feedBytes([0xB3, 7, 17])
        require(m.learnRequest == nil && m.learnStatus.hasPrefix("Learn refused:") &&
                midi.stats.ctl_events == beforeRefusedTrigger + 1, "refused trigger follows old route once")
        midi.feedBytes([0x83, 60, 64]); _ = engine.renderOffline(1)
        for slot in 0..<8 {
            m.patch = PatchLines(base.text + "mod \(base.name) \(slot) eg0 pitch0 1200 0\n")
            m.midiMaps = Array(repeating: "off", count: 16); m.selectLearnInput(slot, slot)
            require(m.startLearn(slot) == nil, "every slot starts")
            midi.feedBytes([UInt8(0xB0 + slot), 7, 64])
            require(m.learnRequest == nil && m.midiMaps[slot] == "cc7:\(slot)" &&
                    m.patch.text.contains("\(slot) ctl\(slot) pitch0"), "every slot/input stages")
        }
        m.patch = source; m.patch.rows[m.patch.rows.count - 1].enabled = false
        require(m.startLearn(0) != nil, "disabled slot refused")
        m.patch = source; m.selectLearnInput(0, 2); require(m.startLearn(0) == nil, "A/B source start")
        m.storeCompare(0); m.storeCompare(1); m.swapCompare()
        require(m.learnRequest == nil && m.learnStatus.isEmpty, "A/B source cancels request")
        m.patch = source; m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        m.selectLearnInput(0, 2); require(m.startLearn(0) == nil, "removed slot start")
        m.patch = base; midi.feedBytes([0xB0, 7, 64])
        require(m.learnRequest == nil && m.patch == base && m.learnStatus.contains("no longer"), "removed slot refuses")
        m.patch = source; require(m.startLearn(0) == nil, "invalid patch start")
        m.patch = PatchLines(source.text + "filter\n")
        let invalid = m.patchState; midi.feedBytes([0xB0, 7, 64])
        require(m.learnRequest == nil && m.patchState == invalid && m.learnStatus.hasPrefix("Learn refused:"), "native patch refusal")
        m.patch = source; require(m.startLearn(0) == nil, "cancel start"); m.cancelLearn()
        midi.feedBytes([0xB0, 7, 64]); require(sourceValue() == "eg0" && m.learnRequest == nil, "cancel")
        require(m.startLearn(0) == nil, "source reset start"); _ = m.pick("builtin")
        require(m.learnRequest == nil && m.learnStatus.isEmpty, "source reset clears request")
        // Learn is useful before a live player exists, with the old no-live
        // accounting retained. Opening an existing record is not an arm.
        engine.open(paths: [engine.recordsRoot.appendingPathComponent("tests/golden/48000/sine.tl").path], title: "learn record")
        let load = engine.loadID
        for _ in 0..<6000 where engine.installedSerial < load { try? await Task.sleep(for: .milliseconds(5)) }
        require(!engine.isLive, "record setup")
        m.patch = source; m.selectLearnInput(0, 2); require(m.startLearn(0) == nil, "not-live start")
        let notLive = midi.notLive
        midi.feedBytes([0xB0, 7, 64])
        require(sourceValue() == "ctl2" && m.midiMaps[0] == "cc7:2" && midi.notLive == notLive + 1 && !engine.isLive,
                "learn before live retains guard")
        // New maps install only on Apply; records contain resolved ctl events.
        var hashes: [UInt64] = []
        for value: UInt8 in [32, 127] {
            m.arm(); require(engine.errorText == nil && m.armedMidiMaps == m.midiMaps, "Apply installs map")
            _ = engine.renderOffline(1); engine.startRecording()
            midi.feedBytes([0x90, 60, 100]); var samples = engine.renderOffline(256).samples
            midi.feedBytes([0xB0, 7, value]); samples += engine.renderOffline(2048).samples
            midi.feedBytes([0x80, 60, 64]); samples += engine.renderOffline(4096).samples
            engine.stopRecording()
            let text = m.takeText, replay = await InstrPatchIO.verifyText(text)
            require(samples.contains { $0 != 0 } && takeHash(text) == FNV64.of(samples), "nonzero take hash")
            let rows: [[Substring]] = text.split(separator: "\n").map { $0.split(separator: " ") }
            let controls = rows.filter { $0.count > 3 && $0[3] == "ctl" }
            require(controls.count == 1 && controls[0].count == 6 && controls[0][0] == "5333334" &&
                    controls[0][4] == "2" && controls[0][5] == String((Int(value) * 32768 + 63) / 127),
                    "resolved ctl exact; trigger never reinjected")
            require(replay.hasPrefix("match:"), "take replay: \(replay)")
            try? text.write(to: out.appendingPathComponent("learn-\(value).tl"), atomically: true, encoding: .utf8)
            hashes.append(FNV64.of(samples))
        }
        require(hashes.count == 2 && hashes[0] != hashes[1], "learned controller changes PCM")
        say("LEARN \(failures == before ? "ok" : "FAIL") checks=\(checks) one_shot=yes old_route=once refused=atomic map=explicit undo=patch_only apply=required ctl=replayed virtual=separate")
    }

    private func learnVirtualCheck() async {
        let before = failures, midi = MidiInput.shared
        let saved = (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE)
        var client = MIDIClientRef(), port = MIDIPortRef(), destination = MIDIEndpointRef()
        defer {
            if destination != 0 { MIDIEndpointDispose(destination) }
            if port != 0 { MIDIPortDispose(port) }
            if client != 0 { MIDIClientDispose(client) }
            _ = m.pick("builtin"); (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE) = saved
            m.cancelLearn(); engine.errorText = nil; m.arm()
        }
        guard Engine.offline else { return fail("LEARN_VIRTUAL requires offline audio") }
        let unique = "JotTune Synth Learn Test " + UUID().uuidString
        var status = MIDIClientCreateWithBlock(unique as CFString, &client) { _ in }
        guard status == noErr else { return fail("LEARN_VIRTUAL client OSStatus=\(status)") }
        status = MIDIOutputPortCreate(client, "Own destination only" as CFString, &port)
        guard status == noErr else { return fail("LEARN_VIRTUAL port OSStatus=\(status)") }
        status = MIDIDestinationCreateWithProtocol(client, unique as CFString, ._1_0, &destination) { list, _ in MidiInput.received(list) }
        guard status == noErr else { return fail("LEARN_VIRTUAL destination OSStatus=\(status)") }
        func send(_ word: UInt32) async -> Bool {
            let before = midi.messages
            var list = MIDIEventList(), value = word
            let result: OSStatus = withUnsafeMutablePointer(to: &list) { p in
                let packet = MIDIEventListInit(p, ._1_0)
                _ = MIDIEventListAdd(p, MemoryLayout<MIDIEventList>.size, packet, 0, 1, &value)
                guard p.pointee.numPackets == 1 && p.pointee.packet.wordCount == 1 else { return -1 }
                return MIDISendEventList(port, destination, p)
            }
            guard result == noErr else { fail("LEARN_VIRTUAL send OSStatus=\(result)"); return false }
            for _ in 0..<1000 where midi.messages == before { try? await Task.sleep(for: .milliseconds(5)) }
            guard midi.messages == before + 1 else { fail("LEARN_VIRTUAL delivery count \(midi.messages - before)"); return false }
            return true
        }
        _ = m.pick("builtin"); m.space = InstrSpace(); m.fx = InstrFx()
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        let base = InstrPatchIO.defaultPatch()
        m.patch = PatchLines(base.text + "mod \(base.name) 0 eg0 pitch0 1200 0\n")
        m.selectLearnInput(0, 2)
        guard m.startLearn(0) == nil, await send(0x20B0_0760), m.learnRequest == nil,
              m.midiMaps[0] == "cc7:2", m.patch.text.contains("0 ctl2 pitch0") else {
            return fail("LEARN_VIRTUAL learn did not stage source/map")
        }
        m.arm(); guard engine.errorText == nil else { return fail("LEARN_VIRTUAL Apply") }
        _ = engine.renderOffline(1); engine.startRecording()
        guard await send(0x2090_3C64) else { engine.stopRecording(); return }
        var pcm = engine.renderOffline(256).samples
        guard await send(0x20B0_077F) else { engine.stopRecording(); return }
        pcm += engine.renderOffline(2048).samples
        guard await send(0x2080_3C40) else { engine.stopRecording(); return }
        pcm += engine.renderOffline(4096).samples; engine.stopRecording()
        let text = m.takeText, replay = await InstrPatchIO.verifyText(text)
        let ctl = text.split(separator: "\n").filter { $0.split(separator: " ").dropFirst(3).prefix(2) == ["ctl", "2"] }.count
        if !replay.hasPrefix("match:") || ctl == 0 || !pcm.contains(where: { $0 != 0 }) || takeHash(text) != FNV64.of(pcm) {
            fail("LEARN_VIRTUAL resolved take: \(replay)")
        }
        do { try text.write(to: out.appendingPathComponent("learn-virtual.tl"), atomically: true, encoding: .utf8) }
        catch { fail("LEARN_VIRTUAL save: \(error)") }
        say(String(format: "LEARN_VIRTUAL %@ own_destination=yes external_endpoints=0 audio_device=none delivered=4 ctl=%d hash=%016llx replay=%@", failures == before ? "ok" : "FAIL", ctl, FNV64.of(pcm), replay))
    }

    private func favoriteChecks() async {
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("FAVORITES \(why)") } }
        let dir = out.appendingPathComponent("favorite-files", isDirectory: true)
        let a = dir.appendingPathComponent("a.tl"), b = dir.appendingPathComponent("b.tl")
        let saved = (m.patch, m.patchName, m.space, m.fx, m.midiMaps, m.midiMPE)
        defer {
            while !m.favorites.isEmpty { _ = m.removeFavorite(0) }
            _ = m.pick("builtin"); (m.patch, m.patchName, m.space, m.fx, m.midiMaps, m.midiMPE) = saved
            engine.errorText = nil; m.arm()
        }
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            _ = m.pick("builtin"); m.space = InstrSpace(); m.fx = InstrFx()
            m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
            require(m.addFavorite() != nil && m.favorites.isEmpty, "unsaved builtin refused")
            // Save As can normalize a display name; adding requires reopened native rows.
            m.patchName = "favorite-a"
            require(await m.savePatch(to: a, replacingOrigin: false) == nil, "Save As fixture")
            require(m.addFavorite()?.contains("reopen") == true, "saved normalization needs reopen")
            require(m.openPatch(a, index: 0) == nil, "reopen saved patch")
            let canonical = m.patchState
            var header = InstrSpace(); header.field = Int(FMS_SPACE_BINAURAL)
            header.block = 128; header.near = 321; header.order = 1; header.orderPresent = true
            header.pinnaPresent = true; header.pinna = 3; header.coefficients = [1, 2, 3, 4, 5]
            header.yawPresent = true; header.yaw = -30000
            // Retained values with disabled presence are part of the saved Space too.
            header.panlaw = 23111; header.panlawPresent = false
            m.space = header
            require(m.addFavorite() == nil && m.favorites.count == 1 && m.favorites[0].space == header, "save full Space presence")
            let first = m.favorites[0]
            require(first.path == a.standardizedFileURL.path && first.index == 0 && first.name == canonical.name, "identity metadata")
            for kind in 0..<5 {
                if kind == 0 { m.patchName += "-unsaved" }
                if kind == 1 { m.patch.fs = 44100 }
                if kind == 2 { _ = m.set("op0.ratio", "196608") }
                if kind == 3 { m.patch.rows.append(.init(tokens: ["amp", "invalid"], enabled: false)) }
                if kind == 4 { m.patch.editRow(0, text: "patch") }
                require(m.addFavorite() != nil && m.favorites == [first], "unsaved state refused \(kind)")
                require(m.openPatch(a, index: 0) == nil && m.patchState == canonical, "restore native source \(kind)")
            }
            // Different file identity, then duplicate moves A to the top with new Space.
            let bodyB = try InstrPatchIO.patchFileBody(canonical.patch, name: "favorite-b")
            try bodyB.write(to: b, atomically: true, encoding: .utf8)
            require(m.openPatch(b, index: 0) == nil && m.addFavorite() == nil && m.favorites.count == 2, "second favorite")
            require(m.openPatch(a, index: 0) == nil, "reopen A")
            header.yaw = 22000; m.space = header
            require(m.addFavorite() == nil && m.favorites.count == 2 && m.favorites[0].path == first.path && m.favorites[0].space == header, "duplicate updates and moves to top")
            _ = m.pick("builtin"); m.space = InstrSpace(); m.fx = InstrFx(); m.arm()
            _ = engine.renderOffline(1)
            m.fx.gainOn = true; m.fx.gainDb10 = -90
            let keptFX = m.fx, held = (engine.installedSerial, engine.runningHash, engine.positionFrames, engine.voicePos)
            _ = m.set("values", "defaults"); m.fx = keptFX
            let preference = m.get("values.defaultField")
            require(m.loadFavorite(0) == nil && m.patchState == canonical && m.space == header, "restore favorite independent of import switch")
            require(m.fx == keptFX && m.get("values.defaultField") == preference && m.get("values") == "defaults", "retain FX and explicit preference")
            require(engine.installedSerial == held.0 && engine.runningHash == held.1 && engine.positionFrames == held.2 && engine.voicePos == held.3, "favorite load did not arm or import events")
            require(m.patchJournal.undo.isEmpty && m.compareSelected == 0 && m.compareSlots[0] == m.patchState, "favorite is new staged source")
            // Same index/name deliberately loads current valid content, not stored bytes/digest.
            var changed = canonical.patch; var op = changed.op(0); op.ratio_q16 = 196608; changed.setOp(0, op); changed.validate()
            let changedBody = try InstrPatchIO.patchFileBody(changed, name: canonical.name)
            try changedBody.write(to: a, atomically: true, encoding: .utf8)
            require(m.loadFavorite(0) == nil && m.patch.op(0).ratio_q16 == 196608, "current same-identity content loaded")
            func atomicRefusal(_ why: String) {
                let old = (m.patchState, m.space, m.fx, m.source, m.origin, m.recentPaths, m.favorites, m.compareSlots, m.compareSelected)
                require(m.loadFavorite(0) != nil, "refuse \(why)")
                require(m.patchState == old.0 && m.space == old.1 && m.fx == old.2 && m.source == old.3 && m.origin == old.4 && m.recentPaths == old.5 && m.favorites == old.6 && m.compareSlots == old.7 && m.compareSelected == old.8, "atomic \(why)")
                require(engine.installedSerial == held.0 && engine.runningHash == held.1, "refusal player \(why)")
            }
            try changedBody.replacingOccurrences(of: canonical.name, with: "renamed").write(to: a, atomically: true, encoding: .utf8)
            atomicRefusal("name changed")
            try "invalid timeline\n".write(to: a, atomically: true, encoding: .utf8); atomicRefusal("parse failure")
            try FileManager.default.removeItem(at: a); atomicRefusal("missing file")
            try changedBody.write(to: a, atomically: true, encoding: .utf8)
            let bytes = try Data(contentsOf: a)
            require(m.removeFavorite(0) == nil && (try? Data(contentsOf: a)) == bytes && m.favorites.count == 1, "remove metadata only")
            require(m.loadFavorite(-1) != nil && m.removeFavorite(99) != nil && m.favorites.count == 1, "index refusal")
            // Index identity is independent of the saved name: removing a preceding
            // patch must not silently select another patch at that index.
            let multi = changedBody.replacingOccurrences(of: "voice 0 ", with: InstrPatchIO.patchText(canonical.patch, name: "second") + "voice 0 ")
            try multi.write(to: a, atomically: true, encoding: .utf8)
            require(m.openPatch(a, index: 1) == nil && m.addFavorite() == nil && m.favorites[0].index == 1, "indexed favorite")
            try changedBody.write(to: a, atomically: true, encoding: .utf8)
            atomicRefusal("index missing")
            // Persistence controls use an isolated defaults domain, never the owner's list.
            let suite = "fmspike.uitest.favorites." + UUID().uuidString
            guard let defaults = UserDefaults(suiteName: suite) else { return fail("FAVORITES isolated store") }
            defer { defaults.removePersistentDomain(forName: suite) }
            defaults.removePersistentDomain(forName: suite)
            var store = InstrFavorites(defaults: defaults)
            require(store.entries.isEmpty && store.error == nil, "empty persistent store")
            require(store.add(first) == nil, "persistent add")
            require(InstrFavorites(defaults: defaults).entries == [first], "persistent reopen")
            let stored = defaults.data(forKey: InstrFavorites.key)!
            let object = try JSONSerialization.jsonObject(with: stored) as! [String: Any]
            require(Set(object.keys) == ["version", "entries"], "versioned envelope")
            let rows = object["entries"] as! [[String: Any]]
            require(Set(rows[0].keys) == ["path", "index", "name", "space"], "metadata only")
            for kind in 0..<8 {
                var bad = object
                var rows = rows
                if kind == 0 { bad["version"] = 2 }
                if kind == 1 { rows[0]["index"] = -1 }
                if kind == 2 { rows[0]["path"] = "relative.tl" }
                if kind >= 3 {
                    var space = rows[0]["space"] as! [String: Any]
                    if kind == 3 { space["coefficients"] = [1] }
                    if kind == 4 { space["field"] = 99 }
                    if kind == 5 { space["pinna"] = 99 }
                    if kind == 6 { space["near"] = -1 }
                    if kind == 7 { space["yaw"] = Int64(Int32.max) + 1 }
                    rows[0]["space"] = space
                }
                bad["entries"] = rows
                let data = try JSONSerialization.data(withJSONObject: bad)
                defaults.set(data, forKey: InstrFavorites.key)
                let refused = InstrFavorites(defaults: defaults)
                require(refused.entries.isEmpty && refused.error != nil && defaults.data(forKey: InstrFavorites.key) == data, "corrupt metadata preserved \(kind)")
            }
            defaults.set("wrong type", forKey: InstrFavorites.key)
            require(InstrFavorites(defaults: defaults).error != nil && defaults.string(forKey: InstrFavorites.key) == "wrong type", "wrong type preserved")
            defaults.set(Data("broken".utf8), forKey: InstrFavorites.key)
            var damaged = InstrFavorites(defaults: defaults)
            require(damaged.error != nil && damaged.add(first) == nil && damaged.error == nil && InstrFavorites(defaults: defaults).entries == [first], "explicit recovery")
            defaults.set(Data("broken".utf8), forKey: InstrFavorites.key)
            var toRemove = InstrFavorites(defaults: defaults)
            require(toRemove.removeUnreadable() == nil && toRemove.error == nil && InstrFavorites(defaults: defaults).entries.isEmpty,
                    "explicit corrupt metadata removal")
            var memory = InstrFavorites(defaults: nil); require(memory.add(first) == nil && InstrFavorites(defaults: nil).entries.isEmpty, "offline memory isolation")
            // Every safely represented imported field remains storable, including ambisonic metadata.
            for field in 0...4 {
                var entry = first; entry.space.field = field
                require(memory.add(entry) == nil && memory.entries[0].space.field == field, "field metadata \(field)")
            }
            let splitHeader = dir.appendingPathComponent("header.tl"), splitEvents = dir.appendingPathComponent("events.tl")
            let golden = try Data(contentsOf: engine.recordsRoot.appendingPathComponent("tests/golden/48000/bell.tl"))
            var headerBytes = Data(), eventBytes = Data()
            for line in PatchWriteback.lines(golden) {
                if let first = line.tokens.first, first == "hash" || UInt64(first) != nil { eventBytes.append(line.bytes) }
                else { headerBytes.append(line.bytes) }
            }
            try headerBytes.write(to: splitHeader); try eventBytes.write(to: splitEvents)
            let splitPaths = [splitHeader.path, splitEvents.path]
            let beforeFiles = try splitPaths.map { try Data(contentsOf: URL(fileURLWithPath: $0)) }
            engine.open(paths: splitPaths, title: "Split Favorites fixture"); let want = engine.loadID
            for _ in 0..<6000 where engine.installedSerial < want { try? await Task.sleep(for: .milliseconds(5)) }
            require(engine.installedSerial == want && engine.info != nil && engine.errorText == nil, "split fixture accepted")
            m.snapshotRecord(); m.pick(.record(0))
            let list = m.favorites
            require(m.origin?.paths.count == splitPaths.count && m.addFavorite()?.contains("one saved file") == true && m.favorites == list,
                    "multi-file origin requires standalone Save As")
            require(try splitPaths.map { try Data(contentsOf: URL(fileURLWithPath: $0)) } == beforeFiles, "pool files unchanged")
            say("FAVORITES \(failures == before ? "ok" : "FAIL") checks=\(checks) current=validated identity=path_index_name disabled=unsaved space=exact fx=retained armed=no failures=atomic persistence=version1")
        } catch { fail("FAVORITES \(error)") }
    }

    private func compareChecks() async {
        let saved = (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE)
        let geometry = (m.pos, m.posZ, m.posTarget, m.moveDuration, m.liveYaw)
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("PATCHAB \(why)") } }
        defer {
            _ = m.pick("builtin"); (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE) = saved
            m.posTarget = geometry.2; restoreGeometry(geometry.0, geometry.1, geometry.3, geometry.4)
            engine.errorText = nil; m.arm()
        }
        func take(_ name: String) async -> (String, UInt64?) {
            m.alloc.reset(); engine.startRecording(); m.alloc.noteOn(60, velocityQ15: 22000)
            let samples = engine.renderOffline(4096).samples
            m.alloc.noteOff(60); _ = engine.renderOffline(256); engine.stopRecording()
            let text = m.takeText, hash = takeHash(m.takeText)
            require(samples.contains(where: { $0 != 0 }) && hash != nil, "\(name) nonzero take")
            let replay = await InstrPatchIO.verifyText(text)
            require(replay.hasPrefix("match:"), "\(name) replay: \(replay)")
            try? text.write(to: out.appendingPathComponent("ab-\(name).tl"), atomically: true, encoding: .utf8)
            return (text, hash)
        }
        _ = m.pick("builtin"); m.fx = InstrFx(); m.space = InstrSpace(); m.space.field = Int(FMS_SPACE_STEREO)
        m.fx.gainOn = true; m.fx.gainDb10 = -120
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        require(m.compareSelected == 0 && m.compareSlots[0] == m.patchState && m.compareSlots[1] == m.patchState, "source starts A")
        _ = m.set("op0.ratio", "65536"); m.storeCompare(0)
        _ = m.set("op0.ratio", "131072"); m.storeCompare(1)
        let slots = m.compareSlots
        require(m.compareSelected == 0 && slots[0] != slots[1] && m.compareDiff.contains("A 1:") && m.compareDiff.contains("B 1:"), "store selection and ordered diff")
        m.patchName = "unstored"
        require(m.compareSlots == slots, "ordinary edit silently stored")
        m.arm(); _ = engine.renderOffline(1)
        let storedWithoutArm = (engine.installedSerial, engine.runningHash, engine.positionFrames)
        m.storeCompare(1)
        require(engine.installedSerial == storedWithoutArm.0 && engine.runningHash == storedWithoutArm.1 && engine.positionFrames == storedWithoutArm.2,
                "Store re-armed current player")
        // Restore the intended B after the explicit-store-only check.
        m.patchName = slots[1].name; m.storeCompare(1)
        m.patchName = "unstored"
        let sameSpace = m.space, sameFX = m.fx, sameMaps = m.midiMaps
        m.posTarget = 0; _ = m.set("pos.duration", "0"); _ = m.setPos(CGPoint(x: -700, y: 1200), z: 400)
        m.posTarget = 1; _ = m.setPos(CGPoint(x: 900, y: 1800), z: -500)
        _ = engine.renderOffline(64)
        let placements = engine.voicePos
        let pad = (m.pos, m.posZ, m.posTarget, m.moveDuration)
        m.alloc.noteOn(65, velocityQ15: 20000); MidiInput.shared.feedBytes([0x90, 67, 100])
        _ = engine.renderOffline(64)
        m.resetPatchHistory(); let staged = m.patchState
        m.swapCompare()
        require(m.compareSelected == 1 && m.patchState == slots[1] && m.patchJournal.undo.count == 1, "swap B transaction")
        require(m.alloc.held.isEmpty && m.origin == nil, "held notes or origin survived swap")
        _ = engine.renderOffline(64)
        require(!engine.voiceActive.contains(true), "MIDI/computer notes survived successful swap")
        require(engine.voicePos == placements && m.space == sameSpace && m.fx == sameFX && m.midiMaps == sameMaps, "same geometry and controls")
        require(m.pos == pad.0 && m.posZ == pad.1 && m.posTarget == pad.2 && m.moveDuration == pad.3, "staged pad changed")
        let b = await take("stereo-b")
        require(b.0.contains("0 live 0 pos -700 1200 400") && b.0.contains("0 live 1 pos 900 1800 -500"), "seed missing from take")
        m.swapCompare(); require(m.compareSelected == 0 && m.patchState == slots[0], "swap A")
        let a = await take("stereo-a")
        require(a.1 != b.1 && a.0.contains("gain -120") && b.0.contains("gain -120"), "distinct takes with same FX")
        let unchanged = (engine.runningHash, engine.positionFrames, engine.installedSerial)
        var refusedSeed = m.compareGeometry(); refusedSeed[0].x = 16777217
        var refusedInstalled = true
        engine.openLive(patch: m.patch, name: "badseed", voices: m.voices, space: m.space, extra: m.fx.extra,
                        geometry: refusedSeed) { refusedInstalled = $0 }
        require(!refusedInstalled && engine.runningHash == unchanged.0 && engine.positionFrames == unchanged.1 &&
                engine.errorText?.contains("geometry seed pos") == true, "candidate seed failure replaced player")
        engine.errorText = nil
        let afterRefusalSerial = engine.installedSerial
        require(m.undoPatch() == nil && engine.runningHash == unchanged.0 && engine.positionFrames == unchanged.1 && engine.installedSerial == afterRefusalSerial, "undo armed")
        require(m.redoPatch() == nil && m.patchState == slots[0], "redo A")
        // A refused stored side stays selected and visible, while the sound continues.
        m.patch.rows.append(PatchLines.Line(tokens: ["filter"]))
        m.patch.validate(); m.storeCompare(1)
        require(m.compareDiff.contains("B refused:"), "invalid diagnostic absent from diff")
        m.swapCompare()
        require(m.compareSelected == 1 && m.patch.diagnostic != nil && engine.runningHash == unchanged.0 && engine.positionFrames == unchanged.1,
                "refused side replaced sounding player")
        require(m.undoPatch() == nil && m.patchState == slots[0], "refused side undo")
        m.patch.rows.append(PatchLines.Line(tokens: ["filter"], enabled: false)); m.storeCompare(1)
        require(m.compareDiff.contains("[disabled] filter"), "disabled row absent from diff")
        // Binaural preserves accepted live yaw without changing header presence/value.
        _ = m.pick("builtin"); m.fx = InstrFx(); m.space.field = Int(FMS_SPACE_BINAURAL)
        m.space.yawPresent = true; m.space.yaw = 17000
        m.storeCompare(0); _ = m.set("op0.ratio", "196608"); m.storeCompare(1)
        m.arm(); _ = engine.renderOffline(1)
        m.posTarget = 0; _ = m.set("pos.duration", "0"); _ = m.setPos(CGPoint(x: -1400, y: 900), z: 350)
        require(m.setYaw(-47000) == nil, "accepted live yaw")
        _ = engine.renderOffline(128)
        let beforeMove = engine.voicePos[0]
        _ = m.set("pos.duration", "1000000000"); _ = m.setPos(CGPoint(x: 2000, y: 1500), z: -250)
        _ = engine.renderOffline(256)
        let frozen = engine.voicePos
        require(frozen[0] != beforeMove && frozen[0] != SIMD3<Int32>(2000, 1500, -250), "moving display fixture")
        m.swapCompare()
        require(engine.voicePos == frozen && m.liveYaw == -47000 && m.space.yaw == 17000, "moving static freeze and yaw")
        _ = engine.renderOffline(512)
        require(engine.voicePos == frozen, "trajectory continued after swap")
        let binaural = await take("binaural")
        require(binaural.0.contains("0 live 0 yaw -47000") && binaural.0.contains("yaw 17000"), "yaw header/seed distinction")
        // The snapshot helper uses only existing published voices; new voices
        // use native defaults. A Records source must never import its events.
        engine.openLive(patch: m.patch, name: "few", voices: 1, space: m.space, extra: nil) { _ in }
        _ = engine.renderOffline(1)
        let added = m.compareGeometry()
        require(added.count == 9 && added[1].x == 0 && added[1].y == 1000 && added[7].z == 0, "added spatial voice defaults")
        m.space.field = Int(FMS_SPACE_OFF)
        let off = m.compareGeometry()
        require(off.count == 8 && off[1].x == 0 && off[1].y == 0 && off[7].z == 0, "added off voice defaults")
        m.space.yawPresent = false; m.arm(); _ = engine.renderOffline(1); m.space.field = Int(FMS_SPACE_BINAURAL)
        require(m.compareGeometry().count == 8, "off source incorrectly seeded yaw")
        m.space.yawPresent = true; m.space.yaw = 23000; m.swapCompare()
        require(m.liveYaw == 23000 && m.armedSpace?.yaw == 23000, "candidate header yaw was not retained")
        let record = engine.recordsRoot.appendingPathComponent("tests/golden/space/binaural/binaural3.tl")
        engine.open(paths: [record.path], title: "A/B defaults fixture")
        let serial = engine.loadID
        for _ in 0..<6000 where engine.installedSerial < serial { try? await Task.sleep(for: .milliseconds(5)) }
        _ = engine.renderOffline(128)
        let noImport = m.compareGeometry()
        require(noImport.count == 8 && noImport.allSatisfy { $0.x == 0 && $0.y == 1000 && $0.z == 0 }, "record event geometry imported")
        _ = m.pick("builtin")
        require(m.compareSelected == 0 && m.compareSlots[0] == m.patchState && m.compareSlots[1] == m.patchState && m.patchJournal.undo.isEmpty, "source reset A/B")
        require(staged != slots[1], "journal before fixture")
        picture("patch-ab", settled(InstrumentView().environmentObject(engine).frame(width: 1100, height: 760).environment(\.colorScheme, .dark)), against: nil)
        say("PATCHAB \(failures == before ? "ok" : "FAIL") checks=\(checks) selection=explicit journal=one seed=static yaw=conditional moving=frozen replay=yes refused=preserved notes=cleared")
    }

    private func patchJournalChecks() {
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("PATCHUNDO \(why)") } }
        func canonical(_ state: InstrPatchState) -> String? {
            let n = fms_live_header_text(state.patch.text, state.name, 1, state.patch.fs, nil, nil, nil, 0)
            guard n > 0 else { return nil }
            var text = [CChar](repeating: 0, count: n), error = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            guard fms_live_header_text(state.patch.text, state.name, 1, state.patch.fs, nil, nil, &text, n) == n,
                  let player = fms_player_open_live(text, &error, error.count) else { return nil }
            defer { fms_player_close(player) }
            return PatchLines.read(player, 0, fs: state.patch.fs)?.text
        }
        let saved = (m.patch, m.patchName, m.space, m.fx, m.midiMaps)
        defer {
            (m.patch, m.patchName, m.space, m.fx, m.midiMaps) = saved
            m.resetPatchHistory()
        }
        m.pick(.builtin)
        let initial = m.patchState, initialNative = canonical(m.patchState)
        require(initialNative != nil, "initial native readback")
        let playing = (engine.installedSerial, engine.positionFrames, engine.runningHash)
        let original = m.get("op0.level")
        m.set("op0.level", original == "100" ? "101" : "100")
        let edited = m.patchState, editedNative = canonical(m.patchState)
        require(editedNative != nil && editedNative != initialNative, "edited native readback differs")
        guard m.patchJournal.undo.count == 1, m.undoPatch() == nil, m.patchState == initial else {
            return fail("PATCHUNDO missing journal entry or failed undo")
        }
        require(m.patchJournal.undo.isEmpty && m.patchJournal.redo.count == 1, "undo moves one transaction")
        require(canonical(m.patchState) == initialNative, "undo native readback")
        require(m.redoPatch() == nil && m.patchState == edited, "redo exact state")
        require(canonical(m.patchState) == editedNative, "redo native readback")
        require(engine.installedSerial == playing.0 && engine.positionFrames == playing.1 && engine.runningHash == playing.2,
                "undo or redo armed or advanced the player")
        let count = m.patchJournal.undo.count
        m.set("op0.level", m.get("op0.level")); m.patch.validate()
        require(m.patchJournal.undo.count == count, "no-op or diagnostic refresh recorded")
        m.set("op0.level", "not-a-number")
        require(m.patchJournal.undo.count == count, "refused nonmutation recorded")
        m.set("name", "journal_name")
        require(m.patchJournal.undo.count == count + 1 && m.undoPatch() == nil && m.patchName == edited.name, "display name undo")
        _ = m.redoPatch()
        m.resetPatchHistory()
        let valid = m.patchState
        m.patch.rows.append(PatchLines.Line(tokens: ["filter"]))
        m.patch.validate()
        let invalid = m.patchState
        require(m.patch.diagnostic != nil && m.patchJournal.undo.count == 1, "invalid raw row retained as one edit")
        require(m.undoPatch() == nil && m.patchState == valid && m.patch.diagnostic == nil, "invalid edit undo validates")
        require(m.redoPatch() == nil && m.patchState == invalid && m.patch.diagnostic != nil, "invalid edit redo validates")
        let last = m.patch.rows.count - 1
        m.patch.rows[last].enabled = false
        let disabled = m.patchState
        require(m.undoPatch() == nil && m.patchState == invalid && m.redoPatch() == nil && m.patchState == disabled,
                "disabled malformed token retained")
        m.patch.fs = 44100
        require(m.undoPatch() == nil && m.patch.fs == disabled.patch.fs && m.redoPatch() == nil && m.patch.fs == 44100, "fs replay")
        m.pick(.builtin); require(m.patchJournal.undo.isEmpty && m.patchJournal.redo.isEmpty, "source selection clears both stacks")
        _ = m.set("statement.add", "shape")
        let added = m.patchState
        require(m.patchJournal.undo.count == 1 && m.undoPatch() == nil && !m.patch.rows.contains { $0.statement == "shape" }, "statement addition undo")
        require(m.redoPatch() == nil && m.patchState == added, "statement addition redo")
        m.pick(.builtin)
        let bindingBefore = m.patchState
        _ = InstrCtl.setPatch(&m.patch, "op0.level", 111)
        require(m.patchJournal.undo.count == 1 && m.undoPatch() == nil && m.patchState == bindingBefore,
                "direct typed editor binding bypassed journal")
        m.pick(.builtin)
        let bankBefore = m.patchState
        let bankPath = engine.recordsRoot.appendingPathComponent("tests/golden/48000/wtframe.tl").path
        if case .success(let (source, _, _)) = InstrPatchIO.loadPatch0(paths: [bankPath]), let bank = source.bankNames.first {
            require(m.patch.importBank(bank, from: source) == nil && m.patchJournal.undo.count == 1, "bank import one transaction")
            let imported = m.patchState
            require(m.undoPatch() == nil && m.patchState == bankBefore && m.redoPatch() == nil && m.patchState == imported,
                    "bank asset rows undo/redo")
            let n = m.patchJournal.undo.count
            require(m.patch.importBank("missing", from: source) != nil && m.patchJournal.undo.count == n, "refused bank import recorded")
        } else { fail("PATCHUNDO bank fixture unavailable") }
        m.pick(.builtin)
        m.set("op0.level", "0"); m.resetPatchHistory()
        for n in 1...70 { m.set("op0.level", String(n)) }
        require(m.patchJournal.undo.count == 64 && m.patchJournal.redo.isEmpty, "64 transaction limit")
        for _ in 0..<64 { require(m.undoPatch() == nil, "bounded undo") }
        require(m.get("op0.level") == "6" && m.undoPatch() != nil && m.patchJournal.redo.count == 64, "oldest eviction")
        require(m.redoPatch() == nil && m.get("op0.level") == "7", "bounded redo")
        m.set("op0.level", "100")
        require(m.patchJournal.redo.isEmpty && m.redoPatch() != nil, "new edit discards redo branch")
        let edits = m.patchJournal.undo.count
        m.set("space.block", "32"); m.set("fx.chorus", "1"); m.set("midi.ctl.1", "cc1:0")
        let outside = (m.space, m.fx, m.midiMaps)
        require(m.patchJournal.undo.count == edits, "Space/FX/map edits entered patch journal")
        _ = m.undoPatch()
        require(m.space == outside.0 && m.fx == outside.1 && m.midiMaps == outside.2, "patch undo changed excluded fields")
        require(!InstrPatchIO.patchText(m.patch, name: m.safeName).contains("# edit"), "journal serialized")
        let view = InstrumentView().environmentObject(engine).frame(width: 1100, height: 760)
            .environment(\.colorScheme, .dark)
        picture("patch-undo", settled(view), against: nil)
        say("PATCHUNDO \(failures == before ? "ok" : "FAIL") checks=\(checks) depth=64 invalid=retained disabled=retained replay=validated native=exact branch=yes outside=unchanged armed=no serialization=none")
    }

    private func deviceRateChecks() async {
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("DEVICE_RATE \(why)") } }
        func fresh(_ nominal: Double = 44100) -> (FakeDeviceRate, DeviceRateControl) {
            let fake = FakeDeviceRate()
            fake.snapshot = DeviceRateSnapshot(id: 7, uid: "test-device", alive: true, nominal: nominal, output: nominal,
                                               ranges: [44100...96000], settable: true)
            return (fake, DeviceRateControl(fake))
        }
        do {
            let (fake, control) = fresh()
            require(fake.writes.isEmpty && control.canRequest(48000) && control.canRequest(46000), "initial no-write and full range interior")
            require(!control.canRequest(44099) && !control.canRequest(96001) && !control.canRequest(44100), "exact capability bounds")
            require(control.request(48000) == nil && fake.writes == [48000] && control.pending && control.restoreRate == nil, "request is not confirmation")
            let token = control.generation
            require(control.request(96000) != nil && control.restore() != nil && fake.writes.count == 1, "pending controls refuse")
            fake.snapshot.nominal = 48000; fake.snapshot.output = 48000; control.refresh()
            require(control.pending, "setter/readback alone confirmed")
            fake.notify(nominal: false); require(control.pending, "non-rate notification confirmed")
            control.timeout(token)
            require(control.phase == .error && control.restoreRate == nil && fake.writes.count == 1, "timeout wrote or offered unconfirmed restore")
            fake.notify(token)
            require(control.phase == .confirmed && control.restoreRate == 44100 && fake.writes.count == 1, "late confirmation receipt")
            fake.writeError = "test setter refusal"
            require(control.request(96000) == "test setter refusal" && control.restoreRate == 44100 && fake.writes.count == 1, "setter error lost prior confirmed receipt")
            fake.writeError = nil
            require(control.request(96000) == nil, "second explicit switch")
            let newer = control.generation
            fake.snapshot.nominal = 96000; fake.snapshot.output = 96000
            fake.notify(token); require(control.pending, "stale callback confirmed newer generation")
            fake.notify(newer)
            require(control.restoreRate == 44100 && fake.writes == [48000, 96000], "repeated switch lost original baseline")
            require(control.restore() == nil && control.pending, "explicit restore request")
            fake.snapshot.nominal = 44100; fake.snapshot.output = 44100; fake.notify()
            require(control.phase == .confirmed && control.restoreRate == nil && fake.writes == [48000, 96000, 44100], "restore confirmation")
        }
        do {
            let (fake, control) = fresh()
            require(control.request(48000) == nil, "expired supersession setup")
            let old = control.generation
            fake.snapshot.nominal = 48000; control.timeout(old)
            require(control.request(96000) == nil, "explicit supersession after timeout")
            let newer = control.generation
            fake.snapshot.nominal = 96000; fake.notify(old)
            require(control.pending && control.restoreRate == nil, "expired stale notification")
            fake.notify(newer); require(control.restoreRate == 44100, "expired same-target baseline retained")
        }
        do {
            let (fake, control) = fresh()
            _ = control.request(48000); let old = control.generation; control.timeout(old)
            fake.snapshot.nominal = 88200; fake.snapshot.output = 88200
            require(control.request(96000) == nil, "changed actual rate supersession")
            fake.snapshot.nominal = 96000; fake.notify()
            require(control.restoreRate == 88200, "changed actual baseline was not used")
            fake.snapshot.nominal = 48000; fake.notify()
            require(control.restoreRate == nil, "external rate did not clear restore")
        }
        for defect in ["dead", "read-only", "missing", "invalid-rate", "bad-range"] {
            let (fake, control) = fresh()
            switch defect {
            case "dead": fake.snapshot.alive = false
            case "read-only": fake.snapshot.settable = false
            case "missing": fake.snapshot.id = 0; fake.snapshot.uid = ""
            case "invalid-rate": fake.snapshot.nominal = .nan
            default: fake.snapshot.ranges = [96000...192000]
            }
            control.refresh()
            require(control.request(48000) != nil && fake.writes.isEmpty, "refused capability \(defect)")
        }
        do {
            let (fake, control) = fresh()
            fake.beforeWrite = { fake.snapshot.uid = "replacement" }
            require(control.request(48000) != nil && fake.writes.isEmpty, "identity revalidation before write")
        }
        do {
            let (fake, control) = fresh()
            _ = control.request(48000); fake.snapshot.nominal = 48000; fake.notify()
            let old = control.generation
            fake.snapshot.uid = "replacement"; fake.snapshot.id = 8; fake.notify(nominal: false)
            require(control.restoreRate == nil && control.restore() != nil && fake.writes == [48000], "replacement device restore")
            fake.notify(old); require(control.restoreRate == nil, "retired-device callback restored receipt")
        }
        for changed in ["uid", "rate", "capability"] {
            let (fake, control) = fresh()
            _ = control.request(48000); fake.snapshot.nominal = 48000; fake.notify()
            switch changed {
            case "uid": fake.snapshot.id = 8; fake.snapshot.uid = "unnotified-replacement"
            case "rate": fake.snapshot.nominal = 88200
            default: fake.snapshot.settable = false
            }
            require(control.restore() != nil && fake.writes == [48000], "unnotified restore revalidation \(changed)")
        }
        do {
            let (fake, control) = fresh(48000)
            require(control.request(44100) == nil, "44100 request")
            fake.snapshot.nominal = 44100; fake.snapshot.output = 44100; fake.notify()
            require(control.restoreRate == 48000, "44100 confirmation and baseline")
        }
        // The same coordinator is attached to the real Engine's fake backend.
        // Exercise all non-click paths before making any explicit request.
        guard Engine.offline, let fake = engine.deviceControl.backend as? FakeDeviceRate else { return fail("DEVICE_RATE hardware backend in script mode") }
        let saved = (m.patch, m.patchName, m.space, m.fx, m.midiMaps, m.midiMPE)
        defer {
            (m.patch, m.patchName, m.space, m.fx, m.midiMaps, m.midiMPE) = saved
            engine.simulateDeviceRate(0); m.arm(); engine.errorText = nil
        }
        engine.simulateDeviceRate(44100); fake.writes.removeAll()
        m.patch = InstrPatchIO.defaultPatch(); m.patchName = "rate_test"; m.space = InstrSpace(); m.fx = InstrFx()
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        m.arm(); engine.play(); _ = engine.renderOffline(64); engine.stop(); engine.readDeviceRate()
        guard fake.writes.isEmpty else { return fail("DEVICE_RATE automatic write before explicit click") }
        require(true, "arm/play/pause/refresh do not write")
        engine.play(); m.alloc.noteOn(60, velocityQ15: 26000); _ = engine.renderOffline(64)
        let active = (engine.positionFrames, engine.runningHash, engine.isPlaying, engine.info?.fs)
        let recoveries = engine.rateRecoveries
        engine.configurationChanged(); engine.configurationChanged()
        for _ in 0..<30 where engine.rateRecoveries == recoveries { await Task.yield() }
        require(engine.rateRecoveries == recoveries + 1 && engine.positionFrames == active.0 && engine.runningHash == active.1 &&
                engine.isPlaying == active.2 && engine.info?.fs == active.3 && fake.writes.isEmpty, "coalesced recovery changed integer state or wrote")
        require(engine.requestDeviceRate(48000) == nil && fake.writes == [48000] && engine.deviceControl.pending, "Engine explicit click")
        fake.snapshot.nominal = 48000; fake.snapshot.output = 48000; fake.notify()
        require(engine.deviceControl.phase == .confirmed && engine.deviceRate == 48000 && engine.deviceControl.restoreRate == 44100,
                "Engine confirmed actual label")
        require(engine.positionFrames == active.0 && engine.runningHash == active.1 && engine.isPlaying == active.2, "rate changed player")
        engine.stop(); let paused = (engine.positionFrames, engine.runningHash)
        engine.configurationChanged(); let count = engine.rateRecoveries
        for _ in 0..<30 where engine.rateRecoveries == count { await Task.yield() }
        require(!engine.isPlaying && engine.positionFrames == paused.0 && engine.runningHash == paused.1, "paused recovery")
        require(engine.requestDeviceRate(44100, restore: true) == nil, "Engine restore click")
        fake.snapshot.nominal = 44100; fake.snapshot.output = 44100; fake.notify()
        require(engine.deviceControl.restoreRate == nil && engine.deviceRate == 44100, "Engine restore readback")
        require(engine.requestDeviceRate(48000) == nil, "source-change setup")
        let pending = engine.deviceControl.generation, writes = fake.writes.count
        let path = engine.recordsRoot.appendingPathComponent("tests/golden/44100/sine.tl").path
        engine.open(paths: [path], title: "rate source change")
        let serial = engine.loadID
        for _ in 0..<6000 where engine.installedSerial < serial { try? await Task.sleep(for: .milliseconds(5)) }
        require(engine.info?.fs == 44100 && fake.writes.count == writes, "source load automatically wrote")
        fake.snapshot.nominal = 48000; fake.snapshot.output = 48000; fake.notify(pending)
        require(engine.info?.fs == 44100 && engine.loadedPaths == [path] && fake.writes.count == writes, "late confirmation reinstated old source")
        let view = DeviceRateView(control: engine.deviceControl, playerFs: 44100) { _, _ in }
            .padding(12).frame(width: 650).background(Color.black).environment(\.colorScheme, .dark)
        picture("device-rate", settled(view), against: nil)
        say("DEVICE_RATE \(failures == before ? "ok" : "FAIL") checks=\(checks) no_click_writes=0 confirmation=notification timeout=5s late=yes superseded=yes restore=explicit recovery=coalesced player=unchanged hardware=not_run")
    }

    private func spacePreferenceChecks() {
        let before = failures
        var checks = 0
        func require(_ ok: Bool, _ why: String) { checks += 1; if !ok { fail("SPACEPREF \(why)") } }
        let suite = "fmspike.uitest.field." + UUID().uuidString
        guard let defaults = UserDefaults(suiteName: suite) else { return fail("SPACEPREF isolated store") }
        defaults.removePersistentDomain(forName: suite)
        defer { defaults.removePersistentDomain(forName: suite) }
        require(InstrFieldPreference(defaults: defaults).field == Int(FMS_SPACE_STEREO), "empty store default")
        for field in 0...2 {
            var choice = InstrFieldPreference(defaults: defaults); choice.remember(field)
            require(choice.field == field && defaults.string(forKey: InstrFieldPreference.key) == InstrSpace.fields[field], "explicit write \(field)")
            require(InstrFieldPreference(defaults: defaults).field == field, "new session reload \(field)")
            choice.remember(3); require(choice.field == field && InstrFieldPreference(defaults: defaults).field == field, "amb refusal preserves preference")
        }
        for invalid in ["", "amb1", "amb3", "bad", "1", "-1"] {
            defaults.set(invalid, forKey: InstrFieldPreference.key)
            require(InstrFieldPreference(defaults: defaults).field == Int(FMS_SPACE_STEREO), "invalid stored choice \(invalid)")
        }
        defaults.set(17, forKey: InstrFieldPreference.key)
        require(InstrFieldPreference(defaults: defaults).field == Int(FMS_SPACE_STEREO), "wrong stored type")
        var memory = InstrFieldPreference(defaults: nil); memory.remember(Int(FMS_SPACE_BINAURAL))
        require(memory.field == Int(FMS_SPACE_BINAURAL) && InstrFieldPreference(defaults: nil).field == Int(FMS_SPACE_STEREO), "offline isolation")
        let saved = (m.space, m.fx, m.get("values.defaultField"))
        defer { _ = m.set("space.field", saved.2); m.space = saved.0; m.fx = saved.1 }
        require(m.set("space.field", "binaural") == nil && m.get("values.defaultField") == "binaural", "model explicit choice")
        // The initial header path assigns Space directly; it must not write
        // the separately stored explicit field choice.
        m.space = InstrSpace(); m.space.field = Int(FMS_SPACE_STEREO)
        require(m.get("values.defaultField") == "binaural", "import changed preference")
        require(m.set("space.field", "amb1") != nil && m.get("values.defaultField") == "binaural", "refused field changed preference")
        m.stereo = false; require(m.get("values.defaultField") == "off", "legacy explicit toggle")
        say("SPACEPREF \(failures == before ? "ok" : "FAIL") checks=\(checks) first=stereo reload=yes imports=excluded invalid=fallback offline=isolated")
    }

    private func spaceImportChecks() async {
        let saved = (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE)
        let preferred = m.get("values.defaultField"), mode = m.get("values")
        let position = (m.pos, m.posZ, m.posTarget, m.moveDuration, m.liveYaw)
        let beforeFailures = failures
        var checks = 0
        func require(_ ok: Bool, _ message: String) { checks += 1; if !ok { fail("SPACEIMPORT \(message)") } }
        defer {
            _ = m.pick("builtin"); _ = m.set("values", mode); _ = m.set("space.field", preferred)
            (m.patch, m.patchName, m.fx, m.space, m.midiMaps, m.midiMPE) = saved
            m.posTarget = position.2; restoreGeometry(position.0, position.1, position.3, position.4)
            engine.errorText = nil; m.arm()
        }
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        _ = m.pick("builtin"); m.space = InstrSpace(); m.fx = InstrFx(); m.arm()
        _ = engine.renderOffline(1)
        require(m.set("space.field", "stereo") == nil && m.set("values", "record") == nil, "setup")
        let fixtures = ["space/binaural/binaural1.tl", "space/binaural/binaural3.tl", "space/panlaw.tl", "fx/chain.tl", "space/amb3-yaw.tl"]
        for name in fixtures {
            let url = engine.recordsRoot.appendingPathComponent("tests/golden/" + name)
            var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            guard let plain = InstrPatchIO.withCStrings([url.path], { fms_player_open($0, 1, &err, err.count) }),
                  let expected = InstrHeader.read(plain) else { fail("SPACEIMPORT fixture \(name): \(String(cString: err))"); continue }
            defer { fms_player_close(plain) }
            let bytes = try? Data(contentsOf: url)
            let unchanged = (engine.runningHash, engine.positionFrames, engine.voicePos, m.pos, m.posZ, m.liveYaw)
            require(m.openPatch(url, index: 0) == nil, "file import \(name)")
            require(m.space == expected.space && m.fx == expected.fx, "native metadata \(name)")
            require(m.get("values.defaultField") == "stereo", "import changed explicit preference")
            require(engine.runningHash == unchanged.0 && engine.positionFrames == unchanged.1 && engine.voicePos == unchanged.2 &&
                    m.pos == unchanged.3 && m.posZ == unchanged.4 && m.liveYaw == unchanged.5, "import applied source events")
            require(m.set("values", "defaults") == nil && m.space.fieldName == "stereo" &&
                    m.space.block == 64 && m.space.near == 100 && m.space.native.present == 0 && m.fx.extra == nil,
                    "defaults did not replace imported header and FX")
            require(m.set("values", "record") == nil && m.space == expected.space && m.fx == expected.fx, "cached record restoration")
            require((try? Data(contentsOf: url)) == bytes, "source bytes changed")
            if name.contains("amb3") {
                m.arm(); require(engine.errorText?.contains("ambisonic records play in Records") == true &&
                                engine.runningHash == unchanged.0, "amb metadata live refusal")
                engine.errorText = nil
            }
        }
        // Header projection covers every current native FX family; these
        // metadata-only opens render no audio and accept native values as-is.
        let effects = ["dcblock 100000", "chorus 25000 5000 3000", "delay 1000000 29491 20000",
                       "pingpong 1000000 29491 20000 2000", "reverb 1000 1000", "limiter 200 5000",
                       "compressor 600 200 100 5000 240", "softclip", "gain 240", "dither 4294967295",
                       "flanger 10000 10000 2000 29491 1", "crush", "decodable"]
        let patch = InstrPatchIO.defaultPatch()
        for row in effects {
            var space = InstrSpace().native
            if row.hasPrefix("pingpong ") { space.field = UInt32(FMS_SPACE_STEREO) }
            let n = fms_live_header_text(patch.text, "fx_import", 1, 48000, &space, row, nil, 0)
            var text = [CChar](repeating: 0, count: n), err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            require(n > 0 && fms_live_header_text(patch.text, "fx_import", 1, 48000, &space, row, &text, n) == n, "FX header \(row)")
            if let player = fms_player_open_live(text, &err, err.count) {
                let read = InstrHeader.read(player); fms_player_close(player)
                require(read?.fx.extra == row + "\n", "FX projection \(row)")
            } else { fail("SPACEIMPORT native FX fixture \(row): \(String(cString: err))") }
        }
        let future = "future_fx 3 7\n"
        require(InstrHeader.projectFX(future).extra == future, "unrepresented row preservation (projection only)")
        // A custom coefficient header exercises optional presence without
        // confusing the initial header yaw with later recorded yaw events.
        var custom = InstrSpace(); custom.field = Int(FMS_SPACE_BINAURAL); custom.block = 128; custom.near = 777
        custom.orderPresent = true; custom.order = 1; custom.pinnaPresent = true; custom.pinna = 3
        custom.coefficients = [0, 250, 500, 750, 1000]; custom.yawPresent = true; custom.yaw = -360000
        var native = custom.native
        let count = fms_live_header_text(patch.text, "custom_import", 1, 48000, &native, nil, nil, 0)
        var header = [CChar](repeating: 0, count: count), err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        require(fms_live_header_text(patch.text, "custom_import", 1, 48000, &native, nil, &header, count) == count, "custom header")
        if let player = fms_player_open_live(header, &err, err.count) {
            require(InstrHeader.read(player)?.space == custom, "custom coefficients/presence/native bounds"); fms_player_close(player)
        } else { fail("SPACEIMPORT custom header: \(String(cString: err))") }
        // Exercise the Records picker cache, then import it while another
        // player sounds. The golden includes pos/move/yaw, none imported.
        let golden = engine.recordsRoot.appendingPathComponent("tests/golden/space/binaural/binaural3.tl")
        engine.open(paths: [golden.path], title: "spatial import fixture")
        let serial = engine.loadID
        for _ in 0..<6000 where engine.installedSerial < serial { try? await Task.sleep(for: .milliseconds(5)) }
        m.snapshotRecord()
        require(engine.initialHeader?.space.fieldName == "binaural", "Records metadata cache")
        _ = m.pick("builtin"); m.space = InstrSpace(); m.fx = InstrFx(); m.arm(); _ = engine.renderOffline(1)
        m.posTarget = 0; _ = m.set("pos.duration", "0"); _ = m.setPos(CGPoint(x: 321, y: 654), z: 987)
        _ = engine.renderOffline(64)
        let current = (engine.runningHash, engine.positionFrames, engine.voicePos, m.pos, m.posZ, m.liveYaw)
        require(m.pick("record:0") == nil && m.space.fieldName == "binaural" && m.space.pinna == 1, "Records picker import")
        require(engine.runningHash == current.0 && engine.positionFrames == current.1 && engine.voicePos == current.2 &&
                m.pos == current.3 && m.posZ == current.4 && m.liveYaw == current.5, "Records picker imported choreography")
        require(m.set("values", "defaults") == nil && m.space.fieldName == "stereo", "defaults remembered explicit field")
        require(m.set("values", "record") == nil && m.space.fieldName == "binaural", "Records cache after switch")
        // Independently render the committed prefix; manually send exactly
        // its first ON/POS, then replay the whole newly recorded prefix.
        guard let plain = InstrPatchIO.withCStrings([golden.path], { fms_player_open($0, 1, &err, err.count) }) else {
            return fail("SPACEIMPORT prefix open")
        }
        defer { fms_player_close(plain) }
        var pcm = [Int16](repeating: 0, count: 8192)
        require(fms_player_render(plain, &pcm, 4096) == 4096, "plain prefix render")
        m.arm(); require(engine.errorText == nil, "imported live arm")
        _ = engine.renderOffline(1); m.alloc.reset(); m.press("record")
        m.alloc.noteOn(69, velocityQ15: 16000); _ = m.setPos(CGPoint(x: 1000, y: 1000), z: 0)
        let first = engine.renderOffline(64).samples
        let rest = engine.renderOffline(4032).samples
        require(first + rest == pcm && first.contains(where: { $0 != 0 }), "imported LIVE values PCM64/4096")
        m.press("stop")
        require(engine.runningHash == FNV64.of(pcm), "imported prefix hash")
        do { try m.takeText.write(to: out.appendingPathComponent("space-import.tl"), atomically: true, encoding: .utf8) }
        catch { fail("SPACEIMPORT save take: \(error)") }
        let imported = (m.space, m.fx)
        _ = m.pick("builtin"); require(m.space == imported.0 && m.fx == imported.1, "builtin changed manual values")
        _ = m.pick("recipe:epiano"); require(m.space == imported.0 && m.fx == imported.1, "recipe changed manual values")
        require(m.set("values", "unknown") != nil, "invalid switch accepted")
        say("SPACEIMPORT \(failures == beforeFailures ? "ok" : "FAIL") checks=\(checks) file=yes record=yes defaults=yes optional_presence=yes FX=13 events=not_imported prefix64_4096=exact source_bytes=unchanged")
    }

    private func spaceChecks() {
        let saved = (m.patch, m.fx, m.space, m.midiMaps, m.midiMPE)
        let position = (m.pos, m.posZ, m.posTarget, m.moveDuration, m.liveYaw)
        let failuresBefore = failures
        var n = 0
        func require(_ ok: Bool, _ why: String) { n += 1; if !ok { fail("spacecheck \(why)") } }
        defer {
            (m.patch, m.fx, m.space, m.midiMaps, m.midiMPE) = saved
            m.posTarget = position.2
            restoreGeometry(position.0, position.1, position.3, position.4)
            m.arm()
        }
        m.patch = InstrPatchIO.defaultPatch(); m.fx = InstrFx(); m.space = InstrSpace()
        m.midiMaps = Array(repeating: "off", count: 16); m.midiMPE = "off"
        for (key, bad) in [("space.field", "amb1"), ("space.field", "amb3"), ("space.field", "unknown"),
                           ("space.block", "0"), ("space.block", "63"), ("space.near", "0"), ("space.near", "16777217"),
                           ("space.order", "2"), ("space.panlaw", "0"), ("space.panlaw", "32769"),
                           ("space.panlaw.enabled", "2"), ("space.pinna", "unknown"), ("space.pinna.0", "-1"),
                           ("space.pinna.4", "1001"), ("space.yaw", "-360001"), ("space.yaw", "360001"),
                           ("space.yaw.enabled", "2"), ("pos.x", "16777217"), ("pos.y", "-16777217"),
                           ("pos.z", "16777217"), ("pos.voice", "8"), ("pos.duration", "-1"),
                           ("pos.duration", "18446744073709551616")] {
            let before = (m.space, m.pos, m.posZ, m.posTarget, m.moveDuration)
            require(m.set(key, bad) != nil, "accepted invalid \(key)=\(bad)")
            require(before.0 == m.space && before.1 == m.pos && before.2 == m.posZ &&
                    before.3 == m.posTarget && before.4 == m.moveDuration, "invalid edit changed model \(key)")
        }
        require(m.set("space.field", "binaural") == nil, "binaural field")
        m.arm(); require(engine.errorText == nil, "valid arm")
        m.alloc.noteOn(60, velocityQ15: 26000); _ = engine.renderOffline(64)
        m.fx.dcOn = true; m.arm()
        require(engine.errorText == nil && m.spaceConflict == nil && m.armedFx?.dcOn == true &&
                m.armedSpace == m.space, "binaural FX candidate accepted")
        m.alloc.noteOn(60, velocityQ15: 26000); _ = engine.renderOffline(64)
        let current = (m.armedSpace, engine.positionFrames, engine.runningHash, m.alloc.held)
        m.space.field = Int(FMS_SPACE_STEREO); m.space.panlawPresent = true; m.arm()
        require(engine.errorText != nil && m.fx.dcOn && m.space.panlawPresent &&
                engine.runningHash == current.2, "panlaw/FX refusal")
        m.fx = InstrFx(); m.space = InstrSpace(); m.space.field = Int(FMS_SPACE_AMB1); m.arm()
        require(engine.errorText?.contains("ambisonic records play in Records") == true &&
                engine.runningHash == current.2, "ambisonic metadata escaped native live refusal")
        m.space = InstrSpace(); _ = m.set("space.field", "binaural"); m.arm()
        require(engine.errorText == nil && m.armedSpace == m.space, "repair")
        _ = m.set("pos.duration", "0"); m.posTarget = 0; _ = m.setPos(.zero, z: 0)
        m.alloc.reset(); _ = engine.renderOffline(1)
        m.press("record"); m.alloc.noteOn(60, velocityQ15: 26000)
        require(m.setPos(CGPoint(x: 1000, y: 1000), z: 500) == nil, "selected position")
        _ = engine.renderOffline(64)
        require(engine.voicePos[0] == SIMD3(1000, 1000, 500) && engine.voicePos.dropFirst().allSatisfy { $0 == SIMD3(0, 1000, 0) }, "selected native XYZ")
        require(m.set("pos.duration", "2666667") == nil && m.set("pos.x", "2000") == nil, "two-block move setter")
        _ = engine.renderOffline(64)
        require(engine.voicePos[0] == SIMD3(1500, 1000, 500), "move midpoint")
        _ = engine.renderOffline(64)
        require(engine.voicePos[0] == SIMD3(2000, 1000, 500), "move endpoint")
        require(m.set("live.yaw", "30000") == nil, "live yaw setter")
        _ = engine.renderOffline(64)
        _ = m.set("pos.duration", "0"); m.posTarget = -1
        require(m.setPos(CGPoint(x: -16777216, y: 16777216), z: -16777216) == nil, "full native XYZ range")
        _ = engine.renderOffline(64)
        require(engine.voicePos.allSatisfy { $0 == SIMD3(-16777216, 16777216, -16777216) }, "all-voice native XYZ")
        let before = m.pos
        _ = m.set("pos.duration", String(UInt64.max))
        require(m.setPos(.zero) != nil && m.pos == before, "oversized move changed position")
        _ = m.set("pos.duration", "0")
        m.alloc.noteOff(60); _ = engine.renderOffline(64); m.press("stop")
        let rows = m.takeText.split(separator: "\n").map { $0.split(separator: " ") }
        require(rows.contains { $0.count == 8 && $0[3] == "move" && $0[2] == "0" && $0[4] == "2000" && $0[7] == "2666667" }, "move record fields")
        require(rows.contains { $0.count == 5 && $0[3] == "yaw" && $0[4] == "30000" }, "yaw record fields")
        do { try m.takeText.write(to: out.appendingPathComponent("space-controls.tl"), atomically: true, encoding: .utf8) }
        catch { fail("spacecheck save replay: \(error)") }
        // Independent last-valid timestamp, from floor(T*fs/1e9)<=UINT32_MAX.
        // No long render: test both sides of the end bound at several frames.
        require(!InstrModel.moveFits(frame: 1, fs: 48000, duration: 89478485312500), "ceil move boundary accepted")
        require(InstrModel.moveFits(frame: 1, fs: 48000, duration: 89478485312499), "ceil move boundary neighbour refused")
        for rate: UInt32 in [44100, 48000] {
            let last = ((UInt64(UInt32.max) + 1) * 1_000_000_000 - 1) / UInt64(rate)
            for frame: UInt32 in [0, 1, 63, 64, 65, UInt32.max - 1, UInt32.max] {
                let numerator = UInt64(frame) * 1_000_000_000
                let first = numerator / UInt64(rate) + (numerator % UInt64(rate) == 0 ? 0 : 1)
                require(InstrModel.moveFits(frame: frame, fs: rate, duration: last - first), "last valid move boundary")
                require(!InstrModel.moveFits(frame: frame, fs: rate, duration: last - first + 1), "first invalid move boundary")
            }
        }
        require(!InstrModel.moveFits(frame: 0, fs: 0, duration: 1), "zero rate move")
        require(!InstrModel.moveFits(frame: 0, fs: 48000, duration: 0), "zero duration is pos, not move")
        let size = CGSize(width: 180, height: 180)
        for (input, want) in [(CGPoint.zero, CGPoint(x: -4000, y: 4000)),
                              (CGPoint(x: 90, y: 90), .zero),
                              (CGPoint(x: 180, y: 180), CGPoint(x: 4000, y: -4000)),
                              (CGPoint(x: -90, y: 270), CGPoint(x: -4000, y: -4000))] {
            require(InstrXYPad.point(at: input, size: size) == want, "map coordinate")
        }
        let png = render(InstrXYPad(pos: .zero, dots: [(CGPoint(x: 1000, y: 1000), true), (.zero, false)], onMove: { _ in })
            .frame(width: 180, height: 180).background(Color.black).environment(\.colorScheme, .dark))
        require(!png.isEmpty, "map image empty")
        try? png.write(to: out.appendingPathComponent("space-map.png"))
        say("SPACEUI \(failures == failuresBefore ? "ok" : "FAIL") checks=\(n) ranges=native refused_unchanged=yes selected_all=yes move=two_blocks map=180x180")
    }

    private func restoreGeometry(_ pos: CGPoint, _ z: Int, _ duration: UInt64, _ yaw: Int) {
        _ = m.set("pos.duration", "0")
        _ = m.setPos(pos, z: z)
        _ = m.set("pos.duration", String(duration))
        if m.armedSpace?.field == Int(FMS_SPACE_BINAURAL) { _ = m.setYaw(yaw) }
    }

    private static func isLive(_ name: String) -> Bool {
        name.hasPrefix("send.") || name.hasPrefix("live.") || name.hasPrefix("pos.")
    }

    private func probe(_ name: String, _ value: String?, with: (String, String)?, retrig: Bool, mpeMember: Int) -> Probe {
        var r = Probe()
        if let value, !Self.isLive(name) { r.setError = m.set(name, value) }
        m.alloc.reset()   // both runs on the same voices
        // Apply cleanup events to the previous player before the fresh arm.
        // They must not override the new header at the start of its take.
        _ = engine.renderOffline(1)
        m.press("play_live")
        m.press("record")
        let key = m.octaveBase   // the computer keys' A
        let midiChannel = name == "midi.mpe" ? mpeMember :
            (name.hasPrefix("midi.ctl.") ? Int(name.dropFirst(9)).map { $0 - 1 } : nil)
        func on() {
            if let channel = midiChannel {
                if name == "midi.mpe" {
                    MidiInput.shared.feedBytes([UInt8(0xE0 + channel), 0, 64])
                    MidiInput.shared.feedBytes([UInt8(0xD0 + channel), 32])
                    MidiInput.shared.feedBytes([UInt8(0xB0 + channel), 74, 24])
                } else {
                    MidiInput.shared.feedBytes([UInt8(0xB0 + channel), 1, 64])
                    MidiInput.shared.feedBytes([UInt8(0xB0 + channel), 11, 0])
                }
                MidiInput.shared.feedBytes([UInt8(0x90 + channel), UInt8(key), 100])
            } else { m.alloc.noteOn(key, velocityQ15: UInt32(m.velocity)) }
        }
        func off() {
            if let channel = midiChannel { MidiInput.shared.feedBytes([UInt8(0x80 + channel), UInt8(key), 64]) }
            else { m.alloc.noteOff(key) }
        }
        var idle: [Int16] = []
        if retrig {   // the key once for 25 ms, then 25 ms for its release to end
            on()
            let a = engine.renderOffline(fs / 40)
            off()
            let b = engine.renderOffline(fs / 40)
            m.alloc.reset()   // the note again on the same voice, from silence
            idle = a.samples + b.samples
            r.display = a.display ?? b.display
        }
        on()
        let pre = engine.renderOffline(fs / 20)
        if let channel = midiChannel {
            if name == "midi.mpe" {
                // Expression only while held:  post-key-up path stays excluded.
                MidiInput.shared.feedBytes([UInt8(0xE0 + channel), 0, 96])
                MidiInput.shared.feedBytes([UInt8(0xD0 + channel), 100])
                MidiInput.shared.feedBytes([UInt8(0xB0 + channel), 74, 96])
            } else {
                MidiInput.shared.feedBytes([UInt8(0xB0 + channel), 1, 127])
                MidiInput.shared.feedBytes([UInt8(0xB0 + channel), 11, 32])
            }
        }
        if let value, Self.isLive(name) { r.setError = m.set(name, value) }
        if let (wn, wv) = with { m.set(wn, wv) }
        let body = engine.renderOffline(fs / 5)
        off()
        let tail = engine.renderOffline(fs / 10)
        m.press("stop")
        r.take = m.takeText
        r.hook = r.take.split(separator: "\n").filter { !$0.hasPrefix("hash ") }.joined(separator: "\n")
        r.all = idle + pre.samples + body.samples + tail.samples
        r.after = body.samples + tail.samples
        r.body = stride(from: 0, to: body.samples.count, by: body.channels).map { body.samples[$0] }
        r.display = r.display ?? pre.display ?? body.display ?? tail.display
        return r
    }

    private func check(_ a: [String], line: Int) async {
        guard a.count >= 2 else { return fail("line \(line): check NAME VALUE ...") }
        let name = a[0], value = a[1]
        var expectSound = "changed", dirs: [String] = [], with: (String, String)?, retrig = false
        var k = 2
        while k < a.count {
            switch a[k] {
            case "same", "inactive", "any": expectSound = a[k]
            case "retrig": retrig = true
            case "with" where k + 2 < a.count: with = (a[k + 1], a[k + 2]); k += 2
            default: dirs.append(a[k])
            }
            k += 1
        }
        let old = m.get(name), oldWith = with.map { m.get($0.0) } ?? ""
        if old == value { return fail("check \(name) \(value): the control is already at \(value)") }
        let saved = (m.patch, m.patchName, m.fx, m.space, m.velocity, m.octaveBase, m.posTarget, m.midiMaps, m.midiMPE)
        let mpeMember = value.hasPrefix("upper:") ? 14 : 1
        let positions = (m.pos, m.posZ, m.moveDuration, m.liveYaw)
        let pa = probe(name, nil, with: with, retrig: retrig, mpeMember: mpeMember)
        restoreGeometry(positions.0, positions.1, positions.2, positions.3)
        let pb = probe(name, value, with: with, retrig: retrig, mpeMember: mpeMember)
        var inactiveWhy: String?
        if name.hasPrefix("op"), let i = Int(name.dropFirst(2).prefix(1)), let key = name.split(separator: ".").last,
           let f = InstrCtl.op[String(key)] {
            inactiveWhy = instrInactive(m.patch, i)[f.label]
        } else if InstrCtl.stmt[name] != nil {
            inactiveWhy = instrStmtInactive(m.patch)[name]
        }
        m.set(name, old)   // the live controls keep their value in the model; the source its pick
        if let (wn, _) = with { m.set(wn, oldWith) }
        (m.patch, m.patchName, m.fx, m.space, m.velocity, m.octaveBase, m.posTarget, m.midiMaps, m.midiMPE) = saved
        restoreGeometry(positions.0, positions.1, positions.2, positions.3)
        if let e = pb.setError { return fail("check \(name) \(value): \(e)") }
        let tag = "\(line)-\(name)-\(value)".replacingOccurrences(of: ":", with: "_")
        for (s, p) in [("a", pa), ("b", pb)] {
            try? p.take.write(to: out.appendingPathComponent("checks/\(tag)-\(s).tl"), atomically: true, encoding: .utf8)
        }
        var bad: [String] = []
        // An effect control must change its own native header row. Unrelated
        // queued send events can otherwise make a missing setter look changed.
        let hook: Bool
        if name.hasPrefix("fx."), let effect = name.split(separator: ".").dropFirst().first {
            func effectRow(_ p: Probe) -> String {
                p.take.split(separator: "\n").filter { $0.hasPrefix(effect + " ") || $0 == effect }.joined(separator: "\n")
            }
            hook = effectRow(pa) != effectRow(pb)
        } else if name.hasPrefix("space.") {
            let key = String(name.dropFirst(6)).split(separator: ".").first.map(String.init) ?? ""
            let row = key == "field" ? "space" : key
            func headerRow(_ p: Probe) -> String {
                p.take.split(separator: "\n").filter { $0.hasPrefix(row + " ") }.joined(separator: "\n")
            }
            hook = headerRow(pa) != headerRow(pb)
        } else if name.hasPrefix("live.crush.") {
            func crusherEvents(_ p: Probe) -> String {
                p.take.split(separator: "\n").filter {
                    let t = $0.split(separator: " "); return t.count == 6 && t[3] == "crush"
                }.joined(separator: "\n")
            }
            hook = crusherEvents(pa) != crusherEvents(pb)
        } else if name.hasPrefix("pos.") || name == "live.yaw" {
            func spatialEvents(_ p: Probe) -> String {
                p.take.split(separator: "\n").filter {
                    let t = $0.split(separator: " ")
                    return t.count > 3 && (name == "live.yaw" ? t[3] == "yaw" : ["pos", "move"].contains(t[3]))
                }.joined(separator: "\n")
            }
            hook = spatialEvents(pa) != spatialEvents(pb)
        } else { hook = pa.hook != pb.hook }
        if !hook { bad.append("hook: the take read back from the bridge did not change") }
        let ha = FNV64.of(pa.after), hb = FNV64.of(pb.after)
        let sound = ha != hb
        switch expectSound {
        case "changed": if !sound { bad.append("sound: unchanged") }
        case "same": if sound { bad.append("sound: changed, expected the same") }
        case "inactive":
            if sound { bad.append("sound: changed, but the field is meant to be inactive") }
            if inactiveWhy == nil { bad.append("display: the editor does not dim \(name)") }
        default: break
        }
        var metrics: [String] = []
        for d in dirs {
            let kind = String(d.dropLast()), up = d.hasSuffix("+")
            let va = metric(kind, pa.body), vb = metric(kind, pb.body)
            metrics.append(String(format: "%@ %.4g->%.4g", kind, va, vb))
            if up ? !(vb > va) : !(vb < va) { bad.append("direction: \(d) not met") }
        }
        for (s, p) in [("a", pa), ("b", pb)] {
            let th = takeHash(p.take)
            if th == nil || th != FNV64.of(p.all) {
                bad.append(String(format: "record: take %@ hash %016llx is not the frames' %016llx", s, th ?? 0, FNV64.of(p.all)))
            }
            if let d = p.display { bad.append("display \(s): \(d)") }
        }
        let summary = "check \(name) \(old)->\(value) hook=\(hook ? "changed" : "SAME") sound=\(sound ? "changed" : "same")"
            + (metrics.isEmpty ? "" : " " + metrics.joined(separator: " "))
            + (inactiveWhy.map { " (dimmed: \($0))" } ?? "")
        if bad.isEmpty { say(summary + " ok") } else { fail(summary + ": " + bad.joined(separator: "; ")) }
    }

    private func takeHash(_ take: String) -> UInt64? {
        for l in take.split(separator: "\n") where l.hasPrefix("hash ") {
            return UInt64(l.dropFirst(5), radix: 16)
        }
        return nil
    }

    /// rms (Q15 units), hf (the share of the energy above 1.5 times the
    /// played note) or centroid (Hz), from a Hann-windowed 4096-point DFT.
    private func metric(_ kind: String, _ x: [Int16]) -> Double {
        if kind == "rms" {
            var s = 0.0
            for v in x { s += Double(v) * Double(v) }
            return (s / Double(max(1, x.count))).squareRoot()
        }
        let n = 4096
        guard x.count >= n, let dft = try? vDSP.DiscreteFourierTransform(count: n, direction: .forward,
                                                                          transformType: .complexComplex, ofType: Float.self)
        else { return 0 }
        let start = (x.count - n) / 2
        let win = vDSP.window(ofType: Float.self, usingSequence: .hanningDenormalized, count: n, isHalfWindow: false)
        let re = vDSP.multiply((0..<n).map { Float(x[start + $0]) }, win)
        var r = [Float](repeating: 0, count: n), im = [Float](repeating: 0, count: n)
        dft.transform(inputReal: re, inputImaginary: [Float](repeating: 0, count: n), outputReal: &r, outputImaginary: &im)
        let bin = Double(fs) / Double(n)
        let f0 = Double(noteMilliHz(m.octaveBase)) / 1000
        var tot = 0.0, hi = 0.0, cen = 0.0
        for k in 1..<(n / 2) {
            let p = Double(r[k] * r[k] + im[k] * im[k])
            tot += p
            cen += p * Double(k) * bin
            if Double(k) * bin > 1.5 * f0 { hi += p }
        }
        guard tot > 0 else { return 0 }
        return kind == "hf" ? hi / tot : cen / tot
    }

    // ---- snapshots ----

    private func snapshot() async -> Data {
        let pv = await InstrPreview.compute(m.patch)
        let view = InstrSnapshot(patch: m.patch, patchText: InstrPatchIO.patchText(m.patch, name: m.safeName),
                                 preview: pv, scope: engine.scope, channels: engine.scopeChannels,
                                 peaks: engine.peaks, status: snapStatus(), labels: labels())
        let r = ImageRenderer(content: view)
        r.scale = 1
        guard let cg = r.cgImage else { return Data() }
        let rep = NSBitmapImageRep(cgImage: cg)
        return rep.representation(using: .png, properties: [:]) ?? Data()
    }

    /// Any view offscreen, as snapshot draws (scale 1, PNG).
    private func render(_ view: some View) -> Data {
        let r = ImageRenderer(content: view)
        r.scale = 1
        guard let cg = r.cgImage else { return Data() }
        return NSBitmapImageRep(cgImage: cg).representation(using: .png, properties: [:]) ?? Data()
    }

    /// The first renders of a view can differ by a few antialiased pixels
    /// from later ones of the same state (a make uitest run saw one; see the
    /// warm-up snapshots): renders until two in a row agree, at most four.
    private func settled(_ view: some View) -> Data {
        var last = render(view)
        for _ in 0..<3 {
            let next = render(view)
            if next == last { break }
            last = next
        }
        return last
    }

    /// Writes NAME.png and compares it with an earlier picture, as snap does.
    private func picture(_ name: String, _ png: Data, against: (String, String)?) {
        try? png.write(to: out.appendingPathComponent(name + ".png"))
        snaps[name] = png
        var note = "\(png.count) bytes"
        if let (how, other) = against {
            guard let o = snaps[other] else { return fail("\(name): no picture \(other)") }
            let changed = o != png
            note += changed ? ", changed from \(other)" : ", same as \(other)"
            if (how == "changed") != changed { fail("\(name): expected \(how) against \(other)") }
        }
        say("picture \(name): \(note)")
    }

    /// 16-bit PCM WAV, two channels interleaved, little-endian.
    private func wav(_ x: [Int16], fs: UInt32) -> Data {
        var d = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        let bytes = UInt32(x.count * 2)
        d.append(contentsOf: Array("RIFF".utf8)); u32(36 + bytes); d.append(contentsOf: Array("WAVEfmt ".utf8))
        u32(16); u16(1); u16(2); u32(fs); u32(fs * 4); u16(4); u16(16)
        d.append(contentsOf: Array("data".utf8)); u32(bytes)
        x.withUnsafeBufferPointer { b in
            for v in b { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        }
        return d
    }

    private func snapStatus() -> String {
        String(format: "live %d playing %d recording %d frame %u hash %016llx fx [%@] stereo %d",
               engine.isLive ? 1 : 0, engine.isPlaying ? 1 : 0, engine.isRecording ? 1 : 0,
               engine.positionFrames, engine.runningHash,
               (m.fx.extra ?? "").replacingOccurrences(of: "\n", with: "; "), m.stereo ? 1 : 0)
    }
}

/// What snap draws: the patch editor, the preview, the patch text, the
/// scope, the meters and a status line, as the Instrument tab shows them.
struct InstrSnapshot: View {
    let patch: PatchLines
    let patchText: String
    let preview: InstrPreview
    let scope: [Int16]
    let channels: Int
    let peaks: [UInt32]
    let status: String
    var labels: [String] = []

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            PatchEditor(patch: .constant(patch)).frame(width: 640)
            VStack(alignment: .leading, spacing: 8) {
                InstrPreviewPanel(preview: preview)
                Text(patchText).font(.caption.monospaced())
                ScopeView(samples: scope, channels: channels).frame(height: 120)
                MetersView(peaks: peaks, hold: peaks, labels: []).frame(height: 60)
                Text(status).font(.caption.monospaced())
                ForEach(labels, id: \.self) { Text($0).font(.caption).foregroundStyle(.orange) }
            }
            .frame(width: 450)
        }
        .padding(8)
        .background(Color.black)
        .environment(\.colorScheme, .dark)
    }
}
