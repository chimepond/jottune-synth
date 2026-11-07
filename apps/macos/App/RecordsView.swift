// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// RecordsView.swift: browse the golden records, the music pieces and the
// recipes; play one and watch it.

import SwiftUI

/// Keeps the piano roll data for the loaded record (computed once per load).
@MainActor final class RollCache: ObservableObject {
    private var key: Int?
    private(set) var data = RollData(events: [], frames: 1, nvoices: 0)
    func get(_ e: Engine) -> RollData {
        // Requests advance loadID before their data arrives. Only an installed
        // result can replace the roll, including equal-size edits to one file.
        let k = e.installedSerial
        if k != key {
            key = k
            data = RollData(events: e.events, frames: e.info?.frames ?? 0, nvoices: Int(e.info?.nvoices ?? 0))
        }
        return data
    }
}

/// Draws its content from the per-block values (Engine.live), so only it
/// redraws at 30 Hz, not the cards around it .
struct Live<Content: View>: View {
    @ObservedObject private var live = Engine.shared.live
    private let content: (LiveState) -> Content
    init(@ViewBuilder _ content: @escaping (LiveState) -> Content) { self.content = content }
    var body: some View { content(live) }
}

@MainActor final class RecordBrowserModel: ObservableObject {
    enum Category: String, CaseIterable, Identifiable {
        case songs = "Songs", examples = "Examples", recipes = "Recipes"
        var id: String { rawValue }
    }
    @Published var category: Category = .songs
    @Published var search = ""
    @Published private(set) var songs: [LibrarySong] = []
    @Published private(set) var examples: [RecordRef] = []
    @Published private(set) var recipes: [RecordRef] = []
    @Published private(set) var selection = SongSelection()
    @Published private(set) var selectedExampleID: String?
    @Published private(set) var diagnostic: String?
    private let engine: Engine
    init(engine: Engine? = nil) { self.engine = engine ?? .shared }

    func reload(libraryURL: URL? = nil) {
        let records = engine.listRecords()
        do {
            let library = try RecordLibrary.load(from: libraryURL ?? engine.recordsRoot.appendingPathComponent("music/library.json"),
                                                 root: engine.recordsRoot)
            let songRecords = Set(library.songs.flatMap { $0.variants.map(\.id) })
            let separateRecords = Set(records.filter { $0.group != "music" }.map(\.id))
            guard songRecords.isDisjoint(with: separateRecords) else {
                throw RecordLibrary.Invalid(message: "A song variant ID conflicts with an example or recipe ID.")
            }
            songs = library.songs
            examples = records.filter { $0.group != "recipes" && !songRecords.contains($0.id) }
            diagnostic = nil
        } catch {
            songs = []
            examples = records.filter { $0.group != "recipes" }
            diagnostic = "Song library unavailable: \(error.localizedDescription)"
        }
        recipes = records.filter { $0.group == "recipes" }
        if selectedRecord == nil { selection = SongSelection() }
    }

    var filteredSongs: [LibrarySong] {
        if dropped("library.search") { return songs }
        return songs.filter { $0.matches(search) }
    }
    var selectedSong: LibrarySong? { songs.first { $0.id == selection.songID } }
    var selectedRecord: RecordRef? { selectedSong?.record(selection.kind, root: engine.recordsRoot) }
    var filteredRecords: [RecordRef] {
        let source = category == .recipes ? recipes : examples
        let q = search.trimmingCharacters(in: .whitespacesAndNewlines)
        return q.isEmpty ? source : source.filter { r in
            [r.title, r.id, r.group].contains { $0.localizedCaseInsensitiveContains(q) }
        }
    }
    var totalRecords: Int { category == .recipes ? recipes.count : examples.count }
    @discardableResult func chooseSong(_ id: String) -> Bool {
        guard let song = songs.first(where: { $0.id == id }) else { return false }
        selection.select(song)
        if dropped("library.default") { _ = selection.selectVariant(.standard, in: song) }
        guard let record = selectedRecord else { return false }
        engine.open(record)
        return true
    }
    @discardableResult func chooseVariant(_ kind: SongVariantKind) -> Bool {
        guard !dropped("library.variant"), let song = selectedSong,
              selection.selectVariant(kind, in: song), let record = selectedRecord else { return false }
        engine.open(record)
        return true
    }
    func chooseExample(_ id: String) {
        guard let record = (examples + recipes).first(where: { $0.id == id }) else { return }
        selectedExampleID = id; engine.open(record)
    }
    private func dropped(_ name: String) -> Bool {
        Engine.offline && ProcessInfo.processInfo.environment["FMS_UITEST_DROP"] == name
    }
}

