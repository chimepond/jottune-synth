// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// MidiInput.swift: live MIDI input (shared MIDI translator; , , ,
// ). CoreMIDI gives the app one client, one input port opened for MIDI
// 1.0 and connected to every source, and a virtual destination,
// "JotTune Synth Input", that other apps can play into. CoreMIDI's thread copies
// each packet's words and hands them to the main thread, the live queue's
// one producer (fms_host_push). There the bridge's translator (fms_midi.c,
// by midi2tl.py's rules) makes the live events, so a take holds them like
// the piano's and replays to the same hash. The translator's voices are the
// live player's 8: the piano's allocator and the translator each tell the
// other which voices they take. Messages that come while no live player runs
// are refused and counted (nothing arms on MIDI). Events the full live queue
// drops are counted too. The live box shows both, and what was ignored.
// Ordinary script mode opens no CoreMIDI; its `midi` step feeds bytes,
// timed by the live player's frames (now), so every run is the same.

import CoreMIDI
import Foundation

@MainActor final class MidiInput: ObservableObject {
    static let shared = MidiInput()

    /// The label the live box shows (label); refreshed at most 10 times a second.
    @Published private(set) var shown = "MIDI in: no CoreMIDI in script mode"

    private var tr: OpaquePointer?               // fms_midi_t *: made at each arm
    private var out = [fms_event_t](repeating: fms_event_t(), count: Int(FMS_MIDI_OUT))
    private var client = MIDIClientRef()
    private var port = MIDIPortRef()
    private var dest = MIDIEndpointRef()
    private var connected: [MIDIEndpointRef] = []
    private var setupError: String?
    private var publishPending = false

    // Since the last arm.
    private(set) var messages: UInt64 = 0        // UMP messages received
    private(set) var notLive: UInt64 = 0         // refused: no live player ran
    private(set) var dropped: UInt64 = 0         // events the full live queue refused
    private(set) var events: UInt64 = 0          // events pushed

    /// CoreMIDI: the client, the port on every source, the virtual destination.
    func start() {
        guard !Engine.offline, client == 0 else { return }
        var st = MIDIClientCreateWithBlock("JotTune Synth" as CFString, &client) { n in
            if n.pointee.messageID == .msgSetupChanged {
                DispatchQueue.main.async { MainActor.assumeIsolated { MidiInput.shared.connectSources() } }
            }
        }
        guard st == noErr else { return failed("no MIDI client (OSStatus \(st))") }
        st = MIDIInputPortCreateWithProtocol(client, "JotTune Synth In" as CFString, ._1_0, &port) { list, _ in
            MidiInput.received(list)
        }
        guard st == noErr else { return failed("no MIDI input port (OSStatus \(st))") }
        st = MIDIDestinationCreateWithProtocol(client, "JotTune Synth Input" as CFString, ._1_0, &dest) { list, _ in
            MidiInput.received(list)
        }
        if st != noErr { setupError = "no virtual input (OSStatus \(st))" }
        connectSources()
    }

    private func failed(_ s: String) {
        setupError = s
        publish()
    }

    /// Every source, again after each setup change (a device plugged or unplugged).
    func connectSources() {
        guard port != 0 else { return }
        for s in connected { MIDIPortDisconnectSource(port, s) }
        connected = []
        for i in 0..<MIDIGetNumberOfSources() {
            let s = MIDIGetSource(i)
            if s != 0 && s != dest && MIDIPortConnectSource(port, s, nil) == noErr { connected.append(s) }
        }
        publish()
    }

    /// On CoreMIDI's thread: the words copied, and fed on the main thread in order.
    nonisolated static func received(_ list: UnsafePointer<MIDIEventList>) {
        var packets: [(UInt64, [UInt32])] = []
        let offset = MemoryLayout<MIDIEventPacket>.offset(of: \MIDIEventPacket.words) ?? 12
        for p in list.unsafeSequence() {
            let base = (UnsafeRawPointer(p) + offset).assumingMemoryBound(to: UInt32.self)
            packets.append((p.pointee.timeStamp, Array(UnsafeBufferPointer(start: base, count: Int(p.pointee.wordCount)))))
        }
        DispatchQueue.main.async {
            MainActor.assumeIsolated {
                for (ts, words) in packets { MidiInput.shared.feed(words, ns: hostNs(ts)) }
            }
        }
    }

