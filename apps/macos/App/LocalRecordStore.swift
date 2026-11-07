// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import Foundation
import CryptoKit

struct LocalRecordEntry: Identifiable, Hashable, Sendable {
    let id: String
    let title: String
}

struct LocalRecordFailure: Error, CustomStringConvertible {
    let description: String
    init(_ text: String) { description = text }
}

/// Original TL bytes are immutable private copies. Titles are optional metadata;
/// legacy hash-named imports remain discoverable without an index migration.
struct LocalRecordStore: Sendable {
    let directory: URL
    static func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
    private func file(_ id: String) throws -> URL {
        guard id.count == 64, id.allSatisfy({ "0123456789abcdef".contains($0) }) else {
            throw LocalRecordFailure("Invalid local song identifier.")
        }
        return directory.appendingPathComponent(id + ".tl")
    }
    private func titles() throws -> [String: String] {
        let url = directory.appendingPathComponent("titles.json")
        if !FileManager.default.fileExists(atPath: url.path) { return [:] }
        return try JSONDecoder().decode([String: String].self, from: Data(contentsOf: url))
    }
    private func saveTitles(_ value: [String: String]) throws {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(value).write(to: directory.appendingPathComponent("titles.json"), options: .atomic)
    }
    func entries() throws -> [LocalRecordEntry] {
        if !FileManager.default.fileExists(atPath: directory.path) { return [] }
        let names = (try? titles()) ?? [:]
        return try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            .compactMap { url -> LocalRecordEntry? in
                guard url.pathExtension == "tl", (try? file(url.deletingPathExtension().lastPathComponent)) != nil else { return nil }
                let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
                guard values.isRegularFile == true, values.isSymbolicLink != true else { return nil }
                let id = url.deletingPathExtension().lastPathComponent
                return LocalRecordEntry(id: id, title: names[id] ?? "Imported song " + id.prefix(8))
            }.sorted { $0.title == $1.title ? $0.id < $1.id : $0.title.localizedStandardCompare($1.title) == .orderedAscending }
    }
    @discardableResult func insert(_ bytes: Data, title: String) throws -> LocalRecordEntry {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let id = Self.digest(bytes), url = try file(id)
        let metadata = try? titles()
        var names = metadata ?? [:]
        if FileManager.default.fileExists(atPath: url.path) {
            guard try read(id) == bytes else { throw LocalRecordFailure("The retained song differs from its original bytes.") }
        } else { try bytes.write(to: url, options: .atomic) }
        if names[id] == nil { names[id] = title.isEmpty ? "Imported song" : title; if metadata != nil { try saveTitles(names) } }
        return LocalRecordEntry(id: id, title: names[id]!)
    }
    func read(_ id: String) throws -> Data {
        let url = try file(id)
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
        guard values.isRegularFile == true, values.isSymbolicLink != true else { throw LocalRecordFailure("The retained song is not a regular local file.") }
        let bytes = try Data(contentsOf: url)
        guard Self.digest(bytes) == id else { throw LocalRecordFailure("The retained song has changed. Remove this copy and import the original TL again.") }
        return bytes
    }
    func url(_ id: String) throws -> URL { _ = try read(id); return try file(id) }
    func remove(_ id: String) throws {
        let url = try file(id)
        let metadata = try? titles()
        var names = metadata ?? [:]
        // Only this validated hash-named private copy is removed, never a source.
        if FileManager.default.fileExists(atPath: url.path) { try FileManager.default.removeItem(at: url) }
        names.removeValue(forKey: id); if metadata != nil { try saveTitles(names) }
    }
}
