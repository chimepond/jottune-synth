// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Native metadata, not a timeline validator. Values remain exact strings;
// dependencies, effective clamps and omission defaults are descriptions.

import Foundation

enum InstrTypedStatements {
    // Shared with PatchLines: membership means some typed controls exist,
    // not that every field or alternative form has a typed widget.
    static let names: Set<String> = ["patch", "route", "filter", "lfo", "peg", "fenv", "shape", "amp", "egloop", "kstype", "fbtype", "mod", "eg", "ks", "noise", "pd", "vector", "formant", "ksource", "wtuse", "harmonic"]
}

struct NativeParams: Equatable, Sendable {
    struct Row: Equatable, Sendable {
        let kind: String
        let values: [String: String]
        subscript(_ key: String) -> String { values[key] ?? "" }
    }
    let engine: String
    let rows: [Row]
    // Loaded once. A missing or malformed bundle never supplies fallback bounds.
    static let bundled: NativeParams? = {
        guard let url = Bundle.main.url(forResource: "params", withExtension: "txt"),
              let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        return NativeParams(text)
    }()
    // ///: existing raw-integer controls only. Maps tokens, not ranges,
    // seeds, steps, enum choices, coupled constraints or effective clamps.
    static let numericControls: [String: Set<Int>] = [
        "shape": [2], "amp": [2, 3, 4, 5], "egloop": [2], "kstype": [2], "fbtype": [2],
        "mod": [6], "eg": [3, 4, 5], "ks": [3], "noise": [3], "pd": [3],
        "vector": [2, 3], "formant": [3, 4, 5], "ksource": [3, 4], "wtuse": [4],
        "harmonic": [2, 3, 4, 5], "filter": [3, 4], "lfo": [4, 5, 6, 7, 8, 9],
        "peg": [2, 3, 4, 5, 6, 7, 8, 9], "fenv": [2, 3, 4, 5, 6, 7],
        "patch": [5, 6, 8, 9, 11, 12, 15, 16, 17]
    ]
    static var numericControlCount: Int { numericControls.values.reduce(0) { $0 + $1.count } }
    static let unavailableMessage = "Parameter metadata is unavailable. Edit this statement as text."
    struct NumericField: Equatable, Sendable {
        let id: String
        let range: ClosedRange<Int>
        let unit: String, scale: String, clamp: String
        var help: String {
            "Raw range \(range.lowerBound)...\(range.upperBound). Native unit: \(unit); divide raw values by \(scale). Engine clamp: \(clamp)."
        }
    }
    func numericField(statement: String, token: Int) -> NumericField? {
        guard Self.numericControls[statement]?.contains(token) == true else { return nil }
        let owners = statements.filter { $0.kind == "STATEMENT" && $0["name"] == statement }
        guard owners.count == 1 else { return nil }
        let matches = parameters.filter { $0["statement"] == owners[0]["id"] && $0["position"] == String(token) }
        guard matches.count == 1, let row = matches.first, row["form"] == "all",
              row["type"].first == "i" || row["type"].first == "u",
              let low = Int(row["lo"]), let high = Int(row["hi"]), low <= high else { return nil }
        return NumericField(id: row["id"], range: low...high, unit: row["unit"], scale: row["scale"], clamp: row["clamp"])
    }
    var missingNumericControls: [String] {
        Self.numericControls.flatMap { statement, tokens in
            tokens.filter { numericField(statement: statement, token: $0) == nil }.map { statement + ":" + String($0) }
        }.sorted()
    }
    var statements: [Row] { rows.filter { $0.kind == "STATEMENT" || $0.kind == "EVENT" } }
    var parameters: [Row] { rows.filter { $0.kind == "PARAM" } }
    var forms: [Row] { rows.filter { $0.kind == "FORM" } }
    var missingTypedStatements: [String] {
        InstrTypedStatements.names.subtracting(Set(statements.filter { $0.kind == "STATEMENT" }.map { $0["name"] })).sorted()
    }
    var statementsWithoutTypedRows: [String] {
        statements.filter { ["patch", "asset"].contains($0["section"]) && !InstrTypedStatements.names.contains($0["name"]) }
            .map { $0["name"] }.sorted()
    }

    private static let keys: [String: Set<String>] = [
        "STATEMENT": ["id", "name", "section", "once", "min_values", "max_values", "word_position"],
        "EVENT": ["id", "name", "section", "once", "min_values", "max_values", "word_position"],
        "PARAM": ["id", "statement", "name", "position", "type", "lo", "hi", "unit", "human", "scale", "clamp", "default", "mutable", "depends", "checker", "choices", "form"],
        "FORM": ["statement", "id", "position", "match", "values", "checker"],
        "FAMILY": ["id", "path", "checker"]
    ]

    private static func row(_ line: String) -> Row? {
        let words = line.split(whereSeparator: { $0 == " " || $0 == "\t" })
        guard let kind = words.first else { return nil }
        var values: [String: String] = [:]
        for word in words.dropFirst() {
            let pair = word.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard pair.count == 2, !pair[0].isEmpty, !pair[1].isEmpty, values[String(pair[0])] == nil else { return nil }
            values[String(pair[0])] = String(pair[1])
        }
        return Row(kind: String(kind), values: values)
    }

