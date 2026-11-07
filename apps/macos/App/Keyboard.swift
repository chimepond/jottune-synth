// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// Keyboard.swift: a two-octave on-screen piano, the computer keys (A W S E D
// F T G Y H U J K for one octave, Z and X shift it) and a round-robin voice
// allocator over the live player's voices. Frequencies are 12-TET with
// A4 = 440 Hz, in millihertz.

import AppKit
import SwiftUI

/// MIDI note number to millihertz, 12-TET, A4 (69) = 440 Hz.
func noteMilliHz(_ note: Int) -> UInt32 {
    UInt32((440_000.0 * pow(2.0, Double(note - 69) / 12.0)).rounded())
}

func noteName(_ note: Int) -> String {
    let names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return names[((note % 12) + 12) % 12] + String(note / 12 - 1)
}

/// Gives each held note a voice: a free voice when there is one (the one
/// freed longest ago), else the oldest held voice, retriggered.
@MainActor final class VoiceAllocator: ObservableObject {
    @Published private(set) var held: [Int: Int] = [:]   // note -> voice
    private var stamp: [UInt64]
    private var owner: [Int?]
    private var clock: UInt64 = 0
    let voices: Int
    /// Called on a note when no live player runs: it arms one, and calls
    /// armed(_:) when that is done.
    var autoArm: (() -> Void)?
    /// Keys pressed while a live player opens: played once it is armed,
    /// forgotten if the key goes up first or the arm fails.
    private var waiting: [(note: Int, vel: UInt32)] = []

    init(voices: Int = 8) {
        self.voices = voices
        stamp = Array(repeating: 0, count: voices)
        owner = Array(repeating: nil, count: voices)
    }

    func noteOn(_ note: Int, velocityQ15: UInt32) {
        let engine = Engine.shared
        guard engine.isLive else {
            guard let autoArm else { return }
            waiting.removeAll { $0.note == note }
            waiting.append((note, velocityQ15))
            if waiting.count == 1 { autoArm() }   // one arm for every key pressed meanwhile
            return
        }
        if let v = held[note] {          // the same key again: retrigger its voice
            engine.noteOn(voice: v, freqMilliHz: noteMilliHz(note), velocityQ15: velocityQ15)
            return
        }
        let free = (0..<voices).filter { owner[$0] == nil }
        let pool = free.isEmpty ? Array(0..<voices) : free
        let v = pool.min { stamp[$0] < stamp[$1] }!
        if let old = owner[v] { held[old] = nil }
        clock += 1
        stamp[v] = clock
        owner[v] = note
        held[note] = v
        engine.noteOn(voice: v, freqMilliHz: noteMilliHz(note), velocityQ15: velocityQ15)
    }

    /// The arm finished: play the keys still down, or forget them.
    func armed(_ ok: Bool) {
        let keys = waiting
        waiting = []
        if ok { for k in keys { noteOn(k.note, velocityQ15: k.vel) } }
    }

    func noteOff(_ note: Int) {
        waiting.removeAll { $0.note == note }
        guard let v = held[note] else { return }
        held[note] = nil
        owner[v] = nil
        clock += 1
        stamp[v] = clock
        if Engine.shared.isLive { Engine.shared.noteOff(voice: v) }
    }

    func allOff() {
        for note in Array(held.keys) { noteOff(note) }
    }

    /// Live MIDI input (MidiInput) took voice v (taken) or ended its note
    /// there: a key on it loses its voice, with no note-off of its own, and
    /// the voice counts as held while MIDI has it (owner -1), so a key takes
    /// a free voice first.
    func external(voice v: Int, taken: Bool) {
        guard v >= 0, v < voices else { return }
        if let old = owner[v], old >= 0 { held[old] = nil }
        owner[v] = taken ? -1 : nil
        clock += 1
        stamp[v] = clock
    }

    /// Every voice MIDI held, free again (a new live player).
    func clearExternal() {
        for v in 0..<voices where owner[v] == -1 { owner[v] = nil }
    }

    /// All keys up, and the allocation order forgotten: the next note takes
    /// voice 0. FMS_SCRIPT's checks call it so both runs of a check use the
    /// same voices.
    func reset() {
        allOff()
        stamp = Array(repeating: 0, count: voices)
        owner = Array(repeating: nil, count: voices)
        clock = 0
    }
}

