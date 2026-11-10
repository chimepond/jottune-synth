// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// InstrModel.swift: the Instrument tab's state and every control's setter,
// in one place. The views bind through these setters, and FMS_SCRIPT
// (UIScript.swift, `make uitest`) drives the same setters by name, so a
// control whose setter is lost fails the script as it fails the knob.
//
// Names: opN.FIELD (FIELD as InstrCtl.op), route.ops, route.carrier.N,
// route.w.F.T, filter.mode, filter.cutoff, filter.q, lfo.FIELD, peg.FIELD
// and fenv.FIELD (InstrCtl.stmt, ), source, name, stereo,
// fx.EFFECT and fx.EFFECT.VALUE (InstrFx.fields), send.BUS, live.cutoff,
// live.q, pos.voice, pos.x, pos.y, velocity, octave, and button.NAME for the
// buttons (InstrModel.buttons).

import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

/// The patch controls: operator fields and the route and filter controls.
enum InstrCtl {
    struct OpField {
        let label: String                          // as the editor and instrInactive name it
        let range: ClosedRange<UInt32>?            // nil: required native metadata, no fallback
        let get: (fms_op_t) -> UInt32
        let set: (inout fms_op_t, UInt32) -> Void
    }

    /// Operator fields by script key, in the editor's order.
    static let opKeys = ["ratio", "level", "index", "feedback", "vel_sens", "attack", "decay", "sustain", "release",
                         "wave", "rate_scale", "ks_break", "ks_slope", "fixed", "phase", "free_run"]
    static let op: [String: OpField] = [
        "ratio": OpField(label: "ratio", range: 0...UInt32.max, get: { $0.ratio_q16 }, set: { $0.ratio_q16 = $1 }),
        "level": OpField(label: "level", range: 0...1200, get: { $0.level_db10 }, set: { $0.level_db10 = $1 }),
        "index": OpField(label: "index", range: nil, get: { $0.index_q10 }, set: { $0.index_q10 = $1 }),
        "feedback": OpField(label: "feedback", range: nil, get: { $0.feedback_q10 }, set: { $0.feedback_q10 = $1 }),
        "vel_sens": OpField(label: "vel sens", range: 0...256, get: { $0.vel_sens_q8 }, set: { $0.vel_sens_q8 = $1 }),
        "attack": OpField(label: "attack", range: nil, get: { $0.attack_ms }, set: { $0.attack_ms = $1 }),
        "decay": OpField(label: "decay", range: nil, get: { $0.decay_ms }, set: { $0.decay_ms = $1 }),
        "sustain": OpField(label: "sustain", range: 0...1200, get: { $0.sustain_db10 }, set: { $0.sustain_db10 = $1 }),
        "release": OpField(label: "release", range: nil, get: { $0.release_ms }, set: { $0.release_ms = $1 }),
        // fm_a1.h's waves 0 to 11; the picker offers those
        "wave": OpField(label: "wave", range: 0...11, get: { $0.wave }, set: { $0.wave = $1 }),
        "rate_scale": OpField(label: "rate scale", range: nil, get: { $0.rate_scale }, set: { $0.rate_scale = $1 }),
        "ks_break": OpField(label: "ks break", range: nil, get: { $0.ks_break_millihz }, set: { $0.ks_break_millihz = $1 }),
        "ks_slope": OpField(label: "ks slope", range: 0...1200, get: { $0.ks_slope_db10 }, set: { $0.ks_slope_db10 = $1 }),
        "fixed": OpField(label: "fixed", range: nil, get: { $0.fixed_millihz }, set: { $0.fixed_millihz = $1 }),
        "phase": OpField(label: "phase", range: nil, get: { $0.phase_u16 }, set: { $0.phase_u16 = $1 }),
        // fm_a1.h : 0 key sync, 1 frozen while idle, 2 continuous
        "free_run": OpField(label: "free run", range: 0...2, get: { $0.free_run }, set: { $0.free_run = $1 }),
    ]

    static func clamp(_ v: Int, _ r: ClosedRange<UInt32>) -> UInt32 {
        UInt32(min(max(Int64(v), Int64(r.lowerBound)), Int64(r.upperBound)))
    }

    /// One field of the lfo, peg or fenv statement .
    struct StmtField {
        let label: String
        let range: ClosedRange<Int>?               // nil: required native metadata, no fallback
        let unit: String
        let get: (PatchLines) -> Int
        let set: (inout PatchLines, Int) -> Void
    }

    /// The statement fields by script name, in the editor's order.
    static let lfoKeys = ["lfo.rate", "lfo.wave", "lfo.delay", "lfo.fade", "lfo.pitch", "lfo.mask_pitch",
                          "lfo.level", "lfo.mask_level", "lfo.index", "lfo.mask_index", "lfo.phase", "lfo.free_run"]
    static let pegKeys = ["peg.l1", "peg.t1", "peg.l2", "peg.t2", "peg.l3", "peg.t3", "peg.l4", "peg.t4", "peg.mask"]
    static let fenvKeys = ["fenv.depth", "fenv.attack", "fenv.decay", "fenv.sustain", "fenv.release",
                           "fenv.track", "fenv.ref"]
    static let opMask = (1 << Int(FMS_OPS)) - 1

    private static func lfo(_ label: String, _ r: ClosedRange<Int>, _ unit: String,
                            _ kp: WritableKeyPath<fms_lfo_t, UInt32>) -> StmtField {
        StmtField(label: label, range: r, unit: unit, get: { Int($0.lfo[keyPath: kp]) },
                  set: { $0.lfo[keyPath: kp] = UInt32(min(max($1, r.lowerBound), r.upperBound)) })
    }

    private static func nativeLfo(_ label: String, _ unit: String,
                                  _ kp: WritableKeyPath<fms_lfo_t, UInt32>) -> StmtField {
        StmtField(label: label, range: nil, unit: unit, get: { Int($0.lfo[keyPath: kp]) },
                  set: { $0.lfo[keyPath: kp] = UInt32($1) })
    }

    private static func fenv(_ label: String, _ unit: String,
                             _ kp: WritableKeyPath<fms_fenv_t, UInt32>) -> StmtField {
        StmtField(label: label, range: nil, unit: unit, get: { Int($0.fenv[keyPath: kp]) },
                  set: { $0.fenv[keyPath: kp] = UInt32($1) })
    }

    private static func pegLevel(_ k: Int) -> StmtField {
        StmtField(label: "L\(k + 1)", range: nil, unit: "cents",
                  get: { p in withUnsafeBytes(of: p.peg.level_cents) { Int($0.load(fromByteOffset: k * 4, as: Int32.self)) } },
                  set: { p, v in
                      withUnsafeMutableBytes(of: &p.peg.level_cents) {
                          $0.storeBytes(of: Int32(v), toByteOffset: k * 4, as: Int32.self)
                      }
                  })
    }

    private static func pegTime(_ k: Int) -> StmtField {
        StmtField(label: "T\(k + 1)", range: nil, unit: "ms",
                  get: { p in withUnsafeBytes(of: p.peg.time_ms) { Int($0.load(fromByteOffset: k * 4, as: UInt32.self)) } },
                  set: { p, v in
                      withUnsafeMutableBytes(of: &p.peg.time_ms) {
                          $0.storeBytes(of: UInt32(v), toByteOffset: k * 4, as: UInt32.self)
                      }
                  })
    }

    static let stmt: [String: StmtField] = {
        var d: [String: StmtField] = [
            // fm_a1.h: rate 0 is no LFO, else the library clamps to 10 .. 50000
            "lfo.rate": StmtField(label: "rate", range: 0...50000, unit: "mHz", get: { Int($0.lfo.rate_millihz) },
                                  set: { $0.lfo.rate_millihz = $1 <= 0 ? 0 : UInt32(min(max($1, 10), 50000)) }),
            "lfo.wave": lfo("wave", 0...6, "", \.wave),
            "lfo.delay": nativeLfo("delay", "ms", \.delay_ms),
            "lfo.fade": nativeLfo("fade", "ms", \.fade_ms),
            "lfo.pitch": nativeLfo("pitch", "cents", \.pitch_cents),
            "lfo.level": nativeLfo("level", "dB10", \.level_db10),
            "lfo.index": nativeLfo("index", "Q10", \.index_q10),
            "lfo.phase": nativeLfo("phase", "/65536", \.phase_u16),
            "lfo.mask_pitch": lfo("pitch ops", 0...opMask, "bits", \.mask_pitch),
            "lfo.mask_level": lfo("level ops", 0...opMask, "bits", \.mask_level),
            "lfo.mask_index": lfo("index ops", 0...opMask, "bits", \.mask_index),
            // fm_a1.h : 2 continuous, other values key sync; the text takes only 0 or 2
            "lfo.free_run": StmtField(label: "free run", range: 0...2, unit: "", get: { Int($0.lfo.free_run) },
                                      set: { $0.lfo.free_run = $1 == 2 ? 2 : 0 }),
            "peg.mask": StmtField(label: "ops", range: 0...opMask, unit: "bits", get: { Int($0.peg.mask) },
                                  set: { $0.peg.mask = UInt32(min(max($1, 0), opMask)) }),
            "fenv.attack": fenv("attack", "ms", \.attack_ms),
            "fenv.decay": fenv("decay", "ms", \.decay_ms),
            "fenv.sustain": fenv("sustain", "/1000", \.sustain_permille),
            "fenv.release": fenv("release", "ms", \.release_ms),
            "fenv.depth": StmtField(label: "depth", range: nil, unit: "cents", get: { Int($0.fenv.depth_cents) },
                                    set: { $0.fenv.depth_cents = Int32($1) }),
            "fenv.track": fenv("track", "/1000", \.track_permille),
            "fenv.ref": StmtField(label: "ref", range: 0...Int(UInt32.max), unit: "mHz", get: { Int($0.fenv.ref_millihz) },
                                  set: { $0.fenv.ref_millihz = UInt32(min(max($1, 0), Int(UInt32.max))) }),
        ]
        for k in 0..<4 { d["peg.l\(k + 1)"] = pegLevel(k); d["peg.t\(k + 1)"] = pegTime(k) }
        return d
    }()