struct RecordsView: View {
    @EnvironmentObject private var engine: Engine
    // The browser observes load-time metadata, never the 30 Hz audio state.
    @StateObject private var browser = RecordBrowserModel()
    private var songGroups: [(String, [LibrarySong])] {
        let grouped = Dictionary(grouping: browser.filteredSongs, by: \.collection)
        return grouped.keys.sorted().map { ($0, grouped[$0]!.sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }) }
    }
    private var recordGroups: [(String, [RecordRef])] {
        let grouped = Dictionary(grouping: browser.filteredRecords, by: \.group)
        return grouped.keys.sorted().map { ($0, grouped[$0]!) }
    }
    var body: some View {
        if !engine.hasSourceLibrary {
            RecordDetail()
        } else {
        NavigationSplitView {
            VStack(spacing: 6) {
                Picker("Library", selection: $browser.category) {
                    ForEach(RecordBrowserModel.Category.allCases) { Text($0.rawValue).tag($0) }
                }.pickerStyle(.segmented).padding(.horizontal, 6)
                if browser.category == .songs {
                    Text("\(browser.filteredSongs.count) of \(browser.songs.count) songs").font(.caption).foregroundStyle(.secondary)
                    if let error = browser.diagnostic {
                        Text(error).font(.caption).foregroundStyle(.red).textSelection(.enabled).padding(8)
                    }
                    List(selection: Binding(get: { browser.selection.songID }, set: { if let id = $0 { browser.chooseSong(id) } })) {
                        ForEach(songGroups, id: \.0) { group in
                            Section(group.0) {
                                ForEach(group.1) { song in
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(song.title)
                                        Text(song.edition).font(.caption2).foregroundStyle(.secondary)
                                    }.tag(song.id)
                                }
                            }
                        }
                    }
                } else {
                    Text("\(browser.filteredRecords.count) of \(browser.totalRecords) records").font(.caption).foregroundStyle(.secondary)
                    List(selection: Binding(get: { browser.selectedExampleID }, set: { if let id = $0 { browser.chooseExample(id) } })) {
                        ForEach(recordGroups, id: \.0) { group in
                            Section(group.0) {
                                ForEach(group.1) { r in
                                    Text((r.id as NSString).lastPathComponent + (r.paths.count > 1 ? " (\(r.paths.count))" : "")).tag(r.id)
                                }
                            }
                        }
                    }
                }
            }
            .searchable(text: $browser.search, placement: .sidebar, prompt: "Search titles or collections")
            .navigationSplitViewColumnWidth(min: 260, ideal: 310)
            .toolbar {
                Button { browser.reload() } label: { Image(systemName: "arrow.clockwise") }
                    .help("Reload the song library, examples and recipes")
            }
        } detail: {
            VStack(spacing: 0) {
                SongVariantPicker(browser: browser)
                RecordDetail()
            }
        }
        .task { browser.reload() }
        }
    }
}

