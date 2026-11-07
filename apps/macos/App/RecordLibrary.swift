// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0

import Foundation

private struct LibraryKey: CodingKey {
    var stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

private func libraryKeys(_ decoder: Decoder, allowed: Set<String>) throws {
    let keys = try decoder.container(keyedBy: LibraryKey.self).allKeys.map(\.stringValue)
    if let unknown = keys.first(where: { !allowed.contains($0) }) {
        throw RecordLibrary.Invalid(message: "Unknown song library member: \(unknown)")
    }
}

/// JSONDecoder coalesces repeated object members. After it validates the JSON,
/// walk its member tokens to refuse ambiguous names (including escaped names).
/// No TL text or synthesis semantics are interpreted here.
private struct LibraryMembers {
    let bytes: [UInt8]
    var index = 0
    mutating func whitespace() { while index < bytes.count && [9, 10, 13, 32].contains(bytes[index]) { index += 1 } }
    mutating func take(_ byte: UInt8) -> Bool {
        whitespace()
        guard index < bytes.count, bytes[index] == byte else { return false }
        index += 1; return true
    }
    mutating func string() throws -> Data {
        whitespace(); let start = index
        guard take(34) else { throw RecordLibrary.Invalid(message: "Invalid JSON member.") }
        while index < bytes.count {
            let byte = bytes[index]; index += 1
            if byte == 34 { return Data(bytes[start..<index]) }
            if byte == 92 { index += 1 }
        }
        throw RecordLibrary.Invalid(message: "Unterminated JSON string.")
    }
    mutating func value(depth: Int = 0) throws {
        whitespace()
        if take(123) {
            var keys = Set<String>()
            if take(125) { return }
            repeat {
                let key = try JSONDecoder().decode(String.self, from: string())
                guard keys.insert(key).inserted else { throw RecordLibrary.Invalid(message: "Duplicate JSON member: \(key)") }
                guard take(58) else { throw RecordLibrary.Invalid(message: "Invalid JSON member separator.") }
                if depth == 0 && key == "version" {
                    whitespace()
                    guard index + 1 < bytes.count, bytes[index] == 49,
                          [9, 10, 13, 32, 44, 125].contains(bytes[index + 1]) else {
                        throw RecordLibrary.Invalid(message: "Song library version must be the JSON integer 1.")
                    }
                }
                try value(depth: depth + 1)
            } while take(44)
            guard take(125) else { throw RecordLibrary.Invalid(message: "Invalid JSON object.") }
        } else if take(91) {
            if take(93) { return }
            repeat { try value(depth: depth + 1) } while take(44)
            guard take(93) else { throw RecordLibrary.Invalid(message: "Invalid JSON array.") }
        } else if index < bytes.count && bytes[index] == 34 {
            _ = try string()
        } else {
            let start = index
            while index < bytes.count && ![9, 10, 13, 32, 44, 93, 125].contains(bytes[index]) { index += 1 }
            guard index > start else { throw RecordLibrary.Invalid(message: "Invalid JSON value.") }
        }
    }
}

struct RecordRef: Identifiable, Hashable {
    let id: String                 // Stable native record/pool identity.
    let group: String
    let paths: [String]            // Explicit pool order, never inferred from a title.
    var displayTitle: String? = nil
    var title: String { displayTitle ?? id }
}

enum SongVariantKind: String, Decodable, CaseIterable, Identifiable, Hashable {
    case standard, keys, mirror
    var id: String { rawValue }
    var title: String {
        switch self {
        case .standard: return "Standard"
        case .keys: return "Keys stereo"
        case .mirror: return "Keys mirror"
        }
    }
}

struct SongVariant: Decodable, Hashable {
    let kind: SongVariantKind
    let id: String
    let paths: [String]
    private enum CodingKeys: String, CodingKey { case kind, id, paths, discovery }
    init(from decoder: Decoder) throws {
        try libraryKeys(decoder, allowed: ["kind", "id", "paths", "discovery"])
        let values = try decoder.container(keyedBy: CodingKeys.self)
        kind = try values.decode(SongVariantKind.self, forKey: .kind)
        id = try values.decode(String.self, forKey: .id)
        paths = try values.decode([String].self, forKey: .paths)
        // Discovery is an optional sidecar object, validated by the source
        // exporter. Playback uses only the explicit TL identity and paths.
        if values.contains(.discovery) {
            _ = try values.superDecoder(forKey: .discovery).container(keyedBy: LibraryKey.self)
        }
    }
}

struct LibrarySong: Decodable, Identifiable, Hashable {
    let id: String
    let title: String
    let collection: String
    let revision: String?
    let variants: [SongVariant]
    var edition: String { revision ?? "Published" }

    private enum CodingKeys: String, CodingKey { case id, title, collection, revision, variants }
    init(from decoder: Decoder) throws {
        try libraryKeys(decoder, allowed: ["id", "title", "collection", "revision", "variants"])
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        title = try values.decode(String.self, forKey: .title)
        collection = try values.decode(String.self, forKey: .collection)
        // A supplied revision must be a string; null is not an edition label.
        revision = values.contains(.revision) ? try values.decode(String.self, forKey: .revision) : nil
        variants = try values.decode([SongVariant].self, forKey: .variants)
    }