    /// Sets one patch control by name; false for an unknown name.
    @discardableResult
    static func setPatch(_ p: inout PatchLines, _ name: String, _ v: Int) -> Bool {
        let t = name.split(separator: ".").map(String.init)
        if t.count == 2, t[0].hasPrefix("op"), let i = Int(t[0].dropFirst(2)), (0..<Int(FMS_OPS)).contains(i),
           let f = op[t[1]] {
            let range: ClosedRange<UInt32>
            if let legacy = f.range { range = legacy }
            else {
                guard let field = p.operatorField(t[1]) else { return false }
                range = UInt32(field.range.lowerBound)...UInt32(field.range.upperBound)
            }
            var o = opArray(p)[i]
            f.set(&o, clamp(v, range))
            setOp(&p, i, o)
            p.validate()
            return true
        }
        if let f = stmt[name] {
            if f.range == nil {
                guard let field = p.statementField(name) else { return false }
                f.set(&p, min(max(v, field.range.lowerBound), field.range.upperBound))
            } else { f.set(&p, v) }                 // preserve aliases and enum policies
            p.validate()
            return true
        }
        switch (t.count, t.first ?? "") {
        case (2, "route") where t[1] == "ops":
            setRouteOps(&p, [0, 2, 3, 4].contains(v) ? v : 0)
        case (3, "route") where t[1] == "carrier":
            guard let i = Int(t[2]), (0..<Int(FMS_OPS)).contains(i) else { return false }
            let bit = UInt32(1) << UInt32(i)
            let m = v != 0 ? p.route_carriers | bit : p.route_carriers & ~bit
            if m != 0 { p.route_carriers = m }          // a route keeps one carrier at least
        case (4, "route") where t[1] == "w":
            guard let f = Int(t[2]), let to = Int(t[3]) else { return false }
            setRouteWeight(&p, f, to, UInt32(min(max(v, 0), 1024)))
        case (2, "filter") where t[1] == "mode":
            let m = min(max(v, 0), 4)
            p.filter_mode = UInt32(m)
            if m != 0 && p.filter_cutoff_millihz == 0 { p.filter_cutoff_millihz = 2_000_000 }
            if m != 0 && p.filter_q_milli == 0 { p.filter_q_milli = 707 }
        case (2, "filter") where t[1] == "cutoff" || t[1] == "q":
            guard let field = p.filterField(name) else { return false }
            let value = clamp(v, UInt32(field.range.lowerBound)...UInt32(field.range.upperBound))
            if t[1] == "cutoff" { p.filter_cutoff_millihz = value }
            else { p.filter_q_milli = value }
        default:
            return false
        }
        p.validate()
        return true
    }

    static func getPatch(_ p: PatchLines, _ name: String) -> Int? {
        let t = name.split(separator: ".").map(String.init)
        if t.count == 2, t[0].hasPrefix("op"), let i = Int(t[0].dropFirst(2)), (0..<Int(FMS_OPS)).contains(i),
           let f = op[t[1]] {
            return Int(f.get(opArray(p)[i]))
        }
        if let f = stmt[name] { return f.get(p) }
        switch (t.count, t.first ?? "") {
        case (2, "route") where t[1] == "ops": return Int(p.route_ops)
        case (3, "route") where t[1] == "carrier":
            guard let i = Int(t[2]) else { return nil }
            return p.route_carriers & (UInt32(1) << UInt32(i)) != 0 ? 1 : 0
        case (4, "route") where t[1] == "w":
            guard let f = Int(t[2]), let to = Int(t[3]) else { return nil }
            return Int(routeWeight(p, f, to))
        case (2, "filter") where t[1] == "mode": return Int(p.filter_mode)
        case (2, "filter") where t[1] == "cutoff": return Int(p.filter_cutoff_millihz)
        case (2, "filter") where t[1] == "q": return Int(p.filter_q_milli)
        default: return nil
        }
    }

    /// The route's operator count: 0 (the fixed 2-op path), 2, 3 or 4.
    static func setRouteOps(_ p: inout PatchLines, _ n: Int) {
        let was = Int(p.route_ops)
        if n == was { return }
        let defined = n == 0 ? 2 : n
        for i in 0..<defined where p.ops_defined & (1 << UInt32(i)) == 0 {
            setOp(&p, i, InstrPatchIO.plainOp())
        }
        p.ops_defined = (1 << UInt32(defined)) - 1
        p.route_ops = UInt32(n)
        for f in 0..<Int(FMS_OPS) {
            for t in 0..<Int(FMS_OPS) where n == 0 || f >= n || t >= n { setRouteWeight(&p, f, t, 0) }
        }
        if n == 0 {
            p.route_carriers = 0
        } else {
            p.route_carriers &= (1 << UInt32(n)) - 1
            if p.route_carriers == 0 { p.route_carriers = 1 }
            if was == 0 { setRouteWeight(&p, 1, 0, 256) }   // what the fixed path did
        }
    }
}

extension InstrFx {
    /// The effect toggles and values by script name (fx.EFFECT, fx.EFFECT.VALUE),
    /// with the ranges that the harness checks for each value.
    static let toggles: [String: WritableKeyPath<InstrFx, Bool>] = [
        "dcblock": \.dcOn, "chorus": \.chorusOn, "delay": \.delayOn, "reverb": \.reverbOn,
        "limiter": \.limiterOn, "softclip": \.softclip, "gain": \.gainOn, "dither": \.ditherOn,
        "pingpong": \.pingpongOn, "compressor": \.compressorOn,
        "flanger": \.flangerOn, "decodable": \.decodable, "crush": \.crushOn,
    ]
    static let values: [String: (WritableKeyPath<InstrFx, UInt32>, ClosedRange<UInt32>)] = [
        "dcblock.fc": (\.dcFc, 1...100_000),
        "chorus.base": (\.chBase, 7000...25000), "chorus.depth": (\.chDepth, 1000...5000),
        "chorus.rate": (\.chRate, 100...3000),
        "flanger.base": (\.flBase, 100...10000), "flanger.depth": (\.flDepth, 100...10000),
        "flanger.rate": (\.flRate, 0...2000), "flanger.feedback": (\.flFeedback, 0...29491),
        "flanger.invert": (\.flInvert, 0...1),
        "delay.time": (\.dlTime, 50_000...1_000_000), "delay.feedback": (\.dlFb, 0...29491),
        "delay.lowpass": (\.dlLp, 20...20000),
        "pingpong.time": (\.ppTime, 50_000...1_000_000), "pingpong.feedback": (\.ppFb, 0...29491),
        "pingpong.lowpass": (\.ppLp, 20...20000), "pingpong.highpass": (\.ppHp, 0...2000),
        "reverb.room": (\.rvRoom, 0...1000), "reverb.damp": (\.rvDamp, 0...1000),
        "compressor.threshold": (\.compThr, 0...600), "compressor.ratio": (\.compRatio, 10...200),
        "compressor.attack": (\.compAttack, 0...100), "compressor.release": (\.compRelease, 1...5000),
        "compressor.makeup": (\.compMakeup, 0...240),
        "limiter.threshold": (\.lmThr, 0...200), "limiter.release": (\.lmRel, 1...5000),
        "dither.seed": (\.ditherSeed, 1...UInt32.max),
    ]
    static let signedValues: [String: (WritableKeyPath<InstrFx, Int>, ClosedRange<Int>)] = [
        "gain.db10": (\.gainDb10, -600...240),
    ]
}

/// Header values, not voice positions or recorded choreography. Optional
/// presence survives separately from editable values .
struct InstrSpace: Equatable, Sendable, Codable {
    static let fields = ["off", "stereo", "binaural", "amb1", "amb3"]
    var field = 0, block = 64, near = 100, order = 3
    var orderPresent = false, panlawPresent = false, pinnaPresent = false, yawPresent = false
    var panlaw = 32768, pinna = 0, coefficients = [0, 0, 0, 0, 0], yaw = 0

