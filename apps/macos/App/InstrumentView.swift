// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// InstrumentView.swift: play the synth live with a patch you can see and
// edit. A patch comes from a recipe, from the loaded record or the built-in
// default; an offline note previews each edit; "Play live" arms a live player
// (patch, voices, effects, stereo) through Engine.openLive, and a take of the
// live events can be saved and verified against its own hash line.

import Accelerate
import AppKit
import SwiftUI

// ---- effects: one toggle and the values of each header statement ----

struct InstrFx: Equatable, Sendable {
    var dcOn = false, dcFc: UInt32 = 5000
    var chorusOn = false, chBase: UInt32 = 15000, chDepth: UInt32 = 2500, chRate: UInt32 = 800
    var flangerOn = false, flBase: UInt32 = 2000, flDepth: UInt32 = 1000, flRate: UInt32 = 300
    var flFeedback: UInt32 = 20000, flInvert: UInt32 = 0
    var decodable = false
    var delayOn = false, dlTime: UInt32 = 300_000, dlFb: UInt32 = 9830, dlLp: UInt32 = 6000
    var pingpongOn = false, ppTime: UInt32 = 375_000, ppFb: UInt32 = 19661, ppLp: UInt32 = 3000, ppHp: UInt32 = 120
    var reverbOn = false, rvRoom: UInt32 = 500, rvDamp: UInt32 = 300
    var compressorOn = false, compThr: UInt32 = 120, compRatio: UInt32 = 40
    var compAttack: UInt32 = 5, compRelease: UInt32 = 120, compMakeup: UInt32 = 60
    var limiterOn = false, lmThr: UInt32 = 10, lmRel: UInt32 = 200
    var softclip = false
    var crushOn = false
    var gainOn = false, gainDb10 = -120
    var ditherOn = false, ditherSeed: UInt32 = .max
    var unrepresented: [String] = [] // accepted canonical rows without a typed control

    /// The statements for fms_live_header_text's extra, one per line.
    var extra: String? {
        var l: [String] = []
        if dcOn { l.append("dcblock \(dcFc)") }
        if chorusOn { l.append("chorus \(chBase) \(chDepth) \(chRate)") }
        if flangerOn { l.append("flanger \(flBase) \(flDepth) \(flRate) \(flFeedback) \(flInvert)") }
        if delayOn { l.append("delay \(dlTime) \(dlFb) \(dlLp)") }
        if pingpongOn { l.append("pingpong \(ppTime) \(ppFb) \(ppLp) \(ppHp)") }
        if reverbOn { l.append("reverb \(rvRoom) \(rvDamp)") }
        if compressorOn { l.append("compressor \(compThr) \(compRatio) \(compAttack) \(compRelease) \(compMakeup)") }
        if limiterOn { l.append("limiter \(lmThr) \(lmRel)") }
        if softclip { l.append("softclip") }
        if gainOn { l.append("gain \(gainDb10)") }
        if ditherOn { l.append("dither \(ditherSeed)") }
        if crushOn { l.append("crush") }
        l += unrepresented
        if decodable { l.append("decodable") }
        return l.isEmpty ? nil : l.joined(separator: "\n") + "\n"
    }

    func busOn(_ b: Int) -> Bool { [chorusOn, delayOn || pingpongOn, reverbOn, compressorOn, flangerOn][b] }
}

// ---- the offline preview of one note ----

struct InstrPreview: Sendable {
    var lo: [Int16] = []
    var hi: [Int16] = []
    var specDb: [Float] = []     // bins 0 ..< n/2 of a 4096-point DFT
    var peakDb: Double = -120
    var error: String?

    static let fs: UInt32 = 48000
    static let frames: UInt32 = 72000       // 1.5 s
    static let offFrame: UInt32 = 48000     // 1.0 s on
    static let columns = 600
    static let nfft = 4096

