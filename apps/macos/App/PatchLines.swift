// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// One patch is an ordered set of accepted statement tokens. Typed controls
// are views over these rows; the harness alone validates edits. Disabled
// controls retain their token rows but do not emit them. No C patch copy is
// stored, and statements without typed controls stay in this same model.

import Foundation

struct PatchLines: Equatable, Sendable {
    struct Line: Equatable, Sendable {
        var tokens: [String]
        var enabled = true
        var statement: String { tokens.first ?? "" }
        var text: String { tokens.joined(separator: " ") }
    }
    var rows: [Line]
    private(set) var diagnostic: String?
    var fs: UInt32 = 48000
    private let parameters: NativeParams?

    init(_ text: String = "", fs: UInt32 = 48000, parameters: NativeParams? = NativeParams.bundled) {
        self.fs = fs
        self.parameters = parameters
        rows = text.split(separator: "\n").map { Line(tokens: $0.split(whereSeparator: { $0 == " " || $0 == "\t" }).map(String.init)) }
    }
    // Catalog context does not change patch identity or the undo journal.
    static func == (lhs: PatchLines, rhs: PatchLines) -> Bool {
        lhs.rows == rhs.rows && lhs.diagnostic == rhs.diagnostic && lhs.fs == rhs.fs
    }

    var text: String { rows.filter(\.enabled).map(\.text).joined(separator: "\n") + "\n" }
    var name: String { rows.first(where: { $0.statement == "patch" && $0.tokens.count > 1 })?.tokens.dropFirst().first ?? "live" }
    var hasGenerator: Bool { rows.contains { $0.enabled && $0.statement == "gen_clock" } }
    var key: Data { Data(text.utf8) }

    // The main Filter view and its named setter share this descriptor. UInt32
    // representability is a bridge requirement, not a fallback parameter range.
    func filterField(_ name: String) -> NativeParams.NumericField? {
        let token: Int
        switch name {
        case "filter.cutoff": token = 3
        case "filter.q": token = 4
        default: return nil
        }
        guard let field = parameters?.numericField(statement: "filter", token: token),
              UInt32(exactly: field.range.lowerBound) != nil,
              UInt32(exactly: field.range.upperBound) != nil else { return nil }
        return field
    }

    // Unconditional statement fields only; omission aliases, masks and enums
    // retain their existing policies. Storage checks guard the bridge writes.
    static let statementTokens = [
        "lfo.delay": 4, "lfo.fade": 5, "lfo.pitch": 6, "lfo.level": 7, "lfo.index": 8, "lfo.phase": 9,
        "peg.l1": 2, "peg.t1": 3, "peg.l2": 4, "peg.t2": 5, "peg.l3": 6, "peg.t3": 7, "peg.l4": 8, "peg.t4": 9,
        "fenv.attack": 2, "fenv.decay": 3, "fenv.sustain": 4, "fenv.release": 5, "fenv.depth": 6, "fenv.track": 7
    ]
    func statementField(_ name: String) -> NativeParams.NumericField? {
        guard let token = Self.statementTokens[name], let statement = name.split(separator: ".").first,
              let field = parameters?.numericField(statement: String(statement), token: token) else { return nil }
        if name.hasPrefix("peg.l") || name == "fenv.depth" {
            guard Int32(exactly: field.range.lowerBound) != nil,
                  Int32(exactly: field.range.upperBound) != nil else { return nil }
        } else {
            guard UInt32(exactly: field.range.lowerBound) != nil,
                  UInt32(exactly: field.range.upperBound) != nil else { return nil }
        }
        return field
    }

    // Equal-bound operator fields only. Ratio conversion, enums and the
    // existing effective level/velocity/slope caps retain their own policies.
    static let operatorTokens = ["index": 5, "feedback": 6, "attack": 8, "decay": 9,
                                 "release": 11, "ks_break": 12, "rate_scale": 15, "fixed": 16, "phase": 17]
    func operatorField(_ key: String) -> NativeParams.NumericField? {
        guard let token = Self.operatorTokens[key],
              let field = parameters?.numericField(statement: "patch", token: token),
              UInt32(exactly: field.range.lowerBound) != nil,
              UInt32(exactly: field.range.upperBound) != nil else { return nil }
        return field
    }