    init() {}
    init(native s: fms_space_t) {
        field = Int(s.field); block = Int(s.block); near = Int(s.near_mm); order = Int(s.order)
        orderPresent = s.present & UInt32(FMS_SPACE_ORDER) != 0
        panlawPresent = s.present & UInt32(FMS_SPACE_PANLAW) != 0
        pinnaPresent = s.present & UInt32(FMS_SPACE_PINNA) != 0
        yawPresent = s.present & UInt32(FMS_SPACE_YAW) != 0
        if panlawPresent { panlaw = Int(s.panlaw_q15) }
        pinna = Int(s.pinna); yaw = Int(s.yaw_mdeg)
        coefficients = [s.pinna_d.0, s.pinna_d.1, s.pinna_d.2, s.pinna_d.3, s.pinna_d.4].map(Int.init)
    }

    /// Persisted metadata must be safe to project before native validation.
    /// This checks representation/indexing only, not a second header grammar.
    var safelyRepresentable: Bool {
        Self.fields.indices.contains(field) && (0...3).contains(pinna) && coefficients.count == 5 &&
        [block, near, order, panlaw].allSatisfy { UInt32(exactly: $0) != nil } &&
        coefficients.allSatisfy { UInt32(exactly: $0) != nil } && Int32(exactly: yaw) != nil
    }

    var fieldName: String { Self.fields.indices.contains(field) ? Self.fields[field] : "invalid" }
    var native: fms_space_t {
        var s = fms_space_t(); fms_space_defaults(&s)
        s.field = UInt32(field); s.block = UInt32(block); s.near_mm = UInt32(near); s.order = UInt32(order)
        s.panlaw_q15 = panlawPresent ? UInt32(panlaw) : 0; s.pinna = pinnaPresent ? UInt32(pinna) : 0
        if pinnaPresent && pinna == 3 { s.pinna_d = (UInt32(coefficients[0]), UInt32(coefficients[1]), UInt32(coefficients[2]), UInt32(coefficients[3]), UInt32(coefficients[4])) }
        s.yaw_mdeg = yawPresent ? Int32(yaw) : 0
        s.present = (orderPresent ? UInt32(FMS_SPACE_ORDER) : 0) | (panlawPresent ? UInt32(FMS_SPACE_PANLAW) : 0) |
                    (pinnaPresent ? UInt32(FMS_SPACE_PINNA) : 0) | (yawPresent ? UInt32(FMS_SPACE_YAW) : 0)
        return s
    }

    func value(_ key: String) -> String {
        switch key {
        case "field": return fieldName
        case "block": return String(block)
        case "near": return String(near)
        case "order": return orderPresent ? String(order) : "0"
        case "panlaw.enabled": return panlawPresent ? "1" : "0"
        case "panlaw": return String(panlaw)
        case "pinna": return pinnaPresent ? ["off", "pbnh", "rd", "custom"][pinna] : "absent"
        case "yaw.enabled": return yawPresent ? "1" : "0"
        case "yaw": return String(yaw)
        default:
            if key.hasPrefix("pinna."), let i = Int(key.dropFirst(6)), coefficients.indices.contains(i) { return String(coefficients[i]) }
            return ""
        }
    }

    mutating func set(_ key: String, _ text: String) -> String? {
        if key == "field" {
            guard let f = Self.fields.firstIndex(of: text), f <= 2 else { return "Live space takes off, stereo or binaural; ambisonic records play in Records." }
            field = f; return nil
        }
        if key == "pinna" {
            if text == "absent" { pinnaPresent = false; return nil }
            guard let p = ["off", "pbnh", "rd", "custom"].firstIndex(of: text) else { return "Pinna takes absent, off, pbnh, rd or custom." }
            pinnaPresent = true; pinna = p; return nil
        }
        guard let v = Int(text) else { return "Space \(key) takes an integer in native units." }
        switch key {
        case "block": guard [32, 64, 128].contains(v) else { return "Block takes 32, 64 or 128 frames." }; block = v
        case "near": guard (1...16777216).contains(v) else { return "Near takes 1...16777216 mm." }; near = v
        case "order": guard [0, 1, 3].contains(v) else { return "Order takes native default (0), 1 or 3." }; orderPresent = v != 0; order = v == 0 ? 3 : v
        case "panlaw.enabled": guard (0...1).contains(v) else { return "Panlaw enabled takes 0 or 1." }; panlawPresent = v != 0
        case "panlaw": guard (1...32768).contains(v) else { return "Panlaw takes 1...32768 Q15." }; panlaw = v
        case "yaw.enabled": guard (0...1).contains(v) else { return "Yaw enabled takes 0 or 1." }; yawPresent = v != 0
        case "yaw": guard (-360000...360000).contains(v) else { return "Yaw takes -360000...360000 millidegrees." }; yaw = v
        default:
            guard key.hasPrefix("pinna."), let i = Int(key.dropFirst(6)), coefficients.indices.contains(i), (0...1000).contains(v) else { return "Unknown space control or pinna coefficient outside 0...1000." }
            coefficients[i] = v
        }
        return nil
    }
}

/// Initial metadata read before a player is handed to the callback. FX text
/// is already canonical and accepted by native code; this is a projection,
/// not another validator. Unrepresented rows remain in the emitted header.
struct InstrHeader: Sendable {
    var space: InstrSpace
    var fx: InstrFx

    static func read(_ player: OpaquePointer) -> InstrHeader? {
        var native = fms_space_t(); fms_player_space(player, &native)
        let n = fms_player_fx_text(player, nil, 0)
        guard n > 0 else { return nil }
        var text = [CChar](repeating: 0, count: n)
        guard fms_player_fx_text(player, &text, n) == n else { return nil }
        return InstrHeader(space: InstrSpace(native: native), fx: projectFX(String(cString: text)))
    }

    static func projectFX(_ text: String) -> InstrFx {
        let columns: [String: [String]] = [
            "dcblock": ["fc"], "chorus": ["base", "depth", "rate"],
            "flanger": ["base", "depth", "rate", "feedback", "invert"], "decodable": [],
            "delay": ["time", "feedback", "lowpass"], "pingpong": ["time", "feedback", "lowpass", "highpass"],
            "reverb": ["room", "damp"], "compressor": ["threshold", "ratio", "attack", "release", "makeup"],
            "limiter": ["threshold", "release"],
            "softclip": [], "gain": ["db10"], "dither": ["seed"], "crush": []]
        var fx = InstrFx()
        for row in text.split(separator: "\n") {
            let t = row.split(whereSeparator: { $0.isWhitespace }).map(String.init)
            guard let family = t.first, let fields = columns[family], t.count == fields.count + 1,
                  let toggle = InstrFx.toggles[family] else { fx.unrepresented.append(String(row)); continue }
            var candidate = fx, represented = true
            for (i, field) in fields.enumerated() {
                let key = family + "." + field
                if let (path, _) = InstrFx.values[key], let value = UInt32(t[i + 1]) { candidate[keyPath: path] = value }
                else if let (path, _) = InstrFx.signedValues[key], let value = Int(t[i + 1]) { candidate[keyPath: path] = value }
                else { represented = false; break }
            }
            if represented { candidate[keyPath: toggle] = true; fx = candidate }
            else { fx.unrepresented.append(String(row)) }
        }
        return fx
    }
}

/// A preference is an explicit field choice, never an imported record value.
/// Offline scripts pass nil; isolated tests supply their own defaults suite.
struct InstrFieldPreference {
    static let key = "fmspike.explicit-space-field"
    private let defaults: UserDefaults?
    private(set) var field: Int

    init(defaults: UserDefaults?) {
        self.defaults = defaults
        let stored = defaults?.string(forKey: Self.key)
        field = stored.flatMap { ["off", "stereo", "binaural"].firstIndex(of: $0) } ?? Int(FMS_SPACE_STEREO)
    }

    mutating func remember(_ value: Int) {
        guard (0...2).contains(value) else { return }
        field = value
        defaults?.set(InstrSpace.fields[value], forKey: Self.key)
    }
}

/// A favorite stores a file identity and Space header, never patch/event bytes.
struct InstrFavorite: Codable, Equatable {
    var path: String
    var index: Int
    var name: String
    var space: InstrSpace
    var safelyRepresentable: Bool {
        path.hasPrefix("/") && !path.utf8.contains(0) &&
        URL(fileURLWithPath: path).standardizedFileURL.path == path &&
        index >= 0 && UInt32(exactly: index) != nil && !name.isEmpty && space.safelyRepresentable
    }
}

/// Versioned host preferences; offline scripts keep this store in memory.
struct InstrFavorites {
    static let key = "fmspike.favorites.v1"
    private struct Envelope: Codable { var version = 1; var entries: [InstrFavorite] }
    private let defaults: UserDefaults?
    private(set) var entries: [InstrFavorite] = []
    private(set) var error: String?