    /// A packet's host time in ns; now when it has none or lies ahead.
    nonisolated static func hostNs(_ ts: MIDITimeStamp) -> UInt64 {
        var tb = mach_timebase_info_data_t()
        mach_timebase_info(&tb)
        let now = mach_absolute_time()
        let t = ts == 0 || ts > now ? now : ts
        let n = UInt64(tb.numer), d = UInt64(max(tb.denom, 1))
        return t / d * n + t % d * n / d
    }

    /// The translator's clock now: host time live; offline, the live
    /// player's frames at its 48 kHz.
    func now() -> UInt64 {
        guard Engine.offline else { return Self.hostNs(0) }
        return UInt64(Engine.shared.positionFrames) * 1_000_000_000 / 48_000
    }

    /// Prepare off the audio path; an invalid staged map changes neither the
    /// current translator nor the player. The caller owns a successful result.
    func prepare(_ p: PatchLines, voices: Int, maps: [String], mpe: String) throws -> OpaquePointer {
        guard maps.count == 16 else { throw InstrError("MIDI needs one map per channel.") }
        var c = fms_midi_config_t()
        c.nvoices = UInt32(max(1, min(voices, Int(FMS_MAX_VOICES))))
        c.stream = 0
        c.channels = 0xFFFF                     // omni
        c.release_ms = fms_midi_release_ms(p.text, p.fs)
        c.bend_cents = 200
        c.rpn = 1
        c.ctl_reads = fms_midi_ctl_reads(p.text, 48000)
        if p.filter_mode != 0 && p.filter_cutoff_millihz != 0 {   // CC 74 moves the armed patch's filter
            c.cutoff_millihz = p.filter_cutoff_millihz
            c.q_milli = p.filter_q_milli == 0 ? 707 : p.filter_q_milli
        }
        guard let candidate = fms_midi_create(&c) else { throw InstrError("MIDI configuration refused.") }
        for (channel, spec) in maps.enumerated() {
            var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            if fms_midi_set_ctl(candidate, UInt32(channel), spec, &err, err.count) != 0 {
                fms_midi_destroy(candidate)
                throw InstrError("MIDI channel \(channel + 1): \(String(cString: err))")
            }
        }
        var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        if fms_midi_set_mpe(candidate, mpe, &err, err.count) != 0 {
            fms_midi_destroy(candidate)
            throw InstrError("MIDI MPE: \(String(cString: err))")
        }
        return candidate
    }

    /// Install only after the matching live player has successfully opened.
    func armed(_ candidate: OpaquePointer) {
        if let tr { fms_midi_destroy(tr) }
        tr = candidate
        messages = 0; notLive = 0; dropped = 0; events = 0
        publish()
    }

    /// The piano took voice v (on) or let it go: the translator's pool weighs it.
    func external(voice v: Int, on: Bool) {
        guard let tr, Engine.shared.isLive, v >= 0 else { return }
        fms_midi_external(tr, now(), UInt32(v), on ? 1 : 0)
    }