    static func read(_ player: OpaquePointer, _ index: UInt32, fs: UInt32,
                     parameters: NativeParams? = NativeParams.bundled) -> PatchLines? {
        let n = fms_player_patch_lines(player, index, nil, 0)
        guard n > 0 else { return nil }
        var b = [CChar](repeating: 0, count: n)
        guard fms_player_patch_lines(player, index, &b, n) == n else { return nil }
        return PatchLines(String(cString: b), fs: fs, parameters: parameters)
    }

    mutating func validate() {
        var e = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        let t = text
        let bad = t.withCString { fms_patch_lines_check($0, t.utf8.count, fs, &e, e.count) }
        diagnostic = bad == 0 ? nil : String(cString: e)
    }

    // A raw edit may be incomplete or rename a statement. Keep it as entered;
    // projections are safe views, and only an explicit edit fills tokens.
    mutating func editRow(_ index: Int, text: String) {
        guard rows.indices.contains(index) else { return }
        rows[index].tokens = text.split(whereSeparator: { $0 == " " || $0 == "\t" }).map(String.init)
        validate()
    }
    private func padded(_ tokens: [String], _ defaults: [String]) -> [String] {
        tokens + defaults.dropFirst(min(tokens.count, defaults.count))
    }

    func index(_ statement: String, _ op: Int? = nil) -> Int? {
        rows.firstIndex { r in r.statement == statement && (op == nil || (r.tokens.count > 2 && r.tokens[2] == String(op!))) }
    }
    func tokens(_ statement: String, _ op: Int? = nil) -> [String] {
        index(statement, op).map { rows[$0].tokens } ?? []
    }
    func number(_ statement: String, _ op: Int? = nil, _ token: Int) -> Int {
        let t = tokens(statement, op)
        return token >= 0 && token < t.count ? Int(t[token]) ?? 0 : 0
    }
    mutating func put(_ statement: String, _ op: Int? = nil, _ values: [String], enabled: Bool = true) {
        if let i = index(statement, op) { rows[i].tokens = values; rows[i].enabled = enabled }
        else {
            // Insert new controls in the harness writer's statement order;
            // existing rows, including untyped lines, retain their order.
            let order = ["gen_clock", "gen", "gen_axiom", "gen_rule", "patch", "route", "filter", "shape", "amp", "lfo", "peg", "fenv", "mod", "eg", "ks", "kstype", "fbtype", "egloop", "noise", "pd", "vector", "formant", "wtbank", "wtdata", "wthash", "wtuse", "ksource"]
            let rank = order.firstIndex(of: statement) ?? order.count
            let at = rows.firstIndex { (order.firstIndex(of: $0.statement) ?? order.count) > rank } ?? rows.endIndex
            rows.insert(Line(tokens: values, enabled: enabled), at: at)
        }
    }
    mutating func set(_ statement: String, op: Int? = nil, token: Int, value: String) {
        guard let i = index(statement, op), token >= 0, token < rows[i].tokens.count else { return }
        rows[i].tokens[token] = value
        validate()
    }