private struct SongVariantPicker: View {
    @ObservedObject var browser: RecordBrowserModel
    @EnvironmentObject var engine: Engine
    var body: some View {
        if let song = browser.selectedSong, browser.selectedRecord?.paths == engine.loadedPaths {
            HStack {
                VStack(alignment: .leading) {
                    Text(song.title).font(.headline)
                    Text("\(song.collection) · \(song.edition)").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Picker("Version", selection: Binding(get: { browser.selection.kind }, set: { _ = browser.chooseVariant($0) })) {
                    ForEach(SongVariantKind.allCases.filter { song.variant($0) != nil }) { Text($0.title).tag($0) }
                }.pickerStyle(.segmented).frame(maxWidth: 360)
                    .help("Keys maps pitch to stereo position; it is separate from binaural decoding.")
            }.padding(12)
            Divider()
        }
    }
}

struct RecordDetail: View {
    @EnvironmentObject var engine: Engine
    var body: some View {
        ScrollView { RecordDetailContent() }
            .navigationTitle(engine.title)
    }
}

struct RecordDetailContent: View {
    @EnvironmentObject var engine: Engine
    @StateObject private var roll = RollCache()
    @State private var spaceMode = 0       // 0: top view, 1: 3D ()
    @State private var stereoOn = false    // the stereo picture, an extra option (section 5, item 2)

