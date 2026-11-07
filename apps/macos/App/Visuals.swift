// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// Visuals.swift: the drawings. Scope, spectrum, meters, piano roll, spatial
// map, patch inspector, envelope and route graph. Floating point here is for
// drawing only; nothing feeds back into the engine.

import SwiftUI
import Accelerate

// ---- colours ----

let streamPalette: [Color] = [
    Color(red: 1.00, green: 0.62, blue: 0.26), Color(red: 0.30, green: 0.80, blue: 0.95),
    Color(red: 0.95, green: 0.40, blue: 0.60), Color(red: 0.45, green: 0.85, blue: 0.45),
    Color(red: 0.98, green: 0.85, blue: 0.30), Color(red: 0.70, green: 0.55, blue: 1.00),
    Color(red: 0.40, green: 0.95, blue: 0.80), Color(red: 1.00, green: 0.45, blue: 0.35),
    Color(red: 0.45, green: 0.60, blue: 1.00), Color(red: 0.85, green: 0.70, blue: 0.55),
]

func streamColor(_ i: Int) -> Color { streamPalette[((i % streamPalette.count) + streamPalette.count) % streamPalette.count] }

let panelBackground = Color(nsColor: .controlBackgroundColor).opacity(0.6)

/// A rounded panel with a small title.
struct Card<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !title.isEmpty {
                Text(title.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
            }
            content
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(panelBackground))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.primary.opacity(0.08)))
    }
}

/// Channel names for a record's layout.
func channelLabels(_ info: fms_info_t?) -> [String] {
    guard let i = info else { return [] }
    let n = Int(i.channels)
    switch n {
    case 1: return ["M"]
    case 2: return ["L", "R"]
    case 4: return ["W", "Y", "Z", "X"]
    default: return (0..<n).map { "\($0)" }
    }
}

func layoutName(_ info: fms_info_t) -> String {
    switch info.route {
    case 1: return "amb1"
    case 2: return "amb3"
    case 3: return "binaural order \(info.order)"
    case 4: return "panlaw"
    default: return info.channels == 1 ? "mono" : (info.channels == 2 ? "stereo keys" : "\(info.channels) ch")
    }
}

// ---- audition labels  ----

/// The device's rate when it is not the record's: AVAudioEngine's mixer then
/// resamples, outside what the hash covers. nil with no device (offline).
func deviceRateLabel(recordFs: UInt32, deviceFs: UInt32) -> String? {
    guard deviceFs != 0, recordFs != 0, deviceFs != recordFs else { return nil }
    return "The output device runs at \(deviceFs) Hz, not this player's \(recordFs) Hz: what you hear is resampled; the hash covers the \(recordFs) Hz stream only."
}

