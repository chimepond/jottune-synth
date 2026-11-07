// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// PatchEditor.swift: every field of PatchLines with its units and the
// library's ranges (timeline grammar and fm_a1.h). Operators: two on
// the fixed path, route_ops under a route; then the lfo, peg and fenv
// statements . The text fields take the full range; the sliders cover
// the useful part of it. A field the engine ignores in this patch is dimmed,
// stays editable and says why (instrInactive, instrStmtInactive).

import SwiftUI

private let filterNames = ["none", "lp", "bp", "hp", "notch"]
private let opColors: [Color] = [.orange, .teal, .purple, .pink]

/// Why the engine ignores operator i's fields in this patch, by field label;
/// a field not listed reaches the sample. The rules are fm_a1.c's:
/// the fixed path (fm_voice_step) reads op 0's level, op 1's index and
/// feedback, and both waves and envelopes; a routed voice
/// (fm_voice_step_routed) sums only the carriers' levels and reads an
/// operator's index only through its edges (fm_set_edges); key scaling
/// scales the index alone (fm_keyscale_q30, 1.0 at slope 0); the decay ends
/// at the sustain level, so it moves nothing at sustain 0 dB.
func instrInactive(_ p: PatchLines, _ i: Int) -> [String: String] {
    var r: [String: String] = [:]
    let op = opArray(p)[i]
    let all = ["ratio", "level", "index", "feedback", "vel sens", "attack", "decay", "sustain", "release",
               "wave", "rate scale", "ks break", "ks slope", "fixed", "phase", "free run"]
    if p.route_ops == 0 {
        if i == 0 {
            r["index"] = "the fixed path ignores op 0's index"
            r["feedback"] = "the fixed path ignores op 0's feedback"
            r["ks break"] = "key scaling scales the index, which the fixed path ignores on op 0"
            r["ks slope"] = r["ks break"]
        } else {
            r["level"] = "the fixed path ignores op 1's level: op 1 only modulates op 0"
        }
    } else {
        let n = Int(p.route_ops)
        let carrier = p.route_carriers & (1 << UInt32(i)) != 0
        let edges = (0..<n).contains { $0 != i && routeWeight(p, i, $0) != 0 }
        if !carrier && !edges {
            for f in all { r[f] = "op \(i) is not a carrier and has no edge out: it plays no part" }
            return r
        }
        if !carrier { r["level"] = "op \(i) is not a carrier: its level is unused, its index sets the depth" }
        if !edges {
            r["index"] = "op \(i) has no edge out: its index is unused"
            r["ks break"] = "key scaling scales the index, which op \(i) does not use"
            r["ks slope"] = r["ks break"]
        }
    }
    if op.ks_slope_db10 == 0 && r["ks break"] == nil { r["ks break"] = "ks slope is 0: no key scaling" }
    if op.sustain_db10 == 0 && r["decay"] == nil { r["decay"] = "sustain is 0 dB: the decay ends where it starts" }
    return r
}

