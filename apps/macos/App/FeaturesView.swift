// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// FeaturesView.swift: shows FEATURES.md from the bundle and the build's
// build details (source description and engine line, from Info.plist).

import SwiftUI

struct FeaturesView: View {
    private let lines: [String]?
    private let head: String
    private let engineLine: String

    init() {
        let plist = Bundle.main.infoDictionary ?? [:]
        head = (plist["FMSHeadCommit"] as? String) ?? "unknown (no FMSHeadCommit in Info.plist)"
        engineLine = (plist["FMSEngineLine"] as? String) ?? "unknown (no FMSEngineLine in Info.plist)"
        if let url = Bundle.main.url(forResource: "FEATURES", withExtension: "md"),
           let text = try? String(contentsOf: url, encoding: .utf8) {
            var content = FeaturesView.joinParagraphs(text.components(separatedBy: "\n").filter { !$0.hasPrefix("<!--") })
            if let catalog = Bundle.main.url(forResource: "gen-params", withExtension: "txt"),
               let data = try? String(contentsOf: catalog, encoding: .utf8),
               let parameters = FeaturesView.generatorParameters(data) {
                content += parameters
            } else {
                content += ["## Generator parameters", "The build's parameter catalog is unavailable or invalid."]
            }
            if let parameters = NativeParams.bundled {
                content += parameters.displayLines
            } else {
                content += ["## Native parameters", "The build's native parameter inventory is unavailable or invalid."]
            }
            lines = content
        } else {
            lines = nil
        }
    }

    var body: some View {
        ScrollView { FeaturesContent(lines: lines, head: head, engineLine: engineLine) }
    }

    /// The native parser exports gen/params.def. Keep decimal values as
    /// strings: display never rounds Q48 defaults or invents validation bounds.
    static func generatorParameters(_ text: String) -> [String]? {
        let raw = text.split(separator: "\n").map(String.init)
        guard raw.first == "GEN_PARAMS version=1", let end = raw.last,
              end.hasPrefix("GEN_PARAMS count="),
              let count = Int(end.dropFirst("GEN_PARAMS count=".count)) else { return nil }
        var kinds: [Int: String] = [:]
        var fields: [[String: String]] = []
        var ids = Set<Int>()
        for row in raw.dropFirst().dropLast() {
            let words = row.split(separator: " ")
            var values: [String: String] = [:]
            for word in words.dropFirst() {
                let pair = word.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
                guard pair.count == 2, values[String(pair[0])] == nil else { return nil }
                values[String(pair[0])] = String(pair[1])
            }
            guard let id = values["id"].flatMap(Int.init), let name = values["name"], !name.isEmpty else { return nil }
            if words.first == "GEN_KIND" {
                guard id > 0, id <= 16, kinds[id] == nil, values.count == 2 else { return nil }
                kinds[id] = name
            } else if words.first == "GEN_PARAM" {
                let required: Set<String> = ["id", "name", "kinds", "slot", "type", "min", "max", "unit", "mutable", "has_default", "default"]
                guard Set(values.keys) == required, ids.insert(id).inserted,
                      let lo = values["min"].flatMap(Int64.init),
                      let hi = values["max"].flatMap(Int64.init), lo <= hi,
                      values["default"].flatMap(Int64.init) != nil,
                      ["0", "1"].contains(values["mutable"]!),
                      ["0", "1"].contains(values["has_default"]!) else { return nil }
                fields.append(values)
            } else { return nil }
        }
        guard !kinds.isEmpty, !fields.isEmpty, fields.count == count else { return nil }
        let mask = kinds.keys.reduce(0) { $0 | (1 << ($1 - 1)) }
        var result = ["## Generator parameters", "Read-only catalog from this build's shared gen/params.def table. Bounds are outer limits; the native parser also checks coupled values, references and text expansions. Records play live generator banks. Instrument editing and live-bank takes are not implemented."]
        for field in fields {
            guard let selected = field["kinds"].flatMap(Int.init), selected >= 0, selected & ~mask == 0 else { return nil }
            let scope = selected == 0 ? "session" : selected == mask ? "all sources" : kinds.keys.sorted().filter { selected & (1 << ($0 - 1)) != 0 }.map { kinds[$0]! }.joined(separator: ", ")
            let change = field["mutable"] == "1" ? "runtime updates" : "header only"
            let initial = field["has_default"] == "1" ? "default " + field["default"]! : "no default"
            result.append("- **" + field["name"]! + "** (ID " + field["id"]! + "; " + scope + "; " + field["type"]! + "): " + field["min"]! + " to " + field["max"]! + " " + field["unit"]! + "; " + initial + "; " + change + ".")
        }
        return result
    }