/// The piano: base is the lowest C shown; two octaves.
struct PianoKeyboard: View {
    @ObservedObject var alloc: VoiceAllocator
    let base: Int
    let velocityQ15: UInt32

    private static let whiteSteps = [0, 2, 4, 5, 7, 9, 11]
    private static let blackSteps: [(step: Int, slot: Double)] = [(1, 1), (3, 2), (6, 4), (8, 5), (10, 6)]

    var body: some View {
        GeometryReader { geo in
            let whites = 14
            let w = geo.size.width / CGFloat(whites)
            let h = geo.size.height
            ZStack(alignment: .topLeading) {
                ForEach(0..<whites, id: \.self) { i in
                    let note = base + 12 * (i / 7) + Self.whiteSteps[i % 7]
                    key(note: note, black: false)
                        .frame(width: w - 1, height: h)
                        .offset(x: CGFloat(i) * w)
                }
                ForEach(0..<10, id: \.self) { j in
                    let oct = j / 5
                    let b = Self.blackSteps[j % 5]
                    let note = base + 12 * oct + b.step
                    key(note: note, black: true)
                        .frame(width: w * 0.62, height: h * 0.6)
                        .offset(x: (CGFloat(oct * 7) + CGFloat(b.slot)) * w - w * 0.31)
                }
            }
        }
    }

    private func key(note: Int, black: Bool) -> some View {
        let down = alloc.held[note] != nil
        return RoundedRectangle(cornerRadius: 3)
            .fill(down ? Color.accentColor : (black ? Color.black : Color.white))
            .overlay(RoundedRectangle(cornerRadius: 3).stroke(Color.gray.opacity(0.6)))
            .overlay(alignment: .bottom) {
                if !black && note % 12 == 0 {
                    Text(noteName(note)).font(.caption2).foregroundStyle(.gray).padding(.bottom, 3)
                }
            }
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { _ in if alloc.held[note] == nil { alloc.noteOn(note, velocityQ15: velocityQ15) } }
                .onEnded { _ in alloc.noteOff(note) })
    }
}

/// The computer keys while the Instrument tab shows. Typing into a text
/// field is left alone.
@MainActor final class ComputerKeys {
    private var monitor: Any?
    private var down: [UInt16: Int] = [:]         // key code -> note
    var octaveBase: Int = 60                       // the note of key A
    var velocityQ15: UInt32 = 26000
    var onOctave: (Int) -> Void = { _ in }
    let alloc: VoiceAllocator

    // A W S E D F T G Y H U J K: semitones 0 to 12 from octaveBase.
    private static let map: [Character: Int] = [
        "a": 0, "w": 1, "s": 2, "e": 3, "d": 4, "f": 5, "t": 6, "g": 7,
        "y": 8, "h": 9, "u": 10, "j": 11, "k": 12,
    ]

    init(alloc: VoiceAllocator) { self.alloc = alloc }

    func start() {
        guard monitor == nil else { return }
        monitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .keyUp]) { [weak self] ev in
            guard let self else { return ev }
            return MainActor.assumeIsolated { self.handle(ev) } ? nil : ev
        }
    }

    func stop() {
        if let m = monitor { NSEvent.removeMonitor(m) }
        monitor = nil
        for (_, n) in down { alloc.noteOff(n) }
        down.removeAll()
    }

    /// true: the event was a note key and is consumed.
    private func handle(_ ev: NSEvent) -> Bool {
        // Release keys we started even if focus, modifiers or layout changed.
        if ev.type == .keyUp, let n = down.removeValue(forKey: ev.keyCode) {
            alloc.noteOff(n)
            return true
        }
        if ev.window?.firstResponder is NSText { return false }
        if !ev.modifierFlags.intersection([.command, .control, .option]).isEmpty { return false }
        guard let ch = ev.charactersIgnoringModifiers?.lowercased().first else { return false }
        if ev.type == .keyDown {
            if ev.isARepeat { return Self.map[ch] != nil || ch == "z" || ch == "x" }
            if ch == "z" { octaveBase = max(12, octaveBase - 12); onOctave(octaveBase); return true }
            if ch == "x" { octaveBase = min(108, octaveBase + 12); onOctave(octaveBase); return true }
            guard let s = Self.map[ch] else { return false }
            let n = octaveBase + s
            down[ev.keyCode] = n
            alloc.noteOn(n, velocityQ15: velocityQ15)
            return true
        }
        return Self.map[ch] != nil
    }
}