/// Why the engine ignores a field of the lfo, peg or fenv statement, by
/// script name (InstrCtl.stmt). The rules are fm_a1.h's: rate 0 is no
/// LFO; a depth reaches only the operators its mask names, and a mask moves
/// nothing at depth 0; a fixed-frequency operator takes no pitch offset; a
/// peg with all levels 0 is off whatever its times and mask; the fenv acts
/// only with a filter (and the grammar refuses it without one, ) and
/// when its depth or tracking is not 0; its envelope moves nothing at depth
/// 0, the reference is unused without tracking, and the decay ends where it
/// starts at sustain 1000.
func instrStmtInactive(_ p: PatchLines) -> [String: String] {
    var r: [String: String] = [:]
    let l = p.lfo
    if l.rate_millihz == 0 {
        for k in InstrCtl.lfoKeys where k != "lfo.rate" { r[k] = "rate 0: no lfo statement" }
    } else {
        let nops = p.route_ops == 0 ? 2 : Int(p.route_ops)
        var fixed = 0
        for i in 0..<nops where opArray(p)[i].fixed_millihz != 0 { fixed |= 1 << i }
        let pairs: [(String, String, UInt32, UInt32, String)] = [
            ("lfo.pitch", "lfo.mask_pitch", l.pitch_cents, l.mask_pitch, "pitch"),
            ("lfo.level", "lfo.mask_level", l.level_db10, l.mask_level, "level"),
            ("lfo.index", "lfo.mask_index", l.index_q10, l.mask_index, "index"),
        ]
        var reach = false
        for (d, m, depth, mask, what) in pairs {
            var takes = Int(mask)
            if what == "pitch" { takes &= ~fixed }
            if depth == 0 { r[m] = "\(what) depth 0: the mask moves nothing" }
            if depth != 0 && mask == 0 { r[d] = "no operator takes the \(what) offset: its mask is 0" }
            else if depth != 0 && takes == 0 {
                r[d] = "the operators its mask names are fixed-frequency: they take no pitch offset"
                r[m] = r[d]
            }
            if depth != 0 && takes != 0 { reach = true }
        }
        if !reach {
            for k in ["lfo.rate", "lfo.wave", "lfo.delay", "lfo.fade", "lfo.phase", "lfo.free_run"] {
                r[k] = "no depth reaches an operator"
            }
        }
    }
    let g = InstrCtl.pegKeys.filter { $0.hasPrefix("peg.l") }.allSatisfy { InstrCtl.getPatch(p, $0) == 0 }
    if g {
        for k in InstrCtl.pegKeys where !k.hasPrefix("peg.l") { r[k] = "all levels 0: the pitch envelope is off" }
    }
    let f = p.fenv
    if p.filter_mode == 0 {
        for k in InstrCtl.fenvKeys { r[k] = "no filter: the fenv acts on the filter, and the grammar refuses it without one ()" }
    } else if f.depth_cents == 0 && f.track_permille == 0 {
        for k in InstrCtl.fenvKeys where k != "fenv.depth" && k != "fenv.track" { r[k] = "depth and track 0: the fenv is off" }
    } else {
        if f.depth_cents == 0 {
            for k in ["fenv.attack", "fenv.decay", "fenv.sustain", "fenv.release"] { r[k] = "depth 0: the envelope moves nothing" }
        } else if f.sustain_permille == 1000 {
            r["fenv.decay"] = "sustain 1000: the decay ends where it starts"
        }
        if f.track_permille == 0 { r["fenv.ref"] = "track 0: no key tracking, so no reference" }
    }
    return r
}

/// What the timeline grammar would refuse in this patch's lfo, peg or fenv,
/// as Apply's error would say it; empty when it takes the patch.
func instrStmtProblems(_ p: PatchLines) -> [String] { p.diagnostic.map { [$0] } ?? [] }

extension View {
    /// Dims a control the engine ignores and gives the reason as its help.
    @ViewBuilder func inactiveLook(_ why: String?) -> some View {
        if let w = why { self.opacity(0.45).help("inactive: " + w) } else { self }
    }
}

/// One numeric field: label, slider, text field, unit and a readout.
struct InstrParam: View {
    let label: String
    @Binding var value: UInt32
    let range: ClosedRange<UInt32>          // what the library takes
    var slider: ClosedRange<Double>? = nil  // nil: no slider
    var unit: String = ""
    var readout: ((UInt32) -> String)? = nil
    var inactive: String? = nil             // why the engine ignores it, nil when it does not

    var body: some View {
        HStack(spacing: 6) {
            Text(label).frame(width: 70, alignment: .leading)
            if let s = slider {
                Slider(value: Binding(get: { min(max(Double(value), s.lowerBound), s.upperBound) },
                                      set: { value = clamp(UInt64($0.rounded())) }),
                       in: s)
                    .frame(minWidth: 60)
            }
            TextField("", value: Binding(get: { Int(value) }, set: { value = clamp(UInt64(max(0, $0))) }),
                      format: .number.grouping(.never))
                .frame(width: 64)
                .multilineTextAlignment(.trailing)
            Text(unit).foregroundStyle(.secondary).frame(width: 46, alignment: .leading)
            if let r = readout {
                Text(r(value)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    .frame(width: 64, alignment: .leading)
            }
        }
        .font(.callout)
        .inactiveLook(inactive)
    }

    private func clamp(_ v: UInt64) -> UInt32 {
        UInt32(min(max(v, UInt64(range.lowerBound)), UInt64(range.upperBound)))
    }
}

/// One signed numeric field of a statement (InstrCtl.stmt): label, slider,
/// text field, unit and a readout, through the field's setter.
struct InstrIntParam: View {
    let label: String
    @Binding var value: Int
    let range: ClosedRange<Int>
    var slider: ClosedRange<Double>? = nil
    var unit: String = ""
    var readout: ((Int) -> String)? = nil
    var inactive: String? = nil

