// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import Foundation
import SwiftUI
import AppKit
import UniformTypeIdentifiers

/// The worker retains its cancellation token until every native call ends.
final class RecordWork: @unchecked Sendable {
    let token: OpaquePointer
    init() throws {
        guard let value = fms_cancel_create() else { throw RecordDocument.Refused("Cannot allocate a record operation.") }
        token = value
    }
    func cancel() { fms_cancel_request(token) }
    deinit { fms_cancel_close(token) }
}

enum RecordDocument {
    struct Refused: Error, CustomStringConvertible {
        let description: String
        init(_ text: String) { description = text }
    }

    /// Private players only. The production reader and checked renderer are
    /// also the authority for complete pooled-record exports.
    static func canonical(paths: [String] = [], data: Data? = nil, work: RecordWork) throws -> Data {
        var error = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        var player: OpaquePointer?
        if let data {
            let status = data.withUnsafeBytes { bytes in
                fms_player_import(bytes.bindMemory(to: CChar.self).baseAddress, data.count,
                                  work.token, &player, &error, error.count)
            }
            if status == FMS_CANCELLED { throw CancellationError() }
            guard status == FMS_OK else { throw Refused(String(cString: error)) }
        } else {
            let strings = paths.map { strdup($0) }
            defer { strings.forEach { free($0) } }
            player = strings.map { UnsafePointer($0) }.withUnsafeBufferPointer {
                fms_player_open($0.baseAddress, Int32($0.count), &error, error.count)
            }
        }
        guard let player else { throw Refused(String(cString: error)) }
        defer { fms_player_close(player) }
        var info = fms_info_t()
        fms_player_info(player, &info)
        var pcm = [Int16](repeating: 0, count: 256 * Int(max(1, info.channels)))
        repeat {
            let count = min(256, info.frames - fms_player_position(player))
            var written: UInt32 = 0
            let status = fms_player_render_checked(player, &pcm, count, work.token, &written, &error, error.count)
            if status == FMS_CANCELLED { throw CancellationError() }
            guard status == FMS_OK else { throw Refused(String(cString: error)) }
            guard written == count else { throw Refused("Record rendering stopped before its end.") }
        } while fms_player_position(player) < info.frames
        let needed = fms_player_record_text(player, nil, 0)
        guard needed > 0 else { throw Refused("The complete verified record could not be exported.") }
        var text = [CChar](repeating: 0, count: needed)
        guard fms_player_record_text(player, &text, text.count) == needed else {
            throw Refused("The complete verified record could not be exported.")
        }
        return text.withUnsafeBytes { Data($0.dropLast()) }
    }
}

@MainActor final class LocalRecords: ObservableObject {
    static let shared = LocalRecords()
    @Published private(set) var records: [LocalRecordEntry] = []
    @Published var selection = ""
    @Published private(set) var busy = false
    @Published private(set) var canCancel = false
    @Published private(set) var status = ""
    private var generation = 0
    private var task: Task<Void, Never>?
    private var work: RecordWork?
    private let store: LocalRecordStore
    private var engine: Engine { .shared }

    init(directory: URL? = nil) {
        var base = directory
        if base == nil, Engine.offline {
            let env = ProcessInfo.processInfo.environment
            let output = env["FMS_SCRIPT_OUT"].map { URL(fileURLWithPath: $0) }
                ?? URL(fileURLWithPath: env["FMS_SCRIPT"] ?? ".").deletingLastPathComponent().appendingPathComponent("uitest-out")
            base = output.appendingPathComponent("imported-records", isDirectory: true)
        }
        self.store = LocalRecordStore(directory: base ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("JotTuneSynth/ImportedRecords", isDirectory: true))
        reload()
    }

    func cancel(replacing: Bool = false) {
        guard canCancel || (replacing && busy) else { return }
        generation += 1
        work?.cancel(); task?.cancel()
        work = nil; task = nil; busy = false; canCancel = false
        status = "Cancelled."
    }

    private func begin(_ message: String, requireInstalled: Bool = true) throws -> (Int, Int, Int, RecordWork) {
        guard !busy else { throw RecordDocument.Refused("A record operation is already running.") }
        guard !requireInstalled || engine.installedSerial == engine.loadID else {
            throw RecordDocument.Refused("Wait for the current record to finish loading.")
        }
        let work = try RecordWork()
        generation += 1
        self.work = work; busy = true; canCancel = true; status = message
        return (generation, engine.loadID, engine.installedSerial, work)
    }

    private func current(_ request: Int, _ load: Int, _ installed: Int, allowPending: Bool = false) throws {
        try Task.checkCancellation()
        guard generation == request, engine.loadID == load,
              allowPending || engine.installedSerial == installed else { throw CancellationError() }
    }

    private func finish(_ request: Int) {
        if generation == request { work = nil; task = nil; busy = false; canCancel = false }
    }

    func reload() {
        do { records = try store.entries(); if !records.contains(where: { $0.id == selection }) { selection = records.first?.id ?? "" } }
        catch { status = "Library unavailable: \(error)" }
    }