    nonisolated static func compute(_ patch: PatchLines) async -> InstrPreview {
        await Task.detached(priority: .userInitiated) { () -> InstrPreview in
            let lines = patch.text
            var buf = [Int16](repeating: 0, count: Int(frames))
            var out = InstrPreview()
            if let d = patch.diagnostic { out.error = d; return out }
            let rc = fms_render_note(lines, fs, 261_626, 26000, offFrame, &buf, frames)
            if rc != 0 { out.error = "fms_render_note refused the patch (\(rc))"; return out }
            let per = buf.count / columns
            out.lo.reserveCapacity(columns); out.hi.reserveCapacity(columns)
            var peak: Int32 = 0
            for c in 0..<columns {
                var mn = Int16.max, mx = Int16.min
                for s in buf[(c * per)..<((c + 1) * per)] { mn = min(mn, s); mx = max(mx, s) }
                out.lo.append(mn); out.hi.append(mx)
                peak = max(peak, abs(Int32(mn)), abs(Int32(mx)))
            }
            out.peakDb = peak == 0 ? -120 : 20 * log10(Double(peak) / 32768)
            // Spectrum: a Hann window from 50 ms in.
            let start = 2400
            var re = [Float](repeating: 0, count: nfft)
            var win = [Float](repeating: 0, count: nfft)
            vDSP_hann_window(&win, vDSP_Length(nfft), Int32(vDSP_HANN_DENORM))
            for i in 0..<nfft { re[i] = Float(buf[start + i]) / 32768 * win[i] }
            let im = [Float](repeating: 0, count: nfft)
            if let dft = try? vDSP.DiscreteFourierTransform(previous: nil, count: nfft, direction: .forward,
                                                            transformType: .complexComplex, ofType: Float.self) {
                var r = [Float](repeating: 0, count: nfft), i = [Float](repeating: 0, count: nfft)
                dft.transform(inputReal: re, inputImaginary: im, outputReal: &r, outputImaginary: &i)
                let ref = Float(nfft) / 4      // a full-scale sine under a Hann window
                out.specDb = (0..<(nfft / 2)).map { k in
                    let m = (r[k] * r[k] + i[k] * i[k]).squareRoot() / ref
                    return 20 * log10(max(m, 1e-7))
                }
            }
            return out
        }.value
    }
}

struct InstrWaveView: View {
    let pv: InstrPreview
    var body: some View {
        Canvas { ctx, size in
            let n = pv.lo.count
            guard n > 0 else { return }
            let mid = size.height / 2
            var path = Path()
            for c in 0..<n {
                let x = CGFloat(c) / CGFloat(n) * size.width
                path.move(to: CGPoint(x: x, y: mid - CGFloat(pv.hi[c]) / 32768 * mid))
                path.addLine(to: CGPoint(x: x, y: mid - CGFloat(pv.lo[c]) / 32768 * mid + 0.5))
            }
            ctx.stroke(path, with: .color(.accentColor), lineWidth: max(1, size.width / CGFloat(n)))
            let off = CGFloat(InstrPreview.offFrame) / CGFloat(InstrPreview.frames) * size.width
            ctx.stroke(Path { $0.move(to: CGPoint(x: off, y: 0)); $0.addLine(to: CGPoint(x: off, y: size.height)) },
                       with: .color(.secondary), style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
        }
        .background(Color.primary.opacity(0.04))
    }
}

struct InstrSpectrumView: View {
    let pv: InstrPreview
    var body: some View {
        Canvas { ctx, size in
            let n = pv.specDb.count
            guard n > 1 else { return }
            let binHz = Double(InstrPreview.fs) / Double(InstrPreview.nfft)
            let lo = log10(20.0), hi = log10(Double(InstrPreview.fs) / 2)
            var path = Path()
            var first = true
            for k in 1..<n {
                let x = (log10(Double(k) * binHz) - lo) / (hi - lo) * size.width
                if x < 0 { continue }
                let db = Double(max(pv.specDb[k], -100))
                let y = (-db / 100) * size.height
                if first { path.move(to: CGPoint(x: x, y: y)); first = false } else { path.addLine(to: CGPoint(x: x, y: y)) }
            }
            ctx.stroke(path, with: .color(.green), lineWidth: 1)
            for f in [100.0, 1000.0, 10000.0] {
                let x = (log10(f) - lo) / (hi - lo) * size.width
                ctx.stroke(Path { $0.move(to: CGPoint(x: x, y: 0)); $0.addLine(to: CGPoint(x: x, y: size.height)) },
                           with: .color(.secondary.opacity(0.4)), lineWidth: 0.5)
                ctx.draw(Text(f >= 1000 ? "\(Int(f / 1000))k" : "\(Int(f))").font(.caption2),
                         at: CGPoint(x: x + 10, y: size.height - 8))
            }
        }
        .background(Color.primary.opacity(0.04))
    }
}

/// A centred top-down map. Dragging spans +/-4000 mm; numeric XYZ keeps the full native range.
struct InstrXYPad: View {
    let pos: CGPoint                         // mm; onMove owns the model update
    let dots: [(CGPoint, Bool)]               // other voices, mm, sounding
    let onMove: (CGPoint) -> Void

    static func point(at location: CGPoint, size: CGSize) -> CGPoint {
        guard size.width > 0, size.height > 0 else { return .zero }
        let x = min(max(location.x / size.width * 8000 - 4000, -4000), 4000)
        let y = min(max((size.height - location.y) / size.height * 8000 - 4000, -4000), 4000)
        return CGPoint(x: x.rounded(), y: y.rounded())
    }