    var body: some View {
        HStack(spacing: 6) {
            Text(label).frame(width: 70, alignment: .leading)
            if let s = slider {
                Slider(value: Binding(get: { min(max(Double(value), s.lowerBound), s.upperBound) },
                                      set: { value = min(max(Int($0.rounded()), range.lowerBound), range.upperBound) }),
                       in: s)
                    .frame(minWidth: 60)
            }
            TextField("", value: Binding(get: { value }, set: { value = min(max($0, range.lowerBound), range.upperBound) }),
                      format: .number.grouping(.never))
                .frame(width: 64)
                .multilineTextAlignment(.trailing)
            Text(unit).foregroundStyle(.secondary).frame(width: 46, alignment: .leading)
            if let r = readout {
                Text(r(value)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    .frame(width: 72, alignment: .leading)
            }
        }
        .font(.callout)
        .inactiveLook(inactive)
    }
}

private let lfoWaveNames = ["sine", "triangle", "saw up", "saw down", "square", "sample and hold", "smoothed random"]

private func db10(_ v: UInt32) -> String { String(format: "-%.1f dB", Double(v) / 10) }
private func rad(_ v: UInt32) -> String { String(format: "%.3f rad", Double(v) / 1024) }
private func hz(_ v: UInt32) -> String { v == 0 ? "-" : String(format: "%.2f Hz", Double(v) / 1000) }

/// The editor for one operator. Every field writes through field(key),
/// which PatchEditor builds on InstrCtl.setPatch, the setter FMS_SCRIPT calls.
struct InstrOpEditor: View {
    let op: fms_op_t
    let index: Int
    let note: String
    var inactive: [String: String] = [:]
    let field: (String) -> Binding<UInt32>
    let metadata: (String) -> NativeParams.NumericField?

    @ViewBuilder private func nativeParam(_ key: String, label: String, slider: ClosedRange<Double>? = nil,
                                          unit: String = "", readout: ((UInt32) -> String)? = nil) -> some View {
        if let descriptor = metadata(key) {
            InstrParam(label: label, value: field(key),
                       range: UInt32(descriptor.range.lowerBound)...UInt32(descriptor.range.upperBound),
                       slider: slider, unit: unit, readout: readout, inactive: inactive[label])
                .help(descriptor.help + (inactive[label].map { " Inactive: " + $0 + "." } ?? ""))
        } else { unavailable(key, label: label, unit: unit) }
    }

    private func unavailable(_ key: String, label: String, unit: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(label).frame(width: 70, alignment: .leading)
                TextField("", value: field(key), format: .number.grouping(.never))
                    .frame(width: 64).multilineTextAlignment(.trailing).disabled(true)
                Text(unit).foregroundStyle(.secondary)
            }.font(.callout).inactiveLook(inactive[label])
            Text(NativeParams.unavailableMessage).font(.caption).foregroundStyle(.orange)
        }.help(NativeParams.unavailableMessage)
    }

    @ViewBuilder private var rateScale: some View {
        if let descriptor = metadata("rate_scale") {
            HStack {
                Text("rate scale").frame(width: 70, alignment: .leading)
                Stepper("\(op.rate_scale)", value: Binding(
                    get: { min(max(Int(op.rate_scale), descriptor.range.lowerBound), descriptor.range.upperBound) },
                    set: { field("rate_scale").wrappedValue = UInt32($0) }), in: descriptor.range)
                Text("\(descriptor.range.lowerBound) to \(descriptor.range.upperBound)").foregroundStyle(.secondary)
            }.inactiveLook(inactive["rate scale"])
                .help(descriptor.help + (inactive["rate scale"].map { " Inactive: " + $0 + "." } ?? ""))
        } else { unavailable("rate_scale", label: "rate scale", unit: "") }
    }