    // fms_op_t and the small statement structs below are transient UI views.
    // Only the token rows above are stored or passed to the bridge.
    func op(_ i: Int) -> fms_op_t {
        var o = fms_op_t()
        o.ratio_q16 = UInt32(clamping: number("patch", i, 3))
        o.level_db10 = UInt32(clamping: number("patch", i, 4))
        o.index_q10 = UInt32(clamping: number("patch", i, 5))
        o.feedback_q10 = UInt32(clamping: number("patch", i, 6))
        o.vel_sens_q8 = UInt32(clamping: number("patch", i, 7))
        o.attack_ms = UInt32(clamping: number("patch", i, 8))
        o.decay_ms = UInt32(clamping: number("patch", i, 9))
        o.sustain_db10 = UInt32(clamping: number("patch", i, 10))
        o.release_ms = UInt32(clamping: number("patch", i, 11))
        o.ks_break_millihz = UInt32(clamping: number("patch", i, 12))
        o.ks_slope_db10 = UInt32(clamping: number("patch", i, 13))
        o.wave = UInt32(clamping: number("patch", i, 14))
        o.rate_scale = UInt32(clamping: number("patch", i, 15))
        o.fixed_millihz = UInt32(clamping: number("patch", i, 16))
        o.phase_u16 = UInt32(clamping: number("patch", i, 17))
        o.free_run = UInt32(clamping: number("patch", i, 18))
        return o
    }
    mutating func setOp(_ i: Int, _ o: fms_op_t) {
        let exact = index("patch", i)
        // An explicit op edit can complete a single header missing its op
        // token, when that op has no row already. Ambiguous rows stay raw.
        let partials = rows.indices.filter { rows[$0].statement == "patch" && rows[$0].tokens.count < 3 }
        let target = exact ?? (partials.count == 1 ? partials.first : nil)
        var t = target.map { rows[$0].tokens } ?? []
        let prior = op(i), incomplete = t.count < 12
        let enabled = target.map { rows[$0].enabled } ?? (i < (route_ops == 0 ? 2 : Int(route_ops)))
        t = padded(t, ["patch", name, String(i)] + Array(repeating: "0", count: 9))
        let pairs: [(UInt32, UInt32)] = [
            (o.ratio_q16, prior.ratio_q16),
            (o.level_db10, prior.level_db10),
            (o.index_q10, prior.index_q10),
            (o.feedback_q10, prior.feedback_q10),
            (o.vel_sens_q8, prior.vel_sens_q8),
            (o.attack_ms, prior.attack_ms),
            (o.decay_ms, prior.decay_ms),
            (o.sustain_db10, prior.sustain_db10),
            (o.release_ms, prior.release_ms),
            (o.ks_break_millihz, prior.ks_break_millihz),
            (o.ks_slope_db10, prior.ks_slope_db10),
            (o.wave, prior.wave),
            (o.rate_scale, prior.rate_scale),
            (o.fixed_millihz, prior.fixed_millihz),
            (o.phase_u16, prior.phase_u16),
            (o.free_run, prior.free_run)
        ]
        for (k, pair) in pairs.enumerated() where pair.0 != pair.1 || (incomplete && k < 9) {
            let last = k == 9 ? 13 : k + 3 // key-scaling break and slope form a pair
            while t.count <= last { t.append("0") }
            t[k + 3] = String(pair.0)
        }
        if let target { rows[target] = Line(tokens: t, enabled: enabled) }
        else { put("patch", i, t, enabled: enabled) }
    }
    var ops_defined: UInt32 {
        get { rows.reduce(0) { mask, r in
            guard r.enabled, r.statement == "patch", r.tokens.count > 2, let i = UInt32(r.tokens[2]), i < 4 else { return mask }
            return mask | (1 << i)
        } }
        set { for i in rows.indices where rows[i].statement == "patch" {
            if rows[i].tokens.count > 2, let op = UInt32(rows[i].tokens[2]), op < 4 { rows[i].enabled = newValue & (1 << op) != 0 }
        } }
    }
    private var routeTokens: [String] { padded(tokens("route"), ["route", name, "0", "0"]) }
    var route_ops: UInt32 {
        get { guard let i = index("route"), rows[i].enabled else { return 0 }; return min(UInt32(FMS_OPS), UInt32(clamping: number("route", nil, 2))) }
        set { var t = routeTokens; t[2] = String(newValue); put("route", nil, t, enabled: newValue != 0) }
    }
    var route_carriers: UInt32 {
        get { UInt32(clamping: number("route", nil, 3)) }
        set { var t = routeTokens; t[3] = String(newValue); put("route", nil, t, enabled: route_ops != 0) }
    }
    func routeWeight(_ from: Int, _ to: Int) -> UInt32 {
        let prefix = "\(from)>\(to):"
        guard let e = tokens("route").dropFirst(4).first(where: { $0.hasPrefix(prefix) }) else { return 0 }
        return UInt32(e.dropFirst(prefix.count).split(separator: ":").first ?? "0") ?? 0
    }
    mutating func setRouteWeight(_ from: Int, _ to: Int, _ weight: UInt32) {
        var t = routeTokens
        let prefix = "\(from)>\(to):"
        let i = t.indices.dropFirst(4).first { t[$0].hasPrefix(prefix) }
        let suffix = i.map { t[$0].split(separator: ":").dropFirst(2).map(String.init) } ?? []
        if let i { t.remove(at: i) }
        if weight != 0 { t.append(prefix + String(weight) + (suffix.isEmpty ? "" : ":" + suffix.joined(separator: ":"))) }
        put("route", nil, t, enabled: route_ops != 0)
    }
    private var filterTokens: [String] { padded(tokens("filter"), ["filter", name, "lp", "0", "0"]) }
    private static let filterModes = ["off", "lp", "bp", "hp", "notch"]
    var filter_mode: UInt32 {
        get { guard let i = index("filter"), rows[i].enabled, rows[i].tokens.count > 2 else { return 0 }; return UInt32(Self.filterModes.firstIndex(of: rows[i].tokens[2]) ?? 0) }
        set { var t = filterTokens; t[2] = Self.filterModes[min(Int(newValue), 4)]; put("filter", nil, t, enabled: newValue != 0) }
    }
    var filter_cutoff_millihz: UInt32 {
        get { UInt32(clamping: number("filter", nil, 3)) }
        set { var t = filterTokens; t[3] = String(newValue); put("filter", nil, t, enabled: filter_mode != 0) }
    }
    var filter_q_milli: UInt32 {
        get { UInt32(clamping: number("filter", nil, 4)) }
        set { var t = filterTokens; t[4] = String(newValue); put("filter", nil, t, enabled: filter_mode != 0) }
    }
    var lfo: fms_lfo_t {
        get {
            var v = fms_lfo_t()
            v.wave = UInt32(clamping: number("lfo", nil, 2))
            v.rate_millihz = UInt32(clamping: number("lfo", nil, 3))
            v.delay_ms = UInt32(clamping: number("lfo", nil, 4))
            v.fade_ms = UInt32(clamping: number("lfo", nil, 5))
            v.pitch_cents = UInt32(clamping: number("lfo", nil, 6))
            v.level_db10 = UInt32(clamping: number("lfo", nil, 7))
            v.index_q10 = UInt32(clamping: number("lfo", nil, 8))
            v.phase_u16 = UInt32(clamping: number("lfo", nil, 9))
            v.mask_pitch = UInt32(clamping: number("lfo", nil, 10))
            v.mask_level = UInt32(clamping: number("lfo", nil, 11))
            v.mask_index = UInt32(clamping: number("lfo", nil, 12))
            v.free_run = UInt32(clamping: number("lfo", nil, 13))
            return v
        }
        set {
            let t = ["lfo", name] + [String(newValue.wave), String(newValue.rate_millihz), String(newValue.delay_ms), String(newValue.fade_ms), String(newValue.pitch_cents), String(newValue.level_db10), String(newValue.index_q10), String(newValue.phase_u16), String(newValue.mask_pitch), String(newValue.mask_level), String(newValue.mask_index), String(newValue.free_run)]
            put("lfo", nil, t, enabled: newValue.rate_millihz != 0)
        }
    }
    var fenv: fms_fenv_t {
        get {
            var v = fms_fenv_t()
            v.attack_ms = UInt32(clamping: number("fenv", nil, 2))
            v.decay_ms = UInt32(clamping: number("fenv", nil, 3))
            v.sustain_permille = UInt32(clamping: number("fenv", nil, 4))
            v.release_ms = UInt32(clamping: number("fenv", nil, 5))
            v.depth_cents = Int32(clamping: number("fenv", nil, 6))
            v.track_permille = UInt32(clamping: number("fenv", nil, 7))
            v.ref_millihz = UInt32(clamping: number("fenv", nil, 8))
            return v
        }
        set {
            var t = ["fenv", name] + [String(newValue.attack_ms), String(newValue.decay_ms), String(newValue.sustain_permille), String(newValue.release_ms), String(newValue.depth_cents), String(newValue.track_permille), String(newValue.ref_millihz)]
            if newValue.ref_millihz == 0 { t.removeLast() }
            put("fenv", nil, t, enabled: newValue.attack_ms != 0 || newValue.decay_ms != 0 || newValue.sustain_permille != 0 || newValue.release_ms != 0 || newValue.depth_cents != 0 || newValue.track_permille != 0 || newValue.ref_millihz != 0)
        }
    }
    var peg: fms_peg_t {
        get {
            var v = fms_peg_t()
            withUnsafeMutableBytes(of: &v.level_cents) { b in
                for k in 0..<4 { b.storeBytes(of: Int32(clamping: number("peg", nil, 2 + k * 2)), toByteOffset: k * 4, as: Int32.self) }
            }
            withUnsafeMutableBytes(of: &v.time_ms) { b in
                for k in 0..<4 { b.storeBytes(of: UInt32(clamping: number("peg", nil, 3 + k * 2)), toByteOffset: k * 4, as: UInt32.self) }
            }
            v.mask = UInt32(clamping: number("peg", nil, 10))
            return v
        }
        set {
            var t = ["peg", name]
            for k in 0..<4 {
                t.append(withUnsafeBytes(of: newValue.level_cents) { String($0.load(fromByteOffset: k * 4, as: Int32.self)) })
                t.append(withUnsafeBytes(of: newValue.time_ms) { String($0.load(fromByteOffset: k * 4, as: UInt32.self)) })
            }
            if newValue.mask != 0 { t.append(String(newValue.mask)) }
            put("peg", nil, t, enabled: t.dropFirst(2).contains { $0 != "0" })
        }
    }
}