    @discardableResult func importFile(_ url: URL) -> Task<Void, Never>? {
        do {
            guard url.isFileURL, url.pathExtension.lowercased() == "tl" else { throw RecordDocument.Refused("Choose a local .tl file.") }
            cancel(replacing: true)
            engine.pauseForImport()
            let (request, load, installed, work) = try begin("Checking local TL…", requireInstalled: false)
            let result = Task { @MainActor in
                defer { finish(request) }
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                do {
                    let bytes = try await Task.detached(priority: .userInitiated) {
                        let bytes = try Data(contentsOf: url)
                        _ = try RecordDocument.canonical(data: bytes, work: work)
                        return bytes
                    }.value
                    try current(request, load, installed, allowPending: true)
                    let record = try store.insert(bytes, title: url.lastPathComponent)
                    reload(); selection = record.id
                    canCancel = false; status = "Opening retained local TL…"
                    engine.openReadOnly(path: try store.url(record.id).path, title: record.title)
                    let serial = engine.loadID
                    while engine.installedSerial < serial {
                        try Task.checkCancellation()
                        guard generation == request, engine.loadID == serial else { throw CancellationError() }
                        try await Task.sleep(for: .milliseconds(5))
                    }
                    guard generation == request, engine.loadID == serial, engine.info != nil else {
                        throw RecordDocument.Refused(engine.errorText ?? "The local record could not be opened.")
                    }
                    status = "Imported locally. Press Play when ready."
                } catch is CancellationError {
                    if generation == request { status = "Source changed or import cancelled." }
                } catch { if generation == request { status = "Import refused: \(error)" } }
            }
            task = result; return result
        } catch { status = "Import refused: \(error)"; return nil }
    }

    func openSelected() {
        guard !busy, let entry = records.first(where: { $0.id == selection }) else { return }
        do { engine.openReadOnly(path: try store.url(entry.id).path, title: entry.title); status = "Opening local TL. Press Play when ready." }
        catch { status = "Open refused: \(error)" }
    }
    func removeSelected() {
        guard !busy, !selection.isEmpty else { return }
        do {
            let removed = store.directory.appendingPathComponent(selection + ".tl")
            if engine.loadedPaths.contains(removed.path) { engine.stop() }
            try store.remove(selection); reload(); status = "Removed the library copy. Your original file is unchanged."
        } catch { status = "Remove refused: \(error)" }
    }

    @discardableResult func export(to destination: URL) -> Task<Void, Never>? {
        do {
            guard engine.info != nil, !engine.isLive, !engine.loadedPaths.isEmpty else {
                throw RecordDocument.Refused("Open a complete record before exporting it.")
            }
            let paths = engine.loadedPaths
            guard !paths.contains(where: { URL(fileURLWithPath: $0).resolvingSymlinksInPath() == destination.resolvingSymlinksInPath() }) else {
                throw RecordDocument.Refused("Choose a new destination to preserve the original record.")
            }
            let (request, load, installed, work) = try begin("Verifying the complete record for export…")
            let result = Task { @MainActor in
                defer { finish(request) }
                do {
                    let store = self.store
                    let snapshot = try await Task.detached(priority: .userInitiated) { () -> Data in
                        if paths.count == 1 {
                            let source = URL(fileURLWithPath: paths[0])
                            let bytes = source.deletingLastPathComponent().standardizedFileURL == store.directory.standardizedFileURL
                                ? try store.read(source.deletingPathExtension().lastPathComponent) : try Data(contentsOf: source)
                            _ = try RecordDocument.canonical(data: bytes, work: work)
                            return bytes
                        }
                        return try RecordDocument.canonical(paths: paths, work: work)
                    }.value
                    try current(request, load, installed)
                    let bytes = snapshot
                    try bytes.write(to: destination, options: .atomic)
                    status = paths.count == 1 ? "Exported original TL bytes." : "Exported one complete verified TL."
                } catch is CancellationError {
                    if generation == request { status = "Source changed or export cancelled." }
                } catch {
                    if generation == request { status = "Export refused: \(error)" }
                }
            }
            task = result
            return result
        } catch { status = "Export refused: \(error)"; return nil }
    }

    func openPanel() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.init(filenameExtension: "tl") ?? .plainText]
        panel.allowsMultipleSelection = false; panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        importFile(url)
    }

    func exportPanel() {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.init(filenameExtension: "tl") ?? .plainText]
        panel.nameFieldStringValue = "song.tl"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        export(to: url)
    }

 }

struct LocalRecordsView: View {
    @EnvironmentObject var engine: Engine
    @ObservedObject private var records = LocalRecords.shared
    var body: some View {
        Card(title: "My local tunes") {
            HStack {
                Button("Import TL…") { records.openPanel() }.disabled(records.busy)
                Picker("Saved tune", selection: $records.selection) {
                    Text("Choose a tune").tag("")
                    ForEach(records.records) { Text($0.title).tag($0.id) }
                }.disabled(records.busy || records.records.isEmpty)
                Button("Open") { records.openSelected() }.disabled(records.busy || records.selection.isEmpty)
                Button("Remove copy") { records.removeSelected() }.disabled(records.busy || records.selection.isEmpty)
            }
            HStack {
                Button("Export TL…") { records.exportPanel() }.disabled(records.busy || engine.info == nil || engine.isLive)
                if records.busy { ProgressView().controlSize(.small) }
                if records.canCancel { Button("Cancel") { records.cancel() } }
            }
            Text("Imported tunes stay on this Mac. Opening prepares playback silently; your original files are preserved.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if !records.status.isEmpty { Text(records.status).font(.caption).textSelection(.enabled) }
        }
    }
}