    /// One packet's words, each UMP message through the translator.
    func feed(_ words: [UInt32], ns: UInt64) {
        let sizes = [1, 1, 1, 2, 2, 4, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4]
        var at = 0
        while at < words.count {
            messages += 1
            let word = words[at]
            if word >> 28 == 2, (word >> 20) & 15 == 11,
               (word >> 8) & 255 < 128, word & 255 < 128 {
                InstrModel.shared.learnCC(channel: Int((word >> 16) & 15), number: Int((word >> 8) & 127))
            }
            guard let tr, Engine.shared.isLive else {
                notLive += 1
                at += sizes[Int(words[at] >> 28)]
                continue
            }
            var used: UInt32 = 0
            let n = words.withUnsafeBufferPointer { w in
                out.withUnsafeMutableBufferPointer { o in
                    fms_midi_ump(tr, ns, w.baseAddress! + at, UInt32(w.count - at), o.baseAddress, UInt32(o.count), &used)
                }
            }
            for i in 0..<Int(n) { send(out[i]) }
            at += max(1, Int(used))
        }
        changed()
    }

    /// MIDI 1.0 bytes (script mode's `midi` step): one message as a UMP.
    func feedBytes(_ b: [UInt8]) {
        guard let s = b.first else { return }
        var w: [UInt32]
        if s == 0xF0 {                           // SysEx: one packet of its first 6 data bytes
            let d = Array(b.dropFirst().prefix { $0 != 0xF7 }.prefix(6)).map { UInt32($0 & 0x7F) }
            let x = d + Array(repeating: 0, count: 6 - d.count)
            w = [0x3000_0000 | UInt32(d.count) << 16 | x[0] << 8 | x[1], x[2] << 24 | x[3] << 16 | x[4] << 8 | x[5]]
        } else {
            let d1 = b.count > 1 ? UInt32(b[1]) : 0, d2 = b.count > 2 ? UInt32(b[2]) : 0
            w = [(s >= 0xF0 ? 0x1000_0000 : 0x2000_0000) | UInt32(s) << 16 | d1 << 8 | d2]
        }
        feed(w, ns: now())
    }

    private func send(_ e: fms_event_t) {
        if Engine.shared.push(e) { events += 1 } else { dropped += 1 }
        switch Int(e.type) {
        case FMS_EV_ON: InstrModel.shared.alloc.external(voice: Int(e.voice), taken: true)
        case FMS_EV_OFF: InstrModel.shared.alloc.external(voice: Int(e.voice), taken: false)
        default: break
        }
    }

    /// The live box's label in orange: something was refused or dropped.
    var warns: Bool { notLive > 0 || dropped > 0 || stats.refused > 0 }

    var stats: fms_midi_stats_t {
        var s = fms_midi_stats_t()
        if let tr { fms_midi_stats(tr, &s) }
        return s
    }

    /// What the live box says: the sources until a message comes, then the
    /// counts since the last arm; CoreMIDI's failure first.
    var label: String {
        let s = stats
        var parts: [String] = []
        if let e = setupError { parts.append(e) }
        if messages == 0 {
            parts.append(client == 0 ? "no CoreMIDI in script mode" : "\(connected.count) sources and the virtual input JotTune Synth Input")
            return "MIDI in: " + parts.joined(separator: "; ")
        }
        parts.append("\(messages) messages, \(s.notes) notes")
        if s.ctl_events > 0 { parts.append("\(s.ctl_events) ctl events") }
        if s.mpe_notes > 0 { parts.append("\(s.mpe_notes) MPE notes") }
        let ignored = s.programs + s.aftertouch + s.pressure + s.other_ccs + s.other_channels + s.system + s.unknown + s.mcm
        if ignored > 0 { parts.append("\(ignored) ignored") }
        if notLive > 0 { parts.append("\(notLive) refused: no live player (Play live or a key arms one)") }
        if s.refused > 0 { parts.append("\(s.refused) refused: 512 notes down, or a cut packet") }
        if dropped > 0 { parts.append("\(dropped) events dropped: live queue full") }
        return "MIDI in: " + parts.joined(separator: "; ")
    }

    private func changed() {
        if Engine.offline { return publish() }
        guard !publishPending else { return }
        publishPending = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
            MainActor.assumeIsolated {
                MidiInput.shared.publishPending = false
                MidiInput.shared.publish()
            }
        }
    }

    private func publish() {
        let l = label
        if shown != l { shown = l }
    }
}
