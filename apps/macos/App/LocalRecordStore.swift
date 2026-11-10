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

/// Original TL bytes are immutable private copies. New entries use canonical
/// music-body IDs and retain a separate full-file checksum in the local index.
/// Legacy full-file hash names remain discoverable without a migration.
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
    private struct StoredRecord: Codable {
        let title: String
        let fullfilechecksum: String?
    }
    private func metadata() throws -> [String: StoredRecord] {
        let url = directory.appendingPathComponent("titles.json")
        if !FileManager.default.fileExists(atPath: url.path) { return [:] }
        let bytes = try Data(contentsOf: url)
        if let records = try? JSONDecoder().decode([String: StoredRecord].self, from: bytes) { return records }
        return try JSONDecoder().decode([String: String].self, from: bytes).mapValues {
            StoredRecord(title: $0, fullfilechecksum: nil)
        }
    }
    private func saveMetadata(_ value: [String: StoredRecord]) throws {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(value).write(to: directory.appendingPathComponent("titles.json"), options: .atomic)
    }
    func entries() throws -> [LocalRecordEntry] {
        if !FileManager.default.fileExists(atPath: directory.path) { return [] }
        let records = (try? metadata()) ?? [:]
        return try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            .compactMap { url -> LocalRecordEntry? in
                guard url.pathExtension == "tl", (try? file(url.deletingPathExtension().lastPathComponent)) != nil else { return nil }
                let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
                guard values.isRegularFile == true, values.isSymbolicLink != true else { return nil }
                let id = url.deletingPathExtension().lastPathComponent
                return LocalRecordEntry(id: id, title: records[id]?.title ?? "Imported song " + id.prefix(8))
            }.sorted { $0.title == $1.title ? $0.id < $1.id : $0.title.localizedStandardCompare($1.title) == .orderedAscending }
    }
    /// canonicalBody is supplied only after the production reader and checked
    /// renderer validate the complete imported document. nil retains legacy use.
    @discardableResult func insert(_ bytes: Data, title: String, canonicalBody: Data? = nil) throws -> LocalRecordEntry {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let originalID = Self.digest(bytes)
        // Reimporting an existing legacy source keeps its established local ID.
        let id = FileManager.default.fileExists(atPath: try file(originalID).path)
            ? originalID : Self.digest(canonicalBody ?? bytes)
        let url = try file(id)
        let previous = try? metadata()
        var records = previous ?? [:]
        guard previous != nil || id == originalID else {
            throw LocalRecordFailure("The local library index is unreadable. Restore it before importing a portable TL.")
        }
        let retained: Data
        if FileManager.default.fileExists(atPath: url.path) {
            retained = try read(id)
            if canonicalBody == nil && retained != bytes {
                throw LocalRecordFailure("The retained song differs from its original bytes.")
            }
        } else {
            try bytes.write(to: url, options: .atomic)
            retained = bytes
        }
        let name = records[id]?.title ?? (title.isEmpty ? "Imported song" : title)
        records[id] = StoredRecord(title: name, fullfilechecksum: Self.digest(retained))
        if previous != nil { try saveMetadata(records) }
        return LocalRecordEntry(id: id, title: name)
    }
    func read(_ id: String) throws -> Data {
        let url = try file(id)
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
        guard values.isRegularFile == true, values.isSymbolicLink != true else { throw LocalRecordFailure("The retained song is not a regular local file.") }
        let bytes = try Data(contentsOf: url)
        let checksum = Self.digest(bytes)
        guard checksum == id || (try? metadata()[id]?.fullfilechecksum) == checksum else {
            throw LocalRecordFailure("The retained song has changed or its checksum is unavailable. Remove this copy and import the original TL again.")
        }
        return bytes
    }
    func url(_ id: String) throws -> URL { _ = try read(id); return try file(id) }
    func remove(_ id: String) throws {
        let url = try file(id)
        let previous = try? metadata()
        var records = previous ?? [:]
        // Only this validated hash-named private copy is removed, never a source.
        if FileManager.default.fileExists(atPath: url.path) { try FileManager.default.removeItem(at: url) }
        records.removeValue(forKey: id); if previous != nil { try saveMetadata(records) }
    }
}