// Editor projections for the remaining statement families. These positions,
// units and outer clamps come from the harness's tl_header_* functions;
// feature applicability and coupled constraints are still checked there.
extension PatchLines {
    struct Field: Identifiable {
        let id: String, label: String, unit: String
        let row: Int, token: Int
        var range: ClosedRange<Int>?
        var choices: [String] = []
        var native: NativeParams.NumericField?
        var metadataRequired = false
        var unavailableReason: String? {
            metadataRequired && native == nil ? NativeParams.unavailableMessage : nil
        }
    }
    static let typedStatements = InstrTypedStatements.names
    var fields: [Field] {
        var result: [Field] = []
        for (r, row) in rows.enumerated() where row.enabled {
            let t = row.tokens, s = row.statement
            func field(_ key: String, _ label: String, _ token: Int, _ unit: String = "", _ range: ClosedRange<Int>? = nil, _ choices: [String] = []) {
                let migrated = NativeParams.numericControls[s]?.contains(token) == true
                let native = migrated ? parameters?.numericField(statement: s, token: token) : nil
                result.append(Field(id: key, label: label, unit: unit, row: r, token: token,
                                    range: migrated ? native?.range : range, choices: choices,
                                    native: native, metadataRequired: migrated))
            }
            let i = t.count > 2 ? t[2] : "0", prefix = s + "." + i
            switch s {
            case "route":
                for (j, edge) in t.enumerated().dropFirst(4) {
                    let parts = edge.split(separator: ":"), ends = parts.first?.split(separator: ">") ?? []
                    if ends.count == 2 { field("route.kind.\(ends[0]).\(ends[1])", "edge \(ends[0]) → \(ends[1]) kind", j, "", nil, ["pm", "am", "ring", "sync"]) }
                }
            case "filter": field("filter.stages", "filter stages", 5, "", nil, ["1", "2"])
            case "shape":
                field("shape.drive", "drive", 2, "dB10")
                field("shape.kind", "shape", 3, "", nil, ["hard", "cubic", "fold", "asym"])
                field("shape.position", "position", 4, "", nil, ["post", "pre"])
            case "amp":
                field("amp.attack", "amp attack", 2, "ms")
                field("amp.decay", "amp decay", 3, "ms")
                field("amp.sustain", "amp sustain", 4, "dB10")
                field("amp.release", "amp release", 5, "ms")
            case "egloop": field(prefix + ".op", "loop operator", 2)
            case "kstype":
                field(prefix + ".op", "key scaling operator", 2)
                field(prefix + ".target", "op \(i) key scaling target", 3, "", nil, ["index", "level", "both"])
                field(prefix + ".right", "op \(i) right side", 4, "", nil, ["-", "+"])
                field(prefix + ".left", "op \(i) left side", 5, "", nil, ["-", "+"])
            case "fbtype":
                field(prefix + ".op", "feedback operator", 2)
                field(prefix + ".type", "op \(i) feedback type", 3, "", nil, ["classic", "pre", "square", "presquare"])
            case "mod":
                field(prefix + ".source", "slot \(i) source", 3)
                field(prefix + ".target", "slot \(i) target + index", 4)
                field(prefix + ".amount", "slot \(i) amount", 5, "target units", -2147483647...2147483647)
                field(prefix + ".shift", "slot \(i) shift", 6)
            case "eg":
                field(prefix + ".delay", "op \(i) delay", 3, "ms")
                field(prefix + ".decay2", "op \(i) second decay", 4, "ms")
                field(prefix + ".sustain2", "op \(i) second sustain", 5, "dB10")
            case "ks": field(prefix + ".slope", "op \(i) left slope", 3, "dB10/oct")
            case "noise": field(prefix + ".shift", "op \(i) colour shift", 3)
            case "pd": field(prefix + ".knee", "op \(i) knee", 3, "Q15")
            case "vector":
                field("vector.x", "x", 2, "Q15"); field("vector.y", "y", 3, "Q15")
            case "formant":
                field(prefix + ".center", "slot \(i) center", 3, "mHz")
                field(prefix + ".q", "slot \(i) Q", 4, "/1000")
                field(prefix + ".level", "slot \(i) attenuation", 5, "dB10")
            case "ksource":
                field(prefix + ".loss", "op \(i) loss", 3, "Q15")
                field(prefix + ".seed", "op \(i) seed", 4)
                field(prefix + ".buffer", "op \(i) buffer", 5, "samples", nil, ["64", "128", "256", "512", "1024", "2048", "4096", "8192"])
            case "wtuse":
                field(prefix + ".bank", "op \(i) bank", 3, "", nil, bankNames)
                field(prefix + ".position", "op \(i) position", 4, "Q15")
            case "harmonic" where t.count >= 4:
                let key = "harmonic.\(t[1]).\(t[2]).\(t[3])"
                field(key + ".frame", "frame", 2)
                field(key + ".number", "harmonic number", 3)
                field(key + ".level", "\(t[1]) frame \(t[2]) harmonic \(t[3])", 4, "dB10")
                field(key + ".phase", "phase", 5, "/65536")
            default: break
            }
        }
        return result
    }
    var bankNames: [String] { rows.filter { $0.enabled && $0.statement == "wtbank" && $0.tokens.count > 1 }.map { $0.tokens[1] } }
    func value(_ field: Field) -> String {
        guard rows.indices.contains(field.row), field.token >= 0 else { return "" }
        let t = rows[field.row].tokens
        if field.id.hasPrefix("route.kind.") { return field.token < t.count ? t[field.token].split(separator: ":").dropFirst(2).first.map(String.init) ?? "pm" : "pm" }
        if field.id == "filter.stages" { return t.count > 5 ? "2" : "1" }
        if field.id == "shape.position" { return t.count > 4 ? t[4] : "post" }
        if field.id.hasPrefix("mod."), field.id.hasSuffix(".shift"), t.count <= field.token { return "3" }
        return field.token < t.count ? t[field.token] : "0"
    }
    @discardableResult mutating func setField(_ name: String, _ value: String) -> Bool {
        guard let f = fields.first(where: { $0.id == name }) else { return false }
        guard f.unavailableReason == nil else { return false }
        var t = rows[f.row].tokens, v = value
        if let range = f.range, let n = Int(value) { v = String(min(max(n, range.lowerBound), range.upperBound)) }
        if name.hasPrefix("route.kind.") {
            guard t.indices.contains(f.token) else { return false }
            var parts = t[f.token].split(separator: ":").prefix(2).map(String.init)
            if v != "pm" { parts.append(v) }
            t[f.token] = parts.joined(separator: ":")
        } else if name == "filter.stages" {
            t = Array(padded(t, ["filter", self.name, "lp", "0", "0"]).prefix(5)); if v != "1" { t.append(v == "2" ? "x2" : v) }
        } else if name == "shape.position" {
            t = Array(padded(t, ["shape", self.name, "0", "hard"]).prefix(4)); if v != "post" { t.append(v) }
        } else {
            while t.count <= f.token { t.append("0") }
            t[f.token] = v
        }
        rows[f.row].tokens = t
        validate()
        return true
    }