    /// Joins wrapped text lines onto the paragraph or bullet they continue.
    static func joinParagraphs(_ raw: [String]) -> [String] {
        func starts(_ t: String) -> Bool {
            t.isEmpty || t.hasPrefix("#") || t.hasPrefix("- ") || t.hasPrefix("* ") || t.hasPrefix("|") || t.hasPrefix("```")
        }
        var out: [String] = []
        for l in raw {
            let t = l.trimmingCharacters(in: .whitespaces)
            if !starts(t), let prev = out.last {
                let p = prev.trimmingCharacters(in: .whitespaces)
                if !p.isEmpty && !p.hasPrefix("#") && !p.hasPrefix("|") {
                    out[out.count - 1] = prev + " " + t
                    continue
                }
            }
            out.append(l)
        }
        return out
    }
}

enum FeatureBlock {
    case line(String)
    case table([[String]])   // header row first; the |---| row dropped
}

struct FeaturesContent: View {
    let lines: [String]?
    let head: String
    let engineLine: String

    private var blocks: [FeatureBlock] {
        var out: [FeatureBlock] = []
        var rows: [[String]] = []
        func flush() { if !rows.isEmpty { out.append(.table(rows)); rows = [] } }
        for l in lines ?? [] {
            let t = l.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("|") {
                var cells = t.split(separator: "|", omittingEmptySubsequences: false).map { $0.trimmingCharacters(in: .whitespaces) }
                if cells.first == "" { cells.removeFirst() }
                if cells.last == "" { cells.removeLast() }
                if !cells.allSatisfy({ $0.allSatisfy { "-: ".contains($0) } }) { rows.append(cells) }
            } else {
                flush()
                out.append(.line(l))
            }
        }
        flush()
        return out
    }

    var body: some View {
            VStack(alignment: .leading, spacing: 4) {
                Card(title: "Build") {
                    HStack { Text("Source").foregroundStyle(.secondary); Text(head).font(.callout.monospaced()) }
                    HStack(alignment: .top) {
                        Text("engine").foregroundStyle(.secondary)
                        Text(engineLine).font(.callout.monospaced()).textSelection(.enabled)
                    }
                }
                .padding(.bottom, 8)
                if lines != nil {
                    ForEach(Array(blocks.enumerated()), id: \.offset) { _, b in
                        switch b {
                        case .line(let l): line(l)
                        case .table(let rows): table(rows)
                        }
                    }
                } else {
                    Text("FEATURES.md is not in this bundle (run the app built by make).")
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: 900, alignment: .leading)
            .padding(18)
            .textSelection(.enabled)
    }

    private func inline(_ s: String) -> Text {
        if let a = try? AttributedString(markdown: s) { return Text(a) }
        return Text(s)
    }

    private func table(_ rows: [[String]]) -> some View {
        let n = rows.map(\.count).max() ?? 0
        return Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 4) {
            ForEach(Array(rows.enumerated()), id: \.offset) { r, cells in
                GridRow {
                    ForEach(0..<n, id: \.self) { c in
                        inline(c < cells.count ? cells[c] : "")
                            .font(r == 0 ? .callout.bold() : .callout)
                            .foregroundStyle(r == 0 ? .secondary : .primary)
                    }
                }
                if r == 0 { Divider().gridCellUnsizedAxes(.horizontal) }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.05)))
        .padding(.vertical, 4)
    }

    @ViewBuilder private func line(_ l: String) -> some View {
        let t = l.trimmingCharacters(in: .whitespaces)
        let indent = CGFloat(l.prefix(while: { $0 == " " }).count) * 6
        if t.isEmpty {
            Spacer().frame(height: 6)
        } else if t.hasPrefix("### ") {
            inline(String(t.dropFirst(4))).font(.headline).padding(.top, 6)
        } else if t.hasPrefix("## ") {
            inline(String(t.dropFirst(3))).font(.title3.bold()).padding(.top, 10)
        } else if t.hasPrefix("# ") {
            inline(String(t.dropFirst(2))).font(.title.bold())
        } else if t.hasPrefix("- ") || t.hasPrefix("* ") {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("•").foregroundStyle(.secondary)
                inline(String(t.dropFirst(2)))
            }
            .padding(.leading, indent)
        } else if t.hasPrefix("```") {
            EmptyView()
        } else {
            inline(t).padding(.leading, indent)
        }
    }
}