    var body: some View {
        GeometryReader { g in
            let w = g.size.width, h = g.size.height
            let toView = { (p: CGPoint) in CGPoint(x: min(max((p.x + 4000) / 8000 * w, 0), w),
                                                        y: min(max(h - (p.y + 4000) / 8000 * h, 0), h)) }
            ZStack {
                Rectangle().fill(Color.primary.opacity(0.05))
                Path { $0.move(to: CGPoint(x: w / 2, y: 0)); $0.addLine(to: CGPoint(x: w / 2, y: h)) }
                    .stroke(Color.secondary.opacity(0.4), lineWidth: 0.5)
                Path { $0.move(to: CGPoint(x: 0, y: h / 2)); $0.addLine(to: CGPoint(x: w, y: h / 2)) }
                    .stroke(Color.secondary.opacity(0.4), lineWidth: 0.5)
                Text("listener").font(.caption2).foregroundStyle(.secondary).position(x: w / 2, y: h / 2 + 10)
                Text("+y").font(.caption2).position(x: w / 2 + 10, y: 8)
                Text("+x").font(.caption2).position(x: w - 10, y: h / 2 - 10)
                ForEach(0..<dots.count, id: \.self) { i in
                    if dots[i].1 {
                        Circle().fill(Color.green).frame(width: 7, height: 7).position(toView(dots[i].0))
                        Text("\(i)").font(.caption2).position(x: toView(dots[i].0).x + 7, y: toView(dots[i].0).y - 7)
                    }
                }
                Circle().stroke(Color.accentColor, lineWidth: 2).frame(width: 14, height: 14).position(toView(pos))
            }
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0).onChanged { v in
                onMove(Self.point(at: v.location, size: g.size))
            })
        }
    }
}

// ---- the tab ----

/// The tab draws InstrModel.shared and changes it only through its setters
/// (set, press, setPos, pick), the ones FMS_SCRIPT calls by name.
struct InstrumentView: View {
    @EnvironmentObject var engine: Engine
    @ObservedObject private var m = InstrModel.shared
    @ObservedObject private var recipes = InstrModel.shared.recipes
    @State private var keys: ComputerKeys?

    @State private var preview = InstrPreview()
    @State private var patchText = ""