/// One actual-rate row and the same explicit actions in both app tabs.
struct DeviceRateView: View {
    @ObservedObject var control: DeviceRateControl
    let playerFs: UInt32
    let request: (Double, Bool) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Device nominal: \(DeviceRateControl.rate(control.snapshot.nominal)) Hz; output: \(DeviceRateControl.rate(control.snapshot.output)) Hz.")
                .font(.caption).textSelection(.enabled)
            if control.snapshot.output > 0 && control.snapshot.output != Double(playerFs) {
                Text("Playback is resampled to the output rate; the hash covers the player's \(playerFs) Hz integer stream.")
                    .font(.caption).foregroundStyle(.orange)
            }
            HStack {
                if playerFs != 0 && control.snapshot.offers(Double(playerFs)) && control.snapshot.nominal != Double(playerFs) {
                    Button("Switch the device to \(playerFs) Hz") { request(Double(playerFs), false) }
                        .disabled(!control.canRequest(Double(playerFs)))
                }
                if let restore = control.restoreRate {
                    Button("Restore device to \(DeviceRateControl.rate(restore)) Hz") { request(restore, true) }
                        .disabled(!control.canRequest(restore))
                }
            }
            if let error = control.snapshot.error { Text(error).font(.caption).foregroundStyle(.orange) }
            if !control.message.isEmpty {
                Text(control.message).font(.caption).foregroundStyle(control.phase == .error ? .orange : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// Raw field channels remain available to rendering, drawing and export.
func auditionLabel(_ info: fms_info_t?) -> String? {
    guard let i = info, i.channels > Engine.outChannels else { return nil }
    return "This raw \(i.channels)-channel field cannot play on the two-channel device output. Every channel remains available for visualization and TL export; use a stereo or binaural record for listening."
}

/// Patch or effect edits made since the player was armed: they reach it only
/// through Apply to live, which swaps the player and cuts what is sounding.
func rearmLabel(dirty: Bool) -> String? {
    dirty ? "These edits need a re-arm: Apply to live swaps the player, which cuts the sounding notes and their release and effect tails." : nil
}

// ---- scope ----

struct ScopeView: View {
    let samples: [Int16]
    let channels: Int
    var labels: [String] = []
    var body: some View {
        Canvas { ctx, size in
            let ch = max(1, channels)
            let shown = min(ch, 4)
            let frames = samples.count / ch
            guard frames > 1 else { return }
            let laneH = size.height / CGFloat(shown)
            for c in 0..<shown {
                let mid = laneH * (CGFloat(c) + 0.5)
                var axis = Path()
                axis.move(to: CGPoint(x: 0, y: mid)); axis.addLine(to: CGPoint(x: size.width, y: mid))
                ctx.stroke(axis, with: .color(.primary.opacity(0.12)), lineWidth: 0.5)
                var p = Path()
                let step = max(1, frames / Int(max(1, size.width * 2)))
                var f = 0
                while f < frames {
                    let v = CGFloat(samples[f * ch + c]) / 32768
                    let pt = CGPoint(x: CGFloat(f) / CGFloat(frames - 1) * size.width, y: mid - v * laneH * 0.47)
                    if f == 0 { p.move(to: pt) } else { p.addLine(to: pt) }
                    f += step
                }
                ctx.stroke(p, with: .color(streamColor(c + 1)), lineWidth: 1.2)
                if c < labels.count {
                    ctx.draw(Text(labels[c]).font(.caption2).foregroundStyle(.secondary),
                             at: CGPoint(x: 8, y: mid - laneH * 0.5 + 8))
                }
            }
            if ch > shown {
                ctx.draw(Text("first \(shown) of \(ch) channels").font(.caption2).foregroundStyle(.secondary),
                         at: CGPoint(x: size.width - 70, y: size.height - 8))
            }
        }
        .background(Color.black.opacity(0.25))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

// ---- spectrum (vDSP DFT of channel 0, or the mono mix of a stereo pair) ----

enum Spectrum {
    static let n = 1024
    static let dft = try? vDSP.DiscreteFourierTransform(count: n, direction: .forward,
                                                        transformType: .complexComplex, ofType: Float.self)
    static let window = vDSP.window(ofType: Float.self, usingSequence: .hanningDenormalized, count: n, isHalfWindow: false)

    /// dBFS per bin 0..n/2.
    static func dB(_ samples: [Int16], channels: Int) -> [Float] {
        let ch = max(1, channels)
        let frames = samples.count / ch
        guard frames > 16, let dft else { return [] }
        var x = [Float](repeating: 0, count: n)
        let start = max(0, frames - n)
        for i in 0..<min(n, frames) {
            let f = start + i
            var v = Float(samples[f * ch])
            if ch == 2 { v = (v + Float(samples[f * ch + 1])) * 0.5 }
            x[i] = v / 32768
        }
        x = vDSP.multiply(x, window)
        let zeros = [Float](repeating: 0, count: n)
        var re = [Float](repeating: 0, count: n), im = [Float](repeating: 0, count: n)
        dft.transform(inputReal: x, inputImaginary: zeros, outputReal: &re, outputImaginary: &im)
        let half = n / 2
        var mag = [Float](repeating: 0, count: half + 1)
        let norm = Float(n) / 4
        for k in 0...half {
            let m = (re[k] * re[k] + im[k] * im[k]).squareRoot() / norm
            mag[k] = 20 * log10(max(m, 1e-6))
        }
        return mag
    }
}

struct SpectrumView: View {
    let samples: [Int16]
    let channels: Int
    let fs: UInt32
    var body: some View {
        let db = Spectrum.dB(samples, channels: channels)
        Canvas { ctx, size in
            let nyq = Double(max(fs, 8000)) / 2
            let lo = 20.0
            let span = log10(nyq / lo)
            func xOf(_ f: Double) -> CGFloat { CGFloat(log10(max(f, lo) / lo) / span) * size.width }
            func yOf(_ d: Float) -> CGFloat { CGFloat(-max(-100, min(0, d)) / 100) * size.height }
            for f in [100.0, 1000.0, 10000.0] where f < nyq {
                var g = Path()
                g.move(to: CGPoint(x: xOf(f), y: 0)); g.addLine(to: CGPoint(x: xOf(f), y: size.height))
                ctx.stroke(g, with: .color(.primary.opacity(0.12)), lineWidth: 0.5)
                ctx.draw(Text(f >= 1000 ? "\(Int(f / 1000))k" : "\(Int(f))").font(.caption2).foregroundStyle(.secondary),
                         at: CGPoint(x: xOf(f) + 10, y: size.height - 8))
            }
            for d in stride(from: -20, through: -80, by: -20) {
                let y = yOf(Float(d))
                var g = Path()
                g.move(to: CGPoint(x: 0, y: y)); g.addLine(to: CGPoint(x: size.width, y: y))
                ctx.stroke(g, with: .color(.primary.opacity(0.08)), lineWidth: 0.5)
                ctx.draw(Text("\(d)").font(.caption2).foregroundStyle(.tertiary), at: CGPoint(x: 14, y: y - 7))
            }
            guard db.count > 2 else { return }
            let binHz = nyq / Double(db.count - 1)
            var p = Path()
            p.move(to: CGPoint(x: 0, y: size.height))
            for k in 1..<db.count {
                p.addLine(to: CGPoint(x: xOf(Double(k) * binHz), y: yOf(db[k])))
            }
            p.addLine(to: CGPoint(x: size.width, y: size.height))
            p.closeSubpath()
            ctx.fill(p, with: .linearGradient(Gradient(colors: [streamColor(1).opacity(0.7), streamColor(5).opacity(0.15)]),
                                              startPoint: .zero, endPoint: CGPoint(x: 0, y: size.height)))
            ctx.stroke(p, with: .color(streamColor(1)), lineWidth: 1)
        }
        .background(Color.black.opacity(0.25))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

// ---- meters ----

struct MetersView: View {
    let peaks: [UInt32]
    let hold: [UInt32]
    let labels: [String]
    var body: some View {
        Canvas { ctx, size in
            let n = max(1, peaks.count)
            let gap: CGFloat = n > 8 ? 2 : 4
            let w = (size.width - gap * CGFloat(n - 1)) / CGFloat(n)
            let labelH: CGFloat = 12
            let h = size.height - labelH
            func frac(_ v: UInt32) -> CGFloat {   // -60..0 dBFS
                guard v > 0 else { return 0 }
                let d = 20 * log10(Double(v) / 32768)
                return CGFloat(max(0, min(1, (d + 60) / 60)))
            }
            for i in 0..<peaks.count {
                let x = CGFloat(i) * (w + gap)
                let r = CGRect(x: x, y: 0, width: w, height: h)
                ctx.fill(Path(roundedRect: r, cornerRadius: 2), with: .color(.primary.opacity(0.08)))
                let f = frac(peaks[i])
                let bar = CGRect(x: x, y: h * (1 - f), width: w, height: h * f)
                ctx.fill(Path(roundedRect: bar, cornerRadius: 2),
                         with: .linearGradient(Gradient(colors: [.red, .yellow, .green]),
                                               startPoint: CGPoint(x: 0, y: 0), endPoint: CGPoint(x: 0, y: h)))
                if i < hold.count {
                    let y = h * (1 - frac(hold[i]))
                    ctx.fill(Path(CGRect(x: x, y: y, width: w, height: 1.5)), with: .color(.white.opacity(0.8)))
                }
                if i < labels.count {
                    ctx.draw(Text(labels[i]).font(.system(size: 8)).foregroundStyle(.secondary),
                             at: CGPoint(x: x + w / 2, y: size.height - 5))
                }
            }
        }
    }
}

// ---- piano roll ----

struct RollNote { let start: UInt32; let end: UInt32; let logf: Float; let stream: Int; let voice: Int }
struct RollMark { let sample: UInt32; let type: Int }

/// Notes and markers from a record's events, computed once per load.
final class RollData: Equatable {
    let notes: [RollNote]
    let marks: [RollMark]
    let yaws: [(UInt32, Int32)]
    let voiceStream: [Int]
    let frames: UInt32
    let minLog: Float, maxLog: Float
    static func == (a: RollData, b: RollData) -> Bool { a === b }

    init(events: [fms_event_t], frames: UInt32, nvoices: Int) {
        var notes: [RollNote] = []
        var marks: [RollMark] = []
        var yaws: [(UInt32, Int32)] = []
        var vs = [Int](repeating: -1, count: max(nvoices, 1))      // stream of the voice's first note
        var vsPos = [Int](repeating: -1, count: max(nvoices, 1))   // else of its first pos or move
        var open: [Int: (UInt32, UInt32, Int)] = [:]   // voice -> (start, freq, stream)
        var last: UInt32 = 0
        func close(_ v: Int, at s: UInt32) {
            if let o = open[v], o.1 > 0 {
                notes.append(RollNote(start: o.0, end: max(s, o.0 + 1), logf: log2(Float(o.1) / 1000), stream: o.2, voice: v))
            }
            open[v] = nil
        }
        for e in events {
            let v = Int(e.voice), s = e.sample
            last = max(last, s)
            switch Int(e.type) {
            case FMS_EV_ON:
                close(v, at: s)
                open[v] = (s, e.freq_millihz, Int(e.stream))
                if v < vs.count, vs[v] < 0 { vs[v] = Int(e.stream) }
            case FMS_EV_OFF:
                close(v, at: s)
            case FMS_EV_PITCH:
                if let o = open[v] { close(v, at: s); open[v] = (s, e.freq_millihz, o.2) }
            case FMS_EV_YAW:
                yaws.append((s, e.x))
                marks.append(RollMark(sample: s, type: Int(e.type)))
            default:
                marks.append(RollMark(sample: s, type: Int(e.type)))
                if Int(e.type) == FMS_EV_POS || Int(e.type) == FMS_EV_MOVE, v < vsPos.count, vsPos[v] < 0 { vsPos[v] = Int(e.stream) }
            }
        }
        let end = frames > 0 ? frames : last + 1
        for v in Array(open.keys) { close(v, at: end) }
        self.notes = notes
        self.marks = marks
        self.yaws = yaws
        self.voiceStream = zip(vs, vsPos).map { $0 >= 0 ? $0 : max($1, 0) }
        self.frames = end
        let logs = notes.map(\.logf)
        let lo = (logs.min() ?? 6) - 0.5, hi = (logs.max() ?? 10) + 0.5
        minLog = lo
        maxLog = max(hi, lo + 1)
    }

    /// The listener's yaw at a frame (the last yaw event at or before it).
    func yaw(at frame: UInt32, initial: Int32) -> Int32 {
        var y = initial
        for (s, v) in yaws { if s <= frame { y = v } else { break } }
        return y
    }
}

func markColor(_ type: Int) -> Color {
    switch type {
    case FMS_EV_POS: return .cyan
    case FMS_EV_MOVE: return .teal
    case FMS_EV_SEND: return .purple
    case FMS_EV_FILTER: return .orange
    case FMS_EV_YAW: return .yellow
    default: return .gray
    }
}

/// The notes and markers; redrawn only when the data changes.
struct RollCanvas: View, Equatable {
    let data: RollData
    var body: some View {
        Canvas { ctx, size in
            let markH: CGFloat = 14
            let h = size.height - markH - 2
            let fr = CGFloat(max(1, data.frames))
            let span = CGFloat(data.maxLog - data.minLog)
            // octave lines
            var o = Int(ceil(data.minLog))
            while Float(o) <= data.maxLog {
                let y = h - CGFloat(Float(o) - data.minLog) / span * h
                var g = Path(); g.move(to: CGPoint(x: 0, y: y)); g.addLine(to: CGPoint(x: size.width, y: y))
                ctx.stroke(g, with: .color(.primary.opacity(0.1)), lineWidth: 0.5)
                let hz = Int(pow(2, Double(o)))
                ctx.draw(Text(hz >= 1000 ? "\(hz / 1000)k" : "\(hz)").font(.system(size: 8)).foregroundStyle(.tertiary),
                         at: CGPoint(x: 12, y: y - 6))
                o += 1
            }
            let nh = max(2, min(6, h / (span * 12)))
            for n in data.notes {
                let x0 = CGFloat(n.start) / fr * size.width
                let x1 = CGFloat(n.end) / fr * size.width
                let y = h - CGFloat(n.logf - data.minLog) / span * h - nh / 2
                ctx.fill(Path(roundedRect: CGRect(x: x0, y: y, width: max(1, x1 - x0), height: nh), cornerRadius: 1),
                         with: .color(streamColor(n.stream).opacity(0.9)))
            }
            let my = size.height - markH
            ctx.fill(Path(CGRect(x: 0, y: my, width: size.width, height: markH)), with: .color(.primary.opacity(0.05)))
            for m in data.marks {
                let x = CGFloat(m.sample) / fr * size.width
                ctx.fill(Path(CGRect(x: x - 0.5, y: my + 2, width: 1.5, height: markH - 4)), with: .color(markColor(m.type)))
            }
        }
        .drawingGroup()
    }
}

struct PianoRoll: View {
    let data: RollData
    let position: UInt32
    let streams: [String]
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            GeometryReader { geo in
                ZStack(alignment: .topLeading) {
                    RollCanvas(data: data).equatable()
                    Rectangle().fill(Color.white.opacity(0.85))
                        .frame(width: 1.5)
                        .offset(x: CGFloat(min(position, data.frames)) / CGFloat(max(1, data.frames)) * geo.size.width)
                }
            }
            .background(Color.black.opacity(0.25))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(Array(streams.enumerated()), id: \.offset) { i, s in
                        HStack(spacing: 4) {
                            RoundedRectangle(cornerRadius: 2).fill(streamColor(i)).frame(width: 10, height: 10)
                            Text(s).font(.caption)
                        }
                    }
                    Divider().frame(height: 10)
                    ForEach([(FMS_EV_POS, "pos"), (FMS_EV_MOVE, "move"), (FMS_EV_SEND, "send"),
                             (FMS_EV_FILTER, "filter"), (FMS_EV_YAW, "yaw"), (FMS_EV_OTHER, "other")], id: \.0) { t, s in
                        HStack(spacing: 3) {
                            Rectangle().fill(markColor(t)).frame(width: 2, height: 10)
                            Text(s).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                    Text("\(data.notes.count) notes, \(data.marks.count) markers").font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }
}

// ---- spatial map: top view, x right, y forward, mm ----

struct SpatialMap: View {
    let info: fms_info_t
    let pos: [SIMD3<Int32>]
    let active: [Bool]
    let voiceStream: [Int]
    let yawMdeg: Int32
    var body: some View {
        Canvas { ctx, size in
            let c = CGPoint(x: size.width / 2, y: size.height / 2)
            var extent = Double(max(info.near_mm, 500)) * 1.5
            for p in pos { extent = max(extent, Double(abs(p.x)) * 1.2, Double(abs(p.y)) * 1.2) }
            let scale = max(1, Double(min(size.width, size.height)) / 2 - 20) / extent
            func pt(_ x: Int32, _ y: Int32) -> CGPoint {
                CGPoint(x: c.x + CGFloat(Double(x) * scale), y: c.y - CGFloat(Double(y) * scale))
            }
            // metre rings
            let step = extent > 8000 ? 5000.0 : 1000.0
            var r = step
            while r < extent * 1.5 {
                let rr = CGFloat(r * scale)
                ctx.stroke(Path(ellipseIn: CGRect(x: c.x - rr, y: c.y - rr, width: rr * 2, height: rr * 2)),
                           with: .color(.primary.opacity(0.08)), lineWidth: 0.5)
                r += step
            }
            var axes = Path()
            axes.move(to: CGPoint(x: 0, y: c.y)); axes.addLine(to: CGPoint(x: size.width, y: c.y))
            axes.move(to: CGPoint(x: c.x, y: 0)); axes.addLine(to: CGPoint(x: c.x, y: size.height))
            ctx.stroke(axes, with: .color(.primary.opacity(0.12)), lineWidth: 0.5)
            if info.near_mm > 0 {
                let nr = CGFloat(Double(info.near_mm) * scale)
                ctx.stroke(Path(ellipseIn: CGRect(x: c.x - nr, y: c.y - nr, width: nr * 2, height: nr * 2)),
                           with: .color(.yellow.opacity(0.5)), style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
            }
            // voices
            for (i, p) in pos.enumerated() {
                let on = i < active.count && active[i]
                let col = streamColor(i < voiceStream.count ? voiceStream[i] : 0)
                let q = pt(p.x, p.y)
                let rad: CGFloat = on ? 7 : 4
                if on {
                    ctx.fill(Path(ellipseIn: CGRect(x: q.x - 13, y: q.y - 13, width: 26, height: 26)), with: .color(col.opacity(0.25)))
                }
                ctx.fill(Path(ellipseIn: CGRect(x: q.x - rad, y: q.y - rad, width: rad * 2, height: rad * 2)),
                         with: .color(col.opacity(on ? 1 : 0.3)))
                if on {
                    ctx.draw(Text("\(i)").font(.system(size: 8)).foregroundStyle(.secondary), at: CGPoint(x: q.x + 10, y: q.y - 8))
                }
            }
            // listener and yaw arrow, over the voices: space/SPEC.md section 13's
            // yaw turns the listener clockwise, to the right, as seen from above
            let th = Double(yawMdeg) / 1000 * .pi / 180
            let a = CGPoint(x: c.x + CGFloat(sin(th)) * 24, y: c.y - CGFloat(cos(th)) * 24)
            var arrow = Path(); arrow.move(to: c); arrow.addLine(to: a)
            ctx.stroke(arrow, with: .color(.white), lineWidth: 2)
            ctx.stroke(Path(ellipseIn: CGRect(x: c.x - 7, y: c.y - 7, width: 14, height: 14)), with: .color(.white), lineWidth: 2)
            ctx.fill(Path(ellipseIn: CGRect(x: a.x - 3, y: a.y - 3, width: 6, height: 6)), with: .color(.white))
            ctx.draw(Text(String(format: "%.1f m across · yaw %.1f°", extent * 2 / 1000, Double(yawMdeg) / 1000))
                .font(.caption2).foregroundStyle(.secondary), at: CGPoint(x: 80, y: size.height - 8))
            ctx.draw(Text("front").font(.caption2).foregroundStyle(.tertiary), at: CGPoint(x: c.x, y: 8))
        }
        .background(Color.black.opacity(0.25))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

// ---- 3D space view  ----

/// The camera of the 3D view, an orthographic projection: az turns the scene
/// about the vertical (degrees; 0: seen from behind the listener), el tilts
/// it (degrees; 0: level, 90: from above, which is the top view).
struct SpaceCamera: Equatable {
    var az: Double = 0
    var el: Double = 30
    static let home = SpaceCamera()
}

/// The listener, the yaw, each voice's position (Engine.voicePos: where the
/// spatial module puts it now) with a line down to the floor, and each
/// voice's path (fms_player_paths: solid for straight motion, dotted for a
/// jump), on the floor's metre rings; x right, y forward, z up, mm.
struct SpaceView3D: View {
    let info: fms_info_t
    let pos: [SIMD3<Int32>]
    let active: [Bool]
    let voiceStream: [Int]
    let yawMdeg: Int32
    let paths: [fms_pathpt_t]
    let camera: SpaceCamera

    var body: some View {
        Canvas { ctx, size in
            let c = CGPoint(x: size.width / 2, y: size.height / 2)
            var extent = Double(max(info.near_mm, 500)) * 1.5
            func grow(_ x: Int32, _ y: Int32, _ z: Int32) {
                extent = max(extent, Double(abs(x)) * 1.2, Double(abs(y)) * 1.2, Double(abs(z)) * 1.2)
            }
            for p in pos { grow(p.x, p.y, p.z) }
            for q in paths { grow(q.x, q.y, q.z) }
            let scale = max(1, Double(min(size.width, size.height)) / 2 - 20) / extent
            let az = camera.az * .pi / 180, el = camera.el * .pi / 180
            let ca = cos(az), sa = sin(az), ce = cos(el), se = sin(el)
            func pt(_ x: Double, _ y: Double, _ z: Double) -> CGPoint {
                let xr = x * ca - y * sa, yr = x * sa + y * ca
                return CGPoint(x: c.x + CGFloat(xr * scale), y: c.y - CGFloat((z * ce + yr * se) * scale))
            }
            func ring(_ r: Double) -> Path {
                var p = Path()
                for k in 0...72 {
                    let t = Double(k) * 2 * .pi / 72
                    let q = pt(r * cos(t), r * sin(t), 0)
                    if k == 0 { p.move(to: q) } else { p.addLine(to: q) }
                }
                return p
            }
            // the floor: metre rings, the near circle, the axes
            let step = extent > 8000 ? 5000.0 : 1000.0
            var r = step
            while r < extent * 1.5 {
                ctx.stroke(ring(r), with: .color(.primary.opacity(0.08)), lineWidth: 0.5)
                r += step
            }
            if info.near_mm > 0 {
                ctx.stroke(ring(Double(info.near_mm)), with: .color(.yellow.opacity(0.5)), style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
            }
            let ax = extent * 0.6
            for (end, name) in [((ax, 0.0, 0.0), "x"), ((0.0, ax, 0.0), "front"), ((0.0, 0.0, ax), "up")] {
                var p = Path(); p.move(to: pt(0, 0, 0)); p.addLine(to: pt(end.0, end.1, end.2))
                ctx.stroke(p, with: .color(.primary.opacity(0.25)), lineWidth: 0.5)
                ctx.draw(Text(name).font(.caption2).foregroundStyle(.tertiary), at: pt(end.0 * 1.08, end.1 * 1.08, end.2 * 1.08))
            }
            // paths, per voice in its stream's colour
            var last: [UInt32: CGPoint] = [:]
            var solid: [Int: Path] = [:], jumps = Path()
            for q in paths {
                let v = Int(q.voice)
                let here = pt(Double(q.x), Double(q.y), Double(q.z))
                if let from = last[q.voice] {
                    if q.pen != 0 {
                        var p = solid[v] ?? Path(); p.move(to: from); p.addLine(to: here); solid[v] = p
                    } else {
                        jumps.move(to: from); jumps.addLine(to: here)
                    }
                }
                last[q.voice] = here
            }
            ctx.stroke(jumps, with: .color(.primary.opacity(0.3)), style: StrokeStyle(lineWidth: 1, dash: [1, 3]))
            for (v, p) in solid {
                ctx.stroke(p, with: .color(streamColor(v < voiceStream.count ? voiceStream[v] : 0).opacity(0.6)), lineWidth: 1.5)
            }
            // voices, each with a line down to the floor
            for (i, p) in pos.enumerated() {
                let on = i < active.count && active[i]
                let col = streamColor(i < voiceStream.count ? voiceStream[i] : 0)
                let q = pt(Double(p.x), Double(p.y), Double(p.z))
                if p.z != 0 {
                    var drop = Path(); drop.move(to: q); drop.addLine(to: pt(Double(p.x), Double(p.y), 0))
                    ctx.stroke(drop, with: .color(col.opacity(0.4)), style: StrokeStyle(lineWidth: 1, dash: [2, 2]))
                }
                let rad: CGFloat = on ? 7 : 4
                if on {
                    ctx.fill(Path(ellipseIn: CGRect(x: q.x - 13, y: q.y - 13, width: 26, height: 26)), with: .color(col.opacity(0.25)))
                }
                ctx.fill(Path(ellipseIn: CGRect(x: q.x - rad, y: q.y - rad, width: rad * 2, height: rad * 2)),
                         with: .color(col.opacity(on ? 1 : 0.3)))
                if on {
                    ctx.draw(Text("\(i)").font(.system(size: 8)).foregroundStyle(.secondary), at: CGPoint(x: q.x + 10, y: q.y - 8))
                }
            }
            // the listener and its yaw on the floor (clockwise, to the right)
            let o = pt(0, 0, 0)
            let th = Double(yawMdeg) / 1000 * .pi / 180
            let len = 24 / scale
            let a = pt(sin(th) * len, cos(th) * len, 0)
            var arrow = Path(); arrow.move(to: o); arrow.addLine(to: a)
            ctx.stroke(arrow, with: .color(.white), lineWidth: 2)
            ctx.stroke(Path(ellipseIn: CGRect(x: o.x - 7, y: o.y - 7, width: 14, height: 14)), with: .color(.white), lineWidth: 2)
            ctx.fill(Path(ellipseIn: CGRect(x: a.x - 3, y: a.y - 3, width: 6, height: 6)), with: .color(.white))
            ctx.draw(Text(String(format: "%.1f m across · yaw %.1f° · turned %.0f°, tilted %.0f°", extent * 2 / 1000,
                                 Double(yawMdeg) / 1000, camera.az, camera.el))
                .font(.caption2).foregroundStyle(.secondary), at: CGPoint(x: size.width / 2, y: size.height - 8))
        }
        .background(Color.black.opacity(0.25))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

/// SpaceView3D with its camera: drag to turn and tilt, double-click for the
/// first view.
struct SpacePanel3D: View {
    let info: fms_info_t
    let pos: [SIMD3<Int32>]
    let active: [Bool]
    let voiceStream: [Int]
    let yawMdeg: Int32
    let paths: [fms_pathpt_t]
    @State private var camera = SpaceCamera.home
    @State private var start: SpaceCamera?

    var body: some View {
        SpaceView3D(info: info, pos: pos, active: active, voiceStream: voiceStream, yawMdeg: yawMdeg, paths: paths, camera: camera)
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 2)
                .onChanged { g in
                    let s = start ?? camera
                    if start == nil { start = camera }
                    camera.az = s.az + Double(g.translation.width) * 0.5
                    camera.el = min(90, max(-90, s.el + Double(g.translation.height) * 0.5))
                }
                .onEnded { _ in start = nil })
            .onTapGesture(count: 2) { camera = .home }
            .help("Drag to turn and tilt; double-click for the first view.")
    }
}

/// One voice's path as text, for `make uitest` (UIScript's expect space.path):
/// each point x,y,z@sample, "jump " before a pen-0 point.
func pathText(_ paths: [fms_pathpt_t], voice: UInt32) -> String {
    paths.filter { $0.voice == voice }
        .map { ($0.pen == 0 ? "jump " : "") + "\($0.x),\($0.y),\($0.z)@\($0.sample)" }
        .joined(separator: " | ")
}

// ---- stereo picture  ----

/// tools/music/analyze.py's stereo measures (cmd_overview), from exact
/// integer sums: mid = (L + R) / 2 and side = (L - R) / 2 as floats in
/// [-1, 1); side-mid is the side's mean square over the mid's in dB, each
/// floored at 1e-20 before the log (its db); corr is numpy's corrcoef of L
/// and R (Pearson, means removed, clipped to -1...1), nil where numpy gives
/// nan (a constant channel).
struct StereoSums {
    var n: Int64 = 0, l: Int64 = 0, r: Int64 = 0, ll: Int64 = 0, rr: Int64 = 0, lr: Int64 = 0
    mutating func add(_ a: Int16, _ b: Int16) {
        let x = Int64(a), y = Int64(b)
        n += 1; l += x; r += y; ll += x * x; rr += y * y; lr += x * y
    }
    var sideMid: Double {
        let d = 4 * 1073741824 * Double(max(n, 1))
        let side = Double(ll + rr - 2 * lr) / d, mid = Double(ll + rr + 2 * lr) / d
        return 10 * log10(max(side, 1e-20)) - 10 * log10(max(mid, 1e-20))
    }
    var corr: Double? {
        let N = Double(n)
        let cov = N * Double(lr) - Double(l) * Double(r)
        let vl = N * Double(ll) - Double(l) * Double(l), vr = N * Double(rr) - Double(r) * Double(r)
        guard n > 1, vl > 0, vr > 0 else { return nil }
        return min(1, max(-1, cov / (vl.squareRoot() * vr.squareRoot())))
    }
}

/// analyze.py overview's stereo columns over a whole record: one row per
/// nominal window, with the remainder included in the final row. A short
/// record has one row. Undefined correlation is nil (JSON null).
struct StereoRow { let start: UInt32; let length: UInt32; let sideMid: Double; let corr: Double? }
struct StereoStrip {
    let key: Int                  // Engine.loadID it was made for
    let window: UInt32            // frames
    let fs: UInt32
    let frames: UInt32
    let rows: [StereoRow]
    let hash: UInt64              // of the frames rendered
    let error: String?
}

/// The goniometer: each frame of the scope's buffer at (side, mid), mono up
/// the middle, the left channel alone on the upper-left diagonal; under it
/// the side-mid and corr of those frames.
struct Goniometer: View {
    let samples: [Int16]
    let channels: Int

    var body: some View {
        Canvas { ctx, size in
            let half = max(10, min(size.width, size.height - 16) / 2 - 4)
            let c = CGPoint(x: size.width / 2, y: half + 4)
            var frame = Path()
            frame.addRect(CGRect(x: c.x - half, y: c.y - half, width: half * 2, height: half * 2))
            frame.move(to: CGPoint(x: c.x, y: c.y - half)); frame.addLine(to: CGPoint(x: c.x, y: c.y + half))
            frame.move(to: CGPoint(x: c.x - half, y: c.y)); frame.addLine(to: CGPoint(x: c.x + half, y: c.y))
            frame.move(to: CGPoint(x: c.x - half, y: c.y - half)); frame.addLine(to: CGPoint(x: c.x + half, y: c.y + half))
            frame.move(to: CGPoint(x: c.x + half, y: c.y - half)); frame.addLine(to: CGPoint(x: c.x - half, y: c.y + half))
            ctx.stroke(frame, with: .color(.primary.opacity(0.12)), lineWidth: 0.5)
            ctx.draw(Text("L").font(.caption2).foregroundStyle(.tertiary), at: CGPoint(x: c.x - half + 8, y: c.y - half + 8))
            ctx.draw(Text("R").font(.caption2).foregroundStyle(.tertiary), at: CGPoint(x: c.x + half - 8, y: c.y - half + 8))
            ctx.draw(Text("M").font(.caption2).foregroundStyle(.tertiary), at: CGPoint(x: c.x + 8, y: c.y - half + 8))
            guard channels == 2, samples.count >= 4 else { return }
            var dots = Path()
            var s = StereoSums()
            for i in stride(from: 0, to: samples.count - 1, by: 2) {
                let L = Double(samples[i]), R = Double(samples[i + 1])
                let p = CGPoint(x: c.x + CGFloat((R - L) / 65536) * half, y: c.y - CGFloat((L + R) / 65536) * half)
                dots.addRect(CGRect(x: p.x, y: p.y, width: 1, height: 1))
                s.add(samples[i], samples[i + 1])
            }
            ctx.fill(dots, with: .color(.green.opacity(0.8)))
            let cc = s.corr.map { String(format: "%.2f", $0) } ?? "-"
            ctx.draw(Text(String(format: "corr %@ · side-mid %.1f dB · %d frames", cc, s.sideMid, Int(s.n)))
                .font(.caption2.monospacedDigit()).foregroundStyle(.secondary), at: CGPoint(x: c.x, y: size.height - 6))
        }
    }
}

/// The record's width and correlation over time: analyze.py overview's
/// side-mid dB and corr per window, with the playhead.
struct StereoStripView: View {
    let strip: StereoStrip?
    let position: UInt32

    var body: some View {
        Canvas { ctx, size in
            guard let st = strip else {
                ctx.draw(Text("rendering...").font(.caption).foregroundStyle(.secondary), at: CGPoint(x: size.width / 2, y: size.height / 2))
                return
            }
            let secs = Double(st.window) / Double(max(st.fs, 1))
            if let e = st.error {
                ctx.draw(Text(e).font(.caption).foregroundStyle(.red), at: CGPoint(x: size.width / 2, y: size.height / 2))
                return
            }
            if st.rows.isEmpty {
                ctx.draw(Text("No audio frames to analyze.")
                    .font(.caption).foregroundStyle(.secondary), at: CGPoint(x: size.width / 2, y: size.height / 2))
                return
            }
            let span = Double(max(st.frames, 1))
            func x(_ f: Double) -> CGFloat { CGFloat(f / span) * size.width }
            let laneH = (size.height - 28) / 2
            // corr, -1 to 1
            let top = CGFloat(12)
            var zero = Path(); zero.move(to: CGPoint(x: 0, y: top + laneH / 2)); zero.addLine(to: CGPoint(x: size.width, y: top + laneH / 2))
            ctx.stroke(zero, with: .color(.primary.opacity(0.15)), lineWidth: 0.5)
            var cl = Path()
            for row in st.rows {
                guard let cv = row.corr else { continue }
                let y = top + laneH / 2 - CGFloat(cv) * laneH / 2
                cl.move(to: CGPoint(x: x(Double(row.start)), y: y))
                cl.addLine(to: CGPoint(x: x(Double(row.start) + Double(row.length)), y: y))
            }
            ctx.stroke(cl, with: .color(.green), lineWidth: 2)
            ctx.draw(Text("corr  1 / 0 / -1").font(.caption2).foregroundStyle(.secondary), at: CGPoint(x: 52, y: 5))
            // side-mid dB, the rows' range
            let sm = st.rows.map(\.sideMid)
            let lo = max(-60, sm.min() ?? -60), hi = max(lo + 1, sm.max() ?? 0)
            let top2 = top + laneH + 14
            var sl = Path()
            for row in st.rows {
                let v = min(hi, max(lo, row.sideMid))
                let y = top2 + laneH - CGFloat((v - lo) / (hi - lo)) * laneH
                sl.move(to: CGPoint(x: x(Double(row.start)), y: y))
                sl.addLine(to: CGPoint(x: x(Double(row.start) + Double(row.length)), y: y))
            }
            ctx.stroke(sl, with: .color(.orange), lineWidth: 2)
            ctx.draw(Text(String(format: "side-mid dB  %.1f to %.1f%@", hi, lo, (sm.min() ?? 0) < -60 ? " (lower drawn at -60)" : ""))
                .font(.caption2).foregroundStyle(.secondary), at: CGPoint(x: 110, y: top + laneH + 6))
            // the playhead
            let px = x(Double(min(position, st.frames)))
            var ph = Path(); ph.move(to: CGPoint(x: px, y: top)); ph.addLine(to: CGPoint(x: px, y: size.height))
            ctx.stroke(ph, with: .color(.white.opacity(0.7)), lineWidth: 1)
            ctx.draw(Text(String(format: "%d rows · %g s nominal", st.rows.count, secs)).font(.caption2).foregroundStyle(.tertiary),
                     at: CGPoint(x: size.width - 60, y: 5))
        }
        .background(Color.black.opacity(0.25))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

// ---- envelope ----

struct EnvelopeView: View {
    let op: fms_op_t
    var color: Color = .accentColor
    var body: some View {
        Canvas { ctx, size in
            let a = Double(op.attack_ms), d = Double(op.decay_ms), r = Double(op.release_ms)
            let hold = max(100, (a + d + r) * 0.3)
            let total = max(1, a + d + hold + r)
            let sus = pow(10, -Double(op.sustain_db10) / 200)
            let h = Double(size.height) - 12, w = Double(size.width)
            func p(_ t: Double, _ amp: Double) -> CGPoint { CGPoint(x: t / total * w, y: 2 + (1 - amp) * (h - 2)) }
            var path = Path()
            path.move(to: p(0, 0))
            path.addLine(to: p(a, 1))
            let steps = 24
            for i in 1...steps {   // decay: linear in dB from 0 to the sustain level
                let f = Double(i) / Double(steps)
                path.addLine(to: p(a + d * f, pow(sus, f)))
            }
            path.addLine(to: p(a + d + hold, sus))
            let floor = 1e-4
            for i in 1...steps {   // release: linear in dB down to -80 dB
                let f = Double(i) / Double(steps)
                let amp = sus * pow(floor / max(sus, floor), f)
                path.addLine(to: p(a + d + hold + r * f, f == 1 ? 0 : amp))
            }
            var fill = path
            fill.addLine(to: p(total, 0)); fill.closeSubpath()
            ctx.fill(fill, with: .color(color.opacity(0.18)))
            ctx.stroke(path, with: .color(color), lineWidth: 1.5)
            var off = Path()
            off.move(to: p(a + d + hold, 0)); off.addLine(to: p(a + d + hold, 1))
            ctx.stroke(off, with: .color(.primary.opacity(0.2)), style: StrokeStyle(lineWidth: 0.5, dash: [2, 2]))
            ctx.draw(Text("A\(op.attack_ms) D\(op.decay_ms) S-\(fmt1(Double(op.sustain_db10) / 10))dB R\(op.release_ms) ms")
                .font(.system(size: 8)).foregroundStyle(.secondary), at: CGPoint(x: size.width / 2, y: size.height - 5))
        }
    }
}

func fmt1(_ v: Double) -> String { String(format: "%.1f", v) }
func fmt2(_ v: Double) -> String { String(format: "%.2f", v) }

// ---- route graph ----

struct RouteGraph: View {
    let patch: PatchLines
    var body: some View {
        Canvas { ctx, size in
            let ops = opArray(patch)
            let n = patch.route_ops == 0 ? 2 : Int(min(patch.route_ops, UInt32(FMS_OPS)))
            var edges: [(Int, Int, UInt32)] = []
            var carriers = Set<Int>()
            if patch.route_ops == 0 {
                edges = [(1, 0, 256)]
                carriers = [0]
            } else {
                for f in 0..<n { for t in 0..<n { let w = routeWeight(patch, f, t); if w > 0 { edges.append((f, t, w)) } } }
                for i in 0..<n where patch.route_carriers & (1 << UInt32(i)) != 0 { carriers.insert(i) }
            }
            let outY = size.height - 14
            let nodeY = size.height * 0.55
            let r: CGFloat = 15
            func center(_ i: Int) -> CGPoint {
                // op 0 on the right so a 2-op path reads modulator -> carrier
                let slot = CGFloat(n - 1 - i)
                return CGPoint(x: size.width * (slot + 0.5) / CGFloat(n), y: nodeY)
            }
            // output bus
            var bus = Path()
            bus.move(to: CGPoint(x: 10, y: outY)); bus.addLine(to: CGPoint(x: size.width - 10, y: outY))
            ctx.stroke(bus, with: .color(.green.opacity(0.6)), lineWidth: 2)
            ctx.draw(Text("out").font(.caption2).foregroundStyle(.green), at: CGPoint(x: 20, y: outY - 8))
            for i in carriers.sorted() {
                let c = center(i)
                var l = Path(); l.move(to: CGPoint(x: c.x, y: c.y + r)); l.addLine(to: CGPoint(x: c.x, y: outY))
                ctx.stroke(l, with: .color(.green.opacity(0.6)), lineWidth: 1.5)
            }
            for (f, t, w) in edges {
                let a = center(f), b = center(t)
                var p = Path()
                let label: CGPoint
                if f == t {
                    p.addEllipse(in: CGRect(x: a.x - 9, y: a.y - r - 18, width: 18, height: 20))
                    label = CGPoint(x: a.x + 18, y: a.y - r - 14)
                } else {
                    let up: CGFloat = f > t ? 1 : -1   // edges toward op 0 arc above, back edges below
                    let dist = abs(b.x - a.x)
                    let ctl = CGPoint(x: (a.x + b.x) / 2, y: a.y - up * (18 + dist * 0.22))
                    func rim(_ c: CGPoint) -> CGPoint {   // where the curve leaves a node's circle
                        let dx = ctl.x - c.x, dy = ctl.y - c.y, l = max(1, (dx * dx + dy * dy).squareRoot())
                        return CGPoint(x: c.x + dx / l * r, y: c.y + dy / l * r)
                    }
                    let s = rim(a), e = rim(b)
                    p.move(to: s); p.addQuadCurve(to: e, control: ctl)
                    label = CGPoint(x: ctl.x, y: (ctl.y + a.y) / 2 - up * 6)
                    // arrow head
                    let ang = atan2(e.y - ctl.y, e.x - ctl.x)
                    var h = Path()
                    h.move(to: e)
                    h.addLine(to: CGPoint(x: e.x - 7 * cos(ang - 0.4), y: e.y - 7 * sin(ang - 0.4)))
                    h.move(to: e)
                    h.addLine(to: CGPoint(x: e.x - 7 * cos(ang + 0.4), y: e.y - 7 * sin(ang + 0.4)))
                    ctx.stroke(h, with: .color(.orange), lineWidth: 1.5)
                }
                ctx.stroke(p, with: .color(f == t ? .pink : .orange), lineWidth: 1.2)
                ctx.draw(Text(fmt2(Double(w) / 256)).font(.system(size: 9)).foregroundStyle(.secondary), at: label)
            }
            for i in 0..<n {
                let c = center(i)
                let carrier = carriers.contains(i)
                let fb = ops[i].feedback_q10
                if fb > 0 && !edges.contains(where: { $0.0 == i && $0.1 == i }) {
                    ctx.stroke(Path(ellipseIn: CGRect(x: c.x - 9, y: c.y - r - 18, width: 18, height: 20)),
                               with: .color(.pink), style: StrokeStyle(lineWidth: 1, dash: [3, 2]))
                    ctx.draw(Text("fb \(fmt2(Double(fb) / 1024))").font(.system(size: 9)).foregroundStyle(.pink),
                             at: CGPoint(x: c.x + 24, y: c.y - r - 14))
                }
                let rect = CGRect(x: c.x - r, y: c.y - r, width: r * 2, height: r * 2)
                ctx.fill(Path(ellipseIn: rect), with: .color(carrier ? Color.green.opacity(0.75) : Color.blue.opacity(0.55)))
                ctx.stroke(Path(ellipseIn: rect), with: .color(.white.opacity(0.7)), lineWidth: 1)
                ctx.draw(Text("op\(i)").font(.caption.bold()).foregroundStyle(.white), at: c)
            }
        }
        .frame(minHeight: 130)
    }
}

// ---- patch inspector ----

/// The operator waves of fm_a1.h, 0 to 11; any other value plays as 0.
let inspWaveNames = ["sine", "half", "abs", "quarter", "noise", "alt sine",
                     "camel", "square", "log-saw", "pink", "colour lp", "colour hp"]
let inspFilterModes = ["off", "low", "band", "high", "notch"]

func opRows(_ op: fms_op_t) -> [(String, String)] {
    [
        ("ratio", op.fixed_millihz != 0 ? "fixed" : String(format: "%.4f", Double(op.ratio_q16) / 65536)),
        ("fixed Hz", op.fixed_millihz == 0 ? "-" : fmt2(Double(op.fixed_millihz) / 1000)),
        ("level dB", "-" + fmt1(Double(op.level_db10) / 10)),
        ("index rad", fmt2(Double(op.index_q10) / 1024)),
        ("feedback rad", fmt2(Double(op.feedback_q10) / 1024)),
        ("vel sens", fmt2(Double(op.vel_sens_q8) / 256)),
        ("attack ms", "\(op.attack_ms)"),
        ("decay ms", "\(op.decay_ms)"),
        ("sustain dB", "-" + fmt1(Double(op.sustain_db10) / 10)),
        ("release ms", "\(op.release_ms)"),
        ("ks break Hz", op.ks_break_millihz == 0 ? "-" : fmt1(Double(op.ks_break_millihz) / 1000)),
        ("ks slope dB", fmt1(Double(op.ks_slope_db10) / 10)),
        ("wave", Int(op.wave) < inspWaveNames.count ? inspWaveNames[Int(op.wave)] : "\(op.wave)"),
        ("rate scale", "\(op.rate_scale)"),
        ("phase °", fmt1(Double(op.phase_u16) / 65536 * 360)),
        ("free run", op.free_run != 0 ? "yes" : "no"),
    ]
}

struct PatchInspector: View {
    let patches: [PatchLines]
    let names: [String]
    var dropped: [String] = []   // per patch: the statements not shown (instrDropped)
    @State private var pick = 0
    var body: some View {
        if patches.isEmpty {
            Text("No patches.").foregroundStyle(.secondary)
        } else {
            let i = min(pick, patches.count - 1)
            let p = patches[i]
            let ops = opArray(p)
            let defined = (0..<Int(FMS_OPS)).filter { p.ops_defined & (1 << UInt32($0)) != 0 }
            let shown = defined.isEmpty ? [0, 1] : defined
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Picker("Patch", selection: $pick) {
                        ForEach(Array(names.enumerated()), id: \.offset) { k, n in Text(n).tag(k) }
                    }
                    .frame(maxWidth: 260)
                    Spacer()
                    Text("route \(p.route_ops == 0 ? "fixed 2-op" : "\(p.route_ops) ops") · filter \(Int(p.filter_mode) < inspFilterModes.count ? inspFilterModes[Int(p.filter_mode)] : "\(p.filter_mode)")"
                         + (p.filter_mode != 0 ? " \(fmt1(Double(p.filter_cutoff_millihz) / 1000)) Hz Q \(fmt2(Double(p.filter_q_milli) / 1000))" : ""))
                        .font(.caption).foregroundStyle(.secondary)
                }
                if i < dropped.count, !dropped[i].isEmpty {
                    Text("Not shown here (the editable copy does not carry them; playback uses them): "
                         + dropped[i].split(separator: " ").joined(separator: ", "))
                        .font(.caption).foregroundStyle(.orange)
                }
                ModMatrixView(patch: p)
                HStack(alignment: .top, spacing: 12) {
                    Grid(alignment: .trailing, horizontalSpacing: 14, verticalSpacing: 2) {
                        GridRow {
                            Text("").gridColumnAlignment(.leading)
                            ForEach(shown, id: \.self) { k in Text("op\(k)").font(.caption.bold()).foregroundStyle(streamColor(k)) }
                        }
                        let rows = shown.map { opRows(ops[$0]) }
                        ForEach(0..<16, id: \.self) { r in
                            GridRow {
                                Text(rows[0][r].0).font(.caption).foregroundStyle(.secondary).gridColumnAlignment(.leading)
                                ForEach(0..<rows.count, id: \.self) { c in
                                    Text(rows[c][r].1).font(.caption.monospacedDigit())
                                }
                            }
                        }
                    }
                    .padding(8)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.black.opacity(0.15)))
                    VStack(alignment: .leading, spacing: 6) {
                        RouteGraph(patch: p)
                            .background(RoundedRectangle(cornerRadius: 6).fill(Color.black.opacity(0.15)))
                        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 6) {
                            ForEach(shown, id: \.self) { k in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("op\(k) envelope").font(.caption2).foregroundStyle(.secondary)
                                    EnvelopeView(op: ops[k], color: streamColor(k)).frame(height: 70)
                                }
                            }
                        }
                    }
                    .frame(minWidth: 280)
                }
            }
        }
    }
}

// ---- voices: one chip per voice, lit while it sounds ----

struct VoiceGrid: View {
    let active: [Bool]
    let patchOf: [String]
    let voiceStream: [Int]
    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 78), spacing: 4)], spacing: 4) {
            ForEach(0..<patchOf.count, id: \.self) { v in
                let on = v < active.count && active[v]
                let col = streamColor(v < voiceStream.count ? voiceStream[v] : 0)
                HStack(spacing: 3) {
                    Text("\(v)").font(.system(size: 9, weight: .bold).monospacedDigit())
                    Text(patchOf[v]).font(.system(size: 9)).lineLimit(1)
                }
                .padding(.horizontal, 5).padding(.vertical, 3)
                .frame(maxWidth: .infinity, alignment: .leading)
                .foregroundStyle(on ? Color.black : Color.secondary)
                .background(RoundedRectangle(cornerRadius: 4).fill(on ? col : col.opacity(0.12)))
            }
        }
    }
}

// Authored matrix routes, read only. Amounts are displayed in their target's
// units; the grid is a view of authored slot sums, not DSP state or gain.
struct ModMatrixView: View {
    let patch: PatchLines
    private var slots: [[String]] { patch.rows.filter { $0.enabled && $0.statement == "mod" && $0.tokens.count >= 6 }.map(\.tokens) }
    private func token(_ row: [String], _ i: Int, _ fallback: String = "—") -> String { i < row.count ? row[i] : fallback }
    var body: some View {
        let rows = slots
        VStack(alignment: .leading, spacing: 8) {
            Text("Modulation matrix").font(.headline)
            if rows.isEmpty { Text("No modulation slots.").foregroundStyle(.secondary) }
            else {
                Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 4) {
                    GridRow { ForEach(["slot", "source", "target / index", "amount", "shift", "curve", "via"], id: \.self) { Text($0).bold() } }
                    ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                        GridRow {
                            Text(row[2]); Text(row[3]); Text(row[4]); Text(row[5])
                            Text(token(row, 6, "3")); Text(token(row, 7)); Text(token(row, 8))
                        }
                    }
                }.font(.system(.caption, design: .monospaced))
                let sources = Array(Set(rows.map { $0[3] })).sorted()
                let targets = Array(Set(rows.map { $0[4] })).sorted()
                let maximum = max(1, rows.compactMap { Int64($0[5]) }.map { abs($0) }.max() ?? 1)
                Grid(horizontalSpacing: 4, verticalSpacing: 4) {
                    GridRow { Text("source → target"); ForEach(targets, id: \.self) { Text($0) } }
                    ForEach(sources, id: \.self) { source in
                        GridRow {
                            Text(source).frame(maxWidth: .infinity, alignment: .leading)
                            ForEach(targets, id: \.self) { target in
                                let values = rows.filter { $0[3] == source && $0[4] == target }.compactMap { Int64($0[5]) }
                                let total = values.reduce(Int64(0), +)
                                Text(values.isEmpty ? "·" : String(total))
                                    .frame(minWidth: 64, minHeight: 28)
                                    .background((total < 0 ? Color.orange : Color.blue).opacity(values.isEmpty ? 0.05 : 0.12 + 0.7 * min(Double(abs(total)) / Double(maximum), 1)))
                                    .help(values.map(String.init).joined(separator: " + "))
                            }
                        }
                    }
                }.font(.system(.caption, design: .monospaced))
                Text("Authored amount sums; targets have different units. Blue positive, orange negative. Read only; playback and spatial events are unchanged.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