    func variant(_ kind: SongVariantKind) -> SongVariant? { variants.first { $0.kind == kind } }
    func matches(_ query: String) -> Bool {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return q.isEmpty || [title, collection, id].contains { $0.localizedCaseInsensitiveContains(q) }
    }
    func record(_ kind: SongVariantKind, root: URL) -> RecordRef? {
        guard let v = variant(kind) else { return nil }
        return RecordRef(id: v.id, group: collection,
                         paths: v.paths.map { root.appendingPathComponent($0).path },
                         displayTitle: "\(title) — \(kind.title)")
    }
}

struct RecordLibrary: Decodable {
    let version: Int
    let songs: [LibrarySong]
    private enum CodingKeys: String, CodingKey { case version, songs }
    init(from decoder: Decoder) throws {
        try libraryKeys(decoder, allowed: ["version", "songs"])
        let values = try decoder.container(keyedBy: CodingKeys.self)
        version = try values.decode(Int.self, forKey: .version)
        songs = try values.decode([LibrarySong].self, forKey: .songs)
    }

    struct Invalid: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    static func load(from url: URL, root: URL) throws -> RecordLibrary {
        try decode(Data(contentsOf: url), root: root)
    }

    /// Catalog structure and file membership only. Native readers own TL syntax.
    static func decode(_ data: Data, root: URL) throws -> RecordLibrary {
        func fail(_ message: String) throws -> Never { throw Invalid(message: message) }
        func nonempty(_ text: String) -> Bool { !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        let library: RecordLibrary
        do { library = try JSONDecoder().decode(RecordLibrary.self, from: data) }
        catch { throw Invalid(message: "Invalid song library JSON: \(error.localizedDescription)") }
        var members = LibraryMembers(bytes: Array(data))
        try members.value()
        members.whitespace()
        guard members.index == members.bytes.count else { try fail("Song library must be one UTF-8 JSON object.") }
        guard library.version == 1 else { try fail("Unsupported song library version \(library.version); expected 1.") }
        guard !library.songs.isEmpty else { try fail("The song library contains no songs.") }
        var songIDs = Set<String>(), recordIDs = Set<String>()
        let boundary = root.standardizedFileURL.resolvingSymlinksInPath().path + "/"
        for song in library.songs {
            guard nonempty(song.id), songIDs.insert(song.id).inserted else { try fail("Empty or duplicate song ID: \(song.id)") }
            guard nonempty(song.title), nonempty(song.collection), song.revision.map(nonempty) ?? true else {
                try fail("Song \(song.id) needs a title, collection and nonempty revision when supplied.")
            }
            var kinds = Set<SongVariantKind>()
            for variant in song.variants {
                guard kinds.insert(variant.kind).inserted else { try fail("Duplicate \(variant.kind.rawValue) variant in \(song.id).") }
                guard nonempty(variant.id), recordIDs.insert(variant.id).inserted else { try fail("Empty or duplicate variant ID: \(variant.id)") }
                guard (1...17).contains(variant.paths.count), Set(variant.paths).count == variant.paths.count else {
                    try fail("Variant \(variant.id) needs 1 to 17 distinct ordered TL paths.")
                }
                for path in variant.paths {
                    let parts = path.split(separator: "/", omittingEmptySubsequences: false)
                    guard !path.contains("\0"), !path.contains("\\"), path.hasSuffix(".tl"), !parts.isEmpty,
                          parts.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else {
                        try fail("Variant \(variant.id) has an invalid relative TL path: \(path)")
                    }
                    let file = root.appendingPathComponent(path).standardizedFileURL
                    guard file.resolvingSymlinksInPath().path.hasPrefix(boundary) else {
                        try fail("Variant \(variant.id) resolves outside the record directory: \(path)")
                    }
                    guard (try? file.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true,
                          FileManager.default.isReadableFile(atPath: file.path) else {
                        try fail("Variant \(variant.id) is missing a readable TL file: \(path)")
                    }
                }
            }
            guard kinds.contains(.standard) else {
                try fail("Song \(song.id) requires a Standard variant.")
            }
        }
        return library
    }

    func matching(_ query: String) -> [LibrarySong] { songs.filter { $0.matches(query) } }
}

/// Prefer Keys when supplied; songs with only their original sound select Standard.
struct SongSelection {
    private(set) var songID: String?
    private(set) var kind: SongVariantKind = .keys
    mutating func select(_ song: LibrarySong) {
        songID = song.id; kind = song.variant(.keys) == nil ? .standard : .keys
    }
    mutating func selectVariant(_ next: SongVariantKind, in song: LibrarySong) -> Bool {
        guard songID == song.id, song.variant(next) != nil else { return false }
        kind = next; return true
    }
}