    var body: some View {
        VStack(spacing: 0) {
            if let notice = engine.patchNotice {
                Text(notice).font(.caption).foregroundStyle(.secondary).padding(.horizontal, 12)
            }
            topBar.padding(8)
            HStack {
                Picker("Record / file import", selection: textControl("values")) {
                    Text("Record values").tag("record")
                    Text("Defaults").tag("defaults")
                }.frame(width: 300)
                Text("Initial header and FX only; recorded pos, move and yaw are not imported.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
            }.padding(.horizontal, 8).padding(.bottom, 6)
            if !m.fileStatus.isEmpty {
                Text(m.fileStatus).font(.caption).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 12)
            }
            if let d = m.dropNote {
                Text(d).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 12).padding(.bottom, 4)
            }
            if let e = engine.errorText {
                HStack {
                    Text(e).foregroundStyle(.red).font(.callout).textSelection(.enabled)
                    Spacer()
                    Button("Dismiss") { engine.errorText = nil }
                }.padding(.horizontal, 8)
            }
            DisclosureGroup("Favorites (\(m.favorites.count))") {
                VStack(alignment: .leading, spacing: 5) {
                    Button("Add current file and Space") { engine.errorText = m.addFavorite() }
                        .accessibilityIdentifier("favorite.add")
                    Text("Load stages the current file at the saved patch index and name, restores Space, keeps current effects, and does not play. Unsaved edits need Save As and reopen.")
                        .font(.caption).foregroundStyle(.secondary)
                    if let error = m.favoriteStore.error {
                        Text(error).foregroundStyle(.orange).textSelection(.enabled)
                        Button("Remove unreadable favorites") { engine.errorText = m.removeUnreadableFavorites() }
                            .accessibilityIdentifier("favorite.removeUnreadable")
                    }
                    ForEach(Array(m.favorites.enumerated()), id: \.offset) { index, favorite in
                        HStack {
                            Button("Load \(favorite.name) · \(favorite.space.fieldName)") { engine.errorText = m.loadFavorite(index) }
                                .accessibilityIdentifier("favorite.load.\(index)")
                            Text("\(URL(fileURLWithPath: favorite.path).lastPathComponent) [\(favorite.index)]")
                                .font(.caption).help(favorite.path)
                            Button("Remove") { engine.errorText = m.removeFavorite(index) }
                                .accessibilityIdentifier("favorite.remove.\(index)")
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.padding(.horizontal, 8)
            HStack {
                Text("Patch edits").font(.caption.bold())
                Button("Undo (\(m.patchJournal.undo.count))") { m.undoPatch() }.disabled(m.patchJournal.undo.isEmpty)
                Button("Redo (\(m.patchJournal.redo.count))") { m.redoPatch() }.disabled(m.patchJournal.redo.isEmpty)
                Text("Staged only: Apply to hear. Space, FX and MIDI maps are outside patch undo.").font(.caption).foregroundStyle(.secondary)
                Spacer()
            }.padding(.horizontal, 8)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("A/B selected: " + (m.compareSelected == 0 ? "A" : "B")).font(.caption.bold())
                    Button("Store A") { m.storeCompare(0) }.accessibilityIdentifier("store_a")
                    Button("Store B") { m.storeCompare(1) }.accessibilityIdentifier("store_b")
                    Button("Swap and apply") { m.swapCompare() }.accessibilityIdentifier("swap_ab")
                }
                Text("Swap clears held notes and freezes the last displayed positions. Live yaw is kept only when both players are binaural; otherwise the header yaw applies. Moving paths and tails do not continue. Store does not select or apply; edits do not update stored sides.")
                    .font(.caption2).foregroundStyle(.secondary)
                DisclosureGroup("Stored patch differences") {
                    Text(m.compareDiff).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }.padding(.horizontal, 8)
            Divider()
            HStack(alignment: .top, spacing: 0) {
                ScrollView {
                    PatchEditor(learn: InstrLearnControls(inputs: m.learnInputs, pending: m.learnRequest, message: m.learnStatus,
                                                          select: m.selectLearnInput, begin: { engine.errorText = m.startLearn($0) },
                                                          cancel: m.cancelLearn), patch: $m.patch).padding(8)
                }
                .frame(minWidth: 400, maxWidth: .infinity)
                .clipped()
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        previewBox
                        spaceBox
                        effectsBox
                        liveBox
                        takeBox
                        patchTextBox
                    }.padding(8)
                }
                .frame(width: 450)
            }
            Divider()
            keyboardBar.padding(8)
        }
        .task(id: m.patchKey) {
            try? await Task.sleep(for: .milliseconds(150))
            if Task.isCancelled { return }
            patchText = InstrPatchIO.patchText(m.patch, name: m.safeName)
            let pv = await InstrPreview.compute(m.patch)
            if !Task.isCancelled { preview = pv }
        }
        .onChange(of: m.patchName) { _, _ in patchText = InstrPatchIO.patchText(m.patch, name: m.safeName) }
        .onAppear {
            m.press("rescan")
            m.snapshotRecord()
            if keys == nil { keys = ComputerKeys(alloc: m.alloc) }
            keys?.octaveBase = m.octaveBase
            keys?.velocityQ15 = UInt32(m.velocity)
            keys?.onOctave = { m.set("octave", String($0)) }
            keys?.start()
        }
        .onDisappear { keys?.stop() }
        .onChange(of: m.velocity) { _, v in keys?.velocityQ15 = UInt32(v) }
        .onChange(of: m.octaveBase) { _, b in keys?.octaveBase = b }
        .onChange(of: engine.isLive) { _, live in if !live { m.alloc.allOff() } }
        .onChange(of: engine.title) { _, _ in m.snapshotRecord() }
    }

    // ---- source ----