    var body: some View {
            VStack(alignment: .leading, spacing: 12) {
                LocalRecordsView()
                if let err = engine.errorText {
                    Card(title: "Refused") {
                        if engine.info == nil { Text(engine.title).font(.headline) }
                        Text(err).font(.system(.callout, design: .monospaced)).foregroundStyle(.red)
                            .textSelection(.enabled)
                    }
                }
                if let info = engine.info {
                    HeaderCard(key: engine.loadID, title: engine.title, live: engine.isLive, info: info).equatable()
                    TransportCard(info: info).id(engine.installedSerial)
                    let data = roll.get(engine)
                    if !engine.events.isEmpty {
                        Card(title: "Piano roll") {
                            let streams = engine.streams
                            Live { l in PianoRoll(data: data, position: l.positionFrames, streams: streams) }
                                .frame(height: 240)
                        }
                    }
                    HStack(alignment: .top, spacing: 12) {
                        Card(title: "Scope") {
                            Live { l in ScopeView(samples: l.scope, channels: l.scopeChannels, labels: channelLabels(info)) }
                                .frame(height: 150)
                        }
                        Card(title: "Spectrum") {
                            Live { l in SpectrumView(samples: l.scope, channels: l.scopeChannels, fs: info.fs) }
                                .frame(height: 150)
                        }
                        Card(title: "Peaks") {
                            Live { l in MetersView(peaks: l.peaks, hold: l.peakHold, labels: channelLabels(info)) }
                                .frame(width: info.channels > 4 ? 200 : 90, height: 150)
                        }
                        .frame(width: info.channels > 4 ? 222 : 112)
                    }
                    if info.channels == 2 {
                        Toggle("Stereo picture: goniometer, and analyze.py's width and correlation per window", isOn: $stereoOn)
                            .font(.caption)
                        if stereoOn {
                            Card(title: "Stereo") {
                                HStack(alignment: .top, spacing: 12) {
                                    Live { l in Goniometer(samples: l.scope, channels: l.scopeChannels) }
                                        .frame(width: 200, height: 216)
                                    let strip = engine.stereo?.key == engine.loadID ? engine.stereo : nil
                                    Live { l in StereoStripView(strip: strip, position: l.positionFrames) }
                                        .frame(height: 216)
                                }
                            }
                            .onAppear { engine.computeStereo() }
                            .onChange(of: engine.loadID) { _, _ in engine.computeStereo() }
                        }
                    }
                    HStack(alignment: .top, spacing: 12) {
                        if info.space != 0 || info.route != 0 {
                            Card(title: "Space (x right, y forward, z up)") {
                                Picker("", selection: $spaceMode) {
                                    Text("Top view").tag(0)
                                    Text("3D, drag to turn").tag(1)
                                }
                                .pickerStyle(.segmented).labelsHidden()
                                if spaceMode == 0 {
                                    Live { l in
                                        SpatialMap(info: info, pos: l.voicePos, active: l.voiceActive, voiceStream: data.voiceStream,
                                                   yawMdeg: data.yaw(at: l.positionFrames, initial: info.yaw_mdeg))
                                    }
                                    .frame(height: 300)
                                } else {
                                    let paths = engine.spacePaths
                                    Live { l in
                                        SpacePanel3D(info: info, pos: l.voicePos, active: l.voiceActive, voiceStream: data.voiceStream,
                                                     yawMdeg: data.yaw(at: l.positionFrames, initial: info.yaw_mdeg), paths: paths)
                                    }
                                    .frame(height: 300)
                                }
                            }
                            .frame(maxWidth: 560)
                        }
                        Card(title: "Voices") {
                            let patchOf = engine.voicePatch
                            Live { l in VoiceGrid(active: l.voiceActive, patchOf: patchOf, voiceStream: data.voiceStream) }
                        }
                    }
                    Card(title: "Patches") {
                        LoadedPatches(key: engine.loadID, patches: engine.patches, names: engine.patchNames,
                                      dropped: engine.patchDropped)
                            .equatable()
                            .id(engine.title)
                    }
                } else if engine.errorText == nil {
                    Text("Pick a record in the sidebar.").foregroundStyle(.secondary).padding()
                }
            }
            .padding(14)
    }
}

/// The patch inspector, redrawn only when a new record loads (not at 30 Hz).
struct LoadedPatches: View, Equatable {
    let key: Int
    let patches: [PatchLines]
    let names: [String]
    let dropped: [String]
    static func == (a: Self, b: Self) -> Bool { a.key == b.key }
    var body: some View { PatchInspector(patches: patches, names: names, dropped: dropped) }
}

/// Load-time facts only, so it is redrawn per load, not per tick.
struct HeaderCard: View, Equatable {
    let key: Int
    let title: String
    let live: Bool
    let info: fms_info_t
    static func == (a: Self, b: Self) -> Bool { a.key == b.key && a.title == b.title && a.live == b.live }
    var body: some View {
        Card(title: "") {
            Text(title).font(.title2.bold())
            let cols = [GridItem(.adaptive(minimum: 130), alignment: .leading)]
            LazyVGrid(columns: cols, alignment: .leading, spacing: 6) {
                stat("fs", "\(info.fs) Hz")
                stat("channels", "\(info.channels) · \(layoutName(info))")
                stat("voices", "\(info.nvoices)")
                stat("patches", "\(info.npatches)")
                stat("streams", "\(info.nstreams)")
                stat("events", "\(info.nevents)")
                stat("length", live ? "live" : String(format: "%.2f s (%u frames)", Double(info.length_ns) / 1e9, info.frames))
                stat("draft", "\(info.draft)")
                stat("block", info.block == 0 ? "-" : "\(info.block)")
                stat("near", info.near_mm == 0 ? "-" : "\(info.near_mm) mm")
                stat("yaw", info.yaw_mdeg == 0 ? "0" : fmt1(Double(info.yaw_mdeg) / 1000) + "°")
                stat("fx", info.fx_on != 0 ? "on" : "off")
            }
            HStack(spacing: 6) {
                Text("engine").font(.caption).foregroundStyle(.secondary)
                Text(cString(info.engine)).font(.caption.monospaced()).textSelection(.enabled)
            }
            HStack(spacing: 6) {
                Text("expected hash").font(.caption).foregroundStyle(.secondary)
                Text(info.have_hash != 0 ? String(format: "%016llx", info.hash_expected) : "none")
                    .font(.caption.monospaced()).textSelection(.enabled)
            }
        }
    }
    func stat(_ k: String, _ v: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(k).font(.caption2).foregroundStyle(.secondary)
            Text(v).font(.callout.monospacedDigit())
        }
    }
}

struct TransportCard: View {
    @EnvironmentObject var engine: Engine
    let info: fms_info_t
    @State private var verifying = false
    @State private var verifyText = ""
    @State private var verifyOK: Bool?
    @State private var seekFrame = 0.0
    @State private var draggingSeek = false

