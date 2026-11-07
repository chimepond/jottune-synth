// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Checks the production Features parser without an audio device or app bundle.
import SwiftUI

// FeaturesContent needs only this layout dependency; parsing is unchanged.
struct Card<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content
    var body: some View { content }
}

@main struct CatalogCheck {
    static func main() throws {
        if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--mutate-param-range" {
            let raw = try String(contentsOfFile: CommandLine.arguments[2], encoding: .utf8)
            guard NativeParams(raw) != nil else { fatalError("native catalog refused before mutation") }
            print(changedRange(raw), terminator: "")
            return
        }
        guard CommandLine.arguments.count == 3 else { fatalError("usage: catalog_verify GEN_PARAMS PARAMS") }
        let raw = try String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8)
        guard let rows = FeaturesView.generatorParameters(raw) else { fatalError("native catalog refused") }
        let parameters = raw.split(separator: "\n").filter { $0.hasPrefix("GEN_PARAM ") }
        precondition(rows.count == parameters.count + 2)
        for parameter in parameters {
            let fields = Dictionary(uniqueKeysWithValues: parameter.split(separator: " ").dropFirst().map {
                let pair = $0.split(separator: "=", maxSplits: 1)
                return (String(pair[0]), String(pair[1]))
            })
            let row = rows.first { $0.contains("(ID " + fields["id"]! + ";") }!
            precondition(row.contains(fields["min"]! + " to " + fields["max"]! + " " + fields["unit"]!))
            precondition(row.contains(fields["has_default"] == "1" ? "default " + fields["default"]! : "no default"))
            precondition(row.contains(fields["mutable"] == "1" ? "runtime updates" : "header only"))
        }
        // Mutate actual parsed rows, so controls remain meaningful when the
        // catalog grows or its IDs, names, units and kind membership change.
        let lines = raw.split(separator: "\n").map(String.init)
        let firstParameter = lines.firstIndex { $0.hasPrefix("GEN_PARAM ") }!
        let firstKind = lines.firstIndex { $0.hasPrefix("GEN_KIND ") }!
        func fields(_ line: String) -> [String: String] {
            Dictionary(uniqueKeysWithValues: line.split(separator: " ").dropFirst().map {
                let pair = $0.split(separator: "=", maxSplits: 1)
                return (String(pair[0]), String(pair[1]))
            })
        }
        func changedRow(_ index: Int, _ key: String, _ value: String?) -> String {
            var copy = lines
            copy[index] = copy[index].split(separator: " ").compactMap { word -> String? in
                guard word.hasPrefix(key + "=") else { return String(word) }
                return value.map { key + "=" + $0 }
            }.joined(separator: " ")
            return copy.joined(separator: "\n") + "\n"
        }
        func duplicatedRow(_ index: Int, parameter: Bool) -> String {
            var copy = lines
            copy.insert(copy[index], at: index + 1)
            if parameter { copy[copy.count - 1] = "GEN_PARAMS count=\(parameters.count + 1)" }
            return copy.joined(separator: "\n") + "\n"
        }
        let first = fields(lines[firstParameter])
        let kind = fields(lines[firstKind])
        let version = Int(fields(lines[0])["version"]!)!
        let count = Int(fields(lines.last!)["count"]!)!
        let kindMask = lines.filter { $0.hasPrefix("GEN_KIND ") }.reduce(0) {
            $0 | (1 << (Int(fields($1)["id"]!)! - 1))
        }
        // The production parser admits kind IDs 1...16; at least bit16 is free.
        let outsideKind = (0...16).map { 1 << $0 }.first { kindMask & $0 == 0 }!
        let controls: [(name: String, text: String)] = [
            ("version", changedRow(0, "version", String(version + 1))),
            ("count", changedRow(lines.count - 1, "count", String(count - 1))),
            ("duplicate parameter ID \(first["id"]!) (\(first["name"]!))", duplicatedRow(firstParameter, parameter: true)),
            ("invalid minimum for \(first["name"]!)", changedRow(firstParameter, "min", "invalid")),
            ("unknown kind bit", changedRow(firstParameter, "kinds", String(Int(first["kinds"]!)! | outsideKind))),
            ("invalid mutability", changedRow(firstParameter, "mutable", "2")),
            ("missing unit \(first["unit"]!)", changedRow(firstParameter, "unit", nil)),
            ("duplicate kind ID \(kind["id"]!) (\(kind["name"]!))", duplicatedRow(firstKind, parameter: false))
        ]
        for (index, control) in controls.enumerated() {
            let text = control.text
            if text == raw {
                print("CATALOG control \(index + 1) (\(control.name)) did not apply")
                exit(1)
            }
            if FeaturesView.generatorParameters(text) != nil {
                print("CATALOG control \(index + 1) (\(control.name)) accepted")
                exit(1)
            }
        }
        print("CATALOG ok fields=\(parameters.count) controls=\(controls.count)")
        try nativeParameters(CommandLine.arguments[2])
    }

    static func fields(_ line: String) -> [String: String] {
        Dictionary(uniqueKeysWithValues: line.split(separator: " ").dropFirst().map {
            let pair = $0.split(separator: "=", maxSplits: 1)
            return (String(pair[0]), String(pair[1]))
        })
    }

    static func changed(_ lines: [String], _ index: Int, _ key: String, _ value: String?) -> String {
        var copy = lines
        copy[index] = copy[index].split(separator: " ").compactMap { word -> String? in
            guard word.hasPrefix(key + "=") else { return String(word) }
            return value.map { key + "=" + $0 }
        }.joined(separator: " ")
        return copy.joined(separator: "\n") + "\n"
    }

    static func changedRange(_ raw: String) -> String {
        let lines = raw.split(separator: "\n").map(String.init)
        let index = lines.firstIndex { line in
            guard line.hasPrefix("PARAM "), let low = Int64(fields(line)["lo"] ?? ""),
                  let high = UInt64(fields(line)["hi"] ?? "") else { return false }
            return low >= 0 && low < Int64.max && UInt64(low) < high
        }!
        let result = changed(lines, index, "lo", String(Int64(fields(lines[index])["lo"]!)! + 1))
        precondition(result != raw && NativeParams(result) != nil, "range mutation must change valid metadata")
        return result
    }

    static func nativeParameters(_ path: String) throws {
        let raw = try String(contentsOfFile: path, encoding: .utf8)
        guard let catalog = NativeParams(raw) else { fatalError("native parameters refused") }
        precondition(catalog.missingTypedStatements.isEmpty, "typed statement missing native metadata")
        precondition(catalog.missingNumericControls.isEmpty, "numeric control missing native metadata")
        let lines = raw.split(separator: "\n").map(String.init)
        let parameter = lines.firstIndex { $0.hasPrefix("PARAM ") }!
        let numeric = lines.firstIndex { $0.hasPrefix("PARAM ") && fields($0)["type"] == "u8" }!
        let signed = lines.firstIndex { $0.hasPrefix("PARAM ") && fields($0)["type"] == "i16" }!
        let wide = lines.firstIndex { $0.hasPrefix("PARAM ") && fields($0)["hi"] == String(UInt64.max) }!
        let statement = lines.firstIndex { $0.hasPrefix("STATEMENT ") }!
        let form = lines.firstIndex { $0.hasPrefix("FORM ") }!
        let footer = fields(lines.last!)
        func duplicate(_ index: Int) -> String {
            var copy = lines; copy.insert(copy[index], at: index)
            return copy.joined(separator: "\n") + "\n"
        }
        let controls: [(String, String)] = [
            ("version", changed(lines, 0, "version", "2")),
            ("count", changed(lines, lines.count - 1, "count", String(Int(footer["count"]!)! - 1))),
            ("statement count", changed(lines, lines.count - 1, "statements", "0")),
            ("form count", changed(lines, lines.count - 1, "forms", "0")),
            ("duplicate parameter", duplicate(parameter)),
            ("duplicate statement", duplicate(statement)),
            ("duplicate form", duplicate(form)),
            ("missing unit", changed(lines, parameter, "unit", nil)),
            ("unknown statement", changed(lines, parameter, "statement", "missing_statement")),
            ("unknown parameter form", changed(lines, parameter, "form", "missing_form")),
            ("unknown form statement", changed(lines, form, "statement", "missing_statement")),
            ("unknown form match", changed(lines, form, "match", "unknown")),
            ("nonnumeric bound", changed(lines, parameter, "lo", "0")),
            ("unsigned negative", changed(lines, numeric, "lo", "-1")),
            ("inverted bounds", changed(lines, numeric, "lo", "256")),
            ("unsigned width", changed(lines, numeric, "hi", "256")),
            ("signed width", changed(lines, signed, "lo", "-32769")),
            ("wide overflow", changed(lines, wide, "hi", String(UInt64.max) + "0")),
            ("type", changed(lines, numeric, "type", "u08")),
            ("zero scale", changed(lines, numeric, "scale", "0")),
            ("mutability", changed(lines, numeric, "mutable", "2")),
            ("unknown row", raw.replacingOccurrences(of: "FAMILY id=", with: "UNKNOWN id=")),
            ("duplicate key", raw.replacingOccurrences(of: lines[parameter], with: lines[parameter] + " unit=extra")),
            ("extra key", raw.replacingOccurrences(of: lines[parameter], with: lines[parameter] + " extra=1")),
            ("unknown family", raw.replacingOccurrences(of: "FAMILY id=generator", with: "FAMILY id=unknown"))
        ]
        for (name, mutant) in controls {
            precondition(mutant != raw, "control did not apply: " + name)
            precondition(NativeParams(mutant) == nil, "malformed metadata accepted: " + name)
        }
        let display = catalog.displayLines
        for parameter in catalog.parameters {
            let expected = fields(lines.first { $0.hasPrefix("PARAM id=" + parameter["id"] + " ") }!)
            precondition(parameter.values == expected, "native field changed while parsing")
            let row = display.first { $0.contains("[" + parameter["id"] + "]") }!
            for key in ["clamp", "default", "scale", "unit", "depends", "choices"] {
                precondition(row.contains(parameter[key]), "display omitted " + key)
            }
        }
        precondition(catalog.parameters.contains { $0["hi"] == String(UInt64.max) }, "wide unsigned bound was rounded")
        precondition(catalog.engine == lines[0].replacingOccurrences(of: "PARAMS version=1 engine=", with: ""))
        let removed = catalog.statements.first { InstrTypedStatements.names.contains($0["name"]) }!
        let missingID = removed["id"]
        let missingParameters = catalog.parameters.filter { $0["statement"] == missingID }.count
        let missingForms = catalog.forms.filter { $0["statement"] == missingID }.count
        var missingLines = lines.filter { line in
            let values = fields(line)
            return !((line.hasPrefix("STATEMENT ") && values["id"] == missingID) ||
                     (["PARAM ", "FORM "].contains(where: { line.hasPrefix($0) }) && values["statement"] == missingID))
        }
        missingLines[missingLines.count - 1] = "PARAMS count=\(catalog.parameters.count - missingParameters) statements=\(catalog.statements.count - 1) forms=\(catalog.forms.count - missingForms)"
        guard let missing = NativeParams(missingLines.joined(separator: "\n") + "\n") else { fatalError("missing-statement control malformed") }
        precondition(missing.missingTypedStatements == [removed["name"]], "coverage control did not detect removed typed statement")
        let unchanged = NativeParams(changedRange(raw))!
        precondition(unchanged.parameters.map(\.values) != catalog.parameters.map(\.values), "range control did not change metadata")
        print("PARAMS ok rows=\(catalog.parameters.count) statements=\(catalog.statements.count) forms=\(catalog.forms.count) typed=\(InstrTypedStatements.names.count) controls=\(controls.count + 2) numeric_mappings=\(NativeParams.numericControlCount)")
    }
}