    private static func unsigned(_ text: String) -> UInt64? {
        guard !text.isEmpty, text.utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }) else { return nil }
        return UInt64(text)
    }

    private static func signed(_ text: String) -> Int64? {
        let digits = text.hasPrefix("-") ? String(text.dropFirst()) : text
        guard unsigned(digits) != nil else { return nil }
        return Int64(text)
    }

    private static func validBounds(_ row: Row) -> Bool {
        let type = row["type"]
        if ["enum", "external", "hex16", "name", "token"].contains(type) {
            return row["lo"] == "none" && row["hi"] == "none"
        }
        guard let sign = type.first, sign == "i" || sign == "u",
              let bits = Int(type.dropFirst()), [8, 16, 32, 64].contains(bits),
              type == String(sign) + String(bits),
              let low = signed(row["lo"]), let high = unsigned(row["hi"]) else { return false }
        if sign == "i" {
            let minimum = bits == 64 ? Int64.min : -(Int64(1) << (bits - 1))
            let maximum = bits == 64 ? UInt64(Int64.max) : (UInt64(1) << (bits - 1)) - 1
            return low >= minimum && high <= maximum && (low < 0 || UInt64(low) <= high)
        }
        let maximum = bits == 64 ? UInt64.max : (UInt64(1) << bits) - 1
        return low >= 0 && UInt64(low) <= high && high <= maximum
    }

    init?(_ text: String) {
        var lines = text.components(separatedBy: "\n")
        if lines.last == "" { lines.removeLast() }
        guard lines.count >= 3, let first = Self.row(lines[0]), let last = Self.row(lines[lines.count - 1]),
              first.kind == "PARAMS", first["version"] == "1",
              Set(first.values.keys) == ["version", "engine", "sine_bits", "exp_bits", "phase64", "env64", "fb_avg", "tables"],
              last.kind == "PARAMS", Set(last.values.keys) == ["count", "statements", "forms"],
              let parameterCount = Self.unsigned(last["count"]), let statementCount = Self.unsigned(last["statements"]),
              let formCount = Self.unsigned(last["forms"]) else { return nil }
        var parsed: [Row] = []
        var statementIDs = Set<String>(), parameterIDs = Set<String>(), formIDs = Set<String>(), familyIDs = Set<String>()
        for line in lines.dropFirst().dropLast() {
            guard let row = Self.row(line), let keys = Self.keys[row.kind], Set(row.values.keys) == keys else { return nil }
            switch row.kind {
            case "STATEMENT", "EVENT":
                guard statementIDs.insert(row["id"]).inserted,
                      let low = Self.signed(row["min_values"]), low >= 0,
                      let high = Self.signed(row["max_values"]), high == -1 || high >= low,
                      Self.unsigned(row["word_position"]) != nil,
                      (row.kind == "EVENT") == (row["section"] == "event") else { return nil }
            case "PARAM":
                guard parameterIDs.insert(row["id"]).inserted, Self.validBounds(row),
                      let scale = Self.unsigned(row["scale"]), scale > 0,
                      ["0", "1"].contains(row["mutable"]) else { return nil }
            case "FORM":
                guard formIDs.insert(row["statement"] + ":" + row["id"]).inserted,
                      ["count", "default_or_exact", "exact", "prefix"].contains(row["match"]) else { return nil }
            case "FAMILY": guard familyIDs.insert(row["id"]).inserted else { return nil }
            default: return nil
            }
            parsed.append(row)
        }
        guard UInt64(parameterIDs.count) == parameterCount, UInt64(statementIDs.count) == statementCount,
              UInt64(formIDs.count) == formCount, familyIDs == ["generator"] else { return nil }
        for row in parsed where row.kind == "PARAM" || row.kind == "FORM" {
            guard statementIDs.contains(row["statement"]) else { return nil }
            if row.kind == "PARAM" && row["form"] != "all" {
                guard formIDs.contains(row["statement"] + ":" + row["form"]) else { return nil }
            }
        }
        engine = first["engine"] + ["sine_bits", "exp_bits", "phase64", "env64", "fb_avg", "tables"]
            .map { " " + $0 + "=" + first[$0] }.joined()
        rows = parsed
    }

    var displayLines: [String] {
        var result = ["## Native parameters",
                      "Read-only inventory from this build's src/host/params.def: \(statements.count) statements, \(parameters.count) fields and \(forms.count) alternative forms. The native reader validates authored input and coupled constraints. \(Self.numericControlCount) existing numeric controls use this table's raw bounds; other controls retain their existing settings.",
                      "Bounds below are accepted raw limits; engine clamps are separate. Defaults describe omitted tokens, omitted statements or initial state, not factory sound settings. No editing steps are provided by this table.",
                      "Typed patch/asset statement families: \(InstrTypedStatements.names.count). Membership means some typed controls exist, not complete field or widget coverage.",
                      "Patch/asset statements without a typed row: " + (statementsWithoutTypedRows.isEmpty ? "none" : statementsWithoutTypedRows.joined(separator: ", ")) + "."]
        if !missingTypedStatements.isEmpty {
            result.append("Editor statements missing native metadata: " + missingTypedStatements.joined(separator: ", ") + ".")
        }
        for statement in statements {
            result.append("### " + statement["name"] + " (" + statement["section"] + ")")
            for parameter in parameters where parameter["statement"] == statement["id"] {
                let bounds = parameter["lo"] == "none" ? "nonnumeric" : parameter["lo"] + " to " + parameter["hi"]
                result.append("- **" + parameter["name"] + "** [" + parameter["id"] + "] at " + parameter["position"] +
                              ": " + parameter["type"] + "; raw bounds " + bounds + "; unit " + parameter["unit"] +
                              "; human divisor " + parameter["scale"] + "; clamp " + parameter["clamp"] +
                              "; omission " + parameter["default"] + "; " + (parameter["mutable"] == "1" ? "event mutable" : "no event mutability declared") +
                              "; form " + parameter["form"] + "; choices " + parameter["choices"] + "; constraints " + parameter["depends"] + ".")
            }
        }
        return result
    }
}