    init(defaults: UserDefaults?) {
        self.defaults = defaults
        guard let object = defaults?.object(forKey: Self.key) else { return }
        do {
            guard let data = object as? Data else { throw InstrError("wrong stored type") }
            let decoded = try JSONDecoder().decode(Envelope.self, from: data)
            guard decoded.version == 1 else { throw InstrError("unsupported version") }
            guard decoded.entries.allSatisfy(\.safelyRepresentable) else { throw InstrError("unsafe stored values") }
            entries = decoded.entries
        } catch {
            self.error = "Favorites could not be read (\(error)). Stored data is preserved. Add or Remove unreadable favorites explicitly replaces it."
        }
    }

    private mutating func save(_ candidate: [InstrFavorite]) -> String? {
        do {
            let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
            let bytes = try encoder.encode(Envelope(entries: candidate))
            defaults?.set(bytes, forKey: Self.key)
            entries = candidate; error = nil; return nil
        } catch { return "Favorites could not be saved: \(error)" }
    }
    mutating func add(_ entry: InstrFavorite) -> String? {
        guard entry.safelyRepresentable else { return "Favorite metadata cannot be represented safely." }
        return save([entry] + entries.filter { $0.path != entry.path || $0.index != entry.index })
    }
    mutating func remove(_ index: Int) -> String? {
        guard entries.indices.contains(index) else { return "Favorite index is outside the list." }
        var next = entries; next.remove(at: index); return save(next)
    }
    mutating func removeUnreadable() -> String? {
        guard error != nil else { return "There are no unreadable favorites to remove." }
        return save([])
    }
}

struct InstrLearnRequest: Equatable { var slot: Int; var input: Int }
struct InstrLearnControls {
    var inputs: [Int]
    var pending: InstrLearnRequest?
    var message: String
    var select: (Int, Int) -> Void
    var begin: (Int) -> Void
    var cancel: () -> Void
}

/// Only patch statements and their staged display name belong to this journal.
/// Diagnostics are derived again on replay; disabled tokens and fs are retained.
struct InstrPatchState: Equatable {
    var patch: PatchLines
    var name: String
    static func == (a: Self, b: Self) -> Bool {
        a.patch.rows == b.patch.rows && a.patch.fs == b.patch.fs && a.name == b.name
    }
}

struct InstrPatchJournal {
    struct Edit { let before: InstrPatchState; let after: InstrPatchState }
    private(set) var undo: [Edit] = []
    private(set) var redo: [Edit] = []
    mutating func record(_ before: InstrPatchState, _ after: InstrPatchState) {
        guard before != after else { return }
        undo.append(Edit(before: before, after: after)); redo.removeAll()
        if undo.count > 64 { undo.removeFirst(undo.count - 64) }
    }
    mutating func back() -> InstrPatchState? {
        guard let edit = undo.popLast() else { return nil }
        redo.append(edit); return edit.before
    }
    mutating func forward() -> InstrPatchState? {
        guard let edit = redo.popLast() else { return nil }
        undo.append(edit); return edit.after
    }
}

@MainActor final class InstrModel: ObservableObject {
    static let shared = InstrModel()

    let voices = 8
    let alloc = VoiceAllocator(voices: 8)
    let recipes = RecipeLibrary()
    var engine: Engine { Engine.shared }

    @Published var patch = InstrPatchIO.defaultPatch() {
        didSet { recordPatchEdit(InstrPatchState(patch: oldValue, name: patchName)) }
    }
    @Published var patchName = "default" {
        didSet { recordPatchEdit(InstrPatchState(patch: patch, name: oldValue)) }
    }
    @Published private(set) var patchJournal = InstrPatchJournal()
    private var replayingPatch = false
    @Published private(set) var compareSlots: [InstrPatchState] = []
    @Published private(set) var compareSelected = 0
    @Published private(set) var source: InstrSource = .builtin
    @Published private(set) var origin: PatchOrigin?
    @Published private(set) var fileStatus = ""
    @Published private(set) var fileBusy = false
    @Published private(set) var recentPaths: [String] = []
    @Published private(set) var favoriteStore: InstrFavorites
    var favorites: [InstrFavorite] { favoriteStore.entries }
    private let recentDefaults: UserDefaults?
    private static let recentKey = "fmspike.recent-patches"
    private var recPaths: [String] = []
    private var recFiles: [Data]?
    private var recHeader: InstrHeader?
    private var selectedHeader: InstrHeader?
    @Published private(set) var recordValues = true
    private var fieldPreference: InstrFieldPreference
    private var preferredField: Int { fieldPreference.field }
    @Published var loadNote: String?
    @Published var dropNote: String?          // what the loaded patch lost (instrDropLabel)

    @Published var fx = InstrFx()
    @Published var space = InstrSpace()
    var stereo: Bool {
        get { space.field == Int(FMS_SPACE_STEREO) }
        set { space.field = Int(newValue ? FMS_SPACE_STEREO : FMS_SPACE_OFF); fieldPreference.remember(space.field) }
    }
    @Published var velocity: Double = 26000
    @Published var octaveBase = 60
    @Published var midiMaps = Array(repeating: "off", count: 16)
    @Published var midiMPE = "off"
    @Published private(set) var learnInputs = Array(repeating: 0, count: 8)
    @Published private(set) var learnRequest: InstrLearnRequest?
    @Published private(set) var learnStatus = ""


    // what the live player was armed with
    @Published private(set) var armedFx: InstrFx?
    @Published private(set) var armedPatch: PatchLines?
    @Published private(set) var armedSpace: InstrSpace?
    var armedStereo: Bool { armedSpace?.field == Int(FMS_SPACE_STEREO) }
    var armedSpatial: Bool { (armedSpace?.field ?? 0) != Int(FMS_SPACE_OFF) }
    @Published private(set) var armedMidiMaps = Array(repeating: "off", count: 16)
    @Published private(set) var armedMidiMPE = "off"

    @Published private(set) var sends = Array(repeating: 0.0, count: InstrModel.busNames.count)
    @Published private(set) var liveCutoff: Double = 2_000_000
    @Published private(set) var liveQ: Double = 707
    @Published private(set) var liveCrushBits = 24
    @Published private(set) var liveCrushRateDiv = 1
    private var crusherBeforePendingRecord: (bits: Int, rateDiv: Int)?
    @Published var posTarget = -1          // -1: every voice
    @Published private(set) var pos = CGPoint(x: 0, y: 1000)
    @Published private(set) var posZ = 0
    @Published private(set) var moveDuration: UInt64 = 0
    @Published private(set) var liveYaw = 0

    @Published var takeText = ""
    @Published var takeVerify = ""
    @Published private(set) var takeSave = ""
    @Published var copied = false

    // the last record's patches (the live player replaces Engine.patches)
    @Published private(set) var recTitle = ""
    @Published private(set) var recPatches: [PatchLines] = []
    @Published private(set) var recNames: [String] = []
    @Published private(set) var recDropped: [String] = []

    /// FMS_SCRIPT's positive control: the setter of this control name does
    /// nothing, as if its binding were removed by hand (`make uitest`).
    let dropped = ProcessInfo.processInfo.environment["FMS_UITEST_DROP"] ?? ""

    init() {
        // Offline scripts exercise an isolated in-memory list, never the
        // owner's recent files. Normal app launches persist at most ten.
        recentDefaults = Engine.offline ? nil : .standard
        favoriteStore = InstrFavorites(defaults: recentDefaults)
        fieldPreference = InstrFieldPreference(defaults: recentDefaults)
        space.field = fieldPreference.field
        recentPaths = Array((recentDefaults?.stringArray(forKey: Self.recentKey) ?? []).prefix(10))
        resetCompare()
        alloc.autoArm = { [unowned self] in self.arm() }
        engine.onTake = { [unowned self] t, started in
            // Cancellation before callback claim keeps the old native pair.
            // A newer explicit edit/arm clears the backup and owns its values.
            if !started, let prior = self.crusherBeforePendingRecord {
                self.liveCrushBits = prior.bits; self.liveCrushRateDiv = prior.rateDiv
            }
            self.crusherBeforePendingRecord = nil
            self.takeText = t; self.takeVerify = ""
        }
        MidiInput.shared.start()
    }

    /// What Copy writes to: the clipboard, or under FMS_SCRIPT a private
    /// pasteboard, so `make uitest` leaves the clipboard alone.
    static var board: NSPasteboard {
        Engine.offline ? NSPasteboard(name: NSPasteboard.Name("fmspike.uitest")) : .general
    }

    static let busNames = ["chorus", "delay", "reverb", "duck", "flanger"]
    static let buttons = ["play_live", "apply", "pause", "resume", "record", "stop", "verify_take", "save",
                          "copy", "oct_down", "oct_up", "rescan"]