    private var topBar: some View {
        HStack(spacing: 12) {
            Picker("Patch", selection: Binding(get: { m.source }, set: { m.pick($0) })) {
                Text("built-in default (2-op 1:1)").tag(InstrSource.builtin)
                if !recipes.entries.isEmpty {
                    Section("Recipes (\(recipes.entries.count))") {
                        ForEach(recipes.entries) { Text($0.id + ($0.error == nil ? "" : " — import failed")).tag(InstrSource.recipe($0.id)) }
                    }
                }
                if !m.recNames.isEmpty {
                    Section("Record: \(m.recTitle)") {
                        ForEach(0..<m.recNames.count, id: \.self) { i in
                            Text(m.recNames[i]).tag(InstrSource.record(i))
                        }
                    }
                }
            }
            .frame(width: 340)
            Button("Rescan recipes") { m.press("rescan") }.disabled(recipes.importing)
            if recipes.importing { ProgressView().controlSize(.small) }
            if let error = recipes.loadError { Text(error).font(.caption).foregroundStyle(.red) }
            if recipes.entries.isEmpty {
                Text("no recipes in build/recipes").font(.caption).foregroundStyle(.secondary)
            }
            TextField("name", text: Binding(get: { m.patchName }, set: { m.set("name", $0) })).frame(width: 120)
            if let n = m.loadNote { Text(n).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
            Spacer()
        }
    }

    // ---- preview ----

    private var previewBox: some View {
        GroupBox("Preview: one note, C4 261.626 Hz, velocity 26000, 1.0 s on of 1.5 s, 48 kHz") {
            InstrPreviewPanel(preview: preview)
        }
    }

    // ---- effects ----

    private func textControl(_ name: String) -> Binding<String> {
        Binding(get: { m.get(name) }, set: { if let error = m.set(name, $0) { engine.errorText = error } })
    }

    private func spaceNumber(_ label: String, _ key: String, _ unit: String) -> some View {
        HStack {
            Text(label).frame(width: 90, alignment: .leading)
            TextField(label, value: Binding(get: { Int(m.get(key)) ?? 0 },
                                           set: { if let error = m.set(key, String($0)) { engine.errorText = error } }),
                      format: .number.grouping(.never))
                .frame(width: 108).multilineTextAlignment(.trailing)
            Text(unit).foregroundStyle(.secondary)
        }.font(.callout)
    }

    private var spaceBox: some View {
        GroupBox("Space") {
            VStack(alignment: .leading, spacing: 5) {
                Picker("Field", selection: textControl("space.field")) {
                    Text("off").tag("off"); Text("stereo").tag("stereo")
                    Text("binaural (headphones)").tag("binaural")
                    if m.space.field >= Int(FMS_SPACE_AMB1) {
                        Text("\(m.space.fieldName) (Records only)").tag(m.space.fieldName).disabled(true)
                    }
                }
                Picker("Record/file import", selection: textControl("values")) {
                    Text("record values").tag("record"); Text("defaults").tag("defaults")
                }
                Text("Imports initial header and effects only; never recorded position, move or yaw events.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Ambisonic records play in Records. Header changes need Apply; no output-route detection.")
                    .font(.caption).foregroundStyle(.secondary)
                Picker("Block", selection: textControl("space.block")) {
                    ForEach([32, 64, 128], id: \.self) { Text("\($0) frames").tag(String($0)) }
                }
                spaceNumber("near", "space.near", "mm (1...16777216)")
                Picker("Binaural order", selection: textControl("space.order")) {
                    Text("native default (3)").tag("0"); Text("1").tag("1"); Text("3").tag("3")
                }
                Toggle("Stereo panlaw row", isOn: m.flag("space.panlaw.enabled"))
                if m.space.panlawPresent { spaceNumber("width", "space.panlaw", "Q15 (1...32768)") }
                Picker("Pinna row", selection: textControl("space.pinna")) {
                    ForEach(["absent", "off", "pbnh", "rd", "custom"], id: \.self) { Text($0).tag($0) }
                }
                if m.space.pinnaPresent && m.space.pinna == 3 {
                    ForEach(0..<5, id: \.self) { spaceNumber("D\($0 + 1)", "space.pinna.\($0)", "thousandths (0...1000)") }
                }
                Toggle("Initial yaw row", isOn: m.flag("space.yaw.enabled"))
                if m.space.yawPresent { spaceNumber("initial yaw", "space.yaw", "mdeg (±360000)") }
                Text("Order, pinna and yaw require binaural live. Panlaw requires stereo. Incompatible values remain staged and native Apply refuses them.")
                    .font(.caption).foregroundStyle(.secondary)
                Divider()
                HStack(alignment: .top) {
                    Live { l in
                        InstrXYPad(pos: m.pos, dots: voiceDots(l)) { p in
                            if let error = m.setPos(p) { engine.errorText = error }
                        }
                    }.frame(width: 180, height: 180)
                    VStack(alignment: .leading) {
                        Picker("voice", selection: m.integer("pos.voice")) {
                            Text("all").tag(-1)
                            ForEach(0..<m.voices, id: \.self) { Text("\($0)").tag($0) }
                        }
                        Text("x right, y forward, z up\n±4000 mm view/drag\nOff-map marks at edge")
                            .font(.caption).foregroundStyle(.secondary)
                        Text(String(format: "x %.0f mm\ny %.0f mm\nz %d mm", m.pos.x, m.pos.y, m.posZ))
                            .font(.caption.monospacedDigit())
                    }
                }
                spaceNumber("x", "pos.x", "mm (±16777216)")
                spaceNumber("y", "pos.y", "mm (±16777216)")
                spaceNumber("z", "pos.z", "mm (±16777216)")
                HStack {
                    Text("move duration").frame(width: 90, alignment: .leading)
                    TextField("nanoseconds", value: Binding(get: { m.moveDuration },
                              set: { if let error = m.set("pos.duration", String($0)) { engine.errorText = error } }),
                              format: .number.grouping(.never)).multilineTextAlignment(.trailing)
                    Text("ns").foregroundStyle(.secondary)
                }.font(.callout)
                Text("0 sends pos; a positive duration sends move on the next XYZ edit. Native endpoint limits apply.")
                    .font(.caption).foregroundStyle(.secondary)
                spaceNumber("live yaw", "live.yaw", "mdeg (±360000)")
                    .disabled(!engine.isLive || m.armedSpace?.field != Int(FMS_SPACE_BINAURAL))
                Text("Yaw rotates the listener's binaural field; XYZ moves the selected voice(s).")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var effectsBox: some View {
        GroupBox("Effects (header statements for the live player)") {
            VStack(alignment: .leading, spacing: 3) {
                if let conflict = m.spaceConflict {
                    Text(conflict).font(.caption).foregroundStyle(.orange)
                }
                if !m.fx.unrepresented.isEmpty {
                    Text("Native FX rows retained without typed controls:\n" + m.fx.unrepresented.joined(separator: "\n"))
                        .font(.caption.monospaced()).textSelection(.enabled)
                }
                fxToggle("dcblock", [("fc", "fc", "mHz", 1...100_000)])
                fxToggle("chorus", [("base", "base", "us", 7000...25000), ("depth", "depth", "us", 1000...5000),
                                    ("rate", "rate", "mHz", 100...3000)])
                fxToggle("flanger", [("base", "base", "us", 100...10000), ("depth", "depth", "us", 100...10000),
                                     ("rate", "rate", "mHz", 0...2000), ("feedback", "feedback", "Q15", 0...29491),
                                     ("invert", "invert", "0/1", 0...1)])
                if m.fx.flangerOn {
                    Text("Depth must not exceed base. Rate zero freezes the tap; feedback and invert remain separate.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                fxToggle("delay", [("time", "time", "us", 50_000...1_000_000), ("feedback", "feedback", "Q15", 0...29491),
                                   ("lowpass", "lowpass", "Hz", 20...20000)])
                fxToggle("pingpong", [("time", "time", "us", 50_000...1_000_000), ("feedback", "feedback", "Q15", 0...29491),
                                      ("lowpass", "lowpass", "Hz", 20...20000), ("highpass", "highpass", "Hz", 0...2000)])
                if m.fx.pingpongOn {
                    Text("Requires ordinary stereo or a supported field route; delay and pingpong share one send bus. High-pass 0 is off.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                fxToggle("crush", [])
                fxToggle("reverb", [("room", "room", "permille", 0...1000), ("damp", "damp", "permille", 0...1000)])
                fxToggle("compressor", [("threshold attenuation", "threshold", "dB10", 0...600),
                                         ("ratio", "ratio", "tenths", 10...200), ("attack", "attack", "ms", 0...100),
                                         ("release", "release", "ms", 1...5000), ("makeup", "makeup", "dB10", 0...240)])
                if m.fx.compressorOn {
                    Text("Linked master detector; duck sends replace it without an audible return. One-block latency. Makeup can raise peaks; the limiter remains separate.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                fxToggle("limiter", [("threshold", "threshold", "dB10", 0...200), ("release", "release", "ms", 1...5000)])
                fxToggle("softclip", [])
                Toggle("gain", isOn: m.flag("fx.gain"))
                if m.fx.gainOn {
                    InstrIntParam(label: "gain", value: m.integer("fx.gain.db10"),
                                  range: -600...240, slider: -600...240, unit: "dB10")
                }
                fxToggle("dither", [("seed", "seed", "", 1...Double(UInt32.max))])
                fxToggle("decodable", [])
                if m.fx.decodable {
                    Text("Reader profile: refuses nonlinear or time-varying paths. Live positions are fixed once per voice at frame zero; no moves. This is not a proof of invertibility or unclipped output. Apply validates staged changes.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    /// One effect: its toggle, then its values while it is on.
    @ViewBuilder
    private func fxToggle(_ name: String, _ values: [(String, String, String, ClosedRange<Double>)]) -> some View {
        Toggle(name, isOn: m.flag("fx." + name))
        if m.get("fx." + name) == "1" {
            ForEach(values, id: \.0) { v in
                let r = InstrFx.values[name + "." + v.1]?.1 ?? 0...UInt32.max
                InstrParam(label: v.0, value: m.u32("fx.\(name).\(v.1)"), range: r, slider: v.3, unit: v.2)
            }
        }
    }

    // ---- live ----

    /// Live MIDI input's sources and counts (MidiInput.label), in orange
    /// when a message was refused or an event dropped.
    private struct MidiLabel: View {
        @ObservedObject private var midi = MidiInput.shared
        var body: some View {
            Text(midi.shown).font(.caption).foregroundStyle(midi.warns ? .orange : .secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var liveBox: some View {
        GroupBox("Live (8 voices, 48 kHz)") {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    if engine.isLive {
                        Button("Apply to live") { m.press("apply") }.disabled(!m.liveDirty)
                        Button(engine.isPlaying ? "Pause" : "Resume") { m.press(engine.isPlaying ? "pause" : "resume") }
                        Text(m.liveDirty ? "edits not applied yet" : "live: \(engine.title)")
                            .font(.caption).foregroundStyle(m.liveDirty ? .orange : .secondary)
                    } else {
                        Button("Play live") { m.press("play_live") }
                        Text("arms a live player with this patch and these effects").font(.caption).foregroundStyle(.secondary)
                    }
                }
                DeviceRateView(control: engine.deviceControl, playerFs: engine.info?.fs ?? 0) { rate, restore in
                    _ = engine.requestDeviceRate(rate, restore: restore)
                }
                ForEach(m.liveLabels, id: \.self) { s in
                    Text(s).font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                }
                MidiLabel()
                HStack {
                    Text("MPE zone").frame(width: 80, alignment: .leading)
                    TextField("off, lower:N, or upper:N", text: Binding(
                        get: { m.get("midi.mpe") }, set: { m.set("midi.mpe", $0) }))
                        .textFieldStyle(.roundedBorder)
                }
                Text("One selected zone, N = 1…15 members, shares this patch and voice pool. Other channels are counted and ignored. Members cannot have controller maps; the master cannot map ctl1 or ctl4. Apply when arming; live playback stays at 48 kHz.")
                    .font(.caption).foregroundStyle(.secondary)
                DisclosureGroup("MIDI controller maps (apply when arming)") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(1...16, id: \.self) { channel in
                            HStack {
                                Text("Channel \(channel)").frame(width: 80, alignment: .leading)
                                TextField("off, default, or SOURCE:INPUT", text: Binding(
                                    get: { m.get("midi.ctl.\(channel)") },
                                    set: { m.set("midi.ctl.\(channel)", $0) }))
                                    .textFieldStyle(.roundedBorder)
                            }
                        }
                        Text("Off unless chosen. Use default or comma-separated sources such as cc1:0,pressure:1. Only inputs read by this patch emit ctl events; invalid maps keep the current live player.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                if engine.isLive {
                    Live { l in
                        Text(String(format: "frame %u  hash %016llx  clipped %u", l.positionFrames, l.runningHash, l.clipped))
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
                if engine.isLive, let afx = m.armedFx {
                    ForEach(InstrModel.busNames.indices, id: \.self) { b in
                        if afx.busOn(b) {
                            let name = InstrModel.busNames[b]
                            HStack {
                                Text("send \(name)").frame(width: 100, alignment: .leading)
                                Slider(value: Binding(get: { m.sends[b] }, set: { m.set("send." + name, String(Int($0.rounded()))) }),
                                       in: 0...32768)
                                Text(String(Int(m.sends[b]))).font(.caption.monospacedDigit()).frame(width: 48)
                            }
                        }
                    }
                }
                if engine.isLive, m.armedFx?.crushOn == true {
                    HStack {
                        Text("crusher bits").frame(width: 100, alignment: .leading)
                        Stepper(value: m.integer("live.crush.bits"), in: 1...24) {
                            Text(String(m.liveCrushBits)).monospacedDigit()
                        }
                        .accessibilityIdentifier("live.crush.bits")
                        .accessibilityLabel("Crusher bits")
                    }
                    HStack {
                        Text("rate divisor").frame(width: 100, alignment: .leading)
                        Stepper(value: m.integer("live.crush.rate_div"), in: 1...64) {
                            Text(String(m.liveCrushRateDiv)).monospacedDigit()
                        }
                        .accessibilityIdentifier("live.crush.rate_div")
                        .accessibilityLabel("Crusher rate divisor")
                    }
                    Text("Applies to all armed voices at the next FX block. Successful rearm and Record reset to bypass (24 bits, divisor 1).")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if engine.isLive, let ap = m.armedPatch, ap.filter_mode != 0 {
                    HStack {
                        Text("cutoff").frame(width: 100, alignment: .leading)
                        Slider(value: Binding(get: { log10(m.liveCutoff) }, set: { v in
                            m.set("live.cutoff", String(Int(pow(10, v).rounded())))
                        }), in: log10(20_000.0)...log10(20_000_000.0))
                        Text(String(format: "%.0f Hz", m.liveCutoff / 1000)).font(.caption.monospacedDigit()).frame(width: 64)
                    }
                    HStack {
                        Text("Q").frame(width: 100, alignment: .leading)
                        Slider(value: Binding(get: { m.liveQ }, set: { m.set("live.q", String(Int($0.rounded()))) }), in: 500...16000)
                        Text(String(format: "%.2f", m.liveQ / 1000)).font(.caption.monospacedDigit()).frame(width: 64)
                    }
                    Text("Filter events hold until the next note-on reloads the patch's filter.")
                        .font(.caption).foregroundStyle(.secondary)
                }

            }
        }
    }

    private func voiceDots(_ l: LiveState) -> [(CGPoint, Bool)] {
        (0..<min(m.voices, l.voicePos.count)).map { v in
            let p = l.voicePos[v]
            let on = v < l.voiceActive.count && l.voiceActive[v]
            return (CGPoint(x: CGFloat(p.x), y: CGFloat(p.y)), on)
        }
    }

    // ---- take ----

    private var takeBox: some View {
        GroupBox("Take") {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Button("Record") { m.press("record") }
                        .disabled(!engine.isLive || engine.isRecording)
                    Button("Stop") { m.press("stop") }
                        .disabled(!engine.isRecording)
                    if engine.isRecording { Circle().fill(.red).frame(width: 10, height: 10); Text("recording") }
                    if let note = engine.takeEndNote, !engine.isRecording {
                        Text(note).font(.caption).foregroundStyle(.orange)
                    }
                    Spacer()
                    if !m.takeText.isEmpty {
                        Text(String(m.takeText.split(separator: "\n").count) + " lines").font(.caption)
                        Button("Save...") { m.press("save") }
                            .disabled(m.takeSave == "Saving...")
                        Button("Verify take") { m.press("verify_take") }
                    }
                }
                if !m.takeSave.isEmpty {
                    Text(m.takeSave).font(.caption)
                        .foregroundStyle(m.takeSave.hasPrefix("Saved ") ? .green : (m.takeSave == "Saving..." ? .secondary : .red))
                        .textSelection(.enabled)
                }
                if !m.takeVerify.isEmpty {
                    Text(m.takeVerify).font(.caption.monospaced())
                        .foregroundStyle(m.takeVerify.hasPrefix("match") ? .green : (m.takeVerify.hasPrefix("rendering") ? .secondary : .red))
                        .textSelection(.enabled)
                }
                if !m.takeText.isEmpty {
                    ScrollView([.vertical, .horizontal]) {
                        Text(m.takeText).font(.caption.monospaced()).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(height: 160)
                    .background(Color.primary.opacity(0.04))
                }
            }
        }
    }

    // ---- patch text ----

    private var patchTextBox: some View {
        let text = patchText
        return GroupBox("Patch text (PatchLinesext)") {
            VStack(alignment: .leading, spacing: 4) {
                Text(text).font(.caption.monospaced()).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button(m.copied ? "Copied" : "Copy") { m.press("copy") }
            }
        }
    }

    // ---- keyboard ----

    private var keyboardBar: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Button("Oct -") { m.press("oct_down") }
                    Button("Oct +") { m.press("oct_up") }
                }
                Text("keys A W S E D F T G Y H U J K from \(noteName(m.octaveBase)); Z X shift")
                    .font(.caption).foregroundStyle(.secondary).frame(width: 170, alignment: .leading)
                HStack {
                    Text("vel")
                    Slider(value: Binding(get: { m.velocity }, set: { m.set("velocity", String(Int($0))) }), in: 0...32767)
                        .frame(width: 110)
                    Text(String(Int(m.velocity))).font(.caption.monospacedDigit())
                }
                if !engine.isLive { Text("A key arms the live player").font(.caption).foregroundStyle(.secondary) }
            }
            .frame(width: 210)
            PianoKeyboard(alloc: m.alloc, base: m.octaveBase, velocityQ15: UInt32(m.velocity))
                .frame(height: 100)
        }
    }
}

/// The preview's drawing: the waveform, the spectrum and the peak.
struct InstrPreviewPanel: View {
    let preview: InstrPreview
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let e = preview.error {
                Text(e).foregroundStyle(.red)
            } else {
                InstrWaveView(pv: preview).frame(height: 90)
                InstrSpectrumView(pv: preview).frame(height: 90)
                Text(String(format: "peak %.1f dBFS", preview.peakDb)).font(.caption.monospacedDigit())
            }
        }
    }
}