    var body: some View {
        GroupBox {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .top, spacing: 12) { fieldsA; fieldsB }
                VStack(alignment: .leading, spacing: 8) { fieldsA; fieldsB }
            }
        } label: {
            HStack {
                Circle().fill(opColors[index % opColors.count]).frame(width: 8, height: 8)
                Text("op \(index)").bold()
                Text(note).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var fieldsA: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text("ratio").frame(width: 70, alignment: .leading)
                Slider(value: Binding(get: { min(Double(op.ratio_q16) / 65536, 16) },
                                      set: { field("ratio").wrappedValue = UInt32(($0 * 4).rounded() / 4 * 65536) }),
                       in: 0...16).frame(minWidth: 60)
                TextField("", value: Binding(get: { Double(op.ratio_q16) / 65536 },
                                             set: { field("ratio").wrappedValue = UInt32(min(max($0, 0), 65535.99998) * 65536 + 0.5) }),
                          format: .number.precision(.fractionLength(0...5)).grouping(.never))
                    .frame(width: 64).multilineTextAlignment(.trailing)
                Text("x note").foregroundStyle(.secondary).frame(width: 46, alignment: .leading)
                Text("q16 " + String(op.ratio_q16)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }.font(.callout).inactiveLook(inactive["ratio"])
            InstrParam(label: "level", value: field("level"), range: 0...1200, slider: 0...1200,
                       unit: "dB10", readout: db10,
                       inactive: inactive["level"])
            nativeParam("index", label: "index", slider: 0...16384, unit: "Q10", readout: rad)
            nativeParam("feedback", label: "feedback", slider: 0...4096, unit: "Q10", readout: rad)
            InstrParam(label: "vel sens", value: field("vel_sens"), range: 0...256, slider: 0...256,
                       unit: "/256",
                       inactive: inactive["vel sens"])
            nativeParam("attack", label: "attack", slider: 0...3000, unit: "ms")
            nativeParam("decay", label: "decay", slider: 0...5000, unit: "ms")
            InstrParam(label: "sustain", value: field("sustain"), range: 0...1200, slider: 0...1200,
                       unit: "dB10", readout: db10,
                       inactive: inactive["sustain"])
            nativeParam("release", label: "release", slider: 0...5000, unit: "ms")
        }
    }

    private var fieldsB: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("wave").frame(width: 70, alignment: .leading)
                // fm_a1.h's waves 0 to 11 (inspWaveNames); any other value plays as 0
                Picker("", selection: Binding(get: { op.wave < UInt32(inspWaveNames.count) ? Int(op.wave) : 0 },
                                              set: { field("wave").wrappedValue = UInt32($0) })) {
                    ForEach(0..<inspWaveNames.count, id: \.self) { Text("\($0) \(inspWaveNames[$0])").tag($0) }
                }.labelsHidden().frame(width: 130)
            }.inactiveLook(inactive["wave"])
            rateScale
            nativeParam("ks_break", label: "ks break", unit: "mHz", readout: hz)
            InstrParam(label: "ks slope", value: field("ks_slope"), range: 0...1200, slider: 0...1200,
                       unit: "dB10", readout: { String(format: "-%.1f dB/oct", Double($0) / 10) },
                       inactive: inactive["ks slope"])
            nativeParam("fixed", label: "fixed", unit: "mHz", readout: { $0 == 0 ? "follows" : hz($0) })
            nativeParam("phase", label: "phase", slider: 0...65535,
                        unit: "/65536", readout: { String(format: "%.1f deg", Double($0) * 360 / 65536) })
            HStack {
                Text("free run").frame(width: 70, alignment: .leading)
                // fm_a1.h : 0 key sync, 1 frozen while idle, 2 continuous;
                // other nonzero values play as 1
                Picker("", selection: Binding(get: { op.free_run == 0 ? 0 : (op.free_run == 2 ? 2 : 1) },
                                              set: { field("free_run").wrappedValue = UInt32($0) })) {
                    Text("0 key sync").tag(0)
                    Text("1 frozen while idle").tag(1)
                    Text("2 continuous").tag(2)
                }.labelsHidden().frame(width: 170)
            }.inactiveLook(inactive["free run"])
            EnvelopeView(op: op, color: opColors[index % opColors.count])
                .frame(height: 80)
        }
    }
}