    var patchState: InstrPatchState { InstrPatchState(patch: patch, name: patchName) }
    private func recordPatchEdit(_ before: InstrPatchState) {
        guard !replayingPatch, dropped != "patch.journal" else { return }
        patchJournal.record(before, patchState)
    }
    func resetPatchHistory() { patchJournal = InstrPatchJournal() }
    private func replayPatch(_ state: InstrPatchState) {
        replayingPatch = true
        patch = state.patch; patchName = state.name; patch.validate()
        replayingPatch = false
    }
    /// Source replacement starts a new in-memory journal. It does not arm.
    private func replaceSourcePatch(_ p: PatchLines, name: String) {
        cancelLearn()
        replayingPatch = true
        patch = p; patchName = name
        replayingPatch = false; resetPatchHistory(); resetCompare()
    }
    private func resetCompare() {
        compareSlots = [patchState, patchState]; compareSelected = 0
    }
    func storeCompare(_ side: Int) {
        guard compareSlots.indices.contains(side) else { return }
        compareSlots[side] = patchState
    }
    var compareDiff: String {
        guard compareSlots.count == 2 else { return "" }
        let a = compareSlots[0], b = compareSlots[1]
        var lines: [String] = []
        if a.name != b.name { lines += ["A name: " + a.name, "B name: " + b.name] }
        if a.patch.fs != b.patch.fs { lines += ["A fs: \(a.patch.fs)", "B fs: \(b.patch.fs)"] }
        for i in 0..<max(a.patch.rows.count, b.patch.rows.count) {
            let x = a.patch.rows.indices.contains(i) ? a.patch.rows[i] : nil
            let y = b.patch.rows.indices.contains(i) ? b.patch.rows[i] : nil
            guard x != y else { continue }
            func row(_ r: PatchLines.Line?) -> String {
                guard let r else { return "(absent)" }
                return (r.enabled ? "" : "[disabled] ") + r.text
            }
            lines += ["A \(i + 1): " + row(x), "B \(i + 1): " + row(y)]
        }
        if let d = a.patch.diagnostic { lines.append("A refused: " + d) }
        if let d = b.patch.diagnostic { lines.append("B refused: " + d) }
        return lines.isEmpty ? "Stored A and B have identical patch statements." : lines.joined(separator: "\n")
    }
    /// A static displayed placement, never a mutable active-player read.
    /// Moves freeze here; neither their remaining path nor their phase survives.
    func compareGeometry() -> [fms_event_t] {
        let current = engine.isLive ? engine.voicePos : []
        let fallback = SIMD3<Int32>(0, space.field == Int(FMS_SPACE_OFF) ? 0 : 1000, 0)
        var events = (0..<voices).map { v -> fms_event_t in
            let p = current.indices.contains(v) ? current[v] : fallback
            var e = fms_event_t(); e.type = UInt32(FMS_EV_POS); e.voice = UInt32(v)
            e.x = p.x; e.y = p.y; e.z = p.z; return e
        }
        if engine.isLive, armedSpace?.field == Int(FMS_SPACE_BINAURAL), space.field == Int(FMS_SPACE_BINAURAL) {
            var e = fms_event_t(); e.type = UInt32(FMS_EV_YAW); e.x = Int32(liveYaw); events.append(e)
        }
        return events
    }
    func swapCompare() {
        guard compareSlots.count == 2 else { return }
        cancelLearn()
        let geometry = compareGeometry(), before = patchState
        compareSelected = 1 - compareSelected
        replayPatch(compareSlots[compareSelected]); recordPatchEdit(before)
        origin = nil
        arm(geometry: geometry)
    }
    @discardableResult func undoPatch() -> String? {
        guard let state = patchJournal.back() else { return "There is no patch edit to undo." }
        replayPatch(state); return nil
    }
    @discardableResult func redoPatch() -> String? {
        guard let state = patchJournal.forward() else { return "There is no patch edit to redo." }
        replayPatch(state); return nil
    }

    func selectLearnInput(_ slot: Int, _ input: Int) {
        guard learnRequest == nil, learnInputs.indices.contains(slot), (0..<8).contains(input) else { return }
        learnInputs[slot] = input
    }
    @discardableResult func startLearn(_ slot: Int) -> String? {
        guard learnRequest == nil else { return "Cancel the pending Learn request first." }
        guard learnInputs.indices.contains(slot), patch.fields.contains(where: { $0.id == "mod.\(slot).source" }) else {
            return "Learn needs an existing enabled mod slot."
        }
        learnRequest = InstrLearnRequest(slot: slot, input: learnInputs[slot])
        learnStatus = "Waiting for one MIDI 1 CC for slot \(slot), ctl\(learnInputs[slot]). Cancel or select another patch to stop."
        return nil
    }
    func cancelLearn() { learnRequest = nil; learnStatus = "" }

    /// Called once at the shared input boundary, before the old translator.
    /// The first eligible CC consumes the request even when validation refuses.
    func learnCC(channel: Int, number: Int) {
        guard let request = learnRequest else { return }
        learnRequest = nil
        do {
            guard midiMaps.indices.contains(channel), (0...127).contains(number) else { throw InstrError("Invalid CC/channel.") }
            var next = patch, maps = midiMaps
            guard next.setField("mod.\(request.slot).source", "ctl\(request.input)") else {
                throw InstrError("The selected mod slot is no longer enabled or present.")
            }
            if let error = next.diagnostic { throw InstrError(error) }
            let original = maps[channel], trimmed = original.trimmingCharacters(in: .whitespacesAndNewlines)
            let items = trimmed.split(separator: ",", omittingEmptySubsequences: false).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            guard !items.contains("default") else { throw InstrError("Edit or clear the default MIDI map before Learn; it is never expanded or replaced automatically.") }
            // Recognize only an identical CC item. Native parsing below still
            // validates the complete unchanged/extended map and every conflict.
            let identical = items.contains { item in
                let pair = item.split(separator: ":", omittingEmptySubsequences: false)
                guard pair.count == 2, pair[0].hasPrefix("cc") else { return false }
                let digits = pair[0].dropFirst(2)
                return !digits.isEmpty && digits.allSatisfy { $0 >= "0" && $0 <= "9" } &&
                       Int(digits) == number && pair[1] == String(request.input)
            }
            if !identical {
                let item = "cc\(number):\(request.input)"
                maps[channel] = trimmed.isEmpty || trimmed == "off" ? item : original + "," + item
            }
            let candidate = try MidiInput.shared.prepare(next, voices: voices, maps: maps, mpe: midiMPE)
            fms_midi_destroy(candidate)
            if dropped != "learn.commit" { patch = next; midiMaps = maps }
            learnStatus = "Learned channel \(channel + 1) CC \(number) → ctl\(request.input), slot \(request.slot). Apply to install. Patch Undo does not undo the MIDI map."
            engine.errorText = nil
        } catch {
            learnStatus = "Learn refused: \(error). Press Learn to try another CC."
            engine.errorText = learnStatus
        }
    }

    // ---- by name: what the views and FMS_SCRIPT call ----

    /// Sets a control by name; nil when done, else why not.
    @discardableResult
    func set(_ name: String, _ token: String) -> String? {
        if name == dropped { return nil }
        let v = Int(token)
        if name == "values" {
            guard ["record", "defaults"].contains(token) else { return "Import values takes record or defaults." }
            recordValues = token == "record"; applySelectedValues(); return nil
        }
        if name.hasPrefix("space.") {
            let error = space.set(String(name.dropFirst(6)), token)
            if error == nil && name == "space.field" { fieldPreference.remember(space.field) }
            return error
        }
        if name == "midi.mpe" { midiMPE = token; return nil }
        if name.hasPrefix("midi.ctl."), let channel = Int(name.dropFirst(9)), (1...16).contains(channel) {
            midiMaps[channel - 1] = token; return nil
        }
        if name == "statement.add" { return patch.addStatement(token) }
        if patch.setField(name, token) { return nil }
        if name.hasPrefix("op") || name.hasPrefix("route.") || name.hasPrefix("filter.") || InstrCtl.stmt[name] != nil {
            guard let v else { return "\(name) takes a number" }
            if ["filter.cutoff", "filter.q"].contains(name), patch.filterField(name) == nil {
                return NativeParams.unavailableMessage
            }
            if let f = InstrCtl.stmt[name], f.range == nil, patch.statementField(name) == nil {
                return NativeParams.unavailableMessage
            }
            let parts = name.split(separator: ".").map(String.init)
            if parts.count == 2, parts[0].hasPrefix("op"), let i = Int(parts[0].dropFirst(2)),
               (0..<Int(FMS_OPS)).contains(i), let f = InstrCtl.op[parts[1]],
               f.range == nil, patch.operatorField(parts[1]) == nil {
                return NativeParams.unavailableMessage
            }
            var p = patch
            guard InstrCtl.setPatch(&p, name, v) else { return "unknown control \(name)" }
            patch = p
            return nil
        }
        if name.hasPrefix("fx.") {
            let k = String(name.dropFirst(3))
            if let kp = InstrFx.toggles[k] { fx[keyPath: kp] = (v ?? 0) != 0; return nil }
            if let (kp, r) = InstrFx.values[k], let v { fx[keyPath: kp] = InstrCtl.clamp(v, r); return nil }
            if let (kp, r) = InstrFx.signedValues[k], let v {
                fx[keyPath: kp] = min(max(v, r.lowerBound), r.upperBound); return nil
            }
            return "unknown control \(name)"
        }
        if name.hasPrefix("send."), let b = Self.busNames.firstIndex(of: String(name.dropFirst(5))), let v {
            setSend(b, Double(v)); return nil
        }
        if name.hasPrefix("button.") { return press(String(name.dropFirst(7)), token) }
        switch name {
        case "source": return pick(token)
        case "name": patchName = token
        case "stereo": stereo = (v ?? 0) != 0
        case "live.cutoff": guard let v else { return "number" }; setLiveCutoff(Double(v))
        case "live.q": guard let v else { return "number" }; setLiveQ(Double(v))
        case "live.yaw": guard let v else { return "number" }; return setYaw(v)
        case "live.crush.bits": guard let v else { return "integer bits required" }; return setLiveCrush(bits: v, rateDiv: liveCrushRateDiv)
        case "live.crush.rate_div": guard let v else { return "integer rate divisor required" }; return setLiveCrush(bits: liveCrushBits, rateDiv: v)
        case "pos.voice": guard let v, (-1..<voices).contains(v) else { return "Voice takes all (-1) or 0...\(voices - 1)." }; posTarget = v
        case "pos.x": guard let v else { return "number" }; return setPos(CGPoint(x: CGFloat(v), y: pos.y))
        case "pos.y": guard let v else { return "number" }; return setPos(CGPoint(x: pos.x, y: CGFloat(v)))
        case "pos.z": guard let v, (-16777216...16777216).contains(v) else { return "Z takes -16777216...16777216 mm." }; return setPos(pos, z: v)
        case "pos.duration": guard let d = UInt64(token) else { return "Move duration takes unsigned integer nanoseconds; 0 selects pos." }; moveDuration = d
        case "velocity": guard let v else { return "number" }; velocity = Double(min(max(v, 0), 32767))
        case "octave": guard let v else { return "number" }; setOctave(v)
        default: return "unknown control \(name)"
        }
        return nil
    }

