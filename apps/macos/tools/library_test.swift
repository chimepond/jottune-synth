// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0

import Foundation

@main struct LibraryTest {
    static func fail(_ message: String) -> Never {
        FileHandle.standardError.write(Data(("LIBRARY_MODEL FAIL " + message + "\n").utf8))
        exit(1)
    }
    static func main() {
        do { try run() } catch { fail(error.localizedDescription) }
    }
    static func run() throws {
        guard (2...3).contains(CommandLine.arguments.count) else { fail("library_test WORK_DIR [REPOSITORY_ROOT]") }
        let fm = FileManager.default
        let work = URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent("library-model-" + UUID().uuidString)
        let root = work.appendingPathComponent("records")
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: work) }
        let original = Data("Catalog checks do not interpret native TL syntax.\n".utf8)
        for name in ["first.tl", "second.tl", "keys.tl", "mirror.tl"] { try original.write(to: root.appendingPathComponent(name)) }
        var checks = 0
        func require(_ pass: Bool, _ reason: String) {
            guard pass else { fail(reason) }
            checks += 1
        }
        func variant(_ kind: String, _ id: String, _ paths: [String]) -> [String: Any] {
            ["kind": kind, "id": id, "paths": paths]
        }
        let standard = variant("standard", "legacy-pool", ["second.tl", "first.tl"])
        let keys = variant("keys", "legacy-pool-keys", ["keys.tl"])
        let mirror = variant("mirror", "legacy-pool-keys-mirror", ["mirror.tl"])
        let base: [String: Any] = ["id": "stable/song-a", "title": "Sodium Afterimage", "collection": "Process and Pressure",
                                   "variants": [standard, keys, mirror]]
        var other = base; other["id"] = "stable/song-b"; other["title"] = "Root and Branches"
        other["collection"] = "Generative Studies"; other["revision"] = "Arrangement 2"
        other["variants"] = [variant("standard", "b", ["first.tl"]), variant("keys", "b-keys", ["keys.tl"])]
        func data(_ songs: [[String: Any]], version: Any = 1) throws -> Data {
            try JSONSerialization.data(withJSONObject: ["version": version, "songs": songs])
        }
        func refuse(_ name: String, _ songs: [[String: Any]], version: Any = 1) throws {
            do { _ = try RecordLibrary.decode(data(songs, version: version), root: root) }
            catch { checks += 1; return }
            fail("accepted \(name)")
        }
        let library = try RecordLibrary.decode(data([base, other]), root: root)
        let song = library.songs[0], next = library.songs[1]
        require(song.edition == "Published" && next.edition == "Arrangement 2", "edition labels")
        require(song.record(.standard, root: root)?.paths == ["second.tl", "first.tl"].map { root.appendingPathComponent($0).path }, "explicit pool order")
        require(song.record(.keys, root: root)?.id == "legacy-pool-keys", "legacy identity")
        require(song.record(.keys, root: root)?.title == "Sodium Afterimage — Keys stereo", "readable variant title")
        var discoveredKeys = keys
        discoveredKeys["discovery"] = ["record_sha256": String(repeating: "a", count: 64),
            "source": "music/source.py", "metadata": ["genre": ["value": "ambient", "source": "declared"]],
            "featured": ["order": 1, "note": "Editorial selection"]] as [String: Any]
        var discoveredSong = base; discoveredSong["variants"] = [standard, discoveredKeys, mirror]
        let enriched = try RecordLibrary.decode(data([discoveredSong]), root: root).songs[0]
        require(enriched == song && enriched.record(.keys, root: root) == song.record(.keys, root: root),
                "optional discovery leaves record identity, pool order and playback selection unchanged")
        var discoveredSelection = SongSelection(); discoveredSelection.select(enriched)
        require(discoveredSelection.kind == .keys && enriched.matches("sodium"), "discovery preserves Keys default and title search")
        for shape: Any in [NSNull(), "metadata", 1, [] as [String]] {
            var badKeys = keys; badKeys["discovery"] = shape
            var badSong = base; badSong["variants"] = [standard, badKeys]
            try refuse("non-object discovery", [badSong])
        }
        for query in ["  sOdIuM  ", "process", "stable/song-a"] {
            require(library.matching(query).map(\.id) == [song.id], "title/collection/id search \(query)")
        }
        require(library.matching(" ").count == 2 && library.matching("missing song").isEmpty, "search counts")
        var selection = SongSelection()
        require(!selection.selectVariant(.standard, in: song), "variant before selection refused")
        selection.select(song)
        require(selection.kind == .keys && selection.songID == song.id, "Keys default")
        require(selection.selectVariant(.standard, in: song) && selection.kind == .standard, "Standard choice")
        require(selection.selectVariant(.mirror, in: song) && selection.kind == .mirror, "Mirror choice")
        selection.select(next)
        require(selection.kind == .keys && !selection.selectVariant(.mirror, in: next), "new song resets Keys; absent mirror refused")
        require(!selection.selectVariant(.standard, in: song) && selection.songID == next.id, "stale song refused")
        var originalOnly = base; originalOnly["variants"] = [standard]
        let single = try RecordLibrary.decode(data([originalOnly]), root: root).songs[0]
        selection.select(single)
        require(selection.kind == .standard && single.record(selection.kind, root: root) != nil,
                "standard-only song loads a playable selection")
        require(!selection.selectVariant(.keys, in: single) && !selection.selectVariant(.mirror, in: single),
                "unavailable alternative sounds stay unavailable")
        try refuse("duplicate song ID", [base, base])
        var duplicateRecord = other; duplicateRecord["variants"] = [standard, keys]
        try refuse("global duplicate variant ID", [base, duplicateRecord])
        try refuse("unknown version", [base], version: 2)
        try refuse("boolean version", [base], version: true)
        try refuse("empty library", [])
        var unknown = base; unknown["unexpected"] = "ignored?"
        try refuse("unknown song member", [unknown])
        var unknownVariant = keys; unknownVariant["route"] = "stereo"
        unknown = base; unknown["variants"] = [standard, unknownVariant]
        try refuse("unknown variant member", [unknown])
        let goodJSON = String(data: try data([base]), encoding: .utf8)!
        for token in ["1.0", "1e0"] {
            let json = goodJSON.replacingOccurrences(of: "\"version\":1", with: "\"version\":" + token)
            do { _ = try RecordLibrary.decode(Data(json.utf8), root: root); fail("accepted non-integer version token \(token)") }
            catch { checks += 1 }
        }
        for json in ["{\"unexpected\":0," + goodJSON.dropFirst(),
                     "{\"version\":1," + goodJSON.dropFirst(),
                     "{\"ver\\u0073ion\":1," + goodJSON.dropFirst(),
                     goodJSON.replacingOccurrences(of: "\"title\":", with: "\"title\":\"duplicate\",\"title\":"),
                     goodJSON.replacingOccurrences(of: "\"kind\":", with: "\"kind\":\"keys\",\"kind\":")] {
            do { _ = try RecordLibrary.decode(Data(json.utf8), root: root); fail("ambiguous JSON accepted") }
            catch { checks += 1 }
        }
        for field in ["id", "title", "collection", "revision"] {
            var bad = base; bad[field] = " \n"
            try refuse("empty \(field)", [bad])
            bad[field] = NSNull()
            try refuse("null \(field)", [bad])
        }
        for field in ["id", "title", "collection", "variants"] {
            var bad = base; bad.removeValue(forKey: field)
            try refuse("missing \(field)", [bad])
        }
        for variants in [[], [keys], [standard, keys, keys],
                         [standard, keys, variant("binaural", "x", ["first.tl"])],
                         [standard, variant("keys", " ", ["keys.tl"])],
                         [standard, variant("keys", "x", [])]] {
            var bad = base; bad["variants"] = variants
            try refuse("invalid variants", [bad])
        }
        for path in ["/first.tl", "../first.tl", "./first.tl", "a/../first.tl", "a//first.tl", "", "first.wav", "first.tl/", "first\0.tl", "missing.tl", "a\\first.tl"] {
            var bad = base; bad["variants"] = [standard, variant("keys", "x", [path])]
            try refuse("invalid path", [bad])
        }
        var bad = base; bad["variants"] = [standard, variant("keys", "x", ["keys.tl", "keys.tl"])]
        try refuse("duplicate pool path", [bad])
        for n in 0..<18 { try original.write(to: root.appendingPathComponent("pool-\(n).tl")) }
        bad["variants"] = [standard, variant("keys", "x", (0..<18).map { "pool-\($0).tl" })]
        try refuse("pool over native limit", [bad])
        bad["variants"] = [standard, variant("keys", "x", (0..<17).map { "pool-\($0).tl" })]
        require(try RecordLibrary.decode(data([bad]), root: root).songs[0].variant(.keys)?.paths.count == 17, "native pool limit accepted")
        try fm.createDirectory(at: root.appendingPathComponent("directory.tl"), withIntermediateDirectories: false)
        bad["variants"] = [standard, variant("keys", "x", ["directory.tl"])]
        try refuse("directory named TL", [bad])
        let outside = work.appendingPathComponent("outside.tl")
        try original.write(to: outside)
        try fm.createSymbolicLink(at: root.appendingPathComponent("escape.tl"), withDestinationURL: outside)
        bad["variants"] = [standard, variant("keys", "x", ["escape.tl"])]
        try refuse("symlink escape", [bad])
        do { _ = try RecordLibrary.load(from: root.appendingPathComponent("absent.json"), root: root); fail("missing catalog accepted") }
        catch { checks += 1 }
        do { _ = try RecordLibrary.decode(Data("{broken".utf8), root: root); fail("invalid JSON accepted") }
        catch { checks += 1 }
        require(try Data(contentsOf: root.appendingPathComponent("first.tl")) == original, "source unchanged")
        var catalog = "published_catalog=not_requested"
        if CommandLine.arguments.count == 3 {
            let root = URL(fileURLWithPath: CommandLine.arguments[2])
            let published = try RecordLibrary.load(from: root.appendingPathComponent("music/library.json"), root: root)
            require(!published.songs.isEmpty, "published catalog is populated")
            catalog = "songs=\(published.songs.count) variants=\(published.songs.reduce(0) { $0 + $1.variants.count })"
        }
        print("LIBRARY_MODEL checks=\(checks) \(catalog) ok")
    }
}
