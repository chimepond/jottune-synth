// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// FMSpikeApp.swift: the app's entry point. Three tabs over one engine.

import SwiftUI
import AppKit
import UniformTypeIdentifiers

@MainActor enum RecordOpen {
    static let scheme = "fmspike"
    static func open(_ input: URL) -> String? {
        if ProcessInfo.processInfo.environment["FMS_UITEST_DROP"] == "record.openurl" { return nil }
        if ["http", "https"].contains(input.scheme?.lowercased() ?? "") {
            return "Open a local .tl file. Internet song links are handled by ChimePond in your browser."
        }
        let url: URL
        var autoplay = false
        if input.isFileURL {
            guard input.pathExtension.lowercased() == "tl" else { return "Open expects a .tl record." }
            return LocalRecords.shared.importFile(input) == nil ? LocalRecords.shared.status : nil
        }
        else {
            guard let c = URLComponents(url: input, resolvingAgainstBaseURL: false),
                  c.scheme == scheme, c.host == "open" || c.host == "play", c.path.isEmpty,
                  c.user == nil, c.password == nil, c.port == nil, c.fragment == nil,
                  c.queryItems?.count == 1, c.queryItems?.first?.name == "path",
                  let path = c.queryItems?.first?.value, path.hasPrefix("/"), !path.contains("\0") else {
                return "Use fmspike://open?path= or fmspike://play?path= followed by an encoded absolute record path."
            }
            url = URL(fileURLWithPath: path)
            autoplay = c.host == "play"
        }
        guard url.pathExtension.lowercased() == "tl" else { return "Open expects a .tl record." }
        if Engine.offline, ProcessInfo.processInfo.environment["FMS_UITEST_DROP"] == "record.playurl" { autoplay = false }
        Engine.shared.open(paths: [url.standardizedFileURL.path], title: url.lastPathComponent, autoplay: autoplay)
        return nil
    }
}


final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ n: Notification) {
        // Run as a regular app when started from the command line too.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
}

struct FMSpikeApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    // Not observed here: the views that draw engine state observe it, so the
    // 30 Hz updates do not rebuild the whole scene.
    private let engine = Engine.shared

    var body: some Scene {
        WindowGroup("JotTune Synth") {
            FMSpikeWindow(engine: engine)
        }
        .commands { FMSpikeCommands() }
    }
}

private struct FMSpikeTabKey: FocusedValueKey {
    typealias Value = Binding<String>
}

private extension FocusedValues {
    var fmSpikeTab: Binding<String>? {
        get { self[FMSpikeTabKey.self] }
        set { self[FMSpikeTabKey.self] = newValue }
    }
}

// SceneStorage must be installed in a scene's view tree. On App its binding
// stays at the default, so a tab click cannot change the selected content.
private struct FMSpikeWindow: View {
    let engine: Engine
    // FMS_TAB=instrument or features starts on that tab (smoke tests).
    @SceneStorage("fmspike.selected-tab") private var tab = "records"
    @SceneStorage("fmspike.last-record") private var lastRecord = ""

    var body: some View {
        TabView(selection: $tab) {
            RecordsView().tabItem { Label("Records", systemImage: "list.bullet.rectangle") }.tag("records")
            InstrumentView().tabItem { Label("Instrument", systemImage: "pianokeys") }.tag("instrument")
            FeaturesView().tabItem { Label("Features", systemImage: "doc.text") }.tag("features")
        }
        .environmentObject(engine)
        .focusedSceneValue(\.fmSpikeTab, $tab)
        .frame(minWidth: 1000, minHeight: 700)
        .preferredColorScheme(.dark)
        .onOpenURL { url in
            tab = "records"
            if let error = RecordOpen.open(url) { engine.errorText = error }
        }
        .onDrop(of: [.fileURL], isTargeted: nil) { providers in
            guard let first = providers.first else { return false }
            _ = first.loadObject(ofClass: URL.self) { url, _ in
                guard let url else { return }
                Task { @MainActor in
                    tab = "records"
                    if let error = RecordOpen.open(url) { engine.errorText = error }
                }
            }
            return true
        }
        .onReceive(engine.$loadedPaths) { paths in
            // This scene intentionally does not observe all Engine state.
            // Subscribe to the actual load publication, whose new paths
            // arrive after info/isLive, rather than waiting for a redraw.
            if engine.info != nil && !engine.isLive && paths.count == 1 { lastRecord = paths[0] }
        }
        .task {
            if let initial = ProcessInfo.processInfo.environment["FMS_TAB"] { tab = initial }
            if engine.info == nil && ProcessInfo.processInfo.environment["FMS_OPEN"] == nil && !lastRecord.isEmpty {
                if let error = RecordOpen.open(URL(fileURLWithPath: lastRecord)) { engine.errorText = error }
            }
        }
    }
}

private struct FMSpikeCommands: Commands {
    @ObservedObject private var instrument = InstrModel.shared
    @FocusedValue(\.fmSpikeTab) private var tab: Binding<String>?
    private let engine = Engine.shared

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("Import TL…") {
                LocalRecords.shared.openPanel()
                tab?.wrappedValue = "records"
            }.keyboardShortcut("o", modifiers: [.command, .shift])
            Button("Open Patch…") {
                let selection = tab
                InstrModel.shared.openPatchPanel()
                selection?.wrappedValue = "instrument"
            }
                .keyboardShortcut("o", modifiers: .command)
            Menu("Open Recent Patch") {
                ForEach(Array(instrument.recentPaths.enumerated()), id: \.element) { i, path in
                    Button("\(i): \(URL(fileURLWithPath: path).lastPathComponent)") {
                        let selection = tab
                        if let error = instrument.pick("recent:\(i)") { engine.errorText = error }
                        else { selection?.wrappedValue = "instrument" }
                    }.help(path)
                }
            }.disabled(instrument.recentPaths.isEmpty)
            if RecipeLibrary.toolsAvailable {
            Button("Export Recipe…") { instrument.exportRecipePanel() }
            Button("Import Recipes…") {
                let selection = tab
                instrument.importRecipesPanel()
                selection?.wrappedValue = "instrument"
            }
            }
            Button("Save Patch") { InstrModel.shared.savePatchPanel(asNew: false) }
                .keyboardShortcut("s", modifiers: .command)
            Button("Write Back into Record") {
                Task { if let error = await InstrModel.shared.writeBack() { engine.errorText = error } }
            }
            Button("Save Patch As…") { InstrModel.shared.savePatchPanel(asNew: true) }
                .keyboardShortcut("s", modifiers: [.command, .shift])
        }
    }
}