    /// The value of a control by name, as text ("" for a button).
    func get(_ name: String) -> String {
        if name == "values" { return recordValues ? "record" : "defaults" }
        if name == "values.defaultField" { return InstrSpace.fields[preferredField] }
        if name.hasPrefix("space.") { return space.value(String(name.dropFirst(6))) }
        if name == "midi.mpe" { return midiMPE }
        if name.hasPrefix("midi.ctl."), let channel = Int(name.dropFirst(9)), (1...16).contains(channel) {
            return midiMaps[channel - 1]
        }
        if let f = patch.fields.first(where: { $0.id == name }) { return patch.value(f) }
        if let v = InstrCtl.getPatch(patch, name) { return String(v) }
        if name.hasPrefix("fx.") {
            let k = String(name.dropFirst(3))
            if let kp = InstrFx.toggles[k] { return fx[keyPath: kp] ? "1" : "0" }
            if let (kp, _) = InstrFx.values[k] { return String(fx[keyPath: kp]) }
            if let (kp, _) = InstrFx.signedValues[k] { return String(fx[keyPath: kp]) }
        }
        if name.hasPrefix("send."), let b = Self.busNames.firstIndex(of: String(name.dropFirst(5))) {
            return String(Int(sends[b]))
        }
        switch name {
        case "source":
            switch source {
            case .builtin: return "builtin"
            case .recipe(let n): return "recipe:" + n
            case .record(let i): return "record:\(i)"
            case .file(let path, let i): return "file:\(path)#\(i)"
            }
        case "name": return patchName
        case "stereo": return stereo ? "1" : "0"
        case "live.cutoff": return String(Int(liveCutoff))
        case "live.q": return String(Int(liveQ))
        case "live.yaw": return String(liveYaw)
        case "live.crush.bits": return String(liveCrushBits)
        case "live.crush.rate_div": return String(liveCrushRateDiv)
        case "pos.voice": return String(posTarget)
        case "pos.x": return String(Int(pos.x))
        case "pos.y": return String(Int(pos.y))
        case "pos.z": return String(posZ)
        case "pos.duration": return String(moveDuration)
        case "velocity": return String(Int(velocity))
        case "octave": return String(octaveBase)
        default: return ""
        }
    }

    /// A binding through set and get, for a numeric control.
    func u32(_ name: String) -> Binding<UInt32> {
        Binding(get: { UInt32(self.get(name)) ?? 0 }, set: { self.set(name, String($0)) })
    }

    func integer(_ name: String) -> Binding<Int> {
        Binding(get: { Int(self.get(name)) ?? 0 }, set: { self.set(name, String($0)) })
    }

    func flag(_ name: String) -> Binding<Bool> {
        Binding(get: { self.get(name) == "1" }, set: { self.set(name, $0 ? "1" : "0") })
    }

    /// A button by name; nil when done, else why not.
    @discardableResult
    func press(_ b: String, _ arg: String = "") -> String? {
        if "button." + b == dropped { return nil }
        switch b {
        case "undo_patch": return undoPatch()
        case "redo_patch": return redoPatch()
        case "store_a": storeCompare(0)
        case "store_b": storeCompare(1)
        case "swap_ab": swapCompare()
        case "play_live": arm()
        case "apply": guard liveDirty else { return "apply is disabled: nothing to apply" }; arm()
        case "pause": engine.stop()
        case "resume": engine.play()
        case "record":
            takeVerify = ""
            if engine.startRecording() {
                crusherBeforePendingRecord = (liveCrushBits, liveCrushRateDiv)
                liveCrushBits = 24; liveCrushRateDiv = 1
            }
        case "stop": engine.stopRecording()
        case "verify_take":
            takeVerify = "rendering..."
            let t = takeText
            Task { takeVerify = await InstrPatchIO.verifyText(t) }
        case "save":   // the Save panel's path; FMS_SCRIPT passes it
            if takeSave == "Saving..." { return "A take save is already running." }
            if arg.isEmpty { saveTake() } else { writeTake(URL(fileURLWithPath: arg)) }
        case "copy":
            let text = InstrPatchIO.patchText(patch, name: safeName)
            Self.board.clearContents()
            Self.board.setString(text, forType: .string)
            copied = true
            Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
        case "oct_down": setOctave(octaveBase - 12)
        case "oct_up": setOctave(octaveBase + 12)
        case "rescan": recipes.refresh(engine.recipesDir)
        default: return "unknown button \(b)"
        }
        return nil
    }

    // ---- source ----

    func snapshotRecord() {
        guard !engine.isLive else { return }
        recPaths = engine.loadedPaths
        recFiles = try? recPaths.map { try Data(contentsOf: URL(fileURLWithPath: $0)) }
        recTitle = engine.title
        recPatches = engine.patches
        recNames = engine.patchNames
        recDropped = engine.patchDropped
        recHeader = engine.initialHeader
        if case .record = source { source = .builtin }   // the old index means nothing now
    }

    /// builtin, recipe:NAME or record:INDEX.
    func pick(_ token: String) -> String? {
        if token.hasPrefix("open:") {
            let path = String(token.dropFirst(5))
            guard !path.isEmpty else { return "open: needs a file path" }
            return openPatch(URL(fileURLWithPath: path))
        }
        if token.hasPrefix("recent:") {
            guard let i = Int(token.dropFirst(7)), recentPaths.indices.contains(i) else { return "recent: needs an index from the current recent list" }
            return openPatch(URL(fileURLWithPath: recentPaths[i]))
        }
        if token == "builtin" { pick(.builtin); return nil }
        if token.hasPrefix("recipe:") { pick(.recipe(String(token.dropFirst(7)))); return nil }
        if token.hasPrefix("record:"), let i = Int(token.dropFirst(7)) { pick(.record(i)); return nil }
        return "source takes builtin, recipe:NAME, record:INDEX, open:PATH or recent:INDEX"
    }

    func pick(_ s: InstrSource) {
        source = s
        origin = nil
        loadNote = nil
        dropNote = nil
        switch s {
        case .builtin:
            selectedHeader = nil
            replaceSourcePatch(InstrPatchIO.defaultPatch(), name: "default")
        case .record(let i):
            guard i >= 0, i < recPatches.count, i < recNames.count else { return }
            guard let header = recHeader else { engine.errorText = "Native record header read-back failed; patch was not imported."; return }
            origin = PatchOrigin(paths: recPaths, patchIndex: i, files: recFiles)
            replaceSourcePatch(recPatches[i], name: recNames[i])
            selectedHeader = header; applySelectedValues()
            loadNote = "from \(recTitle)"
            if i < recDropped.count { dropNote = instrDropLabel(recDropped[i]) }
        case .file(let path, let index):
            if let error = openPatch(URL(fileURLWithPath: path), index: index) { engine.errorText = error }
        case .recipe(let name):
            selectedHeader = nil
            guard let e = recipes.entries.first(where: { $0.id == name }) else {
                engine.errorText = "recipe \(name): not in the current recipe list"
                return
            }
            if let error = e.error { engine.errorText = error; return }
            switch InstrPatchIO.loadPatch0(paths: [e.url.path]) {
            case .success(let (p, n, dropped)):
                replaceSourcePatch(p, name: n.isEmpty ? name : n)
                loadNote = "recipe \(name)"
                dropNote = instrDropLabel(dropped)
            case .failure(let err):
                engine.errorText = "recipe \(name): \(err.description)"
            }
        }
    }

