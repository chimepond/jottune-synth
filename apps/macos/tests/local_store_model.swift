// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import Foundation

@main struct LocalStoreChecks {
    static func main() throws {
        func check(_ value: Bool) { precondition(value) }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let original = root.appendingPathComponent("original.tl"), bytes = Data("timeline fixture\n# exact source\n".utf8)
        try bytes.write(to: original)
        let store = LocalRecordStore(directory: root.appendingPathComponent("library"))
        check(try store.entries().isEmpty)
        let a = try store.insert(bytes, title: "First tune")
        let duplicate = try store.insert(bytes, title: "Another filename")
        precondition(a == duplicate)
        let b = try store.insert(Data("second source".utf8), title: "Second tune")
        check(try store.entries().count == 2)
        check(try store.read(a.id) == bytes)
        let reopened = LocalRecordStore(directory: store.directory)
        check(try reopened.entries() == store.entries())
        do { _ = try store.read("../original"); preconditionFailure("Traversal accepted") } catch {}
        do { try store.remove("../original"); preconditionFailure("Traversal removal accepted") } catch {}
        let legacy = Data("legacy import".utf8), id = LocalRecordStore.digest(legacy)
        try legacy.write(to: store.directory.appendingPathComponent(id + ".tl"))
        check(try store.entries().contains(where: { $0.id == id }))
        // Body identity is independent of transported metadata; the private
        // copy still has exact full-file integrity and remains immutable.
        let documents = LocalRecordStore(directory: root.appendingPathComponent("documents"))
        let body = Data("canonical native body\n".utf8)
        let firstDocument = Data("# jottune-tl 1 1\n# {\"title\":\"First\"}\n# end-jottune-tl\n".utf8) + body
        let secondDocument = Data("# jottune-tl 1 1\n# {\"title\":\"Renamed\"}\n# end-jottune-tl\n".utf8) + body
        let portable = try documents.insert(firstDocument, title: "Portable", canonicalBody: body)
        check(portable.id == LocalRecordStore.digest(body))
        check(portable.id != LocalRecordStore.digest(firstDocument))
        let renamed = try documents.insert(secondDocument, title: "Renamed", canonicalBody: body)
        check(renamed == portable)
        check(try documents.entries().count == 1)
        check(try documents.read(portable.id) == firstDocument)
        let portableURL = documents.directory.appendingPathComponent(portable.id + ".tl")
        try secondDocument.write(to: portableURL)
        do { _ = try documents.read(portable.id); preconditionFailure("Changed document checksum accepted") } catch {}
        try firstDocument.write(to: portableURL)
        let documentIndex = documents.directory.appendingPathComponent("titles.json")
        let documentMetadata = try Data(contentsOf: documentIndex)
        try Data("broken metadata".utf8).write(to: documentIndex)
        do { _ = try documents.read(portable.id); preconditionFailure("Body-addressed record accepted without checksum") } catch {}
        do { _ = try documents.insert(secondDocument, title: "Renamed", canonicalBody: body); preconditionFailure("Unreadable document index overwritten") } catch {}
        try documentMetadata.write(to: documentIndex)
        check(try documents.read(portable.id) == firstDocument)
        let legacyMetadata = Data(try JSONSerialization.data(withJSONObject: [a.id: "Legacy title", b.id: "Second tune"], options: [.sortedKeys]))
        try legacyMetadata.write(to: store.directory.appendingPathComponent("titles.json"))
        check(try store.read(a.id) == bytes)
        check(try store.entries().contains(where: { $0.id == a.id && $0.title == "Legacy title" }))
        let corrupted = store.directory.appendingPathComponent(b.id + ".tl")
        try Data("changed".utf8).write(to: corrupted)
        do { _ = try store.read(b.id); preconditionFailure("Tampered data accepted") } catch {}
        try store.remove(b.id)
        precondition(!FileManager.default.fileExists(atPath: corrupted.path))
        let metadata = store.directory.appendingPathComponent("titles.json")
        try Data("broken metadata".utf8).write(to: metadata)
        check(try store.entries().count == 2)
        check(try store.read(a.id) == bytes)
        let c = try store.insert(Data("new source".utf8), title: "New tune")
        check(try store.read(c.id) == Data("new source".utf8))
        try store.remove(c.id)
        check(try String(contentsOf: metadata, encoding: .utf8) == "broken metadata")
        let link = store.directory.appendingPathComponent(String(repeating: "f", count: 64) + ".tl")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: original)
        check(try !store.entries().contains(where: { $0.id == String(repeating: "f", count: 64) }))
        do { _ = try store.read(String(repeating: "f", count: 64)); preconditionFailure("Symlink accepted") } catch {}
        try store.remove(a.id)
        check(try Data(contentsOf: original) == bytes)
        print("LOCAL_STORE ok duplicate=retained migration=legacy corrupt=refused metadata=recoverable document=stable-body-id fullfile=checked delete=private")
    }
}
