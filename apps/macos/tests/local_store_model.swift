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
        print("LOCAL_STORE ok duplicate=retained migration=legacy corrupt=refused metadata=recoverable delete=private")
    }
}