    // Add rows with visible, editable values. Missing prerequisites remain
    // a harness diagnostic; addition never silently edits another feature.
    static let additions = ["shape", "amp", "egloop", "kstype", "fbtype", "mod", "eg", "ks", "noise", "pd", "vector", "formant", "ksource", "wtuse", "harmonic"]
    mutating func addStatement(_ specification: String) -> String? {
        let parts = specification.split(separator: ":").map(String.init)
        let statement = parts.first ?? "", chosen = parts.count == 2 ? Int(parts[1]) ?? -1 : 0
        guard (0..<4).contains(chosen) else { return "Choose operator 0 to 3." }
        let slot: Int
        if ["mod", "formant"].contains(statement) {
            slot = (0..<(statement == "mod" ? 8 : 3)).first { index(statement, $0) == nil } ?? -1
        } else { slot = chosen }
        if slot < 0 { return "Every \(statement) slot is already present." }
        let t: [String]
        switch statement {
        case "shape": t = [statement, name, "0", "hard"]
        case "amp": t = [statement, name, "5", "200", "120", "300"]
        case "egloop": t = [statement, name, String(slot)]
        case "kstype": t = [statement, name, String(slot), "level", "-", "-"]
        case "fbtype": t = [statement, name, String(slot), "square"]
        case "mod": t = [statement, name, String(slot), "eg0", "pitch0", "100", "3"]
        case "eg": t = [statement, name, String(slot), "0", "0", String(op(slot).sustain_db10)]
        case "ks": t = [statement, name, String(slot), "60"]
        case "noise": t = [statement, name, String(slot), "4"]
        case "pd": t = [statement, name, String(slot), "16384"]
        case "vector": t = [statement, name, "16384", "16384"]
        case "formant": t = [statement, name, String(slot), "500000", "2000", "60"]
        case "ksource": t = [statement, name, String(slot), "32700", "7", "2048"]
        case "wtuse": t = [statement, name, String(slot), bankNames.first ?? "bank", "0"]
        case "harmonic":
            if let row = rows.first(where: { $0.statement == "harmonic" && $0.tokens.count > 3 }) {
                let bank = row.tokens[1]
                let used = Set(rows.filter { $0.statement == "harmonic" && $0.tokens.count > 3 && $0.tokens[1] == bank && $0.tokens[2] == "0" }.compactMap { Int($0.tokens[3]) })
                guard let number = (1...1024).first(where: { !used.contains($0) }) else { return "Every harmonic of frame 0 is present." }
                let at = rows.lastIndex(where: { $0.statement == "harmonic" && $0.tokens.count > 1 && $0.tokens[1] == bank })! + 1
                rows.insert(Line(tokens: ["harmonic", bank, "0", String(number), "60", "0"]), at: at)
            } else {
                var bank = "harmonics", suffix = 1
                while bankNames.contains(bank) { suffix += 1; bank = "harmonics\(suffix)" }
                rows.append(Line(tokens: ["wtbank", bank, "1", "256", "12"]))
                rows.append(Line(tokens: ["harmonic", bank, "0", "1", "0", "0"]))
                put("wtuse", chosen, ["wtuse", name, String(chosen), bank, "0"])
            }
            validate(); return nil
        default: return "Unknown statement \(statement)"
        }
        let keyed = ["mod", "egloop", "kstype", "fbtype", "eg", "ks", "noise", "pd", "formant", "ksource", "wtuse"].contains(statement)
        if index(statement, keyed ? slot : nil) != nil { return "\(statement) is already present; edit its row." }
        put(statement, keyed ? slot : nil, t)
        validate(); return nil
    }

    mutating func importBank(_ bank: String, from other: PatchLines) -> String? {
        let kinds: Set<String> = ["wtbank", "wtdata", "wthash", "harmonic"]
        guard other.bankNames.contains(bank) else { return "The record has no bank named \(bank)." }
        rows.removeAll { kinds.contains($0.statement) && $0.tokens.count > 1 && $0.tokens[1] == bank }
        let bankRows = other.rows.filter { kinds.contains($0.statement) && $0.tokens.count > 1 && $0.tokens[1] == bank }
        let at = rows.firstIndex { $0.statement == "wtuse" || $0.statement == "ksource" } ?? rows.endIndex
        rows.insert(contentsOf: bankRows, at: at)
        validate(); return nil
    }
}
