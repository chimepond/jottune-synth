// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// list_warning.swift: the smallest app that logs AppKit's "reentrant
// operation in its NSTableView delegate" warning, which the app logs when
// the Records sidebar fills . Nothing of the app is in
// it: one SwiftUI List of N plain Text rows. Build and run it by hand:
//
//   swiftc -O -target arm64-apple-macos14.0 -parse-as-library \
//       tools/list_warning.swift -o build/list_warning
//   N=300 build/list_warning          # quit it after a few seconds
//   N=300 SPLIT=1 build/list_warning  # the List as a split view's sidebar
//
// and count "reentrant" on its stderr. On macOS 26.5 (2026-10-17): N=300
// logs it once, N=100 not; as a split view's sidebar N=100 logs it too.

import SwiftUI

@main struct ListWarning: App {
    let n = Int(ProcessInfo.processInfo.environment["N"] ?? "300") ?? 300
    let split = ProcessInfo.processInfo.environment["SPLIT"] != nil
    var body: some Scene {
        WindowGroup {
            if split {
                NavigationSplitView { List(0..<n, id: \.self) { Text("row \($0)") } } detail: { Text("detail") }
            } else {
                List(0..<n, id: \.self) { Text("row \($0)") }
            }
        }
    }
}