    var body: some View {
        Card(title: "Transport") {
            HStack(spacing: 10) {
                Button { engine.isPlaying ? engine.stop() : engine.play() } label: {
                    Image(systemName: engine.isPlaying ? "pause.fill" : "play.fill").frame(width: 22)
                }.keyboardShortcut(.space, modifiers: [])
                    .disabled(info.channels > Engine.outChannels)
                    .help(info.channels > Engine.outChannels ? "Use a stereo or binaural record for device playback." : "Play or pause")
                Button { engine.rewind() } label: { Image(systemName: "backward.end.fill") }
                    .disabled(engine.isSeeking)
                Live { l in Text(timeLabel(l.positionFrames)).font(.system(.body, design: .monospaced)) }
                if engine.isRecording {
                    Label("REC", systemImage: "record.circle.fill").font(.caption.bold()).foregroundStyle(.red)
                }
                if !engine.isLive && info.frames > 0 {
                    Live { l in
                        Slider(value: Binding(get: {
                            draggingSeek ? seekFrame : Double(min(l.positionFrames, info.frames))
                        }, set: { seekFrame = $0 }), in: 0...Double(info.frames), step: 1) { editing in
                            draggingSeek = editing
                            if editing { seekFrame = Double(min(l.positionFrames, info.frames)) }
                            else if let error = engine.seek(UInt32(seekFrame.rounded())) { engine.errorText = error }
                        }
                        .accessibilityIdentifier("record.seek")
                        .accessibilityLabel("Record position in frames")
                        .disabled(engine.isSeeking)
                    }.frame(minWidth: 120)
                    if engine.isSeeking { Text("Seeking...").font(.caption).foregroundStyle(.secondary) }
                } else { Spacer() }
                let live = engine.isLive
                Live { l in matchBadge(live: live, position: l.positionFrames, hash: l.runningHash) }
            }
            HStack(spacing: 10) {
                Text("running hash").font(.caption).foregroundStyle(.secondary)
                Live { l in
                    Text(String(format: "%016llx", l.runningHash)).font(.caption.monospaced())
                    if l.clipped > 0 {
                        Text("clipped \(l.clipped)").font(.caption).foregroundStyle(.orange)
                    }
                }
                Spacer()
                Button(verifying ? "Verifying..." : "Verify chunks") {
                    verifying = true
                    verifyText = ""
                    engine.verifyChunked { _, ok, s in
                        verifying = false
                        verifyOK = ok
                        verifyText = s
                    }
                }
                .disabled(verifying || engine.isLive)
            }
            DeviceRateView(control: engine.deviceControl, playerFs: info.fs) { rate, restore in
                _ = engine.requestDeviceRate(rate, restore: restore)
            }
            ForEach(auditionLabels, id: \.self) { s in
                Text(s).font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            if !verifyText.isEmpty {
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: verifyOK == true ? "checkmark.seal.fill" : "xmark.seal.fill")
                        .foregroundStyle(verifyOK == true ? .green : .red)
                    Text(verifyText).font(.caption.monospaced()).textSelection(.enabled)
                }
            }
        }
        .onChange(of: engine.installedSerial) { _, _ in verifying = false; verifyText = ""; verifyOK = nil }
    }

    /// : what the audition does not play as the record has it.
    private var auditionLabels: [String] {
        [auditionLabel(info)].compactMap { $0 }
    }

    private func timeLabel(_ position: UInt32) -> String {
        let fs = Double(max(1, info.fs))
        let t = Double(position) / fs
        if engine.isLive || info.frames == 0 { return String(format: "%7.2f s", t) }
        return String(format: "%6.2f / %.2f s", t, Double(info.frames) / fs)
    }

    @ViewBuilder private func matchBadge(live: Bool, position: UInt32, hash: UInt64) -> some View {
        if !live && info.frames > 0 && position >= info.frames {
            if info.have_hash != 0 {
                let ok = hash == info.hash_expected
                Text(ok ? "match" : "mismatch")
                    .font(.caption.bold())
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Capsule().fill(ok ? Color.green.opacity(0.3) : Color.red.opacity(0.35)))
            } else {
                Text("end (no hash)").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