/// The patch: operators, route and filter. Every control writes through
/// ctl(name), that is InstrCtl.setPatch, the setter FMS_SCRIPT calls by name.
struct PatchEditor: View {
    var learn: InstrLearnControls? = nil
    @Binding var patch: PatchLines
    @State private var bankRecord: PatchLines?
    @State private var selectedBank = ""
    @State private var bankError: String?

    private var nops: Int { patch.route_ops == 0 ? 2 : Int(min(patch.route_ops, UInt32(FMS_OPS))) }

    private func ctl(_ name: String) -> Binding<Int> {
        Binding(get: { InstrCtl.getPatch(patch, name) ?? 0 }, set: { InstrCtl.setPatch(&patch, name, $0) })
    }

    private func u32(_ name: String) -> Binding<UInt32> {
        Binding(get: { UInt32(InstrCtl.getPatch(patch, name) ?? 0) }, set: { InstrCtl.setPatch(&patch, name, Int($0)) })
    }

    private func opNote(_ i: Int) -> String {
        let role: String
        if patch.route_ops == 0 {
            role = i == 0 ? "carrier" : "modulator of op 0"
        } else {
            role = patch.route_carriers & (1 << UInt32(i)) != 0 ? "carrier" : "modulator"
        }
        let off = instrInactive(patch, i)
        if off.isEmpty { return role }
        let order = ["ratio", "level", "index", "feedback", "vel sens", "attack", "decay", "sustain", "release",
                     "wave", "rate scale", "ks break", "ks slope", "fixed", "phase", "free run"]
        let names = order.filter { off[$0] != nil }
        return role + " (inactive here: " + (names.count == order.count ? "every field" : names.joined(separator: ", ")) + ")"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            routeEditor
            filterEditor
            ForEach(0..<nops, id: \.self) { i in
                InstrOpEditor(op: opArray(patch)[i], index: i, note: opNote(i), inactive: instrInactive(patch, i),
                              field: { u32("op\(i).\($0)") }, metadata: { patch.operatorField($0) })
            }
            stmtEditor
            extraRows
            rawLines
        }
    }

    @ViewBuilder private func learnRow(_ field: PatchLines.Field) -> some View {
        if let learn, field.id.hasPrefix("mod."), field.id.hasSuffix(".source"),
           let slot = Int(field.id.split(separator: ".")[1]), learn.inputs.indices.contains(slot) {
            HStack {
                Text("slot \(slot) CC learn").font(.caption)
                Picker("Controller input", selection: Binding(get: { learn.inputs[slot] }, set: { learn.select(slot, $0) })) {
                    ForEach(0..<8, id: \.self) { Text("ctl\($0)").tag($0) }
                }.frame(width: 130).disabled(learn.pending != nil).accessibilityIdentifier("learn.\(slot).input")
                Button("Learn") { learn.begin(slot) }.disabled(learn.pending != nil).accessibilityIdentifier("learn.\(slot).start")
                Text("Stages source + channel map; Apply installs.").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var extraRows: some View {
        VStack(alignment: .leading, spacing: 8) {
            Menu("Add statement") {
                ForEach(PatchLines.additions, id: \.self) { kind in
                    if ["eg", "egloop", "kstype", "fbtype", "ks", "noise", "pd", "ksource", "wtuse"].contains(kind) {
                        Menu(kind) { ForEach(0..<4, id: \.self) { op in Button("operator \(op)") { bankError = patch.addStatement("\(kind):\(op)") } } }
                    } else { Button(kind) { bankError = patch.addStatement(kind) } }
                }
            }
            ForEach(patch.fields) { f in
                HStack {
                    Text(f.label).frame(width: 160, alignment: .leading)
                    let value = Binding(get: { patch.value(f) }, set: { _ = patch.setField(f.id, $0) })
                    if !f.choices.isEmpty {
                        Picker(f.label, selection: value) { ForEach(f.choices, id: \.self) { Text($0).tag($0) } }.labelsHidden()
                    } else { TextField(f.label, text: value).textFieldStyle(.roundedBorder) }
                    Text(f.unit).font(.caption).foregroundStyle(.secondary).frame(width: 80, alignment: .leading)
                }.font(.callout)
                .disabled(f.unavailableReason != nil)
                .help(f.unavailableReason ?? f.native?.help ?? "")
                if let reason = f.unavailableReason {
                    Text(reason).font(.caption).foregroundStyle(.orange)
                }
                learnRow(f)
            }
            if let learn, !learn.message.isEmpty {
                HStack {
                    Text(learn.message).font(.caption).textSelection(.enabled)
                    if learn.pending != nil { Button("Cancel Learn", action: learn.cancel).accessibilityIdentifier("learn.cancel") }
                }
            }
            Text("Mod sources include ctl0…ctl7 and gen.ID.x/y. Target-specific ranges and prerequisites are checked by the harness.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Load bank from record…") { loadBankRecord() }
                if let source = bankRecord {
                    Picker("Bank", selection: $selectedBank) { ForEach(source.bankNames, id: \.self) { Text($0).tag($0) } }
                    Button("Use bank") { bankError = patch.importBank(selectedBank, from: source) }.disabled(selectedBank.isEmpty)
                }
            }
            if let e = bankError { Text(e).font(.caption).foregroundStyle(.red) }
        }
    }

    private func loadBankRecord() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        guard let p = InstrPatchIO.withCStrings([url.path], { fms_player_open($0, 1, &err, err.count) }) else {
            bankError = instrCStr(err); return
        }
        defer { fms_player_close(p) }
        var info = fms_info_t(); fms_player_info(p, &info)
        bankRecord = PatchLines.read(p, 0, fs: info.fs)
        selectedBank = bankRecord?.bankNames.first ?? ""
        bankError = selectedBank.isEmpty ? "The record contains no wavetable bank." : nil
    }

    private var rawLines: some View {
        let indices = patch.rows.indices.filter { patch.diagnostic != nil || !PatchLines.typedStatements.contains(patch.rows[$0].statement) }
        return DisclosureGroup("Raw statement lines (\(indices.count))") {
            LazyVStack(alignment: .leading, spacing: 4) {
                ForEach(indices, id: \.self) { i in
                    if patch.rows.indices.contains(i) {
                        TextField(patch.rows[i].statement, text: Binding(
                            get: { patch.rows.indices.contains(i) ? patch.rows[i].text : "" },
                            set: { patch.editRow(i, text: $0) }))
                            .font(.system(.caption, design: .monospaced))
                    }
                }
            }
        }
    }

    // ---- lfo, peg, fenv  ----

    @ViewBuilder private func sparam(_ key: String, slider: ClosedRange<Double>? = nil, readout: ((Int) -> String)? = nil,
                        _ off: [String: String]) -> some View {
        let f = InstrCtl.stmt[key]!
        if let field = patch.statementField(key) {
            InstrIntParam(label: f.label, value: ctl(key), range: field.range, slider: slider, unit: f.unit,
                          readout: readout, inactive: off[key])
                .help(field.help + (off[key].map { " Inactive: " + $0 + "." } ?? ""))
        } else if let range = f.range {
            InstrIntParam(label: f.label, value: ctl(key), range: range, slider: slider, unit: f.unit,
                          readout: readout, inactive: off[key])
        } else {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(f.label).frame(width: 70, alignment: .leading)
                    TextField("", value: ctl(key), format: .number.grouping(.never))
                        .frame(width: 64).multilineTextAlignment(.trailing).disabled(true)
                    Text(f.unit).foregroundStyle(.secondary)
                }.font(.callout).inactiveLook(off[key])
                Text(NativeParams.unavailableMessage).font(.caption).foregroundStyle(.orange)
            }.help(NativeParams.unavailableMessage)
        }
    }

    /// One toggle per operator of the patch for a mask field.
    private func maskRow(_ key: String, _ off: [String: String], note: String = "") -> some View {
        let f = InstrCtl.stmt[key]!
        return HStack {
            Text(f.label).frame(width: 70, alignment: .leading)
            ForEach(0..<nops, id: \.self) { i in
                Toggle("op \(i)", isOn: Binding(
                    get: { ctl(key).wrappedValue & (1 << i) != 0 },
                    set: { ctl(key).wrappedValue = $0 ? ctl(key).wrappedValue | (1 << i) : ctl(key).wrappedValue & ~(1 << i) }))
            }
            if !note.isEmpty { Text(note).font(.caption).foregroundStyle(.secondary) }
        }.font(.callout).inactiveLook(off[key])
    }

    private var stmtEditor: some View {
        let off = instrStmtInactive(patch)
        let problems = instrStmtProblems(patch)
        return VStack(alignment: .leading, spacing: 10) {
            GroupBox("LFO (lfo statement)") {
                VStack(alignment: .leading, spacing: 3) {
                    sparam("lfo.rate", slider: 0...20000, readout: { $0 == 0 ? "off" : String(format: "%.2f Hz", Double($0) / 1000) }, off)
                    HStack {
                        Text("wave").frame(width: 70, alignment: .leading)
                        Picker("", selection: ctl("lfo.wave")) {
                            ForEach(0..<lfoWaveNames.count, id: \.self) { Text("\($0) \(lfoWaveNames[$0])").tag($0) }
                        }.labelsHidden().frame(width: 170)
                    }.font(.callout).inactiveLook(off["lfo.wave"])
                    sparam("lfo.delay", slider: 0...3000, off)
                    sparam("lfo.fade", slider: 0...3000, off)
                    sparam("lfo.pitch", slider: 0...1200, off)
                    maskRow("lfo.mask_pitch", off)
                    sparam("lfo.level", slider: 0...1200, readout: { String(format: "-%.1f dB", Double($0) / 10) }, off)
                    maskRow("lfo.mask_level", off)
                    sparam("lfo.index", slider: 0...8192, readout: { String(format: "%.3f rad", Double($0) / 1024) }, off)
                    maskRow("lfo.mask_index", off)
                    sparam("lfo.phase", slider: 0...65535, readout: { String(format: "%.1f deg", Double($0) * 360 / 65536) }, off)
                    HStack {
                        Text("free run").frame(width: 70, alignment: .leading)
                        Picker("", selection: ctl("lfo.free_run")) {
                            Text("0 key sync").tag(0)
                            Text("2 continuous").tag(2)
                        }.labelsHidden().frame(width: 170)
                    }.font(.callout).inactiveLook(off["lfo.free_run"])
                    Text("Rate 0 writes no lfo statement. The masks name the operators each depth reaches.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            GroupBox("Pitch envelope (peg statement)") {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(1...4, id: \.self) { k in
                        HStack(spacing: 12) {
                            sparam("peg.l\(k)", slider: -2400...2400, off)
                            sparam("peg.t\(k)", slider: 0...3000, off)
                        }
                    }
                    maskRow("peg.mask", off, note: "none ticked: every operator")
                    Text("From silence it starts at L4, glides to L1, L2 and L3, holds L3, then releases to L4.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            GroupBox("Filter envelope (fenv statement)") {
                VStack(alignment: .leading, spacing: 3) {
                    sparam("fenv.depth", slider: -4800...4800, off)
                    sparam("fenv.attack", slider: 0...3000, off)
                    sparam("fenv.decay", slider: 0...3000, off)
                    sparam("fenv.sustain", slider: 0...1000, off)
                    sparam("fenv.release", slider: 0...3000, off)
                    sparam("fenv.track", slider: 0...2000, off)
                    sparam("fenv.ref", readout: { $0 == 0 ? "261.626 Hz" : String(format: "%.3f Hz", Double($0) / 1000) }, off)
                    Text("It moves the filter's cutoff, so it needs a filter mode; ref 0 writes 261626.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            ForEach(problems, id: \.self) { Text("Apply would be refused: " + $0).font(.caption).foregroundStyle(.red) }
        }
    }

    // ---- route ----

    private var routeEditor: some View {
        GroupBox("Route") {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Picker("operators", selection: ctl("route.ops")) {
                        Text("fixed 2-op (op 1 -> op 0)").tag(0)
                        Text("2").tag(2)
                        Text("3").tag(3)
                        Text("4").tag(4)
                    }.frame(width: 300)
                    if patch.route_ops == 0 {
                        Text("carriers and edge weights: inactive on the fixed path, which plays op 1 into op 0 "
                             + "at op 1's index; pick 2 to 4 operators to edit them")
                            .font(.caption).foregroundStyle(.secondary).opacity(0.7)
                    }
                    if patch.route_ops != 0 {
                        let n = Int(patch.route_ops)
                        HStack {
                            Text("carriers")
                            ForEach(0..<n, id: \.self) { i in
                                Toggle("op \(i)", isOn: Binding(
                                    get: { ctl("route.carrier.\(i)").wrappedValue != 0 },
                                    set: { ctl("route.carrier.\(i)").wrappedValue = $0 ? 1 : 0 }))
                            }
                        }
                        Text("edge weights from (row) to (column): 0 no edge, 1 to 1024 in 1/256 (256 unity)")
                            .font(.caption).foregroundStyle(.secondary)
                        Grid(alignment: .trailing, horizontalSpacing: 6, verticalSpacing: 4) {
                            GridRow {
                                Text("from \\ to").font(.caption)
                                ForEach(0..<n, id: \.self) { t in Text("op \(t)").font(.caption) }
                            }
                            ForEach(0..<n, id: \.self) { f in
                                GridRow {
                                    Text("op \(f)").font(.caption)
                                    ForEach(0..<n, id: \.self) { t in
                                        if f == t {
                                            Text("-").foregroundStyle(.secondary).frame(width: 56)
                                        } else {
                                            TextField("", value: ctl("route.w.\(f).\(t)"),
                                                      format: .number.grouping(.never))
                                                .frame(width: 56).multilineTextAlignment(.trailing)
                                                .inactiveLook(routeWeight(patch, f, t) != 0 && opArray(patch)[f].index_q10 == 0
                                                               ? "op \(f)'s index is 0: this edge adds nothing" : nil)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                RouteGraph(patch: patch)
                    .frame(width: 220, height: 150)
            }
        }
    }

    // ---- filter ----

    @ViewBuilder private func filterParam(_ key: String, label: String, slider: ClosedRange<Double>,
                                          unit: String, readout: @escaping (UInt32) -> String,
                                          inactive: String?) -> some View {
        if let field = patch.filterField(key) {
            InstrParam(label: label, value: u32(key),
                       range: UInt32(field.range.lowerBound)...UInt32(field.range.upperBound),
                       slider: slider, unit: unit, readout: readout, inactive: inactive)
                .help(field.help + (inactive.map { " Inactive: " + $0 + "." } ?? ""))
        } else {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(label).frame(width: 70, alignment: .leading)
                    TextField("", value: ctl(key), format: .number.grouping(.never))
                        .frame(width: 64).multilineTextAlignment(.trailing).disabled(true)
                    Text(unit).foregroundStyle(.secondary)
                }.font(.callout).inactiveLook(inactive)
                Text(NativeParams.unavailableMessage).font(.caption).foregroundStyle(.orange)
            }.help(NativeParams.unavailableMessage)
        }
    }

    private var filterEditor: some View {
        GroupBox("Filter") {
            VStack(alignment: .leading, spacing: 4) {
                Picker("mode", selection: Binding(get: { min(ctl("filter.mode").wrappedValue, 4) },
                                                  set: { ctl("filter.mode").wrappedValue = $0 })) {
                    ForEach(0..<5, id: \.self) { Text(filterNames[$0]).tag($0) }
                }
                .pickerStyle(.segmented).frame(width: 320)
                let off: String? = patch.filter_mode == 0 ? "mode none: no filter" : nil
                filterParam("filter.cutoff", label: "cutoff", slider: 20_000...20_000_000,
                            unit: "mHz", readout: hz, inactive: off)
                filterParam("filter.q", label: "Q", slider: 500...16000,
                            unit: "milli", readout: { String(format: "%.3f", Double($0) / 1000) }, inactive: off)
                Text(off == nil ? "The engine clamps the cutoff to fs/2400 .. 0.45 fs and Q to 0.5 .. 16."
                                : "Cutoff and Q are inactive while the mode is none; a mode writes the filter statement.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