    private struct OpenedPatch {
        var patch: PatchLines
        var header: InstrHeader
        var origin: PatchOrigin
        var title: String
    }

    /// One native parse supplies both identity checks and the published value.
    /// Nothing observable changes until the complete result has been accepted.
    private func readPatch(_ url: URL, index: Int?, expectedName: String? = nil) -> Result<OpenedPatch?, InstrError> {
        var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        let path = url.standardizedFileURL.path
        guard let p = TLDocumentMetadata.open(paths: [path], error: &err) else {
            return .failure(InstrError(instrCStr(err)))
        }
        defer { fms_player_close(p) }
        var info = fms_info_t(); fms_player_info(p, &info)
        guard info.npatches > 0 else { return .failure(InstrError("The record has no patches.")) }
        var chosen = index ?? 0
        if index == nil && info.npatches > 1 {
            if Engine.offline { return .failure(InstrError("This record has several patches; use openpatch RECORD INDEX in script mode.")) }
            let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 300, height: 28))
            for i in 0..<info.npatches {
                let name = PatchLines.read(p, i, fs: info.fs)?.name ?? "?"
                picker.addItem(withTitle: "\(i): \(name)")
            }
            let alert = NSAlert(); alert.messageText = "Choose a patch"
            alert.informativeText = url.lastPathComponent
            alert.accessoryView = picker; alert.addButton(withTitle: "Open"); alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return .success(nil) }
            chosen = picker.indexOfSelectedItem
        }
        guard chosen >= 0, chosen < Int(info.npatches),
              let loaded = PatchLines.read(p, UInt32(chosen), fs: info.fs) else { return .failure(InstrError("Patch index is outside this record.")) }
        if let expectedName, loaded.name != expectedName {
            return .failure(InstrError("Favorite patch name no longer matches index \(chosen); the current patch was not changed."))
        }
        guard !loaded.hasGenerator else { return .failure(InstrError("Generator-bound patches are read-only in Records; live bank editing is not implemented.")) }
        let dropped = instrDropped(p, UInt32(chosen))
        guard dropped.isEmpty else { return .failure(InstrError(instrDropLabel(dropped) ?? dropped)) }
        guard let header = InstrHeader.read(p) else { return .failure(InstrError("Native record header read-back failed; patch was not imported.")) }
        return .success(OpenedPatch(patch: loaded, header: header,
                                   origin: PatchOrigin(paths: [path], patchIndex: chosen,
                                                       patchFile: fms_player_is_patch_file(p) != 0, bytes: try? Data(contentsOf: url)),
                                   title: "from \(url.lastPathComponent), patch \(chosen)"))
    }

    private func publishPatch(_ opened: OpenedPatch, favoriteSpace: InstrSpace? = nil) {
        replaceSourcePatch(opened.patch, name: opened.patch.name)
        selectedHeader = opened.header
        if let favoriteSpace { if dropped != "favorite.space" { space = favoriteSpace } } // current FX intentionally retained
        else { applySelectedValues() }
        source = .file(opened.origin.paths[0], opened.origin.patchIndex); origin = opened.origin
        loadNote = opened.title; dropNote = nil
        rememberPatch(opened.origin.paths[0])
    }

    /// Metadata only; the audio player and source bytes are unchanged.
    @discardableResult
    func openPatch(_ url: URL, index: Int? = nil) -> String? {
        switch readPatch(url, index: index) {
        case .success(let opened): if let opened { publishPatch(opened) }; return nil
        case .failure(let error): return error.description
        }
    }

    @discardableResult
    func addFavorite() -> String? {
        guard !fileBusy else { return "Wait for the current file operation before adding a favorite." }
        guard let origin, origin.paths.count == 1 else {
            return "Favorites need one saved file. Use Save As and reopen it before adding."
        }
        switch readPatch(URL(fileURLWithPath: origin.paths[0]), index: origin.patchIndex) {
        case .failure(let error): return "Favorite refused: " + error.description
        case .success(let opened):
            guard let opened, InstrPatchState(patch: opened.patch, name: opened.patch.name) == patchState else {
                return "Unsaved or normalized patch rows differ from the file. Use Save As and reopen before adding a favorite."
            }
            return favoriteStore.add(InstrFavorite(path: opened.origin.paths[0], index: origin.patchIndex,
                                                   name: opened.patch.name, space: space))
        }
    }

    @discardableResult
    func loadFavorite(_ index: Int) -> String? {
        guard !fileBusy else { return "Wait for the current file operation before loading a favorite." }
        guard favorites.indices.contains(index) else { return "Favorite index is outside the list." }
        let favorite = favorites[index]
        switch readPatch(URL(fileURLWithPath: favorite.path), index: favorite.index, expectedName: favorite.name) {
        case .failure(let error): return "Favorite refused: " + error.description
        case .success(let opened):
            guard let opened else { return "Favorite patch is unavailable." }
            publishPatch(opened, favoriteSpace: favorite.space); return nil
        }
    }
    @discardableResult func removeFavorite(_ index: Int) -> String? { favoriteStore.remove(index) }
    @discardableResult func removeUnreadableFavorites() -> String? { favoriteStore.removeUnreadable() }

    private func applySelectedValues() {
        guard let header = selectedHeader else { return }
        if recordValues { space = header.space; fx = header.fx }
        else { space = InstrSpace(); space.field = preferredField; fx = InstrFx() }
    }

    private func rememberPatch(_ path: String) {
        let path = URL(fileURLWithPath: path).standardizedFileURL.path
        recentPaths = Array(([path] + recentPaths.filter { $0 != path }).prefix(10))
        recentDefaults?.set(recentPaths, forKey: Self.recentKey)
    }

    func importRecipesPanel() {
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.plainText]
        panel.allowsMultipleSelection = false; panel.canChooseDirectories = false
        if panel.runModal() == .OK, let url = panel.url { Task { await recipes.importFile(url) } }
    }

    func exportRecipePanel() {
        guard !fileBusy else { return }
        let panel = NSSavePanel(); panel.allowedContentTypes = [.plainText]
        panel.nameFieldStringValue = safeName + "-recipes.txt"
        if panel.runModal() == .OK, let url = panel.url {
            Task { if let error = await exportRecipe(to: url) { engine.errorText = error } }
        }
    }

    func exportRecipe(to url: URL) async -> String? {
        guard !fileBusy else { return "A file operation is already running." }
        fileBusy = true; fileStatus = "Validating recipe export…"; defer { fileBusy = false }
        do {
            let pending = try await InstrPatchIO.exportRecipe(patch, name: safeName, to: url)
            fileStatus = "Exported recipe; \(pending) pending statement(s) preserved as comments"; return nil
        } catch { fileStatus = "Export refused: \(error)"; return fileStatus }
    }

    func openPatchPanel() {
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.init(filenameExtension: "tl") ?? .plainText]
        panel.allowsMultipleSelection = false; panel.canChooseDirectories = false
        if panel.runModal() == .OK, let url = panel.url, let error = openPatch(url) { engine.errorText = error }
    }

    func savePatchPanel(asNew: Bool) {
        guard !fileBusy else { return }
        if !asNew, let origin, origin.patchFile, let path = origin.paths.first {
            Task { if let error = await savePatch(to: URL(fileURLWithPath: path), replacingOrigin: true) { engine.errorText = error } }
            return
        }
        let panel = NSSavePanel(); panel.allowedContentTypes = [.init(filenameExtension: "tl") ?? .plainText]
        panel.nameFieldStringValue = safeName + ".tl"
        if panel.runModal() == .OK, let url = panel.url {
            Task { if let error = await savePatch(to: url, replacingOrigin: false) { engine.errorText = error } }
        }
    }

    @discardableResult
    func savePatch(to url: URL, replacingOrigin: Bool) async -> String? {
        guard !fileBusy else { return "A file operation is already running." }
        fileBusy = true; fileStatus = "Rendering patch file…"
        defer { fileBusy = false }
        let selected = patch, name = safeName, oldOrigin = origin
        do {
            if replacingOrigin {
                guard let oldOrigin, oldOrigin.patchFile, oldOrigin.paths == [url.path],
                      let old = oldOrigin.bytes, try Data(contentsOf: url) == old else {
                    throw InstrError("The origin is not an unchanged patch file. Use Save As or reopen it.")
                }
            }
            let text = try await InstrPatchIO.sealRecord(InstrPatchIO.patchFileBody(selected, name: name))
            // Recheck after rendering; never overwrite an intervening external edit.
            if replacingOrigin, let old = oldOrigin?.bytes, try Data(contentsOf: url) != old {
                throw InstrError("The origin changed while rendering. No file was saved.")
            }
            let bytes = Data(text.utf8); try bytes.write(to: url, options: .atomic)
            if patch == selected && safeName == name {
                origin = PatchOrigin(paths: [url.path], patchIndex: 0, patchFile: true, bytes: bytes)
                source = .file(url.path, 0); loadNote = "saved \(url.lastPathComponent)"
            }
            rememberPatch(url.path)
            fileStatus = "Saved \(url.lastPathComponent)"; return nil
        } catch {
            fileStatus = "Save refused: \(error)"; return fileStatus
        }
    }

    func writeBack() async -> String? {
        guard !fileBusy, let origin else { return "Open a record before Write Back." }
        guard safeName == patch.name else { return "Write Back keeps the record's patch name. Use Save As for a renamed patch." }
        fileBusy = true; fileStatus = "Checking record write-back…"
        defer { fileBusy = false }
        do {
            let candidate = try PatchWriteback.prepare(origin, patch: patch)
            let sealed = try await candidate.sealed()
            try sealed.publish()
            if self.origin == origin {
                self.origin = PatchOrigin(paths: sealed.paths, patchIndex: origin.patchIndex, patchFile: origin.patchFile,
                                          bytes: sealed.files.count == 1 ? sealed.files[0] : nil, files: sealed.files)
            }
            fileStatus = sealed.target == nil ? "Record unchanged" : "Wrote patch and hash; all other bytes preserved"
            return nil
        } catch { fileStatus = "Write Back refused: \(error)"; return fileStatus }
    }

    // ---- live ----

    func arm(geometry: [fms_event_t]? = nil) {
        if let d = patch.diagnostic { engine.errorText = d; return }
        if patch.hasGenerator { engine.errorText = "Generator-bound patches are read-only in Records."; return }
        // What is armed is what the controls held at the press, even if
        // they change while the player opens.
        let p = patch, f = fx, sp = space, maps = midiMaps, mpe = midiMPE
        let translator: OpaquePointer
        do { translator = try MidiInput.shared.prepare(p, voices: voices, maps: maps, mpe: mpe) }
        catch { engine.errorText = String(describing: error); alloc.armed(false); return }
        engine.openLive(patch: p, name: safeName, voices: voices, space: sp, extra: f.extra, geometry: geometry) { [unowned self] ok in
            if ok {
                alloc.allOff()
                armedFx = f; armedPatch = p; armedSpace = sp
                liveYaw = geometry?.first(where: { $0.type == UInt32(FMS_EV_YAW) }).map { Int($0.x) } ?? (sp.yawPresent ? sp.yaw : 0)
                armedMidiMaps = maps; armedMidiMPE = mpe
                sends = Array(repeating: 0.0, count: Self.busNames.count)
                liveCrushBits = 24; liveCrushRateDiv = 1
                crusherBeforePendingRecord = nil
                liveCutoff = Double(p.filter_cutoff_millihz == 0 ? 2_000_000 : p.filter_cutoff_millihz)
                liveQ = Double(p.filter_q_milli == 0 ? 707 : p.filter_q_milli)
                alloc.clearExternal()
                MidiInput.shared.armed(translator)
            } else { fms_midi_destroy(translator) }
            alloc.armed(ok)
        }
    }

    /// The name as the timeline grammar takes it: letters, digits, '.', '_', '-'.
    var safeName: String {
        let ok = patchName.unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) && $0.isASCII || "._-".unicodeScalars.contains($0) }
        let s = String(String.UnicodeScalarView(ok)).prefix(Int(FMS_NAME) - 1)
        return s.isEmpty ? "live" : String(s)
    }

    var patchKey: Data { patch.key }

    var liveDirty: Bool {
        guard engine.isLive, let ap = armedPatch else { return false }
        return armedFx != fx || armedSpace != space || ap.key != patchKey || armedMidiMaps != midiMaps || armedMidiMPE != midiMPE
    }

    /// The live box's labels : the re-arm and tail loss, and the
    /// device rate when it is not the live player's.
    var liveLabels: [String] {
        guard engine.isLive else { return [] }
        return [rearmLabel(dirty: liveDirty)].compactMap { $0 }
    }

    private func pushAll(_ make: (Int) -> fms_event_t) {
        for v in 0..<voices { engine.push(make(v)) }
    }

    private func event(_ type: Int32, _ voice: Int) -> fms_event_t {
        var e = fms_event_t()
        e.type = UInt32(type)
        e.voice = UInt32(voice)
        return e
    }

    func setSend(_ b: Int, _ v: Double) {
        let level = min(max(v.rounded(), 0), 32768)
        sends[b] = level
        pushAll { var e = event(Int32(FMS_EV_SEND), $0); e.bus = UInt32(b); e.value = UInt32(level); return e }
    }

    func setLiveCutoff(_ v: Double) { liveCutoff = min(max(v.rounded(), 20_000), 20_000_000); pushFilter() }
    func setLiveQ(_ v: Double) { liveQ = min(max(v.rounded(), 500), 16000); pushFilter() }

    private func setLiveCrush(bits: Int, rateDiv: Int) -> String? {
        guard engine.isLive, armedFx?.crushOn == true else { return "Arm a crusher-enabled player before editing live crusher values." }
        guard (1...24).contains(bits), (1...64).contains(rateDiv) else { return "Crusher bits takes 1...24 and rate divisor takes 1...64." }
        liveCrushBits = bits; liveCrushRateDiv = rateDiv
        crusherBeforePendingRecord = nil
        pushAll { var e = event(Int32(FMS_EV_CRUSH), $0); e.value = UInt32(bits); e.bus = UInt32(rateDiv); return e }
        return nil
    }

    private func pushFilter() {
        pushAll { var e = event(Int32(FMS_EV_FILTER), $0); e.freq_millihz = UInt32(liveCutoff); e.value = UInt32(liveQ); return e }
    }

    var spaceConflict: String? {
        if space.field >= Int(FMS_SPACE_AMB1) { return "Ambisonic records play in Records; choose a live field explicitly." }
        if fx.extra != nil && space.panlawPresent {
            return "Effects with panlaw remain unsupported. Values are retained; remove panlaw or disable effects before Apply. Native validation checks the selected field and profile."
        }
        return nil
    }

    /// Mirror native fms_live_event's ceil spelling of the current frame.
    /// Callback advancement can still make a queued move fail native validation.
    static func moveFits(frame: UInt32, fs: UInt32, duration: UInt64) -> Bool {
        guard fs > 0, duration > 0 else { return false }
        let rate = UInt64(fs)
        let start = (UInt64(frame) * 1_000_000_000 + rate - 1) / rate
        guard start <= UInt64.max / rate, duration <= UInt64.max / rate - start else { return false }
        return (start + duration) * rate / 1_000_000_000 <= UInt64(UInt32.max)
    }

    /// Numeric positions retain the complete native range. Only the map's
    /// drag gesture has a smaller, explicitly labelled display extent.
    @discardableResult func setPos(_ p: CGPoint, z: Int? = nil) -> String? {
        let zz = z ?? posZ
        guard p.x.isFinite, p.y.isFinite, (-16777216...16777216).contains(p.x),
              (-16777216...16777216).contains(p.y), (-16777216...16777216).contains(zz) else {
            return "XYZ takes -16777216...16777216 mm."
        }
        if engine.isLive && armedFx?.decodable == true && (moveDuration > 0 || engine.positionFrames > 0) {
            return "The armed decodable profile permits one fixed position per voice at frame zero; position was not changed."
        }
        let q = CGPoint(x: p.x.rounded(), y: p.y.rounded())
        if moveDuration > 0, engine.isLive, let info = engine.info {
            guard Self.moveFits(frame: engine.positionFrames, fs: info.fs, duration: moveDuration) else {
                return "Move duration ends past the native 2^32-1-frame limit; position was not changed."
            }
        }
        pos = q; posZ = zz
        let targets = posTarget < 0 ? Array(0..<voices) : [posTarget]
        for v in targets {
            var e = event(Int32(moveDuration == 0 ? FMS_EV_POS : FMS_EV_MOVE), v)
            e.x = Int32(q.x); e.y = Int32(q.y); e.z = Int32(zz); e.dur_ns = moveDuration
            if engine.isLive && !engine.push(e) { return "Position event refused: the live queue is full." }
        }
        return nil
    }

    @discardableResult func setYaw(_ v: Int) -> String? {
        guard (-360000...360000).contains(v) else { return "Yaw takes -360000...360000 millidegrees." }
        guard engine.isLive, armedSpace?.field == Int(FMS_SPACE_BINAURAL) else { return "Live yaw requires an armed binaural field." }
        var e = event(Int32(FMS_EV_YAW), 0); e.x = Int32(v)
        guard engine.push(e) else { return "Yaw event refused: the live queue is full." }
        liveYaw = v; return nil
    }

    func setOctave(_ b: Int) { octaveBase = min(max(b, 12), 108) }

    // ---- take ----

    private func saveTake() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "take.tl"
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        writeTake(url)
    }

    private func writeTake(_ url: URL) {
        let text = takeText
        takeSave = "Saving..."
        Task {
            if let error = await InstrPatchIO.saveTake(text, to: url) {
                takeSave = error
                engine.errorText = error
            } else {
                takeSave = "Saved \(url.lastPathComponent) and \(url.deletingPathExtension().appendingPathExtension("wav").lastPathComponent)"
            }
        }
    }
}
